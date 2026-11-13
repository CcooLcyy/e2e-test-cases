"""通过上位机反馈和模拟设备实际收到的控制值验收 AGC 通信重分配。"""

from time import monotonic
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

from case_support.case_metadata import execution_logic
from case_support.login_plan import login_steps
from case_support.measurement_expiry_agc import MeasurementExpiryAgc
from case_support.measurement_expiry_run import (
    SIM_CLEANUP, UPPER_CLEANUP, _cleanup, _expect_writes, _protocol, prepare_control_rtu,
)
from case_support.measurement_expiry_simulator import MeasurementExpirySimulator
from case_support.modbus_rtu_page import ModbusRtuPage
from case_support.power_range_page import PowerRangePage
from case_support.power_range_run import (
    _environment, _load_account, _save_frames, master_measurement_baseline, verify_power_telemetry,
)
from case_support.rtu_quality_iec104 import RtuQualityIec104


pytestmark = [pytest.mark.case_group("AGC 控制"), pytest.mark.requires_modbus]
MEMBERS = {name: (80, 0) for name in ("A", "B", "C")}
PROPORTIONAL_CAPACITIES = {"A": 100, "B": 200}
RANGE_TAGS = ("当前可调有功下限", "当前可调有功上限")


def _prepare_group(context):
    saved = context.group.create_communication_group(context.name, context.connections, capacities=context.capacities)
    context.target_tag = saved["command_tag"]


def _prepare_104(context, telemetry_tags=()):
    page = context.iec104
    page.create()
    context.ioa = page.import_point(context.name, context.target_tag, f"{context.name}-104-target",
                                    module="AGC", business_type="遥调")
    for tag in telemetry_tags:
        context.ioas[tag] = page.import_point(context.name, tag, f"{context.name}-104-{tag}", module="AGC")
    assert len({context.ioa, *context.ioas.values()}) == 1 + len(telemetry_tags), (
        f"本例 IEC104 IOA 重复：遥调 {context.ioa}，遥测 {context.ioas}")
    page.check_routes()
    page.start()
    context.desktop.metadata["本例 IEC104 点表"] = {"遥调": context.ioa, "遥测": context.ioas}


def _connect(context):
    context.master.prepare_target(context.host, context.iec104.port, 1)
    context.master.connect()
    context.group.start()
    context.desktop.metadata["本例 IEC104 遥调点"] = {
        "主机": context.host, "端口": context.iec104.port, "公共地址": 1, "点号": context.ioa,
    }


def _wait_communication(context, expected):
    actual = {}
    with context.desktop.step("观察本例成员的通信参与条件", expected=expected, actual=actual):

        def matches(_):
            actual.update((name, context.group.read_member_communication(name)) for name in expected)
            return {name: details["condition"] for name, details in actual.items()} == expected

        context.group._until(matches, f"成员通信状态未达到预期：{expected}", timeout=context.timeout)


def _wait_online(context):
    _wait_communication(context, dict.fromkeys(context.connections, "通信正常"))


def _observe_setpoints(context, expected):
    for name, value in expected.items():
        point = context.points[name][1]
        actual = context.simulator.observe(point["bus"], point["station_id"], point["function"],
                                           point["address"], verify_manual=False)
        for field in ("actual_device", "link_path"):
            assert actual[field] == point[field], f"成员 {name} 的模拟串口身份已变化：{actual!r}"
        context.desktop.check(f"核对 {name} 的实际设定寄存器", actual["value"], value)


def _send_and_verify(context, target, expected):
    sender = Master104CommandSender(context.master, 1, context.ioa)
    marker = len(context.simulator.events()) - 1
    context.desktop.metadata["本例 AGC 遥调"] = sender.send_target_kw(target, timeout=context.timeout)
    actual = {}
    with context.desktop.step("核对 RTU 实收有功设定和应答", expected=expected, actual=actual):
        actual.update(_expect_writes(context, expected, after=marker))
    _observe_setpoints(context, expected)


def _verify_range(context, lower, upper):
    station = dict(zip(RANGE_TAGS, (lower, upper), strict=True))
    context.power_view.conn_id = context.group.conn_id
    context.power_view.wait_station_values(station, context.timeout)
    verify_power_telemetry(context, station, context.range_baseline)


