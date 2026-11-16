"""验证无路由临时模拟遥测按填写值原样通过 IEC104 上报。"""

import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from selenium.webdriver.common.by import By

from testbench.case_plan import (
    ContinueAfterFailure, DESKTOP_FINISH, DESKTOP_START, MASTER104_FINISH, MASTER104_START,
    SITE_CONNECT, Step, case_plan, run_steps, track_fixture_step,
)
from testbench.login_page import PlatformLoginPage, load_login_account
from testbench.model import Environment
from testbench.workspace import local_config

from case_support.avc_period_iec104 import snapshot
from case_support.case_metadata import execution_logic
from case_support.login_plan import login_steps
from case_support.modbus_rtu_page import ModbusRtuPage, xpath_text
from case_support.rtu_quality_frames import decode_frame
from case_support.rtu_quality_iec104 import RtuQualityIec104


pytestmark = pytest.mark.case_group("IEC104")

CA = 1
# 分别绕开倍率、偏移及两者的组合；模拟值均可用短浮点精确表示。
POINTS = (
    ("scale", 0x4001, 100, 0, 100),
    ("offset", 0x4002, 1, 20, -30.5),
    ("both", 0x4003, 4, -8, 42.25),
)


class SimulationIec104(RtuQualityIec104):
    SIMULATION = "//div[@role='dialog'][.//span[normalize-space(.)='无路由模拟数据']]"
    MANUAL = "//div[@role='dialog'][.//div[starts-with(normalize-space(.),'手动选点模拟 · ')]]"
    PERIODIC = (By.XPATH, SIMULATION + "//button[@aria-label='模拟值周期上报']")

    def add_telemetry(self, tag, ioa, scale, offset):
        self._button("添加点位", self.POINT_CARD, "打开 IEC104 添加点位菜单")
        self.upper._click((By.XPATH, "//*[@role='menuitem'][normalize-space(.)='添加点位']"), "手动创建无路由点位")
        ioa_field = (By.XPATH, self.LINK_DIALOG + "//label[@for='ioa']/ancestor::div["
                     "contains(concat(' ',normalize-space(@class),' '),' ant-form-item ')][1]//input[@role='spinbutton']")
        self._fill("tag", tag)
        self.upper._replace(self.upper._until(lambda _: self.upper._visible(ioa_field), "未找到 IOA 输入框"), ioa)
        business = (self.LINK_DIALOG + "//*[@id='business_type']/ancestor::div["
                    "contains(concat(' ',normalize-space(@class),' '),' ant-select ')][1]")
        selector = (By.XPATH, business + "//div[contains(concat(' ',normalize-space(@class),' '),' ant-select-content ')"
                    " or contains(concat(' ',normalize-space(@class),' '),' ant-select-selector ')]")
        self.upper._click(selector, "打开点位业务类型选项")
        combo = (By.XPATH, self.LINK_DIALOG + "//*[@id='business_type' and @aria-expanded='true' and @aria-controls]")
        field = self.upper._until(lambda _: self.upper._visible(combo), "业务类型下拉框未打开")
        self.upper._click((By.XPATH, "//div[contains(@class,'ant-select-dropdown')"
                           " and not(contains(@class,'ant-select-dropdown-hidden'))][.//*[@id="
                           + xpath_text(field.get_attribute("aria-controls")) + "]]//div["
                           "contains(concat(' ',normalize-space(@class),' '),' ant-select-item-option ')]"
                           "[.//div[contains(@class,'ant-select-item-option-content') and normalize-space(.)='遥测']]"),
                          "选择遥测业务类型")
        self._fill("scale", scale)
        self._fill("offset", offset)
        self._button("添加点位", self.LINK_DIALOG, f"保存短浮点模拟点 {tag}")
        self.upper._until(lambda _: not self.upper._visible((By.XPATH, self.LINK_DIALOG)), "点位保存窗口未关闭")
        cells = self._saved_point_cells(tag)
        assert len(cells) == 11, f"点位 {tag} 的配置列不完整：{cells!r}"
        _, saved_tag, address, point_type, business_type, _, fixed, saved_scale, saved_offset, _, _ = cells
        self.upper.desktop.check(f"核对 {tag} 的 IOA、类型、固定上报开关、倍率和偏移",
                                 (saved_tag, address.split()[0], point_type, business_type, fixed,
                                  float(saved_scale), float(saved_offset)),
                                 (tag, str(ioa), "FLOAT (短浮点值)", "遥测", "关闭", scale, offset))

    def save_simulation(self, points):
        self._button("模拟数据", self.POINT_CARD, "打开无路由模拟数据")
        self._button("手动选点", self.SIMULATION, "打开手动选点模拟")
        for point in points:
            tag = point["tag"]
            row = self.MANUAL + "//tr[@data-row-key=" + xpath_text(tag) + "]"
            self.upper._click((By.XPATH, row + "//label[contains(@class,'ant-checkbox-wrapper')]"), f"勾选 {tag}")
            locator = (By.XPATH, row + "//input[@aria-label=" + xpath_text(f"{tag} 模拟值") + "]")
            field = self.upper._until(lambda _: self.upper._visible(locator), f"未找到 {tag} 的模拟值输入框")
            self.upper._replace(field, point["value"])
        self._button("保存模拟值", self.MANUAL, "保存本次手动模拟值")
        self.upper._until(lambda _: not self.upper._visible((By.XPATH, self.MANUAL)), "手动模拟保存窗口未关闭")

    def prepare_periodic(self):
        self.upper._until(lambda _: (switch := self.upper._visible(self.PERIODIC)) and switch.is_enabled(),
                          "模拟值周期上报开关未就绪，请核对上下位机版本及周期上报状态")
        self.upper.desktop.check("确认专用链路的周期上报尚未开启",
                                 self.upper._visible(self.PERIODIC).get_attribute("aria-checked"), "false")
        locator = (By.XPATH, self.SIMULATION + "//input[@aria-label='周期上报间隔（秒）']")
        field = self.upper._until(lambda _: self.upper._visible(locator), "未找到周期上报间隔")
        if field.get_property("value") != "1":
            self.upper._replace(field, 1)
            self._button("应用间隔", self.SIMULATION, "将模拟周期上报间隔设为 1 秒")
        apply_button = self.upper._button("应用间隔", self.SIMULATION)
        self.upper._until(lambda _: (switch := self.upper._visible(self.PERIODIC)) and switch.is_enabled()
                          and (interval := self.upper._visible(locator)) and interval.get_property("value") == "1"
                          and (button := self.upper._visible(apply_button)) and not button.is_enabled(),
                          "未确认模拟周期上报间隔保存为 1 秒")

    def start_periodic(self):
        self.upper._click(self.PERIODIC, "开启本例模拟值周期上报")
        running = (By.XPATH, self.SIMULATION + "//span[normalize-space(.)='周期上报中']")
        self.upper._until(lambda _: self.upper._visible(running), "上位机未确认本例模拟值周期上报已运行")

    def _discard_dialogs(self):
        # 链路删除会一并清除本例模拟快照和周期任务；先解除界面遮挡。
        for dialog in (self.MANUAL, self.SIMULATION):
            if self.upper._visible((By.XPATH, dialog)):
                close = (By.XPATH, dialog + "//button[contains(@class,'ant-modal-close')]")
                self.upper._click(close, "关闭本例模拟数据窗口")
                self.upper._until(lambda _: not self.upper._visible((By.XPATH, dialog)), "模拟数据窗口未关闭")
        super()._discard_dialogs()


