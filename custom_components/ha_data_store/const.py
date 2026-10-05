"""ha_data_store 集成常量定义。"""
from __future__ import annotations

DOMAIN = "ha_data_store"

# 数据库文件名
DATABASE_FILENAME = "ha_data_store.db"

# 数据库表名
TABLE_ENTITY_CONFIGS = "entity_configs"
TABLE_DEVICE_HISTORY = "device_history"
TABLE_CUSTOM_ROUTES = "custom_routes"
TABLE_API_ENDPOINTS = "api_endpoints"
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
TABLE_CONTROL_LOGS = "control_logs"
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
TABLE_METRICS_CATALOG = "metrics_catalog"

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
ATTR_MODE_COMM = "comm"      # 通讯数据：列表展开 + 固定通讯字段列（列名/类型不可改）

# 附加标量字段 JSON 合并列名
EXTRA_JSON_COLUMN = "extra_json"

# ── 通讯数据采集（comm 模式）──────────────────────────────────────────────
# 建议的 attr_type_defs.type_name（决定数据表名 attr_comm_records）；
# 查询 API 默认读写该类型，可通过 type_name 参数覆盖。
COMM_DEFAULT_TYPE_NAME = "comm_records"

# 通讯数据表类型名设置实体：留空 = 自动探测 attr_type_defs 中 mode=comm 的类型名
# （优先取数据表已存在者），仅在自动探测不符合预期（如同一库里有多张通讯表）时才需显式指定。
COMM_TYPE_NAME_ENTITY_ID = "text.ha_data_store_comm_type_name"
COMM_TYPE_NAME_DEFAULT = ""

# 固定通讯字段定义：(目标列名, 列类型, 中文标签, 是否必填)
# 所有列在建表时全部创建，field_mapping 只决定采集时从源数据填充哪些列。
COMM_FIELDS: tuple[tuple[str, str, str, bool], ...] = (
    ("my_number",    "TEXT",    "我方号码",   False),
    ("party_number", "TEXT",    "对方号码",   False),
    ("party_place",  "TEXT",    "对方归属地", False),
    ("party_name",   "TEXT",    "对方姓名",   False),
    ("time",         "TEXT",    "通讯时间",   True),
    ("location",     "TEXT",    "通讯地点",   False),
    ("msg_type",     "TEXT",    "消息类型",   False),
    ("channel",      "TEXT",    "数据来源",   False),
    ("call_type",    "TEXT",    "呼叫类型",   False),
    ("duration",     "INTEGER", "时长(秒)",   False),
    ("cost",         "REAL",    "金额(元)",   False),
    ("traffic_usage", "REAL",   "流量(MB)",   False),
    ("traffic_type", "TEXT",    "流量类型",   False),
    ("content",      "TEXT",    "消息内容",   False),
    ("image_path",   "TEXT",    "图片路径",   False),
    ("location_coordinate", "TEXT", "我的坐标",   False),
    ("party_isp",           "TEXT", "对方运营商", False),
    ("party_coordinate",    "TEXT", "对方坐标",   False),
)

# 通讯列名白名单（顺序即建表顺序）
COMM_COLUMNS: tuple[str, ...] = tuple(f[0] for f in COMM_FIELDS)

# 通讯列名 → 列类型
COMM_COLUMN_TYPES: dict[str, str] = {f[0]: f[1] for f in COMM_FIELDS}

# 通讯列名 → 中文标签
COMM_COLUMN_LABELS: dict[str, str] = {f[0]: f[2] for f in COMM_FIELDS}

# 必填列（保存配置时校验）
COMM_REQUIRED_COLUMNS: tuple[str, ...] = tuple(f[0] for f in COMM_FIELDS if f[3])

# 通讯表附加索引：(索引后缀, 列元组)
COMM_INDEX_DEFS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("time",  ("time",)),
    ("party", ("party_number", "time")),
    ("name",  ("party_name", "time")),
    ("place", ("location", "time")),
)


def is_comm_column(name: str) -> bool:
    """判断给定列名是否属于通讯固定字段白名单。"""
    return name in COMM_COLUMN_TYPES

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

# ── 近期使用设备（传感器 all 节点 + API type=device_last_used）──
RECENT_DAYS_ENTITY_ID = "number.ha_data_store_recent_days"  # 统计窗口天数设置实体
RECENT_WINDOW_DAYS_DEFAULT = 30     # 默认窗口（天）：设置实体缺失/非法时回退
RECENT_WINDOW_DAYS_MIN = 1
RECENT_WINDOW_DAYS_MAX = 365
RECENT_EXCLUDE_SETTING_KEY = "recent_exclude_entities"  # api_settings 中的排除项键（JSON 数组）

# ── 历史今日（传感器 + 时间范围设置实体）──
# 传感器状态 = 三类数据的记录总数；属性含 comm / device / env 三个节点（各自的汇总与明细）
TODAY_IN_HISTORY_SENSOR_ID = "sensor.ha_data_store_today_in_history"
# 时间范围设置实体：写法 `<时间>,<前后分钟>`，如 "01,80"（01:00 前后 80 分钟）、
# "now,60"（此刻前后 60 分钟）；留空 = 全部数据（不限定时间范围）
TODAY_IN_HISTORY_RANGE_ENTITY_ID = "text.ha_data_store_today_in_history_set"
TODAY_IN_HISTORY_RANGE_DEFAULT = ""            # 默认留空 = 不限定
TODAY_IN_HISTORY_RANGE_EXAMPLE = "now,60"      # 供表单 / 提示展示的示例
# 排除实体（存 api_settings 键值表，JSON 数组）：被排除的 entity_id 不进入历史今日统计，
# 同时作用于「历史今日」API（/api/ha_data_store/onthisday）与传感器。
TODAY_IN_HISTORY_EXCLUDE_SETTING_KEY = "today_in_history_exclude_entities"

# ── 实体→网络（push_targets / control_logs）──
# 控制总开关在 hass.data 中的键（由 switch.py 的 HaDataStorePushControlSwitch 维护）
PUSH_CONTROL_SWITCH_KEY = "push_control_enabled"
# 每个控制 token 默认每分钟最大控制次数（0=不限）
PUSH_CONTROL_DEFAULT_RATE_LIMIT = 60

# 系统版本号（与 manifest.json 同步）
VERSION = "4.17.1"
