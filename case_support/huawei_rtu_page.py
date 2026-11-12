"""华为行为模型的上位机点表适配，业务数值只读取可见运行表。"""
from dataclasses import asdict, is_dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
import json
import logging

from selenium.webdriver.common.by import By

from .modbus_rtu_page import ModbusRtuPage, xpath_text


LOGGER = logging.getLogger(__name__)
FUNCTION_LABELS = {3: "0x03 读保持寄存器", 6: "0x06 写单寄存器", 16: "0x10 写多寄存器"}


def normalize_point(spec):
    """在产生页面副作用之前校验目录描述，避免隐式改变字宽或BOOL语义。"""
    value = asdict(spec) if is_dataclass(spec) else dict(spec)
    for name in ("key", "name"):
        if not isinstance(value.get(name), str) or not value[name].strip():
            raise ValueError(f"华为点位{name}不能为空")
    value.setdefault("function", 3)
    value.setdefault("offset", 0)
    value.setdefault("bit_index", None)
    value.setdefault("enum_value", None)
    value.setdefault("unit", "")
    function, dtype = value["function"], value.get("data_type")
    if type(function) is not int or function not in FUNCTION_LABELS:
        raise ValueError("华为点表仅支持0x03读取及0x06/0x10控制配置")
    if dtype not in ("UINT16", "INT16", "UINT32", "INT32", "BOOL"):
        raise ValueError("华为点表数据类型不受支持")
    count = 2 if dtype in ("UINT32", "INT32") else 1
    if type(value.get("reg_count")) is not int or value["reg_count"] != count:
        raise ValueError("华为点位寄存器数与数据类型不一致")
    address = value.get("address")
    if type(address) is not int or not 0 <= address <= 65536 - count:
        raise ValueError("华为点位地址或多字范围越界")
    if function == 6 and count != 1:
        raise ValueError("写单寄存器不能配置32位点")
    try:
        scale, offset = Decimal(str(value.get("scale", "1"))), Decimal(str(value["offset"]))
        if not scale.is_finite() or not offset.is_finite() or scale == 0:
            raise ValueError()
    except (InvalidOperation, ValueError):
        raise ValueError("华为点位倍率和偏移必须为有限数值，倍率不能为0") from None
    value["scale"] = str(scale)
    if dtype == "BOOL":
        bit, enum = value["bit_index"], value["enum_value"]
        if function != 3 or (bit is None) == (enum is None):
            raise ValueError("状态BOOL须为读取点且只能配置位索引或整字相等值之一")
        if bit is not None and (type(bit) is not int or not 0 <= bit <= 15):
            raise ValueError("华为状态位索引必须在0至15之间")
        if enum is not None and (type(enum) is not int or not 0 <= enum <= 65535):
            raise ValueError("华为状态整字相等值必须在0至65535之间")
    elif value["bit_index"] is not None or value["enum_value"] is not None:
        raise ValueError("非BOOL点不能配置状态位或整字相等值")
    return value


def parse_visible_value(text):
    """只转换协议页面明确的true/false文本，不将未知状态变成布尔。"""
    return {"true": True, "false": False}.get(text.strip(), text.strip())


