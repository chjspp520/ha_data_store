"""元数据 + 通用指标引擎（`metrics_catalog`）。

本模块提供两层能力：

一、**元数据（schema catalog）**
    对集成数据库的表做语义标注（中文名、分组、时间列、实体列、值列、房间列…），
    对外暴露"表 → 列 → 可聚合"的目录，供 db_viewer「指标管理」渲染下拉框，
    同时作为通用引擎的**列白名单**，杜绝 SQL 注入。

二、**指标目录 + 通用查询引擎（metrics_catalog）**
    · 一条指标 = {源表 + 值列/表达式 + 聚合方式 + 默认分组 + 默认过滤 + 单位}
    · 启动时把内置指标 seed 进表（幂等，`INSERT OR IGNORE`，不覆盖用户改动）
    · `GET /query?type=metrics_query&metric_id=xxx` 可带时间/实体/分组/聚合/排序参数，
      由本模块编译成参数化 SQL 并执行，返回统一结构（rows + summary + series）

占位符（写在指标定义里，编译时按源表元数据解析成真实列名）：
    @time   时间列      @entity 实体列     @value 默认值列
    @room   房间列      @name   名称列     @id     主键列

安全约束：
    · 表名必须在 schema 白名单（或 sqlite_master 中存在）
    · 列名必须存在于 `PRAGMA table_info` 结果
    · 所有标识符统一用双引号包裹；值一律走参数化 `?`
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta

from .const import (
    ATTR_TABLE_PREFIX,
    DEFAULT_TIMEZONE,
    ENV_TABLE_PREFIX,
    TABLE_ATTR_TYPE_DEFS,
    TABLE_AUTOMATION_LOGS,
    TABLE_DEVICE_HISTORY,
    TABLE_HEALTH_RECORDS,
    TABLE_METRICS_CATALOG,
    TABLE_POWER_ENERGY_DAILY,
    TABLE_REPORT_ENTITIES,
    TABLE_USER_ACTIONS,
    VALID_METRICS,
)

_LOGGER = logging.getLogger(__name__)

# ── 指标目录表结构（由 __init__.py 在 _init_database 中执行）──
METRICS_CATALOG_SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS {TABLE_METRICS_CATALOG} (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    metric_id    TEXT NOT NULL UNIQUE,
    name         TEXT NOT NULL DEFAULT '',
    category     TEXT NOT NULL DEFAULT 'custom',
    source_table TEXT NOT NULL DEFAULT '',
    value_col    TEXT NOT NULL DEFAULT '',
    value_expr   TEXT NOT NULL DEFAULT '',
    agg          TEXT NOT NULL DEFAULT 'avg',
    unit         TEXT NOT NULL DEFAULT '',
    icon         TEXT NOT NULL DEFAULT '',
    group_by     TEXT NOT NULL DEFAULT 'day',
    group_col    TEXT NOT NULL DEFAULT '',
    filters      TEXT NOT NULL DEFAULT '',
    enabled      INTEGER NOT NULL DEFAULT 1,
    builtin      INTEGER NOT NULL DEFAULT 0,
    sort_order   INTEGER NOT NULL DEFAULT 100,
    remark       TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL DEFAULT '',
    updated_at   TEXT NOT NULL DEFAULT ''
)
"""

# ── 受支持的聚合与分组（白名单）──
AGG_FUNCS = ("avg", "sum", "max", "min", "count", "count_distinct")
GROUP_BY = ("none", "entity", "room", "name", "type", "day", "hour", "month", "year")
_TIME_GROUPS = ("day", "hour", "month", "year")
_CATEGORIES = (
    ("environment", "🌡 环境"),
    ("device", "🔌 设备"),
    ("power", "⚡ 用电"),
    ("health", "❤️ 健康"),
    ("action", "👤 操作"),
    ("automation", "🤖 自动化"),
    ("device_card", "🧾 卡片上报"),
    ("attribute", "🧩 属性提取"),
    ("custom", "✏️ 自定义"),
)
_CATEGORY_LABEL = dict(_CATEGORIES)

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_METRIC_ID_RE = re.compile(r"^[a-z0-9_]{1,64}$")

# 环境指标（env_* 表）—— 表结构统一：id/entity_id/name/datetime/value/room
_ENV_META = {
    "temperature": ("温度", "°C", "mdi:thermometer"),
    "humidity": ("湿度", "%", "mdi:water-percent"),
    "pm25": ("PM2.5", "μg/m³", "mdi:air-filter"),
    "co2": ("CO₂", "ppm", "mdi:molecule-co2"),
    "power": ("功率", "W", "mdi:flash"),
    "sensor": ("文本传感器", "", "mdi:text"),
}

