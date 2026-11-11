"""使用本机测试账号验证真实平台登录及工作台入口。"""

from types import SimpleNamespace

import pytest

from testbench.case_plan import (
    DESKTOP_FINISH, DESKTOP_START, Step, case_plan, desktop_action, run_case_plan,
)
from testbench.login_page import PlatformLoginPage, load_login_account
from case_support.login_plan import login_steps
from testbench.workspace import local_config

pytestmark = pytest.mark.case_group("基础登录")
PAGE = PlatformLoginPage
LOGOUT_TARGET = PAGE.target(PAGE.LOGOUT, "检查工作台退出入口")
BADGE_TARGET = PAGE.target(PAGE.BADGE_ANNOTATION, "检查工作台账号信息")


def _capture_workspace(context):
    context.desktop.capture(
        "successful-login-workspace",
        description="正确账号登录后显示的工作台及退出入口",
        expected="登录页消失，工作台显示账号信息和退出入口",
        actual="已等待登录页消失，并验证退出入口可见、工作台账号信息非空",
        targets=BADGE_TARGET + LOGOUT_TARGET,
    )


@case_plan(
    DESKTOP_START,
    desktop_action("account.read", "读取本机测试账号", "账号和密码均非空",
                   lambda _: load_login_account(local_config("login-account.json")), save_as="account"),
    *login_steps("login"),
    Step("workspace.capture", "保存工作台截图", "记录账号信息和退出入口", _capture_workspace),
    DESKTOP_FINISH,
)
def test_valid_account_enters_workspace(request, fresh_desktop):
    """正确账号密码登录成功并进入配置工作台。"""
    run_case_plan(request, SimpleNamespace(desktop=fresh_desktop, page=PAGE(fresh_desktop)))
