"""根据模拟器页面实际配置选择资源，不从环境快照推测串口或站号。"""
from pathlib import PurePosixPath
from decimal import Decimal

from testbench.modbus_rtu_config import decimal_value


def validate_fleet_capacity(actual_buses, planned_buses):
    """所有设备的能力先满足原固定工况，再允许修改任一设备场景。"""
    actual = {bus["name"]: bus for bus in actual_buses}
    observations = []
    for bus in planned_buses:
        for device in bus["devices"]:
            identity = f"{bus['name']}/站号{device['station_id']}"
            try:
                slave = next(slave for slave in actual[bus["name"]]["slaves"]
                             if slave["station_id"] == device["station_id"])
                capacity = {}
                for key, address in (("pmax", 30075), ("smax", 30077), ("qmax", 30079), ("qmin", 30081)):
                    matches = [reg for reg in slave["registers"] if reg["function"] == "0x03" and reg["address"] == address]
                    if len(matches) != 1 or matches[0].get("source") not in ("const", "manual"):
                        raise ValueError(f"能力寄存器{address}缺失、重复或不是固定来源")
                    capacity[key] = decimal_value(matches[0].get("value")) / 1000
                pmax, smax, qmax, qmin = (capacity[key] for key in ("pmax", "smax", "qmax", "qmin"))
                generation = decimal_value(device["dc_available_kw"]) * decimal_value(device["efficiency"])
                limited = pmax * (Decimal(10) + Decimal(device["ordinal"]) / 10) / 100
                pf_q = limited * (1 / Decimal("0.9") ** 2 - 1).sqrt()
                negative_q = -Decimal("0.2") * smax
                if pmax <= 0 or smax <= 0 or qmax < 0 or qmin > 0:
                    raise ValueError("有功、视在或无功能力范围无效")
                if generation > min(pmax, smax):
                    raise ValueError(f"固定发电目标{generation}kW超过实际有功或视在容量")
                if limited >= generation:
                    raise ValueError(f"固定限功目标{limited}kW未低于发电目标{generation}kW")
                if pf_q > qmax or limited ** 2 + pf_q ** 2 > smax ** 2:
                    raise ValueError("实际无功或视在容量不能满足固定PF0.9工况")
                if negative_q < qmin or limited ** 2 + negative_q ** 2 > smax ** 2:
                    raise ValueError("实际无功或视在容量不能满足固定负Q/S工况")
            except (KeyError, StopIteration, ValueError, TypeError) as error:
                raise AssertionError(f"华为机群前置条件不满足：{identity}：{error}；未调整固定工况及验收断言") from error
            observations.append({"设备": identity, **{key: str(value) for key, value in capacity.items()},
                                 "固定发电目标kW": str(generation), "固定限功目标kW": str(limited)})
    return observations


def validate_topology(buses):
    names, links = set(), set()
    for bus in buses:
        name, link = bus.get("name"), bus.get("link")
        if (not isinstance(name, str) or not name.strip() or not isinstance(link, str)
                or not link.startswith("/") or link == "/" or str(PurePosixPath(link)) != link
                or ".." in PurePosixPath(link).parts or any(c.isspace() or ord(c) < 32 for c in link)):
            raise ValueError("slaveSim 页面总线名称或固定配置路径无效，未推测替代路径")
        if name in names or link in links:
            raise ValueError("slaveSim 页面出现重复总线名称或固定路径，无法唯一匹配")
        names.add(name); links.add(link)
        stations = [slave["station_id"] for slave in bus["slaves"]]
        if (any(type(station) is not int or not 1 <= station <= 247 for station in stations)
                or len(set(stations)) != len(stations)):
            raise ValueError("slaveSim 页面从站站号无效或重复")


def basic_candidate(buses, station_count=2):
    validate_topology(buses)
    for bus in buses:
        stations = []
        for slave in bus["slaves"]:
            if slave["model"]:
                continue
            for reg in slave["registers"]:
                if (reg["function"] == "0x03" and reg["type"] == "UINT16"
                        and reg["source"] == "manual" and reg["byte_order"] == "AB"
                        and reg["access"] == "readwrite" and type(reg["address"]) is int
                        and 0 <= reg["address"] <= 65535):
                    stations.append({"station_id": slave["station_id"], "address": reg["address"]})
                    break
        if len(stations) >= station_count:
            return bus, stations[:station_count]
    return None


def fleet_candidates(buses, addresses):
    validate_topology(buses)
    selected = []
    for count in range(1, 7):
        candidates = [bus for bus in buses if len(bus["slaves"]) == count
                      and sorted(slave["station_id"] for slave in bus["slaves"]) == list(range(1, count + 1))
                      and all(slave["model"] == "huawei_v3" and addresses <= {
                          reg["address"] for reg in slave["registers"] if reg["function"] == "0x03"}
                              for slave in bus["slaves"])]
        if not candidates:
            return None
        selected.append(candidates[0])
    return selected
