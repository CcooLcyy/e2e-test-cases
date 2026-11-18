"""验证 IEC104 批量迁址与修改业务类型可以独立保存。"""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from selenium.webdriver.common.by import By

from testbench.case_plan import DESKTOP_FINISH, DESKTOP_START, SITE_CONNECT, Step, case_plan, run_steps, track_fixture_step
from testbench.login_page import PlatformLoginPage, load_login_account
from testbench.workspace import local_config

from case_support.case_metadata import execution_logic
from case_support.iec104_batch_edit import BatchEditIec104
from case_support.login_plan import login_steps
from case_support.modbus_rtu_page import ModbusRtuPage


pytestmark = pytest.mark.case_group("IEC104")

MOVING = "batch-outside-range"
INSIDE = "batch-inside-range"
OCCUPIED = "batch-unselected-occupied"
SELECTED = (MOVING, INSIDE)
SINGLE = "SINGLE (单点值)"
FLOAT = "FLOAT (短浮点值)"


def _assert_point(context, tag, ioa, category, point_type, business):
    cells = context.iec104.point_cells(tag)
    address, _, address_category = cells[2].partition(")")
    context.desktop.check(
        f"核对 {tag} 的地址、地址类别、编码类型和业务类型",
        (cells[1], address + ")", address_category.strip(), cells[3], cells[4]),
        (tag, f"{ioa} (0x{ioa:06X})", category, point_type, business),
    )


def _prepare(context):
    page = context.iec104
    port = page.create()
    context.desktop.metadata["本例 IEC104 配置"] = {
        "链路": page.name, "端口": port, "目标地址段": "遥测", "目标业务类型": "参数",
        "初始 IOA": {MOVING: 1, INSIDE: 0x4001, OCCUPIED: 0x4002},
    }
    page.add_point(MOVING, 1, business="遥信", point_type=SINGLE)
    page.add_point(INSIDE, 0x4001)
    page.add_point(OCCUPIED, 0x4002)
    _assert_point(context, MOVING, 1, "遥信", SINGLE, "遥信")
    _assert_point(context, INSIDE, 0x4001, "遥测", FLOAT, "遥测")
    _assert_point(context, OCCUPIED, 0x4002, "遥测", FLOAT, "遥测")


def _preview_migration(context):
    page = context.iec104
    page.open_batch(SELECTED)
    page.choose_batch("IOA 业务类别", "遥测")
    page.check_counts(2, 1)
    preview = (By.XPATH, page.BATCH_DIALOG + "//ul[@aria-label='IOA 变更预览']")
    entries = page.upper._until(lambda _: page.upper._visible(preview), "未显示 IOA 变更预览")
    context.desktop.check("核对迁址预览仅列出段外点位且新地址避开已占用地址",
                          [item.text for item in entries.find_elements(By.TAG_NAME, "li")],
                          [f"{MOVING}：1 (0x000001) → 16387 (0x004003)"])


def _verify_migration(context):
    context.iec104.save_batch()
    context.iec104.reopen_points()
    _assert_point(context, MOVING, 0x4003, "遥测", SINGLE, "遥信")
    _assert_point(context, INSIDE, 0x4001, "遥测", FLOAT, "遥测")
    _assert_point(context, OCCUPIED, 0x4002, "遥测", FLOAT, "遥测")


def _preview_business_type(context):
    page = context.iec104
    page.open_batch(SELECTED)
    page.choose_batch("业务类型", "参数")
    page.check_counts(2, 2)
    preview = (By.XPATH, page.BATCH_DIALOG + "//ul[@aria-label='IOA 变更预览']")
    context.desktop.check("仅修改业务类型时不显示 IOA 变更预览", bool(page.upper._visible(preview)), False)


def _verify_business_type(context):
    context.iec104.save_batch()
    context.iec104.reopen_points()
    _assert_point(context, MOVING, 0x4003, "遥测", SINGLE, "参数")
    _assert_point(context, INSIDE, 0x4001, "遥测", FLOAT, "参数")
    _assert_point(context, OCCUPIED, 0x4002, "遥测", FLOAT, "遥测")


LOGIN = (
    Step("iec104-batch-ioa.account", "读取测试账号", "专用账号配置有效",
         lambda c: setattr(c, "account", load_login_account(local_config("login-account.json")))),
    *login_steps("iec104-batch-ioa.login"),
)
STEPS = (
    Step("iec104-batch-ioa.prepare", "建立专用 IEC104 链路和三个点位",
         "段外点、段内点及占用下一目标地址的未选点均以预设字段保存", _prepare),
    Step("iec104-batch-ioa.preview", "勾选段外与段内点位，批量设置 IOA 业务类别为遥测",
         "已选 2 个点位，预计修改 1 个；预览仅显示 IOA 1 → 16387", _preview_migration),
    Step("iec104-batch-ioa.save", "保存迁址并重新读取点表",
         "仅段外点迁入新地址；业务类型、编码类型和其他点位地址保持原值", _verify_migration),
    Step("iec104-batch-ioa.business", "批量设置两个选中点的业务类型为参数",
         "已选 2 个点位，预计修改 2 个；没有 IOA 变更预览", _preview_business_type),
    Step("iec104-batch-ioa.verify", "保存业务类型并重新读取点表",
         "两个选中点的业务类型为参数，IOA 和编码类型保持原值，未选点不变", _verify_business_type),
)
CLEANUP = Step("iec104-batch-ioa.cleanup", "清理本例 IEC104 链路",
               "仅删除本例专用链路及点表，刷新后确认已移除")


@case_plan(DESKTOP_START, *LOGIN, SITE_CONNECT, *STEPS, CLEANUP, DESKTOP_FINISH)
@execution_logic("""
1. 连接现场下位机，通过 DSP 上位机建立专用 IEC104 被控站和三个点位：IOA 1 的遥信、
   IOA 16385 的遥测，以及占用 IOA 16386 的未选遥测点。
2. 批量设置前两个点的 IOA 业务类别为遥测。检查预览只迁移第一个点，且从 1 迁到 16387。
   保存并重新读取点表，核对迁址保留业务类型和编码类型，段内与未选点地址不变。
3. 再次选中这两个点，只将业务类型批量改为参数。保存并重新读取点表，核对两个点的业务类型、
   保持原样的地址与编码类型，以及未选点。保留失败现场并清理本例专用链路。
""")
def test_iec104_batch_ioa_category_and_business_type(request):
    """IEC104 批量修改 IOA 业务类别和业务类型互不影响"""
    desktop = request.getfixturevalue("fresh_desktop")
    upper = ModbusRtuPage(desktop)
    context = SimpleNamespace(
        desktop=desktop, page=PlatformLoginPage(desktop),
        iec104=BatchEditIec104(upper, f"e2e-batch-ioa-{uuid4().hex[:8]}"),
    )
    failure = None
    try:
        run_steps(request, LOGIN, context)
        with track_fixture_step(request, SITE_CONNECT):
            upper.connect_site()
        run_steps(request, STEPS, context)
    except BaseException as error:
        failure = error
        try:
            desktop.capture("iec104-batch-ioa-failure", description="IEC104 批量迁址及业务类型清理前现场")
        except Exception as capture_error:
            failure.add_note(f"截图失败：{capture_error}")
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
