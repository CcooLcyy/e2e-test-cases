"""通过上位机反馈和模拟设备实际收到的控制值验收 AGC 通信重分配。"""

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
from case_support.case_metadata import execution_logic
from case_support.login_plan import login_steps
from case_support.measurement_expiry_agc import MeasurementExpiryAgc
from case_support.measurement_expiry_run import (
    SIM_CLEANUP, UPPER_CLEANUP, _cleanup, _expect_writes, _prepare_104, prepare_control_rtu,
)
from case_support.measurement_expiry_simulator import MeasurementExpirySimulator
from case_support.modbus_rtu_page import ModbusRtuPage
from case_support.power_range_run import _environment, _load_account


pytestmark = [pytest.mark.case_group("AGC 控制"), pytest.mark.requires_modbus]
MEMBERS = {name: (80, 0) for name in ("A", "B", "C")}


def _prepare_group(context):
    saved = context.group.create_communication_group(context.name, context.connections)
    context.target_tag = saved["command_tag"]


def _connect(context):
    context.master.prepare_target(context.host, context.iec104.port, 1)
    context.master.connect()
    context.group.start()


def _wait_online(context):
    status = {}
    with context.desktop.step("观察三个成员的通信参与条件", expected="A、B、C 均通信正常") as entry:
        try:
            for name in MEMBERS:
                def healthy(_, member=name):
                    status[member] = context.group.read_member_communication(member)
                    return status[member]["condition"] == "通信正常"

                context.group._until(healthy, f"成员 {name} 未进入通信正常", timeout=context.timeout)
        finally:
            entry["实际"] = dict(status)


def _baseline(context):
    sender = Master104CommandSender(context.master, 1, context.ioa)
    marker = len(context.simulator.events()) - 1
    context.desktop.metadata["重分配遥调"] = sender.send_target_kw(300)
    expected = dict.fromkeys(MEMBERS, 100)
    with context.desktop.step("核对三台 RTU 实收有功设定和应答", expected=expected) as entry:
        entry["实际"] = _expect_writes(context, expected, after=marker)
    for name, points in context.points.items():
        point = points[1]
        actual = context.simulator.observe(point["bus"], point["station_id"], point["function"],
                                           point["address"], verify_manual=False)
        context.desktop.check(f"核对 {name} 的实际设定寄存器", actual["value"], 100)


def _check_fault_control(context):
    """留存当前站点运行页证据，防止通过重建站点伪造设定保留。"""
    point = context.points["C"][1]
    simulator = context.simulator
    action = "检查第三台 RTU 的独立应答切换入口"
    with simulator._step(action, expected="可暂停及恢复该站应答，保留实际串口、站号和设定寄存器") as entry:
        _, device, _ = simulator._row((point["bus"], point["station_id"], point["function"], point["address"]))
        controls = sorted({button.text for button in device.find_elements(By.TAG_NAME, "button")
                           if button.is_displayed() and button.text})
        entry["实际"] = {"总线": point["bus"], "站号": point["station_id"], "运行页按钮": controls}
        raise RuntimeError("测试前置条件未满足：当前没有已确认的单站应答切换操作。"
                           "保存修改后的从站配置会重新初始化设定，无法验证第三台保留原设定。"
                           "失联分配和恢复分配未执行。")


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
    Step("rebalance.104", "建立本例专用 IEC104 总目标路由", "回读本例遥调点、路由和监听端口", _prepare_104),
)
VERIFY = (
    Step("rebalance.connect", "连接主站并启动本例 AGC 控制组", "主站连接就绪，控制组运行且允许远方", _connect),
    Step("rebalance.online", "观察三个 AGC 成员的通信参与条件", "三台均通信正常", _wait_online),
    Step("rebalance.baseline", "仅下发一次 300 kW", "三台各收到 100 kW 写入并应答", _baseline),
    Step("rebalance.fault_control", "检查第三台的独立应答切换入口", "可只切换第三台且保留实际串口、站号和原设定",
         _check_fault_control),
    Step("rebalance.fault", "保持总目标并观察第三台失联后的设定", "前两台各收到 150 kW，第三台保留 100 kW"),
    Step("rebalance.recovery", "恢复第三台应答并继续观察", "无需再次遥调，三台重新各收到 100 kW"),
)


@case_plan(ENVIRONMENT, SLAVESIM_BROWSER_READY, DESKTOP_START, *LOGIN, SITE_CONNECT, SIM_START, *PREPARE,
           MASTER104_START, *VERIFY, UPPER_CLEANUP, SIM_CLEANUP, SIM_FINISH, MASTER104_FINISH, DESKTOP_FINISH)
@execution_logic("""
1. 登录 DSP 上位机并连接现场，从 slaveSim 回读三台专用 RTU 的实际串口、站号和寄存器。
2. 创建并回读三台 200 kW 等权 AGC 成员的通信状态、量测和设定映射，建立专用 IEC104 总目标路由。
3. 确认三台通信正常后，遥调一次 300 kW，核对三台各收到 100 kW 写入、协议应答和寄存器反馈。
4. 检查第三台是否具备保持设定的独立应答切换入口。缺少该入口时保留当前证据，失联和恢复验收记为未执行。
5. 失联验收要求前两台各收到 150 kW，第三台保留 100 kW；恢复验收要求保持原总目标且三台重新各收到 100 kW。
""")
def test_agc_redistributes_when_rtu_member_loses_communication(request):
    """RTU 失联与恢复时重分配"""
    with track_fixture_step(request, ENVIRONMENT):
        url, timeout = _environment()
    with track_fixture_step(request, SLAVESIM_BROWSER_READY):
        browser = request.getfixturevalue("slavesim_browser")
    desktop = request.getfixturevalue("fresh_desktop")
    name = f"e2e-rebalance-{uuid4().hex[:8]}"
    rtu = ModbusRtuPage(desktop)
    context = SimpleNamespace(name=name, module="AGC", timeout=timeout, desktop=desktop,
                              page=PlatformLoginPage(desktop), rtu=rtu, sim_started=False,
                              group=MeasurementExpiryAgc(desktop, capacity_kw=200),
                              iec104=AgcDirectionIec104(rtu, name), master=None)
    run_steps(request, LOGIN, context)
    with track_fixture_step(request, SITE_CONNECT):
        context.host = rtu.connect_site().rsplit(":", 1)[0]
    with track_fixture_context(request, SIM_START, SIM_FINISH,
                               MeasurementExpirySimulator(desktop, url, browser=browser)) as simulator:
        context.simulator = simulator
        failure = None
        try:
            run_steps(request, PREPARE, context)
            context.master = request.getfixturevalue("master104")
            run_steps(request, VERIFY, context)
        except BaseException as error:
            failure = error
            for source in (desktop, simulator, context.master):
                if source is not None:
                    try:
                        source.capture("agc-rebalance-failure", description="AGC 通信重分配清理前现场")
                    except Exception as capture_error:
                        failure.add_note(f"截图失败：{capture_error}")
            raise
        finally:
            _cleanup(context, request, failure)
