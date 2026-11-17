"""Configure and read back AGC active-power step limits through the group editor."""

from dataclasses import KW_ONLY, dataclass
from decimal import Decimal, InvalidOperation
import re

from selenium.webdriver.common.by import By


_TITLE = "有功分步限幅"
_SWITCH = "//*[@role='switch' or @role='checkbox' or (self::input and @type='checkbox')]"
_NUMBER = "//input[not(@type='hidden' or @type='checkbox' or @type='radio')]"
_SYNC = re.compile(r"同步升降(?:功率)?(?:幅度|调整量)")
_ENABLE = re.compile(r"(?:启用(?:有功)?分步限幅|有功分步限幅)")
_SHARED = re.compile(r"(?:升降共用)?单次最大调整量")
_UP_WIDTH = re.compile(r"(?:单次最大(?:升|上调)(?:功率)?调整量|(?:升|上调)(?:功率)?单次最大调整量)")
_DOWN_WIDTH = re.compile(r"(?:单次最大(?:降|下调)(?:功率)?调整量|(?:降|下调)(?:功率)?单次最大调整量)")
_UP_INTERVAL = re.compile(r"(?:有功)?(?:升|上调)(?:功率)?(?:分步)?(?:下发)?(?:最小)?间隔")
_DOWN_INTERVAL = re.compile(r"(?:有功)?(?:降|下调)(?:功率)?(?:分步)?(?:下发)?(?:最小)?间隔")
_SEPARATE_INTERVAL = re.compile(r"(?:有功)?(?:分步限幅|限幅|分步下发)(?:最小)?间隔")
_FORM_LABEL = ("./ancestor::div[contains(concat(' ', normalize-space(@class), ' '), ' ant-form-item ')][1]"
               "//*[contains(concat(' ', normalize-space(@class), ' '), ' ant-form-item-label ')]//label")


@dataclass(frozen=True)
class AgcStepLimit:
    up_kw: int | float | Decimal
    down_kw: int | float | Decimal | None
    _: KW_ONLY
    up_seconds: int | float | Decimal | None = None
    down_seconds: int | float | Decimal | None = None
    sync_width: bool = False

    def __post_init__(self):
        if (self.up_seconds is None) != (self.down_seconds is None):
            raise ValueError("事件触发须同时提供升、降功率下发间隔")
        if not self.sync_width and self.down_kw is None:
            raise ValueError("未同步升降幅度时须提供单次最大降功率调整量")
        if self.sync_width and self.down_kw is not None and self.up_kw != self.down_kw:
            raise ValueError("同步升降幅度时只能使用一个共用调整量")


def _normalize_label(label):
    return re.sub(r"[（(][^）)]*[）)]$", "", re.sub(r"\s+", "", label).rstrip(":：")).rstrip(":：")


def _matching_controls(page, dialog, control, *patterns):
    matches = []
    for element in page.driver.find_elements(By.XPATH, dialog + control):
        if not element.is_displayed():
            continue
        names = [element.accessible_name, *(label.text for label in element.find_elements(By.XPATH, _FORM_LABEL))]
        label = next((name for name in names if name and any(
            pattern.fullmatch(_normalize_label(name)) for pattern in patterns)), None)
        if label is not None:
            matches.append((element, label))
    return matches


def _find_control(page, dialog, control, name, pattern):
    matches = _matching_controls(page, dialog, control, pattern)
    if len(matches) > 1:
        raise AssertionError(f"AGC「{name}」定位不唯一：{[label for _, label in matches]}")
    return matches[0][0] if matches else None


def _require_control(page, dialog, control, name, pattern):
    return page._until(lambda _: _find_control(page, dialog, control, name, pattern),
                       f"AGC 控制组窗口未显示「{name}」控件")


def _checked(element):
    state = element.get_attribute("aria-checked")
    if state in ("true", "false"):
        return state == "true"
    if element.tag_name == "input" and element.get_attribute("type") == "checkbox":
        return element.is_selected()
    raise AssertionError("AGC 选项未提供可读的开关状态")


