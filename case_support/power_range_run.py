"""AGC 有功范围和 AVC 无功裕度共用的端到端准备、观测和清理。"""

from decimal import Decimal
import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By

from testbench.case_plan import (
    DESKTOP_FINISH, DESKTOP_START, MASTER104_FINISH, MASTER104_START, SITE_CONNECT,
    SLAVESIM_BROWSER_READY, Step, run_steps, track_fixture_context, track_fixture_step,
)
from testbench.login_page import PlatformLoginPage, load_login_account
from testbench.model import Environment, validate_slavesim_url
from testbench.workspace import local_config

from .login_plan import login_steps
from .power_range_page import PowerRangePage
from .rtu_quality_frames import capture_frames
from .rtu_quality_iec104 import RtuQualityIec104
from .slavesim_page import SlaveSimPage


class PowerRangeSimulator(SlaveSimPage):
    def _station_editor(self, name):
        station = self._unique(self._config_bus(name).find_elements(
            By.CSS_SELECTOR, ":scope > details.slave-editor"), "本例模拟从站")
        self._open(station)
        return station

    def prepare(self, name, initial, check_paths):
        before = self._prepare_empty_configuration("功率场景准备前")
        draft, link = self._add_default_bus(replace_before=before)
        self._replace(self._field(self._config_bus(draft), "总线名称"), name)
        self._add_basic_stations(name, 1)
        station = self._station_editor(name)
        station.find_element(By.XPATH, "./button[normalize-space(.)='新增寄存器']").click()
        # 新增寄存器会重绘配置区，需要重新定位从站。
        station = self._station_editor(name)
        group = station.find_element(By.CSS_SELECTOR, ":scope > details.register-group")
        self._open(group)
        registers = group.find_elements(By.CSS_SELECTOR, ":scope > details.register-editor")
        for address, (register, value) in enumerate(zip(registers, (initial, 0), strict=True)):
            self._open(register)
            self._replace(self._field(register, "协议地址"), address)
            self._select_register_option(register, "访问权限", "readwrite")
            self._select_register_option(register, "数值来源", "manual")
            self._replace(self._field(register, "原始值"), value)
        buses = self._save_configuration(
            "建立功率实测和设定寄存器", before=before, owned_names=(name,),
            removed_names=tuple(bus["name"] for bus in before),
        )
        bus = self._unique(buses, "本例模拟总线")
        assert (bus["name"], bus["link"]) == (name, link), f"总线身份或路径不符：{bus}"
        slave = self._unique(bus["slaves"], "本例模拟从站")
        actual = sorted((row["address"], row["function"], row["type"], row["byte_order"], row["access"],
                         row["source"], int(row["value"])) for row in slave["registers"])
        assert actual == [(0, "0x03", "UINT16", "AB", "readwrite", "manual", initial),
                          (1, "0x03", "UINT16", "AB", "readwrite", "manual", 0)], actual
        measured = self.observe(name, slave["station_id"], "0x03", 0)
        setpoint = self.observe(name, slave["station_id"], "0x03", 1)
        assert measured["link_path"] == setpoint["link_path"] == link
        assert measured["actual_device"] == setpoint["actual_device"]
        assert (measured["value"], setpoint["value"]) == (initial, 0)
        check_paths((measured["actual_device"],))
        return measured


def _load_account(context):
    context.account = load_login_account(local_config("login-account.json"))


def _environment():
    task_path = os.environ.get("TESTBENCH_TASK")
    if not task_path:
        raise RuntimeError("请从测试上位机运行并启用独立 IEC104 模拟主站")
    environment = Environment.from_dict(json.loads(Path(task_path).read_text(encoding="utf-8"))["environment"])
    environment.validate_master104(required=True)
    environment.validate_master104_driver()
    url = validate_slavesim_url(environment.slavesim_url, required=True)
    timeout = environment.modbus_acquisition_timeout
    if type(timeout) not in (int, float) or not 5 <= timeout <= 300:
        raise ValueError("采集阶段等待时限必须为 5 至 300 秒")
    return url, timeout


def _prepare_sim(context):
    context.sim_started = True
    page = context.upper
    page.open_current_links()
    context.measured = context.simulator.prepare(
        context.name, context.initial - context.offset, page.assert_serial_paths_available,
    )


def _prepare_rtu(context):
    page, measured = context.upper, context.measured
    page.open_current_links()
    page.create_connection(context.name, measured["actual_device"], 9600, 8, 1, "N", measured["station_id"], 1000)
    page.add_point("power-measured", offset=context.offset)
    page.add_point("power-setpoint", function=6, address=1)
    page.assert_saved_configuration()
    page.start()
    page.wait_value("power-measured", context.initial, 0, context.timeout)


