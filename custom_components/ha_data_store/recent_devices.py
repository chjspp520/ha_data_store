"""近期使用设备：device_history 全量设备的「最近使用」计算 + 相关设置读写。

用途：
  · 传感器「近期使用设备」的 `all` 节点（数据源 device_history，覆盖自动化触发等
    非前端卡片操作，与 `devices` 节点互补）
  · API `GET /query?type=device_last_used`（支持时间段/指定时间/实体过滤/排除项）

口径（每条规则与 daily_summary 的设备明细保持一致）：
  · 每个 `entity_id` 取窗口内**最新一条**记录（`on_time` 最大，其次 `id` 最大）
  · 最新记录 `on_time` 有值且 `off_time` 为空 → `running=true`，`last_used_text` = 当前时刻
  · 否则 `last_used_text` = 该记录的 `off_time`
  · `count` / `duration`(秒) / `energy`(kWh) 为窗口内累计；运行中记录时长按
    「当前时间 − on_time」计、用电按 `now_kwh − on_power`（无来源记 0）

窗口与过滤：
  · 默认窗口天数读 `number.ha_data_store_recent_days`（缺失/非法回退 30 天）
  · 窗口过滤作用于 `on_time`：`on_time >= 今天-(N-1) 00:00:00`（含今天的 N 个自然日）
  · 显式传入 start/end/date/month/year 时优先按它们过滤（优先级与其它接口一致）
  · 排除项来自 `api_settings.recent_exclude_entities`（JSON 数组），可由 db_viewer 配置
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta

from .const import (
    DEFAULT_TIMEZONE,
    RECENT_DAYS_ENTITY_ID,
    RECENT_EXCLUDE_SETTING_KEY,
    RECENT_WINDOW_DAYS_DEFAULT,
    RECENT_WINDOW_DAYS_MAX,
    RECENT_WINDOW_DAYS_MIN,
    TABLE_API_SETTINGS,
    TABLE_DEVICE_HISTORY,
)

_LOGGER = logging.getLogger(__name__)

_EPOCH = datetime(1970, 1, 1)


def _local_now() -> datetime:
    """当前东八区本地时间。"""
    return datetime.utcnow() + timedelta(hours=DEFAULT_TIMEZONE)


def _to_ms(text: str) -> int:
    """把本地时间字符串（yyyy-mm-dd HH:MM:SS）转为毫秒时间戳；非法返回 0。

    不依赖主机本地时区：按东八区墙钟时间换算成 UTC 时间戳。
    """
    if not text:
        return 0
    try:
        dt = datetime.strptime(str(text)[:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return 0
    return int(((dt - timedelta(hours=DEFAULT_TIMEZONE)) - _EPOCH).total_seconds() * 1000)


def get_window_days(hass=None) -> int:
    """读取窗口天数设置（`number.ha_data_store_recent_days`）。

    实体缺失 / 状态非法 / 超出 1~365 → 一律回退默认 30。
    """
    if hass is None:
        return RECENT_WINDOW_DAYS_DEFAULT
    try:
        state = hass.states.get(RECENT_DAYS_ENTITY_ID)
        raw = getattr(state, "state", "") if state is not None else ""
        days = int(float(str(raw).strip()))
    except Exception:
        return RECENT_WINDOW_DAYS_DEFAULT
    if days < RECENT_WINDOW_DAYS_MIN or days > RECENT_WINDOW_DAYS_MAX:
        return RECENT_WINDOW_DAYS_DEFAULT
    return days


def get_exclude_entities(db_path: str) -> list[str]:
    """读取排除实体列表（api_settings.recent_exclude_entities，JSON 数组）。

    兼容逗号/换行分隔的纯文本写法（便于前端手输）。
    """
    try:
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                f"SELECT svalue FROM {TABLE_API_SETTINGS} WHERE skey = ?",
                (RECENT_EXCLUDE_SETTING_KEY,),
            ).fetchone()
        finally:
            conn.close()
    except Exception:
        return []
    raw = (row[0] if row else "") or ""
    if not raw:
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [str(x).strip() for x in data if str(x).strip()]
    except Exception:
        pass
    return [p.strip() for p in raw.replace("\n", ",").split(",") if p.strip()]


def set_exclude_entities(db_path: str, entity_ids) -> list[str]:
    """保存排除实体列表（去重、去空），返回实际保存的列表。"""
    out: list[str] = []
    seen: set[str] = set()
    for x in entity_ids or []:
        s = str(x or "").strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            f"INSERT OR REPLACE INTO {TABLE_API_SETTINGS} (skey, svalue) VALUES (?, ?)",
            (RECENT_EXCLUDE_SETTING_KEY, json.dumps(out, ensure_ascii=False)),
        )
        conn.commit()
    finally:
        conn.close()
    return out


def _range_conds(entities, start, end, date, month, year, window_days):
    """组装实体/时间过滤条件（优先级 start/end > date > month > year > 窗口天数）。

    返回 (conds, params, label)；窗口天数条件作用于 `on_time`。
    """
    conds: list[str] = []
    params: list = []
    label = ""
    if entities:
        conds.append(f"entity_id IN ({','.join(['?'] * len(entities))})")
        params.extend(entities)
    if start or end:
        if start:
            conds.append("on_time >= ?")
            params.append(f"{start} 00:00:00")
        if end:
            conds.append("on_time <= ?")
            params.append(f"{end} 23:59:59")
        label = f"{start or ''}~{end or ''}"
    elif date:
        conds.append("on_time LIKE ?")
        params.append(f"{date}%")
        label = date
    elif month:
        conds.append("on_time LIKE ?")
        params.append(f"{month}-%")
        label = month
    elif year:
        conds.append("on_time LIKE ?")
        params.append(f"{year}-%")
        label = year
    elif window_days and int(window_days) > 0:
        days = max(1, int(window_days))
        start_day = (_local_now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
        conds.append("on_time >= ?")
        params.append(f"{start_day} 00:00:00")
        label = f"最近 {days} 天"
    return conds, params, label


def _fetch_rows(conn, where: str, params: list):
    """取记录（兼容旧库无 icon 列）。"""
    cols = ("id, entity_id, name, room, icon, on_time, off_time, duration, "
            "energy_consumed, now_kwh, on_power")
    try:
        return conn.execute(
            f"SELECT {cols} FROM {TABLE_DEVICE_HISTORY} {where} "
            f"ORDER BY on_time ASC, id ASC",
            params,
        ).fetchall()
    except sqlite3.OperationalError:
        # 旧库可能尚未补 icon 列
        cols = cols.replace("room, icon,", "room, '' AS icon,")
        return conn.execute(
            f"SELECT {cols} FROM {TABLE_DEVICE_HISTORY} {where} "
            f"ORDER BY on_time ASC, id ASC",
            params,
        ).fetchall()


def compute_device_last_used_sync(
    db_path: str,
    window_days: int | None = None,
    start: str = "",
    end: str = "",
    date: str = "",
    month: str = "",
    year: str = "",
    entities=None,
    exclude=None,
    use_exclude: bool = True,
    running_only: bool = False,
    limit: int = 0,
    offset: int = 0,
    detail: bool = True,
) -> dict:
    """计算每个实体的「最近使用」情况。

    参数：
      window_days  → 窗口天数：None/缺省 = 用默认 30；**0 = 不限窗口**（全部历史）；
                     1~365 正常；非法/超范围回退 30。仅在未传时间过滤时生效。
      start/end/date/month/year → 时间过滤（作用于 on_time，优先级同其它接口）
      entities     → 只统计这些 entity_id（None/空 = 全部）
      exclude      → 排除的 entity_id；None = 读取 api_settings 中保存的排除项
      use_exclude  → 是否启用「排除项过滤」（False = 忽略保存的排除项与 exclude，等效关闭过滤）
      running_only → 只返回正在运行的设备
      limit/offset → 分页（0 = 不限）
      detail       → 是否返回 items 明细（False 只返回 entities 列表 + 元信息）

    返回：{range, window_days, use_exclude, exclude_count, total, count, entities[], items[]}
    """
    # 窗口天数：区分「未传（None → 默认 30）」与「0（不限窗口）」
    if window_days is not None and int(window_days) == 0:
        days = 0
    else:
        days = int(window_days) if window_days else RECENT_WINDOW_DAYS_DEFAULT
        if days < RECENT_WINDOW_DAYS_MIN or days > RECENT_WINDOW_DAYS_MAX:
            days = RECENT_WINDOW_DAYS_DEFAULT

    # 排除项：use_exclude=False 时完全不过滤；否则显式传入优先，未传则读保存的设置
    if not use_exclude:
        ex: set[str] = set()
    elif exclude is None:
        ex = set(get_exclude_entities(db_path))
    else:
        ex = {str(x).strip() for x in exclude if str(x).strip()}

    conds, params, label = _range_conds(entities, start, end, date, month, year, days)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = _fetch_rows(conn, where, params)
    finally:
        conn.close()

    now_dt = _local_now()
    now_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")
    groups: dict[str, dict] = {}

    for r in rows:
        eid = (r["entity_id"] or "").strip()
        if not eid or eid in ex:
            continue
        g = groups.get(eid)
        if g is None:
            g = {
                "entity_id": eid, "name": r["name"] or eid, "room": r["room"] or "",
                "icon": r["icon"] or "", "count": 0, "duration": 0.0, "energy": 0.0,
                "last_id": -1, "last_on_time": "", "last_off_time": "",
            }
            groups[eid] = g
        g["count"] += 1

        # 时长（秒）：已关闭用 duration；运行中按 当前时间 − on_time
        dur = r["duration"]
        if dur is None and r["on_time"]:
            if not (r["off_time"] or ""):
                try:
                    on_dt = datetime.strptime(str(r["on_time"])[:19], "%Y-%m-%d %H:%M:%S")
                    dur = max(0.0, (now_dt - on_dt).total_seconds())
                except Exception:
                    dur = 0.0
        g["duration"] += float(dur or 0)

        # 用电（kWh）：已关闭用 energy_consumed；运行中用 now_kwh − on_power
        eng = r["energy_consumed"]
        if eng is None and r["now_kwh"] is not None and r["on_power"] is not None:
            eng = float(r["now_kwh"]) - float(r["on_power"])
        g["energy"] += float(eng or 0)

        # 最新一条：on_time 最大（并列取 id 最大）
        key = (str(r["on_time"] or ""), int(r["id"] or 0))
        if key >= (g["last_on_time"], g["last_id"]):
            g["last_id"] = int(r["id"] or 0)
            g["last_on_time"] = str(r["on_time"] or "")
            g["last_off_time"] = str(r["off_time"] or "")
            if r["name"]:
                g["name"] = r["name"]
            if r["room"]:
                g["room"] = r["room"]
            if r["icon"]:
                g["icon"] = r["icon"]

    items: list[dict] = []
    for g in groups.values():
        running = bool(g["last_on_time"]) and not g["last_off_time"]
        last_text = now_str if running else g["last_off_time"]
        items.append({
            "entity_id": g["entity_id"],
            "name": g["name"],
            "room": g["room"],
            "icon": g["icon"],
            "running": running,
            "on_time": g["last_on_time"],
            "off_time": g["last_off_time"],
            "last_used": _to_ms(last_text),
            "last_used_text": last_text,
            "count": g["count"],
            "duration": round(g["duration"], 0),
            "duration_hour": round(g["duration"] / 3600.0, 2),
            "energy": round(g["energy"], 3),
        })

    if running_only:
        items = [i for i in items if i["running"]]
    items.sort(key=lambda i: (i["last_used"] or 0, i["entity_id"]), reverse=True)

    total = len(items)
    if limit and int(limit) > 0:
        off = max(0, int(offset or 0))
        items = items[off: off + int(limit)]

    out = {
        "range": label or "全部时间",
        "window_days": days,
        "use_exclude": bool(use_exclude),
        "exclude_count": len(ex),
        "total": total,
        "count": len(items),
        "entities": [i["entity_id"] for i in items],
    }
    if detail:
        out["items"] = items
    return out
