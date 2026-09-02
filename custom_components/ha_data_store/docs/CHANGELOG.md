# 更新日志

## 2026-09-02 — v3.1.0 新增系统资源占用传感器 + 系统/HA 基本信息采集

### 📊 新增 3 个系统资源占用传感器（主值=使用率百分比，每 30 秒刷新）
- **`system_resources.py`（新建）**：集中系统资源采集逻辑，提供 `CpuUsageSensor` / `MemoryUsageSensor` / `DiskUsageSensor` 三个传感器类，以及 `collect_system_info` / `collect_usage` 采集函数。
- **固定实体 ID**（`sensor.py` `async_setup_entry` 内通过 registry 强制固定并兼容改名）：
  - `sensor.ha_data_store_cpu_usage`：CPU 占用率（%）
  - `sensor.ha_data_store_memory_usage`：内存占用率（%），attributes 附 `used_mb`/`total_mb`
  - `sensor.ha_data_store_disk_usage`：硬盘已用率（%），attributes 附 `used_mb`/`total_mb`
- 三者 attributes 均带 `type` / `percent` 字段；已接入 `async_track_time_interval` 每 30 秒刷新。

### 🖥️ 系统/HA 基本信息采集
- **`sensor.py`**：`DbViewerUrlSensor._fetch_url` 在 attributes 新增 `system` 子对象（每 10 分钟随 DB 地址低频刷新一并更新），包含：
  - HA 侧：`ha_version` / `installation_type`（用官方 `homeassistant.helpers.system_info.async_get_system_info` 获取，失败时以 `.HA_VERSION` 文件兜底版本）/ `frontend_version`（读 `home-assistant-frontend` 包）/ `install_time`（近似=configuration.yaml mtime）/ `config_dir`
  - CPU：`cpu_model` / `cpu_physical_cores` / `cpu_logical_cores` / `cpu_freq_mhz`
  - 内存：`mem_total_mb`；硬盘整体：`disk_total_mb`（汇总所有真实本地磁盘分区）
  - `uptime_seconds` / `uptime_text`（系统开机时长）、`updated_at`
- psutil 为 HA 环境自带依赖，缺失时自动降级（相关字段返回 None / 传感器不报错），不影响集成其它功能。

### 🌐 翻译
- **`strings.json` / `translations/en.json`（`entity.sensor`）**、**`translations/zh-Hans.json`（`sensor`）**：新增 `cpu_usage` / `memory_usage` / `disk_usage` 实体名称翻译。

### 依赖文件
| 文件 | 改动 |
|------|------|
| `system_resources.py` | 新建：系统资源采集 + 3 个资源占用传感器类 |
| `sensor.py` | 导入新传感器；`async_setup_entry` 注册 3 传感器 + 固定实体 ID + 30s 刷新；`DbViewerUrlSensor` 加 `system` attributes |
| `const.py` | VERSION → 3.1.0 |
| `manifest.json` | 版本 3.1.0 |
| `strings.json` / `translations/*.json` | 新增 3 传感器名称翻译 |

## 2026-09-01 — v3.0.2 上报实体来源/设备/区域字段 + 实体健康实体固定 ID + 数据库浏览器列宽拖拽

### 🗄️ report_entities 表新增来源/设备/区域字段
- **`__init__.py`**：`report_entities` 建表新增 `entity_type` / `entity_device` / `entity_area` 三列（`TEXT NOT NULL DEFAULT ''`）；并添加**迁移逻辑**（循环检测列，对已存在旧表 `ALTER TABLE ... ADD COLUMN` 补列，`CREATE TABLE IF NOT EXISTS` 不会改旧表）。
- 字段含义：
  - `entity_type`：实体类型（前端根据配置/域判断后提交）
  - `entity_device`：实体所属**设备名称**（HA「设置→设备与服务」中实体的设备名，前端从 `hass.devices` 注册表映射）
  - `entity_area`：设备所属**区域名称**（前端从 `hass.areas` 注册表映射）

### 🔌 上报/查询接口同步新字段
- **`http_api.py`**：
  - `ReportEntitiesView` POST 写入：从前端提交的 item 提取 `entity_type`/`entity_device`/`entity_area` 并入库。
  - `ReportEntitiesView` GET 查询：`SELECT` 带上三字段。
  - `ReportAutoEntitiesView`：`SELECT` 带上三字段。
- **`sensor.py`**：
  - `AutomationStatusSensor` 的 `ha_automation[]` 查询：`SELECT` 带上三字段。
  - `ReportedEntitiesHealthSensor`（前端卡片实体健康）查询与 `entities[]` 输出：带上 `entity_type`/`entity_device`/`entity_area`。

### 🏷️ "前端卡片实体健康"实体固定 ID
- **`sensor.py`**：`ReportedEntitiesHealthSensor` 在 `__init__` 显式设置 `self.entity_id = "sensor.ha_data_entities_health"`，实体 ID 不再随翻译名/unique_id 变化。

### 🖱️ 数据库浏览器表格列宽拖拽
- **`db_viewer.html`**：
  - 新增列宽拖拽：数据表格表头 `th.resizable` 挂载 `resize-handle` 手柄，`mousedown` 拖拽实时调整 `th` 宽度并同步该列各行 `td`（`onResizeStart`/`onResizeMove`/`onResizeEnd`）。
  - **修复**拖拽放手误触排序：拖拽手柄阻止 `click`/`dblclick` 冒泡 + `_resizing` 标志兜底（`sortTable` 开头判断），拖拽列宽不再触发该字段排序。
  - 新增 CSS：`th.resizable` / `.resize-handle` / `body.col-resizing`。

### 依赖文件
| 文件 | 改动 |
|------|------|
| `__init__.py` | `report_entities` 建表 + ALTER 迁移新增 `entity_type`/`entity_device`/`entity_area` |
| `http_api.py` | 上报实体写入/查询、自动化上报实体查询带新字段 |
| `sensor.py` | 自动化状态传感器 ha_automation 与实体健康传感器带新字段；实体健康实体固定 ID `sensor.ha_data_entities_health` |
| `db_viewer.html` | 表格列宽拖拽 + 修复拖拽误触排序 |
| `const.py` | VERSION → 3.0.2 |
| `manifest.json` | 版本 3.0.2 |

## 2026-09-01 — v3.0.1 操作记录头像统一接口 + 自动化 API 增强 + 自动化状态传感器实时刷新 + 数据库浏览器地址传感器

### 👤 操作记录用户头像统一 API（配合前端）
- **前端**（`history-bubble.js`）：用户头像获取从两条路径（`device_history` 自带用户 / `device_user_by_date` 补充）收敛为**单一 `user_actions_daily` 接口**，用 `ts_text` 与实体状态时间对比匹配用户（±2 秒容差，可扩展）。
- **前端**（`data-fetch.js`）：`fetchApiUserActionsDaily` 新增第 4 参 `entityId`（URL 附加 `&entity_id=` 过滤）；新增 `buildUserMapFromActions`（ts 毫秒键 + `ts_text` 解析兜底）；删除废弃的 `fetchApiDeviceUserData` / `buildUserMapFromRecords`。

### 🐛 修复 user_actions_daily 的 entity_id 参数无效
- `_query_user_actions`：`user_actions_daily` 与 `user_actions_range` 分支改为**动态 WHERE 条件 + 可选 `entity_id = ?`**（此前参数已解析但未拼入 SQL，返回全部记录）。

### 🐛 修复 db_viewer 实体下拉框加载失败
- **后端**：`_check_api_enabled` / `_check_master_switch` / `_check_db_viewer_enabled` / `_check_db_edit_enabled` 鉴权失败由**空 body 403** 改为 `web.json_response({"success": False, "error": "..."}, status=403)`，前端 `resp.json()` 不再抛 "Unexpected end of JSON input"。
- **前端**（`db_viewer.html`）：无选中 API Key 时提前拦截提示；增加 `resp.ok` / `json.success` 检查并显示具体错误信息。

### 🤖 自动化 API 增强（执行结果查询丰富统计）
- **按名称查询（auto_lookup）**：新增 `date=YYYY-MM-DD`（按 `trigger_time` 日期过滤 recent_logs）、`limit=N`（输出条数限制，默认 5 上限 50）。
- **执行结果查询（auto_logs）**：
  - 新增 `keyword`（按 `automation_name LIKE` 模糊筛选）、`date`（按触发日期过滤）。
  - 分页参数**优先 `limit/offset`**，兼容原有 `size/page`（修复前端发 `limit/offset` 时后端只读 `size/page` 导致的分页失效）。
  - 响应顶层新增 **`run_count` / `success_count`** 与 **`stats` 聚合对象**：`run_count` / `success_count` / `failed_count` / `partial_failed_count` / `skipped_count` / `success_rate` / `avg|min|max_duration_ms` / `last_run` / `first_run`。
- **新增 `automation_stats` 汇总接口**（`GET /api/ha_data_store/automation_stats?date=&limit=`）：返回 `today`（当日统计）/ `total`（累计统计）/ `ranking`（执行排行，含 automation_id/name/run_count/success_rate/last_run）。
- **前端**（`db_viewer.html`）：`auto_stats` 下拉选项；auto_lookup 显示条数参数、auto_logs 显示名称/日期/条数参数；`generateApiUrl` 同步生成对应参数。

