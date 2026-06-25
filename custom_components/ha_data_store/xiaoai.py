"""小爱对话采集模块 — 独立模块。

职责：
  - 建表 xiaoai_configs / xiaoai_conversations
  - state_changed 事件采集（upsert 合并"用户说话"与"AI 回复"两次触发）
  - HTTP API：配置 CRUD + 对话记录查询

数据来源（小爱对话 sensor 的一次 state_changed）：
  state                              → 用户说的话 (user_text)
  attributes.answers[0].tts.text     → AI 回复 (ai_text)
  attributes.timestamp               → 对话时间 (conv_time)，格式 yyyy-mm-dd hh:mm:ss
                                       （原值为 ISO8601，取前19位并替换 T 为空格，无需时区转换）

采集策略（upsert）：
  小爱一次对话在 HA 中通常触发两次 state_changed：
    ① 用户说话：state 变为新话，timestamp 更新，answers 可能尚未填充
    ② AI 回复：state 不变，answers 更新，timestamp 仍是本次对话时间
  以 (entity_id, conv_time, user_text) 为唯一键：
    ① 时插入 user_text + conv_time，ai_text 暂空
    ② 时同键命中，UPDATE 补充 ai_text
  使用 INSERT ... ON CONFLICT(...) DO UPDATE SET ai_text=excluded.ai_text 一条语句完成。
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .logger import get_logger

_LOGGER = logging.getLogger(__name__)

# 表名（模块内私有常量，不污染 const.py）
TABLE_XIAOAI_CONFIGS = "xiaoai_configs"
TABLE_XIAOAI_CONVERSATIONS = "xiaoai_conversations"


# =========================================================================== #
#  数据库初始化                                                                  #
# =========================================================================== #
def init_database(db_path: str) -> None:
    """建表 + 迁移。由 __init__._init_database 调用。

    使用独立连接（_init_database 末尾已 close 自身连接）。
    """
    conn = sqlite3.connect(db_path)
    local_logger = get_logger()
    try:
        # 配置表（支持多个实体，entity_id 区分）
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE_XIAOAI_CONFIGS} (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_id  TEXT NOT NULL UNIQUE,
                name       TEXT NOT NULL DEFAULT '',
                enabled    INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        # 对话记录表
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE_XIAOAI_CONVERSATIONS} (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_id  TEXT NOT NULL,
                user_text  TEXT NOT NULL DEFAULT '',
                ai_text    TEXT NOT NULL DEFAULT '',
                conv_time  TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                UNIQUE(entity_id, conv_time, user_text)
            )
            """
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_xiaoai_entity_time "
            f"ON {TABLE_XIAOAI_CONVERSATIONS} (entity_id, conv_time)"
        )
        conn.commit()
        if local_logger:
            local_logger.info("[xiaoai] 小爱对话表结构已就绪")
    finally:
        conn.close()


# =========================================================================== #
#  监听集合                                                                      #
# =========================================================================== #
def get_monitored_entities(db_path: str) -> set[str]:
    """返回需要监听 state_changed 的 entity_id 集合。

    由 __init__._refresh_monitored_set_sync 调用，并入总白名单；
    同时该集合会单独存入 hass.data[DOMAIN]["xiaoai_entities"]，用于采集时 O(1) 识别。
    """
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            f"SELECT entity_id FROM {TABLE_XIAOAI_CONFIGS} WHERE enabled = 1"
        ).fetchall()
        return {r[0] for r in rows}
    except sqlite3.OperationalError:
        # 表可能尚未创建（首次启动调用顺序问题）
        return set()
    finally:
        conn.close()


# =========================================================================== #
#  采集处理                                                                      #
# =========================================================================== #
def handle_state_changed_sync(db_path: str, entity_id: str, new_state) -> bool:
    """同步处理 state_changed 事件，写入对话记录。

    返回 True 表示已处理（调用方应 return 不再走后续流程），False 表示未命中/未处理。

    由 __init__._async_state_changed 通过 hass.async_add_executor_job 调用。
    """
    if not new_state:
        return False
    attrs = getattr(new_state, "attributes", None) or {}
    user_text = (new_state.state or "").strip()
    ts = attrs.get("timestamp", "")
    if not ts:
        return False  # 没有时间戳，非正常对话事件

    # 时间格式化：ISO8601 → yyyy-mm-dd hh:mm:ss（取前19位，T 替换为空格）
    conv_time = str(ts)[:19].replace("T", " ")

    # AI 回复：仅取 answers[0].tts.text（按需求，不处理多 answers 情况）
    ai_text = ""
    answers = attrs.get("answers")
    if isinstance(answers, list) and answers:
        first = answers[0]
        if isinstance(first, dict):
            tts = first.get("tts")
            if isinstance(tts, dict):
                ai_text = (tts.get("text") or "").strip()

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    local_logger = get_logger()

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            f"INSERT INTO {TABLE_XIAOAI_CONVERSATIONS} "
            f"(entity_id, user_text, ai_text, conv_time, created_at) "
            f"VALUES (?, ?, ?, ?, ?) "
            f"ON CONFLICT(entity_id, conv_time, user_text) DO UPDATE SET ai_text = excluded.ai_text",
            (entity_id, user_text, ai_text, conv_time, now_str),
        )
        conn.commit()
        if local_logger:
            local_logger.info(
                "[xiaoai] 采集对话 entity_id=%s conv_time=%s user=%r ai=%r",
                entity_id, conv_time,
                user_text[:40], ai_text[:40],
            )
    except sqlite3.IntegrityError:
        # 极端情况：并发触发，忽略
        pass
    finally:
        conn.close()
    return True


