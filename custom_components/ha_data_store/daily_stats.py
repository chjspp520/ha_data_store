"""日积月累（daily_stats）：按实体采集每日开启/关闭与使用量，并生成统计实体。

功能
----
用户在前台（db_viewer → 系统配置 → 📆 日积月累）配置一个源实体后，本模块：

1. 监听该实体的状态变化，**只在「开启 ↔ 关闭」判定翻转时动作**；
   同一状态的不同取值（如 climate 的 cool→heat、cover 的 open→opening）不算翻转，
   不写库、不写状态；unavailable / unknown 不判定，会话保持原状。
2. 开启时写一条会话行（on_time），关闭时回填 off_time / duration / usage。
3. 跨午夜仍在开启：00:00 把上一段关在 23:59:59，并补一段 00:00:00 起的续接会话
   （所以 daylist 每天都能看到完整时段）。
4. 每条配置生成一个传感器 ``sensor.hds_ds_<stat_id>``：
   - state 可选：count（今日开关次数，默认）/ usage（今日使用量）/
     duration（今日开启时长，分钟）/ state（当前开启/关闭）
   - 属性 daylist 全量（不裁剪）列出每天的汇总与逐段会话
   - monthlist / yearlist 由配置勾选生成，实时从日数据聚合，不落表
5. 使用量两种口径：
   - ``entity``：绑定一个累计读数实体，会话用量 = 关闭读数 − 开启读数（× 换算系数）
   - ``power`` ：给定固定功率 W，会话用量 = W/1000 × 时长(小时)

开关判定
--------
总原则「非 off / 非未知 / 非离线 = 开启」，再按域补词表：
  climate：off 才算关；cover：open/opening 算开；media_player：off/standby 算关；
  vacuum：docked/idle/off 算关；device_tracker/person：home 算开；lock：locked 算关。
纯数值域（sensor/number/input_number/counter）没有开关语义，只有字面量
on/off/true/false/open/closed 才判定，其余返回 None（不产生会话）。

时间口径
--------
全部使用本地时间字符串 ``YYYY-MM-DD HH:MM:SS``（时区偏移取
``hass.data[DOMAIN]["timezone"]``，缺省东八区），与集成其它模块一致。

已知限制
--------
HA 停机期间无法感知开关变化：重启对账时用 ``last_changed`` 兜底，
跨天补出来的会话标记 ``restored=1``；``entity`` 口径在这些补出来的段上
``usage_known=0``（不参与使用量求和），避免把停机期间的读数差算进某一天。
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime, timedelta

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.components.sensor import SensorEntity
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.event import async_track_time_change

from .const import (
    DAILY_STATS_DEVICE_IDENT,
    DAILY_STATS_DEVICE_NAME,
    DAILY_STATS_ENTITY_PREFIX,
    DAILY_STATS_MANUFACTURER,
    DEFAULT_TIMEZONE,
    DOMAIN,
    TABLE_DAILY_STATS_CONFIGS,
    TABLE_DAILY_STATS_SESSIONS,
)

_LOGGER = logging.getLogger(__name__)

# ── 枚举与校验 ──────────────────────────────────────────────────────────────
STATE_METRICS = ("count", "usage", "duration", "state")
USAGE_MODES = ("none", "entity", "power")

STAT_ID_RE = re.compile(r"^[a-z0-9_]{1,40}$")

_UNKNOWN_STATES = {"unavailable", "unknown", "", "none"}

# 域级「开启」词表（命中即：在集合内 = 开，否则 = 关）
_ON_STATE_MAP = {
    "cover": {"open", "opening"},
    "device_tracker": {"home"},
    "person": {"home"},
}

# 域级「关闭」词表（命中即：在集合内 = 关，否则 = 开）
_OFF_STATE_MAP = {
    "climate": {"off"},
    "water_heater": {"off"},
    "media_player": {"off", "standby"},
    "vacuum": {"docked", "idle", "off"},
    "lock": {"locked"},
    "valve": {"closed", "closing"},
}

# 没有开关语义的域：只有字面量才判定，避免「任意数值都算开」
_NO_SWITCH_DOMAINS = {"sensor", "number", "input_number", "counter", "proximity"}


def _empty_day(date: str = "") -> dict:
    """空的一天（每次新建，避免 sessions 列表被多个日期共享）。"""
    return {
        "date": date,
        "on": "",
        "off": "",
        "count": 0,
        "duration": 0,
        "usage": 0.0,
        "sessions": [],
    }


# =========================================================================== #
#  时间工具                                                                    #
# =========================================================================== #
def _tz_offset(hass) -> int:
    try:
        return int(hass.data.get(DOMAIN, {}).get("timezone", DEFAULT_TIMEZONE))
    except Exception:
        return DEFAULT_TIMEZONE


def _now(hass) -> datetime:
    return datetime.utcnow() + timedelta(hours=_tz_offset(hass))


def _now_str(hass) -> str:
    return _now(hass).strftime("%Y-%m-%d %H:%M:%S")


def _today_str(hass) -> str:
    return _now(hass).strftime("%Y-%m-%d")


def _utc_to_local_str(hass, dt) -> str:
    """把 HA 给的 UTC datetime 转成本地时间字符串（失败回退当前时间）。"""
    try:
        if dt is None:
            return _now_str(hass)
        off = dt.utcoffset()
        if off is not None:
            dt = dt.replace(tzinfo=None) - off
        return (dt + timedelta(hours=_tz_offset(hass))).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return _now_str(hass)


def _stamp() -> str:
    """落库用时间戳（本地时间，与表内其它时间列同口径）。"""
    return (datetime.utcnow() + timedelta(hours=DEFAULT_TIMEZONE)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def _parse_dt(text) -> datetime | None:
    try:
        return datetime.strptime(str(text)[:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _hhmmss(text) -> str:
    """'2026-11-22 08:12:03' → '08:12:03'（空值原样返回空串）。"""
    s = str(text or "")
    return s[11:19] if len(s) >= 19 else ""


# =========================================================================== #
#  开关判定                                                                    #
# =========================================================================== #
def classify_state(entity_id: str, state) -> str | None:
    """返回 'on' / 'off' / None（None = 不判定，会话保持原状）。"""
    s = str(state or "").strip().lower()
    if not s or s in _UNKNOWN_STATES:
        return None
    domain = entity_id.split(".", 1)[0] if "." in entity_id else ""
    on_set = _ON_STATE_MAP.get(domain)
    if on_set is not None:
        return "on" if s in on_set else "off"
    off_set = _OFF_STATE_MAP.get(domain)
    if off_set is not None:
        return "off" if s in off_set else "on"
    if domain in _NO_SWITCH_DOMAINS:
        if s in ("on", "true", "open"):
            return "on"
        if s in ("off", "false", "closed"):
            return "off"
        return None
    return "on" if s != "off" else "off"


# =========================================================================== #
#  建表                                                                        #
# =========================================================================== #
def ensure_tables(db_path: str) -> None:
    """幂等建表（由 async_start 在 executor 中调用）。"""
    if not db_path:
        return
    conn = sqlite3.connect(db_path, timeout=15)
    try:
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE_DAILY_STATS_CONFIGS} (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                stat_id         TEXT NOT NULL UNIQUE,
                name            TEXT NOT NULL DEFAULT '',
                icon            TEXT NOT NULL DEFAULT 'mdi:calendar-clock',
                entity_id       TEXT NOT NULL DEFAULT '',
                state_metric    TEXT NOT NULL DEFAULT 'count',
                usage_mode      TEXT NOT NULL DEFAULT 'none',
                usage_entity_id TEXT NOT NULL DEFAULT '',
                usage_power_w   REAL,
                usage_unit      TEXT NOT NULL DEFAULT '',
                usage_scale     REAL NOT NULL DEFAULT 1.0,
                opt_monthlist   INTEGER NOT NULL DEFAULT 0,
                opt_yearlist    INTEGER NOT NULL DEFAULT 0,
                enabled         INTEGER NOT NULL DEFAULT 1,
                created_at      TEXT NOT NULL DEFAULT '',
                updated_at      TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE_DAILY_STATS_SESSIONS} (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                stat_id     TEXT NOT NULL,
                date        TEXT NOT NULL DEFAULT '',
                on_time     TEXT NOT NULL DEFAULT '',
                off_time    TEXT NOT NULL DEFAULT '',
                duration    REAL,
                usage       REAL,
                usage_known INTEGER NOT NULL DEFAULT 1,
                on_reading  REAL,
                off_reading REAL,
                restored    INTEGER NOT NULL DEFAULT 0,
                updated_at  TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_dstats_sess_stat_date "
            f"ON {TABLE_DAILY_STATS_SESSIONS} (stat_id, date)"
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_dstats_sess_open "
            f"ON {TABLE_DAILY_STATS_SESSIONS} (stat_id, off_time)"
        )
        conn.commit()
    finally:
        conn.close()


# =========================================================================== #
#  配置 CRUD（同步，供 executor / 事件循环内小读取复用）                          #
# =========================================================================== #
def _open_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def _row_to_cfg(row) -> dict:
    d = dict(row)
    d["opt_monthlist"] = int(d.get("opt_monthlist") or 0)
    d["opt_yearlist"] = int(d.get("opt_yearlist") or 0)
    d["enabled"] = int(d.get("enabled") or 0)
    d["usage_scale"] = float(d.get("usage_scale") or 1.0)
    d["usage_power_w"] = (float(d["usage_power_w"])
                          if d.get("usage_power_w") is not None else None)
    return d


def list_configs_sync(db_path: str, enabled_only: bool = False) -> list[dict]:
    if not db_path:
        return []
    conn = _open_db(db_path)
    try:
        sql = f"SELECT * FROM {TABLE_DAILY_STATS_CONFIGS}"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY id"
        return [_row_to_cfg(r) for r in conn.execute(sql).fetchall()]
    finally:
        conn.close()


def get_config_sync(db_path: str, stat_id: str) -> dict | None:
    if not db_path or not stat_id:
        return None
    conn = _open_db(db_path)
    try:
        row = conn.execute(
            f"SELECT * FROM {TABLE_DAILY_STATS_CONFIGS} WHERE stat_id = ?", (stat_id,)
        ).fetchone()
        return _row_to_cfg(row) if row else None
    finally:
        conn.close()


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", str(text or "").lower()).strip("_")
    return s[:40]


def suggest_stat_id(db_path: str, entity_id: str) -> str:
    """按源实体生成不冲突的 stat_id（如 switch.ke_ting_deng → ke_ting_deng）。"""
    obj = entity_id.split(".", 1)[1] if "." in entity_id else (entity_id or "stat")
    base = _slugify(obj) or "stat"
    base = base[:36]
    existing = {c["stat_id"] for c in list_configs_sync(db_path)}
    if base not in existing:
        return base
    for i in range(2, 100):
        cand = f"{base}_{i}"
        if cand not in existing:
            return cand
    return f"{base}_{datetime.utcnow().strftime('%H%M%S')}"  # 兜底：极端重名


def save_config_sync(db_path: str, data: dict) -> dict:
    """新建 / 更新配置。校验失败抛 ValueError（前端显示 400）。"""
    if not isinstance(data, dict):
        raise ValueError("参数格式错误")

    stat_id = str(data.get("stat_id") or "").strip().lower()
    entity_id = str(data.get("entity_id") or "").strip()

    old = get_config_sync(db_path, stat_id) if stat_id else None
    if not stat_id:
        if not entity_id:
            raise ValueError("stat_id 与 entity_id 不能同时为空")
        stat_id = suggest_stat_id(db_path, entity_id)
    if not STAT_ID_RE.match(stat_id):
        raise ValueError("stat_id 只能是小写字母/数字/下划线（1~40 位）")
    if "." not in entity_id:
        raise ValueError("源实体 ID 不合法（需形如 switch.xxx）")
    if old and old.get("entity_id") and old["entity_id"] != entity_id:
        raise ValueError("源实体不允许修改；需要换实体请新建配置")

    state_metric = str(data.get("state_metric") or "count").strip().lower()
    if state_metric not in STATE_METRICS:
        raise ValueError(f"state 指标不合法：{state_metric}")

    usage_mode = str(data.get("usage_mode") or "none").strip().lower()
    if usage_mode not in USAGE_MODES:
        raise ValueError(f"使用量模式不合法：{usage_mode}")

    usage_entity_id = str(data.get("usage_entity_id") or "").strip()
    usage_power_w = data.get("usage_power_w")
    if usage_mode == "entity":
        if "." not in usage_entity_id:
            raise ValueError("使用量模式为「读数实体」时必须选择用电量实体")
    else:
        usage_entity_id = ""
    if usage_mode == "power":
        try:
            usage_power_w = float(usage_power_w)
        except Exception:
            raise ValueError("固定功率必须是数字（单位 W）")
        if usage_power_w <= 0:
            raise ValueError("固定功率必须大于 0")
    else:
        usage_power_w = None
    if state_metric == "usage" and usage_mode == "none":
        raise ValueError("state 指标选「今日使用量」时必须配置使用量")

    try:
        usage_scale = float(data.get("usage_scale") or 1.0)
    except Exception:
        usage_scale = 1.0
    if usage_scale <= 0:
        usage_scale = 1.0

    usage_unit = str(data.get("usage_unit") or "").strip()[:16]
    name = str(data.get("name") or "").strip()[:60] or entity_id
    icon = str(data.get("icon") or "").strip()[:64] or "mdi:calendar-clock"
    opt_monthlist = 1 if data.get("opt_monthlist") else 0
    opt_yearlist = 1 if data.get("opt_yearlist") else 0
    enabled = 0 if data.get("enabled") in (0, False, "0", "false") else 1

    now = _stamp()
    conn = _open_db(db_path)
    try:
        if old:
            conn.execute(
                f"""UPDATE {TABLE_DAILY_STATS_CONFIGS} SET
                      name=?, icon=?, state_metric=?, usage_mode=?, usage_entity_id=?,
                      usage_power_w=?, usage_unit=?, usage_scale=?,
                      opt_monthlist=?, opt_yearlist=?, enabled=?, updated_at=?
                    WHERE stat_id=?""",
                (name, icon, state_metric, usage_mode, usage_entity_id,
                 usage_power_w, usage_unit, usage_scale,
                 opt_monthlist, opt_yearlist, enabled, now, stat_id),
            )
        else:
            conn.execute(
                f"""INSERT INTO {TABLE_DAILY_STATS_CONFIGS}
                    (stat_id, name, icon, entity_id, state_metric, usage_mode,
                     usage_entity_id, usage_power_w, usage_unit, usage_scale,
                     opt_monthlist, opt_yearlist, enabled, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (stat_id, name, icon, entity_id, state_metric, usage_mode,
                 usage_entity_id, usage_power_w, usage_unit, usage_scale,
                 opt_monthlist, opt_yearlist, enabled, now, now),
            )
        conn.commit()
    finally:
        conn.close()
    return get_config_sync(db_path, stat_id) or {}


