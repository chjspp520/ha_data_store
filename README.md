# HA 数据统一存储系统 (ha_data_store)

ha_data_store 是一款 Home Assistant 自定义集成：**无需修改 `configuration.yaml`**，全部通过配置界面与内置管理页面完成，把「采集 → 存储 → 分析 → 展示 → 远程交互」串成一套家庭数据平台。

| 环节 | 能力 |
|---|---|
| **采集** | 设备开关记录（含跨午夜拆分、启动恢复、用电量核算）、传感器环境数据、实体属性提取、扫地机轨迹、健康记录、打印机用量、小爱对话 |
| **存储** | 统一落库 SQLite（WAL 模式）；支持整库备份与「排队 + 重启后原子应用」式恢复 |
| **对外** | 完整 HTTP API（API Key 鉴权、安全沙箱下的自定义 SQL 路由）、**实体→网络（读数据 / 控制设备）**、实体→JSON 文件、文件源 / API 源 ↔ 实体映射 |
| **交互** | 远程 HA 设备桥接（WebSocket）、虚拟设备、原生辅助元素自管实体化、轻量自动化引擎 |
| **分析展示** | 内置数据库浏览器、可视化查询构造器、通用指标引擎、家庭洞察、今日家庭状态中文总结、系统健康监控传感器 |
| **安全** | API 访问 / 数据库浏览 / 数据库修改 / 实体网络控制 等独立开关，控制类接口另有读写凭证分离、动作白名单、参数锁定与审计日志 |
 
--- 

## 目录

