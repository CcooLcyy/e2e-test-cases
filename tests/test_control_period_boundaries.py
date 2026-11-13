"""通过 IEC104 遥调、计算遥测和 RTU 写入验证控制周期。"""

from datetime import datetime
from decimal import Decimal
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from selenium.webdriver.common.by import By

from case_support.agc_direction_setup import AgcDirectionSetup
from case_support.avc_period_iec104 import send_burst, snapshot, wait_calculations
from case_support.avc_period_page import AvcPeriodPage
from case_support.case_metadata import execution_logic
from case_support.login_plan import login_steps
from case_support.measurement_expiry_run import SIM_CLEANUP, UPPER_CLEANUP, _await
from case_support.measurement_expiry_simulator import MeasurementExpirySimulator
from case_support.modbus_rtu_page import ModbusRtuPage
from case_support.power_range_run import _environment, _load_account
from case_support.rtu_quality_iec104 import RtuQualityIec104
from testbench.case_plan import (
    DESKTOP_FINISH, DESKTOP_START, MASTER104_FINISH, MASTER104_START, SITE_CONNECT, SLAVESIM_BROWSER_READY,
    Step, case_plan, run_steps, track_fixture_context, track_fixture_step,
)
from testbench.login_page import PlatformLoginPage
from testbench.master104_commands import Master104CommandSender


pytestmark = pytest.mark.requires_modbus
MEASUREMENT_TAGS = {"agc": "active-power", "avc": "reactive-power", "voltage": "bus-voltage"}
SETPOINT = "setpoint"


def _prepare_simulator(context):
    context.rtu.open_current_links()
    context.sim_started = True
    specs = {"agc": (40, 0)}
    if context.avc is not None:
        specs.update(avc=(0, 0), voltage=(220,))
    context.points = context.simulator.prepare(context.name, specs, context.rtu.assert_serial_paths_available)


def _prepare_rtu(context):
    page = context.rtu
    for role, points in context.points.items():
        measurement = points[0]
        name = f"{context.name}-{role}"
        page.create_connection(name, measurement["actual_device"], 9600, 8, 1, "N",
                               measurement["station_id"], 1000)
        page.add_point(MEASUREMENT_TAGS[role], address=measurement["address"])
        if len(points) == 2:
            page.add_point(SETPOINT, function=6, address=points[1]["address"])
        page.assert_saved_configuration()
        page.start()
        page.wait_value(MEASUREMENT_TAGS[role], measurement["value"], 0, context.timeout)
        context.connections[role] = name


def _prepare_agc(context):
    saved = context.agc.create_group(
        context.name, "active-member", {"name": context.connections["agc"]}, MEASUREMENT_TAGS["agc"], SETPOINT,
    )
    context.agc_target_tag = saved["command_tag"]


def _prepare_avc(context):
    context.avc.create_group(
        context.avc_name, mode="voltage", calculation_period=15, command_period=30,
        voltage_tag=MEASUREMENT_TAGS["voltage"], voltage_connection=context.connections["voltage"],
        target_tag=context.avc_target_tag, rtu_connection=context.connections["avc"],
        q_measurement_tag=MEASUREMENT_TAGS["avc"], q_setpoint_tag=SETPOINT,
    )


def _prepare_iec104(context):
    page = context.iec104
    page.create()
    context.agc_ioa = page.import_point(
        context.name, context.agc_target_tag, f"{context.name}-104-p", module="AGC", business_type="遥调",
    )
    if context.agc_calculation:
        context.agc_calculation_ioa = page.import_point(
            context.name, "AGC总有功目标点", f"{context.name}-104-agc-result", module="AGC",
        )
        assert context.agc_calculation_ioa != context.agc_ioa, "AGC 目标遥调和计算遥测点号重复"
    if context.avc is not None:
        context.avc_ioa = page.import_point(
            context.avc_name, context.avc_target_tag, f"{context.name}-104-v", module="AVC", business_type="遥调",
        )
        context.avc_calculation_ioa = page.import_point(
            context.avc_name, "电压偏差", f"{context.name}-104-deviation", module="AVC",
        )
        assert len({context.agc_ioa, context.avc_ioa, context.agc_calculation_ioa,
                    context.avc_calculation_ioa}) == 4, "IEC104 用例点号重复"
    page.check_routes()
    page.start()


