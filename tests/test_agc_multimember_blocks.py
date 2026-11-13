"""验收多成员到限后的有功分配和 AGC 组级增减闭锁遥信。"""

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
from case_support.case_metadata import execution_logic
from case_support.login_plan import login_steps
from case_support.measurement_expiry_agc import MeasurementExpiryAgc
from case_support.measurement_expiry_run import (
    SIM_CLEANUP, UPPER_CLEANUP, _cleanup, _expect_writes, _protocol, prepare_control_rtu,
)
from case_support.measurement_expiry_simulator import MeasurementExpirySimulator
from case_support.modbus_rtu_page import ModbusRtuPage
from case_support.power_range_run import _environment, _load_account, _save_frames


pytestmark = [pytest.mark.case_group("AGC 控制"), pytest.mark.requires_modbus]

CAPACITIES = {"A": 20, "B": 100}


def _prepare_group(context):
    context.target_tag = context.group.create_communication_group(
        context.name, context.connections, capacities=CAPACITIES,
    )["command_tag"]


def _prepare_104(context):
    context.iec104.create()
    context.mapping = context.iec104.configure(context.name, command_tag=context.target_tag)
    context.desktop.metadata["本例 IEC104 点表和路由"] = {
        **context.mapping, "路由": list(context.iec104.routes),
    }
    context.iec104.start()


def _connect(context):
    mapping = context.mapping
    context.master.prepare_target(context.host, mapping["port"], mapping["ca"])
    context.master.connect()
    context.sender = Master104CommandSender(context.master, mapping["ca"], mapping["target_ioa"])
    context.observer = Master104PointObserver(
        context.master, {"increase": mapping["increase_ioa"], "decrease": mapping["decrease_ioa"]}, mapping["ca"],
    )
    context.group.start()

    def online(_):
        return all(context.group.read_member_communication(name)["condition"] == "通信正常" for name in CAPACITIES)

    context.group._until(online, "AGC 成员未全部通信正常", timeout=context.timeout)


def _phase(identifier, action, target, a, b, *, increase=False, decrease=False):
    expected = {"A": a, "B": b}
    signals = {"increase": increase, "decrease": decrease}
    feedback = f"RTU 设定 A={a}、B={b} kW，IEC104 增闭锁={int(increase)}、减闭锁={int(decrease)}，品质有效"

    def run(context):
        baseline = context.observer.checkpoint() if context.previous else None
        if baseline is not None:
            current = {key: point["value"] for key, point in baseline["points"].items()}
            context.master.check("核对遥调前闭锁基线", current, context.previous_signals)
        marker = len(context.simulator.events()) - 1
        result = context.sender.send_target_kw(target, timeout=context.timeout)
        record = {"总目标 kW": target, "设定预期": expected, "闭锁预期": signals, "遥调结果": result}
        context.desktop.metadata.setdefault("多成员闭锁观察", []).append(record)
        changed_setpoints = {name: value for name, value in expected.items() if context.previous.get(name) != value}
        record["本轮实收写入和应答"] = _expect_writes(context, changed_setpoints, after=marker, previous=context.previous)
        for name, value in expected.items():
            point = context.points[name][1]
            actual = context.simulator.observe(
                point["bus"], point["station_id"], point["function"], point["address"], verify_manual=False,
            )
            assert (actual["actual_device"], actual["link_path"]) == (point["actual_device"], point["link_path"]), (
                f"成员 {name} 的模拟串口身份变化：{actual!r}")
            context.desktop.check(f"核对 {name} 的设定寄存器", actual["value"], value)

        if baseline is not None:
            changed_signals = tuple(key for key, value in signals.items() if baseline["points"][key]["value"] != value)
            if changed_signals:
                baseline["wall_time"] = result["sent_at"]
                record["本轮遥信变位"] = context.observer.wait_state(
                    signals, baseline, context.timeout, fresh_keys=changed_signals, sent_frame_id=result["sent_frame_id"],
                )
        context.master.interrogate()
        checkpoint = context.observer.checkpoint()
        record["总召后闭锁遥信"] = checkpoint
        context.master.check("核对两路有效闭锁遥信",
                             {key: point["value"] for key, point in checkpoint["points"].items()}, signals)
        assert all(point["frame_id"] > result["sent_frame_id"] for point in checkpoint["points"].values()), (
            f"闭锁遥信缺少本轮遥调后的接收证据：{checkpoint!r}")
        _, writes = _protocol(context)
        for name, value in expected.items():
            allowed = {value, context.previous.get(name)}
            unexpected = [entry["value"] for entry in writes[name]
                          if entry["index"] > marker and entry["value"] not in allowed]
            context.desktop.check(f"核对 {name} 本轮写入无错误目标", unexpected, [])
        context.previous = expected
        context.previous_signals = signals

    return Step(f"agc.multiblock.{identifier}", action, feedback, run)


