"""ha_data_store — HA数据统一存储系统集成。

核心特性：
  - 设备类：监听 ON/OFF 状态变化，自动记录开关时间和用电量
  - 传感器类：定时轮询采集，每个指标可指定独立传感器和采集频率
  - 属性类：提取实体状态属性中的指定字段，支持字段快照和列表展开两种模式
  - 统一泛域名动态路由，运行时从 SQLite 实时检索并执行 SQL
  - 通过 Config Flow 添加，无需修改 configuration.yaml
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, Event, callback
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers.event import async_track_time_interval, async_track_time_change
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import (
    DOMAIN,
    DATABASE_FILENAME,
    TABLE_ENTITY_CONFIGS,
    TABLE_DEVICE_HISTORY,
    TABLE_ENVIRONMENT_HISTORY,
    TABLE_CUSTOM_ROUTES,
    TABLE_ATTR_TYPE_DEFS,
    TABLE_EXPORT_CONFIGS,
    TABLE_FILE_SOURCE_CONFIGS,
    TABLE_API_SOURCE_CONFIGS,
    TABLE_API_KEYS,
    TABLE_API_SETTINGS,
    TABLE_VACUUM_TYPE_DEFS,
    TABLE_VACUUM_CONFIGS,
    TABLE_VACUUM_HISTORY,
    TABLE_PUSH_TARGETS,
    TABLE_BRIDGE_CONNECTIONS,
    TABLE_BRIDGE_ENTITIES,
    TABLE_HEALTH_RECORDS,
    TABLE_MEDIA_PLAYLISTS,
    TABLE_MEDIA_QUEUE,
    CATEGORY_DEVICE,
    CATEGORY_ENVIRONMENT,
    CATEGORY_ATTRIBUTE,
    ATTR_MODE_FIELDS,
    ATTR_MODE_LIST,
    ATTR_MODE_MULTI,
    COLLECT_MODE_POLL,
    COLLECT_MODE_EVENT,
    EXTRA_JSON_COLUMN,
    VALID_METRICS,
    METRIC_SENSOR,
    get_env_table_name,
    get_attr_table_name,
    ATTR_TABLE_PREFIX,
    DEFAULT_TIMEZONE,
    PENDING_JSON_FILENAME,
    SHUTDOWN_THRESHOLD_SECONDS,
)
from .logger import get_logger

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[str] = [
    "switch", "sensor",
    "light", "climate", "cover", "fan", "lock",
    "number", "select", "binary_sensor", "vacuum",
    "media_player",
]


def _refresh_monitored_set_sync(db_path: str) -> set[str]:
    """同步查询数据库，返回需要监听 state_changed 的 entity_id 集合。

    只包含用户主动配置且已启用的实体，避免全量监听 HA 所有实体变化。
    """
    import sqlite3
    monitored: set[str] = set()
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        # 1. device 类：entity_id + power_entity
        rows = conn.execute(
            f"SELECT entity_id, power_entity FROM {TABLE_ENTITY_CONFIGS} "
            f"WHERE enabled = 1 AND category = ?",
            (CATEGORY_DEVICE,),
        ).fetchall()
        for row in rows:
            monitored.add(row["entity_id"])
            if row["power_entity"]:
                monitored.add(row["power_entity"])
        # 2. attribute 类 event 模式：entity_id
        rows = conn.execute(
            f"SELECT entity_id FROM {TABLE_ENTITY_CONFIGS} "
            f"WHERE enabled = 1 AND category = ? AND collect_mode = ?",
            (CATEGORY_ATTRIBUTE, COLLECT_MODE_EVENT),
        ).fetchall()
        for row in rows:
            monitored.add(row["entity_id"])
        # 3. vacuum 类：trigger_entity_id
        rows = conn.execute(
            f"SELECT trigger_entity_id FROM {TABLE_VACUUM_CONFIGS} WHERE enabled = 1"
        ).fetchall()
        for row in rows:
            monitored.add(row["trigger_entity_id"])
    finally:
        conn.close()
    return monitored


# =========================================================================== #
#  时区辅助函数                                                                   #
# =========================================================================== #
def _get_local_now(timezone_offset: int) -> datetime:
    """将当前 UTC 时间加上时区偏移，返回本地时间。"""
    return datetime.utcnow() + timedelta(hours=timezone_offset)


def _get_local_now_str(timezone_offset: int, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """返回格式化的本地时间字符串。"""
    return _get_local_now(timezone_offset).strftime(fmt)


def _utc_to_local_str(utc_dt: datetime, timezone_offset: int) -> str:
    """将 UTC datetime 按自定义时区偏移转为本地时间字符串。"""
    if utc_dt.tzinfo is not None:
        utc_dt = utc_dt.replace(tzinfo=None) - utc_dt.utcoffset()
    return (utc_dt + timedelta(hours=timezone_offset)).strftime("%Y-%m-%d %H:%M:%S")


def _get_timezone(hass: HomeAssistant) -> int:
    """从 hass.data 获取当前配置的时区偏移。"""
    return hass.data.get(DOMAIN, {}).get("timezone", DEFAULT_TIMEZONE)

# =========================================================================== #
#  设备类实体的"开/关"状态判定规则                                               #
# =========================================================================== #
_ON_STATES = {
    "climate": {"auto", "cool", "dry", "heat", "fan_only"},
    "cover": {"open"},
    "device_tracker": {"home"},
}
_OFF_STATES = {
    "climate": {"off"},
    "cover": {"closed"},
    "device_tracker": {"not_home"},
}


def _get_entity_domain(entity_id: str) -> str:
    return entity_id.split(".", 1)[0] if "." in entity_id else ""


def _is_on_state(entity_id: str, state: str) -> bool:
    domain = _get_entity_domain(entity_id)
    on_set = _ON_STATES.get(domain)
    if on_set:
        return state.lower() in on_set
    return state.lower() == "on"


def _is_off_state(entity_id: str, state: str) -> bool:
    domain = _get_entity_domain(entity_id)
    off_set = _OFF_STATES.get(domain)
    if off_set:
        return state.lower() in off_set
    return state.lower() == "off"


# =========================================================================== #
#  async_setup                                                                 #
# =========================================================================== #
async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    return True


# =========================================================================== #
#  数据库初始化 + 迁移                                                          #
# =========================================================================== #
def _init_database(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    local_logger = get_logger()
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")

        # 1) 实体配置表（联合唯一(entity_id, attr_type)，允许同一实体多个属性类型）
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE_ENTITY_CONFIGS} (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_id        TEXT NOT NULL,
                enabled          INTEGER NOT NULL DEFAULT 1,
                category         TEXT NOT NULL DEFAULT '{CATEGORY_DEVICE}',
                metric_type      TEXT NOT NULL DEFAULT '',
                collect_interval INTEGER NOT NULL DEFAULT 30,
                round_minute     INTEGER NOT NULL DEFAULT 0,
                power_entity     TEXT NOT NULL DEFAULT '',
                friendly_name    TEXT NOT NULL DEFAULT '',
                device_name      TEXT NOT NULL DEFAULT '',
                room             TEXT NOT NULL DEFAULT '',
                attr_type        TEXT NOT NULL DEFAULT '',
                collect_mode     TEXT NOT NULL DEFAULT '{COLLECT_MODE_POLL}',
                created_at       TEXT NOT NULL DEFAULT '',
                updated_at       TEXT NOT NULL DEFAULT '',
                UNIQUE(entity_id, attr_type)
            );
            """
        )

        # 2) 设备类历史表
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE_DEVICE_HISTORY} (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_id       TEXT NOT NULL,
                name            TEXT NOT NULL DEFAULT '',
                on_time         TEXT NOT NULL DEFAULT '',
                off_time        TEXT NOT NULL DEFAULT '',
                on_power        REAL,
                off_power       REAL,
                energy_consumed REAL,
                duration        REAL,
                cross_day       INTEGER NOT NULL DEFAULT 0,
                room            TEXT NOT NULL DEFAULT '',
                state_attr      TEXT NOT NULL DEFAULT '',
                now_kwh         REAL
            );
            """
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_device_entity_time "
            f"ON {TABLE_DEVICE_HISTORY} (entity_id, on_time);"
        )

        # 迁移：已有表补充新列
        existing_cols = [row[1] for row in conn.execute(f"PRAGMA table_info({TABLE_DEVICE_HISTORY})")]
        if "state_attr" not in existing_cols:
            conn.execute(f"ALTER TABLE {TABLE_DEVICE_HISTORY} ADD COLUMN state_attr TEXT NOT NULL DEFAULT ''")
        if "now_kwh" not in existing_cols:
            conn.execute(f"ALTER TABLE {TABLE_DEVICE_HISTORY} ADD COLUMN now_kwh REAL")

        # 3) 传感器数据表：每种指标独立建表（统一结构 id, entity_id, name, datetime, value）
        #    sensor 类型 value 为 TEXT（支持非数值），其余为 REAL
        for metric in VALID_METRICS:
            tbl = get_env_table_name(metric)
            value_type = "TEXT" if metric == METRIC_SENSOR else "REAL"
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {tbl} (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_id TEXT NOT NULL,
                    name      TEXT NOT NULL DEFAULT '',
                    datetime  TEXT NOT NULL DEFAULT '',
                    value     {value_type},
                    room      TEXT NOT NULL DEFAULT ''
                );
                """
            )
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{tbl}_entity_time "
                f"ON {tbl} (entity_id, datetime);"
            )

        # 4) 自定义路由表
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE_CUSTOM_ROUTES} (
                route_path    TEXT PRIMARY KEY,
                sql_statement TEXT NOT NULL,
                description   TEXT NOT NULL DEFAULT '',
                created_at    TEXT NOT NULL DEFAULT '',
                updated_at    TEXT NOT NULL DEFAULT ''
            );
            """
        )

        # 5) 属性类型定义表
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE_ATTR_TYPE_DEFS} (
                type_name      TEXT PRIMARY KEY,
                mode           TEXT NOT NULL DEFAULT '{ATTR_MODE_FIELDS}',
                array_path     TEXT NOT NULL DEFAULT '',
                key_field      TEXT NOT NULL DEFAULT '',
                compare_limit  INTEGER NOT NULL DEFAULT 30,
                decimal_places INTEGER NOT NULL DEFAULT 2,
                field_mapping  TEXT NOT NULL DEFAULT '',
                field_types    TEXT NOT NULL DEFAULT '',
                description    TEXT NOT NULL DEFAULT '',
                created_at     TEXT NOT NULL DEFAULT '',
                updated_at     TEXT NOT NULL DEFAULT ''
            );
            """
        )

        # 6) 实体导出配置表
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE_EXPORT_CONFIGS} (
                entity_id   TEXT PRIMARY KEY,
                file_name   TEXT NOT NULL DEFAULT '',
                enabled     INTEGER NOT NULL DEFAULT 1,
                created_at  TEXT NOT NULL DEFAULT '',
                updated_at  TEXT NOT NULL DEFAULT ''
            );
            """
        )

        # 7) 文件源配置表（JSON 文件 → 实体）
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE_FILE_SOURCE_CONFIGS} (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                name           TEXT NOT NULL DEFAULT '',
                file_path      TEXT NOT NULL,
                state_field    TEXT NOT NULL DEFAULT '',
                entity_prefix  TEXT NOT NULL DEFAULT 'sensor.file_',
                poll_interval  INTEGER NOT NULL DEFAULT 10,
                enabled        INTEGER NOT NULL DEFAULT 1,
                last_mtime     REAL NOT NULL DEFAULT 0,
                device_id      TEXT NOT NULL DEFAULT '',
                created_at     TEXT NOT NULL DEFAULT '',
                updated_at     TEXT NOT NULL DEFAULT ''
            );
            """
        )

        # 8) API 源配置表（网络 API → 实体）
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE_API_SOURCE_CONFIGS} (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                name           TEXT NOT NULL DEFAULT '',
                url            TEXT NOT NULL,
                method         TEXT NOT NULL DEFAULT 'GET',
                state_field    TEXT NOT NULL DEFAULT '',
                entity_prefix  TEXT NOT NULL DEFAULT 'sensor.api_',
                poll_interval  INTEGER NOT NULL DEFAULT 60,
                timeout        INTEGER NOT NULL DEFAULT 15,
                max_retries    INTEGER NOT NULL DEFAULT 5,
                headers_json   TEXT NOT NULL DEFAULT '',
                enabled        INTEGER NOT NULL DEFAULT 1,
                fail_count     INTEGER NOT NULL DEFAULT 0,
                device_id      TEXT NOT NULL DEFAULT '',
                created_at     TEXT NOT NULL DEFAULT '',
                updated_at     TEXT NOT NULL DEFAULT ''
            );
            """
        )

        # 9) API 密钥表
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE_API_KEYS} (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                key        TEXT NOT NULL UNIQUE,
                name       TEXT NOT NULL DEFAULT '',
                enabled    INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT ''
            );
            """
        )

        # 10) API 设置表
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE_API_SETTINGS} (
                skey TEXT PRIMARY KEY,
                svalue TEXT NOT NULL DEFAULT ''
            );
            """
        )
        conn.execute(
            f"INSERT OR IGNORE INTO {TABLE_API_SETTINGS} (skey, svalue) VALUES ('admin_password', 'admin')"
        )

        # 健康记录表
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE_HEALTH_RECORDS} (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                date_time TEXT NOT NULL DEFAULT '',
                name      TEXT NOT NULL DEFAULT '',
                dp        REAL,
                sp        REAL,
                pr        REAL,
                height    REAL,
                weight    REAL,
                bmi       REAL,
                temp      REAL,
                type      TEXT NOT NULL DEFAULT '',
                remark    TEXT NOT NULL DEFAULT ''
            );
            """
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_health_name_time ON {TABLE_HEALTH_RECORDS} (name, date_time);"
        )
        # 14) 媒体播放列表表 — 存储用户播放列表名称 + 歌曲 JSON
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE_MEDIA_PLAYLISTS} (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user_name   TEXT NOT NULL,
                name        TEXT NOT NULL DEFAULT '',
                songs       TEXT NOT NULL DEFAULT '[]',
                created_at  TEXT NOT NULL DEFAULT '',
                updated_at  TEXT NOT NULL DEFAULT '',
                UNIQUE(user_name, name)
            )
            """
        )
        # 迁移：补充 songs 列（旧表）
        mp_columns = [row[1] for row in conn.execute(f"PRAGMA table_info({TABLE_MEDIA_PLAYLISTS})")]
        if "songs" not in mp_columns:
            conn.execute(f"ALTER TABLE {TABLE_MEDIA_PLAYLISTS} ADD COLUMN songs TEXT NOT NULL DEFAULT '[]'")
        # 15) 媒体播放队列表 — 后端列表播放
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE_MEDIA_QUEUE} (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_id     TEXT NOT NULL UNIQUE,
                tracks        TEXT NOT NULL DEFAULT '[]',
                current_index INTEGER NOT NULL DEFAULT 0,
                is_active     INTEGER NOT NULL DEFAULT 0,
                created_at    TEXT NOT NULL DEFAULT '',
                updated_at    TEXT NOT NULL DEFAULT ''
            )
            """
        )

        if local_logger:
            local_logger.info(
                "[db] 数据库表结构已就绪 tables=%d env_tables=%s",
                8 + len(VALID_METRICS), [get_env_table_name(m) for m in VALID_METRICS],
            )

        conn.commit()
        _migrate_database(conn)
        if local_logger:
            local_logger.info("[db] 数据库初始化完成 path=%s", db_path)
        _LOGGER.warning("[HDS] 数据库初始化完成: %s", db_path)
    finally:
        conn.close()


