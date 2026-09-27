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
from homeassistant.helpers.event import async_track_point_in_time, async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .logger import get_logger
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
    TIMER_HISTORY_RETENTION_DAYS,
    TIMER_BRIEF_LIMIT,
    LATE_EXECUTE_MAX_SECONDS,
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

    # timer_tasks 表的冗余查询列：task_data(JSON) 仍是权威存储，
    # 这些列由 JSON 提取而来，仅供历史查询与 db_viewer 直接查看。
    # 注意：新增/调整顺序时必须同步 _extract_query_columns 的元组顺序。
    _DB_COLUMNS = (
        ("entity_id", "TEXT NOT NULL DEFAULT ''"),
        ("entity_name", "TEXT NOT NULL DEFAULT ''"),
        ("task_type", "TEXT NOT NULL DEFAULT ''"),
        ("status", "TEXT NOT NULL DEFAULT ''"),
        ("action_desc", "TEXT NOT NULL DEFAULT ''"),
        ("duration", "TEXT NOT NULL DEFAULT ''"),
        ("repeat_type", "TEXT NOT NULL DEFAULT ''"),
        ("created_at", "TEXT NOT NULL DEFAULT ''"),
        ("end_time", "TEXT NOT NULL DEFAULT ''"),
        ("next_execution", "TEXT NOT NULL DEFAULT ''"),
        ("executed_at", "TEXT NOT NULL DEFAULT ''"),
        ("execution_result", "TEXT NOT NULL DEFAULT ''"),
    )

    # 动作参数白名单：前端/外部会把参数放在事件顶层（如 climate_mode / temperature），
    # 归一化时统一并入 action_data；命名对齐 push_control.ACTION_CATALOG，便于复用动作目录。
    _ACTION_PARAM_KEYS = (
        "temperature", "target_temp_high", "target_temp_low", "hvac_mode", "mode",
        "fan_mode", "swing_mode", "preset_mode", "position", "tilt_position",
        "brightness", "brightness_pct", "percentage", "color_temp_kelvin",
        "humidity", "operation_mode", "oscillating", "direction",
        "media_content_id", "volume_level", "effect",
    )

    # 空调动作别名 → 内部动作名（对齐 push_control 目录命名，并兼容旧前端字段名）
    _CLIMATE_ACTION_ALIASES = {
        "off": "turn_off",
        "on": "turn_on",
        "set_hvac_mode": "set_mode",
        "hvac_mode": "set_mode",
        "mode": "set_mode",
        "temperature": "set_temperature",
    }

    # HA climate 的模式名：可直接当作 action_type 传入。
    # 注意：这里**不含 "auto"** —— "auto" 是本引擎的语义动作（设备开着就关、关着就尝试还原），
    # 而卡片对空调固定发送 action_type="auto"；若把它当模式名会误切成 HA 的 auto 模式。
    # 需要切到 auto 模式时用 set_mode / set_hvac_mode + hvac_mode=auto。
    _CLIMATE_MODE_NAMES = ("cool", "heat", "dry", "fan_only", "heat_cool")

    # 窗帘动作别名 → 内部动作名（对齐 push_control 目录：open_cover / close_cover /
    # stop_cover / set_cover_position，同时兼容旧前端的 open / close / set_position）
    _COVER_ACTION_ALIASES = {
        "open_cover": "open",
        "close_cover": "close",
        "stop_cover": "stop",
        "set_cover_position": "set_position",
    }

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

        # 卸载时统一回收的句柄：bus 监听 + 兜底调度 tick
        self._unsubs: List[Callable] = []
        self._housekeeping_unsub: Optional[Callable] = None

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
        self._unsubs.append(
            self.hass.bus.async_listen("ha_data_store_timer_event", self._handle_frontend_event)
        )
        # 监听空调/窗帘状态变化（独立监听，不依赖 ha_data_store 的白名单）
        self._unsubs.append(
            self.hass.bus.async_listen("state_changed", self._handle_state_changed)
        )
        # 恢复任务
        await self._init_db_table()
        await self.restore_tasks()
        await self.update_stats()
        await self._update_sensor()
        # 兜底调度：周期任务若因睡死/漏触发没能排上，靠它自愈
        # （原 timer_backend 用 1 天间隔，这里收紧到 1 小时，代价只是一次内存遍历）
        self._housekeeping_unsub = async_track_time_interval(
            self.hass, self._async_housekeeping, timedelta(hours=1)
        )
        _LOGGER.info(f"[timer_elves] Coordinator started (timezone={self.time_zone})")
        # 启动摘要写入本地日志（「日志查看」页可直接确认重启后的任务恢复情况）
        local = get_logger()
        if local is not None:
            active_timers = sum(
                1 for td in self.tasks.values()
                if not td.get("is_recurring") and td.get("status") == "active"
            )
            active_schedules = sum(
                1 for td in self.tasks.values()
                if td.get("is_recurring") and td.get("status") == "active"
            )
            local.info(
                "[定时精灵] 引擎已启动 时区=%s 任务总数=%d 活跃定时器=%d 活跃周期=%d",
                self.time_zone, len(self.tasks), active_timers, active_schedules,
            )

    async def _async_housekeeping(self, _now=None) -> None:
        """每小时兜底：周期任务重排 + 历史清理（内存裁剪与库过期行删除）。"""
        await self.check_recurring_schedules()
        try:
            await self.prune_history()
        except Exception as exc:
            _LOGGER.warning(f"[timer_elves] 历史清理异常: {exc}")

    async def async_unload(self) -> None:
        """卸载协调器。"""
        if self._delayed_update_unsub:
            self._delayed_update_unsub()
            self._delayed_update_unsub = None
        if self._housekeeping_unsub:
            self._housekeeping_unsub()
            self._housekeeping_unsub = None
        for unsub in list(self._unsubs):
            try:
                if unsub:
                    unsub()
            except Exception:
                pass
        self._unsubs.clear()
        for h in list(self.timers.values()):
            if h: h()
        for h in list(self.recurring_timers.values()):
            if h: h()
        self.timers.clear()
        self.recurring_timers.clear()
        # 卸载必须落盘：force 绕过节流，否则最后 5 秒内的变更会丢
        await self.save_tasks(force=True)
        _LOGGER.info("[timer_elves] Coordinator stopped")

    # ================================================================== #
    #  SQLite 持久化                                                       #
    # ================================================================== #

    async def _init_db_table(self) -> None:
        """确保 timer_tasks 表存在（含冗余查询列与索引）。"""
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
                # 兼容早期建的表（只有三列）：按项目惯例用 PRAGMA + ALTER 补列，不重建表
                cols = {row[1] for row in conn.execute(f"PRAGMA table_info({TABLE_TIMER_TASKS})")}
                for name, ddl in self._DB_COLUMNS:
                    if name not in cols:
                        conn.execute(f"ALTER TABLE {TABLE_TIMER_TASKS} ADD COLUMN {name} {ddl}")
                conn.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_{TABLE_TIMER_TASKS}_entity "
                    f"ON {TABLE_TIMER_TASKS} (entity_id)"
                )
                conn.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_{TABLE_TIMER_TASKS}_status "
                    f"ON {TABLE_TIMER_TASKS} (status)"
                )
                conn.commit()
            finally:
                conn.close()
        await self.hass.async_add_executor_job(_init)

    def _extract_query_columns(self, td: dict) -> tuple:
        """从任务数据提取冗余查询列（顺序必须与 _DB_COLUMNS 完全一致）。"""
        if td.get("is_recurring"):
            action_desc = self._get_task_action_description(td)
        else:
            action = td.get("action") or {}
            action_desc = (
                action.get("description") or action.get("service") or td.get("task_action", "")
            )
        return (
            td.get("entity_id", ""),
            td.get("entity_name", ""),
            "周期任务" if td.get("is_recurring") else "定时任务",
            td.get("status", ""),
            action_desc,
            td.get("duration", ""),
            td.get("repeat_type", "none"),
            td.get("created_at") or td.get("creation_time") or "",
            td.get("end_time", ""),
            td.get("next_execution", ""),
            td.get("executed_at", ""),
            td.get("execution_result", ""),
        )

    async def save_tasks(self, force: bool = False) -> None:
        """保存所有任务到 SQLite。

        force=True 绕过节流（卸载/显式写入时必须用），默认 5 秒节流用于高频路径。
        """
        now = self.get_local_now()
        if not force and self._last_save_timestamp is not None:
            diff = (now - self._last_save_timestamp).total_seconds()
            if diff < 5:
                _LOGGER.debug(f"[timer_elves] Save skipped ({diff:.1f}s)")
                return
        try:
            now_str = now.strftime("%Y-%m-%d %H:%M:%S")
            rows = [
                (
                    tid,
                    json.dumps(tdata, default=str, ensure_ascii=False),
                    now_str,
                    *self._extract_query_columns(tdata),
                )
                for tid, tdata in self.tasks.items()
            ]
            col_names = ", ".join(
                ["task_id", "task_data", "updated_at"] + [n for n, _ in self._DB_COLUMNS]
            )
            placeholders = ", ".join(["?"] * (3 + len(self._DB_COLUMNS)))

            def _save():
                conn = sqlite3.connect(self.db_path)
                try:
                    c = conn.cursor()
                    c.executemany(
                        f"INSERT OR REPLACE INTO {TABLE_TIMER_TASKS} ({col_names}) "
                        f"VALUES ({placeholders})",
                        rows,
                    )
                    conn.commit()
                finally:
                    conn.close()
            await self.hass.async_add_executor_job(_save)
            self._last_save_timestamp = now
            _LOGGER.debug(f"[timer_elves] Saved {len(rows)} tasks")
        except Exception as e:
            _LOGGER.error(f"[timer_elves] save_tasks failed: {e}")

    def _prune_memory(self, keep: int = MAX_HISTORY_RECORDS) -> list:
        """按上限裁剪内存中的非活跃任务，返回被移除的 task_id 列表。"""
        non_active = [
            (tid, td) for tid, td in self.tasks.items() if td.get("status") != "active"
        ]
        if len(non_active) <= keep:
            return []
        non_active.sort(
            key=lambda x: x[1].get("created_at") or x[1].get("creation_time") or "",
            reverse=True,
        )
        removed = []
        for tid, _ in non_active[keep:]:
            del self.tasks[tid]
            removed.append(tid)
        return removed

    async def _delete_tasks_from_db(self, task_ids: list) -> int:
        """从 timer_tasks 表删除指定任务行（供历史裁剪使用）。"""
        ids = [tid for tid in (task_ids or []) if tid]
        if not ids:
            return 0

        def _del():
            conn = sqlite3.connect(self.db_path)
            try:
                conn.executemany(
                    f"DELETE FROM {TABLE_TIMER_TASKS} WHERE task_id = ?",
                    [(tid,) for tid in ids],
                )
                conn.commit()
            finally:
                conn.close()
            return len(ids)

        try:
            return await self.hass.async_add_executor_job(_del)
        except Exception as exc:
            _LOGGER.warning(f"[timer_elves] 删除历史任务失败: {exc}")
            return 0

    async def _delete_all_history_in_db(self) -> int:
        """删除库中全部非活跃任务行（清空历史用）。"""
        def _del():
            conn = sqlite3.connect(self.db_path)
            try:
                cursor = conn.execute(
                    f"DELETE FROM {TABLE_TIMER_TASKS} WHERE status != 'active'"
                )
                conn.commit()
                return cursor.rowcount or 0
            finally:
                conn.close()

        try:
            return await self.hass.async_add_executor_job(_del)
        except Exception as exc:
            _LOGGER.warning(f"[timer_elves] 清空历史失败: {exc}")
            return 0

    async def prune_history(
        self, retention_days: int = TIMER_HISTORY_RETENTION_DAYS
    ) -> dict:
        """定期清理历史，避免库与内存无限增长。

        ① 内存中非活跃任务超过 MAX_HISTORY_RECORDS 的部分 → 从内存与库中同时删除
        ② 库中非活跃且创建时间早于「保留天数」的行 → 删除（retention_days<=0 表示不按天数清理）
        """
        trimmed = self._prune_memory()
        db_deleted = await self._delete_tasks_from_db(trimmed)
        db_purged = 0
        if retention_days and retention_days > 0:
            cutoff = self.datetime_to_iso(
                self.get_local_now() - timedelta(days=retention_days)
            )

            def _purge():
                conn = sqlite3.connect(self.db_path)
                try:
                    cursor = conn.execute(
                        f"DELETE FROM {TABLE_TIMER_TASKS} "
                        f"WHERE status != 'active' AND created_at != '' AND created_at < ?",
                        (cutoff,),
                    )
                    conn.commit()
                    return cursor.rowcount or 0
                finally:
                    conn.close()

            try:
                db_purged = await self.hass.async_add_executor_job(_purge)
            except Exception as exc:
                _LOGGER.warning(f"[timer_elves] 清理过期历史失败: {exc}")
        if trimmed or db_deleted or db_purged:
            await self.save_tasks(force=True)
            local = get_logger()
            if local is not None:
                local.info(
                    "[定时精灵] 历史清理：内存裁剪 %d 条、库删除 %d 条、过期清理 %d 条（保留 %d 天）",
                    len(trimmed), db_deleted, db_purged, retention_days,
                )
        return {
            "memory_trimmed": len(trimmed),
            "db_deleted": db_deleted,
            "db_purged": db_purged,
            "retention_days": retention_days,
        }

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
        expired_skipped = 0
        bad_rows = 0
        for timer_id, timer_data in data.items():
            # 逐条容错：单条坏数据不应导致整个定时精灵模块启动失败
            try:
                repeat_type = timer_data.get("repeat_type", "none")
                schedule_time = timer_data.get("schedule_time")
                if repeat_type != "none" and schedule_time:
                    await self._restore_recurring_timer(timer_id, timer_data)
                    recurring_restored += 1
                elif timer_data.get("status") == "active":
                    entity_id = timer_data.get("entity_id")
                    if not entity_id or not timer_data.get("end_time"):
                        raise ValueError("缺少 entity_id / end_time")
                    end_time = self.iso_to_datetime(timer_data["end_time"])
                    now = self.get_local_now()
                    if end_time > now:
                        # 空调/窗帘必须走各自的执行器，否则 restore_previous 会丢
                        # （通用 execute_timer 不处理 is_climate/is_cover 的恢复逻辑）
                        if timer_data.get("is_climate"):
                            callback = lambda n, tid=timer_id: self.execute_climate_timer(n, tid)
                        elif timer_data.get("is_cover"):
                            callback = lambda n, tid=timer_id: self.execute_cover_timer(n, tid)
                        else:
                            callback = lambda n, tid=timer_id: self.execute_timer(n, tid)
                        timer_handle = async_track_point_in_time(
                            self.hass, callback, end_time
                        )
                        self.timers[timer_id] = timer_handle
                        start_time = self.iso_to_datetime(
                            timer_data.get("start_time") or timer_data.get("created_at") or ""
                        )
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
                        late_seconds = int((now - end_time).total_seconds())
                        if late_seconds > LATE_EXECUTE_MAX_SECONDS:
                            # 停机期间错过的任务：超过补执行时限则标记 expired，不再补执行
                            # （避免重启瞬间集中操作设备，尤其是空调/窗帘这类会改状态的）
                            timer_data["status"] = "expired"
                            timer_data["execution_result"] = "expired"
                            timer_data["late_seconds"] = late_seconds
                            self.tasks[timer_id] = timer_data
                            expired_skipped += 1
                            _LOGGER.info(
                                "[timer_elves] 跳过迟到任务（迟到 %ds > %ds）: %s",
                                late_seconds, LATE_EXECUTE_MAX_SECONDS, timer_id,
                            )
                        else:
                            timer_data["late_seconds"] = late_seconds
                            self.tasks[timer_id] = timer_data
                            if timer_data.get("is_climate"):
                                self.hass.add_job(self._async_execute_climate_timer, timer_id)
                            elif timer_data.get("is_cover"):
                                self.hass.add_job(self._async_execute_cover_timer, timer_id)
                            else:
                                self.hass.add_job(self._async_execute_timer, timer_id)
                else:
                    self.tasks[timer_id] = timer_data
            except Exception as exc:
                bad_rows += 1
                _LOGGER.warning(f"[timer_elves] 恢复任务失败（已跳过）{timer_id}: {exc}")

        # 恢复后立即按上限裁剪：否则被裁剪过的历史会随重启"复活"，
        # 让内存与传感器属性体积反弹（库中对应行一并删除）
        removed = self._prune_memory()
        if removed:
            await self._delete_tasks_from_db(removed)

        _LOGGER.info(
            f"[timer_elves] Restored {restored} timers + {recurring_restored} schedules "
            f"(total {len(data)}, 跳过迟到 {expired_skipped}, 坏数据 {bad_rows})"
        )

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
        elif action == "update_task":
            try:
                patch = data.get("patch") or {
                    k: data[k] for k in
                    ("duration", "action_type", "action_data", "repeat_type",
                     "schedule_time", "weekdays", "month_days")
                    if k in data
                }
                await self.update_task(data.get("task_id"), patch)
            except Exception as e:
                _LOGGER.error(f"[timer_elves] update_task event failed: {e}")
                self._fire_event("error", {"error": str(e), "success": False, "action": "update_task"})
        elif action == "get_history":
            try:
                result = await self.get_history(
                    limit=data.get("limit", 50),
                    offset=data.get("offset", 0),
                    entity_id=data.get("entity_id"),
                    status=data.get("status"),
                    start=data.get("start"),
                    end=data.get("end"),
                    with_data=bool(data.get("with_data")),
                )
                self._fire_event("history_result", result)
            except Exception as e:
                _LOGGER.error(f"[timer_elves] get_history event failed: {e}")
                self._fire_event("error", {"error": str(e), "success": False, "action": "get_history"})
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

            action_data = self._normalize_action_data(data)
            action_type = self._resolve_action_type(data, action_data, default="auto")
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
                "action": self.generate_action(entity_id, action_type, None, action_data),
                "action_type": action_type,
                "action_data": action_data,
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
            # 参数归一化：顶层 climate_mode / temperature / fan_mode 等并入 action_data
            # 动作名解析：卡片固定发 action_type='auto'，真实动作在 action_config 里
            action_data = self._normalize_action_data(data)
            action_type = self._resolve_action_type(data, action_data, default="turn_off")
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
            action = self.generate_climate_action(
                entity_id, action_type, action_data,
                previous_state=self.climate_previous_states.get(entity_id))
            timer_data = {
                "timer_id": timer_id, "entity_id": entity_id,
                "duration": duration_str, "start_time": self.datetime_to_iso(start_time),
                "end_time": self.datetime_to_iso(end_time), "status": "active",
                "entity_name": self.get_friendly_name(entity_id),
                "entity_state": current_state, "domain": "climate",
                "created_by": data.get("user_id", "unknown"),
                "created_at": self.datetime_to_iso(self.get_local_now()),
                "action": action,
                "action_type": action_type, "action_data": action_data,
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
            action_data = self._normalize_action_data(data)
            action_type = self._resolve_action_type(data, action_data, default="close")
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
            action = self.generate_cover_action(
                entity_id, action_type, action_data,
                previous_state=self.cover_previous_states.get(entity_id))
            timer_data = {
                "timer_id": timer_id, "entity_id": entity_id,
                "duration": duration_str, "start_time": self.datetime_to_iso(start_time),
                "end_time": self.datetime_to_iso(end_time), "status": "active",
                "entity_name": self.get_friendly_name(entity_id),
                "entity_state": current_state, "domain": "cover",
                "created_by": data.get("user_id", "unknown"),
                "created_at": self.datetime_to_iso(self.get_local_now()),
                "action": action,
                # 与 create_climate_timer 保持一致：动作名与动作参数必须存进任务，
                # 否则 send_all_timers 只能推一句英文描述（Set position to 70%），
                # 卡片渲染不出【开·70%】这种「方向·位置」格式。
                "action_type": action_type, "action_data": action_data,
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
            action_data = self._normalize_action_data(data)
            action_type = self._resolve_action_type(data, action_data, default="auto")
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
                "action_type": action_type, "action_data": action_data,
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

    async def create_task(self, data: dict) -> dict:
        """API/自动化入口：按 kind 创建任务并返回新任务数据。

        内部 create_* 为了兼容前端事件会吞掉异常，这里用「调用前后任务集合差异」
        判定成败——失败时抛 ValueError，便于 HTTP 层返回准确状态码。
        """
        kind = (data.get("kind") or "timer").strip()
        repeat_type = (data.get("repeat_type") or "none")
        before = set(self.tasks.keys())
        if kind in ("schedule", "create_schedule") or (
            repeat_type != "none" and data.get("schedule_time")
        ):
            await self.create_schedule(data)
        elif kind in ("climate", "climate_timer", "create_climate_timer"):
            await self.create_climate_timer(data)
        elif kind in ("cover", "cover_timer", "create_cover_timer"):
            await self.create_cover_timer(data)
        else:
            await self.create_timer(data)
        new_ids = [tid for tid in self.tasks.keys() if tid not in before]
        if not new_ids:
            raise ValueError("创建失败：实体不存在、参数非法或该实体已有定时任务被拒绝")
        return self.tasks[new_ids[-1]]

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
        # hass.add_job 为线程安全调度入口（在 loop 线程直接调度，其他线程自动切回）
        self.hass.add_job(self._async_execute_recurring_schedule, schedule_id)

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
                action_data = self._normalize_action_data(sd)
                action = self.generate_climate_action(
                    entity_id, action_type, action_data,
                    previous_state=sd.get("previous_state"))
                if action["type"] == "service_call":
                    domain, service = action["service"].split(".")
                    sd2 = action.get("data", {}).copy()
                    if action_type == "restore_previous" and "restore_data" in action:
                        rd = action["restore_data"]
                        # 先切模式（顺带开机）再设温度/风速
                        if rd.get("hvac_mode"):
                            await self.hass.services.async_call(
                                "climate", "set_hvac_mode",
                                {"entity_id": entity_id, "hvac_mode": rd["hvac_mode"]}, blocking=True)
                        if rd.get("temperature"):
                            await self.hass.services.async_call(
                                "climate", "set_temperature",
                                {"entity_id": entity_id, "temperature": rd["temperature"]}, blocking=True)
                        if rd.get("fan_mode"):
                            await self.hass.services.async_call(
                                "climate", "set_fan_mode",
                                {"entity_id": entity_id, "fan_mode": rd["fan_mode"]}, blocking=True)
                        success = True
                    else:
                        await self._run_extra_calls(action, "pre_calls")
                        await self.hass.services.async_call(domain, service, sd2, blocking=True)
                        await self._run_extra_calls(action)
                        success = True
            elif sd.get("is_cover"):
                action_data = self._normalize_action_data(sd)
                action = self.generate_cover_action(
                    entity_id, action_type, action_data,
                    previous_state=sd.get("previous_state"))
                if action["type"] == "service_call":
                    domain, service = action["service"].split(".")
                    sd2 = action.get("data", {}).copy()
                    if action_type == "restore_previous" and "restore_data" in action:
                        pos = action["restore_data"].get("current_position")
                        if pos is not None:
                            await self.hass.services.async_call(
                                "cover", "set_cover_position",
                                {"entity_id": entity_id, "position": pos}, blocking=True)
                            success = True
                    else:
                        await self.hass.services.async_call(domain, service, sd2, blocking=True)
                        success = True
            else:
                action = self.generate_action(
                    entity_id, action_type, None, self._normalize_action_data(sd))
                if action["type"] == "service_call":
                    domain, service = action["service"].split(".")
                    await self._run_extra_calls(action, "pre_calls")
                    await self.hass.services.async_call(
                        domain, service, action.get("data", {}), blocking=True)
                    await self._run_extra_calls(action)
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
            deleted = await self._delete_all_history_in_db()
            await self._update_sensor()
            _LOGGER.info("[timer_elves] All history cleared (db deleted=%d)", deleted)
            local = get_logger()
            if local is not None:
                local.info(
                    "[定时精灵] 已清空历史记录（保留活跃任务 %d 条，库删除 %d 行）",
                    len(active_tasks), deleted,
                )
        except Exception as e:
            _LOGGER.error(f"[timer_elves] clear_all_history failed: {e}")

    # ================================================================== #
    #  动作生成                                                             #
    # ================================================================== #

    def _parse_action_config(self, data: dict) -> tuple:
        """解析前端卡片的 action_config，返回 (动作名提示, 参数字典)。

        action_config 是「动作名 → {service, data}」结构（与卡片及旧 timer_backend 的
        apps.yaml 一致），例如空调：
          {"set_mode": {"service": "climate.set_hvac_mode", "data": {"hvac_mode": "cool"}},
           "set_temperature": {"service": "climate.set_temperature", "data": {"temperature": 26}}}
        或关机：{"turn_off": {"service": "climate.turn_off", "data": {...}}}

        卡片对空调固定发送 action_type='auto'，真实意图只在这个结构里，
        因此必须解析它——否则用户选的「制冷 26℃」会被丢弃，退化成关机/静默无动作。
        """
        cfg = data.get("action_config")
        if not isinstance(cfg, dict) or not cfg:
            return None, {}
        params: dict = {}
        for spec in cfg.values():
            spec_data = spec.get("data") if isinstance(spec, dict) else None
            if isinstance(spec_data, dict):
                for key in self._ACTION_PARAM_KEYS:
                    if spec_data.get(key) is not None:
                        params.setdefault(key, spec_data[key])
        # 动作名提示优先级：开关机 > 设温（同时带模式）> 设模式 > 窗帘位置/开合
        for name, hint in (
            ("turn_off", "turn_off"), ("turn_on", "turn_on"),
            ("set_temperature", "set_temperature"), ("set_mode", "set_mode"),
            ("set_position", "set_position"), ("open", "open"), ("close", "close"),
        ):
            if name in cfg:
                return hint, params
        return None, params

    def _resolve_action_type(self, data: dict, action_data: dict, default: str = "auto") -> str:
        """确定本次任务真正要执行的动作名。

        优先级：显式且非 auto 的 action_type > action_config 推导 > 模式名 > default。
        （卡片对空调固定发 action_type='auto'，真实动作在 action_config 里。）
        """
        explicit = (data.get("action_type") or "").strip()
        if explicit and explicit != "auto":
            return explicit
        hint, _ = self._parse_action_config(data)
        if hint:
            return hint
        mode = action_data.get("hvac_mode") or action_data.get("mode")
        if mode:
            return mode
        return explicit or default

    def _normalize_action_data(self, data: dict) -> dict:
        """把事件顶层 / action_config 里的动作参数并入 action_data。

        - 顶层参数不覆盖 action_data 里已显式给出的同名键
        - 解析前端卡片的 action_config（见 _parse_action_config）
        - 兼容前端历史字段：climate_mode → hvac_mode / mode
        - hvac_mode 与 mode 互为别名；temperature 统一为 float
        """
        ad = dict(data.get("action_data") or {})
        for key in self._ACTION_PARAM_KEYS:
            if key in data and data[key] is not None and key not in ad:
                ad[key] = data[key]
        _, cfg_params = self._parse_action_config(data)
        for key, value in cfg_params.items():
            ad.setdefault(key, value)
        climate_mode = data.get("climate_mode")
        if climate_mode not in (None, ""):
            ad.setdefault("hvac_mode", climate_mode)
            ad.setdefault("mode", climate_mode)
        if ad.get("hvac_mode") and not ad.get("mode"):
            ad["mode"] = ad["hvac_mode"]
        elif ad.get("mode") and not ad.get("hvac_mode"):
            ad["hvac_mode"] = ad["mode"]
        if ad.get("temperature") is not None:
            try:
                ad["temperature"] = float(ad["temperature"])
            except (TypeError, ValueError):
                pass
        return ad

    def _generate_action_from_catalog(
        self, entity_id: str, action_type: str, action_data: dict = None
    ) -> dict | None:
        """按 push_control 的动作目录（ACTION_CATALOG）生成服务调用。

        复用「域 → 动作 → 服务 + 参数」这一份定义，使定时任务的 action_type
        与参数命名跟「实体→网络」控制目录保持一致；未命中返回 None。
        """
        if not action_type or "." not in entity_id:
            return None
        try:
            from .push_control import ACTION_CATALOG
        except Exception:
            return None
        domain, _, _ = entity_id.partition(".")
        spec = ((ACTION_CATALOG.get(domain) or {}).get("actions") or {}).get(action_type)
        if not spec:
            return None
        service = str(spec.get("service") or action_type)
        if "." not in service:
            service = f"{domain}.{service}"
        payload = {k: v for k, v in (action_data or {}).items() if v is not None}
        payload["entity_id"] = entity_id
        return {
            "type": "service_call",
            "service": service,
            "data": payload,
            "description": spec.get("label") or action_type,
        }

    def generate_action(
        self, entity_id: str, action_type: str = "auto",
        current_state: str = None, action_data: dict = None,
    ) -> dict:
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

        # 兜底：按 push_control 的动作目录生成（动作名/参数名与目录一致）
        catalog_action = self._generate_action_from_catalog(entity_id, action_type, action_data)
        if catalog_action:
            return catalog_action

        return {"type": "service_call", "service": f"{domain}.turn_off",
                "data": {"entity_id": entity_id}, "description": "Turn off"}

    def generate_climate_action(
        self, entity_id: str, action_type: str = "turn_off",
        action_data: dict = None, previous_state: dict = None,
    ) -> dict:
        """生成空调动作。

        action_type 支持三类写法：
          1) 本引擎语义动作：turn_on / turn_off / set_temperature / set_mode /
             set_fan_mode / set_swing_mode / set_preset_mode / restore_previous / auto
          2) push_control 目录命名：set_hvac_mode（= set_mode）
          3) 直接给 HA 模式名：cool / heat / dry / fan_only / auto / heat_cool
             带了 temperature 就用 set_temperature 一次设完，否则用 set_hvac_mode
        参数从 action_data 读取（temperature / hvac_mode / fan_mode / swing_mode /
        preset_mode）；风速/摆风/预设在设温度场景下作为附加调用（extra_calls）下发。
        """
        action_data = action_data or {}
        ca = self.default_actions.get("climate", {})
        at = self._CLIMATE_ACTION_ALIASES.get(action_type, action_type)

        if at in self._CLIMATE_MODE_NAMES:
            temp = action_data.get("temperature")
            if temp is None:
                return self._climate_set_mode(entity_id, at, ca)
            return self._climate_set_temperature(entity_id, temp, at, action_data, ca)

        if at == "turn_on":
            if "turn_on" in ca:
                return {"type": "service_call", "service": ca["turn_on"]["service"],
                        "data": {**ca["turn_on"].get("data", {}), "entity_id": entity_id},
                        "description": ca["turn_on"].get("description", "Turn on AC")}
            return {"type": "service_call", "service": "climate.turn_on",
                    "data": {"entity_id": entity_id}, "description": "Turn on AC"}

        if at == "turn_off":
            if "turn_off" in ca:
                return {"type": "service_call", "service": ca["turn_off"]["service"],
                        "data": {**ca["turn_off"].get("data", {}), "entity_id": entity_id},
                        "description": ca["turn_off"].get("description", "Turn off AC")}
            return {"type": "service_call", "service": "climate.turn_off",
                    "data": {"entity_id": entity_id}, "description": "Turn off AC"}

        if at == "set_temperature":
            temp = action_data.get("temperature", self.climate_config["default_temperature"])
            mode = (action_data.get("hvac_mode") or action_data.get("mode")
                    or self.climate_config["default_mode"])
            return self._climate_set_temperature(entity_id, temp, mode, action_data, ca)

        if at == "set_mode":
            mode = action_data.get("mode") or action_data.get("hvac_mode") or "cool"
            if mode == "off":
                return self.generate_climate_action(entity_id, "turn_off")
            return self._climate_set_mode(entity_id, mode, ca)

        if at in ("set_fan_mode", "set_swing_mode", "set_preset_mode"):
            key = {"set_fan_mode": "fan_mode",
                   "set_swing_mode": "swing_mode",
                   "set_preset_mode": "preset_mode"}[at]
            value = action_data.get(key)
            if not value:
                _LOGGER.warning(f"[timer_elves] {at} 缺少参数 {key}，退化为关机")
                return self.generate_climate_action(entity_id, "turn_off")
            return {"type": "service_call", "service": f"climate.{at}",
                    "data": {"entity_id": entity_id, key: value},
                    "description": f"{at} → {value}"}

        if at == "restore_previous":
            # 优先用任务内快照（重启后依然有效），其次退回内存字典
            ps = previous_state or self.climate_previous_states.get(entity_id) or {}
            if not ps:
                # 快照缺失时显式失败，避免旧行为（一个服务都不发却记 success）
                return {"type": "noop",
                        "description": "无可还原的空调状态（缺少快照）"}
            mode = ps.get("hvac_mode", "cool")
            return {"type": "service_call", "service": "climate.set_hvac_mode",
                    "data": {"entity_id": entity_id, "hvac_mode": mode},
                    "restore_data": ps,
                    "description": f"Restore previous ({mode})"}

        if at == "auto":
            st = self.hass.states.get(entity_id)
            cs = st.state if st else "off"
            if cs == "off":
                return self.generate_climate_action(entity_id, "restore_previous")
            return self.generate_climate_action(entity_id, "turn_off")

        # 目录兜底（push_control 自定义/新增动作）
        catalog_action = self._generate_action_from_catalog(entity_id, action_type, action_data)
        if catalog_action:
            return catalog_action

        _LOGGER.warning(f"[timer_elves] 未知空调动作 {action_type}，退化为关机")
        return self.generate_climate_action(entity_id, "turn_off")

    def _climate_set_mode(self, entity_id: str, mode: str, ca: dict) -> dict:
        """设为指定运行模式（参数名对齐目录：hvac_mode）。"""
        if "set_mode" in ca:
            return {"type": "service_call", "service": ca["set_mode"]["service"],
                    "data": {**ca["set_mode"].get("data", {}),
                             "entity_id": entity_id, "hvac_mode": mode},
                    "description": ca["set_mode"].get("description", f"Set mode to {mode}")}
        return {"type": "service_call", "service": "climate.set_hvac_mode",
                "data": {"entity_id": entity_id, "hvac_mode": mode},
                "description": f"Set mode to {mode}"}

    def _climate_set_temperature(self, entity_id: str, temp, mode: str, action_data: dict, ca: dict) -> dict:
        """设置温度，可同时设模式；风速/摆风/预设走 extra_calls。

        HA 的 climate.set_temperature 支持 hvac_mode，但不接受 fan_mode / swing_mode /
        preset_mode，因此这些参数作为附加调用在执行时依次下发。
        """
        payload = {"entity_id": entity_id, "temperature": temp}
        if mode:
            payload["hvac_mode"] = mode
        if "set_temperature" in ca:
            action = {"type": "service_call", "service": ca["set_temperature"]["service"],
                      "data": {**ca["set_temperature"].get("data", {}), **payload},
                      "description": ca["set_temperature"].get("description", f"Set temp to {temp}°C")}
        else:
            action = {"type": "service_call", "service": "climate.set_temperature",
                      "data": payload, "description": f"Set temp to {temp}°C"}
        if mode and mode != "off":
            # 部分空调集成（含本集成自带的虚拟空调）的 async_set_temperature 会忽略
            # hvac_mode 参数 → 只改目标温度、不切模式，看起来像"执行没生效"。
            # 故把「切模式（顺带开机）」作为前置调用，保证模式真正落地。
            action["pre_calls"] = [{
                "service": "climate.set_hvac_mode",
                "data": {"entity_id": entity_id, "hvac_mode": mode},
            }]
        extra = []
        for key, service in (("fan_mode", "climate.set_fan_mode"),
                             ("swing_mode", "climate.set_swing_mode"),
                             ("preset_mode", "climate.set_preset_mode")):
            if action_data.get(key):
                extra.append({"service": service,
                              "data": {"entity_id": entity_id, key: action_data[key]}})
        if extra:
            action["extra_calls"] = extra
        return action

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

    def generate_cover_action(
        self, entity_id: str, action_type: str = "close",
        action_data: dict = None, previous_state: dict = None,
    ) -> dict:
        action_data = action_data or {}
        ca = self.default_actions.get("cover", {})
        at = self._COVER_ACTION_ALIASES.get(action_type, action_type)

        if at == "close":
            if "close" in ca:
                s = self.get_available_cover_service(ca["close"]["service"], "cover.close_cover")
                return {"type": "service_call", "service": s,
                        "data": {**ca["close"].get("data", {}), "entity_id": entity_id},
                        "description": ca["close"].get("description", "Close cover")}
            s = self.get_available_cover_service("cover.close_cover", "cover.close")
            return {"type": "service_call", "service": s, "data": {"entity_id": entity_id},
                    "description": "Close cover"}
        if at == "open":
            if "open" in ca:
                s = self.get_available_cover_service(ca["open"]["service"], "cover.open_cover")
                return {"type": "service_call", "service": s,
                        "data": {**ca["open"].get("data", {}), "entity_id": entity_id},
                        "description": ca["open"].get("description", "Open cover")}
            s = self.get_available_cover_service("cover.open_cover", "cover.open")
            return {"type": "service_call", "service": s, "data": {"entity_id": entity_id},
                    "description": "Open cover"}
        if at == "set_position":
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
        if at == "stop":
            s = self.get_available_cover_service("cover.stop_cover", "cover.stop")
            return {"type": "service_call", "service": s,
                    "data": {"entity_id": entity_id}, "description": "Stop cover"}

        if at == "restore_previous":
            # 优先用任务内快照（重启后依然有效），其次退回内存字典
            ps = previous_state or self.cover_previous_states.get(entity_id) or {}
            pos = ps.get("current_position")
            if pos is None:
                # 位置 0 是合法值，这里用 None 判断（旧逻辑把 0 当成"没记录"）
                st = self.hass.states.get(entity_id)
                pos = st.attributes.get("current_position", 0) if st else 0
            s = self.get_available_cover_service("cover.set_cover_position", "cover.set_position")
            return {"type": "service_call", "service": s,
                    "data": {"entity_id": entity_id, "position": pos},
                    "restore_data": ps,
                    "description": f"Restore position ({pos}%)"}

        if at == "auto":
            st = self.hass.states.get(entity_id)
            cs = st.state if st else "closed"
            if cs == "closed":
                return self.generate_cover_action(entity_id, "restore_previous")
            return self.generate_cover_action(entity_id, "close")

        # 兜底：按 push_control 的动作目录生成（覆盖 stop_cover / set_cover_tilt_position 等）
        catalog_action = self._generate_action_from_catalog(entity_id, action_type, action_data)
        if catalog_action:
            return catalog_action

        _LOGGER.warning(f"[timer_elves] 未知窗帘动作 {action_type}，退化为关闭")
        return self.generate_cover_action(entity_id, "close")

    # ================================================================== #
    #  执行定时器（一次性）                                                   #
    # ================================================================== #

    def execute_timer(self, now, timer_id: str, *args, **kwargs) -> None:
        if timer_id in self.tasks:
            self.hass.add_job(self._async_execute_timer, timer_id)

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
                await self._run_extra_calls(action, "pre_calls")
                # blocking=True：让服务调用的错误（参数非法 / 服务不存在）能被捕获，
                # 否则任务会被误判为 success，而设备实际没动。
                await self.hass.services.async_call(
                    domain, service, action.get("data", {}), blocking=True)
                await self._run_extra_calls(action)
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
            # 完成后主动推一次列表：卡片的任务条靠这份推送增删任务，
            # 不推的话已完成的任务会一直挂在列表里显示 00:00:00。
            await self.send_all_timers()
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
            self.hass.add_job(self._async_execute_climate_timer, timer_id)

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
                    # 先切模式（顺带开机）再设温度，避免个别集成在 off 状态拒绝设温
                    if rd.get("hvac_mode"):
                        await self.hass.services.async_call(
                            "climate", "set_hvac_mode",
                            {"entity_id": entity_id, "hvac_mode": rd["hvac_mode"]}, blocking=True)
                    if rd.get("temperature"):
                        await self.hass.services.async_call(
                            "climate", "set_temperature",
                            {"entity_id": entity_id, "temperature": rd["temperature"]}, blocking=True)
                    if rd.get("fan_mode"):
                        await self.hass.services.async_call(
                            "climate", "set_fan_mode",
                            {"entity_id": entity_id, "fan_mode": rd["fan_mode"]}, blocking=True)
                    success = True
                else:
                    await self._run_extra_calls(action, "pre_calls")
                    await self.hass.services.async_call(domain, service, sd, blocking=True)
                    await self._run_extra_calls(action)
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
            # 完成后主动推一次列表（原因同 execute_timer：不推则任务条不会清空）
            await self.send_all_timers()
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
            self.hass.add_job(self._async_execute_cover_timer, timer_id)

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
                await self._run_extra_calls(action, "pre_calls")
                await self.hass.services.async_call(domain, service, sd, blocking=True)
                await self._run_extra_calls(action)
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
            # 完成后主动推一次列表（原因同 execute_timer：不推则任务条不会清空）
            await self.send_all_timers()
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
        """取消同实体的其它「一次性定时器」（内部辅助）。

        周期任务**不在此处理**——它们必须走 cancel_schedule（注销周期句柄 + 发事件），
        否则会被静默标记为 cancelled 而定时器句柄残留、并让用户以为周期任务还在。
        """
        if self.entity_timers.get(entity_id) not in (None, exclude_timer_id):
            ref = self.entity_timers[entity_id]
            if ref in self.tasks:
                target = self.tasks[ref]
                if target.get("status") == "active" and not target.get("is_recurring"):
                    target["status"] = "cancelled"
                    target["cancelled_at"] = self.datetime_to_iso(self.get_local_now())
                    target["execution_result"] = "success" if target.get("executed_at") else "cancelled"
            del self.entity_timers[entity_id]
        for tid, td in list(self.tasks.items()):
            if tid == exclude_timer_id or td.get("entity_id") != entity_id:
                continue
            if td.get("status") != "active" or td.get("is_recurring"):
                continue
            for key in (tid, f"{tid}_pre"):
                handle = self.timers.pop(key, None)
                if handle:
                    try:
                        handle()
                    except Exception:
                        pass
            td["status"] = "cancelled"
            td["cancelled_at"] = self.datetime_to_iso(self.get_local_now())
            td["execution_result"] = "success" if td.get("executed_at") else "cancelled"

    async def cancel_entity_timer(self, entity_id: str, user_id: str = None) -> None:
        """取消该实体的全部任务。

        按类型正确分派：一次性走 cancel_timer、周期任务走 cancel_schedule
        （两者都会注销各自的定时器句柄并发出取消事件），避免句柄残留。
        """
        self.entity_timers.pop(entity_id, None)
        target_ids = [
            tid for tid, td in self.tasks.items()
            if td.get("entity_id") == entity_id and td.get("status") == "active"
        ]
        cancelled = 0
        for tid in target_ids:
            task = self.tasks.get(tid)
            if not task or task.get("status") != "active":
                continue  # 已被上一步的清理逻辑连带取消
            if task.get("is_recurring"):
                await self.cancel_schedule(tid)
            else:
                await self.cancel_timer(tid)
            if self.tasks.get(tid, {}).get("status") != "active":
                cancelled += 1
        if cancelled > 0:
            await self.save_tasks()
            await self._update_sensor()
            _LOGGER.info(f"[timer_elves] Cancelled {cancelled} task(s) for {entity_id}")

    # ================================================================== #
    #  修改任务                                                             #
    # ================================================================== #

    async def update_task(self, task_id: str, patch: dict) -> dict:
        """原地修改活跃任务。

        - 一次性定时器：改 duration 后按「此刻 + 新时长」重算结束时间并重排句柄
        - 周期任务：改 repeat_type / schedule_time / weekdays / month_days 后重排下一次执行
        - 两者都可改 action_type / action_data（会重新生成动作）

        返回修改后的任务数据；任务不存在或已结束时抛 ValueError。
        """
        patch = patch or {}
        td = self.tasks.get(task_id)
        if not td:
            raise ValueError(f"任务不存在: {task_id}")
        if td.get("status") != "active":
            raise ValueError(f"任务已结束，无法修改（status={td.get('status')}）")

        entity_id = td.get("entity_id")
        state = self.hass.states.get(entity_id)
        if state is None:
            raise ValueError(f"实体不存在: {entity_id}")

        # 动作相关补丁（action_data / action_config / 顶层参数都支持，命名对齐动作目录）
        if patch.get("action_data") is not None or patch.get("action_config") is not None \
                or patch.get("climate_mode") is not None or any(
                    k in patch for k in self._ACTION_PARAM_KEYS
                ):
            raw = {"action_data": dict(td.get("action_data") or {})}
            raw.update({k: patch[k] for k in self._ACTION_PARAM_KEYS if k in patch})
            if patch.get("climate_mode") is not None:
                raw["climate_mode"] = patch["climate_mode"]
            if patch.get("action_config") is not None:
                raw["action_config"] = patch["action_config"]
            td["action_data"] = self._normalize_action_data(raw)
        action_data = td.get("action_data") or {}
        # 动作名：显式 patch.action_type 优先，其次由 action_config 推导，最后沿用原动作
        if patch.get("action_type") or patch.get("action_config"):
            td["action_type"] = self._resolve_action_type(
                patch, action_data, default=td.get("action_type") or "auto")
        if td.get("is_climate"):
            td["action"] = self.generate_climate_action(
                entity_id, td.get("action_type", "turn_off"), action_data,
                previous_state=td.get("previous_state"))
        elif td.get("is_cover"):
            td["action"] = self.generate_cover_action(
                entity_id, td.get("action_type", "close"), action_data,
                previous_state=td.get("previous_state"))
        else:
            td["action"] = self.generate_action(
                entity_id, td.get("action_type", "auto"), state.state, action_data)

        if td.get("is_recurring"):
            for key in ("repeat_type", "schedule_time", "weekdays", "month_days"):
                if patch.get(key) not in (None, ""):
                    td[key] = patch[key]
            old_handle = self.recurring_timers.pop(task_id, None)
            if old_handle:
                old_handle()
            await self.schedule_recurring_timer(task_id, td)
        else:
            # 一次性定时器不接受周期字段（避免「改了但没生效」的误解）
            if any(
                patch.get(k) not in (None, "")
                for k in ("repeat_type", "schedule_time", "weekdays", "month_days")
            ):
                raise ValueError("一次性定时器不能改成周期任务，请取消后新建周期任务")
            if patch.get("duration"):
                td["duration"] = patch["duration"]
            duration = self.parse_duration(td.get("duration", "00:30:00"))
            for key in (task_id, f"{task_id}_pre"):
                old_handle = self.timers.pop(key, None)
                if old_handle:
                    old_handle()
            start_time = self.get_local_now()
            end_time = start_time + duration
            td["start_time"] = self.datetime_to_iso(start_time)
            td["end_time"] = self.datetime_to_iso(end_time)
            if td.get("is_climate"):
                callback = lambda n, tid=task_id: self.execute_climate_timer(n, tid)
            elif td.get("is_cover"):
                callback = lambda n, tid=task_id: self.execute_cover_timer(n, tid)
            else:
                callback = lambda n, tid=task_id: self.execute_timer(n, tid)
            self.timers[task_id] = async_track_point_in_time(self.hass, callback, end_time)
            pre_time = end_time - timedelta(seconds=10)
            if pre_time > start_time:
                self.timers[f"{task_id}_pre"] = async_track_point_in_time(
                    self.hass, lambda n, tid=task_id: self._pre_capture_state(n, tid), pre_time)
            self.entity_timers[entity_id] = task_id

        await self.save_tasks(force=True)
        await self.update_stats()
        await self._update_sensor()
        await self.send_all_timers()
        _LOGGER.info(f"[timer_elves] Task updated: {task_id}")
        local = get_logger()
        if local is not None:
            local.info(
                "[定时精灵] 已修改任务 实体=%s 任务ID=%s 到点动作=%s",
                td.get("entity_name") or td.get("entity_id") or "-", task_id,
                (td.get("action") or {}).get("description", "-"),
            )
        return td

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
                            # 周期任务同样带上动作参数（卡片显示「模式 + 温度」要用）
                            "action_data": timer.get("action_data", {}),
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
                        # 仅当该索引确实指向本任务时才清理，避免误删同实体的其它任务
                        if self.entity_timers.get(eid) == tid:
                            del self.entity_timers[eid]
                        # 弹出并「取消」底层句柄：只 del 引用会让定时器继续存在，
                        # 到点仍会触发一次执行（重复动作）
                        for key in (tid, f"{tid}_pre"):
                            handle = self.timers.pop(key, None)
                            if handle:
                                try:
                                    handle()
                                except Exception:
                                    pass
                        continue
                    info = {
                        "timer_id": tid, "entity_id": timer["entity_id"],
                        "entity_name": timer["entity_name"],
                        "duration": timer["duration"], "end_time": timer["end_time"],
                        "remaining_seconds": remaining,
                        "action": self.get_action_description(timer["action"]),
                        # 卡片任务条要按「模式 + 温度」这类结构化参数渲染（如【制冷 25℃】），
                        # 只给一句 action 描述不够 —— 缺这两个字段时卡片只能退化成【开启】。
                        # 与 schedules 分支保持同名字段，前端一套逻辑即可通吃。
                        "action_type": timer.get("action_type", "auto"),
                        "action_data": timer.get("action_data", {}),
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
        payload = {
            "active_tasks": active_timers + active_schedules,
            "active_timers": active_timers,
            "active_schedules": active_schedules,
            "total_tasks": len(all_task_list),
            "current_task": active_timers + active_schedules,
            "successful_task": self.stats["successful_task"],
            "failed_task": self.stats["failed_task"],
            "today_task": self.stats["today_task"],
            "all_task_list": all_task_list,
        }
        if self._in_event_loop():
            async_dispatcher_send(self.hass, TIMER_SIGNAL_UPDATE_SENSOR, payload)
        else:
            # 非 loop 线程派发时回调会在该线程执行，必须切回 loop
            self.hass.loop.call_soon_threadsafe(
                async_dispatcher_send, self.hass, TIMER_SIGNAL_UPDATE_SENSOR, payload
            )

    # ================================================================== #
    #  查询：概览 / 历史                                                      #
    # ================================================================== #

    async def get_summary(self) -> dict:
        """当前概览：活跃定时器 + 活跃周期任务 + 统计。"""
        timers = [
            td for td in self.tasks.values()
            if not td.get("is_recurring") and td.get("status") == "active"
        ]
        schedules = [
            td for td in self.tasks.values()
            if td.get("is_recurring") and td.get("status") == "active"
        ]
        return {
            "active_timers": len(timers),
            "active_schedules": len(schedules),
            "active_tasks": len(timers) + len(schedules),
            "total_tasks": len(self.tasks),
            "successful_task": self.stats.get("successful_task", 0),
            "failed_task": self.stats.get("failed_task", 0),
            "today_task": self.stats.get("today_task", 0),
            "time_zone": self.time_zone,
            "timers": timers,
            "schedules": schedules,
        }

    async def get_task(self, task_id: str) -> dict | None:
        """按 id 取单个任务（含内存态最新数据，未命中返回 None）。"""
        return self.tasks.get(task_id)

    async def get_history(
        self,
        limit: int = 50,
        offset: int = 0,
        entity_id: str = None,
        status: str = None,
        start: str = None,
        end: str = None,
        with_data: bool = False,
    ) -> dict:
        """从 SQLite 查历史任务（status != 'active'），支持分页与过滤。

        start / end 按 created_at 前缀比较，可传 'YYYY-MM-DD' 或 'YYYY-MM-DD HH:MM:SS'；
        end 传纯日期时自动补到当天 23:59:59。
        """
        limit = max(1, min(int(limit or 50), 500))
        offset = max(0, int(offset or 0))

        def _query():
            conn = sqlite3.connect(self.db_path)
            try:
                conn.row_factory = sqlite3.Row
                where = ["status != 'active'"]
                params: list = []
                if entity_id:
                    where.append("entity_id = ?")
                    params.append(entity_id)
                if status:
                    where.append("status = ?")
                    params.append(status)
                if start:
                    where.append("created_at >= ?")
                    params.append(start)
                if end:
                    where.append("created_at <= ?")
                    params.append(end + " 23:59:59" if len(end) == 10 else end)
                where_sql = " AND ".join(where)
                total = conn.execute(
                    f"SELECT COUNT(*) FROM {TABLE_TIMER_TASKS} WHERE {where_sql}", params
                ).fetchone()[0]
                extra = ", task_data" if with_data else ""
                rows = conn.execute(
                    f"SELECT task_id, entity_id, entity_name, task_type, status, action_desc, "
                    f"duration, repeat_type, created_at, end_time, executed_at, "
                    f"execution_result, updated_at{extra} "
                    f"FROM {TABLE_TIMER_TASKS} WHERE {where_sql} "
                    f"ORDER BY COALESCE(NULLIF(executed_at, ''), created_at) DESC "
                    f"LIMIT ? OFFSET ?",
                    [*params, limit, offset],
                ).fetchall()
                return total, [dict(r) for r in rows]
            finally:
                conn.close()

        total, items = await self.hass.async_add_executor_job(_query)
        if with_data:
            for item in items:
                raw = item.pop("task_data", None)
                if raw:
                    try:
                        item["data"] = json.loads(raw)
                    except Exception:
                        item["data"] = None
        return {
            "total": total,
            "count": len(items),
            "limit": limit,
            "offset": offset,
            "items": items,
        }

    async def get_entity_tasks(
        self,
        entities: list | None = None,
        include_history: bool = False,
        active_only: bool = False,
        is_recurring: bool | None = None,
        limit: int = 500,
    ) -> dict:
        """查询一个或多个实体的定时任务（内存态）。

        - entities 为空 = 不限实体；传多个实体时按实体分组返回
        - 默认只返回活跃任务；include_history=True 时把已完成/失败/取消的一并纳入
        - is_recurring=True 只看周期任务，False 只看一次性定时器
        说明：内存中的历史受 MAX_HISTORY_RECORDS 上限裁剪，需要完整历史请走 get_history()。
        """
        wanted = {e for e in (entities or []) if e}
        try:
            limit = max(1, min(int(limit or 500), 2000))
        except (TypeError, ValueError):
            limit = 500
        items: list = []
        groups: dict = {}
        for tid, td in self.tasks.items():
            entity_id = td.get("entity_id", "")
            if wanted and entity_id not in wanted:
                continue
            is_active = td.get("status") == "active"
            # active_only 强制「只看活跃」；未指定时默认也只返回活跃，除非显式要求 include_history
            if not is_active and (active_only or not include_history):
                continue
            if is_recurring is not None and bool(td.get("is_recurring")) is not is_recurring:
                continue
            item = dict(td)
            item["id"] = tid
            items.append(item)
            group = groups.setdefault(entity_id, {
                "entity_id": entity_id,
                "entity_name": td.get("entity_name", entity_id),
                "count": 0,
                "active": 0,
                "active_timers": 0,
                "active_schedules": 0,
                "tasks": [],
            })
            group["count"] += 1
            if is_active:
                group["active"] += 1
                if td.get("is_recurring"):
                    group["active_schedules"] += 1
                else:
                    group["active_timers"] += 1
            group["tasks"].append(item)
        items.sort(key=lambda x: x.get("end_time") or x.get("created_at") or "")
        return {
            "entities": sorted(wanted) if wanted else sorted(groups.keys()),
            "total": len(items),
            "count": len(items[:limit]),
            "include_history": include_history,
            "by_entity": sorted(groups.values(), key=lambda g: g["entity_id"]),
            "tasks": items[:limit],
        }

    async def get_month_dates(
        self,
        month: str,
        entities: list | None = None,
        basis: str = "created",
    ) -> dict:
        """查询指定月内哪些日期有定时任务数据（可选按实体/多实体过滤）。

        - basis='created'  按任务创建时间统计（默认）
        - basis='executed' 按任务实际执行时间统计（只统计已执行过的）
        返回：dates（按日期聚合，附各实体条数）与 by_entity（按实体列出有数据的日期）。
        """
        month = (month or "").strip()
        if not month:
            raise ValueError("month 必填，格式 YYYY-MM")
        column = "executed_at" if basis == "executed" else "created_at"
        wanted = [e for e in (entities or []) if e]

        def _query():
            conn = sqlite3.connect(self.db_path)
            try:
                conn.row_factory = sqlite3.Row
                where = [f"substr({column},1,7) = ?"]
                params: list = [month]
                if basis == "executed":
                    where.append(f"{column} != ''")
                if wanted:
                    placeholders = ",".join(["?"] * len(wanted))
                    where.append(f"entity_id IN ({placeholders})")
                    params.extend(wanted)
                rows = conn.execute(
                    f"SELECT entity_id, substr({column},1,10) AS day, COUNT(*) AS count "
                    f"FROM {TABLE_TIMER_TASKS} WHERE {' AND '.join(where)} "
                    f"GROUP BY entity_id, substr({column},1,10) ORDER BY day ASC",
                    params,
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

        rows = await self.hass.async_add_executor_job(_query)
        day_map: dict = {}
        entity_map: dict = {}
        for row in rows:
            day = row.get("day") or ""
            entity_id = row.get("entity_id") or ""
            count = int(row.get("count") or 0)
            if not day:
                continue
            slot = day_map.setdefault(day, {"day": day, "count": 0, "entities": {}})
            slot["count"] += count
            slot["entities"][entity_id] = slot["entities"].get(entity_id, 0) + count
            entity_slot = entity_map.setdefault(entity_id, {
                "entity_id": entity_id, "count": 0, "days": [],
            })
            entity_slot["count"] += count
            if day not in entity_slot["days"]:
                entity_slot["days"].append(day)
        return {
            "month": month,
            "basis": basis,
            "entities": sorted(entity_map.keys()),
            "count": len(day_map),
            "dates": [day_map[d] for d in sorted(day_map.keys())],
            "by_entity": sorted(entity_map.values(), key=lambda x: x["entity_id"]),
        }

    async def get_entity_daily_tasks(
        self,
        date: str,
        entities: list | None = None,
        basis: str = "created",
        status: str | None = None,
        limit: int = 500,
    ) -> dict:
        """查询指定实体（可多个）在某一天的定时任务。

        - basis='created'  按任务创建日期（默认）
        - basis='executed' 按任务实际执行日期（只计已执行的）
        - status 可选过滤（active / completed / failed / cancelled / error）

        数据来源 = SQLite 历史（含已从内存裁剪的旧记录）+ 内存中的当前任务（内存优先），
        因此刚创建、尚未落盘的任务（save_tasks 有 5 秒节流）也能查到。
        """
        date = (date or "").strip()
        if not date:
            raise ValueError("date 必填，格式 YYYY-MM-DD")
        column = "executed_at" if basis == "executed" else "created_at"
        wanted = [e for e in (entities or []) if e]
        wanted_set = set(wanted)
        try:
            limit = max(1, min(int(limit or 500), 2000))
        except (TypeError, ValueError):
            limit = 500

        def _query():
            conn = sqlite3.connect(self.db_path)
            try:
                conn.row_factory = sqlite3.Row
                where = [f"substr({column},1,10) = ?"]
                params: list = [date]
                if basis == "executed":
                    where.append(f"{column} != ''")
                if wanted:
                    placeholders = ",".join(["?"] * len(wanted))
                    where.append(f"entity_id IN ({placeholders})")
                    params.extend(wanted)
                if status:
                    where.append("status = ?")
                    params.append(status)
                rows = conn.execute(
                    f"SELECT task_id, task_data FROM {TABLE_TIMER_TASKS} "
                    f"WHERE {' AND '.join(where)} ORDER BY {column} ASC LIMIT ?",
                    [*params, limit],
                ).fetchall()
                out = []
                for row in rows:
                    try:
                        task = json.loads(row["task_data"])
                    except Exception:
                        continue
                    task["id"] = row["task_id"]
                    out.append(task)
                return out
            finally:
                conn.close()

        merged: dict = {}
        for task in await self.hass.async_add_executor_job(_query):
            tid = task.get("id")
            if not tid:
                continue
            if tid in self.tasks:
                # 内存中仍在的任务一律以内存为准（DB 可能是 5 秒节流前的旧快照），
                # 避免同一任务以「旧状态 + 新状态」重复出现
                continue
            merged[tid] = task

        # 内存态覆盖（内存里的是最新状态；未落盘的新任务也在这里补上）
        for tid, td in self.tasks.items():
            entity_id = td.get("entity_id", "")
            if wanted_set and entity_id not in wanted_set:
                continue
            if status and td.get("status") != status:
                continue
            stamp = td.get("executed_at") if basis == "executed" else (
                td.get("created_at") or td.get("creation_time"))
            if not stamp or str(stamp)[:10] != date:
                continue
            item = dict(td)
            item["id"] = tid
            merged[tid] = item

        items = list(merged.values())
        items.sort(key=lambda x: str(
            x.get("executed_at") if basis == "executed" else (x.get("created_at") or "")
        ))
        items = items[:limit]

        groups: dict = {}
        for item in items:
            entity_id = item.get("entity_id", "")
            group = groups.setdefault(entity_id, {
                "entity_id": entity_id,
                "entity_name": item.get("entity_name", entity_id),
                "count": 0,
                "active": 0,
                "active_timers": 0,
                "active_schedules": 0,
                "tasks": [],
            })
            group["count"] += 1
            if item.get("status") == "active":
                group["active"] += 1
                if item.get("is_recurring"):
                    group["active_schedules"] += 1
                else:
                    group["active_timers"] += 1
            group["tasks"].append(item)
        return {
            "date": date,
            "basis": basis,
            "status": status or "",
            "entities": sorted(wanted) if wanted else sorted(groups.keys()),
            "count": len(items),
            "by_entity": sorted(groups.values(), key=lambda g: g["entity_id"]),
            "tasks": items,
        }

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
            # 裁剪内存历史，并同步删除库中对应行（否则表会无限增长）
            removed = self._prune_memory()
            await self.save_tasks()
            if removed:
                await self._delete_tasks_from_db(removed)
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

            # 任务摘要（体积敏感）：只保留前端渲染所需字段，历史最多 TIMER_BRIEF_LIMIT 条。
            # 完整任务数据请走 API（/timer/tasks、/timer/history）——
            # 曾因把全量字段塞进状态属性，导致体积超过 HA recorder 的 16384 字节上限。
            active_briefs, history_briefs = [], []
            for task_id, td in self.tasks.items():
                brief = self._build_task_brief(task_id, td)
                if td.get("status") == "active":
                    active_briefs.append(brief)
                else:
                    history_briefs.append(brief)
            history_briefs.sort(
                key=lambda x: x.get("executed_at") or x.get("created_at") or "", reverse=True
            )
            self.stats["all_task_list"] = active_briefs + history_briefs[:TIMER_BRIEF_LIMIT]
            self.stats["brief_truncated"] = max(0, len(history_briefs) - TIMER_BRIEF_LIMIT)
        except Exception as e:
            _LOGGER.error(f"[timer_elves] update_stats failed: {e}")

    def _build_task_brief(self, task_id: str, td: dict) -> dict:
        """构造任务摘要（供传感器状态属性与事件负载使用）。

        体积敏感：只保留前端渲染必需字段，**不要**加回 action / previous_state /
        action_data / restore_data 这类大字段（曾导致属性超过 recorder 的 16384 字节上限）。
        完整任务数据走 API（/timer/tasks、/timer/history）。
        """
        keys = (
            "entity_id", "entity_name", "status", "is_recurring",
            "day", "start_time", "end_time", "executed_at", "created_at",
            "cancelled_at", "last_executed", "next_execution",
            "before_entity_state", "after_entity_state", "execution_result",
        )
        if td.get("is_recurring"):
            action_text = self._get_task_action_description(td)
        else:
            action = td.get("action") or {}
            action_text = (
                action.get("description") or action.get("service") or td.get("task_action", "未知")
            )
        brief = {"id": task_id, "timer_id": task_id}
        for key in keys:
            value = td.get(key)
            if value not in (None, ""):
                brief[key] = value
        brief["task_type"] = "周期任务" if td.get("is_recurring") else "定时任务"
        brief["task_action"] = action_text
        for tk in ("created_at", "executed_at", "cancelled_at", "start_time",
                   "end_time", "next_execution", "last_executed"):
            if brief.get(tk):
                brief[tk] = self._convert_to_local_time_str(brief[tk])
        if not brief.get("execution_result"):
            brief["execution_result"] = {
                "completed": "success", "failed": "failed", "error": "failed",
                "cancelled": "cancelled", "expired": "unknown", "active": "",
            }.get(td.get("status", "unknown"), "unknown")
        elif brief.get("execution_result") in ("", "unknown"):
            status = td.get("status", "unknown")
            if status == "completed":
                brief["execution_result"] = "success"
            elif status in ("failed", "error"):
                brief["execution_result"] = "failed"
            elif status == "cancelled":
                brief["execution_result"] = "cancelled"
        if "after_entity_state" not in brief and td.get("status") in ("completed", "failed", "error"):
            brief["after_entity_state"] = td.get("after_entity_state", "unknown")
        if not brief.get("day"):
            created = td.get("created_at") or td.get("creation_time") or ""
            if created:
                brief["day"] = created.split("T")[0] if "T" in created else created.split(" ")[0]
        return brief

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
            self.hass.add_job(self._async_pre_capture_state, timer_id)

    async def _async_pre_capture_state(self, timer_id: str) -> None:
        if timer_id in self.tasks:
            timer = self.tasks[timer_id]
            eid = timer["entity_id"]
            bs = self.hass.states.get(eid)
            timer["before_entity_state"] = bs.state if bs else "unknown"
            await self.save_tasks()

    async def _run_extra_calls(self, action: dict, key: str = "extra_calls") -> None:
        """执行附加调用。

        key='pre_calls'   → 主调用之前执行（如设温度前先切模式，兼容忽略 hvac_mode 的实体）
        key='extra_calls' → 主调用之后执行（如设风速 / 摆风 / 预设）

        单个附加调用失败只记警告，不影响主调用的成功判定。
        """
        for extra in action.get(key) or []:
            try:
                ex_service = str(extra.get("service") or "")
                if "." not in ex_service:
                    continue
                ex_domain, ex_name = ex_service.split(".", 1)
                await self.hass.services.async_call(
                    ex_domain, ex_name, extra.get("data", {}), blocking=True)
            except Exception as exc:
                _LOGGER.warning(f"[timer_elves] 附加动作执行失败 {extra}: {exc}")

    def _in_event_loop(self) -> bool:
        """当前是否运行在 HA 事件循环线程内。"""
        try:
            return asyncio.get_running_loop() is self.hass.loop
        except RuntimeError:
            return False

    def _fire_event(self, action: str, data: dict) -> None:
        """触发定时精灵总线事件（线程安全）。

        dispatcher 与总线 fire 都不会切线程：在哪个线程调用，回调就在哪个线程执行。
        定时到点的回调有可能来自非事件循环线程，这里显式切回 loop，
        否则下游（如传感器实体里的 async_create_task）会触发 HA 的线程安全告警。
        """
        payload = dict(data or {})
        inner_action = payload.pop("action", None)
        event_data = {"action": action, "source": "timer_elves", **payload}
        if inner_action and inner_action != action:
            # 原 data 里的 action（如 update_task / get_history）改名保留为 source_action，
            # 否则会覆盖事件主 action，导致前端认不出 error 事件
            event_data["source_action"] = inner_action
        self._log_event_locally(action, event_data)
        if self._in_event_loop():
            self.hass.bus.async_fire(TIMER_RESPONSE_EVENT, event_data)
        else:
            self.hass.loop.call_soon_threadsafe(
                self.hass.bus.async_fire, TIMER_RESPONSE_EVENT, event_data
            )

    def _log_event_locally(self, action: str, data: dict) -> None:
        """把定时精灵的关键事件写入本地日志（集成目录 logs/YYYY-MM-DD.log）。

        与 _fire_event 同源，因此「创建 / 执行 / 取消 / 失败」全生命周期都会出现在
        db_viewer 的「日志查看」页（该页读的就是本地日志文件，支持关键字搜索）。
        列表推送（timers_list / schedules_list）与查询结果（history_result）不记录，避免噪声。
        """
        try:
            if action in ("timers_list", "schedules_list", "history_result"):
                return
            local = get_logger()
            if local is None:
                return
            entity = data.get("entity_name") or data.get("entity_id") or "-"
            if action == "timer_created":
                local.info(
                    "[定时精灵] 创建定时器 实体=%s 时长=%s 到点动作=%s 结束时间=%s",
                    entity, data.get("duration", "-"),
                    data.get("action_description", "-"), data.get("end_time", "-"),
                )
            elif action == "schedule_created":
                local.info(
                    "[定时精灵] 创建周期任务 实体=%s 重复=%s 时刻=%s 到点动作=%s 下次执行=%s",
                    entity, data.get("repeat_type", "-"), data.get("schedule_time", "-"),
                    data.get("action_description", "-"), data.get("next_execution", "-"),
                )
            elif action == "timer_completed":
                if data.get("success"):
                    local.info(
                        "[定时精灵] 执行成功 实体=%s 到点动作=%s 状态 %s→%s",
                        entity, data.get("action_description", "-"),
                        data.get("before_entity_state", "-"), data.get("after_entity_state", "-"),
                    )
                else:
                    local.warning(
                        "[定时精灵] 执行失败 实体=%s 到点动作=%s 状态 %s→%s",
                        entity, data.get("action_description", "-"),
                        data.get("before_entity_state", "-"), data.get("after_entity_state", "-"),
                    )
            elif action == "schedule_executed":
                if data.get("success"):
                    local.info(
                        "[定时精灵] 周期任务执行成功 实体=%s 重复=%s 状态 %s→%s",
                        entity, data.get("repeat_type", "-"),
                        data.get("before_entity_state", "-"), data.get("after_entity_state", "-"),
                    )
                else:
                    local.warning(
                        "[定时精灵] 周期任务执行失败 实体=%s 重复=%s 状态 %s→%s",
                        entity, data.get("repeat_type", "-"),
                        data.get("before_entity_state", "-"), data.get("after_entity_state", "-"),
                    )
            elif action == "timer_cancelled":
                local.info("[定时精灵] 已取消定时器 实体=%s", entity)
            elif action == "schedule_cancelled":
                local.info("[定时精灵] 已取消周期任务 实体=%s", entity)
            elif action == "error":
                local.warning(
                    "[定时精灵] 操作失败 环节=%s 错误=%s",
                    data.get("source_action") or "-", data.get("error", "-"),
                )
        except Exception:
            pass  # 日志写入失败绝不影响定时逻辑

    def _convert_to_local_time_str(self, iso_str: str) -> str:
        if not iso_str:
            return ""
        try:
            dt = self.iso_to_datetime(iso_str)
            return self.datetime_to_iso(dt)
        except Exception:
            return iso_str
