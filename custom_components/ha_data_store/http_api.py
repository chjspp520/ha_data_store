"""ha_data_store HTTP API — 标准配置接口 + 万能参数化查询 + 动态路由。

路由总览：
  GET  /api/ha_data_store/config         → 获取所有监控实体配置
  POST /api/ha_data_store/config         → 新增/修改监控实体配置
  GET  /api/ha_data_store/routes         → 获取所有自定义路由
  POST /api/ha_data_store/routes         → 新增/修改自定义路由
  GET  /api/ha_data_store/query          → 万能参数化查询
  *    /api/ha_data_store/custom/{tail}  → 动态路由（高级功能，运行时查库执行 SQL）
  GET  /api/ha_data_store/db_viewer      → 内置数据库浏览器
  GET  /api/ha_data_store/db_viewer/data → 数据库浏览器数据 API
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import (
    DOMAIN,
    TABLE_ENTITY_CONFIGS,
    TABLE_DEVICE_HISTORY,
    TABLE_CUSTOM_ROUTES,
    TABLE_ATTR_TYPE_DEFS,
    TABLE_EXPORT_CONFIGS,
    TABLE_FILE_SOURCE_CONFIGS,
    TABLE_API_SOURCE_CONFIGS,
    TABLE_API_KEYS,
    TABLE_API_SETTINGS,
    TABLE_VACUUM_TYPE_DEFS,
    TABLE_VACUUM_CONFIGS,
    TABLE_VACUUM_HISTORY,
    TABLE_PUSH_TARGETS,
    TABLE_BRIDGE_CONNECTIONS,
    TABLE_BRIDGE_ENTITIES,
    TABLE_HEALTH_RECORDS,
    TABLE_MEDIA_PLAYLISTS,
    TABLE_MEDIA_QUEUE,
    CATEGORY_DEVICE,
    CATEGORY_ENVIRONMENT,
    CATEGORY_ATTRIBUTE,
    CATEGORY_VACUUM,
    ATTR_MODE_FIELDS,
    ATTR_MODE_LIST,
    ATTR_MODE_MULTI,
    EXTRA_JSON_COLUMN,
    ATTR_TABLE_PREFIX,
    get_attr_table_name,
    VALID_METRICS,
    get_env_table_name,
    DEFAULT_TIMEZONE,
    COLLECT_MODE_POLL,
)
from .logger import get_logger as _log_local

_LOGGER = logging.getLogger(__name__)

# SQL 安全沙箱：禁止出现的关键字（不区分大小写）
_DANGEROUS_KEYWORDS = ("DROP", "DELETE", "UPDATE", "INSERT", "ALTER", "CREATE", "TRUNCATE", "EXEC", "EXECUTE")


def _get_local_iso(timezone_offset: int) -> str:
    """返回当前本地时间的 ISO 格式字符串。"""
    return (datetime.utcnow() + timedelta(hours=timezone_offset)).isoformat()


def _get_client_ip(request: web.Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote or ""


def _to_float_or_none(val) -> float | None:
    """将值转为 float，无效值返回 None。"""
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _same_subnet(ip1: str, ip2: str) -> bool:
    """判断两个 IP 是否在同一 /24 子网。"""
    try:
        p1 = ip1.rsplit(".", 1)[0] if "." in ip1 else ""
        p2 = ip2.rsplit(".", 1)[0] if "." in ip2 else ""
        return bool(p1 and p1 == p2)
    except Exception:
        return False


def _get_ha_subnet(request: web.Request) -> str:
    """获取 HA 服务器的子网前缀。"""
    host = request.host.split(":")[0] if request.host else ""
    if host and not _is_private_ip(host):
        for key in ("X-Forwarded-Host", "Host"):
            v = request.headers.get(key, "")
            if v:
                h = v.split(":")[0]
                if _is_private_ip(h):
                    host = h
                    break
    return host.rsplit(".", 1)[0] if "." in host else ""


def _is_private_ip(ip: str) -> bool:
    try:
        parts = [int(p) for p in ip.split(".")]
        if len(parts) != 4:
            return False
        a, b = parts[0], parts[1]
        return a == 10 or (a == 172 and 16 <= b <= 31) or (a == 192 and b == 168) or a == 127
    except Exception:
        return False


def _make_auth_token(db_path: str) -> str:
    pw = "admin"
    try:
        conn = sqlite3.connect(db_path)
        row = conn.execute(
            f"SELECT svalue FROM {TABLE_API_SETTINGS} WHERE skey='admin_password'"
        ).fetchone()
        if row: pw = row[0]
        conn.close()
    except Exception:
        pass
    return hashlib.sha256(f"hds_auth_{pw}".encode()).hexdigest()


_LOGIN_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>登录 - HA数据统一存储系统</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#1a1a2e;color:#eaeaea;font-family:-apple-system,sans-serif;display:flex;justify-content:center;align-items:center;min-height:100vh}
.card{background:#16213e;border:1px solid #0f3460;border-radius:12px;padding:32px 28px;width:340px;text-align:center}
.card h1{font-size:20px;color:#e94560;margin-bottom:8px}
.card p{font-size:13px;color:#a0a0b0;margin-bottom:20px}
.card input{width:100%;background:#0d1117;color:#eaeaea;border:1px solid #0f3460;border-radius:6px;padding:10px;font-size:14px;margin-bottom:12px}
.card input:focus{outline:2px solid #e94560}
.card button{width:100%;background:#e94560;color:#fff;border:none;border-radius:6px;padding:10px;font-size:14px;cursor:pointer}
.card button:hover{background:#c73852}
.err{color:#e94560;font-size:12px;margin-bottom:8px}
</style></head>
<body>
<div class="card">
<h1>HA数据统一存储系统</h1>
<p>管理面板 · 仅限局域网访问</p>
<form method="post" action="/api/ha_data_store/db_viewer/login">
<input type="password" name="password" placeholder="管理员密码" autofocus>
<button type="submit">登 录</button>
</form>
<p class="err">{error}</p>
</div>
</body></html>"""


class _BaseDBView(HomeAssistantView):
    """所有 API 视图的公共基类，封装线程池数据库操作。"""

    requires_auth = False  # 外部 UI 跨域免鉴权
    cors_allowed = True    # 允许跨域

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    async def _exec_in_executor(self, hass: HomeAssistant, func, *args):
        """将阻塞函数提交到 HA 线程池执行。"""
        return await hass.async_add_executor_job(func, *args)

    def _check_api_enabled(self, request: web.Request) -> web.Response | None:
        """检查 API 访问开关 + API Key。开关关闭或无有效 Key 返回 403。"""
        hass: HomeAssistant = request.app["hass"]
        if not hass.data.get(DOMAIN, {}).get("api_enabled", True):
            return web.Response(status=403)
        # 检查 API Key
        key = request.query.get("key", "") or request.headers.get("Authorization", "").replace("Bearer ", "")
        if not key:
            return web.Response(status=403)
        def _verify():
            conn = sqlite3.connect(self._db_path)
            try:
                # 确保表存在
                conn.execute(
                    f"CREATE TABLE IF NOT EXISTS {TABLE_API_KEYS} ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL UNIQUE,"
                    "name TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1,"
                    "created_at TEXT NOT NULL DEFAULT '')"
                )
                conn.execute(
                    f"CREATE TABLE IF NOT EXISTS {TABLE_API_SETTINGS} ("
                    "skey TEXT PRIMARY KEY, svalue TEXT NOT NULL DEFAULT '')"
                )
                conn.execute(
                    f"INSERT OR IGNORE INTO {TABLE_API_SETTINGS} (skey, svalue) VALUES ('admin_password', 'admin')"
                )
                conn.commit()
                row = conn.execute(
                    f"SELECT id FROM {TABLE_API_KEYS} WHERE key = ? AND enabled = 1", (key,)
                ).fetchone()
                return row is not None
            except Exception:
                return False
            finally:
                conn.close()
        if not _verify():
            return web.Response(status=403)
        return None

    @staticmethod
    def _check_master_switch(hass: HomeAssistant) -> web.Response | None:
        """仅检查主开关（不含 Key），用于 db_viewer 等管理页面。"""
        if not hass.data.get(DOMAIN, {}).get("api_enabled", True):
            return web.Response(status=403)
        return None

    @staticmethod
    def _check_db_viewer_enabled(hass: HomeAssistant) -> web.Response | None:
        """检查数据库浏览器访问开关，关闭时返回 403。"""
        if not hass.data.get(DOMAIN, {}).get("db_viewer_enabled", True):
            return web.Response(status=403)
        return None

    @staticmethod
    def _check_db_edit_enabled(hass: HomeAssistant) -> web.Response | None:
        """检查数据库修改开关，关闭时返回 403。"""
        if not hass.data.get(DOMAIN, {}).get("db_edit_enabled", True):
            return web.Response(status=403)
        return None


# ========================================================================== #
#  1. GET /api/device_energy/config — 获取监控实体列表                         #
# ========================================================================== #
class EntityConfigListView(_BaseDBView):
    """获取所有监控实体配置。"""

    url = "/api/ha_data_store/config"
    name = "api:ha_data_store:config_list"

    async def get(self, request: web.Request) -> web.Response:
        db_path = self._db_path

        def _query() -> list[dict]:
            conn = sqlite3.connect(db_path)
            try:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    f"SELECT entity_id, enabled, category, metric_type, collect_interval, "
                    f"  power_entity, friendly_name, device_name, room, attr_type, collect_mode, created_at, updated_at "
                    f"FROM {TABLE_ENTITY_CONFIGS} ORDER BY entity_id"
                )
                return [dict(row) for row in cursor.fetchall()]
            finally:
                conn.close()

        try:
            hass: HomeAssistant = request.app["hass"]
            if (resp := self._check_api_enabled(request)):
                return resp
            rows = await self._exec_in_executor(hass, _query)
            # 附加 HA 实时状态
            for row in rows:
                state_obj = hass.states.get(row["entity_id"])
                if state_obj:
                    row["status"] = "online" if state_obj.state not in ("unavailable", "unknown", None) else "unavailable"
                    row["state_label"] = state_obj.state
                else:
                    row["status"] = "offline"
                    row["state_label"] = "N/A"
                # 查询真实最后数据时间
                row["last_data_time"] = ""
            # 批量查询各实体的最后数据时间
            await self._exec_in_executor(hass, self._fill_last_data_times, rows, db_path)
            return self.json({"success": True, "data": rows})
        except Exception as exc:
            _LOGGER.exception("获取实体配置失败")
            return self.json({"success": False, "error": str(exc)}, status_code=500)

    @staticmethod
    def _fill_last_data_times(rows: list[dict], db_path: str) -> None:
        """为每个实体查询其在数据库中的最新数据时间。"""
        if not rows:
            return
        conn = sqlite3.connect(db_path)
        try:
            conn.row_factory = sqlite3.Row
            for row in rows:
                eid = row.get("entity_id", "")
                cat = row.get("category", "")
                try:
                    if cat == "device":
                        r = conn.execute(
                            f"SELECT COALESCE(NULLIF(off_time,''), on_time) AS last_time "
                            f"FROM {TABLE_DEVICE_HISTORY} WHERE entity_id = ? "
                            f"ORDER BY id DESC LIMIT 1",
                            (eid,),
                        ).fetchone()
                        if r and r["last_time"]:
                            row["last_data_time"] = r["last_time"]
                    elif cat == "environment":
                        metric = row.get("metric_type", "")
                        if metric and metric in VALID_METRICS:
                            tbl = get_env_table_name(metric)
                            r = conn.execute(
                                f"SELECT MAX(datetime) AS last_time FROM {tbl} "
                                f"WHERE entity_id = ?",
                                (eid,),
                            ).fetchone()
                            if r and r["last_time"]:
                                row["last_data_time"] = r["last_time"]
                    elif cat == CATEGORY_ATTRIBUTE:
                        atype = row.get("attr_type", "")
                        if atype:
                            tbl = get_attr_table_name(atype)
                            r = conn.execute(
                                f"SELECT MAX(datetime) AS last_time FROM {tbl} "
                                f"WHERE entity_id = ?",
                                (eid,),
                            ).fetchone()
                            if r and r["last_time"]:
                                row["last_data_time"] = r["last_time"]
                except Exception:
                    pass
        finally:
            conn.close()


# ========================================================================== #
#  2. POST /api/device_energy/config — 新增/修改监控实体配置                   #
# ========================================================================== #
class EntityConfigView(_BaseDBView):
    """新增或修改监控实体配置（使用 ON CONFLICT 实现无感 upsert）。"""

    url = "/api/ha_data_store/config"
    name = "api:ha_data_store:config_update"

    async def post(self, request: web.Request) -> web.Response:
        db_path = self._db_path
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_master_switch(hass)):
            return resp
        if (resp := self._check_db_edit_enabled(hass)):
            return resp
        tz = hass.data.get(DOMAIN, {}).get("timezone", DEFAULT_TIMEZONE)

        try:
            body = await request.json()
        except Exception:
            return self.json({"success": False, "error": "请求体不是合法的 JSON"}, status_code=400)

        entity_id = body.get("entity_id", "").strip()
        if not entity_id:
            return self.json({"success": False, "error": "entity_id 不能为空"}, status_code=400)

        enabled = int(body.get("enabled", 1))
        category = body.get("category", "device")
        metric_type = body.get("metric_type", "")
        collect_interval = int(body.get("collect_interval", 30))
        round_minute = int(body.get("round_minute", 0))
        power_entity = body.get("power_entity", "")
        friendly_name = body.get("friendly_name", "")
        device_name = body.get("device_name", "")
        room = body.get("room", "")
        now = _get_local_iso(tz)

        def _upsert() -> None:
            conn = sqlite3.connect(db_path)
            try:
                # 检查是否已存在，保护 attr_type/collect_mode 不被覆盖
                row = conn.execute(
                    f"SELECT attr_type, collect_mode FROM {TABLE_ENTITY_CONFIGS} WHERE entity_id = ?",
                    (entity_id,),
                ).fetchone()
                existing_attr_type = row[0] if row else ""
                existing_collect_mode = row[1] if row else ""

                # 请求没有传 attr_type 时保留旧值
                if "attr_type" not in body:
                    attr_type_val = existing_attr_type
                    collect_mode_val = existing_collect_mode
                    category_val = body.get("category", "device")
                    # 如果旧值是 attribute 类别，且请求也没说要改，保留
                    if not body.get("category") and row:
                        cat_row = conn.execute(
                            f"SELECT category FROM {TABLE_ENTITY_CONFIGS} WHERE entity_id = ?",
                            (entity_id,),
                        ).fetchone()
                        if cat_row and cat_row[0] == CATEGORY_ATTRIBUTE:
                            category_val = CATEGORY_ATTRIBUTE
                else:
                    attr_type_val = body.get("attr_type", "")
                    collect_mode_val = body.get("collect_mode", COLLECT_MODE_POLL)
                    category_val = body.get("category", "device")

                conn.execute(
                    f"""
                    INSERT INTO {TABLE_ENTITY_CONFIGS}
                        (entity_id, enabled, category, metric_type, collect_interval, round_minute,
                         power_entity, friendly_name, device_name, room,
                         attr_type, collect_mode, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(entity_id, attr_type) DO UPDATE SET
                        enabled          = excluded.enabled,
                        category         = excluded.category,
                        metric_type      = excluded.metric_type,
                        collect_interval = excluded.collect_interval,
                        round_minute     = excluded.round_minute,
                        power_entity     = excluded.power_entity,
                        friendly_name    = excluded.friendly_name,
                        device_name      = excluded.device_name,
                        room             = excluded.room,
                        collect_mode     = excluded.collect_mode,
                        updated_at       = excluded.updated_at
                    """,
                    (entity_id, enabled, category_val, metric_type, collect_interval, round_minute,
                     power_entity, friendly_name, device_name, room,
                     attr_type_val, collect_mode_val, now, now),
                )
                conn.commit()
            finally:
                conn.close()

        try:
            await self._exec_in_executor(hass, _upsert)
            return self.json({"success": True, "message": f"实体 {entity_id} 配置已保存"})
        except Exception as exc:
            _LOGGER.exception("保存实体配置失败")
            return self.json({"success": False, "error": str(exc)}, status_code=500)

    async def delete(self, request: web.Request) -> web.Response:
        """DELETE /api/ha_data_store/config?entity_id=xxx → 禁用该实体。"""
        db_path = self._db_path
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_api_enabled(request)):
            return resp
        if (resp := self._check_db_edit_enabled(hass)):
            return resp
        entity_id = request.query.get("entity_id", "").strip()
        if not entity_id:
            return self.json({"success": False, "error": "缺少 entity_id 参数"}, status_code=400)

        tz = hass.data.get(DOMAIN, {}).get("timezone", DEFAULT_TIMEZONE)
        now = _get_local_iso(tz)

        def _disable() -> None:
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    f"UPDATE {TABLE_ENTITY_CONFIGS} SET enabled = 0, updated_at = ? "
                    f"WHERE entity_id = ?", (now, entity_id),
                )
                conn.commit()
            finally:
                conn.close()

        try:
            await self._exec_in_executor(hass, _disable)
            return self.json({"success": True, "message": f"实体 {entity_id} 已移除"})
        except Exception as exc:
            _LOGGER.exception("删除实体配置失败")
            return self.json({"success": False, "error": str(exc)}, status_code=500)


# ========================================================================== #
#  3. GET /api/device_energy/routes — 获取所有自定义路由                       #
# ========================================================================== #
class CustomRoutesListView(_BaseDBView):
    """获取所有自定义路由及 SQL。"""

    url = "/api/ha_data_store/routes"
    name = "api:ha_data_store:routes_list"

    async def get(self, request: web.Request) -> web.Response:
        db_path = self._db_path

        def _query() -> list[dict]:
            conn = sqlite3.connect(db_path)
            try:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    f"SELECT route_path, sql_statement, description, created_at, updated_at "
                    f"FROM {TABLE_CUSTOM_ROUTES} ORDER BY route_path"
                )
                return [dict(row) for row in cursor.fetchall()]
            finally:
                conn.close()

        try:
            hass: HomeAssistant = request.app["hass"]
            if (resp := self._check_api_enabled(request)):
                return resp
            rows = await self._exec_in_executor(hass, _query)
            return self.json({"success": True, "data": rows})
        except Exception as exc:
            _LOGGER.exception("获取自定义路由失败")
            return self.json({"success": False, "error": str(exc)}, status_code=500)


# ========================================================================== #
#  4. POST /api/device_energy/routes — 新增/修改自定义路由                      #
# ========================================================================== #
class CustomRoutesView(_BaseDBView):
    """新增或修改自定义路由（包含 route_path, sql_statement, description）。"""

    url = "/api/ha_data_store/routes"
    name = "api:ha_data_store:routes_update"

    async def post(self, request: web.Request) -> web.Response:
        db_path = self._db_path
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_master_switch(hass)):
            return resp
        if (resp := self._check_db_edit_enabled(hass)):
            return resp
        tz = hass.data.get(DOMAIN, {}).get("timezone", DEFAULT_TIMEZONE)

        try:
            body = await request.json()
        except Exception:
            return self.json({"success": False, "error": "请求体不是合法的 JSON"}, status_code=400)

        route_path = body.get("route_path", "").strip()
        sql_statement = body.get("sql_statement", "").strip()
        description = body.get("description", "")

        if not route_path:
            return self.json({"success": False, "error": "route_path 不能为空"}, status_code=400)
        if not sql_statement:
            return self.json({"success": False, "error": "sql_statement 不能为空"}, status_code=400)

        # --- 保存时做安全预检：只允许 SELECT 开头 ---
        sql_upper = sql_statement.strip().upper()
        if not sql_upper.startswith("SELECT"):
            return self.json(
                {"success": False, "error": "SQL 必须以 SELECT 开头"},
                status_code=403,
            )
        for keyword in _DANGEROUS_KEYWORDS:
            if keyword in sql_upper:
                return self.json(
                    {"success": False, "error": f"SQL 包含危险关键字 '{keyword}'，已拒绝"},
                    status_code=403,
                )

        now = _get_local_iso(tz)

        def _upsert() -> None:
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    f"""
                    INSERT INTO {TABLE_CUSTOM_ROUTES}
                        (route_path, sql_statement, description, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(route_path) DO UPDATE SET
                        sql_statement = excluded.sql_statement,
                        description   = excluded.description,
                        updated_at    = excluded.updated_at
                    """,
                    (route_path, sql_statement, description, now, now),
                )
                conn.commit()
            finally:
                conn.close()

        try:
            await self._exec_in_executor(hass, _upsert)
            return self.json({"success": True, "message": f"路由 '{route_path}' 已保存"})
        except Exception as exc:
            _LOGGER.exception("保存自定义路由失败")
            return self.json({"success": False, "error": str(exc)}, status_code=500)


# ========================================================================== #
#  5. ★ 核心万能动态路由 DynamicRouterView ★                                 #
#     挂载路径: /api/device_energy/custom/{tail:.*}                          #
#     运行时从 custom_routes 表实时检索 SQL 并执行                              #
# ========================================================================== #
class DynamicRouterView(_BaseDBView):
    """万能动态路由：拦截请求 → 提取尾缀 → 查库取 SQL → 安全校验 → 执行返回。"""

    url = "/api/ha_data_store/custom/{tail:.*}"
    name = "api:ha_data_store:dynamic_router"

    def __init__(self, db_path: str, hass: HomeAssistant) -> None:
        super().__init__(db_path)
        self._hass = hass

    # ------------------------------------------------------------------ #
    #  统一入口：所有 HTTP 方法都走此逻辑                                    #
    # ------------------------------------------------------------------ #
    async def _handle_dynamic(self, request: web.Request) -> web.Response:
        """核心调度：提取路径 → 查库 → 安全校验 → 执行 → 返回。"""
        if (resp := self._check_api_enabled(request)):
            return resp
        tail: str = request.match_info.get("tail", "").strip()
        if not tail:
            return self.json({"success": False, "error": "路由路径为空"}, status_code=400)

        tail = tail.strip("/")
        db_path = self._db_path

        def _lookup_sql() -> dict | None:
            conn = sqlite3.connect(db_path)
            try:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    f"SELECT sql_statement, description FROM {TABLE_CUSTOM_ROUTES} "
                    f"WHERE route_path = ?",
                    (tail,),
                )
                row = cursor.fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

        try:
            route_info = await self._exec_in_executor(self._hass, _lookup_sql)
        except Exception as exc:
            _LOGGER.exception("动态路由查库失败 [%s]", tail)
            return self.json({"success": False, "error": f"数据库查询异常: {exc}"}, status_code=500)

        if route_info is None:
            return self.json(
                {"success": False, "error": f"未找到路由 '{tail}'，请先在前端配置"},
                status_code=404,
            )

        sql_statement = route_info["sql_statement"]

        # 安全沙箱校验
        sql_upper = sql_statement.strip().upper()
        if not sql_upper.startswith("SELECT"):
            return self.json({"success": False, "error": "SQL 必须以 SELECT 开头"}, status_code=403)
        for keyword in _DANGEROUS_KEYWORDS:
            if keyword in sql_upper:
                return self.json(
                    {"success": False, "error": f"SQL 包含危险关键字 '{keyword}'，已拒绝执行"},
                    status_code=403,
                )

        # 解析 GET Query 参数，构建安全参数绑定
        query_params = dict(request.query)
        params: list[str] = []
        if query_params:
            for key in sorted(query_params.keys()):
                params.append(query_params[key])

        def _execute_sql() -> list[dict]:
            conn = sqlite3.connect(db_path)
            try:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(sql_statement, params)
                rows = cursor.fetchall()
                return [dict(row) for row in rows]
            finally:
                conn.close()

        try:
            result = await self._exec_in_executor(self._hass, _execute_sql)
            return self.json({"success": True, "data": result})
        except sqlite3.OperationalError as exc:
            _LOGGER.warning("动态路由 SQL 执行错误 [%s]: %s", tail, exc)
            return self.json({"success": False, "error": f"SQL 执行错误: {exc}"}, status_code=400)
        except Exception as exc:
            _LOGGER.exception("动态路由未知异常 [%s]", tail)
            return self.json({"success": False, "error": f"服务器内部错误: {exc}"}, status_code=500)

    async def get(self, request: web.Request) -> web.Response:
        return await self._handle_dynamic(request)

    async def post(self, request: web.Request) -> web.Response:
        return await self._handle_dynamic(request)

    async def put(self, request: web.Request) -> web.Response:
        return await self._handle_dynamic(request)

    async def delete(self, request: web.Request) -> web.Response:
        return await self._handle_dynamic(request)


