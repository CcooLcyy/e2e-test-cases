"""通过专用 AVC 控制组、模拟 104 主站与 slaveSim 验收周期和限频。"""

from datetime import datetime
from decimal import Decimal
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from case_support.avc_period_iec104 import AvcPeriodIec104, send_burst, snapshot, wait_calculations
from case_support.avc_period_page import AvcPeriodPage
from case_support.avc_period_simulator import AvcPeriodSimulator
from case_support.case_metadata import execution_logic
from case_support.login_plan import login_steps
from case_support.modbus_rtu_page import ModbusRtuPage
from testbench.case_plan import (
    DESKTOP_FINISH, DESKTOP_START, MASTER104_FINISH, MASTER104_START, SITE_CONNECT, SLAVESIM_BROWSER_READY,
    Step, case_plan, run_steps, track_fixture_context, track_fixture_step,
)
from testbench.login_page import PlatformLoginPage, load_login_account
from testbench.master104_commands import Master104CommandSender
from testbench.model import Environment, validate_slavesim_url
from testbench.workspace import local_config


pytestmark = [pytest.mark.case_group("AVC 控制"), pytest.mark.requires_modbus]


def _environment():
    task = os.environ.get("TESTBENCH_TASK")
    assert task, "请从测试上位机运行本用例并配置 slaveSim 和独立 104 主站"
    environment = Environment.from_dict(json.loads(Path(task).read_text(encoding="utf-8"))["environment"])
    simulator = validate_slavesim_url(environment.slavesim_url, required=True)
    timeout = environment.modbus_acquisition_timeout
    assert type(timeout) in (int, float) and 5 <= timeout <= 300, "RTU 采集等待时限必须为 5～300 秒"
    return simulator, timeout


def _prepare_simulator(context):
    context.rtu.open_current_links()
    context.simulator_preparation_started = True
    context.voltage, context.measured_q, context.command, *target = context.simulator.prepare(
        context.name, context.rtu.assert_serial_paths_available, include_target=context.rtu_target_tag is not None)
    context.rtu_target = target[0] if target else None
    context.desktop.metadata["AVC模拟点配置"] = {
        "电压": context.voltage, "无功": context.measured_q, "成员设定": context.command, "RTU目标": context.rtu_target,
    }


def _prepare_rtu(context):
    rtu = context.rtu
    rtu.open_current_links()
    rtu.create_connection(context.name, context.voltage["actual_device"], 9600, 8, 1, "N",
                          context.voltage["station_id"], 100 if context.rtu_target else 1000)
    rtu.add_point(context.voltage_tag, address=context.voltage["address"])
    rtu.add_point(context.measured_q_tag, address=context.measured_q["address"])
    rtu.add_point(context.member_tag, function=6, address=context.command["address"])
    if context.rtu_target:
        rtu.add_point(context.rtu_target_tag, address=context.rtu_target["address"])
    rtu.assert_saved_configuration()
    rtu.start()
    rtu.wait_value(context.voltage_tag, 220, 0, context.timeout)
    rtu.wait_value(context.measured_q_tag, 0, 0, context.timeout)
    if context.rtu_target:
        rtu.wait_value(context.rtu_target_tag, 10, 0, context.timeout)


def _prepare_avc(context):
    context.avc.create_group(
        context.name, mode=context.mode, calculation_period=1, command_period=context.command_period,
        voltage_tag=context.voltage_tag, target_tag=context.target_tag, rtu_connection=context.name,
        q_measurement_tag=context.measured_q_tag, q_setpoint_tag=context.member_tag, rtu_target_tag=context.rtu_target_tag,
    )


def _prepare_iec104(context):
    context.target_ioa, context.calculation_ioa = context.iec104.configure(
        context.name, context.target_tag if context.rtu_target is None else None, context.calculation_tag,
    )


def _connect_master(context):
    master = context.master
    master.prepare_target(context.master_host, context.iec104.port, 1)
    master.connect()
    if context.target_ioa is not None:
        context.sender = Master104CommandSender(master, 1, context.target_ioa)
    master.metadata["AVC测试路由"] = context.iec104.routes
    context.avc.start()


def _send(context, target):
    return context.sender.send_target_kw(target)