# 静态表元数据（其余 env_* / attr_* 动态生成）
_SCHEMA_STATIC: list[dict] = [
    {
        "table": TABLE_DEVICE_HISTORY,
        "label": "设备开关记录",
        "category": "device",
        "time_col": "on_time",
        "time_kind": "datetime",
        "entity_col": "entity_id",
        "name_col": "name",
        "room_col": "room",
        "icon_col": "icon",
        "value_col": "duration",
        "remark": "每次开关一条记录；off_time 为空 = 仍在运行",
    },
    {
        "table": TABLE_HEALTH_RECORDS,
        "label": "健康记录",
        "category": "health",
        "time_col": "date_time",
        "time_kind": "datetime",
        "entity_col": "",
        "name_col": "name",
        "room_col": "",
        "icon_col": "",
        "value_col": "dp",
        "remark": "血压/体重/体温等，name 为成员名",
    },
    {
        "table": TABLE_USER_ACTIONS,
        "label": "用户操作埋点",
        "category": "action",
        "time_col": "ts_text",
        "time_kind": "datetime",
        "entity_col": "entity_id",
        "name_col": "name",
        "room_col": "room_name",
        "icon_col": "icon",
        "value_col": "id",
        "remark": "前端卡片操作上报（含 user_name / action / service）",
    },
    {
        "table": TABLE_POWER_ENERGY_DAILY,
        "label": "每日用电量",
        "category": "power",
        "time_col": "date",
        "time_kind": "date",
        "entity_col": "entity_id",
        "name_col": "device_name",
        "room_col": "room",
        "icon_col": "",
        "value_col": "kwh",
        "remark": "每实体每天一行（UNIQUE(entity_id, date)）",
    },
    {
        "table": TABLE_AUTOMATION_LOGS,
        "label": "自动化执行记录",
        "category": "automation",
        "time_col": "trigger_time",
        "time_kind": "datetime",
        "entity_col": "",
        "name_col": "automation_name",
        "room_col": "",
        "icon_col": "",
        "value_col": "duration_ms",
        "remark": "每次触发一条；status 为执行结果",
    },
    {
        "table": TABLE_REPORT_ENTITIES,
        "label": "前端卡片实体上报",
        "category": "device_card",
        "time_col": "last_report_time",
        "time_kind": "datetime",
        "entity_col": "entity_id",
        "name_col": "name",
        "room_col": "room_name",
        "icon_col": "icon",
        "value_col": "id",
        "remark": "前端卡片全量上报（含 icon / rooms / card_type）",
    },
]


# ========================================================================== #
#  基础工具                                                                   #
# ========================================================================== #
def _local_now() -> datetime:
    """当前东八区本地时间（与其它模块口径一致）。"""
    return datetime.utcnow() + timedelta(hours=DEFAULT_TIMEZONE)


def _now_str() -> str:
    return _local_now().strftime("%Y-%m-%d %H:%M:%S")


def _q(col: str) -> str:
    """标识符加双引号（防止关键字/中文列名冲突）。"""
    return '"' + str(col).replace('"', '""') + '"'


def _valid_ident(name: str) -> bool:
    return bool(_IDENT_RE.match(str(name or "")))


def _json_load(raw, default):
    if not raw:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        val = json.loads(raw)
        return val if isinstance(val, type(default)) else default
    except Exception:
        return default


# ========================================================================== #
#  一、schema 元数据                                                          #
# ========================================================================== #
def _table_columns(conn: sqlite3.Connection, table: str) -> list[dict]:
    """取表列（名称 + 类型）；表不存在返回空列表。"""
    try:
        rows = conn.execute(f"PRAGMA table_info({_q(table)})").fetchall()
    except sqlite3.Error:
        return []
    return [{"name": r[1], "type": (r[2] or "TEXT").upper()} for r in rows]


def _env_schema(exists: set[str]) -> list[dict]:
    """env_* 表元数据（按 VALID_METRICS 生成）。"""
    out: list[dict] = []
    for metric in VALID_METRICS:
        tbl = f"{ENV_TABLE_PREFIX}{metric}"
        if tbl not in exists:
            continue
        label, unit, icon = _ENV_META.get(metric, (metric, "", "mdi:chart-line"))
        out.append({
            "table": tbl,
            "label": f"环境·{label}",
            "category": "environment",
            "time_col": "datetime",
            "time_kind": "datetime",
            "entity_col": "entity_id",
            "name_col": "name",
            "room_col": "room",
            "icon_col": "",
            "value_col": "value",
            "remark": f"传感器采样（单位 {unit}）" if unit else "传感器采样",
        })
    return out


def _attr_schema(conn: sqlite3.Connection, exists: set[str]) -> list[dict]:
    """attr_* 表元数据（按 attr_type_defs 动态生成）。"""
    out: list[dict] = []
    try:
        rows = conn.execute(
            f"SELECT type_name, description FROM {TABLE_ATTR_TYPE_DEFS} ORDER BY type_name"
        ).fetchall()
    except sqlite3.Error:
        return out
    for type_name, desc in rows:
        tbl = f"{ATTR_TABLE_PREFIX}{type_name}"
        if tbl not in exists:
            continue
        out.append({
            "table": tbl,
            "label": f"属性·{type_name}",
            "category": "attribute",
            "time_col": "datetime",
            "time_kind": "datetime",
            "entity_col": "entity_id",
            "name_col": "name",
            "room_col": "room",
            "icon_col": "",
            "value_col": "",
            "remark": desc or "属性提取数据表（列由 field_mapping 决定）",
        })
    return out


