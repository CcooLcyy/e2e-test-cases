"""从本次创建的六路华为行为模型验收 RTU 双端采集。"""
import json
import logging
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from testbench.case_plan import (
    ContinueAfterFailure, DESKTOP_FINISH, DESKTOP_START, SITE_CONNECT, SLAVESIM_BROWSER_READY, Step, case_plan,
    run_steps, track_fixture_context, track_fixture_step,
)
from case_support.case_metadata import execution_logic
from case_support.huawei_fleet import READ_POINTS
from case_support.huawei_fleet_run import DEVICE_EXPECTED, STAGES, HuaweiFleetRun, device_step_title
from case_support.huawei_rtu_page import HuaweiRtuPage
from testbench.login_page import PlatformLoginPage, load_login_account
from case_support.login_plan import login_steps
from testbench.modbus_environment import HUAWEI_CASE_ID, validate_selected_modbus_cases
from testbench.model import validate_slavesim_url
from testbench.workspace import local_config


pytestmark = [pytest.mark.case_group("Modbus RTU"), pytest.mark.requires_modbus]

FLEET_PREPARE = Step("fleet.prepare", "检查 slaveSim 入口并读取测试账号", "已提供模拟器页面、等待时限和专用账号")
SIMULATOR_START = Step("fleet.simulator.open", "打开 slaveSim 观察会话", "运行页面显示新的状态刷新")
SIMULATOR_FINISH = Step("fleet.simulator.close", "关闭 slaveSim 观察会话", "释放本次浏览器资源")
OPEN_LINKS = Step("fleet.links.open", "打开上位机当前目标的 RTU 配置", "当前设备的 RTU 连接列表可用")
SIMULATOR_CREATE = Step("fleet.simulator.create", "读取并替换 slaveSim 全部总线，创建六路 21 台华为机群",
                        "保存应用后从运行页读回六路实际设备，核对固定配置路径和站号，检查已有连接是否占用实际设备")
FLEET_CLEANUP = Step("fleet.links.cleanup", "停止并删除本次机群连接", "本次创建的连接均已清理")
SIMULATOR_CLEANUP = Step("fleet.simulator.cleanup", "清空 slaveSim 全部总线", "读取配置确认没有剩余总线")
LOGIN_STEPS = login_steps("login")


@pytest.fixture
def huawei_fleet(request):
    """先验证环境，再打开两端；失败现场先于本例浏览器关闭。"""
    from case_support.huawei_slavesim_page import HuaweiSlaveSimPage

    with track_fixture_step(request, FLEET_PREPARE):
        task_path = os.environ.get("TESTBENCH_TASK")
        if not task_path:
            raise RuntimeError("请从测试上位机执行华为机群用例，以读取任务环境和模拟器地址")
        task = json.loads(Path(task_path).read_text(encoding="utf-8"))
        environment = task["environment"]
        validate_selected_modbus_cases(environment, [HUAWEI_CASE_ID])
        url = validate_slavesim_url(environment.get("slavesim_url", ""), required=True)
        timeout = float(environment.get("modbus_acquisition_timeout", 300))
        account = load_login_account(local_config("login-account.json"))
    with track_fixture_step(request, SLAVESIM_BROWSER_READY):
        browser = request.getfixturevalue("slavesim_browser")
    desktop = request.getfixturevalue("fresh_desktop")
    desktop.metadata["华为验收阶段"] = "前置条件"
    upper = HuaweiRtuPage(desktop)
    with track_fixture_context(request, SIMULATOR_START, SIMULATOR_FINISH,
                               HuaweiSlaveSimPage(desktop, url, browser=browser)) as simulator:
        scenario = None
        setup_failed = False
        simulator_preparation_started = False

        def capture_failure(description):
            if scenario is not None:
                scenario.capture_both(description)
                return
            # 预置资源无法观察时仍保留两端现场。
            for source, capture in (("slaveSim", simulator.capture), ("DSP上位机", desktop.capture)):
                try:
                    capture("huawei-runtime-preparation-failed", description=f"{source}：{description}")
                except Exception as error:
                    logging.getLogger(__name__).exception("%s 运行时准备失败现场采集异常", source)
                    desktop.metadata.setdefault("华为取证错误", []).append(f"{source}：{error}")

        try:
            run_steps(request, LOGIN_STEPS, SimpleNamespace(desktop=desktop, page=PlatformLoginPage(desktop), account=account))
            with track_fixture_step(request, SITE_CONNECT):
                upper.connect_site()
            with track_fixture_step(request, OPEN_LINKS):
                upper.open_current_links()
            run_id = uuid4().hex[:16]
            with track_fixture_step(request, SIMULATOR_CREATE):
                simulator_preparation_started = True
                config, buses = simulator.prepare_owned_fleet(timeout, run_id, check_paths=upper.assert_serial_paths_available)
            scenario = HuaweiFleetRun(desktop, simulator, upper, config, run_id, buses=buses)
            desktop.metadata["华为验收阶段"] = "准备本例连接并驱动模拟器工况"
            yield scenario
        except BaseException as error:
            setup_failed = True
            desktop.failure = desktop.failure or str(error)
            capture_failure("前置阶段失败，保留两端现场")
            raise
        finally:
            business_failed = any(report.failed for report in getattr(request.node, "_bench_reports", []))
            stop_path = os.environ.get("TESTBENCH_STOP")
            case_stopped = bool(stop_path and Path(stop_path).exists())
            had_failure = setup_failed or sys.exc_info()[0] is not None or business_failed or case_stopped
            try:
                if not setup_failed and (business_failed or case_stopped):
                    capture_failure("机群验收失败或中断，清理前保留两端现场")
                if scenario is not None:
                    try:
                        scenario.write_report()
                    except Exception as error:
                        desktop.metadata["华为报告写入错误"] = str(error)
                        logging.getLogger(__name__).exception("华为汇总报告写入失败")
                        if not had_failure:
                            raise
            finally:
                cleanup_failure = None
                try:
                    if upper.creation_started:
                        with track_fixture_step(request, FLEET_CLEANUP):
                            upper.cleanup()
                except Exception as error:
                    cleanup_failure = error
                    desktop.metadata["华为连接清理错误"] = str(error)
                    logging.getLogger(__name__).exception("华为连接清理失败")
                    capture_failure("上位机连接清理失败，清空模拟器前保留两端现场")
                finally:
                    if simulator_preparation_started:
                        try:
                            with track_fixture_step(request, SIMULATOR_CLEANUP):
                                simulator.clear_all_buses()
                        except Exception as error:
                            if cleanup_failure is None:
                                cleanup_failure = error
                            desktop.metadata["华为模拟器清理错误"] = str(error)
                            logging.getLogger(__name__).exception("华为模拟器资源清理失败")
                if cleanup_failure is not None and not (had_failure or sys.exc_info()[0] is not None):
                    raise cleanup_failure


