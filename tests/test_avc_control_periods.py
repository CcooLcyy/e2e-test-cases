"""保留 AVC 周期验收目标，明确报告当前无法从上位机完成的前置条件。"""

import pytest

from case_support.blocked_control import SITE_CONNECTION_FLOW, report_blocked_control_case


pytestmark = pytest.mark.case_group("AVC 控制")


@pytest.mark.case_flow(
    SITE_CONNECTION_FLOW,
    ("在 slaveSim 准备本例初态，在 DSP 上位机核对量测并打开专用 AVC 组",
     "实测为 220 V、初始总无功为 0 kVar，可观察逐轮电压偏差和成员命令及其时间"),
    ("将该组按定压周期模式运行", "计算周期 1 秒，命令最小间隔 4 秒，比例系数 2"),
    ("经本例目标输入链路提交 230 V 并观察首轮成员设定", "首轮下发 20 kVar"),
    ("保持输入不变，连续观察三轮计算", "每轮电压偏差为 10 V，相邻计算至少间隔 1 秒"),
    blocked="当前上位机定压模式仅支持 PI 事件触发，本例还缺可确认的目标电压输入链路。",
)
def test_avc_voltage_deviation_recalculates_without_new_input(request):
    """AVC 定压持续偏差且没有新输入时按配置周期重新计算。"""
    report_blocked_control_case(
        request,
        required="上位机支持定压周期模式，本例可提交目标电压并观察逐轮偏差和成员命令",
    )


@pytest.mark.case_flow(
    SITE_CONNECTION_FLOW,
    ("在 DSP 上位机打开专用 AVC 组",
     "该组按总无功模式运行，计算周期 1 秒、命令最小间隔 4 秒，可以观察成员命令及其时间"),
    ("经本例目标输入链路提交 10 kVar，记录首条成员命令的时间", "首条成员设定为 10 kVar"),
    ("首条下发后 4 秒内连续提交 20 至 49 kVar 的目标", "间隔窗口内不额外发布成员命令"),
    ("观察下一条成员设定", "取最新目标 49 kVar，与首条至少间隔 4 秒"),
    blocked="AVC 页面只配置总无功来源，尚无本例可确认的实际目标输入链路及连续成员命令时间记录。",
)
def test_avc_burst_respects_command_interval_and_uses_latest_target(request):
    """总无功高频输入只在限频窗口后发布最新目标。"""
    report_blocked_control_case(
        request,
        required="本例可提交总无功目标，并能记录目标到达和成员命令时刻",
    )


@pytest.mark.case_flow(
    SITE_CONNECTION_FLOW,
    ("在 DSP 上位机打开专用 AVC 组",
     "该组按总无功模式运行，计算周期 1 秒、命令间隔 30 秒，可以观察逐轮计算和成员命令及其时间"),
    ("经本例目标输入链路提交 10 kVar 并观察首条成员命令", "首条成员设定为 10 kVar"),
    ("在上位机停组，经本例目标输入链路提交 20 kVar", "上位机确认停组，业务输入端确认目标提交"),
    ("保存计算周期 3 秒、命令间隔 7 秒后重新启动控制组", "启动后 3 秒内发布 20 kVar"),
    ("随后提交 30 kVar，观察相邻两轮计算", "计算从 20 更新到 30 kVar，间隔至少 3 秒且小于 4.5 秒"),
    ("观察成员命令更新", "发布 30 kVar，与重新启动后的首条命令至少间隔 7 秒且小于 15 秒"),
    blocked="尚无本例可确认的运行及停组目标输入链路，也缺少逐轮计算与成员命令的独立时间记录。",
)
def test_avc_stopped_group_applies_new_periods_after_restart(request):
    """停止、保存并再启动 AVC 控制组后应用新的计算和命令周期。"""
    report_blocked_control_case(
        request,
        required="本例在运行和停组时均可提交总无功目标，并能观察逐轮计算结果和成员命令",
    )