### ⚡ 自动化状态传感器更新规则改造（`sensor.ha_data_store_automation`）
- **automation_logs 有新数据 → 自动更新**：自动化引擎每次执行记录落库后经 `_notify_status_sensor()` 回调传感器**即时刷新**（不等 30 秒轮询）；另加轮询 `MAX(id)` 增量检测兜底（覆盖外部直写数据库场景）。
- **ha_automation 节点（`automation.*` 实体）状态变化 → 自动更新**：注册 `state_changed` 事件监听，实体 ID 以 `automation.` 开头即触发刷新，内置 **2 秒防抖**合并高频变化。
- **新增 `async_trigger_refresh` 防重入入口**：定时器、日志回调、实体变化、手动按钮统一走此入口。
- **新增手动刷新按钮** `button.ha_data_store_automation_status_refresh`（"刷新自动化状态" / "Refresh Automation Status"，图标 `mdi:refresh`）。

### 🔗 新增数据库浏览器地址传感器（`sensor.ha_data_store_db_viewer_url`）
- **state** = db_viewer 完整可访问地址，如 `http://IP:端口/api/ha_data_store/db_viewer`；**attributes**：`path` / `base_url` / `updated_at`（取地址失败记录 `error`）。
- 地址经 `hass_network.get_url` 获取（自动优先 `external_url` 其次 `internal_url`，适配 HA 实际监听地址）；**启动时立即获取** + **每 10 分钟低频刷新**（外部/内部地址变更时自动更新）。

### 依赖文件
| 文件 | 改动 |
|------|------|
| `http_api.py` | 修复 `user_actions_daily/range` entity_id 过滤；4 个鉴权检查 403 返回 JSON body；auto_lookup 增 `date/limit`；auto_logs 增 `keyword/date`、`limit/offset` 优先分页、顶层 `run_count/success_count` + `stats`；新增 `AutomationStatsView`（automation_stats） |
| `automations.py` | 新增 `_notify_status_sensor`：automation_logs 写日志后回调刷新自动化状态传感器 |
| `sensor.py` | 自动化状态传感器：`async_trigger_refresh` 防重入入口 + automation_logs `MAX(id)` 增量检测 + `automation.*` 状态变化监听（2 秒防抖）；新增 `DbViewerUrlSensor`（启动即取 + 10 分钟刷新） |
| `button.py` | 新增 `AutomationStatusButton` 手动刷新按钮 |
| `const.py` | VERSION → 3.0.1 |
| `manifest.json` | 版本 3.0.1 |
| `translations/zh-Hans.json` | 新增 `automation_status_refresh` / `db_viewer_url` 名称 |
| `translations/en.json` | 新增 `automation_status_refresh` / `db_viewer_url` 名称 |
| `db_viewer.html` | 鉴权错误提示优化；新增 auto_stats 选项；auto_lookup/logs 日期/条数/keyword 参数 UI |
| `C:\HA\src\modules\utils\data-fetch.js` | 前端：`fetchApiUserActionsDaily` 增 entityId 参数、新增 `buildUserMapFromActions`、删除废弃接口 |
| `C:\HA\src\modules\cards\history-bubble.js` | 前端：头像获取统一 `user_actions_daily` 单接口，±2 秒容差匹配 |

## 2026-08-31 — v3.0.0 操作记录时间权威对齐 + 简单自动化引擎与自动化管理卡片

### ⏱️ 操作记录时间与实体状态时间严格对齐（差一秒修复）

**背景**：前端操作记录时间 `ts` 此前取自**点击时刻**（浏览器时钟），而后端 `device_history` 的 `on_time/off_time` 直接采用**实体状态变化时刻**（`last_changed`），两端时间源不一致，且浏览器时钟与服务器时钟存在 1-2 秒偏差，导致 `user_actions.ts_text` 与 `device_history` 精确匹配时差一秒。

- **前端**（room-elves-card `action-log.js`）：`ts` 改为**无条件以实体状态时间（`last_changed`）为准**，消除浏览器/服务器时钟偏差。
  - 覆盖条件从 `stateTs >= clickTs` 改为**时间窗判断**：`|stateTs - clickTs| <= 8 秒` 即用实体状态时间覆盖（`_ACTION_LOG_ALIGN_TOLERANCE`）。
  - 500ms 延迟核对改为**轮询式**（`_ACTION_LOG_ALIGN_MAX_ATTEMPTS`，最多 8 次约 4 秒）：慢设备状态未变化完成（读到上一次 `last_changed` 超容差）时延迟 500ms 再核对；`state_log` 组装加幂等保护。
  - `_flushActionLog` fetch 前同步补核对同样改为时间窗判断（防抖/阈值调度先于延迟核对触发时的兜底）。
- **后端**（本集成）：
  - `__init__.py`：启动数据清理——将历史 `device_history.on_time/off_time` 中带毫秒后缀的时间戳**截断到秒**（`SUBSTR(...,1,19)`），保证与前端 `ts_text`（秒精度）格式一致，精确匹配即可命中。
  - `http_api.py`：`ts_text` 统一由前端 `ts` 经 `_format_ts_ms` 按**秒精度**格式化（本地时区）。
  - `_link_device_history_to_actions`：用 `on_time == ts_text` / `off_time == ts_text` **精确相等**关联回填操作用户，杜绝差一秒漏关联。

### ⚡ 操作记录上报链路健壮性（配合前端性能优化）

前端 `action-log.js` 上报逻辑重构，后端无需改动即可受益：

- **fetch 超时**：新增 `_ACTION_LOG_FLUSH_TIMEOUT=15s`，`AbortController` 中止挂起请求，避免 `pending` 标志被永久锁死导致后续上报全部跳过。
- **失败自动重试**：指数退避（5s→10s→…上限 5 分钟），不再依赖"下次用户操作"才恢复。
- **防抖式上报**：操作停止 5 秒后统一上报（`_ACTION_LOG_FLUSH_INTERVAL`），连续操作合并为一批；阈值满 20 条立即上报并清理定时器。
- **游标防倒退**：上报前读 localStorage 游标取 `max`，防止多实例/多标签页并发重复上报。
- **fetch 前同步补核对**：确保 `ts`（实体状态时间）与 `state_log` 在任意调度时序下都是权威值。

### 🤖 简单自动化引擎（定时/间隔/条件 + 执行记录）
- **触发方式**：① 定时（每天固定时间 + 可选星期白名单）② 间隔（每 N 秒，重启后过期不补跑，直接顺延）。
- **多条件执行**：基于实体状态比较，运算符 `==`/`!=`/`>`/`>=`/`<`/`<=`/`contains`，支持 `all`（全部满足）/`any`（任一满足）组合；`unavailable`/`unknown` 视为条件不成立并记录明细。
- **动作**：顺序调用 HA 服务（`domain.service` + 实体 + 参数 JSON），逐条记录成功/失败；`stop_on_error` 可配置失败即停。
- **执行记录落库**（`automation_logs` 表）：时间、触发描述、条件逐条明细、动作逐条结果、耗时、状态（success/failed/partial_failed/skipped），默认保留 30 天自动清理。
- **调度**：统一 30 秒 tick（`async_track_time_interval`），配置每次从 DB 重读（增删改立即生效）；`next_run` 持久化到 DB；防重入（执行中跳过重复触发）。
- **API**（沿用 API Key 鉴权）：
  - `GET/POST /api/ha_data_store/automations` — 列表/新增
  - `PUT/DELETE /api/ha_data_store/automations/{id}` — 修改（部分更新，改后自动重算 next_run）/删除
  - `POST /api/ha_data_store/automations/{id}/run?force=1` — 手动触发（force 跳过条件）
  - `GET/DELETE /api/ha_data_store/automation_logs` — 执行记录分页查询/清理（`?days=N` 或 `?automation_id=`）
- **前端**：db_viewer.html 新增「🤖 自动化」页签，含自动化列表（启用开关/手动运行/强制运行/编辑/删除）+ 编辑弹窗（触发/条件动态行/动作动态行）+ 执行记录子页签（分页/按自动化与状态过滤/明细弹窗/清理）。

### 📊 新增「自动化状态」传感器（`sensor.ha_data_store_automation`）
- **实体 ID 固定**：自动化状态传感器实体 ID 固定为 **`sensor.ha_data_store_automation`**（HA 实体 ID 不允许点号，配置中 `sensor.ha.data.store.automation` 的点号写法会自动归一为下划线）。
- 旧实体 `sensor.hashu_ju_tong_yi_cun_chu_xi_tong_automation_status` 自动重命名（registry 迁移）。
- 前端卡片支持 `entity` 配置项自定义数据实体，默认 `sensor.ha_data_store_automation`。
- **状态值** = 系统中有多少个启用自动化。
- **状态属性**：
  - `total`/`enabled`/`disabled` — 自动化总数 / 启用 / 停用。
  - `success`/`failed`/`skipped`/`never` — 按**最近一次执行结果**统计的自动化数量（成功/失败+部分失败/条件跳过/从未执行）。
  - `total_runs` — 全部执行次数合计；`updated_at` — 数据更新时间。
  - `automations[]` — 每个自动化的详细信息：`id/name/enabled/trigger_type/trigger_desc/stop_on_error/next_run/last_run/last_result/last_duration_ms/run_count/success_count/failed_count/skipped_count`。
- 30 秒定时刷新（`async_track_time_interval`），与现有传感器一致。

### 🃏 room-elves-card 自动化管理卡片增强（选项卡 + 编辑功能）
- **头部**：去掉右侧启用数（`auto-header-right`），副标题只显示"更新于 xxx"。
- **新增选项卡栏**（信息 | 编辑）：
  - **信息选项卡**：原整体统计卡（7 项）+ 自动化明细列表。
  - **编辑选项卡**：自动化管理列表（对接后端 REST API），每个自动化支持：
    - 启停切换（PUT enabled）
    - 手动运行（POST run）
    - 强制运行（跳过条件，force=1）
    - 编辑（打开表单弹窗回填，PUT）
    - 删除（DELETE，带确认）
    - ＋ 新建自动化（表单弹窗，POST）
