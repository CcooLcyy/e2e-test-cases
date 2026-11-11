"""自行建立专用 RTU 场景并验收 IEC104 对外品质。"""
from decimal import Decimal
import json
import math
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By

from testbench.case_plan import (
    DESKTOP_FINISH, DESKTOP_START, MASTER104_FINISH, MASTER104_START, SITE_CONNECT, SLAVESIM_BROWSER_READY,
    Step, case_plan, run_steps, track_fixture_context, track_fixture_step,
)
from testbench.login_page import PlatformLoginPage, load_login_account
from case_support.case_metadata import execution_logic
from case_support.login_plan import login_steps
from case_support.modbus_rtu_page import ModbusRtuPage, xpath_text
from testbench.model import Environment, validate_slavesim_url
from case_support.rtu_quality_frames import capture_frames, decode_frame
from case_support.rtu_quality_iec104 import RtuQualityIec104
from case_support.rtu_quality_simulator import RtuQualitySimulator
from testbench.workspace import local_config


pytestmark = [pytest.mark.case_group("Modbus RTU"), pytest.mark.requires_modbus]
PHASES = (
    ("首次有效采集", 0x00),
    ("通信超时", 0x40),
    ("设备异常响应", 0xC0),
    ("同值恢复", 0x00),
)


TARGET_STAGES = ((0, 100), (None, 0), (1, 100), (0, 100))


def _load_account(context):
    context.account = load_login_account(local_config("login-account.json"))


def _check_environment():
    task_path = os.environ.get("TESTBENCH_TASK")
    if not task_path:
        raise RuntimeError("前置条件未满足：请从测试上位机运行并启用专用 104 主站")
    task = json.loads(Path(task_path).read_text(encoding="utf-8"))
    environment = Environment.from_dict(task["environment"])
    environment.validate_master104(required=True)
    environment.validate_master104_driver()
    simulator_url = validate_slavesim_url(environment.slavesim_url, required=True)
    timeout = environment.modbus_acquisition_timeout
    if type(timeout) not in (int, float) or not 5 <= timeout <= 300:
        raise ValueError("采集阶段等待时限必须为5至300秒")
    return simulator_url, timeout


def _open_protocol(context):
    context.rtu.open_current_links()


def _target_row(context):
    locator = (By.XPATH, "//div[contains(@class,'protocol-point-card')]//tr[.//td//strong[normalize-space(.)="
               + xpath_text(context.profile["rtu_tag"]) + "]]")
    return context.rtu._visible(locator)


def _prepare_simulator(context):
    context.simulator_preparation_started = True
    context.paths = context.simulator.prepare(context.rtu.assert_serial_paths_available)
    context.desktop.metadata["本例品质模拟串口"] = context.paths


def _prepare_rtu(context, role):
    page = context.rtu
    connection, tag = f"{context.name}-{role}", f"{context.name}-{role}-rtu"
    page.create_connection(connection, context.paths[role], 9600, 8, 1, "N", 1, 1000)
    page.add_point(tag, deadband=1)
    page.assert_saved_configuration()
    context.rtu_points[role] = (connection, tag)
    if role == "target":
        context.profile["rtu_tag"] = tag


def _create_iec104(context):
    context.profile["master_port"] = context.iec104.create()


def _import_iec104(context, role):
    connection, tag = context.rtu_points[role]
    context.profile[f"{role}_ioa"] = context.iec104.import_point(connection, tag, f"{context.name}-{role}-104")


def _verify_routes(context):
    assert context.profile["target_ioa"] != context.profile["reference_ioa"], "IEC104 目标与参照 IOA 必须不同"
    context.iec104.check_routes()


def _start_rtu(context):
    page = context.rtu
    page.open_current_links()
    reference, reference_tag = context.rtu_points["reference"]
    page.select(reference)
    page.start()
    page.wait_value(reference_tag, 100, 0, context.profile["phase_timeout_seconds"])
    page.select(context.rtu_points["target"][0])
    page.start()
    context.simulator_stage = context.simulator.wait_target_exchange(None, context.profile["phase_timeout_seconds"])


