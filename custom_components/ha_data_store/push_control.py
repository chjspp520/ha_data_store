"""实体→网络：控制动作目录与执行引擎。

职责：
  - 内置「实体域 → 允许动作 → HA 服务 + 参数 schema」白名单目录（ACTION_CATALOG）
  - 把调用方请求（action + params）解析为具体的 HA 服务调用（含参数校验 / 锁定 / 限幅）
  - 能力发现：按实体属性动态展开参数（枚举 / 范围），供 db_viewer 与第三方系统使用
  - 限流与审计日志写入

设计要点（安全边界）：
  1. 只允许目录中**枚举过**的动作，不接受任意 domain.service（raw 模式也强制 domain == 实体域）。
  2. 参数一律经 `_coerce_value` 校验类型 / 取值 / 枚举成员，越界直接拒绝。
  3. 配置端可对参数做「锁定」：lock=value 固定值（调用方传参被忽略）、lock=range 限幅（超界裁剪）。
  4. 高风险域（lock / alarm_control_panel / siren）在 catalog 中标记 high_risk，UI 额外警告。
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import time
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant

from .const import DOMAIN, DEFAULT_TIMEZONE
from .logger import get_logger

_LOGGER = logging.getLogger(__name__)

# 审计日志保留天数
CONTROL_LOG_RETENTION_DAYS = 30
# 每写入 N 条审计日志触发一次过期清理
_CONTROL_LOG_CLEANUP_EVERY = 200
# 执行后等待状态刷新（wait_state=true）的最长秒数与轮询间隔
_WAIT_STATE_TIMEOUT = 3.0
_WAIT_STATE_INTERVAL = 0.2

_ENTITY_ID_RE = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z0-9_]+$")


# =========================================================================== #
#  参数 schema 构造辅助                                                          #
# =========================================================================== #
def _int(min_v: int | None = None, max_v: int | None = None, label: str = "",
         required: bool = False) -> dict:
    spec: dict[str, Any] = {"type": "int", "label": label}
    if min_v is not None:
        spec["min"] = min_v
    if max_v is not None:
        spec["max"] = max_v
    if required:
        spec["required"] = True
    return spec


def _float(min_v: float | None = None, max_v: float | None = None, step: float | None = None,
           label: str = "", required: bool = False) -> dict:
    spec: dict[str, Any] = {"type": "float", "label": label}
    if min_v is not None:
        spec["min"] = min_v
    if max_v is not None:
        spec["max"] = max_v
    if step is not None:
        spec["step"] = step
    if required:
        spec["required"] = True
    return spec


def _enum(dynamic: str, label: str = "", required: bool = False) -> dict:
    """枚举参数：选项从实体属性动态读取（见 _DYNAMIC_ENUM_ATTRS）。"""
    return {"type": "enum", "dynamic": dynamic, "label": label, "required": required}


def _fixed_enum(options: list[str], label: str = "", required: bool = False) -> dict:
    return {"type": "enum", "options": list(options), "label": label, "required": required}


def _str(label: str = "", required: bool = False, max_len: int | None = None) -> dict:
    spec: dict[str, Any] = {"type": "str", "label": label}
    if required:
        spec["required"] = True
    if max_len is not None:
        spec["max_len"] = max_len
    return spec


def _bool(label: str = "", required: bool = False) -> dict:
    return {"type": "bool", "label": label, "required": required}


def _dyn_range(kind: str, label: str = "", required: bool = False) -> dict:
    """数值参数：范围/步长从实体属性动态读取（见 _resolve_dynamic_range）。"""
    return {"type": "float", "dynamic_range": kind, "label": label, "required": required}


# =========================================================================== #
#  动作目录（白名单）                                                            #
# =========================================================================== #
# 结构：domain → {high_risk: bool, actions: {action: {service, label, params}}}
ACTION_CATALOG: dict[str, dict[str, Any]] = {
    "switch": {"actions": {
        "turn_on": {"service": "turn_on", "label": "打开"},
        "turn_off": {"service": "turn_off", "label": "关闭"},
        "toggle": {"service": "toggle", "label": "翻转"},
    }},
    "light": {"actions": {
        "turn_on": {"service": "turn_on", "label": "开灯", "params": {
            "brightness": _int(0, 255, "亮度(0-255)"),
            "color_temp_kelvin": _int(1000, 10000, "色温(K)"),
            "rgb_color": {"type": "rgb", "label": "RGB 颜色"},
            "effect": _enum("effect_list", "灯效"),
            "transition": _float(0, 300, label="过渡秒数"),
        }},
        "turn_off": {"service": "turn_off", "label": "关灯", "params": {
            "transition": _float(0, 300, label="过渡秒数"),
        }},
        "toggle": {"service": "toggle", "label": "翻转"},
    }},
    "climate": {"actions": {
        "set_hvac_mode": {"service": "set_hvac_mode", "label": "设置模式", "params": {
            "hvac_mode": _enum("hvac_modes", "运行模式", required=True),
        }},
        "set_temperature": {"service": "set_temperature", "label": "设置温度", "params": {
            "temperature": _dyn_range("temperature", "目标温度"),
            "hvac_mode": _enum("hvac_modes", "同时切模式"),
            "fan_mode": _enum("fan_modes", "同时设风速"),
        }},
        "set_fan_mode": {"service": "set_fan_mode", "label": "设置风速", "params": {
            "fan_mode": _enum("fan_modes", "风速", required=True),
        }},
        "set_swing_mode": {"service": "set_swing_mode", "label": "设置摆风", "params": {
            "swing_mode": _enum("swing_modes", "摆风", required=True),
        }},
        "turn_on": {"service": "turn_on", "label": "开机"},
        "turn_off": {"service": "turn_off", "label": "关机"},
    }},
    "cover": {"actions": {
        "open_cover": {"service": "open_cover", "label": "全开"},
        "close_cover": {"service": "close_cover", "label": "全关"},
        "stop_cover": {"service": "stop_cover", "label": "停止"},
        "set_cover_position": {"service": "set_cover_position", "label": "设置开合度", "params": {
            "position": _int(0, 100, "开合度(0-100)", required=True),
        }},
        "open_cover_tilt": {"service": "open_cover_tilt", "label": "翻转片全开"},
        "close_cover_tilt": {"service": "close_cover_tilt", "label": "翻转片全关"},
        "set_cover_tilt_position": {"service": "set_cover_tilt_position", "label": "设置翻转片角度", "params": {
            "tilt_position": _int(0, 100, "角度(0-100)", required=True),
        }},
    }},
    "fan": {"actions": {
        "turn_on": {"service": "turn_on", "label": "打开", "params": {
            "percentage": _int(0, 100, "风速百分比"),
            "preset_mode": _enum("preset_modes", "预设模式"),
        }},
        "turn_off": {"service": "turn_off", "label": "关闭"},
        "toggle": {"service": "toggle", "label": "翻转"},
        "set_percentage": {"service": "set_percentage", "label": "设置风速", "params": {
            "percentage": _int(0, 100, "风速百分比", required=True),
        }},
        "set_preset_mode": {"service": "set_preset_mode", "label": "设置预设", "params": {
            "preset_mode": _enum("preset_modes", "预设模式", required=True),
        }},
        "oscillate": {"service": "oscillate", "label": "摇头开关", "params": {
            "oscillating": _bool("是否摇头", required=True),
        }},
        "set_direction": {"service": "set_direction", "label": "设置转向", "params": {
            "direction": _fixed_enum(["forward", "reverse"], "方向", required=True),
        }},
    }},
    "humidifier": {"actions": {
        "turn_on": {"service": "turn_on", "label": "打开"},
        "turn_off": {"service": "turn_off", "label": "关闭"},
        "toggle": {"service": "toggle", "label": "翻转"},
        "set_humidity": {"service": "set_humidity", "label": "设置目标湿度", "params": {
            "humidity": _dyn_range("humidity", "目标湿度", required=True),
        }},
        "set_mode": {"service": "set_mode", "label": "设置模式", "params": {
            "mode": _enum("available_modes", "模式", required=True),
        }},
    }},
    "water_heater": {"actions": {
        "turn_on": {"service": "turn_on", "label": "打开"},
        "turn_off": {"service": "turn_off", "label": "关闭"},
        "set_temperature": {"service": "set_temperature", "label": "设置温度", "params": {
            "temperature": _dyn_range("temperature", "目标温度", required=True),
        }},
        "set_operation_mode": {"service": "set_operation_mode", "label": "设置模式", "params": {
            "operation_mode": _enum("operation_list", "模式", required=True),
        }},
    }},
    "media_player": {"actions": {
        "turn_on": {"service": "turn_on", "label": "开机"},
        "turn_off": {"service": "turn_off", "label": "关机"},
        "volume_set": {"service": "volume_set", "label": "设置音量", "params": {
            "volume_level": _dyn_range("volume", "音量(0-1)", required=True),
        }},
        "volume_up": {"service": "volume_up", "label": "音量+"},
        "volume_down": {"service": "volume_down", "label": "音量-"},
        "volume_mute": {"service": "volume_mute", "label": "静音开关", "params": {
            "is_volume_muted": _bool("是否静音", required=True),
        }},
        "media_play": {"service": "media_play", "label": "播放"},
        "media_pause": {"service": "media_pause", "label": "暂停"},
        "media_stop": {"service": "media_stop", "label": "停止"},
        "media_next_track": {"service": "media_next_track", "label": "下一曲"},
        "media_previous_track": {"service": "media_previous_track", "label": "上一曲"},
        "select_source": {"service": "select_source", "label": "切换输入源", "params": {
            "source": _enum("source_list", "输入源", required=True),
        }},
        "select_sound_mode": {"service": "select_sound_mode", "label": "切换音效模式", "params": {
            "sound_mode": _enum("sound_mode_list", "音效模式", required=True),
        }},
    }},
    "vacuum": {"actions": {
        "start": {"service": "start", "label": "开始清扫"},
        "pause": {"service": "pause", "label": "暂停"},
        "stop": {"service": "stop", "label": "停止"},
        "return_to_base": {"service": "return_to_base", "label": "回充"},
        "locate": {"service": "locate", "label": "定位"},
        "set_fan_speed": {"service": "set_fan_speed", "label": "设置吸力", "params": {
            "fan_speed": _enum("fan_speed_list", "吸力档位", required=True),
        }},
    }},
    "number": {"actions": {
        "set_value": {"service": "set_value", "label": "设置数值", "params": {
            "value": _dyn_range("number", "数值", required=True),
        }},
    }},
    "input_number": {"actions": {
        "set_value": {"service": "set_value", "label": "设置数值", "params": {
            "value": _dyn_range("number", "数值", required=True),
        }},
    }},
    "select": {"actions": {
        "select_option": {"service": "select_option", "label": "选择选项", "params": {
            "option": _enum("options", "选项", required=True),
        }},
    }},
    "input_select": {"actions": {
        "select_option": {"service": "select_option", "label": "选择选项", "params": {
            "option": _enum("options", "选项", required=True),
        }},
    }},
    "input_boolean": {"actions": {
        "turn_on": {"service": "turn_on", "label": "打开"},
        "turn_off": {"service": "turn_off", "label": "关闭"},
        "toggle": {"service": "toggle", "label": "翻转"},
    }},
    "text": {"actions": {
        "set_value": {"service": "set_value", "label": "写入文本", "params": {
            "value": _str("文本内容", required=True, max_len=255),
        }},
    }},
    "input_text": {"actions": {
        "set_value": {"service": "set_value", "label": "写入文本", "params": {
            "value": _str("文本内容", required=True, max_len=255),
        }},
    }},
    "counter": {"actions": {
        "increment": {"service": "increment", "label": "加一"},
        "decrement": {"service": "decrement", "label": "减一"},
        "reset": {"service": "reset", "label": "重置"},
    }},
    "timer": {"actions": {
        "start": {"service": "start", "label": "开始计时"},
        "pause": {"service": "pause", "label": "暂停"},
        "cancel": {"service": "cancel", "label": "取消"},
        "finish": {"service": "finish", "label": "结束"},
    }},
    "button": {"actions": {
        "press": {"service": "press", "label": "按下"},
    }},
    "input_button": {"actions": {
        "press": {"service": "press", "label": "按下"},
    }},
    "scene": {"actions": {
        "turn_on": {"service": "turn_on", "label": "激活场景", "params": {
            "transition": _float(0, 300, label="过渡秒数"),
        }},
    }},
    "script": {"actions": {
        "turn_on": {"service": "turn_on", "label": "执行脚本"},
        "turn_off": {"service": "turn_off", "label": "停止脚本"},
    }},
    "automation": {"actions": {
        "turn_on": {"service": "turn_on", "label": "启用自动化"},
        "turn_off": {"service": "turn_off", "label": "停用自动化"},
        "trigger": {"service": "trigger", "label": "手动触发"},
    }},
    "siren": {"high_risk": True, "actions": {
        "turn_on": {"service": "turn_on", "label": "鸣响", "params": {
            "tone": _enum("available_tones", "音调"),
            "duration": _int(1, 3600, "持续秒数"),
            "volume_level": _dyn_range("volume", "音量(0-1)"),
        }},
        "turn_off": {"service": "turn_off", "label": "停止鸣响"},
    }},
    "lock": {"high_risk": True, "actions": {
        "lock": {"service": "lock", "label": "上锁"},
        "unlock": {"service": "unlock", "label": "开锁", "params": {
            "code": _str("密码(如设备需要)"),
        }},
        "open": {"service": "open", "label": "开门（高风险）"},
    }},
    "alarm_control_panel": {"high_risk": True, "actions": {
        "alarm_arm_home": {"service": "alarm_arm_home", "label": "在家布防", "params": {
            "code": _str("密码(如设备需要)"),
        }},
        "alarm_arm_away": {"service": "alarm_arm_away", "label": "离家布防", "params": {
            "code": _str("密码(如设备需要)"),
        }},
        "alarm_arm_night": {"service": "alarm_arm_night", "label": "夜间布防", "params": {
            "code": _str("密码(如设备需要)"),
        }},
        "alarm_disarm": {"service": "alarm_disarm", "label": "撤防", "params": {
            "code": _str("密码(如设备需要)"),
        }},
    }},
    "valve": {"actions": {
        "open_valve": {"service": "open_valve", "label": "打开阀门"},
        "close_valve": {"service": "close_valve", "label": "关闭阀门"},
        "stop_valve": {"service": "stop_valve", "label": "停止"},
        "set_valve_position": {"service": "set_valve_position", "label": "设置开度", "params": {
            "position": _int(0, 100, "开度(0-100)", required=True),
        }},
    }},
    "lawn_mower": {"actions": {
        "start_mowing": {"service": "start_mowing", "label": "开始割草"},
        "pause": {"service": "pause", "label": "暂停"},
        "dock": {"service": "dock", "label": "回桩"},
    }},
    "todo": {"actions": {
        "add_item": {"service": "add_item", "label": "添加条目", "params": {
            "item": _str("条目内容", required=True),
        }},
        "update_item": {"service": "update_item", "label": "更新条目", "params": {
            "item": _str("原条目", required=True),
            "rename": _str("新内容"),
            "status": _fixed_enum(
                ["needs_action", "completed"], "状态"),
        }},
        "remove_item": {"service": "remove_item", "label": "删除条目", "params": {
            "item": _str("条目内容", required=True),
        }},
    }},
}

# 只读域：明确不支持控制（用于给出更友好的提示）
READ_ONLY_DOMAINS: frozenset[str] = frozenset({
    "sensor", "binary_sensor", "device_tracker", "person", "sun", "weather",
    "image", "update", "event", "geo_location", "calendar", "camera",
    "tag", "zone", "air_quality", "stt", "tts", "wake_word",
})

# raw 模式永久禁止的域（这些域没有实体语义或具备系统级破坏力）
RAW_BLOCKED_DOMAINS: frozenset[str] = frozenset({
    "shell_command", "python_script", "hassio", "homeassistant", "recorder",
    "logger", "system_log", "frontend", "lovelace", "auth", "onboarding",
    "backup", "hardware", "supervisor", "config", "cloud", "rest_command",
    "command_line", "notify", "persistent_notification", "assist_pipeline",
    "conversation", "analytics", "mobile_app", "device_tracker",
})

# 动态枚举：key 即实体属性名
_DYNAMIC_ENUM_ATTRS: frozenset[str] = frozenset({
    "hvac_modes", "fan_modes", "swing_modes", "swing_horizontal_modes",
    "preset_modes", "effect_list", "options", "source_list",
    "sound_mode_list", "fan_speed_list", "operation_list", "available_modes",
    "available_tones", "mode_list",
})

# cover 动作 → supported_features 位
_COVER_FEATURE_BITS: dict[str, int] = {
    "open_cover": 1,
    "close_cover": 2,
    "set_cover_position": 4,
    "stop_cover": 8,
    "open_cover_tilt": 16,
    "close_cover_tilt": 32,
    "stop_cover_tilt": 64,
    "set_cover_tilt_position": 128,
}


# =========================================================================== #
#  异常                                                                        #
# =========================================================================== #
class ControlError(Exception):
    """控制请求被拒绝或执行失败。code 为建议的 HTTP 状态码。"""

    def __init__(self, message: str, code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


# =========================================================================== #
#  参数 schema 动态展开                                                          #
# =========================================================================== #
def _resolve_dynamic_range(kind: str, attrs: dict) -> dict:
    """把 dynamic_range 展开为具体的 min / max / step。"""
    if kind == "temperature":
        return {
            "min": _num(attrs.get("min_temp"), 5),
            "max": _num(attrs.get("max_temp"), 35),
            "step": _num(attrs.get("target_temp_step"), 0.5),
        }
    if kind == "humidity":
        return {
            "min": _num(attrs.get("min_humidity"), 0),
            "max": _num(attrs.get("max_humidity"), 100),
            "step": 1,
        }
    if kind == "number":
        return {
            "min": _num(attrs.get("min"), 0),
            "max": _num(attrs.get("max"), 100),
            "step": _num(attrs.get("step"), 1),
        }
    if kind == "volume":
        return {"min": 0, "max": 1, "step": 0.01}
    return {}


def _num(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def resolve_param_spec(spec: dict, attrs: dict) -> dict | None:
    """展开参数 schema；若动态枚举无可用选项则返回 None（表示该实体不支持此参数）。"""
    out = {k: v for k, v in spec.items() if k not in ("dynamic", "dynamic_range")}
    dynamic = spec.get("dynamic")
    if dynamic:
        options = attrs.get(dynamic)
        if not isinstance(options, (list, tuple)) or not options:
            return None
        out["options"] = [str(o) for o in options]
        out.pop("options_hint", None)
    dyn_range = spec.get("dynamic_range")
    if dyn_range:
        out.update(_resolve_dynamic_range(dyn_range, attrs))
    return out


def _action_supported(domain: str, action: str, attrs: dict) -> bool:
    """按实体实际能力过滤动作（目前处理 cover 的 supported_features）。"""
    if domain == "cover" and action in _COVER_FEATURE_BITS:
        sf = attrs.get("supported_features")
        if isinstance(sf, int):
            return bool(sf & _COVER_FEATURE_BITS[action])
    return True


# =========================================================================== #
#  能力发现                                                                     #
# =========================================================================== #
def build_capabilities(hass: HomeAssistant, entity_id: str,
                       allowed_actions: Any = None) -> dict:
    """返回某实体的可控制能力（动作 + 参数 schema），供 UI / 第三方系统使用。

    allowed_actions 传入时按其过滤动作清单（None = 不过滤，用于配置向导展示全量动作）；
    `["*"]` 表示全量，`[]` 表示一个都不允许（结果 actions 为空）。
    """
    domain = entity_id.split(".", 1)[0]
    state_obj = hass.states.get(entity_id)
    attrs: dict = dict(state_obj.attributes) if state_obj else {}
    allowed = _normalize_allowed(allowed_actions)

    entry = ACTION_CATALOG.get(domain)
    if not entry:
        reason = ("只读实体，不支持控制" if domain in READ_ONLY_DOMAINS
                  else f"实体域 {domain} 暂未纳入控制白名单")
        return {
            "entity_id": entity_id,
            "domain": domain,
            "state": state_obj.state if state_obj else None,
            "controllable": False,
            "high_risk": False,
            "error": reason,
            "actions": [],
            "allowed_actions": [],
        }

    actions: list[dict] = []
    for name, adef in entry["actions"].items():
        if allowed is not None and name not in allowed:
            continue
        if not _action_supported(domain, name, attrs):
            continue
        params: dict[str, dict] = {}
        for pname, pspec in (adef.get("params") or {}).items():
            resolved = resolve_param_spec(pspec, attrs)
            if resolved is None:
                continue
            params[pname] = resolved
        actions.append({
            "action": name,
            "label": adef.get("label", name),
            "service": f"{domain}.{adef['service']}",
            "params": params,
            "high_risk": bool(adef.get("high_risk")),
        })

    return {
        "entity_id": entity_id,
        "domain": domain,
        "state": state_obj.state if state_obj else None,
        "controllable": bool(actions),
        "high_risk": bool(entry.get("high_risk")),
        "error": None if actions else (
            "该 token 未授权任何控制动作" if allowed == [] else "该实体当前没有可用的控制动作"),
        "actions": actions,
        "allowed_actions": [a["action"] for a in actions],
    }


def catalog_overview() -> list[dict]:
    """返回整个动作目录概览（供前端展示可用域）。"""
    out = []
    for domain, entry in ACTION_CATALOG.items():
        out.append({
            "domain": domain,
            "high_risk": bool(entry.get("high_risk")),
            "actions": [
                {"action": n, "label": d.get("label", n), "service": f"{domain}.{d['service']}"}
                for n, d in entry["actions"].items()
            ],
        })
    return out


# =========================================================================== #
#  参数校验                                                                     #
# =========================================================================== #
_TRUE_TOKENS = {"true", "1", "on", "yes", "y"}
_FALSE_TOKENS = {"false", "0", "off", "no", "n"}


def _coerce_value(name: str, value: Any, spec: dict, lax_bounds: bool = False) -> Any:
    """按 schema 校验并转换参数值；不合法抛 ControlError。"""
    ptype = spec.get("type", "str")
    label = spec.get("label") or name

    if ptype in ("int", "float"):
        if isinstance(value, bool):
            raise ControlError(f"参数 {label} 需要数字", 400)
        try:
            num = float(value)
        except (TypeError, ValueError):
            raise ControlError(f"参数 {label} 需要数字，收到 {value!r}", 400)
        lo, hi = spec.get("min"), spec.get("max")
        if not lax_bounds:
            if lo is not None and num < lo:
                raise ControlError(f"参数 {label} 不能小于 {lo}", 400)
            if hi is not None and num > hi:
                raise ControlError(f"参数 {label} 不能大于 {hi}", 400)
        if ptype == "int":
            return int(round(num))
        return num

    if ptype == "bool":
        if isinstance(value, bool):
            return value
        token = str(value).strip().lower()
        if token in _TRUE_TOKENS:
            return True
        if token in _FALSE_TOKENS:
            return False
        raise ControlError(f"参数 {label} 需要布尔值", 400)

    if ptype == "enum":
        options = [str(o) for o in (spec.get("options") or [])]
        token = str(value)
        if token not in options:
            raise ControlError(
                f"参数 {label} 取值非法（可选：{' / '.join(options) or '无'}）", 400)
        return token

    if ptype == "rgb":
        if isinstance(value, str):
            value = [p.strip() for p in value.replace("，", ",").split(",")]
        if not isinstance(value, (list, tuple)) or len(value) != 3:
            raise ControlError(f"参数 {label} 需要 [r,g,b] 三个整数", 400)
        out = []
        for item in value:
            try:
                c = int(item)
            except (TypeError, ValueError):
                raise ControlError(f"参数 {label} 的 RGB 分量必须是整数", 400)
            if not 0 <= c <= 255:
                raise ControlError(f"参数 {label} 的 RGB 分量必须在 0-255", 400)
            out.append(c)
        return out

    # str
    text = "" if value is None else str(value)
    max_len = spec.get("max_len")
    if max_len is not None and len(text) > int(max_len):
        raise ControlError(f"参数 {label} 长度不能超过 {max_len}", 400)
    return text


def _clamp_value(value: Any, lo: Any, hi: Any) -> Any:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return value
    try:
        if lo is not None and value < lo:
            return lo
        if hi is not None and value > hi:
            return hi
    except TypeError:
        return value
    return value


# =========================================================================== #
#  请求解析                                                                     #
# =========================================================================== #
def _normalize_allowed(allowed_actions: Any) -> list[str] | None:
    """归一化 allowed_actions。

    返回值语义：
      - None  → 不做动作过滤（= 目录内全部动作；仅当字段缺失或显式给出 "*" 时）
      - []    → 不允许任何动作（控制实际不可用，最安全）
      - [...] → 仅允许列出的动作

    支持 ["turn_on", ...] / [{"action": "turn_on"}, ...] / "turn_on,turn_off"。
    """
    if allowed_actions is None:
        return None
    if isinstance(allowed_actions, str):
        text = allowed_actions.strip()
        if not text:
            return []
        try:
            import json as _json
            allowed_actions = _json.loads(text)
        except Exception:
            allowed_actions = [x.strip() for x in text.split(",")]
    if not isinstance(allowed_actions, (list, tuple)):
        return None
    out: list[str] = []
    for item in allowed_actions:
        if isinstance(item, str):
            token = item.strip()
            if token == "*":
                return None
            if token:
                out.append(token)
        elif isinstance(item, dict):
            act = str(item.get("action", "")).strip()
            if act == "*":
                return None
            if act:
                out.append(act)
    return out


def build_service_call(
    entity_id: str,
    action: str,
    provided: Any,
    constraints: dict | None,
    allowed_actions: list[str] | None,
    attrs: dict,
) -> tuple[str, dict]:
    """把 action + params 解析为 (service, data)；非法请求抛 ControlError。"""
    domain = entity_id.split(".", 1)[0]
    entry = ACTION_CATALOG.get(domain)
    if not entry:
        raise ControlError(f"实体域 {domain} 不支持控制", 400)

    # 自包含归一化：允许调用方直接传 ["*"] / JSON 字符串 / dict 列表
    allowed_actions = _normalize_allowed(allowed_actions)
    if allowed_actions is not None and not allowed_actions:
        raise ControlError("该 token 未授权任何控制动作（允许动作清单为空）", 403)
    if isinstance(constraints, str):
        import json as _json
        try:
            constraints = _json.loads(constraints or "{}")
        except Exception:
            constraints = {}

    adef = entry["actions"].get(action)
    if adef is None:
        raise ControlError(f"未知动作: {action}", 400)
    if allowed_actions is not None and action not in allowed_actions:
        raise ControlError(f"动作 {action} 未被授权（该 token 的允许动作清单中没有它）", 403)
    if not _action_supported(domain, action, attrs):
        raise ControlError(f"实体 {entity_id} 当前不支持动作 {action}", 400)

    provided = provided if isinstance(provided, dict) else {}
    specs: dict[str, dict] = {}
    for pname, pspec in (adef.get("params") or {}).items():
        resolved = resolve_param_spec(pspec, attrs)
        if resolved is not None:
            specs[pname] = resolved

    unknown = [k for k in provided if k not in specs]
    if unknown:
        raise ControlError(f"不支持的参数: {', '.join(unknown)}", 400)

    rules = constraints.get(action) if isinstance(constraints, dict) else None
    rules = rules if isinstance(rules, dict) else {}

    data: dict[str, Any] = {}
    for pname, spec in specs.items():
        rule = rules.get(pname)
        rule = rule if isinstance(rule, dict) else {}
        lock = str(rule.get("lock", "")).strip().lower()
        label = spec.get("label") or pname

        # 固定值：忽略调用方传参
        if lock == "value":
            data[pname] = _coerce_value(pname, rule.get("value"), spec)
            continue

        if pname not in provided:
            if spec.get("required"):
                raise ControlError(f"缺少必填参数: {label}", 400)
            continue

        if lock == "range":
            # 限幅：先用宽松模式转类型，再裁剪到锁定区间
            value = _coerce_value(pname, provided[pname], spec, lax_bounds=True)
            lo = rule.get("min", spec.get("min"))
            hi = rule.get("max", spec.get("max"))
            data[pname] = _clamp_value(value, lo, hi)
        else:
            data[pname] = _coerce_value(pname, provided[pname], spec)

    data["entity_id"] = entity_id
    return f"{domain}.{adef['service']}", data


def build_raw_service_call(
    entity_id: str,
    domain: str,
    service: str,
    data: Any,
) -> tuple[str, dict]:
    """raw 模式：按 domain.service 直调，但强制 domain 与实体域一致。"""
    entity_domain = entity_id.split(".", 1)[0]
    domain = (domain or "").strip().lower()
    service = (service or "").strip().lower()
    if not domain or not service:
        raise ControlError("raw 模式需要 service 参数（形如 climate.set_temperature）", 400)
    if domain in RAW_BLOCKED_DOMAINS:
        raise ControlError(f"raw 模式禁止调用 {domain} 域", 403)
    if domain != entity_domain:
        raise ControlError(
            f"raw 模式只允许调用实体自身域（{entity_domain}），收到 {domain}", 403)
    if not _ENTITY_ID_RE.match(entity_id):
        raise ControlError(f"实体 ID 非法: {entity_id}", 400)
    payload = dict(data) if isinstance(data, dict) else {}
    payload["entity_id"] = entity_id
    return f"{domain}.{service}", payload


# =========================================================================== #
#  限流                                                                        #
# =========================================================================== #
_RATE_BUCKETS: dict[str, list[float]] = {}
_rate_sweep_at = 0.0


def check_rate_limit(key: str, limit_per_min: int) -> int:
    """返回剩余配额；-1 表示不限流。超限则抛 ControlError(429)。"""
    if not limit_per_min or limit_per_min <= 0:
        return -1
    now = time.time()
    window_start = now - 60.0
    bucket = [t for t in _RATE_BUCKETS.get(key, []) if t > window_start]
    if len(bucket) >= limit_per_min:
        _RATE_BUCKETS[key] = bucket
        raise ControlError(f"请求过于频繁（限制 {limit_per_min} 次/分钟）", 429)
    bucket.append(now)
    _RATE_BUCKETS[key] = bucket
    _sweep_rate_buckets(now, window_start)
    return max(0, limit_per_min - len(bucket))


def _sweep_rate_buckets(now: float, window_start: float) -> None:
    """偶尔清理过期桶，避免 token 数量增长导致内存缓慢膨胀。"""
    global _rate_sweep_at
    if now - _rate_sweep_at < 600:
        return
    _rate_sweep_at = now
    for k in [k for k, v in _RATE_BUCKETS.items() if not any(t > window_start for t in v)]:
        _RATE_BUCKETS.pop(k, None)


def reset_rate_limit(key: str) -> None:
    _RATE_BUCKETS.pop(key, None)


# =========================================================================== #
#  审计日志                                                                     #
# =========================================================================== #
CONTROL_LOG_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS control_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    target_id   INTEGER NOT NULL DEFAULT 0,
    entity_id   TEXT NOT NULL DEFAULT '',
    action      TEXT NOT NULL DEFAULT '',
    service     TEXT NOT NULL DEFAULT '',
    params      TEXT NOT NULL DEFAULT '{}',
    success     INTEGER NOT NULL DEFAULT 0,
    error       TEXT NOT NULL DEFAULT '',
    client_ip   TEXT NOT NULL DEFAULT '',
    source      TEXT NOT NULL DEFAULT 'api',
    created_at  TEXT NOT NULL DEFAULT ''
)
"""