def delete_config_sync(db_path: str, stat_id: str, keep_history: bool = True) -> bool:
    if not db_path or not stat_id:
        return False
    conn = _open_db(db_path)
    try:
        cur = conn.execute(
            f"DELETE FROM {TABLE_DAILY_STATS_CONFIGS} WHERE stat_id = ?", (stat_id,)
        )
        deleted = cur.rowcount > 0
        if not keep_history:
            conn.execute(
                f"DELETE FROM {TABLE_DAILY_STATS_SESSIONS} WHERE stat_id = ?", (stat_id,)
            )
        conn.commit()
        return deleted
    finally:
        conn.close()


def clear_history_sync(db_path: str, stat_id: str) -> int:
    if not db_path or not stat_id:
        return 0
    conn = _open_db(db_path)
    try:
        cur = conn.execute(
            f"DELETE FROM {TABLE_DAILY_STATS_SESSIONS} WHERE stat_id = ?", (stat_id,)
        )
        conn.commit()
        return int(cur.rowcount or 0)
    finally:
        conn.close()


# =========================================================================== #
#  会话读写（同步）                                                             #
# =========================================================================== #
def get_open_session_sync(db_path: str, stat_id: str) -> dict | None:
    if not db_path or not stat_id:
        return None
    conn = _open_db(db_path)
    try:
        row = conn.execute(
            f"""SELECT * FROM {TABLE_DAILY_STATS_SESSIONS}
                WHERE stat_id = ? AND (off_time = '' OR off_time IS NULL)
                ORDER BY id DESC LIMIT 1""",
            (stat_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def insert_session_sync(db_path: str, stat_id: str, date: str, on_time: str,
                        on_reading=None, restored: int = 0) -> int:
    conn = _open_db(db_path)
    try:
        cur = conn.execute(
            f"""INSERT INTO {TABLE_DAILY_STATS_SESSIONS}
                (stat_id, date, on_time, off_time, duration, usage, usage_known,
                 on_reading, off_reading, restored, updated_at)
                VALUES (?, ?, ?, '', NULL, NULL, 0, ?, NULL, ?, ?)""",
            (stat_id, date, on_time, on_reading, int(restored), _stamp()),
        )
        conn.commit()
        return int(cur.lastrowid or 0)
    finally:
        conn.close()


def close_session_sync(db_path: str, sess_id: int, off_time: str, duration: float,
                       usage, usage_known: int, off_reading=None,
                       restored: int = 0) -> None:
    conn = _open_db(db_path)
    try:
        conn.execute(
            f"""UPDATE {TABLE_DAILY_STATS_SESSIONS} SET
                  off_time=?, duration=?, usage=?, usage_known=?,
                  off_reading=?, restored=?, updated_at=?
                WHERE id=?""",
            (off_time, round(float(duration or 0.0), 0),
             (round(float(usage), 3) if usage is not None else None),
             int(usage_known), off_reading, int(restored), _stamp(), int(sess_id)),
        )
        conn.commit()
    finally:
        conn.close()


def load_day_rows_sync(db_path: str, stat_id: str, date: str) -> list[dict]:
    if not db_path or not stat_id or not date:
        return []
    conn = _open_db(db_path)
    try:
        rows = conn.execute(
            f"""SELECT * FROM {TABLE_DAILY_STATS_SESSIONS}
                WHERE stat_id = ? AND date = ? ORDER BY on_time ASC, id ASC""",
            (stat_id, date),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def load_all_day_rows_sync(db_path: str, stat_id: str) -> dict[str, list[dict]]:
    """返回 {date: [rows]}；一次性取全量，供启动时建缓存（不限量）。"""
    if not db_path or not stat_id:
        return {}
    conn = _open_db(db_path)
    try:
        rows = conn.execute(
            f"""SELECT * FROM {TABLE_DAILY_STATS_SESSIONS}
                WHERE stat_id = ? ORDER BY date ASC, on_time ASC, id ASC""",
            (stat_id,),
        ).fetchall()
    finally:
        conn.close()
    out: dict[str, list[dict]] = {}
    for r in rows:
        d = r["date"] or _hhmmss_date(r["on_time"])
        out.setdefault(d, []).append(dict(r))
    return out


def _hhmmss_date(on_time) -> str:
    s = str(on_time or "")
    return s[:10]


def list_sessions_sync(db_path: str, stat_id: str, date: str = "",
                       start: str = "", end: str = "", limit: int = 500) -> list[dict]:
    if not db_path or not stat_id:
        return []
    conds = ["stat_id = ?"]
    params: list = [stat_id]
    if date:
        conds.append("date = ?")
        params.append(date)
    else:
        if start:
            conds.append("date >= ?")
            params.append(start)
        if end:
            conds.append("date <= ?")
            params.append(end)
    sql = (f"SELECT * FROM {TABLE_DAILY_STATS_SESSIONS} WHERE "
           + " AND ".join(conds) + " ORDER BY on_time DESC, id DESC")
    if limit and int(limit) > 0:
        sql += f" LIMIT {int(limit)}"
    conn = _open_db(db_path)
    try:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()
    for r in rows:
        r["on"] = _hhmmss(r["on_time"])
        r["off"] = _hhmmss(r["off_time"])
        r["duration"] = r.get("duration")
        r["usage"] = r.get("usage") if r.get("usage_known") else None
    return rows


# =========================================================================== #
#  日/月/年聚合                                                                 #
# =========================================================================== #
def _build_day(date: str, rows: list[dict], now: datetime) -> dict:
    """把某天的会话行汇总成 daylist 的一项（sessions 全展开）。"""
    if not rows:
        return _empty_day(date)
    count = 0
    duration = 0.0
    usage = 0.0
    first_on = ""
    last_off = ""
    sessions: list[dict] = []
    for r in rows:
        dur = r.get("duration")
        on_text = str(r.get("on_time") or "")
        off_text = str(r.get("off_time") or "")
        if dur is None and not off_text:
            st = _parse_dt(on_text)
            dur = max(0.0, (now - st).total_seconds()) if st is not None else 0.0
        dur = float(dur or 0.0)
        use = r.get("usage")
        if use is not None and int(r.get("usage_known") or 0):
            usage += float(use)
        else:
            use = None
        ses = {
            "on": _hhmmss(on_text),
            "off": _hhmmss(off_text),
            "dur": int(round(dur)),
        }
        if use is not None:
            ses["usage"] = round(float(use), 3)
        sessions.append(ses)
        count += 1
        duration += dur
        if not first_on:
            first_on = _hhmmss(on_text)
        if off_text:
            last_off = _hhmmss(off_text)
    return {
        "date": date,
        "on": first_on,
        "off": last_off,
        "count": count,
        "duration": int(round(duration)),
        "usage": round(usage, 3),
        "sessions": sessions,
    }


def _build_periods(days: dict[str, dict], field: str) -> list[dict]:
    """按月（field='month'）或按年（field='year'）聚合 daylist。"""
    key_len = 7 if field == "month" else 4
    acc: dict[str, dict] = {}
    for date in sorted(days):
        entry = days[date]
        key = date[:key_len]
        a = acc.get(key)
        if a is None:
            a = {field: key, "count": 0, "duration": 0, "usage": 0.0, "days": 0}
            acc[key] = a
        a["count"] += int(entry.get("count") or 0)
        a["duration"] += int(entry.get("duration") or 0)
        a["usage"] = round(a["usage"] + float(entry.get("usage") or 0.0), 3)
        a["days"] += 1
    return [acc[k] for k in sorted(acc)]


# =========================================================================== #
#  实体                                                                        #
# =========================================================================== #
class DailyStatSensor(SensorEntity):
    """一条配置对应的统计传感器（固定 entity_id：sensor.hds_ds_<stat_id>）。"""

    _attr_should_poll = False
    _attr_has_entity_name = False
    # recorder 单状态属性上限 16384 字节：大列表不进历史（state 值仍然记）
    _unrecorded_attributes = frozenset({"today", "daylist", "monthlist", "yearlist"})

    def __init__(self, mgr: "DailyStatsManager", cfg: dict) -> None:
        self._mgr = mgr
        self._cfg = cfg
        self._last_value = object()
        self._last_attrs: dict | None = None
        self._attr_unique_id = f"{DOMAIN}_dstats_{cfg['stat_id']}"
        self.entity_id = f"sensor.{DAILY_STATS_ENTITY_PREFIX}{cfg['stat_id']}"
        self._attr_device_info = mgr.device_info()
        self._attr_native_value = None
        self._attr_extra_state_attributes = {}
        self.apply_config(cfg)

    def apply_config(self, cfg: dict) -> None:
        self._cfg = cfg
        self._attr_name = cfg.get("name") or cfg.get("stat_id") or "日积月累"
        self._attr_icon = cfg.get("icon") or "mdi:calendar-clock"
        metric = cfg.get("state_metric") or "count"
        mode = cfg.get("usage_mode") or "none"
        if metric == "duration":
            self._attr_native_unit_of_measurement = "min"
        elif metric == "usage":
            unit = cfg.get("usage_unit") or ("kWh" if mode == "power" else "")
            self._attr_native_unit_of_measurement = unit or None
        else:
            self._attr_native_unit_of_measurement = None

    def refresh(self, value, attrs: dict) -> None:
        """脏检查后写状态：值与属性都没变就直接返回，不产生 state_changed。"""
        if value == self._last_value and attrs == self._last_attrs:
            return
        self._last_value = value
        self._last_attrs = attrs
        self._attr_native_value = value
        self._attr_extra_state_attributes = attrs
        if self.hass is not None:
            self.async_write_ha_state()


# =========================================================================== #
#  管理器                                                                      #
# =========================================================================== #
class DailyStatsManager:
    """采集配置、状态监听、日值缓存与实体注册。"""

    def __init__(self, hass: HomeAssistant, db_path: str) -> None:
        self._hass = hass
        self._db_path = db_path
        self._cfgs: dict[str, dict] = {}
        self._by_entity: dict[str, list[str]] = {}
        self._sensors: dict[str, DailyStatSensor] = {}
        self._days: dict[str, dict[str, dict]] = {}
        self._kind: dict[str, str | None] = {}
        self._open: dict[str, bool] = {}
        self._unsub_bus = None
        self._unsub_midnight = None

    # ---------- 设备 ---------- #
    @staticmethod
    def device_info() -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, DAILY_STATS_DEVICE_IDENT)},
            name=DAILY_STATS_DEVICE_NAME,
            manufacturer=DAILY_STATS_MANUFACTURER,
            model="每日使用统计",
        )

    # ---------- 配置同步 ---------- #
    def load_configs(self) -> dict[str, dict]:
        return {c["stat_id"]: c for c in list_configs_sync(self._db_path)}

    def _reindex(self) -> None:
        by_entity: dict[str, list[str]] = {}
        for sid, cfg in self._cfgs.items():
            eid = cfg.get("entity_id") or ""
            if eid:
                by_entity.setdefault(eid, []).append(sid)
        self._by_entity = by_entity

    def _current_kind(self, cfg: dict) -> str | None:
        eid = cfg.get("entity_id") or ""
        st: State | None = self._hass.states.get(eid) if eid else None
        if st is None:
            return None
        return classify_state(eid, st.state)

    # ---------- 聚合缓存 ---------- #
    def _load_day(self, stat_id: str, date: str) -> dict:
        rows = load_day_rows_sync(self._db_path, stat_id, date)
        return _build_day(date, rows, _now(self._hass))

    def _load_days(self, stat_id: str) -> dict[str, dict]:
        out: dict[str, dict] = {}
        now = _now(self._hass)
        for date, rows in load_all_day_rows_sync(self._db_path, stat_id).items():
            out[date] = _build_day(date, rows, now)
        return out

    def _refresh_day(self, stat_id: str, date: str) -> None:
        if not date:
            return
        self._days.setdefault(stat_id, {})[date] = self._load_day(stat_id, date)

    # ---------- 实体注册/移除 ---------- #
    def reload_sync(self) -> None:
        """按配置表同步实体（新建/更新/删除），可在事件循环中调用。"""
        cfgs = self.load_configs()
        # 删除已不存在的配置 / 已禁用的配置
        for sid in list(self._sensors):
            cfg = cfgs.get(sid)
            if cfg is None or not cfg.get("enabled"):
                self._remove_entity(sid)
        self._cfgs = cfgs
        self._reindex()

        add_cb = self._hass.data.get(DOMAIN, {}).get("async_add_sensor")
        new_entities: list[DailyStatSensor] = []
        new_sids: list[str] = []
        for sid, cfg in cfgs.items():
            if not cfg.get("enabled"):
                continue
            ent = self._sensors.get(sid)
            if ent is None:
                if add_cb is None:
                    _LOGGER.warning(
                        "[daily_stats] sensor 平台未就绪，%s 待下次同步重建", sid
                    )
                    continue
                ent = DailyStatSensor(self, cfg)
                self._sensors[sid] = ent
                new_entities.append(ent)
                new_sids.append(sid)
            else:
                ent.apply_config(cfg)

        if new_entities and add_cb is not None:
            add_cb(new_entities)

        # 缓存与会话状态（只补缺，不重复扫全表）
        for sid, cfg in cfgs.items():
            if not cfg.get("enabled"):
                continue
            if sid not in self._days:
                self._days[sid] = self._load_days(sid)
            sess = get_open_session_sync(self._db_path, sid)
            self._open[sid] = sess is not None
            if sid not in self._kind:
                self._kind[sid] = self._current_kind(cfg)

        # 新建/重新启用时，若实体当前正处于开启状态，立刻补一段会话
        # （否则要等下一次「关→开」才开始统计）。
        for sid in new_sids:
            cfg = cfgs.get(sid) or {}
            if self._kind.get(sid) != "on":
                continue
            if get_open_session_sync(self._db_path, sid) is not None:
                continue
            try:
                self._open_session(cfg, self._current_when(cfg), restored=1)
                self.push(sid)
            except Exception:
                _LOGGER.exception("[daily_stats] 新配置补开启会话失败 %s", sid)

    def preload_days(self) -> None:
        """在 executor 中预载全部日缓存（daylist 不裁剪，历史可能很多行）。"""
        for sid, cfg in self.load_configs().items():
            if not cfg.get("enabled"):
                continue
            if sid not in self._days:
                self._days[sid] = self._load_days(sid)

    def _current_when(self, cfg: dict) -> str:
        """取源实体当前状态变化的本地时间（拿不到就用现在）。"""
        eid = cfg.get("entity_id") or ""
        st = self._hass.states.get(eid) if eid else None
        return (_utc_to_local_str(self._hass, st.last_changed)
                if st is not None else _now_str(self._hass))

    def _remove_entity(self, stat_id: str) -> None:
        ent = self._sensors.pop(stat_id, None)
        if ent is not None:
            eid = ent.entity_id
            try:
                reg = er.async_get(self._hass)
                if reg.async_get(eid) is not None:
                    reg.async_remove(eid)
            except Exception:
                _LOGGER.debug("[daily_stats] 移除实体注册失败 %s", eid, exc_info=True)
            try:
                self._hass.states.async_remove(eid)
            except Exception:
                pass
            try:
                ent.async_remove(force_remove=True)
            except Exception:
                pass
        self._days.pop(stat_id, None)
        self._kind.pop(stat_id, None)
        self._open.pop(stat_id, None)

    # ---------- 使用量 ---------- #
    def _read_reading(self, cfg: dict):
        """读累计读数实体当前值；非数值/unavailable 返回 None。"""
        eid = cfg.get("usage_entity_id") or ""
        if not eid:
            return None
        st = self._hass.states.get(eid)
        if st is None:
            return None
        try:
            return float(str(st.state).strip())
        except Exception:
            return None

    @staticmethod
    def _compute_usage(cfg: dict, on_reading, off_reading, duration: float,
                       trusted: bool = True) -> tuple[float | None, int]:
        mode = cfg.get("usage_mode") or "none"
        if mode == "power":
            try:
                power = float(cfg.get("usage_power_w") or 0.0)
            except Exception:
                power = 0.0
            if power <= 0:
                return None, 0
            return round(power / 1000.0 * max(0.0, duration) / 3600.0, 3), 1
        if mode == "entity":
            if not trusted:
                return None, 0
            if on_reading is None or off_reading is None:
                return None, 0
            try:
                scale = float(cfg.get("usage_scale") or 1.0)
            except Exception:
                scale = 1.0
            delta = (float(off_reading) - float(on_reading)) * scale
            if delta < 0:
                delta = 0.0
            return round(delta, 3), 1
        return None, 0

    # ---------- 会话动作 ---------- #
    def _open_session(self, cfg: dict, when: str, restored: int = 0) -> None:
        sid = cfg["stat_id"]
        if get_open_session_sync(self._db_path, sid) is not None:
            return
        when = str(when or _now_str(self._hass))
        reading = (self._read_reading(cfg)
                   if (cfg.get("usage_mode") == "entity") else None)
        insert_session_sync(self._db_path, sid, when[:10], when, reading, restored)
        self._open[sid] = True
        self._refresh_day(sid, when[:10])

    def _close_open_session(self, cfg: dict, when: str, trusted: bool = True,
                            restored: int = 0) -> bool:
        sid = cfg["stat_id"]
        sess = get_open_session_sync(self._db_path, sid)
        if sess is None:
            self._open[sid] = False
            return False
        et = _parse_dt(when) or _now(self._hass)
        st = _parse_dt(sess.get("on_time"))
        if st is not None and et < st:
            et = st
        duration = max(0.0, (et - st).total_seconds()) if st is not None else 0.0
        off_reading = None
        if cfg.get("usage_mode") == "entity" and trusted:
            off_reading = self._read_reading(cfg)
        usage, known = self._compute_usage(
            cfg, sess.get("on_reading"), off_reading, duration, trusted
        )
        close_session_sync(
            self._db_path, int(sess["id"]),
            et.strftime("%Y-%m-%d %H:%M:%S"), duration, usage, known,
            off_reading, restored,
        )
        self._open[sid] = False
        self._refresh_day(sid, sess.get("date") or et.strftime("%Y-%m-%d"))
        return True

    def _split_open_sessions(self, trusted: bool = False) -> None:
        """把跨过午夜仍未关闭的会话按天拆段（沿日期一直补到今天）。"""
        today = _today_str(self._hass)
        for sid, cfg in list(self._cfgs.items()):
            if not cfg.get("enabled"):
                continue
            for _ in range(4000):
                sess = get_open_session_sync(self._db_path, sid)
                if sess is None:
                    self._open[sid] = False
                    break
                date = sess.get("date") or _hhmmss_date(sess.get("on_time"))
                if date >= today:
                    self._open[sid] = True
                    break
                off_time = f"{date} 23:59:59"
                st = _parse_dt(sess.get("on_time"))
                et = _parse_dt(off_time)
                duration = (max(0.0, (et - st).total_seconds())
                            if st is not None and et is not None else 0.0)
                off_reading = None
                if cfg.get("usage_mode") == "entity" and trusted:
                    off_reading = self._read_reading(cfg)
                usage, known = self._compute_usage(
                    cfg, sess.get("on_reading"), off_reading, duration, trusted
                )
                close_session_sync(
                    self._db_path, int(sess["id"]), off_time, duration,
                    usage, known, off_reading, 1,
                )
                nxt = (datetime.strptime(date, "%Y-%m-%d")
                       + timedelta(days=1)).strftime("%Y-%m-%d")
                carry = off_reading if off_reading is not None else sess.get("on_reading")
                insert_session_sync(
                    self._db_path, sid, nxt, f"{nxt} 00:00:00", carry, 1
                )
                self._refresh_day(sid, date)
                self._refresh_day(sid, nxt)
                self._open[sid] = True

    # ---------- 状态监听 ---------- #
    @callback
    def _on_state_changed(self, event: Event) -> None:
        eid = event.data.get("entity_id")
        sids = self._by_entity.get(eid)
        if not sids:
            return
        new_state: State | None = event.data.get("new_state")
        old_state: State | None = event.data.get("old_state")
        if new_state is None:
            return
        new_kind = classify_state(eid, new_state.state)
        if new_kind is None:
            return
        when = _utc_to_local_str(self._hass, new_state.last_changed)
        for sid in sids:
            cfg = self._cfgs.get(sid)
            if not cfg or not cfg.get("enabled"):
                continue
            prev = self._kind.get(sid)
            if prev is None and old_state is not None:
                prev = classify_state(eid, old_state.state)
            if prev == new_kind:
                self._kind[sid] = new_kind
                continue
            self._kind[sid] = new_kind
            try:
                if new_kind == "on":
                    self._open_session(cfg, when)
                else:
                    self._close_open_session(cfg, when)
            except Exception:
                _LOGGER.exception("[daily_stats] 处理开关事件失败 %s", sid)
            try:
                self.push(sid)
            except Exception:
                _LOGGER.exception("[daily_stats] 刷新实体失败 %s", sid)

    # ---------- 写状态 ---------- #
    def _metric_value(self, cfg: dict, day: dict, stat_id: str):
        metric = cfg.get("state_metric") or "count"
        mode = cfg.get("usage_mode") or "none"
        if metric == "usage":
            unit = cfg.get("usage_unit") or ("kWh" if mode == "power" else "")
            return round(float(day.get("usage") or 0.0), 3), unit
        if metric == "duration":
            return round(float(day.get("duration") or 0.0) / 60.0, 1), "min"
        if metric == "state":
            return ("开启" if self._open.get(stat_id) else "关闭"), None
        return int(day.get("count") or 0), None

    def push(self, stat_id: str) -> None:
        cfg = self._cfgs.get(stat_id)
        ent = self._sensors.get(stat_id)
        if not cfg or ent is None:
            return
        days = self._days.setdefault(stat_id, {})
        today = _today_str(self._hass)
        if today not in days:
            days[today] = self._load_day(stat_id, today)
        day = days.get(today) or _empty_day(today)
        value, unit = self._metric_value(cfg, day, stat_id)
        attrs = {
            "stat_id": stat_id,
            "source_entity": cfg.get("entity_id") or "",
            "state_metric": cfg.get("state_metric") or "count",
            "usage_mode": cfg.get("usage_mode") or "none",
            "usage_entity": cfg.get("usage_entity_id") or "",
            "unit": unit or "",
            "sessions_total": sum(int(d.get("count") or 0) for d in days.values()),
            "today": day,
            "daylist": [days[k] for k in sorted(days)],
        }
        if cfg.get("opt_monthlist"):
            attrs["monthlist"] = _build_periods(days, "month")
        if cfg.get("opt_yearlist"):
            attrs["yearlist"] = _build_periods(days, "year")
        ent.refresh(value, attrs)

    def push_all(self) -> None:
        for sid in list(self._cfgs):
            try:
                self.push(sid)
            except Exception:
                _LOGGER.exception("[daily_stats] 刷新实体失败 %s", sid)

    # ---------- 每日零点 ---------- #
    @callback
    def _async_midnight(self, now=None) -> None:
        try:
            self._split_open_sessions(trusted=True)
        except Exception:
            _LOGGER.exception("[daily_stats] 跨日拆段失败")
        today = _today_str(self._hass)
        yesterday = (_now(self._hass) - timedelta(days=1)).strftime("%Y-%m-%d")
        for sid, cfg in list(self._cfgs.items()):
            if not cfg.get("enabled"):
                continue
            try:
                self._refresh_day(sid, yesterday)
                self._refresh_day(sid, today)
                self.push(sid)
            except Exception:
                _LOGGER.exception("[daily_stats] 跨日刷新失败 %s", sid)

    # ---------- 启动对账 ---------- #
    def reconcile(self) -> None:
        """启动时对齐：把停机期间漏掉的关闭/开启补齐。"""
        self._split_open_sessions(trusted=False)
        for sid, cfg in list(self._cfgs.items()):
            if not cfg.get("enabled"):
                continue
            try:
                eid = cfg.get("entity_id") or ""
                st = self._hass.states.get(eid) if eid else None
                kind = classify_state(eid, st.state) if st is not None else None
                sess = get_open_session_sync(self._db_path, sid)
                if sess is not None and kind != "on":
                    when = (_utc_to_local_str(self._hass, st.last_changed)
                            if st is not None else _now_str(self._hass))
                    # 极端情况下（停机时并发写入）可能有不止一条未闭合行，全部收尾
                    for _ in range(50):
                        if not self._close_open_session(
                                cfg, when, trusted=True, restored=1):
                            break
                        if get_open_session_sync(self._db_path, sid) is None:
                            break
                elif sess is None and kind == "on":
                    when = (_utc_to_local_str(self._hass, st.last_changed)
                            if st is not None else _now_str(self._hass))
                    self._open_session(cfg, when, restored=1)
                self._kind[sid] = kind
            except Exception:
                _LOGGER.exception("[daily_stats] 启动对账失败 %s", sid)

    # ---------- 生命周期 ---------- #
    async def async_start(self) -> None:
        await self._hass.async_add_executor_job(ensure_tables, self._db_path)
        # 日列表不裁剪，历史行可能很多：预载放 executor，别堵事件循环
        await self._hass.async_add_executor_job(self.preload_days)
        self.reload_sync()
        self.reconcile()
        self._unsub_bus = self._hass.bus.async_listen(
            EVENT_STATE_CHANGED, self._on_state_changed
        )
        self._unsub_midnight = async_track_time_change(
            self._hass, self._async_midnight, hour=0, minute=0, second=0
        )
        self.push_all()
        if self._cfgs:
            _LOGGER.info(
                "[daily_stats] 已启动：%d 条配置，%d 个实体",
                len(self._cfgs), len(self._sensors),
            )

    async def async_stop(self) -> None:
        for attr in ("_unsub_bus", "_unsub_midnight"):
            unsub = getattr(self, attr, None)
            if unsub:
                try:
                    unsub()
                except Exception:
                    pass
                setattr(self, attr, None)

    # ---------- 供 API 读取 ---------- #
    def snapshot_configs(self) -> list[dict]:
        """配置列表 + 今日摘要（给 db_viewer 用）。"""
        out: list[dict] = []
        today = _today_str(self._hass)
        for sid, cfg in self._cfgs.items():
            day = (self._days.get(sid) or {}).get(today) or {}
            item = dict(cfg)
            item["sensor_entity_id"] = f"sensor.{DAILY_STATS_ENTITY_PREFIX}{sid}"
            item["open"] = bool(self._open.get(sid))
            item["today"] = {
                "date": today,
                "count": int(day.get("count") or 0),
                "duration": int(day.get("duration") or 0),
                "usage": round(float(day.get("usage") or 0.0), 3),
                "on": day.get("on") or "",
                "off": day.get("off") or "",
            }
            out.append(item)
        return out

    def values(self, stat_id: str, group: str = "day", days: int = 0) -> list[dict]:
        data = self._days.get(stat_id) or {}
        if group in ("month", "year"):
            return _build_periods(data, group)
        keys = sorted(data)
        if days and int(days) > 0:
            keys = keys[-int(days):]
        return [data[k] for k in keys]


