"""验证 IEC104 批量编辑的固定上报、工程参数及类型切换。"""

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By

from testbench.case_plan import (
    DESKTOP_FINISH, DESKTOP_START, MASTER104_FINISH, MASTER104_START, SITE_CONNECT,
    Step, case_plan, run_steps, track_fixture_step,
)
from testbench.login_page import PlatformLoginPage, load_login_account
from testbench.workspace import local_config

from case_support.avc_period_iec104 import snapshot
from case_support.case_metadata import execution_logic
from case_support.iec104_batch_edit import BatchEditIec104
from case_support.login_plan import login_steps
from case_support.modbus_rtu_page import ModbusRtuPage, xpath_text
from case_support.rtu_quality_frames import decode_frame


pytestmark = pytest.mark.case_group("IEC104")

CA = 1
FLOAT = "FLOAT (短浮点值)"
SINGLE = "SINGLE (单点值)"
TYPE = "类型 (Type)"
FIXED_MODE = "固定上报值"
FIXED_VALUE = "固定报文数值"
DEFAULT_ENGINEERING = (1, 0, 0)
UPDATED_ENGINEERING = (4.5, -7.25, 0.5)
ENGINEERING_FIELDS = ("Scale (倍率)", "Offset (偏移)", "Deadband (死区)")
SELECTED_FLOATS = ("float-a", "float-b")
SELECTED = (*SELECTED_FLOATS, "single")
POINTS = {
    "float-a": (0x4001, "遥测", FLOAT, (2.5, -1.25, 0.25)),
    "float-b": (0x4002, "遥测", FLOAT, (3.5, 2.25, 0.75)),
    "single": (0x0001, "遥信", SINGLE, DEFAULT_ENGINEERING),
    "untouched": (0x4003, "遥测", FLOAT, (8.5, 3.25, 1.25)),
}


def _read_points(context):
    values = {}
    for tag in POINTS:
        cells = context.iec104.point_cells(tag)
        _, saved_tag, address, point_type, business, _, fixed, scale, offset, deadband, _ = cells
        values[tag] = (saved_tag, int(address.split()[0]), business, point_type, fixed,
                       (float(scale), float(offset), float(deadband)))
    return values


def _check_points(context, label, *, enabled=False, updated=False, switched=False):
    expected = {}
    for tag, (ioa, business, point_type, engineering) in POINTS.items():
        if switched and tag in SELECTED_FLOATS:
            point_type, engineering = SINGLE, DEFAULT_ENGINEERING
        elif updated and tag in SELECTED_FLOATS:
            engineering = UPDATED_ENGINEERING
        expected[tag] = (tag, ioa, business, point_type,
                         "固定：1" if enabled and tag in SELECTED else "关闭", engineering)
    context.iec104.reopen_points()
    context.desktop.check(label, _read_points(context), expected)


def _prepare(context):
    page = context.iec104
    port = page.create()
    context.desktop.metadata["本例 IEC104 配置"] = {
        "连接": page.name, "主站连接地址": context.host, "端口": port, "CA": CA, "点位": POINTS,
    }
    for tag, (ioa, business, point_type, (scale, offset, deadband)) in POINTS.items():
        page.add_point(tag, ioa, business=business, point_type=point_type,
                       scale=scale, offset=offset, deadband=deadband)
    _check_points(context, "回读三个勾选对象和未勾选参照点的初始配置")


