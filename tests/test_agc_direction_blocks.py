"""通过专用 IEC104 业务链路验收 AGC 增减闭锁。"""
from decimal import Decimal
import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from case_support.agc_direction_iec104 import AgcDirectionIec104
from case_support.agc_direction_scenario import AgcDirectionProfile, AgcDirectionScenario
from case_support.agc_direction_setup import AgcDirectionSetup
from case_support.agc_direction_simulator import AgcDirectionSimulator
from case_support.login_plan import login_steps
from case_support.modbus_rtu_page import ModbusRtuPage
from testbench.case_plan import (
    DESKTOP_FINISH, DESKTOP_START, MASTER104_FINISH, MASTER104_START, SITE_CONNECT, SLAVESIM_BROWSER_READY,
    Step, case_plan, run_steps, track_fixture_context, track_fixture_step,
)
from testbench.login_page import PlatformLoginPage, load_login_account
from testbench.master104_commands import Master104CommandSender
from testbench.master104_signals import Master104PointObserver
from testbench.model import Environment, validate_slavesim_url
from testbench.workspace import local_config


pytestmark = [pytest.mark.case_group("AGC 控制"), pytest.mark.requires_modbus]

LOWER_KW = Decimal("10")
MIDDLE_KW = Decimal("50")
UPPER_KW = Decimal("100")


def _environment():
    path = os.environ.get("TESTBENCH_TASK")
    if not path:
        raise RuntimeError("前置条件未满足：请从测试上位机运行并配置 slaveSim 和模拟 104 主站")
    environment = Environment.from_dict(json.loads(Path(path).read_text(encoding="utf-8"))["environment"])
    environment.validate_master104(required=True)
    environment.validate_master104_driver()
    simulator_url = validate_slavesim_url(environment.slavesim_url, required=True)
    timeout = environment.modbus_acquisition_timeout
    if type(timeout) not in (int, float) or not 5 <= timeout <= 300:
        raise ValueError("采集阶段等待时限必须为 5 至 300 秒")
    return simulator_url, timeout


def _connect_site(context):
    address = context.rtu.connect_site()
    context.master_host = address.rsplit(":", 1)[0]
    context.rtu.open_current_links()


def _prepare_simulator(context):
    context.simulator_preparation_started = True
    context.measured, context.setpoint = context.simulator.prepare(
        context.name, context.rtu.assert_serial_paths_available)


def _configure_rtu(context):
    page = context.rtu
    measured, setpoint = context.measured, context.setpoint
    context.connection = page.create_connection(
        context.name, measured["actual_device"], 9600, 8, 1, "N", measured["station_id"], 1000)
    page.add_point(context.measured_tag, address=measured["address"])
    page.add_point(context.setpoint_tag, function=6, address=setpoint["address"])
    page.assert_saved_configuration()
    page.start()
    page.wait_value(context.measured_tag, measured["value"], 0, context.timeout)


def _configure_agc(context):
    context.group = context.agc.create_group(
        context.name, context.member_name, context.connection, context.measured_tag, context.setpoint_tag)


def _configure_104(context):
    context.iec104.create()
    mapping = context.iec104.configure(
        context.group["group_name"], command_tag=context.group["command_tag"],
        measured_tag=context.group["measured_tag"] if context.require_lag else None)
    context.profile = AgcDirectionProfile.from_dict({
        "master_host": context.master_host, "master_port": mapping["port"], "common_address": mapping["ca"],
        "command_ioa": mapping["target_ioa"], "increase_ioa": mapping["increase_ioa"],
        "decrease_ioa": mapping["decrease_ioa"], "measured_ioa": mapping["measured_ioa"],
        "lower_kw": LOWER_KW, "middle_kw": MIDDLE_KW, "upper_kw": UPPER_KW, "timeout_seconds": context.timeout,
    })
    context.desktop.metadata["AGC 用例实际配置"] = {
        "AGC": context.group, "RTU": context.connection, "IEC104": context.profile.to_dict(),
        "IEC104 路由": context.iec104.routes,
    }


def _connect_master(context):
    profile = context.profile
    master = context.master
    master.prepare_target(profile.master_host, profile.master_port, profile.common_address)
    master.connect()
    observer = Master104PointObserver(master, profile.ioas, profile.common_address, measured_ioa=profile.measured_ioa)
    sender = Master104CommandSender(master, profile.common_address, profile.command_ioa)
    context.scenario = AgcDirectionScenario(master, observer, sender, profile)


def _initialize(context, prepare_kw):
    context.scenario.initialize_target(prepare_kw)
    context.simulator.wait_setpoint(context.setpoint, int(prepare_kw), context.timeout)