def _prepare_device_step(route, station):
    def prepare(fleet):
        bus = fleet.buses[route - 1]
        fleet.create_device(bus, bus["devices"][station - 1])

    first = (route, station) == (1, 1)
    action = "新建" if first else "复制并配置"
    expected = (f"按实际串口和站号创建连接，录入并回读 {len(READ_POINTS)} 个点后启动轮询" if first else
                f"复制首条连接，保存本台连接名、串口和站号，回读 {len(READ_POINTS)} 个点后启动轮询")
    return Step(f"fleet.prepare.route-{route}.station-{station}", f"{action}第{route}路站号{station}的 RTU 连接",
                expected, prepare,
                group="准备本例 RTU 连接")


PREPARE_STEPS = (
    Step("fleet.source", "读取新建的六路 21 台 slaveSim 模型", "核对各台站号、实际设备和固定配置路径，读取寄存器地址",
         HuaweiFleetRun.check_sources),
    *(_prepare_device_step(route, station) for route in range(1, 7) for station in range(1, route + 1)),
)


def _device_step(stage_index, stage, route, station):
    def observe(fleet):
        bus = fleet.buses[route - 1]
        device = bus["devices"][station - 1]
        error = fleet.observe_device(stage, bus, device)
        if error is not None:
            raise ContinueAfterFailure(str(error)) from error

    return Step(f"fleet.stage-{stage_index}.route-{route}.station-{station}",
                device_step_title(stage, route, station), DEVICE_EXPECTED, observe, group=stage)


def _stage_steps(index, stage):
    if index > 1:
        yield Step(f"fleet.stage-{index}.apply", f"通过 slaveSim 设置全机群{stage}工况", "21台模型确认应用本轮工况",
                   lambda fleet: fleet.apply_stage(stage), group=stage)
    for route in range(1, 7):
        for station in range(1, route + 1):
            yield _device_step(index, stage, route, station)


BUSINESS_STEPS = (
    *(step for index, stage in enumerate(STAGES, 1) for step in _stage_steps(index, stage)),
    Step("fleet.summary", "核对机群汇总结果", "所有设备工况均已完成且通过，并保留双端证据",
         HuaweiFleetRun.assert_complete),
)


@case_plan(
    FLEET_PREPARE, SLAVESIM_BROWSER_READY, DESKTOP_START, SIMULATOR_START,
    *LOGIN_STEPS, SITE_CONNECT, OPEN_LINKS, SIMULATOR_CREATE,
    *PREPARE_STEPS, *BUSINESS_STEPS, FLEET_CLEANUP, SIMULATOR_CLEANUP, SIMULATOR_FINISH, DESKTOP_FINISH,
)
@execution_logic(f"""
1. 登录上位机并连接现场下位机。清除 slaveSim 原有总线，重建六路分别含 1 至 6 台华为逆变器的模拟总线。固定每台的可用直流功率和电压，关闭扰动。保存应用后从运行页读取各路实际设备（/dev/pts/N），核对固定配置路径，检查已有连接是否占用实际设备，并读取各台站号和寄存器地址。
2. 按 slaveSim 实际设备、站号和寄存器地址，在上位机创建首条 RTU 连接并逐点录入 {len(READ_POINTS)} 个采集点，保存后回读配置。寄存器 32000 配置为发电、待机和限电停运三个 BOOL 位点，32089 配置为 UINT16 设备状态码。其余 20 台通过上位机复制首条连接，修改并回读各自的连接名、实际串口和站号，核对点表与首台一致。每条连接核对后启动轮询。
3. 从初始发电开始，通过 slaveSim 切换有功调节、正无功、负无功、待机、限电停运和恢复发电。每台每轮先按预定工况核对源端的电压、功率、功率因数和状态（含 32000 原始整字值），再与上位机的 {len(READ_POINTS)} 个采集点对照。核对换算、状态位、地址、功能码和正常质量，有变化的点还需更新时间。
4. 147 项设备工况均须在各自时限内达到预期并保存双端证据。记录逐次采样、双端截图和汇总报告，失败时保留现场。停止并删除本例连接，清空 slaveSim 总线并回读确认。
""")
def test_huawei_six_buses_21_devices_match(request, huawei_fleet):
    """华为六路 21 台七工况采集"""
    run_steps(request, (*PREPARE_STEPS, *BUSINESS_STEPS), huawei_fleet)