# ========================================================================== #
#  6. ★ 万能参数化查询 QueryView ★                                            #
#     挂载路径: GET /api/device_energy/query                                  #
#     参数: type, entity_id, date, month, year, detail, metric, limit, room   #
# ========================================================================== #
class QueryView(_BaseDBView):
    """万能参数化查询：通过 type 参数路由到预定义查询。

    支持的 type:
      - device_history  : 设备开关记录（按日/月/年智能返回 + 内嵌汇总）
      - device_summary  : 纯汇总（不返回记录，仅汇总数字）
      - env_history     : 环境历史记录（含元数据：最新日期、总条数、起止时间）
      - env_latest      : 环境最新一条记录
      - attr_history    : 属性历史记录
      - attr_latest     : 属性最新一条记录
      - entities        : 已配置实体列表
    """

    url = "/api/ha_data_store/query"
    name = "api:ha_data_store:query"

    # ------------------------------------------------------------------ #
    #  GET 入口                                                           #
    # ------------------------------------------------------------------ #
    async def get(self, request: web.Request) -> web.Response:
        query_type = request.query.get("type", "").strip().lower()
        if not query_type:
            return self.json(
                {"success": False, "error": "缺少 type 参数，可选: device_history, device_summary, env_history, env_latest, attr_history, attr_latest, attr_daily, entities, rooms_daily, rooms_multi_metric, vacuum_history, entity_data_dates, aggregate_daily, aggregate_monthly, aggregate_yearly, ranking_daily, ranking_monthly, ranking_yearly, electricity_standard"},
                status_code=400,
            )

        db_path = self._db_path
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_api_enabled(request)):
            return resp

        try:
            if query_type == "device_history":
                result = await self._exec_in_executor(hass, self._query_device_history, db_path, request)
            elif query_type == "device_summary":
                result = await self._exec_in_executor(hass, self._query_device_summary, db_path, request)
            elif query_type == "env_history":
                result = await self._exec_in_executor(hass, self._query_env_history, db_path, request)
            elif query_type == "env_latest":
                result = await self._exec_in_executor(hass, self._query_env_latest, db_path, request)
            elif query_type == "attr_history":
                result = await self._exec_in_executor(hass, self._query_attr_history, db_path, request)
            elif query_type == "attr_latest":
                result = await self._exec_in_executor(hass, self._query_attr_latest, db_path, request)
            elif query_type == "attr_daily":
                result = await self._exec_in_executor(hass, self._query_attr_daily, db_path, request)
            elif query_type == "entities":
                result = await self._exec_in_executor(hass, self._query_entities, db_path, request)
            elif query_type == "rooms_daily":
                result = await self._exec_in_executor(hass, self._query_rooms_daily, db_path, request)
            elif query_type == "rooms_multi_metric":
                result = await self._exec_in_executor(hass, self._query_rooms_multi_metric, db_path, request)
            elif query_type == "vacuum_history":
                result = await self._exec_in_executor(hass, self._query_vacuum_history, db_path, request)
            elif query_type == "entity_data_dates":
                result = await self._exec_in_executor(hass, self._query_entity_data_dates, db_path, request)
            elif query_type == "aggregate_daily":
                result = await self._exec_in_executor(hass, self._query_aggregate_daily, db_path, request)
            elif query_type == "aggregate_monthly":
                result = await self._exec_in_executor(hass, self._query_aggregate_monthly, db_path, request)
            elif query_type == "aggregate_yearly":
                result = await self._exec_in_executor(hass, self._query_aggregate_yearly, db_path, request)
            elif query_type in ("ranking_daily", "ranking_monthly", "ranking_yearly"):
                result = await self._exec_in_executor(hass, self._query_ranking, db_path, request)
            elif query_type == "electricity_standard":
                result = await self._exec_in_executor(hass, self._query_electricity_standard, db_path, request)
                # electricity_standard 直接返回，不走 success/data 包装
                return self.json(result)
            elif query_type == "health_history":
                result = await self._exec_in_executor(hass, self._query_health_history, db_path, request)
            elif query_type == "health_latest":
                result = await self._exec_in_executor(hass, self._query_health_latest, db_path, request)
            else:
                return self.json(
                    {"success": False, "error": f"未知的 type '{query_type}'"},
                    status_code=400,
                )
            return self.json({"success": True, "data": result})
        except ValueError as exc:
            return self.json({"success": False, "error": str(exc)}, status_code=400)
        except Exception as exc:
            _LOGGER.exception("万能查询异常 [%s]", query_type)
            return self.json({"success": False, "error": str(exc)}, status_code=500)

    # ------------------------------------------------------------------ #
    #  辅助：从请求中提取公共参数                                            #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _extract_params(request: web.Request) -> dict:
        """提取并校验公共查询参数。"""
        entity_id = request.query.get("entity_id", "").strip()
        date = request.query.get("date", "").strip()       # YYYY-MM-DD
        month = request.query.get("month", "").strip()     # YYYY-MM
        year = request.query.get("year", "").strip()       # YYYY
        start = request.query.get("start", "").strip()     # YYYY-MM-DD（起始日期，与 end 配合使用）
        end = request.query.get("end", "").strip()         # YYYY-MM-DD（截止日期，与 start 配合使用）
        detail = request.query.get("detail", "").strip().lower() in ("true", "1", "yes")
        metric = request.query.get("metric", "").strip()   # temperature/humidity/pm25/co2/power/sensor
        try:
            limit = int(request.query.get("limit", "0").strip())
        except ValueError:
            limit = 0
        category = request.query.get("category", "").strip()  # device/environment
        room = request.query.get("room", "").strip()          # 房间过滤
        return {
            "entity_id": entity_id, "date": date, "month": month,
            "year": year, "start": start, "end": end,
            "detail": detail, "metric": metric,
            "limit": limit, "category": category, "room": room,
        }

    # ------------------------------------------------------------------ #
    #  辅助：根据 room 查找 entity_id 列表                                 #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _get_entity_ids_by_room(conn: sqlite3.Connection, room: str) -> list[str]:
        """从 entity_configs 表中获取指定房间的所有 entity_id。"""
        cursor = conn.execute(
            f"SELECT entity_id FROM {TABLE_ENTITY_CONFIGS} WHERE room = ? AND enabled = 1",
            (room,),
        )
        return [row[0] for row in cursor.fetchall()]

    # ------------------------------------------------------------------ #
    #  device_history：按时间粒度智能返回 + 内嵌汇总                        #
    # ------------------------------------------------------------------ #
    def _parse_records_state_attr(self, records: list[dict]) -> None:
        """将 records 中 state_attr 字符串原地解析为 JSON 数组。"""
        for r in records:
            sa = r.get("state_attr")
            if sa:
                try:
                    r["state_attr"] = json.loads(sa)
                except (json.JSONDecodeError, TypeError):
                    r["state_attr"] = []
            else:
                r["state_attr"] = []

    def _query_device_history(self, db_path: str, request: web.Request) -> dict:
        params = self._extract_params(request)
        entity_id = params["entity_id"]
        room = params["room"]

        if not entity_id and not room:
            raise ValueError("entity_id 或 room 至少需要一个")

        date = params["date"]
        month = params["month"]
        year = params["year"]
        detail = params["detail"]
        limit = params["limit"] or 1000

        conn = sqlite3.connect(db_path)
        try:
            conn.row_factory = sqlite3.Row

            # 构建 WHERE 条件：entity_id 和/或 room
            conditions = []
            sql_params: list = []

            if entity_id:
                conditions.append("entity_id = ?")
                sql_params.append(entity_id)
            if room:
                conditions.append("room = ?")
                sql_params.append(room)

            where_clause = " AND ".join(conditions)

            # ---------- 按日查：返回当天所有记录 + 汇总 ----------
            if date:
                pattern = f"{date}%"
                cursor = conn.execute(
                    f"SELECT * FROM {TABLE_DEVICE_HISTORY} "
                    f"WHERE {where_clause} AND on_time LIKE ? "
                    f"ORDER BY on_time ASC LIMIT ?",
                    (*sql_params, pattern, limit),
                )
                records = [dict(row) for row in cursor.fetchall()]
                self._parse_records_state_attr(records)
                summary = self._calc_device_summary_by_where(conn, where_clause, sql_params, pattern)
                return {"records": records, "summary": summary}

            # ---------- 按月查：默认返回每日汇总，detail=true 返回原始记录 ----------
            if month:
                pattern = f"{month}-%"
                if detail:
                    cursor = conn.execute(
                        f"SELECT * FROM {TABLE_DEVICE_HISTORY} "
                        f"WHERE {where_clause} AND on_time LIKE ? "
                        f"ORDER BY on_time ASC LIMIT ?",
                        (*sql_params, pattern, limit),
                    )
                    records = [dict(row) for row in cursor.fetchall()]
                    self._parse_records_state_attr(records)
                    summary = self._calc_device_summary_by_where(conn, where_clause, sql_params, pattern)
                    return {"records": records, "summary": summary}
                else:
                    # 按日汇总
                    cursor = conn.execute(
                        f"SELECT SUBSTR(on_time, 1, 10) AS date, "
                        f"  COUNT(*) AS on_count, "
                        f"  COALESCE(SUM(energy_consumed), 0) AS total_energy, "
                        f"  COALESCE(SUM(duration), 0) AS total_duration "
                        f"FROM {TABLE_DEVICE_HISTORY} "
                        f"WHERE {where_clause} AND on_time LIKE ? "
                        f"  AND off_time != '' AND off_time IS NOT NULL "
                        f"GROUP BY SUBSTR(on_time, 1, 10) "
                        f"ORDER BY date",
                        (*sql_params, pattern),
                    )
                    daily_summaries = [dict(row) for row in cursor.fetchall()]
                    summary = self._calc_device_summary_by_where(conn, where_clause, sql_params, pattern)
                    return {"daily_summaries": daily_summaries, "summary": summary}

            # ---------- 按年查：返回每月汇总 ----------
            if year:
                pattern = f"{year}-%"
                cursor = conn.execute(
                    f"SELECT SUBSTR(on_time, 1, 7) AS month, "
                    f"  COUNT(*) AS on_count, "
                    f"  COALESCE(SUM(energy_consumed), 0) AS total_energy, "
                    f"  COALESCE(SUM(duration), 0) AS total_duration "
                    f"FROM {TABLE_DEVICE_HISTORY} "
                    f"WHERE {where_clause} AND on_time LIKE ? "
                    f"  AND off_time != '' AND off_time IS NOT NULL "
                    f"GROUP BY SUBSTR(on_time, 1, 7) "
                    f"ORDER BY month",
                    (*sql_params, pattern),
                )
                monthly_summaries = [dict(row) for row in cursor.fetchall()]
                summary = self._calc_device_summary_by_where(conn, where_clause, sql_params, pattern)
                return {"monthly_summaries": monthly_summaries, "summary": summary}

            # ---------- 无时间范围：返回最近记录 + 累计汇总 ----------
            cursor = conn.execute(
                f"SELECT * FROM {TABLE_DEVICE_HISTORY} "
                f"WHERE {where_clause} ORDER BY on_time DESC LIMIT ?",
                (*sql_params, limit),
            )
            records = [dict(row) for row in cursor.fetchall()]
            self._parse_records_state_attr(records)
            summary = self._calc_device_summary_by_where(conn, where_clause, sql_params, "%")
            return {"records": records, "summary": summary}

        finally:
            conn.close()

    # ------------------------------------------------------------------ #
    #  device_summary：纯汇总（不返回记录）                                  #
    # ------------------------------------------------------------------ #
    def _query_device_summary(self, db_path: str, request: web.Request) -> dict:
        params = self._extract_params(request)
        entity_id = params["entity_id"]
        room = params["room"]

        if not entity_id and not room:
            raise ValueError("entity_id 或 room 至少需要一个")

        date = params["date"]
        month = params["month"]
        year = params["year"]

        # 确定时间匹配模式
        if date:
            pattern = f"{date}%"
        elif month:
            pattern = f"{month}-%"
        elif year:
            pattern = f"{year}-%"
        else:
            pattern = "%"  # 全部累计

        conn = sqlite3.connect(db_path)
        try:
            conditions = []
            sql_params: list = []
            if entity_id:
                conditions.append("entity_id = ?")
                sql_params.append(entity_id)
            if room:
                conditions.append("room = ?")
                sql_params.append(room)
            where_clause = " AND ".join(conditions)

            return self._calc_device_summary_by_where(conn, where_clause, sql_params, pattern)
        finally:
            conn.close()

    # ------------------------------------------------------------------ #
    #  env_history：环境历史 + 元数据                                       #
    # ------------------------------------------------------------------ #
    def _query_env_history(self, db_path: str, request: web.Request) -> dict:
        params = self._extract_params(request)
        entity_id = params["entity_id"]
        room = params["room"]

        if not entity_id and not room:
            raise ValueError("entity_id 或 room 至少需要一个")

        metric_raw = params["metric"]
        if not metric_raw:
            raise ValueError("env_history 必须指定 metric 参数（temperature/humidity/pm25/co2/power/sensor）")

        # 支持逗号分隔的多指标
        metrics = [m.strip() for m in metric_raw.split(",") if m.strip()]
        invalid = [m for m in metrics if m not in VALID_METRICS]
        if invalid:
            raise ValueError(f"无效的 metric: {', '.join(invalid)}，可选: {', '.join(VALID_METRICS)}")

        date = params["date"]
        month = params["month"]
        year = params["year"]
        start = params["start"]
        end = params["end"]
        limit = params["limit"] or 1000

        # 构建 datetime 过滤条件
        datetime_conditions: list[str] = []
        datetime_params: list[str] = []

        if start and end:
            # start/end 范围查询
            datetime_conditions.append("datetime >= ?")
            datetime_params.append(f"{start} 00:00:00")
            datetime_conditions.append("datetime <= ?")
            datetime_params.append(f"{end} 23:59:59")
        elif date:
            datetime_conditions.append("datetime LIKE ?")
            datetime_params.append(f"{date}%")
        elif month:
            datetime_conditions.append("datetime LIKE ?")
            datetime_params.append(f"{month}-%")
        elif year:
            datetime_conditions.append("datetime LIKE ?")
            datetime_params.append(f"{year}-%")

        conn = sqlite3.connect(db_path)
        try:
            conn.row_factory = sqlite3.Row

            conditions = []
            sql_params_base: list = []
            if entity_id:
                conditions.append("entity_id = ?")
                sql_params_base.append(entity_id)
            if room:
                conditions.append("room = ?")
                sql_params_base.append(room)
            conditions.extend(datetime_conditions)
            where_clause = " AND ".join(conditions) if conditions else "1=1"

            all_records: list = []
            for metric in metrics:
                tbl = get_env_table_name(metric)
                sql = (
                    f"SELECT id, entity_id, name, datetime, value, room, "
                    f"  '{metric}' AS metric "
                    f"FROM {tbl} WHERE {where_clause} "
                    f"ORDER BY datetime ASC LIMIT ?"
                )
                sql_params = list(sql_params_base) + datetime_params + [limit]
                cursor = conn.execute(sql, sql_params)
                for row in cursor.fetchall():
                    all_records.append(dict(row))

            # 多指标时按 datetime 排序
            if len(metrics) > 1:
                all_records.sort(key=lambda r: r.get("datetime", ""))

            return {
                "metrics": metrics,
                "records": all_records,
                "metadata": {"total_count": len(all_records)},
            }
        finally:
            conn.close()

    # ------------------------------------------------------------------ #
    #  env_latest：环境最新一条记录                                         #
    # ------------------------------------------------------------------ #
    def _query_env_latest(self, db_path: str, request: web.Request) -> dict:
        params = self._extract_params(request)
        entity_id = params["entity_id"]
        room = params["room"]

        if not entity_id and not room:
            raise ValueError("entity_id 或 room 至少需要一个")

        metric_raw = params["metric"]
        if not metric_raw:
            raise ValueError("env_latest 必须指定 metric 参数（temperature/humidity/pm25/co2/power/sensor）")

        # 支持逗号分隔的多指标
        metrics = [m.strip() for m in metric_raw.split(",") if m.strip()]
        invalid = [m for m in metrics if m not in VALID_METRICS]
        if invalid:
            raise ValueError(f"无效的 metric: {', '.join(invalid)}，可选: {', '.join(VALID_METRICS)}")

        conn = sqlite3.connect(db_path)
        try:
            conn.row_factory = sqlite3.Row
            conditions = []
            sql_params: list = []
            if entity_id:
                conditions.append("entity_id = ?")
                sql_params.append(entity_id)
            if room:
                conditions.append("room = ?")
                sql_params.append(room)
            where_clause = " AND ".join(conditions)

            results: dict = {}
            for metric in metrics:
                tbl = get_env_table_name(metric)
                cursor = conn.execute(
                    f"SELECT id, entity_id, name, datetime, value, room, '{metric}' AS metric "
                    f"FROM {tbl} WHERE {where_clause} ORDER BY datetime DESC LIMIT 1",
                    (*sql_params,),
                )
                row = cursor.fetchone()
                if row:
                    results[metric] = dict(row)

            return {"metrics": metrics, "latest": results}
        finally:
            conn.close()

    # ------------------------------------------------------------------ #
    #  entities：已配置实体列表                                              #
    # ------------------------------------------------------------------ #
    def _query_entities(self, db_path: str, request: web.Request) -> dict:
        params = self._extract_params(request)
        category = params["category"]
        room = params["room"]

        conn = sqlite3.connect(db_path)
        try:
            conn.row_factory = sqlite3.Row
            sql = (
                f"SELECT entity_id, enabled, category, metric_type, collect_interval, "
                f"  power_entity, friendly_name, room, created_at, updated_at "
                f"FROM {TABLE_ENTITY_CONFIGS}"
            )
            sql_params: list = []
            conditions = []
            if category:
                conditions.append("category = ?")
                sql_params.append(category)
            if room:
                conditions.append("room = ?")
                sql_params.append(room)
            if conditions:
                sql += " WHERE " + " AND ".join(conditions)
            sql += " ORDER BY entity_id"

            cursor = conn.execute(sql, sql_params)
            rows = [dict(row) for row in cursor.fetchall()]
            return {"entities": rows}
        finally:
            conn.close()

    # ------------------------------------------------------------------ #
    #  entity_data_dates：查询指定实体某月哪些日期有数据                       #
    # ------------------------------------------------------------------ #
    def _query_entity_data_dates(self, db_path: str, request: web.Request) -> dict:
        params = self._extract_params(request)
        entity_id = params["entity_id"]
        month = params["month"]

        if not entity_id:
            raise ValueError("entity_data_dates 需要 entity_id 参数")
        if not month:
            raise ValueError("entity_data_dates 需要 month 参数（格式：YYYY-MM）")

        # 校验 month 格式
        import re
        if not re.match(r"^\d{4}-\d{2}$", month):
            raise ValueError("month 参数格式错误，应为 YYYY-MM")

        date_field = request.query.get("date_field", "").strip()

        conn = sqlite3.connect(db_path)
        try:
            # 如果未指定 date_field，则自动检测
            if not date_field:
                # 从 entity_configs 获取实体的 category
                cursor = conn.execute(
                    f"SELECT category, metric_type, attr_type FROM {TABLE_ENTITY_CONFIGS} WHERE entity_id = ?",
                    (entity_id,),
                )
                row = cursor.fetchone()
                if not row:
                    raise ValueError(f"未找到实体 {entity_id} 的配置")

                category = row[0]
                metric_type = row[1]
                attr_type = row[2]

                if category == CATEGORY_DEVICE:
                    date_field = "on_time"
                elif category == CATEGORY_ENVIRONMENT:
                    date_field = "datetime"
                elif category == CATEGORY_ATTRIBUTE:
                    date_field = "datetime"
                elif category == CATEGORY_VACUUM:
                    date_field = "datetime"
                else:
                    date_field = "datetime"

            pattern = f"{month}-%"

            # 确定要查询的表
            tables_to_query = []

            if date_field == "on_time":
                # 查 device_history 表
                tables_to_query.append((TABLE_DEVICE_HISTORY, "on_time"))
            elif date_field == "datetime":
                # 需要确定具体的环境/属性表
                cursor = conn.execute(
                    f"SELECT category, metric_type, attr_type FROM {TABLE_ENTITY_CONFIGS} WHERE entity_id = ?",
                    (entity_id,),
                )
                row = cursor.fetchone()
                if row:
                    category = row[0]
                    metric_type = row[1]
                    attr_type = row[2]
                    if category == CATEGORY_ENVIRONMENT and metric_type:
                        tables_to_query.append((get_env_table_name(metric_type), "datetime"))
                    elif category == CATEGORY_ATTRIBUTE and attr_type:
                        tables_to_query.append((get_attr_table_name(attr_type), "datetime"))
                    else:
                        # 尝试所有环境表和属性表
                        for metric in VALID_METRICS:
                            tables_to_query.append((get_env_table_name(metric), "datetime"))
                        # 也尝试常见的属性表
                        cursor2 = conn.execute(
                            f"SELECT type_name FROM {TABLE_ATTR_TYPE_DEFS}"
                        )
                        for arow in cursor2.fetchall():
                            tables_to_query.append((get_attr_table_name(arow[0]), "datetime"))
                else:
                    # 未找到配置，尝试所有表
                    tables_to_query.append((TABLE_DEVICE_HISTORY, "on_time"))
                    for metric in VALID_METRICS:
                        tables_to_query.append((get_env_table_name(metric), "datetime"))
                    cursor2 = conn.execute(
                        f"SELECT type_name FROM {TABLE_ATTR_TYPE_DEFS}"
                    )
                    for arow in cursor2.fetchall():
                        tables_to_query.append((get_attr_table_name(arow[0]), "datetime"))
            else:
                # 用户指定了自定义 date_field，查询所有可能的表
                tables_to_query.append((TABLE_DEVICE_HISTORY, date_field))
                for metric in VALID_METRICS:
                    tables_to_query.append((get_env_table_name(metric), date_field))
                cursor2 = conn.execute(
                    f"SELECT type_name FROM {TABLE_ATTR_TYPE_DEFS}"
                )
                for arow in cursor2.fetchall():
                    tables_to_query.append((get_attr_table_name(arow[0]), date_field))

            all_dates = set()
            for tbl, dfield in tables_to_query:
                # 检查表是否存在
                try:
                    conn.execute(f"SELECT 1 FROM {tbl} LIMIT 1")
                except Exception:
                    continue
                # 检查字段是否存在
                try:
                    col_info = conn.execute(f"PRAGMA table_info({tbl})").fetchall()
                    col_names = [c[1] for c in col_info]
                    if dfield not in col_names:
                        continue
                except Exception:
                    continue

                cursor = conn.execute(
                    f"SELECT DISTINCT SUBSTR({dfield}, 1, 10) AS date "
                    f"FROM {tbl} "
                    f"WHERE entity_id = ? AND {dfield} LIKE ? "
                    f"ORDER BY date",
                    (entity_id, pattern),
                )
                for row in cursor.fetchall():
                    if row[0]:
                        all_dates.add(row[0])

            dates = sorted(all_dates)
            return {"dates": dates, "count": len(dates), "month": month, "entity_id": entity_id, "date_field": date_field}
        finally:
            conn.close()

    # ------------------------------------------------------------------ #
    #  aggregate_daily：指定实体指定月的每日数据汇总                            #
    # ------------------------------------------------------------------ #
    def _query_aggregate_daily(self, db_path: str, request: web.Request) -> dict:
        params = self._extract_params(request)
        entity_id = params["entity_id"]
        month = params["month"]

        if not entity_id:
            raise ValueError("aggregate_daily 需要 entity_id 参数")
        if not month:
            raise ValueError("aggregate_daily 需要 month 参数（格式：YYYY-MM）")

        import re
        if not re.match(r"^\d{4}-\d{2}$", month):
            raise ValueError("month 参数格式错误，应为 YYYY-MM")

        date_field = request.query.get("date_field", "").strip()

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            # 自动检测 date_field
            if not date_field:
                cursor = conn.execute(
                    f"SELECT category, metric_type, attr_type FROM {TABLE_ENTITY_CONFIGS} WHERE entity_id = ?",
                    (entity_id,),
                )
                row = cursor.fetchone()
                if not row:
                    raise ValueError(f"未找到实体 {entity_id} 的配置")
                category = row[0]
                if category == CATEGORY_DEVICE:
                    date_field = "on_time"
                elif category == CATEGORY_VACUUM:
                    date_field = "datetime"
                else:
                    date_field = "datetime"

            # 确定要查询的表
            table_name, actual_date_field = self._resolve_aggregate_table(conn, entity_id, date_field)
            if not table_name:
                raise ValueError(f"未找到实体 {entity_id} 对应的数据表")

            # 检查表是否包含 duration/energy_consumed 字段
            col_names = self._get_table_columns(conn, table_name)
            has_duration = "duration" in col_names
            has_energy = "energy_consumed" in col_names

            if not has_duration and not has_energy:
                return {
                    "entity_id": entity_id,
                    "month": month,
                    "date_field": actual_date_field,
                    "daily_summaries": [],
                    "warning": f"表 {table_name} 不包含 duration/energy_consumed 字段，无法聚合",
                }

            pattern = f"{month}-%"
            sum_parts = [f"COUNT(*) AS on_count"]
            if has_energy:
                sum_parts.append(f"COALESCE(SUM(CASE WHEN energy_consumed IS NOT NULL THEN energy_consumed ELSE 0 END), 0) AS total_energy")
            if has_duration:
                sum_parts.append(f"COALESCE(SUM(CASE WHEN duration IS NOT NULL THEN duration ELSE 0 END), 0) AS total_duration")

            cursor = conn.execute(
                f"SELECT SUBSTR({actual_date_field}, 1, 10) AS date, "
                f"  {', '.join(sum_parts)}, "
                f"  MAX(CASE WHEN off_time IS NULL OR off_time = '' THEN 1 ELSE 0 END) AS is_running "
                f"FROM {table_name} "
                f"WHERE entity_id = ? AND {actual_date_field} LIKE ? "
                f"GROUP BY SUBSTR({actual_date_field}, 1, 10) "
                f"ORDER BY date",
                (entity_id, pattern),
            )

            has_off_time_col = "off_time" in col_names
            daily_summaries = []
            for row in cursor.fetchall():
                item = {"date": row["date"], "on_count": row["on_count"]}
                if has_energy:
                    item["total_energy"] = round(row["total_energy"], 2)
                if has_duration:
                    item["total_duration"] = round(row["total_duration"], 0)
                # 有 off_time 列时，标记该日是否有正在运行的记录
                if has_off_time_col:
                    item["is_running"] = row["is_running"] == 1
                daily_summaries.append(item)

            return {
                "entity_id": entity_id,
                "month": month,
                "date_field": actual_date_field,
                "daily_summaries": daily_summaries,
            }
        finally:
            conn.close()

    # ------------------------------------------------------------------ #
    #  aggregate_monthly：指定实体指定年的每月数据汇总                          #
    # ------------------------------------------------------------------ #
    def _query_aggregate_monthly(self, db_path: str, request: web.Request) -> dict:
        params = self._extract_params(request)
        entity_id = params["entity_id"]
        year = params["year"]

        if not entity_id:
            raise ValueError("aggregate_monthly 需要 entity_id 参数")
        if not year:
            raise ValueError("aggregate_monthly 需要 year 参数（格式：YYYY）")

        import re
        if not re.match(r"^\d{4}$", year):
            raise ValueError("year 参数格式错误，应为 YYYY")

        date_field = request.query.get("date_field", "").strip()

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            # 自动检测 date_field
            if not date_field:
                cursor = conn.execute(
                    f"SELECT category, metric_type, attr_type FROM {TABLE_ENTITY_CONFIGS} WHERE entity_id = ?",
                    (entity_id,),
                )
                row = cursor.fetchone()
                if not row:
                    raise ValueError(f"未找到实体 {entity_id} 的配置")
                category = row[0]
                if category == CATEGORY_DEVICE:
                    date_field = "on_time"
                elif category == CATEGORY_VACUUM:
                    date_field = "datetime"
                else:
                    date_field = "datetime"

            # 确定要查询的表
            table_name, actual_date_field = self._resolve_aggregate_table(conn, entity_id, date_field)
            if not table_name:
                raise ValueError(f"未找到实体 {entity_id} 对应的数据表")

            # 检查表是否包含 duration/energy_consumed 字段
            col_names = self._get_table_columns(conn, table_name)
            has_duration = "duration" in col_names
            has_energy = "energy_consumed" in col_names

            if not has_duration and not has_energy:
                return {
                    "entity_id": entity_id,
                    "year": year,
                    "date_field": actual_date_field,
                    "monthly_summaries": [],
                    "warning": f"表 {table_name} 不包含 duration/energy_consumed 字段，无法聚合",
                }

            pattern = f"{year}-%"
            sum_parts = [f"COUNT(*) AS on_count"]
            if has_energy:
                sum_parts.append(f"COALESCE(SUM(CASE WHEN energy_consumed IS NOT NULL THEN energy_consumed ELSE 0 END), 0) AS total_energy")
            if has_duration:
                sum_parts.append(f"COALESCE(SUM(CASE WHEN duration IS NOT NULL THEN duration ELSE 0 END), 0) AS total_duration")

            cursor = conn.execute(
                f"SELECT SUBSTR({actual_date_field}, 1, 7) AS month, "
                f"  {', '.join(sum_parts)} "
                f"FROM {table_name} "
                f"WHERE entity_id = ? AND {actual_date_field} LIKE ? "
                f"GROUP BY SUBSTR({actual_date_field}, 1, 7) "
                f"ORDER BY month",
                (entity_id, pattern),
            )
            monthly_summaries = []
            for row in cursor.fetchall():
                item = {"month": row["month"], "on_count": row["on_count"]}
                if has_energy:
                    item["total_energy"] = round(row["total_energy"], 2)
                if has_duration:
                    item["total_duration"] = round(row["total_duration"], 0)
                monthly_summaries.append(item)

            return {
                "entity_id": entity_id,
                "year": year,
                "date_field": actual_date_field,
                "monthly_summaries": monthly_summaries,
            }
        finally:
            conn.close()

    # ------------------------------------------------------------------ #
    #  aggregate_yearly：指定实体所有年的年度数据汇总                           #
    # ------------------------------------------------------------------ #
    def _query_aggregate_yearly(self, db_path: str, request: web.Request) -> dict:
        params = self._extract_params(request)
        entity_id = params["entity_id"]

        if not entity_id:
            raise ValueError("aggregate_yearly 需要 entity_id 参数")

        date_field = request.query.get("date_field", "").strip()

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            # 自动检测 date_field
            if not date_field:
                cursor = conn.execute(
                    f"SELECT category, metric_type, attr_type FROM {TABLE_ENTITY_CONFIGS} WHERE entity_id = ?",
                    (entity_id,),
                )
                row = cursor.fetchone()
                if not row:
                    raise ValueError(f"未找到实体 {entity_id} 的配置")
                category = row[0]
                if category == CATEGORY_DEVICE:
                    date_field = "on_time"
                elif category == CATEGORY_VACUUM:
                    date_field = "datetime"
                else:
                    date_field = "datetime"

            # 确定要查询的表
            table_name, actual_date_field = self._resolve_aggregate_table(conn, entity_id, date_field)
            if not table_name:
                raise ValueError(f"未找到实体 {entity_id} 对应的数据表")

            # 检查表是否包含 duration/energy_consumed 字段
            col_names = self._get_table_columns(conn, table_name)
            has_duration = "duration" in col_names
            has_energy = "energy_consumed" in col_names

            if not has_duration and not has_energy:
                return {
                    "entity_id": entity_id,
                    "date_field": actual_date_field,
                    "yearly_summaries": [],
                    "warning": f"表 {table_name} 不包含 duration/energy_consumed 字段，无法聚合",
                }

            sum_parts = [f"COUNT(*) AS on_count"]
            if has_energy:
                sum_parts.append(f"COALESCE(SUM(CASE WHEN energy_consumed IS NOT NULL THEN energy_consumed ELSE 0 END), 0) AS total_energy")
            if has_duration:
                sum_parts.append(f"COALESCE(SUM(CASE WHEN duration IS NOT NULL THEN duration ELSE 0 END), 0) AS total_duration")

            cursor = conn.execute(
                f"SELECT SUBSTR({actual_date_field}, 1, 4) AS year, "
                f"  {', '.join(sum_parts)} "
                f"FROM {table_name} "
                f"WHERE entity_id = ? "
                f"GROUP BY SUBSTR({actual_date_field}, 1, 4) "
                f"ORDER BY year",
                (entity_id,),
            )
            yearly_summaries = []
            for row in cursor.fetchall():
                item = {"year": row["year"], "on_count": row["on_count"]}
                if has_energy:
                    item["total_energy"] = round(row["total_energy"], 2)
                if has_duration:
                    item["total_duration"] = round(row["total_duration"], 0)
                yearly_summaries.append(item)

            return {
                "entity_id": entity_id,
                "date_field": actual_date_field,
                "yearly_summaries": yearly_summaries,
            }
        finally:
            conn.close()

    # ------------------------------------------------------------------ #
    #  ranking_daily/monthly/yearly：按实体ID汇总排行                         #
    # ------------------------------------------------------------------ #
    def _query_ranking(self, db_path: str, request: web.Request) -> dict:
        """排行榜查询：按 entity_id 汇总 duration / energy_consumed，降序排列。"""
        query_type = request.query.get("type", "").strip().lower()
        limit = int(request.query.get("limit", "0").strip()) or 50

        # 根据排行榜类型解析时间参数
        if query_type == "ranking_daily":
            date = request.query.get("date", "").strip()
            if not date:
                raise ValueError("ranking_daily 必须指定 date 参数（YYYY-MM-DD）")
            pattern = f"{date}%"
            period_label = date
        elif query_type == "ranking_monthly":
            month = request.query.get("month", "").strip()
            if not month:
                raise ValueError("ranking_monthly 必须指定 month 参数（YYYY-MM）")
            pattern = f"{month}-%"
            period_label = month
        elif query_type == "ranking_yearly":
            year = request.query.get("year", "").strip()
            if not year:
                raise ValueError("ranking_yearly 必须指定 year 参数（YYYY）")
            pattern = f"{year}-%"
            period_label = year
        else:
            raise ValueError(f"未知的排行榜类型: {query_type}")

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.execute(
                f"SELECT entity_id, "
                f"  MAX(name) AS name, "
                f"  MAX(room) AS room, "
                f"  COUNT(*) AS on_count, "
                f"  COALESCE(SUM(duration), 0) AS total_duration, "
                f"  COALESCE(SUM(energy_consumed), 0) AS total_energy "
                f"FROM {TABLE_DEVICE_HISTORY} "
                f"WHERE on_time LIKE ? "
                f"  AND off_time != '' AND off_time IS NOT NULL "
                f"GROUP BY entity_id "
                f"ORDER BY total_duration DESC "
                f"LIMIT ?",
                (pattern, limit),
            )
            rankings = []
            for rank, row in enumerate(cursor.fetchall(), start=1):
                rankings.append({
                    "rank": rank,
                    "entity_id": row["entity_id"],
                    "name": row["name"],
                    "room": row["room"],
                    "on_count": row["on_count"],
                    "total_duration": round(row["total_duration"], 2),
                    "total_energy": round(row["total_energy"], 4),
                })

            return {
                "type": query_type,
                "period": period_label,
                "count": len(rankings),
                "rankings": rankings,
            }
        finally:
            conn.close()

    # ------------------------------------------------------------------ #
    #  内部：根据 entity_id 和 date_field 解析聚合查询的目标表                  #
    # ------------------------------------------------------------------ #
    def _resolve_aggregate_table(self, conn: sqlite3.Connection, entity_id: str, date_field: str) -> tuple:
        """返回 (table_name, actual_date_field)，未找到返回 (None, None)。"""
        if date_field == "on_time":
            # 查 device_history 表
            try:
                conn.execute(f"SELECT 1 FROM {TABLE_DEVICE_HISTORY} LIMIT 1")
                col_names = self._get_table_columns(conn, TABLE_DEVICE_HISTORY)
                if "on_time" in col_names:
                    return (TABLE_DEVICE_HISTORY, "on_time")
            except Exception:
                pass
        elif date_field == "datetime":
            # 需要确定具体的环境/属性表
            cursor = conn.execute(
                f"SELECT category, metric_type, attr_type FROM {TABLE_ENTITY_CONFIGS} WHERE entity_id = ?",
                (entity_id,),
            )
            row = cursor.fetchone()
            if row:
                category, metric_type, attr_type = row[0], row[1], row[2]
                if category == CATEGORY_ENVIRONMENT and metric_type:
                    tbl = get_env_table_name(metric_type)
                    try:
                        conn.execute(f"SELECT 1 FROM {tbl} LIMIT 1")
                        return (tbl, "datetime")
                    except Exception:
                        pass
                elif category == CATEGORY_ATTRIBUTE and attr_type:
                    tbl = get_attr_table_name(attr_type)
                    try:
                        conn.execute(f"SELECT 1 FROM {tbl} LIMIT 1")
                        return (tbl, "datetime")
                    except Exception:
                        pass
            return (None, None)
        else:
            # 自定义 date_field，尝试所有表
            tables = [(TABLE_DEVICE_HISTORY, date_field)]
            for metric in VALID_METRICS:
                tables.append((get_env_table_name(metric), date_field))
            cursor2 = conn.execute(f"SELECT type_name FROM {TABLE_ATTR_TYPE_DEFS}")
            for arow in cursor2.fetchall():
                tables.append((get_attr_table_name(arow[0]), date_field))
            for tbl, df in tables:
                try:
                    conn.execute(f"SELECT 1 FROM {tbl} LIMIT 1")
                    col_names = self._get_table_columns(conn, tbl)
                    if df in col_names:
                        return (tbl, df)
                except Exception:
                    continue
        return (None, None)

    # ------------------------------------------------------------------ #
    #  内部：获取表的列名列表                                                 #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _get_table_columns(conn: sqlite3.Connection, table_name: str) -> list:
        """返回指定表的列名列表。"""
        col_info = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        return [c[1] for c in col_info]

    # ------------------------------------------------------------------ #
    #  内部：计算设备汇总                                                    #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _calc_device_summary(conn: sqlite3.Connection, entity_id: str, pattern: str) -> dict:
        """计算设备汇总：on_count, total_energy, total_duration（仅计已关闭记录）。"""
        cursor = conn.execute(
            f"SELECT COUNT(*) AS on_count, "
            f"  COALESCE(SUM(energy_consumed), 0) AS total_energy, "
            f"  COALESCE(SUM(duration), 0) AS total_duration "
            f"FROM {TABLE_DEVICE_HISTORY} "
            f"WHERE entity_id = ? AND on_time LIKE ? "
            f"  AND off_time != '' AND off_time IS NOT NULL",
            (entity_id, pattern),
        )
        row = cursor.fetchone()
        if row:
            return {
                "on_count": row["on_count"],
                "total_energy": round(row["total_energy"], 2),
                "total_duration": round(row["total_duration"], 0),
            }
        return {"on_count": 0, "total_energy": 0, "total_duration": 0}

    # ------------------------------------------------------------------ #
    #  attr_history：属性历史记录（支持多选表，每个表独立节点）                    #
    # ------------------------------------------------------------------ #
    def _query_attr_history(self, db_path: str, request: web.Request) -> dict:
        params = self._extract_params(request)
        entity_id = params["entity_id"]
        room = params["room"]
        date = params["date"]
        limit = params["limit"] or 500
        attr_type_raw = request.query.get("attr_type", "").strip()
        order_by = request.query.get("order_by", "").strip()
        try:
            offset = int(request.query.get("offset", "0").strip())
        except ValueError:
            offset = 0
        fields_raw = request.query.get("fields", "").strip()

        if not entity_id and not room:
            raise ValueError("attr_history 需要 entity_id 或 room 参数")
        if not attr_type_raw:
            raise ValueError("attr_history 需要 attr_type 参数")

        # 支持逗号分隔的多类型
        attr_types = [a.strip() for a in attr_type_raw.split(",") if a.strip()]

        conn = sqlite3.connect(db_path)
        try:
            conn.row_factory = sqlite3.Row

            conditions = []
            sql_params: list = []
            if entity_id:
                conditions.append("entity_id = ?")
                sql_params.append(entity_id)
            if room:
                conditions.append("room = ?")
                sql_params.append(room)
            if date:
                conditions.append("datetime LIKE ?")
                sql_params.append(f"{date}%")

            where_clause = " AND ".join(conditions) if conditions else "1=1"

            records_by_table = {}
            all_tables = []
            grand_total = 0

            for attr_type in attr_types:
                tbl = get_attr_table_name(attr_type)
                all_tables.append(tbl)

                try:
                    # 获取列名
                    table_info = conn.execute(f"PRAGMA table_info({tbl})").fetchall()
                    col_names = [row[1] for row in table_info if row[1] != "id"]

                    cursor = conn.execute(
                        f"SELECT * FROM {tbl} WHERE {where_clause} "
                        f"ORDER BY datetime DESC LIMIT ?",
                        (*sql_params, limit),
                    )
                    batch = [dict(row) for row in cursor.fetchall()]

                    # 统计该表总条数
                    count_row = conn.execute(
                        f"SELECT COUNT(*) FROM {tbl} WHERE {where_clause}",
                        sql_params,
                    ).fetchone()
                    tbl_total = count_row[0] if count_row else 0

                    records_by_table[tbl] = {
                        "records": batch,
                        "total": tbl_total,
                        "columns": col_names,
                    }
                    grand_total += tbl_total
                except Exception:
                    records_by_table[tbl] = {
                        "records": [],
                        "total": 0,
                        "columns": [],
                        "error": "表不存在或查询失败",
                    }
                    _LOGGER.warning("[query] attr_history 表 %s 不存在或查询失败", tbl)

            return {
                "records": records_by_table,
                "total": grand_total,
                "tables": all_tables,
            }
        except Exception:
            return {"records": {}, "total": 0, "error": f"表查询失败: {', '.join(get_attr_table_name(a) for a in attr_types)}"}
        finally:
            conn.close()

    # ------------------------------------------------------------------ #
    #  attr_latest：最新一条属性记录                                        #
    # ------------------------------------------------------------------ #
    def _query_attr_latest(self, db_path: str, request: web.Request) -> dict:
        params = self._extract_params(request)
        entity_id = params["entity_id"]
        attr_type_raw = request.query.get("attr_type", "").strip()

        if not entity_id:
            raise ValueError("attr_latest 需要 entity_id 参数")
        if not attr_type_raw:
            raise ValueError("attr_latest 需要 attr_type 参数")

        # 支持逗号分隔的多类型
        attr_types = [a.strip() for a in attr_type_raw.split(",") if a.strip()]

        conn = sqlite3.connect(db_path)
        try:
            conn.row_factory = sqlite3.Row
            results: dict = {}
            for attr_type in attr_types:
                tbl = get_attr_table_name(attr_type)
                try:
                    cursor = conn.execute(
                        f"SELECT * FROM {tbl} WHERE entity_id = ? "
                        f"ORDER BY datetime DESC LIMIT 1",
                        (entity_id,),
                    )
                    row = cursor.fetchone()
                    results[attr_type] = dict(row) if row else None
                except Exception:
                    results[attr_type] = None
            return {"attr_types": attr_types, "results": results}
        finally:
            conn.close()

    # ------------------------------------------------------------------ #
    #  attr_latest：属性最新记录（兼容旧格式）                               #
    # ------------------------------------------------------------------ #
    def _query_attr_latest_single(self, db_path: str, request: web.Request) -> dict:
        params = self._extract_params(request)
        entity_id = params["entity_id"]
        attr_type = request.query.get("attr_type", "").strip()

        if not entity_id:
            raise ValueError("attr_latest 需要 entity_id 参数")
        if not attr_type:
            raise ValueError("attr_latest 需要 attr_type 参数")

        tbl = get_attr_table_name(attr_type)
        conn = sqlite3.connect(db_path)
        try:
            conn.row_factory = sqlite3.Row
            table_info = conn.execute(f"PRAGMA table_info({tbl})").fetchall()
            col_names = [row[1] for row in table_info if row[1] != "id"]

            cursor = conn.execute(
                f"SELECT * FROM {tbl} WHERE entity_id = ? "
                f"ORDER BY datetime DESC LIMIT 1",
                (entity_id,),
            )
            row = cursor.fetchone()
            return {"record": dict(row) if row else None, "columns": col_names}
        except Exception:
            return {"record": None, "error": f"表 {tbl} 可能不存在"}
        finally:
            conn.close()

    @staticmethod
    def _calc_device_summary_by_where(conn: sqlite3.Connection, where_clause: str, where_params: list, pattern: str) -> dict:
        """计算设备汇总：通过自定义 WHERE 条件过滤（支持 entity_id 和 room 组合）。"""
        cursor = conn.execute(
            f"SELECT COUNT(*) AS on_count, "
            f"  COALESCE(SUM(energy_consumed), 0) AS total_energy, "
            f"  COALESCE(SUM(duration), 0) AS total_duration "
            f"FROM {TABLE_DEVICE_HISTORY} "
            f"WHERE {where_clause} AND on_time LIKE ? "
            f"  AND off_time != '' AND off_time IS NOT NULL",
            (*where_params, pattern),
        )
        row = cursor.fetchone()
        if row:
            return {
                "on_count": row["on_count"],
                "total_energy": round(row["total_energy"], 2),
                "total_duration": round(row["total_duration"], 0),
            }
        return {"on_count": 0, "total_energy": 0, "total_duration": 0}

    # ------------------------------------------------------------------ #
    #  attr_history：属性历史记录 + 汇总（支持多选表）                            #
    # ------------------------------------------------------------------ #
    def _query_attr_history(self, db_path: str, request: web.Request) -> dict:
        params = self._extract_params(request)
        entity_id = params["entity_id"]
        room = params["room"]
        date = params["date"]
        limit = params["limit"] or 500
        attr_type_raw = request.query.get("attr_type", "").strip()
        order_by = request.query.get("order_by", "").strip()
        try:
            offset = int(request.query.get("offset", "0").strip())
        except ValueError:
            offset = 0
        fields_raw = request.query.get("fields", "").strip()

        if not entity_id and not room:
            raise ValueError("attr_history 需要 entity_id 或 room 参数")
        if not attr_type_raw:
            raise ValueError("attr_history 需要 attr_type 参数")

        # 支持逗号分隔的多类型
        attr_types = [a.strip() for a in attr_type_raw.split(",") if a.strip()]

        conn = sqlite3.connect(db_path)
        try:
            conn.row_factory = sqlite3.Row

            conditions = []
            sql_params: list = []
            if entity_id:
                conditions.append("entity_id = ?")
                sql_params.append(entity_id)
            if room:
                conditions.append("room = ?")
                sql_params.append(room)
            if date:
                conditions.append("datetime LIKE ?")
                sql_params.append(f"{date}%")

            where_clause = " AND ".join(conditions) if conditions else "1=1"

            all_records = []
            all_tables = []
            all_columns = set()
            combined_summary = {}

            for attr_type in attr_types:
                tbl = get_attr_table_name(attr_type)
                all_tables.append(tbl)

                try:
                    # 获取列名和类型
                    table_info = conn.execute(f"PRAGMA table_info({tbl})").fetchall()
                    col_names = [row[1] for row in table_info if row[1] != "id"]

                    # 字段过滤
                    safe_cols = {row[1] for row in table_info}
                    if fields_raw:
                        requested = [f.strip() for f in fields_raw.split(",") if f.strip() in safe_cols]
                        select_fields = ", ".join(f'"{f}"' for f in requested) if requested else "*"
                    else:
                        select_fields = "*"

                    # 排序
                    order_clause = "ORDER BY datetime DESC"
                    if order_by and order_by.lstrip("-") in safe_cols:
                        direction = "DESC" if order_by.startswith("-") else "ASC"
                        col = order_by.lstrip("-")
                        order_clause = f'ORDER BY "{col}" {direction}'

                    cursor = conn.execute(
                        f"SELECT {select_fields} FROM {tbl} WHERE {where_clause} "
                        f"{order_clause} LIMIT ? OFFSET ?",
                        (*sql_params, limit, offset),
                    )
                    batch = [dict(row) for row in cursor.fetchall()]
                    for rec in batch:
                        rec["_table"] = tbl
                    all_records.extend(batch)
                    all_columns.update(col_names)

                    # 汇总统计
                    numeric_cols = [
                        row[1] for row in table_info
                        if row[1] not in ("id", "entity_id", "name", "datetime", "room", "updated_at", "_table")
                        and row[2].upper() in ("REAL", "INTEGER")
                    ]
                    if numeric_cols:
                        agg_parts = []
                        for col in numeric_cols:
                            qn = f'"{col}"'
                            agg_parts.append(f"SUM({qn}) AS sum_{col}")
                            agg_parts.append(f"AVG({qn}) AS avg_{col}")
                            agg_parts.append(f"MIN({qn}) AS min_{col}")
                            agg_parts.append(f"MAX({qn}) AS max_{col}")
                        agg_sql = ", ".join(agg_parts)
                        agg_row = conn.execute(
                            f"SELECT COUNT(*) AS cnt, {agg_sql} FROM {tbl} WHERE {where_clause}",
                            sql_params,
                        ).fetchone()
                        if agg_row:
                            key = f"_{tbl}"
                            combined_summary[attr_type] = {}
                            for col in numeric_cols:
                                s_val = agg_row[f"sum_{col}"]
                                a_val = agg_row[f"avg_{col}"]
                                n_val = agg_row[f"min_{col}"]
                                x_val = agg_row[f"max_{col}"]
                                combined_summary[attr_type][col] = {
                                    "sum": round(float(s_val), 2) if s_val is not None else 0,
                                    "avg": round(float(a_val), 2) if a_val is not None else 0,
                                    "min": round(float(n_val), 2) if n_val is not None else 0,
                                    "max": round(float(x_val), 2) if x_val is not None else 0,
                                }
                except Exception:
                    _LOGGER.warning("[query] attr_history 表 %s 不存在或查询失败", tbl)

            total = len(all_records)
            tables_str = ", ".join(all_tables)

            return {
                "records": all_records, "total": total,
                "tables": all_tables, "table": tables_str,
                "columns": sorted(all_columns) if all_columns else [],
                "summary": combined_summary,
            }
        except Exception:
            return {"records": [], "total": 0, "error": f"表查询失败: {', '.join(get_attr_table_name(a) for a in attr_types)}"}
        finally:
            conn.close()

    # ------------------------------------------------------------------ #
    #  attr_latest：最新一条属性记录                                        #
    # ------------------------------------------------------------------ #
    def _query_attr_latest(self, db_path: str, request: web.Request) -> dict:
        params = self._extract_params(request)
        entity_id = params["entity_id"]
        attr_type_raw = request.query.get("attr_type", "").strip()

        if not entity_id:
            raise ValueError("attr_latest 需要 entity_id 参数")
        if not attr_type_raw:
            raise ValueError("attr_latest 需要 attr_type 参数")

        # 支持逗号分隔的多类型
        attr_types = [a.strip() for a in attr_type_raw.split(",") if a.strip()]

        conn = sqlite3.connect(db_path)
        try:
            conn.row_factory = sqlite3.Row
            results: dict = {}
            for attr_type in attr_types:
                tbl = get_attr_table_name(attr_type)
                try:
                    cursor = conn.execute(
                        f"SELECT * FROM {tbl} WHERE entity_id = ? "
                        f"ORDER BY datetime DESC LIMIT 1",
                        (entity_id,),
                    )
                    row = cursor.fetchone()
                    results[attr_type] = dict(row) if row else None
                except Exception:
                    results[attr_type] = None
            return {"attr_types": attr_types, "results": results}
        finally:
            conn.close()

    # ------------------------------------------------------------------ #
    #  attr_latest：属性最新记录（兼容旧格式）                               #
    # ------------------------------------------------------------------ #
    def _query_attr_latest_single(self, db_path: str, request: web.Request) -> dict:
        params = self._extract_params(request)
        entity_id = params["entity_id"]
        attr_type = request.query.get("attr_type", "").strip()

        if not entity_id:
            raise ValueError("attr_latest 需要 entity_id 参数")
        if not attr_type:
            raise ValueError("attr_latest 需要 attr_type 参数")

        tbl = get_attr_table_name(attr_type)
        conn = sqlite3.connect(db_path)
        try:
            conn.row_factory = sqlite3.Row
            table_info = conn.execute(f"PRAGMA table_info({tbl})").fetchall()
            col_names = [row[1] for row in table_info if row[1] != "id"]

            cursor = conn.execute(
                f"SELECT * FROM {tbl} WHERE entity_id = ? "
                f"ORDER BY datetime DESC LIMIT 1",
                (entity_id,),
            )
            row = cursor.fetchone()
            return {"record": dict(row) if row else None, "columns": col_names}
        except Exception:
            return {"record": None, "error": f"表 {tbl} 可能不存在"}
        finally:
            conn.close()

    # ------------------------------------------------------------------ #
    #  attr_history：属性历史记录（单表兼容旧格式，多表按表分组，支持start/end范围）#
    # ------------------------------------------------------------------ #
    def _query_attr_history(self, db_path: str, request: web.Request) -> dict:
        params = self._extract_params(request)
        entity_id = params["entity_id"]
        room = params["room"]
        date = params["date"]
        start = params["start"]
        end = params["end"]
        limit = params["limit"] or 500
        attr_type_raw = request.query.get("attr_type", "").strip()
        order_by = request.query.get("order_by", "").strip()
        try:
            offset = int(request.query.get("offset", "0").strip())
        except ValueError:
            offset = 0
        fields_raw = request.query.get("fields", "").strip()

        if not entity_id and not room:
            raise ValueError("attr_history 需要 entity_id 或 room 参数")
        if not attr_type_raw:
            raise ValueError("attr_history 需要 attr_type 参数")

        # 支持逗号分隔的多类型
        attr_types = [a.strip() for a in attr_type_raw.split(",") if a.strip()]

        conn = sqlite3.connect(db_path)
        try:
            conn.row_factory = sqlite3.Row

            conditions = []
            sql_params: list = []
            if entity_id:
                conditions.append("entity_id = ?")
                sql_params.append(entity_id)
            if room:
                conditions.append("room = ?")
                sql_params.append(room)
            if date:
                conditions.append("datetime LIKE ?")
                sql_params.append(f"{date}%")
            if start:
                conditions.append("datetime >= ?")
                sql_params.append(start)
            if end:
                conditions.append("datetime <= ?")
                sql_params.append(end + " 23:59:59")

            where_clause = " AND ".join(conditions) if conditions else "1=1"

            records_by_table = {}
            all_tables = []
            grand_total = 0

            for attr_type in attr_types:
                tbl = get_attr_table_name(attr_type)
                all_tables.append(tbl)

                try:
                    table_info = conn.execute(f"PRAGMA table_info({tbl})").fetchall()
                    col_names = [row[1] for row in table_info if row[1] != "id"]

                    cursor = conn.execute(
                        f"SELECT * FROM {tbl} WHERE {where_clause} "
                        f"ORDER BY datetime DESC LIMIT ?",
                        (*sql_params, limit),
                    )
                    batch = [dict(row) for row in cursor.fetchall()]

                    count_row = conn.execute(
                        f"SELECT COUNT(*) FROM {tbl} WHERE {where_clause}",
                        sql_params,
                    ).fetchone()
                    tbl_total = count_row[0] if count_row else 0

                    records_by_table[tbl] = {
                        "records": batch,
                        "total": tbl_total,
                        "columns": col_names,
                    }
                    grand_total += tbl_total
                except Exception:
                    records_by_table[tbl] = {
                        "records": [],
                        "total": 0,
                        "columns": [],
                        "error": "表不存在或查询失败",
                    }
                    _LOGGER.warning("[query] attr_history 表 %s 不存在或查询失败", tbl)

            # 单表保持旧格式兼容，多表按表分组
            is_multi = len(attr_types) > 1
            single_tbl = all_tables[0] if all_tables else ""

            if is_multi:
                return {
                    "records": records_by_table,
                    "total": grand_total,
                    "tables": all_tables,
                }
            else:
                tbl_info = records_by_table.get(single_tbl, {})
                return {
                    "records": tbl_info.get("records", []),
                    "total": tbl_info.get("total", 0),
                    "table": single_tbl,
                    "columns": tbl_info.get("columns", []),
                }
        except Exception:
            if len(attr_types) > 1:
                return {"records": {}, "total": 0, "tables": [get_attr_table_name(a) for a in attr_types], "error": f"表查询失败: {', '.join(get_attr_table_name(a) for a in attr_types)}"}
            tbl = get_attr_table_name(attr_types[0]) if attr_types else ""
            return {"records": [], "total": 0, "table": tbl, "columns": [], "error": f"表 {tbl} 可能不存在"}
        finally:
            conn.close()

    # ------------------------------------------------------------------ #
    #  attr_daily：按天返回指定月份的属性记录（仅单表）                         #
    # ------------------------------------------------------------------ #
    def _query_attr_daily(self, db_path: str, request: web.Request) -> dict:
        """返回指定月份每一天的属性记录分组。
        参数: attr_type(单表), entity_id, month(YYYY-MM), date_field(可选，默认从表列名自动检测)
        """
        params = self._extract_params(request)
        entity_id = params["entity_id"]
        month = params["month"]
        attr_type_raw = request.query.get("attr_type", "").strip()
        date_field = request.query.get("date_field", "").strip()

        if not entity_id:
            raise ValueError("attr_daily 需要 entity_id 参数")
        if not attr_type_raw:
            raise ValueError("attr_daily 需要 attr_type 参数")
        if not month:
            raise ValueError("attr_daily 需要 month 参数（格式：YYYY-MM）")

        import re
        if not re.match(r"^\d{4}-\d{2}$", month):
            raise ValueError("month 参数格式错误，应为 YYYY-MM")

        # 仅支持单表
        if "," in attr_type_raw:
            raise ValueError("attr_daily 仅支持单表查询，请指定一个 attr_type")
        attr_type = attr_type_raw.strip()
        tbl = get_attr_table_name(attr_type)

        conn = sqlite3.connect(db_path)
        try:
            conn.row_factory = sqlite3.Row

            # 从表结构自动检测日期字段
            table_info = conn.execute(f"PRAGMA table_info({tbl})").fetchall()
            if not table_info:
                return {
                    "entity_id": entity_id, "attr_type": attr_type,
                    "month": month, "table": tbl,
                    "daily_records": [], "error": f"表 {tbl} 不存在",
                }

            col_names = [row[1] for row in table_info if row[1] != "id"]

            if not date_field:
                # 优先 datetime，其次按常见日期字段名匹配
                if "datetime" in col_names:
                    date_field = "datetime"
                else:
                    candidates = [c for c in col_names if c in ("on_time", "date", "day", "created_at", "updated_at")]
                    if not candidates:
                        candidates = [c for c in col_names if "time" in c.lower() or "date" in c.lower()]
                    date_field = candidates[0] if candidates else col_names[0]
            elif date_field not in col_names:
                raise ValueError(f"日期字段 '{date_field}' 在表 {tbl} 中不存在，可选字段: {', '.join(col_names)}")

            pattern = f"{month}-%"
            cursor = conn.execute(
                f"SELECT * FROM {tbl} WHERE entity_id = ? AND {date_field} LIKE ? "
                f"ORDER BY {date_field} DESC",
                (entity_id, pattern),
            )
            all_rows = [dict(row) for row in cursor.fetchall()]

            daily: dict[str, list] = {}
            for row in all_rows:
                val = str(row.get(date_field, ""))
                day_key = val[:10] if len(val) >= 10 else val
                daily.setdefault(day_key, []).append(row)

            daily_records = []
            for day_key in sorted(daily.keys(), reverse=True):
                daily_records.append({
                    "date": day_key,
                    "records": daily[day_key],
                    "count": len(daily[day_key]),
                })

            # 取第一条记录的日期字段值作为参考
            sample_val = str(all_rows[0].get(date_field, ""))[:19] if all_rows else None

            return {
                "entity_id": entity_id,
                "attr_type": attr_type,
                "table": tbl,
                "month": month,
                "date_field": date_field,
                "daily_records": daily_records,
                "total_days": len(daily_records),
                "columns": col_names,
                "total_rows": len(all_rows),
                "sample_date_value": sample_val,
            }
        except Exception as exc:
            _LOGGER.exception("[query] attr_daily 查询异常")
            return {
                "entity_id": entity_id, "attr_type": attr_type,
                "month": month, "table": tbl,
                "daily_records": [], "error": str(exc),
            }
        finally:
            conn.close()

    # ------------------------------------------------------------------ #
    #  attr_latest：最新一条属性记录                                        #
    # ------------------------------------------------------------------ #
    def _query_attr_latest(self, db_path: str, request: web.Request) -> dict:
        params = self._extract_params(request)
        entity_id = params["entity_id"]
        attr_type_raw = request.query.get("attr_type", "").strip()

        if not entity_id:
            raise ValueError("attr_latest 需要 entity_id 参数")
        if not attr_type_raw:
            raise ValueError("attr_latest 需要 attr_type 参数")

        # 支持逗号分隔的多类型
        attr_types = [a.strip() for a in attr_type_raw.split(",") if a.strip()]

        conn = sqlite3.connect(db_path)
        try:
            conn.row_factory = sqlite3.Row
            results: dict = {}
            for attr_type in attr_types:
                tbl = get_attr_table_name(attr_type)
                try:
                    cursor = conn.execute(
                        f"SELECT * FROM {tbl} WHERE entity_id = ? "
                        f"ORDER BY datetime DESC LIMIT 1",
                        (entity_id,),
                    )
                    row = cursor.fetchone()
                    results[attr_type] = dict(row) if row else None
                except Exception:
                    results[attr_type] = None
            return {"attr_types": attr_types, "results": results}
        finally:
            conn.close()

    # ------------------------------------------------------------------ #
    #  attr_latest：属性最新记录（兼容旧格式）                               #
    # ------------------------------------------------------------------ #
    def _query_attr_latest_single(self, db_path: str, request: web.Request) -> dict:
        params = self._extract_params(request)
        entity_id = params["entity_id"]
        attr_type = request.query.get("attr_type", "").strip()

        if not entity_id:
            raise ValueError("attr_latest 需要 entity_id 参数")
        if not attr_type:
            raise ValueError("attr_latest 需要 attr_type 参数")

        tbl = get_attr_table_name(attr_type)
        conn = sqlite3.connect(db_path)
        try:
            conn.row_factory = sqlite3.Row
            table_info = conn.execute(f"PRAGMA table_info({tbl})").fetchall()
            col_names = [row[1] for row in table_info if row[1] != "id"]

            cursor = conn.execute(
                f"SELECT * FROM {tbl} WHERE entity_id = ? "
                f"ORDER BY datetime DESC LIMIT 1",
                (entity_id,),
            )
            row = cursor.fetchone()
            return {"record": dict(row) if row else None, "columns": col_names}
        except Exception:
            return {"record": None, "error": f"表 {tbl} 可能不存在"}
        finally:
            conn.close()

    # ------------------------------------------------------------------ #
    #  rooms_daily：某一天指定类型的全部房间数据                            #
    # ------------------------------------------------------------------ #
    def _query_rooms_daily(self, db_path: str, request: web.Request) -> dict:
        """返回某一天指定 category/type 的所有房间数据，按 room 分组。

        参数：
          - category: device | environment | attribute
          - date:     YYYY-MM-DD
          - attr_type: 属性类型名（category=attribute 时必需）
          - metric:    环境指标（category=environment 时必需）
          - limit:     每个房间最多返回条数（默认 1000）
        """
        category = request.query.get("category", "").strip().lower()
        date = request.query.get("date", "").strip()
        try:
            limit = int(request.query.get("limit", "1000").strip())
        except ValueError:
            limit = 1000

        if not category:
            raise ValueError("rooms_daily 需要 category 参数（device/environment/attribute）")
        if not date:
            raise ValueError("rooms_daily 需要 date 参数（YYYY-MM-DD 格式）")

        conn = sqlite3.connect(db_path)
        pattern = f"{date}%"
        try:
            conn.row_factory = sqlite3.Row

            if category == "device":
                # 查询 device_history 表
                cursor = conn.execute(
                    f"SELECT DISTINCT room FROM {TABLE_DEVICE_HISTORY} "
                    f"WHERE on_time LIKE ? AND room != '' ORDER BY room",
                    (pattern,),
                )
                all_rooms = [row["room"] for row in cursor.fetchall()]

                rooms_data = {}
                for room in all_rooms:
                    cursor = conn.execute(
                        f"SELECT * FROM {TABLE_DEVICE_HISTORY} "
                        f"WHERE room = ? AND on_time LIKE ? "
                        f"ORDER BY on_time ASC LIMIT ?",
                        (room, pattern, limit),
                    )
                    rooms_data[room] = [dict(row) for row in cursor.fetchall()]

                return {
                    "date": date,
                    "category": "device",
                    "rooms": rooms_data,
                    "room_list": all_rooms,
                    "total_rooms": len(all_rooms),
                    "total_records": sum(len(v) for v in rooms_data.values()),
                }

            elif category == "environment":
                metric_raw = request.query.get("metric", "").strip()
                if not metric_raw:
                    raise ValueError("category=environment 时需要 metric 参数")

                # 支持逗号分隔的多指标
                metrics = [m.strip() for m in metric_raw.split(",") if m.strip()]
                invalid = [m for m in metrics if m not in VALID_METRICS]
                if invalid:
                    raise ValueError(f"无效的 metric: {', '.join(invalid)}，可选: {', '.join(VALID_METRICS)}")

                # 收集所有房间（跨指标合并）
                all_rooms_set: set = set()
                for metric in metrics:
                    tbl = get_env_table_name(metric)
                    cursor = conn.execute(
                        f"SELECT DISTINCT room FROM {tbl} "
                        f"WHERE datetime LIKE ? AND room != ''",
                        (pattern,),
                    )
                    for row in cursor.fetchall():
                        all_rooms_set.add(row["room"])
                all_rooms = sorted(all_rooms_set)

                # 按 room → metric 查询
                rooms_data: dict = {}
                total_records = 0
                for room in all_rooms:
                    room_metrics: dict = {}
                    for metric in metrics:
                        tbl = get_env_table_name(metric)
                        cursor = conn.execute(
                            f"SELECT id, entity_id, name, datetime, value "
                            f"FROM {tbl} WHERE room = ? AND datetime LIKE ? "
                            f"ORDER BY datetime ASC LIMIT ?",
                            (room, pattern, limit),
                        )
                        records = [dict(row) for row in cursor.fetchall()]
                        room_metrics[metric] = records
                        total_records += len(records)
                    rooms_data[room] = room_metrics

                return {
                    "date": date,
                    "category": "environment",
                    "metrics": metrics,
                    "rooms": rooms_data,
                    "room_list": all_rooms,
                    "total_rooms": len(all_rooms),
                    "total_records": total_records,
                }

            elif category == "attribute":
                attr_type = request.query.get("attr_type", "").strip()
                if not attr_type:
                    raise ValueError("category=attribute 时需要 attr_type 参数")

                tbl = get_attr_table_name(attr_type)
                cursor = conn.execute(
                    f"SELECT DISTINCT room FROM {tbl} "
                    f"WHERE datetime LIKE ? AND room != '' ORDER BY room",
                    (pattern,),
                )
                all_rooms = [row["room"] for row in cursor.fetchall()]

                # 获取列名
                table_info = conn.execute(f"PRAGMA table_info({tbl})").fetchall()
                col_names = [row[1] for row in table_info if row[1] != "id"]

                rooms_data = {}
                total = 0
                for room in all_rooms:
                    cursor = conn.execute(
                        f"SELECT * FROM {tbl} WHERE room = ? AND datetime LIKE ? "
                        f"ORDER BY datetime ASC LIMIT ?",
                        (room, pattern, limit),
                    )
                    records = [dict(row) for row in cursor.fetchall()]
                    rooms_data[room] = records
                    total += len(records)

                return {
                    "date": date,
                    "category": "attribute",
                    "attr_type": attr_type,
                    "rooms": rooms_data,
                    "room_list": all_rooms,
                    "total_rooms": len(all_rooms),
                    "total_records": total,
                    "columns": col_names,
                }
            elif category == "vacuum_cleaner":
                # 按 vacuum_id 分组返回
                cursor = conn.execute(
                    f"SELECT DISTINCT vacuum_id FROM {TABLE_VACUUM_HISTORY} "
                    f"WHERE datetime LIKE ? AND vacuum_id != '' ORDER BY vacuum_id",
                    (pattern,),
                )
                all_vacuum_ids = [row["vacuum_id"] for row in cursor.fetchall()]
                rooms_data = {}
                total = 0
                for vid in all_vacuum_ids:
                    cursor = conn.execute(
                        f"SELECT * FROM {TABLE_VACUUM_HISTORY} "
                        f"WHERE vacuum_id = ? AND datetime LIKE ? "
                        f"ORDER BY seq ASC LIMIT 5000",
                        (vid, pattern),
                    )
                    records = [dict(row) for row in cursor.fetchall()]
                    rooms_data[vid] = records
                    total += len(records)
                return {
                    "date": date,
                    "category": "vacuum_cleaner",
                    "rooms": rooms_data,
                    "room_list": all_vacuum_ids,
                    "total_rooms": len(all_vacuum_ids),
                    "total_records": total,
                }
            else:
                raise ValueError(f"无效的 category: {category}，可选: device, environment, attribute, vacuum_cleaner")

        except sqlite3.OperationalError as exc:
            _LOGGER.warning("[rooms_daily] 表不存在或查询失败: %s", exc)
            return {"date": date, "category": category, "rooms": {}, "room_list": [], "total_rooms": 0, "total_records": 0, "error": str(exc)}
        finally:
            conn.close()


    # ------------------------------------------------------------------ #
    #  rooms_multi_metric：多指标按日按房间汇总                                 #
    # ------------------------------------------------------------------ #
    def _query_rooms_multi_metric(self, db_path: str, request: web.Request) -> dict:
        """返回指定日期所有房间的多个环境指标数据，按 room → metric 分组。

        参数：
          - date:    YYYY-MM-DD（必填）
          - metrics: 逗号分隔的指标列表，如 temperature,humidity（必填）
          - limit:   每个房间每种指标最多返回条数（默认 500）
        """
        date = request.query.get("date", "").strip()
        if not date:
            raise ValueError("rooms_multi_metric 需要 date 参数（YYYY-MM-DD 格式）")

        metrics_str = request.query.get("metrics", "").strip()
        if not metrics_str:
            raise ValueError("rooms_multi_metric 需要 metrics 参数，如: temperature,humidity,pm25")

        metrics = [m.strip() for m in metrics_str.split(",") if m.strip()]
        invalid = [m for m in metrics if m not in VALID_METRICS]
        if invalid:
            raise ValueError(
                f"无效的 metrics: {', '.join(invalid)}，可选: {', '.join(VALID_METRICS)}"
            )

        try:
            limit = int(request.query.get("limit", "500").strip())
        except ValueError:
            limit = 500

        pattern = f"{date}%"
        conn = sqlite3.connect(db_path)

        try:
            conn.row_factory = sqlite3.Row

            # 收集所有房间
            all_rooms_set: set = set()
            for metric in metrics:
                tbl = get_env_table_name(metric)
                cursor = conn.execute(
                    f"SELECT DISTINCT room FROM {tbl} WHERE datetime LIKE ? AND room != ''",
                    (pattern,),
                )
                for row in cursor.fetchall():
                    all_rooms_set.add(row["room"])

            all_rooms = sorted(all_rooms_set)

            # 按 room → metric 查询数据
            rooms_data: dict[str, dict[str, list]] = {}
            total_records = 0
            for room in all_rooms:
                room_metrics: dict[str, list] = {}
                for metric in metrics:
                    tbl = get_env_table_name(metric)
                    cursor = conn.execute(
                        f"SELECT id, entity_id, name, datetime, value "
                        f"FROM {tbl} WHERE room = ? AND datetime LIKE ? "
                        f"ORDER BY datetime ASC LIMIT ?",
                        (room, pattern, limit),
                    )
                    records = [dict(row) for row in cursor.fetchall()]
                    room_metrics[metric] = records
                    total_records += len(records)
                rooms_data[room] = room_metrics

            return {
                "date": date,
                "metrics": metrics,
                "rooms": rooms_data,
                "room_list": all_rooms,
                "total_rooms": len(all_rooms),
                "total_records": total_records,
            }

        except sqlite3.OperationalError as exc:
            _LOGGER.warning("[rooms_multi_metric] 查询失败: %s", exc)
            return {
                "date": date, "metrics": metrics, "rooms": {}, "room_list": [],
                "total_rooms": 0, "total_records": 0, "error": str(exc),
            }
        finally:
            conn.close()


    # ------------------------------------------------------------------ #
    #  vacuum_history：扫地机器人位置轨迹查询                                 #
    # ------------------------------------------------------------------ #
    def _query_vacuum_history(self, db_path: str, request: web.Request) -> dict:
        """查询扫地机器人位置轨迹。

        参数：
          - vacuum_id: 机器人ID（必填）
          - date:      YYYY-MM-DD（可选）
          - limit:     返回条数（默认 5000）
        """
        vacuum_id = request.query.get("vacuum_id", "").strip()
        date = request.query.get("date", "").strip()
        try:
            limit = int(request.query.get("limit", "5000").strip())
        except ValueError:
            limit = 5000

        if not vacuum_id:
            raise ValueError("vacuum_history 需要 vacuum_id 参数")

        conn = sqlite3.connect(db_path)
        try:
            conn.row_factory = sqlite3.Row

            conditions = ["vacuum_id = ?"]
            sql_params: list = [vacuum_id]

            if date:
                conditions.append("datetime LIKE ?")
                sql_params.append(f"{date}%")

            where_clause = " AND ".join(conditions)

            cursor = conn.execute(
                f"SELECT * FROM {TABLE_VACUUM_HISTORY} "
                f"WHERE {where_clause} ORDER BY seq ASC LIMIT ?",
                (*sql_params, limit),
            )
            records = [dict(row) for row in cursor.fetchall()]

            # 获取列名
            table_info = conn.execute(f"PRAGMA table_info({TABLE_VACUUM_HISTORY})").fetchall()
            col_names = [row[1] for row in table_info if row[1] != "id"]

            count_row = conn.execute(
                f"SELECT COUNT(*) FROM {TABLE_VACUUM_HISTORY} WHERE {where_clause}",
                sql_params,
            ).fetchone()
            total = count_row[0] if count_row else 0

            return {
                "records": records,
                "total": total,
                "columns": col_names,
                "vacuum_id": vacuum_id,
            }
        finally:
            conn.close()


    # ------------------------------------------------------------------ #
    #  electricity_standard：标准电费数据                                   #
    # ------------------------------------------------------------------ #
    def _query_electricity_standard(self, db_path: str, request: web.Request) -> dict:
        """返回标准电费数据，包含 state、attributes（daylist/monthlist/yearlist/计费标准等）。"""
        entity_id = request.query.get("entity_id", "").strip()
        if not entity_id:
            raise ValueError("electricity_standard 需要 entity_id 参数")

        day_table = request.query.get("day_table", "attr_ele_day").strip()
        month_table = request.query.get("month_table", "attr_ele_month").strip()
        year_table = request.query.get("year_table", "attr_ele_year").strip()

        # ★ translated 字段来源：默认 dayEleCost，可通过参数指定任意字段
        translated_field = request.query.get("translated_field", "dayEleCost").strip()

        try:
            day_limit = int(request.query.get("day_limit", "0").strip())
        except ValueError:
            day_limit = 0
        try:
            month_limit = int(request.query.get("month_limit", "0").strip())
        except ValueError:
            month_limit = 0
        try:
            year_limit = int(request.query.get("year_limit", "0").strip())
        except ValueError:
            year_limit = 0

        debug = request.query.get("debug", "").strip().lower() in ("true", "1", "yes")

        conn = sqlite3.connect(db_path)
        try:
            conn.row_factory = sqlite3.Row

            # ---- 辅助：获取表的列名 ----
            def _get_columns(tbl_name: str) -> list[str]:
                try:
                    info = conn.execute(f"PRAGMA table_info({tbl_name})").fetchall()
                    return [row[1] for row in info]
                except Exception:
                    return []

            # ---- 1. 从日表获取最新一条记录（按 day 降序，获取最新日期的数据） ----
            table_missing = False
            try:
                cursor = conn.execute(
                    f"SELECT * FROM {day_table} WHERE entity_id = ? ORDER BY day DESC LIMIT 1",
                    (entity_id,),
                )
                latest_row = cursor.fetchone()
            except Exception:
                latest_row = None
                table_missing = True

            if table_missing:
                raise ValueError(f"表 {day_table} 不存在，请检查表名是否正确（属性表通常带 attr_ 前缀，如 attr_ele_day）")

            if not latest_row:
                raise ValueError(f"在表 {day_table} 中未找到 entity_id={entity_id} 的记录，请确认实体ID和表名是否正确")

            latest = dict(latest_row)

            # ---- 2. 单独获取有计费标准数据的最新记录 ----
            # 日表的最新记录可能计费标准字段为空，需要找有计费标准数据的最新记录
            billing_prefix = "计费标准_"
            day_columns = _get_columns(day_table)
            billing_columns = [c for c in day_columns if c.startswith(billing_prefix)]

            billing_latest = {}
            if billing_columns:
                # 构建：至少一个计费标准字段非空的查询
                billing_non_empty_conditions = " OR ".join(
                    f"({c} IS NOT NULL AND {c} != '')" for c in billing_columns
                )
                try:
                    cursor = conn.execute(
                        f"SELECT * FROM {day_table} WHERE entity_id = ? "
                        f"AND ({billing_non_empty_conditions}) "
                        f"ORDER BY day DESC LIMIT 1",
                        (entity_id,),
                    )
                    billing_row = cursor.fetchone()
                    if billing_row:
                        billing_latest = dict(billing_row)
                except Exception:
                    pass

            # 如果找到了有计费标准的记录，用它覆盖计费标准相关字段
            billing_source = billing_latest if billing_latest else latest

            # ---- 3. 构建 state 和时间戳 ----
            raw_value = str(latest.get(translated_field, "")) if translated_field else ""
            updated_at = latest.get("updated_at", "")
            last_changed = updated_at
            last_updated = updated_at
            if updated_at:
                try:
                    dt = datetime.strptime(updated_at, "%Y-%m-%d %H:%M:%S")
                    iso = dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"
                    last_changed = iso
                    last_updated = iso
                except (ValueError, TypeError):
                    pass

            # ---- 4. 构建 daylist ----
            daylist = []
            try:
                day_sql = (
                    f"SELECT day, dayEleNum, dayEleCost, dayTPq, dayPPq, dayNPq, dayVPq "
                    f"FROM {day_table} WHERE entity_id = ? ORDER BY day DESC"
                )
                day_params: list = [entity_id]
                if day_limit > 0:
                    day_sql += " LIMIT ?"
                    day_params.append(day_limit)
                cursor = conn.execute(day_sql, day_params)
                for row in cursor.fetchall():
                    daylist.append(dict(row))
            except Exception:
                pass

            # ---- 5. 构建 monthlist ----
            monthlist = []
            month_error = None
            actual_month_table = month_table
            try:
                month_sql = (
                    f"SELECT month, monthEleNum, monthEleCost, monthTPq, monthPPq, monthNPq, monthVPq "
                    f"FROM {month_table} WHERE entity_id = ? ORDER BY month DESC"
                )
                month_params: list = [entity_id]
                if month_limit > 0:
                    month_sql += " LIMIT ?"
                    month_params.append(month_limit)
                cursor = conn.execute(month_sql, month_params)
                for row in cursor.fetchall():
                    monthlist.append(dict(row))
            except Exception:
                # 尝试去掉或加上 attr_ 前缀
                fallback = month_table.replace("attr_", "", 1) if month_table.startswith("attr_") else f"attr_{month_table}"
                try:
                    month_sql = (
                        f"SELECT month, monthEleNum, monthEleCost, monthTPq, monthPPq, monthNPq, monthVPq "
                        f"FROM {fallback} WHERE entity_id = ? ORDER BY month DESC"
                    )
                    month_params: list = [entity_id]
                    if month_limit > 0:
                        month_sql += " LIMIT ?"
                        month_params.append(month_limit)
                    cursor = conn.execute(month_sql, month_params)
                    for row in cursor.fetchall():
                        monthlist.append(dict(row))
                    actual_month_table = fallback
                except Exception:
                    month_error = f"表 {month_table} 查询失败"

            # ---- 6. 构建 yearlist ----
            yearlist = []
            year_error = None
            actual_year_table = year_table
            try:
                year_sql = (
                    f"SELECT year, yearEleNum, yearEleCost, yearTPq, yearPPq, yearNPq, yearVPq "
                    f"FROM {year_table} WHERE entity_id = ? ORDER BY year DESC"
                )
                year_params: list = [entity_id]
                if year_limit > 0:
                    year_sql += " LIMIT ?"
                    year_params.append(year_limit)
                cursor = conn.execute(year_sql, year_params)
                for row in cursor.fetchall():
                    yearlist.append(dict(row))
            except Exception:
                # 尝试去掉或加上 attr_ 前缀
                fallback = year_table.replace("attr_", "", 1) if year_table.startswith("attr_") else f"attr_{year_table}"
                try:
                    year_sql = (
                        f"SELECT year, yearEleNum, yearEleCost, yearTPq, yearPPq, yearNPq, yearVPq "
                        f"FROM {fallback} WHERE entity_id = ? ORDER BY year DESC"
                    )
                    year_params: list = [entity_id]
                    if year_limit > 0:
                        year_sql += " LIMIT ?"
                        year_params.append(year_limit)
                    cursor = conn.execute(year_sql, year_params)
                    for row in cursor.fetchall():
                        yearlist.append(dict(row))
                    actual_year_table = fallback
                except Exception:
                    year_error = f"表 {year_table} 查询失败"

            # ---- 7. 构建 计费标准（从有计费标准数据的最新记录） ----
            billing_standard = {}
            # 优先从独立列读取（旧方式：计费标准_xxx 列）
            for key, value in billing_source.items():
                if key.startswith(billing_prefix):
                    sub_key = key[len(billing_prefix):]
                    if value is not None and str(value).strip() != "":
                        billing_standard[sub_key] = value

            # 如果独立列无数据，尝试从 extra_json 列读取（新方式：JSON 节点）
            if not billing_standard:
                extra_json_str = billing_source.get("extra_json", "")
                if extra_json_str:
                    try:
                        extra_json_data = json.loads(extra_json_str)
                        if isinstance(extra_json_data, dict) and "计费标准" in extra_json_data:
                            billing_val = extra_json_data["计费标准"]
                            if isinstance(billing_val, dict):
                                billing_standard = billing_val
                    except (json.JSONDecodeError, TypeError):
                        pass

            # 如果最新行无计费标准，查找有 extra_json 计费标准数据的最新记录
            if not billing_standard:
                try:
                    # 查找 extra_json 中包含"计费标准"的最新记录
                    cursor = conn.execute(
                        f"SELECT * FROM {day_table} WHERE entity_id = ? "
                        f"AND extra_json LIKE '%计费标准%' "
                        f"ORDER BY day DESC LIMIT 1",
                        (entity_id,),
                    )
                    extra_billing_row = cursor.fetchone()
                    if extra_billing_row:
                        extra_json_str = dict(extra_billing_row).get("extra_json", "")
                        if extra_json_str:
                            try:
                                extra_json_data = json.loads(extra_json_str)
                                if isinstance(extra_json_data, dict) and "计费标准" in extra_json_data:
                                    billing_val = extra_json_data["计费标准"]
                                    if isinstance(billing_val, dict):
                                        billing_standard = billing_val
                            except (json.JSONDecodeError, TypeError):
                                pass
                except Exception:
                    pass

            # ---- 8. 构建返回结果（直接返回扁平 attributes，不嵌套 state/attributes） ----
            result = {
                "translated": raw_value if raw_value else "",
                "raw": raw_value,
                "last_changed": last_changed,
                "last_updated": last_updated,
                "日均消费": billing_source.get("日均消费"),
                "剩余天数": billing_source.get("剩余天数"),
                "预付费": billing_source.get("预付费"),
                "date": billing_source.get("date"),
                "daylist": daylist,
                "monthlist": monthlist,
                "yearlist": yearlist,
            }

            if billing_standard:
                result["计费标准"] = billing_standard

            if billing_source.get("数据源") is not None and str(billing_source["数据源"]).strip() != "":
                result["数据源"] = billing_source["数据源"]
            if billing_source.get("最后同步日期") is not None and str(billing_source["最后同步日期"]).strip() != "":
                result["最后同步日期"] = billing_source["最后同步日期"]

            result["unit_of_measurement"] = "元"
            result["icon"] = "mdi:flash"
            if billing_source.get("name"):
                result["friendly_name"] = billing_source["name"]

            # 警告信息
            warnings = []
            if month_error:
                warnings.append(month_error)
            if year_error:
                warnings.append(year_error)
            if warnings:
                result["warnings"] = warnings

            # debug 模式：返回诊断信息
            if debug:
                extra_json_val = latest.get("extra_json", "")
                extra_json_parsed = None
                if extra_json_val:
                    try:
                        extra_json_parsed = list(json.loads(extra_json_val).keys()) if extra_json_val else None
                    except Exception:
                        pass
                result["_debug"] = {
                    "day_table": day_table,
                    "day_columns": day_columns,
                    "billing_columns": billing_columns,
                    "latest_record_keys": list(latest.keys()),
                    "latest_has_billing": any(
                        v is not None and str(v).strip() != ""
                        for k, v in latest.items() if k.startswith(billing_prefix)
                    ),
                    "latest_extra_json_nodes": extra_json_parsed,
                    "billing_standard_source": "columns" if any(k.startswith(billing_prefix) for k in billing_source if billing_source.get(k)) else "extra_json",
                    "billing_source_from": "billing_latest" if billing_latest else "latest",
                    "billing_latest_has_data": bool(billing_latest),
                    "month_table_actual": actual_month_table,
                    "year_table_actual": actual_year_table,
                    "monthlist_count": len(monthlist),
                    "yearlist_count": len(yearlist),
                }

            return result
        finally:
            conn.close()

    # ------------------------------------------------------------------
    #  健康查询
    # ------------------------------------------------------------------
    def _query_health_history(self, db_path: str, request: web.Request) -> dict:
        """查询健康记录。参数: name, type, start, end, limit, offset, order_by"""
        name = request.query.get("name", "").strip()
        health_type = request.query.get("health_type", "").strip()
        start = request.query.get("start", "").strip()
        end = request.query.get("end", "").strip()
        try:
            limit = int(request.query.get("limit", "100").strip())
        except ValueError:
            limit = 100
        try:
            offset = int(request.query.get("offset", "0").strip())
        except ValueError:
            offset = 0
        order_by = request.query.get("order_by", "").strip()

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            where_clauses: list[str] = []
            params: list = []

            if name:
                where_clauses.append("name = ?")
                params.append(name)
            if health_type:
                where_clauses.append("type = ?")
                params.append(health_type)
            if start:
                where_clauses.append("date_time >= ?")
                params.append(start)
            if end:
                where_clauses.append("date_time <= ?")
                params.append(end + " 23:59:59")

            # 排序
            safe_cols = {"date_time", "name", "type", "dp", "sp", "pr", "height", "weight", "bmi", "temp"}
            if order_by and order_by.lstrip("-") in safe_cols:
                desc = "DESC" if order_by.startswith("-") else "ASC"
                col = order_by.lstrip("-")
                order_clause = f'ORDER BY "{col}" {desc}'
            else:
                order_clause = "ORDER BY date_time DESC"

            if where_clauses:
                where_sql = " WHERE " + " AND ".join(where_clauses)
                count_sql = f'SELECT COUNT(*) FROM "{TABLE_HEALTH_RECORDS}"{where_sql}'
                data_sql = f'SELECT * FROM "{TABLE_HEALTH_RECORDS}"{where_sql} {order_clause} LIMIT ? OFFSET ?'
                total = conn.execute(count_sql, tuple(params)).fetchone()[0]
                rows = conn.execute(data_sql, tuple(params) + (limit, offset)).fetchall()
            else:
                count_sql = f'SELECT COUNT(*) FROM "{TABLE_HEALTH_RECORDS}"'
                data_sql = f'SELECT * FROM "{TABLE_HEALTH_RECORDS}" {order_clause} LIMIT ? OFFSET ?'
                total = conn.execute(count_sql).fetchone()[0]
                rows = conn.execute(data_sql, (limit, offset)).fetchall()

            return {
                "rows": [dict(r) for r in rows],
                "total": total,
                "limit": limit,
                "offset": offset,
            }
        finally:
            conn.close()

    def _query_health_latest(self, db_path: str, request: web.Request) -> dict:
        """查询某人最新健康记录。参数: name"""
        name = request.query.get("name", "").strip()
        if not name:
            raise ValueError("health_latest 需要 name 参数")

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                f'SELECT * FROM "{TABLE_HEALTH_RECORDS}" WHERE name = ? ORDER BY date_time DESC LIMIT 1',
                (name,),
            ).fetchone()
            return dict(row) if row else {}
        finally:
            conn.close()


