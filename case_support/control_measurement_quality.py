"""在 RTU 链路仍正常时验证控制侧缺值、坏品质和恢复。"""

from datetime import datetime
from decimal import Decimal
import json
import struct
from time import monotonic
from types import SimpleNamespace
from uuid import uuid4

from selenium.webdriver.common.by import By

from testbench.case_plan import (
    DESKTOP_FINISH, DESKTOP_START, MASTER104_FINISH, MASTER104_START, SITE_CONNECT,
    SLAVESIM_BROWSER_READY, Step, run_steps, track_fixture_context, track_fixture_step,
)
from testbench.login_page import PlatformLoginPage
from testbench.master104_commands import Master104CommandSender

from .avc_period_iec104 import snapshot
from .avc_period_page import AvcPeriodPage
from .measurement_expiry_agc import MeasurementExpiryAgc
from .measurement_expiry_run import LOGIN, SCENARIOS, SIM_CLEANUP, UPPER_CLEANUP, _cleanup, _expect_writes, _protocol
from .measurement_expiry_simulator import MeasurementExpirySimulator
from .modbus_rtu_page import ModbusRtuPage
from .power_range_run import _environment
from .rtu_quality_iec104 import RtuQualityIec104


VALIDITY_SECONDS = 60
OBSERVATION_SECONDS = 6
KEY_ADDRESS = 0
WITNESS_ADDRESS = 2
UNMAPPED_ADDRESS = 3
SETPOINT_ADDRESS = 1


class QualitySimulator(MeasurementExpirySimulator):
    def move_measurement(self, points, old_address, new_address):
        """只修改本例量测地址，保留同站参考寄存器，验证两个串口身份。"""
        measurement, witness = points[0], points[2]
        name = measurement["bus"]
        before = self._read_configuration("变更本例量测地址前")
        station = self._station_editor(name)
        group = station.find_element(By.CSS_SELECTOR, ":scope > details.register-group")
        self._open(group)
        register = self._unique([
            item for item in group.find_elements(By.CSS_SELECTOR, ":scope > details.register-editor")
            if int(self._field(item, "协议地址").get_property("value")) == old_address
        ], "本例量测寄存器")
        self._open(register)
        self._replace(self._field(register, "协议地址"), new_address)
        label = f"将 {name} 量测地址从 {old_address} 切换到 {new_address}"
        actual = self._save_configuration(label, before=before, owned_names=(name,),
                                          legacy_owned_names=tuple(bus["name"] for bus in before))
        saved = self._unique([bus for bus in actual if bus["name"] == name], "本例量测总线")
        slave = self._unique(saved["slaves"], "本例量测从站")
        assert (saved["link"], slave["station_id"], slave["fields"]["未配置地址"]) == (
            measurement["link_path"], measurement["station_id"], "exception"), saved
        assert {reg["address"] for reg in slave["registers"]} == {new_address, SETPOINT_ADDRESS, WITNESS_ADDRESS}, slave
        self.driver.find_element(By.ID, "show-runtime").click()
        actual_point = self.observe(name, witness["station_id"], witness["function"], witness["address"], verify_manual=False)
        assert (actual_point["actual_device"], actual_point["link_path"]) == (witness["actual_device"], witness["link_path"]), (
            f"切换量测地址后，模拟器串口身份变化：{actual_point!r}")
        actual_measurement = self.observe(name, measurement["station_id"], measurement["function"], new_address,
                                          verify_manual=False)
        assert actual_measurement["value"] == measurement["value"], f"量测寄存器原值变化：{actual_measurement!r}"


def _setup_rtu(context):
    context.rtu.open_current_links()
    context.sim_started = True
    specs = dict(SCENARIOS[context.module])
    specs[context.slow] = (specs[context.slow][0], 0, 1)
    points = context.simulator.prepare(context.name, specs, context.rtu.assert_serial_paths_available)
    context.sim_points = points[context.slow]
    context.points = {**points, context.slow: points[context.slow][:2 if context.module == "AGC" else 1]}
    if context.missing:
        context.simulator.move_measurement(context.sim_points, KEY_ADDRESS, UNMAPPED_ADDRESS)
    context.connections = {}
    for role, observed in context.points.items():
        name = f"{context.name}-{role}"
        point = observed[0]
        page = context.rtu
        page.create_connection(name, point["actual_device"], 9600, 8, 1, "N", point["station_id"], 1000)
        page.add_point("measured", address=point["address"])
        if len(observed) == 2:
            page.add_point("setpoint", function=6, address=observed[1]["address"])
        if role == context.slow:
            page.add_point("witness", address=WITNESS_ADDRESS)
        page.assert_saved_configuration()
        context.connections[role] = name
        page.start()
        if role == context.slow:
            page.wait_value("witness", 1, 0, context.timeout)
        if not context.missing or role != context.slow:
            page.wait_value("measured", point["value"], 0, context.timeout)


