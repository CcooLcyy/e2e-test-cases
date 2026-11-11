"""通过真实桌面重开验证记住密码与下次自动登录，全部用例独立准备状态。"""

import pytest

from testbench.case_plan import (
    DESKTOP_FINISH, DESKTOP_START, LOGIN_CLEANUP, LOGIN_GUARD, LOGIN_PREPARE,
    Step, case_plan, desktop_action, run_case_plan,
)
from case_support.login_plan import (
    fill_steps, form_steps, login_steps, logout_steps, option_check_steps, option_steps,
    password_check, reopen, submit_steps, username_step, wait_until_password_is_empty, workspace_steps,
)

pytestmark = pytest.mark.case_group("密码记忆与自动登录")


def session_plan(*steps):
    return case_plan(LOGIN_PREPARE, DESKTOP_START, LOGIN_GUARD, *steps, LOGIN_CLEANUP, DESKTOP_FINISH)


def capture(id, description, expected, actual):
    return Step(f"capture.{id}", f"截图：{description}", expected,
                lambda c: c.desktop.capture(id, description=description, expected=expected, actual=actual))


@session_plan(
    *login_steps("initial"),
    reopen("reopen"),
    *form_steps("form", remember=False, auto=False, filled=False),
    capture("no-remember-after-reopen",
            "未记住密码时重开后的登录表单",
            "保留原账号，密码为空；记住密码和自动登录均未勾选，等待手动登录",
            "已验证账号保留、密码为空、两项均未勾选，且仍需手动登录"),
)
def test_no_remember_requires_password_after_reopen(login_memory, request):
    """不记住密码时重开上位机仍需输入密码。"""
    run_case_plan(request, login_memory)


@session_plan(
    *login_steps("initial", remember=True),
    reopen("reopen"),
    *form_steps("form", remember=True, auto=False, filled=True),
    capture("remember-password-after-reopen",
            "仅记住密码时重开后的登录表单",
            "回填原账号和密码，密码保持掩码；仅勾选记住密码，等待手动登录",
            "已验证账号和密码回填、密码掩码及两项勾选状态，页面仍等待手动登录"),
    *submit_steps("manual_submit"),
    *workspace_steps("manual_workspace", compare_badge=True),
    capture("remember-password-manual-login",
            "直接使用回填密码手动登录后的工作台",
            "点击登录后进入原账号工作台",
            "已验证工作台可见、退出入口可见，且登录账号与重开前一致"),
)
def test_remember_password_requires_manual_login_after_reopen(login_memory, request):
    """仅记住密码时重开回填密码，手动点击后登录成功。"""
    run_case_plan(request, login_memory)


@session_plan(
    *login_steps("initial", auto=True),
    reopen("reopen"),
    # 重开后不填表、不点击登录，直接等待工作台作为通过条件。
    *workspace_steps("auto_workspace", compare_badge=True),
    capture("auto-login-after-reopen",
            "自动登录后进入原账号工作台",
            "重开后无需填表或点击登录，自动进入原账号工作台",
            "重开后未操作登录表单，已验证工作台及退出入口可见，账号与重开前一致"),
    *logout_steps("logout"),
    *form_steps("logout_form", remember=True, auto=True, filled=True),
)
def test_auto_login_enters_same_account_after_reopen(login_memory, request):
    """下次自动登录在重开上位机后自动进入原账号工作台。"""
    run_case_plan(request, login_memory)


@session_plan(
    *fill_steps("fill"),
    *option_check_steps("initial_options", False, False),
    *option_steps("enable_auto", "auto_login", True),
    *option_check_steps("linked_options", True, True),
    *option_steps("disable_auto", "auto_login", False),
    *option_check_steps("remember_options", True, False),
    *submit_steps("submit"),
    *workspace_steps("workspace"),
    reopen("reopen"),
    *form_steps("form", remember=True, auto=False, filled=True),
    capture("checkbox-linkage-persisted",
            "取消自动登录后重开的登录表单",
            "仍回填原账号和掩码密码，记住密码已勾选、自动登录未勾选",
            "已验证账号和密码回填、密码保持掩码、记住密码已勾选，且等待手动登录"),
)
def test_auto_login_and_remember_password_checkbox_linkage(login_memory, request):
    """自动登录会勾选记住密码，单独取消自动登录仍记住密码。"""
    run_case_plan(request, login_memory)