# ========================================================================== #
#  7. ★ 数据库浏览器 — DBViewerDataView (数据API) ★                            #
#     挂载路径: GET /api/device_energy/db_viewer/data                          #
#     参数: table, page                                                        #
# ========================================================================== #
class DBViewerDataView(_BaseDBView):
    """数据库浏览器数据 API：返回表列表、分页数据。"""

    url = "/api/ha_data_store/db_viewer/data"
    name = "api:ha_data_store:db_viewer_data"

    async def get(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_master_switch(hass)):
            return resp
        db_path = self._db_path
        table = request.query.get("table", "").strip()
        try:
            page = int(request.query.get("page", "1").strip())
            if page < 1:
                page = 1
        except ValueError:
            page = 1

        order_by = request.query.get("order_by", "").strip()
        order_dir = request.query.get("order_dir", "DESC").strip().upper()
        if order_dir not in ("ASC", "DESC"):
            order_dir = "DESC"

        filter_raw = request.query.get("filter", "").strip()

        per_page = 100

        def _query() -> dict:
            conn = sqlite3.connect(db_path)
            try:
                conn.row_factory = sqlite3.Row

                # 获取所有表名
                cursor = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                )
                tables = [row[0] for row in cursor.fetchall()]

                result: dict[str, Any] = {"tables": tables}

                if not table or table not in tables:
                    return result

                # 获取列名（构建安全白名单）
                cursor = conn.execute(f'PRAGMA table_info("{table}")')
                table_info_rows = cursor.fetchall()
                columns = [row[1] for row in table_info_rows]
                safe_cols = set(columns)

                # 解析筛选条件
                filter_conditions: list[str] = []
                filter_params: list = []
                if filter_raw:
                    try:
                        filters = json.loads(filter_raw)
                    except (json.JSONDecodeError, TypeError):
                        filters = []

                    for f in filters:
                        col = f.get("col", "")
                        op = f.get("op", "eq")
                        val = f.get("val", "")
                        if col not in safe_cols:
                            continue

                        qn = f'"{col}"'
                        if op == "eq":
                            filter_conditions.append(f"{qn} = ?")
                            filter_params.append(val)
                        elif op == "neq":
                            filter_conditions.append(f"{qn} != ?")
                            filter_params.append(val)
                        elif op == "contains":
                            filter_conditions.append(f"{qn} LIKE ?")
                            filter_params.append(f"%{val}%")
                        elif op == "gt":
                            filter_conditions.append(f"{qn} > ?")
                            filter_params.append(val)
                        elif op == "lt":
                            filter_conditions.append(f"{qn} < ?")
                            filter_params.append(val)
                        elif op == "gte":
                            filter_conditions.append(f"{qn} >= ?")
                            filter_params.append(val)
                        elif op == "lte":
                            filter_conditions.append(f"{qn} <= ?")
                            filter_params.append(val)
                        elif op == "null":
                            filter_conditions.append(f"{qn} IS NULL")
                        elif op == "notnull":
                            filter_conditions.append(f"{qn} IS NOT NULL")
                            # 忽略其他未知操作符

                filter_clause = (" AND ".join(filter_conditions)) if filter_conditions else "1=1"

                # 获取总行数和筛选后行数
                cursor = conn.execute(f'SELECT COUNT(*) FROM "{table}"')
                total_count = cursor.fetchone()[0]

                if filter_conditions:
                    cursor = conn.execute(
                        f'SELECT COUNT(*) FROM "{table}" WHERE {filter_clause}',
                        filter_params,
                    )
                    filtered_count = cursor.fetchone()[0]
                else:
                    filtered_count = total_count

                # 确定排序字段和方向
                final_order_by = order_by
                final_order_dir = order_dir

                # 默认排序：attr_* 表优先按 key_field DESC，否则按 rowid DESC
                if not final_order_by and table.startswith("attr_") and table != TABLE_ATTR_TYPE_DEFS:
                    type_name = table[5:]  # 去掉 "attr_" 前缀
                    row = conn.execute(
                        f"SELECT key_field FROM {TABLE_ATTR_TYPE_DEFS} WHERE type_name = ?",
                        (type_name,),
                    ).fetchone()
                    if row and row[0] and row[0] in safe_cols:
                        final_order_by = row[0]
                        final_order_dir = "DESC"

                # 校验排序字段
                if final_order_by and final_order_by in safe_cols:
                    order_clause = f'ORDER BY "{final_order_by}" {final_order_dir}'
                else:
                    final_order_by = ""
                    final_order_dir = ""
                    order_clause = "ORDER BY rowid DESC"

                # 分页查询
                offset = (page - 1) * per_page
                cursor = conn.execute(
                    f'SELECT rowid AS _rowid, * FROM "{table}" '
                    f'WHERE {filter_clause} {order_clause} LIMIT ? OFFSET ?',
                    (*filter_params, per_page, offset),
                )
                rows = [dict(row) for row in cursor.fetchall()]

                total_pages = (filtered_count + per_page - 1) // per_page if filtered_count > 0 else 1

                result["table"] = table
                result["columns"] = columns
                result["rows"] = rows
                result["page"] = page
                result["total_pages"] = total_pages
                result["total_count"] = total_count
                result["filtered_count"] = filtered_count
                result["order_by"] = final_order_by
                result["order_dir"] = final_order_dir

                return result
            finally:
                conn.close()

        try:
            hass: HomeAssistant = request.app["hass"]
            if (resp := self._check_api_enabled(request)):
                return resp
            if (resp := self._check_db_viewer_enabled(hass)):
                return resp
            data = await self._exec_in_executor(hass, _query)
            return self.json({"success": True, "data": data})
        except Exception as exc:
            _LOGGER.exception("数据库浏览器查询失败")
            return self.json({"success": False, "error": str(exc)}, status_code=500)