def _migrate_database(conn: sqlite3.Connection) -> None:
    """迁移：为旧表补充缺失的列 + 调整主键为 (entity_id, attr_type) 联合唯一。"""
    try:
        columns = [row[1] for row in conn.execute(f"PRAGMA table_info({TABLE_ENTITY_CONFIGS})")]

        # ★ 关键迁移：将 entity_configs 主键从 entity_id 改为 (entity_id, attr_type)
        # 允许同一实体有多个属性类型（如 ele_day + ele_month 同时采集同一实体）
        if "id" not in columns:
            # 创建新表结构
            new_columns_def = [
                "id INTEGER PRIMARY KEY AUTOINCREMENT",
                "entity_id TEXT NOT NULL",
                "enabled INTEGER NOT NULL DEFAULT 1",
                f"category TEXT NOT NULL DEFAULT '{CATEGORY_DEVICE}'",
                "metric_type TEXT NOT NULL DEFAULT ''",
                "collect_interval INTEGER NOT NULL DEFAULT 30",
                "round_minute INTEGER NOT NULL DEFAULT 0",
                "power_entity TEXT NOT NULL DEFAULT ''",
                "friendly_name TEXT NOT NULL DEFAULT ''",
                "device_name TEXT NOT NULL DEFAULT ''",
                "room TEXT NOT NULL DEFAULT ''",
                "attr_type TEXT NOT NULL DEFAULT ''",
                f"collect_mode TEXT NOT NULL DEFAULT '{COLLECT_MODE_POLL}'",
                "created_at TEXT NOT NULL DEFAULT ''",
                "updated_at TEXT NOT NULL DEFAULT ''",
                "UNIQUE(entity_id, attr_type)",
            ]
            conn.execute(f"CREATE TABLE {TABLE_ENTITY_CONFIGS}_new ({', '.join(new_columns_def)})")

            # 获取旧表所有列名
            old_columns = [row[1] for row in conn.execute(f"PRAGMA table_info({TABLE_ENTITY_CONFIGS})")]

            # 找出两表共有的列（排除 id，旧表没有）
            old_col_names = [c for c in old_columns if c != "id"]
            # 收集实际需要复制的列（新表中有定义的）
            copy_cols = []
            for c in old_col_names:
                if c in ["entity_id", "enabled", "category", "metric_type", "collect_interval",
                          "round_minute", "power_entity", "friendly_name", "device_name",
                          "room", "attr_type", "collect_mode", "created_at", "updated_at"]:
                    copy_cols.append(c)

            col_str = ", ".join(copy_cols)
            placeholders = ", ".join(["?" for _ in copy_cols])

            # 从旧表复制数据
            old_rows = conn.execute(f"SELECT {col_str} FROM {TABLE_ENTITY_CONFIGS}").fetchall()
            conn.executemany(
                f"INSERT INTO {TABLE_ENTITY_CONFIGS}_new ({col_str}) VALUES ({placeholders})",
                old_rows,
            )

            # 替换表
            conn.execute(f"DROP TABLE {TABLE_ENTITY_CONFIGS}")
            conn.execute(f"ALTER TABLE {TABLE_ENTITY_CONFIGS}_new RENAME TO {TABLE_ENTITY_CONFIGS}")
            conn.commit()

        if "category" not in columns:
            conn.execute(
                f"ALTER TABLE {TABLE_ENTITY_CONFIGS} "
                f"ADD COLUMN category TEXT NOT NULL DEFAULT '{CATEGORY_DEVICE}'"
            )
        if "metric_type" not in columns:
            conn.execute(
                f"ALTER TABLE {TABLE_ENTITY_CONFIGS} "
                f"ADD COLUMN metric_type TEXT NOT NULL DEFAULT ''"
            )
        if "collect_interval" not in columns:
            conn.execute(
                f"ALTER TABLE {TABLE_ENTITY_CONFIGS} "
                f"ADD COLUMN collect_interval INTEGER NOT NULL DEFAULT 30"
            )
        if "power_entity" not in columns:
            conn.execute(
                f"ALTER TABLE {TABLE_ENTITY_CONFIGS} "
                f"ADD COLUMN power_entity TEXT NOT NULL DEFAULT ''"
            )
        if "room" not in columns:
            conn.execute(
                f"ALTER TABLE {TABLE_ENTITY_CONFIGS} "
                f"ADD COLUMN room TEXT NOT NULL DEFAULT ''"
            )
        if "attr_type" not in columns:
            conn.execute(
                f"ALTER TABLE {TABLE_ENTITY_CONFIGS} "
                f"ADD COLUMN attr_type TEXT NOT NULL DEFAULT ''"
            )
        if "collect_mode" not in columns:
            conn.execute(
                f"ALTER TABLE {TABLE_ENTITY_CONFIGS} "
                f"ADD COLUMN collect_mode TEXT NOT NULL DEFAULT '{COLLECT_MODE_POLL}'"
            )
        if "round_minute" not in columns:
            conn.execute(
                f"ALTER TABLE {TABLE_ENTITY_CONFIGS} "
                f"ADD COLUMN round_minute INTEGER NOT NULL DEFAULT 0"
            )
        if "device_name" not in columns:
            conn.execute(
                f"ALTER TABLE {TABLE_ENTITY_CONFIGS} "
                f"ADD COLUMN device_name TEXT NOT NULL DEFAULT ''"
            )

        # device_history 表：补充 cross_day / room 列
        dh_columns = [row[1] for row in conn.execute(f"PRAGMA table_info({TABLE_DEVICE_HISTORY})")]
        if "cross_day" not in dh_columns:
            conn.execute(
                f"ALTER TABLE {TABLE_DEVICE_HISTORY} "
                f"ADD COLUMN cross_day INTEGER NOT NULL DEFAULT 0"
            )
        if "room" not in dh_columns:
            conn.execute(
                f"ALTER TABLE {TABLE_DEVICE_HISTORY} "
                f"ADD COLUMN room TEXT NOT NULL DEFAULT ''"
            )

        # env_* 表：补充 room 列
        for metric in VALID_METRICS:
            tbl = get_env_table_name(metric)
            try:
                env_columns = [row[1] for row in conn.execute(f"PRAGMA table_info({tbl})")]
                if "room" not in env_columns:
                    conn.execute(
                        f"ALTER TABLE {tbl} "
                        f"ADD COLUMN room TEXT NOT NULL DEFAULT ''"
                    )
            except Exception:
                pass  # 表可能尚未创建

        conn.commit()

        # attr_type_defs 表：补充 field_types / decimal_places / extra_fields 列
        try:
            atd_columns = [row[1] for row in conn.execute(f"PRAGMA table_info({TABLE_ATTR_TYPE_DEFS})")]
            if "field_types" not in atd_columns:
                conn.execute(
                    f"ALTER TABLE {TABLE_ATTR_TYPE_DEFS} "
                    f"ADD COLUMN field_types TEXT NOT NULL DEFAULT ''"
                )
            if "decimal_places" not in atd_columns:
                conn.execute(
                    f"ALTER TABLE {TABLE_ATTR_TYPE_DEFS} "
                    f"ADD COLUMN decimal_places INTEGER NOT NULL DEFAULT 2"
                )
            if "extra_fields" not in atd_columns:
                conn.execute(
                    f"ALTER TABLE {TABLE_ATTR_TYPE_DEFS} "
                    f"ADD COLUMN extra_fields TEXT NOT NULL DEFAULT ''"
                )
            if "extra_json_nodes" not in atd_columns:
                conn.execute(
                    f"ALTER TABLE {TABLE_ATTR_TYPE_DEFS} "
                    f"ADD COLUMN extra_json_nodes TEXT NOT NULL DEFAULT ''"
                )
            conn.commit()
        except Exception:
            pass  # 表可能尚未创建

        # file_source / api_source 表：补充 device_id / name 列
        for tbl in (TABLE_FILE_SOURCE_CONFIGS, TABLE_API_SOURCE_CONFIGS):
            try:
                tbl_columns = [row[1] for row in conn.execute(f"PRAGMA table_info({tbl})")]
                for col, dtype in ("device_id", "TEXT NOT NULL DEFAULT ''"), ("name", "TEXT NOT NULL DEFAULT ''"):
                    if col not in tbl_columns:
                        conn.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {dtype}")
                conn.commit()
            except Exception:
                pass

        # 迁移旧 environment_history 表数据到新的分指标表
        _migrate_env_table(conn)

        # 扫地机器人：创建类型定义表、实例配置表、历史记录表
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE_VACUUM_TYPE_DEFS} (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                type_name       TEXT NOT NULL UNIQUE,
                position_path   TEXT NOT NULL DEFAULT 'vacuum_position',
                working_states  TEXT NOT NULL DEFAULT 'cleaning',
                field_mapping   TEXT NOT NULL DEFAULT '{{}}',
                created_at      TEXT NOT NULL DEFAULT '',
                updated_at      TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE_VACUUM_CONFIGS} (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                vacuum_id           TEXT NOT NULL UNIQUE,
                type_name           TEXT NOT NULL,
                trigger_entity_id   TEXT NOT NULL,
                enabled             INTEGER NOT NULL DEFAULT 1,
                created_at          TEXT NOT NULL DEFAULT '',
                updated_at          TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE_VACUUM_HISTORY} (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                vacuum_id   TEXT NOT NULL,
                entity_id   TEXT NOT NULL,
                datetime    TEXT NOT NULL,
                seq         INTEGER NOT NULL DEFAULT 0,
                pos_x       REAL,
                pos_y       REAL,
                pos_a       REAL,
                state       TEXT NOT NULL DEFAULT ''
            )
            """
        )
        # 实体→网络 访问目标表
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE_PUSH_TARGETS} (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_id       TEXT NOT NULL UNIQUE,
                name            TEXT NOT NULL DEFAULT '',
                push_token      TEXT NOT NULL DEFAULT '',
                body_mode       TEXT NOT NULL DEFAULT 'full',
                field_mapping   TEXT NOT NULL DEFAULT '{{}}',
                interval_min    INTEGER NOT NULL DEFAULT 0,
                enabled         INTEGER NOT NULL DEFAULT 1,
                created_at      TEXT NOT NULL DEFAULT '',
                updated_at      TEXT NOT NULL DEFAULT ''
            )
            """
        )
        # 11) 桥接连接表 — 远程 HA 连接配置
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE_BRIDGE_CONNECTIONS} (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                name            TEXT NOT NULL DEFAULT '',
                remote_url      TEXT NOT NULL,
                access_token    TEXT NOT NULL,
                verify_ssl      INTEGER NOT NULL DEFAULT 1,
                enabled         INTEGER NOT NULL DEFAULT 1,
                created_at      TEXT NOT NULL DEFAULT '',
                updated_at      TEXT NOT NULL DEFAULT ''
            )
            """,
        )
        # 12) 桥接实体表 — 要桥接的实体列表
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {TABLE_BRIDGE_ENTITIES} (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                connection_id   INTEGER NOT NULL,
                entity_id       TEXT NOT NULL,
                enabled         INTEGER NOT NULL DEFAULT 1,
                created_at      TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (connection_id) REFERENCES {TABLE_BRIDGE_CONNECTIONS}(id),
                UNIQUE(connection_id, entity_id)
            )
            """,
        )
        # 13) 虚拟设备持久化表
        conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS virtual_devices (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_id       TEXT NOT NULL UNIQUE,
                device_type     TEXT NOT NULL,
                device_name     TEXT NOT NULL DEFAULT '',
                entity_name     TEXT NOT NULL DEFAULT '',
                extra_config    TEXT NOT NULL DEFAULT '{{}}',
                created_at      TEXT NOT NULL DEFAULT ''
            )
            """,
        )
        # 迁移旧表：补缺失列、补 token、修复 url 约束
        try:
            pt_columns = [row[1] for row in conn.execute(f"PRAGMA table_info({TABLE_PUSH_TARGETS})")]
            for col_name, col_def in [
                ("push_token", "TEXT NOT NULL DEFAULT ''"),
                ("interval_min", "INTEGER NOT NULL DEFAULT 0"),
                ("body_mode", "TEXT NOT NULL DEFAULT 'full'"),
                ("field_mapping", "TEXT NOT NULL DEFAULT '{}'"),
                ("url", "TEXT NOT NULL DEFAULT ''"),  # 旧表可能残留 NOT NULL 无默认值
            ]:
                if col_name not in pt_columns:
                    conn.execute(f"ALTER TABLE {TABLE_PUSH_TARGETS} ADD COLUMN {col_name} {col_def}")
            conn.execute(f"UPDATE {TABLE_PUSH_TARGETS} SET push_token = hex(randomblob(16)) WHERE push_token = ''")
        except Exception:
            pass
        conn.commit()

        # ── 数据清理：截断 on_time / off_time 中的毫秒后缀 ──
        try:
            cleaned_on = conn.execute(
                f"UPDATE {TABLE_DEVICE_HISTORY} "
                f"SET on_time = SUBSTR(on_time, 1, 19) "
                f"WHERE LENGTH(on_time) > 19"
            ).rowcount
            cleaned_off = conn.execute(
                f"UPDATE {TABLE_DEVICE_HISTORY} "
                f"SET off_time = SUBSTR(off_time, 1, 19) "
                f"WHERE LENGTH(off_time) > 19 AND off_time != ''"
            ).rowcount
            if cleaned_on or cleaned_off:
                conn.commit()
                if local_logger:
                    local_logger.info(
                        "[db] 毫秒清理: on_time=%d条 off_time=%d条",
                        cleaned_on, cleaned_off,
                    )
        except Exception:
            pass

        # ── 健康记录：截断 date_time 中的毫秒后缀 ──
        try:
            cleaned_health = conn.execute(
                f"UPDATE {TABLE_HEALTH_RECORDS} "
                f"SET date_time = SUBSTR(date_time, 1, 19) "
                f"WHERE LENGTH(date_time) > 19"
            ).rowcount
            if cleaned_health:
                conn.commit()
                if local_logger:
                    local_logger.info(
                        "[db] 健康毫秒清理: date_time=%d条", cleaned_health,
                    )
        except Exception:
            pass
    except Exception as exc:
        local_logger = get_logger()
        if local_logger:
            local_logger.warning("[db] 数据库迁移异常: %s", exc)


def _migrate_env_table(conn: sqlite3.Connection) -> None:
    """将旧 environment_history 表数据迁移到新的分指标表。"""
    local_logger = get_logger()
    try:
        # 检查旧表是否存在
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )]
        if TABLE_ENVIRONMENT_HISTORY not in tables:
            return

        if local_logger:
            local_logger.info("[db] 检测到旧环境表 %s，开始迁移...", TABLE_ENVIRONMENT_HISTORY)

        # 确保新表存在
        for metric in VALID_METRICS:
            tbl = get_env_table_name(metric)
            value_type = "TEXT" if metric == METRIC_SENSOR else "REAL"
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {tbl} (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_id TEXT NOT NULL,
                    name      TEXT NOT NULL DEFAULT '',
                    datetime  TEXT NOT NULL DEFAULT '',
                    value     {value_type},
                    room      TEXT NOT NULL DEFAULT ''
                );
                """
            )

        # 逐指标迁移数据
        total_migrated = 0
        conn.row_factory = sqlite3.Row
        for metric in VALID_METRICS:
            tbl = get_env_table_name(metric)
            try:
                cursor = conn.execute(
                    f"SELECT entity_id, name, datetime, {metric} "
                    f"FROM {TABLE_ENVIRONMENT_HISTORY} "
                    f"WHERE {metric} IS NOT NULL"
                )
                count = 0
                for row in cursor:
                    conn.execute(
                        f"INSERT INTO {tbl} (entity_id, name, datetime, value) "
                        f"VALUES (?, ?, ?, ?)",
                        (row["entity_id"], row["name"], row["datetime"], row[metric]),
                    )
                    count += 1
                if count > 0:
                    total_migrated += count
                    if local_logger:
                        local_logger.info("[db] 迁移指标 %s → %s: %d 条记录", metric, tbl, count)
            except Exception as exc:
                if local_logger:
                    local_logger.warning("[db] 迁移指标 %s 异常: %s", metric, exc)

        # 删除旧表
        conn.execute(f"DROP TABLE IF EXISTS {TABLE_ENVIRONMENT_HISTORY}")
        conn.commit()
        if local_logger:
            local_logger.info("[db] 旧环境表迁移完成，共迁移 %d 条记录，旧表已删除", total_migrated)
    except Exception as exc:
        if local_logger:
            local_logger.error("[db] 旧环境表迁移异常: %s", exc)


# =========================================================================== #
#  数据库查询辅助（阻塞函数）                                                    #
# =========================================================================== #
def _get_entity_info(db_path: str, entity_id: str) -> dict | None:
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            f"SELECT enabled, category, metric_type, collect_interval, power_entity, friendly_name, device_name, room, attr_type, collect_mode "
            f"FROM {TABLE_ENTITY_CONFIGS} WHERE entity_id = ?",
            (entity_id,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _get_all_env_entities(db_path: str) -> list[dict]:
    """获取所有启用的传感器类实体配置。"""
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            f"SELECT entity_id, metric_type, collect_interval, round_minute, friendly_name, room "
            f"FROM {TABLE_ENTITY_CONFIGS} "
            f"WHERE enabled = 1 AND category = '{CATEGORY_ENVIRONMENT}'"
        )
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


# =========================================================================== #
#  设备类：开机 → 插入新记录                                                     #
# =========================================================================== #
def _insert_device_on_record(
    db_path: str, entity_id: str, name: str, on_time: str, on_power: float | None,
    room: str = "", pending_json_path: str = "", state_attr: str = "",
) -> int | None:
    """写入开机记录，返回新记录 id。若 pending_json_path 非空则同步写入 JSON。"""
    # 毫秒防护：截断可能的毫秒后缀
    if len(on_time) > 19:
        on_time = on_time[:19]
    if on_power is not None:
        on_power = round(on_power, 2)
    record_id: int | None = None
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            f"INSERT INTO {TABLE_DEVICE_HISTORY} "
            f"(entity_id, name, on_time, off_time, on_power, off_power, energy_consumed, duration, room) "
            f"VALUES (?, ?, ?, '', ?, NULL, NULL, NULL, ?)",
            (entity_id, name, on_time, on_power, room),
        )
        record_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

        # 开机时写入初始 state_attr（兼容旧版设备无此字段则静默失败）
        if state_attr:
            try:
                conn.execute(
                    f"UPDATE {TABLE_DEVICE_HISTORY} SET state_attr = ? WHERE id = ?",
                    (state_attr, record_id),
                )
            except Exception:
                pass

        conn.commit()
        local_logger = get_logger()
        if local_logger:
            local_logger.info(
                "[device] 开机记录已写入 entity_id=%s name=%s on_time=%s on_power=%s room=%s",
                entity_id, name, on_time, on_power or "N/A", room or "N/A",
            )
    except Exception as exc:
        local_logger = get_logger()
        if local_logger:
            local_logger.error("[device] 写入开机记录失败 entity_id=%s: %s", entity_id, exc)
    finally:
        conn.close()

    # 同步写入 pending JSON
    if record_id is not None and pending_json_path:
        try:
            _pending_json_add(pending_json_path, entity_id, record_id, on_time, on_power, room)
        except Exception:
            pass

    return record_id


# =========================================================================== #
#  设备类：关机 → 更新最新未关闭记录                                              #
# =========================================================================== #
def _update_device_off_record(
    db_path: str, entity_id: str, off_time: str, off_power: float | None,
    pending_json_path: str = "",
) -> None:
    # 毫秒防护：截断可能的毫秒后缀
    if len(off_time) > 19:
        off_time = off_time[:19]
    if off_power is not None:
        off_power = round(off_power, 2)
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            f"SELECT id, on_time, on_power, now_kwh FROM {TABLE_DEVICE_HISTORY} "
            f"WHERE entity_id = ? AND (off_time = '' OR off_time IS NULL) "
            f"ORDER BY id DESC LIMIT 1",
            (entity_id,),
        )
        row = cursor.fetchone()
        if not row:
            local_logger = get_logger()
            if local_logger:
                local_logger.warning("[device] 未找到未关闭记录 entity_id=%s", entity_id)
            return

        record_id = row["id"]
        on_time = row["on_time"]
        on_power = row["on_power"]
        now_kwh = row["now_kwh"]

        # ★ 如果关机时没有捕获 off_power，用 now_kwh 回退（每分钟更新，误差可接受）
        if off_power is None and now_kwh is not None:
            off_power = round(now_kwh, 2)

        # 毫秒防护：截断 on_time 中的毫秒
        if len(on_time) > 19:
            on_time = on_time[:19]

        energy_consumed = None
        if on_power is not None and off_power is not None:
            energy_consumed = round(off_power - on_power, 2)

        duration = None
        try:
            fmt = "%Y-%m-%d %H:%M:%S"
            dt_on = datetime.strptime(on_time, fmt)
            dt_off = datetime.strptime(off_time, fmt)
            duration = round((dt_off - dt_on).total_seconds(), 0)
        except Exception as exc:
            local_logger = get_logger()
            if local_logger:
                local_logger.warning("[device] 计算时长失败 entity_id=%s: %s", entity_id, exc)

        conn.execute(
            f"UPDATE {TABLE_DEVICE_HISTORY} "
            f"SET off_time = ?, off_power = ?, energy_consumed = ?, duration = ?, now_kwh = NULL "
            f"WHERE id = ?",
            (off_time, off_power, energy_consumed, duration, record_id),
        )
        conn.commit()
        local_logger = get_logger()
        if local_logger:
            local_logger.info(
                "[device] 关机记录已更新 entity_id=%s record_id=%d "
                "on_time=%s off_time=%s on_power=%s off_power=%s energy=%.2fWh duration=%.0fs",
                entity_id, record_id, on_time, off_time,
                on_power or "N/A", off_power or "N/A",
                energy_consumed or 0, duration or 0,
            )
    except Exception as exc:
        local_logger = get_logger()
        if local_logger:
            local_logger.error("[device] 更新关机记录失败 entity_id=%s: %s", entity_id, exc)

    finally:
        conn.close()

    # 同步删除 pending JSON
    if pending_json_path:
        try:
            _pending_json_remove(pending_json_path, entity_id)
        except Exception:
            pass


