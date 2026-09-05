# =========================================================================== #
# action_log_inbox.py — 用户操作记录"收件箱"（先落盘、后落库）
# ---------------------------------------------------------------------------
# 背景：
#   room-elves-card 前端把操作记录 POST /api/ha_data_store/action_log 上报（含
#   sendBeacon 无响应投递）。原实现是收到后立刻直写 SQLite：若此刻正被其它采集
#   任务短暂锁库（SQLite 单写者），写入抛错 → 返回失败；前端在 sendBeacon 场景
#   没有响应可重试、普通场景也可能在重试前关闭页面 → 形成"部分操作没有上报"。
#
# 方案（收到即不丢）：
#   1. POST 到达后先追加写入本地 JSON 收件箱文件（几乎不会失败），立刻返回成功；
#   2. 后台任务按小批次把收件箱数据迁入 user_actions 表（带 busy_timeout + 锁冲突
#      重试），迁移成功才从收件箱删除；
#   3. 任一次迁移失败都保留在收件箱中，下次继续重试，即使 HA 中途重启也不丢；
#   4. 收件箱同时承担 device_history 关联与常用设备 sensor 刷新（原 ActionLogView
#      在请求路径内做的事，迁到后台批量做）。
#
# 收件箱文件：与数据库同目录 action_log_inbox.json（即 {config_dir}/storage/）
# =========================================================================== #

import asyncio
import json
import logging
import os
import sqlite3
import time

from homeassistant.core import HomeAssistant

from .const import DEFAULT_TIMEZONE, DOMAIN, TABLE_USER_ACTIONS
from .http_api import _format_ts_ms, _link_device_history_to_actions

_LOGGER = logging.getLogger(__name__)

_INBOX_FILE = "action_log_inbox.json"
# 后台搬运间隔（秒）
_DRAIN_INTERVAL = 3
# 单次搬运条数上限（防止一次 SQL 事务过大）
_BATCH_MAX = 300
# 锁冲突（database is locked）时重试次数与等待间隔（秒）
_LOCK_RETRY_SLEEPS = (0.3, 0.6, 1.2, 2.4, 4.0)
# 数据库连续写失败后的冷却时间（秒），期间不再反复冲击数据库
_FAIL_COOLDOWN = 15
# 收件箱内存/文件最大条数上限（防 SQLite 长期不可用时无限膨胀；超出丢弃最旧并告警）
_MAX_ITEMS = 20000


