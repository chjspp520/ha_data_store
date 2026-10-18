"""ha_data_store — 个人账单模块（独立模块）。

数据表：personal_bills（个人账单明细，由支付宝 / 微信等账单导出整理而来）
说明：原始 CSV 中的 year / day / month_day 不单独建列，统一从 bill_time 用
      substr/strftime 派生（年 / 年月 / 年月日 / 小时）。

独立 API（自带路径，不走 /api/ha_data_store/query）：
  GET  /api/ha_data_store/bill?type=bill_history&...   查询 / 分析（见 run_bill_query）
  GET  /api/ha_data_store/bill/types                   可选值清单
  GET  /api/ha_data_store/bill/notes?keyword=&limit=   备注候选清单（去重+次数，可点击选择）
  POST /api/ha_data_store/bill/add                     提交单条账单
  POST /api/ha_data_store/bill/batch                   批量提交账单
  POST /api/ha_data_store/bill/update                  修改账单（含备注 note/remark，支持 id/ids 批量）
  POST /api/ha_data_store/bill/remark                  单独提交备注（{id|ids, note, mode}）
  POST|DELETE /api/ha_data_store/bill/delete?id=1,2    删除账单

接入方式：
  建表     ：__init__ 启动时调用 ensure_tables(db_path)
  注册视图 ：__init__._register_api_views 调用 register_api_views(hass, db_path)
"""
from __future__ import annotations

import calendar
import logging
import re
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from aiohttp import web
from homeassistant.core import HomeAssistant

from .http_api import _BaseDBView, _to_float_or_none

_LOGGER = logging.getLogger(__name__)

# =========================================================================== #
#  表结构与字段定义                                                            #
# =========================================================================== #
TABLE_PERSONAL_BILLS = "personal_bills"

# 字段定义：(列名, 列类型, 中文标签)
BILL_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("source",               "TEXT", "数据来源"),
    ("bill_time",            "TEXT", "交易时间"),
    ("bill_no",              "TEXT", "交易单号"),
    ("category",             "TEXT", "交易类型"),
    ("direction",            "TEXT", "收支方向"),
    ("pay_method",           "TEXT", "收/支方式"),
    ("amount",               "REAL", "金额"),
    ("counterparty",         "TEXT", "对方"),
    ("counterparty_bill_no", "TEXT", "对方单号"),
    ("counterparty_account", "TEXT", "对方账号"),
    ("status",               "TEXT", "交易状态"),
    ("product",              "TEXT", "商品说明"),
    ("remark",               "TEXT", "备注（导入来源）"),
    ("note",                 "TEXT", "备注（可编辑，支持 POST 修改）"),
    ("created_at",           "TEXT", "入库时间"),
)

# 账单列名（顺序即建表顺序）
BILL_COLUMNS: tuple[str, ...] = tuple(f[0] for f in BILL_FIELDS)

# 账单列名 → 列类型
BILL_COLUMN_TYPES: dict[str, str] = {f[0]: f[1] for f in BILL_FIELDS}

# 账单列名 → 中文标签
BILL_COLUMN_LABELS: dict[str, str] = {f[0]: f[2] for f in BILL_FIELDS}

# 可作为分组 / 排行 / 交叉汇总维度的列
BILL_DIMENSIONS: tuple[str, ...] = (
    "source", "category", "direction", "pay_method", "counterparty", "status",
)

# 账单表索引：(索引后缀, 列元组)
BILL_INDEX_DEFS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("time",         ("bill_time",)),
    ("source",       ("source", "bill_time")),
    ("category",     ("category", "bill_time")),
    ("direction",    ("direction", "bill_time")),
    ("counterparty", ("counterparty", "bill_time")),
)

# 支持的查询 / 分析类型（供文档与校验）
BILL_QUERY_TYPES: tuple[str, ...] = (
    # 明细 / 定位
    "bill_history", "bill_recent", "bill_latest",
    "bill_by_no", "bill_by_account", "bill_by_names", "bill_split",
    "bill_by_day", "bill_calendar", "bill_periods", "bill_options", "bill_meta",
    # 首页仪表盘（一次取全：总计 + 收支占比 + 年度收支 + 最近 N 日小计与明细）
    "bill_dashboard",
    # 聚合 / 分布
    "bill_summary", "bill_group", "bill_trend", "bill_ranking", "bill_top",
    "bill_hour_dist", "bill_weekday_dist", "bill_dom_dist", "bill_amount_dist",
    "bill_status_dist", "bill_product_top", "bill_seasonal",
    # 对比 / 画像
    "bill_compare", "bill_profile", "bill_yoy", "bill_month_day_years",
    "bill_crosstab", "bill_heatmap", "bill_flow",
    # 深度分析
    "bill_first_last", "bill_interval", "bill_frequency", "bill_netflow",
    "bill_income_expense", "bill_anomaly", "bill_recurring", "bill_fixed_cost",
    "bill_percentile", "bill_quality",
    # 历史今日
    "bill_onthisday",
    # 备注
    "bill_notes",
)

VIEW_URL = "/api/ha_data_store/bill"
VIEW_NAME = "api:ha_data_store:bill"

# 时间派生表达式：year / month / day / hour / weekday
_BILL_TIME_BUCKETS: dict[str, str] = {
    "year": "substr(bill_time,1,4)",
    "month": "substr(bill_time,1,7)",
    "day": "substr(bill_time,1,10)",
    "hour": "substr(bill_time,12,2)",
    "weekday": "strftime('%w', bill_time)",
}

# 提交写入的列（顺序固定；id 自增，year/day/month_day 由 bill_time 派生不入库）
_BILL_WRITE_COLS: tuple[str, ...] = (
    "source", "bill_time", "bill_no", "category", "direction", "pay_method", "amount",
    "counterparty", "counterparty_bill_no", "counterparty_account",
    "status", "product", "remark", "note", "created_at",
)

_BILL_STR_FIELDS: tuple[str, ...] = (
    "source", "bill_time", "bill_no", "category", "direction", "pay_method",
    "counterparty", "counterparty_bill_no", "counterparty_account",
    "status", "product", "remark", "note",
)

# 可通过 POST /bill/update 修改的字段（id / created_at 不允许改）
_BILL_EDITABLE: tuple[str, ...] = tuple(c for c in BILL_COLUMNS if c != "created_at")