# =========================================================================== #
#  HTTP API                                                                    #
# =========================================================================== #
class _DailyStatsBaseView(HomeAssistantView):
    """日积月累 API 公共基类（鉴权口径与其它模块一致）。"""

    requires_auth = False
    cors_allowed = True

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    async def _exec(self, hass: HomeAssistant, func, *args):
        return await hass.async_add_executor_job(func, *args)

    @staticmethod
    def _check(hass: HomeAssistant, write: bool = False) -> web.Response | None:
        data = hass.data.get(DOMAIN, {})
        if not data.get("api_enabled", True):
            return web.json_response({"success": False, "error": "API 访问未启用"}, status=403)
        if not data.get("db_viewer_enabled", True):
            return web.json_response(
                {"success": False, "error": "数据库浏览器访问未启用"}, status=403)
        if write and not data.get("db_edit_enabled", True):
            return web.json_response(
                {"success": False, "error": "数据库修改未启用"}, status=403)
        return None

    @staticmethod
    def _manager(hass: HomeAssistant):
        return hass.data.get(DOMAIN, {}).get("daily_stats_manager")


class DailyStatsConfigView(_DailyStatsBaseView):
    """日积月累配置 CRUD。

    GET    /api/ha_data_store/daily_stats            → 列表（含今日摘要）
    POST   /api/ha_data_store/daily_stats            → 新建/更新（保存即建实体）
    DELETE /api/ha_data_store/daily_stats?stat_id=&keep_history=1
    """

    url = "/api/ha_data_store/daily_stats"
    name = "api:ha_data_store:daily_stats"

    async def get(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check(hass)):
            return resp
        mgr = self._manager(hass)
        if mgr is not None:
            items = mgr.snapshot_configs()
            return self.json({"success": True, "count": len(items), "items": items})
        try:
            items = await self._exec(
                hass, list_configs_sync, self._db_path, False
            )
        except Exception as exc:
            _LOGGER.exception("[daily_stats] 读取配置失败")
            return self.json({"success": False, "error": str(exc)}, status_code=500)
        return self.json({"success": True, "count": len(items), "items": items})

    async def post(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check(hass, write=True)):
            return resp
        try:
            body = await request.json()
        except Exception:
            return self.json({"success": False, "error": "请求体不是合法 JSON"},
                             status_code=400)
        if not isinstance(body, dict):
            return self.json({"success": False, "error": "请求体必须是对象"},
                             status_code=400)
        try:
            cfg = await self._exec(hass, save_config_sync, self._db_path, body)
        except ValueError as exc:
            return self.json({"success": False, "error": str(exc)}, status_code=400)
        except Exception as exc:
            _LOGGER.exception("[daily_stats] 保存配置失败")
            return self.json({"success": False, "error": str(exc)}, status_code=500)

        auto_unit = ""
        try:
            if cfg.get("usage_mode") == "entity" and not cfg.get("usage_unit"):
                st = hass.states.get(cfg.get("usage_entity_id") or "")
                if st is not None:
                    auto_unit = str(
                        (st.attributes or {}).get("unit_of_measurement") or ""
                    )[:16]
                    if auto_unit:
                        cfg = await self._exec(
                            hass, save_config_sync,
                            self._db_path,
                            dict(cfg, usage_unit=auto_unit),
                        )
        except Exception:
            _LOGGER.debug("[daily_stats] 自动补单位失败", exc_info=True)

        mgr = self._manager(hass)
        if mgr is not None:
            try:
                mgr.reload_sync()
                mgr.push(cfg.get("stat_id") or "")
            except Exception:
                _LOGGER.exception("[daily_stats] 保存后同步实体失败")
        return self.json({"success": True, "item": cfg})

    async def delete(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check(hass, write=True)):
            return resp
        stat_id = (request.query.get("stat_id") or "").strip()
        if not stat_id:
            return self.json({"success": False, "error": "缺少 stat_id"}, status_code=400)
        keep = (request.query.get("keep_history", "1") or "1").strip().lower()
        keep_history = keep not in ("0", "false", "no")
        try:
            ok = await self._exec(
                hass, delete_config_sync, self._db_path, stat_id, keep_history
            )
        except Exception as exc:
            _LOGGER.exception("[daily_stats] 删除配置失败")
            return self.json({"success": False, "error": str(exc)}, status_code=500)
        if not ok:
            return self.json({"success": False, "error": f"配置不存在: {stat_id}"},
                             status_code=404)
        mgr = self._manager(hass)
        if mgr is not None:
            try:
                mgr.reload_sync()
            except Exception:
                _LOGGER.exception("[daily_stats] 删除后同步实体失败")
        return self.json({"success": True, "keep_history": keep_history})


