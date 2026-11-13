"""通过 RTU 写报文和 IEC104 遥测检查控制侧量测可用性。"""

import pytest

from testbench.case_plan import case_plan

from case_support.case_metadata import execution_logic
from case_support.control_measurement_quality import measurement_plan, run_measurement_case


pytestmark = pytest.mark.requires_modbus


@pytest.mark.case_group("AGC 控制")
@case_plan(*measurement_plan("AGC", missing=True))
@execution_logic("""
1. 为两台 AGC 成员建立独立 RTU 连接和设点，A 的同站参考寄存器可读，有功量测地址暂未配置。启用成员量测有效期并回读。
2. 确认 A 通信正常、关键量测收到 RTU 异常应答，且 IEC104 主站仅观察到正常参考值。下发 80 kW，B 收到 80 kW，A 无写请求。
3. 恢复 A 的有功量测地址，核对正常采样和 IEC104 遥测。保留 80 kW 总目标，两台各收到 40 kW 并应答。
""")
def test_agc_excludes_member_until_first_measurement(request):
    """AGC 成员通信正常但量测无首值"""
    run_measurement_case(request, "AGC", missing=True)


@pytest.mark.case_group("AGC 控制")
@case_plan(*measurement_plan("AGC", missing=False))
@execution_logic("""
1. 建立两个 RTU 成员并配置 60 秒量测有效期。下发 80 kW，确认两台各收到 40 kW，A 的实测品质有效。
2. 只让 A 的量测地址返回异常码 0x02，同站参考值仍正常。IEC104 应保留原值并上送 0xC0。
3. 在有效期到达前下发 100 kW，只有 B 收到 100 kW。恢复 A 的同值有效采样后，两台自动各收到 50 kW。
""")
def test_agc_excludes_member_with_bad_measurement(request):
    """AGC 成员坏品质退出及恢复"""
    run_measurement_case(request, "AGC", missing=False)


@pytest.mark.case_group("AGC 控制")
@case_plan(*measurement_plan("AGC", missing=False, partial_recovery=True))
@execution_logic("""
1. 两台 AGC 成员各收到 40 kW。仅停止 A 的 RTU 轮询，确认其退出，B 接收 80 kW。
2. 恢复 A 的轮询，同站参考读正常，关键有功量测仍返回异常码 0x02。遥调 100 kW，只向 B 写入。
3. 恢复 A 的有功量测。保持总目标，核对两台各收到 50 kW 并应答。
""")
def test_agc_waits_for_measurement_after_rtu_polling_restarts(request):
    """AGC 通信先恢复时等待关键有功量测"""
    run_measurement_case(request, "AGC", missing=False, partial_recovery=True)


@pytest.mark.case_group("AVC 控制")
@case_plan(*measurement_plan("AVC", missing=True))
@execution_logic("""
1. 配置 AVC 定压组及电压和逆变器的两条 RTU 连接。电压同站参考寄存器可读，关键电压地址暂未配置。
2. 确认通信正常、IEC104 总召及遥测点表无电压首值。提交 230 V 遥调，收到与本次命令匹配的负确认，逆变器无写请求。
3. 恢复电压寄存器并核对 220 V 正常上送。重新提交 230 V，逆变器收到 20 kvar 写入并应答。
""")
def test_avc_rejects_voltage_target_until_first_measurement(request):
    """AVC 电压无首值时拒绝定压遥调"""
    run_measurement_case(request, "AVC", missing=True)


@pytest.mark.case_group("AVC 控制")
@case_plan(*measurement_plan("AVC", missing=False))
@execution_logic("""
1. 配置 AVC 定压组，电压初值 220 V、无功实测为 0。下发 230 V，逆变器收到 20 kvar。
2. 只让关键电压地址返回异常码 0x02，同站参考值仍正常。核对 IEC104 电压值保持 220 V、品质为 0xC0。
3. 保持目标 230 V，将无功实测改为 10 kvar。故障期间逆变器无写请求；同值 220 V 恢复后，逆变器自动收到 30 kvar。
""")
def test_avc_suspends_voltage_control_with_bad_measurement(request):
    """AVC 电压坏品质时暂停下发并恢复"""
    run_measurement_case(request, "AVC", missing=False)