def _prepare_group(context):
    context.upper.create_group(context.name, *context.limits)
    context.upper.start_group()


def _prepare_iec104(context):
    context.port = context.iec104.create()
    for tag in context.remote_tags:
        context.ioas[tag] = context.iec104.import_point(context.name, tag, tag, module=context.module)
    assert len(set(context.ioas.values())) == len(context.remote_tags), f"IEC104 IOA 重复：{context.ioas}"
    context.iec104.check_routes()
    context.iec104.start()
    context.desktop.metadata["本例 IEC104 遥测"] = {
        "主机": context.host, "端口": context.port, "公共地址": 1, "点位": context.ioas,
    }


def _connect_master(context):
    with context.master.step("连接专用 IEC104 链路", expected=f"{context.host}:{context.port}，CA=1"):
        context.master.prepare_target(context.host, context.port, 1)
        context.master.connect()


def _wait_master(context, expected, baseline):
    master, ioas = context.master, context.ioas
    last = {}

    def observed():
        rows = master.read_measurements()["rows"]
        for tag, value in expected.items():
            matches = [row for row in rows if row["cells"][:2] == ["1", str(ioas[tag])]]
            assert len(matches) <= 1, f"CA=1 IOA={ioas[tag]} 的遥测行不唯一：{matches!r}"
            last[tag] = matches[0] if matches else None
            if not matches:
                continue
            row = matches[0]
            if (int(row["frame_id"] or 0) <= baseline.get(ioas[tag], 0)
                    or int(row["cells"][2]) not in (13, 36)
                    or Decimal(row["cells"][3]) != value or int(row["cells"][4], 16) != 0):
                return False
        return all(last.get(tag) is not None for tag in expected)

    with master.step("核对本阶段 IEC104 实收值及品质", expected=f"{expected}，品质 0x00") as entry:
        try:
            master.wait_for(observed, "IEC104 遥测未达到本阶段预期", timeout=context.timeout)
        except TimeoutException as error:
            raise AssertionError(f"IEC104 遥测不符，预期 {expected}，最后观察 {last}") from error
        finally:
            entry["实际"] = str(last)
            master.metadata.setdefault("本例遥测观察", []).append({"预期": expected, "实际": dict(last)})


def power_phase(identifier, label, measurement, station, remote, *, configured_upper=None):
    """每个工况使用固定期望，并要求本轮产生新的主站接收帧。"""
    def check(context):
        baseline = {
            int(row["cells"][1]): int(row["frame_id"] or 0)
            for row in context.master.read_measurements()["rows"] if row["cells"][0] == "1"
        }
        if configured_upper is not None:
            context.upper.set_member_upper_limit(configured_upper)
        measured = context.measured
        if measurement != context.current:
            context.upper.open_current_links()
            context.upper.select(context.name)
            previous_time = context.upper.observe("power-measured")["timestamp"]
            actual = context.simulator.set_value(measured["bus"], measured["station_id"], measured["function"],
                                                 measured["address"], measurement - context.offset)
            assert actual["actual_device"] == measured["actual_device"], "工况变化后实际串口与本例 RTU 链路不符"
            context.upper.wait_value("power-measured", measurement, 0, context.timeout, previous_time)
            context.current = measurement
        context.upper.wait_station_values(station, context.timeout)
        if context.previous_remote is not None:
            changed = {tag: value for tag, value in remote.items() if context.previous_remote[tag] != value}
            if changed:
                _wait_master(context, changed, baseline)
        with context.master.step("总召读取本阶段遥测", expected="收到本次总召确认和终止"):
            context.master.interrogate()
        _wait_master(context, remote, baseline)
        context.previous_remote = remote

    return Step(identifier, label, f"实测 {measurement}，全站 {station}，IEC104 {remote} 且品质有效", check)


ENVIRONMENT = Step("power.environment", "检查 slaveSim 和独立 IEC104 主站入口", "模拟器、主站和驱动配置有效")
SIM_START = Step("power.sim.start", "打开 slaveSim", "本例能访问模拟器配置和运行页面")
SIM_FINISH = Step("power.sim.finish", "关闭 slaveSim 浏览器", "本例浏览器会话已关闭")
LOGIN = (Step("power.account", "读取专用测试账号", "账号配置有效", _load_account), *login_steps("power.login"))
PREPARE = (
    Step("power.sim.prepare", "重建模拟总线和寄存器", "读回本例实测、设定寄存器和实际串口", _prepare_sim),
    Step("power.rtu.prepare", "创建并启动本例 RTU 采集", "使用模拟器实际串口、站号及寄存器，采集值有效", _prepare_rtu),
    Step("power.group.prepare", "建立并运行本例控制组", "控制组配置和成员点路由保存成功，组在运行中", _prepare_group),
    Step("power.iec104.prepare", "建立本例 IEC104 遥测链路", "两个控制组输出点均可导入，路由、IOA 和监听端口可读回",
         _prepare_iec104),
)
CONNECT = Step("power.master.connect", "连接模拟 IEC104 主站", "使用现场连接主机和本例端口，收到 STARTDT 确认", _connect_master)
CAPTURE = Step("power.master.capture", "保存模拟主站原始报文", "本例主站报文已记录")
UPPER_CLEANUP = Step("power.upper.cleanup", "删除本例 IEC104 链路、控制组和 RTU 连接", "专用配置和路由已清理")
SIM_CLEANUP = Step("power.sim.cleanup", "清空 slaveSim 总线", "回读配置确认总线数为 0")


