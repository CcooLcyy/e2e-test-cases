"""验证专用 IEC104 链路仅转发明确选择的闭锁方向。"""

from selenium.webdriver.common.by import By

from testbench.case_plan import Step

from .agc_direction_page import AgcDirectionPage, _direction_point_acceptable
from .avc_period_iec104 import snapshot
from .measurement_expiry_run import _expect_writes
from .modbus_rtu_page import xpath_text
from .rtu_quality_frames import decode_frame


def prepare_single_block_forwarding(context):
    page = context.iec104
    page.create()
    origin = f"{context.module}/{context.name}"
    command, selected = f"{context.name}-target", f"{context.name}-increase"
    points = ((context.target_tag, command, page.FLOAT, "遥调"),
              (f"{context.module}增闭锁", selected, page.SINGLE, "遥信"))
    saved = {}
    for source, destination, point_type, business_type in points:
        page._import_agc_point(origin, source, destination, point_type, business_type)
        saved[destination] = page._read_point(destination, point_type, business_type)
    assert saved[command] != saved[selected], f"遥调和遥信 IOA 重复：{saved!r}"
    page.check_routes()

    # 本例 IEC104 链路是专用接收方，只有一条遥信路由和一条遥调路由。
    # 在源点仍未分配 IOA 时，不能用猜测的 IOA 做缺席断言。
    routes = []
    prefix = f"IEC104/{context.name} : "
    for row in page.upper.driver.find_elements(By.CSS_SELECTOR, ".data-bus-route-card tbody tr[data-row-key]"):
        cells = row.find_elements(By.CSS_SELECTOR, ":scope > td")
        if len(cells) >= 4:
            pair = tuple(cells[index].find_element(By.TAG_NAME, "strong").text.strip() for index in (1, 3))
            if any(endpoint.startswith(prefix) for endpoint in pair):
                routes.append(pair)
    context.desktop.check("核对专用链路只保存增闭锁转发和总目标遥调", sorted(routes), sorted(page.routes))

    page._open()
    page._select_link(page.name)
    page._assert_link()
    page.upper._replace(page._point_search(), "")
    table = (By.XPATH, page.POINT_CARD + "//tbody/tr[@data-row-key]")
    names = [row.get_attribute("data-row-key") for row in page.upper.driver.find_elements(*table) if row.is_displayed()]
    context.desktop.check("读回专用点表的全部点", sorted(names), sorted(saved))
    context.desktop.metadata["本例仅转发增闭锁的点表与路由"] = {"点表": saved, "路由": routes}
    page.start()
    context.target_ioa, context.increase_ioa = saved[command], saved[selected]
    context.mapping = {"port": page.port, "ca": 1, "target_ioa": context.target_ioa,
                       "increase_ioa": context.increase_ioa}
    context.signal_ioas = {"increase": context.increase_ioa}


def _read_local(context, increase, decrease):
    expected = {f"{context.module}增闭锁": increase, f"{context.module}减闭锁": decrease}
    group = context.group
    last = {}
    context.desktop.metadata["本地闭锁最后观察"] = last
    if context.module == "AGC":
        def matches(_):
            last.update((tag, group.read_direction_point(tag)) for tag in expected)
            return all(point["value"] is expected[tag] and _direction_point_acceptable(point)
                       for tag, point in last.items())
    else:
        tab = (By.XPATH, "//*[@aria-label='控制页面切换']//*[@role='tab'][normalize-space(.)='默认点']")
        element = group._until(lambda _: group._visible(tab), "AVC 默认点页签未显示")
        if element.get_attribute("aria-selected") != "true":
            group._click(tab, "打开本组 AVC 默认点和运行摘要")
        item = group._until(lambda _: group._group_item(), "未找到本例 AVC 控制组")
        assert group._group_identity(item) == group.conn_id, "AVC 默认点页面组身份不一致"
        item.click()
        card = (By.XPATH, "//div[contains(@class,'protocol-point-card')][./div[contains(@class,'ant-card-head')]"
                "//*[normalize-space(.)='默认点']]")

        def matches(_):
            panel = group._visible(card)
            if panel is None:
                return False
            for kind, (tag, value) in enumerate(expected.items(), 13):
                rows = panel.find_elements(By.XPATH, ".//tr[@data-row-key=" + xpath_text(f"{kind}-{tag}") + "]")
                if len(rows) != 1:
                    return False
                last[tag] = AgcDirectionPage._row_values(panel, rows[0])["最新值"]
                if last[tag] != ("是" if value else "否"):
                    return False
            # AVC 默认点表显示值，运行摘要另行统计所有已观察点的无效或不确定质量。
            summary = panel.find_element(By.CSS_SELECTOR, ".control-runtime-status").text
            last["运行摘要"] = summary
            return ("数据更新时间：未知" not in summary and "连接异常" not in summary
                    and "质量未指定、无效或不确定" not in summary)

    group._until(matches, f"未观察到本组当前有效的闭锁状态：{expected}", timeout=context.timeout)
    return last


