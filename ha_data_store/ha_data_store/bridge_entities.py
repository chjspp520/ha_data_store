"""设备桥接代理实体 — 将远程 HA 的实体以同 entity_id 映射到本地。

提供以下代理实体类型：
  - switch / light / climate / cover / fan / lock / number / select（可读写）
  - sensor / binary_sensor（只读）

所有控制操作通过 REST API 转发到远程 HA，状态通过 WebSocket 推送同步到本地。
"""

from __future__ import annotations

import logging
import aiohttp
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.components.light import LightEntity, ColorMode
from homeassistant.components.climate import ClimateEntity, HVACMode
from homeassistant.components.cover import CoverEntity
from homeassistant.components.fan import FanEntity
from homeassistant.components.lock import LockEntity
from homeassistant.components.number import NumberEntity
from homeassistant.components.select import SelectEntity
from homeassistant.components.sensor import SensorEntity
from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN, TABLE_BRIDGE_ENTITIES

_LOGGER = logging.getLogger(__name__)


# =========================================================================== #
#  REST API 辅助函数                                                              #
# =========================================================================== #
async def _call_remote_service(
    hass: HomeAssistant,
    bridge_config: dict,
    domain: str,
    service: str,
    target_entity_id: str,
    extra_data: dict | None = None,
) -> bool:
    url = f"{bridge_config['remote_url'].rstrip('/')}/api/services/{domain}/{service}"
    headers = {
        "Authorization": f"Bearer {bridge_config['access_token']}",
        "Content-Type": "application/json",
    }
    payload: dict[str, Any] = {"entity_id": target_entity_id}
    if extra_data:
        payload.update(extra_data)

    verify_ssl = bridge_config.get("verify_ssl", 1) != 0
    try:
        from homeassistant.helpers.aiohttp_client import async_get_clientsession
        session = async_get_clientsession(hass)
        async with session.post(url, json=payload, headers=headers, ssl=verify_ssl) as resp:
            if resp.status in (200, 201, 202):
                _LOGGER.debug("[bridge] ✅ %s/%s → %s", domain, service, target_entity_id)
                return True
            else:
                body = await resp.text()
                _LOGGER.warning("[bridge] ❌ %s/%s → %s status=%s %s",
                                domain, service, target_entity_id, resp.status, body[:200])
                return False
    except Exception as e:
        _LOGGER.error("[bridge] ❌ %s/%s → %s: %s", domain, service, target_entity_id, e)
        return False


# =========================================================================== #
#  基类                                                                           #
# =========================================================================== #
class BridgeBaseEntity:
    _attr_has_entity_name = True
    _attr_should_poll = False  # 由 WebSocket 推送更新，不轮询
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, bridge_config: dict[str, Any], entity_id: str) -> None:
        self._bridge_config = bridge_config
        self._bridge_remote_entity_id = entity_id

    @callback
    def push_remote_state(self, new_state_obj: Any) -> None:
        if new_state_obj is None:
            return
        try:
            attrs = new_state_obj.get("attributes", {})
            # 同步远程友好名称
            friendly = attrs.get("friendly_name")
            if friendly:
                self._attr_name = friendly
            self._bridge_update_from_remote(
                new_state_obj.get("state", ""),
                attrs,
            )
            self.async_write_ha_state()
        except Exception:
            _LOGGER.error("[bridge] 状态同步失败 entity=%s", self._bridge_remote_entity_id, exc_info=True)

    def _bridge_update_from_remote(self, state: str, attributes: dict) -> None:
        raise NotImplementedError

    async def _service_call(self, domain: str, service: str, extra: dict | None = None) -> bool:
        return await _call_remote_service(
            self.hass if self.hass else None,
            self._bridge_config,
            domain, service,
            self._bridge_remote_entity_id,
            extra,
        )


# =========================================================================== #
#  Switch                                                                        #
# =========================================================================== #
class BridgeSwitch(SwitchEntity, BridgeBaseEntity):
    def __init__(self, hass: HomeAssistant, bridge_config: dict, entity_id: str) -> None:
        SwitchEntity.__init__(self)
        BridgeBaseEntity.__init__(self, bridge_config, entity_id)
        self._attr_unique_id = f"{DOMAIN}_bridge_{entity_id}"
        self._attr_name = entity_id  # 显示"switch.fan"
        self._attr_is_on = False

    def _bridge_update_from_remote(self, state: str, attributes: dict) -> None:
        self._attr_is_on = state == "on"

    async def async_turn_on(self, **kwargs) -> None:
        if await self._service_call("switch", "turn_on"):
            self._attr_is_on = True
            self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        if await self._service_call("switch", "turn_off"):
            self._attr_is_on = False
            self.async_write_ha_state()

    @property
    def is_on(self) -> bool:
        return self._attr_is_on