def _prepare_link(context):
    port = context.iec104.create()
    context.desktop.metadata["本例 IEC104 配置"] = {
        "连接": context.iec104.name, "主站连接地址": context.host, "端口": port, "CA": CA, "遥测点": context.points,
    }
    for point in context.points:
        context.iec104.add_telemetry(point["tag"], point["ioa"], point["scale"], point["offset"])
    context.iec104.start()


def _check_environment():
    task_path = os.environ.get("TESTBENCH_TASK")
    if not task_path:
        raise RuntimeError("请从测试上位机运行本例并启用专用 104 主站")
    task = json.loads(Path(task_path).read_text(encoding="utf-8"))
    environment = Environment.from_dict(task["environment"])
    environment.validate_master104(required=True)
    environment.validate_master104_driver()


def _connect_master(context):
    context.master.prepare_target(context.host, context.iec104.port, CA)
    context.master.connect()


def _observe_upload(context, mode, start, frame_count):
    master = context.master
    expected = {str(point["ioa"]): point["value"] for point in context.points}
    marker = max((frame["id"] for frame in snapshot(master)["frames"]), default=0)
    samples = {ioa: [] for ioa in expected}
    master.metadata[mode] = samples

    def collect():
        received = {ioa: [] for ioa in expected}
        for frame in snapshot(master)["frames"]:
            if frame["id"] <= marker or frame["direction"] != "receive":
                continue
            decoded = decode_frame({"direction": "接收", "raw": frame["raw"]})
            if not decoded or (decoded["ca"], decoded["type_id"], decoded["cot"]) != (CA, 13, 3):
                continue
            if decoded["test"] or decoded["negative"]:
                continue
            for point in decoded["objects"]:
                ioa = str(point["ioa"])
                if ioa in received:
                    received[ioa].append({"frame_id": frame["id"], "value": point["value"], "raw": frame["raw"]})
        samples.update(received)
        return all(len({sample["frame_id"] for sample in entries}) >= frame_count for entries in samples.values())

    with master.step(f"核对{mode}的短浮点报文原始值", expected=expected, actual=samples):
        start()
        master.wait_for(collect, f"{mode}未收到所有专用 IOA 的 {frame_count} 轮新短浮点自发遥测")
        actual = {ioa: [sample["value"] for sample in entries] for ioa, entries in samples.items()}
        wrong = {ioa: {"预期": expected[ioa], "实际": values}
                 for ioa, values in actual.items() if any(value != expected[ioa] for value in values)}
        if wrong:
            # 手动发送的数值不符时仍检查独立的周期发送，保留两项结果。
            raise ContinueAfterFailure(f"{mode}未按填写值原样上报（按 IOA）：{wrong}")


