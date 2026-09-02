"""ha_data_store 简单自动化执行引擎。

支持四大功能：
1. 定时执行：每天固定时间 + 可选星期白名单
2. 间隔执行：固定秒/分钟间隔
3. 条件执行：多条件（all/any 组合）基于实体状态比较
4. 执行记录落库：automation_logs 表记录时间、触发、条件明细、动作结果、耗时

调度策略：统一 tick（async_track_time_interval，默认 30 秒），
每次从数据库重读启用配置（增删改立即生效，无需 reload 机制）。
防重入：正在执行的自动化在下一个 tick 撞上时跳过。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    AUTOMATION_LOG_CLEANUP_EVERY,
    AUTOMATION_LOG_RETENTION_DAYS,
    AUTOMATION_TICK_SECONDS,
    DEFAULT_TIMEZONE,
    DOMAIN,
    TABLE_AUTOMATION_LOGS,
    TABLE_AUTOMATIONS,
)
from .logger import get_logger

_LOGGER = logging.getLogger(__name__)

# 条件运算符（前端选项列表需与此保持一致）
CONDITION_OPERATORS = ("==", "!=", ">=", "<=", ">", "<", "contains")

# 星期名称（datetime.weekday(): 0=周一 ... 6=周日）
WEEKDAY_NAMES = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


class AutomationManager:
    """自动化执行管理器：统一 tick 调度 + 条件求值 + 动作执行 + 记录落库。"""

    def __init__(self, hass: HomeAssistant, db_path: str) -> None:
        self.hass = hass
        self.db_path = db_path
        self._tick_unsub = None
        self._running: set[int] = set()  # 防重入：正在执行的自动化 id
        self._tick_count = 0
        self._timezone = DEFAULT_TIMEZONE

    # ─────────── 生命周期 ───────────
    def start(self) -> None:
        """启动调度器：注册 tick。"""
        if self._tick_unsub:
            return
        self._tick_unsub = async_track_time_interval(
            self.hass, self._tick, timedelta(seconds=AUTOMATION_TICK_SECONDS)
        )
        _LOGGER.info("[automation] 简单自动化调度器已启动（tick=%ss）", AUTOMATION_TICK_SECONDS)
        local_logger = get_logger()
        if local_logger:
            local_logger.info("[automation] 调度器已启动（tick=%ss）", AUTOMATION_TICK_SECONDS)

    def stop(self) -> None:
        """停止调度器（集成卸载时调用）。"""
        if self._tick_unsub:
            self._tick_unsub()
            self._tick_unsub = None
        _LOGGER.info("[automation] 简单自动化调度器已停止")
        local_logger = get_logger()
        if local_logger:
            local_logger.info("[automation] 调度器已停止")

    # ─────────── 时间工具 ───────────
    def now_str(self) -> str:
        """当前本地时间字符串（YYYY-MM-DD HH:MM:SS），与 DEFAULT_TIMEZONE 偏移一致。"""
        return (datetime.utcnow() + timedelta(hours=self._timezone)).strftime("%Y-%m-%d %H:%M:%S")

    # ─────────── 调度 tick ───────────
    async def _tick(self, _now) -> None:
        """每个 tick：顺带清理过期记录 + 检查所有启用自动化是否到期。"""
        self._tick_count += 1
        if self._tick_count % AUTOMATION_LOG_CLEANUP_EVERY == 0:
            await self.hass.async_add_executor_job(self._cleanup_logs)

        autos = await self.hass.async_add_executor_job(self._load_enabled)
        now_str = self.now_str()
        for auto in autos:
            aid = int(auto["id"])
            if aid in self._running:
                continue
            next_run = auto.get("next_run") or ""
            if not next_run:
                # 新建/刚启用还没有 next_run：只计算不执行
                next_run = self.compute_next_run(auto, now_str)
                await self.hass.async_add_executor_job(self._set_next_run, aid, next_run)
                continue
            if now_str >= next_run:
                self.hass.async_create_task(
                    self._run_automation(auto, force=False, trigger="schedule")
                )

    # ─────────── 数据库读取 ───────────
    def _load_enabled(self) -> list[dict]:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(
                f"SELECT * FROM {TABLE_AUTOMATIONS} WHERE enabled = 1"
            ).fetchall()]
        finally:
            conn.close()

    def _load_by_id(self, automation_id: int) -> dict | None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                f"SELECT * FROM {TABLE_AUTOMATIONS} WHERE id = ?", (automation_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def _set_next_run(self, automation_id: int, next_run: str) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                f"UPDATE {TABLE_AUTOMATIONS} SET next_run = ? WHERE id = ?",
                (next_run, automation_id),
            )
            conn.commit()
        finally:
            conn.close()

    # ─────────── 手动触发（API 调用） ───────────
    async def run_by_id(self, automation_id: int, force: bool = False) -> dict:
        """手动触发指定自动化；force=True 跳过条件直接执行。"""
        auto = await self.hass.async_add_executor_job(self._load_by_id, automation_id)
        if auto is None:
            local_logger = get_logger()
            if local_logger:
                local_logger.warning(
                    "[automation] 手动触发失败 automation_id=%s：自动化不存在", automation_id
                )
            return {"success": False, "error": "自动化不存在"}
        return await self._run_automation(auto, force=force, trigger="manual")

    async def recompute_next_run(self, automation_id: int) -> str | None:
        """重新计算并写入 next_run（新增/修改/启用后由 API 调用）。"""
        auto = await self.hass.async_add_executor_job(self._load_by_id, automation_id)
        if auto is None or not auto.get("enabled"):
            return None
        next_run = self.compute_next_run(auto, self.now_str())
        await self.hass.async_add_executor_job(self._set_next_run, automation_id, next_run)
        return next_run

    # ─────────── 核心执行流程 ───────────
    async def _run_automation(
        self, auto: dict, force: bool = False, trigger: str = "schedule"
    ) -> dict:
        """执行单个自动化：条件求值 → 动作执行 → 写记录 → 更新 next_run。"""
        aid = int(auto["id"])
        if aid in self._running:
            local_logger = get_logger()
            if local_logger:
                local_logger.info(
                    "[automation] 防重入跳过 automation_id=%s name=%s（正在执行中）",
                    aid, auto.get("name") or "",
                )
            return {"success": False, "error": "该自动化正在执行中，已跳过本次触发"}
        self._running.add(aid)
        start_ms = time.monotonic()
        try:
            name = auto.get("name") or ""
            ttype = auto.get("trigger_type") or "time"
            # 1. 条件求值
            conds = self._parse_json(auto.get("conditions"), [])
            logic = auto.get("logic") or "all"
            if force or not conds:
                cond_ok, cond_detail = True, []
            else:
                cond_ok, cond_detail = await self._evaluate_conditions(conds, logic)
            # 2. 动作执行
            actions = self._parse_json(auto.get("actions"), [])
            actions_result: list[dict] = []
            if cond_ok:
                actions_result = await self._execute_actions(
                    actions, bool(auto.get("stop_on_error", 0))
                )
                ok_list = [r.get("ok") for r in actions_result]
                if all(ok_list):
                    status = "success"
                elif any(ok_list):
                    status = "partial_failed"
                else:
                    status = "failed"
            else:
                status = "skipped"
            duration_ms = int((time.monotonic() - start_ms) * 1000)
            trigger_time = self.now_str()
            trigger_desc = self.describe_trigger(auto)
            # 3. 写执行记录
            await self.hass.async_add_executor_job(
                self._insert_log,
                aid, name, ttype, trigger_desc, trigger_time,
                1 if cond_ok else 0,
                json.dumps(cond_detail, ensure_ascii=False, default=str),
                status,
                json.dumps(actions_result, ensure_ascii=False, default=str),
                duration_ms, trigger_time,
            )
            # 4. 更新 last_run / last_result / next_run
            next_run = self.compute_next_run(auto, trigger_time)
            await self.hass.async_add_executor_job(
                self._update_after_run, aid, trigger_time, status, next_run,
            )
            # 5. automation_logs 已写入新记录 → 通知自动化状态传感器立即刷新
            self._notify_status_sensor()
            _LOGGER.info(
                "[automation] %s 触发成功 trigger=%s status=%s 动作数=%d 耗时=%dms",
                name, trigger, status, len(actions_result), duration_ms,
            )
            local_logger = get_logger()
            if local_logger:
                local_logger.info(
                    "[automation] 执行完成 automation_id=%s name=%s trigger=%s status=%s "
                    "动作数=%d 耗时=%dms",
                    aid, name, trigger, status, len(actions_result), duration_ms,
                )
            return {
                "success": True, "status": status, "triggered": cond_ok,
                "duration_ms": duration_ms, "actions": actions_result,
            }
        except Exception as exc:  # 兜底：任何异常都不让调度器崩溃
            _LOGGER.error("[automation] 执行异常 automation_id=%s: %s", aid, exc)
            local_logger = get_logger()
            if local_logger:
                local_logger.error("[automation] 执行异常 automation_id=%s: %s", aid, exc)
            return {"success": False, "error": str(exc)}
        finally:
            self._running.discard(aid)

    # ─────────── 条件引擎 ───────────
    async def _evaluate_conditions(self, conds: list, logic: str) -> tuple[bool, list]:
        """多条件求值。logic: all（全满足）| any（任一满足）。返回 (是否通过, 逐条明细)。"""
        results: list[dict] = []
        for cond in conds:
            entity_id = str(cond.get("entity_id") or "").strip()
            op = str(cond.get("operator") or "==")
            expected = cond.get("value")
            detail: dict[str, Any] = {
                "entity_id": entity_id, "operator": op,
                "value": expected, "matched": False,
            }
            state = self.hass.states.get(entity_id)
            if state is None or state.state in ("unavailable", "unknown"):
                detail["actual"] = state.state if state else "missing"
                results.append(detail)
                continue
            actual = state.state
            detail["actual"] = actual
            detail["matched"] = self._compare(actual, op, expected)
            results.append(detail)
        if not results:
            return True, []
        ok = all(r["matched"] for r in results) if logic == "all" else any(r["matched"] for r in results)
        return ok, results

    @staticmethod
    def _compare(actual: Any, op: str, expected: Any) -> bool:
        """单个条件比较：数值优先，转换失败降级字符串比较。"""
        try:
            a_num, b_num = float(actual), float(expected)
        except (TypeError, ValueError):
            a_num = b_num = None
        if a_num is not None:
            if op == ">":
                return a_num > b_num
            if op == ">=":
                return a_num >= b_num
            if op == "<":
                return a_num < b_num
            if op == "<=":
                return a_num <= b_num
            if op == "==":
                return a_num == b_num
            if op == "!=":
                return a_num != b_num
        a_str, b_str = str(actual), str(expected)
        if op == "contains":
            return b_str in a_str
        if op == "==":
            return a_str == b_str
        if op == "!=":
            return a_str != b_str
        if op == ">":
            return a_str > b_str
        if op == ">=":
            return a_str >= b_str
        if op == "<":
            return a_str < b_str
        if op == "<=":
            return a_str <= b_str
        return False

    # ─────────── 动作执行 ───────────
    async def _execute_actions(self, actions: list, stop_on_error: bool) -> list[dict]:
        """顺序执行服务调用动作，逐条记录结果；stop_on_error 时失败即停。"""
        results: list[dict] = []
        for idx, act in enumerate(actions):
            service = str(act.get("service") or "").strip()
            entity_id = str(act.get("entity_id") or "").strip()
            data = act.get("data")
            data = data if isinstance(data, dict) else {}
            result: dict[str, Any] = {
                "index": idx, "service": service, "entity_id": entity_id,
                "data": data, "ok": False, "error": "",
            }
            if not service or "." not in service:
                result["error"] = "service 格式应为 domain.service"
                results.append(result)
                if stop_on_error:
                    break
                continue
            domain, srv = service.split(".", 1)
            target = {"entity_id": entity_id} if entity_id else None
            try:
                await self.hass.services.async_call(
                    domain, srv, data, target=target, blocking=True,
                )
                result["ok"] = True
            except Exception as exc:
                result["error"] = str(exc)
            results.append(result)
            if not result["ok"] and stop_on_error:
                break
        return results

    # ─────────── next_run 计算 ───────────
    def compute_next_run(self, auto: dict, now_str: str) -> str:
        """计算下次触发时间（本地字符串 YYYY-MM-DD HH:MM:SS）。
        定时型：找下一个满足 time + 星期白名单的分钟点（今天已过则顺延）。
        间隔型：now + interval_seconds（重启后过期不补跑，直接顺延）。
        """
        ttype = auto.get("trigger_type") or "time"
        cfg = self._parse_json(auto.get("trigger_config"), {})
        base = datetime.strptime(now_str, "%Y-%m-%d %H:%M:%S")
        if ttype == "interval":
            try:
                seconds = max(int(cfg.get("interval_seconds") or 60), 10)
            except (TypeError, ValueError):
                seconds = 60
            return (base + timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S")
        # 定时型
        try:
            hh, mm = [int(x) for x in str(cfg.get("time") or "00:00").split(":")[:2]]
        except (TypeError, ValueError):
            hh, mm = 0, 0
        days = cfg.get("days") or []
        if not isinstance(days, list):
            days = []
        target = base.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target <= base:
            target += timedelta(days=1)
        if days:
            for _ in range(8):  # 最多跳 7 天必然命中
                if target.weekday() in days:
                    break
                target += timedelta(days=1)
        return target.strftime("%Y-%m-%d %H:%M:%S")

    def describe_trigger(self, auto: dict) -> str:
        """生成人类可读的触发描述（写入执行记录，前端展示）。"""
        ttype = auto.get("trigger_type") or "time"
        cfg = self._parse_json(auto.get("trigger_config"), {})
        if ttype == "interval":
            try:
                secs = max(int(cfg.get("interval_seconds") or 60), 10)
            except (TypeError, ValueError):
                secs = 60
            if secs % 3600 == 0:
                return f"每 {secs // 3600} 小时"
            if secs % 60 == 0:
                return f"每 {secs // 60} 分钟"
            return f"每 {secs} 秒"
        time_str = str(cfg.get("time") or "00:00")
        days = cfg.get("days") or []
        if not isinstance(days, list) or not days:
            return f"每天 {time_str}"
        names = "、".join(WEEKDAY_NAMES[d] for d in sorted(days) if 0 <= d <= 6)
        return f"{names} {time_str}"

    # ─────────── 落库 ───────────
    def _insert_log(
        self, automation_id, name, ttype, trigger_desc, trigger_time,
        cond_ok, cond_json, status, actions_json, duration_ms, created_at,
    ) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                f"INSERT INTO {TABLE_AUTOMATION_LOGS} "
                f"(automation_id, automation_name, trigger_type, trigger_desc, trigger_time, "
                f"condition_result, conditions_checked, status, actions_result, duration_ms, created_at) "
                f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (automation_id, name, ttype, trigger_desc, trigger_time,
                 cond_ok, cond_json, status, actions_json, duration_ms, created_at),
            )
            conn.commit()
        finally:
            conn.close()

    def _update_after_run(self, automation_id, last_run, last_result, next_run) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                f"UPDATE {TABLE_AUTOMATIONS} SET last_run = ?, last_result = ?, "
                f"next_run = ?, updated_at = ? WHERE id = ?",
                (last_run, last_result, next_run, last_run, automation_id),
            )
            conn.commit()
        finally:
            conn.close()

    def _notify_status_sensor(self) -> None:
        """automation_logs 写入新记录后，通知自动化状态传感器立即刷新。"""
        try:
            sensor = self.hass.data.get(DOMAIN, {}).get("automation_status_sensor")
            if sensor is not None:
                self.hass.async_create_task(sensor.async_trigger_refresh())
        except Exception as e:
            _LOGGER.debug("[automation] 通知自动化状态传感器刷新失败: %s", e)

    def _cleanup_logs(self) -> int:
        """清理超过保留天数的执行记录，返回删除行数。"""
        cutoff = (
            datetime.utcnow() + timedelta(hours=self._timezone, days=-AUTOMATION_LOG_RETENTION_DAYS)
        ).strftime("%Y-%m-%d %H:%M:%S")
        conn = sqlite3.connect(self.db_path)
        try:
            cur = conn.execute(
                f"DELETE FROM {TABLE_AUTOMATION_LOGS} WHERE created_at < ?", (cutoff,)
            )
            conn.commit()
            if cur.rowcount:
                _LOGGER.info("[automation] 已清理过期执行记录 %d 条", cur.rowcount)
                local_logger = get_logger()
                if local_logger:
                    local_logger.info("[automation] 已清理过期执行记录 %d 条", cur.rowcount)
            return cur.rowcount
        finally:
            conn.close()

    # ─────────── 工具 ───────────
    @staticmethod
    def _parse_json(raw, default):
        """安全解析 JSON 字段：已是 list/dict 直接返回，否则尝试 json.loads。"""
        if isinstance(raw, (list, dict)):
            return raw
        if not raw:
            return default
        try:
            return json.loads(raw)
        except Exception:
            return default