def _configure_validity(page, dialog, *, verify=False):
    field = "measurement_validity_ms"
    if not verify:
        page._fill_group_field(dialog, field, VALIDITY_SECONDS * 1000)
    locator = (By.XPATH, dialog + "//input[@id='measurement_validity_ms']")
    element = page._until(lambda _: page._visible(locator), "未显示 AGC 量测有效期")
    page.desktop.check("核对 AGC 成员量测有效期毫秒", element.get_property("value"), str(VALIDITY_SECONDS * 1000))


def _setup_group(context):
    if context.module == "AGC":
        config = context.group.create_communication_group(
            context.name, context.connections, configure_expiry=_configure_validity,
        )
        context.target_tag = config["command_tag"]
    else:
        context.target_tag = f"{context.name}-voltage-target"
        context.group.create_group(
            context.name, mode="voltage", calculation_period=1, command_period=4, voltage_tag="measured",
            target_tag=context.target_tag, voltage_connection=context.connections[context.slow],
            rtu_connection=context.connections[context.fast], q_measurement_tag="measured", q_setpoint_tag="setpoint",
        )


def _setup_104(context):
    page = context.iec104
    page.create()
    context.ioa = page.import_point(context.name, context.target_tag, f"{context.name}-104-target",
                                    module=context.module, business_type="遥调")
    context.measured_ioa = page.import_point(context.connections[context.slow], "measured", f"{context.name}-104-meas")
    context.witness_ioa = page.import_point(context.connections[context.slow], "witness", f"{context.name}-104-witness")
    assert len({context.ioa, context.measured_ioa, context.witness_ioa}) == 3, "本例 IEC104 IOA 重复"
    page.check_routes()
    page.start()
    context.desktop.metadata["本例 IEC104 点位"] = {
        "端口": page.port, "CA": 1, "总目标": context.ioa,
        "待测量测": context.measured_ioa, "同站参考量测": context.witness_ioa,
    }


def _connect(context):
    context.master.prepare_target(context.host, context.iec104.port, 1)
    context.master.connect()
    context.sender = Master104CommandSender(context.master, 1, context.ioa)
    context.group.start()
    context.master.interrogate()


def _measurement_rows(context):
    rows = context.master.read_measurements()["rows"]
    matching = [row for row in rows if row["cells"][0] == "1"]
    indexed = {int(row["cells"][1]): row for row in matching}
    assert len(indexed) == len(matching), "本例 IEC104 主站出现重复 IOA 遥测"
    return indexed


def _wait_telemetry(context, ioa, value, quality, *, after=0):
    def match():
        row = _measurement_rows(context).get(ioa)
        if row and int(row["frame_id"] or 0) > after and int(row["cells"][4], 16) == quality:
            assert Decimal(row["cells"][3]) == value, f"IOA={ioa} 的实收值应为 {value}：{row!r}"
            return row
    return context.master.wait_for(match, f"IOA={ioa} 未收到品质 0x{quality:02X} 的 {value}", context.timeout)


def _reads(context, address, response_prefix, *, after):
    """从同一 RTU 总线按请求地址对应应答，避免用同站参考读误判关键量测。"""
    point = context.sim_points[0]
    pending = None
    for index, event in enumerate(context.simulator.events()):
        if event["bus"] != point["bus"] or event.get("device_id") != point["station_id"]:
            continue
        frame = bytes.fromhex(event["frame"])
        if event["direction"] == "接收":
            pending = (index, int.from_bytes(frame[2:4], "big")) if frame[1:2] == b"\x03" else None
        elif pending is not None:
            request, requested_address = pending
            pending = None
            if request > after and requested_address == address and frame[1:].startswith(response_prefix):
                yield index, datetime.fromisoformat(event["time"])


def _assert_online(context):
    if context.module == "AGC":
        last = {}
        def match(_):
            last.update(context.group.read_member_communication(context.slow))
            return last["condition"] == "通信正常"
        with context.desktop.step("核对同站参考读使 AGC 成员通信状态保持正常", expected="通信正常", actual=last):
            context.group._until(match, "成员通信条件未恢复正常", context.timeout)
    else:
        context.rtu.open_current_links()
        card = context.rtu._connection_locator(context.connections[context.slow])
        health = (By.XPATH, card[1] + "//*[@aria-label='通信正常']")
        context.rtu._until(lambda _: context.rtu._visible(health), "电压 RTU 连接未显示通信正常", context.timeout)


