"""通知中心后端 — HA 官方推送统一投递 + 历史落库。

职责边界（方案：c:\\HA\\src\\plan\\notification-center-plan.md）：
  1. 唯一投递入口 `NotificationManager.send()`：调用 `notify.*` 服务把通知推到手机；
  2. 落库 `notifications` 表，供前端通知卡片展示与历史分页；
  3. 刷新 `sensor.ha_data_store_notifications`（state = 未读数），前端据此自动弹窗。

不做（刻意）：
  · 不做内容模板库 / 富文本 / 按渠道定制文案（内容由调用方给，本模块原样转发）；
  · 不做 WS 事件通道（前端订阅 sensor 状态即可）；
  · 不做页面可见性监听（前端自己的事）。
"""
from __future__ import annotations

import hashlib
import inspect
import json
import logging
import posixpath
import re
import os
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.core import HomeAssistant

from .const import (
    DEFAULT_TIMEZONE,
    DOMAIN,
    NOTIFICATION_ATTACH_ATTR_MAX,
    NOTIFICATION_ATTACH_KINDS,
    NOTIFICATION_ATTACH_MAX,
    NOTIFICATION_CLEANUP_HOUR,
    NOTIFICATION_DETAIL_ATTR_MAX,
    NOTIFICATION_DETAIL_MAX,
    NOTIFICATION_LEVELS,
    NOTIFICATION_MESSAGE_MAX,
    NOTIFICATION_RETENTION_DAYS,
    NOTIFICATION_TITLE_MAX,
    NOTIFICATION_TMP_SUBDIR,
    NOTIFICATION_TODAY_LATEST_MAX,
    TABLE_NOTIFICATIONS,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_TITLE = "通知"
DEFAULT_CATEGORY = "通知"
# 通用 notify 服务名：不是具体设备，不允许作为收件人
_GENERIC_NOTIFY_SERVICES = ("send_message", "notify")

# level 只校验格式（不再列白名单）：小写字母开头，允许字母/数字/下划线/短横，最长 24
NOTIFICATION_LEVEL_RE = re.compile(r"^[a-z][a-z0-9_-]{0,23}$")
# 单条通知最多指定多少个收件用户（展开后仍受 targets 上限约束）
NOTIFICATION_USERS_MAX = 20
# 「未关联用户」分组的虚拟 user_id：这些设备没匹配到具体 HA 用户，但仍要能被选中
UNBOUND_USER_ID = "__unbound__"

_COLUMNS = (
    "ts, level, category, title, message, detail, targets, status, results, "
    "url, payload, source_type, source_id, source_name, attachments, from_config_id, "
    "user_ids, read, read_at"
)

# 附件扩展名 → MIME（只用于前端决定用 img/video/audio 渲染，不影响投递）
_EXT_MIME = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
    ".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime",
    ".m4v": "video/x-m4v",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg",
    ".m4a": "audio/mp4", ".aac": "audio/aac", ".flac": "audio/flac",
    ".pdf": "application/pdf", ".txt": "text/plain", ".csv": "text/csv",
    ".json": "application/json", ".zip": "application/zip",
}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _tz() -> timezone:
    return timezone(timedelta(hours=DEFAULT_TIMEZONE))


def _today_start_ms() -> int:
    """东八区今天 00:00 的毫秒时间戳。"""
    now = datetime.now(_tz())
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp() * 1000)


def _tmp_attachment_prefix() -> str:
    """集成自己生成的附件目录前缀（config 相对的 posix 路径）。"""
    return "/".join(NOTIFICATION_TMP_SUBDIR) + "/"


def _is_tmp_attachment(path: str) -> bool:
    """附件是否落在 `_notify_tmp` 下 —— 只有这些是集成自己生成的，可随记录一起删。

    用户自己指的路径（如 `eques_dingdong/image/xxx.jpg`）绝对不碰。
    """
    p = str(path or "").replace("\\", "/").strip()
    if not p:
        return False
    while p.startswith("./"):
        p = p[2:]
    p = p.lstrip("/")
    # 规范化后再判前缀：`a/../b` 折成 `b`；仍带 `..` 的直接拒绝（防穿越）
    p = posixpath.normpath(p) if p else ""
    if not p or p == ".." or p.startswith("../"):
        return False
    return p.startswith(_tmp_attachment_prefix())


def _attachment_paths(raw: Any) -> set[str]:
    """从 attachments 列（JSON 文本）里取出**可清理的**临时文件路径。"""
    out: set[str] = set()
    if not raw:
        return out
    try:
        items = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return out
    if not isinstance(items, list):
        return out
    for it in items:
        if not isinstance(it, dict):
            continue
        p = str(it.get("path") or "").strip()
        if p and _is_tmp_attachment(p):
            out.add(p)
    return out


def _loads(raw: Any, default: Any) -> Any:
    if isinstance(raw, (list, dict)):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return default
    try:
        out = json.loads(raw)
        return out if isinstance(out, type(default)) else default
    except Exception:
        return default


def _row_get(row: sqlite3.Row, key: str, default: Any = "") -> Any:
    """安全读取一行里的列：老库还没 ALTER 补列时（迁移晚于首次读），不能抛。"""
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


def row_to_item(row: sqlite3.Row, *, brief: bool = False) -> dict:
    """DB 行 → API/传感器用的通知对象。brief=True 时截断 detail（属性体积保护）。"""
    detail = row["detail"] or ""
    truncated = False
    if brief and len(detail) > NOTIFICATION_DETAIL_ATTR_MAX:
        detail = detail[:NOTIFICATION_DETAIL_ATTR_MAX]
        truncated = True
    item = {
        "id": row["id"],
        "ts": row["ts"],
        "level": row["level"] or "info",
        "category": row["category"] or DEFAULT_CATEGORY,
        "title": row["title"] or DEFAULT_TITLE,
        "message": row["message"] or "",
        "detail": detail,
        "targets": _loads(row["targets"], []),
        "status": row["status"] or "",
        "results": _loads(row["results"], []),
        "url": row["url"] or "",
        "source_type": row["source_type"] or "",
        "source_id": row["source_id"] or "",
        "source_name": row["source_name"] or "",
        "read": 1 if row["read"] else 0,
        "read_at": row["read_at"] or 0,
        "attachments": _loads(_row_get(row, "attachments", ""), []),
        "att_total": len(_loads(_row_get(row, "attachments", ""), [])),
        "from_config_id": _row_get(row, "from_config_id", "") or "",
        "user_ids": _loads(_row_get(row, "user_ids", ""), []),
    }
    if brief:
        item["detail_truncated"] = truncated
        # 属性体积：只留前 N 个附件，其余让前端走单条 API
        atts = item["attachments"]
        if len(atts) > NOTIFICATION_ATTACH_ATTR_MAX:
            item["attachments"] = atts[:NOTIFICATION_ATTACH_ATTR_MAX]
        item["att_trimmed"] = len(atts) > NOTIFICATION_ATTACH_ATTR_MAX
        # ⚠️ 这里**只裁数量，不裁字段** —— 附件里的 `error`（快照失败原因）
        #    必须原样带到 sensor 属性上，前端才能把"设备没开机"和"没配 API Key"
        #    区分开（见 notice-center.js 的 _ncCreateAttachment）。
    return item