def _wait_member(context, target, after=0, *, minimum_wait=0):
    return context.simulator.wait_command(target, after=after, timeout=max(context.timeout, minimum_wait))


def _seconds_between(first, second):
    return (datetime.fromisoformat(second) - datetime.fromisoformat(first)).total_seconds()


def _assert_voltage_first(context):
    result = _send(context, 230)
    context.first_frame = result["sent_frame_id"]
    _wait_member(context, 20)


def _assert_voltage_calculation(context):
    samples = wait_calculations(context.master, 1, context.calculation_ioa, context.first_frame, 3, context.timeout)
    assert [Decimal(sample["value"]) for sample in samples] == [10, 10, 10], samples
    for first, second in zip(samples, samples[1:]):
        assert _seconds_between(first["at"], second["at"]) >= 1, f"相邻两轮计算间隔不足 1 秒：{samples!r}"


def _first_q_target(context):
    context.first_input = _send(context, 10)
    context.first_command = _wait_member(context, 10)


def _send_high_frequency_targets(context):
    # 首条成员命令在首次 104 发送和本机收到 slaveSim 写请求日志之间发生。
    # 用后者停止输入，再用前者证明输入满足窗口，避免混用两个服务端时钟。
    observed_ms = int(context.first_command["request"]["received_at"] * 1000)
    last_accepted = send_burst(context.master, 1, context.target_ioa, range(20, 50), observed_ms + 4000)
    earliest_first_ms = context.first_input["frames"]["sent"]["at_ms"]
    assert last_accepted < earliest_first_ms + 4000, (
        "高频输入时间证据不足，不能确认全部目标在首条成员命令后 4 秒内送达。"
        f"首个遥调发送={earliest_first_ms}，成员写请求日志收到={observed_ms}，最后目标终止={last_accepted}"
    )


def _assert_burst_result(context):
    first = context.first_command
    second = _wait_member(context, 49, first["sequence"], minimum_wait=10)
    interval = second["request_at"] - first["request_at"]
    assert interval >= 4, f"成员命令间隔 {interval} 秒小于 4 秒：{first!r}，{second!r}"


def _set_rtu_target(context, value):
    point = context.rtu_target
    context.simulator.set_value(point["bus"], point["station_id"], point["function"], point["address"], value)


def _first_rtu_target(context):
    context.first_command = _wait_member(context, 10)


def _stop_and_submit_target(context):
    context.avc.stop()
    _set_rtu_target(context, 20)
    context.rtu.open_current_links()
    context.rtu.select(context.name)
    context.rtu.wait_value(context.rtu_target_tag, 20, 0, context.timeout)
    history = context.simulator.command_history()
    assert len(history) == 1, f"停组后不应下发成员命令：{history!r}"


def _restart_with_new_periods(context):
    frames = snapshot(context.master)["frames"]
    context.restart_frame = max((frame["id"] for frame in frames), default=0)
    clock = context.simulator.observation_clock
    start_requested = context.avc.update_periods(3, 7, clock=clock)
    if start_requested is None:
        start_requested = context.avc.start(clock=clock)
    assert start_requested is not None, "未取得本例 AVC 控制组重新启动的请求时间"
    context.restarted_command = _wait_member(context, 20, context.first_command["sequence"])
    # 点击前和收到写日志时使用同一 slaveSim 浏览器的时钟，所得时差是响应上界。
    elapsed = context.restarted_command["request"]["received_at"] - start_requested
    context.desktop.metadata["AVC重启首发观察耗时秒"] = elapsed
    assert 0 <= elapsed <= 3, f"重启与观察到成员设定 20 kVar 的时间差为 {elapsed:.3f} 秒，未证实 3 秒内首发"


def _assert_new_calculation_period(context):
    before_input_ms = time.time_ns() // 1_000_000
    _set_rtu_target(context, 30)
    samples = wait_calculations(
        context.master, 1, context.calculation_ioa, context.restart_frame, 2, context.timeout, first_value=20,
    )
    assert before_input_ms <= samples[1]["received_ms"], (
        "本例在第二轮计算遥测到达后才开始提交 30 kVar，未建立参数切换的输入时序"
    )
    assert [Decimal(sample["value"]) for sample in samples] == [20, 30], samples
    interval = _seconds_between(samples[0]["at"], samples[1]["at"])
    assert 3 <= interval < 4.5, f"重新启动后相邻计算间隔 {interval} 秒：{samples!r}"


