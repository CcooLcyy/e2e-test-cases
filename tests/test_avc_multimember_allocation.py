"""通过真实 RTU 写入和 IEC104 遥测验收 AVC 多成员分配。"""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from testbench.case_plan import (
    DESKTOP_FINISH, DESKTOP_START, MASTER104_FINISH, MASTER104_START, SITE_CONNECT,
    SLAVESIM_BROWSER_READY, Step, case_plan, run_steps, track_fixture_context, track_fixture_step,
)
from testbench.login_page import PlatformLoginPage
from testbench.master104_commands import Master104CommandSender

from case_support.avc_period_page import AvcPeriodPage
from case_support.case_metadata import execution_logic
from case_support.login_plan import login_steps
from case_support.measurement_expiry_run import SIM_CLEANUP, UPPER_CLEANUP, _cleanup, _expect_writes, prepare_control_rtu
from case_support.measurement_expiry_simulator import MeasurementExpirySimulator
from case_support.modbus_rtu_page import ModbusRtuPage
from case_support.power_range_page import PowerRangePage
from case_support.power_range_run import (
    _environment, _load_account, _save_frames, master_measurement_baseline, verify_power_telemetry,
)
from case_support.rtu_quality_iec104 import RtuQualityIec104


pytestmark = [pytest.mark.case_group("AVC 控制"), pytest.mark.requires_modbus]
LOWER_TAG = "当前可调无功下限"
UPPER_TAG = "当前可调无功上限"


def _prepare_rtu(context):
    specs = {"A": (10, 0), "B": (10, 0), "V": (220,)}
    if not context.proportional:
        specs["P"] = (20,)
    prepare_control_rtu(context, specs)


def _prepare_group(context):
    connections = context.connections
    upper_a, upper_b = (40, 80) if context.proportional else (30, 100)
    additional = [("B", connections["B"], "measured", "setpoint", 0, upper_b)]
    if not context.proportional:
        additional.append(("P", connections["P"], "measured", None, 0, 0))
    context.group.create_group(
        context.name, mode="q_total", calculation_period=1, command_period=4, voltage_tag="measured",
        target_tag=context.target_tag, rtu_connection=connections["A"], q_measurement_tag="measured",
        q_setpoint_tag="setpoint", member_name="A", q_max=upper_a, voltage_connection=connections["V"],
        additional_members=additional, proportional=context.proportional,
    )
    context.power_view.conn_id = context.group.conn_id
    allocation = "按可调范围比例" if context.proportional else "平均分配（等权）"
    context.desktop.check("核对 AVC 已保存的分配方式", context.group._read_summary("分配方式"), allocation)


def _prepare_104(context):
    page = context.iec104
    page.create()
    context.ioa = page.import_point(context.name, context.target_tag, f"{context.name}-104-target",
                                    module="AVC", business_type="遥调")
    if not context.proportional:
        for tag in (LOWER_TAG, UPPER_TAG):
            context.ioas[tag] = page.import_point(context.name, tag, f"{context.name}-104-{tag}", module="AVC")
    assert len({context.ioa, *context.ioas.values()}) == 1 + len(context.ioas), "本例 AVC 的 IEC104 IOA 重复"
    page.check_routes()
    page.start()
    context.desktop.metadata["本例 AVC IEC104 点表"] = {"遥调": context.ioa, "遥测": context.ioas}


def _connect(context):
    context.master.prepare_target(context.host, context.iec104.port, 1)
    context.master.connect()
    context.group.start()


def _setpoints(context, expected, marker, *, previous=None):
    actual = {}
    with context.desktop.step("核对 AVC 成员的 RTU 写请求和应答", expected=expected, actual=actual):
        actual.update(_expect_writes(context, expected, after=marker, previous=previous))
    for role, value in expected.items():
        point = context.points[role][1]
        sample = context.simulator.observe(point["bus"], point["station_id"], point["function"], point["address"],
                                           verify_manual=False)
        context.desktop.check(f"核对 AVC 成员 {role} 的设定寄存器", sample["value"], value)


def _target(context, value, expected):
    marker = len(context.simulator.events()) - 1
    sender = Master104CommandSender(context.master, 1, context.ioa)
    context.master.metadata["本例 AVC 遥调"] = sender.send_target_kw(value, timeout=context.timeout)
    _setpoints(context, expected, marker)


def _range(context, lower, upper, baseline):
    expected = {LOWER_TAG: lower, UPPER_TAG: upper}
    station = {"理论可调无功下限": 0, "理论可调无功上限": 130, **expected}
    context.power_view.wait_station_values(station, context.timeout)
    verify_power_telemetry(context, expected, baseline)


def _saturate(context):
    baseline = master_measurement_baseline(context.master)
    _range(context, 20, 150, baseline)
    _target(context, 100, {"A": 30, "B": 50})


def _change_passive_output(context, measured, expected_b, previous_b):
    baseline = master_measurement_baseline(context.master)
    marker = len(context.simulator.events()) - 1
    point = context.points["P"][0]
    context.simulator.set_value(point["bus"], point["station_id"], point["function"], point["address"], measured)
    context.rtu.open_current_links()
    context.rtu.select(context.connections["P"])
    context.rtu.wait_value("measured", measured, 0, context.timeout)
    _setpoints(context, {"B": expected_b}, marker, previous={"B": previous_b})
    point_a = context.points["A"][1]
    state = context.simulator.observe(point_a["bus"], point_a["station_id"], point_a["function"],
                                      point_a["address"], verify_manual=False)
    context.desktop.check("核对到限成员仍为 30 kvar", state["value"], 30)
    _range(context, measured, measured + 130, baseline)