def _send_manual(context):
    page = context.iec104
    _observe_upload(context, "应用并发送",
                    lambda: page._button("应用并发送", page.SIMULATION, "主动发送临时模拟值"), 1)


def _send_periodic(context):
    page = context.iec104
    page.prepare_periodic()
    _observe_upload(context, "周期上报", page.start_periodic, 2)


def _save_frames(context):
    path = context.master.artifacts / "iec104-simulation-raw-frames.json"
    path.write_text(json.dumps(snapshot(context.master)["frames"], ensure_ascii=False, indent=2), encoding="utf-8")
    context.master.metadata["原始报文文件"] = path.name


ENVIRONMENT = Step("iec104-simulation.env", "检查独立 IEC104 主站入口", "模拟主站和驱动配置有效")
LOGIN = (
    Step("iec104-simulation.account", "读取测试账号", "专用账号配置有效",
         lambda c: setattr(c, "account", load_login_account(local_config("login-account.json")))),
    *login_steps("iec104-simulation.login"),
)
PREPARE = Step("iec104-simulation.prepare", "建立专用 IEC104 被控站和三个无路由短浮点遥测点",
               "点表显示各点 IOA、倍率、偏移及关闭的固定上报值，专用链路已启动", _prepare_link)
RUN = (
    Step("iec104-simulation.connect", "连接本例 IEC104 被控站", "模拟主站收到 STARTDT 确认", _connect_master),
    Step("iec104-simulation.save", "手动勾选三点并保存模拟值", "保存窗口关闭",
         lambda c: c.iec104.save_simulation(c.points)),
    Step("iec104-simulation.manual", "应用并发送，检查主站收到的新报文",
         "三个 IOA 的报文原始值分别为 100、-30.5、42.25", _send_manual),
    Step("iec104-simulation.periodic", "开启周期上报，检查主站至少收到两轮新报文",
         "每轮三个 IOA 的报文原始值分别为 100、-30.5、42.25", _send_periodic),
    Step("iec104-simulation.frames", "保存模拟主站的原始报文", "本次原始报文已落盘", _save_frames),
)
CLEANUP = Step("iec104-simulation.cleanup", "清理本例 IEC104 链路",
               "仅删除本例链路、点表及其临时模拟状态，确认专用连接已移除")


@case_plan(ENVIRONMENT, DESKTOP_START, *LOGIN, SITE_CONNECT, PREPARE, MASTER104_START, *RUN,
           CLEANUP, MASTER104_FINISH, DESKTOP_FINISH)
@execution_logic("""
1. 通过上位机连接现场下位机并建立专用 IEC104 被控站。直接新建三个无路由短浮点遥测点，
   分别配置倍率/偏移 100/0、1/20、4/-8，核对已保存点表，三个点均关闭固定上报值。
2. 独立模拟 104 主站确认 STARTDT 后，在上位机手动保存三点的临时模拟值 100、-30.5、42.25。
3. 点击“应用并发送”，按本例 CA、IOA 解码主站收到的新短浮点自发上报，验证原始值等于实际填写值。
4. 开启 1 秒周期上报，不再次手动发送；验证至少两轮新的自发上报均原样发送。保留报文和失败现场，再清理专用链路。
""")
def test_iec104_simulation_uploads_entered_values_without_scaling(request):
    """IEC104 模拟数据上报不受倍率和偏移影响"""
    with track_fixture_step(request, ENVIRONMENT):
        _check_environment()
    desktop = request.getfixturevalue("fresh_desktop")
    upper = ModbusRtuPage(desktop)
    context = SimpleNamespace(
        desktop=desktop, page=PlatformLoginPage(desktop), master=None,
        iec104=SimulationIec104(upper, f"e2e-sim-{uuid4().hex[:8]}"),
        points=[dict(tag=f"sim-{tag}", ioa=ioa, scale=scale, offset=offset, value=value)
                for tag, ioa, scale, offset, value in POINTS],
    )
    run_steps(request, LOGIN, context)
    with track_fixture_step(request, SITE_CONNECT):
        context.host = upper.connect_site().rsplit(":", 1)[0]
    failure = None
    try:
        run_steps(request, (PREPARE,), context)
        context.master = request.getfixturevalue("master104")
        run_steps(request, RUN, context)
    except BaseException as error:
        failure = error
        for source in (desktop, context.master):
            if source is not None:
                try:
                    source.capture("iec104-simulation-failure", description="IEC104 模拟数据上报清理前现场")
                except Exception as capture_error:
                    failure.add_note(f"截图失败：{capture_error}")
        if context.master is not None and "原始报文文件" not in context.master.metadata:
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
