"""通过 DSP 上位机验证 RTU 配置，通过 slaveSim 验证采集。"""
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime
import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from testbench.case_plan import (
    DESKTOP_FINISH, DESKTOP_START, SITE_CONNECT, SLAVESIM_BROWSER_READY, Step, case_plan,
    run_case_plan, run_steps, track_fixture_context, track_fixture_step,
)
from testbench.login_page import PlatformLoginPage, load_login_account
from case_support.case_metadata import execution_logic
from case_support.login_plan import login_steps
from testbench.modbus_rtu_config import assert_dual_observation, expected_value
from testbench.modbus_environment import BASIC_CASE_IDS, validate_selected_modbus_cases
from case_support.modbus_rtu_page import ModbusRtuPage
from testbench.workspace import local_config


pytestmark = pytest.mark.case_group("Modbus RTU")
LOGGER = logging.getLogger(__name__)
RAW_FACTORS = (1, 0)
SCALED_FACTORS = ("0.1", 2)
RTU_PREPARE = Step("rtu.prepare", "读取 RTU 测试入口", "已提供 slaveSim 入口、采集时限和专用测试账号")
SIMULATOR_START = Step("rtu.simulator.start", "打开 slaveSim 观察会话", "运行页面已加载且收到新刷新")
RTU_LOGIN_STEPS = login_steps("login")
RTU_OPEN = Step("rtu.open", "通过上位机当前业务入口打开 RTU 配置", "上位机显示可读取的连接列表")
RTU_PREPARE_SIM = Step("rtu.simulator.prepare", "检查并清空 slaveSim 原有总线，按本例重建总线、站点及寄存器",
                       "仅保留本例新建总线，已记录运行态总线、串口和实际站点映射")
RTU_CLEANUP = Step("rtu.cleanup", "停止并清理本次创建的 RTU 连接及点表", "本例连接已删除")
CONFIGURATION_PREPARE = Step("rtu.config.prepare", "读取专用测试账号", "测试账号配置有效")
CONFIGURATION_CLEANUP = Step("rtu.config.cleanup", "清理本次创建的 RTU 连接及点表", "本例连接已删除")
SIMULATOR_CLEANUP = Step("rtu.simulator.cleanup", "清空 slaveSim 的全部总线", "保存后回读确认无总线")
SIMULATOR_FINISH = Step("rtu.simulator.finish", "关闭 slaveSim 会话", "浏览器会话已关闭")