# =========================================================================== #
#  建表                                                                        #
# =========================================================================== #
def ensure_tables(db_path: str) -> None:
    """创建 personal_bills 表、补齐缺失列并建立索引（幂等）。"""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE_PERSONAL_BILLS} (
                id                     INTEGER PRIMARY KEY AUTOINCREMENT,
                source                 TEXT NOT NULL DEFAULT '',
                bill_time              TEXT NOT NULL DEFAULT '',
                bill_no                TEXT NOT NULL DEFAULT '',
                category               TEXT NOT NULL DEFAULT '',
                direction              TEXT NOT NULL DEFAULT '',
                pay_method             TEXT NOT NULL DEFAULT '',
                amount                 REAL,
                counterparty           TEXT NOT NULL DEFAULT '',
                counterparty_bill_no   TEXT NOT NULL DEFAULT '',
                counterparty_account   TEXT NOT NULL DEFAULT '',
                status                 TEXT NOT NULL DEFAULT '',
                product                TEXT NOT NULL DEFAULT '',
                remark                 TEXT NOT NULL DEFAULT '',
                created_at             TEXT NOT NULL DEFAULT ''
            );
            """
        )
        # 迁移：为早于本版本创建的表补齐缺失列
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({TABLE_PERSONAL_BILLS})")}
        for col, ctype, _label in BILL_FIELDS:
            if col not in existing:
                # 与建表保持一致：TEXT 列带 NOT NULL DEFAULT ''（避免旧库补列后为 NULL）
                col_def = f"{col} {ctype}"
                if ctype == "TEXT":
                    col_def += " NOT NULL DEFAULT ''"
                try:
                    conn.execute(f"ALTER TABLE {TABLE_PERSONAL_BILLS} ADD COLUMN {col_def}")
                except Exception as e:  # noqa: BLE001
                    _LOGGER.warning("[bill] personal_bills 补充列 %s 失败（可忽略）: %s", col, e)
        for suffix, cols in BILL_INDEX_DEFS:
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_bill_{suffix} "
                f"ON {TABLE_PERSONAL_BILLS} ({', '.join(cols)});"
            )
        conn.commit()
    finally:
        conn.close()


# =========================================================================== #
#  参数与 SQL 辅助                                                             #
# =========================================================================== #
def _s(params: dict[str, Any], key: str) -> str:
    """取字符串参数（去空白）。"""
    return str(params.get(key, "") or "").strip()


def _flag(params: dict[str, Any], key: str) -> bool:
    """取布尔参数（1/true/yes）。"""
    return _s(params, key).lower() in ("1", "true", "yes")


def _int_param(params: dict[str, Any], key: str, default: int,
               minimum: int = 0, maximum: int = 100000) -> int:
    try:
        val = int(_s(params, key) or default)
    except (ValueError, TypeError):
        val = default
    return max(minimum, min(maximum, val))


def _parse_mode(params: dict[str, Any], default: str = "normal") -> str:
    mode = _s(params, "mode").lower() or default
    if mode not in ("minimal", "simple", "normal", "full"):
        raise ValueError("mode 仅支持: minimal, simple, normal, full")
    return mode


def _fields(params: dict[str, Any]) -> list[str] | None:
    """解析返回字段设置 fields=col1,col2（白名单校验）；未指定返回 None。

    也接受 1 作为 id 的别名；非法列会被忽略。
    """
    raw = _s(params, "fields")
    if not raw:
        return None
    allowed = set(BILL_COLUMNS) | {"id"}
    got = [f.strip() for f in raw.split(",") if f.strip() in allowed]
    return got or None


def _select_cols(fields: list[str] | None) -> str:
    return ", ".join(f'"{c}"' for c in fields) if fields else "*"


def _append_cond(base_sql: str, cond: str) -> str:
    """在已有 where 子句后追加条件（自动补 AND / WHERE）。"""
    return f"{base_sql} AND {cond}" if base_sql else f" WHERE {cond}"


def _dim_expr(name: str) -> str:
    """把维度名转成 SQL 表达式（列名或时间派生表达式）。"""
    key = (name or "").strip().lower()
    if key in BILL_DIMENSIONS:
        return key
    if key in _BILL_TIME_BUCKETS:
        return _BILL_TIME_BUCKETS[key]
    raise ValueError(
        "维度仅支持: " + ", ".join(list(BILL_DIMENSIONS) + list(_BILL_TIME_BUCKETS)))


def _dim_cond(dim: str, value: str, exact: bool) -> tuple[str, list[Any]]:
    """按维度生成「单值」过滤条件，返回 (condition, params)。

    counterparty/account/bill_no 会同时匹配「对方」与「对方单号 / 账号」等派生列。
    """
    key = (dim or "").strip().lower()
    if key == "counterparty":
        if exact:
            return "counterparty = ?", [value]
        return "counterparty LIKE ?", [f"%{value}%"]
    if key == "account":
        if exact:
            return "(counterparty_account = ? OR counterparty_bill_no = ?)", [value, value]
        return ("(counterparty_account LIKE ? OR counterparty_bill_no LIKE ?)",
                [f"%{value}%", f"%{value}%"])
    if key == "bill_no":
        if exact:
            return "(bill_no = ? OR counterparty_bill_no = ?)", [value, value]
        return ("(bill_no LIKE ? OR counterparty_bill_no LIKE ?)",
                [f"%{value}%", f"%{value}%"])
    if key in BILL_DIMENSIONS:
        if exact:
            return f"{key} = ?", [value]
        return f"{key} LIKE ?", [f"%{value}%"]
    raise ValueError(
        "分类维度仅支持: counterparty, account, bill_no, " + ", ".join(BILL_DIMENSIONS))


def _build_filters(params: dict[str, Any], include_time: bool = True) -> tuple[str, list[Any]]:
    """根据参数构建账单过滤条件（各查询 / 汇总 / 分组共用）。

    维度（均可留空）：
      source/category/direction/pay_method/status：精确，逗号分隔多值
      counterparty：对方（模糊）；names：多名称（逗号分隔，任一命中；names_exact=1 精确）
      account：对方账号/单号（模糊；account_exact=1 精确）
      bill_no：本单号/对方单号（模糊；bill_no_exact=1 精确）
      keyword：商品/单号/对方/账号 模糊
      date/month/year、start/end：时间维度
      month_day(MM-DD)/day_of_month(DD)/month_of_year(MM)：跨年周期过滤
      hour：小时（0-23，逗号分隔多个；跨日期按"几点"过滤）
      min_amount/max_amount：金额区间
    include_time=False 时忽略所有时间维度参数（用于周期对比等自建时段场景）。
    """
    conds: list[str] = []
    sql_params: list[Any] = []

    # 精确维度（支持逗号分隔多值）；counterparty 单独模糊匹配
    for col in ("source", "category", "direction", "pay_method", "status"):
        raw = _s(params, col)
        if not raw:
            continue
        vals = [v.strip() for v in raw.split(",") if v.strip()]
        if len(vals) == 1:
            conds.append(f"{col} = ?")
            sql_params.append(vals[0])
        elif vals:
            conds.append(f"{col} IN ({','.join('?' * len(vals))})")
            sql_params.extend(vals)

    # 对方（支持多个：逗号 / 分号 / 换行分隔，每个值各自作为一个条件，任一命中）
    #   counterparty_exact=1 精确匹配；names / names_exact 为等价别名
    cp_raw = _s(params, "counterparty")
    names_raw = _s(params, "names")
    cp_values: list[str] = []
    for raw in (cp_raw, names_raw):
        if raw:
            cp_values.extend([n.strip() for n in re.split(r"[,\n;]+", raw) if n.strip()])
    if cp_values:
        cp_exact = _flag(params, "counterparty_exact") or _flag(params, "names_exact")
        if cp_exact:
            conds.append("(" + " OR ".join("counterparty = ?" for _ in cp_values) + ")")
            sql_params.extend(cp_values)
        else:
            conds.append("(" + " OR ".join("counterparty LIKE ?" for _ in cp_values) + ")")
            sql_params.extend(f"%{v}%" for v in cp_values)

    # 对方账号 / 对方单号
    account = _s(params, "account")
    if account:
        if _flag(params, "account_exact"):
            conds.append("(counterparty_account = ? OR counterparty_bill_no = ?)")
            sql_params.extend([account, account])
        else:
            conds.append("(counterparty_account LIKE ? OR counterparty_bill_no LIKE ?)")
            sql_params.extend([f"%{account}%", f"%{account}%"])

    # 单号（本单号 / 对方单号）
    bill_no = _s(params, "bill_no")
    if bill_no:
        if _flag(params, "bill_no_exact"):
            conds.append("(bill_no = ? OR counterparty_bill_no = ?)")
            sql_params.extend([bill_no, bill_no])
        else:
            conds.append("(bill_no LIKE ? OR counterparty_bill_no LIKE ?)")
            sql_params.extend([f"%{bill_no}%", f"%{bill_no}%"])

    # 关键词（商品 / 单号 / 对方 / 账号 模糊）
    keyword = _s(params, "keyword")
    if keyword:
        like = f"%{keyword}%"
        conds.append(
            "(product LIKE ? OR bill_no LIKE ? OR counterparty LIKE ? "
            "OR counterparty_bill_no LIKE ? OR counterparty_account LIKE ?)"
        )
        sql_params.extend([like] * 5)

    # 时间维度
    if include_time:
        date = _s(params, "date")
        month = _s(params, "month")
        year = _s(params, "year")
        if date:
            conds.append("substr(bill_time,1,10) = ?")
            sql_params.append(date)
        elif month:
            conds.append("substr(bill_time,1,7) = ?")
            sql_params.append(month)
        elif year:
            conds.append("substr(bill_time,1,4) = ?")
            sql_params.append(year)
        start = _s(params, "start")
        end = _s(params, "end")
        if start:
            conds.append("bill_time >= ?")
            sql_params.append(start if len(start) > 10 else start + " 00:00:00")
        if end:
            conds.append("bill_time <= ?")
            sql_params.append(end if len(end) > 10 else end + " 23:59:59")

        # 跨年周期过滤：指定「月日」(MM-DD) / 「几号」(DD) / 「月份」(MM)
        month_day = _s(params, "month_day")
        if month_day:
            parts = re.split(r"[-/.]", month_day)
            if len(parts) == 2:
                try:
                    conds.append("substr(bill_time,6,5) = ?")
                    sql_params.append(f"{int(parts[0]):02d}-{int(parts[1]):02d}")
                except ValueError:
                    pass
        day_of_month = _s(params, "day_of_month")
        if day_of_month:
            try:
                conds.append("substr(bill_time,9,2) = ?")
                sql_params.append(f"{int(day_of_month):02d}")
            except ValueError:
                pass
        month_of_year = _s(params, "month_of_year")
        if month_of_year:
            try:
                conds.append("substr(bill_time,6,2) = ?")
                sql_params.append(f"{int(month_of_year):02d}")
            except ValueError:
                pass

        # 小时过滤：hour=12 或 hour=8,12,20（"点某根小时柱看这一时段的账单"用）
        hour_raw = _s(params, "hour")
        if hour_raw:
            hours: list[str] = []
            for v in re.split(r"[,\s]+", hour_raw):
                if not v:
                    continue
                try:
                    hours.append(f"{int(v) % 24:02d}")
                except ValueError:
                    continue
            if hours:
                conds.append(
                    f"substr(bill_time,12,2) IN ({','.join('?' * len(hours))})")
                sql_params.extend(hours)

    # 金额区间
    min_amount = _to_float_or_none(_s(params, "min_amount"))
    if min_amount is not None:
        conds.append("amount >= ?")
        sql_params.append(min_amount)
    max_amount = _to_float_or_none(_s(params, "max_amount"))
    if max_amount is not None:
        conds.append("amount <= ?")
        sql_params.append(max_amount)

    where_sql = (" WHERE " + " AND ".join(conds)) if conds else ""
    return where_sql, sql_params


def _agg_select() -> str:
    """收入 / 支出 / 净额 / 笔数 统一聚合表达式。"""
    return (
        "COUNT(*) AS count, "
        "COALESCE(SUM(CASE WHEN direction='收入' THEN amount ELSE 0 END),0) AS income, "
        "COALESCE(SUM(CASE WHEN direction='支出' THEN amount ELSE 0 END),0) AS expense, "
        "COALESCE(SUM(amount),0) AS amount_sum"
    )


def _scalar(conn: sqlite3.Connection, where_sql: str, sql_params: list[Any]) -> dict[str, Any]:
    """区间标量汇总：笔数 / 收入 / 支出 / 净额 / 极值 / 首末时间。"""
    row = conn.execute(
        f"SELECT {_agg_select()}, "
        f"COALESCE(MAX(amount),0) AS max_amount, COALESCE(MIN(amount),0) AS min_amount, "
        f"COALESCE(AVG(amount),0) AS avg_amount, MIN(bill_time) AS min_time, "
        f"MAX(bill_time) AS max_time FROM {TABLE_PERSONAL_BILLS}{where_sql}",
        sql_params,
    ).fetchone()
    d = dict(row) if row else {}
    if d:
        d["net"] = round((d.get("income") or 0) - (d.get("expense") or 0), 2)
    return d


def _bucket_rows(conn: sqlite3.Connection, where_sql: str, sql_params: list[Any],
                 expr: str, alias: str, mode: str = "normal") -> list[Any]:
    """按表达式 expr 分桶聚合。

    mode: minimal(极简:仅键) / simple(精简:键+笔数) /
          normal(一般:再含收支净额) / full(完全:再含合计与首末时间)
    """
    base = f"FROM {TABLE_PERSONAL_BILLS}{where_sql}"
    if mode == "minimal":
        return [r[0] for r in conn.execute(
            f"SELECT {expr} {base} GROUP BY {expr} ORDER BY {expr}", sql_params
        ).fetchall()]
    select = f"{expr} AS {alias}, COUNT(*) AS count"
    if mode in ("normal", "full"):
        select += (
            ", COALESCE(SUM(CASE WHEN direction='收入' THEN amount ELSE 0 END),0) AS income"
            ", COALESCE(SUM(CASE WHEN direction='支出' THEN amount ELSE 0 END),0) AS expense"
        )
    if mode == "full":
        select += (
            ", COALESCE(SUM(amount),0) AS amount_sum"
            ", MIN(bill_time) AS min_time, MAX(bill_time) AS max_time"
        )
    rows = [dict(r) for r in conn.execute(
        f"SELECT {select} {base} GROUP BY {expr} ORDER BY {expr}", sql_params
    ).fetchall()]
    if mode in ("normal", "full"):
        for d in rows:
            d["net"] = round((d.get("income") or 0) - (d.get("expense") or 0), 2)
    return rows


def _rows(conn: sqlite3.Connection, where_sql: str, sql_params: list[Any],
          limit: int = 100, offset: int = 0, order_by: str = "",
          fields: list[str] | None = None, include_summary: bool = True) -> dict[str, Any]:
    """明细结果（rows + total [+ summary]）。

    排序：order_by 支持 `列名` / `-列名`（'-' = 降序），列名必须在白名单内。
    **第二排序键恒为 id DESC** —— 单字段排序遇到相同值（同一秒的多笔、同金额）时
    SQLite 不保证顺序，翻页就会把同一条吐两次、另一条永远看不到；id 唯一 → 排序稳定。
    """
    tbl = TABLE_PERSONAL_BILLS
    safe_cols = set(BILL_COLUMNS) | {"id"}
    if order_by and order_by.lstrip("-") in safe_cols:
        desc = "DESC" if order_by.startswith("-") else "ASC"
        col = order_by.lstrip("-")
        # 按 id 本身排序时就没必要再加第二键了
        order_clause = f'ORDER BY "{col}" {desc}' + ("" if col == "id" else ", id DESC")
    else:
        order_clause = "ORDER BY bill_time DESC, id DESC"
    total = conn.execute(f"SELECT COUNT(*) FROM {tbl}{where_sql}", sql_params).fetchone()[0]
    rows = conn.execute(
        f"SELECT {_select_cols(fields)} FROM {tbl}{where_sql} {order_clause} LIMIT ? OFFSET ?",
        (*sql_params, limit, offset),
    ).fetchall()
    out: dict[str, Any] = {
        "rows": [dict(r) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }
    if include_summary:
        out["summary"] = _scalar(conn, where_sql, sql_params)
    return out


def _prev_range(period: str, granularity: str, yoy: bool
                ) -> tuple[tuple[str, str], tuple[str, str]]:
    """根据 period + granularity 计算 (本期起止, 上期起止) 两段日期。

    granularity: month(YYYY-MM) / day(YYYY-MM-DD) / year(YYYY)
    yoy=True 取去年同期，否则取上一期（环比）。
    """
    gran = (granularity or "month").strip().lower()
    try:
        if gran == "day":
            d = datetime.strptime(period, "%Y-%m-%d")
            cur = (d.strftime("%Y-%m-%d"), d.strftime("%Y-%m-%d"))
            d2 = d.replace(year=d.year - 1) if yoy else (d - timedelta(days=1))
            prev = (d2.strftime("%Y-%m-%d"), d2.strftime("%Y-%m-%d"))
            return cur, prev
        if gran == "year":
            y = int(period[:4])
            return (f"{y}-01-01", f"{y}-12-31"), (f"{y - 1}-01-01", f"{y - 1}-12-31")
        # month（默认）
        dt = datetime.strptime(period[:7], "%Y-%m")
        y, m = dt.year, dt.month
        last = calendar.monthrange(y, m)[1]
        cur = (f"{y:04d}-{m:02d}-01", f"{y:04d}-{m:02d}-{last:02d}")
        if yoy:
            py, pm = y - 1, m
        else:
            py, pm = (y - 1, 12) if m == 1 else (y, m - 1)
        last2 = calendar.monthrange(py, pm)[1]
        prev = (f"{py:04d}-{pm:02d}-01", f"{py:04d}-{pm:02d}-{last2:02d}")
        return cur, prev
    except Exception:
        raise ValueError(
            "period 与 granularity 不匹配（month: YYYY-MM，day: YYYY-MM-DD，year: YYYY）"
        )


# =========================================================================== #
#  查询 / 分析执行入口（在 executor 线程中调用）                                 #
# =========================================================================== #
def run_bill_query(db_path: str, params: dict[str, Any]) -> dict[str, Any]:
    """执行个人账单查询 / 分析。params 为请求参数字典（含 type）。

    type 取值见 BILL_QUERY_TYPES；未知类型抛 ValueError。
    """
    query_type = _s(params, "type").lower()
    if not query_type:
        raise ValueError("缺少 type 参数，可选: " + ", ".join(BILL_QUERY_TYPES))

    where_sql, sql_params = _build_filters(params)
    tbl = TABLE_PERSONAL_BILLS

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        # 1) 明细查询
        if query_type == "bill_history":
            return _rows(conn, where_sql, sql_params,
                         _int_param(params, "limit", 100, 1),
                         _int_param(params, "offset", 0),
                         _s(params, "order_by"), _fields(params))

        # 2) 最新一条
        if query_type == "bill_latest":
            row = conn.execute(
                f"SELECT {_select_cols(_fields(params))} FROM {tbl}{where_sql} "
                f"ORDER BY bill_time DESC, id DESC LIMIT 1", sql_params
            ).fetchone()
            return {"record": dict(row) if row else None}

        # 3) 数据可用性 / 周期清单
        if query_type == "bill_periods":
            level = (_s(params, "level").lower() or "all")
            if level not in ("all", "year", "month", "day"):
                raise ValueError("bill_periods 的 level 仅支持: all, year, month, day")
            mode = _parse_mode(params)
            result: dict[str, Any] = {"level": level, "mode": mode}
            if level in ("all", "year"):
                result["years"] = _bucket_rows(conn, where_sql, sql_params,
                                               "substr(bill_time,1,4)", "year", mode)
            if level in ("all", "month"):
                result["months"] = _bucket_rows(conn, where_sql, sql_params,
                                                "substr(bill_time,1,7)", "month", mode)
            if level in ("all", "day"):
                result["dates"] = _bucket_rows(conn, where_sql, sql_params,
                                               "substr(bill_time,1,10)", "date", mode)
            if mode != "minimal":
                row = conn.execute(
                    f"SELECT COUNT(*) AS total, MIN(bill_time) AS min_time, "
                    f"MAX(bill_time) AS max_time FROM {tbl}{where_sql}", sql_params
                ).fetchone()
                result["range"] = dict(row) if row else {}
            return result

        # 4) 汇总统计
        if query_type == "bill_summary":
            return _scalar(conn, where_sql, sql_params)

        # 4.1) 首页仪表盘：一次取全（总计 / 收支占比 / 年度收支 / 最近 N 日小计 + 明细）
        #   取数口径有两条时间轴，刻意分开：
        #     · 总计 / 年度收支 / 概览  → 吃全部过滤（含 year/month，用户选了年份就要收窄）
        #     · 最近 N 日               → 只吃维度过滤（source/category/…），
        #       基准日取「最新一笔账单的日期」而不是今天 —— 账单常是导入的历史账，
        #       按今天算会得到一串空值。
        if query_type == "bill_dashboard":
            days = _int_param(params, "days", 7, 2, 90)
            recent_limit = _int_param(params, "recent_limit", 80, 1, 500)

            dim_where, dim_params = _build_filters(params, include_time=False)
            latest = conn.execute(
                f"SELECT MAX(bill_time) FROM {tbl}{dim_where}", dim_params
            ).fetchone()[0]
            base = str(latest)[:10] if latest else datetime.now().strftime("%Y-%m-%d")
            try:
                base_dt = datetime.strptime(base, "%Y-%m-%d")
            except ValueError:
                base_dt = datetime.now()
                base = base_dt.strftime("%Y-%m-%d")
            start = (base_dt - timedelta(days=days - 1)).strftime("%Y-%m-%d")

            win_where = _append_cond(dim_where, "bill_time >= ?")
            win_params: list[Any] = [*dim_params, start + " 00:00:00"]
            win_where = _append_cond(win_where, "bill_time <= ?")
            win_params = [*win_params, base + " 23:59:59"]

            # 逐日汇总：补齐窗口内没有账单的日期（前端柱图要等距的 N 根柱子）
            day_map = {r["date"]: dict(r) for r in _bucket_rows(
                conn, win_where, win_params, "substr(bill_time,1,10)", "date", "normal")}
            day_list = []
            for i in range(days):
                key = (base_dt - timedelta(days=days - 1 - i)).strftime("%Y-%m-%d")
                day_list.append(day_map.get(key) or {
                    "date": key, "count": 0, "income": 0, "expense": 0, "net": 0})

            days_total = {"count": 0, "income": 0.0, "expense": 0.0, "net": 0.0}
            for item in day_list:
                days_total["count"] += item.get("count") or 0
                days_total["income"] += item.get("income") or 0
                days_total["expense"] += item.get("expense") or 0
                days_total["net"] += item.get("net") or 0
            for key in ("income", "expense", "net"):
                days_total[key] = round(days_total[key], 2)

            summary = _scalar(conn, where_sql, sql_params)
            income = summary.get("income") or 0
            expense = summary.get("expense") or 0
            base_sum = income + expense

            year_rows = conn.execute(
                f"SELECT substr(bill_time,1,4) AS key, {_agg_select()} FROM {tbl}{where_sql} "
                f"GROUP BY key ORDER BY key DESC", sql_params).fetchall()
            years_out = []
            for r in year_rows:
                d = dict(r)
                d["net"] = round((d.get("income") or 0) - (d.get("expense") or 0), 2)
                years_out.append(d)

            # 来源分布（前端 hero 下面那条"各来源占比"小条）：
            #   **刻意剔除 source 过滤**，其余条件照吃 —— 否则选中某个来源后
            #   这条就只剩自己一项，失去了"一眼看全部来源"的意义。
            #   amount = 该来源的流水总额（收入 + 支出），pct = 占总流水的比重。
            src_where, src_sql_params = _build_filters(
                {k: v for k, v in params.items() if k != "source"})
            src_rows = _bucket_rows(conn, src_where, src_sql_params, "source", "source", "normal")
            for d in src_rows:
                d["amount"] = round((d.get("income") or 0) + (d.get("expense") or 0), 2)
            src_total = sum(d["amount"] for d in src_rows)
            sources_out = []
            for d in sorted(src_rows, key=lambda x: x["amount"], reverse=True):
                sources_out.append({
                    "source": d.get("source") or "",
                    "count": d.get("count") or 0,
                    "income": round(d.get("income") or 0, 2),
                    "expense": round(d.get("expense") or 0, 2),
                    "net": d.get("net") or 0,
                    "amount": d["amount"],
                    "pct": round(d["amount"] / src_total * 100, 2) if src_total else 0,
                })

            meta_row = conn.execute(
                f"SELECT COUNT(*) AS total, MIN(bill_time) AS min_time, "
                f"MAX(bill_time) AS max_time, "
                f"COUNT(DISTINCT substr(bill_time,1,10)) AS days, "
                f"COUNT(DISTINCT counterparty) AS counterparties FROM {tbl}{where_sql}",
                sql_params).fetchone()

            recent = [dict(r) for r in conn.execute(
                f"SELECT * FROM {tbl}{win_where} ORDER BY bill_time DESC, id DESC LIMIT ?",
                (*win_params, recent_limit)).fetchall()]

            return {
                "days_window": days,
                "base_date": base,
                "start_date": start,
                "meta": dict(meta_row) if meta_row else {},
                "summary": summary,
                "ratio": {
                    "income_pct": round(income / base_sum * 100, 2) if base_sum else 0,
                    "expense_pct": round(expense / base_sum * 100, 2) if base_sum else 0,
                    "saving_rate": round((income - expense) / income * 100, 2) if income else None,
                },
                "years": years_out,
                "sources": sources_out,
                "days": day_list,
                "days_total": days_total,
                "recent": recent,
            }

        # 5) 分组汇总（多维度）
        if query_type == "bill_group":
            group_by = _s(params, "group_by").lower() or "category"
            if group_by in BILL_DIMENSIONS:
                group_expr = group_by
            elif group_by in _BILL_TIME_BUCKETS:
                group_expr = _BILL_TIME_BUCKETS[group_by]
            else:
                raise ValueError(
                    "bill_group 的 group_by 仅支持: "
                    + ", ".join(list(BILL_DIMENSIONS) + list(_BILL_TIME_BUCKETS))
                )
            limit = _int_param(params, "limit", 100, 1)
            rows = conn.execute(
                f"SELECT {group_expr} AS key, {_agg_select()} FROM {tbl}{where_sql} "
                f"GROUP BY {group_expr} ORDER BY amount_sum DESC LIMIT ?",
                (*sql_params, limit),
            ).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["net"] = round((d.get("income") or 0) - (d.get("expense") or 0), 2)
                out.append(d)
            return {"group_by": group_by, "rows": out}

        # 6) 趋势序列
        if query_type == "bill_trend":
            granularity = _s(params, "granularity").lower() or "month"
            group_expr = _BILL_TIME_BUCKETS.get(granularity)
            if not group_expr:
                raise ValueError("bill_trend 的 granularity 仅支持: year, month, day, hour")
            limit = _int_param(params, "limit", 2000, 1)
            rows = conn.execute(
                f"SELECT {group_expr} AS bucket, {_agg_select()} FROM {tbl}{where_sql} "
                f"GROUP BY {group_expr} ORDER BY bucket ASC LIMIT ?",
                (*sql_params, limit),
            ).fetchall()
            series = []
            for r in rows:
                d = dict(r)
                d["net"] = round((d.get("income") or 0) - (d.get("expense") or 0), 2)
                series.append(d)
            return {"granularity": granularity, "series": series}

        # 7) 排行榜
        if query_type == "bill_ranking":
            dimension = _s(params, "dimension").lower() or "counterparty"
            if dimension not in BILL_DIMENSIONS:
                raise ValueError(
                    "bill_ranking 的 dimension 仅支持: " + ", ".join(BILL_DIMENSIONS))
            by = _s(params, "by").lower() or "amount"
            order_field = "count" if by == "count" else "amount_sum"
            limit = _int_param(params, "limit", 20, 1)
            rows = conn.execute(
                f"SELECT {dimension} AS key, {_agg_select()} FROM {tbl}{where_sql} "
                f"GROUP BY {dimension} ORDER BY {order_field} DESC LIMIT ?",
                (*sql_params, limit),
            ).fetchall()
            return {"dimension": dimension, "by": by, "rows": [dict(r) for r in rows]}

        # 8) 可选值清单
        if query_type == "bill_options":
            return _options(conn)

        # 9) 按单号查询
        if query_type == "bill_by_no":
            if not _s(params, "bill_no"):
                raise ValueError("bill_by_no 需要 bill_no 参数")
            return _rows(conn, where_sql, sql_params,
                         _int_param(params, "limit", 200, 1),
                         _int_param(params, "offset", 0),
                         _s(params, "order_by"), _fields(params))

        # 10) 按对方账号 / 对方查询
        if query_type == "bill_by_account":
            if not (_s(params, "account") or _s(params, "counterparty")):
                raise ValueError("bill_by_account 需要 account 或 counterparty 参数")
            result = _rows(conn, where_sql, sql_params,
                           _int_param(params, "limit", 200, 1),
                           _int_param(params, "offset", 0),
                           _s(params, "order_by"), _fields(params))
            result["counterparties"] = [
                dict(r) for r in conn.execute(
                    f"SELECT counterparty AS key, {_agg_select()} FROM {tbl}{where_sql} "
                    f"GROUP BY counterparty ORDER BY amount_sum DESC LIMIT 50", sql_params
                ).fetchall()
            ]
            return result

        # 11) 多名称查询：按名称分组返回，可选明细（支持 fields 字段设置）
        if query_type == "bill_by_names":
            names_raw = _s(params, "names")
            if not names_raw:
                raise ValueError("bill_by_names 需要 names 参数（逗号分隔多个名称）")
            names = [n.strip() for n in re.split(r"[,\n;]+", names_raw) if n.strip()]
            if not names:
                raise ValueError("names 解析为空")
            n_exact = _flag(params, "names_exact")
            detail = _flag(params, "detail")
            fields = _fields(params)
            order_by = _s(params, "order_by")
            limit = _int_param(params, "limit", 100, 1, 2000)
            offset = _int_param(params, "offset", 0)
            groups = []
            matched = []
            for nm in names:
                cond = "counterparty = ?" if n_exact else "counterparty LIKE ?"
                extra = [nm] if n_exact else [f"%{nm}%"]
                w = _append_cond(where_sql, cond)
                p = [*sql_params, *extra]
                summary = _scalar(conn, w, p)
                group: dict[str, Any] = {"name": nm, "summary": summary}
                if summary.get("count"):
                    matched.append(nm)
                if detail:
                    group["rows"] = _rows(conn, w, p, limit, offset, order_by,
                                          fields, include_summary=False)["rows"]
                groups.append(group)
            return {"names": names, "matched_names": matched, "exact": n_exact,
                    "detail": detail, "fields": fields or "all", "groups": groups}

        # 12) 指定日交易
        if query_type == "bill_by_day":
            date = _s(params, "date")
            if not date:
                raise ValueError("bill_by_day 需要 date 参数 (YYYY-MM-DD)")
            hours_rows = _bucket_rows(conn, where_sql, sql_params,
                                      "substr(bill_time,12,2)", "hour", "normal")
            hmap = {r["hour"]: r for r in hours_rows}
            hours = [
                hmap.get(f"{h:02d}") or {"hour": f"{h:02d}", "count": 0,
                                          "income": 0, "expense": 0, "net": 0}
                for h in range(24)
            ]
            records = [dict(r) for r in conn.execute(
                f"SELECT {_select_cols(_fields(params))} FROM {tbl}{where_sql} "
                f"ORDER BY bill_time ASC, id ASC LIMIT ?",
                (*sql_params, _int_param(params, "record_limit", 1000, 1, 5000)),
            ).fetchall()]
            return {
                "date": date,
                "summary": _scalar(conn, where_sql, sql_params),
                "hours": hours,
                "categories": _bucket_rows(conn, where_sql, sql_params,
                                           "category", "category", "normal"),
                "pay_methods": _bucket_rows(conn, where_sql, sql_params,
                                            "pay_method", "pay_method", "normal"),
                "records": records,
            }

        # 13) 活跃日历
        if query_type == "bill_calendar":
            month = _s(params, "month")
            if not month:
                raise ValueError("bill_calendar 需要 month 参数 (YYYY-MM)")
            days = _bucket_rows(conn, where_sql, sql_params,
                                "substr(bill_time,1,10)", "date", "normal")
            if _flag(params, "fill"):
                try:
                    y, m = int(month[:4]), int(month[5:7])
                    dmap = {d["date"]: d for d in days}
                    days = [
                        dmap.get(f"{y:04d}-{m:02d}-{d:02d}")
                        or {"date": f"{y:04d}-{m:02d}-{d:02d}", "count": 0,
                            "income": 0, "expense": 0, "net": 0}
                        for d in range(1, calendar.monthrange(y, m)[1] + 1)
                    ]
                except Exception:
                    pass
            return {"month": month, "days": days,
                    "summary": _scalar(conn, where_sql, sql_params)}

        # 14) 最近 N 条明细
        if query_type == "bill_recent":
            limit = _int_param(params, "limit", 20, 1, 1000)
            return {"rows": [dict(r) for r in conn.execute(
                f"SELECT {_select_cols(_fields(params))} FROM {tbl}{where_sql} "
                f"ORDER BY bill_time DESC, id DESC LIMIT ?", (*sql_params, limit)
            ).fetchall()]}

        # 15) 单笔 Top N
        if query_type == "bill_top":
            order = _s(params, "order").lower()
            if order not in ("desc", "asc"):
                order = "desc"
            limit = _int_param(params, "limit", 20, 1, 500)
            base = _append_cond(where_sql, "amount IS NOT NULL")
            rows = [dict(r) for r in conn.execute(
                f"SELECT {_select_cols(_fields(params))} FROM {tbl}{base} "
                f"ORDER BY amount {order.upper()}, bill_time DESC, id DESC LIMIT ?",
                (*sql_params, limit)
            ).fetchall()]
            return {"order": order, "rows": rows}

        # 16) 小时分布
        if query_type == "bill_hour_dist":
            rows = _bucket_rows(conn, where_sql, sql_params,
                                "substr(bill_time,12,2)", "hour", "normal")
            hmap = {r["hour"]: r for r in rows}
            return {"hours": [
                hmap.get(f"{h:02d}") or {"hour": f"{h:02d}", "count": 0,
                                          "income": 0, "expense": 0, "net": 0}
                for h in range(24)
            ]}

        # 17) 星期分布
        if query_type == "bill_weekday_dist":
            rows = _bucket_rows(conn, where_sql, sql_params,
                                "strftime('%w', bill_time)", "wd", "normal")
            labels = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"]
            wmap = {str(r["wd"]): r for r in rows}
            out = []
            for i in range(7):
                item = wmap.get(str(i)) or {"wd": str(i), "count": 0,
                                            "income": 0, "expense": 0, "net": 0}
                item["weekday"] = i
                item["label"] = labels[i]
                out.append(item)
            return {"weekdays": out}

        # 18) 月内几号分布
        if query_type == "bill_dom_dist":
            rows = _bucket_rows(conn, where_sql, sql_params,
                                "substr(bill_time,9,2)", "day", "normal")
            dmap = {r["day"]: r for r in rows}
            return {"days": [
                dmap.get(f"{d:02d}") or {"day": f"{d:02d}", "count": 0,
                                         "income": 0, "expense": 0, "net": 0}
                for d in range(1, 32)
            ]}

        # 19) 金额区间分布
        if query_type == "bill_amount_dist":
            buckets_raw = _s(params, "buckets")
            if buckets_raw:
                try:
                    bounds = sorted({float(x) for x in re.split(r"[,\s]+", buckets_raw) if x.strip()})
                except ValueError:
                    raise ValueError("buckets 需为逗号分隔的数字边界，如 0,10,50,100,500,1000")
            else:
                step = _to_float_or_none(_s(params, "step")) or 100.0
                if step <= 0:
                    step = 100.0
                maxv = conn.execute(
                    f"SELECT COALESCE(MAX(amount),0) FROM {tbl}{where_sql}", sql_params
                ).fetchone()[0] or 0
                n = min(50, int(maxv // step) + 1)
                bounds = [i * step for i in range(n + 1)]
            if not bounds:
                bounds = [0.0]
            case_sql = "CASE"
            labels = []
            for i, lo in enumerate(bounds):
                if i + 1 < len(bounds):
                    hi = bounds[i + 1]
                    case_sql += f" WHEN amount >= {lo} AND amount < {hi} THEN '{lo}-{hi}'"
                    labels.append(f"{lo}-{hi}")
                else:
                    case_sql += f" WHEN amount >= {lo} THEN '>= {lo}'"
                    labels.append(f">= {lo}")
            case_sql += " ELSE 'other' END"
            rows = _bucket_rows(conn, where_sql, sql_params, case_sql, "bucket", "normal")
            bmap = {r["bucket"]: r for r in rows}
            return {"bounds": bounds, "buckets": [
                bmap.get(lab) or {"bucket": lab, "count": 0,
                                  "income": 0, "expense": 0, "net": 0}
                for lab in labels
            ]}

        # 20) 周期对比
        if query_type == "bill_compare":
            dim_where, dim_params = _build_filters(params, include_time=False)
            s1, e1 = _s(params, "start1"), _s(params, "end1")
            s2, e2 = _s(params, "start2"), _s(params, "end2")
            yoy = _flag(params, "yoy")
            if s1 or e1 or s2 or e2:
                range_a, range_b = (s1, e1), (s2, e2)
            else:
                period = _s(params, "period")
                if not period:
                    raise ValueError(
                        "bill_compare 需要 period（配合 granularity）或 start1/end1/start2/end2")
                range_a, range_b = _prev_range(
                    period, _s(params, "granularity") or "month", yoy)

            def _seg(rng: tuple[str, str]) -> dict[str, Any]:
                w, p = dim_where, list(dim_params)
                if rng[0]:
                    w = _append_cond(w, "bill_time >= ?")
                    p.append(rng[0] if len(rng[0]) > 10 else rng[0] + " 00:00:00")
                if rng[1]:
                    w = _append_cond(w, "bill_time <= ?")
                    p.append(rng[1] if len(rng[1]) > 10 else rng[1] + " 23:59:59")
                return _scalar(conn, w, p)

            a = _seg(range_a)
            b = _seg(range_b)

            def _chg(cur: float, base: float):
                return None if not base else round((cur - base) / abs(base) * 100, 2)

            return {
                "comparison": "同比" if yoy else "环比",
                "a": {"range": list(range_a), "summary": a},
                "b": {"range": list(range_b), "summary": b},
                "delta": {
                    "count": (a.get("count") or 0) - (b.get("count") or 0),
                    "income": round((a.get("income") or 0) - (b.get("income") or 0), 2),
                    "expense": round((a.get("expense") or 0) - (b.get("expense") or 0), 2),
                    "net": round((a.get("net") or 0) - (b.get("net") or 0), 2),
                },
                "change_pct": {
                    "count": _chg(a.get("count") or 0, b.get("count") or 0),
                    "income": _chg(a.get("income") or 0, b.get("income") or 0),
                    "expense": _chg(a.get("expense") or 0, b.get("expense") or 0),
                    "net": _chg(a.get("net") or 0, b.get("net") or 0),
                },
            }

        # 21) 对方 / 账号画像（支持多名称）
        if query_type == "bill_profile":
            names_raw = _s(params, "names")
            if not (names_raw or _s(params, "counterparty") or _s(params, "account")):
                raise ValueError("bill_profile 需要 counterparty / account / names 参数")
            recent_fields = _fields(params)

            def _profile(w: str, p: list[Any]) -> dict[str, Any]:
                return {
                    "summary": _scalar(conn, w, p),
                    "by_year": _bucket_rows(conn, w, p, "substr(bill_time,1,4)", "year", "normal"),
                    "by_month": _bucket_rows(conn, w, p, "substr(bill_time,1,7)", "month", "normal"),
                    "by_category": _bucket_rows(conn, w, p, "category", "category", "normal"),
                    "by_pay_method": _bucket_rows(conn, w, p, "pay_method", "pay_method", "normal"),
                    "by_direction": _bucket_rows(conn, w, p, "direction", "direction", "normal"),
                    "by_hour": _bucket_rows(conn, w, p, "substr(bill_time,12,2)", "hour", "normal"),
                    "recent": _rows(conn, w, p, 20, 0, "", recent_fields,
                                    include_summary=False)["rows"],
                }

            if names_raw:
                names = [n.strip() for n in re.split(r"[,\n;]+", names_raw) if n.strip()]
                n_exact = _flag(params, "names_exact")
                profiles = []
                for nm in names:
                    cond = "counterparty = ?" if n_exact else "counterparty LIKE ?"
                    extra = [nm] if n_exact else [f"%{nm}%"]
                    profiles.append({"name": nm,
                                     **_profile(_append_cond(where_sql, cond),
                                                [*sql_params, *extra])})
                return {"names": names, "exact": n_exact, "profiles": profiles}

            result = _profile(where_sql, sql_params)
            result["filter"] = {"counterparty": _s(params, "counterparty"),
                                "account": _s(params, "account")}
            return result

        # 22) 数据概览
        if query_type == "bill_meta":
            row = conn.execute(
                f"SELECT COUNT(*) AS total, MIN(bill_time) AS min_time, "
                f"MAX(bill_time) AS max_time, "
                f"COUNT(DISTINCT substr(bill_time,1,10)) AS days, "
                f"COUNT(DISTINCT counterparty) AS counterparties, "
                f"COUNT(DISTINCT bill_no) AS bill_nos FROM {tbl}{where_sql}", sql_params
            ).fetchone()

            def _distinct(col: str) -> list[str]:
                w = _append_cond(where_sql, f"{col} != ''")
                return [r[0] for r in conn.execute(
                    f"SELECT DISTINCT {col} FROM {tbl}{w} ORDER BY {col}", sql_params).fetchall()]

            return {
                "overview": dict(row) if row else {},
                "sources": _distinct("source"),
                "categories": _distinct("category"),
                "directions": _distinct("direction"),
                "pay_methods": _distinct("pay_method"),
                "statuses": _distinct("status"),
                "summary": _scalar(conn, where_sql, sql_params),
            }

        # 23) 数据质量检查
        if query_type == "bill_quality":
            def _cnt(cond: str) -> int:
                return conn.execute(
                    f"SELECT COUNT(*) FROM {tbl}{_append_cond(where_sql, cond)}", sql_params
                ).fetchone()[0]

            dup_w = _append_cond(where_sql, "bill_no != ''")
            dups = [dict(r) for r in conn.execute(
                f"SELECT bill_no AS key, COUNT(*) AS count, "
                f"MIN(bill_time) AS min_time, MAX(bill_time) AS max_time "
                f"FROM {tbl}{dup_w} GROUP BY bill_no HAVING COUNT(*) > 1 "
                f"ORDER BY count DESC LIMIT ?", (*sql_params, 50)).fetchall()]
            return {
                "total": _cnt("1=1"),
                "empty_time": _cnt("bill_time = ''"),
                "null_amount": _cnt("amount IS NULL"),
                "zero_amount": _cnt("amount = 0"),
                "empty_source": _cnt("source = ''"),
                "empty_category": _cnt("category = ''"),
                "empty_counterparty": _cnt("counterparty = ''"),
                "negative_amount": _cnt("amount < 0"),
                "duplicate_bill_no": {"groups": len(dups), "samples": dups},
            }

        # 24) 指定月日 / 几号 → 各年汇总
        if query_type == "bill_month_day_years":
            if not (_s(params, "month_day") or _s(params, "day_of_month")):
                raise ValueError("bill_month_day_years 需要 month_day(MM-DD) 或 day_of_month(DD) 参数")
            mode = _parse_mode(params)
            return {
                "month_day": _s(params, "month_day"),
                "day_of_month": _s(params, "day_of_month"),
                "mode": mode,
                "years": _bucket_rows(conn, where_sql, sql_params,
                                      "substr(bill_time,1,4)", "year", mode),
            }

        # 24.1) 历史今日（首页主区块）：今天的「月-日」在往年各天的账单
        #   默认 month_day = 今天（MM-DD），可用 month_day / day_of_month 覆盖；
        #   detail 默认 1（返回每年明细），detail=0 只返回按年汇总；
        #   only_past=1 只看往年（排除今年）；limit = 每年明细条数上限。
        if query_type in ("bill_onthisday", "bill_today_years"):
            month_day = _s(params, "month_day")
            dom = _s(params, "day_of_month")
            base_date = datetime.now().strftime("%Y-%m-%d")
            if not month_day and not dom:
                month_day = datetime.now().strftime("%m-%d")
            # 注入默认月日后重算过滤条件（其余维度照旧生效）
            p2 = dict(params)
            p2["month_day"] = month_day
            w, wp = _build_filters(p2)
            mode = _parse_mode(params)
            detail = _s(params, "detail").lower() not in ("0", "false", "no")
            only_past = _flag(params, "only_past")
            fields = _fields(params)
            per_year_limit = _int_param(params, "limit", 50, 1, 1000)
            order_by = _s(params, "order_by")
            current_year = base_date[:4]

            year_rows = [dict(r) for r in conn.execute(
                f"SELECT substr(bill_time,1,4) AS year, {_agg_select()} "
                f"FROM {tbl}{w} GROUP BY substr(bill_time,1,4) ORDER BY year DESC", wp
            ).fetchall()]

            years_out: list[Any] = []
            flat_rows: list[dict[str, Any]] = []
            total = {"count": 0, "income": 0.0, "expense": 0.0, "amount_sum": 0.0}
            for yr in year_rows:
                year = str(yr.get("year") or "")
                if only_past and year == current_year:
                    continue
                if mode == "minimal":
                    years_out.append(year)
                    continue
                item = dict(yr)
                item["net"] = round((item.get("income") or 0) - (item.get("expense") or 0), 2)
                item["date"] = f"{year}-{month_day}" if month_day else f"{year}-{dom}"
                if detail:
                    cond = _append_cond(w, "substr(bill_time,1,4) = ?")
                    item["rows"] = _rows(conn, cond, [*wp, year], per_year_limit, 0,
                                         order_by, fields, include_summary=False)["rows"]
                    flat_rows.extend(item["rows"])
                years_out.append(item)
                total["count"] += item.get("count") or 0
                total["income"] += item.get("income") or 0
                total["expense"] += item.get("expense") or 0
                total["amount_sum"] += item.get("amount_sum") or 0
            total["income"] = round(total["income"], 2)
            total["expense"] = round(total["expense"], 2)
            total["amount_sum"] = round(total["amount_sum"], 2)
            total["net"] = round(total["income"] - total["expense"], 2)
            return {
                "on_this_day": month_day,
                "day_of_month": dom,
                "base_date": base_date,
                "mode": mode,
                "detail": detail,
                "only_past": only_past,
                "fields": fields or "all",
                "years_count": len(years_out),
                "total": total,
                "years": years_out,
                "rows": flat_rows,
            }

        # 25) 交叉汇总（行维度 × 列维度）
        if query_type == "bill_crosstab":
            row_dim = _s(params, "row_dim") or "category"
            col_dim = _s(params, "col_dim") or "month"
            row_expr = _dim_expr(row_dim)
            col_expr = _dim_expr(col_dim)
            rows = [dict(r) for r in conn.execute(
                f"SELECT {row_expr} AS row_key, {col_expr} AS col_key, {_agg_select()} "
                f"FROM {tbl}{where_sql} GROUP BY {row_expr}, {col_expr} "
                f"ORDER BY row_key, col_key", sql_params
            ).fetchall()]
            for r in rows:
                r["net"] = round((r.get("income") or 0) - (r.get("expense") or 0), 2)
            row_keys = sorted({r["row_key"] for r in rows}, key=lambda x: (x is None, x))
            col_keys = sorted({r["col_key"] for r in rows}, key=lambda x: (x is None, x))
            idx = {(r["row_key"], r["col_key"]): r for r in rows}
            matrix = [
                {"row_key": rk,
                 "cells": {ck: (idx.get((rk, ck)) or {"count": 0, "income": 0,
                                                      "expense": 0, "net": 0})
                           for ck in col_keys}}
                for rk in row_keys
            ]
            return {"row_dim": row_dim, "col_dim": col_dim,
                    "row_keys": row_keys, "col_keys": col_keys,
                    "rows": rows, "matrix": matrix}

        # 26) 时段热力图（星期 × 小时）
        if query_type == "bill_heatmap":
            rows = [dict(r) for r in conn.execute(
                f"SELECT strftime('%w', bill_time) AS wd, substr(bill_time,12,2) AS hour, "
                f"{_agg_select()} FROM {tbl}{where_sql} "
                f"GROUP BY strftime('%w', bill_time), substr(bill_time,12,2)", sql_params
            ).fetchall()]
            matrix = [[0] * 24 for _ in range(7)]
            for r in rows:
                try:
                    wd = int(r["wd"])
                    hh = int(r["hour"])
                except (TypeError, ValueError):
                    continue
                if 0 <= wd < 7 and 0 <= hh < 24:
                    matrix[wd][hh] = r["count"]
            return {"labels": ["周日", "周一", "周二", "周三", "周四", "周五", "周六"],
                    "matrix": matrix, "rows": rows}

        # ------------------------------------------------------------ #
        #  以下为「分类汇总 / 深度分析」扩展接口                          #
        # ------------------------------------------------------------ #
        # 27) 多值分类汇总：把输入的多个值各作为一个条件，分别汇总（按输入分类）
        #     例：dim=counterparty&values=美团,滴滴,京东 → 每个值一组
        if query_type == "bill_split":
            dim = _s(params, "dim").lower() or "counterparty"
            values_raw = _s(params, "values") or _s(params, "counterparty") or _s(params, "names")
            if not values_raw:
                raise ValueError("bill_split 需要 values 参数（逗号分隔多个值）")
            values = [v.strip() for v in re.split(r"[,\n;]+", values_raw) if v.strip()]
            if not values:
                raise ValueError("values 解析为空")
            exact = _flag(params, "exact") or _flag(params, "counterparty_exact") \
                or _flag(params, "names_exact")
            detail = _flag(params, "detail")
            fields = _fields(params)
            limit = _int_param(params, "limit", 100, 1, 2000)
            offset = _int_param(params, "offset", 0)
            order_by = _s(params, "order_by")
            # 若是用 counterparty/names 作为值来源，where 里已含 OR，这里再 AND 单值即可
            groups = []
            matched: list[str] = []
            total = {"count": 0, "income": 0.0, "expense": 0.0, "amount_sum": 0.0}
            for v in values:
                cond, extra = _dim_cond(dim, v, exact)
                w = _append_cond(where_sql, cond)
                p = [*sql_params, *extra]
                s = _scalar(conn, w, p)
                group: dict[str, Any] = {"value": v, "summary": s}
                if s.get("count"):
                    matched.append(v)
                    total["count"] += s.get("count") or 0
                    total["income"] += s.get("income") or 0
                    total["expense"] += s.get("expense") or 0
                    total["amount_sum"] += s.get("amount_sum") or 0
                if detail:
                    group["rows"] = _rows(conn, w, p, limit, offset, order_by,
                                          fields, include_summary=False)["rows"]
                groups.append(group)
            total["income"] = round(total["income"], 2)
            total["expense"] = round(total["expense"], 2)
            total["amount_sum"] = round(total["amount_sum"], 2)
            total["net"] = round(total["income"] - total["expense"], 2)
            return {"dim": dim, "values": values, "exact": exact, "detail": detail,
                    "fields": fields or "all", "matched_values": matched,
                    "total": total, "groups": groups}

        # 28) 首末交易：每个维度值的首次 / 最近一次交易
        if query_type == "bill_first_last":
            dim = _s(params, "dim").lower() or "counterparty"
            expr = _dim_expr(dim)
            limit = _int_param(params, "limit", 100, 1, 1000)
            order = _s(params, "order").lower()
            order_field = {"first": "first_time", "amount": "amount_sum",
                           "count": "count"}.get(order, "last_time")
            rows = conn.execute(
                f"SELECT {expr} AS key, COUNT(*) AS count, "
                f"COALESCE(SUM(CASE WHEN direction='收入' THEN amount ELSE 0 END),0) AS income, "
                f"COALESCE(SUM(CASE WHEN direction='支出' THEN amount ELSE 0 END),0) AS expense, "
                f"COALESCE(SUM(amount),0) AS amount_sum, "
                f"MIN(bill_time) AS first_time, MAX(bill_time) AS last_time "
                f"FROM {tbl}{where_sql} GROUP BY {expr} "
                f"ORDER BY {order_field} DESC LIMIT ?", (*sql_params, limit)
            ).fetchall()
            return {"dim": dim, "order": order or "last", "rows": [dict(r) for r in rows]}

        # 29) 交易间隔分析：同一维度值相邻交易的间隔天数
        if query_type == "bill_interval":
            dim = _s(params, "dim").lower() or "counterparty"
            expr = _dim_expr(dim)
            min_count = _int_param(params, "min_count", 3, 2, 10000)
            limit = _int_param(params, "limit", 100, 1, 1000)
            sub = (f"SELECT {expr} AS key, bill_time, "
                   f"LAG(bill_time) OVER (PARTITION BY {expr} ORDER BY bill_time) AS prev_time "
                   f"FROM {tbl}{where_sql}")
            rows = conn.execute(
                f"SELECT key, COUNT(*) AS interval_count, "
                f"ROUND(AVG(julianday(bill_time) - julianday(prev_time)),2) AS avg_days, "
                f"ROUND(MIN(julianday(bill_time) - julianday(prev_time)),2) AS min_days, "
                f"ROUND(MAX(julianday(bill_time) - julianday(prev_time)),2) AS max_days "
                f"FROM ({sub}) WHERE prev_time IS NOT NULL GROUP BY key "
                f"HAVING COUNT(*) >= ? ORDER BY avg_days ASC LIMIT ?",
                (*sql_params, min_count, limit)
            ).fetchall()
            return {"dim": dim, "min_count": min_count, "rows": [dict(r) for r in rows]}

        # 30) 交易频率排行（笔数 / 活跃天数 / 跨期 / 日均）
        if query_type == "bill_frequency":
            dim = _s(params, "dim").lower() or "counterparty"
            expr = _dim_expr(dim)
            limit = _int_param(params, "limit", 100, 1, 1000)
            rows = [dict(r) for r in conn.execute(
                f"SELECT {expr} AS key, COUNT(*) AS count, "
                f"COUNT(DISTINCT substr(bill_time,1,10)) AS active_days, "
                f"COUNT(DISTINCT substr(bill_time,1,7)) AS active_months, "
                f"MIN(bill_time) AS first_time, MAX(bill_time) AS last_time, "
                f"ROUND(julianday(MAX(bill_time)) - julianday(MIN(bill_time)),1) AS span_days, "
                f"COALESCE(SUM(amount),0) AS amount_sum "
                f"FROM {tbl}{where_sql} GROUP BY {expr} ORDER BY count DESC LIMIT ?",
                (*sql_params, limit)
            ).fetchall()]
            for d in rows:
                span = d.get("span_days") or 0
                d["per_month"] = round((d["count"] or 0) / max(d.get("active_months") or 1, 1), 2)
                d["per_day"] = round((d["count"] or 0) / (span + 1), 3) if span else None
            return {"dim": dim, "rows": rows}

        # 31) 净现金流（按粒度，含累计净额）
        if query_type == "bill_netflow":
            granularity = _s(params, "granularity").lower() or "month"
            expr = _BILL_TIME_BUCKETS.get(granularity)
            if not expr or granularity == "weekday":
                raise ValueError("bill_netflow 的 granularity 仅支持: year, month, day, hour")
            limit = _int_param(params, "limit", 5000, 1)
            rows = _bucket_rows(conn, where_sql, sql_params, expr, "bucket", "normal")
            cum = 0.0
            series = []
            for r in rows[:limit]:
                cum = round(cum + (r.get("net") or 0), 2)
                r["cumulative_net"] = cum
                series.append(r)
            return {"granularity": granularity, "net_total": cum, "series": series}

        # 32) 收支对比（比例 + 趋势）
        if query_type == "bill_income_expense":
            s = _scalar(conn, where_sql, sql_params)
            income = s.get("income") or 0
            expense = s.get("expense") or 0
            base = income + expense
            granularity = _s(params, "granularity").lower() or "month"
            expr = _BILL_TIME_BUCKETS.get(granularity)
            series = _bucket_rows(conn, where_sql, sql_params, expr, "bucket", "normal") \
                if expr and granularity != "weekday" else []
            return {
                "summary": s,
                "ratio": {
                    "income_pct": round(income / base * 100, 2) if base else 0,
                    "expense_pct": round(expense / base * 100, 2) if base else 0,
                    "saving_rate": round((income - expense) / income * 100, 2) if income else None,
                },
                "granularity": granularity,
                "series": series,
            }

        # 33) 交易状态分布
        if query_type == "bill_status_dist":
            rows = [dict(r) for r in conn.execute(
                f"SELECT CASE WHEN status='' THEN '未填写' ELSE status END AS key, "
                f"{_agg_select()} FROM {tbl}{where_sql} GROUP BY key ORDER BY amount_sum DESC",
                sql_params
            ).fetchall()]
            for r in rows:
                r["net"] = round((r.get("income") or 0) - (r.get("expense") or 0), 2)
            return {"rows": rows}

        # 34) 商品 / 说明 Top N
        if query_type == "bill_product_top":
            by = _s(params, "by").lower() or "amount"
            order_field = "count" if by == "count" else "amount_sum"
            limit = _int_param(params, "limit", 20, 1, 500)
            rows = [dict(r) for r in conn.execute(
                f"SELECT CASE WHEN product='' THEN '(空)' ELSE product END AS key, "
                f"{_agg_select()} FROM {tbl}{where_sql} GROUP BY key "
                f"ORDER BY {order_field} DESC LIMIT ?", (*sql_params, limit)
            ).fetchall()]
            return {"by": by, "rows": rows}

        # 35) 季节性（1-12 月跨年汇总）
        if query_type == "bill_seasonal":
            rows = {r["month"]: dict(r) for r in conn.execute(
                f"SELECT substr(bill_time,6,2) AS month, {_agg_select()} "
                f"FROM {tbl}{where_sql} GROUP BY month", sql_params
            ).fetchall()}
            season_months: list[Any] = []
            for i in range(1, 13):
                k = f"{i:02d}"
                item = rows.get(k) or {"month": k, "count": 0, "income": 0,
                                       "expense": 0, "amount_sum": 0}
                item["month"] = k
                item["net"] = round((item.get("income") or 0) - (item.get("expense") or 0), 2)
                season_months.append(item)
            return {"months": season_months}

        # 36) 逐年同月对比（同比矩阵）
        if query_type == "bill_yoy":
            rows = [dict(r) for r in conn.execute(
                f"SELECT substr(bill_time,1,4) AS year, substr(bill_time,6,2) AS month, "
                f"COALESCE(SUM(CASE WHEN direction='收入' THEN amount ELSE 0 END),0) AS income, "
                f"COALESCE(SUM(CASE WHEN direction='支出' THEN amount ELSE 0 END),0) AS expense, "
                f"COUNT(*) AS count FROM {tbl}{where_sql} "
                f"GROUP BY year, month ORDER BY year, month", sql_params
            ).fetchall()]
            data: dict[str, dict[str, Any]] = {}
            for r in rows:
                data.setdefault(r["year"], {})[r["month"]] = r
            years = sorted(data)
            out_years = []
            for idx, y in enumerate(years):
                prev = years[idx - 1] if idx > 0 else None
                month_cells: dict[str, Any] = {}
                y_exp = 0.0
                y_net = 0.0
                for i in range(1, 13):
                    k = f"{i:02d}"
                    cur = data[y].get(k)
                    if not cur:
                        month_cells[k] = None
                        continue
                    net = round((cur.get("income") or 0) - (cur.get("expense") or 0), 2)
                    entry: dict[str, Any] = {"count": cur["count"], "income": cur["income"],
                                             "expense": cur["expense"], "net": net,
                                             "yoy_expense_pct": None}
                    prev_row = data.get(prev, {}).get(k) if prev else None
                    if prev_row and prev_row.get("expense"):
                        entry["yoy_expense_pct"] = round(
                            (cur["expense"] - prev_row["expense"]) / prev_row["expense"] * 100, 2)
                    month_cells[k] = entry
                    y_exp += cur["expense"] or 0
                    y_net += net
                out_years.append({"year": y, "expense": round(y_exp, 2),
                                  "net": round(y_net, 2), "months": month_cells})
            return {"years": out_years}

        # 37) 异常 / 大额交易（组内均值倍数）
        if query_type == "bill_anomaly":
            dim = _s(params, "dim").lower() or "category"
            expr = _dim_expr(dim)
            factor = _to_float_or_none(_s(params, "factor")) or 3.0
            if factor <= 0:
                factor = 3.0
            min_count = _int_param(params, "min_count", 3, 2, 10000)
            limit = _int_param(params, "limit", 100, 1, 1000)
            base = _append_cond(where_sql, "amount IS NOT NULL")
            rows = [dict(r) for r in conn.execute(
                f"SELECT * FROM (SELECT *, "
                f"AVG(amount) OVER (PARTITION BY {expr}) AS grp_avg, "
                f"COUNT(*) OVER (PARTITION BY {expr}) AS grp_count "
                f"FROM {tbl}{base}) "
                f"WHERE grp_avg > 0 AND grp_count >= ? AND amount >= grp_avg * ? "
                f"ORDER BY amount DESC LIMIT ?",
                (*sql_params, min_count, factor, limit)
            ).fetchall()]
            for r in rows:
                avg = r.get("grp_avg") or 0
                r["ratio"] = round((r.get("amount") or 0) / avg, 2) if avg else None
            return {"dim": dim, "factor": factor, "min_count": min_count, "rows": rows}

        # 38) 疑似定期 / 订阅支出（间隔接近月/周）
        if query_type == "bill_recurring":
            dim = _s(params, "dim").lower() or "counterparty"
            expr = _dim_expr(dim)
            min_count = _int_param(params, "min_count", 3, 2, 1000)
            min_days = _to_float_or_none(_s(params, "min_days")) or 26.0
            max_days = _to_float_or_none(_s(params, "max_days")) or 35.0
            limit = _int_param(params, "limit", 100, 1, 1000)
            sub = (f"SELECT {expr} AS key, bill_time, amount, direction, "
                   f"LAG(bill_time) OVER (PARTITION BY {expr} ORDER BY bill_time) AS prev_time "
                   f"FROM {tbl}{where_sql}")
            rows = [dict(r) for r in conn.execute(
                f"SELECT key, COUNT(*) AS interval_count, "
                f"ROUND(AVG(julianday(bill_time) - julianday(prev_time)),2) AS avg_days, "
                f"ROUND(MIN(julianday(bill_time) - julianday(prev_time)),2) AS min_days, "
                f"ROUND(MAX(julianday(bill_time) - julianday(prev_time)),2) AS max_days, "
                f"ROUND(AVG(amount),2) AS avg_amount, ROUND(SUM(amount),2) AS amount_sum "
                f"FROM ({sub}) WHERE prev_time IS NOT NULL GROUP BY key "
                f"HAVING COUNT(*) >= ? "
                f"AND AVG(julianday(bill_time) - julianday(prev_time)) BETWEEN ? AND ? "
                f"ORDER BY avg_days ASC LIMIT ?",
                (*sql_params, min_count, min_days, max_days, limit)
            ).fetchall()]
            return {"dim": dim, "min_count": min_count,
                    "interval_range": [min_days, max_days], "rows": rows}

        # 39) 固定开销（在多个不同月份都出现的维度值）
        if query_type == "bill_fixed_cost":
            dim = _s(params, "dim").lower() or "counterparty"
            expr = _dim_expr(dim)
            min_months = _int_param(params, "min_months", 6, 2, 120)
            limit = _int_param(params, "limit", 100, 1, 1000)
            rows = [dict(r) for r in conn.execute(
                f"SELECT {expr} AS key, COUNT(*) AS count, "
                f"COUNT(DISTINCT substr(bill_time,1,7)) AS active_months, "
                f"ROUND(AVG(amount),2) AS avg_amount, ROUND(SUM(amount),2) AS amount_sum, "
                f"MIN(bill_time) AS first_time, MAX(bill_time) AS last_time "
                f"FROM {tbl}{where_sql} GROUP BY {expr} "
                f"HAVING active_months >= ? ORDER BY avg_amount DESC LIMIT ?",
                (*sql_params, min_months, limit)
            ).fetchall()]
            for d in rows:
                d["est_monthly_cost"] = round(
                    (d.get("amount_sum") or 0) / max(d.get("active_months") or 1, 1), 2)
            return {"dim": dim, "min_months": min_months, "rows": rows}

        # 40) 资金流向（两维度之间的流量，可用于桑基图）
        if query_type == "bill_flow":
            from_dim = _s(params, "from_dim").lower() or "counterparty"
            to_dim = _s(params, "to_dim").lower() or "category"
            fexpr = _dim_expr(from_dim)
            texpr = _dim_expr(to_dim)
            limit = _int_param(params, "limit", 500, 1, 5000)
            rows = [dict(r) for r in conn.execute(
                f"SELECT {fexpr} AS source, {texpr} AS target, {_agg_select()} "
                f"FROM {tbl}{where_sql} GROUP BY source, target "
                f"ORDER BY amount_sum DESC LIMIT ?", (*sql_params, limit)
            ).fetchall()]
            nodes = sorted({r["source"] for r in rows} | {r["target"] for r in rows},
                           key=lambda x: (x is None, x))
            return {"from_dim": from_dim, "to_dim": to_dim, "nodes": nodes,
                    "links": [{"source": r["source"], "target": r["target"],
                               "count": r["count"], "income": r["income"],
                               "expense": r["expense"], "amount_sum": r["amount_sum"]}
                              for r in rows]}

        # 41) 金额分位数（P25/P50/P75/P90/P95/P99）
        if query_type == "bill_percentile":
            base = _append_cond(where_sql, "amount IS NOT NULL")
            vals = [r[0] for r in conn.execute(
                f"SELECT amount FROM {tbl}{base} ORDER BY amount ASC", sql_params
            ).fetchall()]
            n = len(vals)

            def _pct(pf: float):
                if not n:
                    return None
                if n == 1:
                    return round(vals[0], 2)
                k = (n - 1) * pf
                lo = int(k)
                hi = min(lo + 1, n - 1)
                return round(vals[lo] + (vals[hi] - vals[lo]) * (k - lo), 2)

            return {
                "count": n,
                "min": round(vals[0], 2) if n else None,
                "p25": _pct(0.25), "p50": _pct(0.50), "p75": _pct(0.75),
                "p90": _pct(0.90), "p95": _pct(0.95), "p99": _pct(0.99),
                "max": round(vals[-1], 2) if n else None,
            }

        # 42) 备注候选清单：去重 + 使用次数（供前端点击选择，不用手打）
        if query_type == "bill_notes":
            # 备注关键词用 note_keyword（兼容 keyword）；注意通用 keyword 是对「交易字段」
            # 的关键词，会把结果过滤掉，这里重算不含 keyword 的条件。
            note_kw = _s(params, "note_keyword") or _s(params, "keyword")
            sub_params = {k: v for k, v in params.items() if k != "keyword"}
            w, p = _build_filters(sub_params)
            base = _append_cond(w, "note != ''")
            note_params = list(p)
            if note_kw:
                base = _append_cond(base, "note LIKE ?")
                note_params.append(f"%{note_kw}%")
            limit = _int_param(params, "limit", 50, 1, 500)
            rows = [dict(r) for r in conn.execute(
                f"SELECT note AS note, COUNT(*) AS count, "
                f"MIN(bill_time) AS first_time, MAX(bill_time) AS last_time "
                f"FROM {tbl}{base} GROUP BY note ORDER BY count DESC, note ASC LIMIT ?",
                (*note_params, limit)
            ).fetchall()]
            return {"note_keyword": note_kw, "count": len(rows), "notes": rows}

        raise ValueError(f"未知的账单查询类型: {query_type}")
    finally:
        conn.close()


def _options(conn: sqlite3.Connection) -> dict[str, Any]:
    """账单可选值清单（来源 / 类型 / 方向 / 方式 / 状态 + 统计范围）。"""
    def _distinct(col: str) -> list[str]:
        return [
            r[0] for r in conn.execute(
                f"SELECT DISTINCT {col} FROM {TABLE_PERSONAL_BILLS} "
                f"WHERE {col} != '' ORDER BY {col}"
            ).fetchall()
        ]

    row = conn.execute(
        f"SELECT COUNT(*), MIN(bill_time), MAX(bill_time) FROM {TABLE_PERSONAL_BILLS}"
    ).fetchone()
    return {
        "sources": _distinct("source"),
        "categories": _distinct("category"),
        "directions": _distinct("direction"),
        "pay_methods": _distinct("pay_method"),
        "statuses": _distinct("status"),
        "total": int(row[0]) if row else 0,
        "min_time": (row[1] or "") if row else "",
        "max_time": (row[2] or "") if row else "",
    }


# =========================================================================== #
#  写入                                                                        #
# =========================================================================== #
def _clean_record(item: dict[str, Any]) -> dict[str, Any]:
    """清洗单条账单记录；缺少关键信息（时间与金额都为空）时返回空 dict。"""
    rec: dict[str, Any] = {}
    for f in _BILL_STR_FIELDS:
        val = str(item.get(f) or "").strip()
        if f == "bill_time" and val and "." in val:
            val = val.split(".")[0]
        rec[f] = val
    rec["amount"] = _to_float_or_none(item.get("amount"))
    if not rec["bill_time"] and rec["amount"] is None:
        return {}
    return rec


def _insert_rows_sync(db_path: str, records: list[dict[str, Any]]) -> list[int]:
    """批量写入账单记录，返回自增 id 列表。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    col_sql = ",".join(_BILL_WRITE_COLS)
    ph_sql = ",".join("?" * len(_BILL_WRITE_COLS))
    ids: list[int] = []
    conn = sqlite3.connect(db_path)
    try:
        for rec in records:
            rec["created_at"] = rec.get("created_at") or now
            values = []
            for col in _BILL_WRITE_COLS:
                val = rec.get(col)
                if col == "amount":
                    values.append(_to_float_or_none(val))
                else:
                    values.append("" if val is None else str(val))
            cursor = conn.execute(
                f"INSERT INTO {TABLE_PERSONAL_BILLS} ({col_sql}) VALUES ({ph_sql})",
                tuple(values),
            )
            ids.append(int(cursor.lastrowid or 0))
        conn.commit()
        return ids
    finally:
        conn.close()


