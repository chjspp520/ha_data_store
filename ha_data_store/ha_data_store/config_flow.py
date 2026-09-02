"""ha_data_store 配置流程。

包含：
  - Config Flow：首次添加集成（一键确认）
  - Options Flow：齿轮菜单 → 添加设备类 / 添加传感器类 / 删除 / 查看 / 完成
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry, OptionsFlow
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import (
    DOMAIN, TABLE_ENTITY_CONFIGS, TABLE_CUSTOM_ROUTES,
    CATEGORY_DEVICE, CATEGORY_ENVIRONMENT,
    VALID_METRICS, DANGEROUS_KEYWORDS,
    DEFAULT_TIMEZONE,
)
from .logger import get_logger

_LOGGER = logging.getLogger(__name__)


# =========================================================================== #
#  数据库辅助（阻塞函数）                                                        #
# =========================================================================== #
def _get_all_monitored(db_path: str) -> list[dict]:
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            f"SELECT entity_id, category, metric_type, collect_interval, power_entity, friendly_name, room "
            f"FROM {TABLE_ENTITY_CONFIGS} WHERE enabled = 1 "
            f"ORDER BY category, entity_id"
        )
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def _add_device_entities(db_path: str, entries: list[dict]) -> None:
    """添加设备类实体。每条包含 entity_id, device_name, room 和可选的 power_entity。"""
    now = datetime.utcnow().isoformat()
    conn = sqlite3.connect(db_path)
    try:
        # 确保列存在
        columns = [row[1] for row in conn.execute(f"PRAGMA table_info({TABLE_ENTITY_CONFIGS})")]
        for col, default, col_type in [
            ("category", f"'{CATEGORY_DEVICE}'", "TEXT"),
            ("metric_type", "''", "TEXT"),
            ("collect_interval", "30", "INTEGER"),
            ("power_entity", "''", "TEXT"),
            ("room", "''", "TEXT"),
            ("device_name", "''", "TEXT"),
            ("power_rating", "0", "REAL"),
        ]:
            if col not in columns:
                conn.execute(f"ALTER TABLE {TABLE_ENTITY_CONFIGS} ADD COLUMN {col} {col_type} NOT NULL DEFAULT {default}")
                conn.commit()

        for entry in entries:
            dn = entry.get("device_name", "")
            conn.execute(
                f"""
                INSERT INTO {TABLE_ENTITY_CONFIGS}
                    (entity_id, enabled, category, metric_type, collect_interval, power_entity, friendly_name, device_name, room, attr_type, created_at, updated_at)
                VALUES (?, 1, ?, '', 30, ?, '', ?, ?, '', ?, ?)
                ON CONFLICT(entity_id, attr_type) DO UPDATE SET
                    enabled = 1, category = excluded.category,
                    power_entity = excluded.power_entity,
                    device_name = excluded.device_name,
                    room = excluded.room,
                    updated_at = excluded.updated_at
                """,
                (entry["entity_id"], CATEGORY_DEVICE, entry.get("power_entity", ""),
                 dn or "", entry.get("room", ""), now, now),
            )
        conn.commit()
        local_logger = get_logger()
        if local_logger:
            local_logger.info("[cfg] 成功写入 %d 个设备类实体 entries=%s", len(entries),
                              [e["entity_id"] for e in entries])
    except Exception as exc:
        local_logger = get_logger()
        if local_logger:
            local_logger.error("[cfg] 写入设备类实体失败: %s", exc)
        raise
    finally:
        conn.close()


def _add_env_entities(db_path: str, entries: list[dict]) -> None:
    """添加传感器类实体。每条包含 entity_id, metric_type, collect_interval, round_minute, room。"""
    now = datetime.utcnow().isoformat()
    conn = sqlite3.connect(db_path)
    try:
        # 确保列存在
        columns = [row[1] for row in conn.execute(f"PRAGMA table_info({TABLE_ENTITY_CONFIGS})")]
        for col, default in [
            ("power_entity", "''"),
            ("room", "''"),
            ("round_minute", "0"),
        ]:
            if col not in columns:
                if "INTEGER" in default or default.isdigit() or default.lstrip("-").isdigit():
                    conn.execute(f"ALTER TABLE {TABLE_ENTITY_CONFIGS} ADD COLUMN {col} INTEGER NOT NULL DEFAULT {default}")
                else:
                    conn.execute(f"ALTER TABLE {TABLE_ENTITY_CONFIGS} ADD COLUMN {col} TEXT NOT NULL DEFAULT {default}")
                conn.commit()

        for entry in entries:
            rm = entry.get("round_minute", 0)
            conn.execute(
                f"""
                INSERT INTO {TABLE_ENTITY_CONFIGS}
                    (entity_id, enabled, category, metric_type, collect_interval, round_minute, power_entity, friendly_name, room, attr_type, created_at, updated_at)
                VALUES (?, 1, ?, ?, ?, ?, '', '', ?, '', ?, ?)
                ON CONFLICT(entity_id, attr_type) DO UPDATE SET
                    enabled = 1, category = excluded.category,
                    metric_type = excluded.metric_type,
                    collect_interval = excluded.collect_interval,
                    round_minute = excluded.round_minute,
                    room = excluded.room,
                    updated_at = excluded.updated_at
                """,
                (entry["entity_id"], CATEGORY_ENVIRONMENT,
                 entry["metric_type"], entry["collect_interval"],
                 rm, entry.get("room", ""), now, now),
            )
        conn.commit()
        local_logger = get_logger()
        if local_logger:
            local_logger.info("[cfg] 成功写入 %d 个传感器类实体 entries=%s", len(entries),
                              [(e["entity_id"], e["metric_type"]) for e in entries])
    except Exception as exc:
        local_logger = get_logger()
        if local_logger:
            local_logger.error("[cfg] 写入传感器类实体失败: %s", exc)
        raise
    finally:
        conn.close()


def _remove_entities(db_path: str, entity_ids: list[str]) -> None:
    now = datetime.utcnow().isoformat()
    conn = sqlite3.connect(db_path)
    try:
        for entity_id in entity_ids:
            conn.execute(
                f"UPDATE {TABLE_ENTITY_CONFIGS} SET enabled = 0, updated_at = ? "
                f"WHERE entity_id = ?",
                (now, entity_id),
            )
        conn.commit()
    finally:
        conn.close()


# ---- 自定义路由数据库操作 ---- #

def _get_all_routes(db_path: str) -> list[dict]:
    """获取所有自定义路由。"""
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            f"SELECT route_path, sql_statement, description, created_at, updated_at "
            f"FROM {TABLE_CUSTOM_ROUTES} ORDER BY route_path"
        )
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def _add_route(db_path: str, route_path: str, sql_statement: str, description: str) -> None:
    """添加或更新一条自定义路由。"""
    now = datetime.utcnow().isoformat()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            f"""
            INSERT INTO {TABLE_CUSTOM_ROUTES} (route_path, sql_statement, description, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(route_path) DO UPDATE SET
                sql_statement = excluded.sql_statement,
                description   = excluded.description,
                updated_at    = excluded.updated_at
            """,
            (route_path, sql_statement, description, now, now),
        )
        conn.commit()
    finally:
        conn.close()


def _delete_route(db_path: str, route_path: str) -> None:
    """删除一条自定义路由。"""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(f"DELETE FROM {TABLE_CUSTOM_ROUTES} WHERE route_path = ?", (route_path,))
        conn.commit()
    finally:
        conn.close()


# =========================================================================== #
#  设备桥接 数据库操作辅助函数                                                       #
# =========================================================================== #
def _add_bridge_connection(db_path: str, remote_url: str, access_token: str, name: str, verify_ssl: int) -> None:
    """新增一条桥接连接。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            f"INSERT INTO bridge_connections (name, remote_url, access_token, verify_ssl, enabled, created_at, updated_at) "
            f"VALUES (?, ?, ?, ?, 1, ?, ?)",
            (name, remote_url, access_token, verify_ssl, now, now),
        )
        conn.commit()
    finally:
        conn.close()