# ========================================================================== #
#  8. ★ 数据库浏览器 — 单元格更新 API ★                                         #
#     挂载路径: POST /api/ha_data_store/db_viewer/update                        #
#     参数: { table, row_id, column, value }                                    #
# ========================================================================== #
class DBViewerUpdateView(_BaseDBView):
    """数据库浏览器单元格更新 API。"""

    url = "/api/ha_data_store/db_viewer/update"
    name = "api:ha_data_store:db_viewer_update"

    async def post(self, request: web.Request) -> web.Response:
        db_path = self._db_path
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

        table = body.get("table", "").strip()
        try:
            row_id = int(body.get("row_id", 0))
        except (ValueError, TypeError):
            return self.json({"success": False, "error": "row_id 无效"}, status_code=400)
        column = body.get("column", "").strip()
        value = body.get("value")

        if not table or not column or row_id <= 0:
            return self.json({"success": False, "error": "缺少 table / row_id / column 参数"}, status_code=400)

        # NULL 特殊处理：value 为 None 或空字符串时设为 None
        if value is None:
            value = None
        elif isinstance(value, str):
            value = value.strip()
            if value == "" and column == "state_attr":
                value = '[]'  # state_attr 是 NOT NULL，空值用空数组代替

        # state_attr 是 NOT NULL 列，null 要转为空数组
        if value is None and column == "state_attr":
            value = '[]'

        def _update() -> None:
            conn = sqlite3.connect(db_path)
            local_log = _log_local()
            try:
                # 安全校验：表名必须在 sqlite_master 中
                tables = [r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )]
                if table not in tables:
                    raise ValueError(f"表 '{table}' 不存在")

                # 安全校验：列名必须在表结构中
                cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
                if column not in cols:
                    raise ValueError(f"列 '{column}' 不存在于表 '{table}' 中")

                # 读取旧值
                old_row = conn.execute(
                    f'SELECT "{column}" FROM "{table}" WHERE rowid = ?', (row_id,)
                ).fetchone()
                old_value = old_row[0] if old_row else None

                # 执行参数化 UPDATE
                conn.execute(
                    f'UPDATE "{table}" SET "{column}" = ? WHERE rowid = ?',
                    (value, row_id),
                )
                conn.commit()

                if local_log:
                    local_log.warning(
                        "[db_edit] 单元格修改 table=%s rowid=%d column=%s old=%s new=%s",
                        table, row_id, column, old_value, value,
                    )
            finally:
                conn.close()

        try:
            await self._exec_in_executor(hass, _update)
            return self.json({"success": True, "message": f"{table}.{column} 已更新"})
        except ValueError as exc:
            return self.json({"success": False, "error": str(exc)}, status_code=400)
        except Exception as exc:
            _LOGGER.exception("数据库更新失败")
            return self.json({"success": False, "error": str(exc)}, status_code=500)

    async def delete(self, request: web.Request) -> web.Response:
        """DELETE /api/ha_data_store/db_viewer/update?table=xxx&row_id=123 → 删除整行。
           也支持批量删除: ?table=xxx&row_ids=1,2,3"""
        db_path = self._db_path
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_api_enabled(request)):
            return resp
        if (resp := self._check_db_viewer_enabled(hass)):
            return resp
        if (resp := self._check_db_edit_enabled(hass)):
            return resp

        table = request.query.get("table", "").strip()
        row_ids_str = request.query.get("row_ids", "").strip()

        if not table:
            return self.json({"success": False, "error": "缺少 table 参数"}, status_code=400)

        # 保护核心配置表，不允许通过数据库浏览器删除
        _PROTECTED_TABLES = {
            TABLE_ENTITY_CONFIGS, TABLE_ATTR_TYPE_DEFS, TABLE_CUSTOM_ROUTES,
            TABLE_EXPORT_CONFIGS, TABLE_FILE_SOURCE_CONFIGS, TABLE_API_SOURCE_CONFIGS,
            TABLE_API_KEYS, TABLE_API_SETTINGS, TABLE_VACUUM_TYPE_DEFS, TABLE_VACUUM_CONFIGS,
            TABLE_PUSH_TARGETS,
        }
        if table in _PROTECTED_TABLES:
            return self.json(
                {"success": False, "error": f"核心配置表 '{table}' 不允许通过数据库浏览器删除，请使用系统配置页面操作"},
                status_code=400,
            )

        # 批量删除模式
        if row_ids_str:
            try:
                row_ids = [int(x.strip()) for x in row_ids_str.split(",") if x.strip()]
            except (ValueError, TypeError):
                return self.json({"success": False, "error": "row_ids 格式无效"}, status_code=400)

            if not row_ids:
                return self.json({"success": False, "error": "row_ids 为空"}, status_code=400)

            def _batch_delete() -> int:
                conn = sqlite3.connect(db_path)
                local_log = _log_local()
                deleted = 0
                try:
                    tables = [r[0] for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )]
                    if table not in tables:
                        raise ValueError(f"表 '{table}' 不存在")

                    conn.row_factory = sqlite3.Row
                    placeholders = ",".join("?" for _ in row_ids)
                    conn.execute(
                        f'DELETE FROM "{table}" WHERE rowid IN ({placeholders})',
                        row_ids,
                    )
                    deleted = conn.total_changes
                    conn.commit()

                    if local_log:
                        local_log.warning(
                            "[db_edit] 批量删除 table=%s rowids=%s count=%d",
                            table, str(row_ids)[:200], deleted,
                        )
                finally:
                    conn.close()
                return deleted

            try:
                deleted_count = await self._exec_in_executor(hass, _batch_delete)
                return self.json({
                    "success": True,
                    "deleted_count": deleted_count,
                    "message": f"已批量删除 {deleted_count} 行",
                })
            except ValueError as exc:
                return self.json({"success": False, "error": str(exc)}, status_code=400)
            except Exception as exc:
                _LOGGER.exception("数据库批量删除失败")
                return self.json({"success": False, "error": str(exc)}, status_code=500)

        # 单行删除模式
        try:
            row_id = int(request.query.get("row_id", "0"))
        except (ValueError, TypeError):
            return self.json({"success": False, "error": "row_id 无效"}, status_code=400)

        if row_id <= 0:
            return self.json({"success": False, "error": "缺少 row_id 参数"}, status_code=400)

        def _delete() -> None:
            conn = sqlite3.connect(db_path)
            local_log = _log_local()
            try:
                tables = [r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )]
                if table not in tables:
                    raise ValueError(f"表 '{table}' 不存在")

                # 读取整行数据用于日志记录
                conn.row_factory = sqlite3.Row
                old_row = conn.execute(
                    f'SELECT * FROM "{table}" WHERE rowid = ?', (row_id,)
                ).fetchone()
                row_preview = ""
                if old_row:
                    row_preview = str(dict(old_row))[:200]

                conn.execute(f'DELETE FROM "{table}" WHERE rowid = ?', (row_id,))
                conn.commit()

                if local_log:
                    local_log.warning(
                        "[db_edit] 整行删除 table=%s rowid=%d data=%s",
                        table, row_id, row_preview or "(空)",
                    )
            finally:
                conn.close()

        try:
            await self._exec_in_executor(hass, _delete)
            return self.json({"success": True, "message": f"已删除 {table} 中 rowid={row_id} 的行"})
        except ValueError as exc:
            return self.json({"success": False, "error": str(exc)}, status_code=400)
        except Exception as exc:
            _LOGGER.exception("数据库行删除失败")
            return self.json({"success": False, "error": str(exc)}, status_code=500)


