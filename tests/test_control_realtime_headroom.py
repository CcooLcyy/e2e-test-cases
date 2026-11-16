"""以实际 RTU 失联和不同实时裕量验收 AGC 与 AVC 分配。"""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from selenium.webdriver.common.by import By

from testbench.case_plan import (
    DESKTOP_FINISH, DESKTOP_START, MASTER104_FINISH, MASTER104_START, SITE_CONNECT,
    SLAVESIM_BROWSER_READY, Step, case_plan, run_steps, track_fixture_context, track_fixture_step,
)
from testbench.login_page import PlatformLoginPage
from testbench.master104_commands import Master104CommandSender

from case_support.agc_direction_iec104 import AgcDirectionIec104
from case_support.avc_period_page import AvcPeriodPage
from case_support.case_metadata import execution_logic
from case_support.login_plan import login_steps
from case_support.measurement_expiry_agc import MeasurementExpiryAgc
from case_support.measurement_expiry_run import (
    SIM_CLEANUP, UPPER_CLEANUP, _cleanup, _prepare_104, prepare_control_rtu,
)
from case_support.measurement_expiry_simulator import MeasurementExpirySimulator
from case_support.modbus_rtu_page import ModbusRtuPage, xpath_text
from case_support.power_range_run import _environment, _load_account, _save_frames
from case_support.realtime_headroom_evidence import (
    assert_a_offline_and_no_writes, assert_allocated, pause_member_a, wait_member_a_dropped_poll,
)


pytestmark = pytest.mark.requires_modbus

REALTIME_ALLOCATION = "按实时剩余裕量"
MEASURED = {"A": 0, "B": 90, "C": 60}
UPPER_LIMITS = {"A": 50, "B": 100, "C": 100}
ALL_ONLINE = {"A": 10, "B": 92, "C": 68}
OFFLINE_INCREASE = {"B": 94, "C": 76}
OFFLINE_DECREASE = {"B": 78, "C": 52}


def _prepare_rtu(context):
    points = {member: (value, 0) for member, value in MEASURED.items()}
    if context.module == "AVC":
        points["V"] = (220,)
    prepare_control_rtu(context, points)
    context.desktop.metadata["实时裕量场景"] = {
        "初始出力": MEASURED, "成员下限": 0, "成员上限": UPPER_LIMITS,
    }


def _prepare_group(context):
    if context.module == "AGC":
        saved = context.group.create_communication_group(
            context.name, context.connections, capacities=UPPER_LIMITS, allocation=REALTIME_ALLOCATION,
        )
        context.target_tag = saved["command_tag"]
        return
    context.target_tag = f"{context.name}-q-target"
    context.group.create_group(
        context.name, mode="q_total", calculation_period=1, command_period=4,
        voltage_tag="measured", voltage_connection=context.connections["V"], target_tag=context.target_tag,
        rtu_connection=context.connections["A"], q_measurement_tag="measured", q_setpoint_tag="setpoint",
        member_name="A", q_min=0, q_max=UPPER_LIMITS["A"],
        additional_members=tuple((member, context.connections[member], "measured", "setpoint", 0, UPPER_LIMITS[member])
                                 for member in ("B", "C")),
        allocation=REALTIME_ALLOCATION,
    )
    context.desktop.check("回读 AVC 分配方式", context.group._read_summary("分配方式"), REALTIME_ALLOCATION)


def _wait_communications(context, *, offline):
    expected = {name: "通信正常" for name in MEASURED}
    if context.module == "AGC":
        if offline:
            expected["A"] = "通信异常"
        readings = {}
        with context.desktop.step("核对 AGC 成员通信状态", expected=expected, actual=readings):
            def matches(_):
                readings.update({name: context.group.read_member_communication(name)["condition"] for name in expected})
                return readings == expected

            context.group._until(matches, f"AGC 未显示预期通信状态：{expected}", timeout=context.timeout)
        return

    if offline:
        expected["A"] = "本轮无有效数据"
    page = context.rtu
    page.open_current_links()
    for name, status in expected.items():
        connection = context.connections[name]
        page.select(connection)
        _, card = page._connection_locator(connection)
        locator = (By.XPATH, card + "//*[@aria-label=" + xpath_text(status) + "]")
        with context.desktop.step(f"核对 AVC 成员 {name} 的 RTU 链路状态", expected=status):
            page._until(lambda _: page._visible(locator), f"RTU 成员 {name} 未显示 {status}", timeout=context.timeout)