def _baseline(context):
    context.range_baseline = master_measurement_baseline(context.master)
    _send_and_verify(context, 300, dict.fromkeys(MEMBERS, 100))
    _verify_range(context, 30, 600)


def _set_third_station_response(context, *, enabled):
    point = context.points["C"][1]
    simulator = context.simulator
    key = (point["bus"], point["station_id"], point["function"], point["address"])
    action, confirmed = ("恢复应答", "暂停应答") if enabled else ("暂停应答", "恢复应答")

    def control(label):
        _, device, _ = simulator._row(key)
        return [button for button in device.find_elements(By.TAG_NAME, "button")
                if button.is_displayed() and button.text == label]

    with simulator._step(f"第三台 RTU {action}", expected={"总线": key[0], "站号": key[1], "可用操作": confirmed}) as entry:
        buttons = control(action)
        if len(buttons) != 1 or not buttons[0].is_enabled():
            entry["实际"] = {"总线": key[0], "站号": key[1], "可见匹配按钮数": len(buttons)}
            raise RuntimeError(f"slaveSim 第三台 RTU 未提供可用的单站「{action}」操作")
        marker = len(simulator.events()) - 1
        tick = simulator._tick()
        buttons[0].click()
        simulator._wait(
            lambda _: simulator._tick() > max(tick, 0)
            and len(buttons := control(confirmed)) == 1 and buttons[0].is_enabled(),
            f"未确认第三台 RTU 已{action}",
            timeout=context.timeout,
        )
        entry["实际"] = {"总线": key[0], "站号": key[1], "可用操作": confirmed}
        return marker


def _third_station_frames(context, after):
    point = context.points["C"][0]
    return [(index, event) for index, event in enumerate(context.simulator.events()) if index > after
            and event["bus"] == point["bus"] and event.get("device_id") == point["station_id"]]


def _wait_third_station_poll(context, after, *, replying):
    def observed(_):
        if replying:
            reads, _ = _protocol(context)
            return next((index for index, _, value in reads["C"]
                         if index > after and value == MEMBERS["C"][0]), None)
        return next((index for index, event in _third_station_frames(context, after)
                     if event["direction"] == "接收" and bytes.fromhex(event["frame"])[1:2] == b"\x03"
                     and event["summary"] == "从站已暂停应答，已丢弃请求"), None)

    phase = "恢复后有效应答" if replying else "暂停后仍收到轮询请求"
    return context.simulator._wait(observed, f"未观察到第三台 RTU {phase}", timeout=context.timeout)


def _suspend(context):
    context.range_baseline = master_measurement_baseline(context.master)
    context.suspend_marker = _set_third_station_response(context, enabled=False)


def _verify_outage(context):
    first_drop = _wait_third_station_poll(context, context.suspend_marker, replying=False)
    expected = {"A": 150, "B": 150}
    actual = {}
    with context.desktop.step("核对失联后的 RTU 实收有功设定和应答", expected=expected, actual=actual):
        actual.update(_expect_writes(context, expected, after=first_drop, previous={"A": 100, "B": 100}))
    _wait_communication(context, {"A": "通信正常", "B": "通信正常", "C": "通信异常"})
    context.offline_marker = len(context.simulator.events()) - 1
    # 覆盖本例配置的 4 秒命令最小间隔。
    context.offline_until = monotonic() + 4
    _observe_setpoints(context, {**expected, "C": 100})
    _verify_range(context, 20, 400)
    _wait_third_station_poll(context, context.offline_marker, replying=False)
    transmissions = [event for _, event in _third_station_frames(context, first_drop)
                     if event["direction"] == "发送"]
    context.desktop.check("第三台暂停后收到轮询但未应答", transmissions, [])


def _resume(context):
    context.range_baseline = master_measurement_baseline(context.master)

    def no_offline_writes(_):
        _, writes = _protocol(context)
        requests = [entry["value"] for entry in writes["C"] if entry["index"] > context.offline_marker]
        assert not requests, f"第三台确认失联后仍收到有功设定写请求：{requests}"
        return monotonic() >= context.offline_until

    with context.desktop.step("核对第三台确认失联后的有功设定报文", expected="至少 4 秒无新写请求，含未应答请求"):
        context.simulator._wait(no_offline_writes, "未完成失联后的设定写入观察窗口", timeout=context.timeout)
    context.resume_marker = _set_third_station_response(context, enabled=True)


