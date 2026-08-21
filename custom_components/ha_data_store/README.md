# HA 数据统一存储系统 (ha_data_store)

Home Assistant 自定义集成，提供**数据采集、存储、对外 API 服务、设备桥接**一体化能力。无需修改 `configuration.yaml`，通过配置界面即可动态管理监控实体。

---

## 目录

- [功能总览](#功能总览)
- [安装](#安装)
- [快速开始](#快速开始)
- [功能详解](#功能详解)
  - [今日家庭状态总结](#0-今日家庭状态总结)
  - [设备类数据采集](#1-设备类数据采集)
  - [传感器类数据采集](#2-传感器类数据采集)
  - [属性提取](#3-属性提取)
  - [自定义路由](#4-自定义路由)
  - [设备桥接](#5-设备桥接)
  - [文件源 → 实体](#6-文件源--实体)
  - [API源 → 实体](#7-api源--实体)
  - [虚拟设备](#8-虚拟设备)
  - [实体导出为JSON](#9-实体导出为json)
  - [打印机数据采集](#10-打印机数据采集)
- [API 接口文档](#api-接口文档)
  - [数据查询接口](#数据查询接口)
  - [配置管理接口](#配置管理接口)
  - [管理接口](#管理接口局域网)
  - [高级接口](#高级接口)
- [内置数据库浏览器](#内置数据库浏览器)
- [控制开关](#控制开关)
- [安全架构](#安全架构)
- [数据库表结构](#数据库表结构)
- [常见问题](#常见问题)
- [更新日志](#更新日志)

---

## 功能总览

| 模块 | 说明 |
|------|------|
| 📡 **设备类** | 监听实体 ON/OFF 状态变化，自动记录开关机时间、持续时长、用电量变化，支持午夜跨天拆分 |
| 🌡️ **传感器类** | 定时采集温湿度 / PM2.5 / CO2 / 功率 / 通用传感器数据，支持整分钟对齐 |
| 📊 **属性提取** | 从实体属性的数组/嵌套字段中提取数据，独立建表存储，支持字段快照、列表展开、混合三种模式 |
| 🧹 **扫地机器人** | 监听扫地机器人坐标变化，记录轨迹数据 |
| 🩺 **健康数据** | 存储血压、体温、身高、体重等健康记录，支持按人员查询 |
| 🔗 **设备桥接** | 通过 WebSocket 连接远程 HA，将远程实体的状态和控制在本地无缝映射（开关/灯光/气候/窗帘/风扇/门锁/数值/选择/传感器/二进制传感器） |
| 🖥️ **虚拟设备** | 动态创建自定义实体，支持多种设备类型和自定义属性 |
| 🖨️ **打印机数据采集** | 采集打印机统计数据与当日作业明细，支持多台、配置管理、数据查询与系统监控 |
| 📁 **文件源 → 实体** | 监听本地 JSON 文件变化，自动将数据映射为 HA 实体 |
| 🌐 **API源 → 实体** | 定时请求外部 HTTP API，将 JSON 响应解析并映射为 HA 实体 |
| 📄 **实体→ JSON** | 将 HA 实体状态实时导出为 JSON 文件，供外部系统消费 |
| 📤 **推送目标** | 将指定实体的状态变化定时推送到外部 HTTP 端点 |
| 🔑 **API 密钥** | API Key 鉴权，支持多密钥，可独立开关 |
| 🛡️ **安全控制** | 三个独立开关控制 API 访问、数据库浏览、数据库修改 |
| 📈 **系统监控** | `sensor.ha_data_store_info` 实时展示 6 大类健康状态（设备/环境/属性/导出/文件源/API源） |
| 📋 **内置数据库浏览器** | 管理页面直接浏览、编辑数据库，无需 SQL 工具 |
| 🔗 **自定义路由** | 通过 GUI 或 API 定义自定义 HTTP 路由，绑定任意 SQL 查询 |
| 🗂️ **统一泛域名动态路由** | 万能路由 `/api/ha_data_store/custom/{tail}` 运行时查库执行任意自定义 SQL |
| 🏠 **今日家庭状态总结** | 聚合历史表生成今日家庭中文总结，`sensor.today_family_status` + 按钮/服务按需触发 |

---

## 安装

### 方式一：通过 HACS 安装（推荐）

1. 确保已安装 [HACS](https://hacs.xyz/)
2. 将本仓库添加为自定义仓库
3. 搜索 "HA数据统一存储系统" 并安装
4. 重启 Home Assistant

### 方式二：手动安装

将 `ha_data_store` 文件夹复制到 Home Assistant 的 `custom_components` 目录：

```bash
# Linux / macOS
cp -r ha_data_store /path/to/config/custom_components/

# Windows
# 将 ha_data_store 文件夹复制到 %CONFIG_DIR%/custom_components/
```

重启 Home Assistant。

---

## 快速开始

### 1. 添加集成

**配置 → 设备与服务 → 添加集成 → 搜索 "HA数据统一存储系统"**

一键确认，无需任何参数。集成会自动创建所需的数据库文件。

### 2. 添加监控实体

添加集成后，点击条目下方的 **"配置"** 按钮进入管理菜单：

| 菜单项 | 功能 |
|--------|------|
| 添加设备类实体 | 添加开关/灯/空调等 ON/OFF 设备的监控 |
| 添加传感器类实体 | 添加温湿度、功率等传感器的定时采集 |
| 删除实体 | 移除不再需要的监控实体 |
| 查看实体 | 查看当前所有已配置的监控实体 |
| 时区设置 | 设置本地时区偏移（默认 UTC+8） |
| 日志保留时长 | 设置本地日志文件保留天数（默认 7 天） |
| 路由管理 | 管理自定义 HTTP 路由 |
| 设备桥接 | 管理远程 HA 设备桥接连接 |

### 3. 访问数据库浏览器

```
http://你的HA地址:8123/api/ha_data_store/db_viewer
```

默认密码：`admin`（首次使用请立即修改）

---

## 功能详解

### 0. 今日家庭状态总结

基于数据库各历史表，聚合**今日**家庭事实，渲染为精简中文段落（为 0 的项自动跳过）。

**新增实体：**
| 实体 | 类型 | 说明 |
|------|------|------|
| `sensor.today_family_status` | sensor | **状态值**=极简一句（家中有人/无人 + 门状态 + 提醒数，≤255字符）；`attributes.summary`=完整段落；`attributes.sections`=完整结构化分节；`attributes.overall`=normal/warning；`attributes.alerts`=异常提醒列表 |
| `button.ha_data_store_daily_summary` | button | 仪表盘放置按钮卡片，点击立即触发分析 |

**新增服务：** `ha_data_store.generate_daily_summary`
- 参数 `date`（可选，默认今天，格式 `yyyy-mm-dd`）
- 供自动化 / Node-RED 按需调用

**自动刷新：**
- HA 启动后 **1 分钟** 自动生成一次
- 之后每 **30 分钟**（整 30 分钟，即 00 分/30 分）自动更新
- 也可手动按钮 / 服务触发

**聚合维度与提醒阈值：**
| 节 | 数据源 | 内容 |
|----|--------|------|
| 环境 | env_temperature/humidity/pm25/co2 | 今日最高/最低/平均，房间温差≥2°C 时补充房间明细 |
| 设备 | device_history | 共 N 台/总时长 + 运行最久亮点 + **用电 TOP3 + 开关频次 TOP3**（完整逐台明细在 sections） |
| 用电 | env_power | 当日自增读数（最后一条 = 今日总用电，kWh）+ 昨日用电 + 环比 |
| 家庭事件 | vacuum_history / health_records / xiaoai_conversations | 扫地机次数、健康记录条数、小爱对话条数及时段 |
| 人在/门 | device_history（name=人在/入户门） | on_time 非空且 off_time 空=该房间有人/门开；否则家中无人/门关 |
| 灯光 | device_history（name 含"灯"） | 每盏灯取最新一条，on_time 非空且 off_time 空=该灯开着，统计"开着 x 盏灯" |
| 离线实体 | report_entities + 实时 states | 三态判定，unavailable 算离线、unknown 不算；有离线时 summary 末尾显示"离线设备 x 台" |

异常提醒（阈值写死）：高温 ≥30°C、低温 ≤5°C、单台连续运行 >6 小时、用电环比波动 >20%；存在任一提醒时 `overall=warning`，提醒文字放 `alert_text` 字段（`alerts` 为提醒列表），`summary` 段落末尾单独显示"离线设备 x 台"。

**工作原理：**

```
用户触发 ──► 聚合(查库) ──► 渲染(拼段落) ──► 刷新传感器(状态值 + attributes)

┌──────────────────────────────────────────────────────────────┐
│ 触发层                                                       │
│  按钮 button.ha_data_store_daily_summary                     │
│  服务 ha_data_store.generate_daily_summary(可选 date)        │
└───────────────────────────────┬──────────────────────────────┘
                                ▼
┌──────────────────────────────────────────────────────────────┐
│ 聚合层 build_daily_summary_sync()                            │
│  查今日数据（datetime LIKE 'yyyy-mm-dd%'）                   │
│  环境env_* / 设备device_history / 扫地vacuum_history          │
│  健康health_records / 小爱xiaoai_conversations               │
│  + 异常提醒(高温/低温/运行>6h/用电环比>20%)                    │
└───────────────────────────────┬──────────────────────────────┘
                                ▼
┌──────────────────────────────────────────────────────────────┐
│ 渲染层 render_summary()                                      │
│  精简中文段落（为 0 的项整段跳过）                            │
└───────────────────────────────┬──────────────────────────────┘
                                ▼
┌──────────────────────────────────────────────────────────────┐
│ 输出层 sensor.today_family_status                            │
│  状态值=一段话；attributes={summary, sections, overall,      │
│          alerts, date, generated_at}                         │
│  async_trigger_refresh() 主动刷新（无30s轮询）                │
└──────────────────────────────────────────────────────────────┘
```

**核心 SQL：**
- 环境：`SELECT value, room, datetime FROM env_temperature WHERE datetime LIKE '2026-08-21%'`
- 设备（今日，含单台用电）：`SELECT entity_id, name, duration, energy_consumed, on_power, now_kwh FROM device_history WHERE on_time LIKE '2026-08-21%'`
  - 单台用电（kWh）：已关闭直接用 `energy_consumed`；正在运行（`energy_consumed` 空且 `now_kwh` 非空）用 `now_kwh - on_power`
- 家庭总用电（今日，kWh）：`SELECT entity_id, datetime, value FROM env_power WHERE datetime LIKE '2026-08-21%'` —— `value` 为**当日自增**读数，按 `entity_id` 各取**最后一条**（当日累计 = 当日消耗）
- 家庭总用电（昨日，环比基准）：同上取 `'2026-08-20%'` 最后一条
  - 用电环比：`(今日总用电 - 昨日用电) / 昨日用电 × 100%`
- 扫地机：`SELECT DISTINCT datetime FROM vacuum_history WHERE datetime LIKE '2026-08-21%'`
- 健康记录：`SELECT name FROM health_records WHERE date_time LIKE '2026-08-21%'`
- 小爱对话：`SELECT conv_time FROM xiaoai_conversations WHERE conv_time LIKE '2026-08-21%'`
- 人在/门：`SELECT id, name, room, on_time, off_time FROM device_history WHERE name IN ('人在','入户门')` —— 人在：`on_time` 非空且 `off_time` 空=该房间有人；入户门：最新一条（id 最大）`off_time` 空=门开，否则门关

> 说明：家庭总用电取自 `env_power`（电表当日自增读数，最后一条即当日用电，**不做加法累加**）；`device_history.energy_consumed` 的单位为 **kWh**，仅用于单台设备明细，不参与总用电求和。

### 1. 设备类数据采集

监听实体 `state_changed` 事件，当状态从 ON 变为 OFF 时自动记录：

- **on_time**: 开机时间
- **off_time**: 关机时间
- **duration**: 持续时长（秒）
- **on_power**: 开机时电表读数
- **off_power**: 关机时电表读数
- **energy_consumed**: 本次用电量（off_power - on_power）
- **room**: 所属房间
- **cross_day**: 是否跨天

**支持的 domain 状态判定：**

| Domain | ON 状态 | OFF 状态 |
|--------|---------|----------|
| switch, light, fan, lock, binary_sensor, input_boolean | `on` | `off` |
| climate | `auto`, `cool`, `dry`, `heat`, `fan_only` | `off` |
| cover | `open` | `closed` |
| device_tracker | `home` | `not_home` |

**电量读取策略（优先级）：**
1. 配置中指定的 `power_entity` 传感器
2. 设备自身属性中的 `power` / `current_power` / `energy` / `meter_reading` / `energy_consumed` / `today_energy` / `total_energy`
3. 设备 state 值（若非 unavailable/unknown/on/off 等）

**配置格式（在管理界面输入）：**

```
旧格式（每行一条）:
room, device_name, entity_id, power_entity

新格式（JSON，支持多条）:
[
  {"entity_id": "switch.fan", "device_name": "风扇", "room": "客厅"},
  {"entity_id": "switch.kettle", "power_entity": "sensor.kettle_power"}
]
```

所有字段均可选填。

#### 午夜拆分

每天 00:00:00 自动执行：
- 检测所有未关闭的设备记录
- 将跨天记录拆分为两条：前一天记录在 23:59:59 关闭，当天 00:00:00 开启新记录
- 自动计算各段的持续时长和用电量
- 读取当前电表读数作为拆分的 off_power

#### 启动恢复机制

集成会在本地 JSON 缓存文件中记录所有未关闭的设备开关事件。当 HA 重启时：
- **停机 ≤ 30 分钟**：自动使用当前时间作为关机时间补全记录
- **停机 > 30 分钟**：自动删除异常的未关闭记录
- **设备仍然开机**：保留记录和缓存

#### 定时修正扫描

每 10 分钟扫描今日数据，检测同一设备的多条未关闭记录异常，通过 HA recorder 查询历史状态进行修正。

---

### 2. 传感器类数据采集

定时对指定实体进行轮询，将数据写入独立的指标分表。

**支持的指标类型：**

| 指标 | 表名 | 值类型 | 说明 |
|------|------|--------|------|
| `temperature` | `env_temperature` | REAL | 温度 |
| `humidity` | `env_humidity` | REAL | 湿度 |
| `pm25` | `env_pm25` | REAL | PM2.5 |
| `co2` | `env_co2` | REAL | 二氧化碳 |
| `power` | `env_power` | REAL | 功率/电量 |
| `sensor` | `env_sensor` | TEXT | 通用传感器（支持非数值，如光照度、AQI） |

**配置格式（在管理界面输入）：**

```
旧格式（每行一条）:
metric_type: room, entity_id, interval_minutes, [int|json|keep_blank]

示例:
power: 客厅, sensor.power, 10, int
sensor: 卧室, sensor.illuminance, 15

新格式（JSON）:
[
  {"entity_id": "sensor.temperature", "metric_type": "temperature", "room": "厨房", "collect_interval": 5, "round_minute": 0}
]
```

- `collect_interval`：采集间隔（分钟）
- `round_minute`：整分钟对齐模式（`int` = 在整分钟边界采集，如 10,int 在 0/10/20/30/40/50 分采集）
- `sensor` 指标的值类型为 TEXT，支持非数值 state（如 `bright`、`rainy` 等）
- 所有指标的 `value` 为 NULL 的记录也会记录，用于标记采集时间点

---

### 3. 属性提取

提取实体状态属性中的指定字段，独立建表存储。可在数据库浏览器中配置。

**三种模式：**

| 模式 | 常量 | 说明 |
|------|------|------|
| **字段快照** | `fields` | 提取实体属性的指定字段，每字段一列，定时快照记录 |
| **列表展开** | `list` | 将实体属性的某个数组展开，每个数组元素一行，可指定 key 字段做去重 |
| **混合模式** | `multi` | 列表展开 + 附加标量字段，extra_fields 做独立列，extra_json_nodes 合并到 JSON 列 |

**配置示例（在数据库浏览器中操作）：**

1. 在 `attr_type_defs` 表中定义属性类型
2. 在 `entity_configs` 表中添加对应属性实体配置
3. 系统自动为每种属性类型创建 `attr_{type_name}` 表

---

### 4. 自定义路由

通过 GUI 或 API 定义自定义 HTTP 路由，绑定任意 SQL 查询。

**通过管理界面配置：**
- 路由路径：`/api/ha_data_store/custom/{path}`
- SQL 语句：支持参数占位符，如 `?entity_id`、`?date`
- 描述：可选的说明文字

**通过 API 配置：**

```bash
# 添加路由
curl -X POST /api/ha_data_store/routes \
  -H "Content-Type: application/json" \
  -d '{"route_path": "my_query", "sql_statement": "SELECT * FROM device_history WHERE entity_id = '?entity_id'"}'
```

**SQL 安全沙箱：** 禁止 DROP、DELETE、UPDATE、INSERT、ALTER、CREATE、TRUNCATE、EXEC、EXECUTE 等危险关键字。

**万能动态路由：** 任何没有对应静态路由的 `/api/ha_data_store/custom/{tail}` 请求都会自动查询 `custom_routes` 表并执行 SQL 返回结果。

---

### 5. 设备桥接

通过 WebSocket 连接远程 HA 实例，将远程实体的状态和控制在本地无缝映射。

**支持的实体类型：**

| 类型 | 可读写 | 远程控制方式 |
|------|--------|-------------|
| switch | 可读写 | REST API 转发 turn_on/turn_off |
| light | 可读写 | REST API 转发 turn_on/turn_off + brightness |
| climate | 可读写 | REST API 转发 set_hvac_mode + set_temperature |
| cover | 可读写 | REST API 转发 open_cover/close_cover |
| fan | 可读写 | REST API 转发 turn_on/turn_off |
| lock | 可读写 | REST API 转发 lock/unlock |
| number | 可读写 | REST API 转发 set_value |
| select | 可读写 | REST API 转发 select_option |
| sensor | 只读 | WebSocket 推送状态同步 |
| binary_sensor | 只读 | WebSocket 推送状态同步 |

**配置步骤：**

1. **添加远程连接**：输入远程 HA 地址、长期访问令牌、SSL 验证选项
2. **添加桥接实体**：从连接中选择一个或多个实体 ID 进行桥接
3. 系统自动在本地创建对应的代理实体，操作和状态与远程同步

**断线重连：** 自动检测 WebSocket 断开并重连，采用指数退避（5 秒 → 5 分钟）。

---

### 6. 文件源 → 实体

监听本地 JSON 文件的变化，自动将数据映射为 HA 实体。

**处理流程：**
1. 每 5 秒检查文件 mtime 是否变化
2. 读取 JSON 文件内容
3. 根据 `state_field` 提取状态值
4. 创建或更新 `sensor.file_{name}` 实体
5. JSON 中的其他字段自动映射为实体属性

**配置通过 API：**

```bash
POST /api/ha_data_store/file_source
{
  "name": "weather_data",
  "file_path": "/config/data/weather.json",
  "state_field": "temperature",
  "entity_prefix": "sensor.file_",
  "poll_interval": 10
}
```

---

### 7. API源 → 实体

定时请求外部 HTTP API，将 JSON 响应解析并映射为 HA 实体。

**配置通过 API：**

```bash
POST /api/ha_data_store/api_source
{
  "name": "external_weather",
  "url": "https://api.example.com/weather",
  "method": "GET",
  "headers_json": "{\"Authorization\": \"Bearer token123\"}",
  "state_field": "current.temp",
  "entity_prefix": "sensor.api_",
  "poll_interval": 60,
  "timeout": 15,
  "max_retries": 5
}
```

- 支持点号路径深度提取（如 `data.temperature.value`）
- 自动重试（失败指数退避）
- 健康状态监控（在系统信息传感器中展示失败次数）

---

### 8. 虚拟设备

动态创建自定义实体，支持多种设备类型和自定义属性。

**配置通过 API：**

```bash
POST /api/ha_data_store/virtual_device
{
  "entity_id": "sensor.virtual_room_temp",
  "device_type": "sensor",
  "device_name": "虚拟房间温度",
  "entity_name": "Room Temp",
  "extra_config": {
    "unit_of_measurement": "°C",
    "icon": "mdi:thermometer"
  }
}
```

虚拟设备会持久化到 `virtual_devices` 表，HA 重启后自动恢复。

---

### 9. 实体导出为JSON

将指定 HA 实体的状态实时导出为 JSON 文件，供外部系统消费。

**配置通过 API：**

```bash
POST /api/ha_data_store/export
{
  "entity_id": "sensor.power_meter",
  "file_name": "power_export.json"
}
```

每当实体的状态发生变化时，系统会自动写入对应的 JSON 文件。

---

### 10. 打印机数据采集

采集打印机统计数据与当日作业明细，支持多台打印机、配置管理、数据查询与系统监控。

**配置两个实体（通过"系统配置 → 打印机配置"）：**

| 实体类型 | 数据来源 | 说明 |
|---|---|---|
| **统计数据实体** | `attributes.daylist` | 每日汇总（print/scan/copy/fax/jam_printer）+ 墨量（ink_*） |
| **当日详细数据实体** | `attributes` 各类型明细数组 | 当日各类型作业明细 |

**采集时机：**
- 以实体**状态值变化**判定触发（统计实体 state 为五项累计合计、详细实体为当日作业总数，每次打印都变化）
- 当日多次打印时，每次变化都会覆盖更新当日记录（汇总 + 墨量 + 明细），保证始终为最新
- 保存配置时主动采集一次当前数据

**数据存储（单张主记录表 `printer_daily`，每天一条）：**
- `name`：打印机名称（用户设置）
- `day`：日期
- `print/scan/copy/fax/jam_printer`：当日汇总计数
- `ink_black/ink_cyan/ink_magenta/ink_yellow`：墨量
- `printer_jobs`：当日作业明细（JSON，含各类型作业数组 + total + date）

**配置接口：**
```bash
GET    /api/ha_data_store/printer/configs                     # 配置列表
POST   /api/ha_data_store/printer/configs                     # 新增/修改配置
DELETE /api/ha_data_store/printer/configs?id=xxx              # 删除配置
POST   /api/ha_data_store/printer/configs/recollect?name=xxx  # 主动重采
```

**数据查询（详见下方"数据查询接口"的 `printer_*` 类型）：**
| 查询类型 | 功能 |
|---|---|
| `printer_years` | 打印机有哪些年数据 |
| `printer_month_dates` | 指定月哪些日期有数据 |
| `printer_total` | 打印机合计数据 |
| `printer_monthly_total` | 按年月统计合计数据 |
| `printer_daily_range` | 指定日期区间数据 |
| `printer_detail` | 指定日期详细数据 |

---

## API 接口文档

所有 API 通过 `/api/ha_data_store/` 路径访问。外部访问需在 URL 或 Header 中携带 API Key。

### 数据查询接口

```
GET /api/ha_data_store/query?type=xxx&key=你的APIKey
```

**通用参数：**
| 参数 | 默认值 | 说明 |
|------|--------|------|
| `entity_id` | - | 实体 ID |
| `metric` | - | 指标类型（环境类查询） |
| `date` | - | 日期 `YYYY-MM-DD` |
| `month` | - | 月份 `YYYY-MM` |
| `year` | - | 年份 `YYYY` |
| `start` / `end` | - | 起止日期范围 |
| `limit` | 500 | 返回条数上限（0=不限制） |
| `offset` | 0 | 分页偏移 |
| `order_by` | - | 排序字段（如 `datetime DESC`） |
| `fields` | - | 返回字段（逗号分隔） |
| `detail` | false | 是否返回详细记录（仅汇总类查询） |
| `room` | - | 房间过滤 |
| `key` | - | API Key |

**查询类型一览：**

| type | 说明 | 必需参数 |
|------|------|---------|
| `device_history` | 设备开关记录（按日/月/年智能返回，内嵌汇总） | entity_id |
| `device_summary` | 纯汇总（只返回统计数字，不返回记录） | entity_id, date/month/year(可选) |
| `env_history` | 环境历史记录（含最新日期、总条数等元数据） | entity_id, metric |
| `env_latest` | 环境最新一条记录 | entity_id, metric |
| `attr_history` | 属性历史 | entity_id, attr_type(可选) |
| `attr_latest` | 属性最新 | entity_id, attr_type(可选) |
| `entities` | 已配置实体列表 | 无 |
| `rooms_daily` | 按房间每日汇总 | date |
| `rooms_multi_metric` | 按房间多指标汇总 | date, room |
| `aggregate_daily` | 所有实体按日聚合 | - |
| `aggregate_monthly` | 所有实体按月聚合 | - |
| `aggregate_yearly` | 所有实体按年聚合 | - |
| `ranking_daily` | 日排行榜 | - |
| `ranking_monthly` | 月排行榜 | - |
| `ranking_yearly` | 年排行榜 | - |
| `vacuum_history` | 扫地机器人轨迹历史 | vacuum_id(可选) |
| `electricity_standard` | 电量标准数据 | 无 |
| `health_history` | 健康数据历史 | name(可选) |
| `health_latest` | 最新健康数据 | name(可选) |
| `entity_data_dates` | 实体有数据的所有日期 | entity_id |
| `room_data_dates` | 房间指定月有数据日期（按 device/environment/attribute 多选查询） | room, month, category |
| `xiaoai_history` | 小爱对话记录 | entity_id |
| `printer_years` | 打印机有哪些年数据 | stats_entity |
| `printer_month_dates` | 打印机指定月哪些日期有数据 | stats_entity, month |
| `printer_total` | 打印机合计数据 | stats_entity |
| `printer_monthly_total` | 打印机按年月统计合计数据 | stats_entity |
| `printer_daily_range` | 打印机指定日期区间数据 | stats_entity, start/end |
| `printer_detail` | 打印机指定日期详细数据 | stats_entity, date |

**查询示例：**

```bash
# 查询设备开关记录
curl "http://ha:8123/api/ha_data_store/query?type=device_history&entity_id=switch.fan&date=2024-01-15&key=your_api_key"

# 查询环境历史
curl "http://ha:8123/api/ha_data_store/query?type=env_history&entity_id=sensor.temperature&metric=temperature&limit=100"

# 查询按房间的每日汇总
curl "http://ha:8123/api/ha_data_store/query?type=rooms_daily&date=2024-01-15&key=your_api_key"

# 查询月排行榜
curl "http://ha:8123/api/ha_data_store/query?type=ranking_monthly&month=2024-01&detail=true"

# 查询客厅 2025-01 哪些日期有数据
curl "http://ha:8123/api/ha_data_store/query?type=room_data_dates&room=客厅&month=2025-01&category=device&key=your_api_key"
```

### 配置管理接口

```
GET  /api/ha_data_store/config         → 获取所有监控实体配置
POST /api/ha_data_store/config         → 新增/修改监控实体配置
```

**POST 请求体示例：**

```json
[
  {
    "entity_id": "switch.fan",
    "category": "device",
    "room": "客厅",
    "device_name": "风扇",
    "power_entity": "sensor.fan_power",
    "enabled": 1
  }
]
```

```
GET  /api/ha_data_store/routes         → 获取所有自定义路由
POST /api/ha_data_store/routes         → 新增/修改自定义路由
GET  /api/ha_data_store/attr_types     → 获取所有属性类型定义
GET  /api/ha_data_store/entity_state?entity_id=xxx → 获取实体状态+属性树
```

**打印机配置接口：**

```
GET    /api/ha_data_store/printer/configs                      → 配置列表
POST   /api/ha_data_store/printer/configs                      → 新增/修改配置
DELETE /api/ha_data_store/printer/configs?id=xxx               → 删除配置
POST   /api/ha_data_store/printer/configs/recollect?name=xxx   → 主动重采指定打印机
```

**打印机配置 POST 请求体示例：**

```json
{
  "name": "HP Printer",
  "stats_entity": "sensor.hp_printer_yong_liang_tong_ji",
  "detail_entity": "sensor.hp_printer_jin_ri_zuo_ye",
  "enabled": true
}
```

### 管理接口（仅局域网）

以下接口仅允许局域网同网段访问，需要管理员密码登录。

| 接口 | 方法 | 说明 |
|------|------|------|
| `/api/ha_data_store/db_viewer` | GET | 管理页面 |
| `/api/ha_data_store/db_viewer/data?table=xxx` | GET | 表数据分页 |
| `/api/ha_data_store/db_viewer/update` | POST | 编辑单元格 |
| `/api/ha_data_store/db_viewer/update` | DELETE | 删除行 |
| `/api/ha_data_store/apikey` | GET | 列出所有 API Key |
| `/api/ha_data_store/apikey` | POST | 创建 API Key |
| `/api/ha_data_store/apikey` | DELETE | 删除 API Key |
| `/api/ha_data_store/apikey/settings` | POST | 修改管理员密码 |
| `/api/ha_data_store/stats` | GET | 数据统计（各行数+磁盘大小） |
| `/api/ha_data_store/logs` | GET | 本地日志文件列表 |
| `/api/ha_data_store/logs/content?date=YYYY-MM-DD` | GET | 查看日志内容 |
| `/api/ha_data_store/export` | GET/POST/DELETE | 导出配置管理 |
| `/api/ha_data_store/file_source` | GET/POST/DELETE | 文件源配置管理 |
| `/api/ha_data_store/api_source` | GET/POST/DELETE | API源配置管理 |
| `/api/ha_data_store/push_targets` | GET/POST/DELETE | 推送目标管理 |
| `/api/ha_data_store/attr_config` | GET/POST | 属性类型配置管理 |
| `/api/ha_data_store/attr_manual_trigger` | POST | 手动触发属性采集 |
| `/api/ha_data_store/vacuum_type_defs` | GET/POST | 扫地机器人类型管理 |
| `/api/ha_data_store/vacuum_configs` | GET/POST/DELETE | 扫地机器人配置管理 |
| `/api/ha_data_store/bridge_connections` | GET/POST | 桥接连接配置管理 |
| `/api/ha_data_store/bridge_entities` | GET/POST/DELETE | 桥接实体配置管理 |
| `/api/ha_data_store/bridge_reload` | POST | 重新加载所有桥接连接 |
| `/api/ha_data_store/virtual_device` | GET/POST/DELETE | 虚拟设备管理 |
| `/api/ha_data_store/health_add` | POST | 添加健康记录 |
| `/api/ha_data_store/health_types` | GET/POST/DELETE | 健康数据类型管理 |
| `/api/ha_data_store/batch_entity_state` | POST | 批量写入实体状态 |
| `/api/ha_data_store/db_maintain` | POST | 数据库维护（VACUUM/REINDEX） |
| `/api/ha_data_store/entity_monitor` | GET | 实体在线监控 |

### 高级接口

#### 万能动态路由

```
GET /api/ha_data_store/custom/{tail}
```

任何没有对应静态路由的路径会从 `custom_routes` 表中查找并执行 SQL。SQL 中的 `?param` 格式参数会自动从 URL 查询参数中提取。

**示例：**
- 定义路由路径：`my_custom`
- SQL 语句：`SELECT * FROM device_history WHERE entity_id = '?entity_id' AND date(on_time) = '?date'`
- 访问：`/api/ha_data_store/custom/my_custom?entity_id=switch.fan&date=2024-01-15`

**高级用法 — 无路由名时使用 `q` 参数：**

```
GET /api/ha_data_store/custom?q=SELECT...&key=xxx
```

直接传递 SQL 语句（安全沙箱限制写入操作）。

---

## 内置数据库浏览器

访问 `http://你的HA地址:8123/api/ha_data_store/db_viewer`

**特性：**
- 表结构查看（列名、类型、约束）
- 数据分页浏览
- 在线编辑、删除行
- 按列排序
- 直接添加新行

**安全限制：** 默认仅限同网段访问，需要管理员密码登录（默认 `admin`）。

---

## 控制开关

集成会自动创建三个开关实体，用于控制 API 的安全访问：

| 实体 ID | 名称 | 说明 |
|---------|------|------|
| `switch.ha_data_store_api` | API 访问 | OFF 时所有 API 请求返回 403 |
| `switch.ha_data_store_db_browse` | 数据库浏览器 | OFF 时禁止查看数据内容 |
| `switch.ha_data_store_db_modify` | 数据库修改 | OFF 时禁止写入操作 |
| `switch.ha_data_store_remote_access` | 远程访问 | OFF 时仅允许同网段访问数据库浏览器；ON 时允许任意网段访问（默认关闭，重启后自动重置为关闭） |

---

## 安全架构

```
外部网络                    局域网
    │                         │
    │  query?key=xxx          │  db_viewer（管理页）
    │  ✅ 允许               │  ⚠️ 同网段+密码
    │                         │  （远程访问开关可放行跨网段）
    │  db_viewer              │  密钥管理 → 需密码
    │  ❌ 拒绝                │  修改密码 → 需旧密码
```

**API Key 鉴权方式：**
- URL 参数：`?key=your_api_key`
- Header：`Authorization: Bearer your_api_key`

**安全管理：**
- 三个独立开关控制 API/浏览/修改
- 管理员密码用于管理页面登录
- API Key 可独立启用/禁用
- SQL 注入防护：禁止危险关键字
- 子网检测：管理接口仅限同 /24 子网

---

## 数据库表结构

数据库文件位于 `{config_dir}/storage/ha_data_store.db`，使用 SQLite WAL 模式。

### 核心表

| 表名 | 说明 |
|------|------|
| `entity_configs` | 实体配置（联合主键 entity_id + attr_type） |
| `device_history` | 设备开关历史记录 |
| `custom_routes` | 自定义路由定义 |
| `api_keys` | API 密钥 |
| `api_settings` | API 设置（管理员密码等） |

### 传感器数据表

每种指标独立建表：

| 表名 | 指标 |
|------|------|
| `env_temperature` | 温度 |
| `env_humidity` | 湿度 |
| `env_pm25` | PM2.5 |
| `env_co2` | 二氧化碳 |
| `env_power` | 功率/电量 |
| `env_sensor` | 通用传感器（TEXT 类型） |

### 属性提取表

按类型动态创建：`attr_{type_name}`

### 配置表

| 表名 | 说明 |
|------|------|
| `attr_type_defs` | 属性类型定义 |
| `export_configs` | 导出配置 |
| `file_source_configs` | 文件源配置（JSON→实体） |
| `api_source_configs` | API源配置（API→实体） |
| `push_targets` | 推送目标配置（实体→HTTP） |

### 桥接表

| 表名 | 说明 |
|------|------|
| `bridge_connections` | 远程 HA 连接配置 |
| `bridge_entities` | 桥接实体列表 |

### 其他表

| 表名 | 说明 |
|------|------|
| `vacuum_type_defs` | 扫地机器人类型定义 |
| `vacuum_configs` | 扫地机器人配置 |
| `vacuum_history` | 扫地机器人轨迹 |
| `health_records` | 健康记录（血压/体温/体重等） |
| `virtual_devices` | 虚拟设备持久化 |
| `printer_configs` | 打印机配置（支持多台，name 唯一） |
| `printer_daily` | 打印机每日记录（汇总 + 墨量 + 当日明细 JSON） |

---

## 日志系统

集成内置每日滚动的本地日志系统，日志文件保存在集成目录的 `logs/` 文件夹下：

```
{ha_data_store目录}/logs/2024-01-15.log
{ha_data_store目录}/logs/2024-01-16.log
```

**特性：**
- 每个自然日一个文件，格式 `YYYY-MM-DD.log`
- 自动保留最近 N 天（默认 7 天），过期自动清理
- 线程安全，可在线程池中写入
- 日志格式：`[时间] [级别] 消息`

**查看日志：**
- 通过 API：`GET /api/ha_data_store/logs`（列表）和 `GET /api/ha_data_store/logs/content?date=YYYY-MM-DD`
- 通过管理界面：在日志保留设置中可调整保留天数

---

## 常见问题

### Q: 如何添加多个实体？

在管理界面的"添加设备类实体"或"添加传感器类实体"中，使用 JSON 格式支持一次添加多个：

```json
[
  {"entity_id": "switch.device1", "room": "客厅"},
  {"entity_id": "switch.device2", "room": "卧室"}
]
```

### Q: 外网如何访问？

推荐使用 Cloudflare Tunnel（Cloudflared Add-on）进行零配置穿透：

```
https://ha.你的域名.com/api/ha_data_store/query?type=xxx&key=你的Key
```

### Q: 如何重置管理员密码？

通过 API：
```bash
curl -X POST /api/ha_data_store/apikey/settings \
  -H "Content-Type: application/json" \
  -d '{"old_password": "旧密码", "new_password": "新密码"}'
```

或者直接在数据库浏览器中编辑 `api_settings` 表的 `admin_password` 记录。

### Q: 数据量大会不会影响 HA 性能？

- 所有数据库写操作在 HA 线程池（executor）中执行，不阻塞事件循环
- SQLite 使用 WAL 模式，读写不互斥
- 传感器轮询在整秒边界对齐，避免高频写入

### Q: 设备桥接支持哪些认证方式？

使用远程 HA 的**长期访问令牌**（Long-Lived Access Token）。在远程 HA 的"用户资料"→"安全"→"长期访问令牌"中生成。

### Q: 数据库文件位置？

```
{HA配置目录}/storage/ha_data_store.db
```

### Q: 如何清空数据？

1. 在 HA 中删除集成
2. 删除 `{HA配置目录}/storage/ha_data_store.db` 文件
3. 重新添加集成

---

## 技术栈

- **运行环境**: Home Assistant (Python)
- **数据库**: SQLite (WAL 模式)
- **API 框架**: Home Assistant HTTP View (aiohttp)
- **桥接协议**: WebSocket (aiohttp) + REST API
- **配置方式**: Config Flow / Options Flow / REST API

---

> **注意**：本集成是一个综合性数据平台，功能丰富但配置复杂度较高。建议先配置设备类和传感器类采集核心数据，再逐步探索属性提取、桥接等高级功能。

---

## 更新日志

### v2.6.0 打印机数据采集

#### 新增功能

- **打印机数据采集** — 新增独立模块 `printer.py`，采集打印机统计数据与当日作业明细
  - 支持多台打印机，通过"系统配置 → 打印机配置"添加
  - 采集触发：以实体**状态值变化**判断（统计实体 state 为五项累计合计、详细实体为当日作业总数）
  - 当日多次打印时，每次变化都覆盖更新当日记录（汇总 + 墨量 + 明细），保证始终为最新
  - 保存配置时主动采集一次当前数据

- **数据查询** — API 工具新增"打印数据查询"分组，提供 6 种查询：
  - `printer_years` 有哪些年数据 / `printer_month_dates` 指定月日期 / `printer_total` 合计
  - `printer_monthly_total` 按年月统计合计 / `printer_daily_range` 日期区间 / `printer_detail` 指定日明细

- **系统监控** — 新增"🖨️ 打印机监控"统计卡片与折叠区块，展示每台打印机状态、墨量、当日/累计数据

#### 涉及文件

| 文件 | 说明 |
|------|------|
| `printer.py` | 新增：建表、采集、配置 CRUD、数据查询、主动重采 |
| `__init__.py` | 采集接入、实体白名单、API 注册、打印机独立采集分支 |
| `http_api.py` | 万能查询 `printer_*` 分发、`/monitor` 返回打印机监控数据 |
| `db_viewer.html` | 系统配置打印机子页面、API 打印数据查询分组、系统监控打印机卡片与区块 |
| `const.py` | 版本号更新为 `2.6.0` |
| `manifest.json` | 版本号更新为 `2.6.0` |

### v2.0.1

#### 新增功能

- **远程访问开关** — 数据库浏览器新增 `switch.ha_data_store_remote_access` 开关，控制是否允许非同网段（跨网段/外网）客户端访问管理页面
  - 默认关闭状态（仅允许局域网 /24 同网段访问）
  - 重启 Home Assistant 后自动重置为关闭状态，不保留上次开关状态
  - 开启后仍需输入管理员密码登录，不影响现有安全机制

#### Bug 修复

- **移动端 403 下载问题** — 修复数据库浏览器在非同网段访问被拒时，移动端浏览器误触发下载 0kb 文件的问题（响应缺少 content-type 导致浏览器无法正确识别内容）

#### 涉及文件

| 文件 | 说明 |
|------|------|
| `switch.py` | 新增 `HaDataStoreRemoteAccessSwitch` 实体类 |
| `__init__.py` | 初始化 `allow_remote_access = False` |
| `http_api.py` | 同网段检查逻辑增加开关控制；403 响应补全 content-type |
| `translations/zh-Hans.json` | 中文翻译：远程访问 |
| `translations/en.json` | 英文翻译：Remote Access |
| `strings.json` | 默认语言翻译 |

#### 安全建议

> 此开关仅影响数据库浏览器页面（db_viewer），API 接口不受影响。
>
> - 建议使用完毕后及时手动关闭
> - 公网暴露场景下建议配合反向代理 + HTTPS + 强密码使用
