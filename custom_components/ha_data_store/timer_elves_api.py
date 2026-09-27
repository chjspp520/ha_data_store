"""定时精灵 HTTP API - ha_data_store 子模块。

所有接口挂在 /api/ha_data_store/timer 之下，按子路径 + HTTP 方法分派：

  鉴权：所有接口都要求「API 访问」开关开启 + 有效 API Key
        （key 用 ?key=xxx 或 Authorization: Bearer xxx 传入）；
        写接口在此之上再要求「数据库修改」开关开启。

  查询
    GET    /api/ha_data_store/timer
        概览：活跃定时器、活跃周期任务、统计、时区
    GET    /api/ha_data_store/timer/summary
        纯统计数据（不含任务列表）
    GET    /api/ha_data_store/timer/tasks
        任务列表，支持 ?task_id= &entity_id= &status= &active=1 过滤
    GET    /api/ha_data_store/timer/history
        历史分页：?limit= &offset= &entity_id= &status= &start= &end= &with_data=1
    GET    /api/ha_data_store/timer/entity_tasks
        指定实体 / 多实体的定时任务：?entities=a,b &include_history=1 &active=1
        &is_recurring=1|0 &limit= （返回 tasks 与 by_entity 分组）
    GET    /api/ha_data_store/timer/entity_daily
        指定实体 / 多实体在某天的定时任务：?date=YYYY-MM-DD &entities=a,b
        &basis=created|executed &status= &limit=
    GET    /api/ha_data_store/timer/month_dates
        指定月哪些日期有数据：?month=YYYY-MM &entities=a,b &basis=created|executed

  写入（在「API 访问」之上再受「数据库修改」开关约束）
    POST   /api/ha_data_store/timer
        创建任务，body：
          {kind: timer|climate|cover|schedule, entity_id, duration?,
           repeat_type?, schedule_time?, weekdays?, month_days?,
           action_type?, action_data?}
    POST   /api/ha_data_store/timer/update
        修改任务，body：{task_id, patch: {duration?, action_type?, action_data?,
                        repeat_type?, schedule_time?, weekdays?, month_days?}}
    POST   /api/ha_data_store/timer/cancel
        取消任务，body：{task_id? | timer_id? | schedule_id? | entity_id?}
    DELETE /api/ha_data_store/timer?id=xxx
        取消任务（query 形式，可带 schedule_id= / entity_id= / type=）

兼容旧式调用：body 里带 action（create_timer / create_schedule /
create_climate_timer / create_cover_timer / cancel_timer / cancel_schedule /
cancel_entity_timer / update_task）时按 action 路由，字段与旧版一致。
"""
from __future__ import annotations

import logging
import re

from aiohttp import web
from homeassistant.core import HomeAssistant

from .http_api import _BaseDBView

_LOGGER = logging.getLogger(__name__)

# HH:MM:SS 或 MM:SS
_DURATION_RE = re.compile(r"^\d{1,3}:\d{2}(:\d{2})?$")

# 旧式 action → 子路径
_ACTION_TO_SUB = {
    "create_timer": "create",
    "create_schedule": "create",
    "create_climate_timer": "create",
    "create_cover_timer": "create",
    "cancel_timer": "cancel",
    "cancel_schedule": "cancel",
    "cancel_entity_timer": "cancel",
    "update_task": "update",
}

# 旧式 action → 任务类型
_ACTION_TO_KIND = {
    "create_timer": "timer",
    "create_schedule": "schedule",
    "create_climate_timer": "climate",
    "create_cover_timer": "cover",
}

_PATCH_KEYS = (
    "duration",
    "action_type",
    "action_data",
    "repeat_type",
    "schedule_time",
    "weekdays",
    "month_days",
)

_SUMMARY_KEYS = (
    "active_timers",
    "active_schedules",
    "active_tasks",
    "total_tasks",
    "successful_task",
    "failed_task",
    "today_task",
    "time_zone",
)


