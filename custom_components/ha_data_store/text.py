"""ha_data_store 文本(text)实体平台。

此平台本身不创建任何静态实体，仅为「辅助元素」动态创建 text 域实体注册回调。
"""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback):
    # 存储回调，供辅助元素动态创建 text 域实体
    hass.data.setdefault(DOMAIN, {})["async_add_text"] = async_add_entities