class DailyStatsValuesView(_DailyStatsBaseView):
    """日/月/年列表：GET /api/ha_data_store/daily_stats/values?stat_id=&group=&days="""

    url = "/api/ha_data_store/daily_stats/values"
    name = "api:ha_data_store:daily_stats_values"

    async def get(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check(hass)):
            return resp
        stat_id = (request.query.get("stat_id") or "").strip()
        if not stat_id:
            return self.json({"success": False, "error": "缺少 stat_id"}, status_code=400)
        group = (request.query.get("group") or "day").strip().lower()
        if group not in ("day", "month", "year"):
            return self.json({"success": False, "error": "group 只能是 day/month/year"},
                             status_code=400)
        try:
            days = int(request.query.get("days") or 0)
        except Exception:
            days = 0
        mgr = self._manager(hass)
        if mgr is None:
            return self.json({"success": False, "error": "模块未启动"}, status_code=503)
        items = mgr.values(stat_id, group, days)
        return self.json({"success": True, "group": group, "count": len(items),
                          "items": items})


class DailyStatsSessionsView(_DailyStatsBaseView):
    """会话明细：GET /api/ha_data_store/daily_stats/sessions?stat_id=&date=|start=&end="""

    url = "/api/ha_data_store/daily_stats/sessions"
    name = "api:ha_data_store:daily_stats_sessions"

    async def get(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check(hass)):
            return resp
        stat_id = (request.query.get("stat_id") or "").strip()
        if not stat_id:
            return self.json({"success": False, "error": "缺少 stat_id"}, status_code=400)
        date = (request.query.get("date") or "").strip()
        start = (request.query.get("start") or "").strip()
        end = (request.query.get("end") or "").strip()
        try:
            limit = int(request.query.get("limit") or 500)
        except Exception:
            limit = 500
        try:
            items = await self._exec(
                hass, list_sessions_sync, self._db_path, stat_id, date, start, end, limit
            )
        except Exception as exc:
            _LOGGER.exception("[daily_stats] 读取会话失败")
            return self.json({"success": False, "error": str(exc)}, status_code=500)
        return self.json({"success": True, "count": len(items), "items": items})


