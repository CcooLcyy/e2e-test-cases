"""通过外部 RTU 遥信验收 AVC 成员单向闭锁。"""

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

from case_support.avc_period_page import GROUP, MEMBER, SUMMARY, AvcPeriodPage
from case_support.case_metadata import execution_logic
from case_support.external_direction_rtu import (
    LOCK_POINTS, ExternalDirectionRtuPage, check_external_allocation, prepare_external_control_rtu,
    set_external_direction,
)
from case_support.login_plan import login_steps
from case_support.measurement_expiry_run import SIM_CLEANUP, UPPER_CLEANUP, _cleanup
from case_support.measurement_expiry_simulator import MeasurementExpirySimulator
from case_support.modbus_rtu_page import xpath_text
from case_support.power_range_run import _environment, _load_account, _save_frames
from case_support.rtu_quality_iec104 import RtuQualityIec104


pytestmark = [pytest.mark.case_group("AVC 控制"), pytest.mark.requires_modbus]

class AvcExternalBlockPage(AvcPeriodPage):
    def __init__(self, desktop):
        super().__init__(desktop)
        self.lock_connection = None

    def _bind_member(self, label, connection, tag):
        super()._bind_member(label, connection, tag)
        if label == "无功设定点" and connection == self.lock_connection:
            for lock_label, lock_tag in LOCK_POINTS:
                super()._bind_member(lock_label, connection, lock_tag)

    def verify_lock_bindings(self):
        self._select_owned()
        self.group_draft_opened = True
        self._click(self._button("编辑", SUMMARY), "打开 AVC 控制组读回闭锁映射")
        for member in ("A", "B"):
            self._click((By.XPATH, GROUP + f"//button[@aria-label='编辑成员 {member}']"), f"读回 {member} 的外部闭锁配置")
            for label, tag in LOCK_POINTS:
                expected = f"ModbusRTU / {self.lock_connection} / {tag}" if member == "A" else ""
                locator = (By.XPATH, MEMBER + "//input[@aria-label=" + xpath_text(label) + "]"
                           "/ancestor::div[contains(concat(' ',normalize-space(@class),' '),' ant-select ')][1]")
                control = self._until(lambda _: self._visible(locator), f"AVC 闭锁遥信未显示：{label}")
                selected = control.find_elements(By.CSS_SELECTOR, ".ant-select-content-has-value, .ant-select-selection-item")
                value = next((item.text.strip() for item in selected if item.is_displayed()), "")
                self.desktop.check(f"核对 {member} 已保存的{label}", value, expected)
            self._discard_draft(MEMBER, "取消编辑")
        self._discard_draft(GROUP, "取消")
        self.group_draft_opened = False


def _prepare_group(context):
    a, b, voltage = (context.connections[role] for role in ("A", "B", "V"))
    context.group.lock_connection = a
    context.group.create_group(
        context.name, mode="q_total", calculation_period=1, command_period=4, voltage_tag="measured",
        target_tag=context.target_tag, rtu_connection=a, q_measurement_tag="measured", q_setpoint_tag="setpoint",
        member_name="A", q_min=10, q_max=100, voltage_connection=voltage,
        additional_members=(("B", b, "measured", "setpoint", 10, 100),),
    )
    context.desktop.check("核对 AVC 已保存的分配方式", context.group._read_summary("分配方式"), "平均分配（等权）")
    context.group.verify_lock_bindings()


def _prepare_104(context):
    page = context.iec104
    page.create()
    context.ioa = page.import_point(context.name, context.target_tag, f"{context.name}-104-target",
                                    module="AVC", business_type="遥调")
    page.check_routes()
    page.start()
    context.desktop.metadata["AVC 外部闭锁用例遥调 IOA"] = context.ioa


def _connect(context):
    context.master.prepare_target(context.host, context.iec104.port, 1)
    context.master.connect()
    context.sender = Master104CommandSender(context.master, 1, context.ioa)
    context.group.start()


def _phase(identifier, action, expected, *, target=None, signal=None):
    feedback = "，".join(f"{role} 设定 {value} kvar" for role, value in expected.items())

    def run(context):
        marker = len(context.simulator.events()) - 1
        if signal is not None:
            set_external_direction(context, *signal)
        result = context.sender.send_target_kw(target, timeout=context.timeout) if target is not None else None
        observed = check_external_allocation(context, marker, expected)
        context.desktop.metadata.setdefault("AVC 外部闭锁观察", []).append(
            {"目标 kvar": target, "外部信号": signal, "预期设定": expected, "实际写入": observed, "遥调结果": result},
        )

    return Step(f"avc.external-block.{identifier}", action, feedback, run)


ENVIRONMENT = Step("avc.external-block.env", "检查 slaveSim 和独立 IEC104 主站入口", "模拟器、主站和驱动有效")
SIM_START = Step("avc.external-block.sim.start", "打开本例 slaveSim", "可读取配置和协议日志")
SIM_FINISH = Step("avc.external-block.sim.finish", "关闭本例 slaveSim", "独立浏览器会话已关闭")
LOGIN = (Step("avc.external-block.account", "读取测试账号", "专用账号配置有效", _load_account),
         *login_steps("avc.external-block.login"))
