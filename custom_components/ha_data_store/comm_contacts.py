# -*- coding: utf-8 -*-
"""通讯录（号码 → 姓名）本地表，用于给通讯记录回填「对方姓名」。

为什么单独建表：姓名无法从任何内置库推出来，只能由用户自己提供。存在同一套
SQLite 里（`comm_contacts`）便于备份/迁移整套数据，且采集时能一次读进内存做
`party_number → party_name` 的映射，不必逐行查库。

导入格式（`parse_import`，自动嗅探）：
    1. JSON 数组   `[{"number": "13800138000", "name": "张三"}, ...]`
    2. JSON 对象   `{"13800138000": "张三", ...}`
    3. CSV / TSV   `号码,姓名` 每行一条（逗号 / 制表符 / 分号 / 空白分隔）
    4. 手机导出    `张三,13800138000`（列序自动判断：哪一列更像号码）

号码统一归一化（去 `+86` / `0086` / 空格 / 连字符），因此「+86 138-0013-8000」
与「13800138000」视为同一个号码，重复导入时**更新**姓名而不是新增。
"""
from __future__ import annotations

import csv
import io
import json
import logging
import re
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

_LOGGER = logging.getLogger(__name__)

TABLE_COMM_CONTACTS = "comm_contacts"

# 导入时可识别的列名别名（小写比较）
_NUMBER_KEYS = ("number", "phone", "tel", "mobile", "cell", "phone_number",
                "号码", "电话", "手机号", "手机", "号码1")
_NAME_KEYS = ("name", "contact", "nickname", "remark", "display_name",
              "姓名", "名字", "联系人", "备注", "称呼")

# 单次导入上限（防止误粘贴超大文件把内存吃满）
MAX_IMPORT_ROWS = 200000
# 样例返回条数
SAMPLE_LIMIT = 20


# ---------------------------------------------------------------- 号码归一化
def normalize_number(value: Any) -> str:
    """号码归一化：去掉国家码与所有非数字字符，只留纯数字。

    这样同一个号码在通讯录与通讯记录里写法不同也能对上：

        13800138000 / +86 138-0013-8000 / 008613800138000 / 8613800138000
            → 全部归一化为 13800138000

    注意**不带 `+` 的 `86` 前缀**也要处理 —— 有些导出（Excel / 部分手机）
    会给出 `8613800138000` 这种形式，早先只去掉 `+86` / `0086`，
    于是它归一化成 `8613800138000`，与采集到的 `13800138000` 对不上，
    表现为「通讯录里有这个人，但采集时匹配不到姓名」。
    判定条件保守：**纯数字长度 > 11 且以 `86` 开头**才剥掉前两位，
    避免误伤以 86 开头的固话区号（如 8610 北京，那是 10 位，长度不够）。
    """
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("+86"):
        text = text[3:]
    elif text.startswith("0086"):
        text = text[4:]
    digits = re.sub(r"\D", "", text)
    if len(digits) > 11 and digits.startswith("86"):
        digits = digits[2:]
    return digits


def _looks_like_number(text: str) -> bool:
    """判断一段文本「更像号码」还是「更像姓名」（用于 CSV 列序自动判断）。"""
    digits = normalize_number(text)
    if len(digits) < 7:
        return False
    # 号码里除了 +/空格/连字符/括号之外不应有别的字符
    stripped = re.sub(r"[\d\s+\-()]", "", str(text or ""))
    return not stripped