- **编辑/新建表单弹窗**：名称、启用、失败即停、触发类型（定时：时间+星期多选 / 间隔：分钟数）、执行条件（逻辑 all/any + 条件行可增删：实体+操作符+值）、执行动作（常用服务下拉分组+自定义服务、实体、参数 JSON，可增删）。
- **两种触发**：`popup_card`（card.type: `automation` → `createAutomationCard`）与 `action:card`（type: `automation` → `showAutomationPopup`）。
- API 鉴权：复用卡片顶层配置 `api_base_url` + `key`。
- 主题化：全部使用 `--room-*` 主题变量，随房间精灵 5 套主题自动适配。

### 依赖文件
| 文件 | 改动 |
|------|------|
| `__init__.py` | 启动数据清理：截断 `device_history.on_time/off_time` 毫秒后缀；`_init_database` 建 automations/automation_logs 表+索引；setup 启动/停止 AutomationManager；注册 4 个 View |
| `http_api.py` | `ts_text` 秒精度格式化；`_link_device_history_to_actions` 用 `on_time == ts_text` 精确匹配关联；新增 `AutomationsView`/`AutomationItemView`/`AutomationRunView`/`AutomationLogsView` |
| `automations.py` | 新建：`AutomationManager` 执行引擎（调度/条件/动作/记录） |
| `sensor.py` | 新增 `AutomationStatusSensor`（30 秒刷新，实体 ID 固定 `sensor.ha_data_store_automation`） |
| `const.py` | VERSION → 3.0.0；新增 `TABLE_AUTOMATIONS`/`TABLE_AUTOMATION_LOGS` 及调度参数 |
| `manifest.json` | 版本 3.0.0 |
| `translations/zh-Hans.json` | 新增 `automation_status.name` |
| `db_viewer.html` | 新增「🤖 自动化」页签（列表/编辑弹窗/执行记录/明细） |
| `C:\HA\src\modules\core\action-log.js` | 前端配合：时间窗+轮询无条件以 `last_changed` 为准；fetch 超时/重试/防抖/游标防倒退 |
| `C:\HA\src\modules\cards\automation.js` | 新建自动化管理 Mixin：头部/统计卡/明细列表 + 选项卡栏 + 编辑表单弹窗 + API 封装 `_autoFetch`；默认实体 `sensor.ha_data_store_automation`，`_autoNormalizeEntityId` 兼容点号写法 |
| `C:\HA\src\room-elves-card.js` | import + Object.assign + CARD_TYPE_REGISTRY['automation'] |
| `C:\HA\src\modules\cards\card-factory.js` | internalCardTypes 加入 automation |
| `C:\HA\src\modules\core\actions.js` | case 'automation' 分支 |
| `C:\HA\src\room-elves-card-styles.css` | 追加 auto-* 系列样式（含 auto-tabs/auto-edit-*/auto-form-*） |

---

## 2026-08-30 — v2.19.1 修复自定义路由访问 500

### 🐛 修复动态路由访问 500（根因：视图方法缺 tail 参数）
- **根因**：`DynamicRouterView` 的 url 含 `{tail}` 时，HA 会把它作为**关键字参数**传入视图方法，但 `get/post/put/delete` 只接收 `request`，导致 `TypeError: DynamicRouterView.get() got an unexpected keyword argument 'tail'` → 500。
- **修复**：`get/post/put/delete` 方法签名加 `**kwargs`，吸收 `tail` 关键字参数。
- **url 由 `{tail:.*}` 改为 `{tail}`**（单段路径足够，且能触发 match_info 的 tail）。
- **新增 `_safe_dynamic` 兜底**：动态路由任何异常都返回明确错误 JSON 并记录日志，不再空白 500。
- **扩展 SQL 异常捕获**：`sqlite3.ProgrammingError`/`DatabaseError` 也返回明确错误信息。
- **修复无参数 SQL 报错**：排除鉴权参数 `key`/`auth`/`access_token` 进入 SQL 绑定，避免"无参数 SQL 却提供了绑定值"报 `Incorrect number of bindings`。

### 依赖文件
| 文件 | 改动 |
|------|------|
| `http_api.py` | `DynamicRouterView` url 改 `{tail}`；新增 `_safe_dynamic`；扩展 SQL 异常捕获 |

---

## 2026-08-30 — v2.19.0 自定义路由支持删除

### 🗑 自定义路由增加删除功能
- **后端 `CustomRoutesView` 新增 `DELETE /api/ha_data_store/routes?route_path=xxx`**：按 `route_path` 删除自定义路由，鉴权与保存一致（依赖全局「数据库修改」开关）。
- **前端自定义路由列表每行新增"🗑 删除"按钮**：点击二次确认后删除，删除成功后自动刷新列表和地址生成器下拉。

### 依赖文件
| 文件 | 改动 |
|------|------|
| `const.py` | VERSION 2.18.0 → 2.19.0 |
| `http_api.py` | `CustomRoutesView` 新增 `delete` 方法 |
| `db_viewer.html` | 路由列表新增删除按钮 + `deleteCustomRoute` 函数 |
| `manifest.json` | 版本 2.19.0 |

---

## 2026-08-30 — v2.18.0 自定义路由支持命名占位符并接入地址生成器

### ⚡ 自定义路由支持命名占位符（:name）
- **后端 `DynamicRouterView` 支持命名占位符**：SQL 里可用 `:name`（或 `@name` / `$name`），从 GET query 参数**按名**取值绑定；同时**兼容**传统 `?` 占位符（按参数字母序绑定，旧配置不受影响）。
- **自定义路由表单**：提示推荐 `:name` 写法，placeholder 更新为命名占位符示例。

### 🔗 自定义路由接入「API 地址生成器」
- **地址生成器的查询类型新增"自定义路由"分组**：自动列出所有已保存的自定义路由，点选即可。
- **选中自定义路由后自动解析 SQL 的 `:name` 生成参数输入框**，填好参数即自动生成调用 URL（`/api/ha_data_store/custom/{path}?参数=值`），无需手写。
- 新建/编辑路由保存后，地址生成器下拉自动刷新。

### 依赖文件
| 文件 | 改动 |
|------|------|
| `const.py` | VERSION 2.17.0 → 2.18.0 |
| `http_api.py` | `DynamicRouterView` 参数绑定支持 `:name/@/$` 命名占位符，兼容 `?` |
| `db_viewer.html` | apiQueryType 新增"自定义路由"分组；选中自定义路由解析 `:name` 生成参数框；自定义路由表单提示 `:name` 写法 |
| `manifest.json` | 版本 2.18.0 |

---

## 2026-08-30 — v2.17.0 自定义路由管理与数据库字段编辑

### 🛠 数据库浏览器新增「自定义路由管理」
- **API 工具 tab 新增"自定义路由管理"子区域**：可新建/编辑/保存自定义查询接口（`route_path` + SELECT SQL + 描述），保存后通过 `GET /api/ha_data_store/custom/{route_path}?参数=值` 直接调用，**无需再改后端新增 type**。
- 复用后端已有的 `custom_routes` 表与 `DynamicRouterView` 动态路由机制（参数按字母序绑定到 SQL 的 `?` 占位符，安全沙箱只允许 SELECT）。
- 不改变现有查询接口。

### 🧬 数据库浏览新增「字段管理」
- **后端新增 `DbAlterTableView`**（`POST /api/ha_data_store/alter_table`）：支持 `ALTER TABLE ADD COLUMN`（增字段）/ `DROP COLUMN`（删字段）。
  - 依赖全局「数据库修改」开关（`db_edit_enabled`），无需单独管理员密码。
  - 字段类型白名单（TEXT/INTEGER/REAL/NUMERIC/BOOLEAN/BLOB）、标识符防注入校验。
  - 保护核心表（`entity_configs`/`custom_routes`/`api_keys` 等）不可改字段；主键字段不可删；删字段要求 SQLite 3.35+。
- **数据库浏览 tab 新增"🧬 字段管理"按钮**：弹窗查看当前表字段（列名+类型）、添加字段（字段名/类型/默认值）、删除字段。

### 依赖文件
| 文件 | 改动 |
|------|------|
| `const.py` | VERSION 2.16.0 → 2.17.0 |
| `http_api.py` | 新增 `DbAlterTableView`（alter_table 增删字段）+ `_sqlite_version_ge`；新增 `import re` |
| `__init__.py` | 注册 `DbAlterTableView` 视图 |
| `db_viewer.html` | API 工具新增自定义路由管理子区域；数据库浏览新增字段管理模态框与按钮 |
| `manifest.json` | 版本 2.17.0 |

---

## 2026-08-30 — v2.16.0 设备历史新增用户维度查询 API

### 📊 API 工具新增设备用户维度查询
基于 `device_history` 表的 `on_user` / `off_user` / `on_snapshot` / `off_snapshot` 字段，新增 5 个查询 type（查 `/api/ha_data_store/query`，**不影响**原有 `device_history` / `device_summary` 等查询逻辑）：