ENVIRONMENT = Step("avc.members.env", "检查 slaveSim 和独立 IEC104 主站入口", "模拟器、主站和驱动可用")
SIM_START = Step("avc.members.sim", "打开本例 slaveSim", "模拟器配置和报文日志可用")
SIM_FINISH = Step("avc.members.sim.close", "关闭本例 slaveSim 会话", "浏览器已关闭")
LOGIN = (Step("avc.members.account", "读取专用测试账号", "账号配置有效", _load_account),
         *login_steps("avc.members.login"))
PREPARE = (
    Step("avc.members.rtu", "建立本例 RTU 采集及写入点", "串口、站号和寄存器与模拟器一致", _prepare_rtu),
    Step("avc.members.group", "创建 AVC 多成员控制组", "成员点位和分配方式已保存", _prepare_group),
    Step("avc.members.104", "建立本例 IEC104 业务点及路由", "回读专用 IOA 和路由", _prepare_104),
)
CONNECT = Step("avc.members.connect", "连接 IEC104 主站并启动控制组", "主站会话和控制组均运行", _connect)
CAPTURE = Step("avc.members.capture", "保存主站原始报文", "本例 IEC104 报文已留存")
PROPORTIONAL = (
    Step("avc.members.proportion", "遥调总无功 60 kvar", "可调范围为 0～40、0～80 kvar 的两台实收 20、40 kvar",
         lambda context: _target(context, 60, {"A": 20, "B": 40})),
)
SATURATION = (
    Step("avc.members.saturation", "核对初始范围并遥调 100 kvar",
         "当前范围 20～150 kvar，A 到 30 kvar 上限，B 实收 50 kvar", _saturate),
    Step("avc.members.passive", "将仅测量成员实测由 20 调为 40 kvar",
         "保持总目标，A 保持 30 kvar，B 实收 30 kvar，当前范围和远传更新至 40～170 kvar",
         lambda context: _change_passive_output(context, 40, 30, 50)),
    Step("avc.members.passive.restore", "将仅测量成员实测恢复为 20 kvar",
         "无需重发总目标，A 保持 30 kvar，B 实收 50 kvar，当前范围和远传恢复为 20～150 kvar",
         lambda context: _change_passive_output(context, 20, 50, 30)),
)


def _plan(checks):
    return case_plan(ENVIRONMENT, SLAVESIM_BROWSER_READY, DESKTOP_START, *LOGIN, SITE_CONNECT, SIM_START, *PREPARE,
                     MASTER104_START, CONNECT, *checks, CAPTURE, UPPER_CLEANUP, SIM_CLEANUP, SIM_FINISH,
                     MASTER104_FINISH, DESKTOP_FINISH)


@_plan(PROPORTIONAL)
@execution_logic("""
1. 建立两台可控成员和独立电压量测，配置无功范围 0～40 与 0～80 kvar。
2. 保存并核对「按可调范围比例」分配，建立 IEC104 总无功遥调。
3. 遥调 60 kvar，核对两台 RTU 实际写入、应答及设定寄存器分别为 20 与 40 kvar。
""")
def test_avc_distributes_by_configured_reactive_range(request):
    """多成员按无功可调范围比例分配"""
    _run(request, PROPORTIONAL, proportional=True)


@_plan(SATURATION)
@execution_logic("""
1. 建立两个等权可控成员，范围 0～30、0～100 kvar，以及一台仅参与测量的成员。
2. 仅测量成员实测 20 kvar。核对理论范围 0～130 kvar、当前范围 20～150 kvar 及 IEC104 遥测。
3. 遥调总无功 100 kvar。核对 A 到限 30 kvar，余量由 B 承担，B 实收 50 kvar。
4. 将仅测量成员实测改为 40 kvar。保持总目标，核对 A 为 30 kvar、B 改为 30 kvar、当前范围更新至 40～170 kvar。
5. 将该实测恢复为 20 kvar，无需重发遥调，核对 B 及当前范围和远传恢复。
""")
def test_avc_redistributes_at_limit_as_passive_output_changes(request):
    """成员到限和仅测量成员出力变化时重分配"""
    _run(request, SATURATION, proportional=False)


def _run(request, checks, *, proportional):
    with track_fixture_step(request, ENVIRONMENT):
        url, timeout = _environment()
    with track_fixture_step(request, SLAVESIM_BROWSER_READY):
        browser = request.getfixturevalue("slavesim_browser")
    desktop = request.getfixturevalue("fresh_desktop")
    name = f"e2e-avc-m-{uuid4().hex[:8]}"
    rtu = ModbusRtuPage(desktop)
    context = SimpleNamespace(
        name=name, module="AVC", proportional=proportional, timeout=timeout, desktop=desktop,
        page=PlatformLoginPage(desktop), rtu=rtu, sim_started=False, group=AvcPeriodPage(desktop),
        power_view=PowerRangePage(desktop, "AVC", name), target_tag=f"{name}-target",
        iec104=RtuQualityIec104(rtu, name), ioas={}, previous_remote=None, master=None,
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
            run_steps(request, (CONNECT, *checks), context)
            with track_fixture_step(request, CAPTURE):
                _save_frames(context.master)
        except BaseException as error:
            failure = error
            for source in (desktop, simulator, context.master):
                if source is not None:
                    try:
                        source.capture("avc-members-failure", description="AVC 多成员分配清理前现场")
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
