"""通过上位机为本例 AGC 组创建并读回专用 IEC104 点表和路由。"""
from decimal import Decimal, InvalidOperation
import re

from selenium.webdriver.common.by import By

from .modbus_rtu_page import xpath_text
from .rtu_quality_iec104 import RtuQualityIec104


class AgcDirectionIec104(RtuQualityIec104):
    FLOAT = "FLOAT (短浮点值)"
    SINGLE = "SINGLE (单点值)"

    def _import_agc_point(self, connection, tag, destination, point_type, business_type):
        endpoint = f"{connection} : {tag}"
        self._button("添加点位", self.POINT_CARD, "打开 IEC104 添加点位菜单")
        self.upper._click((By.XPATH, "//*[@role='menuitem'][normalize-space(.)='从数据总线导入']"), "从数据总线导入")
        self._choose("先选择来源连接", connection, connection)
        self._choose("选择一个或多个来源点位", tag, endpoint)
        self._choose("统一点位类型", None, point_type)
        self._choose("统一 IOA 业务类别", None, business_type)
        row = (By.XPATH, self.IMPORT_DIALOG + "//tr[.//span[normalize-space(.)=" + xpath_text(endpoint) + "]]")
        draft = self.upper._until(lambda _: self.upper._visible(row), f"IEC104 导入草稿未显示 {endpoint}")
        self.upper._replace(draft.find_element(By.XPATH, "./td[2]//input"), destination)
        self._button("导入 1 个点位", self.IMPORT_DIALOG, f"保存 IEC104 {business_type}点 {destination} 和路由")
        self.upper._until(lambda _: not self.upper._visible((By.XPATH, self.IMPORT_DIALOG)),
                          f"IEC104 {business_type}点 {destination} 导入窗口未关闭")

        iec104_endpoint = f"IEC104/{self.name} : {destination}"
        # IEC104 被控站导入遥调点时，从本连接向所选 AGC 命令点建路由。
        self.routes.append((iec104_endpoint, endpoint) if business_type == "遥调" else (endpoint, iec104_endpoint))

    def _read_point(self, tag, point_type, business_type):
        self.upper._replace(self._point_search(), tag)
        cells = self._saved_point_cells(tag)
        assert len(cells) == 11, f"IEC104 点 {tag} 的配置列不完整：{cells!r}"
        _, saved_tag, address, saved_type, saved_business, remote_control, fixed, scale, offset, deadband, _ = cells
        match = re.match(r"(\d+)\s*\(0x([0-9a-f]+)\)", address, re.IGNORECASE)
        assert match and int(match[1]) == int(match[2], 16) and 0 < int(match[1]) <= 0xFFFFFF, (
            f"IEC104 点 {tag} 的 IOA 无效：{address!r}"
        )
        assert (saved_tag, saved_type, saved_business) == (tag, point_type, business_type), (
            f"IEC104 点 {tag} 的标签、类型或业务类型不符：{cells!r}"
        )
        # 短浮点遥调 C_SE_NC_1 由主站通过 QOS 的 S/E 位直接执行。
        # 点表中的命令执行方式列属于遥控点，遥调只需核对 FLOAT 与遥调业务类型。
        expected_fixed = "不适用" if business_type == "遥调" else "关闭"
        try:
            engineering = tuple(Decimal(value) for value in (scale, offset, deadband))
        except InvalidOperation as error:
            raise AssertionError(f"IEC104 点 {tag} 的工程量配置不可读：{cells!r}") from error
        assert (remote_control, fixed) == ("-", expected_fixed) and engineering == (1, 0, 0), (
            f"IEC104 点 {tag} 的命令、固定值或工程量配置不符：{cells!r}"
        )
        return int(match[1])

    def configure(self, group_name, *, command_tag, measured_tag=None):
        """导入 AGC 业务点，并返回从专用被控站配置读回的公共地址与 IOA。"""
        self._open()
        self._select_link(self.name)
        self._assert_link()
        point_definitions = [
            ("target_ioa", command_tag, self.FLOAT, "遥调"),
            ("increase_ioa", "AGC增闭锁", self.SINGLE, "遥信"),
            ("decrease_ioa", "AGC减闭锁", self.SINGLE, "遥信"),
        ]
        if measured_tag is not None:
            point_definitions.append(("measured_ioa", measured_tag, self.FLOAT, "遥测"))

        points = []
        for key, tag, point_type, business_type in point_definitions:
            destination = f"{self.name}-{key.removesuffix('_ioa')}"
            self._import_agc_point(f"AGC/{group_name}", tag, destination, point_type, business_type)
            points.append((key, destination, point_type, business_type))

        self.check_routes()
        self._open()
        self._select_link(self.name)
        self._assert_link()
        mapping = {
            "port": int(self._description("本地端点").text.strip().rpartition(":")[2]),
            "ca": int(self._description("公共地址 (CA)").text.strip()),
            "measured_ioa": None,
        }
        for key, tag, point_type, business_type in points:
            mapping[key] = self._read_point(tag, point_type, business_type)
        assert len({mapping[key] for key, *_ in points}) == len(points), f"IEC104 业务点 IOA 重复：{mapping!r}"
        self.upper._replace(self._point_search(), "")
        return mapping