def _measurement(master, ca, ioa):
    rows = [row for row in master.read_measurements()["rows"]
            if int(row["cells"][0]) == ca and int(row["cells"][1]) == ioa]
    assert len(rows) <= 1, f"CA={ca} IOA={ioa} 的遥测行不唯一：{rows!r}"
    return rows[0] if rows else None


def _observation_step(index, label, quality):
    action = f"观察{label}的自发遥测"
    expected = f"值 100，品质 0x{quality:02X}"

    def observe(context):
        master, profile = context.master, context.profile
        last = None

        def match():
            nonlocal last
            last = _measurement(master, profile["common_address"], profile["target_ioa"])
            if last is None or int(last["frame_id"] or 0) <= context.frame_id:
                return False
            return last if int(last["cells"][4], 16) == quality else False

        with master.step(action, expected=expected) as step:
            try:
                row = master.wait_for(match, f"未看到 {label} 的新遥测", timeout=profile["phase_timeout_seconds"])
            except TimeoutException:
                evidence = {"阶段": label, "期望品质": f"0x{quality:02X}", "最后主站点表": last,
                            "上个已通过报文编号": context.frame_id, "应答源状态": context.simulator_stage}
                try:
                    upper = _target_row(context)
                    evidence["上位机目标行"] = upper.text if upper is not None else None
                except Exception as error:
                    evidence["上位机取证错误"] = str(error)
                step["实际"] = evidence
                master.metadata["RTU品质未观察到的阶段"] = evidence
                raise AssertionError(f"等待{label}阶段超时，104 未观察到品质 0x{quality:02X} 的新遥测；"
                                     f"最后点表行：{last!r}。结合本例 slaveSim 工况记录区分工况未到和上送异常") from None
            step["实际"] = row
            context.observations.append(row)
            assert int(row["cells"][2]) == 36, f"目标点须为带时标短浮点遥测：{row!r}"
            assert Decimal(row["cells"][3]) == 100, f"目标值应保持 100：{row!r}"
            context.frame_id = int(row["frame_id"])

    return Step(f"quality.phase.{index}", action, expected, observe)


def _save_frames(master):
    frames = capture_frames(master)
    (master.artifacts / "rtu-104-quality-frames.json").write_text(
        json.dumps(frames, ensure_ascii=False, indent=2), encoding="utf-8")
    return frames


def _assert_wire_sequence(frames, profile):
    decoded = [entry for frame in frames if (entry := decode_frame(frame)) is not None]
    ca, ioa, reference = (profile[key] for key in ("common_address", "target_ioa", "reference_ioa"))

    def gi(entry, direction, cot):
        return (entry["direction"] == direction and entry["type_id"] == 100 and entry["ca"] == ca
                and entry["cot"] == cot and not entry["negative"] and not entry["test"]
                and entry["objects"] == [{"ioa": 0, "qoi": 20}])

    requests = [i for i, entry in enumerate(decoded) if gi(entry, "发送", 6)]
    assert len(requests) == 1, "报文历史须保留本会话唯一的站总召请求"
    confirmations = [i for i in range(requests[0] + 1, len(decoded)) if gi(decoded[i], "接收", 7)]
    terminations = [i for i in range(requests[0] + 1, len(decoded)) if gi(decoded[i], "接收", 10)]
    assert len(confirmations) == len(terminations) == 1 and confirmations[0] < terminations[0], (
        "报文中须有本次总召的肯定确认和终止")

    def points(entries, cot):
        for entry in entries:
            if entry["direction"] == "接收" and entry["ca"] == ca and entry["cot"] == cot:
                assert entry["type_id"] in (13, 36), (
                    f"专用测试 CA 收到非短浮点遥测，无法核对本例点序列：{entry!r}")
                for item in entry["objects"]:
                    yield entry, item

    gi_values = list(points(decoded[confirmations[0] + 1:terminations[0]], 20))
    assert any(entry["type_id"] in (13, 36) and item["ioa"] == reference and item["quality"] == 0
               and isinstance(item["value"], (int, float)) and math.isfinite(item["value"])
               and not entry["negative"] and not entry["test"]
               for entry, item in gi_values), "总召报文中缺少正常参照遥测"
    assert all(item["ioa"] != ioa for _, item in gi_values), "无首值目标点出现在总召响应中"
    assert all(item["ioa"] != ioa for _, item in points(decoded[:terminations[0] + 1], 3)), (
        "初始无样本阶段收到了目标点自发上送")

    sequence = []
    for entry, item in points(decoded[terminations[0] + 1:], 3):
        if item["ioa"] != ioa:
            continue
        assert entry["type_id"] == 36 and not entry["negative"] and not entry["test"], (
            f"目标点不是正常传送的带时标短浮点遥测：{entry!r}")
        assert item["value"] == 100, f"目标遥测数值发生变化：{item!r}"
        assert item["timestamp"], f"目标遥测缺少 CP56Time2a 时标：{item!r}"
        if not sequence or item["quality"] != sequence[-1]["quality"]:
            sequence.append(item)
        elif item["quality"] in (0x40, 0xC0):
            assert item["timestamp"] == sequence[-1]["timestamp"], f"重复故障报文刷新了采样时标：{item!r}"
    assert [item["quality"] for item in sequence] == [flags for _, flags in PHASES], (
        f"自发遥测品质顺序不符：{sequence!r}")
    assert sequence[1]["timestamp"] == sequence[2]["timestamp"], "两次故障沿用的样本时标不同"
    assert sequence[3]["timestamp"] != sequence[2]["timestamp"], "恢复后未刷新样本时标"
    return sequence


