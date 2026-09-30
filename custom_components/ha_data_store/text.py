"""ha_data_store 文本(text)实体平台。

静态实体：
  text.ha_data_store_ele_list —— 用电计量列表条数设置（状态值 "日,月,年"，如 "5,3,4"）
    默认值 "3,3,3"；仅后端校验格式（^\\d+,\\d+,\\d+$），非法输入拒绝写入；
    读取方（sensor.ha_data_store_all_power）遇到缺失/非法一律回退 "3,3,3"。
    继承 RestoreEntity：重启 HA 后保持用户设置，不会回到默认 "3,3,3"。

  text.ha_data_store_today_in_history_set —— 历史今日的时间范围设置
    写法 `<时间>,<前后分钟>`：
        "01,80"      → 01:00 前后 80 分钟
        "now,60"     → 此刻前后 60 分钟
        "09:02,30"   → 09:02 前后 30 分钟
        留空         → 全部数据（不限定时间范围）
    等价于历史今日接口的 at=<时间>&window=<分钟>；格式校验与传感器读取共用
    onthisday.parse_window_setting()，保证「能不能写」与「读出来是什么」一致。
    读取方为 sensor.ha_data_store_today_in_history，设置变化时立即刷新该传感器。
    同样继承 RestoreEntity，重启 HA 后保持设置。

  text.ha_data_store_comm_type_name —— 通讯数据表类型名设置（对应表名 attr_<类型名>）
    留空 = 自动探测 attr_type_defs 中 mode=comm 的类型名（优先取数据表已存在者），
    通常无需手动设置；存在多张通讯表时可用它明确指向其中一张。
    影响历史今日（comm 数据源）、历史今日传感器、通讯查询 API 的默认类型名；
    API 的 type_name 参数优先级更高。同样继承 RestoreEntity。

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
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    COMM_TYPE_NAME_ENTITY_ID,
    DOMAIN,
    TODAY_IN_HISTORY_RANGE_DEFAULT,
    TODAY_IN_HISTORY_RANGE_ENTITY_ID,
)
from .onthisday import parse_window_setting

_LOGGER = logging.getLogger(__name__)

_EL_LIST_ENTITY_ID = "text.ha_data_store_ele_list"
_EL_LIST_DEFAULT = "3,3,3"
# 校验：3 段、每段 1~4 位数字
_EL_LIST_RE = re.compile(r"^\s*\d{1,4}\s*,\s*\d{1,4}\s*,\s*\d{1,4}\s*$")

# 通讯数据表类型名校验：不含空白、1~50 字符（表名规则 attr_<类型名>）
_COMM_TYPE_RE = re.compile(r"^\S{1,50}$")


def _parse_restored_ele_list(raw) -> str | None:
    """把持久化状态字符串解析为合法的列表条数设置；空值/非法返回 None。"""
    txt = str(raw).strip() if raw is not None else ""
    if not txt or txt in ("unknown", "unavailable"):
        return None
    return txt if _EL_LIST_RE.match(txt) else None


class EleListSettingText(TextEntity, RestoreEntity):
    """用电计量列表条数设置。

    状态值格式 "日条数,月条数,年条数"，如 "5,3,4"；
    控制 sensor.ha_data_store_all_power 中每个实体的 daylist/monthlist/yearlist 显示条数。
    设置值通过 RestoreEntity 持久化，重启 HA 后自动恢复。
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

    async def async_added_to_hass(self) -> None:
        """恢复上次持久化的设置值（重启后保持，而非回到默认 "3,3,3"）。"""
        await super().async_added_to_hass()
        try:
            last = await self.async_get_last_state()
        except Exception:  # noqa: BLE001 - 恢复失败不影响实体可用
            return
        val = _parse_restored_ele_list(getattr(last, "state", None) if last else None)
        if val is not None:
            self._attr_native_value = val
            _LOGGER.info("[HDS] 用电计量列表条数已恢复上次设置: %s", val)

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


def _parse_restored_otd_range(raw) -> str | None:
    """把持久化状态解析为合法的范围设置；空值/非法返回 None。

    空字符串本身是合法设置（表示全部数据），因此这里只校验「非空时能否解析」。
    """
    txt = str(raw).strip() if raw is not None else ""
    if not txt or txt in ("unknown", "unavailable"):
        return None
    if txt.lower() in ("none", "-"):
        return None
    return txt if parse_window_setting(txt) else None


