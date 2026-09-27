"""定时精灵集成常量 - ha_data_store 子模块。"""

# 总线事件前缀
TIMER_EVENT_PREFIX = "ha_data_store_timer"

# 信号
TIMER_SIGNAL_UPDATE_SENSOR = f"{TIMER_EVENT_PREFIX}_update_sensor"

# SQLite 表名
TABLE_TIMER_TASKS = "timer_tasks"

# 默认配置
DEFAULT_TIME_ZONE = "Asia/Shanghai"

# 属性名
ATTR_ACTIVE_TASKS = "active_tasks"
ATTR_TOTAL_TASKS = "total_tasks"
ATTR_ACTIVE_TIMERS = "active_timers"
ATTR_ACTIVE_SCHEDULES = "active_schedules"
ATTR_CURRENT_TASK = "current_task"
ATTR_SUCCESSFUL_TASK = "successful_task"
ATTR_FAILED_TASK = "failed_task"
ATTR_TODAY_TASK = "today_task"
ATTR_ALL_TASK_LIST = "all_task_list"

# 历史记录上限（内存与传感器摘要都会按此裁剪；库中数据由保留天数控制）
MAX_HISTORY_RECORDS = 100

# 库中历史保留天数：每小时兜底任务会删除「非活跃且创建时间早于该天数」的行（0 = 不按天数清理）
TIMER_HISTORY_RETENTION_DAYS = 30

# 传感器属性 / 事件负载里携带的任务摘要条数。
# 完整列表请走 API（GET /api/ha_data_store/timer/tasks 等），
# 避免状态属性体积超过 HA recorder 的 16384 字节上限导致不记录历史。
TIMER_BRIEF_LIMIT = 20

# 重启后补偿执行的时限（秒）：错过超过该时长的一次性任务不再补执行，直接标记 expired
LATE_EXECUTE_MAX_SECONDS = 300

# 默认动作配置（内置默认值）
DEFAULT_DEFAULT_ACTIONS = {
    "light": {
        "turn_off": {"service": "light.turn_off", "description": "关闭灯光"},
        "turn_on": {"service": "light.turn_on", "description": "打开灯光"},
    },
    "switch": {
        "turn_off": {"service": "switch.turn_off", "description": "关闭开关"},
        "turn_on": {"service": "switch.turn_on", "description": "打开开关"},
    },
    "media_player": {
        "turn_off": {"service": "media_player.turn_off", "description": "关闭播放器"},
        "pause": {"service": "media_player.media_pause", "description": "暂停播放"},
    },
    "climate": {
        "turn_off": {"service": "climate.turn_off", "description": "关闭空调"},
        "set_temperature": {"service": "climate.set_temperature", "description": "设置温度"},
        "set_mode": {"service": "climate.set_hvac_mode", "description": "设置模式"},
    },
    "input_boolean": {
        "turn_off": {"service": "input_boolean.turn_off", "description": "关闭布尔值"},
        "turn_on": {"service": "input_boolean.turn_on", "description": "打开布尔值"},
        "toggle": {"service": "input_boolean.toggle", "description": "切换布尔值"},
    },
}
