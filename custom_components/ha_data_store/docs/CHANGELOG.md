# 更新日志

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