class NotificationManager:
    """通知投递 + 历史存储。实例挂在 hass.data[DOMAIN]["notification_manager"]。"""

    def __init__(self, hass: HomeAssistant, db_path: str) -> None:
        self._hass = hass
        self._db_path = db_path
        self._cleanup_unsub = None
        # 跨天重算（今日口径在 00:00 翻转，sensor 必须跟着重算）
        self._midnight_unsub = None
        self._refresh_pending = False
        self._tpl_cache: dict[str, Any] = {}
        # 「用户 → 设备」映射缓存（auth + config_entries 扫描结果）
        self._catalog_cache: list[dict] | None = None
        self._catalog_at: float = 0.0

    # ------------------------------------------------------------------ #
    #  生命周期                                                            #
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        """注册每日清理（不产生常驻任务，避免 HA 启动/关停被卡住）。"""
        if self._cleanup_unsub is not None:
            return
        # 老库补列（attachments / from_config_id）：CREATE TABLE IF NOT EXISTS 不会加列
        try:
            await self._hass.async_add_executor_job(self._migrate_sync)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("[notification] 表结构迁移失败（附件字段可能不可用）")
        try:
            from homeassistant.helpers.event import async_track_time_change

            self._cleanup_unsub = async_track_time_change(
                self._hass, self._daily_cleanup,
                hour=NOTIFICATION_CLEANUP_HOUR, minute=0, second=0,
            )
        except Exception:  # pragma: no cover
            _LOGGER.exception("[notification] 清理定时器注册失败")
        # 跨天重算：state / latest / by_* 全是「今日」口径，00:00 过后必须立刻重算，
        #   否则拉手会一直显示昨天的未读数（要等下一条通知或手动刷新才更新）。
        try:
            from homeassistant.helpers.event import async_track_time_change

            self._midnight_unsub = async_track_time_change(
                self._hass, self._do_refresh,
                hour=0, minute=0, second=5,
            )
        except Exception:  # pragma: no cover
            _LOGGER.exception("[notification] 跨天重算定时器注册失败")

    async def stop(self) -> None:
        if self._cleanup_unsub is not None:
            try:
                self._cleanup_unsub()
            except Exception:
                pass
            self._cleanup_unsub = None
        if self._midnight_unsub is not None:
            try:
                self._midnight_unsub()
            except Exception:
                pass
            self._midnight_unsub = None

    def _migrate_sync(self) -> None:
        """给已存在的 notifications 表补 attachments / from_config_id 两列。"""
        conn = sqlite3.connect(self._db_path, timeout=5)
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            cols = {
                r[1] for r in conn.execute(
                    f"PRAGMA table_info({TABLE_NOTIFICATIONS})"
                ).fetchall()
            }
            if not cols:
                return  # 表还没建，交给 __init__ 的建表语句
            if "attachments" not in cols:
                conn.execute(
                    f"ALTER TABLE {TABLE_NOTIFICATIONS} "
                    "ADD COLUMN attachments TEXT NOT NULL DEFAULT '[]'"
                )
                _LOGGER.info("[notification] 已补列 attachments")
            if "from_config_id" not in cols:
                conn.execute(
                    f"ALTER TABLE {TABLE_NOTIFICATIONS} "
                    "ADD COLUMN from_config_id TEXT NOT NULL DEFAULT ''"
                )
                _LOGGER.info("[notification] 已补列 from_config_id")
            if "user_ids" not in cols:
                conn.execute(
                    f"ALTER TABLE {TABLE_NOTIFICATIONS} "
                    "ADD COLUMN user_ids TEXT NOT NULL DEFAULT '[]'"
                )
                _LOGGER.info("[notification] 已补列 user_ids")
            conn.commit()
        finally:
            conn.close()

    async def _daily_cleanup(self, _now=None) -> None:
        try:
            removed = await self.cleanup()
            if removed:
                _LOGGER.info("[notification] 历史清理完成，删除 %s 条", removed)
        except Exception:
            _LOGGER.exception("[notification] 历史清理失败")

    # ------------------------------------------------------------------ #
    #  对外：发送                                                            #
    # ------------------------------------------------------------------ #
    async def send(
        self,
        payload: dict | None,
        *,
        source: dict | None = None,
        dry: bool = False,
        delivery_result: tuple[list[dict], str] | None = None,
    ) -> dict:
        """唯一投递入口。

        payload 字段：title / message / detail / level / category / targets / url
        source：{type, id, name, trigger_type}（自动化动作下一批接入时传入）
        dry=True：只渲染 + 校验，不落库、不投递。
        delivery_result：调用方已完成投递时传入 (results, status)，
            中心只做落库/计未读，不再重复投递（供 ha_harmony_pushkit 镜像登记用）。
        """
        source = source or {}
        data, error = self._normalize(payload)
        if error:
            return {"success": False, "error": error}

        ctx = {
            "automation_name": str(source.get("name") or ""),
            "automation_id": str(source.get("id") or ""),
            "trigger_type": str(source.get("trigger_type") or ""),
        }
        for key in ("title", "message", "detail"):
            data[key] = await self._render(data.get(key) or "", ctx)
            # 渲染后**仍带模板标记** → 一定是渲染失败降级了原文。
            #   这里补一条明确日志（带上字段名与前 100 字），省得逐字段排查。
            if isinstance(data[key], str) and ("{{" in data[key] or "{%" in data[key]):
                _LOGGER.warning(
                    "[notification] %s 渲染后仍含模板标记（渲染失败，已降级原文）: %.100s",
                    key, data[key],
                )
        # 渲染**之后**才按展示上限截断：
        #   · 模板原文可能几百字（`{% if %}…{% endif %}`），渲染结果通常很短；
        #   · 顺序反了就会把模板切残，渲染失败后原样发到手机。
        data["title"] = (data["title"] or DEFAULT_TITLE)[:NOTIFICATION_TITLE_MAX]
        data["message"] = (data["message"] or "")[:NOTIFICATION_MESSAGE_MAX]
        data["detail"] = (data["detail"] or "")[:NOTIFICATION_DETAIL_MAX]

        # 附件：路径/实体都可能写模板 → 先渲染再解析成最终路径。
        # **只落库**，不参与 notify 推送（手机只用文本）
        if data.get("attachments"):
            data["attachments"] = await self._resolve_attachments(data["attachments"], ctx)

        # 收件用户 → 该用户名下所有 notify 服务，与显式 targets 合并去重
        if data.get("users"):
            data["targets"] = await self.expand_users(
                data["users"], data["targets"]
            )
            if not data["targets"]:
                return {"success": False, "error": "所选收件用户没有已注册的推送设备"}

        # 记录涉及的用户（用于卡片「只看我的」过滤与 by_user 统计）
        data["user_ids"] = await self.resolve_user_ids(
            data.get("users"), data["targets"]
        )

        # 点击通知时打开的页面：
        #   · `raw_url` = 用户配的目标页，**落库用**（保持原样，别把 nc_open 写进数据库）
        #   · `data["url"]` 额外拼 `nc_open=1`，只给**推送**用 —— 前端据此直接展开抽屉
        #     （直接打开 App 时 URL 没有这个参数 → 不自动展开）
        raw_url = str(data.get("url") or "").strip()
        data["url"] = self._click_url(raw_url)

        if dry:
            return {
                "success": True,
                "dry": True,
                "payload": data,
            }

        if delivery_result is not None:
            # 投递已由调用方完成（如 ha_harmony_pushkit.send_message），中心只登记
            results, status = delivery_result
        else:
            results, status = await self._deliver(data)

        record = {
            "ts": _now_ms(),
            "level": data["level"],
            "category": data["category"],
            "title": data["title"],
            "message": data["message"],
            "detail": data["detail"],
            "targets": data["targets"],
            "status": status,
            "results": results,
            # 落库存**原始**目标页（不带 nc_open）：那是"这条通知指向哪里"，
            #   而 nc_open=1 只是"怎么唤起抽屉"的投递细节，不该污染数据
            "url": raw_url,
            "payload": payload or {},
            "attachments": data.get("attachments") or [],
            "from_config_id": data.get("from_config_id") or "",
            "user_ids": data.get("user_ids") or [],
            "source_type": str(source.get("type") or "service"),
            "source_id": str(source.get("id") or ""),
            "source_name": str(source.get("name") or ""),
        }
        row_id = await self._store(record)
        self._schedule_refresh()
        return {
            "success": True,
            "id": row_id,
            "status": status,
            "results": results,
            "payload": data,
        }

    # ------------------------------------------------------------------ #
    #  对外：查询与状态                                                      #
    # ------------------------------------------------------------------ #
    async def list_notifications(
        self,
        *,
        limit: int = 20,
        offset: int = 0,
        unread_only: bool = False,
        category: str = "",
        level: str = "",
        since: int = 0,
        user_id: str = "",
    ) -> dict:
        return await self._hass.async_add_executor_job(
            self._list_sync, limit, offset, unread_only, category, level, since, user_id
        )

    async def get(self, notification_id: int) -> dict | None:
        return await self._hass.async_add_executor_job(self._get_sync, notification_id)

    async def mark_read(self, ids=None, all_: bool = False) -> int:
        return await self._hass.async_add_executor_job(self._mark_read_sync, ids, all_)

    async def mark_unread(self, ids=None, all_: bool = False) -> int:
        return await self._hass.async_add_executor_job(self._mark_unread_sync, ids, all_)

    async def remove(self, ids=None, mode: str | None = None) -> int:
        return await self._hass.async_add_executor_job(self._remove_sync, ids, mode)

    async def cleanup(self) -> int:
        return await self._hass.async_add_executor_job(self._cleanup_sync)

    def targets(self) -> list[dict]:
        """可用收件人：hass 里注册的 notify.* 平台服务（排除通用服务）。"""
        services: dict = {}
        try:
            getter = getattr(self._hass.services, "async_services", None)
            if callable(getter):
                services = getter() or {}
            else:  # pragma: no cover - 老版本兜底
                services = getattr(self._hass.services, "services", {}) or {}
        except Exception:
            services = {}
        notify = services.get("notify") or {}
        out: list[dict] = []
        for name, svc in notify.items():
            if name in _GENERIC_NOTIFY_SERVICES:
                continue
            label = ""
            try:
                label = str(getattr(svc, "name", "") or "")
            except Exception:
                label = ""
            out.append({
                "service": f"notify.{name}",
                "name": label or name,
            })
        out.sort(key=lambda x: x["service"])
        return out

    async def users_with_targets(self, *, use_cache: bool = True) -> list[dict]:
        """HA 用户 + 各自名下的 notify 服务。

        [{"user_id", "name", "is_admin", "services": [{"service", "device_name"}]}]

        服务名 ⇄ 设备的对应靠 `mobile_app` config entry：
        `entry.data["device_name"]` → slugify → `notify.mobile_app_<slug>`。
        匹配不上的服务归到 `user_id=""` 的「未关联用户」分组，保证不漏。
        """
        try:
            from homeassistant.util import slugify
        except Exception:  # noqa: BLE001
            # 测试环境 / 极简运行时没有 homeassistant 包：退化成本地 slug
            def slugify(value):
                return re.sub(r"[^a-z0-9]+", "_", str(value).lower()).strip("_")

        by_service = {t["service"]: t for t in self.targets()}
        users: dict[str, dict] = {}
        # 30s 缓存：sensor 会频繁刷新，没必要每次都扫 auth + config_entries
        now = time.time()
        if (use_cache and self._catalog_cache is not None
                and now - self._catalog_at < 30):
            return self._catalog_cache

        try:
            # ⚠️ async_get_users() 是协程，必须 await（早前同步调用会抛
            #    "coroutine was never awaited" 并被 except 吞掉 → 用户列表恒为空）
            for u in await self._hass.auth.async_get_users():
                users[u.id] = {
                    "user_id": u.id,
                    "name": u.name or u.id,
                    "is_admin": bool(getattr(u, "is_admin", False)),
                    "services": [],
                }
        except Exception:  # noqa: BLE001
            _LOGGER.debug("[notification] 读取用户列表失败", exc_info=True)

        # 未关联分组用稳定标识而不是空串：前端把空 user_id 当"不可选"，
        # 一旦所有服务都匹配不上用户（device_name 含中文 / emoji 时 slug 对不上），
        # 整份「收件用户」列表就会变成一项且被禁用 —— 用户完全选不了。
        unknown = {
            "user_id": UNBOUND_USER_ID,
            "name": "未关联用户",
            "is_admin": False,
            "services": [],
        }
        matched: set[str] = set()
        try:
            for entry in self._hass.config_entries.async_entries("mobile_app"):
                data = entry.data or {}
                uid = str(data.get("user_id") or "")
                dev = str(data.get("device_name") or entry.title or "")
                if not dev:
                    continue
                svc = f"notify.mobile_app_{slugify(dev)}"
                item = by_service.get(svc)
                if not item:
                    continue
                rec = {"service": svc, "device_name": dev}
                matched.add(svc)
                if uid in users:
                    users[uid]["services"].append(rec)
                else:
                    unknown["services"].append(rec)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("[notification] 读取 mobile_app 条目失败", exc_info=True)

        for svc, item in by_service.items():
            if svc not in matched:
                unknown["services"].append({
                    "service": svc,
                    "device_name": item.get("name") or svc,
                })

        out = list(users.values())
        if unknown["services"]:
            out.append(unknown)
        self._catalog_cache = out
        self._catalog_at = now
        return out

    async def targets_with_users(self) -> list[dict]:
        """扁平化的「设备 + 归属用户」，供 sensor 属性 / 前端下拉使用。

        [{"service", "name", "user_id", "user_name"}]

        前端据此可以在**不配 API key** 的情况下拿到「收件用户 / 通知设备」清单
        （sensor 属性任何卡片都能读，而接口需要 api_base_url + key）。
        """
        out: list[dict] = []
        for u in await self.users_with_targets():
            for sv in u["services"]:
                out.append({
                    "service": sv["service"],
                    "name": sv.get("device_name") or sv["service"],
                    "user_id": u["user_id"],
                    "user_name": u["name"],
                })
        out.sort(key=lambda x: (x["user_name"], x["service"]))
        return out

    async def service_user_map(self) -> dict[str, str]:
        """{notify 服务名: user_id} —— 反查用（targets → 用户）。"""
        out: dict[str, str] = {}
        for u in await self.users_with_targets():
            for svc in u["services"]:
                if (svc["service"]
                        and u["user_id"]
                        and u["user_id"] != UNBOUND_USER_ID):
                    out[svc["service"]] = u["user_id"]
        return out

    async def expand_users(
        self, user_ids: list[str], explicit: list[str],
    ) -> list[str]:
        """user_id 列表 → 该用户所有 notify 服务，与显式 targets 合并去重。"""
        out = list(dict.fromkeys(explicit or []))
        wanted = {str(u) for u in (user_ids or []) if u}
        if not wanted:
            return out
        for svc, uid in (await self.service_user_map()).items():
            if uid in wanted and svc not in out:
                out.append(svc)
        # 「未关联用户」：把这些设备全部展开（它们没有归属用户，只能按分组处理）
        if UNBOUND_USER_ID in wanted:
            for u in await self.users_with_targets():
                if u["user_id"] != UNBOUND_USER_ID:
                    continue
                for sv in u["services"]:
                    if sv["service"] and sv["service"] not in out:
                        out.append(sv["service"])
        return out

    async def resolve_user_ids(
        self, user_ids: list[str], targets: list[str],
    ) -> list[str]:
        """落库用的 user_ids = 显式 users ∪ 由 targets 反查到的用户。"""
        out = list(dict.fromkeys(
            str(u) for u in (user_ids or [])
            if u and str(u) != UNBOUND_USER_ID
        ))
        if targets:
            mapping = await self.service_user_map()
            for svc in targets:
                uid = mapping.get(svc)
                if uid and uid not in out:
                    out.append(uid)
        return out

    # ------------------------------------------------------------------ #
    #  内部：规整与渲染                                                      #
    # ------------------------------------------------------------------ #
    def _normalize(self, payload: dict | None) -> tuple[dict, str]:
        p = payload if isinstance(payload, dict) else {}

        # ── 收件人 ──
        raw_targets = p.get("targets")
        if isinstance(raw_targets, str):
            raw_targets = raw_targets.replace("，", ",").split(",")
        if not isinstance(raw_targets, (list, tuple)):
            raw_targets = []
        targets: list[str] = []
        for item in raw_targets:
            t = str(item or "").strip()
            if t and t not in targets:
                targets.append(t)
        # ── 收件用户（user_id 列表；展开成该用户所有 notify 服务由 send() 完成）──
        raw_users = p.get("users")
        if isinstance(raw_users, str):
            raw_users = raw_users.replace("，", ",").split(",")
        if not isinstance(raw_users, (list, tuple)):
            raw_users = []
        users: list[str] = []
        for item in raw_users:
            u = str(item or "").strip()
            if u and u not in users:
                users.append(u)
        users = users[:NOTIFICATION_USERS_MAX]

        if not targets and not users:
            return {}, "no_target"
        # 只允许 "notify.<service>"：前缀必须对、服务名非空、服务名里不能再有别的点
        bad = [
            t for t in targets
            if not t.startswith("notify.") or not t[7:] or "." in t[7:]
        ]
        if bad:
            return {}, f"invalid_target:{','.join(bad)}"

        message = str(p.get("message") or "").strip()
        if not message:
            return {}, "empty_message"

        # level 不再走白名单：只校验格式（小写字母开头 + 字母/数字/下划线/短横），
        # 便于以后扩展「消息 / 操作 / 紧急…」，长相由前端注册表决定。
        level = str(p.get("level") or "info").strip().lower()
        if not NOTIFICATION_LEVEL_RE.match(level):
            level = "info"

        attachments, att_err = self._normalize_attachments(p.get("attachments"))
        if att_err:
            return {}, att_err

        data = {
            # ⚠️ 这里**不截断**：title/message/detail 可能是 Jinja2 模板，
            #   模板原文（几百字）远超展示上限，先截断会把 `{% if %}` 切成残句
            #   → 渲染抛 "Unexpected end of template" → 降级原文 → 手机收到模板代码。
            #   截断改到 send() 里**渲染之后**做（见那里的注释）。
            "title": str(p.get("title") or "").strip() or DEFAULT_TITLE,
            "message": message,
            "detail": str(p.get("detail") or "").strip(),
            "level": level,
            "category": (str(p.get("category") or "").strip() or DEFAULT_CATEGORY)[:40],
            "targets": targets,
            "url": str(p.get("url") or "").strip(),
            "attachments": attachments,
            # 指向 RoomElvesStore 里已注册的配置；留空 = 前端只展示内容
            "from_config_id": str(p.get("from_config_id") or "").strip()[:120],
            "users": users,
        }
        return data, ""

    @staticmethod
    def _guess_attach_type(path: str) -> tuple[str, str]:
        """按扩展名猜 (type, mime)。type 只用于前端选渲染方式。"""
        ext = os.path.splitext(str(path or "").lower())[1]
        mime = _EXT_MIME.get(ext, "")
        if mime.startswith("image/"):
            return "image", mime
        if mime.startswith("video/"):
            return "video", mime
        if mime.startswith("audio/"):
            return "audio", mime
        return "file", mime or "application/octet-stream"

    def _normalize_attachments(self, raw: Any) -> tuple[list[dict], str]:
        """附件规整：最多 N 个，逐个补 type/mime。entity 留给 _resolve_attachments 换路径。"""
        if not raw:
            return [], ""
        if isinstance(raw, (str, dict)):
            raw = [raw]
        if not isinstance(raw, (list, tuple)):
            return [], "invalid_attachments"

        out: list[dict] = []
        for item in raw:
            if len(out) >= NOTIFICATION_ATTACH_MAX:
                break
            if isinstance(item, str):
                item = {"path": item}
            if not isinstance(item, dict):
                continue
            entity = str(item.get("entity") or "").strip()
            path = str(item.get("path") or item.get("url") or "").strip()
            if not entity and not path:
                continue
            entry: dict[str, Any] = {"name": str(item.get("name") or "").strip()}
            if entity:
                entry["entity"] = entity
                entry["type"] = "image"
                entry["mime"] = "image/jpeg"
            else:
                guess_type, guess_mime = self._guess_attach_type(path)
                entry["path"] = path
                kind = str(item.get("type") or "").strip().lower()
                entry["type"] = kind if kind in NOTIFICATION_ATTACH_KINDS else guess_type
                entry["mime"] = str(item.get("mime") or guess_mime)
            out.append(entry)
        return out, ""

    async def _snapshot_entity(self, entity_id: str) -> tuple[str, str]:
        """camera/image 实体 → 快照文件 → (config 相对路径, 错误)。"""
        domain = str(entity_id).split(".", 1)[0].lower()
        if domain not in ("camera", "image"):
            return "", f"{domain} 不支持快照，请改用路径"
        if not self._hass.services.has_service(domain, "snapshot"):
            return "", f"当前 HA 没有 {domain}.snapshot 服务"
        tmp_dir = self._hass.config.path(*NOTIFICATION_TMP_SUBDIR)
        try:
            os.makedirs(tmp_dir, exist_ok=True)
        except OSError as exc:
            return "", f"无法创建临时目录：{exc}"
        filename = os.path.join(tmp_dir, f"{uuid.uuid4().hex}.jpg")
        try:
            await self._hass.services.async_call(
                domain, "snapshot", {"filename": filename},
                target={"entity_id": entity_id}, blocking=True,
            )
        except Exception as exc:  # noqa: BLE001
            # 前缀「快照失败：」会一路传到前端（attachments[].error → 卡片提示），
            #   典型场景：设备没开机 / 实体 unavailable → snapshot 直接抛
            #   `Entity not available: image.xxx`，光看原文容易以为是配置问题。
            return "", f"快照失败：{str(exc)[:180]}"
        if not os.path.isfile(filename):
            return "", "snapshot 没有产出文件"
        try:
            rel = os.path.relpath(filename, self._hass.config.path())
        except ValueError:  # pragma: no cover - 跨盘符
            rel = filename
        return rel, ""

    async def _resolve_attachments(self, attachments: list[dict], ctx: dict) -> list[dict]:
        """实体 → 快照落盘；路径模板渲染；`/config/` 前缀归一化；补 size。

        附件不参与推送，只给前端渲染用。
        """
        out: list[dict] = []
        for att in attachments:
            item = dict(att)
            entity = str(item.pop("entity", "") or "")
            # 实体名也可能写模板（如 {{ ... }}）
            if entity and ("{{" in entity or "{%" in entity):
                entity = (await self._render(entity, ctx)).strip()
            if entity:
                path, err = await self._snapshot_entity(entity)
                item["path"] = path
                if err:
                    item["error"] = err
                    _LOGGER.warning(
                        "[notification] 附件实体 %s 快照失败: %s", entity, err
                    )
            else:
                raw_path = str(item.get("path") or "")
                if raw_path and ("{{" in raw_path or "{%" in raw_path):
                    raw_path = (await self._render(raw_path, ctx)).strip()
                item["path"] = self._normalize_asset_path(raw_path)
            p = str(item.get("path") or "")
            if p:
                full = p if os.path.isabs(p) else self._hass.config.path(p)
                try:
                    item["size"] = int(os.path.getsize(full))
                except OSError:
                    item["size"] = 0
            out.append(item)
        return out

    @staticmethod
    def _click_url(url: str) -> str:
        """给「点击通知打开」的页面拼上 nc_open=1（外链不拼，前端读不到参数）。

        ⚠️ 没配「打开页面」时**也要给一个带参数的地址**（默认首页）：
        否则 iOS / Android 点通知只是把 App 拉起来、url 为空 → 前端读不到
        `nc_open=1` → 抽屉不会弹（表现为"点通知进来没反应"）。
        """
        u = str(url or "").strip()
        if not u:
            # 空 url：打开 HA 默认首页并带上参数（前端据此展开通知抽屉）
            return "/?nc_open=1"
        if u.startswith(("http://", "https://")):
            return u
        if "nc_open=" in u:
            return u
        sep = "&" if "?" in u else "?"
        return f"{u}{sep}nc_open=1"

    @staticmethod
    def _normalize_asset_path(path: str) -> str:
        """把 HA 生态里常见的 `/config/xxx` 写法归一成 config 相对路径。"""
        p = str(path or "").strip()
        if not p:
            return ""
        # 模板渲染结果可能带换行/制表：只取首行非空内容，避免路径里混进空白
        if "\n" in p or "\r" in p:
            p = next((ln.strip() for ln in p.splitlines() if ln.strip()), "")
        if p.startswith("/config/"):
            p = p[len("/config/"):]
        elif p == "/config":
            p = ""
        return p.strip()

    async def _render(self, text: str, ctx: dict) -> str:
        """含 `{{ }}` / `{% %}` 才渲染；失败降级原文（并记 warning）。

        排查用：**成功渲染时会记一条 info**（`模板已渲染: 原文片段 → 结果片段`），
        所以「没看到这条日志」= 根本没进渲染分支 —— 多半是模板里用的不是半角
        `{{ }}`（中文输入法容易打出全角 `｛｛ ｝｝`），或者写成了 `{ }`。
        """
        raw = str(text or "")
        if not raw:
            return raw
        if "{{" not in raw and "{%" not in raw:
            # 不是模板：原样返回（纯文本零开销）。这里**故意不记日志**，否则每条都刷屏
            return raw
        # 缓存键用 md5，**不能再用 raw[:4000]** ——
        #   那是把模板原文切了 4000 字去编译，超过 4000 的模板会缺 `{% endif %}`
        #   直接渲染失败（和 v6.2.6 修的"先截断后渲染"是同一类事故）。
        key = hashlib.md5(raw.encode("utf-8")).hexdigest()
        try:
            from homeassistant.helpers.template import Template

            tpl = self._tpl_cache.get(key)
            if tpl is None:
                tpl = Template(raw, hass=self._hass)
                if len(self._tpl_cache) >= 128:
                    self._tpl_cache.clear()
                self._tpl_cache[key] = tpl
            res = tpl.async_render(ctx, parse_result=False)
            if inspect.isawaitable(res):
                res = await res
            out = str(res if res is not None else "").strip()
            if out != raw:
                _LOGGER.info("[notification] 模板已渲染: %.60s → %.60s", raw, out)
            return out
        except Exception as exc:  # noqa: BLE001
            # ⚠️ 别把"坏的 Template"留在缓存里：否则同一条模板以后每次都失败
            self._tpl_cache.pop(key, None)
            _LOGGER.warning(
                "[notification] 模板渲染失败（使用原文）: %.60s | %s: %s",
                raw, type(exc).__name__, exc,
            )
            return raw

    # ------------------------------------------------------------------ #
    #  内部：投递                                                            #
    # ------------------------------------------------------------------ #
    async def _deliver(self, data: dict) -> tuple[list[dict], str]:
        results: list[dict] = []
        for target in data["targets"]:
            domain, _, service = target.partition(".")
            entry = {"target": target, "ok": False, "error": ""}
            if domain != "notify" or not service:
                entry["error"] = "invalid_target"
            elif not self._hass.services.has_service("notify", service):
                entry["error"] = "service_not_found"
            else:
                service_data: dict[str, Any] = {
                    "title": data["title"],
                    "message": data["message"],
                }
                if data.get("url"):
                    service_data["data"] = {"url": data["url"]}
                try:
                    await self._hass.services.async_call(
                        "notify", service, service_data, blocking=True
                    )
                    entry["ok"] = True
                except Exception as exc:  # noqa: BLE001
                    entry["error"] = str(exc)[:300]
                    _LOGGER.warning("[notification] 推送到 %s 失败: %s", target, exc)
            results.append(entry)

        ok_count = sum(1 for r in results if r["ok"])
        if ok_count == len(results) and ok_count > 0:
            status = "ok"
        elif ok_count == 0:
            status = "failed"
        else:
            status = "partial"
        return results, status

    # ------------------------------------------------------------------ #
    #  内部：存储（全部在 executor 线程执行）                                 #
    # ------------------------------------------------------------------ #
    async def _store(self, record: dict) -> int | None:
        try:
            return await self._hass.async_add_executor_job(self._store_sync, record)
        except Exception as exc:  # noqa: BLE001
            # 落库失败不能让调用方失败：通知已经发出去了，前端靠事件/轮询仍可看到
            _LOGGER.warning("[notification] 落库失败（通知已投递）: %s", exc)
            return None

    def _store_sync(self, record: dict) -> int:
        conn = sqlite3.connect(self._db_path, timeout=5)
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            cur = conn.execute(
                f"INSERT INTO {TABLE_NOTIFICATIONS} ({_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0)",
                (
                    int(record["ts"]),
                    record["level"],
                    record["category"],
                    record["title"],
                    record["message"],
                    record["detail"],
                    json.dumps(record["targets"], ensure_ascii=False),
                    record["status"],
                    json.dumps(record["results"], ensure_ascii=False),
                    record["url"],
                    json.dumps(record["payload"], ensure_ascii=False),
                    record["source_type"],
                    record["source_id"],
                    record["source_name"],
                    json.dumps(record.get("attachments") or [], ensure_ascii=False),
                    record.get("from_config_id") or "",
                    json.dumps(record.get("user_ids") or [], ensure_ascii=False),
                ),
            )
            conn.commit()
            return int(cur.lastrowid or 0)
        finally:
            conn.close()

    def _list_sync(
        self, limit: int, offset: int, unread_only: bool,
        category: str, level: str, since: int, user_id: str = "",
    ) -> dict:
        limit = max(1, min(int(limit or 20), 200))
        offset = max(0, int(offset or 0))
        where: list[str] = []
        args: list[Any] = []
        if unread_only:
            where.append("read = 0")
        if category:
            where.append("category = ?")
            args.append(category)
        if level:
            where.append("level = ?")
            args.append(level)
        if since:
            where.append("ts >= ?")
            args.append(int(since))
        if user_id:
            # user_ids 是 JSON 数组：'%"<uid>"%' 精确匹配元素（不会命中前缀相同的别的 id）
            where.append("user_ids LIKE ?")
            args.append(f'%"{user_id}"%')
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""

        conn = sqlite3.connect(self._db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            total = conn.execute(
                f"SELECT COUNT(*) AS c FROM {TABLE_NOTIFICATIONS} {where_sql}", args
            ).fetchone()["c"]
            unread_where = ["read = 0"]
            unread_args: list[Any] = []
            if user_id:
                unread_where.append("user_ids LIKE ?")
                unread_args.append(f'%"{user_id}"%')
            unread = conn.execute(
                f"SELECT COUNT(*) AS c FROM {TABLE_NOTIFICATIONS} "
                f"WHERE {' AND '.join(unread_where)}",
                unread_args,
            ).fetchone()["c"]
            rows = conn.execute(
                f"SELECT * FROM {TABLE_NOTIFICATIONS} {where_sql} "
                "ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
                (*args, limit, offset),
            ).fetchall()
        finally:
            conn.close()

        items = [row_to_item(r) for r in rows]
        return {
            "success": True,
            "total": int(total or 0),
            "unread": int(unread or 0),
            "count": len(items),
            "limit": limit,
            "offset": offset,
            "items": items,
        }

    def _get_sync(self, notification_id: int) -> dict | None:
        conn = sqlite3.connect(self._db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            row = conn.execute(
                f"SELECT * FROM {TABLE_NOTIFICATIONS} WHERE id = ?", (int(notification_id),)
            ).fetchone()
        finally:
            conn.close()
        return row_to_item(row) if row else None

    def _mark_read_sync(self, ids, all_: bool) -> int:
        now = _now_ms()
        conn = sqlite3.connect(self._db_path, timeout=5)
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            if all_:
                cur = conn.execute(
                    f"UPDATE {TABLE_NOTIFICATIONS} SET read = 1, read_at = ? WHERE read = 0",
                    (now,),
                )
            else:
                clean = [int(i) for i in (ids or []) if str(i).strip().isdigit()]
                if not clean:
                    return 0
                marks = ",".join("?" for _ in clean)
                cur = conn.execute(
                    f"UPDATE {TABLE_NOTIFICATIONS} SET read = 1, read_at = ? "
                    f"WHERE read = 0 AND id IN ({marks})",
                    (now, *clean),
                )
            conn.commit()
            return int(cur.rowcount or 0)
        finally:
            conn.close()

    def _mark_unread_sync(self, ids, all_: bool) -> int:
        conn = sqlite3.connect(self._db_path, timeout=5)
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            if all_:
                cur = conn.execute(
                    f"UPDATE {TABLE_NOTIFICATIONS} SET read = 0, read_at = 0 WHERE read = 1"
                )
            else:
                clean = [int(i) for i in (ids or []) if str(i).strip().isdigit()]
                if not clean:
                    return 0
                marks = ",".join("?" for _ in clean)
                cur = conn.execute(
                    f"UPDATE {TABLE_NOTIFICATIONS} SET read = 0, read_at = 0 "
                    f"WHERE read = 1 AND id IN ({marks})",
                    tuple(clean),
                )
            conn.commit()
            return int(cur.rowcount or 0)
        finally:
            conn.close()

    def _delete_rows_sync(
        self, conn: sqlite3.Connection, where: str, params: tuple
    ) -> tuple[int, list[str]]:
        """按条件删行，返回（删行数, 可安全删除的附件路径）。

        ⚠️ 只返回「删完之后**没有其它行还在引用**」的路径 ——
           同一张图片可能被重发的通知引用多次，只顾删行会删掉仍在用的文件。
        """
        victims: set[str] = set()
        for row in conn.execute(
            f"SELECT attachments FROM {TABLE_NOTIFICATIONS} WHERE {where}", params
        ):
            victims.update(_attachment_paths(row[0]))
        keep: set[str] = set()
        if victims:
            for row in conn.execute(
                f"SELECT attachments FROM {TABLE_NOTIFICATIONS} WHERE NOT ({where})", params
            ):
                keep.update(_attachment_paths(row[0]))
        cur = conn.execute(f"DELETE FROM {TABLE_NOTIFICATIONS} WHERE {where}", params)
        return int(cur.rowcount or 0), sorted(victims - keep)

    def _purge_attachment_files(self, rel_paths: list[str]) -> int:
        """删掉 config 目录 `_notify_tmp` 里的附件文件，返回删除个数。

        两道防线：① 前面已按 `_notify_tmp` 前缀筛过；② 这里再用绝对路径校验一次
        （防 `../` 穿越 / 符号链接把删除范围带到临时目录之外）。
        """
        if not rel_paths:
            return 0
        # ⚠️ HA 的 `hass.config.path` 是**方法**（`path(*parts)`），不是属性；
        #   直接拿它当字符串会抛 `TypeError: expected str, bytes or os.PathLike object, not method`。
        #   真正的目录是 `hass.config.config_dir`。
        cfg = getattr(self._hass, "config", None)
        base = getattr(cfg, "config_dir", None)
        if not isinstance(base, str) or not base:
            # 兜底：极老版本没有 config_dir 时，调一次 path()
            try:
                maybe = cfg.path() if callable(getattr(cfg, "path", None)) else None
            except Exception:  # noqa: BLE001
                maybe = None
            base = maybe if isinstance(maybe, str) and maybe else None
        if not base:
            return 0
        root = os.path.abspath(os.path.join(base, *NOTIFICATION_TMP_SUBDIR))
        removed = 0
        for rel in rel_paths:
            try:
                full = os.path.abspath(os.path.join(base, rel))
            except (TypeError, ValueError):
                continue
            if not full.startswith(root + os.sep):
                continue
            try:
                if os.path.isfile(full):
                    os.remove(full)
                    removed += 1
            except OSError as exc:  # noqa: BLE001
                _LOGGER.debug("[notification] 删除附件失败 %s: %s", rel, exc)
        return removed

    def _remove_sync(self, ids, mode: str | None) -> int:
        conn = sqlite3.connect(self._db_path, timeout=5)
        files: list[str] = []
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            if mode == "all":
                where, params = "1=1", ()
            elif mode == "read":
                where, params = "read = 1", ()
            else:
                clean = [int(i) for i in (ids or []) if str(i).strip().isdigit()]
                if not clean:
                    return 0
                marks = ",".join("?" for _ in clean)
                where, params = f"id IN ({marks})", tuple(clean)
            removed, files = self._delete_rows_sync(conn, where, params)
            conn.commit()
        finally:
            conn.close()
        # 提交之后再删文件：即使删文件失败，数据也已经是干净的
        if files:
            self._purge_attachment_files(files)
        return removed

    def _cleanup_sync(self) -> int:
        cutoff = _now_ms() - int(NOTIFICATION_RETENTION_DAYS) * 86400 * 1000
        conn = sqlite3.connect(self._db_path, timeout=5)
        files: list[str] = []
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            # 只按时间（90 天）保留 —— 条数闸门已取消（NOTIFICATION_RETENTION_MAX 停用）
            removed, files = self._delete_rows_sync(conn, "ts < ?", (cutoff,))
            conn.commit()
        finally:
            conn.close()
        if files:
            purged = self._purge_attachment_files(files)
            if purged:
                _LOGGER.debug("[notification] 清理附件文件 %s 个", purged)
        return removed

    # ------------------------------------------------------------------ #
    #  内部：传感器刷新（1 秒合并）                                           #
    # ------------------------------------------------------------------ #
    def _schedule_refresh(self) -> None:
        if self._refresh_pending:
            return
        self._refresh_pending = True
        try:
            from homeassistant.helpers.event import async_call_later

            async_call_later(self._hass, 1.0, self._do_refresh)
        except Exception:  # pragma: no cover
            self._refresh_pending = False

    async def _do_refresh(self, _now=None) -> None:
        self._refresh_pending = False
        await self.refresh_sensor()

    async def refresh_sensor(self) -> None:
        sensor = self._hass.data.get(DOMAIN, {}).get("notification_sensor")
        if sensor is None:
            return
        try:
            await sensor.async_refresh_now()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("[notification] 传感器刷新失败")


def build_sensor_stats(db_path: str, limit: int = NOTIFICATION_TODAY_LATEST_MAX) -> dict:
    """供 sensor.py 在 executor 中调用：**今日**未读 / 今日汇总 / 今日最近 N 条。

    口径（v6.2 起，前端「今日 / 昨天 / 指定日期」的根基）：
      · `unread`       = **今日**未读 → 卡片拉手直接用这个
      · `unread_total` = 全部未读（历史里还有多少没看）
      · `total`        = 今日条数
      · `total_all`    = 全部条数（历史总量，走 API 才用得到）
      · `today`        = 今日条数（与 total 相同，保留老字段名）
      · `by_*`         = 全部按**今日**统计（与列表内容一致，避免"显示 5 打开只有 2"）
      · `latest`       = 今日的，最多 `limit` 条（软上限，防刷屏那天属性膨胀）
      · `history_from` = 历史数据起点（最早一条的时间，毫秒；无数据为 0）

    与 NotificationManager 分离（纯函数、无 hass 依赖），避免 sensor 与 manager 互相 import。
    """
    empty = {
        "unread": 0, "unread_total": 0, "total": 0, "total_all": 0, "today": 0,
        "by_level": {}, "by_category": {}, "by_user": {}, "latest": [],
        "history_from": 0,
    }
    if not db_path:
        return empty
    try:
        conn = sqlite3.connect(db_path, timeout=5)
    except Exception as exc:  # noqa: BLE001
        _LOGGER.warning("[notification] 打开数据库失败: %s", exc)
        return empty
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        t0 = _today_start_ms()
        # 今日口径（state / 列表 / by_* 全走这个）
        total = conn.execute(
            f"SELECT COUNT(*) AS c FROM {TABLE_NOTIFICATIONS} WHERE ts >= ?", (t0,)
        ).fetchone()["c"]
        unread = conn.execute(
            f"SELECT COUNT(*) AS c FROM {TABLE_NOTIFICATIONS} WHERE ts >= ? AND read = 0",
            (t0,),
        ).fetchone()["c"]
        # 全量口径（只给 unread_total / total_all / history_from 用）
        total_all = conn.execute(
            f"SELECT COUNT(*) AS c FROM {TABLE_NOTIFICATIONS}"
        ).fetchone()["c"]
        unread_total = conn.execute(
            f"SELECT COUNT(*) AS c FROM {TABLE_NOTIFICATIONS} WHERE read = 0"
        ).fetchone()["c"]
        history_from = int(
            conn.execute(
                f"SELECT MIN(ts) AS t FROM {TABLE_NOTIFICATIONS}"
            ).fetchone()["t"] or 0
        )
        by_level = {
            r["level"]: r["c"]
            for r in conn.execute(
                f"SELECT level, COUNT(*) AS c FROM {TABLE_NOTIFICATIONS} "
                "WHERE ts >= ? GROUP BY level",
                (t0,),
            ).fetchall()
        }
        by_category = {
            r["category"]: r["c"]
            for r in conn.execute(
                f"SELECT category, COUNT(*) AS c FROM {TABLE_NOTIFICATIONS} "
                "WHERE ts >= ? AND read = 0 GROUP BY category",
                (t0,),
            ).fetchall()
        }
        # 按用户统计（卡片「只看我的」直接用这个，不必再扫 mobile_app）
        by_user: dict[str, dict] = {}
        try:
            for r in conn.execute(
                f"SELECT user_ids, read FROM {TABLE_NOTIFICATIONS} WHERE ts >= ?",
                (t0,),
            ).fetchall():
                for uid in _loads(r["user_ids"], []):
                    rec = by_user.setdefault(uid, {"unread": 0, "total": 0})
                    rec["total"] += 1
                    if not r["read"]:
                        rec["unread"] += 1
        except Exception:  # noqa: BLE001
            # 列还没补（迁移未跑）→ 空表；前端会退化成按 latest 计数
            by_user = {}
        rows = conn.execute(
            f"SELECT * FROM {TABLE_NOTIFICATIONS} WHERE ts >= ? "
            "ORDER BY ts DESC, id DESC LIMIT ?",
            (t0, max(1, int(limit))),
        ).fetchall()
    except Exception as exc:  # noqa: BLE001
        # 表还没建（升级后首次启动）/ 库异常：返回空，不让传感器报错
        _LOGGER.warning("[notification] 传感器统计失败: %s", exc)
        return empty
    finally:
        conn.close()

    latest = [row_to_item(r, brief=True) for r in rows]
    return {
        "unread": int(unread or 0),
        "unread_total": int(unread_total or 0),
        "total": int(total or 0),
        "total_all": int(total_all or 0),
        "today": int(total or 0),
        "by_level": by_level,
        "by_category": by_category,
        "by_user": by_user,
        "latest": latest,
        "history_from": history_from,
    }


def _local_dt(ts_ms: int) -> datetime:
    """毫秒时间戳 → 本地时区的 datetime（与 _today_start_ms 同一口径）。"""
    return datetime.fromtimestamp(float(ts_ms) / 1000.0, _tz())


def build_date_index(db_path: str, month: str = "") -> dict:
    """按「年月 / 日」列出**哪些日期有数据**（前端日期选择器用）。

    返回格式与 project 里同类接口（`room_data_dates` / `device_data_dates`）保持一致：
      · 不传 month → `{"months": ["2026-11", "2026-10"], "count": 2}`
      · month=2026-11 → `{"month": "2026-11", "dates": ["2026-11-17", "2026-11-16"], "count": 2}`

    `dates` 是**日期字符串数组**（升序，与 `room_data_dates` 的 `sorted(all_dates)` 一致），
    不是对象数组 —— 前端拿它直接画日历网格最省事。

    归组在 Python 侧按**本地时区**做，不用 SQLite 的 `localtime`（那个跟服务器时区走，
    容器是 UTC 时会把 23:30 的通知算到第二天）。

    90 天 × 每天 100 条 ≈ 9000 行，全表扫一遍几十毫秒，不需要索引。
    """
    want = str(month or "").strip()
    if want:
        try:
            first = datetime.strptime(want, "%Y-%m").replace(tzinfo=_tz())
        except ValueError:
            return {"ok": False, "error": "month 需形如 2026-11"}
        # 下月 1 号 00:00（先跳到 28 号再加 4 天，避免月底天数差异）
        nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
        lo, hi = int(first.timestamp() * 1000), int(nxt.timestamp() * 1000)
    else:
        lo = hi = 0

    if not db_path:
        return {"ok": False, "error": "数据库未就绪"}

    try:
        conn = sqlite3.connect(db_path, timeout=5)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"打开数据库失败: {exc}"}
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        if want:
            rows = conn.execute(
                f"SELECT ts FROM {TABLE_NOTIFICATIONS} WHERE ts >= ? AND ts < ?",
                (lo, hi),
            ).fetchall()
        else:
            rows = conn.execute(f"SELECT ts FROM {TABLE_NOTIFICATIONS}").fetchall()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"查询失败: {exc}"}
    finally:
        conn.close()

    if want:
        dates = sorted({
            _local_dt(int(row["ts"])).strftime("%Y-%m-%d") for row in rows
        })
        return {"ok": True, "month": want, "dates": dates, "count": len(dates)}

    months = sorted({
        _local_dt(int(row["ts"])).strftime("%Y-%m") for row in rows
    })
    return {"ok": True, "months": months, "count": len(months)}


__all__ = ["NotificationManager", "build_sensor_stats", "build_date_index"]