def _set_switch(page, dialog, name, pattern, expected, *, verify, optional=False):
    lookup = _find_control if optional else _require_control
    element = lookup(page, dialog, _SWITCH, name, pattern)
    if element is None:
        return
    if not verify and _checked(element) != expected:
        if not element.is_enabled() or element.get_attribute("aria-disabled") == "true":
            raise AssertionError(f"AGC「{name}」不可操作")
        element.click()
        element = page._until(lambda _: (current := _find_control(page, dialog, _SWITCH, name, pattern))
                              and _checked(current) == expected and current, f"AGC「{name}」未切换到预期状态")
    page.desktop.check(f"核对 AGC {name}", "开启" if _checked(element) else "关闭",
                       "开启" if expected else "关闭")


def _number(page, dialog, name, pattern, expected, *, verify):
    element = _require_control(page, dialog, _NUMBER, name, pattern)
    if not verify:
        if (not element.is_enabled() or element.get_attribute("aria-disabled") == "true"
                or element.get_attribute("readonly") is not None):
            raise AssertionError(f"AGC「{name}」不可编辑")
        page._replace(element, expected)
        element = _require_control(page, dialog, _NUMBER, name, pattern)
    actual = element.get_property("value")
    expected_decimal = Decimal(str(expected))
    try:
        matches = Decimal(actual) == expected_decimal
    except (InvalidOperation, TypeError):
        matches = False
    page.desktop.check(f"核对 AGC {name}", str(expected_decimal) if matches else repr(actual), str(expected_decimal))


def _check_hidden_fields(page, dialog, name, *patterns):
    visible = _matching_controls(page, dialog, _NUMBER, *patterns)
    page.desktop.check(name, [label for _, label in visible], [])


def configure_step_limit(page, dialog, settings, *, verify=False):
    """Use the open AGC group editor; verify=True only reads the reopened draft."""
    title = dialog + f"//*[normalize-space(text())='{_TITLE}']"
    mode = "周期分配" if settings.up_seconds is None else "事件触发"
    action = "读回" if verify else "配置"
    with page.desktop.step(f"{action} AGC {mode}的{_TITLE}", expected="通过组级配置界面核对升降幅度和间隔"):
        page._until(lambda driver: any(element.is_displayed() for element in driver.find_elements(By.XPATH, title)),
                    f"AGC 控制组窗口未显示「{_TITLE}」配置区")
        _set_switch(page, dialog, "启用有功分步限幅", _ENABLE, True, verify=verify, optional=True)
        _set_switch(page, dialog, "同步升降幅度", _SYNC, settings.sync_width, verify=verify)
        if settings.sync_width:
            _number(page, dialog, "单次最大调整量（kW/次）", _SHARED, settings.up_kw, verify=verify)
            _check_hidden_fields(page, dialog, "核对同步开启时不显示独立升降幅度", _UP_WIDTH, _DOWN_WIDTH)
        else:
            _number(page, dialog, "单次最大升功率调整量", _UP_WIDTH, settings.up_kw, verify=verify)
            _number(page, dialog, "单次最大降功率调整量", _DOWN_WIDTH, settings.down_kw, verify=verify)
            _check_hidden_fields(page, dialog, "核对同步关闭时不显示共用幅度", _SHARED)
        if settings.up_seconds is not None:
            _number(page, dialog, "升功率下发间隔", _UP_INTERVAL, settings.up_seconds, verify=verify)
            _number(page, dialog, "降功率下发间隔", _DOWN_INTERVAL, settings.down_seconds, verify=verify)
        else:
            _check_hidden_fields(page, dialog, "核对周期分配不显示单独的分步限幅间隔",
                                 _UP_INTERVAL, _DOWN_INTERVAL, _SEPARATE_INTERVAL)