def _check_invalid_value(context):
    page = context.iec104
    page.open_batch(SELECTED)
    # FLOAT 允许小数；先填小数，再还原原类型，验证同一草稿对混合点的拦截。
    page.choose_batch(TYPE, FLOAT)
    page.fill_batch(FIXED_VALUE, "0.5")
    save = page.upper._visible(page.batch_button("保存批量修改"))
    context.desktop.check("全为 FLOAT 时允许小数固定值", save.is_enabled(), True)
    type_selector = (page.BATCH_DIALOG + "//*[@aria-label=" + xpath_text(TYPE) + "]/ancestor::div["
                     "contains(concat(' ',normalize-space(@class),' '),' ant-select ')][1]")
    control = (By.XPATH, type_selector + "//div[contains(concat(' ',normalize-space(@class),' '),"
                                         "' ant-select-content ') or contains(concat(' ',normalize-space(@class),' '),"
                                         "' ant-select-selector ')]")
    with context.desktop.step("显示批量类型下拉框的清除选项"):
        field = page.upper._until(lambda _: page.upper._visible(control), "批量类型选择器未显示")
        ActionChains(page.upper.driver).move_to_element(field).perform()
    page.upper._click((By.XPATH, type_selector + "//*[contains(concat(' ',normalize-space(@class),' '),"
                                                 "' ant-select-clear ')]"), "恢复保留各点原类型")
    problem = (By.XPATH, page.BATCH_DIALOG + "//li[contains(normalize-space(.),'SINGLE 固定值只允许 0 或 1')]")
    error = page.upper._until(lambda _: page.upper._visible(problem), "混合点位未提示 SINGLE 不允许小数固定值")
    save = page.upper._visible(page.batch_button("保存批量修改"))
    context.desktop.check("混合点位禁止保存小数固定值", (error.text, save.is_enabled()),
                          ("single：遥信 SINGLE 固定值只允许 0 或 1", False))
    page.cancel_batch()
    _check_points(context, "放弃无效草稿后原点表保持不变")


def _enable_fixed(context):
    page = context.iec104
    page.open_batch(SELECTED)
    page.choose_batch(FIXED_MODE, "开启")
    page.choose_batch(FIXED_VALUE, "1 (合)")
    for label, value in zip(ENGINEERING_FIELDS, UPDATED_ENGINEERING, strict=True):
        page.fill_batch(label, value)
    page.check_counts(3, 3)
    page.save_batch()
    _check_points(context, "回读固定值及 FLOAT 工程参数，SINGLE 和未选点保留原参数",
                  enabled=True, updated=True)


def _connect_master(context):
    context.iec104.start()
    context.master.prepare_target(context.host, context.iec104.port, CA)
    context.master.connect()


def _decode_single(raw_text, type_id):
    """补充已有遥测报文解码器未处理的单点遥信对象。"""
    raw = bytes.fromhex(raw_text)
    count, sequence = raw[7] & 0x7F, bool(raw[7] & 0x80)
    size = 1 if type_id == 1 else 8
    expected_len = 15 + count * size if sequence else 12 + count * (3 + size)
    assert count > 0 and len(raw) == expected_len, f"单点遥信对象数量或长度错误：{raw_text}"
    offset = 15 if sequence else 12
    start_ioa = int.from_bytes(raw[12:15], "little")
    if sequence:
        assert start_ioa + count - 1 <= 0xFFFFFF, f"单点遥信连续 IOA 超出范围：{raw_text}"
    for index in range(count):
        ioa = start_ioa + index if sequence else int.from_bytes(raw[offset:offset + 3], "little")
        if not sequence:
            offset += 3
        yield {"ioa": ioa, "value": raw[offset] & 1}
        offset += size


