"""ha_data_store 文本(text)实体平台。

静态实体：
  text.ha_data_store_ele_list —— 用电计量列表条数设置（状态值 "日,月,年"，如 "5,3,4"）
    默认值 "3,3,3"；仅后端校验格式（^\\d+,\\d+,\\d+$），非法输入拒绝写入；
    读取方（sensor.ha_data_store_all_power）遇到缺失/非法一律回退 "3,3,3"。

同时保留回调，供「辅助元素」动态创建 text 域实体使用。
"""
from __future__ import annotations

import logging
import re

from homeassistant.components.text import TextEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

_EL_LIST_ENTITY_ID = "text.ha_data_store_ele_list"
_EL_LIST_DEFAULT = "3,3,3"
# 校验：3 段、每段 1~4 位数字
_EL_LIST_RE = re.compile(r"^\s*\d{1,4}\s*,\s*\d{1,4}\s*,\s*\d{1,4}\s*$")


class EleListSettingText(TextEntity):
    """用电计量列表条数设置。

    状态值格式 "日条数,月条数,年条数"，如 "5,3,4"；
    控制 sensor.ha_data_store_all_power 中每个实体的 daylist/monthlist/yearlist 显示条数。
    """

    _attr_has_entity_name = False
    _attr_name = "用电计量列表条数"
    _attr_icon = "mdi:format-list-numbered"
    _attr_native_value = _EL_LIST_DEFAULT

    def __init__(self, hass: HomeAssistant, device_info: DeviceInfo):
        self._hass = hass
        self.entity_id = _EL_LIST_ENTITY_ID
        self._attr_unique_id = f"{DOMAIN}_ele_list_setting"
        self._attr_device_info = device_info

    async def async_set_value(self, value: str) -> None:
        """后端校验后写值；格式非法直接拒绝（状态保持不变）。"""
        if not isinstance(value, str):
            value = str(value or "")
        value = value.strip()
        if not _EL_LIST_RE.match(value):
            raise ValueError("格式应为 日条数,月条数,年条数，例如 5,3,4")
        self._attr_native_value = value
        self.async_write_ha_state()
        _LOGGER.info("[HDS] 用电计量列表条数已更新: %s", value)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback):
    # 存储回调，供辅助元素动态创建 text 域实体
    hass.data.setdefault(DOMAIN, {})["async_add_text"] = async_add_entities

    device_info = DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="HA数据统一存储系统", manufacturer="HA数据统一存储系统",
    )
    async_add_entities([EleListSettingText(hass, device_info)])