# =========================================================================== #
#  从实体属性中提取电表读数                                                       #
# =========================================================================== #
def _extract_power_reading(state_obj) -> float | None:
    try:
        val = state_obj.state
        if val not in ("unavailable", "unknown", None, "on", "off",
                        "open", "closed", "auto", "cool", "dry", "heat", "fan_only"):
            return float(val)
    except (ValueError, TypeError, AttributeError):
        pass

    attrs = getattr(state_obj, "attributes", {}) or {}
    for key in ("power", "current_power", "energy", "meter_reading",
                "energy_consumed", "today_energy", "total_energy"):
        val = attrs.get(key)
        if val is not None:
            try:
                return float(val)
            except (ValueError, TypeError):
                continue
    return None


# =========================================================================== #
#  空调状态提取 & 写入 state_attr                                              #
# =========================================================================== #
def _extract_climate_state_attr(state_obj, now_str: str) -> str:
    """从气候实体提取当前状态，返回 JSON entry 字符串。

    temp 记录目标温度（用户设定值），cur_temp 记录当前室温。
    返回格式: {"t":"2026-06-15 13:00:00","state":"cool","temp":26.0,"cur_temp":25.5,"fan":"auto","preset":"eco","swing":"off"}
    state 为实体的主状态值（hvac_mode: cool/heat/auto/dry/fan_only/off）。
    """
    # 实体的 state 就是 HVAC 模式（cool/heat/auto/dry/fan_only/off）
    state_val = state_obj.state if state_obj else ""
    attrs = state_obj.attributes if state_obj else {}

    temp = attrs.get("temperature")  # 目标温度（用户设定值）
    if temp is not None:
        try:
            temp = round(float(temp), 1)
        except (ValueError, TypeError):
            temp = None

    # 当前室温，没有此属性时用 "--" 占位
    cur_temp = attrs.get("current_temperature")
    if cur_temp is not None:
        try:
            cur_temp = round(float(cur_temp), 1)
        except (ValueError, TypeError):
            cur_temp = "--"
    else:
        cur_temp = "--"

    fan = attrs.get("fan_mode", "")
    preset = attrs.get("preset_mode", "")
    swing = attrs.get("swing_mode", "")

    entry = {
        "t": now_str[:19],
        "state": state_val,
        "temp": temp,
        "cur_temp": cur_temp,
        "fan": fan or "",
        "preset": preset or "",
        "swing": swing or "",
    }
    return json.dumps(entry, ensure_ascii=False)


