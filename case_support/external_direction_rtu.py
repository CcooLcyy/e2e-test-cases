"""在模拟逆变器 A 的保持寄存器中产生外部方向闭锁遥信。"""

from selenium.webdriver.common.by import By

from .measurement_expiry_run import _expect_writes, _protocol, prepare_control_rtu
from .modbus_rtu_page import ModbusRtuPage


DIRECTION_TAGS = {"increase": "increase-block", "decrease": "decrease-block"}
DIRECTION_BITS = {"increase": 0, "decrease": 1}
INCREASE_LOCK_POINT = ("禁止增出力遥信（可选）", DIRECTION_TAGS["increase"])
LOCK_POINTS = (
    INCREASE_LOCK_POINT,
    ("禁止减出力遥信（可选）", DIRECTION_TAGS["decrease"]),
)


class ExternalDirectionRtuPage(ModbusRtuPage):
    def add_direction_bit(self, tag, address, bit):
        self._assert_identity()
        self._click(self._button("添加点位", "//div[contains(@class,'protocol-point-card')]"), "新增 RTU 闭锁位")
        self._fill("tag", tag, "Tag")
        self._select_option("function", "0x03 读保持寄存器")
        self._fill("address", address, "点位地址")
        self._select_option("data_type", "BOOL")
        self._select_option("reg_count", "1 个寄存器")
        self._select_option("bool_mode", "位索引")
        self._fill("bit_index", bit, "位索引")
        self._select_option("byte_order", "AB")
        self._submit("添加点位")
        self._click((By.CSS_SELECTOR, f'button[aria-label="编辑点位 {tag}"]'), "重新读取闭锁点配置")
        self._assert_input("address", address)
        self._assert_input("bit_index", bit)
        self._assert_select("data_type", "BOOL")
        self._assert_select("function", "0x03 读保持寄存器")
        self._click(self._button("取消", self.DIALOG_XPATH), "关闭点位核对窗口")

    def wait_direction_bit(self, tag, active, timeout):
        def match(_):
            point = self.observe(tag)
            return (point if point["value"] == str(active).lower() and point["quality"] == "正常"
                    and point["timestamp"] not in ("", "-") else None)

        with self.desktop.step(f"核对闭锁采集 {tag}", expected=str(active).lower()):
            return self._until(match, f"RTU 未收到有效的闭锁遥信 {tag}={active}", timeout=timeout)


def prepare_external_control_rtu(context, specs):
    """复用控制量测与设点接线，并为 A 增加独立的外部遥信输入。"""
    def add_bits(role, extra):
        if role == "A":
            context.external_point, = extra
            for direction, tag in DIRECTION_TAGS.items():
                context.rtu.add_direction_bit(tag, context.external_point["address"], DIRECTION_BITS[direction])

    prepare_control_rtu(context, {**specs, "A": (*specs["A"], 0)}, configure_extra_points=add_bits)
    context.points["A"] = context.points["A"][:2]
    context.external_word = 0
    context.rtu.select(context.connections["A"])
    for tag in DIRECTION_TAGS.values():
        context.rtu.wait_direction_bit(tag, False, context.timeout)


def set_external_direction(context, direction, active):
    """从模拟设备切换外部遥信，等待 DSP 采集端确认。"""
    point = context.external_point
    mask = 1 << DIRECTION_BITS[direction]
    old = context.external_word
    word = old | mask if active else old & ~mask
    result = context.simulator.set_value(point["bus"], point["station_id"], point["function"], point["address"], word)
    context.desktop.check("核对模拟端闭锁状态字", result["value"], word)
    context.rtu.open_current_links()
    context.rtu.select(context.connections["A"])
    observations = {}
    for key, tag in DIRECTION_TAGS.items():
        value = bool(word & (1 << DIRECTION_BITS[key]))
        observations[tag] = context.rtu.wait_direction_bit(tag, value, context.timeout)
    context.external_word = word
    context.desktop.metadata.setdefault("外部闭锁遥信观察", []).append({
        "旧状态字": old, "新状态字": word, "采集": observations,
    })


def check_external_allocation(context, marker, expected):
    """核对设点请求、应答和寄存器，含保持原设点的成员。"""
    previous = context.previous
    changed = {role: value for role, value in expected.items() if previous.get(role) != value}
    observed = _expect_writes(context, changed, after=marker, previous=previous)
    for role, value in expected.items():
        point = context.points[role][1]
        sample = context.simulator.observe(
            point["bus"], point["station_id"], point["function"], point["address"], verify_manual=False,
        )
        assert (sample["actual_device"], sample["link_path"]) == (point["actual_device"], point["link_path"]), (
            f"成员 {role} 的模拟串口身份变化：{sample!r}")
        context.desktop.check(f"核对成员 {role} 的设定寄存器", sample["value"], value)
    _, writes = _protocol(context)
    for role, value in expected.items():
        allowed = {value, previous.get(role)}
        unexpected = [entry for entry in writes[role] if entry["index"] > marker and entry["value"] not in allowed]
        context.desktop.check(f"核对成员 {role} 本轮没有写入其他设定", unexpected, [])
    context.previous = expected
    return observed
