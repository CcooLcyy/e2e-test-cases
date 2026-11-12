"""在慢 RTU 轮询间隔内验证量测有效期与实际控制写入。"""

from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from selenium.webdriver.common.by import By

from testbench.case_plan import (
    DESKTOP_FINISH, DESKTOP_START, MASTER104_FINISH, MASTER104_START, SITE_CONNECT,
    SLAVESIM_BROWSER_READY, Step, run_steps, track_fixture_context, track_fixture_step,
)
from testbench.login_page import PlatformLoginPage, load_login_account
from testbench.master104_commands import Master104CommandSender
from testbench.workspace import local_config

from .agc_direction_iec104 import AgcDirectionIec104
from .avc_period_page import AvcPeriodPage
from .login_plan import login_steps
from .measurement_expiry_agc import MeasurementExpiryAgc
from .measurement_expiry_simulator import MeasurementExpirySimulator
from .modbus_rtu_page import ModbusRtuPage
from .power_range_run import _environment


EXPIRY = 10
SLOW_POLL = 30
WRITE_WINDOW = 6
SCENARIOS = {
    "AGC": {"A": (40, 0), "B": (40, 0)},
    "AVC": {"voltage": (220,), "inverter": (0, 0)},
}


def _configure_expiry(page, dialog, *, verify=False):
    labels = page.driver.find_elements(By.XPATH, dialog + "//label[contains(normalize-space(.),'量测有效期')]")
    controls = [(label, page.driver.find_elements(By.ID, label.get_attribute("for") or ""))
                for label in labels if label.is_displayed() and label.get_attribute("for")]
    fields = [(label, field) for label, elements in controls for field in elements if field.is_displayed()]
    if len(fields) != 1:
        raise AssertionError(f"测试前置条件未满足：控制配置界面需要一个量测有效期输入框，实际找到 {len(fields)} 个")
    label, field = fields[0]
    description = label.find_element(By.XPATH, "ancestor::div[contains(@class,'ant-form-item')][1]").text
    if "毫秒" in description or "ms" in description.lower():
        expected = str(EXPIRY * 1000)
    elif "秒" in description:
        expected = str(EXPIRY)
    else:
        raise AssertionError(f"量测有效期字段未显示秒或毫秒单位：{description!r}")
    if not verify:
        page._replace(field, expected)
    assert field.get_property("value") == expected, f"量测有效期应为 {expected}，实际为 {field.get_property('value')}"


def prepare_control_rtu(context, specs, *, deferred=(), slow_intervals=None):
    """读回模拟器资源并建立对应 RTU 连接，可推迟指定连接启动轮询。"""
    context.sim_started = True
    context.rtu.open_current_links()
    context.points = context.simulator.prepare(context.name, specs, context.rtu.assert_serial_paths_available)
    context.connections = {}
    intervals = slow_intervals or {}
    for role, points in context.points.items():
        point = points[0]
        name = f"{context.name}-{role}"
        context.rtu.create_connection(name, point["actual_device"], 9600, 8, 1, "N", point["station_id"],
                                      intervals.get(role, 1000))
        context.rtu.add_point("measured", address=point["address"])
        if len(points) == 2:
            context.rtu.add_point("setpoint", function=6, address=points[1]["address"])
        context.rtu.assert_saved_configuration()
        context.connections[role] = name
        if role not in deferred:
            context.rtu.start()
            context.rtu.wait_value("measured", point["value"], 0, context.timeout)


def _prepare_rtu(context):
    prepare_control_rtu(context, SCENARIOS[context.module], deferred=(context.slow,),
                        slow_intervals={context.slow: SLOW_POLL * 1000})


def _prepare_group(context):
    if context.module == "AGC":
        result = context.group.create_communication_group(context.name, context.connections, configure_expiry=_configure_expiry)
        context.target_tag = result["command_tag"]
    else:
        context.target_tag = f"{context.name}-voltage-target"
        context.group.create_group(
            context.name, mode="voltage", calculation_period=1, command_period=4, voltage_tag="measured",
            target_tag=context.target_tag, voltage_connection=context.connections[context.slow],
            rtu_connection=context.connections[context.fast], q_measurement_tag="measured", q_setpoint_tag="setpoint",
            configure_expiry=_configure_expiry,
        )
    context.desktop.metadata["本例量测有效期"] = {"有效期秒": EXPIRY, "慢轮询秒": SLOW_POLL}