def _append_state_attr_to_record(db_path: str, entity_id: str, new_entry_json: str) -> None:
    """查出当前未关闭记录，将新的 entry 追加到 state_attr JSON 数组。

    ★ 去重：如果新 entry 的 state 和 temp 与最后一条都相同，跳过（仅真实变化才记录）。
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            f"SELECT id, state_attr FROM {TABLE_DEVICE_HISTORY} "
            f"WHERE entity_id = ? AND (off_time = '' OR off_time IS NULL) "
            f"ORDER BY id DESC LIMIT 1",
            (entity_id,),
        )
        row = cursor.fetchone()
        if not row:
            return

        record_id = row["id"]
        existing = row["state_attr"] or "[]"

        try:
            arr = json.loads(existing)
        except (json.JSONDecodeError, TypeError):
            arr = []

        new_entry = json.loads(new_entry_json)
        new_state = new_entry.get("state", "")
        new_temp = new_entry.get("temp")

        # 去重：state 和 temp 都与最后一条相同则跳过
        if arr:
            last = arr[-1]
            if last.get("state", "") == new_state and last.get("temp") == new_temp:
                return

        arr.append(new_entry)

        new_state_attr = json.dumps(arr, ensure_ascii=False)
        conn.execute(
            f"UPDATE {TABLE_DEVICE_HISTORY} SET state_attr = ? WHERE id = ?",
            (new_state_attr, record_id),
        )
        conn.commit()
    finally:
        conn.close()


# =========================================================================== #
#  设备类 now_kwh：每分钟轮询辅助函数                                            #
# =========================================================================== #
def _get_device_kwh_entities(db_path: str) -> list[dict]:
    """获取所有启用的、配置了 power_entity 的设备类实体。"""
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            f"SELECT entity_id, power_entity FROM {TABLE_ENTITY_CONFIGS} "
            f"WHERE enabled = 1 AND category = '{CATEGORY_DEVICE}' "
            f"AND power_entity != ''"
        )
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def _update_device_now_kwh(db_path: str, entity_id: str, value: float) -> None:
    """更新当前未关闭记录的 now_kwh。"""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            f"UPDATE {TABLE_DEVICE_HISTORY} SET now_kwh = ? "
            f"WHERE entity_id = ? AND (off_time = '' OR off_time IS NULL)",
            (round(value, 3), entity_id),
        )
        conn.commit()
    finally:
        conn.close()


async def _async_device_now_kwh_poll(hass: HomeAssistant, db_path: str, now=None) -> None:
    """每分钟执行：对配置了 power_entity 的设备更新 now_kwh。"""
    try:
        entities = await hass.async_add_executor_job(_get_device_kwh_entities, db_path)
    except Exception:
        return

    if not entities:
        return

    for ent in entities:
        entity_id = ent["entity_id"]
        power_entity = ent["power_entity"]

        p_state = hass.states.get(power_entity)
        if not p_state or p_state.state in ("unavailable", "unknown", None, ""):
            continue

        try:
            value = float(p_state.state)
        except (ValueError, TypeError):
            continue

        try:
            await hass.async_add_executor_job(
                _update_device_now_kwh, db_path, entity_id, value,
            )
        except Exception:
            pass


# =========================================================================== #
#  午夜拆分：获取未关闭设备记录（含 power_entity 配置）                           #
# =========================================================================== #
def _get_unclosed_device_records(db_path: str) -> list[dict]:
    """获取所有未关闭的设备记录及其 power_entity 配置。"""
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            f"SELECT dh.id, dh.entity_id, dh.name, dh.on_time, dh.on_power, "
            f"  ec.power_entity, ec.room "
            f"FROM {TABLE_DEVICE_HISTORY} dh "
            f"LEFT JOIN {TABLE_ENTITY_CONFIGS} ec ON dh.entity_id = ec.entity_id "
            f"WHERE dh.off_time = '' OR dh.off_time IS NULL"
        )
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


# =========================================================================== #
#  今日数据修正：修正单条未关闭记录                                                  #
# =========================================================================== #
async def _async_correct_single_record(
    hass: HomeAssistant, db_path: str, entity_id: str,
    record_id: int, on_time: str, on_power: float | None,
    next_boundary: str, timezone_offset: int,
) -> None:
    """
    修正单条未关闭记录：通过 HA recorder 查询实际关机时间，
    更新 off_time / off_power / energy_consumed / duration。
    """
    local_logger = get_logger()
    fmt = "%Y-%m-%d %H:%M:%S"

    try:
        dt_on = datetime.strptime(on_time, fmt)
        dt_next = datetime.strptime(next_boundary, fmt)
    except ValueError:
        if local_logger:
            local_logger.warning(
                "[修正] 时间格式解析失败 entity_id=%s on_time=%s", entity_id, on_time,
            )
        return

    # 查询 HA recorder
    try:
        from homeassistant.components.recorder.history import state_changes_during_period
        from homeassistant.components.recorder import get_instance

        instance = get_instance(hass)
        if instance is None:
            if local_logger:
                local_logger.warning(
                    "[修正] recorder 不可用，跳过修正 entity_id=%s on_time=%s",
                    entity_id, on_time,
                )
            return

        start = dt_on - timedelta(seconds=30)
        end = dt_next + timedelta(seconds=30)

        changes = await instance.async_add_executor_job(
            state_changes_during_period, hass, start, end, entity_id,
        )
    except Exception:
        if local_logger:
            local_logger.exception(
                "[修正] 查询 recorder 异常 entity_id=%s on_time=%s", entity_id, on_time,
            )
        return

    states = changes.get(entity_id, [])
    if not states:
        if local_logger:
            local_logger.warning(
                "[修正] 未查询到历史状态 entity_id=%s [%s~%s]",
                entity_id, on_time, next_boundary,
            )
        return

    # 从后往前查找最后一个 off 状态
    off_state = None
    for s in reversed(states):
        if _is_off_state(entity_id, s.state):
            off_state = s
            break

    if off_state is None:
        if local_logger:
            local_logger.warning(
                "[修正] 未找到关机状态 entity_id=%s [%s~%s]",
                entity_id, on_time, next_boundary,
            )
        return

    # 转换 UTC last_changed 为本地时间
    corrected_off_time = _utc_to_local_str(off_state.last_changed, timezone_offset)

    # 计算 duration
    try:
        dt_off = datetime.strptime(corrected_off_time, fmt)
        duration = round((dt_off - dt_on).total_seconds(), 0)
        if duration <= 0:
            if local_logger:
                local_logger.warning(
                    "[修正] duration ≤ 0，跳过 entity_id=%s on_time=%s off_time=%s",
                    entity_id, on_time, corrected_off_time,
                )
            return
    except Exception as exc:
        if local_logger:
            local_logger.warning(
                "[修正] 计算时长失败 entity_id=%s: %s", entity_id, exc,
            )
        return

    # 从设备关机状态中提取功率（不存在就为 NULL）
    off_power = _extract_power_reading(off_state)

    # 计算能耗
    energy_consumed = None
    if on_power is not None and off_power is not None:
        energy_consumed = round(off_power - on_power, 2)

    # 更新数据库
    def _update():
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                f"UPDATE {TABLE_DEVICE_HISTORY} "
                f"SET off_time=?, off_power=?, energy_consumed=?, duration=? "
                f"WHERE id=?",
                (corrected_off_time, off_power, energy_consumed, duration, record_id),
            )
            conn.commit()
        finally:
            conn.close()

    try:
        await hass.async_add_executor_job(_update)
    except Exception as exc:
        if local_logger:
            local_logger.exception(
                "[修正] 更新数据库异常 entity_id=%s: %s", entity_id, exc,
            )
        return

    if local_logger:
        local_logger.info(
            "[修正] entity_id=%s on_time=%s 已修正: "
            "off_time=%s duration=%.0fs off_power=%s energy_consumed=%s",
            entity_id, on_time, corrected_off_time, duration or 0,
            off_power or "N/A", energy_consumed or "N/A",
        )


# =========================================================================== #
#  今日数据修正：批量修正同一实体的多条未关闭记录                                      #
# =========================================================================== #
async def _async_correct_unclosed(
    hass: HomeAssistant, db_path: str, entity_id: str,
    unclosed: list | list[tuple], timezone_offset: int,
) -> None:
    """
    修正同一实体的多条未关闭记录。保留最晚的一条，用其 on_time 作为前一条的修正边界；
    若只有一条，则用当前时间作为修正边界。
    """
    if not unclosed:
        return

    now_str = _get_local_now_str(timezone_offset)

    # 按 on_time 升序排列
    unclosed_sorted = sorted(unclosed, key=lambda r: r[1])

    for i, rec in enumerate(unclosed_sorted):
        # 用下一条的 on_time 或当前时间作为修正边界
        if i < len(unclosed_sorted) - 1:
            next_boundary = unclosed_sorted[i + 1][1]
        else:
            next_boundary = now_str

        rid, ron_time, ron_power, = rec[0], rec[1], rec[2]

        await _async_correct_single_record(
            hass, db_path, entity_id, rid, ron_time, ron_power,
            next_boundary, timezone_offset,
        )


# =========================================================================== #
#  今日数据修正：定时扫描今日未关闭记录异常                                           #
# =========================================================================== #
async def _async_correct_periodic_scan(hass: HomeAssistant, db_path: str) -> None:
    """定时扫描：检测今日数据中同一设备的多条未关闭记录，进行修正。"""
    tz = _get_timezone(hass)
    today_prefix = _get_local_now_str(tz)[:10]

    def _get_today_unclosed():
        conn = sqlite3.connect(db_path)
        try:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                f"SELECT id, entity_id, on_time, on_power, room "
                f"FROM {TABLE_DEVICE_HISTORY} "
                f"WHERE off_time = '' AND on_time LIKE ? || '%' "
                f"ORDER BY entity_id, on_time",
                (today_prefix,),
            )
            return [dict(r) for r in cursor.fetchall()]
        finally:
            conn.close()

    records = await hass.async_add_executor_job(_get_today_unclosed)
    if not records:
        return

    # 按 entity_id 分组
    grouped: dict[str, list] = {}
    for r in records:
        grouped.setdefault(r["entity_id"], []).append(r)

    for entity_id, recs in grouped.items():
        if len(recs) < 2:
            continue

        recs.sort(key=lambda r: r["on_time"])

        for i, rec in enumerate(recs[:-1]):
            next_on = recs[i + 1]["on_time"]
            await _async_correct_single_record(
                hass, db_path, entity_id,
                rec["id"], rec["on_time"], rec["on_power"],
                next_on, tz,
            )
# =========================================================================== #
def _read_current_power_value(hass: HomeAssistant, power_entity: str, entity_id: str) -> float | None:
    """读取当前电力值。优先从 power_entity 传感器读取，回退到设备自身属性。"""
    if power_entity:
        p_state = hass.states.get(power_entity)
        if p_state and p_state.state not in ("unavailable", "unknown", None):
            try:
                return round(float(p_state.state), 2)
            except (ValueError, TypeError):
                pass

    state = hass.states.get(entity_id)
    if state:
        val = _extract_power_reading(state)
        if val is not None:
            return val

    return None


# =========================================================================== #
#  午夜拆分：从历史记录获取最近的电量值（回退方案）                                #
# =========================================================================== #
def _get_latest_power_from_history(conn: sqlite3.Connection, entity_id: str) -> float | None:
    """从 device_history 表中获取该实体最近的 off_power 或 on_power。"""
    cursor = conn.execute(
        f"SELECT off_power FROM {TABLE_DEVICE_HISTORY} "
        f"WHERE entity_id = ? AND off_power IS NOT NULL "
        f"ORDER BY id DESC LIMIT 1",
        (entity_id,),
    )
    row = cursor.fetchone()
    if row and row[0] is not None:
        return row[0]

    cursor = conn.execute(
        f"SELECT on_power FROM {TABLE_DEVICE_HISTORY} "
        f"WHERE entity_id = ? AND on_power IS NOT NULL "
        f"ORDER BY id DESC LIMIT 1",
        (entity_id,),
    )
    row = cursor.fetchone()
    if row and row[0] is not None:
        return row[0]

# =========================================================================== #
#  本地 JSON 缓存：防止关机事件丢失                                                #
# =========================================================================== #
def _get_pending_json_path(storage_dir: str) -> str:
    """返回 pending JSON 文件路径。"""
    return os.path.join(storage_dir, PENDING_JSON_FILENAME)


def _load_pending_json(json_path: str) -> dict:
    """读取 pending JSON，返回 {last_shutdown_time, pending: {entity_id: {...}}}。"""
    if not os.path.exists(json_path):
        return {"last_shutdown_time": "", "pending": {}}
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"last_shutdown_time": "", "pending": {}}
        data.setdefault("last_shutdown_time", "")
        data.setdefault("pending", {})
        return data
    except Exception:
        return {"last_shutdown_time": "", "pending": {}}


def _save_pending_json(json_path: str, data: dict) -> None:
    """原子写入 pending JSON。"""
    try:
        tmp_path = json_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, json_path)
    except Exception as exc:
        local_logger = get_logger()
        if local_logger:
            local_logger.warning("[pending] 写入 JSON 失败: %s", exc)


def _pending_json_add(json_path: str, entity_id: str, record_id: int,
                       on_time: str, on_power: float | None, room: str) -> None:
    """向 pending JSON 添加一条待追踪记录，并更新 last_shutdown_time。"""
    data = _load_pending_json(json_path)
    data["pending"][entity_id] = {
        "record_id": record_id,
        "on_time": on_time,
        "on_power": on_power,
        "room": room,
    }
    # 使用 UTC 时间，避免时区不一致问题
    data["last_shutdown_time"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    _save_pending_json(json_path, data)


def _pending_json_remove(json_path: str, entity_id: str) -> None:
    """从 pending JSON 中删除一条记录。"""
    data = _load_pending_json(json_path)
    if entity_id in data["pending"]:
        del data["pending"][entity_id]
        _save_pending_json(json_path, data)


def _pending_json_update(json_path: str, entity_id: str, record_id: int,
                          on_time: str, on_power: float | None, room: str) -> None:
    """更新 pending JSON 中某条记录（午夜拆分后 record_id/on_time 变化时调用）。"""
    data = _load_pending_json(json_path)
    if entity_id in data["pending"]:
        data["pending"][entity_id] = {
            "record_id": record_id,
            "on_time": on_time,
            "on_power": on_power,
            "room": room,
        }
        _save_pending_json(json_path, data)
    else:
        # JSON 中没有（可能之前丢失），补上
        _pending_json_add(json_path, entity_id, record_id, on_time, on_power, room)


# =========================================================================== #
#  午夜拆分：执行数据库拆分操作（阻塞函数）                                       #
# =========================================================================== #
def _do_midnight_splits(db_path: str, items: list[dict], off_time_str: str,
                        on_time_str: str, pending_json_path: str = "") -> None:
    """对未关闭的设备记录执行午夜拆分。

    items 中每条包含: id, entity_id, name, on_time, on_power, current_power, is_on
    off_time_str: 前一天 23:59:59
    on_time_str: 当天 00:00:00
    """
    today_date = on_time_str[:10]  # YYYY-MM-DD
    local_logger = get_logger()
    conn = sqlite3.connect(db_path)
    try:
        # ── 方案五：清理同实体重复的未关闭记录（只保留 id 最大的，其余直接关闭） ──
        entity_max_id: dict[str, int] = {}
        for item in items:
            eid = item["entity_id"]
            rid = item["id"]
            if eid not in entity_max_id or rid > entity_max_id[eid]:
                entity_max_id[eid] = rid

        duplicate_close_count = 0
        for item in items:
            eid = item["entity_id"]
            rid = item["id"]
            if rid != entity_max_id[eid]:
                # 这是重复的未关闭记录，直接关闭
                current_power = item.get("current_power")
                if current_power is not None:
                    current_power = round(current_power, 2)
                # Bug B 修复：如果 on_time 在 off_time_str 之后，用 on_time 作为 off_time（零时长）
                dup_on_time = item["on_time"]
                if len(dup_on_time) > 19:
                    dup_on_time = dup_on_time[:19]
                dup_off_time = off_time_str
                if dup_on_time > off_time_str:
                    dup_off_time = dup_on_time  # on_time 比 off_time 晚，用 on_time
                conn.execute(
                    f"UPDATE {TABLE_DEVICE_HISTORY} "
                    f"SET off_time = ?, off_power = ?, cross_day = 1 "
                    f"WHERE id = ?",
                    (dup_off_time, current_power, rid),
                )
                duplicate_close_count += 1
                if local_logger:
                    local_logger.info(
                        "[midnight] 清理重复记录 entity_id=%s record_id=%d off_time=%s",
                        eid, rid, dup_off_time,
                    )
                # Bug A 修复：不从 JSON 移除，主循环会处理存活的记录

        if duplicate_close_count and local_logger:
            local_logger.info(
                "[midnight] 清理重复未关闭记录 %d 条", duplicate_close_count,
            )

        # ── 主拆分逻辑 ──
        for item in items:
            record_id = item["id"]
            entity_id = item["entity_id"]
            name = item["name"]
            on_power = item["on_power"]
            current_power = item["current_power"]
            is_on = item.get("is_on", True)
            room = item.get("room", "") or ""

            # 跳过已处理的重复记录
            if record_id != entity_max_id.get(entity_id):
                continue

            # 仅拆分 on_time 在今天之前的记录
            raw_on_time = item["on_time"]
            # 毫秒防护：截断毫秒后缀
            if len(raw_on_time) > 19:
                raw_on_time = raw_on_time[:19]
            on_time_date = raw_on_time[:10]
            if on_time_date >= today_date:
                # 记录是今天的，不需要拆分，但需要确保 JSON 指向这条存活的记录
                if pending_json_path:
                    try:
                        _pending_json_update(pending_json_path, entity_id, record_id,
                                             raw_on_time, on_power, room)
                    except Exception:
                        pass
                continue

            # 确认记录仍然未关闭（防止竞态）
            cursor = conn.execute(
                f"SELECT off_time FROM {TABLE_DEVICE_HISTORY} WHERE id = ?",
                (record_id,),
            )
            row = cursor.fetchone()
            if not row or row[0]:
                continue

            # 如果 current_power 仍为空，回退到历史记录
            if current_power is None:
                current_power = _get_latest_power_from_history(conn, entity_id)
            if current_power is not None:
                current_power = round(current_power, 2)

            # 计算旧记录的 energy_consumed 和 duration
            energy_consumed = None
            if on_power is not None and current_power is not None:
                energy_consumed = round(current_power - on_power, 2)

            duration = None
            try:
                fmt = "%Y-%m-%d %H:%M:%S"
                dt_on = datetime.strptime(raw_on_time, fmt)
                dt_off = datetime.strptime(off_time_str, fmt)
                duration = round((dt_off - dt_on).total_seconds(), 0)
            except Exception as exc:
                if local_logger:
                    local_logger.warning("[midnight] 计算时长失败 entity_id=%s: %s", entity_id, exc)

            # 更新旧记录：off_time=前一天23:59:59, cross_day=1
            conn.execute(
                f"UPDATE {TABLE_DEVICE_HISTORY} "
                f"SET off_time = ?, off_power = ?, energy_consumed = ?, duration = ?, cross_day = 1 "
                f"WHERE id = ?",
                (off_time_str, current_power, energy_consumed, duration, record_id),
            )

            # ── 方案二：设备已关机则不创建续接记录 ──
            if not is_on:
                if local_logger:
                    local_logger.info(
                        "[midnight] 设备已关机不续接 entity_id=%s record_id=%d "
                        "on_time=%s off_time=%s off_power=%s cross_day=1",
                        entity_id, record_id, raw_on_time, off_time_str, current_power,
                    )
                # 从 JSON 中移除
                if pending_json_path:
                    try:
                        _pending_json_remove(pending_json_path, entity_id)
                    except Exception:
                        pass
                continue

            # ── 方案三：同实体同日防重复续接 ──
            existing_row = conn.execute(
                f"SELECT id FROM {TABLE_DEVICE_HISTORY} "
                f"WHERE entity_id = ? AND on_time = ? AND (off_time = '' OR off_time IS NULL) "
                f"ORDER BY id DESC LIMIT 1",
                (entity_id, on_time_str),
            ).fetchone()
            if existing_row:
                if local_logger:
                    local_logger.info(
                        "[midnight] 跳过重复续接 entity_id=%s on_time=%s 已存在未关闭记录 id=%d",
                        entity_id, on_time_str, existing_row[0],
                    )
                # 更新 JSON 指向已有记录
                if pending_json_path:
                    try:
                        _pending_json_update(pending_json_path, entity_id, existing_row[0],
                                             on_time_str, current_power, room)
                    except Exception:
                        pass
                continue

            # 插入新记录：on_time=当天00:00:00, off_time='', cross_day=1, room
            init_sa = item.get("init_state_attr", "")
            conn.execute(
                f"INSERT INTO {TABLE_DEVICE_HISTORY} "
                f"(entity_id, name, on_time, off_time, on_power, off_power, energy_consumed, duration, cross_day, room, state_attr) "
                f"VALUES (?, ?, ?, '', ?, NULL, NULL, NULL, 1, ?, ?)",
                (entity_id, name, on_time_str, current_power, room, init_sa),
            )
            new_record_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

            if local_logger:
                local_logger.info(
                    "[midnight] 拆分 entity_id=%s record_id=%d "
                    "on_time=%s off_time=%s off_power=%s cross_day=1 → new_id=%d",
                    entity_id, record_id, raw_on_time, off_time_str, current_power, new_record_id,
                )

            # 更新 pending JSON：record_id 和 on_time 变了
            if pending_json_path:
                try:
                    _pending_json_update(pending_json_path, entity_id, new_record_id,
                                         on_time_str, current_power, room)
                except Exception as exc:
                    if local_logger:
                        local_logger.warning("[attr] 写入附加字段失败 entity_id=%s: %s", entity_id, exc)

        conn.commit()
    except Exception as exc:
        if local_logger:
            local_logger.error("[midnight] 数据库拆分操作失败: %s", exc)
    finally:
        conn.close()


# =========================================================================== #
#  午夜拆分：异步主函数                                                          #
# =========================================================================== #
async def _async_midnight_split(hass: HomeAssistant, db_path: str) -> None:
    """午夜 00:00 执行：对所有未关闭的设备记录按天拆分。"""
    # 1. 获取所有未关闭记录（含 power_entity 配置）
    try:
        unclosed = await hass.async_add_executor_job(_get_unclosed_device_records, db_path)
    except Exception:
        local_logger = get_logger()
        if local_logger:
            local_logger.exception("[midnight] 获取未关闭记录失败")
        return

    if not unclosed:
        return

    local_logger = get_logger()
    tz = _get_timezone(hass)
    local_now = _get_local_now(tz)
    yesterday = local_now - timedelta(days=1)
    off_time_str = yesterday.strftime("%Y-%m-%d") + " 23:59:59"
    on_time_str = local_now.strftime("%Y-%m-%d") + " 00:00:00"

    if local_logger:
        local_logger.info(
            "[midnight] 午夜拆分开始 local_time=%s 未关闭记录=%d",
            local_now.strftime("%Y-%m-%d %H:%M:%S"), len(unclosed),
        )

    # 2. 读取当前电力值 + 设备开关状态（需在 async 上下文访问 hass.states）
    for item in unclosed:
        power_entity = item.get("power_entity", "") or ""
        entity_id = item["entity_id"]
        current_power = _read_current_power_value(hass, power_entity, entity_id)
        item["current_power"] = current_power
        # ── 方案二：读取设备当前是否开机 ──
        state = hass.states.get(entity_id)
        item["is_on"] = state is not None and _is_on_state(entity_id, state.state)

        # ★ 如果是气候实体且当前开机，记录当前状态用于新记录的 state_attr
        domain = entity_id.split(".", 1)[0] if "." in entity_id else ""
        if domain == "climate" and item["is_on"] and state:
            now_str = _get_local_now_str(tz)
            entry = _extract_climate_state_attr(state, now_str)
            item["init_state_attr"] = f"[{entry}]"
        else:
            item["init_state_attr"] = ""

    # 3. 获取 pending JSON 路径
    storage_dir = os.path.dirname(db_path)
    pending_json_path = _get_pending_json_path(storage_dir)

    # 4. 执行拆分
    try:
        await hass.async_add_executor_job(
            _do_midnight_splits, db_path, unclosed, off_time_str, on_time_str, pending_json_path,
        )
    except Exception:
        local_logger = get_logger()
        if local_logger:
            local_logger.exception("[midnight] 午夜拆分执行失败")


# =========================================================================== #
#  传感器类：写入一条记录（单指标）                                                 #
# =========================================================================== #
def _write_env_metric_record(
    db_path: str,
    entity_id: str,
    name: str,
    dt_str: str,
    metric_type: str,
    value: float | str | None,
    room: str = "",
) -> None:
    """向对应的指标分表写入一条记录。数值类型保留3位小数，sensor 类型原样存入。"""
    if value is not None and metric_type != METRIC_SENSOR:
        value = round(value, 3)

    tbl = get_env_table_name(metric_type)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            f"INSERT INTO {tbl} (entity_id, name, datetime, value, room) VALUES (?, ?, ?, ?, ?)",
            (entity_id, name, dt_str, value, room),
        )
        conn.commit()
        local_logger = get_logger()
        if local_logger:
            local_logger.info(
                "[env] 采集写入 entity_id=%s name=%s metric=%s value=%s table=%s room=%s dt=%s",
                entity_id, name or "N/A", metric_type, value, tbl, room or "N/A", dt_str,
            )
    except Exception as exc:
        local_logger = get_logger()
        if local_logger:
            local_logger.error("[env] 写入记录失败 entity_id=%s: %s", entity_id, exc)
    finally:
        conn.close()


# =========================================================================== #
#  属性提取：通用辅助函数                                                          #
# =========================================================================== #
def _extract_nested_value(attrs: dict, path: str) -> Any | None:
    """根据点号路径从属性字典中提取值，如 'power.total' → attrs['power']['total']。"""
    parts = path.split(".")
    value = attrs
    for part in parts:
        if isinstance(value, dict):
            value = value.get(part)
        else:
            return None
    return value


def _infer_sqlite_type(value: Any) -> str:
    """根据 Python 值推断 SQLite 列类型。"""
    if isinstance(value, bool):
        return "INTEGER"
    if isinstance(value, int):
        return "INTEGER"
    if isinstance(value, float):
        return "REAL"
    return "TEXT"


def _safe_column_name(name: str) -> str:
    """将字段名转为安全的 SQL 列名（替换点号为下划线，包裹在双引号中以防关键字冲突）。"""
    safe = name.replace(".", "_")
    return f'"{safe}"'


def _set_bridge_entity_sync(
    hass: HomeAssistant, entity_id: str, state: str, attributes: dict,
    device_id: str, unique_id: str,
) -> None:
    """创建或更新一个关联到设备的实体（同步版本，放线程池执行）。"""
    # 1. 用 entity_registry 注册实体并获取实际的 entity_id
    entity_registry = er.async_get(hass)
    entry = entity_registry.async_get_or_create(
        "sensor",
        DOMAIN,
        unique_id,
        device_id=device_id,
        suggested_object_id=entity_id.split(".", 1)[-1] if "." in entity_id else entity_id,
    )
    actual_entity_id = entry.entity_id

    # 2. 设置状态
    hass.states.async_set(actual_entity_id, state, attributes=attributes)


def _apply_decimal_places(row_data: dict, decimal_places: int) -> None:
    """对 row_data 中数值类型的值应用小数位数量化。decimal_places < 0 表示不限制。"""
    if decimal_places < 0:
        return
    for key, val in row_data.items():
        if isinstance(val, (int, float)) and key not in ("id", "entity_id", "name", "datetime", "room", "updated_at"):
            if decimal_places == 0:
                row_data[key] = int(round(val))
            else:
                row_data[key] = round(float(val), decimal_places)


# =========================================================================== #
#  属性提取：动态建表                                                              #
# =========================================================================== #
def _normalize_extra_fields(extra_fields: dict | None) -> dict:
    """将 extra_fields 统一为 {"src_path": {"target_col": "xxx"}} 格式。

    兼容旧格式: {"src_path": "target_col"} → {"target_col": "target_col"}。
    所有 extra_fields 条目均为独立列；JSON 节点由 extra_json_nodes 独立处理。
    """
    if not extra_fields:
        return {}
    result = {}
    for src_path, value in extra_fields.items():
        if isinstance(value, str):
            # 旧格式：值是目标列名字符串
            result[src_path] = {"target_col": value}
        elif isinstance(value, dict):
            target_col = value.get("target_col", src_path.replace(".", "_"))
            result[src_path] = {"target_col": target_col}
        else:
            result[src_path] = {"target_col": str(value)}
    return result


def _ensure_attr_table(db_path: str, type_name: str, field_mapping: dict,
                        extra_fields: dict | None = None) -> str:
    """确保 attr_{type_name} 表存在，不存在则根据 field_mapping 和 attr_type_defs.field_types 创建。

    field_mapping: {"源字段": "目标列名", ...}
    extra_fields: {"源路径": {"target_col": "目标列名"}, ...}
                   所有 extra_fields 条目均为独立列；extra_json 列始终创建（供 extra_json_nodes 使用）。
                   兼容旧格式: {"源路径": "目标列名"} → 默认 column 模式
    field_types 从 attr_type_defs 表中读取。
    返回表名。
    """
    tbl = get_attr_table_name(type_name)
    conn = sqlite3.connect(db_path)
    try:
        # 解析 extra_fields 为统一格式
        normalized_extra = _normalize_extra_fields(extra_fields)

        # 检查表是否已存在
        existing = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (tbl,),
            )
        ]
        if existing:
            # 表已存在：检查是否需要添加列
            existing_cols = {row[1] for row in conn.execute(f"PRAGMA table_info({tbl})")}
            if normalized_extra:
                for src_path, info in normalized_extra.items():
                    unquoted_name = info["target_col"].replace(".", "_")
                    if unquoted_name not in existing_cols:
                        col_type = "TEXT"
                        conn.execute(
                            f'ALTER TABLE {tbl} ADD COLUMN "{unquoted_name}" {col_type} NOT NULL DEFAULT ""'
                        )
            # 确保 extra_json 列存在（供 extra_json_nodes 使用）
            if EXTRA_JSON_COLUMN not in existing_cols:
                conn.execute(
                    f'ALTER TABLE {tbl} ADD COLUMN {EXTRA_JSON_COLUMN} TEXT NOT NULL DEFAULT ""'
                )
            conn.commit()
            return tbl

        # 从 attr_type_defs 读取 field_types
        field_types: dict = {}
        row = conn.execute(
            f"SELECT field_types FROM {TABLE_ATTR_TYPE_DEFS} WHERE type_name = ?",
            (type_name,),
        ).fetchone()
        if row and row[0]:
            try:
                field_types = json.loads(row[0])
            except json.JSONDecodeError:
                pass

        # 构建列定义：元数据列 + 属性字段列
        columns_defs = [
            "id INTEGER PRIMARY KEY AUTOINCREMENT",
            "entity_id TEXT NOT NULL",
            "name TEXT NOT NULL DEFAULT ''",
            "datetime TEXT NOT NULL DEFAULT ''",
            "room TEXT NOT NULL DEFAULT ''",
        ]
        valid_types = {"TEXT", "INTEGER", "REAL"}
        for target_col in field_mapping.values():
            safe_name = _safe_column_name(target_col)
            col_type = "REAL"  # 默认
            if field_types and target_col in field_types:
                ft = str(field_types[target_col]).upper()
                if ft in valid_types:
                    col_type = ft
            columns_defs.append(f"{safe_name} {col_type}")

        # 附加标量字段列：所有 extra_fields 均建独立列
        if normalized_extra:
            for src_path, info in normalized_extra.items():
                safe_name = _safe_column_name(info["target_col"])
                col_type = "TEXT"  # 附加字段默认 TEXT
                if field_types and info["target_col"] in field_types:
                    ft = str(field_types[info["target_col"]]).upper()
                    if ft in valid_types:
                        col_type = ft
                columns_defs.append(f"{safe_name} {col_type}")

        # extra_json 列：始终创建（供 extra_json_nodes 使用）
        columns_defs.append(f"{EXTRA_JSON_COLUMN} TEXT NOT NULL DEFAULT ''")

        # list 模式额外列
        columns_defs.append("updated_at TEXT NOT NULL DEFAULT ''")

        create_sql = f"CREATE TABLE {tbl} (\n    " + ",\n    ".join(columns_defs) + "\n);"
        conn.execute(create_sql)

        # 建索引
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS idx_{tbl}_entity_time "
            f"ON {tbl} (entity_id, datetime);"
        )
        conn.commit()

        local_logger = get_logger()
        if local_logger:
            local_logger.info("[attr] 创建属性数据表 table=%s columns=%d", tbl, len(columns_defs))
        return tbl
    finally:
        conn.close()


# =========================================================================== #
#  属性提取：核心采集逻辑                                                          #
# =========================================================================== #
def _attr_collect_for_entity(db_path: str, entity_id: str, cfg: dict, state_obj,
                              dt_str: str = "", force: bool = False) -> int:
    """对单个属性实体执行采集。

    cfg 包含: attr_type, mode, array_path, key_field, compare_limit, field_mapping, extra_fields, room
    dt_str: 采集时间字符串
    force: 为 True 时跳过去重，强制写入
    返回写入/更新的行数。
    """
    type_name = cfg.get("attr_type", "")
    if not type_name:
        return 0

    if not dt_str:
        dt_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    tbl = get_attr_table_name(type_name)
    mode = cfg.get("mode", ATTR_MODE_FIELDS)
    room = cfg.get("room", "")
    name = cfg.get("friendly_name", "") or (state_obj.attributes.get("friendly_name", "") if state_obj else "")
    attrs = dict(state_obj.attributes) if state_obj else {}
    # 同时支持从 state 提取：将 state 值注入 attrs 的特殊键
    state_value = state_obj.state if state_obj else None

    # 解析 field_mapping
    field_mapping_raw = cfg.get("field_mapping", "")
    if isinstance(field_mapping_raw, str):
        try:
            field_mapping = json.loads(field_mapping_raw) if field_mapping_raw else {}
        except json.JSONDecodeError:
            return 0
    elif isinstance(field_mapping_raw, dict):
        field_mapping = field_mapping_raw
    else:
        return 0

    if not field_mapping:
        return 0

    # 解析 extra_fields（附加标量字段，list/multi/fields 模式下附加到每行）
    extra_fields: dict = {}
    extra_fields_raw = cfg.get("extra_fields", "")
    if isinstance(extra_fields_raw, str):
        try:
            extra_fields = json.loads(extra_fields_raw) if extra_fields_raw else {}
        except json.JSONDecodeError:
            extra_fields = {}
    elif isinstance(extra_fields_raw, dict):
        extra_fields = extra_fields_raw

    # 将 extra_fields 统一为新格式
    normalized_extra = _normalize_extra_fields(extra_fields)

    # 读取小数位数配置
    decimal_places = 2
    try:
        conn2 = sqlite3.connect(db_path)
        row = conn2.execute(
            f"SELECT decimal_places FROM {TABLE_ATTR_TYPE_DEFS} WHERE type_name = ?",
            (type_name,),
        ).fetchone()
        if row and row[0] is not None:
            decimal_places = int(row[0])
        conn2.close()
    except Exception:
        pass

    # 确保表存在（传入 extra_fields 以便建列）
    _ensure_attr_table(db_path, type_name, field_mapping, normalized_extra or None)

    # 提取独立列附加字段的值（从父实体属性中提取）
    extra_column_values: dict = {}   # {target_col: value}
    if normalized_extra:
        for src_path, info in normalized_extra.items():
            target_col = info["target_col"]
            if src_path == "$state":
                val = state_value
            else:
                val = _extract_nested_value(attrs, src_path)
            extra_column_values[target_col] = val

    # 提取 JSON 节点的值（按节点动态采集，保留原始结构）
    extra_json_node_values: dict = {}   # {node_name: {dict/list值}}
    extra_json_nodes_raw = cfg.get("extra_json_nodes", "")
    if isinstance(extra_json_nodes_raw, str):
        try:
            extra_json_nodes_list = json.loads(extra_json_nodes_raw) if extra_json_nodes_raw else []
        except json.JSONDecodeError:
            extra_json_nodes_list = []
    elif isinstance(extra_json_nodes_raw, list):
        extra_json_nodes_list = extra_json_nodes_raw
    else:
        extra_json_nodes_list = []

    for node_path in extra_json_nodes_list:
        node_val = _extract_nested_value(attrs, node_path)
        if isinstance(node_val, (dict, list)):
            extra_json_node_values[node_path] = node_val

    local_logger = get_logger()
    written = 0

    conn = sqlite3.connect(db_path)
    try:
        if mode in (ATTR_MODE_LIST, ATTR_MODE_MULTI):
            # --- 列表展开模式（含 multi 模式的列表部分）---
            array_path = cfg.get("array_path", "")
            key_field = cfg.get("key_field", "")
            compare_limit = int(cfg.get("compare_limit", 30))

            array = _extract_nested_value(attrs, array_path) if array_path else None
            if not isinstance(array, list) or not array:
                if local_logger:
                    local_logger.warning(
                        "[attr] 数组路径无效或为空 entity_id=%s type=%s array_path=%s",
                        entity_id, type_name, array_path,
                    )
                return 0

            # 查询 DB 中全部记录用于去重（按 entity_id 过滤，数据量可控）
            key_target_col = field_mapping.get(key_field, key_field)
            conn.row_factory = sqlite3.Row
            existing_rows = conn.execute(
                f"SELECT * FROM {tbl} WHERE entity_id = ? ORDER BY datetime DESC",
                (entity_id,),
            ).fetchall()

            lookup: dict[str, dict] = {}
            for row in existing_rows:
                lookup[str(row[key_target_col])] = dict(row)

            # 首次采集：表中无数据，INSERT不带独立列，循环后仅给最新行追加
            # 后续采集：表中已有数据，INSERT/UPDATE随行写入独立列
            is_first_collect = len(lookup) == 0

            # 遍历数组元素
            # 独立列规则：首次采集只写最新行；后续采集只随新插入行写入，UPDATE已存在行时排除独立列
            # JSON规则：永远只写最新行
            latest_key_str = None
            extra_col_keys = set(extra_column_values.keys())

            for element in array:
                if not isinstance(element, dict):
                    continue

                key_value = _extract_nested_value(element, key_field)
                if key_value is None:
                    continue
                key_str = str(key_value)

                if latest_key_str is None or key_str > latest_key_str:
                    latest_key_str = key_str

                # 构建行数据
                row_data = {"entity_id": entity_id, "name": name, "room": room, "datetime": dt_str}
                for src_field, target_col in field_mapping.items():
                    row_data[target_col] = _extract_nested_value(element, src_field)
                _apply_decimal_places(row_data, decimal_places)

                is_new_row = key_str not in lookup

                # 独立列：仅新插入行时附带写入；UPDATE已存在行时排除
                if not is_first_collect and is_new_row:
                    row_data.update(extra_column_values)

                # --- force + 已存在 ---
                if force and not is_new_row:
                    existing = lookup[key_str]
                    changed = False
                    for target_col, new_val in row_data.items():
                        old_val = existing.get(target_col)
                        if old_val is None and new_val is None:
                            continue
                        if str(old_val) != str(new_val):
                            changed = True
                            break
                    if changed:
                        # UPDATE 时排除独立列，不回填历史行
                        update_data = {k: v for k, v in row_data.items() if k not in extra_col_keys}
                        if update_data:
                            conn.execute(
                                f"UPDATE {tbl} SET {', '.join([f'{_safe_column_name(c)} = ?' for c in update_data.keys()])} "
                                f"WHERE entity_id = ? AND {_safe_column_name(key_target_col)} = ?",
                                [update_data.get(c) for c in update_data.keys()] + [entity_id, key_str],
                            )
                        written += 1

                # --- force + 新数据 ---
                elif force and is_new_row:
                    columns = list(row_data.keys())
                    conn.execute(
                        f"INSERT INTO {tbl} ({', '.join([_safe_column_name(c) for c in columns])}) "
                        f"VALUES ({', '.join(['?' for _ in columns])})",
                        [row_data.get(c) for c in columns],
                    )
                    written += 1

                # --- 非force + 新数据 ---
                elif not force and is_new_row:
                    columns = list(row_data.keys())
                    conn.execute(
                        f"INSERT INTO {tbl} ({', '.join([_safe_column_name(c) for c in columns])}) "
                        f"VALUES ({', '.join(['?' for _ in columns])})",
                        [row_data.get(c) for c in columns],
                    )
                    written += 1

                # --- 非force + 已存在 ---
                else:
                    existing = lookup[key_str]
                    changed = False
                    for target_col, new_val in row_data.items():
                        old_val = existing.get(target_col)
                        if old_val is None and new_val is None:
                            continue
                        if str(old_val) != str(new_val):
                            changed = True
                            break
                    if changed:
                        # UPDATE 时排除独立列，不回填历史行
                        update_data = {k: v for k, v in row_data.items() if k not in extra_col_keys}
                        if update_data:
                            conn.execute(
                                f"UPDATE {tbl} SET {', '.join([f'{_safe_column_name(c)} = ?' for c in update_data.keys()])} "
                                f"WHERE entity_id = ? AND {_safe_column_name(key_target_col)} = ?",
                                [update_data.get(c) for c in update_data.keys()] + [entity_id, key_str],
                            )
                        written += 1

            # --- 首次采集：给最新行追加独立列 ---
            # --- 始终：给最新行追加 JSON ---
            if latest_key_str:
                try:
                    if is_first_collect and extra_column_values:
                        # 首次采集：独立列只写最新行，清空其他行
                        set_clauses = [
                            f"{_safe_column_name(c)} = ?" for c in extra_column_values.keys()
                        ]
                        conn.execute(
                            f"UPDATE {tbl} SET {', '.join(set_clauses)} "
                            f"WHERE entity_id = ? AND {_safe_column_name(key_target_col)} = ?",
                            list(extra_column_values.values()) + [entity_id, latest_key_str],
                        )
                        clear_clauses = [
                            f"{_safe_column_name(c)} = ''" for c in extra_column_values.keys()
                        ]
                        conn.execute(
                            f"UPDATE {tbl} SET {', '.join(clear_clauses)} "
                            f"WHERE entity_id = ? AND {_safe_column_name(key_target_col)} != ?",
                            (entity_id, latest_key_str),
                        )
                    if extra_json_node_values:
                        json_str = json.dumps(extra_json_node_values, ensure_ascii=False, default=str)
                        conn.execute(
                            f'UPDATE {tbl} SET {EXTRA_JSON_COLUMN} = ? '
                            f"WHERE entity_id = ? AND {_safe_column_name(key_target_col)} = ?",
                            (json_str, entity_id, latest_key_str),
                        )
                        conn.execute(
                            f"UPDATE {tbl} SET {EXTRA_JSON_COLUMN} = '' "
                            f"WHERE entity_id = ? AND {_safe_column_name(key_target_col)} != ?",
                            (entity_id, latest_key_str),
                        )
                except Exception as exc:
                    if local_logger:
                        local_logger.warning("[attr] 写入附加字段失败 entity_id=%s: %s", entity_id, exc)

        else:
            # --- 字段快照模式 ---
            # 判断是否首次采集（表中无该实体数据）
            is_first_collect_fields = True
            try:
                count = conn.execute(
                    f"SELECT COUNT(*) FROM {tbl} WHERE entity_id = ?",
                    (entity_id,),
                ).fetchone()[0]
                if count > 0:
                    is_first_collect_fields = False
            except Exception:
                pass

            row_data: dict[str, Any] = {
                "entity_id": entity_id, "name": name, "room": room, "datetime": dt_str,
            }
            for src_field, target_col in field_mapping.items():
                if src_field == "$state":
                    row_data[target_col] = state_value
                else:
                    row_data[target_col] = _extract_nested_value(attrs, src_field)
            _apply_decimal_places(row_data, decimal_places)
            # 非首次采集：独立列随新行写入
            if not is_first_collect_fields:
                row_data.update(extra_column_values)

            # 去重：与最近一条记录对比，仅对比主字段（排除独立列）
            extra_col_keys = set(extra_column_values.keys())
            if not force:
                try:
                    conn.row_factory = sqlite3.Row
                    last_row = conn.execute(
                        f"SELECT * FROM {tbl} WHERE entity_id = ? ORDER BY datetime DESC LIMIT 1",
                        (entity_id,),
                    ).fetchone()
                    if last_row:
                        last_dict = dict(last_row)
                        changed = False
                        for target_col, new_val in row_data.items():
                            if target_col == "datetime" or target_col == "id":
                                continue
                            if target_col in extra_col_keys:
                                continue  # 排除独立列
                            old_val = last_dict.get(target_col)
                            if old_val is None and new_val is None:
                                continue
                            if str(old_val) != str(new_val):
                                changed = True
                                break
                        if not changed:
                            return 0  # 无变化，跳过
                except Exception as exc:
                    if local_logger:
                        local_logger.warning("[attr] 写入附加字段失败 entity_id=%s: %s", entity_id, exc)  # 首次采集或表不存在时继续
            # force 模式：跳过去重，直接写入

            columns = list(row_data.keys())
            placeholders = ", ".join(["?" for _ in columns])
            col_names = ", ".join([_safe_column_name(c) for c in columns])
            values = [row_data.get(c) for c in columns]
            conn.execute(
                f"INSERT INTO {tbl} ({col_names}) VALUES ({placeholders})",
                values,
            )
            written += 1

            # --- fields 模式下附加字段 ---
            # 首次采集：仅给最新行追加独立列+JSON
            # 后续采集：独立列已随新行写入，仅追加JSON到最新行
            if extra_column_values or extra_json_node_values:
                try:
                    last_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                    # 首次采集：独立列只写最新行
                    if is_first_collect_fields and extra_column_values:
                        set_clauses = [
                            f"{_safe_column_name(c)} = ?" for c in extra_column_values.keys()
                        ]
                        conn.execute(
                            f"UPDATE {tbl} SET {', '.join(set_clauses)} "
                            f"WHERE entity_id = ? AND id = ?",
                            list(extra_column_values.values()) + [entity_id, last_id],
                        )
                        clear_clauses = [
                            f"{_safe_column_name(c)} = ''" for c in extra_column_values.keys()
                        ]
                        conn.execute(
                            f"UPDATE {tbl} SET {', '.join(clear_clauses)} "
                            f"WHERE entity_id = ? AND id != ?",
                            (entity_id, last_id),
                        )
                    # JSON 合并附加字段：始终仅最新行
                    if extra_json_node_values:
                        json_str = json.dumps(extra_json_node_values, ensure_ascii=False, default=str)
                        conn.execute(
                            f'UPDATE {tbl} SET {EXTRA_JSON_COLUMN} = ? '
                            f"WHERE entity_id = ? AND id = ?",
                            (json_str, entity_id, last_id),
                        )
                        conn.execute(
                            f"UPDATE {tbl} SET {EXTRA_JSON_COLUMN} = '' "
                            f"WHERE entity_id = ? AND id != ?",
                            (entity_id, last_id),
                        )
                except Exception as exc:
                    if local_logger:
                        local_logger.warning("[attr] 写入附加字段失败 entity_id=%s: %s", entity_id, exc)

        conn.commit()

        if local_logger and written > 0:
            local_logger.info(
                "[attr] 采集完成 entity_id=%s type=%s mode=%s written=%d",
                entity_id, type_name, mode, written,
            )
        return written
    except Exception as exc:
        if local_logger:
            local_logger.error("[attr] 采集异常 entity_id=%s: %s", entity_id, exc)
        return 0
    finally:
        conn.close()


# =========================================================================== #
#  属性提取：获取属性实体配置                                                       #
# =========================================================================== #
def _get_all_attr_entities(db_path: str, collect_mode: str | None = None) -> list[dict]:
    """获取所有启用的属性提取实体配置。

    返回包含 attr_type_defs 关联信息的列表。
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        where_clause = (
            f"WHERE ec.enabled = 1 AND ec.category = '{CATEGORY_ATTRIBUTE}' "
            f"AND ec.collect_mode = ?"
            if collect_mode
            else f"WHERE ec.enabled = 1 AND ec.category = '{CATEGORY_ATTRIBUTE}'"
        )
        query = (
            f"SELECT ec.entity_id, ec.attr_type, ec.collect_mode, ec.collect_interval, "
            f"  ec.round_minute, ec.friendly_name, ec.room, "
            f"  atd.mode, atd.array_path, atd.key_field, atd.compare_limit, atd.field_mapping, "
            f"  atd.field_types, atd.decimal_places, atd.extra_fields, atd.extra_json_nodes "
            f"FROM {TABLE_ENTITY_CONFIGS} ec "
            f"JOIN {TABLE_ATTR_TYPE_DEFS} atd ON ec.attr_type = atd.type_name "
            + where_clause
        )
        params = (collect_mode,) if collect_mode else ()
        cursor = conn.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]
    except Exception:
        return []
    finally:
        conn.close()