# ========================================================================== #
#  9. ★ 数据库浏览器 — DBViewerView (HTML页面) ★                               #
#     挂载路径: GET /api/device_energy/db_viewer                               #
# ========================================================================== #
class DBViewerView(_BaseDBView):
    """数据库浏览器 HTML 页面（同网段 + 登录保护）。"""

    url = "/api/ha_data_store/db_viewer"
    name = "api:ha_data_store:db_viewer"

    async def get(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        # 总开关
        if not hass.data.get(DOMAIN, {}).get("api_enabled", True):
            return web.Response(status=403, content_type="text/html; charset=utf-8")
        if not hass.data.get(DOMAIN, {}).get("db_viewer_enabled", True):
            return web.Response(status=403, content_type="text/html; charset=utf-8")
        # 同网段检查（可通过开关放行）
        if not hass.data.get(DOMAIN, {}).get("allow_remote_access", False):
            client_ip = _get_client_ip(request)
            ha_subnet = _get_ha_subnet(request)
            client_subnet = client_ip.rsplit(".", 1)[0] if "." in client_ip else ""
            if not client_subnet or client_subnet != ha_subnet:
                return web.Response(status=403, content_type="text/html; charset=utf-8")
        # 登录检查
        token = request.cookies.get("hds_auth", "")
        if token == _make_auth_token(self._db_path):
            html = await _load_db_viewer_html(hass)
            # 注入第一个 API Key 到 JS 全局变量
            first_key = ""
            try:
                conn = sqlite3.connect(self._db_path)
                row = conn.execute(
                    f"SELECT key FROM {TABLE_API_KEYS} WHERE enabled = 1 ORDER BY id LIMIT 1"
                ).fetchone()
                if row: first_key = row[0]
                conn.close()
            except Exception:
                pass
            if first_key:
                inject = 'window.__HDS_FIRST_KEY__="' + first_key + '";\n'
                html = html.replace("<script>\n// ==============================", "<script>\n" + inject + "// ==============================")
            return web.Response(text=html, content_type="text/html", charset="utf-8")
        # 未登录 → 返回登录页
        error = request.query.get("error", "")
        login_html = _LOGIN_HTML.replace("{error}", error)
        return web.Response(text=login_html, content_type="text/html", charset="utf-8")


# ========================================================================== #
#  DBViewer 登录接口                                                           #
# ========================================================================== #
class DBViewerLoginView(_BaseDBView):
    """数据库浏览器登录验证。"""

    url = "/api/ha_data_store/db_viewer/login"
    name = "api:ha_data_store:db_viewer_login"

    async def post(self, request: web.Request) -> web.Response:
        db_path = self._db_path
        try:
            body = await request.post()
            pw = body.get("password", "")
        except Exception:
            return web.Response(text="Invalid request", status=400)
        # 校验密码
        stored = ""
        try:
            conn = sqlite3.connect(db_path)
            row = conn.execute(
                f"SELECT svalue FROM {TABLE_API_SETTINGS} WHERE skey='admin_password'"
            ).fetchone()
            if row: stored = row[0]
            conn.close()
        except Exception:
            pass
        if pw and pw == stored:
            token = _make_auth_token(db_path)
            resp = web.Response(status=302, headers={"Location": "/api/ha_data_store/db_viewer"})
            resp.set_cookie("hds_auth", token, max_age=86400, httponly=True, samesite="Strict")
            return resp
        return web.Response(
            status=302,
            headers={"Location": "/api/ha_data_store/db_viewer?error=" + "密码错误"},
        )


# ========================================================================== #
#  9. ★ 日志查看器 — LogDataView (数据API) ★                                     #
#     挂载路径: GET /api/ha_data_store/logs/data                               #
#     参数: date (可选，指定日期返回日志内容; 不指定返回日志文件列表)                #
# ========================================================================== #
class LogDataView(_BaseDBView):
    """日志查看数据 API：列出日志文件或读取指定日期日志内容。"""

    url = "/api/ha_data_store/logs/data"
    name = "api:ha_data_store:logs_data"

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__("")  # db_path not needed
        self._hass = hass

    async def get(self, request: web.Request) -> web.Response:
        from .logger import get_logger as _lg

        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_master_switch(hass)):
            return resp

        local_logger = _lg()
        if not local_logger:
            return self.json(
                {"success": False, "error": "日志系统未初始化"},
                status_code=500,
            )

        date = request.query.get("date", "").strip()
        if not date:
            # 返回日志文件列表（扔线程池避免阻塞事件循环）
            try:
                files = await self._hass.async_add_executor_job(local_logger.get_log_files)
                return self.json({"success": True, "data": {"files": files}})
            except Exception as exc:
                return self.json({"success": False, "error": str(exc)}, status_code=500)

        # 读取指定日期日志内容
        try:
            content = await self._hass.async_add_executor_job(local_logger.read_log_content, date)
            if content is None:
                return self.json(
                    {"success": False, "error": f"日志文件 {date}.log 不存在"},
                    status_code=404,
                )
            return self.json({"success": True, "data": {"date": date, "content": content}})
        except Exception as exc:
            return self.json({"success": False, "error": str(exc)}, status_code=500)

    async def delete(self, request: web.Request) -> web.Response:
        """DELETE /api/ha_data_store/logs/data -> 删除全部日志文件。"""
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_master_switch(hass)):
            return resp
        if (resp := self._check_db_viewer_enabled(hass)):
            return resp
        if (resp := self._check_db_edit_enabled(hass)):
            return resp

        import glob
        import os as _os

        from .logger import get_logger as _log_local

        local_logger = _log_local()
        if not local_logger:
            return self.json({"success": False, "error": "日志系统未初始化"}, status_code=500)

        def _clear() -> int:
            log_dir = local_logger._log_dir
            count = 0
            for f in glob.glob(_os.path.join(log_dir, "*.log")):
                try:
                    _os.remove(f)
                    count += 1
                except OSError:
                    pass
            return count

        try:
            count = await self._exec_in_executor(self._hass, _clear)
            return self.json({"success": True, "message": f"已删除 {count} 个日志文件"})
        except Exception as exc:
            return self.json({"success": False, "error": str(exc)}, status_code=500)


# ========================================================================== #
# 10. ★ 实体监控 — EntityMonitorView (数据API) ★                                #
#     挂载路径: GET /api/ha_data_store/monitor                                 #
#     返回所有已启用实体的当前状态 + 最后记录时间 + 汇总                          #
# ========================================================================== #
class EntityMonitorView(_BaseDBView):
    """实体监控 API：返回所有启用实体的实时状态快照。"""

    url = "/api/ha_data_store/monitor"
    name = "api:ha_data_store:monitor"

    def __init__(self, db_path: str, hass: HomeAssistant) -> None:
        super().__init__(db_path)
        self._hass = hass

    async def get(self, request: web.Request) -> web.Response:
        db_path = self._db_path

        def _query() -> dict:
            conn = sqlite3.connect(db_path)
            try:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    f"SELECT entity_id, enabled, category, metric_type, attr_type, "
                    f"  collect_interval, collect_mode, power_entity, friendly_name, device_name, room "
                    f"FROM {TABLE_ENTITY_CONFIGS} WHERE enabled = 1 "
                    f"ORDER BY category, entity_id"
                )
                configs = [dict(row) for row in cursor.fetchall()]
            finally:
                conn.close()

            entities = []
            summary = {"total": 0, "online": 0, "offline": 0, "unavailable": 0}
            tz = self._hass.data.get(DOMAIN, {}).get("timezone", DEFAULT_TIMEZONE)

            for cfg in configs:
                entity_id = cfg["entity_id"]
                state_obj = self._hass.states.get(entity_id)
                state_val = state_obj.state if state_obj else "unavailable"

                # 状态判定
                if state_val in ("unavailable", "unknown", None):
                    status = "unavailable"
                    state_label = "不可用"
                elif cfg["category"] == "device":
                    if state_val in ("on", "open", "heat", "cool", "auto", "dry", "fan_only"):
                        status = "online"
                        state_label = "运行中"
                    elif state_val in ("off", "closed"):
                        status = "offline"
                        state_label = "已关闭"
                    else:
                        status = "online"
                        state_label = state_val
                elif cfg["category"] == CATEGORY_ATTRIBUTE:
                    # 属性提取类：只要实体可用就算在线
                    status = "online"
                    state_label = f"数值: {state_val}"
                else:
                    # 传感器类
                    metric_type = cfg.get("metric_type", "")
                    if metric_type == "sensor":
                        # sensor 类型：任何有效 state 都算在线
                        status = "online"
                        state_label = str(state_val)
                    else:
                        try:
                            float(state_val)
                            status = "online"
                            state_label = f"数值: {state_val}"
                        except (ValueError, TypeError):
                            status = "unavailable"
                            state_label = "不可用"

                entities.append({
                    "entity_id": entity_id,
                    "name": cfg.get("device_name", "") or cfg.get("friendly_name", "") or (state_obj.attributes.get("friendly_name", "") if state_obj else ""),
                    "category": cfg["category"],
                    "category_label": "设备类" if cfg["category"] == "device" else ("属性提取" if cfg["category"] == CATEGORY_ATTRIBUTE else "传感器类"),
                    "metric_type": cfg.get("metric_type", ""),
                    "attr_type": cfg.get("attr_type", ""),
                    "collect_mode": cfg.get("collect_mode", ""),
                    "room": cfg.get("room", ""),
                    "state": state_val,
                    "state_label": state_label,
                    "status": status,
                    "last_updated": "",
                    "collect_interval": cfg.get("collect_interval", 30),
                    "power_entity": cfg.get("power_entity", ""),
                })
                summary["total"] += 1
                if status == "online":
                    summary["online"] += 1
                elif status == "offline":
                    summary["offline"] += 1
                else:
                    summary["unavailable"] += 1

            # -- 查询属性类型定义（补 array_path / key_field）--
            attr_defs_lookup: dict = {}
            conn3 = sqlite3.connect(db_path)
            try:
                conn3.row_factory = sqlite3.Row
                ad_rows = conn3.execute(
                    f"SELECT type_name, mode, array_path, key_field, compare_limit FROM {TABLE_ATTR_TYPE_DEFS}"
                ).fetchall()
                for r in ad_rows:
                    attr_defs_lookup[r["type_name"]] = dict(r)
                for ent in entities:
                    if ent["category"] == CATEGORY_ATTRIBUTE and ent.get("attr_type"):
                        ad = attr_defs_lookup.get(ent["attr_type"])
                        if ad:
                            ent["attr_mode"] = ad.get("mode", "")
                            ent["array_path"] = ad.get("array_path", "")
                            ent["key_field"] = ad.get("key_field", "")
                            ent["compare_limit"] = ad.get("compare_limit", 30)

                # -- 查询各实体在数据库中的最新数据时间 --
                for ent in entities:
                    eid = ent["entity_id"]
                    cat = ent["category"]
                    try:
                        if cat == "device":
                            row = conn3.execute(
                                f"SELECT COALESCE(NULLIF(off_time,''), on_time) AS last_time "
                                f"FROM {TABLE_DEVICE_HISTORY} WHERE entity_id = ? "
                                f"ORDER BY id DESC LIMIT 1",
                                (eid,),
                            ).fetchone()
                            if row and row["last_time"]:
                                ent["last_updated"] = row["last_time"]
                        elif cat == "environment":
                            metric = ent.get("metric_type", "")
                            if metric and metric in VALID_METRICS:
                                tbl = get_env_table_name(metric)
                                row = conn3.execute(
                                    f"SELECT MAX(datetime) AS last_time FROM {tbl} "
                                    f"WHERE entity_id = ?",
                                    (eid,),
                                ).fetchone()
                                if row and row["last_time"]:
                                    ent["last_updated"] = row["last_time"]
                        elif cat == CATEGORY_ATTRIBUTE:
                            atype = ent.get("attr_type", "")
                            if atype:
                                tbl = get_attr_table_name(atype)
                                row = conn3.execute(
                                    f"SELECT MAX(datetime) AS last_time FROM {tbl} "
                                    f"WHERE entity_id = ?",
                                    (eid,),
                                ).fetchone()
                                if row and row["last_time"]:
                                    ent["last_updated"] = row["last_time"]
                    except Exception:
                        pass
            finally:
                conn3.close()

            # -- 实体导出 --
            exports = []
            try:
                conn2 = sqlite3.connect(db_path)
                conn2.row_factory = sqlite3.Row
                exp_rows = conn2.execute(
                    f"SELECT * FROM {TABLE_EXPORT_CONFIGS} WHERE enabled = 1 ORDER BY entity_id"
                ).fetchall()
                conn2.close()
                for row in exp_rows:
                    r = dict(row)
                    eid = r["entity_id"]
                    st = self._hass.states.get(eid)
                    # 获取导出文件的实际写入时间
                    export_last_time = ""
                    fname = r.get("file_name", "") or f"{eid.replace('.', '_')}.json"
                    fpath = os.path.join("/config", "storage", "export_entities", fname)
                    try:
                        if os.path.isfile(fpath):
                            mtime = os.path.getmtime(fpath)
                            export_last_time = (datetime.fromtimestamp(mtime) + timedelta(hours=tz)).strftime("%Y-%m-%d %H:%M:%S")
                    except Exception:
                        pass
                    exports.append(dict(
                        entity_id=eid, file_name=r["file_name"],
                        state=st.state if st else "unavailable",
                        status="在线" if (st and st.state not in ("unavailable","unknown")) else "不可用",
                        updated_at=export_last_time,
                    ))
            except Exception:
                pass

            # -- 文件源 --
            file_sources = []
            try:
                conn2 = sqlite3.connect(db_path)
                conn2.row_factory = sqlite3.Row
                fs_rows = conn2.execute(
                    f"SELECT * FROM {TABLE_FILE_SOURCE_CONFIGS} WHERE enabled = 1 ORDER BY id"
                ).fetchall()
                conn2.close()
                for row in fs_rows:
                    r = dict(row)
                    # 将 last_mtime (float) 转为可读时间
                    fs_last_time = ""
                    try:
                        lm = r.get("last_mtime")
                        if lm:
                            fs_last_time = (datetime.fromtimestamp(float(lm)) + timedelta(hours=tz)).strftime("%Y-%m-%d %H:%M:%S")
                    except Exception:
                        pass
                    file_sources.append(dict(
                        id=r["id"], name=r.get("name",""), file_path=r["file_path"],
                        entity_prefix=r["entity_prefix"], poll_interval=r["poll_interval"],
                        last_mtime=r["last_mtime"],
                        updated_at=fs_last_time,
                    ))
            except Exception:
                pass

            # -- API 源 --
            api_sources = []
            try:
                conn2 = sqlite3.connect(db_path)
                conn2.row_factory = sqlite3.Row
                as_rows = conn2.execute(
                    f"SELECT * FROM {TABLE_API_SOURCE_CONFIGS} WHERE enabled = 1 ORDER BY id"
                ).fetchall()
                conn2.close()
                for row in as_rows:
                    r = dict(row)
                    api_sources.append(dict(
                        id=r["id"], name=r.get("name",""), url=r["url"],
                        entity_prefix=r["entity_prefix"], poll_interval=r["poll_interval"],
                        fail_count=r["fail_count"],
                        updated_at=r.get("updated_at",""),
                    ))
            except Exception:
                pass

            # 构建类型健康度
            types = {}
            # 设备类
            d_ents = [e for e in entities if e["category"] == "device"]
            d_online = sum(1 for e in d_ents if e["status"] == "online")
            d_unavail = sum(1 for e in d_ents if e["status"] == "unavailable")
            types["device"] = dict(
                count=len(d_ents), online=d_online, offline=len(d_ents)-d_online-d_unavail,
                unavailable=d_unavail,
                health="good" if len(d_ents)==0 or d_unavail==0 else ("warn" if d_unavail<len(d_ents) else "bad"),
            )
            # 传感器类
            e_ents = [e for e in entities if e["category"] == "environment"]
            e_online = sum(1 for e in e_ents if e["status"] == "online")
            e_unavail = sum(1 for e in e_ents if e["status"] == "unavailable")
            types["environment"] = dict(
                count=len(e_ents), online=e_online, unavailable=e_unavail,
                health="good" if len(e_ents)==0 or e_unavail==0 else ("warn" if e_unavail<len(e_ents) else "bad"),
            )
            # 属性提取
            a_ents = [e for e in entities if e["category"] == CATEGORY_ATTRIBUTE]
            a_online = sum(1 for e in a_ents if e["status"] == "online")
            a_unavail = sum(1 for e in a_ents if e["status"] == "unavailable")
            types["attribute"] = dict(
                count=len(a_ents), online=a_online, unavailable=a_unavail,
                health="good" if len(a_ents)==0 or a_unavail==0 else ("warn" if a_unavail<len(a_ents) else "bad"),
            )
            # 实体导出：检查实体在线 + 导出文件存在
            exp_bad = 0
            export_dir = os.path.join("/config", "storage", "export_entities")
            for e in exports:
                fname = e.get("file_name", "") or f"{e['entity_id'].replace('.', '_')}.json"
                fpath = os.path.join(export_dir, fname)
                file_exists = os.path.isfile(fpath)
                ent_ok = e["status"] == "在线"
                if not ent_ok or not file_exists:
                    exp_bad += 1
            types["export"] = dict(
                count=len(exports), bad=exp_bad, ok=len(exports)-exp_bad,
                health="good" if len(exports)==0 or exp_bad==0 else ("warn" if exp_bad<len(exports) else "bad"),
            )
            # 文件源：检查文件是否存在 + 通过 entity_registry 查生成的实体状态
            from homeassistant.helpers import entity_registry as _er
            entity_reg = _er.async_get(self._hass)
            fs_info = []
            fs_bad = 0
            for fs in file_sources:
                cfg_id = fs["id"]
                fpath = fs["file_path"]
                exists = os.path.isfile(fpath) if fpath else False
                # 通过 unique_id 查找生成的实体
                ent_ok = True
                uid_prefix = f"file_src_{cfg_id}"
                for ent in entity_reg.entities.values():
                    if ent.unique_id.startswith(uid_prefix) and ent.entity_id:
                        st = self._hass.states.get(ent.entity_id)
                        if st and st.state in ("unavailable", "unknown"):
                            ent_ok = False
                            break
                is_bad = (not exists) or (not ent_ok)
                if is_bad:
                    fs_bad += 1
                fs_info.append(dict(exists=exists, path=fpath, name=fs.get("name",""),
                                    prefix=fs.get("entity_prefix",""), ent_ok=ent_ok))
            types["file_source"] = dict(
                count=len(file_sources), bad=fs_bad,
                health="good" if len(file_sources)==0 or fs_bad==0 else ("warn" if fs_bad<len(file_sources) else "bad"),
                files=fs_info,
            )
            # API 源：检查 fail_count + 通过 entity_registry 查生成的实体状态
            api_src_info = []
            api_bad = 0
            for a in api_sources:
                cfg_id = a["id"]
                has_fail = int(a.get("fail_count", 0)) > 0
                ent_ok = True
                uid_prefix = f"api_src_{cfg_id}"
                for ent in entity_reg.entities.values():
                    if ent.unique_id.startswith(uid_prefix) and ent.entity_id:
                        st = self._hass.states.get(ent.entity_id)
                        if st and st.state in ("unavailable", "unknown"):
                            ent_ok = False
                            break
                is_bad = has_fail or (not ent_ok)
                if is_bad:
                    api_bad += 1
                api_src_info.append(dict(has_fail=has_fail, ent_ok=ent_ok, prefix=a.get("entity_prefix","")))
            types["api_source"] = dict(
                count=len(api_sources), bad=api_bad, ok=len(api_sources)-api_bad,
                health="good" if len(api_sources)==0 or api_bad==0 else ("warn" if api_bad<len(api_sources) else "bad"),
                sources=api_src_info,
            )

            # -- 实体→网络 推送目标 --
            push_targets = []
            try:
                conn2 = sqlite3.connect(db_path)
                conn2.row_factory = sqlite3.Row
                pt_rows = conn2.execute(
                    f"SELECT * FROM {TABLE_PUSH_TARGETS} WHERE enabled = 1 ORDER BY entity_id"
                ).fetchall()
                conn2.close()
                for row in pt_rows:
                    r = dict(row)
                    eid = r["entity_id"]
                    st = self._hass.states.get(eid)
                    push_targets.append(dict(
                        entity_id=eid, name=r.get("name", eid), body_mode=r.get("body_mode", "full"),
                        push_token=r.get("push_token", ""),
                        status="在线" if (st and st.state not in ("unavailable","unknown")) else "不可用",
                        state=st.state if st else "N/A",
                        updated_at=r.get("updated_at",""),
                    ))
            except Exception:
                pass
            pt_bad = sum(1 for p in push_targets if p["status"] != "在线")
            types["push_target"] = dict(
                count=len(push_targets), bad=pt_bad, ok=len(push_targets)-pt_bad,
                health="good" if len(push_targets)==0 or pt_bad==0 else ("warn" if pt_bad<len(push_targets) else "bad"),
            )

            return {"entities": entities, "summary": summary,
                    "exports": exports, "file_sources": file_sources, "api_sources": api_sources,
                    "push_targets": push_targets,
                    "types": types}

        try:
            hass: HomeAssistant = request.app["hass"]
            if (resp := self._check_api_enabled(request)):
                return resp
            data = await self._exec_in_executor(hass, _query)
            return self.json({"success": True, "data": data})
        except Exception as exc:
            _LOGGER.exception("实体监控查询失败")
            return self.json({"success": False, "error": str(exc)}, status_code=500)


