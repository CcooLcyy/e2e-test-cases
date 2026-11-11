"""登录场景共用的逐步操作和检查。"""

from dataclasses import replace

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC

from testbench.case_plan import Step, desktop_action, desktop_check
from testbench.login_page import PlatformLoginPage as Page


def login_visible_step(id):
    return desktop_action(id, "等待登录页可见", "显示登录页面",
                          lambda c: c.desktop.wait.until(EC.visibility_of_element_located(Page.SCREEN)),
                          targets=Page.target(Page.SCREEN, "检查登录页面"))


def ready_steps(prefix):
    return (
        login_visible_step(f"{prefix}.visible"),
        desktop_action(f"{prefix}.initialized", "等待登录表单初始化或退出处理完成", "账号输入框和登录按钮可操作",
                       lambda c: c.desktop.wait.until(c.page._ready, message="登录表单未在规定时间内完成初始化或退出处理"),
                       targets=Page.target(Page.SUBMIT, "等待登录表单就绪")),
    )


def fill_steps(prefix, account=lambda c: c.account):
    return (
        *ready_steps(f"{prefix}.ready"),
        username_step(f"{prefix}.username", "输入专用测试账号", "登录表单接收测试账号",
                      lambda c: account(c).username),
        desktop_action(f"{prefix}.username_synced", "等待账号输入同步", "账号输入框显示测试账号",
                       lambda c: c.desktop.wait.until(lambda _: c.page.username_input().get_attribute("value") == account(c).username),
                       targets=Page.target(Page.USERNAME, "检查账号输入同步")),
        desktop_action(f"{prefix}.password", "输入测试密码", "密码输入框接收输入",
                       lambda c: c.page.enter_test_password(account(c).password),
                       targets=Page.target(Page.PASSWORD, "输入密码，保持掩码")),
        desktop_check(f"{prefix}.username_matches", "登录账号", "登录账号与测试账号一致",
                      lambda c: c.page.username_input().get_attribute("value"), lambda c: account(c).username,
                      targets=Page.target(Page.USERNAME, "检查登录账号")),
        password_check(f"{prefix}.password_matches", lambda c: account(c).password),
    )


def username_step(id, action, expected, username):
    return desktop_action(id, action, expected, lambda c: c.page.replace_text(c.page.username_input(), username(c)),
                          targets=Page.target(Page.USERNAME, "输入测试账号"))


def wait_until_password_is_empty(context, username):
    context.desktop.wait.until(lambda _: (
        context.page.username_input().get_attribute("value") == username
        and context.page.password_input().get_attribute("value") == ""
    ))


def password_check(id, password, *, filled=True):
    name = "密码与测试密码匹配" if filled else "密码为空"
    return Step(id, name, name, lambda c: c.page.check_password(password(c)))


def option_steps(prefix, option, checked):
    label = Page.LABELS[option]
    setting = f"{'勾选' if checked else '取消'}{label}"
    locator = Page.option_locator(option)
    click = desktop_action(f"{prefix}.click", setting, f"{label}切换为{'勾选' if checked else '未勾选'}",
                           lambda c: c.page.option_label(option).click(),
                           targets=Page.target(locator, f"设置{label}"))
    return (
        *ready_steps(f"{prefix}.before"),
        replace(click, when=lambda c: c.page.selected(option) != checked),
        desktop_action(f"{prefix}.synced", f"等待{label}状态同步", f"{label}处于{'勾选' if checked else '未勾选'}状态",
                       lambda c: c.desktop.wait.until(lambda _: c.page.selected(option) == checked),
                       targets=Page.target(locator, f"检查{label}状态")),
        *ready_steps(f"{prefix}.after"),
    )


def option_check_steps(prefix, remember, auto):
    def check(option, selected):
        label = Page.LABELS[option]
        return desktop_check(f"{prefix}.{option}", f"{label}勾选状态", "已勾选" if selected else "未勾选",
                             lambda c: c.page.selected(option), selected,
                             targets=Page.target(Page.option_locator(option), f"检查{label}状态"))

    return check("remember_password", remember), check("auto_login", auto)


def configure_steps(prefix, remember=False, auto=False):
    enable_auto = option_steps(f"{prefix}.enable_auto", "auto_login", True) if auto else ()
    return (
        *option_steps(f"{prefix}.disable_auto", "auto_login", False),
        *option_steps(f"{prefix}.remember", "remember_password", remember),
        *enable_auto,
        *option_check_steps(f"{prefix}.check", remember or auto, auto),
    )


def submit_steps(prefix, *, wait_name="等待登录按钮可点击", click_name="点击登录按钮", targets=None):
    wait_targets = Page.target(Page.SUBMIT, "等待登录按钮可点击") if targets is None else targets
    click_targets = Page.target(Page.SUBMIT, "点击登录") if targets is None else targets
    return (
        desktop_action(f"{prefix}.ready", wait_name, "登录按钮可点击",
                       lambda c: c.desktop.wait.until(EC.element_to_be_clickable(Page.SUBMIT)),
                       targets=wait_targets, save_as="login_button"),
        desktop_action(f"{prefix}.click", click_name, "提交登录表单",
                       lambda c: c.login_button.click(), targets=click_targets),
    )


