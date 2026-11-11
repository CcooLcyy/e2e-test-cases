"""从主站可见的报文监视界面提取 Modbus RTU 品质验收证据。"""
import math
import struct
import sys

from selenium.webdriver.common.by import By


def capture_frames(master):
    """冻结本次可见报文列表并按显示顺序读取原始字节。"""
    master._check_stop()
    driver = master.driver
    paused = bool(driver.find_elements(By.XPATH, "//button[normalize-space(.)='恢复显示']"))
    if not paused:
        master._button("暂停显示").click()
    try:
        master.wait_for(
            lambda: master._button("恢复显示").get_dom_attribute("aria-pressed") == "true",
            "主站报文列表未暂停",
        )
        raw_mode = master._button("原始报文")
        if raw_mode.get_dom_attribute("aria-pressed") != "true":
            raw_mode.click()
        table = driver.find_element(By.CSS_SELECTOR, 'table[aria-label="收发报文"]')
        rows = table.find_elements(By.CSS_SELECTOR, "tbody tr[data-selected]")
        frames = []
        for index, row in enumerate(rows, 1):
            master._check_stop()
            direction = row.find_elements(By.TAG_NAME, "td")[1].text.strip()
            select = row.find_element(By.CSS_SELECTOR, 'button[aria-controls="frame-detail"]')
            select.click()
            master.wait_for(
                lambda: select.get_dom_attribute("aria-pressed") == "true",
                f"主站未选中第 {index} 条可见报文",
            )
            raw = driver.find_element(By.ID, "frame-detail").text.strip()
            try:
                raw = bytes.fromhex(raw).hex(" ").upper()
            except ValueError:
                # 保留界面原文，便于调用者先落盘，再报告解码失败。
                pass
            frames.append({"direction": direction, "raw": raw})
        return frames
    finally:
        if not paused:
            original = sys.exc_info()[1]
            try:
                master._button("恢复显示").click()
            except Exception as error:
                if original is None:
                    raise
                original.add_note(f"恢复主站实时显示失败：{error}")


def decode_frame(frame):
    """解码本例涉及的 104 报文，保留原始 QDS 和 CP56Time2a。"""
    direction = frame["direction"]
    if direction not in ("发送", "接收", "发送失败"):
        raise ValueError(f"104 报文方向无法识别：{direction!r}")
    try:
        raw = bytes.fromhex(frame["raw"])
    except ValueError as error:
        raise ValueError(f"104 原始报文不是十六进制：{frame['raw']!r}") from error
    if len(raw) < 6 or raw[0] != 0x68 or raw[1] > 253 or raw[1] != len(raw) - 2:
        raise ValueError(f"104 报文起始字节或长度不符：{frame['raw']!r}")
    if raw[2] & 1:
        if len(raw) != 6:
            raise ValueError(f"104 非 I 帧长度不符：{frame['raw']!r}")
        return None
    if len(raw) < 12:
        raise ValueError(f"104 I 帧缺少完整 ASDU 头：{frame['raw']!r}")

    type_id, vsq, cause = raw[6:9]
    count = vsq & 0x7F
    sequence = bool(vsq & 0x80)
    objects = []
    if type_id == 100:
        if vsq != 1 or len(raw) != 16:
            raise ValueError(f"104 总召报文对象格式不符：{frame['raw']!r}")
        objects.append({"ioa": int.from_bytes(raw[12:15], "little"), "qoi": raw[15]})
    elif type_id in (13, 36):
        object_size = 12 if type_id == 36 else 5
        expected = 15 + count * object_size if sequence else 12 + count * (3 + object_size)
        if count == 0 or len(raw) != expected:
            raise ValueError(f"104 短浮点遥测对象数量或长度不符：{frame['raw']!r}")
        offset = 12
        if sequence:
            first_ioa = int.from_bytes(raw[offset:offset + 3], "little")
            if first_ioa + count - 1 > 0xFFFFFF:
                raise ValueError(f"104 短浮点遥测连续地址超出范围：{frame['raw']!r}")
            offset += 3
        for index in range(count):
            if sequence:
                ioa = first_ioa + index
            else:
                ioa = int.from_bytes(raw[offset:offset + 3], "little")
                offset += 3
            value, quality = struct.unpack_from("<fB", raw, offset)
            timestamp = raw[offset + 5:offset + 12].hex(" ").upper() if type_id == 36 else None
            objects.append({
                "ioa": ioa,
                "value": value if math.isfinite(value) else str(value),
                "quality": quality,
                "timestamp": timestamp,
            })
            offset += object_size

    return {
        "direction": direction,
        "type_id": type_id,
        "cot": cause & 0x3F,
        "negative": bool(cause & 0x40),
        "test": bool(cause & 0x80),
        "ca": int.from_bytes(raw[10:12], "little"),
        "objects": objects,
    }
