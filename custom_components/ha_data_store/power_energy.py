"""功率→用电计量模块。

用户在前台登记功率实体（+设备名称/房间/英文 ID 段/单位），本模块：
  1. 每 30 秒采样功率实体当前功率，按「时间差 × 功率」积分；
  2. 为每个登记自动注册 3 个固定 ID 传感器：
       sensor.ha_data_store_{id_slug}_daily_ele    今日累计 (kWh)
       sensor.ha_data_store_{id_slug}_monthly_ele  本月累计 (kWh，由日数据实时聚合)
       sensor.ha_data_store_{id_slug}_yearly_ele   本年累计 (kWh，由日数据实时聚合)
  3. 每天一条记录持久化到 power_energy_daily 表（每 60s 落盘），
     月/年通过当日行 + 内存累计实时算出，不单独建月/年表。
  4. 三个传感器状态属性附加历史列表（全量、不含 0 值占位）：
       daylist    [{day, usage}, ...]  — 每日用电（日用电实体）
       monthlist  [{month, usage}, ...] — 每月用电（月用电实体）
       yearlist   [{year, usage}, ...]  — 每年用电（年用电实体）
     列表在 60s 落盘时/跨日重置时从日表重建缓存，
     实体刷新时把「今天/本月/当年」实时值并入尾项（与 state 保持一致）。

积分口径：两次成功采样之间认为功率恒定；
空窗处理：unavailable/unknown 不累计并重置基线；采样间隔异常(>10min)丢弃该空窗。
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.event import async_track_time_interval

from .const import DOMAIN, TABLE_POWER_ENERGY_DAILY, TABLE_POWER_METER_CONFIGS

_LOGGER = logging.getLogger(__name__)

SAMPLE_SECONDS = 10        # 采样/积分间隔
PERSIST_SECONDS = 60       # 日表落盘间隔
GAP_DROP_SECONDS = 300     # 采样间隔超过此值丢弃该空窗（防 HA 停机误算）


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def ensure_tables(db_path: str) -> None:
    """确保功率计量所需的两张表存在（幂等，供模块独立运行时兜底）。"""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE_POWER_METER_CONFIGS} (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_id         TEXT NOT NULL UNIQUE,
                device_name       TEXT NOT NULL DEFAULT '',
                room              TEXT NOT NULL DEFAULT '',
                id_slug           TEXT NOT NULL DEFAULT '',
                unit              TEXT NOT NULL DEFAULT 'W',
                enabled           INTEGER NOT NULL DEFAULT 1,
                created_at        TEXT NOT NULL DEFAULT '',
                updated_at        TEXT NOT NULL DEFAULT ''
            )
            """,
        )
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE_POWER_ENERGY_DAILY} (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_id         TEXT NOT NULL,
                device_name       TEXT NOT NULL DEFAULT '',
                room              TEXT NOT NULL DEFAULT '',
                date              TEXT NOT NULL,
                kwh               REAL NOT NULL DEFAULT 0,
                updated_at        TEXT NOT NULL DEFAULT '',
                UNIQUE(entity_id, date)
            )
            """,
        )
        conn.commit()
    finally:
        conn.close()


def _open_db(db_path: str):
    """打开 DB 连接，若表缺失则自动建表（模块级兜底）。"""
    ensure_tables(db_path)
    return sqlite3.connect(db_path)


def _power_to_kw(value: float, unit: str | None) -> float:
    """按单位把功率数值换算为 kW。登记单位缺省按 W。"""
    u = (unit or "W").strip().lower()
    if "kw" in u or "kwh" in u:
        return value
    return value / 1000.0


def _meter_display_name(cfg: dict, suffix: str) -> str:
    """生成用电实体显示名称（不改变实体 ID）。

    格式：房间_设备_{日/月/年}用电量（下划线分隔）
    - room 与 device_name 相同时去重（如 全屋_全屋 → 全屋）；
    - 不相同或一方为空时只保留存在的部分；
    - 返回如 "客厅_空调_日用电量" / "全屋_日用电量" / "空调_月用电量"。
    """
    room = (cfg.get("room") or "").strip()
    dev = (cfg.get("device_name") or "").strip()
    if room and dev:
        base = room if room == dev else f"{room}_{dev}"
    elif dev:
        base = dev
    elif room:
        base = room
    else:
        base = cfg.get("id_slug") or cfg.get("entity_id") or "用电"
    return f"{base}_{suffix}"


def _merge_period_list(
    cache: list[dict[str, object]], field: str, key: str, value: float
) -> list[dict[str, object]]:
    """把当前周期（日/月/年）的实时累计值并入列表尾项。

    - value > 0：尾项若是 key 则覆盖 usage，否则尾部追加新条目；
    - value <= 0：当前周期暂无数据，尾项若恰为 key 则移除（无数据不占位）。

    返回全新列表，避免污染缓存与已写出的 state 属性。
    field/key 示例：日 ("day", "2026-09-03")、月 ("month", "2026-09")、年 ("year", "2026")。
    """
    items = [dict(x) for x in cache]
    if value > 0:
        if items and items[-1].get(field) == key:
            items[-1]["usage"] = round(value, 3)
        else:
            items.append({field: key, "usage": round(value, 3)})
    elif items and items[-1].get(field) == key:
        items.pop()
    return items


class PowerDailySensor(SensorEntity):
    """今日用电量传感器（固定 ID）。"""

    _attr_has_entity_name = True
    _attr_icon = "mdi:flash"
    _attr_native_unit_of_measurement = "kWh"
    _attr_should_poll = False

    def __init__(self, mgr, cfg: dict) -> None:
        super().__init__()
        self._mgr = mgr
        slug = cfg["id_slug"]
        self._cfg = cfg
        self._attr_unique_id = f"{DOMAIN}_power_meter_{slug}_daily"
        self._attr_name = _meter_display_name(cfg, "日用电量")
        self._attr_device_info = mgr.device_info(cfg)
        self._attr_native_value = 0.0
        self._attr_extra_state_attributes = {}
        # 固定实体 ID
        self.entity_id = f"sensor.ha_data_store_{slug}_daily_ele"

    def refresh_from_mgr(self) -> None:
        cfg = self._cfg
        eid = cfg["entity_id"]
        cur = self._mgr.state_of(eid)
        self._attr_native_value = round(cur["daily"], 3)
        self._attr_extra_state_attributes = {
            "room": cfg.get("room") or "",
            "device_name": cfg.get("device_name") or "",
            "source_entity": eid,
            "period": "daily",
            "date": cur["date"],
            "updated_at": _now_str(),
            "daylist": self._mgr.period_list_of(eid, "day", cur["date"], cur["daily"]),
        }
        self.async_write_ha_state()


class PowerMonthlySensor(SensorEntity):
    """本月用电量传感器（由日数据实时聚合）。"""

    _attr_has_entity_name = True
    _attr_icon = "mdi:calendar-month"
    _attr_native_unit_of_measurement = "kWh"
    _attr_should_poll = False

    def __init__(self, mgr, cfg: dict) -> None:
        super().__init__()
        self._mgr = mgr
        slug = cfg["id_slug"]
        self._cfg = cfg
        self._attr_unique_id = f"{DOMAIN}_power_meter_{slug}_monthly"
        self._attr_name = _meter_display_name(cfg, "月用电量")
        self._attr_device_info = mgr.device_info(cfg)
        self._attr_native_value = 0.0
        self._attr_extra_state_attributes = {}
        self.entity_id = f"sensor.ha_data_store_{slug}_monthly_ele"

    def refresh_from_mgr(self) -> None:
        cfg = self._cfg
        eid = cfg["entity_id"]
        cur = self._mgr.state_of(eid)
        self._attr_native_value = round(cur["monthly"], 3)
        self._attr_extra_state_attributes = {
            "room": cfg.get("room") or "",
            "device_name": cfg.get("device_name") or "",
            "source_entity": eid,
            "period": "monthly",
            "date": cur["date"],
            "updated_at": _now_str(),
            "monthlist": self._mgr.period_list_of(eid, "month", cur["date"][:7], cur["monthly"]),
        }
        self.async_write_ha_state()


class PowerYearlySensor(SensorEntity):
    """本年用电量传感器（由日数据实时聚合）。"""

    _attr_has_entity_name = True
    _attr_icon = "mdi:calendar"
    _attr_native_unit_of_measurement = "kWh"
    _attr_should_poll = False

    def __init__(self, mgr, cfg: dict) -> None:
        super().__init__()
        self._mgr = mgr
        slug = cfg["id_slug"]
        self._cfg = cfg
        self._attr_unique_id = f"{DOMAIN}_power_meter_{slug}_yearly"
        self._attr_name = _meter_display_name(cfg, "年用电量")
        self._attr_device_info = mgr.device_info(cfg)
        self._attr_native_value = 0.0
        self._attr_extra_state_attributes = {}
        self.entity_id = f"sensor.ha_data_store_{slug}_yearly_ele"

    def refresh_from_mgr(self) -> None:
        cfg = self._cfg
        eid = cfg["entity_id"]
        cur = self._mgr.state_of(eid)
        self._attr_native_value = round(cur["yearly"], 3)
        self._attr_extra_state_attributes = {
            "room": cfg.get("room") or "",
            "device_name": cfg.get("device_name") or "",
            "source_entity": eid,
            "period": "yearly",
            "date": cur["date"],
            "updated_at": _now_str(),
            "yearlist": self._mgr.period_list_of(eid, "year", cur["date"][:4], cur["yearly"]),
        }
        self.async_write_ha_state()


class PowerEnergyManager:
    """功率登记配置与用电计量管理。"""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._db_path = hass.data.get(DOMAIN, {}).get("db_path", "")
        # 运行态: entity_id(功率实体) -> 内部状态
        self._meters: dict[str, dict] = {}
        self._last_persist_mono = None
        self._unsub = None

    # ---------- 工具 ---------- #
    def device_info(self, cfg: dict) -> DeviceInfo:
        # 所有用电计量实体共用一个设备「用电计量」
        return DeviceInfo(
            identifiers={(DOMAIN, "power_meter_group")},
            name="用电计量",
            manufacturer="HA数据存储 — 用电计量",
        )

    # ---------- 配置 CRUD（DB 同步，供 executor 调用） ---------- #
    def load_configs(self) -> list[dict]:
        if not self._db_path:
            return []
        conn = _open_db(self._db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                f"SELECT * FROM {TABLE_POWER_METER_CONFIGS} ORDER BY id"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def save_config(self, cfg: dict) -> None:
        """新增/更新登记（UPSERT by entity_id）。"""
        conn = _open_db(self._db_path)
        now = _now_str()
        try:
            conn.execute(
                f"""INSERT OR REPLACE INTO {TABLE_POWER_METER_CONFIGS}
                    (id, entity_id, device_name, room, id_slug, unit, enabled, created_at, updated_at)
                    VALUES (
                      COALESCE((SELECT id FROM {TABLE_POWER_METER_CONFIGS} WHERE entity_id=?), NULL),
                      ?, ?, ?, ?, ?, ?, ?, ?)""",
                (cfg["entity_id"], cfg["entity_id"], cfg.get("device_name", ""),
                 cfg.get("room", ""), cfg.get("id_slug", ""), cfg.get("unit", "W"),
                 1 if cfg.get("enabled", True) else 0, now, now),
            )
            conn.commit()
        finally:
            conn.close()

    def remove_config(self, entity_id: str) -> None:
        conn = _open_db(self._db_path)
        try:
            conn.execute(f"DELETE FROM {TABLE_POWER_METER_CONFIGS} WHERE entity_id = ?", (entity_id,))
            conn.execute(f"DELETE FROM {TABLE_POWER_ENERGY_DAILY} WHERE entity_id = ?", (entity_id,))
            conn.commit()
        finally:
            conn.close()

    # ---------- 日表读写（DB 同步） ---------- #
    def _get_day_row(self, entity_id: str, date: str) -> float:
        if not self._db_path:
            return 0.0
        conn = _open_db(self._db_path)
        try:
            row = conn.execute(
                f"SELECT kwh FROM {TABLE_POWER_ENERGY_DAILY} WHERE entity_id = ? AND date = ?",
                (entity_id, date),
            ).fetchone()
            return float(row[0]) if row else 0.0
        finally:
            conn.close()

    def _persist_day(self, cfg: dict, date: str, kwh: float) -> None:
        """把某日累计写入日表（UPSERT）。kwh 保留 3 位小数。"""
        conn = _open_db(self._db_path)
        try:
            conn.execute(
                f"""INSERT INTO {TABLE_POWER_ENERGY_DAILY}
                    (entity_id, device_name, room, date, kwh, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(entity_id, date) DO UPDATE SET
                      kwh = excluded.kwh,
                      updated_at = excluded.updated_at""",
                (cfg["entity_id"], cfg.get("device_name", ""), cfg.get("room", ""),
                 date, round(kwh, 3), _now_str()),
            )
            conn.commit()
        finally:
            conn.close()

    def _sum_before(self, entity_id: str, prefix: str, date: str) -> float:
        """统计 date 之前（< date 且以 prefix 开头）的历史累计，用于月/年基数。"""
        if not self._db_path:
            return 0.0
        conn = _open_db(self._db_path)
        try:
            row = conn.execute(
                f"SELECT COALESCE(SUM(kwh), 0) FROM {TABLE_POWER_ENERGY_DAILY} "
                f"WHERE entity_id = ? AND date LIKE ? AND date < ?",
                (entity_id, prefix + "%", date),
            ).fetchone()
            return float(row[0]) if row else 0.0
        finally:
            conn.close()

    def recompute_bases(self, cfg: dict, date: str) -> None:
        """重算月/年基数（本月/本年 < date 的日数据总和）。"""
        st = self._meters.get(cfg["entity_id"])
        if not st:
            return
        month_prefix = date[:7]
        year_prefix = date[:4]
        st["month_base"] = self._sum_before(cfg["entity_id"], month_prefix, date)
        st["year_base"] = self._sum_before(cfg["entity_id"], year_prefix, date)

    # ---------- 日/月/年列表缓存 ---------- #
    def _rebuild_lists(self, entity_id: str, st: dict) -> None:
        """从日表重建日/月/年列表缓存（仅计 kwh>0 的行，无数据日期不占位）。

        在 60s 落盘与跨日重置后调用（executor 线程）；缓存缺失时
        period_list_of 会在实体刷新时懒重建兜底（覆盖重启后首个刷新）。
        """
        day_items: list[dict] = []
        month_acc: dict[str, float] = {}
        year_acc: dict[str, float] = {}
        if self._db_path:
            conn = _open_db(self._db_path)
            try:
                rows = conn.execute(
                    f"SELECT date, kwh FROM {TABLE_POWER_ENERGY_DAILY} "
                    "WHERE entity_id = ? AND kwh > 0 ORDER BY date",
                    (entity_id,),
                ).fetchall()
            finally:
                conn.close()
            for date, kwh in rows:
                v = float(kwh)
                day_items.append({"day": date, "usage": round(v, 3)})
                m = date[:7]
                month_acc[m] = month_acc.get(m, 0.0) + v
                y = date[:4]
                year_acc[y] = year_acc.get(y, 0.0) + v
        st["_lists"] = {
            "day": day_items,
            "month": [{"month": m, "usage": round(v, 3)}
                      for m, v in sorted(month_acc.items())],
            "year": [{"year": y, "usage": round(v, 3)}
                     for y, v in sorted(year_acc.items())],
        }

    def period_list_of(
        self, entity_id: str, field: str, key: str, value: float
    ) -> list[dict[str, object]]:
        """返回某实体的日/月/年列表：缓存 + 当前周期实时值并入尾项。"""
        st = self._meters.get(entity_id)
        if not st:
            return []
        if "_lists" not in st:
            self._rebuild_lists(entity_id, st)
        return _merge_period_list(st["_lists"].get(field, []), field, key, value)

    # ---------- 运行时状态 ---------- #
    def state_of(self, entity_id: str) -> dict:
        """返回某功率实体当前累计（daily 内存实时 / monthly / yearly 由基数+实时算）。"""
        st = self._meters.get(entity_id)
        if not st:
            return {"date": _today_str(), "daily": 0.0, "monthly": 0.0, "yearly": 0.0}
        return {
            "date": st["date"],
            "daily": st["cur"],
            "monthly": st["month_base"] + st["cur"],
            "yearly": st["year_base"] + st["cur"],
        }

    def _new_meter_state(self, cfg: dict) -> dict:
        date = _today_str()
        base = self._get_day_row(cfg["entity_id"], date)
        st = {
            "date": date,
            "cur": base,          # 今日累计（从日表上次落盘恢复）
            "month_base": 0.0,
            "year_base": 0.0,
            "last_ts": None,
            "last_power_kw": None,
            "last_persist": None,
            "sensors": {},
        }
        self._meters[cfg["entity_id"]] = st
        self.recompute_bases(cfg, date)
        return st

    # ---------- 实体注册 ---------- #
    def register_entities(self, cfg: dict) -> None:
        add_cb = self._hass.data.get(DOMAIN, {}).get("async_add_sensor")
        if not add_cb:
            raise ValueError("sensor 平台未就绪")
        daily = PowerDailySensor(self, cfg)
        monthly = PowerMonthlySensor(self, cfg)
        yearly = PowerYearlySensor(self, cfg)
        st = self._meters.get(cfg["entity_id"]) or self._new_meter_state(cfg)
        st["sensors"] = {"daily": daily, "monthly": monthly, "yearly": yearly}
        add_cb([daily, monthly, yearly])
        # add 后 hass 可能尚未立即附加，首次刷新失败不影响后续 tick 刷新
        for s in (daily, monthly, yearly):
            try:
                s.refresh_from_mgr()
            except Exception:
                pass

    def unregister_entities(self, cfg: dict) -> None:
        st = self._meters.get(cfg["entity_id"])
        if not st:
            return
        from homeassistant.helpers import entity_registry as er
        reg = er.async_get(self._hass)
        for name, ent in st["sensors"].items():
            eid = getattr(ent, "entity_id", None)
            if eid:
                try:
                    reg.async_remove(eid)
                except Exception:
                    pass
                self._hass.states.async_remove(eid)
        self._meters.pop(cfg["entity_id"], None)

    # ---------- 启动恢复 ---------- #
    def restore_all(self) -> None:
        """启动/重载时恢复全部已登记功率实体。"""
        configs = self.load_configs()
        for cfg in configs:
            if not cfg.get("enabled"):
                continue
            try:
                self.register_entities(cfg)
            except Exception:
                _LOGGER.exception("[power] 恢复功率计量失败 %s", cfg.get("entity_id"))
        _LOGGER.info("[power] 已恢复 %d 个功率计量登记", len(configs))

    # ---------- 积分采样 ---------- #
    async def _tick(self, now=None) -> None:
        now_dt = datetime.now()
        date = now_dt.strftime("%Y-%m-%d")
        for cfg in self.load_configs():
            if not cfg.get("enabled"):
                continue
            eid = cfg["entity_id"]
            st = self._meters.get(eid)
            if not st:
                self._new_meter_state(cfg)
                st = self._meters[eid]
            try:
                self._sample_one(cfg, st, now_dt, date)
            except Exception:
                _LOGGER.warning("[power] 采样失败 %s", eid, exc_info=True)
        # 定期落盘（内存累计 → 日表）：距上次落盘 ≥60s 才执行一次
        if getattr(self, "_last_persist_mono", None) is None or \
           (now_dt - self._last_persist_mono).total_seconds() >= PERSIST_SECONDS:
            self._last_persist_mono = now_dt
            await self._hass.async_add_executor_job(self._persist_all, date)

    def _sample_one(self, cfg: dict, st: dict, now_dt: datetime, date: str) -> None:
        eid = cfg["entity_id"]
        # 跨日：先落盘昨日，重置今日
        if st["date"] != date:
            self._persist_day(cfg, st["date"], st["cur"])
            st["date"] = date
            st["cur"] = self._get_day_row(eid, date)  # 今日已落盘值（重启后为 0）
            st["last_ts"] = None
            st["last_power_kw"] = None
            self.recompute_bases(cfg, date)

        state = self._hass.states.get(eid)
        if not state or state.state in ("unavailable", "unknown", "", None):
            st["last_ts"] = None
            st["last_power_kw"] = None
            return

        try:
            value = float(state.state)
        except (TypeError, ValueError):
            st["last_ts"] = None
            st["last_power_kw"] = None
            return

        unit = cfg.get("unit") or state.attributes.get("unit_of_measurement") or "W"
        kw = _power_to_kw(value, unit)

        if st["last_ts"] is None:
            # 首次采样仅建立基线
            st["last_ts"] = now_dt
            st["last_power_kw"] = kw
            return

        elapsed = (now_dt - st["last_ts"]).total_seconds()
        if elapsed <= 0 or elapsed > GAP_DROP_SECONDS:
            # 异常间隔（时钟回拨 / 停机空窗）丢弃
            st["last_ts"] = now_dt
            st["last_power_kw"] = kw
            return

        st["cur"] += kw * elapsed / 3600.0
        st["last_ts"] = now_dt
        st["last_power_kw"] = kw

        # 实时刷新 3 个实体
        for name, ent in list(st["sensors"].items()):
            try:
                ent.refresh_from_mgr()
            except Exception:
                pass

    def _persist_all(self, date: str) -> None:
        for eid, st in list(self._meters.items()):
            cfg_row = self._config_of(eid)
            if not cfg_row:
                continue
            self._persist_day(cfg_row, st["date"], st["cur"])
            # 顺带重算月/年基数（date 已是最新）
            self.recompute_bases(cfg_row, st["date"])
            # 重建日/月/年列表缓存（当前日期行刚已落盘）
            self._rebuild_lists(eid, st)

    def _config_of(self, entity_id: str) -> dict | None:
        for cfg in self.load_configs():
            if cfg["entity_id"] == entity_id:
                return cfg
        return None

    # ---------- 生命周期 ---------- #
    async def async_start(self) -> None:
        """启动：恢复登记实体并开始定时采样。

        注意：register_entities 内部通过平台 add_cb 注册实体，
        add_cb 必须在事件循环线程中调用（不能放入 executor），
        否则会报 "loop is not the running loop" / coroutine never awaited。
        DB 读取量很小，这里直接同步执行即可。
        """
        self.restore_all()
        self._unsub = async_track_time_interval(
            self._hass, self._tick, timedelta(seconds=SAMPLE_SECONDS)
        )

    async def async_stop(self) -> None:
        # 停止前把内存累计落盘一次
        try:
            await self._hass.async_add_executor_job(self._persist_all, _today_str())
        except Exception:
            pass
        unsub = getattr(self, "_unsub", None)
        if unsub:
            unsub()