def _assert_new_command_period(context):
    first = context.restarted_command
    second = _wait_member(context, 30, first["sequence"], minimum_wait=15)
    interval = second["request_at"] - first["request_at"]
    assert 7 <= interval < 15, f"重新启动后相邻成员命令间隔 {interval} 秒：{first!r}，{second!r}"


def _load_account(context):
    context.account = load_login_account(local_config("login-account.json"))


ENVIRONMENT = Step("avc.environment", "检查本例测试入口", "测试上位机提供有效 slaveSim 入口和采集等待时间")
SIM_START = Step("avc.sim.start", "打开本例 slaveSim 会话", "可以读取模拟器实际配置和报文")
SIM_FINISH = Step("avc.sim.finish", "关闭本例 slaveSim 浏览器", "独立浏览器会话已关闭")
LOGIN = (Step("avc.login.account", "读取测试账号", "专用平台账号有效", _load_account), *login_steps("avc.login"))
PREPARE = (
    Step("avc.sim.prepare", "重建本例专用 RTU 模拟总线", "实际端口可用，初始电压 220 V、总无功 0 kVar", _prepare_simulator),
    Step("avc.rtu", "配置并核对本例 RTU 量测及写点", "上位机实际读到电压 220 V 和无功 0 kVar", _prepare_rtu),
    Step("avc.group", "配置本例 AVC 周期控制组", "配置本例命令模式、计算周期、命令最小间隔及成员映射", _prepare_avc),
    Step("avc.iec104", "建立专用 IEC104 链路及点位路由", "按场景配置目标遥调或带时标计算遥测", _prepare_iec104),
)
CONNECT = Step("avc.master.connect", "连接本例模拟 104 主站并启动控制组", "主站连接本例被控站，AVC 组运行", _connect_master)
CLEANUP = Step("avc.cleanup", "清理本例 AVC、IEC104、RTU 及 slaveSim 配置", "专用控制组和连接已删除，模拟总线已清空")

VOLTAGE = (
    Step("avc.voltage.first", "发送定压目标 230 V，观察首条成员命令", "首轮成员命令为 20 kVar", _assert_voltage_first),
    Step("avc.voltage.recalculate", "保持输入不变，观察三轮独立计算", "每轮偏差 10 V，相邻计算至少间隔 1 秒", _assert_voltage_calculation),
)
FIRST_Q = Step("avc.q.first", "提交首个总无功目标 10 kVar", "取得首条成员命令 10 kVar 及实际报文时间", _first_q_target)
BURST = (
    FIRST_Q,
    Step("avc.q.burst", "在首发后 4 秒窗口内连续发送 20～49 kVar", "全部目标得到协议确认和终止", _send_high_frequency_targets),
    Step("avc.q.latest", "观察第二条成员命令", "取最新目标 49 kVar，两条成员命令间隔至少 4 秒", _assert_burst_result),
)
RESTART = (
    Step("avc.rtu.first", "观察 RTU 初始目标 10 kVar 的首条成员命令", "成员命令为 10 kVar", _first_rtu_target),
    Step("avc.stop", "停组并经 RTU 输入 20 kVar", "组已停止，RTU 确认已采集到 20 kVar，成员未收到额外命令", _stop_and_submit_target),
    Step("avc.restart", "保存 3 秒计算和 7 秒命令间隔，重启控制组", "重启后 3 秒内首发 20 kVar", _restart_with_new_periods),
    Step("avc.new.calc", "提交 30 kVar 并读取两轮计算", "结果从 20 更新为 30 kVar，相邻计算间隔至少 3 秒且小于 4.5 秒", _assert_new_calculation_period),
    Step("avc.new.command", "观察重启后的下一条成员命令", "下发 30 kVar，间隔至少 7 秒且小于 15 秒", _assert_new_command_period),
)


