"""Prepare and remove the dedicated AGC group through the DSP Upper UI."""

import re

from selenium.webdriver.common.by import By

from .agc_direction_page import AgcDirectionPage, unique_match
from .modbus_rtu_page import xpath_text


class AgcDirectionSetup(AgcDirectionPage):
    LIST = "//div[contains(@class,'ant-card')][./div[contains(@class,'ant-card-head')]//*[normalize-space(.)='控制组列表']]"
    GROUP_DIALOG = "//*[@role='dialog' and contains(@class,'agc-group-modal')]"
    MEMBER_DIALOG = "//*[@role='dialog' and contains(@class,'agc-member-modal')]"
    OVERVIEW = "//section[@aria-label='AGC 功率总览']"
    DETAILS = (By.XPATH, OVERVIEW + "//*[@role='button'][.//*[normalize-space(.)='配置与点位详情']]")
    REFRESH = (By.XPATH, LIST + "//div[contains(@class,'ant-card-extra')]//button")

    def __init__(self, desktop):
        super().__init__(desktop)
        self.name = None
        self.member_name = None
        self.group = None
        self._draft_opened = False
        self._save_requested = False

    def _fill_group_field(self, dialog, field, value):
        locator = (By.XPATH, dialog + "//input[@id=" + xpath_text(field) + "]")
        element = self._until(lambda _: self._visible(locator), f"AGC 字段未显示：{field}")
        self._replace(element, value)

    def _choose(self, dialog, field, label, *, search=None, accessible_name=False):
        attribute = "aria-label" if accessible_name else "id"
        select = dialog + f"//input[@{attribute}=" + xpath_text(field) + "]"
        control = select + "/ancestor::div[contains(concat(' ',normalize-space(@class),' '),' ant-select ')][1]"
        target = (By.XPATH, control + "//*[contains(concat(' ',normalize-space(@class),' '),' ant-select-content ')"
                  " or contains(concat(' ',normalize-space(@class),' '),' ant-select-selector ')]")
        self._click(target, f"打开 AGC {field} 选项")
        locator = (By.XPATH, select)

        def open_list(_):
            field_input = self._visible(locator)
            if field_input and field_input.get_attribute("aria-expanded") == "true":
                list_id = field_input.get_attribute("aria-controls")
                if list_id:
                    return field_input, list_id
            return False

        field_input, list_id = self._until(open_list, f"AGC 下拉框未打开：{field}")
        if search is not None:
            field_input.send_keys(search)
        option = (By.XPATH, "//div[contains(concat(' ',normalize-space(@class),' '),' ant-select-dropdown ') and .//*[@id="
                  + xpath_text(list_id) + "]]//div[contains(concat(' ',normalize-space(@class),' '),' ant-select-item-option ')"
                  " and not(contains(@class,'ant-select-item-option-disabled')) and @title=" + xpath_text(label) + "]")
        self._click(option, f"选择 {label}")

    def _refresh_groups(self):
        self._click(self.REFRESH, "刷新 AGC 控制组列表")

        def refreshed(_):
            failure = self._visible((By.XPATH, "//*[contains(@class,'ant-message-error')]"
                                     "//*[starts-with(normalize-space(.),'刷新控制组失败:')]"))
            if failure:
                raise RuntimeError(failure.text)
            button = self._visible(self.REFRESH)
            return button and button.is_enabled() and "ant-btn-loading" not in (button.get_attribute("class") or "")

        self._until(refreshed, "AGC 控制组列表未完成刷新")

    @staticmethod
    def _read_conn_id(item):
        identity = re.search(r"conn_id=(\d+)\s*\|", item.text)
        if not identity or int(identity[1]) == 0:
            raise RuntimeError("AGC 控制组缺少有效的连接编号")
        return int(identity[1])

    def _selected_overview(self):
        return self._visible((By.XPATH, self.OVERVIEW + "[.//h4[normalize-space(.)=" + xpath_text(self.name) + "]]"))

    def _select_owned_group(self):
        item = self._until(lambda _: self._group_item(self.name), "未显示本例 AGC 控制组")
        if self.group is None or self._read_conn_id(item) != self.group["conn_id"]:
            raise RuntimeError("AGC 控制组身份与本例创建记录不一致")
        item.click()
        return self._until(lambda _: self._selected_overview(), "AGC 总览未切换到本例控制组")

    def _members_tab(self):
        locator = (By.XPATH, self.GROUP_DIALOG + "//*[@role='tab'][starts-with(normalize-space(.),'成员设备')]")
        self._click(locator, "打开 AGC 成员设备页签")

    def _wait_dialog_closed(self, dialog, action):
        self._until(lambda _: not self._visible((By.XPATH, dialog)), f"{action}后 AGC 窗口仍然打开")

    def _read_point_specs(self):
        details = self._until(lambda _: self._visible(self.DETAILS), "AGC 配置与点位详情未显示")
        if details.get_attribute("aria-expanded") != "true":
            self._click(self.DETAILS, "展开 AGC 配置与点位详情")
        tags = {}
        for key, label in (("command_tag", "总控输入"), ("measured_tag", "总有功实测")):
            locator = (By.XPATH, self.OVERVIEW + "//div[contains(@class,'ant-descriptions')][./div[contains(@class,'ant-descriptions-header')]"
                       "//div[normalize-space(.)=" + xpath_text(label) + "]]")
            block = self._until(lambda _: self._visible(locator), f"AGC 未显示{label}配置")
            tag = self._description(block, "本地点名")
            if not tag or tag == "未配置":
                raise AssertionError(f"AGC {label}缺少实际 Tag")
            self.desktop.check(f"核对 AGC {label}工程量换算", self._description(block, "配置单位 / 缩放 / 偏移"), "kW / 1 / 0")
            tags[key] = tag
        overview = self._selected_overview()
        self.desktop.check("核对 AGC 连接编号", self._description(overview, "连接 ID"), str(self.group["conn_id"]))
        self.desktop.check("核对 AGC 控制周期", self._description(overview, "控制周期"), "计算 1 秒 / 命令 4 秒")
        return tags

    def _assert_saved_bindings(self, measurement, setpoint):
        self._click(self._button("编辑", self.OVERVIEW), "重新打开本例 AGC 控制组读取已保存映射")
        self._draft_opened = True
        self._members_tab()
        edit = (By.XPATH, self.GROUP_DIALOG + "//button[@aria-label=" + xpath_text(f"编辑成员 {self.member_name}") + "]")
        self._until(lambda _: (button := self._visible(edit)) and button.is_enabled(), "AGC 已保存的成员与映射尚未就绪")
        cards = self.driver.find_elements(By.XPATH, self.GROUP_DIALOG + "//article[contains(@class,'agc-group-member')]")
        member = unique_match(cards, "本例单成员控制组的成员")
        self.desktop.check("核对 AGC 成员身份", member.get_attribute("aria-label"), f"成员 {self.member_name}")
        self.desktop.check("核对 AGC 成员可控状态", member.find_element(By.CSS_SELECTOR, ".ant-tag").text, "可控")
        summary = [node.text for node in member.find_elements(By.CSS_SELECTOR, ".agc-group-member__summary > span")]
        self.desktop.check("核对 AGC 额定容量和调节范围", summary[:2],
                           ["额定容量 100.00 kW", "调节范围 10.00～100.00 kW"])
        for label, endpoint in (("测量点", measurement), ("设定点", setpoint)):
            binding = unique_match(member.find_elements(By.XPATH, ".//div[contains(concat(' ',normalize-space(@class),' '),' agc-group-member__point ')]"
                                    "[./span[normalize-space(.)=" + xpath_text(label) + "]]/div/div"), label)
            self.desktop.check(f"核对已保存的 RTU {label}映射", binding.text, endpoint)
        self._click(edit, "读取 AGC 成员已保存的绝对值模式")
        advanced = (By.XPATH, self.MEMBER_DIALOG + "//summary[normalize-space(.)='更多控制参数']")
        summary = self._until(lambda _: self._visible(advanced), "成员更多控制参数未显示")
        if summary.find_element(By.XPATH, "..").get_attribute("open") is None:
            self._click(advanced, "展开成员更多控制参数")
        choice = (By.XPATH, self.MEMBER_DIALOG + "//input[@id='p_set_mode']/ancestor::div[contains(concat(' ',normalize-space(@class),' '),' ant-select ')][1]"
                  "//*[@title='绝对值']")
        self._until(lambda _: self._visible(choice), "AGC 成员指令未保存为绝对值")
        self._close_draft(self.MEMBER_DIALOG, "取消编辑")
        self._close_draft(self.GROUP_DIALOG, "取消")
        self._draft_opened = False

    def create_group(self, group_name, member_name, rtu_connection, measured_tag, setpoint_tag):
        """Save one controlled member and return the group identity and actual IEC104-facing tags."""
        if self._draft_opened or self._save_requested or self.group is not None:
            raise RuntimeError("本例 AGC 控制组已开始创建")
        self.name, self.member_name = group_name, member_name
        connection = f"ModbusRTU / {rtu_connection['name']}"
        measurement, setpoint = f"{connection} / {measured_tag}", f"{connection} / {setpoint_tag}"
        self._control_view("控制策略")
        self._refresh_groups()
        if self._group_item(group_name):
            raise RuntimeError(f"AGC 已存在同名控制组：{group_name}")

        with self.desktop.step("新增本例 AGC 控制组和单成员", expected={"组": group_name, "成员": member_name,
                               "RTU连接": rtu_connection, "测量来源": measurement, "设定目标": setpoint,
                               "模式": "周期直分配，绝对值", "范围kW": "10～100"}):
            self._click(self._button("+ 新增控制组", self.LIST), "新增专用 AGC 控制组")
            self._draft_opened = True
            self._fill_group_field(self.GROUP_DIALOG, "group_name", group_name)
            self._choose(self.GROUP_DIALOG, "control_mode", "周期直分配")
            self._choose(self.GROUP_DIALOG, "p_cmd_mode", "绝对值")
            self._members_tab()
            self._click((By.XPATH, self.GROUP_DIALOG + "//button[@aria-label='添加成员']"), "添加可控 AGC 成员")
            self._fill_group_field(self.MEMBER_DIALOG, "member_name", member_name)
            for field, value in (("capacity_kw_decimal", 100), ("min_kw_decimal", 10), ("max_kw_decimal", 100)):
                self._fill_group_field(self.MEMBER_DIALOG, field, value)
            for label, tag, endpoint in (("有功测量点", measured_tag, measurement), ("有功设定点", setpoint_tag, setpoint)):
                self._choose(self.MEMBER_DIALOG, f"{label}连接", connection, search=rtu_connection["name"], accessible_name=True)
                self._choose(self.MEMBER_DIALOG, label, endpoint, search=tag, accessible_name=True)
            self._click(self._button("确认成员", self.MEMBER_DIALOG), "确认成员与 RTU 点位绑定草稿")
            self._wait_dialog_closed(self.MEMBER_DIALOG, "确认成员")
            self._click(self._button("保存控制组并建立路由", self.GROUP_DIALOG), "保存控制组并建立 RTU 双向路由")
            self._save_requested = True
            self._wait_dialog_closed(self.GROUP_DIALOG, "保存控制组")
            self._draft_opened = False
            item = self._until(lambda _: self._group_item(group_name), "保存后未显示本例 AGC 控制组")
            self.group = dict(group_name=group_name, member_name=member_name, conn_id=self._read_conn_id(item),
                              lower_kw=10, upper_kw=100, cleanup="待清理")
            self.desktop.metadata["本例 AGC 控制组"] = self.group

        with self.desktop.step("读取已保存的 AGC 配置和实际点位"):
            overview = self._select_owned_group()
            context = overview.find_element(By.CSS_SELECTOR, ".agc-runtime-context").text
            if "周期直分配" not in context or "绝对值" not in context:
                raise AssertionError(f"AGC 控制方式或总控指令模式与本例不符：{context}")
            route_error = self._visible((By.XPATH, "//*[@role='alert'][.//*[normalize-space(.)='控制组配置已保存，映射尚未同步完成']]"))
            if route_error:
                raise AssertionError("AGC 自动路由保存失败：" + route_error.text)
            self.group.update(self._read_point_specs())
            self._assert_saved_bindings(measurement, setpoint)
        return dict(self.group)

    def start(self):
        self._control_view("控制策略")
        self._select_owned_group()
        self._click(self._button("启动控制组", self.OVERVIEW), "启动本例 AGC 控制组")

        def running(_):
            overview = self._selected_overview()
            if overview:
                badges = {node.text for node in overview.find_elements(By.CSS_SELECTOR, ".agc-runtime-heading .ant-tag")}
                return {"运行中", "功能投入", "远方"} <= badges
            return False

        with self.desktop.step("确认 AGC 已运行、投入并允许远方控制"):
            self._until(running, "AGC 控制组未进入运行、功能投入和远方状态")
            self.select_group(self.name, self.group["conn_id"])

    def _close_draft(self, dialog, button):
        if self._visible((By.XPATH, dialog)):
            self._click(self._button(button, dialog), "关闭本例 AGC 草稿窗口")
            discard = self._button("放弃草稿", self.DIALOG_XPATH)
            self._until(lambda _: self._visible(discard) or not self._visible((By.XPATH, dialog)), "未显示草稿放弃确认")
            if self._visible(discard):
                self._click(discard, "放弃本例未提交的 AGC 草稿")
            self._wait_dialog_closed(dialog, "关闭草稿")

    def cleanup(self):
        if not self._draft_opened and not self._save_requested:
            return
        self._cleaning = True
        try:
            with self.desktop.step("停止并删除本例 AGC 控制组及其关联路由", cancellable=False):
                if self._draft_opened:
                    self._close_draft(self.MEMBER_DIALOG, "取消编辑")
                    self._close_draft(self.GROUP_DIALOG, "取消")
                    self._draft_opened = False
                if not self._save_requested:
                    return
                # 切出并重进后按现场已保存配置清理，也能处理配置提交后映射未保存的部分失败。
                self._menu("数据总线")
                self._control_view("控制策略")
                self._refresh_groups()
                item = self._group_item(self.name)
                if not item:
                    self._save_requested = False
                    if self.group is not None:
                        self.group["cleanup"] = "已删除"
                    return
                if self.group is None:
                    # 仅接管本例提交且曾确认名称未被占用的组，仍须核对专用成员身份。
                    item.click()
                    overview = self._until(lambda _: self._selected_overview(), "未能核对本例待清理组")
                    members = [node.text for node in overview.find_elements(By.CSS_SELECTOR, ".agc-runtime-members tr[data-row-key] strong")]
                    if members != [self.member_name]:
                        raise RuntimeError(f"无法确认待清理组 {self.name} 的专用成员：{members}")
                    self.group = dict(group_name=self.name, member_name=self.member_name, conn_id=self._read_conn_id(item), cleanup="待清理")
                    self.desktop.metadata["本例 AGC 控制组"] = self.group
                overview = self._select_owned_group()
                stop = self._button("停止控制组", self.OVERVIEW)
                if self._visible(stop):
                    self._click(stop, "停止本例 AGC 控制组")
                    popup = "//div[contains(@class,'ant-popover')][.//*[normalize-space(.)='确认停止该控制组？']]"
                    self._click(self._button("停止控制组", popup), "确认停止本例 AGC 控制组")
                    self._until(lambda _: self._visible(self._button("启动控制组", self.OVERVIEW)), "本例 AGC 控制组未停止")
                details = self._until(lambda _: self._visible(self.DETAILS), "删除前未显示 AGC 配置详情")
                if details.get_attribute("aria-expanded") != "true":
                    self._click(self.DETAILS, "打开本例 AGC 删除入口")
                delete = self._button("重试删除" if "待删除" in overview.find_element(By.CSS_SELECTOR, ".agc-runtime-heading").text else "删除", self.OVERVIEW)
                self._click(delete, "删除本例 AGC 控制组")
                self._confirm(f"确认删除 {self.name}？", "确认删除本例 AGC 控制组")
                self._until(lambda _: not self._group_item(self.name), "本例 AGC 控制组未删除")
                self._refresh_groups()
                if self._group_item(self.name):
                    raise AssertionError("刷新后仍显示本例 AGC 控制组")
                self.group["cleanup"] = "已删除"
                self._save_requested = False
        finally:
            self._cleaning = False
