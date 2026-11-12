"""通过正常业务界面建立专用 AGC 或 AVC 组并检查全站点值。"""

import re

from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By

from .agc_direction_page import AgcDirectionPage, parse_decimal, unique_match
from .modbus_rtu_page import xpath_text


GROUP = "//div[@role='dialog' and contains(@class,'control-group-modal')]"
MEMBER = "//div[@role='dialog' and contains(@class,'control-member-modal')]"
LIST = "//div[contains(@class,'ant-card')][./div[contains(@class,'ant-card-head')]//*[normalize-space(.)='控制组列表']]"
AVC_SUMMARY = "//div[contains(@class,'control-summary-card')]"


class PowerRangePage(AgcDirectionPage):
    def __init__(self, desktop, module, name):
        super().__init__(desktop)
        self.module = module
        self.group_name = name
        self.conn_id = None
        self.submitted = False
        self.active = module == "AGC"
        self.runtime = "//section[@aria-label='AGC 功率总览']" if self.active else "//div[contains(@class,'control-runtime-card')]"

    def open_groups(self, view="控制策略"):
        self._menu("控制策略")
        for label in (self.module, view):
            tab = (By.XPATH, "//*[@aria-label='控制页面切换']//*[@role='tab'][normalize-space(.)="
                   + xpath_text(label) + "]")
            self._click(tab, f"打开 {self.module} {label} 页签")
        self._until(lambda _: self._visible((By.XPATH, LIST)), f"{self.module} 控制组列表未显示")

    def _field_value(self, root, field, value):
        locator = (By.XPATH, root + "//input[@id=" + xpath_text(field) + "]")
        self._replace(self._until(lambda _: self._visible(locator), f"找不到配置项 {field}"), value)

    def _choose(self, root, attribute, field, value, search=None):
        control = root + f"//input[@{attribute}=" + xpath_text(field) + "]"
        select = control + "/ancestor::div[contains(concat(' ',normalize-space(@class),' '),' ant-select ')][1]"
        selector = (select + "[not(contains(@class,'ant-select-disabled'))]"
                    "//*[contains(concat(' ',normalize-space(@class),' '),' ant-select-content ')"
                    " or contains(concat(' ',normalize-space(@class),' '),' ant-select-selector ')]")
        self._click((By.XPATH, selector), f"打开 {field}")

        def opened(_):
            element = self._visible((By.XPATH, control))
            if element and element.get_attribute("aria-expanded") == "true":
                list_id = element.get_attribute("aria-controls")
                if list_id:
                    return element, list_id
            return False

        field_element, list_id = self._until(opened, f"{field} 未显示可选列表")
        if search:
            field_element.send_keys(search)
        option = ("//div[contains(@class,'ant-select-dropdown') and not(contains(@class,'ant-select-dropdown-hidden'))]"
                  "[.//*[@id=" + xpath_text(list_id) + "]]"
                  "//*[contains(concat(' ',normalize-space(@class),' '),' ant-select-item-option ') and @title="
                  + xpath_text(value) + "]")
        self._click((By.XPATH, option), f"选择 {value}")

    def _bind(self, label, connection, tag):
        source = f"ModbusRTU / {connection}"
        self._choose(MEMBER, "aria-label", label + "连接", source, search=connection)
        self._choose(MEMBER, "aria-label", label, f"{source} / {tag}", search=tag)

    def _open_agc_members(self):
        self._click((By.XPATH, GROUP + "//*[@role='tab'][starts-with(normalize-space(.),'成员设备')]"),
                    "打开 AGC 成员配置")

    def create_group(self, connection, lower, upper):
        self.open_groups()
        assert self._group_item(self.group_name) is None, f"已有同名控制组 {self.group_name}"
        self._click(self._button("+ 新增控制组", LIST), "新增本例控制组")
        if not self.active:
            self._click(self._button("我已了解，继续新增", self.DIALOG_XPATH), "确认新增 AVC 控制组")
        self._field_value(GROUP, "group_name", self.group_name)
        if self.active:
            self._open_agc_members()
        else:
            self._choose(GROUP, "id", "command_mode", "总无功模式")
        self._click((By.XPATH, GROUP + '//button[@aria-label="添加成员"]'), "新增本例成员")
        self._field_value(MEMBER, "member_name", connection)
        if self.active:
            self._field_value(MEMBER, "capacity_kw_decimal", upper)
            self._field_value(MEMBER, "min_kw_decimal", lower)
            self._field_value(MEMBER, "max_kw_decimal", upper)
            self._bind("有功测量点", connection, "power-measured")
            self._bind("有功设定点", connection, "power-setpoint")
            self._bind("通信状态点（可选）", connection, "__communication_state")
        else:
            self._field_value(MEMBER, "q_min_kvar", lower)
            self._field_value(MEMBER, "q_max_kvar", upper)
            self._bind("无功测量点", connection, "power-measured")
            self._bind("无功设定点", connection, "power-setpoint")
        self._click(self._button("确认成员", MEMBER), "确认本例成员")
        self._until(lambda _: not self._visible((By.XPATH, MEMBER)), "成员配置尚未确认")
        self._click(self._button("保存控制组并建立路由", GROUP), "保存本例控制组和成员路由")
        self.submitted = True
        self._until(lambda _: not self._visible((By.XPATH, GROUP)), "控制组和成员路由尚未保存")
        item = self._until(lambda _: self._group_item(self.group_name), "未找到新建控制组")
        identity = re.search(r"conn_id=([1-9]\d*)\s*\|", item.text)
        assert identity, f"本例控制组缺少连接编号：{item.text}"
        self.conn_id = int(identity[1])
        self.desktop.metadata["本例控制组"] = {"模块": self.module, "组名": self.group_name, "连接编号": self.conn_id}

    def open_owned(self, view="控制策略"):
        self.open_groups(view)
        item = self._until(lambda _: self._group_item(self.group_name), "未找到本例控制组")
        assert self.conn_id and f"conn_id={self.conn_id} |" in item.text, f"本例控制组连接身份不符：{item.text}"
        item.click()
        if view == "控制策略":
            root = self.runtime if self.active else AVC_SUMMARY
            selected = (By.XPATH, root + "//*[normalize-space(text())=" + xpath_text(self.group_name) + "]")
            self._until(lambda _: self._visible(selected), "当前控制组详情尚未切换到本例")
        else:
            self._until(lambda _: (selected := self._group_item(self.group_name))
                        and selected.value_of_css_property("background-color") == "rgba(55, 55, 61, 1)",
                        "默认点页面未选中本例控制组")

    def start_group(self):
        self.open_owned()
        if "运行中" not in self._group_item(self.group_name).text:
            self._click(self._button("启动控制组", self.runtime), "启动本例控制组")
        self._until(lambda _: "运行中" in self._group_item(self.group_name).text, "本例控制组未进入运行中")

    def set_member_upper_limit(self, upper):
        self.open_owned()
        self._click(self._button("停止控制组", self.runtime), "停止本例控制组以调整配置上限")
        if self.active:
            popup = ("//div[contains(@class,'ant-popover') and not(contains(@class,'hidden'))]"
                     "[.//*[normalize-space(.)='确认停止该控制组？']]")
            self._click(self._button("停止控制组", popup), "确认停止本例 AGC 组")
        self._until(lambda _: "已停止" in self._group_item(self.group_name).text, "本例控制组未停止")
        root = self.runtime if self.active else AVC_SUMMARY
        self._click(self._button("编辑", root), "编辑本例控制组")
        if self.active:
            self._open_agc_members()
        action = f"编辑成员 {self.group_name}" if self.active else f"编辑 {self.group_name}"
        self._click((By.XPATH, GROUP + "//button[@aria-label=" + xpath_text(action) + "]"), "编辑本例成员")
        self._field_value(MEMBER, "max_kw_decimal" if self.active else "q_max_kvar", upper)
        self._click(self._button("确认成员", MEMBER), "确认新的成员上限")
        self._until(lambda _: not self._visible((By.XPATH, MEMBER)), "成员上限尚未确认")
        self._click(self._button("保存控制组并建立路由", GROUP), "保存本例成员上限")
        self._until(lambda _: not self._visible((By.XPATH, GROUP)), "成员上限尚未保存")
        self.start_group()

    def wait_station_values(self, expected, timeout):
        self.open_owned("控制策略" if self.active else "默认点")
        with self.desktop.step(f"核对 {self.module} 全站状态", expected=str(expected)):
            entry = self.desktop.steps[-1]
            last = {}

            def observed(_):
                matched = True
                for tag, value in expected.items():
                    if self.active:
                        current, quality = self._read_agc_value(tag)
                    else:
                        current = self._read_avc_value(tag)
                        quality = None
                    last[tag] = {"值": current, "品质": quality} if quality is not None else {"值": current}
                    try:
                        value_matches = parse_decimal(current) == value
                    except ValueError:
                        value_matches = False
                    if not value_matches or (self.active and quality != "有效"):
                        matched = False
                entry["实际"] = str(last)
                return matched

            if self.active:
                details = self._button("查看点值明细", self.runtime)
                if self._visible(details):
                    self._click(details, "展开 AGC 运行点值明细")
            try:
                self._until(observed, f"{self.module} 全站状态未达到预期", timeout)
            except TimeoutException as error:
                raise AssertionError(f"{self.module} 全站状态不符，预期 {expected}，最后观察 {last}") from error
            finally:
                self.desktop.metadata.setdefault("全站状态观察", []).append({"预期": expected, "实际": dict(last)})

    def _read_agc_value(self, tag):
        if tag == "总有功实测":
            card = self._visible((By.XPATH, self.runtime + "//article[@aria-label=" + xpath_text(tag) + "]"))
            if card:
                value = card.find_element(By.CSS_SELECTOR, ".agc-runtime-power-value").text
                quality = "有效" if card.text.splitlines()[-1] == "新鲜 · 有效" else card.text.splitlines()[-1]
                return value, quality
        table = self._visible((By.CSS_SELECTOR, '[aria-label="运行点值明细"]'))
        if table:
            rows = table.find_elements(By.XPATH, ".//tr[@data-row-key=" + xpath_text(tag) + "]")
            if rows:
                values = self._row_values(table, unique_match(rows, tag))
                return values["缓存值"], values["质量"]
        return "未显示", "未显示"

    def _read_avc_value(self, tag):
        table = self._visible((By.CSS_SELECTOR, ".protocol-point-card"))
        if table:
            rows = table.find_elements(By.XPATH, ".//tr[./td[2][normalize-space(.)=" + xpath_text(tag) + "]]")
            if rows:
                return self._row_values(table, unique_match(rows, tag))["最新值"]
        return "未显示"

    def cleanup_group(self):
        for root, label in ((MEMBER, "取消编辑"), (GROUP, "取消")):
            if not self._visible((By.XPATH, root)):
                continue
            self._click(self._button(label, root), "关闭本例配置窗口")
            discard = self._button("放弃草稿" if self.active else "放弃改动", self.DIALOG_XPATH)
            self._until(lambda _: self._visible(discard) or not self._visible((By.XPATH, root)), "未显示草稿放弃确认")
            if self._visible(discard):
                self._click(discard, "放弃本例未保存配置")
            self._until(lambda _: not self._visible((By.XPATH, root)), "本例配置窗口未关闭")
        if not self.submitted:
            return
        self.open_owned()
        if self.active:
            details = (By.XPATH, self.runtime + "//*[@role='button'][.//*[normalize-space(.)='配置与点位详情']]")
            panel = self._until(lambda _: self._visible(details), "本例控制组详情未显示")
            if panel.get_attribute("aria-expanded") != "true":
                self._click(details, "展开本例控制组详情")
        root = self.runtime if self.active else AVC_SUMMARY
        button = "重试删除" if "待删除" in self._group_item(self.group_name).text else "删除"
        self._click(self._button(button, root), "删除本例控制组")
        popup = ("//div[contains(@class,'ant-popover') and not(contains(@class,'hidden'))]"
                 "[.//*[contains(@class,'ant-popconfirm-title') and starts-with(normalize-space(.),"
                 + xpath_text(f"确认删除 {self.group_name}？") + ")]]")
        self._click_popconfirm(popup, "确认删除本例控制组及关联路由")
        self._until(lambda _: self._group_item(self.group_name) is None, "本例控制组未删除")
