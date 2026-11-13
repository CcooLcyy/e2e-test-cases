"""使用专用 IEC104 链路输入 AVC 目标并读取实际遥测报文。"""

from datetime import datetime, timedelta
from decimal import Decimal
import struct

from testbench.master104_commands import FRAME_CAUSES, _read_frames

from .rtu_quality_frames import decode_frame
from .rtu_quality_iec104 import RtuQualityIec104


class AvcPeriodIec104(RtuQualityIec104):
    def configure(self, group, target, calculation):
        self.create()
        target_ioa = (
            self.import_point(group, target, f"{self.name}-104-target", module="AVC", business_type="遥调")
            if target is not None else None
        )
        calculation_ioa = (
            self.import_point(group, calculation, f"{self.name}-calculation", module="AVC")
            if calculation is not None else None
        )
        assert target_ioa != calculation_ioa, "AVC 目标输入和计算结果的 IOA 必须不同"
        self.check_routes()
        self.start()
        return target_ioa, calculation_ioa


def snapshot(master):
    """读取独立模拟主站的协议证据，包括真实发送和接收的 IEC104 报文。"""
    master._check_stop()
    result = master.driver.execute_async_script("""
        const done = arguments[0];
        window.__TAURI_INTERNALS__.invoke("protocol_snapshot")
            .then(snapshot => done({snapshot}), error => done({error: String(error)}));
    """)
    if "error" in result:
        raise RuntimeError(f"模拟 104 主站读取报文记录失败：{result['error']}")
    return result["snapshot"]


def send_burst(master, ca, ioa, targets, deadline_ms):
    """通过模拟主站原有短浮点遥调命令，按上一条协议终止连续提交。"""
    master._check_stop()
    # 在模拟主站进程内等待协议结果，避免用界面刷新间隔给发送额外节流。
    result = master.driver.execute_async_script("""
        const [address, values, deadline, done] = arguments;
        const invoke = window.__TAURI_INTERNALS__.invoke;
        const evidence = [];
        async function send() {
            for (const value of values) {
                const attempted = Date.now();
                if (attempted >= deadline) throw new Error("发送已到达 4 秒窗口上限");
                const previous = (await invoke("protocol_snapshot")).setpoint?.operationId;
                if (Date.now() >= deadline) throw new Error("发送已到达 4 秒窗口上限");
                await invoke("protocol_setpoint", {address, value});
                const entry = {value, attempted};
                evidence.push(entry);
                do {
                    const operation = (await invoke("protocol_snapshot")).setpoint;
                    if (operation && operation.operationId !== previous) {
                        entry.operation = operation;
                        if (operation.status === "terminated") break;
                        if (!["queued", "sent", "confirmed"].includes(operation.status)) {
                            throw new Error(operation.message);
                        }
                    }
                    await new Promise(resolve => setTimeout(resolve, 5));
                } while (Date.now() < deadline);
                if (entry.operation?.status !== "terminated") throw new Error("窗口内未取得遥调终止");
            }
        }
        send().then(() => done({evidence}), error => done({evidence, error: String(error)}));
    """, ioa, list(targets), deadline_ms)
    master.metadata["AVC连续遥调原始证据"] = result
    assert len(result["evidence"]) == len(targets) and "error" not in result, (
        f"高频输入条件未建立，已提交 {len(result['evidence'])}/{len(targets)} 条：{result.get('error')}"
    )
    for target, entry in zip(targets, result["evidence"], strict=True):
        operation = entry["operation"]
        assert (operation["commonAddress"], operation["address"], operation["value"]) == (ca, ioa, target), operation
        fields = {}
        for kind, _ in FRAME_CAUSES:
            for name, suffix in (("at", "At"), ("frame-id", "FrameId"), ("raw", "Raw")):
                fields[f"{kind}-{name}"] = operation[kind + suffix]
        payload = ca.to_bytes(2, "little") + ioa.to_bytes(3, "little") + struct.pack("<fB", target, 0)
        frames = _read_frames(fields, payload, entry["attempted"])
        assert frames["terminated"]["at_ms"] < deadline_ms, f"目标 {target} 未在输入窗口内取得肯定终止"
    return frames["terminated"]["at_ms"]


def _sample_time(timestamp):
    raw = bytes.fromhex(timestamp)
    milliseconds = int.from_bytes(raw[:2], "little")
    assert len(raw) == 7 and not raw[2] & 0x80 and milliseconds < 60000, f"无效的计算时标：{timestamp}"
    minute, hour, day, month, year = (raw[2] & 63, raw[3] & 31, raw[4] & 31, raw[5] & 15, raw[6] & 127)
    return datetime(2000 + year, month, day, hour, minute) + timedelta(milliseconds=milliseconds)


def wait_calculations(master, ca, ioa, after_frame, count, timeout, *, first_value=None):
    def collect():
        samples = []
        for frame in snapshot(master)["frames"]:
            if frame["id"] <= after_frame or frame["direction"] != "receive":
                continue
            decoded = decode_frame({"direction": "接收", "raw": frame["raw"]})
            if decoded is None or (decoded["ca"], decoded["type_id"], decoded["cot"]) != (ca, 36, 3):
                continue
            assert not decoded["negative"] and not decoded["test"], decoded
            for point in decoded["objects"]:
                if point["ioa"] == ioa:
                    assert point["quality"] == 0, f"AVC 计算结果品质异常：{point!r}"
                    samples.append({
                        "value": str(Decimal(str(point["value"]))), "at": _sample_time(point["timestamp"]).isoformat(),
                        "frame_id": frame["id"], "received_ms": int(frame["time"]), "raw": frame["raw"],
                    })
        master.metadata["AVC计算遥测"] = samples
        start = 0
        if first_value is not None:
            start = next((index for index, sample in enumerate(samples) if Decimal(sample["value"]) == first_value),
                         len(samples))
        observed = samples[start:start + count]
        return observed if len(observed) == count else False

    return master.wait_for(
        collect, f"计算周期未验证，AVC 结果点缺少 {count} 条独立带时标自发遥测", timeout,
    )
