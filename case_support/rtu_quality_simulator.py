"""配置并核对本例专用的 slaveSim RTU 应答源。"""

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select

from .slavesim_page import SlaveSimPage


class RtuQualitySimulator(SlaveSimPage):
    def __init__(self, desktop, url, name, *, browser=None):
        super().__init__(desktop, url, browser=browser)
        self.names = {role: f"{name}-{role}" for role in ("reference", "target")}
        self.link_paths = {}
        self.actual_devices = {}

    def _check_owned_paths(self, buses):
        for bus in buses:
            if bus["name"] in self.link_paths and bus["link"] != self.link_paths[bus["name"]]:
                raise AssertionError("本例模拟总线的路径已变化，停止配置")
        self._assert_save_safe(buses, self.link_paths)

    def _observe_actual_devices(self):
        devices = {}
        for role, name in self.names.items():
            actual_device, link_path = self.observe_bus_paths(name)
            assert link_path == self.link_paths[name], (
                f"slaveSim 总线 {name} 的固定配置路径应为 {self.link_paths[name]}，运行页显示 {link_path}")
            devices[role] = actual_device
        return devices

    def _station(self, name, address, value):
        bus = self._config_bus(name)
        self._open(bus)
        stations = bus.find_elements(By.CSS_SELECTOR, ":scope > details.slave-editor")
        if stations:
            station = self._unique(stations, "本例模拟从站")
            self._open(station)
            station.find_element(By.XPATH, "./summary/button[normalize-space(.)='删除从站']").click()
        if address is None:
            return
        bus = self._config_bus(name)
        Select(bus.find_element(By.CSS_SELECTOR, "select[name='device-type']")).select_by_value("__custom_modbus__")
        bus.find_element(By.XPATH, ".//button[normalize-space(.)='添加设备']").click()
        station = self._unique(self._config_bus(name).find_elements(By.CSS_SELECTOR, ":scope > details.slave-editor"),
                               "本例模拟从站")
        self._open(station)
        register = self._unique(station.find_elements(By.CSS_SELECTOR, "details.register-editor"), "本例模拟寄存器")
        self._open(station.find_element(By.CSS_SELECTOR, "details.register-group"))
        self._open(register)
        self._replace(self._field(register, "协议地址"), address)
        self._replace(self._field(register, "原始值"), value)

    def _assert_mode(self, buses, role, address, value):
        bus = self._unique([bus for bus in buses if bus["name"] == self.names[role]], f"本例 {role} 总线")
        assert bus["link"] == self.link_paths[bus["name"]], f"模拟总线固定配置路径变化：{bus!r}"
        if address is None:
            assert not bus["slaves"], f"静默阶段目标站仍存在：{bus!r}"
            return
        slave = self._unique(bus["slaves"], f"本例 {role} 从站")
        reg = self._unique(slave["registers"], f"本例 {role} 寄存器")
        assert (slave["station_id"], slave["fields"]["未配置地址"], reg["function"], reg["address"],
                reg["type"], reg["source"], reg["byte_order"], int(reg["value"])) == (
                    1, "exception", "0x03", address, "UINT16", "const", "AB", value), bus

    def prepare(self, check_paths):
        before = self._prepare_empty_configuration("品质场景准备前")
        for name in self.names.values():
            draft, link = self._add_default_bus(replace_before=before)
            self._replace(self._field(self._config_bus(draft), "总线名称"), name)
            self.link_paths[name] = link
        self._station(self.names["reference"], 0, 100)
        actual = self._save_configuration("重建品质验收参照及静默目标总线", before=before,
                                          owned_names=self.link_paths, removed_names=tuple(bus["name"] for bus in before))
        self._assert_mode(actual, "reference", 0, 100)
        self._assert_mode(actual, "target", None, 0)
        self.actual_devices = self._observe_actual_devices()
        assert self.actual_devices["reference"] != self.actual_devices["target"], (
            f"参照与目标总线必须使用不同的实际设备：{self.actual_devices!r}")
        check_paths(tuple(self.actual_devices.values()))
        return self.actual_devices

    def set_target(self, address, value, label):
        before = self._read_configuration(f"切换{label}前")
        self._check_owned_paths(before)
        self._station(self.names["target"], address, value)
        actual = self._save_configuration(label, before=before, owned_names=(self.names["target"],),
                                          legacy_owned_names=self.link_paths)
        self._assert_mode(actual, "target", address, value)
        self._assert_mode(actual, "reference", 0, 100)
        devices = self._observe_actual_devices()
        assert devices == self.actual_devices, (
            f"切换{label}后模拟总线的实际设备已变化，RTU 连接使用 {self.actual_devices!r}，运行页显示 {devices!r}")

    def wait_target_exchange(self, address, timeout):
        """从本页最新配置应用后的报文证明目标实际收到读请求及按场景应答。"""
        prefix = f"总线={self.names['target']} 站号=1 "
        self.driver.find_element(By.ID, "show-runtime").click()
        def exchange(_):
            rows = [row.text for row in self.driver.find_elements(By.CSS_SELECTOR, "#logs > .row")]
            applied = max((i for i, row in enumerate(rows) if "Web 配置已保存并应用" in row), default=-1)
            if applied < 0:
                return False
            requests = [row for row in rows[applied + 1:] if f"[接收] {prefix}01 03 00 00 00 01 " in row]
            if address is None:
                return next((row for row in requests if "站号 1 未配置，已丢弃" in row), False)
            if not requests:
                return False
            frame = "01 03 02 00 64 " if address == 0 else "01 83 02 "
            return next((row for row in rows[applied + 1:] if f"[发送] {prefix}{frame}" in row), False)

        with self._step("核对最新配置应用后的目标站实际 RTU 报文"):
            evidence = self._wait(exchange, "slaveSim 未显示目标站本阶段的请求或应答报文", timeout=timeout)
            self._snapshot({"类型": "目标站实际RTU报文", "目标地址配置": address, "日志": evidence})
            return evidence
