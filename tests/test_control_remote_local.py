"""从 DSP 上位机控制策略页直接验收 AGC、AVC 远方与就地切换。"""

import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By

from case_support.agc_direction_setup import AgcDirectionSetup
from case_support.avc_period_page import SUMMARY, AvcPeriodPage
from case_support.case_metadata import execution_logic
from case_support.login_plan import login_steps
from case_support.measurement_expiry_run import prepare_control_rtu
from case_support.measurement_expiry_simulator import MeasurementExpirySimulator
from case_support.modbus_rtu_page import ModbusRtuPage, xpath_text
from case_support.power_range_run import _load_account
from testbench.case_plan import (
    DESKTOP_FINISH, DESKTOP_START, SITE_CONNECT, SLAVESIM_BROWSER_READY,
    Step, case_plan, run_steps, track_fixture_context, track_fixture_step,
)
from testbench.login_page import PlatformLoginPage
from testbench.model import Environment, validate_slavesim_url


pytestmark = pytest.mark.requires_modbus

STRATEGY_PANEL = ("//div[contains(concat(' ',normalize-space(@class),' '),' control-strategy-split ')]"
                  "/div[contains(concat(' ',normalize-space(@class),' '),' resizable-split-pane--second ')]")
DEFAULT_POINTS = ("//div[contains(@class,'protocol-point-card')]"
                  "[.//*[contains(@class,'ant-card-head-title') and normalize-space(.)='默认点']]")
BOOL_LABEL = {"远方": "是", "就地": "否"}


def _prepare_rtu(context):
    specs = {"A": (40, 0)} if context.module == "AGC" else {"A": (0, 0), "V": (220,)}
    prepare_control_rtu(context, specs)


def _prepare_group(context):
    group = context.group
    a = context.connections["A"]
    if context.module == "AGC":
        identity = group.create_group(context.name, "A", {"name": a}, "measured", "setpoint")
        context.conn_id = identity["conn_id"]
    else:
        group.create_group(
            context.name, mode="q_total", calculation_period=1, command_period=4,
            voltage_tag="measured", voltage_connection=context.connections["V"],
            target_tag=f"{context.name}-target", rtu_connection=a,
            q_measurement_tag="measured", q_setpoint_tag="setpoint", member_name="A",
        )
        context.conn_id = group.conn_id


def _select_strategy_group(context):
    group = context.group
    if context.module == "AGC":
        group._control_view("控制策略")
        group._select_owned_group()
    else:
        group._select_owned()


def _strategy_mode(context):
    group = context.group
    if context.module == "AGC":
        overview = group._selected_overview()
        if overview is None:
            return None
        tags = overview.find_elements(By.CSS_SELECTOR, ".agc-runtime-heading .ant-tag")
    else:
        if (group._read_summary("控制组名称") != context.name
                or group._read_summary("连接 ID") != str(context.conn_id)):
            return None
        tags = group.driver.find_elements(By.XPATH, SUMMARY + "//span[contains(@class,'ant-tag')]")
    modes = [text for tag in tags if tag.is_displayed() and (text := tag.text.strip()) in BOOL_LABEL]
    return modes[0] if len(modes) == 1 else None


def _wait_value(context, label, expected, read):
    actual = None

    def matches(_):
        nonlocal actual
        actual = read()
        return actual == expected

    try:
        context.group._until(matches, label)
    except TimeoutException:
        pass  # 保留超时前实际读到的值，由业务断言生成失败证据。
    context.desktop.check(label, actual, expected)


def _read_default_point(context, mode):
    group = context.group
    tab = (By.XPATH, "//*[@aria-label='控制页面切换']//*[@role='tab'][normalize-space(.)='默认点']")
    group._click(tab, f"打开 {context.module} 默认点页回读远方操作状态")
    item = group._until(lambda _: group._control_group_item(context.name), "默认点页未显示本例控制组")
    context.desktop.check("核对默认点所属控制组连接 ID", group._group_identity(item), context.conn_id)
    item.click()
    kind = 8 if context.module == "AGC" else 12
    name = f"{context.module}远方操作"
    row_xpath = ".//tbody/tr[@data-row-key=" + xpath_text(f"{kind}-{name}") + "]"

    def read():
        card = group._visible((By.XPATH, DEFAULT_POINTS))
        if card is None:
            return None
        rows = card.find_elements(By.XPATH, row_xpath)
        if len(rows) != 1:
            return None
        headers = [header.text.strip() for header in card.find_elements(By.CSS_SELECTOR, "thead th")]
        cells = rows[0].find_elements(By.CSS_SELECTOR, ":scope > td")
        if headers.count("最新值") == 1 and len(headers) == len(cells):
            return cells[headers.index("最新值")].text.strip()
        return None

    _wait_value(context, f"回读默认点 {name}（{mode}）", BOOL_LABEL[mode], read)


def _observe_mode(context, mode):
    _select_strategy_group(context)
    _wait_value(context, "核对本例控制策略页的当前模式", mode, lambda: _strategy_mode(context))
    _read_default_point(context, mode)