def _verify_recovery(context):
    first_reply = _wait_third_station_poll(context, context.resume_marker, replying=True)
    expected = dict.fromkeys(MEMBERS, 100)
    actual = {}
    with context.desktop.step("核对恢复后的 RTU 实收有功设定和应答", expected=expected, actual=actual):
        actual.update(_expect_writes(context, expected, after=first_reply, previous={"A": 150, "B": 150}))
    _wait_online(context)
    _observe_setpoints(context, expected)
    _verify_range(context, 30, 600)


ENVIRONMENT = Step("rebalance.environment", "检查 slaveSim 和独立 IEC104 主站入口", "模拟器、主站和驱动可用")
SIM_START = Step("rebalance.sim.start", "打开本例 slaveSim", "模拟器配置和运行页可访问")
SIM_FINISH = Step("rebalance.sim.finish", "关闭本例 slaveSim 会话", "独立浏览器会话关闭")
LOGIN = (Step("rebalance.account", "读取测试账号", "专用账号配置有效", _load_account),
         *login_steps("rebalance.login"))
PREPARE = (
    Step("rebalance.rtu", "创建并启动三条专用 RTU 连接", "回读实际串口、站号、寄存器和初始有功量测",
         lambda context: prepare_control_rtu(context, MEMBERS)),
    Step("rebalance.agc", "配置三台 200 kW 等权 AGC 成员", "三个成员绑定独立 RTU 通信状态、量测和设定点",
         _prepare_group),
    Step("rebalance.104", "建立本例专用 IEC104 遥调和范围遥测路由",
         "回读总目标、当前可调上下限、路由和监听端口", lambda context: _prepare_104(context, RANGE_TAGS)),
)
CONNECT = Step("allocation.connect", "连接主站并启动本例 AGC 控制组", "主站连接就绪，控制组运行且允许远方", _connect)
ONLINE = Step("allocation.online", "观察本例 AGC 成员的通信参与条件", "各成员均通信正常", _wait_online)
VERIFY = (
    Step("rebalance.baseline", "仅下发一次 300 kW",
         "三台各收到 100 kW 写入并应答；上位机和 IEC104 可调范围为 30～600 kW", _baseline),
    Step("rebalance.suspend", "只暂停第三台 RTU 应答", "第三台应答已暂停，仍可观察原寄存器", _suspend),
    Step("rebalance.fault", "保持总目标并观察第三台失联后的设定",
         "前两台各收到 150 kW；第三台通信异常、无应答且保留 100 kW；上位机和 IEC104 可调范围为 20～400 kW",
         _verify_outage),
    Step("rebalance.resume", "核对失联期间控制报文并恢复第三台 RTU 应答",
         "确认失联后无新设定写请求，第三台原串口与站号恢复应答", _resume),
    Step("rebalance.recovery", "观察第三台恢复后的分配",
         "无需再次遥调，三台重新各收到 100 kW；上位机和 IEC104 可调范围回到 30～600 kW", _verify_recovery),
)
PROPORTIONAL_PREPARE = (
    Step("proportional.rtu", "创建并启动两条专用 RTU 连接", "回读实际串口、站号、寄存器和初始有功量测",
         lambda context: prepare_control_rtu(context, {"A": (40, 0), "B": (40, 0)})),
    Step("proportional.agc", "配置 100 和 200 kW 两台 AGC 成员",
         "按额定容量比例分配，下限均为 0 kW，回读理论占比 33.3% 和 66.7%，成员绑定独立 RTU", _prepare_group),
    Step("proportional.104", "建立本例专用 IEC104 总目标路由", "回读本例遥调点、路由和监听端口", _prepare_104),
)
PROPORTIONAL_VERIFY = (
    Step("proportional.target", "通过 IEC104 下发 150 kW", "A 实收 50 kW，B 实收 100 kW，写入均应答",
         lambda context: _send_and_verify(context, 150, {"A": 50, "B": 100})),
)
CAPTURE = Step("rebalance.capture", "保存模拟主站原始报文", "本例 IEC104 报文已记录")


def _allocation_plan(prepare, verify):
    return case_plan(ENVIRONMENT, SLAVESIM_BROWSER_READY, DESKTOP_START, *LOGIN, SITE_CONNECT, SIM_START, *prepare,
                     MASTER104_START, CONNECT, ONLINE, *verify, CAPTURE, UPPER_CLEANUP, SIM_CLEANUP, SIM_FINISH,
                     MASTER104_FINISH, DESKTOP_FINISH)