# =========================================================================== #
#  属性提取：定时轮询                                                              #
# =========================================================================== #
async def _async_attr_poll(hass: HomeAssistant, db_path: str, now=None) -> None:
    """每分钟执行一次：检查哪些 poll 模式的属性实体到了采集时间。"""
    now_ts = time.time()

    try:
        attr_entities = await hass.async_add_executor_job(
            _get_all_attr_entities, db_path, COLLECT_MODE_POLL,
        )
    except Exception:
        local_logger = get_logger()
        if local_logger:
            local_logger.exception("[attr] 获取属性实体列表失败")
        return

    if not attr_entities:
        return

    tz = _get_timezone(hass)
    dt_str = _get_local_now_str(tz)

    # 按类型累计本次采集写入数
    poll_stats: dict[str, int] = {}

    for ent in attr_entities:
        entity_id = ent["entity_id"]
        attr_type = ent.get("attr_type", "")
        interval_min = int(ent.get("collect_interval", 30))
        round_minute = int(ent.get("round_minute", 0))
        dedup_key = f"{entity_id}|{attr_type}"
        local_logger = get_logger()

        if round_minute:
            # 整分钟采集：只在分钟数为 interval 的整倍数时采集
            if interval_min < 1:
                interval_min = 1
            current_minute = datetime.now().minute
            if current_minute % interval_min != 0:
                continue
            # 防止同一分钟重复采集
            last_minute = hass.data[DOMAIN].setdefault("last_attr_poll_minute", {}).get(dedup_key, -1)
            if last_minute == current_minute:
                if local_logger:
                    local_logger.info("[attr] 跳过 %s (本分钟已采集) dedup_key=%s", entity_id, dedup_key)
                continue
        else:
            # 普通间隔采集
            last_ts = hass.data[DOMAIN].get("last_attr_poll", {}).get(dedup_key, 0)
            if now_ts - last_ts < interval_min * 60:
                if local_logger:
                    local_logger.debug("[attr] 跳过 %s (距上次采集 %.0fs < %dmin) dedup_key=%s",
                                       entity_id, now_ts - last_ts, interval_min, dedup_key)
                continue

        state_obj = hass.states.get(entity_id)
        if not state_obj:
            continue

        try:
            # 把 datetime 注入到 state_obj 的临时属性中（供 _attr_collect_for_entity 使用）
            cfg = dict(ent)

            written = await hass.async_add_executor_job(
                _attr_collect_for_entity, db_path, entity_id, cfg, state_obj, dt_str,
            )
            if written:
                atype = ent.get("attr_type", "")
                if atype:
                    poll_stats[atype] = poll_stats.get(atype, 0) + written
        except Exception:
            local_logger = get_logger()
            if local_logger:
                local_logger.exception("[attr] 属性轮询写入异常 entity_id=%s", entity_id)

        # 更新最后采集时间
        hass.data[DOMAIN].setdefault("last_attr_poll", {})[dedup_key] = now_ts
        if round_minute:
            hass.data[DOMAIN].setdefault("last_attr_poll_minute", {})[dedup_key] = datetime.now().minute

        if local_logger:
            local_logger.info("[attr] 采集完成 %s attr_type=%s written=%s dedup_key=%s",
                              entity_id, attr_type, written, dedup_key)

    # 存储本次轮询的按类型统计
    if poll_stats:
        stats = hass.data[DOMAIN].get("_attr_trigger_stats", {})
        for atype, count in poll_stats.items():
            stats[atype] = {"count": count, "time": dt_str}
        hass.data[DOMAIN]["_attr_trigger_stats"] = stats


