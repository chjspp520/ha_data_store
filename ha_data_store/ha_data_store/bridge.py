"""设备桥接核心模块 — 管理远程 HA WebSocket 连接与状态同步。

职责：
  - 建立 WebSocket 连接到远程 HA（长期令牌认证）
  - 订阅远程 state_changed 事件
  - 将远程实体状态变化推送到本地桥接代理实体
  - 断线自动重连（指数退避）
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN, TABLE_BRIDGE_CONNECTIONS, TABLE_BRIDGE_ENTITIES
from .logger import get_logger

_LOGGER = logging.getLogger(__name__)

# 默认重连参数
RECONNECT_BASE_DELAY = 5   # 秒
RECONNECT_MAX_DELAY = 300  # 秒（5分钟）
RECONNECT_MULTIPLIER = 1.5


def _load_bridge_connections(db_path: str) -> list[dict]:
    """从数据库加载所有已启用的桥接连接配置。"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(
            f"SELECT * FROM {TABLE_BRIDGE_CONNECTIONS} WHERE enabled = 1"
        ).fetchall()]
    finally:
        conn.close()


def _load_bridge_entities(db_path: str, connection_id: int) -> list[str]:
    """从数据库加载某个连接下的所有已启用桥接实体。"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [r["entity_id"] for r in conn.execute(
            f"SELECT entity_id FROM {TABLE_BRIDGE_ENTITIES} "
            f"WHERE connection_id = ? AND enabled = 1",
            (connection_id,),
        ).fetchall()]
    finally:
        conn.close()


class BridgeConnection:
    """单个远程 HA 的 WebSocket 桥接连接。"""

    def __init__(self, hass: HomeAssistant, conn_config: dict, db_path: str, entry_id: str):
        self._hass = hass
        self._conn_config = conn_config
        self._db_path = db_path
        self._entry_id = entry_id
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._session: aiohttp.ClientSession | None = None
        self._ws_task: asyncio.Task | None = None
        self._subscription_id: int | None = None
        self._msg_id_counter = 1
        self._shutdown = False
        self._reconnect_delay = RECONNECT_BASE_DELAY

        # entity_id → True（快速查找）
        self._bridged_entities: set[str] = set()

        local_logger = get_logger()
        if local_logger:
            local_logger.info(
                "[bridge] 桥接连接已初始化 remote=%s name=%s",
                conn_config["remote_url"], conn_config.get("name", ""),
            )

    # ------------------------------------------------------------------ #
    #  WebSocket 连接管理                                                    #
    # ------------------------------------------------------------------ #
    def _ws_url(self) -> str:
        """将 http(s) URL 转为 ws(s) URL。"""
        url = self._conn_config["remote_url"].rstrip("/")
        if url.startswith("https://"):
            return url.replace("https://", "wss://", 1) + "/api/websocket"
        return url.replace("http://", "ws://", 1) + "/api/websocket"

    async def _connect_ws(self) -> None:
        """建立 WebSocket 连接并认证。"""
        verify_ssl = self._conn_config.get("verify_ssl", 1) != 0
        timeout = aiohttp.ClientTimeout(total=10, connect=5)
        self._session = aiohttp.ClientSession(timeout=timeout)
        self._ws = await self._session.ws_connect(
            self._ws_url(), ssl=verify_ssl, heartbeat=30,
        )

        # 1) 接收 auth_required
        auth_msg = await self._ws.receive_json()
        if auth_msg.get("type") != "auth_required":
            raise ConnectionError(f"非预期的 WebSocket 消息: {auth_msg.get('type')}")

        # 2) 发送认证
        self._msg_id_counter += 1
        auth_payload = {
            "type": "auth",
            "access_token": self._conn_config["access_token"],
        }
        await self._ws.send_json(auth_payload)

        # 3) 接收 auth_ok
        auth_result = await self._ws.receive_json()
        if auth_result.get("type") == "auth_ok":
            local_logger = get_logger()
            if local_logger:
                local_logger.info(
                    "[bridge] WebSocket 认证成功 remote=%s",
                    self._conn_config["remote_url"],
                )
            _LOGGER.info("[bridge] WebSocket 认证成功: %s", self._conn_config["remote_url"])
        elif auth_result.get("type") == "auth_invalid":
            raise ConnectionError(
                f"WebSocket 认证失败: {auth_result.get('message', 'invalid token')}"
            )
        else:
            raise ConnectionError(f"非预期的认证响应: {auth_result.get('type')}")

    async def _subscribe_events(self) -> None:
        """订阅远程 state_changed 事件。"""
        self._msg_id_counter += 1
        sub_payload = {
            "id": self._msg_id_counter,
            "type": "subscribe_events",
            "event_type": "state_changed",
        }
        await self._ws.send_json(sub_payload)
        # 等待订阅确认
        result = await self._ws.receive_json()
        if result.get("type") == "result" and result.get("success"):
            self._subscription_id = result["id"]
            _LOGGER.info("[bridge] 已订阅远程 state_changed 事件 subscription_id=%s",
                         self._subscription_id)
        else:
            _LOGGER.error("[bridge] 订阅事件失败: %s", result)

    # ------------------------------------------------------------------ #
    #  主循环                                                                  #
    # ------------------------------------------------------------------ #
    async def run(self) -> None:
        """主 WebSocket 循环（包含断线重连）。"""
        self._shutdown = False
        self._reconnect_delay = RECONNECT_BASE_DELAY

        # 加载桥接实体列表
        self._bridged_entities = set(
            _load_bridge_entities(self._db_path, self._conn_config["id"])
        )

        while not self._shutdown:
            try:
                await self._connect_ws()
                await self._subscribe_events()
                self._reconnect_delay = RECONNECT_BASE_DELAY  # 重置延迟

                # 消息接收循环
                async for msg in self._ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        try:
                            data = json.loads(msg.data)
                            await self._handle_message(data)
                        except json.JSONDecodeError:
                            pass
                    elif msg.type == aiohttp.WSMsgType.CLOSED:
                        _LOGGER.warning("[bridge] WebSocket 被关闭 remote=%s",
                                        self._conn_config["remote_url"])
                        break
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        _LOGGER.error("[bridge] WebSocket 错误 remote=%s",
                                      self._conn_config["remote_url"])
                        break

            except asyncio.CancelledError:
                _LOGGER.info("[bridge] 桥接任务被取消 remote=%s",
                             self._conn_config["remote_url"])
                break
            except ConnectionError as e:
                _LOGGER.warning("[bridge] 连接失败: %s", e)
            except Exception as e:
                _LOGGER.exception("[bridge] 桥接异常: %s", e)

            # 清理旧连接
            await self._close_ws()

            if self._shutdown:
                break

            # 重连延迟
            _LOGGER.info("[bridge] %d 秒后重连... remote=%s",
                         self._reconnect_delay, self._conn_config["remote_url"])
            await asyncio.sleep(self._reconnect_delay)
            self._reconnect_delay = min(
                int(self._reconnect_delay * RECONNECT_MULTIPLIER),
                RECONNECT_MAX_DELAY,
            )

        await self._close_ws()

    async def _handle_message(self, data: dict) -> None:
        """处理 WebSocket 消息。"""
        msg_type = data.get("type")

        if msg_type == "event":
            event = data.get("event", {})
            if event.get("event_type") == "state_changed":
                ev_data = event.get("data", {})
                entity_id = ev_data.get("entity_id", "")
                if entity_id in self._bridged_entities:
                    new_state = ev_data.get("new_state")
                    if new_state is not None:
                        await self._push_to_local(entity_id, new_state)
                    # old_state 也可能为 None（新实体上线），不需要特殊处理

    async def _push_to_local(self, entity_id: str, new_state: dict) -> None:
        """将远程状态推送到本地的代理实体。"""
        registry = self._hass.data.get(DOMAIN, {}).get("bridge_entity_instances", {})
        entity = registry.get(entity_id)
        if entity:
            entity.push_remote_state(new_state)

    async def _close_ws(self) -> None:
        """关闭 WebSocket 连接。"""
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session and not self._session.closed:
            await self._session.close()
        self._ws = None
        self._session = None
        self._subscription_id = None

    async def stop(self) -> None:
        """停止桥接连接。"""
        self._shutdown = True
        if self._ws_task and not self._ws_task.done():
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass

    def start(self) -> asyncio.Task:
        """启动桥接（在 Home Assistant 事件循环中创建后台任务）。

        使用 async_create_background_task 而非 async_create_task，
        因为 run() 是长期运行的 WebSocket 循环，前者不会被 HA 启动流程等待，
        避免阻塞 "wrapping up the start up phase"。
        """
        self._ws_task = self._hass.async_create_background_task(
            self.run(), f"bridge_connection_{self._conn_config.get('id', 'unknown')}"
        )
        return self._ws_task


# =========================================================================== #
#  全局桥接管理器                                                                    #
# =========================================================================== #
class BridgeManager:
    """管理所有桥接连接的生命周期。"""

    def __init__(self, hass: HomeAssistant, db_path: str, entry_id: str):
        self._hass = hass
        self._db_path = db_path
        self._entry_id = entry_id
        self._connections: dict[int, BridgeConnection] = {}  # connection_id → BridgeConnection

    async def start_all(self) -> None:
        """启动所有已启用的桥接连接。"""
        conn_configs = await self._hass.async_add_executor_job(
            _load_bridge_connections, self._db_path
        )

        if not conn_configs:
            _LOGGER.info("[bridge] 没有已启用的桥接连接")
            return

        _LOGGER.info("[bridge] 正在启动 %d 个桥接连接...", len(conn_configs))
        for cfg in conn_configs:
            conn = BridgeConnection(self._hass, cfg, self._db_path, self._entry_id)
            self._connections[cfg["id"]] = conn
            conn.start()

    async def stop_all(self) -> None:
        """停止所有桥接连接。"""
        for conn_id, conn in list(self._connections.items()):
            await conn.stop()
        self._connections.clear()

    async def reload(self) -> None:
        """重新加载配置并重启连接。"""
        await self.stop_all()
        await self.start_all()