def _prepare_104(context):
    page = context.iec104
    page.create()
    tag = f"{context.name}-target"
    page._import_agc_point(f"{context.module}/{context.name}", context.target_tag, tag, page.FLOAT, "遥调")
    context.ioa = page._read_point(tag, page.FLOAT, "遥调")
    page.check_routes()
    page.start()


def _connect(context):
    context.master.prepare_target(context.host, context.iec104.port, 1)
    context.master.connect()
    context.sender = Master104CommandSender(context.master, 1, context.ioa)
    context.group.start()
    context.rtu.open_current_links()
    context.rtu.select(context.connections[context.slow])
    context.rtu.start()


def _protocol(context):
    """用同一 slaveSim 服务端时钟对齐量测、参考读取和实际写入。"""
    roles = {points[0]["bus"]: role for role, points in context.points.items()}
    reads = {role: [] for role in context.points}
    writes = {role: [] for role in context.points}
    for index, event in enumerate(context.simulator.events()):
        role = roles.get(event.get("bus"))
        if role is None or not event.get("frame"):
            continue
        frame = bytes.fromhex(event["frame"])
        points = context.points[role]
        if len(frame) < 2 or frame[0] != points[0]["station_id"]:
            continue
        if event["direction"] == "发送" and len(frame) == 7 and frame[1:3] == b"\x03\x02":
            reads[role].append((index, datetime.fromisoformat(event["time"]), int.from_bytes(frame[3:5], "big")))
        if len(points) != 2 or frame[1] not in (6, 16) or len(frame) < 6:
            continue
        if int.from_bytes(frame[2:4], "big") != points[1]["address"]:
            continue
        if event["direction"] == "接收":
            offset = 4 if frame[1] == 6 else 7
            assert (frame[1] == 6 and len(frame) == 8) or (len(frame) == 11 and frame[4:7] == b"\x00\x01\x02"), frame.hex()
            writes[role].append({"index": index, "time": datetime.fromisoformat(event["time"]),
                                 "value": int.from_bytes(frame[offset:offset + 2], "big"),
                                 "request": frame[:6], "ack": False})
        elif writes[role] and len(frame) == 8 and frame[:6] == writes[role][-1]["request"]:
            writes[role][-1]["ack"] = True
    return reads, writes


def _await(context, check, message, *, timeout=None):
    return context.simulator._wait(lambda _: check(*_protocol(context)), message,
                                   timeout=context.timeout if timeout is None else timeout)


def _expect_writes(context, expected, *, after, before=None):
    def match(_, writes):
        actual = {}
        for role, value in expected.items():
            commands = [entry for entry in writes[role] if entry["index"] > after]
            if not commands or not commands[0]["ack"]:
                return None
            command = commands[0]
            if before is not None:
                assert command["time"] < before, f"{role} 写入晚于有效观察窗口：{command}"
            assert command["value"] == value, f"{role} 应收到 {value}，实际 {command}"
            actual[role] = command["value"]
        return actual
    return _await(context, match, f"slaveSim 未收到预期写请求及应答：{expected}")


def _baseline(context):
    slow, fast = context.slow, context.fast
    context.sample = _await(context, lambda reads, _: reads[slow][-1] if reads[slow] else None,
                            "慢轮询链路尚无成功采样")
    assert context.sample[2] == context.points[slow][0]["value"], context.sample
    marker = len(context.simulator.events()) - 1
    context.sender.send_target_kw(80 if context.module == "AGC" else 230, timeout=EXPIRY)
    expires = context.sample[1] + context.expiry_delta
    expected = {slow: 40, fast: 40} if context.module == "AGC" else {fast: 20}
    actual = _expect_writes(context, expected, after=marker, before=expires)
    context.desktop.metadata["过期用例正常写入"] = actual


def _check_link_health(context):
    if context.module == "AGC":
        status = context.group.read_member_communication(context.slow)
        context.desktop.metadata["过期时成员通信状态"] = status
        assert status["condition"] == "通信正常", f"过期观察时成员通信异常：{status!r}"
    else:
        context.rtu.open_current_links()
        card = context.rtu._connection_locator(context.connections[context.slow])
        health = (By.XPATH, card[1] + "//*[@aria-label='通信正常']")
        context.rtu._until(lambda _: context.rtu._visible(health), "慢轮询链路未显示通信正常", timeout=WRITE_WINDOW)


