# -*- coding: utf-8 -*-
"""通讯表字段回填：用本地归属地库与城市坐标表补全空值。

回填的 4 个字段（顺序即处理顺序——先补归属地，再据它推坐标）：

======================  ====================================================
字段                    来源
======================  ====================================================
`party_place`           `party_number` 查本地归属地库（省+市，省市同名只留一个）
`party_isp`             `party_number` 查本地归属地库（标准化运营商名）
`party_coordinate`      `party_place` 查本地城市坐标表（`"经度,纬度"`）
`location_coordinate`   `location`    查本地城市坐标表
======================  ====================================================

设计要点：

- **只填空值**（默认）：已有内容的字段不动，因此可以反复、分批执行；
- **不联网**：全部查询走本地 `data/phone2region.zdb` 与 `data/city_coordinates.json`；
- 号码查不到（服务号 / 短号 / 未收录号段）时保留原值并计入 `no_region`；
- 支持 `dry_run` 预览（只统计、不写库）与 `limit` 分批（大表保护）。
"""
import logging
import sqlite3
from typing import Any, Dict, List, Optional

from . import city_geo
from .const import get_attr_table_name
from .phone_region import get_index

_LOGGER = logging.getLogger(__name__)

# 可回填的字段（顺序即处理顺序）
BACKFILL_COLUMNS = ("party_place", "party_isp", "location_coordinate", "party_coordinate")

# 预览时最多返回多少条样例
SAMPLE_LIMIT = 20


def _is_blank(value: Any) -> bool:
    """NULL / 空串 / 纯空白都算「空」。"""
    return value is None or str(value).strip() == ""


def merge_place(province: Any, city: Any) -> str:
    """归属地显示文本：省市相同时只留一个（北京|北京 -> 北京）。"""
    province, city = str(province or "").strip(), str(city or "").strip()
    if province and city:
        return province if province == city else f"{province}{city}"
    return province or city


def resolve_comm_table(db_path: str, type_name: str = "") -> tuple[str, str]:
    """确定要回填的通讯表；返回 (类型名, 表名)。

    `type_name` 为空时复用 `comm.resolve_comm_type_name` 的自动探测
    （取 `attr_type_defs` 中 `mode=comm` 且数据表已存在者）。
    """
    from .comm import resolve_comm_type_name  # 延迟导入，避免循环依赖

    name = resolve_comm_type_name(db_path, type_name)
    return name, get_attr_table_name(name)


def _table_exists(conn: sqlite3.Connection, tbl: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (tbl,)
        ).fetchone()
    )


def _table_columns(conn: sqlite3.Connection, tbl: str) -> set:
    return {row[1] for row in conn.execute(f'PRAGMA table_info("{tbl}")')}


def table_status(db_path: str, type_name: str = "") -> Dict[str, Any]:
    """统计通讯表的待回填情况（供前端展示「自动填写」按钮的提示）。"""
    try:
        name, tbl = resolve_comm_table(db_path, type_name)
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "error": str(exc)}

    conn = sqlite3.connect(db_path)
    try:
        if not _table_exists(conn, tbl):
            return {"success": False, "error": f"未找到通讯数据表 {tbl}"}
        cols = _table_columns(conn, tbl)
        active = [c for c in BACKFILL_COLUMNS if c in cols]
        total = int(conn.execute(f'SELECT COUNT(*) FROM "{tbl}"').fetchone()[0])
        blanks = {}
        for col in active:
            blanks[col] = int(
                conn.execute(
                    f'SELECT COUNT(*) FROM "{tbl}" '
                    f'WHERE {col} IS NULL OR TRIM(CAST({col} AS TEXT)) = \'\''
                ).fetchone()[0]
            )
        # 至少有一个待填字段的行数
        if active:
            cond = " OR ".join(
                f"{c} IS NULL OR TRIM(CAST({c} AS TEXT)) = ''" for c in active
            )
            pending = int(
                conn.execute(f'SELECT COUNT(*) FROM "{tbl}" WHERE {cond}').fetchone()[0]
            )
        else:
            pending = 0
        return {
            "success": True,
            "type_name": name,
            "table": tbl,
            "columns": active,
            "total": total,
            "pending": pending,
            "blanks": blanks,
        }
    except sqlite3.OperationalError as exc:
        return {"success": False, "error": f"读取通讯表失败: {exc}"}
    finally:
        conn.close()