- **`device_users_list`**：设备操作用户列表（`on_user`∪`off_user` 去重），每个用户含开启次数/关闭次数/参与次数/涉及设备数。参数：`date/month/year/room/entity_id`。
- **`device_user_history`**：按用户查设备使用记录（匹配 `on_user` 或 `off_user`）。参数：`user_name`(必填)、`direction=on|off|both`、`entity_id/date/room/limit`。每条记录带 `matched` 标注命中开启还是关闭。
- **`device_user_summary`**：按用户汇总（开启次数/关闭次数/设备数/总能耗/总时长）。参数：`user_name`(可选)、`date/month/year/room`。
- **`device_on_user_history`**：按开启用户维度查记录（`on_user` 非空）。参数：`user_name`(可选)、`entity_id/date/room/limit`。返回含该维度用户候选列表。
- **`device_off_user_history`**：按关闭用户维度查记录（`off_user` 非空）。参数同上。

**统计口径**：每条记录的 `on_user` 计 1 次开启、`off_user` 计 1 次关闭，开启/关闭分开计数。

### 依赖文件
| 文件 | 改动 |
|------|------|
| `const.py` | VERSION 2.15.0 → 2.16.0 |
| `http_api.py` | `QueryView` 新增 `device_users_list/device_user_history/device_user_summary/device_on_user_history/device_off_user_history` 5 个路由 + `_build_device_user_where` 辅助 + 5 个查询方法 |
| `db_viewer.html` | API 工具"设备类"分组新增 5 个查询选项 + 用户/方向输入控件 + `onApiTypeChange`/`generateApiUrl`/`_hideAllApiFormRows` 处理 + 使用说明 |
| `manifest.json` | 版本 2.16.0 |

---

## 2026-08-30 — v2.15.0 设备历史记录用户关联

### 🕘 device_history 表新增操作用户字段
- **`device_history` 表新增 4 个字段**：`on_user`（开启用户）、`off_user`（关闭用户）、`on_snapshot`（开启操作快照）、`off_snapshot`（关闭操作快照）。
- **自动迁移**：启动时检测旧表缺列则 `ALTER TABLE ADD COLUMN`，不破坏存量数据。
- **关联逻辑**：前端上报操作写入 `user_actions` 后，按 `entity_id` + 时间戳匹配回填到 `device_history`：
  - `on_time == ts_text` → 该操作视为开启，回填 `on_user` / `on_snapshot`
  - `off_time == ts_text` → 该操作视为关闭，回填 `off_user` / `off_snapshot`
  - 同一条记录的开/关可分别由不同用户操作匹配，互不影响
- **仅覆盖有值项**：用户或快照为空时不覆盖已有值，避免物理按键等无 user_actions 的操作清空历史。

### 依赖文件
| 文件 | 改动 |
|------|------|
| `const.py` | VERSION 2.14.0 → 2.15.0 |
| `__init__.py` | `device_history` 建表新增 on_user/off_user/on_snapshot/off_snapshot 4列 + 迁移补列 |
| `http_api.py` | `ActionLogView.post` 写入 user_actions 后新增 `_link_device_history_to_actions` 关联回填逻辑 |
| `manifest.json` | 版本 2.15.0 |

---

## 2026-08-29 — v2.14.0 近期使用设备按用户分组统计

### 📊 近期使用设备传感器按用户分组
- **`sensor.近期使用设备` 的 `attributes.devices[]` 改为按 (用户, 操作快照) 聚合**：同一设备实体被多个用户操作时，各自独立成条，每条带独立 `user_name` / `count` / `last_used`，不再只保留最后一个用户的数据。
- **`total_devices`（state 值）语义保持不变**：仍为去重后的设备实体数，不随用户重复计算。
- **新增 `total_user_devices` 字段**：表示「用户×设备」的组合条数，供前端按用户统计/筛选使用。

### 依赖文件
| 文件 | 改动 |
|------|------|
| `const.py` | VERSION 2.13.0 → 2.14.0 |
| `sensor.py` | `UserActionsSensor._load_data` 聚合键加入 user_name，同一实体多用户拆分为独立记录；`total_devices` 按 entity_id 去重；新增 `total_user_devices` |
| `manifest.json` | 版本 2.14.0 |

---

## 2026-08-26 — v2.13.0 操作记录新增设备类型 device_type

### 🎯 操作记录新增设备类型字段
- **`user_actions` 表新增 `device_type` 字段**：记录操作所属设备类型（如 light/socket/ac 等），**值由前端上报**（前端后续提交）。
- **写入**：`ActionLogView.post` 接收前端 `item.device_type` 存入独立列，无则空串。
- **自动迁移**：旧表缺 `device_type` 列时启动自动 `ALTER TABLE ADD COLUMN`。

### 📊 近期使用设备传感器新增 device_type
- `sensor.近期使用设备` 的 `attributes.devices[]` 每项新增独立 **`device_type`** 字段。
- `GET /api/ha_data_store/action_log` 返回结果也包含 `device_type`。

### 依赖文件
| 文件 | 改动 |
|------|------|
| `const.py` | VERSION 2.12.0 → 2.13.0 |
| `__init__.py` | 建 `user_actions` 表加 `device_type` 列 + 迁移补列 |
| `http_api.py` | `ActionLogView` POST 写入 device_type、GET 返回 device_type |
| `sensor.py` | `UserActionsSensor` 查询含 device_type + devices 输出独立 device_type |
| `manifest.json` | 版本 2.13.0 |

---

## 2026-08-26 — v2.12.0 用户操作记录新增弹窗 config_id

### 🎯 操作记录支持弹窗 config_id（还原完整弹窗配置）
- **`user_actions` 表新增 `config_id` 字段**：记录操作所属弹窗/选项卡/设备的 config_id（如 `diannao`、`shao_shui_hu`），用于定位并还原该操作的完整弹窗配置。
- **写入来源**：`ActionLogView.post` 优先取前端显式上报的 `config_id`/`device_config_id` 字段，否则从 `action_snapshot` JSON 中解析 `config_id`（前端已将 config_id 注入 action_snapshot）。
- **自动迁移**：旧表缺 `config_id` 列时启动自动 `ALTER TABLE ADD COLUMN`，不破坏存量数据。

### 📊 近期使用设备传感器新增 config_id
- `sensor.近期使用设备` 的 `attributes.devices[]` 每项新增独立 **`config_id`** 字段，便于直接识别该操作所属弹窗。
- `GET /api/ha_data_store/action_log` 返回结果也包含 `config_id`。

### 依赖文件
| 文件 | 改动 |
|------|------|
| `const.py` | VERSION 2.11.1 → 2.12.0 |
| `__init__.py` | 建 `user_actions` 表加 `config_id` 列 + 迁移补列 |
| `http_api.py` | `ActionLogView` POST 写入 config_id（含 action_snapshot 解析）、GET 返回 config_id |
| `sensor.py` | `UserActionsSensor` 查询含 config_id + devices 输出独立 config_id |
| `manifest.json` | 版本 2.12.0 |

---

## 2026-08-25 — v2.11.1 健康记录新增备注/说明字段

### 🏥 健康记录表增加 2 个字段
- **`health_records` 表**新增 `remark`（备注）、`description`（说明）两个字段：
  - `remark`：备注（已有字段保留）
  - `description`：说明（新增）
- **自动迁移**：启动时检测旧表缺 `description` 列则 `ALTER TABLE ADD COLUMN`，历史数据 description 默认为空，不破坏存量
- **API 显示**：`health_history` / `health_latest` 查询结果（`SELECT *`）自动包含 `remark` 与 `description`
- **写入支持**：`POST /api/ha_data_store/health/add` 新增可选参数 `description`

### 依赖文件
| 文件 | 改动 |
|------|------|
| `const.py` | VERSION 2.11.0 → 2.11.1 |
| `__init__.py` | 建表加 `description` 列 + 迁移补列 |
| `http_api.py` | `HealthAddView` INSERT 加 `description` |
| `manifest.json` | 版本 2.11.1 |

---

## 2026-08-24 — v2.11.0 用户操作记录与近期使用设备

### 🎯 用户操作记录（前端埋点 → 后端存储）
- **新增 `user_actions` 表**（追加式，不去重）：保存前端 room-elves-card 埋点上报的每次操作记录，字段含 `user_name / entity_id / action / name / icon / room_name / source / service / card_type / other / state_log / ts / ts_text / action_snapshot`。
- **新增 `POST /api/ha_data_store/action_log`**：前端批量上报操作记录，写入成功后**实时刷新**近期使用设备 sensor。
- **新增 `GET /api/ha_data_store/action_log?days=N`**：查询近 N 天原始操作记录（调试用）。
- `action_snapshot`：完整 tap_action 快照（JSON），用于将来前端还原设备控制面板；`state_log`：操作前→操作后状态（如 `on→off`、`cool→heat`）；`ts_text`：人类可读时间；`user_name`：当前登录用户。

### 📊 近期使用设备传感器
- **新增 `sensor.近期使用设备`**（`sensor.ha_data_store_user_actions`）：
  - **状态值** = 近 30 天有操作的不同设备面板数
  - `attributes.devices` = 按 **action_snapshot 归一化聚合** 的设备列表（含完整 tapAction 快照可还原 + 使用次数 `count` + `last_used`/`last_used_text` + 最近一次 `state_log`），按使用次数降序
  - 30 秒定时刷新 + 写入后实时刷新；统计窗口固定 30 天

### 🧰 API 工具新增"用户动作查询组"
内置数据库浏览器"API工具"页面新增 **🎯 用户动作查询** optgroup，支持 7 种查询：
- `user_actions_daily`：指定日期操作记录（`date`）
- `user_actions_range`：指定日期段操作记录（`start`/`end`）
- `user_actions_month_dates`：指定月哪些日期有数据（`month`）
- `user_actions_hour_dist`：数据点按小时分布（`entity_id` 可选，返回 00-23 各小时次数）
- `user_actions_entity_summary`：实体操作次数排行
- `user_actions_user_summary`：按用户汇总操作次数
- `user_actions_entity_last_today`：指定实体当日最后一条记录（`entity_id` 必填）

