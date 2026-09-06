"""ha_data_store 按钮实体平台。

button.ha_data_store_daily_summary：
  点击（button.press）→ 立即调用 sensor.today_family_status 的 async_trigger_refresh
  执行今日家庭状态聚合并刷新传感器状态。

button.ha_data_store_automation_status_refresh：
  点击 → 立即调用 sensor.ha_data_store_automation 的 async_trigger_refresh
  手动刷新自动化状态传感器（automations / automation_logs / ha_automation 汇总）。

button.ha_data_store_db_compress：
  点击 → 对集成 SQLite 数据库执行 VACUUM 压缩，状态属性记录压缩前/压缩后大小与压缩时间。
"""

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

_COMPRESS_ENTITY_ID = "button.ha_data_store_db_compress"


def _fmt_size(num_bytes: int) -> str:
    """把字节数格式化为易读大小。"""
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1048576:
        return f"{num_bytes / 1024:.1f} KB"
    return f"{num_bytes / 1048576:.2f} MB"


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


class DatabaseCompressButton(ButtonEntity):
    """数据库压缩按钮：点击执行 VACUUM 压缩。

    强制实体ID：button.ha_data_store_db_compress
    状态属性：压缩前大小 / 压缩后大小 / 压缩时间。
    """

    _attr_has_entity_name = False
    _attr_name = "数据库压缩"
    _attr_icon = "mdi:database-arrow-down"

    def __init__(self, hass: HomeAssistant, device_info: DeviceInfo):
        self._hass = hass
        self.entity_id = _COMPRESS_ENTITY_ID
        self._attr_unique_id = f"{DOMAIN}_db_compress"
        self._attr_device_info = device_info
        # 最近一次压缩结果（压缩前大小 / 压缩后大小 / 压缩时间）
        self._last: dict[str, str] = {}

    @property
    def extra_state_attributes(self) -> dict[str, str] | None:
        return self._last or None

    def _do_compress(self) -> dict:
        """在 executor 中执行 VACUUM 压缩（阻塞操作）。"""
        db_path = str(self._hass.data.get(DOMAIN, {}).get("db_path", ""))
        if not db_path or not os.path.isfile(db_path):
            return {"error": f"数据库文件不存在: {db_path}"}
        try:
            size_before = os.path.getsize(db_path)
            conn = sqlite3.connect(db_path)
            try:
                conn.execute("PRAGMA journal_mode=DELETE")
                conn.execute("VACUUM")
                conn.commit()
            finally:
                conn.close()
            size_after = os.path.getsize(db_path)
            return {
                "size_before": size_before,
                "size_after": size_after,
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        except Exception as exc:
            _LOGGER.exception("[HDS] 数据库压缩执行失败")
            return {"error": str(exc)}

    async def async_press(self) -> None:
        """点击执行数据库压缩。"""
        try:
            result = await self._hass.async_add_executor_job(self._do_compress)
        except Exception as exc:
            _LOGGER.exception("[HDS] 数据库压缩任务提交失败")
            return
        if "error" in result:
            _LOGGER.error("[HDS] 数据库压缩失败: %s", result["error"])
            return
        self._last = {
            "压缩前大小": _fmt_size(result["size_before"]),
            "压缩后大小": _fmt_size(result["size_after"]),
            "压缩时间": result["time"],
        }
        self.async_write_ha_state()
        _LOGGER.info(
            "[HDS] 数据库压缩完成: %s -> %s (%s)",
            _fmt_size(result["size_before"]),
            _fmt_size(result["size_after"]),
            result["time"],
        )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback):
    # 存储回调，供辅助元素动态创建
    hass.data.setdefault(DOMAIN, {})["async_add_button"] = async_add_entities

    device_info = DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="HA数据统一存储系统", manufacturer="HA数据统一存储系统",
    )
    async_add_entities([
        DailySummaryButton(hass, device_info),
        AutomationStatusButton(hass, device_info),
        DatabaseCompressButton(hass, device_info),
    ])