async def async_handle_state_changed(
    hass: HomeAssistant, db_path: str, entity_id: str, old_state, new_state
) -> bool:
    """异步包装：提交到 executor 执行同步采集。"""
    return await hass.async_add_executor_job(
        handle_state_changed_sync, db_path, entity_id, new_state
    )


def query_history_sync(
    db_path: str, entity_id: str, start: str = "", end: str = "", limit: int = 500
) -> dict:
    """查询对话记录（同步，供万能查询 /query?type=xiaoai_history 调用）。

    参数：
      entity_id 必填
      start/end 可选，格式 yyyy-mm-dd 或 yyyy-mm-dd hh:mm:ss
      limit 默认 500，最大 5000
    返回 {"rows": [...], "total": N}
    """
    if not entity_id:
        raise ValueError("xiaoai_history 需要 entity_id 参数")
    limit = max(1, min(limit, 5000))

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        sql = (f"SELECT id, entity_id, user_text, ai_text, conv_time, created_at "
               f"FROM {TABLE_XIAOAI_CONVERSATIONS} WHERE entity_id = ?")
        params: list = [entity_id]
        if start:
            sql += " AND conv_time >= ?"
            params.append(start)
        if end:
            # end 若为日期(yyyy-mm-dd)补全到当天末尾，确保包含当天记录
            if len(end) == 10:
                end = end + " 23:59:59"
            sql += " AND conv_time <= ?"
            params.append(end)
        # 总数
        count_sql = f"SELECT COUNT(*) FROM ({sql})"
        total = conn.execute(count_sql, tuple(params)).fetchone()[0]
        # 分页
        sql += " ORDER BY conv_time DESC, id DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, tuple(params)).fetchall()
        return {"rows": [dict(r) for r in rows], "total": total, "limit": limit}
    finally:
        conn.close()


# =========================================================================== #
#  HTTP API Views                                                               #
# =========================================================================== #
class _XiaoaiBaseView(HomeAssistantView):
    """小爱 API 视图公共基类。"""

    requires_auth = False  # 外部 UI 跨域免鉴权
    cors_allowed = True

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    async def _exec(self, hass: HomeAssistant, func, *args):
        return await hass.async_add_executor_job(func, *args)

    @staticmethod
    def _check_master_switch(hass: HomeAssistant) -> web.Response | None:
        """检查主开关（db_viewer 管理页面用，不含 Key 校验）。"""
        if not hass.data.get("ha_data_store", {}).get("api_enabled", True):
            return web.Response(status=403)
        return None


