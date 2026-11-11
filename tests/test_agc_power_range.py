"""验收 AGC 全站有功范围和 IEC104 遥测。"""

import pytest

from testbench.case_plan import case_plan

from case_support.case_metadata import execution_logic
from case_support.power_range_run import power_phase, power_plan, run_power_case


pytestmark = [pytest.mark.case_group("AGC 控制"), pytest.mark.requires_modbus]

REMOTE_TAGS = ("当前可调有功下限", "当前可调有功上限")


def _phase(identifier, label, measurement, upper, **options):
    remote = dict(zip(REMOTE_TAGS, (10, upper), strict=True))
    station = {"总有功实测": measurement, "理论可调有功下限": 10, "理论可调有功上限": upper, **remote}
    return power_phase(identifier, label, measurement, station, remote, **options)


PHASES = (
    _phase("power.agc.baseline", "核对 40 kW 实测时的有功可调范围", 40, 100),
    _phase("power.agc.at-upper", "将实测有功调到上限", 100, 100),
    _phase("power.agc.at-lower", "将实测有功调到下限", 10, 100),
    _phase("power.agc.restore", "恢复到 40 kW 实测", 40, 100),
    _phase("power.agc.changed-limit", "保持实测 40 kW，将成员配置上限改为 70 kW 后重新运行",
           40, 70, configured_upper=70),
)


@case_plan(*power_plan(PHASES))
@execution_logic("""
    1. 自行建立模拟 RTU 有功实测和设定点，建立本例 AGC 组，配置 10～100 kW 可调范围。
    2. 将两个当前可调限值点导入本例 IEC104 遥测。读回 IOA 和路由，启动模拟主站。
    3. 依次验证实测 40、100、10、40 kW 时，理论与当前可调范围均为 10～100 kW。
    4. 保持实测 40 kW，将成员配置上限改为 70 kW 后重新运行。核对理论与当前可调范围更新为 10～70 kW。
    5. 每轮核对 DSP 上位机全站状态及模拟主站本轮收到的值和品质。保留报文并清理本例资源。
""")
def test_agc_station_power_range_reaches_iec104(request):
    """全站有功可调范围与 IEC104 遥测"""
    run_power_case(request, "AGC", (10, 100), 20, 0, REMOTE_TAGS, PHASES)
