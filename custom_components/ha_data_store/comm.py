"""通讯数据查询模块 — 独立模块。

数据来源
--------
本模块 **不含采集逻辑**。数据由「系统配置 → 属性提取」的 **通讯数据采集模式**
（`mode=comm`，默认 `type_name=comm_records`）从 HA 实体的属性数组中采集，
写入动态表 `attr_comm_records`，字段固定为 const.py 中 `COMM_FIELDS` 定义的 16 列：
my_number / party_number / party_place / party_name / time / location /
msg_type / channel / call_type / duration / cost / content

采集时对值做了规范化：`time` 统一为 'YYYY-MM-DD HH:MM:SS'；`duration` 支持
'3分53秒' / '27秒' / '1小时2分3秒' / '3:53'（分:秒）/ '233'（秒）等写法，
自动换算为整数秒；`cost` 转为数值。因此查询侧可直接用 SUM(duration) / SUM(cost)。

查询 API
--------
    GET|POST /api/ha_data_store/comm?type=<查询类型>&...

通用过滤参数（所有 type 均支持，多值参数用逗号分隔）：
    type_name       数据表对应的属性类型名。**一般无需填写**——留空时按
                    「text.ha_data_store_comm_type_name 实体 → attr_type_defs 中
                    mode=comm 且表已存在者 → mode=comm 的类型名 → comm_records」
                    的顺序自动解析（见 resolve_comm_type_name）；
                    仅在需要明确指向某张表时才显式传参
    date            单日  YYYY-MM-DD
    month           单月  YYYY-MM
    year            单年  YYYY
    start / end     时间区间（含边界），'YYYY-MM-DD' 或 'YYYY-MM-DD HH:MM:SS'
    my_numbers      我方号码
    party_numbers   对方号码
    numbers         同时匹配 my_number 与 party_number
    party_names     对方姓名（模糊匹配）
    party_places    对方归属地（模糊匹配）
    places          通讯地点（模糊匹配）
    channels        数据来源
    msg_types       消息类型
    call_types      呼叫类型
    keyword         消息内容模糊匹配
    min_duration / max_duration   时长区间（秒）
    min_cost / max_cost           金额区间（元）
    content_len     消息内容截断长度（0=不截断；超出时截断并追加 …）
    fields          只返回指定列（逗号分隔，如 time,party_number,duration,content）；
                    留空 = 全部列。对 records / detail / longest / contact 生效

type 一览：
    records   明细列表，额外支持 sort/order/limit/offset/content_len
    chat      ★ 聊天记录：以**对端**为中心的一整段对话（含正文），一次给出合计 /
              分渠道 / 分消息类型 / 分天 —— 做「聊天窗口」只要一个请求。
              必填 party_numbers 或 party_names；默认时间正序（对话从上往下读）；
              支持 start/end（可到时分秒，end 为包含语义）
    dates     时间段内「哪些日期有数据」，返回每天的条数/时长/金额/流量/联系人数
    geoflows  ★ 地点坐标流向：从「我的地点」到「对方地点」（接听类反过来），给迁徙图直接用。
              返回 **cities**（name / coord / count / mine / other / duration / cost…）与
              **flows**（from / to / fromCoord / toCoord / dir / count / seconds…）——
              与前端 `_commGeoGraph()` **同形**，拿到就能喂给地图，不必再聚合一次。
              方向判据与前端一致（`msg_type` 里的接听类 ↔ 呼叫类），未接等不编造方向
              （只计地点次数、不成边）；同城不成边。
              `min_count` 滤噪音，`limit` / `nodes_limit` 控规模，
              `resolve=1`（默认）用本地坐标表给缺坐标的地名现算。别名 flows
    months    ★ 时间段内「哪些年月有数据」—— dates 的月粒度版本。不填条件即**全库**。
              返回 **months**（纯清单 ["2019-08", …]，最常见的用法）、**years**
              （按年归拢，每年带自己的月份数组与合计）、**rows**（每月一行，与 dates 同形）
              以及 span（首末年月）、total_records。过滤条件与 dates 完全一致，
              所以"某人哪些年月有数据"叠加 party_names / party_numbers 即可。
              注意：`0000-00` 这类脏值在 SQL 里就被排除了，不占 limit 名额
    calendar  ★ 活跃日历：某月**每一天**的汇总，**缺的天补 0**
              （dates 只返回有数据的天，日历要完整格子才画得出来），
              另附该月 1 号是周几（first_weekday，0=周日）与月度合计 / 静默天数
    stale     ★ 失联名单：超过 N 天没联系的联系人，按失联天数从久到近。
              阈值在 SQL 里用 MAX(time) <= cutoff 过滤，cutoff 由 Python 按**本地时间**
              算好传入 —— 不用 SQLite 的 julianday('now')，那个是 UTC，会差 8 小时
    duration_dist ★ 通话时长分布：按**时长**分桶（默认 <10秒 / 10秒-1分 / 1-5分 /
              5-30分 / 30分-2小时 / ≥2小时），给直方图用；buckets 可自定义边界
    new_peers ★ 新联系人：**全库首次联系时间**落在指定范围内的号码
              （判据不是"范围内有记录"，那样会把老朋友也算进来）
    peak      ★ 最忙的时段 / 日子：dim=date|hour|weekday|month|year|day，
              by=count|duration|cost|traffic_usage。
              ranking 排的是"人 / 地点 / 类型"，peak 排的是**时间本身**
    stats     ★ 统计分析：按时间粒度分组汇总，返回每桶的条数/时长/金额/
              去重联系人数/活跃天数/平均时长，并附合计（total）与每桶均值
              （avg_per_bucket）；额外支持 granularity / sort / order / by /
              limit / fill / with_total / with_avg
    crosstab  ★ 交叉汇总：行维度 × 列维度 的度量矩阵
              rows=msg_type,location&cols=party_name&metric=duration
              cols 留空则只输出各行合计
    compare   ★ 周期对比：当前周期 vs 上一周期（环比）vs 去年同期（同比），
              输出差值与增长率；period=day|week|month|quarter|year
    contact   ★ 单联系人档案：某号码 / 姓名的总量、首末通讯、多久没联系、
              平均间隔、各维度分布、最近明细（party_number 或 party_name）
    meta      ★ 数据概览：表结构、总量、时间跨度、维度取值分布
              （channels / msg_types / call_types / party_places / places / years / months）
    longest   ★ 单次 Top N：最长通话 / 最高金额（by=duration|cost）
    quality   ★ 数据质量：空值 / 异常值 / 疑似重复 / 时间覆盖
    onthisday ★ 历史上的今日：匹配「月日相同」的历年记录，再按 mode 输出
              mode=stats|detail|ranking|crosstab（见下节）
              实现已统一到 onthisday.py，同一套代码也服务设备 / 环境；
              本入口等价于 source=comm，设备与环境用 /api/ha_data_store/onthisday
    ranking   排行榜：按维度排名，额外支持 granularity/period/dimension/by/limit/offset
    trend     趋势序列：时间桶的条数/时长/金额（画折线用），额外支持 granularity/fill
    summary   汇总统计（总量、首末时间、总时长、总金额、联系人数 + 各维度分布）
    parties   联系人清单（按对方号码聚合，含条数/时长/金额/首末时间）
    places    地点清单（按通讯地点聚合）
    heatmap   星期 × 小时 分布

「历史上的今日」（type=onthisday，别名 anniversary）
    实现位于 onthisday.py（同一套代码服务通讯 / 设备 / 环境三类数据源）。
    本接口是 source=comm 的兼容入口；设备与环境请直接用：
        GET /api/ha_data_store/onthisday?source=device|env&...
    按「月日相同」筛选历年同一天的记录，默认基准为今天。
    date              基准日：'2026-09-29' 或 '09-29'；留空 = 今天
    mode              输出模式（默认 stats）
                        stats     历年今日汇总，granularity=year|month|day（默认 year）
                        detail    ★ 历年今日详细明细：逐条记录（扁平列表）+ 各年汇总 + 总体汇总
                        ranking   历年今日排行榜（dimension + by）
                        parties   历年今日联系人汇总（对方号码/姓名 + 条数/时长/金额）
                        records   历年今日明细（可限定时刻，见 at / hour）
                        crosstab  历年今日交叉汇总（rows × cols）
    years             只看指定年份，多值：2024,2025（或 min_year / max_year）
    exclude_current=1 排除今年，只看往年
    at                只看某时刻：'HH:MM' 或 'now'（此刻）
    align             时刻对齐：hour（整点）/ 30 / 15 / 5 / min（分钟）
                      align=hour → 现在 09:02 取「09:00~10:00」
    hours / minutes   跨度：自对齐后的起点向后 N 小时 / N 分钟
                      align=hour&hours=2 → 09:00~11:00；at=09:02&minutes=30 → 09:02~09:32
    window            以 at 为中心的分钟窗口（原行为）：at=17:24&window=30 → 17:24±30 分钟
    hour              不看具体时刻时按时段筛选：9 / 9,10 / 9-18
    granularity       stats 模式：year（每年今日）/ month（每年该月）/ day（每年该日）
    limit / offset    detail 模式：明细条数与偏移（默认 100 / 0，上限 1000）
    sort / order      detail 模式：明细排序字段与方向（默认 time desc）
    with_years=0      detail 模式：不附带各年汇总与总体汇总
    years_limit       detail 模式：最多汇总多少个年份（默认 20）
    content_len       detail 模式：明细内容截断长度（默认 0 = 不截断）

    例：
        ?type=onthisday                                    历年今日按年汇总
        ?type=onthisday&granularity=day&year=2025          2025 年的今日（按日）
        ?type=onthisday&mode=detail                        历年今日逐条明细（附各年汇总）
        ?type=onthisday&mode=detail&limit=50&offset=50      分页取明细
        ?type=onthisday&mode=detail&party_names=张三&content_len=200
        ?type=onthisday&mode=detail&at=now&align=hour       只看「本小时」的逐条明细
        ?type=onthisday&mode=ranking&dimension=party_name&by=duration
        ?type=onthisday&mode=parties&limit=10              历年今日联系最多的人
        ?type=onthisday&mode=records&at=now                「此刻」历年今日的记录
        ?type=onthisday&mode=records&at=now&align=hour     历年今日「本小时」（09:00~10:00）
        ?type=onthisday&mode=records&at=now&align=hour&hours=2   历年今日「最近 2 小时」
        ?type=onthisday&mode=records&at=09:02&minutes=30   09:02 起的 30 分钟
        ?type=onthisday&mode=records&at=17:24&window=60    历年今日 17:24 ± 1 小时
        ?type=onthisday&mode=crosstab&rows=msg_type&cols=party_name
        ?type=onthisday&mode=crosstab&rows=location,call_type&metric=duration
    「历史今日」可与通用过滤叠加（party_numbers / msg_types / places /
    party_places / call_types / keyword …），用于「历年今日我都在和谁通电话」这类查询。

granularity 取值（stats / trend 通用，支持别名）：
    year / quarter / month / week / day / hour / weekday
    别名：年|yyyy|yearly、季度|q|quarterly、月|ym|monthly、周|weekly、日|date|ymd 等

stats 与 trend 的区别：
    trend 只给「时间桶 + 条数/时长/金额」，用于画折线；
    stats 额外给「去重联系人数 / 活跃天数 / 平均时长」并在响应里附合计与均值，
    sort=value 时还能按指标（by=count|duration|cost）排序，取 Top N 时间桶。

    三种常见统计的写法（时间范围由通用过滤参数决定）：
        全部数据按年汇总     ?type=stats&granularity=year
        全部数据按年月汇总   ?type=stats&granularity=month
        指定年 → 按月汇总    ?type=stats&granularity=month&year=2026
        指定年月 → 按日汇总  ?type=stats&granularity=day&month=2026-09
        指定号码 → 按年汇总  ?type=stats&granularity=year&party_numbers=138…,139…
        指定姓名 → 按月汇总  ?type=stats&granularity=month&party_names=张三,李四
        指定姓名 → 按日汇总  ?type=stats&granularity=day&party_names=张三&month=2026-09
    （stats 也接受 period 参数：?type=stats&granularity=quarter&period=2026-Q3）

示例：
    /api/ha_data_store/comm?type=records&date=2026-09-01&party_names=张三&key=xxx
    /api/ha_data_store/comm?type=dates&month=2026-09&party_numbers=13800000000&key=xxx
    /api/ha_data_store/comm?type=months&key=xxx                          # 全库哪些年月有数据
    /api/ha_data_store/comm?type=months&party_names=张三&key=xxx          # 某人哪些年月有数据
    /api/ha_data_store/comm?type=geoflows&key=xxx                        # 全库地点流向
    /api/ha_data_store/comm?type=geoflows&channels=语音&min_count=2&key=xxx # 只说语音、至少通过两次
    /api/ha_data_store/comm?type=stats&granularity=month&year=2026&fill=1&key=xxx
    /api/ha_data_store/comm?type=stats&granularity=day&month=2026-09&party_names=张三&with_avg=1&key=xxx
    /api/ha_data_store/comm?type=ranking&granularity=month&period=2026-09&dimension=party_name&by=duration&limit=20&key=xxx
    /api/ha_data_store/comm?type=trend&granularity=day&month=2026-09&key=xxx
"""
from __future__ import annotations

import logging
import re
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from aiohttp import web
from homeassistant.core import HomeAssistant

from .const import (
    ATTR_MODE_COMM,
    COMM_DEFAULT_TYPE_NAME,
    COMM_TYPE_NAME_ENTITY_ID,
    TABLE_ATTR_TYPE_DEFS,
    get_attr_table_name,
)
from .http_api import _BaseDBView

_LOGGER = logging.getLogger(__name__)

# 通讯查询接口版本。与 db_viewer.html 里的 COMM_VIEWER_EXPECTED_VERSION 配套：
#   每个响应都会带 `api_version`，前端据此提示"服务端接口和页面不匹配"——
#   这套接口最近加得比较密（chat / calendar / stale / duration_dist / new_peers / peak …），
#   有版本号就能一眼确认 Home Assistant 重启后新接口到底生效了没有。
#   **改动接口行为时顺手把它 +1。**
COMM_API_VERSION = "1.3.0"   # 1.3.0：geoflows 支持 md（只要某个「月日」，供「历史今日」地图用）

COMM_VIEW_URL = "/api/ha_data_store/comm"
COMM_VIEW_NAME = "api:ha_data_store:comm"

_TIME_FMT = "%Y-%m-%d %H:%M:%S"

# 明细返回列（按此顺序输出，实际以表中存在的列为准）
_OUTPUT_COLUMNS = (
    "id", "my_number", "party_number", "party_place", "party_name",
    "time", "location", "msg_type", "channel", "call_type",
    "duration", "cost", "traffic_usage", "traffic_type", "content", "image_path",
    "location_coordinate", "party_isp", "party_coordinate",
)

# 排行榜维度白名单：参数值 → 列名
_RANK_DIMENSIONS = {
    "party_number": "party_number",
    "party_name": "party_name",
    "location": "location",
    "party_place": "party_place",
    "my_number": "my_number",
    "channel": "channel",
    "msg_type": "msg_type",
    "call_type": "call_type",
    # 业务类型（普通流量 / 4G / 5G …）：加进白名单后
    #   `crosstab&rows=traffic_type` 与 `ranking&dimension=traffic_type` 立即可用
    "traffic_type": "traffic_type",
}

# 明细排序字段白名单
_SORT_COLUMNS = {
    "time": "time",
    "duration": "duration",
    "cost": "cost",
    "traffic_usage": "traffic_usage",
    "party_name": "party_name",
    "party_number": "party_number",
    "id": "id",
}

# 趋势 / 统计的时间桶表达式（key 为 granularity，顺序即输出顺序）
_BUCKET_EXPR = {
    "day": "SUBSTR(time, 1, 10)",
    "week": "STRFTIME('%Y-W%W', time)",
    "month": "SUBSTR(time, 1, 7)",
    "quarter": "SUBSTR(time, 1, 4) || '-Q' || CAST((CAST(SUBSTR(time, 6, 2) AS INTEGER) + 2) / 3 AS TEXT)",
    "year": "SUBSTR(time, 1, 4)",
    "hour": "SUBSTR(time, 12, 2)",
    "weekday": "STRFTIME('%w', time)",
}

# 时序连续、可被 fill=1 补全空缺的时间桶
_FILLABLE_BUCKETS = ("day", "week", "month", "quarter", "year")

# granularity 参数别名（含中文，方便直接拼 URL）
_GRANULARITY_ALIASES = {
    "date": "day", "daily": "day", "ymd": "day", "日": "day", "按日": "day",
    "weekly": "week", "周": "week", "按周": "week",
    "ym": "month", "monthly": "month", "月": "month", "按月": "month",
    "quarterly": "quarter", "q": "quarter", "季": "quarter", "季度": "quarter",
    "yyyy": "year", "yearly": "year", "annual": "year", "年": "year", "按年": "year",
    "时": "hour", "小时": "hour",
}


class CommTableMissing(ValueError):
    """通讯数据表（attr_<type_name>）尚未创建。"""


# =========================================================================== #
#  工具函数                                                                    #
# =========================================================================== #
def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _load_columns(conn: sqlite3.Connection, tbl: str, type_name: str) -> set[str]:
    """确认通讯数据表存在并返回列名集合。"""
    exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (tbl,)
    ).fetchone()
    if not exists:
        raise CommTableMissing(
            f"未找到通讯数据表 {tbl}。请先在「系统配置 → 属性提取」中新增一个"
            f"「通讯数据采集」模式的配置（类型名 {type_name}），"
            f"待采集到数据后再查询。"
        )
    return {row[1] for row in conn.execute(f'PRAGMA table_info("{tbl}")')}


