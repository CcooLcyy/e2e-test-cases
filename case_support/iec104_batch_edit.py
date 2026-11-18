"""通过 IEC104 配置页面准备和检查批量编辑场景。"""

from selenium.webdriver.common.by import By

from .modbus_rtu_page import xpath_text
from .rtu_quality_iec104 import RtuQualityIec104


class BatchEditIec104(RtuQualityIec104):
    BATCH_DIALOG = "//div[@role='dialog'][.//div[contains(@class,'ant-modal-title') and contains(.,'批量编辑点位')]]"
    ADD_DIALOG = "//div[@role='dialog' and contains(@class,'iec104-batch-point-modal')]"

    def _open_add_menu(self, option):
        self._button("添加点位", self.POINT_CARD, "打开 IEC104 添加点位菜单")
        self.upper._click((By.XPATH, "//*[@role='menuitem'][normalize-space(.)=" + xpath_text(option) + "]"),
                          f"打开{option}")

    def _choose_select(self, combo, option):
        selector = (By.XPATH, combo[1] + "/ancestor::div["
                    "contains(concat(' ',normalize-space(@class),' '),' ant-select ')][1]"
                    "//div[contains(concat(' ',normalize-space(@class),' '),' ant-select-content ')"
                    " or contains(concat(' ',normalize-space(@class),' '),' ant-select-selector ')]")
        self.upper._click(selector, f"打开下拉框以选择 {option}")

        def list_id(_):
            field = self.upper.driver.find_element(*combo)
            if field.get_attribute("aria-expanded") == "true":
                return field.get_attribute("aria-controls")

        control = self.upper._until(list_id, f"下拉框未打开：{option}")
        choice = (By.XPATH, "//div[contains(concat(' ',normalize-space(@class),' '),' ant-select-dropdown ')"
                  " and not(contains(@class,'ant-select-dropdown-hidden'))][.//*[@id=" + xpath_text(control) + "]]"
                  "//div[contains(concat(' ',normalize-space(@class),' '),' ant-select-item-option ')"
                  " and not(contains(@class,'ant-select-item-option-disabled')) and not(@aria-disabled='true')]"
                  "[.//div[contains(@class,'ant-select-item-option-content') and normalize-space(.)=" + xpath_text(option) + "]]")
        self.upper._click(choice, f"选择 {option}")

    def _wait_closed(self, dialog):
        self.upper._until(lambda _: not self.upper._visible((By.XPATH, dialog)), "IEC104 配置弹窗未关闭")

    def _fill_control(self, locator, value, label):
        with self.upper.desktop.step(f"填写 {label}", expected=str(value)):
            control = self.upper._until(lambda _: self.upper._visible(locator), f"未找到 {label}")
            self.upper._replace(control, value)

    def add_point(self, tag, ioa, *, business="遥测", point_type="FLOAT (短浮点值)", scale=1, offset=0, deadband=0):
        self._open_add_menu("添加点位")
        self._fill("tag", tag)
        address = (By.XPATH, self.LINK_DIALOG + "//label[@for='ioa']/ancestor::div["
                   "contains(concat(' ',normalize-space(@class),' '),' ant-form-item ')][1]//input[@role='spinbutton']")
        self._fill_control(address, ioa, "IOA")
        self._choose_select((By.XPATH, self.LINK_DIALOG + "//*[@id='point_type' and @role='combobox']"), point_type)
        self._choose_select((By.XPATH, self.LINK_DIALOG + "//*[@id='business_type' and @role='combobox']"), business)
        if point_type == "FLOAT (短浮点值)":
            for field, value in (("scale", scale), ("offset", offset), ("deadband", deadband)):
                self._fill(field, value)
        self._button("添加点位", self.LINK_DIALOG, f"保存点位 {tag}")
        self._wait_closed(self.LINK_DIALOG)
        cells = self.point_cells(tag)
        actual = (cells[1], int(cells[2].split()[0]), cells[3], cells[4], cells[6], *map(float, cells[7:10]))
        self.upper.desktop.check(f"核对初始点位 {tag}", actual,
                                 (tag, ioa, point_type, business, "关闭", scale, offset, deadband))

    def add_address_block(self, tags, category, start):
        self._open_add_menu("批量添加点位")
        combo = (By.XPATH, self.ADD_DIALOG + "//*[normalize-space(.)='统一 IOA 业务类别'"
                 " and contains(@class,'ant-typography')]/following-sibling::*[1]//*[@role='combobox']")
        self._choose_select(combo, category)
        address = (By.XPATH, self.ADD_DIALOG + "//*[normalize-space(.)='起始 IOA' and contains(@class,'ant-typography')]"
                   "/ancestor::div[contains(concat(' ',normalize-space(@class),' '),' ant-col ')][1]//input[@role='spinbutton']")
        self._fill_control(address, start, "起始 IOA")
        self._fill_control((By.XPATH, self.ADD_DIALOG + "//textarea"), "\n".join(tags), "批量点名")
        self._button(f"添加 {len(tags)} 个点位", self.ADD_DIALOG, f"保存 {len(tags)} 个占位点")
        self._wait_closed(self.ADD_DIALOG)

    def _point_row(self, tag):
        return self.POINT_CARD + "//*[@data-row-key=" + xpath_text(tag) + "]"

    def _search_point(self, tag):
        self.upper._replace(self._point_search(), tag)
        self.upper._until(lambda _: self.upper._visible((By.XPATH, self._point_row(tag))), f"点表未显示 {tag}")

    def point_cells(self, tag):
        try:
            self._search_point(tag)
            cells = self._saved_point_cells(tag)
            assert len(cells) == 11, f"点位 {tag} 的配置列不完整：{cells!r}"
            return cells
        finally:
            self.upper._replace(self._point_search(), "")

    def open_batch(self, tags):
        try:
            for tag in tags:
                self._search_point(tag)
                row = self._point_row(tag)
                checkbox = (By.XPATH, row + "//input[@type='checkbox']")
                checked = self.upper._until(lambda _: self.upper._visible(checkbox), f"找不到 {tag} 的选择框")
                if not checked.is_selected():
                    label = (By.XPATH, row + "//label[.//input[@type='checkbox']]")
                    self.upper._click(label, f"勾选点位 {tag}")
                    self.upper._until(lambda _: self.upper._visible(checkbox).is_selected(), f"点位 {tag} 未被勾选")
        finally:
            self.upper._replace(self._point_search(), "")
        self._button(f"批量编辑 ({len(tags)})", self.POINT_CARD, "打开批量编辑点位")
        self.check_counts(len(tags), 0)

    def choose_batch(self, label, option):
        combo = (By.XPATH, self.BATCH_DIALOG + "//*[@role='combobox' and @aria-label=" + xpath_text(label) + "]")
        self._choose_select(combo, option)

    def fill_batch(self, label, value):
        locator = (By.XPATH, self.BATCH_DIALOG + "//input[not(@role='combobox') and @aria-label=" + xpath_text(label) + "]")
        self._fill_control(locator, value, label)

    def batch_button(self, text):
        return self.upper._button(text, self.BATCH_DIALOG)

    def check_counts(self, selected, changed):
        expected = f"已选 {selected} 个点位，预计修改 {changed} 个"
        locator = (By.XPATH, self.BATCH_DIALOG + "//*[normalize-space(.)=" + xpath_text(expected) + "]"
                   "[not(*[normalize-space(.)=" + xpath_text(expected) + "])]")
        with self.upper.desktop.step("核对批量编辑预计修改数量", expected=expected):
            self.upper._until(lambda _: self.upper._visible(locator), f"未显示预计修改数量：{expected}")

    def save_batch(self):
        self._button("保存批量修改", self.BATCH_DIALOG, "保存批量修改")
        self._wait_closed(self.BATCH_DIALOG)

    def cancel_batch(self):
        button = "放弃草稿" if self.upper._visible(self.batch_button("放弃草稿")) else "取消"
        self._button(button, self.BATCH_DIALOG, "放弃未保存的批量修改")
        self._wait_closed(self.BATCH_DIALOG)

    def reopen_points(self):
        # 离开并重进页面，通过产品正常加载流程重新获取点表。
        self.upper._menu("数据总线")
        self._open()
        self._select_link(self.name)
        self._assert_link()

    def stop(self):
        self._button("断开", self.CARD, "断开本例 IEC104 链路")
        self.upper._confirm("确认断开连接？", "确认断开本例 IEC104 链路")
        self.upper._until(lambda _: (button := self.upper._visible(self.upper._button("连接", self.CARD)))
                          and button.is_enabled(), "专用 IEC104 链路未断开")

    def _discard_dialogs(self):
        for dialog in (self.BATCH_DIALOG, self.ADD_DIALOG):
            if self.upper._visible((By.XPATH, dialog)):
                busy = (By.XPATH, dialog + "//*[contains(concat(' ',normalize-space(@class),' '),' ant-btn-loading ')]")
                self.upper._until(lambda _: not self.upper._visible(busy), "IEC104 批量配置提交未结束")
                if self.upper._visible((By.XPATH, dialog)):
                    button = "放弃草稿" if self.upper._visible(self.upper._button("放弃草稿", dialog)) else "取消"
                    self._button(button, dialog, "关闭本例未提交的批量配置")
                    self._wait_closed(dialog)
        super()._discard_dialogs()
