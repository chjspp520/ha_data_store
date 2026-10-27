"""实体编组的动作适配层。

职责只有三件事：
  1. 把编组语义（``$group`` / ``$group:light`` / 成员 extra）**展开**成标准的
     ``[{"service": "domain.service", "entity_id": ..., "data": {...}}]``；
  2. 交给现有 ``AutomationManager._execute_actions()`` 执行（不新写执行引擎）；
  3. 执行结果写进现有 ``automation_logs``（``automation_id=0`` + ``trigger_type=group``）。

本模块**不 import homeassistant**，只做纯数据变换，因此可以离线单测；
真正调用服务、写库都在传入的 ``hass`` / ``AutomationManager`` 上完成。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from .const import DOMAIN, ENTITY_GROUP_DOMAIN_SERVICE_MAP

_LOGGER = logging.getLogger(__name__)

# 内置命令：只支持明确的 on/off（混编组里"取反"语义不成立）
COMMANDS = ("on", "off")


# ── 纯函数：展开与生成 ──────────────────────────────────────────────────────
def resolve_entities(value: Any, members: list[dict]) -> list[str | None]:
    """把动作里的 ``entity_id`` 展开成具体实体列表。

    - ``None`` / ``""``      → ``[None]``（调用无 target 的服务）
    - ``$group``             → 全部成员
    - ``$group:light``       → 该域成员
    - ``light.xxx``          → 原样
    """
    if not value:
        return [None]
    text = str(value).strip()
    if text == "$group":
        return [m["entity_id"] for m in members] or [None]
    if text.startswith("$group:"):
        domain = text.split(":", 1)[1].strip().lower()
        if not domain:
            return [m["entity_id"] for m in members] or [None]
        picked = [
            m["entity_id"] for m in members
            if str(m.get("entity_id", "")).startswith(f"{domain}.")
        ]
        return picked or [None]
    return [text]


def expand_action_steps(steps: list[dict], members: list[dict]) -> list[dict]:
    """把预设动作的行为条目展开成可执行动作列表（一个实体一条）。"""
    actions: list[dict] = []
    for step in steps or []:
        service = str(step.get("service") or "").strip()
        if not service:
            continue
        data = step.get("data") if isinstance(step.get("data"), dict) else {}
        for entity_id in resolve_entities(step.get("entity_id"), members):
            actions.append({
                "service": service,
                "entity_id": entity_id or "",
                "data": dict(data),
            })
    return actions


def _domain_of(entity_id: str) -> str:
    return entity_id.split(".", 1)[0]


def build_command_actions(
    group: dict,
    command: str,
    service_map: dict | None = None,
) -> list[dict]:
    """按成员域生成内置 on/off 动作（成员 extra 与编组 options 逐层合并）。

    优先级（低 → 高）：设备当前值 < 编组 ``options`` < 成员 ``extra``。
    成员 ``extra`` 里的 ``on_service`` / ``on_data`` 完全接管该成员。
    """
    if command not in COMMANDS:
        raise ValueError(f"command 只能是 {COMMANDS}: {command!r}")

    mapping = service_map or ENTITY_GROUP_DOMAIN_SERVICE_MAP
    options = group.get("options") if isinstance(group.get("options"), dict) else {}
    actions: list[dict] = []

    for member in group.get("members") or []:
        entity_id = str(member.get("entity_id") or "")
        if "." not in entity_id:
            continue
        extra = member.get("extra") if isinstance(member.get("extra"), dict) else {}
        domain = _domain_of(entity_id)

        override_service = str(extra.get(f"{command}_service") or "").strip()
        if override_service and "." in override_service:
            override_data = extra.get(f"{command}_data")
            actions.append({
                "service": override_service,
                "entity_id": entity_id,
                "data": dict(override_data) if isinstance(override_data, dict) else {},
            })
            continue

        spec = mapping.get(domain)
        if not isinstance(spec, dict):
            # 只读域（sensor / binary_sensor 等）：不参与 on/off
            continue
        service = spec.get(command)
        if not service:
            continue

        domain_options = options.get(domain)
        merged: dict[str, Any] = {}
        if isinstance(domain_options, dict):
            merged.update(domain_options)
        merged.update(extra)

        data_keys = spec.get("data_keys") or ()
        data = {k: merged[k] for k in data_keys if k in merged}
        actions.append({"service": service, "entity_id": entity_id, "data": data})

    return actions


def member_is_on(state: str | None, domain: str, service_map: dict | None = None) -> bool:
    """按 ``ENTITY_GROUP_DOMAIN_SERVICE_MAP`` 的口径判断单个成员是否"开启"。"""
    if state is None:
        return False
    spec = (service_map or ENTITY_GROUP_DOMAIN_SERVICE_MAP).get(domain) or {}
    on_state = spec.get("on_state") or {}
    if "in" in on_state:
        return state in on_state["in"]
    if "not_in" in on_state:
        return state not in on_state["not_in"]
    return state not in ("off", "unavailable", "unknown")


def summarize_states(
    group: dict,
    states: dict[str, str | None],
    service_map: dict | None = None,
) -> dict:
    """汇总编组状态：``{is_on, on_count, member_count, partial, missing, on_entities, domains}``。"""
    members = group.get("members") or []
    on_entities: list[str] = []
    missing: list[str] = []
    domains: dict[str, int] = {}
    controllable = 0

    for member in members:
        entity_id = str(member.get("entity_id") or "")
        domain = _domain_of(entity_id)
        domains[domain] = domains.get(domain, 0) + 1
        state = states.get(entity_id)
        if "." not in entity_id:
            continue
        if entity_id not in states:
            missing.append(entity_id)
            continue
        if state in (None, "unavailable", "unknown"):
            missing.append(entity_id)
        spec = (service_map or ENTITY_GROUP_DOMAIN_SERVICE_MAP).get(domain)
        if not isinstance(spec, dict):
            continue  # 只读域不参与判定
        controllable += 1
        if member_is_on(state, domain, service_map):
            on_entities.append(entity_id)

    if controllable == 0:
        is_on = False
    else:
        is_on = len(on_entities) > 0
    return {
        "is_on": is_on,
        "on_count": len(on_entities),
        "member_count": len(members),
        "partial": 0 < len(on_entities) < controllable,
        "missing": missing,
        "on_entities": on_entities,
        "domains": domains,
    }


def pick_action(group: dict, action_id: str | None = None,
                command: str | None = None) -> dict | None:
    """挑选要执行的预设动作；返回 ``None`` 表示走内置 on/off 生成。"""
    actions = [a for a in (group.get("actions") or []) if isinstance(a, dict)]
    if action_id:
        for act in actions:
            if act.get("id") == action_id:
                return act
        return None
    if command in COMMANDS:
        ref = group.get(f"default_{command}_action")
        if ref:
            for act in actions:
                if act.get("id") == ref:
                    return act
    return None


# ── 带 hass 的执行入口（唯一需要运行时的部分）────────────────────────────────
def _status_of(results: list[dict]) -> str:
    if not results:
        return "success"
    failed = [r for r in results if not r.get("ok")]
    if not failed:
        return "success"
    return "failed" if len(failed) == len(results) else "partial_failed"


async def async_execute_group_action(
    hass,
    group: dict,
    action_id: str | None = None,
    command: str | None = None,
    source: str = "http",
) -> dict:
    """执行编组动作：挑动作 → 展开 → 交给 AutomationManager → 写日志。"""
    manager = hass.data.get(DOMAIN, {}).get("automation_manager")
    if manager is None:
        raise RuntimeError("自动化执行器未就绪")

    action = pick_action(group, action_id, command)
    members = group.get("members") or []

    if action is not None:
        actions = expand_action_steps(action.get("actions") or [], members)
        stop_on_error = bool(action.get("stop_on_error"))
        label = action.get("name") or action.get("id") or "预设动作"
    else:
        if command not in COMMANDS:
            raise ValueError("未指定动作，且 command 不是 on/off")
        mapping = None
        store = hass.data.get(DOMAIN, {}).get("entity_group_store")
        if store is not None:
            try:
                mapping = store.domain_service_map()
            except Exception:  # noqa: BLE001
                mapping = None
        actions = build_command_actions(group, command, mapping)
        stop_on_error = False
        label = "开" if command == "on" else "关"

    started = time.time()
    results = await manager._execute_actions(actions, stop_on_error)
    duration_ms = int((time.time() - started) * 1000)
    status = _status_of(results)

    await _write_log(hass, manager, group, label, results, status,
                     duration_ms, source)

    return {
        "group_id": group.get("id"),
        "label": label,
        "status": status,
        "actions": actions,
        "results": results,
        "duration_ms": duration_ms,
    }


async def _write_log(hass, manager, group: dict, label: str,
                     results: list[dict], status: str,
                     duration_ms: int, source: str) -> None:
    """复用 automation_logs（automation_id=0 + trigger_type=group），失败不影响执行。"""
    writer = getattr(manager, "_insert_log", None)
    if writer is None:
        return
    now = int(time.time() * 1000)
    name = f"[编组] {group.get('name') or group.get('id')}"
    if label:
        name = f"{name} / {label}"
    try:
        await hass.async_add_executor_job(
            writer,
            0, name, "group", f"{source}:{label}", now,
            1, "[]", status, json.dumps(results, ensure_ascii=False),
            duration_ms, now,
        )
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("[entity_group] 写执行日志失败: %s", exc)
