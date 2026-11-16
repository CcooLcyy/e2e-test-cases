"""用 RTU 检修遥信、IEC104 遥调和实际写报文验收成员检修。"""

from datetime import timedelta
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
from case_support.avc_period_page import GROUP, MEMBER, SUMMARY, AvcPeriodPage
from case_support.case_metadata import execution_logic
from case_support.control_measurement_quality import _reads
from case_support.external_direction_rtu import ExternalDirectionRtuPage, check_external_allocation
from case_support.login_plan import login_steps
from case_support.measurement_expiry_agc import MeasurementExpiryAgc
from case_support.measurement_expiry_run import (
    SIM_CLEANUP, UPPER_CLEANUP, WRITE_WINDOW, _await, _cleanup, _expect_writes,
    _prepare_104, _protocol, prepare_control_rtu,
)
from case_support.measurement_expiry_simulator import MeasurementExpirySimulator
from case_support.modbus_rtu_page import xpath_text
from case_support.power_range_run import _environment, _load_account, _save_frames


pytestmark = pytest.mark.requires_modbus

STATUS_LABEL = "检修状态点（可选）"
STATUS_TAG = "maintenance-status"


def _read_binding(page, root, member, expected):
    label = xpath_text(STATUS_LABEL)
    locator = (By.XPATH, root + f"//input[@aria-label={label}]"
               "/ancestor::div[contains(concat(' ',normalize-space(@class),' '),' ant-select ')][1]")
    control = page._until(lambda _: page._visible(locator), f"成员 {member} 编辑界面未显示{STATUS_LABEL}绑定入口")
    values = control.find_elements(By.CSS_SELECTOR, ".ant-select-content-has-value, .ant-select-selection-item")
    value = next((item.text.strip() for item in values if item.is_displayed()), "")
    page.desktop.check(f"读回成员 {member} 的检修绑定", value, expected)


class MaintenanceAgcPage(MeasurementExpiryAgc):
    def _edit_group_members(self):
        self._select_owned_group()
        self._click(self._button("编辑", self.OVERVIEW), "打开本例 AGC 控制组配置")
        self._draft_opened = True
        self._members_tab()

    def _edit_member(self, name):
        selector = self.GROUP_DIALOG + "//button[@aria-label=" + xpath_text(f"编辑成员 {name}") + "]"
        self._click((By.XPATH, selector), f"编辑 AGC 成员 {name}")
        self._open_member_advanced()

    def bind_maintenance(self):
        self._edit_group_members()
        self._edit_member("A")
        connection = self.members["A"]
        source = f"ModbusRTU / {connection}"
        self._choose(self.MEMBER_DIALOG, f"{STATUS_LABEL}连接", source, search=connection, accessible_name=True)
        self._choose(self.MEMBER_DIALOG, STATUS_LABEL, f"{source} / {STATUS_TAG}", search=STATUS_TAG, accessible_name=True)
        self._click(self._button("确认成员", self.MEMBER_DIALOG), "确认 A 的检修状态点")
        self._wait_dialog_closed(self.MEMBER_DIALOG, "确认成员")
        self._submit_group("保存 AGC 检修状态点与路由")

        self._edit_group_members()
        for member, expected in (("A", f"{source} / {STATUS_TAG}"), ("B", "")):
            self._edit_member(member)
            _read_binding(self, self.MEMBER_DIALOG, member, expected)
            self._close_draft(self.MEMBER_DIALOG, "取消编辑")
        self._close_draft(self.GROUP_DIALOG, "取消")
        self._draft_opened = False


class MaintenanceAvcPage(AvcPeriodPage):
    def __init__(self, desktop):
        super().__init__(desktop)
        self.maintenance_connection = None

    def _bind_member(self, label, connection, tag):
        super()._bind_member(label, connection, tag)
        if label == "无功设定点" and connection == self.maintenance_connection:
            super()._bind_member(STATUS_LABEL, connection, STATUS_TAG)

    def verify_maintenance_binding(self):
        self._select_owned()
        self._click(self._button("编辑", SUMMARY), "打开本例 AVC 控制组读回检修映射")
        self.group_draft_opened = True
        self._open_group_members(GROUP, "AVC")
        source = f"ModbusRTU / {self.maintenance_connection} / {STATUS_TAG}"
        for member, expected in (("A", source), ("B", "")):
            selector = GROUP + "//button[@aria-label=" + xpath_text(f"编辑成员 {member}") + "]"
            self._click((By.XPATH, selector), f"编辑 AVC 成员 {member}")
            _read_binding(self, MEMBER, member, expected)
            self._discard_draft(MEMBER, "取消编辑")
        self._discard_draft(GROUP, "取消")
        self.group_draft_opened = False


def _prepare_rtu(context):
    specs = {"A": (0, 0, 0), "B": (20, 0)}
    if context.module == "AVC":
        specs["V"] = (220,)

    def configure_status(role, extra_points):
        if role == "A":
            context.status_point, = extra_points
            context.rtu.add_direction_bit(STATUS_TAG, context.status_point["address"], 0)

    prepare_control_rtu(context, specs, configure_extra_points=configure_status)
    context.points["A"] = context.points["A"][:2]
    context.rtu.select(context.connections["A"])
    context.rtu.wait_direction_bit(STATUS_TAG, False, context.timeout)


