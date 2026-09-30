"""数据导入 / 导出模块 — 独立模块。

功能
----
1. 解析 CSV / JSON 文本（文件由前端读成文本后 POST，或直接粘贴）
2. 按「目标列 ← 源列 / 固定值」的映射，向指定表 **追加** 或 **Upsert** 数据
3. 目标表不存在时可按数据源表头 **自动建表**（列类型自动推断）
4. 目标表缺少列时可选择自动补列
5. 导出目标表数据 / 空模板为 CSV

映射语法
--------
mapping = {"源列名": "目标列", "=固定值": "目标列"}
    - 值为源列名时取该列的值（源列名必须出现在数据源表头里）
    - 以 "=" 开头表示固定值，直接写入等号后的字面量

值转换（基础层，可用 options 关闭）
----------------------------------
    - 数值列：去除货币符号与千分位，'¥12,345.6' → 12345.6
    - 日期文本：'2026/9/1 17:24' → '2026-09-01 17:24:00'
    - 空值：NOT NULL 列取其 DDL 默认值，无默认值则记为错误行

API
---
    POST /api/ha_data_store/import/parse      解析数据源文本 → 列名 + 全部行 + 预览
    POST /api/ha_data_store/import            单批导入（支持 dry_run 试运行）
    GET  /api/ha_data_store/import/template   下载目标表空模板 CSV
    GET  /api/ha_data_store/export/csv        导出表数据为 CSV
"""
from __future__ import annotations

import csv
import io
import json
import logging
import re
import sqlite3
from typing import Any

from aiohttp import web
from homeassistant.core import HomeAssistant

from .const import (
    TABLE_API_KEYS,
    TABLE_API_SETTINGS,
    TABLE_API_SOURCE_CONFIGS,
    TABLE_ATTR_TYPE_DEFS,
    TABLE_CUSTOM_ROUTES,
    TABLE_ENTITY_CONFIGS,
    TABLE_EXPORT_CONFIGS,
    TABLE_FILE_SOURCE_CONFIGS,
    TABLE_PUSH_TARGETS,
    TABLE_VACUUM_CONFIGS,
    TABLE_VACUUM_TYPE_DEFS,
)
from .http_api import _BaseDBView

_LOGGER = logging.getLogger(__name__)

IMPORT_PARSE_URL = "/api/ha_data_store/import/parse"
IMPORT_URL = "/api/ha_data_store/import"
IMPORT_TEMPLATE_URL = "/api/ha_data_store/import/template"
EXPORT_CSV_URL = "/api/ha_data_store/export/csv"

# 不允许通过导入写入的核心配置表（与数据库浏览器删除保护保持一致）
PROTECTED_TABLES = frozenset({
    TABLE_ENTITY_CONFIGS, TABLE_ATTR_TYPE_DEFS, TABLE_CUSTOM_ROUTES,
    TABLE_EXPORT_CONFIGS, TABLE_FILE_SOURCE_CONFIGS, TABLE_API_SOURCE_CONFIGS,
    TABLE_API_KEYS, TABLE_API_SETTINGS, TABLE_VACUUM_TYPE_DEFS, TABLE_VACUUM_CONFIGS,
    TABLE_PUSH_TARGETS,
})

_MAX_TEXT_BYTES = 32 * 1024 * 1024    # 单次请求的文本上限（前端超限会自行分片，单片约 4 MB）
_MAX_ROWS_PER_BATCH = 200000          # 单次导入的最大行数
_MAX_ERROR_ROWS = 50                  # 错误明细最多返回条数
_PREVIEW_ROWS = 20                    # 解析预览行数（解析不再回传全量数据）
_INFER_ROWS = 1000                    # 建表类型推断的采样行数
_PROCESS_BATCH = 2000                 # 导入处理批大小（转换 + 写库的批次）
_EXECUTEMANY_BATCH = 500              # executemany 分批大小

_TABLE_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CURRENCY_RE = re.compile(r"[¥￥$€£,\s]")
_INT_RE = re.compile(r"[+-]?\d+")
_DATETIME_RE = re.compile(
    r"^(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})日?"
    r"(?:[ T](\d{1,2})[:.时](\d{1,2})(?:[:.分](\d{1,2}))?秒?)?$"
)