_control_log_writes = 0


def _now_iso() -> str:
    return (datetime.utcnow() + timedelta(hours=DEFAULT_TIMEZONE)).isoformat(timespec="seconds")


def write_control_log(
    db_path: str,
    target_id: int,
    entity_id: str,
    action: str,
    service: str,
    params: dict,
    success: bool,
    error: str = "",
    client_ip: str = "",
    source: str = "api",
) -> None:
    """写入一条控制审计日志（阻塞，调用方应放入线程池）。"""
    global _control_log_writes
    import json as _json
    try:
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(CONTROL_LOG_TABLE_SQL)
            conn.execute(
                "INSERT INTO control_logs "
                "(target_id, entity_id, action, service, params, success, error, client_ip, source, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    int(target_id or 0), entity_id, action, service,
                    _json.dumps(params or {}, ensure_ascii=False, default=str),
                    1 if success else 0, (error or "")[:500], client_ip or "", source or "api",
                    _now_iso(),
                ),
            )
            _control_log_writes += 1
            if _control_log_writes % _CONTROL_LOG_CLEANUP_EVERY == 0:
                cutoff = (datetime.utcnow() + timedelta(hours=DEFAULT_TIMEZONE)
                          - timedelta(days=CONTROL_LOG_RETENTION_DAYS))
                conn.execute("DELETE FROM control_logs WHERE created_at < ?",
                             (cutoff.isoformat(timespec="seconds"),))
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:  # 审计失败不能影响控制结果
        _LOGGER.warning("[push_control] 审计日志写入失败 entity=%s: %s", entity_id, exc)