def _clean_patch(patch: dict[str, Any]) -> dict[str, Any]:
    """清洗待更新字段：只保留白名单列，字符串去空白，amount 转 float。"""
    out: dict[str, Any] = {}
    for key, value in (patch or {}).items():
        col = str(key).strip()
        if col not in _BILL_EDITABLE:
            continue
        if col == "amount":
            out[col] = _to_float_or_none(value)
        else:
            val = str(value if value is not None else "").strip()
            if col == "bill_time" and val and "." in val:
                val = val.split(".")[0]
            out[col] = val
    return out


def _parse_ids(body: dict[str, Any]) -> list[int]:
    """从请求体解析目标 id 列表：支持 ids 数组 / id 单值 / 逗号分隔字符串。"""
    raw = body.get("ids")
    if isinstance(raw, list):
        out = []
        for x in raw:
            try:
                out.append(int(x))
            except (TypeError, ValueError):
                continue
        return out
    if raw not in (None, ""):
        return [int(x) for x in str(raw).split(",") if str(x).strip().isdigit()]
    single = body.get("id")
    if isinstance(single, (list, tuple)):
        return [int(x) for x in single if str(x).strip().isdigit()]
    if single not in (None, ""):
        return [int(x) for x in str(single).split(",") if str(x).strip().isdigit()]
    return []