class TodayInHistoryRangeText(TextEntity, RestoreEntity):
    """历史今日的时间范围设置。

    写法 `<时间>,<前后分钟>`：
        "01,80"      → 01:00 前后 80 分钟
        "now,60"     → 此刻前后 60 分钟
        "09:02,30"   → 09:02 前后 30 分钟
        留空         → 全部数据（不限定时间范围）
    等价于历史今日接口的 at=<时间>&window=<分钟>，由其触发的传感器刷新立即生效。
    设置值通过 RestoreEntity 持久化，重启 HA 后自动恢复。
    """

    _attr_has_entity_name = False
    _attr_name = "历史今日时间范围"
    _attr_icon = "mdi:clock-time-four-outline"
    _attr_native_value = TODAY_IN_HISTORY_RANGE_DEFAULT

    def __init__(self, hass: HomeAssistant, device_info: DeviceInfo):
        self._hass = hass
        self.entity_id = TODAY_IN_HISTORY_RANGE_ENTITY_ID
        self._attr_unique_id = f"{DOMAIN}_today_in_history_range"
        self._attr_device_info = device_info

    async def async_added_to_hass(self) -> None:
        """恢复上次设置的时间范围（重启后保持）。"""
        await super().async_added_to_hass()
        try:
            last = await self.async_get_last_state()
        except Exception:  # noqa: BLE001 - 恢复失败不影响实体可用
            return
        val = _parse_restored_otd_range(getattr(last, "state", None) if last else None)
        if val is not None:
            self._attr_native_value = val
            _LOGGER.info("[HDS] 历史今日时间范围已恢复上次设置: %s", val)

    async def async_set_value(self, value: str) -> None:
        """留空表示全部数据；非空则必须是 `<时间>,<前后分钟>`，否则拒绝。"""
        text = str(value or "").strip()
        if text.lower() in ("none", "-"):
            text = ""
        if text and parse_window_setting(text) is None:
            raise ValueError(
                "格式应为「时间,前后分钟」，例如 01,80（01:00 前后 80 分钟）或 "
                "now,60（此刻前后 60 分钟）；留空表示全部数据"
            )
        self._attr_native_value = text
        self.async_write_ha_state()
        _LOGGER.info("[HDS] 历史今日时间范围已更新: %s", text or "（空 = 全部数据）")


def _parse_restored_comm_type(raw) -> str | None:
    """把持久化状态解析为合法的类型名；空值表示「自动探测」，非法返回 None。"""
    txt = str(raw).strip() if raw is not None else ""
    if not txt or txt in ("unknown", "unavailable"):
        return None
    if txt.lower() in ("none", "-", "auto"):
        return ""
    return txt if _COMM_TYPE_RE.match(txt) else None


class CommTypeNameText(TextEntity, RestoreEntity):
    """通讯数据表类型名设置（对应表名 `attr_<类型名>`）。

    取值：
        留空             → **自动探测** `attr_type_defs` 中 `mode=comm` 的类型名
                           （优先取数据表已存在者），通常无需手动设置
        如 comm_records  → 显式指定；存在多张通讯表时用它明确指向其中一张

    影响范围：历史今日（`comm` 数据源）、历史今日传感器、通讯查询 API
    （`/api/ha_data_store/comm`）的默认类型名；API 的 `type_name` 参数优先级更高。
    设置值通过 RestoreEntity 持久化，重启 HA 后自动恢复。
    """

    _attr_has_entity_name = False
    _attr_name = "通讯数据表类型名"
    _attr_icon = "mdi:table-cog"
    _attr_native_value = ""

    def __init__(self, hass: HomeAssistant, device_info: DeviceInfo):
        self._hass = hass
        self.entity_id = COMM_TYPE_NAME_ENTITY_ID
        self._attr_unique_id = f"{DOMAIN}_comm_type_name"
        self._attr_device_info = device_info

    async def async_added_to_hass(self) -> None:
        """恢复上次设置（重启后保持，而非回到自动探测）。"""
        await super().async_added_to_hass()
        try:
            last = await self.async_get_last_state()
        except Exception:  # noqa: BLE001 - 恢复失败不影响实体可用
            return
        val = _parse_restored_comm_type(raw=getattr(last, "state", None) if last else None)
        if val is not None:
            self._attr_native_value = val
            _LOGGER.info("[HDS] 通讯数据表类型名已恢复上次设置: %s",
                         val or "（空 = 自动探测）")

    async def async_set_value(self, value: str) -> None:
        """留空 = 自动探测；非空则须是合法类型名（无空格、≤50 字符）。"""
        text = str(value or "").strip()
        if text.lower() in ("none", "-", "auto"):
            text = ""
        if text and not _COMM_TYPE_RE.match(text):
            raise ValueError(
                "类型名只能是 1~50 个不含空格的字符（对应表名 attr_<类型名>）；"
                "留空表示自动探测"
            )
        self._attr_native_value = text
        self.async_write_ha_state()
        _LOGGER.info("[HDS] 通讯数据表类型名已更新: %s", text or "（空 = 自动探测）")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback):
    # 存储回调，供辅助元素动态创建 text 域实体
    hass.data.setdefault(DOMAIN, {})["async_add_text"] = async_add_entities

    device_info = DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="HA数据统一存储系统", manufacturer="HA数据统一存储系统",
    )
    async_add_entities([
        EleListSettingText(hass, device_info),
        TodayInHistoryRangeText(hass, device_info),
        CommTypeNameText(hass, device_info),
    ])