def _add_bridge_entities(db_path: str, connection_id: int, entity_ids: list[str]) -> None:
    """批量添加桥接实体。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(db_path)
    try:
        for eid in entity_ids:
            conn.execute(
                f"INSERT OR IGNORE INTO bridge_entities (connection_id, entity_id, enabled, created_at) "
                f"VALUES (?, ?, 1, ?)",
                (connection_id, eid, now),
            )
        conn.commit()
    finally:
        conn.close()


def _get_bridge_connection_options(db_path: str) -> list[dict]:
    """返回所有已启用连接的简化列表。"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(
            "SELECT id, name, remote_url FROM bridge_connections WHERE enabled = 1 ORDER BY id"
        ).fetchall()]
    finally:
        conn.close()


def _get_bridge_info(db_path: str) -> str:
    """返回桥接配置的人类可读摘要。"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        connections = conn.execute(
            "SELECT * FROM bridge_connections WHERE enabled = 1 ORDER BY id"
        ).fetchall()

        if not connections:
            return "当前没有已启用的桥接连接。"

        lines = []
        for conn_row in connections:
            entities = conn.execute(
                "SELECT entity_id FROM bridge_entities WHERE connection_id = ? AND enabled = 1 ORDER BY entity_id",
                (conn_row["id"],),
            ).fetchall()
            entity_list = [e["entity_id"] for e in entities]

            lines.append(f"连接 [{conn_row['id']}] {conn_row.get('name', '') or conn_row['remote_url']}")
            lines.append(f"  地址: {conn_row['remote_url']}")
            lines.append(f"  SSL: {'是' if conn_row.get('verify_ssl', 1) else '否'}")
            if entity_list:
                lines.append(f"  桥接实体 ({len(entity_list)}): {', '.join(entity_list)}")
            else:
                lines.append(f"  桥接实体: 无")
            lines.append("")

        return "\n".join(lines)
    finally:
        conn.close()


def _delete_bridge_config(db_path: str, delete_type: str, target_id: int) -> None:
    """删除桥接配置：'entity' 删除实体，'connection' 删除连接及其实体。"""
    conn = sqlite3.connect(db_path)
    try:
        if delete_type == "connection":
            conn.execute("DELETE FROM bridge_entities WHERE connection_id = ?", (target_id,))
            conn.execute("DELETE FROM bridge_connections WHERE id = ?", (target_id,))
        else:
            conn.execute("DELETE FROM bridge_entities WHERE id = ?", (target_id,))
        conn.commit()
    finally:
        conn.close()


def _validate_sql(sql: str) -> str:
    """校验 SQL 安全性，返回错误信息（空字符串表示通过）。"""
    sql_upper = sql.strip().upper()
    if not sql_upper.startswith("SELECT"):
        return "SQL 必须以 SELECT 开头"
    for kw in DANGEROUS_KEYWORDS:
        if kw in sql_upper:
            return f"SQL 包含危险关键字 '{kw}'"
    return ""


def _parse_device_entries(raw: str) -> tuple[list[dict], str]:
    """解析设备类输入，格式：房间, 设备名称, 设备实体ID, 电量传感器ID

    示例：
      厨房, 烧水壶, input_boolean.shao_shui_hu, sensor.xxx
      客厅, 电视, switch.tv, sensor.tv_energy
      客厅, switch.ac                               （兼容旧格式）
      light.bedroom, sensor.bedroom_energy           （兼容旧格式）

    房间、设备名称、电量传感器均为选配。
    返回: (entries列表, 错误信息)
    """
    entries = []
    errors = []
    seen = set()

    for line_num, line in enumerate(raw.strip().split("\n"), 1):
        line = line.strip()
        if not line:
            continue

        parts = [p.strip() for p in line.split(",")]

        room = ""
        device_name = ""
        entity_id = ""
        power_entity = ""

        # 判断首段是否含"."，含则视为旧格式（entity_id开头）
        if "." in parts[0]:
            # 旧格式：entity_id[, power_entity]
            entity_id = parts[0]
            if len(parts) >= 2 and parts[1]:
                power_entity = parts[1]
        else:
            # 新格式：room, [device_name,] entity_id[, power_entity]
            room = parts[0]
            # 检测第二个字段是设备名称还是实体ID
            if len(parts) >= 4 or (len(parts) >= 3 and "." not in parts[1]):
                # 有 4 段以上，或 3 段且第二段不含"."（第二段是设备名称）
                device_name = parts[1] if len(parts) >= 2 else ""
                if len(parts) < 3 or not parts[2]:
                    errors.append(f"第{line_num}行: 缺少设备实体ID")
                    continue
                entity_id = parts[2] if "." in parts[2] else ""
                power_entity = parts[3] if len(parts) >= 4 and parts[3] else ""
                if not entity_id:
                    # 回退：第二段可能是实体ID
                    if "." in parts[1]:
                        entity_id = parts[1]
                        device_name = ""
                        power_entity = parts[2] if len(parts) >= 3 and parts[2] else ""
                    else:
                        errors.append(f"第{line_num}行: 未找到有效的实体ID")
                        continue
            else:
                # 旧新格式：room, entity_id[, power_entity]
                entity_id = parts[1] if len(parts) >= 2 and "." in parts[1] else ""
                if not entity_id:
                    errors.append(f"第{line_num}行: 缺少设备实体ID")
                    continue
                power_entity = parts[2] if len(parts) >= 3 and parts[2] else ""

        if not entity_id:
            errors.append(f"第{line_num}行: 实体ID为空")
            continue

        if entity_id in seen:
            continue
        seen.add(entity_id)

        entries.append({
            "entity_id": entity_id,
            "power_entity": power_entity,
            "room": room,
            "device_name": device_name,
        })

    return entries, "\n".join(errors)


def _parse_env_entries(raw: str) -> tuple[list[dict], str]:
    """解析传感器类输入，格式：指标: 房间, 实体ID, 频率

    新格式示例：
      power: 客厅, sensor.xxx, 1
      temperature: 卧室, sensor.temp, 30

    旧格式兼容（首段含.视为entity_id）：
      temperature: sensor.living_room_temp, 30

    返回: (entries列表, 错误信息)
    """
    entries = []
    errors = []

    for line_num, line in enumerate(raw.strip().split("\n"), 1):
        line = line.strip()
        if not line:
            continue

        if ":" not in line:
            errors.append(f"第{line_num}行格式错误，需要 '指标: 房间, 实体ID, 频率'")
            continue

        parts = line.split(":", 1)
        metric = parts[0].strip().lower()
        rest = parts[1].strip()

        if metric not in VALID_METRICS:
            errors.append(f"第{line_num}行: 未知指标 '{metric}'，可选: {', '.join(VALID_METRICS)}")
            continue

        comma_parts = [p.strip() for p in rest.split(",")]

        room = ""
        entity_id = ""
        interval = 30

        # 判断冒号后首段是否含"."，含则视为旧格式（entity_id开头）
        if "." in comma_parts[0]:
            # 旧格式：entity_id[, interval]
            entity_id = comma_parts[0]
            if len(comma_parts) >= 2:
                try:
                    interval = int(comma_parts[-1].strip())
                    if interval < 1:
                        interval = 1
                except ValueError:
                    pass  # 保持默认30
        else:
            # 新格式：room, entity_id[, interval]
            room = comma_parts[0]
            if len(comma_parts) < 2 or not comma_parts[1]:
                errors.append(f"第{line_num}行: 缺少实体ID")
                continue
            entity_id = comma_parts[1]
            if len(comma_parts) >= 3:
                try:
                    interval = int(comma_parts[2].strip())
                    if interval < 1:
                        interval = 1
                except ValueError:
                    pass

        if not entity_id:
            errors.append(f"第{line_num}行: 实体ID为空")
            continue

        # 检测 round_minute (int 标志)
        round_minute = 0
        if any(p.strip().lower() == "int" for p in comma_parts):
            round_minute = 1

        entries.append({
            "entity_id": entity_id,
            "metric_type": metric,
            "collect_interval": interval,
            "round_minute": round_minute,
            "room": room,
        })

    return entries, "\n".join(errors)


# =========================================================================== #
#  Config Flow                                                                 #
# =========================================================================== #
class HaDataStoreConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        # 只允许添加一个该集成条目
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        if user_input is not None:
            return self.async_create_entry(title="HA数据统一存储系统", data={})
        return self.async_show_form(step_id="user")

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return HaDataStoreOptionsFlow(config_entry)


# =========================================================================== #
#  Options Flow                                                                #
# =========================================================================== #
class HaDataStoreOptionsFlow(OptionsFlow):
    def __init__(self, config_entry: ConfigEntry) -> None:
        self._config_entry = config_entry
        self._current_entities: list[dict] = []
        self._timezone = config_entry.options.get("timezone", DEFAULT_TIMEZONE)
        self._log_retention_days = config_entry.options.get("log_retention_days", 7)

    def _db_path(self) -> str | None:
        return self.hass.data.get(DOMAIN, {}).get("db_path")

    async def _refresh(self) -> list[dict]:
        db_path = self._db_path()
        if not db_path:
            self._current_entities = []
            return []
        try:
            self._current_entities = await self.hass.async_add_executor_job(_get_all_monitored, db_path)
            return self._current_entities
        except Exception:
            self._current_entities = []
            return []

    def _build_display(self) -> str:
        if not self._current_entities:
            return "当前未监控任何实体，请选择操作开始。"

        devices = [e for e in self._current_entities if e["category"] == CATEGORY_DEVICE]
        envs = [e for e in self._current_entities if e["category"] == CATEGORY_ENVIRONMENT]
        lines = []

        if devices:
            lines.append(f"  【设备类】({len(devices)}个)")
            for e in devices:
                room = e.get("room", "")
                power_ent = e.get("power_entity", "")
                parts = []
                if room:
                    parts.append(room)
                parts.append(e["entity_id"])
                if power_ent:
                    parts.append(power_ent)
                lines.append(f"    {', '.join(parts)}")

        if envs:
            lines.append(f"  【传感器类】({len(envs)}个)")
            for e in envs:
                room = e.get("room", "")
                env_str = f"    {e['metric_type']}: "
                if room:
                    env_str += f"{room}, "
                env_str += f"{e['entity_id']}, {e.get('collect_interval', 30)}分钟"
                lines.append(env_str)

        return f"当前已监控 {len(self._current_entities)} 个实体：\n" + "\n".join(lines)

    # ---- 主菜单 ---- #
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        await self._refresh()
        return self.async_show_menu(
            step_id="init",
            menu_options=["add_device", "add_env", "remove", "view", "timezone", "log_retention", "route_menu", "bridge_menu", "done"],
            description_placeholders={"menu_info": self._build_display()},
        )

    # ---- 添加设备类 ---- #
    async def async_step_add_device(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            raw = user_input.get("entity_ids", "")
            entries, parse_errors = _parse_device_entries(raw)

            if parse_errors:
                errors["entity_ids"] = "parse_error"
            elif not entries:
                errors["entity_ids"] = "请输入至少一个实体 ID"
            else:
                db_path = self._db_path()
                if not db_path:
                    errors["base"] = "db_error"
                else:
                    try:
                        await self.hass.async_add_executor_job(_add_device_entities, db_path, entries)
                    except Exception:
                        errors["base"] = "db_error"
            if not errors:
                return await self.async_step_init()

        return self.async_show_form(
            step_id="add_device",
            data_schema=vol.Schema({
                vol.Required("entity_ids"): selector.TextSelector(
                    selector.TextSelectorConfig(multiline=True),
                ),
            }),
            errors=errors,
            description_placeholders={"hint": (
                "每行一个设备实体，格式：房间, 设备实体ID, 电量传感器ID\n\n"
                "示例：\n"
                "  客厅, input_boolean.ce_shi, sensor.10_ch_monitor_ch_8_total_energy\n"
                "  卧室, switch.ac\n"
                "  light.bedroom, sensor.bedroom_energy  （兼容旧格式，首段含.视为实体ID）\n"
                "  climate.hall\n\n"
                "支持的设备类型：\n"
                "  input_boolean / switch / light / climate / cover / binary_sensor / device_tracker\n"
                "房间和电量传感器均为选配，不配置电量传感器时仅记录开关时间和时长"
            )},
        )

    # ---- 添加传感器类 ---- #
    async def async_step_add_env(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            raw = user_input.get("env_entries", "")
            entries, parse_errors = _parse_env_entries(raw)

            if parse_errors:
                errors["env_entries"] = "parse_error"
            elif not entries:
                errors["env_entries"] = "请输入至少一条配置"
            else:
                db_path = self._db_path()
                if not db_path:
                    errors["base"] = "db_error"
                else:
                    try:
                        await self.hass.async_add_executor_job(_add_env_entities, db_path, entries)
                    except Exception:
                        errors["base"] = "db_error"
            if not errors:
                return await self.async_step_init()

        return self.async_show_form(
            step_id="add_env",
            data_schema=vol.Schema({
                vol.Required("env_entries"): selector.TextSelector(
                    selector.TextSelectorConfig(multiline=True),
                ),
            }),
            errors=errors,
            description_placeholders={"hint": (
                "请按以下格式输入，每行一条：\n"
                "  指标: 房间, 实体ID, 采集频率(分钟)\n\n"
                "示例：\n"
                "  power: 客厅, sensor.10_ch_monitor_sum_power, 1\n"
                "  temperature: 卧室, sensor.living_room_temp, 30\n"
                "  humidity: sensor.living_room_hum, 30  （兼容旧格式）\n"
                "  pm25: 客厅, sensor.pm25, 30\n"
                "  co2: 书房, sensor.co2, 30\n\n"
                f"可选指标: {', '.join(VALID_METRICS)}\n"
                "房间为选配，频率默认30分钟，最小1分钟"
            )},
        )

    # ---- 删除实体 ---- #
    async def async_step_remove(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            raw = user_input.get("entity_ids", "")
            # 删除只需实体ID（含.的部分），跳过房间名
            entity_ids = []
            for p in raw.replace("\n", "\n").split("\n"):
                p = p.strip()
                if not p:
                    continue
                # 找到含.的部分作为entity_id
                for part in p.split(","):
                    part = part.strip()
                    if "." in part:
                        if part not in entity_ids:
                            entity_ids.append(part)
                        break
            if not entity_ids:
                errors["entity_ids"] = "请输入至少一个实体 ID"
            else:
                db_path = self._db_path()
                if not db_path:
                    errors["base"] = "db_error"
                else:
                    try:
                        await self.hass.async_add_executor_job(_remove_entities, db_path, entity_ids)
                    except Exception:
                        errors["base"] = "db_error"
            if not errors:
                return await self.async_step_init()

        await self._refresh()
        return self.async_show_form(
            step_id="remove",
            data_schema=vol.Schema({
                vol.Required("entity_ids"): selector.TextSelector(
                    selector.TextSelectorConfig(multiline=True),
                ),
            }),
            errors=errors,
            description_placeholders={"hint": self._build_display() + "\n\n输入要删除的设备实体ID（逗号前的部分）："},
        )

    # ---- 查看实体 ---- #
    async def async_step_view(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            return await self.async_step_init()
        await self._refresh()
        return self.async_show_form(
            step_id="view",
            data_schema=vol.Schema({}),
            description_placeholders={"info": self._build_display()},
        )

    # ---- 路由管理菜单 ---- #
    async def async_step_route_menu(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """路由管理子菜单。"""
        db_path = self._db_path()
        routes_text = ""
        if db_path:
            try:
                routes = await self.hass.async_add_executor_job(_get_all_routes, db_path)
                if routes:
                    lines = []
                    for r in routes:
                        lines.append(f"  /{r['route_path']}: {r.get('description', '')}")
                    routes_text = f"当前已配置 {len(routes)} 条路由：\n" + "\n".join(lines)
                else:
                    routes_text = "当前没有自定义路由。"
            except Exception:
                routes_text = "读取路由列表失败。"

        return self.async_show_menu(
            step_id="route_menu",
            menu_options=["route_add", "route_list", "route_delete", "route_back"],
            description_placeholders={"routes_info": routes_text},
        )

    # ---- 添加路由 ---- #
    async def async_step_route_add(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            route_path = user_input.get("route_path", "").strip()
            sql_statement = user_input.get("sql_statement", "").strip()
            description = user_input.get("description", "")

            if not route_path:
                errors["route_path"] = "路由路径不能为空"
            elif not sql_statement:
                errors["sql_statement"] = "SQL 不能为空"
            else:
                sql_err = _validate_sql(sql_statement)
                if sql_err:
                    errors["sql_statement"] = sql_err
                else:
                    db_path = self._db_path()
                    if not db_path:
                        errors["base"] = "db_error"
                    else:
                        try:
                            await self.hass.async_add_executor_job(
                                _add_route, db_path, route_path, sql_statement, description,
                            )
                        except Exception:
                            errors["base"] = "db_error"
            if not errors:
                return await self.async_step_route_menu()

        return self.async_show_form(
            step_id="route_add",
            data_schema=vol.Schema({
                vol.Required("route_path"): str,
                vol.Required("sql_statement"): selector.TextSelector(
                    selector.TextSelectorConfig(multiline=True),
                ),
                vol.Optional("description", default=""): str,
            }),
            errors=errors,
            description_placeholders={"hint": (
                "添加一条自定义路由，访问地址为：\n"
                "  GET /api/ha_data_store/custom/{路由路径}\n\n"
                "示例：\n"
                "  路由路径: living_room_report\n"
                "  SQL: SELECT entity_id, state, recorded_at FROM device_history "
                "WHERE entity_id = ? ORDER BY on_time DESC LIMIT 100\n\n"
                "调用方式:\n"
                "  GET /api/ha_data_store/custom/living_room_report?entity_id=switch.ac\n\n"
                "SQL 中使用 ? 占位符，URL 的查询参数按字母排序绑定到占位符"
            )},
        )

    # ---- 查看路由 ---- #
    async def async_step_route_list(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            return await self.async_step_route_menu()

        db_path = self._db_path()
        routes_text = "无法读取路由。"
        if db_path:
            try:
                routes = await self.hass.async_add_executor_job(_get_all_routes, db_path)
                if routes:
                    lines = []
                    for r in routes:
                        lines.append(f"  路径: /api/ha_data_store/custom/{r['route_path']}")
                        lines.append(f"  描述: {r.get('description', '无')}")
                        lines.append(f"  SQL: {r['sql_statement']}")
                        lines.append("")
                    routes_text = "\n".join(lines)
                else:
                    routes_text = "当前没有自定义路由。"
            except Exception:
                routes_text = "读取路由列表失败。"

        return self.async_show_form(
            step_id="route_list",
            data_schema=vol.Schema({}),
            description_placeholders={"info": routes_text + "\n\n点击'提交'返回菜单。"},
        )

    # ---- 删除路由 ---- #
    async def async_step_route_delete(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            route_path = user_input.get("route_path", "").strip()
            if not route_path:
                errors["route_path"] = "请输入路由路径"
            else:
                db_path = self._db_path()
                if not db_path:
                    errors["base"] = "db_error"
                else:
                    try:
                        await self.hass.async_add_executor_job(_delete_route, db_path, route_path)
                    except Exception:
                        errors["base"] = "db_error"
            if not errors:
                return await self.async_step_route_menu()

        # 显示现有路由帮助删除
        db_path = self._db_path()
        routes_text = ""
        if db_path:
            try:
                routes = await self.hass.async_add_executor_job(_get_all_routes, db_path)
                if routes:
                    routes_text = "\n".join(f"  /{r['route_path']}" for r in routes)
            except Exception:
                pass

        return self.async_show_form(
            step_id="route_delete",
            data_schema=vol.Schema({
                vol.Required("route_path"): str,
            }),
            errors=errors,
            description_placeholders={"hint": (
                "输入要删除的路由路径（不含 /api/ha_data_store/custom/ 前缀）：\n\n"
                f"现有路由：\n{routes_text}" if routes_text else "当前没有自定义路由。"
            )},
        )

    # ---- 返回主菜单 ---- #
    async def async_step_route_back(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        return await self.async_step_init()

    # ---- 时区设置 ---- #
    async def async_step_timezone(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            tz = user_input.get("tz_offset", DEFAULT_TIMEZONE)
            try:
                tz = int(tz)
            except (ValueError, TypeError):
                errors["tz_offset"] = "必须为整数"
            if not errors:
                self._timezone = tz
                return await self.async_step_init()

        return self.async_show_form(
            step_id="timezone",
            data_schema=vol.Schema({
                vol.Required("tz_offset", default=self._timezone): int,
            }),
            errors=errors,
            description_placeholders={"current_tz": str(self._timezone)},
        )

    # ---- 日志保留时长 ---- #
    async def async_step_log_retention(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            raw = user_input.get("log_retention_days", 7)
            try:
                self._log_retention_days = int(raw)
            except (ValueError, TypeError):
                errors["log_retention_days"] = "必须为整数"
            if not errors:
                return await self.async_step_init()

        return self.async_show_form(
            step_id="log_retention",
            data_schema=vol.Schema({
                vol.Required("log_retention_days", default=self._log_retention_days): vol.In({
                    1: "1天",
                    3: "3天",
                    7: "7天（默认）",
                    14: "14天",
                    30: "30天",
                }),
            }),
            errors=errors,
            description_placeholders={"current_days": str(self._log_retention_days)},
        )

    # ---- 设备桥接菜单 ---- #
    async def async_step_bridge_menu(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """设备桥接管理子菜单。"""
        db_path = self._db_path()
        info = ""
        if db_path:
            try:
                info = await self.hass.async_add_executor_job(_get_bridge_info, db_path)
            except Exception:
                info = "读取桥接配置失败。"
        return self.async_show_menu(
            step_id="bridge_menu",
            menu_options=[
                "bridge_add_connection", "bridge_add_entity",
                "bridge_view", "bridge_delete", "bridge_back",
            ],
            description_placeholders={"bridge_info": info},
        )

    # ---- 添加桥接连接 ---- #
    async def async_step_bridge_add_connection(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            remote_url = user_input.get("remote_url", "").strip()
            access_token = user_input.get("access_token", "").strip()
            name = user_input.get("name", "").strip()
            verify_ssl = user_input.get("verify_ssl", True)

            if not remote_url:
                errors["remote_url"] = "远程 HA 地址不能为空"
            elif not access_token:
                errors["access_token"] = "访问令牌不能为空"
            else:
                remote_url = remote_url.rstrip("/")
                db_path = self._db_path()
                if not db_path:
                    errors["base"] = "db_error"
                else:
                    try:
                        await self.hass.async_add_executor_job(
                            _add_bridge_connection, db_path, remote_url, access_token, name, 1 if verify_ssl else 0,
                        )
                    except Exception:
                        errors["base"] = "db_error"
            if not errors:
                return await self.async_step_bridge_menu()

        return self.async_show_form(
            step_id="bridge_add_connection",
            data_schema=vol.Schema({
                vol.Required("name", default=""): str,
                vol.Required("remote_url"): str,
                vol.Required("access_token"): str,
                vol.Optional("verify_ssl", default=True): bool,
            }),
            errors=errors,
            description_placeholders={"hint": (
                "添加一个远程 Home Assistant 连接。\n\n"
                "远程 HA 地址示例：http://192.168.1.100:8123\n"
                "访问令牌：在远程 HA「用户资料」中创建长期访问令牌。\n"
                "双方实体 ID 保持一致，例如生产 HA 的 switch.fan 桥接后，测试机也会创建 switch.fan。"
            )},
        )

    # ---- 添加桥接实体 ---- #
    async def async_step_bridge_add_entity(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            raw = user_input.get("entity_ids", "").strip()
            connection_id_str = user_input.get("connection_id", "").strip()

            if not connection_id_str:
                errors["connection_id"] = "请选择连接"
            elif not raw:
                errors["entity_ids"] = "请输入至少一个实体 ID"
            else:
                try:
                    connection_id = int(connection_id_str)
                except ValueError:
                    errors["connection_id"] = "连接 ID 必须为整数"
                else:
                    # 解析实体 ID（每行一个）
                    entity_ids = []
                    for line in raw.replace("\n", "\n").split("\n"):
                        eid = line.strip()
                        if eid and "." in eid:
                            entity_ids.append(eid)

                    if not entity_ids:
                        errors["entity_ids"] = "未找到有效的实体 ID（需包含 .）"
                    else:
                        db_path = self._db_path()
                        if not db_path:
                            errors["base"] = "db_error"
                        else:
                            try:
                                await self.hass.async_add_executor_job(
                                    _add_bridge_entities, db_path, connection_id, entity_ids,
                                )
                            except Exception:
                                errors["base"] = "db_error"
            if not errors:
                return await self.async_step_bridge_menu()

        # 获取连接列表供选择
        db_path = self._db_path()
        conn_options: dict[str, str] = {}
        if db_path:
            try:
                conns = await self.hass.async_add_executor_job(_get_bridge_connection_options, db_path)
                conn_options = {str(c["id"]): f"[{c['id']}] {c.get('name', c['remote_url'])}" for c in conns}
            except Exception:
                pass

        if not conn_options:
            return self.async_show_form(
                step_id="bridge_add_entity",
                data_schema=vol.Schema({}),
                description_placeholders={"hint": "没有可用的桥接连接，请先「添加远程连接」。点击提交返回。",
                                          "conn_options": ""},
            )

        return self.async_show_form(
            step_id="bridge_add_entity",
            data_schema=vol.Schema({
                vol.Required("connection_id", default=list(conn_options.keys())[0]): vol.In(conn_options),
                vol.Required("entity_ids"): selector.TextSelector(
                    selector.TextSelectorConfig(multiline=True),
                ),
            }),
            errors=errors,
            description_placeholders={"hint": (
                "每行输入一个实体 ID（与远程 HA 保持一致）：\n"
                "  switch.fan\n"
                "  light.bedroom\n"
                "  climate.ac\n"
                "  sensor.temp\n\n"
                "支持的类型：switch, light, climate, cover, fan, lock, number, select, sensor, binary_sensor"
            )},
        )

    # ---- 查看桥接配置 ---- #
    async def async_step_bridge_view(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            return await self.async_step_bridge_menu()
        db_path = self._db_path()
        info = "无法读取桥接配置。"
        if db_path:
            try:
                info = await self.hass.async_add_executor_job(_get_bridge_info, db_path)
            except Exception:
                info = "读取失败。"
        return self.async_show_form(
            step_id="bridge_view",
            data_schema=vol.Schema({}),
            description_placeholders={"info": info + "\n\n点击提交返回。"},
        )

    # ---- 删除桥接配置 ---- #
    async def async_step_bridge_delete(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            delete_type = user_input.get("delete_type", "entity")
            target_id = user_input.get("target_id", "").strip()

            if not target_id:
                errors["target_id"] = "请输入要删除的 ID"
            else:
                try:
                    target_id_int = int(target_id)
                except ValueError:
                    errors["target_id"] = "必须为整数 ID"
                else:
                    db_path = self._db_path()
                    if not db_path:
                        errors["base"] = "db_error"
                    else:
                        try:
                            await self.hass.async_add_executor_job(
                                _delete_bridge_config, db_path, delete_type, target_id_int,
                            )
                        except Exception:
                            errors["base"] = "db_error"
            if not errors:
                return await self.async_step_bridge_menu()

        db_path = self._db_path()
        info = ""
        if db_path:
            try:
                info = await self.hass.async_add_executor_job(_get_bridge_info, db_path)
            except Exception:
                pass

        return self.async_show_form(
            step_id="bridge_delete",
            data_schema=vol.Schema({
                vol.Required("delete_type", default="entity"): vol.In({
                    "entity": "删除桥接实体（按实体ID）",
                    "connection": "删除连接（将同时删除其下所有实体）",
                }),
                vol.Required("target_id"): str,
            }),
            errors=errors,
            description_placeholders={"hint": info + "\n\n输入要删除的 ID 号。删除连接会同时删除其下所有桥接实体。"},
        )

    # ---- 返回主菜单 ---- #
    async def async_step_bridge_back(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        return await self.async_step_init()

    # ---- 完成 ---- #
    async def async_step_done(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        await self._refresh()
        return self.async_create_entry(
            title="",
            data={
                "monitored_entities": [e["entity_id"] for e in self._current_entities],
                "timezone": self._timezone,
                "log_retention_days": self._log_retention_days,
            },
        )
