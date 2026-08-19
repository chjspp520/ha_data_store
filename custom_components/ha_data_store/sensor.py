"""ha_data_store 传感器实体平台。"""
from __future__ import annotations

import json, logging, os, sqlite3
from datetime import timedelta
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval

from .const import (DOMAIN, TABLE_ENTITY_CONFIGS, TABLE_EXPORT_CONFIGS,
    TABLE_FILE_SOURCE_CONFIGS, TABLE_API_SOURCE_CONFIGS, TABLE_REPORT_ENTITIES,
    CATEGORY_ATTRIBUTE)
from .bridge_entities import get_bridge_entities_for_platform, get_bridge_device_info

_LOGGER = logging.getLogger(__name__)


class MonitoredEntitiesSensor(SensorEntity):
    _attr_has_entity_name = True; _attr_translation_key = "monitored_entities"
    _attr_icon = "mdi:server"; _attr_native_unit_of_measurement = "个"

    def __init__(self, hass, device_info):
        self._hass = hass
        self._attr_unique_id = f"{DOMAIN}_monitored_entities"
        self._attr_device_info = device_info
        self._attr_native_value = None
        self._attr_extra_state_attributes = {}

    def _load_data(self):
        db_path = self._hass.data.get(DOMAIN, {}).get("db_path")
        if not db_path: return {"total": 0}
        try:
            conn = sqlite3.connect(db_path); conn.row_factory = sqlite3.Row
            try:
                rows = [dict(r) for r in conn.execute(f"SELECT * FROM {TABLE_ENTITY_CONFIGS} WHERE enabled = 1 ORDER BY category, entity_id").fetchall()]
                exports = [dict(r) for r in conn.execute(f"SELECT * FROM {TABLE_EXPORT_CONFIGS} WHERE enabled = 1").fetchall()]
                file_srcs = [dict(r) for r in conn.execute(f"SELECT * FROM {TABLE_FILE_SOURCE_CONFIGS} WHERE enabled = 1").fetchall()]
                api_srcs = [dict(r) for r in conn.execute(f"SELECT * FROM {TABLE_API_SOURCE_CONFIGS} WHERE enabled = 1").fetchall()]
            finally: conn.close()

            device_list, env_list, attr_list = [], [], []
            for r in rows:
                eid = r["entity_id"]; st = self._hass.states.get(eid)
                status = "unknown"; state_val = st.state if st else "unavailable"
                if state_val in ("unavailable", "unknown"): status = "unavailable"
                elif r["category"] == "device":
                    status = "online" if state_val in ("on","open","heat","cool","auto","dry","fan_only","home") else ("offline" if state_val in ("off","closed","not_home") else "online")
                elif r["category"] == CATEGORY_ATTRIBUTE: status = "online"
                else:
                    try: float(state_val); status = "online"
                    except: status = "unavailable"
                item = {"entity_id": eid, "status": status, "state": state_val[:30]}
                if r["category"] == "device": device_list.append(item)
                elif r["category"] == CATEGORY_ATTRIBUTE: attr_list.append(item)
                else: env_list.append(item)

            def _health(lst, sk="unavailable"):
                if not lst: return "good", 0, 0
                bad = sum(1 for e in lst if e["status"] == sk)
                t = len(lst)
                if bad == 0: return "good", t-bad, bad
                if bad < t: return "warn", t-bad, bad
                return "bad", t-bad, bad

            d_h, d_o, d_b = _health(device_list); e_h, e_o, e_b = _health(env_list)
            a_b = sum(1 for e in attr_list if e["status"] == "unavailable")
            a_h = "good" if a_b == 0 else "bad"
            a_o = len(attr_list) - a_b
            exp_bad = sum(1 for r in exports if not self._hass.states.get(r["entity_id"]) or self._hass.states.get(r["entity_id"]).state in ("unavailable","unknown"))
            exp_h = "good" if not exports or exp_bad==0 else ("warn" if exp_bad<len(exports) else "bad")
            fs_bad = sum(1 for r in file_srcs if not r.get("file_path") or not os.path.isfile(r["file_path"]))
            fs_h = "good" if not file_srcs or fs_bad==0 else "bad"
            as_bad = sum(1 for r in api_srcs if int(r.get("fail_count",0))>0)
            as_h = "good" if not api_srcs or as_bad==0 else ("warn" if as_bad<len(api_srcs) else "bad")
            db_size = os.path.getsize(db_path) if os.path.isfile(db_path) else 0
            if db_size<1024: sz = f"{db_size} B"
            elif db_size<1048576: sz = f"{db_size/1024:.0f} KB"
            else: sz = f"{db_size/1048576:.1f} MB"
            return {"total":len(rows),"device":{"count":len(device_list),"health":d_h,"ok":d_o,"bad":d_b},"environment":{"count":len(env_list),"health":e_h,"ok":e_o,"bad":e_b},"attribute":{"count":len(attr_list),"health":a_h,"ok":a_o,"bad":a_b},"export":{"count":len(exports),"health":exp_h,"bad":exp_bad,"ok":len(exports)-exp_bad},"file_source":{"count":len(file_srcs),"health":fs_h,"bad":fs_bad},"api_source":{"count":len(api_srcs),"health":as_h,"bad":as_bad,"ok":len(api_srcs)-as_bad},"db_size":sz,"db_size_bytes":db_size,"entities":rows}
        except Exception as e:
            _LOGGER.error("[HDS] 传感器加载失败: %s", e); return {"total":0,"error":str(e)}

    async def _async_refresh(self, now=None):
        data = await self._hass.async_add_executor_job(self._load_data)
        self._attr_native_value = data.get("total", 0)
        self._attr_extra_state_attributes = data; self.async_write_ha_state()


