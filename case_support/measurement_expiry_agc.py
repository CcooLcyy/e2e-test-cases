"""Configure and observe the dedicated two-member AGC group through DSP Upper."""

from selenium.webdriver.common.by import By

from .agc_direction_page import unique_match
from .agc_direction_setup import AgcDirectionSetup
from .modbus_rtu_page import xpath_text


class MeasurementExpiryAgc(AgcDirectionSetup):
    COMMUNICATION_TIMEOUT_MS = 60000
    MEMBER_POINTS = (
        ("有功测量点", "测量点", "measured"),
        ("有功设定点", "设定点", "setpoint"),
        ("通信状态点（可选）", "通信状态", "__communication_state"),
    )
    MEMBER_CARD = "//article[contains(concat(' ',normalize-space(@class),' '),' agc-group-member ')]"
    RUNTIME_TABLE = (By.XPATH, AgcDirectionSetup.OVERVIEW + "//div[contains(@class,'agc-runtime-members')]")
    RUNTIME_ROWS = ".ant-table-tbody > tr[data-row-key]"

    def __init__(self, desktop):
        super().__init__(desktop)
        self.members = {}

    def _choose_equal_allocation(self):
        locator = (By.XPATH, self.GROUP_DIALOG + "//label[normalize-space(.)='分配方式']"
                   "/ancestor::div[contains(concat(' ',normalize-space(@class),' '),' ant-form-item ')][1]"
                   "//input[@role='combobox']")
        field = self._until(lambda _: self._visible(locator), "AGC 分配方式未显示")
        field_id = field.get_attribute("id")
        if not field_id:
            raise RuntimeError("AGC 分配方式缺少下拉框标识")
        self._choose(self.GROUP_DIALOG, field_id, "平均分配（等权）")

    def _open_member_advanced(self):
        locator = (By.XPATH, self.MEMBER_DIALOG + "//summary[normalize-space(.)='更多控制参数']")
        summary = self._until(lambda _: self._visible(locator), "AGC 成员更多控制参数未显示")
        if summary.find_element(By.XPATH, "..").get_attribute("open") is None:
            self._click(locator, "展开 AGC 成员更多控制参数")

    def _add_member(self, member_name, connection_name, configure_expiry):
        self._click((By.XPATH, self.GROUP_DIALOG + "//button[@aria-label='添加成员']"), f"添加 AGC 成员 {member_name}")
        self._fill_group_field(self.MEMBER_DIALOG, "member_name", member_name)
        for field, value in (("capacity_kw_decimal", 100), ("min_kw_decimal", 10), ("max_kw_decimal", 100)):
            self._fill_group_field(self.MEMBER_DIALOG, field, value)
        connection = f"ModbusRTU / {connection_name}"
        for label, _, tag in self.MEMBER_POINTS:
            self._choose(self.MEMBER_DIALOG, f"{label}连接", connection, search=connection_name, accessible_name=True)
            self._choose(self.MEMBER_DIALOG, label, f"{connection} / {tag}", search=tag, accessible_name=True)
        self._fill_group_field(self.MEMBER_DIALOG, "communication_timeout_ms", self.COMMUNICATION_TIMEOUT_MS)
        self._open_member_advanced()
        self._choose(self.MEMBER_DIALOG, "p_set_mode", "绝对值")
        configure_expiry(self, self.MEMBER_DIALOG)
        self._click(self._button("确认成员", self.MEMBER_DIALOG), f"确认 AGC 成员 {member_name}")
        self._wait_dialog_closed(self.MEMBER_DIALOG, "确认成员")

    def _record_group(self, item):
        self.group = dict(group_name=self.name, members=dict(self.members), conn_id=self._read_conn_id(item),
                          lower_kw=10, upper_kw=100, cleanup="待清理")
        self.desktop.metadata["本例 AGC 控制组"] = self.group

    def create_expiry_group(self, group_name, members, configure_expiry):
        """Bind each member to its own RTU link and return the saved group and command tag."""
        if self._draft_opened or self._save_requested or self.group is not None:
            raise RuntimeError("本例 AGC 控制组已开始创建")
        if len(members) != 2 or len(set(members.values())) != 2:
            raise ValueError("本例 AGC 需要两个成员各自使用独立 RTU 连接")
        self.name = group_name
        self.members = dict(members)
        self._control_view("控制策略")
        self._refresh_groups()
        if self._group_item(group_name):
            raise RuntimeError(f"AGC 已存在同名控制组：{group_name}")

        with self.desktop.step("新增本例 AGC 双成员控制组", expected={
            "组": group_name, "成员与 RTU 连接": self.members, "模式": "周期直分配，绝对值，等权",
            "成员容量及范围 kW": "100，10～100", "通信心跳超时毫秒": self.COMMUNICATION_TIMEOUT_MS,
        }):
            self._click(self._button("+ 新增控制组", self.LIST), "新增专用 AGC 控制组")
            self._draft_opened = True
            self._fill_group_field(self.GROUP_DIALOG, "group_name", group_name)
            self._choose(self.GROUP_DIALOG, "control_mode", "周期直分配")
            self._choose(self.GROUP_DIALOG, "p_cmd_mode", "绝对值")
            self._choose_equal_allocation()
            self._members_tab()
            for member_name, connection_name in self.members.items():
                self._add_member(member_name, connection_name, configure_expiry)
            self._save_requested = True
            self._click(self._button("保存控制组并建立路由", self.GROUP_DIALOG), "保存控制组及两条 RTU 链路的路由")
            self._wait_dialog_closed(self.GROUP_DIALOG, "保存控制组")
            self._draft_opened = False
            item = self._until(lambda _: self._group_item(group_name), "保存后未显示本例 AGC 控制组")
            self._record_group(item)

        with self.desktop.step("读取已保存的 AGC 配置和实际点位"):
            overview = self._select_owned_group()
            self.desktop.check("核对 AGC 控制与分配模式", overview.find_element(By.CSS_SELECTOR, ".agc-runtime-context").text,
                               "周期直分配 · 平均分配（等权） · 绝对值")
            route_error = self._visible((By.XPATH, "//*[@role='alert'][.//*[normalize-space(.)='控制组配置已保存，映射尚未同步完成']]"))
            if route_error:
                raise AssertionError("AGC 自动路由保存失败：" + route_error.text)
            self.group.update(self._read_point_specs())
            self._verify_members(configure_expiry)
        return dict(self.group)

    def _verify_members(self, configure_expiry):
        self._click(self._button("编辑", self.OVERVIEW), "重新打开 AGC 控制组读取已保存映射")
        self._draft_opened = True
        self._members_tab()
        cards = self.driver.find_elements(By.XPATH, self.GROUP_DIALOG + self.MEMBER_CARD)
        self.desktop.check("核对本例 AGC 成员", sorted(card.get_attribute("aria-label") for card in cards),
                           sorted(f"成员 {name}" for name in self.members))
        for name, connection_name in self.members.items():
            edit = (By.XPATH, self.GROUP_DIALOG + "//button[@aria-label=" + xpath_text(f"编辑成员 {name}") + "]")
            self._until(lambda _: (button := self._visible(edit)) and button.is_enabled(), f"成员 {name} 的映射尚未就绪")
            card = self._visible((By.XPATH, self.GROUP_DIALOG + self.MEMBER_CARD + "[@aria-label=" + xpath_text(f"成员 {name}") + "]"))
            self.desktop.check(f"核对成员 {name} 可控状态", card.find_element(By.CSS_SELECTOR, ".ant-tag").text, "可控")
            summary = [node.text for node in card.find_elements(By.CSS_SELECTOR, ".agc-group-member__summary > span")]
            self.desktop.check(f"核对成员 {name} 容量、范围和分配比例", summary,
                               ["额定容量 100.00 kW", "调节范围 10.00～100.00 kW", "理论占比 50.0%"])
            for _, label, tag in self.MEMBER_POINTS:
                binding = unique_match(card.find_elements(By.XPATH, ".//div[contains(concat(' ',normalize-space(@class),' '),"
                                       "' agc-group-member__point ')][./span[normalize-space(.)=" + xpath_text(label) + "]]/div/div"), label)
                self.desktop.check(f"核对成员 {name} 的{label}映射", binding.text, f"ModbusRTU / {connection_name} / {tag}")
            self._click(edit, f"读取成员 {name} 已保存参数")
            timeout = (By.XPATH, self.MEMBER_DIALOG + "//input[@id='communication_timeout_ms']")
            field = self._until(lambda _: self._visible(timeout), f"成员 {name} 的通信心跳超时未显示")
            self.desktop.check(f"核对成员 {name} 通信心跳超时", field.get_attribute("value"), str(self.COMMUNICATION_TIMEOUT_MS))
            self._open_member_advanced()
            mode = (By.XPATH, self.MEMBER_DIALOG + "//input[@id='p_set_mode']/ancestor::div[contains(concat(' ',normalize-space(@class),' '),"
                    "' ant-select ')][1]//*[@title='绝对值']")
            self._until(lambda _: self._visible(mode), f"成员 {name} 的指令未保存为绝对值")
            configure_expiry(self, self.MEMBER_DIALOG, verify=True)
            self._close_draft(self.MEMBER_DIALOG, "取消编辑")
        self._close_draft(self.GROUP_DIALOG, "取消")
        self._draft_opened = False

    def _member_runtime(self, name, point_title):
        self._require_group()
        if name not in self.members:
            raise ValueError(f"成员 {name} 不属于本例 AGC 控制组")
        if self._selected_overview() is None:
            self.select_group(self.name, self.conn_id)
        item = self._group_item(self.name)
        if item is None or self._read_conn_id(item) != self.conn_id:
            raise RuntimeError("AGC 控制组身份在观察过程中变化")
        expand = (By.XPATH, self.OVERVIEW + "//button[@aria-label=" + xpath_text(f"展开成员 {name}") + "]")
        if self._visible(expand):
            self._click(expand, f"展开 AGC 成员 {name} 运行点值")

        def read(_):
            table = self._visible(self.RUNTIME_TABLE)
            if table is None:
                return False
            rows = table.find_elements(By.CSS_SELECTOR, self.RUNTIME_ROWS)
            row = unique_match((row for row in rows if any(node.text == name for node in row.find_elements(By.CSS_SELECTOR, "strong"))),
                               f"AGC 成员 {name}")
            row_key = xpath_text(row.get_attribute("data-row-key"))
            point = (".//tr[@data-row-key=" + row_key + "]/following-sibling::tr[1][contains(@class,'ant-table-expanded-row')]"
                     "//div[contains(@class,'ant-descriptions')][./div[contains(@class,'ant-descriptions-header')]"
                     "//div[normalize-space(.)=" + xpath_text(point_title) + "]]")
            blocks = [block for block in table.find_elements(By.XPATH, point) if block.is_displayed()]
            if not blocks:
                return False
            block = unique_match(blocks, f"成员 {name} 的{point_title}")
            return self._row_values(table, row), dict(tag=self._description(block, "本地点名"),
                                                      quality=self._description(block, "质量"),
                                                      timestamp=self._description(block, "点值时间"))

        return self._until(read, f"成员 {name} 的{point_title}运行详情未显示")

    def read_member_communication(self, name):
        """Read the communication heartbeat's condition, quality and visible timestamp."""
        values, details = self._member_runtime(name, "通信状态点")
        condition, _, reason = values["通信参与条件"].partition("\n")
        return dict(condition=condition, reason=reason, **details)

    def read_member_measurement(self, name):
        """Read the AGC member measurement even when its value or quality is invalid."""
        values, details = self._member_runtime(name, "测量点")
        return dict(value=values["实测"], status=values["测量状态"], **details)

    def cleanup(self):
        if self._save_requested and self.group is None:
            self._cleaning = True
            try:
                self._close_draft(self.MEMBER_DIALOG, "取消编辑")
                self._close_draft(self.GROUP_DIALOG, "取消")
                self._draft_opened = False
                self._menu("数据总线")
                self._control_view("控制策略")
                self._refresh_groups()
                item = self._group_item(self.name)
                if item is None:
                    self._save_requested = False
                    return
                item.click()
                overview = self._until(lambda _: self._selected_overview(), "未能核对本例待清理 AGC 控制组")
                members = [node.text for node in overview.find_elements(By.CSS_SELECTOR, ".agc-runtime-members tr[data-row-key] strong")]
                if sorted(members) != sorted(self.members):
                    raise RuntimeError(f"无法确认待清理组 {self.name} 的专用成员：{members}")
                self._record_group(item)
            finally:
                self._cleaning = False
        super().cleanup()
