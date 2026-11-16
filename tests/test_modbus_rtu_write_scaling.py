"""核对 IEC104 遥调经 Modbus RTU 写点反算后实际收到的寄存器原始值。"""

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from testbench.case_plan import (
    DESKTOP_FINISH, DESKTOP_START, MASTER104_FINISH, MASTER104_START, SITE_CONNECT,
    SLAVESIM_BROWSER_READY, Step, case_plan, run_steps, track_fixture_context, track_fixture_step,
)
from testbench.login_page import PlatformLoginPage
from testbench.master104_commands import Master104CommandSender

from case_support.agc_direction_iec104 import AgcDirectionIec104
from case_support.case_metadata import execution_logic
from case_support.login_plan import login_steps
from case_support.measurement_expiry_run import SIM_CLEANUP, _expect_writes, _protocol
from case_support.measurement_expiry_simulator import MeasurementExpirySimulator
from case_support.modbus_rtu_page import ModbusRtuPage
from case_support.power_range_run import _environment, _load_account
from case_support.rtu_quality_frames import capture_frames


pytestmark = [pytest.mark.case_group("Modbus RTU"), pytest.mark.requires_modbus]

ROLE = "device"
REFERENCE_TAG = "read-reference"
WRITE_TAG = "scaled-setpoint"
SCALE = "0.1"
OFFSET = 2


def _prepare_rtu(context):
    page = context.rtu
    page.open_current_links()
    context.sim_started = True
    context.points = context.simulator.prepare(context.name, {ROLE: (1234, 0)}, page.assert_serial_paths_available)
    reference, write_point = context.points[ROLE]
    context.rtu_name = f"{context.name}-{ROLE}"
    page.create_connection(context.rtu_name, reference["actual_device"], 9600, 8, 1, "N", reference["station_id"], 1000)
    page.add_point(REFERENCE_TAG, address=reference["address"])
    page.add_point(WRITE_TAG, function=6, address=write_point["address"], scale=SCALE, offset=OFFSET)
    page.assert_saved_configuration()
    context.desktop.metadata["本例 RTU 写点反算"] = {
        "连接": context.rtu_name, "站号": write_point["station_id"], "地址": write_point["address"],
        "数据类型": "UINT16", "倍率": SCALE, "偏移": OFFSET, "寄存器初值": write_point["value"],
    }
    page.start()
    page.wait_value(REFERENCE_TAG, 1234, 0, context.timeout)


def _prepare_104(context):
    page = context.iec104
    page.create()
    target = f"{context.name}-target"
    context.ioa = page.import_point(context.rtu_name, WRITE_TAG, target, business_type="遥调")
    context.desktop.check("核对 IEC104 遥调的 IOA、倍率 1 和偏移 0",
                          page._read_point(target, page.FLOAT, "遥调"), context.ioa)
    page.check_routes()
    context.desktop.metadata["本例 IEC104 遥调与路由"] = {
        "端口": page.port, "公共地址": 1, "IOA": context.ioa, "倍率": 1, "偏移": 0, "路由": page.routes,
    }
    page.start()


def _connect(context):
    context.master.prepare_target(context.host, context.iec104.port, 1)
    context.master.connect()
    context.sender = Master104CommandSender(context.master, 1, context.ioa)


def _verify_target(context, target, raw):
    marker = len(context.simulator.events()) - 1
    evidence = {"遥调工程量": target, "期望寄存器原始值": raw}
    context.desktop.metadata.setdefault("反算验收", []).append(evidence)
    evidence["IEC104 遥调结果"] = context.sender.send_target_kw(target, timeout=context.timeout)
    evidence["本轮 RTU 写入和应答"] = _expect_writes(context, {ROLE: raw}, after=marker)
    point = context.points[ROLE][1]
    actual = context.simulator.observe(
        point["bus"], point["station_id"], point["function"], point["address"], verify_manual=False,
    )
    evidence["寄存器回读"] = actual
    context.desktop.check("核对模拟串口身份",
                          (actual["actual_device"], actual["link_path"]),
                          (point["actual_device"], point["link_path"]))
    context.desktop.check("核对模拟寄存器实际原始值", actual["value"], raw)
    _, writes = _protocol(context)
    received = [write for write in writes[ROLE] if write["index"] > marker]
    evidence["本轮 RTU 写请求"] = [
        {**write, "time": write["time"].isoformat(), "request": write["request"].hex(" ")}
        for write in received
    ]
    wrong = [write["value"] for write in received if write["value"] != raw]
    context.desktop.check("核对本轮未写入其他原始值", wrong, [])


def _save_104(context):
    path = context.master.artifacts / "rtu-write-scaling-iec104-frames.json"
    path.write_text(json.dumps(capture_frames(context.master), ensure_ascii=False, indent=2), encoding="utf-8")
    context.desktop.metadata["本例 IEC104 报文"] = path.name


def _capture_failure(context, failure):
    for source in (context.desktop, context.simulator, context.master):
        if source is not None:
            try:
                source.capture("rtu-write-scaling-failure", description="写点反算验收清理前现场")
            except Exception as error:
                failure.add_note(f"截图失败：{error}")
    if context.master is not None:
        try:
            _save_104(context)
        except Exception as error:
            failure.add_note(f"IEC104 报文取证失败：{error}")


