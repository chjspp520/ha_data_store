# -*- coding: utf-8 -*-
"""通讯表字段回填：用本地归属地库、城市坐标表与用户通讯录补全空值。

回填的 5 个字段（顺序即处理顺序——先补归属地/姓名，再据归属地推坐标）：

======================  ====================================================
字段                    来源
======================  ====================================================
`party_place`           `party_number` 查本地归属地库（**只要市，不要省**；市缺失才退回省）
`party_isp`             `party_number` 查本地归属地库（标准化运营商名）
`party_name`            `party_number` 查**用户导入的通讯录**（`comm_contacts` 表）
`party_coordinate`      `party_place` 查本地城市坐标表（`"经度,纬度"`）
`location_coordinate`   `location`    查本地城市坐标表
======================  ====================================================

两条使用路径**共用同一个填充器**（`CommFiller`），保证行为完全一致：

1. **采集时自动回填**（`__init__._attr_collect_for_entity`）：通讯模式每写一行前调用，
   新数据进来就自动补全，用户不必事后手动跑按钮；
2. **手动回填**（`backfill`，由「📇 自动填写」按钮触发）：对**历史**数据补全。

设计要点：

- **归属地不带省份**（`merge_place`）：归属地库给的是 `province='陕西'` + `city='西安'`，
  早先拼成 `陕西西安` —— 省没有信息量，更要紧的是**同一个城市会分裂成两种写法**：
  库里原本多是 `西安`，回填后变成 `陕西西安`，于是筛选、配色、地图上的地点各算两个。
  现在只取市（并过一遍 `city_geo.normalize_place` 去掉「市」这类后缀）。
- **只填空值**（默认）：已有内容的字段不动，因此可以反复、分批执行；
  唯一的例外是「旧值只是带省的同义写法」（`陕西西安` → `西安`）—— 那种也会被纠正，
  否则以前回填进去的带省串在「只填空值」模式下永远留在库里（见 `place_is_padded`）。
- **号码查不到姓名时不动**：通讯录里没有该号码就留空（`no_contact` 计数），
  不会把已有姓名抹掉。
- **不联网**：全部查询走本地 `data/phone2region.zdb`、`data/city_coordinates.json`
  与本地 `comm_contacts` 表；
- 号码查不到归属地（服务号 / 短号 / 未收录号段）时保留原值并计入 `no_region`；
- 支持 `dry_run` 预览（只统计、不写库）与 `limit` 分批（大表保护）。
"""
import logging
import sqlite3
from typing import Any, Dict, Iterable, List, Optional, Set

from . import city_geo, comm_contacts
from .const import get_attr_table_name
from .phone_region import get_index

_LOGGER = logging.getLogger(__name__)

# 可回填的字段（顺序即处理顺序）
BACKFILL_COLUMNS = ("party_place", "party_isp", "party_name",
                    "location_coordinate", "party_coordinate")

# 预览时最多返回多少条样例
SAMPLE_LIMIT = 20
# 通讯录匹配不到的号码，最多记几条（仅供日志 / 返回体诊断用）
MISSED_SAMPLE_LIMIT = 10


def _is_blank(value: Any) -> bool:
    """NULL / 空串 / 纯空白都算「空」。"""
    return value is None or str(value).strip() == ""


def _get(row: Any, col: str, default: Any = "") -> Any:
    """从 dict / sqlite3.Row 里安全取值（列不存在时返回 default）。"""
    try:
        if isinstance(row, sqlite3.Row):
            return row[col] if col in row.keys() else default
        return row.get(col, default)
    except Exception:  # noqa: BLE001
        return default


def merge_place(province: Any, city: Any) -> str:
    """归属地显示文本 —— **只要市，不要省**（用户要求："回填的数据带有省份，能不能不要省"）。

    为什么不要省：
        · "省"几乎没有信息量（一看城市就知道在哪个省），却让每一行都变长；
        · 更要紧的是**同一个城市会分裂成两种写法** —— 库里原本多半是「西安」，
          回填写进去却是「陕西省西安市」，于是筛选、配色、地图上的地点各算两个，
          明细里看着也像两个地方。

    市缺失时退回省（有些号段只给到省一级，总比空着强）；最后统一过一遍
    `city_geo.normalize_place` 去掉后缀（「西安市」→「西安」、「巴音郭楞蒙古自治州」→「巴音郭楞」），
    这样与库里的常见写法、以及坐标表的键都对得上。
    """
    province, city = str(province or "").strip(), str(city or "").strip()
    text = city or province
    if not text:
        return ""
    return city_geo.normalize_place(text) or text