def _set_note_sync(db_path: str, ids: list[int], note: str, append: bool = False) -> int:
    """给指定账单写备注 note；append=True 时在原有备注后追加（换行分隔）。"""
    if not ids:
        return 0
    note = str(note or "")
    ph = ",".join("?" * len(ids))
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        if append and note:
            rows = conn.execute(
                f"SELECT id, note FROM {TABLE_PERSONAL_BILLS} WHERE id IN ({ph})", ids
            ).fetchall()
            for r in rows:
                old = (r["note"] or "").strip()
                new = f"{old}\n{note}" if old else note
                conn.execute(
                    f"UPDATE {TABLE_PERSONAL_BILLS} SET note = ? WHERE id = ?", (new, r["id"]))
            conn.commit()
            return len(rows)
        cursor = conn.execute(
            f"UPDATE {TABLE_PERSONAL_BILLS} SET note = ? WHERE id IN ({ph})", (note, *ids))
        conn.commit()
        return int(cursor.rowcount or 0)
    finally:
        conn.close()


def _update_rows_sync(db_path: str, ids: list[int], patch: dict[str, Any]) -> int:
    """按 id 批量更新指定字段，返回受影响行数。"""
    if not ids or not patch:
        return 0
    set_sql = ", ".join(f"{col} = ?" for col in patch)
    params = [*patch.values(), *ids]
    ph = ",".join("?" * len(ids))
    conn = sqlite3.connect(db_path)
    try:
        cursor = conn.execute(
            f"UPDATE {TABLE_PERSONAL_BILLS} SET {set_sql} WHERE id IN ({ph})", params)
        conn.commit()
        return int(cursor.rowcount or 0)
    finally:
        conn.close()