class DailyStatsEntitiesView(_DailyStatsBaseView):
    """候选实体：GET /api/ha_data_store/daily_stats/entities?keyword=&domain=&limit="""

    url = "/api/ha_data_store/daily_stats/entities"
    name = "api:ha_data_store:daily_stats_entities"

    async def get(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check(hass)):
            return resp
        keyword = (request.query.get("keyword") or "").strip().lower()
        domain = (request.query.get("domain") or "").strip().lower()
        try:
            limit = int(request.query.get("limit") or 800)
        except Exception:
            limit = 800
        rows: list[dict] = []
        for st in hass.states.async_all():
            eid = st.entity_id
            if eid.startswith(f"sensor.{DAILY_STATS_ENTITY_PREFIX}"):
                continue
            d = eid.split(".", 1)[0]
            if domain and d != domain:
                continue
            name = str((st.attributes or {}).get("friendly_name") or eid)
            if keyword and keyword not in eid.lower() and keyword not in name.lower():
                continue
            rows.append({
                "entity_id": eid,
                "name": name,
                "domain": d,
                "state": st.state,
                "unit": str((st.attributes or {}).get("unit_of_measurement") or ""),
            })
            if len(rows) >= limit:
                break
        rows.sort(key=lambda x: (x["domain"], x["entity_id"]))
        return self.json({"success": True, "count": len(rows), "items": rows})