# =========================================================================== #
#  执行                                                                        #
# =========================================================================== #
async def execute_control(
    hass: HomeAssistant,
    target: dict,
    body: dict,
    client_ip: str = "",
) -> dict:
    """执行一次控制请求，返回给调用方的 JSON 数据结构。

    调用方需保证 target 已通过 control_token + enabled 校验。
    """
    import json as _json

    entity_id = str(target.get("entity_id", "")).strip()
    target_id = int(target.get("id") or 0)

    def _fail(message: str, code: int, service: str = "", params: dict | None = None,
              action: str = "") -> dict:
        return {
            "success": False,
            "error": message,
            "code": code,
            "entity_id": entity_id,
            "action": action,
            "service": service,
            "applied_params": params or {},
        }

    if not _ENTITY_ID_RE.match(entity_id):
        return _fail(f"实体 ID 非法: {entity_id}", 400)

    state_obj = hass.states.get(entity_id)
    attrs: dict = dict(state_obj.attributes) if state_obj else {}
    if state_obj is None:
        return _fail(f"实体 {entity_id} 不存在", 404)

    # ── 解析请求 ──
    raw_service = str(body.get("service", "")).strip()
    action = ""
    try:
        if raw_service:
            if not _truthy(target.get("allow_raw_service")):
                raise ControlError("该 token 未开启 raw 服务直传模式", 403)
            if "." in raw_service:
                raw_domain, raw_svc = raw_service.split(".", 1)
            else:
                raw_domain, raw_svc = str(body.get("domain", "")), raw_service
            service, data = build_raw_service_call(entity_id, raw_domain, raw_svc, body.get("data"))
            action = f"raw:{raw_domain}.{raw_svc}"
        else:
            action = str(body.get("action", "")).strip()
            if not action:
                raise ControlError("缺少 action 参数（或使用 service 进入 raw 模式）", 400)
            allowed = _normalize_allowed(target.get("allowed_actions"))
            try:
                constraints = _json.loads(target.get("param_constraints") or "{}")
            except Exception:
                constraints = {}
            service, data = build_service_call(
                entity_id, action, body.get("params"), constraints, allowed, attrs)
    except ControlError as err:
        return _fail(err.message, err.code, action=action)

    # ── 限流 ──
    try:
        limit = int(target.get("rate_limit_per_min") or 0)
        remaining = check_rate_limit(str(target.get("control_token") or target_id), limit)
    except (TypeError, ValueError):
        remaining = -1
    except ControlError as err:
        await _log_async(hass, entity_id, action, service, data, False,
                         err.message, client_ip, target_id)
        return _fail(err.message, err.code, service=service, params=data, action=action)

    # ── 调用服务 ──
    domain, svc = service.split(".", 1)
    if not hass.services.has_service(domain, svc):
        message = f"服务 {service} 不存在或当前未加载"
        await _log_async(hass, entity_id, action, service, data, False,
                         message, client_ip, target_id)
        return _fail(message, 400, service=service, params=data, action=action)

    prev_updated = state_obj.last_updated
    try:
        await hass.services.async_call(domain, svc, data, blocking=True)
    except Exception as exc:
        message = f"调用 {service} 失败: {exc}"
        _LOGGER.warning("[push_control] %s → %s 失败: %s", entity_id, service, exc)
        await _log_async(hass, entity_id, action, service, data, False,
                         message, client_ip, target_id)
        return _fail(message, 502, service=service, params=data, action=action)

    # ── 可选等待状态刷新 ──
    waited = False
    if _truthy(body.get("wait_state")):
        waited = await _wait_state_change(hass, entity_id, prev_updated)

    new_state = hass.states.get(entity_id)
    await _log_async(hass, entity_id, action, service, data, True,
                     "", client_ip, target_id)

    result = {
        "success": True,
        "message": f"{entity_id} 已执行 {action}",
        "entity_id": entity_id,
        "action": action,
        "service": service,
        "applied_params": {k: v for k, v in data.items() if k != "entity_id"},
        "dispatched": True,
        "state": new_state.state if new_state else None,
        "attributes_changed": bool(new_state and prev_updated != new_state.last_updated),
        "waited": waited,
    }
    if remaining >= 0:
        result["rate_limit_remaining"] = remaining
    return result