class ActionLogInbox:
    """操作记录收件箱：JSON 先落盘 → 后台迁入 SQLite。"""

    def __init__(self, hass: HomeAssistant, db_path: str) -> None:
        self._hass = hass
        self._db_path = db_path
        db_dir = os.path.dirname(db_path) or db_path
        self._file = os.path.join(db_dir, _INBOX_FILE)
        self._lock = asyncio.Lock()      # 保护 _items 与文件写
        self._draining = False
        self._unsub = None               # async_track_time_interval 定时器的注销回调
        self._items: list = []
        self._cooldown_until = 0.0       # 写失败后的冷却截止时间（time.monotonic）
        # 统计（供日志/调试）
        self._stats = {"queued": 0, "drained": 0, "failed": 0}

    # ------------------------------------------------------------------ #
    #  对外：待搬运条数 / 文件路径 / 统计                                   #
    # ------------------------------------------------------------------ #
    @property
    def pending_count(self) -> int:
        return len(self._items)

    @property
    def file_path(self) -> str:
        return self._file

    @property
    def stats(self) -> dict:
        return dict(self._stats)

    # ------------------------------------------------------------------ #
    #  对外：启动后台搬运（async_setup_entry 调用）                        #
    # ------------------------------------------------------------------ #
    async def start(self) -> None:
        if self._unsub is not None:
            return
        # 启动时先加载历史遗留文件（executor 中读取，避免阻塞事件循环）
        try:
            await self._hass.async_add_executor_job(self._load_sync)
        except Exception:  # pragma: no cover
            pass
        # 用"定时器回调"驱动后台搬运，而非常驻无限 asyncio 任务。
        # 关键：常驻任务会被 HA 跟踪，bootstrap 会等它导致"Setup timed out"、
        # 关停/重启也会被它卡住（此前报错即由此引起）。
        from datetime import timedelta

        from homeassistant.helpers.event import async_call_later, async_track_time_interval

        self._unsub = async_track_time_interval(
            self._hass, self._tick, timedelta(seconds=_DRAIN_INTERVAL)
        )
        # 启动 2 秒后先补迁一次历史遗留（一次性定时回调，不产生常驻任务）
        async_call_later(self._hass, 2, self._tick)

    # ------------------------------------------------------------------ #
    #  对外：停止后台搬运（async_unload_entry 调用）                       #
    # ------------------------------------------------------------------ #
    async def stop(self) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
        # 卸载前尽量把收件箱迁完（失败仍保留在 JSON 文件，下次启动自动补迁）
        try:
            await self.drain()
        except Exception:
            _LOGGER.exception("[action_log][inbox] 卸载前清空收件箱异常")

    # ------------------------------------------------------------------ #
    #  定时器回调：尝试搬运一次（内部已处理并发 / 冷却 / 失败保留）         #
    # ------------------------------------------------------------------ #
    async def _tick(self, _now=None) -> None:
        try:
            await self.drain()
        except Exception as exc:  # pragma: no cover - 防御
            _LOGGER.warning("[action_log][inbox] 后台搬运异常: %s", exc)

    # ------------------------------------------------------------------ #
    #  对外：收下前端上报的记录（先落 JSON，立刻可返回成功）                 #
    # ------------------------------------------------------------------ #
    async def enqueue(self, items) -> int:
        valid = [it for it in items if isinstance(it, dict)]
        if not valid:
            return 0
        async with self._lock:
            self._items.extend(valid)
            self._stats["queued"] += len(valid)
            if len(self._items) > _MAX_ITEMS:
                dropped = len(self._items) - _MAX_ITEMS
                self._items = self._items[-_MAX_ITEMS:]
                _LOGGER.warning(
                    "[action_log][inbox] 收件箱超上限(%d)，丢弃最旧 %d 条（SQLite 可能长时间不可用）",
                    _MAX_ITEMS, dropped,
                )
            await self._hass.async_add_executor_job(self._persist_sync)
        # 有数据尽快尝试搬运一次
        if not self._draining:
            self._hass.async_create_task(self.drain())
        return len(valid)

    # ------------------------------------------------------------------ #
    #  把收件箱记录批量迁入 SQLite；失败保留待下次重试                      #
    # ------------------------------------------------------------------ #
    async def drain(self) -> None:
        if self._draining or not self._items:
            return
        if time.monotonic() < self._cooldown_until:
            return  # 数据库写失败冷却中：等后台循环到时再试，避免反复冲击
        self._draining = True
        succeeded = True
        try:
            while self._items:
                async with self._lock:
                    if not self._items:
                        break
                    batch = self._items[:_BATCH_MAX]
                try:
                    inserted, matched = await self._hass.async_add_executor_job(
                        self._insert_batch_sync, batch
                    )
                except Exception as exc:
                    # SQLite 锁冲突/写失败：保留在收件箱，进入冷却等下次重试
                    succeeded = False
                    _LOGGER.warning(
                        "[action_log][inbox] SQLite 写入失败，%d 条保留收件箱待重试: %s",
                        len(batch), exc,
                    )
                    self._stats["failed"] += len(batch)
                    self._cooldown_until = time.monotonic() + _FAIL_COOLDOWN
                    break
                # 迁移成功：从收件箱移除该批并落盘。
                # 即使个别非法条目被跳过（inserted < len(batch)）也一并消费，避免卡队列。
                async with self._lock:
                    if self._items[:len(batch)] == batch:
                        self._items = self._items[len(batch):]
                    else:
                        # 期间有并发 enqueue：仅移除本批等量最旧元素
                        if len(self._items) >= len(batch):
                            self._items = self._items[len(batch):]
                        else:
                            self._items = []
                    await self._hass.async_add_executor_job(self._persist_sync)
                self._stats["drained"] += inserted
                if inserted > 0:
                    _LOGGER.info(
                        "[action_log][inbox] 迁入 SQLite %d 条（剩余 %d 条待迁）",
                        inserted, len(self._items),
                    )
                # 关联 device_history（on_user/off_user/快照回填）
                if matched:
                    try:
                        await self._hass.async_add_executor_job(
                            _link_device_history_to_actions, self._db_path, matched
                        )
                    except Exception as exc:
                        _LOGGER.warning(
                            "[action_log][inbox] 关联 device_history 用户失败: %s", exc
                        )
                # 刷新常用设备统计 sensor
                if inserted > 0:
                    await self._refresh_user_actions_sensor()
                # 继续处理剩余批次（每次成功一批后由 while 自判是否还有待迁记录）
        finally:
            self._draining = False
            # 本次全部迁完 → 清除冷却
            if succeeded:
                self._cooldown_until = 0.0

    # ------------------------------------------------------------------ #
    #  私有：加载 / 持久化收件箱文件                                        #
    # ------------------------------------------------------------------ #
    def _load_sync(self) -> None:
        if not os.path.exists(self._file):
            return
        try:
            with open(self._file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                self._items = data
                if self._items:
                    _LOGGER.info(
                        "[action_log][inbox] 加载遗留待迁记录 %d 条 file=%s",
                        len(self._items), self._file,
                    )
            return
        except Exception as exc:
            # 文件损坏：改名备份，避免数据与启动失败
            _LOGGER.error(
                "[action_log][inbox] 收件箱文件解析失败，已改名 .bak 备份: %s", exc
            )
            try:
                os.replace(self._file, self._file + ".bak")
            except Exception:
                pass

    def _persist_sync(self) -> None:
        try:
            tmp = self._file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._items, f, ensure_ascii=False)
            os.replace(tmp, self._file)
        except Exception as exc:
            _LOGGER.error("[action_log][inbox] 收件箱文件写入失败: %s", exc)

    # ------------------------------------------------------------------ #
    #  私有：单批迁入 SQLite（busy_timeout + 锁冲突重试）                   #
    # ------------------------------------------------------------------ #
    def _insert_batch_sync(self, batch: list) -> tuple:
        """返回 (inserted, matched_items)；最终仍失败时抛异常由调用方保留待重试。"""
        last_exc: Exception | None = None
        for attempt in range(len(_LOCK_RETRY_SLEEPS) + 1):
            try:
                return self._insert_once(batch)
            except sqlite3.OperationalError as exc:
                last_exc = exc
                if attempt < len(_LOCK_RETRY_SLEEPS):
                    wait = _LOCK_RETRY_SLEEPS[attempt]
                    _LOGGER.warning(
                        "[action_log][inbox] 数据库忙（%s），第 %d 次重试前等待 %.1fs",
                        exc, attempt + 1, wait,
                    )
                    time.sleep(wait)
                else:
                    break
            except Exception as exc:  # 其它异常不再重试
                raise exc
        raise last_exc if last_exc is not None else RuntimeError("inbox insert failed")

    def _insert_once(self, batch: list) -> tuple:
        conn = sqlite3.connect(self._db_path, timeout=20.0)
        try:
            conn.execute("PRAGMA busy_timeout=20000")
            now = _local_iso_now()
            inserted = 0
            matched_items: list = []
            for item in batch:
                if not isinstance(item, dict):
                    continue
                eid = (item.get("entity_id") or "").strip()
                action = (item.get("action") or "").strip()
                try:
                    ts = int(item.get("ts") or 0)
                except (TypeError, ValueError):
                    ts = 0
                ts_text = _format_ts_ms(ts, DEFAULT_TIMEZONE)
                if not action:
                    continue
                # 幂等键：与 http_api 直写路径一致，op_id=前端 _id，靠唯一索引去重。
                # 收件箱/直写/前端重发任何一次落到库里都只产生一行。
                op_id = (item.get("_id") or item.get("op_id") or "").strip()
                user_name = (item.get("user_name") or item.get("user") or "").strip()
                snap = (item.get("action_snapshot") or "")
                config_id = ""
                for ck in ("config_id", "device_config_id"):
                    cv = item.get(ck)
                    if isinstance(cv, str) and cv.strip():
                        config_id = cv.strip()
                        break
                if not config_id and isinstance(snap, str) and snap:
                    try:
                        snap_obj = json.loads(snap)
                        if isinstance(snap_obj, dict) and isinstance(
                            snap_obj.get("config_id"), str
                        ):
                            config_id = snap_obj["config_id"].strip()
                    except Exception:
                        pass
                device_type = (
                    (item.get("device_type") or "").strip()
                    if isinstance(item.get("device_type"), str)
                    else ""
                )
                cur = conn.execute(
                    f"INSERT OR IGNORE INTO {TABLE_USER_ACTIONS} "
                    f"(op_id, user_name, entity_id, action, name, icon, room_name, source, service, "
                    f"card_type, other, state_log, ts, ts_text, action_snapshot, config_id, device_type, created_at) "
                    f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        op_id,
                        user_name,
                        eid,
                        action,
                        (item.get("name") or "").strip(),
                        (item.get("icon") or "").strip()
                        if isinstance(item.get("icon"), str)
                        else "",
                        (item.get("room_name") or "").strip(),
                        (item.get("source") or "").strip(),
                        (item.get("service") or "").strip()
                        if isinstance(item.get("service"), str)
                        else "",
                        (item.get("card_type") or "").strip()
                        if isinstance(item.get("card_type"), str)
                        else "",
                        (item.get("other") or "").strip()
                        if isinstance(item.get("other"), str)
                        else "",
                        (item.get("state_log") or "").strip()
                        if isinstance(item.get("state_log"), str)
                        else "",
                        ts,
                        ts_text,
                        snap,
                        config_id,
                        device_type,
                        now,
                    ),
                )
                if cur.rowcount <= 0:
                    # 该 op_id 已入库（幂等命中）→ 跳过，避免重复计数/重复关联
                    continue
                inserted += 1
                matched_items.append(
                    {
                        "entity_id": eid,
                        "ts_text": ts_text,
                        "user_name": user_name,
                        "action_snapshot": snap,
                    }
                )
            conn.commit()
            return (inserted, matched_items)
        finally:
            conn.close()

    # ------------------------------------------------------------------ #
    #  私有：刷新 user_actions_sensor（常用设备近30天聚合）                 #
    # ------------------------------------------------------------------ #
    async def _refresh_user_actions_sensor(self) -> None:
        try:
            sensor = self._hass.data.get(DOMAIN, {}).get("user_actions_sensor")
            if sensor is not None and hasattr(sensor, "_async_refresh"):
                await sensor._async_refresh()
        except Exception as exc:
            _LOGGER.warning("[action_log][inbox] 刷新常用设备 sensor 失败: %s", exc)


def _local_iso_now() -> str:
    """本地时间 ISO 字符串（与 http_api._get_local_iso 同逻辑，减少跨模块调用）。"""
    from datetime import datetime, timedelta

    return (datetime.utcnow() + timedelta(hours=DEFAULT_TIMEZONE)).isoformat()
