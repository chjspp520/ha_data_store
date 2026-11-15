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

import asyncio
import inspect
import json
import logging
import os
import sqlite3
import time
import uuid
from datetime import datetime, time as dt_time, timedelta, timezone
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.template import Template

# 控制来源登记（纯内存旁路；让 device_history 能标出"这次是自动化干的"）
from .source_registry import push_many as _push_source_many, peek as _peek_source

from .const import (
    AUTOMATION_ENGINE_VERSION,
    AUTOMATION_LOG_CLEANUP_EVERY,
    AUTOMATION_LOG_RETENTION_DAYS,
    AUTOMATION_TICK_SECONDS,
    SOURCE_TYPE_AUTOMATION,
    DEFAULT_TIMEZONE,
    DOMAIN,
    TABLE_AUTOMATION_LOGS,
    TABLE_AUTOMATIONS,
)
from .logger import get_logger

_LOGGER = logging.getLogger(__name__)

# 条件运算符（前端选项列表需与此保持一致）
#   changed       —— 该实体（或属性）自本规则**上次执行**以来变过（不需要比较值）
#   not_contains  —— 字符串不包含
CONDITION_OPERATORS = (
    "==", "!=", ">=", "<=", ">", "<", "contains", "not_contains", "changed",
)

# ── 赋值动作（v6.16.0）────────────────────────────────────────────────
#   {"assign": {"to": 目标实体, "from": 源实体}}           把实体的值赋给目标实体
#   {"assign": {"to": 目标实体, "template": "{{ ... }}"}}  把 jinja2 渲染结果赋给目标实体
#   {"assign": {"to": 目标实体, "value": "固定内容"}}      直接赋一个固定值（不取实体、不渲染）
#
# 只有**值**会经过 jinja2：服务名 / 实体 ID / 动作序列都不渲染。
# HA 没有通用的 set_state 服务，所以「写值」必须按目标实体的域选服务与参数名；
# 表外的域（light / switch / sensor …）没有"把某个值写进去"的语义，不开放赋值。
ASSIGN_TARGET_SERVICES: dict[str, tuple[str, str]] = {
    "input_number": ("set_value", "value"),
    "number": ("set_value", "value"),
    "input_text": ("set_value", "value"),
    "text": ("set_value", "value"),
    "input_select": ("select_option", "option"),
    "select": ("select_option", "option"),
    # input_datetime 的参数名按实体 has_date / has_time 决定，见 _build_assign_call
    "input_datetime": ("set_datetime", ""),
    "datetime": ("set_value", "value"),
    "date": ("set_value", "value"),
    "time": ("set_value", "value"),
    # 布尔域没有 set_value：按值判定 turn_on / turn_off
    "input_boolean": ("", ""),
}

# 这些值视为"没有有效值"：跳过该条动作，绝不把空串/未知写进目标实体
ASSIGN_EMPTY_VALUES = frozenset({
    "", "none", "null", "unknown", "unavailable", "n/a", "na", "-",
})

# 布尔目标的真值判定（其余一律按关处理，仍不明确的直接报错）
ASSIGN_TRUE_VALUES = frozenset({"on", "true", "1", "yes", "open"})
ASSIGN_FALSE_VALUES = frozenset({"off", "false", "0", "no", "closed", "close"})

# ── 微信 ClawBot 动作（v7.0.0.14）──────────────────────────────────────
#   {"clawbot": {"kind": "text", "target": "@self", "message": "…"}}
#   {"clawbot": {"kind": "image", "entity": "camera.doorbell"}}     实体 → 取一张图
#   {"clawbot": {"kind": "image", "path": "{{ … }}"}}               路径（模板渲染）
#
# 只负责"把东西发出去"：路径一律原样交给 ClawBot（它自己校验 config 目录/扩展名/大小）。
# 实体来源只支持**图片**（camera / image 快照），视频不录像、只收路径。
#
# 域名**不写死**：该集成改过名（目录 ha_clawbot → wx_clawbot，只有 WS 命令/事件名
# 还沿用 ha_clawbot）。写死会在改名后静默失效，报错只剩一句
# "Action ha-clawbot.send_media not found"，从表面看很像路径/图片问题。
# 运行时以 HA 服务注册表为准（谁有 send_media 就用谁），候选表只决定探测顺序。
CLAWBOT_DOMAIN_CANDIDATES = ("wx_clawbot", "ha_clawbot", "ha-clawbot")
CLAWBOT_MEDIA_KINDS = ("image", "video", "voice", "file")
CLAWBOT_ENTITY_DOMAINS = ("camera", "image")
CLAWBOT_TMP_SUBDIR = ("www", "chj", "_clawbot_tmp")

# 星期名称（datetime.weekday(): 0=周一 ... 6=周日）
WEEKDAY_NAMES = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

# ── 状态触发（v6.14.76）的两个时间参数 ──
# 同一规则两次触发的最小间隔：状态抖动和「A 触发 B、B 又触发 A」都靠它兜住。
STATE_TRIGGER_COOLDOWN = 3
# 启动静默期：集成刚起来时实体可能还在陆续恢复，这期间的状态变化不触发规则
#   —— 否则一重启就是"全屋执行一遍"。
STATE_TRIGGER_WARMUP = 30


