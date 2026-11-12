"""通过 slaveSim 可见界面创建华为行为机群并读取协议原始反馈。"""
from dataclasses import replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
import json
import logging

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import Select

from .huawei_fleet import FLEET_SIZE_LABEL, FLEET_STATIONS, HuaweiFleetConfig, build_fleet
from .slavesim_page import SlaveSimPage


LOG = logging.getLogger(__name__)
SCENARIO_LABELS = {"dc_available_kw": "太阳可用直流功率（kW）", "grid_line_voltage_v": "电网线电压（V）",
                   "grid_frequency_hz": "电网频率（Hz）", "ambient_temperature_c": "环境温度（℃）",
                   "efficiency": "转换效率（0～1，不含0）"}


def decode_register(kind, raw, displayed):
    """支持模板 HL/AB 编码，独立交叉检查显示值和真实原始字节。"""
    sizes = {"UINT16": 2, "INT16": 2, "UINT32": 4, "INT32": 4}
    try:
        encoded = bytes.fromhex(raw)
        number = Decimal(str(displayed).strip().split()[0])
        if kind not in sizes or len(encoded) != sizes[kind] or not number.is_finite():
            raise ValueError()
        actual = int.from_bytes(encoded, "big", signed=kind.startswith("INT"))
        if number != actual:
            raise ValueError()
        return actual
    except (ValueError, InvalidOperation, IndexError):
        raise AssertionError(f"华为寄存器显示值与 {kind} HL/AB 原始字节不一致：{displayed} / {raw}") from None


def validate_fleet(buses, *, require_paths=True):
    if not isinstance(buses, list) or len(buses) != len(FLEET_STATIONS):
        raise ValueError(f"华为机群必须恰好有 {len(FLEET_STATIONS)} 路 RS485")
    names, paths = set(), set()
    for index, (bus, stations) in enumerate(zip(buses, FLEET_STATIONS, strict=True), 1):
        name, path = bus.get("name"), bus.get("serial_device")
        if not isinstance(name, str) or not name.strip() or name in names:
            raise ValueError("华为总线名称必须非空且唯一")
        if require_paths and (not isinstance(path, str) or not path.startswith("/") or path in paths):
            raise ValueError("华为总线串口路径必须为唯一绝对路径")
        names.add(name); paths.add(path)
        devices = bus.get("devices", [])
        if (any(type(device.get("station_id")) is not int for device in devices)
                or tuple(device.get("station_id") for device in devices) != stations):
            raise ValueError(f"第 {index} 路站号必须为 {list(stations)}，不得缺站或重复")