# =========================================================================== #
#  属性提取：手动触发采集                                                          #
# =========================================================================== #
async def _async_attr_manual_trigger(hass: HomeAssistant, db_path: str,
                                     type_name: str = "") -> dict:
    """手动触发属性采集（立即执行，不受间隔限制）。

    type_name 非空时仅触发该类型下的所有实体，为空则触发全部。
    返回 {"triggered": N, "written": N}。
    """
    local_logger = get_logger()
    tz = _get_timezone(hass)
    dt_str = _get_local_now_str(tz)
    written_total = 0
    triggered = 0

    if local_logger:
        local_logger.info("[attr] 手动触发开始 type_name=%s", type_name or "_all")

    try:
        all_configs = await hass.async_add_executor_job(
            _get_all_attr_entities, db_path,
        )
        if type_name:
            configs = [c for c in all_configs if c.get("attr_type") == type_name]
        else:
            configs = all_configs
    except Exception:
        if local_logger:
            local_logger.exception("[attr] 手动触发获取实体列表失败")
        return {"triggered": 0, "written": 0, "error": "获取实体列表失败"}

    if local_logger:
        local_logger.info(
            "[attr] 手动触发 待处理实体数=%d type_name=%s",
            len(configs), type_name or "_all",
        )

    for cfg in configs:
        eid = cfg.get("entity_id", "")
        if not eid:
            continue
        state_obj = hass.states.get(eid)
        if not state_obj or state_obj.state in ("unavailable", "unknown", None):
            continue
        try:
            written = await hass.async_add_executor_job(
                _attr_collect_for_entity, db_path, eid, cfg, state_obj, dt_str, True,
            )
            written_total += written
        except Exception:
            if local_logger:
                local_logger.exception("[attr] 手动触发采集异常 entity_id=%s", eid)
        triggered += 1

    # 存储上次触发统计到 hass.data
    if not hass.data.get(DOMAIN):
        hass.data[DOMAIN] = {}
    stats = hass.data[DOMAIN].get("_attr_trigger_stats", {})
    if type_name or not type_name:
        label = type_name or "_all"
        stats[label] = {"count": written_total, "time": dt_str}
    hass.data[DOMAIN]["_attr_trigger_stats"] = stats

    if local_logger:
        local_logger.info(
            "[attr] 手动触发结束 type_name=%s triggered=%d written=%d",
            type_name or "_all", triggered, written_total,
        )

    return {"triggered": triggered, "written": written_total}


# =========================================================================== #
#  属性提取：事件触发                                                              #
# =========================================================================== #
async def _async_attr_event(hass: HomeAssistant, db_path: str, entity_id: str,
                             old_state, new_state) -> None:
    """处理 event 模式属性实体的状态变化事件。"""
    if not new_state:
        return

    # 检查 attributes 是否有变化
    if old_state and new_state.attributes == old_state.attributes:
        return  # 无变化，跳过

    # 获取该实体配置
    try:
        info = await hass.async_add_executor_job(_get_entity_info, db_path, entity_id)
    except Exception:
        return

    if not info or not info["enabled"]:
        return

    category = info.get("category", "")
    if category != CATEGORY_ATTRIBUTE:
        return

    collect_mode = info.get("collect_mode", COLLECT_MODE_POLL)
    if collect_mode != COLLECT_MODE_EVENT:
        return  # poll 模式由定时轮询处理

    attr_type = info.get("attr_type", "")
    if not attr_type:
        return

    # 获取类型定义
    def _get_type_def() -> dict | None:
        conn = sqlite3.connect(db_path)
        try:
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                f"SELECT * FROM {TABLE_ATTR_TYPE_DEFS} WHERE type_name = ?",
                (attr_type,),
            )
            row = cursor.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    type_def = await hass.async_add_executor_job(_get_type_def)
    if not type_def:
        return

    # 合并配置
    cfg = {
        "attr_type": attr_type,
        "mode": type_def.get("mode", ATTR_MODE_FIELDS),
        "array_path": type_def.get("array_path", ""),
        "key_field": type_def.get("key_field", ""),
        "compare_limit": type_def.get("compare_limit", 30),
        "field_mapping": type_def.get("field_mapping", ""),
        "extra_fields": type_def.get("extra_fields", ""),
        "extra_json_nodes": type_def.get("extra_json_nodes", ""),
        "room": info.get("room", ""),
        "friendly_name": info.get("friendly_name", ""),
        "collect_interval": info.get("collect_interval", 30),
    }

    tz = _get_timezone(hass)
    dt_str = _get_local_now_str(tz)

    local_logger = get_logger()
    if local_logger:
        local_logger.info(
            "[attr] 事件触发 entity_id=%s type=%s mode=%s dt=%s",
            entity_id, attr_type, cfg["mode"], dt_str,
        )

    try:
        await hass.async_add_executor_job(
            _attr_collect_for_entity, db_path, entity_id, cfg, new_state, dt_str,
        )
    except Exception:
        if local_logger:
            local_logger.exception("[attr] 事件写入异常 entity_id=%s", entity_id)


# =========================================================================== #
#  实体导出：将 HA 实体状态写入 JSON 文件                                          #
# =========================================================================== #
async def _async_export_entity(hass: HomeAssistant, db_path: str, entity_id: str,
                                new_state) -> None:
    """将实体状态 + 属性导出为 JSON 文件到 config/storage/export_entities/。"""
    try:
        info = await hass.async_add_executor_job(_get_entity_info, db_path, entity_id)
    except Exception:
        return

    if not info or not info["enabled"]:
        return

    # 读取导出配置
    def _get_export_config() -> dict | None:
        conn = sqlite3.connect(db_path)
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                f"SELECT * FROM {TABLE_EXPORT_CONFIGS} WHERE entity_id = ? AND enabled = 1",
                (entity_id,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    export_cfg = await hass.async_add_executor_job(_get_export_config)
    if not export_cfg:
        return

    export_dir = os.path.join(hass.config.config_dir, "storage", "export_entities")
    file_name = export_cfg.get("file_name", "") or f"{entity_id.replace('.', '_')}.json"
    file_path = os.path.join(export_dir, file_name)

    tz = _get_timezone(hass)
    last_updated = ""
    if new_state.last_updated:
        last_updated = (new_state.last_updated + timedelta(hours=tz)).isoformat()

    export_data = {
        "entity_id": entity_id,
        "state": new_state.state,
        "attributes": dict(new_state.attributes),
        "last_updated": last_updated,
    }

    def _write_json() -> None:
        os.makedirs(export_dir, exist_ok=True)
        tmp_path = file_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(export_data, f, ensure_ascii=False, indent=2, default=str)
        os.replace(tmp_path, file_path)

    await hass.async_add_executor_job(_write_json)

    local_logger = get_logger()
    if local_logger:
        local_logger.info("[export] 实体已导出 entity_id=%s file=%s", entity_id, file_name)


# =========================================================================== #
#  文件源：JSON 文件 → HA 实体                                                   #
# =========================================================================== #
async def _async_file_source_poll(hass: HomeAssistant, db_path: str) -> None:
    """每 5 秒检查已配置文件源 mtime，变化时重新加载并更新实体。"""
    def _get_configs() -> list[dict]:
        conn = sqlite3.connect(db_path)
        try:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute(
                f"SELECT * FROM {TABLE_FILE_SOURCE_CONFIGS} WHERE enabled = 1"
            ).fetchall()]
        finally:
            conn.close()

    try:
        configs = await hass.async_add_executor_job(_get_configs)
    except Exception:
        return

    if not configs:
        return

    tz = _get_timezone(hass)
    dt_str = _get_local_now_str(tz)
    now_ts = time.time()

    for cfg in configs:
        cfg_id = cfg["id"]
        interval = max(1, int(cfg.get("poll_interval", 10)))
        last_check = hass.data[DOMAIN].setdefault("last_file_check", {}).get(cfg_id, 0)
        if now_ts - last_check < interval:
            continue

        hass.data[DOMAIN].setdefault("last_file_check", {})[cfg_id] = now_ts

        file_path = cfg["file_path"]
        prefix = cfg.get("entity_prefix", "sensor.file_")
        state_field = cfg.get("state_field", "")
        last_mtime = cfg.get("last_mtime") or 0

        def _check_and_load() -> bool:
            conn = sqlite3.connect(db_path)
            try:
                try:
                    mtime = os.path.getmtime(file_path)
                except OSError:
                    return False

                if mtime <= last_mtime:
                    return False

                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)

                # 更新 mtime
                conn.execute(
                    f"UPDATE {TABLE_FILE_SOURCE_CONFIGS} SET last_mtime = ? WHERE id = ?",
                    (mtime, cfg_id),
                )
                conn.commit()
                return True
            except Exception:
                return False
            finally:
                conn.close()

        changed = await hass.async_add_executor_job(_check_and_load)
        if not changed:
            continue

        # 读取最新 JSON（不在 executor 里读，避免重复）
        def _load_data():
            with open(file_path, "r", encoding="utf-8") as f:
                return json.load(f)

        try:
            data = await hass.async_add_executor_job(_load_data)
        except Exception:
            continue

        # 创建/获取设备
        device_id = cfg.get("device_id", "")
        device_registry = dr.async_get(hass)
        entry_id = hass.data.get(DOMAIN, {}).get("entry_id")
        ident = (DOMAIN, f"file_src_{cfg_id}")
        if not device_id:
            device = device_registry.async_get_or_create(
                config_entry_id=entry_id,
                identifiers={ident},
                name=cfg.get("name") or f"文件源: {os.path.basename(file_path)}",
                manufacturer="HA数据统一存储系统",
                model="JSON 文件源",
            )
            device_id = device.id

            def _save_device_id():
                conn = sqlite3.connect(db_path)
                try:
                    conn.execute(
                        f"UPDATE {TABLE_FILE_SOURCE_CONFIGS} SET device_id = ? WHERE id = ?",
                        (device_id, cfg_id),
                    )
                    conn.commit()
                finally:
                    conn.close()

            await hass.async_add_executor_job(_save_device_id)
        else:
            device_registry.async_get_or_create(
                config_entry_id=entry_id,
                identifiers={ident},
            )

        file_name = os.path.splitext(os.path.basename(file_path))[0]
        safe_name = file_name.replace(".", "_").replace(" ", "_")

        if isinstance(data, list):
            eid = f"{prefix}{safe_name}"
            wrapped = {"data": data}
            if state_field and len(data) > 0 and isinstance(data[0], dict):
                state_val = str(_extract_nested_value(data[0], state_field))
            else:
                state_val = str(len(data))
            _set_bridge_entity_sync(
                hass, eid, state_val, wrapped, device_id,
                f"file_src_{cfg_id}",
            )
        elif isinstance(data, dict):
            eid = f"{prefix}{safe_name}"
            state_val = str(_extract_nested_value(data, state_field)) if state_field else ""
            _set_bridge_entity_sync(
                hass, eid, state_val, data, device_id,
                f"file_src_{cfg_id}",
            )

        local_logger = get_logger()
        if local_logger:
            local_logger.info("[filesrc] 文件源已刷新 file=%s prefix=%s", file_path, prefix)


# =========================================================================== #
#  API 源：网络 API → HA 实体                                                    #
# =========================================================================== #
async def _async_api_source_poll(hass: HomeAssistant, db_path: str) -> None:
    """周期性请求外部 API，将 JSON 响应映射为 HA 实体。"""
    import aiohttp

    def _get_configs() -> list[dict]:
        conn = sqlite3.connect(db_path)
        try:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute(
                f"SELECT * FROM {TABLE_API_SOURCE_CONFIGS} WHERE enabled = 1"
            ).fetchall()]
        finally:
            conn.close()

    try:
        configs = await hass.async_add_executor_job(_get_configs)
    except Exception:
        return

    if not configs:
        return

    tz = _get_timezone(hass)
    dt_str = _get_local_now_str(tz)
    now_ts = time.time()

    for cfg in configs:
        cfg_id = cfg["id"]
        interval = max(1, int(cfg.get("poll_interval", 60)))
        last_check = hass.data[DOMAIN].setdefault("last_api_check", {}).get(cfg_id, 0)
        if now_ts - last_check < interval:
            continue

        hass.data[DOMAIN].setdefault("last_api_check", {})[cfg_id] = now_ts

        url = cfg["url"].strip()
        if not url:
            continue

        method = cfg.get("method", "GET").upper()
        state_field = cfg.get("state_field", "")
        prefix = cfg.get("entity_prefix", "sensor.api_")
        timeout = int(cfg.get("timeout", 15))
        max_retries = int(cfg.get("max_retries", 5))
        headers_raw = cfg.get("headers_json", "")

        headers = {}
        if headers_raw:
            try:
                headers = json.loads(headers_raw)
            except json.JSONDecodeError:
                pass

        local_logger = get_logger()

        data = None
        for attempt in range(max_retries):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.request(
                        method, url, headers=headers,
                        timeout=aiohttp.ClientTimeout(total=timeout),
                    ) as resp:
                        if resp.status != 200:
                            raise Exception(f"HTTP {resp.status}")
                        data = await resp.json()
                break
            except Exception as exc:
                if local_logger:
                    local_logger.warning(
                        "[apisrc] 请求失败 (第%d次) url=%s: %s", attempt + 1, url, exc,
                    )
                if attempt < max_retries - 1:
                    await hass.async_add_executor_job(time.sleep, 1)

        if data is None:
            # 全部重试失败

            def _inc_fail() -> int:
                conn = sqlite3.connect(db_path)
                try:
                    conn.execute(
                        f"UPDATE {TABLE_API_SOURCE_CONFIGS} SET fail_count = fail_count + 1, "
                        f"  updated_at = ? WHERE id = ?",
                        (dt_str, cfg_id),
                    )
                    conn.commit()
                    row = conn.execute(
                        f"SELECT fail_count FROM {TABLE_API_SOURCE_CONFIGS} WHERE id = ?",
                        (cfg_id,),
                    ).fetchone()
                    return row[0] if row else 0
                finally:
                    conn.close()

            fc = await hass.async_add_executor_job(_inc_fail)
            if local_logger and fc >= 5:
                local_logger.error(
                    "[apisrc] API 连续失败 %d 次，已标记异常 url=%s prefix=%s",
                    fc, url, prefix,
                )
            # 更新实体为不可用
            eid = f"{prefix}error"
            hass.states.async_set(eid, "unavailable", attributes={
                "error": f"请求失败 {fc} 次", "url": url,
            })
            continue

        # 成功，重置失败计数

        def _reset_fail():
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    f"UPDATE {TABLE_API_SOURCE_CONFIGS} SET fail_count = 0, "
                    f"  updated_at = ? WHERE id = ?",
                    (dt_str, cfg_id),
                )
                conn.commit()
            finally:
                conn.close()

        await hass.async_add_executor_job(_reset_fail)

        # 创建/获取设备
        device_id = cfg.get("device_id", "")
        device_registry = dr.async_get(hass)
        entry_id = hass.data.get(DOMAIN, {}).get("entry_id")
        ident = (DOMAIN, f"api_src_{cfg_id}")
        if not device_id:
            device = device_registry.async_get_or_create(
                config_entry_id=entry_id,
                identifiers={ident},
                name=cfg.get("name") or f"API源: {url[:50]}",
                manufacturer="HA数据统一存储系统",
                model="网络 API 源",
            )
            device_id = device.id

            def _save_api_device_id():
                conn = sqlite3.connect(db_path)
                try:
                    conn.execute(
                        f"UPDATE {TABLE_API_SOURCE_CONFIGS} SET device_id = ? WHERE id = ?",
                        (device_id, cfg_id),
                    )
                    conn.commit()
                finally:
                    conn.close()

            await hass.async_add_executor_job(_save_api_device_id)
        else:
            device_registry.async_get_or_create(
                config_entry_id=entry_id,
                identifiers={ident},
            )

        # 创建实体
        if isinstance(data, list):
            eid = prefix.rstrip("_")
            wrapped = {"data": data}
            if state_field and len(data) > 0 and isinstance(data[0], dict):
                state_val = str(_extract_nested_value(data[0], state_field))
            else:
                state_val = str(len(data))
            _set_bridge_entity_sync(
                hass, eid, state_val, wrapped, device_id,
                f"api_src_{cfg_id}",
            )
        elif isinstance(data, dict):
            eid = prefix.rstrip("_")
            state_val = str(_extract_nested_value(data, state_field)) if state_field else ""
            _set_bridge_entity_sync(
                hass, eid, state_val, data, device_id,
                f"api_src_{cfg_id}",
            )

        if local_logger:
            local_logger.info("[apisrc] API 已刷新 url=%s prefix=%s", url, prefix)


