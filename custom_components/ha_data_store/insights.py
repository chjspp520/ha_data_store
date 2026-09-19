"""家庭洞察：统一事件流（timeline）与房间占用排行（room_occupancy）。

设计约束（与集成整体一致：**只提供数据，前端负责 UI**）：
  · 不新增业务表，全部由现有表计算得出；
  · 时间单位以「天/日期/时间段/月/年」为界（`device_history` 已在午夜自动拆分，
    因此不存在跨天记录），**不做分页**，返回 `count` + `truncated`；
  · 过滤维度由调用方自选（来源 / 事件类型 / 实体 / 房间 / 用户 / 关键词）。

统一事件流 `build_timeline_sync`
  设备记录按 A 方案**展开为 on / off 两个事件**（运行中只有 on，`off_time` 为空）；
  合并来源：device / user_action / automation / vacuum / xiaoai / health / printer，
  统一字段后按时间倒序输出，前端一套渲染逻辑即可展示全部来源。

房间占用 `compute_room_occupancy_sync`
  数据源：`device_history` 中 `name = '人在'` 的记录（与今日家庭状态同一约定）；
  **严谨口径**：同一房间的重叠区间先做**区间并集**再累计时长（多个"人在"实体、
  抖动重复上报都不会重复计时）；另附**门户事件**（`name = '入户门'`）与
  日/小时分解（`bucket=day|hour`）。

时间窗参数（两个接口共用，优先级 date > start/end > month > year > 默认今日）：
  `date=YYYY-MM-DD` / `start`+`end` / `month=YYYY-MM` / `year=YYYY` / `today=1`。
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta

from .const import (
    DEFAULT_TIMEZONE,
    TABLE_AUTOMATION_LOGS,
    TABLE_DEVICE_HISTORY,
    TABLE_ENTITY_CONFIGS,
    TABLE_HEALTH_RECORDS,
    TABLE_USER_ACTIONS,
    TABLE_VACUUM_HISTORY,
)
from .daily_summary import NAME_DOOR, NAME_PRESENCE

_LOGGER = logging.getLogger(__name__)

# 小爱 / 打印机表名常量定义在各自模块内（项目约定：不入 const.py），此处按同一约定本地声明
TABLE_XIAOAI_CONVERSATIONS = "xiaoai_conversations"
TABLE_PRINTER_DAILY = "printer_daily"

_EPOCH = datetime(1970, 1, 1)

# 全部可用来源（缺表时该来源自动跳过）
ALL_SOURCES = ("device", "user_action", "automation", "vacuum", "xiaoai", "health", "printer")

# 设备事件类型（timeline 的 events 过滤项）
DEVICE_EVENTS = ("on", "off")

MAX_TIMELINE_LIMIT = 5000
DEFAULT_TIMELINE_LIMIT = 500


# =========================================================================== #
#  公共工具                                                                      #
# =========================================================================== #
def _local_now() -> datetime:
    """当前东八区本地时间。"""
    return datetime.utcnow() + timedelta(hours=DEFAULT_TIMEZONE)


def _now_str() -> str:
    return _local_now().strftime("%Y-%m-%d %H:%M:%S")


def to_ms(text: str) -> int:
    """本地时间字符串（yyyy-mm-dd HH:MM:SS）→ 毫秒时间戳；不依赖主机时区。"""
    if not text:
        return 0
    try:
        dt = datetime.strptime(str(text)[:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        try:
            dt = datetime.strptime(str(text)[:10], "%Y-%m-%d")
        except Exception:
            return 0
    return int(((dt - timedelta(hours=DEFAULT_TIMEZONE)) - _EPOCH).total_seconds() * 1000)


def _parse_dt(text: str) -> datetime | None:
    try:
        return datetime.strptime(str(text)[:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _csv(raw) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        items = raw
    else:
        items = str(raw).replace("，", ",").split(",")
    out: list[str] = []
    for x in items:
        s = str(x or "").strip()
        if s and s not in out:
            out.append(s)
    return out


def resolve_window(date: str = "", start: str = "", end: str = "",
                   month: str = "", year: str = "", today: bool = False) -> dict:
    """解析时间窗参数 → {"label", "like", "between", "kind"}。

    优先级：date > start/end > month > year > 默认今日。
      like    → SQL `col LIKE ?`（如 "2026-09-19%"）
      between → SQL `col >= ? AND col <= ?`（含首尾整日）
    """
    date = (date or "").strip()
    start = (start or "").strip()
    end = (end or "").strip()
    month = (month or "").strip()
    year = (year or "").strip()

    if not date and not (start or end) and not month and not year:
        date = _local_now().strftime("%Y-%m-%d")

    if date:
        return {"label": date, "like": f"{date}%", "between": None, "kind": "date", "day": date}
    if start or end:
        lo = f"{start} 00:00:00" if start else "0000-00-00 00:00:00"
        hi = f"{end} 23:59:59" if end else "9999-99-99 23:59:59"
        return {"label": f"{start or ''}~{end or ''}",
                "like": None, "between": (lo, hi), "kind": "range", "day": ""}
    if month:
        return {"label": month, "like": f"{month}-%", "between": None,
                "kind": "month", "day": ""}
    return {"label": year, "like": f"{year}-%", "between": None, "kind": "year", "day": ""}


def _time_cond(window: dict, col: str) -> tuple[str, list]:
    """把时间窗转成 (SQL 条件, 参数)。"""
    if window.get("between"):
        lo, hi = window["between"]
        return f"{col} >= ? AND {col} <= ?", [lo, hi]
    return f"{col} LIKE ?", [window["like"]]


def _in_cond(col: str, values: list[str]) -> tuple[str, list]:
    return f"{col} IN ({','.join(['?'] * len(values))})", list(values)


# =========================================================================== #
#  统一事件流                                                                    #
# =========================================================================== #
def _ev_device(conn, window, opts) -> list[dict]:
    cond, params = _time_cond(window, "on_time")
    conds, params = [cond], list(params)
    if opts["entities"]:
        c, p = _in_cond("entity_id", opts["entities"])
        conds.append(c); params.extend(p)
    if opts["rooms"]:
        c, p = _in_cond("room", opts["rooms"])
        conds.append(c); params.extend(p)
    if opts["users"]:
        c1, p1 = _in_cond("on_user", opts["users"])
        c2, p2 = _in_cond("off_user", opts["users"])
        conds.append(f"({c1} OR {c2})")
        params.extend(p1); params.extend(p2)
    if opts["keyword"]:
        conds.append("name LIKE ?")
        params.append(f"%{opts['keyword']}%")
    rows = conn.execute(
        f"SELECT entity_id, name, room, icon, on_time, off_time, duration, "
        f"energy_consumed, on_user, off_user FROM {TABLE_DEVICE_HISTORY} "
        f"WHERE {' AND '.join(conds)} ORDER BY on_time DESC",
        params,
    ).fetchall()

    now_dt = _local_now()
    out: list[dict] = []
    want = opts["events"]  # ("on","off") 子集
    for r in rows:
        name = r["name"] or r["entity_id"]
        running = not (r["off_time"] or "")
        # 时长：已关闭用 duration（缺失时用 off_time − on_time 兜底）；运行中按 当前时间 − on_time
        dur = r["duration"]
        if dur is None and r["on_time"]:
            on_dt = _parse_dt(r["on_time"])
            if running:
                dur = max(0.0, (now_dt - on_dt).total_seconds()) if on_dt else 0.0
            else:
                off_dt = _parse_dt(r["off_time"])
                dur = max(0.0, (off_dt - on_dt).total_seconds()) if (on_dt and off_dt) else 0.0
        dur = float(dur or 0)
        eng = r["energy_consumed"]
        try:
            eng_v = float(eng) if eng is not None and eng != "" else None
        except (TypeError, ValueError):
            eng_v = None
        # 注：运行中记录的用电需 now_kwh − on_power（依赖功率来源配置），timeline 为保持轻量不计算；
        #     需要精确用电请用 device_usage_detail / device_last_used 接口。

        if "on" in want and r["on_time"]:
            detail = f"已运行 {dur / 3600:.1f} 小时" if running else f"运行 {dur / 3600:.1f} 小时"
            out.append({
                "ts": r["on_time"], "source": "device", "event": "on",
                "title": f"{name} 开启", "entity_id": r["entity_id"], "name": name,
                "room": r["room"] or "", "icon": r["icon"] or "",
                "user": r["on_user"] or "", "detail": detail,
                "extra": {"duration": round(dur, 0), "duration_hour": round(dur / 3600, 2),
                          "energy": round(eng_v, 3) if eng_v is not None else None,
                          "running": running},
            })
        if "off" in want and not running and r["off_time"]:
            bits = [f"运行 {dur / 3600:.1f} 小时"]
            if eng_v is not None:
                bits.append(f"{eng_v:.3f} kWh")
            out.append({
                "ts": r["off_time"], "source": "device", "event": "off",
                "title": f"{name} 关闭", "entity_id": r["entity_id"], "name": name,
                "room": r["room"] or "", "icon": r["icon"] or "",
                "user": r["off_user"] or "", "detail": " · ".join(bits),
                "extra": {"duration": round(dur, 0), "duration_hour": round(dur / 3600, 2),
                          "energy": round(eng_v, 3) if eng_v is not None else None,
                          "running": False},
            })
    return out


def _ev_user_action(conn, window, opts) -> list[dict]:
    cond, params = _time_cond(window, "ts_text")
    conds, params = [cond], list(params)
    if opts["entities"]:
        c, p = _in_cond("entity_id", opts["entities"])
        conds.append(c); params.extend(p)
    if opts["rooms"]:
        c, p = _in_cond("room_name", opts["rooms"])
        conds.append(c); params.extend(p)
    if opts["users"]:
        c, p = _in_cond("user_name", opts["users"])
        conds.append(c); params.extend(p)
    if opts["keyword"]:
        conds.append("(name LIKE ? OR action LIKE ?)")
        params.extend([f"%{opts['keyword']}%", f"%{opts['keyword']}%"])
    rows = conn.execute(
        f"SELECT entity_id, name, icon, room_name, user_name, action, state_log, "
        f"ts_text, device_type, action_snapshot FROM {TABLE_USER_ACTIONS} "
        f"WHERE {' AND '.join(conds)} ORDER BY ts_text DESC",
        params,
    ).fetchall()
    out = []
    for r in rows:
        name = r["name"] or r["entity_id"]
        user = r["user_name"] or ""
        action = r["action"] or ""
        out.append({
            "ts": r["ts_text"], "source": "user_action", "event": "action",
            "title": f"{user} 操作 {name}（{action}）".strip(),
            "entity_id": r["entity_id"] or "", "name": name,
            "room": r["room_name"] or "", "icon": r["icon"] or "",
            "user": user, "detail": r["state_log"] or "",
            "extra": {"action": action, "device_type": r["device_type"] or "",
                      "action_snapshot": r["action_snapshot"] or ""},
        })
    return out


def _ev_automation(conn, window, opts) -> list[dict]:
    cond, params = _time_cond(window, "COALESCE(NULLIF(trigger_time,''), created_at)")
    conds, params = [cond], list(params)
    if opts["keyword"]:
        conds.append("(automation_name LIKE ? OR trigger_desc LIKE ?)")
        params.extend([f"%{opts['keyword']}%", f"%{opts['keyword']}%"])
    rows = conn.execute(
        f"SELECT automation_name, trigger_type, trigger_desc, trigger_time, status, "
        f"duration_ms, created_at FROM {TABLE_AUTOMATION_LOGS} "
        f"WHERE {' AND '.join(conds)} "
        f"ORDER BY COALESCE(NULLIF(trigger_time,''), created_at) DESC",
        params,
    ).fetchall()
    status_text = {"success": "执行成功", "failed": "执行失败", "skipped": "条件跳过",
                   "partial": "部分成功"}
    out = []
    for r in rows:
        st = (r["status"] or "").lower()
        label = status_text.get(st, st or "已执行")
        ts = (r["trigger_time"] or "").strip() or (r["created_at"] or "")
        bits = []
        if r["trigger_desc"]:
            bits.append(r["trigger_desc"])
        if r["duration_ms"]:
            bits.append(f"{float(r['duration_ms']) / 1000:.1f}s")
        out.append({
            "ts": ts, "source": "automation", "event": st or "run",
            "title": f"自动化 {r['automation_name'] or '-'} {label}",
            "entity_id": "", "name": r["automation_name"] or "", "room": "", "icon": "",
            "user": "", "detail": " · ".join(bits),
            "extra": {"trigger_type": r["trigger_type"] or "", "status": st,
                      "duration_ms": r["duration_ms"] or 0},
        })
    return out


def _ev_vacuum(conn, window, opts) -> list[dict]:
    cond, params = _time_cond(window, "datetime")
    conds, params = [cond], list(params)
    if opts["entities"]:
        c, p = _in_cond("entity_id", opts["entities"])
        conds.append(c); params.extend(p)
    if opts["keyword"]:
        conds.append("(vacuum_id LIKE ? OR entity_id LIKE ?)")
        params.extend([f"%{opts['keyword']}%"] * 2)
    rows = conn.execute(
        f"SELECT entity_id, vacuum_id, datetime, state, seq FROM {TABLE_VACUUM_HISTORY} "
        f"WHERE {' AND '.join(conds)} ORDER BY datetime ASC",
        params,
    ).fetchall()
    # 轨迹点是连续采样：只在 state 变化时产出一条事件，避免一天几千条
    state_text = {"cleaning": "开始清扫", "returning": "返回充电", "docked": "回充完成",
                  "paused": "暂停清扫", "idle": "待机", "error": "异常"}
    out = []
    last_state = None
    points = 0
    for r in rows:
        points += 1
        st = (r["state"] or "").strip()
        if st == last_state:
            continue
        last_state = st
        out.append({
            "ts": r["datetime"], "source": "vacuum", "event": st or "state",
            "title": f"扫地机 {state_text.get(st, st or '-')}",
            "entity_id": r["entity_id"] or "", "name": r["vacuum_id"] or r["entity_id"] or "",
            "room": "", "icon": "", "user": "", "detail": f"轨迹点 {points}",
            "extra": {"state": st, "vacuum_id": r["vacuum_id"] or ""},
        })
    return out


def _ev_xiaoai(conn, window, opts) -> list[dict]:
    cond, params = _time_cond(window, "conv_time")
    conds, params = [cond], list(params)
    if opts["entities"]:
        c, p = _in_cond("entity_id", opts["entities"])
        conds.append(c); params.extend(p)
    if opts["keyword"]:
        conds.append("(user_text LIKE ? OR ai_text LIKE ?)")
        params.extend([f"%{opts['keyword']}%", f"%{opts['keyword']}%"])
    rows = conn.execute(
        f"SELECT entity_id, user_text, ai_text, conv_time, type FROM {TABLE_XIAOAI_CONVERSATIONS} "
        f"WHERE {' AND '.join(conds)} ORDER BY conv_time DESC",
        params,
    ).fetchall()
    out = []
    for r in rows:
        q = (r["user_text"] or "").strip()
        a = (r["ai_text"] or "").strip()
        out.append({
            "ts": r["conv_time"], "source": "xiaoai", "event": "conversation",
            "title": f"小爱：{q[:40]}" if q else "小爱对话",
            "entity_id": r["entity_id"] or "", "name": "小爱音箱", "room": "", "icon": "",
            "user": "", "detail": a[:80],
            "extra": {"user_text": q, "ai_text": a, "type": r["type"] or ""},
        })
    return out


def _ev_health(conn, window, opts) -> list[dict]:
    cond, params = _time_cond(window, "date_time")
    conds, params = [cond], list(params)
    if opts["keyword"]:
        conds.append("(name LIKE ? OR type LIKE ? OR remark LIKE ?)")
        params.extend([f"%{opts['keyword']}%"] * 3)
    rows = conn.execute(
        f"SELECT name, type, date_time, dp, sp, pr, height, weight, bmi, temp, "
        f"remark, description FROM {TABLE_HEALTH_RECORDS} "
        f"WHERE {' AND '.join(conds)} ORDER BY date_time DESC",
        params,
    ).fetchall()
    unit = {"dp": "舒张压", "sp": "收缩压", "pr": "心率", "temp": "体温",
            "weight": "体重", "height": "身高", "bmi": "BMI"}
    out = []
    for r in rows:
        bits = []
        for k, label in unit.items():
            v = r[k]
            if v is not None and v != "":
                bits.append(f"{label} {v}")
        nm = r["name"] or "健康记录"
        tp = r["type"] or ""
        title = f"{nm} 记录" + (f"（{tp}）" if tp else "")
        detail = " · ".join(bits) or (r["remark"] or r["description"] or "")
        out.append({
            "ts": r["date_time"], "source": "health", "event": "record",
            "title": title, "entity_id": "", "name": nm, "room": "", "icon": "",
            "user": "", "detail": detail,
            "extra": {"type": tp, "remark": r["remark"] or "",
                      "description": r["description"] or ""},
        })
    return out


def _ev_printer(conn, window, opts) -> list[dict]:
    # printer_daily 为日粒度：day 是日期，用 day LIKE / BETWEEN 匹配
    if window.get("between"):
        lo, hi = window["between"]
        cond = "day >= ? AND day <= ?"
        params = [lo[:10], hi[:10]]
    else:
        cond = "day LIKE ?"
        params = [window["like"]]
    conds, params = [cond], list(params)
    if opts["keyword"]:
        conds.append("name LIKE ?")
        params.append(f"%{opts['keyword']}%")
    rows = conn.execute(
        f"SELECT name, day, print, scan, copy, fax, jam_printer FROM {TABLE_PRINTER_DAILY} "
        f"WHERE {' AND '.join(conds)} ORDER BY day DESC",
        params,
    ).fetchall()
    out = []
    for r in rows:
        total = sum(int(r[k] or 0) for k in ("print", "scan", "copy", "fax", "jam_printer"))
        if total == 0:
            continue
        bits = []
        for k, label in (("print", "打印"), ("scan", "扫描"), ("copy", "复印"),
                         ("fax", "传真"), ("jam_printer", "卡纸")):
            if r[k]:
                bits.append(f"{label} {r[k]}")
        out.append({
            "ts": f"{r['day']} 00:00:00", "source": "printer", "event": "daily",
            "title": f"打印机 {r['name'] or '-'} 当日作业 {total} 次",
            "entity_id": "", "name": r["name"] or "", "room": "", "icon": "",
            "user": "", "detail": " · ".join(bits),
            "extra": {"day": r["day"], "print": r["print"] or 0, "scan": r["scan"] or 0,
                      "copy": r["copy"] or 0, "fax": r["fax"] or 0,
                      "jam": r["jam_printer"] or 0},
        })
    return out


_TIMELINE_HANDLERS = {
    "device": _ev_device,
    "user_action": _ev_user_action,
    "automation": _ev_automation,
    "vacuum": _ev_vacuum,
    "xiaoai": _ev_xiaoai,
    "health": _ev_health,
    "printer": _ev_printer,
}

# 各来源支持的过滤维度：使用 entities/rooms/users 过滤时，**不具备该维度**的来源会被自动剔除，
# 避免"我只想查某个实体，却混进自动化/健康/打印机事件"这类无法解释的结果；
# 被剔除的来源会出现在返回的 `skipped_sources` 中，前端可提示用户。
_SOURCE_CAPS = {
    "device": {"entities", "rooms", "users", "keyword"},
    "user_action": {"entities", "rooms", "users", "keyword"},
    "vacuum": {"entities", "keyword"},
    "xiaoai": {"entities", "keyword"},
    "automation": {"keyword"},
    "health": {"keyword"},
    "printer": {"keyword"},
}


def build_timeline_sync(
    db_path: str,
    date: str = "",
    start: str = "",
    end: str = "",
    month: str = "",
    year: str = "",
    today: bool = False,
    sources=None,
    events=None,
    entities=None,
    rooms=None,
    users=None,
    keyword: str = "",
    limit: int = DEFAULT_TIMELINE_LIMIT,
    detail: bool = True,
) -> dict:
    """统一事件流：多来源合并、按时间倒序、不分页。

    参数：
      date/start+end/month/year/today → 时间窗（默认今日；device_history 已按午夜拆分，无跨天记录）
      sources  → 逗号分隔来源，可选 device/user_action/automation/vacuum/xiaoai/health/printer（空=全部）
      events   → 仅对 device 有效：on / off（空=两者都要）
      entities → 实体过滤（device/user_action/vacuum/xiaoai）
      rooms    → 房间过滤（device.room / user_action.room_name）
      users    → 用户过滤（device.on_user/off_user、user_action.user_name）
      keyword  → 关键词（device.name / user_action.name·action / automation.name·desc /
                 xiaoai 文本 / health 名称·类型·备注 / printer 名称）
      limit    → 单次上限（默认 500，上限 5000；超出时 truncated=true）
      detail   → 是否返回 extra 明细（默认 true）

    返回：{range, count, total, truncated, by_source{}, items[]}（items 按 ts 倒序）
    """
    window = resolve_window(date, start, end, month, year, today)

    src = _csv(sources) or list(ALL_SOURCES)
    src = [s for s in src if s in _TIMELINE_HANDLERS]
    ev = _csv(events) or list(DEVICE_EVENTS)
    ev = [e for e in ev if e in DEVICE_EVENTS] or list(DEVICE_EVENTS)
    opts = {
        "entities": _csv(entities),
        "rooms": _csv(rooms),
        "users": _csv(users),
        "keyword": (keyword or "").strip(),
        "events": tuple(ev),
    }

    # 维度过滤收敛：不具备该维度的来源直接剔除（记录到 skipped_sources）
    skipped: list[str] = []
    for dim in ("entities", "rooms", "users"):
        if opts[dim]:
            keep = [s for s in src if dim in _SOURCE_CAPS.get(s, set())]
            skipped.extend(s for s in src if s not in keep)
            src = keep
    skipped = sorted(set(skipped))

    try:
        limit = int(limit or DEFAULT_TIMELINE_LIMIT)
    except (TypeError, ValueError):
        limit = DEFAULT_TIMELINE_LIMIT
    if limit <= 0:
        limit = DEFAULT_TIMELINE_LIMIT
    limit = min(limit, MAX_TIMELINE_LIMIT)

    items: list[dict] = []
    by_source: dict[str, int] = {}
    errors: dict[str, str] = {}
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        for s in src:
            try:
                got = _TIMELINE_HANDLERS[s](conn, window, opts)
            except sqlite3.OperationalError as exc:
                # 该来源的表可能还没建（功能未启用）
                errors[s] = str(exc)
                continue
            by_source[s] = len(got)
            items.extend(got)
    finally:
        conn.close()

    items.sort(key=lambda x: (x.get("ts") or ""), reverse=True)
    total = len(items)
    truncated = total > limit
    if truncated:
        items = items[:limit]

    for it in items:
        it["ts_ms"] = to_ms(it.get("ts") or "")
        if not detail:
            it.pop("extra", None)

    out = {
        "range": window["label"],
        "count": len(items),
        "total": total,
        "truncated": truncated,
        "by_source": by_source,
        "items": items,
    }
    if skipped:
        # 因不支持的过滤维度被自动剔除的来源（如 entities 过滤会剔除 自动化/健康/打印机）
        out["skipped_sources"] = skipped
    if errors:
        out["errors"] = errors
    return out


# =========================================================================== #
#  房间占用排行（严谨区间并集）+ 门户事件                                          #
# =========================================================================== #
def _merge_intervals(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    """区间并集：输入 (start, end) 列表，返回合并后的不重叠区间（按 start 升序）。"""
    if not intervals:
        return []
    intervals = sorted(intervals, key=lambda x: x[0])
    merged = [intervals[0]]
    for s, e in intervals[1:]:
        ls, le = merged[-1]
        if s <= le:                     # 有重叠或首尾相接 → 合并
            if e > le:
                merged[-1] = (ls, e)
        else:
            merged.append((s, e))
    return merged


def _hour_buckets(intervals: list[tuple[datetime, datetime]], day: str) -> list[float]:
    """把区间切分到 24 小时桶（小时为单位，返回 24 个累计小时数）。"""
    out = [0.0] * 24
    day_start = datetime.strptime(f"{day} 00:00:00", "%Y-%m-%d %H:%M:%S")
    for s, e in intervals:
        if e <= s:
            continue
        for h in range(24):
            hs = day_start + timedelta(hours=h)
            he = hs + timedelta(hours=1)
            lo, hi = max(s, hs), min(e, he)
            if hi > lo:
                out[h] += (hi - lo).total_seconds() / 3600.0
    return [round(v, 3) for v in out]


def compute_room_occupancy_sync(
    db_path: str,
    date: str = "",
    start: str = "",
    end: str = "",
    month: str = "",
    year: str = "",
    today: bool = False,
    rooms=None,
    include_empty: bool = False,
    bucket: str = "none",
    door: bool = True,
    detail: bool = True,
) -> dict:
    """房间占用排行（严谨口径：同房间重叠区间先并集再计时）+ 门户事件。

    参数：
      date/start+end/month/year/today → 时间窗（默认今日）
      rooms        → 只统计这些房间
      include_empty→ 无数据的房间也返回 0（房间清单取自 entity_configs.room）
      bucket       → none(默认) | day（按日序列）| hour（24 小时分布，仅单日有意义）
      door         → 是否附带门户（入户门）事件统计
      detail       → 是否返回每房间的合并区间明细

    返回：{range, total_duration_hour, top_room, occupied_rooms, rooms[], series?, door?}
    """
    window = resolve_window(date, start, end, month, year, today)
    bucket = (bucket or "none").strip().lower()
    if bucket not in ("none", "day", "hour"):
        bucket = "none"
    room_filter = _csv(rooms)
    now_dt = _local_now()

    cond, params = _time_cond(window, "on_time")
    conds, params = [cond, "name = ?"], list(params) + [NAME_PRESENCE]
    if room_filter:
        c, p = _in_cond("room", room_filter)
        conds.append(c); params.extend(p)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            f"SELECT id, room, on_time, off_time, duration FROM {TABLE_DEVICE_HISTORY} "
            f"WHERE {' AND '.join(conds)} ORDER BY on_time ASC",
            params,
        ).fetchall()

        # 门户事件（同一时间窗）
        door_rows = []
        if door:
            dcond, dparams = _time_cond(window, "on_time")
            door_rows = conn.execute(
                f"SELECT name, room, on_time, off_time, duration FROM {TABLE_DEVICE_HISTORY} "
                f"WHERE {dcond} AND name = ? ORDER BY on_time ASC",
                list(dparams) + [NAME_DOOR],
            ).fetchall()

        # 房间清单（include_empty 用）
        known_rooms: list[str] = []
        if include_empty:
            try:
                rr = conn.execute(
                    f"SELECT DISTINCT room FROM {TABLE_ENTITY_CONFIGS} "
                    f"WHERE room IS NOT NULL AND room <> '' ORDER BY room"
                ).fetchall()
                known_rooms = [r["room"] for r in rr]
            except sqlite3.OperationalError:
                known_rooms = []
    finally:
        conn.close()

    # 按房间聚合：原始记录 → 区间（运行中截止到当前时间）
    per_room: dict[str, dict] = {}
    per_room_day: dict[tuple[str, str], list] = {}
    for r in rows:
        room = (r["room"] or "未分配").strip() or "未分配"
        s = _parse_dt(r["on_time"])
        if s is None:
            continue
        running = not (r["off_time"] or "")
        e = now_dt if running else _parse_dt(r["off_time"])
        if e is None or e <= s:
            e = s
        g = per_room.setdefault(room, {
            "room": room, "count": 0, "intervals": [], "occupied": False,
            "last_end": None, "raw_duration": 0.0,
        })
        g["count"] += 1
        g["intervals"].append((s, e))
        if running:
            g["occupied"] = True
        if g["last_end"] is None or e > g["last_end"]:
            g["last_end"] = e
        try:
            g["raw_duration"] += float(r["duration"] or 0)
        except (TypeError, ValueError):
            pass
        day = (r["on_time"] or "")[:10]
        per_room_day.setdefault((room, day), []).append((s, e))

    rooms_out: list[dict] = []
    total_seconds = 0.0
    for room, g in per_room.items():
        merged = _merge_intervals(g["intervals"])
        secs = sum((e - s).total_seconds() for s, e in merged)
        total_seconds += secs
        item = {
            "room": room,
            "duration": round(secs, 0),
            "duration_hour": round(secs / 3600.0, 2),
            "count": g["count"],
            "segments": len(merged),
            "avg_hour": round(secs / 3600.0 / g["count"], 2) if g["count"] else 0.0,
            "occupied": bool(g["occupied"]),
            "last_seen": (g["last_end"] or datetime(1970, 1, 1)).strftime("%Y-%m-%d %H:%M:%S")
                         if g["last_end"] else "",
            # 原始 duration 累计（未并集，含重复上报）——便于对照口径差异
            "raw_duration_hour": round(g["raw_duration"] / 3600.0, 2),
        }
        if detail:
            item["intervals"] = [
                {"start": s.strftime("%Y-%m-%d %H:%M:%S"),
                 "end": e.strftime("%Y-%m-%d %H:%M:%S")}
                for s, e in merged
            ]
        rooms_out.append(item)

    # 无数据房间补 0
    if include_empty:
        have = {r["room"] for r in rooms_out}
        for room in known_rooms:
            if room in have:
                continue
            rooms_out.append({
                "room": room, "duration": 0, "duration_hour": 0.0, "count": 0,
                "segments": 0, "avg_hour": 0.0, "occupied": False, "last_seen": "",
                "raw_duration_hour": 0.0,
            })

    rooms_out.sort(key=lambda x: (x["duration"], x["room"]), reverse=True)
    for item in rooms_out:
        item["share"] = round(item["duration"] / total_seconds * 100, 1) if total_seconds else 0.0

    out: dict = {
        "range": window["label"],
        "total_duration": round(total_seconds, 0),
        "total_duration_hour": round(total_seconds / 3600.0, 2),
        "room_count": len(rooms_out),
        "occupied_rooms": [r["room"] for r in rooms_out if r["occupied"]],
        "top_room": rooms_out[0]["room"] if rooms_out and rooms_out[0]["duration"] > 0 else "",
        "rooms": rooms_out,
    }

    # 按日序列
    if bucket == "day":
        day_map: dict[str, dict] = {}
        for (room, day), ivs in per_room_day.items():
            merged = _merge_intervals(ivs)
            secs = sum((e - s).total_seconds() for s, e in merged)
            day_map.setdefault(day, {})[room] = round(secs / 3600.0, 3)
        out["series"] = [
            {"date": d, "rooms": day_map[d],
             "total_hour": round(sum(day_map[d].values()), 2)}
            for d in sorted(day_map)
        ]

    # 24 小时分布（单日查询时才有意义）
    if bucket == "hour":
        day = window.get("day") or ""
        if day:
            all_ivs: list[tuple[datetime, datetime]] = []
            for g in per_room.values():
                all_ivs.extend(_merge_intervals(g["intervals"]))
            out["by_hour"] = _hour_buckets(_merge_intervals(all_ivs) if all_ivs else [], day)
            out["by_hour_rooms"] = {
                room: _hour_buckets(_merge_intervals(g["intervals"]), day)
                for room, g in per_room.items()
            }

    # 门户事件
    if door:
        opens = 0
        open_secs = 0.0
        events: list[dict] = []
        last_open = ""
        open_now = False
        for r in door_rows:
            s = _parse_dt(r["on_time"])
            running = not (r["off_time"] or "")
            e = now_dt if running else _parse_dt(r["off_time"])
            if s is None:
                continue
            opens += 1
            if running:
                open_now = True
            if e is None or e <= s:
                e = s
            open_secs += max(0.0, (e - s).total_seconds())
            if not last_open or r["on_time"] > last_open:
                last_open = r["on_time"]
            events.append({
                "ts": r["on_time"], "event": "open",
                "title": "入户门 开启", "room": r["room"] or "",
                "detail": "已开 0.0 分钟" if running else "",
                "extra": {"duration": round(max(0.0, (e - s).total_seconds()), 0),
                          "running": running},
            })
            if not running and r["off_time"]:
                events.append({
                    "ts": r["off_time"], "event": "close",
                    "title": "入户门 关闭", "room": r["room"] or "", "detail": "",
                    "extra": {"duration": round(max(0.0, (e - s).total_seconds()), 0),
                              "running": False},
                })
        events.sort(key=lambda x: x.get("ts") or "", reverse=True)
        out["door"] = {
            "name": NAME_DOOR,
            "open_count": opens,
            "open_duration_hour": round(open_secs / 3600.0, 2),
            "last_open": last_open,
            "open_now": open_now,
            "events": events,
        }
    return out