# =========================================================================== #
#  Light                                                                         #
# =========================================================================== #
class BridgeLight(LightEntity, BridgeBaseEntity):
    def __init__(self, hass: HomeAssistant, bridge_config: dict, entity_id: str) -> None:
        # 必须在 LightEntity.__init__ 之前设置，因 proxcache 会立即缓存
        self._attr_supported_color_modes = {ColorMode.ONOFF}
        self._attr_color_mode = ColorMode.ONOFF
        LightEntity.__init__(self)
        BridgeBaseEntity.__init__(self, bridge_config, entity_id)
        self._attr_unique_id = f"{DOMAIN}_bridge_{entity_id}"
        self._attr_name = entity_id
        self._attr_is_on = False

    def _bridge_update_from_remote(self, state: str, attributes: dict) -> None:
        self._attr_is_on = state == "on"
        if attributes.get("brightness"):
            self._attr_brightness = attributes["brightness"]

    async def async_turn_on(self, **kwargs) -> None:
        extra = {}
        if "brightness" in kwargs:
            extra["brightness"] = kwargs["brightness"]
        if await self._service_call("light", "turn_on", extra if extra else None):
            self._attr_is_on = True
            self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        if await self._service_call("light", "turn_off"):
            self._attr_is_on = False
            self.async_write_ha_state()

    @property
    def is_on(self) -> bool:
        return self._attr_is_on


# =========================================================================== #
#  Climate                                                                       #
# =========================================================================== #
class BridgeClimate(ClimateEntity, BridgeBaseEntity):
    def __init__(self, hass: HomeAssistant, bridge_config: dict, entity_id: str) -> None:
        from homeassistant.const import UnitOfTemperature
        self._attr_temperature_unit = UnitOfTemperature.CELSIUS
        ClimateEntity.__init__(self)
        BridgeBaseEntity.__init__(self, bridge_config, entity_id)
        self._attr_unique_id = f"{DOMAIN}_bridge_{entity_id}"
        self._attr_name = entity_id
        self._attr_hvac_mode = HVACMode.OFF
        self._attr_hvac_modes = [HVACMode.OFF, HVACMode.COOL, HVACMode.HEAT, HVACMode.AUTO, HVACMode.DRY, HVACMode.FAN_ONLY]
        self._attr_current_temperature = None
        self._attr_target_temperature = 24
        self._attr_target_temperature_step = 1
        self._attr_min_temp = 16
        self._attr_max_temp = 30

    def _bridge_update_from_remote(self, state: str, attributes: dict) -> None:
        mode = attributes.get("hvac_mode")
        if mode:
            try:
                self._attr_hvac_mode = HVACMode(mode)
            except ValueError:
                pass
        # 不更新 hvac_modes，保持本地列表（桥接应报告自己能支持的模式）
        self._attr_current_temperature = attributes.get("current_temperature")
        t = attributes.get("temperature")
        if t is not None:
            self._attr_target_temperature = t
        if attributes.get("min_temp") is not None:
            self._attr_min_temp = attributes["min_temp"]
        if attributes.get("max_temp") is not None:
            self._attr_max_temp = attributes["max_temp"]

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if await self._service_call("climate", "set_hvac_mode", {"hvac_mode": hvac_mode.value}):
            self._attr_hvac_mode = hvac_mode
            self.async_write_ha_state()

    async def async_set_temperature(self, **kwargs) -> None:
        extra = {}
        if "temperature" in kwargs:
            extra["temperature"] = kwargs["temperature"]
        if await self._service_call("climate", "set_temperature", extra if extra else None):
            self._attr_target_temperature = kwargs.get("temperature", self._attr_target_temperature)
            self.async_write_ha_state()