def read_comm_type_name_setting(hass: HomeAssistant | None) -> str:
    """读取 HA 实体 `text.ha_data_store_comm_type_name`（通讯数据表类型名设置）。

    必须在事件循环线程调用（要访问 `hass.states`）。留空、缺失、`unknown` /
    `unavailable` / `none` / `-` / `auto` 一律返回空串，交给 `resolve_comm_type_name`
    继续自动探测。

    三处调用方共用本函数，避免「传感器读了设置、API 却没读」这类不一致：
      · 传感器 TodayInHistorySensor
      · 通讯查询 API CommApiView
      · 历史今日 API OnThisDayView
    """
    if hass is None:
        return ""
    try:
        state = hass.states.get(COMM_TYPE_NAME_ENTITY_ID)
        raw = str(getattr(state, "state", "") or "").strip()
    except Exception:  # noqa: BLE001 - 读实体失败不应让查询整体失败
        return ""
    if not raw or raw.lower() in ("unknown", "unavailable", "none", "-", "auto"):
        return ""
    return raw


def resolve_comm_type_name(db_path: str, requested: str = "") -> str:
    """确定通讯数据表的类型名（决定表名 `attr_<type_name>`）。

    解析优先级：
      1. 显式 `requested`（API 的 `type_name` 参数 / 传感器读到的设置实体）
      2. `attr_type_defs` 中 `mode='comm'` 且**数据表已存在**的类型名（按 type_name 排序取首个）
      3. `attr_type_defs` 中 `mode='comm'` 的类型名（表尚未建，至少给出正确的名字）
      4. `COMM_DEFAULT_TYPE_NAME`（`comm_records`）

    第 2 步让「在「属性提取」里用了自定义类型名」的用户无需任何额外配置即可查到数据；
    存在多张通讯表时结果稳定地取排序第一个，可用 `type_name` 参数显式指定。
    """
    name = str(requested or "").strip()
    if name:
        return name
    try:
        conn = sqlite3.connect(db_path)
    except Exception:  # noqa: BLE001 - 库不可读时回退默认名，由后续查询给出明确报错
        return COMM_DEFAULT_TYPE_NAME
    try:
        try:
            rows = conn.execute(
                f"SELECT type_name FROM {TABLE_ATTR_TYPE_DEFS} "
                f"WHERE LOWER(IFNULL(mode, '')) = ? ORDER BY type_name",
                (ATTR_MODE_COMM,),
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        first = ""
        for row in rows:
            candidate = str(row[0] or "").strip()
            if not candidate:
                continue
            if not first:
                first = candidate
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (get_attr_table_name(candidate),),
            ).fetchone():
                return candidate
        return first or COMM_DEFAULT_TYPE_NAME
    finally:
        conn.close()


def _num(value: Any) -> float:
    if value is None:
        return 0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0


def _get_int(params: dict[str, Any], key: str, default: int, lo: int | None = None,
             hi: int | None = None) -> int:
    # 注意：不能用 `params.get(key) or ""`——数字 0 是 falsy，会被误判成「未提供」
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


