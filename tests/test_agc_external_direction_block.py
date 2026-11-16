"""通过 RTU 遥信、IEC104 遥调及成员写报文验证 AGC 外部方向闭锁。"""

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
from case_support.external_direction_rtu import (
    LOCK_POINTS, ExternalDirectionRtuPage, check_external_allocation, prepare_external_control_rtu,
    set_external_direction,
)
from case_support.login_plan import login_steps
from case_support.measurement_expiry_agc import MeasurementExpiryAgc
from case_support.measurement_expiry_run import SIM_CLEANUP, UPPER_CLEANUP, _cleanup
from case_support.measurement_expiry_simulator import MeasurementExpirySimulator
from case_support.modbus_rtu_page import xpath_text
from case_support.power_range_run import _environment, _load_account, _save_frames


pytestmark = [pytest.mark.case_group("AGC 控制"), pytest.mark.requires_modbus]

class ExternalBlockAgcPage(MeasurementExpiryAgc):
    def _selected_binding(self, label):
        field = self.MEMBER_DIALOG + "//input[@aria-label=" + xpath_text(label) + "]"
        control = (By.XPATH, field + "/ancestor::div[contains(concat(' ',normalize-space(@class),' '),' ant-select ')][1]")
        select = self._until(lambda _: self._visible(control), f"AGC 成员编辑未显示 {label} 绑定入口")
        values = select.find_elements(By.CSS_SELECTOR, ".ant-select-content-has-value, .ant-select-selection-item")
        return next((value.text.strip() for value in values if value.is_displayed()), "")

    def _open_member_for_external_block(self, name):
        edit = (By.XPATH, self.GROUP_DIALOG + "//button[@aria-label=" + xpath_text(f"编辑成员 {name}") + "]")
        self._click(edit, f"打开 AGC 成员 {name} 的独立编辑窗口")
        self._open_member_advanced()

    def _open_group_for_external_block(self):
        self._select_owned_group()
        self._click(self._button("编辑", self.OVERVIEW), "编辑本例 AGC 控制组")
        self._draft_opened = True
        self._members_tab()

    def configure_external_blocks(self):
        self._open_group_for_external_block()
        self._open_member_for_external_block("A")
        connection_name = self.members["A"]
        source = f"ModbusRTU / {connection_name}"
        for label, tag in LOCK_POINTS:
            self._selected_binding(label)
            self._choose(self.MEMBER_DIALOG, f"{label}连接", source, search=connection_name, accessible_name=True)
            self._choose(self.MEMBER_DIALOG, label, f"{source} / {tag}", search=tag, accessible_name=True)
        self._click(self._button("确认成员", self.MEMBER_DIALOG), "确认成员 A 的增减闭锁输入")
        self._wait_dialog_closed(self.MEMBER_DIALOG, "确认成员")
        self._submit_group("保存 AGC 控制组的外部遥信路由")

        self._open_group_for_external_block()
        for name in ("A", "B"):
            self._open_member_for_external_block(name)
            for label, tag in LOCK_POINTS:
                expected = f"{source} / {tag}" if name == "A" else ""
                self.desktop.check(f"核对 AGC 成员 {name} 保存的{label}", self._selected_binding(label), expected)
            self._close_draft(self.MEMBER_DIALOG, "取消编辑")
        self._close_draft(self.GROUP_DIALOG, "取消")
        self._draft_opened = False


def _prepare_group(context):
    context.target_tag = context.group.create_communication_group(context.name, context.connections)["command_tag"]


def _prepare_104(context):
    page = context.iec104
    page.create()
    tag = f"{context.name}-target"
    page._import_agc_point(f"AGC/{context.name}", context.target_tag, tag, page.FLOAT, "遥调")
    context.ioa = page._read_point(tag, page.FLOAT, "遥调")
    page.check_routes()
    page.start()


def _connect(context):
    context.master.prepare_target(context.host, context.iec104.port, 1)
    context.master.connect()
    context.sender = Master104CommandSender(context.master, 1, context.ioa)
    context.group.start()
    context.group._until(
        lambda _: all(context.group.read_member_communication(name)["condition"] == "通信正常" for name in ("A", "B")),
        "AGC 两成员未全部通信正常",
        timeout=context.timeout,
    )


def _verify_allocation(context, after, expected):
    actual = check_external_allocation(context, after, expected)
    context.desktop.metadata.setdefault("AGC 外部闭锁分配", []).append({"预期 kW": expected, "应答 kW": actual})


def _target(identifier, label, total, a, b):
    def run(context):
        after = len(context.simulator.events()) - 1
        context.sender.send_target_kw(total, timeout=context.timeout)
        _verify_allocation(context, after, {"A": a, "B": b})

    return Step(f"agc.external.{identifier}", label, f"实际 RTU 设定为 A={a} kW、B={b} kW", run)


def _enable(direction, label):
    return Step(f"agc.external.enable-{direction}", f"置位{label}", "DSP 的 RTU 轮询实收该方向的闭锁位",
                lambda context: set_external_direction(context, direction, True))


