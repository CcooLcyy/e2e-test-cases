"""通过专用 IEC104 业务链路验收 AGC 增减闭锁。"""

import pytest

from case_support.blocked_control import SITE_CONNECTION_FLOW, report_blocked_control_case


pytestmark = pytest.mark.case_group("AGC 增减闭锁")

_REQUIRED = "从业务界面确认本例专用 AGC 与 IEC104 映射，取得可连接协议端点和点位后由独立主站遥调及观察"
_BLOCKED = (
    "尚无从已提供的测试入口自主定位专用设备、AGC 组及其 IEC104 路由的流程。"
    "上位机显示的通配监听地址无法用于主站连接，旧方案需要另填下位机 104 地址和 IOA。"
)


def _agc_flow(prepare_kw, initial, target_kw, feedback, *, measured=False):
    mapping = "明确可连接的协议端点、公共地址、总目标遥调及闭锁遥信映射"
    if measured:
        mapping += "，以及实测功率遥测映射"
    return pytest.mark.case_flow(
        SITE_CONNECTION_FLOW,
        ("从正常业务界面确认专用 AGC 组及专用 IEC104 从站点表",
         f"单成员组运行、投入、允许远方绝对值有功控制，上下限为 10 和 100 kW。{mapping}"),
        ("连接独立模拟 104 主站并总召", "从专用点表取得所需遥信或遥测的有效初值"),
        (f"通过模拟 104 主站遥调 {prepare_kw} kW，建立本例初态", initial),
        (f"记录接收基线，通过模拟 104 主站遥调 {target_kw} kW", "本轮短浮点直接执行收到肯定确认和终止"),
        ("在同一模拟 104 主站会话中核对本轮新反馈", feedback),
        blocked=_BLOCKED,
    )


@_agc_flow(50, "增、减闭锁均为否且品质有效", 100, "新收到有效增闭锁置位，减闭锁保持为否且品质有效")
def test_agc_upper_limit_sets_increase_block(request):
    """AGC 下发设定到上限时增闭锁置位，104 主站收到有效的新遥信。"""
    report_blocked_control_case(request, required=_REQUIRED)


@_agc_flow(100, "增闭锁已置位且品质有效", 50, "新收到有效增闭锁解除，减闭锁保持为否且品质有效")
def test_agc_lower_target_clears_increase_block(request):
    """AGC 从上限下调至区间内部后增闭锁解除，104 主站收到新变位。"""
    report_blocked_control_case(request, required=_REQUIRED)


@_agc_flow(50, "增、减闭锁均为否且品质有效", 10, "新收到有效减闭锁置位，增闭锁保持为否且品质有效")
def test_agc_lower_limit_sets_decrease_block(request):
    """AGC 下发设定到下限时减闭锁置位，104 主站收到有效的新遥信。"""
    report_blocked_control_case(request, required=_REQUIRED)


@_agc_flow(10, "减闭锁已置位且品质有效", 50, "新收到有效减闭锁解除，增闭锁保持为否且品质有效")
def test_agc_higher_target_clears_decrease_block(request):
    """AGC 从下限上调至区间内部后减闭锁解除，104 主站收到新变位。"""
    report_blocked_control_case(request, required=_REQUIRED)


@_agc_flow(
    50, "有效实测功率严格位于 10 和 100 kW 之间", 100,
    "同一轮观察中，新收到有效增闭锁置位和低于 100 kW 的新有效实测，减闭锁为否且品质有效",
    measured=True,
)
def test_agc_increase_block_does_not_wait_for_measured_limit(request):
    """实测仍低于上限时，AGC 按已下发上限设定置增闭锁并上送 104 遥信。"""
    report_blocked_control_case(request, required=_REQUIRED)