def _prepare_master_target(context):
    profile = context.profile
    context.master.prepare_target(profile["master_host"], profile["master_port"], profile["common_address"])


def _check_initial_table(context):
    master, profile = context.master, context.profile
    with master.step("检查总召结束时的初始点表", expected="参照点正常，目标点缺席") as step:
        reference = _measurement(master, profile["common_address"], profile["reference_ioa"])
        target = _measurement(master, profile["common_address"], profile["target_ioa"])
        step["实际"] = {"参照点": reference, "目标点": target}
        assert reference and int(reference["cells"][4], 16) == 0, "缺少正常参照点，请检查专用线路"
        assert target is None, f"目标点已有样本或初始无应答窗口不足：{target!r}"
    context.frame_id = 0
    context.observations = []
    master.metadata["RTU品质阶段观察"] = context.observations


def _verify_frames(context):
    master, profile = context.master, context.profile
    with master.step("核对原始总召及自发遥测报文", expected="总召省略目标，随后品质依次为 00、40、C0、00") as step:
        frames = _save_frames(master)
        step["实际"] = f"已保留 {len(frames)} 条收发报文，待核对"
        sequence = _assert_wire_sequence(frames, profile)
        master.metadata["RTU品质报文序列"] = sequence
        step["实际"] = sequence