class HuaweiSlaveSimPage(SlaveSimPage):
    """在独立浏览器中创建本例华为机群并观察实际反馈。"""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            self.close()
        except Exception as error:
            self.desktop.cleanup_failure = f"华为模拟器浏览器关闭失败：{error}"
            self.desktop.failure = self.desktop.failure or self.desktop.cleanup_failure
            self.desktop.metadata["华为模拟器浏览器关闭错误"] = str(error)
            if exc_value is None:
                raise

    @staticmethod
    def _replace(element, value):
        element.click()
        element.send_keys(Keys.CONTROL, "a", Keys.NULL, Keys.BACKSPACE)
        element.send_keys(str(value), Keys.TAB)

    def _config_bus(self, name):
        return self._unique([card for card in self.driver.find_elements(By.CSS_SELECTOR, "#config-buses > details.bus-editor")
                             if self._field(card, "总线名称").get_attribute("value") == name], "目标配置总线")

    def _config_station(self, name, station):
        return self._unique([node for node in self._config_bus(name).find_elements(By.CSS_SELECTOR, ":scope > details.slave-editor")
                             if self._field(node, "站号（1～247）").get_attribute("value") == str(station)], "目标配置从站")

    def prepare_owned_fleet(self, timeout, run_id, *, check_paths):
        config = HuaweiFleetConfig(serial_devices=("",) * len(FLEET_STATIONS), acquisition_timeout=timeout)
        buses = build_fleet(config, run_id)
        actual = self.create_fleet(buses)
        saved = {bus["name"]: bus for bus in actual}
        for bus in buses:
            observed = saved[bus["name"]]
            actual_device, link_path = self.observe_bus_paths(bus["name"])
            if link_path != observed["link"]:
                raise AssertionError(f"{bus['name']} 固定配置路径预期 {observed['link']}，实际 {link_path}")
            bus["serial_device"] = actual_device
            for device, slave in zip(bus["devices"], sorted(observed["slaves"], key=lambda row: row["station_id"]), strict=True):
                device["station_id"] = slave["station_id"]
        validate_fleet(buses)
        serial_devices = tuple(bus["serial_device"] for bus in buses)
        self.desktop.metadata["RTU串口参数来源"] = "DSP 使用 slaveSim 运行页的实际设备和 9600/8N1"
        check_paths(serial_devices)
        return replace(config, serial_devices=serial_devices), buses

    def create_fleet(self, buses):
        validate_fleet(buses, require_paths=False)
        names = {bus["name"] for bus in buses}
        before = self._prepare_empty_configuration("本例机群创建前")
        for bus in buses:
            with self._step(f"新增华为总线 {bus['name']}，共{len(bus['devices'])}台"):
                default_name, link = self._add_default_bus(replace_before=before)
                self._replace(self._field(self._config_bus(default_name), "总线名称"), bus["name"])
                bus["link_path"] = link
            for device in bus["devices"]:
                station = device["station_id"]
                with self._step(f"新增 {bus['name']} / 站号 {station} 华为V3行为模型"):
                    card = self._config_bus(bus["name"])
                    self._open(card)
                    select = card.find_element(By.CSS_SELECTOR, "select[name='device-type']")
                    def option_ready(_):
                        return [item for item in Select(select).options if "华为" in item.text and "行为" in item.text]
                    options = self._wait(option_ready, "没有华为行为模板选项")
                    if len(options) != 1:
                        raise AssertionError("华为行为模板选项不唯一")
                    Select(select).select_by_value(options[0].get_attribute("value"))
                    card.find_element(By.XPATH, ".//button[normalize-space(.)='添加设备']").click()
                    self._wait(lambda _: len(self._config_bus(bus["name"]).find_elements(By.CSS_SELECTOR, ":scope > details.slave-editor")) == station,
                               "华为模板设备未加入草稿")
                    node = self._config_station(bus["name"], station)
                    self._open(node)
                    if "华为逆变器" not in node.find_element(By.CSS_SELECTOR, ":scope > summary").text:
                        raise AssertionError("新增设备不是华为行为模型")
                    model = node.find_element(By.CSS_SELECTOR, ".model-editor")
                    for key, label in SCENARIO_LABELS.items():
                        if key in device:
                            self._replace(self._field(model, label), device[key])
                    disturbance = model.find_element(By.CSS_SELECTOR, "input[name='disturbance_enabled']")
                    if disturbance.is_selected():
                        disturbance.click()
                    if disturbance.is_selected():
                        raise AssertionError("未关闭华为模型轻微扰动")
            self.desktop.metadata.setdefault("华为创建意图", []).append(bus)
        with self._step(f"保存并应用 {FLEET_SIZE_LABEL} 华为模型配置"):
            draft = self._configuration_snapshot("华为机群保存前草稿")
            if {bus["name"]: bus["link"] for bus in draft} != {bus["name"]: bus["link_path"] for bus in buses}:
                raise AssertionError("模拟器草稿的总线或固定配置路径与本例不一致")
            from .slavesim_runtime import validate_fleet_capacity
            capacity = validate_fleet_capacity(draft, buses)
            self._snapshot({"类型": "华为新增机群固定工况能力校验", "设备": capacity})
            actual = self._save_configuration(f"新增 {FLEET_SIZE_LABEL} 华为模型", before=before, owned_names=names,
                                              removed_names=tuple(bus["name"] for bus in before))
            if {bus["name"]: bus for bus in actual} != {bus["name"]: bus for bus in draft}:
                raise AssertionError("模拟器保存后的实际配置与本例华为机群草稿不一致")
            self.desktop.metadata["华为已创建机群"] = buses
            LOG.info("已通过页面保存 %s 华为行为模型", FLEET_SIZE_LABEL)
            return actual

    def _device(self, bus, station):
        workspace = self._runtime_workspace(bus)
        if self._tick() < 0:
            raise AssertionError("模拟器连接中断，拒绝读取缓存反馈")
        device = workspace.find_element(By.CSS_SELECTOR, f"section.runtime-device[data-device-id='{station}']")
        if not device.find_elements(By.CSS_SELECTOR, "details.huawei-card"):
            raise AssertionError("目标运行设备没有华为行为模型")
        return workspace, device

    def observe_device(self, bus, station_id, addresses):
        with self._step(f"读取 {bus} / 站号 {station_id} 华为实际寄存器反馈") as entry:
            before = self._tick()
            self._wait(lambda _: self._tick() > max(before, 0), "华为模拟器没有新的状态刷新")
            workspace, device = self._device(bus, station_id)
            group = device.find_element(By.CSS_SELECTOR, "details.runtime-registers")
            self._open(group)
            # 一次同步读取真实 DOM，避免18个寄存器逐个驱动调用跨过页面刷新。
            visible = self.driver.execute_script("""
                return {rows: Array.from(arguments[0].querySelectorAll('tbody tr'), row =>
                    Array.from(row.querySelectorAll('td'), cell => cell.innerText)),
                    status: arguments[1].querySelector('.huawei-status').innerText};
            """, group, device)
            actual_device, link_path = self.runtime_bus_paths(workspace)
            registers = {}
            for address in addresses:
                cells = self._unique([row for row in visible["rows"] if row[0].strip() == "0x03"
                                      and row[1].strip() == str(address)], f"华为寄存器 {address}")
                kind, raw, displayed = cells[2].strip(), cells[5].strip(), cells[4].strip()
                registers[address] = {"value": decode_register(kind, raw, displayed), "raw": raw, "type": kind,
                                      "tag": cells[3].strip(), "address": int(cells[1].strip())}
            self.driver.execute_script("arguments[0].scrollIntoView({block:'start'});", device)
            result = {"bus": bus, "station_id": int(device.get_attribute("data-device-id")), "registers": registers,
                      "actual_device": actual_device, "link_path": link_path,
                      "status": visible["status"].strip(),
                      "observed_at": datetime.now().astimezone().isoformat(timespec="milliseconds"), "refresh_tick": self._tick()}
            self._snapshot(result)
            entry["实际"] = json.dumps(result, ensure_ascii=False)
            LOG.info("已读取华为实际寄存器：总线=%s，站号=%s，点数=%s", bus, station_id, len(registers))
            return result

    def _panel(self, bus, station):
        _, device = self._device(bus, station)
        panel = device.find_element(By.CSS_SELECTOR, "details.huawei-card")
        self._open(panel)
        return panel

    def _wait_applied(self, panel, message, before):
        def applied(_):
            text = message.text
            if "失败" in text:
                raise AssertionError(f"华为模型页面操作失败：{text}")
            return text.startswith("已应用") and self._tick() > max(before, 0)
        self._wait(applied, "华为页面操作没有确认应用及后续刷新")

    def apply_scenario(self, bus, station_id, **scenario):
        if set(scenario) - (set(SCENARIO_LABELS) | {"disturbance_enabled"}):
            raise ValueError("华为场景含未知参数")
        with self._step(f"应用 {bus} / 站号 {station_id} 华为场景", expected=scenario) as entry:
            panel = self._panel(bus, station_id)
            form = panel.find_element(By.CSS_SELECTOR, "form[data-form='scenario']")
            def observation():
                return {key: (form.find_element(By.CSS_SELECTOR, f"input[name='{key}']").is_selected()
                              if key == "disturbance_enabled" else
                              form.find_element(By.CSS_SELECTOR, f"input[name='{key}']").get_attribute("value"))
                        for key in scenario}
            self._snapshot({"类型": "华为场景调整前", "总线": bus, "站号": station_id, "场景": observation()})
            for key, value in scenario.items():
                if key == "disturbance_enabled":
                    if type(value) is not bool:
                        raise ValueError("轻微扰动开关必须为布尔值")
                    control = form.find_element(By.CSS_SELECTOR, "input[name='disturbance_enabled']")
                    if control.is_selected() != value:
                        control.click()
                else:
                    self._replace(form.find_element(By.CSS_SELECTOR, f"input[name='{key}']"), value)
            before = self._tick()
            form.find_element(By.CSS_SELECTOR, "button[type='submit']").click()
            self._wait_applied(panel, form.find_element(By.CSS_SELECTOR, ".huawei-message"), before)
            applied_tick = self._tick()
            self._wait(lambda _: self._tick() > applied_tick, "场景应用后未取得新的实际状态刷新")
            actual = observation()
            self._snapshot({"类型": "华为场景调整后实际反馈", "总线": bus, "站号": station_id, "场景": actual})
            for key, expected in scenario.items():
                if ((actual[key] != expected) if key == "disturbance_enabled" else
                        Decimal(str(actual[key])) != Decimal(str(expected))):
                    raise AssertionError(f"华为场景实际反馈不符：{key}预期{expected}，实际{actual[key]}")
            entry["实际"] = json.dumps(actual, ensure_ascii=False)

    def apply_control(self, bus, station_id, kind, value):
        if kind not in {"percent", "fixed", "pf", "ratio", "active_gradient", "reactive_gradient"}:
            raise ValueError("不支持的华为模型控制类型；不发送未实现的启停命令")
        with self._step(f"设置 {bus} / 站号 {station_id} 华为{kind}", expected=value) as entry:
            panel = self._panel(bus, station_id)
            control = panel.find_element(By.CSS_SELECTOR, f"input[name='{kind}']")
            self._replace(control, value)
            row = control.find_element(By.XPATH, "ancestor::div[contains(@class,'huawei-command')][1]")
            form = control.find_element(By.XPATH, "ancestor::form[1]")
            before = self._tick()
            row.find_element(By.TAG_NAME, "button").click()
            self._wait_applied(panel, form.find_element(By.CSS_SELECTOR, ".huawei-message"), before)
            entry["实际"] = "页面确认命令已应用，实际行为由后续双端数据判定"
