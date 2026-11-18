"""验证 IEC104 目标 IOA 地址段不够时拒绝整批迁移。"""

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

TARGET_START = 0x6001
TARGET_END = 0x6100
OCCUPIED = tuple(f"e2e-occupied-{ioa:04x}" for ioa in range(TARGET_START, TARGET_END))
MOVING = {"e2e-overflow-a": 0x4001, "e2e-overflow-b": 0x4002}
ERROR = "目标 IOA 地址段没有足够的空闲地址，无法迁移 2 个点位"


def _point_total(context):
    expected = f"显示 {len(OCCUPIED) + len(MOVING)}/{len(OCCUPIED) + len(MOVING)} 个点位"
    page = context.iec104
    locator = (By.XPATH, page.POINT_CARD + f"//span[normalize-space(.)='{expected}']")
    page.upper._until(lambda _: page.upper._visible(locator), f"点表数量不符：{expected}")


def _prepare(context):
    page = context.iec104
    port = page.create()
    context.desktop.metadata["本例 IEC104 配置"] = {
        "链路": page.name, "端口": port, "目标范围": "0x6001–0x6100", "唯一空闲地址": TARGET_END,
        "待迁移": MOVING,
    }
    page.add_address_block(OCCUPIED, "遥控", TARGET_START)
    for tag, ioa in MOVING.items():
        page.add_point(tag, ioa)
    _point_total(context)
    _assert_block_endpoints(context)
    context.before = {tag: page.point_cells(tag)[1:-1] for tag in MOVING}


def _assert_block_endpoints(context):
    page = context.iec104
    for tag, expected in ((OCCUPIED[0], TARGET_START), (OCCUPIED[-1], TARGET_END - 1)):
        actual = int(page.point_cells(tag)[2].split()[0])
        context.desktop.check(f"核对占位点 {tag} 的地址", actual, expected)


def _reject_migration(context):
    page = context.iec104
    page.open_batch(tuple(MOVING))
    page.choose_batch("IOA 业务类别", "遥控")
    locator = (By.XPATH, page.BATCH_DIALOG + f"//li[normalize-space(.)='{ERROR}']")
    error = page.upper._until(lambda _: page.upper._visible(locator), "未提示目标 IOA 地址不足")
    context.desktop.check("核对地址容量错误", error.text.strip(), ERROR)
    button = page.upper._until(lambda _: page.upper._visible(page.batch_button("保存批量修改")), "未显示批量保存按钮")
    context.desktop.check("容量不足时批量保存不可用", button.is_enabled(), False)
    page.check_counts(len(MOVING), 0)
    page.cancel_batch()


def _assert_unchanged(context):
    page = context.iec104
    page.reopen_points()
    _point_total(context)
    _assert_block_endpoints(context)
    for tag, expected in context.before.items():
        context.desktop.check(f"重新加载并核对 {tag} 未发生部分迁移", page.point_cells(tag)[1:-1], expected)


LOGIN = (
    Step("iec104-batch-capacity.account", "读取测试账号", "专用账号配置有效",
         lambda c: setattr(c, "account", load_login_account(local_config("login-account.json")))),
    *login_steps("iec104-batch-capacity.login"),
)
RUN = (
    Step("iec104-batch-capacity.prepare", "建立专用连接并占用遥控地址段的前 255 个地址",
         "点表共有 257 个点位，遥控只剩 0x6100，另有两个待迁移遥测点", _prepare),
    Step("iec104-batch-capacity.reject", "将两个遥测点批量迁移至遥控地址段",
         "提示地址容量不足，预计修改为 0，保存按钮不可用", _reject_migration),
    Step("iec104-batch-capacity.reload", "放弃草稿后重新加载点表",
         "两点配置均未改变，点表总数和占位边界地址不变", _assert_unchanged),
)
CLEANUP = Step("iec104-batch-capacity.cleanup", "清理本例 IEC104 链路", "专用连接已移除")


@case_plan(DESKTOP_START, *LOGIN, SITE_CONNECT, *RUN, CLEANUP, DESKTOP_FINISH)
@execution_logic("""
1. 登录并连接现场下位机，通过 IEC104 界面建立专用被控站。批量添加 255 个遥控点，
   使目标地址范围 0x6001–0x6100 仅余 0x6100；另建两个遥测点，记录初始点表。
2. 将两个遥测点勾选并选择迁移至遥控地址段，断言出现容量不足提示、预计修改为 0 且保存按钮不可用。
3. 放弃草稿，重新加载点表，确认两个待迁移点均未发生部分修改，点位总数和占位边界不变。
4. 保留失败现场，仅清理本例专用链路。
""")
def test_iec104_batch_ioa_rejects_insufficient_capacity(request):
    """IEC104 批量迁移目标 IOA 不足时禁止部分保存"""
    desktop = request.getfixturevalue("fresh_desktop")
    upper = ModbusRtuPage(desktop)
    context = SimpleNamespace(desktop=desktop, page=PlatformLoginPage(desktop),
                              iec104=BatchEditIec104(upper, f"e2e-batch-cap-{uuid4().hex[:8]}"))
    run_steps(request, LOGIN, context)
    with track_fixture_step(request, SITE_CONNECT):
        upper.connect_site()
    failure = None
    try:
        run_steps(request, RUN, context)
    except BaseException as error:
        failure = error
        try:
            desktop.capture("iec104-batch-capacity-failure", description="IEC104 迁移容量不足用例清理前现场")
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