# ---------------------------------------------------------------- 建表
def ensure_table(conn: sqlite3.Connection) -> None:
    """确保通讯录表存在（幂等）。"""
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {TABLE_COMM_CONTACTS} (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            number     TEXT NOT NULL,
            name       TEXT NOT NULL DEFAULT '',
            raw        TEXT NOT NULL DEFAULT '',
            source     TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT '',
            UNIQUE(number)
        )
        """
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_comm_contacts_number "
        f"ON {TABLE_COMM_CONTACTS} (number)"
    )


def _table_exists(conn: sqlite3.Connection) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (TABLE_COMM_CONTACTS,),
        ).fetchone()
    )


# ---------------------------------------------------------------- 导入解析
def _row_from_mapping(item: Dict[str, Any]) -> Tuple[str, str]:
    """从一条 dict 记录里取 (号码, 姓名)，键名大小写不敏感并支持中文别名。"""
    lower = {str(k).strip().lower(): v for k, v in item.items()}
    number = ""
    for key in _NUMBER_KEYS:
        if lower.get(key) not in (None, ""):
            number = str(lower[key])
            break
    name = ""
    for key in _NAME_KEYS:
        if lower.get(key) not in (None, ""):
            name = str(lower[key])
            break
    # 兜底：没识别出列名时，按「哪一列像号码」判断
    if not number or not name:
        for k, v in lower.items():
            text = str(v or "")
            if not text:
                continue
            if _looks_like_number(text):
                number = number or text
            else:
                name = name or text
    return normalize_number(number), name.strip()


def parse_import(text: str, fmt: str = "auto") -> Tuple[List[Tuple[str, str]], List[str]]:
    """解析导入文本，返回 ([(号码, 姓名)], 警告列表)。

    `fmt` 为 `auto` / `json` / `csv`；`auto` 时先试 JSON，失败再按分隔文本解析。
    解析不出号码的行会被跳过并计入警告（最多保留 20 条，避免刷屏）。
    """
    raw = str(text or "").strip()
    if not raw:
        return [], ["内容为空"]

    rows: List[Tuple[str, str]] = []
    warns: List[str] = []

    def _collect(pairs) -> None:
        for number, name in pairs:
            if not number:
                if len(warns) < SAMPLE_LIMIT:
                    warns.append(f"跳过无号码的行：{name or '(空)'}")
                continue
            rows.append((number, name))

    parsed_json = None
    if fmt in ("auto", "json") and raw[:1] in "[{":
        try:
            parsed_json = json.loads(raw)
        except Exception as err:  # noqa: BLE001
            if fmt == "json":
                return [], [f"JSON 解析失败：{err}"]
            parsed_json = None

    if isinstance(parsed_json, list):
        pairs = []
        for item in parsed_json:
            if isinstance(item, dict):
                pairs.append(_row_from_mapping(item))
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                pairs.append((normalize_number(item[0]), str(item[1]).strip()))
        _collect(pairs)
    elif isinstance(parsed_json, dict):
        _collect((normalize_number(k), str(v).strip()) for k, v in parsed_json.items())
    else:
        # CSV / TSV / 分隔文本
        # 先按分隔符嗅探：取首个非空行，看哪种分隔符出现次数最多
        first = next((ln for ln in raw.splitlines() if ln.strip()), "")
        delim = ","
        for candidate in (",", "\t", ";", "|"):
            if first.count(candidate) > first.count(delim):
                delim = candidate
        reader = csv.reader(io.StringIO(raw), delimiter=delim)
        pairs = []
        for idx, cells in enumerate(reader):
            cells = [str(c).strip() for c in cells]
            cells = [c for c in cells if c != ""]
            if len(cells) < 2:
                if cells and idx == 0:
                    warns.append("首行只有一列，已跳过（表头或格式不符）")
                continue
            left, right = cells[0], cells[1]
            # 列序自动判断：手机导出的 CSV 常见「姓名,号码」
            if _looks_like_number(left):
                num, nm = left, right
            elif _looks_like_number(right):
                num, nm = right, left
            else:
                # 都不像号码 —— 首行可能是表头，其余行记警告
                if idx == 0:
                    continue
                if len(warns) < SAMPLE_LIMIT:
                    warns.append(f"跳过无法识别号码的行：{left},{right}")
                continue
            pairs.append((normalize_number(num), nm))
        _collect(pairs)

    # 去重（同一号码保留最后一条），并限制总量
    dedup: Dict[str, str] = {}
    for number, name in rows:
        dedup[number] = name
    out = list(dedup.items())
    if len(out) > MAX_IMPORT_ROWS:
        warns.append(f"仅导入前 {MAX_IMPORT_ROWS} 条（共 {len(out)} 条）")
        out = out[:MAX_IMPORT_ROWS]
    return out, warns


# ---------------------------------------------------------------- 写入
def import_contacts(
    db_path: str,
    text: str,
    fmt: str = "auto",
    mode: str = "merge",
    now: str = "",
    source: str = "",
) -> Dict[str, Any]:
    """导入通讯录。

    `mode="merge"`（默认）按号码 upsert —— 已有号码**更新**姓名；
    `mode="replace"` 先清空再导入。
    返回 {imported, updated, inserted, skipped, total, warnings}。
    """
    pairs, warns = parse_import(text, fmt)
    if not pairs:
        return {
            "imported": 0, "inserted": 0, "updated": 0, "skipped": 0,
            "total": 0, "warnings": warns, "error": "没有解析出可用记录",
        }

    conn = sqlite3.connect(db_path)
    try:
        ensure_table(conn)
        if mode == "replace":
            conn.execute(f"DELETE FROM {TABLE_COMM_CONTACTS}")

        # 判重按**归一化后**的号码；同时记下 rowid —— 因为库里那行存的可能是
        # `+86 138-0013-8000` 这类原始写法，拿规范化号码去 `WHERE number = ?` 匹配不到，
        # 会静默更新 0 行（看起来"导入成功"，其实什么都没改）。
        existing: Dict[str, tuple] = {}
        for rid, raw_num, raw_name in conn.execute(
            f"SELECT rowid, number, name FROM {TABLE_COMM_CONTACTS}"
        ):
            key = normalize_number(raw_num)
            if key:
                existing[key] = (rid, raw_name)
        inserted = updated = 0
        for number, name in pairs:
            if number in existing:
                rid, old_name = existing[number]
                if old_name != name:
                    updated += 1
                conn.execute(
                    f"UPDATE {TABLE_COMM_CONTACTS} SET number = ?, name = ?, raw = ?, "
                    f"source = ?, updated_at = ? WHERE rowid = ?",
                    (number, name, name, source, now, rid),
                )
            else:
                inserted += 1
                conn.execute(
                    f"INSERT INTO {TABLE_COMM_CONTACTS} "
                    f"(number, name, raw, source, created_at, updated_at) "
                    f"VALUES (?, ?, ?, ?, ?, ?)",
                    (number, name, name, source, now, now),
                )
        conn.commit()
        total = int(
            conn.execute(f"SELECT COUNT(*) FROM {TABLE_COMM_CONTACTS}").fetchone()[0]
        )
    finally:
        conn.close()

    invalidate_cache()
    _LOGGER.info(
        "[comm] 通讯录导入：新增 %s 条、更新 %s 条，当前共 %s 条",
        inserted, updated, total,
    )
    return {
        "imported": inserted + updated,
        "inserted": inserted,
        "updated": updated,
        "skipped": 0,
        "total": total,
        "warnings": warns,
        "error": "",
    }


# ---------------------------------------------------------------- 查询
_CACHE_LOCK = threading.Lock()
_CACHE: Dict[str, Any] = {"path": None, "map": None, "fp": None, "ts": 0.0}
# 缓存最长存活时间（秒）：即使指纹没变也会在这么久后重读一次。
# 兜底场景 —— 外部工具直接改了姓名、却没动 updated_at，指纹察觉不到。
_CACHE_TTL = 60.0


def invalidate_cache() -> None:
    """使缓存失效（导入 / 清空 / 删除后调用）。"""
    with _CACHE_LOCK:
        _CACHE["path"] = None
        _CACHE["map"] = None
        _CACHE["fp"] = None
        _CACHE["ts"] = 0.0


def _fingerprint(conn: sqlite3.Connection) -> str:
    """通讯录的廉价指纹（行数 + 最大更新时间）。

    缓存只按 `db_path` 判断是不够的：如果通讯录是**在别处**改的
    （外部工具 / 直接写库 / 另一个进程），`invalidate_cache()` 不会被调用，
    采集侧就会一直用旧快照 —— 表现为「明明导入过，采集时却匹配不到姓名」。
    一次 `COUNT + MAX` 很便宜，足够发现外部改动。
    """
    try:
        if not _table_exists(conn):
            return "absent"
        row = conn.execute(
            f"SELECT COUNT(*), COALESCE(MAX(updated_at), '') FROM {TABLE_COMM_CONTACTS}"
        ).fetchone()
        return f"{row[0]}:{row[1]}"
    except Exception:  # noqa: BLE001
        return ""


def load_names(db_path: str, force: bool = False) -> Dict[str, str]:
    """加载「号码 → 姓名」映射（带缓存；表不存在时返回空 dict）。

    采集时**每行**都要查姓名，逐行查库太慢，因此整个通讯录一次性读进内存。
    通讯录通常几百到几千条，内存开销可以忽略。

    缓存会比对**指纹**（行数 + 最大 `updated_at`），所以即使绕过本模块
    直接改了库（或由别的进程导入），下一次采集也能自动发现并重载。
    """
    now = time.monotonic()
    with _CACHE_LOCK:
        cached_fp = _CACHE.get("fp")
        fresh = (now - float(_CACHE.get("ts") or 0.0)) < _CACHE_TTL
        if (not force and _CACHE["path"] == db_path
                and _CACHE["map"] is not None and fresh):
            cached_map = _CACHE["map"]
        else:
            cached_fp = None          # 过期 → 强制比对指纹并重读
            cached_map = None

    mapping: Dict[str, str] = {}
    fp = ""
    try:
        conn = sqlite3.connect(db_path)
        try:
            fp = _fingerprint(conn)
            if cached_map is not None and cached_fp == fp:
                # 指纹一致 → 复用缓存，并刷新存活时间（省掉一次全表读）
                with _CACHE_LOCK:
                    _CACHE["ts"] = now
                return cached_map
            if fp and fp != "absent":
                for number, name in conn.execute(
                    f"SELECT number, name FROM {TABLE_COMM_CONTACTS}"
                ):
                    # **读取时也要归一化**：界面导入的号码已归一化，但若是外部
                    # 直接写进本表（腾讯 / 微信 / 其它工具导出），存进来的可能是
                    # `+86 138-0013-8000` 这类原始写法 —— 不归一化就永远匹配不上。
                    num = normalize_number(number)
                    nm = str(name or "").strip()
                    if num and nm:
                        # 同一号码多种写法时，以表中靠后的记录为准（与导入行为一致）
                        mapping[num] = nm
        finally:
            conn.close()
    except Exception as err:  # noqa: BLE001 - 通讯录不可用不应影响采集
        _LOGGER.debug("读取通讯录失败（按空处理）: %s", err)
        mapping = {}
    with _CACHE_LOCK:
        _CACHE["path"] = db_path
        _CACHE["map"] = mapping
        _CACHE["fp"] = fp
        _CACHE["ts"] = now
    return mapping


def name_of(number: Any, db_path: str = "") -> str:
    """按号码查姓名；查不到返回空串。"""
    if not db_path:
        return ""
    num = normalize_number(number)
    if not num:
        return ""
    return load_names(db_path).get(num, "")


def stats(db_path: str, sample: bool = True) -> Dict[str, Any]:
    """通讯录统计（供前端展示）。"""
    try:
        conn = sqlite3.connect(db_path)
        try:
            if not _table_exists(conn):
                return {"success": True, "exists": False, "total": 0, "samples": []}
            total = int(
                conn.execute(f"SELECT COUNT(*) FROM {TABLE_COMM_CONTACTS}").fetchone()[0]
            )
            samples = []
            if sample and total:
                samples = [
                    {"number": r[0], "name": r[1]}
                    for r in conn.execute(
                        f"SELECT number, name FROM {TABLE_COMM_CONTACTS} "
                        f"ORDER BY id DESC LIMIT ?",
                        (SAMPLE_LIMIT,),
                    )
                ]
            return {"success": True, "exists": True, "total": total, "samples": samples}
        finally:
            conn.close()
    except Exception as err:  # noqa: BLE001
        return {"success": False, "error": str(err)}


def clear(db_path: str) -> int:
    """清空通讯录，返回删除条数。"""
    conn = sqlite3.connect(db_path)
    try:
        if not _table_exists(conn):
            return 0
        n = int(conn.execute(f"SELECT COUNT(*) FROM {TABLE_COMM_CONTACTS}").fetchone()[0])
        conn.execute(f"DELETE FROM {TABLE_COMM_CONTACTS}")
        conn.commit()
    finally:
        conn.close()
    invalidate_cache()
    _LOGGER.info("[comm] 通讯录已清空，删除 %s 条", n)
    return n


def delete_number(db_path: str, number: Any) -> int:
    """删除单个号码，返回删除条数。"""
    num = normalize_number(number)
    if not num:
        return 0
    conn = sqlite3.connect(db_path)
    try:
        ensure_table(conn)
        cur = conn.execute(
            f"DELETE FROM {TABLE_COMM_CONTACTS} WHERE number = ?", (num,)
        )
        conn.commit()
        deleted = int(cur.rowcount or 0)
    finally:
        conn.close()
    invalidate_cache()
    return deleted
