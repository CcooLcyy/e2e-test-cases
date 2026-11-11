"""验证量测过期时的成员退出、全组停发及恢复。"""

import pytest

from testbench.case_plan import case_plan

from case_support.case_metadata import execution_logic
from case_support.measurement_expiry_run import expiry_plan, run_expiry_case


pytestmark = pytest.mark.requires_modbus


@pytest.mark.case_group("AGC 控制")
@case_plan(*expiry_plan("AGC"))
@execution_logic("""
1. 自行配置两台模拟 RTU 成员、AGC 组和 IEC104 总目标。保存并回读本例 10 秒量测有效期，心跳超时配置为 60 秒。
2. 两台量测均有效时下发 80 kW，核对 A、B 各收到 40 kW。
3. A 使用 30 秒轮询。在 A 量测过期但通信正常、B 仍在刷新时输入 100 kW。核对 A 无新写入、B 收到 100 kW。
4. A 再次采到有效量测后，保持 100 kW 总目标，核对 A、B 各收到 50 kW。失败时先留证，再清理本例资源。
""")
def test_agc_member_measurement_expiry(request):
    """成员有功量测过期退出与恢复"""
    run_expiry_case(request, "AGC")


@pytest.mark.case_group("AVC 控制")
@case_plan(*expiry_plan("AVC"))
@execution_logic("""
1. 自行配置两路模拟 RTU、AVC 定压组和 IEC104 电压目标。保存并回读本例 10 秒电压量测有效期。
2. 母线 220 V 时输入 230 V，核对成员收到 20 kvar。
3. 母线电压使用 30 秒轮询。超过有效期后保持 230 V 目标，将快速采集的无功实测改为 10 kvar。从实际写请求确认成员未收到新指令。
4. 母线再次采到同值且有效的 220 V 样本后，核对成员自动收到 30 kvar，无需重发目标。失败时先留证，再清理本例资源。
""")
def test_avc_voltage_measurement_expiry(request):
    """母线电压过期暂停下发与恢复"""
    run_expiry_case(request, "AVC")