class HuaweiRtuPage(ModbusRtuPage):
    def copy_connection(self, source, name, serial_device, station_id):
        self.select(source)
        source_config = self.owned[source]
        copy_name = f"{source}_copy"
        copy_button = (By.XPATH, "//button[@aria-label=" + xpath_text(f"复制连接 {source}") + "]")
        copies = (By.XPATH, "//button[@aria-label=" + xpath_text(f"复制连接 {copy_name}")
                  + " or starts-with(@aria-label," + xpath_text(f"复制连接 {copy_name}_") + ")]")
        with self.desktop.step("确认华为副本及目标连接名称未被占用", actual=[copy_name, name]):
            if (self.driver.find_elements(*copies) or self.driver.find_elements(*self._connection_locator(name))
                    or copy_name in self.owned or name in self.owned):
                raise AssertionError("华为副本或目标连接名称已存在，拒绝覆盖")

        self.pending_creation = copy_name
        self._click(copy_button, f"复制本例华为 RTU 连接 {source}")
        conn_id = self._read_connection_identity(copy_name, "读取华为连接副本身份")
        if any(conn_id == link["conn_id"] for link in self.owned.values()):
            raise AssertionError(f"华为连接副本 {copy_name} 的编号与已登记连接重复")
        copy = {"name": copy_name, "conn_id": conn_id,
                "fields": {**source_config["fields"], "conn_name": copy_name},
                "options": dict(source_config["options"]),
                "points": {tag: dict(point) for tag, point in source_config["points"].items()}}
        self.owned[copy_name] = copy
        self.current = copy_name
        self.pending_creation = None
        self.desktop.metadata.setdefault("本例RTU连接", []).append(
            {"name": copy_name, "conn_id": conn_id, "复制来源": source, "目标名称": name, "清理状态": "待清理"})

        stopped = (By.XPATH, "//div[contains(@class,'modbus-connection-card')]"
                   "//div[contains(@class,'ant-card-head-title')]"
                   "//span[contains(@class,'ant-tag') and normalize-space(.)='已停止']")
        self.desktop.check("确认华为连接副本处于停止状态", bool(self._visible(stopped)), True)
        self._click(self._button("编辑配置", "//div[contains(@class,'modbus-connection-card')]"), "编辑华为连接副本")
        fields = {"conn_name": name, "serial_device": serial_device, "device_id": station_id}
        for field, value in fields.items():
            self._fill(field, value, field)
        copy["pending_name"] = name
        self._submit("保存修改")
        self._record_connection_rename(copy_name, name)
        copy["fields"].update(fields)
        self.assert_saved_connection()

    def add_huawei_point(self, spec, tag):
        point = normalize_point(spec)
        if not isinstance(tag, str) or not tag.strip() or len(tag) > 128:
            raise ValueError("华为点位Tag须为1至128字符")
        self._assert_identity()
        if tag in self.owned[self.current]["points"]:
            raise ValueError("拒绝覆盖本例已有Tag")
        self._click(self._button("添加点位", "//div[contains(@class,'protocol-point-card')]"), "新增华为点位")
        self._fill("tag", tag, "Tag")
        self._select_option("function", FUNCTION_LABELS[point["function"]])
        self._fill("address", point["address"], "协议地址")
        self._select_option("data_type", point["data_type"])
        self._select_option("reg_count", f"{point['reg_count']} 个寄存器")
        if point["data_type"] == "BOOL":
            mode = "位索引" if point["bit_index"] is not None else "整字相等值"
            self._select_option("bool_mode", mode)
            field = "bit_index" if point["bit_index"] is not None else "enum_value"
            self._fill(field, point[field], "状态解析值")
        else:
            self._fill("scale", point["scale"], "缩放系数")
            self._fill("offset", point["offset"], "偏移量")
            if point["function"] == 3:
                self._fill("deadband", 0, "死区")
        if point["reg_count"] == 2:
            self._select_option("word_order", "HL")
        self._select_option("byte_order", "AB")
        self._submit("添加点位")
        edit = (By.XPATH, "//button[@aria-label=" + xpath_text("编辑点位 " + tag) + "]")
        self._until(lambda _: self._visible(edit), f"保存后点表未显示华为采集点 {tag}")
        point["tag"] = tag
        self.owned[self.current]["points"][tag] = point
        self.desktop.metadata.setdefault("华为配置点表", {}).setdefault(self.current, []).append(dict(point))
        LOGGER.info("已配置华为点位：连接=%s，Tag=%s，地址=%s，类型=%s，功能码=%s",
                    self.current, tag, point["address"], point["data_type"], point["function"])

    def assert_saved_configuration(self):
        """导航离开后重新读取连接及全部点位，分别核对读点、写点和BOOL解析。"""
        name = self.current
        self._menu("模块运维")
        self._rtu()
        self.select(name)
        self.assert_saved_connection()
        for tag, point in self.owned[name]["points"].items():
            locator = (By.XPATH, "//button[@aria-label=" + xpath_text("编辑点位 " + tag) + "]")
            self._click(locator, f"重新读取华为点位 {tag}")
            for field, expected in (("tag", tag), ("address", point["address"])):
                self._assert_input(field, expected)
            self._assert_select("function", FUNCTION_LABELS[point["function"]])
            self._assert_select("data_type", point["data_type"])
            self._assert_select("reg_count", f"{point['reg_count']} 个寄存器")
            if point["data_type"] == "BOOL":
                field = "bit_index" if point["bit_index"] is not None else "enum_value"
                self._assert_select("bool_mode", "位索引" if field == "bit_index" else "整字相等值")
                self._assert_input(field, point[field])
            else:
                self._assert_input("scale", point["scale"])
                self._assert_input("offset", point["offset"])
                if point["function"] == 3:
                    self._assert_input("deadband", 0)
            if point["reg_count"] == 2:
                self._assert_select("word_order", "HL")
            self._assert_select("byte_order", "AB")
            self._close_check_dialog()
        LOGGER.info("华为连接及点表重新读取核对完成：%s，点数=%s", name, len(self.owned[name]["points"]))

    def assert_saved_connection(self):
        self._assert_identity()
        config = self.owned[self.current]
        self._click(self._button("编辑配置", "//div[contains(@class,'modbus-connection-card')]"), "重新读取华为连接配置")
        for field, expected in config["fields"].items():
            self._assert_input(field, expected)
        for field, expected in config["options"].items():
            self._assert_select(field, expected)
        self._close_check_dialog()

    def _close_check_dialog(self):
        self._click(self._button("取消", self.DIALOG_XPATH), "关闭华为配置核对窗口")
        self._until(lambda _: not self._visible(self.DIALOG), "核对窗口未关闭")

    def _read_point_table_rows(self):
        # PointTable当前pagination=false，按同一DOM任务读取全部行；不访问React或接口缓存。
        table = self.driver.find_element(By.CSS_SELECTOR, ".protocol-point-card .ant-table-container")
        return self.driver.execute_script("""
            const root = arguments[0];
            const headers = [...root.querySelectorAll('thead th')].map(n => n.innerText.trim());
            return [...root.querySelectorAll('tbody tr[data-row-key]')].map(row => ({
                tag: row.getAttribute('data-row-key'),
                cells: [...row.querySelectorAll('td')].map(n => n.innerText.trim()), headers
            }));
        """, table)

    def read_point_configuration(self):
        """整表回读复制前后的可见配置，避免逐个重开副本点位编辑框。"""
        self._assert_identity()
        card = "//div[contains(@class,'protocol-point-card')]"
        config_view = (By.XPATH, card + "//label[normalize-space(.)='配置视图']")
        choice = self._until(lambda _: self._visible(config_view), "华为点表未显示配置视图")
        if "ant-segmented-item-selected" not in (choice.get_attribute("class") or ""):
            self._click(config_view, "显示华为点表配置视图")
        show_fields = self._button("显示工程量/字节序", card)
        if self._visible(show_fields):
            self._click(show_fields, "显示华为点表工程量和字节序")
        expected_tags = set(self.owned[self.current]["points"])
        with self.desktop.step(f"整表回读华为连接 {self.current} 的点位配置", expected=f"{len(expected_tags)} 个点位"):
            rows = self._until(lambda _: self._read_point_table_rows(), "华为配置点表未显示任何点位")
            configuration = {}
            for row in rows:
                tag = row["tag"]
                fields = dict(zip(row["headers"], row["cells"]))
                assert tag not in configuration, f"华为配置点表 Tag 重复：{tag}"
                assert {"标签", "地址", "功能码", "寄存器数", "BOOL 解析", "数据类型", "缩放系数",
                        "偏移量", "死区", "字序", "字节序"} <= fields.keys(), "华为配置点表列不完整"
                configuration[tag] = {label: value for label, value in fields.items() if label != "操作"}
            self.desktop.check("核对华为点表全部标签", sorted(configuration), sorted(expected_tags))
            return configuration

    def _read_visible_rows(self):
        rows = self._read_point_table_rows()
        observed_at = datetime.now().astimezone().isoformat(timespec="milliseconds")
        values = {}
        for row in rows:
            tag = row["tag"]
            if tag in values:
                raise AssertionError(f"运行点表Tag不唯一：{tag}")
            columns = dict(zip(row["headers"], row["cells"]))
            required = ("实时值", "质量", "时间", "地址", "功能码")
            if any(name not in columns for name in required):
                raise AssertionError("华为运行点表缺少数值、质量、时间或地址列")
            values[tag] = {"tag": tag, "value": parse_visible_value(columns["实时值"]),
                           "display_value": columns["实时值"], "quality": columns["质量"],
                           "timestamp": columns["时间"], "address": columns["地址"],
                           "function": columns["功能码"], "observed_at": observed_at,
                           "connection": self.current, "conn_id": self._selected_conn_id()}
        return values

    def observe_device(self, tags):
        """一次读取本台所有请求点；任何点缺失不能以部分结果判为成功。"""
        if not isinstance(tags, dict) or not tags or len(set(tags.values())) != len(tags):
            raise ValueError("华为观察点必须为非空且Tag不重复的名称映射")
        self._assert_identity()
        self._runtime_view()
        with self.desktop.step(f"读取华为设备 {self.current} 全部运行点", expected=f"读取{len(tags)}个点位"):
            rows = self._read_visible_rows()
            result = {key: rows[tag] for key, tag in tags.items() if tag in rows}
            self.desktop.metadata.setdefault("华为上位机最后观察", {})[self.current] = result
            missing = [tag for tag in tags.values() if tag not in rows]
            if missing:
                raise AssertionError("华为运行表缺少请求点：" + "、".join(missing))
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            path = self.desktop.artifacts / f"huawei-upper-{self._selected_conn_id()}-{stamp}.json"
            path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            self.desktop.metadata.setdefault("华为上位机页面观察", []).append(path.name)
            LOGGER.info("已读取华为上位机运行点：连接=%s，点数=%s", self.current, len(result))
            return result