def _interrogate(context, prepare_kw):
    context.master.interrogate()
    context.scenario.verify_initial_state(prepare_kw, require_lag=context.require_lag)


def _verify_transition(context, target_kw, changed_key):
    after_send = None
    if context.require_lag:
        def after_send():
            context.simulator.set_lagging_measurement(context.measured)
    context.scenario.verify_transition(target_kw, changed_key, require_lag=context.require_lag, after_send=after_send)


ENVIRONMENT = Step("agc.direction.environment", "检查 slaveSim 和独立 104 主站入口", "模拟器入口、主站程序及驱动可用")
SIM_START = Step("agc.direction.sim.start", "打开本例 slaveSim 会话", "显示模拟器运行页及配置入口")
SIM_FINISH = Step("agc.direction.sim.finish", "关闭本例 slaveSim 浏览器", "独立浏览器会话已关闭")
LOGIN = (
    Step("agc.direction.account", "读取测试账号", "专用平台账号配置有效",
         lambda c: setattr(c, "account", load_login_account(local_config("login-account.json")))),
    *login_steps("agc.direction.login"),
)
PREPARE = (
    Step("agc.direction.sim.prepare", "重建本例 slaveSim 总线和寄存器",
         "读回独立实测与设定寄存器，实际串口未被既有 RTU 连接使用", _prepare_simulator),
    Step("agc.direction.rtu", "建立本例 Modbus RTU 采集与写入点",
         "回读实际串口和站号，实测点采集正常，设定点使用写单寄存器", _configure_rtu),
    Step("agc.direction.group", "建立本例专用单成员 AGC 组",
         "采用绝对值有功控制，单位 kW，成员范围 10～100 kW，点位路由对应本例 RTU", _configure_agc),
    Step("agc.direction.iec104", "建立本例 104 从站及 AGC 路由",
         "回读实际端口、公共地址、总目标遥调和闭锁遥信点号，核对路由方向", _configure_104),
    Step("agc.direction.group.start", "启动本例 AGC 控制组",
         "本例组运行中、功能投入、允许远方", lambda c: c.agc.start()),
    Step("agc.direction.iec104.start", "启动本例 IEC104 从站",
         "本例链路进入运行中", lambda c: c.iec104.start()),
)
MASTER_CONNECT = Step("agc.direction.master.connect", "连接本例 104 从站",
                      "主站使用现场连接主机和本例点表参数，收到 STARTDT 确认", _connect_master)
CLEANUP = (
    Step("agc.direction.iec104.cleanup", "清理本例 IEC104 连接", "本例连接及关联点表路由已清理"),
    Step("agc.direction.group.cleanup", "清理本例 AGC 控制组", "本例控制组和成员路由已清理"),
    Step("agc.direction.rtu.cleanup", "清理本例 RTU 连接", "本例轮询和连接已清理"),
    Step("agc.direction.sim.cleanup", "清空 slaveSim 总线", "重新读取配置确认总线数为 0"),
)


def _scenario_steps(prepare_kw, initial, target_kw, changed_key, feedback, *, measured):
    transition = f"记录基线并遥调 {target_kw} kW 核对本轮反馈"
    if measured:
        transition += "，发出遥调后切换模拟实测工况"
    return (
        Step("agc.direction.initial_target", f"遥调 {prepare_kw} kW 建立本例初态",
             "收到肯定确认和终止，slaveSim 设定寄存器实际收到本例初始设定",
             lambda c: _initialize(c, prepare_kw)),
        Step("agc.direction.interrogation", "同一主站会话总召并核对初态", initial,
             lambda c: _interrogate(c, prepare_kw)),
        Step("agc.direction.transition", transition, f"本轮遥调收到肯定确认和终止，{feedback}",
             lambda c: _verify_transition(c, target_kw, changed_key)),
    )


def _agc_flow(prepare_kw, initial, target_kw, changed_key, feedback, *, measured=False):
    steps = _scenario_steps(prepare_kw, initial, target_kw, changed_key, feedback, measured=measured)

    def decorate(function):
        function.agc_scenario = (steps, measured)
        return case_plan(ENVIRONMENT, SLAVESIM_BROWSER_READY, DESKTOP_START, *LOGIN, SITE_CONNECT,
                         SIM_START, *PREPARE, MASTER104_START, MASTER_CONNECT, *steps,
                         *CLEANUP, SIM_FINISH, MASTER104_FINISH, DESKTOP_FINISH)(function)
    return decorate


