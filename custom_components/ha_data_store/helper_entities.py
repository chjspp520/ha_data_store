"""辅助元素模块 — 把原生 HA 辅助元素(helper)导入为本集成自管的实体。

方案 Y 目标域映射（entity_id 前缀会变化，后段保留原名）：
  input_boolean  → switch
  input_number   → number      (保留 min/max/step/unit)
  counter        → number      (保留计数当前值 + min/max/step/initial)
  input_select   → select      (保留 options 与当前选项)
  input_button   → button      (无状态，仅配置)
  input_text     → text        (保留文本值)
  binary_sensor  → binary_sensor

所有实体使用 RestoreEntity，导入后状态写回会进入 HA restore_state，重启自动恢复。
实体通过平台 setup 时存储的 async_add_<domain> 回调动态创建，无需重启。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.button import ButtonEntity
from homeassistant.components.number import NumberEntity
from homeassistant.components.select import SelectEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.restore_state import RestoreEntity

try:
    from homeassistant.components.text import TextEntity as _TextEntity
except Exception:  # pragma: no cover - 老版本可能无 text 组件
    _TextEntity = None

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

HELPER_GROUP_ID = "helper_entities"


# =========================================================================== #
#  辅助元素目标实体类                                                                #
# =========================================================================== #
def _helper_device_info() -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, HELPER_GROUP_ID)},
        name="辅助元素",
        manufacturer="HA数据存储 — 辅助元素",
    )


class HelperSwitch(SwitchEntity, RestoreEntity):
    """由原生 input_boolean 转换而来（域 switch）。"""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entity_id: str, name: str, icon: str | None = None,
                 initial_on: bool = False) -> None:
        super().__init__()
        self._attr_unique_id = f"{DOMAIN}_helper_{entity_id}"
        self._attr_name = name
        self._attr_device_info = _helper_device_info()
        if icon:
            self._attr_icon = icon
        self._attr_is_on = bool(initial_on)
        self.entity_id = entity_id

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last and last.state is not None:
            self._attr_is_on = last.state == "on"

    async def async_turn_on(self, **kwargs) -> None:
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        self._attr_is_on = False
        self.async_write_ha_state()


class HelperBinarySensor(BinarySensorEntity, RestoreEntity):
    """原生 binary_sensor 转换而来（域 binary_sensor）。"""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entity_id: str, name: str, icon: str | None = None,
                 initial_on: bool = False) -> None:
        super().__init__()
        self._attr_unique_id = f"{DOMAIN}_helper_{entity_id}"
        self._attr_name = name
        self._attr_device_info = _helper_device_info()
        if icon:
            self._attr_icon = icon
        self._attr_is_on = bool(initial_on)
        self.entity_id = entity_id

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last and last.state is not None:
            self._attr_is_on = last.state == "on"


class HelperNumber(NumberEntity, RestoreEntity):
    """原生 input_number / counter 转换而来（域 number）。"""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entity_id: str, name: str, icon: str | None = None,
                 min_val: float | None = None, max_val: float | None = None,
                 step: float | None = None, unit: str | None = None,
                 value: float | None = None) -> None:
        super().__init__()
        self._attr_unique_id = f"{DOMAIN}_helper_{entity_id}"
        self._attr_name = name
        self._attr_device_info = _helper_device_info()
        if icon:
            self._attr_icon = icon
        self._attr_native_min_value = float(min_val) if min_val is not None else 0.0
        self._attr_native_max_value = float(max_val) if max_val is not None else 100.0
        self._attr_native_step = float(step) if step is not None else 1.0
        if unit:
            self._attr_native_unit_of_measurement = unit
        raw_value = float(value) if value is not None else self._attr_native_min_value
        # 约束到 [min, max]，避免超出范围告警
        self._attr_native_value = max(
            self._attr_native_min_value,
            min(self._attr_native_max_value, raw_value),
        )
        self.entity_id = entity_id

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last and last.state is not None:
            try:
                self._attr_native_value = float(last.state)
            except (TypeError, ValueError):
                pass

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = value
        self.async_write_ha_state()

    @property
    def native_value(self) -> float:
        return self._attr_native_value


class HelperSelect(SelectEntity, RestoreEntity):
    """原生 input_select 转换而来（域 select）。"""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entity_id: str, name: str, icon: str | None = None,
                 options: list[str] | None = None,
                 current: str | None = None) -> None:
        super().__init__()
        self._attr_unique_id = f"{DOMAIN}_helper_{entity_id}"
        self._attr_name = name
        self._attr_device_info = _helper_device_info()
        if icon:
            self._attr_icon = icon
        opts = options or ["选项1"]
        if isinstance(opts, str):
            opts = [s.strip() for s in opts.split(",") if s.strip()] or ["选项1"]
        elif not isinstance(opts, list):
            opts = [str(o) for o in opts]
        self._attr_options = opts
        self._attr_current_option = current if current in opts else opts[0]
        self.entity_id = entity_id

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last and last.state is not None and last.state in self._attr_options:
            self._attr_current_option = last.state

    async def async_select_option(self, option: str) -> None:
        self._attr_current_option = option
        self.async_write_ha_state()

    @property
    def current_option(self) -> str | None:
        return self._attr_current_option


class HelperButton(ButtonEntity):
    """原生 input_button 转换而来（域 button，无状态仅配置）。"""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entity_id: str, name: str, icon: str | None = None) -> None:
        super().__init__()
        self._attr_unique_id = f"{DOMAIN}_helper_{entity_id}"
        self._attr_name = name
        self._attr_device_info = _helper_device_info()
        if icon:
            self._attr_icon = icon
        self.entity_id = entity_id

    async def async_press(self) -> None:
        _LOGGER.info("[helper] 按钮 %s 被按下（无绑定动作）", self.entity_id)


if _TextEntity is not None:

    class HelperText(_TextEntity, RestoreEntity):
        """原生 input_text 转换而来（域 text）。"""

        _attr_has_entity_name = True
        _attr_should_poll = False

        def __init__(self, entity_id: str, name: str, icon: str | None = None,
                     value: str | None = None) -> None:
            super().__init__()
            self._attr_unique_id = f"{DOMAIN}_helper_{entity_id}"
            self._attr_name = name
            self._attr_device_info = _helper_device_info()
            if icon:
                self._attr_icon = icon
            self._attr_native_value = value if value is not None else ""
            self.entity_id = entity_id

        async def async_added_to_hass(self) -> None:
            await super().async_added_to_hass()
            last = await self.async_get_last_state()
            if last and last.state not in (None, "unavailable", "unknown"):
                self._attr_native_value = last.state

        async def async_set_value(self, value: str) -> None:
            self._attr_native_value = value
            self.async_write_ha_state()

        @property
        def native_value(self) -> str:
            return self._attr_native_value

else:  # pragma: no cover - 老版本无 text 组件
    HelperText = None


# =========================================================================== #
#  类型映射                                                                       #
# =========================================================================== #
HELPER_DOMAIN_MAP: dict[str, str] = {
    "input_boolean": "switch",
    "input_number": "number",
    "counter": "number",
    "input_select": "select",
    "input_button": "button",
    "input_text": "text",
    "binary_sensor": "binary_sensor",
}

# 扫描候选域（不含 binary_sensor —— 避免把真实二进制传感器当辅助元素误导出）
SCAN_DOMAINS = ["input_boolean", "input_number", "counter",
                "input_select", "input_button", "input_text"]


def _target_entity_id(source_type: str, source_entity_id: str) -> str:
    """由源域类型 + 源实体 ID 计算目标 entity_id（替换前缀）。"""
    obj = source_entity_id.split(".", 1)[1] if "." in source_entity_id else source_entity_id
    return f"{HELPER_DOMAIN_MAP.get(source_type, source_type)}.{obj}"


# =========================================================================== #
#  辅助元素管理器                                                                  #
# =========================================================================== #
class HelperManager:
    """管理辅助元素实体的动态创建、删除、导入、导出与启动恢复。"""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._db_path = hass.data.get(DOMAIN, {}).get("db_path", "")

    # ---------- 内部追踪 ---------- #
    def _find_inmem_item(self, entity_id: str) -> dict | None:
        items = self._hass.data.get(DOMAIN, {}).get("helper_entities", [])
        for item in items:
            if item["entity_id"] == entity_id:
                return item
        return None

    # ---------- 构建实体 ---------- #
    def _build_entity(self, config: dict):
        """根据配置构建目标域实体实例。"""
        eid = config["entity_id"]
        name = config.get("device_name", eid)
        icon = config.get("icon")
        source_type = config.get("source_type", "")
        domain = HELPER_DOMAIN_MAP.get(source_type)
        if source_type in ("input_boolean",):
            return domain, HelperSwitch(eid, name, icon,
                                        initial_on=bool(config.get("initial_on", False)))
        if source_type == "binary_sensor":
            return domain, HelperBinarySensor(eid, name, icon,
                                              initial_on=bool(config.get("initial_on", False)))
        if source_type in ("input_number", "counter"):
            value = config.get("value")
            return domain, HelperNumber(
                eid, name, icon,
                min_val=config.get("min"), max_val=config.get("max"),
                step=config.get("step"), unit=config.get("unit"),
                value=value,
            )
        if source_type == "input_select":
            return domain, HelperSelect(
                eid, name, icon,
                options=config.get("options"),
                current=config.get("current_option"),
            )
        if source_type == "input_button":
            return domain, HelperButton(eid, name, icon)
        if source_type == "input_text":
            if HelperText is None:
                raise ValueError("当前 HA 版本不支持 text 组件，无法导入 input_text")
            return domain, HelperText(eid, name, icon, config.get("value"))
        raise ValueError(f"不支持的辅助元素源类型: {source_type}")

    # ---------- 创建（注册实体 + 落库 + 追踪） ---------- #
    def create_helper(self, config: dict) -> dict:
        domain, ent = self._build_entity(config)
        add_cb = self._hass.data.get(DOMAIN, {}).get(f"async_add_{domain}")
        if not add_cb:
            raise ValueError(f"域 {domain} 平台未就绪")
        add_cb([ent])
        self._save_to_db(config)
        self._hass.data.setdefault(DOMAIN, {}).setdefault("helper_entities", []).append({
            "entity_id": config["entity_id"],
            "source_type": config.get("source_type", ""),
            "source_entity_id": config.get("source_entity_id", ""),
            "device_name": config.get("device_name", ""),
            "entity_count": 1,
            "entities": [ent],
        })
        _LOGGER.info("[helper] 创建辅助元素 %s (%s→%s)",
                     config["entity_id"], config.get("source_type", ""), domain)
        return {"entity_id": config["entity_id"], "entity": ent}

    # ---------- 落库 ---------- #
    def _save_to_db(self, config: dict) -> None:
        if not self._db_path:
            return
        import sqlite3
        from datetime import datetime
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        store_keys = ("source_type", "source_entity_id", "device_name", "icon",
                      "min", "max", "step", "unit", "value",
                      "options", "current_option", "initial_on")
        extra = {k: config.get(k) for k in store_keys if config.get(k) is not None}
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO helper_entities "
                "(entity_id, source_type, source_entity_id, device_name, icon, extra_config, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (config["entity_id"], config.get("source_type", ""),
                 config.get("source_entity_id", ""),
                 config.get("device_name", ""), config.get("icon", ""),
                 json.dumps(extra, ensure_ascii=False), now),
            )
            conn.commit()
        finally:
            conn.close()

    def _remove_from_db(self, entity_id: str) -> None:
        if not self._db_path:
            return
        import sqlite3
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute("DELETE FROM helper_entities WHERE entity_id = ?", (entity_id,))
            conn.commit()
        finally:
            conn.close()

    # ---------- 读取 ---------- #
    def load_from_db(self) -> list[dict]:
        """从 DB 读出全部辅助元素配置（用于启动恢复/再次导出）。"""
        if not self._db_path:
            return []
        import sqlite3
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute("SELECT * FROM helper_entities").fetchall()
            result = []
            for row in rows:
                config = {
                    "source_type": row["source_type"],
                    "source_entity_id": row["source_entity_id"],
                    "entity_id": row["entity_id"],
                    "device_name": row["device_name"] or row["entity_id"],
                    "icon": row["icon"] or None,
                }
                try:
                    extra = json.loads(row["extra_config"] or "{}")
                    config.update(extra)
                except (json.JSONDecodeError, TypeError):
                    pass
                result.append(config)
            return result
        finally:
            conn.close()

    def list_helpers(self) -> list[dict]:
        """运行中的辅助元素列表（内存追踪）。"""
        return self._hass.data.get(DOMAIN, {}).get("helper_entities", [])

    # ---------- 删除 ---------- #
    def delete_helper(self, entity_id: str) -> bool:
        items = self.list_helpers()
        target = None
        for item in items:
            if item["entity_id"] == entity_id:
                target = item
                break
        if not target:
            return False
        from homeassistant.helpers import entity_registry as er
        reg = er.async_get(self._hass)
        for ent in target.get("entities") or []:
            eid = getattr(ent, "entity_id", None) or getattr(ent, "_attr_entity_id", None)
            if eid:
                try:
                    reg.async_remove(eid)
                except Exception:
                    pass
                self._hass.states.async_remove(eid)
        items.remove(target)
        self._remove_from_db(entity_id)
        _LOGGER.info("[helper] 删除辅助元素 %s", entity_id)
        return True

    # ---------- 导出（已创建项，配置 + 状态快照） ---------- #
    async def async_export_items(self) -> dict:
        saved = await self._hass.async_add_executor_job(self.load_from_db)
        items = []
        for cfg in saved:
            eid = cfg["entity_id"]
            item = self._find_inmem_item(eid)
            snapshot: dict = {}
            for ent in (item or {}).get("entities") or []:
                ent_id = getattr(ent, "entity_id", None)
                if not ent_id:
                    continue
                st = self._hass.states.get(ent_id)
                if st is None:
                    continue
                snapshot[ent_id] = {"state": st.state, "attributes": dict(st.attributes)}
            items.append({"config": cfg, "state_snapshot": snapshot})
        return {"schema_version": 1, "items": items}

    # ---------- 导入 ---------- #
    async def async_import_items(self, payload: dict | None, mode: str = "skip") -> dict:
        """导入辅助元素 item 列表（配置 + 可选状态快照），自动建实体。

        mode: skip 默认（目标 entity_id 已存在则跳过）；
              overwrite（目标为本集成已创建的辅助元素/虚拟设备则删旧重建，
              其它来源的同名实体不可覆盖，记录冲突）。
        """
        items = (payload or {}).get("items")
        if not isinstance(items, list):
            raise ValueError("payload.items 必须为数组")
        if mode not in ("skip", "overwrite"):
            raise ValueError(f"mode 仅支持 skip/overwrite，收到: {mode!r}")

        result: dict = {"imported": 0, "skipped": 0, "conflicted": 0, "failed": []}
        pending_apply: list[tuple[Any, dict]] = []

        # 运行中的虚拟设备 entity_id（helper 与 virtual 可能撞名）
        running_virtual = {
            d["entity_id"] for d in self._hass.data.get(DOMAIN, {}).get("virtual_devices", [])
        }

        for item in items:
            cfg = ((item or {}).get("config")) or {}
            snap = ((item or {}).get("state_snapshot")) or {}
            if not isinstance(cfg, dict) or not isinstance(snap, dict):
                snap = {}
            src_type = cfg.get("source_type") or ""
            src_eid = cfg.get("source_entity_id") or ""
            eid = cfg.get("entity_id") or _target_entity_id(src_type, src_eid)
            try:
                if not src_type or not src_eid:
                    raise ValueError("缺少 source_type / source_entity_id")
                if HELPER_DOMAIN_MAP.get(src_type) is None:
                    raise ValueError(f"不支持的源类型: {src_type}")

                exists_inmem = self._find_inmem_item(eid) is not None
                if exists_inmem:
                    if mode == "skip":
                        result["skipped"] += 1
                        continue
                    # overwrite：仅可覆盖本集成创建的辅助元素
                    self.delete_helper(eid)
                elif mode == "overwrite" and eid in running_virtual:
                    # 目标是本集成已有虚拟设备 → 可以删旧重建
                    from .virtual_devices import VirtualDeviceManager
                    vm = VirtualDeviceManager(self._hass, self._entry_id)
                    vm.delete_device(eid)
                elif mode == "overwrite" and self._hass.states.get(eid) is not None:
                    # 其它来源（真实实体/桥接/别的集成）已占用，不可覆盖
                    result["conflicted"] += 1
                    continue
                elif self._hass.states.get(eid) is not None:
                    # skip 模式下已有任意同名实体都算跳过
                    result["skipped"] += 1
                    continue

                cfg = dict(cfg)
                cfg["entity_id"] = eid
                created = self.create_helper(cfg)
                result["imported"] += 1
                if snap:
                    ent = created.get("entity")
                    if ent is not None and getattr(ent, "entity_id", None) in snap:
                        pending_apply.append((ent, snap[ent.entity_id]))
            except Exception as exc:
                result["failed"].append({"entity_id": eid, "error": str(exc)})
                _LOGGER.warning("[helper] 导入辅助元素失败 %s: %s", eid, exc)

        if pending_apply:
            self._hass.async_create_task(self._flush_snapshots(pending_apply))
        return result

    async def _flush_snapshots(self, pending_apply: list[tuple[Any, dict]]) -> None:
        """实体注册完成后，把导入的状态快照写回实体并刷到 HA。"""
        for ent, snap in pending_apply:
            ready = False
            for _ in range(50):  # 最多等待约 5 秒
                if getattr(ent, "hass", None) is not None:
                    ready = True
                    break
                await asyncio.sleep(0.1)
            if not ready:
                _LOGGER.warning("[helper] 导入状态超时（实体未注册，跳过状态回填）: %s",
                                getattr(ent, "entity_id", "?"))
                continue
            try:
                _apply_helper_snapshot(ent, snap)
                ent.async_write_ha_state()
            except Exception:
                _LOGGER.warning("[helper] 导入状态写回失败: %s",
                                getattr(ent, "entity_id", "?"), exc_info=True)


# =========================================================================== #
#  扫描 A 机原生 helper → 导出 item                                             #
# =========================================================================== #
async def async_scan_native_helpers(hass: HomeAssistant,
                                    include_binary_sensor: bool = False) -> list[dict]:
    """扫描当前系统中原生辅助元素(helper)实体，返回可导出 item 列表。

    每项: {"config": {...}, "state_snapshot": {target_eid: {state, attributes}}}
    参数从实体 attributes 提取（min/max/step/options/icon/friendly_name 等）。
    """
    domains = list(SCAN_DOMAINS)
    if include_binary_sensor:
        domains.append("binary_sensor")

    items: list[dict] = []
    for state in hass.states.async_all():
        domain = state.domain
        if domain not in domains:
            continue
        attrs = dict(state.attributes or {})
        obj_id = state.entity_id.split(".", 1)[1]
        name = attrs.get("friendly_name") or obj_id
        icon = attrs.get("icon")
        src_eid = state.entity_id
        src_type = domain

        config: dict = {
            "source_type": src_type,
            "source_entity_id": src_eid,
            "device_name": name,
            "icon": icon or None,
        }
        # 各类型专属参数
        if domain in ("input_number", "counter"):
            config["min"] = attrs.get("min")
            config["max"] = attrs.get("max")
            config["step"] = attrs.get("step")
            if domain == "counter":
                # counter 无 min/max 时给宽松默认（number 域要求 min<=max）
                if config["min"] is None:
                    config["min"] = 0
                if config["max"] is None:
                    config["max"] = 999999999
                if config["step"] is None:
                    config["step"] = 1
        elif domain == "input_select":
            options = attrs.get("options")
            config["options"] = options if isinstance(options, list) else (
                [o for o in (options or "").split(",") if str(o).strip()] if options else ["选项1"]
            )
            config["current_option"] = state.state if state.state in config["options"] else None
        elif domain == "input_text":
            config["value"] = state.state if state.state not in ("unknown", "unavailable") else None
        else:
            # input_boolean / input_button / binary_sensor 无需额外参数
            pass

        # number 当前值（input_number/counter）
        if domain in ("input_number", "counter"):
            try:
                config["value"] = float(state.state)
            except (TypeError, ValueError):
                config["value"] = None

        target_eid = _target_entity_id(src_type, src_eid)
        items.append({
            "config": config,
            "state_snapshot": {
                target_eid: {"state": state.state, "attributes": attrs},
            },
        })
    return items


# =========================================================================== #
#  状态快照回填                                                                   #
# =========================================================================== #
def _apply_helper_snapshot(entity: Any, snapshot: dict) -> None:
    """把 {state, attributes} 快照写回辅助元素实体内部字段。"""
    state = snapshot.get("state")

    if isinstance(entity, HelperSwitch):
        entity._attr_is_on = state == "on"
    elif isinstance(entity, HelperBinarySensor):
        entity._attr_is_on = state == "on"
    elif isinstance(entity, HelperNumber):
        try:
            if state is not None:
                entity._attr_native_value = float(state)
        except (TypeError, ValueError):
            pass
    elif isinstance(entity, HelperSelect):
        if state in (entity._attr_options or []):
            entity._attr_current_option = state
    elif HelperText is not None and isinstance(entity, HelperText):
        if state not in (None, "unknown", "unavailable"):
            entity._attr_native_value = state
    elif isinstance(entity, HelperButton):
        pass  # 无状态