def _switch_mode(context, before, after):
    _select_strategy_group(context)
    _wait_value(context, "切换前核对本例控制策略页的当前模式", before, lambda: _strategy_mode(context))
    group = context.group
    action = f"切换至{after}"
    labels = " or ".join(
        f"normalize-space(.)={xpath_text(label)} or @aria-label={xpath_text(label)}"
        for label in (action, f"切换为{after}", f"切换到{after}")
    )
    locator = (By.XPATH, STRATEGY_PANEL + f"//*[self::button or @role='button'][{labels}]")
    with context.desktop.step(f"在控制策略页直接{action}", expected=f"本例控制组有可用的「{action}」操作"):
        found = False

        def ready(_):
            nonlocal found
            button = group._visible(locator)
            if button is None:
                return None
            found = True
            return button if button.is_enabled() and button.get_attribute("aria-disabled") != "true" else None

        try:
            button = group._until(ready, f"「{action}」操作未就绪")
        except TimeoutException as error:
            actual = "操作入口不可用" if found else "没有直接操作入口"
            raise AssertionError(f"{context.module} 控制策略页显示「{before}」，「{action}」{actual}") from error
        assert _strategy_mode(context) == before, f"点击「{action}」前，本例控制组已不在「{before}」状态"
        button.click()


ENVIRONMENT = Step("remote-local.environment", "核对 slaveSim 测试入口", "模拟器地址和采集等待时间有效")
SIM_START = Step("remote-local.sim.start", "打开本例 slaveSim 会话", "可读写本例模拟配置")
SIM_FINISH = Step("remote-local.sim.finish", "关闭本例 slaveSim 会话", "独立浏览器会话关闭")
UPPER_CLEANUP = Step("remote-local.upper.cleanup", "删除本例控制组、RTU 连接及路由", "本例配置已清理")
SIM_CLEANUP = Step("remote-local.sim.cleanup", "清空 slaveSim 总线", "回读确认总线数为 0")
LOGIN = (Step("remote-local.account", "读取测试账号", "专用账号配置有效", _load_account),
         *login_steps("remote-local.login"))
CHECKS = (
    Step("remote-local.rtu", "建立本例模拟 RTU 量测和设点", "上位机读到模拟初始量测", _prepare_rtu),
    Step("remote-local.group", "新建本例控制组并绑定模拟成员", "上位机读回专用控制组和点位", _prepare_group),
    Step("remote-local.start", "启动本例控制组", "控制组进入运行中", lambda context: context.group.start()),
    Step("remote-local.baseline", "核对切换前的远方状态", "策略页为远方，默认点最新值为是",
         lambda context: _observe_mode(context, "远方")),
    Step("remote-local.to-local", "在控制策略页把远方切到就地", "能直接操作切换至就地",
         lambda context: _switch_mode(context, "远方", "就地")),
    Step("remote-local.read-local", "回读切换后的就地状态", "策略页为就地，默认点最新值为否",
         lambda context: _observe_mode(context, "就地")),
    Step("remote-local.to-remote", "在控制策略页把就地切回远方", "能直接操作切换至远方",
         lambda context: _switch_mode(context, "就地", "远方")),
    Step("remote-local.read-remote", "回读切换后的远方状态", "策略页为远方，默认点最新值为是",
         lambda context: _observe_mode(context, "远方")),
)
PLAN = case_plan(ENVIRONMENT, SLAVESIM_BROWSER_READY, DESKTOP_START, *LOGIN, SITE_CONNECT, SIM_START,
                 *CHECKS, UPPER_CLEANUP, SIM_CLEANUP, SIM_FINISH, DESKTOP_FINISH)
LOGIC = execution_logic("\n".join(f"{index}. {step.action}。预期 {step.expected}。"
                                  for index, step in enumerate((*CHECKS, UPPER_CLEANUP, SIM_CLEANUP), 1)))


def _cleanup(request, context):
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
        if not errors:
            errors.append(str(error))
    if context.sim_started:
        try:
            with track_fixture_step(request, SIM_CLEANUP):
                context.simulator.clear_all_buses()
        except Exception as error:
            errors.append(f"模拟总线：{error}")
    return errors


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
    context = SimpleNamespace(
        name=f"e2e-{module.lower()}-mode-{uuid4().hex[:8]}", module=module, timeout=timeout,
        desktop=desktop, page=PlatformLoginPage(desktop), rtu=ModbusRtuPage(desktop), sim_started=False,
        group=AgcDirectionSetup(desktop) if module == "AGC" else AvcPeriodPage(desktop),
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
                desktop.capture("remote-local-failure", description=f"{module} 远方／就地切换失败，清理前现场")
            except Exception as capture_error:
                error.add_note(f"现场截图失败：{capture_error}")
            raise
        finally:
            if errors := _cleanup(request, context):
                detail = "清理本例资源失败：" + "；".join(errors)
                if failure is None:
                    raise RuntimeError(detail)
                failure.add_note(detail)


@pytest.mark.case_group("AGC 控制")
@PLAN
@LOGIC
def test_agc_switches_remote_local_in_control_strategy(request):
    """AGC 在控制策略页从远方切至就地并切回远方"""
    _run(request, "AGC")


@pytest.mark.case_group("AVC 控制")
@PLAN
@LOGIC
def test_avc_switches_remote_local_in_control_strategy(request):
    """AVC 在控制策略页从远方切至就地并切回远方"""
    _run(request, "AVC")