# =========================================================================== #
#  文本解析                                                                     #
# =========================================================================== #
def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def _split_delimiter(text: str, delimiter: str = "") -> str:
    """确定 CSV 分隔符：显式指定则原样使用，否则按样本嗅探（兜底逗号）。"""
    delim = delimiter or ""
    if delim and delim != "auto":
        return delim
    try:
        return csv.Sniffer().sniff(text[:8192], delimiters=",;\t|").delimiter
    except csv.Error:
        return ","


def _iter_csv(text: str, delimiter: str = "", has_header: bool = True) -> tuple:
    """流式解析 CSV 文本 → (列名列表, 行迭代器)。

    行迭代器逐行产出定长单元格数组（短行补空），内存中只保留当前行，
    因此处理大文件时占用与行数无关。
    """
    reader = csv.reader(io.StringIO(text), delimiter=_split_delimiter(text, delimiter))

    def _cells(raw) -> list:
        return [str(c) for c in raw]

    def _blank(cells: list) -> bool:
        return not any(c.strip() for c in cells)

    columns: list = []
    pending = None
    for raw in reader:
        cells = _cells(raw)
        if _blank(cells):
            continue
        if has_header:
            columns = [c.strip() for c in cells]
        else:
            columns = [f"列{i + 1}" for i in range(len(cells))]
            pending = cells
        break

    if not columns:
        return [], iter(())
    width = len(columns)

    def _rows():
        if pending is not None:
            cells = pending + [""] * (width - len(pending))
            yield cells[:width]
        for raw in reader:
            cells = _cells(raw)
            if _blank(cells):
                continue
            if len(cells) < width:
                cells += [""] * (width - len(cells))
            yield cells[:width]

    return columns, _rows()


def _parse_json(text: str) -> tuple:
    """解析 JSON 文本 → (列名列表, 二维字符串行)，支持对象数组与数组的数组。"""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON 解析失败：{exc.msg}（第 {exc.lineno} 行）") from exc

    if isinstance(payload, dict):
        found = None
        for value in payload.values():
            if isinstance(value, list):
                found = value
                break
        if found is None:
            raise ValueError("JSON 对象中没有数组，无法导入")
        payload = found
    if not isinstance(payload, list) or not payload:
        raise ValueError("JSON 内容为空或不是数组")

    columns: list[str] = []
    for item in payload:
        if isinstance(item, dict):
            for key in item:
                if key not in columns:
                    columns.append(str(key))
    if columns:
        rows = [
            [_stringify(item.get(col)) for col in columns]
            for item in payload
            if isinstance(item, dict)
        ]
        return columns, rows

    first = payload[0]
    if isinstance(first, list):
        width = max(len(r) for r in payload if isinstance(r, list))
        columns = [f"列{i + 1}" for i in range(width)]
        rows = []
        for item in payload:
            if not isinstance(item, list):
                continue
            cells = [_stringify(v) for v in item]
            cells += [""] * (width - len(cells))
            rows.append(cells[:width])
        return columns, rows

    raise ValueError("JSON 数组的元素既不是对象也不是数组，无法解析")


def _normalize_text(text: str) -> str:
    content = (text or "").replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    if not content.strip():
        raise ValueError("数据内容为空")
    if len(content.encode("utf-8")) > _MAX_TEXT_BYTES:
        raise ValueError(
            f"单次提交的数据超过 {_MAX_TEXT_BYTES // 1024 // 1024} MB，"
            f"请在客户端按行拆分后分批提交"
        )
    return content


def _resolve_format(content: str, fmt: str) -> str:
    if fmt in ("", "auto", None):
        return "json" if content.lstrip()[:1] in ("[", "{") else "csv"
    if fmt not in ("csv", "json"):
        raise ValueError("format 只能是 csv 或 json")
    return fmt


def open_source_rows(text: str, fmt: str = "auto", has_header: bool = True,
                     delimiter: str = "") -> tuple:
    """解析数据源文本 → (列名列表, 行迭代器, 实际使用的 format)。

    行迭代器只能消费一次：调用方按需逐批处理，不必把全量数据放进内存。
    """
    content = _normalize_text(text)
    resolved = _resolve_format(content, fmt)
    if resolved == "json":
        columns, rows = _parse_json(content)
        return columns, iter(rows), resolved
    columns, row_iter = _iter_csv(content, delimiter, has_header)
    return columns, row_iter, resolved


