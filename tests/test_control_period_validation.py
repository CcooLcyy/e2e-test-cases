"""通过控制组编辑界面验收 AGC 和 AVC 的周期范围校验。"""

import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys

from case_support.agc_direction_setup import AgcDirectionSetup
from case_support.avc_period_page import GROUP, SUMMARY, AvcPeriodPage
from case_support.case_metadata import execution_logic
from case_support.login_plan import login_steps
from case_support.measurement_expiry_run import prepare_control_rtu
from case_support.measurement_expiry_simulator import MeasurementExpirySimulator
from case_support.modbus_rtu_page import ModbusRtuPage, xpath_text
from case_support.power_range_run import _load_account
from testbench.case_plan import (
    ContinueAfterFailure, DESKTOP_FINISH, DESKTOP_START, SITE_CONNECT, SLAVESIM_BROWSER_READY,
    Step, case_plan, run_steps, track_fixture_context, track_fixture_step,
)
from testbench.login_page import PlatformLoginPage
from testbench.model import Environment, validate_slavesim_url


pytestmark = pytest.mark.requires_modbus

CALCULATION = "calculation_execution_period_seconds"
COMMAND = "command_control_period_seconds"
BASELINE = {CALCULATION: "5", COMMAND: "10"}
BASELINE_SUMMARY = "计算 5 秒 / 命令 10 秒"
SAVE = "保存控制组并建立路由"


def _prepare_rtu(context):
    specs = {"A": (40, 0)} if context.module == "AGC" else {"A": (0, 0), "V": (220,)}
    prepare_control_rtu(context, specs)


def _open_editor(context):
    group = context.group
    if context.module == "AGC":
        group._select_owned_group()
        group._draft_opened = True
        overview = group.OVERVIEW
    else:
        group.stop()
        group.group_draft_opened = True
        overview = SUMMARY
    group._click(group._button("编辑", overview), f"编辑本例 {context.module} 控制组")


def _discard_editor(context):
    group = context.group
    if context.module == "AGC":
        group._close_draft(context.dialog, "取消")
        group._draft_opened = False
    else:
        group._discard_draft(context.dialog, "取消")
        group.group_draft_opened = False


def _set_baseline(context):
    group = context.group
    if context.module == "AGC":
        _open_editor(context)
        for name, value in BASELINE.items():
            group._fill_group_field(context.dialog, name, value)
        group._submit_group("保存 AGC 合法周期基线")
    else:
        group.stop()
        group.update_periods(int(BASELINE[CALCULATION]), int(BASELINE[COMMAND]))


def _saved_periods(context):
    group = context.group
    group._refresh_control_groups(context.module)
    if context.module == "AVC":
        group._select_owned()
        return group._read_summary("控制周期")

    group._select_owned_group()
    details = group._until(lambda _: group._visible(group.DETAILS), "AGC 配置与点位详情未显示")
    if details.get_attribute("aria-expanded") != "true":
        group._click(group.DETAILS, "展开本例 AGC 配置与点位详情")
    return group._description(group._selected_overview(), "控制周期")


def _prepare_group(context):
    group = context.group
    connection = context.connections["A"]
    if context.module == "AGC":
        group.create_group(context.name, "A", {"name": connection}, "measured", "setpoint")
        _set_baseline(context)
    else:
        group.create_group(
            context.name, mode="q_total", calculation_period=int(BASELINE[CALCULATION]),
            command_period=int(BASELINE[COMMAND]), voltage_tag="measured", voltage_connection=context.connections["V"],
            target_tag=f"{context.name}-target", rtu_connection=connection, q_measurement_tag="measured",
            q_setpoint_tag="setpoint", member_name="A",
        )
        group.stop()
    context.desktop.check("刷新并读回已保存的合法周期基线", _saved_periods(context), BASELINE_SUMMARY)


def _period_input(context, field):
    return context.dialog + "//input[@id=" + xpath_text(field) + "]"


def _read_inputs(context):
    group = context.group
    values = {}
    for field in BASELINE:
        locator = (By.XPATH, _period_input(context, field))
        control = group._until(lambda _: group._visible(locator), f"未显示本例周期输入框 {field}")
        values[field] = control.get_property("value")
    return values


