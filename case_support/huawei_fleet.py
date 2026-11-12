"""华为机群固定拓扑、点表及独立双端验收规则。"""
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
import logging

from testbench.modbus_rtu_config import decimal_value


LOGGER = logging.getLogger(__name__)
FLEET_STATIONS = ((1, 2), (1,))
FLEET_DEVICE_COUNT = sum(len(stations) for stations in FLEET_STATIONS)
FLEET_SIZE_LABEL = f"{len(FLEET_STATIONS)} 路 {FLEET_DEVICE_COUNT} 台"


@dataclass(frozen=True)
class HuaweiPoint:
    key: str
    name: str
    address: int
    data_type: str
    reg_count: int = 1
    scale: str = "1"
    function: int = 3
    bit_index: int | None = None
    enum_value: int | None = None
    unit: str = ""


READ_POINTS = (
    HuaweiPoint("voltage_a", "A相电压", 32069, "UINT16", scale="0.1", unit="V"),
    HuaweiPoint("voltage_b", "B相电压", 32070, "UINT16", scale="0.1", unit="V"),
    HuaweiPoint("voltage_c", "C相电压", 32071, "UINT16", scale="0.1", unit="V"),
    HuaweiPoint("current_a", "A相电流", 32072, "INT32", 2, "0.001", unit="A"),
    HuaweiPoint("current_b", "B相电流", 32074, "INT32", 2, "0.001", unit="A"),
    HuaweiPoint("current_c", "C相电流", 32076, "INT32", 2, "0.001", unit="A"),
    HuaweiPoint("active", "有功功率", 32080, "INT32", 2, "0.001", unit="kW"),
    HuaweiPoint("reactive", "无功功率", 32082, "INT32", 2, "0.001", unit="kvar"),
    HuaweiPoint("pf", "功率因数", 32084, "INT16", scale="0.001"),
    HuaweiPoint("status", "设备状态", 32089, "UINT16"),
    HuaweiPoint("generating", "发电", 32000, "BOOL", bit_index=1),
    HuaweiPoint("stopped", "停机_限电停运", 32000, "BOOL", bit_index=7),
    HuaweiPoint("standby", "待机", 32000, "BOOL", bit_index=0),
    HuaweiPoint("active_mode", "有功调节模式", 35300, "UINT16"),
    HuaweiPoint("reactive_mode", "无功调节模式", 35304, "UINT16"),
    HuaweiPoint("active_command", "最后有功指令地址", 35303, "UINT16"),
    HuaweiPoint("reactive_command", "最后无功指令地址", 35307, "UINT16"),
)
CONTROL_POINTS = (
    HuaweiPoint("active_setpoint", "有功百分比设定", 40125, "UINT16", scale="0.1", function=6, unit="%"),
    HuaweiPoint("pf_setpoint", "功率因数设定", 40122, "INT16", scale="0.001", function=6),
    HuaweiPoint("reactive_setpoint", "无功Q_S设定", 40123, "INT16", scale="0.001", function=6),
)
UNIMPLEMENTED_CONTROLS = (
    {"功能": "开机遥控", "地址": 40200, "状态": "模型未实现，未发送命令，未验证"},
    {"功能": "关机遥控", "地址": 40201, "状态": "模型未实现，未发送命令，未验证"},
)
STATUS_VALUES = {"发电": 512, "有功调节": 513, "无功调节": 513, "负无功调节": 513, "待机": 40960, "限电停运": 772,
                 "恢复发电": 512}
STATUS_WORD_VALUES = {"发电": 6, "有功调节": 10, "无功调节": 10, "负无功调节": 10,
                      "待机": 1, "限电停运": 128, "恢复发电": 6}


@dataclass(frozen=True)
class HuaweiFleetConfig:
    serial_devices: tuple
    acquisition_timeout: float = 300
    poll_interval_ms: int = 1000
    baud_rate: int = 9600


def build_fleet(config, run_id):
    """场景固定且每台不同；通过不同电压及有功识别同站号跨总线串站。"""
    buses, ordinal = [], 0
    for index, (path, stations) in enumerate(zip(config.serial_devices, FLEET_STATIONS, strict=True), 1):
        devices = []
        for station in stations:
            ordinal += 1
            devices.append({"station_id": station, "ordinal": ordinal,
                            "dc_available_kw": 10 + ordinal, "grid_line_voltage_v": 380 + ordinal,
                            "grid_frequency_hz": 50, "ambient_temperature_c": 25, "efficiency": 1,
                            "disturbance_enabled": False})
        buses.append({"name": f"e2e-huawei-{run_id}-485-{index}", "index": index,
                      "serial_device": path, "devices": devices})
    LOGGER.info("已生成 %s 华为行为拓扑，所有场景均关闭扰动", FLEET_SIZE_LABEL)
    return buses


def point_expected(point, raw):
    value = decimal_value(raw)
    if value != value.to_integral_value():
        raise ValueError("模拟器原始寄存器值必须为整数")
    if point.data_type == "BOOL":
        return (int(value) == point.enum_value if point.enum_value is not None
                else bool((int(value) >> point.bit_index) & 1))
    return value * Decimal(point.scale)


def compare_point(point, before, after, upper, *, expected_raw=None, previous_timestamp=None):
    """源值稳定时按运行表显示精度核对，保留原始换算结果。"""
    errors = []
    expected = None
    display_expected = None
    actual = upper.get("value")
    difference = None
    try:
        expected = point_expected(point, before)
        if decimal_value(before) != decimal_value(after):
            errors.append("模拟器源值在采样窗口变化，未达到稳态")
        if expected_raw is not None and decimal_value(before) != decimal_value(expected_raw):
            errors.append(f"模拟器状态未达到指定工况：预期原始值{expected_raw}")
        if point.data_type == "BOOL":
            if type(actual) is not bool or actual != expected:
                errors.append("BOOL状态与模拟器状态字不匹配")
        else:
            display_expected = expected.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            if display_expected.is_zero():
                display_expected = display_expected.copy_abs()
            actual_value = decimal_value(actual)
            difference = actual_value - expected
            if actual_value != display_expected:
                errors.append("上位机工程量与两位小数显示期望不匹配")
        if upper.get("quality") != "正常":
            errors.append("上位机质量不是正常")
        timestamp = str(upper.get("timestamp") or "").strip()
        if timestamp in ("", "-", "—", "未知"):
            errors.append("上位机采集时间无效")
        elif previous_timestamp is not None and timestamp == previous_timestamp:
            errors.append("数值变化后上位机仍显示前次时间")
    except (ValueError, TypeError, KeyError) as error:
        errors.append(f"点值格式无效：{error}")
    return {"点名": point.name, "点标识": point.key, "地址": point.address, "类型": point.data_type,
            "倍率": point.scale, "位索引": point.bit_index, "单位": point.unit,
            "模拟器前值": before, "模拟器后值": after, "换算期望": str(expected),
            "显示期望": str(display_expected) if display_expected is not None else "",
            "上位机实际": actual, "差值": str(difference) if difference is not None else "",
            "质量": upper.get("quality"), "时间": upper.get("timestamp"),
            "通过": not errors, "错误": errors}