def _cleanup(context, request, failure):
    errors = []
    upper_failed = False
    try:
        with track_fixture_step(request, UPPER_CLEANUP):
            context.rtu._cleaning = True
            try:
                for label, clean in (("IEC104", context.iec104.cleanup), ("RTU", context.rtu.cleanup)):
                    try:
                        clean()
                    except Exception as error:
                        errors.append(f"{label}：{error}")
            finally:
                context.rtu._cleaning = False
            if errors:
                raise RuntimeError("；".join(errors))
    except Exception as error:
        upper_failed = True
        context.desktop.metadata["清理本例 IEC104 或 RTU 链路错误"] = str(error)
        if failure is None:
            raise
        failure.add_note(f"清理本例链路失败：{error}")
    finally:
        if context.sim_started:
            try:
                with track_fixture_step(request, SIM_CLEANUP):
                    context.simulator.clear_all_buses()
            except Exception as error:
                context.desktop.metadata["清空模拟总线错误"] = str(error)
                if failure is None and not upper_failed:
                    raise
                if failure is not None:
                    failure.add_note(f"清空模拟总线失败：{error}")


ENVIRONMENT = Step("write-scaling.env", "检查 slaveSim 和独立 IEC104 主站入口", "模拟器、主站和驱动可用")
SIM_START = Step("write-scaling.sim.start", "打开本例 slaveSim 会话", "可读配置和实际 RTU 报文")
SIM_FINISH = Step("write-scaling.sim.finish", "关闭本例 slaveSim 会话", "独立浏览器会话已退出")
LOGIN = (Step("write-scaling.account", "读取测试账号", "专用账号配置有效", _load_account),
         *login_steps("write-scaling.login"))
SCENARIO = (
    Step("write-scaling.rtu", "创建 RTU 参考点及带倍率和偏移的写点",
         "读回倍率 0.1、偏移 2，参考点正常采集", _prepare_rtu),
    Step("write-scaling.104", "建立专用 IEC104 短浮点遥调和直达 RTU 写点的路由",
         "回读遥调 IOA、倍率 1、偏移 0 和 IEC104 到 RTU 的路由", _prepare_104),
)
OPERATION = (
    Step("write-scaling.connect", "连接本例 IEC104 被控站", "模拟主站收到 STARTDT 确认", _connect),
    Step("write-scaling.first", "发送工程量 50 并核对实际写入", "本轮 RTU 写 480 并应答，寄存器回读 480",
         lambda context: _verify_target(context, 50, 480)),
    Step("write-scaling.second", "发送工程量 62 并核对实际写入", "本轮 RTU 写 600 并应答，寄存器回读 600",
         lambda context: _verify_target(context, 62, 600)),
    Step("write-scaling.capture", "保存本例主站原始报文", "IEC104 报文已保留", _save_104),
)
UPPER_CLEANUP = Step("write-scaling.upper.cleanup", "清理本例 IEC104、RTU 连接和路由", "本例专用连接已删除")


@case_plan(ENVIRONMENT, SLAVESIM_BROWSER_READY, DESKTOP_START, *LOGIN, SITE_CONNECT, SIM_START, *SCENARIO,
           MASTER104_START, *OPERATION, UPPER_CLEANUP, SIM_CLEANUP, SIM_FINISH, MASTER104_FINISH, DESKTOP_FINISH)
@execution_logic("""
1. 通过 slaveSim 建立专用从站、参考读点和原始值为 0 的写寄存器。创建对应 RTU 连接，配置 UINT16 写点倍率 0.1、偏移 2，回读并确认采集正常。
2. 建立专用 IEC104 短浮点遥调点并导入 RTU 写点作为路由目的。回读遥调 IOA、倍率 1、偏移 0 以及路由，发起独立主站会话。
3. 依次发送工程量 50 和 62，分别核对本轮实际 RTU 写请求与应答中原始值 480 和 600，并读回模拟寄存器。
4. 保存帧证据，失败时先取证，再清理本例专用连接、路由和模拟总线。
""")
def test_rtu_write_reverses_scale_and_offset_from_iec104(request):
    """IEC104 遥调后 RTU 写寄存器按倍率和偏移反算"""
    with track_fixture_step(request, ENVIRONMENT):
        url, timeout = _environment()
    with track_fixture_step(request, SLAVESIM_BROWSER_READY):
        browser = request.getfixturevalue("slavesim_browser")
    desktop = request.getfixturevalue("fresh_desktop")
    name = f"e2e-rtu-write-{uuid4().hex[:8]}"
    rtu = ModbusRtuPage(desktop)
    context = SimpleNamespace(name=name, timeout=timeout, desktop=desktop, page=PlatformLoginPage(desktop),
                              rtu=rtu, iec104=AgcDirectionIec104(rtu, name), sim_started=False, master=None)
    run_steps(request, LOGIN, context)
    with track_fixture_step(request, SITE_CONNECT):
        context.host = rtu.connect_site().rsplit(":", 1)[0]
    with track_fixture_context(request, SIM_START, SIM_FINISH,
                               MeasurementExpirySimulator(desktop, url, browser=browser)) as simulator:
        context.simulator = simulator
        failure = None
        try:
            run_steps(request, SCENARIO, context)
            context.master = request.getfixturevalue("master104")
            run_steps(request, OPERATION, context)
        except BaseException as error:
            failure = error
            _capture_failure(context, failure)
            raise
        finally:
            _cleanup(context, request, failure)