def _connect(context):
    context.master.prepare_target(context.host, context.iec104.port, 1)
    context.master.connect()
    context.agc.start()
    if context.avc is not None:
        context.avc.start()


def _wait_command(context, role, value, after, *, seconds=0):
    def first_write(_, writes):
        command = next((entry for entry in writes[role] if entry["index"] > after), None)
        if command is None:
            return False
        assert command["value"] == value, f"{role} 下一条设定应为 {value}，实际为 {command['value']}"
        return command if command["ack"] else False

    return _await(context, first_write, f"未收到 {role} 设定 {value} 的正常写应答",
                  timeout=max(context.timeout, seconds))


def _send(context, ioa, value):
    return Master104CommandSender(context.master, 1, ioa).send_target_kw(value, timeout=context.timeout)


def _record_interval(context, role, first, second, minimum):
    interval = (second["time"] - first["time"]).total_seconds()
    actual = {"首条": first["value"], "下一条": second["value"], "写入间隔秒": interval}
    with context.desktop.step(f"核对 {role.upper()} 实际成员写入间隔", expected=f"至少 {minimum} 秒", actual=actual):
        assert interval >= minimum, f"{role.upper()} 两条成员命令间隔 {interval} 秒，小于 {minimum} 秒"


def _first_agc(context):
    after = len(context.simulator.events()) - 1
    context.first_agc_input = _send(context, context.agc_ioa, 40)
    context.first_agc = _wait_command(context, "agc", 40, after, seconds=10)


def _burst_agc(context):
    # 104 发送早于首条 RTU 写入，以其定截止时点可证明输入在首条写入后四秒内完成。
    deadline = context.first_agc_input["frames"]["sent"]["at_ms"] + 4000
    send_burst(context.master, 1, context.agc_ioa, range(50, 58), deadline)


def _verify_agc_burst(context):
    second = _wait_command(context, "agc", 57, context.first_agc["index"], seconds=10)
    _record_interval(context, "agc", context.first_agc, second, 4)


def _save_agc_periods_while_running(context):
    group = context.agc
    group._control_view("控制策略")
    group._select_owned_group()
    group._draft_opened = True
    group._click(group._button("编辑", group.OVERVIEW), "编辑运行中的本例 AGC 控制组")
    restart_notice = (By.XPATH, group.GROUP_DIALOG + "//*[normalize-space(.)="
                      "'控制组正在运行：保存时会先停止控制组，保存后恢复运行。']")
    group._until(lambda _: group._visible(restart_notice), "AGC 未显示保存会停止并恢复运行的提示")
    group._fill_group_field(group.GROUP_DIALOG, "calculation_execution_period_seconds", 3)
    group._fill_group_field(group.GROUP_DIALOG, "command_control_period_seconds", 7)
    conn_id = group.group["conn_id"]
    group._submit_group("保存周期并由上位机恢复运行")
    group._until(
        lambda _: (overview := group._selected_overview())
        and "运行中" in {badge.text for badge in overview.find_elements(By.CSS_SELECTOR, ".agc-runtime-heading .ant-tag")},
        "保存后本例 AGC 控制组未恢复运行",
    )
    group.select_group(context.name, conn_id)
    details = group._until(lambda _: group._visible(group.DETAILS), "AGC 配置与点位详情未显示")
    if details.get_attribute("aria-expanded") != "true":
        group._click(group.DETAILS, "展开本例 AGC 配置与点位详情")
    assert group._description(group._selected_overview(), "控制周期") == "计算 3 秒 / 命令 7 秒"