def parse_source_text(text: str, fmt: str = "auto", has_header: bool = True,
                      delimiter: str = "") -> dict:
    """解析数据源文本，只回传列名、总行数与预览行（**不回传全量数据**）。"""
    columns, row_iter, resolved = open_source_rows(text, fmt, has_header, delimiter)
    if not columns:
        raise ValueError("未能识别出任何列（请检查分隔符与表头）")

    preview: list = []
    total = 0
    for row in row_iter:
        total += 1
        if len(preview) < _PREVIEW_ROWS:
            preview.append(row)
    if total == 0:
        raise ValueError("只识别到表头，没有数据行")

    return {
        "format": resolved,
        "columns": columns,
        "total_rows": total,
        "preview": preview,
    }


# =========================================================================== #
#  值转换                                                                       #
# =========================================================================== #
def _clean_number(text: str) -> Any:
    """把 '¥12,345.6' / '12.5元' 之类的文本转成数值，失败返回 None。"""
    cleaned = _CURRENCY_RE.sub("", text).rstrip("%")
    cleaned = cleaned.rstrip("元块个次分钟秒")
    if cleaned in ("", "-", "+", ".", "-.", "+."):
        return None
    if _INT_RE.fullmatch(cleaned):
        try:
            return int(cleaned)
        except ValueError:
            return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _clean_datetime(text: str) -> str | None:
    """把 '2026/9/1 17:24' / '2026年9月1日' 规范化为 'YYYY-MM-DD HH:MM:SS'，不匹配返回 None。"""
    match = _DATETIME_RE.match(text.strip())
    if not match:
        return None
    year, month, day, hour, minute, second = match.groups()
    try:
        return (
            f"{int(year):04d}-{int(month):02d}-{int(day):02d} "
            f"{int(hour or 0):02d}:{int(minute or 0):02d}:{int(second or 0):02d}"
        )
    except (TypeError, ValueError):
        return None


def _parse_default_literal(dflt: Any) -> Any:
    """把 DDL 里的默认值字面量转成 Python 值。"""
    if dflt is None:
        return None
    text = str(dflt).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        return text[1:-1]
    if _INT_RE.fullmatch(text):
        try:
            return int(text)
        except ValueError:
            return text
    try:
        return float(text)
    except ValueError:
        return text


def _empty_value(col_meta: dict) -> Any:
    """空单元格的写入值：可空 → NULL；NOT NULL → 用 DDL 默认值，取不到则回退类型零值。"""
    if not col_meta.get("notnull"):
        return None
    if col_meta.get("dflt_value") is not None:
        fallback = _parse_default_literal(col_meta["dflt_value"])
        if fallback is not None:
            return fallback
    col_type = (col_meta.get("type") or "").upper()
    if "INT" in col_type:
        return 0
    if "REAL" in col_type or "NUM" in col_type or "DEC" in col_type or "FLOA" in col_type:
        return 0.0
    return ""


def _convert_value(raw: Any, col_meta: dict, options: dict) -> Any:
    """按目标列类型与选项转换单元格值。"""
    text = ("" if raw is None else raw if isinstance(raw, str) else str(raw)).strip()
    if text == "":
        return _empty_value(col_meta)

    if options.get("normalize_time", True):
        normalized = _clean_datetime(text)
        if normalized is not None:
            return normalized

    col_type = (col_meta.get("type") or "").upper()
    is_numeric = ("INT" in col_type or "REAL" in col_type
                  or "NUM" in col_type or "DEC" in col_type or "FLOA" in col_type)
    if is_numeric and options.get("parse_number", True):
        number = _clean_number(text)
        if number is not None:
            return number
    return text