ENVIRONMENT = Step("quality.environment", "检查 slaveSim 和独立 104 主站入口", "模拟器入口、主站程序及驱动可用")
SIM_START = Step("quality.sim.start", "打开本例 slaveSim 会话", "显示模拟器运行页及配置入口")
SIM_FINISH = Step("quality.sim.finish", "关闭本例 slaveSim 浏览器", "独立浏览器会话已关闭")
UPPER_CLEANUP = Step("quality.upper.cleanup", "清理本例 IEC104 与 RTU 链路", "专用连接、点表及关联路由已删除")
SIM_CLEANUP = Step("quality.sim.cleanup", "清空 slaveSim 全部总线", "回读配置确认总线数为 0")
LOGIN = (
    Step("quality.login.account", "读取测试账号", "专用平台账号配置有效", _load_account),
    *login_steps("quality.login"),
)
OPEN_PROTOCOL = Step("quality.rtu.links", "打开已连接设备的 RTU 配置", "可读取 RTU 连接列表", _open_protocol)
PREPARE_SCENARIO = (
    Step("quality.sim.prepare", "读取配置、删除旧总线并新建参照与目标总线",
         "一次保存应用后仅有两路新总线，运行页实际设备各不相同且未被既有 RTU 连接使用，参照站应答 100，目标站初始无应答",
         _prepare_simulator),
    Step("quality.rtu.reference", "创建并核对本例参照 RTU 点",
         "参照 RTU 连接与点表使用参照总线的实际设备、站号 1、保持寄存器 0 和正死区",
         lambda context: _prepare_rtu(context, "reference")),
    Step("quality.rtu.target", "创建并核对本例目标 RTU 点",
         "目标 RTU 连接与点表使用目标总线的实际设备、站号 1、保持寄存器 0 和正死区",
         lambda context: _prepare_rtu(context, "target")),
    Step("quality.iec104.create", "建立本例带时标 IEC104 被控站", "页面显示本例独立链路及监听端口", _create_iec104),
    Step("quality.iec104.reference", "导入参照 RTU 点为 IEC104 短浮点遥测", "参照点的 IOA 在点表显示",
         lambda context: _import_iec104(context, "reference")),
    Step("quality.iec104.target", "导入目标 RTU 点为 IEC104 短浮点遥测", "目标点的 IOA 在点表显示",
         lambda context: _import_iec104(context, "target")),
    Step("quality.iec104.routes", "在数据总线核对本例遥测路由", "参照与目标均按 RTU 到 IEC104 的方向路由", _verify_routes),
    Step("quality.iec104.start", "启动本例 IEC104 被控站", "本例链路进入运行中",
         lambda context: context.iec104.start()),
    Step("quality.rtu.poll", "启动本例参照及目标 RTU 轮询",
         "参照点采集正常且为 100，目标仍处于静默阶段", _start_rtu),
)
INITIAL_SESSION = (
    Step("quality.master.target", "填写并核对本例 104 主站连接参数",
         "使用上位机当前主机及本例 IEC104 链路实际端口和公共地址", _prepare_master_target),
    Step("quality.master.connect", "连接本例 104 被控站", "收到 STARTDT 确认",
         lambda context: context.master.connect()),
    Step("quality.gi", "在当前会话发起总召", "总召确认终止，主站实时显示正常",
         lambda context: context.master.interrogate()),
    Step("quality.gi.table", "检查总召结束时的初始点表", "参照点正常，目标点缺席", _check_initial_table),
)


def _drive_target(index, label, address, value, expected):
    def drive(context):
        context.simulator.set_target(address, value, label)
        context.simulator_stage = context.simulator.wait_target_exchange(address, context.profile["phase_timeout_seconds"])
    return Step(f"quality.sim.phase.{index}", f"设置目标站工况：{label}", f"{expected}，两路实际设备路径保持不变", drive)


SIMULATOR_EXPECTED = ("目标站在地址 0 正常应答 100", "目标站不存在，读取无应答",
                      "目标站存在但未配置地址 0，读取应答异常码 0x02", "目标站恢复在地址 0 应答同值 100")
OBSERVATIONS = tuple(
    step
    for index, ((label, quality), (address, value), expected) in enumerate(
        zip(PHASES, TARGET_STAGES, SIMULATOR_EXPECTED, strict=True), start=1)
    for step in (_drive_target(index, label, address, value, expected), _observation_step(index, label, quality))
)
VERIFY_FRAMES = Step("quality.frames", "核对原始总召及自发遥测报文",
                     "总召省略目标，品质依次为 00、40、C0、00，故障沿用样本时标且恢复后刷新", _verify_frames)


def _cleanup_upper(context):
    page = context.rtu
    errors = []
    if context.iec104.port is not None:
        page._cleaning = True
        try:
            context.iec104.cleanup()
        except Exception as error:
            errors.append(f"IEC104 链路：{error}")
        finally:
            page._cleaning = False
    if page.creation_started:
        try:
            page.cleanup()
        except Exception as error:
            errors.append(f"RTU 连接：{error}")
    if errors:
        raise RuntimeError("；".join(errors))


def _capture_failure(context):
    for label, capture in (("上位机", context.desktop.capture), ("slaveSim", context.simulator.capture)):
        try:
            capture("rtu-104-quality-failure-before-cleanup", description=f"{label}清理前现场")
        except Exception as error:
            context.desktop.metadata[f"{label}品质验收取证错误"] = str(error)
    if hasattr(context, "master"):
        try:
            _save_frames(context.master)
        except Exception as error:
            context.master.metadata["RTU品质失败现场取证错误"] = str(error)


