"""ha_data_store 数值(number)实体平台。

静态实体：
  number.ha_data_store_recent_days —— 「近期使用设备」统计窗口天数设置
    范围 1~365，默认 30；读取方：
      · sensor.近期使用设备（devices 窗口 + all 节点）
      · API /query?type=device_last_used（未显式传 window_days 时）
    继承 RestoreEntity：设置为 7 天后重启 HA 仍保持 7（不会被重置回默认 30）。

同时保留回调与桥接逻辑，供「辅助元素」/设备桥接动态创建 number 域实体使用。
"""
from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .bridge_entities import get_bridge_entities_for_platform, get_bridge_device_info
from .const import (
    DOMAIN,
    RECENT_DAYS_ENTITY_ID,
    RECENT_WINDOW_DAYS_DEFAULT,
    RECENT_WINDOW_DAYS_MAX,
    RECENT_WINDOW_DAYS_MIN,
)

_LOGGER = logging.getLogger(__name__)


def _parse_restored_days(raw) -> int | None:
    """把持久化状态字符串解析为合法窗口天数；空值/非法/超范围返回 None。"""
    txt = str(raw).strip() if raw is not None else ""
    if txt in ("", "unknown", "unavailable", "None", "none"):
        return None
    try:
        days = int(float(txt))
    except (TypeError, ValueError):
        return None
    if days < RECENT_WINDOW_DAYS_MIN or days > RECENT_WINDOW_DAYS_MAX:
        return None
    return days


class RecentDaysSettingNumber(NumberEntity, RestoreEntity):
    """「近期使用设备」统计窗口天数设置。

    状态值 = 天数（1~365，默认 30）。用户可在仪表盘调整，读取方（传感器 all 节点 /
    device_last_used 接口）实时生效；缺失或非法值一律回退 30。
    设置值通过 RestoreEntity 持久化，重启 HA 后自动恢复（不会回到默认 30）。
    """

    _attr_has_entity_name = False
    _attr_name = "近期使用天数"
    _attr_icon = "mdi:calendar-range"
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = RECENT_WINDOW_DAYS_MIN
    _attr_native_max_value = RECENT_WINDOW_DAYS_MAX
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "天"
    _attr_native_value = RECENT_WINDOW_DAYS_DEFAULT

    def __init__(self, hass, device_info: DeviceInfo):
        self._hass = hass
        self.entity_id = RECENT_DAYS_ENTITY_ID
        self._attr_unique_id = f"{DOMAIN}_recent_days_setting"
        self._attr_device_info = device_info

    async def async_added_to_hass(self) -> None:
        """恢复上次持久化的天数（重启后保持用户设置，而非回到默认 30）。"""
        await super().async_added_to_hass()
        try:
            last = await self.async_get_last_state()
        except Exception:  # noqa: BLE001 - 恢复失败不影响实体可用
            return
        days = _parse_restored_days(getattr(last, "state", None) if last else None)
        if days is not None:
            self._attr_native_value = days
            _LOGGER.info("[HDS] 近期使用天数已恢复上次设置: %s 天", days)

    async def async_set_native_value(self, value: float) -> None:
        """写值（HA 已按 min/max 校验，这里再取整兜底）。"""
        try:
            days = int(value)
        except (TypeError, ValueError):
            days = RECENT_WINDOW_DAYS_DEFAULT
        days = max(RECENT_WINDOW_DAYS_MIN, min(RECENT_WINDOW_DAYS_MAX, days))
        self._attr_native_value = days
        self.async_write_ha_state()
        _LOGGER.info("[HDS] 近期使用天数已更新: %s 天", days)


async def async_setup_entry(hass, entry, async_add_entities: AddEntitiesCallback):
    # 存储回调，供辅助元素动态创建 number 域实体
    hass.data.setdefault(DOMAIN, {})["async_add_number"] = async_add_entities

    device_info = DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="HA数据统一存储系统", manufacturer="HA数据统一存储系统",
    )
    entities: list = [RecentDaysSettingNumber(hass, device_info)]

    # 设备桥接：动态 number 实体
    bdi = get_bridge_device_info(entry.entry_id)
    try:
        be = get_bridge_entities_for_platform(hass, "number", bdi)
    except Exception as e:  # noqa: BLE001
        _LOGGER.error("[bridge] number: %s", e)
        be = []
    if be:
        reg = er.async_get(hass)
        for eid, ent in be:
            reg.async_get_or_create(domain="number", platform=DOMAIN,
                                    unique_id=ent.unique_id,
                                    suggested_object_id=eid.split(".", 1)[1])
        rb = hass.data.setdefault(DOMAIN, {}).setdefault("bridge_entity_instances", {})
        for eid, ent in be:
            rb[eid] = ent
        entities.extend(ent for _, ent in be)
        _LOGGER.info("[bridge] number 创建 %d 个", len(be))

    async_add_entities(entities)