def backfill(
    db_path: str,
    type_name: str = "",
    only_empty: bool = True,
    limit: int = 0,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """回填通讯表里为空的归属地 / 运营商 / 坐标字段。

    `only_empty=True`（默认）只填空白字段，不覆盖已有内容；
    `dry_run=True` 只统计不写库（预览）；
    `limit>0` 时最多处理这么多行（按 id 升序，便于分批反复执行）。
    """
    index = get_index()
    if index is None:
        raise ValueError(
            "归属地库不可用：请确认 data/phone2region.zdb 存在且未损坏"
        )
    try:
        coords_ready = bool(city_geo.load_coordinates())
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("城市坐标表不可用（将跳过坐标回填）: %s", exc)
        coords_ready = False

    name, tbl = resolve_comm_table(db_path, type_name)

    stats: Dict[str, Any] = {
        "success": True,
        "type_name": name,
        "table": tbl,
        "dry_run": dry_run,
        "only_empty": only_empty,
        "coords_ready": coords_ready,
        "scanned": 0,
        "updated": 0,
        "filled": {c: 0 for c in BACKFILL_COLUMNS},
        "no_region": 0,   # 号码查不到归属地
        "no_coord": 0,    # 地名查不到坐标
        "samples": [],
    }

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        if not _table_exists(conn, tbl):
            raise ValueError(f"未找到通讯数据表 {tbl}")

        cols = _table_columns(conn, tbl)
        active = [c for c in BACKFILL_COLUMNS if c in cols]
        if not active:
            raise ValueError(
                f"{tbl} 中找不到可回填的字段：{'、'.join(BACKFILL_COLUMNS)}"
            )

        select_cols = [
            c for c in ("id", "party_number", "party_place", "party_isp",
                        "location", "location_coordinate", "party_coordinate")
            if c in cols
        ]
        if "id" not in select_cols:
            raise ValueError(f"{tbl} 缺少 id 列，无法定位待更新行")

        pending_cond = (
            "(" + " OR ".join(
                f"{c} IS NULL OR TRIM(CAST({c} AS TEXT)) = ''" for c in active
            ) + ")"
        )
        sql = (
            f'SELECT {", ".join(select_cols)} FROM "{tbl}" '
            f"WHERE {pending_cond} ORDER BY id ASC"
        )
        params: List[Any] = []
        if limit and limit > 0:
            sql += " LIMIT ?"
            params.append(int(limit))

        rows = conn.execute(sql, params).fetchall()
        stats["scanned"] = len(rows)

        updates: List[tuple] = []   # (sql, args)
        for row in rows:
            row_id = row["id"]
            number = str(row["party_number"] or "") if "party_number" in cols else ""
            place_old = str(row["party_place"] or "") if "party_place" in cols else ""
            location = str(row["location"] or "") if "location" in cols else ""

            changes: Dict[str, str] = {}

            # ── 1) 归属地 / 运营商：由号码查库 ──
            info = None
            if number:
                info = index.query(number)
            if info is None and number:
                stats["no_region"] += 1

            place_new = place_old
            if "party_place" in active and (not only_empty or _is_blank(place_old)):
                if info:
                    place_new = merge_place(info.get("province"), info.get("city"))
                    if place_new and place_new != place_old:
                        changes["party_place"] = place_new

            if "party_isp" in active:
                current_isp = row["party_isp"] if "party_isp" in cols else ""
                if (not only_empty or _is_blank(current_isp)) and info:
                    isp = str(info.get("isp") or "")
                    if isp and isp != str(current_isp or ""):
                        changes["party_isp"] = isp

            # ── 2) 坐标：由地名查坐标表 ──
            if coords_ready and "party_coordinate" in active:
                current = row["party_coordinate"] if "party_coordinate" in cols else ""
                if not only_empty or _is_blank(current):
                    if place_new:
                        coord = city_geo.coordinate_text(place_new)
                        if coord:
                            if coord != str(current or ""):
                                changes["party_coordinate"] = coord
                        else:
                            stats["no_coord"] += 1

            if coords_ready and "location_coordinate" in active:
                current = row["location_coordinate"] if "location_coordinate" in cols else ""
                if not only_empty or _is_blank(current):
                    if location:
                        coord = city_geo.coordinate_text(location)
                        if coord:
                            if coord != str(current or ""):
                                changes["location_coordinate"] = coord
                        else:
                            stats["no_coord"] += 1

            if not changes:
                continue

            stats["updated"] += 1
            for col in changes:
                stats["filled"][col] += 1
            if len(stats["samples"]) < SAMPLE_LIMIT:
                sample = {"id": row_id, "party_number": number, **changes}
                stats["samples"].append(sample)

            if not dry_run:
                set_sql = ", ".join(f'"{c}" = ?' for c in changes)
                updates.append(
                    (f'UPDATE "{tbl}" SET {set_sql} WHERE id = ?',
                     [*changes.values(), row_id])
                )

        if updates and not dry_run:
            # 各行的待更新列不同，SQL 不能复用，逐条执行（同一事务内，开销可忽略）
            for update_sql, update_args in updates:
                conn.execute(update_sql, update_args)
            conn.commit()

        stats["filled"] = {k: v for k, v in stats["filled"].items() if v}
        return stats
    finally:
        conn.close()
