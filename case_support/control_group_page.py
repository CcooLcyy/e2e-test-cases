"""Common controls for saving and cleaning up dedicated AGC and AVC groups."""

from contextlib import contextmanager
import re

from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By

from .modbus_rtu_page import ModbusRtuPage, xpath_text


GROUP_LIST = ("//div[contains(concat(' ',normalize-space(@class),' '),' control-strategy-split ')]"
              "/div[contains(concat(' ',normalize-space(@class),' '),' resizable-split-pane--first ')]"
              "/div[contains(concat(' ',normalize-space(@class),' '),' ant-card ')]"
              "[./div[contains(concat(' ',normalize-space(@class),' '),' ant-card-head ')]"
              "//*[contains(concat(' ',normalize-space(@class),' '),' ant-card-head-title ') and normalize-space(.)='控制组列表']]")
AVC_CREATE_NOTICE = ModbusRtuPage.DIALOG_XPATH + "[.//*[normalize-space(text())='新增 AVC 控制组前请确认']]"
ROUTE_ALERT = ("//*[contains(concat(' ',normalize-space(@class),' '),' ant-alert ')]"
               "[.//*[normalize-space(text())='控制组配置已保存，映射尚未同步完成']]")
AGC_MEMBER_NAMES = '.agc-runtime-members tbody > tr[data-row-key] strong'
AVC_MEMBER_NAMES = ("//div[contains(@class,'protocol-point-card')][./div[contains(@class,'ant-card-head')]"
                    "//*[normalize-space(.)='成员配置']]//tbody/tr[@data-row-key]/td[1]//strong")


class ControlGroupPage(ModbusRtuPage):
    @contextmanager
    def _finish_group_save(self):
        """Check stop before submitting, then keep the save and its readback together."""
        self.desktop._check_stop()
        previous = self._cleaning
        self._cleaning = True
        try:
            yield
        finally:
            self._cleaning = previous
        self.desktop._check_stop()

    def _wait_group_save_idle(self, dialog):
        save = self._button("保存控制组并建立路由", dialog)
        self._until(lambda _: not (button := self._visible(save))
                    or "ant-btn-loading" not in (button.get_attribute("class") or "").split(),
                    "保存请求仍在进行，无法确认控制组配置")

    def _close_control_draft(self, dialog, cancel, title, discard_label):
        if not self._visible((By.XPATH, dialog)):
            return
        confirm = self.DIALOG_XPATH + "[.//*[normalize-space(text())=" + xpath_text(title) + "]]"
        discard = self._button(discard_label, confirm)
        if not self._visible(discard):
            self._click(self._button(cancel, dialog), "关闭本例控制组配置草稿")
        self._until(lambda _: self._visible(discard) or not self._visible((By.XPATH, dialog)),
                    "未显示本例草稿的放弃确认")
        if self._visible(discard):
            self._click(discard, "放弃本例未保存的改动")
        self._until(lambda _: not self._visible((By.XPATH, dialog)), "本例配置草稿未关闭")

    @staticmethod
    def _group_identity(item):
        match = re.search(r"\bconn_id=([1-9]\d*)\s*\|", item.text)
        if not match:
            raise RuntimeError(f"控制组缺少有效的连接编号：{item.text}")
        return int(match[1])

    def _control_group_item(self, name):
        return self._visible((By.XPATH, GROUP_LIST + "//li[.//span[normalize-space(.)=" + xpath_text(name) + "]]"))

    def _group_ui_error(self, prefix):
        return self._visible((By.XPATH, "//*[contains(@class,'ant-message-error')]"
                              "//*[starts-with(normalize-space(text())," + xpath_text(prefix) + ")]"))

    def _refresh_control_groups(self, module):
        locator = (By.XPATH, GROUP_LIST + "//div[contains(@class,'ant-card-extra')]//button")
        self._click(locator, f"刷新 {module} 控制组列表")
        prefix = "刷新控制组失败:" if module == "AGC" else "刷新 AVC 控制组失败:"

        def refreshed(_):
            if failure := self._group_ui_error(prefix):
                raise RuntimeError(failure.text)
            button = self._visible(locator)
            return button and button.is_enabled() and "ant-btn-loading" not in (button.get_attribute("class") or "").split()

        self._until(refreshed, f"{module} 控制组列表未完成刷新")

    def _group_confirmation_button(self, title, button, *, running_delete=False):
        matches = "normalize-space(.)=" + xpath_text(title)
        if running_delete:
            matches += " or normalize-space(.)=" + xpath_text(title + " 删除前会停止正在运行的控制组。")
        popup = ("//*[contains(concat(' ',normalize-space(@class),' '),' ant-popconfirm-inner-content ')]"
                 "[.//*[contains(@class,'ant-popconfirm-title') and (" + matches + ")]]"
                 "//*[contains(@class,'ant-popconfirm-buttons')]")
        return self._button(button, popup)

    def _finish_owned_routes(self, module, name):
        pending = self._visible((By.XPATH, ROUTE_ALERT))
        if pending:
            description = pending.find_element(By.CSS_SELECTOR, '.ant-alert-description').text
            groups, separator, _ = description.partition("：待新增/确认 ")
            if not separator or groups != f"{module} / {name}":
                raise RuntimeError(f"待同步映射无法确认仅属于本例控制组：{description}")
            self._click(self._button("仅重试映射", ROUTE_ALERT), "清理前同步本例控制组的待处理映射")
            try:
                self._until(lambda _: not self._visible((By.XPATH, ROUTE_ALERT)),
                            "本例映射同步仍失败，上位机禁止删除控制组")
            except TimeoutException as error:
                current = self._visible((By.XPATH, ROUTE_ALERT))
                raise RuntimeError(f"{error.msg}：{current.text if current else description}") from error

    def _delete_owned_group(self, module, name, conn_id, root):
        def owned_item():
            item = self._control_group_item(name)
            if item is None or self._group_identity(item) != conn_id:
                raise RuntimeError(f"删除前 {module} 控制组 {name} 的连接身份已变化，预期 conn_id={conn_id}")
            return item

        retry = any(tag.text == "待删除" for tag in owned_item().find_elements(By.CSS_SELECTOR, '.ant-tag'))
        self._click(self._button("重试删除" if retry else "删除", root), f"删除本例 {module} 控制组 {name}")
        confirmation = self._group_confirmation_button(f"确认删除 {name}？", "确认", running_delete=True)
        success = (By.XPATH, "//*[contains(concat(' ',normalize-space(@class),' '),' ant-message-success ')]"
                   "//*[normalize-space(text())=" + xpath_text(f"控制组 {name} 已删除") + "]")
        confirmed = False

        def deleted(_):
            nonlocal confirmed
            if failure := self._group_ui_error("删除控制组失败:"):
                raise RuntimeError(failure.text)
            confirmed = confirmed or self._visible(success) is not None
            return confirmed and not self._control_group_item(name)

        with self.desktop.step(f"确认删除本例 {module} 控制组 {name}", cancellable=False):
            button = self._until(lambda _: (button := self._visible(confirmation)) and button.is_enabled() and button,
                                 f"未显示 {module} 控制组 {name} 的删除确认按钮")
            owned_item()
            button.click()
            self._until(deleted, f"未收到 {module} 控制组 {name} 的删除成功响应，或列表仍显示该控制组")
        self._refresh_control_groups(module)
        if self._control_group_item(name):
            raise RuntimeError(f"刷新后仍显示 {module} 控制组 {name}")
