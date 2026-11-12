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
        self.requested = False
        self.confirmed = False
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

    def _fill(self, field, value):
        element = self.upper._until(lambda _: self.upper._visible((By.XPATH, self.LINK_DIALOG + "//input[@id=" + xpath_text(field) + "]")))
        self.upper._replace(element, value)

    def _assert_link(self):
        endpoint = self._description("本地端点")
        ca = self._description("公共地址 (CA)")
        role = self._description("站点角色")
        assert endpoint and endpoint.text.strip().rpartition(":")[2] == str(self.port), "IEC104 连接端口与本例配置不符"
        assert ca and ca.text.strip() == "1", "IEC104 连接公共地址与本例配置不符"
        assert role and "被控站" in role.text, "IEC104 连接不是被控站"

    def create(self):
        self._open()
        refresh = (By.CSS_SELECTOR, 'button[aria-label="刷新连接列表"]')
        self.upper._click(refresh, "刷新既有 IEC104 链路")
        self.upper._until(lambda _: (button := self.upper._visible(refresh)) and button.is_enabled()
                          and "ant-btn-loading" not in (button.get_attribute("class") or ""), "IEC104 链路列表未完成刷新")
        alert = self.upper._visible((By.XPATH, "//div[contains(@class,'iec104-page-alert')][.//*[normalize-space(.)='连接列表刷新失败']]"))
        if alert:
            raise AssertionError(f"未读取到 IEC104 既有端口，停止创建：{alert.text}")
        names = [item.text.strip() for item in self.upper.driver.find_elements(By.CSS_SELECTOR, ".protocol-connection-item-main strong")]
        if self.name in names:
            raise AssertionError("存在同名 IEC104 链路，拒绝接管")
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
        time_switch = (By.XPATH, self.LINK_DIALOG + "//button[@id='point_with_time']")
        self.upper._click(time_switch, "启用 IEC104 带时标上送")
        self.upper._until(lambda _: self.upper._visible(time_switch).get_attribute("aria-checked") == "true", "带时标选项未启用")
        confirm = (By.XPATH, self.LINK_DIALOG + "//div[contains(@class,'ant-modal-footer')]//button[contains(@class,'ant-btn-primary')]")
        self.upper._click(confirm, "保存本例 IEC104 链路")
        self.requested = True
        self.upper._until(lambda _: self.upper._visible(self.upper._connection_locator(self.name)), "保存后未显示本例 IEC104 链路")
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

    def import_point(self, connection, tag, destination, *, module="ModbusRTU"):
        self._select_link(self.name)
        self._assert_link()
        self._button("添加点位", self.POINT_CARD, "打开 IEC104 添加点位菜单")
        self.upper._click((By.XPATH, "//*[@role='menuitem'][normalize-space(.)='从数据总线导入']"), "从数据总线导入")
        source = f"{module}/{connection}"
        self._choose("先选择来源连接", connection, source)
        self._choose("选择一个或多个来源点位", tag, f"{source} : {tag}")
        self._choose("统一点位类型", None, "FLOAT (短浮点值)")
        self._choose("统一 IOA 业务类别", None, "遥测")
        row = (By.XPATH, self.IMPORT_DIALOG + "//tr[.//span[normalize-space(.)=" + xpath_text(f"{source} : {tag}") + "]]")
        draft = self.upper._until(lambda _: self.upper._visible(row), "导入点位草稿未显示")
        self.upper._replace(draft.find_element(By.XPATH, "./td[2]//input"), destination)
        self._button("导入 1 个点位", self.IMPORT_DIALOG, "保存 IEC104 遥测点和数据总线路由")
        cells = []
        with self.upper.desktop.step(f"核对已保存的 IEC104 遥测点 {destination}",
                                     expected="点表显示本例标签、IOA、短浮点类型和遥测业务类别",
                                     actual=cells):
            self.upper._until(lambda _: not self.upper._visible((By.XPATH, self.IMPORT_DIALOG)),
                              f"IEC104 遥测点 {destination} 的导入窗口未关闭")
            self.routes.append((f"{source} : {tag}", f"IEC104/{self.name} : {destination}"))
            search = self._point_search()
            previous_query = search.get_property("value")
            self.upper._replace(search, destination)
            cells.extend(self._saved_point_cells(destination))
            ioa = next((int(value.split()[0]) for value in cells if "(0x" in value and value.split()[0].isdecimal()), None)
            assert ioa is not None and "FLOAT (短浮点值)" in cells and "遥测" in cells, f"IEC104 遥测配置不符：{cells!r}"
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

    def cleanup(self):
        if self.port is None:
            return
        for dialog in (self.IMPORT_DIALOG, self.LINK_DIALOG):
            locator = (By.XPATH, dialog)
            if not self.upper._visible(locator):
                continue
            discard = self.upper._button("放弃草稿", dialog)
            close = (By.XPATH, dialog + "//button[contains(@class,'ant-modal-close')]")
            self.upper._click(discard if self.upper._visible(discard) else close, "关闭本例未提交的 IEC104 配置窗口")
            self.upper._until(lambda _: not self.upper._visible(locator), "本例 IEC104 配置窗口未关闭")
        if not self.requested:
            return
        self._open()
        if not self.confirmed and not self.upper._visible(self.upper._connection_locator(self.name)):
            raise RuntimeError(f"无法确认 IEC104 创建请求及其归属：{self.name}")
        self._select_link(self.name)
        self._assert_link()
        stop = self.upper._visible(self.upper._button("断开", self.CARD))
        if stop and stop.is_enabled():
            self._button("断开", self.CARD, "断开本例 IEC104 链路")
            self.upper._confirm("确认断开连接？", "确认断开本例 IEC104 链路")
            self.upper._until(lambda _: (button := self.upper._visible(self.upper._button("连接", self.CARD))) and button.is_enabled())
        self.upper._click((By.XPATH, "//button[@aria-label=" + xpath_text(f"删除连接 {self.name}") + "]"), "删除本例 IEC104 链路")
        self.upper._confirm(f"确认删除 {self.name}？", "确认删除本例 IEC104 链路")
        self.upper._until(lambda _: not self.upper.driver.find_elements(*self.upper._connection_locator(self.name)), "本例 IEC104 链路未删除")
