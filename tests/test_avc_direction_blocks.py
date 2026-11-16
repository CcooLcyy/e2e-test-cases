"""验收 AVC 全站无功增减闭锁和 IEC104 遥信。"""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from testbench.case_plan import (
    DESKTOP_FINISH, DESKTOP_START, MASTER104_FINISH, MASTER104_START, SITE_CONNECT,
    SLAVESIM_BROWSER_READY, Step, case_plan, run_steps, track_fixture_context, track_fixture_step,
)
from testbench.login_page import PlatformLoginPage
from testbench.master104_commands import Master104CommandSender
from testbench.master104_signals import Master104PointObserver

from case_support.agc_direction_iec104 import AgcDirectionIec104
from case_support.avc_period_page import AvcPeriodPage
from case_support.case_metadata import execution_logic
from case_support.login_plan import login_steps
from case_support.measurement_expiry_run import (
    SIM_CLEANUP, UPPER_CLEANUP, _cleanup, _expect_writes, _protocol, prepare_control_rtu,
)
from case_support.measurement_expiry_simulator import MeasurementExpirySimulator
from case_support.modbus_rtu_page import ModbusRtuPage
from case_support.power_range_run import _environment, _load_account, _save_frames


pytestmark = [pytest.mark.case_group("AVC 控制"), pytest.mark.requires_modbus]
BLOCK_TAGS = {"increase": "AVC增闭锁", "decrease": "AVC减闭锁"}


def _prepare_group(context):
    connections = context.connections
    context.group.create_group(
        context.name, mode="q_total", calculation_period=1, command_period=4,
        voltage_tag="measured", voltage_connection=connections["V"], target_tag=context.target_tag,
        rtu_connection=connections["A"], q_measurement_tag="measured", q_setpoint_tag="setpoint",
        member_name="A", q_min=10, q_max=30,
        additional_members=(("B", connections["B"], "measured", "setpoint", 10, 100),),
    )
    context.desktop.check("核对 AVC 已保存的分配方式", context.group._read_summary("分配方式"), "平均分配（等权）")


def _prepare_104(context):
    page = context.iec104
    page.create()
    source = f"AVC/{context.name}"
    points = [("target", context.target_tag, page.FLOAT, "遥调")]
    points.extend((name, tag, page.SINGLE, "遥信") for name, tag in BLOCK_TAGS.items())
    saved = {}
    for name, source_tag, point_type, business_type in points:
        destination = f"{context.name}-104-{name}"
        page._import_agc_point(source, source_tag, destination, point_type, business_type)
        saved[name] = page._read_point(destination, point_type, business_type)
    assert len(set(saved.values())) == len(saved), f"本例 AVC IEC104 点号重复：{saved!r}"
    page.check_routes()
    page.start()
    context.target_ioa = saved.pop("target")
    context.signal_ioas = saved
    context.desktop.metadata["本例 AVC IEC104 点表"] = {
        "总无功遥调": context.target_ioa, "闭锁遥信": context.signal_ioas, "路由": list(page.routes),
    }


def _connect(context):
    context.master.prepare_target(context.host, context.iec104.port, 1)
    context.master.connect()
    context.sender = Master104CommandSender(context.master, 1, context.target_ioa)
    context.observer = Master104PointObserver(context.master, context.signal_ioas, 1)
    context.group.start()


def _phase(name, action, target, a, b, *, increase=False, decrease=False):
    expected = {"A": a, "B": b}
    blocks = {"increase": increase, "decrease": decrease}

    def run(context):
        baseline = context.observer.checkpoint() if context.previous else None
        marker = len(context.simulator.events()) - 1
        result = context.sender.send_target_kw(target, timeout=context.timeout)
        record = {"总无功目标 kvar": target, "成员目标 kvar": expected, "闭锁预期": blocks, "遥调": result}
        context.desktop.metadata.setdefault("AVC 闭锁观察", []).append(record)
        changed = {role: value for role, value in expected.items() if value != context.previous.get(role)}
        record["本轮 RTU 写入及应答"] = _expect_writes(context, changed, after=marker, previous=context.previous)
        for role, value in expected.items():
            point = context.points[role][1]
            actual = context.simulator.observe(point["bus"], point["station_id"], point["function"], point["address"],
                                               verify_manual=False)
            assert (actual["actual_device"], actual["link_path"]) == (point["actual_device"], point["link_path"]), (
                f"成员 {role} 的模拟串口身份变化：{actual!r}")
            context.desktop.check(f"核对 {role} 的无功设定寄存器", actual["value"], value)

        if baseline is not None:
            changed_keys = tuple(key for key, value in blocks.items() if baseline["points"][key]["value"] != value)
            if changed_keys:
                baseline["wall_time"] = result["sent_at"]
                record["本轮遥信变位"] = context.observer.wait_state(
                    blocks, baseline, context.timeout, fresh_keys=changed_keys, sent_frame_id=result["sent_frame_id"],
                )
        context.master.interrogate()
        observed = context.observer.checkpoint()
        record["本轮总召遥信"] = observed
        context.master.check("核对本轮有效闭锁遥信",
                             {key: point["value"] for key, point in observed["points"].items()}, blocks)
        assert all(point["frame_id"] > result["sent_frame_id"] for point in observed["points"].values()), (
            f"闭锁遥信缺少本轮遥调后的接收证据：{observed!r}")
        _, writes = _protocol(context)
        for role, value in expected.items():
            allowed = {value, context.previous.get(role)}
            incorrect = [entry for entry in writes[role] if entry["index"] > marker and entry["value"] not in allowed]
            context.desktop.check(f"核对 {role} 未收到本轮错误设点", incorrect, [])
        context.previous = expected

    return Step(f"avc.direction.{name}", action,
                f"实际 RTU A/B={a}/{b} kvar，IEC104 增闭锁={int(increase)}、减闭锁={int(decrease)}，品质有效", run)


