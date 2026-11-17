"""从 IEC104 遥调到 RTU 写入，验证 AGC 控制组的有功分步限幅。"""

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from selenium.webdriver.common.by import By

from case_support.agc_direction_iec104 import AgcDirectionIec104
from case_support.agc_direction_setup import AgcDirectionSetup
from case_support.agc_step_limit_page import AgcStepLimit, configure_step_limit
from case_support.avc_period_iec104 import snapshot
from case_support.measurement_expiry_run import (
    ENVIRONMENT, LOGIN, SIM_CLEANUP, SIM_FINISH, SIM_START, UPPER_CLEANUP,
    _await, _cleanup, _prepare_104, prepare_control_rtu,
)
from case_support.measurement_expiry_simulator import MeasurementExpirySimulator
from case_support.modbus_rtu_page import ModbusRtuPage
from case_support.power_range_run import _environment
from testbench.case_plan import (
    DESKTOP_FINISH, DESKTOP_START, MASTER104_FINISH, MASTER104_START, SITE_CONNECT, SLAVESIM_BROWSER_READY,
    Step, case_plan, run_steps, track_fixture_context, track_fixture_step,
)
from testbench.login_page import PlatformLoginPage
from testbench.master104_commands import Master104CommandSender


pytestmark = [pytest.mark.requires_modbus, pytest.mark.case_group("AGC 控制")]
BASE_KW = 40
EVENT = AgcStepLimit(10, 7, up_seconds=2, down_seconds=6)
RETARGET = AgcStepLimit(10, 7, up_seconds=12, down_seconds=3)
CYCLIC = AgcStepLimit(10, None, sync_width=True)
COMMAND_PERIOD = 6


def _prepare_rtu(context):
    # 实测与设定是独立寄存器，接收设定不会改变 40 kW 量测。
    prepare_control_rtu(context, {"agc": (BASE_KW, BASE_KW)})


def _open_group_editor(page):
    page._select_owned_group()
    page._draft_opened = True
    page._click(page._button("编辑", page.OVERVIEW), "打开本例 AGC 控制组配置")


def _prepare_group(context):
    page = context.group
    settings = context.settings
    saved = page.create_group(context.name, "active-member", {"name": context.connections["agc"]}, "measured", "setpoint")
    context.target_tag = saved["command_tag"]
    _open_group_editor(page)
    if settings.up_seconds is not None:
        page._choose(page.GROUP_DIALOG, "control_mode", "事件触发", allow_label_prefix=True)
    else:
        # 计算仍是每 1 秒；预期下发取命令控制周期，不能按计算周期每轮都发。
        page._fill_group_field(page.GROUP_DIALOG, "command_control_period_seconds", COMMAND_PERIOD)
    configure_step_limit(page, page.GROUP_DIALOG, settings)
    page._submit_group("保存本例 AGC 分步限幅参数")
    overview = page._select_owned_group()
    mode = "周期分配" if settings.up_seconds is None else "事件触发"
    actual = overview.find_element(By.CSS_SELECTOR, ".agc-runtime-context").text.split("·", 1)[0].strip()
    with page.desktop.step("核对已保存的控制方式", expected=mode, actual=actual):
        legacy_label = mode == "事件触发" and actual.endswith(" " + mode)
        assert actual == mode or legacy_label, f"保存后控制方式应为 {mode}，实际为 {actual}"
    if settings.up_seconds is None:
        page.desktop.check("核对 AGC 周期配置", page._description(overview, "控制周期"),
                           f"计算 1 秒 / 命令 {COMMAND_PERIOD} 秒")
    _open_group_editor(page)
    configure_step_limit(page, page.GROUP_DIALOG, settings, verify=True)
    page._close_draft(page.GROUP_DIALOG, "取消")
    page._draft_opened = False


def _connect(context):
    context.master.prepare_target(context.host, context.iec104.port, 1)
    context.master.connect()
    context.sender = Master104CommandSender(context.master, 1, context.ioa)
    context.group.start()


def _start_target(context, target):
    context.after = len(context.simulator.events()) - 1
    return context.sender.send_target_kw(target, timeout=context.timeout)


