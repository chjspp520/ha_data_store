"""电力能耗综合查询模块（纯只读，无状态）。

为前端「电力中心」卡片提供 6 个查询，也全部可在 db_viewer 的「API 地址生成器」里直接测：

    power_meta       数据源探测与可用范围（表是否存在、实体、年月清单、最新结算日）
    power_dashboard  下钻主查询（totals + series + room_series + rooms + devices + hourly）
    power_breakdown  房间/设备明细（支持 children 与单设备趋势）
    power_reconcile  三源逐日对账（电网 / 全屋实时 / 设备合计）
    power_periods    数据可用性（年 → 月 → 日 三层）
    power_calendar   指定年月每日用电量（电网优先，实时兜底并标记来源）

三源口径（方案见 src/plan/power-center-plan.md）：

    年    attr_ele_year.yearEleNum   → 日表按年汇总 → env_power 按年汇总
    月    attr_ele_month.monthEleNum → 日表按月汇总 → env_power 按月汇总
    日    attr_ele_day.dayEleNum     → env_power 当天最后一条
    小时  env_power 相邻采样差值（电网没有小时数据）
    房间  device_history 聚合（电网/实时都没有房间级数据）

几条实测数据的纪律（见计划书 2.3，别改回去）：

  · device_history.energy_consumed 有负值（实测 -0.01 / -0.08），一律按"只累加 >0 的部分"处理；
  · cross_day=1 的记录 off_time 是 23:59:59，日归属按 on_time（与 whole_house_usage 一致）；
  · room='全屋' 的设备记录没有电量（实测 1311 条全空），设备聚合一律排除；
  · env_power 是"当日累计值，0 点归零"，所以日合计取当天最后一条，不能取 MAX（异常尖峰会污染）；
  · env_power 采样间隔历史是 5 分钟、近期 20 分钟，小时分摊必须按真实时间差，不能假设固定间隔；
  · attr_ele_day 有缺天（实测 861 天缺 11 天），series 一律用 None 断点，不要补 0。

安全：

  · 表名只允许 `^[A-Za-z_][A-Za-z0-9_]*$` 且必须在 sqlite_master 里真实存在，并带 attr_ 前缀；
  · 列名全部写死在 SQL 里，不来自用户；
  · entity_id / room 一律参数绑定。
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import date, datetime, timedelta

_LOGGER = logging.getLogger(__name__)

# 默认表名（用户可在前端配置里覆盖，例如 attr_ele_day_xxx）
DEFAULT_DAY_TABLE = "attr_ele_day"
DEFAULT_MONTH_TABLE = "attr_ele_month"
DEFAULT_YEAR_TABLE = "attr_ele_year"

# 全屋实时表（固定名：env_ 前缀 + metric_type）
ENV_POWER_TABLE = "env_power"

# 设备历史表（与 const.TABLE_DEVICE_HISTORY 一致；这里写死是为了模块独立可测，
# 也避免被测试脚本 import homeassistant 才能跑）
DEVICE_HISTORY_TABLE = "device_history"

# 合法表名（防注入：表名无法用参数绑定，只能白名单式校验）
_TABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# 设备聚合统一过滤条件：排除"全屋"这个伪房间（其记录没有电量）
_DEVICE_ROOM_EXCLUDE = "room <> '全屋'"

# 电量只认正值（负值是集成/积分异常，累加会把总量拉低）
_DEVICE_ENERGY_EXPR = (
    "SUM(CASE WHEN energy_consumed > 0 THEN energy_consumed ELSE 0 END)"
)


# ========================================================================== #
#  基础工具                                                                   #
# ========================================================================== #
def _f(value, default: float = 0.0) -> float:
    """安全转 float（None / '' / 'n/a' 一律回退）。"""
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _r(value, digits: int = 3) -> float:
    """四舍五入到指定小数（统一输出精度，避免浮点尾巴）。"""
    return round(_f(value), digits)


def _ratio(part: float, total: float) -> float:
    """比例（分母为 0 时返回 0，避免前端 NaN）。"""
    return round(part / total, 4) if total else 0.0


def _parse_date(text) -> date | None:
    """解析 'YYYY-MM-DD' / 'YYYY-MM-DD HH:MM:SS' → date。"""
    if not text:
        return None
    try:
        return datetime.strptime(str(text)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _parse_dt(text) -> datetime | None:
    """解析 'YYYY-MM-DD HH:MM:SS'（也兼容只有日期）。"""
    if not text:
        return None
    s = str(text).strip()
    for fmt, size in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d", 10)):
        try:
            return datetime.strptime(s[:size], fmt)
        except (TypeError, ValueError):
            continue
    return None


def _dstr(d: date) -> str:
    return d.strftime("%Y-%m-%d")


def _dtstr(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _today() -> date:
    return date.today()


def _shift_months(d: date, n: int) -> date:
    """按自然月平移（保持"当月 1 号"语义）。"""
    y = d.year + (d.month - 1 + n) // 12
    m = (d.month - 1 + n) % 12 + 1
    return date(y, m, 1)


def _shift_years(d: date, n: int) -> date:
    try:
        return d.replace(year=d.year + n)
    except ValueError:
        # 2/29 → 2/28
        return d.replace(year=d.year + n, day=28)


def _open(db_path: str) -> sqlite3.Connection:
    """只读连接（query_only 兜底：即使哪里写错也改不了库）。"""
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON;")
    except sqlite3.DatabaseError:
        pass
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    if not name or not _TABLE_RE.match(name):
        return False
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _table_columns(conn: sqlite3.Connection, name: str) -> set[str]:
    try:
        return {r[1] for r in conn.execute(f'PRAGMA table_info("{name}")')}
    except sqlite3.DatabaseError:
        return set()


def _resolve_table(
    conn: sqlite3.Connection,
    name: str,
    default: str,
    prefix: str = "attr_",
    *,
    strict: bool = False,
) -> str:
    """解析用户传入的表名。

    - 空串 → 用默认表名；默认表不存在时返回 ""（表示该数据源不可用，不报错，
      因为用户可能只配了日表、没有月/年表）；
    - 显式传了却不存在 → 先尝试加/去 prefix，仍不存在则：
        strict=True 抛 ValueError（给用户明确错误），否则返回 ""。
    """
    raw = (name or "").strip()
    if not raw:
        return default if _table_exists(conn, default) else ""
    if not _TABLE_RE.match(raw):
        raise ValueError(f"表名不合法：{raw}（只允许字母/数字/下划线）")
    if _table_exists(conn, raw):
        if prefix and not raw.startswith(prefix):
            # 允许显式指定非 attr_ 前缀的表，但必须是真实存在的表（已过存在性校验）
            return raw
        return raw
    alt = raw[len(prefix):] if prefix and raw.startswith(prefix) else f"{prefix}{raw}"
    if _table_exists(conn, alt):
        return alt
    if strict:
        raise ValueError(f"表 {raw} 不存在（检查前缀，属性表通常是 attr_ 开头）")
    return ""


def _pick_entity(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    explicit: str = "",
    where: str = "",
    params: tuple = (),
) -> str:
    """挑一个实体：显式给的优先，否则取该表里记录最多的实体。"""
    if not table:
        return ""
    if explicit:
        return explicit
    if column not in _table_columns(conn, table):
        return ""
    sql = f'SELECT entity_id FROM "{table}"'
    if where:
        sql += f" WHERE {where}"
    sql += f" GROUP BY entity_id ORDER BY COUNT(*) DESC LIMIT 1"
    try:
        row = conn.execute(sql, params).fetchone()
    except sqlite3.DatabaseError:
        return ""
    return str(row[0]) if row and row[0] else ""


def _resolve_sources(
    conn: sqlite3.Connection,
    day_table: str,
    month_table: str,
    year_table: str,
    grid_entity: str,
    env_entity: str,
) -> dict:
    """统一解析四张表与两个实体，后续所有查询都用这份结果。"""
    day_t = _resolve_table(conn, day_table, DEFAULT_DAY_TABLE)
    month_t = _resolve_table(conn, month_table, DEFAULT_MONTH_TABLE)
    year_t = _resolve_table(conn, year_table, DEFAULT_YEAR_TABLE)
    # 环境实时表是固定名，不需要用户配置
    env_t = ENV_POWER_TABLE if _table_exists(conn, ENV_POWER_TABLE) else ""
    dev_t = DEVICE_HISTORY_TABLE if _table_exists(conn, DEVICE_HISTORY_TABLE) else ""

    grid_e = _pick_entity(conn, day_t, "day", grid_entity)
    if not grid_e and month_t:
        grid_e = _pick_entity(conn, month_t, "month", "")
    if not grid_e and year_t:
        grid_e = _pick_entity(conn, year_t, "year", "")

    env_e = _pick_entity(conn, env_t, "datetime", env_entity, "room = ?", ("全屋",))
    if not env_e:
        env_e = _pick_entity(conn, env_t, "datetime", env_entity)

    return {
        "day_table": day_t,
        "month_table": month_t,
        "year_table": year_t,
        "env_table": env_t,
        "device_table": dev_t,
        "grid_entity": grid_e,
        "env_entity": env_e,
    }


# ========================================================================== #
#  周期解析                                                                   #
# ========================================================================== #
def _period(level: str, year: str, month: str, date_s: str, now: date | None = None) -> dict:
    """把 level + 参数解析成一个明确的半开区间 [start, end)。

    返回：
      level / key / label / start(date) / end(date) / buckets(list[str])
    """
    lv = (level or "day").strip().lower()
    if lv not in ("year", "month", "day"):
        lv = "day"
    today = now or _today()

    if lv == "year":
        y = int(year) if str(year).strip().isdigit() else today.year
        start = date(y, 1, 1)
        end = date(y + 1, 1, 1)
        return {
            "level": "year",
            "key": f"{y:04d}",
            "label": f"{y}年",
            "start": start,
            "end": end,
            "buckets": [f"{y:04d}-{m:02d}" for m in range(1, 13)],
        }

    if lv == "month":
        m = _parse_month(month) or date(today.year, today.month, 1)
        start = date(m.year, m.month, 1)
        end = _shift_months(start, 1)
        buckets = []
        cur = start
        while cur < end:
            buckets.append(_dstr(cur))
            cur += timedelta(days=1)
        return {
            "level": "month",
            "key": f"{start.year:04d}-{start.month:02d}",
            "label": f"{start.year}年{start.month}月",
            "start": start,
            "end": end,
            "buckets": buckets,
        }

    d = _parse_date(date_s) or today
    start = d
    end = d + timedelta(days=1)
    return {
        "level": "day",
        "key": _dstr(d),
        "label": _dstr(d),
        "start": start,
        "end": end,
        "buckets": [f"{h:02d}" for h in range(24)],
    }


def _parse_month(text) -> date | None:
    """'YYYY-MM' → 当月 1 号。"""
    s = (text or "").strip()
    if len(s) < 7:
        return None
    try:
        y, m = int(s[:4]), int(s[5:7])
    except ValueError:
        return None
    if not (1 <= m <= 12):
        return None
    return date(y, m, 1)


def _prev_period(per: dict) -> dict:
    """上一个同粒度周期（用于环比）。"""
    start, end = per["start"], per["end"]
    if per["level"] == "year":
        return _period("year", str(start.year - 1), "", "")
    if per["level"] == "month":
        prev = _shift_months(start, -1)
        return _period("month", "", f"{prev.year:04d}-{prev.month:02d}", "")
    prev = start - timedelta(days=1)
    return _period("day", "", "", _dstr(prev))


def _yoy_period(per: dict) -> dict | None:
    """去年同期（日/月有意义；年没有同比）。"""
    start = per["start"]
    if per["level"] == "year":
        return None
    if per["level"] == "month":
        y = start.year - 1
        return _period("month", "", f"{y:04d}-{start.month:02d}", "")
    prev = _shift_years(start, -1)
    return _period("day", "", "", _dstr(prev))


def _bucket_of(level: str, d: date) -> str:
    """日期 → 所属桶（年=YYYY-MM，月=YYYY-MM-DD，日=小时由调用方另算）。"""
    if level == "year":
        return f"{d.year:04d}-{d.month:02d}"
    return _dstr(d)


# ========================================================================== #
#  电网（attr_ele_day / month / year）                                        #
# ========================================================================== #
def _grid_daily(conn, table: str, entity: str, start: date, end: date) -> dict[str, float]:
    """日表 → {day: kwh}。"""
    if not table or not entity:
        return {}
    try:
        rows = conn.execute(
            f'SELECT day, dayEleNum FROM "{table}" '
            f"WHERE entity_id=? AND day>=? AND day<? ORDER BY day",
            (entity, _dstr(start), _dstr(end)),
        ).fetchall()
    except sqlite3.DatabaseError as exc:
        _LOGGER.warning("[power] 读取日表失败 table=%s: %s", table, exc)
        return {}
    out: dict[str, float] = {}
    for r in rows:
        d = str(r["day"] or "")[:10]
        if d:
            out[d] = _f(r["dayEleNum"])
    return out


def _grid_month_value(conn, table: str, entity: str, year: int, month: int) -> float | None:
    """月表某月官方值（没有则 None）。"""
    if not table or not entity:
        return None
    key = f"{year:04d}-{month:02d}"
    try:
        row = conn.execute(
            f'SELECT monthEleNum, is_official FROM "{table}" '
            f"WHERE entity_id=? AND month=? LIMIT 1",
            (entity, key),
        ).fetchone()
    except sqlite3.DatabaseError:
        return None
    return _f(row["monthEleNum"]) if row else None


def _grid_year_value(conn, table: str, entity: str, year: int) -> float | None:
    """年表某年官方值（没有则 None）。"""
    if not table or not entity:
        return None
    try:
        row = conn.execute(
            f'SELECT yearEleNum FROM "{table}" WHERE entity_id=? AND year=? LIMIT 1',
            (entity, f"{year:04d}"),
        ).fetchone()
    except sqlite3.DatabaseError:
        return None
    return _f(row["yearEleNum"]) if row else None


# ========================================================================== #
#  全屋实时（env_power）                                                      #
# ========================================================================== #
def _realtime_daily(
    conn, table: str, entity: str, start: date, end: date
) -> tuple[dict[str, float], dict[str, int]]:
    """每日最后一条采样（当日累计值）。

    为什么不用 MAX：异常尖峰会让"日合计"凭空变大；最后一条才是当天累计的终值。
    返回 ({day: kwh}, {day: 采样条数})。
    """
    if not table or not entity:
        return {}, {}
    try:
        rows = conn.execute(
            f'SELECT datetime, value FROM "{table}" '
            f"WHERE entity_id=? AND datetime>=? AND datetime<? ORDER BY datetime",
            (entity, f"{_dstr(start)} 00:00:00", f"{_dstr(end)} 00:00:00"),
        ).fetchall()
    except sqlite3.DatabaseError as exc:
        _LOGGER.warning("[power] 读取 %s 失败: %s", table, exc)
        return {}, {}

    last: dict[str, float] = {}
    count: dict[str, int] = {}
    for r in rows:
        d = str(r["datetime"] or "")[:10]
        if not d:
            continue
        last[d] = _f(r["value"])
        count[d] = count.get(d, 0) + 1
    return last, count


def _spread_hours(
    buckets: list[float], t0: datetime, t1: datetime, amount: float, day_start: datetime
) -> None:
    """把 amount 按 [t0, t1] 与每个小时桶的重叠比例分摊进 buckets[0..23]。"""
    if amount <= 0 or t1 <= t0:
        return
    total = (t1 - t0).total_seconds()
    if total <= 0:
        return
    cur = t0
    while cur < t1:
        hour_end = cur.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        nxt = min(t1, hour_end)
        seg = (nxt - cur).total_seconds()
        if seg > 0:
            idx = cur.hour
            if 0 <= idx < 24:
                buckets[idx] += amount * seg / total
        cur = nxt


def _realtime_hourly(
    conn, table: str, entity: str, day_start: date
) -> tuple[list[float], list[dict]]:
    """env_power 当天累计值 → 24 小时用电量。

    相邻采样做差；跨日（value 归零）产生的负差直接丢弃。
    返回 (buckets, samples)。
    """
    if not table or not entity:
        return [0.0] * 24, []
    d0 = _dstr(day_start)
    try:
        rows = conn.execute(
            f'SELECT datetime, value FROM "{table}" '
            f"WHERE entity_id=? AND datetime>=? AND datetime<? ORDER BY datetime",
            (entity, f"{d0} 00:00:00", f"{_dstr(day_start + timedelta(days=1))} 00:00:00"),
        ).fetchall()
    except sqlite3.DatabaseError:
        return [0.0] * 24, []

    buckets = [0.0] * 24
    samples: list[dict] = []
    prev_dt: datetime | None = None
    prev_v: float | None = None
    for r in rows:
        dt = _parse_dt(r["datetime"])
        v = _f(r["value"])
        samples.append({"t": str(r["datetime"] or ""), "v": _r(v, 3)})
        if dt is not None and prev_dt is not None and prev_v is not None:
            delta = v - prev_v
            if delta > 0:
                _spread_hours(buckets, prev_dt, dt, delta, datetime.combine(day_start, datetime.min.time()))
        prev_dt, prev_v = dt, v
    return [_r(b, 3) for b in buckets], samples


# ========================================================================== #
#  设备（device_history）                                                     #
# ========================================================================== #
def _device_by_room_entity(
    conn, table: str, start: date, end: date, room: str = "", entity: str = ""
) -> list[dict]:
    """周期内 房间 × 实体 聚合。"""
    if not table:
        return []
    where = [_DEVICE_ROOM_EXCLUDE, "on_time>=?", "on_time<?"]
    params: list = [_dstr(start), _dstr(end)]
    if room:
        where.append("room=?")
        params.append(room)
    if entity:
        where.append("entity_id=?")
        params.append(entity)
    sql = (
        f'SELECT room, entity_id, MAX(name) AS name, {_DEVICE_ENERGY_EXPR} AS kwh, '
        f"COUNT(*) AS cnt FROM {table} WHERE " + " AND ".join(where) +
        " GROUP BY room, entity_id ORDER BY kwh DESC"
    )
    try:
        rows = conn.execute(sql, tuple(params)).fetchall()
    except sqlite3.DatabaseError as exc:
        _LOGGER.warning("[power] 设备聚合失败: %s", exc)
        return []
    return [
        {
            "room": str(r["room"] or ""),
            "entity_id": str(r["entity_id"] or ""),
            "name": str(r["name"] or r["entity_id"] or ""),
            "kwh": _r(r["kwh"]),
            "count": int(r["cnt"] or 0),
        }
        for r in rows
    ]


def _device_matrix(
    conn, table: str, start: date, end: date, room: str = "", entity: str = ""
) -> list[dict]:
    """一次性取「房间 × 实体 × 日」矩阵。

    dashboard 需要三种视图（按实体 / 按日 / 房间×日），分别查 SQL 会把年度
    device_history 扫三遍（实测 185ms × 3）。这里只扫一次，三种视图都在 Python 侧聚合。
    """
    if not table:
        return []
    where = [_DEVICE_ROOM_EXCLUDE, "on_time>=?", "on_time<?"]
    params: list = [_dstr(start), _dstr(end)]
    if room:
        where.append("room=?")
        params.append(room)
    if entity:
        where.append("entity_id=?")
        params.append(entity)
    sql = (
        f'SELECT room, entity_id, MAX(name) AS name, substr(on_time,1,10) AS d, '
        f"{_DEVICE_ENERGY_EXPR} AS kwh, COUNT(*) AS cnt FROM {table} "
        "WHERE " + " AND ".join(where) + " GROUP BY room, entity_id, d"
    )
    try:
        rows = conn.execute(sql, tuple(params)).fetchall()
    except sqlite3.DatabaseError as exc:
        _LOGGER.warning("[power] 设备矩阵查询失败: %s", exc)
        return []
    return [
        {
            "room": str(r["room"] or ""),
            "entity_id": str(r["entity_id"] or ""),
            "name": str(r["name"] or r["entity_id"] or ""),
            "day": str(r["d"] or "")[:10],
            "kwh": _f(r["kwh"]),
            "count": int(r["cnt"] or 0),
        }
        for r in rows
        if r["room"] and r["entity_id"] and r["d"]
    ]


def _matrix_to_entities(matrix: list[dict]) -> list[dict]:
    """房间×实体×日 → 房间×实体（电量 / 次数累加）。"""
    acc: dict[tuple, dict] = {}
    for r in matrix:
        key = (r["room"], r["entity_id"])
        item = acc.get(key)
        if item is None:
            item = {
                "room": r["room"], "entity_id": r["entity_id"], "name": r["name"],
                "kwh": 0.0, "count": 0,
            }
            acc[key] = item
        item["kwh"] += r["kwh"]
        item["count"] += r["count"]
    out = list(acc.values())
    for item in out:
        item["kwh"] = _r(item["kwh"])
    out.sort(key=lambda x: x["kwh"], reverse=True)
    return out


def _matrix_to_days(matrix: list[dict]) -> dict[str, float]:
    """房间×实体×日 → {day: kwh}。"""
    acc: dict[str, float] = {}
    for r in matrix:
        acc[r["day"]] = acc.get(r["day"], 0.0) + r["kwh"]
    return acc


def _matrix_to_room_days(matrix: list[dict]) -> dict[str, dict[str, float]]:
    """房间×实体×日 → {room: {day: kwh}}。"""
    acc: dict[str, dict[str, float]] = {}
    for r in matrix:
        room_map = acc.setdefault(r["room"], {})
        room_map[r["day"]] = room_map.get(r["day"], 0.0) + r["kwh"]
    return acc


def _device_by_day(
    conn, table: str, start: date, end: date, room: str = "", entity: str = ""
) -> dict[str, float]:
    """周期内按天聚合 {day: kwh}。"""
    if not table:
        return {}
    where = [_DEVICE_ROOM_EXCLUDE, "on_time>=?", "on_time<?"]
    params: list = [_dstr(start), _dstr(end)]
    if room:
        where.append("room=?")
        params.append(room)
    if entity:
        where.append("entity_id=?")
        params.append(entity)
    sql = (
        f'SELECT substr(on_time,1,10) AS d, {_DEVICE_ENERGY_EXPR} AS kwh FROM {table} '
        "WHERE " + " AND ".join(where) + " GROUP BY d"
    )
    try:
        rows = conn.execute(sql, tuple(params)).fetchall()
    except sqlite3.DatabaseError:
        return {}
    return {str(r["d"] or ""): _f(r["kwh"]) for r in rows if r["d"]}


def _device_by_room_day(conn, table: str, start: date, end: date) -> dict[str, dict[str, float]]:
    """周期内 房间 × 天 矩阵 {room: {day: kwh}}。"""
    if not table:
        return {}
    sql = (
        f'SELECT room, substr(on_time,1,10) AS d, {_DEVICE_ENERGY_EXPR} AS kwh '
        f"FROM {table} WHERE {_DEVICE_ROOM_EXCLUDE} AND on_time>=? AND on_time<? "
        "GROUP BY room, d"
    )
    try:
        rows = conn.execute(sql, (_dstr(start), _dstr(end))).fetchall()
    except sqlite3.DatabaseError:
        return {}
    out: dict[str, dict[str, float]] = {}
    for r in rows:
        room = str(r["room"] or "")
        d = str(r["d"] or "")
        if not room or not d:
            continue
        out.setdefault(room, {})[d] = _f(r["kwh"])
    return out


def _device_room_totals(conn, table: str, now: date | None = None) -> list[dict]:
    """每个房间的「今日 / 本月 / 今年」（一次 SQL 扫完，房间轴首页用）。"""
    if not table:
        return []
    today = now or _today()
    today_start = _dstr(today)
    month_start = _dstr(date(today.year, today.month, 1))
    year_start = _dstr(date(today.year, 1, 1))
    sql = (
        f'SELECT room, '
        f"SUM(CASE WHEN on_time >= ? THEN e ELSE 0 END) AS today, "
        f"SUM(CASE WHEN on_time >= ? THEN e ELSE 0 END) AS month, "
        f"SUM(CASE WHEN on_time >= ? THEN e ELSE 0 END) AS year, "
        f"COUNT(DISTINCT entity_id) AS device_count "
        f"FROM (SELECT room, entity_id, on_time, "
        f"        CASE WHEN energy_consumed > 0 THEN energy_consumed ELSE 0 END AS e "
        f"      FROM {table} WHERE on_time >= ? AND {_DEVICE_ROOM_EXCLUDE}) "
        f"GROUP BY room ORDER BY today DESC"
    )
    try:
        rows = conn.execute(sql, (today_start, month_start, year_start, year_start)).fetchall()
    except sqlite3.DatabaseError:
        return []
    return [
        {
            "room": str(r["room"] or ""),
            "today": _r(r["today"]),
            "month": _r(r["month"]),
            "year": _r(r["year"]),
            "device_count": int(r["device_count"] or 0),
        }
        for r in rows
        if r["room"]
    ]


def _device_hourly(
    conn, table: str, day: date, room: str = ""
) -> tuple[list[float], dict[str, list[float]], dict[str, dict]]:
    """设备电量分摊到 24 小时（按 on/off 与小时桶的重叠比例）。

    返回 (总量 24 桶, {房间: 24 桶}, {entity_id: {name, room, buckets}})。
    第三项给"时间轴 → 时"这一级用：该小时的设备排行。
    跨天记录按当天 00:00~24:00 截断；运行中（off_time 空）用 now 作终点。
    """
    if not table:
        return [0.0] * 24, {}, {}
    day0 = datetime.combine(day, datetime.min.time())
    day1 = day0 + timedelta(days=1)
    where = [
        "on_time < ?",
        "(off_time = '' OR off_time IS NULL OR off_time >= ?)",
        _DEVICE_ROOM_EXCLUDE,
    ]
    params: list = [_dtstr(day1), _dtstr(day0)]
    if room:
        where.append("room=?")
        params.append(room)
    sql = (
        f'SELECT room, entity_id, name, on_time, off_time, energy_consumed, now_kwh, on_power '
        f"FROM {table} WHERE " + " AND ".join(where)
    )
    try:
        rows = conn.execute(sql, tuple(params)).fetchall()
    except sqlite3.DatabaseError:
        return [0.0] * 24, {}, {}

    buckets = [0.0] * 24
    by_room: dict[str, list[float]] = {}
    by_device: dict[str, dict] = {}
    now_dt = datetime.now()
    for r in rows:
        on_dt = _parse_dt(r["on_time"])
        if on_dt is None:
            continue
        off_raw = r["off_time"]
        off_dt = _parse_dt(off_raw) if off_raw and str(off_raw).strip() else now_dt
        if off_dt is None:
            off_dt = now_dt
        start = max(on_dt, day0)
        end = min(off_dt, day1)
        if end <= start:
            continue

        energy = _f(r["energy_consumed"])
        if energy <= 0:
            # 运行中没有落库电量：尽量用表读数差估算
            now_kwh = r["now_kwh"]
            on_power = r["on_power"]
            if now_kwh is not None and on_power is not None:
                energy = max(_f(now_kwh) - _f(on_power), 0.0)
            else:
                energy = 0.0
        if energy <= 0:
            continue

        room_name = str(r["room"] or "")
        rb = by_room.get(room_name)
        if rb is None:
            rb = [0.0] * 24
            by_room[room_name] = rb
        eid = str(r["entity_id"] or "")
        dev = by_device.get(eid)
        if dev is None:
            dev = {
                "entity_id": eid,
                "name": str(r["name"] or eid),
                "room": room_name,
                "buckets": [0.0] * 24,
            }
            by_device[eid] = dev
        # 先分摊到临时桶再累加，保证总量 / 房间桶 / 设备桶逐项一致
        tmp = [0.0] * 24
        _spread_hours(tmp, start, end, energy, day0)
        for i in range(24):
            buckets[i] += tmp[i]
            rb[i] += tmp[i]
            dev["buckets"][i] += tmp[i]

    devices_out = {}
    for eid, dev in by_device.items():
        arr = [_r(x, 3) for x in dev["buckets"]]
        if sum(arr) <= 0:
            continue
        devices_out[eid] = {
            "entity_id": eid,
            "name": dev["name"],
            "room": dev["room"],
            "buckets": arr,
        }
    return (
        [_r(b, 3) for b in buckets],
        {k: [_r(x, 3) for x in v] for k, v in by_room.items()},
        devices_out,
    )


# ========================================================================== #
#  总量组装                                                                   #
# ========================================================================== #
def _sum_in_range(values: dict[str, float], start: date, end: date) -> float:
    total = 0.0
    cur = start
    while cur < end:
        total += values.get(_dstr(cur), 0.0)
        cur += timedelta(days=1)
    return total


def _grid_total(
    conn, src: dict, per: dict, daily: dict[str, float] | None = None
) -> tuple[float, str, float]:
    """周期内电网总量。

    返回 (总量, 来源说明, 日表汇总对照)。
    - 年：年表 → 日表汇总
    - 月：月表 → 日表汇总
    - 日：日表

    daily 可由调用方预先查好传入（dashboard 同时要用它算 series，避免重复扫表）。
    """
    if daily is None:
        daily = _grid_daily(conn, src["day_table"], src["grid_entity"], per["start"], per["end"])
    day_sum = sum(daily.values())
    if per["level"] == "year" and src["year_table"]:
        v = _grid_year_value(conn, src["year_table"], src["grid_entity"], per["start"].year)
        if v is not None:
            return v, "year_table", day_sum
    if per["level"] == "month" and src["month_table"]:
        v = _grid_month_value(
            conn, src["month_table"], src["grid_entity"], per["start"].year, per["start"].month
        )
        if v is not None:
            return v, "month_table", day_sum
    if daily:
        return day_sum, "day_table", day_sum
    # 日表没有：退到月/年表
    if per["level"] == "month" and src["month_table"]:
        v = _grid_month_value(
            conn, src["month_table"], src["grid_entity"], per["start"].year, per["start"].month
        )
        if v is not None:
            return v, "month_table", 0.0
    if per["level"] == "year" and src["year_table"]:
        v = _grid_year_value(conn, src["year_table"], src["grid_entity"], per["start"].year)
        if v is not None:
            return v, "year_table", 0.0
    return 0.0, "", day_sum


def _realtime_total(conn, src: dict, per: dict) -> tuple[float, int, int]:
    """周期内全屋实时总量（每天最后一条之和）、采样总数、有数据天数。"""
    last, counts = _realtime_daily(conn, src["env_table"], src["env_entity"], per["start"], per["end"])
    return sum(last.values()), sum(counts.values()), len(last)


def _device_total(conn, src: dict, per: dict, room: str = "", entity: str = "") -> tuple[float, int, int]:
    """周期内设备合计、实体数、房间数。"""
    rows = _device_by_room_entity(conn, src["device_table"], per["start"], per["end"], room, entity)
    total = sum(r["kwh"] for r in rows)
    return _r(total), len({r["entity_id"] for r in rows}), len({r["room"] for r in rows})


def _build_series(
    conn, src: dict, per: dict, room: str = "", entity: str = "",
    grid_map: dict[str, float] | None = None,
    rt_daily: dict[str, float] | None = None,
    dev_daily: dict[str, float] | None = None,
) -> dict:
    """按当前 level 组装序列（年=12 月 / 月=逐日 / 日=24 小时）。

    grid_map / rt_daily / dev_daily 可由调用方预取后传入 —— dashboard 的 totals
    已经查过同样的数据，不传就会把整年 4.8 万行 env_power 再扫一遍（实测 +285ms）。
    """
    buckets = per["buckets"]
    if per["level"] == "day":
        rt_buckets, samples = _realtime_hourly(conn, src["env_table"], src["env_entity"], per["start"])
        dev_buckets, dev_by_room, dev_by_device = _device_hourly(
            conn, src["device_table"], per["start"], room
        )
        unmetered = [
            _r(rt_buckets[i] - dev_buckets[i]) if (rt_buckets[i] or dev_buckets[i]) else None
            for i in range(24)
        ]
        return {
            "buckets": buckets,
            "grid": None,           # 电网没有小时数据
            "realtime": rt_buckets,
            "device": dev_buckets,
            "unmetered": unmetered,
            "units": {"energy": "kWh"},
            "samples": samples,
            "by_room": dev_by_room,
            "by_device": dev_by_device,   # 时级下钻：该小时的设备排行
        }

    # 年 / 月：按天/月聚合（优先用调用方预取的结果）
    if grid_map is None:
        grid_map = _grid_daily(conn, src["day_table"], src["grid_entity"], per["start"], per["end"])
    if rt_daily is None:
        rt_daily, _ = _realtime_daily(conn, src["env_table"], src["env_entity"], per["start"], per["end"])
    if dev_daily is None:
        dev_daily = _device_by_day(conn, src["device_table"], per["start"], per["end"], room, entity)

    def _agg(mapping: dict[str, float], key: str, missing_none: bool = False):
        """把 {day: v} 归并到桶（年=月桶，月=日桶）。

        missing_none=True 时"该桶没有任何原始记录"返回 None（图表断开），
        用于电网 / 实时：把缺失画成 0 会让人误以为当天真的没用电。
        设备侧保持 0：设备没记录就是没用电，柱状图不该断。
        """
        if per["level"] == "year":
            pref = key + "-"
            vals = [v for d, v in mapping.items() if d.startswith(pref)]
            if not vals:
                return None if missing_none else 0.0
            return _r(sum(vals))
        if key in mapping:
            return _r(mapping[key])
        return None if missing_none else 0.0

    grid = [_agg(grid_map, b, True) if grid_map else None for b in buckets]
    rt = [_agg(rt_daily, b, True) if rt_daily else None for b in buckets]
    dev = [_agg(dev_daily, b) for b in buckets]
    unmetered = [
        # rt 可能是 None（该桶没有任何实时采样，见 _agg 的 missing_none），
        # 直接相减会 TypeError —— 两个都非 None 才算缺口
        _r(rt[i] - dev[i]) if (rt[i] is not None and dev[i] is not None) else None
        for i in range(len(buckets))
    ]
    return {
        "buckets": buckets,
        "grid": grid,
        "realtime": rt,
        "device": dev,
        "unmetered": unmetered,
        "units": {"energy": "kWh"},
    }


def _build_room_series(
    conn, src: dict, per: dict, room_days: dict[str, dict[str, float]] | None = None
) -> dict[str, list]:
    """房间 × 时间桶矩阵（房间轴用）。日级按 24 小时：只有设备可拆。

    room_days 可由调用方从设备矩阵聚合后传入（dashboard 已经查过矩阵，避免再扫一遍）。
    """
    if per["level"] == "day":
        _, by_room, _ = _device_hourly(conn, src["device_table"], per["start"])
        return by_room

    if room_days is None:
        room_days = _matrix_to_room_days(
            _device_matrix(conn, src["device_table"], per["start"], per["end"])
        )
    out: dict[str, list] = {}
    for room, mapping in room_days.items():
        if per["level"] == "year":
            out[room] = [
                _r(sum(v for d, v in mapping.items() if d.startswith(b + "-")))
                for b in per["buckets"]
            ]
        else:
            out[room] = [_r(mapping.get(b, 0.0)) for b in per["buckets"]]
    return out


def _gaps(grid: float, realtime: float, device: float) -> dict:
    """两层缺口（设备未计量 / 电网差额）。"""
    device_gap = max(realtime - device, 0.0)
    grid_gap = max(grid - realtime, 0.0)
    return {
        "device_gap": _r(device_gap),
        "device_gap_ratio": _ratio(device_gap, realtime),
        "grid_gap": _r(grid_gap),
        "grid_gap_ratio": _ratio(grid_gap, grid),
    }


def _primary(total_grid: float, total_rt: float, total_dev: float, source: str) -> dict:
    """主口径：auto = 电网优先（有值且 >0），否则实时。"""
    src = (source or "auto").strip().lower()
    if src == "auto":
        src = "grid" if total_grid > 0 else "realtime"
    if src == "grid":
        return {"kwh": _r(total_grid), "source": "grid", "settled": True}
    if src == "device":
        return {"kwh": _r(total_dev), "source": "device", "settled": False}
    return {"kwh": _r(total_rt), "source": "realtime", "settled": False}


def _compare(
    conn, src: dict, per: dict, primary_kwh: float, kind: str, primary_source: str = "auto"
) -> dict | None:
    """环比 / 同比（**只查主口径对应的那一个源**，避免电网与实时混着比）。

    早期实现两个源都查：年度一次多扫 4.8 万行 env_power，纯属浪费——
    主数值是电网，就该拿电网的上一周期比。
    """
    if kind == "prev":
        other = _prev_period(per)
    else:
        other = _yoy_period(per)
    if other is None:
        return None
    if primary_source == "grid":
        kwh, _, _ = _grid_total(conn, src, other)
    elif primary_source == "device":
        kwh, _, _ = _device_total(conn, src, other)
    else:
        kwh, _, _ = _realtime_total(conn, src, other)
    if kwh <= 0 and primary_kwh > 0:
        return None
    delta = _r(primary_kwh - kwh)
    return {
        "key": other["key"],
        "label": other["label"],
        "kwh": _r(kwh),
        "delta": delta,
        "delta_pct": _ratio(delta, kwh),
    }


# ========================================================================== #
#  对外：power_meta                                                           #
# ========================================================================== #
def compute_power_meta_sync(
    db_path: str,
    day_table: str = "",
    month_table: str = "",
    year_table: str = "",
    entity_id: str = "",
    env_entity: str = "",
    room: str = "",
) -> dict:
    """数据源探测：表是否存在、实体、时间范围、年月清单、最新结算日。"""
    conn = _open(db_path)
    try:
        src = _resolve_sources(conn, day_table, month_table, year_table, entity_id, env_entity)
        warnings: list[str] = []

        # ---- 电网 ----
        grid = {"available": False, "table": src["day_table"], "entity_id": src["grid_entity"]}
        if src["day_table"] and src["grid_entity"]:
            row = conn.execute(
                f'SELECT COUNT(*) AS c, MIN(day) AS mn, MAX(day) AS mx FROM "{src["day_table"]}" '
                f"WHERE entity_id=?",
                (src["grid_entity"],),
            ).fetchone()
            days = [str(r[0])[:10] for r in conn.execute(
                f'SELECT day FROM "{src["day_table"]}" WHERE entity_id=? ORDER BY day',
                (src["grid_entity"],),
            ).fetchall()]
            missing = 0
            if days:
                d0, d1 = _parse_date(days[0]), _parse_date(days[-1])
                if d0 and d1:
                    have = set(days)
                    cur = d0
                    while cur <= d1:
                        if _dstr(cur) not in have:
                            missing += 1
                        cur += timedelta(days=1)
            grid.update({
                "available": bool(row and row["c"]),
                "min_day": str(row["mn"] or "")[:10] if row else "",
                "max_day": str(row["mx"] or "")[:10] if row else "",
                "day_count": int(row["c"] or 0) if row else 0,
                "missing_days": missing,
            })
            if missing:
                warnings.append(f"电网日表缺少 {missing} 天（日历会显示为无数据）")
        elif day_table:
            warnings.append(f"电网日表 {day_table} 不存在或为空")

        # ---- 全屋实时 ----
        realtime = {"available": False, "table": src["env_table"], "entity_id": src["env_entity"]}
        if src["env_table"] and src["env_entity"]:
            row = conn.execute(
                f'SELECT COUNT(*) AS c, MIN(datetime) AS mn, MAX(datetime) AS mx '
                f"FROM {ENV_POWER_TABLE} WHERE entity_id=?",
                (src["env_entity"],),
            ).fetchone()
            # 采样间隔：取最近 20 条的中位间隔（历史有 5min / 近期 20min 两种）
            recent = conn.execute(
                f"SELECT datetime FROM {ENV_POWER_TABLE} WHERE entity_id=? "
                f"ORDER BY datetime DESC LIMIT 20",
                (src["env_entity"],),
            ).fetchall()
            intervals: list[float] = []
            prev = None
            for r in recent:
                dt = _parse_dt(r["datetime"])
                if dt and prev:
                    intervals.append((prev - dt).total_seconds() / 60)
                prev = dt
            interval = 0
            if intervals:
                intervals.sort()
                interval = int(round(intervals[len(intervals) // 2]))
            today_last = conn.execute(
                f"SELECT value FROM {ENV_POWER_TABLE} WHERE entity_id=? AND datetime>=? "
                f"ORDER BY datetime DESC LIMIT 1",
                (src["env_entity"], f"{_dstr(_today())} 00:00:00"),
            ).fetchone()
            realtime.update({
                "available": bool(row and row["c"]),
                "min_datetime": str(row["mn"] or "") if row else "",
                "max_datetime": str(row["mx"] or "") if row else "",
                "interval_minutes": interval,
                "today_kwh": _r(today_last["value"]) if today_last else 0.0,
            })
        else:
            warnings.append("未找到 env_power 全屋实时数据")

        # ---- 设备 ----
        device = {"available": False, "entities": 0, "rooms": []}
        if src["device_table"]:
            where = _DEVICE_ROOM_EXCLUDE
            params: tuple = ()
            if room:
                where += " AND room=?"
                params = (room,)
            row = conn.execute(
                f"SELECT COUNT(DISTINCT entity_id) AS e, MIN(on_time) AS mn, MAX(on_time) AS mx "
                f"FROM {DEVICE_HISTORY_TABLE} WHERE {where} AND energy_consumed>0",
                params,
            ).fetchone()
            rooms = [str(r[0]) for r in conn.execute(
                f"SELECT DISTINCT room FROM {DEVICE_HISTORY_TABLE} "
                f"WHERE {_DEVICE_ROOM_EXCLUDE} AND room<>'' ORDER BY room"
            ).fetchall()]
            device.update({
                "available": bool(row and row["e"]),
                "entities": int(row["e"] or 0) if row else 0,
                "rooms": rooms,
                "min_time": str(row["mn"] or "") if row else "",
                "max_time": str(row["mx"] or "") if row else "",
            })

        # ---- 年月清单（年份下拉 / 月份面板）----
        years = _periods_years(conn, src)
        months = _periods_months(conn, src, _today().year)

        latest = grid.get("max_day") or ""
        return {
            "sources": {"grid": grid, "realtime": realtime, "device": device},
            "years": years,
            "months": months,
            "latest_settled_day": latest,
            "today": _dstr(_today()),
            "warnings": warnings,
        }
    finally:
        conn.close()


def _periods_years(conn, src: dict) -> list[dict]:
    """有数据的年份（日表 + 实时 + 年表 + 设备合并）。

    count = 该年"有总量数据"的天数（电网与实时取大者）；
    另给 grid_days / realtime_days / device_days 供前端展示。
    """
    kinds: dict[str, set] = {}
    grid_days: dict[str, int] = {}
    rt_days: dict[str, int] = {}
    dev_days: dict[str, int] = {}

    def _mark(y: str, kind: str):
        if y and len(y) >= 4 and y[:4].isdigit():
            kinds.setdefault(y[:4], set()).add(kind)

    if src["day_table"] and src["grid_entity"]:
        try:
            for r in conn.execute(
                f'SELECT substr(day,1,4) AS y, COUNT(DISTINCT day) AS c FROM "{src["day_table"]}" '
                f"WHERE entity_id=? GROUP BY y",
                (src["grid_entity"],),
            ).fetchall():
                y = str(r["y"] or "")
                if y:
                    grid_days[y] = int(r["c"] or 0)
                    _mark(y, "grid")
        except sqlite3.DatabaseError:
            pass
    if src["env_table"] and src["env_entity"]:
        try:
            for r in conn.execute(
                f"SELECT substr(datetime,1,4) AS y, COUNT(DISTINCT substr(datetime,1,10)) AS c "
                f"FROM {ENV_POWER_TABLE} WHERE entity_id=? GROUP BY y",
                (src["env_entity"],),
            ).fetchall():
                y = str(r["y"] or "")
                if y:
                    rt_days[y] = int(r["c"] or 0)
                    _mark(y, "realtime")
        except sqlite3.DatabaseError:
            pass
    if src["year_table"] and src["grid_entity"]:
        try:
            for r in conn.execute(
                f'SELECT DISTINCT year AS y FROM "{src["year_table"]}" WHERE entity_id=?',
                (src["grid_entity"],),
            ).fetchall():
                _mark(str(r["y"] or ""), "grid")
        except sqlite3.DatabaseError:
            pass
    if src["device_table"]:
        try:
            for r in conn.execute(
                f"SELECT substr(on_time,1,4) AS y, COUNT(DISTINCT substr(on_time,1,10)) AS c "
                f"FROM {DEVICE_HISTORY_TABLE} WHERE energy_consumed>0 AND {_DEVICE_ROOM_EXCLUDE} "
                f"GROUP BY y"
            ).fetchall():
                y = str(r["y"] or "")
                if y:
                    dev_days[y] = int(r["c"] or 0)
                    _mark(y, "device")
        except sqlite3.DatabaseError:
            pass

    out = []
    for y in sorted(kinds.keys(), reverse=True):
        out.append({
            "value": y,
            "count": max(grid_days.get(y, 0), rt_days.get(y, 0)),
            "grid_days": grid_days.get(y, 0),
            "realtime_days": rt_days.get(y, 0),
            "device_days": dev_days.get(y, 0),
            "grid": "grid" in kinds[y],
            "realtime": "realtime" in kinds[y],
            "device": "device" in kinds[y],
        })
    return out


def _periods_months(conn, src: dict, year: int) -> list[dict]:
    """指定年份下有数据的月份（日表 + 实时 + 月表 + 设备合并）。"""
    y = f"{year:04d}"
    y0, y1 = f"{y}-01-01", f"{int(y) + 1:04d}-01-01"
    kinds: dict[str, set] = {}
    grid_days: dict[str, int] = {}
    rt_days: dict[str, int] = {}
    dev_days: dict[str, int] = {}

    def _mark(m: str, kind: str):
        if m and len(m) >= 7 and m[:4] == y:
            kinds.setdefault(m[:7], set()).add(kind)

    if src["day_table"] and src["grid_entity"]:
        try:
            for r in conn.execute(
                f'SELECT substr(day,1,7) AS m, COUNT(DISTINCT day) AS c FROM "{src["day_table"]}" '
                f"WHERE entity_id=? AND day>=? AND day<? GROUP BY m",
                (src["grid_entity"], y0, y1),
            ).fetchall():
                m = str(r["m"] or "")
                if m:
                    grid_days[m] = int(r["c"] or 0)
                    _mark(m, "grid")
        except sqlite3.DatabaseError:
            pass
    if src["env_table"] and src["env_entity"]:
        try:
            for r in conn.execute(
                f"SELECT substr(datetime,1,7) AS m, COUNT(DISTINCT substr(datetime,1,10)) AS c "
                f"FROM {ENV_POWER_TABLE} WHERE entity_id=? AND datetime>=? AND datetime<? GROUP BY m",
                (src["env_entity"], f"{y0} 00:00:00", f"{y1} 00:00:00"),
            ).fetchall():
                m = str(r["m"] or "")
                if m:
                    rt_days[m] = int(r["c"] or 0)
                    _mark(m, "realtime")
        except sqlite3.DatabaseError:
            pass
    if src["month_table"] and src["grid_entity"]:
        try:
            for r in conn.execute(
                f'SELECT DISTINCT month AS m FROM "{src["month_table"]}" '
                f"WHERE entity_id=? AND month>=? AND month<?",
                (src["grid_entity"], f"{y}-01", f"{int(y) + 1:04d}-01"),
            ).fetchall():
                _mark(str(r["m"] or ""), "grid")
        except sqlite3.DatabaseError:
            pass
    if src["device_table"]:
        try:
            for r in conn.execute(
                f"SELECT substr(on_time,1,7) AS m, COUNT(DISTINCT substr(on_time,1,10)) AS c "
                f"FROM {DEVICE_HISTORY_TABLE} WHERE energy_consumed>0 AND {_DEVICE_ROOM_EXCLUDE} "
                f"AND on_time>=? AND on_time<? GROUP BY m",
                (y0, y1),
            ).fetchall():
                m = str(r["m"] or "")
                if m:
                    dev_days[m] = int(r["c"] or 0)
                    _mark(m, "device")
        except sqlite3.DatabaseError:
            pass

    out = []
    for m in sorted(kinds.keys(), reverse=True):
        out.append({
            "value": m,
            "count": max(grid_days.get(m, 0), rt_days.get(m, 0)),
            "grid_days": grid_days.get(m, 0),
            "realtime_days": rt_days.get(m, 0),
            "device_days": dev_days.get(m, 0),
            "grid": "grid" in kinds[m],
            "realtime": "realtime" in kinds[m],
            "device": "device" in kinds[m],
        })
    return out


# ========================================================================== #
#  对外：power_dashboard                                                      #
# ========================================================================== #
# ========================================================================== #
#  视图模型（后端直接喂给前端渲染，前端不做数据加工）                          #
# ========================================================================== #
ROOM_COLORS = [
    "#4e79a7", "#f28e2b", "#59a14f", "#e15759", "#76b7b2",
    "#edc948", "#b07aa1", "#ff9da7", "#9c755f", "#bab0ac",
    "#6a9fb5", "#d37295", "#8cd17d", "#f1ce63", "#d4a6c8",
]
COLOR_UNMETERED = "#9e9e9e"
COLOR_GRID = "#ffb300"
COLOR_REALTIME = "#42a5f5"

_ROOM_COLOR_MAP: dict[str, str] = {}


def _room_color(room: str) -> str:
    """房间 → 固定色（按首次出现顺序分配，进程内稳定）。"""
    name = str(room or "")
    if not name:
        return ROOM_COLORS[0]
    color = _ROOM_COLOR_MAP.get(name)
    if color is None:
        color = ROOM_COLORS[len(_ROOM_COLOR_MAP) % len(ROOM_COLORS)]
        _ROOM_COLOR_MAP[name] = color
    return color


def _fmt_num(value) -> str:
    """统一两位小数；None → '—'。"""
    if value is None:
        return "—"
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return "—"


def _fmt_pct(ratio) -> str:
    if ratio is None:
        return "—"
    try:
        return f"{float(ratio) * 100:.1f}%"
    except (TypeError, ValueError):
        return "—"


def _axis_labels(buckets: list[str], level: str) -> list[str]:
    """桶键 → 横轴显示文本（前端不再做任何字符串处理）。"""
    if level == "years":
        return [str(b) for b in buckets]
    if level == "year":
        out = []
        for b in buckets:
            try:
                out.append(f"{int(str(b)[5:7])}月")
            except (TypeError, ValueError):
                out.append(str(b))
        return out
    if level == "month":
        return [str(b)[8:10] if len(str(b)) >= 10 else str(b) for b in buckets]
    if level == "day":
        return [f"{str(b)[:2]}:00" for b in buckets]
    return [str(b) for b in buckets]


def _period_years(conn, src: dict, now: date | None = None) -> dict:
    """「全部年份」周期：buckets = 有数据的年份（升序），区间覆盖首年到末年。"""
    rows = _periods_years(conn, src)
    ys = sorted({int(str(r["value"])) for r in rows if str(r.get("value", "")).isdigit()})
    if not ys:
        ys = [(now or _today()).year]
    return {
        "level": "years",
        "key": "years",
        "label": "全部年份",
        "start": date(ys[0], 1, 1),
        "end": date(ys[-1] + 1, 1, 1),
        "buckets": [f"{y:04d}" for y in ys],
    }


def _years_series(conn, src: dict, years: list[str], matrix: list[dict]) -> dict:
    """主图第一级：每年一根柱（三源 + 未计量），房间按年堆叠。"""
    yset = [int(y) for y in years]
    start = date(yset[0], 1, 1)
    end = date(yset[-1] + 1, 1, 1)
    grid_map = _grid_daily(conn, src["day_table"], src["grid_entity"], start, end)
    rt_daily, _ = _realtime_daily(conn, src["env_table"], src["env_entity"], start, end)

    dev_by_year: dict[str, float] = {}
    room_by_year: dict[str, dict[str, float]] = {}
    for r in matrix:
        y = str(r["day"])[:4]
        dev_by_year[y] = dev_by_year.get(y, 0.0) + r["kwh"]
        room_map = room_by_year.setdefault(r["room"], {})
        room_map[y] = room_map.get(y, 0.0) + r["kwh"]

    grid_vals: list[float | None] = []
    rt_vals: list[float | None] = []
    dev_vals: list[float] = []
    unmetered: list[float | None] = []
    for y in years:
        g = _grid_year_value(conn, src["year_table"], src["grid_entity"], int(y))
        if g is None:
            g = sum(v for d, v in grid_map.items() if str(d).startswith(f"{y}-"))
        grid_vals.append(_r(g) if g else None)
        rt = sum(v for d, v in rt_daily.items() if str(d).startswith(f"{y}-"))
        rt_vals.append(_r(rt) if rt else None)
        dev = _r(dev_by_year.get(y, 0.0))
        dev_vals.append(dev)
        unmetered.append(_r(rt - dev) if (rt or dev) else None)

    return {
        "buckets": list(years),
        "grid": grid_vals,
        "realtime": rt_vals,
        "device": dev_vals,
        "unmetered": unmetered,
        "by_room": {
            room: [_r(mapping.get(y, 0.0)) for y in years]
            for room, mapping in room_by_year.items()
        },
        "by_device": {},
        "samples": [],
    }


def _ranking_items(pairs: list[tuple], limit: int) -> list[dict]:
    """(key, name, sub, value) → 排行项（已带 text/pct/color，前端直接用）。"""
    total = sum(max(0.0, float(p[3] or 0)) for p in pairs)
    items = []
    for key, name, sub, value in pairs:
        v = float(value or 0)
        if v <= 0:
            continue
        items.append({
            "key": str(key or ""),
            "name": str(name or ""),
            "sub": str(sub or ""),
            "value": _r(v),
            "text": _fmt_num(v),
            "pct": _fmt_pct(_ratio(v, total)),
            "color": _room_color(str(sub or name or "")),
        })
    items.sort(key=lambda x: x["value"], reverse=True)
    return items[:limit] if limit > 0 else items


def _build_hourly_charts(series: dict, limit: int) -> dict:
    """日视图的 24 份「房间/设备」排行：时级下钻前端直接取，不再加工。"""
    by_room = series.get("by_room") or {}
    by_dev = series.get("by_device") or {}
    if not by_room and not by_dev:
        return {}
    out: dict = {}
    for h in range(24):
        room_pairs = [
            (room, room, "", (arr[h] if h < len(arr) else 0.0))
            for room, arr in by_room.items()
        ]
        dev_pairs = []
        for d in by_dev.values():
            buckets = d.get("buckets") or []
            dev_pairs.append((
                d.get("entity_id"), d.get("name"), d.get("room"),
                (buckets[h] if h < len(buckets) else 0.0),
            ))
        hour_total = _r(sum(max(0.0, float(p[3] or 0)) for p in room_pairs))
        total_block = {"value": hour_total, "text": f"{_fmt_num(hour_total)} kWh"}
        out[f"{h:02d}"] = {
            "room": {
                "kind": "ranking", "items": _ranking_items(room_pairs, limit),
                "select": "room", "total": total_block,
            },
            "device": {
                "kind": "ranking", "items": _ranking_items(dev_pairs, limit),
                "select": "device", "total": total_block,
            },
        }
    return out


def _build_charts(out: dict, per: dict, top: int) -> dict:
    """把 totals / series / room_series 转成三块图的渲染 spec。"""
    series = out.get("series") or {}
    room_series = out.get("room_series") or {}
    buckets = series.get("buckets") or per.get("buckets") or []
    level = per.get("level") or "day"
    labels = _axis_labels(buckets, level)

    # ---- ① 主图：房间堆叠柱 + 未计量 + 电网/实时折线 ----
    main_series: list[dict] = []
    for room, arr in room_series.items():
        main_series.append({
            "name": room,
            "type": "bar",
            "stack": "kwh",
            "color": _room_color(room),
            "data": arr,
        })
    unmetered = series.get("unmetered")
    if unmetered:
        cleaned = [None if v is None else max(0.0, float(v)) for v in unmetered]
        # 全 0 / 全 None 的"未计量"不画（跨年时实时与设备覆盖不同，常出现整列 0）
        if any(v for v in cleaned):
            main_series.append({
                "name": "未计量",
                "type": "bar",
                "stack": "kwh",
                "color": COLOR_UNMETERED,
                "data": cleaned,
            })
    if series.get("grid"):
        main_series.append({
            "name": "电网",
            "type": "line",
            "smooth": True,          # 曲线（用户要求，不用直线段折线）
            "color": COLOR_GRID,
            "data": series["grid"],
        })
    if series.get("realtime"):
        main_series.append({
            "name": "实时",
            "type": "line",
            "smooth": True,
            "color": COLOR_REALTIME,
            "data": series["realtime"],
        })

    drill = {"years": "year", "year": "month", "month": "day", "day": "hour"}.get(level, "")
    main_chart = {
        "kind": "axis-stack",
        "xAxis": labels,
        "keys": list(buckets),
        "series": main_series,
        "drill": drill,
    }

    # ---- ② 房间图：排行 ----
    rooms = out.get("rooms") or []
    room_items = [
        {
            "key": r["room"],
            "name": r["room"],
            "value": r["kwh"],
            "text": _fmt_num(r["kwh"]),
            "pct": _fmt_pct(r.get("ratio")),
            "color": _room_color(r["room"]),
        }
        for r in rooms[: max(1, top)]
    ]
    # 合计（区域标题右端显示）：房间图是全量房间，合计 = 设备计量总量
    dev_total = (out.get("totals") or {}).get("device", {}).get("kwh") or 0.0
    total_block = {"value": _r(dev_total), "text": f"{_fmt_num(dev_total)} kWh"}
    room_chart = {
        "kind": "ranking", "items": room_items, "select": "room",
        "total": total_block,
    }

    # ---- ③ 设备图：排行（顶层设备，带房间后缀） ----
    devices = out.get("devices") or []
    dev_items = [
        {
            "key": d["entity_id"],
            "name": d["name"],
            "sub": d.get("room") or "",
            "value": d["kwh"],
            "text": _fmt_num(d["kwh"]),
            "pct": _fmt_pct(d.get("ratio")),
            "color": _room_color(d.get("room") or d["name"]),
        }
        for d in devices[: max(1, top)]
    ]
    device_chart = {
        "kind": "ranking", "items": dev_items, "select": "device",
        # 设备列表被 top 截断，合计仍给总量（与房间图一致），不是 items 之和
        "total": total_block,
    }

    charts = {"main": main_chart, "room": room_chart, "device": device_chart}
    # 日视图：把 24 小时的房间/设备排行也一并算好，时级下钻前端零加工
    if level == "day":
        charts["hourly"] = _build_hourly_charts(series, max(1, top))
    return charts


def _build_header(out: dict, level: str = "") -> dict:
    t = out.get("totals") or {}
    primary = t.get("primary") or {}
    gaps = t.get("gaps") or {}
    source = primary.get("source") or "none"
    badge = {"grid": "电网结算", "realtime": "实时估算", "device": "设备计量"}.get(source, "无数据")

    if level == "years":
        # 跨年时三源覆盖的年份不同（电网有 2016+，实时/设备只有近几年），
        # 直接算"电网差额"会得到 80%~90% 的假缺口 —— 这里改为显示覆盖年份
        series = out.get("series") or {}
        grid_years = sum(1 for v in (series.get("grid") or []) if v is not None)
        rt_years = sum(1 for v in (series.get("realtime") or []) if v is not None)
        stats = [
            {"label": "实时", "value": _fmt_num((t.get("realtime") or {}).get("kwh")), "tone": "normal"},
            {"label": "设备", "value": _fmt_num((t.get("device") or {}).get("kwh")), "tone": "normal"},
            {"label": "电网覆盖", "value": f"{grid_years} 年", "tone": "normal"},
            {"label": "实时覆盖", "value": f"{rt_years} 年", "tone": "normal"},
        ]
    else:
        stats = [
            {"label": "实时", "value": _fmt_num((t.get("realtime") or {}).get("kwh")), "tone": "normal"},
            {"label": "设备", "value": _fmt_num((t.get("device") or {}).get("kwh")), "tone": "normal"},
            {
                "label": "设备未计量",
                "value": _fmt_num(gaps.get("device_gap")),
                "extra": _fmt_pct(gaps.get("device_gap_ratio")),
                "tone": "warn",
            },
            {
                "label": "电网差额",
                "value": _fmt_num(gaps.get("grid_gap")),
                "extra": _fmt_pct(gaps.get("grid_gap_ratio")),
                "tone": "warn",
            },
        ]
    cmp_ = out.get("compare") or {}
    for label, obj in (("环比", cmp_.get("prev")), ("同比", cmp_.get("yoy"))):
        if not obj or obj.get("delta_pct") is None:
            continue
        pct = float(obj["delta_pct"])
        stats.append({
            "label": label,
            "value": f"{pct * 100:+.1f}%",
            "tone": "up" if pct >= 0 else "down",
        })
    time_header = {
        "primary": {
            "value": _fmt_num(primary.get("kwh")),
            "unit": "kWh",
            "source": source,
            "badge": badge,
            "settled": bool(primary.get("settled")),
        },
        "stats": stats,
    }

    # 房间页：重点是"各房间的设备计量"，主口径换成设备合计（与房间图分母一致）
    rooms = out.get("rooms") or []
    dev = t.get("device") or {}
    top_room = rooms[0] if rooms else None
    room_header = {
        "primary": {
            "value": _fmt_num(dev.get("kwh")),
            "unit": "kWh",
            "source": "device",
            "badge": "设备计量",
            "settled": False,
        },
        "stats": [
            {"label": "房间数", "value": str(len(rooms)), "tone": "normal"},
            {"label": "设备数", "value": str(int(dev.get("entities") or 0)), "tone": "normal"},
            {
                "label": "最大房间",
                "value": top_room["room"] if top_room else "—",
                "extra": _fmt_pct(top_room.get("ratio")) if top_room else "",
                "tone": "normal",
            },
            {
                "label": "设备未计量",
                "value": _fmt_num(gaps.get("device_gap")),
                "extra": _fmt_pct(gaps.get("device_gap_ratio")),
                "tone": "warn",
            },
        ],
    }
    return {"time": time_header, "room": room_header}


def _build_crumb(per: dict, room: str, entity: str, entity_name: str = "") -> dict:
    """两条主轴各自独立的路径（时间页只含时间层级，房间页只含空间层级）。"""
    # ---- 时间页 ----
    time_crumb = [{"key": "home", "label": "🏠 全屋", "action": "home"}]
    level = per.get("level")
    if level == "years":
        time_crumb.append({"key": "years", "label": "全部年份", "current": True})
    else:
        if level in ("year", "month", "day"):
            time_crumb.append({
                "key": per["key"][:4], "label": f"{per['key'][:4]}年", "action": "year"
            })
        if level in ("month", "day"):
            mm = per["key"][5:7]
            time_crumb.append({
                "key": per["key"][:7], "label": f"{int(mm)}月", "action": "month"
            })
        if level == "day":
            time_crumb.append({"key": per["key"], "label": per["key"][5:], "action": "day"})
        if len(time_crumb) > 1 and time_crumb[-1].get("action"):
            last = dict(time_crumb[-1])
            last.pop("action", None)
            last["current"] = True
            time_crumb[-1] = last

    # ---- 房间页 ----
    room_crumb = [{"key": "home", "label": "🏠 全屋", "action": "home"}]
    if room:
        room_crumb.append({"key": f"room:{room}", "label": room, "action": "room"})
    if entity:
        room_crumb.append({
            "key": f"entity:{entity}", "label": entity_name or entity, "current": True
        })
    if len(room_crumb) == 1:
        # 房间首页：只有"全屋"且是当前级（不可点）
        room_crumb[0] = {"key": "home", "label": "🏠 全屋", "current": True}
    elif room_crumb[-1].get("action"):
        # 末级（房间 / 设备）是当前级，不可点；返回上一级点"全屋"
        last = dict(room_crumb[-1])
        last.pop("action", None)
        last["current"] = True
        room_crumb[-1] = last

    return {"time": time_crumb, "room": room_crumb}


def compute_power_dashboard_sync(
    db_path: str,
    level: str = "day",
    year: str = "",
    month: str = "",
    date: str = "",
    room: str = "",
    entity_id: str = "",
    source: str = "auto",
    day_table: str = "",
    month_table: str = "",
    year_table: str = "",
    env_entity: str = "",
    top: int = 20,
    parts: str = "",
) -> dict:
    """下钻主查询：一次返回当前级的总量、序列、房间/设备清单与房间三级合计。"""
    conn = _open(db_path)
    try:
        src = _resolve_sources(conn, day_table, month_table, year_table, "", env_entity)
        # level=years：主图第一级（每年一根柱），区间覆盖所有有数据的年份
        per = (_period_years(conn, src)
               if (level or "").strip().lower() == "years"
               else _period(level, year, month, date))

        want = {p.strip() for p in (parts or "").split(",") if p.strip()}
        all_parts = not want
        # 房间轴的「今日/本月/今年」会扫今年整年 device_history（实测 ~200ms），
        # 默认不返回；前端切到房间轴时显式 parts=room_totals 再取。
        need_room_totals = "room_totals" in want

        # ---- 预取：totals / series / rooms / devices 共用同一批结果 -------------
        # 早期实现里这三个视图各查一次设备表，年度多花约 370ms；现在只查一次。
        grid_map = _grid_daily(conn, src["day_table"], src["grid_entity"], per["start"], per["end"])
        rt_daily, rt_counts = _realtime_daily(
            conn, src["env_table"], src["env_entity"], per["start"], per["end"]
        )
        # 设备只扫一次（房间×实体×日），三种视图都在内存里聚合
        matrix = _device_matrix(
            conn, src["device_table"], per["start"], per["end"], room, entity_id
        )
        dev_rows = _matrix_to_entities(matrix)
        # 日级 series 走小时分摊，不需要按天聚合
        dev_daily_map: dict[str, float] | None = None
        room_days: dict[str, dict[str, float]] | None = None
        if per["level"] not in ("day", "years"):
            dev_daily_map = _matrix_to_days(matrix)
            room_days = _matrix_to_room_days(matrix)

        years_series: dict | None = None
        if per["level"] == "years":
            # 主图第一级：每年一根柱（年表优先 → 日表汇总 → 实时 → 设备）
            years_series = _years_series(conn, src, per["buckets"], matrix)
            grid_kwh = _r(sum(v for v in years_series["grid"] if v is not None))
            rt_kwh = _r(sum(v for v in years_series["realtime"] if v is not None))
            grid_src = "year_table" if src["year_table"] else "day_table"
            grid_day_sum = grid_kwh
            rt_samples = 0
            rt_days = 0
        else:
            grid_kwh, grid_src, grid_day_sum = _grid_total(conn, src, per, grid_map)
            rt_kwh = sum(rt_daily.values())
            rt_samples = sum(rt_counts.values())
            rt_days = len(rt_daily)
        dev_kwh = _r(sum(r["kwh"] for r in dev_rows))
        dev_entities = len({r["entity_id"] for r in dev_rows})
        dev_rooms = len({r["room"] for r in dev_rows})

        totals = {
            "primary": _primary(grid_kwh, rt_kwh, dev_kwh, source),
            "grid": {
                "kwh": _r(grid_kwh),
                "source": grid_src,
                "by_day_sum": _r(grid_day_sum),
                "days": len(grid_map),
            },
            "realtime": {"kwh": _r(rt_kwh), "samples": rt_samples, "coverage_days": rt_days},
            "device": {"kwh": dev_kwh, "entities": dev_entities, "rooms": dev_rooms},
            "gaps": _gaps(grid_kwh, rt_kwh, dev_kwh),
        }

        out: dict = {
            "period": {
                "level": per["level"],
                "key": per["key"],
                "label": per["label"],
                "start": _dstr(per["start"]),
                "end": _dstr(per["end"] - timedelta(days=1)),
                "buckets": len(per["buckets"]),
            },
            "scope": {"room": room, "entity_id": entity_id},
        }

        if all_parts or "totals" in want:
            out["totals"] = totals
            primary_src = totals["primary"]["source"]
            out["compare"] = {
                "prev": _compare(conn, src, per, totals["primary"]["kwh"], "prev", primary_src),
                "yoy": _compare(conn, src, per, totals["primary"]["kwh"], "yoy", primary_src),
            }

        if all_parts or "series" in want:
            if years_series is not None:
                out["series"] = years_series
            else:
                out["series"] = _build_series(
                    conn, src, per, room, entity_id,
                    grid_map=grid_map, rt_daily=rt_daily, dev_daily=dev_daily_map,
                )

        if all_parts or "room_series" in want:
            if years_series is not None:
                out["room_series"] = years_series.get("by_room") or {}
            else:
                out["room_series"] = _build_room_series(conn, src, per, room_days=room_days)

        if all_parts or "rooms" in want:
            by_room: dict[str, dict] = {}
            for r in dev_rows:
                bucket = by_room.setdefault(r["room"], {
                    "room": r["room"], "kwh": 0.0, "device_count": 0,
                    "top_device": "", "top_kwh": 0.0, "count": 0,
                })
                bucket["kwh"] += r["kwh"]
                bucket["device_count"] += 1
                bucket["count"] += r["count"]
                if r["kwh"] > bucket["top_kwh"]:
                    bucket["top_kwh"] = r["kwh"]
                    bucket["top_device"] = r["name"]
            rooms_out = sorted(by_room.values(), key=lambda x: x["kwh"], reverse=True)
            for item in rooms_out:
                item["kwh"] = _r(item["kwh"])
                item["ratio"] = _ratio(item["kwh"], dev_kwh)
                item.pop("top_kwh", None)
            # 0 电量的房间不进排行（占位会挤掉真正有用的行）
            out["rooms"] = [x for x in rooms_out if x["kwh"] > 0]

        if all_parts or "devices" in want:
            devices_out = []
            for r in dev_rows:
                if r["kwh"] <= 0:
                    continue        # 没有电量的设备（人在/门磁等）不进排行
                devices_out.append({
                    "entity_id": r["entity_id"],
                    "name": r["name"],
                    "room": r["room"],
                    "kwh": r["kwh"],
                    "ratio": _ratio(r["kwh"], dev_kwh),
                    "count": r["count"],
                })
            devices_out.sort(key=lambda x: x["kwh"], reverse=True)
            limit = int(top) if str(top).strip().isdigit() else 20
            out["devices"] = devices_out[:limit] if limit > 0 else devices_out
            out["device_total"] = len(devices_out)

        if need_room_totals:
            out["room_totals"] = _device_room_totals(conn, src["device_table"])

        if per["level"] == "day" and (all_parts or "hourly" in want):
            series = out.get("series") or _build_series(
                conn, src, per, room, entity_id,
                grid_map=grid_map, rt_daily=rt_daily, dev_daily=dev_daily_map,
            )
            out["hourly"] = {
                "buckets": series["buckets"],
                "realtime": series["realtime"],
                "device": series["device"],
                "by_room": series.get("by_room") or {},
                "by_device": series.get("by_device") or {},
                "samples": series.get("samples") or [],
            }

        out["meta"] = {
            "day_table": src["day_table"],
            "month_table": src["month_table"],
            "year_table": src["year_table"],
            "env_entity": src["env_entity"],
            "grid_entity": src["grid_entity"],
            "room": room,
        }

        # ---- 前端视图模型：前端只做渲染，不再聚合/格式化 ----
        limit = int(top) if str(top).strip().isdigit() else 20
        entity_label = next((r["name"] for r in dev_rows if r["entity_id"] == entity_id), "")
        out["state"] = {
            "level": per["level"],
            "key": per["key"],
            "label": per["label"],
            "start": _dstr(per["start"]),
            "end": _dstr(per["end"] - timedelta(days=1)),
            "buckets": len(per["buckets"]),
        }
        out["header"] = _build_header(out, per["level"])
        out["crumb"] = _build_crumb(per, room, entity_id, entity_label)
        out["charts"] = _build_charts(out, per, limit if limit > 0 else 20)
        return out
    finally:
        conn.close()


# ========================================================================== #
#  对外：power_breakdown                                                      #
# ========================================================================== #
def compute_power_breakdown_sync(
    db_path: str,
    group_by: str = "room",
    level: str = "month",
    year: str = "",
    month: str = "",
    date: str = "",
    start: str = "",
    end: str = "",
    room: str = "",
    entity_id: str = "",
    limit: int = 0,
    day_table: str = "",
    month_table: str = "",
    year_table: str = "",
    env_entity: str = "",
) -> dict:
    """维度明细：房间 / 设备 / 房间→设备；支持自定义 start~end 与单设备趋势。"""
    conn = _open(db_path)
    try:
        src = _resolve_sources(conn, day_table, month_table, year_table, "", env_entity)

        # 区间优先级：start/end > level 周期
        s_date = _parse_date(start)
        e_date = _parse_date(end)
        if s_date and e_date:
            start_d = s_date
            end_d = e_date + timedelta(days=1)
            per = {"level": "range", "key": f"{_dstr(s_date)}~{_dstr(e_date)}",
                   "label": f"{_dstr(s_date)} ~ {_dstr(e_date)}", "start": start_d,
                   "end": end_d, "buckets": []}
        else:
            per = _period(level, year, month, date)
            start_d, end_d = per["start"], per["end"]

        rows = _device_by_room_entity(conn, src["device_table"], start_d, end_d, room, entity_id)
        total = sum(r["kwh"] for r in rows)

        gb = (group_by or "room").strip().lower()
        items: list[dict] = []
        if gb == "device":
            for r in rows:
                items.append({
                    "entity_id": r["entity_id"], "name": r["name"], "room": r["room"],
                    "kwh": r["kwh"], "ratio": _ratio(r["kwh"], total), "count": r["count"],
                })
        elif gb == "room_device":
            by_room: dict[str, dict] = {}
            for r in rows:
                bucket = by_room.setdefault(r["room"], {"room": r["room"], "kwh": 0.0, "children": []})
                bucket["kwh"] += r["kwh"]
                bucket["children"].append({
                    "entity_id": r["entity_id"], "name": r["name"], "room": r["room"],
                    "kwh": r["kwh"], "ratio": _ratio(r["kwh"], total), "count": r["count"],
                })
            for bucket in by_room.values():
                bucket["kwh"] = _r(bucket["kwh"])
                bucket["ratio"] = _ratio(bucket["kwh"], total)
                bucket["children"].sort(key=lambda x: x["kwh"], reverse=True)
                items.append(bucket)
        else:
            by_room: dict[str, dict] = {}
            for r in rows:
                bucket = by_room.setdefault(r["room"], {
                    "room": r["room"], "kwh": 0.0, "count": 0,
                    "entities": set(),
                })
                bucket["kwh"] += r["kwh"]
                bucket["count"] += r["count"]
                bucket["entities"].add(r["entity_id"])
            for bucket in by_room.values():
                bucket["kwh"] = _r(bucket["kwh"])
                bucket["ratio"] = _ratio(bucket["kwh"], total)
                bucket["device_count"] = len(bucket.pop("entities"))
                items.append(bucket)

        items = [x for x in items if x.get("kwh", 0) > 0]
        items.sort(key=lambda x: x.get("kwh", 0), reverse=True)
        if limit and int(limit) > 0:
            items = items[: int(limit)]

        # 单设备趋势（给了 entity_id 时附带，省一次请求）
        series = None
        if entity_id:
            dev_daily = _device_by_day(conn, src["device_table"], start_d, end_d, room, entity_id)
            days = []
            cur = start_d
            while cur < end_d:
                days.append(_dstr(cur))
                cur += timedelta(days=1)
            series = {"buckets": days, "device": [_r(dev_daily.get(d, 0.0)) for d in days]}

        grid_kwh, grid_src, _ = _grid_total(conn, src, per)
        rt_kwh, _, _ = _realtime_total(conn, src, per)
        return {
            "group_by": gb,
            "range": {"start": _dstr(start_d), "end": _dstr(end_d - timedelta(days=1))},
            "items": items,
            "total_kwh": _r(total),
            "totals": {"grid": _r(grid_kwh), "realtime": _r(rt_kwh), "device": _r(total),
                       "grid_source": grid_src},
            "series": series,
        }
    finally:
        conn.close()


# ========================================================================== #
#  对外：power_reconcile                                                      #
# ========================================================================== #
def compute_power_query_sync(
    db_path: str,
    keyword: str = "",
    start: str = "",
    end: str = "",
    room: str = "",
    entity_id: str = "",
    limit: int = 50,
    day_table: str = "",
    month_table: str = "",
    year_table: str = "",
    env_entity: str = "",
) -> dict:
    """查询页：实体 id / 名称模糊查询 + 日期范围；给 entity_id 时附带逐日趋势。

    前端只渲染：items 已带 text/pct/color，trend 已带 xAxis 与色。
    """
    conn = _open(db_path)
    try:
        src = _resolve_sources(conn, day_table, month_table, year_table, "", env_entity)
        table = src["device_table"]
        today = _today()
        d_end = _parse_date(end) or today
        d_start = _parse_date(start) or (d_end - timedelta(days=29))
        if d_start > d_end:
            d_start, d_end = d_end, d_start
        end_excl = d_end + timedelta(days=1)

        where = [_DEVICE_ROOM_EXCLUDE, "on_time>=?", "on_time<?"]
        params: list = [_dstr(d_start), _dtstr(datetime.combine(end_excl, datetime.min.time()))]
        kw = (keyword or "").strip()
        if kw:
            where.append("(entity_id LIKE ? OR name LIKE ?)")
            like = f"%{kw}%"
            params.extend([like, like])
        if room:
            where.append("room=?")
            params.append(room)
        if entity_id:
            where.append("entity_id=?")
            params.append(entity_id)

        rows: list = []
        if table:
            sql = (
                f"SELECT room, entity_id, MAX(name) AS name, {_DEVICE_ENERGY_EXPR} AS kwh, "
                f"COUNT(*) AS cnt FROM {table} WHERE " + " AND ".join(where) +
                " GROUP BY room, entity_id"
            )
            try:
                rows = conn.execute(sql, tuple(params)).fetchall()
            except sqlite3.DatabaseError as exc:
                _LOGGER.warning("[power] 查询失败: %s", exc)
                rows = []

        total = _r(sum(_f(r["kwh"]) for r in rows))
        items_all = []
        for r in rows:
            kwh = _r(_f(r["kwh"]))
            if kwh <= 0:
                continue
            items_all.append({
                "entity_id": str(r["entity_id"] or ""),
                "name": str(r["name"] or r["entity_id"] or ""),
                "room": str(r["room"] or ""),
                "value": kwh,
                "text": _fmt_num(kwh),
                "pct": _fmt_pct(_ratio(kwh, total)),
                "count": int(r["cnt"] or 0),
                "color": _room_color(str(r["room"] or r["name"] or "")),
            })
        items_all.sort(key=lambda x: x["value"], reverse=True)
        lim = int(limit) if str(limit).strip().isdigit() else 50
        items = items_all[:lim] if lim > 0 else items_all

        # 逐日趋势：指定 entity_id 用它，否则自动取 Top1 设备（查询页只展示汇总 + 趋势）
        trend = None
        target = entity_id or (items[0]["entity_id"] if items else "")
        if target and table:
            matrix = _device_matrix(conn, table, d_start, end_excl, "", target)
            by_day: dict[str, float] = {}
            for r in matrix:
                by_day[r["day"]] = by_day.get(r["day"], 0.0) + r["kwh"]
            days: list[str] = []
            values: list[float | None] = []
            cur = d_start
            while cur <= d_end:
                key = _dstr(cur)
                days.append(key[5:])
                values.append(_r(by_day.get(key, 0.0)))
                cur += timedelta(days=1)
            name = items[0]["name"] if items else entity_id
            trend = {
                "kind": "line",
                "xAxis": days,
                "series": [{
                    "name": name,
                    "color": _room_color(items[0]["room"] if items else name),
                    "data": values,
                }],
            }

        days = max(1, (d_end - d_start).days + 1)
        return {
            "filters": {
                "keyword": kw,
                "start": _dstr(d_start),
                "end": _dstr(d_end),
                "room": room,
                "entity_id": entity_id,
            },
            # 查询页自己的指标（与时间/房间页的重点不同）
            "header": {
                "primary": {
                    "value": _fmt_num(total),
                    "unit": "kWh",
                    "source": "device",
                    "badge": "设备计量",
                    "settled": False,
                },
                "stats": [
                    {"label": "设备台数", "value": str(len(items_all)), "tone": "normal"},
                    {"label": "天数", "value": f"{days} 天", "tone": "normal"},
                    {"label": "日均", "value": _fmt_num(total / days), "tone": "normal"},
                    {
                        "label": "Top 设备",
                        "value": items[0]["name"] if items else "—",
                        "extra": items[0]["pct"] if items else "",
                        "tone": "normal",
                    },
                ],
            },
            "total": {"value": _fmt_num(total), "text": f"{_fmt_num(total)} kWh"},
            "items": items,
            "item_count": len(items_all),
            "trend": trend,
        }
    finally:
        conn.close()


def compute_power_reconcile_sync(
    db_path: str,
    start: str = "",
    end: str = "",
    day_table: str = "",
    month_table: str = "",
    year_table: str = "",
    env_entity: str = "",
    days: int = 30,
) -> dict:
    """逐日三源对账（默认最近 N 天）。"""
    conn = _open(db_path)
    try:
        src = _resolve_sources(conn, day_table, month_table, year_table, "", env_entity)
        e_date = _parse_date(end) or _today()
        s_date = _parse_date(start) or (e_date - timedelta(days=max(int(days or 30), 1) - 1))
        if s_date > e_date:
            s_date, e_date = e_date, s_date
        end_d = e_date + timedelta(days=1)

        grid_map = _grid_daily(conn, src["day_table"], src["grid_entity"], s_date, end_d)
        rt_map, _ = _realtime_daily(conn, src["env_table"], src["env_entity"], s_date, end_d)
        dev_map = _device_by_day(conn, src["device_table"], s_date, end_d)

        rows = []
        cur = s_date
        while cur < end_d:
            key = _dstr(cur)
            g = grid_map.get(key)
            rt = rt_map.get(key)
            dv = dev_map.get(key, 0.0)
            rows.append({
                "day": key,
                "grid": None if g is None else _r(g),
                "realtime": None if rt is None else _r(rt),
                "device": _r(dv),
                "grid_vs_realtime_pct": (None if (g is None or rt is None or g <= 0)
                                          else _ratio(g - rt, g)),
                "realtime_vs_device_pct": (None if (rt is None or rt <= 0)
                                            else _ratio(rt - dv, rt)),
            })
            cur += timedelta(days=1)

        grid_total = sum(r["grid"] for r in rows if r["grid"] is not None)
        rt_total = sum(r["realtime"] for r in rows if r["realtime"] is not None)
        dev_total = sum(r["device"] for r in rows)
        gp = [r["grid_vs_realtime_pct"] for r in rows if r["grid_vs_realtime_pct"] is not None]
        dp = [r["realtime_vs_device_pct"] for r in rows if r["realtime_vs_device_pct"] is not None]

        return {
            "start": _dstr(s_date),
            "end": _dstr(e_date),
            "rows": rows,
            "summary": {
                "days": len(rows),
                "grid_total": _r(grid_total),
                "realtime_total": _r(rt_total),
                "device_total": _r(dev_total),
                "avg_grid_vs_realtime_pct": _r(sum(gp) / len(gp), 4) if gp else None,
                "avg_realtime_vs_device_pct": _r(sum(dp) / len(dp), 4) if dp else None,
                "days_missing_grid": sum(1 for r in rows if r["grid"] is None),
                "days_missing_realtime": sum(1 for r in rows if r["realtime"] is None),
            },
        }
    finally:
        conn.close()


# ========================================================================== #
#  对外：power_periods                                                        #
# ========================================================================== #
def compute_power_periods_sync(
    db_path: str,
    level: str = "year",
    year: str = "",
    month: str = "",
    day_table: str = "",
    env_entity: str = "",
) -> dict:
    """数据可用性：年 → 月 → 日 三层（日历/下拉的置灰依据）。"""
    conn = _open(db_path)
    try:
        src = _resolve_sources(conn, day_table, "", "", "", env_entity)
        lv = (level or "year").strip().lower()
        if lv not in ("year", "month", "day"):
            lv = "year"

        if lv == "year":
            years = _periods_years(conn, src)
            return {"level": "year", "years": years, "total": len(years)}

        if lv == "month":
            y = int(year) if str(year).strip().isdigit() else _today().year
            months = _periods_months(conn, src, y)
            # 补成完整 12 个月，前端可直接铺满
            have = {m["value"]: m for m in months}
            full = []
            for i in range(1, 13):
                key = f"{y:04d}-{i:02d}"
                item = have.get(key) or {"value": key, "count": 0, "grid": False,
                                         "realtime": False, "device": False}
                full.append(item)
            return {"level": "month", "year": f"{y:04d}", "months": full,
                    "total": sum(1 for m in full if m["count"])}

        # day 层
        m = _parse_month(month) or date(_today().year, _today().month, 1)
        start = date(m.year, m.month, 1)
        end = _shift_months(start, 1)
        grid_map = _grid_daily(conn, src["day_table"], src["grid_entity"], start, end)
        rt_map, _ = _realtime_daily(conn, src["env_table"], src["env_entity"], start, end)
        dev_map = _device_by_day(conn, src["device_table"], start, end)

        dates = []
        cur = start
        filled = 0
        missing = 0
        while cur < end:
            key = _dstr(cur)
            g = key in grid_map
            rt = key in rt_map
            dv = dev_map.get(key, 0.0) > 0
            source = "grid" if g else ("realtime" if rt else "none")
            if source == "realtime":
                filled += 1
            if source == "none":
                missing += 1
            dates.append({
                "value": key,
                # 兼容既有 periods 结构（value + count）：这里的 count 只表达"这天有没有数据"
                "count": 1 if source != "none" else 0,
                "grid": g,
                "realtime": rt,
                "device": dv,
                "source": source,
                "filled_by_realtime": (source == "realtime"),
            })
            cur += timedelta(days=1)

        return {
            "level": "day",
            "month": f"{start.year:04d}-{start.month:02d}",
            "days_in_month": len(dates),
            "dates": dates,
            "source_summary": {
                "grid_days": sum(1 for d in dates if d["grid"]),
                "realtime_days": sum(1 for d in dates if d["realtime"]),
                "device_days": sum(1 for d in dates if d["device"]),
                "filled_by_realtime": filled,
                "missing": missing,
            },
        }
    finally:
        conn.close()


# ========================================================================== #
#  对外：power_calendar                                                       #
# ========================================================================== #
def compute_power_calendar_sync(
    db_path: str,
    month: str = "",
    day_table: str = "",
    env_entity: str = "",
    room: str = "",
    include_device: bool = True,
) -> dict:
    """指定年月每日用电量：电网优先 → env_power 当天最后一条 → 无数据。"""
    conn = _open(db_path)
    try:
        src = _resolve_sources(conn, day_table, "", "", "", env_entity)
        m = _parse_month(month) or date(_today().year, _today().month, 1)
        start = date(m.year, m.month, 1)
        end = _shift_months(start, 1)

        grid_map = _grid_daily(conn, src["day_table"], src["grid_entity"], start, end)
        rt_map, _ = _realtime_daily(conn, src["env_table"], src["env_entity"], start, end)
        dev_map = _device_by_day(conn, src["device_table"], start, end, room) if include_device else {}

        items = []
        max_kwh = 0.0
        filled = 0
        missing = 0
        cur = start
        while cur < end:
            key = _dstr(cur)
            g = grid_map.get(key)
            rt = rt_map.get(key)
            dv = dev_map.get(key, 0.0)
            if g is not None:
                kwh, source, settled = g, "grid", True
            elif rt is not None:
                kwh, source, settled = rt, "realtime", False
                filled += 1
            else:
                kwh, source, settled = None, "none", False
                missing += 1
            if kwh is not None:
                max_kwh = max(max_kwh, kwh)
            items.append({
                "day": key,
                "kwh": None if kwh is None else _r(kwh, 2),
                "source": source,
                "settled": settled,
                "grid": None if g is None else _r(g, 2),
                "realtime": None if rt is None else _r(rt, 2),
                "device": _r(dv, 2),
                "filled_by_realtime": source == "realtime",
                "has_data": kwh is not None,
            })
            cur += timedelta(days=1)

        grid_total = sum(v for v in grid_map.values())
        rt_total = sum(v for v in rt_map.values())
        dev_total = sum(v for v in dev_map.values())
        return {
            "month": f"{start.year:04d}-{start.month:02d}",
            "days_in_month": len(items),
            "today": _dstr(_today()),
            "max_kwh": _r(max_kwh, 2),
            "items": items,
            "summary": {
                "grid_total": _r(grid_total),
                "grid_days": len(grid_map),
                "realtime_total": _r(rt_total),
                "realtime_days": len(rt_map),
                "device_total": _r(dev_total),
                "filled_by_realtime": filled,
                "missing": missing,
            },
        }
    finally:
        conn.close()
