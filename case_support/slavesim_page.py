"""slaveSim 独立浏览器页面操作，仅使用可见配置与运行反馈判定业务。"""
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import logging
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from uuid import uuid4

from selenium import webdriver
from selenium.common.exceptions import NoAlertPresentException, StaleElementReferenceException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.edge.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support.ui import Select

from testbench.model import validate_slavesim_url
from testbench.workspace import local_config


LOG = logging.getLogger(__name__)


def _literal(value):
    """生成 XPath 字面量，允许总线名包含引号。"""
    value = str(value)
    if "'" not in value:
        return "'" + value + "'"
    if '"' not in value:
        return '"' + value + '"'
    return "concat(" + ', "\'", '.join("'" + part + "'" for part in value.split("'")) + ")"


class SlaveSimPage:
    """管理本例浏览器及模拟资源，按模拟器保存能力保护无关设备。"""

    def __init__(self, desktop, url, *, browser=None):
        self.desktop = desktop
        self.url = validate_slavesim_url(url, required=True)
        self.browser = browser
        self.driver = None
        self._verified = set()
        self._restoring = False
        self._snapshots = 0

    def __enter__(self):
        try:
            with self._step("打开独立管理页面"):
                browser = self.browser
                if browser is None:
                    raise RuntimeError("slaveSim 启动前缺少已匹配的系统 Edge 驱动")
                self.desktop.metadata["slaveSim浏览器版本"] = browser.browser_version
                self.desktop.metadata["slaveSim驱动版本"] = browser.driver_version
                self.desktop.metadata["slaveSim驱动路径"] = str(browser.driver)
                profile = self.desktop.artifacts / "slavesim-edge-profile"
                profile.mkdir(parents=True, exist_ok=True)
                options = webdriver.EdgeOptions()
                options.set_capability("unhandledPromptBehavior", "ignore")
                options.binary_location = str(browser.binary)
                options.add_argument(f"--user-data-dir={profile.resolve()}")
                options.add_argument("--no-first-run")
                options.add_argument("--window-size=1600,1000")
                service = Service(executable_path=str(browser.driver),
                                  service_args=["--log-level=OFF"],
                                  log_output=str(self.desktop.artifacts / "slavesim-webdriver.log"))
                self.driver = webdriver.Edge(service=service, options=options)
                self.driver.set_page_load_timeout(self.desktop.timeout)
                self.driver.set_script_timeout(self.desktop.timeout)
                self.driver.get(self.url)
                self._wait(lambda _: self.driver.find_element(By.ID, "show-runtime").is_displayed(),
                           "slaveSim 页面未显示运行入口")
                # 只观察页面已有的刷新事件；业务数值仍从可见表格读取。
                self.driver.execute_script("""
                    window.__testbenchSim = {tick: 0, failed: false};
                    window.addEventListener('sim-state', () => {
                        window.__testbenchSim.tick++; window.__testbenchSim.failed = false;
                    });
                    window.addEventListener('sim-state-error', () => {window.__testbenchSim.failed = true;});
                """)
                self._wait(lambda _: self._tick() > 0, "slaveSim 未出现新的运行状态刷新")
                self.desktop.metadata["slaveSim管理页"] = self.url
                self.desktop.metadata["slaveSim浏览器版本"] = self.driver.capabilities.get("browserVersion")
            return self
        except BaseException:
            try:
                self.close()
            except Exception as error:
                self.desktop.metadata["slaveSim浏览器清理错误"] = str(error)
                LOG.exception("slaveSim 初始化失败后浏览器清理异常，保留原始错误")
            raise

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            self.close()
        except Exception as error:
            self.desktop.cleanup_failure = f"slaveSim 浏览器清理失败：{error}"
            self.desktop.failure = self.desktop.failure or self.desktop.cleanup_failure
            self.desktop.metadata["slaveSim浏览器清理错误"] = str(error)
            if exc_value is None:
                raise RuntimeError(self.desktop.cleanup_failure) from error

    def close(self):
        """仅退出本对象创建的浏览器会话。"""
        driver, self.driver = self.driver, None
        if driver is not None:
            driver.quit()

    def capture(self, name, *, expected=None, actual=None, description=None):
        """失败后在清理之前留存当前页面，不切换总线或重新打开窗口。"""
        entries = self.desktop._active_steps or self.desktop.steps
        entry = dict(entries[-1] if entries else {"序号": 0})
        entry["步骤"] = f"[slaveSim] {description or '保留现场 ' + Path(name).name}"
        if expected is not None:
            entry["预期"] = expected
        if actual is not None:
            entry["实际"] = actual
        failed = self.desktop.failure or entry.get("状态") == "失败" or entry.get("错误")
        self._capture(entry, "failed" if failed else "unknown", required=True)

    def _wait(self, predicate, message, *, timeout=None):
        def condition(driver):
            if not self._restoring:
                self.desktop._check_stop()
            try:
                return predicate(driver)
            except StaleElementReferenceException:
                return False
        wait = self.desktop.timeout if timeout is None else timeout
        return WebDriverWait(self.driver, wait, poll_frequency=0.2).until(condition, message)

    def _tick(self):
        state = self.driver.execute_script("return window.__testbenchSim")
        if not state or state["failed"]:
            return -1
        return state["tick"]

    @contextmanager
    def _step(self, name, expected=None):
        with self.desktop.step(f"[slaveSim] {name}", expected=expected, cancellable=not self._restoring):
            entry = self.desktop._active_steps[-1]
            entry["来源"] = "slaveSim"
            LOG.info("slaveSim 页面步骤：%s", name)
            try:
                yield entry
            except BaseException as error:
                entry["错误"] = str(error)
                self._capture(entry, "failed", required=False)
                raise
            else:
                self._capture(entry, "passed", required=not self._restoring)

    def _capture(self, entry, status, *, required):
        stem = f"slavesim-step-{entry['序号']:04d}-{status}"
        target = self.desktop.artifacts / f"{stem}.png"
        serial = 2
        known = {shot["name"] for shot in self.desktop.screenshots}
        while target.exists() or target.name in known:
            target = self.desktop.artifacts / f"{stem}-{serial}.png"
            serial += 1
        shot = {"name": target.name, "description": entry["步骤"], "source": "slavesim",
                "step_id": entry["序号"], "expected": entry.get("预期", "查看模拟器实际页面"),
                "actual": entry.get("实际", entry.get("错误", "已执行页面操作")),
                "status": status, "capture_status": "missing", "error": "",
                "time": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                "automatic": True, "annotated_name": "", "annotation_status": "disabled",
                "annotation_error": "", "annotations": []}
        self.desktop.screenshots.append(shot)
        try:
            if self.driver is None:
                raise RuntimeError("模拟器浏览器未建立，无法保存页面现场")
            if not self.driver.save_screenshot(str(target)) or not target.is_file() or target.stat().st_size == 0:
                raise RuntimeError("模拟器浏览器未保存有效截图")
            shot["capture_status"] = "saved"
        except Exception as error:
            shot["error"] = f"slaveSim 截图缺失：{error}"
            LOG.warning("%s", shot["error"])
            if required:
                raise RuntimeError(shot["error"]) from error

    @staticmethod
    def _open(details):
        if details.get_attribute("open") is None:
            details.find_element(By.XPATH, "./summary").click()

    @staticmethod
    def _field(root, label):
        return root.find_element(By.XPATH, ".//label[span[normalize-space(.)=" + _literal(label) + "]]/*[self::input or self::select]")

    @staticmethod
    def _unique(items, description):
        if len(items) != 1:
            raise AssertionError(f"{description}必须唯一，实际找到 {len(items)} 项")
        return items[0]

    @staticmethod
    def _key(bus, station_id, function, address):
        if function != "0x03":
            raise ValueError("首批 slaveSim 页面用例只支持 0x03 保持寄存器")
        if not isinstance(bus, str) or not bus or type(station_id) is not int or not 1 <= station_id <= 247:
            raise ValueError("模拟器总线与站号无效")
        if type(address) is not int or not 0 <= address <= 65535:
            raise ValueError("模拟器协议地址必须在 0～65535 范围内")
        return bus, station_id, function, address

    @staticmethod
    def _replace(element, value):
        element.click()
        element.send_keys(Keys.CONTROL, "a", Keys.NULL, Keys.BACKSPACE)
        element.send_keys(str(value), Keys.TAB)

    def _select_register_option(self, register, label, value):
        control = self._field(register, label)
        options = [option.get_property("value") for option in Select(control).options]
        if value not in options:
            raise AssertionError(f"slaveSim 寄存器「{label}」缺少选项 {value!r}")
        selected = control.get_property("selectedIndex")
        if selected < 0:
            raise AssertionError(f"slaveSim 寄存器「{label}」没有当前选项")
        offset = options.index(value) - selected
        if offset:
            # 通过原生键盘选择触发配置草稿依赖的 input 事件。
            arrow = Keys.ARROW_DOWN if offset > 0 else Keys.ARROW_UP
            control.send_keys(arrow * abs(offset), Keys.TAB)
        actual = control.get_property("value")
        if actual != value:
            raise AssertionError(f"slaveSim 寄存器「{label}」选择失败，预期 {value!r}，实际 {actual!r}")

    def _config_bus(self, name):
        return self._unique([node for node in self.driver.find_elements(By.CSS_SELECTOR, "#config-buses > details.bus-editor")
                             if self._field(node, "总线名称").get_attribute("value") == name], "目标配置总线")

    def _configuration_snapshot(self, label):
        """只读取页面配置控件，不访问隐藏草稿或业务接口。"""
        buses = self.driver.execute_script("""
            const fields = root => Object.fromEntries(Array.from(root.querySelectorAll('label.config-field'), label => {
                const input = label.querySelector('input,select');
                return [label.querySelector('span').textContent.trim(), input?.type === 'checkbox' ? input.checked : input?.value];
            }));
            return Array.from(document.querySelectorAll('#config-buses > details.bus-editor'), bus => {
                const f = fields(bus.querySelector(':scope > .bus-fields'));
                return {name: f['总线名称'], link: f['串口软链接路径'], slaves:
                    Array.from(bus.querySelectorAll(':scope > details.slave-editor'), slave => {
                        const sf = fields(slave.querySelector(':scope > .config-grid'));
                        const model = slave.querySelector(':scope > .model-editor');
                        return {station_id: Number(sf['站号（1～247）']), fields: sf,
                            model: model ? (model.textContent.includes('当前页面不支持模型') ? '未知模型' : 'huawei_v3') : '',
                            model_fields: model ? fields(model) : {},
                            registers: Array.from(slave.querySelectorAll(':scope > details.register-group > details.register-editor'), reg => {
                                const rf = fields(reg);
                                return {function: rf['功能码'], address: Number(rf['协议地址']), type: rf['数据类型'],
                                    source: rf['数值来源'], byte_order: rf['字节序'], word_order: rf['32 位字序'],
                                    access: rf['访问权限'], value: rf['原始值'], fields: rf};
                            })};
                    })};
            });
        """)
        from .slavesim_runtime import validate_topology
        validate_topology(buses)
        self._snapshot({"类型": "模拟器实际配置", "阶段": label, "buses": buses})
        self.desktop.metadata.setdefault("slaveSim配置读取", []).append({"阶段": label, "总线数": len(buses)})
        return buses

    def _read_configuration(self, label):
        with self._step(f"读取slaveSim当前配置：{label}"):
            self.driver.find_element(By.ID, "show-config").click()
            self._wait(lambda _: self.driver.find_element(By.ID, "config-fields").is_enabled(), "配置读取尚未完成")
            self.driver.find_element(By.ID, "reload-config").click()
            if self._restoring:
                try:
                    alert = self.driver.switch_to.alert
                except NoAlertPresentException:
                    pass
                else:
                    if alert.text != "重新读取会丢弃尚未保存的配置修改，是否继续？":
                        raise RuntimeError(f"slaveSim 清理时出现未预期的对话框：{alert.text}")
                    alert.accept()
            self._wait(lambda _: (self.driver.find_element(By.ID, "config-fields").is_enabled()
                                  and self.driver.find_element(By.ID, "config-status").text == "已读取当前配置"),
                       "未读取slaveSim当前实际配置")
            return self._configuration_snapshot(label)

    def _prepare_empty_configuration(self, label):
        before = self._read_configuration(label)
        if before:
            if not self._restoring:
                self._remember_serial_directory(PurePosixPath(before[0]["link"]).parent)
            with self._step("从配置草稿删除全部 slaveSim 总线"):
                self.driver.find_element(By.ID, "delete-all-buses").click()
                self._wait(lambda _: not self.driver.find_elements(By.CSS_SELECTOR, "#config-buses > details.bus-editor"),
                           "删除全部总线后配置草稿仍存在总线")
        return before

    def _serial_directory_file(self):
        endpoint = sha256(self.url.rstrip("/").encode("utf-8")).hexdigest()
        return local_config("slavesim-serial-directories") / f"{endpoint}.txt"

    def _remember_serial_directory(self, directory):
        target = self._serial_directory_file()
        target.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(dir=target.parent) as temporary:
            pending = Path(temporary) / target.name
            pending.write_text(str(directory) + "\n", encoding="utf-8")
            pending.replace(target)

    def _serial_directory(self, default_link):
        try:
            stored = self._serial_directory_file().read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return PurePosixPath(default_link).parent
        directory = PurePosixPath(stored)
        if not directory.is_absolute() or ".." in directory.parts:
            raise ValueError("本机记录的 slaveSim 串口目录无效")
        return directory

    def _add_default_bus(self, *, replace_before):
        before = len(self.driver.find_elements(By.CSS_SELECTOR, "#config-buses > details.bus-editor"))
        self.driver.find_element(By.ID, "add-bus").click()
        self._wait(lambda _: len(self.driver.find_elements(By.CSS_SELECTOR, "#config-buses > details.bus-editor")) == before + 1,
                   "新增总线未显示")
        card = self.driver.find_elements(By.CSS_SELECTOR, "#config-buses > details.bus-editor")[-1]
        name = self._field(card, "总线名称").get_attribute("value")
        field = self._field(card, "串口软链接路径")
        # 沿用实际共享目录，每条新总线使用独立 PTY。
        directory = (PurePosixPath(replace_before[0]["link"]).parent if replace_before
                     else self._serial_directory(field.get_attribute("value")))
        link = str(directory / f"e2e-rtu-{uuid4().hex[:16]}")
        self._replace(field, link)
        return name, link

    def _assert_save_safe(self, before, owned_names=()):
        fieldset = self.driver.find_element(By.ID, "config-fields")
        preserves = fieldset.get_dom_attribute("data-preserves-unchanged-slaves") == "true"
        if not preserves and any(bus["name"] not in owned_names and bus["slaves"] for bus in before):
            raise RuntimeError("此 slaveSim 版本保存配置会重置既有设备运行状态，本例未保存配置")
        return preserves

    @staticmethod
    def _assert_unrelated_unchanged(before, after, owned_names):
        expected = {bus["name"]: bus for bus in before if bus["name"] not in owned_names}
        actual = {bus["name"]: bus for bus in after if bus["name"] not in owned_names}
        if actual != expected:
            changed = sorted(name for name in expected.keys() | actual.keys() if expected.get(name) != actual.get(name))
            raise AssertionError("无关 slaveSim 总线配置发生变化：" + "、".join(changed))

    def _save_configuration(self, label, *, before, owned_names, legacy_owned_names=None, removed_names=()):
        remaining_before = [bus for bus in before if bus["name"] not in removed_names]
        with self._step(f"保存并应用slaveSim必要配置：{label}"):
            legacy_owned_names = owned_names if legacy_owned_names is None else legacy_owned_names
            preserves = self._assert_save_safe(remaining_before, legacy_owned_names)
            draft = self._configuration_snapshot(label + "保存前草稿")
            self._assert_unrelated_unchanged(remaining_before, draft, owned_names)
            self.driver.find_element(By.ID, "save-config").click()
            def saved(_):
                node = self.driver.find_element(By.ID, "config-status")
                if "error" in (node.get_attribute("class") or ""):
                    raise AssertionError(f"slaveSim配置保存失败：{node.text}")
                return node.text.startswith("配置已保存并应用")
            self._wait(saved, "slaveSim配置未确认保存并应用")
            self.desktop.metadata.setdefault("slaveSim配置调整", []).append({
                "内容": label, "保留未改动从站运行状态": preserves,
                "运行影响": "按页面声明保留未改动从站" if preserves else "旧版本全量重建，仅允许影响本例资源"})
        actual = self._read_configuration(label + "保存后实际配置")
        self._assert_unrelated_unchanged(remaining_before, actual, owned_names)
        return actual

    def _add_basic_stations(self, name, station_count):
        for station in range(1, station_count + 1):
            card = self._config_bus(name)
            self._open(card)
            Select(card.find_element(By.CSS_SELECTOR, "select[name='device-type']")).select_by_value("__custom_modbus__")
            card.find_element(By.XPATH, ".//button[normalize-space(.)='添加设备']").click()
            self._wait(lambda _: len(self._config_bus(name).find_elements(By.CSS_SELECTOR, ":scope > details.slave-editor")) == station,
                       "新增自定义从站未显示")
            slave = self._config_bus(name).find_elements(By.CSS_SELECTOR, ":scope > details.slave-editor")[-1]
            self._open(slave)
            group = slave.find_element(By.CSS_SELECTOR, ":scope > details.register-group")
            self._open(group)
            reg = self._unique(group.find_elements(By.CSS_SELECTOR, ":scope > details.register-editor"), "新增从站默认寄存器")
            self._open(reg)
            self._select_register_option(reg, "访问权限", "readwrite")
            self._select_register_option(reg, "数值来源", "manual")

    def prepare_basic(self, environment, *, station_count=2, check_paths=None):
        """清空现有总线并通过页面新建本例总线及手动点。"""
        from testbench.modbus_rtu_config import RtuRuntime, RtuStation
        from .slavesim_runtime import basic_candidate
        before = self._prepare_empty_configuration("基础采集准备前")
        removed_names = tuple(bus["name"] for bus in before)
        with self._step("按当前模拟器页面新增基础采集总线与手动点"):
            default_name, new_link = self._add_default_bus(replace_before=before)
            name = f"e2e-rtu-{uuid4().hex[:12]}"
            self._replace(self._field(self._config_bus(default_name), "总线名称"), name)
            self._add_basic_stations(name, station_count)
            if check_paths is not None:
                check_paths((new_link,))
        actual = self._save_configuration("清空原有总线并建立本例总线及手动寄存器",
                                          before=before, owned_names=(name,), removed_names=removed_names)
        rebuilt = self._unique(actual, "重建后 slaveSim 总线")
        if (rebuilt["name"], rebuilt["link"]) != (name, new_link):
            raise AssertionError("重建后 slaveSim 总线名称或串口路径与本例不一致")
        slaves = rebuilt["slaves"]
        if len(slaves) != station_count or any(len(slave["registers"]) != 1 for slave in slaves):
            raise AssertionError("重建后 slaveSim 从站数量或寄存器数量与本例不一致")
        selected = basic_candidate(actual, station_count)
        if selected is None:
            raise AssertionError("新增资源保存后未形成预期的 manual UINT16 AB 读写测试点")
        bus, stations = selected
        runtime = {"bus": bus["name"], "serial_device": bus["link"], "stations": stations}
        config = RtuRuntime(bus["name"], bus["link"], tuple(RtuStation(**station) for station in stations),
                            float(environment.get("modbus_acquisition_timeout", 300)))
        self.desktop.metadata["RTU运行时资源"] = runtime
        self.desktop.metadata["RTU串口参数来源"] = "slaveSim采用PTY，无UART参数；DSP使用9600/8N1，路径是否可访问由真实连接及采集结果判定"
        self.driver.find_element(By.ID, "show-runtime").click()
        for station in config.stations:
            observed = self.observe(config.bus, station.station_id, "0x03", station.address)
            if observed["link_path"] != config.serial_device:
                raise AssertionError("slaveSim运行路径与实际保存配置不一致，未猜测宿主机路径")
        return config

    def clear_all_buses(self):
        """用例结束时清空全部总线并核对保存结果，任务已停止时仍执行。"""
        self._restoring = True
        try:
            before = self._prepare_empty_configuration("用例结束清空前")
            if before:
                names = tuple(bus["name"] for bus in before)
                self._save_configuration("用例结束清空全部总线", before=before, owned_names=(), removed_names=names)
            self.desktop.metadata["slaveSim总线清理"] = {"清理前总线数": len(before), "清理后总线数": 0}
        finally:
            self._restoring = False

    def _verify_manual(self, key):
        """只读配置页，确认测试点允许手动设值。"""
        if key in self._verified:
            return
        bus, station, function, address = key
        self.driver.find_element(By.ID, "show-config").click()
        self._wait(lambda _: self.driver.find_element(By.ID, "config-status").text == "已读取当前配置",
                   "模拟器配置页未成功读取当前配置")
        buses = self.driver.find_elements(By.CSS_SELECTOR, "#config-buses > details.bus-editor")
        card = self._unique([node for node in buses if self._field(node, "总线名称").get_attribute("value") == bus], "模拟器配置总线")
        self._open(card)
        stations = card.find_elements(By.CSS_SELECTOR, ":scope > details.slave-editor")
        slave = self._unique([node for node in stations if self._field(node, "站号（1～247）").get_attribute("value") == str(station)], "模拟器配置从站")
        self._open(slave)
        title = slave.find_element(By.CSS_SELECTOR, ":scope > summary .editor-title").text
        if "自定义 Modbus 设备" not in title:
            raise AssertionError("RTU 测试点必须属于无行为模型的专用自定义设备")
        group = slave.find_element(By.CSS_SELECTOR, ":scope > details.register-group")
        self._open(group)
        registers = group.find_elements(By.CSS_SELECTOR, ":scope > details.register-editor")
        reg = self._unique([node for node in registers
                            if self._field(node, "功能码").get_attribute("value") == function
                            and self._field(node, "协议地址").get_attribute("value") == str(address)], "模拟器配置寄存器")
        self._open(reg)
        for label, expected in (("数值来源", "manual"), ("数据类型", "UINT16"),
                                ("字节序", "AB"), ("访问权限", "readwrite")):
            actual = self._field(reg, label).get_attribute("value")
            if actual != expected:
                raise AssertionError(f"模拟器 {label}：预期 {expected}，实际 {actual}；未修改配置")
        self._verified.add(key)
        self.driver.find_element(By.ID, "show-runtime").click()

    def _row(self, key):
        bus, station, function, address = key
        buttons = self.driver.find_elements(By.XPATH, "//div[@id='buses']//button[contains(@class,'bus-choice')][strong[normalize-space(.)=" + _literal(bus) + "]]")
        button = self._unique(buttons, "模拟器运行总线")
        if button.get_attribute("aria-pressed") != "true":
            button.click()
        bus_key = button.get_attribute("data-bus-key")
        workspace = self.driver.find_element(By.XPATH, "//section[contains(@class,'bus-workspace')][@data-bus-key=" + _literal(bus_key) + "]")
        if "连接中断" in workspace.find_element(By.CSS_SELECTOR, ".bus-summary").text:
            raise AssertionError("slaveSim 连接中断，页面反馈可能过期")
        device = workspace.find_element(By.CSS_SELECTOR, f"section.runtime-device[data-device-id='{station}']")
        group = device.find_element(By.CSS_SELECTOR, "details.runtime-registers")
        self._open(group)
        rows = group.find_elements(By.XPATH, ".//tbody/tr[td[1][normalize-space(.)=" + _literal(function)
                                   + "] and td[2][normalize-space(.)=" + _literal(address) + "]]")
        row = self._unique(rows, "模拟器运行寄存器")
        # 只调整可见区域，截图应包含被读取的寄存器。
        self.driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", row)
        return workspace, device, row

    def _read(self, key):
        if self._tick() < 0:
            raise AssertionError("slaveSim 状态刷新失败，拒绝使用过期页面值")
        workspace, _, row = self._row(key)
        cells = row.find_elements(By.TAG_NAME, "td")
        if cells[2].text != "UINT16":
            raise AssertionError("模拟器运行类型不是 UINT16")
        raw = cells[5].text.strip()
        try:
            encoded = bytes.fromhex(raw)
            value = Decimal(cells[4].text.strip().split()[0])
        except (ValueError, InvalidOperation, IndexError):
            raise AssertionError("模拟器当前值或原始字节格式无效") from None
        if len(encoded) != 2 or not value.is_finite() or value != int.from_bytes(encoded, "big"):
            raise AssertionError(f"模拟器当前值与 UINT16 AB 原始字节不一致：{cells[4].text} / {raw}")
        paths = workspace.find_elements(By.CSS_SELECTOR, ".bus-path-value")
        return {"bus": key[0], "station_id": key[1], "function": key[2], "address": key[3],
                "type": cells[2].text, "tag": cells[3].text, "value": int(value), "raw": raw,
                "link_path": paths[1].text, "observed_at": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                "refresh_tick": self._tick()}

    def _snapshot(self, result):
        self._snapshots += 1
        path = self.desktop.artifacts / f"slavesim-observation-{self._snapshots:04d}.json"
        path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        self.desktop.metadata.setdefault("slaveSim页面观察", []).append(path.name)

    def observe(self, bus, station_id, function, address, *, verify_manual=True):
        key = self._key(bus, station_id, function, address)
        with self._step(f"读取总线 {bus} / 站号 {station_id} / 地址 {address} 实际反馈") as entry:
            if verify_manual:
                self._verify_manual(key)
            before = self._tick()
            self._wait(lambda _: self._tick() > max(before, 0), "slaveSim 没有新的状态反馈，拒绝使用旧页面值")
            result = self._read(key)
            self._snapshot(result)
            entry["实际"] = json.dumps(result, ensure_ascii=False)
            return result

    def set_value(self, bus, station_id, function, address, value):
        key = self._key(bus, station_id, function, address)
        if type(value) is not int or not 0 <= value <= 65535:
            raise ValueError("UINT16 测试值必须为 0～65535 的整数")
        self.observe(*key)
        with self._step(f"设置总线 {bus} / 站号 {station_id} / 地址 {address}", expected=value) as entry:
            _, _, row = self._row(key)
            input_box = row.find_element(By.CSS_SELECTOR, "input[type=text]")
            input_box.click()
            input_box.send_keys(Keys.CONTROL, "a", Keys.NULL, Keys.BACKSPACE)
            input_box.send_keys(str(value), Keys.TAB)
            before = self._tick()
            row.find_element(By.CSS_SELECTOR, "button.primary").click()
            def confirmed(_):
                _, current_device, _ = self._row(key)
                message = current_device.find_element(By.CSS_SELECTOR, ".register-message").text
                if "失败" in message or "请输入" in message:
                    raise AssertionError(f"slaveSim 设值失败：{message}")
                return message.startswith("已设置") and self._tick() > max(before, 0) and self._read(key)["value"] == value
            self._wait(confirmed, "slaveSim 手动设值未在时限内得到页面实际值确认")
            result = self._read(key)
            self._snapshot(result)
            entry["实际"] = json.dumps(result, ensure_ascii=False)
            return result
