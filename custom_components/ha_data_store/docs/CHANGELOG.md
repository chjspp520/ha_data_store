# 更新日志

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