# =========================================================================== #
#  Cover                                                                         #
# =========================================================================== #
class BridgeCover(CoverEntity, BridgeBaseEntity):
    def __init__(self, hass: HomeAssistant, bridge_config: dict, entity_id: str) -> None:
        CoverEntity.__init__(self)
        BridgeBaseEntity.__init__(self, bridge_config, entity_id)
        self._attr_unique_id = f"{DOMAIN}_bridge_{entity_id}"
        self._attr_name = entity_id
        self._attr_is_closed = True

    def _bridge_update_from_remote(self, state: str, attributes: dict) -> None:
        self._attr_is_closed = state == "closed"
        self._attr_current_cover_position = attributes.get("current_position")

    async def async_open_cover(self, **kwargs) -> None:
        if await self._service_call("cover", "open_cover"):
            self._attr_is_closed = False
            self.async_write_ha_state()

    async def async_close_cover(self, **kwargs) -> None:
        if await self._service_call("cover", "close_cover"):
            self._attr_is_closed = True
            self.async_write_ha_state()

    @property
    def is_closed(self) -> bool:
        return self._attr_is_closed


# =========================================================================== #
#  Fan                                                                           #
# =========================================================================== #
class BridgeFan(FanEntity, BridgeBaseEntity):
    def __init__(self, hass: HomeAssistant, bridge_config: dict, entity_id: str) -> None:
        FanEntity.__init__(self)
        BridgeBaseEntity.__init__(self, bridge_config, entity_id)
        self._attr_unique_id = f"{DOMAIN}_bridge_{entity_id}"
        self._attr_name = entity_id
        self._attr_is_on = False

    def _bridge_update_from_remote(self, state: str, attributes: dict) -> None:
        self._attr_is_on = state == "on"

    async def async_turn_on(self, **kwargs) -> None:
        if await self._service_call("fan", "turn_on"):
            self._attr_is_on = True
            self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        if await self._service_call("fan", "turn_off"):
            self._attr_is_on = False
            self.async_write_ha_state()

    @property
    def is_on(self) -> bool:
        return self._attr_is_on


# =========================================================================== #
#  Lock                                                                          #
# =========================================================================== #
class BridgeLock(LockEntity, BridgeBaseEntity):
    def __init__(self, hass: HomeAssistant, bridge_config: dict, entity_id: str) -> None:
        LockEntity.__init__(self)
        BridgeBaseEntity.__init__(self, bridge_config, entity_id)
        self._attr_unique_id = f"{DOMAIN}_bridge_{entity_id}"
        self._attr_name = entity_id
        self._attr_is_locked = True

    def _bridge_update_from_remote(self, state: str, attributes: dict) -> None:
        self._attr_is_locked = state in ("locked", "locking")

    async def async_lock(self, **kwargs) -> None:
        if await self._service_call("lock", "lock"):
            self._attr_is_locked = True
            self.async_write_ha_state()

    async def async_unlock(self, **kwargs) -> None:
        if await self._service_call("lock", "unlock"):
            self._attr_is_locked = False
            self.async_write_ha_state()

    @property
    def is_locked(self) -> bool:
        return self._attr_is_locked


# =========================================================================== #
#  Number                                                                       #
# =========================================================================== #
class BridgeNumber(NumberEntity, BridgeBaseEntity):
    def __init__(self, hass: HomeAssistant, bridge_config: dict, entity_id: str) -> None:
        NumberEntity.__init__(self)
        BridgeBaseEntity.__init__(self, bridge_config, entity_id)
        self._attr_unique_id = f"{DOMAIN}_bridge_{entity_id}"
        self._attr_name = entity_id
        self._attr_native_value: float = 0
        self._attr_native_min_value = 0
        self._attr_native_max_value = 100
        self._attr_native_step = 1

    def _bridge_update_from_remote(self, state: str, attributes: dict) -> None:
        try:
            self._attr_native_value = float(state)
        except (ValueError, TypeError):
            pass

    async def async_set_native_value(self, value: float) -> None:
        if await self._service_call("number", "set_value", {"value": value}):
            self._attr_native_value = value
            self.async_write_ha_state()

    @property
    def native_value(self) -> float | None:
        return self._attr_native_value


# =========================================================================== #
#  Select                                                                        #
# =========================================================================== #
class BridgeSelect(SelectEntity, BridgeBaseEntity):
    def __init__(self, hass: HomeAssistant, bridge_config: dict, entity_id: str) -> None:
        SelectEntity.__init__(self)
        BridgeBaseEntity.__init__(self, bridge_config, entity_id)
        self._attr_unique_id = f"{DOMAIN}_bridge_{entity_id}"
        self._attr_name = entity_id
        self._attr_current_option: str | None = None
        self._attr_options: list[str] = ["—"]

    def _bridge_update_from_remote(self, state: str, attributes: dict) -> None:
        self._attr_current_option = state or None
        options = attributes.get("options")
        if options and isinstance(options, list):
            self._attr_options = options

    async def async_select_option(self, option: str) -> None:
        if await self._service_call("select", "select_option", {"option": option}):
            self._attr_current_option = option
            self.async_write_ha_state()

    @property
    def current_option(self) -> str | None:
        return self._attr_current_option


