"""ha_data_store 传感器实体平台。"""
from __future__ import annotations

import asyncio
import json, logging, os, sqlite3, time
from datetime import timedelta
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import EVENT_STATE_CHANGED, HomeAssistant
from homeassistant.helpers import entity_registry as er, network as hass_network
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval, async_track_utc_time_change

from .const import (DOMAIN, TABLE_ENTITY_CONFIGS, TABLE_EXPORT_CONFIGS,
    TABLE_FILE_SOURCE_CONFIGS, TABLE_API_SOURCE_CONFIGS, TABLE_REPORT_ENTITIES,
    TABLE_USER_ACTIONS, TABLE_AUTOMATIONS, TABLE_AUTOMATION_LOGS,
    CATEGORY_ATTRIBUTE)
from .bridge_entities import get_bridge_entities_for_platform, get_bridge_device_info
from .daily_summary import build_daily_summary_sync

_LOGGER = logging.getLogger(__name__)

# 自动化星期名（与 automations.py 保持一致）
_WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


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


class UserActionsSensor(SensorEntity):
    """前端操作记录统计传感器（常用设备分析）。

    状态值 = 近 N 天有操作的不同设备实体数（按 entity_id 去重，不随用户重复计算）
    状态属性 = 近 N 天按 (用户, action_snapshot) 聚合的设备列表（含完整 tapAction 快照，
              前端可直接据此还原设备控制面板），同一实体被不同用户操作时各自独立成条，
              每条带独立 user_name / count / last_used，按使用次数降序。
              额外字段：total_user_devices = 用户×设备 组合条数。
    """

    _attr_has_entity_name = False
    _attr_name = "近期使用设备"
    _attr_icon = "mdi:chart-histogram"
    _attr_native_unit_of_measurement = "个"

    # 统计窗口（天），可整体调整
    WINDOW_DAYS = 30

    def __init__(self, hass, device_info):
        self._hass = hass
        self._attr_unique_id = f"{DOMAIN}_user_actions"
        self._attr_device_info = device_info
        self._attr_native_value = None
        self._attr_extra_state_attributes = {}

    def _load_data(self):
        db_path = self._hass.data.get(DOMAIN, {}).get("db_path")
        if not db_path:
            return {"window_days": self.WINDOW_DAYS, "total_actions": 0, "total_devices": 0, "devices": []}
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            try:
                cutoff = int(time.time() * 1000) - self.WINDOW_DAYS * 24 * 3600 * 1000
                rows = [dict(r) for r in conn.execute(
                    f"SELECT user_name, entity_id, action, name, icon, room_name, "
                    f"service, card_type, other, state_log, ts, ts_text, action_snapshot, config_id, device_type FROM {TABLE_USER_ACTIONS} "
                    f"WHERE ts >= ? ORDER BY ts ASC", (cutoff,)
                ).fetchall()]
            finally:
                conn.close()
        except Exception as e:
            _LOGGER.error("[HDS] user_actions 传感器加载失败: %s", e)
            return {"window_days": self.WINDOW_DAYS, "total_actions": 0, "total_devices": 0, "devices": [], "error": str(e)}

        # 按 (用户, action_snapshot) 归一化聚合。
        # 同一设备面板多实体组合归为一条；同一实体被不同用户操作时，各自独立成条
        # （每条带独立 user_name / count / last_used），供前端按用户筛选与展示。
        groups = {}
        for r in rows:
            snap = r.get("action_snapshot") or ""
            skey = self._normalize_snapshot_key(snap, r.get("entity_id"))
            user = (r.get("user_name") or "").strip()
            # 用户 + 快照键 作为聚合键：不同用户操作同一实体拆分为独立记录
            key = f"{user}\x00{skey}"
            g = groups.get(key)
            if g is None:
                # config_id：优先取库中独立列，其次从 action_snapshot JSON 解析
                config_id = (r.get("config_id") or "").strip()
                if not config_id and snap:
                    try:
                        snap_obj = json.loads(snap)
                        if isinstance(snap_obj, dict) and isinstance(snap_obj.get("config_id"), str):
                            config_id = snap_obj["config_id"].strip()
                    except Exception:
                        pass
                g = {
                    "entity_id": r.get("entity_id") or "",
                    "action": r.get("action") or "",
                    "name": r.get("name") or "",
                    "icon": r.get("icon") or "",
                    "room_name": r.get("room_name") or "",
                    "service": r.get("service") or "",
                    "user_name": user,
                    "config_id": config_id,
                    "device_type": (r.get("device_type") or "").strip(),
                    "count": 0,
                    "last_used": 0,
                    "state_log": "",
                    "action_snapshot": snap,
                }
                groups[key] = g
            g["count"] += 1
            # 记录最近一次的状态变化（rows 按 ts 升序，最后覆盖的即最新）
            cur_state = r.get("state_log") or ""
            if cur_state:
                g["state_log"] = cur_state
            if (r.get("ts") or 0) > g["last_used"]:
                g["last_used"] = r.get("ts") or 0

        devices = list(groups.values())
        # total_devices 保持为去重实体数（不随用户重复计算）
        dedup_entities = len({d["entity_id"] for d in devices if d["entity_id"]})
        for d in devices:
            # 增加人类可读的最近使用时间（本地时区），保留原始时间戳供程序使用
            d["last_used_text"] = self._format_ts(d.get("last_used") or 0)
        devices.sort(key=lambda d: (d["count"], d["last_used"]), reverse=True)
        return {
            "window_days": self.WINDOW_DAYS,
            "total_actions": len(rows),
            "total_devices": dedup_entities,
            "total_user_devices": len(devices),
            "devices": devices,
        }

    @staticmethod
    def _format_ts(ts_ms):
        """把毫秒时间戳格式化为本地可读时间字符串；非法值返回空串。"""
        if not ts_ms:
            return ""
        try:
            return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts_ms / 1000.0))
        except Exception:
            return ""

    @staticmethod
    def _normalize_snapshot_key(snapshot_json, entity_id):
        """把 action_snapshot 归一化为稳定的聚合键。

        策略：能解析为 JSON 对象时，剔除运行时动态字段（ts 等），按键名排序后
        JSON 序列化作为键；解析失败则回退用 entity_id 作键（保证不塌缩）。
        """
        if not snapshot_json:
            return f"eid:{entity_id}"
        try:
            obj = json.loads(snapshot_json)
            if isinstance(obj, dict):
                # 剔除明显属于运行时动态/实例相关的字段
                for k in ("ts", "timestamp"):
                    obj.pop(k, None)
                try:
                    return json.dumps(obj, sort_keys=True, ensure_ascii=False)
                except Exception:
                    return f"eid:{entity_id}"
            return snapshot_json
        except Exception:
            return f"eid:{entity_id}"

    async def _async_refresh(self, now=None):
        data = await self._hass.async_add_executor_job(self._load_data)
        self._attr_native_value = data.get("total_devices", 0)
        self._attr_extra_state_attributes = data
        self.async_write_ha_state()


