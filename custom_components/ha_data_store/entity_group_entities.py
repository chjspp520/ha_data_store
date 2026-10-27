"""实体编组的实体层：1 个目录 sensor + 每编组 1 个 switch。

数据流（只读一条，写一条）：
- 读：``EntityGroupStore.snapshot()``（executor）→ sensor 属性 / switch 属性
- 写：HTTP view 写库后 ``async_notify_groups_changed()`` → 重读快照 → 增量同步 switch

> 分派与状态口径全部在 ``ENTITY_GROUP_DOMAIN_SERVICE_MAP``（``const.py``），
> 本模块不写任何 domain 判断。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from homeassistant.core import EVENT_STATE_CHANGED, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.components.sensor import SensorEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.const import UnitOfTemperature

# climate 走"软导入"：万一某个 HA 版本改了 const 结构，也只让空调编组降级成
# 聚合开关，而不是 ImportError 把 switch / sensor / climate 三个平台全拖挂。
try:  # noqa: SIM105
    from homeassistant.components.climate import ClimateEntity
    from homeassistant.components.climate.const import ClimateEntityFeature, HVACMode

    CLIMATE_SUPPORT = True
except Exception:  # noqa: BLE001
    CLIMATE_SUPPORT = False

    class ClimateEntity:  # type: ignore[no-redef]
        """占位基类（climate 不可用时不会被实例化）。"""

    class ClimateEntityFeature:  # type: ignore[no-redef]
        TARGET_TEMPERATURE = 0
        TURN_ON = 0
        TURN_OFF = 0
        FAN_MODE = 0
        SWING_MODE = 0

    class HVACMode:  # type: ignore[no-redef]
        OFF = "off"

# 灯光 / 窗帘 / 媒体播放器同样"软导入"：任何一个平台的常量结构变了，只让对应
# 编组降级成聚合开关，绝不能 ImportError 把整个平台链拖挂。
try:  # noqa: SIM105
    from homeassistant.components.light import (
        ATTR_BRIGHTNESS,
        ATTR_COLOR_TEMP_KELVIN,
        ATTR_EFFECT,
        ATTR_HS_COLOR,
        ATTR_RGB_COLOR,
        ColorMode,
        LightEntity,
    )

    LIGHT_SUPPORT = True
except Exception:  # noqa: BLE001
    LIGHT_SUPPORT = False

    class LightEntity:  # type: ignore[no-redef]
        """占位基类（light 不可用时不会被实例化）。"""

    class ColorMode:  # type: ignore[no-redef]
        ONOFF = "onoff"
        BRIGHTNESS = "brightness"
        COLOR_TEMP = "color_temp"
        HS = "hs"

    ATTR_BRIGHTNESS = "brightness"
    ATTR_COLOR_TEMP_KELVIN = "color_temp_kelvin"
    ATTR_HS_COLOR = "hs_color"
    ATTR_RGB_COLOR = "rgb_color"
    ATTR_EFFECT = "effect"

try:  # noqa: SIM105
    from homeassistant.components.cover import (
        ATTR_POSITION,
        CoverEntity,
        CoverEntityFeature,
    )

    COVER_SUPPORT = True
except Exception:  # noqa: BLE001
    COVER_SUPPORT = False

    class CoverEntity:  # type: ignore[no-redef]
        """占位基类（cover 不可用时不会被实例化）。"""

    class CoverEntityFeature:  # type: ignore[no-redef]
        OPEN = 1
        CLOSE = 2
        SET_POSITION = 4
        STOP = 8

    ATTR_POSITION = "position"

try:  # noqa: SIM105
    from homeassistant.components.media_player import (
        MediaPlayerEntity,
        MediaPlayerEntityFeature,
        MediaPlayerState,
    )

    MEDIA_SUPPORT = True
except Exception:  # noqa: BLE001
    MEDIA_SUPPORT = False

    class MediaPlayerEntity:  # type: ignore[no-redef]
        """占位基类（media_player 不可用时不会被实例化）。"""

    class MediaPlayerEntityFeature:  # type: ignore[no-redef]
        PAUSE = 1
        SEEK = 2
        VOLUME_SET = 4
        VOLUME_MUTE = 8
        PREVIOUS_TRACK = 16
        NEXT_TRACK = 32
        TURN_ON = 128
        TURN_OFF = 256
        PLAY = 16384
        STOP = 32768

    class MediaPlayerState:  # type: ignore[no-redef]
        OFF = "off"
        IDLE = "idle"
        PLAYING = "playing"
        PAUSED = "paused"

# 服务数据键直接用字面量：这些 ATTR_* 常量在不同 HA 版本里会"搬家"
# （新版 ATTR_TEMPERATURE 已从 climate.const 移到 homeassistant.const），
# 写死字符串可避免 ImportError 把整个集成平台拖挂。
ATTR_TEMPERATURE = "temperature"
ATTR_TARGET_TEMP_HIGH = "target_temp_high"
ATTR_TARGET_TEMP_LOW = "target_temp_low"
ATTR_HVAC_MODE = "hvac_mode"
ATTR_FAN_MODE = "fan_mode"
ATTR_SWING_MODE = "swing_mode"

_TEMP_UNIT = UnitOfTemperature.CELSIUS

from .const import DOMAIN, ENTITY_GROUP_DOMAIN_SERVICE_MAP
from .entity_group_store import EntityGroupStore
from .entity_group_actions import (
    async_execute_group_action,
    summarize_states,
)

_LOGGER = logging.getLogger(__name__)

SENSOR_ENTITY_ID = "sensor.ha_data_store_entity_groups"
# 「所有编组实体」：只列编组（entity_id/icon/name）和它的子实体，供前端/语音直接遍历
GROUP_ENTITIES_SENSOR_ID = "sensor.ha_data_store_group_entities"
SWITCH_OBJECT_PREFIX = "hds_eg_"
REFRESH_SECONDS = 30
DEFAULT_GROUP_ICON = "mdi:group"
DEFAULT_AC_ICON = "mdi:air-conditioner"
DEFAULT_MEMBER_ICON = "mdi:radiobox-blank"


GROUP_DEVICE_IDENTIFIER = f"{DOMAIN}_entity_groups"


def entity_group_device_info() -> DeviceInfo:
    """独立的「编组设备」：所有编组实体（目录 sensor + 编组 switch）都挂在它下面。

    与主设备（`identifiers={(DOMAIN, entry.entry_id)}`）分开，避免编组实体把
    主设备的实体列表撑乱；设备 id 固定，改名/重启都不会新建设备。
    """
    return DeviceInfo(
        identifiers={(DOMAIN, GROUP_DEVICE_IDENTIFIER)},
        name="编组设备",
        manufacturer="HA数据统一存储系统",
        model="实体编组",
        suggested_area=None,
    )


def _object_id(group_id: str) -> str:
    """实体 object_id：只依赖不可变的 group_id（改名不会换 id）。"""
    gid = str(group_id or "")
    return f"{SWITCH_OBJECT_PREFIX}{gid[3:] or gid}"


def _switch_entity_id(group_id: str) -> str:
    return f"switch.{_object_id(group_id)}"


# 编组可以投影成哪几个域（由 kind 决定）
GROUP_ENTITY_DOMAINS = ("switch", "light", "cover", "media_player", "climate")

# kind → 目标域：用户在编辑界面选什么类型，就生成什么域的实体。
# mixed（混编）/ device / 未识别的 kind → 统一 switch（聚合开关）。
_KIND_DOMAIN_MAP = {
    "ac": "climate",
    "climate": "climate",
    "lights": "light",
    "light": "light",
    "socket": "switch",
    "switch": "switch",
    "curtain": "cover",
    "cover": "cover",
    "media": "media_player",
    "media_player": "media_player",
}

# 编组实体域与成员真实域不一致时的兜底服务：switch 成员用 turn_on / turn_off 等价替代
# （空字符串 = 开关没有该语义，直接跳过，例如窗帘的 stop、媒体音量）
_SWITCH_FALLBACK_SERVICE = {
    "turn_on": "turn_on",
    "turn_off": "turn_off",
    "open_cover": "turn_on",
    "close_cover": "turn_off",
    "stop_cover": "",
    "media_play": "turn_on",
    "media_pause": "turn_off",
    "media_stop": "turn_off",
    "volume_set": "",
    "volume_mute": "",
}

# 各域平台是否可用（软导入失败 → 该域不可用 → 降级为 switch）
_DOMAIN_SUPPORT = {
    "switch": True,
    "climate": CLIMATE_SUPPORT,
    "light": LIGHT_SUPPORT,
    "cover": COVER_SUPPORT,
    "media_player": MEDIA_SUPPORT,
}


def group_entity_domain(group: dict) -> str:
    """编组投影成哪个域的实体：**完全由 ``kind``（编辑界面选的类型）决定**。

    1. ``kind`` 命中映射（lights/light→light、curtain→cover、media→media_player、
       ac→climate、socket→switch）→ 用该域；
    2. ``mixed``（混编）/ ``device`` / 未识别的 kind → **switch**（聚合开关）；
    3. 该域平台不可用 → **switch**（兜底）。

    注意：**不再看成员域**。以前"成员全是同一域时以成员域为准"，
    导致选「窗帘」但成员是 switch 设备时，实体前缀仍是 ``switch``，
    与界面显示/用户预期不一致。现在界面上选什么类型就生成什么前缀。
    """
    kind = str(group.get("kind") or "").strip().lower()

    domain = _KIND_DOMAIN_MAP.get(kind, "switch")
    if domain not in GROUP_ENTITY_DOMAINS or not _DOMAIN_SUPPORT.get(domain):
        return "switch"
    return domain


def group_entity_id(group: dict) -> str:
    """编组实体 id：``<域>.hds_eg_<编组 id 去 eg_ 后缀>``。"""
    gid = str(group.get("id") or "")
    if not gid:
        return ""
    return f"{group_entity_domain(group)}.{_object_id(gid)}"


# 「颜色」类模式的别名：统一收敛成 ColorMode.HS
LIGHT_COLOR_ALIASES = frozenset({"hs", "rgb", "rgbw", "rgbww", "xy"})


def normalize_light_color_modes(modes) -> set[str]:
    """把成员声明的 supported_color_modes 收敛成 HA 合法的集合。

    HA 的硬规则（``valid_supported_color_modes``）：
      - ``onoff`` 只能单独出现；
      - ``brightness`` 也只能单独出现；
      - ``color_temp`` 可以与 ``hs`` 共存。
    违规会让实体直接加不进来：
      ``HomeAssistantError: ... sets invalid color modes {...}``。
    """
    raw = {str(m).strip().lower() for m in (modes or []) if m}
    if not raw:
        # 成员没声明能力：给最通用的两种（含亮度，因为都不是 onoff）
        return {ColorMode.COLOR_TEMP, ColorMode.HS}
    has_color = bool(raw & LIGHT_COLOR_ALIASES)
    has_temp = ColorMode.COLOR_TEMP in raw
    if not has_color and not has_temp:
        # 只剩 onoff / brightness
        return {ColorMode.BRIGHTNESS} if ColorMode.BRIGHTNESS in raw else {ColorMode.ONOFF}
    out: set[str] = set()
    if has_temp:
        out.add(ColorMode.COLOR_TEMP)
    if has_color:
        out.add(ColorMode.HS)
    return out


# ── 目录 sensor ────────────────────────────────────────────────────────────
class EntityGroupsSensor(SensorEntity):
    """编组目录：state = 编组数量，attributes = 全量快照（前端唯一数据源）。"""

    _attr_has_entity_name = False
    _attr_should_poll = False
    _attr_icon = "mdi:group"
    _attr_native_unit_of_measurement = "个"
    # recorder 单状态属性上限 16384 字节，大对象不进历史
    _unrecorded_attributes = frozenset({"groups", "kinds", "counts", "service_map", "debug"})

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._attr_unique_id = f"{DOMAIN}_entity_groups"
        self._attr_name = "实体编组"
        self.entity_id = SENSOR_ENTITY_ID
        self._attr_device_info = entity_group_device_info()
        self._attr_native_value = 0
        self._attr_extra_state_attributes = self._empty_attrs()
        self._refreshing = False
        self._unsub = None

    @staticmethod
    def _empty_attrs() -> dict:
        return {
            "groups": [],
            "version": 0,
            "updated_at": 0,
            "kinds": {},
            "counts": {"groups": 0, "members": 0},
            "service_map": ENTITY_GROUP_DOMAIN_SERVICE_MAP,
            "debug": {"groups_in_snapshot": 0, "switches": 0, "adder_ready": False},
        }

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        async def _tick(now=None) -> None:
            await self.async_trigger_refresh()

        self._unsub = async_track_time_interval(
            self._hass, _tick, timedelta(seconds=REFRESH_SECONDS)
        )
        await self.async_trigger_refresh()

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None

    def apply_snapshot(self, snapshot: dict, debug: dict | None = None) -> None:
        """由 manager 调用：快照已在 executor 里读好。"""
        groups = snapshot.get("groups") or []
        kinds: dict[str, int] = {}
        members = 0
        for group in groups:
            kind = str(group.get("kind") or "mixed")
            kinds[kind] = kinds.get(kind, 0) + 1
            members += len(group.get("members") or [])
        self._attr_native_value = len(groups)
        self._attr_extra_state_attributes = {
            "groups": groups,
            "version": snapshot.get("version", 0),
            "updated_at": snapshot.get("updated_at", 0),
            "kinds": kinds,
            "counts": {"groups": len(groups), "members": members},
            "service_map": ENTITY_GROUP_DOMAIN_SERVICE_MAP,
            "debug": debug or {},
        }
        if self.hass is not None:
            self.async_write_ha_state()

    async def async_trigger_refresh(self) -> None:
        if self._refreshing:
            return
        self._refreshing = True
        try:
            manager = get_manager(self._hass)
            await manager.async_reload()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("[entity_group] 刷新编组目录失败")
        finally:
            self._refreshing = False


# ── 所有编组实体 sensor ────────────────────────────────────────────────────
class EntityGroupEntitiesSensor(SensorEntity):
    """「所有编组实体」：state = 编组内实体总数。

    属性是**精简投影**：每层只留 entity_id / name / icon，
    去掉 kind / rev / extra / actions / 颜色等全部冗余字段，
    方便前端与语音助手直接遍历（编组的 entity_id 即它的聚合 switch）：

        groups:
          - name: 客厅灯
            entity_id: switch.hds_eg_ke_ting
            icon: mdi:lightbulb-group
            entities:
              - {name: 客厅主灯, entity_id: light.ke_ting, icon: mdi:ceiling-light}
    """

    _attr_has_entity_name = False
    _attr_should_poll = False
    _attr_icon = DEFAULT_GROUP_ICON
    _attr_native_unit_of_measurement = "个"
    # recorder 单状态属性上限 16384 字节，大对象不进历史
    _unrecorded_attributes = frozenset({"groups"})

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._attr_unique_id = f"{DOMAIN}_group_entities"
        self._attr_name = "所有编组实体"
        self.entity_id = GROUP_ENTITIES_SENSOR_ID
        self._attr_device_info = entity_group_device_info()
        self._attr_native_value = 0
        self._attr_extra_state_attributes = {"groups": []}

    # -- 投影 ---------------------------------------------------------------
    def _registry(self):
        if self._hass is None:
            return None
        try:
            return er.async_get(self._hass)
        except Exception:  # noqa: BLE001 - 注册表未就绪（极早期）时降级
            return None

    def _member_name(self, member: dict) -> str:
        name = member.get("name")
        if name:
            return str(name)
        entity_id = member.get("entity_id")
        if not entity_id:
            return ""
        state = self._hass.states.get(entity_id) if self._hass is not None else None
        if state is not None and state.name:
            return str(state.name)
        return str(entity_id)

    @staticmethod
    def _state_icon(hass: HomeAssistant | None, entity_id: str | None) -> str | None:
        if not entity_id or hass is None:
            return None
        state = hass.states.get(entity_id)
        if state is None:
            return None
        icon = (state.attributes or {}).get("icon")
        return str(icon) if icon else None

    def _member_icon(self, member: dict, registry) -> str:
        icon = member.get("icon")
        if icon:
            return str(icon)
        entity_id = member.get("entity_id")
        # 成员没配图标：先用实体当前状态上的 icon，再退注册表，最后给默认图标
        icon = self._state_icon(self._hass, entity_id)
        if icon:
            return icon
        if registry is not None and entity_id:
            entry = registry.async_get(entity_id)
            if entry is not None and entry.icon:
                return str(entry.icon)
        return DEFAULT_MEMBER_ICON

    def apply_snapshot(self, snapshot: dict) -> None:
        """由 manager 调用：快照已在 executor 里读好，这里只做精简投影。"""
        registry = self._registry()
        groups: list[dict] = []
        total = 0
        for group in snapshot.get("groups") or []:
            entities = [
                {
                    "name": self._member_name(member),
                    "entity_id": str(member.get("entity_id") or ""),
                    "icon": self._member_icon(member, registry),
                }
                for member in (group.get("members") or [])
            ]
            total += len(entities)
            gid = str(group.get("id") or "")
            groups.append({
                "name": str(group.get("name") or gid),
                # 编组自己的实体 id（ac 编组 → climate.hds_eg_xxx，其余 → switch.hds_eg_xxx）
                "entity_id": group_entity_id(group),
                "icon": str(group.get("icon") or DEFAULT_GROUP_ICON),
                "entities": entities,
            })
        self._attr_native_value = total
        self._attr_extra_state_attributes = {"groups": groups}
        if self.hass is not None:
            self.async_write_ha_state()


# ── 编组实体公共部分 ────────────────────────────────────────────────────────
class _EntityGroupBase:
    """编组实体公共部分：装配 / 壳更新 / 状态汇总 / 属性投影 / 成员广播。

    - ``entity_domain``：该实体属于哪个平台（switch / climate），manager 用它
      判断"编组类型变了要换实体"（例如 kind 从 lights 改成 ac）。
    """

    entity_domain = "switch"
    _default_icon = DEFAULT_GROUP_ICON
    _attr_has_entity_name = False
    _attr_should_poll = False

    # -- 装配 -----------------------------------------------------------
    def _init_group(self, hass: HomeAssistant, manager: "EntityGroupManager",
                    group: dict, device_info=None) -> None:
        self._hass = hass
        self._manager = manager
        self._group = group
        self._attr_unique_id = f"{DOMAIN}_{group['id']}"
        self._attr_name = str(group.get("name") or group["id"])
        self._attr_icon = str(group.get("icon") or self._default_icon)
        self.entity_id = group_entity_id(group)
        self._attr_device_info = device_info or entity_group_device_info()
        self._attr_available = True
        self._last_status: str | None = None

    # 编组壳被改名/改图标后由 manager 更新，不重建实体
    def apply_group(self, group: dict) -> None:
        self._group = group
        if group.get("name"):
            self._attr_name = str(group["name"])
        if group.get("icon"):
            self._attr_icon = str(group["icon"])
        if self.hass is not None:
            self.async_write_ha_state()

    # -- 成员 / 汇总 -----------------------------------------------------
    def _member_ids(self, domain: str | None = None) -> list[str]:
        out: list[str] = []
        for member in self._group.get("members") or []:
            entity_id = str(member.get("entity_id") or "")
            if "." not in entity_id:
                continue
            if domain is None or entity_id.split(".", 1)[0] == domain:
                out.append(entity_id)
        return out

    def _target_ids(self, domain: str) -> list[str]:
        """编组实体域对应的成员：本域成员优先。

        "灯/窗帘接开关"很常见（成员是 ``switch.xxx``，用户把类型选成
        lights / curtain）。此时本域成员为空，实体不能变成空壳，
        所以回退到 ``switch`` 成员——控制时用 ``switch.turn_on/off`` 等价替代。
        """
        ids = self._member_ids(domain)
        if ids:
            return ids
        if domain in ("light", "cover"):
            return self._member_ids("switch")
        return ids

    async def _call_members(self, domain: str, service: str, data: dict,
                            members: list[str]) -> None:
        """按成员真实域分派服务。

        同域成员直接调本域服务；``switch`` 成员用 turn_on / turn_off 兜底
        （不传额外参数，避免服务校验拒绝）。
        """
        grouped: dict[str, list[str]] = {}
        for entity_id in members:
            edom = entity_id.split(".", 1)[0]
            if edom == domain:
                grouped.setdefault(domain, []).append(entity_id)
            elif edom == "switch":
                grouped.setdefault("switch", []).append(entity_id)
        for edom, ids in grouped.items():
            if edom == domain:
                await self._hass.services.async_call(
                    edom, service, {"entity_id": ids, **data}, blocking=True
                )
                continue
            fallback = _SWITCH_FALLBACK_SERVICE.get(service)
            if not fallback:
                continue
            await self._hass.services.async_call(
                edom, fallback, {"entity_id": ids}, blocking=True
            )

    def _summary(self) -> dict:
        states: dict[str, str | None] = {}
        for entity_id in self._member_ids():
            state = self._hass.states.get(entity_id)
            states[entity_id] = state.state if state is not None else None
        return summarize_states(self._group, states, ENTITY_GROUP_DOMAIN_SERVICE_MAP)

    def _base_attributes(self, summary: dict) -> dict:
        return {
            "group_id": self._group.get("id"),
            "kind": self._group.get("kind"),
            "rev": self._group.get("rev"),
            "member_count": summary["member_count"],
            "on_count": summary["on_count"],
            "partial": summary["partial"],
            "missing": summary["missing"],
            "on_entities": summary["on_entities"],
            "domains": summary["domains"],
            "actions": [a.get("id") for a in (self._group.get("actions") or [])],
            "last_status": self._last_status,
        }


# ── 编组 switch ────────────────────────────────────────────────────────────
class EntityGroupSwitch(_EntityGroupBase, SwitchEntity):
    """每个编组一个聚合开关：state 由成员状态实时汇总。"""

    entity_domain = "switch"

    def __init__(self, hass: HomeAssistant, manager: "EntityGroupManager",
                 group: dict, device_info=None) -> None:
        self._init_group(hass, manager, group, device_info)

    @property
    def is_on(self) -> bool:
        return bool(self._summary()["is_on"])

    @property
    def extra_state_attributes(self) -> dict:
        return self._base_attributes(self._summary())

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._run("on")

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._run("off")

    async def _run(self, command: str) -> None:
        try:
            result = await async_execute_group_action(
                self._hass, self._group, command=command, source=f"switch.{command}"
            )
            self._last_status = result.get("status")
        except Exception:  # noqa: BLE001
            _LOGGER.exception("[entity_group] 执行编组 %s 失败", self._group.get("id"))
            self._last_status = "failed"
            raise
        finally:
            self.async_write_ha_state()


# ── 编组 climate（空调编组）────────────────────────────────────────────────
class EntityGroupClimate(_EntityGroupBase, ClimateEntity):
    """空调编组：成员全是 climate（或 kind=ac）时用它代替聚合开关。

    - 状态：任一成员开着 → 取第一个非 off 成员的模式，否则 off；
    - 当前温度 / 目标温度 = 成员平均值（目标温度按 0.5 取整）；
    - 模式 / 温度 / 风速 / 摆风 → 广播给全部 climate 成员。
    """

    entity_domain = "climate"
    _default_icon = DEFAULT_AC_ICON
    _attr_temperature_unit = _TEMP_UNIT

    def __init__(self, hass: HomeAssistant, manager: "EntityGroupManager",
                 group: dict, device_info=None) -> None:
        self._init_group(hass, manager, group, device_info)

    # -- 成员属性读取 ---------------------------------------------------
    def _attr_of(self, entity_id: str, key: str, default=None):
        state = self._hass.states.get(entity_id)
        if state is None:
            return default
        return (state.attributes or {}).get(key, default)

    def _first_attr(self, key: str, default=None):
        for entity_id in self._member_ids("climate"):
            value = self._attr_of(entity_id, key)
            if value not in (None, ""):
                return value
        return default

    def _union_attrs(self, key: str) -> list[str]:
        out: list[str] = []
        for entity_id in self._member_ids("climate"):
            values = self._attr_of(entity_id, key)
            if not isinstance(values, (list, tuple)):
                continue
            for value in values:
                if value not in out:
                    out.append(value)
        return out

    def _numbers(self, key: str) -> list[float]:
        out: list[float] = []
        for entity_id in self._member_ids("climate"):
            value = self._attr_of(entity_id, key)
            if isinstance(value, (int, float)):
                out.append(float(value))
        return out

    # -- 能力 -----------------------------------------------------------
    @property
    def hvac_modes(self) -> list[str]:
        modes = [HVACMode.OFF]
        for mode in self._union_attrs("hvac_modes"):
            if mode not in modes:
                modes.append(mode)
        return modes

    @property
    def supported_features(self) -> int:
        features = (
            ClimateEntityFeature.TARGET_TEMPERATURE
            | ClimateEntityFeature.TURN_ON
            | ClimateEntityFeature.TURN_OFF
        )
        if self.fan_modes:
            features |= ClimateEntityFeature.FAN_MODE
        if self.swing_modes:
            features |= ClimateEntityFeature.SWING_MODE
        return features

    @property
    def min_temp(self) -> float:
        values = self._numbers("min_temp")
        return min(values) if values else 7.0

    @property
    def max_temp(self) -> float:
        values = self._numbers("max_temp")
        return max(values) if values else 35.0

    @property
    def target_temperature_step(self) -> float:
        return float(self._first_attr("target_temp_step", 0.5) or 0.5)

    # -- 状态 -----------------------------------------------------------
    @property
    def hvac_mode(self) -> str:
        if not self._summary()["is_on"]:
            return HVACMode.OFF
        for entity_id in self._member_ids("climate"):
            state = self._hass.states.get(entity_id)
            if state is None:
                continue
            if state.state in (HVACMode.OFF, "unavailable", "unknown", ""):
                continue
            return state.state
        return HVACMode.OFF

    @property
    def current_temperature(self):
        values = self._numbers("current_temperature")
        return round(sum(values) / len(values), 1) if values else None

    @property
    def target_temperature(self):
        values = self._numbers("temperature")
        if not values:
            return None
        return round(sum(values) / len(values) * 2) / 2

    @property
    def fan_modes(self):
        return self._union_attrs("fan_modes") or None

    @property
    def fan_mode(self):
        return self._first_attr("fan_mode")

    @property
    def swing_modes(self):
        return self._union_attrs("swing_modes") or None

    @property
    def swing_mode(self):
        return self._first_attr("swing_mode")

    @property
    def extra_state_attributes(self) -> dict:
        attrs = self._base_attributes(self._summary())
        attrs["entities"] = self._member_ids("climate")
        return attrs

    # -- 控制：广播给全部 climate 成员 -----------------------------------
    async def _broadcast(self, service: str, data: dict) -> None:
        members = self._member_ids("climate")
        if not members:
            return
        try:
            await self._hass.services.async_call(
                "climate", service, {"entity_id": members, **data}, blocking=True
            )
            self._last_status = "success"
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "[entity_group] 编组 %s 调用 climate.%s 失败", self._group.get("id"), service
            )
            self._last_status = "failed"
            raise
        finally:
            self.async_write_ha_state()

    async def async_turn_on(self) -> None:
        await self._broadcast("turn_on", {})

    async def async_turn_off(self) -> None:
        await self._broadcast("turn_off", {})

    async def async_set_hvac_mode(self, hvac_mode: str) -> None:
        if hvac_mode in (None, "", HVACMode.OFF):
            await self.async_turn_off()
            return
        await self._broadcast("set_hvac_mode", {ATTR_HVAC_MODE: hvac_mode})

    async def async_set_temperature(self, **kwargs: Any) -> None:
        data = {
            key: kwargs[key]
            for key in (ATTR_TEMPERATURE, ATTR_TARGET_TEMP_HIGH, ATTR_TARGET_TEMP_LOW)
            if kwargs.get(key) is not None
        }
        if not data:
            return
        await self._broadcast("set_temperature", data)

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        await self._broadcast("set_fan_mode", {ATTR_FAN_MODE: fan_mode})

    async def async_set_swing_mode(self, swing_mode: str) -> None:
        await self._broadcast("set_swing_mode", {ATTR_SWING_MODE: swing_mode})


# ── 编组 light（灯光组）────────────────────────────────────────────────────
class EntityGroupLight(_EntityGroupBase, LightEntity):
    """灯光编组：成员全是 light（或 kind=lights/light）时投影成 light 实体。

    亮度取开启成员平均值，色温/色相取第一个开着且有值的成员；
    turn_on 的 brightness / color_temp_kelvin / hs_color / rgb_color 广播给全部
    可调光的 light 成员，turn_off 广播关闭。
    """

    entity_domain = "light"
    _default_icon = "mdi:lightbulb-group"

    def __init__(self, hass: HomeAssistant, manager: "EntityGroupManager",
                 group: dict, device_info=None) -> None:
        self._init_group(hass, manager, group, device_info)

    # -- 成员值 ---------------------------------------------------------
    def _attr_of(self, entity_id: str, key: str, default=None):
        state = self._hass.states.get(entity_id)
        if state is None:
            return default
        return (state.attributes or {}).get(key, default)

    def _light_ids(self, on_only: bool = False) -> list[str]:
        out = []
        for entity_id in self._target_ids("light"):
            if on_only:
                state = self._hass.states.get(entity_id)
                if state is None or state.state != "on":
                    continue
            out.append(entity_id)
        return out

    def _numbers(self, key: str, on_only: bool = False) -> list[float]:
        out = []
        for entity_id in self._light_ids(on_only):
            value = self._attr_of(entity_id, key)
            if isinstance(value, (int, float)):
                out.append(float(value))
        return out

    # -- 能力 -----------------------------------------------------------
    @property
    def supported_color_modes(self) -> set:
        if not self._member_ids("light"):
            # 成员全是 switch（灯接开关，走了 _target_ids 的兜底）：只支持开/关，
            # 别再给亮度/色温滑条，否则用户拖了没反应。
            return {ColorMode.ONOFF}
        modes: set = set()
        for entity_id in self._light_ids():
            values = self._attr_of(entity_id, "supported_color_modes")
            if isinstance(values, (list, tuple, set)):
                modes.update(str(v).lower() for v in values)
        return normalize_light_color_modes(modes)

    @property
    def min_color_temp_kelvin(self) -> int:
        values = self._numbers("min_color_temp_kelvin")
        return int(min(values)) if values else 2000

    @property
    def max_color_temp_kelvin(self) -> int:
        values = self._numbers("max_color_temp_kelvin")
        return int(max(values)) if values else 6500

    # -- 状态 -----------------------------------------------------------
    @property
    def is_on(self) -> bool:
        return bool(self._summary()["is_on"])

    @property
    def brightness(self):
        values = self._numbers(ATTR_BRIGHTNESS, on_only=True) or self._numbers(ATTR_BRIGHTNESS)
        return int(round(sum(values) / len(values))) if values else None

    @property
    def color_mode(self):
        """必须落回 supported_color_modes 之内，否则 HA 直接拒绝添加实体。"""
        supported = self.supported_color_modes
        mode = ""
        for entity_id in self._light_ids(on_only=True) or self._light_ids():
            value = self._attr_of(entity_id, "color_mode")
            if value:
                mode = str(value).lower()
                break
        if mode in supported:
            return mode
        if mode in LIGHT_COLOR_ALIASES and ColorMode.HS in supported:
            return ColorMode.HS
        for candidate in (ColorMode.COLOR_TEMP, ColorMode.HS,
                          ColorMode.BRIGHTNESS, ColorMode.ONOFF):
            if candidate in supported:
                return candidate
        return next(iter(supported))

    @property
    def color_temp_kelvin(self):
        values = self._numbers(ATTR_COLOR_TEMP_KELVIN)
        return int(round(sum(values) / len(values))) if values else None

    @property
    def hs_color(self):
        for entity_id in self._light_ids(on_only=True) or self._light_ids():
            value = self._attr_of(entity_id, "hs_color")
            if isinstance(value, (list, tuple)) and len(value) == 2:
                return (float(value[0]), float(value[1]))
        return None

    @property
    def rgb_color(self):
        for entity_id in self._light_ids(on_only=True) or self._light_ids():
            value = self._attr_of(entity_id, "rgb_color")
            if isinstance(value, (list, tuple)) and len(value) == 3:
                return tuple(int(x) for x in value)
        return None

    @property
    def effect_list(self):
        out: list[str] = []
        for entity_id in self._light_ids():
            values = self._attr_of(entity_id, "effect_list")
            if not isinstance(values, (list, tuple)):
                continue
            for value in values:
                if value not in out:
                    out.append(value)
        return out or None

    @property
    def effect(self):
        for entity_id in self._light_ids(on_only=True) or self._light_ids():
            value = self._attr_of(entity_id, ATTR_EFFECT)
            if value:
                return value
        return None

    @property
    def extra_state_attributes(self) -> dict:
        attrs = self._base_attributes(self._summary())
        attrs["entities"] = self._light_ids()
        return attrs

    # -- 控制 -----------------------------------------------------------
    async def _broadcast(self, service: str, data: dict) -> None:
        members = self._light_ids()
        if not members:
            return
        try:
            # 成员可能是 switch（灯接开关）→ _call_members 会转而调 switch.turn_on/off
            await self._call_members("light", service, data, members)
            self._last_status = "success"
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "[entity_group] 编组 %s 调用 light.%s 失败", self._group.get("id"), service
            )
            self._last_status = "failed"
            raise
        finally:
            self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        data = {}
        for key in (ATTR_BRIGHTNESS, ATTR_COLOR_TEMP_KELVIN, ATTR_HS_COLOR,
                    ATTR_RGB_COLOR, ATTR_EFFECT, "color_temp"):
            if kwargs.get(key) is not None:
                data[key] = kwargs[key]
        await self._broadcast("turn_on", data)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._broadcast("turn_off", {})


# ── 编组 cover（窗帘组）────────────────────────────────────────────────────
class EntityGroupCover(_EntityGroupBase, CoverEntity):
    """窗帘编组：成员全是 cover（或 kind=curtain/cover）时投影成 cover 实体。

    位置取成员平均；任一成员 opening/closing → 跟随；open/close/stop/set_position 广播。
    """

    entity_domain = "cover"
    _default_icon = "mdi:curtains"

    def __init__(self, hass: HomeAssistant, manager: "EntityGroupManager",
                 group: dict, device_info=None) -> None:
        self._init_group(hass, manager, group, device_info)

    def _cover_ids(self) -> list[str]:
        return self._target_ids("cover")

    def _states_of(self, wanted: tuple[str, ...]) -> list[str]:
        out = []
        for entity_id in self._cover_ids():
            state = self._hass.states.get(entity_id)
            if state is not None and state.state in wanted:
                out.append(state.state)
        return out

    @property
    def supported_features(self) -> int:
        features = CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.STOP
        for entity_id in self._cover_ids():
            state = self._hass.states.get(entity_id)
            if state is not None and (state.attributes or {}).get("current_position") is not None:
                features |= CoverEntityFeature.SET_POSITION
                break
        return features

    @property
    def current_cover_position(self):
        values = []
        for entity_id in self._cover_ids():
            state = self._hass.states.get(entity_id)
            if state is None:
                continue
            value = (state.attributes or {}).get("current_position")
            if isinstance(value, (int, float)):
                values.append(float(value))
            elif entity_id.split(".", 1)[0] == "switch":
                # 开关式窗帘没有位置：按开/关折算 100 / 0
                values.append(100.0 if state.state == "on" else 0.0)
        return int(round(sum(values) / len(values))) if values else None

    @property
    def is_closed(self):
        if not self._cover_ids():
            return None
        return self._all_closed()

    def _all_closed(self) -> bool:
        for entity_id in self._cover_ids():
            state = self._hass.states.get(entity_id)
            if state is None:
                continue
            if state.state not in ("closed", "off", "unknown", "unavailable"):
                return False
        return True

    @property
    def is_opening(self) -> bool:
        return bool(self._states_of(("opening",)))

    @property
    def is_closing(self) -> bool:
        return bool(self._states_of(("closing",)))

    @property
    def extra_state_attributes(self) -> dict:
        attrs = self._base_attributes(self._summary())
        attrs["entities"] = self._cover_ids()
        return attrs

    async def _broadcast(self, service: str, data: dict) -> None:
        members = self._cover_ids()
        if not members:
            return
        try:
            # 成员可能是 switch（窗帘电机接开关）→ open/close 转成 switch.turn_on/off
            await self._call_members("cover", service, data, members)
            self._last_status = "success"
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "[entity_group] 编组 %s 调用 cover.%s 失败", self._group.get("id"), service
            )
            self._last_status = "failed"
            raise
        finally:
            self.async_write_ha_state()

    async def async_open_cover(self, **kwargs: Any) -> None:
        await self._broadcast("open_cover", {})

    async def async_close_cover(self, **kwargs: Any) -> None:
        await self._broadcast("close_cover", {})

    async def async_stop_cover(self, **kwargs: Any) -> None:
        await self._broadcast("stop_cover", {})

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        if kwargs.get(ATTR_POSITION) is None:
            return
        await self._broadcast("set_cover_position", {ATTR_POSITION: kwargs[ATTR_POSITION]})


# ── 编组 media_player（媒体组）─────────────────────────────────────────────
class EntityGroupMediaPlayer(_EntityGroupBase, MediaPlayerEntity):
    """媒体编组：成员全是 media_player（或 kind=media/media_player）时投影成媒体实体。

    状态：任一 playing → playing；否则任一 paused → paused；全 off → off，其余 idle。
    音量取平均，静音任一即静音；播放/暂停/停止/音量等全部广播。
    """

    entity_domain = "media_player"
    _default_icon = "mdi:speaker-multiple"

    def __init__(self, hass: HomeAssistant, manager: "EntityGroupManager",
                 group: dict, device_info=None) -> None:
        self._init_group(hass, manager, group, device_info)

    def _media_ids(self) -> list[str]:
        return self._member_ids("media_player")

    def _member_states(self) -> list[str]:
        out = []
        for entity_id in self._media_ids():
            state = self._hass.states.get(entity_id)
            out.append(state.state if state is not None else "unavailable")
        return out

    def _attr_of(self, entity_id: str, key: str, default=None):
        state = self._hass.states.get(entity_id)
        if state is None:
            return default
        return (state.attributes or {}).get(key, default)

    @property
    def supported_features(self) -> int:
        features = (
            MediaPlayerEntityFeature.PLAY
            | MediaPlayerEntityFeature.PAUSE
            | MediaPlayerEntityFeature.STOP
            | MediaPlayerEntityFeature.TURN_ON
            | MediaPlayerEntityFeature.TURN_OFF
            | MediaPlayerEntityFeature.VOLUME_SET
            | MediaPlayerEntityFeature.VOLUME_MUTE
        )
        return features

    @property
    def state(self) -> str:
        states = self._member_states()
        if not states:
            return MediaPlayerState.OFF
        if MediaPlayerState.PLAYING in states:
            return MediaPlayerState.PLAYING
        if MediaPlayerState.PAUSED in states:
            return MediaPlayerState.PAUSED
        if all(value in (MediaPlayerState.OFF, "unavailable", "unknown") for value in states):
            return MediaPlayerState.OFF
        return MediaPlayerState.IDLE

    @property
    def volume_level(self):
        values = [
            float(value) for value in (
                self._attr_of(entity_id, "volume_level") for entity_id in self._media_ids()
            ) if isinstance(value, (int, float))
        ]
        return round(sum(values) / len(values), 3) if values else None

    @property
    def is_volume_muted(self):
        for entity_id in self._media_ids():
            if self._attr_of(entity_id, "is_volume_muted"):
                return True
        return None

    @property
    def media_title(self):
        for entity_id in self._media_ids():
            value = self._attr_of(entity_id, "media_title")
            if value:
                return value
        return None

    @property
    def app_name(self):
        for entity_id in self._media_ids():
            value = self._attr_of(entity_id, "app_name")
            if value:
                return value
        return None

    @property
    def source_list(self):
        out: list[str] = []
        for entity_id in self._media_ids():
            values = self._attr_of(entity_id, "source_list")
            if not isinstance(values, (list, tuple)):
                continue
            for value in values:
                if value not in out:
                    out.append(value)
        return out or None

    @property
    def extra_state_attributes(self) -> dict:
        attrs = self._base_attributes(self._summary())
        attrs["entities"] = self._media_ids()
        return attrs

    async def _broadcast(self, service: str, data: dict) -> None:
        members = self._media_ids()
        if not members:
            return
        try:
            await self._hass.services.async_call(
                "media_player", service, {"entity_id": members, **data}, blocking=True
            )
            self._last_status = "success"
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "[entity_group] 编组 %s 调用 media_player.%s 失败",
                self._group.get("id"), service,
            )
            self._last_status = "failed"
            raise
        finally:
            self.async_write_ha_state()

    async def async_turn_on(self) -> None:
        await self._broadcast("turn_on", {})

    async def async_turn_off(self) -> None:
        await self._broadcast("turn_off", {})

    async def async_media_play(self) -> None:
        await self._broadcast("media_play", {})

    async def async_media_pause(self) -> None:
        await self._broadcast("media_pause", {})

    async def async_media_stop(self) -> None:
        await self._broadcast("media_stop", {})

    async def async_set_volume_level(self, volume: float) -> None:
        await self._broadcast("volume_set", {"volume_level": volume})

    async def async_mute_volume(self, mute: bool) -> None:
        await self._broadcast("volume_mute", {"is_volume_muted": mute})

    async def async_media_next_track(self) -> None:
        await self._broadcast("media_next_track", {})

    async def async_media_previous_track(self) -> None:
        await self._broadcast("media_previous_track", {})


# 域 → 编组实体类（按 group_entity_domain() 的结果分派）
_GROUP_ENTITY_CLASSES = {
    "switch": EntityGroupSwitch,
    "light": EntityGroupLight,
    "cover": EntityGroupCover,
    "media_player": EntityGroupMediaPlayer,
    "climate": EntityGroupClimate,
}


# ── 管理器 ──────────────────────────────────────────────────────────────────
class EntityGroupManager:
    """持有快照 + 动态增删 switch + 成员状态监听。"""

    def __init__(self, hass: HomeAssistant, db_path: str) -> None:
        self.hass = hass
        self.store = EntityGroupStore(db_path)
        self.sensor: EntityGroupsSensor | None = None
        self.group_entities_sensor: EntityGroupEntitiesSensor | None = None
        # gid → 编组实体（switch / light / cover / media_player / climate 之一）
        self._switches: dict[str, Any] = {}
        # domain → 该平台的 async_add_entities 回调
        self._adders: dict[str, Any] = {}
        self._device_info: Any = None
        self._snapshot: dict = {"groups": [], "version": 0, "updated_at": 0}
        self._entity_to_groups: dict[str, set[str]] = {}
        self._lock = asyncio.Lock()
        self._unsub = None
        self._dirty: set[str] = set()
        self._flush_scheduled = False

    # -- 装配 ---------------------------------------------------------------
    def attach_adder(self, domain: str, async_add_entities, device_info=None) -> None:
        """登记某个平台的 async_add_entities：该域的编组以后都走它创建。"""
        self._adders[domain] = async_add_entities
        if device_info is not None:
            self._device_info = device_info

    def attach_switch_adder(self, async_add_entities, device_info) -> None:
        self.attach_adder("switch", async_add_entities, device_info)

    def attach_climate_adder(self, async_add_entities, device_info=None) -> None:
        """climate 平台回调：ac 编组要用它建 climate 实体。"""
        self.attach_adder("climate", async_add_entities, device_info)

    def attach_sensor(self, sensor: EntityGroupsSensor) -> None:
        self.sensor = sensor

    def attach_group_entities_sensor(self, sensor: EntityGroupEntitiesSensor) -> None:
        self.group_entities_sensor = sensor

    async def async_init(self) -> None:
        await self.async_reload()
        if self._unsub is None:
            self._unsub = self.hass.bus.async_listen(
                EVENT_STATE_CHANGED, self._on_state_changed
            )

    def async_shutdown(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None

    # -- 同步 ---------------------------------------------------------------
    async def async_reload(self) -> dict:
        """重读快照 → 更新 sensor → 增量同步编组实体（switch / climate）。"""
        async with self._lock:
            # 自愈：平台回调可能在传感器平台之后才注册（或曾注册失败），
            # 每次同步都尝试从 hass.data 取一次，避免"永远拿不到回调 → 永远不建实体"。
            data = self.hass.data.get(DOMAIN, {})
            for domain in GROUP_ENTITY_DOMAINS:
                if not self._adders.get(domain):
                    adder = data.get(f"async_add_{domain}")
                    if adder is not None:
                        self._adders[domain] = adder

            snapshot = await self.hass.async_add_executor_job(
                self.store.snapshot, True
            )
            self._snapshot = snapshot
            self._rebuild_index()

            await self._sync_switches(snapshot.get("groups") or [])

            if self.sensor is not None:
                self.sensor.apply_snapshot(snapshot, self.debug_info())

            if self.group_entities_sensor is not None:
                self.group_entities_sensor.apply_snapshot(snapshot)

            return snapshot

    def debug_info(self) -> dict:
        """给目录 sensor 用的自检信息（排查"为什么没生成实体"）。"""
        by_domain: dict[str, int] = {}
        for entity in self._switches.values():
            domain = getattr(entity, "entity_domain", "switch")
            by_domain[domain] = by_domain.get(domain, 0) + 1
        return {
            "groups_in_snapshot": len(self._snapshot.get("groups") or []),
            "switches": by_domain.get("switch", 0),
            "climates": by_domain.get("climate", 0),
            "lights": by_domain.get("light", 0),
            "covers": by_domain.get("cover", 0),
            "media_players": by_domain.get("media_player", 0),
            "adder_ready": bool(self._adders.get("switch")),
            "climate_adder_ready": bool(self._adders.get("climate")),
            "light_adder_ready": bool(self._adders.get("light")),
            "cover_adder_ready": bool(self._adders.get("cover")),
            "media_adder_ready": bool(self._adders.get("media_player")),
        }

    def _rebuild_index(self) -> None:
        index: dict[str, set[str]] = {}
        for group in self._snapshot.get("groups") or []:
            gid = group.get("id")
            for member in group.get("members") or []:
                entity_id = member.get("entity_id")
                if entity_id and gid:
                    index.setdefault(entity_id, set()).add(gid)
        self._entity_to_groups = index

    def _adder_for(self, domain: str):
        return self._adders.get(domain)

    async def _sync_switches(self, groups: list[dict]) -> None:
        wanted = {g["id"]: g for g in groups if g.get("id")}

        # 1) 删除已消失 / 已停用 / **实体域变了**（如 kind 改成 ac → switch 换 climate）的编组
        for gid in list(self._switches):
            entity = self._switches[gid]
            if gid in wanted:
                domain = group_entity_domain(wanted[gid])
                if domain == getattr(entity, "entity_domain", "switch"):
                    continue
                _LOGGER.info(
                    "[entity_group] 编组 %s 实体域变化（%s → %s），重建实体",
                    gid, getattr(entity, "entity_domain", "switch"), domain,
                )
            self._switches.pop(gid, None)
            await self._async_remove_group_entity(gid, entity)

        # 2) 已有编组：只更新壳字段
        for gid, entity in self._switches.items():
            entity.apply_group(wanted[gid])

        # 3) 新增编组（按域分派到 switch / light / cover / media_player / climate 平台）
        new_by_domain: dict[str, list] = {}
        for gid, group in wanted.items():
            if gid in self._switches:
                continue
            domain = group_entity_domain(group)
            if self._adder_for(domain) is None:
                _LOGGER.warning(
                    "[entity_group] %s 平台回调未就绪，编组 %s 暂不建实体（下次同步会重试）",
                    domain, gid,
                )
                continue
            entity_cls = _GROUP_ENTITY_CLASSES.get(domain, EntityGroupSwitch)
            entity = entity_cls(self.hass, self, group)
            self._switches[gid] = entity
            new_by_domain.setdefault(domain, []).append(entity)
        for domain, entities in new_by_domain.items():
            self._adder_for(domain)(entities)
            _LOGGER.info(
                "[entity_group] 新增编组 %s %d 个（编组设备）：%s",
                domain, len(entities), [e.entity_id for e in entities],
            )

        # 4) 清理历史残留：老版本删除编组时只 async_remove、没删实体注册表，
        #    于是实体列表里会永久留一串「实体不可用」。这里按编组集合做一次幂等清理，
        #    用户升级后不必手删（每次编组变更都会再跑一次）。
        self._prune_stale_registry(wanted)

    # -- 实体移除：状态 + 实体注册表 ----------------------------------------
    def _registry(self):
        """实体注册表（极早期未就绪时返回 None）。"""
        try:
            return er.async_get(self.hass)
        except Exception:  # noqa: BLE001
            return None

    def _registry_entries_for(self, gid: str, entity_id: str | None) -> list:
        """找出某个编组实体在注册表里的条目（entity_id + unique_id 双路匹配）。"""
        registry = self._registry()
        if registry is None:
            return []
        found: list = []
        if entity_id:
            entry = registry.async_get(entity_id)
            if entry is not None:
                found.append(entry)
        unique_id = f"{DOMAIN}_{gid}"
        for entry in list(registry.entities.values()):
            if entry.unique_id != unique_id:
                continue
            if any(item.entity_id == entry.entity_id for item in found):
                continue
            found.append(entry)
        return found

    async def _async_remove_group_entity(self, gid: str, entity: Any) -> None:
        """删编组时把实体「彻底」删掉：撤状态 + 删实体注册表条目。

        只 ``async_remove(force_remove=True)`` 的话，实体注册表里的条目会保留
        （编组实体都有 unique_id），HA 会把它当成"已注册但集成不再提供"的实体，
        在实体列表里显示为「实体不可用」并永久驻留 —— 这就是删完编组还能看到实体的原因。
        """
        entity_id = getattr(entity, "entity_id", None)
        # 先取注册表条目：async_remove 之后实体上的相关字段可能已被清空
        entries = self._registry_entries_for(gid, entity_id)
        # 还没挂到 hass 的实体（平台回调未就绪的极端情况）只需删注册表
        if getattr(entity, "hass", None) is not None:
            try:
                await entity.async_remove(force_remove=True)
            except Exception:  # noqa: BLE001
                _LOGGER.debug("[entity_group] 移除编组实体失败: %s", gid)
        self._remove_registry_entries(gid, entries)

    def _remove_registry_entries(self, gid: str, entries: list) -> None:
        """把注册表条目删干净（条目不存在时是幂等的 no-op）。"""
        if not entries:
            return
        registry = self._registry()
        if registry is None:
            return
        for entry in entries:
            # async_remove(force_remove=True) 在部分 HA 版本里已经删过条目，
            # 直接再删会抛 KeyError —— 先确认还在
            if registry.async_get(entry.entity_id) is None:
                continue
            try:
                registry.async_remove(entry.entity_id)
                _LOGGER.info(
                    "[entity_group] 已删除编组 %s 的实体 %s（unique_id=%s）",
                    gid, entry.entity_id, entry.unique_id,
                )
            except Exception:  # noqa: BLE001
                _LOGGER.debug(
                    "[entity_group] 删除实体注册表条目失败: %s", entry.entity_id
                )

    def _prune_stale_registry(self, wanted: dict[str, dict]) -> None:
        """清理「编组已不存在、注册表条目还在」的残留实体（幂等，可反复跑）。

        老版本删除编组只撤状态不删注册表，用户实体列表里会留一串「实体不可用」；
        这里按当前编组集合清一遍，升级后无需手工删除。
        只动本集成创建的编组实体：``unique_id`` 前缀 ``ha_data_store_eg_``
        （编组 id 一律以 ``eg_`` 开头，不会误伤本集成的其它实体）。
        """
        registry = self._registry()
        if registry is None:
            return
        prefix = f"{DOMAIN}_eg_"
        id_prefix = f"{DOMAIN}_"
        removed: list[str] = []
        for entry in list(registry.entities.values()):
            unique_id = entry.unique_id or ""
            if not unique_id.startswith(prefix):
                continue
            if entry.platform != DOMAIN:
                continue
            if unique_id[len(id_prefix):] in wanted:
                continue
            try:
                registry.async_remove(entry.entity_id)
                removed.append(entry.entity_id)
            except Exception:  # noqa: BLE001
                _LOGGER.debug(
                    "[entity_group] 清理残留编组实体失败: %s", entry.entity_id
                )
        if removed:
            _LOGGER.info(
                "[entity_group] 清理 %d 个残留编组实体（编组已删除）：%s",
                len(removed), removed,
            )

    # -- 成员状态变化：只刷新受影响的 switch --------------------------------
    @callback
    def _on_state_changed(self, event) -> None:
        entity_id = (event.data or {}).get("entity_id")
        if not entity_id:
            return
        gids = self._entity_to_groups.get(entity_id)
        if not gids:
            return
        self._dirty.update(gids)
        if not self._flush_scheduled:
            self._flush_scheduled = True
            self.hass.async_create_task(self._flush_dirty())

    async def _flush_dirty(self) -> None:
        try:
            await asyncio.sleep(0)
            dirty, self._dirty = self._dirty, set()
            for gid in dirty:
                entity = self._switches.get(gid)
                if entity is not None and entity.hass is not None:
                    entity.async_write_ha_state()
        finally:
            self._flush_scheduled = False


# ── 模块级入口 ──────────────────────────────────────────────────────────────
def get_manager(hass: HomeAssistant, db_path: str | None = None) -> EntityGroupManager:
    """取（或创建）编组管理器，挂在 ``hass.data[DOMAIN]["entity_group_manager"]``。"""
    store_data = hass.data.setdefault(DOMAIN, {})
    manager = store_data.get("entity_group_manager")
    if manager is None:
        path = db_path or store_data.get("db_path")
        if not path:
            raise RuntimeError("db_path 缺失，编组管理器无法初始化")
        manager = EntityGroupManager(hass, path)
        store_data["entity_group_manager"] = manager
    return manager


async def async_setup_entity_group_sensor(
    hass: HomeAssistant, device_info=None,
) -> tuple[EntityGroupsSensor, EntityGroupEntitiesSensor]:
    """sensor 平台调用：造两个 sensor 并挂到管理器（设备固定为「编组设备」）。

    - ``sensor.ha_data_store_entity_groups``：编组目录（全量快照）
    - ``sensor.ha_data_store_group_entities``：「所有编组实体」（只留 icon/name）
    """
    manager = get_manager(hass)
    sensor = EntityGroupsSensor(hass)
    manager.attach_sensor(sensor)
    group_entities_sensor = EntityGroupEntitiesSensor(hass)
    manager.attach_group_entities_sensor(group_entities_sensor)
    await manager.async_init()
    return sensor, group_entities_sensor


async def async_setup_entity_group_switches(
    hass: HomeAssistant, async_add_entities=None,
) -> list[EntityGroupSwitch]:
    """switch 平台调用：注册动态添加回调，并按当前快照创建已有编组的 switch。

    显式传入平台给的 ``async_add_entities`` 最可靠；拿不到时回退到
    ``hass.data[DOMAIN]["async_add_switch"]``（虚拟设备/桥接也用它）。
    """
    manager = get_manager(hass)
    adder = async_add_entities if callable(async_add_entities) else None
    if adder is None:
        adder = hass.data.get(DOMAIN, {}).get("async_add_switch")
    manager.attach_switch_adder(adder, entity_group_device_info())
    await manager.async_init()
    _LOGGER.info(
        "[entity_group] switch 平台就绪（回调=%s，已建编组实体 %d 个）",
        bool(adder), len(manager._switches),
    )
    return []


async def async_setup_entity_group_platform(
    hass: HomeAssistant, domain: str, async_add_entities=None,
) -> list:
    """通用入口：某平台就绪 → 登记回调 → 立即同步一次（该域编组补建/换域重建）。

    ``domain`` 取 ``switch`` / ``light`` / ``cover`` / ``media_player`` / ``climate``，
    对应 ``async_setup_entity_group_switches / _lights / _covers / _media_players / _climates``。
    """
    manager = get_manager(hass)
    adder = async_add_entities if callable(async_add_entities) else None
    if adder is None:
        adder = hass.data.get(DOMAIN, {}).get(f"async_add_{domain}")
    manager.attach_adder(domain, adder, entity_group_device_info())
    await manager.async_init()
    _LOGGER.info(
        "[entity_group] %s 平台就绪（回调=%s，已建编组实体 %d 个）",
        domain, bool(adder), len(manager._switches),
    )
    return []


async def async_setup_entity_group_climates(
    hass: HomeAssistant, async_add_entities=None,
) -> list:
    """climate 平台调用：空调编组（kind=ac / 成员全 climate）建成 climate 实体。"""
    return await async_setup_entity_group_platform(hass, "climate", async_add_entities)


async def async_setup_entity_group_lights(
    hass: HomeAssistant, async_add_entities=None,
) -> list:
    """light 平台调用：灯光编组（成员全 light / kind=lights）建成 light 实体。"""
    return await async_setup_entity_group_platform(hass, "light", async_add_entities)


async def async_setup_entity_group_covers(
    hass: HomeAssistant, async_add_entities=None,
) -> list:
    """cover 平台调用：窗帘编组（成员全 cover / kind=curtain）建成 cover 实体。"""
    return await async_setup_entity_group_platform(hass, "cover", async_add_entities)


async def async_setup_entity_group_media_players(
    hass: HomeAssistant, async_add_entities=None,
) -> list:
    """media_player 平台调用：媒体编组（成员全 media_player / kind=media）建成媒体实体。"""
    return await async_setup_entity_group_platform(hass, "media_player", async_add_entities)


@callback
def async_notify_groups_changed(hass: HomeAssistant) -> None:
    """写接口成功后调用：后台重读快照并同步实体（不阻塞 HTTP 响应）。"""
    data = hass.data.get(DOMAIN, {})
    manager = data.get("entity_group_manager")
    if manager is None:
        # 平台还没跑到（或曾初始化失败）：这里补建，保证"写完就有实体"
        try:
            manager = get_manager(hass)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("[entity_group] 管理器创建失败，跳过同步", exc_info=True)
            return
        hass.async_create_task(manager.async_init())
        return
    hass.async_create_task(manager.async_reload())