class AutomationManager:
    """自动化执行管理器：统一 tick 调度 + 条件求值 + 动作执行 + 记录落库。"""

    def __init__(self, hass: HomeAssistant, db_path: str) -> None:
        self.hass = hass
        self.db_path = db_path
        self._tick_unsub = None
        self._running: set[int] = set()  # 防重入：正在执行的自动化 id
        self._tick_count = 0
        self._timezone = DEFAULT_TIMEZONE
        # ── 状态触发（v6.14.76）──
        self._state_unsub = None                            # 状态监听的解绑函数
        self._state_autos: dict[str, list[dict]] = {}       # entity_id → 订阅它的规则
        self._state_last: dict[int, float] = {}             # 规则 id → 上次触发（monotonic）
        self._state_ready_at = 0.0                          # 静默期截止（monotonic）
        # ── 赋值动作（v6.16.0）──
        #   模板编译比渲染贵得多，按模板原文缓存 Template 实例；超过上限整表清空。
        self._tpl_cache: dict[str, Template] = {}
        # ── 微信 ClawBot ──
        #   探测到的服务域名（空串 = 还没探测过）；缓存失效靠每次 has_service 复核。
        self._clawbot_domain_cache = ""

    # ─────────── 生命周期 ───────────
    def start(self) -> None:
        """启动调度器：注册 tick + 状态监听。"""
        if self._tick_unsub:
            return
        self._tick_unsub = async_track_time_interval(
            self.hass, self._tick, timedelta(seconds=AUTOMATION_TICK_SECONDS)
        )
        # 【v6.14.76】静默期：从这里起 STATE_TRIGGER_WARMUP 秒内的状态变化不触发规则。
        #   集成启动时实体可能还在陆续恢复，不设这个就是"一重启全屋执行一遍"。
        self._state_ready_at = time.monotonic() + STATE_TRIGGER_WARMUP
        # 状态监听要读库拿规则，而 start() 是同步方法，只能丢到任务里做
        self.hass.async_create_task(self.reload_state_watch())
        _LOGGER.info(
            "[automation] 简单自动化调度器已启动（tick=%ss, engine=%s）",
            AUTOMATION_TICK_SECONDS, AUTOMATION_ENGINE_VERSION,
        )
        local_logger = get_logger()
        if local_logger:
            local_logger.info("[automation] 调度器已启动（tick=%ss）", AUTOMATION_TICK_SECONDS)

    def stop(self) -> None:
        """停止调度器（集成卸载时调用）。"""
        if self._tick_unsub:
            self._tick_unsub()
            self._tick_unsub = None
        # 【v6.14.76】状态监听也要解绑，否则重载集成会积累重复订阅
        if self._state_unsub:
            try:
                self._state_unsub()
            except Exception:
                pass
            self._state_unsub = None
        self._state_autos = {}
        _LOGGER.info("[automation] 简单自动化调度器已停止")
        local_logger = get_logger()
        if local_logger:
            local_logger.info("[automation] 调度器已停止")

    # ─────────── 状态触发（v6.14.76） ───────────
    async def reload_state_watch(self) -> None:
        """按当前规则重建状态监听。

        调用时机：集成启动、规则的增 / 删 / 改之后。

        **必须整体重建而不是增量添加**：删掉的规则若还留着订阅，
        下次状态变化时它会照常执行 —— 而用户以为已经删了。
        """
        if self._state_unsub:
            try:
                self._state_unsub()
            except Exception:
                pass
            self._state_unsub = None

        self._state_autos = {}
        try:
            rows = await self.hass.async_add_executor_job(self._load_enabled)
        except Exception as exc:
            _LOGGER.warning("[automation] 读取规则失败，状态监听未注册: %s", exc)
            return

        for auto in rows:
            if (auto.get("trigger_type") or "") != "state":
                continue
            cfg = self._parse_json(auto.get("trigger_config"), {})
            eid = str(cfg.get("entity_id") or "").strip()
            if not eid:
                continue
            self._state_autos.setdefault(eid, []).append(auto)

        if not self._state_autos:
            _LOGGER.debug("[automation] 没有状态触发型规则，跳过订阅")
            return

        try:
            self._state_unsub = async_track_state_change_event(
                self.hass, list(self._state_autos.keys()), self._on_state_change
            )
            _LOGGER.info(
                "[automation] 状态监听已注册：%d 个实体 / %d 条规则",
                len(self._state_autos),
                sum(len(v) for v in self._state_autos.values()),
            )
        except Exception as exc:
            _LOGGER.error("[automation] 状态监听注册失败: %s", exc)

    @staticmethod
    def _trigger_match(cfg: dict, old_state, new_state) -> bool:
        """这次状态变化是否满足规则的 from / to。

        from / to **都为空 = 任意变化**（v7.0.0.11 起明确语义，前端可选「任意变化」）。
        from 为空 = 不限制来源；to 为空 = 只限制目标值。
        同时支持比较属性（cfg.attribute），与条件里的属性能力保持一致。
        """
        attr = str(cfg.get("attribute") or "").strip()
        if attr:
            old_val = old_state.attributes.get(attr)
            new_val = new_state.attributes.get(attr)
        else:
            old_val = old_state.state
            new_val = new_state.state

        want_from = str(cfg.get("from") or "").strip()
        want_to = str(cfg.get("to") or "").strip()
        if want_from and str(old_val) != want_from:
            return False
        if want_to and str(new_val) != want_to:
            return False
        if not want_from and not want_to:
            # 「任意变化」：必须**值真的变了**。
            #   HA 的 state_changed 事件在"只有属性变化"时也会发（state 不变），
            #   温度/电量这类高频实体会把规则刷爆，所以这里挡掉纯属性刷新。
            return str(old_val) != str(new_val)
        return True

    async def _on_state_change(self, event) -> None:
        """状态变化回调：匹配规则 → 触发执行。"""
        if not self._state_autos:
            return
        data = event.data or {}
        eid = data.get("entity_id")
        new_state = data.get("new_state")
        old_state = data.get("old_state")
        # new_state 为空 = 实体被删除；old_state 为空 = 首次上报（不是"变化"）
        if not eid or new_state is None or old_state is None:
            return
        autos = self._state_autos.get(eid)
        if not autos:
            return
        # 启动静默期
        if time.monotonic() < self._state_ready_at:
            return
        # 自触发：这次变化是自动化自己刚造成的 → 跳过。
        #   否则「灯变 on → 关灯」会无限循环。用 peek 而不是 consume，
        #   否则会把本该给 device_history 的来源记录吃掉。
        try:
            if _peek_source(eid) is not None:
                _LOGGER.debug("[automation] 跳过自触发 entity=%s", eid)
                return
        except Exception:
            pass

        now_m = time.monotonic()
        for auto in autos:
            cfg = self._parse_json(auto.get("trigger_config"), {})
            if not self._trigger_match(cfg, old_state, new_state):
                continue
            aid = int(auto["id"])
            # 冷却：同一规则 N 秒内不重复触发。
            #   【v6.14.77】改为**可配**（`trigger_config.cooldown`），默认沿用 STATE_TRIGGER_COOLDOWN。
            #   用 try/except 而不是 `or 默认值` —— 因为 0 是合法值（= 关闭冷却），
            #   `0 or 3` 会把它变成 3。
            try:
                cool = float(cfg.get("cooldown"))
            except (TypeError, ValueError):
                cool = float(STATE_TRIGGER_COOLDOWN)
            if cool < 0:
                cool = 0.0
            if now_m - self._state_last.get(aid, 0.0) < cool:
                _LOGGER.debug("[automation] 冷却中（%.1fs），跳过 rule=%s", cool, aid)
                continue
            self._state_last[aid] = now_m

            # for：持续 N 秒后仍是该值才执行
            try:
                hold = int(cfg.get("for") or 0)
            except (TypeError, ValueError):
                hold = 0
            if hold > 0:
                self.hass.async_create_task(
                    self._run_after_hold(auto, eid, str(new_state.state), hold)
                )
            else:
                self.hass.async_create_task(
                    self._run_automation(auto, force=False, trigger="state")
                )

    async def _run_after_hold(
        self, auto: dict, entity_id: str, expected: str, hold: int
    ) -> None:
        """等 hold 秒后，实体仍处于 expected 才执行。

        用 asyncio.sleep 而不是注册定时器：结束时会再核对一次当前状态，
        这期间实体又变了就直接放弃 —— 宁可漏一次，也不要在一个
        已经不成立的条件上执行动作。
        """
        try:
            await asyncio.sleep(hold)
        except asyncio.CancelledError:
            return
        st = self.hass.states.get(entity_id)
        if st is None or str(st.state) != expected:
            return
        await self._run_automation(auto, force=False, trigger="state")

    # ─────────── 时间工具 ───────────
    def now_str(self) -> str:
        """当前本地时间字符串（YYYY-MM-DD HH:MM:SS），与 DEFAULT_TIMEZONE 偏移一致。"""
        return (datetime.utcnow() + timedelta(hours=self._timezone)).strftime("%Y-%m-%d %H:%M:%S")

    def _local_str_to_ts(self, s: str) -> float:
        """now_str 口径的本地时间字符串 → Unix 时间戳；空/解析失败返回 0。"""
        text = str(s or "").strip()
        if not text:
            return 0.0
        try:
            dt = datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return 0.0
        return dt.timestamp() - self._timezone * 3600

    # ─────────── 调度 tick ───────────
    async def _tick(self, _now) -> None:
        """每个 tick：顺带清理过期记录 + 检查所有启用自动化是否到期。"""
        self._tick_count += 1
        if self._tick_count % AUTOMATION_LOG_CLEANUP_EVERY == 0:
            await self.hass.async_add_executor_job(self._cleanup_logs)

        autos = await self.hass.async_add_executor_job(self._load_enabled)
        now_str = self.now_str()
        for auto in autos:
            # 【v6.14.76】状态触发型不参与 tick 调度 —— 它们由事件监听驱动，
            #   没有 next_run 概念，混进这里会被反复重算（每 30 秒一次）。
            if (auto.get("trigger_type") or "") == "state":
                continue
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
    async def run_by_id(
        self, automation_id: int, force: bool = False, dry_run: bool = False
    ) -> dict:
        """手动触发指定自动化；force=True 跳过条件；dry_run=True 只算条件不执行动作。"""
        auto = await self.hass.async_add_executor_job(self._load_by_id, automation_id)
        if auto is None:
            local_logger = get_logger()
            if local_logger:
                local_logger.warning(
                    "[automation] 手动触发失败 automation_id=%s：自动化不存在", automation_id
                )
            return {"success": False, "error": "自动化不存在"}
        return await self._run_automation(
            auto, force=force, trigger="dry_run" if dry_run else "manual", dry_run=dry_run
        )

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
        self, auto: dict, force: bool = False, trigger: str = "schedule",
        dry_run: bool = False,
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
            # 【v7.0.0.11】changed 运算符的参照点：本规则上次执行时间
            last_run_ts = self._local_str_to_ts(str(auto.get("last_run") or ""))
            if force or not conds:
                cond_ok, cond_detail = True, []
            else:
                cond_ok, cond_detail = await self._evaluate_conditions(
                    conds, logic, self._parse_json(auto.get("condition_tree"), None),
                    last_run_ts,
                )
            # 2. 动作执行
            actions = self._parse_json(auto.get("actions"), [])
            actions_result: list[dict] = []
            # 【v6.14.92】试运行（dry_run）：**绝不执行动作**，状态用独立的
            #   "dry_run"，不污染 success/failed 统计。
            # 【v6.16.0】但要求值：赋值/模板动作必须能回答"到时候会写进去什么"，
            #   所以走同一个执行器（dry=True：只求值、不调服务、不睡延迟）。
            if dry_run:
                status = "dry_run" if cond_ok else "skipped"
                if cond_ok:
                    actions_result = await self._execute_actions(
                        actions, False, auto, dry=True
                    )
            elif cond_ok:
                actions_result = await self._execute_actions(
                    actions, bool(auto.get("stop_on_error", 0)), auto
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
            #   【v6.14.92】试运行**不更新** —— 否则卡片会显示"刚刚执行过"，
            #   而实际上一个动作都没跑，统计也会虚高。
            #   【v6.14.81】`or ""` 双保险：别把 NULL 写进 NOT NULL 列
            if not dry_run:
                next_run = self.compute_next_run(auto, trigger_time) or ""
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
            # 【v6.14.92】试运行的核心价值 = 把**每个条件的判定明细**带回去给前端展示。
            # 【v6.16.0】动作从 actions_result 生成：赋值动作能显示"会写入什么值"，
            #   这才是模板/赋值唯一的验证手段。
            would_run = []
            if dry_run and cond_ok:
                for r in actions_result:
                    if not isinstance(r, dict):
                        continue
                    if r.get("assign"):
                        if r.get("skipped"):
                            would_run.append(
                                "（跳过" + str(r.get("entity_id") or "") + "："
                                + str(r.get("error") or "源值不可用") + "）"
                            )
                        elif r.get("error"):
                            would_run.append(
                                "✗ " + str(r.get("entity_id") or "") + "：" + str(r["error"])
                            )
                        else:
                            would_run.append(
                                "把「" + str(r.get("resolved") or "") + "」赋给 "
                                + str(r.get("entity_id") or "")
                            )
                    elif r.get("clawbot"):
                        if r.get("skipped"):
                            would_run.append(
                                "（跳过微信：" + str(r.get("error") or "内容为空") + "）"
                            )
                        elif r.get("error"):
                            would_run.append("✗ 微信发送：" + str(r["error"]))
                        else:
                            would_run.append(
                                "发微信[" + str(r.get("kind") or "") + "] → "
                                + str(r.get("target") or "") + "："
                                + str(r.get("resolved") or "")
                            )
                    elif str(r.get("service") or "").strip():
                        would_run.append(str(r["service"]) + (
                            " " + str(r.get("entity_id") or "") if r.get("entity_id") else ""
                        ))
            return {
                "success": True, "status": status, "triggered": cond_ok,
                "duration_ms": duration_ms, "actions": actions_result,
                "conditions": cond_detail,
                "would_run": would_run,
                "dry_run": dry_run,
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
    @staticmethod
    def _eval_one_cond(
        cond: dict, hass, now_str: str, compare, last_run_ts: float = 0.0,
    ) -> dict:
        """求单个叶子条件（实体 / 时间窗口），返回明细 dict（含 matched）。

        【v6.14.93】从 `_evaluate_conditions` 的循环体抽出来 —— 分组模式的递归
        和原来的平铺模式共用同一个叶子实现，避免两套判定逻辑各改各的。

        last_run_ts：本规则上次执行的 Unix 时间戳（0 = 没有记录），
        `changed` 运算符拿它当参照点。
        """
        ctype = str(cond.get("type") or "entity").strip()
        if ctype == "time":
            now_local = datetime.strptime(now_str, "%Y-%m-%d %H:%M:%S").time()
            try:
                f_h, f_m = [int(x) for x in str(cond.get("from") or "0:00").split(":")[:2]]
                t_h, t_m = [int(x) for x in str(cond.get("to") or "23:59").split(":")[:2]]
            except (TypeError, ValueError):
                f_h, f_m, t_h, t_m = 0, 0, 23, 59
            f_t, t_t = dt_time(f_h, f_m), dt_time(t_h, t_m)
            if f_t <= t_t:
                matched = f_t <= now_local <= t_t
            else:
                # 跨零点：now >= from 或 now <= to
                matched = now_local >= f_t or now_local <= t_t
            return {
                "type": "time",
                "from": f"{f_h:02d}:{f_m:02d}",
                "to": f"{t_h:02d}:{t_m:02d}",
                "actual": now_local.strftime("%H:%M:%S"),
                "matched": matched,
            }
        entity_id = str(cond.get("entity_id") or "").strip()
        op = str(cond.get("operator") or "==")
        expected = cond.get("value")
        # 【v6.14.71】可选：比较实体的某个**属性**
        attribute = str(cond.get("attribute") or "").strip()
        detail: dict[str, Any] = {
            "entity_id": entity_id, "operator": op,
            "value": expected, "matched": False,
        }
        if attribute:
            detail["attribute"] = attribute
        state = hass.states.get(entity_id)
        if state is None or state.state in ("unavailable", "unknown"):
            detail["actual"] = state.state if state else "missing"
            return detail
        if attribute:
            actual = state.attributes.get(attribute)
            if actual is None:
                detail["actual"] = None
                return detail
        else:
            actual = state.state
        # ──【v7.0.0.11】「变化」：自本规则上次执行以来，该实体（或属性）变过 ──
        #   不需要比较值。没有上次执行记录 = 没有参照点 → 判不匹配（不瞎猜）。
        if op == "changed":
            if last_run_ts <= 0:
                detail["actual"] = "无上次执行记录"
                return detail
            changed_at = getattr(state, "last_changed", None)
            try:
                ts = changed_at.timestamp() if changed_at is not None else 0.0
            except (AttributeError, ValueError, OSError):
                ts = 0.0
            detail["matched"] = ts > last_run_ts
            detail["actual"] = "已变化" if detail["matched"] else "未变化"
            return detail
        detail["actual"] = actual
        detail["matched"] = compare(actual, op, expected)
        return detail

    def _eval_cond_tree(
        self, node, now_str: str, out: list, depth: int = 1, path: str = "",
        last_run_ts: float = 0.0,
    ) -> bool:
        """递归求值条件树（v6.14.93 分组模式，最多 3 层）。

        组节点: {"logic": "all"|"any", "items": [ 条件 | 子组 ]}
        叶子:   单个条件 dict

        depth 从 1 开始（根组算第 1 层）；depth > 3 直接判不匹配，
        防畸形/恶意深树导致递归过深。
        """
        if not isinstance(node, dict) or depth > 3:
            return False
        items = node.get("items")
        if not isinstance(items, list) or not items:
            return False
        logic = "any" if node.get("logic") == "any" else "all"
        vals = []
        for i, child in enumerate(items):
            child_path = (path + " > " if path else "") + f"G{depth}.{i + 1}"
            if isinstance(child, dict) and isinstance(child.get("items"), list):
                vals.append(self._eval_cond_tree(
                    child, now_str, out, depth + 1, child_path, last_run_ts,
                ))
            else:
                d = self._eval_one_cond(
                    child if isinstance(child, dict) else {},
                    self.hass, now_str, self._compare, last_run_ts,
                )
                d["path"] = child_path
                out.append(d)
                vals.append(bool(d.get("matched")))
        return all(vals) if logic == "all" else any(vals)

    async def _evaluate_conditions(
        self, conds: list, logic: str, tree=None, last_run_ts: float = 0.0
    ) -> tuple[bool, list]:
        """多条件求值。

        tree 非空（有 condition_tree 的新数据）→ 递归分组求值；
        否则走原来的平铺 all/any —— **老数据没有这个字段，行为一行不变**。

        last_run_ts 只被 `changed` 运算符使用（参照点 = 上次执行时间）。
        """
        now_str = self.now_str()
        if isinstance(tree, dict) and tree.get("items"):
            out: list[dict] = []
            ok = self._eval_cond_tree(tree, now_str, out, depth=1, last_run_ts=last_run_ts)
            return ok, out
        results = [
            self._eval_one_cond(c, self.hass, now_str, self._compare, last_run_ts)
            for c in conds
        ]
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
        # 【v6.14.71】布尔统一成小写字符串。
        #   属性值里大量是布尔（如 `occupancy` / `motion`），而 Python 的 str(True) 是 "True"、
        #   前端传的是 "true" —— 直接比会永远不相等（这是属性条件最容易踩的坑）。
        a_str = ("true" if actual else "false") if isinstance(actual, bool) else str(actual)
        b_str = ("true" if expected else "false") if isinstance(expected, bool) else str(expected)
        if op == "contains":
            return b_str in a_str
        if op == "not_contains":
            return b_str not in a_str
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
    def _target_entities(self, entity_id: str, data: dict) -> list[str]:
        """从动作的 entity_id / data.entity_id 里收集目标实体（支持逗号串与数组）。"""
        out: list[str] = []

        def _add(value) -> None:
            if isinstance(value, str):
                for part in value.split(","):
                    item = part.strip()
                    if item:
                        out.append(item)
            elif isinstance(value, (list, tuple)):
                for item in value:
                    _add(item)

        _add(entity_id)
        if isinstance(data, dict):
            _add(data.get("entity_id"))
        return out

    # ─────────── 赋值动作（v6.16.0）───────────

    def _read_entity_value(self, entity_id: str) -> tuple[str, str]:
        """读源实体的状态 → (值, 错误)。"""
        st = self.hass.states.get(entity_id)
        if st is None:
            return "", f"源实体不存在: {entity_id}"
        return str(st.state or "").strip(), ""

    async def _render_tpl(self, raw: Any) -> tuple[str, str]:
        """jinja2 渲染 → (结果字符串, 错误)。

        模板只负责**算出一个值**（服务名/实体 ID/动作序列都不渲染）。
        不含 `{{ }}` / `{% %}` 的输入自动包一层，直接写表达式也能用。
        """
        text = str(raw if raw is not None else "").strip()
        if not text:
            return "", "模板为空"
        if "{{" not in text and "{%" not in text:
            text = "{{ " + text + " }}"
        try:
            tpl = self._tpl_cache.get(text)
            if tpl is None:
                tpl = Template(text, hass=self.hass)
                if len(self._tpl_cache) >= 256:
                    self._tpl_cache.clear()
                self._tpl_cache[text] = tpl
            res = tpl.async_render(parse_result=False)
            # 不同 HA 版本 async_render 可能是普通函数，也可能是协程
            if inspect.isawaitable(res):
                res = await res
        except Exception as exc:
            return "", f"模板渲染失败: {exc}"
        return str(res if res is not None else "").strip(), ""

    async def _render_text(self, raw: Any) -> tuple[str, str]:
        """按"文本"口径取值：**含 {{ 或 {% 才渲染**，否则原样返回。

        与 `_render_tpl` 的区别：那个会把裸表达式自动包一层 `{{ }}`（适合"模板字段"）；
        而路径 / 正文里的 `www/a.jpg` 不是表达式，包一层会被 jinja 当成除法去求值。
        """
        text = "" if raw is None else str(raw).strip()
        if not text:
            return "", ""
        if "{{" not in text and "{%" not in text:
            return text, ""
        return await self._render_tpl(text)

    def _build_assign_call(self, to: str, value: str) -> tuple[str, dict, str]:
        """「目标实体 + 值」编译成一次服务调用 → (service, data, 错误)。"""
        domain = to.split(".", 1)[0].lower()
        spec = ASSIGN_TARGET_SERVICES.get(domain)
        if not spec:
            supported = "/".join(sorted(ASSIGN_TARGET_SERVICES))
            return "", {}, f"目标域 {domain} 不支持赋值（支持：{supported}）"
        srv, param = spec
        # 布尔域没有 set_value：按值判定开 / 关，判不出来就报错（不瞎猜）
        if domain == "input_boolean":
            low = str(value).strip().lower()
            if low in ASSIGN_TRUE_VALUES:
                return "input_boolean.turn_on", {}, ""
            if low in ASSIGN_FALSE_VALUES:
                return "input_boolean.turn_off", {}, ""
            return "", {}, f"布尔目标只接受 on/off，收到「{value}」"
        # input_datetime 的参数名取决于实体本身支持日期还是时间
        if domain == "input_datetime":
            st = self.hass.states.get(to)
            attrs = (getattr(st, "attributes", None) or {}) if st else {}
            has_date = bool(attrs.get("has_date", True))
            has_time = bool(attrs.get("has_time", True))
            if has_date and has_time:
                param = "datetime"
            elif has_date:
                param = "date"
            elif has_time:
                param = "time"
            else:
                return "", {}, "input_datetime 实体既无日期也无时间"
        return f"{domain}.{srv}", {param: value}, ""

    async def _run_assign(
        self, results: list[dict], idx: int, assign: dict,
        auto: dict | None, dry: bool,
    ) -> None:
        """执行一条赋值动作：求值 → 编译服务 → 调用，结果 append 到 results。"""
        to = str(assign.get("to") or "").strip()
        result: dict[str, Any] = {
            "index": idx, "service": "", "entity_id": to, "data": {},
            "ok": False, "skipped": False, "error": "", "assign": True,
        }
        results.append(result)

        if not to or "." not in to:
            result["error"] = "赋值缺少目标实体"
            return

        src = str(assign.get("from") or "").strip()
        tpl = str(assign.get("template") or "").strip()
        is_literal = False
        if src:
            result["from"] = src
            value, err = self._read_entity_value(src)
        elif tpl:
            result["template"] = tpl
            value, err = await self._render_tpl(tpl)
        elif "value" in assign:
            # 【v7.0.0.12】直接赋值：把固定文本/数字写给目标实体（不取实体、不渲染模板）
            raw_val = assign.get("value")
            if isinstance(raw_val, (dict, list)):
                value, err = "", "直接赋值只接受文本或数字"
            else:
                value = "" if raw_val is None else str(raw_val).strip()
                result["value"] = value
                is_literal = True
                err = ""
        else:
            value, err = "", "赋值缺少值来源（实体 / 模板 / 直接值）"
        if err:
            result["error"] = err
            return

        # 字面量不做"不可用值"拦截：直接赋值写 "unknown"/空串是用户明确要求的
        if not is_literal and value.strip().lower() in ASSIGN_EMPTY_VALUES:
            # 源值不可用：跳过但不算失败 —— 否则一条正常规则会被记成 failed、拉低成功率
            result["ok"] = True
            result["skipped"] = True
            result["error"] = "源值不可用，已跳过"
            return

        result["resolved"] = value
        service, data, err = self._build_assign_call(to, value)
        if err:
            result["error"] = err
            return
        result["service"] = service
        result["data"] = data

        if dry:
            result["ok"] = True
            result["dry"] = True
            return

        # 控制来源登记：让 device_history 能标出"这次是自动化写的"
        if auto is not None:
            try:
                _push_source_many(
                    [to], SOURCE_TYPE_AUTOMATION,
                    auto.get("id"), auto.get("name") or "",
                )
            except Exception:
                pass
        domain, srv = service.split(".", 1)
        try:
            await self.hass.services.async_call(
                domain, srv, data, target={"entity_id": to}, blocking=True,
            )
            result["ok"] = True
        except Exception as exc:
            result["error"] = str(exc)
        # 回读实际值：部分设备渐进生效，"期望 vs 实际"是排查的关键
        st = self.hass.states.get(to)
        if st is not None:
            result["actual"] = str(st.state or "")

    # ─────────── 微信 ClawBot 动作（v7.0.0.14）───────────

    def _clawbot_domain(self) -> tuple[str, str]:
        """探测 ClawBot 的 HA 服务域名 → (domain, 错误)。

        以**服务注册表**为准，不信任任何写死的名字：候选表只决定探测顺序，
        全落空时再按服务签名（同域有 send_message + send_media，且 send_media
        的 schema 带 media_path）兜底扫一遍，将来再改名也不会静默失效。
        """
        registry = self.hass.services
        cached = self._clawbot_domain_cache
        if cached and registry.has_service(cached, "send_media"):
            return cached, ""
        for domain in CLAWBOT_DOMAIN_CANDIDATES:
            if registry.has_service(domain, "send_media"):
                self._clawbot_domain_cache = domain
                return domain, ""
        try:
            all_services = registry.async_services() or {}
        except Exception:  # noqa: BLE001 - 老版本 HA 没有这个方法
            all_services = {}
        for domain, entries in all_services.items():
            media = entries.get("send_media")
            if media is None or "send_message" not in entries:
                continue
            # Service.schema 在新版 HA 是 vol.Schema（字段在 .schema 里），
            # 老版本可能直接给 dict —— 两种都认。
            schema_obj = getattr(media, "schema", None)
            fields = getattr(schema_obj, "schema", None)
            if fields is None and isinstance(schema_obj, dict):
                fields = schema_obj
            if isinstance(fields, dict) and "media_path" in fields:
                _LOGGER.warning(
                    "[automation] ClawBot 域名不在候选表内，按服务签名选中：%s", domain
                )
                self._clawbot_domain_cache = domain
                return domain, ""
        return "", (
            "未找到 ClawBot 集成：没有任何已注册服务提供 send_media（已探测 "
            + " / ".join(CLAWBOT_DOMAIN_CANDIDATES)
            + "）。请确认集成已安装，并在「设置 → 设备与服务」中加载完成后再试"
        )

    async def _clawbot_entity_file(self, entity_id: str, kind: str) -> tuple[str, str]:
        """实体 → 本地图片文件（camera / image 快照）→ (路径, 错误)。

        只用官方 snapshot 服务：各集成自己知道怎么出图。
        不自己读 camera_proxy —— 那个 URL 需要 HA token，写出来也只有本机能访问。
        """
        if kind != "image":
            return "", "只有图片支持从实体取，视频/语音/文件请改用路径"
        domain = entity_id.split(".", 1)[0].lower()
        if domain not in CLAWBOT_ENTITY_DOMAINS:
            return "", f"实体 {entity_id} 不是 camera / image，无法取图"
        if not self.hass.services.has_service(domain, "snapshot"):
            return "", f"当前 HA 没有 {domain}.snapshot 服务，请改用路径发送"
        tmp_dir = self.hass.config.path(*CLAWBOT_TMP_SUBDIR)
        try:
            os.makedirs(tmp_dir, exist_ok=True)
        except OSError as exc:
            return "", f"无法创建临时目录：{exc}"
        filename = os.path.join(tmp_dir, f"{uuid.uuid4().hex}.jpg")
        try:
            await self.hass.services.async_call(
                domain, "snapshot", {"filename": filename},
                target={"entity_id": entity_id}, blocking=True,
            )
        except Exception as exc:
            return "", f"{domain}.snapshot 失败：{exc}"
        if not os.path.isfile(filename):
            return "", f"{domain}.snapshot 没有产出文件"
        return filename, ""

    async def _run_clawbot(
        self, results: list[dict], idx: int, cfg: dict,
        auto: dict | None, dry: bool,
    ) -> None:
        """执行一条微信 ClawBot 发送动作（文字 / 图片 / 视频 / 语音 / 文件）。"""
        result: dict[str, Any] = {
            "index": idx, "service": "", "entity_id": "", "data": {},
            "ok": False, "skipped": False, "error": "", "clawbot": True,
        }
        results.append(result)

        kind = str(cfg.get("kind") or "text").strip().lower()
        target = str(cfg.get("target") or "").strip() or "@self"
        entry = str(cfg.get("entry") or "").strip()
        result["kind"] = kind
        result["target"] = target

        # 域名先解析：缺集成时立刻给出可读错误，而不是把 ServiceNotFound 原文透传出去。
        # 试运行同样会报错 —— 以前 dry 直接返回 ok，把"服务不存在"一直藏到正式执行。
        domain, derr = self._clawbot_domain()
        if derr:
            result["error"] = derr
            return
        result["domain"] = domain

        tmp_file = ""
        try:
            if kind == "text":
                msg, err = await self._render_text(cfg.get("message") or "")
                if err:
                    result["error"] = err
                    return
                if not msg:
                    result["ok"] = True
                    result["skipped"] = True
                    result["error"] = "文字内容为空，已跳过"
                    return
                service = "send_message"
                payload: dict[str, Any] = {"message": msg, "target": target}
                result["resolved"] = msg
            elif kind in CLAWBOT_MEDIA_KINDS:
                eid = str(cfg.get("entity") or "").strip()
                raw_path = str(cfg.get("path") or "").strip()
                if eid:
                    result["from"] = eid
                    if dry:
                        # 试运行不抓拍、不落文件
                        result["resolved"] = eid
                        result["ok"] = True
                        result["dry"] = True
                        return
                    path, err = await self._clawbot_entity_file(eid, kind)
                    tmp_file = "" if err else path
                elif raw_path:
                    result["path"] = raw_path
                    path, err = await self._render_text(raw_path)
                else:
                    path, err = "", "缺少媒体来源（实体或路径）"
                if err:
                    result["error"] = err
                    return
                if not path:
                    result["ok"] = True
                    result["skipped"] = True
                    result["error"] = "路径为空，已跳过"
                    return
                service = "send_media"
                payload = {"kind": kind, "media_path": path, "target": target}
                caption = ""
                if cfg.get("caption"):
                    caption, cerr = await self._render_text(cfg.get("caption"))
                    if cerr:
                        result["error"] = cerr
                        return
                if caption:
                    payload["caption"] = caption
                result["resolved"] = path
            else:
                result["error"] = f"不支持的发送类型：{kind}"
                return

            if entry:
                payload["entry"] = entry
            if cfg.get("dedupe_key"):
                dedupe, derr = await self._render_text(cfg.get("dedupe_key"))
                if derr:
                    result["error"] = derr
                    return
                if dedupe:
                    payload["dedupe_key"] = dedupe

            result["service"] = f"{domain}.{service}"
            result["data"] = payload

            if dry:
                result["ok"] = True
                result["dry"] = True
                return

            try:
                await self.hass.services.async_call(
                    domain, service, payload, blocking=True,
                )
                result["ok"] = True
            except Exception as exc:
                result["error"] = str(exc)
        finally:
            # 只删自己生成的临时文件；用户给的路径一律不动
            if tmp_file:
                try:
                    os.remove(tmp_file)
                except OSError:
                    pass

    async def _execute_actions(
        self, actions: list, stop_on_error: bool, auto: dict | None = None,
        dry: bool = False,
    ) -> list[dict]:
        """顺序执行动作，逐条记录结果；stop_on_error 时失败即停。

        动作形态：
          {"service":…, "entity_id":…, "data":{…}}              调用服务
          {"delay": 秒}                                          等待
          {"assign": {"to": 实体, "from": 源实体}}                把实体的值赋给目标实体
          {"assign": {"to": 实体, "template": "{{ … }}"}}         把模板结果赋给目标实体
          {"assign": {"to": 实体, "value": "固定内容"}}           直接赋一个固定值
          {"clawbot": {"kind": …, "entity"|"path"|"message": …}}  发微信（文字/图片/视频/语音/文件）

        auto 传入时，会在每次服务调用前把目标实体登记到 source_registry，
        供 device_history 标记「这次状态变化是自动化造成的」（纯内存旁路）。

        dry=True（试运行）：同样求值 / 编译，但不调服务、不睡延迟，
        让前端能看到「到时候会写进去什么值」。
        """
        results: list[dict] = []
        for idx, act in enumerate(actions):
            if not isinstance(act, dict):
                continue
            # ──【v6.14.83】延迟动作：{"delay": 秒}，没有 service ──
            #   典型用法：开灯 → delay 300 → 关灯。
            #   已知限制：延迟期间 HA 重启，后面的动作会丢（不做恢复队列 ——
            #   那需要把"待执行余量"落库并在启动时回放，复杂度与收益不成比例）。
            if not str(act.get("service") or "").strip() and act.get("delay") is not None:
                try:
                    secs = max(0, int(act.get("delay") or 0))
                except (TypeError, ValueError):
                    secs = 0
                results.append({
                    "index": idx, "service": f"（延迟 {secs} 秒）",
                    "entity_id": "", "data": {}, "ok": True, "error": "",
                })
                if secs > 0 and not dry:
                    await asyncio.sleep(secs)
                continue
            # ──【v6.16.0】赋值动作：{"assign": {"to": …, "from"|"template"|"value": …}} ──
            assign = act.get("assign")
            if isinstance(assign, dict):
                await self._run_assign(results, idx, assign, auto, dry)
                if stop_on_error and results and not results[-1].get("ok") \
                        and not results[-1].get("skipped"):
                    break
                continue
            # ──【v7.0.0.14】微信发送：{"clawbot": {"kind": …, "entity"|"path"|"message": …}} ──
            clawbot = act.get("clawbot")
            if isinstance(clawbot, dict):
                await self._run_clawbot(results, idx, clawbot, auto, dry)
                if stop_on_error and results and not results[-1].get("ok") \
                        and not results[-1].get("skipped"):
                    break
                continue
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
            if dry:
                # 试运行：服务调用同样只"计划"不执行（不登记来源、不碰设备）
                result["ok"] = True
                result["dry"] = True
                results.append(result)
                continue
            domain, srv = service.split(".", 1)
            # 【v6.14.92】多目标批量：动作行支持 "a, b, c" 逗号串。
            #   HA 的 target.entity_id 接受数组，但 "a,b" 字符串会被当成一个不存在的实体 ID。
            #   `_target_entities`（写 source_registry 用）本来就能解析逗号串，这里只补 target。
            ids = [x.strip() for x in entity_id.replace("\n", ",").split(",") if x.strip()] if entity_id else []
            target = {"entity_id": ids if len(ids) > 1 else (ids[0] if ids else None)} if ids else None
            # 控制来源：先登记再执行 —— 状态变化监听写 device_history 时会按
            #   entity_id + ±2 秒消费这条，从而标成 automation / 自动化名
            if auto is not None:
                try:
                    _push_source_many(
                        self._target_entities(entity_id, data),
                        SOURCE_TYPE_AUTOMATION,
                        auto.get("id"),
                        auto.get("name") or "",
                    )
                except Exception:
                    pass
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
    def compute_next_run(self, auto: dict, now_str: str) -> str | None:
        """计算下次触发时间（本地字符串 YYYY-MM-DD HH:MM:SS）。
        定时型：找下一个满足 time + 星期白名单的分钟点（今天已过则顺延）。
        间隔型：now + interval_seconds（重启后过期不补跑，直接顺延）。
        """
        ttype = auto.get("trigger_type") or "time"
        cfg = self._parse_json(auto.get("trigger_config"), {})
        # 【v6.14.81】状态触发没有"下次时间"，返回**空字符串**而不是 None ——
        #   表的 `next_run` 列是 `TEXT NOT NULL`（见 __init__.py 建表），
        #   把 None 传下去会在 UPDATE 时触发 NOT NULL 约束（v6.14.80 实测）。
        #   空串与"没算过"在 `_tick` 里是同一个含义（`or ""` 之后为 falsy → 只算不跑），
        #   而且 state 类型本来就被 `_tick` 跳过，这里只是兜底。
        if ttype == "state":
            return ""
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
        # 【v6.14.76】状态触发的人类可读描述
        if ttype == "state":
            eid = str(cfg.get("entity_id") or "").strip() or "（未设置实体）"
            frm = str(cfg.get("from") or "").strip()
            to = str(cfg.get("to") or "").strip()
            attr = str(cfg.get("attribute") or "").strip()
            subject = f"{eid}[{attr}]" if attr else eid
            try:
                hold = int(cfg.get("for") or 0)
            except (TypeError, ValueError):
                hold = 0
            suffix = f"（持续 {hold} 秒）" if hold > 0 else ""
            if frm and to:
                return f"{subject} 从 {frm} 变为 {to}{suffix}"
            if to:
                return f"{subject} 变为 {to}{suffix}"
            if frm:
                return f"{subject} 离开 {frm}{suffix}"
            return f"{subject} 任意变化{suffix}"
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