async def _wait_state_change(hass: HomeAssistant, entity_id: str, prev_updated) -> bool:
    """等待实体状态刷新，最长 _WAIT_STATE_TIMEOUT 秒。"""
    elapsed = 0.0
    while elapsed < _WAIT_STATE_TIMEOUT:
        await asyncio.sleep(_WAIT_STATE_INTERVAL)
        elapsed += _WAIT_STATE_INTERVAL
        cur = hass.states.get(entity_id)
        if cur and cur.last_updated != prev_updated:
            return True
    return False


async def _log_async(hass: HomeAssistant, entity_id: str, action: str,
                     service: str, data: dict, success: bool, error: str,
                     client_ip: str, target_id: int) -> None:
    db_path = hass.data.get(DOMAIN, {}).get("db_path")
    if not db_path:
        return
    params = {k: v for k, v in (data or {}).items() if k != "entity_id"}
    try:
        await hass.async_add_executor_job(
            write_control_log, db_path, target_id, entity_id, action, service,
            params, success, error, client_ip, "api",
        )
    except Exception:
        pass


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in _TRUE_TOKENS
    return False


def truthy(value: Any) -> bool:
    """对外暴露的布尔转换（供 http_api 复用）。"""
    return _truthy(value)


def normalize_allowed(allowed_actions: Any) -> list[str] | None:
    """对外暴露的 allowed_actions 归一化。"""
    return _normalize_allowed(allowed_actions)


# =========================================================================== #
#  启动日志                                                                     #
# =========================================================================== #
def log_catalog_summary() -> None:
    local_logger = get_logger()
    domains = len(ACTION_CATALOG)
    actions = sum(len(e["actions"]) for e in ACTION_CATALOG.values())
    text = f"[push_control] 控制动作目录已加载: {domains} 个域 / {actions} 个动作"
    _LOGGER.info(text)
    if local_logger:
        local_logger.info(text)