class ReportedEntitiesHealthSensor(SensorEntity):
    """前端卡片上报实体的健康监控传感器。

    状态值 = 掉线（unavailable）的实体个数
    状态属性 = 每个上报实体的明细（entity_id/name/icon/room_name/status/state）
    status 取值：offline=unavailable（真离线）、unknown=unknown（无状态值，
    如未被点击过的 button/input_button）、online=其余正常状态。
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
            return {"total": 0, "unknown": 0, "offline": 0, "entities": []}
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            try:
                rows = [dict(r) for r in conn.execute(
                    f"SELECT entity_id, name, icon, room_name, rooms FROM {TABLE_REPORT_ENTITIES} "
                    f"ORDER BY room_name, entity_id"
                ).fetchall()]
            finally:
                conn.close()

            # 属性：保留所有上报行（含重复 entity_id，各自带 room_name）
            entity_list = []
            # 统计按"去重后的 entity_id"计算（同一实体多行只计一次）
            seen = set()
            total_unique = 0
            offline = 0
            unknown = 0
            for r in rows:
                eid = r["entity_id"]
                st = self._hass.states.get(eid)
                if st is None:
                    # 实体不存在于 states（被删除/集成未加载）视为真离线
                    state_val = "unavailable"
                else:
                    state_val = st.state
                if state_val == "unavailable":
                    status = "offline"
                elif state_val == "unknown":
                    status = "unknown"
                else:
                    status = "online"
                entity_list.append({
                    "entity_id": eid,
                    "name": r["name"],
                    "icon": r["icon"],
                    "room_name": r["room_name"],
                    "rooms": r.get("rooms") or "",
                    "status": status,
                    "state": state_val[:30],
                })
                if eid not in seen:
                    seen.add(eid)
                    total_unique += 1
                    if status == "offline":
                        offline += 1
                    elif status == "unknown":
                        unknown += 1

            return {
                "total": total_unique,           # 去重后的实体总数
                "offline": offline,              # 去重后的掉线数（unavailable）
                "unknown": unknown,              # 去重后的未知数（unknown，无状态值）
                "online": total_unique - unknown - offline,  # 去重后的在线数
                "total_rows": len(entity_list),  # 原始上报行数（含重复）
                "entities": entity_list,
            }
        except Exception as e:
            _LOGGER.error("[HDS] 上报实体健康传感器加载失败: %s", e)
            return {"total": 0, "unknown": 0, "offline": 0, "entities": []}

    async def _async_refresh(self, now=None):
        data = await self._hass.async_add_executor_job(self._load_data)
        self._attr_native_value = data.get("offline", 0)
        self._attr_extra_state_attributes = data
        self.async_write_ha_state()


class TodayFamilyStatusSensor(SensorEntity):
    """今日家庭状态总结传感器。

    状态值（native_value）= 精简中文段落（为 0 的项整节跳过）
    attributes：
      summary      → 同一段精简文本
      sections     → 完整结构化分节（environment/devices/vacuum/health/xiaoai）
      date         → 总结日期（东八区）
      alerts       → 异常提醒列表
      overall      → normal | warning（存在提醒时为 warning）
      generated_at → 生成时间

    按需生成：由按钮 button.ha_data_store_daily_summary 或服务
    ha_data_store.generate_daily_summary 触发 async_trigger_refresh。
    不做 30s 轮询（保持轻量）。
    """

    _attr_has_entity_name = True
    _attr_translation_key = "today_family_status"
    _attr_icon = "mdi:clipboard-text-clock-outline"
    _attr_native_value = None
    _attr_extra_state_attributes = {}

    def __init__(self, hass, device_info):
        self._hass = hass
        self._attr_unique_id = f"{DOMAIN}_today_family_status"
        self._attr_device_info = device_info
        self._attr_native_value = None
        self._attr_extra_state_attributes = {}

    def _load_data(self, date_str=None):
        db_path = self._hass.data.get(DOMAIN, {}).get("db_path")
        if not db_path:
            return {"date": date_str, "summary": "今日家庭状态：暂无有效数据。",
                    "status_value": "暂无数据", "sections": {}, "overall": "normal",
                    "alerts": [], "error": "db_path 缺失"}
        return build_daily_summary_sync(db_path, self._hass, date_str)

    async def async_trigger_refresh(self, date_str=None):
        """按需生成今日总结（按钮/服务调用）。"""
        try:
            data = await self._hass.async_add_executor_job(self._load_data, date_str)
        except Exception as e:
            _LOGGER.error("[HDS] 今日总结生成失败: %s", e)
            data = {"date": date_str, "summary": f"今日家庭状态：生成失败（{e}）",
                    "status_value": "生成失败", "sections": {}, "overall": "warning",
                    "alerts": [], "error": str(e)}
        self._attr_native_value = data.get("status_value", data.get("summary"))
        self._attr_extra_state_attributes = data
        self.async_write_ha_state()


class AutomationStatusSensor(SensorEntity):
    """简单自动化 + 上报自动化实体（ha_automation）状态传感器。

    状态值 = 启用自动化总数（简单自动化 enabled 数 + 上报自动化实体中状态为 on 的个数）
    状态属性：
      total        → 自动化总数（简单自动化 + 上报自动化实体）
      enabled      → 启用数（简单自动化 enabled + 上报自动化实体状态 on 数，= 状态值）
      disabled     → 停用数（简单自动化 disabled + 上报自动化实体状态 off/不可用 数）
      success      → 最近一次执行为成功的自动化数（上报自动化按 last_triggered 非空近似）
      failed       → 最近一次执行为失败/部分失败的自动化数
      skipped      → 最近一次执行为条件跳过的自动化数
      never        → 从未执行过的自动化数（上报自动化按 last_triggered 为空）
      total_runs   → 全部执行次数合计（上报自动化按"触发过"各计 1 次近似）
      updated_at   → 数据刷新时间
      automations[] → 每个自动化的详细信息（id/name/enabled/trigger_type/trigger_desc/
                      stop_on_error/next_run/last_run/last_result/last_duration_ms/
                      run_count/success_count/failed_count/skipped_count）
      ha_automation[] → 上报实体中的自动化实体（report_entities 表 entity_id 以
                      automation. 开头的行：entity_id/name/icon/room_name/source/
                      rooms/last_report_time）
    """

    _attr_has_entity_name = True
    _attr_translation_key = "automation_status"
    _attr_icon = "mdi:creation"
    _attr_native_unit_of_measurement = "个"

    def __init__(self, hass, device_info):
        self._hass = hass
        self._attr_unique_id = f"{DOMAIN}_automation_status"
        self._attr_device_info = device_info
        self._attr_native_value = None
        self._attr_extra_state_attributes = {}
        self._last_log_max_id: int | None = None  # automation_logs 最大 id（检测新数据）
        self._state_debounce_until = 0.0          # automation.* 实体变化防抖截止时间（monotonic）
        self._refreshing = False                  # 防重入标志

    @staticmethod
    def _parse_json(raw, default):
        """兼容 dict/list/JSON 字符串。"""
        if isinstance(raw, (dict, list)):
            return raw
        try:
            return json.loads(raw) if raw else default
        except Exception:
            return default

    @staticmethod
    def _describe_trigger(ttype, tcfg):
        """生成人类可读的触发描述（与 automations.py describe_trigger 一致）。"""
        if ttype == "interval":
            try:
                secs = max(int(tcfg.get("interval_seconds") or 60), 10)
            except (TypeError, ValueError):
                secs = 60
            if secs % 3600 == 0:
                return f"每 {secs // 3600} 小时"
            if secs % 60 == 0:
                return f"每 {secs // 60} 分钟"
            return f"每 {secs} 秒"
        time_str = str(tcfg.get("time") or "00:00")
        days = tcfg.get("days") or []
        if not isinstance(days, list) or not days:
            return f"每天 {time_str}"
        names = "、".join(_WEEKDAY_NAMES[d] for d in sorted(days) if 0 <= d <= 6)
        return f"{names} {time_str}"

    def _load_data(self):
        db_path = self._hass.data.get(DOMAIN, {}).get("db_path")
        empty = {"total": 0, "enabled": 0, "disabled": 0, "success": 0,
                 "failed": 0, "skipped": 0, "never": 0, "total_runs": 0,
                 "updated_at": "", "automations": [], "ha_automation": []}
        if not db_path:
            return empty
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            try:
                rows = [dict(r) for r in conn.execute(
                    f"SELECT id, name, enabled, trigger_type, trigger_config, stop_on_error, "
                    f"next_run, last_run, last_result FROM {TABLE_AUTOMATIONS} ORDER BY id ASC"
                ).fetchall()]
                # 按 (automation_id, status) 聚合执行记录数
                stat_rows = [dict(r) for r in conn.execute(
                    f"SELECT automation_id, status, COUNT(*) AS cnt FROM {TABLE_AUTOMATION_LOGS} "
                    f"GROUP BY automation_id, status"
                ).fetchall()]
                # 每个自动化最近一次执行的耗时
                dur_rows = [dict(r) for r in conn.execute(
                    f"SELECT automation_id, duration_ms FROM {TABLE_AUTOMATION_LOGS} "
                    f"WHERE id IN (SELECT MAX(id) FROM {TABLE_AUTOMATION_LOGS} GROUP BY automation_id)"
                ).fetchall()]
                # 当前 automation_logs 最大 id（用于检测新数据触发自动刷新）
                max_log_row = conn.execute(
                    f"SELECT COALESCE(MAX(id), 0) FROM {TABLE_AUTOMATION_LOGS}"
                ).fetchone()
                max_log_id = int(max_log_row[0] if max_log_row else 0)
                # 上报实体中的自动化实体（前端实体健康上报的 automation.* 实体）
                report_rows = [dict(r) for r in conn.execute(
                    f"SELECT entity_id, name, icon, room_name, source, rooms, last_report_time "
                    f"FROM {TABLE_REPORT_ENTITIES} "
                    f"WHERE entity_id LIKE 'automation.%' ORDER BY entity_id ASC"
                ).fetchall()]
            finally:
                conn.close()
        except Exception as e:
            _LOGGER.error("[HDS] 自动化状态传感器加载失败: %s", e)
            empty["error"] = str(e)
            return empty

        stats: dict[int, dict[str, int]] = {}
        for s in stat_rows:
            stats.setdefault(s["automation_id"], {})[s["status"]] = s["cnt"]
        last_dur = {r["automation_id"]: r["duration_ms"] for r in dur_rows}

        automations: list[dict[str, Any]] = []
        success = failed = skipped = never = 0
        total_runs = 0
        for r in rows:
            aid = r["id"]
            st = stats.get(aid, {})
            run_count = int(sum(st.values())) if st else 0
            success_count = int(st.get("success", 0) or 0)
            failed_count = int(st.get("failed", 0) or 0) + int(st.get("partial_failed", 0) or 0)
            skipped_count = int(st.get("skipped", 0) or 0)
            total_runs += run_count
            last_result = str(r.get("last_result") or "")
            item = {
                "id": aid,
                "name": str(r.get("name") or ""),
                "enabled": bool(r.get("enabled")),
                "trigger_type": str(r.get("trigger_type") or ""),
                "trigger_desc": self._describe_trigger(
                    r.get("trigger_type"),
                    self._parse_json(r.get("trigger_config"), {}),
                ),
                "stop_on_error": bool(r.get("stop_on_error")),
                "next_run": str(r.get("next_run") or ""),
                "last_run": str(r.get("last_run") or ""),
                "last_result": last_result,
                "last_duration_ms": int(last_dur.get(aid, 0) or 0),
                "run_count": run_count,
                "success_count": success_count,
                "failed_count": failed_count,
                "skipped_count": skipped_count,
            }
            automations.append(item)
            # 按最近一次执行结果统计自动化数量
            if last_result == "success":
                success += 1
            elif last_result in ("failed", "partial_failed"):
                failed += 1
            elif last_result == "skipped":
                skipped += 1
            else:
                never += 1

        return {
            "total": len(automations),
            "enabled": sum(1 for a in automations if a["enabled"]),
            "disabled": sum(1 for a in automations if not a["enabled"]),
            "success": success,
            "failed": failed,
            "skipped": skipped,
            "never": never,
            "total_runs": total_runs,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "automations": automations,
            "ha_automation": report_rows,
            "log_max_id": max_log_id,
        }

    async def async_trigger_refresh(self, now=None):
        """手动/事件/写日志回调触发的按需刷新（防重入）。

        定时器、automation_logs 新数据回调、automation.* 实体状态变化、
        按钮手动触发均走此入口；若上一轮刷新尚未结束则跳过本次。
        """
        if self._refreshing:
            return
        self._refreshing = True
        try:
            await self._async_refresh(now)
        finally:
            self._refreshing = False

    async def _async_refresh(self, now=None):
        data = await self._hass.async_add_executor_job(self._load_data)
        # 检测 automation_logs 是否有新数据（写日志回调之外的兜底，如外部直写数据库）
        log_max_id = int(data.get("log_max_id") or 0)
        if self._last_log_max_id is not None and log_max_id > self._last_log_max_id:
            _LOGGER.info(
                "[HDS] 检测到 automation_logs 新数据（id %d→%d），已自动刷新自动化状态",
                self._last_log_max_id, log_max_id,
            )
        self._last_log_max_id = log_max_id
        data.pop("log_max_id", None)
        # 合并 ha_automation（上报的 HA 原生自动化实体）状态统计：
        #   - on/off 由实体实时状态判定（无法检测/不可用/unknown 均视为停用）
        #   - last_triggered 非空视为"执行过"（计入 success 与 total_runs 各 1 次近似）
        ha_list = data.get("ha_automation") or []
        if ha_list:
            ha_on = ha_off = ha_triggered = 0
            for item in ha_list:
                st = self._hass.states.get(item.get("entity_id") or "")
                if st is not None and st.state == "on":
                    ha_on += 1
                else:
                    ha_off += 1  # off / unknown / unavailable / 实体不存在 → 停用
                if st and st.attributes.get("last_triggered"):
                    ha_triggered += 1
            data["total"] = int(data.get("total", 0)) + len(ha_list)
            data["enabled"] = int(data.get("enabled", 0)) + ha_on
            data["disabled"] = int(data.get("disabled", 0)) + ha_off
            data["success"] = int(data.get("success", 0)) + ha_triggered
            data["never"] = int(data.get("never", 0)) + (len(ha_list) - ha_triggered)
            data["total_runs"] = int(data.get("total_runs", 0)) + ha_triggered
        self._attr_native_value = data.get("enabled", 0)
        self._attr_extra_state_attributes = data
        self.async_write_ha_state()

    async def _async_on_automation_state_changed(self, event):
        """ha_automation 节点（automation.* 实体）状态变化 → 自动刷新。

        每次变化都刷新会导致高频执行（HA 原生自动化运行即产生 on/off 变化），
        故做 2 秒防抖合并；30 秒定时轮询仍作为最终兜底。
        """
        entity_id = (event.data or {}).get("entity_id", "") or ""
        if not entity_id.startswith("automation."):
            return
        now = time.monotonic()
        if now < self._state_debounce_until:
            return
        self._state_debounce_until = now + 2.0
        try:
            await self.async_trigger_refresh()
            _LOGGER.debug("[HDS] %s 状态变化，已自动刷新自动化状态", entity_id)
        except Exception as e:
            _LOGGER.error("[HDS] 响应 automation 实体变化刷新失败: %s", e)


class DbViewerUrlSensor(SensorEntity):
    """数据库浏览器访问地址传感器。

    state  = 完整可访问地址（http://IP:端口/api/ha_data_store/db_viewer）
    attributes: path / base_url / updated_at
    """

    _attr_has_entity_name = True
    _attr_translation_key = "db_viewer_url"
    _attr_icon = "mdi:database-search-outline"

    def __init__(self, hass, device_info):
        self._hass = hass
        self._attr_unique_id = f"{DOMAIN}_db_viewer_url"
        self._attr_device_info = device_info
        self._attr_native_value = None
        self._attr_extra_state_attributes = {}

    def _load_db_info(self):
        """读取数据库存储位置、文件大小与各表记录数（同步，供 executor 调用）。

        返回 (db_path, size_bytes, total_records, records_by_table)；异常时返回 None 项。
        """
        db_path = self._hass.data.get(DOMAIN, {}).get("db_path")
        if not db_path:
            return None, None, None, None
        size_bytes = None
        total_records = 0
        records_by_table = {}
        try:
            size_bytes = os.path.getsize(db_path)
        except Exception as e:
            _LOGGER.warning("[HDS] 读取数据库文件大小失败: %s", e)
        try:
            conn = sqlite3.connect(db_path)
            try:
                tables = [r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()]
                for t in tables:
                    try:
                        cnt = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                    except Exception:
                        continue
                    records_by_table[t] = cnt
                    total_records += cnt
            finally:
                conn.close()
        except Exception as e:
            _LOGGER.warning("[HDS] 读取数据库记录数失败: %s", e)
        return db_path, size_bytes, total_records, records_by_table

    async def _fetch_url(self):
        """计算 db_viewer 访问地址与数据库信息（不写 state），返回 (url, attributes)。"""
        try:
            # get_url 是同步函数（返回 str），非协程
            base = hass_network.get_url(self._hass).rstrip("/")
            path = "/api/ha_data_store/db_viewer"
            attrs = {
                "path": path,
                "base_url": base,
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            # 数据库存储位置 / 大小 / 记录总条数（executor 避免阻塞事件循环）
            db_path, size_bytes, total_records, records_by_table = \
                await self._hass.async_add_executor_job(self._load_db_info)
            if db_path:
                attrs["db_path"] = db_path
                attrs["db_size_bytes"] = size_bytes
                attrs["db_size_mb"] = round(size_bytes / 1048576, 2) if size_bytes else 0
                attrs["total_records"] = total_records
                attrs["records_by_table"] = records_by_table
            return f"{base}{path}", attrs
        except Exception as e:
            _LOGGER.error("[HDS] 获取 db_viewer 访问地址失败: %s", e)
            return None, {"error": str(e)}

    async def async_trigger_refresh(self, now=None):
        """重新获取 db_viewer 访问地址并刷新传感器。"""
        url, attrs = await self._fetch_url()
        self._attr_native_value = url
        self._attr_extra_state_attributes = attrs
        self.async_write_ha_state()


async def async_setup_entry(hass, entry, async_add_entities):
    # 存储回调
    hass.data.setdefault(DOMAIN, {})["async_add_sensor"] = async_add_entities

    device_info = DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)}, name="HA数据统一存储系统", manufacturer="HA数据统一存储系统")
    sensor = MonitoredEntitiesSensor(hass, device_info)
    report_sensor = ReportedEntitiesHealthSensor(hass, device_info)
    summary_sensor = TodayFamilyStatusSensor(hass, device_info)
    user_actions_sensor = UserActionsSensor(hass, device_info)
    automation_status_sensor = AutomationStatusSensor(hass, device_info)
    db_viewer_url_sensor = DbViewerUrlSensor(hass, device_info)
    # 存引用，供按钮/服务触发按需刷新
    hass.data.setdefault(DOMAIN, {})["today_family_sensor"] = summary_sensor
    hass.data.setdefault(DOMAIN, {})["user_actions_sensor"] = user_actions_sensor
    hass.data.setdefault(DOMAIN, {})["automation_status_sensor"] = automation_status_sensor
    entities = [sensor, report_sensor, summary_sensor, user_actions_sensor,
                automation_status_sensor, db_viewer_url_sensor]
    # db_viewer 访问地址：启动时立即获取一次，后续低频刷新
    url, attrs = await db_viewer_url_sensor._fetch_url()
    db_viewer_url_sensor._attr_native_value = url
    db_viewer_url_sensor._attr_extra_state_attributes = attrs

    # 自动化状态传感器：固定实体 ID 为 sensor.ha_data_store_automation
    # （HA 实体 ID 只允许小写字母/数字/下划线，配置里的点号写法会自动归一为下划线）
    try:
        reg = er.async_get(hass)
        new_eid = "sensor.ha_data_store_automation"
        old_eid = reg.async_get_entity_id("sensor", DOMAIN, automation_status_sensor.unique_id)
        if old_eid and old_eid != new_eid:
            try:
                reg.async_update_entity(old_eid, new_entity_id=new_eid)
            except Exception as e:
                _LOGGER.warning("[HDS] 自动化状态传感器实体重命名失败（%s → %s）: %s", old_eid, new_eid, e)
        reg.async_get_or_create(domain="sensor", platform=DOMAIN,
                                unique_id=automation_status_sensor.unique_id,
                                suggested_object_id="ha_data_store_automation")
    except Exception as e:
        _LOGGER.warning("[HDS] 自动化状态传感器实体ID设置失败: %s", e)

    # 数据库浏览器地址传感器：强制固定实体 ID 为 sensor.ha_data_store_db_viewer_url
    try:
        reg = er.async_get(hass)
        new_eid = "sensor.ha_data_store_db_viewer_url"
        old_eid = reg.async_get_entity_id("sensor", DOMAIN, db_viewer_url_sensor.unique_id)
        if old_eid and old_eid != new_eid:
            try:
                reg.async_update_entity(old_eid, new_entity_id=new_eid)
            except Exception as e:
                _LOGGER.warning("[HDS] 数据库浏览器地址传感器实体重命名失败（%s → %s）: %s", old_eid, new_eid, e)
        reg.async_get_or_create(domain="sensor", platform=DOMAIN,
                                unique_id=db_viewer_url_sensor.unique_id,
                                suggested_object_id="ha_data_store_db_viewer_url")
    except Exception as e:
        _LOGGER.warning("[HDS] 数据库浏览器地址传感器实体ID设置失败: %s", e)

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
    async_track_time_interval(hass, user_actions_sensor._async_refresh, timedelta(seconds=30))
    # 自动化状态传感器：定时轮询作为兜底；automation_logs 新数据由写日志回调触发；
    # automation.* 实体（ha_automation 节点）状态变化实时监听触发（2 秒防抖）
    async_track_time_interval(hass, automation_status_sensor.async_trigger_refresh, timedelta(seconds=30))
    hass.bus.async_listen(
        EVENT_STATE_CHANGED, automation_status_sensor._async_on_automation_state_changed
    )
    # db_viewer 访问地址：每 10 分钟低频刷新（HA 外部/内部地址变化时更新）
    async_track_time_interval(hass, db_viewer_url_sensor.async_trigger_refresh, timedelta(minutes=10))

    # ── 今日家庭状态：启动后 1 分钟自动生成 + 每 30 分钟（整 30 分钟）更新 ──
    async def _delayed_first_refresh():
        """启动后延迟 1 分钟生成一次家庭状态。"""
        try:
            await asyncio.sleep(60)
            await summary_sensor.async_trigger_refresh()
            _LOGGER.info("[HDS] 今日家庭状态已自动生成（启动后1分钟）")
        except Exception as e:
            _LOGGER.exception("[HDS] 启动后自动生成今日家庭状态失败: %s", e)

    async def _periodic_refresh(now=None):
        """每 30 分钟（整 30 分钟）更新家庭状态。"""
        try:
            await summary_sensor.async_trigger_refresh()
        except Exception as e:
            _LOGGER.exception("[HDS] 定时更新今日家庭状态失败: %s", e)

    hass.async_create_task(_delayed_first_refresh())
    # 对齐整 30 分钟（minute=0/30, second=0），HA 内部按本地时区计算
    hass.data.setdefault(DOMAIN, {})["cancel_daily_summary"] = async_track_utc_time_change(
        hass, _periodic_refresh, minute={0, 30}, second=0,
    )