# =========================================================================== #
async def _async_state_changed(hass: HomeAssistant, db_path: str, event: Event) -> None:
    entity_id = event.data.get("entity_id")
    old_state = event.data.get("old_state")
    new_state = event.data.get("new_state")

    if not entity_id or not new_state:
        return

    try:
        info = await hass.async_add_executor_job(_get_entity_info, db_path, entity_id)
    except RuntimeError as exc:
        # HA 关闭时 executor 已 shutdown，静默跳过
        if "shutdown" in str(exc).lower():
            return
        local_logger = get_logger()
        if local_logger:
            local_logger.exception("[device] 查询实体配置异常 entity_id=%s", entity_id)
        return
    except Exception:
        local_logger = get_logger()
        if local_logger:
            local_logger.exception("[device] 查询实体配置异常 entity_id=%s", entity_id)
        return

    if not info or not info["enabled"]:
        return

    # 分类处理
    category = info.get("category", CATEGORY_DEVICE)

    if category == CATEGORY_ATTRIBUTE:
        collect_mode = info.get("collect_mode", COLLECT_MODE_POLL)
        if collect_mode == COLLECT_MODE_EVENT:
            # 事件模式的属性提取：在 state_changed 中即时处理
            await _async_attr_event(hass, db_path, entity_id, old_state, new_state)
        return  # 属性实体不由 device 逻辑处理

    if category != CATEGORY_DEVICE:
        return  # 传感器类由定时轮询处理

    name = info.get("device_name", "") or info.get("friendly_name", "") or new_state.attributes.get("friendly_name", "")
    power_entity = info.get("power_entity", "")
    room = info.get("room", "")
    tz = _get_timezone(hass)
    now_str = _get_local_now_str(tz)
    new_state_val = new_state.state

    local_logger = get_logger()
    if local_logger:
        local_logger.info(
            "[device] 状态变化 entity_id=%s old_state=%s new_state=%s "
            "power_entity=%s name=%s room=%s category=device",
            entity_id, old_state.state if old_state else "N/A", new_state_val,
            power_entity or "N/A", name or "N/A", room or "N/A",
        )

    # 读取电量值：优先从 power_entity 配置的传感器读取
    def _get_power_value() -> float | None:
        if not power_entity:
            return _extract_power_reading(new_state)
        # 从配置的电量传感器读取
        p_state = hass.states.get(power_entity)
        if p_state and p_state.state not in ("unavailable", "unknown", None):
            try:
                return round(float(p_state.state), 2)
            except (ValueError, TypeError):
                pass
        # 回退：从设备自身属性尝试
        return _extract_power_reading(new_state)

    is_on = _is_on_state(entity_id, new_state_val)
    is_off = _is_off_state(entity_id, new_state_val)

    # pending JSON 路径
    storage_dir = os.path.dirname(db_path)
    pending_json_path = _get_pending_json_path(storage_dir)

    if is_on:
        # ★ 如果旧状态也是 on，说明只是模式/温度/速度等属性变化（非真正开机）
        if old_state and _is_on_state(entity_id, old_state.state):
            # ★ 空调设备：HVAC 模式或目标温度变化时记录到 state_attr
            if _get_entity_domain(entity_id) == "climate":
                old_attrs = old_state.attributes if old_state else {}
                old_temp = old_attrs.get("temperature")
                new_temp = new_state.attributes.get("temperature") if new_state.attributes else None
                temp_changed = old_temp is not None and old_temp != new_temp
                if old_state.state != new_state_val or temp_changed:
                    entry = _extract_climate_state_attr(new_state, now_str)
                    await hass.async_add_executor_job(
                        _append_state_attr_to_record, db_path, entity_id, entry,
                    )
            return  # 非开关动作，跳过后续流程

        # 预检测：今日是否有未关闭的旧记录，有则先修正再插入新记录
        today_prefix = now_str[:10]
        def _check_unclosed():
            conn = sqlite3.connect(db_path)
            try:
                cursor = conn.execute(
                    f"SELECT id, on_time, on_power, room FROM {TABLE_DEVICE_HISTORY} "
                    f"WHERE entity_id = ? AND off_time = '' AND on_time LIKE ? || '%' "
                    f"ORDER BY on_time ASC",
                    (entity_id, today_prefix),
                )
                return cursor.fetchall()
            finally:
                conn.close()
        unclosed = await hass.async_add_executor_job(_check_unclosed)
        if unclosed:
            await _async_correct_unclosed(hass, db_path, entity_id, unclosed, tz)

        # ★ 修正后再检查是否有任意未关闭记录（不限日期，重启后可能 on_time 不是今天）
        def _recheck_unclosed():
            conn = sqlite3.connect(db_path)
            try:
                cursor = conn.execute(
                    f"SELECT id FROM {TABLE_DEVICE_HISTORY} "
                    f"WHERE entity_id = ? AND (off_time = '' OR off_time IS NULL) "
                    f"LIMIT 1",
                    (entity_id,),
                )
                return cursor.fetchone()
            finally:
                conn.close()
        after_unclosed = await hass.async_add_executor_job(_recheck_unclosed)

        if after_unclosed:
            # 修正后仍有未关闭记录 → 设备之前就是开机状态，此次是重启后的重复事件，跳过
            local_logger = get_logger()
            if local_logger:
                local_logger.info(
                    "[device] 重启后跳过重复开机 entity_id=%s (已有未关闭记录)", entity_id,
                )
            return

        on_power = _get_power_value()
        local_logger = get_logger()
        if local_logger:
            local_logger.info(
                "[device] 设备开机 entity_id=%s on_time=%s on_power=%s name=%s room=%s",
                entity_id, now_str, on_power or "N/A", name or "N/A", room or "N/A",
            )

        # 提取初始 climate state_attr
        init_state_attr = ""
        if _get_entity_domain(entity_id) == "climate":
            entry = _extract_climate_state_attr(new_state, now_str)
            init_state_attr = f"[{entry}]"

        try:
            await hass.async_add_executor_job(
                _insert_device_on_record, db_path, entity_id, name, now_str, on_power, room,
                pending_json_path, init_state_attr,
            )
        except Exception:
            local_logger = get_logger()
            if local_logger:
                local_logger.exception("[device] 写入开机记录异常 entity_id=%s", entity_id)
    elif is_off:
        # ★ 如果旧状态也是 off，说明只是关闭状态下的属性变化（非真正关机），忽略
        if old_state and _is_off_state(entity_id, old_state.state):
            local_logger = get_logger()
            if local_logger:
                local_logger.info(
                    "[device] 忽略关闭态属性变化 entity_id=%s old=%s new=%s (非开关动作)",
                    entity_id, old_state.state, new_state_val,
                )
            return

        # ★ 关机前先追加末态到 state_attr（仅空调设备）
        if _get_entity_domain(entity_id) == "climate":
            entry = _extract_climate_state_attr(new_state, now_str)
            await hass.async_add_executor_job(
                _append_state_attr_to_record, db_path, entity_id, entry,
            )

        off_power = _get_power_value()

        local_logger = get_logger()
        if local_logger:
            local_logger.info(
                "[device] 设备关机 entity_id=%s off_time=%s off_power=%s name=%s room=%s",
                entity_id, now_str, off_power or "N/A", name or "N/A", room or "N/A",
            )
        try:
            await hass.async_add_executor_job(
                _update_device_off_record, db_path, entity_id, now_str, off_power,
                pending_json_path,
            )
        except Exception:
            local_logger = get_logger()
            if local_logger:
                local_logger.exception("[device] 更新关机记录异常 entity_id=%s", entity_id)

    # 实体导出检查：所有实体状态变化都触发（含设备、环境、属性类）
    try:
        await _async_export_entity(hass, db_path, entity_id, new_state)
    except Exception:
        pass


# =========================================================================== #
#  传感器类定时轮询                                                                #
# =========================================================================== #
async def _async_env_poll(hass: HomeAssistant, db_path: str, now=None) -> None:
    """每分钟执行一次：检查哪些环境实体到了采集时间。"""
    now_ts = time.time()

    try:
        env_entities = await hass.async_add_executor_job(_get_all_env_entities, db_path)
    except Exception:
        local_logger = get_logger()
        if local_logger:
            local_logger.exception("[env] 获取环境实体列表失败")
        return

    if not env_entities:
        return

    for ent in env_entities:
        entity_id = ent["entity_id"]
        metric_type = ent["metric_type"]
        interval_min = ent.get("collect_interval", 30)
        round_minute = int(ent.get("round_minute", 0))
        name = ent.get("friendly_name", "")
        room = ent.get("room", "")

        if not metric_type:
            continue

        last_ts = hass.data[DOMAIN].get("last_poll", {}).get(entity_id, 0)

        if round_minute:
            # 整分钟采集：只在分钟数为 interval 的整倍数时采集
            now_dt = datetime.now()
            current_minute = now_dt.minute
            if interval_min < 1:
                interval_min = 1
            if current_minute % interval_min != 0:
                continue
            # 防止同一分钟重复采集
            last_minute = hass.data[DOMAIN].setdefault("last_poll_minute", {}).get(entity_id, -1)
            if last_minute == current_minute:
                continue
        else:
            # 普通间隔采集
            if now_ts - last_ts < interval_min * 60:
                continue

        # 读取当前实体状态
        state = hass.states.get(entity_id)
        if not state or state.state in ("unavailable", "unknown", None):
            continue

        # 提取数值：sensor 类型优先转 float 并保留3位小数，非数值则原样存字符串
        if metric_type == METRIC_SENSOR:
            try:
                value = round(float(state.state), 3)
            except (ValueError, TypeError):
                value = state.state
        else:
            try:
                value = float(state.state)
            except (ValueError, TypeError):
                continue

        tz = _get_timezone(hass)
        dt_str = _get_local_now_str(tz)

        try:
            await hass.async_add_executor_job(
                _write_env_metric_record, db_path, entity_id, name, dt_str, metric_type, value, room,
            )
        except Exception:
            local_logger = get_logger()
            if local_logger:
                local_logger.exception("[env] 环境采集写入异常 entity_id=%s", entity_id)

        # 更新最后采集时间
        hass.data[DOMAIN].setdefault("last_poll", {})[entity_id] = now_ts
        if round_minute:
            hass.data[DOMAIN].setdefault("last_poll_minute", {})[entity_id] = datetime.now().minute


# =========================================================================== #
#  同步实体列表到数据库                                                          #
# =========================================================================== #
def _sync_entities_to_db(db_path: str, selected: list[str], category: str,
                          metric_type: str = "", collect_interval: int = 30,
                          room: str = "", round_minute: int = 0) -> None:
    now = datetime.utcnow().isoformat()
    conn = sqlite3.connect(db_path)
    try:
        for entity_id in selected:
            conn.execute(
                f"""
                INSERT INTO {TABLE_ENTITY_CONFIGS}
                    (entity_id, enabled, category, metric_type, collect_interval, round_minute, power_entity, friendly_name, room, created_at, updated_at)
                VALUES (?, 1, ?, ?, ?, ?, '', '', ?, ?, ?)
                ON CONFLICT(entity_id) DO UPDATE SET
                    enabled = 1, category = excluded.category,
                    metric_type = excluded.metric_type,
                    collect_interval = excluded.collect_interval,
                    round_minute = excluded.round_minute,
                    room = excluded.room,
                    updated_at = excluded.updated_at
                """,
                (entity_id, category, metric_type, collect_interval, round_minute, room, now, now),
            )
        conn.commit()
    finally:
        conn.close()


# =========================================================================== #
#  扫地机器人：配置查询 + 状态变化采集                                             #
# =========================================================================== #
def _get_vacuum_configs(db_path: str) -> list[dict]:
    """获取所有启用的扫地机器人实例配置。"""
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            f"SELECT vc.*, vtd.position_path, vtd.working_states, vtd.field_mapping "
            f"FROM {TABLE_VACUUM_CONFIGS} vc "
            f"JOIN {TABLE_VACUUM_TYPE_DEFS} vtd ON vc.type_name = vtd.type_name "
            f"WHERE vc.enabled = 1"
        )
        return [dict(row) for row in cursor.fetchall()]
    except Exception:
        return []
    finally:
        conn.close()


def _get_next_vacuum_seq(db_path: str, vacuum_id: str) -> int:
    """获取该机器人的下一个坐标顺序号。"""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            f"SELECT COALESCE(MAX(seq), 0) FROM {TABLE_VACUUM_HISTORY} WHERE vacuum_id = ?",
            (vacuum_id,),
        ).fetchone()
        return (row[0] or 0) + 1
    finally:
        conn.close()


def _ensure_vacuum_history_columns(db_path: str, field_mapping: dict) -> None:
    """确保 vacuum_history 表包含 field_mapping 中指定的额外列。"""
    if not field_mapping:
        return
    conn = sqlite3.connect(db_path)
    try:
        existing = [row[1] for row in conn.execute(f"PRAGMA table_info({TABLE_VACUUM_HISTORY})")]
        for col_name, col_info in field_mapping.items():
            if col_name not in existing:
                col_type = col_info.get("col_type", "REAL")
                conn.execute(
                    f"ALTER TABLE {TABLE_VACUUM_HISTORY} ADD COLUMN {col_name} {col_type}"
                )
        conn.commit()
    finally:
        conn.close()


def _write_vacuum_record(db_path: str, vacuum_id: str, entity_id: str,
                         dt_str: str, seq: int, pos_x: float, pos_y: float,
                         pos_a: float, state: str, extra_values: dict) -> None:
    """写入一条扫地机器人位置记录。"""
    conn = sqlite3.connect(db_path)
    try:
        _ensure_vacuum_history_columns(db_path, extra_values)
        field_mapping_for_cols = {k: v for k, v in extra_values.items() if k}
        if field_mapping_for_cols:
            columns = ["vacuum_id", "entity_id", "datetime", "seq", "pos_x", "pos_y", "pos_a", "state"] + list(field_mapping_for_cols.keys())
            placeholders = ("?, " * len(columns)).rstrip(", ")
            values = [vacuum_id, entity_id, dt_str, seq, pos_x, pos_y, pos_a, state] + list(field_mapping_for_cols.values())
            conn.execute(
                f"INSERT INTO {TABLE_VACUUM_HISTORY} ({', '.join(columns)}) VALUES ({placeholders})",
                values,
            )
        else:
            conn.execute(
                f"INSERT INTO {TABLE_VACUUM_HISTORY} "
                f"(vacuum_id, entity_id, datetime, seq, pos_x, pos_y, pos_a, state) "
                f"VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (vacuum_id, entity_id, dt_str, seq, pos_x, pos_y, pos_a, state),
            )
        conn.commit()
    finally:
        conn.close()


async def _async_vacuum_state_changed(hass: HomeAssistant, db_path: str, event: Event) -> None:
    """扫地机器人 state_changed 事件处理：坐标变化时采集多实体快照。"""
    new_state = event.data.get("new_state")
    if not new_state:
        return

    entity_id = new_state.entity_id
    tz = _get_timezone(hass)

    try:
        vacuum_configs = await hass.async_add_executor_job(_get_vacuum_configs, db_path)
    except Exception:
        return

    for cfg in vacuum_configs:
        if cfg["trigger_entity_id"] != entity_id:
            continue

        # 检查状态是否在工作状态中
        working_states_raw = cfg.get("working_states", "cleaning")
        working_states = set(s.strip().lower() for s in working_states_raw.split(",") if s.strip())
        if new_state.state.lower() not in working_states:
            return

        # 从 attributes 中提取坐标
        position_path = cfg.get("position_path", "vacuum_position")
        attrs = new_state.attributes or {}
        pos = _extract_nested_value(attrs, position_path) if position_path else None
        if not isinstance(pos, dict):
            return

        pos_x = pos.get("x")
        pos_y = pos.get("y")
        pos_a = pos.get("a")
        if pos_x is None or pos_y is None:
            return

        # 去重：和最近一条坐标对比
        try:
            last_pos = await hass.async_add_executor_job(
                lambda: _get_last_vacuum_position(db_path, cfg["vacuum_id"])
            )
        except Exception:
            last_pos = None
        if last_pos and last_pos.get("pos_x") == pos_x and last_pos.get("pos_y") == pos_y:
            return

        dt_str = _get_local_now_str(tz)
        seq = await hass.async_add_executor_job(_get_next_vacuum_seq, db_path, cfg["vacuum_id"])

        # 读取额外数据源
        extra_values = {}
        field_mapping_raw = cfg.get("field_mapping", "{}")
        if isinstance(field_mapping_raw, str):
            try:
                field_mapping = json.loads(field_mapping_raw)
            except Exception:
                field_mapping = {}
        else:
            field_mapping = field_mapping_raw

        for col_name, col_info in field_mapping.items():
            src_entity = col_info.get("source_entity", "")
            src_path = col_info.get("source_path", "state")
            if not src_entity:
                continue
            src_state = hass.states.get(src_entity)
            if not src_state:
                continue
            if src_path == "state":
                raw_val = src_state.state
            else:
                raw_val = _extract_nested_value(src_state.attributes, src_path) if src_state.attributes else None
            # 类型转换
            col_type = col_info.get("col_type", "TEXT")
            try:
                if col_type in ("REAL", "INTEGER"):
                    extra_values[col_name] = float(raw_val) if raw_val is not None else None
                else:
                    extra_values[col_name] = str(raw_val) if raw_val is not None else None
            except (ValueError, TypeError):
                extra_values[col_name] = None

        await hass.async_add_executor_job(
            _write_vacuum_record, db_path,
            cfg["vacuum_id"], entity_id, dt_str, seq,
            pos_x, pos_y, pos_a, new_state.state,
            extra_values,
        )