def _cleanup(context, request, failure):
    upper_failed = False
    metadata = context.desktop.metadata
    try:
        with track_fixture_step(request, UPPER_CLEANUP):
            _cleanup_upper(context)
    except BaseException as error:
        upper_failed = True
        metadata.setdefault("RTU品质清理错误", []).append(f"{UPPER_CLEANUP.action}：{error}")
        if failure is None:
            _capture_failure(context)
            raise
    finally:
        if context.simulator_preparation_started:
            try:
                with track_fixture_step(request, SIM_CLEANUP):
                    context.simulator.clear_all_buses()
            except BaseException as error:
                metadata.setdefault("RTU品质清理错误", []).append(f"{SIM_CLEANUP.action}：{error}")
                metadata["待核对的品质模拟资源"] = {
                    "固定配置路径": context.simulator.link_paths,
                    "实际设备": context.simulator.actual_devices,
                }
                if failure is None and not upper_failed:
                    raise


@case_plan(ENVIRONMENT, SLAVESIM_BROWSER_READY, DESKTOP_START, *LOGIN, SITE_CONNECT, OPEN_PROTOCOL, SIM_START, *PREPARE_SCENARIO,
           MASTER104_START, *INITIAL_SESSION, *OBSERVATIONS, VERIFY_FRAMES,
           UPPER_CLEANUP, SIM_CLEANUP, SIM_FINISH, MASTER104_FINISH, DESKTOP_FINISH)
@execution_logic("""
1. 登录上位机并连接现场下位机，清除 slaveSim 原有总线，重建参照和目标两路串口。参照站正常应答 100，目标站初始无应答。从运行页读取两路「实际设备」，确认未被既有 RTU 连接使用后创建两个 RTU 采集点，设置正死区，导入专用 IEC104 被控站为带时标短浮点遥测，核对两个点的 IOA 和数据总线路由，启动链路与轮询。
2. 用独立 IEC104 主站建立会话，收到 STARTDT 确认后发起总召。核对主站点表和原始报文中参照点有效，目标点在总召响应及总召结束前的自发遥测中均缺席。
3. 依次将目标站切为正常应答 100、无应答、异常应答 0x02、恢复应答 100。每次切换后确认两路实际设备路径保持不变，在 slaveSim 报文中确认工况实际生效，并在主站等待新的目标自发遥测。每轮值均为 100，品质依次为正常（0x00）、NT（0x40）、IV+NT（0xC0）、恢复正常（0x00）。
4. 保存并复核原始报文。核对总召肯定确认和终止、目标遥测的传送类型及 CP56Time2a 时标。两种故障沿用相同时标，重复故障不刷新，恢复后时标更新。失败时保留现场和报文，随后清理本例 IEC104、RTU 和路由配置，清空 slaveSim 总线并回读确认。
""")
def test_rtu_quality_reaches_iec104_with_unchanged_value(request):
    """RTU 值不变时 IEC104 品质上送"""
    with track_fixture_step(request, ENVIRONMENT):
        simulator_url, timeout = _check_environment()
    with track_fixture_step(request, SLAVESIM_BROWSER_READY):
        browser = request.getfixturevalue("slavesim_browser")
    desktop = request.getfixturevalue("fresh_desktop")
    name = f"e2e-quality-{uuid4().hex[:8]}"
    rtu = ModbusRtuPage(desktop)
    context = SimpleNamespace(name=name, profile={"common_address": 1, "phase_timeout_seconds": timeout},
                              desktop=desktop, page=PlatformLoginPage(desktop), rtu=rtu,
                              iec104=RtuQualityIec104(rtu, name), rtu_points={}, simulator_preparation_started=False)
    run_steps(request, LOGIN, context)
    with track_fixture_step(request, SITE_CONNECT):
        address = rtu.connect_site()
        context.profile["master_host"] = address.rsplit(":", 1)[0]
    run_steps(request, (OPEN_PROTOCOL,), context)
    with track_fixture_context(request, SIM_START, SIM_FINISH,
                               RtuQualitySimulator(desktop, simulator_url, name, browser=browser)) as simulator:
        context.simulator = simulator
        failure = None
        try:
            run_steps(request, PREPARE_SCENARIO, context)
            context.master = request.getfixturevalue("master104")
            context.master.metadata["RTU品质验收配置"] = context.profile
            run_steps(request, (*INITIAL_SESSION, *OBSERVATIONS, VERIFY_FRAMES), context)
        except BaseException as error:
            failure = error
            _capture_failure(context)
            raise
        finally:
            _cleanup(context, request, failure)
