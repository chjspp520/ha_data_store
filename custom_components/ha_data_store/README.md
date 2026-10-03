# HA 数据统一存储系统 (ha_data_store)

ha_data_store 是一款 Home Assistant 自定义集成：**无需修改 `configuration.yaml`**，全部通过配置界面与内置管理页面完成，把「采集 → 存储 → 分析 → 展示 → 远程交互」串成一套家庭数据平台。

| 环节 | 能力 |
|---|---|
| **采集** | 设备开关记录（含跨午夜拆分、启动恢复、用电量核算）、传感器环境数据、实体属性提取（含**通讯数据采集**）、扫地机轨迹、健康记录、打印机用量、小爱对话 |
| **存储** | 统一落库 SQLite（WAL 模式）；支持整库备份与「排队 + 重启后原子应用」式恢复 |
| **导入导出** | 数据浏览页把 CSV / JSON 导入任意表（字段映射 + 固定值、追加 / Upsert、自动建表与补列），并把表数据或空模板导出为 CSV |
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
  - [21. 通讯数据（采集 + 查询）](#21-通讯数据采集--查询)
  - [22. 数据导入 / 导出（CSV · JSON）](#22-数据导入--导出csv--json)
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
| 📞 **通讯数据** | 属性提取新增 `comm` 采集模式：按数组展开写入 **18 个固定字段**（我方/对方号码、归属地、姓名、通讯时间、地点、消息类型、数据来源、呼叫类型、时长、金额、流量、流量类型、内容、图片路径、我的坐标、对方运营商、对方坐标），列名与类型由系统固定，目标列可选源字段或**直接填固定值**；`2026/9/1 17:24` 自动规范化为 `2026-09-01 17:24:00`，`3分53秒` 等中文时长自动换算为秒，`1.5GB` / `512MB` 等流量自动换算为 MB；`comm.py` 提供明细/日期/排行/趋势/汇总/联系人/地点/时段 8 类查询，API 工具含专属分组 |
| 📥 **数据导入 / 导出** | 数据浏览页把 CSV / JSON 导入任意表：字段映射 + 固定值、**追加 / Upsert**（多列唯一键）、自动建表与补列、🧪 试运行、分批进度与错误明细；基础值转换（`¥1,234.5`→`1234.5`、`2026/9/1`→`2026-09-01`）；导出表数据或空模板为 CSV（UTF-8 BOM，Excel 友好） |

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

### 21. 通讯数据（采集 + 查询）

把手机端同步过来的通讯记录（通话 / 短信 / 微信 / QQ 等）落库并查询。

#### 21.1 采集：属性提取新增「通讯数据采集」模式

「系统配置 → 📊 属性提取」的第四种模式 **通讯数据采集**（`mode=comm`）：
选实体 → 选数组路径 → 选唯一键字段（通常是 `id`）→ 把源字段对应到 16 个固定列。

**固定字段表 `attr_comm_records`：**

| 列 | 类型 | 说明 |
|---|---|---|
| `my_number` | TEXT | 我方号码（微信 / QQ 场景可留空） |
| `party_number` | TEXT | 对方号码 |
| `party_place` | TEXT | 对方号码归属地 |
| `party_name` | TEXT | 对方姓名 |
| `time` | TEXT | 通讯时间 `YYYY-MM-DD HH:MM:SS` |
| `location` | TEXT | 通讯地点 |
| `msg_type` | TEXT | 消息类型 |
| `channel` | TEXT | 数据来源（call / sms / wechat / qq …） |
| `call_type` | TEXT | 呼叫类型 |
| `duration` | INTEGER | 时长（秒） |
| `cost` | REAL | 金额（元） |
| `content` | TEXT | 消息内容 |
| `image_path` | TEXT | 图片路径（图片消息的文件路径 / URL） |
| `location_coordinate` | TEXT | 我的坐标 |
| `party_isp` | TEXT | 对方运营商 |
| `party_coordinate` | TEXT | 对方坐标 |

**列名与类型由系统固定**（不受前端勾选影响），因此 `duration` 恒为整数、`cost` 恒为数值，
聚合查询不会因列类型被勾错而失效；建表时自动创建 `time`、`(party_number,time)`、
`(party_name,time)`、`(location,time)` 四个索引。

**配置要点：**

- 每个目标列既可以**选数组里的字段名**，也可以**直接填固定值**
  （如 `channel` 填 `wechat`、`my_number` 填自己的号码）；输入框边框绿=源列 / 橙=固定值，
  也可加 `=` 前缀强制指定固定值
- `time` 为唯一必填映射项；未填写的目标列保持为空
- 值转换：`2026-09-01T17:24:33+08:00` / `2026/9/1 17:24` / 时间戳 → `YYYY-MM-DD HH:MM:SS`；
  时长支持 `27秒`、`3分53秒`、`1小时2分3秒`、`3分53`、`1小时30`、`3:53`（分:秒）、
  `1:02:03`（时:分:秒）、`233`、`27s`、`3m53s` 等写法，自动换算为**秒**
- **「对比最近条数」是去重窗口**：每次采集回查最近 N 条历史记录并按唯一键比对，
  命中则更新、未命中才插入（通讯模式默认 1000）。采集时还会自动放大到至少 2 倍本次条数，
  所以即使设小也不会重复写入本次数据

#### 21.2 查询 API（`comm.py`）

```
GET|POST /api/ha_data_store/comm?type=xxx&key=你的APIKey
```

| type | 说明 | 关键参数 |
|---|---|---|
| `records` | 明细列表 | `sort` / `order` / `limit` / `offset` / `content_len` |
| `dates` | 哪些日期有数据（每天条数 / 时长 / 金额 / 联系人数） | 任意过滤 |
| `stats` | ★ **统计分析**：按时间粒度分组汇总，每桶含条数 / 时长 / 金额 / 去重联系人数 / 活跃天数 / 平均时长，并附 `total`（合计）与可选 `avg_per_bucket`（每桶均值） | `granularity=year\|quarter\|month\|week\|day\|hour\|weekday`、`sort=time\|value`、`order`、`by`、`limit`、`fill`、`with_total`、`with_avg` |
| `crosstab` | ★ **交叉汇总**：行维度 × 列维度 的度量矩阵（如「按消息类型汇总各联系人的条数与时长」） | `rows`（1~3 个维度，逗号分隔）、`cols`（0~1 个，留空只输出行合计）、`metric=count\|duration\|cost`、`limit` / `col_limit` |
| `onthisday` | ★ **历史上的今日**：按「月日相同」筛选历年同一天的数据，再按 `mode` 输出（详见下节） | `mode=stats\|ranking\|parties\|records\|crosstab`、`date`（基准日）、`years` / `min_year` / `max_year` / `exclude_current`、`at` / `window` / `hour` |
| `compare` | ★ **周期对比**：当前周期 vs 上一周期（环比）vs 去年同期（同比），直接返回差值与增长率 | `period=day\|week\|month\|quarter\|year`、`date`（基准日）、`compare=prev\|yoy\|both` |
| `contact` | ★ **联系人档案**：某号码 / 姓名的完整画像（总量 / 首末通讯 / 多久没联系 / 平均间隔 / 各维度分布 / 最近明细） | `party_number` 或 `party_name`（必填其一）、`recent` / `top_days` / `months` |
| `longest` | **单次 Top N**：最长通话 / 最高金额（按单条记录排序，区别于按人聚合的排行榜） | `by=duration\|cost`、`order`、`limit` / `offset` |
| `quality` | **数据质量检查**：时间为空 / 格式异常 / 时长与金额异常 / 内容为空 / 图片缺路径 + 疑似重复 + 时间覆盖 | `dup_limit` |
| `meta` | **数据概览**：表结构、总量、时间跨度、去重联系人数、维度取值清单（供前端下拉与健康检查） | 任意过滤 |
| `ranking` | 排行榜：`granularity` 指定粒度 + `period` 指定年/月/日后按维度排名；不填 `period` 即全部数据排名 | `dimension`、`by=count\|duration\|cost` |
| `trend` | 趋势序列（按时间桶统计，`fill=1` 补零） | `granularity=day\|month\|year\|hour\|weekday` |
| `summary` | 汇总统计（总量 / 首末时间 / 总时长 / 总金额 / 联系人数 / 活跃天数 + 各维度分布） | 任意过滤 |
| `parties` | 联系人清单（按对方号码聚合，含首末通讯时间） | `by`、`limit` / `offset` |
| `places` | 通讯地点清单 | `limit` / `offset` |
| `heatmap` | 星期 × 小时分布 | 任意过滤 |

**通用过滤参数**（所有 type 均支持，多值参数逗号分隔）：

| 参数 | 说明 |
|---|---|
| `date` / `month` / `year` | 单日 / 单月 / 单年 |
| `start` / `end` | 自定义区间（含边界，`YYYY-MM-DD` 或 `YYYY-MM-DD HH:MM:SS`） |
| `party_numbers` / `my_numbers` / `numbers` | 对方号码 / 我方号码 / 任一方号码（多值） |
| `party_names` / `places` / `party_places` | 姓名 / 通讯地点 / 归属地（多值，模糊匹配） |
| `channels` / `msg_types` / `call_types` | 数据来源 / 消息类型 / 呼叫类型（多值） |
| `keyword` | 消息内容模糊匹配 |
| `min_duration` / `max_duration` / `min_cost` / `max_cost` | 时长（秒）与金额（元）区间 |
| `fields` | **只返回指定列**（逗号分隔，如 `time,party_number,duration,content`）；留空 = 全部列。对 `records` / `onthisday&mode=detail` / `longest` / `contact` 的明细生效 |
| `type_name` | 数据表对应的属性类型名。**一般无需填写**——留空时自动解析（顺序见 [21.7](#217-历史今日--ha-实体)）；仅在需要明确指向某张表时才显式传参 |

**示例：**

```
# 某天某人
/api/ha_data_store/comm?type=records&date=2026-09-01&party_names=张三&key=xxx

# 指定号码在 9 月哪些日期有数据
/api/ha_data_store/comm?type=dates&month=2026-09&party_numbers=13800000000&key=xxx

# 2026-09 按姓名排行（按通话时长）
/api/ha_data_store/comm?type=ranking&granularity=month&period=2026-09&dimension=party_name&by=duration&limit=20&key=xxx

# 9 月每日趋势（缺失日期补 0）
/api/ha_data_store/comm?type=trend&granularity=day&month=2026-09&fill=1&key=xxx

# 全部数据按年汇总 / 按年月汇总
/api/ha_data_store/comm?type=stats&granularity=year&key=xxx
/api/ha_data_store/comm?type=stats&granularity=month&key=xxx

# 指定年 → 按月汇总（缺失月份补 0）
/api/ha_data_store/comm?type=stats&granularity=month&year=2026&fill=1&key=xxx

# 指定年月 → 按日汇总（并返回每桶均值）
/api/ha_data_store/comm?type=stats&granularity=day&month=2026-09&with_avg=1&key=xxx

# 指定号码 / 姓名（支持多值）按年 / 按年月 / 按年月日汇总
/api/ha_data_store/comm?type=stats&granularity=year&party_numbers=13800000000,13900000000&key=xxx
/api/ha_data_store/comm?type=stats&granularity=month&party_names=张三,李四&key=xxx
/api/ha_data_store/comm?type=stats&granularity=day&month=2026-09&party_names=张三&key=xxx

# 通讯时长最多的前 5 个月（sort=value 默认降序）
/api/ha_data_store/comm?type=stats&granularity=month&sort=value&by=duration&limit=5&key=xxx
```

**`stats` 与 `trend` 的区别**：`trend` 只给「时间桶 + 条数/时长/金额」，用于画折线；
`stats` 额外给「去重联系人数 / 活跃天数 / 平均时长」，并在响应里返回合计行与每桶均值，
`sort=value` 时可按指标（`by=count|duration|cost`）取 Top N 时间桶。
`granularity` 支持别名与中文（`年` / `月` / `日` / `季度` / `周` / `yyyy` / `ym` / `ymd`），
也支持 `period` 参数（如 `period=2026-Q3`、`period=2026-W35`）。

#### 21.3 历史上的今日（`onthisday`）

按「月日相同」筛选**历年同一天**的记录，再按 `mode` 输出五种视角：

| mode | 用途 |
|---|---|
| `stats` | 历年今日汇总：`granularity=year`（每年今日）/ `month`（每年该月）/ `day`（每年该日），默认 `year` |
| `detail` | ★ **详细明细**：返回跨年份的**逐条记录**（扁平列表），并附带 `total`（匹配总数）、`year_summaries`（各年汇总）与 `summary`（总体汇总） |
| `ranking` | 历年今日排行榜：`dimension` + `by` |
| `parties` | 历年今日联系人汇总：对方号码 / 姓名 + 条数 / 时长 / 金额 / 首末时间 |
| `records` | 历年今日明细：可用 `at` / `window` / `hour` 限定时刻，如「此刻」 |
| `crosstab` | 历年今日交叉汇总：`rows` × `cols` |

专用参数：

| 参数 | 说明 |
|---|---|
| `date` | 基准日：`09-29` 或 `2026-09-29`；留空 = 今天 |
| `years` | 只看这些年份，多值：`2024,2025` |
| `min_year` / `max_year` | 年份范围 |
| `exclude_current=1` | 排除今年，只看往年 |
| `at` | 只看某时刻：`17:24`，或 `now`（此刻） |
| `align` | 时刻**对齐**：`hour`（整点）/ `30` / `15` / `5` / `min`。`at=now&align=hour` → 现在 09:02 取 **09:00~10:00** |
| `hours` / `minutes` | 自对齐后起点向后的**跨度**：`align=hour&hours=2` → 09:00~11:00；`at=09:02&minutes=30` → 09:02~09:32 |
| `window` | 以 `at` 为中心的分钟窗口：`at=17:24&window=30` → 17:24 ± 30 分钟 |
| `hour` | 按时段筛选：`9` / `9,10` / `9-18` |
| `limit` / `offset` | `detail` 模式：明细条数与偏移（默认 100 / 0，上限 1000） |
| `sort` / `order` | `detail` 模式：明细排序字段与方向（默认 `time` / `desc`） |
| `with_years=0` / `years_limit` | `detail` 模式：不附带各年汇总 / 最多汇总年数（默认 20） |

> 注意：`month` / `start` / `end` 这类时间区间参数在「历史今日」中**不生效**（`date` 已被用作基准日），
> 请改用 `years` / `min_year` / `max_year`。其余通用过滤（号码 / 姓名 / 消息类型 / 地点 / 归属地 /
> 呼叫类型 / 关键词等）均可叠加。`type=anniversary` 与 `type=onthisday` 等价。

```
# 历年今日逐条明细（rows 即每条记录，另附各年汇总与总体汇总）
/api/ha_data_store/comm?type=onthisday&mode=detail&key=xxx

# 分页取明细，每条内容截断到 200 字
/api/ha_data_store/comm?type=onthisday&mode=detail&limit=50&offset=50&content_len=200&key=xxx

# 只看和张三有关的历年今日明细
/api/ha_data_store/comm?type=onthisday&mode=detail&party_names=张三&key=xxx

# 历年今日按年汇总（每年一条）
/api/ha_data_store/comm?type=onthisday&key=xxx

# 2025 年的今日，按日看
/api/ha_data_store/comm?type=onthisday&granularity=day&year=2025&key=xxx

# 历年今日，谁联系得最多（按通话时长）
/api/ha_data_store/comm?type=onthisday&mode=ranking&dimension=party_name&by=duration&key=xxx

# 历年今日的联系人汇总（只看张李二人）
/api/ha_data_store/comm?type=onthisday&mode=parties&party_names=张三,李四&key=xxx

# 「此刻」历年今天都在做什么
/api/ha_data_store/comm?type=onthisday&mode=records&at=now&window=60&key=xxx

# 历年今日的「本小时」（现在 09:02 → 09:00~10:00）
/api/ha_data_store/comm?type=onthisday&mode=records&at=now&align=hour&key=xxx

# 历年今日「最近 2 小时」／ 指定起点起算的 30 分钟
/api/ha_data_store/comm?type=onthisday&mode=records&at=now&align=hour&hours=2&key=xxx
/api/ha_data_store/comm?type=onthisday&mode=records&at=09:02&minutes=30&key=xxx

# 历年今日：按消息类型 × 联系人 交叉汇总通话时长
/api/ha_data_store/comm?type=onthisday&mode=crosstab&rows=msg_type&cols=party_name&metric=duration&key=xxx
```

#### 21.4 交叉汇总（`crosstab`）

不限「历史今日」，对全部（或指定范围的）数据生效，用于「按某维度汇总另一维度」：

```
/api/ha_data_store/comm?type=crosstab&rows=msg_type&cols=party_name&metric=count&key=xxx
/api/ha_data_store/comm?type=crosstab&rows=location,call_type&metric=duration&key=xxx
```

可用维度（`rows` / `cols` 通用）：`msg_type`、`location`、`party_place`、`call_type`、
`party_name`、`party_number`、`channel`、`my_number`。
`rows` 支持 1~3 个（多维度用 ` | ` 拼接为键），`cols` 最多 1 个（留空则只输出各行合计）。
返回 `row_keys` / `col_keys` / `matrix` / `row_metric` / `col_metric` / `grand_total`，可直接渲染表格。

#### 21.5 分析类接口（compare / contact / longest / quality / meta）

**周期对比 `compare`** —— 环比与同比一次拿全：

```
# 本月 vs 上月 vs 去年同期
/api/ha_data_store/comm?type=compare&period=month&key=xxx

# 指定基准日、只看同比
/api/ha_data_store/comm?type=compare&period=day&date=2026-09-29&compare=yoy&key=xxx

# 只看与张三的对比
/api/ha_data_store/comm?type=compare&period=month&party_name=张三&key=xxx
```

返回 `current` / `previous` / `yoy`（各含 `count` / `duration` / `cost` / `party_count` /
`active_days` / `label` / `start` / `end`）与 `diff`（差值与增长率 `*_pct`）。
`period=year` 时同比不适用，返回 `yoy: null`。

**联系人档案 `contact`** —— 给号码或姓名，拿完整画像：

```
/api/ha_data_store/comm?type=contact&party_number=13800000000&key=xxx
/api/ha_data_store/comm?type=contact&party_name=张三&month=2026-09&key=xxx
```

返回 `total`（总量 / 首末通讯 / 平均时长 / 活跃天数）、`days_since_last`（多久没联系）、
`span_days`、`avg_interval_days`（平均联系间隔）、`by_hour` / `by_weekday` / `by_month` /
`by_channel` / `by_msg_type` / `by_call_type`、`top_days`（联系最多的日子）与 `recent`（最近明细）。

**单次 Top N `longest`** —— 单条记录排行，区别于按人聚合的排行榜：

```
/api/ha_data_store/comm?type=longest&by=duration&limit=20&key=xxx
/api/ha_data_store/comm?type=longest&by=cost&party_name=张三&key=xxx
```

**数据质量 `quality`**：

```
/api/ha_data_store/comm?type=quality&key=xxx
```

`issues` 每项含条数与占比：时间为空、时间格式异常、既无号码也无姓名、时长为 0 / 为负 /
超 24 小时、金额为负、内容为空、图片消息缺路径；另有 `duplicates`（疑似重复：同 时间 +
号码 + 内容 出现多次）、`coverage`（活跃天数 / 跨度天数 / 覆盖率）与 `ok`（严重项是否全为 0）。

**数据概览 `meta`**：

```
/api/ha_data_store/comm?type=meta&key=xxx
```

返回表名、列清单、总量、时间跨度、`days_since_last`，以及 `values`（实际存在的渠道 /
消息类型 / 呼叫类型 / 归属地 / 地点及各自条数）与 `years` / `months` 清单——
适合给前端下拉当数据源，也能快速判断采集是否正常。

#### 21.6 历史今日（`onthisday`）· 通讯 / 设备 / 环境

「历史今日」已抽为独立模块 `onthisday.py`，一套代码同时服务三类数据源：

| source | 数据表 | 时间列 | 指标 | 维度 |
|---|---|---|---|---|
| `comm` | `attr_<type_name>` | `time` | 条数 / 通话时长 / 金额 | 对方姓名 / 号码、地点、归属地、消息类型、呼叫类型、来源、我方号码 |
| `device` | `device_history` | `on_time` | 开关次数 / 运行时长 / 用电(kWh) / 平均时长 | 实体 / 名称 / 房间 |
| `env` | `env_<metric>` | `datetime` | 采样数 / 平均值 / 最大值 / 最小值 | 实体 / 名称 / 房间 |

```
GET|POST /api/ha_data_store/onthisday?source=comm|device|env&key=你的APIKey
```

**通用参数**（三类通用）：

| 参数 | 说明 |
|---|---|
| `source` | 数据源：`comm`（默认）/ `device` / `env` |
| `date` | 基准日：`09-29` 或 `2026-09-29`；留空 = 今天 |
| `years` / `min_year` / `max_year` / `exclude_current` | 只看指定年份 / 年份范围 / 排除今年 |
| `at` / `align` / `hours` / `minutes` / `window` / `hour` | 时刻筛选（`at=now&align=hour` → 09:00~10:00） |
| `mode` | `stats`（默认）/ `detail` / `ranking` / `crosstab` |
| `limit` / `offset` / `sort` / `order` / `fields` / `content_len` | 明细分页、排序与字段筛选 |
| `drop_fields` | **黑名单**：从明细中剔除指定列（逗号分隔）。优先级低于 `fields`（两者同时给出时以 `fields` 为准）；未裁剪时默认返回全部列 |
| `env_by_room` | **仅 env 源**：`1`（默认）= 明细按「房间 × 时间点」聚合（一个房间一行、多种指标成列）；`0` = 平铺，此时每行附带 `metric` 字段。详见 21.7.1 |
| `limit` | 分页条数：**只设默认值、不设上限**。缺省 = 默认条数（明细类 100），`0` 或负数 = **不限条数**，其余按传入值返回 |
| `room_bucket` | 配合 `env_by_room`：时间聚合精度（分钟，默认 `1`）；`0` = 精确到秒不合并 |

**数据源专属**：`comm` 的类型名一般无需填写（自动解析，见 21.7）；`device` 用 `entity_ids` / `rooms` / `names`；
`env` 用 `env_metric`（`temperature` / `humidity` / `pm25` / `co2` / `power` / `sensor`；
留空 = 合并所有存在的指标表，均值按采样数加权）与 `entity_ids` / `rooms`。

> ⚠️ **`metric` 是交叉汇总的「测度」参数**（`count` / `duration` / `max_value` …），
> 与环境指标不是同一个东西——环境指标请用 `env_metric`。
> （为兼容，`metric` 的取值恰好是环境指标名时也会生效；取 `count` 之类的测度值会被忽略。）

```
# 去年今天设备开了多久、用了多少电（按日）
/api/ha_data_store/onthisday?source=device&date=2025-09-29&mode=stats&granularity=day

# 历年今日的设备排行（按用电）
/api/ha_data_store/onthisday?source=device&mode=ranking&dimension=name&by=energy

# 历年今日客厅温度的平均值与极值
/api/ha_data_store/onthisday?source=env&env_metric=temperature&room=客厅

# 「此刻」历年今日的设备开关明细（只取三列）
/api/ha_data_store/onthisday?source=device&mode=detail&at=now&align=hour&fields=on_time,name,duration
```

> **与通讯版的兼容**：`/api/ha_data_store/comm?type=onthisday&...` 仍然可用，内部会委托到
> 同一个模块（固定 `source=comm`）。旧的 `mode=records` / `mode=parties` 会自动映射为
> `detail` / `ranking`（`dimension=party_number`）。

#### 21.7 历史今日 · HA 实体

| 实体 | 说明 |
|---|---|
| `sensor.ha_data_store_today_in_history` | 状态 = 三类数据的记录总数。属性含 **`comm` / `device` / `env` 三个节点**，每个节点有 `count`（总数）、`summary`（总体汇总）、`years`（各年汇总）、`detail`（逐条明细，每类最多 10 条）、`metrics` / `dimensions`（可用指标与维度说明）。**每整点自动刷新一次**，设置实体变化时立即刷新（不参与 HA 轮询） |
| `text.ha_data_store_today_in_history_set` | 时间范围设置，写法 **`<时间>,<前后分钟>`**：`01,80` = 01:00 前后 80 分钟、`now,60` = 此刻前后 60 分钟、`09:02,30` = 09:02 前后 30 分钟；**留空 = 全部数据**。等价于接口的 `at=<时间>&window=<分钟>`。写入后传感器**立即重算三个节点**；重启后由 `RestoreEntity` 保持设置 |
| `text.ha_data_store_comm_type_name` | 通讯数据表类型名（对应表名 `attr_<类型名>`）。**留空 = 自动探测** `attr_type_defs` 中 `mode=comm` 的类型名（优先取数据表已存在者），通常无需设置；存在多张通讯表时用它明确指向其中一张。影响历史今日（`comm` 源）、传感器与通讯查询 API 的默认类型名 |

传感器直接复用本模块的查询实现（`run_onthisday_query`），因此与 API 口径完全一致。
某一类数据源的表不存在时，该节点只返回 `error`，不影响其它两类（`warnings` 会列出原因）。

**明细呈现方式**（只作用于 `detail`，`summary` / `years` 等汇总不受影响）：

| 数据源 | 排序字段 | 明细形态 |
|---|---|---|
| `comm` | `time` | 剔除 `id` / `datetime` / `extra_json` / `name` / `room` / `updated_at` 六个通用元数据列，保留全部通讯业务字段（含 `entity_id`） |
| `device` | `on_time` | 不裁剪（保留全部列） |
| `env` | `datetime` | **按「房间 × 时间点」聚合**——一个房间一行，各指标独立成列（见下） |

通讯用**排除列表**（`drop_fields`）而非白名单，因此将来表里新增列会自动保留。

#### 21.7.1 环境明细：一个房间的多种数据

环境数据分布在多张表（`env_temperature` / `env_humidity` / `env_pm25` / `env_co2` /
`env_power` / `env_sensor`）。若逐条平铺，不同指标的值会混在同一个 `value` 列里而
**无法区分指标**：

```yaml
# 平铺（API 默认）：哪个是温度、哪个是湿度看不出来
- {datetime: '2026-09-29 10:00:00', room: 客厅, value: 25.0}
- {datetime: '2026-09-29 10:00:00', room: 客厅, value: 58.0}
```

传感器改为按「房间 × 时间点」聚合，每个指标独立成列：

```yaml
env:
  count: 535          # 采样总数（与聚合前口径一致）
  group_count: 54     # 聚合后的行数（房间 × 时间点）
  room_count: 10
  rooms: [主卧, 次卧, 客厅, 厨房, ...]
  metric_names: [temperature, humidity, pm25, co2]
  summary:                        # 按指标分组——各指标量纲不同，混算没有意义
    count: 535                    # 采样总数（跨指标求和仍有意义：采了多少条）
    by_metric:
      temperature: {count: 90, avg_value: 24.5, max_value: 26.1, min_value: 19.8}
      humidity:    {count: 90, avg_value: 57.2, max_value: 68.0, min_value: 45.0}
      pm25:        {count: 90, avg_value: 35.1, max_value: 52.0, min_value: 18.0}
  years:
    - year: '2026'
      count: 400
      by_metric: {temperature: {...}, humidity: {...}, pm25: {...}}
  detail:
    - {room: 客厅, datetime: '2026-09-29 10:00:00', temperature: 25.0, humidity: 58.0, pm25: 35.0}
    - {room: 次卧, datetime: '2026-09-29 10:00:00', temperature: 24.3, humidity: 56.0, pm25: 28.0}
```

**`summary` / `years` 同样按指标分组**（无论明细是否聚合）。温度 25、湿度 58、CO₂ 800
混在一起求平均得到 46.99——这个数字不代表任何东西，所以顶层只保留 `count`（采样总数），
`avg_value` / `max_value` / `min_value` 一律放进各指标内部。comm / device 各表量纲一致，
沿用原有结构不变。

**分页与截断**：`count` 是**本页**行数，`total` 是采样总数，`group_count` 是聚合后的总行数
（房间 × 时间点）。响应含 `truncated`（是否还有未返回的行）与 `remaining`（未返回行数），
不必再靠数数判断。

> **`limit` 只设默认值、不设上限**：
> - 不传 → 用默认条数（明细类 100）
> - 传 `limit=0`（或负数）→ **不限条数，一次返回全部**
> - 传其它值 → 按该值返回，**不会被钳制**
>
> 响应里的 `limit_max` 恒为 `null`，表示无上限。数据量大时可配合 `offset` 翻页。

> ⚠️ **低频指标容易被挤出窗口**：温度湿度每批 12 条、`power` 每批只有 1 条（它是「全屋」总表）。
> 时间倒序排列时，`limit` 偏小的查询会先被高频指标占满，看起来像"power 少了"——实际是分页截断。
> 只想看某个指标时用 `env_metric` 筛选（如 `env_metric=power`），或把 `limit` 调大（上限 1000）。

**`summary.by_metric` 的键与 `tables` 一一对应**——包括**存在但当前筛选下无数据**的指标，
它们以 `{count: 0, avg_value: null, max_value: null, min_value: null}` 占位。
这样一眼就能区分"这个指标没采到数据"和"它被漏统计了"（后者曾经发生过：空指标被静默跳过）。
聚合值用 `null` 而非 `0`：没有采样 ≠ 采样值恰好是 0（温度 0℃ 是有效读数）。

按年的 `years[].by_metric` 则**只列该年有数据的指标**——年份维度上补空没有意义。

- **`room_bucket`**：时间聚合精度（分钟，默认 1）。同一房间同一分钟内的多条采样取平均，
  因此各指标表的时间戳差几秒也能对齐到同一行；设 `0` = 精确到秒不合并
- 同一房间有多个同类传感器时，该指标取这些采样的**平均值**
- `room` 为空的环境记录单独成行（房间显示为空字符串）
- `total` / `count` 仍是**采样总数**，与聚合前一致；`returned` 才是聚合行数
- 排序为「时间倒序，同一时刻按房间名正序」

**API 与传感器默认都用聚合格式**，直接请求即可：

```
/api/ha_data_store/onthisday?source=env&mode=detail&key=xxx
```

需要旧的平铺格式时传 `env_by_room=0`，此时每行会附带 **`metric`** 字段标明指标名，
否则同一条记录的 `value` 看不出是温度还是湿度：

```json
{"datetime": "...12:30:00", "room": "次卧", "value": 24.8, "metric": "temperature"}
```

`metric` 在平铺模式下**总会附带**（即使指定了 `fields` 白名单），因为缺了它就无法分辨指标。
聚合模式下 `fields` / `drop_fields` 不适用（列由数据动态决定）。

传感器的开关是类常量 `DETAIL_ENV_BY_ROOM`（默认 `True`）。

**通讯类型名的解析顺序**（见 `comm.resolve_comm_type_name`）：

1. 显式 `type_name` 参数（**一般无需填写**，需要明确指向某张表时才传）
2. `text.ha_data_store_comm_type_name` 实体的设置值
3. **自动探测**：`attr_type_defs` 中 `mode='comm'` 且**数据表已存在**的类型名（按名称排序取首个）
4. `attr_type_defs` 中 `mode='comm'` 的类型名（表尚未建，至少给出正确的名字）
5. 默认 `comm_records`

第 3 步意味着：在「属性提取」里用了自定义类型名（如 `my_phone`）也**无需任何额外配置**即可查询；
若表确实不存在，报错信息会列出已登记的通讯类型名，便于定位。

前两步由 **传感器、通讯查询 API（`/api/ha_data_store/comm`）、历史今日 API
（`/api/ha_data_store/onthisday`）共用同一实现**（`comm.read_comm_type_name_setting` 读实体、
`comm.resolve_comm_type_name` 做探测），因此三处口径必然一致——不会出现「传感器认得设置、
API 却不认」的情况。响应体里的 `type_name` 回显的是**实际生效值**（含自动探测结果）。

属性结构示例：

```yaml
state: 7                     # 三类记录总数
on_this_day: "09-29"
base_date: "2026-09-29"
range: "全天（不限定）"        # 或 "01:00 ± 80 分钟"（随设置实体变化）
at: ""                       # 设置解析出的时刻（"01:00" / "now"）
window: 0                    # 设置解析出的前后分钟数
exclude_entities: []         # 生效的排除实体（见 21.8）
exclude_count: 0
comm:
  count: 3
  summary: {count: 3, duration: 443, cost: 0, ...}
  years: [{year: "2026", count: 1, ...}, ...]
  detail: [{time: "...", party_name: "...", duration: 90, ...}, ...]
  metrics: {count: "条数", duration: "通话时长(秒)", cost: "金额(元)"}
  dimensions: [party_name, party_number, ...]
device:
  count: 2
  summary: {count: 2, duration: 7200, energy: 1.5, ...}
  detail: [...]
env:
  count: 2
  summary: {count: 2, avg_value: 25.5, max_value: 26, min_value: 25}
  detail: [...]
```

**DB 浏览器入口**：API 工具 → 查询类型新增两个分组：
- 「📞 通讯数据查询」除明细 / 日期 / 排行 / 趋势 / 汇总 / 联系人 / 地点 / 时段外，另有
  6 个统计分析入口、交叉汇总、周期对比、联系人档案、单次 Top N、数据质量、数据概览
- 「📜 历史上的今日」5 个入口（汇总 / 排行榜 / 联系人汇总 / 明细 / 交叉汇总）
- 「📜 历史今日 · 设备 / 环境」8 个入口（设备 / 环境 × 汇总 / 明细 / 排行 / 交叉汇总）

选中后参数区按类型（及预设）动态渲染，粒度与条件均可再改。

#### 21.8 排除实体（传感器与 API 共用）

在 **系统配置 → 📜 历史今日** 中维护一份**排除实体**清单
（存 `api_settings.today_in_history_exclude_entities`），被排除的 `entity_id`
不参与历史今日的**任何数据源、任何 mode** 的统计：

- 对 **API**（`/api/ha_data_store/onthisday`）与 **传感器**
  （`sensor.ha_data_store_today_in_history`）**同时生效**
- 三类数据源（`comm` / `device` / `env`）都按各自的 `entity_id` 列过滤
- `entity_id` 为空的历史记录**不受影响**（不会被误排除）

页面提供三种添加方式：手动输入（逗号 / 换行，支持一次粘贴多个）、chips 列表点 ✕ 删除、
从候选实体列表（来源为 `device_history` / `env_*` / `attr_*` 的 `entity_id` 并集）搜索后 ➕ 添加。
保存后传感器立即刷新。

单次查询还可用参数 `exclude_entities` 临时追加排除项（多值，逗号分隔），与设置取**并集**：

```
# 临时排除某实体（不改变持久化设置）
/api/ha_data_store/onthisday?source=device&exclude_entities=switch.ac&key=xxx

# 与 entity_ids 叠加：先按 entity_ids 收窄，再剔除排除项（取交集）
/api/ha_data_store/onthisday?source=device&entity_ids=switch.a,light.b&exclude_entities=light.b&key=xxx
```

配置读写接口：

```
GET|POST /api/ha_data_store/onthisday/exclude      # 读 / 写排除清单
                                                   # POST body: {"exclude":[...]} 或 {"text":"a,b"}
GET      /api/ha_data_store/onthisday/entities     # 候选实体列表（供页面选择）
```

响应体会回显 `exclude_entities` / `exclude_count`，便于确认当前口径；传感器属性同样带这两项。

---

### 22. 数据导入 / 导出（CSV · JSON）

数据浏览页工具栏：**📥 导入数据**（把外部数据写进任意数据表）、**⬇️ 导出CSV**（导出当前表）。

#### 22.1 导入流程（5 步）

| 步骤 | 说明 |
|---|---|
| ① 数据源 | 选择 CSV / JSON 文件（自动识别 UTF-8 与 GBK）或直接粘贴文本。CSV 自动嗅探分隔符（`,` `;` `\t` `\|`）、正确处理引号内的逗号与换行、可指定「首行是表头」；JSON 支持对象数组、`{"data":[...]}`、数组的数组。**大文本自动按行分片提交，无需自己拆分文件** |
| ② 目标表 | 任选已有表；可勾选「表不存在时自动建表」（按数据推断列类型）与「缺少的列自动创建」 |
| ③ 字段映射 | 以**目标列为基准**，每行填源列名或固定值（与通讯采集同一套交互，绿=源列 / 橙=固定值）。留空的目标列不写入 |
| ④ 写入模式 | **追加** / **Upsert**（勾选唯一键列，可多列组合）+ 日期时间规范化、数值清洗开关 |
| ⑤ 执行 | 🧪 试运行（不写库，只回报将新增/更新多少行、会建哪些表列）/ ▶ 开始导入 / ⬇️ 下载目标表空模板 |

结果报告包含新增 / 更新 / 失败数、耗时、建表与补列明细，以及最多 50 条错误行（带行号）。

#### 22.2 值转换（基础层，可分别关闭）

| 输入 | 输出 |
|---|---|
| `¥1,234.50` / `12.5元` | `1234.5` / `12.5` |
| `2026/9/1 17:24` / `2026年9月1日` | `2026-09-01 17:24:00` / `2026-09-01 00:00:00` |
| 空单元格 | 可空列 → NULL；`NOT NULL` 列 → 该列的 DDL 默认值 |

> 中文时长（`3分53秒`）不在本模块的转换范围内，如需请用通讯采集模式。

#### 22.3 接口

```
POST /api/ha_data_store/import/parse       解析数据源文本（列名 + 全部行 + 前 20 行预览）
POST /api/ha_data_store/import             单批导入（支持 dry_run 试运行）
GET  /api/ha_data_store/import/template?table=xxx    下载目标表空模板 CSV
GET  /api/ha_data_store/export/csv?table=xxx         导出表数据为 CSV
```

**大文件处理**：解析只回传列名、总行数与前 20 行预览（**不回传全量数据**）；
导入时前端把文本按行切片（每片约 200 万字符、各片自带表头）分多次提交，避免超出 HA 的请求体上限（约 16 MB），
切片时不会切断引号内的跨行字段。服务端**流式解析**并按每 2000 行一批写入，内存占用与总行数无关；
整批失败时自动降级为逐行重试并记录失败行，不会因一行错误丢掉整批。

**安全**：表名 / 列名白名单校验 + 严格参数化写入；核心配置表（`api_keys`、`entity_configs`、
`attr_type_defs`、`push_targets` 等 11 张）与 `sqlite_*` 系统表禁止导入；
写操作受「数据库修改」开关约束。

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

**通讯数据查询（独立路径）：**

```
GET /api/ha_data_store/comm?type=records&date=2026-09-01&key=你的APIKey
```

`type` 可取 `records` / `dates` / `stats` / `crosstab` / `compare` / `contact` / `longest` /
`quality` / `meta` / `onthisday` / `ranking` / `trend` / `summary` / `parties` / `places` / `heatmap`；
参数与返回结构详见 [21. 通讯数据（采集 + 查询）](#21-通讯数据采集--查询)。

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
| `/api/ha_data_store/clear_table` | POST | 清空指定表并重置自增 ID（body `{table, admin_password, vacuum?}`，需管理员密码；核心表禁止） |
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
- 📥 导入数据（CSV / JSON，字段映射 + 固定值、追加 / Upsert、自动建表与补列、试运行）
- ⬇️ 导出CSV（当前表数据；另可下载目标表空模板）
- 🧹 清空表（删除全部数据并让自增 ID 从 1 重新开始，需管理员密码 + 二次确认）

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

通讯数据采集（`mode=comm`）使用 `attr_comm_records`，固定 16 列
（见 [21. 通讯数据（采集 + 查询）](#21-通讯数据采集--查询)），并为
`time`、`(party_number,time)`、`(party_name,time)`、`(location,time)` 建立索引。

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

### v4.16.7 新增「👥 按通讯录回填姓名」按钮（一键，无需输入）（2026-10-03）

**问题**：「👥 通讯录」这个按钮点开是**导入面板**（让你粘贴 / 选文件），
但按"通讯录回填"的字面理解，应该点一下就**按 `comm_contacts` 表回填姓名字段** ——
两者是不同的事，按钮名字没区分开。

**现在拆成两个按钮**：

| 按钮 | 作用 |
|---|---|
| **👥 按通讯录回填姓名** | **点一下就按 `comm_contacts` 表回填「对方姓名」列**，无需任何输入 |
| 📥 通讯录管理 | 管理通讯录数据本身（导入 / 查看 / 清空），再点一次收起 |

原有的「📇 全部回填（历史数据）」保留（一次补归属地 / 运营商 / 姓名 / 坐标）。

**后端新增「只回填指定列」能力**（`comm_backfill`）：

- `CommFiller(..., only_columns=[...])`：`_has()` 先判限定列，不在范围内直接跳过
- `build_filler(..., only_columns=...)`
- `backfill(..., columns=["party_name"])`：`active` 列表按 `columns` 过滤，
  报错信息也据此给出
- `POST /api/ha_data_store/comm/backfill` 新增 `columns`（数组或逗号分隔字符串）

```
{ "type_name": "comm_records", "columns": ["party_name"], "only_empty": 1 }
```

**前端**：

- `runCommBackfill(dry, columns)` —— 第二个参数指定只跑哪几列
- 确认框与结果摘要都带上用途，如
  `🧪 预览（「姓名」）：扫描 53365 行，将更新 41207 行（party_name +41207）`
- 新增 `_commColLabel()` 把列名翻成中文标签（延迟查 `COMM_FIELD_DEFS`，
  避开常量初始化顺序）

> 版本号 → `4.16.7`。需重启 HA 生效（前端刷新页面即可）。

### v4.16.6 「其他数据回填」界面收纳：长列表折叠（2026-10-03）

**问题**：点「👥 通讯录」后面板摊一大片 —— 主要是状态行里一次铺出
**最近导入的 20 条**「号码 → 姓名」（一千多人的通讯录，这 20 条能占好几行）。

**收纳改动**：

| 位置 | 之前 | 现在 |
|---|---|---|
| 最近导入列表 | 20 条全铺 | **5 条** + 「展开其余 N 条」（`<details>`） |
| 通讯录格式说明 | 4 行常显 | 一行摘要 + 「格式说明」折叠 |
| 粘贴框 | 7 行高 | 5 行高 |
| 待回填字段明细 | `party_place 78025、party_isp 110790、…` 挤在统计行里 | 「各字段待填」折叠 |
| 「👥 通讯录」按钮 | 只能打开 | **再点一次收起**（toggle） |

功能一处没减，只是把"不常看"的部分折起来。折叠用的是原生 `<details>`，
没有额外的 JS 状态要维护。

> 版本号 → `4.16.6`。前端改动，**硬刷新页面**（Ctrl+F5）即可。

### v4.16.5 修复：外部导入的通讯录匹配不上（读取端未归一化）（2026-10-03）

**症状**：通讯录（`comm_contacts` 表）里明明有这个人，采集时却匹配不到姓名。

**根因**：**读取端没有归一化号码**。

```python
# comm_contacts.load_names() 旧实现
for number, name in conn.execute("SELECT number, name FROM comm_contacts"):
    num = str(number or "").strip()      # ← 只 strip，没归一化
    mapping[num] = nm
```

- 走界面「👥 通讯录」**导入**的号码，在 `import_contacts()` 里已归一化，所以能匹配；
- 但如果是**外部工具直接写进这张表**（腾讯 / 微信 / 其它导出，号码形如
  `+86 138-0013-8000`、`8613900139000`），存进来的就是原始写法 ——
  做 key 时对不上规范号码，**永远匹配不上**。

**修复**：

| 位置 | 改动 |
|---|---|
| `load_names()` | 读取时 `num = normalize_number(number)` |
| `import_contacts()` | 判重 key 同样归一化，**并记下 `rowid`** |
| `import_contacts()` 的 UPDATE | 改用 `WHERE rowid = ?`（原实现拿规范化号码去匹配库里原始写法，静默更新 0 行） |
| UPDATE 语句 | 顺带把该行 `number` 写回规范形式 |

**顺带**：缓存加 **TTL（60 秒）**。指纹（行数 + `MAX(updated_at)`）能发现大多数外部改动，
但如果外部只改姓名、没动 `updated_at`，指纹察觉不到 —— TTL 兜底，
即使这样也会在 60 秒内重读。

> 版本号 → `4.16.5`。需重启 HA 生效。

### v4.16.4 数据回填独立成「其他数据回填」子页（2026-10-03）

原来「通讯字段回填」这块挤在 **API 工具 → 通讯数据查询**的参数区里 ——
它其实是**数据维护**操作，跟"构造查询 URL"不是一回事，位置也不显眼。

现在移到 **系统配置 → 📇 其他数据回填**：

- 新增顶层子选项卡 `📇 其他数据回填`（在「📊 指标管理」之后）
- 子页顶部补充了完整说明：补哪些字段、各自的本地数据源、只填空值、
  新数据已在采集时自动回填（可逐个实体开关）
- 原位置只留一行指路提示

**顺带加了「数据类型」下拉**（原来只能靠后端自动探测）：

- 只列出 `mode=comm` 的类型，选项「（自动探测）」= 保持原行为
- 切换即刷新状态；「自动填写 / 预览」都会带上所选类型
- 进入该子页时自动加载类型列表与库状态

> 版本号 → `4.16.4`。前端改动，**硬刷新页面**（Ctrl+F5）即可。

### v4.16.3 修复通讯录姓名匹配不上的两个原因（2026-10-03）

**症状**：通讯录导入了，但采集时没有补上「对方姓名」。

**原因一：号码归一化漏了不带 `+` 的国家码**

```python
# 旧：只去 +86 / 0086
13800138000 / +86 138-0013-8000 / 008613800138000  →  都能对上
8613800138000                                      →  仍是 8613800138000  ← 对不上
```

有些导出（Excel、部分手机）给的是 `8613800138000` 这种形式，
它归一化后多出 `86`，与采集到的 `13800138000` 不匹配 → 姓名补不上。

现在补上：**纯数字长度 > 11 且以 `86` 开头**才剥掉前两位。
判据保守 —— 10 位的 `8610`（北京固话区号）不会被误剥。

**原因二：缓存感知不到外部改动**

`load_names()` 原来只按 `db_path` 缓存。如果通讯录是**在别处**改的
（外部工具 / 直接写库 / 另一个进程），`invalidate_cache()` 不会被调用，
采集侧就一直用旧快照。

现在缓存比对**指纹** —— `COUNT(*)` + `MAX(updated_at)`（一次廉价查询）：

```python
row = conn.execute("SELECT COUNT(*), COALESCE(MAX(updated_at), '') FROM comm_contacts").fetchone()
fp = f"{row[0]}:{row[1]}"
```

指纹一致才用缓存，否则重载。所以绕过界面直接改库也能在下一轮采集生效。

**顺带增强了诊断**，方便对账：

- 采集日志的「通讯自动回填已启用」由 `debug` 提到 **`info`**，
  打出「归属地=有/无 坐标=有/无 通讯录=N 条」
- 匹配不到的号码会**记下前 10 个**（归一化后），并在「📇 自动填写」的
  预览结果里显示（如 `通讯录里没有 3 个号码（如 13511112222、13511113333）`）
  —— 直接看出是"通讯录没这个人"还是"号码写法对不上"

> 版本号 → `4.16.3`。需重启 HA 生效。

### v4.16.2 修复「更换采集实体」的真正原因：比较对象被提前覆盖（2026-10-03）

**症状**：在「修改属性提取配置」里改了实体，提示保存成功，**刷新后又回到原样**。

**根因**（v4.16.1 没修到点子上）：

`saveAttrEdit()` 第 3 段会调 `_aeSyncEntityInputs()`，把表格输入同步回 `ctx.entities`：

```js
if (idEl) e.entity_id = (idEl.value || '').trim();   // ← 这里就把原始值覆盖掉了
```

而第 6 段判断是否换了实体时，用的正是这个**已被覆盖**的字段：

```js
if ((orig.entity_id || '') !== newId) { entRenames.push(...); }   // 永远相等 → 恒不触发
```

于是 `entRenames` **永远是空的** —— 既不会调接口，也不会报错。如果这期间还改了别的
（比如自动回填开关），保存会成功并提示「修改已保存」，但 `entity_id` 一动没动，
重新打开就"回去"了。

**修复**：

- 打开弹窗时给每一行记下**原始 ID**：`(ecs.rows || []).forEach(r => { r._origId = r.entity_id || ''; })`
- 比较改用 `_origId`：`const oldId = (orig._origId !== undefined) ? orig._origId : (orig.entity_id || '');`
- 顺带删掉一处**重复的 rename 循环**（v4.16.1 的两次编辑都落了盘，导致同一行会被
  改两次 —— 第二次拿旧 ID 去查必然失败）

**后端加「回读确认」**：`UPDATE` + `commit()` 之后重新 `SELECT` 一次，
查不到新配置行就报错。这样"接口说成功、库里其实没变"这类静默失败不会再出现。

> 版本号 → `4.16.2`。需重启 HA 生效。

### v4.16.1 修复「更换采集实体」不生效 + 新增每实体的「自动回填」开关（2026-10-03）

**一、修改「实体」后不生效**

原来「更换实体」是把 `entity_id` 当成**单元格更新**提交的，并且在保存前先用
`/api/ha_data_store/entity_state` 预校验新实体存在。问题在于：

- 校验失败时只弹一个**一闪而过的 toast** 就 `return`，很容易以为已保存；
- 走通用单元格更新时，冲突 / 失败的原因也只是一句泛泛的错误。

现在改用**专用分支**（`POST /api/ha_data_store/attr_entity_mapping`，`action: "rename"`）：

```
Body: { action: "rename", entity_id: "旧实体", new_entity_id: "新实体", attr_type: "comm_records" }
  → { success, renamed, entity_id, old_entity_id }
```

- 按 **(entity_id, attr_type)** 定位，不再依赖前端拿到的 `rowid`
- 后端用 `hass.states.get()` 校验新实体确实存在
- 冲突（新实体已有同类型配置）/ 原配置不存在 / 实体不存在，各自返回**明确原因**
- 前端把错误直接抛到状态栏，不再静默返回

**二、新增每实体的「自动回填」开关**

「实体采集参数」表格新增一列 **自动回填**（复选框，默认勾选）：

| 列 | 存储 | 说明 |
|---|---|---|
| 自动回填 | `entity_configs.autofill` | 1=采集每行前自动补全空字段；0=完全不回填 |

- **只对通讯（comm）模式有意义**，其它模式该列显示「不适用」且禁用
- 存储：`entity_configs` 新增 `autofill INTEGER NOT NULL DEFAULT 1`，
  `_migrate_database` 自动补齐（**重启即升级，老实体默认开启**）
- 接口 `POST /api/ha_data_store/attr_entity_mapping` 新增 `autofill` 字段；
  **不传则保持不变**（`CASE WHEN ? >= 0 THEN ? ELSE autofill END`），
  避免只改字段映射时把开关重置
- 采集侧：`_attr_collect_for_entity` 的判断改为
  `if mode == ATTR_MODE_COMM and int(cfg.get("autofill", 1) or 0):`

> 关掉之后，该实体写库时不再补归属地 / 运营商 / 姓名 / 坐标，
> 完全按源数据与字段映射原样写入。

> 版本号 → `4.16.1`。需重启 HA 生效。

### v4.16.0 通讯记录：新增「通讯录」与采集时自动回填（2026-10-03）

数据**一进来就是全的** —— 不必事后再点「自动填写」。

**回填 5 个字段**（只填空值，不动源数据已给出的内容）：

| 字段 | 来源 |
|---|---|
| `party_place` | `party_number` 查本地归属地库（**只要市不要省**） |
| `party_isp` | `party_number` 查本地归属地库（标准化运营商名） |
| `party_name` | `party_number` 查**你导入的通讯录**（新增） |
| `party_coordinate` | `party_place` 查本地城市坐标表（420 个城市） |
| `location_coordinate` | `location` 查本地城市坐标表 |

**一、新增通讯录**（`comm_contacts` 表）

姓名无法从任何内置库推出来，只能你自己提供。在「通讯数据查询」页点
**👥 通讯录** 导入，支持三种格式（自动嗅探）：

```
13800138000,张三          # 每行一条，逗号/制表符/分号/竖线都行
13900139000,李四          # 手机导出的「姓名,号码」也能直接粘（列序自动判断）

[{"number":"13800138000","name":"张三"}]        # JSON 数组（键名支持 phone/手机/号码…）
{"13800138000":"张三"}                           # JSON 对象
```

- 号码**统一归一化**（去 `+86` / `0086` / 空格 / 连字符），所以
  `+86 138-0013-8000` 与 `13800138000` 是同一个号，重复导入是**更新**而非新增
- 可选「先清空再导入」做整份替换；也可从文件选择（`.csv` / `.txt` / `.json`）
- 界面会显示总人数与最近导入的条目，并在状态栏提示通讯录是否为空

**二、采集时自动回填**

通讯模式下，每写入一行前都会做一次本地回填 —— `poll` 轮询、`event` 状态变化、
手动触发**三条路径全部生效**。因为公司/坐标/姓名都来自本地（内存缓存），
对采集速度没有可感知的影响。

> 回填器与「📇 自动填写」按钮**共用同一份判定逻辑**（`comm_backfill.CommFiller`），
> 区别只是时机 —— 一个在写入前，一个用来补历史数据。

**三、其它**

- 按钮文案改为「📇 自动填写（历史数据）」，说明它补的是历史数据
- 状态栏新增「通讯录 N 人」，为空时提示去导入
- 回填结果新增「通讯录里没有 N 个号码」统计
- 回填失败不影响采集（归属地库缺失时按空处理并继续）

> 版本号 → `4.16.0`。需重启 HA 生效。

### v4.15.4 通讯查询新增 6 个接口 · 流量改按数据量统计（2026-10-02）

**一、新增 6 个接口**（`/api/ha_data_store/comm`）：

| type | 用途 |
|---|---|
| `chat` | 以**对端**为中心的一整段对话（含正文）。`mode=detail` 只回逐条内容、`summary` 只回汇总、`both`（默认）都回；`empty_content=0` 只留有正文的记录 |
| `calendar` | 某月**每一天**的汇总，**缺的天补 0** —— `dates` 只返回有数据的天，画日历要完整格子；另附 `first_weekday`（该月 1 号是周几）与月度合计 |
| `stale` | 失联名单：超过 N 天没联系的联系人（按失联天数从久到近） |
| `duration_dist` | 通话时长分布：按**时长**分桶（`<10秒 / 10秒-1分 / 1-5分 / 5-30分 / 30分-2小时 / ≥2小时`），给直方图用 |
| `new_peers` | 新联系人：**全库首次联系时间**落在指定范围内的号码（不是"范围内有记录"） |
| `peak` | 最忙的时段 / 日子：`dim=date\|hour\|weekday\|month\|year\|day`，`by=count\|duration\|cost\|traffic_usage` |

另外 `compare` 新增 `compare=range`：用 `a_start/a_end` 与 `b_start/b_end` 对**任意两个区间**做对比。

**二、流量改按数据量统计**：流量记录的 `duration` 是"上网时长"，与"用了多少流量"是两个口径
（实测某条会话 642 秒只跑 0.05 MB）。`stats` 的桶与 `total`、`crosstab` 的行、`dates` 的每天
都补上 `traffic_usage`；`traffic_type` 加进维度白名单。

**三、接口版本号**：`comm.py` 新增 `COMM_API_VERSION`，每个响应都带 `api_version`；
db_viewer 侧对应 `COMM_VIEWER_EXPECTED_VERSION`，显示在通讯查询说明区 ——
后端改动必须重启才生效，有版本号就不用猜。

**四、db_viewer.html**：「📞 通讯数据查询」新增对应入口（含「聊天记录明细」）；
清理预设参数里的真实姓名与号码（`13363902861` → `13800000000`、`宋平平` → `张三`）。

> 验证：新接口用内存 SQLite + 打桩加载真实 `comm.py` 跑过（43 + 21 + 16 项）；
> 前端改动用语法检查 + 真实 eval 字段规格验过（28 + 11 + 4 项）。均全通过。

> 版本号 → `4.15.4`。**需重启 HA 生效**（`comm.py` 有改动）；前端硬刷新页面即可。

### v4.15.3 「类型级映射」表格加醒目提示（避免改了看不到效果）（2026-09-30）

界面上有**两处**显示字段映射，容易混淆：

| 位置 | 数据来源 | 优先级 |
|---|---|---|
| **属性提取 → 编辑** 的「实体采集参数」→ 行尾「编辑」 | 实体级 → 内置预设 → 类型级 | 前两层可覆盖类型级 |
| **属性提取 → 新建/编辑** 的「通讯字段映射」表格 | **纯类型级** | 只对「无预设、无自定义」的实体生效 |

第二处**现在只是兜底** —— `*_calls` / `*_sms` / `*_traffic` 走内置预设、
其它实体多半有实体级配置，所以**在那里改映射往往看不到效果**。

该表格顶部现在加了橙色警示条，写明「这是类型级默认映射，只对没有预设也没有单独配置的实体生效；
`*_calls` / `*_sms` / `*_traffic` 会走内置预设；那两层都优先于这里」，并指明去哪改。

> 功能没变，只是把"改了为什么没反应"这件事说清楚。
> 各实体实际用哪份映射，看「实体采集参数」里点「编辑」后面板**头部**的标注
> （`内置预设：xxx` / `自定义` / `继承类型级`）。

> 版本号 → `4.15.3`。前端改动，**硬刷新页面**（Ctrl+F5）即可，无需重启 HA。

### v4.15.2 修复：实体映射面板把「内置预设」和「类型级」串在一起（2026-09-30）

**问题**：流量实体的映射面板里，除了正确的预设 7 项外，还混进了一堆**通话**的字段：

```
party_number ← phone_number      ← 流量不该有
party_place  ← number_location   ← 流量不该有
location     ← location          ← 流量不该有
msg_type     ← type              ← 流量不该有
call_type    ← call_type         ← 流量不该有
party_isp / party_coordinate / location_coordinate  ← 流量不该有
```

而 `time ← datetime`、`duration ← duration_seconds`、`traffic_usage ← volume_mb` 又是对的 ——
**对的是预设项，错的全来自类型级**（库里类型级 `field_mapping` 存的是通话那份）。

**根因**：源字段反查被写成了**逐列回退**：

```js
Object.keys(own).forEach(...);           // 实体级
if (!src) Object.keys(effMap).forEach(...);   // 内置预设
if (!src) Object.keys(typeMap).forEach(...);  // 类型级 ← 错在这里
```

但「实体级 > 内置预设 > 类型级」是**整体**关系，不是逐列回退 —— 预设一旦存在，
类型级就**完全不该参与**。于是每列在预设里找不到时，就掉进了通话映射。

**修复**：抽出两个职责单一的函数，并把"整体三层"这件事写死在一处：

```js
// 三层整体取一层：上层存在就完全取代下层
function aeEffectiveMapping(own, presetMapping, typeMap) {
  if (own && Object.keys(own).length) return { map: own, scope: 'entity' };
  if (presetMapping && Object.keys(presetMapping).length) return { map: presetMapping, scope: 'preset' };
  return { map: typeMap || {}, scope: 'type' };
}
// 只在「生效映射」里反向找源字段（不再跨层回退）
function aeSourceOf(target, effMap) { ... }
```

面板现在严格按生效的那一层渲染。顺带：

- 「清除」按钮文案改为「清除自定义映射（恢复内置预设 / 继承类型级）」，确认框说明会回落到哪一层
- 表格上方的说明补上"三层是**整体**关系，命中上层就完全取代下层"

> 后端 `_merge_entity_mapping` 一直是**整体覆盖**（`if ec_fm ... elif preset_fm ...`），
> 采集结果本来就是对的；这次修的纯粹是**前端展示**。

> 版本号 → `4.15.2`。需重启 HA 生效（前端刷新页面即可，无需清缓存）。

### v4.15.1 修正内置预设的映射 + 修复保存时误报「数组路径不能为空」（2026-09-30）

**一、修复保存被误拦**

v4.14.0 把「数组路径 / 唯一键字段 / 对比最近条数 / 小数位数」下移到实体行后，
`saveAttrEdit()` 里仍在读已被删除的 `#attrEditArrayPath` —— `getElementById` 返回 `null`，
取到空串，于是**每次保存都误报**

```
数组路径不能为空（列表展开/混合/通讯模式必填）（共 2 项，已中止保存）
```

同处还有两个连带缺陷（都会导致保存异常）：

- `arrPath` / `keyField` / `compareLimit` / `decimalPlaces` 四个变量已不存在，但后面仍在
  用于计算类型级 diff → `ReferenceError`
- 通讯模式不再渲染类型级字段映射表，`mapping` 恒为空 → 原来那句
  `defUpdates.push(['field_mapping', ...])` 会**把类型级映射清空**（兜底默认值被抹掉）

现在：类型级的这四项**不再在编辑弹窗里改动**（保留库中原值继续作兜底）；
校验改为按「**实体级 → 内置预设 → 类型级**」三级判断，三者都取不到才报错，
错误信息会指明是哪个实体的哪一项。

**二、修正三条内置预设（按实际数据形状）**

| | `*_calls` | `*_sms` | `*_traffic` |
|---|---|---|---|
| 采集节点 | `通话流水清单` | `短信记录` | `上网会话清单` |
| 唯一键 | `call_time` | `datetime` | `datetime` |
| `channel` 默认值 | **`语音`** | `短信` | `流量` |

**通话**（13 项）：`phone_number→party_number`、`number_location→party_place`、
`call_time→time`、`location→location`、**`type→msg_type`**、**`call_type→call_type`**、
`duration→duration`、`fee→cost`、`location_coordinate→location_coordinate`、
`number_isp→party_isp`、`number_location_coordinate→party_coordinate`

> 两处与 v4.15.0 不同：`type`（呼叫/接听，方向）应进 `msg_type` 而不是 `call_type`；
> 源字段里的 `call_type`（国内通话/漫游）才是 `call_type` 列。

**短信**（9 项）：`phone_number→party_number`、`datetime→time`、`type→msg_type`、
`fee→cost`、`number_isp→party_isp`、`number_location_coordinate→party_coordinate`、
`number_location→party_place`

**流量**（7 项）：`datetime→time`、`duration_seconds→duration`、`fee→cost`、
`volume_mb→traffic_usage`、`business_type→traffic_type`

**三、`my_number` 从实体 ID 自动提取**

三条预设都会根据实体 ID 里的电话号码生成 `my_number` 固定值映射 ——
`sensor.17792405320_sms` → `=17792405320`。正则取末段 7~15 位连续数字
（`extract_phone()`），兼容手机号与带区号固话；提取不到就不生成该项。

实现上 `my_number` **不写进** `COMM_PRESETS` 常量（保持前后端可静态比对），
由 `preset_field_mapping(entity_id)` 运行时叠加。

> 版本号 → `4.15.1`。需重启 HA 生效。

### v4.15.0 通讯采集内置映射预设（按实体后缀开箱即用）（2026-09-30）

针对 `shaobo_pocket_carrier` 集成产生的三类实体，内置「采集节点 + 字段映射 + 唯一键」，
**无需任何配置**即可采集。按实体 ID **末段后缀**识别：

| 后缀 | 采集节点 | 唯一键 | 主要映射 |
|---|---|---|---|
| `*_calls` | **`通话流水清单`** | `call_time` | `phone_number→party_number`、`number_location→party_place`、`number_isp→party_isp`、`number_location_coordinate→party_coordinate`、`location→location`、`location_coordinate→location_coordinate`、`type→call_type`、`duration→duration`、`fee→cost`、`=通话→channel` |
| `*_sms` | **`短信记录`** | `datetime` | `phone_number→party_number`、`number_location→party_place`、`number_isp→party_isp`、`number_location_coordinate→party_coordinate`、`type→msg_type`、`fee→cost`、`=短信→channel` |
| `*_traffic` | **`上网会话清单`** | `datetime` | `volume_mb→traffic_usage`、`business_type→traffic_type`、`duration_seconds→duration`、`fee→cost`、`=流量→channel` |

三个实体里都有多个数组（`按天汇总` / `最近一条` / `排序选项` 等），
预设一律选**明细清单**那一个。`channel` 用**固定值**写法（`=通话` 等）。

**优先级**：`实体级配置` → `内置预设` → `类型级配置`

- 已手工配过的实体不受影响（实体级非空即优先）
- 新加的 `*_sms` / `*_traffic` / `*_calls` 实体默认就能采到数据
- 手工配过之后想退回预设：清掉该实体的实体级配置即可

**实现**：

- 新增 `comm_presets.py`：`detect_preset(entity_id)`（按**末段**后缀匹配，
  避免 `sensor.sms_gateway_temperature` 这类误命中）、`preset_summary()`（给前端）
- `_merge_entity_mapping(row, mode)` 增加 `mode` 参数，在实体级与类型级之间插入预设层；
  **仅通讯模式**生效（预设的映射项都是通讯固定列）
- `_async_attr_event` 的 `cfg` 组装同样插入预设层
- `AttrEntityMappingView` 的 GET 返回 `preset` 供前端展示
- 前端内置一份镜像（`COMM_PRESETS` + `commPresetOf()`），实体行的「字段映射」列
  显示 **内置：通话记录** 之类的绿色标识；点「编辑」时映射面板会**按预设预填**
  并提示来源（后缀），保存即固化为实体级配置

> 版本号 → `4.15.0`。需重启 HA 生效。

### v4.14.1 修正「唯一键字段」的候选来源与语义说明（2026-09-30）

**先明确语义**：`key_field` 填的是**源字段名**（**该实体采集节点下数组元素里的字段**，
如 `call_time` / `b_when`），**不是数据库列名**。因为去重时是拿它到数组元素里取值：

```python
key_target_col = field_mapping.get(key_field, key_field)   # 该源字段映射到哪一列
key_value = _extract_nested_value(element, key_field)      # 从数组元素里取值
```

**问题**：实体表格里这列的输入框绑定的是 `list="attrEditFieldList"` ——
那是**类型级**探测出来的扁平候选，于是 `sensor.x_sms` 这一行会给出 `call_time`
这类**别的实体的字段名**，必须手工敲。

**修复**：

- 每行挂一个**独立 datalist**（`#aeKeyList_<i>`），输入框绑定各自的
- `_aeLoadEntityFields()` 为每个实体**按它自己的采集节点**（行内下拉优先、否则库里的
  `array_path`）算出字段候选，存进 `ctx.entityFieldCands[entity_id]`
- `_aeUpdateDatalists()` 把候选填进各行的 datalist
- 某行的「采集节点」变化时（`aeOnEntityNodeChanged`，300ms 防抖）会重新探测并刷新候选
- 占位符与提示文案明确写出「源字段名（该节点下）」、**不是数据库列名**，
  以及填错时的兜底行为（自动回退到时间列并在日志提示，见 v4.9.1）

实测：`通话流水清单` / `短信清单` / `上网会话清单` 三个节点各返回各自的字段集，
互不混入；节点用错（对某实体不存在）时按既有规则退回顶层标量。

> 版本号 → `4.14.1`。需重启 HA 生效。

### v4.14.0 采集参数全部下移到实体级 + 编辑弹窗支持最大化（2026-09-30）

`key_field`（唯一键）、`compare_limit`（去重窗口）、`decimal_places`（小数位）
也支持**实体级覆盖**（与 `array_path` / `field_mapping` 同一套规则），
于是「采集参数」区只剩类型名与模式（只读展示），实体表格扩到 10 列：

```
实体 | 房间 | 采集方式 | 间隔(分钟) | 采集节点（数组路径） | 唯一键字段 | 对比最近条数 | 小数位数 | 字段映射 | 操作
```

- 三列输入框的 **placeholder 显示当前继承值**（如 `继承：call_time`），留空即继承类型级
- 数值项用哨兵值区分「未设置」：`compare_limit` 用 `≤0`、`decimal_places` 用 `< -1`
  （因为 `decimal_places` 的 `-1`（不限）与 `0~6` 都是合法值，哨兵取 `-2`）

**类型级「通讯字段映射」区已移除** —— 通讯模式一律走每个实体行的「编辑」按钮。
类型级 `field_mapping` 仍作为**回退默认值**保留在库里，但不再在界面上暴露；
非通讯模式（fields / list / multi）的类型级映射区保持不变。

**编辑弹窗支持最大化**：标题栏新增 `⛶ 最大化 / ⤡ 还原` 按钮，
最大化时铺满视口（`99vw × 96vh`），配合 `#attrEditBody` 既有的 `overflow:auto`
可正常滚动查看 10 列表格。

后端：`entity_configs` 新增 `key_field` / `compare_limit` / `decimal_places` 三列（含迁移）；
采集链路与 `AttrEntityMappingView` 同步支持读写这三项。

> 版本号 → `4.14.0`。需重启 HA 生效。

### v4.13.0 采集节点改为「每个实体各自指定」（2026-09-30）

`array_path`（要采集的数组节点）原先只是**类型级**配置，同一类型下的所有实体共用。
但同一类型的各实体数据形状往往不同 —— 例如 `rec` 类型下的三个实体：

| 实体 | 数组节点 |
|---|---|
| `sensor.xxx_traffic` | `data.traffic` |
| `sensor.xxx_sms` | `data.sms.list` |
| `sensor.xxx_calls` | `data.calls` |

现在 `array_path` 与 `field_mapping`、`field_types` 一样支持**实体级覆盖**
（`ENTITY_CONFIGS.array_path` 列，`RestoreEntity` 式的「实体级非空优先、否则回退类型级」）。

**实体表格新增「采集节点（数组路径）」列**，每行一个下拉：

```
实体 | 房间 | 采集方式 | 间隔(分钟) | 采集节点（数组路径） | 字段映射 | 操作
```

- 选项来自**该实体自己**属性树里的数组节点（带元素数；由 `_aeLoadEntityFields` 逐实体探测）
- 首项 `（继承类型级：data.records）` 表示沿用类型级；空 = 回退类型级
- 若当前值没探测到（实体不可用/手工填过），仍保留为一个 `（当前，未探测到）` 选项

**点该行「编辑」打开字段映射时**：面板标题会显示该实体的采集节点，
**源字段候选按该实体 + 该节点重新探测**（只列该节点下元素的字段），
关闭面板时还原为类型级候选。

**保存**：节点变化走 `entity_configs.array_path` 的 UPDATE；新增实体时先走
`EntityConfigView` 建行，再补一次 `AttrEntityMappingView` 设节点。

接口 `GET|POST /api/ha_data_store/attr_entity_mapping` 相应增加 `array_path` 字段
（GET 另返回 `type_array_path`；`scope` 也计入节点）。

> 版本号 → `4.13.0`。需重启 HA 生效。

### v4.12.1 采集节点与字段映射候选联动（2026-09-30）

v4.12.0 加的「节点选择面板」只是让你**能挑**节点，但挑完节点后，
下方「字段映射」的**源字段候选并没有跟着变** —— 这才是真正需要的能力。

**两处修正**：

**一、候选改为「节点优先」**（`_aeFieldCandidates`）

| 情况 | 修改前 | 修改后 |
|---|---|---|
| 已指定采集节点 | 该节点下字段 **+ 顶层标量属性**（混在一起） | **只**列该节点下元素的字段 |
| 未指定节点 | 顶层标量属性 | 不变 |

指定节点后候选里混着无关的顶层字段，映射时要在里面翻 —— 现在不会了。

**二、候选随节点变化「整体重建」+ 自动联动**

- `_aeLoadEntityFields()` 原来用 `if (ctx.fieldCands.indexOf(c) < 0) push(c)` **累加**候选，
  换节点后旧节点的字段仍然留在候选里；现在改为**重新构建** `ctx.fieldCands`
- 「数组路径」输入框加 `oninput` / `onchange` → `aeOnArrayPathChanged()`（350ms 防抖）
  → 重新探测实体属性 → 重建候选 → 刷新 datalist 与提示
- 新增 **「从实体属性选节点」下拉**（`#aeArrayPathSelect`）：直接列出实体属性树里的
  **所有数组节点**（带元素数），选中即填入并触发联动 —— 等价于创建流程的 `#attrArrayPath`
- 采集参数区下方新增**提示行**：显示当前节点下取到多少个字段、已并入候选多少个

**三处入口都会触发联动**：改输入框、用下拉选、点节点面板里的「设为数组路径」。

> 版本号 → `4.12.1`。需重启 HA 生效。

### v4.12.0 属性提取编辑界面新增「从实体属性选择节点」（2026-09-30）

**问题**：创建类型时（「步骤 1：加载实体状态」）可以用 `#attrArrayPath` 下拉从实体属性树里
挑数组节点，编辑已有类型时**没有这个入口** —— 只有输入框 + datalist 候选，看不到属性树的形状，
只能盲猜路径。通讯模式的 `array_path` / `extra_json_nodes` 尤其受影响。

**修复**：编辑弹窗的「采集参数」区新增 **🌳 从实体属性选择节点** 面板：

- 顶部选择要探测的实体（自动列出该类型下**启用中**的实体，多实体时可切换）
- **🔍 探测属性树** 调用既有 `/api/ha_data_store/entity_state`，按层级缩进渲染全部属性节点
- 每个节点显示 `type` 与规模：list 显示 `[N 个元素]`、dict 显示 `{N 个键}`

每个节点按类型给出可执行动作：

| 节点类型 | 可用动作 |
|---|---|
| `list` | **设为数组路径**（填入 `array_path`，当前值会标 `✔`） |
| `list` / `dict` | **加为 JSON 节点**（追加到 `extra_json_nodes`，已在列表的标 `✔`） |
| 任意 | **设为唯一键**（填入 `key_field`）、**复制路径** |

**实现**（`db_viewer.html`，纯前端，未改后端）：

- HTML 面板插在数组路径输入行之后（编辑弹窗内）
- 新增 10 个函数：`aeToggleNodePicker` / `aeCloseNodePicker` / `_aeFillNodePickerEntities` /
  `aeProbeNodes` / `_aeNodeDepth` / `aeRenderNodePicker` /
  `aePickNodeAsArrayPath` / `aePickNodeAsKeyField` / `aePickNodeAsJsonNode` / `aeCopyNodePath`
- 「加为 JSON 节点」复用既有的 `_aeNodeRowHtml(path)` 行模板，并先按 `input[data-role="node"]`
  现有值去重
- `_aeRender()` 末尾会**恢复面板展开状态**（重渲染会重建 DOM），并按最新输入重绘 `✔` 标记；
  `closeAttrEditModal()` 里重置 `aeNodePickerOpen` / `aeNodeTree`，避免残留上个类型的属性树

后端 `attribute_tree` 的节点结构（`entity_state` 返回，未改动）：
`{path, type, keys?, length?, value?, first_element?}`，其中 `type` 为
`"list"` / `"dict"` 或 Python 类型名（`str` / `int` / `NoneType` 等）。

> 版本号 → `4.12.0`。需重启 HA 生效。

### v4.11.0 属性提取编辑界面支持增删实体（多实体配置）（2026-09-30）

**问题**：编辑已有类型时**无法新增实体**。原因是前端「属性提取」编辑弹窗里
`_aeRender()` 只遍历从数据库读到的**已存在**实体，页面没有「添加」入口；
而保存函数 `saveAttrEdit()` 对没有 `_rowid` 的行直接 `return` 跳过 ——
**整条链路没有 INSERT 分支**（后端其实一直能 upsert，只是前端从不调用）。

**修复**：编辑弹窗的「实体采集参数」区改为始终渲染表格并支持增删：

| 入口 | 行为 |
|---|---|
| **➕ 添加实体** | 追加一行空行（沿用上一行的房间 / 采集方式 / 间隔），可直接输入 entity_id |
| **📋 批量添加** | 弹框粘贴多个 entity_id（逗号 / 换行 / 空格 / 中文逗号均可），自动去重 |
| **🗑 删除** | 待新增行直接移除；已有行标记为「待停用」（半透明，可点「↩ 恢复」取消） |

保存时：

- 新增实体走 `EntityConfigView`（`POST /api/ha_data_store/config`）的 **upsert**，
  传 `entity_id` + `attr_type` + `category=attribute` + 采集方式 / 间隔 / 房间，
  **无需传 `field_mapping`**（新实体默认继承类型级映射）
- 停用实体走 `enabled=0` **软删**（只影响该实体的本类型配置，历史数据保留，
  再次编辑并保存即可恢复）
- 新增前会用 `/api/ha_data_store/entity_state` 校验实体是否存在（与「更换实体」同样对待）
- 确认框会列出新增 / 停用的实体清单

**其它**：`_aeRender()` 开头新增 `_aeSyncEntityInputs()`，在重渲染前把表格里
未保存的输入收回内存，避免「添加 / 删除行」时丢失刚填的内容。

> 版本号 → `4.11.0`。需重启 HA 生效。

### v4.10.0 通讯表字段「自动填写」（号码归属地 / 运营商 / 坐标）（2026-09-30）

在 **API 工具 → 📞 通讯数据查询** 页面下方新增「📇 自动填写」区，用本地离线数据补全通讯表
中的空字段（**不联网**）：

| 字段 | 来源 |
|---|---|
| `party_place`（对方归属地） | `party_number` 查本地归属地库（省+市，省市同名只留一个） |
| `party_isp`（对方运营商） | `party_number` 查本地归属地库（标准化为 `中国移动` 等） |
| `party_coordinate`（对方坐标） | `party_place` 查本地城市坐标表（`"经度,纬度"`） |
| `location_coordinate`（我的坐标） | `location` 查本地城市坐标表 |

**数据文件**（放在集成目录的 `data/` 下，也可放在 `config/ha_data_store/` 覆盖）：

| 文件 | 内容 |
|---|---|
| `phone2region.zdb` | 手机号 / 固话归属地库（[ALI1416/phone2region](https://github.com/ALI1416/phone2region)，Apache-2.0） |
| `city_coordinates.json` | 420 个城市中心坐标（`{"coordinates": {"西安": [108.948, 34.2632], ...}}`） |

**页面操作**：

- **📇 自动填写**：只补空字段（默认），已有内容不覆盖，可反复执行（幂等）
- **🧪 预览**：只统计会补多少条，不写库
- **覆盖已有值**：勾选后连已有内容一并重算
- **最多处理 N 行**：`0` = 全部；大表可分批跑
- **🔄 刷新状态**：读取库版本 / 坐标表条数 / 待回填行数

**接口**：

```
GET  /api/ha_data_store/comm/backfill?type_name=
       → {success, region:{库状态}, coords:{坐标表状态}, table:{待回填统计}}
POST /api/ha_data_store/comm/backfill
       Body: {type_name?, dry_run?, only_empty?, limit?}
       → {success, scanned, updated, filled:{...}, no_region, no_coord, samples}
```

`type_name` 留空时沿用 `comm.resolve_comm_type_name` 的自动探测（见 v4.6.1）。

**实现**：新增 `phone_region.py`（归属地库解析与查询）与 `city_geo.py`（城市坐标匹配），
两者移植自同仓库的 `shaobo_pocket_carrier` 集成，逻辑保持一致；新增 `comm_backfill.py`
承载回填逻辑。

库格式（已实测确认，小端序）：`.zdb` 是 ZIP 容器，内层 `phone2region.db` 为
`[20字节头: CRC32+版本+记录区指针+二级索引指针+一级索引指针]` +
`[记录区: <长度1B><UTF-8 "省|市|邮编|区号|运营商">]` +
`[二级索引: 2736 × int32]` + `[一级索引: N × [号段低8位1B][记录区偏移4B]]`；
手机号查询 `key = 前7位 - 1300000`，`key>>8` 定位块、块内按 `key&0xFF` 顺序扫描。

> 版本号 → `4.10.0`。需重启 HA 生效。

### v4.9.1 修复：多实体各写各的映射时，去重键 `key_field` 不匹配导致整批跳过（2026-09-30）

**问题**：`key_field` 是**类型级**配置，而 `field_mapping` 可以被实体级覆盖（v4.8.1）。
多实体共用一张表、各写各的源字段名时（手机 A 用 `a_time`、手机 B 用 `b_when`，都映射到 `time`），
类型级的 `key_field`（`a_time`）对 B 而言不存在 → 取不到 key → **B 的数组元素整批被静默跳过，
一条都不写**，且日志无任何提示。

**并非**"因为 time 一致而互相覆盖"——去重与更新都以 `entity_id` 为条件隔离，
不同实体即使 `time` 完全相同也各写各行、互不干扰。真正的问题是"不匹配的实体什么都不写"。

**修复**（新增 `_resolve_collect_key(field_mapping, key_field)`）：

| 顺序 | 条件 | 结果 |
|---|---|---|
| 1 | `key_field` 在本实体映射的源字段中 | 直接用（不回退） |
| 2 | 本实体映射中有源字段映射到 `time` 列 | 用该源字段（comm 模式 `time` 必填，必命中） |
| 3 | 其它 | 退化为映射中的第一个目标列（保证是表中真实存在的列，避免取列异常） |

发生回退时写一条 warning 日志，指出实际使用的源字段与目标列，便于排查。第 1 种情况
（即绝大多数单实体或映射一致的场景）**行为完全不变**。

**顺带明确的两点既有行为**（未改动，仅说明）：

- **同一轮数组内**两条记录 `key` 相同时，两条都会插入（`lookup` 只在采集开始时从库中读取一次，
  不含本轮新插入的行）。同一秒的两条不同通话本就该是两行，这是对的。
- **跨轮次**、同一 `entity_id` + 同一 `key` 时按 key 去重：内容无变化不写，有变化则 UPDATE（不新增行）。

> 版本号 → `4.9.1`。需重启 HA 生效。

### v4.9.0 通讯表新增「流量 / 流量类型」字段（2026-09-30）

通讯固定字段由 16 个增至 **18 个**：

| 列名 | 类型 | 标签 | 说明 |
|---|---|---|---|
| `traffic_usage` | REAL | 流量(MB) | 数据流量，统一换算为 **MB** |
| `traffic_type` | TEXT | 流量类型 | 如 `移动数据` / `WLAN` / `5G` |

位置在 `cost`（金额）之后、`content`（消息内容）之前，避免破坏既有列序。

**自动升级**：字段定义改为 `COMM_FIELDS` 后，建表、补列（`_ensure_comm_columns`）与
启动时的全表升级（`_ensure_all_comm_tables`）自动包含新列——**已有通讯表重启 HA 后自动补列**，
历史数据保留（新列历史行为默认值 `0` / 空串）。

**流量单位自动换算**（`_parse_comm_traffic`，采集时对源字段值生效）：

| 源值 | 结果 | 说明 |
|---|---|---|
| `123` / `123.5` | 123 / 123.5 | 纯数字按 MB |
| `512MB` / `200kb` | 512 / 0.1953 | 支持 KB / MB / GB / TB / B |
| `1.5GB` / `2G` | 1536 / 2048 | 单字母写法也可（`K`/`M`/`G`/`T`） |
| `abc` / `1.5XB` | 0 | 无法识别按 0 |

单位大小写不敏感、允许空格，换算结果保留 4 位小数。

**查询接入**：`traffic_usage` / `traffic_type` 进入明细输出列（`_OUTPUT_COLUMNS`）、
排序白名单（`_SORT_COLUMNS`，可按 `sort=traffic_usage` 排序）、排行榜指标
（`by=traffic_usage` 按流量排序）与汇总（summary 返回 `traffic_usage` 合计）。

**前端**：`COMM_FIELD_DEFS`（通讯字段映射表）与 `COMM_SOURCE_ALIASES`（源字段自动预选别名）
同步新增两项，别名覆盖 `traffic` / `data_usage` / `flow` / `net_type` / `network_type` 等常见写法。

> 注意：`channel`（数据来源）的别名中已有 `data_type`，因此**未**把它作为 `traffic_type` 的别名，
> 避免同一源字段被两列争抢。若源数据的 `data_type` 实际表示流量类型，请手工把它映射到「流量类型」。

> 版本号 → `4.9.0`。需重启 HA 生效。

### v4.8.1 通话记录支持多实体采集到同一张表（每实体独立字段映射）（2026-09-30）

通讯采集（以及所有属性提取模式）此前**字段映射是「类型级」的**——同一个类型下的所有实体
共用一份映射。两台手机的通话记录 JSON 结构不同时，只能二选一。

现在支持**实体级映射**：每个实体可指定自己的「源字段 → 目标列」，写入的仍是**同一张表**
`attr_<类型名>`。

| 层级 | 存储 | 说明 |
|---|---|---|
| 类型级 | `attr_type_defs.field_mapping` | 该类型的**默认**映射 |
| 实体级 | `entity_configs.field_mapping` | **新增**；非空则覆盖类型级，留空 = 继承 |

**优先级**：实体级非空 → 用实体级；否则回退类型级。旧配置（只有类型级映射）行为**完全不变**。

**列结构**：各实体采集时各自补列，所以表是各实体映射的**并集**；某实体未映射的列在该行写入
时留默认值。两个不同的源字段名映射到同一个目标列是允许的（如 A 的 `a_time` 与 B 的 `b_when`
都映射到 `time`）。

前端：「属性提取 → 编辑」的**实体采集参数**表格新增「字段映射」列，显示
`继承类型级` / `自定义 N 项`，点行尾「编辑」展开该实体的映射表（目标列由类型决定：
通讯模式为 18 个固定字段，其它模式为类型级映射的目标列），填源字段或 `=固定值`。
保存/清除即时写入（`entity_configs.field_mapping`），下一轮采集生效。

接口：

```
GET  /api/ha_data_store/attr_entity_mapping?entity_id=&attr_type=
       → {scope: "entity"|"type", field_mapping, field_types, type_field_mapping, ...}
POST /api/ha_data_store/attr_entity_mapping
       Body: {entity_id, attr_type, field_mapping: {...}}
       · 空映射 = 清除实体级，回退继承
       · 通讯模式校验：目标列须在 COMM_COLUMNS 白名单内、必填列（time）必须映射
```

> 版本号 → `4.8.1`。需重启 HA 生效。

### v4.8.0 属性提取编辑时支持更换「被采集的实体」（2026-09-30）

**问题**：「属性提取」编辑已有采集配置时，「实体采集参数」表格里的**实体 ID 是只读文本**——
只能改房间 / 采集方式 / 间隔，无法更换被采集的实体。要换实体只能删掉重建配置。

**修复**：实体 ID 改为可编辑输入框。

| 行为 | 说明 |
|---|---|
| 直接编辑实体 ID | 保存时按新 ID 更新 `entity_configs`（主键为 `entity_id + attr_type`，等价于"换绑"） |
| 空值校验 | 实体 ID 不能为空 |
| 重复校验 | 同类型下不能出现重复实体 ID |
| **存在性预校验** | 保存前先探一次实体状态，不存在或不可用则拒绝保存并提示 |
| 确认框强化 | 明确列出「旧实体 → 新实体」，并提醒核对数组路径 / 唯一键 / 字段映射 |
| 友好报错 | 若目标实体已存在同类型配置（主键冲突），提示「请先删除它的旧配置」而不是抛原始 SQL 错误 |

**注意事项**（确认框里也提示了）：

- 旧数据**保留在原表中，不迁移**——新实体从下一轮采集开始写入，历史数据仍归原类型
- 若新实体的属性结构与原实体差异较大，需要一并核对「数组路径 / 唯一键字段 / 字段映射」，
  否则采集可能取不到数据（配置本身允许保存，不会报错）

> 版本号 → `4.8.0`。需重启 HA 生效。

### v4.7.9 修复：通讯表字段升级在「配置记录丢失」时失效（2026-09-29）

**问题**：v4.7.8 新增的 3 个字段没有出现在已有通讯表里。

**根因**：补列函数 `_ensure_comm_columns()` 只在 `_ensure_attr_table()` 内被调用，
而它要求 `attr_type_defs` 中**存在对应 type_name 的记录**。若采集配置记录已丢失
（但表和 10 万行数据仍在），就永远不会走到补列逻辑——表结构永久停在旧版本。
**与自定义表名无关**，自定义类型名本身是支持的。

**修复**：新增 `_ensure_all_comm_tables()`，在数据库初始化时（`_migrate_database()` 之后）
运行，用**两条互补途径**识别通讯表：

1. `attr_type_defs` 中 `mode=comm` 的 type_name
2. **结构特征**：`attr_*` 表中含 `my_number` / `party_number` / `time` 三列的

只要表还在就能升级，不受配置记录影响。非通讯表（电费、燃气等）不会被误改。

> 识别条件宽松但要求三列同时存在，避免误判。单表补列与建索引都幂等，可重复执行。

> 版本号 → `4.7.9`。需重启 HA 生效。

### v4.7.8 通讯表新增 3 个字段（2026-09-29）

通讯数据采集新增第 14~16 个固定字段：

| 列 | 类型 | 标签 | 必填 |
|---|---|---|---|
| `location_coordinate` | TEXT | 我的坐标 | 否 |
| `party_isp` | TEXT | 对方运营商 | 否 |
| `party_coordinate` | TEXT | 对方坐标 | 否 |

- 加入 `const.py` 的 `COMM_FIELDS`（追加在末尾），随之进入 `COMM_COLUMNS` /
  `COMM_COLUMN_TYPES` 白名单，采集配置的目标列白名单校验与前端映射表自动生效
- **已存在的表自动补列**：`_ensure_comm_columns()` 幂等，重启后保存一次采集配置
  （或等下一次采集触发）即执行 `ALTER TABLE ADD COLUMN`，**不需要重建表、不丢数据**
- `comm.py` 的 `_OUTPUT_COLUMNS` 同步加入，`type=records` 明细会返回这三列
- 前端「属性提取 → 通讯字段映射」新增三行，同样支持**选源字段**或**直接填固定值**；
  常见别名自动预选：
  - 我的坐标：`my_coordinate` / `my_coord` / `coordinate` / `coord` / `gps` / `geo` / `position` / `lonlat` / `lnglat` …
  - 对方运营商：`isp` / `carrier` / `operator` / `party_carrier` / `sim_isp` / `network` …
  - 对方坐标：`party_coord` / `party_position` / `party_gps` / `party_geo` / `peer_coordinate` …

字段**追加在末尾**而非插入中间——这样 `ALTER TABLE ADD COLUMN` 的列顺序与新建表一致。

> 版本号 → `4.7.8`。需重启 HA 生效。

### v4.7.7 分页不再设上限 + limit=0 表示不限（2026-09-29）

`limit` 现在**只设默认值，不设上限**：

| 传值 | 行为 |
|---|---|
| 不传 | 用默认条数（明细类 100） |
| `limit=0` / 负数 | **不限条数，一次返回全部** |
| 其它值 | 按该值返回，**不再被钳制** |

响应里的 `limit_max` 恒为 `null`，表示无上限。原先各查询类型的上限（明细 1000、
stats 5000、ranking 500、crosstab 200 等）全部移除。

```bash
# 一次拿全（502 条）
/api/ha_data_store/comm?type=onthisday&mode=detail&limit=0&key=xxx
#   → count: 502, truncated: false, limit: null, limit_max: null

# 传具体值也不再被钳制
/api/ha_data_store/comm?type=onthisday&mode=detail&limit=100000&key=xxx
#   → limit: 100000（原值保留）
```

**顺带修掉一个 bug**：`comm.py` 有自己的 `_get_int()`，仍是旧写法
（`params.get(key) or ""`），导致整数 `0` 被当成「未提供」而套用默认值——
所以 `limit=0` 在 `records` 模式下会静默变成 100。已与 `onthisday.py` 统一为显式判空。

> 版本号 → `4.7.7`。需重启 HA 生效。

### v4.7.6 分页参数回显 limit_max（2026-09-29）

**澄清一个常见误解**：`limit` 不是"强制值"，而是**默认值**——不传时取 100，
可以显式调大。此前没有任何地方告知上限是多少，只能靠试错。

```bash
# 不传 limit → 默认 100 条
/api/ha_data_store/comm?type=onthisday&mode=detail&key=xxx
#   → count: 100, total: 502, truncated: true, remaining: 402

# 传 limit=1000 → 一次拿全 502 条
/api/ha_data_store/comm?type=onthisday&mode=detail&limit=1000&key=xxx
#   → count: 502, truncated: false, remaining: 0
```

改动：

| 项 | 说明 |
|---|---|
| 响应新增 `limit_max` | 该查询类型的 `limit` 上限（明细类为 1000） |
| `comm._query_records` 补充 `truncated` / `remaining` | 上一版只给 `onthisday` 的 detail 加了这两个字段，`records` 漏了 |
| 常量提取 | `_DETAIL_LIMIT_DEFAULT/MAX`（onthisday）、`_RECORDS_LIMIT_DEFAULT/MAX`（comm），默认值与上限不再散落在代码里 |

各查询类型的 `limit` 默认值并不相同（明细 100、统计/趋势 1000、排行榜 20…），
上限也不同——以响应里的 `limit` / `limit_max` 为准。

> 版本号 → `4.7.6`。需重启 HA 生效。

### v4.7.5 明细分页新增 truncated / remaining 标记（2026-09-29）

排查「power 数据少了」时发现：数据一条没丢，是 **`limit` 分页截断**被误读成缺数据——
`total: 663`、`group_count: 351`，而 `limit=100` 只返回了最近 100 组。

原因还有一个放大因素：**低频指标在时间倒序下容易被高频指标挤出窗口**。温度湿度每批 12 条
（12 个房间），`power` 每批只有 1 条（「全屋」总表），100 行的窗口很快就被温湿占满。

本次改动：

| 字段 | 含义 |
|---|---|
| `truncated` | 是否还有未返回的行 |
| `remaining` | 未返回的行数 |

聚合与平铺两种模式都会给出；配合已有的 `count`（本页行数）/ `total`（采样总数）/
`group_count`（聚合后总行数），一眼就能看出是否被截断，不用数数。

**想看某个指标时用 `env_metric` 筛选**，别靠翻页：

```
/api/ha_data_store/onthisday?source=env&mode=detail&env_metric=power&limit=1000&key=xxx
```

（`limit` 上限 1000；本例 `group_count` 351，调大后一次可拿全。）

> 版本号 → `4.7.5`。需重启 HA 生效。

### v4.7.4 环境明细 API 默认返回「带指标类型」的格式（2026-09-29）

**问题**：环境明细的聚合格式之前只对传感器生效，API 默认仍是平铺——而平铺时所有指标的值
都叫 `value`，**看不出哪条是温度、哪条是湿度**：

```json
{"datetime": "...12:30:00", "name": "", "room": "次卧", "value": 24.8}
{"datetime": "...12:30:00", "name": "", "room": "次卧", "value": 72.0}
```

**修复**：

| 改动 | 说明 |
|---|---|
| `env_by_room` 默认值 `0` → **`1`** | env 源的明细**默认**按「房间 × 时间点」聚合，指标名即字段名 |
| 平铺模式（`env_by_room=0`）补 **`metric`** 字段 | 每行标明 `temperature` / `humidity` / `power` …，即使 `fields` 白名单也会附带 |

现在同一个请求返回：

```json
{"room": "全屋", "datetime": "2026-09-29 12:40:00", "power": 4.383}
{"room": "次卧", "datetime": "2026-09-29 12:30:00", "temperature": 24.8, "humidity": 72.0}
{"room": "厨房", "datetime": "2026-09-29 12:30:00", "temperature": 25.3, "humidity": 70.0}
```

平铺模式仍可用（`env_by_room=0`），只是每行多了 `metric` 字段：

```json
{"datetime": "...12:30:00", "room": "次卧", "value": 24.8, "metric": "temperature"}
```

注意：聚合模式下 `fields` / `drop_fields` **不适用**（列由数据动态决定），
以此前带 `fields=datetime,name,room,value` 的 URL 请求，会直接得到聚合结果而不报错。

> 版本号 → `4.7.4`。需重启 HA 生效。

### v4.7.3 环境汇总列出「存在但无数据」的指标（2026-09-29）

`summary.by_metric` 之前只包含**有数据**的指标，导致它的键少于 `tables`（例如有 6 张指标表，
汇总里只出现 3 个），看起来像"漏统计了另外几个指标"。

现在 **`by_metric` 的键与 `tables` 一一对应**，无数据的指标以占位形式列出：

```yaml
by_metric:
  temperature: {count: 144, avg_value: 23.7, max_value: 27.7, min_value: 18.8}
  humidity:    {count: 144, avg_value: 74.3708, max_value: 91, min_value: 54}
  power:       {count: 19, avg_value: 3.2239, max_value: 4.383, min_value: 1.801}
  pm25:        {count: 0, avg_value: null, max_value: null, min_value: null}   # 无数据
  co2:         {count: 0, avg_value: null, max_value: null, min_value: null}
  sensor:      {count: 0, avg_value: null, max_value: null, min_value: null}
```

聚合值用 `null` 而非 `0`——「没有采样」和「采样值恰好是 0」是两回事（温度 0℃ 是有效读数）。

按年的 `years[].by_metric` 仍只列该年有数据的指标（年份维度补空无意义）。

> 若某个指标一直是 `count: 0`，说明该表在当前筛选（月日相同）下确实没有数据。
> 可用 `SELECT COUNT(*), MIN(datetime), MAX(datetime) FROM env_pm25;` 确认表里到底有没有数据。

> 版本号 → `4.7.3`。需重启 HA 生效。

### v4.7.2 修复：API 未读取「通讯数据表类型名」设置（2026-09-29）

**问题**：设置了 `text.ha_data_store_comm_type_name` 后，传感器能正确查到通讯表，但通讯查询 API
（`/api/ha_data_store/comm`）仍报「未找到通讯数据表 attr_comm_records」。

**根因**：设置实体只在**传感器**路径被读取，两条 API 路径都漏了——`CommApiView` 只从请求参数取
`type_name`（前端已不再发送该参数，于是恒为空），`OnThisDayView` 更是完全没处理。它们只能靠
`attr_type_defs` 自动探测，而库里若没有 `mode=comm` 的登记记录，就会回退到默认名。

**修复**：

| 改动 | 说明 |
|---|---|
| 新增 `comm.read_comm_type_name_setting(hass)` | 统一读取设置实体（须在事件循环线程调用），空值 / `unknown` / `auto` 等一律返回空串 |
| `CommApiView._handle` | 参数为空时读取设置实体，再交给 `run_comm_query` 做自动探测 |
| `OnThisDayView._handle` | 同上（此前完全没有这一步） |
| `sensor.py` | `_read_comm_type_name()` 改为调用同一函数，消除重复实现 |
| `run_comm_query` | 返回值新增 `type_name`（**实际生效值**，含自动探测结果）；`CommApiView` 不再回显参数原值 |

三条路径现在共用同一套解析逻辑，口径不可能再漂移。

> 版本号 → `4.7.2`。需重启 HA 生效。

### v4.7.1 环境汇总也按指标分组（2026-09-29）

上一版只改了明细，`summary` / `years` 仍在**跨指标混算**：温度 25、湿度 58、CO₂ 800
一起求平均得到 `avg_value: 46.9853`、`max_value: 91`（那是 CO₂ 的值）、`min_value: 0`——
这三个数字不代表任何东西。现在环境汇总按指标分组：

```yaml
summary:
  count: 535                    # 采样总数（跨指标求和仍有意义：采了多少条）
  by_metric:
    temperature: {count: 90, avg_value: 24.5, max_value: 26.1, min_value: 19.8}
    humidity:    {count: 90, avg_value: 57.2, max_value: 68.0, min_value: 45.0}
    pm25:        {count: 90, avg_value: 35.1, max_value: 52.0, min_value: 18.0}
years:
  - {year: '2026', count: 400, by_metric: {temperature: {...}, humidity: {...}, ...}}
```

- 顶层只保留 `count`；`avg_value` / `max_value` / `min_value` 只出现在各指标内部
- 无论明细是否聚合（`env_by_room`），汇总都按指标分组
- **仅影响 env 源**：comm / device 各表量纲一致，`summary` / `years` 结构完全不变

> 版本号 → `4.7.1`。需重启 HA 生效。

### v4.7.0 环境明细改为「一个房间多种数据」（2026-09-29）

环境数据分布在 6 张指标表（`env_temperature` / `env_humidity` / `env_pm25` / `env_co2` /
`env_power` / `env_sensor`），逐条平铺时不同指标的值混在同一个 `value` 列里，
**看不出哪个是温度、哪个是湿度**。现在传感器的 `env.detail` 改为按「房间 × 时间点」聚合：

```yaml
env:
  count: 535          # 采样总数（口径不变）
  group_count: 54     # 聚合后行数
  room_count: 10
  rooms: [主卧, 次卧, 客厅, ...]
  metric_names: [temperature, humidity, pm25, co2]
  detail:
    - {room: 客厅, datetime: '2026-09-29 10:00:00', temperature: 25.0, humidity: 58.0, pm25: 35.0}
```

| 参数 | 说明 |
|---|---|
| `env_by_room` | **仅 env 源**，`1` = 聚合（默认 `0` = 平铺，保持向后兼容） |
| `room_bucket` | 时间聚合精度（分钟，默认 `1`）。同一房间同一分钟内的采样取平均，故各指标表时间戳差几秒也能对齐到同一行；`0` = 精确到秒 |

顺带修掉 `_get_int()` 的一个 bug：

```python
# 修复前：整数 0 是 falsy，会被当成「未提供」而套用默认值
val = int(str(params.get(key, "") or "").strip() or default)
```

这导致 Python 侧传 `room_bucket=0`（精确到秒）时会被静默改成 `1`。URL 参数是字符串不受影响，
但语义上确实是错的，已改为显式判空。

> 版本号 → `4.7.0`。需重启 HA 生效。

### v4.6.3 历史今日传感器精简明细字段（2026-09-29）

传感器三个节点的 `detail`（逐条明细）字段按数据源裁剪，减少状态属性体积：

| 数据源 | 排序字段 | 明细列 |
|---|---|---|
| `comm` | `time` | 剔除 `id` / `datetime` / `extra_json` / `name` / `room` / `updated_at`（6 个通用元数据列），保留全部通讯业务字段（含 `entity_id`） |
| `device` | `on_time` | 不裁剪 |
| `env` | `datetime` | **只保留** `datetime` / `room` / `value` |

新增查询参数 `drop_fields`（黑名单，逗号分隔）——通讯源用它，因此**将来表里新增列会自动保留**；
环境源用 `fields`（白名单）。两者同时给出时 `fields` 优先。
只影响 `detail`，`summary` / `years` 等汇总统计不受影响。

> 关于环境数据：若明细只出现今年，通常是环境表里**确实只有今年**的数据
> （历年的 `MM-DD` 不存在），而非筛选失效。可用
> `SELECT SUBSTR(datetime,6,5) AS d, COUNT(*) FROM env_temperature GROUP BY d` 自查。

> 版本号 → `4.6.3`。需重启 HA 生效。

### v4.6.2 通讯 API 工具不再暴露「类型名」参数（2026-09-29）

既然类型名已由设置实体与自动探测决定，**DB 浏览器「API 工具」中的通讯查询参数区已移除
「类型名 type_name」输入项**，生成的 URL 也不带该参数——共 7 处（明细 / 日期 / 统计 / 历史今日 /
交叉汇总 / 周期对比 / 联系人档案的公共参数，以及历史今日设备环境的 `comm` 数据源参数）。

提示文案同步说明：数据表类型名**自动探测**（取 `attr_type_defs` 中 `mode=comm` 且数据表已存在者），
如需明确指定请设置 HA 实体 `text.ha_data_store_comm_type_name`。

**后端行为不变**：`type_name` 参数仍然保留（向后兼容），已有显式传参的调用与自动化不受影响；
只是前端不再引导用户填写。

> 版本号 → `4.6.2`。需重启 HA 生效。

### v4.6.1 通讯表类型名自动探测 + 新增设置实体（2026-09-29）

之前历史今日（及通讯查询 API）在 `type_name` 未指定时**写死**用默认名 `comm_records`，
于是在「属性提取」里用了自定义类型名的用户会看到「未找到通讯数据表 attr_comm_records」。
现在改为按以下顺序解析：

| 优先级 | 来源 |
|---|---|
| 1 | 显式 `type_name` 参数 |
| 2 | **新增** HA 实体 `text.ha_data_store_comm_type_name`（留空 = 自动） |
| 3 | **自动探测**：`attr_type_defs` 中 `mode='comm'` 且数据表已存在的类型名（按名称排序取首个） |
| 4 | `attr_type_defs` 中 `mode='comm'` 的类型名（表尚未建） |
| 5 | 默认 `comm_records` |

**大多数人不需要做任何事**——第 3 步会自动找到你在「属性提取」里配置的通讯表。设置实体只在
「同一库里有多个通讯表、需要明确指定」时才需要。

新增实体：

| 实体 | 说明 |
|---|---|
| `text.ha_data_store_comm_type_name` | 通讯数据表类型名（`attr_<类型名>`）；留空 = 自动探测。校验：不含空白、≤50 字符。`RestoreEntity` 跨重启保持 |

生效范围：历史今日（`comm` 数据源）、历史今日传感器、通讯查询 API（`/api/ha_data_store/comm`）
的默认类型名。**该实体变化时传感器立即刷新**（与时间范围设置实体一同监听）。

其余改动：报错信息会列出 `attr_type_defs` 中已登记的通讯类型名并提示可用参数 / 实体；
传感器属性新增 `comm_type_name`（回显实际生效的类型名，含自动探测结果）；
历史今日响应体在 `comm` 源下新增 `type_name` 字段。

> 版本号 → `4.6.1`。需重启 HA 生效。

### v4.6.0 历史今日新增「排除实体」配置（2026-09-29）

「系统配置」新增子选项卡 **📜 历史今日**，用于维护「排除实体」清单：被排除的 `entity_id`
不参与历史今日的**任何数据源、任何 mode** 统计，**API 与传感器同时生效**。

| 项目 | 说明 |
|---|---|
| 存储 | `api_settings.today_in_history_exclude_entities`（JSON 数组，无需新建表） |
| 页面 | 系统配置 → 📜 历史今日（三种添加方式：手动输入 / chips 删除 / 候选列表搜索添加），角标显示 `-N` |
| 接口 | `GET\|POST /api/ha_data_store/onthisday/exclude`（读写）、`GET /api/ha_data_store/onthisday/entities`（候选实体） |
| 生效范围 | `comm` / `device` / `env` 三类数据源的全部 mode（stats / detail / ranking / crosstab） |
| 临时排除 | 查询参数 `exclude_entities`（多值），与设置取并集；与 `entity_ids` 叠加时取交集 |
| 回显 | 响应体与传感器属性均含 `exclude_entities` / `exclude_count` |

细节：用 `IFNULL("entity_id", '') NOT IN (...)` 过滤，保证 `entity_id` 为空的历史记录
**不会被误排除**（SQLite 中 `NULL NOT IN (...)` 结果为 NULL）。

顺带把 `api_settings` 的列表读写抽为通用模块 `app_settings.py`
（`get_list` / `set_list`，兼容 JSON 数组与逗号 / 换行 / 全角逗号纯文本），
`recent_devices` 的排除项读写改为调用它——两处共用同一套容错逻辑，行为不变。

> 版本号 → `4.6.0`。需重启 HA 生效。

### v4.5.2 历史今日实体改为每整点刷新（2026-09-29）

`sensor.ha_data_store_today_in_history` 的定时刷新由「每 5 分钟」改为**每整点刷新一次**
（`async_track_time_change(..., minute=0, second=0)`，即每小时 0 分 0 秒触发）。
同时显式声明 `should_poll = False`，不再参与 HA 默认轮询——刷新完全由三种方式驱动：

1. **每整点**定时刷新
2. 设置实体（时间范围）变化时**立即**刷新
3. HA 启动后延迟 5 秒首次刷新

> 注意：`now,60` 这类含 `now` 的设置，窗口会随当前时刻滑动。整点刷新意味着窗口最久滞后 1 小时，
> 实时性要求高时建议改用固定时刻写法（如 `09,60`）并在需要时手动触发。

> 版本号 → `4.5.2`。需重启 HA 生效。

### v4.5.1 历史今日时间范围改为「时间,前后分钟」写法（2026-09-29）

设置实体 `text.ha_data_store_today_in_history_set` 的取值改为 **`<时间>,<前后分钟>`**：

| 写法 | 含义 |
|---|---|
| `01,80` | 01:00 前后 80 分钟 |
| `now,60` | 此刻前后 60 分钟 |
| `09:02,30` | 09:02 前后 30 分钟 |
| 留空 | **全部数据**（不限定时间范围） |

等价于接口的 `at=<时间>&window=<分钟>`。解析规则抽到 `onthisday.parse_window_setting()`，
text 实体的格式校验与传感器的读取**共用同一个函数**，因此「能不能写」和「读出来是什么」必然一致；
传感器另有 `range` / `at` / `window` 三个属性回显当前口径。

格式校验：必须两段、时间须为 `now` 或 `HH[:MM]`（0~23 时 / 0~59 分）、分钟须为非负整数且不超过 720。
`unknown` / `unavailable` / `-` 与留空等价，按「全部数据」处理。

设置实体的 `entity_id` 未变（仍是 `text.ha_data_store_today_in_history_set`），
旧值（`0`~`23` 的纯数字）现在都无法解析，会按「全部数据」处理——重新设置一次即可。

**联动**：设置实体发生变化时，立即重算传感器的 `comm` / `device` / `env` 三个节点
（无需等下一次定时刷新），并在日志中记录新值。

> 版本号 → `4.5.1`。需重启 HA 生效。

### v4.5.0 新增「历史今日」传感器与时间范围设置实体（2026-09-29）

**新增两个实体**：

- `sensor.ha_data_store_today_in_history` —— 状态为三类数据的记录总数，
  属性含 `comm` / `device` / `env` 三个节点（各含 `count` / `summary` / `years` / `detail`），
  5 分钟自动刷新
- `text.ha_data_store_today_in_history_set` —— 时间范围设置（0~23）：`0` = 全天，
  `N` = 从当前整点起最后 N 小时；写入后传感器立即刷新，`RestoreEntity` 跨重启保持

两者都**复用 `onthisday.py` 的查询实现**，与 API 口径完全一致；某类表不存在时该节点只返回
`error`，不影响其它两类。

**顺带修复一个已存在的问题**：`onthisday` 里 comm 数据源会把 `date` 透传给
`comm._build_filters`，而那里 `date` 表示"指定单日"——于是「历史今日」被悄悄收窄成一天。
以前没暴露是因为测试与前端默认都用 `09-29` 这种短格式（解析不出日期，恰好绕过了）。
现在 comm 数据源的业务过滤会先剔除 `date` / `month` / `year` / `start` / `end` / `period`，
统一由「历史今日」自己的基准日与年份范围参数负责。

> 版本号 → `4.5.0`。需重启 HA 生效。

### v4.4.1 修复环境数据源参数冲突与参数区串味（2026-09-29）

**修复 1：`metric` 参数冲突**。`metric` 在环境数据源里表示「环境指标」（`temperature`…），
在交叉汇总里表示「测度」（`count`…）。参数区把 crosstab 的 `metric=count` 也拼进了 URL，
后端于是拿 `count` 去拼表名 `env_count`，报「未找到任何环境数据表」。

- 环境指标改用 **`env_metric`**（别名 `env_metrics`）
- 为兼容，`metric` 取值确实属于环境指标名时仍然生效；取 `count` 之类的测度值会被忽略
- `comm` 的委托入口同样适用

**修复 2：参数区按模式裁剪**。此前参数区一次性渲染了所有模式的字段，导致 URL 里同名字段
重复出现——例如 `limit=1000&…&limit=100&…&limit=20&…&limit=30`，后端只取最后一个，
用户设的明细条数被交叉汇总的 `limit` 覆盖。现在只渲染当前模式的字段，
切换「输出模式」会重建参数区（`onOtdModeChange`）。

**修复 3：交叉汇总的合计语义**。`grand_total` / `row_metric` / `col_metric` 原先一律求和，
对 `max_value` / `min_value` 这类极值指标是错的（"各格最大值之和"没有意义）。
现在按指标语义合并：极值类取极值，其余求和。

> 版本号 → `4.4.1`。需重启 HA 生效。

### v4.4.0 「历史今日」抽为独立模块，支持设备与环境（2026-09-29）

新增 `onthisday.py`（独立模块），把「历史今日」从通讯专用扩展为**通用查询**：

| source | 数据表 | 时间列 | 指标 |
|---|---|---|---|
| `comm` | `attr_<type_name>` | `time` | 条数 / 通话时长 / 金额 |
| `device` | `device_history` | `on_time` | 开关次数 / 运行时长 / 用电 / 平均时长 |
| `env` | `env_<metric>` | `datetime` | 采样数 / 平均值 / 最大值 / 最小值 |

新接口 `GET|POST /api/ha_data_store/onthisday?source=comm|device|env`，
模式沿用 `stats` / `detail` / `ranking` / `crosstab`，参数沿用基准日、年份范围、时刻窗口
（`at` / `align` / `hours` / `minutes` / `hour`）与明细分页 / 排序 / `fields` 字段点选。

实现要点：

- 用「数据源描述」抹平三者差异：`time_col`（`time` / `on_time` / `datetime`）、
  `dims`、`metrics`（`count` / `sum` / `avg` / `max` / `min` 五种聚合）各自声明
- 支持按原生列聚合，避免把 `duration` 和 `energy_consumed` 硬编码进表结构判断
- `env` 不指定 `metric` 时自动合并所有存在的 `env_*` 表，均值用采样数**加权**、
  极值取极值，不会出现"把两个指标的平均值再平均"的错误
- 明细跨表合并后统一排序分页；`fields` 按各表实际列过滤

通讯侧：`/comm?type=onthisday` 保留为兼容入口，内部委托到新模块（固定 `source=comm`），
`mode=records` / `mode=parties` 自动映射为 `detail` / `ranking`；`comm.py` 内原历史今日实现
已移除（-231 行），`_query_crosstab` 因独立 `type=crosstab` 仍在用而保留。

「API 工具 → 查询类型」新增分组「📜 历史今日 · 设备 / 环境」（8 个入口）。

> 版本号 → `4.4.0`。需重启 HA 生效。

### v4.3.9 修复「历史今日」模式未随 URL 发送 + 明细支持点选返回字段（2026-09-29）

**修复**：API 工具里选择「历年今日 · 明细 / 详细明细 / 排行榜 …」后，生成的 URL **缺少 `mode`
参数**——`mode` 来自下拉选项的第二段（`comm:onthisday:detail`），但生成 URL 时只用于显示提示，
没有拼进查询串，导致后端一律走默认的 `stats`，看起来「只有汇总、没有明细」。
现在参数区新增「输出模式 mode」下拉（默认即所选入口的模式，切换会重建参数区），
`mode` 会正常随 URL 发送。

**新增**：`fields` 参数——只返回指定列，明细类查询（`records` / `onthisday&mode=detail` /
`longest` / `contact`）都生效。API 工具中对应「返回字段 fields」**点选**控件（复选框），
全不勾选 = 返回全部字段。

```
?type=onthisday&mode=detail&fields=time,party_number,party_name,duration
?type=records&fields=time,party_name,content&limit=50
```

字段名大小写不敏感、自动去重、非法列名被忽略（不会拼进 SQL）。

> 版本号 → `4.3.9`。需重启 HA 生效。

### v4.3.8 「历史今日 · detail」改为逐条明细（2026-09-29）

修正 `mode=detail` 的语义：它应当返回**每一条历史今日的数据**，而不是按年嵌套的汇总档案。

现在 `rows` 是**跨年份的扁平明细列表**（每条含完整字段），并附带：

- `total`：匹配总数（不受分页影响）
- `year_summaries` + `years`：各年汇总，便于做年度对比
- `summary`：总体汇总（条数 / 时长 / 金额 / 联系人数 / 活跃天数 / 首末时间）

参数相应调整为：`limit` / `offset`（分页）、`sort` / `order`（默认 `time desc`）、
`content_len`（截断）、`with_years=0`（关闭年度汇总）、`years_limit`（最多年数），
以及原有的 `at` / `align` / `hours` / `minutes` / `hour`（限定时刻）。
原先的 `record_limit` / `party_limit`（按年嵌套用）已移除。

「📜 历史上的今日」入口更名为「历年今日 · 详细明细（逐条记录 + 各年汇总对比）」。

> 版本号 → `4.3.8`。需重启 HA 生效。

### v4.3.7 「历史上的今日」新增详细档案（detail）（2026-09-29）

新增 `mode=detail`：按年组织，**每年一条**，包含该年的汇总（条数 / 时长 / 金额 / 联系人数 /
首末时间）、联系人 Top N、时段分布与明细。一次调用就能看到「历史上每年的今天都发生了什么」，
不必再分别调 stats / parties / records 再手动按年对齐。

专属参数：`limit`（最多年数，默认 10）、`record_limit`（每年明细条数，默认 20，0=不返回）、
`party_limit`（每年联系人数，默认 10）、`content_len`（明细截断，默认 80）；
也可用 `at` / `align` / `hours` / `minutes` / `hour` 把范围收窄到某时刻，
并叠加任意通用过滤（如只与某人有关）。

「📜 历史上的今日」分组新增第 6 个入口，参数区会按 `detail` 渲染专属字段。

> 版本号 → `4.3.7`。需重启 HA 生效（后端新增模式）。

### v4.3.6 修复「历史上的今日」缺 type_name 参数（2026-09-29）

「历史今日」参数区漏了 `type_name`（类型名），当数据表不用默认的 `comm_records` 时无法指定
目标表。已在公共参数组中补上，5 个模式（汇总 / 排行榜 / 联系人汇总 / 明细 / 交叉汇总）全部生效。
其余查询类型原本已提供该参数。

> 仅涉及 `db_viewer.html`，刷新浏览器即可生效（无需重启 HA）。

### v4.3.5 通讯查询新增 5 个分析接口 + 「此刻」时间粒度（2026-09-29）

新增 5 个查询类型：

- **`compare`（周期对比）**：当前周期 vs 上一周期（环比）vs 去年同期（同比），返回差值与增长率。
  `period=day|week|month|quarter|year`，`compare=prev|yoy|both`。
- **`contact`（联系人档案）**：给 `party_number` 或 `party_name`，一次拿到总量、首末通讯、
  「多久没联系」、平均联系间隔、按小时 / 星期 / 月份 / 来源分布、联系最多的日子与最近明细。
- **`longest`（单次 Top N）**：按 `by=duration|cost` 取单条记录前 N 条。
- **`quality`（数据质量）**：时间为空 / 格式异常 / 时长与金额异常 / 内容为空 / 图片缺路径，
  以及疑似重复（同 时间+号码+内容）与时间覆盖，附 `ok` 判定。
- **`meta`（数据概览）**：表结构、总量、时间跨度，以及实际存在的渠道 / 消息类型 / 呼叫类型 /
  归属地 / 地点 / 年份 / 月份清单。

「历史今日」的**此刻查询**新增时间粒度：`align`（`hour` 整点 / `30` / `15` / `5` / `min`）
决定起点对齐，`hours` / `minutes` 决定跨度。于是「现在 09:02 查 09:00~10:00」写作
`?type=onthisday&mode=records&at=now&align=hour`；「最近 2 小时」写作 `&align=hour&hours=2`；
原有 `window`（以 `at` 为中心 ± N 分钟）保持不变。

顺带修复与增强：

- 修复 `crosstab` 在「无任何过滤 + 指定列维度」时拼接出 `FROM tbl AND (...)` 的 SQL 语法错误
  （新增 `_and_sql()` 统一处理有无 `WHERE` 两种情形）
- 通用过滤新增单数别名：`party_number` / `party_name` / `my_number` 现在等价于对应的多值参数，
  所有查询类型统一支持
- `invalid_time` 检查从「只判长度」升级为「字段越界也判」（如 `2026-13-01`）

「API 工具 → 查询类型 → 📞 通讯数据查询」新增 5 个入口（共 25 个通讯查询选项）。

> 版本号 → `4.3.5`。需重启 HA 生效。

### v4.3.4 通讯查询新增「历史上的今日」与「交叉汇总」（2026-09-29）

新增两个查询类型：

- **`onthisday`（历史上的今日）**：按「月日相同」筛选历年同一天的记录，`mode` 可选
  `stats`（按年 / 年月 / 年月日汇总）、`ranking`（排行榜）、`parties`（对方号码 / 姓名汇总）、
  `records`（明细，`at=now` 看「此刻」）、`crosstab`（交叉汇总）。用 `date` 指定基准日
  （`09-29` 或 `2026-09-29`），`years` / `min_year` / `max_year` / `exclude_current` 限定年份，
  `at` / `window` / `hour` 限定时刻。
- **`crosstab`（交叉汇总）**：行维度 × 列维度 的度量矩阵，`rows=msg_type&cols=party_name&metric=duration`，
  可用维度含 `msg_type` / `location` / `party_place` / `call_type` / `party_name` / `party_number` /
  `channel` / `my_number`；`cols` 留空则只输出各行合计。

「API 工具 → 查询类型」新增独立分组「📜 历史上的今日」（5 个入口），并在「📞 通讯数据查询」中
加入统计分析与交叉汇总入口。

> 版本号 → `4.3.4`。需重启 HA 生效。

### v4.3.3 通讯查询新增「统计分析」（2026-09-29）

通讯查询新增 `stats` 类型：按 年 / 季度 / 月 / 周 / 日 / 小时 / 星期 分组汇总，每桶返回
条数、时长、金额、去重联系人数、活跃天数、平均时长，并附合计行（`total`）与可选每桶均值
（`avg_per_bucket`）。时间范围留空即「全部数据」，填 `year` / `month` 即「指定年 / 指定年月」，
再叠加 `party_numbers` / `party_names`（支持多值）就是「指定号码 / 姓名」的汇总。
`sort=value` 配合 `by=count|duration|cost` 可按指标取 Top N 时间桶；`fill=1` 补全空缺桶。

「API 工具 → 查询类型 → 📞 通讯数据查询」新增 6 个统计入口（全部数据按年 / 按年月、
指定年 → 按月、指定年月 → 按日、指定号码姓名、自定义），选中后按类型动态渲染参数区。

> 版本号 → `4.3.3`。需重启 HA 生效。

### v4.3.2 通讯表新增「图片路径」字段（2026-09-28）

通讯数据采集新增第 13 个固定字段 `image_path`（TEXT，非必填），用于存储图片消息的文件路径 / URL。
属性提取的通讯字段映射表多出一行「图片路径」，同样支持选源字段或直接填固定值（常见别名如
`image` / `img` / `pic` / `photo` / `file_path` 会自动预选）；`type=records` 查询会返回该列。
**表结构自动升级**：重启后保存一次采集配置（或等下一次采集触发）即自动 `ALTER TABLE ADD COLUMN`，
不需要重建表、不丢数据。

> 版本号 → `4.3.2`。需重启 HA 生效（后端字段定义变更）。

### v4.3.1 数据库浏览器新增「清空表」（2026-09-27）

数据浏览页工具栏新增 **🧹 清空表**：删除当前表的全部数据，并让**自增 ID 从 1 重新开始**
（等价于 `TRUNCATE TABLE`）。需输入**管理员密码**并二次确认；核心配置表与 `sqlite_*`
系统表禁止清空。清空后如需回收磁盘空间，可再点「🗜 压缩」。
接口：`POST /api/ha_data_store/clear_table`（body `{table, admin_password, vacuum?}`）。

> 版本号 → `4.3.1`。需重启 HA 生效（后端改动）。

### v4.3.0 通讯数据模块 + 数据导入 / 导出（2026-09-27）

> 本次新增两个模块：**通讯数据**（属性提取新增 `comm` 采集模式 + 独立查询 API）与
> **数据导入 / 导出**（CSV · JSON）。完整记录见 [`docs/CHANGELOG.md`](docs/CHANGELOG.md)。

**一、通讯数据**：「系统配置 → 📊 属性提取」新增第四种模式 **通讯数据采集**（`mode=comm`），
把手机端同步过来的通讯记录按数组展开写入 **13 个固定字段**（`my_number` / `party_number` /
`party_place` / `party_name` / `time` / `location` / `msg_type` / `channel` / `call_type` /
`duration`(秒) / `cost`(元) / `content` / `image_path`），列名与类型由系统固定、自动建好查询索引；
每个目标列既可以选源字段，也可以**直接填固定值**；`2026/9/1 17:24` 自动规范化为
`2026-09-01 17:24:00`，`3分53秒` 等中文时长自动换算为秒。新增 `comm.py` 提供 **8 类查询**
（明细 / 日期 / 排行 / 趋势 / 汇总 / 联系人 / 地点 / 时段），支持多号码、多姓名、多地点、
时间段、关键词、时长与金额区间过滤；API 工具的「查询类型」新增「📞 通讯数据查询」分组。

**二、数据导入 / 导出**：新增 `data_import.py` 与数据浏览页「📥 导入数据」面板，
支持 CSV / JSON 文件或直接粘贴文本导入**任意数据表**：字段映射 + 固定值、
**追加 / Upsert**（多列唯一键）、自动建表与补列、🧪 试运行、分批进度与错误明细；
基础值转换（`¥1,234.5` → `1234.5`、`2026/9/1` → `2026-09-01`）。
另可导出当前表数据或目标表空模板为 CSV（UTF-8 BOM，Excel 直接打开不乱码）。

**三、修复**：属性提取数组展开模式的去重窗口原先会**全表加载**（通讯级数据量下会拖死采集），
改为按配置窗口查询并自动放大到 2 倍本次条数（对既有 list / multi 模式同样生效）；
`compare_limit` 等采集参数在类型已存在时**不会写库**（改了不生效），通讯模式已放开；
前端「源列 / 固定值」的判定逻辑统一为公共函数，通讯采集与数据导入行为保持一致。

> 版本号 → `4.3.0`。需重启 HA 生效（涉及 Python 改动）。

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