def power_plan(phases):
    return (ENVIRONMENT, SLAVESIM_BROWSER_READY, DESKTOP_START, *LOGIN, SITE_CONNECT, SIM_START, *PREPARE,
            MASTER104_START, CONNECT, *phases, CAPTURE, UPPER_CLEANUP, SIM_CLEANUP,
            SIM_FINISH, MASTER104_FINISH, DESKTOP_FINISH)


def _save_frames(master):
    path = master.artifacts / "power-range-iec104-frames.json"
    path.write_text(json.dumps(capture_frames(master), ensure_ascii=False, indent=2), encoding="utf-8")


def _cleanup(request, context, failure):
    errors = []
    upper = context.upper
    metadata = context.desktop.metadata
    try:
        with track_fixture_step(request, UPPER_CLEANUP):
            upper._cleaning = True
            try:
                for label, cleanup in (("IEC104 连接", context.iec104.cleanup),
                                       (f"{context.module} 控制组 {upper.group_name}", upper.cleanup_group),
                                       ("RTU 连接", upper.cleanup)):
                    try:
                        cleanup()
                    except Exception as error:
                        errors.append(f"{label}：{error}")
            finally:
                upper._cleaning = False
            if errors:
                metadata.setdefault("测试资源清理错误", []).extend(errors)
                raise RuntimeError(f"上位机资源清理失败：{errors}")
    except Exception as error:
        if failure is None:
            failure = error
            raise
        failure.add_note(str(error))
    finally:
        if context.sim_started:
            try:
                with track_fixture_step(request, SIM_CLEANUP):
                    context.simulator.clear_all_buses()
            except Exception as error:
                metadata.setdefault("测试资源清理错误", []).append(f"{SIM_CLEANUP.action}：{error}")
                if failure is None:
                    raise
                failure.add_note(f"清空 slaveSim 失败：{error}")


def run_power_case(request, module, limits, initial, offset, remote_tags, phases):
    with track_fixture_step(request, ENVIRONMENT):
        url, timeout = _environment()
    with track_fixture_step(request, SLAVESIM_BROWSER_READY):
        browser = request.getfixturevalue("slavesim_browser")
    desktop = request.getfixturevalue("fresh_desktop")
    name = f"e2e-{module.lower()}-range-{uuid4().hex[:8]}"
    upper = PowerRangePage(desktop, module, name)
    context = SimpleNamespace(
        name=name, module=module, desktop=desktop, page=PlatformLoginPage(desktop), upper=upper,
        limits=limits, initial=initial, current=initial, offset=offset, timeout=timeout, remote_tags=remote_tags,
        iec104=RtuQualityIec104(upper, name), ioas={}, sim_started=False, master=None, previous_remote=None,
    )
    run_steps(request, LOGIN, context)
    with track_fixture_step(request, SITE_CONNECT):
        context.host = upper.connect_site().rsplit(":", 1)[0]
    with track_fixture_context(request, SIM_START, SIM_FINISH,
                               PowerRangeSimulator(desktop, url, browser=browser)) as simulator:
        context.simulator = simulator
        failure = None
        try:
            run_steps(request, PREPARE, context)
            context.master = request.getfixturevalue("master104")
            run_steps(request, (CONNECT, *phases), context)
            with track_fixture_step(request, CAPTURE):
                _save_frames(context.master)
        except BaseException as error:
            failure = error
            for label, capture in (("DSP 上位机", desktop.capture), ("slaveSim", simulator.capture)):
                try:
                    capture("power-range-failure", description=f"{label}清理前现场")
                except Exception as evidence_error:
                    error.add_note(f"{label}取证失败：{evidence_error}")
            if context.master is not None:
                try:
                    _save_frames(context.master)
                except Exception as evidence_error:
                    error.add_note(f"主站报文取证失败：{evidence_error}")
            raise
        finally:
            _cleanup(request, context, failure)