def build_schema_catalog(conn: sqlite3.Connection) -> list[dict]:
    """构建完整 schema 目录（分组 → 表 → 列）。

    返回 [{key, label, tables: [{table, label, category, time_col, time_kind,
          entity_col, name_col, room_col, icon_col, value_col, remark,
          columns: [{name, type}]}]}]
    """
    try:
        exists = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    except sqlite3.Error:
        return []

    metas: list[dict] = []
    metas.extend(_SCHEMA_STATIC)
    metas.extend(_env_schema(exists))
    metas.extend(_attr_schema(conn, exists))

    known = {m["table"] for m in metas}
    # 未标注的表归入「其它」（只读、可用于自定义指标）
    metas.extend({
        "table": t, "label": t, "category": "custom",
        "time_col": "", "time_kind": "datetime",
        "entity_col": "", "name_col": "", "room_col": "", "icon_col": "",
        "value_col": "", "remark": "未标注的数据表",
    } for t in sorted(exists - known) if not t.startswith("sqlite_"))

    groups: dict[str, list[dict]] = {}
    for m in metas:
        cols = _table_columns(conn, m["table"])
        if not cols:
            continue  # 表尚未创建（如 attr 类型未采集）
        item = dict(m)
        item["columns"] = cols
        item["column_names"] = [c["name"] for c in cols]
        groups.setdefault(m["category"], []).append(item)

    out: list[dict] = []
    for key, label in _CATEGORIES:
        if key in groups:
            out.append({"key": key, "label": label, "tables": groups.pop(key)})
    for key in sorted(groups):  # 兜底
        out.append({"key": key, "label": _CATEGORY_LABEL.get(key, key), "tables": groups[key]})
    return out


def get_schema_catalog_sync(db_path: str) -> list[dict]:
    """供 API 调用：返回 schema 目录。"""
    conn = sqlite3.connect(db_path)
    try:
        return build_schema_catalog(conn)
    finally:
        conn.close()


def _schema_index(conn: sqlite3.Connection) -> dict[str, dict]:
    """{table: meta}，含 column_names 集合。"""
    idx: dict[str, dict] = {}
    for grp in build_schema_catalog(conn):
        for tbl in grp["tables"]:
            idx[tbl["table"]] = tbl
    return idx


# ========================================================================== #
#  二、占位符解析 / SQL 编译                                                   #
# ========================================================================== #
def _resolve_token(token: str, meta: dict) -> str:
    """把 @entity/@time/@value/@room/@name/@id 解析成真实列名。

    非占位符原样返回（后续按列名白名单校验）。
    """
    t = (token or "").strip()
    if not t:
        raise ValueError("列名为空")
    if t.startswith("@"):
        key = t[1:]
        if key == "id":
            return "id"
        col = meta.get(f"{key}_col", "") or ""
        if not col:
            raise ValueError(f"表 {meta['table']} 不支持占位符 {t}（未声明 {key}_col）")
        return col
    return t


def _subst(expr: str, meta: dict) -> str:
    """把表达式中的占位符替换为带引号的列名（如 `CAST(@value AS REAL)`）。"""

    def _repl(m: re.Match) -> str:
        return _q(_resolve_token(m.group(0), meta))

    return re.sub(r"@[A-Za-z_][A-Za-z0-9_]*", _repl, expr)


def _group_expr(group_by: str, meta: dict, group_col: str = "") -> str | None:
    """分组维度 → SQL 表达式；'none' 返回 None。"""
    gb = (group_by or "none").strip().lower()
    if gb in ("", "none"):
        return None
    cols = set(meta.get("column_names") or [])
    if gb == "entity":
        col = _resolve_token("@entity", meta)
    elif gb == "room":
        col = _resolve_token("@room", meta)
    elif gb == "name":
        col = _resolve_token("@name", meta)
    elif gb == "type":
        col = group_col or _resolve_token("@name", meta)
    elif gb in _TIME_GROUPS:
        time_col = _resolve_token("@time", meta)
        if gb == "hour":
            if meta.get("time_kind") == "date":
                raise ValueError(f"表 {meta['table']} 的时间列仅到日期，不支持按小时分组")
            return f"substr({_q(time_col)}, 12, 2)"
        _len = {"day": 10, "month": 7, "year": 4}[gb]
        return f"substr({_q(time_col)}, 1, {_len})"
    else:
        col = _resolve_token(gb, meta)
    if col not in cols:
        raise ValueError(f"分组列 {col} 不存在于表 {meta['table']}")
    return _q(col)


def _agg_expr(agg: str, value_sql: str) -> str:
    """聚合表达式（白名单）。"""
    a = (agg or "avg").strip().lower()
    if a not in AGG_FUNCS:
        raise ValueError(f"不支持的聚合方式: {agg}（可选 {', '.join(AGG_FUNCS)}）")
    if a == "count":
        return "COUNT(*)"
    if a == "count_distinct":
        if not value_sql:
            raise ValueError("count_distinct 需要指定值列")
        return f"COUNT(DISTINCT {value_sql})"
    if not value_sql:
        raise ValueError(f"{a} 需要指定值列")
    return f"{a.upper()}({value_sql})"


def _time_conds(meta: dict, start: str, end: str, date: str, month: str,
                year: str, days: int) -> tuple[list[str], list, str]:
    """时间过滤条件（优先级 start/end > date > month > year > days）。"""
    col = _resolve_token("@time", meta)
    if col not in (meta.get("column_names") or []):
        raise ValueError(f"表 {meta['table']} 未声明有效的时间列（{col}）")
    is_date = meta.get("time_kind") == "date"
    lo = "" if is_date else " 00:00:00"
    hi = "" if is_date else " 23:59:59"
    qc = _q(col)
    conds: list[str] = []
    params: list = []
    label = ""

    if start or end:
        if start:
            conds.append(f"{qc} >= ?")
            params.append(f"{start}{lo}")
        if end:
            conds.append(f"{qc} <= ?")
            params.append(f"{end}{hi}")
        label = f"{start or '最早'}~{end or '最新'}"
    elif date:
        conds.append(f"substr({qc}, 1, 10) = ?")
        params.append(date)
        label = date
    elif month:
        conds.append(f"substr({qc}, 1, 7) = ?")
        params.append(month)
        label = month
    elif year:
        conds.append(f"substr({qc}, 1, 4) = ?")
        params.append(year)
        label = year
    elif days and int(days) > 0:
        start_day = (_local_now() - timedelta(days=int(days) - 1)).strftime("%Y-%m-%d")
        conds.append(f"{qc} >= ?")
        params.append(f"{start_day}{lo}")
        label = f"最近 {int(days)} 天"
    return conds, params, label


