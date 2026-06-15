# 更新日志

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
