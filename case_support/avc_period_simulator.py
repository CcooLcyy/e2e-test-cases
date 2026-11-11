"""为 AVC 周期用例配置 RTU 量测并保留成员写命令的实际报文。"""

from datetime import datetime
import json

from selenium.webdriver.common.by import By

from .slavesim_page import SlaveSimPage


class AvcPeriodSimulator(SlaveSimPage):
    STATION = 1
    FUNCTION = "0x03"
    VOLTAGE = 0
    MEASURED_Q = 1
    COMMAND = 2

    def __init__(self, desktop, url, *, browser=None):
        super().__init__(desktop, url, browser=browser)
        self._observing_commands = False
        self._command_events = []
        self._writes = []

    def _station_editor(self, name):
        bus = self._config_bus(name)
        self._open(bus)
        station = self._unique(bus.find_elements(By.CSS_SELECTOR, ":scope > details.slave-editor"), "本例 AVC 从站")
        self._open(station)
        return station

    def prepare(self, name, check_paths=None, *, include_target=False):
        """返回运行页实际读回的电压、无功实测和成员命令点。"""
        before = self._prepare_empty_configuration("AVC 周期场景准备前")
        points = (
            (self.VOLTAGE, "avc-voltage", "V", "readonly", "const", 220),
            (self.MEASURED_Q, "avc-q-measured", "kVar", "readonly", "const", 0),
            (self.COMMAND, "avc-q-command", "kVar", "readwrite", "manual", 0),
        )
        if include_target:
            points += ((3, "avc-q-target", "kVar", "readwrite", "manual", 10),)
        with self._step("新建本例 RTU 总线及三个 AVC 寄存器"):
            default_name, link = self._add_default_bus(replace_before=before)
            self._replace(self._field(self._config_bus(default_name), "总线名称"), name)
            self._add_basic_stations(name, 1)
            for _ in points[1:]:
                self._station_editor(name).find_element(By.XPATH, "./button[normalize-space(.)='新增寄存器']").click()
            group = self._station_editor(name).find_element(By.CSS_SELECTOR, ":scope > details.register-group")
            self._open(group)
            registers = group.find_elements(By.CSS_SELECTOR, ":scope > details.register-editor")
            assert len(registers) == len(points), f"本例 AVC 寄存器数量应为 3，实际 {len(registers)}"
            for register, (address, tag, unit, access, source, initial) in zip(registers, points, strict=True):
                self._open(register)
                self._replace(self._field(register, "协议地址"), address)
                self._replace(self._field(register, "点名"), tag)
                self._replace(self._field(register, "显示单位"), unit)
                self._select_register_option(register, "访问权限", access)
                self._select_register_option(register, "数值来源", source)
                self._replace(self._field(register, "原始值"), initial)

        self._observe_commands(name)
        saved = self._save_configuration("建立 AVC 电压、无功实测与成员命令寄存器", before=before,
                                         owned_names=(name,), removed_names=tuple(bus["name"] for bus in before))
        bus = self._unique(saved, "本例 AVC 总线")
        assert (bus["name"], bus["link"]) == (name, link), f"模拟总线名称或路径与预期不符：{bus!r}"
        station = self._unique(bus["slaves"], "本例 AVC 从站")
        assert station["station_id"] == self.STATION and not station["model"], f"模拟从站配置不符：{station!r}"
        actual = sorted((p["address"], p["fields"]["点名"], p["fields"]["显示单位"], p["access"], p["source"],
                         int(p["value"]), p["function"], p["type"], p["byte_order"]) for p in station["registers"])
        expected = [(*point, self.FUNCTION, "UINT16", "AB") for point in points]
        assert actual == expected, f"AVC 寄存器保存结果不符：{actual!r}"

        self.driver.find_element(By.ID, "show-runtime").click()
        def log_ready(_):
            result = self._browser_events(read=False)
            self._check_log_connection(result)
            return result["ready"]

        self._wait(log_ready, "slaveSim 报文日志没有本例串口就绪事件")
        observations = tuple(self.observe(name, self.STATION, self.FUNCTION, address, verify_manual=False)
                             for address, *_ in points)
        devices = {point["actual_device"] for point in observations}
        serial_device = self._unique(tuple(devices), "本例 AVC 总线实际设备")
        assert {point["link_path"] for point in observations} == {link}, "AVC 运行页软链接与保存结果不一致"
        assert tuple(point["value"] for point in observations) == tuple(point[-1] for point in points), (
            f"AVC 模拟初态不符：{observations!r}"
        )
        self.desktop.metadata["RTU运行时资源"] = {"bus": name, "serial_device": serial_device, "link_path": link,
                                                  "points": observations}
        self.desktop.metadata["RTU串口参数来源"] = "DSP 使用 slaveSim 运行页的实际设备和 9600/8N1"
        if check_paths is not None:
            check_paths((serial_device,))
        return observations

    def _observe_commands(self, name):
        """旁听运行页已有日志，在页面将报文时间缩为整秒前保留原值。"""
        self._wait(lambda driver: driver.find_element(By.ID, "conn").get_property("textContent") == "已连接",
                   "slaveSim 报文日志未连接")
        self.driver.execute_script("""
            const [bus, station] = arguments;
            const append = window.appendLog;
            const status = document.getElementById('conn');
            if (typeof append !== 'function' || !status || window.__testbenchAvcLog) {
                throw new Error('slaveSim 运行页缺少可观察的报文日志入口');
            }
            const watch = {events: [], ready: false, disconnected: status.textContent !== '已连接', append};
            watch.wrapped = event => {
                if (event.bus === bus) {
                    if (event.summary?.startsWith('虚拟串口已就绪:')) watch.ready = true;
                    const fn = event.frame?.slice(3, 5);
                    if (event.device_id === station && ['06', '10', '86', '90'].includes(fn)) {
                        watch.events.push({...event, received_at: (performance.timeOrigin + performance.now()) / 1000});
                    }
                }
                append(event);
            };
            watch.status = new MutationObserver(records => {
                if (status.textContent !== '已连接' || records.some(record =>
                    Array.from(record.removedNodes).some(node => node.textContent.includes('连接中断')))) {
                    watch.disconnected = true;
                }
            });
            watch.status.observe(status, {childList: true});
            window.appendLog = watch.wrapped;
            window.__testbenchAvcLog = watch;
        """, name, self.STATION)
        self._observing_commands = True
        self.desktop.metadata["AVC成员命令时间来源"] = "slaveSim 运行页报文日志的服务端事件时间；浏览器接收时间单独记录"

    def _browser_events(self, *, read=True, finish=False):
        return self.driver.execute_script("""
            const [read, finish] = arguments;
            const watch = window.__testbenchAvcLog;
            if (!watch || window.appendLog !== watch.wrapped) return {missing: true, events: []};
            const result = {ready: watch.ready, events: read ? watch.events.splice(0) : [],
                disconnected: watch.disconnected || document.getElementById('conn').textContent !== '已连接'};
            if (finish) {
                watch.status.disconnect();
                window.appendLog = watch.append;
                delete window.__testbenchAvcLog;
            }
            return result;
        """, read, finish)

    @staticmethod
    def _check_log_connection(result):
        if result.get("missing") or result.get("disconnected"):
            raise AssertionError("slaveSim 报文日志连接中断，成员命令历史可能不完整")

    @staticmethod
    def _request_value(frame):
        if frame[1] == 0x06 and len(frame) == 8:
            return int.from_bytes(frame[4:6], "big")
        if frame[1] == 0x10 and len(frame) == 11 and frame[4:7] == b"\x00\x01\x02":
            return int.from_bytes(frame[7:9], "big")
        raise AssertionError(f"AVC 成员写请求长度或数量不符：{frame.hex(' ')}")

    @staticmethod
    def _timestamp(event):
        timestamp = datetime.fromisoformat(event["time"])
        if timestamp.utcoffset() is None:
            raise AssertionError(f"slaveSim 报文时间缺少时区：{event!r}")
        return timestamp.timestamp()

    def _record_command(self, event):
        frame = bytes.fromhex(event["frame"])
        assert frame[0] == self.STATION, f"AVC 报文站号不符：{event!r}"
        pending = self._writes[-1] if self._writes and self._writes[-1]["status"] == "pending" else None
        if event["direction"] == "接收":
            if pending is not None:
                pending["status"] = "no_response"
            if int.from_bytes(frame[2:4], "big") != self.COMMAND:
                return
            self._writes.append({"sequence": len(self._writes) + 1, "value": self._request_value(frame),
                                 "request_at": self._timestamp(event), "response_at": None,
                                 "request": event, "response": None, "status": "pending"})
            return
        if event["direction"] != "发送" or pending is None:
            return
        request = bytes.fromhex(pending["request"]["frame"])
        pending.update(response=event, response_at=self._timestamp(event))
        if len(frame) == 5 and frame[1] == (request[1] | 0x80):
            pending["status"] = "exception"
        elif len(frame) == 8 and frame[:6] == request[:6]:
            pending["status"] = "acknowledged"
        else:
            pending["status"] = "mismatched_response"

    def _collect_commands(self, *, finish=False):
        if not self._observing_commands:
            return
        result = self._browser_events(finish=finish)
        if finish:
            self._observing_commands = False
        if result["events"] or result.get("missing") or result.get("disconnected") or finish:
            path = self.desktop.artifacts / "slavesim-avc-command-history.json"
            self.desktop.metadata["AVC成员命令报文"] = path.name
            self._command_events.extend(result["events"])
            try:
                for event in result["events"]:
                    self._record_command(event)
            finally:
                path.write_text(json.dumps({"commands": self._writes, "events": self._command_events,
                                            "stream_missing": result.get("missing", False),
                                            "stream_disconnected": result.get("disconnected", False)},
                                           ensure_ascii=False, indent=2), encoding="utf-8")
        self._check_log_connection(result)

    def command_history(self):
        """返回所有实际写请求及已观察的响应，重复值保留独立序号。"""
        self._collect_commands()
        return [dict(command) for command in self._writes]

    def observation_clock(self):
        return self.driver.execute_script("return (performance.timeOrigin + performance.now()) / 1000")

    def wait_command(self, value=None, *, after=0, timeout=None):
        """等待 after 序号之后首条写请求完成，核对其应答和设定值。"""
        def first_completed(_):
            commands = self.command_history()
            if len(commands) <= after or commands[after]["status"] == "pending":
                return False
            command = commands[after]
            assert command["status"] == "acknowledged", f"AVC 成员写命令未收到正常响应：{command!r}"
            if value is not None:
                assert command["value"] == value, f"AVC 成员设定应为 {value}，实际 {command!r}"
            return command

        return self._wait(first_completed, "slaveSim 未收到下一条 AVC 成员写命令及应答", timeout=timeout)

    def clear_all_buses(self):
        """先保存已收到的报文，再按用例约定清空并回读总线。"""
        try:
            self._collect_commands(finish=True)
        finally:
            super().clear_all_buses()