PREPARE = (
    Step("avc.external-block.rtu", "建立无功设点、电压量测和 A 的两路 RTU 遥信",
         "A/B 的量测、写入点和 A 的两路 BOOL 遥信就绪",
         lambda context: prepare_external_control_rtu(context, {"A": (20, 0), "B": (20, 0), "V": (220,)})),
    Step("avc.external-block.group", "创建 AVC 组并配置 A 的两路可选外部闭锁输入",
         "两路遥信均能绑定并读回，B 无需配置闭锁输入", _prepare_group),
    Step("avc.external-block.104", "建立专用 IEC104 总无功遥调", "遥调点和业务路由已保存", _prepare_104),
)
CONNECT = Step("avc.external-block.connect", "连接主站并启动 AVC 控制组", "主站连接且控制组运行", _connect)
PHASES = (
    _phase("baseline", "遥调 80 kvar，建立正常分配基线", {"A": 40, "B": 40}, target=80),
    _phase("increase", "禁止 A 上调，遥调 100 kvar，由未闭锁的 B 接手增量",
           {"A": 40, "B": 60}, target=100, signal=("increase", True)),
    _phase("increase-reverse", "仍禁止 A 上调，遥调 60 kvar，核对允许 A 下调",
           {"A": 30, "B": 30}, target=60),
    _phase("increase-hold", "仍禁止 A 上调，遥调 80 kvar，核对 A 保持新的设定",
           {"A": 30, "B": 50}, target=80),
    _phase("increase-release", "解除 A 增闭锁并保持原总目标", {"A": 40, "B": 40}, signal=("increase", False)),
    _phase("decrease", "禁止 A 下调，遥调 60 kvar，由未闭锁的 B 接手减量",
           {"A": 40, "B": 20}, target=60, signal=("decrease", True)),
    _phase("decrease-reverse", "仍禁止 A 下调，遥调 100 kvar，核对允许 A 上调",
           {"A": 50, "B": 50}, target=100),
    _phase("decrease-hold", "仍禁止 A 下调，遥调 80 kvar，核对 A 保持新的设定",
           {"A": 50, "B": 30}, target=80),
    _phase("decrease-release", "解除 A 减闭锁并保持原总目标", {"A": 40, "B": 40}, signal=("decrease", False)),
)
CAPTURE = Step("avc.external-block.capture", "保存模拟主站原始报文", "已保留本例 IEC104 收发报文")


@case_plan(ENVIRONMENT, SLAVESIM_BROWSER_READY, DESKTOP_START, *LOGIN, SITE_CONNECT, SIM_START, *PREPARE,
           MASTER104_START, CONNECT, *PHASES, CAPTURE, UPPER_CLEANUP, SIM_CLEANUP, SIM_FINISH,
           MASTER104_FINISH, DESKTOP_FINISH)
@execution_logic("""
1. 创建两台等权 AVC 成员，各自可调范围为 10～100 kvar。仅给 A 绑定两路可选 Modbus RTU 闭锁遥信并读回。
2. IEC104 遥调 80 kvar 建立 40/40 kvar 基线。禁止 A 上调后，验证 B 接手增量、A 仍能下调。
3. 用仍被闭锁的目标验证解除后无需重发命令，AVC 自动恢复等权分配。
4. 禁止 A 下调，验证 B 接手减量、A 仍能上调。保持最近一次被闭锁的目标并解除闭锁，核对自动恢复等权分配。
5. 每轮核对 RTU 写请求、应答和设定寄存器。失败时保留现场截图和报文，再清理本例资源。
""")
def test_avc_external_member_direction_block_redistributes_and_recovers(request):
    """AVC 外部遥信只闭锁指定成员及方向，解除后恢复分配"""
    with track_fixture_step(request, ENVIRONMENT):
        url, timeout = _environment()
    with track_fixture_step(request, SLAVESIM_BROWSER_READY):
        browser = request.getfixturevalue("slavesim_browser")
    desktop = request.getfixturevalue("fresh_desktop")
    name = f"e2e-avc-ex-{uuid4().hex[:8]}"
    rtu = ExternalDirectionRtuPage(desktop)
    context = SimpleNamespace(
        name=name, module="AVC", timeout=timeout, desktop=desktop, page=PlatformLoginPage(desktop),
        rtu=rtu, group=AvcExternalBlockPage(desktop), iec104=RtuQualityIec104(rtu, name),
        target_tag=f"{name}-target", master=None, sim_started=False, previous={},
    )
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
            run_steps(request, (CONNECT, *PHASES), context)
            with track_fixture_step(request, CAPTURE):
                _save_frames(context.master)
        except BaseException as error:
            failure = error
            for source in (desktop, simulator, context.master):
                if source is not None:
                    try:
                        source.capture("avc-external-block-failure", description="AVC 外部闭锁清理前现场")
                    except Exception as evidence_error:
                        error.add_note(f"截图失败：{evidence_error}")
            if context.master is not None:
                try:
                    _save_frames(context.master)
                except Exception as evidence_error:
                    error.add_note(f"主站报文取证失败：{evidence_error}")
            raise
        finally:
            _cleanup(context, request, failure)