def _next_write(context, value, minimum, *, before=None):
    actual = {}
    previous = context.previous

    def first(_, writes):
        write = next((entry for entry in writes["agc"] if entry["index"] > context.after), None)
        if write is None:
            return False
        actual.update({"写入 kW": write["value"], "时标": write["time"].isoformat(), "已应答": write["ack"]})
        assert write["value"] == value, f"下一条 RTU 写入应为 {value} kW，实际为 {actual!r}"
        if previous is not None:
            elapsed = (write["time"] - previous["time"]).total_seconds()
            actual["相邻写入间隔秒"] = elapsed
            assert elapsed >= minimum, f"相邻 RTU 写入应至少间隔 {minimum} 秒，实际为 {actual!r}"
            if before is not None:
                assert elapsed < before, f"升功率不应等到 {before} 秒的降功率间隔，实际为 {actual!r}"
        return write if write["ack"] else False

    expected = {"写入 kW": value, "已应答": True}
    if previous is not None:
        expected["相邻写入最小间隔秒"] = minimum
        if before is not None:
            expected["相邻写入应早于（秒）"] = before
    with context.desktop.step(f"核对下一条 RTU 设定 {value} kW", expected=expected, actual=actual):
        write = _await(context, first, f"未等到 {value} kW 的 RTU 写入及正常应答",
                       timeout=max(context.timeout, minimum + 8))
    context.previous = write
    context.after = write["index"]


def _follow_target(context, target, sequence, minimum, *, before=None):
    _start_target(context, target)
    for value in sequence:
        _next_write(context, value, minimum, before=before)


def _settled(context, seconds):
    last = context.previous
    after = last["index"]
    # 用持续 RTU 读应答跨过下一次可下发机会，同时证明模拟量测未变化。
    def hold(reads, writes):
        unexpected = [entry["value"] for entry in writes["agc"] if entry["index"] > after
                      and entry["value"] != last["value"]]
        assert not unexpected, f"达到 {last['value']} kW 后不应继续改变设定，实际还有 {unexpected!r}"
        measured = [(stamp, value) for index, stamp, value in reads["agc"] if index > context.observation_start]
        assert all(value == BASE_KW for _, value in measured), f"本例应保持 {BASE_KW} kW 量测：{measured!r}"
        return measured and (measured[-1][0] - last["time"]).total_seconds() >= seconds

    _await(context, hold, f"未观察到达到目标后至少 {seconds} 秒的固定量测",
           timeout=max(context.timeout, seconds + 8))


def _retarget_up(context):
    command = _start_target(context, 75)
    _next_write(context, 50, RETARGET.up_seconds)
    context.retarget_deadline = command["frames"]["sent"]["at_ms"] + RETARGET.up_seconds * 1000


def _retarget_down(context):
    command = context.sender.send_target_kw(35, timeout=context.timeout)
    sent = command["frames"]["sent"]["at_ms"]
    assert sent < context.retarget_deadline, (
        f"测试前置条件未满足：反向遥调未在首条遥调后的 {RETARGET.up_seconds} 秒内发出，"
        f"发送时标 {sent}，截止时标 {context.retarget_deadline}"
    )
    # 保留上一步 RTU 写入下标；若继续按旧目标升功率，第一条多余写入就会失败。
    for value in (43, 36, 35):
        _next_write(context, value, RETARGET.down_seconds)


PREPARE = (
    Step("agc-step.rtu", "重建本例模拟总线并配置 RTU 链路", "实测和设定寄存器独立，初值均为 40 kW", _prepare_rtu),
    Step("agc-step.group", "配置本例 AGC 分步限幅并重新打开回读", "模式、升降幅度和适用的间隔按场景保存", _prepare_group),
    Step("agc-step.iec104", "建立 IEC104 总有功遥调点和路由", "从上位机读回本例点号及实际端口", _prepare_104),
)
CONNECT = Step("agc-step.connect", "连接主站并启动本例控制组", "主站在线，AGC 功能投入且处于远方", _connect)
CAPTURE = Step("agc-step.capture", "保存本例 IEC104 协议记录", "保留遥调原始报文")
EVENT_STEPS = (
    Step("agc-step.event-up", "单次遥调 65 kW，量测保持 40 kW",
         "RTU 写入 50、60、65 kW，相邻写入至少间隔 2 秒，且早于 6 秒的降功率间隔",
         lambda c: _follow_target(c, 65, (50, 60, 65), EVENT.up_seconds, before=EVENT.down_seconds)),
    Step("agc-step.event-up-hold", "观察达到升功率目标后是否停止分步", "保持 65 kW，量测仍为 40 kW",
         lambda c: _settled(c, EVENT.up_seconds + 1)),
    Step("agc-step.event-down", "单次遥调 47 kW，量测仍保持 40 kW",
         "从已下发的 65 kW 降到 58、51、47 kW，相邻写入至少间隔 6 秒",
         lambda c: _follow_target(c, 47, (58, 51, 47), EVENT.down_seconds)),
    Step("agc-step.event-down-hold", "观察达到降功率目标后是否停止分步", "保持 47 kW，量测仍为 40 kW",
         lambda c: _settled(c, EVENT.down_seconds + 1)),
)
RETARGET_STEPS = (
    Step("agc-step.retarget-first", "遥调 75 kW 并等待首步", "RTU 先写入 50 kW 并应答", _retarget_up),
    Step("agc-step.retarget-reverse", "下一次升功率机会前，遥调改为 35 kW",
         "此后直接从 50 kW 改为 43、36、35 kW，相邻写入至少间隔 3 秒", _retarget_down),
    Step("agc-step.retarget-hold", "跨过旧目标的升功率间隔继续观察", "保持 35 kW，不再向 75 kW 推进",
         lambda c: _settled(c, RETARGET.up_seconds + 1)),
)
CYCLIC_STEPS = (
    Step("agc-step.cyclic-up", "周期分配下遥调 65 kW", "按共用幅度写入 50、60、65 kW，相邻写入至少间隔 6 秒",
         lambda c: _follow_target(c, 65, (50, 60, 65), COMMAND_PERIOD)),
    Step("agc-step.cyclic-down", "周期分配下遥调 40 kW", "按同一幅度写入 55、45、40 kW，相邻写入至少间隔 6 秒",
         lambda c: _follow_target(c, 40, (55, 45, 40), COMMAND_PERIOD)),
    Step("agc-step.cyclic-hold", "跨过一个命令控制周期观察设定", "保持 40 kW，未越过目标",
         lambda c: _settled(c, COMMAND_PERIOD + 1)),
)