# ========================================================================== #
#  三、指标目录 CRUD                                                           #
# ========================================================================== #
_COLUMNS = ("id", "metric_id", "name", "category", "source_table", "value_col",
            "value_expr", "agg", "unit", "icon", "group_by", "group_col",
            "filters", "enabled", "builtin", "sort_order", "remark",
            "created_at", "updated_at")


def _row_to_metric(row) -> dict:
    d: dict[str, object] = dict(zip(_COLUMNS, row))
    d["enabled"] = bool(d.get("enabled"))
    d["builtin"] = bool(d.get("builtin"))
    d["filters"] = _json_load(d.get("filters"), {})
    d["filters_text"] = json.dumps(d["filters"], ensure_ascii=False) if d["filters"] else ""
    return d


def list_metrics_sync(db_path: str, category: str = "", enabled_only: bool = False,
                      keyword: str = "") -> list[dict]:
    """列出指标（可按分类/启用状态/关键字过滤）。"""
    conn = sqlite3.connect(db_path)
    try:
        where, params = [], []
        if category:
            where.append("category = ?")
            params.append(category)
        if enabled_only:
            where.append("enabled = 1")
        if keyword:
            where.append("(metric_id LIKE ? OR name LIKE ? OR remark LIKE ?)")
            params.extend([f"%{keyword}%"] * 3)
        sql = (f"SELECT {','.join(_COLUMNS)} FROM {TABLE_METRICS_CATALOG} "
               + (("WHERE " + " AND ".join(where)) if where else "")
               + " ORDER BY category, sort_order, id")
        return [_row_to_metric(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def get_metric_sync(db_path: str, metric_id: str) -> dict | None:
    """按 metric_id 取单条指标定义。"""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            f"SELECT {','.join(_COLUMNS)} FROM {TABLE_METRICS_CATALOG} WHERE metric_id = ?",
            (metric_id,),
        ).fetchone()
        return _row_to_metric(row) if row else None
    finally:
        conn.close()


def validate_metric(conn: sqlite3.Connection, data: dict) -> dict:
    """校验并规范化一条指标定义（就地返回 new 值），失败抛 ValueError。

    只校验不写库；供 upsert 与「试运行」共用。
    """
    metric_id = str(data.get("metric_id", "")).strip()
    if not _METRIC_ID_RE.match(metric_id):
        raise ValueError("metric_id 只能由小写字母/数字/下划线组成（1~64 字符）")

    table = str(data.get("source_table", "")).strip()
    if not _valid_ident(table):
        raise ValueError("source_table 非法")
    idx = _schema_index(conn)
    meta = idx.get(table)
    if meta is None:
        raise ValueError(f"未知的数据表: {table}")
    cols = set(meta.get("column_names") or [])

    def _check_col(token: str, field: str) -> str:
        if not token:
            return ""
        try:
            col = _resolve_token(token, meta)
        except ValueError as exc:
            raise ValueError(f"{field}: {exc}") from exc
        if col not in cols:
            raise ValueError(f"{field}: 列 {col} 不存在于表 {table}")
        return token  # 保留原始写法（占位符或列名）

    value_col = _check_col(str(data.get("value_col", "")).strip(), "value_col")
    group_col = _check_col(str(data.get("group_col", "")).strip(), "group_col") \
        if str(data.get("group_col", "")).strip() else ""

    value_expr = str(data.get("value_expr", "")).strip()
    if value_expr:
        try:
            _subst(value_expr, meta)
        except ValueError as exc:
            raise ValueError(f"value_expr: {exc}") from exc

    agg = str(data.get("agg", "avg")).strip().lower() or "avg"
    if agg not in AGG_FUNCS:
        raise ValueError(f"agg 非法（可选 {', '.join(AGG_FUNCS)}）")
    if agg not in ("count", "count_distinct") and not (value_expr or value_col):
        raise ValueError(f"agg={agg} 需要指定 value_col 或 value_expr")

    group_by = str(data.get("group_by", "day")).strip().lower() or "day"
    if group_by == "type" and not group_col:
        raise ValueError("group_by=type 需要指定 group_col")
    try:
        _group_expr(group_by, meta, group_col)
    except ValueError as exc:
        raise ValueError(f"group_by: {exc}") from exc

    filters = _json_load(data.get("filters"), {})
    if not isinstance(filters, dict):
        filters = {}
    for col in filters:
        if _resolve_token(str(col), meta) not in cols:
            raise ValueError(f"filters: 列 {col} 不存在于表 {table}")

    category = str(data.get("category", "custom")).strip() or "custom"
    return {
        "metric_id": metric_id,
        "name": str(data.get("name", "")).strip() or metric_id,
        "category": category,
        "source_table": table,
        "value_col": value_col,
        "value_expr": value_expr,
        "agg": agg,
        "unit": str(data.get("unit", "")).strip(),
        "icon": str(data.get("icon", "")).strip(),
        "group_by": group_by,
        "group_col": group_col,
        "filters": json.dumps(filters, ensure_ascii=False),
        "sort_order": int(data.get("sort_order", 100) or 100),
        "remark": str(data.get("remark", "")).strip(),
    }


def upsert_metric_sync(db_path: str, data: dict) -> dict:
    """新增/修改指标（内置指标允许改参数，但不允许改 metric_id 冲突）。"""
    conn = sqlite3.connect(db_path)
    try:
        now = _now_str()
        clean = validate_metric(conn, data)
        existing = conn.execute(
            f"SELECT builtin, created_at FROM {TABLE_METRICS_CATALOG} WHERE metric_id = ?",
            (clean["metric_id"],),
        ).fetchone()
        builtin = int(existing[0]) if existing else int(bool(data.get("builtin")))
        created = (existing[1] if existing and existing[1] else now)
        conn.execute(
            f"INSERT OR REPLACE INTO {TABLE_METRICS_CATALOG} "
            f"(metric_id, name, category, source_table, value_col, value_expr, agg, "
            f" unit, icon, group_by, group_col, filters, enabled, builtin, sort_order, "
            f" remark, created_at, updated_at) "
            f"VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                clean["metric_id"], clean["name"], clean["category"], clean["source_table"],
                clean["value_col"], clean["value_expr"], clean["agg"], clean["unit"],
                clean["icon"], clean["group_by"], clean["group_col"], clean["filters"],
                int(bool(data.get("enabled", True))), builtin, clean["sort_order"],
                clean["remark"], created, now,
            ),
        )
        conn.commit()
        return get_metric_sync(db_path, clean["metric_id"]) or clean
    finally:
        conn.close()