def _capture_failure(context):
    sessions = (("DSP 上位机", context.desktop), ("slaveSim", context.simulator), ("模拟 104 主站", context.master))
    for label, session in sessions:
        if session is not None:
            try:
                session.capture("agc-direction-before-cleanup", description=f"{label}清理前现场")
            except Exception as error:
                context.desktop.metadata.setdefault("AGC 用例取证错误", []).append(f"{label}：{error}")


def _cleanup(context, request, failure):
    def cleanup_104():
        context.rtu._cleaning = True
        try:
            context.iec104.cleanup()
        finally:
            context.rtu._cleaning = False

    actions = (
        cleanup_104, context.agc.cleanup, context.rtu.cleanup, context.simulator.clear_all_buses,
    )
    needed = (context.iec104.port is not None, context.simulator_preparation_started,
              context.rtu.creation_started, context.simulator_preparation_started)
    errors = []
    for step, action, active in zip(CLEANUP, actions, needed, strict=True):
        if not active:
            continue
        try:
            with track_fixture_step(request, step):
                action()
        except Exception as error:
            errors.append(f"{step.action}：{error}")
    if errors:
        context.desktop.metadata["AGC 用例清理错误"] = errors
        if failure is None:
            _capture_failure(context)
            raise RuntimeError("；".join(errors))
        failure.add_note("AGC 用例清理失败：" + "；".join(errors))


def _run(request):
    steps, require_lag = request.node.obj.agc_scenario
    with track_fixture_step(request, ENVIRONMENT):
        simulator_url, timeout = _environment()
    with track_fixture_step(request, SLAVESIM_BROWSER_READY):
        browser = request.getfixturevalue("slavesim_browser")
    desktop = request.getfixturevalue("fresh_desktop")
    name = f"e2e-agc-{uuid4().hex[:8]}"
    rtu = ModbusRtuPage(desktop)
    context = SimpleNamespace(
        name=name, member_name=f"{name}-member", measured_tag=f"{name}-meas", setpoint_tag=f"{name}-set",
        desktop=desktop, page=PlatformLoginPage(desktop), rtu=rtu, timeout=timeout, require_lag=require_lag,
        agc=AgcDirectionSetup(desktop), iec104=AgcDirectionIec104(rtu, name), master=None,
        simulator_preparation_started=False,
    )
    run_steps(request, LOGIN, context)
    with track_fixture_step(request, SITE_CONNECT):
        _connect_site(context)
    with track_fixture_context(request, SIM_START, SIM_FINISH,
                               AgcDirectionSimulator(desktop, simulator_url, browser=browser)) as simulator:
        context.simulator = simulator
        failure = None
        try:
            run_steps(request, PREPARE, context)
            context.master = request.getfixturevalue("master104")
            run_steps(request, (MASTER_CONNECT, *steps), context)
        except BaseException as error:
            failure = error
            _capture_failure(context)
            raise
        finally:
            _cleanup(context, request, failure)


@_agc_flow(MIDDLE_KW, "增、减闭锁均为否且品质有效", UPPER_KW, "increase",
           "新收到有效增闭锁置位，减闭锁保持为否且品质有效")
def test_agc_upper_limit_sets_increase_block(request):
    """上限设定触发增闭锁"""
    _run(request)


@_agc_flow(UPPER_KW, "增闭锁已置位且品质有效", MIDDLE_KW, "increase",
           "新收到有效增闭锁解除，减闭锁保持为否且品质有效")
def test_agc_lower_target_clears_increase_block(request):
    """从上限下调解除增闭锁"""
    _run(request)


@_agc_flow(MIDDLE_KW, "增、减闭锁均为否且品质有效", LOWER_KW, "decrease",
           "新收到有效减闭锁置位，增闭锁保持为否且品质有效")
def test_agc_lower_limit_sets_decrease_block(request):
    """下限设定触发减闭锁"""
    _run(request)


@_agc_flow(LOWER_KW, "减闭锁已置位且品质有效", MIDDLE_KW, "decrease",
           "新收到有效减闭锁解除，增闭锁保持为否且品质有效")
def test_agc_higher_target_clears_decrease_block(request):
    """从下限上调解除减闭锁"""
    _run(request)


@_agc_flow(MIDDLE_KW, "增、减闭锁为否，实测严格位于上下限之间，品质均有效", UPPER_KW, "increase",
           "同一轮收到有效增闭锁置位和低于 100 kW 的新有效实测，减闭锁为否且品质有效", measured=True)
def test_agc_increase_block_does_not_wait_for_measured_limit(request):
    """设定达上限且实测未达时增闭锁"""
    _run(request)