@_allocation_plan(PREPARE, VERIFY)
@execution_logic("""
1. 登录 DSP 上位机并连接现场，从 slaveSim 回读三台专用 RTU 的实际串口、站号和寄存器。
2. 创建并回读三台 200 kW 等权 AGC 成员的通信状态、量测和设定映射，建立专用 IEC104 总目标路由。
3. 确认三台通信正常后，遥调一次 300 kW，核对三台各收到 100 kW 写入、协议应答和寄存器反馈。核对上位机和 IEC104 可调范围 30～600 kW。
4. 暂停第三台应答。前两台各收到 150 kW，第三台保留 100 kW，上位机和 IEC104 可调范围为 20～400 kW。确认失联后观察到持续轮询且无新控制写请求。
5. 恢复第三台应答，三台无需再次遥调就各收到 100 kW，上位机和 IEC104 可调范围回到 30～600 kW。失败时先取证，再清理本例资源。
""")
def test_agc_redistributes_when_rtu_member_loses_communication(request):
    """RTU 失联与恢复时重分配"""
    _run_allocation_case(request, PREPARE, VERIFY)


@_allocation_plan(PROPORTIONAL_PREPARE, PROPORTIONAL_VERIFY)
@execution_logic("""
1. 从 slaveSim 读回两台独立 RTU 的实际串口、站号和寄存器，通过 DSP 上位机完成采集配置。
2. 建立两台额定容量分别为 100 和 200 kW 的 AGC 成员，下限均设 0 kW。选择按额定容量比例，回读成员理论占比、量测及设定点，建立 IEC104 总目标路由。
3. 确认两台通信正常，通过 IEC104 下发 150 kW，从实际 RTU 写请求、应答和设定寄存器核对两台分别收到 50 和 100 kW。失败时先取证，再清理本例资源。
""")
def test_agc_distributes_by_rated_capacity(request):
    """按额定容量比例分配有功"""
    _run_allocation_case(request, PROPORTIONAL_PREPARE, PROPORTIONAL_VERIFY, capacities=PROPORTIONAL_CAPACITIES)


def _run_allocation_case(request, prepare, verify, *, capacities=None):
    with track_fixture_step(request, ENVIRONMENT):
        url, timeout = _environment()
    with track_fixture_step(request, SLAVESIM_BROWSER_READY):
        browser = request.getfixturevalue("slavesim_browser")
    desktop = request.getfixturevalue("fresh_desktop")
    name = f"e2e-agc-{uuid4().hex[:8]}"
    rtu = ModbusRtuPage(desktop)
    context = SimpleNamespace(name=name, module="AGC", timeout=timeout, desktop=desktop,
                              page=PlatformLoginPage(desktop), rtu=rtu, sim_started=False,
                              group=MeasurementExpiryAgc(desktop, capacity_kw=200, minimum_kw=10 if capacities is None else 0),
                              iec104=RtuQualityIec104(rtu, name), master=None, capacities=capacities,
                              power_view=PowerRangePage(desktop, "AGC", name), ioas={}, previous_remote=None)
    run_steps(request, LOGIN, context)
    with track_fixture_step(request, SITE_CONNECT):
        context.host = rtu.connect_site().rsplit(":", 1)[0]
    with track_fixture_context(request, SIM_START, SIM_FINISH,
                               MeasurementExpirySimulator(desktop, url, browser=browser)) as simulator:
        context.simulator = simulator
        failure = None
        try:
            run_steps(request, prepare, context)
            context.master = request.getfixturevalue("master104")
            run_steps(request, (CONNECT, ONLINE, *verify), context)
            with track_fixture_step(request, CAPTURE):
                _save_frames(context.master)
        except BaseException as error:
            failure = error
            for source in (desktop, simulator, context.master):
                if source is not None:
                    try:
                        source.capture("agc-allocation-failure", description="AGC 功率分配清理前现场")
                    except Exception as capture_error:
                        failure.add_note(f"截图失败：{capture_error}")
            if context.master is not None:
                try:
                    _save_frames(context.master)
                except Exception as evidence_error:
                    error.add_note(f"主站报文取证失败：{evidence_error}")
            raise
        finally:
            _cleanup(context, request, failure)
