"""通过上位机反馈和模拟设备实际收到的控制值验收 AGC 通信重分配。"""
import pytest

from case_support.blocked_control import SITE_CONNECTION_FLOW, report_blocked_control_case


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
def test_agc_redistributes_when_rtu_member_loses_communication(request):
    """一次下发 300 kW，单台 RTU 通信异常后重新分配，恢复后自动恢复三台均分。"""
    report_blocked_control_case(
        request,
        required="经专用业务链路提交 300 kW，总目标不变时独立切换和恢复第三台 RTU 应答",
    )