# =========================================================================== #
#  HTTP 视图                                                                   #
# =========================================================================== #
class PersonalBillQueryView(_BaseDBView):
    """个人账单查询 / 分析。

    GET|POST /api/ha_data_store/bill?type=<bill_xxx>&...
    支持的 type 见 BILL_QUERY_TYPES；返回 {"success": true, "type": ..., "data": {...}}。
    """
    url = VIEW_URL
    name = VIEW_NAME

    async def get(self, request: web.Request) -> web.Response:
        return await self._handle(request, {})

    async def post(self, request: web.Request) -> web.Response:
        body: dict[str, Any] = {}
        try:
            parsed = await request.json()
            if isinstance(parsed, dict):
                body = parsed
        except Exception:
            body = {}
        return await self._handle(request, body)

    async def _handle(self, request: web.Request, extra: dict[str, Any]) -> web.Response:
        if (resp := self._check_api_enabled(request)):
            return resp
        params: dict[str, Any] = {k: v for k, v in request.query.items()}
        for key, value in (extra or {}).items():
            if value not in (None, ""):
                params[key] = value
        query_type = str(params.get("type") or "").strip().lower()
        hass: HomeAssistant = request.app["hass"]
        try:
            result = await self._exec_in_executor(hass, run_bill_query, self._db_path, params)
        except ValueError as exc:
            return self.json({"success": False, "error": str(exc)}, status_code=400)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception("[bill] 查询失败 type=%s", query_type)
            return self.json({"success": False, "error": str(exc)}, status_code=500)
        return self.json({"success": True, "type": query_type, "data": result})