def _connect(context):
    context.master.prepare_target(context.host, context.iec104.port, 1)
    context.master.connect()
    context.sender = Master104CommandSender(context.master, 1, context.ioa)
    context.group.start()
    _wait_communications(context, offline=False)


def _wait_surviving_measurements(context):
    context.rtu.open_current_links()
    readings = {}
    for name in ("B", "C"):
        context.rtu.select(context.connections[name])
        readings[name] = context.rtu.wait_value("measured", MEASURED[name], 0, context.timeout)
    context.desktop.metadata.setdefault("剩余成员的有效实测", []).append(readings)


def _issue_target(context, target):
    marker = len(context.simulator.events()) - 1
    result = context.sender.send_target_kw(target, timeout=context.timeout)
    context.desktop.metadata.setdefault("实时裕量 IEC104 遥调", []).append(result)
    return marker


def _baseline(context):
    assert_allocated(context, _issue_target(context, 170), ALL_ONLINE)


def _lose_a(context):
    marker = pause_member_a(context)
    context.first_drop = wait_member_a_dropped_poll(context, marker)
    _wait_communications(context, offline=True)
    context.offline_marker = len(context.simulator.events()) - 1
    _wait_surviving_measurements(context)


def _redistribute(context):
    after = assert_allocated(context, context.first_drop, OFFLINE_INCREASE)
    assert_a_offline_and_no_writes(context, context.offline_marker, after=after)


def _check_decrease(context):
    _wait_surviving_measurements(context)
    after = assert_allocated(context, _issue_target(context, 130), OFFLINE_DECREASE)
    assert_a_offline_and_no_writes(context, context.offline_marker, after=after)


ENVIRONMENT = Step("headroom.environment", "检查 slaveSim 和模拟 IEC104 主站入口", "专用入口和驱动可用")
SIM_START = Step("headroom.sim.start", "打开本例 slaveSim", "可读实际配置和 RTU 报文")
SIM_FINISH = Step("headroom.sim.finish", "关闭本例 slaveSim", "独立浏览器会话已退出")
LOGIN = (Step("headroom.account", "读取测试账号", "专用账号配置有效", _load_account), *login_steps("headroom.login"))
PREPARE = (
    Step("headroom.rtu", "建立 A、B、C 独立 RTU 量测与设点", "初始实测为 0/90/60 且采集有效", _prepare_rtu),
    Step("headroom.group", "选择按实时剩余裕量分配并保存控制组",
         "回读分配方式，三台上限为 50/100/100，下限均为 0", _prepare_group),
    Step("headroom.104", "建立 IEC104 总目标遥调点", "读回遥调 IOA 和数据总线路由", _prepare_104),
)
CONNECT = Step("headroom.connect", "连接主站并启动控制组", "主站就绪且三台 RTU 成员通信正常", _connect)
CHECKS = (
    Step("headroom.baseline", "三台在线时发送总目标 170", "A/B/C 按上调裕量分得 10/2/8，实际设点 10/92/68", _baseline),
    Step("headroom.outage", "仅暂停 A 的 RTU 应答", "A 已失联且仍收到轮询，B/C 实测 90/60 有效", _lose_a),
    Step("headroom.redistribute", "保持总目标 170，观察失联后的重新分配",
         "B/C 新增 4/16，实际写入 94/76，A 无 RTU 写请求", _redistribute),
    Step("headroom.decrease", "下发总目标 130，观察反方向分配",
         "由实测计算 B/C 下调裕量 90/60，下调 12/8，实际写入 78/52，A 仍无写请求", _check_decrease),
)
CAPTURE = Step("headroom.capture", "保存 IEC104 主站报文", "本例遥调与协议确认已留存")