# ========================================================================== #
# 11. ★ 实体状态查询 — EntityStateView ★                                      #
#     GET /api/ha_data_store/entity_state?entity_id=xxx                      #
#     返回实体的完整 state + attributes（属性树预览用）                          #
# ========================================================================== #
class EntityStateView(_BaseDBView):
    """获取指定实体的当前状态和属性（用于前端属性提取配置的字段选择器）。"""

    url = "/api/ha_data_store/entity_state"
    name = "api:ha_data_store:entity_state"

    def __init__(self, db_path: str, hass: HomeAssistant) -> None:
        super().__init__(db_path)
        self._hass = hass

    async def get(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_api_enabled(request)):
            return resp

        entity_id = request.query.get("entity_id", "").strip()
        if not entity_id:
            return self.json({"success": False, "error": "缺少 entity_id 参数"}, status_code=400)

        state_obj = hass.states.get(entity_id)
        if not state_obj:
            return self.json({"success": False, "error": f"实体 {entity_id} 不存在"}, status_code=404)

        try:
            tz = hass.data.get(DOMAIN, {}).get("timezone", DEFAULT_TIMEZONE)

            # 构建属性树：扁平化 + 标注数组 + 展开dict子字段
            attr_tree = []
            attrs = dict(state_obj.attributes)
            for key, value in attrs.items():
                node: dict[str, Any] = {"path": key, "type": type(value).__name__}
                if isinstance(value, list):
                    node["type"] = "list"
                    node["length"] = len(value)
                    if value and isinstance(value[0], dict):
                        node["first_element"] = {
                            k: type(v).__name__ for k, v in value[0].items()
                        }
                elif isinstance(value, dict):
                    node["type"] = "dict"
                    node["keys"] = list(value.keys())
                    # 展开 dict 子字段，供 extra_fields 或 fields 模式选择
                    for sub_key, sub_value in value.items():
                        sub_node: dict[str, Any] = {"path": f"{key}.{sub_key}"}
                        if isinstance(sub_value, dict):
                            sub_node["type"] = "dict"
                            sub_node["keys"] = list(sub_value.keys())
                            # 二级展开
                            for sub2_key, sub2_value in sub_value.items():
                                sub2_node: dict[str, Any] = {"path": f"{key}.{sub_key}.{sub2_key}"}
                                if isinstance(sub2_value, (str, int, float, bool)) or sub2_value is None:
                                    sub2_node["type"] = type(sub2_value).__name__ if sub2_value is not None else "NoneType"
                                    sub2_node["value"] = sub2_value
                                else:
                                    sub2_node["type"] = type(sub2_value).__name__
                                attr_tree.append(sub2_node)
                        elif isinstance(sub_value, list):
                            sub_node["type"] = "list"
                            sub_node["length"] = len(sub_value)
                        else:
                            sub_node["type"] = type(sub_value).__name__ if sub_value is not None else "NoneType"
                            sub_node["value"] = sub_value
                        attr_tree.append(sub_node)
                else:
                    node["value"] = value
                attr_tree.append(node)

            return self.json({
                "success": True,
                "data": {
                    "entity_id": entity_id,
                    "state": state_obj.state,
                    "last_changed": (
                        state_obj.last_changed.isoformat() if state_obj.last_changed else ""
                    ),
                    "last_updated": (
                        state_obj.last_updated.isoformat() if state_obj.last_updated else ""
                    ),
                    "attributes": attrs,
                    "attribute_tree": attr_tree,
                },
            })
        except Exception as exc:
            _LOGGER.exception("获取实体状态失败")
            return self.json({"success": False, "error": str(exc)}, status_code=500)


# ========================================================================== #
# 12. ★ 属性类型列表 — AttrTypesView ★                                       #
#     GET /api/ha_data_store/attr_types                                      #
#     返回所有属性类型定义                                                      #
# ========================================================================== #
class AttrTypesView(_BaseDBView):
    """获取所有属性类型定义。"""

    url = "/api/ha_data_store/attr_types"
    name = "api:ha_data_store:attr_types"

    async def get(self, request: web.Request) -> web.Response:
        db_path = self._db_path

        def _query() -> list[dict]:
            conn = sqlite3.connect(db_path)
            try:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(
                    f"SELECT type_name, mode, array_path, key_field, compare_limit, "
                    f"  field_mapping, extra_fields, description, created_at, updated_at "
                    f"FROM {TABLE_ATTR_TYPE_DEFS} ORDER BY type_name"
                )
                rows = [dict(row) for row in cursor.fetchall()]

                # 解析 field_mapping JSON
                for row in rows:
                    fm = row.get("field_mapping", "")
                    if isinstance(fm, str) and fm:
                        try:
                            row["field_mapping"] = json.loads(fm)
                        except json.JSONDecodeError:
                            row["field_mapping"] = {}
                    # 解析 extra_fields JSON
                    ef = row.get("extra_fields", "")
                    if isinstance(ef, str) and ef:
                        try:
                            row["extra_fields"] = json.loads(ef)
                        except json.JSONDecodeError:
                            row["extra_fields"] = {}
                    elif not ef:
                        row["extra_fields"] = {}

                # 补充每个类型的实体数和更新方式
                for row in rows:
                    cursor2 = conn.execute(
                        f"SELECT entity_id, collect_mode, collect_interval FROM {TABLE_ENTITY_CONFIGS} "
                        f"WHERE enabled = 1 AND category = ? AND attr_type = ?",
                        (CATEGORY_ATTRIBUTE, row["type_name"]),
                    )
                    rows2 = cursor2.fetchall()
                    entity_ids = [r[0] for r in rows2]
                    row["entity_ids"] = entity_ids
                    row["entity_count"] = len(entity_ids)
                    # 取第一个实体的采集方式（用于显示）
                    if rows2:
                        row["collect_mode"] = rows2[0]["collect_mode"] or ""
                        row["collect_interval"] = rows2[0]["collect_interval"] or 30
                    else:
                        row["collect_mode"] = ""
                        row["collect_interval"] = 30

                return rows
            finally:
                conn.close()

        try:
            hass: HomeAssistant = request.app["hass"]
            if (resp := self._check_api_enabled(request)):
                return resp
            data = await self._exec_in_executor(hass, _query)
            # 附加上次触发统计
            stats = hass.data.get(DOMAIN, {}).get("_attr_trigger_stats", {})
            for row in data:
                tn = row["type_name"]
                st = stats.get(tn, {})
                row["last_trigger_count"] = st.get("count", 0)
                row["last_trigger_time"] = st.get("time", "")
            return self.json({"success": True, "data": data})
        except Exception as exc:
            _LOGGER.exception("获取属性类型列表失败")
            return self.json({"success": False, "error": str(exc)}, status_code=500)


# ========================================================================== #
# 13. ★ 属性配置管理 — AttrConfigView ★                                      #
#     POST /api/ha_data_store/attr_config                                    #
#     一站式创建：写入 attr_type_defs + 建 attr_* 表 + 写入 entity_configs      #
# ========================================================================== #
def _normalize_extra_fields_api(extra_fields) -> dict:
    """将 extra_fields 统一为 {"src_path": {"target_col": "xxx"}} 格式。

    兼容旧格式: {"src_path": "target_col"} → {"target_col": "target_col"}。
    新格式:     {"src_path": {"target_col": "xxx", ...}} → 取 target_col。
    """
    if not extra_fields or not isinstance(extra_fields, dict):
        return {}
    result = {}
    for src_path, value in extra_fields.items():
        if isinstance(value, str):
            result[src_path] = {"target_col": value}
        elif isinstance(value, dict):
            target_col = value.get("target_col", src_path.replace(".", "_"))
            result[src_path] = {"target_col": target_col}
        else:
            result[src_path] = {"target_col": str(value)}
    return result


class TableColumnsView(_BaseDBView):
    """获取指定数据表的列名。"""

    url = "/api/ha_data_store/table_columns"
    name = "api:ha_data_store:table_columns"

    async def get(self, request: web.Request) -> web.Response:
        db_path = self._db_path
        table = request.query.get("table", "").strip()
        if not table:
            return self.json({"success": False, "error": "缺少 table 参数"}, status_code=400)

        import sqlite3
        try:
            conn = sqlite3.connect(db_path)
            try:
                cursor = conn.execute(f"PRAGMA table_info(`{table}`)")
                columns = [{"cid": r[0], "name": r[1], "type": r[2]} for r in cursor.fetchall()]
                return self.json({"success": True, "data": columns})
            finally:
                conn.close()
        except Exception as exc:
            return self.json({"success": False, "error": str(exc)}, status_code=500)


