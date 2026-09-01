"""ha_data_store 按钮实体平台。

button.ha_data_store_daily_summary：
  点击（button.press）→ 立即调用 sensor.today_family_status 的 async_trigger_refresh
  执行今日家庭状态聚合并刷新传感器状态。

button.ha_data_store_automation_status_refresh：
  点击 → 立即调用 sensor.ha_data_store_automation 的 async_trigger_refresh
  手动刷新自动化状态传感器（automations / automation_logs / ha_automation 汇总）。
"""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class DailySummaryButton(ButtonEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "daily_summary_trigger"
    _attr_icon = "mdi:clipboard-text-play-outline"

    def __init__(self, hass, device_info):
        self._hass = hass
        self._attr_unique_id = f"{DOMAIN}_daily_summary_trigger"
        self._attr_device_info = device_info

    async def async_press(self) -> None:
        """点击触发今日家庭状态总结。"""
        try:
            sensor = self._hass.data.get(DOMAIN, {}).get("today_family_sensor")
            if sensor is None:
                _LOGGER.error("[HDS] 今日总结传感器未初始化，无法触发")
                return
            await sensor.async_trigger_refresh()
            _LOGGER.info("[HDS] 今日家庭状态总结已触发")
        except Exception as e:
            _LOGGER.exception("[HDS] 触发今日家庭状态总结失败: %s", e)


class AutomationStatusButton(ButtonEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "automation_status_refresh"
    _attr_icon = "mdi:refresh"

    def __init__(self, hass, device_info):
        self._hass = hass
        self._attr_unique_id = f"{DOMAIN}_automation_status_refresh"
        self._attr_device_info = device_info

    async def async_press(self) -> None:
        """点击手动刷新自动化状态传感器。"""
        try:
            sensor = self._hass.data.get(DOMAIN, {}).get("automation_status_sensor")
            if sensor is None:
                _LOGGER.error("[HDS] 自动化状态传感器未初始化，无法刷新")
                return
            await sensor.async_trigger_refresh()
            _LOGGER.info("[HDS] 自动化状态已手动刷新")
        except Exception as e:
            _LOGGER.exception("[HDS] 手动刷新自动化状态失败: %s", e)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback):
    device_info = DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="HA数据统一存储系统", manufacturer="HA数据统一存储系统",
    )
    async_add_entities([
        DailySummaryButton(hass, device_info),
        AutomationStatusButton(hass, device_info),
    ])
