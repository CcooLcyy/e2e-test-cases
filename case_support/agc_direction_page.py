"""AGC 闭锁验收的可见页面观察；不调用内部脚本、接口或发送控制。"""
from datetime import datetime
from decimal import Decimal, InvalidOperation
import logging
import re
import time

from selenium.common.exceptions import NoSuchElementException, StaleElementReferenceException, TimeoutException
from selenium.webdriver.common.by import By

from .modbus_rtu_page import ModbusRtuPage, xpath_text


LOGGER = logging.getLogger(__name__)
BLOCK_TAGS = ("AGC增闭锁", "AGC减闭锁")


def unique_match(matches, name):
    """身份不明确时不选择第一项继续执行。"""
    matches = list(matches)
    if not matches:
        raise RuntimeError(f"未找到{name}")
    if len(matches) != 1:
        raise RuntimeError(f"{name}定位不唯一，拒绝使用歧义现场")
    return matches[0]


def parse_decimal(text):
    """只解析页面明确的有限十进制工程量，禁止将缺测转成零。"""
    text = str(text).strip()
    if text.endswith("kW"):
        text = text[:-2].strip()
    if not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", text):
        raise ValueError(f"AGC 页面功率不可解析：{text!r}")
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise ValueError("AGC 页面功率不是合法十进制数") from None
    if not value.is_finite():
        raise ValueError("AGC 页面功率不是有限数")
    return value


def parse_bool(text):
    value = str(text).strip()
    if value not in ("是", "否"):
        raise ValueError(f"闭锁点没有明确 BOOL 值：{value!r}")
    return value == "是"


def _timestamp(text):
    """只接受实际日期时间，不把采集瞬间替代点值更新时间。"""
    text = str(text).strip()
    normalized = text.replace("/", "-").replace("年", "-").replace("月", "-").replace("日", " ")
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            datetime.strptime(normalized, pattern)
            return text
        except ValueError:
            pass
    raise ValueError(f"AGC 点值更新时间缺失或不可解析：{text!r}")


def parse_member_snapshot(raw):
    """校验专用单成员场景的完整可见读数。"""
    if not str(raw["caption"]).startswith("可控"):
        raise ValueError("AGC 验收成员必须参与控制")
    capacity = re.search(r"容量\s+(.+?)\s*kW", raw["caption"])
    limits = re.fullmatch(r"调节范围\s+(.+?)～(.+?)\s*kW", raw["limits"].strip())
    if not capacity or not limits:
        raise ValueError("AGC 成员容量或可调范围不可读")
    capacity_kw = parse_decimal(capacity[1])
    lower, maximum = parse_decimal(limits[1]), parse_decimal(limits[2])
    upper = capacity_kw if maximum == 0 else min(capacity_kw, maximum)
    if capacity_kw <= 0 or lower >= upper:
        raise ValueError("AGC 验收成员可调范围无效")
    for kind in ("measurement", "setpoint"):
        if raw[f"{kind}_quality"] != "有效":
            raise ValueError(f"AGC {kind}质量不是有效")
        unit, scale, offset = [item.strip() for item in raw[f"{kind}_units"].split("/")]
        # 旧协议的scale=0等价于1；只允许默认工程量换算。
        if unit != "kW" or parse_decimal(scale) not in (Decimal(0), Decimal(1)) or parse_decimal(offset) != 0:
            raise ValueError("AGC 验收要求 kW、默认倍率及零偏移")
        _timestamp(raw[f"{kind}_time"])
    return dict(measurement=parse_decimal(raw["measurement"]), setpoint=parse_decimal(raw["setpoint"]),
                measurement_quality=raw["measurement_quality"], setpoint_quality=raw["setpoint_quality"],
                measurement_time=raw["measurement_time"], setpoint_time=raw["setpoint_time"],
                lower_kw=lower, upper_kw=upper, capacity_kw=capacity_kw, absolute=True)


def select_bus_row(rows, conn_id, endpoint, tag):
    """同时用可见完整端点和表格目标身份定位本组自己的缓存。"""
    expected = re.sub(r"\s+", "", f"{endpoint} : {tag}")
    target_key = re.compile(re.escape(f"{conn_id}:{tag}-") + r"\d+$")
    candidates = [row for row in rows if re.sub(r"\s+", "", row["target"]) == expected
                  and target_key.fullmatch(row["key"].partition("->")[2])]
    row = unique_match(candidates, f"数据总线目标 {endpoint} / {tag}")
    if row["quality"] != "正常":
        raise ValueError(f"闭锁点质量不是正常：{row['quality']}")
    return dict(value=parse_bool(row["value"]), quality=row["quality"], timestamp=_timestamp(row["timestamp"]))