class AttrConfigView(_BaseDBView):
    """属性提取配置管理。"""

    url = "/api/ha_data_store/attr_config"
    name = "api:ha_data_store:attr_config"

    async def post(self, request: web.Request) -> web.Response:
        db_path = self._db_path
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_master_switch(hass)):
            return resp
        if (resp := self._check_db_edit_enabled(hass)):
            return resp

        tz = hass.data.get(DOMAIN, {}).get("timezone", DEFAULT_TIMEZONE)
        now = _get_local_iso(tz)

        try:
            body = await request.json()
        except Exception:
            return self.json({"success": False, "error": "请求体不是合法的 JSON"}, status_code=400)

        type_name = body.get("type_name", "").strip()
        mode = body.get("mode", ATTR_MODE_FIELDS)
        entity_id = body.get("entity_id", "").strip()
        array_path = body.get("array_path", "").strip()
        key_field = body.get("key_field", "").strip()
        compare_limit = int(body.get("compare_limit", 30))
        field_mapping = body.get("field_mapping", {})
        field_types = body.get("field_types", {})
        extra_fields = body.get("extra_fields", {})
        extra_json_nodes = body.get("extra_json_nodes", [])
        decimal_places = int(body.get("decimal_places", 2))
        collect_interval = int(body.get("collect_interval", 30))
        collect_mode = body.get("collect_mode", "poll").strip()
        room = body.get("room", "").strip()

        if not type_name:
            return self.json({"success": False, "error": "type_name 不能为空"}, status_code=400)
        if not entity_id:
            return self.json({"success": False, "error": "entity_id 不能为空"}, status_code=400)
        if not field_mapping:
            return self.json({"success": False, "error": "field_mapping 不能为空"}, status_code=400)
        if mode not in (ATTR_MODE_FIELDS, ATTR_MODE_LIST, ATTR_MODE_MULTI):
            return self.json({"success": False, "error": f"mode 必须是 {ATTR_MODE_FIELDS}、{ATTR_MODE_LIST} 或 {ATTR_MODE_MULTI}"}, status_code=400)
        if collect_mode not in ("poll", "event"):
            return self.json({"success": False, "error": "collect_mode 必须是 poll 或 event"}, status_code=400)
        if mode in (ATTR_MODE_LIST, ATTR_MODE_MULTI):
            if not array_path:
                return self.json({"success": False, "error": "list/multi 模式必须指定 array_path"}, status_code=400)
            if not key_field:
                return self.json({"success": False, "error": "list/multi 模式必须指定 key_field"}, status_code=400)

        field_mapping_json = json.dumps(field_mapping, ensure_ascii=False)
        field_types_json = json.dumps(field_types, ensure_ascii=False) if isinstance(field_types, dict) else "{}"
        extra_fields_json = json.dumps(extra_fields, ensure_ascii=False) if isinstance(extra_fields, dict) and extra_fields else ""
        extra_json_nodes_json = json.dumps(extra_json_nodes, ensure_ascii=False) if isinstance(extra_json_nodes, list) and extra_json_nodes else ""

        def _do_config() -> str:
            conn = sqlite3.connect(db_path)
            try:
                # 1. 检查该 entity 是否已关联此 type（防止重复）
                existing = conn.execute(
                    f"SELECT attr_type FROM {TABLE_ENTITY_CONFIGS} "
                    f"WHERE entity_id = ? AND enabled = 1 AND category = ?",
                    (entity_id, CATEGORY_ATTRIBUTE),
                ).fetchone()
                if existing:
                    existing_type = existing[0]
                    if existing_type == type_name:
                        raise ValueError(f"实体 {entity_id} 已关联类型 '{type_name}'，不能重复添加")
                    # 不同 type 允许

                # 2. 检查 type 是否已存在
                existing_type_row = conn.execute(
                    f"SELECT type_name, field_mapping FROM {TABLE_ATTR_TYPE_DEFS} WHERE type_name = ?",
                    (type_name,),
                ).fetchone()

                if existing_type_row:
                    # 已存在：校验 field_mapping 一致性
                    existing_fm = existing_type_row[1]
                    if existing_fm != field_mapping_json:
                        raise ValueError(
                            f"类型 '{type_name}' 已存在，字段定义为 {existing_fm}，"
                            f"与当前定义 {field_mapping_json} 不一致"
                        )
                    # 更新 extra_fields 和/或 extra_json_nodes（允许追加附加字段）
                    if extra_fields_json or extra_json_nodes_json:
                        conn.execute(
                            f"UPDATE {TABLE_ATTR_TYPE_DEFS} SET extra_fields = ?, extra_json_nodes = ?, updated_at = ? WHERE type_name = ?",
                            (extra_fields_json, extra_json_nodes_json, now, type_name),
                        )
                else:
                    # 不存在：创建类型定义
                    conn.execute(
                        f"""
                        INSERT INTO {TABLE_ATTR_TYPE_DEFS}
                            (type_name, mode, array_path, key_field, compare_limit, decimal_places, field_mapping, field_types, extra_fields, extra_json_nodes, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (type_name, mode, array_path, key_field, compare_limit, decimal_places, field_mapping_json, field_types_json, extra_fields_json, extra_json_nodes_json, now, now),
                    )

                # 3. 立即创建数据表（如已存在则添加 extra_fields 列）
                tbl = get_attr_table_name(type_name)
                existing_tbl = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (tbl,),
                ).fetchone()
                if not existing_tbl:
                    valid_types = {"TEXT", "INTEGER", "REAL"}
                    columns_defs = [
                        "id INTEGER PRIMARY KEY AUTOINCREMENT",
                        "entity_id TEXT NOT NULL",
                        "name TEXT NOT NULL DEFAULT ''",
                        "datetime TEXT NOT NULL DEFAULT ''",
                        "room TEXT NOT NULL DEFAULT ''",
                    ]
                    for target_col in field_mapping.values():
                        safe_name = f'"{target_col.replace(".", "_")}"'
                        col_type = "REAL"
                        if isinstance(field_types, dict) and target_col in field_types:
                            ft = str(field_types[target_col]).upper()
                            if ft in valid_types:
                                col_type = ft
                        columns_defs.append(f"{safe_name} {col_type}")
                    # extra_fields 列（所有 extra_fields 均建独立列）
                    normalized_extra = _normalize_extra_fields_api(extra_fields)
                    if normalized_extra:
                        for src_path, info in normalized_extra.items():
                            target_col = info["target_col"]
                            safe_name = f'"{target_col.replace(".", "_")}"'
                            col_type = "TEXT"
                            if isinstance(field_types, dict) and target_col in field_types:
                                ft = str(field_types[target_col]).upper()
                                if ft in valid_types:
                                    col_type = ft
                            columns_defs.append(f"{safe_name} {col_type}")
                    # extra_json 列：始终创建（供 extra_json_nodes 使用）
                    columns_defs.append(f"{EXTRA_JSON_COLUMN} TEXT NOT NULL DEFAULT ''")
                    columns_defs.append("updated_at TEXT NOT NULL DEFAULT ''")
                    create_sql = f"CREATE TABLE {tbl} (\n    " + ",\n    ".join(columns_defs) + "\n);"
                    conn.execute(create_sql)
                    conn.execute(
                        f"CREATE INDEX IF NOT EXISTS idx_{tbl}_entity_time "
                        f"ON {tbl} (entity_id, datetime);"
                    )
                else:
                    # 表已存在：添加 extra_fields 列 + 确保 extra_json 列
                    existing_cols = {row[1] for row in conn.execute(f"PRAGMA table_info({tbl})")}
                    if normalized_extra:
                        for src_path, info in normalized_extra.items():
                            safe_name = info["target_col"].replace(".", "_")
                            if safe_name not in existing_cols:
                                conn.execute(
                                    f'ALTER TABLE {tbl} ADD COLUMN "{safe_name}" TEXT NOT NULL DEFAULT ""'
                                )
                    # 确保 extra_json 列存在
                    if EXTRA_JSON_COLUMN not in existing_cols:
                        conn.execute(
                            f'ALTER TABLE {tbl} ADD COLUMN {EXTRA_JSON_COLUMN} TEXT NOT NULL DEFAULT ""'
                        )

                # 4. 写入 entity_configs（联合唯一 entity_id+attr_type）
                conn.execute(
                    f"""
                    INSERT INTO {TABLE_ENTITY_CONFIGS}
                        (entity_id, enabled, category, metric_type, collect_interval,
                         power_entity, friendly_name, room, attr_type, collect_mode, created_at, updated_at)
                    VALUES (?, 1, ?, '', ?, '', '', ?, ?, ?, ?, ?)
                    ON CONFLICT(entity_id, attr_type) DO UPDATE SET
                        enabled          = 1,
                        category         = excluded.category,
                        collect_mode     = excluded.collect_mode,
                        collect_interval = excluded.collect_interval,
                        room             = excluded.room,
                        updated_at       = excluded.updated_at
                    """,
                    (entity_id, CATEGORY_ATTRIBUTE, collect_interval, room, type_name, collect_mode, now, now),
                )
                conn.commit()
                return type_name
            finally:
                conn.close()

        try:
            result_type = await self._exec_in_executor(hass, _do_config)
            return self.json({
                "success": True,
                "message": f"属性提取配置已保存，类型: {result_type}, 实体: {entity_id}",
            })
        except ValueError as exc:
            return self.json({"success": False, "error": str(exc)}, status_code=400)
        except Exception as exc:
            _LOGGER.exception("保存属性提取配置失败")
            return self.json({"success": False, "error": str(exc)}, status_code=500)

    async def delete(self, request: web.Request) -> web.Response:
        """DELETE /api/ha_data_store/attr_config?entity_id=xxx 或 ?type_name=xxx → 删除配置。"""
        db_path = self._db_path
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_api_enabled(request)):
            return resp
        if (resp := self._check_db_edit_enabled(hass)):
            return resp

        entity_id = request.query.get("entity_id", "").strip()
        type_name = request.query.get("type_name", "").strip()

        def _do_delete() -> str:
            conn = sqlite3.connect(db_path)
            try:
                if entity_id:
                    conn.execute(
                        f"UPDATE {TABLE_ENTITY_CONFIGS} SET enabled = 0, updated_at = ? WHERE entity_id = ?",
                        (_get_local_iso(hass.data.get(DOMAIN, {}).get("timezone", DEFAULT_TIMEZONE)), entity_id),
                    )
                    conn.commit()
                    return f"实体 {entity_id} 已移除"
                elif type_name:
                    # 删除类型定义 + 禁用关联实体
                    conn.execute(f"DELETE FROM {TABLE_ATTR_TYPE_DEFS} WHERE type_name = ?", (type_name,))
                    conn.execute(
                        f"UPDATE {TABLE_ENTITY_CONFIGS} SET enabled = 0 WHERE attr_type = ?",
                        (type_name,),
                    )
                    conn.commit()
                    return f"类型 '{type_name}' 及关联实体已移除"
                else:
                    raise ValueError("缺少 entity_id 或 type_name 参数")
            finally:
                conn.close()

        try:
            msg = await self._exec_in_executor(hass, _do_delete)
            return self.json({"success": True, "message": msg})
        except ValueError as exc:
            return self.json({"success": False, "error": str(exc)}, status_code=400)
        except Exception as exc:
            return self.json({"success": False, "error": str(exc)}, status_code=500)


# ========================================================================== #
# 14. ★ 实体导出配置 — ExportConfigView ★                                     #
#     GET/DELETE  /api/ha_data_store/export_config                          #
#     POST        /api/ha_data_store/export_config                          #
# ========================================================================== #
class ExportConfigView(_BaseDBView):
    """实体导出为 JSON 文件的配置管理。JSON 保存到 config/storage/export_entities/。"""

    url = "/api/ha_data_store/export_config"
    name = "api:ha_data_store:export_config"

    async def get(self, request: web.Request) -> web.Response:
        db_path = self._db_path

        def _query() -> list[dict]:
            conn = sqlite3.connect(db_path)
            try:
                conn.row_factory = sqlite3.Row
                return [dict(row) for row in conn.execute(
                    f"SELECT * FROM {TABLE_EXPORT_CONFIGS} WHERE enabled = 1 ORDER BY entity_id"
                ).fetchall()]
            finally:
                conn.close()

        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_api_enabled(request)):
            return resp
        data = await self._exec_in_executor(hass, _query)
        return self.json({"success": True, "data": data})

    async def post(self, request: web.Request) -> web.Response:
        db_path = self._db_path
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_master_switch(hass)):
            return resp
        if (resp := self._check_db_edit_enabled(hass)):
            return resp
        tz = hass.data.get(DOMAIN, {}).get("timezone", DEFAULT_TIMEZONE)
        now = _get_local_iso(tz)

        try:
            body = await request.json()
        except Exception:
            return self.json({"success": False, "error": "请求体不是合法的 JSON"}, status_code=400)

        entity_id = body.get("entity_id", "").strip()
        if not entity_id:
            return self.json({"success": False, "error": "entity_id 不能为空"}, status_code=400)

        file_name = body.get("file_name", "").strip() or f"{entity_id.replace('.', '_')}.json"

        def _upsert():
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    f"INSERT INTO {TABLE_EXPORT_CONFIGS} (entity_id, file_name, enabled, created_at, updated_at) "
                    f"VALUES (?, ?, 1, ?, ?) "
                    f"ON CONFLICT(entity_id) DO UPDATE SET file_name=excluded.file_name, enabled=1, updated_at=excluded.updated_at",
                    (entity_id, file_name, now, now),
                )
                conn.commit()
            finally:
                conn.close()

        await self._exec_in_executor(hass, _upsert)

        # 立即触发一次导出
        state_obj = hass.states.get(entity_id)
        if state_obj:
            await self._exec_in_executor(hass, lambda: None)  # just for consistency
            # 写初始 JSON
            export_dir = os.path.join(hass.config.config_dir, "storage", "export_entities")
            file_path = os.path.join(export_dir, file_name)

            def _write():
                os.makedirs(export_dir, exist_ok=True)
                data = {
                    "entity_id": entity_id,
                    "state": state_obj.state,
                    "attributes": dict(state_obj.attributes),
                    "last_updated": (state_obj.last_updated + timedelta(hours=tz)).isoformat() if state_obj.last_updated else "",
                }
                tmp = file_path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2, default=str)
                os.replace(tmp, file_path)

            await self._exec_in_executor(hass, _write)

        return self.json({"success": True, "message": f"导出配置已保存: {entity_id}"})

    async def delete(self, request: web.Request) -> web.Response:
        db_path = self._db_path
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_api_enabled(request)):
            return resp
        if (resp := self._check_db_edit_enabled(hass)):
            return resp
        entity_id = request.query.get("entity_id", "").strip()
        if not entity_id:
            return self.json({"success": False, "error": "缺少 entity_id"}, status_code=400)

        def _del():
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(f"UPDATE {TABLE_EXPORT_CONFIGS} SET enabled = 0 WHERE entity_id = ?", (entity_id,))
                conn.commit()
            finally:
                conn.close()

        await self._exec_in_executor(hass, _del)
        return self.json({"success": True, "message": f"导出配置已移除: {entity_id}"})


# ========================================================================== #
# 15. ★ 文件源配置 — FileSourceConfigView ★                                  #
#     GET/DELETE  /api/ha_data_store/file_source                            #
#     POST        /api/ha_data_store/file_source                            #
# ========================================================================== #
class FileSourceConfigView(_BaseDBView):
    """JSON 文件源配置管理。"""

    url = "/api/ha_data_store/file_source"
    name = "api:ha_data_store:file_source"

    async def get(self, request: web.Request) -> web.Response:
        db_path = self._db_path

        def _query() -> list[dict]:
            conn = sqlite3.connect(db_path)
            try:
                conn.row_factory = sqlite3.Row
                return [dict(row) for row in conn.execute(
                    f"SELECT * FROM {TABLE_FILE_SOURCE_CONFIGS} ORDER BY id"
                ).fetchall()]
            finally:
                conn.close()

        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_api_enabled(request)):
            return resp
        data = await self._exec_in_executor(hass, _query)
        return self.json({"success": True, "data": data})

    async def post(self, request: web.Request) -> web.Response:
        db_path = self._db_path
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_master_switch(hass)):
            return resp
        if (resp := self._check_db_edit_enabled(hass)):
            return resp
        tz = hass.data.get(DOMAIN, {}).get("timezone", DEFAULT_TIMEZONE)
        now = _get_local_iso(tz)

        try:
            body = await request.json()
        except Exception:
            return self.json({"success": False, "error": "请求体不是合法的 JSON"}, status_code=400)

        file_path = body.get("file_path", "").strip()
        if not file_path:
            return self.json({"success": False, "error": "file_path 不能为空"}, status_code=400)

        name = body.get("name", "").strip()
        state_field = body.get("state_field", "").strip()
        entity_prefix = body.get("entity_prefix", "").strip() or "sensor.file_"
        poll_interval = int(body.get("poll_interval", 10))

        def _upsert():
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    f"INSERT INTO {TABLE_FILE_SOURCE_CONFIGS} "
                    f"(name, file_path, state_field, entity_prefix, poll_interval, enabled, last_mtime, created_at, updated_at) "
                    f"VALUES (?, ?, ?, ?, ?, 1, 0, ?, ?)",
                    (name, file_path, state_field, entity_prefix, poll_interval, now, now),
                )
                conn.commit()
            finally:
                conn.close()

        await self._exec_in_executor(hass, _upsert)
        return self.json({"success": True, "message": f"文件源已添加: {file_path}"})

    async def delete(self, request: web.Request) -> web.Response:
        db_path = self._db_path
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_api_enabled(request)):
            return resp
        if (resp := self._check_db_edit_enabled(hass)):
            return resp
        sid = request.query.get("id", "").strip()
        if not sid:
            return self.json({"success": False, "error": "缺少 id"}, status_code=400)

        def _del() -> str:
            conn = sqlite3.connect(db_path)
            try:
                row = conn.execute(
                    f"SELECT device_id FROM {TABLE_FILE_SOURCE_CONFIGS} WHERE id = ?",
                    (sid,),
                ).fetchone()
                device_id = row[0] if row else ""
                conn.execute(f"DELETE FROM {TABLE_FILE_SOURCE_CONFIGS} WHERE id = ?", (sid,))
                conn.commit()
                return device_id
            finally:
                conn.close()

        device_id = await self._exec_in_executor(hass, _del)
        if device_id:
            from homeassistant.helpers import device_registry as dr2
            dr2_inst = dr2.async_get(hass)
            dr2_inst.async_remove_device(device_id)
        return self.json({"success": True, "message": f"文件源已删除: {sid}"})


# ========================================================================== #
# 16. ★ API 源配置 — ApiSourceConfigView ★                                   #
#     GET/DELETE  /api/ha_data_store/api_source                             #
#     POST        /api/ha_data_store/api_source                             #
# ========================================================================== #
class ApiSourceConfigView(_BaseDBView):
    """网络 API 源配置管理。"""

    url = "/api/ha_data_store/api_source"
    name = "api:ha_data_store:api_source"

    async def get(self, request: web.Request) -> web.Response:
        db_path = self._db_path

        def _query() -> list[dict]:
            conn = sqlite3.connect(db_path)
            try:
                conn.row_factory = sqlite3.Row
                return [dict(row) for row in conn.execute(
                    f"SELECT * FROM {TABLE_API_SOURCE_CONFIGS} ORDER BY id"
                ).fetchall()]
            finally:
                conn.close()

        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_api_enabled(request)):
            return resp
        data = await self._exec_in_executor(hass, _query)
        return self.json({"success": True, "data": data})

    async def post(self, request: web.Request) -> web.Response:
        db_path = self._db_path
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_master_switch(hass)):
            return resp
        if (resp := self._check_db_edit_enabled(hass)):
            return resp
        tz = hass.data.get(DOMAIN, {}).get("timezone", DEFAULT_TIMEZONE)
        now = _get_local_iso(tz)

        try:
            body = await request.json()
        except Exception:
            return self.json({"success": False, "error": "请求体不是合法的 JSON"}, status_code=400)

        url = body.get("url", "").strip()
        if not url:
            return self.json({"success": False, "error": "url 不能为空"}, status_code=400)

        name = body.get("name", "").strip()
        method = body.get("method", "GET").strip().upper()
        state_field = body.get("state_field", "").strip()
        entity_prefix = body.get("entity_prefix", "").strip() or "sensor.api_"
        poll_interval = int(body.get("poll_interval", 60))
        timeout = int(body.get("timeout", 15))
        max_retries = int(body.get("max_retries", 5))
        headers_raw = body.get("headers_json", "").strip()

        if headers_raw:
            try:
                json.loads(headers_raw)
            except json.JSONDecodeError:
                return self.json({"success": False, "error": "请求头不是合法的 JSON"}, status_code=400)

        def _upsert():
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    f"INSERT INTO {TABLE_API_SOURCE_CONFIGS} "
                    f"(name, url, method, state_field, entity_prefix, poll_interval, timeout, max_retries, headers_json, enabled, created_at, updated_at) "
                    f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                    (name, url, method, state_field, entity_prefix, poll_interval, timeout, max_retries, headers_raw, now, now),
                )
                conn.commit()
            finally:
                conn.close()

        await self._exec_in_executor(hass, _upsert)
        return self.json({"success": True, "message": f"API 源已添加: {url}"})

    async def delete(self, request: web.Request) -> web.Response:
        db_path = self._db_path
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_api_enabled(request)):
            return resp
        if (resp := self._check_db_edit_enabled(hass)):
            return resp
        sid = request.query.get("id", "").strip()
        if not sid:
            return self.json({"success": False, "error": "缺少 id"}, status_code=400)

        def _del() -> str:
            conn = sqlite3.connect(db_path)
            try:
                row = conn.execute(
                    f"SELECT device_id FROM {TABLE_API_SOURCE_CONFIGS} WHERE id = ?",
                    (sid,),
                ).fetchone()
                device_id = row[0] if row else ""
                conn.execute(f"DELETE FROM {TABLE_API_SOURCE_CONFIGS} WHERE id = ?", (sid,))
                conn.commit()
                return device_id
            finally:
                conn.close()

        device_id = await self._exec_in_executor(hass, _del)
        if device_id:
            from homeassistant.helpers import device_registry as dr2
            dr2_inst = dr2.async_get(hass)
            dr2_inst.async_remove_device(device_id)
        return self.json({"success": True, "message": f"API 源已删除: {sid}"})


# ========================================================================== #
#  数据库浏览器 HTML 加载（从独立文件读取，带缓存）                                 #
# ========================================================================== #
_DB_VIEWER_HTML_CACHE: str | None = None


# ========================================================================== #
# 17. ★ 数据统计 — StatsView ★                                              #
#     GET /api/ha_data_store/stats                                           #
#     返回各表行数、磁盘占用、最后写入时间                                        #
# ========================================================================== #
class StatsView(_BaseDBView):
    """数据统计 API。"""

    url = "/api/ha_data_store/stats"
    name = "api:ha_data_store:stats"

    async def get(self, request: web.Request) -> web.Response:
        db_path = self._db_path

        def _stats() -> dict:
            conn = sqlite3.connect(db_path)
            try:
                # 所有用户表
                tables = [
                    row[0] for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                    ).fetchall()
                ]
                table_stats = []
                total_rows = 0
                for tbl in tables:
                    try:
                        cnt = conn.execute(f"SELECT COUNT(*) FROM [{tbl}]").fetchone()[0]
                    except Exception:
                        cnt = 0
                    # 最后记录时间
                    last_dt = ""
                    try:
                        cols = [r[1] for r in conn.execute(f"PRAGMA table_info([{tbl}])")]
                        if "datetime" in cols:
                            r = conn.execute(f"SELECT MAX(datetime) FROM [{tbl}]").fetchone()
                            if r and r[0]:
                                last_dt = str(r[0])
                        elif "on_time" in cols:
                            r = conn.execute(f"SELECT MAX(on_time) FROM [{tbl}]").fetchone()
                            if r and r[0]:
                                last_dt = str(r[0])
                    except Exception:
                        pass
                    table_stats.append({"name": tbl, "rows": cnt, "last_datetime": last_dt})
                    total_rows += cnt

                db_size = os.path.getsize(db_path) if os.path.isfile(db_path) else 0
                # 格式化大小
                if db_size < 1024:
                    size_str = f"{db_size} B"
                elif db_size < 1048576:
                    size_str = f"{db_size/1024:.1f} KB"
                else:
                    size_str = f"{db_size/1048576:.2f} MB"

                return {"tables": table_stats, "total_rows": total_rows,
                        "db_size_bytes": db_size, "db_size": size_str}
            finally:
                conn.close()

        try:
            hass: HomeAssistant = request.app["hass"]
            if (resp := self._check_api_enabled(request)):
                return resp
            data = await self._exec_in_executor(hass, _stats)
            return self.json({"success": True, "data": data})
        except Exception as exc:
            return self.json({"success": False, "error": str(exc)}, status_code=500)


# ========================================================================== #
#  API Key 管理                                                               #
# ========================================================================== #
class ApiKeyView(_BaseDBView):

    url = "/api/ha_data_store/apikey"
    name = "api:ha_data_store:apikey"

    async def get(self, request: web.Request) -> web.Response:
        db_path = self._db_path
        def _query():
            conn = sqlite3.connect(db_path)
            try:
                conn.row_factory = sqlite3.Row
                keys = [dict(r) for r in conn.execute(
                    f"SELECT id, key, name, enabled, created_at FROM {TABLE_API_KEYS} ORDER BY id"
                ).fetchall()]
                if not keys:
                    dk = secrets.token_hex(16)
                    conn.execute(
                        f"INSERT INTO {TABLE_API_KEYS} (key, name, enabled, created_at) VALUES (?, 'default', 1, ?)",
                        (dk, datetime.utcnow().isoformat()),
                    )
                    conn.commit()
                    keys = [dict(r) for r in conn.execute(
                        f"SELECT id, key, name, enabled, created_at FROM {TABLE_API_KEYS} ORDER BY id"
                    ).fetchall()]
                return keys
            finally:
                conn.close()
        try:
            hass: HomeAssistant = request.app["hass"]
            if (resp := self._check_master_switch(hass)):
                return resp
            data = await self._exec_in_executor(hass, _query)
            return self.json({"success": True, "data": data})
        except Exception as exc:
            return self.json({"success": False, "error": str(exc)}, status_code=500)

    async def post(self, request: web.Request) -> web.Response:
        db_path = self._db_path
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_master_switch(hass)):
            return resp
        try:
            body = await request.json()
        except Exception:
            return self.json({"success": False, "error": "请求体不是合法的 JSON"}, status_code=400)
        name = body.get("name", "").strip()
        def _create():
            conn = sqlite3.connect(db_path)
            try:
                nk = secrets.token_hex(16)
                conn.execute(
                    f"INSERT INTO {TABLE_API_KEYS} (key, name, enabled, created_at) VALUES (?, ?, 1, ?)",
                    (nk, name or "new key", datetime.utcnow().isoformat()),
                )
                conn.commit()
                return nk
            finally:
                conn.close()
        try:
            nk = await self._exec_in_executor(hass, _create)
            return self.json({"success": True, "key": nk, "message": "密钥已创建"})
        except Exception as exc:
            return self.json({"success": False, "error": str(exc)}, status_code=500)

    async def delete(self, request: web.Request) -> web.Response:
        db_path = self._db_path
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_master_switch(hass)):
            return resp
        kid = request.query.get("id", "").strip()
        if not kid:
            return self.json({"success": False, "error": "缺少 id"}, status_code=400)
        def _del():
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(f"DELETE FROM {TABLE_API_KEYS} WHERE id = ?", (kid,))
                conn.commit()
            finally:
                conn.close()
        await self._exec_in_executor(hass, _del)
        return self.json({"success": True, "message": "密钥已删除"})


class ApiSettingsView(_BaseDBView):

    url = "/api/ha_data_store/apikey/settings"
    name = "api:ha_data_store:apikey_settings"

    async def post(self, request: web.Request) -> web.Response:
        db_path = self._db_path
        hass: HomeAssistant = request.app["hass"]
        if not hass.data.get(DOMAIN, {}).get("api_enabled", True):
            return web.Response(status=403)
        try:
            body = await request.json()
        except Exception:
            return self.json({"success": False, "error": "请求体不是合法的 JSON"}, status_code=400)
        old_pw = body.get("old_password", "")
        new_pw = body.get("new_password", "")
        if not old_pw or not new_pw:
            return self.json({"success": False, "error": "需要旧密码和新密码"}, status_code=400)
        # 校验旧密码
        def _check():
            conn = sqlite3.connect(db_path)
            try:
                row = conn.execute(
                    f"SELECT svalue FROM {TABLE_API_SETTINGS} WHERE skey='admin_password'"
                ).fetchone()
                return row and row[0] == old_pw
            finally:
                conn.close()
        if not _check():
            return self.json({"success": False, "error": "旧密码错误"}, status_code=403)
        def _update():
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    f"UPDATE {TABLE_API_SETTINGS} SET svalue=? WHERE skey='admin_password'",
                    (new_pw,),
                )
                conn.commit()
            finally:
                conn.close()
        await self._exec_in_executor(hass, _update)
        return self.json({"success": True, "message": "密码已更新"})


# ========================================================================== #
#  批量获取实体实时状态                                                          #
# ========================================================================== #
class BatchEntityStateView(_BaseDBView):
    """接受 entity_ids 列表，返回每个实体的 HA 实时状态。"""

    url = "/api/ha_data_store/batch_states"
    name = "api:ha_data_store:batch_states"

    async def post(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_master_switch(hass)):
            return resp
        try:
            body = await request.json()
        except Exception:
            return self.json({"success": False, "error": "请求体不是合法的 JSON"}, status_code=400)

        entity_ids = body.get("entity_ids", [])
        if not isinstance(entity_ids, list):
            entity_ids = []

        states = {}
        for eid in entity_ids:
            s = hass.states.get(eid)
            if s:
                states[eid] = {
                    "state": s.state,
                    "status": "online" if s.state not in ("unavailable", "unknown", None) else "unavailable",
                }
            else:
                states[eid] = {"state": "N/A", "status": "offline"}
        return self.json({"success": True, "data": states})


# ========================================================================== #
#  属性提取：手动触发采集                                                        #
# ========================================================================== #
class AttrManualTriggerView(_BaseDBView):
    """手动立即触发属性采集（无需密码）。"""

    url = "/api/ha_data_store/attr_trigger"
    name = "api:ha_data_store:attr_trigger"

    async def post(self, request: web.Request) -> web.Response:
        db_path = self._db_path
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_master_switch(hass)):
            return resp

        try:
            body = await request.json()
        except Exception:
            body = {}
        type_name = body.get("type_name", "").strip()

        try:
            from . import _async_attr_manual_trigger
            result = await _async_attr_manual_trigger(hass, db_path, type_name=type_name)
            return self.json({"success": True, "data": result})
        except Exception as exc:
            _LOGGER.exception("手动触发属性采集失败")
            return self.json({"success": False, "error": str(exc)}, status_code=500)


# ========================================================================== #
#  数据库维护：VACUUM 压缩 + 删表                                               #
# ========================================================================== #
class DbMaintainView(_BaseDBView):
    """数据库维护：VACUUM 压缩、删除表。"""

    url = "/api/ha_data_store/db_maintain"
    name = "api:ha_data_store:db_maintain"

    async def post(self, request: web.Request) -> web.Response:
        """POST: 执行 VACUUM 压缩数据库。"""
        db_path = self._db_path
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_master_switch(hass)):
            return resp

        def _vacuum() -> dict:
            size_before = os.path.getsize(db_path)
            try:
                conn = sqlite3.connect(db_path)
                conn.execute("PRAGMA journal_mode=DELETE")
                conn.execute("VACUUM")
                conn.close()
            except Exception as e:
                return {"error": str(e)}
            size_after = os.path.getsize(db_path)
            return {
                "size_before": f"{size_before / 1024 / 1024:.2f} MB",
                "size_after": f"{size_after / 1024 / 1024:.2f} MB",
                "saved": f"{(size_before - size_after) / 1024 / 1024:.2f} MB",
                "ratio": f"{(1 - size_after / max(size_before, 1)) * 100:.1f}%",
            }

        try:
            result = await self._exec_in_executor(hass, _vacuum)
            if "error" in result:
                return self.json({"success": False, "error": result["error"]}, status_code=500)
            return self.json({"success": True, "data": result, "message": "数据库已压缩"})
        except Exception as exc:
            return self.json({"success": False, "error": str(exc)}, status_code=500)

    async def delete(self, request: web.Request) -> web.Response:
        """DELETE: 删除指定表（需要密码验证）。保护核心表不被删除。"""
        db_path = self._db_path
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_master_switch(hass)):
            return resp
        if (resp := self._check_db_edit_enabled(hass)):
            return resp

        table = request.query.get("table", "").strip()
        admin_pw = request.query.get("admin_password", "").strip()
        if not table:
            return self.json({"success": False, "error": "缺少 table 参数"}, status_code=400)
        if not admin_pw:
            return self.json({"success": False, "error": "管理员密码不能为空"}, status_code=400)

        # 保护核心表（不许删除）
        PROTECTED = {
            TABLE_ENTITY_CONFIGS, TABLE_CUSTOM_ROUTES,
            TABLE_ATTR_TYPE_DEFS, TABLE_API_KEYS, TABLE_API_SETTINGS,
            TABLE_EXPORT_CONFIGS, TABLE_FILE_SOURCE_CONFIGS, TABLE_API_SOURCE_CONFIGS,
            TABLE_VACUUM_TYPE_DEFS, TABLE_VACUUM_CONFIGS,
        }
        if table in PROTECTED:
            return self.json({"success": False, "error": f"核心表 '{table}' 不允许删除"}, status_code=400)

        if not _verify_admin(db_path, admin_pw):
            return self.json({"success": False, "error": "管理员密码错误"}, status_code=403)

        def _drop():
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(f"DROP TABLE IF EXISTS [{table}]")
                conn.commit()
            finally:
                conn.close()

        try:
            await self._exec_in_executor(hass, _drop)
            return self.json({"success": True, "message": f"表 '{table}' 已删除"})
        except Exception as exc:
            return self.json({"success": False, "error": str(exc)}, status_code=500)


def _verify_admin(db_path: str, password: str) -> bool:
    """验证管理员密码。"""
    if not password:
        return False
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {TABLE_API_SETTINGS} ("
            "skey TEXT PRIMARY KEY, svalue TEXT NOT NULL DEFAULT '')"
        )
        conn.execute(
            f"INSERT OR IGNORE INTO {TABLE_API_SETTINGS} (skey, svalue) VALUES ('admin_password', 'admin')"
        )
        conn.commit()
        row = conn.execute(
            f"SELECT svalue FROM {TABLE_API_SETTINGS} WHERE skey = 'admin_password'"
        ).fetchone()
        return row and password == row[0]
    except Exception:
        return False
    finally:
        conn.close()


# ========================================================================== #
#  实体→网络：数据访问管理（自动生成唯一地址）                                     #
# ========================================================================== #
class PushTargetsView(_BaseDBView):
    """管理数据访问目标，自动生成唯一 access token。"""

    url = "/api/ha_data_store/push_targets"
    name = "api:ha_data_store:push_targets"

    async def get(self, request: web.Request) -> web.Response:
        db_path = self._db_path
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_master_switch(hass)):
            return resp

        def _query():
            conn = sqlite3.connect(db_path)
            try:
                conn.row_factory = sqlite3.Row
                return [dict(r) for r in conn.execute(
                    f"SELECT * FROM {TABLE_PUSH_TARGETS} ORDER BY entity_id"
                ).fetchall()]
            finally:
                conn.close()

        try:
            data = await self._exec_in_executor(hass, _query)
            return self.json({"success": True, "data": data})
        except Exception as exc:
            return self.json({"success": False, "error": str(exc)}, status_code=500)

    async def post(self, request: web.Request) -> web.Response:
        db_path = self._db_path
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_master_switch(hass)):
            return resp
        if (resp := self._check_db_edit_enabled(hass)):
            return resp
        try:
            body = await request.json()
        except Exception:
            return self.json({"success": False, "error": "请求体不是合法的 JSON"}, status_code=400)

        entity_id = body.get("entity_id", "").strip()
        if not entity_id:
            return self.json({"success": False, "error": "entity_id 不能为空"}, status_code=400)

        name = body.get("name", entity_id).strip()
        body_mode = body.get("body_mode", "full").strip()
        field_mapping = json.dumps(body.get("field_mapping", {}), ensure_ascii=False) if isinstance(body.get("field_mapping"), dict) else body.get("field_mapping", "{}")
        interval_min = int(body.get("interval_min", 0))
        now = _get_local_iso(DEFAULT_TIMEZONE)

        def _upsert():
            conn = sqlite3.connect(db_path)
            try:
                existing = conn.execute(
                    f"SELECT push_token FROM {TABLE_PUSH_TARGETS} WHERE entity_id = ?",
                    (entity_id,),
                ).fetchone()
                push_token = existing[0] if (existing and existing[0]) else secrets.token_hex(16)
                conn.execute(
                    f"""
                    INSERT INTO {TABLE_PUSH_TARGETS}
                        (entity_id, name, push_token, url, body_mode, field_mapping, interval_min, enabled, created_at, updated_at)
                    VALUES (?, ?, ?, '', ?, ?, ?, 1, ?, ?)
                    ON CONFLICT(entity_id) DO UPDATE SET
                        name = excluded.name,
                        push_token = excluded.push_token,
                        body_mode = excluded.body_mode,
                        field_mapping = excluded.field_mapping,
                        interval_min = excluded.interval_min,
                        enabled = 1,
                        updated_at = excluded.updated_at
                    """,
                    (entity_id, name, push_token, body_mode, field_mapping, interval_min, now, now),
                )
                conn.commit()
                return push_token
            finally:
                conn.close()

        try:
            token = await self._exec_in_executor(hass, _upsert)
            return self.json({
                "success": True,
                "message": f"数据访问 {entity_id} 已保存",
                "push_token": token,
            })
        except Exception as exc:
            _LOGGER.exception("保存数据访问目标失败")
            return self.json({"success": False, "error": str(exc)}, status_code=500)

    async def delete(self, request: web.Request) -> web.Response:
        db_path = self._db_path
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_master_switch(hass)):
            return resp
        if (resp := self._check_db_edit_enabled(hass)):
            return resp
        entity_id = request.query.get("entity_id", "").strip()
        if not entity_id:
            try:
                id_val = int(request.query.get("id", "0").strip())
            except ValueError:
                return self.json({"success": False, "error": "需要 entity_id 或 id 参数"}, status_code=400)
            where = "id = ?"
            param = id_val
        else:
            where = "entity_id = ?"
            param = entity_id

        def _delete():
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(f"DELETE FROM {TABLE_PUSH_TARGETS} WHERE {where}", (param,))
                conn.commit()
            finally:
                conn.close()

        try:
            await self._exec_in_executor(hass, _delete)
            return self.json({"success": True, "message": "数据访问目标已删除"})
        except Exception as exc:
            return self.json({"success": False, "error": str(exc)}, status_code=500)


# ========================================================================== #
#  实体→网络：数据访问端点（外部系统 GET 读取实体数据）                                #
# ========================================================================== #
class PushDataView(_BaseDBView):
    """外部系统通过生成的 URL 读取实体数据。"""

    url = "/api/ha_data_store/push_data/{push_token}"
    name = "api:ha_data_store:push_data"

    async def get(self, request: web.Request, push_token: str = "") -> web.Response:
        try:
            db_path = self._db_path
            hass: HomeAssistant = request.app["hass"]
            push_token = push_token.strip()
            if not push_token:
                return self.json({"success": False, "error": "缺少 push_token"}, status_code=400)

            # 受 API 访问开关控制（token 本身就是密钥，无需额外 Key）
            if not hass.data.get(DOMAIN, {}).get("api_enabled", True):
                return web.Response(status=403)

            def _get_target():
                conn = sqlite3.connect(db_path)
                try:
                    conn.row_factory = sqlite3.Row
                    row = conn.execute(
                        f"SELECT * FROM {TABLE_PUSH_TARGETS} WHERE push_token = ? AND enabled = 1",
                        (push_token,),
                    ).fetchone()
                    return dict(row) if row else None
                finally:
                    conn.close()

            target = await self._exec_in_executor(hass, _get_target)
            if not target:
                return self.json({"success": False, "error": "无效的 token"}, status_code=404)

            entity_id = target["entity_id"]
            state_obj = hass.states.get(entity_id)
            if not state_obj:
                return self.json({"success": False, "error": f"实体 {entity_id} 不存在"}, status_code=404)

            body_mode = target.get("body_mode", "full")
            if body_mode == "compact":
                data = {
                    "entity_id": entity_id,
                    "state": state_obj.state,
                    "last_updated": str(state_obj.last_updated) if state_obj.last_updated else "",
                }
            elif body_mode == "custom":
                data = {"entity_id": entity_id, "state": state_obj.state}
                try:
                    fm = json.loads(target.get("field_mapping", "{}"))
                except Exception:
                    fm = {}
                attrs = state_obj.attributes or {}
                for src_field, target_col in fm.items():
                    if target_col == "__node__":
                        data[src_field] = _extract_nested_value_static(attrs, src_field)
                    else:
                        val = _extract_nested_value_static(attrs, src_field)
                        data[target_col] = val
            else:
                attrs = state_obj.attributes
                data = {
                    "entity_id": entity_id,
                    "state": state_obj.state,
                    "attributes": dict(attrs) if isinstance(attrs, dict) else {},
                    "last_updated": str(state_obj.last_updated) if state_obj.last_updated else "",
                }

            return self.json({"success": True, "data": data})
        except Exception as exc:
            _LOGGER.exception("PushDataView 异常")
            return self.json({"success": False, "error": str(exc)}, status_code=500)


def _extract_nested_value_static(attrs: dict, path: str):
    """根据点号路径从字典中提取值。"""
    if not path or not attrs:
        return None
    val = attrs
    for p in path.split("."):
        if isinstance(val, dict):
            val = val.get(p)
        else:
            return None
    return val


# ========================================================================== #
#  扫地机器人：类型定义管理                                                      #
# ========================================================================== #
class VacuumTypeDefsView(_BaseDBView):
    """扫地机器人类型定义管理（CRUD）。"""

    url = "/api/ha_data_store/vacuum_types"
    name = "api:ha_data_store:vacuum_types"

    async def get(self, request: web.Request) -> web.Response:
        db_path = self._db_path
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_master_switch(hass)):
            return resp

        def _query():
            conn = sqlite3.connect(db_path)
            try:
                conn.row_factory = sqlite3.Row
                rows = [dict(r) for r in conn.execute(
                    f"SELECT * FROM {TABLE_VACUUM_TYPE_DEFS} ORDER BY type_name"
                ).fetchall()]
                for row in rows:
                    fm = row.get("field_mapping", "{}")
                    if isinstance(fm, str):
                        try:
                            row["field_mapping"] = json.loads(fm)
                        except Exception:
                            row["field_mapping"] = {}
                return rows
            finally:
                conn.close()

        try:
            data = await self._exec_in_executor(hass, _query)
            return self.json({"success": True, "data": data})
        except Exception as exc:
            return self.json({"success": False, "error": str(exc)}, status_code=500)

    async def post(self, request: web.Request) -> web.Response:
        db_path = self._db_path
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_db_edit_enabled(hass)):
            return resp
        try:
            body = await request.json()
        except Exception:
            return self.json({"success": False, "error": "请求体不是合法的 JSON"}, status_code=400)

        type_name = body.get("type_name", "").strip()
        if not type_name:
            return self.json({"success": False, "error": "type_name 不能为空"}, status_code=400)

        position_path = body.get("position_path", "vacuum_position").strip()
        working_states = body.get("working_states", "cleaning").strip()
        field_mapping = json.dumps(body.get("field_mapping", {}), ensure_ascii=False)
        now = _get_local_iso(DEFAULT_TIMEZONE)

        def _upsert():
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    f"""
                    INSERT INTO {TABLE_VACUUM_TYPE_DEFS}
                        (type_name, position_path, working_states, field_mapping, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(type_name) DO UPDATE SET
                        position_path = excluded.position_path,
                        working_states = excluded.working_states,
                        field_mapping = excluded.field_mapping,
                        updated_at = excluded.updated_at
                    """,
                    (type_name, position_path, working_states, field_mapping, now, now),
                )
                conn.commit()
            finally:
                conn.close()

        try:
            await self._exec_in_executor(hass, _upsert)
            return self.json({"success": True, "message": f"类型 {type_name} 配置已保存"})
        except Exception as exc:
            _LOGGER.exception("保存真空类型定义失败")
            return self.json({"success": False, "error": str(exc)}, status_code=500)

    async def delete(self, request: web.Request) -> web.Response:
        db_path = self._db_path
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_db_edit_enabled(hass)):
            return resp
        type_name = request.query.get("type_name", "").strip()
        if not type_name:
            return self.json({"success": False, "error": "缺少 type_name 参数"}, status_code=400)

        def _delete():
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    f"DELETE FROM {TABLE_VACUUM_TYPE_DEFS} WHERE type_name = ?",
                    (type_name,),
                )
                conn.commit()
            finally:
                conn.close()

        try:
            await self._exec_in_executor(hass, _delete)
            return self.json({"success": True, "message": f"类型 {type_name} 已删除"})
        except Exception as exc:
            return self.json({"success": False, "error": str(exc)}, status_code=500)


# ========================================================================== #
#  扫地机器人：实例配置管理                                                      #
# ========================================================================== #
class VacuumConfigsView(_BaseDBView):
    """扫地机器人实例配置管理（CRUD）。"""

    url = "/api/ha_data_store/vacuum_configs"
    name = "api:ha_data_store:vacuum_configs"

    async def get(self, request: web.Request) -> web.Response:
        db_path = self._db_path
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_master_switch(hass)):
            return resp

        def _query():
            conn = sqlite3.connect(db_path)
            try:
                conn.row_factory = sqlite3.Row
                rows = [dict(r) for r in conn.execute(
                    f"SELECT vc.*, vtd.working_states, vtd.position_path "
                    f"FROM {TABLE_VACUUM_CONFIGS} vc "
                    f"JOIN {TABLE_VACUUM_TYPE_DEFS} vtd ON vc.type_name = vtd.type_name "
                    f"ORDER BY vc.vacuum_id"
                ).fetchall()]
                return rows
            finally:
                conn.close()

        try:
            data = await self._exec_in_executor(hass, _query)
            return self.json({"success": True, "data": data})
        except Exception as exc:
            return self.json({"success": False, "error": str(exc)}, status_code=500)

    async def post(self, request: web.Request) -> web.Response:
        db_path = self._db_path
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_db_edit_enabled(hass)):
            return resp
        try:
            body = await request.json()
        except Exception:
            return self.json({"success": False, "error": "请求体不是合法的 JSON"}, status_code=400)

        vacuum_id = body.get("vacuum_id", "").strip()
        type_name = body.get("type_name", "").strip()
        trigger_entity_id = body.get("trigger_entity_id", "").strip()
        if not vacuum_id or not type_name or not trigger_entity_id:
            return self.json({"success": False, "error": "vacuum_id, type_name, trigger_entity_id 不能为空"}, status_code=400)

        now = _get_local_iso(DEFAULT_TIMEZONE)

        def _upsert():
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    f"""
                    INSERT INTO {TABLE_VACUUM_CONFIGS}
                        (vacuum_id, type_name, trigger_entity_id, enabled, created_at, updated_at)
                    VALUES (?, ?, ?, 1, ?, ?)
                    ON CONFLICT(vacuum_id) DO UPDATE SET
                        type_name = excluded.type_name,
                        trigger_entity_id = excluded.trigger_entity_id,
                        enabled = 1,
                        updated_at = excluded.updated_at
                    """,
                    (vacuum_id, type_name, trigger_entity_id, now, now),
                )
                conn.commit()
            finally:
                conn.close()

        try:
            await self._exec_in_executor(hass, _upsert)
            return self.json({"success": True, "message": f"机器人 {vacuum_id} 配置已保存"})
        except Exception as exc:
            _LOGGER.exception("保存真空配置失败")
            return self.json({"success": False, "error": str(exc)}, status_code=500)

    async def delete(self, request: web.Request) -> web.Response:
        db_path = self._db_path
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_db_edit_enabled(hass)):
            return resp
        vacuum_id = request.query.get("vacuum_id", "").strip()
        if not vacuum_id:
            return self.json({"success": False, "error": "缺少 vacuum_id 参数"}, status_code=400)

        def _delete():
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    f"DELETE FROM {TABLE_VACUUM_CONFIGS} WHERE vacuum_id = ?",
                    (vacuum_id,),
                )
                conn.commit()
            finally:
                conn.close()

        try:
            await self._exec_in_executor(hass, _delete)
            return self.json({"success": True, "message": f"机器人 {vacuum_id} 已删除"})
        except Exception as exc:
            return self.json({"success": False, "error": str(exc)}, status_code=500)


# ===========================================================================
#  桥接连接配置 API — BridgeConnectionsView                                      #
# ===========================================================================
class BridgeConnectionsView(_BaseDBView):
    """桥接连接管理。"""

    url = "/api/ha_data_store/bridge/connections"
    name = "api:ha_data_store:bridge_connections"

    async def get(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        db_path = self._db_path
        check = self._check_api_enabled(request)
        if check is not None:
            return check

        def _list():
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            try:
                rows = [dict(r) for r in conn.execute(
                    f"SELECT * FROM {TABLE_BRIDGE_CONNECTIONS} ORDER BY id"
                ).fetchall()]
                return rows
            finally:
                conn.close()

        try:
            data = await self._exec_in_executor(hass, _list)
            return self.json({"success": True, "data": data})
        except Exception as exc:
            return self.json({"success": False, "error": str(exc)}, status_code=500)

    async def post(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        db_path = self._db_path
        check = self._check_api_enabled(request)
        if check is not None:
            return check

        try:
            body = await request.json()
        except Exception:
            return self.json({"success": False, "error": "请求体需为 JSON"}, status_code=400)

        remote_url = (body.get("remote_url") or "").strip().rstrip("/")
        access_token = (body.get("access_token") or "").strip()
        name = (body.get("name") or "").strip()
        verify_ssl = 1 if body.get("verify_ssl", True) else 0

        if not remote_url or not access_token:
            return self.json({"success": False, "error": "remote_url 和 access_token 必填"}, status_code=400)

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        def _add():
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    f"INSERT INTO {TABLE_BRIDGE_CONNECTIONS} (name, remote_url, access_token, verify_ssl, enabled, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, 1, ?, ?)",
                    (name, remote_url, access_token, verify_ssl, now, now),
                )
                conn.commit()
                cid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                return cid
            finally:
                conn.close()

        try:
            cid = await self._exec_in_executor(hass, _add)
            return self.json({"success": True, "id": cid})
        except Exception as exc:
            return self.json({"success": False, "error": str(exc)}, status_code=500)

    async def delete(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        db_path = self._db_path
        check = self._check_api_enabled(request)
        if check is not None:
            return check

        cid_str = request.query.get("id", "")
        if not cid_str:
            return self.json({"success": False, "error": "缺少 id 参数"}, status_code=400)
        try:
            cid = int(cid_str)
        except ValueError:
            return self.json({"success": False, "error": "id 必须为整数"}, status_code=400)

        def _delete():
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(f"DELETE FROM {TABLE_BRIDGE_ENTITIES} WHERE connection_id = ?", (cid,))
                conn.execute(f"DELETE FROM {TABLE_BRIDGE_CONNECTIONS} WHERE id = ?", (cid,))
                conn.commit()
            finally:
                conn.close()

        try:
            await self._exec_in_executor(hass, _delete)
            return self.json({"success": True, "message": f"连接 {cid} 已删除"})
        except Exception as exc:
            return self.json({"success": False, "error": str(exc)}, status_code=500)


# ===========================================================================
#  桥接实体配置 API — BridgeEntitiesView                                        #
# ===========================================================================
class BridgeEntitiesView(_BaseDBView):
    """桥接实体管理。"""

    url = "/api/ha_data_store/bridge/entities"
    name = "api:ha_data_store:bridge_entities"

    async def get(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        db_path = self._db_path
        check = self._check_api_enabled(request)
        if check is not None:
            return check

        conn_id_str = request.query.get("connection_id", "")

        def _list():
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            try:
                if conn_id_str:
                    conn_id = int(conn_id_str)
                    rows = [dict(r) for r in conn.execute(
                        f"SELECT * FROM {TABLE_BRIDGE_ENTITIES} WHERE connection_id = ? ORDER BY entity_id",
                        (conn_id,),
                    ).fetchall()]
                else:
                    rows = [dict(r) for r in conn.execute(
                        f"SELECT * FROM {TABLE_BRIDGE_ENTITIES} ORDER BY connection_id, entity_id"
                    ).fetchall()]
                return rows
            finally:
                conn.close()

        try:
            data = await self._exec_in_executor(hass, _list)
            return self.json({"success": True, "data": data})
        except Exception as exc:
            return self.json({"success": False, "error": str(exc)}, status_code=500)

    async def post(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        db_path = self._db_path
        check = self._check_api_enabled(request)
        if check is not None:
            return check

        try:
            body = await request.json()
        except Exception:
            return self.json({"success": False, "error": "请求体需为 JSON"}, status_code=400)

        connection_id = body.get("connection_id")
        entity_ids = body.get("entity_ids", [])

        if not connection_id:
            return self.json({"success": False, "error": "connection_id 必填"}, status_code=400)
        if not entity_ids or not isinstance(entity_ids, list):
            return self.json({"success": False, "error": "entity_ids 必须为非空数组"}, status_code=400)

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        def _add():
            conn = sqlite3.connect(db_path)
            try:
                count = 0
                for eid in entity_ids:
                    eid = eid.strip()
                    if not eid or "." not in eid:
                        continue
                    cursor = conn.execute(
                        f"INSERT OR IGNORE INTO {TABLE_BRIDGE_ENTITIES} (connection_id, entity_id, enabled, created_at) "
                        "VALUES (?, ?, 1, ?)",
                        (connection_id, eid, now),
                    )
                    if cursor.rowcount:
                        count += 1
                conn.commit()
                return count
            finally:
                conn.close()

        try:
            count = await self._exec_in_executor(hass, _add)
            return self.json({"success": True, "added": count})
        except Exception as exc:
            return self.json({"success": False, "error": str(exc)}, status_code=500)

    async def delete(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        db_path = self._db_path
        check = self._check_api_enabled(request)
        if check is not None:
            return check

        eid_str = request.query.get("id", "")
        if not eid_str:
            return self.json({"success": False, "error": "缺少 id 参数"}, status_code=400)
        try:
            eid = int(eid_str)
        except ValueError:
            return self.json({"success": False, "error": "id 必须为整数"}, status_code=400)

        def _delete():
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(f"DELETE FROM {TABLE_BRIDGE_ENTITIES} WHERE id = ?", (eid,))
                conn.commit()
            finally:
                conn.close()

        try:
            await self._exec_in_executor(hass, _delete)
            return self.json({"success": True, "message": f"桥接实体 {eid} 已删除"})
        except Exception as exc:
            return self.json({"success": False, "error": str(exc)}, status_code=500)


# ===========================================================================
#  桥接重载 API — BridgeReloadView                                               #
# ===========================================================================
class BridgeReloadView(_BaseDBView):
    """重新加载桥接配置（重连 WebSocket + 重建实体）。

    POST /api/ha_data_store/bridge/reload
    """

    url = "/api/ha_data_store/bridge/reload"
    name = "api:ha_data_store:bridge_reload"

    async def post(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        check = self._check_api_enabled(request)
        if check is not None:
            return check

        entry_id = hass.data.get(DOMAIN, {}).get("entry_id")
        if not entry_id:
            return self.json({"success": False, "error": "集成条目未找到"}, status_code=500)

        # 先保存 entry_id（重载期间 hass.data[DOMAIN] 会被清理）
        try:
            await hass.config_entries.async_reload(entry_id)
            return self.json({"success": True, "message": "桥接已重新加载，实体已刷新"})
        except Exception as exc:
            return self.json({"success": False, "error": str(exc)}, status_code=500)


# ===========================================================================
#  虚拟设备 API — VirtualDeviceView                                              #
# ===========================================================================
class VirtualDeviceView(_BaseDBView):
    """动态创建/管理虚拟设备。

    GET    /api/ha_data_store/virtual_device            → 列出所有
    POST   /api/ha_data_store/virtual_device            → 创建设备
    DELETE /api/ha_data_store/virtual_device?entity_id= → 删除
    """

    url = "/api/ha_data_store/virtual_device"
    name = "api:ha_data_store:virtual_device"

    async def get(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        check = self._check_api_enabled(request)
        if check is not None:
            return check

        vd_list = hass.data.get(DOMAIN, {}).get("virtual_devices", [])
        data = [{"entity_id": d["entity_id"], "device_type": d["device_type"],
                  "device_name": d["device_name"], "entity_count": d["entity_count"]}
                 for d in vd_list]
        return self.json({"success": True, "data": data})

    async def post(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        check = self._check_api_enabled(request)
        if check is not None:
            return check

        try:
            body = await request.json()
        except Exception:
            return self.json({"success": False, "error": "请求体需为 JSON"}, status_code=400)

        device_type = (body.get("device_type") or "").strip()
        entity_id = (body.get("entity_id") or "").strip()
        device_name = (body.get("device_name") or "").strip()
        entity_name = (body.get("entity_name") or "").strip()

        if not device_type or not entity_id:
            return self.json({"success": False, "error": "device_type 和 entity_id 必填"}, status_code=400)
        if not all(ord(c) < 128 for c in entity_id):
            return self.json({"success": False, "error": "entity_id 必须为纯英文（如 light.test）"}, status_code=400)

        valid_types = ["switch", "light", "climate", "cover", "fan", "lock", "sensor", "binary_sensor", "number", "select", "vacuum", "media", "speaker"]
        if device_type not in valid_types:
            return self.json({"success": False, "error": f"类型需为 {', '.join(valid_types)}"}, status_code=400)

        domain = entity_id.split(".", 1)[0] if "." in entity_id else device_type
        if not entity_name:
            entity_name = entity_id.split(".", 1)[1] if "." in entity_id else entity_id
        if not device_name:
            device_name = entity_name

        config = {
            "device_type": device_type,
            "entity_id": entity_id,
            "device_name": device_name,
            "entity_name": entity_name,
            "init_value": body.get("init_value"),
            "unit": body.get("unit"),
            "min": body.get("min"),
            "max": body.get("max"),
            "step": body.get("step"),
            "options": body.get("options"),
        }

        try:
            from .virtual_devices import VirtualDeviceManager
            entry_id = hass.data.get(DOMAIN, {}).get("entry_id", "")
            mgr = VirtualDeviceManager(hass, entry_id)
            result = mgr.create_device(config)
            return self.json({"success": True, "data": result})
        except Exception as exc:
            return self.json({"success": False, "error": str(exc)}, status_code=500)

    async def delete(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        check = self._check_api_enabled(request)
        if check is not None:
            return check

        entity_id = request.query.get("entity_id", "").strip()
        if not entity_id:
            return self.json({"success": False, "error": "缺少 entity_id 参数"}, status_code=400)

        try:
            from .virtual_devices import VirtualDeviceManager
            entry_id = hass.data.get(DOMAIN, {}).get("entry_id", "")
            mgr = VirtualDeviceManager(hass, entry_id)
            ok = mgr.delete_device(entity_id)
            if ok:
                return self.json({"success": True, "message": f"虚拟设备 {entity_id} 已删除"})
            return self.json({"success": False, "error": "设备未找到"}, status_code=404)
        except Exception as exc:
            return self.json({"success": False, "error": str(exc)}, status_code=500)


# ===========================================================================
#  健康记录 API — HealthAddView                                              #
# ===========================================================================
class HealthAddView(_BaseDBView):
    """提交健康记录。

    POST /api/ha_data_store/health/add
    Body: { name (必填), dp, sp, pr, height, weight, bmi, temp, type, date_time, remark }
    """

    url = "/api/ha_data_store/health/add"
    name = "api:ha_data_store:health_add"

    async def post(self, request: web.Request) -> web.Response:
        db_path = self._db_path
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_api_enabled(request)):
            return resp

        try:
            body = await request.json()
        except Exception:
            return self.json({"success": False, "error": "请求体需为 JSON"}, status_code=400)

        name = (body.get("name") or "").strip()
        if not name:
            return self.json({"success": False, "error": "name 必填"}, status_code=400)

        date_time = body.get("date_time", "").strip()
        # 移除毫秒部分（如 2026-04-15 09:20:00.000 → 2026-04-15 09:20:00）
        if date_time and "." in date_time:
            date_time = date_time.split(".")[0]
        if not date_time:
            date_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        def _insert() -> int:
            conn = sqlite3.connect(db_path)
            try:
                cursor = conn.execute(
                    f"""INSERT INTO {TABLE_HEALTH_RECORDS}
                        (date_time, name, dp, sp, pr, height, weight, bmi, temp, type, remark)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        date_time,
                        name,
                        _to_float_or_none(body.get("dp")),
                        _to_float_or_none(body.get("sp")),
                        _to_float_or_none(body.get("pr")),
                        _to_float_or_none(body.get("height")),
                        _to_float_or_none(body.get("weight")),
                        _to_float_or_none(body.get("bmi")),
                        _to_float_or_none(body.get("temp")),
                        (body.get("type") or "").strip(),
                        (body.get("remark") or "").strip(),
                    ),
                )
                conn.commit()
                return cursor.lastrowid
            finally:
                conn.close()

        try:
            rid = await self._exec_in_executor(hass, _insert)
            return self.json({"success": True, "id": rid, "message": "健康记录已添加"})
        except Exception as exc:
            _LOGGER.exception("添加健康记录失败")
            return self.json({"success": False, "error": str(exc)}, status_code=500)