def _expired(context):
    slow, fast = context.slow, context.fast
    sample = context.sample
    expires = sample[1] + context.expiry_delta

    def after_expiry(reads, _):
        assert reads[slow][-1][0] == sample[0], "慢轮询已产生新样本，未建立有效的过期窗口"
        return reads[fast][-1] if reads[fast] and reads[fast][-1][1] >= expires else None

    _await(context, after_expiry, "参考量测未跨过待测点的到期时刻")
    _check_link_health(context)
    _await(context, after_expiry, "核对链路状态后待测量测不再过期")
    marker = len(context.simulator.events()) - 1
    if context.module == "AGC":
        context.sender.send_target_kw(100, timeout=WRITE_WINDOW)
        _expect_writes(context, {fast: 100}, after=marker, before=sample[1] + context.poll_delta)
    else:
        # 保持 230 V 目标。无功实测由 0 改为 10 kvar，正常定压计算将从 20 改为 30 kvar。
        point = context.points[fast][0]
        context.simulator.set_value(point["bus"], point["station_id"], point["function"], point["address"], 10)

    def changed_reference(reads, writes):
        latest = after_expiry(reads, writes)
        expected = 40 if context.module == "AGC" else 10
        return latest if latest and latest[0] > marker and latest[2] == expected else None

    reference = _await(context, changed_reference, "改变控制输入后未采到有效参考量测")
    end = reference[1] + context.write_delta
    inhibited = (slow,) if context.module == "AGC" else (fast,)

    def stayed_quiet(reads, writes):
        latest = after_expiry(reads, writes)
        assert not any(entry["index"] > marker for role in inhibited for entry in writes[role]), (
            f"量测过期窗口内仍有成员写请求：{[writes[role] for role in inhibited]!r}")
        return latest if latest and latest[1] >= end else None

    observed_end = _await(context, stayed_quiet, "未完成覆盖控制周期和命令间隔的无写入观察窗口")
    slow_bus = context.points[slow][0]["bus"]
    for event in context.simulator.events()[sample[0] + 1:observed_end[0] + 1]:
        if event.get("bus") == slow_bus and event.get("direction") == "接收":
            assert bytes.fromhex(event["frame"])[1:2] != b"\x03", f"过期窗口内出现新的慢点读取：{event!r}"
    context.desktop.metadata["量测过期观察"] = {"末次采样": sample[1].isoformat(), "到期": expires.isoformat(),
                                         "参考点仍在更新": end.isoformat(), "无新写入": inhibited}


def _recovery(context):
    slow, fast = context.slow, context.fast
    sample = _await(context, lambda reads, _: reads[slow][-1] if reads[slow][-1][0] > context.sample[0] else None,
                    "慢轮询未收到下一次正常应答", timeout=max(context.timeout, SLOW_POLL))
    assert sample[2] == context.sample[2], f"恢复应收到同值新采样：{sample}"
    expected = {slow: 50, fast: 50} if context.module == "AGC" else {fast: 30}
    actual = _expect_writes(context, expected, after=sample[0], before=sample[1] + context.expiry_delta)
    context.desktop.metadata["量测恢复写入"] = actual


ENVIRONMENT = Step("expiry.environment", "检查 slaveSim 和独立 IEC104 主站入口", "模拟器、主站和驱动可用")
SIM_START = Step("expiry.sim.start", "打开本例 slaveSim", "模拟器配置和协议日志可用")
SIM_FINISH = Step("expiry.sim.finish", "关闭本例 slaveSim 会话", "独立浏览器会话关闭")
UPPER_CLEANUP = Step("expiry.upper.cleanup", "清理本例控制组与协议链路", "本例控制组、连接及路由已删除")
SIM_CLEANUP = Step("expiry.sim.cleanup", "保存报文并清空 slaveSim 总线", "回读确认总线数为 0")
LOGIN = (Step("expiry.account", "读取测试账号", "专用账号配置有效",
              lambda context: setattr(context, "account", load_login_account(local_config("login-account.json")))),
         *login_steps("expiry.login"))
PREPARE = (
    Step("expiry.rtu", "配置本例 RTU 快慢轮询", "慢链路 30 秒，参考链路 1 秒，实际串口互相独立", _prepare_rtu),
    Step("expiry.group", "配置控制组并回读 10 秒量测有效期", "必需量测和设定点路由有效，量测有效期已保存", _prepare_group),
    Step("expiry.iec104", "配置 IEC104 总目标", "回读本例遥调点、路由和实际端口", _prepare_104),
)
CONNECT = Step("expiry.connect", "连接主站并启动控制组和慢轮询", "收到 STARTDT 确认，控制组和慢链路运行", _connect)