实体下拉支持自动填充可查询实体；表为空/加载失败时给出明确提示。

### 依赖文件
| 文件 | 改动 |
|------|------|
| `const.py` | 新增 `TABLE_USER_ACTIONS`；VERSION 2.10.0 → 2.11.0 |
| `__init__.py` | 建 `user_actions` 表 + 迁移补列（ts_text/card_type/other/state_log）+ 注册 `ActionLogView` |
| `http_api.py` | 新增 `ActionLogView`（POST/GET action_log）+ `_query_user_actions`（7 种子类型查询） |
| `sensor.py` | 新增 `UserActionsSensor`（近期使用设备，30 天聚合 + 实时刷新） |
| `db_viewer.html` | API 工具新增用户动作查询组 + 实体下拉自动填充 |
| `manifest.json` | 版本 2.11.0 |

---

## 2026-08-21 — v2.10.0 今日家庭状态总结

### 🏠 今日家庭状态总结（自动 + 手动触发）
基于数据库历史表聚合今日事实，渲染为精简中文段落，为 0 的项自动跳过：

- **新增传感器** `sensor.today_family_status`：
  - **状态值** = 极简一句（家中有人/无人 + 开着几盏灯 + 入户门状态及时长，≤255字符）
  - `attributes.summary` = 完整段落；`sections` = 完整结构化分节（environment/devices/power/vacuum/health/xiaoai/presence/lights/door）；`overall` = normal | warning；`alerts` = 异常提醒列表；`alert_text` = 提醒文字；`offline` = 离线设备数
- **新增按钮** `button.ha_data_store_daily_summary`：仪表盘放置按钮卡片，点击立即触发分析
- **新增服务** `ha_data_store.generate_daily_summary`（可选参数 `date`，默认今天，供自动化/NR 调用）
- **自动刷新**：HA 启动后 1 分钟自动生成一次；之后每 30 分钟（整 30 分钟，即 00 分/30 分）自动更新

### 📊 聚合维度
| 节 | 数据源 | 内容 |
|----|--------|------|
| 环境 | env_temperature/humidity/pm25/co2 | 今日最高/最低/平均 + 房间明细（温差≥2°C 时补充） |
| 设备 | device_history | N 台/总时长 + 运行最久亮点 + **用电 TOP3 + 开关频次 TOP3**（房间名+设备名）；完整逐台明细进 sections |
| 用电 | env_power | 当日自增读数最后一条 = 今日总用电（kWh，非加法）+ 昨日 + 环比 |
| 家庭事件 | vacuum_history / health_records / xiaoai_conversations | 扫地机次数、健康记录条数、小爱对话条数及时段 |
| 人在/门 | device_history（name=人在/入户门） | on_time 非空且 off_time 空=该房间有人/门开（显示开门时长）；否则家中无人/门关 |
| 灯光 | device_history（name 含"灯"） | 每盏灯取最新一条，on_time 非空且 off_time 空=该灯开着，统计"开着 x 盏灯（房间）" |
| 离线实体 | report_entities + 实时 states | 三态判定，unavailable 算离线、unknown 不算；有离线时 summary 末尾显示"离线设备 x 台" |

### ⚠️ 异常提醒（阈值写死）
- 高温 ≥30°C、低温 ≤5°C
- 单台连续运行 >6 小时
- 用电环比波动 >20%
- 存在任一提醒时 `overall=warning`，提醒文字放 `alert_text` 字段（`alerts` 为列表）；`summary` 段落末尾单独显示"离线设备 x 台"（离线不进 alerts）

### 依赖文件
| 文件 | 改动 |
|------|------|
| `daily_summary.py` | 新增：聚合 + 渲染 |
| `button.py` | 新增：按钮平台 |
| `sensor.py` | 新增 TodayFamilyStatusSensor |
| `__init__.py` | PLATFORMS 加 button + 注册 generate_daily_summary 服务 |
| `const.py` | VERSION 2.9.0 → 2.10.0 |
| `manifest.json` | 版本 2.10.0 |
| `translations/*.json` | sensor/button 翻译 key |

---

## 2026-08-21 — v2.9.0 实体健康三态判定（offline/unknown/online）

### 🧭 健康传感器判定口径变更
`sensor.reported_entities_health`（前端卡片实体健康）离线判定改为**三态**：

- `offline`：实体状态为 `unavailable`（集成未加载/实体被删除/设备无响应，真离线）
- `unknown`：实体状态为 `unknown`（**不计离线**，如未被点击过的 `button`/`input_button` 等无状态实体，属正常）
- `online`：其余正常状态

### 🖥️ 后端行为
- 状态值（掉线数）仍统计 `offline`（unavailable）个数，`unknown` 不再计入
- `attributes` 新增 `unknown` 字段（去重后的未知状态实体数）
- `online = total - unknown - offline`
- `entities[]` 每项 `status` 取值变为 `online | unknown | offline`
- 空数据返回结构同步补充 `unknown: 0`

### 🎨 前端（room-elves-card）
- 顶部统计新增"未知"卡片（黄色，`mdi:help-circle`），点击弹出未知实体气泡
- 户型图房间配色三态：红（有离线）/ 黄（有未知无离线）/ 绿（正常）/ 灰（无数据）；角标显示"在线x · 未知y · 离线z"（0 值段省略）
- 房间气泡改为三选项卡（在线/未知/离线），默认优先显示有问题的选项卡
- ECharts 堆叠柱状图新增"未知"系列（黄色），顺序 在线→未知→离线
- 按状态列表排序：离线 → 未知 → 在线；未知行淡黄底色、黄色状态点/图标

### 依赖文件
| 文件 | 改动 |
|------|------|
| `const.py` | `VERSION` 2.8.0 → 2.9.0 |
| `manifest.json` | 版本 2.9.0（保持） |
| `sensor.py` | 健康传感器三态判定，新增 `unknown` 计数 |
| `docs/CHANGELOG.md` | 本条目 |

---

## 2026-08-19 — v2.8.0 上报实体表结构调整（允许重复 entity_id）

### 🔧 数据结构变更
`report_entities` 表主键由 `entity_id` 改为自增 `id`，**允许同一 `entity_id` 重复存储**（不再唯一约束）。

- 旧表（`entity_id` 主键）启动时自动检测并 DROP 重建（该表每次全量重置，数据可安全丢弃）
- 新增 `entity_id` 索引（`idx_report_entities_eid`）加速按实体查询

### 🖥️ 后端行为
- `POST /api/ha_data_store/report`：仍为**全量重置**（清空整表重写），但**不做去重**——前端上报多少行就存多少行，同一 `entity_id` 多行直接插入（改为普通 `INSERT`，不再 `INSERT OR REPLACE`）
- 前端是否去重由前端/用户控制，后端不干预，直接存储

### 🧭 健康传感器统计口径
`sensor.reported_entities_health`（前端卡片实体健康）：
- **状态值（掉线数）按"去重后的 entity_id"统计**——同一实体出现多行时，掉线只计一次
- 属性新增 `total_rows`（原始上报行数，含重复）
- 属性 `entities` 保留所有行（含重复，各自带 room_name/status/state）

### 依赖文件
| 文件 | 改动 |
|------|------|
| `const.py` | `VERSION` 2.7.0 → 2.8.0 |
| `manifest.json` | 版本 2.7.0 → 2.8.0 |
| `__init__.py` | `report_entities` 建表改自增 id 主键；加旧表迁移 DROP 重建；加 entity_id 索引 |
| `http_api.py` | POST 全量重置改为普通 `INSERT`（不再去重/`INSERT OR REPLACE`） |
| `sensor.py` | 健康传感器按去重 entity_id 统计掉线数，新增 `total_rows` 属性 |

---

## 2026-08-17 — v2.7.0 前端卡片实体健康监控

### ✨ 新功能
新增**前端卡片实体上报监控**：配合 room-elves-card 前端卡片，点击卡片上的 `report` 按钮即可将该卡片配置中涉及的全部实体一键上报后端，后端**全量重置存储**（只保留最新数据），并生成一个健康监控传感器，实时统计"前端涉及实体中掉线（unavailable/unknown）的个数"。

### 🔗 完整链路
```
room-elves-card 前端（report 按钮，多卡片共用同一按钮）
  → 点击一次，聚合所有 room-elves-card 实例的实体（entity_id/name/icon/room_name）
  → POST /api/ha_data_store/report（后端清空整表后写入全部实体）
  → 写入 report_entities 表（全量重置，只保留最新数据）
  → 新传感器 sensor.reported_entities_health 每 30 秒刷新
      状态值 = 掉线实体个数；属性 = 每个实体明细（entity_id/name/icon/room_name/status/state）
```

### 🗄️ 数据库结构
新增单表 **`report_entities`**（前端卡片上报实体表）：
- 字段：`entity_id(主键), name, icon, room_name, source, last_report_time`
- 已加入数据库浏览器的"用户表"（始终显示）
- 按 `room_name` 建索引