# ===========================================================================
#  健康类型管理 API — HealthTypesView                                        #
# ===========================================================================
class HealthTypesView(_BaseDBView):
    """管理健康类型列表（存储在 api_settings 中）。

    GET  /api/ha_data_store/health/types → 获取类型列表
    POST /api/ha_data_store/health/types → 添加新类型  Body: { type: "运动后" }
    """

    url = "/api/ha_data_store/health/types"
    name = "api:ha_data_store:health_types"

    async def get(self, request: web.Request) -> web.Response:
        db_path = self._db_path
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_api_enabled(request)):
            return resp

        def _load():
            conn = sqlite3.connect(db_path)
            try:
                # 从 api_settings 读取预定义类型
                row = conn.execute(
                    f"SELECT svalue FROM {TABLE_API_SETTINGS} WHERE skey = 'health_types'"
                ).fetchone()
                defined_types: list = json.loads(row[0]) if row and row[0] else []

                # 从实际数据中取唯一的 type 值（排除空值）
                distinct_types = conn.execute(
                    f"SELECT DISTINCT type FROM {TABLE_HEALTH_RECORDS} WHERE type != '' ORDER BY type"
                ).fetchall()
                data_types = [r[0] for r in distinct_types]

                # 合并去重
                seen = set()
                merged_types: list[str] = []
                for t in defined_types + data_types:
                    if t not in seen:
                        seen.add(t)
                        merged_types.append(t)

                # 从实际数据中取唯一的 name 值（排除空值）
                distinct_names = conn.execute(
                    f"SELECT DISTINCT name FROM {TABLE_HEALTH_RECORDS} WHERE name != '' ORDER BY name"
                ).fetchall()
                names = [r[0] for r in distinct_names]

                return {"types": merged_types, "names": names}
            except Exception:
                return {"types": [], "names": []}
            finally:
                conn.close()

        try:
            types = await self._exec_in_executor(hass, _load)
            return self.json({"success": True, "data": types})
        except Exception as exc:
            return self.json({"success": False, "error": str(exc)}, status_code=500)

    async def post(self, request: web.Request) -> web.Response:
        db_path = self._db_path
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_api_enabled(request)):
            return resp

        try:
            body = await request.json()
        except Exception:
            return self.json({"success": False, "error": "请求体需为 JSON"}, status_code=400)

        new_type = (body.get("type") or "").strip()
        if not new_type:
            return self.json({"success": False, "error": "type 必填"}, status_code=400)

        def _add():
            conn = sqlite3.connect(db_path)
            try:
                row = conn.execute(
                    f"SELECT svalue FROM {TABLE_API_SETTINGS} WHERE skey = 'health_types'"
                ).fetchone()
                types = json.loads(row[0]) if row and row[0] else []
                if new_type in types:
                    return False
                types.append(new_type)
                conn.execute(
                    f"INSERT OR REPLACE INTO {TABLE_API_SETTINGS} (skey, svalue) VALUES ('health_types', ?)",
                    (json.dumps(types, ensure_ascii=False),),
                )
                conn.commit()
                return True
            finally:
                conn.close()

        try:
            added = await self._exec_in_executor(hass, _add)
            if added:
                return self.json({"success": True, "message": f"类型 '{new_type}' 已添加"})
            return self.json({"success": True, "message": f"类型 '{new_type}' 已存在"})
        except Exception as exc:
            return self.json({"success": False, "error": str(exc)}, status_code=500)


# ===========================================================================
#  健康记录删除 API — HealthDeleteView                                        #
# ===========================================================================
class HealthDeleteView(_BaseDBView):
    """删除健康记录。

    DELETE /api/ha_data_store/health/delete?id=123
    """

    url = "/api/ha_data_store/health/delete"
    name = "api:ha_data_store:health_delete"

    async def delete(self, request: web.Request) -> web.Response:
        db_path = self._db_path
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_api_enabled(request)):
            return resp

        id_str = request.query.get("id", "").strip()
        if not id_str:
            return self.json({"success": False, "error": "缺少 id 参数"}, status_code=400)
        try:
            record_id = int(id_str)
        except ValueError:
            return self.json({"success": False, "error": "id 必须为整数"}, status_code=400)

        def _delete():
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(f"DELETE FROM {TABLE_HEALTH_RECORDS} WHERE id = ?", (record_id,))
                conn.commit()
            finally:
                conn.close()

        try:
            await self._exec_in_executor(hass, _delete)
            return self.json({"success": True, "message": f"记录 {record_id} 已删除"})
        except Exception as exc:
            return self.json({"success": False, "error": str(exc)}, status_code=500)


# ===========================================================================
#  媒体播放列表管理 API                                                         #
# ===========================================================================
class MediaPlaylistView(_BaseDBView):
    """管理用户播放列表。
    GET    /api/ha_data_store/media/playlists?user=xxx   → 列出用户播放列表
    GET    /api/ha_data_store/media/users                → 列出所有用户
    POST   /api/ha_data_store/media/playlist             → 新增/重命名播放列表
    DELETE /api/ha_data_store/media/playlist?id=xxx      → 删除播放列表
    """

    url = "/api/ha_data_store/media/playlists"
    name = "api:ha_data_store:media_playlists"

    async def get(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_api_enabled(request)):
            return resp
        db_path = self._db_path
        user = request.query.get("user", "").strip()
        list_users = request.query.get("users", "").strip().lower() in ("true", "1")

        def _list_users():
            conn = sqlite3.connect(db_path)
            try:
                rows = conn.execute(
                    f"SELECT DISTINCT user_name FROM {TABLE_MEDIA_PLAYLISTS} ORDER BY user_name"
                ).fetchall()
                return [{"user_name": r[0]} for r in rows]
            finally:
                conn.close()

        import json as _json
        def _list_playlists():
            conn = sqlite3.connect(db_path)
            try:
                conn.row_factory = sqlite3.Row
                if user:
                    rows = conn.execute(
                        f"SELECT * FROM {TABLE_MEDIA_PLAYLISTS} WHERE user_name = ? ORDER BY name",
                        (user,),
                    ).fetchall()
                    playlist_count = len(rows)
                else:
                    rows = conn.execute(
                        f"SELECT * FROM {TABLE_MEDIA_PLAYLISTS} ORDER BY user_name, name"
                    ).fetchall()
                    playlist_count = len(rows)
                playlists = []
                for r in rows:
                    d = dict(r)
                    if isinstance(d.get("songs"), str):
                        try:
                            d["songs"] = _json.loads(d["songs"])
                        except _json.JSONDecodeError:
                            d["songs"] = []
                    playlists.append(d)
                return {
                    "playlists": playlists,
                    "total": playlist_count,
                }
            finally:
                conn.close()

        try:
            if list_users:
                data = await self._exec_in_executor(hass, _list_users)
                return self.json({"success": True, "data": data})
            data = await self._exec_in_executor(hass, _list_playlists)
            return self.json({"success": True, "data": data})
        except Exception as exc:
            return self.json({"success": False, "error": str(exc)}, status_code=500)

    async def post(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_api_enabled(request)):
            return resp
        db_path = self._db_path
        try:
            body = await request.json()
        except Exception:
            return self.json({"success": False, "error": "请求体需为 JSON"}, status_code=400)

        user_name = (body.get("user_name") or "").strip()
        name = (body.get("name") or "").strip()
        playlist_id = body.get("id")
        songs = body.get("songs")

        if not user_name:
            return self.json({"success": False, "error": "user_name 必填"}, status_code=400)
        if not name and not playlist_id:
            return self.json({"success": False, "error": "name 或 id 必填"}, status_code=400)

        now = __import__('datetime').datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        def _save():
            conn = sqlite3.connect(db_path)
            try:
                if playlist_id:
                    # 更新名称
                    if songs is not None:
                        songs_json = json.dumps(songs, ensure_ascii=False)
                        conn.execute(
                            f"UPDATE {TABLE_MEDIA_PLAYLISTS} SET name = ?, songs = ?, updated_at = ? WHERE id = ?",
                            (name, songs_json, now, playlist_id),
                        )
                    else:
                        conn.execute(
                            f"UPDATE {TABLE_MEDIA_PLAYLISTS} SET name = ?, updated_at = ? WHERE id = ?",
                            (name, now, playlist_id),
                        )
                else:
                    songs_json = json.dumps(songs, ensure_ascii=False) if songs is not None else '[]'
                    conn.execute(
                        f"INSERT OR IGNORE INTO {TABLE_MEDIA_PLAYLISTS} (user_name, name, songs, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                        (user_name, name, songs_json, now, now),
                    )
                conn.commit()
                return {"success": True, "message": f"播放列表 '{name}' 已保存"}
            except Exception as exc:
                return {"success": False, "error": str(exc)}
            finally:
                conn.close()

        result = await self._exec_in_executor(hass, _save)
        return self.json(result)

    async def delete(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_api_enabled(request)):
            return resp
        db_path = self._db_path
        playlist_id = request.query.get("id", "").strip()
        user_name = request.query.get("user", "").strip()

        if not playlist_id and not user_name:
            return self.json({"success": False, "error": "需要 id 或 user 参数"}, status_code=400)

        def _delete():
            conn = sqlite3.connect(db_path)
            try:
                if playlist_id:
                    conn.execute(f"DELETE FROM {TABLE_MEDIA_PLAYLISTS} WHERE id = ?", (playlist_id,))
                elif user_name:
                    conn.execute(f"DELETE FROM {TABLE_MEDIA_PLAYLISTS} WHERE user_name = ?", (user_name,))
                conn.commit()
            finally:
                conn.close()

        try:
            await self._exec_in_executor(hass, _delete)
            msg = f"已删除用户 '{user_name}' 的全部播放列表" if user_name else f"播放列表已删除"
            return self.json({"success": True, "message": msg})
        except Exception as exc:
            return self.json({"success": False, "error": str(exc)}, status_code=500)

    async def put(self, request: web.Request) -> web.Response:
        """更新播放列表的歌曲字段。
        PUT /api/ha_data_store/media/playlists?id=xxx&key=xxx
        Body: { "songs": [...] }
        """
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_api_enabled(request)):
            return resp
        db_path = self._db_path
        playlist_id = request.query.get("id", "").strip()
        if not playlist_id:
            return self.json({"success": False, "error": "缺少 id 参数"}, status_code=400)
        try:
            body = await request.json()
        except Exception:
            return self.json({"success": False, "error": "请求体需为 JSON"}, status_code=400)
        songs = body.get("songs")
        if songs is None:
            return self.json({"success": False, "error": "缺少 songs 字段"}, status_code=400)

        now = __import__('datetime').datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        songs_json = json.dumps(songs, ensure_ascii=False)

        def _update():
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    f"UPDATE {TABLE_MEDIA_PLAYLISTS} SET songs = ?, updated_at = ? WHERE id = ?",
                    (songs_json, now, playlist_id),
                )
                conn.commit()
                return {"success": True, "message": "歌曲已更新"}
            finally:
                conn.close()

        try:
            result = await self._exec_in_executor(hass, _update)
            return self.json(result)
        except Exception as exc:
            return self.json({"success": False, "error": str(exc)}, status_code=500)


# ===========================================================================
#  媒体播放队列 API — 后端列表播放                                             #
# ===========================================================================
class MediaQueueView(_BaseDBView):
    """管理后端列表播放队列。
    GET    /api/ha_data_store/media/queue?entity_id=xxx&key=xxx        → 查询队列状态
    POST   /api/ha_data_store/media/queue?key=xxx                      → 设置队列
    DELETE /api/ha_data_store/media/queue?entity_id=xxx&key=xxx        → 停止队列
    """

    url = "/api/ha_data_store/media/queue"
    name = "api:ha_data_store:media_queue"

    async def get(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_api_enabled(request)):
            return resp
        entity_id = request.query.get("entity_id", "").strip()
        if not entity_id:
            return self.json({"success": False, "error": "缺少 entity_id 参数"}, status_code=400)

        def _query():
            conn = sqlite3.connect(self._db_path)
            try:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    f"SELECT * FROM {TABLE_MEDIA_QUEUE} WHERE entity_id = ?",
                    (entity_id,),
                ).fetchone()
                if not row:
                    return None
                d = dict(row)
                if isinstance(d.get("tracks"), str):
                    d["tracks"] = json.loads(d["tracks"])
                return d
            finally:
                conn.close()

        try:
            data = await self._exec_in_executor(hass, _query)
            return self.json({"success": True, "data": data})
        except Exception as exc:
            return self.json({"success": False, "error": str(exc)}, status_code=500)

    async def post(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_api_enabled(request)):
            return resp
        try:
            body = await request.json()
        except Exception:
            return self.json({"success": False, "error": "请求体需为 JSON"}, status_code=400)

        entity_id = (body.get("entity_id") or "").strip()
        tracks = body.get("tracks", [])
        current_index = int(body.get("current_index", 0))
        is_active = 1 if body.get("is_active", True) else 0

        if not entity_id:
            return self.json({"success": False, "error": "entity_id 必填"}, status_code=400)

        now = __import__('datetime').datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        tracks_json = json.dumps(tracks, ensure_ascii=False)

        def _save():
            conn = sqlite3.connect(self._db_path)
            try:
                conn.execute(
                    f"INSERT OR REPLACE INTO {TABLE_MEDIA_QUEUE} "
                    f"(entity_id, tracks, current_index, is_active, created_at, updated_at) "
                    f"VALUES (?, ?, ?, ?, ?, ?)",
                    (entity_id, tracks_json, current_index, is_active, now, now),
                )
                conn.commit()
                return {"success": True, "message": "队列已设置"}
            finally:
                conn.close()

        try:
            result = await self._exec_in_executor(hass, _save)
            return self.json(result)
        except Exception as exc:
            return self.json({"success": False, "error": str(exc)}, status_code=500)

    async def delete(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_api_enabled(request)):
            return resp
        entity_id = request.query.get("entity_id", "").strip()
        if not entity_id:
            return self.json({"success": False, "error": "缺少 entity_id 参数"}, status_code=400)

        def _stop():
            conn = sqlite3.connect(self._db_path)
            try:
                conn.execute(
                    f"UPDATE {TABLE_MEDIA_QUEUE} SET is_active = 0, updated_at = ? WHERE entity_id = ?",
                    (__import__('datetime').datetime.now().strftime("%Y-%m-%d %H:%M:%S"), entity_id),
                )
                conn.commit()
            finally:
                conn.close()

        try:
            await self._exec_in_executor(hass, _stop)
            return self.json({"success": True, "message": "队列已停止"})
        except Exception as exc:
            return self.json({"success": False, "error": str(exc)}, status_code=500)


async def _load_db_viewer_html(hass: HomeAssistant) -> str:
    """从同目录下的 db_viewer.html 读取页面内容，首次读取后缓存。"""
    global _DB_VIEWER_HTML_CACHE
    if _DB_VIEWER_HTML_CACHE is not None:
        return _DB_VIEWER_HTML_CACHE
    html_path = Path(__file__).parent / "db_viewer.html"
    _DB_VIEWER_HTML_CACHE = await hass.async_add_executor_job(
        lambda: html_path.read_text(encoding="utf-8")
    )
    return _DB_VIEWER_HTML_CACHE