ENVIRONMENT = Step("agc.multiblock.environment", "检查 slaveSim 和模拟 104 主站入口", "测试入口及驱动有效")
SIM_START = Step("agc.multiblock.sim.start", "打开本例 slaveSim", "能读取配置与运行报文")
SIM_FINISH = Step("agc.multiblock.sim.finish", "关闭本例 slaveSim", "独立浏览器会话已关闭")
LOGIN = (Step("agc.multiblock.account", "读取测试账号", "专用账号配置有效", _load_account),
         *login_steps("agc.multiblock.login"))
PREPARE = (
    Step("agc.multiblock.rtu", "建立并启动两条专用 RTU 连接", "读回实际串口、站号、寄存器和初始有功量测",
         lambda context: prepare_control_rtu(context, {"A": (15, 0), "B": (35, 0)})),
    Step("agc.multiblock.group", "建立按容量比例分配的双成员 AGC 组",
         "两台成员的调节范围为 10～20 和 10～100 kW", _prepare_group),
    Step("agc.multiblock.104", "建立专用 IEC104 遥调与增减闭锁遥信", "读回点号及路由，链路已启动", _prepare_104),
)
CONNECT = Step("agc.multiblock.connect", "连接主站并启动本例 AGC 控制组", "主站连接且两台成员均通信正常", _connect)
PHASES = (
    _phase("initial", "遥调 56 kW 建立两台均未到限的基线", 56, 16, 40),
    _phase("partial", "遥调 86 kW，检查单台到限和剩余目标分配", 86, 20, 66),
    _phase("upper", "遥调 120 kW，检查两台均到上限", 120, 20, 100, increase=True),
    _phase("down", "反向遥调 56 kW，检查增闭锁解除", 56, 16, 40),
    _phase("lower", "遥调 20 kW，检查两台均到下限", 20, 10, 10, decrease=True),
    _phase("up", "反向遥调 56 kW，检查减闭锁解除", 56, 16, 40),
)
CAPTURE = Step("agc.multiblock.capture", "保存模拟主站原始报文", "保留本例 IEC104 收发报文")


@case_plan(ENVIRONMENT, SLAVESIM_BROWSER_READY, DESKTOP_START, *LOGIN, SITE_CONNECT, SIM_START, *PREPARE,
           MASTER104_START, CONNECT, *PHASES, CAPTURE, UPPER_CLEANUP, SIM_CLEANUP, SIM_FINISH,
           MASTER104_FINISH, DESKTOP_FINISH)
@execution_logic("""
1. 独立建立两台模拟 RTU、AGC 控制组和专用 IEC104 路由，成员范围为 10～20 和 10～100 kW。
2. 设为按额定容量比例分配。遥调 56 kW，核对 16/40 kW 及两方向均未闭锁。
3. 遥调 86 kW，核对 A 到 20 kW 上限，B 承担剩余目标 66 kW，组增闭锁仍为否。
4. 遥调 120 kW 和 20 kW，分别核对全组到上限和下限时对应闭锁置位。每次到限后反向遥调 56 kW，核对写入和闭锁解除。
5. 每轮核对实际 RTU 报文、寄存器及 IEC104 遥信值和品质。失败时先取证，再清理本例资源。
""")
def test_agc_partial_saturation_redistributes_without_group_block(request):
    """AGC 部分成员到限重分配和全组增减闭锁"""
    with track_fixture_step(request, ENVIRONMENT):
        url, timeout = _environment()
    with track_fixture_step(request, SLAVESIM_BROWSER_READY):
        browser = request.getfixturevalue("slavesim_browser")
    desktop = request.getfixturevalue("fresh_desktop")
    name = f"e2e-block-{uuid4().hex[:8]}"
    rtu = ModbusRtuPage(desktop)
    context = SimpleNamespace(
        name=name, module="AGC", timeout=timeout, desktop=desktop, page=PlatformLoginPage(desktop), rtu=rtu,
        sim_started=False, group=MeasurementExpiryAgc(desktop), iec104=AgcDirectionIec104(rtu, name),
        master=None, previous={},
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
            run_steps(request, (CONNECT, *PHASES), context)
            with track_fixture_step(request, CAPTURE):
                _save_frames(context.master)
        except BaseException as error:
            failure = error
            for source in (desktop, simulator, context.master):
                if source is not None:
                    try:
                        source.capture("agc-multiblock-failure", description="AGC 多成员闭锁清理前现场")
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
