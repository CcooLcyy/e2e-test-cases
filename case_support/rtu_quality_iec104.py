"""通过 DSP 上位机正常 IEC104 页面建立本例专用的被控站链路。"""
from selenium.webdriver.common.by import By

from .modbus_rtu_page import xpath_text


class RtuQualityIec104:
    CARD = "//div[contains(@class,'iec104-connection-card')]"
    POINT_CARD = "//div[contains(@class,'protocol-point-card')]"
    LINK_DIALOG = "//div[@role='dialog' and contains(@class,'iec104-config-modal')]"
    IMPORT_DIALOG = "//div[@role='dialog' and contains(@class,'iec104-import-modal')]"

    def __init__(self, upper, name):
        self.upper = upper
        self.name = name
        self.port = None
        self.endpoint = None
        self.originator_address = None
        self.requested = False
        self.confirmed = False
        self.creation_conflict = None
        self.routes = []

    def _button(self, text, root, action):
        self.upper._click(self.upper._button(text, root), action)

    def _open(self):
        option = (By.XPATH, "//li[@role='menuitem'][.//span[normalize-space(.)='IEC104']]")
        if not self.upper._visible(option):
            self.upper._click((By.XPATH, "//div[@role='menuitem'][.//span[normalize-space(.)='协议接入']]"), "展开协议接入")
        self.upper._click(option, "打开 IEC104 协议页面")
        tab = (By.XPATH, "//*[@aria-label='协议页面切换']//*[@role='tab'][normalize-space(.)='连接配置']")
        if self.upper._visible(tab):
            self.upper._click(tab, "切换到 IEC104 连接配置")
        self.upper._until(lambda _: self.upper._visible((By.CSS_SELECTOR, ".iec104-connection-card")),
                          "IEC104 连接详情未显示")

    def _select_link(self, name):
        self.upper._click(self.upper._connection_locator(name), f"选择 IEC104 链路 {name}")
        self.upper._until(lambda _: self.upper._visible((By.XPATH, self.CARD
                          + "//span[contains(@class,'iec104-connection-name') and normalize-space(.)=" + xpath_text(name) + "]")),
                          "IEC104 链路详情未切换到目标连接")

    def _description(self, label):
        return self.upper._visible((By.XPATH, self.CARD + "//td[.//*[contains(@class,'ant-descriptions-item-label') and normalize-space(.)="
                                    + xpath_text(label) + "]]//*[contains(@class,'ant-descriptions-item-content')]"))

    def _field(self, field):
        locator = (By.XPATH, self.LINK_DIALOG + "//input[@id=" + xpath_text(field) + "]")
        return self.upper._until(lambda _: self.upper._visible(locator), f"IEC104 配置字段 {field} 未显示")

    def _fill(self, field, value):
        self.upper._replace(self._field(field), value)

    def _assert_link(self):
        for label, expected in (("本地端点", self.endpoint), ("公共地址 (CA)", "1"),
                                ("源地址 (OA)", self.originator_address)):
            field = self._description(label)
            actual = field.text.strip() if field else None
            if actual != expected:
                raise RuntimeError(f"IEC104 链路 {self.name} 的{label}不符，预期 {expected}，实际 {actual}")
        role = self._description("站点角色")
        if not role or "被控站" not in role.text:
            raise RuntimeError(f"IEC104 链路 {self.name} 的站点角色与本例被控站配置不符")

    def _track_creation(self):
        self.upper.desktop._check_stop()
        self.requested = True

    def _creation_window_closed(self):
        self.creation_conflict = self.upper._creation_conflict(self.LINK_DIALOG)
        if self.creation_conflict:
            raise RuntimeError(f"IEC104 链路 {self.name} 创建发生冲突：{self.creation_conflict}")
        return not self.upper._visible((By.XPATH, self.LINK_DIALOG))

    def create(self):
        self._open()
        self.upper._refresh_connection_list("IEC104", "iec104-page-alert")
        if self.upper.driver.find_elements(*self.upper._connection_locator(self.name)):
            raise AssertionError(f"存在同名 IEC104 链路 {self.name}，拒绝接管")
        names = [item.text.strip() for item in self.upper.driver.find_elements(By.CSS_SELECTOR, ".protocol-connection-item-main strong")]
        ports = set()
        for name in names:
            self._select_link(name)
            endpoint = self._description("本地端点")
            if endpoint:
                port = endpoint.text.strip().rpartition(":")[2]
                if port.isdecimal():
                    ports.add(int(port))
        self.port = next(port for port in range(2404, 65536) if port not in ports)
        self._button("新增连接", "//div[contains(@class,'protocol-connection-list-card')]", "新增本例 IEC104 链路")
        self._fill("conn_name", self.name)
        self._fill("local_port", self.port)
        local_ip = self._field("local_ip").get_attribute("value").strip()
        self.endpoint = f"{local_ip}:{self.port}"
        self.originator_address = self._field("oa").get_attribute("value").strip()
        time_switch = (By.XPATH, self.LINK_DIALOG + "//button[@id='point_with_time']")
        self.upper._click(time_switch, "启用 IEC104 带时标上送")
        self.upper._until(lambda _: self.upper._visible(time_switch).get_attribute("aria-checked") == "true", "带时标选项未启用")
        confirm = (By.XPATH, self.LINK_DIALOG + "//div[contains(@class,'ant-modal-footer')]//button[contains(@class,'ant-btn-primary')]")
        with self.upper._defer_stop():
            self.upper._click(confirm, f"保存本例 IEC104 链路 {self.name}", before_click=self._track_creation)
            self.upper._until(lambda _: self._creation_window_closed(), f"IEC104 链路 {self.name} 的创建窗口仍然打开")
            self._select_link(self.name)
            self._assert_link()
            self.confirmed = True
        return self.port

    def _choose(self, placeholder, query, text):
        control_path = (
            self.IMPORT_DIALOG
            + "//div[contains(concat(' ',normalize-space(@class),' '),' ant-select ')"
              " and not(contains(@class,'ant-select-disabled'))]"
              "[.//*[(contains(@class,'ant-select-placeholder') or contains(@class,'ant-select-selection-placeholder'))"
              " and normalize-space(.)=" + xpath_text(placeholder) + "]]"
        )
        selector = (By.XPATH, control_path
                    + "//div[contains(concat(' ',normalize-space(@class),' '),' ant-select-content ')"
                      " or contains(concat(' ',normalize-space(@class),' '),' ant-select-selector ')]")
        self.upper._click(selector, f"打开{placeholder}")

        def opened_list(_):
            control = self.upper._visible((By.XPATH, control_path))
            if control is None:
                return None
            field = control.find_element(By.CSS_SELECTOR, "[role='combobox']")
            list_id = field.get_attribute("aria-controls")
            if field.get_attribute("aria-expanded") == "true" and list_id:
                return field, list_id

        field, list_id = self.upper._until(opened_list, f"{placeholder}下拉框未打开")
        if query:
            field.send_keys(query)
        # 虚拟列表的 ARIA 节点与可点击选项同属一个下拉弹层。
        choice = (By.XPATH, "//div[contains(concat(' ',normalize-space(@class),' '),' ant-select-dropdown ')"
                  " and not(contains(@class,'ant-select-dropdown-hidden'))][.//*[@id=" + xpath_text(list_id) + "]]"
                  "//div[contains(concat(' ',normalize-space(@class),' '),' ant-select-item-option ')"
                  " and not(contains(@class,'ant-select-item-option-disabled')) and not(@aria-disabled='true')]"
                  "[.//div[contains(@class,'ant-select-item-option-content') and normalize-space(.)=" + xpath_text(text) + "]]")
        self.upper._click(choice, f"选择{text}")

    def _point_search(self):
        search = (By.XPATH, self.POINT_CARD + "//input[@placeholder='搜索 Tag 或 IOA']")
        if not self.upper._visible(search):
            button = (By.XPATH, self.POINT_CARD + "//button[@aria-expanded='false'][starts-with(normalize-space(.),'筛选')]")
            self.upper._click(button, "打开 IEC104 点表筛选")
        return self.upper._until(lambda _: self.upper._visible(search), "IEC104 点表搜索框未显示")

    def _saved_point_cells(self, tag):
        locator = (By.XPATH, self.POINT_CARD + "//*[@data-row-key=" + xpath_text(tag) + "]")
        row = self.upper._until(lambda _: self.upper._visible(locator), f"IEC104 点表未显示已保存的 {tag}")
        cells = row.find_elements(By.CSS_SELECTOR, ":scope > .ant-table-cell")
        return [cell.get_property("innerText").strip() for cell in cells]

    def import_point(self, connection, tag, destination, *, module="ModbusRTU", business_type="遥测"):
        self._select_link(self.name)
        self._assert_link()
        self._button("添加点位", self.POINT_CARD, "打开 IEC104 添加点位菜单")
        self.upper._click((By.XPATH, "//*[@role='menuitem'][normalize-space(.)='从数据总线导入']"), "从数据总线导入")
        source = f"{module}/{connection}"
        self._choose("先选择来源连接", connection, source)
        self._choose("选择一个或多个来源点位", tag, f"{source} : {tag}")
        self._choose("统一点位类型", None, "FLOAT (短浮点值)")
        self._choose("统一 IOA 业务类别", None, business_type)
        row = (By.XPATH, self.IMPORT_DIALOG + "//tr[.//span[normalize-space(.)=" + xpath_text(f"{source} : {tag}") + "]]")
        draft = self.upper._until(lambda _: self.upper._visible(row), "导入点位草稿未显示")
        self.upper._replace(draft.find_element(By.XPATH, "./td[2]//input"), destination)
        self._button("导入 1 个点位", self.IMPORT_DIALOG, f"保存 IEC104 {business_type}点和数据总线路由")
        cells = []
        with self.upper.desktop.step(f"核对已保存的 IEC104 {business_type}点 {destination}",
                                     expected=f"点表显示本例标签、IOA、短浮点类型和{business_type}业务类别",
                                     actual=cells):
            self.upper._until(lambda _: not self.upper._visible((By.XPATH, self.IMPORT_DIALOG)),
                              f"IEC104 {business_type}点 {destination} 的导入窗口未关闭")
            bus_endpoint = f"{source} : {tag}"
            iec104_endpoint = f"IEC104/{self.name} : {destination}"
            self.routes.append((iec104_endpoint, bus_endpoint) if business_type == "遥调"
                               else (bus_endpoint, iec104_endpoint))
            search = self._point_search()
            previous_query = search.get_property("value")
            self.upper._replace(search, destination)
            cells.extend(self._saved_point_cells(destination))
            ioa = next((int(value.split()[0]) for value in cells if "(0x" in value and value.split()[0].isdecimal()), None)
            assert ioa is not None and "FLOAT (短浮点值)" in cells and business_type in cells, (
                f"IEC104 {business_type}配置不符：{cells!r}"
            )
            self.upper._replace(self._point_search(), previous_query)
        return ioa

    def check_routes(self):
        self.upper._menu("数据总线")
        for source, target in self.routes:
            route = (By.XPATH, "//div[contains(@class,'data-bus-route-card')]//tr["
                     "td[2]//*[normalize-space(.)=" + xpath_text(source) + "] and td[4]//*[normalize-space(.)="
                     + xpath_text(target) + "]]")
            self.upper._until(lambda _: self.upper._visible(route), f"数据总线未显示本例路由 {source} → {target}")

    def start(self):
        self._open()
        self._select_link(self.name)
        self._assert_link()
        self._button("连接", self.CARD, "启动本例 IEC104 被控站链路")
        self.upper._until(lambda _: (button := self.upper._visible(self.upper._button("断开", self.CARD))) and button.is_enabled(),
                          "专用 IEC104 链路未启动")

    def _discard_dialogs(self):
        for dialog in (self.IMPORT_DIALOG, self.LINK_DIALOG):
            locator = (By.XPATH, dialog)
            if not self.upper._visible(locator):
                continue
            busy = (By.XPATH, dialog + "//*[contains(concat(' ',normalize-space(@class),' '),' ant-btn-loading ')]")
            self.upper._until(lambda _: not self.upper._visible(busy), f"IEC104 链路 {self.name} 的配置提交未结束")
            if dialog == self.LINK_DIALOG and self.requested and not self.confirmed and not self.creation_conflict:
                self.creation_conflict = self.upper._creation_conflict(self.LINK_DIALOG)
            if not self.upper._visible(locator):
                continue
            discard = self.upper._button("放弃草稿", dialog)
            close = (By.XPATH, dialog + "//button[contains(@class,'ant-modal-close')]")
            self.upper._click(discard if self.upper._visible(discard) else close, "关闭本例未提交的 IEC104 配置窗口")
            self.upper._until(lambda _: not self.upper._visible(locator), "本例 IEC104 配置窗口未关闭")

    def _delete_link(self):
        if self.creation_conflict:
            raise RuntimeError(f"创建请求发生冲突，拒绝接管：{self.creation_conflict}")
        self._open()
        self.upper._refresh_connection_list("IEC104", "iec104-page-alert")
        if not self.confirmed and not self.upper._visible(self.upper._connection_locator(self.name)):
            raise RuntimeError("刷新后未找到链路，无法确认创建请求结果及其归属")
        self._select_link(self.name)
        self._assert_link()
        stop = self.upper._visible(self.upper._button("断开", self.CARD))
        if stop and stop.is_enabled():
            self._button("断开", self.CARD, "断开本例 IEC104 链路")
            self.upper._confirm("确认断开连接？", "确认断开本例 IEC104 链路")
            self.upper._until(lambda _: (button := self.upper._visible(self.upper._button("连接", self.CARD))) and button.is_enabled(),
                              f"IEC104 链路 {self.name} 未断开")
        self._assert_link()
        self.upper._click((By.XPATH, "//button[@aria-label=" + xpath_text(f"删除连接 {self.name}") + "]"), "删除本例 IEC104 链路")
        self.upper._confirm(f"确认删除 {self.name}？", "确认删除本例 IEC104 链路", clear_delete_tooltip=True)
        self.upper._until(lambda _: not self.upper.driver.find_elements(*self.upper._connection_locator(self.name)), "本例 IEC104 链路未删除")
        self.upper._refresh_connection_list("IEC104", "iec104-page-alert")
        if self.upper.driver.find_elements(*self.upper._connection_locator(self.name)):
            raise RuntimeError("刷新后仍存在同名链路，未确认删除结果")
        self.requested = False

    def cleanup(self):
        if self.port is None:
            return
        was_cleaning = self.upper._cleaning
        self.upper._cleaning = True
        errors = []
        try:
            try:
                if self.requested and not self.confirmed and not self.creation_conflict:
                    self.creation_conflict = self.upper._creation_conflict(self.LINK_DIALOG)
                self._discard_dialogs()
            except Exception as error:
                errors.append(f"关闭配置窗口失败：{getattr(error, 'msg', None) or error}")
            if self.requested:
                try:
                    self._delete_link()
                except Exception as error:
                    errors.append(str(getattr(error, 'msg', None) or error))
            if errors:
                raise RuntimeError(f"IEC104 链路 {self.name} 清理失败：" + "；".join(errors))
        finally:
            self.upper._cleaning = was_cleaning