def _cleanup(context, request, failure):
    errors = []
    with track_fixture_step(request, CLEANUP):
        context.rtu._cleaning = True
        actions = [("IEC104", context.iec104.cleanup), ("AVC", context.avc.cleanup)]
        if context.rtu.creation_started:
            actions.append(("RTU", context.rtu.cleanup))
        if context.simulator_preparation_started:
            actions.append(("slaveSim", context.simulator.clear_all_buses))
        for label, cleanup in actions:
            try:
                cleanup()
            except Exception as error:
                errors.append(f"{label} 清理失败：{error}")
        if errors:
            message = "；".join(errors)
            context.desktop.metadata["AVC清理错误"] = errors
            if failure is not None:
                failure.add_note(message)
            raise RuntimeError(message)


def _run(request, steps, *, mode, interval, calculation_tag, rtu_target=False):
    with track_fixture_step(request, ENVIRONMENT):
        simulator_url, timeout = _environment()
    with track_fixture_step(request, SLAVESIM_BROWSER_READY):
        browser = request.getfixturevalue("slavesim_browser")
    desktop = request.getfixturevalue("fresh_desktop")
    name = f"e2e-avc-{uuid4().hex[:8]}"
    rtu = ModbusRtuPage(desktop)
    context = SimpleNamespace(
        name=name, mode=mode, command_period=interval, calculation_tag=calculation_tag, timeout=timeout,
        desktop=desktop, page=PlatformLoginPage(desktop), rtu=rtu, avc=AvcPeriodPage(desktop),
        iec104=AvcPeriodIec104(rtu, name), simulator_preparation_started=False,
        voltage_tag=f"{name}-v", measured_q_tag=f"{name}-q", member_tag=f"{name}-set", target_tag=f"{name}-target",
        rtu_target_tag=f"{name}-rtu-target" if rtu_target else None,
    )
    run_steps(request, LOGIN, context)
    with track_fixture_step(request, SITE_CONNECT):
        context.master_host = rtu.connect_site().rsplit(":", 1)[0]
    simulator = AvcPeriodSimulator(desktop, simulator_url, browser=browser)
    with track_fixture_context(request, SIM_START, SIM_FINISH, simulator):
        context.simulator = simulator
        failure = None
        try:
            run_steps(request, PREPARE, context)
            context.master = request.getfixturevalue("master104")
            run_steps(request, (CONNECT, *steps), context)
        except BaseException as error:
            failure = error
            for source in (desktop, simulator, getattr(context, "master", None)):
                if source is not None:
                    try:
                        source.capture("avc-failure-before-cleanup", description="清理前的 AVC 验收现场")
                    except Exception as capture_error:
                        error.add_note(f"清理前截图失败：{capture_error}")
            raise
        finally:
            try:
                _cleanup(context, request, failure)
            except Exception:
                if failure is None:
                    raise


def _avc_plan(*checks):
    plan = case_plan(
        ENVIRONMENT, SLAVESIM_BROWSER_READY, DESKTOP_START, *LOGIN, SITE_CONNECT, SIM_START, *PREPARE,
        MASTER104_START, CONNECT, *checks, CLEANUP, SIM_FINISH, MASTER104_FINISH, DESKTOP_FINISH,
    )
    description = execution_logic("\n".join(f"{index}. {step.action}。预期 {step.expected}。"
                                             for index, step in enumerate((*PREPARE, CONNECT, *checks, CLEANUP), 1)))

    def decorate(function):
        return description(plan(function))

    return decorate


@_avc_plan(*VOLTAGE)
def test_avc_voltage_deviation_recalculates_without_new_input(request):
    """定压偏差持续时周期重算"""
    _run(request, VOLTAGE, mode="voltage", interval=4, calculation_tag="电压偏差")


@_avc_plan(*BURST)
def test_avc_burst_respects_command_interval_and_uses_latest_target(request):
    """总无功限频后下发最新目标"""
    _run(request, BURST, mode="q_total", interval=4, calculation_tag=None)


@_avc_plan(*RESTART)
def test_avc_stopped_group_applies_new_periods_after_restart(request):
    """控制组重启采用新计算与命令周期"""
    _run(request, RESTART, mode="q_total", interval=30, calculation_tag="总无功目标", rtu_target=True)