def delete_metric_sync(db_path: str, metric_id: str) -> bool:
    """删除指标（内置指标禁止删除，只能停用）。"""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            f"SELECT builtin FROM {TABLE_METRICS_CATALOG} WHERE metric_id = ?", (metric_id,)
        ).fetchone()
        if not row:
            return False
        if int(row[0] or 0):
            raise ValueError("内置指标不可删除（可将其停用或复制为自定义指标）")
        conn.execute(f"DELETE FROM {TABLE_METRICS_CATALOG} WHERE metric_id = ?", (metric_id,))
        conn.commit()
        return True
    finally:
        conn.close()


# ========================================================================== #
#  四、内置指标 seed                                                          #
# ========================================================================== #
def _builtin_defs(conn: sqlite3.Connection) -> list[dict]:
    """生成内置指标定义（按实际存在的表裁剪）。"""
    idx = _schema_index(conn)
    exists = set(idx)
    out: list[dict] = []

    def _add(metric_id, name, table, value_col, agg, unit, icon, group_by,
             remark="", value_expr="", filters=None, category="custom", sort=100):
        if table not in exists:
            return
        out.append({
            "metric_id": metric_id, "name": name, "source_table": table,
            "value_col": value_col, "value_expr": value_expr, "agg": agg,
            "unit": unit, "icon": icon, "group_by": group_by, "group_col": "",
            "filters": filters or {}, "category": category, "remark": remark,
            "sort_order": sort, "builtin": 1, "enabled": True,
        })

    # ── 环境 ──
    for metric in VALID_METRICS:
        tbl = f"{ENV_TABLE_PREFIX}{metric}"
        if tbl not in exists:
            continue
        label, unit, icon = _ENV_META.get(metric, (metric, "", "mdi:chart-line"))
        if metric == "sensor":
            _add(f"env_{metric}_count", f"{label}采样数", tbl, "id", "count", "条",
                 "mdi:counter", "day", "文本型传感器的采样条数", category="environment", sort=60)
            continue
        _add(f"env_{metric}_avg", f"{label}均值", tbl, "@value", "avg", unit, icon,
             "day", f"按天统计{label}平均值", category="environment", sort=10)
        _add(f"env_{metric}_max", f"{label}最高", tbl, "@value", "max", unit, icon,
             "day", f"按天统计{label}最高值", category="environment", sort=20)
        _add(f"env_{metric}_min", f"{label}最低", tbl, "@value", "min", unit, icon,
             "day", f"按天统计{label}最低值", category="environment", sort=30)

    # ── 设备 ──
    _add("device_on_count", "设备开启次数", TABLE_DEVICE_HISTORY, "id", "count", "次",
         "mdi:counter", "entity", "每台设备的开启次数", category="device", sort=10)
    _add("device_duration_sum", "设备运行时长合计", TABLE_DEVICE_HISTORY, "duration", "sum",
         "秒", "mdi:timer-outline", "entity", "累计运行时长（秒）", category="device", sort=20)
    _add("device_duration_avg", "设备单次时长均值", TABLE_DEVICE_HISTORY, "duration", "avg",
         "秒", "mdi:timer", "entity", "平均每次运行时长（秒）", category="device", sort=30)
    _add("device_energy_sum", "设备用电合计", TABLE_DEVICE_HISTORY, "energy_consumed", "sum",
         "kWh", "mdi:flash", "entity", "累计用电量", category="device", sort=40)
    _add("device_running_now", "运行中设备数", TABLE_DEVICE_HISTORY, "id", "count", "台",
         "mdi:play-circle", "entity", "off_time 为空 = 仍在运行",
         filters={"off_time": ""}, category="device", sort=50)

    # ── 用电 ──
    _add("power_kwh_sum", "用电量合计", TABLE_POWER_ENERGY_DAILY, "kwh", "sum", "kWh",
         "mdi:flash", "day", "按天汇总全屋/指定实体用电", category="power", sort=10)
    _add("power_kwh_avg", "日均用电量", TABLE_POWER_ENERGY_DAILY, "kwh", "avg", "kWh",
         "mdi:chart-bell-curve", "entity", "按实体统计日均用电", category="power", sort=20)

    # ── 健康 ──
    _add("health_dp_avg", "血压高压均值", TABLE_HEALTH_RECORDS, "dp", "avg", "mmHg",
         "mdi:heart-pulse", "name", "按成员统计收缩压均值", category="health", sort=10)
    _add("health_sp_avg", "血压低压均值", TABLE_HEALTH_RECORDS, "sp", "avg", "mmHg",
         "mdi:heart-pulse", "name", "按成员统计舒张压均值", category="health", sort=20)
    _add("health_weight_avg", "体重均值", TABLE_HEALTH_RECORDS, "weight", "avg", "kg",
         "mdi:scale-bathroom", "name", "按成员统计体重均值", category="health", sort=30)
    _add("health_record_count", "健康记录条数", TABLE_HEALTH_RECORDS, "id", "count", "条",
         "mdi:counter", "name", "按成员统计记录条数", category="health", sort=40)

    # ── 操作 ──
    _add("user_action_count", "用户操作次数", TABLE_USER_ACTIONS, "id", "count", "次",
         "mdi:gesture-tap", "entity", "按设备统计被操作次数", category="action", sort=10)
    _add("user_action_users", "活跃用户数", TABLE_USER_ACTIONS, "user_name",
         "count_distinct", "人", "mdi:account-multiple", "day",
         "按天统计有多少不同用户操作过", category="action", sort=20)

    # ── 自动化 ──
    _add("automation_run_count", "自动化执行次数", TABLE_AUTOMATION_LOGS, "id", "count", "次",
         "mdi:robot", "name", "按自动化名称统计触发次数", category="automation", sort=10)
    _add("automation_duration_avg", "自动化平均耗时", TABLE_AUTOMATION_LOGS, "duration_ms",
         "avg", "ms", "mdi:timer-outline", "name", "按自动化统计平均执行耗时",
         category="automation", sort=20)

    # ── 卡片上报 ──
    _add("report_card_count", "上报卡片数", TABLE_REPORT_ENTITIES, "entity_id",
         "count_distinct", "个", "mdi:card", "day", "按天统计上报的卡片实体数",
         category="device_card", sort=10)

    # ── 属性提取（每个 attr 类型：记录数 + 每个 REAL 列均值）──
    try:
        attr_rows = conn.execute(
            f"SELECT type_name FROM {TABLE_ATTR_TYPE_DEFS} ORDER BY type_name").fetchall()
    except sqlite3.Error:
        attr_rows = []
    for (type_name,) in attr_rows:
        tbl = f"{ATTR_TABLE_PREFIX}{type_name}"
        meta = idx.get(tbl)
        if not meta:
            continue
        _add(f"attr_{type_name}_count", f"{type_name} 记录数", tbl, "id", "count", "条",
             "mdi:counter", "day", f"属性类型 {type_name} 的采集条数",
             category="attribute", sort=10)
        real_cols = [c["name"] for c in meta.get("columns", [])
                     if c["type"] == "REAL" and _valid_ident(c["name"])]
        for col in real_cols[:5]:  # 每类型最多 5 个，避免指标爆炸
            _add(f"attr_{type_name}_{col}_avg", f"{type_name}·{col} 均值", tbl, col, "avg",
                 "", "mdi:chart-line", "day", f"属性类型 {type_name} 的 {col} 均值",
                 category="attribute", sort=20)
    return out