def _verify_saved_agc_periods(context):
    first = _agc_calculations(context, context.first_agc_input, 40, 1)[0]
    send_burst(context.master, 1, context.agc_ioa, (70,), first["received_ms"] + 2500)
    samples = _agc_calculations(context, context.first_agc_input, 40, 2)
    interval = _calculation_interval(samples[0], samples[1])
    assert [Decimal(sample["value"]) for sample in samples] == [40, 70], samples
    assert 3 <= interval < 6, f"AGC 相邻计算间隔 {interval} 秒，未观察到新 3 秒周期：{samples}"
    second = _wait_command(context, "agc", 70, context.first_agc["index"], seconds=17)
    _record_interval(context, "agc", context.first_agc, second, 7)


def _calculation_interval(previous, current):
    return (datetime.fromisoformat(current["at"]) - datetime.fromisoformat(previous["at"])).total_seconds()


def _agc_calculations(context, command, first_value, count):
    samples = wait_calculations(
        context.master, 1, context.agc_calculation_ioa, command["sent_frame_id"], count,
        max(context.timeout, 12), first_value=first_value,
    )
    context.master.metadata["AGC计算遥测"] = context.master.metadata.pop("AVC计算遥测")
    return samples


def _avc_calculations(context, count, seconds):
    return wait_calculations(
        context.master, 1, context.avc_calculation_ioa, context.first_avc_input["sent_frame_id"], count,
        max(context.timeout, seconds), first_value=10,
    )


def _first_avc(context):
    after = len(context.simulator.events()) - 1
    context.first_avc_input = _send(context, context.avc_ioa, 230)
    context.first_avc = _wait_command(context, "avc", 20, after, seconds=20)


def _send_new_parallel_targets(context):
    # 用主站接收首轮计算报文的时钟给同一主站提交新目标，后续断言第二轮已采用 240 V。
    first = _avc_calculations(context, 1, 20)[0]
    send_burst(context.master, 1, context.avc_ioa, (235, 240), first["received_ms"] + 10000)
    context.second_agc_input = _send(context, context.agc_ioa, 70)
    context.second_agc = _wait_command(context, "agc", 70, context.first_agc["index"], seconds=10)
    _record_interval(context, "agc", context.first_agc, context.second_agc, 4)


def _verify_parallel_commands(context):
    second_avc = _wait_command(context, "avc", 40, context.first_avc["index"], seconds=55)
    _record_interval(context, "avc", context.first_avc, second_avc, 30)
    assert context.first_avc["time"] < context.second_agc["time"] < second_avc["time"], (
        f"AGC 的 70 kW 应在两条 AVC 命令之间送达：AGC {context.second_agc}，AVC {context.first_avc}、{second_avc}"
    )


def _verify_parallel_calculations(context):
    agc_samples = _agc_calculations(context, context.second_agc_input, 70, 2)
    agc_interval = _calculation_interval(*agc_samples)
    assert [Decimal(sample["value"]) for sample in agc_samples] == [70, 70], agc_samples
    assert 1 <= agc_interval < 3, f"AGC 相邻计算间隔 {agc_interval} 秒，未观察到 1 秒周期：{agc_samples}"
    samples = _avc_calculations(context, 3, 50)
    assert [Decimal(sample["value"]) for sample in samples] == [10, 20, 20], samples
    for previous, current in zip(samples, samples[1:]):
        interval = _calculation_interval(previous, current)
        assert 15 <= interval < 30, f"AVC 相邻计算间隔 {interval} 秒，未观察到 15 秒周期：{samples}"