def workspace_steps(prefix, compare_badge=False):
    steps = (
        desktop_action(f"{prefix}.visible", "等待登录页消失并进入工作台", "登录页隐藏，进入工作台",
                       lambda c: c.desktop.wait.until(c.page._workspace_visible, message="登录后未在规定时间内进入工作台"),
                       targets=Page.target(Page.LOGOUT, "检查工作台入口")),
        desktop_action(f"{prefix}.logout_visible", "等待工作台退出入口可见", "显示退出入口",
                       lambda c: c.desktop.wait.until(EC.visibility_of_element_located(Page.LOGOUT)),
                       targets=Page.target(Page.LOGOUT, "检查退出入口")),
        desktop_action(f"{prefix}.badge", "等待并读取工作台账号信息", "工作台显示账号信息",
                       lambda c: c.desktop.wait.until(EC.visibility_of_element_located(Page.BADGE)).text.strip(),
                       targets=Page.target(Page.BADGE_ANNOTATION, "读取工作台账号信息"),
                       save_as="reopened_badge" if compare_badge else "badge"),
        desktop_check(f"{prefix}.nonempty", "工作台账号信息非空", "工作台账号信息非空",
                      lambda c: bool(c.reopened_badge if compare_badge else c.badge), True,
                      targets=Page.target(Page.BADGE_ANNOTATION, "检查工作台账号信息")),
    )
    if not compare_badge:
        return steps
    return (*steps, desktop_check(f"{prefix}.same", "重开前后工作台账号一致", "工作台账号与重开前一致",
                                 lambda c: c.reopened_badge, lambda c: c.badge,
                                 targets=Page.target(Page.BADGE_ANNOTATION, "检查重开前后账号一致")))


def login_steps(prefix, remember=False, auto=False):
    return (
        *fill_steps(f"{prefix}.fill"),
        *configure_steps(f"{prefix}.options", remember, auto),
        *submit_steps(f"{prefix}.submit"),
        *workspace_steps(f"{prefix}.workspace"),
    )


def reopen(id):
    return desktop_action(id, "重新打开本次测试上位机", "同一测试副本重开并保留登录数据",
                          lambda c: c.desktop.restart())


def form_steps(prefix, remember, auto, filled):
    def expected_password(context):
        return context.account.password if filled else ""

    return (
        *ready_steps(f"{prefix}.ready"),
        desktop_action(f"{prefix}.synced", "等待账号、密码回填和登录选项同步",
                       "原账号、密码及登录选项均符合预期",
                       lambda c: c.page.wait_for_form_values(c.account.username, expected_password(c), remember, auto),
                       targets=Page.target(Page.USERNAME, "检查账号回填")
                       + Page.target(Page.PASSWORD, "检查密码回填，保持掩码")),
        desktop_check(f"{prefix}.username", "回填账号", "回填原测试账号",
                      lambda c: c.page.username_input().get_attribute("value"), lambda c: c.account.username,
                      targets=Page.target(Page.USERNAME, "检查回填账号")),
        password_check(f"{prefix}.password", expected_password, filled=filled),
        desktop_check(f"{prefix}.masked", "密码保持掩码显示", "密码框使用掩码",
                      lambda c: c.page.password_input().get_attribute("type"), "password",
                      targets=Page.target(Page.PASSWORD, "检查密码掩码显示")),
        *option_check_steps(f"{prefix}.options", remember, auto),
        desktop_action(f"{prefix}.signed_out", "持续确认仍等待手动登录", "登录页保持可见，未进入工作台",
                       lambda c: c.page.wait_signed_out(), targets=Page.target(Page.SCREEN, "检查仍停留在登录页")),
    )


def logout_steps(prefix):
    confirm = (By.XPATH, '//div[contains(@class,"ant-modal-confirm-btns")]//button[normalize-space(.)="退出登录"]')
    return (
        desktop_action(f"{prefix}.ready", "等待工作台退出按钮可点击", "退出按钮可点击",
                       lambda c: c.desktop.wait.until(EC.element_to_be_clickable(Page.LOGOUT)),
                       targets=Page.target(Page.LOGOUT, "等待退出按钮可点击"), save_as="logout_button"),
        desktop_action(f"{prefix}.click", "点击工作台退出按钮", "显示退出确认对话框",
                       lambda c: c.logout_button.click(), targets=Page.target(Page.LOGOUT, "点击退出")),
        desktop_action(f"{prefix}.confirm_ready", "等待退出确认按钮可点击", "退出确认按钮可点击",
                       lambda c: c.desktop.wait.until(EC.element_to_be_clickable(confirm)),
                       targets=Page.target(confirm, "等待退出确认"), save_as="confirm_logout_button"),
        desktop_action(f"{prefix}.confirm", "确认退出登录", "开始退出登录",
                       lambda c: c.confirm_logout_button.click(), targets=Page.target(confirm, "确认退出登录")),
        *ready_steps(f"{prefix}.login"),
    )