def _direction_point_acceptable(point):
    """展示陈旧不等于业务失效，变化点的新时间由主站观察器按操作基线验证。"""
    # 只豁免明确的展示年龄提示，时间未知/超前、质量异常和未知新提示都不放行。
    issues = {text.strip() for text in point["issues"].split("；")}
    return point["quality"] == "有效" and issues <= {"无", "数据陈旧"}


class AgcDirectionPage(ModbusRtuPage):
    """只读已有专用组，接管身份后不创建、修改或删除被测配置。"""

    def __init__(self, desktop):
        super().__init__(desktop)
        self.group_name = None
        self.conn_id = None
        self._observation_deadline = None

    def _until(self, predicate, message="", timeout=None):
        if self._observation_deadline is not None:
            remaining = self._observation_deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutException("AGC只读观察已达到本轮截止时间")
            timeout = min(float(timeout or self.desktop.timeout), remaining)
        return super()._until(predicate, message, timeout)

    def connect_device(self):
        self.connect_site()
        with self.desktop.step("确认 AGC、DataCenter、IEC104 模块已运行"):
            def ready(_):
                for name in ("AGC", "DataCenter", "IEC104"):
                    row = self._visible((By.XPATH, "//div[contains(@class,'module-ops-list-card')]//tr[@data-row-key=" + xpath_text(name) + "]"))
                    if row is None or "运行中" not in row.text:
                        return False
                    if not any(button.is_enabled() for button in row.find_elements(By.XPATH, ".//button[normalize-space(.)='停止']")):
                        return False
                return True
            self._until(ready, "专用AGC测试所需模块未运行；本例不自动启停模块")

    def _control_view(self, view):
        self._menu("控制策略")
        root = "//*[@aria-label='控制页面切换']"
        for label in ("AGC", view):
            self._click((By.XPATH, root + "//*[@role='tab'][normalize-space(.)=" + xpath_text(label) + "]"), f"切换 AGC {label} 页签")

    def _group_item(self, group_name):
        return self._visible((By.XPATH, "//div[contains(@class,'ant-card')][./div[contains(@class,'ant-card-head')]//*[normalize-space(.)='控制组列表']]//li[contains(@class,'ant-list-item')][.//span[normalize-space(.)=" + xpath_text(group_name) + "]]"))

    def select_group(self, group_name, expected_conn_id=None):
        self._control_view("控制策略")
        item = self._until(lambda _: self._group_item(group_name), "专用AGC控制组不存在")
        match = re.search(r"conn_id=(\d+)\s*\|", item.text)
        if not match:
            raise RuntimeError("控制组列表缺少连接身份")
        conn_id = int(match[1])
        if expected_conn_id is not None and conn_id != int(expected_conn_id):
            raise RuntimeError("AGC 控制组 conn_id 与测试配置不一致")
        with self.desktop.step("选择并核对专用 AGC 控制组", expected=f"{group_name}, conn_id={conn_id}"):
            item.click()
            def matching_overview(_):
                # 切组会按组名重建React节点，每轮重新定位，不能反复读取旧节点。
                node = self._visible((By.CSS_SELECTOR, 'section[aria-label="AGC 功率总览"]'))
                return node if node is not None and node.find_element(By.CSS_SELECTOR, ".agc-runtime-heading h4").text == group_name else False
            overview = self._until(matching_overview, "AGC运行总览未切换到指定组")
            text = overview.find_element(By.CSS_SELECTOR, ".agc-runtime-heading").text
            if not all(label in text for label in ("运行中", "功能投入", "远方")):
                raise AssertionError("AGC 验收要求控制组运行、功能投入并允许远方")
            if "绝对值" not in overview.find_element(By.CSS_SELECTOR, ".agc-runtime-context").text:
                raise AssertionError("AGC 验收要求总有功命令为绝对值模式")
            self.group_name, self.conn_id = group_name, conn_id
            self.desktop.metadata["AGC专用控制组"] = dict(group_name=group_name, conn_id=conn_id)

    def _require_group(self):
        if self.group_name is None or self.conn_id is None:
            raise RuntimeError("尚未选择并确认专用AGC控制组")

    @staticmethod
    def _description(root, label):
        return unique_match(root.find_elements(By.XPATH, ".//*[contains(@class,'ant-descriptions-item')][./*[contains(@class,'ant-descriptions-item-label') and normalize-space(.)=" + xpath_text(label) + "]]/*[contains(@class,'ant-descriptions-item-content')]"), label).text.strip()

    @staticmethod
    def _row_values(table, row):
        headers = [node.text.strip() for node in table.find_elements(By.CSS_SELECTOR, ".ant-table-thead > tr > th")]
        cells = row.find_elements(By.CSS_SELECTOR, ":scope > td")
        if len(headers) != len(cells) or len(set(headers)) != len(headers):
            raise RuntimeError("AGC 表格列不完整或不唯一")
        return dict(zip(headers, (cell.text.strip() for cell in cells)))

    def read_member(self, member_name):
        self._require_group()
        overview = self._visible((By.CSS_SELECTOR, 'section[aria-label="AGC 功率总览"]'))
        if overview is None or overview.find_element(By.CSS_SELECTOR, ".agc-runtime-heading h4").text != self.group_name:
            self.select_group(self.group_name, self.conn_id)
        else:
            item = self._group_item(self.group_name)
            match = re.search(r"conn_id=(\d+)\s*\|", item.text) if item is not None else None
            if not match or int(match[1]) != self.conn_id:
                raise RuntimeError("AGC 控制组身份在观察过程中变化")
        table = self._until(lambda _: self._visible((By.CSS_SELECTOR, ".agc-runtime-members")))
        rows = [row for row in table.find_elements(By.CSS_SELECTOR, ".ant-table-tbody > tr[data-row-key]")
                if "ant-table-expanded-row" not in (row.get_attribute("class") or "")]
        if len(rows) != 1:
            raise AssertionError("AGC 增减闭锁用例要求专用单成员控制组")
        row = unique_match([row for row in rows if any(node.text.strip() == member_name for node in row.find_elements(By.CSS_SELECTOR, "strong"))], "AGC成员")
        values = self._row_values(table, row)
        captions = [node.text.strip() for node in row.find_elements(By.CSS_SELECTOR, ".agc-runtime-member-caption")]
        capacity = unique_match([text for text in captions if "容量" in text], "成员容量")
        if "（增量）" in values["已发布设定"]:
            raise ValueError("本组验收要求成员绝对值设定")
        # 组名和成员名由精确XPath比较，避免特殊字符进入CSS选择器。
        expand = (By.XPATH, "//button[@aria-label=" + xpath_text(f"展开成员 {member_name}") + "]")
        if self._visible(expand):
            self._click(expand, f"读取成员 {member_name} 详细质量与约束")
        member_key = row.get_attribute("data-row-key")
        detail_row = self._until(lambda _: table.find_element(By.XPATH, ".//tr[@data-row-key=" + xpath_text(member_key) + "]/following-sibling::tr[contains(@class,'ant-table-expanded-row')][1]"))
        details = {}
        for title, kind in (("测量点", "measurement"), ("已发布设定点", "setpoint")):
            block = unique_match(detail_row.find_elements(By.XPATH, ".//div[contains(@class,'ant-descriptions')][./div[contains(@class,'ant-descriptions-header')]//div[normalize-space(.)=" + xpath_text(title) + "]]"), title)
            details[f"{kind}_quality"] = self._description(block, "质量")
            details[f"{kind}_time"] = self._description(block, "点值时间")
            details[f"{kind}_units"] = self._description(block, "配置单位 / 缩放 / 偏移")
        limits = unique_match([node.text.strip() for node in detail_row.find_elements(By.CSS_SELECTOR, ".ant-typography") if node.text.strip().startswith("调节范围 ")], "成员可调范围")
        return parse_member_snapshot(dict(measurement=values["实测"], setpoint=values["已发布设定"], caption=capacity, limits=limits, **details))

    def read_default_bool(self, tag):
        self._require_group()
        if tag not in BLOCK_TAGS:
            raise ValueError("只读取已确认的AGC增减闭锁默认点")
        self._control_view("默认点")
        item = self._until(lambda _: self._group_item(self.group_name))
        if f"conn_id={self.conn_id} |" not in item.text:
            raise RuntimeError("默认点页面控制组身份已变化")
        item.click()
        row = self._until(lambda _: self._visible((By.XPATH, "//div[contains(@class,'protocol-point-card')]//tr[@data-row-key=" + xpath_text(f"{9 if tag == BLOCK_TAGS[0] else 10}-{tag}") + "]")))
        table = self._visible((By.CSS_SELECTOR, ".protocol-point-card"))
        return parse_bool(self._row_values(table, row)["最新值"])

    def read_bus_point(self, tag):
        self._require_group()
        self._menu("数据总线")
        self._click((By.XPATH, "//*[@aria-label='数据总线页面切换']//*[@role='tab'][normalize-space(.)='运行监视']"), "读取数据总线闭锁质量与时间")
        search = self._until(lambda _: self._visible((By.CSS_SELECTOR, 'input[placeholder="筛选标签或连接编号"]')))
        self._replace(search, tag)
        card = self._until(lambda _: self._visible((By.XPATH, "//div[contains(@class,'ant-card')][./div[contains(@class,'ant-card-head')]//span[normalize-space(.)='实时值']]")))
        rows = []
        for row in card.find_elements(By.CSS_SELECTOR, ".ant-table-tbody > tr[data-row-key]"):
            cells = self._row_values(card, row)
            rows.append(dict(key=row.get_attribute("data-row-key"), target=cells["目标"], value=cells["当前值"], quality=cells["质量"], timestamp=cells["数据时间"]))
        return select_bus_row(rows, self.conn_id, f"AGC/{self.group_name}", tag)

    def read_direction_point(self, tag):
        """读取本组运行点值明细，不切换到数据总线或使用非本组的转发缓存。"""
        self._require_group()
        if tag not in BLOCK_TAGS:
            raise ValueError("只读取已确认的AGC增减闭锁默认点")
        overview = self._visible((By.CSS_SELECTOR, 'section[aria-label="AGC 功率总览"]'))
        if overview is None:
            raise RuntimeError("AGC运行总览不在当前可见页面")
        button = self._button("查看点值明细", "//section[@aria-label='AGC 功率总览']")
        if self._visible(button):
            self._click(button, "展开本组运行点值明细")
        table = self._until(lambda _: self._visible((By.CSS_SELECTOR, '[aria-label="运行点值明细"]')))
        row = unique_match(table.find_elements(By.XPATH, ".//tr[@data-row-key=" + xpath_text(tag) + "]"), f"本组运行点 {tag}")
        values = self._row_values(table, row)
        return dict(value=parse_bool(values["缓存值"]), quality=values["质量"],
                    timestamp=_timestamp(values["点值时间"]), issues=values["问题"])

    def wait_snapshot(self, profile, target, increase, decrease, timeout):
        """轮询只读页面，不重发遥调；超时保留最后实际观察。"""
        deadline = time.monotonic() + float(timeout)
        target = Decimal(str(target))
        last = None
        with self.desktop.step("等待 AGC 已发布设定与闭锁状态一致", expected=f"设定={target}, 增闭锁={increase}, 减闭锁={decrease}"):
            entry = self.desktop.steps[-1]
            self._observation_deadline = deadline
            try:
                self.select_group(profile["group_name"], profile.get("conn_id"))
                while time.monotonic() < deadline:
                    self.desktop._check_stop()
                    try:
                        member = self.read_member(profile["member_name"])
                        inc_bus, dec_bus = self.read_direction_point(BLOCK_TAGS[0]), self.read_direction_point(BLOCK_TAGS[1])
                        inc, dec = inc_bus["value"], dec_bus["value"]
                        last = dict(member=member, increase=inc, decrease=dec, increase_point=inc_bus, decrease_point=dec_bus)
                        entry["实际"] = str(last)
                        if member["lower_kw"] != Decimal(str(profile["lower_kw"])) or member["upper_kw"] != Decimal(str(profile["upper_kw"])):
                            raise AssertionError("AGC 成员有效上下限与固定验收范围不一致")
                        if (member["setpoint"] == target and inc is increase and dec is decrease
                                and all(_direction_point_acceptable(point) for point in (inc_bus, dec_bus))):
                            LOGGER.info("AGC 页面已确认当前下发目标与闭锁：%s", last)
                            return last
                    except (NoSuchElementException, StaleElementReferenceException):
                        # 只容忍页面刷新中的短暂元素失效，身份歧义或坏质量不被重试掩盖。
                        pass
                    time.sleep(min(0.25, max(0, deadline - time.monotonic())))
            except TimeoutException as error:
                entry["实际"] = f"最后观察={last}；窗口等待截止：{error}"
            finally:
                self._observation_deadline = None
            raise AssertionError(f"AGC 设定与闭锁等待超时，最后观察：{last}")