# =========================================================================== #
#  建表 / 补列                                                                  #
# =========================================================================== #
def _sample_values(target_src: dict, rows: list) -> dict:
    """按目标列采样非空原始值（用于类型推断）。"""
    samples: dict[str, list] = {col: [] for col in target_src}
    for row in rows[:_INFER_ROWS]:
        if not isinstance(row, (list, tuple)):
            continue
        for target_col, ref in target_src.items():
            if ref[0] == "fixed":
                raw = ref[1]
            else:
                raw = row[ref[1]] if ref[1] < len(row) else ""
            text = ("" if raw is None else str(raw)).strip()
            if text:
                samples[target_col].append(text)
    return samples


def _infer_columns(target_src: dict, rows: list) -> list:
    """推断目标列类型：全整数 → INTEGER，全数值 → REAL，其余 TEXT。"""
    samples = _sample_values(target_src, rows)
    inferred = []
    for target_col in target_src:
        values = samples[target_col]
        if not values:
            inferred.append((target_col, "TEXT"))
            continue
        if all(_INT_RE.fullmatch(_CURRENCY_RE.sub("", v).rstrip("元块个次分钟秒")) for v in values):
            inferred.append((target_col, "INTEGER"))
        elif all(_clean_number(v) is not None for v in values):
            inferred.append((target_col, "REAL"))
        else:
            inferred.append((target_col, "TEXT"))
    return inferred


def _create_table(conn: sqlite3.Connection, table: str, target_src: dict,
                  rows: list) -> list:
    """按推断出的列类型建表。"""
    inferred = _infer_columns(target_src, rows)
    col_defs = ", ".join(f'"{name}" {col_type}' for name, col_type in inferred)
    conn.execute(f'CREATE TABLE "{table}" ({col_defs})')
    _LOGGER.info("[import] 已创建表 %s（%d 列）", table, len(inferred))
    return inferred


def _add_columns(conn: sqlite3.Connection, table: str, target_src: dict,
                 rows: list, missing: list) -> list:
    """为缺失的目标列补建列（类型按数据推断）。"""
    inferred = dict(_infer_columns(target_src, rows))
    added = []
    for col in missing:
        col_type = inferred.get(col, "TEXT")
        conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{col}" {col_type}')
        added.append((col, col_type))
    if added:
        _LOGGER.info("[import] 表 %s 新增列 %s", table, added)
    return added


# =========================================================================== #
#  导入核心                                                                     #
# =========================================================================== #
def _make_key(values: list) -> str:
    return "\x1f".join("\x00" if v is None else str(v) for v in values)


def _table_meta(conn: sqlite3.Connection, table: str) -> dict:
    """读取列元信息 {列名: {type, notnull, dflt_value}}。"""
    meta = {}
    for row in conn.execute(f'PRAGMA table_info("{table}")'):
        meta[row[1]] = {
            "type": (row[2] or "").upper(),
            "notnull": bool(row[3]),
            "dflt_value": row[4],
        }
    return meta


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def import_payload(db_path: str, payload: dict) -> dict:
    """导入入口：payload 提供 text（推荐，后端自行解析并分批）或 columns + rows（兼容）。"""
    text = payload.get("text")
    if isinstance(text, str) and text.strip():
        columns, row_iter, _fmt = open_source_rows(
            text,
            str(payload.get("format") or "auto"),
            bool(payload.get("has_header", True)),
            str(payload.get("delimiter") or ""),
        )
        return _do_import(db_path, payload, columns, row_iter)
    return import_rows(db_path, payload)


def import_rows(db_path: str, payload: dict, src_columns: list | None = None,
                row_iter=None) -> dict:
    """执行导入（在 executor 线程中调用）。

    默认从 `payload` 的 `columns` + `rows` 取数据（兼容旧调用）；
    也可由 `import_payload` 直接传入解析好的 `src_columns` 与行迭代器。
    """
    if src_columns is None or row_iter is None:
        src_columns = [str(c) for c in (payload.get("columns") or [])]
        raw_rows = payload.get("rows")
        if not isinstance(raw_rows, list) or not raw_rows:
            raise ValueError("没有可导入的数据（请提供 text 或 columns + rows）")
        if len(raw_rows) > _MAX_ROWS_PER_BATCH:
            raise ValueError(f"单次最多 {_MAX_ROWS_PER_BATCH} 行，请分批提交")
        row_iter = iter(raw_rows)
    return _do_import(db_path, payload, src_columns, row_iter)