def _get_float(params: dict[str, Any], key: str) -> float | None:
    raw = (params.get(key) or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _split_multi(raw: Any) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        items = [str(v) for v in raw]
    else:
        items = str(raw).replace("，", ",").split(",")
    return [v.strip() for v in items if v.strip()]


def _parse_dt(text: str) -> datetime | None:
    """宽松解析时间文本：支持 YYYY-MM-DD / YYYY-MM-DD HH:MM / YYYY-MM-DD HH:MM:SS。"""
    t = (text or "").strip().replace("T", " ")
    if not t:
        return None
    candidates = [t, t[:19], t[:16], t[:10]]
    for candidate in dict.fromkeys(candidates):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(candidate, fmt)
            except ValueError:
                continue
    return None


def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    idx = year * 12 + (month - 1) + delta
    return idx // 12, idx % 12 + 1


def _resolve_time_range(params: dict[str, Any]) -> tuple[str, str, str]:
    """解析时间过滤条件 → (start_inclusive, end_exclusive, label)，空串表示不过滤。

    优先级：start/end > date > month > year
    """
    start_raw = (params.get("start") or "").strip()
    end_raw = (params.get("end") or "").strip()
    if start_raw or end_raw:
        start_dt = _parse_dt(start_raw)
        end_dt = _parse_dt(end_raw)
        start = start_dt.strftime(_TIME_FMT) if start_dt else ""
        end = ""
        if end_dt:
            # end 为包含语义，转为独占上界：
            # 只给日期（2026-09-30）→ 取到当天结束（次日 00:00:00）
            # 给了具体时刻（2026-09-30 12:00:00）→ 再加 1 秒
            if len(end_raw.replace("T", " ")) <= 10:
                end = (end_dt + timedelta(days=1)).strftime(_TIME_FMT)
            else:
                end = (end_dt + timedelta(seconds=1)).strftime(_TIME_FMT)
        return start, end, "custom"

    date_raw = (params.get("date") or "").strip()
    if date_raw:
        d = _parse_dt(date_raw)
        if d:
            return (d.strftime(_TIME_FMT),
                    (d + timedelta(days=1)).strftime(_TIME_FMT),
                    d.strftime("%Y-%m-%d"))

    month_raw = (params.get("month") or "").strip()
    if month_raw:
        year = month = 0
        try:
            year, month = int(month_raw[:4]), int(month_raw[5:7])
        except (ValueError, IndexError):
            year = month = 0
        if year and 1 <= month <= 12:
            ny, nm = _shift_month(year, month, 1)
            return (f"{year:04d}-{month:02d}-01 00:00:00",
                    f"{ny:04d}-{nm:02d}-01 00:00:00",
                    f"{year:04d}-{month:02d}")

    year_raw = (params.get("year") or "").strip()
    if year_raw:
        try:
            year = int(year_raw[:4])
        except ValueError:
            year = 0
        if year:
            return (f"{year:04d}-01-01 00:00:00",
                    f"{year + 1:04d}-01-01 00:00:00",
                    f"{year:04d}")

    return "", "", ""


def _normalize_period(params: dict[str, Any]) -> dict[str, Any]:
    """把 granularity + period 归一化为 date / month / year / start+end 参数。"""
    period = (params.get("period") or "").strip()
    raw = (params.get("granularity") or "").strip().lower()
    granularity = _GRANULARITY_ALIASES.get(raw, raw)
    if not period or not granularity:
        return params
    if any((params.get(k) or "").strip() for k in ("date", "month", "year", "start", "end")):
        return params
    if granularity == "day":
        params["date"] = period
    elif granularity == "month":
        params["month"] = period
    elif granularity == "year":
        params["year"] = period
    elif granularity in ("quarter", "week"):
        # period 形如 '2026-Q3' / '2026-W35'，换算为闭区间 start ~ end
        parts = period.split("-")
        try:
            year = int(parts[0])
            unit = int(parts[1].lstrip("QqWw")) if len(parts) > 1 else 0
        except (ValueError, IndexError):
            return params
        if granularity == "quarter" and 1 <= unit <= 4:
            first_month = (unit - 1) * 3 + 1
            ny, nm = _shift_month(year, first_month, 3)
            params["start"] = f"{year:04d}-{first_month:02d}-01"
            params["end"] = (datetime(ny, nm, 1) - timedelta(days=1)).strftime("%Y-%m-%d")
        elif granularity == "week" and 0 <= unit <= 53:
            try:
                week_start = datetime.strptime(f"{year}-W{unit:02d}-1", "%Y-W%W-%w")
            except ValueError:
                return params
            params["start"] = week_start.strftime("%Y-%m-%d")
            params["end"] = (week_start + timedelta(days=6)).strftime("%Y-%m-%d")
    return params


def _resolve_granularity(params: dict[str, Any], default: str) -> str:
    """解析 granularity 参数（支持别名），非法值抛 ValueError。"""
    raw = (params.get("granularity") or "").strip().lower()
    if not raw:
        return default
    mapped = _GRANULARITY_ALIASES.get(raw, raw)
    if mapped not in _BUCKET_EXPR:
        raise ValueError("granularity 只能是 " + " / ".join(_BUCKET_EXPR))
    return mapped


def _where_sql(where: list[str]) -> str:
    return (" WHERE " + " AND ".join(where)) if where else ""


def _and_sql(where_sql: str, condition: str) -> str:
    """在已有 WHERE 片段后追加条件（自动处理有无 WHERE 两种情形）。"""
    return f"{where_sql} AND {condition}" if where_sql else f" WHERE {condition}"


def _build_filters(params: dict[str, Any], cols: set[str]) -> tuple[list[str], list[Any]]:
    """构建过滤条件（时间 + 多值匹配 + 数值区间）。"""
    where: list[str] = []
    args: list[Any] = []

    start, end, _label = _resolve_time_range(params)
    if "time" in cols:
        if start:
            where.append("time >= ?")
            args.append(start)
        if end:
            where.append("time < ?")
            args.append(end)

    def _multi(param_name: str, column: str, fuzzy: bool = False,
               alias: str | None = None) -> None:
        if column not in cols:
            return
        values = _split_multi(params.get(param_name))
        if not values and alias:
            # 单数别名：party_name / party_number / my_number 等价于多值参数的单值写法
            values = _split_multi(params.get(alias))
        if not values:
            return
        parts = []
        for value in values:
            if fuzzy:
                parts.append(f'"{column}" LIKE ?')
                args.append(f"%{value}%")
            else:
                parts.append(f'"{column}" = ?')
                args.append(value)
        where.append("(" + " OR ".join(parts) + ")")

    _multi("my_numbers", "my_number", alias="my_number")
    _multi("party_numbers", "party_number", alias="party_number")
    _multi("party_names", "party_name", fuzzy=True, alias="party_name")
    _multi("party_places", "party_place", fuzzy=True)
    _multi("places", "location", fuzzy=True)
    _multi("channels", "channel")
    _multi("msg_types", "msg_type")
    _multi("call_types", "call_type")

    # numbers：同时匹配我方与对方号码
    numbers = _split_multi(params.get("numbers"))
    if numbers:
        parts, sub = [], []
        for value in numbers:
            if "my_number" in cols:
                parts.append('"my_number" = ?')
                sub.append(value)
            if "party_number" in cols:
                parts.append('"party_number" = ?')
                sub.append(value)
        if parts:
            where.append("(" + " OR ".join(parts) + ")")
            args.extend(sub)

    keyword = (params.get("keyword") or "").strip()
    if keyword and "content" in cols:
        where.append('"content" LIKE ?')
        args.append(f"%{keyword}%")

    if "duration" in cols:
        min_duration = _get_float(params, "min_duration")
        if min_duration is not None:
            where.append('"duration" >= ?')
            args.append(min_duration)
        max_duration = _get_float(params, "max_duration")
        if max_duration is not None:
            where.append('"duration" <= ?')
            args.append(max_duration)

    if "cost" in cols:
        min_cost = _get_float(params, "min_cost")
        if min_cost is not None:
            where.append('"cost" >= ?')
            args.append(min_cost)
        max_cost = _get_float(params, "max_cost")
        if max_cost is not None:
            where.append('"cost" <= ?')
            args.append(max_cost)

    return where, args


def _collect_filters(params: dict[str, Any], cols: set[str]) -> tuple[list[str], list[Any]]:
    """构建过滤条件；若 params 带 __extra_where / __extra_args 则一并合并。

    __extra_* 供「历史今日」等扩展查询注入额外约束（月日相同、年份限定、时刻窗口），
    使扩展查询可以直接复用现有的明细 / 统计 / 排行 / 联系人等实现。
    """
    where, args = _build_filters(params, cols)
    extra_where = params.get("__extra_where")
    if extra_where:
        where = where + list(extra_where)
        args = args + list(params.get("__extra_args") or [])
    return where, args


def _with_time_not_empty(where: list[str], cols: set[str]) -> list[str]:
    if "time" in cols:
        return where + ["time <> ''"]
    return where


def _select_expr(cols: set[str], fields: list[str] | None = None) -> str:
    """生成 SELECT 列清单；fields 指定时只返回这些列（顺序按用户给定，自动去重）。"""
    wanted: list[str] = []
    for name in (fields or _OUTPUT_COLUMNS):
        if name in cols and name not in wanted:
            wanted.append(name)
    if not wanted:
        wanted = [c for c in _OUTPUT_COLUMNS if c in cols]
    if not wanted:
        wanted = [c for c in ("id", "time") if c in cols]
    return ", ".join(f'"{c}"' for c in wanted) or "*"


def _parse_fields(params: dict[str, Any]) -> list[str] | None:
    """解析 fields 参数（逗号分隔的返回字段清单）；留空返回 None 表示返回全部列。"""
    values = [v.lower() for v in _split_multi(params.get("fields"))]
    return values or None


def _sum_expr(column: str, cols: set[str]) -> str:
    return f'SUM("{column}")' if column in cols else "0"


def _avg_expr(column: str, cols: set[str]) -> str:
    return f'AVG("{column}")' if column in cols else "0"


def _metric_expr(by: str, cols: set[str]) -> str:
    if by == "duration" and "duration" in cols:
        return 'SUM("duration")'
    if by == "cost" and "cost" in cols:
        return 'SUM("cost")'
    if by == "traffic_usage" and "traffic_usage" in cols:
        return 'SUM("traffic_usage")'
    return "COUNT(*)"


def _time_bounds_expr(cols: set[str]) -> tuple[str, str]:
    """返回 (最早时间表达式, 最晚时间表达式)；表无 time 列时退化为空串。"""
    if "time" in cols:
        return "MIN(time)", "MAX(time)"
    return "''", "''"


def _trim_content(rows: list[dict[str, Any]], max_len: int) -> None:
    if max_len <= 0:
        return
    for row in rows:
        value = row.get("content")
        if isinstance(value, str) and len(value) > max_len:
            row["content"] = value[:max_len] + "…"


def _shape_agg_row(row: Any) -> dict[str, Any]:
    data = dict(row)
    if "count" in data:
        data["count"] = int(data.get("count") or 0)
    for key in ("duration",):
        if key in data:
            data[key] = _num(data.get(key))
    if "cost" in data:
        data["cost"] = round(_num(data.get("cost")), 4)
    if "traffic_usage" in data:
        data["traffic_usage"] = round(_num(data.get("traffic_usage")), 4)
    return data


# =========================================================================== #
#  各查询类型实现                                                                #
# =========================================================================== #
# 明细（records）分页：只设默认值，**不设上限**。
#   limit 缺省 → 默认条数；limit=0（或负数）→ 不限条数；其余按传入值返回。
# 响应里的 limit_max 恒为 null，表示无上限。
_RECORDS_LIMIT_DEFAULT = 100


def _query_records(db_path: str, type_name: str, params: dict[str, Any]) -> dict[str, Any]:
    """明细列表查询。"""
    tbl = get_attr_table_name(type_name)
    conn = _connect(db_path)
    try:
        cols = _load_columns(conn, tbl, type_name)
        where, args = _collect_filters(params, cols)
        where_sql = _where_sql(where)

        order_col = _SORT_COLUMNS.get((params.get("sort") or "time").strip().lower(), "time")
        if order_col not in cols:
            order_col = "time" if "time" in cols else ("id" if "id" in cols else "")
        order_dir = "ASC" if (params.get("order") or "").strip().lower() in ("asc", "1", "true") else "DESC"
        order_sql = f' ORDER BY "{order_col}" {order_dir}' if order_col else ""

        limit = _get_int(params, "limit", _RECORDS_LIMIT_DEFAULT, None)
        if limit <= 0:
            limit = -1                      # 不限条数（SQLite LIMIT -1）
        offset = _get_int(params, "offset", 0, 0)

        total = int(
            conn.execute(f'SELECT COUNT(*) FROM "{tbl}"{where_sql}', args).fetchone()[0] or 0
        )
        rows = conn.execute(
            f'SELECT {_select_expr(cols, _parse_fields(params))} FROM "{tbl}"{where_sql}{order_sql} LIMIT ? OFFSET ?',
            [*args, limit, offset],
        ).fetchall()
        result = [dict(row) for row in rows]
        _trim_content(result, _get_int(params, "content_len", 0, 0))
        return {
            "count": len(result),
            "total": total,
            "truncated": offset + len(result) < total,
            "remaining": max(total - offset - len(result), 0),
            "limit": None if limit < 0 else limit,   # null = 不限条数
            "limit_max": None,                       # 不设上限
            "offset": offset,
            "rows": result,
        }
    finally:
        conn.close()


def _query_dates(db_path: str, type_name: str, params: dict[str, Any]) -> dict[str, Any]:
    """某时间段内「哪些日期有数据」。"""
    tbl = get_attr_table_name(type_name)
    conn = _connect(db_path)
    try:
        cols = _load_columns(conn, tbl, type_name)
        if "time" not in cols:
            raise ValueError("通讯数据表缺少 time 列，无法按日期统计")
        where, args = _collect_filters(params, cols)
        where = _with_time_not_empty(where, cols)
        duration_expr = _sum_expr("duration", cols)
        cost_expr = _sum_expr("cost", cols)
        party_expr = 'COUNT(DISTINCT "party_number")' if "party_number" in cols else "0"
        limit = _get_int(params, "limit", 400, 1)

        rows = conn.execute(
            f'SELECT SUBSTR(time, 1, 10) AS date, COUNT(*) AS count, '
            f'{duration_expr} AS duration, {cost_expr} AS cost, '
            f'{_sum_expr("traffic_usage", cols)} AS traffic_usage, {party_expr} AS party_count '
            f'FROM "{tbl}"{_where_sql(where)} '
            f'GROUP BY SUBSTR(time, 1, 10) ORDER BY date ASC LIMIT ?',
            [*args, limit],
        ).fetchall()
        result = [_shape_agg_row(row) for row in rows]
        for item in result:
            item["party_count"] = int(item.get("party_count") or 0)
        _, _, label = _resolve_time_range(params)
        total_count = sum(item["count"] for item in result)
        return {
            "range": label,
            "count": len(result),
            "days": len(result),
            "total_records": total_count,
            "rows": result,
        }
    finally:
        conn.close()


def _query_months(db_path: str, type_name: str, params: dict[str, Any]) -> dict[str, Any]:
    """全部数据里「哪些年月有数据」—— 与 `_query_dates` 是一对（那个按**日**，这个按**月**）。

    用途：
        · 回答"这些数据横跨哪些年、每年哪几个月有动静"；
        · 前端可以拿它把月份选择器的可选范围收敛到**真的有数据**的那一段
          （现在的做法是按当前年份往前写死 20 年）。

    过滤条件与 dates 完全一致（同一套通用过滤：year / month / start / end /
    party_numbers / party_names / channels / places …），所以"某人哪些年月有数据"
    直接叠加即可，不用另写一套。

    返回：
        months        纯清单（`["2019-08", "2019-09", …]`，按 order 排）—— 最常用的那一个：
                      只想知道"哪些年月"时不必再去解析 rows
        years         按年归拢（`[{year, months: [...], count, duration, cost}]`），
                      省得调用方自己 group；年内的月份同样按 order 排
        rows          每月一行（与 dates 同形：count / duration / cost / traffic_usage /
                      party_count / active_days），要逐月指标时用它
        span          首末年月（`{first, last}`），一眼看出数据跨度
        total_records 各月条数之和
    """
    tbl = get_attr_table_name(type_name)
    conn = _connect(db_path)
    try:
        cols = _load_columns(conn, tbl, type_name)
        if "time" not in cols:
            raise ValueError("通讯数据表缺少 time 列，无法按月统计")
        where, args = _collect_filters(params, cols)
        where = _with_time_not_empty(where, cols)
        # **脏值过滤下沉到 SQL**：库里确实存在 `0000-00-00 00:00:00` 这类时间（占位 / 导入残留），
        #   它们不是"有数据的年月"。只在 Python 侧剔除的话，`LIMIT` 会先被脏值占掉名额 ——
        #   `0000-00` 在字典序上排在 `2019-08` **前面**，实测 `limit=3` 时取回
        #   `0000-00 / 2019-08 / 2019-09`，剔完只剩 2 个真月份（少给了一个月）。
        #   GLOB 比 strftime 快，也不受格式/时区影响；下面的 Python 侧再兜一层
        where = where + [
            "time GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'",
            "SUBSTR(time, 1, 4) <> '0000'",
            "SUBSTR(time, 6, 2) <> '00'",
        ]
        duration_expr = _sum_expr("duration", cols)
        cost_expr = _sum_expr("cost", cols)
        party_expr = 'COUNT(DISTINCT "party_number")' if "party_number" in cols else "0"
        day_expr = 'COUNT(DISTINCT SUBSTR(time, 1, 10))'
        # 月份数上限：40 年 × 12 = 480，默认 1200 足够宽松。
        #   lo 传 None（不是 1）：`limit=0` 是"不限条数"，写 1 会被夹成 1
        limit = _get_int(params, "limit", 1200, None)
        if limit <= 0:
            limit = -1                      # SQLite：LIMIT -1 = 不限条数

        order_raw = (params.get("order") or "").strip().lower()
        desc = order_raw in ("desc", "1", "true")
        # `YYYY-MM` 是定长字符串，字典序即时间序，直接 ORDER BY 即可
        rows = conn.execute(
            f'SELECT SUBSTR(time, 1, 7) AS month, COUNT(*) AS count, '
            f'{duration_expr} AS duration, {cost_expr} AS cost, '
            f'{_sum_expr("traffic_usage", cols)} AS traffic_usage, {party_expr} AS party_count, '
            f'{day_expr} AS active_days '
            f'FROM "{tbl}"{_where_sql(where)} '
            f'GROUP BY SUBSTR(time, 1, 7) '
            f'ORDER BY month {"DESC" if desc else "ASC"} LIMIT ?',
            [*args, limit],
        ).fetchall()

        result: list[dict[str, Any]] = []
        for row in rows:
            item = _shape_agg_row(row)
            month = str(item.get("month") or "")
            # 脏数据兜底：库里确实存在 `0000-00-00 00:00:00` 这类时间（占位/导入残留），
            #   它们不是"有数据的年月"；混进清单会让前端的可选范围凭空多出几年
            if (len(month) != 7 or month[4] != "-"
                    or not month[:4].isdigit() or not month[5:].isdigit()):
                continue
            if month[:4] == "0000" or month[5:] == "00":
                continue
            item["month"] = month
            item["year"] = month[:4]
            item["party_count"] = int(item.get("party_count") or 0)
            item["active_days"] = int(item.get("active_days") or 0)
            result.append(item)

        # 按年归拢。月份已按时间排序 ⇒ **同一年必然连续**，所以只需看上一行（升序降序都成立）；
        #   上面的脏值过滤也不会打断它 —— 被滤掉的本来就不是这一年的正常月份
        years: list[dict[str, Any]] = []
        for item in result:
            last = years[-1] if years else None
            if not last or last["year"] != item["year"]:
                last = {"year": item["year"], "months": [], "count": 0, "duration": 0, "cost": 0.0}
                years.append(last)
            last["months"].append(item["month"])
            last["count"] += item["count"]
            last["duration"] += item["duration"]
            last["cost"] = round(last["cost"] + item["cost"], 4)

        _, _, label = _resolve_time_range(params)
        months = [item["month"] for item in result]
        return {
            "range": label,
            "count": len(result),
            "month_count": len(result),
            "months": months,
            "years": years,
            "year_count": len(years),
            # 首末按月取值（不是取 `rows` 的首尾）：`order=desc` 时清单是倒着的
            "span": {
                "first": min(months) if months else "",
                "last": max(months) if months else "",
            },
            "total_records": sum(item["count"] for item in result),
            "rows": result,
        }
    finally:
        conn.close()


def _query_chat(db_path: str, type_name: str, params: dict[str, Any]) -> dict[str, Any]:
    """聊天记录：**以对端为中心**的一整段对话（含正文），一次给出各维度分布。

    与 records 的区别：
        · `records` 是通用明细，不带"这是和谁的对话"的语义；
          `chat` 必须指定对端（party_numbers / party_names），
          并把这段对话的**合计 / 分渠道 / 分消息类型 / 分天**一起返回 ——
          前端做「聊天窗口」原来要自己拼 3~4 个请求，现在一个够。
        · 时间范围走通用过滤：`start` / `end` 支持到时分秒，`end` 为**包含**语义，
          所以"某段时间内的聊天记录"直接写起止时间即可（优先级 start/end > date > month > year）。
        · 默认 `order=asc`：对话是从上往下读的，时间正序比倒序自然。

    参数：party_numbers / party_names（**必填其一**）、start / end / month / year、
          channels / msg_types / keyword / min_duration / max_duration / min_cost / max_cost、
          content_len（截断正文）、limit（默认 200，0=不限）/ offset、order（asc|desc）
          **mode = both（默认）| detail | summary** ——
            detail  只回**逐条聊天内容**（+ 分页信息），不返回任何 by_* 汇总，也不跑汇总那几条 SQL
            summary 只回汇总；both 两者都给
          **empty_content = 1（默认）| 0** —— 内容（content）为空的记录**是否显示**；
            传 0 只留正文非空的那批（语音 / 短信的绝大多数没有正文，看聊天内容时是噪音）。
            它作用在共用 WHERE 上，汇总会一起收窄
    """
    params = _normalize_period(dict(params))
    numbers = (params.get("party_numbers") or "").strip()
    names = (params.get("party_names") or "").strip()
    if not numbers and not names:
        raise ValueError("chat 需要 party_numbers 或 party_names（指定对话对象）；查全库明细请用 records")
    # mode 决定"回什么"：
    #   detail  —— **只给逐条明细**（含正文），不返回任何 by_* 汇总，也不做汇总那几条 SQL
    #   summary —— 只给汇总
    #   both    —— 两者都给（默认，保持兼容）
    mode = (params.get("mode") or "both").strip().lower()
    if mode not in ("both", "detail", "summary"):
        raise ValueError("mode 只能是 both / detail / summary（detail=逐条聊天内容）")
    want_summary = mode in ("both", "summary")

    tbl = get_attr_table_name(type_name)
    conn = _connect(db_path)
    try:
        cols = _load_columns(conn, tbl, type_name)
        if "time" not in cols:
            raise ValueError("通讯数据表缺少 time 列，无法读聊天记录")
        where, args = _collect_filters(params, cols)
        where = _with_time_not_empty(where, cols)
        # `empty_content=0` → 只保留**正文非空**的记录（内容为空的都不显示）；
        #   默认 1 = 照常返回全部，所以不传这个参数时行为与以前完全一致。
        #   过滤加在**共用的 WHERE** 上：汇总（total / by_*）会跟着一起收窄 ——
        #   这样"筛掉空内容之后的统计"与明细是同一份数据，不会自相矛盾
        if not _get_int(params, "empty_content", 1, 0, 1) and "content" in cols:
            # 带 TRIM：全是空白的也算"空"（与前端 `_commContentOf` 的 trim 判据一致）
            where = where + ['TRIM(IFNULL("content", \'\')) <> \'\'']
        where_sql = _where_sql(where)

        party_expr = 'COUNT(DISTINCT "party_number")' if "party_number" in cols else "0"
        day_expr = 'COUNT(DISTINCT SUBSTR(time, 1, 10))'
        first_expr, last_expr = _time_bounds_expr(cols)

        total_row = conn.execute(
            f'SELECT COUNT(*) AS count, {_sum_expr("duration", cols)} AS duration, '
            f'{_sum_expr("cost", cols)} AS cost, {_sum_expr("traffic_usage", cols)} AS traffic_usage, '
            f'{party_expr} AS party_count, {day_expr} AS active_days, '
            f'{first_expr} AS first_time, {last_expr} AS last_time '
            f'FROM "{tbl}"{where_sql}',
            args,
        ).fetchone()
        total = _stats_row(total_row) if total_row else {}

        def _dist(column: str, limit: int = 60) -> list[dict[str, Any]]:
            """按某个维度分布（维度为空值的行剔除）。"""
            if column not in cols:
                return []
            # 条件单独拼：f-string 里再嵌同类型引号在 3.12 之前是语法错误
            cond = list(where)
            cond.append('"{0}" IS NOT NULL'.format(column))
            cond.append('"{0}" <> \'\''.format(column))
            rows = conn.execute(
                f'SELECT "{column}" AS key, COUNT(*) AS count, '
                f'{_sum_expr("duration", cols)} AS duration, {_sum_expr("cost", cols)} AS cost '
                f'FROM "{tbl}"{_where_sql(cond)} '
                f'GROUP BY "{column}" ORDER BY count DESC LIMIT ?',
                [*args, limit],
            ).fetchall()
            return [_stats_row(r) for r in rows]

        # 按天：对话流的分段依据（前端据此插日期分隔）。只在要汇总时才查
        by_day = conn.execute(
            f'SELECT SUBSTR(time, 1, 10) AS date, COUNT(*) AS count, '
            f'{_sum_expr("duration", cols)} AS duration, {_sum_expr("cost", cols)} AS cost, '
            f'{_sum_expr("traffic_usage", cols)} AS traffic_usage '
            f'FROM "{tbl}"{where_sql} GROUP BY SUBSTR(time, 1, 10) ORDER BY date ASC',
            args,
        ).fetchall() if want_summary else []

        order = (params.get("order") or "asc").strip().lower()
        order = "DESC" if order.startswith("desc") else "ASC"
        # 同一秒可能有多条：带 id 才稳定（表里没有 id 时退化为只按时间）
        order_cols = f'time {order}, id {order}' if "id" in cols else f'time {order}'
        limit = _get_int(params, "limit", 200, 0)
        if limit <= 0:
            limit = -1
        offset = _get_int(params, "offset", 0, 0)

        # 只要汇总时不查明细（summary 模式）—— 省一次全表扫描
        result: list[dict[str, Any]] = []
        if mode in ("both", "detail"):
            rows_out = conn.execute(
                f'SELECT {_select_expr(cols, _parse_fields(params))} FROM "{tbl}"{where_sql} '
                f'ORDER BY {order_cols} LIMIT ? OFFSET ?',
                [*args, limit, offset],
            ).fetchall()
            result = [dict(r) for r in rows_out]
            _trim_content(result, _get_int(params, "content_len", 0, 0))

        out: dict[str, Any] = {
            "mode": mode,
            "peer": {
                "party_numbers": numbers,
                "party_names": names,
                "match": "party_numbers" if numbers else "party_names",
            },
            "range": _resolve_time_range(params)[2],
            "count": len(result) if mode != "summary" else int(total.get("count") or 0),
            "limit": None if limit < 0 else limit,
            "offset": offset,
            "order": order.lower(),
        }
        if mode == "detail":
            # 只给明细：连 total 都只留一个数字（分页要知道总数），
            #   不返回任何聚合块 —— 用这个入口的人要的就是"一条条聊天内容"
            out["total_count"] = int(total.get("count") or 0)
            out["rows"] = result
        else:
            out["total"] = total
            out["active_days"] = int(total.get("active_days") or 0)
            if want_summary:
                out["by_channel"] = _dist("channel")
                out["by_msg_type"] = _dist("msg_type")
                out["by_day"] = [_stats_row(r) for r in by_day]
            if mode == "both":
                out["rows"] = result
        return out
    finally:
        conn.close()


def _query_calendar(db_path: str, type_name: str, params: dict[str, Any]) -> dict[str, Any]:
    """活跃日历：某个月**每一天**的汇总，**缺的天补 0**。

    与 `dates` 只差一条、但很关键：`dates` 只返回"有数据的天"，
    日历要的是完整的一格一格 —— 缺的天必须补 0，前端才画得出空白格。
    另外多给「该月 1 号是周几」（前端排版要用）与月度合计 / 静默天数。

    月份来源：`month=2025-09`；没给就从 `start` 推断。
    同样支持 party_numbers / party_names / channels 等通用过滤。
    """
    params = _normalize_period(dict(params))
    month = (params.get("month") or "").strip()
    if not month:
        dt = _parse_dt((params.get("start") or "").strip())
        if dt:
            month = dt.strftime("%Y-%m")
    ok = len(month) == 7 and month[4] == "-" and month[:4].isdigit() and month[5:].isdigit()
    if not ok:
        raise ValueError("calendar 需要 month=YYYY-MM（或能推断出月份的 start）")

    year, mon = int(month[:4]), int(month[5:7])
    if not (1 <= mon <= 12):
        raise ValueError("calendar 的 month 月份非法")
    # 该月天数：下个月 1 号减本月 1 号（不引 calendar 模块）
    ny, nm = _shift_month(year, mon, 1)
    days_in_month = (datetime(ny, nm, 1) - datetime(year, mon, 1)).days

    # 复用 dates 拿"有数据的天"，再补零
    data = _query_dates(db_path, type_name, dict(params, month=month))
    have = {str(r.get("date")): r for r in (data.get("rows") or [])}

    rows: list[dict[str, Any]] = []
    for day in range(1, days_in_month + 1):
        key = f"{year:04d}-{mon:02d}-{day:02d}"
        hit = have.get(key)
        if hit:
            row = _stats_row(dict(hit))
            row["has_data"] = True
        else:
            row = {"date": key, "count": 0, "duration": 0, "cost": 0, "traffic_usage": 0,
                   "party_count": 0, "active_days": 0, "avg_duration": 0}
            row["has_data"] = False
        row["day"] = day
        # 0=周日（前端日历常用）；同时给 0=周一 的版本，免得前端再换算
        row["weekday_sun0"] = (datetime(year, mon, day).weekday() + 1) % 7
        row["weekday"] = datetime(year, mon, day).weekday()
        rows.append(row)

    active = [r for r in rows if r["has_data"]]
    return {
        "month": month,
        "days": days_in_month,
        # 该月 1 号是周几（0=周日）——前端排版第一个格子要空几格
        "first_weekday": rows[0]["weekday_sun0"] if rows else 0,
        "active_days": len(active),
        "rest_days": days_in_month - len(active),
        "range": data.get("range"),
        "total": {
            "count": sum(r["count"] for r in rows),
            "duration": sum(_num(r["duration"]) for r in rows),
            "cost": round(sum(_num(r["cost"]) for r in rows), 4),
            "traffic_usage": round(sum(_num(r["traffic_usage"]) for r in rows), 4),
        },
        "rows": rows,
    }


def _query_stale(db_path: str, type_name: str, params: dict[str, Any]) -> dict[str, Any]:
    """失联名单：**超过 N 天没联系**的联系人，按失联天数从久到近排。

    与 parties 的区别：parties 排的是"最常联系的人"（按总量），
    这里排的是"最久没联系的人"（按每个号码的 `last_time`）—— 用来找"该主动联系一下"的人。

    阈值在 SQL 里用 `MAX(time) <= cutoff` 过滤，cutoff 由 Python 按**本地时间**算好传进去；
    不用 SQLite 的 `julianday('now')`，那个是 UTC，会跟库里的本地时间差 8 小时。

    参数：days（阈值天数，默认 30）/ min_count（历史最少条数，默认 1，滤掉只出现一次的陌生号）
          / max_count（可选上限，0=不限）/ by = days（默认）| count | duration / limit / offset
    """
    days = _get_int(params, "days", 30, 0)
    min_count = _get_int(params, "min_count", 1, 1)
    max_count = _get_int(params, "max_count", 0, 0)
    by = (params.get("by") or "days").strip().lower()
    limit = _get_int(params, "limit", 50, 1)
    offset = _get_int(params, "offset", 0, 0)

    tbl = get_attr_table_name(type_name)
    conn = _connect(db_path)
    try:
        cols = _load_columns(conn, tbl, type_name)
        if "party_number" not in cols or "time" not in cols:
            raise ValueError("通讯数据表缺少 party_number / time 列，无法统计失联名单")

        where, args = _collect_filters(params, cols)
        where = _with_time_not_empty(where, cols)
        where = where + ['"party_number" IS NOT NULL', '"party_number" <> \'\'']

        now = datetime.now()
        cutoff = (now - timedelta(days=days)).strftime(_TIME_FMT)
        name_expr = 'MAX("party_name")' if "party_name" in cols else "''"
        first_expr, last_expr = _time_bounds_expr(cols)

        having = ["COUNT(*) >= ?", f'MAX(time) <= ?']
        having_args: list[Any] = [min_count, cutoff]
        if max_count > 0:
            having.append("COUNT(*) <= ?")
            having_args.append(max_count)

        # 一律取回来后在 Python 里按 by 排序再截断：失联天数是算出来的，
        #   而 count / duration 两种排序又不与它同序，SQL 里排不了
        rows = conn.execute(
            f'SELECT "party_number" AS party_number, {name_expr} AS party_name, '
            f'COUNT(*) AS count, {_sum_expr("duration", cols)} AS duration, '
            f'{_sum_expr("cost", cols)} AS cost, {_sum_expr("traffic_usage", cols)} AS traffic_usage, '
            f'{first_expr} AS first_time, {last_expr} AS last_time '
            f'FROM "{tbl}"{_where_sql(where)} '
            f'GROUP BY "party_number" HAVING {" AND ".join(having)}',
            [*args, *having_args],
        ).fetchall()

        result = []
        for row in rows:
            item = _stats_row(row)
            last_dt = _parse_dt(str(item.get("last_time") or ""))
            item["days_since_last"] = (now.date() - last_dt.date()).days if last_dt else None
            result.append(item)

        if by == "count":
            result.sort(key=lambda x: (-(x.get("count") or 0), str(x.get("last_time") or "")))
        elif by == "duration":
            result.sort(key=lambda x: (-(x.get("duration") or 0), str(x.get("last_time") or "")))
        else:
            # 默认：失联越久越靠前
            result.sort(key=lambda x: str(x.get("last_time") or ""))

        total = len(result)
        page = result[offset:offset + limit]
        return {
            "as_of": now.strftime(_TIME_FMT),
            "threshold_days": days,
            "cutoff": cutoff,
            "min_count": min_count,
            "by": by,
            "count": len(page),
            "total": total,
            "offset": offset,
            "limit": limit,
            "rows": page,
        }
    finally:
        conn.close()


def _query_duration_dist(db_path: str, type_name: str, params: dict[str, Any]) -> dict[str, Any]:
    """通话时长分布：按**时长区间**分桶，给直方图用。

    与 longest 的区别：longest 给"最长的几条"，看不出整体习惯；
    这里给"各时长段各有多少条"，一眼能看出是"多而短"还是"少而长"。
    与 stats 的区别：stats 按时间分桶，这里按**时长**分桶。

    参数：buckets（自定义边界，逗号分隔的秒数，默认 10,60,300,1800,7200
          → <10秒 / 10秒-1分 / 1-5分 / 5-30分 / 30分-2小时 / ≥2小时）
          only_positive（默认 1：只统计 duration > 0 的记录 ——
                        短信 / 微信的 duration 恒为 0，不排掉会全落进第一个桶）
    """
    raw = (params.get("buckets") or "").strip()
    if raw:
        parts = [x.strip() for x in raw.replace("，", ",").split(",") if x.strip()]
        try:
            edges = sorted({int(float(x)) for x in parts})
        except ValueError:
            raise ValueError("buckets 需要是逗号分隔的秒数，如 10,60,300,1800")
        if not edges:
            raise ValueError("buckets 不能为空")
    else:
        edges = [10, 60, 300, 1800, 7200]
    only_positive = _get_int(params, "only_positive", 1, 0, 1)

    tbl = get_attr_table_name(type_name)
    conn = _connect(db_path)
    try:
        cols = _load_columns(conn, tbl, type_name)
        if "duration" not in cols:
            raise ValueError("通讯数据表缺少 duration 列，无法统计时长分布")

        where, args = _collect_filters(params, cols)
        where = _with_time_not_empty(where, cols)
        if only_positive:
            where = where + ['IFNULL("duration", 0) > 0']

        # CASE WHEN 分桶：0 → <e1、1 → [e1,e2)、…、len(edges) → >= 最后一个边界
        whens = " ".join(
            "WHEN IFNULL(duration, 0) < {0} THEN {1}".format(edge, idx)
            for idx, edge in enumerate(edges)
        )
        bucket_expr = "CASE {0} ELSE {1} END".format(whens, len(edges))

        rows = conn.execute(
            f'SELECT {bucket_expr} AS bucket, COUNT(*) AS count, '
            f'{_sum_expr("duration", cols)} AS duration, {_sum_expr("cost", cols)} AS cost '
            f'FROM "{tbl}"{_where_sql(where)} GROUP BY bucket ORDER BY bucket',
            args,
        ).fetchall()

        def _label(idx: int) -> str:
            if idx == 0:
                return "< {0}".format(_fmt_dur_short(edges[0]))
            if idx >= len(edges):
                return "≥ {0}".format(_fmt_dur_short(edges[-1]))
            return "{0} ~ {1}".format(_fmt_dur_short(edges[idx - 1]), _fmt_dur_short(edges[idx]))

        got = {int(r["bucket"]): r for r in rows}
        total = sum(int(r["count"]) for r in rows) or 0
        result = []
        for idx in range(len(edges) + 1):
            row = got.get(idx)
            count = int(row["count"]) if row else 0
            result.append({
                "idx": idx,
                "key": _label(idx),
                "min": 0 if idx == 0 else edges[idx - 1],
                "max": edges[idx] if idx < len(edges) else None,
                "count": count,
                "duration": _num(row["duration"]) if row else 0,
                "cost": round(_num(row["cost"]), 4) if row else 0,
                "pct": round(count / total * 100, 2) if total else 0,
            })

        _, _, label = _resolve_time_range(params)
        return {
            "range": label,
            "buckets": edges,
            "only_positive": bool(only_positive),
            "total": total,
            "rows": result,
        }
    finally:
        conn.close()


def _query_new_peers(db_path: str, type_name: str, params: dict[str, Any]) -> dict[str, Any]:
    """新联系人：**首次联系时间落在指定范围**内的号码。

    判据是 `MIN(time)`（全库首次）落在范围内，而不是"范围内有记录" ——
    后者会把老朋友也算进来。用它发现"这个月新出现的陌生号"很直接。

    注意：`HAVING` 用的是**全库**的 `MIN(time)`，所以 `count` / `duration` 这些
    统计的是这个号码的**全部**记录，不是范围内那部分（新号码通常本来就都在范围内）。
    参数：时间范围（start/end/month/year …）、min_count（最少条数，默认 1）、limit / offset
    """
    tbl = get_attr_table_name(type_name)
    conn = _connect(db_path)
    try:
        cols = _load_columns(conn, tbl, type_name)
        if "party_number" not in cols or "time" not in cols:
            raise ValueError("通讯数据表缺少 party_number / time 列，无法统计新联系人")

        # 时间范围单独解析：它不进 WHERE，而是进 HAVING（判"首次"）
        p = dict(params)
        for key in ("start", "end", "date", "month", "year", "period", "granularity"):
            p.pop(key, None)
        where, args = _collect_filters(p, cols)
        where = _with_time_not_empty(where, cols)
        where = where + ['"party_number" IS NOT NULL', '"party_number" <> \'\'']

        start_s, end_s, label = _resolve_time_range(params)
        min_count = _get_int(params, "min_count", 1, 1)
        limit = _get_int(params, "limit", 100, 1)
        offset = _get_int(params, "offset", 0, 0)

        having = ["COUNT(*) >= ?"]
        having_args: list[Any] = [min_count]
        if start_s:
            having.append("MIN(time) >= ?")
            having_args.append(start_s)
        if end_s:
            having.append("MIN(time) < ?")
            having_args.append(end_s)
        if not start_s and not end_s:
            raise ValueError("new_peers 需要时间范围（start/end 或 month/year），否则「新」无从判断")

        name_expr = 'MAX("party_name")' if "party_name" in cols else "''"
        first_expr, last_expr = _time_bounds_expr(cols)
        rows = conn.execute(
            f'SELECT "party_number" AS party_number, {name_expr} AS party_name, '
            f'COUNT(*) AS count, {_sum_expr("duration", cols)} AS duration, '
            f'{_sum_expr("cost", cols)} AS cost, {_sum_expr("traffic_usage", cols)} AS traffic_usage, '
            f'{first_expr} AS first_time, {last_expr} AS last_time '
            f'FROM "{tbl}"{_where_sql(where)} '
            f'GROUP BY "party_number" HAVING {" AND ".join(having)} '
            f'ORDER BY first_time DESC LIMIT ? OFFSET ?',
            [*args, *having_args, limit, offset],
        ).fetchall()

        result = [_stats_row(r) for r in rows]
        return {
            "range": label,
            "match": "全库首次联系时间落在该范围内",
            "min_count": min_count,
            "count": len(result),
            "limit": limit,
            "offset": offset,
            "rows": result,
        }
    finally:
        conn.close()


# 峰值统计可选的时间维度（键 → SQL 表达式）
_PEAK_DIMS = {
    "date": 'SUBSTR(time, 1, 10)',        # 具体某天：2025-09-15
    "month": 'SUBSTR(time, 1, 7)',        # 年月：2025-09
    "year": 'SUBSTR(time, 1, 4)',
    "hour": 'SUBSTR(time, 12, 2)',        # 一天里的第几小时
    "weekday": "STRFTIME('%w', time)",    # 0=周日
    "day": 'SUBSTR(time, 9, 2)',          # 月内第几天
}

_WEEKDAY_NAMES = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"]


def _query_peak(db_path: str, type_name: str, params: dict[str, Any]) -> dict[str, Any]:
    """最忙的时段 / 日子：按**时间维度**排 Top N。

    与 ranking 的区别：ranking 排的是「人 / 地点 / 类型」，peak 排的是**时间本身**。
    与 heatmap 的区别：heatmap 给完整矩阵，peak 只给前几名，拿来直接显示。

    参数：dim = date（默认，具体某天）| hour | weekday | month | year | day
          by  = count（默认）| duration | cost | traffic_usage
          limit（默认 10）/ 以及通用时间与条件过滤
    """
    dim = (params.get("dim") or "date").strip().lower()
    if dim not in _PEAK_DIMS:
        raise ValueError("dim 只能是 " + " / ".join(_PEAK_DIMS))
    by = (params.get("by") or "count").strip().lower()
    if by not in ("count", "duration", "cost", "traffic_usage"):
        raise ValueError("by 只能是 count / duration / cost / traffic_usage")
    limit = _get_int(params, "limit", 10, 1)

    tbl = get_attr_table_name(type_name)
    conn = _connect(db_path)
    try:
        cols = _load_columns(conn, tbl, type_name)
        if "time" not in cols:
            raise ValueError("通讯数据表缺少 time 列，无法统计时段")
        where, args = _collect_filters(params, cols)
        where = _with_time_not_empty(where, cols)

        metric = _metric_expr(by, cols)
        expr = _PEAK_DIMS[dim]
        rows = conn.execute(
            f'SELECT {expr} AS key, COUNT(*) AS count, '
            f'{_sum_expr("duration", cols)} AS duration, {_sum_expr("cost", cols)} AS cost, '
            f'{_sum_expr("traffic_usage", cols)} AS traffic_usage '
            f'FROM "{tbl}"{_where_sql(where)} '
            f'GROUP BY key ORDER BY {metric} DESC, count DESC LIMIT ?',
            [*args, limit],
        ).fetchall()

        result = []
        for index, row in enumerate(rows, start=1):
            item = _stats_row(row)
            item["rank"] = index
            item["label"] = _peak_label(dim, str(item.get("key") or ""))
            item["value"] = _num(
                item.get(by) if by != "count" else item.get("count")
            )
            result.append(item)

        _, _, label = _resolve_time_range(params)
        return {
            "dim": dim,
            "by": by,
            "range": label,
            "count": len(result),
            "limit": limit,
            "rows": result,
        }
    finally:
        conn.close()


def _fmt_dur_short(seconds: int) -> str:
    """桶边界的人话写法：60 → '1 分'、3600 → '1 小时'（配合时长分布用）。"""
    s = int(seconds)
    if s % 3600 == 0 and s >= 3600:
        return "{0} 小时".format(s // 3600)
    if s % 60 == 0 and s >= 60:
        return "{0} 分".format(s // 60)
    return "{0} 秒".format(s)


def _peak_label(dim: str, key: str) -> str:
    """峰值行的显示标签（把裸键变成人话）。"""
    if dim == "hour":
        return "{0} 时".format(key.zfill(2)) if key.isdigit() else key
    if dim == "weekday":
        try:
            return _WEEKDAY_NAMES[int(key)]
        except (ValueError, IndexError):
            return key
    if dim == "day":
        try:
            return "{0} 号".format(int(key))
        except ValueError:
            return key
    return key


def _query_ranking(db_path: str, type_name: str, params: dict[str, Any]) -> dict[str, Any]:
    """排行榜：按维度聚合排序（可先用 granularity+period 限定年/月/日）。"""
    params = _normalize_period(dict(params))
    tbl = get_attr_table_name(type_name)
    conn = _connect(db_path)
    try:
        cols = _load_columns(conn, tbl, type_name)
        dimension = (params.get("dimension") or "party_name").strip().lower()
        dim_col = _RANK_DIMENSIONS.get(dimension)
        if not dim_col or dim_col not in cols:
            available = "、".join(k for k in _RANK_DIMENSIONS if _RANK_DIMENSIONS[k] in cols)
            raise ValueError(f"dimension 不支持或该列不存在，可用维度：{available}")

        by = (params.get("by") or "count").strip().lower()
        granularity = (params.get("granularity") or "").strip().lower()
        period = (params.get("period") or "").strip()
        limit = _get_int(params, "limit", 20, 1)
        offset = _get_int(params, "offset", 0, 0)

        where, args = _collect_filters(params, cols)
        where = _with_time_not_empty(where, cols)
        where = where + [f'"{dim_col}" IS NOT NULL', f'"{dim_col}" <> \'\'']

        metric = _metric_expr(by, cols)
        rows = conn.execute(
            f'SELECT "{dim_col}" AS key, COUNT(*) AS count, '
            f'{_sum_expr("duration", cols)} AS duration, {_sum_expr("cost", cols)} AS cost '
            f'FROM "{tbl}"{_where_sql(where)} '
            f'GROUP BY "{dim_col}" ORDER BY {metric} DESC, count DESC LIMIT ? OFFSET ?',
            [*args, limit, offset],
        ).fetchall()
        result = []
        for index, row in enumerate(rows, start=offset + 1):
            item = _shape_agg_row(row)
            item["rank"] = index
            result.append(item)
        return {
            "dimension": dimension,
            "by": by,
            "granularity": granularity or "all",
            "period": period,
            "rows": result,
        }
    finally:
        conn.close()


def _fill_buckets(rows: list[dict[str, Any]], granularity: str, start: str,
                  end: str) -> list[dict[str, Any]]:
    """按天/月/年补全缺失的时间桶（无数据补 0）。"""
    bucket_map = {str(row.get("bucket")): row for row in rows}
    start_dt = _parse_dt(start)
    end_dt = _parse_dt(end)
    if not start_dt or not end_dt:
        return rows
    filled: list[dict[str, Any]] = []
    cursor = start_dt
    guard = 0
    while cursor < end_dt and guard < 5000:
        guard += 1
        if granularity == "day":
            key = cursor.strftime("%Y-%m-%d")
            cursor = cursor + timedelta(days=1)
        elif granularity == "week":
            key = cursor.strftime("%Y-W%W")
            cursor = cursor + timedelta(days=7)
        elif granularity == "month":
            key = cursor.strftime("%Y-%m")
            year, month = _shift_month(cursor.year, cursor.month, 1)
            cursor = datetime(year, month, 1)
        elif granularity == "quarter":
            key = f"{cursor.year:04d}-Q{(cursor.month - 1) // 3 + 1}"
            year, month = _shift_month(cursor.year, cursor.month, 3)
            cursor = datetime(year, month, 1)
        elif granularity == "year":
            key = cursor.strftime("%Y")
            cursor = datetime(cursor.year + 1, 1, 1)
        else:
            return rows
        if key in bucket_map:
            filled.append(bucket_map.pop(key))
        else:
            filled.append({"bucket": key, "count": 0, "duration": 0, "cost": 0.0})
    filled.extend(bucket_map.values())
    return filled


def _query_trend(db_path: str, type_name: str, params: dict[str, Any]) -> dict[str, Any]:
    """趋势序列：按时间桶统计。"""
    params = _normalize_period(dict(params))
    tbl = get_attr_table_name(type_name)
    conn = _connect(db_path)
    try:
        cols = _load_columns(conn, tbl, type_name)
        if "time" not in cols:
            raise ValueError("通讯数据表缺少 time 列，无法做趋势统计")
        granularity = _resolve_granularity(params, "day")
        bucket_expr = _BUCKET_EXPR[granularity]

        where, args = _collect_filters(params, cols)
        where = _with_time_not_empty(where, cols)
        limit = _get_int(params, "limit", 1000, 1)

        rows = conn.execute(
            f'SELECT {bucket_expr} AS bucket, COUNT(*) AS count, '
            f'{_sum_expr("duration", cols)} AS duration, {_sum_expr("cost", cols)} AS cost '
            f'FROM "{tbl}"{_where_sql(where)} '
            f'GROUP BY bucket ORDER BY bucket ASC LIMIT ?',
            [*args, limit],
        ).fetchall()
        result = [_shape_agg_row(row) for row in rows]

        fill = _get_int(params, "fill", 0, 0, 1)
        if fill and granularity in _FILLABLE_BUCKETS:
            start, end, _label = _resolve_time_range(params)
            if start and end:
                result = _fill_buckets(result, granularity, start, end)
        return {
            "granularity": granularity,
            "count": len(result),
            "rows": result,
        }
    finally:
        conn.close()


def _stats_row(row: Any) -> dict[str, Any]:
    """规范化分组统计行（补全补空桶缺失的指标）。"""
    data = _shape_agg_row(row)
    data["count"] = int(data.get("count") or 0)
    data["duration"] = _num(data.get("duration"))
    data["cost"] = round(_num(data.get("cost")), 4)
    # 流量：上网数据量（MB）。与 duration（上网时长）是两个口径，前者才是"用了多少流量"，
    # 前端「流量」渠道的纵轴 / KPI 都取它（表里没这一列时 _sum_expr 返回 0）
    data["traffic_usage"] = round(_num(data.get("traffic_usage")), 4)
    data["avg_duration"] = int(round(_num(data.get("avg_duration"))))
    data["party_count"] = int(data.get("party_count") or 0)
    data["active_days"] = int(data.get("active_days") or 0)
    return data


def _query_stats(db_path: str, type_name: str, params: dict[str, Any]) -> dict[str, Any]:
    """统计分析：按时间粒度分组汇总。

    粒度 granularity：year / quarter / month / week / day / hour / weekday（默认 month）。

    与 trend 的区别：每桶附带去重联系人数、活跃天数、平均时长，并额外返回
    「合计」（单独聚合一次，避免把各桶的去重数累加导致重复计数）与「每桶均值」。

    时间范围完全由通用过滤参数决定，因此三种常见统计都能直接表达：
        全部数据按年 / 按年月        → 不填范围 + granularity=year / month
        指定年按月、指定年月按日      → year=2026 / month=2026-09 + granularity=month / day
        指定号码 / 姓名按年 / 月 / 日 → 叠加 party_numbers=… / party_names=… + 上述粒度
    """
    params = _normalize_period(dict(params))
    tbl = get_attr_table_name(type_name)
    conn = _connect(db_path)
    try:
        cols = _load_columns(conn, tbl, type_name)
        if "time" not in cols:
            raise ValueError("通讯数据表缺少 time 列，无法做分组统计")

        granularity = _resolve_granularity(params, "month")
        bucket_expr = _BUCKET_EXPR[granularity]

        where, args = _collect_filters(params, cols)
        where = _with_time_not_empty(where, cols)
        where_sql = _where_sql(where)
        limit = _get_int(params, "limit", 1000, 1)

        party_expr = 'COUNT(DISTINCT "party_number")' if "party_number" in cols else "0"
        day_expr = 'COUNT(DISTINCT SUBSTR(time, 1, 10))'

        # 排序：默认按时间桶升/降序；sort=value 时按 by 指定的指标排序
        sort = (params.get("sort") or "time").strip().lower()
        order_raw = (params.get("order") or "").strip().lower()
        if order_raw in ("desc", "1", "true"):
            order = "DESC"
        elif order_raw in ("asc", "0", "false"):
            order = "ASC"
        else:
            # sort=value 用于取 Top N → 默认降序；sort=time 用于时间线 → 默认升序
            order = "DESC" if sort == "value" else "ASC"
        by = (params.get("by") or "count").strip().lower()
        order_expr = _metric_expr(by, cols) if sort == "value" else "bucket"

        rows = conn.execute(
            f'SELECT {bucket_expr} AS bucket, COUNT(*) AS count, '
            f'{_sum_expr("duration", cols)} AS duration, {_sum_expr("cost", cols)} AS cost, '
            f'{_sum_expr("traffic_usage", cols)} AS traffic_usage, '
            f'{_avg_expr("duration", cols)} AS avg_duration, '
            f'{party_expr} AS party_count, {day_expr} AS active_days '
            f'FROM "{tbl}"{where_sql} '
            f'GROUP BY bucket ORDER BY {order_expr} {order}, bucket ASC LIMIT ?',
            [*args, limit],
        ).fetchall()
        result = [_stats_row(row) for row in rows]

        # 补全空缺时间桶（fill=1 且能推导出时间范围时）
        start, end, label = _resolve_time_range(params)
        if _get_int(params, "fill", 0, 0, 1) and granularity in _FILLABLE_BUCKETS and start and end:
            result = [_stats_row(row) for row in _fill_buckets(result, granularity, start, end)]

        # 合计：单独聚合一次（去重指标不能按桶累加）
        first_expr, last_expr = _time_bounds_expr(cols)
        total_row = conn.execute(
            f'SELECT COUNT(*) AS count, {_sum_expr("duration", cols)} AS duration, '
            f'{_sum_expr("cost", cols)} AS cost, {_sum_expr("traffic_usage", cols)} AS traffic_usage, '
            f'{party_expr} AS party_count, '
            f'{day_expr} AS active_days, {first_expr} AS first_time, {last_expr} AS last_time '
            f'FROM "{tbl}"{where_sql}',
            args,
        ).fetchone()
        total = _stats_row(total_row) if total_row else {}

        out: dict[str, Any] = {
            "granularity": granularity,
            "range": label,
            "count": len(result),
            "rows": result,
        }
        if _get_int(params, "with_total", 1, 0, 1):
            out["total"] = total
        if _get_int(params, "with_avg", 0, 0, 1) and result:
            buckets = len(result)
            out["avg_per_bucket"] = {
                "count": round(sum(i["count"] for i in result) / buckets, 2),
                "duration": round(sum(i["duration"] for i in result) / buckets, 1),
                "cost": round(sum(i["cost"] for i in result) / buckets, 4),
            }
        return out
    finally:
        conn.close()


def _query_summary(db_path: str, type_name: str, params: dict[str, Any]) -> dict[str, Any]:
    """汇总统计。"""
    tbl = get_attr_table_name(type_name)
    conn = _connect(db_path)
    try:
        cols = _load_columns(conn, tbl, type_name)
        where, args = _collect_filters(params, cols)
        where = _with_time_not_empty(where, cols)
        where_sql = _where_sql(where)

        party_expr = 'COUNT(DISTINCT "party_number")' if "party_number" in cols else "0"
        my_expr = 'COUNT(DISTINCT "my_number")' if "my_number" in cols else "0"
        first_expr, last_expr = _time_bounds_expr(cols)
        days_expr = 'COUNT(DISTINCT SUBSTR(time, 1, 10))' if "time" in cols else "0"
        row = conn.execute(
            f'SELECT COUNT(*) AS total, {first_expr} AS first_time, {last_expr} AS last_time, '
            f'{_sum_expr("duration", cols)} AS duration, {_sum_expr("cost", cols)} AS cost, '
            f'{_sum_expr("traffic_usage", cols)} AS traffic_usage, '
            f'{party_expr} AS party_count, {my_expr} AS my_number_count, {days_expr} AS active_days '
            f'FROM "{tbl}"{where_sql}',
            args,
        ).fetchone()
        data = _shape_agg_row(row) if row else {}
        data["total"] = int(data.get("total") or 0)
        data["party_count"] = int(data.get("party_count") or 0)
        data["my_number_count"] = int(data.get("my_number_count") or 0)
        data["active_days"] = int(data.get("active_days") or 0)
        data["duration"] = _num(data.get("duration"))
        data["cost"] = round(_num(data.get("cost")), 4)
        data["traffic_usage"] = round(_num(data.get("traffic_usage")), 4)

        for key, column in (
            ("by_channel", "channel"),
            ("by_msg_type", "msg_type"),
            ("by_call_type", "call_type"),
        ):
            if column not in cols:
                continue
            dist_where = where + [f'"{column}" IS NOT NULL', f'"{column}" <> \'\'']
            dist_rows = conn.execute(
                f'SELECT "{column}" AS key, COUNT(*) AS count, '
                f'{_sum_expr("duration", cols)} AS duration, {_sum_expr("cost", cols)} AS cost '
                f'FROM "{tbl}"{_where_sql(dist_where)} '
                f'GROUP BY "{column}" ORDER BY count DESC',
                args,
            ).fetchall()
            data[key] = [_shape_agg_row(item) for item in dist_rows]

        _, _, label = _resolve_time_range(params)
        data["range"] = label
        return {"data": data}
    finally:
        conn.close()


def _query_parties(db_path: str, type_name: str, params: dict[str, Any]) -> dict[str, Any]:
    """联系人清单（按对方号码聚合）。"""
    tbl = get_attr_table_name(type_name)
    conn = _connect(db_path)
    try:
        cols = _load_columns(conn, tbl, type_name)
        if "party_number" not in cols:
            raise ValueError("通讯数据表缺少 party_number 列，无法聚合联系人")
        where, args = _collect_filters(params, cols)
        where = _with_time_not_empty(where, cols)
        where = where + ['"party_number" IS NOT NULL', '"party_number" <> \'\'']
        limit = _get_int(params, "limit", 100, 1)
        offset = _get_int(params, "offset", 0, 0)
        by = (params.get("by") or "count").strip().lower()
        if by == "duration" and "duration" in cols:
            order_by = "duration DESC"
        elif by == "cost" and "cost" in cols:
            order_by = "cost DESC"
        else:
            order_by = "count DESC"
        name_expr = 'MAX("party_name")' if "party_name" in cols else "''"
        first_expr, last_expr = _time_bounds_expr(cols)

        rows = conn.execute(
            f'SELECT "party_number" AS party_number, {name_expr} AS party_name, '
            f'COUNT(*) AS count, {_sum_expr("duration", cols)} AS duration, '
            f'{_sum_expr("cost", cols)} AS cost, {first_expr} AS first_time, {last_expr} AS last_time '
            f'FROM "{tbl}"{_where_sql(where)} '
            f'GROUP BY "party_number" ORDER BY {order_by} LIMIT ? OFFSET ?',
            [*args, limit, offset],
        ).fetchall()
        return {"count": len(rows), "rows": [dict(row) for row in rows]}
    finally:
        conn.close()


def _query_places(db_path: str, type_name: str, params: dict[str, Any]) -> dict[str, Any]:
    """地点清单（按通讯地点聚合）。"""
    tbl = get_attr_table_name(type_name)
    conn = _connect(db_path)
    try:
        cols = _load_columns(conn, tbl, type_name)
        if "location" not in cols:
            raise ValueError("通讯数据表缺少 location 列，无法聚合地点")
        where, args = _collect_filters(params, cols)
        where = _with_time_not_empty(where, cols)
        where = where + ['"location" IS NOT NULL', '"location" <> \'\'']
        limit = _get_int(params, "limit", 100, 1)
        offset = _get_int(params, "offset", 0, 0)
        party_expr = 'COUNT(DISTINCT "party_number")' if "party_number" in cols else "0"
        first_expr, last_expr = _time_bounds_expr(cols)

        rows = conn.execute(
            f'SELECT "location" AS location, COUNT(*) AS count, '
            f'{_sum_expr("duration", cols)} AS duration, {_sum_expr("cost", cols)} AS cost, '
            f'{party_expr} AS party_count, {first_expr} AS first_time, {last_expr} AS last_time '
            f'FROM "{tbl}"{_where_sql(where)} '
            f'GROUP BY "location" ORDER BY count DESC LIMIT ? OFFSET ?',
            [*args, limit, offset],
        ).fetchall()
        return {"count": len(rows), "rows": [dict(row) for row in rows]}
    finally:
        conn.close()


# 通话方向判据 —— **必须与前端 `_commCallDirArrow` 完全一致**：
#   接听类 → 线从「对方地点」流向「我的地点」；呼叫类 → 我 → 对方；
#   其它（未接 / 未知）**不编造方向**，那些行只进地点次数、不进流向。
#   判在 SQL 里（几万行也只是一次分组），中文可直接 LIKE。
#   值取自 `msg_type`：据 comm_presets.py，通话的"呼叫 / 接听"方向是放在 `msg_type` 的，
#   源字段 `call_type`（国内通话 / 漫游…）是另一回事
_DIR_CASE_SQL = (
    'CASE WHEN ("msg_type" LIKE \'%接听%\' OR "msg_type" LIKE \'%被叫%\' '
    'OR "msg_type" LIKE \'%呼入%\' OR "msg_type" LIKE \'%来电%\') THEN \'in\' '
    'WHEN ("msg_type" LIKE \'%呼叫%\' OR "msg_type" LIKE \'%主叫%\' '
    'OR "msg_type" LIKE \'%呼出%\' OR "msg_type" LIKE \'%去电%\') THEN \'out\' '
    "ELSE '' END"
)

# `"经度,纬度"`（也容忍空格分隔）
_COORD_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*[,\s]\s*(-?\d+(?:\.\d+)?)\s*$")


def _parse_coord_text(value: Any) -> list[float] | None:
    """解析坐标：`"108.948,34.2632"` 或 `[lng, lat]`；解析不出返回 None。

    与前端 `_commCoord` 同一套规则 —— 两边判据必须一致，
    否则"这条记录到底有没有坐标"会各说各话（前端画不上、后端却说有）。
    """
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        try:
            return [float(value[0]), float(value[1])]
        except (TypeError, ValueError):
            return None
    m = _COORD_RE.match("" if value is None else str(value))
    if not m:
        return None
    return [float(m.group(1)), float(m.group(2))]


def _query_geoflows(db_path: str, type_name: str, params: dict[str, Any]) -> dict[str, Any]:
    """地点坐标流向：从「我的地点」到「对方地点」（接听类反过来），给迁徙图直接用。

    **输出与前端 `_commGeoGraph()` 同形** —— 前端拿到就能丢进 `_commMigrationOption`，
    不必再自己聚合一遍：
        cities   地点节点 `[{name, coord, count, mine, other, duration, cost, traffic_usage}]`
        flows    有向边  `[{from, to, fromCoord, toCoord, dir, count, seconds, duration,
                          cost, traffic_usage, first_time, last_time}]`
        missing  有地名却缺坐标的记录数（用于提示"部分记录没上图"）

    要点：
        · **方向**判据与前端一致（`msg_type` 里的接听类 / 呼叫类）；未接等**不编造方向** ——
          那些行只计入 `cities` 的次数，不进 `flows`（与前端 `touch()` 的行为一致）
        · **同城**（两侧同名）不入 `flows`，只计次数
        · `resolve=1`（默认）时，坐标为空的地点用本地城市坐标表**现算**
          （表里缺坐标的行很多，"回填"是另一件事，查询侧能兜就兜），
          `resolved` 回报有多少个地点是靠现算补上的
        · 聚合全部在 SQL 里完成，不把明细拉回 Python

    参数：通用过滤全部可用（`channels` / `start` / `end` / `party_names` / `places` …）
          `md`           只要这个「月日」的记录（`MM-DD`）—— 供「历史今日」那类视图用
          `min_count`    流向的最少次数（默认 1；调大可直接滤掉只通了一次的噪音）
          `by`           `count`（默认）/ `duration` / `cost` / `traffic_usage`
          `order`        `desc`（默认）/ `asc`
          `limit`        流向数上限（默认 200，0 = 不限）
          `nodes_limit`  地点数上限（默认 0 = 不限）
          `resolve`      坐标为空时是否用本地坐标表现算（默认 1）
    """
    tbl = get_attr_table_name(type_name)
    conn = _connect(db_path)
    try:
        cols = _load_columns(conn, tbl, type_name)
        if "location" not in cols and "party_place" not in cols:
            raise ValueError("通讯数据表既没有 location 也没有 party_place 列，无法算地点流向")

        where, args = _collect_filters(params, cols)
        where = _with_time_not_empty(where, cols)
        # **`md`：只要这个「月日」的记录**（`MM-DD`）—— 服务第三页的「历史今日」地图：
        #   那个视图看的是"历年同一天"，而通用时间参数只能给连续区间，表达不了它。
        #   `time` 是 `YYYY-MM-DD HH:MM:SS`，`SUBSTR(time, 6, 5)` 正好是 `MM-DD`。
        #   合法性先判一道（不合法就当没传，不至于让整条查询变空）
        md = str(params.get("md") or "").strip()
        if re.fullmatch(r"\d{2}-\d{2}", md) and "time" in cols:
            where = where + ["SUBSTR(time, 6, 5) = ?"]
            args = [*args, md]
        # 两侧地名都为空的行没有流向可言，直接排除（也在 SQL 里省掉一批）
        names = [f'"{c}"' for c in ("location", "party_place") if c in cols]
        if names:
            where = where + ["(" + " OR ".join(f"{n} IS NOT NULL AND {n} <> ''" for n in names) + ")"]

        mine_name = '"location"' if "location" in cols else "''"
        other_name = '"party_place"' if "party_place" in cols else "''"
        mine_coord_expr = '"location_coordinate"' if "location_coordinate" in cols else "''"
        other_coord_expr = '"party_coordinate"' if "party_coordinate" in cols else "''"
        dir_expr = _DIR_CASE_SQL if "msg_type" in cols else "''"
        first_expr, last_expr = _time_bounds_expr(cols)

        rows = conn.execute(
            f'SELECT {mine_name} AS mine_name, {other_name} AS other_name, '
            f'{mine_coord_expr} AS mine_coord, {other_coord_expr} AS other_coord, '
            f'{dir_expr} AS dir, COUNT(*) AS count, '
            f'{_sum_expr("duration", cols)} AS duration, {_sum_expr("cost", cols)} AS cost, '
            f'{_sum_expr("traffic_usage", cols)} AS traffic_usage, '
            f'{first_expr} AS first_time, {last_expr} AS last_time '
            f'FROM "{tbl}"{_where_sql(where)} '
            f'GROUP BY {mine_name}, {other_name}, {mine_coord_expr}, {other_coord_expr}, dir',
            args,
        ).fetchall()
    finally:
        conn.close()

    need_resolve = _get_int(params, "resolve", 1, 0, 1) == 1
    if need_resolve:
        try:
            from .city_geo import coordinate_of  # 延迟导入：模块级依赖越少越好
        except Exception:  # pragma: no cover - 坐标表缺失时不影响主流程
            coordinate_of = None
    else:
        coordinate_of = None

    resolved_names: set[str] = set()
    # 坐标缓存：同一地名在几百行里反复出现，别每次去查表
    coord_cache: dict[str, list[float] | None] = {}

    def coord_of(name: str, raw: Any) -> list[float] | None:
        parsed = _parse_coord_text(raw)
        if parsed:
            return parsed
        if not name or coordinate_of is None:
            return None
        if name not in coord_cache:
            try:
                coord_cache[name] = coordinate_of(name)
            except Exception:  # pragma: no cover
                coord_cache[name] = None
        hit = coord_cache[name]
        if hit:
            resolved_names.add(name)
        return hit

    cities: dict[str, dict[str, Any]] = {}
    flows: dict[str, dict[str, Any]] = {}
    missing = 0
    total_records = 0

    def touch_city(name: str, coord: list[float] | None, side: str,
                   count: int, duration: int, cost: float, traffic: float) -> None:
        """累计一个地点（指标**一起进来**）。

        指标必须在这里加，不能在调用处另开一轮：**同城通话**（`mine == other`）会以
        `mine` / `other` 两个身份各调一次，这里各加一份正好是想要的口径
        （"作为我的地点 1 次 + 作为对方地点 1 次"）—— 分开加会变成两倍。
        """
        if not name:
            return
        city = cities.get(name)
        if city is None:
            city = {"name": name, "coord": None, "count": 0, "mine": 0, "other": 0,
                    "duration": 0, "cost": 0.0, "traffic_usage": 0.0}
            cities[name] = city
        if city["coord"] is None and coord:
            city["coord"] = coord
        # `side` 加的是**行数**（`count`）而不是 1：SQL 已经分好组，一个组可能代表好几行
        #   （实测 `西安→北京` 被 dir 拆成 in / out / '' 三组）。前端是**逐行**遍历这些记录，
        #   那边的 `+= 1` 正等价于这里的 `+= count` —— 写成 `+= 1` 会漏掉同一组里的重复行
        city[side] += count
        city["duration"] += duration
        city["cost"] = round(city["cost"] + cost, 4)
        city["traffic_usage"] = round(city["traffic_usage"] + traffic, 4)

    for row in rows:
        item = dict(row)
        mine = str(item.get("mine_name") or "").strip()
        other = str(item.get("other_name") or "").strip()
        mine_coord = coord_of(mine, item.get("mine_coord"))
        other_coord = coord_of(other, item.get("other_coord"))
        count = int(item.get("count") or 0)
        duration = int(_num(item.get("duration")))
        cost = round(_num(item.get("cost")), 4)
        traffic = round(_num(item.get("traffic_usage")), 4)
        total_records += count

        # `missing` 与前端同一个口径：**只要该侧有地名却没坐标**就算一条
        if (mine and not mine_coord) or (other and not other_coord):
            missing += count

        touch_city(mine, mine_coord, "mine", count, duration, cost, traffic)
        touch_city(other, other_coord, "other", count, duration, cost, traffic)

        dir_value = str(item.get("dir") or "")
        if not mine or not other or mine == other or not dir_value:
            continue                      # 同名（同城）/ 无方向：只计次数，不成边
        if not mine_coord or not other_coord:
            continue                      # 端点没坐标，画不出线
        if mine_coord == other_coord:
            continue                      # 坐标重合 = 零长度线段（前端也这样滤掉）

        reverse = dir_value == "in"
        frm, to = (other, mine) if reverse else (mine, other)
        entry = flows.get(f"{dir_value}|{frm}→{to}")
        if entry is None:
            entry = {
                "from": frm, "to": to,
                "fromCoord": (other_coord if reverse else mine_coord),
                "toCoord": (mine_coord if reverse else other_coord),
                "dir": dir_value,
                "count": 0,
                # `seconds` 与前端同名字段对齐（前端 `_commGeoGraph` 用的就是它）；
                #   `duration` 一起给，方便直接照 SQL 列名取用
                "seconds": 0, "duration": 0, "cost": 0.0, "traffic_usage": 0.0,
                "first_time": str(item.get("first_time") or ""),
                "last_time": str(item.get("last_time") or ""),
            }
            flows[f"{dir_value}|{frm}→{to}"] = entry
        entry["count"] += count
        entry["seconds"] += duration
        entry["duration"] += duration
        entry["cost"] = round(entry["cost"] + cost, 4)
        entry["traffic_usage"] = round(entry["traffic_usage"] + traffic, 4)
        ft = str(item.get("first_time") or "")
        lt = str(item.get("last_time") or "")
        if ft and (not entry["first_time"] or ft < entry["first_time"]):
            entry["first_time"] = ft
        if lt > entry["last_time"]:
            entry["last_time"] = lt

    min_count = _get_int(params, "min_count", 1, 1)
    by = (params.get("by") or "count").strip().lower()
    if by not in ("count", "duration", "cost", "traffic_usage"):
        by = "count"
    desc = (params.get("order") or "desc").strip().lower() not in ("asc", "0", "false")
    limit = _get_int(params, "limit", 200, None)
    if limit <= 0:
        limit = -1                        # 0 = 不限

    flow_list = [f for f in flows.values() if f["count"] >= min_count]
    # 排序键：`seconds` 就是 `duration`，按 `duration` 给的值也要能用
    sort_key = "seconds" if by == "duration" else by
    flow_list.sort(key=lambda f: (f.get(sort_key) or 0, f["count"]), reverse=desc)
    flow_list = flow_list if limit < 0 else flow_list[:limit]

    nodes_limit = _get_int(params, "nodes_limit", 0, 0)
    # 地点：`count = mine + other`，与前端 `_commGeoGraph` 的收尾完全一致；
    #   **没有坐标的地点直接丢掉** —— 地图上根本画不出来，留着只会让"多少个地点"虚高
    #   （前端也是 `filter(c => !!c.coord)` 后再排序）
    city_list = [c for c in cities.values() if c["coord"]]
    for c in city_list:
        c["count"] = c["mine"] + c["other"]
    # 降序；同数量按名称兜底（排序结果稳定，前端也一样）
    city_list.sort(key=lambda c: (-c["count"], c["name"]))
    if nodes_limit > 0:
        city_list = city_list[:nodes_limit]

    _, _, label = _resolve_time_range(params)
    return {
        "range": label,
        "count": len(flow_list),
        "cities": city_list,
        "flows": flow_list,
        "missing": missing,
        "resolved": len(resolved_names),
        "total_records": total_records,
    }


def _query_heatmap(db_path: str, type_name: str, params: dict[str, Any]) -> dict[str, Any]:
    """星期 × 小时 分布热力数据。"""
    tbl = get_attr_table_name(type_name)
    conn = _connect(db_path)
    try:
        cols = _load_columns(conn, tbl, type_name)
        if "time" not in cols:
            raise ValueError("通讯数据表缺少 time 列，无法做时段分布统计")
        where, args = _collect_filters(params, cols)
        where = _with_time_not_empty(where, cols)
        rows = conn.execute(
            f"SELECT CAST(STRFTIME('%w', time) AS INTEGER) AS weekday, "
            f"CAST(SUBSTR(time, 12, 2) AS INTEGER) AS hour, "
            f'COUNT(*) AS count, {_sum_expr("duration", cols)} AS duration '
            f'FROM "{tbl}"{_where_sql(where)} '
            f"GROUP BY weekday, hour ORDER BY weekday ASC, hour ASC",
            args,
        ).fetchall()
        result = [_shape_agg_row(row) for row in rows]
        return {"count": len(result), "rows": result}
    finally:
        conn.close()


# =========================================================================== #
#  分析类查询：周期对比 / 单联系人档案 / 数据概览 / 单次 Top N / 数据质量        #
# =========================================================================== #
_COMPARE_PERIODS = ("day", "week", "month", "quarter", "year")

_COMPARE_ALIASES = {
    "date": "day", "d": "day", "日": "day",
    "w": "week", "周": "week",
    "m": "month", "月": "month",
    "q": "quarter", "季": "quarter", "季度": "quarter",
    "y": "year", "年": "year",
}

# 数据质量检查项：(key, 中文标签, 依赖列, 判定条件)
_QUALITY_CHECKS: tuple[tuple[str, str, str | None, str], ...] = (
    ("empty_time", "时间为空", "time", "IFNULL(time, '') = ''"),
    ("invalid_time", "时间格式异常（非 YYYY-MM-DD HH:MM:SS 或字段越界）", "time",
     "IFNULL(time, '') <> '' AND (LENGTH(time) < 16"
     " OR CAST(SUBSTR(time, 6, 2) AS INTEGER) NOT BETWEEN 1 AND 12"
     " OR CAST(SUBSTR(time, 9, 2) AS INTEGER) NOT BETWEEN 1 AND 31"
     " OR CAST(SUBSTR(time, 12, 2) AS INTEGER) NOT BETWEEN 0 AND 23"
     " OR CAST(SUBSTR(time, 15, 2) AS INTEGER) NOT BETWEEN 0 AND 59)"),
    ("empty_party", "既无对方号码也无对方姓名", None,
     "IFNULL(party_number, '') = '' AND IFNULL(party_name, '') = ''"),
    ("zero_duration", "时长为 0（非通话类记录属正常）", "duration", "IFNULL(duration, 0) = 0"),
    ("negative_duration", "时长为负", "duration", "IFNULL(duration, 0) < 0"),
    ("abnormal_duration", "时长超过 24 小时", "duration", "IFNULL(duration, 0) > 86400"),
    ("negative_cost", "金额为负", "cost", "IFNULL(cost, 0) < 0"),
    ("empty_content", "消息内容为空", "content", "IFNULL(content, '') = ''"),
    ("image_without_path", "图片消息（msg_type=image）缺图片路径", "image_path",
     "msg_type = 'image' AND IFNULL(image_path, '') = ''"),
)

# 严重问题：任一非 0 即判定数据「不完全健康」
_QUALITY_SEVERE = ("empty_time", "invalid_time", "negative_duration", "negative_cost")


def _shift_to_period(base: datetime, period: str) -> tuple[datetime, datetime, str]:
    """返回 base 所在周期的 [start, end) 与标签。"""
    if period == "day":
        start = datetime(base.year, base.month, base.day)
        return start, start + timedelta(days=1), start.strftime("%Y-%m-%d")
    if period == "week":
        start = datetime(base.year, base.month, base.day) - timedelta(days=base.weekday())
        return start, start + timedelta(days=7), start.strftime("%Y-W%W")
    if period == "quarter":
        first_month = (base.month - 1) // 3 * 3 + 1
        start = datetime(base.year, first_month, 1)
        ny, nm = _shift_month(base.year, first_month, 3)
        return start, datetime(ny, nm, 1), f"{base.year:04d}-Q{(base.month - 1) // 3 + 1}"
    if period == "year":
        return datetime(base.year, 1, 1), datetime(base.year + 1, 1, 1), f"{base.year:04d}"
    start = datetime(base.year, base.month, 1)
    ny, nm = _shift_month(base.year, base.month, 1)
    return start, datetime(ny, nm, 1), start.strftime("%Y-%m")


def _growth(current: float, previous: float | None) -> tuple[float, float | None]:
    """返回 (差值, 增长率%)；上一周期缺失或为 0 时增长率为 None。"""
    if previous is None:
        return 0.0, None
    diff = current - previous
    if not previous:
        return diff, None
    return diff, round(diff / previous * 100, 2)


def _compare_ranges(db_path: str, type_name: str, params: dict[str, Any]) -> dict[str, Any]:
    """任意两个区间的对比（`compare=range`）。

    与默认的 prev / yoy 不同，这里不依赖"当前周期"的概念 —— 直接给两段起止：
        a_start / a_end   区间 A
        b_start / b_end   区间 B
    两端的 `end` 都是**包含**语义（与全局过滤一致），所以 `a_end=2023-09-30`
    会把 09-30 一整天算进去。

    其余过滤参数（party_numbers / channels / msg_types / keyword …）对两段**同时生效** ——
    所以「某个人今年 9 月 vs 去年 9 月」直接叠加 party_numbers 即可。

    返回 a / b 各自的指标与 diff；diff 是 **a 减 b**，增长率以 b 为基准。
    """
    a_start_raw = (params.get("a_start") or "").strip()
    b_start_raw = (params.get("b_start") or "").strip()
    if not (a_start_raw and b_start_raw):
        raise ValueError("compare=range 需要 a_start 与 b_start（两个区间的起点必填）")

    tbl = get_attr_table_name(type_name)
    conn = _connect(db_path)
    try:
        cols = _load_columns(conn, tbl, type_name)
        if "time" not in cols:
            raise ValueError("通讯数据表缺少 time 列，无法做区间对比")

        # 区间参数不能参与通用过滤，否则会被当成"当前范围"再叠一层
        p = dict(params)
        for key in ("a_start", "a_end", "b_start", "b_end", "period", "compare",
                    "date", "month", "year", "start", "end"):
            p.pop(key, None)
        where, args = _collect_filters(p, cols)
        where = _with_time_not_empty(where, cols)

        party_expr = 'COUNT(DISTINCT "party_number")' if "party_number" in cols else "0"
        day_expr = 'COUNT(DISTINCT SUBSTR(time, 1, 10))'

        def _span(start_raw: str, end_raw: str) -> tuple[str, str, str]:
            """区间 → (start, end_exclusive, label)；复用全局那段，含 end 的包含语义处理。"""
            probe = dict(p)
            probe["start"] = start_raw
            if end_raw:
                probe["end"] = end_raw
            s, e, _ = _resolve_time_range(probe)
            if not s:
                raise ValueError(f"无法解析区间起点：{start_raw}")
            # 不复用 `_resolve_time_range` 的 label —— 它对 start/end 组合只会给 `custom`，
            #   这里直接写成能读的区间（控件上两段对比要看得出是哪两段）
            label = f"{start_raw[:10]} ~ {end_raw[:10]}" if end_raw else f"{start_raw[:10]} 起"
            return s, e, label

        def _agg(start_s: str, end_s: str, label: str) -> dict[str, Any]:
            cond = list(where)
            vals = list(args)
            cond.append("time >= ?")
            vals.append(start_s)
            if end_s:
                cond.append("time < ?")
                vals.append(end_s)
            row = conn.execute(
                f'SELECT COUNT(*) AS count, {_sum_expr("duration", cols)} AS duration, '
                f'{_sum_expr("cost", cols)} AS cost, {_sum_expr("traffic_usage", cols)} AS traffic_usage, '
                f'{party_expr} AS party_count, {day_expr} AS active_days '
                f'FROM "{tbl}"{_where_sql(cond)}',
                vals,
            ).fetchone()
            data = _stats_row(row) if row else {}
            data["label"] = label
            data["start"] = start_s
            data["end"] = end_s
            return data

        a = _agg(*_span(a_start_raw, (params.get("a_end") or "").strip()))
        b = _agg(*_span(b_start_raw, (params.get("b_end") or "").strip()))

        diff: dict[str, Any] = {}
        for metric in ("count", "duration", "cost", "traffic_usage", "party_count", "active_days"):
            delta, pct = _growth(_num(a.get(metric)), _num(b.get(metric)))
            diff[metric] = int(round(delta)) if metric in ("count", "party_count", "active_days") else round(delta, 4)
            diff[metric + "_pct"] = pct

        return {
            "mode": "range",
            "a": a,
            "b": b,
            "diff": diff,
            "note": "diff = a - b；增长率以 b 为基准（b 为 0 时 pct 为 null）",
        }
    finally:
        conn.close()


def _query_compare(db_path: str, type_name: str, params: dict[str, Any]) -> dict[str, Any]:
    """周期对比：当前周期 vs 上一周期（环比）vs 去年同期（同比）。

    period   对比周期：day / week / month（默认）/ quarter / year
    date     基准日，默认今天（决定「当前周期」）
    compare  对比项：prev（环比）/ yoy（同比）/ both（默认）
             / **range（任意两区间）** —— 这时改用 a_start/a_end 与 b_start/b_end，
             不走"当前周期"那套（见 _compare_ranges）
    """
    period = (params.get("period") or "month").strip().lower()
    period = _COMPARE_ALIASES.get(period, period)
    if period not in _COMPARE_PERIODS:
        raise ValueError("period 只能是 " + " / ".join(_COMPARE_PERIODS))

    compare = (params.get("compare") or "both").strip().lower()
    # 任意两区间对比（compare=range）走独立实现，复用不到「当前 vs 上一周期」那套
    if compare == "range":
        return _compare_ranges(db_path, type_name, params)
    if compare not in ("prev", "yoy", "both"):
        raise ValueError("compare 只能是 prev / yoy / both / range")

    base = _parse_dt((params.get("date") or "").strip()) or datetime.now()

    p = dict(params)
    for key in ("date", "month", "year", "start", "end", "period", "compare"):
        p.pop(key, None)

    tbl = get_attr_table_name(type_name)
    conn = _connect(db_path)
    try:
        cols = _load_columns(conn, tbl, type_name)
        if "time" not in cols:
            raise ValueError("通讯数据表缺少 time 列，无法做周期对比")
        where, args = _collect_filters(p, cols)
        where = _with_time_not_empty(where, cols)
        base_where_sql = _where_sql(where)
        party_expr = 'COUNT(DISTINCT "party_number")' if "party_number" in cols else "0"

        def _agg(start_dt: datetime, end_dt: datetime, label: str) -> dict[str, Any]:
            row = conn.execute(
                f'SELECT COUNT(*) AS count, {_sum_expr("duration", cols)} AS duration, '
                f'{_sum_expr("cost", cols)} AS cost, {party_expr} AS party_count, '
                f'COUNT(DISTINCT SUBSTR(time, 1, 10)) AS active_days '
                f'FROM "{tbl}"{_and_sql(base_where_sql, "time >= ? AND time < ?")}',
                [*args, start_dt.strftime(_TIME_FMT), end_dt.strftime(_TIME_FMT)],
            ).fetchone()
            data = _stats_row(row) if row else {}
            data["label"] = label
            data["start"] = start_dt.strftime(_TIME_FMT)
            data["end"] = end_dt.strftime(_TIME_FMT)
            return data

        cur_start, cur_end, cur_label = _shift_to_period(base, period)
        current = _agg(cur_start, cur_end, cur_label)
        result: dict[str, Any] = {
            "period": period,
            "base_date": base.strftime("%Y-%m-%d"),
            "compare": compare,
            "current": current,
        }
        metrics = ("count", "duration", "cost", "party_count", "active_days")
        int_metrics = ("count", "party_count", "active_days")
        diffs: dict[str, Any] = {}

        for key in ("prev", "yoy"):
            if compare not in ("both", key):
                result["previous" if key == "prev" else "yoy"] = None
                continue
            if key == "prev":
                span_start, span_end, span_label = _shift_to_period(cur_start - timedelta(days=1), period)
            else:
                if period == "year":
                    # 年对年没有「同比」，同比项对 year 周期不适用（环比即上一年）
                    result["yoy"] = None
                    continue
                try:
                    ref = base.replace(year=base.year - 1)
                except ValueError:      # 2 月 29 日
                    ref = base - timedelta(days=365)
                span_start, span_end, span_label = _shift_to_period(ref, period)
            agg = _agg(span_start, span_end, span_label)
            result["previous" if key == "prev" else "yoy"] = agg
            diff: dict[str, Any] = {}
            for metric in metrics:
                delta, pct = _growth(_num(current.get(metric)), _num(agg.get(metric)))
                diff[metric] = int(delta) if metric in int_metrics else round(delta, 4)
                diff[metric + "_pct"] = pct
            diffs[key] = diff
        if diffs:
            result["diff"] = diffs
        return result
    finally:
        conn.close()


def _query_contact(db_path: str, type_name: str, params: dict[str, Any]) -> dict[str, Any]:
    """单联系人档案：以 party_number 或 party_name 为身份，汇总全量画像。

    返回总量、首末通讯、多久没联系、平均间隔、各维度分布、最近明细、联系最多的日子。
    其余过滤参数照常生效（可用 date / month / year / start / end 收窄统计范围）。
    """
    number = (params.get("party_number") or "").strip()
    names = _split_multi(params.get("party_names"))
    name = (params.get("party_name") or "").strip() or (names[0] if names else "")
    if not number and not name:
        raise ValueError("contact 需要提供 party_number 或 party_name")

    p = dict(params)
    for key in ("party_number", "party_name", "party_names"):
        p.pop(key, None)
    if number:
        p["party_numbers"] = number
        match_field, match_value = "party_number", number
    else:
        p["party_names"] = name
        match_field, match_value = "party_name", name

    tbl = get_attr_table_name(type_name)
    conn = _connect(db_path)
    try:
        cols = _load_columns(conn, tbl, type_name)
        where, args = _collect_filters(p, cols)
        where = _with_time_not_empty(where, cols)
        where_sql = _where_sql(where)

        first_expr, last_expr = _time_bounds_expr(cols)
        row = conn.execute(
            f'SELECT COUNT(*) AS count, {_sum_expr("duration", cols)} AS duration, '
            f'{_sum_expr("cost", cols)} AS cost, {_avg_expr("duration", cols)} AS avg_duration, '
            f'{first_expr} AS first_time, {last_expr} AS last_time, '
            f'COUNT(DISTINCT SUBSTR(time, 1, 10)) AS active_days '
            f'FROM "{tbl}"{where_sql}',
            args,
        ).fetchone()
        total = _stats_row(row) if row else {}
        total["avg_duration"] = int(round(_num(total.get("avg_duration"))))

        first_dt = _parse_dt(str(total.get("first_time") or ""))
        last_dt = _parse_dt(str(total.get("last_time") or ""))
        now = datetime.now()
        days_since_last = (now.date() - last_dt.date()).days if last_dt else None
        span_days = (last_dt.date() - first_dt.date()).days if (first_dt and last_dt) else 0
        active_days = int(total.get("active_days") or 0)
        avg_interval = round(span_days / (active_days - 1), 2) if active_days > 1 else None

        def _dist(expr: str, extra: str | None = None, limit: int = 50,
                  order: str = "count DESC") -> list[dict[str, Any]]:
            sql_where = _and_sql(where_sql, extra) if extra else where_sql
            rows = conn.execute(
                f'SELECT {expr} AS key, COUNT(*) AS count, {_sum_expr("duration", cols)} AS duration '
                f'FROM "{tbl}"{sql_where} GROUP BY key ORDER BY {order} LIMIT ?',
                [*args, limit],
            ).fetchall()
            return [_stats_row(r) for r in rows]

        recent = [dict(r) for r in conn.execute(
            f'SELECT {_select_expr(cols, _parse_fields(params))} FROM "{tbl}"{where_sql} '
            f'ORDER BY time DESC LIMIT ?',
            [*args, _get_int(params, "recent", 20, 1, 200)],
        ).fetchall()]
        _trim_content(recent, _get_int(params, "content_len", 80, 0))

        top_days = [{'date': r["date"], 'count': int(r["count"] or 0), 'duration': _num(r["duration"])}
                    for r in conn.execute(
                        f'SELECT SUBSTR(time, 1, 10) AS date, COUNT(*) AS count, '
                        f'{_sum_expr("duration", cols)} AS duration '
                        f'FROM "{tbl}"{where_sql} GROUP BY date ORDER BY count DESC LIMIT ?',
                        [*args, _get_int(params, "top_days", 10, 1, 100)],
                    ).fetchall()]

        return {
            "target": {"match_field": match_field, "match_value": match_value},
            "total": total,
            "days_since_last": days_since_last,
            "avg_interval_days": avg_interval,
            "span_days": span_days,
            "by_hour": _dist("SUBSTR(time, 12, 2)", limit=24),
            "by_weekday": _dist("STRFTIME('%w', time)", limit=7),
            "by_month": _dist("SUBSTR(time, 1, 7)", limit=_get_int(params, "months", 24, 1, 120),
                              order="key DESC"),
            "by_channel": _dist('"channel"', extra='"channel" <> \'\'', limit=20),
            "by_msg_type": _dist('"msg_type"', extra='"msg_type" <> \'\'', limit=20),
            "by_call_type": _dist('"call_type"', extra='"call_type" <> \'\'', limit=20),
            "top_days": top_days,
            "recent": recent,
        }
    finally:
        conn.close()


def _query_meta(db_path: str, type_name: str, params: dict[str, Any]) -> dict[str, Any]:
    """数据概览：表结构、总量、时间跨度、维度取值分布（供前端下拉与健康检查）。

    不受通信时间过滤影响（不排除 time 为空的行），只应用调用方传入的过滤条件。
    """
    tbl = get_attr_table_name(type_name)
    conn = _connect(db_path)
    try:
        cols = _load_columns(conn, tbl, type_name)
        where, args = _collect_filters(params, cols)
        where_sql = _where_sql(where)

        first_expr, last_expr = _time_bounds_expr(cols)
        party_expr = 'COUNT(DISTINCT "party_number")' if "party_number" in cols else "0"
        number_expr = 'COUNT(DISTINCT "my_number")' if "my_number" in cols else "0"
        row = conn.execute(
            f'SELECT COUNT(*) AS count, {first_expr} AS first_time, {last_expr} AS last_time, '
            f'{_sum_expr("duration", cols)} AS duration, {_sum_expr("cost", cols)} AS cost, '
            f'{party_expr} AS party_count, {number_expr} AS my_number_count, '
            f'COUNT(DISTINCT SUBSTR(time, 1, 10)) AS active_days '
            f'FROM "{tbl}"{where_sql}',
            args,
        ).fetchone()
        total = _stats_row(row) if row else {}

        def _values(column: str, limit: int = 50) -> list[dict[str, Any]]:
            if column not in cols:
                return []
            rows = conn.execute(
                f'SELECT "{column}" AS key, COUNT(*) AS count FROM "{tbl}"'
                f'{_and_sql(where_sql, f"\"{column}\" IS NOT NULL AND \"{column}\" <> \'\'")} '
                f'GROUP BY key ORDER BY count DESC LIMIT ?',
                [*args, limit],
            ).fetchall()
            return [{"key": r["key"], "count": int(r["count"] or 0)} for r in rows]

        def _buckets(expr: str, limit: int = 120) -> list[str]:
            rows = conn.execute(
                f'SELECT DISTINCT {expr} AS b FROM "{tbl}"'
                f'{_and_sql(where_sql, f"{expr} IS NOT NULL AND {expr} <> \'\'")} '
                f"ORDER BY b DESC LIMIT ?",
                [*args, limit],
            ).fetchall()
            return [str(r["b"]) for r in rows]

        now = datetime.now()
        last_dt = _parse_dt(str(total.get("last_time") or ""))
        return {
            "table": tbl,
            "type_name": type_name,
            "columns": sorted(cols),
            "total": total,
            "checked_at": now.strftime(_TIME_FMT),
            "days_since_last": (now.date() - last_dt.date()).days if last_dt else None,
            "values": {
                "channels": _values("channel"),
                "msg_types": _values("msg_type"),
                "call_types": _values("call_type"),
                "party_places": _values("party_place", 30),
                "places": _values("location", 30),
            },
            "years": _buckets("SUBSTR(time, 1, 4)", 30),
            "months": _buckets("SUBSTR(time, 1, 7)", 60),
        }
    finally:
        conn.close()


def _query_longest(db_path: str, type_name: str, params: dict[str, Any]) -> dict[str, Any]:
    """单次记录 Top N：最长通话 / 最高金额（区别于按维度聚合的 ranking）。"""
    tbl = get_attr_table_name(type_name)
    conn = _connect(db_path)
    try:
        cols = _load_columns(conn, tbl, type_name)
        by = (params.get("by") or "duration").strip().lower()
        order_col = "cost" if by == "cost" else "duration"
        if order_col not in cols:
            order_col = "duration" if "duration" in cols else ("id" if "id" in cols else "")
        if not order_col:
            raise ValueError("通讯数据表缺少可排序的数值列（duration / cost）")

        where, args = _collect_filters(params, cols)
        where = _with_time_not_empty(where, cols)
        where = where + [f'"{order_col}" > 0']
        where_sql = _where_sql(where)
        order_dir = "ASC" if (params.get("order") or "").strip().lower() in ("asc", "1", "true") else "DESC"
        limit = _get_int(params, "limit", 20, 1)
        offset = _get_int(params, "offset", 0, 0)

        total = conn.execute(f'SELECT COUNT(*) FROM "{tbl}"{where_sql}', args).fetchone()[0]
        rows = conn.execute(
            f'SELECT {_select_expr(cols, _parse_fields(params))} FROM "{tbl}"{where_sql} '
            f'ORDER BY "{order_col}" {order_dir}, time DESC LIMIT ? OFFSET ?',
            [*args, limit, offset],
        ).fetchall()
        result = [dict(r) for r in rows]
        for index, item in enumerate(result, start=offset + 1):
            item["rank"] = index
        _trim_content(result, _get_int(params, "content_len", 0, 0))
        return {
            "by": order_col,
            "order": order_dir.lower(),
            "count": len(result),
            "total": int(total or 0),
            "limit": limit,
            "offset": offset,
            "rows": result,
        }
    finally:
        conn.close()


def _query_quality(db_path: str, type_name: str, params: dict[str, Any]) -> dict[str, Any]:
    """数据质量检查：空值 / 异常值 / 疑似重复 / 时间覆盖。"""
    tbl = get_attr_table_name(type_name)
    conn = _connect(db_path)
    try:
        cols = _load_columns(conn, tbl, type_name)
        where, args = _collect_filters(params, cols)
        where_sql = _where_sql(where)
        total = int(conn.execute(f'SELECT COUNT(*) FROM "{tbl}"{where_sql}', args).fetchone()[0] or 0)

        issues: list[dict[str, Any]] = []
        for key, label, dep_col, cond in _QUALITY_CHECKS:
            if dep_col and dep_col not in cols:
                continue
            try:
                count = int(conn.execute(
                    f'SELECT COUNT(*) FROM "{tbl}"{_and_sql(where_sql, cond)}', args
                ).fetchone()[0] or 0)
            except sqlite3.OperationalError:
                continue
            issues.append({
                "key": key,
                "label": label,
                "count": count,
                "ratio": round(count / total * 100, 2) if total else 0.0,
            })

        # 疑似重复：同 时间 + 对方号码 + 内容 出现多次（多为增量同步未去重）
        duplicates = [
            {"time": r["time"], "party_number": r["party_number"], "count": int(r["count"] or 0)}
            for r in conn.execute(
                f'SELECT time, party_number, COUNT(*) AS count FROM "{tbl}"{where_sql} '
                f'GROUP BY time, party_number, IFNULL(content, \'\') HAVING COUNT(*) > 1 '
                f'ORDER BY count DESC LIMIT ?',
                [*args, _get_int(params, "dup_limit", 10, 1, 100)],
            ).fetchall()
        ]

        # 时间覆盖：有数据的天数 / 首末跨度
        first_expr, last_expr = _time_bounds_expr(cols)
        bounds = conn.execute(
            f'SELECT {first_expr} AS first_time, {last_expr} AS last_time, '
            f'COUNT(DISTINCT SUBSTR(time, 1, 10)) AS active_days '
            f'FROM "{tbl}"{_and_sql(where_sql, "time IS NOT NULL AND time <> \'\'")}',
            args,
        ).fetchone()
        first_dt = _parse_dt(str((bounds["first_time"] if bounds else "") or ""))
        last_dt = _parse_dt(str((bounds["last_time"] if bounds else "") or ""))
        active_days = int((bounds["active_days"] if bounds else 0) or 0)
        span_days = (last_dt.date() - first_dt.date()).days + 1 if (first_dt and last_dt) else 0
        coverage = {
            "first_time": first_dt.strftime(_TIME_FMT) if first_dt else "",
            "last_time": last_dt.strftime(_TIME_FMT) if last_dt else "",
            "active_days": active_days,
            "span_days": span_days,
            "coverage_pct": round(active_days / span_days * 100, 2) if span_days else 0.0,
        }

        severe = {i["key"]: i["count"] for i in issues if i["key"] in _QUALITY_SEVERE}
        return {
            "total": total,
            "ok": not any(severe.values()),
            "severe": severe,
            "issues": issues,
            "duplicates": duplicates,
            "coverage": coverage,
        }
    finally:
        conn.close()


# =========================================================================== #
#  「历史上的今日」（onthisday）                                                 #
# =========================================================================== #
# 交叉汇总可用维度（复用排行榜白名单）
_CT_DIMENSIONS = dict(_RANK_DIMENSIONS)

# onthisday 的 mode → 复用的查询实现（crosstab 单独处理）
def _query_crosstab(db_path: str, type_name: str, params: dict[str, Any]) -> dict[str, Any]:
    """交叉汇总：行维度 × 列维度 的度量矩阵。

    rows    行维度（1~3 个，逗号分隔）：msg_type / location / party_place / call_type /
            party_name / party_number / channel / my_number
    cols    列维度（0~1 个）：同上；留空则只输出各行的合计
    metric  测度：count（默认）/ duration / cost
    """
    tbl = get_attr_table_name(type_name)
    conn = _connect(db_path)
    try:
        table_cols = _load_columns(conn, tbl, type_name)
        rows_raw = _split_multi(params.get("rows")) or ["msg_type"]
        cols_raw = _split_multi(params.get("cols"))
        if len(cols_raw) > 1:
            raise ValueError("cols 最多支持 1 个维度（多个维度请放进 rows）")

        def _dim(name: str) -> str:
            col = _CT_DIMENSIONS.get(name)
            if not col:
                raise ValueError(f"不支持的分组维度 {name}，可用：" + "、".join(_CT_DIMENSIONS))
            if col not in table_cols:
                raise ValueError(f"维度 {name} 对应的列 {col} 不存在于数据表")
            return col

        row_cols = [_dim(n) for n in rows_raw[:3]]
        col_cols = [_dim(n) for n in cols_raw]

        metric = (params.get("metric") or params.get("by") or "count").strip().lower()
        metric_expr = _metric_expr(metric, table_cols)

        where, args = _collect_filters(params, table_cols)
        where = _with_time_not_empty(where, table_cols)
        for col in row_cols + col_cols:
            where = where + [f'"{col}" IS NOT NULL', f'"{col}" <> \'\'']
        where_sql = _where_sql(where)

        row_expr = " || ' | ' || ".join(f'IFNULL("{c}", \'\')' for c in row_cols)
        limit = _get_int(params, "limit", 30, 1)
        col_limit = _get_int(params, "col_limit", 20, 1, 100)
        party_expr = 'COUNT(DISTINCT "party_number")' if "party_number" in table_cols else "0"
        day_expr = 'COUNT(DISTINCT SUBSTR(time, 1, 10))'

        if not col_cols:
            rows = conn.execute(
                f'SELECT {row_expr} AS key, COUNT(*) AS count, '
                f'{_sum_expr("duration", table_cols)} AS duration, '
                f'{_sum_expr("cost", table_cols)} AS cost, '
                f'{_sum_expr("traffic_usage", table_cols)} AS traffic_usage, '
                f'{party_expr} AS party_count, {day_expr} AS active_days '
                f'FROM "{tbl}"{where_sql} GROUP BY key '
                f'ORDER BY {metric_expr} DESC, count DESC LIMIT ?',
                [*args, limit],
            ).fetchall()
            return {
                "rows_dim": rows_raw[:3],
                "cols_dim": [],
                "metric": metric,
                "count": len(rows),
                "rows": [_stats_row(r) for r in rows],
            }

        # 有列维度：先用行合计取 Top N 行，再取这些行的 (行, 列) 矩阵
        row_totals = conn.execute(
            f'SELECT {row_expr} AS key, {metric_expr} AS value, COUNT(*) AS count '
            f'FROM "{tbl}"{where_sql} GROUP BY key '
            f'ORDER BY value DESC, count DESC LIMIT ?',
            [*args, limit],
        ).fetchall()
        row_keys = [str(r["key"]) for r in row_totals]
        if not row_keys:
            return {
                "rows_dim": rows_raw[:3],
                "cols_dim": cols_raw,
                "metric": metric,
                "row_keys": [],
                "col_keys": [],
                "matrix": [],
                "row_metric": [],
                "col_metric": [],
                "grand_total": 0,
            }

        col_expr = f'IFNULL("{col_cols[0]}", \'\')'
        placeholders = ", ".join("?" * len(row_keys))
        cells = conn.execute(
            f'SELECT {row_expr} AS rk, {col_expr} AS ck, {metric_expr} AS value, COUNT(*) AS count '
            f'FROM "{tbl}"{_and_sql(where_sql, f"({row_expr}) IN ({placeholders})")} '
            f"GROUP BY rk, ck ORDER BY value DESC, count DESC",
            [*args, *row_keys],
        ).fetchall()

        cell_map: dict[tuple[str, str], float] = {}
        col_metric: dict[str, float] = {}
        for row in cells:
            rk, ck = str(row["rk"]), str(row["ck"])
            value = _num(row["value"])
            cell_map[(rk, ck)] = value
            col_metric[ck] = col_metric.get(ck, 0) + value
        col_keys = [k for k, _ in sorted(col_metric.items(), key=lambda kv: kv[1], reverse=True)][:col_limit]

        def _round(value: float) -> Any:
            return int(value) if metric == "count" else round(value, 4)

        row_metric = [_num(r["value"]) for r in row_totals]
        return {
            "rows_dim": rows_raw[:3],
            "cols_dim": cols_raw,
            "metric": metric,
            "row_keys": row_keys,
            "col_keys": col_keys,
            "matrix": [[_round(cell_map.get((rk, ck), 0)) for ck in col_keys] for rk in row_keys],
            "row_metric": [_round(v) for v in row_metric],
            "col_metric": [_round(col_metric.get(ck, 0)) for ck in col_keys],
            "grand_total": _round(sum(row_metric)),
        }
    finally:
        conn.close()


def _query_onthisday(db_path: str, type_name: str, params: dict[str, Any]) -> dict[str, Any]:
    """历史上的今日 — 委托给通用模块 onthisday.py（本入口保持 URL 兼容）。

    实现已统一到 onthisday.py（同一套代码同时服务通讯 / 设备 / 环境），
    这里只是把 `/comm?type=onthisday` 的请求转过去，并固定 source=comm。

    兼容说明：早期 mode=parties / records 现分别对应通用模块的 ranking / detail，
    由 onthisday.py 内部做别名映射；新旧 URL 均可继续使用。
    """
    from .onthisday import run_onthisday_query  # 延迟导入，避免与 onthisday 互相导入

    merged = dict(params)
    merged["source"] = "comm"
    merged.setdefault("type_name", type_name)
    alias = {"parties": "ranking", "records": "detail"}.get(
        (merged.get("mode") or "").strip().lower()
    )
    if alias:
        merged["mode"] = alias
        if alias == "ranking" and not (merged.get("dimension") or "").strip():
            merged["dimension"] = "party_number"
    return run_onthisday_query(db_path, merged)


_QUERY_DISPATCH = {
    "records": _query_records,
    "list": _query_records,
    "chat": _query_chat,
    "dates": _query_dates,
    "months": _query_months,
    "calendar": _query_calendar,
    "stale": _query_stale,
    "duration_dist": _query_duration_dist,
    "new_peers": _query_new_peers,
    "peak": _query_peak,
    "stats": _query_stats,
    "group": _query_stats,
    "compare": _query_compare,
    "contact": _query_contact,
    "meta": _query_meta,
    "longest": _query_longest,
    "quality": _query_quality,
    "crosstab": _query_crosstab,
    "onthisday": _query_onthisday,
    "anniversary": _query_onthisday,
    "ranking": _query_ranking,
    "trend": _query_trend,
    "summary": _query_summary,
    "parties": _query_parties,
    "places": _query_places,
    "geoflows": _query_geoflows,
    "flows": _query_geoflows,
    "heatmap": _query_heatmap,
}


def run_comm_query(db_path: str, type_name: str, query_type: str, params: dict[str, Any]) -> dict[str, Any]:
    """同步执行通讯查询（在 executor 线程中调用）。"""
    handler = _QUERY_DISPATCH.get(query_type)
    if handler is None:
        raise ValueError(
            f"不支持的 type：{query_type}（可用："
            + "、".join(sorted(set(_QUERY_DISPATCH)))
            + "）"
        )
    # type_name 留空时自动探测（「属性提取」里可能用了自定义类型名）
    resolved = resolve_comm_type_name(db_path, type_name)
    result = handler(db_path, resolved, params)
    if isinstance(result, dict):
        # 回显**实际生效**的类型名（可能来自自动探测），供调用方确认查的是哪张表
        result.setdefault("type_name", resolved)
        # 每个响应都带接口版本：确认服务端跑的是不是最新的那份接口
        result.setdefault("api_version", COMM_API_VERSION)
    return result


BACKFILL_VIEW_URL = "/api/ha_data_store/comm/backfill"
BACKFILL_VIEW_NAME = "api:ha_data_store:comm_backfill"


class CommBackfillView(_BaseDBView):
    """通讯记录字段回填：用本地归属地库与城市坐标表补全空字段。

    GET /api/ha_data_store/comm/backfill?type_name=<类型名>
      → {success, region:{...库状态}, coords:{...坐标表状态}, table:{...待回填统计}}

    POST /api/ha_data_store/comm/backfill
      Body: {type_name?, dry_run?, only_empty?, limit?, columns?}
        · dry_run=1    只统计不写库（预览）
        · only_empty   默认 1：只填空字段，不覆盖已有内容
        · limit>0      最多处理这么多行（便于大表分批执行）
        · columns      可选，只处理其中几列（如 ["party_name"] 表示「只按通讯录补姓名」）
      → {success, scanned, updated, filled:{...}, no_region, no_coord, no_contact,
         missed_numbers, samples}

    回填字段（见 `comm_backfill.BACKFILL_COLUMNS`）：
      party_place         <- party_number 查归属地库
      party_isp           <- party_number 查归属地库
      party_coordinate    <- party_place  查城市坐标表
      location_coordinate <- location     查城市坐标表

    **不联网**：只读取 `data/phone2region.zdb` 与 `data/city_coordinates.json`。
    """

    url = BACKFILL_VIEW_URL
    name = BACKFILL_VIEW_NAME

    async def get(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_master_switch(hass)):
            return resp
        if (resp := self._check_db_viewer_enabled(hass)):
            return resp

        type_name = (request.query.get("type_name") or "").strip()
        from . import city_geo, comm_backfill, phone_region

        # 解析库路径（用户库 config/ha_data_store -> 集成内置 data/），首次使用才加载
        await phone_region.async_prepare(hass)
        await hass.async_add_executor_job(city_geo.load_coordinates, hass, True)

        def _load() -> dict:
            return {
                "region": phone_region.db_status(),
                "coords": city_geo.status(hass),
                "table": comm_backfill.table_status(self._db_path, type_name),
            }

        try:
            data = await self._exec_in_executor(hass, _load)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception("[comm] 读取回填状态失败")
            return self.json({"success": False, "error": str(exc)}, status_code=500)
        return self.json({"success": True, **data})

    async def post(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_master_switch(hass)):
            return resp
        if (resp := self._check_db_edit_enabled(hass)):
            return resp

        try:
            body = await request.json()
        except Exception:
            return self.json({"success": False, "error": "请求体不是合法的 JSON"}, status_code=400)
        if not isinstance(body, dict):
            body = {}

        type_name = str(body.get("type_name") or "").strip()
        dry_run = bool(body.get("dry_run"))
        only_empty = body.get("only_empty")
        only_empty = True if only_empty is None else bool(only_empty)
        try:
            limit = max(0, int(body.get("limit") or 0))
        except (TypeError, ValueError):
            limit = 0

        # 可选：只处理其中几列（如「按通讯录回填姓名」只跑 party_name）
        raw_cols = body.get("columns")
        columns = None
        if isinstance(raw_cols, (list, tuple)):
            columns = [str(c).strip() for c in raw_cols if str(c).strip()] or None
        elif isinstance(raw_cols, str) and raw_cols.strip():
            columns = [c.strip() for c in raw_cols.split(",") if c.strip()] or None

        from . import comm_backfill, phone_region

        await phone_region.async_prepare(hass)
        try:
            stats = await self._exec_in_executor(
                hass, comm_backfill.backfill, self._db_path,
                type_name, only_empty, limit, dry_run, columns,
            )
        except ValueError as exc:
            return self.json({"success": False, "error": str(exc)}, status_code=400)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception("[comm] 字段回填失败")
            return self.json({"success": False, "error": str(exc)}, status_code=500)

        _LOGGER.info(
            "[comm] 字段回填%s: 表 %s 扫描 %s 行、更新 %s 行（%s）",
            "（预览）" if dry_run else "", stats.get("table"), stats.get("scanned"),
            stats.get("updated"),
            "、".join(f"{k} {v}" for k, v in (stats.get("filled") or {}).items()) or "无字段变化",
        )
        return self.json(stats)


CONTACTS_VIEW_URL = "/api/ha_data_store/comm/contacts"
CONTACTS_VIEW_NAME = "api:ha_data_store:comm_contacts"


class CommContactsView(_BaseDBView):
    """通讯录（号码 → 姓名）管理：用户自行导入，用于回填通讯记录的 `party_name`。

    GET  /api/ha_data_store/comm/contacts
      → {success, exists, total, samples:[{number,name}]}

    POST /api/ha_data_store/comm/contacts
      Body: { action: "import", text, format?, mode? }
              → {imported, inserted, updated, total, warnings}
            { action: "clear" }            → {deleted}
            { action: "delete", number }   → {deleted}

    导入格式（自动嗅探，见 `comm_contacts.parse_import`）：

    · `13800138000,张三` 每行一条（逗号 / 制表符 / 分号 / 竖线分隔；
      列序自动判断，因此手机导出的 `张三,13800138000` 也能直接粘）
    · `[{"number":"13800138000","name":"张三"}, ...]`
    · `{"13800138000":"张三", ...}`

    号码会归一化（去 `+86` / 空格 / 连字符），所以重复导入是**更新**姓名而非新增；
    `mode="replace"` 则先清空再导入（整份替换）。
    """

    url = CONTACTS_VIEW_URL
    name = CONTACTS_VIEW_NAME

    async def get(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_master_switch(hass)):
            return resp
        if (resp := self._check_db_viewer_enabled(hass)):
            return resp

        from . import comm_contacts

        try:
            data = await self._exec_in_executor(
                hass, comm_contacts.stats, self._db_path, True
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception("[comm] 读取通讯录失败")
            return self.json({"success": False, "error": str(exc)}, status_code=500)
        return self.json(data)

    async def post(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_master_switch(hass)):
            return resp
        if (resp := self._check_db_edit_enabled(hass)):
            return resp

        try:
            body = await request.json()
        except Exception:
            return self.json({"success": False, "error": "请求体不是合法的 JSON"}, status_code=400)
        if not isinstance(body, dict):
            body = {}

        action = str(body.get("action") or "import").strip().lower()
        from . import comm_contacts

        def _now() -> str:
            from datetime import datetime
            return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if action == "clear":
            try:
                deleted = await self._exec_in_executor(
                    hass, comm_contacts.clear, self._db_path
                )
            except Exception as exc:  # noqa: BLE001
                _LOGGER.exception("[comm] 清空通讯录失败")
                return self.json({"success": False, "error": str(exc)}, status_code=500)
            _LOGGER.info("[comm] 通讯录已清空，删除 %s 条", deleted)
            return self.json({"success": True, "deleted": deleted, "total": 0})

        if action == "delete":
            number = str(body.get("number") or "").strip()
            if not number:
                return self.json(
                    {"success": False, "error": "缺少 number 参数"}, status_code=400
                )
            try:
                deleted = await self._exec_in_executor(
                    hass, comm_contacts.delete_number, self._db_path, number
                )
            except Exception as exc:  # noqa: BLE001
                _LOGGER.exception("[comm] 删除通讯录条目失败")
                return self.json({"success": False, "error": str(exc)}, status_code=500)
            return self.json({"success": True, "deleted": deleted})

        # 默认动作：导入
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            return self.json(
                {"success": False, "error": "没有可导入的内容（text 为空）"}, status_code=400
            )
        fmt = str(body.get("format") or "auto").strip().lower()
        if fmt not in ("auto", "json", "csv"):
            fmt = "auto"
        mode = str(body.get("mode") or "merge").strip().lower()
        if mode not in ("merge", "replace"):
            mode = "merge"

        try:
            result = await self._exec_in_executor(
                hass, comm_contacts.import_contacts,
                self._db_path, text, fmt, mode, _now(), "import",
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception("[comm] 导入通讯录失败")
            return self.json({"success": False, "error": str(exc)}, status_code=500)

        if result.get("error"):
            return self.json({"success": False, "error": result["error"], **result},
                             status_code=400)
        _LOGGER.info(
            "[comm] 通讯录导入完成：新增 %s、更新 %s，当前共 %s 条",
            result.get("inserted"), result.get("updated"), result.get("total"),
        )
        return self.json({"success": True, "mode": mode, **result})


# =========================================================================== #
#  HTTP API                                                                   #
# =========================================================================== #
class CommApiView(_BaseDBView):
    """通讯数据查询 API。

    GET|POST /api/ha_data_store/comm?type=<类型>&...
    详见模块头部文档。
    """

    url = COMM_VIEW_URL
    name = COMM_VIEW_NAME

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
        if extra:
            for key, value in extra.items():
                if value not in (None, ""):
                    params[key] = value

        query_type = (params.get("type") or "records").strip().lower()
        hass: HomeAssistant = request.app["hass"]
        # 类型名优先级：显式参数 > 设置实体（text.ha_data_store_comm_type_name）
        #             > attr_type_defs 自动探测 > 默认名（后两步在 run_comm_query 内完成）
        type_name = (params.get("type_name") or "").strip() or read_comm_type_name_setting(hass)

        try:
            result = await self._exec_in_executor(
                hass, run_comm_query, self._db_path, type_name, query_type, params
            )
        except CommTableMissing as exc:
            return self.json({"success": False, "error": str(exc)}, status_code=404)
        except ValueError as exc:
            return self.json({"success": False, "error": str(exc)}, status_code=400)
        except Exception as exc:
            _LOGGER.exception("[comm] 查询失败 type=%s", query_type)
            return self.json({"success": False, "error": str(exc)}, status_code=500)

        # type_name 由 run_comm_query 回显（实际生效值，含自动探测结果）
        payload = {"success": True, "type": query_type}
        payload.update(result or {})
        return self.json(payload)


def register_api_views(hass: HomeAssistant, db_path: str) -> None:
    """注册通讯数据查询 API。由 __init__._register_api_views 调用。"""
    hass.http.register_view(CommApiView(db_path))
    # 字段回填（归属地 / 运营商 / 姓名 / 坐标）
    hass.http.register_view(CommBackfillView(db_path))
    # 通讯录（号码 → 姓名）：用户自行导入，供 party_name 回填
    hass.http.register_view(CommContactsView(db_path))
    _LOGGER.info(
        "[comm] API 已注册：%s / %s / %s",
        COMM_VIEW_URL, BACKFILL_VIEW_URL, CONTACTS_VIEW_URL,
    )