@session_plan(
    *login_steps("initial", remember=True),
    reopen("initial_reopen"),
    *form_steps("initial_form", remember=True, auto=False, filled=True),
    *option_steps("enable_auto", "auto_login", True),
    *option_check_steps("linked_options", True, True),
    *option_steps("disable_remember", "remember_password", False),
    *option_check_steps("cleared_options", False, False),
    password_check("cleared_password", lambda c: "", filled=False),
    # 不重新登录或保存连接设置，证明取消操作本身已持久化。
    reopen("final_reopen"),
    *form_steps("final_form", remember=False, auto=False, filled=False),
    capture("forget-password-after-reopen",
            "取消记住密码后重开的登录表单",
            "保留原账号，密码为空，记住密码和自动登录均未勾选",
            "取消记住密码后未重新登录，重开后已验证密码为空、两项均未勾选，并等待手动登录"),
)
def test_forget_password_clears_saved_password_and_auto_login(login_memory, request):
    """取消记住密码后清空密码并关闭自动登录，重开后密码仍为空。"""
    run_case_plan(request, login_memory)


@session_plan(
    *login_steps("initial", auto=True),
    *logout_steps("logout"),
    *form_steps("logout_form", remember=True, auto=True, filled=True),
    capture("explicit-logout-login-form",
            "主动退出后的登录表单",
            "保留原账号和掩码密码，记住密码和下次自动登录均勾选，停留在登录页",
            "已验证原账号和密码回填、密码保持掩码、两项仍勾选，且停留在登录页"),
    reopen("reopen"),
    *workspace_steps("reopened_workspace", compare_badge=True),
    capture("explicit-logout-after-reopen",
            "主动退出后重开自动进入原账号工作台",
            "重开后无需填表或点击登录，自动进入原账号工作台",
            "重开后未操作登录表单，已验证工作台及退出入口可见，账号与退出前一致"),
)
def test_explicit_logout_preserves_auto_login_after_reopen(login_memory, request):
    """主动退出后保留自动登录，重开上位机仍自动进入原账号工作台。"""
    run_case_plan(request, login_memory)


def alternate_username(context):
    return "testbench-other" if context.account.username != "testbench-other" else "testbench-other-2"


@session_plan(
    *login_steps("initial", remember=True),
    reopen("reopen"),
    *form_steps("initial_form", remember=True, auto=False, filled=True),
    username_step("other_account", "切换为其他登录账号", "账号输入框接收其他账号", alternate_username),
    desktop_action("wait_for_empty_password", "等待切换账号后密码清空", "账号已切换且密码为空",
                   lambda c: wait_until_password_is_empty(c, alternate_username(c))),
    password_check("empty_password", lambda c: "", filled=False),
    capture("different-account-empty-password",
            "切换为其他账号后的空密码表单",
            "切换账号时清空原账号密码",
            "已等待账号切换完成并验证密码为空"),
    username_step("original_account", "切回原测试账号", "账号输入框接收原测试账号", lambda c: c.account.username),
    *form_steps("restored_form", remember=True, auto=False, filled=True),
    capture("original-account-password-restored",
            "切回原账号后的密码回填表单",
            "恢复原账号后回填原密码，保持掩码显示，等待手动登录",
            "已验证原账号和密码回填、密码保持掩码及登录选项，页面仍等待手动登录"),
)
def test_changing_account_does_not_reuse_saved_password(login_memory, request):
    """修改已记住密码的账号时密码清空，切回原账号才能回填。"""
    run_case_plan(request, login_memory)