def _flush_batch(conn: sqlite3.Connection, insert_sql: str, update_sql: str,
                 inserts: list, updates: list) -> tuple:
    """写出一个批次（整批失败时降级为逐行重试），返回 (新增, 更新, 错误列表)，并清空入参。"""
    written_insert = 0
    written_update = 0
    errors: list = []

    if inserts:
        try:
            conn.executemany(insert_sql, [values for _no, values, _key in inserts])
            written_insert = len(inserts)
        except sqlite3.Error:
            for row_no, values, _key in inserts:
                try:
                    conn.execute(insert_sql, values)
                    written_insert += 1
                except sqlite3.Error as exc:
                    errors.append({"row": row_no, "reason": f"写入失败：{exc}"})

    if updates:
        try:
            conn.executemany(update_sql, [values + [rowid] for _no, values, rowid in updates])
            written_update = len(updates)
        except sqlite3.Error:
            for row_no, values, rowid in updates:
                try:
                    conn.execute(update_sql, values + [rowid])
                    written_update += 1
                except sqlite3.Error as exc:
                    errors.append({"row": row_no, "reason": f"更新失败：{exc}"})

    inserts.clear()
    updates.clear()
    return written_insert, written_update, errors


def _do_import(db_path: str, payload: dict, src_columns: list, row_iter) -> dict:
    """校验 → 采样推断 → 分批转换与写入（内存中只保留当前批，与总行数无关）。"""
    table = str(payload.get("table") or "").strip()
    mode = str(payload.get("mode") or "append").strip().lower()
    mapping = payload.get("mapping") or {}
    options = payload.get("options") or {}
    key_columns = [str(c).strip() for c in (payload.get("key_columns") or []) if str(c).strip()]
    dry_run = bool(payload.get("dry_run"))
    allow_create = bool(payload.get("create_table"))
    allow_add_columns = bool(payload.get("add_columns"))

    if not table:
        raise ValueError("缺少目标表名")
    if not _TABLE_NAME_RE.fullmatch(table):
        raise ValueError("表名只能包含字母、数字与下划线，且不能以数字开头")
    if table.startswith("sqlite_") or table in PROTECTED_TABLES:
        raise ValueError(f"核心配置表「{table}」不允许通过导入写入，请使用对应的系统配置页面")
    if mode not in ("append", "upsert"):
        raise ValueError("mode 只能是 append 或 upsert")
    if mode == "upsert" and not key_columns:
        raise ValueError("Upsert 模式必须指定唯一键列")
    if not isinstance(mapping, dict) or not mapping:
        raise ValueError("缺少字段映射")
    if not src_columns:
        raise ValueError("未能识别出数据源列（请检查分隔符与表头）")

    # 映射解析：目标列 → ("col", 源列下标) / ("fixed", 字面值)
    src_index = {name: i for i, name in enumerate(src_columns)}
    target_src: dict[str, tuple] = {}
    for raw_src, raw_target in mapping.items():
        target_col = str(raw_target).strip()
        src_key = str(raw_src)
        if not target_col:
            continue
        if not _TABLE_NAME_RE.fullmatch(target_col):
            raise ValueError(f"目标列名「{target_col}」不合法")
        if src_key.startswith("="):
            target_src[target_col] = ("fixed", src_key[1:])
            continue
        if src_key not in src_index:
            raise ValueError(f"数据源里不存在列「{src_key}」")
        target_src[target_col] = ("col", src_index[src_key])
    if not target_src:
        raise ValueError("没有有效的字段映射")

    target_cols = list(target_src.keys())
    for col in key_columns:
        if col not in target_cols:
            raise ValueError(f"唯一键列「{col}」必须出现在字段映射里")

    # 采样若干行用于建表类型推断；采样行随后重新并入处理流程，不会丢数据
    sample: list = []
    for row in row_iter:
        sample.append(row)
        if len(sample) >= _INFER_ROWS:
            break
    if not sample:
        raise ValueError("没有可导入的数据行")

    def _all_rows():
        yield from sample
        yield from row_iter

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        created_columns: list = []
        added_columns: list = []
        existed = _table_exists(conn, table)
        if not existed:
            if not allow_create:
                raise ValueError(
                    f"表「{table}」不存在；如需按数据表头自动建表，请勾选「自动建表」"
                )
            inference = _infer_columns(target_src, sample)
            if dry_run:
                # 试运行不改变表结构，只记录将会执行的建表动作
                created_columns = inference
                col_meta = {
                    name: {"type": col_type, "notnull": False, "dflt_value": None}
                    for name, col_type in inference
                }
            else:
                created_columns = _create_table(conn, table, target_src, sample)
                col_meta = _table_meta(conn, table)
        else:
            col_meta = _table_meta(conn, table)

        missing = [c for c in target_cols if c not in col_meta]
        if missing:
            if not allow_add_columns:
                raise ValueError(
                    "目标表缺少列：" + "、".join(missing)
                    + "（可勾选「自动补列」，或先在数据浏览的字段管理中添加）"
                )
            inferred_map = dict(_infer_columns(target_src, sample))
            if dry_run:
                added_columns = [(c, inferred_map.get(c, "TEXT")) for c in missing]
                for col in missing:
                    col_meta[col] = {
                        "type": inferred_map.get(col, "TEXT"),
                        "notnull": False,
                        "dflt_value": None,
                    }
            else:
                added_columns = _add_columns(conn, table, target_src, sample, missing)
                col_meta = _table_meta(conn, table)

        # Upsert：一次性读取现有唯一键 → rowid
        key_map: dict[str, int] = {}
        if mode == "upsert" and existed:
            select_cols = ", ".join(f'"{c}"' for c in key_columns)
            for row in conn.execute(f'SELECT rowid AS _rid, {select_cols} FROM "{table}"'):
                key_map[_make_key([row[c] for c in key_columns])] = row["_rid"]

        col_sql = ", ".join(f'"{c}"' for c in target_cols)
        placeholder_sql = ", ".join("?" for _ in target_cols)
        insert_sql = f'INSERT INTO "{table}" ({col_sql}) VALUES ({placeholder_sql})'
        set_sql = ", ".join(f'"{c}" = ?' for c in target_cols)
        update_sql = f'UPDATE "{table}" SET {set_sql} WHERE rowid = ?'

        errors: list = []
        written_insert = 0
        written_update = 0
        total = 0
        inserts: list = []                   # (行号, [值...], 唯一键)
        updates: list = []                   # (行号, [值...], rowid)
        batch_seen: dict[str, int] = {}      # 批内去重：唯一键 → inserts 下标

        for row in _all_rows():
            total += 1
            if total > _MAX_ROWS_PER_BATCH:
                raise ValueError(f"单次导入最多 {_MAX_ROWS_PER_BATCH} 行，请分批提交")
            if not isinstance(row, (list, tuple)):
                errors.append({"row": total, "reason": "该行不是数组"})
                continue

            values = {}
            for target_col, ref in target_src.items():
                if ref[0] == "fixed":
                    raw = ref[1]
                else:
                    index = ref[1]
                    raw = row[index] if index < len(row) else ""
                values[target_col] = _convert_value(raw, col_meta[target_col], options)
            ordered = [values.get(c) for c in target_cols]

            if mode == "upsert":
                key = _make_key([values.get(c) for c in key_columns])
                rowid = key_map.get(key)
                if rowid is not None:
                    updates.append((total, ordered, rowid))
                else:
                    previous = batch_seen.get(key)
                    if previous is not None:
                        # 同一批内出现重复唯一键 → 保留最后一条
                        inserts[previous] = (inserts[previous][0], ordered, key)
                    else:
                        batch_seen[key] = len(inserts)
                        inserts.append((total, ordered, key))
            else:
                inserts.append((total, ordered, None))

            if len(inserts) + len(updates) >= _PROCESS_BATCH:
                if dry_run:
                    written_insert += len(inserts)
                    written_update += len(updates)
                    inserts.clear()
                    updates.clear()
                    batch_seen.clear()
                else:
                    got_insert, got_update, batch_errors = _flush_batch(
                        conn, insert_sql, update_sql, inserts, updates
                    )
                    written_insert += got_insert
                    written_update += got_update
                    errors.extend(batch_errors)
                    batch_seen.clear()

        if inserts or updates:
            if dry_run:
                written_insert += len(inserts)
                written_update += len(updates)
                inserts.clear()
                updates.clear()
            else:
                got_insert, got_update, batch_errors = _flush_batch(
                    conn, insert_sql, update_sql, inserts, updates
                )
                written_insert += got_insert
                written_update += got_update
                errors.extend(batch_errors)

        if dry_run:
            conn.rollback()
        else:
            conn.commit()
        _LOGGER.info(
            "[import] 表 %s 模式 %s 新增 %d 更新 %d 失败 %d（共 %d 行）",
            table, mode, written_insert, written_update, len(errors), total,
        )
        return {
            "dry_run": dry_run,
            "table": table,
            "mode": mode,
            "inserted": written_insert,
            "updated": written_update,
            "failed": len(errors),
            "errors": errors[:_MAX_ERROR_ROWS],
            "created_table": bool(created_columns),
            "created_columns": [{"column": c, "type": t} for c, t in created_columns],
            "added_columns": [{"column": c, "type": t} for c, t in added_columns],
            "total_rows": total,
        }
    finally:
        conn.close()


