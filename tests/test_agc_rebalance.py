"""通过上位机反馈和模拟设备实际收到的控制值验收 AGC 通信重分配。"""
import pytest

from case_support.blocked_control import SITE_CONNECTION_FLOW, report_blocked_control_case
from case_support.case_metadata import execution_logic


pytestmark = pytest.mark.case_group("AGC 控制")


@pytest.mark.case_flow(
    SITE_CONNECTION_FLOW,
    ("在 slaveSim 准备三台专用逆变器并读取配置，在 DSP 上位机配置并核对本例 AGC 组",
     "三台额定 200 kW 的 RTU 成员均在线，组按通信状态筛选成员"),
    ("经本例已确认的业务链路提交一次 300 kW 有功总目标，并观察 slaveSim 收到的设定",
     "三台各分配 100 kW"),
    ("保持原目标，在模拟端仅切换第三台 RTU 为失联并观察通信状态和实际设定",
     "另外两台各分配 150 kW，第三台保留原设定"),
    ("保持原目标，在模拟端恢复第三台 RTU 并观察通信状态和实际设定",
     "无需再次下发总目标，三台重新各分配 100 kW"),
    blocked="尚无本例可确认的专用总目标提交链路。slaveSim 保存并应用从站配置会重置运行状态，"
            "尚无只切换第三台应答且可确认其保留原设定的执行步骤。",
)
@execution_logic("""
    1. 当前运行只报告前置阻塞。尚缺本例专用的总目标提交链路，以及仅切换第三台 RTU 应答并确认其保留原设定的步骤。slaveSim 保存并应用从站配置会重置运行状态。下列业务步骤待验证。
    2. 登录 DSP 上位机并连接现场。计划在 slaveSim 准备三台额定 200 kW 的专用 RTU 逆变器，读取配置后在上位机建立 AGC 组，确认三台均在线且组按通信状态筛选成员。
    3. 经专用业务链路提交一次 300 kW 有功总目标，从 slaveSim 核对三台各收到 100 kW 设定。
    4. 保持原目标，仅使第三台 RTU 失联。观察通信状态和 slaveSim 实际设定，要求仍在线的两台各收到 150 kW，第三台保留原来的 100 kW。
    5. 恢复第三台 RTU 应答，继续保持同一个总目标。确认第三台恢复通信后，三台自动回到各 100 kW。
""")
def test_agc_redistributes_when_rtu_member_loses_communication(request):
    """RTU 失联与恢复时重分配"""
    report_blocked_control_case(
        request,
        required="经专用业务链路提交 300 kW，总目标不变时独立切换和恢复第三台 RTU 应答",
    )
