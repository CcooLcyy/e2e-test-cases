"""通过104对外接口验证AGC方向闭锁。"""
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
import ipaddress
import json
import logging
import math
from pathlib import Path
import time


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgcDirectionProfile:
    master_host: str
    master_port: int
    common_address: int
    command_ioa: int
    increase_ioa: int
    decrease_ioa: int
    lower_kw: Decimal
    middle_kw: Decimal
    upper_kw: Decimal
    timeout_seconds: float
    measured_ioa: int | None = None

    @classmethod
    def from_dict(cls, data):
        # 旧版专项配置可继续用于闭锁遥信用例，原白盒字段不参与任何连接或判定。
        legacy_fields = {"group_name", "member_name", "conn_id", "manager_address"}
        optional_fields = legacy_fields | {"measured_ioa"}
        if (not isinstance(data, dict) or set(data) - optional_fields !=
                set(cls.__dataclass_fields__) - {"measured_ioa"}):
            raise ValueError("AGC验收配置字段不完整或含未知字段，请按示例填写")
        values = {name: value for name, value in data.items() if name not in legacy_fields}
        point_names = ["command_ioa", "increase_ioa", "decrease_ioa"]
        if values.get("measured_ioa") is not None:
            point_names.append("measured_ioa")
        for name, maximum in (("master_port", 65535), ("common_address", 65534),
                              *((name, 0xffffff) for name in point_names)):
            if type(values[name]) is not int or not 1 <= values[name] <= maximum:
                raise ValueError(f"AGC验收{name}须为范围内的正整数")
        if len({values[name] for name in point_names}) != len(point_names):
            raise ValueError("AGC遥调、遥信及实测功率的IOA必须不同")
        try:
            if not isinstance(values["master_host"], str):
                raise ValueError()
            values["master_host"] = str(ipaddress.ip_address(values["master_host"]))
        except ValueError:
            raise ValueError("AGC验收须填写专用104下位机IP") from None
        for name in ("lower_kw", "middle_kw", "upper_kw"):
            try:
                if isinstance(values[name], (bool, float)):
                    raise ValueError()
                values[name] = Decimal(values[name])
                if not values[name].is_finite():
                    raise ValueError()
            except (ValueError, TypeError, InvalidOperation):
                raise ValueError(f"AGC验收{name}须为有限十进制文本") from None
        if not values["lower_kw"] < values["middle_kw"] < values["upper_kw"]:
            raise ValueError("AGC固定期望必须满足下限 < 中间目标 < 上限")
        try:
            timeout = values["timeout_seconds"]
            if isinstance(timeout, bool) or not math.isfinite(float(timeout)) or not 5 <= float(timeout) <= 300:
                raise ValueError()
            values["timeout_seconds"] = float(timeout)
        except (ValueError, TypeError, OverflowError):
            raise ValueError("AGC验收等待时间须在5至300秒之间") from None
        return cls(**values)

    @classmethod
    def load(cls, path):
        path = Path(path)
        if not path.is_file():
            raise ValueError("缺少local/agc-direction-block.json，请按docs示例确认专用测试组和104地址")
        try:
            return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, UnicodeError):
            raise ValueError("AGC验收配置不是有效的UTF-8 JSON") from None

    @property
    def ioas(self):
        return {"increase": self.increase_ioa, "decrease": self.decrease_ioa}

    def to_dict(self):
        return {key: str(value) if isinstance(value, Decimal) else value for key, value in asdict(self).items()}

    def expected(self, target):
        return {"increase": target >= self.upper_kw, "decrease": target <= self.lower_kw}


class AgcDirectionScenario:
    def __init__(self, master, observer, sender, profile):
        self.master = master
        self.observer, self.sender, self.profile = observer, sender, profile
        self.observations = []

    def _wait_current(self, expected):
        deadline = time.monotonic() + self.profile.timeout_seconds
        last = None
        while time.monotonic() < deadline:
            last = self.observer.checkpoint()
            if all(last["points"][key]["value"] == value for key, value in expected.items()):
                return last
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        raise TimeoutError(f"准备阶段主站遥信未满足固定期望：预期{expected}，最后观察{last}")

    def _record(self, row):
        self.observations.append(row)
        self.master.metadata["AGC主站观察"] = self.observations
        (self.master.artifacts / "agc-direction-observations.json").write_text(
            json.dumps(self.observations, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    def _preserve_failure(self, error, row):
        try:
            self._record(row)
        except Exception as evidence_error:
            message = f"AGC失败取证保存异常：{evidence_error}"
            self.master.metadata.setdefault("AGC取证错误", []).append(message)
            error.add_note(message)
            LOGGER.exception("AGC失败观察记录保存异常，保留原始错误")

    def _apply(self, target, *, fresh_key=None, stage, require_lag=False):
        expected = self.profile.expected(target)
        row = {"阶段": stage, "固定目标kW": str(target), "固定闭锁期望": expected,
               "配置": self.profile.to_dict()}
        try:
            with self.master.step(f"核对104目标并单次下发AGC目标 {target} kW", expected=expected):
                self.master.verify_target(self.profile.master_host, self.profile.master_port,
                                          self.profile.common_address)
                baseline = self.observer.checkpoint(include_measurement=require_lag)
                row["基线"] = baseline
                if fresh_key and baseline["points"][fresh_key]["value"] == expected[fresh_key]:
                    raise AssertionError("业务操作前闭锁已是最终期望，不能证明本次变位")
                if require_lag:
                    measured_kw = Decimal(baseline["measurement"]["value_kw"])
                    if not self.profile.lower_kw < measured_kw < self.profile.upper_kw:
                        raise AssertionError(f"发送上限目标前实测功率须严格位于区间内，实际 {measured_kw} kW")
                row["发送结果"] = self.sender.send_target_kw(target)
                # 使用主站实际发送时刻划定本轮接收边界。
                baseline["wall_time"] = row["发送结果"]["sent_at"]
            title = "核对本轮闭锁变位及实测未到上限" if require_lag else "核对主站闭锁值、品质与本轮变位"
            with self.master.step(title, expected=expected):
                changed = (fresh_key,) if fresh_key else tuple(
                    key for key, value in expected.items() if baseline["points"][key]["value"] != value)
                if changed:
                    row["master"] = self.observer.wait_state(
                        expected, baseline, self.profile.timeout_seconds,
                        fresh_keys=changed, sent_frame_id=row["发送结果"]["sent_frame_id"],
                        measurement_below=self.profile.upper_kw if require_lag else None)
                else:
                    row["master"] = self._wait_current(expected)
            row["状态"] = "通过"
            LOGGER.info("AGC闭锁主站步骤完成：阶段=%s，目标=%s，期望=%s", stage, target, expected)
            self._record(row)
            return row
        except BaseException as error:
            row["状态"], row["错误"] = "失败", f"{type(error).__name__}：{error}"
            # 取证失败不得替换原业务失败，不下发任何恢复或重试命令。
            self._preserve_failure(error, row)
            raise

    def run_case(self, prepare, target, changed_key, *, require_lag=False):
        if changed_key not in self.profile.ioas:
            raise ValueError("AGC验收必须明确增闭锁或减闭锁变位点")
        if require_lag:
            with self.master.step("核对总召后实测功率点可用"):
                self.observer.checkpoint(include_measurement=True)
        self._apply(prepare, stage="独立准备")
        return self._apply(target, fresh_key=changed_key, stage="业务验收", require_lag=require_lag)
