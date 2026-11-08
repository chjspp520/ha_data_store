"""控制来源登记队列（内存，旁路）。

用途
----
`device_history` 记录的是「设备会话」（on_time → off_time），写入发生在状态变化监听里；
而"这次变化是自动化/定时器干的"这个事实只有引擎侧知道。引擎在 `hass.services.async_call`
之前把身份丢进本模块，写库时再按 `entity_id + 时间` 取回，从而给 device_history 盖上来
源戳（`on_source_*` / `off_source_*`）。

设计约束（务必遵守）
--------------------
1. **纯内存**：不建表、不落盘、不起定时器；重启丢几条只影响来源标注，不影响任务执行。
2. **旁路**：本模块任何异常都必须被吞掉并返回安全的空值 —— 绝不能让写库或引擎执行失败。
3. **有界**：容量上限 + 30 秒过期，防止长时间运行的实例内存增长。
4. **一次性消费**：一条登记只能匹配一次，避免同一登记被重复盖章。

时间语义
--------
`device_history.on_time / off_time` 是本地时间字符串（`YYYY-MM-DD HH:MM:SS`），
而 push 时用的是 `time.time()` 秒级浮点。`consume()` 会按本地时区把字符串解析回时间戳再比较，
容差默认 ±2 秒（与 `_link_device_history_to_actions` 的容差一致）。
"""

from __future__ import annotations

import threading
import time
from datetime import datetime

# 队列容量：一次只会有极少数引擎在"执行中"，500 已经非常宽松
CAPACITY = 500

# 登记有效期（秒）：超过这个时间的登记不会被任何写入匹配
TTL_SECONDS = 30.0

# 匹配容差（秒）：与 http_api._LINK_TOLERANCE_SECONDS 保持一致
TOLERANCE_SECONDS = 2.0

_lock = threading.Lock()

# [{ "entity_id": str, "ts": float, "type": str, "id": str, "name": str }]
_items: list[dict] = []


def _prune_locked(now: float) -> None:
    """丢弃过期条目（调用方必须已持锁）。"""
    if not _items:
        return
    keep = [it for it in _items if (now - it.get("ts", 0.0)) <= TTL_SECONDS]
    if len(keep) != len(_items):
        _items[:] = keep
    # 超容量时丢最旧的
    overflow = len(_items) - CAPACITY
    if overflow > 0:
        del _items[:overflow]


def _to_ts(value) -> float:
    """把 'YYYY-MM-DD HH:MM:SS' / ISO / 毫秒时间戳 统一成秒级时间戳；失败返回 0。"""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        v = float(value)
        # 毫秒（> 1e11）自动降级成秒
        return v / 1000.0 if v > 1e11 else v
    text = str(value).strip()
    if not text:
        return 0.0
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(text[:19] if "T" in fmt or len(text) >= 19 else text, fmt).timestamp()
        except Exception:
            continue
    try:
        return datetime.fromisoformat(text).timestamp()
    except Exception:
        return 0.0


def push(entity_id: str, source_type: str, source_id, source_name: str, ts=None) -> bool:
    """登记一次"即将发生的控制"。

    @param entity_id   被控实体
    @param source_type 'automation' / 'timer'（'card' 走 user_actions 回填，无需登记）
    @param source_id   对应表主键（automations.id / timer_id）
    @param source_name 展示名快照（自动化名 / 「倒计时 关灯」）
    @param ts          发生时刻（默认 now）
    @return 是否登记成功（异常时 False，调用方无需关心）
    """
    try:
        eid = str(entity_id or "").strip()
        if not eid:
            return False
        when = _to_ts(ts) or time.time()
        item = {
            "entity_id": eid,
            "ts": when,
            "type": str(source_type or ""),
            "id": "" if source_id is None else str(source_id),
            "name": str(source_name or ""),
        }
        with _lock:
            _prune_locked(time.time())
            _items.append(item)
        return True
    except Exception:
        return False


def push_many(entity_ids, source_type: str, source_id, source_name: str, ts=None) -> int:
    """一次登记多个实体（一条自动化控制多台设备时用）。返回成功条数。"""
    count = 0
    try:
        for eid in (entity_ids or []):
            if push(eid, source_type, source_id, source_name, ts):
                count += 1
    except Exception:
        pass
    return count


def consume(entity_id: str, when, tolerance: float = TOLERANCE_SECONDS) -> dict | None:
    """取回并删除一条最匹配的登记。

    @param entity_id 被控实体
    @param when      写入 device_history 的时间（on_time / off_time 字符串，或时间戳）
    @param tolerance 允许的时间差（秒）
    @return {"type","id","name"} 或 None（无匹配 / 异常）
    """
    try:
        eid = str(entity_id or "").strip()
        if not eid:
            return None
        target = _to_ts(when)
        if not target:
            return None
        now = time.time()
        best_idx = -1
        best_diff = None
        with _lock:
            _prune_locked(now)
            for idx, it in enumerate(_items):
                if it.get("entity_id") != eid:
                    continue
                diff = abs(float(it.get("ts", 0.0)) - target)
                if diff > tolerance:
                    continue
                if best_diff is None or diff < best_diff:
                    best_diff = diff
                    best_idx = idx
            if best_idx < 0:
                return None
            it = _items.pop(best_idx)
        return {"type": it.get("type", ""), "id": it.get("id", ""), "name": it.get("name", "")}
    except Exception:
        return None


def pending_count() -> int:
    """当前待匹配条数（仅用于日志/调试，正常应长期接近 0）。"""
    try:
        with _lock:
            _prune_locked(time.time())
            return len(_items)
    except Exception:
        return 0


def clear() -> None:
    """清空队列（测试 / 卸载时用）。"""
    try:
        with _lock:
            _items.clear()
    except Exception:
        pass