class RtuScenario:
    def __init__(self, desktop, simulator, upper, environment):
        self.desktop, self.simulator, self.upper, self.environment = desktop, simulator, upper, environment
        self.connections = {}
        self.observations = {}
        self.pairs = []

    def point(self, index):
        return self.environment.stations[index]

    def observe_simulator(self, index):
        point = self.point(index)
        observed = self.simulator.observe(self.environment.bus, point.station_id, "0x03", point.address,
                                          verify_manual=False)
        self.desktop.check("两端使用同一专用串口路径", observed["link_path"], self.environment.serial_device)
        return observed

    def set_simulator(self, index, expected):
        point = self.point(index)
        config = self.environment
        observed = self.simulator.set_value(config.bus, point.station_id, "0x03", point.address, expected)
        self.desktop.check("模拟器工况对应本例串口", observed["link_path"], config.serial_device)
        self.desktop.check("模拟器工况已生效", observed["value"], expected)

    def create_connection(self, index, *, scaled=False):
        config = self.environment
        name = f"e2e-rtu-{uuid4().hex[:16]}"
        self.upper.create_connection(name, config.serial_device, config.baud_rate, config.data_bits,
                                     config.stop_bits, config.parity, self.point(index).station_id, config.poll_interval_ms)
        scale, offset = SCALED_FACTORS if scaled else RAW_FACTORS
        self.connections[index] = {"name": name, "tag": f"{name}-value", "scale": scale, "offset": offset}

    def add_point(self, index):
        point = self.connections[index]
        self.upper.add_point(point["tag"], address=self.point(index).address, scale=point["scale"], offset=point["offset"])

    def start(self, index):
        self.upper.select(self.connections[index]["name"])
        self.upper.start()

    def select_for_observation(self, index, raw, previous_timestamp):
        connection = self.connections[index]
        self.upper.select(connection["name"])
        return RtuObservation(self, index, raw, connection, previous_timestamp)

    def _record_pair(self, index, raw, upper, simulator, *, error=None):
        """成功与超时都保存固定期望及两端观察，缺失观察必须明确标注。"""
        connection = self.connections[index]
        pair = {"观察时间": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                "总线": self.environment.bus, "串口": self.environment.serial_device,
                "站号": self.point(index).station_id, "功能码": "0x03", "零基地址": self.point(index).address,
                "Tag": connection["tag"], "倍率": str(connection["scale"]), "偏移": str(connection["offset"]),
                "原始期望": str(raw), "工程量期望": str(expected_value(raw, connection["scale"], connection["offset"])),
                "slaveSim": simulator, "DSP上位机": upper}
        if error:
            pair["观察错误"] = error
        self.pairs.append(pair)
        self.desktop.metadata["RTU双端校验"] = self.pairs
        (self.desktop.artifacts / "modbus-rtu-observations.json").write_text(
            json.dumps(self.pairs, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


class RtuObservation:
    """同一轮双端观察及其失败证据，供各验收场景复用。"""

    def __init__(self, scenario, index, raw, connection, previous_timestamp):
        self.scenario, self.index, self.raw = scenario, index, raw
        self.connection, self.previous_timestamp = connection, previous_timestamp
        self.expected = expected_value(raw, connection["scale"], connection["offset"])
        self.desktop = scenario.desktop
        self.upper = self.simulator = None
        self.evidence_attempted = False

    def wait_upper(self):
        try:
            self.upper = self.scenario.upper.wait_value(
                self.connection["tag"], self.expected, "0.000001", self.scenario.environment.acquisition_timeout,
                previous_timestamp=self.previous_timestamp,
            )
        except BaseException as error:
            self._preserve_read_failure(error)
            raise

    def read_simulator(self):
        try:
            self.simulator = self.scenario.observe_simulator(self.index)
        except BaseException as error:
            self._preserve_read_failure(error)
            raise

    def _preserve_read_failure(self, error):
        if isinstance(error, InterruptedError):
            self.preserve_interruption(error)
            return
        # 补读另一端只用于取证，保留失败时上位机最后观察和原始异常。
        self.upper = self._last_upper()
        try:
            self.simulator = self.scenario.observe_simulator(self.index)
        except Exception as observation_error:
            self.simulator = {"观察错误": str(observation_error)}
        self._preserve_failure(error)

    def _last_upper(self):
        connection = self.connection
        return self.desktop.metadata.get("RTU最后观察", {}).get(
            f"{connection['name']}/{connection['tag']}", {"观察错误": "本轮未取得上位机点值"})

    def preserve_interruption(self, error):
        if self.evidence_attempted:
            return
        if self.upper is None:
            self.upper = self._last_upper()
        if self.simulator is None:
            self.simulator = {"观察错误": "任务停止，未完成本轮模拟器观察"}
        self._preserve_failure(error)

    def _preserve_failure(self, error):
        self.evidence_attempted = True
        try:
            self.scenario._record_pair(self.index, self.raw, self.upper, self.simulator, error=str(error))
        except Exception as evidence_error:
            self.desktop.metadata.setdefault("RTU取证错误", []).append(str(evidence_error))
            LOGGER.exception("双端失败对照保存异常，保留原业务错误")

    def check_values(self):
        station = self.scenario.point(self.index).station_id
        with self.desktop.step(f"双端核对站号 {station} 采集结果",
                               expected=f"slaveSim={self.raw}；上位机={self.expected}，质量正常且采集时间有效"):
            try:
                assert_dual_observation(self.simulator, self.upper, self.raw, scale=self.connection["scale"],
                                        offset=self.connection["offset"], previous_timestamp=self.previous_timestamp)
            except AssertionError as error:
                self._preserve_failure(error)
                raise

    def check_address(self):
        self._check_point("上位机显示配置的寄存器地址", self.upper["address"],
                          str(self.scenario.point(self.index).address))

    def check_function(self):
        self._check_point("上位机显示读取保持寄存器", "0x03" in self.upper["function"], True)

    def _check_point(self, name, actual, expected):
        try:
            self.desktop.check(name, actual, expected)
        except AssertionError as error:
            self._preserve_failure(error)
            raise

    def save_evidence(self):
        self.evidence_attempted = True
        self.scenario._record_pair(self.index, self.raw, self.upper, self.simulator)
        self.desktop.capture(f"rtu-pair-{len(self.scenario.pairs):02d}", description="DSP上位机 RTU 采集结果",
                             expected=str(self.expected), actual=json.dumps(self.upper, ensure_ascii=False, default=str))


def observation_steps(prefix, index, raw, *, scaled=False, previous=None):
    expected = expected_value(raw, *(SCALED_FACTORS if scaled else RAW_FACTORS))
    label = f"第 {index + 1} 站{'后续' if previous else '初始'}采集"

    def select(scenario):
        previous_timestamp = scenario.observations[previous].upper["timestamp"] if previous else None
        scenario.observations[prefix] = scenario.select_for_observation(index, raw, previous_timestamp)

    def action(method):
        return lambda scenario: method(scenario.observations[prefix])

    time_expected = "采集时间与前次不同" if previous else "采集时间有效"
    return (
        Step(f"{prefix}.select", f"选择{label}的连接", "连接编号与本次创建的一致", select),
        Step(f"{prefix}.upper", f"等待{label}的上位机点值", f"显示 {expected}，质量正常，{time_expected}",
             action(RtuObservation.wait_upper)),
        Step(f"{prefix}.simulator", f"读取{label}的 slaveSim 寄存器", "读到同一专用链路上的目标寄存器",
             action(RtuObservation.read_simulator)),
        Step(f"{prefix}.compare", f"核对{label}的双端结果",
             f"slaveSim 原始值为 {raw}，上位机显示 {expected}，质量正常，{time_expected}",
             action(RtuObservation.check_values)),
        Step(f"{prefix}.address", f"核对{label}的寄存器地址", "上位机显示配置的寄存器地址",
             action(RtuObservation.check_address)),
        Step(f"{prefix}.function", f"核对{label}的功能码", "上位机显示读取保持寄存器 0x03",
             action(RtuObservation.check_function)),
        Step(f"{prefix}.evidence", f"保存{label}的双端证据", "保存本轮观察及上位机截图",
             action(RtuObservation.save_evidence)),
    )


def rtu_plan(*steps):
    return case_plan(RTU_PREPARE, SLAVESIM_BROWSER_READY, DESKTOP_START, SIMULATOR_START,
                     *RTU_LOGIN_STEPS, SITE_CONNECT, RTU_OPEN, RTU_PREPARE_SIM,
                     *steps, RTU_CLEANUP, SIMULATOR_CLEANUP, SIMULATOR_FINISH, DESKTOP_FINISH)


def run_observations(request, steps, scenario):
    try:
        run_steps(request, steps, scenario)
    except InterruptedError as error:
        latest = next(reversed(scenario.observations.values()), None)
        if latest is not None:
            latest.preserve_interruption(error)
        raise


@contextmanager
def _rtu_session(request, *, station_count=1):
    """打开本例的两个会话，失败时在清理前保留现场。"""
    from case_support.slavesim_page import SlaveSimPage
    from testbench.model import validate_slavesim_url

    with track_fixture_step(request, RTU_PREPARE):
        task_path = os.environ.get("TESTBENCH_TASK")
        if not task_path:
            raise RuntimeError("RTU 双端用例需要从测试上位机运行，以读取任务环境和 slaveSim 地址")
        task = json.loads(Path(task_path).read_text(encoding="utf-8"))
        environment = task["environment"]
        validate_selected_modbus_cases(environment, BASIC_CASE_IDS)
        simulator_url = validate_slavesim_url(environment.get("slavesim_url", ""), required=True)
        account = load_login_account(local_config("login-account.json"))
    with track_fixture_step(request, SLAVESIM_BROWSER_READY):
        browser = request.getfixturevalue("slavesim_browser")
    desktop = request.getfixturevalue("fresh_desktop")
    desktop.metadata["RTU验收阶段"] = "前置条件"
    upper = ModbusRtuPage(desktop)

    def capture_failure(simulator):
        for source, capture in (("slaveSim", simulator.capture), ("DSP上位机", desktop.capture)):
            try:
                capture("rtu-failure-before-cleanup", description=f"{source} 清理前失败现场")
            except Exception as error:
                LOGGER.exception("%s 失败现场采集异常，保留原业务错误", source)
                desktop.metadata.setdefault("RTU取证错误", []).append(f"{source}：{error}")

    with track_fixture_context(request, SIMULATOR_START, SIMULATOR_FINISH,
                               SlaveSimPage(desktop, simulator_url, browser=browser)) as simulator:
        setup_failed = False
        simulator_preparation_started = False
        try:
            login = SimpleNamespace(desktop=desktop, page=PlatformLoginPage(desktop), account=account)
            run_steps(request, RTU_LOGIN_STEPS, login)
            with track_fixture_step(request, SITE_CONNECT):
                upper.connect_site()
            with track_fixture_step(request, RTU_OPEN):
                upper.open_current_links()
            with track_fixture_step(request, RTU_PREPARE_SIM):
                simulator_preparation_started = True
                config = simulator.prepare_basic(environment, station_count=station_count,
                                                 check_paths=upper.assert_serial_paths_available)
            desktop.metadata["RTU环境"] = asdict(config)
            desktop.metadata["RTU验收阶段"] = "业务验收"
            yield RtuScenario(desktop, simulator, upper, config)
        except BaseException:
            setup_failed = True
            capture_failure(simulator)
            raise
        finally:
            case_failed = any(report.failed for report in getattr(request.node, "_bench_reports", []))
            stop_path = os.environ.get("TESTBENCH_STOP")
            case_stopped = bool(stop_path and Path(stop_path).exists())
            had_failure = setup_failed or case_failed or case_stopped
            if not setup_failed and (case_failed or case_stopped):
                capture_failure(simulator)
            cleanup_failure = None
            if upper.creation_started:
                try:
                    with track_fixture_step(request, RTU_CLEANUP):
                        upper.cleanup()
                except Exception as error:
                    cleanup_failure = error
                    desktop.metadata["RTU连接清理错误"] = str(error)
                    LOGGER.exception("RTU连接清理失败")
                    if not had_failure:
                        capture_failure(simulator)
            if simulator_preparation_started:
                try:
                    with track_fixture_step(request, SIMULATOR_CLEANUP):
                        simulator.clear_all_buses()
                except Exception as error:
                    cleanup_failure = cleanup_failure or error
                    desktop.metadata["slaveSim清理错误"] = str(error)
                    LOGGER.exception("slaveSim 总线清理失败")
            if cleanup_failure is not None and not had_failure:
                raise cleanup_failure


@pytest.fixture
def independent_rtu(request):
    with _rtu_session(request) as scenario:
        yield scenario


@pytest.fixture
def two_station_rtu(request):
    with _rtu_session(request, station_count=2) as scenario:
        yield scenario


@pytest.fixture
def configuration_rtu(request):
    with track_fixture_step(request, CONFIGURATION_PREPARE):
        account = load_login_account(local_config("login-account.json"))
    desktop = request.getfixturevalue("fresh_desktop")
    context = SimpleNamespace(
        desktop=desktop, page=PlatformLoginPage(desktop), account=account,
        upper=ModbusRtuPage(desktop), name=f"e2e-rtu-{uuid4().hex[:16]}", site_connected=False,
    )
    try:
        yield context
    finally:
        if context.upper.creation_started:
            with track_fixture_step(request, CONFIGURATION_CLEANUP):
                try:
                    if not context.site_connected or desktop.driver is None:
                        raise RuntimeError(f"现场直连不可用，请检查本例连接 {context.name} 是否仍存在")
                    context.upper.cleanup()
                except Exception as error:
                    desktop.metadata["RTU连接清理错误"] = str(error)
                    raise


def connect_configuration_site(context):
    context.upper.connect_site()
    context.site_connected = True


def restart_configuration_desktop(context):
    context.site_connected = False
    context.desktop.restart()


def point_setup(index, *, scaled=False):
    prefix = f"rtu.s{index + 1}"
    label = f"第 {index + 1} 站"
    factors = "，倍率 0.1、偏移 2" if scaled else ""
    return (
        Step(f"{prefix}.create", f"创建并保存{label} RTU 连接", "列表显示新连接及设备分配的编号",
             lambda scenario: scenario.create_connection(index, scaled=scaled)),
        Step(f"{prefix}.add_point", f"新增并保存{label}采集点", f"点表显示 0x03 UINT16 采集点{factors}",
             lambda scenario: scenario.add_point(index)),
    )


def start_station(index):
    return Step(f"rtu.s{index + 1}.start", f"启动第 {index + 1} 站轮询并打开运行视图",
                "连接进入运行中，显示实时值列", lambda scenario: scenario.start(index))


def initial_source(index, expected):
    return Step(f"rtu.s{index + 1}.initial_source", f"设置 slaveSim 第 {index + 1} 站的初始原始值",
                f"实际寄存器显示 {expected}", lambda scenario: scenario.set_simulator(index, expected))


SET_FIRST_CHANGE = Step("rtu.s1.set_change", "将 slaveSim 第一站切换到下一工况",
                        "同一寄存器的新反馈显示 2345", lambda scenario: scenario.set_simulator(0, 2345))


FIRST_BASELINE = observation_steps("first.baseline", 0, 1234)
FIRST_CHANGE = observation_steps("first.change", 0, 2345, previous="first.baseline")
FIRST_SOURCE = initial_source(0, 1234)
INITIAL_ACQUISITION = (FIRST_SOURCE, *point_setup(0), start_station(0), *FIRST_BASELINE)
SOURCE_CHANGE = (*INITIAL_ACQUISITION, SET_FIRST_CHANGE, *FIRST_CHANGE)
SCALED_CHANGE = (
    FIRST_SOURCE, *point_setup(0, scaled=True), start_station(0),
    *observation_steps("scaled.baseline", 0, 1234, scaled=True),
    SET_FIRST_CHANGE,
    *observation_steps("scaled.change", 0, 2345, scaled=True, previous="scaled.baseline"),
)
TWO_STATIONS = (
    FIRST_SOURCE, initial_source(1, 4321),
    *point_setup(0), *point_setup(1), start_station(0), start_station(1),
    *FIRST_BASELINE,
    *observation_steps("second.baseline", 1, 4321),
    SET_FIRST_CHANGE, *FIRST_CHANGE,
    *observation_steps("second.change", 1, 4321, previous="second.baseline"),
)


@case_plan(
    CONFIGURATION_PREPARE, DESKTOP_START,
    *RTU_LOGIN_STEPS,
    Step(SITE_CONNECT.id, SITE_CONNECT.action, SITE_CONNECT.expected, connect_configuration_site),
    Step(RTU_OPEN.id, RTU_OPEN.action, RTU_OPEN.expected, lambda context: context.upper.open_current_links()),
    Step("rtu.config.create", "创建并保存 RTU 连接", "列表显示新连接及设备分配的编号",
         lambda context: context.upper.create_connection(
             context.name, serial_device=f"/dev/{context.name}", baud_rate=19200, data_bits=8,
             stop_bits=1, parity="E", station_id=7, poll_interval_ms=750,
         )),
    Step("rtu.config.point", "新增并保存采集点", "点表显示 0x03 UINT16 采集点",
         lambda context: context.upper.add_point(
             f"{context.name}-value", address=16, scale="0.1", offset=2, deadband="0.5",
         )),
    Step("rtu.config.restart", "完全关闭并重新启动 DSP 上位机", "原进程退出，同一测试副本重新启动",
         restart_configuration_desktop),
    *login_steps("rtu.relogin"),
    Step("rtu.config.reconnect", "重新连接同一台现场下位机", SITE_CONNECT.expected, connect_configuration_site),
    Step("rtu.config.readback", "重新打开 RTU 配置并核对连接和点表", "回读配置逐项与保存前一致",
         lambda context: context.upper.assert_saved_configuration()),
    CONFIGURATION_CLEANUP, DESKTOP_FINISH,
)
@execution_logic("""
1. 在独立副本中登录并连接现场下位机。新建专用 RTU 连接，配置本地串口、19200 波特率、8E1、站号 7、零基地址、750 ms 轮询和 1000 ms 读取超时，保存并记录连接编号。
2. 新增并保存地址 16 的 0x03 UINT16 采集点，设置单寄存器、AB 字节序、倍率 0.1、偏移 2、死区 0.5。
3. 正常关闭并重开同一副本，重新登录和连接同一下位机。从上位机重新打开该连接和点位的编辑界面，逐项核对回读配置与保存值一致。
4. 删除本例连接及点表，并确认连接从列表消失。
""")
def test_rtu_configuration_saved_on_both_pages(request, configuration_rtu):
    """重开后回读 RTU 连接与点表"""
    run_case_plan(request, configuration_rtu)


@pytest.mark.requires_modbus
@rtu_plan(*INITIAL_ACQUISITION)
@execution_logic("""
1. 登录上位机并连接现场下位机。清除 slaveSim 原有总线，重建单站总线及手动寄存器，读回实际串口、站号和地址。
2. 将模拟器寄存器置为 1234 并确认生效。用实测参数创建 RTU 连接和 0x03 UINT16 点，设置倍率 1、偏移 0，启动轮询并打开运行视图。
3. 在采集时限内核对模拟器原始值和上位机实时值均为 1234，且上位机质量正常、采集时间有效、地址和功能码与配置一致。
4. 保存双端观察和上位机截图，失败时先保留现场。删除本例 RTU 连接及点表，清空 slaveSim 总线并回读确认。
""")
def test_rtu_initial_acquisition_matches_simulator(request, independent_rtu):
    """RTU 首次采集匹配模拟值"""
    run_observations(request, INITIAL_ACQUISITION, independent_rtu)


@pytest.mark.requires_modbus
@rtu_plan(*SOURCE_CHANGE)
@execution_logic("""
1. 登录上位机并连接现场下位机。用 slaveSim 重建单站总线，将手动寄存器置为 1234 并确认生效。按实际串口、站号和地址配置 0x03 UINT16 点，倍率为 1、偏移为 0，启动轮询。
2. 在采集时限内核对模拟器和上位机均为 1234，且上位机质量正常、地址和功能码正确，记录有效的采集时间。
3. 在 slaveSim 将同一寄存器改为 2345 并回读确认。保持轮询，在时限内核对上位机实时值为 2345、质量仍正常、采集时间有效且与前次不同。
4. 保存两轮双端对照和截图，失败时先保留现场。删除本例 RTU 连接及点表，清空 slaveSim 总线并回读确认。
""")
def test_rtu_source_change_updates_value_and_time(request, independent_rtu):
    """RTU 源值变化刷新值和时间"""
    run_observations(request, SOURCE_CHANGE, independent_rtu)


@pytest.mark.requires_modbus
@rtu_plan(*SCALED_CHANGE)
@execution_logic("""
1. 登录上位机并连接现场下位机。用 slaveSim 重建单站总线，将手动寄存器置为 1234 并确认生效，读回实际串口、站号和地址。
2. 按实际参数新建 RTU 连接和 0x03 UINT16 点，设置倍率 0.1、偏移 2，启动轮询。在时限内核对模拟器为 1234、上位机工程量为 125.4，并记录采集时间。
3. 将同一寄存器改为 2345 并确认生效，在时限内核对上位机工程量更新为 236.5，采集时间与前次不同。两轮均核对质量正常、采集时间有效、地址和功能码正确。
4. 保存两轮原始值和工程量对照及截图，失败时先保留现场。删除本例 RTU 连接及点表，清空 slaveSim 总线并回读确认。
""")
def test_rtu_scale_and_offset_match_fixed_expectations(request, independent_rtu):
    """RTU 倍率与偏移换算"""
    run_observations(request, SCALED_CHANGE, independent_rtu)


@pytest.mark.requires_modbus
@rtu_plan(*TWO_STATIONS)
@execution_logic("""
1. 登录上位机并连接现场下位机。用 slaveSim 重建一条总线和两个从站，将两站手动寄存器分别置为 1234、4321 并确认生效。
2. 在上位机用同一实际串口、不同站号分别创建连接和 0x03 UINT16 点，倍率为 1、偏移为 0，启动两站轮询。在时限内逐站核对双端值，并记录两站的采集时间。
3. 只把第一站改为 2345。在时限内确认第一站采集到 2345，第二站采集时间已刷新且仍为 4321。两轮均逐站核对模拟器原始值、上位机质量、采集时间、地址和功能码。
4. 保存各站两轮的双端对照和截图，失败时先保留现场。删除本例 RTU 连接及点表，清空 slaveSim 总线并回读确认。
""")
def test_rtu_two_stations_do_not_cross_values(request, two_station_rtu):
    """同一 RTU 总线两站不串值"""
    run_observations(request, TWO_STATIONS, two_station_rtu)
