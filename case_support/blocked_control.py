"""从待验证流程生成控制用例的前置失败报告。"""

from testbench.case_plan import SITE_CONNECT
from testbench.pytest_plugin import fail_e2e_prerequisite


SITE_CONNECTION_FLOW = (f"登录 DSP 上位机并{SITE_CONNECT.action}", SITE_CONNECT.expected)


def report_blocked_control_case(request, *, required):
    flow = request.node.get_closest_marker("case_flow")
    fail_e2e_prerequisite(
        request,
        required=required,
        missing=f"当前流程预先标记为阻塞，未启动现场验证。{flow.kwargs['blocked']}",
        checks=flow.args,
    )