def sync_builtin_metrics(conn: sqlite3.Connection, reset: bool = False) -> int:
    """同步内置指标到 metrics_catalog。

    · reset=False（默认，启动时调用）：`INSERT OR IGNORE`，**不覆盖**用户改动，
      仅补齐缺失的内置指标；
    · reset=True（db_viewer「重置内置指标」）：按最新定义覆盖内置指标的参数。

    返回新增/更新的条数。
    """
    try:
        conn.execute(METRICS_CATALOG_SCHEMA_SQL)
    except sqlite3.Error as exc:
        _LOGGER.warning("[HDS] 创建 %s 失败: %s", TABLE_METRICS_CATALOG, exc)
        return 0
    defs = _builtin_defs(conn)
    now = _now_str()
    changed = 0
    for d in defs:
        try:
            clean = validate_metric(conn, d)
        except ValueError as exc:
            _LOGGER.debug("[HDS] 跳过内置指标 %s: %s", d.get("metric_id"), exc)
            continue
        row = conn.execute(
            f"SELECT id FROM {TABLE_METRICS_CATALOG} WHERE metric_id = ?",
            (clean["metric_id"],),
        ).fetchone()
        if row and not reset:
            continue  # 已存在且不覆盖
        params = (
            clean["metric_id"], clean["name"], clean["category"], clean["source_table"],
            clean["value_col"], clean["value_expr"], clean["agg"], clean["unit"],
            clean["icon"], clean["group_by"], clean["group_col"], clean["filters"],
            1, 1, clean["sort_order"], clean["remark"],
        )
        conn.execute(
            f"INSERT OR REPLACE INTO {TABLE_METRICS_CATALOG} "
            f"(metric_id, name, category, source_table, value_col, value_expr, agg, "
            f" unit, icon, group_by, group_col, filters, enabled, builtin, sort_order, "
            f" remark, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            params + (now, now),
        )
        changed += 1
    try:
        conn.commit()
    except sqlite3.Error:
        pass
    return changed