def _release(direction, label, each):
    def run(context):
        after = len(context.simulator.events()) - 1
        set_external_direction(context, direction, False)
        _verify_allocation(context, after, {"A": each, "B": each})

    return Step(f"agc.external.release-{direction}", f"解除{label}，保持总目标",
                f"同一总目标自动恢复等分，各写入 {each} kW", run)


ENVIRONMENT = Step("agc.external.environment", "检查 slaveSim 和模拟 IEC104 主站", "专用测试入口和驱动可用")
SIM_START = Step("agc.external.sim.start", "打开本例 slaveSim 会话", "能配置模拟总线并观察报文")
SIM_FINISH = Step("agc.external.sim.finish", "关闭本例 slaveSim 会话", "独立浏览器会话已关闭")
LOGIN = (
    Step("agc.external.account", "读取测试账号", "专用账号配置有效", _load_account),
    *login_steps("agc.external.login"),
)
PREPARE = (
    Step("agc.external.rtu", "搭建两个 RTU 成员和 A 的两路外部闭锁遥信", "已回读 BOOL 点表、实际串口和初始量测",
         lambda context: prepare_external_control_rtu(context, {"A": (40, 0), "B": (40, 0)})),
    Step("agc.external.group", "保存包含 A 和 B 的等权 AGC 控制组", "组和两个成员的业务路由已读回", _prepare_group),
    Step("agc.external.bind", "仅绑定成员 A 的可选增减闭锁遥信", "A 的两路遥信保存并读回，B 保持未绑定",
         lambda context: context.group.configure_external_blocks()),
    Step("agc.external.iec104", "建立并启动 IEC104 遥调连接", "回读专用总有功遥调点和路由", _prepare_104),
)
CONNECT = Step("agc.external.connect", "连接主站并启动 AGC 组", "连接建立且两成员均通信正常", _connect)
CHECKS = (
    _target("baseline", "遥调 80 kW，核对闭锁位未置位时两台等分", 80, 40, 40),
    _enable("increase", "A 的禁止增出力遥信"),
    _target("increase-blocked", "遥调 120 kW，验证 B 接手全部增量", 120, 40, 80),
    _target("decrease-allowed", "在增闭锁仍有效时遥调 60 kW，验证 A 可以下调", 60, 30, 30),
    _target("increase-before-release", "遥调 100 kW，保持 A 增闭锁", 100, 30, 70),
    _release("increase", "A 的禁止增出力遥信", 50),
    _enable("decrease", "A 的禁止减出力遥信"),
    _target("decrease-blocked", "遥调 80 kW，验证 B 接手全部减量", 80, 50, 30),
    _target("increase-allowed", "在减闭锁仍有效时遥调 120 kW，验证 A 可以上调", 120, 60, 60),
    _target("decrease-before-release", "遥调 100 kW，保持 A 减闭锁", 100, 60, 40),
    _release("decrease", "A 的禁止减出力遥信", 50),
)
CAPTURE = Step("agc.external.capture", "保存 IEC104 主站报文", "已留存遥调和协议确认")


@case_plan(ENVIRONMENT, SLAVESIM_BROWSER_READY, DESKTOP_START, *LOGIN, SITE_CONNECT, SIM_START, *PREPARE,
           MASTER104_START, CONNECT, *CHECKS, CAPTURE, UPPER_CLEANUP, SIM_CLEANUP, SIM_FINISH,
           MASTER104_FINISH, DESKTOP_FINISH)
@execution_logic("""
1. 使用 slaveSim 和 DSP 上位机建立两个可控成员，A 映射两路可选闭锁遥信，B 无闭锁绑定。两个成员范围均为 10～100 kW。
2. 通过 IEC104 遥调建立 40/40 kW 基线。置位 A 的外部增闭锁，B 再分配上调目标，A 仍能下调；解除后不重送总目标，恢复等权分配。
3. 置位 A 的外部减闭锁，B 再分配下调目标，A 仍能上调；解除后不重送总目标，恢复等权分配。
4. 每次置位或解除核对模拟寄存器和 DSP 采集值。每轮核对 RTU 写请求、应答和寄存器。保存报文及失败现场，再清理本例资源。
""")
def test_agc_optional_external_direction_block_redistributes_members(request):
    """AGC 外部成员增减闭锁与剩余分配"""
    with track_fixture_step(request, ENVIRONMENT):
        url, timeout = _environment()
    with track_fixture_step(request, SLAVESIM_BROWSER_READY):
        browser = request.getfixturevalue("slavesim_browser")
    desktop = request.getfixturevalue("fresh_desktop")
    name = f"e2e-pblock-{uuid4().hex[:8]}"
    rtu = ExternalDirectionRtuPage(desktop)
    context = SimpleNamespace(
        name=name, module="AGC", timeout=timeout, desktop=desktop, page=PlatformLoginPage(desktop),
        rtu=rtu, group=ExternalBlockAgcPage(desktop), iec104=AgcDirectionIec104(rtu, name),
        master=None, sim_started=False, previous={},
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
                        source.capture("agc-external-block-failure", description="AGC 外部闭锁清理前现场")
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