ENVIRONMENT = Step("avc.direction.environment", "检查 slaveSim 和 IEC104 模拟主站", "模拟入口和驱动可用")
SIM_START = Step("avc.direction.sim.start", "打开本例 slaveSim", "可读取配置和协议报文")
SIM_FINISH = Step("avc.direction.sim.finish", "关闭本例 slaveSim", "独立浏览器会话已关闭")
LOGIN = (Step("avc.direction.account", "读取测试账号", "专用账号配置有效", _load_account),
         *login_steps("avc.direction.login"))
PREPARE = (
    Step("avc.direction.rtu", "建立两台可控成员和独立电压量测的 RTU 连接", "串口和寄存器与模拟器一致",
         lambda context: prepare_control_rtu(context, {"A": (10, 0), "B": (10, 0), "V": (220,)})),
    Step("avc.direction.group", "建立总无功等权分配组", "两台范围为 10～30 和 10～100 kvar", _prepare_group),
    Step("avc.direction.104", "从 AVC 默认点导入 IEC104 增减闭锁遥信并配置遥调",
         "可选中 AVC增闭锁、AVC减闭锁，保存单点遥信及路由并读回 IOA", _prepare_104),
)
CONNECT = Step("avc.direction.connect", "连接 IEC104 主站并启动 AVC 控制组", "主站会话和控制组均运行", _connect)
PHASES = (
    _phase("initial", "遥调总无功 50 kvar，建立两台均可增减的基线", 50, 25, 25),
    _phase("partial-upper", "上调至 90 kvar，核对单台到上限、余量由另一台承担", 90, 30, 60),
    _phase("all-upper", "上调至 130 kvar，核对全组到上限才置增闭锁", 130, 30, 100, increase=True),
    _phase("partial-lower", "下调至 60 kvar，核对增闭锁解除且单台到下限时不置减闭锁", 60, 10, 50),
    _phase("all-lower", "下调至 20 kvar，核对全组到下限才置减闭锁", 20, 10, 10, decrease=True),
    _phase("reverse", "反向上调至 50 kvar，核对减闭锁解除", 50, 25, 25),
)
CAPTURE = Step("avc.direction.capture", "保存模拟主站原始报文", "本例 IEC104 报文已留存")


@case_plan(ENVIRONMENT, SLAVESIM_BROWSER_READY, DESKTOP_START, *LOGIN, SITE_CONNECT, SIM_START, *PREPARE,
           MASTER104_START, CONNECT, *PHASES, CAPTURE, UPPER_CLEANUP, SIM_CLEANUP, SIM_FINISH,
           MASTER104_FINISH, DESKTOP_FINISH)
@execution_logic("""
1. 建立两个等权可控成员，范围为 10～30 和 10～100 kvar，保持两台无功实测均为 10 kvar。
2. 通过正常点表把 AVC增闭锁、AVC减闭锁导入 IEC104 单点遥信，读回 IOA 和数据总线路由。
3. 遥调 50、90、130 kvar，成员依次收到 25/25、30/60、30/100 kvar。只有全组已下发设点到上限才置增闭锁。
4. 从 130 下调至 60 kvar，等权各先分配 35 kvar 减量，A 只能减 20 kvar，其余 15 kvar 交给 B。
   核对成员收到 10/50 kvar，两方向均未闭锁。再下调至 20 kvar 才置减闭锁，反向上调到 50 kvar 后解除。
5. 每次核对实际写请求及应答、模拟寄存器、IEC104 遥信值和品质。失败时保留现场，再清理专用资源。
""")
def test_avc_reports_direction_blocks_when_all_members_reach_limits(request):
    """AVC 单成员到限后重新分配与全站增减闭锁"""
    with track_fixture_step(request, ENVIRONMENT):
        url, timeout = _environment()
    with track_fixture_step(request, SLAVESIM_BROWSER_READY):
        browser = request.getfixturevalue("slavesim_browser")
    desktop = request.getfixturevalue("fresh_desktop")
    name = f"e2e-avc-block-{uuid4().hex[:8]}"
    rtu = ModbusRtuPage(desktop)
    context = SimpleNamespace(
        name=name, module="AVC", timeout=timeout, desktop=desktop, page=PlatformLoginPage(desktop),
        rtu=rtu, group=AvcPeriodPage(desktop), iec104=AgcDirectionIec104(rtu, name),
        target_tag=f"{name}-command", sim_started=False, master=None, previous={},
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
                        source.capture("avc-direction-failure", description="AVC 增减闭锁清理前现场")
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