class TimerElvesAPIView(_BaseDBView):
    """定时精灵 HTTP API View。"""

    url = "/api/ha_data_store/timer"
    name = "api:ha_data_store:timer"
    extra_urls = [
        "/api/ha_data_store/timer/summary",
        "/api/ha_data_store/timer/tasks",
        "/api/ha_data_store/timer/entity_tasks",
        "/api/ha_data_store/timer/entity_daily",
        "/api/ha_data_store/timer/month_dates",
        "/api/ha_data_store/timer/history",
        "/api/ha_data_store/timer/update",
        "/api/ha_data_store/timer/cancel",
    ]

    def __init__(self, hass: HomeAssistant, db_path: str, coordinator) -> None:
        super().__init__(db_path)
        self.hass = hass
        self.coordinator = coordinator

    # ========== 辅助 ==========

    def _sub(self, request: web.Request) -> str:
        """取 URL 子路径（''/summary/tasks/history/update/cancel）。"""
        path = request.path.rstrip("/")
        base = self.url.rstrip("/")
        if path == base:
            return ""
        if not path.startswith(base):
            return ""
        return path[len(base):].strip("/")

    def _fail(self, message: str, status_code: int = 400) -> web.Response:
        return self.json({"success": False, "error": message}, status_code=status_code)

    # ========== GET ==========

    async def get(self, request: web.Request) -> web.Response:
        # 鉴权：读接口也强制校验 API Key（与本项目其它接口一致，用 _check_api_enabled）
        if (resp := self._check_api_enabled(request)):
            return resp
        sub = self._sub(request)
        try:
            if sub == "history":
                return await self._history(request)
            if sub == "tasks":
                return await self._tasks(request)
            if sub == "entity_tasks":
                return await self._entity_tasks(request)
            if sub == "entity_daily":
                return await self._entity_daily(request)
            if sub == "month_dates":
                return await self._month_dates(request)
            if sub == "summary":
                summary = await self.coordinator.get_summary()
                return self.json(
                    {"success": True, "summary": {k: summary.get(k) for k in _SUMMARY_KEYS}}
                )
            if sub == "":
                return self.json({"success": True, **await self.coordinator.get_summary()})
            return self._fail(f"未知子路径: {request.path}", 404)
        except Exception as exc:
            _LOGGER.exception("[timer_elves] 查询失败")
            return self._fail(str(exc), 500)

    async def _tasks(self, request: web.Request) -> web.Response:
        query = request.query
        task_id = (query.get("task_id") or "").strip()
        entity_id = (query.get("entity_id") or "").strip()
        status = (query.get("status") or "").strip()
        active_only = (query.get("active") or "") in ("1", "true", "yes")

        if task_id:
            task = await self.coordinator.get_task(task_id)
            if not task:
                return self._fail(f"任务不存在: {task_id}", 404)
            return self.json({"success": True, "task": task})

        items = []
        for tid, td in self.coordinator.tasks.items():
            if entity_id and td.get("entity_id") != entity_id:
                continue
            if status and td.get("status") != status:
                continue
            if active_only and td.get("status") != "active":
                continue
            item = dict(td)
            item["id"] = tid
            items.append(item)
        return self.json({"success": True, "count": len(items), "tasks": items})

    async def _history(self, request: web.Request) -> web.Response:
        query = request.query
        try:
            limit = int(query.get("limit") or 50)
            offset = int(query.get("offset") or 0)
        except (TypeError, ValueError):
            return self._fail("limit / offset 必须为整数")
        result = await self.coordinator.get_history(
            limit=limit,
            offset=offset,
            entity_id=(query.get("entity_id") or "").strip() or None,
            status=(query.get("status") or "").strip() or None,
            start=(query.get("start") or "").strip() or None,
            end=(query.get("end") or "").strip() or None,
            with_data=(query.get("with_data") or "") in ("1", "true", "yes"),
        )
        return self.json({"success": True, **result})

    @staticmethod
    def _parse_entities(request: web.Request) -> list:
        """解析实体参数：优先 entities，其次 entity_id；支持逗号 / 中文逗号 / 分号 / 空格分隔。"""
        raw = (request.query.get("entities") or request.query.get("entity_id") or "").strip()
        if not raw:
            return []
        for sep in ("，", ";", "；", "|", "\n", "\t"):
            raw = raw.replace(sep, ",")
        raw = raw.replace(" ", ",")
        out: list = []
        for part in raw.split(","):
            entity_id = part.strip()
            if entity_id and entity_id not in out:
                out.append(entity_id)
        return out

    async def _entity_tasks(self, request: web.Request) -> web.Response:
        """查询一个/多个实体的定时任务。

        ?entities=climate.a,light.b  多实体（逗号分隔；兼容 entity_id 参数与中文逗号）
        ?include_history=1           把历史（已完成/失败/取消）一并返回，默认只返回活跃任务
        ?active=1                    只返回活跃任务
        ?is_recurring=1|0            只取周期任务 / 只取一次性定时器
        ?limit=                      任务条数上限（默认 500，最大 2000）
        """
        query = request.query
        try:
            limit = int(query.get("limit") or 500)
        except (TypeError, ValueError):
            return self._fail("limit 必须为整数")
        recurring_raw = (query.get("is_recurring") or "").strip().lower()
        is_recurring = None
        if recurring_raw in ("1", "true", "yes"):
            is_recurring = True
        elif recurring_raw in ("0", "false", "no"):
            is_recurring = False
        result = await self.coordinator.get_entity_tasks(
            entities=self._parse_entities(request),
            include_history=(query.get("include_history") or "") in ("1", "true", "yes"),
            active_only=(query.get("active") or "") in ("1", "true", "yes"),
            is_recurring=is_recurring,
            limit=limit,
        )
        return self.json({"success": True, **result})

    async def _entity_daily(self, request: web.Request) -> web.Response:
        """查询指定实体（可多个）在某一天的定时任务。

        ?date=YYYY-MM-DD（必填）
        ?entities=climate.a,light.b  可选，多实体过滤（兼容 entity_id，支持中文逗号）
        ?basis=created|executed      日期口径：任务创建日期（默认）/ 实际执行日期
        ?status=active|completed|failed|cancelled  可选状态过滤
        ?limit=                      条数上限（默认 500，最大 2000）
        """
        query = request.query
        date = (query.get("date") or "").strip()
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
            return self._fail("date 必填，格式 YYYY-MM-DD")
        basis = (query.get("basis") or "created").strip().lower()
        if basis not in ("created", "executed"):
            return self._fail("basis 仅支持 created / executed")
        try:
            limit = int(query.get("limit") or 500)
        except (TypeError, ValueError):
            return self._fail("limit 必须为整数")
        result = await self.coordinator.get_entity_daily_tasks(
            date=date,
            entities=self._parse_entities(request),
            basis=basis,
            status=(query.get("status") or "").strip() or None,
            limit=limit,
        )
        return self.json({"success": True, **result})

    async def _month_dates(self, request: web.Request) -> web.Response:
        """查询指定月内哪些日期有定时任务数据。

        ?month=YYYY-MM（必填）
        ?entities=climate.a,light.b  可选，多实体过滤（兼容 entity_id，支持中文逗号）
        ?basis=created|executed      统计口径：任务创建日期（默认）/ 实际执行日期
        """
        query = request.query
        month = (query.get("month") or "").strip()
        if not re.match(r"^\d{4}-\d{2}$", month):
            return self._fail("month 必填，格式 YYYY-MM")
        basis = (query.get("basis") or "created").strip().lower()
        if basis not in ("created", "executed"):
            return self._fail("basis 仅支持 created / executed")
        result = await self.coordinator.get_month_dates(
            month=month,
            entities=self._parse_entities(request),
            basis=basis,
        )
        return self.json({"success": True, **result})

    # ========== POST ==========

    async def post(self, request: web.Request) -> web.Response:
        # 鉴权：写接口在 API Key 之上，再要求「数据库修改」开关开启
        if (resp := self._check_api_enabled(request)):
            return resp
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_db_edit_enabled(hass)):
            return resp
        try:
            body = await request.json()
        except Exception:
            return self._fail("请求体需为 JSON")
        if not isinstance(body, dict):
            return self._fail("请求体需为 JSON 对象")

        sub = self._sub(request)
        action = (body.get("action") or "").strip()
        if action and not sub:
            sub = _ACTION_TO_SUB.get(action, "")
            if not sub:
                return self._fail(f"未知 action: {action}")

        try:
            if sub in ("", "create"):
                return await self._create(body)
            if sub == "update":
                return await self._update(body)
            if sub == "cancel":
                return await self._cancel(body)
            return self._fail(f"未知子路径: {request.path}", 404)
        except ValueError as exc:
            return self._fail(str(exc))
        except Exception as exc:
            _LOGGER.exception("[timer_elves] 写操作失败")
            return self._fail(str(exc), 500)

    async def _create(self, body: dict) -> web.Response:
        entity_id = (body.get("entity_id") or "").strip()
        if not entity_id or "." not in entity_id:
            return self._fail("entity_id 必填，格式为 domain.object_id")
        if self.hass.states.get(entity_id) is None:
            return self._fail(f"实体不存在: {entity_id}", 404)

        action = (body.get("action") or "").strip()
        kind = (body.get("kind") or _ACTION_TO_KIND.get(action) or "timer").strip()
        repeat_type = (body.get("repeat_type") or "none").strip()
        schedule_time = (body.get("schedule_time") or "").strip()

        if kind == "schedule" or repeat_type != "none":
            if not schedule_time:
                return self._fail("周期任务必须提供 schedule_time（HH:MM:SS）")
            if not re.match(r"^\d{1,2}:\d{2}(:\d{2})?$", schedule_time):
                return self._fail("schedule_time 格式应为 HH:MM:SS")
            if repeat_type == "weekly" and not body.get("weekdays"):
                return self._fail("weekly 必须提供 weekdays（1=周一 ... 7=周日）")
            if repeat_type == "monthly" and not body.get("month_days"):
                return self._fail("monthly 必须提供 month_days（1-31）")
        else:
            duration = (body.get("duration") or "").strip()
            if duration and not _DURATION_RE.match(duration):
                return self._fail("duration 格式应为 HH:MM:SS 或 MM:SS")

        payload = dict(body)
        payload["kind"] = kind
        payload.setdefault("user_id", "api")
        task = await self.coordinator.create_task(payload)
        return self.json({"success": True, "message": "任务已创建", "task": task})

    async def _update(self, body: dict) -> web.Response:
        task_id = (body.get("task_id") or body.get("id") or "").strip()
        if not task_id:
            return self._fail("task_id 必填")
        patch = body.get("patch") if isinstance(body.get("patch"), dict) else None
        if patch is None:
            patch = {k: body[k] for k in _PATCH_KEYS if k in body}
        if not patch:
            return self._fail(f"patch 不能为空，可改字段：{', '.join(_PATCH_KEYS)}")
        duration = (patch.get("duration") or "").strip() if isinstance(patch.get("duration"), str) else ""
        if duration and not _DURATION_RE.match(duration):
            return self._fail("duration 格式应为 HH:MM:SS 或 MM:SS")
        task = await self.coordinator.update_task(task_id, patch)
        return self.json({"success": True, "message": "任务已更新", "task": task})

    async def _cancel(self, body: dict) -> web.Response:
        task_id = (body.get("task_id") or body.get("timer_id") or "").strip()
        schedule_id = (body.get("schedule_id") or "").strip()
        entity_id = (body.get("entity_id") or "").strip()

        if task_id:
            task = self.coordinator.tasks.get(task_id)
            if task is None:
                return self._fail(f"任务不存在: {task_id}", 404)
            if task.get("is_recurring"):
                await self.coordinator.cancel_schedule(task_id)
            else:
                await self.coordinator.cancel_timer(task_id)
            return self.json({"success": True, "message": f"任务已取消: {task_id}"})

        if schedule_id:
            if schedule_id not in self.coordinator.tasks:
                return self._fail(f"周期任务不存在: {schedule_id}", 404)
            await self.coordinator.cancel_schedule(schedule_id)
            return self.json({"success": True, "message": f"周期任务已取消: {schedule_id}"})

        if entity_id:
            if self.hass.states.get(entity_id) is None and not any(
                td.get("entity_id") == entity_id for td in self.coordinator.tasks.values()
            ):
                return self._fail(f"没有该实体的任务: {entity_id}", 404)
            await self.coordinator.cancel_entity_timer(entity_id, "api")
            return self.json({"success": True, "message": f"该实体全部任务已取消: {entity_id}"})

        return self._fail("需要提供 task_id / schedule_id / entity_id 之一")

    # ========== DELETE ==========

    async def delete(self, request: web.Request) -> web.Response:
        if (resp := self._check_api_enabled(request)):
            return resp
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_db_edit_enabled(hass)):
            return resp
        query = request.query
        try:
            return await self._cancel(
                {
                    "task_id": (query.get("id") or query.get("task_id") or "").strip(),
                    "schedule_id": (query.get("schedule_id") or "").strip(),
                    "entity_id": (query.get("entity_id") or "").strip(),
                }
            )
        except ValueError as exc:
            return self._fail(str(exc))
        except Exception as exc:
            _LOGGER.exception("[timer_elves] 取消失败")
            return self._fail(str(exc), 500)


async def async_setup_timer_api(hass: HomeAssistant, db_path: str, coordinator) -> None:
    """注册定时精灵 HTTP API。"""
    view = TimerElvesAPIView(hass, db_path, coordinator)
    hass.http.register_view(view)
    _LOGGER.info("[timer_elves] API registered at %s", TimerElvesAPIView.url)
