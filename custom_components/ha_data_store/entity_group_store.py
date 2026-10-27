"""实体编组（Entity Groups）存储层。

设计原则
--------
1. **纯 sqlite，不依赖 HomeAssistant**：可在临时库上离线单测。
2. **两层 CRUD**：编组（entity_groups）+ 成员（entity_group_members）。
3. **version 单调自增**：任何写操作在同一事务里 +1，前端据此判断是否需要重建。
4. **成员变更级联父组**：成员的增删改都会让父组 rev +1、updated_at 更新，
   这样用 rev 做乐观锁时能检出"成员在此期间被别人改过"。
5. **不做动作执行**：动作展开与调用交给 entity_group_actions.py，
   最终复用 automations.py 的 AutomationManager。

数据形状（与 API / sensor 属性完全一致）
----------------------------------------
group = {
    "id": "eg_a1b2c3", "name": "客厅主灯", "kind": "lights",
    "icon": None, "icon_on": None, "icon_off": None,
    "color_on": None, "color_off": None,
    "options": {}, "actions": [], "default_on_action": None,
    "default_off_action": None, "sort": 10, "enabled": True, "rev": 1,
    "created_at": 1760800000000, "updated_at": 1760800500000,
    "members": [
        {"entity_id": "light.x", "name": None, "icon": None, "sort": 0, "extra": {}},
    ],
}
"""
from __future__ import annotations

import json
import logging
import random
import re
import sqlite3
import string
import time
from typing import Any, Iterable

from .const import (
    ENTITY_GROUP_DOMAIN_SERVICE_MAP,
    ENTITY_GROUP_KINDS,
    ENTITY_GROUP_MEMBER_OVERRIDE_KEYS,
    ENTITY_GROUP_MEMBER_PARAM_KEYS,
    TABLE_ENTITY_GROUP_MEMBERS,
    TABLE_ENTITY_GROUPS,
    TABLE_ENTITY_GROUPS_META,
)

_LOGGER = logging.getLogger(__name__)

# ── 校验常量 ────────────────────────────────────────────────────────────────
# 编组 id 可自定义（决定 switch 实体 id：switch.hds_eg_<id 去掉 eg_ 前缀>）。
# 限制：必须以 eg_ 开头、首字符为小写字母或数字、之后允许小写字母/数字/下划线，
# 总长 3~32，这样拼出来的 entity_id 一定合法。
GROUP_ID_RE = re.compile(r"^eg_[a-z0-9][a-z0-9_]{1,31}$")
ENTITY_ID_RE = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")
ACTION_ID_RE = re.compile(r"^[a-z0-9_]{1,24}$")

MEMBER_LIMIT = 200          # 单编组成员上限
ACTION_LIMIT = 12           # 单编组预设动作上限
ACTION_STEP_LIMIT = 20      # 单个预设动作的行为条目上限
NAME_MAX = 50
TEXT_MAX = 200              # icon/color 等短文本上限

_SORT_FIELDS = {
    "sort": "g.sort ASC, g.id ASC",
    "name": "g.name ASC, g.id ASC",
    "updated_at": "g.updated_at DESC, g.id ASC",
}

_PARAM_KEYS = set(ENTITY_GROUP_MEMBER_PARAM_KEYS)
_OVERRIDE_KEYS = set(ENTITY_GROUP_MEMBER_OVERRIDE_KEYS)


# ── 异常 ────────────────────────────────────────────────────────────────────
class EntityGroupError(Exception):
    """带错误码的校验/业务异常，由 HTTP 层转换为对应状态码。"""

    def __init__(self, error: str, detail: str = "", status: int = 400, **extra: Any) -> None:
        super().__init__(detail or error)
        self.error = error
        self.detail = detail
        self.status = status
        self.extra = extra

    def to_dict(self) -> dict:
        data = {"ok": False, "error": self.error, "detail": self.detail}
        data.update(self.extra)
        return data


def _now_ms() -> int:
    return int(time.time() * 1000)


def _gen_group_id() -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "eg_" + "".join(random.choices(alphabet, k=8))


def _loads(raw: Any, default: Any) -> Any:
    """安全解析 JSON 字段：已是目标类型则直接返回。"""
    if isinstance(raw, (dict, list)):
        return raw
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _clean_text(value: Any, limit: int = TEXT_MAX) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise EntityGroupError("invalid_param", f"文本字段类型错误: {value!r}")
    value = value.strip()
    if not value:
        return None
    return value[:limit]


