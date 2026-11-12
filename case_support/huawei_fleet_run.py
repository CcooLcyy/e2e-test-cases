"""华为机群的页面操作和逐点采集证据。"""
from collections import Counter
from dataclasses import replace
from decimal import Decimal
import json
import logging
import time

from selenium.common.exceptions import NoSuchElementException, TimeoutException

from .huawei_fleet import (FLEET_DEVICE_COUNT, FLEET_SIZE_LABEL, READ_POINTS, STATUS_VALUES, STATUS_WORD_VALUES,
                           UNIMPLEMENTED_CONTROLS, build_fleet, compare_point, point_expected)


LOGGER = logging.getLogger(__name__)
STAGES = ("发电", "有功调节", "无功调节", "负无功调节", "待机", "限电停运", "恢复发电")
DEVICE_EXPECTED = f"slaveSim 已出现预定工况，{len(READ_POINTS)} 个采集点全部匹配且质量正常"


def device_step_title(stage, route, station):
    return f"第{route}路站号{station}：{stage}双端匹配"


class HuaweiFleetRun:
    def __init__(self, desktop, simulator, upper, config, run_id, *, buses=None):
        self.desktop, self.simulator, self.upper, self.config = desktop, simulator, upper, config
        self.run_id = run_id
        # 执行时准备的实际总线名称和站号优先，不能重新生成假名称覆盖发现结果。
        self.buses = build_fleet(config, run_id) if buses is None else buses
        self.connections, self.results, self.previous, self.evidence_errors = {}, [], {}, []
        self.copy_source = None
        self.source_point_configuration = None
        self.addresses = sorted({point.address for point in READ_POINTS} | {30075})
        desktop.metadata["华为机群拓扑"] = self.buses
        desktop.metadata["华为遥控缺口"] = list(UNIMPLEMENTED_CONTROLS)
        desktop.metadata["华为验收范围"] = "本次创建的机群连接对 slaveSim 驱动工况的抄读匹配"

    @staticmethod
    def _identity(bus, device):
        return f"{bus['index']}/{device['station_id']}"

    def check_sources(self):
        for bus in self.buses:
            for device in bus["devices"]:
                station = device["station_id"]
                try:
                    source = self.simulator.observe_device(bus["name"], station, self.addresses)
                except (AssertionError, NoSuchElementException, TimeoutException) as error:
                    raise RuntimeError(f"{bus['name']} 站号{station}的新建华为寄存器不可观察：{error}") from error
                self.desktop.check(f"确认第{bus['index']}路站号{station}的模拟设备及串口",
                                   (source["bus"], source["station_id"], source["actual_device"], source["link_path"]),
                                   (bus["name"], station, bus["serial_device"], bus["link_path"]))
                device["point_addresses"] = {point.key: source["registers"][point.address]["address"] for point in READ_POINTS}

    def create_device(self, bus, device):
        station = device["station_id"]
        name = f"e2e-huawei-{self.run_id}-r{bus['index']}-s{station}"
        tags = {point.key: f"{self.copy_source or name}-{point.key}" for point in READ_POINTS}
        self.desktop.check(f"创建前核对第{bus['index']}路的实际设备和固定配置路径",
                           self.simulator.observe_bus_paths(bus["name"]),
                           (bus["serial_device"], bus["link_path"]))
        if self.copy_source is None:
            self.upper.create_connection(name, bus["serial_device"], self.config.baud_rate, 8, 1, "N", station,
                                         self.config.poll_interval_ms)
            for point in READ_POINTS:
                self.upper.add_huawei_point(replace(point, address=device["point_addresses"][point.key]), tags[point.key])
            self.upper.assert_saved_configuration()
            self.source_point_configuration = self.upper.read_point_configuration()
        else:
            self.desktop.check("确认华为复制源点表与本台模拟寄存器地址一致",
                               {key: self.source_point_configuration[tag]["地址"] for key, tag in tags.items()},
                               {key: str(address) for key, address in device["point_addresses"].items()})
            self.upper.copy_connection(self.copy_source, name, bus["serial_device"], station)
            self.desktop.check("核对华为连接副本的可见点表与首台一致", self.upper.read_point_configuration(),
                               self.source_point_configuration)
        self.upper.start()
        self.connections[self._identity(bus, device)] = {"name": name, "tags": tags}
        if self.copy_source is None:
            self.copy_source = name

    def apply_stage(self, stage):
        for bus in self.buses:
            for device in bus["devices"]:
                args = bus["name"], device["station_id"]
                self.simulator.observe_device(*args, (32089, 32080, 32082, 35303, 35307))
                if stage == "有功调节":
                    self.simulator.apply_control(*args, "percent", Decimal(10) + Decimal(device["ordinal"]) / 10)
                elif stage == "无功调节":
                    self.simulator.apply_control(*args, "pf", Decimal("0.9"))
                elif stage == "负无功调节":
                    self.simulator.apply_control(*args, "ratio", Decimal("-0.2"))
                elif stage == "待机":
                    self.simulator.apply_scenario(*args, dc_available_kw=0)
                elif stage == "限电停运":
                    self.simulator.apply_control(*args, "percent", 0)
                    self.simulator.apply_scenario(*args, dc_available_kw=device["dc_available_kw"])
                elif stage == "恢复发电":
                    self.simulator.apply_control(*args, "pf", 1)
                    self.simulator.apply_control(*args, "percent", 100)

    def _physics_errors(self, stage, device, snapshot):
        """固定场景与目标独立约束，避免两端同时落在错误工况仍通过。"""
        registers = snapshot["registers"]
        power = Decimal(registers[32080]["value"]) / 1000
        reactive = Decimal(registers[32082]["value"]) / 1000
        pf = Decimal(registers[32084]["value"]) / 1000
        errors = []
        # 源端工况按协议分辨率核对；逐点比对按上位机显示精度进行。
        phase_voltage = Decimal(device["grid_line_voltage_v"]) / Decimal(3).sqrt()
        for address in (32069, 32070, 32071):
            if abs(Decimal(registers[address]["value"]) / 10 - phase_voltage) > Decimal("0.1"):
                errors.append(f"地址{address}电压不符合本台固定场景")
        if stage in ("待机", "限电停运"):
            if power != 0 or reactive != 0 or pf != 1:
                errors.append("待机/限电停运应为P=0、Q=0、PF=1")
            if any(registers[address]["value"] != 0 for address in (32072, 32074, 32076)):
                errors.append("待机/限电停运的三相电流应为0")
        else:
            target = (Decimal(str(device["dc_available_kw"])) * Decimal(str(device["efficiency"]))
                      if stage in ("发电", "恢复发电") else
                      Decimal(registers[30075]["value"]) / 1000
                      * (Decimal(10) + Decimal(device["ordinal"]) / 10) / 100)
            if abs(power - target) > Decimal("0.001"):
                errors.append(f"有功未达到本台固定目标{target}kW")
            if stage in ("发电", "有功调节", "恢复发电") and (reactive != 0 or pf != 1):
                errors.append("零无功场景应为Q=0、PF=1")
            if stage == "无功调节" and (reactive <= 0 or abs(pf - Decimal("0.9")) > Decimal("0.001")):
                errors.append("PF0.9调节后应有正无功且PF接近0.9")
            if stage == "负无功调节" and (reactive >= 0 or not -1 < pf < 0):
                errors.append("负Q/S调节后Q和PF应为负值")
        return errors

    def sample(self, stage, bus, device):
        identity = self._identity(bus, device)
        connection = self.connections[identity]
        self.upper.select(connection["name"])
        started = time.monotonic()
        deadline = started + self.config.acquisition_timeout
        attempts, last = [], None
        source_stage_seen = False
        while True:
            self.desktop._check_stop()
            before = self.simulator.observe_device(bus["name"], device["station_id"], self.addresses)
            upper = self.upper.observe_device(connection["tags"])
            after = self.simulator.observe_device(bus["name"], device["station_id"], self.addresses)
            rows = []
            for point in READ_POINTS:
                prior = self.previous.get((identity, point.key))
                previous_timestamp = None
                if prior and prior["换算期望"] != str(point_expected(point, before["registers"][point.address]["value"])):
                    previous_timestamp = prior["时间"]
                expected_raw = STATUS_VALUES[stage] if point.key == "status" else None
                row = compare_point(point, before["registers"][point.address]["value"],
                                    after["registers"][point.address]["value"], upper.get(point.key, {}),
                                    expected_raw=expected_raw,
                                    previous_timestamp=previous_timestamp)
                row["Tag"] = connection["tags"][point.key]
                observation = upper.get(point.key, {})
                if str(observation.get("address")) != str(point.address) or "0x03" not in str(observation.get("function", "")):
                    row["错误"].append("运行表地址或功能码与点表目录不一致")
                expected_type = "UINT16" if point.data_type == "BOOL" else point.data_type
                if before["registers"][point.address]["type"] != expected_type:
                    row["错误"].append("模拟器寄存器类型与点表目录不一致")
                row["通过"] = not row["错误"]
                rows.append(row)
            stage_errors = self._physics_errors(stage, device, before)
            for address, expected in ((32089, STATUS_VALUES[stage]), (32000, STATUS_WORD_VALUES[stage])):
                if before["registers"][address]["value"] != expected:
                    stage_errors.append(f"源寄存器{address}尚未达到工况{stage}的状态值{expected}")
            source_stage_seen = source_stage_seen or not stage_errors
            errors = stage_errors.copy()
            if before["actual_device"] != bus["serial_device"] or after["actual_device"] != bus["serial_device"]:
                errors.append(f"模拟器实际设备与下位机连接配置 {bus['serial_device']} 不一致，"
                              f"前采样 {before['actual_device']}，后采样 {after['actual_device']}")
            if before["link_path"] != bus["link_path"] or after["link_path"] != bus["link_path"]:
                errors.append(f"模拟器固定配置路径与保存值 {bus['link_path']} 不一致，"
                              f"前采样 {before['link_path']}，后采样 {after['link_path']}")
            if after["refresh_tick"] <= before["refresh_tick"]:
                errors.append("模拟器后采样不是新的页面状态刷新")
            if (before["bus"], before["station_id"]) != (bus["name"], device["station_id"]):
                errors.append("模拟器设备身份与目标不一致")
            elapsed = time.monotonic() - started
            if elapsed > self.config.acquisition_timeout:
                errors.append(f"采样已超过{self.config.acquisition_timeout}秒时限，不能判定按时匹配")
            last = {"工况": stage, "路号": bus["index"], "总线": bus["name"], "站号": device["station_id"],
                    "连接名称": connection["name"], "串口": bus["serial_device"], "固定配置路径": bus["link_path"],
                    "点位": rows, "模拟器前采样": before, "模拟器后采样": after,
                    "上位机采样": upper, "工况错误": errors,
                    "已观察到预定源工况": source_stage_seen,
                    "采样时长秒": round(elapsed, 3),
                    "通过": not errors and all(row["通过"] for row in rows)}
            attempts.append(last)
            finished = last["通过"] or time.monotonic() >= deadline
            if finished and not last["通过"] and not source_stage_seen:
                errors.insert(0, f"模拟器工况未就绪：在等待窗口内未观察到{stage}的预定源状态")
            self._save_attempts(identity, stage, attempts)
            if finished:
                for row in rows:
                    self.previous[(identity, row["点标识"])] = row
                return last
            # 等待本例设置的模拟器工况与两端反馈收敛。
            time.sleep(0.2)

    def _save_attempts(self, identity, stage, attempts):
        path = self.desktop.artifacts / f"huawei-samples-{identity.replace('/', '-')}-{STAGES.index(stage)}.json"
        path.write_text(json.dumps(attempts, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    def capture_both(self, description):
        shots = []
        for source, capture in (("slaveSim", self.simulator.capture), ("DSP上位机", self.desktop.capture)):
            try:
                begin = len(self.desktop.screenshots)
                capture("huawei-fleet-scene", description=f"{source}：{description}")
                shots.extend(shot["name"] for shot in self.desktop.screenshots[begin:])
            except Exception as error:
                message = f"{source}：{error}"
                self.evidence_errors.append(message)
                self.desktop.metadata.setdefault("华为取证错误", []).append(message)
                LOGGER.exception("华为%s现场采集失败，保留原始业务结果", source)
        return shots

    def observe_device(self, stage, bus, device):
        """采集并记录单台工况，返回可继续观察其他设备的失败。"""
        title = device_step_title(stage, bus["index"], device["station_id"])
        last = None
        try:
            with self.desktop.step(title, expected=DEVICE_EXPECTED):
                last = self.sample(stage, bus, device)
                prior_evidence_errors = len(self.evidence_errors)
                last["截图"] = self.capture_both(title)
                if len(self.evidence_errors) > prior_evidence_errors:
                    last["工况错误"].extend("双端证据缺失：" + error for error in self.evidence_errors[prior_evidence_errors:])
                    last["通过"] = False
                self.results.append(last)
                self.write_report()
                assert last["通过"], self.failure_description(last)
        except (InterruptedError, KeyboardInterrupt):
            self.write_report()
            raise
        except Exception as error:
            if last is None:
                last = {"工况": stage, "路号": bus["index"], "总线": bus["name"],
                        "站号": device["station_id"], "点位": [], "通过": False,
                        "工况错误": [f"操作或取数失败，未完成目标验收：{error}"],
                        "截图": self.capture_both(title + "失败现场")}
                self.results.append(last)
            elif last["通过"]:
                last["通过"] = False
                last["工况错误"].append(f"验收或报告处理失败：{error}")
            LOGGER.exception("华为设备验收失败，保留证据并继续其他设备：%s", title)
            if isinstance(error, OSError):
                raise
            self.write_report()
            return error

    def assert_complete(self):
        failed = [item for item in self.results if not item["通过"]]
        expected_count = FLEET_DEVICE_COUNT * len(STAGES)
        if len(self.results) != expected_count or failed:
            raise AssertionError(f"华为 {FLEET_SIZE_LABEL} 机群未全部通过：完成{len(self.results)}/{expected_count}项设备工况，失败{len(failed)}项；详见huawei-fleet-results.md")

    @staticmethod
    def failure_description(result):
        failures = [f"{point['点名']}：{'；'.join(point['错误'])}" for point in result["点位"] if not point["通过"]]
        return "；".join(result["工况错误"] + failures)

    def write_report(self):
        completed = len(self.results)
        total = FLEET_DEVICE_COUNT * len(STAGES)
        failed = [item for item in self.results if not item["通过"]]
        devices, channels = [], []
        for bus in self.buses:
            passed_devices = 0
            for device in bus["devices"]:
                records = [item for item in self.results if item["路号"] == bus["index"] and item["站号"] == device["station_id"]]
                passed = (len(records) == len(STAGES) and {item["工况"] for item in records} == set(STAGES)
                          and all(item["通过"] and len(item["点位"]) == len(READ_POINTS) for item in records))
                passed_devices += int(passed)
                devices.append({"路号": bus["index"], "站号": device["station_id"], "完成工况": len(records), "全部匹配": passed})
            channels.append({"路号": bus["index"], "设备数": len(bus["devices"]), "通过设备": passed_devices})
        passed_devices = sum(int(item["全部匹配"]) for item in devices)
        self.desktop.metadata["华为验收汇总"] = {"声明观察设备": len(self.connections), "计划设备": FLEET_DEVICE_COUNT,
                                                 "已完成工况": completed, "计划工况": total, "失败工况": len(failed),
                                                 "通过设备": passed_devices, "逐路": channels, "逐台": devices,
                                                 "全部抄通": len(self.connections) == FLEET_DEVICE_COUNT
                                                 and completed == total and passed_devices == FLEET_DEVICE_COUNT}
        report = {"拓扑": self.buses, "汇总": self.desktop.metadata["华为验收汇总"],
                  "未验证控制": list(UNIMPLEMENTED_CONTROLS), "设备工况结果": self.results}
        (self.desktop.artifacts / "huawei-fleet-results.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        lines = [f"# 华为 {FLEET_SIZE_LABEL} 抄读匹配结果", "", f"设备工况完成：{completed}/{total}，失败：{len(failed)}。",
                 "", "范围：本次创建的机群连接对 slaveSim 工况的 DSP 抄读匹配。DSP 下发调节及 104 遥控未验证。",
                 "40200/40201启停遥控：模型未实现，未发送命令，不计为通过。", "",
                 "| 路号 | 设备数 | 全部工况匹配设备数 |", "| --- | --- | --- |"]
        lines.extend(f"| {item['路号']} | {item['设备数']} | {item['通过设备']} |" for item in channels)
        lines.extend(["", "| 路号 | 站号 | 工况 | 匹配点数 | 结果 | 错误 |", "| --- | --- | --- | --- | --- | --- |"])
        for item in self.results:
            counts = Counter(point["通过"] for point in item["点位"])
            error = self.failure_description(item).replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {item['路号']} | {item['站号']} | {item['工况']} | {counts[True]}/{len(READ_POINTS)} | {'通过' if item['通过'] else '失败'} | {error} |")
        lines.extend(["", "逐点原始值、换算期望、显示期望、上位机实际值、相对换算期望的差值、质量和时间见 [完整对照](huawei-fleet-results.json)。",
                      "每台各工况的采样过程保存在 huawei-samples-*.json，截图路径记录在完整对照中。"])
        (self.desktop.artifacts / "huawei-fleet-results.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