def sync_builtin_metrics_sync(db_path: str, reset: bool = False) -> int:
    """供 API 调用的内置指标同步。"""
    conn = sqlite3.connect(db_path)
    try:
        return sync_builtin_metrics(conn, reset)
    finally:
        conn.close()


# ========================================================================== #
#  五、通用查询引擎                                                            #
# ========================================================================== #
def build_metric_sql(conn: sqlite3.Connection, metric: dict, *,
                     start: str = "", end: str = "", date: str = "", month: str = "",
                     year: str = "", days: int = 0, entities=None, room: str = "",
                     group_by: str = "", agg: str = "", order: str = "auto",
                     limit: int = 0, offset: int = 0,
                     extra_filters: dict | None = None) -> tuple[str, list, dict]:
    """编译指标的 SQL（参数化），返回 (sql, params, info)。

    info 含：分组表达式、排序字段、是否时间维度等，供结果整理使用。
    """
    table = metric["source_table"]
    idx = _schema_index(conn)
    meta = idx.get(table)
    if meta is None:
        raise ValueError(f"指标 {metric.get('metric_id')} 的源表 {table} 不存在")

    # 值表达式
    raw_expr = (metric.get("value_expr") or "").strip() or (metric.get("value_col") or "").strip()
    value_sql = _subst(raw_expr, meta) if raw_expr else ""

    agg = (agg or metric.get("agg") or "avg").strip().lower()
    agg_sql = _agg_expr(agg, value_sql)

    gb = (group_by or metric.get("group_by") or "day").strip().lower()
    group_sql = _group_expr(gb, meta, metric.get("group_col", ""))

    conds: list[str] = []
    params: list = []
    tc, tp, label = _time_conds(meta, start, end, date, month, year, days)
    conds.extend(tc)
    params.extend(tp)

    if entities:
        col = _resolve_token("@entity", meta)
        if col not in (meta.get("column_names") or []):
            raise ValueError(f"表 {table} 不支持 entity 过滤（无实体列）")
        conds.append(f"{_q(col)} IN ({','.join(['?'] * len(entities))})")
        params.extend(entities)
    if room:
        col = _resolve_token("@room", meta)
        if col in (meta.get("column_names") or []):
            conds.append(f"{_q(col)} = ?")
            params.append(room)

    # 过滤：指标自带 + 调用方附加
    filters = metric.get("filters") or {}
    if not isinstance(filters, dict):
        filters = _json_load(filters, {})
    for col, val in {**filters, **(extra_filters or {})}.items():
        real = _resolve_token(str(col), meta)
        if real not in (meta.get("column_names") or []):
            raise ValueError(f"过滤列 {col} 不存在于表 {table}")
        conds.append(f"{_q(real)} = ?")
        params.append(val)

    where = (" WHERE " + " AND ".join(conds)) if conds else ""

    # 分组附带信息（实体/房间维度的名称、图标）
    extra_select, extra_map = "", []
    if gb in ("entity", "name", "type"):
        for token, alias in (("@name", "name"), ("@room", "room"), ("@icon", "icon")):
            try:
                col = _resolve_token(token, meta)
            except ValueError:
                continue
            if col in (meta.get("column_names") or []):
                extra_select += f", MAX({_q(col)}) AS {alias}"
                extra_map.append(alias)

    if group_sql:
        is_time = gb in _TIME_GROUPS
        sort_col = "k" if is_time else "v"
        direction = (order or "auto").strip().lower()
        if direction == "auto":
            direction = "asc" if is_time else "desc"
        if direction not in ("asc", "desc"):
            direction = "asc"
        sql = (f"SELECT {group_sql} AS k, COUNT(*) AS c, {agg_sql} AS v{extra_select} "
               f"FROM {_q(table)}{where} GROUP BY k "
               f"ORDER BY {sort_col} {direction.upper()}")
    else:
        sql = f"SELECT COUNT(*) AS c, {agg_sql} AS v FROM {_q(table)}{where}"

    lim = int(limit or 0)
    if lim > 0:
        sql += f" LIMIT {lim}"
        off = int(offset or 0)
        if off > 0:
            sql += f" OFFSET {off}"

    info = {"group_by": gb, "group_sql": group_sql, "is_time_group": gb in _TIME_GROUPS,
            "extra_map": extra_map, "agg": agg, "range": label, "table": table}
    return sql, params, info


def _round(v):
    """数值保留 6 位，避免浮点噪音。"""
    if isinstance(v, float):
        return round(v, 6)
    return v