def _submit_invalid(context, field, label, invalid, valid_range):
    group = context.group
    dialog = (By.XPATH, context.dialog)
    error_locator = (
        By.XPATH,
        _period_input(context, field)
        + "/ancestor::*[contains(concat(' ',normalize-space(@class),' '),' ant-form-item ')][1]"
          "//*[contains(concat(' ',normalize-space(@class),' '),' ant-form-item-explain-error ')"
          " and contains(normalize-space(.)," + xpath_text(valid_range) + ")]",
    )
    _open_editor(context)
    context.desktop.check("核对尝试前两项已保存周期", _read_inputs(context), BASELINE)
    control = group._until(lambda _: group._visible((By.XPATH, _period_input(context, field))), f"未显示{label}")
    control.click()
    control.send_keys(Keys.CONTROL, "a", Keys.NULL, Keys.BACKSPACE)
    control.send_keys(str(invalid))
    entered = _read_inputs(context)
    observation = {"字段": label, "越界尝试值": invalid, "保存前输入值": entered}
    context.desktop.metadata.setdefault("周期越界观察", []).append(observation)
    expected_input = {**BASELINE, field: str(invalid)}
    screenshot = f"period-validation-{field}-{invalid}"
    if entered != expected_input:
        context.desktop.capture(screenshot, description=f"{context.module} {label}失焦前的输入现场",
                                expected=expected_input, actual=entered)
        _discard_editor(context)
        observation["刷新后的控制周期"] = _saved_periods(context)
        raise ContinueAfterFailure(f"未取得{label} {invalid} 秒的失焦前输入，无法验证保存拒绝：{observation!r}")
    context.desktop.check("核对失焦前的输入框原值", entered, expected_input)

    def saved_or_rejected(_):
        if not group._visible(dialog):
            return "已关闭编辑窗口", ""
        if error := group._visible(error_locator):
            return "已显示范围错误", error.text

    with group._finish_group_save():
        group._click(group._button(SAVE, context.dialog), f"尝试保存{label} {invalid} 秒")
        try:
            status, message = group._until(saved_or_rejected, f"未观察到{label}范围提示或编辑窗口关闭")
        except TimeoutException:
            status, message = "未观察到范围错误", ""
        group._wait_group_save_idle(context.dialog)
        draft_open = group._visible(dialog) is not None
        observation.update({"保存结果": status, "范围错误": message, "编辑窗口仍打开": draft_open})
        if draft_open:
            observation["保存操作后的输入值"] = _read_inputs(context)
        context.desktop.capture(screenshot, description=f"{context.module} {label}尝试保存后的现场",
                                expected=f"窗口保留并提示 {valid_range} 秒范围错误", actual=observation)
        _discard_editor(context)
        saved = _saved_periods(context)
        observation["刷新后的控制周期"] = saved

    expected = f"{label}提示 {valid_range} 秒范围错误且保持{BASELINE_SUMMARY}"
    if status != "已显示范围错误" or not draft_open or saved != BASELINE_SUMMARY:
        error = ContinueAfterFailure(f"{context.module} 预期{expected}，实际 {observation!r}")
        if saved != BASELINE_SUMMARY:
            try:
                _set_baseline(context)
                context.desktop.check("为下一独立越界尝试恢复原周期", _saved_periods(context), BASELINE_SUMMARY)
            except Exception as restore_error:
                error.add_note(f"恢复本例合法周期基线失败：{restore_error}")
                raise RuntimeError(str(error)) from restore_error
        raise error
    context.desktop.check("刷新后仍为已保存的合法周期", saved, BASELINE_SUMMARY)


def _invalid_step(field, label, value, valid_range):
    other = f"命令最小间隔保持 {BASELINE[COMMAND]} 秒" if field == CALCULATION else f"计算周期保持 {BASELINE[CALCULATION]} 秒"
    return Step(
        f"period-validation.{field}.{value}", f"输入{label} {value} 秒并尝试保存，{other}",
        f"提示 {valid_range} 秒范围错误，刷新后仍是计算 5 秒、命令最小间隔 10 秒",
        lambda context: _submit_invalid(context, field, label, value, valid_range),
    )


ENVIRONMENT = Step("period-validation.environment", "核对 slaveSim 测试入口", "模拟器 URL 和采集等待配置有效")
SIM_START = Step("period-validation.sim.start", "打开本例 slaveSim 会话", "可读写专用模拟配置")
SIM_FINISH = Step("period-validation.sim.finish", "关闭本例 slaveSim 会话", "独立浏览器会话已关闭")
UPPER_CLEANUP = Step("period-validation.upper.cleanup", "删除本例控制组和 RTU 连接", "专用配置和路由已清理")
SIM_CLEANUP = Step("period-validation.sim.cleanup", "清空 slaveSim 总线", "回读确认总线数为 0")
LOGIN = (Step("period-validation.account", "读取测试账号", "专用账号配置有效", _load_account),
         *login_steps("period-validation.login"))
