"""ha_data_store 集成常量定义。"""
from __future__ import annotations

DOMAIN = "ha_data_store"

# 数据库文件名
DATABASE_FILENAME = "ha_data_store.db"

# 数据库表名
TABLE_ENTITY_CONFIGS = "entity_configs"
TABLE_DEVICE_HISTORY = "device_history"
TABLE_CUSTOM_ROUTES = "custom_routes"
TABLE_ATTR_TYPE_DEFS = "attr_type_defs"
TABLE_EXPORT_CONFIGS = "export_configs"
TABLE_FILE_SOURCE_CONFIGS = "file_source_configs"
TABLE_API_SOURCE_CONFIGS = "api_source_configs"
TABLE_API_KEYS = "api_keys"
TABLE_API_SETTINGS = "api_settings"
TABLE_VACUUM_TYPE_DEFS = "vacuum_type_defs"
TABLE_VACUUM_CONFIGS = "vacuum_configs"
TABLE_VACUUM_HISTORY = "vacuum_history"
TABLE_PUSH_TARGETS = "push_targets"
TABLE_BRIDGE_CONNECTIONS = "bridge_connections"
TABLE_BRIDGE_ENTITIES = "bridge_entities"
TABLE_HEALTH_RECORDS = "health_records"
TABLE_REPORT_ENTITIES = "report_entities"
TABLE_USER_ACTIONS = "user_actions"
TABLE_MEDIA_PLAYLISTS = "media_playlists"
TABLE_MEDIA_SONGS = "media_songs"
TABLE_MEDIA_QUEUE = "media_queue"
TABLE_MEDIA_NOW_PLAYING = "media_now_playing"
TABLE_AUTOMATIONS = "automations"
TABLE_AUTOMATION_LOGS = "automation_logs"
TABLE_POWER_METER_CONFIGS = "power_meter_configs"
TABLE_POWER_ENERGY_DAILY = "power_energy_daily"

# 传感器类：每种指标独立建表，表名前缀
ENV_TABLE_PREFIX = "env_"

# 属性提取类：每种类型独立建表，表名前缀
ATTR_TABLE_PREFIX = "attr_"

# 实体分类
CATEGORY_DEVICE = "device"
CATEGORY_ENVIRONMENT = "environment"
CATEGORY_ATTRIBUTE = "attribute"
CATEGORY_VACUUM = "vacuum_cleaner"

# 属性提取模式
ATTR_MODE_FIELDS = "fields"   # 字段快照
ATTR_MODE_LIST = "list"       # 列表展开
ATTR_MODE_MULTI = "multi"    # 混合模式：列表展开 + 附加字段

# 附加标量字段 JSON 合并列名
EXTRA_JSON_COLUMN = "extra_json"

# 采集模式
COLLECT_MODE_POLL = "poll"
COLLECT_MODE_EVENT = "event"

# 传感器类指标类型 → 对应表名映射
METRIC_TEMPERATURE = "temperature"
METRIC_HUMIDITY = "humidity"
METRIC_PM25 = "pm25"
METRIC_CO2 = "co2"
METRIC_POWER = "power"
METRIC_SENSOR = "sensor"

VALID_METRICS = [
    METRIC_TEMPERATURE,
    METRIC_HUMIDITY,
    METRIC_PM25,
    METRIC_CO2,
    METRIC_POWER,
    METRIC_SENSOR,
]


def get_env_table_name(metric_type: str) -> str:
    """根据指标类型返回对应的表名，如 temperature → env_temperature。"""
    return f"{ENV_TABLE_PREFIX}{metric_type}"


def get_attr_table_name(type_name: str) -> str:
    """根据属性类型名返回对应的表名，如 electricity_daily → attr_electricity_daily。"""
    return f"{ATTR_TABLE_PREFIX}{type_name}"


# 兼容旧表名（迁移用）
TABLE_ENVIRONMENT_HISTORY = "environment_history"

# SQL 安全沙箱：禁止出现的关键字
DANGEROUS_KEYWORDS = ("DROP", "DELETE", "UPDATE", "INSERT", "ALTER", "CREATE", "TRUNCATE", "EXEC", "EXECUTE")

# 默认时区偏移（小时），东八区
DEFAULT_TIMEZONE = 8

# 本地 JSON 缓存文件名（用于关机事件丢失恢复）
PENDING_JSON_FILENAME = "ha_data_store_pending.json"

# HA 停机判定阈值（秒）：超过此时间视为长时间停机
SHUTDOWN_THRESHOLD_SECONDS = 1800

# 简单自动化引擎参数
AUTOMATION_TICK_SECONDS = 30        # 调度器 tick 间隔（秒）
AUTOMATION_LOG_RETENTION_DAYS = 30  # 执行记录保留天数（自动清理）
AUTOMATION_LOG_CLEANUP_EVERY = 60   # 每 N 次 tick 清理一次过期执行记录

# 系统版本号（与 manifest.json 同步）
VERSION = "3.4.0"