- [功能总览](#功能总览)
- [安装](#安装)
- [快速开始](#快速开始)
- [功能详解](#功能详解)
  - [0. 今日家庭状态总结](#0-今日家庭状态总结)
  - [1. 设备类数据采集](#1-设备类数据采集)
  - [2. 传感器类数据采集](#2-传感器类数据采集)
  - [3. 属性提取](#3-属性提取)
  - [4. 自定义路由](#4-自定义路由)
  - [5. 设备桥接](#5-设备桥接)
  - [6. 文件源 → 实体](#6-文件源--实体)
  - [7. API源 → 实体](#7-api源--实体)
  - [8. 虚拟设备](#8-虚拟设备)
  - [9. 实体导出为JSON](#9-实体导出为json)
  - [10. 打印机数据采集](#10-打印机数据采集)
  - [11. 用户操作记录](#11-用户操作记录)
  - [12. 辅助元素](#12-辅助元素)
  - [13. 用电计量](#13-用电计量)
  - [14. 设备清理](#14-设备清理)
  - [15. 可视化查询构造器 与 数据库新建表](#15-可视化查询构造器-与-数据库新建表)
  - [16. 接口管理（新接口 · 免重启）](#16-接口管理新接口--免重启)
  - [17. 家庭洞察（统一事件流 / 房间占用）](#17-家庭洞察统一事件流--房间占用)
  - [18. 通用指标引擎（元数据驱动 · `metrics_catalog`）](#18-通用指标引擎元数据驱动--metrics_catalog)
  - [19. 实体→网络（读数据 / 控制）](#19-实体网络读数据--控制)
  - [20. 整库备份](#20-整库备份)
- [API 接口文档](#api-接口文档)
  - [数据查询接口](#数据查询接口)
  - [配置管理接口](#配置管理接口)
  - [管理接口（仅局域网）](#管理接口仅局域网)
  - [高级接口](#高级接口)
- [内置数据库浏览器](#内置数据库浏览器)
- [控制开关](#控制开关)
- [安全架构](#安全架构)
- [数据库表结构](#数据库表结构)
- [日志系统](#日志系统)
- [常见问题](#常见问题)
- [技术栈](#技术栈)
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
| 🖥️ **虚拟设备** | 动态创建自定义实体，支持多种设备类型和自定义属性；支持导出/导入（配置+状态）跨机迁移 |
| 🧩 **辅助元素** | 扫描原生 HA 辅助元素（input_* / counter / binary_sensor）导出，B 机导入转为本集成自管实体（switch/number/select/button/text/binary_sensor），支持前台新建、全选批量删除；汇总实体 `sensor.ha_data_store_helper` |
| ⚡ **用电计量** | 登记功率实体，10 秒采样积分自动生成日/月/年用电量实体并按天入库；汇总实体 `sensor.ha_data_store_all_power`；API 工具含查询分组 |
| 🧹 **设备清理** | 一键扫描并清理本集成下无实体的空设备（安全，不误删主设备） |
| 💾 **整库备份** | 顶部选项卡「💾 数据备份」：`VACUUM INTO` / 在线备份取一致快照，支持关闭·每小时·每天·每周计划 + 保留份数；恢复采用「校验后排队 → 重启 HA 时在数据库被打开前原子替换」，替换前自动留快照；支持备份到 NAS 挂载点（本地生成校验后再写入共享） |
| 💬 **小爱对话** | 采集小爱音箱的对话记录（用户说话 / AI 回复 / 对话时间）落库，可查询历史与统计 |
| 🖨️ **打印机数据采集** | 采集打印机统计数据与当日作业明细，支持多台、配置管理、数据查询与系统监控 |
| 📁 **文件源 → 实体** | 监听本地 JSON 文件变化，自动将数据映射为 HA 实体 |
| 🌐 **API源 → 实体** | 定时请求外部 HTTP API，将 JSON 响应解析并映射为 HA 实体 |
| 📄 **实体 → JSON 文件** | 将 HA 实体状态实时导出为 JSON 文件，供外部系统消费 |
| 🌐 **实体→网络（读 / 控制）** | 把实体映射成外部可直接调用的 HTTP 地址：**读地址**（`GET /push_data/{token}`，只读凭证）与**控制地址**（`POST /push_control/{token}`，独立写凭证；动作白名单 + 参数可锁定为固定值/限幅 + 限流 + 审计日志）；同一实体可挂多套配置，总闸为开关「实体网络控制」 |
| 🔑 **API 密钥** | API Key 鉴权，支持多密钥，可独立开关 |
| 🛡️ **安全控制** | 三个独立开关控制 API 访问、数据库浏览、数据库修改 |
| 📈 **系统监控** | `sensor.ha_data_store_info` 实时展示 6 大类健康状态（设备/环境/属性/导出/文件源/API源） |
| 📋 **内置数据库浏览器** | 管理页面直接浏览、编辑数据库，无需 SQL 工具；支持可视化「新建表」 |
| 🧩 **查询构造器** | db_viewer 内可视化定义查询接口：选表 → 动态参数（字段/时间段/LIKE/多值 IN）→ 排序/上限 → 汇总(总条数/合计) → 试运行 → 发布；定义落库随库迁移 |
| 🔗 **自定义路由** | 通过 GUI 或 API 定义自定义 HTTP 路由，绑定任意 SQL 查询；支持发布开关、来源/状态管理 |
| 🗂️ **统一泛域名动态路由** | 万能路由 `/api/ha_data_store/custom/{tail}` 运行时查库执行任意自定义 SQL |
| 🏠 **今日家庭状态总结** | 聚合历史表生成今日家庭中文总结，`sensor.today_family_status` + 按钮/服务按需触发；启动后 1 分钟生成、之后每 30 秒刷新（逐台设备含运行中/实时状态/操作用户） |
| 🤖 **简单自动化引擎** | 定时/间隔触发 + 多条件判断 + 顺序执行服务动作，执行记录落库（30 秒调度，db_viewer 管理） |
| 📊 **自动化状态传感器** | `sensor.ha_data_store_automation` 实时统计自动化总数/启停/执行结果，前端自动化管理卡片数据源 |
| 🎯 **用户操作记录** | 前端埋点上报每次操作（含完整 action_snapshot + config_id），ts 采用实体状态时间与 device_history 精确关联，`sensor.近期使用设备` 窗口聚合（`number.ha_data_store_recent_days` 可调天数）；另含 `all` 节点（device_history 全量设备最近使用）与 `device_last_used` 查询接口，API工具支持多维度查询 |
| 📊 **通用指标引擎** | 元数据驱动：新表 `metrics_catalog` 存放指标定义（源表 / 值列或 `@value` 表达式 / 聚合 / 默认分组 / 过滤），通用引擎编译成参数化 SQL 执行；内置 20+ 指标（环境/设备/用电/健康/操作/自动化/attr_*），`GET /query?type=metrics_query&metric_id=xxx` 查询，db_viewer「📊 指标管理」增删改与试运行，**新增指标免重启** |
| 🏠 **全屋用电/用时** | `whole_house_usage` 查询按 年/月/日 返回 总计→房间→设备 三级统计（时长/用电/开启次数/运行中设备/设备数量/单纯房间名列表），API 工具含查询分组；汇总传感器 `sensor.ha_data_store_all_room_usage` 输出 本年/本月/今日 三级（状态=今日用电 kWh），每分钟刷新 |
| 🗂️ **全屋实体** | 传感器 `sensor.ha_data_store_all_entities` 按 `report_entities.entity_type` 分组展示全部上报实体（支持多值逗号拆分、跨节点归属），状态值=去重实体个数，表变化才更新 |
| 🕘 **家庭洞察** | 统一事件流 `timeline`（设备/操作/自动化/扫地机/小爱/健康/打印机合并成一条时间线，设备记录展开 on/off）+ 房间占用排行 `room_occupancy`（严谨区间并集、含门户事件与日/小时分解）；均按天/日期/时间段查询、不分页 |

---

## 安装

### 方式一：通过 HACS 安装（推荐）

1. 确保已安装 [HACS](https://hacs.xyz/)
2. 将本仓库（https://github.com/chjspp520/ha_data_store）添加为自定义仓库
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
| `sensor.today_family_status` | sensor | **状态值**=极简一句（家中有人/无人 + 开着几盏灯 + 入户门状态及时长，≤255字符）；`attributes.summary`=完整段落；`attributes.sections`=完整结构化分节；`attributes.overall`=normal/warning；`attributes.alerts`=异常提醒列表；`attributes.alert_text`=提醒文字；`attributes.offline`=离线设备数 |
| `button.ha_data_store_daily_summary` | button | 仪表盘放置按钮卡片，点击立即触发分析 |

**新增服务：** `ha_data_store.generate_daily_summary`
- 参数 `date`（可选，默认今天，格式 `yyyy-mm-dd`）
- 供自动化 / Node-RED 按需调用

**自动刷新：**
- HA 启动后 **1 分钟** 自动生成一次
- 之后每 **30 秒** 自动更新（设备 `running` / `state` / 操作用户等实时字段需时效性）
- 定时刷新**默认强制写入**（`sensor.FAMILY_STATUS_FORCE_WRITE = True`），`generated_at` / `last_updated` 每 30 秒确实刷新；置 `False` 则聚合内容未变化时跳过写入，减轻 recorder 压力
- 也可手动按钮 / 服务触发（手动触发始终强制写入）

**聚合维度与提醒阈值：**
| 节 | 数据源 | 内容 |
|----|--------|------|
| 环境 | env_temperature/humidity/pm25/co2 | 今日最高/最低/平均，房间温差≥2°C 时补充房间明细 |
| 设备 | device_history + 实时 states | 共 N 台/总时长 + 运行最久亮点 + **用电 TOP3 + 开关频次 TOP3**；完整逐台明细在 `sections.devices.devices[]`（含 `running`/`time`/`state`/`on_user`/`off_user`） |
| 用电 | env_power | 当日自增读数（最后一条 = 今日总用电，kWh）+ 昨日用电 + 环比 |
| 家庭事件 | vacuum_history / health_records / xiaoai_conversations | 扫地机次数、健康记录条数、小爱对话条数及时段 |
| 人在/门 | device_history（name=人在/入户门） | on_time 非空且 off_time 空=该房间有人/门开；否则家中无人/门关 |
| 灯光 | device_history（name 含"灯"） | 每盏灯取最新一条，on_time 非空且 off_time 空=该灯开着，统计"开着 x 盏灯" |
| 离线实体 | report_entities + 实时 states | 三态判定，unavailable 算离线、unknown 不算；有离线时 summary 末尾显示"离线设备 x 台" |

**字段说明：**
- `status_value`：极简状态值（`家中有人（客厅）、开着 5 盏灯、入户门开1小时8分钟`）
- `summary`：完整段落（不含提醒，含"离线设备 x 台"）
- `alert_text` / `alerts`：异常提醒（高温/运行超时/用电环比）
- `sections.lights`：开灯房间去重（`开着 6 盏灯（主卧、儿童房、客厅等 5 个房间）`）
- `sections.devices.times_top`：开关频次 TOP（`卫生间浴霸灯44 次`）
- `sections.devices.devices[]`：完整逐台明细；每台除 `entity_id/name/room/times/duration/energy` 外，另含实时/最近状态字段（`energy_top` / `times_top` 与 `devices` 共享同一份数据，字段同步生效）：

| 字段 | 说明 |
|------|------|
| `running` | 当日最新一条 `device_history`（`id` 最大）已开未关 = `true`（正在运行，历史口径） |
| `time` | 已关闭 = 该记录 `off_time`（最近一次关闭时间）；运行中 = `null` |
| `on_user` | 运行中 = 该记录 `on_user`（开机操作用户）；已关闭 = `""` |
| `off_user` | 已关闭 = 该记录 `off_user`（关机操作用户）；运行中 = `""` |
| `state` | HA **实时状态值**（`hass.states.get(entity_id).state`）；实体不在状态机中 = `null` |

> `state` 为实时值、`running` 为历史记录口径，两者可能短暂不一致（如实体已关但关机事件尚未落库），分别用于"看现状"与"看记录"。

**异常提醒（阈值写死）：** 高温 ≥30°C、低温 ≤5°C、单台连续运行 >6 小时、用电环比波动 >20%；存在任一提醒时 `overall=warning`，提醒文字放 `alert_text` 字段（`alerts` 为列表）；`summary` 段落末尾单独显示"离线设备 x 台"（离线不进 alerts）。

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
│  状态值=极简一句；attributes={summary, sections, overall,     │
│          alerts, alert_text, status_value, offline,          │
│          presence, lights, date, generated_at}               │
│  自动刷新：启动后1分钟 + 每30秒（默认强制写入状态）           │
└──────────────────────────────────────────────────────────────┘
```

**核心 SQL：**
- 环境：`SELECT value, room, datetime FROM env_temperature WHERE datetime LIKE '2026-08-21%'`
- 设备（今日，含单台用电与实时状态字段）：`SELECT entity_id, name, room, duration, energy_consumed, on_power, now_kwh, on_time, off_time, on_user, off_user FROM device_history WHERE on_time LIKE '2026-08-21%'`
  - 每实体取**当日最新一条**（`id` 最大）→ `running`（`off_time` 空）／`time`（`off_time`）／`on_user`／`off_user`；`state` 另取 HA 实时状态
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
- **on_user**: 开机操作用户（由前端操作记录 `user_actions` 按 `entity_id` + 时间关联回填）
- **off_user**: 关机操作用户（同上，关联回填）
- **icon**: 该设备对应前端卡片的图标（`device_history.entity_id = report_entities.entity_id` 时，取 `report_entities` 中该实体**最新上报**（`id` 最大）的 `icon` 写入，**原样写入、不过滤空值**；历史数据回填**不随 HA 启动执行**，由按钮 `button.ha_data_store_fill_device_icon` 按需触发，**覆盖更新**、不管原值是否为空，无上报记录的实体不动）

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

**发布管理（v3.5.0）：**
- 每条路由带 **启用/停用** 开关，停用后外部调用返回 403；无 `LIMIT` 的手写 SQL 执行时自动套用 `max_rows` 安全上限；
- 路由列表显示来源（🧩 构造器定义 / ✍️ 手写 SQL）与状态；手写 SQL 仍是完整路由定义的一部分；
- 不想手写 SQL？用 **🧩 查询构造器**（见 [15. 可视化查询构造器](#15-可视化查询构造器-与-数据库新建表)）可视化生成并发布。

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

### 11. 用户操作记录

前端 room-elves-card 通过埋点记录用户每次操作（开关、调值、按钮、场景、自动化等），上报后端存储，用于**分析使用习惯**和**将来还原设备控制面板**。

#### 数据存储（`user_actions` 表）

每条操作记录含：

| 字段 | 说明 |
|------|------|
| `user_name` | 当前登录用户 |
| `entity_id` | 操作实体 |
| `action` | 动作类型（toggle/set_value/press/call-service 等） |
| `name` / `icon` | 实体显示名 / 图标 |
| `room_name` | 所在房间 |
| `source` | 触发位置（head/room/standalone） |
| `card_type` | 弹窗卡片类型（预留） |
| `other` | 预留字段 |
| `state_log` | 操作前后状态变化（如 `on→off`、`cool→heat`） |
| `ts` / `ts_text` | 时间戳 / 人类可读时间 |
| `config_id` | 操作所属弹窗/选项卡/设备的 config_id（如 `diannao`），用于定位并还原完整弹窗配置 |
| `device_type` | 设备类型（如 light/socket/ac 等） |
| `action_snapshot` | 完整 tap_action 快照（JSON，用于还原设备面板） |

**时间权威源**：`ts` 取**实体状态变化时刻**（HA `last_changed`）而非前端点击时刻，前后端时间源统一，消除浏览器/服务器时钟偏差（前端 8 秒时间窗 + 最多 8 次轮询核对后覆盖）。后端 `ts_text` 由 `ts` 按秒精度本地化；`device_history.on_time/off_time` 同为实体状态时间（秒精度，毫秒自动截断），`_link_device_history_to_actions` 用 `on_time == ts_text` 精确匹配回填操作者，保证跨表时间严格一致。

#### 近期使用设备传感器

`user_actions` 写入后，`sensor.近期使用设备`（`sensor.ha_data_store_user_actions`）实时聚合**近 N 天**数据：

- **状态值** = 近 N 天有操作的不同设备面板数
- `attributes.devices` = 按 **action_snapshot 归一化聚合** 的设备列表，每项含：`entity_id / name / icon / room_name / count`（使用次数）/ `last_used` + `last_used_text`（最近使用）/ `state_log`（最近状态变化）/ `config_id` / `device_type` / `action_snapshot`（完整还原快照），按使用次数降序
- 30 秒定时刷新 + 写入后实时刷新

前端可直接订阅该 sensor，无需调 API 即可渲染"常用设备小卡片"，并凭 `action_snapshot` 还原设备控制面板。

##### `all` 节点（数据源 `device_history`）

`devices` 只覆盖**前端卡片埋点**；`all` 从 `device_history` 取**全量设备**的最近使用（含自动化、定时开关等非卡片操作），每个 `entity_id` 一条：

| 字段 | 说明 |
|------|------|
| `entity_id` / `name` / `room` / `icon` | 该实体最新一条记录的信息（`icon` 取 v3.6.8 新增的 `device_history.icon`） |
| `running` | 最新记录 `on_time` 有值且 `off_time` 为空 → `true`（正在运行） |
| `last_used_text` / `last_used` | 运行中 = **当前时刻**；否则 = 该记录 `off_time`（附毫秒时间戳） |
| `on_time` / `off_time` | 最新记录的开关时间 |
| `count` | 窗口内开关次数 |
| `duration` / `duration_hour` | 窗口内累计时长（秒 / 小时）；运行中记录按「当前时间 − `on_time`」计 |
| `energy` | 窗口内累计用电（kWh）；已关闭取 `energy_consumed`，运行中取 `now_kwh − on_power`，无来源记 0 |

- 按 `last_used` 倒序；额外属性：`total_all`（条数）、`all_range`（如"最近 30 天"）、`exclude_count`（生效排除项数）。

##### 窗口天数设置实体

`number.ha_data_store_recent_days`（**近期使用天数**）：范围 **1~365**、默认 **30**、单位「天」，
可直接在仪表盘调整；缺失/非法/超范围一律回退 30。同时作用于 `devices` 窗口、`all` 节点与
`device_last_used` 接口；**改动后立即刷新**传感器（`EVENT_STATE_CHANGED` 监听）。
设置值通过 **`RestoreEntity` 持久化**，**重启 HA 后保持**（不会回到默认 30）。

> 同类设置实体 `text.ha_data_store_ele_list`（用电计量列表条数）也已支持重启恢复。

窗口过滤作用于 `on_time`：从「今天 − (N−1) 天 00:00:00」起算（含今天共 N 个自然日）。

##### 排除项（db_viewer 配置，无字符数限制）

被排除的 `entity_id` 不进入 `all` 节点与 `device_last_used` 接口。存储于
`api_settings.recent_exclude_entities`（JSON 数组），在 db_viewer
**「系统配置 → 🕘 近期使用设备」** 子页配置：已选标签（可移除）+ 手动输入（逗号/换行）+
**实体选择器**（搜索 `entity_id / 名称 / 房间`，一键「➕ 排除」）。保存后立即刷新传感器。

| 接口 | 说明 |
|------|------|
| `GET /api/ha_data_store/recent/exclude` | 读取排除项 `{success, count, exclude[]}` |
| `POST /api/ha_data_store/recent/exclude` | 保存（Body `{"exclude":[...]}` 或 `{"text":"a,b\nc"}`） |
| `GET /api/ha_data_store/recent/entities` | `device_history` 实体唯一值 `{entity_id, name, room}`（供选择） |

#### 近期使用设备接口（`device_last_used`）

`GET /api/ha_data_store/query?type=device_last_used` —— 每个实体的最近使用情况（与 `all` 节点同源同口径），
**`entities` 为去重后的 entity_id 列表**（按最近使用倒序）：

| 参数 | 说明 |
|------|------|
| `entities` / `entity_id` | 逗号分隔实体（空 = 全部） |
| `start` / `end` | 时间段（作用 `on_time`） |
| `date` / `month` / `year` | 指定日 / 月 / 年 |
| `window_days` | 窗口天数（**仅在未传 start/end/date/month/year 时生效**；缺省读设置实体；**传 `0` = 不限窗口/全部历史**） |
| `filter` | **是否启用「排除项过滤」**，默认 `1`（启用，别名 `use_exclude`）；`0` = 不应用排除项（`exclude` 一并忽略），返回全部设备 |
| `exclude` | 逗号分隔排除实体；**不传** = 用保存的排除项，显式传空 = 不排除 |
| `running` | `1` 只返回正在运行的设备 |
| `detail` | `0` 只返回 `entities` 列表（不返回 `items` 明细），默认 1 |
| `limit` / `offset` | 分页（0 = 不限） |

时间过滤优先级：`start/end` > `date` > `month` > `year` > 窗口天数 —— 即**时间模式与窗口天数互斥**，
选了时间段/指定日/月/年后 `window_days` 自动不生效。
`filter=0` 只关闭**排除项过滤**，不影响时间/实体/运行中等其它条件；
需要「全量、不做任何过滤」时用 `?type=device_last_used&window_days=0&filter=0`。返回：

```json
{ "range": "最近 30 天", "window_days": 30, "use_exclude": true, "exclude_count": 2, "total": 42, "count": 42,
  "entities": ["light.a", "switch.b"],
  "items": [ { "entity_id": "light.a", "name": "大灯", "room": "客厅", "icon": "mdi:lightbulb",
               "running": false, "on_time": "...", "off_time": "...",
               "last_used": 1789778169240, "last_used_text": "2026-09-19 08:00:00",
               "count": 6, "duration": 3600.0, "duration_hour": 1.0, "energy": 0.5 } ] }
```

db_viewer「API 工具 → 设备类 → 🕘 近期使用设备」提供可视化参数表单：多实体 + **时间模式**
（全部时间 / **最近 N 天（窗口）** / 时间段 / 指定日 / 指定月 / 指定年）+ 是否应用排除项过滤 +
只看正在运行 + 是否返回明细。

> **时间模式与窗口天数互斥**：「最近 N 天（窗口）」是时间模式的一个选项（该选项只在本次查询接口可见，
> 切到其它接口自动隐藏并回落为「时间段」；进入本接口时默认选中），选中后出现「天数（留空 = 用设置实体）」
> 输入框；选「全部时间」则**真正不限窗口**（避免"全部时间却只返回 30 天"的歧义）。

##### 监控页展示与角标

- **系统监控** summary 新增卡片 **🕘 最近使用设备**（数字 = 统计到的设备数），点击展开对应区块，
  表格列：实体ID / 名称 / 房间 / 状态（运行中·已关闭）/ 最近使用 / 次数 / 时长(h) / 用电(kWh)，
  标题右侧显示 `统计范围 · 运行中 N · 排除项 M`；数据来自 `/monitor` 的 `recent` 节点（最多返回前 100 台）。
- **系统配置 → 🕘 近期使用设备** 子标签角标显示**排除项数量的负值**（如 `-5` 表示已排除 5 个实体），
  页面加载即显示，配置页内增删排除项即时反映。

#### 上报接口

```
POST /api/ha_data_store/action_log   Body: { actions: [ {user_name, entity_id, action, ..., action_snapshot} ] }
GET  /api/ha_data_store/action_log?days=30   查询近 N 天原始记录
```

前端本地缓冲，**操作停止 5 秒后统一上报**（防抖合并连续操作），**满 20 条立即上报**；fetch 15 秒超时（AbortController 防 pending 锁死）、失败**指数退避自动重试**（5s 起、上限 5 分钟）、上报前游标取 max 防多实例重复上报；fetch 前同步补核对（实体状态时间 + state_log 权威值）。

#### 查询（API 工具 → 🎯 用户动作查询组）

见 [数据查询接口](#数据查询接口) 的 `user_actions_*` 系列类型。

---

### 12. 辅助元素

原生 HA 辅助元素（`input_boolean/input_number/input_select/input_text/input_button/counter/binary_sensor`）的配置存于 HA `.storage`，无法由本集成直接管理。本功能把 A 机原生 helper 扫描导出为 JSON（配置 + 状态），在 B 机导入后转换为**本集成自管实体**（RestoreEntity，无需重启、状态自动回填并在重启后保持）：

| 源 | 目标 | 保留 |
|------|------|------|
| `input_boolean` | `switch` | icon/name + on/off |
| `input_number` | `number` | min/max/step/unit + 当前值 |
| `counter` | `number` | min/max/step/initial + 计数 |
| `input_select` | `select` | options + 当前选项 |
| `input_button` | `button` | 配置（无状态） |
| `input_text` | `text` | 文本值 |
| `binary_sensor` | `binary_sensor` | on/off |

- **UI**：db_viewer「系统配置 → 🧩 辅助元素」——已导入列表(可删/全选批量删)、扫描并导出、导出已导入项、导入文件(可覆盖同名)、🆕 前台新建；
- **汇总实体** `sensor.ha_data_store_helper`：状态 = 辅助元素个数，attributes `entities[]` = 每个实体明细（entity_id/name/icon/source_type/source_entity_id），30s 刷新；
- **接口**：`GET /api/ha_data_store/helper/scan`、`/helper/export`、`POST /helper/import`、`GET/POST/DELETE /helper`。

### 13. 用电计量

db_viewer「系统配置 → ⚡ 用电计量」登记**功率实体**（填功率实体 ID / 设备名 / 房间 / ID 段 / 单位 W·kW），系统 **10 秒采样积分**（功率 × 时间差）自动生成三个累计实体并按天入库：

- `sensor.ha_data_store_{id}_daily_ele`（日，今日累计）
- `sensor.ha_data_store_{id}_monthly_ele`（月，由日数据实时聚合）
- `sensor.ha_data_store_{id}_yearly_ele`（年，由日数据实时聚合）

> 这三个实体由 **ID 段自动派生**，是登记的产物，**不需要也不允许手填**。库中 `daily_entity_id` 字段即等于 `sensor.ha_data_store_{ID段}_daily_ele`，仅用于前端关联跳转查询，永远与实际注册的实体 ID 保持一致。

特性：
- 每天一条落库 `power_energy_daily`（60s 落盘 + 跨日自动分账，kwh 保留 3 位小数）；月/年不建表，实时聚合；
- 单位 W/kW 自动识别（登记优先，其次读实体 `unit_of_measurement`）；`unavailable/unknown` 不累计；采样空窗 >5 分钟丢弃（防停机误算）；
- 全部实体归入统一设备「用电计量」；重启自动恢复，卸载前自动落盘；
- **历史列表状态属性**：三个累计实体状态属性自动附带全量历史列表 —— 日用电 `daylist`（每日用电）、月用电 `monthlist`（每月用电）、年用电 `yearlist`（每年用电），元素形如 `{day|month|year, usage}`（usage 单位 kWh，保留 3 位小数）；全量不设上限、**无数据日期不占位**，**今天/本月/当年并入实时值**（与实体 state 一致），由 Manager 缓存并在 60s 落盘/跨日时重建，重启自动恢复；
- **汇总实体** `sensor.ha_data_store_all_power`：状态 = 用电实体个数，attributes 顶层 `total`（**不受 `ele_list` 条数限制**）= 合计节点 `{count, power, today, month, year, room[]}`：`power` 当前全屋功率(W，仅 ≥0 有效读数计入)，`today/month/year` 今日/本月/本年用电合计(kWh，直接对全部启用 meter 求和)，`room[]` 按房间汇总 `{room,count,power,today,month,year}`（room 为空归入「未分配」）；`entities[]` = 每个用电实体的明细（entity_id/name/icon/room/device/power_entity + `period` + 对应列表 daily→`daylist`、monthly→`monthlist`、yearly→`yearlist`，升序保留最近 N 条），30s 刷新；明细列表条数由设置实体 **`text.ha_data_store_ele_list`**（状态“日,月,年”，默认 `3,3,3`）控制，该 text 变化时立即刷新 all_power；三个用电实体自身的列表保持全量不受影响；
- **取消登记 = 软删除（回收站）**：点「取消登记」不会物理删除配置，而是把 `power_meter_configs` 行置为 `enabled=0` 归档并移入「♻️ 回收站」；历史日表 `power_energy_daily` **任何情况下都不删除**；
- **重新登记自动沿用历史数据**：对同一功率实体重新登记时，会自动沿用原有的 `id_slug` / 设备名 / 房间（表单里 ID 段留空即自动带出，或点行内「编辑」一键回填），因此实体 ID 不会分叉，历史日用电数据**无缝接续**；
- **日用电量实体自动派生**：登记时按 ID 段自动生成 `sensor.ha_data_store_{ID段}_daily_ele` 并写入 `daily_entity_id`（配置表 + 日表），表单中该项为**只读预览**、随 ID 段实时变化；启动时若发现历史值缺失或曾被手填错误，会自动按 ID 段纠正（也可点 **🔧 按 ID 段纠正日用电实体** 手动触发）；
- 回收站中可 **♻️ 一键还原全部**、单条 **♻ 恢复**（重新注册三个用电实体，历史接续）、**彻底删除**（物理删配置行，日表仍保留）、**清空回收站**；
- **接口**：`GET /api/ha_data_store/power_energy`（`type=configs` 生效中 / `type=archived` 回收站 / `type=lookup&entity_id=` 查单个含归档 / `type=query&kind=daily|monthly|yearly|range|latest`，支持 entity_id/room/date/month/year/start/end），`POST`（`action=create|delete|restore|purge|purge_all`）；API 工具含「⚡ 用电计量」查询分组；
- **多实体 × 多维度聚合**：`GET /api/ha_data_store/query?type=power_energy_multi`，参数
  `entities`（逗号分隔，空=全部）、`bucket=day|month|year`、`view=entity|date`、
  `start`/`end`/`date`/`month`/`year`（优先级 start/end > date > month > year，全不传 = 全部时间）；
  API 工具中对应「**时间模式**」下拉：全部时间 / 时间段 / 指定日 / 指定月 / 指定年
  （`device_usage_multi` 同样支持）。
  `view=entity` 返回 `entities[].series[]`（按实体），`view=date` 返回 `dates[].devices[]`（按时间）；
  两者均含 `totals.kwh` / `day_count`。API 工具「⚡ 用电计量」分组有对应可视化配置项；
- 数据浏览器中 `power_energy_daily` 为用户表（默认可见）。

### 14. 设备清理

集成运行过程中可能残留"没有实体"的空设备（例如删除实体后遗留）。db_viewer「系统配置 → 🧹 设备清理」可一键扫描并清理：

- 仅清理 **identifiers 以本集成 domain 开头**、**非 entry 主设备**、且按 entity_registry 统计**无任何实体**的空设备，安全不误删；
- `GET /api/ha_data_store/devices/cleanup` 扫描预览，`POST`（`{confirm:true}`）真删（删除前自动解除 config entry 关联，以最终是否仍在 registry 判定成功）。

---

### 15. 可视化查询构造器 与 数据库新建表

「API 工具」内新增 **🧩 查询构造器**，无需手写 SQL 即可把任意数据表发布为一个受鉴权的查询接口；「数据库浏览」工具栏同时支持可视化 **➕ 新建表**。

**构造器流程（5 步）：**

1. **① 选数据表与返回字段**：表目录按数据类别分组（环境/属性/设备历史/其它自定义）并显示行数，返回字段默认全部、可勾选精简；
2. **② 动态参数（过滤条件）**：每行 = 字段 + 比较方式 + 值来源。按字段类型提供合适操作符：
   - 文本/ID：`=` / `≠`；数值字段：`>` `≥` `<` `≤`
   - 文本还支持 **包含 LIKE / 不包含 / 多值 IN**（调用时逗号分隔，如 `rooms=客厅,卧室`）与「为空」
   - 时间/日期字段提供 **「时间段(起,止)」单行 between**（自动生成 `start_time`/`end_time` 两个参数）；可先用顶部「时间字段」下拉指定列，再点「＋ 时间段」
   - 值选“前端传参”即成动态参数（填参数名/默认值/必填/说明）；或选“固定值”写死
3. **③ 排序**：字段 + 升/降序；
4. **④ 汇总信息**：可选返回总条数 `count` 与数值列合计/平均/最大/最小；
5. **⑤ 试运行并发布**：填试运行参数值 → 执行结果表 + SQL 预览 → 保存即发布。

**发布后调用：**

```http
GET /api/ha_data_store/custom/{route_path}?key=你的Key&entity_id=sensor.a&start_time=2026-09-01 00:00:00&end_time=2026-09-04 23:59:59
```

- 响应默认：`{"success":true, "data":[...], "count":匹配总数, "summary":{合计/平均...}}`；
- 可选参数未传且无默认值时**自动跳过该过滤**（前端可只传部分条件）；必填缺失返回 400 中文错误；加 `&_debug=1` 可查看实际生成的 SQL；
- 鉴权沿用「API 访问」总开关 + API Key；路由有独立 **启用/停用** 开关；
- 定义（`query_def`）落库 `custom_routes`，复制数据库即可迁移。

**新建表：** 填表名（自附加自增主键可选）与字段（名称/类型/主键/默认值），后端白名单校验后建表；新表即刻进入“用户表”下拉与查询构造器可选范围。

---

### 16. 接口管理（新接口 · 免重启）

「API 工具」内新增 **🧩 接口管理（新接口）** 子页，用于发布**新接口**，特点是**新增 / 修改 / 启停 / 删除接口均无需重启 HA**。

**原理**：HA 的 `register_view()` 仅在集成 setup 时注册路由且无公开注销 API，故以往新增接口必须重启。本模块用一个**固定通配路由** `/api/ha_data_store/ext/{接口名}`（setup 时注册一次、此后永不变），接口**定义存库**并在运行时读取 → 变更即时生效。

**流程（不重启）：**
1. 「API 工具 → 🧩 接口管理（新接口）」→ **➕ 新建接口**：填 `name`（URL 用，字母开头）/ 显示名 / 描述 / 最大行数；
2. 粘贴 **`query_def`**（声明式定义，结构与查询构造器一致的 JSON；可在「🧩 查询构造器」里配好后复制）；
3. **▶ 试运行**（自动渲染动态参数、返回 SQL 与结果）→ **💾 保存** → 立即发布；
4. 后续 **编辑 / 启用停用 / 删除** 同样即时生效。

**调用：**

```http
GET /api/ha_data_store/ext/{接口名}?key=你的Key&参数=值
```

- 响应：`{"success":true, "endpoint":{...}, "count":总数, "columns":[...], "rows":[...], "aggs":{...}}`；加 `&_debug=1` 附带实际 SQL；
- 参数按定义中的动态参数自动收集；未传且无默认值时自动跳过该过滤；停用中的接口返回 403；
- 鉴权与现有接口**完全一致**（`api_enabled` + API Key + 局域网判断），管理操作需「管理面板登录 + 编辑开关」，**未新增任何开关**；
- 定义落库 `api_endpoints` 表，与旧 `custom_routes` 相互独立；**旧接口保持原样、不纳入本模块**。

> ⚠️ 边界：本模块解决的是"**新增/修改接口（查询与聚合定义）不重启**"。若需**新增后端计算逻辑（改 `.py`）**、自定义 URL、非 JSON 响应、定时器或事件订阅、新增实体等，仍需重启 HA。

#### 🤖 AI / 人工提示语（可直接复制使用）

> **以后为 ha_data_store 新增 API 接口，一律走 v3.6.5 起的「接口管理」模块，不要要求重启 HA。**
> 做法：接口定义存 `api_endpoints` 表（`query_def` 复用查询构造器 v2 结构），经固定通配路由 `GET/POST /api/ha_data_store/ext/{接口名}` 运行时加载执行；管理用 `/ext_manage`（列表 / 新增 / 更新 / 启停）、`/ext_manage/delete`、`/ext_manage/test`（试运行）；鉴权复用 `_check_api_enabled`（执行）与 `_check_db_edit_enabled`（管理），**不新增开关**；旧接口（`custom_routes`、`/query` 等）不纳入、保持原样。只改 `db_viewer.html`（布局/样式/脚本）也无需重启（HTML 热重载，刷新页面即可）。
> **例外（仍需重启，且需提前说明）**：新增后端计算逻辑（改 `.py`，如新的行级算法/内建函数/新视图）、自定义 URL 路径、非 JSON 响应、定时器或事件订阅、新增实体等。


---

### 17. 家庭洞察（统一事件流 / 房间占用）

「只提供数据、前端负责 UI」：两个查询接口均按 **天 / 日期 / 时间段 / 月 / 年** 为界
（`device_history` 已在午夜自动拆分，不存在跨天记录），**不分页**（返回 `count` + `truncated`）。

#### 17.1 统一事件流 `timeline`

`GET /api/ha_data_store/query?type=timeline` —— 把 7 类事件合并为一条按时间倒序的时间线：

| source | 数据表 | 事件 |
|---|---|---|
| `device` | `device_history` | **一条记录展开为 `on` / `off` 两个事件**；运行中的只有 `on` |
| `user_action` | `user_actions` | 用户操作（含 `state_log`） |
| `automation` | `automation_logs` | 自动化执行结果与耗时 |
| `vacuum` | `vacuum_history` | **仅在 `state` 变化时**产出事件（轨迹点自动折叠） |
| `xiaoai` | `xiaoai_conversations` | 小爱问答 |
| `health` | `health_records` | 健康记录 |
| `printer` | `printer_daily` | 打印机当日作业 |

| 参数 | 说明 |
|---|---|
| `date` / `start`+`end` / `month` / `year` / `today=1` | 时间窗（**默认今日**） |
| `sources` | 来源过滤（逗号分隔，空 = 全部） |
| `events` | 仅 `device`：`on` / `off`（空 = 两者） |
| `entities` / `rooms` / `users` | 实体 / 房间 / 用户过滤 |
| `keyword` | 关键词（设备名 / 操作 / 自动化 / 扫地机 ID / 小爱文本 / 健康 / 打印机） |
| `limit`（默认 500，上限 5000）/ `detail=0` | 条数上限与明细开关 |

> **来源能力收敛**：`entities` / `rooms` / `users` 属"实体维度"过滤，**不具备该维度的来源会被自动剔除**
> （如 `entities=` 会剔除 automation / health / printer），被剔除项见返回的 `skipped_sources`。

条目结构：`{ts, ts_ms, source, event, title, entity_id, name, room, icon, user, detail, extra{}}`
（`ts_ms` 便于前端排序；`extra` 按来源给出 duration/energy/state_log/… 等明细）。

#### 17.2 房间占用排行 `room_occupancy`

`GET /api/ha_data_store/query?type=room_occupancy` —— 数据源 `device_history` 中 `name='人在'` 的记录：

- **严谨口径**：同一房间的重叠区间先做**区间并集**再计时（多个"人在"实体、抖动重复上报都不会重复计时），
  同时给出 `raw_duration_hour` 作为"未并集口径"对照；
- 每房间输出：`duration(_hour)` / `count` / `segments` / `avg_hour` / `occupied` / `last_seen` / `share`；
  顶层：`total_duration_hour` / `top_room` / `occupied_rooms` / `room_count`；
- 参数：时间窗（默认今日）、`rooms`、`include_empty=1`（无数据房间也返回 0）、
  `bucket=none|day|hour`（按日序列 / 24 小时分布）、`door=0`、`detail=0`；
- **门户事件** `door`：`open_count` / `open_duration_hour` / `last_open` / `open_now` / `events[]`
  —— 可直接与 timeline 组合出"回家 / 外出"时间线。

db_viewer：「API 工具 → **家庭洞察**」分组提供上面两个接口的可视化表单（时间模式为
指定日 / 时间段 / 指定月 / 指定年，缺省今日；含来源勾选、设备事件、关键词、房间、分解粒度、
含无数据房间、门户事件等控件）。

---

### 18. 通用指标引擎（元数据驱动 · `metrics_catalog`）

以往每加一种统计就要新增一个内建 `type=` 分支；现在把「怎么查」变成**一条数据（指标定义）**：
**新增 / 修改指标无需重启 HA**（定义存库，运行时读取）。

**两层元数据**
- **schema 元数据**（不落表）：每张表的中文名 / 分组 / 时间列 / 实体列 / 房间列 / 值列 / 时间粒度（`datetime`|`date`）；`env_*`、`attr_*` 动态生成 —— 同时是引擎的**列白名单**（安全边界）。
- **指标定义**（新表 `metrics_catalog`）：`metric_id / name / category / source_table / value_col / value_expr / agg / unit / icon / group_by / group_col / filters / enabled / builtin / sort_order / remark`。

**占位符**：`@time` `@entity` `@value` `@room` `@name` `@id` —— 编译时按源表解析为真实列名，同一份定义可跨表复用。

**接口**

| 接口 | 说明 |
|---|---|
| `GET /api/ha_data_store/query?type=metrics_catalog` | 指标目录（可按 `category` / `keyword` 过滤，默认只返回启用项） |
| `GET /api/ha_data_store/query?type=metrics_query&metric_id=xxx` | 执行指定指标 |

**`metrics_query` 参数**：`metric_id`（必填）、`start/end`、`date`、`month`、`year`、`days`（最近 N 天）、
`entities`、`room`、`group_by`（none/entity/room/name/day/hour/month/year）、
`agg`（avg/sum/max/min/count/count_distinct）、`order`、`filters`（JSON 字符串）、`limit/offset`、`detail`、`sql`。

**返回**：
```json
{"metric_id":"env_temperature_avg","name":"温度均值","unit":"°C","range":"最近 7 天",
 "group_by":"day","agg":"avg","row_count":842,"count":7,
 "summary":{"rows":7,"samples":842,"total_count":842,"value":162.4,"avg":23.2,"min":17,"max":26.4},
 "rows":[{"key":"2026-09-18","value":23.2,"count":120}],
 "series":{"labels":["2026-09-18"],"values":[23.2],"counts":[120]}}
```

**示例**：`.../query?type=metrics_query&metric_id=env_temperature_avg&days=7&group_by=day`

**管理**：db_viewer「系统配置 → 📊 指标管理」可新建 / 编辑 / 复制 / 启停 / 删除指标、同步内置指标，
并**试运行**（时间模式 + 实体 + 分组/聚合覆盖，展示生成的 SQL、`summary` 与结果表）；
「API 工具」新增「📊 通用指标（元数据驱动）」分组（自动生成带 `metric_id` 的 URL）。

**内置指标**（启动时 seed，不覆盖用户改动）：环境（温度/湿度/PM2.5/CO₂/功率 × 均值·最高·最低）、
设备（开启次数·时长合计·单次均值·用电合计·运行中设备数）、用电、健康、操作（活跃用户数）、
自动化、卡片上报，以及每个 `attr_*` 类型的记录数与 REAL 列均值（每类型最多 5 列）。

---

### 19. 实体→网络（读数据 / 控制）

`API工具 → 🔗 API 地址生成器` 的「查询类型」下拉里有独立分组 **🌐 实体→网络（token 鉴权）**：

- 动态列出所有已配置目标（每个目标 1~2 项：📥 读数据 / 🎛 控制），选中即生成带真实 token 的地址
- 地址**不附加 `?key=`** —— token 本身就是密钥、URL 自鉴权
- 选中控制项时会展示多行 `curl -X POST` 示例、该 token 当前的授权动作清单，
  以及 `push_capabilities/{token}` 能力清单地址；未开启控制时提示会 404

---

### 20. 整库备份

顶部选项卡 `💾 数据备份` 提供整库备份能力：

- **备份方式**：优先 `VACUUM INTO`，回退 SQLite 在线备份 API，运行期取一致快照、不需停机
- **自动计划**：关闭 / 每小时 / 每天 / 每周，可配置执行时刻与保留份数（默认保留 10 份自动备份）
- **恢复**：不在运行期覆盖库 —— 校验后排队，**重启 HA 时在数据库被打开之前原子替换**，
  替换前自动留一份「恢复前快照」；校验失败则取消恢复并保留现有库
- **设置存放**：`api_settings` 表的 `backup_*` 键（不新增表，库损坏时模块仍可用）
- 备份目录默认 `storage/ha_data_store_backups`，可改为 **NAS 挂载后的绝对路径**

#### 20.1 备份目录与网络共享（SMB / NFS）

**不能直接填 `smb://host/share` 这类协议地址** —— 程序只能读写文件系统路径。
填了会在 HA 配置目录里静默造出一个同名垃圾目录（备份显示"成功"但没到 NAS），
因此保存时会直接报错并给出挂载指引。正确做法是先挂载，再填挂载后的路径：

| 环境 | 挂载方式 | 填写的路径示例 |
|---|---|---|
| HA OS / Supervised | 设置 → 系统 → 存储 → 添加网络存储（用途选 media / share） | `/media/ha_backups`、`/share/backups` |
| HA Container / Core | 宿主机 `mount -t cifs //192.168.1.102/media /mnt/ha_backups -o username=用户,password=密码,uid=1000` | `/mnt/ha_backups` |

面向共享的写入做了额外加固：备份**先在本地生成并校验，再搬到共享** ——
避免 SQLite 直接在网络盘上做 journal / 依赖文件锁（官方不建议），
也避免网络中断在共享上留下半个损坏文件。挂载点需确保在 HA 启动时就已就绪。

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
| `entity_daily_by_year` | 实体按日用电（指定年，含 totals 合计与运行中 running 标记） | entity_id, year |
| `entity_daily_all` | 实体按日用电（全部历史，含 totals 合计与运行中 running 标记） | entity_id |
| `entities_daily_flat` | 全部实体按月按日（平铺：日×设备扁平行 + totals） | month |
| `entities_daily_by_day` | 全部实体按月按日（按日分组：days[].devices + totals） | month |
| `entity_hour_dist` | 实体时段分布（几点使用/分时用电，运行中设备处理） | entity_id + 可选范围 |
| `entity_hour_dates` | 设备小时开启日期（某小时开过哪些天） | entity_id, hour + 可选范围 |
| `entities_period_agg` | 多实体按日/月/年汇聚（每实体 series + totals；view=entity\|date） | entities(可选), bucket |
| `entities_dates` | 多实体有数据日期（`group=all`(0) 合并 / `entity`(1) 按实体 / **`count`** 按日期数量 / **`both`** 日期数量+实体 / **`simple`** 极简 `list:[{date,count}]` 日期倒序） | entities(可选), start/end/date/month/year |
| `entities_hours_agg` | 多实体时段分布（group=0 合并 / 1 按实体） | entities(可选) |
| `entities_weekday_hours` | 多实体时段分布网格（dim=week\|month\|day × 小时，7/12/31×24） | entities(可选), dim |
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
| `whole_house_usage` | 全屋用电/用时（总计→房间→设备 三级，含运行中设备统计与设备数量） | year(必填)；month/date 可选精确 |
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
| `user_actions_daily` | 用户动作·指定日期操作记录 | date |
| `user_actions_range` | 用户动作·日期段操作记录 | start/end |
| `user_actions_month_dates` | 用户动作·指定月哪些日期有数据 | month |
| `user_actions_hour_dist` | 用户动作·数据点按小时分布 | entity_id(可选) |
| `user_actions_entity_summary` | 用户动作·实体操作次数排行 | entity_id(可选) |
| `user_actions_user_summary` | 用户动作·按用户汇总 | 无 |
| `user_actions_entity_last_today` | 用户动作·实体当日最后一条记录 | entity_id |

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

# 用户动作：指定日期操作记录
curl "http://ha:8123/api/ha_data_store/query?type=user_actions_daily&date=2026-08-24&key=your_api_key"

# 用户动作：数据点按小时分布（指定实体）
curl "http://ha:8123/api/ha_data_store/query?type=user_actions_hour_dist&entity_id=light.living_room&key=your_api_key"

# 用户动作：实体当日最后一条记录
curl "http://ha:8123/api/ha_data_store/query?type=user_actions_entity_last_today&entity_id=light.living_room&key=your_api_key"

# 全屋用电/用时：指定年（可叠加 month/date 精确到月/日）
curl "http://ha:8123/api/ha_data_store/query?type=whole_house_usage&year=2026&month=2026-09&date=2026-09-07&key=your_api_key"
```

**`whole_house_usage`（全屋用电/用时）返回结构：**

- 三级：`total` → `rooms[]`（每个房间含 `devices[]`）→ 设备项；`year/month/date` 按最近一级精确匹配（`date > month > year`）
- 每级字段：`count`(开启次数) / `duration_hour`(小时,2位) / `energy_kwh`(kWh,4位) / `running_count`
- 顶层：`room_count`(房间数) + `room_names`(单纯房间名列表，如 `["客厅","餐厅",...]`)；`total.device_count`(设备总数)
- 每个房间：`device_count`(该房间设备数)
- 运行中设备（`on_time` 非空、`off_time` 空）纳入统计并带 `running` 标记：时长=当前时间−`on_time`；用电=有电表取 `now_kwh−on_power`，无电表但有固定功率按 `power_rating(W)/1000×时长` 折算
- 汇总传感器 `sensor.ha_data_store_all_room_usage` 直接输出 本年/本月/今日 三级（状态值=今日用电 kWh，每 1 分钟刷新）

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
| `/api/ha_data_store/push_targets` | GET/POST/DELETE | 实体→网络 目标管理（读 + 控制配置） |
| `/api/ha_data_store/push_data/{push_token}` | GET | 外部读取实体数据（只读，token 即密钥） |
| `/api/ha_data_store/push_control/{control_token}` | POST | 外部控制实体（动作模式 / raw 模式） |
| `/api/ha_data_store/push_capabilities/{control_token}` | GET | 该控制 token 的可用动作与参数 schema |
| `/api/ha_data_store/push_entity_capabilities?entity_id=` | GET | 指定实体的可控能力（配置向导用） |
| `/api/ha_data_store/push_control_logs` | GET | 控制审计日志（limit/entity_id/success/clear） |
| `/api/ha_data_store/backup` | GET/POST/DELETE | 整库备份：状态与列表 / 立即备份·保存设置·排队恢复·取消·完整性检查·清理 / 删除 |
| `/api/ha_data_store/backup/download?name=` | GET | 下载指定备份文件 |
| `/api/ha_data_store/attr_config` | GET/POST | 属性类型配置管理 |
| `/api/ha_data_store/attr_manual_trigger` | POST | 手动触发属性采集 |
| `/api/ha_data_store/vacuum_type_defs` | GET/POST | 扫地机器人类型管理 |
| `/api/ha_data_store/vacuum_configs` | GET/POST/DELETE | 扫地机器人配置管理 |
| `/api/ha_data_store/bridge_connections` | GET/POST | 桥接连接配置管理 |
| `/api/ha_data_store/bridge_entities` | GET/POST/DELETE | 桥接实体配置管理 |
| `/api/ha_data_store/bridge_reload` | POST | 重新加载所有桥接连接 |
| `/api/ha_data_store/virtual_device` | GET/POST/DELETE | 虚拟设备管理 |
| `/api/ha_data_store/virtual_device/export` | GET | 导出全部虚拟设备（配置 + 当前状态，跨机迁移用） |
| `/api/ha_data_store/virtual_device/import` | POST | 导入虚拟设备并重建（body `{mode: skip\|overwrite, devices:[...]}`） |
| `/api/ha_data_store/helper` | GET/POST/DELETE | 辅助元素列表/新建/删除 |
| `/api/ha_data_store/helper/scan` | GET | 扫描原生 HA helper（可 `?include_binary_sensor=1`）生成可导出 item |
| `/api/ha_data_store/helper/export` | GET | 导出已导入的辅助元素（配置 + 状态） |
| `/api/ha_data_store/helper/import` | POST | 导入辅助元素并转为本集成实体（body `{mode: skip\|overwrite, items:[...]}`） |
| `/api/ha_data_store/power_energy` | GET | 用电量查询：`type=configs`（生效中）/ `type=archived`（回收站）/ `type=lookup&entity_id=`（单个含归档）/ `type=query&kind=daily\|monthly\|yearly\|range\|latest`（支持 entity_id/room/date/month/year/start/end 过滤） |
| `/api/ha_data_store/power_energy` | POST | 登记管理（body `{action: create\|delete\|restore\|restore_all\|purge\|purge_all\|backfill_deid, entity_id, ...}`；delete 为软删除归档，日表数据保留；`daily_entity_id` 不接受传入，由 `id_slug` 派生） |
| `/api/ha_data_store/query?type=power_energy_multi` | GET | **用电量多实体×多维度聚合**：`entities`（逗号分隔，空=全部）、`bucket=day\|month\|year`、`view=entity\|date`、`start/end/date/month/year`、`devices=0` 可省明细。`view=entity` → `entities[].series[]`；`view=date` → `dates[].devices[]`；均含 `totals.kwh`/`day_count` |
| `/api/ha_data_store/query?type=device_usage_multi` | GET | **设备用时/用电多实体×多维度聚合**（`device_history`）：参数同上。指标为 `count` 开启次数 / `duration_hour` 时长 / `energy_kwh` 用电量（无来源为 `null`）/ `running`。运行中记录按「当前时间 − on_time」计时。额外带 `room` |
| `/api/ha_data_store/query?type=device_usage_detail` | GET | **多实体明细（不聚合）**：`entities`、时间参数（`start`/`end` 时间段、`date` 指定日、`month` 指定月、`year` 指定年，优先级 start/end > date > month > year，全不传 = 全部）、`limit`/`offset`、`full=1`、`summary=0`。按 `on_time` 倒序，返回 `total`/`returned`/`records[]` 与 **`summary` 合计节点**（全局 `totals` + 每实体合计，含 `running_count`；**基于分页前全量记录计算，不受 `limit`/`offset` 影响**）。**API 工具中同样使用「时间模式」下拉**。**`full=1` 返回 `device_history` 全部字段**（`id`/`on_power`/`off_power`/`energy_consumed`/`duration`/`cross_day`/`state_attr`(JSON)/`now_kwh`/`on_user`/`off_user`/`on_snapshot`/`off_snapshot`/`power_entity`/`power_rating` 等）；不传 `full` 为精简字段 |
| `/api/ha_data_store/query?type=device_usage_total` | GET | **多实体合计（年/月/日/全部）**：`entities`、`scope=all\|year\|month\|date`（配 `year`/`month`/`date`，`month` 支持 `9`/`09`/`2026-09`）。返回每实体合计 + 全局 `totals` |
| `/api/ha_data_store/query?type=device_usage_history` | GET | **多实体历史同期**：`entities`、`scope=today`（历年同月同日）/ `scope=month`（历年同月），**排除今年**。返回 `history`（含 `by_year` 逐年）+ `current`（今年同期） |
| `/api/ha_data_store/query?type=device_usage_avg` | GET | **多实体平均指标**：`entities`、`start`/`end`/`date`/`month`/`year`（默认全部）。返回 `avg_daily_count` 平均每日次数 / `avg_daily_duration_hour` 平均每日时长 / `avg_per_count_hour` 平均每次时长（全局按加权计算） |
| `/api/ha_data_store/devices/cleanup` | GET/POST | 扫描空设备 / 清理空设备（POST body `{confirm:true}`） |
| `/api/ha_data_store/health_add` | POST | 添加健康记录（body 可选 `remark` 备注 / `description` 说明） |
| `/api/ha_data_store/health_types` | GET/POST/DELETE | 健康数据类型管理 |
| `/api/ha_data_store/batch_entity_state` | POST | 批量写入实体状态 |
| `/api/ha_data_store/db_maintain` | POST | 数据库维护（VACUUM/REINDEX） |
| `/api/ha_data_store/entity_monitor` | GET | 实体在线监控 |
| `/api/ha_data_store/automations` | GET/POST | 自动化配置列表/新增 |
| `/api/ha_data_store/automations/{id}` | PUT/DELETE | 修改/删除自动化（修改后自动重算下次运行时间） |
| `/api/ha_data_store/automations/{id}/run?force=1` | POST | 手动运行（force=1 跳过条件） |
| `/api/ha_data_store/automation_logs` | GET/DELETE | 执行记录分页查询 / 清理（?days=N 或 ?automation_id=） |
| `/api/ha_data_store/automation_lookup` | GET | 按名称查询自动化详细信息（?name= 精确 / &fuzzy=1 模糊，含配置+统计） |

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

> 💡 **开发提示**：页面 HTML 带热重载——只修改 `db_viewer.html`（布局/CSS/JS）后**刷新浏览器页面即可生效，无需重启 HA**（建议 Ctrl+F5 强刷避开浏览器缓存）；修改任何 `.py` 文件仍需重启 HA。页面标题栏右侧会显示当前集成版本号。

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
| `push_targets` | 实体→网络 目标配置（读 token + 控制 token，同一实体可多套配置） |
| `control_logs` | 实体→网络 控制审计日志（保留 30 天） |

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
| `health_records` | 健康记录（血压/体温/体重等，含 `remark` 备注 / `description` 说明） |
| `virtual_devices` | 虚拟设备持久化 |
| `printer_configs` | 打印机配置（支持多台，name 唯一） |
| `printer_daily` | 打印机每日记录（汇总 + 墨量 + 当日明细 JSON） |
| `user_actions` | 用户操作记录（前端埋点上报，含 action_snapshot/state_log/ts_text/config_id/device_type 等；ts 采用实体状态时间，与 device_history 精确关联） |
| `automations` | 自动化配置（触发/条件/动作，30 秒调度执行） |
| `automation_logs` | 自动化执行记录（时间、条件明细、动作结果、耗时、状态，保留 30 天） |
| `helper_entities` | 辅助元素持久化（原生 helper 导入为本集成自管实体，含 source_type/source_entity_id/extra_config） |
| `power_meter_configs` | 功率→用电计量登记表（功率实体/设备名/房间/id_slug/日用电量实体/单位/启用；`enabled=0` 表示已取消登记并归档到回收站，行保留以便恢复与沿用） |
| `power_energy_daily` | 用电计量日表（每功率实体每天一条 kwh，月/年由日表实时聚合） |

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

### v4.0.0 实体→网络「可控制」+ 整库备份 + API 工具整合（2026-09-26）

> 当日全部改动的汇总版（原 3.7.0 / 3.8.0 / 3.8.1 / 3.8.2 / 3.8.3 合并为 4.0.0）。

**一、实体→网络从「只读映射」升级为「可控映射」**。读 / 写凭证彻底分离：`push_token` 只读、
`control_token` 可写且**只接受 POST**（GET 会被浏览器预取、被代理与日志记录、被爬虫扫到，
等于把「开锁」变成可被随机触发的动作）。新增总闸开关「实体网络控制」（设备「HA数据统一存储系统」下，
首次安装默认关闭，状态跨重启保留），关闭时所有控制请求 403。新增 `push_control.py`：**29 个域 / 97 个动作**
的白名单目录（`lock`/`alarm_control_panel`/`siren` 标记高风险），参数 schema 从实体属性**动态展开**
（`climate` 取 `min_temp/max_temp/target_temp_step`、`hvac_modes` 等），`cover` 按 `supported_features`
位掩码过滤动作；参数支持**两层控制** —— 调用方传参 + 配置端锁定（`lock=value` 固定值忽略传参、
`lock=range` 限幅自动裁剪），于是外部系统拿到的是「受限能力」而非「实体控制权」；授权清单支持
`["*"]`（全部）/ `[]`（不允许任何动作）/ 具体列表；raw 逃生口默认关闭且**只允许调用实体自身域**
（禁止 `shell_command`/`hassio`/`homeassistant` 等）。另有限流（429）、`wait_state` 状态等待、
`control_logs` 审计（保留 30 天）、`push_capabilities/{token}` 能力发现。数据库：`push_targets`
新增 6 个控制列，并**放开 `entity_id` 唯一约束**（SQLite 不支持 DROP CONSTRAINT，改用建新表拷贝重建 +
`push_token`/`control_token` 部分唯一索引），使同一实体可挂多套配置（只读 / 只许关 / 全控各一套 token）。

**二、子选项卡内嵌「📖 使用方法」**（默认折叠，收起时保留一行摘要）：快速上手 4 步 / 两类地址与放行条件 /
6 段 curl 示例 / 动作模式与 raw 模式 / 参数两层控制 / 动作清单语义 / 返回字段与错误码表 /
安全建议，并附「📋 加载动作目录」从后端实时拉取域与动作一览。

**三、整库备份（`backup.py` 新增，顶层选项卡「💾 数据备份」）**。备份优先 `VACUUM INTO`、回退 SQLite
官方在线备份 API，运行期取一致快照、不需停机（不会丢 `-wal` 未合并事务）。自动计划支持
关闭/每小时/每天/每周 + 执行时刻 + 保留份数（默认 10 份，**只作用于自动备份**，手动与恢复前快照永不自动删除）；
调度由 10 分钟 tick 驱动，运行期改计划无需重注册，HA 中途重启会**自动补上当天那次**。
恢复采用**排队 + 启动时原子应用**：校验备份（`quick_check` + 必需表）后排队，重启 HA 时在任何连接
打开数据库**之前**先留「恢复前快照」、清理旧 `-wal`/`-shm`、再原子替换；校验失败则取消恢复并保留现有库。
面向 **SMB/NFS 共享**做了两处加固：① 协议地址（`smb://`、`smb:\\`、`nfs://` …）会被**显式拒绝**
并给出挂载指引 —— 直接填会在配置目录里静默造出同名垃圾目录（备份显示"成功"但没到 NAS）；
② 备份**先在本地生成并校验、再搬到共享**，避免 SQLite 在网络盘上做 journal / 依赖文件锁，
也避免断网在共享上留下半个损坏文件。另提供完整性检查、流式下载、严格文件名校验。

**四、API 工具新增 🌐 实体→网络 查询类型分组**：动态列出所有已配置目标（`📥 读数据` / `🎛 控制`），
选中即生成带真实 token 的地址（**刻意不附加 `?key=`**，token 即密钥、URL 自鉴权），
并展示多行 `curl -X POST` 示例、当前授权动作清单与能力清单地址。

**五、修复**：「🌐 实体→网络」分组为空（填充函数只挂在默认激活的子选项卡回调上，从未被调用）；
备份同秒重复触发产生 `_2` 后缀导致文件**存在但列表不可见**；启动早期 `load_settings_sync()` 会
**凭空创建空库**；协议地址正则漏掉 `smb:\\` 写法。

> 版本号 → `4.0.0`。需重启 HA 生效（控制与备份均涉及后端新增逻辑）。

---

更早版本的完整更新记录见 [`docs/CHANGELOG.md`](docs/CHANGELOG.md)。