CHECKS = (
    Step("period-validation.rtu", "配置本例成员及所需量测的 RTU 点", "读到初始量测并读回设点配置", _prepare_rtu),
    Step("period-validation.baseline", "以周期分配建立本例控制组并保存合法周期",
         "刷新并读回计算 5 秒、命令最小间隔 10 秒，若为 AVC 则确认控制组已停止", _prepare_group),
    _invalid_step(CALCULATION, "计算周期", 0, "1～15"),
    _invalid_step(CALCULATION, "计算周期", 16, "1～15"),
    _invalid_step(COMMAND, "命令最小间隔", 3, "4～30"),
    _invalid_step(COMMAND, "命令最小间隔", 31, "4～30"),
)
PLAN = case_plan(ENVIRONMENT, SLAVESIM_BROWSER_READY, DESKTOP_START, *LOGIN, SITE_CONNECT, SIM_START,
                 *CHECKS, UPPER_CLEANUP, SIM_CLEANUP, SIM_FINISH, DESKTOP_FINISH)
LOGIC = execution_logic("\n".join(f"{index}. {step.action}。预期 {step.expected}。"
                                  for index, step in enumerate((*CHECKS, UPPER_CLEANUP, SIM_CLEANUP), 1)))


def _cleanup(request, context, failure):
    errors = []
    try:
        with track_fixture_step(request, UPPER_CLEANUP):
            for label, cleanup in ((context.module, context.group.cleanup), ("RTU", context.rtu.cleanup)):
                try:
                    cleanup()
                except Exception as error:
                    errors.append(f"{label}：{error}")
            if errors:
                raise RuntimeError("清理本例上位机配置失败：" + "；".join(errors))
    except Exception as error:
        if failure is None:
            raise
        failure.add_note(str(error))
    finally:
        if context.sim_started:
            try:
                with track_fixture_step(request, SIM_CLEANUP):
                    context.simulator.clear_all_buses()
            except Exception as error:
                if failure is None:
                    raise
                failure.add_note(f"清空本例模拟总线失败：{error}")


def _run(request, module):
    with track_fixture_step(request, ENVIRONMENT):
        task_path = os.environ.get("TESTBENCH_TASK")
        if not task_path:
            raise RuntimeError("请从测试上位机运行并配置 slaveSim 测试入口")
        environment = Environment.from_dict(json.loads(Path(task_path).read_text(encoding="utf-8"))["environment"])
        url = validate_slavesim_url(environment.slavesim_url, required=True)
        timeout = environment.modbus_acquisition_timeout
        if type(timeout) not in (int, float) or not 5 <= timeout <= 300:
            raise ValueError("采集阶段等待时限必须为 5 至 300 秒")
    with track_fixture_step(request, SLAVESIM_BROWSER_READY):
        browser = request.getfixturevalue("slavesim_browser")
    desktop = request.getfixturevalue("fresh_desktop")
    agc = module == "AGC"
    context = SimpleNamespace(
        name=f"e2e-{module.lower()}-period-check-{uuid4().hex[:8]}", module=module, timeout=timeout,
        desktop=desktop, page=PlatformLoginPage(desktop), rtu=ModbusRtuPage(desktop), sim_started=False,
        group=AgcDirectionSetup(desktop) if agc else AvcPeriodPage(desktop),
        dialog=AgcDirectionSetup.GROUP_DIALOG if agc else GROUP,
    )
    run_steps(request, LOGIN, context)
    with track_fixture_step(request, SITE_CONNECT):
        context.rtu.connect_site()
    with track_fixture_context(request, SIM_START, SIM_FINISH,
                               MeasurementExpirySimulator(desktop, url, browser=browser)) as simulator:
        context.simulator = simulator
        failure = None
        try:
            run_steps(request, CHECKS, context)
        except BaseException as error:
            failure = error
            try:
                desktop.capture("period-validation-failure", description=f"{module} 周期校验用例清理前现场")
            except Exception as capture_error:
                error.add_note(f"现场截图失败：{capture_error}")
            raise
        finally:
            _cleanup(request, context, failure)


@pytest.mark.case_group("AGC 控制")
@PLAN
@LOGIC
def test_agc_rejects_out_of_range_control_periods(request):
    """AGC 拒绝越界的计算周期和命令最小间隔"""
    _run(request, "AGC")


@pytest.mark.case_group("AVC 控制")
@PLAN
@LOGIC
def test_avc_rejects_out_of_range_control_periods(request):
    """AVC 拒绝越界的计算周期和命令最小间隔"""
    _run(request, "AVC")
