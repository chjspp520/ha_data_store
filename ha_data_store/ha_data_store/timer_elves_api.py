"""定时精灵 HTTP API - ha_data_store 子模块。"""
from __future__ import annotations

import logging
from typing import Any

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class TimerElvesAPIView(HomeAssistantView):
    """定时精灵 HTTP API View，挂载于 /api/ha_data_store/timer。"""

    name = "api:ha_data_store:timer"
    url = "/api/ha_data_store/timer"

    requires_auth = True

    def __init__(self, hass: HomeAssistant, coordinator) -> None:
        self.hass = hass
        self.coordinator = coordinator

    async def get(self, request: web.Request) -> web.Response:
        """GET - 获取任务列表。"""
        try:
            entity_id = request.query.get("entity_id")
            if entity_id:
                tasks = await self._get_entity_tasks(entity_id)
                return self.json({"success": True, "entity_id": entity_id, "tasks": tasks})
            all_tasks = await self._get_all_tasks()
            return self.json({"success": True, "tasks": all_tasks, "count": len(all_tasks)})
        except Exception as e:
            _LOGGER.error(f"[timer_elves] API GET error: {e}")
            return self.json({"success": False, "error": str(e)}, status_code=500)

    async def post(self, request: web.Request) -> web.Response:
        """POST - 创建/取消/查询任务。"""
        try:
            data = await request.json()
            action = data.get("action", "create_timer")
            handlers = {
                "create_timer": self._create_timer,
                "create_schedule": self._create_schedule,
                "create_climate_timer": self._create_climate_timer,
                "cancel_timer": self._cancel_timer,
                "cancel_schedule": self._cancel_schedule,
                "cancel_entity_timer": self._cancel_entity_timer,
                "get_timers": self._get_timers,
                "get_schedules": self._get_schedules,
                "get_entity_tasks": self._get_entity_tasks_api,
            }
            handler = handlers.get(action)
            if not handler:
                return self.json({"success": False, "error": f"Unknown action: {action}"}, status_code=400)
            return await handler(data)
        except Exception as e:
            _LOGGER.error(f"[timer_elves] API POST error: {e}")
            return self.json({"success": False, "error": str(e)}, status_code=500)

    async def delete(self, request: web.Request) -> web.Response:
        """DELETE - 取消任务。"""
        try:
            data = await request.json()
            timer_id = data.get("timer_id")
            schedule_id = data.get("schedule_id")
            entity_id = data.get("entity_id")
            if timer_id:
                return await self._cancel_timer({"timer_id": timer_id})
            if schedule_id:
                return await self._cancel_schedule({"schedule_id": schedule_id})
            if entity_id:
                return await self._cancel_entity_timer({"entity_id": entity_id})
            return self.json({"success": False, "error": "Missing timer_id, schedule_id or entity_id"}, status_code=400)
        except Exception as e:
            _LOGGER.error(f"[timer_elves] API DELETE error: {e}")
            return self.json({"success": False, "error": str(e)}, status_code=500)

    # ========== 创建任务 ==========

    async def _create_timer(self, data: dict) -> web.Response:
        try:
            entity_id = data.get("entity_id")
            if not entity_id:
                return self.json({"success": False, "error": "entity_id is required"}, status_code=400)
            timer_data = {
                "entity_id": entity_id,
                "duration": data.get("duration", "00:30:00"),
                "action_type": data.get("action_type", "auto"),
                "action_data": data.get("action_data", {}),
                "user_id": "api_user",
            }
            result = await self.coordinator.create_timer(timer_data)
            return self.json({"success": True, "message": "Timer created", "timer": result})
        except Exception as e:
            _LOGGER.error(f"[timer_elves] Create timer error: {e}")
            return self.json({"success": False, "error": str(e)}, status_code=500)

    async def _create_climate_timer(self, data: dict) -> web.Response:
        try:
            entity_id = data.get("entity_id")
            if not entity_id:
                return self.json({"success": False, "error": "entity_id is required"}, status_code=400)
            timer_data = {
                "entity_id": entity_id,
                "duration": data.get("duration", "01:00:00"),
                "action_type": data.get("action_type", "turn_off"),
                "action_data": data.get("action_data", {}),
                "user_id": "api_user",
            }
            result = await self.coordinator.create_climate_timer(timer_data)
            return self.json({"success": True, "message": "Climate timer created", "timer": result})
        except Exception as e:
            _LOGGER.error(f"[timer_elves] Create climate timer error: {e}")
            return self.json({"success": False, "error": str(e)}, status_code=500)

    async def _create_schedule(self, data: dict) -> web.Response:
        try:
            entity_id = data.get("entity_id")
            if not entity_id:
                return self.json({"success": False, "error": "entity_id is required"}, status_code=400)
            schedule_data = {
                "entity_id": entity_id,
                "repeat_type": data.get("repeat_type", "daily"),
                "schedule_time": data.get("schedule_time", "08:00:00"),
                "action_type": data.get("action_type", "auto"),
                "action_data": data.get("action_data", {}),
                "weekdays": data.get("weekdays", []),
                "month_days": data.get("month_days", []),
                "user_id": "api_user",
            }
            result = await self.coordinator.create_schedule(schedule_data)
            return self.json({"success": True, "message": "Schedule created", "schedule": result})
        except Exception as e:
            _LOGGER.error(f"[timer_elves] Create schedule error: {e}")
            return self.json({"success": False, "error": str(e)}, status_code=500)

    # ========== 取消任务 ==========

    async def _cancel_timer(self, data: dict) -> web.Response:
        try:
            timer_id = data.get("timer_id")
            if not timer_id:
                return self.json({"success": False, "error": "timer_id is required"}, status_code=400)
            await self.coordinator.cancel_timer(timer_id)
            return self.json({"success": True, "message": f"Timer {timer_id} cancelled"})
        except Exception as e:
            return self.json({"success": False, "error": str(e)}, status_code=500)

    async def _cancel_schedule(self, data: dict) -> web.Response:
        try:
            schedule_id = data.get("schedule_id")
            if not schedule_id:
                return self.json({"success": False, "error": "schedule_id is required"}, status_code=400)
            await self.coordinator.cancel_schedule(schedule_id)
            return self.json({"success": True, "message": f"Schedule {schedule_id} cancelled"})
        except Exception as e:
            return self.json({"success": False, "error": str(e)}, status_code=500)

    async def _cancel_entity_timer(self, data: dict) -> web.Response:
        try:
            entity_id = data.get("entity_id")
            if not entity_id:
                return self.json({"success": False, "error": "entity_id is required"}, status_code=400)
            await self.coordinator.cancel_entity_timer(entity_id, "api_user")
            return self.json({"success": True, "message": f"All tasks for {entity_id} cancelled"})
        except Exception as e:
            return self.json({"success": False, "error": str(e)}, status_code=500)

    # ========== 查询任务 ==========

    async def _get_timers(self) -> web.Response:
        try:
            timers = []
            for task_id, td in self.coordinator.tasks.items():
                if td.get("is_recurring"):
                    continue
                if td.get("status") == "active":
                    timers.append(td)
            return self.json({"success": True, "timers": timers, "count": len(timers)})
        except Exception as e:
            return self.json({"success": False, "error": str(e)}, status_code=500)

    async def _get_schedules(self) -> web.Response:
        try:
            schedules = []
            for task_id, td in self.coordinator.tasks.items():
                if td.get("is_recurring") and td.get("status") == "active":
                    schedules.append(td)
            return self.json({"success": True, "schedules": schedules, "count": len(schedules)})
        except Exception as e:
            return self.json({"success": False, "error": str(e)}, status_code=500)

    async def _get_entity_tasks_api(self, data: dict) -> web.Response:
        try:
            entity_id = data.get("entity_id")
            if not entity_id:
                return self.json({"success": False, "error": "entity_id is required"}, status_code=400)
            tasks = await self._get_entity_tasks(entity_id)
            return self.json({"success": True, "entity_id": entity_id, "tasks": tasks, "count": len(tasks)})
        except Exception as e:
            return self.json({"success": False, "error": str(e)}, status_code=500)

    async def _get_entity_tasks(self, entity_id: str) -> list:
        return [td for td in self.coordinator.tasks.values() if td.get("entity_id") == entity_id]

    async def _get_all_tasks(self) -> list:
        return list(self.coordinator.tasks.values())


async def async_setup_timer_api(hass: HomeAssistant, coordinator) -> None:
    """注册定时精灵 HTTP API。"""
    view = TimerElvesAPIView(hass, coordinator)
    hass.http.register_view(view)
    _LOGGER.info("[timer_elves] API registered at /api/ha_data_store/timer")
