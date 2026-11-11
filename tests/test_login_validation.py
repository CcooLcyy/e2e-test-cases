"""登录页外部行为用例，不需要平台账号或连接下位机。"""

from types import SimpleNamespace

import pytest
from selenium.webdriver.common.by import By

from testbench.case_plan import (
    DESKTOP_FINISH, DESKTOP_START, Step, case_plan, desktop_action, desktop_check, run_case_plan,
)
from testbench.login_page import PlatformLoginPage
from case_support.case_metadata import execution_logic
from case_support.login_plan import password_check, ready_steps, submit_steps

pytestmark = pytest.mark.case_group("基础登录")
PAGE = PlatformLoginPage
USERNAME_TARGET = PAGE.target(PAGE.USERNAME, "检查空账号输入框")
SUBMIT_TARGET = PAGE.target(PAGE.SUBMIT, "提交空账号密码")
USERNAME_ERROR = PAGE.target((By.XPATH, '//div[contains(@class,"ant-form-item-explain-error") and normalize-space(.)="请输入平台账号"]'), "检查账号必填提示")
PASSWORD_ERROR = PAGE.target((By.XPATH, '//div[contains(@class,"ant-form-item-explain-error") and normalize-space(.)="请输入密码"]'), "检查密码必填提示")


def _visible_messages(driver):
    messages = [item.text.strip() for item in driver.find_elements(
        By.CSS_SELECTOR, ".ant-form-item-explain-error") if item.is_displayed()]
    return messages if {"请输入平台账号", "请输入密码"}.issubset(messages) else False


def _capture_validation(context):
    context.desktop.capture(
        "empty-login-validation",
        description="提交空表单后的账号和密码必填提示",
        expected="同时显示“请输入平台账号”和“请输入密码”",
        actual="已等待两项必填提示出现，并逐项验证提示文本",
        targets=USERNAME_ERROR + PASSWORD_ERROR,
    )


@case_plan(
    DESKTOP_START,
    *ready_steps("login"),
    desktop_check("username.empty", "账号初始值", "账号为空",
                  lambda c: c.page.username_input().get_attribute("value"), "", targets=USERNAME_TARGET),
    password_check("password.empty", lambda _: "", filled=False),
    *submit_steps("submit", wait_name="等待空表单登录按钮可点击", click_name="点击提交空账号密码",
                  targets=SUBMIT_TARGET),
    desktop_action("errors.wait", "等待账号和密码必填提示同时显示", "两项必填提示同时可见",
                   lambda c: c.desktop.wait.until(_visible_messages),
                   targets=USERNAME_ERROR + PASSWORD_ERROR, save_as="messages"),
    desktop_check("username.error", "账号必填提示", "显示「请输入平台账号」",
                  lambda c: "请输入平台账号" in c.messages, True, targets=USERNAME_ERROR),
    desktop_check("password.error", "密码必填提示", "显示「请输入密码」",
                  lambda c: "请输入密码" in c.messages, True, targets=PASSWORD_ERROR),
    Step("validation.capture", "保存必填提示截图", "记录两项必填提示", _capture_validation),
    desktop_check("login.remains_visible", "登录页可见", "仍停留在登录页",
                  lambda c: c.desktop.driver.find_element(*PAGE.SCREEN).is_displayed(), True,
                  targets=PAGE.target(PAGE.SCREEN, "检查仍停留在登录页")),
    DESKTOP_FINISH,
)
@execution_logic("""
1. 启动独立的 DSP 上位机副本，等待登录表单就绪，核对账号和密码初始均为空。
2. 保持两个输入框为空，点击登录，等待两个字段的必填提示同时出现。
3. 逐项核对「请输入平台账号」和「请输入密码」，保存提示截图，并确认登录页仍然可见。
""")
def test_empty_login_reports_required_fields(request, fresh_desktop):
    """空账号和密码提示必填"""
    run_case_plan(request, SimpleNamespace(desktop=fresh_desktop, page=PAGE(fresh_desktop)))
