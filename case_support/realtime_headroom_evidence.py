"""从 slaveSim 界面和原始报文验证设备失联期间的裕量分配。"""

from time import monotonic
from selenium.webdriver.common.by import By

from .external_direction_rtu import check_external_allocation


COMMAND_INTERVAL_SECONDS = 4
_DROPPED_REQUEST = "从站已暂停应答，已丢弃请求"


def pause_member_a(context):
    """在模拟器运行界面只暂停 A 的应答，返回点击前的报文序号。"""
    simulator = context.simulator
    point = context.points["A"][0]
    key = (point["bus"], point["station_id"], point["function"], point["address"])

    def buttons(label):
        _, device, _ = simulator._row(key)
        return [button for button in device.find_elements(By.TAG_NAME, "button")
                if button.is_displayed() and button.text == label]

    with simulator._step("暂停设备 A 的 RTU 应答",
                         expected={"总线": key[0], "站号": key[1], "可用操作": "恢复应答"}) as evidence:
        candidates = buttons("暂停应答")
        if len(candidates) != 1 or not candidates[0].is_enabled():
            evidence["实际"] = {"总线": key[0], "站号": key[1], "可见匹配按钮数": len(candidates)}
            raise RuntimeError("slaveSim 设备 A 未提供可用的单站「暂停应答」操作")
        marker = len(simulator.events()) - 1
        tick = simulator._tick()
        candidates[0].click()
        simulator._wait(
            lambda _: simulator._tick() > max(tick, 0)
            and len(available := buttons("恢复应答")) == 1 and available[0].is_enabled(),
            "界面未确认设备 A 已暂停应答",
            timeout=context.timeout,
        )
        evidence["实际"] = {"总线": key[0], "站号": key[1], "可用操作": "恢复应答", "操作前报文序号": marker}
        return marker


def _member_a_frames(context, after):
    point = context.points["A"][0]
    return [(index, event, bytes.fromhex(event["frame"]))
            for index, event in enumerate(context.simulator.events()) if index > after
            and event["bus"] == point["bus"] and event.get("device_id") == point["station_id"]]


def _is_dropped_measurement_read(context, event, frame):
    point = context.points["A"][0]
    return (event["direction"] == "接收" and event["summary"] == _DROPPED_REQUEST
            and len(frame) == 8 and frame[:2] == bytes((point["station_id"], 3))
            and int.from_bytes(frame[2:4], "big") == point["address"] and frame[4:6] == b"\x00\x01")


def wait_member_a_dropped_poll(context, after):
    """等待 A 的量测轮询实际被丢弃，返回对应报文序号。"""
    observation = {}

    def match(_):
        for index, event, frame in _member_a_frames(context, after):
            if _is_dropped_measurement_read(context, event, frame):
                observation.update(报文序号=index, 丢弃请求=event)
                return True
        return False

    with context.desktop.step("核对设备 A 的 RTU 量测请求被丢弃", expected="从站收到请求并丢弃", actual=observation):
        context.simulator._wait(match, "未观察到设备 A 丢弃 RTU 量测请求", timeout=context.timeout)
    return observation["报文序号"]


def assert_allocated(context, marker, expected):
    """核对 RTU 写入、应答和寄存器，保留未参与成员上次写入值。"""
    previous = context.previous
    observation = {"预期设点": expected, "起始报文序号": marker}
    with context.desktop.step("核对设备的实际裕量分配", expected=expected, actual=observation):
        observation["已应答的写入"] = check_external_allocation(context, marker, expected)
        observation["读回后的报文序号"] = len(context.simulator.events()) - 1
    context.previous = {**previous, **expected}
    return observation["读回后的报文序号"]


def assert_a_offline_and_no_writes(context, marker, *, after):
    """失联确认后查找所有写请求，并观察持续丢弃轮询的证据。"""
    assert after >= marker, "观察起点必须晚于失联标记"
    until = monotonic() + COMMAND_INTERVAL_SECONDS
    window_end_marker = None
    observation = {"失联确认报文序号": marker, "设点读回报文序号": after}

    def remained_offline(_):
        nonlocal window_end_marker
        frames = _member_a_frames(context, marker)
        writes = [event for _, event, raw in frames if event["direction"] == "接收" and raw[1:2] in (b"\x06", b"\x10")]
        assert not writes, f"设备 A 失联后仍收到设点写请求：{writes!r}"
        replies = [event for _, event, _ in frames if event["direction"] == "发送"]
        assert not replies, f"设备 A 暂停应答期间仍有响应：{replies!r}"
        if monotonic() < until:
            return False
        if window_end_marker is None:
            window_end_marker = max(after, frames[-1][0] if frames else marker)
        for index, event, frame in frames:
            if index > window_end_marker and _is_dropped_measurement_read(context, event, frame):
                observation.update(持续观察秒数=COMMAND_INTERVAL_SECONDS, 后续报文序号=index, 丢弃请求=event)
                return True
        return False

    with context.desktop.step("核对 A 持续失联且没有设点写请求", expected="至少 4 秒并继续收到丢弃的轮询",
                              actual=observation):
        context.simulator._wait(remained_offline, "未观察到设备 A 在命令间隔后的丢弃轮询",
                                timeout=max(context.timeout, COMMAND_INTERVAL_SECONDS + 5))