ENVIRONMENT = Step("period.environment", "核对 slaveSim 和独立 IEC104 主站入口", "模拟器、主站及驱动可用")
SIM_START = Step("period.sim.start", "打开本例 slaveSim 会话", "模拟器配置页和报文入口可访问")
SIM_FINISH = Step("period.sim.finish", "关闭本例 slaveSim 会话", "独立浏览器会话已关闭")
LOGIN = (Step("period.account", "读取测试账号", "专用账号配置有效", _load_account), *login_steps("period.login"))
PREPARE = (
    Step("period.sim", "重建本例 RTU 模拟总线", "实际串口、站号和各初始寄存器读回一致", _prepare_simulator),
    Step("period.rtu", "配置、启动并核对 RTU 量测和写入点", "已收到初始量测，设定点已保存", _prepare_rtu),
    Step("period.agc", "建立 AGC 控制组", "周期直分配，计算 1 秒、命令间隔 4 秒", _prepare_agc),
)
PREPARE_AVC = Step("period.avc", "建立 AVC 定压控制组", "计算 15 秒、命令间隔 30 秒", _prepare_avc)
PREPARE_104 = Step("period.104", "建立本例 IEC104 点表和路由", "保存、读回本例点号并启动被控站", _prepare_iec104)
CONNECT = Step("period.connect", "连接专用 IEC104 主站并启动本例控制组", "主站与本例控制组已运行", _connect)
FIRST_AGC = Step("period.agc.first", "遥调总有功 40 kW", "AGC 成员收到 40 kW 写入并应答", _first_agc)
BURST = (
    FIRST_AGC,
    Step("period.agc.burst", "在首条写入的 4 秒窗口内连续遥调 50～57 kW",
         "各目标均取得肯定确认和终止", _burst_agc),
    Step("period.agc.latest", "观察下一条 AGC 成员命令", "下一条为 57 kW，实际间隔至少 4 秒", _verify_agc_burst),
)
RESTART = (
    Step("period.agc.restart", "在 AGC 控制组运行时保存 3 秒计算、7 秒命令间隔",
         "出现停组后恢复运行的提示，保存后同一控制组自动恢复并显示新周期", _save_agc_periods_while_running),
    FIRST_AGC,
    Step("period.agc.new-interval", "遥调 70 kW，比较两轮计算与两条实际写入",
         "相邻计算约 3 秒，实际写入间隔至少 7 秒", _verify_saved_agc_periods),
)
PARALLEL = (
    FIRST_AGC,
    Step("period.avc.first", "遥调定压目标 230 V", "220 V 实测下 AVC 成员收到 20 kvar 并应答", _first_avc),
    Step("period.parallel.inputs", "连续遥调 235、240 V，再遥调 AGC 70 kW",
         "全部遥调得到肯定确认和终止，AGC 成员实际写入 70 kW", _send_new_parallel_targets),
    Step("period.parallel.commands", "核对两组成员的实际命令时序",
         "AVC 至少间隔 30 秒下发 40 kvar，AGC 的 70 kW 在两条 AVC 写入之间送达", _verify_parallel_commands),
    Step("period.parallel.calculations", "核对 AGC 和 AVC 各自的带时标计算遥测",
         "AGC 按 1 秒周期保持 70 kW，AVC 按 15 秒周期报告 10、20、20 V 电压偏差", _verify_parallel_calculations),
)
CAPTURE = Step("period.capture", "保存主站协议记录", "本例 IEC104 原始报文已记录")


def _prepare_steps(with_avc):
    return (*PREPARE, PREPARE_AVC, PREPARE_104) if with_avc else (*PREPARE, PREPARE_104)


def _plan(*checks, with_avc=False):
    prepare = _prepare_steps(with_avc)
    plan = case_plan(
        ENVIRONMENT, SLAVESIM_BROWSER_READY, DESKTOP_START, *LOGIN, SITE_CONNECT, SIM_START, *prepare,
        MASTER104_START, CONNECT, *checks, CAPTURE, UPPER_CLEANUP, SIM_CLEANUP,
        SIM_FINISH, MASTER104_FINISH, DESKTOP_FINISH,
    )
    description = execution_logic("\n".join(
        f"{index}. {step.action}。预期 {step.expected}。"
        for index, step in enumerate((*prepare, CONNECT, *checks, CAPTURE, UPPER_CLEANUP, SIM_CLEANUP), 1)
    ))

    def decorate(function):
        return description(plan(function))

    return decorate


def _save_frames(context):
    path = context.master.artifacts / "control-period-iec104-frames.json"
    path.write_text(json.dumps(snapshot(context.master)["frames"], ensure_ascii=False, indent=2), encoding="utf-8")