### 🖥️ HTTP API
新增 `ReportEntitiesView`：
| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/api/ha_data_store/report` | 接收前端全量上报，**清空整表后写入全部实体**（只保留最新数据） |
| GET | `/api/ha_data_store/report` | 查询全部上报实体 |

- key 作为 query 参数鉴权（`?key=xxx`），与现有 API 一致

### 🧭 房间名归属与全量重置
- 每张 room-elves-card 是一个房间，实体归属顶层 `room_name`；`head: true`（头部/全屋总览卡片）时房间名固定为 `"头部"`
- **多张卡片共用同一个 report 按钮**：点击一次 → 前端聚合所有 room-elves-card 实例的实体 → 单次请求上报后端
- 后端收到上报后**清空整表再写入全部实体**（全量重置，只保留最新数据，无历史残留）

### 🎛️ 前端适配（room-elves-card）
- 新增配置项 `report: 按钮实体`（如 `input_button.report`）
- 点击按钮触发上报：递归扫描配置提取所有实体，name 优先（配置 name > friendly_name > entity_id），icon 仅保留字符串（动态图标对象置空）
- 支持 `input_button`/`button` 域（按 state 或 `last_triggered` 变化触发）与开关类（off→on 触发）

### 依赖文件
| 文件 | 改动 |
|------|------|
| `const.py` | 新增 `TABLE_REPORT_ENTITIES`；`VERSION` 升级 2.6.0 → 2.7.0 |
| `__init__.py` | 建 `report_entities` 表；注册 `ReportEntitiesView` |
| `http_api.py` | 新增 `ReportEntitiesView`（POST 全量重置 / GET 查询） |
| `sensor.py` | 新增 `ReportedEntitiesHealthSensor`（前端卡片实体健康，30 秒刷新） |
| `db_viewer.html` | `isUserTable` 加入 `report_entities`（用户表始终显示） |
| `translations/*.json` | 新增 `reported_entities_health` 实体名称翻译 |
| `manifest.json` | 版本 2.6.0 → 2.7.0 |

---

## 2026-08-13 — v2.6.0 打印机数据采集功能

### ✨ 新增模块
新增打印机数据采集模块 `printer.py`（独立模块，遵循 `xiaoai.py` 架构），采集 HP 打印机统计数据与当日作业明细，并提供配置管理、数据查询和系统监控。

### 🗄️ 数据库结构
新增两张表：

**`printer_configs`**（配置表，支持多台打印机）
- 字段：`id, name(唯一), stats_entity, detail_entity, enabled, created_at, updated_at`

**`printer_daily`**（单张主记录表，每天一条）
- 字段：`name(打印机名称), day(日期), print/scan/copy/fax/jam_printer(当日汇总), ink_black/ink_cyan/ink_magenta/ink_yellow(墨量), printer_jobs(当日明细JSON), created_at, updated_at`

### 📥 数据采集（两个实体 → 单张主记录表）
| 实体 | 数据来源 | 更新内容 |
|---|---|---|
| 统计数据实体 | `attributes.daylist` | 每日汇总 + 墨量 |
| 当日详细数据实体 | `attributes` 各类型明细数组 | 当日汇总（各类型 count 求和）+ 墨量 + `printer_jobs` JSON |

- **当日数据实时更新**：当日多次打印时，详细实体每次变化都会覆盖更新当日记录（汇总 + 墨量 + 明细），保证始终为最新
- **触发判定**：以实体**状态值变化**判断（统计实体 state 为五项累计合计，详细实体为当日作业总数，每次打印都变化）
- **保存配置时主动采集一次**，避免空窗期

### 🖥️ 配置管理（系统配置 → 打印机配置）
- 支持多台打印机，配置项：名称、统计数据实体、当日详细数据实体
- 支持增删、主动重采（`/api/ha_data_store/printer/configs/recollect?name=xxx`）

### 📊 数据查询（API工具 → 打印数据查询分组）
| 查询类型 | 功能 |
|---|---|
| `printer_years` | 打印机有哪些年数据 |
| `printer_month_dates` | 指定月哪些日期有数据 |
| `printer_total` | 打印机合计数据（数据库 daylist 求和） |
| `printer_monthly_total` | 按年月统计合计数据 |
| `printer_daily_range` | 指定日期区间数据（含墨量 + 当日明细） |
| `printer_detail` | 指定日期详细数据 |

### 📈 系统监控（新增打印机监控）
- 统计卡片："🖨️ 打印机"（含健康状态点）
- 折叠区块：展示每台打印机的状态、墨量（K/C/M/Y）、当日/累计五项计数、统计与详细实体
- 随 `loadMonitor()` 自动刷新（含 15 秒自动刷新）

### 🔄 数据库迁移
- 自动为旧 `printer_daily` 表补充 `printer_jobs`/`updated_at` 列
- 自动迁移 `printer_id` → `name` 结构（重建配置表与主表）
- 自动删除旧版独立的 `printer_jobs` 明细表

### 依赖文件
| 文件 | 改动 |
|------|------|
| `printer.py` | 新增：建表、采集（状态值触发）、配置 CRUD、数据查询、主动重采 |
| `__init__.py` | 采集接入、实体白名单、API 注册、`_async_state_changed` 打印机独立分支 |
| `http_api.py` | 万能查询 `printer_*` 分发、`/monitor` 返回打印机监控数据 |
| `db_viewer.html` | 系统配置打印机子页面、API 打印数据查询分组、系统监控打印机卡片与区块 |

---

## 2026-06-27 — v2.5.1 小爱对话采集修复（LLM 连续对话 + other 字段）

### 🐛 Bug 修复

#### 1. `type: LLM` 连续对话未采集
- **现象**：小爱连续对话（大模型回复）的 AI 回复文本丢失，`ai_text` 为空，`type` 也未记录
- **根因**：`handle_state_changed_sync` 的 answers 遍历逻辑只处理 `type=="TTS"` 的项取 `tts.text`。当小爱返回 `type: LLM`（连续对话/大模型回复）时，回复文本在 `llm.text` 而非 `tts.text`，且该对话没有 TTS 类型的 answer，导致 `ai_text` 取不到，连续对话内容丢失
- **修复**：answers 遍历新增 `elif ans_type == "LLM"` 分支，从 `ans.get("llm").get("text")` 取 AI 回复文本。优先级：TTS > LLM（`not ai_text` 判断保证不覆盖已取到的 TTS 文本）。同时 LLM 也会作为 `conv_type` 事件类型记录
- **修复后采集效果**：
  - `user_text`：用户说的话（如"打开主卧灯和空调"）
  - `ai_text`：大模型回复（如"没问题。好的，先帮你打开主卧吸顶灯啦。"，来自 `answers[0].llm.text`）
  - `type`：`LLM`

#### 2. `other` 字段未存储 attributes JSON
- **现象**：`other` 字段本应存储完整 attributes 的 JSON 对象，但实际为空字符串
- **根因**：`json.dumps(attrs, ensure_ascii=False)` 在 attributes 包含不可 JSON 序列化的对象（如 `datetime`、自定义对象等）时抛 `TypeError`，被 `except` 捕获后 `other_text = ""`，导致 other 字段存空字符串。HA 实体的 attributes 可能包含各种不可序列化对象
- **修复**：`json.dumps` 增加 `default=str` 参数，任何不可序列化的对象都会被 `str()` 转成字符串，确保完整 attributes JSON 不丢失地存入 other 字段

### 依赖文件

| 文件 | 改动 |
|------|------|
| `xiaoai.py` | `handle_state_changed_sync` 新增 LLM 类型分支取 `llm.text`；`json.dumps` 增加 `default=str` 兜底；文件头注释同步更新 |

---

## 2026-06-26 — v2.5.0 音乐播放列表元数据探测（标签/封面/歌词）

### ✨ 新功能

#### 1. 播放列表子表化重构（`media_songs`）
- 原 `media_playlists.songs` JSON 列拆分为独立子表 `media_songs`，每首歌独立一行
- 子表字段：`id, playlist_id, sort_order, media_content_id, media_type, title, artist, album, duration, has_cover, has_lyrics, lyrics, extra, created_at, updated_at`
- 启动时自动迁移旧 `songs` JSON 数据到子表，迁移后删除旧列（SQLite 3.35+，旧版本保留空列）
- 删除播放列表时手动级联删除子表歌曲（SQLite 默认未开启外键约束）

#### 2. 音乐元数据探测（`media_meta.py` 新模块）
- `resolve_media_path(hass, media_content_id)`：解析 `media-source://` 路径到本地文件
- `probe_media_meta(full_path)`：用 mutagen 读取标签（title/artist/album）、时长、封面标志位、歌词
  - 歌词来源：同名 `.lrc` 文件优先（UTF-8/GB18030/GBK 自动探测编码），ID3 内嵌 USLT 兜底
  - 标签支持：MP3(ID3)、FLAC、M4A/MP4、OGG 多格式统一
- `extract_cover(full_path)`：提取内嵌封面图二进制（APIC/pictures/covr）

#### 3. RESTful 媒体 API（9 个接口）
| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/media/playlists?user=xxx` | 列出播放列表（含歌曲元数据，不含歌词） |
| POST | `/media/playlists` | 新建播放列表 |
| GET | `/media/playlists/{id}` | 获取播放列表详情 |
| PUT | `/media/playlists/{id}` | 重命名 |
| PUT | `/media/playlists/{id}?refresh_meta=1` | 整列刷新元数据（读文件） |
| DELETE | `/media/playlists/{id}` | 删除播放列表 |
| POST | `/media/playlists/{id}/songs` | 添加歌曲 |
| PUT | `/media/songs/{id}?refresh_meta=1` | 单首刷新元数据 |
| PUT | `/media/songs/{id}` | 调序 |
| DELETE | `/media/songs/{id}` | 删除歌曲 |
| GET | `/media/songs/{id}/lyrics` | 获取歌词文本 |
| GET | `/media/songs/{id}/cover` | 获取封面图（二进制，带 HTTP 缓存） |

- 保存播放列表/添加歌曲不触发探测，元数据刷新为显式操作
- GET 列表不带 `lyrics` 列（轻量化），前端播放时按需拉取歌词

#### 4. db_viewer 适配
- 播放列表管理页：表格增加歌曲数列、刷新元数据按钮
- API 工具新增「🎵 音乐媒体」分组，含 11 个接口 URL 生成器

---

## 2026-06-26 — v2.4.2 房间聚合查询 + 小爱采集白名单修复

### ✨ 新功能

#### 1. 房间聚合查询 API（3 个新查询类型）
- 新增 `aggregate_room_daily`：按 **房间 + 月份 + 数据类别（可多选）** 返回每日用电量/时长/条数汇总
- 新增 `aggregate_room_monthly`：按 **房间 + 年份 + 数据类别（可多选）** 返回每月用电量/时长/条数汇总
- 新增 `aggregate_room_yearly_daily`：按 **房间 + 年份 + 数据类别（可多选）** 返回每日汇总（一年 365 条）
- `category` 支持逗号分隔多选（`device,environment,attribute`），不填默认 `device`
- 各类别聚合字段：
  - `device`：`on_count` + `total_energy`（用电量）+ `total_duration`（时长秒）
  - `environment`：按 metric 分别聚合 `on_count` + `total_value`
  - `attribute`：按 attr_type 分别聚合 `on_count`（字段不固定不盲目求和）
- 返回按类别分组的 `summaries` 结构
- db_viewer「API工具」的「聚合查询」分组新增 3 个选项，复用复选框组（设备类/传感器类/属性提取）

**调用示例：**
```
GET /api/ha_data_store/query?type=aggregate_room_daily&room=客厅&month=2026-06&category=device,environment&key=xxx
GET /api/ha_data_store/query?type=aggregate_room_monthly&room=客厅&year=2026&category=device&key=xxx
GET /api/ha_data_store/query?type=aggregate_room_yearly_daily&room=客厅&year=2026&category=device,environment,attribute&key=xxx
```

### 🐛 Bug 修复

#### 1. 小爱对话运行时新增配置不生效
- **现象**：通过 db_viewer 配置小爱对话实体后，不重启 HA 则无任何对话记录采集，且日志无任何 `[xiaoai]` 输出
- **根因**：`XiaoaiConfigView.post` / `delete` 写入数据库后未调用 `_refresh_monitored`，导致内存白名单（`monitored_entities` + `xiaoai_entities` 两个集合）未刷新；`state_changed` 事件在 `_internal_state_listener` 关卡1 被 `entity_id not in monitored` 静默拦截，到不了采集函数
- **修复**：`post` 和 `delete` 成功后均补 `await _refresh_monitored(hass, self._db_path)`，与 `http_api.py` 中其他 6 处配置增删改保持一致，使运行时新增/删除配置立即生效
- **验证**：重启 HA 后采集成功，日志输出 `[xiaoai] 采集对话 entity_id=... conv_time=... user='...' ai='...'`

#### 2. 小爱闹钟/特殊事件 AI 回复采集不全
- **现象**：小爱定闹钟等含 `ALERT` 事件的对话，`ai_text` 始终为空，AI 回复文本丢失
- **根因**：`handle_state_changed_sync` 写死只取 `answers[0].tts.text`；而闹钟场景 `answers` 结构为 `[{type:ALERT, alert:{...}}, {type:TTS, tts:{text:...}}]`，`answers[0]` 是 ALERT 无 `tts` 字段，导致取空
- **修复**：改为遍历 `answers` 列表 —— 找 `type=="TTS"` 的项取 `tts.text`（普通对话 `answers[0]` 即 TTS 不受影响，闹钟场景能正确命中 `answers[1]`）
- **新增字段**：`xiaoai_conversations` 表新增两列（`ALTER TABLE` 迁移，老数据留空兼容）
  - `type`：事件类型键值。遍历 `answers` 取第一个 `type!="TTS"` 的项（如 `ALERT`）；全为 TTS 则存空，表示普通对话。前端可用映射表（如 `{ALERT:"闹钟"}`）解析为中文
  - `other`：完整 `attributes` 的 JSON（`ensure_ascii=False`），含 `answers` 详情（闹钟 datetime/circle、AI 原文）、`history`、`timestamp` 等，信息不丢失，便于后期追溯丢失内容
- **影响范围**：`handle_state_changed_sync` 采集逻辑、`INSERT ... ON CONFLICT DO UPDATE`（同时更新 ai_text/type/other）、两处 `SELECT` 查询（`query_history_sync` + `XiaoaiHistoryView._query`）
- **不做**：不回填老数据（answers 早已不在 HA 状态中，无法还原）；本次不改前端美化（仅保证新字段在 API 返回中出现）

### 依赖文件

| 文件 | 改动 |
|------|------|
| `http_api.py` | 新增 `_aggregate_room_by_period` 通用聚合方法 + 3 个查询方法 + type 列表/分发分支扩展 |
| `xiaoai.py` | `XiaoaiConfigView.post` / `delete` 补调 `_refresh_monitored`；`handle_state_changed_sync` 改为遍历 answers 取 TTS + 新增 type/other 字段；建表迁移补两列；两处 SELECT 加字段 |
| `db_viewer.html` | 「聚合查询」分组新增 3 个 option + 显示分支 + URL 构造 + 说明表格 + 参数文档 |
| `manifest.json` | 版本 2.4.1 → 2.4.2 |

---

## 2026-06-26 — v2.4.1 房间数据日历查询 + 桥接启动阻塞修复

### ✨ 新功能

#### 1. `room_data_dates` API — 房间指定月有数据日期查询
- 新增万能查询类型 `room_data_dates`，按 **房间 + 月份 + 数据类别（可多选）** 查询哪些日期有数据
- `category` 支持逗号分隔多选（`device,environment,attribute`），由用户勾选决定查哪几类
- 自动检测日期字段：设备类用 `on_time`，环境/属性类用 `datetime`；也可通过 `date_field` 自定义
- 返回合并去重后的日期列表，便于前端日历标记
- db_viewer「API工具」新增「房间数据日历」分组，含复选框（可多选）+ 全选按钮

**调用示例：**
```
GET /api/ha_data_store/query?type=room_data_dates&room=主卧&month=2026-05&category=device,environment&key=xxx
```

### 🐛 Bug 修复

#### 1. 桥接连接阻塞 HA 启动
- **现象**：HA 启动时报警告 `Something is blocking Home Assistant from wrapping up the start up phase`，等待 `BridgeConnection.run()` 任务
- **根因**：`bridge.py` 的 `BridgeConnection.start()` 使用 `hass.async_create_task()` 创建 WebSocket 长连接任务，而 `run()` 是无限循环，HA 启动流程会等待该任务完成
- **修复**：改用 `hass.async_create_background_task()`，后台任务不会被启动流程等待，与项目中其他长期任务（虚拟设备恢复、桥接延迟启动）保持一致

### 依赖文件

| 文件 | 改动 |
|------|------|
| `http_api.py` | 新增 `room_data_dates` 查询分支 + `_query_room_data_dates` 方法 |
| `db_viewer.html` | API工具新增「房间数据日历」分组、复选框组、全选函数、URL生成、参数文档 |
| `bridge.py` | `BridgeConnection.start()` 改用 `async_create_background_task` |
| `manifest.json` | 版本 2.4.0 → 2.4.1 |
| `README.md` | 查询类型表格新增 `room_data_dates` 行 + curl 示例 |

---

## 2026-06-21 — v2.3.0 固定功率计算用电量 + 前端SQL执行

### ✨ 新功能

#### 1. 固定功率计算用电量
- 设备类实体新增 `power_rating`（功率瓦特）配置，适用于无电量传感器的设备
- 每分钟根据 `power_rating × 时长` 计算 `energy_consumed`，保留2位小数
- 前端添加格式：`房间, 设备名称, entity_id, 功率值W`（第4参数纯数字→固定功率，含`.`→电量传感器）
- 已有设备可在数据库浏览器中直接编辑 `entity_configs.power_rating` 列
- SQL 弹窗提供一键补填历史数据的示例语句

#### 2. 前端SQL执行
- HA设备新增开关"前端执行SQL语句"（默认关，重启后强制关）
- 数据库浏览 toolbar 新增 `▶ 执行SQL` 按钮
- 支持 SELECT 查询（自动子查询分页）和非 SELECT 语句
- 开关开启后方可执行，控制权全在后端

### 🔧 其他优化

#### 1. `entity_configs` 表结构变更
- 新增列 `power_rating REAL NOT NULL DEFAULT 0`
- 自动迁移，无需手动操作

### 依赖文件

| 文件 | 改动 |
|------|------|
| `__init__.py` | `power_rating` 列；`_get_device_kwh_entities`；`_async_device_now_kwh_poll` 固定功率分支；`_update_device_off_record` 保留已有 energy_consumed；注册 DBViewerSQLView |
| `http_api.py` | `EntityConfigView`/`EntityConfigListView`/`EntityMonitorView` 增加 power_rating；新增 `DBViewerSQLView` |
| `switch.py` | 新增 `HaDataStoreDbSQLSwitch` |
| `config_flow.py` | `power_rating` 列检查 |
| `db_viewer.html` | 添加设备格式扩展；SQL 弹窗+分页+历史补填提示；实体列表显示功率值 |
| `manifest.json` | 版本 2.2.3 → 2.3.0 |
| `const.py` | 新增 `VERSION` 常量 |

---

## 2026-06-16 — v2.2.3 删除实体自动清理 + bug 修复

### 🐛 Bug 修复

#### 1. 属性轮询 `last_attr_poll` 使用 `(entity_id, attr_type)` 联合 key
- **问题**：`last_attr_poll` 和 `last_attr_poll_minute` 仅以 `entity_id` 为 key，同一 entity 多个 `attr_type`（如 `ele_year`、`ele_month`、`ele_day`）时，第一个类型采集后覆盖了其他类型的时间记录，导致后续类型被跳过
- **修复**：key 改为 `f"{entity_id}|{attr_type}"` 联合字符串，每个 attr_type 独立计时

#### 2. 删除文件源/API源/桥接/虚拟设备后实体残留不可用
- **问题**：从前端删除文件源、API源、桥接连接、桥接实体、虚拟设备时，只删除了配置和 device_registry，entity_registry 和 state_machine 中的实体仍然存在，显示为"不可用"
- **修复**：在删除配置前，遍历 entity_registry 中关联的实体，依次执行 `async_remove` + `hass.states.async_remove`，彻底清除

### 🔧 其他优化

#### 1. 属性轮询增加日志输出
- 跳过时记录 `info`/`debug` 级别日志，采集完成时记录 `info` 日志，便于排查去重问题

#### 2. 日志查看器倒序显示
- `db_viewer.html` 日志页面改为最新日志在最上方，搜索过滤后同样倒序

#### 3. Sub-tab 角标显示个数
- `db_viewer.html` 系统监控页面各子选项卡右上角增加红色数字角标，显示当前项目数，数量为 0 时自动隐藏

#### 4. 受监控实体白名单（仅监听用户配置的实体）
- **问题**：`state_changed` 监听器注册了 HA 全部实体变化，每次变化都查一次 SQLite，未配置的实体空耗性能，且 HA 关闭时刷屏 `Executor shutdown` 错误
- **修复**：新增 `_refresh_monitored_set_sync` 函数，启动时从 `entity_configs`、`vacuum_configs` 查询用户主动配置且已启用的实体，构建内存白名单 `Set[str]`
- `_internal_state_listener` 和 `_vacuum_state_listener` 入口做 O(1) 集合检查，不在白名单中直接跳过
- 白名单初始化移至监听器注册之前，避免空窗期穿透
- 日志信息从"全量监听"改为"白名单过滤"，与实际行为一致
- 用户通过 API 新增/修改/删除配置后自动刷新白名单，并输出日志确认

#### 5. 属性提取卡片颜色优化
- 系统监控页属性提取的健康指示改为二级（红色=有离线，绿色=无离线），移除中间的橙色状态

---

## 2026-06-15 — v2.2.2 state_attr 增强 + 目标温度追踪

### 🆕 新增功能

#### 1. `state_attr` 新增当前室温 `cur_temp` 字段
- 每个状态 entry 增加 `cur_temp` 记录空调回读的实时室温
- 空调不提供 `current_temperature` 属性时，`cur_temp` 填入 `"--"` 占位
- 后端 `_extract_climate_state_attr` 提取逻辑已更新

#### 2. 目标温度变化触发 state_attr 记录
- 之前仅 HVAC 模式变化时记录，现在**目标温度（`temperature`）变化时也记录**
- 每次调温在 `state_attr` 中追加一条新 entry
- 去重逻辑同步升级：`state` 和 `temp` 都相同时才跳过（风速/预设/摆风变化仍不记录）

#### 3. 前端 `usage-card.js` 状态时间线适配
- 弹窗折叠详情每行增加 `室××°C` 显示当前室温
- 显示格式：`设26°C · 室25.5°C · 风自动`

### 🐛 Bug 修复

#### 1. `_recheck_unclosed` 条件限制导致重复开记录
- **问题**：`_recheck_unclosed` 被包裹在 `if unclosed:` 内，当运行记录 `on_time` 不是今天时（午夜拆分未执行场景），`_check_unclosed` 返回空导致重查被跳过
- **修复**：去掉 `if unclosed:` 限制，`_recheck_unclosed` 无条件执行，查询全量未关闭记录

---

## 2026-06-15 — v2.2.1 Bug 修复

### 🐛 Bug 修复

#### 1. `state_attr` 误写入非空调设备
- **问题**：`_async_state_changed` 中 `_append_state_attr_to_record` 未做 domain 判断，`binary_sensor` 等设备也被写入空调状态数据
- **修复**：on→on、on→off 两处追加操作均增加 `_get_entity_domain(entity_id) == "climate"` 检查

#### 2. 重启后所有设备重复开记录
- **问题**：HA 重启后 `old_state = None`，走 off→on 分支。修正旧记录后 `_recheck_unclosed` 查询带日期限制（`on_time LIKE today%`），多日运行未跨夜的设备查不到 → 重复 INSERT
- **修复**：`_recheck_unclosed` 去掉 `AND on_time LIKE` 日期过滤，改为查询全部未关闭记录

#### 3. db_viewer 编辑 `state_attr` 保存失败
- **问题**：前端 `val === ''` 转 `null`，后端 UPDATE 设置 `NULL` 触发 `NOT NULL` 约束
- **修复**：`DBViewerUpdateView` 中 `state_attr` 列为 `null` 时自动转为 `'[]'`

---

## 2026-06-15 — v2.2.0 空调状态采集 + 分钟级功率快照（2026-06-15）

### 🆕 新增功能

#### 1. `device_history` 表新增 `state_attr` 字段（空调状态变化链）
- 空调 HVAC 模式变化时（`off→cool`、`cool→heat`、`heat→off` 等）自动记录
- JSON 数组格式存储完整开机周期内的状态变化：
  ```json
  {"t":"2026-06-15 14:04:21","state":"cool","temp":26.0,"fan":"自动","preset":"","swing":"off"}
  ```
- 包含：时间、HVAC 模式（`state`）、温度、风速、预设模式、摆风
- **去重**：仅 HVAC 模式真实变化时写入，属性变化自动跳过
- 跨天午夜拆分时新记录也自动携带当前状态
- API 返回时自动解析为原生 JSON 数组（前端直接使用）

#### 2. `device_history` 表新增 `now_kwh` 字段（分钟级功率快照）
- 对配置了 `power_entity` 的设备，每分钟自动写入传感器当前值
- **只保留最新一条**：设备关闭时 `now_kwh` 自动清空
- 关机时若 `off_power` 未被捕获，自动用 `now_kwh` 补齐计算能耗
- 前端直接取 `record.now_kwh` 作为实时读数

### 🐛 Bug 修复

#### 1. 关机时 `off_power` 缺失导致能耗为 0
- **问题**：关机时 `_get_power_value()` 返回 `None`，`energy_consumed` 无法计算
- **修复**：`_update_device_off_record` 中查询记录时一并取出 `now_kwh`
- 若 `off_power` 为空且 `now_kwh` 有值，自动补齐为关机读数
- 回退方案：改用分钟级 `now_kwh` 而非 recorder 历史查询（更可靠）

### 🔧 其他优化

#### API 返回 `state_attr` 预解析为 JSON 数组
- `_query_device_history` 新增 `_parse_records_state_attr` 方法
- 所有 device_history 查询出口（按日/按月/无时间范围）均经过预解析
- 前端直接用 `record.state_attr[0].state`，无需手动 `JSON.parse`
- 空记录自动转为空数组 `[]`

---

## 2026-06-11 — 多项功能新增与修复

### 🆕 新增功能

#### 1. 虚拟设备：媒体播放器 & 音响
- 新增 `VirtualMedia` 类 → 媒体虚拟设备（完整影音播放器）
- 新增 `VirtualSpeaker` 类 → 音响虚拟设备（专注音频体验）
- 两个类型均使用 `media_player` 域
- 支持：播放/暂停/停止/开关机、音量控制、音源切换、音效模式、上下曲等
- 新增 `media_player.py` 平台文件，注册 `async_add_media_player` 回调
- `PLATFORMS` 列表新增 `media_player`

#### 2. 属性查询：`attr_daily` 按日分组
- 新增查询类型 `attr_daily`：按天分组返回指定月份属性记录
- 仅支持单表查询
- 日期字段自动从表列检测（优先 `datetime` → `day` → `on_time` 等）
- 支持手动指定 `date_field` 参数
- 前端 API 工具新增 `📅 属性按日分组` 选项
- 新增后端接口 `/api/ha_data_store/table_columns` 获取表列名

### 🐛 Bug 修复

#### 1. `attr_history` 多表查询修复
- **问题**：多选属性表时，逗号分隔的 `attr_type` 被整体当作一个表名处理
- **修复**：拆分后逐个加 `attr_` 前缀分别查询，合并结果
- 单表保持旧返回格式兼容，多表返回按表分组格式

#### 2. `attr_history` 支持 `start`/`end` 日期范围
- **问题**：`start`/`end` 参数虽已提取但未被使用
- **修复**：加入 WHERE 条件，支持 `datetime >= start AND datetime <= end 23:59:59`

#### 3. 传感器小数位数改为3位
- **问题**：传感器数值统一保留2位小数，精度不足
- **修复**：将 `round(value, 2)` 全部改为 `round(value, 3)`
- 修改位置：`_write_env_metric_record` 和采集循环中的数值提取

### 🔧 其他优化

#### 前端 API 工具界面改进
- `attr_daily` 模式下日期字段改为文本输入框，用户可手动填写字段名
- 不影响其他查询类型的下拉选择功能
- 属性类型复选框显示完整表名（`attr_xxx`）