class BillTypesView(_BaseDBView):
    """账单可选值清单（供前端表单下拉）。

    GET /api/ha_data_store/bill/types
    """
    url = VIEW_URL + "/types"
    name = VIEW_NAME + ":types"

    async def get(self, request: web.Request) -> web.Response:
        if (resp := self._check_api_enabled(request)):
            return resp
        hass: HomeAssistant = request.app["hass"]

        def _load() -> dict[str, Any]:
            conn = sqlite3.connect(self._db_path)
            try:
                return _options(conn)
            except Exception:
                return {"sources": [], "categories": [], "directions": [],
                        "pay_methods": [], "statuses": [], "total": 0,
                        "min_time": "", "max_time": ""}
            finally:
                conn.close()

        try:
            data = await self._exec_in_executor(hass, _load)
            return self.json({"success": True, "data": data})
        except Exception as exc:  # noqa: BLE001
            return self.json({"success": False, "error": str(exc)}, status_code=500)


class BillAddView(_BaseDBView):
    """提交单条账单记录。

    POST /api/ha_data_store/bill/add
    Body: {source, bill_time, bill_no, category, direction, pay_method, amount,
           counterparty, counterparty_bill_no, counterparty_account, status,
           product, remark}
    """
    url = VIEW_URL + "/add"
    name = VIEW_NAME + ":add"

    async def post(self, request: web.Request) -> web.Response:
        if (resp := self._check_api_enabled(request)):
            return resp
        try:
            body = await request.json()
        except Exception:
            return self.json({"success": False, "error": "请求体需为 JSON"}, status_code=400)
        if not isinstance(body, dict):
            return self.json({"success": False, "error": "请求体需为 JSON 对象"}, status_code=400)

        rec = _clean_record(body)
        if not rec:
            return self.json({"success": False, "error": "至少需要 bill_time 或 amount"},
                             status_code=400)
        hass: HomeAssistant = request.app["hass"]
        try:
            ids = await self._exec_in_executor(hass, _insert_rows_sync, self._db_path, [rec])
            return self.json({"success": True, "id": ids[0], "message": "账单已提交"})
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception("[bill] 提交账单失败")
            return self.json({"success": False, "error": str(exc)}, status_code=500)