# =========================================================================== #
#  导出 CSV                                                                     #
# =========================================================================== #
def _csv_bytes(header: list, rows: list) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(header)
    writer.writerows(rows)
    # utf-8-sig：带 BOM，保证 Excel 直接双击不乱码
    return buffer.getvalue().encode("utf-8-sig")


def build_template_csv(db_path: str, table: str) -> tuple:
    """生成目标表的空模板 CSV（仅列头）。"""
    if not _TABLE_NAME_RE.fullmatch(table):
        raise ValueError("表名不合法")
    conn = sqlite3.connect(db_path)
    try:
        if not _table_exists(conn, table):
            raise ValueError(f"表「{table}」不存在")
        columns = [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]
    finally:
        conn.close()
    if not columns:
        raise ValueError(f"表「{table}」没有可导出的列")
    return _csv_bytes(columns, []), columns


def export_table_csv(db_path: str, table: str, limit: int = 0,
                     order_by: str = "", order_dir: str = "ASC") -> tuple:
    """导出表数据为 CSV。"""
    if not _TABLE_NAME_RE.fullmatch(table):
        raise ValueError("表名不合法")
    conn = sqlite3.connect(db_path)
    try:
        if not _table_exists(conn, table):
            raise ValueError(f"表「{table}」不存在")
        columns = [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]
        if not columns:
            raise ValueError(f"表「{table}」没有可导出的列")

        order_sql = ""
        if order_by and order_by in columns:
            direction = "DESC" if str(order_dir).upper() == "DESC" else "ASC"
            order_sql = f' ORDER BY "{order_by}" {direction}'
        limit_sql = f" LIMIT {int(limit)}" if limit and int(limit) > 0 else ""

        col_sql = ", ".join(f'"{c}"' for c in columns)
        rows = [
            ["" if v is None else v for v in row]
            for row in conn.execute(f'SELECT {col_sql} FROM "{table}"{order_sql}{limit_sql}')
        ]
    finally:
        conn.close()
    return _csv_bytes(columns, rows), columns, len(rows)


