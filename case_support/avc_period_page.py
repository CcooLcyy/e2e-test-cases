"""通过 DSP 上位机建立 AVC 周期用例的专用控制组。"""

import re
import time

from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

from .modbus_rtu_page import ModbusRtuPage, xpath_text


GROUP = "//div[@role='dialog' and contains(@class,'control-group-modal')]"
MEMBER = "//div[@role='dialog' and contains(@class,'control-member-modal')]"
GROUP_LIST = ("//div[contains(@class,'ant-card')][./div[contains(@class,'ant-card-head')]"
              "//*[normalize-space(.)='控制组列表']]")
SUMMARY = "//div[contains(@class,'control-summary-card')]"
RUNTIME = "//div[contains(@class,'control-runtime-card')]"
ROUTE_ALERT = ("//*[contains(concat(' ',normalize-space(@class),' '),' ant-alert ')]"
               "[.//*[normalize-space(text())='控制组配置已保存，映射尚未同步完成']]")
MODE_LABELS = {"voltage": "目标电压模式", "q_total": "总无功模式"}


class AvcPeriodPage(ModbusRtuPage):
    def __init__(self, desktop):
        super().__init__(desktop)
        self.group_name = None
        self.conn_id = None
        self.group_draft_opened = False
        self.group_submitted = False

    def _open_groups(self):
        self._menu("控制策略")
        for label in ("AVC", "控制策略"):
            tab = (By.XPATH, "//*[@aria-label='控制页面切换']//*[@role='tab'][normalize-space(.)="
                   + xpath_text(label) + "]")
            element = self._until(lambda _: self._visible(tab), f"AVC 页签未显示：{label}")
            if element.get_attribute("aria-selected") != "true":
                self._click(tab, f"切换 AVC {label} 页签")
            self._until(lambda _: (selected := self._visible(tab)) and selected.get_attribute("aria-selected") == "true",
                        f"AVC 页签未选中：{label}")
        self._until(lambda _: self._visible((By.XPATH, GROUP_LIST)), "AVC 控制组列表未显示")

    def _refresh_groups(self):
        locator = (By.XPATH, GROUP_LIST + "//div[contains(@class,'ant-card-extra')]//button")
        self._click(locator, "刷新 AVC 控制组列表")
        self._until(lambda _: (button := self._visible(locator)) and button.is_enabled()
                    and "ant-btn-loading" not in (button.get_attribute("class") or ""), "AVC 控制组列表未完成刷新")

    def _group_item(self):
        return self._visible((By.XPATH, GROUP_LIST + "//li[.//span[normalize-space(.)="
                              + xpath_text(self.group_name) + "]]"))

    @staticmethod
    def _item_identity(item):
        match = re.search(r"conn_id=([1-9]\d*)\s*\|", item.text)
        if not match:
            raise AssertionError(f"AVC 控制组连接编号不可读：{item.text}")
        return int(match[1])

    def _read_summary(self, label):
        locator = (By.XPATH, SUMMARY + "//*[contains(@class,'ant-descriptions-item-label')"
                   " and normalize-space(.)=" + xpath_text(label) + "]"
                   "/following-sibling::*[contains(@class,'ant-descriptions-item-content')][1]")
        element = self._visible(locator)
        return element.text.strip() if element else None

    def _known_state(self, _):
        state = self._read_summary("控制状态")
        return state if state in ("已停止", "运行中") else None

    def _ready_button(self, locator):
        button = self._visible(locator)
        if button and button.is_enabled() and button.get_attribute("aria-disabled") != "true":
            return button

    def _select_owned(self):
        if self.conn_id is None:
            raise RuntimeError("本例 AVC 控制组尚未确认连接编号")
        self._open_groups()
        item = self._until(lambda _: self._group_item(), "未找到本例 AVC 控制组")
        if self._item_identity(item) != self.conn_id:
            raise AssertionError(f"本例 AVC 控制组身份已变化：{item.text}")
        item.click()
        self._until(lambda _: self._read_summary("控制组名称") == self.group_name
                    and self._read_summary("连接 ID") == str(self.conn_id),
                    "AVC 组配置未切换至本例控制组")

    def _fill_field(self, root, name, value):
        locator = (By.XPATH, root + "//input[@id=" + xpath_text(name) + "]")
        element = self._until(lambda _: self._visible(locator), f"AVC 配置项未显示：{name}")
        self._replace(element, value)

    def _choose(self, root, attribute, name, label, *, search=None, multiple=False):
        field_locator = root + f"//input[@{attribute}=" + xpath_text(name) + "]"
        control = field_locator + "/ancestor::div[contains(concat(' ',normalize-space(@class),' '),' ant-select ')][1]"
        trigger = (By.XPATH, control + "[not(contains(@class,'ant-select-disabled'))]"
                   "//*[contains(concat(' ',normalize-space(@class),' '),' ant-select-content ')"
                   " or contains(concat(' ',normalize-space(@class),' '),' ant-select-selector ')]")
        with self.desktop.step(f"选择 AVC {name}", expected=label):
            element = self._until(lambda _: self._visible(trigger), f"AVC 下拉框未就绪：{name}")
            element.click()

            def opened_list(_):
                field = self._visible((By.XPATH, field_locator))
                if field and field.get_attribute("aria-expanded") == "true":
                    list_id = field.get_attribute("aria-controls")
                    if list_id:
                        return field, list_id

            field, list_id = self._until(opened_list, f"AVC 下拉框未打开：{name}")
            if search is not None:
                field.send_keys(search)
            dropdown = ("//div[contains(concat(' ',normalize-space(@class),' '),' ant-select-dropdown ')"
                        " and not(contains(@class,'ant-select-dropdown-hidden'))][.//*[@id=" + xpath_text(list_id) + "]]")
            option = (By.XPATH, dropdown + "//div[contains(concat(' ',normalize-space(@class),' '),' ant-select-item-option ')"
                      " and @title=" + xpath_text(label) + "]")
            choice = self._until(lambda _: self._visible(option), f"AVC {name} 未显示选项：{label}")
            disabled = (choice.get_attribute("aria-disabled") == "true"
                        or "ant-select-item-option-disabled" in (choice.get_attribute("class") or "").split())
            if disabled:
                raise AssertionError(f"AVC {name} 的选项「{label}」在当前命令模式下已禁用")
            choice.click()
            self._until(lambda _: (selected := self._visible((By.XPATH, control))) and label in selected.text,
                        f"AVC {name} 未选中：{label}")
            if multiple and self._visible((By.XPATH, dropdown)):
                self._visible((By.XPATH, field_locator)).send_keys(Keys.ESCAPE)
                self._until(lambda _: not self._visible((By.XPATH, dropdown)), f"AVC 选项列表未关闭：{name}")

    def _bind_member(self, label, connection, tag):
        source = f"ModbusRTU / {connection}"
        self._choose(MEMBER, "aria-label", f"{label}连接", source, search=connection)
        self._choose(MEMBER, "aria-label", label, f"{source} / {tag}", search=tag)

    def _set_period_fields(self, calculation_period, command_period):
        self._fill_field(GROUP, "calculation_execution_period_seconds", calculation_period)
        self._fill_field(GROUP, "command_control_period_seconds", command_period)

    def _assert_periods(self, calculation_period, command_period):
        expected = f"计算 {calculation_period} 秒 / 命令 {command_period} 秒"
        with self.desktop.step("核对 AVC 已保存的控制周期", expected=expected):
            self._until(lambda _: self._read_summary("控制周期") == expected, f"AVC 未显示已保存的控制周期：{expected}")

    def _wait_draft_closed(self, root, message):
        self._until(lambda _: not self._visible((By.XPATH, root)), message)

    def _check_route_save(self):
        alert = self._visible((By.XPATH, ROUTE_ALERT))
        if alert:
            raise AssertionError(f"本例 AVC 外部点位映射未保存：{alert.text}")

    def create_group(self, group_name, *, mode, calculation_period, command_period, voltage_tag,
                     target_tag, rtu_connection, q_measurement_tag, q_setpoint_tag, kp=2,
                     member_name="inverter", q_min=0, q_max=100, rtu_target_tag=None,
                     voltage_connection=None, configure_expiry=None):
        self._open_groups()
        self._refresh_groups()
        self.group_name = group_name
        if self._group_item():
            raise AssertionError(f"现场已存在同名 AVC 控制组：{group_name}")
        self._click(self._button("+ 新增控制组", GROUP_LIST), "新增本例 AVC 控制组")
        self.group_draft_opened = True
        self._click(self._button("我已了解，继续新增", self.DIALOG_XPATH), "确认新增 AVC 控制组")
        self._fill_field(GROUP, "group_name", group_name)
        self._choose(GROUP, "id", "command_mode", MODE_LABELS[mode])
        self._choose(GROUP, "id", "control_mode", "周期直分配")
        self._set_period_fields(calculation_period, command_period)
        self._fill_field(GROUP, "voltage_meas_tag", voltage_tag)
        self._fill_field(GROUP, "voltage_meas_unit", "V")
        if mode == "voltage":
            self._fill_field(GROUP, "voltage_cmd_tag", target_tag)
            self._fill_field(GROUP, "voltage_cmd_unit", "V")
            self._fill_field(GROUP, "voltage_control_kp", kp)
            self._fill_field(GROUP, "voltage_control_deadband", 0)
        else:
            self._fill_field(GROUP, "q_total_cmd_signal_tag", target_tag)
            self._choose(GROUP, "id", "q_total_cmd_mode", "绝对值")
        voltage_source = voltage_connection or rtu_connection
        self._choose(GROUP, "aria-label", "电压量测数据来源", f"ModbusRTU / {voltage_source} / {voltage_tag}",
                     search=voltage_tag, multiple=True)
        if configure_expiry is not None:
            configure_expiry(self, GROUP)
        if rtu_target_tag is not None:
            self._choose(GROUP, "aria-label", "总无功命令数据来源", f"ModbusRTU / {rtu_connection} / {rtu_target_tag}",
                         search=rtu_target_tag, multiple=True)
        self._click((By.XPATH, GROUP + '//button[@aria-label="添加成员"]'), "新增本例 AVC 成员")
        self._fill_field(MEMBER, "member_name", member_name)
        self._fill_field(MEMBER, "q_min_kvar", q_min)
        self._fill_field(MEMBER, "q_max_kvar", q_max)
        self._bind_member("无功测量点", rtu_connection, q_measurement_tag)
        self._bind_member("无功设定点", rtu_connection, q_setpoint_tag)
        self._click(self._button("确认成员", MEMBER), "确认本例 AVC 成员")
        self._wait_draft_closed(MEMBER, "AVC 成员草稿未确认")
        self._click(self._button("保存控制组并建立路由", GROUP), "保存本例 AVC 控制组与路由")
        self.group_submitted = True
        self._wait_draft_closed(GROUP, "AVC 控制组与路由未保存")
        self.group_draft_opened = False
        item = self._until(lambda _: self._group_item(), "新建 AVC 控制组未显示")
        self.conn_id = self._item_identity(item)
        self._select_owned()
        self._check_route_save()
        if configure_expiry is not None:
            self._click(self._button("编辑", SUMMARY), "重新打开 AVC 控制组读回量测有效期")
            self.group_draft_opened = True
            configure_expiry(self, GROUP, verify=True)
            self._discard_draft(GROUP, "取消")
            self.group_draft_opened = False
        self.desktop.metadata["本例 AVC 控制组"] = {
            "组名": group_name, "连接编号": self.conn_id, "电压量测": voltage_tag,
            "目标命令": target_tag, "成员": member_name, "无功量测": q_measurement_tag,
            "无功设定": q_setpoint_tag, "RTU 连接": rtu_connection,
        }
        self.desktop.check("核对 AVC 命令模式", self._read_summary("命令模式"), MODE_LABELS[mode])
        self.desktop.check("核对 AVC 控制方式", self._read_summary("控制方式"), "周期直分配")
        self.desktop.check("核对 AVC 命令点", self._read_summary("命令点"), target_tag)
        self._assert_periods(calculation_period, command_period)
        state = self._until(self._known_state, "新建 AVC 控制组未显示明确状态")
        self.desktop.metadata["本例 AVC 控制组"]["保存后状态"] = state

    def start(self, *, clock=time.monotonic):
        self._select_owned()
        with self.desktop.step("启动本例 AVC 控制组", expected="控制状态为运行中"):
            locator = self._button("启动控制组", RUNTIME)

            def ready(driver):
                state = self._known_state(driver)
                if state == "运行中":
                    return state, None
                if state == "已停止" and (button := self._ready_button(locator)):
                    return state, button

            _, button = self._until(ready, "AVC 控制组未显示运行状态或可用的启动按钮")
            if button is None:
                return None
            started_at = clock()
            button.click()
            self._until(lambda _: self._read_summary("控制状态") == "运行中", "AVC 控制组未进入运行状态")
            return started_at

    def stop(self):
        self._select_owned()
        if self._read_summary("控制状态") != "已停止":
            self._click(self._button("停止控制组", RUNTIME), "停止本例 AVC 控制组")
            self._until(lambda _: self._read_summary("控制状态") == "已停止", "AVC 控制组未进入停止状态")

    def update_periods(self, calculation_period, command_period, *, clock=time.monotonic):
        self._select_owned()
        state = self._until(self._known_state, "修改周期前 AVC 控制组未显示明确状态")
        self.desktop.check("核对修改周期前 AVC 已停止", state, "已停止")
        self._click(self._button("编辑", SUMMARY), "编辑本例 AVC 控制组周期")
        self.group_draft_opened = True
        self._set_period_fields(calculation_period, command_period)
        with self.desktop.step("保存新的 AVC 控制周期"):
            locator = self._button("保存控制组并建立路由", GROUP)
            button = self._until(lambda _: self._ready_button(locator), "AVC 周期保存按钮不可用")
            saved_at = clock()
            button.click()
            self._wait_draft_closed(GROUP, "AVC 新周期未保存")
        self.group_draft_opened = False
        self._select_owned()
        self._check_route_save()
        self._assert_periods(calculation_period, command_period)
        state = self._until(self._known_state, "保存新周期后 AVC 控制组未显示明确状态")
        self.desktop.metadata["AVC 保存新周期后状态"] = state
        return saved_at if state == "运行中" else None

    def _discard_draft(self, root, cancel):
        if not self._visible((By.XPATH, root)):
            return
        self._click(self._button(cancel, root), "关闭本例 AVC 配置草稿")
        discard = self._button("放弃改动", self.DIALOG_XPATH)

        def closed_or_confirm(_):
            return self._visible(discard) or not self._visible((By.XPATH, root))

        result = self._until(closed_or_confirm, "AVC 草稿未关闭，也未显示放弃确认")
        if result is not True:
            self._click(discard, "放弃本例 AVC 未保存改动")
            self._wait_draft_closed(root, "AVC 草稿未关闭")

    def cleanup(self):
        self._cleaning = True
        try:
            if self.group_draft_opened:
                notice = self.DIALOG_XPATH + "[.//*[normalize-space(text())='新增 AVC 控制组前请确认']]"
                if self._visible((By.XPATH, notice)):
                    self._click(self._button("取消", notice), "关闭本例 AVC 新增确认窗口")
                    self._wait_draft_closed(notice, "AVC 新增确认窗口未关闭")
                self._discard_draft(MEMBER, "取消编辑")
                self._discard_draft(GROUP, "取消")
                self.group_draft_opened = False
            if not self.group_submitted:
                return
            self._open_groups()
            if self.conn_id is None:
                self._refresh_groups()
                item = self._group_item()
                if item is None:
                    return
                self.conn_id = self._item_identity(item)
            self.stop()
            # 页面要求先处理部分保存留下的路由，才能删除已创建的组。
            pending = self._visible((By.XPATH, ROUTE_ALERT))
            if pending:
                if f"AVC / {self.group_name}：" not in pending.text:
                    raise RuntimeError(f"待同步路由无法确认属于本例 AVC 控制组：{pending.text}")
                self._click(self._button("仅重试映射", ROUTE_ALERT), "清理前解除本例控制组的映射同步锁定")
                self._until(lambda _: not self._visible((By.XPATH, ROUTE_ALERT)),
                            "本例映射同步仍失败，上位机禁止删除控制组")
            self._click(self._button("删除", SUMMARY), "删除本例 AVC 控制组")
            self._confirm(f"确认删除 {self.group_name}？", "确认删除本例 AVC 控制组")
            self._until(lambda _: not self._group_item(), "本例 AVC 控制组未删除")
            self.group_submitted = False
        finally:
            self._cleaning = False