# =========================================================================== #
#  Sensor（只读）                                                                  #
# =========================================================================== #
class BridgeSensor(SensorEntity, BridgeBaseEntity):
    def __init__(self, hass: HomeAssistant, bridge_config: dict, entity_id: str) -> None:
        SensorEntity.__init__(self)
        BridgeBaseEntity.__init__(self, bridge_config, entity_id)
        self._attr_unique_id = f"{DOMAIN}_bridge_{entity_id}"
        self._attr_name = entity_id
        self._attr_native_value = None

    def _bridge_update_from_remote(self, state: str, attributes: dict) -> None:
        try:
            self._attr_native_value = float(state)
        except (ValueError, TypeError):
            self._attr_native_value = state
        if attributes.get("unit_of_measurement"):
            self._attr_native_unit_of_measurement = attributes["unit_of_measurement"]


# =========================================================================== #
#  BinarySensor（只读）                                                             #
# =========================================================================== #
class BridgeBinarySensor(BinarySensorEntity, BridgeBaseEntity):
    def __init__(self, hass: HomeAssistant, bridge_config: dict, entity_id: str) -> None:
        BinarySensorEntity.__init__(self)
        BridgeBaseEntity.__init__(self, bridge_config, entity_id)
        self._attr_unique_id = f"{DOMAIN}_bridge_{entity_id}"
        self._attr_name = entity_id
        self._attr_is_on = False

    def _bridge_update_from_remote(self, state: str, attributes: dict) -> None:
        self._attr_is_on = state == "on"


# =========================================================================== #
#  Domain → 实体类 映射                                                           #
# =========================================================================== #
BRIDGE_ENTITY_MAP: dict[str, type] = {
    "switch": BridgeSwitch,
    "light": BridgeLight,
    "climate": BridgeClimate,
    "cover": BridgeCover,
    "fan": BridgeFan,
    "lock": BridgeLock,
    "number": BridgeNumber,
    "select": BridgeSelect,
    "sensor": BridgeSensor,
    "binary_sensor": BridgeBinarySensor,
}

SUPPORTED_BRIDGE_DOMAINS = list(BRIDGE_ENTITY_MAP.keys())


# =========================================================================== #
#  平台辅助函数                                                                     #
# =========================================================================== #
def get_bridge_entities_for_platform(
    hass: HomeAssistant, domain: str, device_info: DeviceInfo,
) -> list:
    import sqlite3

    db_path = hass.data.get(DOMAIN, {}).get("db_path")
    if not db_path:
        return []

    entity_class = BRIDGE_ENTITY_MAP.get(domain)
    if not entity_class:
        return []

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                f"SELECT be.entity_id, bc.remote_url, bc.access_token, bc.verify_ssl "
                f"FROM {TABLE_BRIDGE_ENTITIES} be "
                f"JOIN bridge_connections bc ON be.connection_id = bc.id "
                f"WHERE be.enabled = 1 AND bc.enabled = 1 "
                f"AND be.entity_id LIKE ?",
                (f"{domain}.%",),
            ).fetchall()
        finally:
            conn.close()

        result = []
        for row in rows:
            entity_id = row["entity_id"]
            verify_ssl = row["verify_ssl"]
            if verify_ssl is None:
                verify_ssl = 1
            bridge_config = {
                "remote_url": row["remote_url"],
                "access_token": row["access_token"],
                "verify_ssl": verify_ssl,
            }
            entity = entity_class(hass, bridge_config, entity_id)
            entity._attr_device_info = device_info
            # 强制使用与远程一致的 entity_id
            entity._attr_entity_id = entity_id
            result.append((entity_id, entity))

        _LOGGER.info("[bridge] 加载 %s 桥接实体: %d 个", domain, len(result))
        return result

    except Exception as e:
        _LOGGER.error("[bridge] 加载 %s 桥接实体失败: %s", domain, e)
        return []


def get_bridge_device_info(entry_id: str) -> DeviceInfo:
    """返回桥接设备的 DeviceInfo（独立于本集成其他设备）。"""
    return DeviceInfo(
        identifiers={(DOMAIN, "bridge", entry_id)},
        name="桥接设备",
        manufacturer="HA数据统一存储系统",
        model="远程 HA 实体代理",
    )