def _prepare_group(context):
    group = context.group
    if context.module == "AGC":
        context.target_tag = group.create_communication_group(context.name, context.connections)["command_tag"]
        group.bind_maintenance()
        return
    a, b, voltage = (context.connections[key] for key in ("A", "B", "V"))
    group.maintenance_connection = a
    context.target_tag = f"{context.name}-target"
    group.create_group(
        context.name, mode="q_total", calculation_period=1, command_period=4,
        voltage_tag="measured", voltage_connection=voltage, target_tag=context.target_tag,
        rtu_connection=a, q_measurement_tag="measured", q_setpoint_tag="setpoint",
        member_name="A", q_min=10, q_max=100,
        additional_members=(("B", b, "measured", "setpoint", 10, 100),),
    )
    context.desktop.check("核对 AVC 已保存的分配方式", group._read_summary("分配方式"), "平均分配（等权）")
    group.verify_maintenance_binding()


def _connect(context):
    context.master.prepare_target(context.host, context.iec104.port, 1)
    context.master.connect()
    context.sender = Master104CommandSender(context.master, 1, context.ioa)
    context.group.start()
    if context.module == "AGC":
        context.group._until(
            lambda _: all(context.group.read_member_communication(member)["condition"] == "通信正常" for member in ("A", "B")),
            "AGC 两个成员未全部通信正常",
            timeout=context.timeout,
        )


def _status_samples(context, value, after):
    point = context.status_point
    observation = SimpleNamespace(simulator=context.simulator, sim_points=(point,))
    return _reads(observation, point["address"], bytes((3, 2, 0, value)), after=after)


def _set_maintenance(context, active):
    marker = len(context.simulator.events()) - 1
    point = context.status_point
    value = int(active)
    actual = context.simulator.set_value(point["bus"], point["station_id"], point["function"], point["address"], value)
    context.desktop.check("核对模拟端检修状态字", actual["value"], value)
    context.rtu.open_current_links()
    context.rtu.select(context.connections["A"])
    state = context.rtu.wait_direction_bit(STATUS_TAG, active, context.timeout)
    measured = context.rtu.wait_value("measured", 0, 0, context.timeout)
    sample = context.simulator._wait(
        lambda _: next(_status_samples(context, value, marker), None),
        f"未收到检修地址 {point['address']} 值为 {value} 的实际 RTU 读应答", timeout=context.timeout,
    )
    context.desktop.metadata.setdefault("检修遥信与实测观察", []).append({
        "状态": state, "实测": measured, "报文序号": sample[0], "报文时间": sample[1].isoformat(),
    })
    return sample[0]


def _baseline(context):
    marker = len(context.simulator.events()) - 1
    context.sender.send_target_kw(80, timeout=context.timeout)
    check_external_allocation(context, marker, {"A": 40, "B": 40})


def _enter_maintenance(context):
    _set_maintenance(context, True)
    context.maintenance_marker = len(context.simulator.events()) - 1


def _assert_no_a_writes(context, writes, *, before=None):
    actual = [{"序号": entry["index"], "寄存器值": entry["value"], "收到应答": entry["ack"]}
              for entry in writes["A"] if entry["index"] > context.maintenance_marker
              and (before is None or entry["index"] < before)]
    assert not actual, f"成员 A 检修期间仍收到设点写请求：{actual!r}"


def _check_maintenance_target(context, total):
    marker = len(context.simulator.events()) - 1
    context.sender.send_target_kw(total, timeout=context.timeout)
    changed = _expect_writes(context, {"B": total}, after=marker, previous=context.previous)
    _, writes = _protocol(context)
    accepted = next(entry for entry in writes["B"]
                    if entry["index"] > marker and entry["value"] == changed["B"] and entry["ack"])
    deadline = accepted["time"] + timedelta(seconds=WRITE_WINDOW)

    def stayed_quiet(_reads, writes):
        _assert_no_a_writes(context, writes)
        return next((sample for sample in _status_samples(context, 1, accepted["index"])
                     if sample[1] >= deadline), None)

    last = _await(context, stayed_quiet, "成员 A 未持续应答检修点，无法证明检修期间通信正常且未下发",
                  timeout=max(context.timeout, WRITE_WINDOW + 5))
    _, writes = _protocol(context)
    _assert_no_a_writes(context, writes)
    context.desktop.metadata.setdefault("检修期间下发观察", []).append({
        "目标": total, "B 实际收到的设点": changed["B"], "A 最后成功应答": last[1].isoformat(),
        "A 无写入秒数": (last[1] - accepted["time"]).total_seconds(),
    })
    context.previous["B"] = total


def _recover(context):
    cleared = _set_maintenance(context, False)
    _, writes = _protocol(context)
    _assert_no_a_writes(context, writes, before=cleared)
    check_external_allocation(context, cleared, {"A": 30, "B": 30})


