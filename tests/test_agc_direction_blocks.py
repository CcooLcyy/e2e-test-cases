"""通过专用 IEC104 业务链路验收 AGC 增减闭锁。"""

import pytest

from case_support.blocked_control import SITE_CONNECTION_FLOW, report_blocked_control_case
from case_support.case_metadata import execution_logic


pytestmark = pytest.mark.case_group("AGC 控制")

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
@execution_logic("""
    1. 当前运行只报告前置阻塞。测试入口尚无法定位专用 AGC 组及 IEC104 路由，也缺少主站可连接的地址和点位映射。下列业务步骤待验证。
    2. 登录 DSP 上位机并连接现场。计划确认专用单成员 AGC 组运行且投入，允许远方绝对值有功控制，上下限为 100 和 10 kW，并核对 IEC104 公共地址、总目标遥调和闭锁遥信点位。
    3. 连接独立模拟 104 主站并总召，读取有效初值。遥调 50 kW，确认增、减闭锁均为否且品质有效。
    4. 记录接收基线，遥调 100 kW。要求短浮点直接执行收到肯定确认和终止，同一会话新收到品质有效的增闭锁置位遥信，减闭锁保持为否且品质有效。
""")
def test_agc_upper_limit_sets_increase_block(request):
    """上限设定触发增闭锁"""
    report_blocked_control_case(request, required=_REQUIRED)


@_agc_flow(100, "增闭锁已置位且品质有效", 50, "新收到有效增闭锁解除，减闭锁保持为否且品质有效")
@execution_logic("""
    1. 当前运行只报告前置阻塞。测试入口尚无法定位专用 AGC 组及 IEC104 路由，也缺少主站可连接的地址和点位映射。下列业务步骤待验证。
    2. 登录 DSP 上位机并连接现场。计划确认专用单成员 AGC 组运行且投入，允许远方绝对值有功控制，上下限为 100 和 10 kW，并核对 IEC104 公共地址、总目标遥调和闭锁遥信点位。
    3. 连接独立模拟 104 主站并总召，读取有效初值。遥调至 100 kW 上限，确认增闭锁已置位且品质有效。
    4. 记录接收基线，遥调至区间内的 50 kW。要求短浮点直接执行收到肯定确认和终止，同一会话收到有效的增闭锁解除变位，减闭锁保持为否且品质有效。
""")
def test_agc_lower_target_clears_increase_block(request):
    """从上限下调解除增闭锁"""
    report_blocked_control_case(request, required=_REQUIRED)


@_agc_flow(50, "增、减闭锁均为否且品质有效", 10, "新收到有效减闭锁置位，增闭锁保持为否且品质有效")
@execution_logic("""
    1. 当前运行只报告前置阻塞。测试入口尚无法定位专用 AGC 组及 IEC104 路由，也缺少主站可连接的地址和点位映射。下列业务步骤待验证。
    2. 登录 DSP 上位机并连接现场。计划确认专用单成员 AGC 组运行且投入，允许远方绝对值有功控制，上下限为 100 和 10 kW，并核对 IEC104 公共地址、总目标遥调和闭锁遥信点位。
    3. 连接独立模拟 104 主站并总召，读取有效初值。遥调 50 kW，确认增、减闭锁均为否且品质有效。
    4. 记录接收基线，遥调 10 kW。要求短浮点直接执行收到肯定确认和终止，同一会话新收到品质有效的减闭锁置位遥信，增闭锁保持为否且品质有效。
""")
def test_agc_lower_limit_sets_decrease_block(request):
    """下限设定触发减闭锁"""
    report_blocked_control_case(request, required=_REQUIRED)


@_agc_flow(10, "减闭锁已置位且品质有效", 50, "新收到有效减闭锁解除，增闭锁保持为否且品质有效")
@execution_logic("""
    1. 当前运行只报告前置阻塞。测试入口尚无法定位专用 AGC 组及 IEC104 路由，也缺少主站可连接的地址和点位映射。下列业务步骤待验证。
    2. 登录 DSP 上位机并连接现场。计划确认专用单成员 AGC 组运行且投入，允许远方绝对值有功控制，上下限为 100 和 10 kW，并核对 IEC104 公共地址、总目标遥调和闭锁遥信点位。
    3. 连接独立模拟 104 主站并总召，读取有效初值。遥调至 10 kW 下限，确认减闭锁已置位且品质有效。
    4. 记录接收基线，遥调至区间内的 50 kW。要求短浮点直接执行收到肯定确认和终止，同一会话收到有效的减闭锁解除变位，增闭锁保持为否且品质有效。
""")
def test_agc_higher_target_clears_decrease_block(request):
    """从下限上调解除减闭锁"""
    report_blocked_control_case(request, required=_REQUIRED)


@_agc_flow(
    50, "有效实测功率严格位于 10 和 100 kW 之间", 100,
    "同一轮观察中，新收到有效增闭锁置位和低于 100 kW 的新有效实测，减闭锁为否且品质有效",
    measured=True,
)
@execution_logic("""
    1. 当前运行只报告前置阻塞。测试入口尚无法定位专用 AGC 组及 IEC104 路由，也缺少主站可连接的地址和点位映射。下列业务步骤待验证。
    2. 登录 DSP 上位机并连接现场。计划确认专用单成员 AGC 组运行且投入，允许远方绝对值有功控制，上下限为 100 和 10 kW，并核对 IEC104 公共地址、总目标遥调、闭锁遥信和实测功率遥测点位。
    3. 连接独立模拟 104 主站并总召，读取有效遥信和遥测初值。遥调 50 kW，确认有效实测功率大于 10 kW 且小于 100 kW。
    4. 记录接收基线，遥调 100 kW，要求短浮点直接执行收到肯定确认和终止。同一会话的同一轮观察中，应新收到品质有效的增闭锁置位遥信和低于 100 kW 的有效实测，减闭锁为否且品质有效。
""")
def test_agc_increase_block_does_not_wait_for_measured_limit(request):
    """设定达上限且实测未达时增闭锁"""
    report_blocked_control_case(request, required=_REQUIRED)
