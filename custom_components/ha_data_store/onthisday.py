"""历史今日查询模块 — 通用实现（通讯 / 设备 / 环境）。

数据源
------
    comm    通讯数据   表 attr_<type_name>   时间列 time      指标 条数 / 时长 / 金额
    device  设备开关   表 device_history     时间列 on_time   指标 次数 / 运行时长 / 用电
    env     环境数据   表 env_<metric>       时间列 datetime  指标 采样数 / 均值 / 最大 / 最小

三类表的时间列都是 'YYYY-MM-DD HH:MM:SS' 文本，因此「历史今日」统一用
`SUBSTR(<时间列>, 6, 5) = 'MM-DD'` 匹配历年同一天。

API
---
    GET|POST /api/ha_data_store/onthisday?source=comm|device|env&...
    兼容入口：/api/ha_data_store/comm?type=onthisday&...（等价 source=comm）

通用参数（三类通用）
--------------------
    source           数据源：comm（默认）/ device / env
    date             基准日：'09-29' 或 '2026-09-29'；留空 = 今天
    years            只看这些年份，多值：2024,2025
    min_year / max_year   年份范围
    exclude_current=1     排除今年，只看往年
    at               时刻：'HH:MM' 或 'now'（此刻）
    align            时刻对齐：hour（整点）/ 30 / 15 / 5 / min
    hours / minutes  自对齐后的起点向后的跨度
    window           以 at 为中心的分钟窗口
    hour             时段：9 / 9,10 / 9-18
    mode             stats（默认）/ detail / ranking / crosstab
    limit / offset   分页
    sort / order     明细排序（默认按时间倒序）
    fields           白名单：只返回指定列（detail 模式，逗号分隔）
    drop_fields      黑名单：从明细中剔除指定列（detail 模式，逗号分隔）；
                     priority 低于 fields（两者同时给出时以 fields 为准）
    content_len      内容截断长度（仅 comm 的 content）
    exclude_entities 本次查询临时排除的 entity_id（多值，逗号分隔）；
                     与「排除实体」设置合并生效（设置见 db_viewer → 系统配置 → 📜 历史今日）

环境明细的「一个房间多种指标」聚合（env 源专属，默认开启）
----------------------------------------------------
    env 源的 detail **默认**按「房间 × 时间点」聚合，每行一个房间、各指标独立成列——
    环境数据分布在多张表（env_temperature / env_humidity / …），平铺时不同指标的值会混在
    同一个 value 列里而无法区分指标。

        聚合（env_by_room 默认 1）  {room, datetime, temperature, humidity, pm25, co2, …}
        平铺（env_by_room=0）       {datetime, room, value, metric}   ← 每行补 metric 标明指标

    传 env_by_room=0 可退回平铺格式（向后兼容），此时每行会多一个 `metric` 字段
    （temperature / humidity / …）——即使指定了 `fields` 也会附带该字段，否则无法分辨指标。
    聚合模式下 `fields` / `drop_fields` 不适用（列由数据动态决定）。

    room_bucket     时间聚合精度（分钟，默认 1）：同一房间同一分钟内的多条采样取平均，
                    因此各指标表的时间戳差几秒也能对齐到同一行；设 0 = 精确到秒
    返回值额外含 group_count（聚合后行数）、room_count / rooms（涉及房间）、metric_names；
    total 仍是采样总数，与聚合前口径一致

    分页与截断
    ----------
        count      本页行数（≤ limit）
        total      采样总数
        group_count 聚合后的总行数（房间 × 时间点）—— 分页是否截断看它与 offset+count 的关系
        truncated  是否还有未返回的行
        remaining  未返回的行数

    注意低频指标容易被挤出窗口：温度湿度每批 12 条、power 每批只有 1 条，
    时间倒序时 limit 小的查询会先被高频指标占满。只想看某个指标时用 env_metric 筛选
    （如 env_metric=power），或把 limit 调大（上限 1000）。

    env 源的 summary / years 同样按指标分组（与明细是否聚合无关）——不同指标量纲不同，
    混在一起算平均没有意义：

        summary: {count: 535, by_metric: {temperature: {count, avg_value, max_value, min_value}, …}}
        years:   [{year: '2026', count: 535, by_metric: {…}}, …]

    顶层 count 是采样总数（跨指标求和仍有意义：采了多少条），
    而 avg_value / max_value / min_value 只出现在各指标内部。

排除实体
--------
    除了参数 exclude_entities，还在 api_settings 中读取一份**持久化**的排除清单
    （键 today_in_history_exclude_entities），对 API 与传感器同时生效。
    被排除的 entity_id 不参与任何 mode / source 的统计；entity_id 为空的历史记录不受影响。

数据源专属参数
--------------
    comm    type_name（留空 = 自动探测 attr_type_defs 中 mode=comm 的类型名；
                       也可由 text.ha_data_store_comm_type_name 实体指定）
    device  entity_ids / rooms / names
    env     env_metric（temperature/humidity/pm25/co2/power/sensor，多值，留空 = 全部）
            注意：metric 是交叉汇总的「测度」参数，环境指标请用 env_metric；
            为兼容，metric 取值确实属于环境指标时也会生效
            env_by_room     明细是否按「房间 × 时间点」聚合（**默认 1 = 聚合**）；
                            =0 退回平铺，此时每行附带 metric 字段标明指标名
            room_bucket     聚合的时间精度（分钟，默认 1）；0 = 精确到秒

输出结构
--------
    stats     {granularity, rows:[{bucket, count, <指标>…}], total:{…}, years:[…]}
    detail    {rows:[逐条记录], count, total, years, year_summaries, summary}
    ranking   {dimension, by, rows:[{key, count, <指标>…, rank}]}
    crosstab  {row_keys, col_keys, matrix, row_metric, col_metric, grand_total}
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from aiohttp import web
from homeassistant.core import HomeAssistant

from .app_settings import get_list
from .const import (
    ATTR_MODE_COMM,
    COMM_DEFAULT_TYPE_NAME,
    TABLE_ATTR_TYPE_DEFS,
    TABLE_DEVICE_HISTORY,
    TODAY_IN_HISTORY_EXCLUDE_SETTING_KEY,
    VALID_METRICS,
    get_attr_table_name,
    get_env_table_name,
)
from .http_api import _BaseDBView

_LOGGER = logging.getLogger(__name__)

VIEW_URL = "/api/ha_data_store/onthisday"
VIEW_NAME = "api:ha_data_store:onthisday"
_TIME_FMT = "%Y-%m-%d %H:%M:%S"

MODES = ("stats", "detail", "ranking", "crosstab")

_SOURCE_ALIASES = {
    "comm": "comm", "comm_records": "comm", "通讯": "comm", "消息": "comm",
    "device": "device", "device_history": "device", "设备": "device",
    "env": "env", "environment": "env", "env_history": "env", "环境": "env",
}

_GRANULARITIES = ("year", "quarter", "month", "week", "day", "hour", "weekday")
_GRANULARITY_ALIASES = {
    "date": "day", "daily": "day", "ymd": "day", "日": "day", "按日": "day",
    "weekly": "week", "周": "week",
    "ym": "month", "monthly": "month", "月": "month", "按月": "month",
    "quarterly": "quarter", "q": "quarter", "季": "quarter",
    "yyyy": "year", "yearly": "year", "年": "year", "按年": "year",
    "时": "hour", "小时": "hour",
}
_FILLABLE = ("day", "week", "month", "quarter", "year")

_WINDOW_MAX_MINUTES = 720      # 设置实体「前后分钟」的上限（与 window 参数一致）

_ALIGN_STEPS = {
    "": 0, "0": 0, "none": 0,
    "minute": 1, "min": 1, "1m": 1, "分钟": 1,
    "5": 5, "10": 10, "15": 15, "20": 20, "30": 30, "half": 30,
    "hour": 60, "h": 60, "1h": 60, "整点": 60, "小时": 60,
}


# =========================================================================== #
#  数据源描述                                                                   #
# =========================================================================== #
@dataclass
class Source:
    """一类数据源的查询描述。"""

    key: str
    label: str
    unit: str                      # 记录量词（条 / 次 / 个）
    tables: list[str]
    time_col: str
    cols: set[str] = field(default_factory=set)
    dims: dict[str, str] = field(default_factory=dict)
    metrics: dict[str, dict] = field(default_factory=dict)
    default_dim: str = ""
    default_metric: str = "count"
    extra_where: list[str] = field(default_factory=list)

    @property
    def primary_table(self) -> str:
        return self.tables[0]


# 数据源 → 指标定义：agg ∈ count / sum / avg / max / min
_SOURCE_DEFS: dict[str, dict] = {
    "comm": {
        "label": "通讯数据",
        "unit": "条",
        "time_col": "time",
        "dims": {
            "party_name": "party_name", "party_number": "party_number",
            "location": "location", "party_place": "party_place",
            "msg_type": "msg_type", "call_type": "call_type",
            "channel": "channel", "my_number": "my_number",
        },
        "metrics": {
            "count": {"label": "条数", "agg": "count"},
            "duration": {"label": "通话时长(秒)", "agg": "sum", "column": "duration"},
            "cost": {"label": "金额(元)", "agg": "sum", "column": "cost"},
        },
        "default_dim": "party_name",
    },
    "device": {
        "label": "设备开关",
        "unit": "次",
        "time_col": "on_time",
        "dims": {
            "entity_id": "entity_id", "name": "name", "room": "room",
        },
        "metrics": {
            "count": {"label": "开关次数", "agg": "count"},
            "duration": {"label": "运行时长(秒)", "agg": "sum", "column": "duration"},
            "energy": {"label": "用电(kWh)", "agg": "sum", "column": "energy_consumed"},
            "avg_duration": {"label": "平均时长(秒)", "agg": "avg", "column": "duration"},
        },
        "default_dim": "name",
    },
    "env": {
        "label": "环境数据",
        "unit": "个",
        "time_col": "datetime",
        "dims": {
            "entity_id": "entity_id", "name": "name", "room": "room",
        },
        "metrics": {
            "count": {"label": "采样数", "agg": "count"},
            "avg_value": {"label": "平均值", "agg": "avg", "column": "value"},
            "max_value": {"label": "最大值", "agg": "max", "column": "value"},
            "min_value": {"label": "最小值", "agg": "min", "column": "value"},
        },
        "default_dim": "entity_id",
    },
}


# =========================================================================== #
#  工具函数                                                                     #
# =========================================================================== #
def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _num(value: Any) -> float:
    if value is None:
        return 0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0


def _split_multi(raw: Any) -> list[str]:
    if not raw:
        return []
    items = [str(v) for v in raw] if isinstance(raw, (list, tuple)) else str(raw).replace("，", ",").split(",")
    return [v.strip() for v in items if v.strip()]


def _get_int(params: dict[str, Any], key: str, default: int, lo: int | None = None,
             hi: int | None = None) -> int:
    # 注意：不能用 `params.get(key) or ""`——数字 0 是 falsy，会被误判成「未提供」而套用默认值
    raw = params.get(key)
    text = "" if raw is None else str(raw).strip()
    try:
        val = int(text) if text else default
    except (TypeError, ValueError):
        val = default
    if lo is not None:
        val = max(lo, val)
    if hi is not None:
        val = min(hi, val)
    return val


def _parse_dt(text: str) -> datetime | None:
    t = (text or "").strip().replace("T", " ")
    if not t:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        for candidate in dict.fromkeys([t, t[:19], t[:16], t[:10]]):
            try:
                return datetime.strptime(candidate, fmt)
            except ValueError:
                continue
    return None


def _where_sql(where: list[str]) -> str:
    return (" WHERE " + " AND ".join(where)) if where else ""


def _and_sql(where_sql: str, condition: str) -> str:
    return f"{where_sql} AND {condition}" if where_sql else f" WHERE {condition}"


def _table_exists(conn: sqlite3.Connection, tbl: str) -> bool:
    return bool(conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (tbl,)
    ).fetchone())


def _table_cols(conn: sqlite3.Connection, tbl: str) -> set[str]:
    return {row[1] for row in conn.execute(f'PRAGMA table_info("{tbl}")')}


def _comm_types_hint(conn: sqlite3.Connection) -> str:
    """列出 attr_type_defs 中已登记的通讯类型名，附在报错信息里便于定位。"""
    try:
        rows = conn.execute(
            f"SELECT type_name FROM {TABLE_ATTR_TYPE_DEFS} "
            "WHERE LOWER(IFNULL(mode, '')) = ? ORDER BY type_name",
            (ATTR_MODE_COMM,),
        ).fetchall()
    except sqlite3.OperationalError:
        return ""
    names = [str(row[0] or "").strip() for row in rows if str(row[0] or "").strip()]
    if not names:
        return ""
    return "；已登记的通讯类型名：" + " / ".join(names) + "（可用参数 type_name 指定，或设置 " \
        "text.ha_data_store_comm_type_name 实体）"


def _align_step(raw: Any) -> int:
    text = str(raw or "").strip().lower()
    if text in _ALIGN_STEPS:
        return _ALIGN_STEPS[text]
    return min(max(int(text), 0), 720) if text.isdigit() else 0


def _resolve_granularity(params: dict[str, Any], default: str) -> str:
    raw = (params.get("granularity") or "").strip().lower()
    if not raw:
        return default
    mapped = _GRANULARITY_ALIASES.get(raw, raw)
    if mapped not in _GRANULARITIES:
        raise ValueError("granularity 只能是 " + " / ".join(_GRANULARITIES))
    return mapped


def _bucket_expr(time_col: str, gran: str) -> str:
    t = f'"{time_col}"'
    return {
        "day": f"SUBSTR({t}, 1, 10)",
        "week": f"STRFTIME('%Y-W%W', {t})",
        "month": f"SUBSTR({t}, 1, 7)",
        "quarter": (f"SUBSTR({t}, 1, 4) || '-Q' || "
                    f"CAST((CAST(SUBSTR({t}, 6, 2) AS INTEGER) + 2) / 3 AS TEXT)"),
        "year": f"SUBSTR({t}, 1, 4)",
        "hour": f"SUBSTR({t}, 12, 2)",
        "weekday": f"STRFTIME('%w', {t})",
    }[gran]


def _parse_fields(params: dict[str, Any]) -> list[str] | None:
    values = [v.lower() for v in _split_multi(params.get("fields"))]
    return values or None


def _parse_drop_fields(params: dict[str, Any]) -> list[str] | None:
    values = [v.lower() for v in _split_multi(params.get("drop_fields"))]
    return values or None


def parse_window_setting(raw: Any) -> tuple[str, int] | None:
    """解析设置实体的时间范围写法：`<时间>,<前后分钟>`。

    支持：
        '01,80'      → ('01:00', 80)   01:00 前后 80 分钟
        'now,60'     → ('now', 60)     此刻前后 60 分钟
        '09:02,30'   → ('09:02', 30)   09:02 前后 30 分钟
        空 / 无法识别 → None            表示不限定（全部数据）

    text 实体与传感器共用本函数，保证「设置能不能写」与「读出来是什么」一致。
    """
    text = str(raw or "").strip()
    if not text or text.lower() in ("unknown", "unavailable", "none", "-"):
        return None

    parts = [p.strip() for p in text.replace("，", ",").split(",")]
    if len(parts) != 2:
        return None
    at_raw, win_raw = parts

    if at_raw.lower() in ("now", "此刻", "现在"):
        at = "now"
    else:
        hh, _, mm = at_raw.replace("：", ":").partition(":")
        hh, mm = hh.strip(), (mm.strip() or "0")
        if not (hh.isdigit() and mm.isdigit()):
            return None
        hour, minute = int(hh), int(mm)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        at = f"{hour:02d}:{minute:02d}"

    if not win_raw.isdigit():
        return None
    window = int(win_raw)
    if window > _WINDOW_MAX_MINUTES:
        return None
    return at, window


def describe_window_setting(parsed: tuple[str, int] | None) -> str:
    """把解析结果转成可读说明，用于传感器属性。"""
    if not parsed:
        return "全天（不限定）"
    at, window = parsed
    return f"{at} ± {window} 分钟"


# =========================================================================== #
#  数据源构建                                                                   #
# =========================================================================== #
def _build_source(conn: sqlite3.Connection, params: dict[str, Any]) -> Source:
    """按 source 参数构建数据源描述（env 会展开为多张指标表）。"""
    raw = (params.get("source") or "comm").strip().lower()
    key = _SOURCE_ALIASES.get(raw)
    if key is None:
        raise ValueError("source 只能是 comm / device / env")

    base = _SOURCE_DEFS[key]
    extra: list[str] = []

    if key == "comm":
        type_name = (params.get("type_name") or "").strip() or COMM_DEFAULT_TYPE_NAME
        tbl = get_attr_table_name(type_name)
        if not _table_exists(conn, tbl):
            raise ValueError(
                f"未找到通讯数据表 {tbl}（类型名 {type_name}）。请先在「系统配置 → 属性提取」"
                f"中配置「通讯数据采集」，待采集到数据后再查询" + _comm_types_hint(conn)
            )
        tables = [tbl]
    elif key == "device":
        if not _table_exists(conn, TABLE_DEVICE_HISTORY):
            raise ValueError(
                f"未找到设备开关记录表 {TABLE_DEVICE_HISTORY}。"
                f"该表由设备状态监听自动写入，请先确认已有设备开关记录。"
            )
        tables = [TABLE_DEVICE_HISTORY]
    else:
        # 环境指标用 env_metric / env_metrics 指定；metric 仅在取值确属环境指标时兼容。
        # （metric 同时是交叉汇总的「测度」参数，直接采用会把 count 当成表名 env_count）
        wanted = _split_multi(params.get("env_metric") or params.get("env_metrics"))
        if not wanted:
            wanted = [m for m in _split_multi(params.get("metric")) if m in VALID_METRICS]
        invalid = [m for m in wanted if m not in VALID_METRICS]
        if invalid:
            raise ValueError(
                "env_metric 不支持：" + " / ".join(invalid)
                + "；可用：" + " / ".join(VALID_METRICS)
            )
        names = wanted or list(VALID_METRICS)
        tables = [t for t in (get_env_table_name(m) for m in names) if _table_exists(conn, t)]
        if not tables:
            raise ValueError(
                "未找到任何环境数据表。已检查：" + " / ".join(names)
                + "；可用 metric：" + " / ".join(VALID_METRICS)
                + "。若这些表尚未创建，请先在「系统配置」中启用对应的环境采集。"
            )

    cols: set[str] = set()
    for tbl in tables:
        cols |= _table_cols(conn, tbl)

    dims = {k: v for k, v in base["dims"].items() if v in cols}
    metrics = {}
    for name, spec in base["metrics"].items():
        col = spec.get("column")
        if col and col not in cols:
            continue
        metrics[name] = dict(spec)
    if "count" not in metrics:
        metrics["count"] = {"label": base["unit"] + "数", "agg": "count"}

    default_dim = base["default_dim"] if base["default_dim"] in dims else next(iter(dims), "")

    return Source(
        key=key,
        label=base["label"],
        unit=base["unit"],
        tables=tables,
        time_col=base["time_col"],
        cols=cols,
        dims=dims,
        metrics=metrics,
        default_dim=default_dim,
        default_metric="count",     # 排行 / 交叉默认按条数，需要其它指标时显式传 by / metric
        extra_where=extra,
    )


def _exclude_ids(db_path: str, params: dict[str, Any]) -> list[str]:
    """解析要排除的 entity_id：设置里的（永久）+ 本次参数 exclude_entities（临时）。

    设置存 api_settings（见 db_viewer → 系统配置 → 📜 历史今日），对 API 与传感器同时生效；
    exclude_entities 供单次查询临时追加，二者合并去重。
    """
    out: list[str] = []
    seen: set[str] = set()
    try:
        saved = get_list(db_path, TODAY_IN_HISTORY_EXCLUDE_SETTING_KEY)
    except Exception:  # noqa: BLE001 - 读设置失败不应让查询整体失败
        saved = []
    for x in list(saved) + _split_multi(params.get("exclude_entities")):
        s = str(x or "").strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _filters(src: Source, params: dict[str, Any]) -> tuple[list[str], list[Any]]:
    """业务过滤（不含月日 / 年份 / 时刻，那些由 _onthisday_where 负责）。"""
    where: list[str] = []
    args: list[Any] = []

    if src.key == "comm":
        from .comm import _build_filters  # 延迟导入，复用通讯专属过滤
        # date / month / year / start / end 在「历史今日」里语义不同：
        # date 是基准日、年份范围用 years / min_year / max_year。
        # 原样透传会被 _build_filters 当成「指定单日」把结果收窄成一天。
        narrow = {k: v for k, v in params.items()
                  if k not in ("date", "month", "year", "start", "end", "period")}
        where, args = _build_filters(narrow, src.cols)
    else:
        def _multi(param: str, column: str, fuzzy: bool = False) -> None:
            if column not in src.cols:
                return
            parts, vals = [], []
            for value in _split_multi(params.get(param)):
                parts.append(f'"{column}" LIKE ?' if fuzzy else f'"{column}" = ?')
                vals.append(f"%{value}%" if fuzzy else value)
            if parts:
                where.append("(" + " OR ".join(parts) + ")")
                args.extend(vals)

        _multi("entity_ids", "entity_id")
        _multi("entity_id", "entity_id")
        _multi("rooms", "room")
        _multi("room", "room")
        _multi("names", "name", fuzzy=True)

    # 排除实体：对所有数据源（comm / device / env）统一生效。
    # 用 IFNULL 兜底——entity_id 为空的历史记录不应被误排除（NULL NOT IN (...) 在 SQLite 中为 NULL）。
    excludes = [str(x).strip() for x in (params.get("__exclude_ids") or []) if str(x).strip()]
    if excludes and "entity_id" in src.cols:
        where.append('IFNULL("entity_id", \'\') NOT IN (' + ", ".join("?" * len(excludes)) + ")")
        args.extend(excludes)
    return where, args


# =========================================================================== #
#  「历史今日」时间过滤                                                          #
# =========================================================================== #
def _resolve_base(params: dict[str, Any]) -> tuple[datetime, str]:
    """解析基准日期 → (基准 datetime, 月日 'MM-DD')；date 支持 'MM-DD' 或 'YYYY-MM-DD'。"""
    raw = (params.get("date") or "").strip()
    base: datetime | None = None
    if raw:
        txt = raw.replace("/", "-").replace("T", " ").split(" ")[0]
        parts = txt.split("-")
        try:
            if len(parts) == 2:
                base = datetime(datetime.now().year, int(parts[0]), int(parts[1]))
            elif len(parts) >= 3:
                base = datetime(int(parts[0]), int(parts[1]), int(parts[2]))
        except ValueError:
            base = None
    if base is None:
        base = datetime.now()
    return base, base.strftime("%m-%d")


def _onthisday_where(src: Source, params: dict[str, Any]) -> tuple[list[str], list[Any], datetime, str]:
    """构造「历史今日」过滤：月日相同 + 年份限定 + 时刻窗口。"""
    base, mmdd = _resolve_base(params)
    t = f'"{src.time_col}"'
    where: list[str] = [f"SUBSTR({t}, 6, 5) = ?"]
    args: list[Any] = [mmdd]

    years = _split_multi(params.get("years"))
    if years:
        where.append(f"SUBSTR({t}, 1, 4) IN ({', '.join('?' * len(years))})")
        args.extend(years)
    else:
        min_year = (params.get("min_year") or "").strip()[:4]
        max_year = (params.get("max_year") or "").strip()[:4]
        if min_year.isdigit():
            where.append(f"SUBSTR({t}, 1, 4) >= ?")
            args.append(min_year)
        if max_year.isdigit():
            where.append(f"SUBSTR({t}, 1, 4) <= ?")
            args.append(max_year)
        if _get_int(params, "exclude_current", 0, 0, 1):
            where.append(f"SUBSTR({t}, 1, 4) <> ?")
            args.append(f"{base.year:04d}")

    minutes_expr = (f"(CAST(SUBSTR({t}, 12, 2) AS INTEGER) * 60 "
                    f"+ CAST(SUBSTR({t}, 15, 2) AS INTEGER))")
    at_raw = (params.get("at") or "").strip().lower()
    if at_raw == "now":
        at_raw = datetime.now().strftime("%H:%M")
    if at_raw:
        hh, _, mm = at_raw.replace("：", ":").partition(":")
        center: int | None = None
        if hh.strip().isdigit():
            center = (int(hh.strip()) % 24) * 60
            if mm.strip().isdigit():
                center += int(mm.strip()) % 60
        if center is not None:
            step = _align_step(params.get("align"))
            span = _get_int(params, "hours", 0, 0, 24) * 60 + _get_int(params, "minutes", 0, 0, 1440)
            if span or step:
                start = (center // step) * step if step else center
                end = start + (span or step)
                if end <= start:
                    end = start + (step or 1)
                where.append(f"{minutes_expr} >= ? AND {minutes_expr} < ?")
                args.extend([start, min(end, 1440)])
            else:
                window = _get_int(params, "window", 0, 0, 720)
                where.append(f"{minutes_expr} BETWEEN ? AND ?")
                args.extend([center - window, center + window])
    else:
        bounds: list[tuple[int, int]] = []
        for item in _split_multi(params.get("hour")):
            if "-" in item:
                lo, _, hi = item.partition("-")
                if lo.strip().isdigit() and hi.strip().isdigit():
                    lo_i, hi_i = int(lo.strip()) % 24, int(hi.strip()) % 24
                    if lo_i > hi_i:
                        lo_i, hi_i = hi_i, lo_i
                    bounds.append((lo_i * 60, hi_i * 60 + 59))
            elif item.strip().isdigit():
                hour_i = int(item.strip()) % 24
                bounds.append((hour_i * 60, hour_i * 60 + 59))
        if bounds:
            where.append("(" + " OR ".join(f"{minutes_expr} BETWEEN ? AND ?" for _ in bounds) + ")")
            for lo, hi in bounds:
                args.extend([lo, hi])

    return where, args, base, mmdd


def _base_filters(src: Source, params: dict[str, Any]) -> tuple[list[str], list[Any], datetime, str]:
    """业务过滤 + 历史今日过滤（含数据源固定附加条件）。"""
    where, args = _filters(src, params)
    owhere, oargs, base, mmdd = _onthisday_where(src, params)
    where = where + src.extra_where + owhere
    if src.time_col in src.cols:
        where = where + [f'"{src.time_col}" <> \'\'']
    return where, args + oargs, base, mmdd


# =========================================================================== #
#  聚合（跨表合并）                                                             #
# =========================================================================== #
def _agg_keys(src: Source) -> list[str]:
    """参与聚合的数值列。"""
    cols: list[str] = []
    for spec in src.metrics.values():
        col = spec.get("column")
        if col and col not in cols and spec["agg"] in ("sum", "avg", "max", "min"):
            cols.append(col)
    return cols


def _merge_into(acc: dict[str, Any], key: str, row: Any, columns: list[str]) -> None:
    """把单表聚合行合并进合并字典（sum 累加 / max 取大 / min 取小 / n 累加）。"""
    bucket_acc = acc.setdefault(key, {"count": 0})
    bucket_acc["count"] += int(row["c"] or 0)
    for col in columns:
        slot = bucket_acc.setdefault(col, {"sum": 0.0, "max": None, "min": None, "n": 0})
        slot["sum"] += _num(row[f"{col}__sum"])
        slot["n"] += int(row[f"{col}__n"] or 0)
        value_max, value_min = row[f"{col}__max"], row[f"{col}__min"]
        if value_max is not None:
            slot["max"] = value_max if slot["max"] is None else max(slot["max"], value_max)
        if value_min is not None:
            slot["min"] = value_min if slot["min"] is None else min(slot["min"], value_min)


def _metrics_out(acc: dict[str, Any], src: Source) -> dict[str, Any]:
    """按指标定义把合并后的原始量转成输出字段。"""
    out: dict[str, Any] = {}
    for name, spec in src.metrics.items():
        agg = spec["agg"]
        if agg == "count":
            out[name] = acc.get("count", 0)
            continue
        slot = acc.get(spec["column"], {})
        if agg == "sum":
            out[name] = round(_num(slot.get("sum")), 4)
        elif agg == "avg":
            n = int(slot.get("n") or 0)
            out[name] = round(_num(slot.get("sum")) / n, 4) if n else 0
        elif agg == "max":
            value = slot.get("max")
            out[name] = round(_num(value), 4) if value is not None else None
        else:
            value = slot.get("min")
            out[name] = round(_num(value), 4) if value is not None else None
    return out


def _agg_select(columns: list[str]) -> str:
    parts = ""
    for col in columns:
        parts += (f', SUM(CAST("{col}" AS REAL)) AS "{col}__sum"'
                  f', SUM(CASE WHEN "{col}" IS NOT NULL THEN 1 ELSE 0 END) AS "{col}__n"'
                  f', MAX("{col}") AS "{col}__max"'
                  f', MIN("{col}") AS "{col}__min"')
    return parts


def _grouped_scan(conn: sqlite3.Connection, src: Source, where_sql: str, args: list[Any],
                  group_expr: str, columns: list[str], order_sql: str = "",
                  limit: int | None = None) -> dict[str, dict[str, Any]]:
    """对数据源的每张表做分组聚合，跨表按 key 合并。"""
    acc: dict[str, dict[str, Any]] = {}
    agg_sql = _agg_select(columns)
    tail = f"{order_sql}"
    if limit:
        tail += " LIMIT ?"
    for tbl in src.tables:
        params = [*args]
        if limit:
            params.append(limit)
        rows = conn.execute(
            f'SELECT {group_expr} AS k, COUNT(*) AS c{agg_sql} '
            f'FROM "{tbl}"{where_sql} GROUP BY k{tail}',
            params,
        ).fetchall()
        for row in rows:
            key = "" if row["k"] is None else str(row["k"])
            _merge_into(acc, key, row, columns)
    return acc


def _total_of(conn: sqlite3.Connection, src: Source, where_sql: str, args: list[Any],
              columns: list[str]) -> dict[str, Any]:
    """整体合计（独立聚合，不受分页影响）。"""
    acc: dict[str, dict[str, Any]] = {}
    agg_sql = _agg_select(columns)
    for tbl in src.tables:
        row = conn.execute(
            f'SELECT COUNT(*) AS c{agg_sql} FROM "{tbl}"{where_sql}', args
        ).fetchone()
        if row:
            _merge_into(acc, "_", row, columns)
    return _metrics_out(acc.get("_", {"count": 0}), src)


def _fill_buckets(rows: list[dict[str, Any]], gran: str, start: datetime,
                  end: datetime, template: dict[str, Any]) -> list[dict[str, Any]]:
    """补全空缺时间桶（无数据补 0，仅对 fill=1 生效）。"""
    bucket_map = {str(r.get("bucket")): r for r in rows}
    filled: list[dict[str, Any]] = []
    cursor = start
    guard = 0
    while cursor < end and guard < 5000:
        guard += 1
        if gran == "day":
            key = cursor.strftime("%Y-%m-%d")
            cursor = cursor + timedelta(days=1)
        elif gran == "week":
            key = cursor.strftime("%Y-W%W")
            cursor = cursor + timedelta(days=7)
        elif gran == "month":
            key = cursor.strftime("%Y-%m")
            idx = cursor.year * 12 + cursor.month  # 下一个月
            cursor = datetime(idx // 12, idx % 12 + 1, 1)
        elif gran == "quarter":
            key = f"{cursor.year:04d}-Q{(cursor.month - 1) // 3 + 1}"
            idx = cursor.year * 12 + cursor.month - 1 + 3
            cursor = datetime(idx // 12, idx % 12 + 1, 1)
        else:  # year
            key = cursor.strftime("%Y")
            cursor = datetime(cursor.year + 1, 1, 1)
        if key in bucket_map:
            filled.append(bucket_map.pop(key))
        else:
            empty = {"bucket": key}
            empty.update({k: 0 for k in template})
            filled.append(empty)
    filled.extend(bucket_map.values())
    return filled


# =========================================================================== #
#  四种输出模式                                                                 #
# =========================================================================== #
def _q_stats(conn: sqlite3.Connection, src: Source, where: list[str], args: list[Any],
             base: datetime, mmdd: str, params: dict[str, Any]) -> dict[str, Any]:
    """stats：按时间粒度（年 / 季 / 月 / 周 / 日 / 小时 / 星期）分组汇总。"""
    gran = _resolve_granularity(params, "year")
    where_sql = _where_sql(where)
    columns = _agg_keys(src)
    acc = _grouped_scan(
        conn, src, where_sql, args, _bucket_expr(src.time_col, gran), columns,
        order_sql=" ORDER BY k ASC", limit=_get_int(params, "limit", 1000, 1),
    )
    rows: list[dict[str, Any]] = []
    for key in sorted(acc):
        item: dict[str, Any] = {"bucket": key}
        item.update(_metrics_out(acc[key], src))
        rows.append(item)

    template = _metrics_out({"count": 0}, src)
    if _get_int(params, "fill", 0, 0, 1) and gran in _FILLABLE:
        start, end = _period_start(base, gran), None
        end = _period_next(start, gran)
        rows = _fill_buckets(rows, gran, start, end, template)

    total = _total_of(conn, src, where_sql, args, columns)
    out: dict[str, Any] = {
        "granularity": gran,
        "count": len(rows),
        "rows": rows,
        "total": total,
        "buckets": [r["bucket"] for r in rows],
    }
    if _get_int(params, "with_avg", 0, 0, 1) and rows:
        n = len(rows)
        out["avg_per_bucket"] = {
            key: round(sum(_num(r.get(key)) for r in rows) / n, 4) for key in src.metrics
        }
    return out


def _period_start(moment: datetime, gran: str) -> datetime:
    if gran == "day":
        return datetime(moment.year, moment.month, moment.day)
    if gran == "week":
        return datetime(moment.year, moment.month, moment.day) - timedelta(days=moment.weekday())
    if gran == "month":
        return datetime(moment.year, moment.month, 1)
    if gran == "quarter":
        return datetime(moment.year, (moment.month - 1) // 3 * 3 + 1, 1)
    return datetime(moment.year, 1, 1)


def _period_next(start: datetime, gran: str) -> datetime:
    if gran == "day":
        return start + timedelta(days=1)
    if gran == "week":
        return start + timedelta(days=7)
    if gran == "month":
        idx = start.year * 12 + start.month
        return datetime(idx // 12, idx % 12 + 1, 1)
    if gran == "quarter":
        idx = start.year * 12 + start.month - 1 + 3
        return datetime(idx // 12, idx % 12 + 1, 1)
    return datetime(start.year + 1, 1, 1)


# detail 模式的分页：只设默认值，**不设上限**。
#   limit 缺省 → 默认条数；limit=0（或负数）→ 不限条数；其余按传入值返回。
# 响应里的 limit_max 恒为 null，表示无上限。
_DETAIL_LIMIT_DEFAULT = 100


def _paging_limit(params: dict[str, Any], default: int) -> int:
    """解析分页 limit：缺省用 default，0 / 负数表示「不限」（返回 -1 交给 SQL）。"""
    value = _get_int(params, "limit", default, None)
    return -1 if value <= 0 else value


def _page_slice(items: list[Any], offset: int, limit: int) -> list[Any]:
    """按 limit / offset 切片；limit < 0（不限）时只应用 offset。"""
    if limit < 0:
        return items[offset:]
    return items[offset:offset + limit]


def _detail_select(conn: sqlite3.Connection, tbl: str, fields: list[str] | None,
                   drop: list[str] | None = None) -> str:
    """明细返回列。

    优先级：`fields`（白名单，只返回列出的列）> `drop_fields`（黑名单，剔除列出的列）
    > 全部列。白名单中不存在的列会被静默忽略（跨表合并时各表列可能不同）。
    """
    cols = _table_cols(conn, tbl)
    picked = [c for c in (fields or []) if c in cols]
    if not picked:
        dropped = set(drop or [])
        picked = [c for c in sorted(cols) if c not in dropped]
        if "id" in cols and "id" not in dropped:
            picked = ["id"] + [c for c in picked if c != "id"]
    return ", ".join(f'"{c}"' for c in picked) or "*"


def _env_metric_of(tbl: str) -> str:
    """从环境表名反推指标名：env_temperature → temperature。"""
    prefix = "env_"
    return tbl[len(prefix):] if tbl.startswith(prefix) else tbl


def _env_metric_row(row: Any) -> dict[str, Any]:
    """单指标聚合行 → 统一的输出结构。"""
    return {
        "count": int(row["count"] or 0),
        "avg_value": round(_num(row["avg_value"]), 4),
        "max_value": _num(row["max_value"]),
        "min_value": _num(row["min_value"]),
    }


def _env_metric_empty() -> dict[str, Any]:
    """无数据指标的占位。

    聚合值用 `null` 而非 0——「没有采样」和「采样值恰好是 0」是两回事
    （温度 0℃ 是有效读数）。
    """
    return {"count": 0, "avg_value": None, "max_value": None, "min_value": None}


def _env_metric_agg(conn: sqlite3.Connection, src: Source, where_sql: str,
                    args: list[Any], year_expr: str | None = None) -> dict[str, Any]:
    """按**指标分别**聚合环境采样（不同指标量纲不同，混算出的均值/极值没有意义）。

    温度 25、湿度 58、CO₂ 800 混在一起求平均 → 46.99，这个数字不代表任何东西。
    因此逐表（即逐指标）各自聚合：

        不给 year_expr → {temperature: {count, avg_value, max_value, min_value}, …}
        给了 year_expr → {2026: {temperature: {…}, humidity: {…}}, 2025: {…}}
    """
    out: dict[str, Any] = {}
    for tbl in src.tables:
        metric = _env_metric_of(tbl)
        if year_expr:
            sql = (f'SELECT {year_expr} AS k, COUNT(*) AS count, AVG("value") AS avg_value, '
                   f'MAX("value") AS max_value, MIN("value") AS min_value '
                   f'FROM "{tbl}"{where_sql} GROUP BY k')
            for row in conn.execute(sql, args).fetchall():
                key = str(row["k"] or "")
                if not key:
                    continue
                out.setdefault(key, {})[metric] = _env_metric_row(row)
        else:
            row = conn.execute(
                f'SELECT COUNT(*) AS count, AVG("value") AS avg_value, '
                f'MAX("value") AS max_value, MIN("value") AS min_value '
                f'FROM "{tbl}"{where_sql}', args).fetchone()
            if row is not None and int(row["count"] or 0) > 0:
                out[metric] = _env_metric_row(row)
            else:
                # 表存在但当前筛选下无数据：也要列出（count=0）。
                # 否则 by_metric 的键会少于 tables，看起来像「漏统计了这个指标」。
                out[metric] = _env_metric_empty()
    return out


def _append_env_year_summary(out: dict[str, Any], conn: sqlite3.Connection, src: Source,
                             where_sql: str, args: list[Any], params: dict[str, Any]) -> None:
    """env 源的分年 / 总体汇总：`by_metric` 内按指标分别给出均值与极值。

    顶层 `count` 仍是采样总数（跨指标求和，有意义——它只是「采了多少条」）；
    而 `avg_value` / `max_value` / `min_value` 只出现在各指标内部，避免跨量纲混算。
    """
    limit = _get_int(params, "years_limit", 30, 1, 100)
    t = f'"{src.time_col}"'
    years_map = _env_metric_agg(conn, src, where_sql, args, year_expr=f"SUBSTR({t}, 1, 4)")

    summaries: list[dict[str, Any]] = []
    for key in sorted(years_map, reverse=True)[:limit]:
        block = years_map[key]
        summaries.append({
            "year": key,
            "count": sum(item["count"] for item in block.values()),
            "by_metric": block,
        })
    out["years"] = [s["year"] for s in summaries]
    out["year_summaries"] = summaries

    block = _env_metric_agg(conn, src, where_sql, args)
    out["summary"] = {
        "count": sum(item["count"] for item in block.values()),
        "by_metric": block,
    }


def _append_year_summary(out: dict[str, Any], conn: sqlite3.Connection, src: Source,
                         where_sql: str, args: list[Any], params: dict[str, Any]) -> None:
    """给 detail 结果附上「各年汇总」与「总体汇总」。

    env 源走专门实现（按指标分别汇总）——它由多张量纲不同的指标表组成，
    混算出的 avg / max / min 没有意义。comm / device 各表量纲一致，沿用通用逻辑。
    """
    if not _get_int(params, "with_years", 1, 0, 1):
        return
    if src.key == "env":
        _append_env_year_summary(out, conn, src, where_sql, args, params)
        return
    columns = _agg_keys(src)
    year_acc = _grouped_scan(
        conn, src, where_sql, args, f'SUBSTR("{src.time_col}", 1, 4)', columns,
        order_sql=" ORDER BY k DESC", limit=_get_int(params, "years_limit", 30, 1, 100),
    )
    summaries: list[dict[str, Any]] = []
    for key in sorted(year_acc, reverse=True):
        if not key:
            continue
        item: dict[str, Any] = {"year": key}
        item.update(_metrics_out(year_acc[key], src))
        summaries.append(item)
    out["years"] = [s["year"] for s in summaries]
    out["year_summaries"] = summaries
    out["summary"] = _total_of(conn, src, where_sql, args, columns)


def _q_detail_env_by_room(conn: sqlite3.Connection, src: Source, where: list[str],
                          args: list[Any], base: datetime, mmdd: str,
                          params: dict[str, Any]) -> dict[str, Any]:
    """env 明细 · 按「房间 × 时间点」聚合：每行一个房间的**多种指标**（env 源的默认格式）。

    环境数据分布在多张表（env_temperature / env_humidity / …）。若逐条平铺，不同指标
    的值会混在同一个 `value` 列里而**无法区分**；这里改为按房间与时间点分组，每个指标
    独立成列：

        {room: 客厅, datetime: '2026-09-29 10:00:00', temperature: 25.0, humidity: 58.0}

    - `room_bucket`：时间聚合精度（分钟，默认 1）。同一房间同一分钟内的多条采样取平均，
      因此各指标表的时间戳差几秒也能对齐到同一行（0 = 精确到秒，不合并）
    - `total` 仍是**采样总数**（与聚合前口径一致），另有 `group_count`（聚合后行数）、
      `room_count` / `rooms`（涉及房间）与 `metrics`（出现的指标名）
    """
    limit = _paging_limit(params, _DETAIL_LIMIT_DEFAULT)
    offset = _get_int(params, "offset", 0, 0)
    bucket = _get_int(params, "room_bucket", 1, 0, 60)
    where_sql = _where_sql(where)
    t = f'"{src.time_col}"'

    groups: dict[tuple[str, str], dict[str, Any]] = {}
    metrics: list[str] = []
    total = 0
    for tbl in src.tables:
        metric = _env_metric_of(tbl)
        if metric not in metrics:
            metrics.append(metric)
        key_expr = f"SUBSTR({t}, 1, 16)" if bucket else t
        rows = conn.execute(
            f'SELECT IFNULL("room", \'\') AS room, {key_expr} AS ts_key, '
            f'MIN({t}) AS ts, AVG("value") AS value, COUNT(*) AS n '
            f'FROM "{tbl}"{where_sql} GROUP BY room, ts_key',
            args,
        ).fetchall()
        for row in rows:
            room = str(row["room"] or "")
            key = (str(row["ts_key"] or ""), room)
            item = groups.get(key)
            if item is None:
                item = {"room": room, "datetime": str(row["ts"] or "")}
                groups[key] = item
            item[metric] = round(_num(row["value"]), 4)
            total += int(row["n"] or 0)

    items = list(groups.values())
    items.sort(key=lambda x: x["room"])                 # 时间倒序、同刻按房间正序
    items.sort(key=lambda x: x["datetime"], reverse=True)
    page = _page_slice(items, offset, limit)
    rooms = sorted({item["room"] for item in items})

    out: dict[str, Any] = {
        "detail": True,
        "by_room": True,
        "count": len(page),                 # 本页行数
        "total": total,                     # 采样总数（与聚合前一致）
        "group_count": len(items),          # 聚合后的总行数（房间 × 时间点）
        "truncated": offset + len(page) < len(items),
        "remaining": max(len(items) - offset - len(page), 0),
        "room_count": len(rooms),
        "rooms": rooms,
        # 字段名避开 metrics（run_onthisday_query 会用它回显各指标的展示名）
        "metric_names": metrics,
        "limit": None if limit < 0 else limit,   # null = 不限条数
        "limit_max": None,                       # 不设上限
        "offset": offset,
        "rows": page,
    }
    _append_year_summary(out, conn, src, where_sql, args, params)
    return out


def _q_detail(conn: sqlite3.Connection, src: Source, where: list[str], args: list[Any],
              base: datetime, mmdd: str, params: dict[str, Any]) -> dict[str, Any]:
    """detail：返回历年今日的逐条记录（跨表合并、扁平列表），附各年汇总与总体汇总。

    环境源**默认**走「一个房间一行、多种指标成列」的聚合格式
    （见 _q_detail_env_by_room）——环境分散在多张指标表，平铺时不同指标的值混在同一个
    `value` 列里、无法区分。需要旧格式时传 `env_by_room=0`，此时每行会补一个 `metric`
    字段标明指标名。
    """
    if src.key == "env" and _get_int(params, "env_by_room", 1, 0, 1):
        return _q_detail_env_by_room(conn, src, where, args, base, mmdd, params)

    limit = _paging_limit(params, _DETAIL_LIMIT_DEFAULT)
    offset = _get_int(params, "offset", 0, 0)
    content_len = _get_int(params, "content_len", 0, 0)
    fields = _parse_fields(params)
    drop = _parse_drop_fields(params)
    where_sql = _where_sql(where)

    sort_col = (params.get("sort") or src.time_col).strip().lower()
    if sort_col not in src.cols:
        sort_col = src.time_col
    order = "ASC" if (params.get("order") or "").strip().lower() in ("asc", "1", "true") else "DESC"

    total = 0
    merged: list[dict[str, Any]] = []
    for tbl in src.tables:
        total += int(conn.execute(f'SELECT COUNT(*) FROM "{tbl}"{where_sql}', args).fetchone()[0] or 0)
        rows = conn.execute(
            f'SELECT {_detail_select(conn, tbl, fields, drop)} FROM "{tbl}"{where_sql} '
            f'ORDER BY "{sort_col}" {order} LIMIT ? OFFSET 0',
            [*args, -1 if limit < 0 else limit + offset],   # -1 = 不限（SQLite LIMIT -1）
        ).fetchall()
        table_rows = [dict(row) for row in rows]
        if src.key == "env":
            # 环境是多张指标表合并的，平铺时必须标明每行来自哪个指标，
            # 否则同一条记录的 value 看不出是温度还是湿度。
            metric = _env_metric_of(tbl)
            for item in table_rows:
                item["metric"] = metric
        merged.extend(table_rows)
    merged.sort(key=lambda r: str(r.get(sort_col) or ""), reverse=(order == "DESC"))
    page = _page_slice(merged, offset, limit)
    if content_len:
        for item in page:
            value = item.get("content")
            if isinstance(value, str) and len(value) > content_len:
                item["content"] = value[:content_len] + "…"

    out: dict[str, Any] = {
        "detail": True,
        "count": len(page),
        "total": total,
        # 平铺模式下 total 即为全部匹配行数，据此判断是否被分页截断
        "truncated": offset + len(page) < total,
        "remaining": max(total - offset - len(page), 0),
        "limit": None if limit < 0 else limit,   # null = 不限条数
        "limit_max": None,                       # 不设上限
        "offset": offset,
        "rows": page,
    }

    _append_year_summary(out, conn, src, where_sql, args, params)
    return out


def _q_ranking(conn: sqlite3.Connection, src: Source, where: list[str], args: list[Any],
               base: datetime, mmdd: str, params: dict[str, Any]) -> dict[str, Any]:
    """ranking：按维度排行（维度与指标随数据源而定）。"""
    dim = (params.get("dimension") or src.default_dim).strip().lower()
    col = src.dims.get(dim)
    if not col:
        raise ValueError(
            f"dimension 不支持该数据源，可用：" + "、".join(src.dims) if src.dims
            else "该数据源没有可用的维度"
        )
    by = (params.get("by") or src.default_metric).strip().lower()
    if by not in src.metrics:
        by = src.default_metric

    where_sql = _where_sql(where + [f'"{col}" IS NOT NULL', f'"{col}" <> \'\''])
    columns = _agg_keys(src)
    acc = _grouped_scan(
        conn, src, where_sql, args, f'"{col}"', columns,
        limit=_get_int(params, "scan_limit", 20000, 1, 200000),
    )
    rows: list[dict[str, Any]] = []
    for key, value in acc.items():
        if not key:
            continue
        item: dict[str, Any] = {"key": key}
        item.update(_metrics_out(value, src))
        rows.append(item)
    reverse = (params.get("order") or "desc").strip().lower() not in ("asc", "1", "true")
    rows.sort(key=lambda x: (_num(x.get(by)), _num(x.get("count"))), reverse=reverse)

    limit = _get_int(params, "limit", 20, 1)
    offset = _get_int(params, "offset", 0, 0)
    paged = rows[offset:offset + limit]
    for index, item in enumerate(paged, start=offset + 1):
        item["rank"] = index
    return {
        "dimension": dim,
        "by": by,
        "count": len(paged),
        "total_keys": len(rows),
        "rows": paged,
    }


def _q_crosstab(conn: sqlite3.Connection, src: Source, where: list[str], args: list[Any],
                base: datetime, mmdd: str, params: dict[str, Any]) -> dict[str, Any]:
    """crosstab：行维度 × 列维度 的度量矩阵（维度随数据源而定）。"""
    row_dims = _split_multi(params.get("rows")) or [src.default_dim]
    col_dims = _split_multi(params.get("cols"))
    if len(col_dims) > 1:
        raise ValueError("cols 最多支持 1 个维度（多个维度请放进 rows）")

    def _dim(name: str) -> str:
        col = src.dims.get(name)
        if not col:
            raise ValueError(f"不支持的分组维度 {name}，可用：" + "、".join(src.dims))
        return col

    row_cols = [_dim(n) for n in row_dims[:3]]
    col_cols = [_dim(n) for n in col_dims]
    metric = (params.get("metric") or params.get("by") or src.default_metric).strip().lower()
    if metric not in src.metrics:
        metric = src.default_metric

    conditions = list(where)
    for col in row_cols + col_cols:
        conditions += [f'"{col}" IS NOT NULL', f'"{col}" <> \'\'']
    where_sql = _where_sql(conditions)

    row_expr = " || ' | ' || ".join(f'IFNULL("{c}", \'\')' for c in row_cols)
    col_expr = f'IFNULL("{col_cols[0]}", \'\')' if col_cols else "''"
    columns = _agg_keys(src)

    cells: dict[tuple[str, str], float] = {}
    if col_cols:
        for tbl in src.tables:
            rows = conn.execute(
                f'SELECT {row_expr} AS rk, {col_expr} AS ck, COUNT(*) AS c{_agg_select(columns)} '
                f'FROM "{tbl}"{where_sql} GROUP BY rk, ck',
                args,
            ).fetchall()
            for row in rows:
                key = (str(row["rk"] or ""), str(row["ck"] or ""))
                bucket: dict[str, dict[str, Any]] = {}
                _merge_into(bucket, "_", row, columns)
                cells[key] = cells.get(key, 0.0) + _num(_metrics_out(bucket["_"], src).get(metric))

        limit = _get_int(params, "limit", 30, 1)
        col_limit = _get_int(params, "col_limit", 20, 1, 100)
        # 合计按指标语义合并：极值类取极值，其余求和
        # （否则 metric=max_value 时 grand_total 会变成「各格最大值之和」，没有意义）
        metric_agg = src.metrics.get(metric, {}).get("agg", "sum")

        def _combine(values: list[float]) -> float:
            if not values:
                return 0.0
            if metric_agg == "max":
                return max(values)
            if metric_agg == "min":
                return min(values)
            return sum(values)

        row_values: dict[str, list[float]] = {}
        col_values: dict[str, list[float]] = {}
        for (rk, ck), value in cells.items():
            row_values.setdefault(rk, []).append(value)
            col_values.setdefault(ck, []).append(value)
        row_metric = {k: _combine(v) for k, v in row_values.items()}
        col_metric = {k: _combine(v) for k, v in col_values.items()}
        row_keys = sorted(row_metric, key=lambda k: row_metric[k], reverse=True)[:limit]
        all_cols = sorted(col_metric, key=lambda k: col_metric[k], reverse=True)
        col_keys = []
        for key in all_cols:
            if key and key not in col_keys:
                col_keys.append(key)
        col_keys = col_keys[:col_limit]

        def _round(value: float) -> Any:
            return int(value) if metric == "count" else round(value, 4)

        return {
            "rows_dim": row_dims[:3],
            "cols_dim": col_dims,
            "metric": metric,
            "row_keys": row_keys,
            "col_keys": col_keys,
            "matrix": [[_round(cells.get((rk, ck), 0.0)) for ck in col_keys] for rk in row_keys],
            "row_metric": [_round(row_metric[k]) for k in row_keys],
            "col_metric": [_round(col_metric[k]) for k in col_keys],
            "grand_total": _round(_combine(list(cells.values()))),
        }

    acc = _grouped_scan(conn, src, where_sql, args, row_expr, columns,
                        limit=_get_int(params, "scan_limit", 20000, 1, 200000))
    rows_out: list[dict[str, Any]] = []
    for key in acc:
        if not key:
            continue
        item: dict[str, Any] = {"key": key}
        item.update(_metrics_out(acc[key], src))
        rows_out.append(item)
    rows_out.sort(key=lambda x: _num(x.get(metric)), reverse=True)
    limit = _get_int(params, "limit", 30, 1)
    return {
        "rows_dim": row_dims[:3],
        "cols_dim": [],
        "metric": metric,
        "count": len(rows_out[:limit]),
        "rows": rows_out[:limit],
    }


_MODES = {
    "stats": _q_stats,
    "detail": _q_detail,
    "ranking": _q_ranking,
    "crosstab": _q_crosstab,
}


def run_onthisday_query(db_path: str, params: dict[str, Any]) -> dict[str, Any]:
    """同步执行「历史今日」查询（在 executor 线程中调用）。

    排除实体在入口处统一注入：来源为 api_settings 设置（db_viewer → 系统配置 →
    📜 历史今日）＋本次参数 `exclude_entities`（临时追加）。
    API 与传感器都经由本函数，因此两者自动遵守同一份设置。

    注意：与 params 中显式传入的 `entity_ids` / `entity_id` 是「叠加」关系
    （先按 entity_ids 收窄，再剔除排除项），两者同时给出时取交集。
    """
    mode = (params.get("mode") or "stats").strip().lower()
    handler = _MODES.get(mode)
    if handler is None:
        raise ValueError("mode 只能是 " + " / ".join(MODES))

    params = dict(params)
    # 通讯源的类型名留空时自动探测：「属性提取」里可能用了自定义类型名，
    # 此时不该因为默认名 comm_records 找不到表就报错（device / env 源会忽略该参数）。
    if not str(params.get("type_name") or "").strip():
        from .comm import resolve_comm_type_name  # 延迟导入，复用同一套解析
        params["type_name"] = resolve_comm_type_name(db_path)
    params["__exclude_ids"] = _exclude_ids(db_path, params)

    conn = _connect(db_path)
    try:
        src = _build_source(conn, params)
        where, args, base, mmdd = _base_filters(src, params)
        result = handler(conn, src, where, args, base, mmdd, params)
        result.update({
            "mode": mode,
            "source": src.key,
            "source_label": src.label,
            "tables": src.tables,
            "unit": src.unit,
            "on_this_day": mmdd,
            "base_date": base.strftime("%Y-%m-%d"),
            "metrics": {k: v["label"] for k, v in src.metrics.items()},
            "dimensions": sorted(src.dims),
            "exclude_entities": params["__exclude_ids"],
            "exclude_count": len(params["__exclude_ids"]),
        })
        if src.key == "comm":
            # 回显实际生效的通讯类型名（可能是自动探测的结果）
            result["type_name"] = params.get("type_name") or ""
        return result
    finally:
        conn.close()


# =========================================================================== #
#  HTTP API                                                                   #
# =========================================================================== #
class OnThisDayView(_BaseDBView):
    """历史今日查询 API（通讯 / 设备 / 环境通用）。

    GET|POST /api/ha_data_store/onthisday?source=comm|device|env&mode=...
    详见模块头部文档。
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

        hass: HomeAssistant = request.app["hass"]
        # 通讯类型名：显式参数 > 设置实体 > attr_type_defs 自动探测（后两步在查询内完成）
        # 此前这里完全没处理——即使设置了实体，comm 源仍会去找默认的 comm_records。
        if not str(params.get("type_name") or "").strip():
            from .comm import read_comm_type_name_setting  # 延迟导入，避免模块互相导入
            setting = read_comm_type_name_setting(hass)
            if setting:
                params["type_name"] = setting

        try:
            result = await self._exec_in_executor(
                hass, run_onthisday_query, self._db_path, params
            )
        except ValueError as exc:
            return self.json({"success": False, "error": str(exc)}, status_code=400)
        except Exception as exc:
            _LOGGER.exception("[onthisday] 查询失败 source=%s", params.get("source"))
            return self.json({"success": False, "error": str(exc)}, status_code=500)

        payload = {"success": True, "type": "onthisday", "source": params.get("source") or "comm"}
        payload.update(result or {})
        return self.json(payload)


def register_api_views(hass: HomeAssistant, db_path: str) -> None:
    """注册「历史今日」查询 API。由 __init__._register_api_views 调用。"""
    hass.http.register_view(OnThisDayView(db_path))
    _LOGGER.info("[onthisday] API 已注册：%s", VIEW_URL)