class DailyStatsActionView(_DailyStatsBaseView):
    """维护动作：POST /api/ha_data_store/daily_stats/action {action, stat_id}

    action: clear_history（清空该配置历史）| refresh（立即重算并刷新实体）
    """

    url = "/api/ha_data_store/daily_stats/action"
    name = "api:ha_data_store:daily_stats_action"

    async def post(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check(hass, write=True)):
            return resp
        try:
            body = await request.json()
        except Exception:
            return self.json({"success": False, "error": "请求体不是合法 JSON"},
                             status_code=400)
        action = str((body or {}).get("action") or "").strip().lower()
        stat_id = str((body or {}).get("stat_id") or "").strip()
        if not stat_id:
            return self.json({"success": False, "error": "缺少 stat_id"}, status_code=400)
        mgr = self._manager(hass)
        try:
            if action == "clear_history":
                removed = await self._exec(
                    hass, clear_history_sync, self._db_path, stat_id
                )
                if mgr is not None:
                    mgr._days[stat_id] = {}  # noqa: SLF001 - 内部缓存同步
                    mgr.push(stat_id)
                return self.json({"success": True, "removed": removed})
            if action == "refresh":
                if mgr is None:
                    return self.json({"success": False, "error": "模块未启动"},
                                     status_code=503)
                mgr._days[stat_id] = await self._exec(  # noqa: SLF001
                    hass, mgr._load_days, stat_id
                )
                mgr.push(stat_id)
                return self.json({"success": True})
        except Exception as exc:
            _LOGGER.exception("[daily_stats] 动作失败 %s", action)
            return self.json({"success": False, "error": str(exc)}, status_code=500)
        return self.json({"success": False, "error": f"未知动作: {action}"},
                         status_code=400)


def register_api_views(hass: HomeAssistant, db_path: str) -> None:
    """注册日积月累 API View（由 __init__ 调用）。"""
    for view_cls in (
        DailyStatsConfigView,
        DailyStatsValuesView,
        DailyStatsSessionsView,
        DailyStatsEntitiesView,
        DailyStatsActionView,
    ):
        hass.http.register_view(view_cls(db_path))


# =========================================================================== #
#  启动入口                                                                     #
# =========================================================================== #
async def async_setup_daily_stats(hass: HomeAssistant, db_path: str) -> "DailyStatsManager":
    """由 __init__.async_setup_entry 在平台就绪后调用（延迟后台启动）。"""
    mgr = DailyStatsManager(hass, db_path)
    hass.data.setdefault(DOMAIN, {})["daily_stats_manager"] = mgr
    await mgr.async_start()
    return mgr