def _checks(module):
    expired = "A 无写入，B 收到 100 kW" if module == "AGC" else "无功实测更新为 10 kvar，成员无写入"
    restored = "保持原目标，两台各收到 50 kW" if module == "AGC" else "保持 230 V 目标，成员自动收到 30 kvar"
    return (
        Step("expiry.baseline", "在量测有效期内验证正常下发", "成员收到基线设定并应答", _baseline),
        Step("expiry.expired", "在通信在线时观察量测过期后的下发", expired, _expired),
        Step("expiry.recovery", "等待下一笔有效量测并核对恢复下发", restored, _recovery),
    )


def expiry_plan(module):
    return (ENVIRONMENT, SLAVESIM_BROWSER_READY, DESKTOP_START, *LOGIN, SITE_CONNECT, SIM_START,
            *PREPARE, MASTER104_START, CONNECT, *_checks(module), UPPER_CLEANUP, SIM_CLEANUP,
            SIM_FINISH, MASTER104_FINISH, DESKTOP_FINISH)


def _cleanup(context, request, failure):
    metadata = context.desktop.metadata
    upper_failed = False
    try:
        with track_fixture_step(request, UPPER_CLEANUP):
            errors = []
            context.rtu._cleaning = True
            try:
                for label, cleanup in (("IEC104 连接", context.iec104.cleanup),
                                       (f"{context.module} 控制组", context.group.cleanup),
                                       ("RTU 连接", context.rtu.cleanup)):
                    try:
                        cleanup()
                    except Exception as error:
                        errors.append(f"{label}：{error}")
            finally:
                context.rtu._cleaning = False
            if errors:
                metadata.setdefault("测试资源清理错误", []).extend(errors)
                raise RuntimeError("；".join(errors))
    except Exception as error:
        upper_failed = True
        if failure is None:
            raise
        failure.add_note(f"控制组或链路清理失败：{error}")
    finally:
        if context.sim_started:
            try:
                with track_fixture_step(request, SIM_CLEANUP):
                    context.simulator.clear_all_buses()
            except Exception as error:
                metadata.setdefault("测试资源清理错误", []).append(f"{SIM_CLEANUP.action}：{error}")
                if failure is None and not upper_failed:
                    raise
                if failure is not None:
                    failure.add_note(f"模拟总线清理失败：{error}")


def run_expiry_case(request, module):
    with track_fixture_step(request, ENVIRONMENT):
        url, timeout = _environment()
    with track_fixture_step(request, SLAVESIM_BROWSER_READY):
        browser = request.getfixturevalue("slavesim_browser")
    desktop = request.getfixturevalue("fresh_desktop")
    name = f"e2e-age-{uuid4().hex[:8]}"
    rtu = ModbusRtuPage(desktop)
    slow, fast = SCENARIOS[module]
    context = SimpleNamespace(name=name, module=module, slow=slow, fast=fast, timeout=timeout, desktop=desktop,
                              page=PlatformLoginPage(desktop), rtu=rtu, sim_started=False,
                              group=MeasurementExpiryAgc(desktop) if module == "AGC" else AvcPeriodPage(desktop),
                              iec104=AgcDirectionIec104(rtu, name), master=None,
                              expiry_delta=timedelta(seconds=EXPIRY), poll_delta=timedelta(seconds=SLOW_POLL),
                              write_delta=timedelta(seconds=WRITE_WINDOW))
    run_steps(request, LOGIN, context)
    with track_fixture_step(request, SITE_CONNECT):
        context.host = rtu.connect_site().rsplit(":", 1)[0]
    with track_fixture_context(request, SIM_START, SIM_FINISH, MeasurementExpirySimulator(desktop, url, browser=browser)) as simulator:
        context.simulator = simulator
        failure = None
        try:
            run_steps(request, PREPARE, context)
            context.master = request.getfixturevalue("master104")
            run_steps(request, (CONNECT, *_checks(module)), context)
        except BaseException as error:
            failure = error
            for source in (desktop, simulator, context.master):
                if source is not None:
                    try:
                        source.capture("measurement-expiry-failure", description="量测过期用例清理前现场")
                    except Exception as capture_error:
                        failure.add_note(f"截图失败：{capture_error}")
            raise
        finally:
            _cleanup(context, request, failure)
