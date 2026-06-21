"""ha_data_store 开关实体平台。"""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN
from .bridge_entities import get_bridge_entities_for_platform, get_bridge_device_info

_LOGGER = logging.getLogger(__name__)


class HaDataStoreMasterSwitch(SwitchEntity, RestoreEntity):
    _attr_has_entity_name = True
    def __init__(self, hass, device_info, key=None, translation_key=None):
        self._hass = hass
        self._key = key or getattr(self, '_key', 'unknown')
        self._attr_unique_id = f"{DOMAIN}_{self._key}"
        self._attr_device_info = device_info
        if translation_key:
            self._attr_translation_key = translation_key
        self._attr_is_on = True
    async def async_turn_on(self, **kwargs): self._attr_is_on = True; self._hass.data.setdefault(DOMAIN, {})[self._key] = True; self.async_write_ha_state()
    async def async_turn_off(self, **kwargs): self._attr_is_on = False; self._hass.data.setdefault(DOMAIN, {})[self._key] = False; self.async_write_ha_state()
    async def async_added_to_hass(self):
        await super().async_added_to_hass(); last = await self.async_get_last_state()
        if last is not None: self._attr_is_on = last.state == "on"
        self._hass.data.setdefault(DOMAIN, {})[self._key] = self._attr_is_on

class HaDataStoreApiSwitch(HaDataStoreMasterSwitch):
    _key = "api_enabled"; _attr_translation_key = "api_access"
class HaDataStoreDbViewerSwitch(HaDataStoreMasterSwitch):
    _key = "db_viewer_enabled"; _attr_translation_key = "db_viewer_access"
class HaDataStoreDbEditSwitch(HaDataStoreMasterSwitch):
    _key = "db_edit_enabled"; _attr_translation_key = "db_edit_access"
class HaDataStoreRemoteAccessSwitch(HaDataStoreMasterSwitch):
    _key = "allow_remote_access"; _attr_translation_key = "remote_access"
    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        # 每次启动/重启都强制关闭，不同步历史状态
        self._attr_is_on = False
        self._hass.data.setdefault(DOMAIN, {})[self._key] = False
        self.async_write_ha_state()

class HaDataStoreDbSQLSwitch(HaDataStoreMasterSwitch):
    _key = "db_sql_enabled"; _attr_name = "前端执行SQL语句"
    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        # 每次启动/重启都强制关闭，不同步历史状态
        self._attr_is_on = False
        self._hass.data.setdefault(DOMAIN, {})[self._key] = False
        self.async_write_ha_state()


async def async_setup_entry(hass, entry, async_add_entities):
    # 存储回调，供虚拟设备动态创建
    hass.data.setdefault(DOMAIN, {})["async_add_switch"] = async_add_entities

    device_info = DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="HA数据统一存储系统", manufacturer="HA数据统一存储系统",
    )
    entities = [
        HaDataStoreApiSwitch(hass, device_info),
        HaDataStoreDbViewerSwitch(hass, device_info),
        HaDataStoreDbEditSwitch(hass, device_info),
        HaDataStoreRemoteAccessSwitch(hass, device_info),
        HaDataStoreDbSQLSwitch(hass, device_info),
    ]

    # 桥接开关
    bdi = get_bridge_device_info(entry.entry_id)
    try:
        bridge_entities = get_bridge_entities_for_platform(hass, "switch", bdi)
    except Exception as e:
        bridge_entities = []
        _LOGGER.error("[bridge] switch 失败: %s", e)
    if bridge_entities:
        entities.extend(ent for _, ent in bridge_entities)
        reg_er = er.async_get(hass)
        for eid, ent in bridge_entities:
            reg_er.async_get_or_create(domain="switch", platform=DOMAIN, unique_id=ent.unique_id, suggested_object_id=eid.split(".", 1)[1])
        reg = hass.data.setdefault(DOMAIN, {}).setdefault("bridge_entity_instances", {})
        for eid, ent in bridge_entities: reg[eid] = ent
        _LOGGER.info("[bridge] switch 创建 %d 个实体", len(bridge_entities))

    async_add_entities(entities)