ENVIRONMENT = Step("maintenance.environment", "检查 slaveSim 和模拟 IEC104 主站", "专用测试入口和驱动可用")
SIM_START = Step("maintenance.sim.start", "打开本例 slaveSim", "可读写模拟配置及观察 RTU 报文")
SIM_FINISH = Step("maintenance.sim.finish", "关闭本例 slaveSim", "独立浏览器会话已关闭")
LOGIN = (Step("maintenance.account", "读取测试账号", "专用账号配置有效", _load_account), *login_steps("maintenance.login"))
PREPARE = (
    Step("maintenance.rtu", "建立两台模拟成员并给 A 配置 RTU 检修位", "读回实际串口及检修位初值 0", _prepare_rtu),
    Step("maintenance.group", "创建等权控制组并仅为 A 绑定检修状态点", "读回 A 的 BOOL 绑定，B 留空", _prepare_group),
    Step("maintenance.104", "建立专用 IEC104 总目标遥调", "回读遥调点及路由", _prepare_104),
)
CONNECT = Step("maintenance.connect", "连接主站并启动控制组", "控制组运行，主站可遥调", _connect)
CHECKS = (
    Step("maintenance.baseline", "发送 80 总目标，核对正常状态分配", "A、B 均收到实际设点 40", _baseline),
    Step("maintenance.set", "置位 A 的检修遥信", "RTU 采集检修位为 1，A 的实测仍有效", _enter_maintenance),
    Step("maintenance.increase", "上调总目标到 90", "只有 B 收到 90，随后至少 6 秒内 A 应答正常且无写请求",
         lambda context: _check_maintenance_target(context, 90)),
    Step("maintenance.decrease", "下调总目标到 60", "只有 B 收到 60，随后至少 6 秒内 A 应答正常且无写请求",
         lambda context: _check_maintenance_target(context, 60)),
    Step("maintenance.clear", "解除 A 的检修遥信并保持总目标 60", "两台自动恢复分配，均收到实际设点 30", _recover),
)
CAPTURE = Step("maintenance.capture", "保存 IEC104 主站报文", "保留实际遥调及确认报文")


def _maintenance_plan():
    return case_plan(ENVIRONMENT, SLAVESIM_BROWSER_READY, DESKTOP_START, *LOGIN, SITE_CONNECT, SIM_START, *PREPARE,
                     MASTER104_START, CONNECT, *CHECKS, CAPTURE, UPPER_CLEANUP, SIM_CLEANUP, SIM_FINISH,
                     MASTER104_FINISH, DESKTOP_FINISH)


MAINTENANCE_LOGIC = """
1. 建立两台等权成员，只给 A 绑定可选检修状态点。检查 A 实测持续为零，检修点初值为 0。
2. 用 IEC104 下发 80，核对 A、B 均实际收到 40。切换 A 检修位至 1，确认其 RTU 量测仍正常。
3. 依次上调到 90、下调到 60，核对只有 B 接手新目标。每轮以 B 的成功写入为起点，再观察至少 6 秒及 A 的检修位应答。自检修位生效后 A 不得收到任何设点写请求。
4. 将 A 的检修位清零，保留总目标 60，核对 A、B 均重新收到设点 30。保存报文及失败现场并清理。
"""


@pytest.mark.case_group("AGC 控制")
@_maintenance_plan()
@execution_logic(MAINTENANCE_LOGIC)
def test_agc_member_maintenance_excludes_both_directions_until_cleared(request):
    """AGC 成员检修期间双向停发，解除后恢复分配"""
    _run_maintenance(request, "AGC")


@pytest.mark.case_group("AVC 控制")
@_maintenance_plan()
@execution_logic(MAINTENANCE_LOGIC)
def test_avc_member_maintenance_excludes_both_directions_until_cleared(request):
    """AVC 成员检修期间双向停发，解除后恢复分配"""
    _run_maintenance(request, "AVC")


def _run_maintenance(request, module):
    with track_fixture_step(request, ENVIRONMENT):
        url, timeout = _environment()
    with track_fixture_step(request, SLAVESIM_BROWSER_READY):
        browser = request.getfixturevalue("slavesim_browser")
    desktop = request.getfixturevalue("fresh_desktop")
    name = f"e2e-{module.lower()}-maint-{uuid4().hex[:8]}"
    rtu = ExternalDirectionRtuPage(desktop)
    context = SimpleNamespace(
        name=name, module=module, timeout=timeout, desktop=desktop, page=PlatformLoginPage(desktop),
        rtu=rtu, group=MaintenanceAgcPage(desktop) if module == "AGC" else MaintenanceAvcPage(desktop),
        iec104=AgcDirectionIec104(rtu, name), master=None, sim_started=False, previous={},
    )
    run_steps(request, LOGIN, context)
    with track_fixture_step(request, SITE_CONNECT):
        context.host = rtu.connect_site().rsplit(":", 1)[0]
    with track_fixture_context(request, SIM_START, SIM_FINISH, MeasurementExpirySimulator(desktop, url, browser=browser)) as simulator:
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
                        source.capture("member-maintenance-failure", description=f"{module} 检修用例清理前现场")
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
