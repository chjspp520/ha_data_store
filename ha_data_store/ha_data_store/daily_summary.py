"""今日家庭状态总结模块 — 纯规则模板。

职责：
  - build_daily_summary_sync(db_path, hass, date_str) → 聚合各历史表生成结构化事实（sections）
  - render_summary(data) → 将结构化事实渲染为精简中文段落（为 0 的项整节跳过）

输出约定：
  - 状态值（native_value）= 精简一段话
  - attributes.sections = 完整结构化分节（供自动化精确读取）
  - overall = normal | warning（存在异常提醒时为 warning）

设备节策略（精简+亮点，避免开关很多导致过长）：
  - 段落只出：共 N 台设备、总运行时长、总用电 + 用电环比 + 运行最久的 1~3 台亮点
  - 完整逐台明细放 sections.devices，不进段落
  - 单台运行时间 < 5 分钟的不单独提名

异常阈值（写死默认值）：
  - 高温 ≥ 30°C
  - 低温 ≤ 5°C
  - 单台连续运行 > 6 小时
  - 用电环比波动 > 20%（相对昨日）
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from .const import (
    DEFAULT_TIMEZONE,
    TABLE_DEVICE_HISTORY,
    TABLE_HEALTH_RECORDS,
    TABLE_REPORT_ENTITIES,
    METRIC_TEMPERATURE,
    METRIC_HUMIDITY,
    METRIC_PM25,
    METRIC_CO2,
    METRIC_POWER,
    get_env_table_name,
)

_LOGGER = logging.getLogger(__name__)

# 各指标的中文名
_METRIC_LABEL = {
    METRIC_TEMPERATURE: "温度",
    METRIC_HUMIDITY: "湿度",
    METRIC_PM25: "PM2.5",
    METRIC_CO2: "CO2",
}

# 异常阈值
TEMP_HIGH = 30.0     # 高温阈值 °C
TEMP_LOW = 5.0       # 低温阈值 °C
DEVICE_RUN_HOURS = 6.0  # 单台连续运行告警阈值（小时）
POWER_CHANGE_PCT = 20.0 # 用电环比告警阈值（%）
MIN_NOMINATE_SEC = 300  # 单台设备提名最短运行时长（秒）= 5 分钟
MAX_NOMINATE = 3        # 段落里最多提名的设备台数
MAX_ALERTS = 5          # 段落里最多显示的提醒条数
TOP_ENERGY_COUNT = 3    # 设备用电 TOP 展示台数
TOP_TIMES_COUNT = 3     # 设备开关频次 TOP 展示台数
MIN_TIMES_TOP = 2       # 设备开关频次上榜最少次数（避免开关1次的都上榜）

_XIAOAI_TABLE = "xiaoai_conversations"
_VACUUM_TABLE = "vacuum_history"
# 特殊实体：在 device_history 中按 name 识别
NAME_PRESENCE = "人在"     # 人在传感器
NAME_DOOR = "入户门"       # 入户门磁
LIGHT_KEYWORD = "灯"       # name 含"灯"视为灯光


def _today_local() -> str:
    """当前东八区本地日期 yyyy-mm-dd。"""
    return (datetime.utcnow() + timedelta(hours=DEFAULT_TIMEZONE)).strftime("%Y-%m-%d")


def _connect(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


# =========================================================================== #
#  环境聚合（env_* 表）                                                          #
# =========================================================================== #
def _agg_env(conn, metric: str, day: str) -> dict:
    """聚合单个环境指标今日的 最高/最低/平均 + 各房间均值。"""
    tbl = get_env_table_name(metric)
    try:
        rows = conn.execute(
            f"SELECT value, room, datetime FROM {tbl} "
            f"WHERE datetime LIKE ? AND value IS NOT NULL",
            (f"{day}%",),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    if not rows:
        return {}
    nums = []
    by_room: dict[str, list[float]] = {}
    for r in rows:
        try:
            v = float(r["value"])
        except (TypeError, ValueError):
            continue
        nums.append(v)
        room = (r["room"] or "").strip()
        if room:
            by_room.setdefault(room, []).append(v)
    if not nums:
        return {}
    return {
        "metric": metric,
        "label": _METRIC_LABEL.get(metric, metric),
        "min": round(min(nums), 1),
        "max": round(max(nums), 1),
        "avg": round(sum(nums) / len(nums), 1),
        "count": len(nums),
        "rooms": {room: round(sum(vs) / len(vs), 1) for room, vs in by_room.items()},
    }


# =========================================================================== #
#  设备聚合（device_history）                                                    #
# =========================================================================== #
def _agg_devices(conn, day: str) -> dict:
    """聚合今日设备：总台数/总时长/逐台明细（含单台用电，kWh）。

    单台设备用电（energy_consumed 单位 kWh）：
      - 已关闭：直接用 energy_consumed
      - 正在运行（energy_consumed 为空且 now_kwh 非空）：用 now_kwh - on_power（当前消耗）
    注意：总用电（家庭总表）由 env_power 表提供，这里不做累加。
    """
    rows = conn.execute(
        f"SELECT entity_id, name, room, duration, energy_consumed, "
        f"       on_power, now_kwh, "
        f"       (off_time != '' AND off_time IS NOT NULL) AS closed "
        f"FROM {TABLE_DEVICE_HISTORY} WHERE on_time LIKE ?",
        (f"{day}%",),
    ).fetchall()

    # 排除特殊状态实体（人在/入户门），它们不是耗电设备，不参与设备统计
    _SPECIAL_NAMES = {NAME_PRESENCE, NAME_DOOR}
    total_duration = 0.0  # 秒
    per_device: dict[str, dict] = {}
    for r in rows:
        eid = r["entity_id"]
        if (r["name"] or "").strip() in _SPECIAL_NAMES:
            continue  # 跳过"人在/入户门"
        dur = r["duration"] or 0
        total_duration += dur
        d = per_device.setdefault(
            eid,
            {"entity_id": eid, "name": r["name"] or eid, "room": r["room"] or "",
             "times": 0, "duration": 0.0, "energy": 0.0},
        )
        d["times"] += 1
        d["duration"] += dur
        # 单台设备用电量（kWh）
        eng = r["energy_consumed"]
        if eng is None and r["now_kwh"] is not None and r["on_power"] is not None:
            # 正在运行：now_kwh - on_power = 当前消耗
            eng = round(r["now_kwh"] - r["on_power"], 2)
        elif eng is None:
            eng = 0.0
        d["energy"] += eng

    device_list = sorted(
        per_device.values(),
        key=lambda d: d["duration"],
        reverse=True,
    )
    # 设备用电 TOP（按单台用电降序，只取用电 > 0 的）
    energy_top = sorted(
        (d for d in per_device.values() if d["energy"] > 0),
        key=lambda d: d["energy"],
        reverse=True,
    )[:TOP_ENERGY_COUNT]
    # 设备开关频次 TOP（按开关次数降序，只取次数 >= MIN_TIMES_TOP 的）
    times_top = sorted(
        (d for d in per_device.values() if d["times"] >= MIN_TIMES_TOP),
        key=lambda d: d["times"],
        reverse=True,
    )[:TOP_TIMES_COUNT]
    return {
        "device_count": len(device_list),
        "total_duration": round(total_duration, 0),   # 秒
        "devices": device_list,                        # 完整明细（按时长降序，含单台 energy kWh）
        "energy_top": energy_top,                      # 设备用电 TOP
        "times_top": times_top,                        # 设备开关频次 TOP
    }


def _agg_power(conn, day: str) -> dict:
    """聚合家庭总用电（env_power 表，value 为当日自增读数，单位 kWh）。

    - 按 entity_id 分组，各取当日最后一条 value（当日自增累计 = 当日消耗）
    - 多实体则求和；单个实体直接取该值
    返回：{"today": float(kWh) | None, "by_entity": {eid: kWh}}
    """
    tbl = get_env_table_name(METRIC_POWER)
    try:
        rows = conn.execute(
            f"SELECT entity_id, datetime, value FROM {tbl} "
            f"WHERE datetime LIKE ? AND value IS NOT NULL",
            (f"{day}%",),
        ).fetchall()
    except sqlite3.OperationalError:
        return {"today": None, "by_entity": {}}
    if not rows:
        return {"today": None, "by_entity": {}}

    # 每个 entity_id 取 datetime 最大的那一条（最后一条）
    last_by_entity: dict[str, tuple] = {}
    for r in rows:
        eid = r["entity_id"]
        cur = last_by_entity.get(eid)
        if cur is None or r["datetime"] > cur[0]:
            last_by_entity[eid] = (r["datetime"], r["value"])

    by_entity = {}
    for eid, (dt, val) in last_by_entity.items():
        try:
            by_entity[eid] = round(float(val), 2)
        except (TypeError, ValueError):
            continue
    if not by_entity:
        return {"today": None, "by_entity": {}}
    return {"today": round(sum(by_entity.values()), 2), "by_entity": by_entity}


# =========================================================================== #
#  离线实体统计（实时）                                                            #
# =========================================================================== #
def _agg_offline_entities(hass, conn) -> dict:
    """统计前端上报实体的当前离线情况（实时，非历史）。

    数据源：report_entities 表 + HA 实时 states。
    三态口径：offline = state == 'unavailable'；unknown 不算离线；其余在线。
    按 entity_id 去重后统计。
    返回：{"offline": int, "offline_entities": [name], "total": int}
    """
    if hass is None:
        return {"offline": 0, "offline_entities": [], "total": 0}
    try:
        rows = conn.execute(
            f"SELECT entity_id, name FROM {TABLE_REPORT_ENTITIES} "
        ).fetchall()
    except sqlite3.OperationalError:
        return {"offline": 0, "offline_entities": [], "total": 0}

    seen = set()
    offline_names = []
    for r in rows:
        eid = r["entity_id"]
        if eid in seen:
            continue
        seen.add(eid)
        st = hass.states.get(eid)
        state_val = st.state if st else "unavailable"
        if state_val == "unavailable":
            offline_names.append(r["name"] or eid)
    return {
        "offline": len(offline_names),
        "offline_entities": offline_names,
        "total": len(seen),
    }


# =========================================================================== #
#  人在 / 入户门 状态（实时）                                                        #
# =========================================================================== #
def _agg_presence(conn, now=None) -> dict:
    """根据 device_history 判断当前家中是否有人 + 入户门状态（实时）。

    - 人在传感器：name='人在'，on_time 非空 且 off_time 为空 → 该房间当前有人
    - 入户门：name='入户门' 最新一条（id 最大），on_time 非空 且 off_time 为空 → 门开着
    now：可选，当前 datetime（东八区）；用于计算门开时长，缺省用东八区现在。
    返回：{"rooms": [有人房间], "has_person": bool,
          "door": "open"|"closed"|"", "door_name": str,
          "door_open_minutes": int|None}  # 门开着时的持续分钟数
    """
    if now is None:
        now = datetime.utcnow() + timedelta(hours=DEFAULT_TIMEZONE)
    try:
        rows = conn.execute(
            f"SELECT id, name, room, on_time, off_time FROM {TABLE_DEVICE_HISTORY} "
            f"WHERE name IN (?, ?)",
            (NAME_PRESENCE, NAME_DOOR),
        ).fetchall()
    except sqlite3.OperationalError:
        return {"rooms": [], "has_person": False, "door": "", "door_name": "", "door_open_minutes": None}

    rooms = []
    door = ""
    door_name = ""
    door_open_minutes = None
    latest_door_id = -1
    for r in rows:
        name = r["name"]
        on_time = r["on_time"]
        off_time = r["off_time"]
        is_open = bool(on_time and not off_time)  # 当前处于"打开/有人"状态
        if name == NAME_PRESENCE and is_open:
            room = (r["room"] or "").strip()
            if room:
                rooms.append(room)
        elif name == NAME_DOOR:
            # 取最新一条（id 最大的那条，即当前状态）
            if r["id"] > latest_door_id:
                latest_door_id = r["id"]
                door = "open" if is_open else "closed"
                door_name = (r["room"] or "").strip() or NAME_DOOR
                # 门开着时计算持续分钟数
                if door == "open" and on_time:
                    try:
                        on_dt = datetime.strptime(on_time[:19], "%Y-%m-%d %H:%M:%S")
                        seconds = (now - on_dt).total_seconds()
                        door_open_minutes = int(seconds // 60) if seconds > 0 else 0
                    except (ValueError, TypeError):
                        door_open_minutes = None

    return {
        "rooms": sorted(set(rooms)),
        "has_person": bool(rooms),
        "door": door,
        "door_name": door_name,
        "door_open_minutes": door_open_minutes,
    }


# =========================================================================== #
#  灯光统计（实时）                                                                #
# =========================================================================== #
def _agg_lights(conn) -> dict:
    """统计当前开着的灯数量（device_history 中 name 含"灯"）。

    - 每盏灯按 entity_id 取最新一条（id 最大）
    - 最新一条 on_time 非空 且 off_time 为空 → 该灯当前开着
    返回：{"on_count": int, "on_lights": [灯名], "on_rooms": [房间去重], "total": int}
    """
    try:
        rows = conn.execute(
            f"SELECT id, entity_id, name, room, on_time, off_time FROM {TABLE_DEVICE_HISTORY} "
            f"WHERE name LIKE ?",
            (f"%{LIGHT_KEYWORD}%",),
        ).fetchall()
    except sqlite3.OperationalError:
        return {"on_count": 0, "on_lights": [], "on_rooms": [], "total": 0}
    if not rows:
        return {"on_count": 0, "on_lights": [], "on_rooms": [], "total": 0}

    # 每盏灯（entity_id）取最新一条
    latest_by_light: dict[str, dict] = {}
    for r in rows:
        eid = r["entity_id"]
        cur = latest_by_light.get(eid)
        if cur is None or r["id"] > cur["id"]:
            latest_by_light[eid] = dict(r)

    on_lights = []
    on_rooms = []
    for eid, rec in latest_by_light.items():
        on_time = rec["on_time"]
        off_time = rec["off_time"]
        if on_time and not off_time:  # 当前开着
            on_lights.append(rec["name"] or eid)
            room = (rec["room"] or "").strip()
            if room:
                on_rooms.append(room)
    return {
        "on_count": len(on_lights),
        "on_lights": on_lights,
        "on_rooms": sorted(set(on_rooms)),
        "total": len(latest_by_light),
    }


# =========================================================================== #
#  家庭事件（vacuum / health / xiaoai）                                          #
# =========================================================================== #
def _agg_vacuum(conn, day: str) -> dict:
    try:
        rows = conn.execute(
            f"SELECT DISTINCT datetime FROM {_VACUUM_TABLE} WHERE datetime LIKE ?",
            (f"{day}%",),
        ).fetchall()
    except sqlite3.OperationalError:
        return {"count": 0}
    return {"count": len(rows)}


def _agg_health(conn, day: str) -> dict:
    try:
        rows = conn.execute(
            f"SELECT name FROM {TABLE_HEALTH_RECORDS} WHERE date_time LIKE ?",
            (f"{day}%",),
        ).fetchall()
    except sqlite3.OperationalError:
        return {"count": 0, "names": []}
    names = sorted({r["name"] for r in rows if r["name"]})
    return {"count": len(rows), "names": names}


def _agg_xiaoai(conn, day: str) -> dict:
    try:
        rows = conn.execute(
            f"SELECT conv_time FROM {_XIAOAI_TABLE} WHERE conv_time LIKE ?",
            (f"{day}%",),
        ).fetchall()
    except sqlite3.OperationalError:
        return {"count": 0}
    if not rows:
        return {"count": 0}
    hours = []
    for r in rows:
        try:
            hours.append(int(r["conv_time"][11:13]))
        except Exception:
            pass
    if hours:
        lo = min(hours)
        hi = max(hours)
        span = f"{lo:02d}:00~{hi:02d}:00"
    else:
        span = ""
    return {"count": len(rows), "span": span}


# =========================================================================== #
#  异常提醒                                                                      #
# =========================================================================== #
def _build_alerts(env: dict, devices: dict, power_changed: float | None) -> list[str]:
    alerts: list[str] = []

    # 温度异常
    t = env.get("temperature")
    if t:
        if t["max"] >= TEMP_HIGH:
            alerts.append(f"今日最高温 {t['max']}°C 偏高")
        if t["min"] <= TEMP_LOW:
            alerts.append(f"今日最低温 {t['min']}°C 偏低")

    # 单台连续运行超阈值（用逐台时长判断）
    for d in devices.get("devices", []):
        if d["duration"] >= DEVICE_RUN_HOURS * 3600:
            name = d["name"]
            h = round(d["duration"] / 3600, 1)
            alerts.append(f"{name}运行超 {h} 小时")

    # 用电环比波动
    if power_changed is not None and abs(power_changed) > POWER_CHANGE_PCT:
        direction = "上升" if power_changed > 0 else "下降"
        alerts.append(f"今日用电较昨日{direction} {abs(power_changed):.0f}%")

    return alerts[:MAX_ALERTS]


# =========================================================================== #
#  主聚合函数                                                                    #
# =========================================================================== #
def build_daily_summary_sync(db_path: str, hass=None, date_str: str | None = None) -> dict:
    """聚合今日家庭状态。

    参数：
      db_path  数据库路径
      hass     可选，用于读取 report_entities 的实时在线/离线（本版未启用，预留）
      date_str 可选，yyyy-mm-dd；缺省为今天（东八区）
    返回：
      {date, sections, overall, summary, generated_at}
    """
    day = date_str or _today_local()

    conn = _connect(db_path)
    try:
        # 环境
        env = {}
        for m in (METRIC_TEMPERATURE, METRIC_HUMIDITY, METRIC_PM25, METRIC_CO2):
            agg = _agg_env(conn, m, day)
            if agg:
                env[m] = agg

        # 设备（单台明细，不含总用电累加）
        devices = _agg_devices(conn, day)

        # 家庭总用电（env_power 当日自增读数）
        power = _agg_power(conn, day)
        yesterday = (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
        power_yesterday = _agg_power(conn, yesterday)

        # 家庭事件
        vacuum = _agg_vacuum(conn, day)
        health = _agg_health(conn, day)
        xiaoai = _agg_xiaoai(conn, day)

        # 离线实体（实时，需读表 + hass states）
        offline = _agg_offline_entities(hass, conn)

        # 人在 / 入户门 状态（实时）
        presence = _agg_presence(conn)

        # 灯光统计（实时）
        lights = _agg_lights(conn)
    finally:
        conn.close()

    # 总用电 + 环比（相对昨日）
    today_energy = power["today"]            # kWh
    yesterday_energy = power_yesterday.get("today")  # kWh
    power_change = None
    if today_energy is not None and yesterday_energy and yesterday_energy > 0:
        power_change = (today_energy - yesterday_energy) / yesterday_energy * 100

    power_data = {
        "today": today_energy,
        "yesterday": yesterday_energy,
        "change_pct": (round(power_change, 1) if power_change is not None else None),
        "by_entity": power.get("by_entity", {}),
    }

    # 异常提醒（离线设备单独在 summary 体现，不并入 alerts）
    alerts = _build_alerts(env, devices, power_change)

    # 结构化分节（只保留有数据的）
    sections: dict[str, Any] = {}
    if env:
        sections["environment"] = env
    if today_energy is not None:
        sections["power"] = power_data
    if devices["device_count"] > 0:
        sections["devices"] = devices
    if vacuum.get("count"):
        sections["vacuum"] = vacuum
    if health.get("count"):
        sections["health"] = health
    if xiaoai.get("count"):
        sections["xiaoai"] = xiaoai
    sections["presence"] = presence
    sections["door"] = {"state": presence["door"], "name": presence["door_name"]}
    sections["lights"] = lights

    summary = render_summary(env, presence, lights, power_data, devices, vacuum, health, xiaoai, offline["offline"], alerts)
    status_value = render_status_value(presence, lights)
    alert_text = render_alert_text(alerts)

    return {
        "date": day,
        "sections": sections,
        "overall": "warning" if alerts else "normal",
        "alerts": alerts,
        "alert_text": alert_text,
        "summary": summary,
        "status_value": status_value,
        "offline": offline["offline"],
        "offline_entities": offline["offline_entities"],
        "presence": presence,
        "lights": lights,
        "generated_at": (datetime.utcnow() + timedelta(hours=DEFAULT_TIMEZONE)).strftime("%Y-%m-%d %H:%M:%S"),
    }


# =========================================================================== #
#  渲染：精简中文段落                                                            #
# =========================================================================== #
def _fmt_hours(seconds: float) -> str:
    """秒 → 人话时长。"""
    h = seconds / 3600
    if h >= 1:
        if h == int(h):
            return f"{int(h)} 小时"
        return f"{h:.1f} 小时"
    m = seconds / 60
    if m >= 1:
        return f"{m:.0f} 分钟"
    return f"{seconds:.0f} 秒"


def _fmt_energy(kwh: float) -> str:
    """kWh 文本（单位已是 kWh，不换算）。"""
    return f"{kwh:.2f}kWh"


def _fmt_open_duration(minutes: int) -> str:
    """开门/运行持续分钟 → 人话时长。"""
    if minutes < 60:
        return f"{int(minutes)} 分钟"
    h = int(minutes // 60)
    m = int(minutes % 60)
    if m == 0:
        return f"{h} 小时"
    return f"{h}小时{m}分钟"


def _render_env(env: dict) -> str:
    parts = []
    t = env.get("temperature")
    if t:
        parts.append(f"气温 {t['min']}~{t['max']}°C，平均 {t['avg']}°C")
        # 房间温差明显时补充房间明细（最多 3 个房间）
        rooms = t.get("rooms")
        if rooms and len(rooms) >= 2:
            vals = list(rooms.values())
            if (max(vals) - min(vals)) >= 2.0:
                top = sorted(rooms.items(), key=lambda kv: kv[1], reverse=True)[:3]
                parts.append("、".join(f"{name} {v}°C" for name, v in top))
    h = env.get("humidity")
    if h:
        parts.append(f"湿度 {h['avg']}%")
    pm = env.get("pm25")
    if pm:
        parts.append(f"PM2.5 平均 {pm['avg']}")
    co2 = env.get("co2")
    if co2:
        parts.append(f"CO2 平均 {co2['avg']}")
    return "，".join(parts)


def _render_devices(devices: dict) -> str:
    parts = [f"今日共 {devices['device_count']} 台设备启用"]
    if devices["total_duration"] > 0:
        parts.append(f"总运行 {_fmt_hours(devices['total_duration'])}")
    # 亮点：运行最久的 1~3 台（时长 ≥ 5 分钟）
    highlights = [d for d in devices["devices"] if d["duration"] >= MIN_NOMINATE_SEC][:MAX_NOMINATE]
    if highlights:
        names = [f"{d['name']} {_fmt_hours(d['duration'])}" for d in highlights]
        parts.append("其中 " + "、".join(names))
    return "；".join(parts)


def _render_device_rank(devices: dict) -> str:
    """渲染设备用电 TOP + 开关频次 TOP。"""
    parts = []
    energy_top = devices.get("energy_top", [])
    if energy_top:
        items = [f"{d['name']} {_fmt_energy(d['energy'])}" for d in energy_top]
        parts.append("用电最多 " + "、".join(items))
    times_top = devices.get("times_top", [])
    if times_top:
        items = []
        for d in times_top:
            room = (d.get("room") or "").strip()
            label = f"{room}{d['name']}" if room else d["name"]
            items.append(f"{label}{d['times']} 次")
        parts.append("开关最频繁 " + "、".join(items))
    return "；".join(parts)


def _render_power(power: dict) -> str:
    today = power.get("today")
    if today is None:
        return ""
    s = f"今日用电 {_fmt_energy(today)}"
    pc = power.get("change_pct")
    if pc is not None:
        direction = "上升" if pc > 0 else "下降"
        s += f" 较昨日{direction} {abs(pc):.0f}%"
    return s


def _render_presence(presence: dict) -> str:
    """渲染家中有人/无人 + 入户门状态。"""
    parts = []
    if presence.get("has_person"):
        rooms = presence.get("rooms", [])
        if rooms:
            parts.append("家中有人（" + "、".join(rooms) + "）")
        else:
            parts.append("家中有人")
    else:
        parts.append("家中无人")
    door = presence.get("door")
    if door == "open":
        minutes = presence.get("door_open_minutes")
        if minutes is not None:
            parts.append(f"入户门开{_fmt_open_duration(minutes)}")
        else:
            parts.append("入户门开着")
    elif door == "closed":
        parts.append("入户门关闭")
    return "；".join(parts)


def _render_lights(lights: dict) -> str:
    """渲染开着的灯数量（只在本有灯开启时显示），括号内显示开灯的房间（去重）。"""
    on_count = lights.get("on_count", 0)
    if on_count <= 0:
        return ""
    on_rooms = sorted(set(lights.get("on_rooms", [])))
    if on_rooms:
        # 显示开灯所在房间（唯一值，最多列前几个）
        shown = on_rooms[:3]
        more = f"等 {len(on_rooms)} 个房间" if len(on_rooms) > len(shown) else ""
        return f"开着 {on_count} 盏灯（{'、'.join(shown)}{more}）"
    return f"开着 {on_count} 盏灯"


def _render_events(vacuum: dict, health: dict, xiaoai: dict) -> str:
    parts = []
    if vacuum.get("count"):
        parts.append(f"扫地机今日工作 {vacuum['count']} 次")
    if health.get("count"):
        parts.append(f"健康记录 {health['count']} 条")
    if xiaoai.get("count"):
        s = f"小爱对话 {xiaoai['count']} 条"
        if xiaoai.get("span"):
            s += f"（集中在 {xiaoai['span']}）"
        parts.append(s)
    return "；".join(parts)


def render_summary(env: dict, presence: dict, lights: dict, power: dict, devices: dict, vacuum: dict, health: dict, xiaoai: dict, offline_count: int, alerts: list[str]) -> str:
    """渲染精简段落。所有为 0 / 空的节自动跳过。"""
    segs = []
    env_str = _render_env(env)
    if env_str:
        segs.append(env_str)
    # 家庭状态（有人/无人 + 入户门）
    presence_str = _render_presence(presence)
    if presence_str:
        segs.append(presence_str)
    # 灯光
    lights_str = _render_lights(lights)
    if lights_str:
        segs.append(lights_str)
    if devices.get("device_count", 0) > 0:
        segs.append(_render_devices(devices))
    rank_str = _render_device_rank(devices)
    if rank_str:
        segs.append(rank_str)
    power_str = _render_power(power)
    if power_str:
        segs.append(power_str)
    ev_str = _render_events(vacuum, health, xiaoai)
    if ev_str:
        segs.append(ev_str)
    # 离线设备（家庭健康状态，作为独立节）
    if offline_count > 0:
        segs.append(f"离线设备 {offline_count} 台")

    if not segs:
        return "今日家庭状态：暂无有效数据。"

    body = "今日家庭状态：" + "；".join(segs)
    # 提醒文字单独放 alert_text 字段，段落末尾不内嵌"提醒：..."
    if not alerts:
        body += "。整体状态正常。"
    return body


def render_alert_text(alerts: list[str]) -> str:
    """把提醒列表拼接为独立文字（供 alert_text 字段）。"""
    if not alerts:
        return ""
    return "提醒：" + "，".join(alerts)


def render_status_value(presence: dict, lights: dict) -> str:
    """渲染极简状态值（供 native_value，控制在 255 字符内）。

    只显示核心一句：家中有人/无人 + 灯 + 入户门状态。
    完整段落放 attributes.summary；提醒信息在 attributes.alerts/alert_text。
    """
    if not presence:
        return "暂无数据"
    parts = []
    if presence.get("has_person"):
        rooms = presence.get("rooms", [])
        if rooms:
            parts.append("家中有人（" + "、".join(rooms) + "）")
        else:
            parts.append("家中有人")
    else:
        parts.append("家中无人")
    on_count = lights.get("on_count", 0)
    if on_count > 0:
        parts.append(f"开着 {on_count} 盏灯")
    door = presence.get("door")
    if door == "open":
        minutes = presence.get("door_open_minutes")
        if minutes is not None:
            parts.append(f"入户门开{_fmt_open_duration(minutes)}")
        else:
            parts.append("入户门开着")
    elif door == "closed":
        parts.append("入户门关闭")
    # 状态值不显示提醒数（提醒信息在 alerts/alert_text 字段）
    return "，".join(parts)
