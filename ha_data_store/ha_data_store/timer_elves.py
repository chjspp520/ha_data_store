"""定时精灵核心引擎 - ha_data_store 子模块。

适配自 timer_backend/coordinator.py 的 TimerBackendCoordinator。
核心变更：
  - SQLite 替代 JSON 文件持久化（复用 ha_data_store 的数据库）
  - 总线事件从 timer_backend_response 迁移到 ha_data_store_timer_response
  - 常量/信号迁移到 timer_elves_const.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import uuid
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .timer_elves_const import (
    TIMER_EVENT_PREFIX,
    TIMER_SIGNAL_UPDATE_SENSOR,
    TABLE_TIMER_TASKS,
    DEFAULT_TIME_ZONE,
    ATTR_ACTIVE_TASKS,
    ATTR_TOTAL_TASKS,
    ATTR_ACTIVE_TIMERS,
    ATTR_ACTIVE_SCHEDULES,
    ATTR_CURRENT_TASK,
    ATTR_SUCCESSFUL_TASK,
    ATTR_FAILED_TASK,
    ATTR_TODAY_TASK,
    ATTR_ALL_TASK_LIST,
    MAX_HISTORY_RECORDS,
    DEFAULT_DEFAULT_ACTIONS,
)

_LOGGER = logging.getLogger(__name__)

TIMER_RESPONSE_EVENT = f"{TIMER_EVENT_PREFIX}_response"


class RepeatType(Enum):
    NONE = "none"
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


class TimerElvesCoordinator:
    """定时精灵协调器 - 一次性/周期定时器引擎，支持空调/窗帘等 domain。"""

    def __init__(
        self,
        hass: HomeAssistant,
        db_path: str,
        time_zone: str = DEFAULT_TIME_ZONE,
        default_actions: dict | None = None,
    ) -> None:
        self.hass = hass
        self.db_path = db_path
        self.time_zone = time_zone
        self.default_actions = default_actions or {}

        # 空调/窗帘配置
        self.climate_config = {
            "default_temperature": 25.0,
            "default_mode": "cool",
            "restore_previous": True,
            "save_state_on_timer": True,
        }
        self.cover_config = {
            "default_position": 50,
            "restore_previous": True,
            "save_state_on_timer": True,
        }

        # 存储
        self.tasks: Dict[str, Any] = {}
        self.timers: Dict[str, Any] = {}          # 一次性定时器句柄
        self.recurring_timers: Dict[str, Any] = {} # 周期定时器句柄
        self.entity_timers: Dict[str, str] = {}    # entity_id → timer_id 索引
        self.climate_previous_states: Dict[str, Any] = {}
        self.cover_previous_states: Dict[str, Any] = {}

        # 统计数据
        self.stats = {
            "total_task": 0,
            "successful_task": 0,
            "failed_task": 0,
            "today_task": 0,
        }

        # 频率限制
        self._last_save_timestamp: Optional[datetime] = None
        self._delayed_update_unsub: Optional[Callable] = None

        # 时区
        try:
            self.tz = dt_util.get_time_zone(time_zone)
            if self.tz is None:
                _LOGGER.warning(f"[timer_elves] Invalid time zone: {time_zone}, using system timezone")
                self.tz = dt_util.DEFAULT_TIME_ZONE
        except Exception:
            _LOGGER.warning(f"[timer_elves] Invalid time zone: {time_zone}, using system timezone")
            self.tz = dt_util.DEFAULT_TIME_ZONE

    # ================================================================== #
    #  生命周期管理                                                         #
    # ================================================================== #

    async def async_setup(self) -> None:
        """启动协调器。"""
        # 监听前端事件
        self.hass.bus.async_listen("ha_data_store_timer_event", self._handle_frontend_event)
        # 监听空调/窗帘状态变化（独立监听，不依赖 ha_data_store 的白名单）
        self.hass.bus.async_listen("state_changed", self._handle_state_changed)
        # 恢复任务
        await self._init_db_table()
        await self.restore_tasks()
        await self.update_stats()
        await self._update_sensor()
        _LOGGER.info(f"[timer_elves] Coordinator started (timezone={self.time_zone})")

    async def async_unload(self) -> None:
        """卸载协调器。"""
        if self._delayed_update_unsub:
            self._delayed_update_unsub()
            self._delayed_update_unsub = None
        for h in list(self.timers.values()):
            if h: h()
        for h in list(self.recurring_timers.values()):
            if h: h()
        await self.save_tasks()
        _LOGGER.info("[timer_elves] Coordinator stopped")

    # ================================================================== #
    #  SQLite 持久化                                                       #
    # ================================================================== #

    async def _init_db_table(self) -> None:
        """确保 timer_tasks 表存在。"""
        def _init():
            conn = sqlite3.connect(self.db_path)
            try:
                conn.execute(f"""
                    CREATE TABLE IF NOT EXISTS {TABLE_TIMER_TASKS} (
                        task_id TEXT PRIMARY KEY,
                        task_data TEXT NOT NULL,
                        updated_at TEXT NOT NULL DEFAULT ''
                    )
                """)
                conn.commit()
            finally:
                conn.close()
        await self.hass.async_add_executor_job(_init)

    async def save_tasks(self) -> None:
        """保存所有任务到 SQLite（带 5 秒频率限制）。"""
        now = self.get_local_now()
        if self._last_save_timestamp is not None:
            diff = (now - self._last_save_timestamp).total_seconds()
            if diff < 5:
                _LOGGER.debug(f"[timer_elves] Save skipped ({diff:.1f}s)")
                return
        try:
            now_str = now.strftime("%Y-%m-%d %H:%M:%S")
            items = list(self.tasks.items())
            def _save():
                conn = sqlite3.connect(self.db_path)
                try:
                    c = conn.cursor()
                    for tid, tdata in items:
                        c.execute(
                            f"INSERT OR REPLACE INTO {TABLE_TIMER_TASKS} (task_id, task_data, updated_at) VALUES (?, ?, ?)",
                            (tid, json.dumps(tdata, default=str, ensure_ascii=False), now_str),
                        )
                    conn.commit()
                finally:
                    conn.close()
            await self.hass.async_add_executor_job(_save)
            self._last_save_timestamp = now
            _LOGGER.debug(f"[timer_elves] Saved {len(items)} tasks")
        except Exception as e:
            _LOGGER.error(f"[timer_elves] save_tasks failed: {e}")

    async def restore_tasks(self) -> None:
        """从 SQLite 恢复所有任务并重新调度。"""
        try:
            def _load():
                conn = sqlite3.connect(self.db_path)
                try:
                    rows = conn.execute(
                        f"SELECT task_id, task_data FROM {TABLE_TIMER_TASKS}"
                    ).fetchall()
                    result = {}
                    for tid, tdata_json in rows:
                        try:
                            result[tid] = json.loads(tdata_json)
                        except json.JSONDecodeError:
                            continue
                    return result
                finally:
                    conn.close()
            data = await self.hass.async_add_executor_job(_load)
        except Exception as e:
            _LOGGER.error(f"[timer_elves] restore_tasks load failed: {e}")
            data = {}

        restored = 0
        recurring_restored = 0
        for timer_id, timer_data in data.items():
            repeat_type = timer_data.get("repeat_type", "none")
            schedule_time = timer_data.get("schedule_time")
            if repeat_type != "none" and schedule_time:
                await self._restore_recurring_timer(timer_id, timer_data)
                recurring_restored += 1
            elif timer_data.get("status") == "active":
                entity_id = timer_data["entity_id"]
                end_time = self.iso_to_datetime(timer_data["end_time"])
                now = self.get_local_now()
                if end_time > now:
                    timer_handle = async_track_point_in_time(
                        self.hass, lambda n, tid=timer_id: self.execute_timer(n, tid), end_time
                    )
                    self.timers[timer_id] = timer_handle
                    start_time = self.iso_to_datetime(timer_data["start_time"])
                    pre_time = end_time - timedelta(seconds=10)
                    if pre_time > start_time:
                        pre_h = async_track_point_in_time(
                            self.hass, lambda n, tid=timer_id: self._pre_capture_state(n, tid), pre_time
                        )
                        self.timers[f"{timer_id}_pre"] = pre_h
                    self.entity_timers[entity_id] = timer_id
                    self.tasks[timer_id] = timer_data
                    restored += 1
                else:
                    self.tasks[timer_id] = timer_data
                    if timer_data.get("is_climate"):
                        self.hass.async_create_task(self._async_execute_climate_timer(timer_id))
                    elif timer_data.get("is_cover"):
                        self.hass.async_create_task(self._async_execute_cover_timer(timer_id))
                    else:
                        self.hass.async_create_task(self._async_execute_timer(timer_id))
            else:
                self.tasks[timer_id] = timer_data

        _LOGGER.info(f"[timer_elves] Restored {restored} timers + {recurring_restored} schedules (total {len(data)})")

    async def _restore_recurring_timer(self, timer_id: str, timer_data: dict) -> None:
        self.tasks[timer_id] = timer_data
        if timer_data.get("status") == "active":
            await self.schedule_recurring_timer(timer_id, timer_data)

    # ================================================================== #
    #  时区辅助                                                             #
    # ================================================================== #

    def get_local_now(self) -> datetime:
        return dt_util.now(self.tz)

    def parse_local_time(self, time_str: str, date_obj: datetime = None) -> datetime:
        if not date_obj:
            date_obj = self.get_local_now()
        h, m, s = map(int, time_str.split(":"))
        return date_obj.replace(hour=h, minute=m, second=s, microsecond=0)

    def datetime_to_iso(self, dt: datetime) -> str:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=self.tz)
        local_dt = dt.astimezone(self.tz)
        return local_dt.strftime("%Y-%m-%dT%H:%M:%S")

    def iso_to_datetime(self, iso_str: str) -> datetime:
        try:
            if iso_str.endswith("Z"):
                iso_str = iso_str.replace("Z", "+00:00")
                return datetime.fromisoformat(iso_str).astimezone(self.tz)
            try:
                dt = datetime.fromisoformat(iso_str)
                if dt.tzinfo is None:
                    return dt.replace(tzinfo=self.tz)
                return dt.astimezone(self.tz)
            except Exception:
                pass
            dt = datetime.strptime(iso_str, "%Y-%m-%dT%H:%M:%S")
            return dt.replace(tzinfo=self.tz)
        except Exception as e:
            _LOGGER.warning(f"[timer_elves] parse datetime failed: {iso_str}: {e}")
            return self.get_local_now()

    # ================================================================== #
    #  事件监听                                                             #
    # ================================================================== #

    async def _handle_state_changed(self, event) -> None:
        entity_id = event.data.get("entity_id")
        if entity_id and entity_id.startswith("climate."):
            await self._handle_climate_state_change(entity_id)
        elif entity_id and entity_id.startswith("cover."):
            await self._handle_cover_state_change(entity_id)

    async def _handle_climate_state_change(self, entity_id: str) -> None:
        if entity_id not in self.climate_previous_states:
            state = self.hass.states.get(entity_id)
            if state:
                self.climate_previous_states[entity_id] = {
                    "hvac_mode": state.attributes.get("hvac_mode"),
                    "temperature": state.attributes.get("temperature"),
                    "fan_mode": state.attributes.get("fan_mode"),
                    "swing_mode": state.attributes.get("swing_mode"),
                    "preset_mode": state.attributes.get("preset_mode"),
                    "saved_at": self.datetime_to_iso(self.get_local_now()),
                }

    async def _handle_cover_state_change(self, entity_id: str) -> None:
        if entity_id not in self.cover_previous_states:
            state = self.hass.states.get(entity_id)
            if state:
                self.cover_previous_states[entity_id] = {
                    "state": state.state,
                    "current_position": state.attributes.get("current_position", 0),
                    "saved_at": self.datetime_to_iso(self.get_local_now()),
                }

    async def _handle_frontend_event(self, event) -> None:
        """处理前端发来的事件（ha_data_store_timer_event）。"""
        data = event.data
        action = data.get("action")
        if action == "create_timer":
            await self.create_timer(data)
        elif action == "get_all_timers":
            await self.send_all_timers(data.get("user_id"))
        elif action == "cancel_timer":
            await self.cancel_timer(data.get("timer_id"))
        elif action == "cancel_entity_timer":
            await self.cancel_entity_timer(data.get("entity_id"), data.get("user_id"))
        elif action == "create_climate_timer":
            await self.create_climate_timer(data)
        elif action == "create_cover_timer":
            await self.create_cover_timer(data)
        elif action == "create_schedule":
            await self.create_schedule(data)
        elif action == "cancel_schedule":
            await self.cancel_schedule(data.get("schedule_id"))
        elif action == "get_all_schedules":
            await self.send_all_schedules(data.get("user_id"))
        elif action == "clear_all_history":
            await self._clear_all_history()

    # ================================================================== #
    #  创建定时器（一次性）                                                   #
    # ================================================================== #

    async def create_timer(self, data: dict) -> None:
        try:
            entity_id = data.get("entity_id")
            duration_str = data.get("duration", "00:30:00")
            if not entity_id:
                raise ValueError("Entity ID is required")
            if self.hass.states.get(entity_id) is None:
                raise ValueError(f"Entity {entity_id} does not exist")
            if entity_id.startswith("climate."):
                return await self.create_climate_timer(data)
            if entity_id.startswith("cover."):
                return await self.create_cover_timer(data)

            duration = self.parse_duration(duration_str)
            if entity_id in self.entity_timers:
                await self.cancel_entity_timer(entity_id, data.get("user_id"))

            timer_id = str(uuid.uuid4())
            start_time = self.get_local_now()
            end_time = start_time + duration
            entity_state = self.hass.states.get(entity_id)
            state = entity_state.state if entity_state else "unknown"

            timer_data = {
                "timer_id": timer_id,
                "entity_id": entity_id,
                "duration": duration_str,
                "start_time": self.datetime_to_iso(start_time),
                "end_time": self.datetime_to_iso(end_time),
                "status": "active",
                "entity_name": self.get_friendly_name(entity_id),
                "entity_state": state,
                "domain": entity_id.split(".")[0],
                "created_by": data.get("user_id", "unknown"),
                "created_at": self.datetime_to_iso(self.get_local_now()),
                "action": self.generate_action(entity_id, data.get("action_type", "auto")),
                "repeat_type": "none",
                "is_recurring": False,
            }
            timer_handle = async_track_point_in_time(
                self.hass, lambda n: self.execute_timer(n, timer_id), end_time
            )
            pre_time = end_time - timedelta(seconds=10)
            if pre_time > start_time:
                pre_h = async_track_point_in_time(
                    self.hass, lambda n: self._pre_capture_state(n, timer_id), pre_time
                )
                self.timers[f"{timer_id}_pre"] = pre_h
            self.timers[timer_id] = timer_handle
            self.entity_timers[entity_id] = timer_id
            self.tasks[timer_id] = timer_data
            await self.save_tasks()
            await self._update_sensor()

            self._fire_event("timer_created", {
                "timer_id": timer_id,
                "entity_id": entity_id,
                "entity_name": timer_data["entity_name"],
                "duration": duration_str,
                "end_time": self.datetime_to_iso(end_time),
                "status": "active",
                "action_description": self.get_action_description(timer_data["action"]),
                "message": f"Timer set for {timer_data['entity_name']}",
                "time_zone": self.time_zone,
            })
            await self.send_all_timers()
            _LOGGER.info(f"[timer_elves] Timer created: {entity_id} - {duration_str}")
        except Exception as e:
            _LOGGER.error(f"[timer_elves] create_timer failed: {e}")
            self._fire_event("error", {"error": str(e), "success": False})

    async def create_climate_timer(self, data: dict) -> None:
        try:
            entity_id = data.get("entity_id")
            duration_str = data.get("duration", "01:00:00")
            action_type = data.get("action_type", "turn_off")
            if not entity_id:
                raise ValueError("Climate entity ID is required")
            if not entity_id.startswith("climate."):
                raise ValueError("Climate entity required")
            if self.hass.states.get(entity_id) is None:
                raise ValueError(f"Climate entity {entity_id} does not exist")
            repeat_type = data.get("repeat_type", "none")
            schedule_time = data.get("schedule_time")
            if repeat_type != "none" and schedule_time:
                return await self.create_schedule(data)

            duration = self.parse_duration(duration_str)
            if entity_id in self.entity_timers:
                await self.cancel_entity_timer(entity_id, data.get("user_id"))
            timer_id = str(uuid.uuid4())
            start_time = self.get_local_now()
            end_time = start_time + duration
            state = self.hass.states.get(entity_id)
            current_attrs = state.attributes if state else {}
            current_state = state.state if state else "off"

            if self.climate_config["save_state_on_timer"]:
                self.climate_previous_states[entity_id] = {
                    "hvac_mode": current_attrs.get("hvac_mode", "off"),
                    "temperature": current_attrs.get("temperature"),
                    "fan_mode": current_attrs.get("fan_mode"),
                    "swing_mode": current_attrs.get("swing_mode"),
                    "preset_mode": current_attrs.get("preset_mode"),
                    "current_temperature": current_attrs.get("current_temperature"),
                    "saved_at": self.datetime_to_iso(self.get_local_now()),
                }
            action = self.generate_climate_action(entity_id, action_type, data.get("action_data", {}))
            timer_data = {
                "timer_id": timer_id, "entity_id": entity_id,
                "duration": duration_str, "start_time": self.datetime_to_iso(start_time),
                "end_time": self.datetime_to_iso(end_time), "status": "active",
                "entity_name": self.get_friendly_name(entity_id),
                "entity_state": current_state, "domain": "climate",
                "created_by": data.get("user_id", "unknown"),
                "created_at": self.datetime_to_iso(self.get_local_now()),
                "action": action,
                "previous_state": self.climate_previous_states.get(entity_id, {}),
                "is_climate": True, "repeat_type": "none", "is_recurring": False,
            }
            timer_handle = async_track_point_in_time(
                self.hass, lambda n: self.execute_climate_timer(n, timer_id), end_time
            )
            self.timers[timer_id] = timer_handle
            self.entity_timers[entity_id] = timer_id
            self.tasks[timer_id] = timer_data
            await self.save_tasks()
            await self._update_sensor()
            self._fire_event("timer_created", {
                "timer_id": timer_id, "entity_id": entity_id,
                "entity_name": timer_data["entity_name"], "duration": duration_str,
                "end_time": self.datetime_to_iso(end_time), "status": "active",
                "action_description": self.get_climate_action_description(action),
                "previous_mode": timer_data["previous_state"].get("hvac_mode", "Unknown"),
                "target_action": action_type,
                "message": f"Climate timer set for {timer_data['entity_name']}",
                "time_zone": self.time_zone,
            })
            await self.send_all_timers()
            _LOGGER.info(f"[timer_elves] Climate timer created: {entity_id} - {duration_str}")
        except Exception as e:
            _LOGGER.error(f"[timer_elves] create_climate_timer failed: {e}")
            self._fire_event("error", {"error": str(e), "success": False})

    async def create_cover_timer(self, data: dict) -> None:
        try:
            entity_id = data.get("entity_id")
            duration_str = data.get("duration", "00:30:00")
            action_type = data.get("action_type", "close")
            if not entity_id:
                raise ValueError("Cover entity ID is required")
            if not entity_id.startswith("cover."):
                raise ValueError("Cover entity required")
            if self.hass.states.get(entity_id) is None:
                raise ValueError(f"Cover entity {entity_id} does not exist")
            repeat_type = data.get("repeat_type", "none")
            schedule_time = data.get("schedule_time")
            if repeat_type != "none" and schedule_time:
                return await self.create_schedule(data)

            duration = self.parse_duration(duration_str)
            if entity_id in self.entity_timers:
                await self.cancel_entity_timer(entity_id, data.get("user_id"))
            timer_id = str(uuid.uuid4())
            start_time = self.get_local_now()
            end_time = start_time + duration
            state = self.hass.states.get(entity_id)
            current_attrs = state.attributes if state else {}
            current_state = state.state if state else "closed"

            if self.cover_config["save_state_on_timer"]:
                self.cover_previous_states[entity_id] = {
                    "state": current_state,
                    "current_position": current_attrs.get("current_position", 0),
                    "saved_at": self.datetime_to_iso(self.get_local_now()),
                }
            action = self.generate_cover_action(entity_id, action_type, data.get("action_data", {}))
            timer_data = {
                "timer_id": timer_id, "entity_id": entity_id,
                "duration": duration_str, "start_time": self.datetime_to_iso(start_time),
                "end_time": self.datetime_to_iso(end_time), "status": "active",
                "entity_name": self.get_friendly_name(entity_id),
                "entity_state": current_state, "domain": "cover",
                "created_by": data.get("user_id", "unknown"),
                "created_at": self.datetime_to_iso(self.get_local_now()),
                "action": action,
                "previous_state": self.cover_previous_states.get(entity_id, {}),
                "is_cover": True, "repeat_type": "none", "is_recurring": False,
            }
            timer_handle = async_track_point_in_time(
                self.hass, lambda n: self.execute_cover_timer(n, timer_id), end_time
            )
            self.timers[timer_id] = timer_handle
            self.entity_timers[entity_id] = timer_id
            self.tasks[timer_id] = timer_data
            await self.save_tasks()
            await self._update_sensor()
            self._fire_event("timer_created", {
                "timer_id": timer_id, "entity_id": entity_id,
                "entity_name": timer_data["entity_name"], "duration": duration_str,
                "end_time": self.datetime_to_iso(end_time), "status": "active",
                "action_description": self.get_cover_action_description(action),
                "previous_position": timer_data["previous_state"].get("current_position", 0),
                "target_action": action_type,
                "message": f"Cover timer set for {timer_data['entity_name']}",
                "time_zone": self.time_zone,
            })
            await self.send_all_timers()
            _LOGGER.info(f"[timer_elves] Cover timer created: {entity_id} - {duration_str}")
        except Exception as e:
            _LOGGER.error(f"[timer_elves] create_cover_timer failed: {e}")
            self._fire_event("error", {"error": str(e), "success": False})

    # ================================================================== #
    #  创建周期任务                                                          #
    # ================================================================== #

    async def create_schedule(self, data: dict) -> None:
        try:
            entity_id = data.get("entity_id")
            repeat_type = data.get("repeat_type", "none")
            schedule_time = data.get("schedule_time")
            action_type = data.get("action_type", "auto")
            if not entity_id:
                raise ValueError("Entity ID is required")
            if repeat_type == "none":
                raise ValueError("Repeat type must be specified for schedule")
            if not schedule_time:
                raise ValueError("Schedule time must be specified")
            if self.hass.states.get(entity_id) is None:
                raise ValueError(f"Entity {entity_id} does not exist")

            schedule_id = str(uuid.uuid4())
            time_parts = schedule_time.split(":")
            if len(time_parts) != 3:
                raise ValueError("Schedule time must be in HH:MM:SS format")
            _ = list(map(int, time_parts))
            entity_state = self.hass.states.get(entity_id)
            state = entity_state.state if entity_state else "unknown"

            schedule_data = {
                "schedule_id": schedule_id, "entity_id": entity_id,
                "repeat_type": repeat_type, "schedule_time": schedule_time,
                "status": "active", "entity_name": self.get_friendly_name(entity_id),
                "entity_state": state, "domain": entity_id.split(".")[0],
                "created_by": data.get("user_id", "unknown"),
                "created_at": self.datetime_to_iso(self.get_local_now()),
                "action_type": action_type, "action_data": data.get("action_data", {}),
                "is_recurring": True, "last_executed": None, "next_execution": None,
                "time_zone": self.time_zone,
            }
            if repeat_type == "weekly":
                weekdays = data.get("weekdays", [])
                if not weekdays:
                    raise ValueError("Weekdays must be specified for weekly schedule")
                schedule_data["weekdays"] = weekdays
            elif repeat_type == "monthly":
                month_days = data.get("month_days", [])
                if not month_days:
                    raise ValueError("Month days must be specified for monthly schedule")
                schedule_data["month_days"] = month_days

            if entity_id.startswith("climate."):
                s = self.hass.states.get(entity_id)
                ca = s.attributes if s else {}
                if self.climate_config["save_state_on_timer"]:
                    schedule_data["previous_state"] = {
                        "hvac_mode": ca.get("hvac_mode", "off"),
                        "temperature": ca.get("temperature"),
                        "fan_mode": ca.get("fan_mode"),
                        "swing_mode": ca.get("swing_mode"),
                        "preset_mode": ca.get("preset_mode"),
                        "saved_at": self.datetime_to_iso(self.get_local_now()),
                    }
                schedule_data["is_climate"] = True
                schedule_data["is_cover"] = False
            elif entity_id.startswith("cover."):
                s = self.hass.states.get(entity_id)
                ca = s.attributes if s else {}
                if self.cover_config["save_state_on_timer"]:
                    schedule_data["previous_state"] = {
                        "state": s.state, "current_position": ca.get("current_position", 0),
                        "saved_at": self.datetime_to_iso(self.get_local_now()),
                    }
                schedule_data["is_climate"] = False
                schedule_data["is_cover"] = True
            else:
                schedule_data["is_climate"] = False
                schedule_data["is_cover"] = False

            await self.schedule_recurring_timer(schedule_id, schedule_data)
            self.tasks[schedule_id] = schedule_data
            await self.save_tasks()
            await self._update_sensor()

            resp = {
                "action": "schedule_created", "schedule_id": schedule_id,
                "entity_id": entity_id, "entity_name": schedule_data["entity_name"],
                "repeat_type": repeat_type, "schedule_time": schedule_time,
                "status": "active", "next_execution": schedule_data.get("next_execution"),
                "message": f"Schedule created for {schedule_data['entity_name']}",
                "time_zone": self.time_zone,
            }
            if repeat_type == "weekly":
                resp["weekdays"] = schedule_data.get("weekdays", [])
            elif repeat_type == "monthly":
                resp["month_days"] = schedule_data.get("month_days", [])
            self._fire_event("schedule_created", resp)
            await self.send_all_timers()
            _LOGGER.info(f"[timer_elves] Schedule created: {entity_id} - {repeat_type} at {schedule_time}")
        except Exception as e:
            _LOGGER.error(f"[timer_elves] create_schedule failed: {e}")
            self._fire_event("error", {"error": str(e), "success": False})

    # ================================================================== #
    #  周期调度引擎                                                          #
    # ================================================================== #

    async def schedule_recurring_timer(self, schedule_id: str, schedule_data: dict) -> None:
        try:
            repeat_type = schedule_data["repeat_type"]
            schedule_time = schedule_data["schedule_time"]
            next_execution = self.calculate_next_execution(repeat_type, schedule_time, schedule_data)
            if not next_execution:
                raise ValueError("无法计算下次执行时间")
            now = self.get_local_now()
            delay = (next_execution - now).total_seconds()
            if delay < 0:
                await self.check_recurring_schedules()
                return
            if schedule_id in self.recurring_timers:
                if self.recurring_timers[schedule_id]:
                    self.recurring_timers[schedule_id]()
            pre_key = f"{schedule_id}_pre"
            if pre_key in self.recurring_timers:
                self.recurring_timers[pre_key]()
                del self.recurring_timers[pre_key]
            pre_time = next_execution - timedelta(seconds=10)
            if pre_time > now:
                pre_h = async_track_point_in_time(
                    self.hass, lambda n: self._pre_capture_state(n, schedule_id), pre_time
                )
                self.recurring_timers[pre_key] = pre_h
            timer_handle = async_track_point_in_time(
                self.hass, lambda n: self.execute_recurring_schedule(n, schedule_id), next_execution
            )
            schedule_data["next_execution"] = self.datetime_to_iso(next_execution)
            self.recurring_timers[schedule_id] = timer_handle
        except Exception as e:
            _LOGGER.error(f"[timer_elves] schedule_recurring_timer failed: {e}")

    def calculate_next_execution(self, repeat_type: str, schedule_time: str, schedule_data: dict) -> Optional[datetime]:
        now = self.get_local_now()
        hour, minute, second = map(int, schedule_time.split(":"))
        if repeat_type == "daily":
            today_time = self.parse_local_time(schedule_time, now)
            if today_time <= now:
                return self.parse_local_time(schedule_time, now + timedelta(days=1))
            return today_time
        elif repeat_type == "weekly":
            weekdays = schedule_data.get("weekdays", [])
            if not weekdays:
                return None
            target_days = [self.parse_weekday(d) for d in weekdays]
            for offset in range(7):
                check_date = now + timedelta(days=offset)
                if check_date.weekday() in target_days:
                    check_time = self.parse_local_time(schedule_time, check_date)
                    if offset == 0 and check_time <= now:
                        continue
                    return check_time
            return None
        elif repeat_type == "monthly":
            month_days = schedule_data.get("month_days", [])
            if not month_days:
                return None
            import calendar
            cy, cm = now.year, now.month
            for mo in range(12):
                check_year = cy + ((cm - 1 + mo) // 12)
                check_month = ((cm - 1 + mo) % 12) + 1
                dim = calendar.monthrange(check_year, check_month)[1]
                for day in sorted(month_days):
                    if day > dim:
                        continue
                    try:
                        check_date = datetime(check_year, check_month, day, tzinfo=self.tz)
                        check_time = self.parse_local_time(schedule_time, check_date)
                        if check_time > now:
                            return check_time
                    except Exception:
                        continue
            return None
        return None

    def parse_weekday(self, weekday_str: str) -> int:
        m = {
            "monday": 0, "mon": 0,
            "tuesday": 1, "tue": 1,
            "wednesday": 2, "wed": 2,
            "thursday": 3, "thu": 3,
            "friday": 4, "fri": 4,
            "saturday": 5, "sat": 5,
            "sunday": 6, "sun": 6,
        }
        return m.get(weekday_str.lower(), 0)

    def execute_recurring_schedule(self, now, schedule_id: str, *args, **kwargs) -> None:
        if schedule_id not in self.tasks:
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self._async_execute_recurring_schedule(schedule_id), self.hass.loop
            )
        except RuntimeError:
            self.hass.async_create_task(self._async_execute_recurring_schedule(schedule_id))

    async def _async_execute_recurring_schedule(self, schedule_id: str) -> None:
        if schedule_id not in self.tasks:
            return
        sd = self.tasks[schedule_id]
        if sd.get("status") != "active":
            return
        entity_id = sd["entity_id"]
        action_type = sd.get("action_type", "auto")
        before_entity_state = sd.get("before_entity_state", "unknown")
        if before_entity_state == "unknown":
            bs = self.hass.states.get(entity_id)
            before_entity_state = bs.state if bs else "unknown"

        try:
            sd["last_executed"] = self.datetime_to_iso(self.get_local_now())
            success = False
            action = None

            if sd.get("is_climate"):
                action_data = sd.get("action_data", {})
                action = self.generate_climate_action(entity_id, action_type, action_data)
                if action["type"] == "service_call":
                    domain, service = action["service"].split(".")
                    sd2 = action.get("data", {}).copy()
                    if action_type == "restore_previous" and "restore_data" in action:
                        rd = action["restore_data"]
                        if rd.get("temperature"):
                            await self.hass.services.async_call("climate", "set_temperature", {"entity_id": entity_id, "temperature": rd["temperature"]})
                        if rd.get("fan_mode"):
                            await self.hass.services.async_call("climate", "set_fan_mode", {"entity_id": entity_id, "fan_mode": rd["fan_mode"]})
                        if rd.get("hvac_mode"):
                            await self.hass.services.async_call("climate", "set_hvac_mode", {"entity_id": entity_id, "hvac_mode": rd["hvac_mode"]})
                        success = True
                    else:
                        await self.hass.services.async_call(domain, service, sd2)
                        success = True
            elif sd.get("is_cover"):
                action_data = sd.get("action_data", {})
                action = self.generate_cover_action(entity_id, action_type, action_data)
                if action["type"] == "service_call":
                    domain, service = action["service"].split(".")
                    sd2 = action.get("data", {}).copy()
                    if action_type == "restore_previous" and "restore_data" in action:
                        pos = action["restore_data"].get("current_position")
                        if pos is not None:
                            await self.hass.services.async_call("cover", "set_cover_position", {"entity_id": entity_id, "position": pos})
                            success = True
                    else:
                        await self.hass.services.async_call(domain, service, sd2)
                        success = True
            else:
                action = self.generate_action(entity_id, action_type)
                if action["type"] == "service_call":
                    domain, service = action["service"].split(".")
                    await self.hass.services.async_call(domain, service, action.get("data", {}))
                    success = True

            if success:
                await asyncio.sleep(2)
            after_state = self.hass.states.get(entity_id)
            after_entity_state = after_state.state if after_state else "unknown"
            await self._add_history_record(
                timer_id=schedule_id, entity_id=entity_id,
                entity_name=sd.get("entity_name", entity_id),
                task_action=action.get("description", action.get("service", "unknown")) if action else "unknown",
                before_entity_state=before_entity_state,
                after_entity_state=after_entity_state,
                execution_result="success" if success else "failed",
                start_time=sd.get("last_executed", ""), end_time="",
                creation_time=sd.get("created_at", ""),
            )
            await self.update_stats()
            self._fire_event("schedule_executed", {
                "schedule_id": schedule_id, "entity_id": entity_id,
                "entity_name": sd["entity_name"], "repeat_type": sd["repeat_type"],
                "success": success, "before_entity_state": before_entity_state,
                "after_entity_state": after_entity_state,
                "message": f"Schedule executed for {sd['entity_name']}",
                "time_zone": self.time_zone,
            })
            await self.reschedule_recurring_timer(schedule_id, sd)
        except Exception as e:
            _LOGGER.error(f"[timer_elves] execute recurring schedule failed: {e}")
            after_state = self.hass.states.get(entity_id)
            after_entity_state = after_state.state if after_state else "unknown"
            await self._add_history_record(
                timer_id=schedule_id, entity_id=entity_id,
                entity_name=sd.get("entity_name", entity_id),
                task_action=action.get("description", "unknown") if action else "unknown",
                before_entity_state=before_entity_state,
                after_entity_state=after_entity_state,
                execution_result="failed",
                start_time=sd.get("last_executed", ""), end_time="",
                creation_time=sd.get("created_at", ""),
            )
            await self.update_stats()
            try:
                await self.reschedule_recurring_timer(schedule_id, sd)
            except Exception as re:
                _LOGGER.error(f"[timer_elves] reschedule after error failed: {re}")

    async def reschedule_recurring_timer(self, schedule_id: str, schedule_data: dict) -> None:
        try:
            next_execution = self.calculate_next_execution(
                schedule_data["repeat_type"], schedule_data["schedule_time"], schedule_data
            )
            if not next_execution:
                return
            now = self.get_local_now()
            delay = (next_execution - now).total_seconds()
            if schedule_id in self.recurring_timers:
                if self.recurring_timers[schedule_id]:
                    self.recurring_timers[schedule_id]()
            if delay > 0:
                timer_handle = async_track_point_in_time(
                    self.hass, lambda n: self.execute_recurring_schedule(n, schedule_id), next_execution
                )
                self.recurring_timers[schedule_id] = timer_handle
                schedule_data["next_execution"] = self.datetime_to_iso(next_execution)
                if "before_entity_state" in schedule_data:
                    del schedule_data["before_entity_state"]
            else:
                schedule_data["next_execution"] = None
            await self.save_tasks()
        except Exception as e:
            _LOGGER.error(f"[timer_elves] reschedule_recurring_timer failed: {e}")

    async def check_recurring_schedules(self) -> None:
        try:
            now = self.get_local_now()
            _LOGGER.debug(f"[timer_elves] Checking recurring schedules at {now}")
            for sid, sd in self.tasks.items():
                if sd.get("is_recurring") and sd.get("status") == "active":
                    nex = sd.get("next_execution")
                    if not nex:
                        await self.schedule_recurring_timer(sid, sd)
                    else:
                        try:
                            if self.iso_to_datetime(nex) <= now:
                                await self.schedule_recurring_timer(sid, sd)
                        except Exception:
                            await self.schedule_recurring_timer(sid, sd)
        except Exception as e:
            _LOGGER.error(f"[timer_elves] check_recurring_schedules failed: {e}")

    async def _clear_all_history(self) -> None:
        """清除所有历史记录（保留活跃任务）。"""
        try:
            active_tasks = {}
            for tid, td in self.tasks.items():
                if td.get("status") == "active":
                    active_tasks[tid] = td
            self.tasks = active_tasks
            await self.update_stats()
            await self.save_tasks()
            await self._update_sensor()
            _LOGGER.info("[timer_elves] All history cleared")
        except Exception as e:
            _LOGGER.error(f"[timer_elves] clear_all_history failed: {e}")

    # ================================================================== #
    #  动作生成                                                             #
    # ================================================================== #

    def generate_action(self, entity_id: str, action_type: str = "auto", current_state: str = None) -> dict:
        domain = entity_id.split(".")[0]
        domain_actions = self.default_actions.get(domain, {})
        if current_state is None:
            st = self.hass.states.get(entity_id)
            current_state = st.state if st else "unknown"
        if domain == "climate":
            return self.generate_climate_action(entity_id, action_type)
        if domain == "cover":
            return self.generate_cover_action(entity_id, action_type)
        if not isinstance(current_state, str):
            current_state = "unknown"

        if action_type == "auto":
            if domain in self.default_actions:
                if current_state == "on" and "turn_off" in domain_actions:
                    ac = domain_actions["turn_off"]
                elif current_state == "off" and "turn_on" in domain_actions:
                    ac = domain_actions["turn_on"]
                else:
                    ac = domain_actions.get("turn_off", {"service": f"{domain}.turn_off"})
                return {"type": "service_call", "service": ac["service"],
                        "data": {**ac.get("data", {}), "entity_id": entity_id},
                        "description": ac.get("description", "Auto action")}
            if domain == "light":
                return {"type": "service_call",
                        "service": "light.turn_off" if current_state == "on" else "light.turn_on",
                        "data": {"entity_id": entity_id},
                        "description": "Turn off" if current_state == "on" else "Turn on"}
            if domain == "switch":
                return {"type": "service_call",
                        "service": "switch.turn_off" if current_state == "on" else "switch.turn_on",
                        "data": {"entity_id": entity_id},
                        "description": "Turn off" if current_state == "on" else "Turn on"}
            if domain == "media_player":
                if current_state == "playing":
                    return {"type": "service_call", "service": "media_player.media_pause",
                            "data": {"entity_id": entity_id}, "description": "Pause"}
                return {"type": "service_call", "service": "media_player.turn_off",
                        "data": {"entity_id": entity_id}, "description": "Turn off"}
            if domain == "input_boolean":
                return {"type": "service_call",
                        "service": "input_boolean.turn_off" if current_state == "on" else "input_boolean.turn_on",
                        "data": {"entity_id": entity_id},
                        "description": "Turn off" if current_state == "on" else "Turn on"}
            return {"type": "service_call", "service": f"{domain}.turn_off",
                    "data": {"entity_id": entity_id}, "description": "Turn off"}

        if action_type == "toggle":
            if "toggle" in domain_actions:
                ac = domain_actions["toggle"]
                return {"type": "service_call", "service": ac["service"],
                        "data": {**ac.get("data", {}), "entity_id": entity_id},
                        "description": ac.get("description", "Toggle")}
            return {"type": "service_call", "service": f"{domain}.toggle",
                    "data": {"entity_id": entity_id}, "description": "Toggle"}

        if action_type == "turn_off":
            if "turn_off" in domain_actions:
                ac = domain_actions["turn_off"]
                return {"type": "service_call", "service": ac["service"],
                        "data": {**ac.get("data", {}), "entity_id": entity_id},
                        "description": ac.get("description", "Turn off")}
            return {"type": "service_call", "service": f"{domain}.turn_off",
                    "data": {"entity_id": entity_id}, "description": "Turn off"}

        if action_type == "turn_on":
            if "turn_on" in domain_actions:
                ac = domain_actions["turn_on"]
                return {"type": "service_call", "service": ac["service"],
                        "data": {**ac.get("data", {}), "entity_id": entity_id},
                        "description": ac.get("description", "Turn on")}
            return {"type": "service_call", "service": f"{domain}.turn_on",
                    "data": {"entity_id": entity_id}, "description": "Turn on"}

        return {"type": "service_call", "service": f"{domain}.turn_off",
                "data": {"entity_id": entity_id}, "description": "Turn off"}

    def generate_climate_action(self, entity_id: str, action_type: str = "turn_off", action_data: dict = None) -> dict:
        action_data = action_data or {}
        ca = self.default_actions.get("climate", {})
        if action_type == "turn_off":
            if "turn_off" in ca:
                return {"type": "service_call", "service": ca["turn_off"]["service"],
                        "data": {**ca["turn_off"].get("data", {}), "entity_id": entity_id},
                        "description": ca["turn_off"].get("description", "Turn off AC")}
            return {"type": "service_call", "service": "climate.turn_off",
                    "data": {"entity_id": entity_id}, "description": "Turn off AC"}
        if action_type == "set_temperature":
            temp = action_data.get("temperature", self.climate_config["default_temperature"])
            mode = action_data.get("hvac_mode", self.climate_config["default_mode"])
            if "set_temperature" in ca:
                return {"type": "service_call", "service": ca["set_temperature"]["service"],
                        "data": {**ca["set_temperature"].get("data", {}), "entity_id": entity_id, "temperature": temp, "hvac_mode": mode},
                        "description": ca["set_temperature"].get("description", f"Set temp to {temp}°C")}
            return {"type": "service_call", "service": "climate.set_temperature",
                    "data": {"entity_id": entity_id, "temperature": temp, "hvac_mode": mode},
                    "description": f"Set temp to {temp}°C"}
        if action_type == "set_mode":
            mode = action_data.get("mode", "cool")
            if "set_mode" in ca:
                return {"type": "service_call", "service": ca["set_mode"]["service"],
                        "data": {**ca["set_mode"].get("data", {}), "entity_id": entity_id, "hvac_mode": mode},
                        "description": ca["set_mode"].get("description", f"Set mode to {mode}")}
            return {"type": "service_call", "service": "climate.set_hvac_mode",
                    "data": {"entity_id": entity_id, "hvac_mode": mode},
                    "description": f"Set mode to {mode}"}
        if action_type == "restore_previous":
            ps = self.climate_previous_states.get(entity_id, {})
            mode = ps.get("hvac_mode", "cool")
            return {"type": "service_call", "service": "climate.set_hvac_mode",
                    "data": {"entity_id": entity_id, "hvac_mode": mode},
                    "restore_data": ps,
                    "description": f"Restore previous ({mode})"}
        if action_type == "auto":
            st = self.hass.states.get(entity_id)
            cs = st.state if st else "off"
            if cs == "off":
                return self.generate_climate_action(entity_id, "restore_previous")
            return self.generate_climate_action(entity_id, "turn_off")
        return self.generate_climate_action(entity_id, "turn_off")

    def get_available_cover_service(self, preferred: str, fallback: str) -> str:
        try:
            d, s = preferred.split(".")
            if self.hass.services.has_service(d, s):
                return preferred
            d2, s2 = fallback.split(".")
            if self.hass.services.has_service(d2, s2):
                return fallback
            return preferred
        except Exception:
            return fallback

    def generate_cover_action(self, entity_id: str, action_type: str = "close", action_data: dict = None) -> dict:
        action_data = action_data or {}
        ca = self.default_actions.get("cover", {})

        if action_type == "close":
            if "close" in ca:
                s = self.get_available_cover_service(ca["close"]["service"], "cover.close_cover")
                return {"type": "service_call", "service": s,
                        "data": {**ca["close"].get("data", {}), "entity_id": entity_id},
                        "description": ca["close"].get("description", "Close cover")}
            s = self.get_available_cover_service("cover.close_cover", "cover.close")
            return {"type": "service_call", "service": s, "data": {"entity_id": entity_id},
                    "description": "Close cover"}
        if action_type == "open":
            if "open" in ca:
                s = self.get_available_cover_service(ca["open"]["service"], "cover.open_cover")
                return {"type": "service_call", "service": s,
                        "data": {**ca["open"].get("data", {}), "entity_id": entity_id},
                        "description": ca["open"].get("description", "Open cover")}
            s = self.get_available_cover_service("cover.open_cover", "cover.open")
            return {"type": "service_call", "service": s, "data": {"entity_id": entity_id},
                    "description": "Open cover"}
        if action_type == "set_position":
            pos = action_data.get("position")
            if pos is None:
                st = self.hass.states.get(entity_id)
                pos = st.attributes.get("current_position", 0) if st else 0
            if "set_position" in ca:
                s = self.get_available_cover_service(ca["set_position"]["service"], "cover.set_cover_position")
                return {"type": "service_call", "service": s,
                        "data": {**ca["set_position"].get("data", {}), "entity_id": entity_id, "position": pos},
                        "description": ca["set_position"].get("description", f"Set position to {pos}%")}
            s = self.get_available_cover_service("cover.set_cover_position", "cover.set_position")
            return {"type": "service_call", "service": s,
                    "data": {"entity_id": entity_id, "position": pos},
                    "description": f"Set position to {pos}%"}
        if action_type == "restore_previous":
            ps = self.cover_previous_states.get(entity_id, {})
            pos = ps.get("current_position", 0)
            if not ps or pos == 0:
                st = self.hass.states.get(entity_id)
                if st:
                    pos = st.attributes.get("current_position", 0)
            s = self.get_available_cover_service("cover.set_cover_position", "cover.set_position")
            return {"type": "service_call", "service": s,
                    "data": {"entity_id": entity_id, "position": pos},
                    "restore_data": ps,
                    "description": f"Restore position ({pos}%)"}
        if action_type == "auto":
            st = self.hass.states.get(entity_id)
            cs = st.state if st else "closed"
            if cs == "closed":
                return self.generate_cover_action(entity_id, "restore_previous")
            return self.generate_cover_action(entity_id, "close")
        return self.generate_cover_action(entity_id, "close")

    # ================================================================== #
    #  执行定时器（一次性）                                                   #
    # ================================================================== #

    def execute_timer(self, now, timer_id: str, *args, **kwargs) -> None:
        if timer_id in self.tasks:
            asyncio.run_coroutine_threadsafe(
                self._async_execute_timer(timer_id), self.hass.loop
            )

    async def _async_execute_timer(self, timer_id: str) -> None:
        if timer_id not in self.tasks:
            return
        timer = self.tasks[timer_id]
        entity_id = timer["entity_id"]
        if timer.get("status") == "cancelled":
            return
        before_entity_state = timer.get("before_entity_state", "unknown")
        if before_entity_state == "unknown":
            bs = self.hass.states.get(entity_id)
            before_entity_state = bs.state if bs else "unknown"
        try:
            action = timer["action"]
            success = False
            if action["type"] == "service_call":
                domain, service = action["service"].split(".")
                await self.hass.services.async_call(domain, service, action.get("data", {}))
                success = True
            if success:
                await asyncio.sleep(2)
            after_state = self.hass.states.get(entity_id)
            after_entity_state = after_state.state if after_state else "unknown"
            if success:
                timer["status"] = "completed"
                timer["executed_at"] = self.datetime_to_iso(self.get_local_now())
                timer["execution_result"] = "success"
            else:
                timer["status"] = "failed"
                timer["execution_result"] = "failed"
            await self._add_history_record(
                timer_id=timer_id, entity_id=entity_id,
                entity_name=timer.get("entity_name", entity_id),
                task_action=action.get("description", action.get("service", "unknown")),
                before_entity_state=before_entity_state,
                after_entity_state=after_entity_state,
                execution_result="success" if success else "failed",
                start_time=timer.get("start_time", ""), end_time=timer.get("end_time", ""),
                creation_time=timer.get("created_at", ""),
            )
            await self.update_stats()
            if entity_id in self.entity_timers:
                del self.entity_timers[entity_id]
            if timer_id in self.timers:
                del self.timers[timer_id]
            await self.save_tasks()
            await self._update_sensor()
            self._fire_event("timer_completed", {
                "timer_id": timer_id, "entity_id": entity_id,
                "entity_name": timer["entity_name"], "success": success,
                "before_entity_state": before_entity_state,
                "after_entity_state": after_entity_state,
                "message": f"Timer executed for {timer['entity_name']}",
                "time_zone": self.time_zone,
            })
            if success:
                _LOGGER.info(f"[timer_elves] Timer executed: {entity_id}")
            else:
                _LOGGER.error(f"[timer_elves] Timer execution failed: {entity_id}")
        except Exception as e:
            _LOGGER.error(f"[timer_elves] execute_timer failed: {e}")
            timer["status"] = "error"
            timer["error"] = str(e)
            timer["execution_result"] = "failed"
            after_state = self.hass.states.get(entity_id)
            after_entity_state = after_state.state if after_state else "unknown"
            await self._add_history_record(
                timer_id=timer_id, entity_id=entity_id,
                entity_name=timer.get("entity_name", entity_id),
                task_action=action.get("description", "unknown"),
                before_entity_state=before_entity_state,
                after_entity_state=after_entity_state,
                execution_result="failed",
                start_time=timer.get("start_time", ""),
                end_time=timer.get("end_time", ""),
                creation_time=timer.get("created_at", ""),
            )
            await self.update_stats()
            await self.save_tasks()
            await self._update_sensor()

    def execute_climate_timer(self, now, timer_id: str, *args, **kwargs) -> None:
        if timer_id in self.tasks:
            asyncio.run_coroutine_threadsafe(
                self._async_execute_climate_timer(timer_id), self.hass.loop
            )

    async def _async_execute_climate_timer(self, timer_id: str) -> None:
        if timer_id not in self.tasks:
            return
        timer = self.tasks[timer_id]
        entity_id = timer["entity_id"]
        if timer.get("status") == "cancelled":
            return
        before_entity_state = timer.get("before_entity_state", "unknown")
        if before_entity_state == "unknown":
            bs = self.hass.states.get(entity_id)
            before_entity_state = bs.state if bs else "unknown"
        try:
            action = timer["action"]
            success = False
            if action["type"] == "service_call":
                domain, service = action["service"].split(".")
                sd = action.get("data", {}).copy()
                if timer.get("action_type") == "restore_previous" and "restore_data" in action:
                    rd = action["restore_data"]
                    if rd.get("temperature"):
                        await self.hass.services.async_call("climate", "set_temperature", {"entity_id": entity_id, "temperature": rd["temperature"]})
                    if rd.get("fan_mode"):
                        await self.hass.services.async_call("climate", "set_fan_mode", {"entity_id": entity_id, "fan_mode": rd["fan_mode"]})
                    if rd.get("hvac_mode"):
                        await self.hass.services.async_call("climate", "set_hvac_mode", {"entity_id": entity_id, "hvac_mode": rd["hvac_mode"]})
                    success = True
                else:
                    await self.hass.services.async_call(domain, service, sd)
                    success = True
            if success:
                await asyncio.sleep(2)
            after_state = self.hass.states.get(entity_id)
            after_entity_state = after_state.state if after_state else "unknown"
            if success:
                timer["status"] = "completed"
                timer["executed_at"] = self.datetime_to_iso(self.get_local_now())
                timer["execution_result"] = "success"
            else:
                timer["status"] = "failed"
                timer["execution_result"] = "failed"
            await self._add_history_record(
                timer_id=timer_id, entity_id=entity_id,
                entity_name=timer.get("entity_name", entity_id),
                task_action=action.get("description", action.get("service", "unknown")),
                before_entity_state=before_entity_state,
                after_entity_state=after_entity_state,
                execution_result="success" if success else "failed",
                start_time=timer.get("start_time", ""), end_time=timer.get("end_time", ""),
                creation_time=timer.get("created_at", ""),
            )
            await self.update_stats()
            if entity_id in self.entity_timers:
                del self.entity_timers[entity_id]
            if timer_id in self.timers:
                del self.timers[timer_id]
            await self.save_tasks()
            await self._update_sensor()
            self._fire_event("timer_completed", {
                "timer_id": timer_id, "entity_id": entity_id,
                "entity_name": timer["entity_name"], "success": success,
                "action_description": timer["action"].get("description", ""),
                "before_entity_state": before_entity_state,
                "after_entity_state": after_entity_state,
                "message": f"Climate timer executed for {timer['entity_name']}",
                "time_zone": self.time_zone,
            })
            _LOGGER.info(f"[timer_elves] Climate timer executed: {entity_id}")
        except Exception as e:
            _LOGGER.error(f"[timer_elves] execute_climate_timer failed: {e}")
            timer["status"] = "error"
            timer["error"] = str(e)
            timer["execution_result"] = "failed"
            after_state = self.hass.states.get(entity_id)
            after_entity_state = after_state.state if after_state else "unknown"
            await self._add_history_record(
                timer_id=timer_id, entity_id=entity_id,
                entity_name=timer.get("entity_name", entity_id),
                task_action=timer.get("action", {}).get("description", "unknown"),
                before_entity_state=before_entity_state,
                after_entity_state=after_entity_state,
                execution_result="failed",
                start_time=timer.get("start_time", ""), end_time=timer.get("end_time", ""),
                creation_time=timer.get("created_at", ""),
            )
            await self.update_stats()
            await self.save_tasks()
            await self._update_sensor()

    def execute_cover_timer(self, now, timer_id: str, *args, **kwargs) -> None:
        if timer_id in self.tasks:
            asyncio.run_coroutine_threadsafe(
                self._async_execute_cover_timer(timer_id), self.hass.loop
            )

    async def _async_execute_cover_timer(self, timer_id: str) -> None:
        if timer_id not in self.tasks:
            return
        timer = self.tasks[timer_id]
        entity_id = timer["entity_id"]
        if timer.get("status") == "cancelled":
            return
        before_entity_state = timer.get("before_entity_state", "unknown")
        if before_entity_state == "unknown":
            bs = self.hass.states.get(entity_id)
            before_entity_state = bs.state if bs else "unknown"
        try:
            action = timer["action"]
            success = False
            if action["type"] == "service_call":
                if "service" not in action:
                    raise ValueError(f"Action missing 'service': {action}")
                sn = action["service"]
                if "." not in sn:
                    raise ValueError(f"Invalid service: {sn}")
                domain, service = sn.split(".", 1)
                sd = action.get("data", {}).copy()
                if not self.hass.services.has_service(domain, service):
                    raise ValueError(f"Service {domain}.{service} not found")
                await self.hass.services.async_call(domain, service, sd, blocking=True)
                success = True
            if success:
                await asyncio.sleep(2)
            after_state = self.hass.states.get(entity_id)
            after_entity_state = after_state.state if after_state else "unknown"
            if success:
                timer["status"] = "completed"
                timer["executed_at"] = self.datetime_to_iso(self.get_local_now())
                timer["execution_result"] = "success"
            else:
                timer["status"] = "failed"
                timer["execution_result"] = "failed"
            await self._add_history_record(
                timer_id=timer_id, entity_id=entity_id,
                entity_name=timer.get("entity_name", entity_id),
                task_action=action.get("description", action.get("service", "unknown")),
                before_entity_state=before_entity_state,
                after_entity_state=after_entity_state,
                execution_result="success" if success else "failed",
                start_time=timer.get("start_time", ""), end_time=timer.get("end_time", ""),
                creation_time=timer.get("created_at", ""),
            )
            await self.update_stats()
            if entity_id in self.entity_timers:
                del self.entity_timers[entity_id]
            if timer_id in self.timers:
                del self.timers[timer_id]
            await self.save_tasks()
            await self._update_sensor()
            self._fire_event("timer_completed", {
                "timer_id": timer_id, "entity_id": entity_id,
                "entity_name": timer["entity_name"], "success": success,
                "action_description": timer["action"].get("description", ""),
                "before_entity_state": before_entity_state,
                "after_entity_state": after_entity_state,
                "message": f"Cover timer executed for {timer['entity_name']}",
                "time_zone": self.time_zone,
            })
            _LOGGER.info(f"[timer_elves] Cover timer executed: {entity_id}")
        except Exception as e:
            _LOGGER.error(f"[timer_elves] execute_cover_timer failed: {e}")
            timer["status"] = "error"
            timer["error"] = str(e)
            timer["execution_result"] = "failed"
            after_state = self.hass.states.get(entity_id)
            after_entity_state = after_state.state if after_state else "unknown"
            await self._add_history_record(
                timer_id=timer_id, entity_id=entity_id,
                entity_name=timer.get("entity_name", entity_id),
                task_action=timer.get("action", {}).get("description", "unknown"),
                before_entity_state=before_entity_state,
                after_entity_state=after_entity_state,
                execution_result="failed",
                start_time=timer.get("start_time", ""), end_time=timer.get("end_time", ""),
                creation_time=timer.get("created_at", ""),
            )
            await self.update_stats()
            await self.save_tasks()
            await self._update_sensor()

    # ================================================================== #
    #  取消任务                                                             #
    # ================================================================== #

    async def cancel_timer(self, timer_id: str) -> None:
        if timer_id in self.tasks:
            try:
                timer = self.tasks[timer_id]
                entity_id = timer["entity_id"]
                if timer.get("is_recurring"):
                    return await self.cancel_schedule(timer_id)
                if timer_id in self.timers:
                    if self.timers[timer_id]: self.timers[timer_id]()
                    del self.timers[timer_id]
                pre_key = f"{timer_id}_pre"
                if pre_key in self.timers:
                    self.timers[pre_key]()
                    del self.timers[pre_key]
                timer["status"] = "cancelled"
                timer["cancelled_at"] = self.datetime_to_iso(self.get_local_now())
                timer["execution_result"] = "success" if timer.get("executed_at") else "cancelled"
                if entity_id in self.entity_timers and self.entity_timers[entity_id] == timer_id:
                    del self.entity_timers[entity_id]
                await self._cleanup_entity_timers(entity_id, timer_id)
                await self.save_tasks()
                await self._update_sensor()
                self._fire_event("timer_cancelled", {
                    "timer_id": timer_id, "entity_id": entity_id,
                    "entity_name": timer["entity_name"],
                    "message": f"Timer cancelled for {timer['entity_name']}",
                    "time_zone": self.time_zone,
                })
                _LOGGER.info(f"[timer_elves] Timer cancelled: {timer_id}")
            except Exception as e:
                _LOGGER.error(f"[timer_elves] cancel_timer failed: {e}")

    async def cancel_schedule(self, schedule_id: str) -> None:
        if schedule_id in self.tasks:
            try:
                schedule = self.tasks[schedule_id]
                if not schedule.get("is_recurring"):
                    return
                if schedule_id in self.recurring_timers:
                    if self.recurring_timers[schedule_id]: self.recurring_timers[schedule_id]()
                    del self.recurring_timers[schedule_id]
                pre_key = f"{schedule_id}_pre"
                if pre_key in self.recurring_timers:
                    self.recurring_timers[pre_key]()
                    del self.recurring_timers[pre_key]
                schedule["status"] = "cancelled"
                schedule["cancelled_at"] = self.datetime_to_iso(self.get_local_now())
                schedule["execution_result"] = "success" if schedule.get("last_executed") else "cancelled"
                await self.save_tasks()
                await self._update_sensor()
                self._fire_event("schedule_cancelled", {
                    "schedule_id": schedule_id, "entity_id": schedule["entity_id"],
                    "entity_name": schedule["entity_name"],
                    "message": f"Schedule cancelled for {schedule['entity_name']}",
                    "time_zone": self.time_zone,
                })
                _LOGGER.info(f"[timer_elves] Schedule cancelled: {schedule_id}")
            except Exception as e:
                _LOGGER.error(f"[timer_elves] cancel_schedule failed: {e}")

    async def _cleanup_entity_timers(self, entity_id: str, exclude_timer_id: str = None) -> None:
        if entity_id in self.entity_timers:
            ref = self.entity_timers[entity_id]
            if ref != exclude_timer_id and ref in self.tasks:
                t = self.tasks[ref]
                if t.get("status") == "active":
                    t["status"] = "cancelled"
                    t["cancelled_at"] = self.datetime_to_iso(self.get_local_now())
                    t["execution_result"] = "success" if t.get("executed_at") else "cancelled"
                del self.entity_timers[entity_id]
        for tid, td in list(self.tasks.items()):
            if tid != exclude_timer_id and td.get("entity_id") == entity_id and td.get("status") == "active":
                if tid in self.timers:
                    if self.timers[tid]: self.timers[tid]()
                    del self.timers[tid]
                td["status"] = "cancelled"
                td["cancelled_at"] = self.datetime_to_iso(self.get_local_now())

    async def cancel_entity_timer(self, entity_id: str, user_id: str = None) -> None:
        cancelled = 0
        if entity_id in self.entity_timers:
            tid = self.entity_timers[entity_id]
            del self.entity_timers[entity_id]
            if tid in self.tasks:
                await self.cancel_timer(tid)
                cancelled += 1
        for tid, td in list(self.tasks.items()):
            if td.get("entity_id") == entity_id and td.get("status") == "active":
                if tid not in self.timers:
                    td["status"] = "cancelled"
                    td["cancelled_at"] = self.datetime_to_iso(self.get_local_now())
                    td["execution_result"] = "success" if td.get("executed_at") else "cancelled"
                    cancelled += 1
        if cancelled > 0:
            await self.save_tasks()
            await self._update_sensor()
            _LOGGER.info(f"[timer_elves] Cancelled {cancelled} timer(s) for {entity_id}")

    # ================================================================== #
    #  状态查询与推送                                                        #
    # ================================================================== #

    async def send_all_timers(self, user_id: str = None) -> None:
        try:
            active_timers = []
            active_schedules = []
            now = self.get_local_now()
            for tid, timer in self.tasks.items():
                if timer.get("is_recurring"):
                    if timer["status"] == "active":
                        info = {
                            "schedule_id": tid, "entity_id": timer["entity_id"],
                            "entity_name": timer["entity_name"],
                            "repeat_type": timer["repeat_type"],
                            "schedule_time": timer["schedule_time"],
                            "status": timer["status"],
                            "last_executed": timer.get("last_executed"),
                            "next_execution": timer.get("next_execution"),
                            "is_climate": timer.get("is_climate", False),
                            "is_cover": timer.get("is_cover", False),
                            "action_type": timer.get("action_type", "auto"),
                            "time_zone": timer.get("time_zone", self.time_zone),
                        }
                        if timer["repeat_type"] == "weekly":
                            info["weekdays"] = timer.get("weekdays", [])
                        elif timer["repeat_type"] == "monthly":
                            info["month_days"] = timer.get("month_days", [])
                        if user_id and timer.get("created_by") not in [user_id, "api_user", None]:
                            continue
                        active_schedules.append(info)
                elif timer["status"] == "active":
                    end_time = self.iso_to_datetime(timer["end_time"])
                    remaining = max(0, (end_time - now).total_seconds())
                    if remaining <= 0:
                        timer["status"] = "completed"
                        timer["executed_at"] = self.datetime_to_iso(now)
                        if not timer.get("execution_result"):
                            timer["execution_result"] = "unknown"
                        eid = timer["entity_id"]
                        if eid in self.entity_timers: del self.entity_timers[eid]
                        if tid in self.timers: del self.timers[tid]
                        continue
                    info = {
                        "timer_id": tid, "entity_id": timer["entity_id"],
                        "entity_name": timer["entity_name"],
                        "duration": timer["duration"], "end_time": timer["end_time"],
                        "remaining_seconds": remaining,
                        "action": self.get_action_description(timer["action"]),
                        "is_climate": timer.get("is_climate", False),
                        "is_cover": timer.get("is_cover", False),
                        "time_zone": self.time_zone,
                    }
                    if timer.get("is_climate"):
                        info["previous_mode"] = timer.get("previous_state", {}).get("hvac_mode", "Unknown")
                        info["target_action"] = timer.get("action", {}).get("description", "Climate")
                    if timer.get("is_cover"):
                        info["previous_position"] = timer.get("previous_state", {}).get("current_position", 0)
                        info["target_action"] = timer.get("action", {}).get("description", "Cover")
                    if user_id and timer.get("created_by") not in [user_id, "api_user", None]:
                        continue
                    active_timers.append(info)
            self._fire_event("timers_list", {
                "timers": active_timers, "schedules": active_schedules,
                "timer_count": len(active_timers), "schedule_count": len(active_schedules),
                "timestamp": self.datetime_to_iso(now), "time_zone": self.time_zone,
            })
            need_save = any(t.get("status") == "completed" for t in self.tasks.values())
            if need_save:
                ct = self.get_local_now()
                lst = getattr(self, "_last_tasks_save_time", None)
                if lst is None or (ct - lst).total_seconds() > 10:
                    await self.save_tasks()
                    self._last_tasks_save_time = ct
        except Exception as e:
            _LOGGER.error(f"[timer_elves] send_all_timers failed: {e}")

    async def send_all_schedules(self, user_id: str = None) -> None:
        try:
            active = []
            for tid, timer in self.tasks.items():
                if timer.get("is_recurring") and timer["status"] == "active":
                    info = {
                        "schedule_id": tid, "entity_id": timer["entity_id"],
                        "entity_name": timer["entity_name"],
                        "repeat_type": timer["repeat_type"],
                        "schedule_time": timer["schedule_time"],
                        "status": timer["status"],
                        "last_executed": timer.get("last_executed"),
                        "next_execution": timer.get("next_execution"),
                        "is_climate": timer.get("is_climate", False),
                        "is_cover": timer.get("is_cover", False),
                        "action_type": timer.get("action_type", "auto"),
                        "time_zone": timer.get("time_zone", self.time_zone),
                    }
                    if timer["repeat_type"] == "weekly":
                        info["weekdays"] = timer.get("weekdays", [])
                    elif timer["repeat_type"] == "monthly":
                        info["month_days"] = timer.get("month_days", [])
                    if user_id and timer.get("created_by") not in [user_id, "api_user", None]:
                        continue
                    active.append(info)
            self._fire_event("schedules_list", {
                "schedules": active, "count": len(active),
                "timestamp": self.datetime_to_iso(self.get_local_now()),
                "time_zone": self.time_zone,
            })
        except Exception as e:
            _LOGGER.error(f"[timer_elves] send_all_schedules failed: {e}")

    # ================================================================== #
    #  传感器更新                                                           #
    # ================================================================== #

    async def _update_sensor(self) -> None:
        active_timers = sum(1 for t in self.tasks.values() if not t.get("is_recurring") and t.get("status") == "active")
        active_schedules = sum(1 for t in self.tasks.values() if t.get("is_recurring") and t.get("status") == "active")
        all_task_list = self.stats.get("all_task_list", [])
        async_dispatcher_send(self.hass, TIMER_SIGNAL_UPDATE_SENSOR, {
            "active_tasks": active_timers + active_schedules,
            "active_timers": active_timers,
            "active_schedules": active_schedules,
            "total_tasks": len(all_task_list),
            "current_task": active_timers + active_schedules,
            "successful_task": self.stats["successful_task"],
            "failed_task": self.stats["failed_task"],
            "today_task": self.stats["today_task"],
            "all_task_list": all_task_list,
        })

    # ================================================================== #
    #  历史记录                                                             #
    # ================================================================== #

    async def _add_history_record(
        self, timer_id: str, entity_id: str, entity_name: str, task_action: str,
        before_entity_state: str, after_entity_state: str, execution_result: str,
        start_time: str, end_time: str, creation_time: str,
    ) -> None:
        try:
            now = self.get_local_now()
            if timer_id in self.tasks:
                task = self.tasks[timer_id]
                task["day"] = now.strftime("%Y-%m-%d")
                task["before_entity_state"] = before_entity_state
                task["after_entity_state"] = after_entity_state
                task["execution_result"] = execution_result
                task["task_action"] = task_action
                if not task.get("executed_at"):
                    task["executed_at"] = end_time
            else:
                self.tasks[timer_id] = {
                    "id": timer_id, "entity_id": entity_id,
                    "entity_name": entity_name,
                    "status": "completed" if execution_result == "success" else "failed",
                    "task_type": "定时任务", "day": now.strftime("%Y-%m-%d"),
                    "creation_time": creation_time, "start_time": start_time,
                    "end_time": end_time,
                    "before_entity_state": before_entity_state,
                    "after_entity_state": after_entity_state,
                    "execution_result": execution_result,
                    "task_action": task_action,
                    "is_recurring": False, "repeat_type": "none",
                }
            non_active = [(tid, td) for tid, td in self.tasks.items() if td.get("status") != "active"]
            if len(non_active) > MAX_HISTORY_RECORDS:
                non_active.sort(key=lambda x: x[1].get("created_at") or x[1].get("creation_time") or "", reverse=True)
                for tid, _ in non_active[MAX_HISTORY_RECORDS:]:
                    del self.tasks[tid]
            await self.save_tasks()
        except Exception as e:
            _LOGGER.error(f"[timer_elves] add_history_record failed: {e}")

    async def update_stats(self) -> None:
        try:
            now = self.get_local_now()
            today_str = now.strftime("%Y-%m-%d")
            self.stats["total_task"] = len(self.tasks)
            self.stats["successful_task"] = sum(1 for td in self.tasks.values() if td.get("execution_result") == "success")
            self.stats["failed_task"] = sum(1 for td in self.tasks.values() if td.get("execution_result") == "failed")
            self.stats["today_task"] = sum(1 for td in self.tasks.values() if td.get("day") == today_str)
            self.stats["active_timers"] = sum(1 for td in self.tasks.values() if td.get("status") == "active" and not td.get("is_recurring"))
            self.stats["active_schedules"] = sum(1 for td in self.tasks.values() if td.get("status") == "active" and td.get("is_recurring"))

            all_task_list = []
            for task_id, td in self.tasks.items():
                if td.get("is_recurring"):
                    ta = self._get_task_action_description(td)
                else:
                    action = td.get("action", {})
                    ta = action.get("description", action.get("service", td.get("task_action", "未知")))
                ti = dict(td)
                ti["id"] = task_id
                ti["task_type"] = "周期任务" if td.get("is_recurring") else "定时任务"
                ti["task_action"] = ta
                for tk in ("created_at", "executed_at", "cancelled_at", "start_time", "end_time", "next_execution", "last_executed"):
                    if ti.get(tk):
                        ti[tk] = self._convert_to_local_time_str(ti[tk])
                if not td.get("execution_result"):
                    st = td.get("status", "unknown")
                    if st == "completed": ti["execution_result"] = "success"
                    elif st in ("failed", "error"): ti["execution_result"] = "failed"
                    elif st == "cancelled": ti["execution_result"] = "cancelled"
                    elif st == "expired": ti["execution_result"] = "unknown"
                    elif st == "active": ti["execution_result"] = ""
                    else: ti["execution_result"] = "unknown"
                elif td.get("execution_result") in ("", None, "unknown"):
                    st = td.get("status", "unknown")
                    if st == "completed": ti["execution_result"] = "success"
                    elif st in ("failed", "error"): ti["execution_result"] = "failed"
                    elif st == "cancelled": ti["execution_result"] = "cancelled"
                if "after_entity_state" not in ti and td.get("status") in ("completed", "failed", "error"):
                    ti["after_entity_state"] = td.get("after_entity_state", "unknown")
                if not ti.get("day"):
                    created = td.get("created_at") or td.get("creation_time") or ""
                    if created:
                        ti["day"] = created.split("T")[0] if "T" in created else created.split(" ")[0]
                all_task_list.append(ti)
            self.stats["all_task_list"] = all_task_list
        except Exception as e:
            _LOGGER.error(f"[timer_elves] update_stats failed: {e}")

    def _get_task_action_description(self, task_data: dict) -> str:
        try:
            eid = task_data.get("entity_id", "")
            at = task_data.get("action_type", "auto")
            ad = task_data.get("action_data", {})
            if at == "turn_on": return "打开"
            if at == "turn_off": return "关闭"
            if at == "toggle": return "切换"
            if at == "auto":
                es = task_data.get("entity_state", "")
                if es == "on": return "关闭"
                if es == "off": return "打开"
                return "自动操作"
            if at == "set_temperature":
                temp = ad.get("temperature", "")
                return f"设置温度{temp}°C" if temp else "设置温度"
            if at == "set_mode":
                mode = ad.get("mode", "")
                return f"设置模式{mode}" if mode else "设置模式"
            return at
        except Exception:
            return "未知动作"

    # ================================================================== #
    #  工具方法                                                             #
    # ================================================================== #

    def get_friendly_name(self, entity_id: str) -> str:
        st = self.hass.states.get(entity_id)
        if st and st.attributes.get("friendly_name"):
            return st.attributes["friendly_name"]
        return entity_id

    def parse_duration(self, duration_str: str) -> timedelta:
        try:
            if ":" in duration_str:
                parts = duration_str.split(":")
                if len(parts) == 2:
                    h, m = 0, int(parts[0])
                    s = int(parts[1])
                else:
                    h, m, s = map(int, parts)
            else:
                s = int(duration_str)
                h = s // 3600
                m = (s % 3600) // 60
                s = s % 60
            return timedelta(hours=h, minutes=m, seconds=s)
        except Exception:
            raise ValueError("Invalid time format, use HH:MM:SS or seconds")

    def get_action_description(self, action: dict) -> str:
        return action.get("description", action.get("service", "Unknown"))

    def get_climate_action_description(self, action: dict) -> str:
        desc = action.get("description", "")
        if action.get("service") == "climate.set_temperature":
            temp = action.get("data", {}).get("temperature")
            if temp:
                desc = f"Set temperature to {temp}°C"
        return desc

    def get_cover_action_description(self, action: dict) -> str:
        desc = action.get("description", "")
        if action.get("service") == "cover.set_cover_position":
            pos = action.get("data", {}).get("position")
            if pos is not None:
                desc = f"Set cover position to {pos}%"
        return desc

    def _pre_capture_state(self, now, timer_id: str, *args, **kwargs) -> None:
        if timer_id in self.tasks:
            asyncio.run_coroutine_threadsafe(
                self._async_pre_capture_state(timer_id), self.hass.loop
            )

    async def _async_pre_capture_state(self, timer_id: str) -> None:
        if timer_id in self.tasks:
            timer = self.tasks[timer_id]
            eid = timer["entity_id"]
            bs = self.hass.states.get(eid)
            timer["before_entity_state"] = bs.state if bs else "unknown"
            await self.save_tasks()

    def _fire_event(self, action: str, data: dict) -> None:
        """触发定时精灵总线事件。"""
        event_data = {"action": action, "source": "timer_elves", **data}
        self.hass.bus.fire(TIMER_RESPONSE_EVENT, event_data)

    def _convert_to_local_time_str(self, iso_str: str) -> str:
        if not iso_str:
            return ""
        try:
            dt = self.iso_to_datetime(iso_str)
            return self.datetime_to_iso(dt)
        except Exception:
            return iso_str
