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

    # 实体编组：媒体编组（成员全是 media_player / kind=media）投影成媒体实体
    try:
        from .entity_group_entities import async_setup_entity_group_media_players
        await async_setup_entity_group_media_players(hass, async_add_entities)
    except Exception as e:  # noqa: BLE001
        _LOGGER.exception("[entity_group] 编组 media_player 初始化失败: %s", e)
