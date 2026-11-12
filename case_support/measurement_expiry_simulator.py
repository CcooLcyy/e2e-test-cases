"""从 slaveSim 页面准备独立 RTU 总线并保留实际收发报文。"""

import json

from selenium.webdriver.common.by import By

from .slavesim_page import SlaveSimPage


class MeasurementExpirySimulator(SlaveSimPage):
    STATION = 1
    FUNCTION = "0x03"

    def __init__(self, desktop, url, *, browser=None):
        super().__init__(desktop, url, browser=browser)
        self._watching = False
        self._ready = {}
        self._events = []
        self._stream_error = None

    def _station_editor(self, name):
        bus = self._config_bus(name)
        self._open(bus)
        station = self._unique(bus.find_elements(By.CSS_SELECTOR, ":scope > details.slave-editor"), "本例 RTU 从站")
        self._open(station)
        return station

    def _add_registers(self, name, values):
        self._add_basic_stations(name, 1)
        for _ in values[1:]:
            self._station_editor(name).find_element(By.XPATH, "./button[normalize-space(.)='新增寄存器']").click()
        group = self._station_editor(name).find_element(By.CSS_SELECTOR, ":scope > details.register-group")
        self._open(group)
        registers = group.find_elements(By.CSS_SELECTOR, ":scope > details.register-editor")
        assert len(registers) == len(values), f"{name} 寄存器数量不符：{len(registers)}"
        for address, (register, value) in enumerate(zip(registers, values, strict=True)):
            self._open(register)
            self._replace(self._field(register, "协议地址"), address)
            self._select_register_option(register, "访问权限", "readwrite")
            self._select_register_option(register, "数值来源", "manual")
            self._replace(self._field(register, "原始值"), value)

    def prepare(self, name, specs, check_paths):
        """每个角色新建一条独立总线，按地址返回运行页读回的寄存器。"""
        assert specs and all(specs.values()), "本例每条模拟总线至少需要一个寄存器"
        names = {role: f"{name}-{role}" for role in specs}
        before = self._prepare_empty_configuration("本例模拟场景准备前")
        links = {}
        with self._step("新建本例 RTU 总线和手动寄存器"):
            for role, bus in names.items():
                draft, links[bus] = self._add_default_bus(replace_before=before)
                self._replace(self._field(self._config_bus(draft), "总线名称"), bus)
                self._add_registers(bus, specs[role])

        self._start_log(tuple(names.values()))
        saved = self._save_configuration("建立本例的独立总线", before=before,
                                         owned_names=links, removed_names=tuple(bus["name"] for bus in before))
        assert {bus["name"]: bus["link"] for bus in saved} == links, f"保存后的模拟总线和路径不符：{saved!r}"

        def all_buses_ready(_):
            self._collect_log()
            return links.keys() <= self._ready.keys()

        with self._step("确认各模拟串口就绪且运行页报文日志连接正常"):
            self.driver.find_element(By.ID, "show-runtime").click()
            self._wait(all_buses_ready, "slaveSim 报文日志缺少本例串口就绪事件")

        observations = {}
        devices = []
        for role, name in names.items():
            bus = self._unique([bus for bus in saved if bus["name"] == name], f"本例 {role} 总线")
            station = self._unique(bus["slaves"], f"本例 {role} 从站")
            station_id = station["station_id"]
            assert station_id == self.STATION and not station["model"], f"模拟从站配置不符：{station!r}"
            actual = sorted((p["address"], p["function"], p["type"], p["byte_order"], p["access"], p["source"],
                             int(p["value"])) for p in station["registers"])
            expected = [(address, self.FUNCTION, "UINT16", "AB", "readwrite", "manual", value)
                        for address, value in enumerate(specs[role])]
            assert actual == expected, f"{name} 保存后的寄存器不符：{actual!r}"
            points = tuple(self.observe(name, station_id, self.FUNCTION, address, verify_manual=False)
                           for address in range(len(expected)))
            assert {p["link_path"] for p in points} == {links[name]}, f"{name} 运行页的固定配置路径变化"
            assert tuple(p["value"] for p in points) == tuple(specs[role]), f"{name} 运行页的寄存器初值不符：{points!r}"
            devices.append(self._unique(list({p["actual_device"] for p in points}), f"{name} 实际串口设备"))
            observations[role] = points

        assert len(set(devices)) == len(devices), f"本例各总线应使用不同的实际串口：{devices!r}"
        self.desktop.metadata["RTU运行时资源"] = observations
        self.desktop.metadata["RTU串口参数来源"] = "DSP 使用 slaveSim 运行页的实际设备和 9600/8N1"
        self._collect_log()
        check_paths(tuple(devices))
        return observations

    def _start_log(self, buses):
        """沿用 AVC 周期用例的运行页日志观察入口，保留原始服务端事件。"""
        self._wait(lambda driver: driver.find_element(By.ID, "conn").get_property("textContent") == "已连接",
                   "slaveSim 报文日志未连接")
        self.driver.execute_script("""
            const buses = new Set(arguments[0]);
            const connection = document.getElementById('conn');
            const append = window.appendLog;
            if (typeof append !== 'function' || connection?.textContent !== '已连接' || window.__testbenchExpiryLog) {
                throw new Error('slaveSim 运行页缺少已连接的报文日志入口');
            }
            const watch = {append, connection, ready: new Map(), events: [], disconnected: false};
            watch.wrapped = event => {
                if (buses.has(event.bus)) {
                    if (event.summary?.startsWith('虚拟串口已就绪:')) watch.ready.set(event.bus, event);
                    if (event.frame) watch.events.push(event);
                }
                append(event);
            };
            watch.status = new MutationObserver(records => {
                if (connection.textContent !== '已连接' || records.some(record =>
                    Array.from(record.removedNodes).some(node => node.textContent !== '已连接'))) {
                    watch.disconnected = true;
                }
            });
            watch.status.observe(connection, {childList: true});
            window.appendLog = watch.wrapped;
            window.__testbenchExpiryLog = watch;
        """, buses)
        self._watching = True

    def _collect_log(self, *, finish=False):
        try:
            result = self.driver.execute_script("""
                const watch = window.__testbenchExpiryLog;
                if (!watch) return {missing: true, events: [], ready: {}};
                const missing = window.appendLog !== watch.wrapped || document.getElementById('conn') !== watch.connection;
                const disconnected = watch.disconnected || watch.connection.textContent !== '已连接';
                const result = {missing, disconnected, events: watch.events.splice(0), ready: Object.fromEntries(watch.ready)};
                if (arguments[0]) {
                    watch.status.disconnect();
                    if (window.appendLog === watch.wrapped) window.appendLog = watch.append;
                    delete window.__testbenchExpiryLog;
                }
                return result;
            """, finish)
            self._events.extend(result["events"])
            self._ready.update(result["ready"])
            if result["missing"] or result.get("disconnected"):
                self._stream_error = "slaveSim 报文日志连接中断或观察入口丢失，历史可能不完整"
        except Exception as error:
            self._stream_error = f"无法读取 slaveSim 运行页报文日志：{error}"
            raise
        if self._stream_error:
            raise AssertionError(self._stream_error)

    def events(self):
        """返回迄今全部本例 RTU 报文，保持运行页事件顺序和字段。"""
        self._collect_log()
        return [dict(event) for event in self._events]

    def clear_all_buses(self):
        """先留存报文和日志连接状态，再清空总线并回读。"""
        try:
            if self._watching:
                try:
                    self._collect_log(finish=True)
                finally:
                    self._watching = False
                    path = self.desktop.artifacts / "slavesim-measurement-expiry-rtu.json"
                    path.write_text(json.dumps({"ready": self._ready, "events": self._events,
                                                "stream_error": self._stream_error}, ensure_ascii=False, indent=2),
                                    encoding="utf-8")
                    self.desktop.metadata["量测过期RTU报文"] = path.name
        finally:
            super().clear_all_buses()
