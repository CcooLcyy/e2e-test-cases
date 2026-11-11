"""从 slaveSim 页面准备独立的有功实测和设定寄存器。"""
from selenium.webdriver.common.by import By

from .slavesim_page import SlaveSimPage


class AgcDirectionSimulator(SlaveSimPage):
    INITIAL_KW = 40
    LAGGING_KW = 45

    def _registers(self, bus_name):
        bus = self._config_bus(bus_name)
        self._open(bus)
        station = self._unique(bus.find_elements(By.CSS_SELECTOR, ":scope > details.slave-editor"), "本例从站")
        self._open(station)
        group = station.find_element(By.CSS_SELECTOR, ":scope > details.register-group")
        self._open(group)
        return station, group.find_elements(By.CSS_SELECTOR, ":scope > details.register-editor")

    def prepare(self, name, check_paths):
        before = self._prepare_empty_configuration("AGC 场景准备前")
        draft, link = self._add_default_bus(replace_before=before)
        self._replace(self._field(self._config_bus(draft), "总线名称"), name)
        self._add_basic_stations(name, 1)
        station, _ = self._registers(name)
        station.find_element(By.XPATH, "./button[normalize-space(.)='新增寄存器']").click()
        _, registers = self._registers(name)
        assert len(registers) == 2, "本例需要分别配置实测与设定寄存器"
        for address, (register, initial) in enumerate(zip(registers, (self.INITIAL_KW, 0), strict=True)):
            self._open(register)
            self._replace(self._field(register, "协议地址"), address)
            self._select_register_option(register, "访问权限", "readwrite")
            self._select_register_option(register, "数值来源", "manual")
            self._replace(self._field(register, "原始值"), initial)

        saved = self._save_configuration("建立 AGC 有功实测与设定寄存器", before=before,
                                         owned_names=(name,), removed_names=tuple(bus["name"] for bus in before))
        bus = self._unique(saved, "本例 AGC 总线")
        assert (bus["name"], bus["link"]) == (name, link), "保存后模拟总线名称或路径变化"
        station = self._unique(bus["slaves"], "本例 AGC 从站")
        points = sorted(station["registers"], key=lambda point: point["address"])
        actual = [(p["function"], p["address"], p["type"], p["byte_order"], p["access"], p["source"], int(p["value"]))
                  for p in points]
        assert actual == [("0x03", 0, "UINT16", "AB", "readwrite", "manual", self.INITIAL_KW),
                          ("0x03", 1, "UINT16", "AB", "readwrite", "manual", 0)], actual
        station_id = station["station_id"]
        measured, setpoint = [self.observe(name, station_id, "0x03", p["address"]) for p in points]
        assert measured["link_path"] == setpoint["link_path"] == link, "运行页软链接与保存的配置不一致"
        assert measured["actual_device"] == setpoint["actual_device"], "两点没有位于同一本例串口"
        assert (measured["value"], setpoint["value"]) == (self.INITIAL_KW, 0), "模拟寄存器初值与本例场景不一致"
        check_paths((measured["actual_device"],))
        return measured, setpoint

    def wait_setpoint(self, setpoint, value, timeout):
        key = self._key(setpoint["bus"], setpoint["station_id"], setpoint["function"], setpoint["address"])
        with self._step("从 slaveSim 确认本例初始有功设定", expected=value) as entry:
            def received(_):
                actual = self._read(key)
                assert actual["actual_device"] == setpoint["actual_device"], "等待设定时模拟串口发生变化"
                entry["实际"] = str(actual)
                return actual if actual["value"] == value else False

            self._snapshot(self._wait(received, "slaveSim 未读到本例初始有功设定", timeout=timeout))

    def set_lagging_measurement(self, measured):
        actual = self.set_value(measured["bus"], measured["station_id"], measured["function"],
                                measured["address"], self.LAGGING_KW)
        assert actual["actual_device"] == measured["actual_device"], "实测工况切换后串口发生变化"
