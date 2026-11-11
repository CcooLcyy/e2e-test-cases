from pathlib import Path
from contextlib import contextmanager
import json
import os
from types import SimpleNamespace

import pytest

from desktop_demo import ReleaseDesktop
from testbench.case_plan import (
    DESKTOP_FINISH, DESKTOP_START, LOGIN_CLEANUP, LOGIN_GUARD, LOGIN_PREPARE,
    MASTER104_FINISH, MASTER104_START, track_fixture_context, track_fixture_step,
)
from testbench.workspace import local_config


# 直接运行 pytest 与界面工作进程共用同一套自动证据和报告逻辑。
pytest_plugins = ("testbench.pytest_plugin",)


@pytest.fixture
def desktop(request):
    """仅创建和管理本次测试的桌面实例。"""
    with track_fixture_context(request, DESKTOP_START, DESKTOP_FINISH, _desktop_session(request)) as app:
        yield app


@pytest.fixture
def fresh_desktop(request):
    """为需要干净启动状态的用例创建独立副本。"""
    with track_fixture_context(request, DESKTOP_START, DESKTOP_FINISH,
                               _desktop_session(request, fresh=True)) as app:
        yield app


@pytest.fixture(scope="session")
def slavesim_browser():
    """同一任务只解析或准备一次系统 Edge 驱动，保留失败供所选协议用例报告。"""
    from testbench.edge_browser import prepare_system_edge

    return prepare_system_edge(local_config("environment.json").parent.parent / ".tools" / "system-edge")


@contextmanager
def _desktop_session(request, *, fresh=False):
    options = {}
    task_path = os.environ.get("TESTBENCH_TASK")
    if task_path:
        task = json.loads(Path(task_path).read_text(encoding="utf-8"))
        environment = task["environment"]
        options = {"mode": environment["mode"], "workspace_path": environment["workspace_path"],
                   "baseline_path": environment["baseline_path"],
                   "annotation_enabled": environment.get("annotation_enabled", True),
                   "close_app": environment["close_app"] or request.node.nodeid != task["case_ids"][-1]}
        if private_webview := task.get("private_webview"):
            options["private_webview_pair"] = (Path(private_webview["runtime_path"]), Path(private_webview["driver_path"]))
    if fresh:
        options.update(mode="fresh", workspace_path="")
    driver_path = request.config.getoption("--driver-path")
    if not driver_path and "private_webview_pair" not in options:
        pytest.fail("运行桌面用例需要指定 --driver-path")
    for option in ("--app-path", "--artifacts-dir"):
        if not request.config.getoption(option):
            pytest.fail(f"运行桌面用例需要指定 {option}")
    app = ReleaseDesktop(
        app_path=Path(request.config.getoption("--app-path")),
        driver_path=Path(driver_path) if driver_path else None,
        artifacts_dir=Path(getattr(request.node, "_bench_artifacts", request.config.getoption("--artifacts-dir"))),
        timeout=float(request.config.getoption("--ui-timeout")), **options,
    )
    request.node._bench_desktop = app
    app.metadata["用例"] = (getattr(request.node.obj, "__doc__", "") or request.node.name).strip()
    app.metadata["用例ID"] = request.node.nodeid
    with app:
        yield app
        reports = getattr(request.node, "_bench_reports", [])
        if any(report.failed for report in reports):
            app.failure = app.failure or "用例断言失败，详见任务报告"
    if app.cleanup_failure:
        pytest.fail(app.cleanup_failure)


@pytest.fixture
def master104(request):
    """为本用例启动独立的 104 主站，由业务用例建立协议连接。"""
    with track_fixture_context(request, MASTER104_START, MASTER104_FINISH, _master104_session(request)) as app:
        yield app


@contextmanager
def _master104_session(request):
    from testbench.master104 import Master104Session
    from testbench.model import Environment

    task_path = os.environ.get("TESTBENCH_TASK")
    if not task_path:
        pytest.fail("104主站会话需要测试上位机提供任务环境配置")
    task = json.loads(Path(task_path).read_text(encoding="utf-8"))
    environment = Environment.from_dict(task["environment"])
    environment.validate_master104(required=True)
    environment.validate_master104_driver()
    directory = getattr(request.node, "_bench_artifacts", request.config.getoption("--artifacts-dir"))
    if not directory:
        pytest.fail("104主站会话需要本例证据目录")
    app = Master104Session(artifacts_dir=Path(directory), path=environment.master104_path,
                           driver_path=environment.driver_path, timeout=environment.master104_timeout)
    request.node._bench_master104 = app
    app.metadata["用例"] = (getattr(request.node.obj, "__doc__", "") or request.node.name).strip()
    app.metadata["用例ID"] = request.node.nodeid
    with app:
        yield app
        if any(report.failed for report in getattr(request.node, "_bench_reports", [])):
            app.failure = app.failure or "用例断言失败，详见任务报告"
    if app.cleanup_failure:
        pytest.fail(app.cleanup_failure)


@pytest.fixture
def login_memory(request):
    """登录记忆用例始终独立运行，并保护系统中已有的平台凭据。"""
    from testbench.login_page import load_login_account, PlatformLoginPage
    from testbench.login_credentials import WindowsCredentialGuard, cleanup_login_credentials

    with track_fixture_step(request, LOGIN_PREPARE):
        account = load_login_account(local_config("login-account.json"))
    app = request.getfixturevalue("fresh_desktop")
    page = PlatformLoginPage(app)
    with track_fixture_step(request, LOGIN_GUARD), app.step("确认专用测试账号没有已有保存凭据"):
        guard = WindowsCredentialGuard(page.endpoint(), account.username)
        guard.__enter__()
    try:
        yield SimpleNamespace(desktop=app, page=page, account=account)
    finally:
        # 先停止可能仍在保存密码的请求，再删凭据；清理不能被取消标记跳过。
        with track_fixture_step(request, LOGIN_CLEANUP):
            cleanup_login_credentials(app, guard)