def _plan(steps):
    return case_plan(ENVIRONMENT, SLAVESIM_BROWSER_READY, DESKTOP_START, *LOGIN, SITE_CONNECT, SIM_START, *PREPARE,
                     MASTER104_START, CONNECT, *steps, CAPTURE, UPPER_CLEANUP, SIM_CLEANUP, SIM_FINISH,
                     MASTER104_FINISH, DESKTOP_FINISH)


def _capture_frames(context):
    path = context.master.artifacts / "agc-step-limit-iec104-frames.json"
    path.write_text(json.dumps(snapshot(context.master)["frames"], ensure_ascii=False, indent=2), encoding="utf-8")


def _run(request, settings, checks):
    with track_fixture_step(request, ENVIRONMENT):
        url, timeout = _environment()
    with track_fixture_step(request, SLAVESIM_BROWSER_READY):
        browser = request.getfixturevalue("slavesim_browser")
    desktop = request.getfixturevalue("fresh_desktop")
    name = f"e2e-step-{uuid4().hex[:8]}"
    rtu = ModbusRtuPage(desktop)
    context = SimpleNamespace(name=name, module="AGC", timeout=timeout, desktop=desktop, page=PlatformLoginPage(desktop),
                              rtu=rtu, group=AgcDirectionSetup(desktop), iec104=AgcDirectionIec104(rtu, name),
                              settings=settings, master=None, sim_started=False, previous=None)
    run_steps(request, LOGIN, context)
    with track_fixture_step(request, SITE_CONNECT):
        context.host = rtu.connect_site().rsplit(":", 1)[0]
    with track_fixture_context(request, SIM_START, SIM_FINISH, MeasurementExpirySimulator(desktop, url, browser=browser)) as simulator:
        context.simulator = simulator
        failure = None
        try:
            run_steps(request, PREPARE, context)
            context.master = request.getfixturevalue("master104")
            run_steps(request, (CONNECT,), context)
            context.observation_start = len(simulator.events()) - 1
            run_steps(request, checks, context)
            with track_fixture_step(request, CAPTURE):
                _capture_frames(context)
        except BaseException as error:
            failure = error
            for source in (desktop, simulator, context.master):
                if source is not None:
                    try:
                        source.capture("agc-step-limit-failure", description="AGC 分步限幅用例清理前现场")
                    except Exception as capture_error:
                        error.add_note(f"截图失败：{capture_error}")
            if context.master is not None:
                try:
                    _capture_frames(context)
                except Exception as capture_error:
                    error.add_note(f"主站报文保存失败：{capture_error}")
            raise
        finally:
            _cleanup(context, request, failure)


@_plan(EVENT_STEPS)
def test_agc_event_steps_up_and_down_without_new_inputs(request):
    """AGC 事件触发按独立幅度和间隔逐步升降有功"""
    _run(request, EVENT, EVENT_STEPS)


@_plan(RETARGET_STEPS)
def test_agc_event_reverses_ramp_from_last_written_target(request):
    """AGC 事件触发分步期间从已下发值切换到反向新目标"""
    _run(request, RETARGET, RETARGET_STEPS)


@_plan(CYCLIC_STEPS)
def test_agc_cyclic_shares_step_width_and_uses_command_period(request):
    """AGC 周期分配同步升降幅度并沿用命令控制周期"""
    _run(request, CYCLIC, CYCLIC_STEPS)
