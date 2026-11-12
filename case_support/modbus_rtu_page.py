"""通过真实桌面界面配置 RTU 并读取采集结果，仅清理本例创建的连接。"""
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
import logging

from selenium.common.exceptions import NoSuchElementException, StaleElementReferenceException, TimeoutException
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait

from testbench.workspace import SITE_MANAGER_ADDRESS


def xpath_text(value):
    """将外部名称作为 XPath 字面量，避免名称改变定位语义。"""
    value = str(value)
    if "'" not in value:
        return "'" + value + "'"
    if '"' not in value:
        return '"' + value + '"'
    return "concat(" + ",\"'\",".join("'" + part + "'" for part in value.split("'")) + ")"


class ModbusRtuPage:
    """只使用 Selenium 可见控件，不调用被测系统内部 API。"""

    DIALOG = (By.CSS_SELECTOR, '.ant-modal[role="dialog"]')
    DIALOG_XPATH = "//div[@role='dialog' and contains(concat(' ',normalize-space(@class),' '),' ant-modal ')]"

    def __init__(self, desktop):
        self.desktop = desktop
        self.owned = {}
        self.current = None
        self.creation_started = False
        self.pending_creation = None
        self._cleaning = False
        self._stop_deferred = False

    @property
    def driver(self):
        return self.desktop.driver

    @property
    def _cancellable(self):
        return not (self._cleaning or self._stop_deferred)

    @contextmanager
    def _defer_stop(self):
        self.desktop._check_stop()
        self._stop_deferred = True
        try:
            yield
        finally:
            self._stop_deferred = False
        self.desktop._check_stop()

    def _until(self, predicate, message="", timeout=None):
        def probe(driver):
            if self._cancellable:
                self.desktop._check_stop()
            return predicate(driver)
        return WebDriverWait(self.driver, timeout or self.desktop.timeout,
                             ignored_exceptions=(NoSuchElementException, StaleElementReferenceException)).until(probe, message)

    def _visible(self, locator):
        matches = [element for element in self.driver.find_elements(*locator) if element.is_displayed()]
        if len(matches) > 1:
            raise RuntimeError(f"界面定位不唯一，拒绝操作：{locator}")
        return matches[0] if matches else None

    def _click(self, locator, name, *, before_click=None):
        def ready(_):
            element = self._visible(locator)
            if element and element.is_enabled() and element.get_attribute("aria-disabled") != "true":
                return element
            return False

        with self.desktop.step(name, cancellable=self._cancellable):
            element = self._until(ready, f"控件未就绪：{name}")
            if before_click:
                before_click()
            element.click()

    @staticmethod
    def _button(text, root=""):
        return (By.XPATH, root + "//button[translate(normalize-space(.),' ','')=" + xpath_text(text.replace(" ", "")) + "]")

    def _menu(self, label):
        locator = (By.XPATH, "//li[@role='menuitem'][.//span[normalize-space(.)=" + xpath_text(label) + "]]")
        self._click(locator, f"打开{label}页面")

    def _rtu(self):
        locator = (By.XPATH, "//li[@role='menuitem'][.//span[normalize-space(.)='Modbus RTU']]")
        if not self._visible(locator):
            self._click((By.XPATH, "//div[@role='menuitem'][.//span[normalize-space(.)='协议接入']]"), "展开协议接入菜单")
        self._click(locator, "打开 Modbus RTU 页面")
        config_tab = (By.XPATH, "//*[@aria-label='协议页面切换']//*[@role='tab'][normalize-space(.)='连接配置']")
        if self._visible(config_tab):
            self._click(config_tab, "切换 RTU 配置页签")
        self._until(lambda _: self._visible((By.CSS_SELECTOR, ".protocol-connection-list-card")), "RTU连接列表未显示")

    def open_current_links(self):
        """进入 DSP 上位机当前业务上下文，不建立下位机管理连接。"""
        with self.desktop.step("通过上位机当前业务入口打开 RTU 配置"):
            offline = (By.XPATH, "//header//*[contains(@class,'ant-tag') and normalize-space(.)='离线工作区']")
            if self._visible(offline):
                raise RuntimeError("当前上位机处于离线工作区，无法执行在线 RTU 业务验收")
            self._rtu()

    def connect_site(self):
        """只通过上位机可见界面建立本例现场直连并等待明确结果。"""
        back_online = self._button("切回在线", "//header")
        if self._visible(back_online):
            self._click(back_online, "将测试实例切回在线")
            self._until(lambda _: not self._visible(back_online), "测试实例未切回在线")
        self._menu("模块运维")
        field = (By.CSS_SELECTOR, 'input[aria-label="ModuleManager 地址"]')
        if not self._visible(field):
            self._click((By.XPATH, "//*[@aria-label='连接方式']//label[normalize-space(.)='现场直连']"), "选择现场直连")
        switch = self._button("切换至现场直连")
        if self._visible(switch):
            self._click(switch, "切换运行连接至现场直连")

        def editable(_driver):
            element = self._visible(field)
            return element if element and element.is_enabled() else False

        def address_ready(driver):
            element = editable(driver)
            return element and element.get_attribute("value") == SITE_MANAGER_ADDRESS

        with self.desktop.step("确认现场直连地址", expected=SITE_MANAGER_ADDRESS):
            element = self._until(editable, "ModuleManager 地址输入框未就绪")
            if element.get_attribute("value") != SITE_MANAGER_ADDRESS:
                self._replace(element, SITE_MANAGER_ADDRESS)
            self._until(address_ready,
                        f"ModuleManager 地址未更新为 {SITE_MANAGER_ADDRESS}")
        controls = "//div[contains(@class,'module-ops-connection-controls')]"
        self._click(self._button("连接", controls), "连接本例现场设备")

        def result(_):
            error = self._visible((By.CSS_SELECTOR, ".module-ops-page > .ant-alert-error"))
            if error:
                raise AssertionError(f"现场直连 {SITE_MANAGER_ADDRESS} 失败：{error.text}")
            return self._visible((By.XPATH, "//*[contains(@class,'ant-message-success')]//*[normalize-space(.)='ModuleManager 已连接']"))

        with self.desktop.step("确认现场直连成功", expected=SITE_MANAGER_ADDRESS):
            self._until(result, f"上位机未确认连接 {SITE_MANAGER_ADDRESS}")
        self.desktop.metadata["现场直连实际地址"] = SITE_MANAGER_ADDRESS
        return SITE_MANAGER_ADDRESS

    def _refresh_connection_list(self, protocol, alert_class):
        refresh = (By.CSS_SELECTOR, 'button[aria-label="刷新连接列表"]')

        def ready(_):
            button = self._visible(refresh)
            return button and button.is_enabled() and "ant-btn-loading" not in (button.get_attribute("class") or "")

        self._until(ready, f"{protocol} 连接列表仍在加载或暂不可刷新")
        self._click(refresh, f"刷新 {protocol} 连接列表")
        self._until(ready, f"{protocol} 连接列表刷新未完成")
        for alert in self.driver.find_elements(By.CSS_SELECTOR, f".{alert_class}"):
            if alert.is_displayed() and "连接列表刷新失败" in alert.text:
                raise RuntimeError(f"{protocol} 连接列表刷新失败：{alert.text}")

    def assert_serial_paths_available(self, paths):
        """创建连接前，只读检查目标串口是否被现有配置占用。"""
        with self.desktop.step("检查目标串口未被既有RTU连接使用"):
            self._refresh_connection_list("RTU", "modbus-page-alert")
            names = [node.text.strip() for node in self.driver.find_elements(
                By.CSS_SELECTOR, ".protocol-connection-item-main strong")]
            observed = []
            for name in names:
                self._click(self._connection_locator(name), f"只读检查既有连接 {name}")
                title = (By.XPATH, "//div[contains(@class,'modbus-connection-card')]"
                         "//div[contains(@class,'ant-card-head-title')]//span[normalize-space(.)=" + xpath_text(name) + "]")
                self._until(lambda _: self._visible(title), "既有连接详情未切换至目标连接")
                labels = [node.text.strip() for node in self.driver.find_elements(
                    By.CSS_SELECTOR, ".modbus-connection-card .ant-descriptions-item-label")]
                if "串口设备" in labels:
                    path = self._detail("串口设备")
                    observed.append({"连接": name, "串口": path})
                    if path in paths:
                        raise AssertionError(f"专用串口 {path} 已被既有连接 {name} 使用，请准备独立测试链路")
            self.desktop.metadata["RTU既有串口检查"] = observed

    @staticmethod
    def _replace(element, value):
        element.click()
        element.send_keys(Keys.CONTROL, "a", Keys.NULL, Keys.BACKSPACE)
        element.send_keys(str(value), Keys.TAB)

    def _dialog(self):
        return self._until(lambda _: self._visible(self.DIALOG), "配置窗口未打开")

    def _field(self, name):
        return self._dialog().find_element(By.CSS_SELECTOR, f'[id="{name}"]')

    def _fill(self, name, value, label):
        with self.desktop.step(f"填写{label}", expected=str(value), cancellable=not self._cleaning):
            self._replace(self._field(name), value)

    def _select_control(self, name):
        return self._field(name).find_element(
            By.XPATH, "ancestor::div[contains(concat(' ',normalize-space(@class),' '),' ant-select ')][1]")

    def _select_value(self, name):
        return self._select_control(name).find_element(
            By.CSS_SELECTOR, ".ant-select-content-has-value, .ant-select-selection-item").text.strip()

    def _select_option(self, name, label):
        def opened_list(_):
            field = self._field(name)
            return field.get_attribute("aria-expanded") == "true" and field.get_attribute("aria-controls")

        with self.desktop.step(f"设置{name}为{label}", expected=label, cancellable=not self._cleaning):
            # Ant Design 6 使用 content，旧版使用 selector。两者都是可见的点击区域。
            self._select_control(name).find_element(
                By.CSS_SELECTOR, ".ant-select-content, .ant-select-selector").click()
            list_id = self._until(opened_list, f"{name} 下拉框未打开")
            option = (By.XPATH, "//div[contains(concat(' ',normalize-space(@class),' '),' ant-select-dropdown ')]"
                      "[.//*[@id=" + xpath_text(list_id) + "]]"
                      "//div[contains(concat(' ',normalize-space(@class),' '),' ant-select-item-option ')"
                      " and not(contains(concat(' ',normalize-space(@class),' '),' ant-select-item-option-disabled '))"
                      " and not(@aria-disabled='true') and @title=" + xpath_text(label) + "]")
            self._until(lambda _: self._visible(option), f"{name} 未找到可选项：{label}").click()
            self._until(lambda _: self._select_value(name) == label, f"{name} 未选中 {label}")

    def _submit(self, text, *, before_click=None):
        self._click(self._button(text, self.DIALOG_XPATH), f"提交{text}", before_click=before_click)
        self._until(lambda _: not self._visible(self.DIALOG), f"{text}未成功，配置窗口仍然打开")

    def _connection_locator(self, name):
        return (By.XPATH, "//div[contains(@class,'protocol-connection-item-main')][.//strong[normalize-space(.)=" + xpath_text(name) + "]]")

    def _detail(self, label):
        return self.driver.find_element(By.XPATH, "//div[contains(@class,'modbus-connection-card')]//td[.//*[contains(@class,'ant-descriptions-item-label') and normalize-space(.)="
                                        + xpath_text(label) + "]]//*[contains(@class,'ant-descriptions-item-content')]").text.strip()

    def _assert_identity(self):
        if self.current not in self.owned:
            raise RuntimeError("未选择本例拥有的 RTU 连接")
        actual = self._detail("连接编号")
        if actual != self.owned[self.current]["conn_id"]:
            raise RuntimeError("RTU连接编号已变化，拒绝操作其他连接")

    def _selected_conn_id(self):
        return self.owned[self.current]["conn_id"]

    def _read_connection_identity(self, name, action):
        self._click(self._connection_locator(name), action)
        title = (By.XPATH, "//div[contains(@class,'modbus-connection-card')]"
                 "//div[contains(@class,'ant-card-head-title')]//span[normalize-space(.)=" + xpath_text(name) + "]")
        self._until(lambda _: self._visible(title), f"RTU 连接详情未切换到 {name}")

        def visible_id(_):
            conn_id = self._detail("连接编号")
            return conn_id if conn_id.isdecimal() and int(conn_id) > 0 else False

        return self._until(visible_id, f"未取得 RTU 连接 {name} 的编号")

    def select(self, name):
        if name not in self.owned:
            raise RuntimeError("拒绝选择非本例创建的连接")
        if self._read_connection_identity(name, f"选择本例 RTU 连接 {name}") != self.owned[name]["conn_id"]:
            raise RuntimeError("RTU连接编号已变化，拒绝操作其他连接")
        self.current = name

    def _record_connection_rename(self, old_name, new_name):
        connection = self.owned[old_name]
        conn_id = self._read_connection_identity(new_name, f"确认本例 RTU 连接改名为 {new_name}")
        if conn_id != connection["conn_id"]:
            raise RuntimeError(f"改名后的 RTU 连接 {new_name} 编号不符，拒绝接管")
        connection["name"] = new_name
        connection["fields"]["conn_name"] = new_name
        connection.pop("pending_name", None)
        self.owned[new_name] = self.owned.pop(old_name)
        for identity in self.desktop.metadata.get("本例RTU连接", []):
            if identity["name"] == old_name and identity["conn_id"] == conn_id:
                identity["name"] = new_name
        self.current = new_name

    def _confirm_created_connection(self):
        connection = self.pending_creation
        name = connection["name"]

        def candidate(_):
            self._assert_no_creation_conflict()
            alternatives = connection.get("alternatives")
            if alternatives:
                other_names = [button.get_attribute("aria-label").removeprefix("复制连接 ")
                               for button in self.driver.find_elements(*alternatives)]
                if other_names:
                    raise RuntimeError(f"副本候选名称不唯一或与预期不符：{name}、{'、'.join(other_names)}")
            return self._visible(self._connection_locator(name))

        self._until(candidate, f"列表未显示 RTU 连接 {name}")
        conn_id = self._read_connection_identity(name, f"读取新建 RTU 连接 {name} 的编号")
        if not candidate(self.driver):
            raise RuntimeError(f"读取编号后列表未显示 RTU 连接 {name}")
        if any(conn_id == link["conn_id"] for link in self.owned.values()):
            raise RuntimeError(f"连接编号 {conn_id} 与本例已登记连接重复")
        fields, options = connection["fields"], connection["options"]
        details = {
            "传输方式": options["transport_type"],
            "串口设备": fields["serial_device"],
            "波特率": fields["baud_rate"],
            "数据位": fields["data_bits"],
            "校验位": options["parity"],
            "停止位": options["stop_bits"],
            "读取超时": f'{fields["read_timeout_ms"]} ms',
            "轮询周期": f'{fields["poll_interval_ms"]} ms',
        }
        for label, expected in details.items():
            actual = self._detail(label)
            if actual != str(expected):
                raise RuntimeError(f"{name} 的{label}不符，预期 {expected}，实际 {actual}")
        station = (By.XPATH, "//div[contains(@class,'modbus-connection-card')]"
                   "//div[contains(@class,'ant-card-head-title')]//span[normalize-space(.)="
                   + xpath_text(f'从站 {fields["device_id"]}') + "]")
        if not self._visible(station):
            raise RuntimeError(f'{name} 的从站号不符，预期 {fields["device_id"]}')
        self.owned[name] = {key: connection[key] for key in ("name", "fields", "options", "points")}
        self.owned[name]["conn_id"] = conn_id
        self.pending_creation = None
        self.desktop.metadata.setdefault("本例RTU连接", []).append(
            {"name": name, "conn_id": conn_id, "清理状态": "待清理", **connection.get("metadata", {})})
        self.current = name
        logging.getLogger(__name__).info("已确认本例 RTU 连接：%s，编号=%s", name, conn_id)
        return {"name": name, "conn_id": conn_id}

    def _track_creation(self, connection):
        self.desktop._check_stop()
        self.pending_creation = connection

    def _creation_conflict(self, dialog):
        field_errors = (By.XPATH, dialog + "//*[contains(@class,'ant-form-item-explain-error')]")
        notices = (By.CSS_SELECTOR, ".ant-message-error")
        for locator in (field_errors, notices):
            for item in self.driver.find_elements(*locator):
                if not item.is_displayed():
                    continue
                message = item.text.strip()
                if locator == notices and not message.startswith(("保存连接失败", "复制连接失败", "操作失败")):
                    continue
                if any(word in message.lower() for word in ("已存在", "already exists", "alreadyexists", "already_exists", "duplicate")):
                    return message
        return None

    def _assert_no_creation_conflict(self):
        conflict = self.pending_creation.get("conflict") or self._creation_conflict(self.DIALOG_XPATH)
        if conflict:
            self.pending_creation["conflict"] = conflict
            raise RuntimeError(f'RTU 连接 {self.pending_creation["name"]} 创建发生冲突：{conflict}')

    def _creation_window_closed(self):
        self._assert_no_creation_conflict()
        return not self._visible(self.DIALOG)

    def create_connection(self, name, serial_device, baud_rate, data_bits, stop_bits, parity,
                          station_id, poll_interval_ms, address_base=0, read_timeout_ms=1000):
        if address_base not in (0, 1) or parity not in ("N", "O", "E"):
            raise ValueError("地址基准必须为0或1，校验位必须为N/O/E")
        with self.desktop.step("确认本例连接名称未被占用", expected="不存在同名连接", actual=name):
            self._refresh_connection_list("RTU", "modbus-page-alert")
            if self.driver.find_elements(*self._connection_locator(name)) or name in self.owned:
                raise RuntimeError(f"存在同名 RTU 连接 {name}，拒绝接管或覆盖")
        expected = dict(conn_name=name, serial_device=serial_device, baud_rate=baud_rate,
                        data_bits=data_bits, device_id=station_id, poll_interval_ms=poll_interval_ms,
                        read_timeout_ms=read_timeout_ms)
        options = dict(transport_type="本地串口", parity={"N": "无校验", "O": "奇校验", "E": "偶校验"}[parity],
                       stop_bits=str(stop_bits), address_base="0 基 (协议偏移)" if address_base == 0 else "1 基 (人类编号)")
        self.creation_started = True
        self._click(self._button("新增连接", "//div[contains(@class,'protocol-connection-list-card')]"), "新增专用 RTU 连接")
        self._select_option("transport_type", options["transport_type"])
        for field, value in expected.items():
            self._fill(field, value, field)
        for field, value in options.items():
            if field != "transport_type":
                self._select_option(field, value)
        self.desktop.metadata.setdefault("RTU创建意图", []).append({"连接名称": name, "串口": serial_device,
                                                               "站号": station_id, "说明": "仅为创建请求，连接身份待页面确认"})
        connection = {"name": name, "fields": expected, "options": options, "points": {}}
        with self._defer_stop():
            self._click(self._button("创建连接", self.DIALOG_XPATH), f"提交创建 RTU 连接 {name}",
                        before_click=lambda: self._track_creation(connection))
            self._until(lambda _: self._creation_window_closed(), f"RTU 连接 {name} 的创建窗口仍然打开")
            return self._confirm_created_connection()

    def add_point(self, tag, function=3, address=0, data_type="UINT16", scale=1, offset=0, *, deadband=0):
        self._assert_identity()
        functions = {3: "0x03 读保持寄存器", 4: "0x04 读输入寄存器", 6: "0x06 写单寄存器"}
        if function not in functions or data_type != "UINT16":
            raise ValueError("RTU 测试点仅支持 0x03/0x04/0x06 UINT16")
        self._click(self._button("添加点位", "//div[contains(@class,'protocol-point-card')]"), "新增 RTU 点位")
        self._fill("tag", tag, "Tag")
        function_label = functions[function]
        self._select_option("function", function_label)
        self._fill("address", address, "点位地址")
        self._select_option("data_type", data_type)
        self._select_option("reg_count", "1 个寄存器")
        self._fill("scale", scale, "缩放系数")
        self._fill("offset", offset, "偏移量")
        if function != 6:
            self._fill("deadband", deadband, "死区")
        self._select_option("byte_order", "AB")
        self._submit("添加点位")
        edit = (By.CSS_SELECTOR, f'button[aria-label="编辑点位 {tag}"]')
        self._until(lambda _: self._visible(edit), f"保存后点表未显示 RTU 点 {tag}")
        self.owned[self.current]["points"][tag] = {"tag": tag, "function": function_label, "address": address,
                                                     "data_type": data_type, "scale": scale, "offset": offset,
                                                     "deadband": None if function == 6 else deadband}

    def _assert_input(self, field, expected):
        actual = self._field(field).get_attribute("value")
        try:
            equal = Decimal(actual) == Decimal(str(expected))
        except InvalidOperation:
            equal = actual == str(expected)
        self.desktop.check(f"保存后{field}一致", equal, True)

    def _assert_select(self, field, expected):
        self.desktop.check(f"保存后{field}一致", self._select_value(field), expected)

    def assert_saved_configuration(self):
        name = self.current
        self._menu("数据总线")
        self.open_current_links()
        self.select(name)
        self._click(self._button("编辑配置", "//div[contains(@class,'modbus-connection-card')]"), "重新读取连接配置")
        for field, value in self.owned[name]["fields"].items():
            self._assert_input(field, value)
        for field, value in self.owned[name]["options"].items():
            self._assert_select(field, value)
        self._click(self._button("取消", self.DIALOG_XPATH), "关闭配置核对窗口")
        for tag, expected in self.owned[name]["points"].items():
            self._click((By.CSS_SELECTOR, f'button[aria-label="编辑点位 {tag}"]'), f"重新读取点位 {tag}")
            for field in ("tag", "address", "scale", "offset", "deadband"):
                if expected[field] is not None:
                    self._assert_input(field, expected[field])
            for field in ("function", "data_type"):
                self._assert_select(field, expected[field])
            self._assert_select("reg_count", "1 个寄存器")
            self._assert_select("byte_order", "AB")
            self._click(self._button("取消", self.DIALOG_XPATH), "关闭点位核对窗口")

    def start(self):
        self._assert_identity()
        self._click(self._button("启动轮询", "//div[contains(@class,'modbus-connection-card')]"), "启动本例 RTU 连接轮询")
        self._until(lambda _: "运行中" in self.driver.find_element(By.CSS_SELECTOR, ".modbus-connection-card .ant-card-head").text,
                    "连接未进入运行中")
        self._runtime_view()

    def _runtime_view(self):
        locator = (By.XPATH, "//div[contains(@class,'protocol-point-card')]//label[normalize-space(.)='运行视图']")
        choice = self._until(lambda _: self._visible(locator), "运行视图切换未显示")
        if "ant-segmented-item-selected" not in (choice.get_attribute("class") or ""):
            self._click(locator, "显示 RTU 运行点值")
        self._until(lambda _: any(cell.text.strip() == "实时值" for cell in
                                 self.driver.find_elements(By.CSS_SELECTOR, ".protocol-point-card .ant-table-thead th")),
                    "运行点表未显示实时值列")

    def observe(self, tag):
        self._assert_identity()
        # 切换连接会重新创建点表组件，默认回到配置视图。
        self._runtime_view()
        row = self.driver.find_element(By.XPATH, "//div[contains(@class,'protocol-point-card')]//tr[.//td//strong[normalize-space(.)=" + xpath_text(tag) + "]]")
        cells = row.find_elements(By.CSS_SELECTOR, "td")
        headers = [cell.text.strip() for cell in self.driver.find_elements(By.CSS_SELECTOR, ".protocol-point-card .ant-table-thead th")]
        values = {label: cell.text.strip() for label, cell in zip(headers, cells)}
        observation = {"tag": tag, "value": values["实时值"], "quality": values["质量"], "timestamp": values["时间"],
                "address": values["地址"], "function": values["功能码"], "connection": self.current,
                "conn_id": self._selected_conn_id()}
        self.desktop.metadata.setdefault("RTU最后观察", {})[f"{self.current}/{tag}"] = observation
        return observation

    def wait_value(self, tag, expected, tolerance, timeout, previous_timestamp=None):
        last = {}
        expected, tolerance = Decimal(str(expected)), Decimal(str(tolerance))
        def matches(_):
            nonlocal last
            last = self.observe(tag)
            try:
                value = Decimal(last["value"])
            except InvalidOperation:
                return False
            return (last if value.is_finite() and abs(value - expected) <= tolerance
                    and last["quality"] == "正常" and last["timestamp"] not in ("", "-")
                    and (previous_timestamp is None or last["timestamp"] != previous_timestamp) else False)
        with self.desktop.step(f"核对上位机点值 {tag}", expected=f"值={expected}，误差≤{tolerance}，质量正常且采集时间有效"):
            try:
                snapshot = self._until(matches, timeout=timeout)
            except TimeoutException:
                raise AssertionError(f"RTU点值未在{timeout}秒内满足预期；预期={expected}，容差={tolerance}，上一时间={previous_timestamp}，最后观察={last}") from None
            self.desktop.capture("rtu-value", description=f"DSP上位机点值：{tag}", expected=str(expected), actual=str(snapshot))
            logging.getLogger(__name__).info("RTU双端校验上位机观察：%s", snapshot)
            return snapshot

    def stop(self):
        self._assert_identity()
        header = self.driver.find_element(By.CSS_SELECTOR, ".modbus-connection-card .ant-card-head").text
        if "运行中" in header:
            self._click(self._button("停止轮询", "//div[contains(@class,'modbus-connection-card')]"), "停止本例 RTU 轮询")
            self._confirm("确认停止轮询？", "确认停止本例轮询")
            self._until(lambda _: "已停止" in self.driver.find_element(By.CSS_SELECTOR, ".modbus-connection-card .ant-card-head").text,
                        "本例连接未停止")

    def _confirm(self, title, step, *, clear_delete_tooltip=False):
        popup = ("//div[contains(@class,'ant-popover') and not(contains(@class,'hidden'))]"
                 "[.//*[contains(@class,'ant-popconfirm-title') and normalize-space(.)=" + xpath_text(title) + "]]")
        if clear_delete_tooltip:
            heading = (By.CSS_SELECTOR, ".protocol-connection-list-card .ant-card-head-title")
            tooltip = (By.XPATH, "//*[@role='tooltip' and normalize-space(.)='删除连接']")
            with self.desktop.step("移开删除连接的悬停提示", cancellable=not self._cleaning):
                target = self._until(lambda _: self._visible(heading), "连接列表标题未显示")
                ActionChains(self.driver).move_to_element(target).perform()
                self._until(lambda driver: not any(item.is_displayed() for item in driver.find_elements(*tooltip)),
                            "删除连接的悬停提示未消失")
        self._click_popconfirm(popup, step)

    def _click_popconfirm(self, popup, step):
        confirm = "//*[contains(@class,'ant-popconfirm-buttons')]/button[contains(@class,'ant-btn-primary')]"
        self._click((By.XPATH, popup + confirm), step)

    def _discard_rtu_dialog(self):
        dialog = (By.CSS_SELECTOR, '.modbus-config-modal[role="dialog"]')
        busy = (By.CSS_SELECTOR, '.modbus-config-modal[role="dialog"] .ant-btn-loading')
        self._until(lambda _: not self._visible(busy), "本例 RTU 配置提交未结束")
        if self.pending_creation and not self.pending_creation.get("conflict"):
            self.pending_creation["conflict"] = self._creation_conflict(self.DIALOG_XPATH)
        if self._visible(dialog):
            discard = self._button("放弃草稿", self.DIALOG_XPATH)
            close = (By.XPATH, self.DIALOG_XPATH + "//button[contains(@class,'ant-modal-close')]")
            self._click(discard if self._visible(discard) else close, "关闭本例未提交的 RTU 配置窗口")
            self._until(lambda _: not self._visible(dialog), "本例 RTU 配置窗口未关闭")

    def cleanup(self):
        """协作取消时仍清理；身份不符时停止，不删除未知连接。"""
        was_cleaning = self._cleaning
        self._cleaning = True
        errors = []
        try:
            try:
                self._discard_rtu_dialog()
            except Exception as error:
                errors.append(f"关闭本例配置窗口：{getattr(error, 'msg', None) or error}")
            if self.pending_creation:
                name = self.pending_creation["name"]
                try:
                    conflict = self.pending_creation.get("conflict")
                    if conflict:
                        raise RuntimeError(f"创建请求发生冲突，拒绝接管：{conflict}")
                    self._rtu()
                    self._refresh_connection_list("RTU", "modbus-page-alert")
                    self._confirm_created_connection()
                except Exception as error:
                    errors.append(f"{name} 无法确认创建请求及连接归属：{getattr(error, 'msg', None) or error}")
            for name in list(self.owned):
                try:
                    with self.desktop.step(f"清理本例 RTU 连接 {name}", cancellable=False):
                        self._rtu()
                        self._refresh_connection_list("RTU", "modbus-page-alert")
                        pending_name = self.owned[name].get("pending_name")
                        if (pending_name and self._visible(self._connection_locator(pending_name))
                                and self._read_connection_identity(pending_name, f"核对 RTU 改名结果 {pending_name}")
                                == self.owned[name]["conn_id"]):
                            self._record_connection_rename(name, pending_name)
                            name = pending_name
                        self.select(name)
                        self.stop()
                        self._assert_identity()
                        self._click((By.XPATH, "//button[@aria-label=" + xpath_text("删除连接 " + name) + "]"), "删除本例专用连接")
                        self._confirm(f"确认删除 {name}？", "确认删除本例专用连接", clear_delete_tooltip=True)
                        self._until(lambda _: not self.driver.find_elements(*self._connection_locator(name)), f"RTU 连接 {name} 未删除")
                        self._refresh_connection_list("RTU", "modbus-page-alert")
                        if self.driver.find_elements(*self._connection_locator(name)):
                            raise RuntimeError("刷新后仍存在同名连接，未确认删除结果")
                        for identity in self.desktop.metadata.get("本例RTU连接", []):
                            if identity["name"] == name and identity["conn_id"] == self.owned[name]["conn_id"]:
                                identity["清理状态"] = "已删除"
                        del self.owned[name]
                except Exception as error:
                    errors.append(f"{name}：{getattr(error, 'msg', None) or error}")
            if errors:
                raise RuntimeError("RTU连接清理失败，请人工检查本例资源：" + "；".join(errors))
        finally:
            self._cleaning = was_cleaning
