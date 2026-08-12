"""打印机数据采集模块 — 独立模块。

职责：
  - 建表 printer_configs / printer_daily（单张主记录表，当日明细存 printer_jobs JSON 字段）
  - state_changed 事件采集（last_changed 变化时写入）
  - 配置 CRUD + 打印数据查询 API

数据来源（两个可配实体）→ 单张主记录表 printer_daily（每天一条记录）：

① 统计数据实体（如 sensor.hp_printer_yong_liang_tong_ji）：
  state                          → 当前日期（YYYY-MM-DD）
  attributes.daylist             → 每日汇总数组（每项含 day/print/scan/copy/fax/jam_printer
                                     /ink_black/ink_cyan/ink_magenta/ink_yellow）
  采集：遍历 daylist，按 (name, day) upsert 汇总数据 + 墨量

② 当日详细数据实体（如 sensor.hp_printer_jin_ri_zuo_ye）：
  state                          → 当日作业总数（每次打印变化）
  attributes.date                → 作业日期（YYYY-MM-DD）
  attributes.print/scan/copy/fax/jam_printer → 当日各类型作业明细数组
  采集：把当日完整 attributes（含各类型明细、total、date）以 JSON 整体写入
        当日记录的 printer_jobs 字段；同时从各类型明细统计当日汇总字段
        （print/scan/copy/fax/jam_printer，随打印次数变化更新），并从统计数据实体
        当前状态提取当日墨量（ink_*）。当日打印数量变化时（last_changed 变化），
        整体覆盖更新当日记录，保证当日汇总 + 墨量 + 明细始终为最新。

采集触发（last_changed 判断）：
  统计实体 state 是当日日期，当日内 state 值不变；详细实体每次作业 state 总数变化。
  统一以 last_changed 时间是否变化作为"有新数据"的判定（old.last_changed != new.last_changed）。
  同时配置保存时会主动全量采集一次，避免空窗期。
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .logger import get_logger
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# 表名（模块内私有常量，不污染 const.py）
TABLE_PRINTER_CONFIGS = "printer_configs"
TABLE_PRINTER_DAILY = "printer_daily"

# 墨量字段（跟随 daylist 每条记录存储）
_INK_FIELDS = ("ink_black", "ink_cyan", "ink_magenta", "ink_yellow")
# 计数字段（参与合计/求和）
_COUNT_FIELDS = ("print", "scan", "copy", "fax", "jam_printer")


# =========================================================================== #
#  数据库初始化                                                                  #
# =========================================================================== #
def init_database(db_path: str) -> None:
    """建表 + 迁移。由 __init__ 调用（独立连接）。"""
    conn = sqlite3.connect(db_path)
    local_logger = get_logger()
    try:
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE_PRINTER_CONFIGS} (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                name          TEXT NOT NULL DEFAULT '',
                stats_entity  TEXT NOT NULL DEFAULT '',
                detail_entity TEXT NOT NULL DEFAULT '',
                enabled       INTEGER NOT NULL DEFAULT 1,
                created_at    TEXT NOT NULL DEFAULT '',
                updated_at    TEXT NOT NULL DEFAULT '',
                UNIQUE(name)
            )
            """
        )
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE_PRINTER_DAILY} (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                name         TEXT NOT NULL DEFAULT '',
                day          TEXT NOT NULL DEFAULT '',
                print        INTEGER NOT NULL DEFAULT 0,
                scan         INTEGER NOT NULL DEFAULT 0,
                copy         INTEGER NOT NULL DEFAULT 0,
                fax          INTEGER NOT NULL DEFAULT 0,
                jam_printer  INTEGER NOT NULL DEFAULT 0,
                ink_black    TEXT NOT NULL DEFAULT '',
                ink_cyan     TEXT NOT NULL DEFAULT '',
                ink_magenta  TEXT NOT NULL DEFAULT '',
                ink_yellow   TEXT NOT NULL DEFAULT '',
                printer_jobs TEXT NOT NULL DEFAULT '',
                created_at   TEXT NOT NULL DEFAULT '',
                updated_at   TEXT NOT NULL DEFAULT '',
                UNIQUE(name, day)
            )
            """
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_printer_daily_pd "
            f"ON {TABLE_PRINTER_DAILY} (name, day)"
        )
        # 迁移①：配置表 name 需为唯一键（旧版是 UNIQUE(stats_entity)），否则 ON CONFLICT(name) 无效
        if not _configs_name_unique(conn):
            _migrate_configs_to_name_unique(conn)
        # 迁移②：检测旧版 printer_daily 表（含 printer_id 列、无 name 列），迁移为 name 结构
        cols = conn.execute(f"PRAGMA table_info({TABLE_PRINTER_DAILY})").fetchall()
        col_names = {c[1] for c in cols}
        if "name" not in col_names and "printer_id" in col_names:
            _migrate_daily_to_name(conn)
            cols = conn.execute(f"PRAGMA table_info({TABLE_PRINTER_DAILY})").fetchall()
            col_names = {c[1] for c in cols}
        if "printer_jobs" not in col_names:
            conn.execute(
                f"ALTER TABLE {TABLE_PRINTER_DAILY} ADD COLUMN printer_jobs TEXT NOT NULL DEFAULT ''"
            )
        if "updated_at" not in col_names:
            conn.execute(
                f"ALTER TABLE {TABLE_PRINTER_DAILY} ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''"
            )
        # 迁移：旧版独立的 printer_jobs 明细表已并入 printer_daily.printer_jobs JSON 字段，
        # 删除残留旧表（避免混淆）
        old_jobs = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='printer_jobs'"
        ).fetchone()
        if old_jobs:
            conn.execute("DROP TABLE IF EXISTS printer_jobs")
        conn.commit()
        if local_logger:
            local_logger.info("[printer] 打印机表结构已就绪")
    finally:
        conn.close()


def _configs_name_unique(conn) -> bool:
    """检测 printer_configs 表的 name 列是否已有唯一约束。"""
    try:
        indexes = conn.execute(
            f"PRAGMA index_list({TABLE_PRINTER_CONFIGS})"
        ).fetchall()
        for idx in indexes:
            # idx 结构: (seq, name, unique, origin, partial)
            if idx[2] != 1:  # 非唯一索引
                continue
            cols = conn.execute(
                f"PRAGMA index_info({idx[1]})"
            ).fetchall()
            if len(cols) == 1 and cols[0][2] == "name":
                return True
        return False
    except sqlite3.OperationalError:
        return False


def _migrate_configs_to_name_unique(conn) -> None:
    """迁移：重建 printer_configs 表，使 name 为唯一键（旧版为 UNIQUE(stats_entity)）。"""
    old_rows = conn.execute(
        f"SELECT id, name, stats_entity, detail_entity, enabled, created_at, updated_at "
        f"FROM {TABLE_PRINTER_CONFIGS}"
    ).fetchall()
    conn.execute(f"DROP TABLE {TABLE_PRINTER_CONFIGS}")
    conn.execute(
        f"""
        CREATE TABLE {TABLE_PRINTER_CONFIGS} (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT NOT NULL DEFAULT '',
            stats_entity  TEXT NOT NULL DEFAULT '',
            detail_entity TEXT NOT NULL DEFAULT '',
            enabled       INTEGER NOT NULL DEFAULT 1,
            created_at    TEXT NOT NULL DEFAULT '',
            updated_at    TEXT NOT NULL DEFAULT '',
            UNIQUE(name)
        )
        """
    )
    for r in old_rows:
        # r: id, name, stats_entity, detail_entity, enabled, created_at, updated_at
        conn.execute(
            f"INSERT OR IGNORE INTO {TABLE_PRINTER_CONFIGS} "
            f"(name, stats_entity, detail_entity, enabled, created_at, updated_at) "
            f"VALUES (?, ?, ?, ?, ?, ?)",
            (r[1], r[2], r[3], r[4], r[5], r[6]),
        )


def _migrate_daily_to_name(conn) -> None:
    """迁移：旧 printer_daily 表（printer_id）迁移为 name 结构。

    旧表以 printer_id 关联 printer_configs，迁移时通过配置 id 反查 name。
    迁移成功后重建表（保留汇总/墨量/JSON 数据）。
    """
    # 收集旧数据：printer_id -> 该打印机配置的 name
    config_names = dict(conn.execute(
        f"SELECT id, name FROM {TABLE_PRINTER_CONFIGS}"
    ).fetchall())
    old_rows = conn.execute(
        f"SELECT printer_id, day, print, scan, copy, fax, jam_printer, "
        f"ink_black, ink_cyan, ink_magenta, ink_yellow, printer_jobs, created_at, updated_at "
        f"FROM {TABLE_PRINTER_DAILY}"
    ).fetchall()
    conn.execute(f"DROP TABLE {TABLE_PRINTER_DAILY}")
    conn.execute(
        f"""
        CREATE TABLE {TABLE_PRINTER_DAILY} (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            name         TEXT NOT NULL DEFAULT '',
            day          TEXT NOT NULL DEFAULT '',
            print        INTEGER NOT NULL DEFAULT 0,
            scan         INTEGER NOT NULL DEFAULT 0,
            copy         INTEGER NOT NULL DEFAULT 0,
            fax          INTEGER NOT NULL DEFAULT 0,
            jam_printer  INTEGER NOT NULL DEFAULT 0,
            ink_black    TEXT NOT NULL DEFAULT '',
            ink_cyan     TEXT NOT NULL DEFAULT '',
            ink_magenta  TEXT NOT NULL DEFAULT '',
            ink_yellow   TEXT NOT NULL DEFAULT '',
            printer_jobs TEXT NOT NULL DEFAULT '',
            created_at   TEXT NOT NULL DEFAULT '',
            updated_at   TEXT NOT NULL DEFAULT '',
            UNIQUE(name, day)
        )
        """
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_printer_daily_pd "
        f"ON {TABLE_PRINTER_DAILY} (name, day)"
    )
    for r in old_rows:
        pid, day = r[0], r[1]
        name = config_names.get(pid, "")
        if not name:
            continue
        conn.execute(
            f"INSERT OR IGNORE INTO {TABLE_PRINTER_DAILY} "
            f"(name, day, print, scan, copy, fax, jam_printer, "
            f"ink_black, ink_cyan, ink_magenta, ink_yellow, printer_jobs, created_at, updated_at) "
            f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (name, day, r[2], r[3], r[4], r[5], r[6],
             r[7], r[8], r[9], r[10], r[11], r[12], r[13]),
        )


# =========================================================================== #
#  监听集合                                                                      #
# =========================================================================== #
def get_monitored_entities(db_path: str) -> set[str]:
    """返回需要监听 state_changed 的 printer 实体集合。

    并入 __init__._refresh_monitored_set_sync 的总白名单。
    """
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            f"SELECT stats_entity, detail_entity FROM {TABLE_PRINTER_CONFIGS} "
            f"WHERE enabled = 1"
        ).fetchall()
        entities: set[str] = set()
        for r in rows:
            if r[0]:
                entities.add(r[0])
            if r[1]:
                entities.add(r[1])
        return entities
    except sqlite3.OperationalError:
        return set()
    finally:
        conn.close()


def get_printer_entities(db_path: str) -> dict:
    """返回 {entity_id: {"name": 名称, "type": "stats"|"detail", "stats_entity": ...}} 映射。

    供 __init__._async_state_changed O(1) 识别实体属于哪个打印机配置。
    存入 hass.data[DOMAIN]["printer_entities"]。
    """
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            f"SELECT name, stats_entity, detail_entity FROM {TABLE_PRINTER_CONFIGS} "
            f"WHERE enabled = 1"
        ).fetchall()
        mapping: dict = {}
        for name, stats, detail in rows:
            if stats:
                mapping[stats] = {
                    "name": name, "type": "stats",
                    "stats_entity": stats, "detail_entity": detail,
                }
            if detail:
                mapping[detail] = {
                    "name": name, "type": "detail",
                    "stats_entity": stats, "detail_entity": detail,
                }
        return mapping
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()


# =========================================================================== #
#  采集处理                                                                      #
# =========================================================================== #
def _to_int(value, default: int = 0) -> int:
    """把可能为 '3' / 3 / None 的值转为 int。"""
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(str(value).strip())
    except (ValueError, TypeError):
        return default


def _collect_stats_sync(conn, name: str, new_state) -> int:
    """解析统计数据实体，upsert 到 printer_daily。返回写入/更新的条数。

    只更新汇总字段 + 墨量 + updated_at；不覆盖 printer_jobs（当日明细由 detail 实体维护）。
    """
    attrs = getattr(new_state, "attributes", None) or {}
    daylist = attrs.get("daylist")
    if not isinstance(daylist, list):
        return 0
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    written = 0
    for item in daylist:
        if not isinstance(item, dict):
            continue
        day = str(item.get("day", "")).strip()
        if not day:
            continue
        params = {
            "name": name,
            "day": day,
            "print": _to_int(item.get("print")),
            "scan": _to_int(item.get("scan")),
            "copy": _to_int(item.get("copy")),
            "fax": _to_int(item.get("fax")),
            "jam_printer": _to_int(item.get("jam_printer")),
            "ink_black": str(item.get("ink_black") or ""),
            "ink_cyan": str(item.get("ink_cyan") or ""),
            "ink_magenta": str(item.get("ink_magenta") or ""),
            "ink_yellow": str(item.get("ink_yellow") or ""),
            "created_at": now_str,
            "updated_at": now_str,
        }
        conn.execute(
            f"INSERT INTO {TABLE_PRINTER_DAILY} "
            f"(name, day, print, scan, copy, fax, jam_printer, "
            f"ink_black, ink_cyan, ink_magenta, ink_yellow, created_at, updated_at) "
            f"VALUES (:name, :day, :print, :scan, :copy, :fax, :jam_printer, "
            f":ink_black, :ink_cyan, :ink_magenta, :ink_yellow, :created_at, :updated_at) "
            f"ON CONFLICT(name, day) DO UPDATE SET "
            f"print = excluded.print, scan = excluded.scan, copy = excluded.copy, "
            f"fax = excluded.fax, jam_printer = excluded.jam_printer, "
            f"ink_black = excluded.ink_black, ink_cyan = excluded.ink_cyan, "
            f"ink_magenta = excluded.ink_magenta, ink_yellow = excluded.ink_yellow, "
            f"updated_at = excluded.updated_at",
            params,
        )
        written += 1
    return written


def _sum_job_counts(items) -> int:
    """汇总各类型作业明细数组的 count 之和（当日该类型作业数）。"""
    if not isinstance(items, list):
        return 0
    total = 0
    for item in items:
        if isinstance(item, dict):
            total += _to_int(item.get("count"))
        else:
            total += 1
    return total


def _collect_detail_sync(conn, name: str, new_state, stats_state=None) -> int:
    """解析当日详细数据实体，更新当日记录。

    - 把完整 attributes 以 JSON 整体覆盖写入 printer_jobs 字段（当日明细最新）
    - 从各 type 数组统计当日汇总字段（print/scan/copy/fax/jam_printer，随打印次数变化更新）
    - 从统计数据实体当前状态提取当日墨量（ink_*）
    当日记录不存在时自动创建。返回写入/更新的条数。
    """
    attrs = getattr(new_state, "attributes", None) or {}
    date = str(attrs.get("date", "")).strip()
    if not date:
        return 0
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 当日汇总：各类型作业明细 count 求和（当日打印不是只打一次，需随变化更新）
    daily_counts = {
        jtype: _sum_job_counts(attrs.get(jtype))
        for jtype in ("print", "scan", "copy", "fax", "jam_printer")
    }
    # 当日墨量：从统计数据实体当前状态 daylist 中提取当日 ink_*
    ink = _extract_ink_for_day(stats_state, date)

    # 当日详细数据 JSON：仅保留 attributes 中的业务节点
    jobs_payload = {
        "date": date,
        "total": _to_int(attrs.get("total")),
        "print": attrs.get("print", []),
        "scan": attrs.get("scan", []),
        "copy": attrs.get("copy", []),
        "fax": attrs.get("fax", []),
        "jam_printer": attrs.get("jam_printer", []),
    }
    try:
        jobs_json = json.dumps(jobs_payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        jobs_json = ""

    # 当日记录不存在时 INSERT，存在时更新 汇总字段 + 墨量 + printer_jobs + updated_at
    conn.execute(
        f"INSERT INTO {TABLE_PRINTER_DAILY} "
        f"(name, day, print, scan, copy, fax, jam_printer, "
        f"ink_black, ink_cyan, ink_magenta, ink_yellow, printer_jobs, created_at, updated_at) "
        f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        f"ON CONFLICT(name, day) DO UPDATE SET "
        f"print = excluded.print, scan = excluded.scan, copy = excluded.copy, "
        f"fax = excluded.fax, jam_printer = excluded.jam_printer, "
        f"ink_black = excluded.ink_black, ink_cyan = excluded.ink_cyan, "
        f"ink_magenta = excluded.ink_magenta, ink_yellow = excluded.ink_yellow, "
        f"printer_jobs = excluded.printer_jobs, updated_at = excluded.updated_at",
        (
            name, date,
            daily_counts["print"], daily_counts["scan"],
            daily_counts["copy"], daily_counts["fax"], daily_counts["jam_printer"],
            ink["ink_black"], ink["ink_cyan"], ink["ink_magenta"], ink["ink_yellow"],
            jobs_json, now_str, now_str,
        ),
    )
    return 1


def _extract_ink_for_day(stats_state, date: str) -> dict:
    """从统计数据实体当前状态的 daylist 中提取指定日期的墨量（ink_*）。

    返回 {"ink_black":..., "ink_cyan":..., "ink_magenta":..., "ink_yellow":...}
    """
    result = {f: "" for f in ("ink_black", "ink_cyan", "ink_magenta", "ink_yellow")}
    if not stats_state:
        return result
    attrs = getattr(stats_state, "attributes", None) or {}
    daylist = attrs.get("daylist")
    if not isinstance(daylist, list):
        return result
    for item in daylist:
        if not isinstance(item, dict):
            continue
        if str(item.get("day", "")).strip() == date:
            for f in result:
                result[f] = str(item.get(f) or "")
            break
    return result


def handle_state_changed_sync(db_path: str, entity_id: str, new_state, meta: dict, partner_state=None) -> bool:
    """同步处理 state_changed 事件，写入打印机数据。

    meta: {"name": 名称, "type": "stats"|"detail", ...}
    partner_state: 可选，配套实体当前状态。detail 实体触发时传入统计数据实体状态，
                   用于提取当日墨量（ink_*）。

    返回 True 表示已处理，False 表示未命中/未处理。
    """
    if not new_state or not meta:
        return False
    name = meta.get("name")
    etype = meta.get("type")
    if not name or etype not in ("stats", "detail"):
        return False

    local_logger = get_logger()
    conn = sqlite3.connect(db_path)
    try:
        if etype == "stats":
            written = _collect_stats_sync(conn, name, new_state)
            tag = "统计"
        else:
            written = _collect_detail_sync(conn, name, new_state, partner_state)
            tag = "明细"
        conn.commit()
        if local_logger and written:
            local_logger.info(
                "[printer] 采集%s name=%s entity_id=%s 写入/更新 %d 条",
                tag, name, entity_id, written,
            )
        return True
    except sqlite3.IntegrityError:
        pass
    except Exception:
        local_logger = get_logger()
        if local_logger:
            local_logger.exception("[printer] 采集异常 entity_id=%s", entity_id)
    finally:
        conn.close()
    return True


def collect_printer_sync(db_path: str, name: str, stats_state, detail_state) -> dict:
    """主动采集：配置保存时调用，读取当前两个实体并落库。

    stats_state / detail_state 可为 None（未提供对应实体时跳过）。
    返回 {"stats_written": N, "detail_written": N}
    """
    result = {"stats_written": 0, "detail_written": 0}
    conn = sqlite3.connect(db_path)
    try:
        if stats_state is not None:
            result["stats_written"] = _collect_stats_sync(conn, name, stats_state)
        if detail_state is not None:
            result["detail_written"] = _collect_detail_sync(conn, name, detail_state, stats_state)
        conn.commit()
    finally:
        conn.close()
    return result


# =========================================================================== #
#  查询函数（供万能查询 type=printer_* 调用）                                     #
# =========================================================================== #
def query_printer_years(db_path: str, name: str) -> dict:
    """打印数据查询①：打印机有哪些年数据。返回 {"years": [...]}"""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            f"SELECT DISTINCT substr(day, 1, 4) AS y FROM {TABLE_PRINTER_DAILY} "
            f"WHERE name = ? AND day != '' ORDER BY y DESC",
            (name,),
        ).fetchall()
        return {"years": [r[0] for r in rows]}
    finally:
        conn.close()


def query_printer_month_dates(db_path: str, name: str, month: str) -> dict:
    """打印数据查询②：指定月哪些日期有数据（daylist 落库的日期）。
    month 格式：YYYY-MM。返回 {"month": "2026-08", "dates": ["2026-08-01", ...]}
    """
    if not month:
        raise ValueError("printer_month_dates 需要 month 参数(格式 YYYY-MM)")
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            f"SELECT DISTINCT day FROM {TABLE_PRINTER_DAILY} "
            f"WHERE name = ? AND day LIKE ? ORDER BY day ASC",
            (name, month + "%"),
        ).fetchall()
        return {"month": month, "dates": [r[0] for r in rows]}
    finally:
        conn.close()


def query_printer_total(db_path: str, name: str) -> dict:
    """打印数据查询③：打印机合计数据（基于数据库实际落库的 daylist 求和）。

    返回 {"total": {print, scan, copy, fax, jam_printer}}
    """
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            f"SELECT COALESCE(SUM(print),0) AS print, COALESCE(SUM(scan),0) AS scan, "
            f"COALESCE(SUM(copy),0) AS copy, COALESCE(SUM(fax),0) AS fax, "
            f"COALESCE(SUM(jam_printer),0) AS jam_printer, "
            f"COUNT(*) AS days FROM {TABLE_PRINTER_DAILY} WHERE name = ?",
            (name,),
        ).fetchone()
        return {
            "total": {
                "print": row[0], "scan": row[1], "copy": row[2],
                "fax": row[3], "jam_printer": row[4],
            },
            "days": row[5],
        }
    finally:
        conn.close()


def query_printer_monthly_total(db_path: str, name: str) -> dict:
    """打印数据查询⑥：按年月统计合计数据。

    按 day 的年月(YYYY-MM)分组，SUM 各计数 + 天数。
    返回 {"rows": [{"month": "2026-08", print, scan, copy, fax, jam_printer, days}, ...]}
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            f"SELECT substr(day, 1, 7) AS month, "
            f"COALESCE(SUM(print),0) AS print, COALESCE(SUM(scan),0) AS scan, "
            f"COALESCE(SUM(copy),0) AS copy, COALESCE(SUM(fax),0) AS fax, "
            f"COALESCE(SUM(jam_printer),0) AS jam_printer, COUNT(*) AS days "
            f"FROM {TABLE_PRINTER_DAILY} WHERE name = ? AND day != '' "
            f"GROUP BY month ORDER BY month DESC",
            (name,),
        ).fetchall()
        return {"rows": [dict(r) for r in rows]}
    finally:
        conn.close()


