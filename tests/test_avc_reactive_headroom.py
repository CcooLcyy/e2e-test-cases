"""验收 AVC 全站无功上下裕度和 IEC104 遥测。"""

import pytest

from testbench.case_plan import case_plan

from case_support.case_metadata import execution_logic
from case_support.power_range_run import power_phase, power_plan, run_power_case


pytestmark = [pytest.mark.case_group("AVC 控制"), pytest.mark.requires_modbus]

REMOTE_TAGS = ("无功上调裕度", "无功下调裕度")


def _phase(identifier, label, measurement, upper, up, down, **options):
    remote = dict(zip(REMOTE_TAGS, (up, down), strict=True))
    station = {"总无功实测": measurement, "当前可调无功下限": -40, "当前可调无功上限": upper, **remote}
    return power_phase(identifier, label, measurement, station, remote, **options)


PHASES = (
    _phase("power.avc.baseline", "核对实测 30 kvar 时的无功上下裕度", 30, 80, 50, 70),
    _phase("power.avc.at-upper", "将实测无功调到上限", 80, 80, 0, 120),
    _phase("power.avc.at-lower", "将实测无功调到负下限", -40, 80, 120, 0),
    _phase("power.avc.restore", "恢复到 30 kvar 实测", 30, 80, 50, 70),
    _phase("power.avc.changed-limit", "保持实测 30 kvar，将成员配置上限改为 50 kvar 后重新运行",
           30, 50, 20, 70, configured_upper=50),
)


@case_plan(*power_plan(PHASES))
@execution_logic("""
    1. 自行建立模拟 RTU 无功实测和设定点。实测采用偏移量 -100 的 UINT16，覆盖正负无功。
    2. 建立本例 AVC 组，可调范围为 -40～80 kvar。导入上下裕度遥测点，读回 IOA 和路由并连接模拟主站。
    3. 依次将实测设为 30、80、-40、30 kvar，上调和下调裕度应依次为 (50, 70)、(0, 120)、(120, 0)、(50, 70) kvar。
    4. 保持实测 30 kvar，将成员配置上限改为 50 kvar 后重新运行。上调和下调裕度应为 20 和 70 kvar。
    5. 每轮核对 DSP 上位机全站状态及模拟主站本轮收到的值和品质。保留报文并清理本例资源。
""")
def test_avc_station_reactive_headroom_reaches_iec104(request):
    """全站无功上下裕度与 IEC104 遥测"""
    run_power_case(request, "AVC", (-40, 80), 0, -100, REMOTE_TAGS, PHASES)