def _wait_measurement_fault(context, after):
    evidence = {}
    with context.desktop.step("确认关键量测异常而同站参考寄存器仍在更新", expected="FC03 异常码 0x02，参考点应答 1",
                              actual=evidence):
        def match(_):
            exception = next(_reads(context, KEY_ADDRESS, b"\x83\x02", after=after), None)
            witness = next(_reads(context, WITNESS_ADDRESS, b"\x03\x02\x00\x01", after=after), None)
            if exception and witness:
                evidence.update({"异常应答时间": exception[1].isoformat(), "参考正常应答时间": witness[1].isoformat()})
                return True
        context.simulator._wait(match, "未同时观察到关键量测异常应答与同站参考正常应答", timeout=context.timeout)
        _assert_online(context)


def _send_missing_voltage_target(context):
    master = context.master
    last_id = max((frame["id"] for frame in snapshot(master)["frames"]), default=0)
    driver = master.driver
    payload = b"\x01\x00" + context.ioa.to_bytes(3, "little") + struct.pack("<fB", 230, 0)
    evidence = {}
    with master.step("无电压首值时提交 230 V", expected="本次 IEC104 遥调收到负确认", actual=evidence):
        master._assert_live_display()
        driver.find_element(By.ID, "tab-setpoints").click()
        button = driver.find_element(By.ID, "send-setpoint")
        master.wait_for(button.is_enabled, "主站遥调操作未就绪", context.timeout)
        master.fill(driver.find_element(By.ID, "setpoint-ioa"), str(context.ioa))
        master.fill(driver.find_element(By.ID, "setpoint-value"), "230")
        button.click()

        def observe():
            submitted = None
            for frame in snapshot(master)["frames"]:
                if frame["id"] <= last_id:
                    continue
                raw = bytes.fromhex(frame["raw"])
                if len(raw) != 20 or raw[6:8] != b"\x32\x01" or raw[10:] != payload:
                    continue
                if frame["direction"] == "send" and raw[8] == 6:
                    submitted = frame
                if submitted and frame["direction"] == "receive":
                    assert raw[8] == 0x47, f"缺失电压时本次遥调应收到负确认：{frame!r}"
                    evidence.update({"发送": submitted, "负确认": frame})
                    return True
        master.wait_for(observe, "未收到缺失电压时本次遥调的负确认", context.timeout)


def _no_write_window(context, after):
    excluded = context.slow if context.module == "AGC" else context.fast
    end = monotonic() + OBSERVATION_SECONDS
    actual = {}
    with context.desktop.step("观察无控制写入窗口", expected=f"{excluded} 在 6 秒内无新 RTU 写请求", actual=actual):
        def quiet(_):
            _, writes = _protocol(context)
            commands = [entry["value"] for entry in writes[excluded] if entry["index"] > after]
            actual["写请求"] = commands
            assert not commands, f"{excluded} 仍有实际 RTU 写请求：{commands!r}"
            return monotonic() >= end
        context.simulator._wait(quiet, "未完成覆盖计算和命令间隔的无写请求窗口",
                                timeout=max(context.timeout, OBSERVATION_SECONDS + 2))


def _check_initial_missing(context):
    _wait_measurement_fault(context, -1)
    _wait_telemetry(context, context.witness_ioa, 1, 0)
    assert context.measured_ioa not in _measurement_rows(context), "无首值时 IEC104 已出现待测量测"
    marker = len(context.simulator.events()) - 1
    if context.module == "AGC":
        context.sender.send_target_kw(80, timeout=context.timeout)
        _expect_writes(context, {"B": 80}, after=marker)
    else:
        _send_missing_voltage_target(context)
    _no_write_window(context, marker)
    assert not list(_reads(context, KEY_ADDRESS, b"\x03\x02", after=-1)), "观察期间已出现待测量测首值"


def _baseline(context):
    value = context.sim_points[0]["value"]
    context.good_telemetry = _wait_telemetry(context, context.measured_ioa, value, 0)
    marker = len(context.simulator.events()) - 1
    context.sender.send_target_kw(80 if context.module == "AGC" else 230, timeout=context.timeout)
    expected = {"A": 40, "B": 40} if context.module == "AGC" else {"inverter": 20}
    _expect_writes(context, expected, after=marker)