class ReportedEntitiesHealthSensor(SensorEntity):
    """前端卡片上报实体的健康监控传感器。

    状态值 = 掉线（unavailable/unknown）的实体个数
    状态属性 = 每个上报实体的明细（entity_id/name/icon/room_name/status/state）
    """

    _attr_has_entity_name = True
    _attr_translation_key = "reported_entities_health"
    _attr_icon = "mdi:monitor-cellphone"
    _attr_native_unit_of_measurement = "个"

    def __init__(self, hass, device_info):
        self._hass = hass
        self._attr_unique_id = f"{DOMAIN}_reported_entities_health"
        self._attr_device_info = device_info
        self._attr_native_value = None
        self._attr_extra_state_attributes = {}

    def _load_data(self):
        db_path = self._hass.data.get(DOMAIN, {}).get("db_path")
        if not db_path:
            return {"total": 0, "offline": 0, "entities": []}
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            try:
                rows = [dict(r) for r in conn.execute(
                    f"SELECT entity_id, name, icon, room_name FROM {TABLE_REPORT_ENTITIES} "
                    f"ORDER BY room_name, entity_id"
                ).fetchall()]
            finally:
                conn.close()

            # 属性：保留所有上报行（含重复 entity_id，各自带 room_name）
            entity_list = []
            # 掉线统计按"去重后的 entity_id"计算（同一实体多行只计一次）
            seen = set()
            total_unique = 0
            offline = 0
            for r in rows:
                eid = r["entity_id"]
                st = self._hass.states.get(eid)
                state_val = st.state if st else "unavailable"
                if state_val in ("unavailable", "unknown"):
                    status = "offline"
                else:
                    status = "online"
                entity_list.append({
                    "entity_id": eid,
                    "name": r["name"],
                    "icon": r["icon"],
                    "room_name": r["room_name"],
                    "status": status,
                    "state": state_val[:30],
                })
                if eid not in seen:
                    seen.add(eid)
                    total_unique += 1
                    if status == "offline":
                        offline += 1

            return {
                "total": total_unique,           # 去重后的实体总数
                "offline": offline,              # 去重后的掉线数
                "online": total_unique - offline,  # 去重后的在线数
                "total_rows": len(entity_list),  # 原始上报行数（含重复）
                "entities": entity_list,
            }
        except Exception as e:
            _LOGGER.error("[HDS] 上报实体健康传感器加载失败: %s", e)
            return {"total": 0, "offline": 0, "entities": []}

    async def _async_refresh(self, now=None):
        data = await self._hass.async_add_executor_job(self._load_data)
        self._attr_native_value = data.get("offline", 0)
        self._attr_extra_state_attributes = data
        self.async_write_ha_state()


async def async_setup_entry(hass, entry, async_add_entities):
    # 存储回调
    hass.data.setdefault(DOMAIN, {})["async_add_sensor"] = async_add_entities

    device_info = DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)}, name="HA数据统一存储系统", manufacturer="HA数据统一存储系统")
    sensor = MonitoredEntitiesSensor(hass, device_info)
    report_sensor = ReportedEntitiesHealthSensor(hass, device_info)
    entities = [sensor, report_sensor]

    bdi = get_bridge_device_info(entry.entry_id)
    try:
        bridge_entities = get_bridge_entities_for_platform(hass, "sensor", bdi)
    except Exception as e:
        bridge_entities = []
        _LOGGER.error("[bridge] sensor 失败: %s", e)
    if bridge_entities:
        entities.extend(ent for _, ent in bridge_entities)
        reg_er = er.async_get(hass)
        for eid, ent in bridge_entities:
            reg_er.async_get_or_create(domain="sensor", platform=DOMAIN, unique_id=ent.unique_id, suggested_object_id=eid.split(".", 1)[1])
        reg = hass.data.setdefault(DOMAIN, {}).setdefault("bridge_entity_instances", {})
        for eid, ent in bridge_entities: reg[eid] = ent
        _LOGGER.info("[bridge] sensor 创建 %d 个实体", len(bridge_entities))

    async_add_entities(entities)
    async_track_time_interval(hass, sensor._async_refresh, timedelta(seconds=30))
    async_track_time_interval(hass, report_sensor._async_refresh, timedelta(seconds=30))