# =========================================================================== #
#  HTTP API                                                                   #
# =========================================================================== #
class DataImportParseView(_BaseDBView):
    """解析数据源文本（CSV / JSON），返回列名与全部数据行。"""

    url = IMPORT_PARSE_URL
    name = "api:ha_data_store:data_import_parse"

    async def post(self, request: web.Request) -> web.Response:
        if (resp := self._check_master_switch(request.app["hass"])):
            return resp
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_db_viewer_enabled(hass)):
            return resp

        try:
            body = await request.json()
        except Exception:
            return self.json({"success": False, "error": "请求体不是合法的 JSON"}, status_code=400)

        text = body.get("text") or ""
        fmt = str(body.get("format") or "auto")
        has_header = bool(body.get("has_header", True))
        delimiter = str(body.get("delimiter") or "")

        try:
            result = await self._exec_in_executor(
                hass, parse_source_text, text, fmt, has_header, delimiter
            )
        except ValueError as exc:
            return self.json({"success": False, "error": str(exc)}, status_code=400)
        except Exception as exc:
            _LOGGER.exception("[import] 解析数据源失败")
            return self.json({"success": False, "error": str(exc)}, status_code=500)

        payload = {"success": True}
        payload.update(result)
        return self.json(payload)


class DataImportView(_BaseDBView):
    """执行一批数据导入（支持 dry_run 试运行）。"""

    url = IMPORT_URL
    name = "api:ha_data_store:data_import"

    async def post(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_master_switch(hass)):
            return resp
        if (resp := self._check_db_viewer_enabled(hass)):
            return resp
        if (resp := self._check_db_edit_enabled(hass)):
            return resp

        try:
            body = await request.json()
        except Exception:
            return self.json({"success": False, "error": "请求体不是合法的 JSON"}, status_code=400)
        if not isinstance(body, dict):
            return self.json({"success": False, "error": "请求体必须是 JSON 对象"}, status_code=400)

        try:
            result = await self._exec_in_executor(hass, import_payload, self._db_path, body)
        except ValueError as exc:
            return self.json({"success": False, "error": str(exc)}, status_code=400)
        except Exception as exc:
            _LOGGER.exception("[import] 导入失败")
            return self.json({"success": False, "error": str(exc)}, status_code=500)

        payload = {"success": True}
        payload.update(result)
        return self.json(payload)