def _check_bad_quality(context):
    marker = len(context.simulator.events()) - 1
    context.simulator.move_measurement(context.sim_points, KEY_ADDRESS, UNMAPPED_ADDRESS)
    _wait_measurement_fault(context, marker)
    value = context.sim_points[0]["value"]
    bad = _wait_telemetry(context, context.measured_ioa, value, 0xC0, after=int(context.good_telemetry["frame_id"]))
    context.bad_frame = int(bad["frame_id"])
    key_reads = list(_reads(context, KEY_ADDRESS, b"\x03\x02", after=-1))
    assert key_reads, "缺少最近一次正常量测报文"
    sampled = key_reads[-1][1]
    marker = len(context.simulator.events()) - 1
    if context.module == "AGC":
        context.sender.send_target_kw(100, timeout=context.timeout)
        _expect_writes(context, {"B": 100}, after=marker)
    else:
        point = context.points[context.fast][0]
        context.simulator.set_value(point["bus"], point["station_id"], point["function"], point["address"], 10)
        context.simulator._wait(
            lambda _: any(index > marker and value == 10 for index, _, value in _protocol(context)[0][context.fast]),
            "无功实测尚未更新到 10 kvar", timeout=context.timeout,
        )
    _no_write_window(context, marker)
    recent = list(_reads(context, WITNESS_ADDRESS, b"\x03\x02\x00\x01", after=marker))
    assert recent, "无写请求观察窗口内同站参考点也没有正常应答"
    if context.module == "AGC":
        assert 0 <= (recent[-1][1] - sampled).total_seconds() < VALIDITY_SECONDS, (
            "观察结束前已到达量测有效期，无法单独证明坏品质导致退出")


def _recover(context):
    marker = len(context.simulator.events()) - 1
    context.simulator.move_measurement(context.sim_points, UNMAPPED_ADDRESS, KEY_ADDRESS)
    value = context.sim_points[0]["value"]
    reply = context.simulator._wait(
        lambda _: next(_reads(context, KEY_ADDRESS, b"\x03\x02" + value.to_bytes(2, "big"), after=marker), None),
        "关键量测未恢复正常应答", timeout=context.timeout,
    )
    _wait_telemetry(context, context.measured_ioa, value, 0, after=getattr(context, "bad_frame", 0))
    if context.module == "AGC":
        expected = dict.fromkeys(("A", "B"), 40 if context.missing else 50)
    else:
        if context.missing:
            context.sender.send_target_kw(230, timeout=context.timeout)
        expected = {"inverter": 20 if context.missing else 30}
    _expect_writes(context, expected, after=reply[0])


def _stop_member_polling(context):
    marker = len(context.simulator.events()) - 1
    context.rtu.open_current_links()
    context.rtu.select(context.connections["A"])
    context.rtu.stop()
    context.group._until(
        lambda _: context.group.read_member_communication("A")["condition"] == "已停止",
        "AGC 成员 A 未显示已停止", context.timeout,
    )
    _expect_writes(context, {"B": 80}, after=marker, previous={"B": 40})


def _restore_communication_only(context):
    context.simulator.move_measurement(context.sim_points, KEY_ADDRESS, UNMAPPED_ADDRESS)
    marker = len(context.simulator.events()) - 1
    context.rtu.open_current_links()
    context.rtu.select(context.connections["A"])
    context.rtu.start()
    _wait_measurement_fault(context, marker)
    bad = _wait_telemetry(context, context.measured_ioa, context.sim_points[0]["value"], 0xC0)
    context.bad_frame = int(bad["frame_id"])
    context.sender.send_target_kw(100, timeout=context.timeout)
    _expect_writes(context, {"B": 100}, after=marker, previous={"B": 80})
    _no_write_window(context, marker)


ENVIRONMENT = Step("measurement-quality.environment", "检查 slaveSim 和独立 IEC104 主站入口",
                   "模拟器、主站和驱动可用")
SIM_START = Step("measurement-quality.sim.start", "打开本例 slaveSim", "模拟器配置和报文日志可用")
SIM_FINISH = Step("measurement-quality.sim.finish", "关闭本例 slaveSim", "独立浏览器会话关闭")
PREPARE = (
    Step("measurement-quality.rtu", "创建专用 RTU 量测、参考和设定点", "回读两条连接和寄存器，参考量测正常", _setup_rtu),
    Step("measurement-quality.group", "创建本例控制组", "控制点位绑定本例 RTU 连接", _setup_group),
    Step("measurement-quality.104", "配置 IEC104 总目标及遥测路由", "回读本例端口、IOA 与路由", _setup_104),
)
CONNECT = Step("measurement-quality.connect", "连接主站、启动控制组并总召", "主站与控制组运行，收到总召确认和终止", _connect)
CAPTURE = Step("measurement-quality.capture", "保存模拟主站原始报文", "记录本例 IEC104 收发报文")