def _frames(master, ioa):
    recorded = snapshot(master)["frames"]
    ids = [row["id"] for row in recorded]
    assert ids and ids == list(range(1, ids[-1] + 1)), f"本例主站报文历史不完整：{ids!r}"
    asdus = []
    labels = {"send": "发送", "receive": "接收", "sendFailed": "发送失败"}
    for row in recorded:
        packet = {"direction": labels[row["direction"]], "raw": row["raw"]}
        asdu = decode_frame(packet)
        if asdu is None:
            continue
        asdu.update(id=row["id"], raw=row["raw"])
        if asdu["direction"] == "接收" and asdu["type_id"] not in (50, 100):
            # 只接受单点监视方向的 I 帧。拒绝额外点、其它 ASDU 类型或未知地址（包括 SQ 帧）。
            raw = bytes.fromhex(row["raw"])
            assert asdu["type_id"] in (1, 30) and asdu["ca"] == 1, f"专用链路收到未配置的监视报文：{asdu!r}"
            size = 8 if asdu["type_id"] == 30 else 1
            assert raw[7] in (1, 0x81) and len(raw) == 15 + size, f"单点遥信对象数量或长度异常：{asdu!r}"
            assert int.from_bytes(raw[12:15], "little") == ioa, f"专用链路收到多余的遥信点：{asdu!r}"
            asdu.update(value=bool(raw[15] & 1), quality=raw[15] & 0xF0)
        asdus.append(asdu)
    return asdus


def _valid_selected(asdu, cot, value):
    return (asdu["direction"] == "接收" and asdu["type_id"] in (1, 30) and asdu["cot"] == cot
            and not asdu["negative"] and not asdu["test"] and asdu["quality"] == 0 and asdu["value"] is value)


def _verify_gi(context, marker, increase):
    context.master.interrogate()

    def collect():
        frames = [frame for frame in _frames(context.master, context.increase_ioa) if frame["id"] > marker]
        gi = [frame for frame in frames if frame["type_id"] == 100 and frame["ca"] == 1
              and not frame["negative"] and not frame["test"] and frame["objects"] == [{"ioa": 0, "qoi": 20}]]
        if [(frame["direction"], frame["cot"]) for frame in gi] != [("发送", 6), ("接收", 7), ("接收", 10)]:
            return False
        updates = [frame for frame in frames if gi[1]["id"] < frame["id"] < gi[2]["id"]
                   and frame["direction"] == "接收" and frame["cot"] == 20]
        assert len(updates) == 1 and _valid_selected(updates[0], 20, increase), (
            f"本轮总召须仅收到有效的已选增闭锁，实际：{updates!r}"
        )
        return updates[0]

    return context.master.wait_for(collect, "本轮总召未收到有效的已选增闭锁遥信和完整确认/终止", context.timeout)


def forwarding_phase(prefix, name, target, a, b, *, increase, decrease):
    expected = {"A": a, "B": b}
    action = f"遥调 {target}，核对本地闭锁与 IEC104 总召及自发报文"
    feedback = f"RTU 设定 A/B={a}/{b}，本地增/减闭锁={int(increase)}/{int(decrease)}，主站仅见增闭锁"

    def run(context):
        marker = len(context.simulator.events()) - 1
        result = context.sender.send_target_kw(target, timeout=context.timeout)
        previous_change = getattr(context, "single_output_previous_change", None)
        if previous_change:
            _verify_spontaneous_consistency(context, previous_change, result["sent_frame_id"])
        evidence = {"遥调": result, "预期成员设定": expected}
        context.desktop.metadata.setdefault("仅转发一路闭锁观察", []).append(evidence)
        changed = {role: value for role, value in expected.items() if context.previous.get(role) != value}
        evidence["实际 RTU 写和应答"] = _expect_writes(context, changed, after=marker, previous=context.previous)
        evidence["本组本地闭锁"] = _read_local(context, increase, decrease)
        if context.previous and context.previous_signals["increase"] is not increase:
            change = context.master.wait_for(
                lambda: next((frame for frame in _frames(context.master, context.increase_ioa)
                              if frame["id"] > result["sent_frame_id"] and _valid_selected(frame, 3, increase)), False),
                "未收到本轮选择转发的增闭锁自发变位", context.timeout,
            )
            evidence["已选增闭锁的自发报文"] = change
            context.single_output_previous_change = change
        gi_marker = snapshot(context.master)["frames"][-1]["id"]
        evidence["总召回来的增闭锁"] = _verify_gi(context, gi_marker, increase)
        if change := getattr(context, "single_output_previous_change", None):
            _verify_spontaneous_consistency(context, change)
        context.previous = expected
        context.previous_signals = {"increase": increase}

    return Step(f"{prefix}.single-output.{name}", action, feedback, run)


def _verify_spontaneous_consistency(context, baseline, before=None):
    unexpected = [frame for frame in _frames(context.master, context.increase_ioa)
                  if frame["direction"] == "接收" and frame["type_id"] in (1, 30) and frame["cot"] == 3
                  and frame["id"] > baseline["id"] and (before is None or frame["id"] < before)
                  and not _valid_selected(frame, 3, baseline["value"])]
    assert not unexpected, f"无新目标时专用链路收到不符合已选增闭锁的自发报文：{unexpected!r}"