class BillBatchAddView(_BaseDBView):
    """批量提交账单记录。

    POST /api/ha_data_store/bill/batch
    Body: {records: [{...}, {...}]}  或直接传数组 [{...}, {...}]
    """
    url = VIEW_URL + "/batch"
    name = VIEW_NAME + ":batch"

    async def post(self, request: web.Request) -> web.Response:
        if (resp := self._check_api_enabled(request)):
            return resp
        try:
            body = await request.json()
        except Exception:
            return self.json({"success": False, "error": "请求体需为 JSON"}, status_code=400)

        if isinstance(body, dict):
            raw_list = body.get("records")
        elif isinstance(body, list):
            raw_list = body
        else:
            raw_list = None
        if not isinstance(raw_list, list) or not raw_list:
            return self.json({"success": False, "error": "请求体需为数组或 {records:[...]}"},
                             status_code=400)

        records = []
        skipped = 0
        for item in raw_list:
            if not isinstance(item, dict):
                skipped += 1
                continue
            rec = _clean_record(item)
            if rec:
                records.append(rec)
            else:
                skipped += 1
        if not records:
            return self.json({"success": False,
                              "error": "无有效记录（每条至少需要 bill_time 或 amount）"},
                             status_code=400)
        hass: HomeAssistant = request.app["hass"]
        try:
            ids = await self._exec_in_executor(hass, _insert_rows_sync, self._db_path, records)
            return self.json({"success": True, "inserted": len(ids), "skipped": skipped,
                              "ids": ids, "message": f"已提交 {len(ids)} 条账单"})
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception("[bill] 批量提交账单失败")
            return self.json({"success": False, "error": str(exc)}, status_code=500)


