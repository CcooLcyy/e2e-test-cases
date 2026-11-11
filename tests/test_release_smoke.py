from types import SimpleNamespace

import pytest
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC

from testbench.case_plan import (
    DESKTOP_FINISH, DESKTOP_START, Step, case_plan, desktop_action, desktop_check, run_case_plan,
)
from testbench.login_page import PlatformLoginPage
from case_support.login_plan import ready_steps

pytestmark = pytest.mark.case_group("启动与界面")
HEADER = (By.CSS_SELECTOR, ".platform-login-connection .ant-collapse-header")
ADDRESS = (By.CSS_SELECTOR, 'input[placeholder="https://platform.example:50000"]')


def capture_step(id, description, expected, actual):
    return Step(id, f"截图：{description}", expected,
                lambda c: c.desktop.capture(id, description=description, expected=expected, actual=actual))


def toggle_steps(expanded):
    action = "展开" if expanded else "折叠"
    state = "true" if expanded else "false"
    field_condition = EC.visibility_of_element_located if expanded else EC.invisibility_of_element_located
    field_state = "可见" if expanded else "隐藏"
    prefix = "expand" if expanded else "collapse"
    return (
        desktop_action(f"{prefix}.click", f"点击{action}平台连接设置", f"请求{action}设置",
                       lambda c: c.header.click()),
        desktop_action(f"{prefix}.wait", f"等待设置{action}状态同步", f"设置处于{action}状态",
                       lambda c: c.desktop.wait.until(lambda _: c.header.get_attribute("aria-expanded") == state)),
        desktop_action(f"{prefix}.address", f"等待平台地址输入框{field_state}", f"平台地址输入框{field_state}",
                       lambda c: c.desktop.wait.until(field_condition(ADDRESS))),
    )


def _address_editable(context):
    field = context.desktop.driver.find_element(*ADDRESS)
    return field.is_enabled() and field.get_dom_attribute("readonly") is None


@case_plan(
    DESKTOP_START,
    *ready_steps("login"),
    desktop_action("settings.ready", "等待平台连接设置入口可点击", "设置入口可操作",
                   lambda c: c.desktop.wait.until(EC.element_to_be_clickable(HEADER)), save_as="header"),
    desktop_check("settings.label", "设置入口文字", "显示平台连接设置",
                  lambda c: "平台连接设置" in c.header.text, True),
    desktop_check("settings.initial", "设置初始折叠", "设置默认折叠",
                  lambda c: c.header.get_attribute("aria-expanded"), "false"),
    desktop_check("address.initial", "地址输入框初始隐藏", "地址输入框不可见",
                  lambda c: any(field.is_displayed() for field in c.desktop.driver.find_elements(*ADDRESS)), False),
    capture_step("01-login", "平台连接设置初始折叠的登录页",
                 "登录页包含平台连接设置入口，地址输入框初始隐藏",
                 "已验证上位机登录页可见、设置入口文字正确、设置折叠且地址输入框隐藏"),
    *toggle_steps(True),
    desktop_check("address.editable", "展开后地址输入框可编辑", "地址输入框可编辑",
                  _address_editable, True),
    capture_step("02-settings-expanded", "展开平台连接设置后的地址输入框",
                 "点击设置入口后展开地址输入框，并允许编辑",
                 "已等待设置展开及输入框可见，并验证地址输入框可编辑"),
    *toggle_steps(False),
    capture_step("03-settings-collapsed", "再次折叠平台连接设置后的登录页",
                 "再次点击设置入口后收起地址输入框",
                 "已等待设置折叠，且地址输入框不可见"),
    DESKTOP_FINISH,
)
def test_release_platform_settings_expand_and_collapse(request, fresh_desktop):
    """平台连接设置可以展开并折叠。"""
    run_case_plan(request, SimpleNamespace(desktop=fresh_desktop, page=PlatformLoginPage(fresh_desktop)))
