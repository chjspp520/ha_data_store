"""SQLite 时间列索引工具（LIKE 日期前缀过滤专用）。

为什么需要它
------------
本集成的日期过滤统一写作 `col LIKE 'YYYY-MM-DD%'` / `'YYYY-MM%'` / `'YYYY%'`
（全仓上百处），而 `device_history`、`env_*` 这类大表原先只有
`(entity_id, on_time)`、`(entity_id, datetime)` 这样的**复合**索引：首列
（entity_id）没有等值约束时复合索引用不上，`WHERE on_time LIKE '日期%'`
就会退化成全表扫描。实测（真实库副本，16.8 万 / 19.2 万行）：

    env_temperature  WHERE datetime LIKE '2026-10-03%'   27.9 ms → 2.4 ms
    device_history   WHERE on_time   LIKE '2026-10-03%'   22.9 ms → 2.2 ms

注意：**单纯把查询改写成范围比较 `col >= ? AND col < ?` 并不会变快**，
因为复合索引同样用不上（实测 PLAN 仍是 SCAN）。必须补一条**独立**时间列索引。

为什么索引一定要 `COLLATE NOCASE`
---------------------------------
SQLite 的「LIKE 前缀优化」只在两种情形下生效：
  ① 列排序规则为 BINARY 且打开 `case_sensitive_like`（已废弃，不推荐）；
  ② 列排序规则为 NOCASE 且 `case_sensitive_like` 关闭（默认行为）。
本集成使用默认 LIKE（大小写不敏感），因此索引建成 NOCASE 即命中情形 ②，
**一行 SQL 都不用改**就能让现存 LIKE 全部走索引，PLAN 由
`SCAN` 变为 `SEARCH ... USING COVERING INDEX ..._nc (datetime>? AND datetime<?)`。

维护提示（重要）
----------------
若将来把某处查询改写成范围比较 `col >= ? AND col < ?`，**NOCASE 索引不会被采用**
（该表达式的排序规则是 BINARY），会静默退回全表扫描。届时要么保持 LIKE 写法，
要么把索引改回 BINARY（`DROP INDEX` 后重建）。时间串只含数字 / `-` / `:` / 空格，
NOCASE 与 BINARY 的排序与比较结果完全一致，故 NOCASE 索引不会改变
`ORDER BY` / 范围比较的语义，也不需要改动任何历史数据。
"""
from __future__ import annotations

import logging
import sqlite3

_LOGGER = logging.getLogger(__name__)


def time_index_name(table: str, column: str) -> str:
    """返回时间列索引名（统一命名，便于识别与回滚）。"""
    return f"idx_{table}_{column}_nc"


def open_rows_index_name(table: str) -> str:
    """返回「未关闭行」部分索引名。"""
    return f"idx_{table}_open"


def ensure_open_rows_index(conn: sqlite3.Connection, table: str,
                           column: str = "off_time") -> str | None:
    """为「未关闭行」建**部分索引**（WHERE off_time = ''），幂等。

    用途：判断实时状态（现在有没有人 / 门开着没 / 哪几盏灯开着）只需要
    「尚未关闭」的那些行，而这些行在任何时刻都极少（实测当前库里 0 行）。
    部分索引只收录这些行，因此：
      · 索引体积几乎为 0（实测：建索引 29 ms，库大小不变）
      · 查询代价与历史总行数**无关**（实测 presence 277 ms → <1 ms）
    注意：SQLite 只有在查询 WHERE 中**显式包含** `off_time = ''`（或其它蕴含该
    谓词的条件）时才会使用部分索引，因此查询必须写成 `WHERE off_time = '' AND ...`；
    写成 `(off_time = '' OR off_time IS NULL)` 会导致索引失效（本表该列 NOT NULL，
    正常不会有 NULL）。
    """
    name = open_rows_index_name(table)
    try:
        conn.execute(
            f'CREATE INDEX IF NOT EXISTS "{name}" ON "{table}" (name) '
            f'WHERE {column} = \'\';'
        )
        return name
    except Exception as exc:  # noqa: BLE001
        _LOGGER.warning("[HDS] 建「未关闭行」索引失败 table=%s: %s", table, exc)
        return None


def ensure_time_index(conn: sqlite3.Connection, table: str, column: str) -> str | None:
    """为时间列建 NOCASE 独立索引（幂等）。失败只记日志，不抛异常。

    首次创建会扫描全表（实测：device_history 16.8 万行约 200 ms、
    env_* 19.2 万行各约 150 ms），之后 `CREATE INDEX IF NOT EXISTS` 为空操作。
    调用方应确保在 executor 线程内执行，避免阻塞事件循环。
    """
    name = time_index_name(table, column)
    try:
        conn.execute(
            f'CREATE INDEX IF NOT EXISTS "{name}" '
            f'ON "{table}" ("{column}" COLLATE NOCASE);'
        )
        return name
    except Exception as exc:  # noqa: BLE001 —— 建索引失败不应影响集成加载
        _LOGGER.warning("[HDS] 建时间列索引失败 table=%s column=%s: %s", table, column, exc)
        return None