def compute_metrics_query_sync(
    db_path: str,
    metric_id: str = "",
    *,
    metric: dict | None = None,
    start: str = "", end: str = "", date: str = "", month: str = "", year: str = "",
    days: int = 0,
    entities=None,
    room: str = "",
    group_by: str = "",
    agg: str = "",
    order: str = "auto",
    limit: int = 0,
    offset: int = 0,
    filters: dict | None = None,
    detail: bool = False,
    detail_limit: int = 50,
    include_sql: bool = False,
) -> dict:
    """执行一条指标查询。

    · metric_id：指标目录中的 ID；也可直接传 metric（试运行，未经保存的定义）
    · 时间：start/end > date > month > year > days（最近 N 天），作用于指标源表的时间列
    · entities/room：实体与房间过滤
    · group_by / agg / order：覆盖指标默认值
    · filters：附加等值过滤（JSON 键值对），与指标自带 filters 合并
    · detail=1：额外返回原始记录（最多 detail_limit 行）

    返回：{metric, range, agg, unit, group_by, row_count, count, summary, rows, series}
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        if metric is None:
            if not metric_id:
                raise ValueError("缺少 metric_id")
            row = conn.execute(
                f"SELECT {','.join(_COLUMNS)} FROM {TABLE_METRICS_CATALOG} WHERE metric_id = ?",
                (metric_id,),
            ).fetchone()
            if not row:
                raise ValueError(f"未找到指标: {metric_id}")
            if not int(row[13] or 0):  # enabled
                raise ValueError(f"指标 {metric_id} 已停用")
            metric = _row_to_metric(row)

        sql, params, info = build_metric_sql(
            conn, metric, start=start, end=end, date=date, month=month, year=year,
            days=days, entities=entities or [], room=room, group_by=group_by,
            agg=agg, order=order, limit=limit, offset=offset, extra_filters=filters,
        )
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

        # 原始记录条数（不受分组影响）
        table = info["table"]
        idx = _schema_index(conn)
        meta = idx.get(table) or {}
        cnt_conds: list[str] = []
        cnt_params: list = []
        tc, tp, _ = _time_conds(meta, start, end, date, month, year, days)
        cnt_conds.extend(tc)
        cnt_params.extend(tp)
        if entities:
            col = _resolve_token("@entity", meta)
            cnt_conds.append(f"{_q(col)} IN ({','.join(['?'] * len(entities))})")
            cnt_params.extend(entities)
        f = metric.get("filters") or {}
        if not isinstance(f, dict):
            f = {}
        for col, val in {**f, **(filters or {})}.items():
            cnt_conds.append(f"{_q(_resolve_token(str(col), meta))} = ?")
            cnt_params.append(val)
        cnt_where = (" WHERE " + " AND ".join(cnt_conds)) if cnt_conds else ""
        try:
            row_count = conn.execute(
                f"SELECT COUNT(*) FROM {_q(table)}{cnt_where}", cnt_params).fetchone()[0]
        except sqlite3.Error:
            row_count = sum(int(r.get("c") or 0) for r in rows)

        out_rows: list[dict] = []
        for r in rows:
            item: dict = {
                "key": "" if info["group_sql"] is None else (r.get("k") if r.get("k") is not None else ""),
                "value": _round(r.get("v")),
                "count": int(r.get("c") or 0),
            }
            for alias in info["extra_map"]:
                item[alias] = r.get(alias) or ""
            out_rows.append(item)

        values = [x["value"] for x in out_rows if isinstance(x["value"], (int, float))]
        summary = {
            "rows": len(out_rows),
            "samples": int(row_count),
            "total_count": sum(int(x["count"]) for x in out_rows),
            "value": _round(sum(values)) if values else 0,
            "avg": _round(sum(values) / len(values)) if values else 0,
            "min": _round(min(values)) if values else 0,
            "max": _round(max(values)) if values else 0,
        }

        result: dict = {
            "metric_id": metric.get("metric_id", ""),
            "name": metric.get("name", ""),
            "category": metric.get("category", ""),
            "unit": metric.get("unit", ""),
            "icon": metric.get("icon", ""),
            "source_table": info["table"],
            "range": info["range"] or "全部时间",
            "group_by": info["group_by"],
            "agg": info["agg"],
            "row_count": int(row_count),
            "count": len(out_rows),
            "summary": summary,
            "rows": out_rows,
            "series": {
                "labels": [x["key"] for x in out_rows],
                "values": [x["value"] for x in out_rows],
                "counts": [x["count"] for x in out_rows],
            },
        }

        if detail:
            dcols = meta.get("column_names") or []
            sel = ", ".join(_q(c) for c in dcols[:25])
            try:
                dsql = f"SELECT {sel} FROM {_q(table)}{cnt_where} ORDER BY rowid DESC LIMIT ?"
                result["detail"] = [dict(x) for x in conn.execute(
                    dsql, cnt_params + [max(1, int(detail_limit))]).fetchall()]
            except sqlite3.Error as exc:
                result["detail_error"] = str(exc)
        if include_sql:
            result["sql"] = sql
            result["params"] = [str(p) for p in params]
        return result
    finally:
        conn.close()


def test_metric_sync(db_path: str, data: dict, **kwargs) -> dict:
    """试运行：不保存，按传入定义直接执行（供 db_viewer 验证）。"""
    conn = sqlite3.connect(db_path)
    try:
        metric = validate_metric(conn, data)
    finally:
        conn.close()
    metric["filters"] = _json_load(metric.get("filters"), {})
    kwargs.setdefault("include_sql", True)
    return compute_metrics_query_sync(db_path, metric=metric, **kwargs)