def query_printer_daily_range(db_path: str, name: str, start: str, end: str) -> dict:
    """打印数据查询④：指定日期区间数据（含墨量）。

    返回 {"start": ..., "end": ..., "rows": [...]}
    """
    if not start and not end:
        raise ValueError("printer_daily_range 需要 start 和/或 end 参数(格式 YYYY-MM-DD)")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        sql = (f"SELECT day, print, scan, copy, fax, jam_printer, "
               f"ink_black, ink_cyan, ink_magenta, ink_yellow, printer_jobs "
               f"FROM {TABLE_PRINTER_DAILY} WHERE name = ?")
        params: list = [name]
        if start:
            sql += " AND day >= ?"
            params.append(start)
        if end:
            sql += " AND day <= ?"
            params.append(end)
        sql += " ORDER BY day ASC"
        rows = conn.execute(sql, tuple(params)).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            jobs = d.get("printer_jobs", "")
            if jobs:
                try:
                    d["printer_jobs"] = json.loads(jobs)
                except (ValueError, TypeError):
                    d["printer_jobs"] = None
            else:
                d["printer_jobs"] = None
            result.append(d)
        return {"start": start, "end": end, "rows": result}
    finally:
        conn.close()


def query_printer_detail(db_path: str, name: str, date: str) -> dict:
    """打印数据查询⑤：指定日期的详细数据。

    直接从 printer_daily 的 printer_jobs JSON 字段解析返回，结构与当日详细实体 attributes 一致：
      {"date": "2026-08-12", "total": N,
       "print": [...], "scan": [...], "copy": [...], "fax": [...], "jam_printer": [...]}
    """
    if not date:
        raise ValueError("printer_detail 需要 date 参数(格式 YYYY-MM-DD)")
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            f"SELECT printer_jobs FROM {TABLE_PRINTER_DAILY} "
            f"WHERE name = ? AND day = ?",
            (name, date),
        ).fetchone()
        if not row or not row[0]:
            return {"date": date, "total": 0,
                    "print": [], "scan": [], "copy": [], "fax": [], "jam_printer": []}
        try:
            data = json.loads(row[0])
        except (ValueError, TypeError):
            return {"date": date, "total": 0,
                    "print": [], "scan": [], "copy": [], "fax": [], "jam_printer": []}
        if not isinstance(data, dict):
            return {"date": date, "total": 0,
                    "print": [], "scan": [], "copy": [], "fax": [], "jam_printer": []}
        return data
    finally:
        conn.close()