def place_is_padded(old: Any, new: Any) -> bool:
    """旧值是不是「新值前面多挂了一级省名」（`陕西省西安市` / `陕西西安` vs `西安`）。

    用于**修正历史数据**：以前回填写进去的带省串，在「只填空值」模式下永远不会被改写，
    就一直躺在库里。但它和新值指向的是**同一个地方**，纠正它不算覆盖用户填的内容。

    判据保守（**宁可漏改也不误改**）：两边都先 `normalize_place`（否则 `陕西省西安市`
    的结尾是 `西安市`、`endswith('西安')` 会不成立），然后要求 `old` 以 `new` 结尾且更长，
    多出来的那截前缀长度 ≤ 8（省一级最长也就「新疆维吾尔自治区」这个量级），
    且归一化后**不包含** `new` 本身。
    """
    o, n = str(old or "").strip(), str(new or "").strip()
    if not o or not n:
        return False
    o_n, n_n = city_geo.normalize_place(o), city_geo.normalize_place(n)
    if not o_n or not n_n or len(o_n) <= len(n_n) or not o_n.endswith(n_n):
        return False
    prefix = o_n[: len(o_n) - len(n_n)]
    if not prefix or len(prefix) > 8:
        return False
    return n_n not in city_geo.normalize_place(prefix)


class CommFiller:
    """通讯字段填充器 —— 采集与手动回填共用同一份判定逻辑。

    构造时把三份数据源准备好（归属地索引 / 坐标表是否可用 / 通讯录映射），
    之后 `fill()` 对每一行返回 `{列名: 新值}`（只含**确实需要写入**的字段）。
    """

    def __init__(
        self,
        index,
        coords_ready: bool,
        contacts: Optional[Dict[str, str]] = None,
        only_empty: bool = True,
        table_cols: Optional[Iterable[str]] = None,
        only_columns: Optional[Iterable[str]] = None,
    ) -> None:
        self.index = index
        self.coords_ready = bool(coords_ready)
        self.contacts: Dict[str, str] = contacts or {}
        self.only_empty = bool(only_empty)
        self.table_cols: Optional[Set[str]] = set(table_cols) if table_cols else None
        # 只回填这几列（None = 全部）。用于「按通讯录回填姓名」这类单一用途的按钮。
        self.only_columns: Optional[Set[str]] = (
            set(only_columns) if only_columns else None
        )
        self.stats: Dict[str, int] = {
            "no_region": 0,   # 号码查不到归属地
            "no_coord": 0,    # 地名查不到坐标
            "no_contact": 0,  # 通讯录里没有该号码
        }
        # 通讯录匹配不到的号码样例（归一化后），用于回答「为什么没补上姓名」
        self.missed_numbers: List[str] = []

    # ------------------------------------------------------------ 内部
    def _has(self, row: Any, col: str) -> bool:
        """该列是否参与回填（表里得真有这一列；若限定了列，还必须在限定范围内）。"""
        if self.only_columns is not None and col not in self.only_columns:
            return False
        if self.table_cols is not None:
            return col in self.table_cols
        if isinstance(row, dict):
            # row 为 dict 时视为「键可能不全」，不因缺失而排除该列
            return True
        try:
            return col in row.keys()
        except Exception:  # noqa: BLE001
            return False

    def _should_fill(self, current: Any) -> bool:
        return (not self.only_empty) or _is_blank(current)

    # ------------------------------------------------------------ 主体
    def fill(self, row: Any) -> Dict[str, str]:
        """算出该行需要补写的字段；返回 {列名: 新值}（无改动时为空 dict）。"""
        changes: Dict[str, str] = {}

        number = str(_get(row, "party_number") or "")
        place_old = str(_get(row, "party_place") or "")
        location = str(_get(row, "location") or "")

        # ── 1) 号码 → 归属地 / 运营商 / 姓名 ──
        info = None
        if number and self.index is not None:
            try:
                info = self.index.query(number)
            except Exception as err:  # noqa: BLE001 - 单行查询失败不应中断采集
                _LOGGER.debug("归属地查询失败 %s: %s", number, err)
                info = None
        if number and info is None:
            self.stats["no_region"] += 1

        place_new = place_old
        if info is not None and self._has(row, "party_place"):
            candidate = merge_place(info.get("province"), info.get("city"))
            # 三种情况都写：
            #   ① 允许覆盖（`only_empty=False`）
            #   ② 旧值为空（正常回填）
            #   ③ 旧值只是**带省的同义写法**（`陕西省西安市` → `西安`）—— 即使开着
            #      「只填空值」也要纠正，否则以前填进去的带省串会永远留在库里
            if candidate and (
                (not self.only_empty)
                or _is_blank(place_old)
                or place_is_padded(place_old, candidate)
            ):
                if candidate != place_old:
                    changes["party_place"] = candidate
                    place_new = candidate

        if info is not None and self._has(row, "party_isp"):
            current_isp = _get(row, "party_isp")
            if self._should_fill(current_isp):
                isp = str(info.get("isp") or "")
                if isp and isp != str(current_isp or ""):
                    changes["party_isp"] = isp

        # 姓名：只能来自用户通讯录
        if self._has(row, "party_name"):
            current_name = _get(row, "party_name")
            if self._should_fill(current_name):
                name = ""
                if number and self.contacts:
                    name = self.contacts.get(
                        comm_contacts.normalize_number(number), ""
                    )
                if name:
                    changes["party_name"] = name
                elif number:
                    self.stats["no_contact"] += 1
                    # 记下前几个匹配不到的号码，便于对账（只在日志里，不写库）
                    if len(self.missed_numbers) < MISSED_SAMPLE_LIMIT:
                        self.missed_numbers.append(
                            comm_contacts.normalize_number(number)
                        )

        # ── 2) 地名 → 坐标 ──
        if self.coords_ready and self._has(row, "party_coordinate"):
            current = _get(row, "party_coordinate")
            if self._should_fill(current) and place_new:
                coord = city_geo.coordinate_text(place_new)
                if coord:
                    if coord != str(current or ""):
                        changes["party_coordinate"] = coord
                else:
                    self.stats["no_coord"] += 1

        if self.coords_ready and self._has(row, "location_coordinate"):
            current = _get(row, "location_coordinate")
            if self._should_fill(current) and location:
                coord = city_geo.coordinate_text(location)
                if coord:
                    if coord != str(current or ""):
                        changes["location_coordinate"] = coord
                else:
                    self.stats["no_coord"] += 1

        return changes


