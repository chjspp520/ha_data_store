"""ha_data_store 媒体播放器实体平台。"""
from __future__ import annotations

import logging
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    """设置媒体播放器平台，存储回调供虚拟设备使用。"""
    # 存储回调，供虚拟设备动态创建
    hass.data.setdefault(DOMAIN, {})["async_add_media_player"] = async_add_entities
    _LOGGER.info("[media_player] 平台已就绪，虚拟设备回调已存储")