def _check_gi(context):
    master = context.master
    expected = {str(POINTS[tag][0]): 1 for tag in SELECTED}
    marker = max((frame["id"] for frame in snapshot(master)["frames"]), default=0)
    with master.step("核对本次总召回传的固定报文值", expected=expected) as step:
        master.interrogate()
        frames = snapshot(master)["frames"]
        context.frames = frames
        actual = {ioa: [] for ioa in expected}
        step["实际"] = actual
        for frame in frames:
            if frame["id"] <= marker or frame["direction"] != "receive":
                continue
            asdu = decode_frame({"direction": "接收", "raw": frame["raw"]})
            if not asdu or (asdu["ca"], asdu["cot"]) != (CA, 20) or asdu["test"] or asdu["negative"]:
                continue
            type_id = asdu["type_id"]
            points = _decode_single(frame["raw"], type_id) if type_id in (1, 30) else asdu["objects"]
            for point in points:
                ioa = str(point["ioa"])
                if ioa in actual:
                    actual[ioa].append({"帧号": frame["id"], "类型": type_id, "值": point["value"]})
        master.metadata["本次总召固定上报值"] = actual
        for tag in SELECTED:
            ioa = str(POINTS[tag][0])
            types = (1, 30) if tag == "single" else (13, 36)
            rows = actual[ioa]
            assert rows and all(row["类型"] in types and row["值"] == expected[ioa] for row in rows), (
                f"{tag} (IOA {ioa}) 总召应该使用类型 {types} 上报固定值 1，实际：{rows!r}"
            )


def _save_frames(context):
    path = context.master.artifacts / "iec104-batch-fixed-reporting-frames.json"
    frames = context.frames if context.frames is not None else snapshot(context.master)["frames"]
    path.write_text(json.dumps(frames, ensure_ascii=False, indent=2), encoding="utf-8")
    context.master.metadata["原始报文文件"] = path.name


def _disable_fixed(context):
    page = context.iec104
    page.stop()
    page.open_batch(SELECTED)
    page.choose_batch(FIXED_MODE, "关闭")
    page.check_counts(3, 3)
    page.save_batch()
    _check_points(context, "回读固定上报已关闭且工程参数未还原", updated=True)
    for tag in SELECTED:
        page._search_point(tag)
        edit = (By.XPATH, page.POINT_CARD + "//button[@aria-label=" + xpath_text(f"编辑点位 {tag}") + "]")
        page.upper._click(edit, f"打开 {tag} 单点配置核对保留的固定值")
        if tag == "single":
            locator = (By.XPATH, page.LINK_DIALOG + "//*[@id='fixed_value' and @role='combobox']"
                       "/ancestor::div[contains(concat(' ',normalize-space(@class),' '),' ant-select ')][1]"
                       "//*[contains(concat(' ',normalize-space(@class),' '),' ant-select-content-has-value ')"
                       " or contains(concat(' ',normalize-space(@class),' '),' ant-select-selection-item ')]")
            display = page.upper._until(lambda _: page.upper._visible(locator), "未显示保留的 SINGLE 固定值")
            saved, expected = display.text.strip(), "1 (合)"
        else:
            saved, expected = page._field("fixed_value").get_property("value"), "1"
        context.desktop.check(f"关闭后 {tag} 保留固定数值", saved, expected)
        page._button("取消", page.LINK_DIALOG, "关闭单点配置")
        page.upper._until(lambda _: not page.upper._visible((By.XPATH, page.LINK_DIALOG)), "单点配置未关闭")


def _switch_type(context):
    page = context.iec104
    page.open_batch(SELECTED_FLOATS)
    page.choose_batch(TYPE, SINGLE)
    page.check_counts(2, 2)
    page.save_batch()
    _check_points(context, "回读两个改为 SINGLE 的点位及重置后的 1/0/0 参数，其余点不变", switched=True)


