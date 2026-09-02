"""小爱对话采集模块 — 独立模块。

职责：
  - 建表 xiaoai_configs / xiaoai_conversations
  - state_changed 事件采集（upsert 合并"用户说话"与"AI 回复"两次触发）
  - HTTP API：配置 CRUD + 对话记录查询

数据来源（小爱对话 sensor 的一次 state_changed）：
  state                              → 用户说的话 (user_text)
  attributes.answers[*].tts.text     → AI 回复 (ai_text)，优先 type==TTS 取 tts.text
  attributes.answers[*].llm.text     → AI 回复 (ai_text)，其次 type==LLM 取 llm.text（连续对话/大模型回复）
  attributes.timestamp               → 对话时间 (conv_time)，格式 yyyy-mm-dd hh:mm:ss
                                       （原值为 ISO8601，取前19位并替换 T 为空格，无需时区转换）
  attributes.answers[*].type         → 事件类型 (type)，遍历找第一个非 TTS 的 type 键值
                                       （如 ALERT / LLM；全为 TTS 则存空，表示普通对话）
  attributes                         → 完整 JSON (other)，ensure_ascii=False，default=str 兜底，信息不丢失

采集策略（upsert）：
  小爱一次对话在 HA 中通常触发两次 state_changed：
    ① 用户说话：state 变为新话，timestamp 更新，answers 可能尚未填充
    ② AI 回复：state 不变，answers 更新，timestamp 仍是本次对话时间
  以 (entity_id, conv_time, user_text) 为唯一键：
    ① 时插入 user_text + conv_time，ai_text/type/other 暂空
    ② 时同键命中，UPDATE 补充 ai_text + type + other
  使用 INSERT ... ON CONFLICT(...) DO UPDATE 一条语句完成。
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .logger import get_logger
from .const import DOMAIN, TABLE_API_KEYS, TABLE_API_SETTINGS

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
                type       TEXT NOT NULL DEFAULT '',
                other      TEXT NOT NULL DEFAULT '',
                UNIQUE(entity_id, conv_time, user_text)
            )
            """
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_xiaoai_entity_time "
            f"ON {TABLE_XIAOAI_CONVERSATIONS} (entity_id, conv_time)"
        )
        # 迁移：为已存在的表补充 type / other 列（CREATE TABLE IF NOT EXISTS 不会改老表）
        cols = conn.execute(f"PRAGMA table_info({TABLE_XIAOAI_CONVERSATIONS})").fetchall()
        col_names = {c[1] for c in cols}
        if "type" not in col_names:
            conn.execute(
                f"ALTER TABLE {TABLE_XIAOAI_CONVERSATIONS} ADD COLUMN type TEXT NOT NULL DEFAULT ''"
            )
        if "other" not in col_names:
            conn.execute(
                f"ALTER TABLE {TABLE_XIAOAI_CONVERSATIONS} ADD COLUMN other TEXT NOT NULL DEFAULT ''"
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

    # AI 回复 + 事件类型：遍历 answers
    #   - ai_text：优先 type==TTS 取 tts.text；其次 type==LLM 取 llm.text（连续对话/大模型回复）
    #   - type：找第一个 type!=TTS 的项存其键值（如 ALERT / LLM）；全为 TTS 则存空
    #   - other：完整 attributes 的 JSON，信息不丢失（default=str 兜底不可序列化对象）
    ai_text = ""
    conv_type = ""
    answers = attrs.get("answers")
    if isinstance(answers, list):
        for ans in answers:
            if not isinstance(ans, dict):
                continue
            ans_type = ans.get("type", "")
            if ans_type == "TTS":
                tts = ans.get("tts")
                if isinstance(tts, dict) and not ai_text:
                    ai_text = (tts.get("text") or "").strip()
            elif ans_type == "LLM":
                # LLM 类型：连续对话/大模型回复，文本在 llm.text
                llm = ans.get("llm")
                if isinstance(llm, dict) and not ai_text:
                    ai_text = (llm.get("text") or "").strip()
                if not conv_type:
                    conv_type = ans_type  # LLM 作为事件类型
            elif ans_type and not conv_type:
                conv_type = ans_type  # 第一个非 TTS 类型（如 ALERT）
    # other：完整 attributes 的 JSON，default=str 兜底不可序列化对象（如 datetime）
    try:
        other_text = json.dumps(attrs, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        other_text = ""

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    local_logger = get_logger()

    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            f"INSERT INTO {TABLE_XIAOAI_CONVERSATIONS} "
            f"(entity_id, user_text, ai_text, conv_time, created_at, type, other) "
            f"VALUES (?, ?, ?, ?, ?, ?, ?) "
            f"ON CONFLICT(entity_id, conv_time, user_text) DO UPDATE SET "
            f"ai_text = excluded.ai_text, type = excluded.type, other = excluded.other",
            (entity_id, user_text, ai_text, conv_time, now_str, conv_type, other_text),
        )
        conn.commit()
        if local_logger:
            local_logger.info(
                "[xiaoai] 采集对话 entity_id=%s conv_time=%s user=%r ai=%r type=%s",
                entity_id, conv_time,
                user_text[:40], ai_text[:40], conv_type or "-",
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
        sql = (f"SELECT id, entity_id, user_text, ai_text, conv_time, created_at, type, other "
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

    def _check_api_enabled(self, request: web.Request) -> web.Response | None:
        """检查 API 访问开关 + API Key。开关关闭或无有效 Key 返回 403。"""
        hass: HomeAssistant = request.app["hass"]
        if not hass.data.get(DOMAIN, {}).get("api_enabled", True):
            return web.Response(status=403)
        key = request.query.get("key", "") or request.headers.get("Authorization", "").replace("Bearer ", "")
        if not key:
            return web.Response(status=403)

        def _verify():
            conn = sqlite3.connect(self._db_path)
            try:
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
            # 刷新受监控实体白名单（含 xiaoai_entities），使新配置立即生效
            from .http_api import _refresh_monitored
            await _refresh_monitored(hass, self._db_path)
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
            # 刷新受监控实体白名单（含 xiaoai_entities），移除已删除的实体
            from .http_api import _refresh_monitored
            await _refresh_monitored(hass, self._db_path)
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
                sql = f"SELECT id, entity_id, user_text, ai_text, conv_time, created_at, type, other " \
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


class XiaoaiRecordView(_XiaoaiBaseView):
    """写入单条对话记录（前端播报/命令等主动写入）。

    POST /api/ha_data_store/xiaoai/record?key=xxx
      body JSON: {entity_id, user_text, type, conv_time}
        entity_id  必填，对话记录实体 ID（卡片配置的 entity，如 sensor.xxx_conversation）
        user_text  必填，用户发送的文本（播报内容）
        type       可选，记录类型（如 play_text），默认空
        conv_time  必填，对话时间，格式 yyyy-mm-dd HH:MM:SS（前端生成）
      返回 {success: true} 或 {success: false, error}

    鉴权：需 API Key（?key=xxx），复用 _check_api_enabled。
    用途：播报（play_text_entity）不触发 sensor state_changed，由前端主动写入；
          entity_id 必须为卡片配置的 entity（与查询一致），而非 play_text_entity。
    """

    url = "/api/ha_data_store/xiaoai/record"
    name = "api:ha_data_store:xiaoai_record"

    async def post(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_api_enabled(request)):
            return resp
        try:
            body = await request.json()
        except Exception:
            return self.json({"success": False, "error": "请求体需为 JSON"}, status_code=400)

        entity_id = (body.get("entity_id") or "").strip()
        user_text = (body.get("user_text") or "").strip()
        conv_type = (body.get("type") or "").strip()
        conv_time = (body.get("conv_time") or "").strip()
        if not entity_id or not user_text or not conv_time:
            return self.json({"success": False, "error": "entity_id, user_text, conv_time 必填"}, status_code=400)

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        def _insert():
            conn = sqlite3.connect(self._db_path)
            try:
                # ai_text 留空（播报无 AI 回复）；other 留空
                # upsert：同 entity_id+conv_time+user_text 则更新 type，否则插入
                conn.execute(
                    f"INSERT INTO {TABLE_XIAOAI_CONVERSATIONS} "
                    f"(entity_id, user_text, ai_text, conv_time, created_at, type, other) "
                    f"VALUES (?, ?, ?, ?, ?, ?, ?) "
                    f"ON CONFLICT(entity_id, conv_time, user_text) DO UPDATE SET "
                    f"type = excluded.type",
                    (entity_id, user_text, "", conv_time, now_str, conv_type, ""),
                )
                conn.commit()
            finally:
                conn.close()

        try:
            await self._exec(hass, _insert)
            _LOGGER.info(
                "[xiaoai] 写入记录 entity_id=%s conv_time=%s type=%s user=%r",
                entity_id, conv_time, conv_type or "-", user_text[:40],
            )
            return self.json({"success": True})
        except Exception as exc:
            _LOGGER.exception("[xiaoai] 写入记录失败")
            return self.json({"success": False, "error": str(exc)}, status_code=500)


# =========================================================================== #
#  注册入口                                                                      #
# =========================================================================== #
def register_api_views(hass: HomeAssistant, db_path: str) -> None:
    """注册小爱相关 API View。由 __init__._register_api_views 调用。"""
    hass.http.register_view(XiaoaiConfigView(db_path))
    hass.http.register_view(XiaoaiHistoryView(db_path))
    hass.http.register_view(XiaoaiRecordView(db_path))