def build_filler(
    db_path: str,
    only_empty: bool = True,
    need_region: bool = True,
    need_coords: bool = True,
    need_contacts: bool = True,
    table_cols: Optional[Iterable[str]] = None,
    only_columns: Optional[Iterable[str]] = None,
) -> CommFiller:
    """按需准备数据源并构造填充器（采集时可直接调用，内部有缓存）。"""
    index = get_index() if need_region else None
    coords_ready = False
    if need_coords:
        try:
            coords_ready = bool(city_geo.load_coordinates())
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("城市坐标表不可用（将跳过坐标回填）: %s", exc)
    contacts: Dict[str, str] = {}
    if need_contacts:
        try:
            contacts = comm_contacts.load_names(db_path)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("通讯录不可用（将跳过姓名回填）: %s", exc)
    return CommFiller(
        index=index,
        coords_ready=coords_ready,
        contacts=contacts,
        only_empty=only_empty,
        table_cols=table_cols,
        only_columns=only_columns,
    )


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
        contacts = comm_contacts.stats(db_path, sample=False)
        return {
            "success": True,
            "type_name": name,
            "table": tbl,
            "columns": active,
            "total": total,
            "pending": pending,
            "blanks": blanks,
            "contacts_total": int(contacts.get("total") or 0),
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
    columns: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """回填通讯表里为空的归属地 / 运营商 / 姓名 / 坐标字段。

    `only_empty=True`（默认）只填空白字段，不覆盖已有内容；
    `dry_run=True` 只统计不写库（预览）；
    `limit>0` 时最多处理这么多行（按 id 升序，便于分批反复执行）。

    说明：**采集时也会自动做同样的回填**（见 `CommFiller`），所以本函数主要用于
    给**历史数据**补全。
    """
    name, tbl = resolve_comm_table(db_path, type_name)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        if not _table_exists(conn, tbl):
            raise ValueError(f"未找到通讯数据表 {tbl}")

        cols = _table_columns(conn, tbl)
        # `columns` 可限定只处理其中几列（如「按通讯录回填姓名」只跑 party_name）
        wanted = set(columns) if columns else None
        active = [c for c in BACKFILL_COLUMNS
                  if c in cols and (wanted is None or c in wanted)]
        if not active:
            raise ValueError(
                f"{tbl} 中找不到可回填的字段："
                + ("、".join(sorted(wanted)) if wanted else "、".join(BACKFILL_COLUMNS))
            )

        filler = build_filler(
            db_path, only_empty=only_empty, table_cols=cols, only_columns=wanted
        )

        stats: Dict[str, Any] = {
            "success": True,
            "type_name": name,
            "table": tbl,
            "dry_run": dry_run,
            "only_empty": only_empty,
            "region_ready": filler.index is not None,
            "coords_ready": filler.coords_ready,
            "contacts_total": len(filler.contacts),
            "scanned": 0,
            "updated": 0,
            "filled": {c: 0 for c in BACKFILL_COLUMNS},
            "no_region": 0,
            "no_coord": 0,
            "no_contact": 0,
            "samples": [],
        }

        select_cols = [
            c for c in ("id", "party_number", "party_place", "party_isp", "party_name",
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
            number = str(_get(row, "party_number") or "")
            changes = filler.fill(row)
            if not changes:
                continue

            stats["updated"] += 1
            for col in changes:
                stats["filled"][col] += 1
            if len(stats["samples"]) < SAMPLE_LIMIT:
                stats["samples"].append({"id": row_id, "party_number": number, **changes})

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

        stats["no_region"] = filler.stats["no_region"]
        stats["no_coord"] = filler.stats["no_coord"]
        stats["no_contact"] = filler.stats["no_contact"]
        # 匹配不到的号码样例：回答「为什么没补上姓名」
        stats["missed_numbers"] = filler.missed_numbers
        stats["missed_numbers"] = filler.missed_numbers
        stats["filled"] = {k: v for k, v in stats["filled"].items() if v}
        return stats
    finally:
        conn.close()