def _get_last_vacuum_position(db_path: str, vacuum_id: str) -> dict | None:
    """获取指定真空机器人最近一条位置记录。"""
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            f"SELECT pos_x, pos_y FROM {TABLE_VACUUM_HISTORY} "
            f"WHERE vacuum_id = ? ORDER BY seq DESC LIMIT 1",
            (vacuum_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# =========================================================================== #
#  注册 HTTP API 视图                                                          #
# =========================================================================== #
def _register_api_views(hass: HomeAssistant, db_path: str) -> None:
    from .http_api import (
        EntityConfigView,
        EntityConfigListView,
        CustomRoutesView,
        CustomRoutesListView,
        DynamicRouterView,
        QueryView,
        DBViewerView,
        DBViewerDataView,
        DBViewerUpdateView,
        LogDataView,
        EntityMonitorView,
        EntityStateView,
        AttrTypesView,
        AttrConfigView,
        ExportConfigView,
        FileSourceConfigView,
        ApiSourceConfigView,
        StatsView,
        ApiKeyView,
        ApiSettingsView,
        DBViewerLoginView,
        VacuumTypeDefsView,
        VacuumConfigsView,
        AttrManualTriggerView,
        DbMaintainView,
        BatchEntityStateView,
        PushTargetsView,
        PushDataView,
        BridgeConnectionsView,
        BridgeEntitiesView,
        BridgeReloadView,
        VirtualDeviceView,
        HealthAddView,
        HealthTypesView,
        HealthDeleteView,
        MediaPlaylistView,
        MediaQueueView,
        TableColumnsView,
    )
    hass.http.register_view(EntityConfigView(db_path))
    hass.http.register_view(EntityConfigListView(db_path))
    hass.http.register_view(CustomRoutesView(db_path))
    hass.http.register_view(CustomRoutesListView(db_path))
    hass.http.register_view(DynamicRouterView(db_path, hass))
    hass.http.register_view(QueryView(db_path))
    hass.http.register_view(DBViewerView(db_path))
    hass.http.register_view(DBViewerDataView(db_path))
    hass.http.register_view(DBViewerUpdateView(db_path))
    hass.http.register_view(LogDataView(hass))
    hass.http.register_view(EntityMonitorView(db_path, hass))
    hass.http.register_view(EntityStateView(db_path, hass))
    hass.http.register_view(AttrTypesView(db_path))
    hass.http.register_view(AttrConfigView(db_path))
    hass.http.register_view(ExportConfigView(db_path))
    hass.http.register_view(FileSourceConfigView(db_path))
    hass.http.register_view(ApiSourceConfigView(db_path))
    hass.http.register_view(StatsView(db_path))
    hass.http.register_view(ApiKeyView(db_path))
    hass.http.register_view(ApiSettingsView(db_path))
    hass.http.register_view(DBViewerLoginView(db_path))
    hass.http.register_view(VacuumTypeDefsView(db_path))
    hass.http.register_view(VacuumConfigsView(db_path))
    hass.http.register_view(AttrManualTriggerView(db_path))
    hass.http.register_view(DbMaintainView(db_path))
    hass.http.register_view(BatchEntityStateView(db_path))
    hass.http.register_view(PushTargetsView(db_path))
    hass.http.register_view(PushDataView(db_path))
    # 设备桥接 API
    hass.http.register_view(BridgeConnectionsView(db_path))
    hass.http.register_view(BridgeEntitiesView(db_path))
    hass.http.register_view(BridgeReloadView(db_path))
    hass.http.register_view(VirtualDeviceView(db_path))
    hass.http.register_view(HealthAddView(db_path))
    hass.http.register_view(HealthTypesView(db_path))
    hass.http.register_view(HealthDeleteView(db_path))
    hass.http.register_view(MediaPlaylistView(db_path))
    hass.http.register_view(MediaQueueView(db_path))
    hass.http.register_view(TableColumnsView(db_path))


# =========================================================================== #
#  Config Entry 入口                                                           #
# =========================================================================== #
async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    storage_dir = os.path.join(hass.config.config_dir, "storage")
    os.makedirs(storage_dir, exist_ok=True)
    db_path = os.path.join(storage_dir, DATABASE_FILENAME)

    # 初始化本地文件日志
    from .logger import async_setup_local_logger
    log_retention_days = entry.options.get("log_retention_days", 7)
    log_dir = os.path.join(os.path.dirname(__file__), "logs")
    local_logger = await async_setup_local_logger(hass, log_dir, keep_days=log_retention_days)
    await hass.async_add_executor_job(
        local_logger.info, "[sys] 本地日志系统已启动 log_dir=%s keep_days=%d",
        log_dir, log_retention_days,
    )

    await hass.async_add_executor_job(_init_database, db_path)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN]["entry_id"] = entry.entry_id
    hass.data[DOMAIN]["db_path"] = db_path
    hass.data[DOMAIN]["log_dir"] = log_dir
    hass.data[DOMAIN]["last_poll"] = {}  # 记录每个环境实体最后采集时间
    hass.data[DOMAIN]["last_attr_poll"] = {}  # 记录每个属性实体最后采集时间
    hass.data[DOMAIN]["timezone"] = entry.options.get("timezone", DEFAULT_TIMEZONE)
    hass.data[DOMAIN].setdefault("api_enabled", True)
    hass.data[DOMAIN].setdefault("db_viewer_enabled", True)
    hass.data[DOMAIN].setdefault("db_edit_enabled", True)
    hass.data[DOMAIN].setdefault("allow_remote_access", False)

    _register_api_views(hass, db_path)

    # ── 媒体播放队列：后端自动切歌 ──
    import json as _json

    def _handle_media_queue_event(event) -> None:
        """监听 media_player 状态变化，自动推进播放队列。"""
        entity_id = event.data.get("entity_id", "")
        if not entity_id or not entity_id.startswith("media_player."):
            return
        old_state = event.data.get("old_state")
        new_state = event.data.get("new_state")
        if not new_state or not old_state:
            return
        # 只处理从 playing 变为 idle/paused 的情况
        if old_state.state != "playing" or new_state.state not in ("idle", "paused"):
            return
        # 检查是否有人在听歌中途暂停（位置远小于总时长）
        old_pos = (old_state.attributes or {}).get("media_position", 0) or 0
        old_dur = (old_state.attributes or {}).get("media_duration", 0) or 0
        if old_dur > 0 and old_pos < old_dur * 0.85:
            return  # 用户手动暂停，不切歌
        # 查询该实体是否有活跃队列
        import sqlite3
        try:
            conn = sqlite3.connect(db_path)
            try:
                row = conn.execute(
                    f"SELECT * FROM {TABLE_MEDIA_QUEUE} WHERE entity_id = ? AND is_active = 1",
                    (entity_id,),
                ).fetchone()
                if not row:
                    return
                tracks = _json.loads(row[2]) if isinstance(row[2], str) else []
                cur_idx = int(row[3])
                next_idx = cur_idx + 1
                if next_idx >= len(tracks):
                    # 播完，停止队列
                    conn.execute(
                        f"UPDATE {TABLE_MEDIA_QUEUE} SET is_active = 0, updated_at = ? WHERE entity_id = ?",
                        (__import__('datetime').datetime.now().strftime("%Y-%m-%d %H:%M:%S"), entity_id),
                    )
                    conn.commit()
                    _LOGGER.info("[media_queue] 列表播放完成 entity=%s", entity_id)
                    return
                next_track = tracks[next_idx]
                media_id = next_track.get("media_content_id") or next_track.get("url", "")
                if not media_id:
                    return
                # 更新索引
                now = __import__('datetime').datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                conn.execute(
                    f"UPDATE {TABLE_MEDIA_QUEUE} SET current_index = ?, updated_at = ? WHERE entity_id = ?",
                    (next_idx, now, entity_id),
                )
                conn.commit()
                # 调用 HA 服务播放下一首
                hass.async_create_task(
                    hass.services.async_call(
                        "media_player", "play_media",
                        {"entity_id": entity_id, "media_content_id": media_id,
                         "media_content_type": next_track.get("media_type", "music")},
                    )
                )
                _LOGGER.info("[media_queue] 自动切歌 entity=%s idx=%d->%d title=%s",
                             entity_id, cur_idx, next_idx, next_track.get("title", ""))
            finally:
                conn.close()
        except Exception:
            _LOGGER.exception("[media_queue] 切歌处理异常 entity=%s", entity_id)

    hass.bus.async_listen("state_changed", _handle_media_queue_event)
    _LOGGER.info("[media_queue] 后端列表播放监听已启动")

    # ── 方案一：启动时从 pending JSON 恢复丢失的关机事件 ──
    async def _recover_pending_on_startup(_now=None) -> None:
        """HA 启动后恢复 pending JSON 中未关闭的设备记录。"""
        storage_dir = os.path.dirname(db_path)
        json_path = _get_pending_json_path(storage_dir)

        # 阻塞文件读取走 executor
        data = await hass.async_add_executor_job(_load_pending_json, json_path)
        pending = data.get("pending", {})
        if not pending:
            return

        tz = _get_timezone(hass)
        local_now = _get_local_now(tz)
        now_str = local_now.strftime("%Y-%m-%d %H:%M:%S")
        last_shutdown_time = data.get("last_shutdown_time", "")

        # 计算停机时长（用 UTC 比较，与写入时一致）
        downtime_seconds = 0
        if last_shutdown_time:
            try:
                dt_shutdown = datetime.strptime(last_shutdown_time[:19], "%Y-%m-%d %H:%M:%S")
                downtime_seconds = (datetime.utcnow() - dt_shutdown).total_seconds()
            except Exception:
                pass

        local_logger = get_logger()
        if local_logger:
            local_logger.info(
                "[pending] 启动恢复开始 pending=%d last_shutdown=%s downtime=%.0fs",
                len(pending), last_shutdown_time or "N/A", downtime_seconds,
            )

        recovered = deleted = kept = 0
        for entity_id, info in list(pending.items()):
            record_id = info.get("record_id")
            if not record_id:
                continue

            state = hass.states.get(entity_id)
            is_on = state is not None and _is_on_state(entity_id, state.state)

            if is_on:
                # 设备仍然开机，保留记录和 JSON
                kept += 1
                continue

            # 设备已关机
            if downtime_seconds > 0 and downtime_seconds <= SHUTDOWN_THRESHOLD_SECONDS:
                # 停机 ≤30 分钟，用当前时间作为 off_time
                try:
                    await hass.async_add_executor_job(
                        _update_device_off_record, db_path, entity_id, now_str, None,
                        json_path,
                    )
                    recovered += 1
                except Exception as exc:
                    if local_logger:
                        local_logger.warning("[attr] 写入附加字段失败 entity_id=%s: %s", entity_id, exc)
            elif downtime_seconds > SHUTDOWN_THRESHOLD_SECONDS:
                # 停机 >30 分钟，删除数据库中该条未关闭记录
                def _delete_unclosed_record(_db_path=db_path, _rid=record_id) -> None:
                    conn = sqlite3.connect(_db_path)
                    try:
                        conn.execute(
                            f"DELETE FROM {TABLE_DEVICE_HISTORY} WHERE id = ?",
                            (_rid,),
                        )
                        conn.commit()
                    finally:
                        conn.close()
                try:
                    await hass.async_add_executor_job(_delete_unclosed_record)
                    # 从 JSON 中移除（阻塞 I/O，走 executor）
                    await hass.async_add_executor_job(_pending_json_remove, json_path, entity_id)
                    deleted += 1
                except Exception as exc:
                    if local_logger:
                        local_logger.warning("[attr] 写入附加字段失败 entity_id=%s: %s", entity_id, exc)
            else:
                # downtime_seconds == 0（首次使用新代码，无 last_shutdown_time），
                # 用当前时间作为 off_time（保守处理，保留记录）
                try:
                    await hass.async_add_executor_job(
                        _update_device_off_record, db_path, entity_id, now_str, None,
                        json_path,
                    )
                    recovered += 1
                except Exception as exc:
                    if local_logger:
                        local_logger.warning("[attr] 写入附加字段失败 entity_id=%s: %s", entity_id, exc)

        if local_logger:
            local_logger.info(
                "[pending] 启动恢复完成 recovered=%d deleted=%d kept=%d",
                recovered, deleted, kept,
            )

    # 在 HA 完全启动后执行恢复
    hass.bus.async_listen_once("homeassistant_started", _recover_pending_on_startup)

    # 初始化受监控实体的白名单（必须在监听器注册之前，避免空窗期穿透）
    hass.data[DOMAIN]["monitored_entities"] = await hass.async_add_executor_job(
        _refresh_monitored_set_sync, db_path,
    )
    monitored_count = len(hass.data[DOMAIN]["monitored_entities"])
    _LOGGER.warning("[HDS] 受监控实体白名单已加载 count=%d", monitored_count)
    if local_logger:
        await hass.async_add_executor_job(
            local_logger.info,
            "[sys] 受监控实体白名单已加载 count=%d entities=%s",
            monitored_count, sorted(hass.data[DOMAIN]["monitored_entities"]),
        )

    # 设备类：监听 state_changed 事件（仅处理用户配置过的实体）
    async def _internal_state_listener(event: Event) -> None:
        entity_id = event.data.get("entity_id")
        monitored = hass.data.get(DOMAIN, {}).get("monitored_entities")
        if monitored is not None and entity_id and entity_id not in monitored:
            return  # 不在监控白名单中，跳过
        try:
            await _async_state_changed(hass, db_path, event)
        except Exception:
            local_logger = get_logger()
            if local_logger:
                local_logger.exception("[device] 状态回调异常")

    cancel_bus = hass.bus.async_listen("state_changed", _internal_state_listener)
    hass.data[DOMAIN]["cancel_bus_listener"] = cancel_bus

    # 扫地机器人：监听 state_changed 事件（坐标变化时触发多实体快照采集）
    async def _vacuum_state_listener(event: Event) -> None:
        entity_id = event.data.get("entity_id")
        monitored = hass.data.get(DOMAIN, {}).get("monitored_entities")
        if monitored is not None and entity_id and entity_id not in monitored:
            return  # 不在监控白名单中，跳过
        try:
            await _async_vacuum_state_changed(hass, db_path, event)
        except Exception:
            local_logger = get_logger()
            if local_logger:
                local_logger.exception("[vacuum] 状态回调异常")

    cancel_vacuum_bus = hass.bus.async_listen("state_changed", _vacuum_state_listener)
    hass.data[DOMAIN]["cancel_vacuum_listener"] = cancel_vacuum_bus

    # 传感器类：整秒轮询（second=0 对齐分钟边界，确保 round_minute 整分钟采集精确）
    async def _env_poll_callback(now=None) -> None:
        try:
            await _async_env_poll(hass, db_path, now)
        except Exception:
            local_logger = get_logger()
            if local_logger:
                local_logger.exception("[env] 环境轮询异常")

    cancel_poll = async_track_time_change(
        hass, _env_poll_callback, second=0,
    )
    hass.data[DOMAIN]["cancel_env_poll"] = cancel_poll

    # 属性类：整秒轮询（second=0 对齐分钟边界，确保 round_minute 整分钟采集精确）
    async def _attr_poll_callback(now=None) -> None:
        try:
            await _async_attr_poll(hass, db_path, now)
        except Exception:
            local_logger = get_logger()
            if local_logger:
                local_logger.exception("[attr] 属性轮询异常")

    cancel_attr_poll = async_track_time_change(
        hass, _attr_poll_callback, second=0,
    )
    hass.data[DOMAIN]["cancel_attr_poll"] = cancel_attr_poll

    # now_kwh：每分钟更新一次配置了 power_entity 的设备的当前值
    async def _device_kwh_callback(now=None) -> None:
        try:
            await _async_device_now_kwh_poll(hass, db_path, now)
        except Exception:
            local_logger = get_logger()
            if local_logger:
                local_logger.exception("[device_kwh] now_kwh 轮询异常")

    cancel_device_kwh = async_track_time_change(
        hass, _device_kwh_callback, second=0,
    )
    hass.data[DOMAIN]["cancel_device_kwh"] = cancel_device_kwh

    # 午夜拆分：每天 00:00 执行
    async def _midnight_split_callback(now=None) -> None:
        try:
            await _async_midnight_split(hass, db_path)
        except Exception:
            local_logger = get_logger()
            if local_logger:
                local_logger.exception("[midnight] 午夜拆分异常")

    cancel_midnight = async_track_time_change(
        hass, _midnight_split_callback, hour=0, minute=0, second=0,
    )
    hass.data[DOMAIN]["cancel_midnight_split"] = cancel_midnight

    # 定时修正扫描：每 10 分钟检测今日数据中同一设备的多条未关闭记录并修正
    async def _correction_scan_callback(now=None) -> None:
        try:
            await _async_correct_periodic_scan(hass, db_path)
        except Exception:
            local_logger = get_logger()
            if local_logger:
                local_logger.exception("[correction] 定时修正扫描异常")

    cancel_correction = async_track_time_interval(
        hass, _correction_scan_callback, timedelta(minutes=10),
    )
    hass.data[DOMAIN]["cancel_correction_scan"] = cancel_correction

    # 文件源：每 5 秒检查 mtime
    async def _file_source_callback(now=None) -> None:
        try:
            await _async_file_source_poll(hass, db_path)
        except Exception:
            local_logger = get_logger()
            if local_logger:
                local_logger.exception("[filesrc] 文件源轮询异常")

    # 重启时重置文件源的 last_mtime，强制首次刷新实体状态
    def _reset_file_mtimes():
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(f"UPDATE {TABLE_FILE_SOURCE_CONFIGS} SET last_mtime = 0 WHERE enabled = 1")
            conn.commit()
        finally:
            conn.close()
    await hass.async_add_executor_job(_reset_file_mtimes)

    # 立即触发首次文件源 / API 源刷新
    async def _bridge_startup_callback(now=None):
        await _async_file_source_poll(hass, db_path)
        await _async_api_source_poll(hass, db_path)
    hass.async_create_task(_bridge_startup_callback())

    cancel_file_src = async_track_time_interval(
        hass, _file_source_callback, timedelta(seconds=5),
    )
    hass.data[DOMAIN]["cancel_file_src"] = cancel_file_src

    # API 源：每 10 秒调度一次（各 API 按自己的 interval 运行）
    async def _api_source_callback(now=None) -> None:
        try:
            await _async_api_source_poll(hass, db_path)
        except Exception:
            local_logger = get_logger()
            if local_logger:
                local_logger.exception("[apisrc] API 源轮询异常")

    cancel_api_src = async_track_time_interval(
        hass, _api_source_callback, timedelta(seconds=10),
    )
    hass.data[DOMAIN]["cancel_api_src"] = cancel_api_src

    _LOGGER.warning("[HDS] 监听已注册（白名单+环境轮询+属性轮询+文件源+API源+午夜拆分）")

    if local_logger:
        await hass.async_add_executor_job(
            local_logger.info,
            "[sys] 监听已注册 功能=白名单过滤+环境轮询+午夜拆分 "
            "db_path=%s log_dir=%s domain=%s",
            db_path, log_dir, DOMAIN,
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # 启动时恢复已持久化的虚拟设备（后台任务，不阻塞启动）
    async def _restore_virtual_devices():
        from .virtual_devices import VirtualDeviceManager
        vm = VirtualDeviceManager(hass, entry.entry_id)
        saved = await hass.async_add_executor_job(vm.load_from_db)
        if saved:
            _LOGGER.info("[virtual] 正在恢复 %d 个虚拟设备...", len(saved))
            for cfg in saved:
                try:
                    vm.create_device(cfg)
                except Exception:
                    _LOGGER.exception("[virtual] 恢复设备失败 %s", cfg.get("entity_id"))
            _LOGGER.info("[virtual] 已恢复 %d 个虚拟设备", len(saved))
    hass.async_create_background_task(_restore_virtual_devices(), "restore_virtual_devices")

    # 启动设备桥接（延迟后台，不阻塞 HA 启动）
    from .bridge import BridgeManager
    bridge_manager = BridgeManager(hass, db_path, entry.entry_id)
    hass.data[DOMAIN]["bridge_manager"] = bridge_manager

    async def _delayed_bridge_start():
        await asyncio.sleep(3)  # 等 HA 完全启动后再连接
        await bridge_manager.start_all()

    hass.async_create_background_task(_delayed_bridge_start(), "bridge_start")

    if local_logger:
        await hass.async_add_executor_job(
            local_logger.info, "[sys] 设备桥接已启动",
        )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    # 停止设备桥接
    bridge_manager = hass.data.get(DOMAIN, {}).get("bridge_manager")
    if bridge_manager:
        await bridge_manager.stop_all()

    for key in ("cancel_bus_listener", "cancel_env_poll", "cancel_attr_poll", "cancel_file_src", "cancel_api_src", "cancel_midnight_split", "cancel_correction_scan"):
        cancel = hass.data.get(DOMAIN, {}).get(key)
        if cancel:
            cancel()

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data.pop(DOMAIN, None)

    local_logger = get_logger()
    if local_logger:
        await hass.async_add_executor_job(
            local_logger.info, "[sys] ha_data_store 已卸载",
        )
    return unload_ok