def _clean_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class EntityGroupStore:
    """实体编组存储：建表 + 两层 CRUD + 版本号。"""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    # ── 基础设施 ────────────────────────────────────────────────────────────
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        # 级联删除依赖它，必须每个连接单独开
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def ensure_schema(self) -> None:
        """建表（幂等）。启动时调用一次。"""
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {TABLE_ENTITY_GROUPS} (
                        id                 TEXT PRIMARY KEY,
                        name               TEXT NOT NULL,
                        kind               TEXT NOT NULL DEFAULT 'mixed',
                        icon               TEXT,
                        icon_on            TEXT,
                        icon_off           TEXT,
                        color_on           TEXT,
                        color_off          TEXT,
                        options            TEXT NOT NULL DEFAULT '{{}}',
                        actions            TEXT NOT NULL DEFAULT '[]',
                        default_on_action  TEXT,
                        default_off_action TEXT,
                        sort               INTEGER NOT NULL DEFAULT 0,
                        enabled            INTEGER NOT NULL DEFAULT 1,
                        rev                INTEGER NOT NULL DEFAULT 1,
                        created_at         INTEGER NOT NULL DEFAULT 0,
                        updated_at         INTEGER NOT NULL DEFAULT 0
                    )
                    """
                )
                conn.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {TABLE_ENTITY_GROUP_MEMBERS} (
                        group_id  TEXT NOT NULL,
                        entity_id TEXT NOT NULL,
                        name      TEXT,
                        icon      TEXT,
                        sort      INTEGER NOT NULL DEFAULT 0,
                        extra     TEXT NOT NULL DEFAULT '{{}}',
                        PRIMARY KEY (group_id, entity_id),
                        FOREIGN KEY (group_id) REFERENCES {TABLE_ENTITY_GROUPS}(id)
                            ON DELETE CASCADE
                    )
                    """
                )
                conn.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {TABLE_ENTITY_GROUPS_META} (
                        key   TEXT PRIMARY KEY,
                        value INTEGER NOT NULL DEFAULT 0
                    )
                    """
                )
                conn.execute(
                    f"INSERT OR IGNORE INTO {TABLE_ENTITY_GROUPS_META}(key, value) "
                    f"VALUES ('version', 0)"
                )
                conn.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_eg_sort "
                    f"ON {TABLE_ENTITY_GROUPS} (enabled, sort, id)"
                )
                conn.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_egm_entity "
                    f"ON {TABLE_ENTITY_GROUP_MEMBERS} (entity_id)"
                )
                conn.execute(
                    f"CREATE INDEX IF NOT EXISTS idx_egm_group "
                    f"ON {TABLE_ENTITY_GROUP_MEMBERS} (group_id, sort)"
                )
        finally:
            conn.close()

    # ── 版本 ────────────────────────────────────────────────────────────────
    @staticmethod
    def _bump_version(conn: sqlite3.Connection) -> int:
        conn.execute(
            f"INSERT OR IGNORE INTO {TABLE_ENTITY_GROUPS_META}(key, value) "
            f"VALUES ('version', 0)"
        )
        conn.execute(
            f"UPDATE {TABLE_ENTITY_GROUPS_META} SET value = value + 1 WHERE key = 'version'"
        )
        row = conn.execute(
            f"SELECT value FROM {TABLE_ENTITY_GROUPS_META} WHERE key = 'version'"
        ).fetchone()
        return int(row["value"]) if row else 0

    def get_version(self) -> dict:
        conn = self._connect()
        try:
            row = conn.execute(
                f"SELECT value FROM {TABLE_ENTITY_GROUPS_META} WHERE key = 'version'"
            ).fetchone()
            version = int(row["value"]) if row else 0
            count = conn.execute(f"SELECT COUNT(*) FROM {TABLE_ENTITY_GROUPS}").fetchone()[0]
            updated = conn.execute(
                f"SELECT COALESCE(MAX(updated_at), 0) FROM {TABLE_ENTITY_GROUPS}"
            ).fetchone()[0]
            return {"version": version, "count": count, "updated_at": updated}
        finally:
            conn.close()

    # ── 行转换 ──────────────────────────────────────────────────────────────
    @staticmethod
    def _member_from_row(row: sqlite3.Row) -> dict:
        return {
            "entity_id": row["entity_id"],
            "name": row["name"],
            "icon": row["icon"],
            "sort": int(row["sort"] or 0),
            "extra": _loads(row["extra"], {}),
        }

    def _group_from_row(self, conn: sqlite3.Connection, row: sqlite3.Row,
                        with_members: bool = True) -> dict:
        group = {
            "id": row["id"],
            "name": row["name"],
            "kind": row["kind"],
            "icon": row["icon"],
            "icon_on": row["icon_on"],
            "icon_off": row["icon_off"],
            "color_on": row["color_on"],
            "color_off": row["color_off"],
            "options": _loads(row["options"], {}),
            "actions": _loads(row["actions"], []),
            "default_on_action": row["default_on_action"],
            "default_off_action": row["default_off_action"],
            "sort": int(row["sort"] or 0),
            "enabled": bool(row["enabled"]),
            "rev": int(row["rev"] or 1),
            "created_at": int(row["created_at"] or 0),
            "updated_at": int(row["updated_at"] or 0),
        }
        if with_members:
            group["members"] = [
                self._member_from_row(r)
                for r in conn.execute(
                    f"SELECT entity_id, name, icon, sort, extra "
                    f"FROM {TABLE_ENTITY_GROUP_MEMBERS} "
                    f"WHERE group_id = ? ORDER BY sort, entity_id",
                    (group["id"],),
                ).fetchall()
            ]
        return group

    # ── 读 ──────────────────────────────────────────────────────────────────
    def list_groups(
        self,
        ids: Iterable[str] | None = None,
        kinds: Iterable[str] | None = None,
        entity: str | None = None,
        keyword: str | None = None,
        enabled: Any = 1,
        with_members: bool = True,
        sort: str = "sort",
        limit: int | None = None,
        offset: int = 0,
    ) -> dict:
        """条件查询编组列表。返回 {groups, total}。"""
        where: list[str] = []
        params: list[Any] = []

        id_list = [i for i in (ids or []) if i]
        if id_list:
            where.append(f"g.id IN ({','.join('?' * len(id_list))})")
            params.extend(id_list)

        kind_list = [k for k in (kinds or []) if k]
        if kind_list:
            where.append(f"g.kind IN ({','.join('?' * len(kind_list))})")
            params.extend(kind_list)

        if entity:
            where.append(
                f"EXISTS (SELECT 1 FROM {TABLE_ENTITY_GROUP_MEMBERS} m "
                f"WHERE m.group_id = g.id AND m.entity_id = ?)"
            )
            params.append(entity)

        if keyword:
            escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            where.append("g.name LIKE ? ESCAPE '\\'")
            params.append(f"%{escaped}%")

        if enabled in (0, 1, True, False):
            where.append("g.enabled = ?")
            params.append(1 if enabled else 0)

        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        order_sql = _SORT_FIELDS.get(sort, _SORT_FIELDS["sort"])

        page_sql = ""
        page_params: list[Any] = []
        if limit is not None:
            page_sql = " LIMIT ? OFFSET ?"
            page_params = [max(int(limit), 0), max(int(offset or 0), 0)]

        conn = self._connect()
        try:
            total = conn.execute(
                f"SELECT COUNT(*) FROM {TABLE_ENTITY_GROUPS} g {where_sql}", params
            ).fetchone()[0]
            rows = conn.execute(
                f"SELECT g.* FROM {TABLE_ENTITY_GROUPS} g {where_sql} "
                f"ORDER BY {order_sql}{page_sql}",
                params + page_params,
            ).fetchall()
            groups = [self._group_from_row(conn, r, with_members) for r in rows]
            return {"groups": groups, "total": total}
        finally:
            conn.close()

    def get_group(self, group_id: str, with_members: bool = True) -> dict | None:
        conn = self._connect()
        try:
            row = conn.execute(
                f"SELECT * FROM {TABLE_ENTITY_GROUPS} WHERE id = ?", (group_id,)
            ).fetchone()
            return self._group_from_row(conn, row, with_members) if row else None
        finally:
            conn.close()

    def find_groups_by_entity(self, entity_id: str) -> list[dict]:
        """反查某实体所属的全部编组（只返回壳，够前端提示用）。"""
        conn = self._connect()
        try:
            rows = conn.execute(
                f"SELECT g.id, g.name, g.kind, g.sort, g.enabled "
                f"FROM {TABLE_ENTITY_GROUPS} g "
                f"JOIN {TABLE_ENTITY_GROUP_MEMBERS} m ON m.group_id = g.id "
                f"WHERE m.entity_id = ? ORDER BY g.sort, g.id",
                (entity_id,),
            ).fetchall()
            return [
                {
                    "id": r["id"],
                    "name": r["name"],
                    "kind": r["kind"],
                    "sort": int(r["sort"] or 0),
                    "enabled": bool(r["enabled"]),
                }
                for r in rows
            ]
        finally:
            conn.close()

    def list_members(self, group_id: str) -> list[dict]:
        conn = self._connect()
        try:
            return [
                self._member_from_row(r)
                for r in conn.execute(
                    f"SELECT entity_id, name, icon, sort, extra "
                    f"FROM {TABLE_ENTITY_GROUP_MEMBERS} "
                    f"WHERE group_id = ? ORDER BY sort, entity_id",
                    (group_id,),
                ).fetchall()
            ]
        finally:
            conn.close()

    # ── 校验：编组 ──────────────────────────────────────────────────────────
    def _normalize_group(self, payload: Any, partial: bool = False,
                         warnings: list[str] | None = None) -> dict:
        """校验并规范化编组字段。partial=True 时只返回传入的键。"""
        if not isinstance(payload, dict):
            raise EntityGroupError("invalid_param", "编组必须是对象")
        warnings = warnings if warnings is not None else []
        out: dict[str, Any] = {}

        def has(key: str) -> bool:
            return (key in payload) if partial else (key in payload)

        if "id" in payload:
            gid = payload.get("id")
            if gid is not None:
                gid = str(gid).strip()
                if not GROUP_ID_RE.match(gid):
                    raise EntityGroupError(
                        "invalid_param",
                        "编组 id 格式非法（应以 eg_ 开头，后接 2~32 位小写字母/数字/下划线，"
                        f"且不能以 _ 开头）: {gid}",
                    )
                out["id"] = gid

        if "name" in payload:
            name = payload.get("name")
            if not isinstance(name, str) or not name.strip():
                raise EntityGroupError("invalid_param", "name 不能为空")
            name = name.strip()
            if len(name) > NAME_MAX:
                raise EntityGroupError("invalid_param", f"name 超长（≤{NAME_MAX}）")
            if "\n" in name or "\r" in name:
                raise EntityGroupError("invalid_param", "name 不能包含换行")
            out["name"] = name
        elif not partial:
            raise EntityGroupError("invalid_param", "缺少 name")

        if "kind" in payload:
            kind = str(payload.get("kind") or "").strip()
            if kind not in ENTITY_GROUP_KINDS:
                raise EntityGroupError(
                    "invalid_param", f"kind 不在允许列表: {kind}"
                )
            out["kind"] = kind
        elif not partial:
            out["kind"] = "mixed"

        for key in ("icon", "icon_on", "icon_off", "color_on", "color_off"):
            if key in payload:
                out[key] = _clean_text(payload.get(key))

        if "options" in payload:
            options = payload.get("options")
            if options is None:
                options = {}
            if not isinstance(options, dict):
                raise EntityGroupError("invalid_param", "options 必须是对象")
            out["options"] = self._normalize_options(options, warnings)
        elif not partial:
            out["options"] = {}

        if "actions" in payload:
            out["actions"] = self._normalize_actions(payload.get("actions"), warnings)
        elif not partial:
            out["actions"] = []

        for key in ("default_on_action", "default_off_action"):
            if key in payload:
                value = payload.get(key)
                value = str(value).strip() if value else None
                out[key] = value or None

        if "sort" in payload:
            out["sort"] = _clean_int(payload.get("sort"), 0)
        elif not partial:
            out["sort"] = 0

        if "enabled" in payload:
            out["enabled"] = 1 if payload.get("enabled") in (1, True, "1", "true") else 0
        elif not partial:
            out["enabled"] = 1

        # default_on/off 必须指向存在的动作
        if "actions" in out or "default_on_action" in out or "default_off_action" in out:
            valid_ids = {a["id"] for a in (out.get("actions") or []) if isinstance(a, dict)}
            for key in ("default_on_action", "default_off_action"):
                if key in out and out[key] and valid_ids and out[key] not in valid_ids:
                    warnings.append(f"{key}={out[key]} 不存在于 actions，已置空")
                    out[key] = None
                elif key in out and out[key] and not valid_ids:
                    warnings.append(f"{key}={out[key]} 无对应 actions，已置空")
                    out[key] = None

        if "members" in payload:
            out["members"] = self._normalize_members(payload.get("members"), warnings)

        return out

    @staticmethod
    def _normalize_options(options: dict, warnings: list[str]) -> dict:
        """只保留已知子对象；子对象内只保留白名单键。"""
        result: dict[str, Any] = {}
        allowed = {
            "light": {"brightness_pct", "color_temp_kelvin", "rgb_color", "hs_color"},
            "climate": {"hvac_mode", "temperature", "fan_level"},
            "cover": {"position"},
            "media": {"volume_level"},
        }
        for sub, keys in allowed.items():
            src = options.get(sub)
            if not isinstance(src, dict):
                continue
            clean = {k: v for k, v in src.items() if k in keys}
            dropped = [k for k in src if k not in keys]
            if dropped:
                warnings.append(f"options.{sub} 忽略未知键: {', '.join(dropped)}")
            if clean:
                result[sub] = clean
        return result

    def _normalize_actions(self, actions: Any, warnings: list[str]) -> list[dict]:
        if actions is None:
            return []
        if not isinstance(actions, list):
            raise EntityGroupError("invalid_param", "actions 必须是数组")
        if len(actions) > ACTION_LIMIT:
            raise EntityGroupError("invalid_param", f"预设动作不超过 {ACTION_LIMIT} 个")

        result: list[dict] = []
        seen: set[str] = set()
        for idx, act in enumerate(actions):
            if not isinstance(act, dict):
                raise EntityGroupError("invalid_param", f"actions[{idx}] 必须是对象")
            aid = str(act.get("id") or "").strip()
            if not ACTION_ID_RE.match(aid):
                raise EntityGroupError(
                    "invalid_param", f"actions[{idx}].id 非法: {aid!r}"
                )
            if aid in seen:
                raise EntityGroupError("invalid_param", f"动作 id 重复: {aid}")
            seen.add(aid)

            name = str(act.get("name") or aid).strip()
            if len(name) > NAME_MAX:
                raise EntityGroupError("invalid_param", f"actions[{idx}].name 超长")

            steps = act.get("actions")
            if steps is None:
                steps = []
            if not isinstance(steps, list):
                raise EntityGroupError("invalid_param", f"actions[{idx}].actions 必须是数组")
            if len(steps) > ACTION_STEP_LIMIT:
                raise EntityGroupError(
                    "invalid_param", f"actions[{idx}] 行为条目不超过 {ACTION_STEP_LIMIT} 条"
                )

            clean_steps: list[dict] = []
            for sidx, step in enumerate(steps):
                if not isinstance(step, dict):
                    raise EntityGroupError(
                        "invalid_param", f"actions[{idx}].actions[{sidx}] 必须是对象"
                    )
                service = str(step.get("service") or "").strip()
                if not service or "." not in service:
                    raise EntityGroupError(
                        "invalid_param",
                        f"actions[{idx}].actions[{sidx}].service 应为 domain.service",
                    )
                entity_id = step.get("entity_id")
                if entity_id is not None:
                    entity_id = str(entity_id).strip() or None
                data = step.get("data")
                if data is None:
                    data = {}
                if not isinstance(data, dict):
                    raise EntityGroupError(
                        "invalid_param", f"actions[{idx}].actions[{sidx}].data 必须是对象"
                    )
                clean_steps.append(
                    {"service": service, "entity_id": entity_id, "data": data}
                )

            result.append(
                {
                    "id": aid,
                    "name": name,
                    "icon": _clean_text(act.get("icon")),
                    "confirm": bool(act.get("confirm")),
                    "stop_on_error": bool(act.get("stop_on_error")),
                    "actions": clean_steps,
                }
            )
        return result

    def _normalize_members(self, members: Any,
                           warnings: list[str]) -> list[dict]:
        if members is None:
            return []
        if not isinstance(members, list):
            raise EntityGroupError("invalid_param", "members 必须是数组")
        if len(members) > MEMBER_LIMIT:
            raise EntityGroupError("invalid_param", f"成员不超过 {MEMBER_LIMIT} 个")

        result: list[dict] = []
        seen: set[str] = set()
        for idx, item in enumerate(members):
            member = self._normalize_member(item, idx, warnings)
            if member["entity_id"] in seen:
                warnings.append(f"成员重复已忽略: {member['entity_id']}")
                continue
            seen.add(member["entity_id"])
            result.append(member)
        return result

    @staticmethod
    def _normalize_member(item: Any, idx: int, warnings: list[str]) -> dict:
        if not isinstance(item, dict):
            raise EntityGroupError("invalid_param", f"members[{idx}] 必须是对象")
        entity_id = str(item.get("entity_id") or "").strip()
        if not ENTITY_ID_RE.match(entity_id):
            raise EntityGroupError(
                "invalid_param", f"members[{idx}].entity_id 非法: {entity_id!r}"
            )
        if entity_id.startswith("eg_"):
            raise EntityGroupError("invalid_param", "不支持嵌套编组作为成员")

        extra = item.get("extra")
        extra = extra if isinstance(extra, dict) else {}
        clean_extra, dropped = EntityGroupStore._normalize_extra(extra)
        if dropped:
            warnings.append(
                f"成员 {entity_id} 忽略 extra 未知键: {', '.join(dropped)}"
            )

        raw_sort = item.get("sort")
        return {
            "entity_id": entity_id,
            "name": _clean_text(item.get("name"), NAME_MAX),
            "icon": _clean_text(item.get("icon")),
            # None 表示"调用方未指定"，由插入路径决定默认值
            "sort": None if raw_sort is None else _clean_int(raw_sort, 0),
            "extra": clean_extra,
        }

    @staticmethod
    def _normalize_extra(extra: dict) -> tuple[dict, list[str]]:
        """只保留白名单键，并做轻量类型校验。"""
        clean: dict[str, Any] = {}
        dropped: list[str] = []
        for key, value in extra.items():
            if key in _PARAM_KEYS:
                if key in ("rgb_color", "hs_color"):
                    if (not isinstance(value, (list, tuple)) or len(value) < 2
                            or not all(isinstance(v, (int, float)) for v in value)):
                        dropped.append(key)
                        continue
                    clean[key] = list(value)
                elif key == "fan_level":
                    try:
                        level = int(value)
                    except (TypeError, ValueError):
                        dropped.append(key)
                        continue
                    clean[key] = max(0, min(7, level))
                elif key in ("brightness_pct", "position"):
                    try:
                        clean[key] = max(0, min(100, int(float(value))))
                    except (TypeError, ValueError):
                        dropped.append(key)
                elif key == "temperature":
                    try:
                        clean[key] = float(value)
                    except (TypeError, ValueError):
                        dropped.append(key)
                elif key == "volume_level":
                    try:
                        clean[key] = max(0.0, min(1.0, float(value)))
                    except (TypeError, ValueError):
                        dropped.append(key)
                elif key == "fan_mode":
                    clean[key] = str(value)
                elif key == "hvac_mode":
                    clean[key] = str(value)
                else:
                    clean[key] = value
            elif key in _OVERRIDE_KEYS:
                if key in ("on_data", "off_data"):
                    if not isinstance(value, dict):
                        dropped.append(key)
                        continue
                    clean[key] = value
                else:  # on_service / off_service
                    service = str(value or "").strip()
                    if "." not in service:
                        dropped.append(key)
                        continue
                    clean[key] = service
            else:
                dropped.append(key)
        return clean, dropped

    # ── 写：编组 ────────────────────────────────────────────────────────────
    @staticmethod
    def _assert_id_available(conn: sqlite3.Connection, new_id: str) -> None:
        row = conn.execute(
            f"SELECT 1 FROM {TABLE_ENTITY_GROUPS} WHERE id = ?", (new_id,)
        ).fetchone()
        if row is not None:
            raise EntityGroupError("conflict", f"编组 id 已被占用: {new_id}", status=409)

    @staticmethod
    def _rename_group(conn: sqlite3.Connection, old_id: str, new_id: str) -> None:
        """改 id（= 换 switch 实体 id）。用「复制父行 → 迁移子行 → 删旧行」实现，
        避免直接 UPDATE 主键触发外键约束；rev 由调用方的 UPDATE 统一 +1。"""
        conn.execute(
            f"INSERT INTO {TABLE_ENTITY_GROUPS} "
            f"(id, name, kind, icon, icon_on, icon_off, color_on, color_off, options, actions, "
            f" default_on_action, default_off_action, sort, enabled, rev, created_at, updated_at) "
            f"SELECT ?, name, kind, icon, icon_on, icon_off, color_on, color_off, options, actions, "
            f"       default_on_action, default_off_action, sort, enabled, rev, created_at, ? "
            f"FROM {TABLE_ENTITY_GROUPS} WHERE id = ?",
            (new_id, _now_ms(), old_id),
        )
        conn.execute(
            f"UPDATE {TABLE_ENTITY_GROUP_MEMBERS} SET group_id = ? WHERE group_id = ?",
            (new_id, old_id),
        )
        conn.execute(f"DELETE FROM {TABLE_ENTITY_GROUPS} WHERE id = ?", (old_id,))

    def create_group(self, payload: dict) -> dict:
        warnings: list[str] = []
        data = self._normalize_group(payload, partial=False, warnings=warnings)
        group_id = data.pop("id", None) or _gen_group_id()
        members = data.pop("members", [])
        now = _now_ms()

        conn = self._connect()
        try:
            with conn:
                exists = conn.execute(
                    f"SELECT 1 FROM {TABLE_ENTITY_GROUPS} WHERE id = ?", (group_id,)
                ).fetchone()
                if exists:
                    raise EntityGroupError(
                        "conflict", f"编组已存在: {group_id}", status=409
                    )
                conn.execute(
                    f"INSERT INTO {TABLE_ENTITY_GROUPS} "
                    f"(id, name, kind, icon, icon_on, icon_off, color_on, color_off, "
                    f" options, actions, default_on_action, default_off_action, "
                    f" sort, enabled, rev, created_at, updated_at) "
                    f"VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        group_id, data["name"], data["kind"], data.get("icon"),
                        data.get("icon_on"), data.get("icon_off"),
                        data.get("color_on"), data.get("color_off"),
                        _dumps(data.get("options") or {}),
                        _dumps(data.get("actions") or []),
                        data.get("default_on_action"), data.get("default_off_action"),
                        data.get("sort", 0), data.get("enabled", 1),
                        1, now, now,
                    ),
                )
                self._insert_members(conn, group_id, members)
                version = self._bump_version(conn)
            group = self.get_group(group_id)
            return {"group": group, "version": version, "warnings": warnings}
        finally:
            conn.close()

    def update_group(self, group_id: str, payload: dict,
                     rev: int | None = None) -> dict:
        """PUT：全量更新（未提供的字段保持原值，members 整组替换）。"""
        warnings: list[str] = []
        data = self._normalize_group(payload, partial=True, warnings=warnings)
        members = data.pop("members", None)

        conn = self._connect()
        try:
            with conn:
                row = conn.execute(
                    f"SELECT * FROM {TABLE_ENTITY_GROUPS} WHERE id = ?", (group_id,)
                ).fetchone()
                if row is None:
                    raise EntityGroupError("not_found", f"编组不存在: {group_id}", status=404)
                self._check_rev(row, rev)

                new_id = data.pop("id", None)
                if new_id and new_id != group_id:
                    self._assert_id_available(conn, new_id)
                    self._rename_group(conn, group_id, new_id)
                    group_id = new_id

                fields: list[str] = []
                params: list[Any] = []
                for key, column in (
                    ("name", "name"), ("kind", "kind"), ("icon", "icon"),
                    ("icon_on", "icon_on"), ("icon_off", "icon_off"),
                    ("color_on", "color_on"), ("color_off", "color_off"),
                    ("default_on_action", "default_on_action"),
                    ("default_off_action", "default_off_action"),
                ):
                    if key in data:
                        fields.append(f"{column} = ?")
                        params.append(data[key])
                if "options" in data:
                    fields.append("options = ?")
                    params.append(_dumps(data["options"]))
                if "actions" in data:
                    fields.append("actions = ?")
                    params.append(_dumps(data["actions"]))
                if "sort" in data:
                    fields.append("sort = ?")
                    params.append(int(data["sort"]))
                if "enabled" in data:
                    fields.append("enabled = ?")
                    params.append(int(data["enabled"]))

                fields.extend(["rev = rev + 1", "updated_at = ?"])
                params.append(_now_ms())
                params.append(group_id)
                conn.execute(
                    f"UPDATE {TABLE_ENTITY_GROUPS} SET {', '.join(fields)} WHERE id = ?",
                    params,
                )

                if members is not None:
                    conn.execute(
                        f"DELETE FROM {TABLE_ENTITY_GROUP_MEMBERS} WHERE group_id = ?",
                        (group_id,),
                    )
                    self._insert_members(conn, group_id, members)

                version = self._bump_version(conn)
            group = self.get_group(group_id)
            return {"group": group, "version": version, "warnings": warnings}
        finally:
            conn.close()

    def patch_group(self, group_id: str, patch: dict,
                    rev: int | None = None) -> dict:
        """PATCH：只改传入字段；members 缺省不动，传 null 清空。"""
        warnings: list[str] = []
        data = self._normalize_group(patch, partial=True, warnings=warnings)
        has_members = "members" in data
        members = data.pop("members", None)

        conn = self._connect()
        try:
            with conn:
                row = conn.execute(
                    f"SELECT * FROM {TABLE_ENTITY_GROUPS} WHERE id = ?", (group_id,)
                ).fetchone()
                if row is None:
                    raise EntityGroupError("not_found", f"编组不存在: {group_id}", status=404)
                self._check_rev(row, rev)

                new_id = data.pop("id", None)
                if new_id and new_id != group_id:
                    self._assert_id_available(conn, new_id)
                    self._rename_group(conn, group_id, new_id)
                    group_id = new_id

                fields: list[str] = []
                params: list[Any] = []
                for key in ("name", "kind", "icon", "icon_on", "icon_off",
                            "color_on", "color_off",
                            "default_on_action", "default_off_action"):
                    if key in data:
                        fields.append(f"{key} = ?")
                        params.append(data[key])
                if "options" in data:
                    fields.append("options = ?")
                    params.append(_dumps(data["options"]))
                if "actions" in data:
                    fields.append("actions = ?")
                    params.append(_dumps(data["actions"]))
                if "sort" in data:
                    fields.append("sort = ?")
                    params.append(int(data["sort"]))
                if "enabled" in data:
                    fields.append("enabled = ?")
                    params.append(int(data["enabled"]))

                fields.extend(["rev = rev + 1", "updated_at = ?"])
                params.append(_now_ms())
                params.append(group_id)
                conn.execute(
                    f"UPDATE {TABLE_ENTITY_GROUPS} SET {', '.join(fields)} WHERE id = ?",
                    params,
                )

                if has_members:
                    conn.execute(
                        f"DELETE FROM {TABLE_ENTITY_GROUP_MEMBERS} WHERE group_id = ?",
                        (group_id,),
                    )
                    if members:
                        self._insert_members(conn, group_id, members)

                version = self._bump_version(conn)
            group = self.get_group(group_id)
            return {"group": group, "version": version, "warnings": warnings}
        finally:
            conn.close()

    def delete_group(self, group_id: str) -> dict:
        conn = self._connect()
        try:
            with conn:
                row = conn.execute(
                    f"SELECT id FROM {TABLE_ENTITY_GROUPS} WHERE id = ?", (group_id,)
                ).fetchone()
                if row is None:
                    raise EntityGroupError("not_found", f"编组不存在: {group_id}", status=404)
                removed = conn.execute(
                    f"SELECT COUNT(*) FROM {TABLE_ENTITY_GROUP_MEMBERS} WHERE group_id = ?",
                    (group_id,),
                ).fetchone()[0]
                conn.execute(f"DELETE FROM {TABLE_ENTITY_GROUPS} WHERE id = ?", (group_id,))
                version = self._bump_version(conn)
            return {"removed_members": int(removed), "version": version}
        finally:
            conn.close()

    def batch_save(self, mode: str, groups: Any, deleted: Any) -> dict:
        """批量保存：mode=upsert（默认，局部提交）| replace（整库覆盖）。"""
        mode = (mode or "upsert").strip().lower()
        if mode not in ("upsert", "replace"):
            raise EntityGroupError("invalid_param", f"mode 非法: {mode}")

        groups = groups if isinstance(groups, list) else []
        deleted = deleted if isinstance(deleted, list) else []

        created = updated = removed = 0
        warnings: list[str] = []
        conn = self._connect()
        try:
            with conn:
                if mode == "replace":
                    removed = conn.execute(
                        f"SELECT COUNT(*) FROM {TABLE_ENTITY_GROUPS}"
                    ).fetchone()[0]
                    conn.execute(f"DELETE FROM {TABLE_ENTITY_GROUPS}")

                for payload in groups:
                    warnings_local: list[str] = []
                    data = self._normalize_group(payload, partial=False,
                                                 warnings=warnings_local)
                    warnings.extend(warnings_local)
                    members = data.pop("members", [])
                    group_id = data.pop("id", None) or _gen_group_id()
                    now = _now_ms()
                    exists = conn.execute(
                        f"SELECT 1 FROM {TABLE_ENTITY_GROUPS} WHERE id = ?", (group_id,)
                    ).fetchone()
                    if exists:
                        conn.execute(
                            f"UPDATE {TABLE_ENTITY_GROUPS} SET name=?, kind=?, icon=?, "
                            f"icon_on=?, icon_off=?, color_on=?, color_off=?, options=?, "
                            f"actions=?, default_on_action=?, default_off_action=?, "
                            f"sort=?, enabled=?, rev=rev+1, updated_at=? WHERE id=?",
                            (
                                data["name"], data["kind"], data.get("icon"),
                                data.get("icon_on"), data.get("icon_off"),
                                data.get("color_on"), data.get("color_off"),
                                _dumps(data.get("options") or {}),
                                _dumps(data.get("actions") or []),
                                data.get("default_on_action"),
                                data.get("default_off_action"),
                                data.get("sort", 0), data.get("enabled", 1),
                                now, group_id,
                            ),
                        )
                        updated += 1
                    else:
                        conn.execute(
                            f"INSERT INTO {TABLE_ENTITY_GROUPS} "
                            f"(id, name, kind, icon, icon_on, icon_off, color_on, color_off, "
                            f" options, actions, default_on_action, default_off_action, "
                            f" sort, enabled, rev, created_at, updated_at) "
                            f"VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (
                                group_id, data["name"], data["kind"], data.get("icon"),
                                data.get("icon_on"), data.get("icon_off"),
                                data.get("color_on"), data.get("color_off"),
                                _dumps(data.get("options") or {}),
                                _dumps(data.get("actions") or []),
                                data.get("default_on_action"),
                                data.get("default_off_action"),
                                data.get("sort", 0), data.get("enabled", 1),
                                1, now, now,
                            ),
                        )
                        created += 1
                    conn.execute(
                        f"DELETE FROM {TABLE_ENTITY_GROUP_MEMBERS} WHERE group_id = ?",
                        (group_id,),
                    )
                    self._insert_members(conn, group_id, members)

                for gid in deleted:
                    gid = str(gid or "").strip()
                    if not gid:
                        continue
                    cur = conn.execute(
                        f"DELETE FROM {TABLE_ENTITY_GROUPS} WHERE id = ?", (gid,)
                    )
                    removed += cur.rowcount or 0

                version = self._bump_version(conn)

            # 必须在事务提交后重读，否则新连接看不到未提交的数据
            result = self.list_groups(enabled=None, with_members=True)
            return {
                "groups": result["groups"],
                "total": result["total"],
                "version": version,
                "applied": {"created": created, "updated": updated, "deleted": removed},
                "warnings": warnings,
            }
        finally:
            conn.close()

    def reorder(self, order: Any) -> dict:
        if not isinstance(order, list) or not order:
            raise EntityGroupError("invalid_param", "order 必须是非空数组")
        ids = [str(i).strip() for i in order if str(i).strip()]
        conn = self._connect()
        try:
            with conn:
                for idx, gid in enumerate(ids):
                    conn.execute(
                        f"UPDATE {TABLE_ENTITY_GROUPS} SET sort = ?, rev = rev + 1, "
                        f"updated_at = ? WHERE id = ?",
                        (idx * 10, _now_ms(), gid),
                    )
                version = self._bump_version(conn)
            return {"version": version, "count": len(ids)}
        finally:
            conn.close()

    # ── 写：成员 ────────────────────────────────────────────────────────────
    def add_members(self, group_id: str, items: Any) -> dict:
        """批量添加成员：幂等 upsert，已存在的按传入字段合并。"""
        if isinstance(items, dict):
            items = [items]
        if not isinstance(items, list) or not items:
            raise EntityGroupError("invalid_param", "members 必须是非空数组")

        warnings: list[str] = []
        normalized = [self._normalize_member(item, i, warnings)
                      for i, item in enumerate(items)]

        conn = self._connect()
        try:
            with conn:
                row = conn.execute(
                    f"SELECT * FROM {TABLE_ENTITY_GROUPS} WHERE id = ?", (group_id,)
                ).fetchone()
                if row is None:
                    raise EntityGroupError("not_found", f"编组不存在: {group_id}", status=404)

                existing = {
                    r["entity_id"]: r
                    for r in conn.execute(
                        f"SELECT entity_id, name, icon, sort, extra "
                        f"FROM {TABLE_ENTITY_GROUP_MEMBERS} WHERE group_id = ?",
                        (group_id,),
                    ).fetchall()
                }
                max_sort = max([int(r["sort"] or 0) for r in existing.values()], default=-10)

                added: list[str] = []
                updated: list[str] = []
                skipped: list[str] = []

                for member in normalized:
                    eid = member["entity_id"]
                    if eid in existing:
                        old = existing[eid]
                        old_extra = _loads(old["extra"], {})
                        merged_extra = {**old_extra, **member["extra"]}
                        name = member["name"] if member["name"] is not None else old["name"]
                        icon = member["icon"] if member["icon"] is not None else old["icon"]
                        sort_old = int(old["sort"] or 0)
                        sort_new = sort_old if member["sort"] is None else member["sort"]
                        if (name == old["name"] and icon == old["icon"]
                                and merged_extra == old_extra and sort_new == sort_old):
                            skipped.append(eid)
                            continue
                        conn.execute(
                            f"UPDATE {TABLE_ENTITY_GROUP_MEMBERS} "
                            f"SET name = ?, icon = ?, sort = ?, extra = ? "
                            f"WHERE group_id = ? AND entity_id = ?",
                            (name, icon, sort_new, _dumps(merged_extra), group_id, eid),
                        )
                        updated.append(eid)
                    else:
                        if len(existing) + len(added) >= MEMBER_LIMIT:
                            raise EntityGroupError(
                                "too_many_members",
                                f"成员超过上限 {MEMBER_LIMIT}",
                            )
                        if member["sort"] is None:
                            max_sort += 10
                            member["sort"] = max_sort
                        conn.execute(
                            f"INSERT INTO {TABLE_ENTITY_GROUP_MEMBERS} "
                            f"(group_id, entity_id, name, icon, sort, extra) "
                            f"VALUES (?,?,?,?,?,?)",
                            (group_id, eid, member["name"], member["icon"],
                             member["sort"], _dumps(member["extra"])),
                        )
                        added.append(eid)

                self._touch_group(conn, group_id)
                version = self._bump_version(conn)

            return {
                "added": added,
                "updated": updated,
                "skipped": skipped,
                "members": self.list_members(group_id),
                "version": version,
                "warnings": warnings,
            }
        finally:
            conn.close()

    def update_member(self, group_id: str, entity_id: str, payload: dict) -> dict:
        """PUT：整体替换单个成员（name/icon/sort/extra）。"""
        member = self._normalize_member(
            {**payload, "entity_id": entity_id}, 0, []
        )
        conn = self._connect()
        try:
            with conn:
                self._require_group(conn, group_id)
                exists = conn.execute(
                    f"SELECT 1 FROM {TABLE_ENTITY_GROUP_MEMBERS} "
                    f"WHERE group_id = ? AND entity_id = ?",
                    (group_id, entity_id),
                ).fetchone()
                if not exists:
                    raise EntityGroupError(
                        "member_not_found", f"成员不存在: {entity_id}", status=404
                    )
                conn.execute(
                    f"UPDATE {TABLE_ENTITY_GROUP_MEMBERS} "
                    f"SET name = ?, icon = ?, sort = ?, extra = ? "
                    f"WHERE group_id = ? AND entity_id = ?",
                    (member["name"], member["icon"],
                     0 if member["sort"] is None else member["sort"],
                     _dumps(member["extra"]), group_id, entity_id),
                )
                self._touch_group(conn, group_id)
                version = self._bump_version(conn)
            return {"member": member, "version": version}
        finally:
            conn.close()

    def patch_member(self, group_id: str, entity_id: str, patch: dict) -> dict:
        """PATCH：局部更新单个成员，extra 浅合并。"""
        if not isinstance(patch, dict):
            raise EntityGroupError("invalid_param", "patch 必须是对象")

        conn = self._connect()
        try:
            with conn:
                self._require_group(conn, group_id)
                row = conn.execute(
                    f"SELECT * FROM {TABLE_ENTITY_GROUP_MEMBERS} "
                    f"WHERE group_id = ? AND entity_id = ?",
                    (group_id, entity_id),
                ).fetchone()
                if row is None:
                    raise EntityGroupError(
                        "member_not_found", f"成员不存在: {entity_id}", status=404
                    )

                name = row["name"]
                if "name" in patch:
                    name = _clean_text(patch.get("name"), NAME_MAX)
                icon = row["icon"]
                if "icon" in patch:
                    icon = _clean_text(patch.get("icon"))
                sort = int(row["sort"] or 0)
                if "sort" in patch:
                    sort = _clean_int(patch.get("sort"), sort)

                old_extra = _loads(row["extra"], {})
                new_extra = old_extra
                if "extra" in patch:
                    raw = patch.get("extra")
                    if raw is None:
                        new_extra = {}
                    elif isinstance(raw, dict):
                        clean, dropped = self._normalize_extra(raw)
                        new_extra = {**old_extra, **clean}
                        if dropped:
                            _LOGGER.debug(
                                "[entity_group] %s extra 忽略键: %s", entity_id, dropped
                            )
                    else:
                        raise EntityGroupError("invalid_param", "extra 必须是对象")

                conn.execute(
                    f"UPDATE {TABLE_ENTITY_GROUP_MEMBERS} "
                    f"SET name = ?, icon = ?, sort = ?, extra = ? "
                    f"WHERE group_id = ? AND entity_id = ?",
                    (name, icon, sort, _dumps(new_extra), group_id, entity_id),
                )
                self._touch_group(conn, group_id)
                version = self._bump_version(conn)

            return {
                "member": {
                    "entity_id": entity_id,
                    "name": name,
                    "icon": icon,
                    "sort": sort,
                    "extra": new_extra,
                },
                "version": version,
            }
        finally:
            conn.close()

    def remove_member(self, group_id: str, entity_id: str) -> dict:
        conn = self._connect()
        try:
            with conn:
                self._require_group(conn, group_id)
                cur = conn.execute(
                    f"DELETE FROM {TABLE_ENTITY_GROUP_MEMBERS} "
                    f"WHERE group_id = ? AND entity_id = ?",
                    (group_id, entity_id),
                )
                if not cur.rowcount:
                    raise EntityGroupError(
                        "member_not_found", f"成员不存在: {entity_id}", status=404
                    )
                self._touch_group(conn, group_id)
                version = self._bump_version(conn)
                count = conn.execute(
                    f"SELECT COUNT(*) FROM {TABLE_ENTITY_GROUP_MEMBERS} WHERE group_id = ?",
                    (group_id,),
                ).fetchone()[0]
            return {"removed": True, "member_count": int(count), "version": version}
        finally:
            conn.close()

    def remove_members(self, group_id: str, entities: Any) -> dict:
        if not isinstance(entities, list) or not entities:
            raise EntityGroupError("invalid_param", "entities 必须是非空数组")
        ids = [str(e).strip() for e in entities if str(e).strip()]
        removed: list[str] = []

        conn = self._connect()
        try:
            with conn:
                self._require_group(conn, group_id)
                for eid in ids:
                    cur = conn.execute(
                        f"DELETE FROM {TABLE_ENTITY_GROUP_MEMBERS} "
                        f"WHERE group_id = ? AND entity_id = ?",
                        (group_id, eid),
                    )
                    if cur.rowcount:
                        removed.append(eid)
                if removed:
                    self._touch_group(conn, group_id)
                version = self._bump_version(conn)
                count = conn.execute(
                    f"SELECT COUNT(*) FROM {TABLE_ENTITY_GROUP_MEMBERS} WHERE group_id = ?",
                    (group_id,),
                ).fetchone()[0]
            return {
                "removed": removed,
                "member_count": int(count),
                "version": version,
            }
        finally:
            conn.close()

    def reorder_members(self, group_id: str, order: Any) -> dict:
        if not isinstance(order, list) or not order:
            raise EntityGroupError("invalid_param", "order 必须是非空数组")
        ids = [str(e).strip() for e in order if str(e).strip()]

        conn = self._connect()
        try:
            with conn:
                self._require_group(conn, group_id)
                for idx, eid in enumerate(ids):
                    conn.execute(
                        f"UPDATE {TABLE_ENTITY_GROUP_MEMBERS} SET sort = ? "
                        f"WHERE group_id = ? AND entity_id = ?",
                        (idx * 10, group_id, eid),
                    )
                self._touch_group(conn, group_id)
                version = self._bump_version(conn)
            return {"version": version, "count": len(ids),
                    "members": self.list_members(group_id)}
        finally:
            conn.close()

    # ── 内部工具 ────────────────────────────────────────────────────────────
    @staticmethod
    def _check_rev(row: sqlite3.Row, rev: int | None) -> None:
        if rev is None:
            return
        try:
            expected = int(rev)
        except (TypeError, ValueError):
            return
        current = int(row["rev"] or 1)
        if expected != current:
            raise EntityGroupError(
                "conflict",
                f"rev 冲突：期望 {expected}，当前 {current}",
                status=409,
                current_rev=current,
            )

    @staticmethod
    def _require_group(conn: sqlite3.Connection, group_id: str) -> None:
        row = conn.execute(
            f"SELECT id FROM {TABLE_ENTITY_GROUPS} WHERE id = ?", (group_id,)
        ).fetchone()
        if row is None:
            raise EntityGroupError("not_found", f"编组不存在: {group_id}", status=404)

    @staticmethod
    def _touch_group(conn: sqlite3.Connection, group_id: str) -> None:
        conn.execute(
            f"UPDATE {TABLE_ENTITY_GROUPS} SET rev = rev + 1, updated_at = ? WHERE id = ?",
            (_now_ms(), group_id),
        )

    @staticmethod
    def _insert_members(conn: sqlite3.Connection, group_id: str,
                        members: list[dict]) -> None:
        for idx, member in enumerate(members):
            sort_val = member.get("sort")
            if sort_val is None:
                sort_val = idx * 10
            conn.execute(
                f"INSERT INTO {TABLE_ENTITY_GROUP_MEMBERS} "
                f"(group_id, entity_id, name, icon, sort, extra) VALUES (?,?,?,?,?,?)",
                (
                    group_id, member["entity_id"], member.get("name"),
                    member.get("icon"), int(sort_val),
                    _dumps(member.get("extra") or {}),
                ),
            )

    # ── 供实体层使用的只读快照 ──────────────────────────────────────────────
    def snapshot(self, enabled_only: bool = True) -> dict:
        """一次性读出全部编组（含成员），供 sensor/switch 投影使用。"""
        result = self.list_groups(enabled=1 if enabled_only else None,
                                  with_members=True)
        version_info = self.get_version()
        return {
            "groups": result["groups"],
            "total": result["total"],
            "version": version_info["version"],
            "updated_at": version_info["updated_at"],
        }

    def domain_service_map(self) -> dict:
        """内置 on/off 的域映射（同时下发给前端做本地执行）。"""
        return ENTITY_GROUP_DOMAIN_SERVICE_MAP