LOGIN = (
    Step("iec104-batch-fixed.account", "读取测试账号", "专用账号配置有效",
         lambda c: setattr(c, "account", load_login_account(local_config("login-account.json")))),
    *login_steps("iec104-batch-fixed.login"),
)
RUN = (
    Step("iec104-batch-fixed.prepare", "创建专用 IEC104 被控站、三点测试对象和一个参照点",
         "四个点位的初始类型、业务类型、IOA、工程参数及关闭状态可回读", _prepare),
    Step("iec104-batch-fixed.reject", "验证混合类型拒绝小数固定值并放弃草稿",
         "SINGLE 固定值只允许 0 或 1，保存不可用，点表未改动", _check_invalid_value),
    Step("iec104-batch-fixed.enable", "批量开启固定上报并调整工程参数",
         "三点保存固定值 1，仅 FLOAT 改为倍率 4.5、偏移 -7.25、死区 0.5，未选点不变", _enable_fixed),
    Step("iec104-batch-fixed.connect", "启动专用被控站链路并连接独立 104 主站", "主站收到 STARTDT 确认", _connect_master),
    Step("iec104-batch-fixed.gi", "主站发起总召并核对新收到的报文",
         "按本次 CA 和 IOA，两个 FLOAT 及一个 SINGLE 均上报固定值 1", _check_gi),
    Step("iec104-batch-fixed.frames", "保存模拟主站原始报文", "本次原始报文已落盘", _save_frames),
    Step("iec104-batch-fixed.disable", "断开专用链路，批量关闭固定上报",
         "三点已关闭；固定报文数值、工程参数和未选点保持不变", _disable_fixed),
    Step("iec104-batch-fixed.type", "批量把两个 FLOAT 改为 SINGLE",
         "两个点的类型保存为 SINGLE、工程参数重置为 1/0/0，其余点不变", _switch_type),
)
CLEANUP = Step("iec104-batch-fixed.cleanup", "清理本例 IEC104 链路", "仅删除本例链路及点表并读回确认")


@case_plan(DESKTOP_START, *LOGIN, SITE_CONNECT, MASTER104_START, *RUN, CLEANUP, MASTER104_FINISH, DESKTOP_FINISH)
@execution_logic("""
1. 建立专用 IEC104 被控站，创建两个参数各异的 FLOAT 遥测、一个 SINGLE 遥信和一个未选的 FLOAT 参照点。
2. 勾选两遥测一遥信，验证草稿中的小数固定值不能应用到混合点，放弃草稿后读回原点表。
3. 批量开启固定上报值并设置为 1，同时填写倍率、偏移、死区。重开点表核对三点固定值、仅 FLOAT 更新工程参数且参照点不变。
4. 启动被控站并用独立模拟 104 主站总召，按本次 CA、IOA 和报文类型核对三个固定值均为 1，保存原始报文。
5. 断开专用链路，批量关闭固定上报。读回配置确认数值和工程参数保留；再将两个 FLOAT 批量改为 SINGLE，读回工程参数重置为 1/0/0。
   失败时先留证，再清理本例链路。
""")
def test_iec104_batch_fixed_reporting_and_type_switch(request):
    """IEC104 批量编辑固定上报、工程参数及类型切换"""
    desktop = request.getfixturevalue("fresh_desktop")
    upper = ModbusRtuPage(desktop)
    context = SimpleNamespace(
        desktop=desktop, page=PlatformLoginPage(desktop), master=None, frames=None,
        iec104=BatchEditIec104(upper, f"e2e-batch-fixed-{uuid4().hex[:8]}"),
    )
    run_steps(request, LOGIN, context)
    with track_fixture_step(request, SITE_CONNECT):
        context.host = upper.connect_site().rsplit(":", 1)[0]
    context.master = request.getfixturevalue("master104")
    failure = None
    try:
        run_steps(request, RUN, context)
    except BaseException as error:
        failure = error
        for source in (desktop, context.master):
            try:
                source.capture("iec104-batch-fixed-failure", description="IEC104 批量固定上报清理前现场")
            except Exception as capture_error:
                failure.add_note(f"截图失败：{capture_error}")
        if "原始报文文件" not in context.master.metadata:
            try:
                _save_frames(context)
            except Exception as capture_error:
                failure.add_note(f"报文取证失败：{capture_error}")
        raise
    finally:
        if context.iec104.port is not None:
            try:
                with track_fixture_step(request, CLEANUP):
                    context.iec104.cleanup()
            except Exception as cleanup_error:
                desktop.metadata["本例 IEC104 清理失败"] = str(cleanup_error)
                if failure is None:
                    raise
                failure.add_note(f"专用 IEC104 链路清理失败：{cleanup_error}")