def measurement_steps(module, *, missing, partial_recovery=False):
    if partial_recovery:
        recovery = "补齐 A 的有功量测后两台自动各收到 50 kW"
        verify = (
            Step("measurement-quality.baseline", "遥调 80 kW", "两台各收到 40 kW", _baseline),
            Step("measurement-quality.stop", "只停止成员 A 的 RTU 轮询",
                 "A 已停止，B 收到 80 kW", _stop_member_polling),
            Step("measurement-quality.partial", "恢复 A 的轮询，但只恢复同站参考读",
                 "A 通信正常、关键量测坏品质；遥调 100 kW 仍只写入 B", _restore_communication_only),
        )
    elif missing:
        check = "仅 B 收到 80 kW，A 无写请求" if module == "AGC" else "目标电压遥调收到负确认，成员无写请求"
        recovery = "沿用总目标，两台各收到 40 kW" if module == "AGC" else "重发电压目标获接受，成员收到 20 kvar"
        verify = (Step("measurement-quality.missing", "观察关键量测无首值时的实际下发", check, _check_initial_missing),)
    else:
        check = "A 量测 IEC104 品质 0xC0，A 无写请求且 B 收到 100 kW" if module == "AGC" else (
            "电压 IEC104 品质 0xC0，更新无功量测后成员无写请求")
        recovery = "沿用总目标，两台各收到 50 kW" if module == "AGC" else "沿用电压目标，成员收到 30 kvar"
        verify = (
            Step("measurement-quality.baseline", "使用有效量测完成正常下发", "成员收到基线写请求并应答", _baseline),
            Step("measurement-quality.bad", "保持同站通信正常并注入量测异常应答", check, _check_bad_quality),
        )
    return (CONNECT, *verify, Step("measurement-quality.recovery", "恢复关键量测并验证实际下发", recovery, _recover))


def measurement_plan(module, *, missing, partial_recovery=False):
    return (ENVIRONMENT, SLAVESIM_BROWSER_READY, DESKTOP_START, *LOGIN, SITE_CONNECT, SIM_START, *PREPARE,
            MASTER104_START, *measurement_steps(module, missing=missing, partial_recovery=partial_recovery), CAPTURE,
            UPPER_CLEANUP, SIM_CLEANUP, SIM_FINISH, MASTER104_FINISH, DESKTOP_FINISH)


def _save_master_evidence(master):
    path = master.artifacts / "control-measurement-iec104-frames.json"
    path.write_text(json.dumps(snapshot(master)["frames"], ensure_ascii=False, indent=2), encoding="utf-8")
    master.metadata["本例 IEC104 收发报文"] = path.name


def run_measurement_case(request, module, *, missing, partial_recovery=False):
    with track_fixture_step(request, ENVIRONMENT):
        url, timeout = _environment()
    with track_fixture_step(request, SLAVESIM_BROWSER_READY):
        browser = request.getfixturevalue("slavesim_browser")
    desktop = request.getfixturevalue("fresh_desktop")
    name = f"e2e-meas-{uuid4().hex[:8]}"
    rtu = ModbusRtuPage(desktop)
    slow, fast = SCENARIOS[module]
    group = MeasurementExpiryAgc(desktop) if module == "AGC" else AvcPeriodPage(desktop)
    context = SimpleNamespace(name=name, module=module, slow=slow, fast=fast, missing=missing, timeout=timeout,
                              desktop=desktop, page=PlatformLoginPage(desktop), rtu=rtu, group=group,
                              iec104=RtuQualityIec104(rtu, name), master=None, sim_started=False)
    run_steps(request, LOGIN, context)
    with track_fixture_step(request, SITE_CONNECT):
        context.host = rtu.connect_site().rsplit(":", 1)[0]
    with track_fixture_context(request, SIM_START, SIM_FINISH, QualitySimulator(desktop, url, browser=browser)) as simulator:
        context.simulator = simulator
        failure = None
        try:
            run_steps(request, PREPARE, context)
            context.master = request.getfixturevalue("master104")
            run_steps(request, measurement_steps(module, missing=missing, partial_recovery=partial_recovery), context)
            with track_fixture_step(request, CAPTURE):
                _save_master_evidence(context.master)
        except BaseException as error:
            failure = error
            for source in (desktop, simulator, context.master):
                if source is not None:
                    try:
                        source.capture("control-measurement-failure", description="异常量测验收清理前现场")
                    except Exception as capture_error:
                        error.add_note(f"截图失败：{capture_error}")
            if context.master is not None:
                try:
                    _save_master_evidence(context.master)
                except Exception as evidence_error:
                    error.add_note(f"主站报文取证失败：{evidence_error}")
            raise
        finally:
            _cleanup(context, request, failure)