class XiaoaiConfigView(_XiaoaiBaseView):
    """小爱对话配置 CRUD。

    GET    /api/ha_data_store/xiaoai/configs          → 列出所有配置
    POST   /api/ha_data_store/xiaoai/configs          → 新增/修改配置 {entity_id, name, enabled?}
    DELETE /api/ha_data_store/xiaoai/configs?id=xxx   → 删除配置（不删历史记录）
    """

    url = "/api/ha_data_store/xiaoai/configs"
    name = "api:ha_data_store:xiaoai_configs"

    async def get(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_master_switch(hass)):
            return resp

        def _list():
            conn = sqlite3.connect(self._db_path)
            try:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    f"SELECT * FROM {TABLE_XIAOAI_CONFIGS} ORDER BY id"
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

        try:
            data = await self._exec(hass, _list)
            return self.json({"success": True, "data": data})
        except Exception as exc:
            _LOGGER.exception("[xiaoai] 查询配置失败")
            return self.json({"success": False, "error": str(exc)}, status_code=500)

    async def post(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_master_switch(hass)):
            return resp
        try:
            body = await request.json()
        except Exception:
            return self.json({"success": False, "error": "请求体需为 JSON"}, status_code=400)

        entity_id = (body.get("entity_id") or "").strip()
        name = (body.get("name") or "").strip()
        enabled = 1 if body.get("enabled", True) else 0
        if not entity_id:
            return self.json({"success": False, "error": "entity_id 必填"}, status_code=400)

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        def _upsert():
            conn = sqlite3.connect(self._db_path)
            try:
                conn.execute(
                    f"INSERT INTO {TABLE_XIAOAI_CONFIGS} (entity_id, name, enabled, created_at, updated_at) "
                    f"VALUES (?, ?, ?, ?, ?) "
                    f"ON CONFLICT(entity_id) DO UPDATE SET "
                    f"name = excluded.name, enabled = excluded.enabled, updated_at = excluded.updated_at",
                    (entity_id, name, enabled, now_str, now_str),
                )
                conn.commit()
            finally:
                conn.close()

        try:
            await self._exec(hass, _upsert)
            return self.json({"success": True, "message": "配置已保存"})
        except Exception as exc:
            _LOGGER.exception("[xiaoai] 保存配置失败")
            return self.json({"success": False, "error": str(exc)}, status_code=500)

    async def delete(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_master_switch(hass)):
            return resp
        config_id = request.query.get("id", "").strip()
        entity_id = request.query.get("entity_id", "").strip()
        if not config_id and not entity_id:
            return self.json({"success": False, "error": "需提供 id 或 entity_id 参数"}, status_code=400)

        def _delete():
            conn = sqlite3.connect(self._db_path)
            try:
                if config_id:
                    conn.execute(
                        f"DELETE FROM {TABLE_XIAOAI_CONFIGS} WHERE id = ?", (config_id,)
                    )
                else:
                    conn.execute(
                        f"DELETE FROM {TABLE_XIAOAI_CONFIGS} WHERE entity_id = ?", (entity_id,)
                    )
                conn.commit()
                return conn.total_changes
            finally:
                conn.close()

        try:
            changed = await self._exec(hass, _delete)
            # 注意：仅删除配置，不删除对话历史记录
            return self.json({"success": True, "message": f"已删除 {changed} 条配置（历史记录保留）"})
        except Exception as exc:
            _LOGGER.exception("[xiaoai] 删除配置失败")
            return self.json({"success": False, "error": str(exc)}, status_code=500)


class XiaoaiHistoryView(_XiaoaiBaseView):
    """小爱对话记录查询。

    GET /api/ha_data_store/xiaoai/history?entity_id=xxx&start=&end=&limit=
      entity_id 必填
      start/end 可选，格式 yyyy-mm-dd 或 yyyy-mm-dd hh:mm:ss
      limit 可选，默认 500，最大 5000
    """

    url = "/api/ha_data_store/xiaoai/history"
    name = "api:ha_data_store:xiaoai_history"

    async def get(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_master_switch(hass)):
            return resp
        entity_id = request.query.get("entity_id", "").strip()
        if not entity_id:
            return self.json({"success": False, "error": "entity_id 必填"}, status_code=400)
        start = request.query.get("start", "").strip()
        end = request.query.get("end", "").strip()
        try:
            limit = int(request.query.get("limit", "500"))
        except ValueError:
            limit = 500
        limit = max(1, min(limit, 5000))

        def _query():
            conn = sqlite3.connect(self._db_path)
            try:
                conn.row_factory = sqlite3.Row
                sql = f"SELECT id, entity_id, user_text, ai_text, conv_time, created_at " \
                      f"FROM {TABLE_XIAOAI_CONVERSATIONS} WHERE entity_id = ?"
                params: list = [entity_id]
                if start:
                    sql += " AND conv_time >= ?"
                    params.append(start)
                if end:
                    sql += " AND conv_time <= ?"
                    params.append(end)
                sql += " ORDER BY conv_time DESC, id DESC LIMIT ?"
                params.append(limit)
                rows = conn.execute(sql, params).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()

        try:
            data = await self._exec(hass, _query)
            return self.json({"success": True, "data": data, "count": len(data)})
        except Exception as exc:
            _LOGGER.exception("[xiaoai] 查询对话记录失败")
            return self.json({"success": False, "error": str(exc)}, status_code=500)


# =========================================================================== #
#  注册入口                                                                      #
# =========================================================================== #
def register_api_views(hass: HomeAssistant, db_path: str) -> None:
    """注册小爱相关 API View。由 __init__._register_api_views 调用。"""
    hass.http.register_view(XiaoaiConfigView(db_path))
    hass.http.register_view(XiaoaiHistoryView(db_path))