def _cleanup_upper(context):
    context.rtu._cleaning = True
    errors = []
    try:
        cleanup = [("IEC104", context.iec104.cleanup), ("AGC", context.agc.cleanup), ("RTU", context.rtu.cleanup)]
        if context.avc is not None:
            cleanup.insert(2, ("AVC", context.avc.cleanup))
        for label, action in cleanup:
            try:
                action()
            except Exception as error:
                errors.append(f"{label}：{error}")
        if errors:
            raise RuntimeError("本例上位机资源清理失败：" + "；".join(errors))
    finally:
        context.rtu._cleaning = False


def _cleanup(request, context, failure):
    try:
        with track_fixture_step(request, UPPER_CLEANUP):
            _cleanup_upper(context)
    except Exception as error:
        if failure is None:
            raise
        failure.add_note(str(error))
    finally:
        if context.sim_started:
            try:
                with track_fixture_step(request, SIM_CLEANUP):
                    context.simulator.clear_all_buses()
            except Exception as error:
                if failure is None:
                    raise
                failure.add_note(f"模拟总线清理失败：{error}")


def _run(request, checks, *, with_avc=False, agc_calculation=False):
    with track_fixture_step(request, ENVIRONMENT):
        url, timeout = _environment()
    with track_fixture_step(request, SLAVESIM_BROWSER_READY):
        browser = request.getfixturevalue("slavesim_browser")
    desktop = request.getfixturevalue("fresh_desktop")
    name = f"e2e-period-{uuid4().hex[:8]}"
    rtu = ModbusRtuPage(desktop)
    context = SimpleNamespace(
        name=name, timeout=timeout, desktop=desktop, page=PlatformLoginPage(desktop), rtu=rtu,
        agc=AgcDirectionSetup(desktop), avc=AvcPeriodPage(desktop) if with_avc else None,
        avc_name=f"{name}-avc", avc_target_tag=f"{name}-voltage-target",
        iec104=RtuQualityIec104(rtu, name), connections={},
        master=None, sim_started=False, agc_calculation=agc_calculation or with_avc,
    )
    run_steps(request, LOGIN, context)
    with track_fixture_step(request, SITE_CONNECT):
        context.host = rtu.connect_site().rsplit(":", 1)[0]
    with track_fixture_context(request, SIM_START, SIM_FINISH,
                               MeasurementExpirySimulator(desktop, url, browser=browser)) as simulator:
        context.simulator = simulator
        failure = None
        try:
            run_steps(request, _prepare_steps(with_avc), context)
            context.master = request.getfixturevalue("master104")
            run_steps(request, (CONNECT, *checks), context)
            with track_fixture_step(request, CAPTURE):
                _save_frames(context)
        except BaseException as error:
            failure = error
            for source in (desktop, simulator, context.master):
                if source is not None:
                    try:
                        source.capture("control-period-failure", description="控制周期测试清理前现场")
                    except Exception as capture_error:
                        error.add_note(f"截图失败：{capture_error}")
            if context.master is not None:
                try:
                    _save_frames(context)
                except Exception as capture_error:
                    error.add_note(f"主站报文保存失败：{capture_error}")
            raise
        finally:
            _cleanup(request, context, failure)


@pytest.mark.case_group("AGC 控制")
@_plan(*BURST)
def test_agc_burst_respects_command_interval_and_uses_latest_target(request):
    """AGC 高频遥调限频并下发最新目标"""
    _run(request, BURST)


@pytest.mark.case_group("AGC 控制")
@_plan(*RESTART)
def test_agc_running_group_saves_periods_and_restarts(request):
    """运行中的 AGC 保存周期后自动重启并使用新周期"""
    _run(request, RESTART, agc_calculation=True)


@pytest.mark.case_group("AVC 控制")
@_plan(*PARALLEL, with_avc=True)
def test_agc_avc_run_with_independent_periods_and_avc_upper_boundaries(request):
    """AGC 与 AVC 并行运行，验证 AVC 15 秒计算和 30 秒命令间隔"""
    _run(request, PARALLEL, with_avc=True)