class BillUpdateView(_BaseDBView):
    """修改账单记录（按 id / ids 批量更新指定字段，含备注 note / remark）。

    POST /api/ha_data_store/bill/update
    Body 形式（三者可混用）：
      { "id": 123, "note": "已对账" }                      单条改备注
      { "ids": [1,2,3], "note": "已对账" }                 批量改备注
      { "id": 123, "fields": { "note": "x", "category": "餐饮" } }
    可改字段：除 id / created_at 外的所有业务列（note、remark、category、direction、
    pay_method、status、product、counterparty、counterparty_account、
    counterparty_bill_no、source、bill_time、bill_no、amount）。
    """
    url = VIEW_URL + "/update"
    name = VIEW_NAME + ":update"

    async def post(self, request: web.Request) -> web.Response:
        if (resp := self._check_api_enabled(request)):
            return resp
        try:
            body = await request.json()
        except Exception:
            return self.json({"success": False, "error": "请求体需为 JSON"}, status_code=400)
        if not isinstance(body, dict):
            return self.json({"success": False, "error": "请求体需为 JSON 对象"}, status_code=400)

        # 定位记录：ids 列表 或 单个 id（也接受逗号分隔字符串）
        id_list = _parse_ids(body)
        if not id_list:
            return self.json({"success": False, "error": "缺少 id 或 ids 参数"}, status_code=400)

        # 收集待更新字段：fields 子对象优先，其余取顶层白名单键
        patch: dict[str, Any] = {}
        sub = body.get("fields")
        if isinstance(sub, dict):
            patch.update(sub)
        for key in _BILL_EDITABLE:
            if key in body:
                patch.setdefault(key, body[key])
        clean = _clean_patch(patch)
        if not clean:
            return self.json(
                {"success": False,
                 "error": "没有可更新字段（可用: " + ", ".join(_BILL_EDITABLE) + "）"},
                status_code=400,
            )

        hass: HomeAssistant = request.app["hass"]
        try:
            updated = await self._exec_in_executor(
                hass, _update_rows_sync, self._db_path, id_list, clean)
            return self.json({"success": True, "updated": updated, "ids": id_list,
                              "fields": sorted(clean), "message": f"已更新 {updated} 条账单"})
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception("[bill] 更新账单失败")
            return self.json({"success": False, "error": str(exc)}, status_code=500)


class BillRemarkView(_BaseDBView):
    """单独提交备注（给某条 / 多条账单写 note）。

    POST /api/ha_data_store/bill/remark
    Body:
      { "id": 123, "note": "已对账" }            给单条写备注（前台点开详单后直接提交）
      { "ids": [1,2,3], "note": "报销中" }        批量写备注
      { "id": 123, "note": "补充说明", "mode": "append" }  在原有备注后追加一行
    """
    url = VIEW_URL + "/remark"
    name = VIEW_NAME + ":remark"

    async def post(self, request: web.Request) -> web.Response:
        if (resp := self._check_api_enabled(request)):
            return resp
        try:
            body = await request.json()
        except Exception:
            return self.json({"success": False, "error": "请求体需为 JSON"}, status_code=400)
        if not isinstance(body, dict):
            return self.json({"success": False, "error": "请求体需为 JSON 对象"}, status_code=400)

        id_list = _parse_ids(body)
        if not id_list:
            return self.json({"success": False, "error": "缺少 id 或 ids 参数"}, status_code=400)
        note = str(body.get("note") if body.get("note") is not None else "").strip()
        append = str(body.get("mode") or "").strip().lower() in ("append", "add", "1", "true")
        if not note and append:
            return self.json({"success": False, "error": "mode=append 时 note 不能为空"},
                             status_code=400)

        hass: HomeAssistant = request.app["hass"]
        try:
            updated = await self._exec_in_executor(
                hass, _set_note_sync, self._db_path, id_list, note, append)
            return self.json({"success": True, "updated": updated, "ids": id_list,
                              "note": note, "mode": "append" if append else "set",
                              "message": f"已更新 {updated} 条备注"})
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception("[bill] 提交备注失败")
            return self.json({"success": False, "error": str(exc)}, status_code=500)


class BillNotesView(_BaseDBView):
    """获取备注候选列表（去重 + 使用次数），供前端点击选择、无需手打。

    GET /api/ha_data_store/bill/notes?keyword=&limit=50
    → {"success":true,"data":{"notes":[{"note":"已对账","count":12,...}], ...}}
    """
    url = VIEW_URL + "/notes"
    name = VIEW_NAME + ":notes"

    async def get(self, request: web.Request) -> web.Response:
        if (resp := self._check_api_enabled(request)):
            return resp
        params: dict[str, Any] = {k: v for k, v in request.query.items()}
        params["type"] = "bill_notes"
        hass: HomeAssistant = request.app["hass"]
        try:
            result = await self._exec_in_executor(
                hass, run_bill_query, self._db_path, params)
            return self.json({"success": True, "data": result})
        except Exception as exc:  # noqa: BLE001
            return self.json({"success": False, "error": str(exc)}, status_code=500)


class BillDeleteView(_BaseDBView):
    """删除账单记录（按 id，支持逗号分隔多个）。

    DELETE|POST /api/ha_data_store/bill/delete?id=1,2,3
    """
    url = VIEW_URL + "/delete"
    name = VIEW_NAME + ":delete"

    async def post(self, request: web.Request) -> web.Response:
        return await self.delete(request)

    async def delete(self, request: web.Request) -> web.Response:
        if (resp := self._check_api_enabled(request)):
            return resp
        id_str = _s({k: v for k, v in request.query.items()}, "id")
        if not id_str:
            try:
                body = await request.json()
                if isinstance(body, dict):
                    id_str = str(body.get("id", "")).strip()
                elif isinstance(body, list):
                    id_str = ",".join(str(x) for x in body)
            except Exception:
                id_str = ""
        if not id_str:
            return self.json({"success": False, "error": "缺少 id 参数"}, status_code=400)
        try:
            id_list = [int(x) for x in id_str.split(",") if x.strip()]
        except ValueError:
            return self.json({"success": False, "error": "id 必须为整数（可逗号分隔多个）"},
                             status_code=400)
        if not id_list:
            return self.json({"success": False, "error": "缺少 id 参数"}, status_code=400)

        db_path = self._db_path

        def _delete() -> int:
            conn = sqlite3.connect(db_path)
            try:
                ph = ",".join("?" * len(id_list))
                cursor = conn.execute(
                    f"DELETE FROM {TABLE_PERSONAL_BILLS} WHERE id IN ({ph})", tuple(id_list))
                conn.commit()
                return cursor.rowcount
            finally:
                conn.close()

        hass: HomeAssistant = request.app["hass"]
        try:
            deleted = await self._exec_in_executor(hass, _delete)
            return self.json({"success": True, "deleted": deleted,
                              "message": f"已删除 {deleted} 条账单"})
        except Exception as exc:  # noqa: BLE001
            return self.json({"success": False, "error": str(exc)}, status_code=500)


def register_api_views(hass: HomeAssistant, db_path: str) -> None:
    """注册个人账单 API。由 __init__._register_api_views 调用。"""
    hass.http.register_view(PersonalBillQueryView(db_path))
    hass.http.register_view(BillTypesView(db_path))
    hass.http.register_view(BillAddView(db_path))
    hass.http.register_view(BillBatchAddView(db_path))
    hass.http.register_view(BillUpdateView(db_path))
    hass.http.register_view(BillRemarkView(db_path))
    hass.http.register_view(BillNotesView(db_path))
    hass.http.register_view(BillDeleteView(db_path))
    _LOGGER.info("[bill] API 已注册：%s", VIEW_URL)