# =========================================================================== #
#  HTTP API Views                                                               #
# =========================================================================== #
class _PrinterBaseView(HomeAssistantView):
    """打印机 API 视图公共基类。"""

    requires_auth = False
    cors_allowed = True

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    async def _exec(self, hass: HomeAssistant, func, *args):
        return await hass.async_add_executor_job(func, *args)

    @staticmethod
    def _check_master_switch(hass: HomeAssistant) -> web.Response | None:
        """检查主开关（db_viewer 管理页面用，不含 Key 校验）。"""
        if not hass.data.get("ha_data_store", {}).get("api_enabled", True):
            return web.Response(status=403)
        return None


class PrinterConfigView(_PrinterBaseView):
    """打印机配置 CRUD。

    GET    /api/ha_data_store/printer/configs          → 列出所有配置
    POST   /api/ha_data_store/printer/configs          → 新增/修改配置 {name, stats_entity, detail_entity, enabled?}
    DELETE /api/ha_data_store/printer/configs?id=xxx   → 删除配置（不删历史记录）
    POST   /api/ha_data_store/printer/configs/recollect?id=xxx → 主动重采指定打印机
    """

    url = "/api/ha_data_store/printer/configs"
    name = "api:ha_data_store:printer_configs"
    extra_urls = ["/api/ha_data_store/printer/configs/recollect"]

    async def get(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_master_switch(hass)):
            return resp

        def _list():
            conn = sqlite3.connect(self._db_path)
            try:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    f"SELECT * FROM {TABLE_PRINTER_CONFIGS} ORDER BY id"
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

        try:
            data = await self._exec(hass, _list)
            return self.json({"success": True, "data": data})
        except Exception as exc:
            _LOGGER.exception("[printer] 查询配置失败")
            return self.json({"success": False, "error": str(exc)}, status_code=500)

    async def post(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_master_switch(hass)):
            return resp

        # recollect 子路径：主动重采
        if request.path.rstrip("/").endswith("/recollect"):
            return await self._recollect_view(request)

        try:
            body = await request.json()
        except Exception:
            return self.json({"success": False, "error": "请求体需为 JSON"}, status_code=400)

        name = (body.get("name") or "").strip()
        stats_entity = (body.get("stats_entity") or "").strip()
        detail_entity = (body.get("detail_entity") or "").strip()
        enabled = 1 if body.get("enabled", True) else 0
        if not stats_entity and not detail_entity:
            return self.json({"success": False, "error": "至少填写一个实体（统计数据实体或当日详细实体）"}, status_code=400)
        if not name:
            name = stats_entity or detail_entity

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        def _upsert():
            conn = sqlite3.connect(self._db_path)
            try:
                conn.execute(
                    f"INSERT INTO {TABLE_PRINTER_CONFIGS} "
                    f"(name, stats_entity, detail_entity, enabled, created_at, updated_at) "
                    f"VALUES (?, ?, ?, ?, ?, ?) "
                    f"ON CONFLICT(name) DO UPDATE SET "
                    f"stats_entity = excluded.stats_entity, detail_entity = excluded.detail_entity, "
                    f"enabled = excluded.enabled, updated_at = excluded.updated_at",
                    (name, stats_entity, detail_entity, enabled, now_str, now_str),
                )
                conn.commit()
                # 返回 printer name（唯一标识）
                return name
            finally:
                conn.close()

        try:
            printer_name = await self._exec(hass, _upsert)

            # 保存后主动采集一次当前数据（读取两个实体当前状态）
            await self._recollect(hass, printer_name)
            # 刷新受监控实体白名单
            from .http_api import _refresh_monitored
            await _refresh_monitored(hass, self._db_path)
            return self.json({"success": True, "message": "配置已保存并完成初始采集", "name": printer_name})
        except Exception as exc:
            _LOGGER.exception("[printer] 保存配置失败")
            return self.json({"success": False, "error": str(exc)}, status_code=500)

    async def _recollect(self, hass: HomeAssistant, printer_name: str) -> None:
        """主动采集：读取当前两个实体的最新状态并落库。"""
        stats_entity = detail_entity = None
        conn = sqlite3.connect(self._db_path)
        try:
            row = conn.execute(
                f"SELECT stats_entity, detail_entity FROM {TABLE_PRINTER_CONFIGS} WHERE name = ?",
                (printer_name,),
            ).fetchone()
            if row:
                stats_entity, detail_entity = row
        finally:
            conn.close()
        if not stats_entity and not detail_entity:
            return
        stats_state = hass.states.get(stats_entity) if stats_entity else None
        detail_state = hass.states.get(detail_entity) if detail_entity else None
        try:
            result = await self._exec(
                hass, collect_printer_sync, self._db_path, printer_name,
                stats_state, detail_state,
            )
            _LOGGER.info("[printer] 主动采集 name=%s result=%s", printer_name, result)
        except Exception:
            _LOGGER.exception("[printer] 主动采集异常 name=%s", printer_name)

    async def _recollect_view(self, request: web.Request) -> web.Response:
        """主动重采：POST /api/ha_data_store/printer/configs/recollect?name=xxx"""
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_master_switch(hass)):
            return resp
        printer_name = request.query.get("name", "").strip()
        if not printer_name:
            return self.json({"success": False, "error": "需提供 name 参数"}, status_code=400)
        await self._recollect(hass, printer_name)
        return self.json({"success": True, "message": "已触发重采"})

    async def delete(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_master_switch(hass)):
            return resp
        config_id = request.query.get("id", "").strip()
        if not config_id:
            return self.json({"success": False, "error": "需提供 id 参数"}, status_code=400)

        def _delete():
            conn = sqlite3.connect(self._db_path)
            try:
                conn.execute(
                    f"DELETE FROM {TABLE_PRINTER_CONFIGS} WHERE id = ?", (config_id,)
                )
                conn.commit()
                return conn.total_changes
            finally:
                conn.close()

        try:
            changed = await self._exec(hass, _delete)
            # 刷新受监控实体白名单，移除已删除的实体
            from .http_api import _refresh_monitored
            await _refresh_monitored(hass, self._db_path)
            # 注意：仅删除配置，不删除历史数据
            return self.json({"success": True, "message": f"已删除 {changed} 条配置（历史数据保留）"})
        except Exception as exc:
            _LOGGER.exception("[printer] 删除配置失败")
            return self.json({"success": False, "error": str(exc)}, status_code=500)


# =========================================================================== #
#  注册入口                                                                      #
# =========================================================================== #
def register_api_views(hass: HomeAssistant, db_path: str) -> None:
    """注册打印机相关 API View。由 __init__ 调用。"""
    hass.http.register_view(PrinterConfigView(db_path))