class DataImportTemplateView(_BaseDBView):
    """下载目标表的空模板 CSV。"""

    url = IMPORT_TEMPLATE_URL
    name = "api:ha_data_store:data_import_template"

    async def get(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_master_switch(hass)):
            return resp
        if (resp := self._check_db_viewer_enabled(hass)):
            return resp

        table = (request.query.get("table") or "").strip()
        if not table:
            return self.json({"success": False, "error": "缺少 table 参数"}, status_code=400)

        try:
            content, _columns = await self._exec_in_executor(
                hass, build_template_csv, self._db_path, table
            )
        except ValueError as exc:
            return self.json({"success": False, "error": str(exc)}, status_code=400)
        except Exception as exc:
            _LOGGER.exception("[import] 生成模板失败")
            return self.json({"success": False, "error": str(exc)}, status_code=500)

        filename = f"template_{table}.csv"
        return web.Response(
            body=content,
            content_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )


class DataExportCsvView(_BaseDBView):
    """导出表数据为 CSV。"""

    url = EXPORT_CSV_URL
    name = "api:ha_data_store:export_csv"

    async def get(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_master_switch(hass)):
            return resp
        if (resp := self._check_db_viewer_enabled(hass)):
            return resp

        table = (request.query.get("table") or "").strip()
        if not table:
            return self.json({"success": False, "error": "缺少 table 参数"}, status_code=400)
        try:
            limit = int(request.query.get("limit", 0) or 0)
        except ValueError:
            limit = 0
        order_by = (request.query.get("order_by") or "").strip()
        order_dir = (request.query.get("order_dir") or "ASC").strip()

        try:
            content, _columns, _count = await self._exec_in_executor(
                hass, export_table_csv, self._db_path, table, limit, order_by, order_dir
            )
        except ValueError as exc:
            return self.json({"success": False, "error": str(exc)}, status_code=400)
        except Exception as exc:
            _LOGGER.exception("[import] 导出 CSV 失败")
            return self.json({"success": False, "error": str(exc)}, status_code=500)

        filename = f"{table}.csv"
        return web.Response(
            body=content,
            content_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )


def register_api_views(hass: HomeAssistant, db_path: str) -> None:
    """注册数据导入 / 导出 API。由 __init__._register_api_views 调用。"""
    hass.http.register_view(DataImportParseView(db_path))
    hass.http.register_view(DataImportView(db_path))
    hass.http.register_view(DataImportTemplateView(db_path))
    hass.http.register_view(DataExportCsvView(db_path))
    _LOGGER.info(
        "[import] API 已注册：%s / %s / %s / %s",
        IMPORT_PARSE_URL, IMPORT_URL, IMPORT_TEMPLATE_URL, EXPORT_CSV_URL,
    )