def _headroom_plan():
    return case_plan(
        ENVIRONMENT, SLAVESIM_BROWSER_READY, DESKTOP_START, *LOGIN, SITE_CONNECT, SIM_START, *PREPARE,
        MASTER104_START, CONNECT, *CHECKS, CAPTURE, UPPER_CLEANUP, SIM_CLEANUP, SIM_FINISH,
        MASTER104_FINISH, DESKTOP_FINISH,
    )


HEADROOM_LOGIC = """
1. 建立 A/B/C 三台可控成员，配置下限均为 0，上限为 50/100/100，实测固定为 0/90/60。通过界面保存并回读按实时剩余裕量分配。
2. 三台正常时通过 IEC104 遥调总目标 170。上调裕量为 50/10/40，20 的增量按 10/2/8 分配，RTU 实际收到 10/92/68。
3. 仅暂停 A 的 RTU 应答，确认 A 失联，B/C 仍在线且实测不变。保持总目标 170，B/C 应按 10:40 的上调裕量分得 4/16，实际收到 94/76。
4. 改发总目标 130。按 B/C 当前实测的下调裕量 90:60 分担 20 的减量，实际收到 78/52。两个方向均确认 A 无设点写请求。
5. 核对 RTU 写请求、应答、寄存器以及失联后持续丢弃的轮询。失败时先保存现场和报文，再清理专用资源。
"""


@pytest.mark.case_group("AGC 控制")
@_headroom_plan()
@execution_logic(HEADROOM_LOGIC)
def test_agc_distributes_by_realtime_headroom_after_member_loss(request):
    """AGC 成员失联后按在线成员实时有功裕量分配"""
    _run_headroom(request, "AGC")


@pytest.mark.case_group("AVC 控制")
@_headroom_plan()
@execution_logic(HEADROOM_LOGIC)
def test_avc_distributes_by_realtime_headroom_after_member_loss(request):
    """AVC 成员失联后按在线成员实时无功裕量分配"""
    _run_headroom(request, "AVC")


def _run_headroom(request, module):
    with track_fixture_step(request, ENVIRONMENT):
        url, timeout = _environment()
    with track_fixture_step(request, SLAVESIM_BROWSER_READY):
        browser = request.getfixturevalue("slavesim_browser")
    desktop = request.getfixturevalue("fresh_desktop")
    name = f"e2e-{module.lower()}-headroom-{uuid4().hex[:8]}"
    rtu = ModbusRtuPage(desktop)
    context = SimpleNamespace(
        name=name, module=module, timeout=timeout, desktop=desktop, page=PlatformLoginPage(desktop),
        rtu=rtu, group=MeasurementExpiryAgc(desktop, minimum_kw=0) if module == "AGC" else AvcPeriodPage(desktop),
        iec104=AgcDirectionIec104(rtu, name), master=None, sim_started=False, previous={},
    )
    run_steps(request, LOGIN, context)
    with track_fixture_step(request, SITE_CONNECT):
        context.host = rtu.connect_site().rsplit(":", 1)[0]
    with track_fixture_context(
        request, SIM_START, SIM_FINISH, MeasurementExpirySimulator(desktop, url, browser=browser),
    ) as simulator:
        context.simulator = simulator
        failure = None
        try:
            run_steps(request, PREPARE, context)
            context.master = request.getfixturevalue("master104")
            run_steps(request, (CONNECT, *CHECKS), context)
            with track_fixture_step(request, CAPTURE):
                _save_frames(context.master)
        except BaseException as error:
            failure = error
            for source in (desktop, simulator, context.master):
                if source is not None:
                    try:
                        source.capture("realtime-headroom-failure", description=f"{module} 实时裕量用例清理前现场")
                    except Exception as capture_error:
                        error.add_note(f"截图失败：{capture_error}")
            if context.master is not None:
                try:
                    _save_frames(context.master)
                except Exception as evidence_error:
                    error.add_note(f"主站报文取证失败：{evidence_error}")
            raise
        finally:
            _cleanup(context, request, failure)
