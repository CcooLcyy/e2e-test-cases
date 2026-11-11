"""错误密码应显示明确的认证错误提示，保持独立验收标准。"""
import re
import secrets
import unicodedata

import pytest
from selenium.common.exceptions import StaleElementReferenceException
from selenium.webdriver.common.by import By

from testbench.case_plan import (
    DESKTOP_FINISH, DESKTOP_START, LOGIN_CLEANUP, LOGIN_GUARD, LOGIN_PREPARE,
    Step, case_plan, desktop_check, run_case_plan,
)
from testbench.login_page import LoginAccount, PlatformLoginPage
from case_support.login_plan import configure_steps, fill_steps, login_steps, logout_steps, ready_steps, submit_steps

pytestmark = pytest.mark.case_group("基础登录")
ERROR_SELECTOR = ".platform-login-form-panel .ant-alert-error"
ERROR_TARGETS = PlatformLoginPage.target((By.CSS_SELECTOR, ERROR_SELECTOR), "检查认证错误提示")
EXPLICIT_ERROR = "账号或密码错误，请重新输入（或相同明确语义）"


def _is_explicit_password_error(message):
    normalized = "".join(char for char in message if not char.isspace()
                         and not unicodedata.category(char).startswith("P"))
    return re.fullmatch(
        r"(?:登录失败)?(?:(?:账号|帐号|账户|用户名)或)?密码(?:错误|不正确|有误|不匹配)"
        r"(?:请(?:重新输入(?:密码)?|重新登录|重试))?", normalized,
    ) is not None


def _make_wrong_account(case):
    # 使用固定有效长度的随机错误密码，避开空值或长度校验。
    candidate = secrets.token_urlsafe(18)
    if candidate == case.account.password:
        candidate = ("A" if candidate[0] != "A" else "B") + candidate[1:]
    case.wrong_account = LoginAccount(case.account.username, candidate)


def _wait_error(case):
    def visible_error(driver):
        try:
            if any(item.is_displayed() for item in driver.find_elements(*case.page.LOGOUT)):
                raise AssertionError("错误密码提交后进入了工作台")
            messages = [item.text.strip() for item in driver.find_elements(By.CSS_SELECTOR, ERROR_SELECTOR)
                        if item.is_displayed() and item.text.strip()]
            return "\n".join(messages) if messages else False
        except StaleElementReferenceException:
            return False

    with case.desktop.step("等待错误密码认证提示显示", targets=ERROR_TARGETS):
        case.actual_message = case.desktop.wait.until(visible_error, message="错误密码提交后未显示登录错误提示")


def _editable(element):
    return element.is_enabled() and not element.get_attribute("readonly")


def _verify_error(case):
    actual_message = case.actual_message
    safe_message = actual_message.replace(case.account.password, "[已隐藏密码]")
    case.safe_message = safe_message.replace(case.wrong_account.password, "[已隐藏密码]")
    with case.desktop.step("验证错误提示明确说明账号或密码错误",
                           expected=EXPLICIT_ERROR, actual=case.safe_message, targets=ERROR_TARGETS):
        case.desktop.check("提示符合明确认证错误标准", _is_explicit_password_error(actual_message), True,
                           targets=ERROR_TARGETS)


def _capture_error(case):
    case.desktop.capture(
        "wrong-password-message",
        description="错误密码被拒绝后的认证提示与可重试登录表单",
        expected="显示明确的账号或密码错误提示，仍在登录页并允许重新输入，密码保持掩码",
        actual=f"已验证认证错误提示符合标准、未进入工作台、账号密码可编辑且密码保持掩码；实际提示：{case.safe_message}",
        targets=ERROR_TARGETS,
    )


@case_plan(
    LOGIN_PREPARE, DESKTOP_START, LOGIN_GUARD,
    *login_steps("correct"),
    *logout_steps("logout"),
    *ready_steps("after-logout"),
    Step("wrong.generate", "生成非空错误密码", "准备错误密码用于独立验证", _make_wrong_account),
    desktop_check("wrong.different", "错误密码与正确密码不同", "两个密码不同",
                  lambda c: c.wrong_account.password != c.account.password, True),
    desktop_check("wrong.length", "错误密码长度", "错误密码长 24 字符",
                  lambda c: len(c.wrong_account.password), 24),
    *fill_steps("wrong.fill", account=lambda c: c.wrong_account),
    *configure_steps("wrong.options"),
    *submit_steps("wrong.submit"),
    Step("wrong.error", "等待错误密码认证提示显示", "界面显示登录错误提示", _wait_error),
    desktop_check("wrong.login", "仍停留在登录页", "登录页可见",
                  lambda c: c.desktop.driver.find_element(*c.page.SCREEN).is_displayed(), True),
    desktop_check("wrong.workspace", "没有进入工作台", "工作台未出现",
                  lambda c: any(item.is_displayed() for item in c.desktop.driver.find_elements(*c.page.LOGOUT)), False),
    *ready_steps("retry"),
    desktop_check("retry.submit", "登录按钮可操作", "登录按钮可用",
                  lambda c: c.desktop.driver.find_element(*c.page.SUBMIT).is_enabled(), True),
    desktop_check("retry.username", "账号输入框可编辑", "允许重新输入账号",
                  lambda c: _editable(c.page.username_input()), True),
    desktop_check("retry.password", "密码输入框可编辑", "允许重新输入密码",
                  lambda c: _editable(c.page.password_input()), True),
    desktop_check("retry.mask", "密码保持掩码显示", "密码输入框为掩码类型",
                  lambda c: c.page.password_input().get_attribute("type"), "password"),
    Step("wrong.error.check", "验证错误提示明确说明账号或密码错误", EXPLICIT_ERROR, _verify_error),
    Step("wrong.capture", "保存错误密码验证截图", "记录错误提示和可重试表单", _capture_error),
    LOGIN_CLEANUP, DESKTOP_FINISH,
)
def test_wrong_password_reports_explicit_credentials_error(request, login_memory):
    """正确账号输入错误密码时明确提示认证失败并允许重新输入。"""
    run_case_plan(request, login_memory)
