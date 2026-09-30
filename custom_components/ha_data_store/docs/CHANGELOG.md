# 更新日志

## 2026-09-30 — v4.15.2 修复实体映射面板把「内置预设」与「类型级」串在一起

> 前端展示 bug：流量实体的映射面板混进了通话的字段。
> 后端采集一直是对的（`_merge_entity_mapping` 是整体覆盖），只有面板显示错。

### 一、现象

打开 `sensor.17792405320_traffic` 的字段映射面板，出现：

| 目标列 | 显示的源字段 | |
|---|---|---|
| `time` | `datetime` | ✅ 预设 |
| `duration` | `duration_seconds` | ✅ 预设 |
| `traffic_usage` | `volume_mb` | ✅ 预设 |
| `my_number` | `=17792405320` | ✅ 预设（动态） |
| `party_number` | `phone_number` | ❌ 通话的 |
| `party_place` | `number_location` | ❌ 通话的 |
| `location` | `location` | ❌ 通话的 |
| `msg_type` | `type` | ❌ 通话的 |
| `call_type` | `call_type` | ❌ 通话的 |
| `party_isp` / `party_coordinate` / `location_coordinate` | | ❌ 通话的 |

规律很清楚：**对的是预设项，错的全来自类型级**（库里类型级 `field_mapping` 存的是通话那份）。

### 二、根因

`aeOpenEntityMapping()` 里的源字段反查写了**逐列回退**：

```js
let src = '';
Object.keys(own).forEach(k => { if (own[k] === target) src = k; });       // 实体级
if (!src) Object.keys(effMap).forEach(k => { if (effMap[k] === target) src = k; });  // 预设
if (!src) Object.keys(typeMap).forEach(k => { if (typeMap[k] === target) src = k; }); // 类型级 ❌
```

「实体级 > 内置预设 > 类型级」是**整体**关系 —— 上层存在即完全取代下层。
逐列回退等于把三层**合并**了：预设里没有 `party_number`，就去类型级里取到了通话的 `phone_number`。

> 一个佐证：`time` / `duration` / `traffic_usage` 这几列**两边都有**，
> 逐列回退时预设先命中所以显示正确；而 `party_*` 只有类型级有，就串进来了。

### 三、修复

抽出两个职责单一、可测的纯函数，把"整体三层"写死在一处：

```js
// 三层整体取一层
function aeEffectiveMapping(own, presetMapping, typeMap) {
  if (own && Object.keys(own).length) return { map: own, scope: 'entity' };
  if (presetMapping && Object.keys(presetMapping).length) {
    return { map: presetMapping, scope: 'preset' };
  }
  return { map: typeMap || {}, scope: 'type' };
}

// 只在生效映射里反向找源字段（同一列多来源时取第一个）
function aeSourceOf(target, effMap) {
  const keys = Object.keys(effMap || {});
  for (let i = 0; i < keys.length; i++) {
    if (effMap[keys[i]] === target) return keys[i];
  }
  return '';
}
```

`aeOpenEntityMapping()` 的渲染简化为：

```js
body.innerHTML = targets.map(([target, type]) =>
  _aeMappingRowHtml(aeSourceOf(target, effMap), target, type, target)).join('');
```

**顺带修正**：原来同一目标列有多个源字段时取**最后一个**（`forEach` 覆盖），现在取**第一个**。

文案同步：「清除」按钮改为「清除自定义映射（恢复内置预设 / 继承类型级）」，
确认框说明会回落到哪一层；表格上方说明补上"三层是整体关系"。

### 四、验证（吸取教训）

**上一版只验证了后端函数，没验证前端渲染，所以漏掉了这个 bug。**
这次把前端函数（`COMM_FIELD_DEFS` / `COMM_PRESETS` / `commExtractPhone` /
`commPresetOf` / `aeEffectiveMapping` / `aeSourceOf`）从 HTML 里抽出来，
放进 node 真实执行，直接断言**面板每一列显示什么**：

- **复现用户场景**：`typeMap` 设为通话那份 → 断言流量面板的
  `party_number` / `party_place` / `location` / `msg_type` / `call_type` /
  `location_coordinate` / `party_isp` / `party_coordinate` 等 **11 列全为空**
- 三个实体的**非空项数**分别为 7 / 9 / 13，且**逐项等于用户给定的映射**
- 无预设实体 + 类型级 → 正确继承（`call_time→time` 等）
- 实体级非空 → **只**走实体级（预设项显示为空、类型级项不混入）
- 全空 → 面板全空
- 源码断言：`async function aeOpenEntityMapping` 里不再出现
  `Object.keys(typeMap).forEach`，已改用 `aeSourceOf` + 生效映射
- 全部 `<script>` 块语法检查

约 90 项断言全通过。

> 版本号 → `4.15.2`。需重启 HA 生效；前端刷新页面即可，无需清缓存。

---

## 2026-09-30 — v4.15.1 修正内置预设的映射 + 修复保存时误报「数组路径不能为空」

> 两个问题：保存被无效校验拦下（连带两处会抛异常/清空配置的缺陷），
> 以及三条内置预设的映射与真实数据形状不符。

### 一、保存被误拦（v4.14.0 的连带缺陷）

**主症状**：每次保存都提示

```
数组路径不能为空（列表展开/混合/通讯模式必填）（共 2 项，已中止保存）
```

**根因**：v4.14.0 把这四项下移到实体行后，`saveAttrEdit()` 里仍在读
`document.getElementById('attrEditArrayPath')` —— 元素已不存在，`val(null)` 得到 `''`，
于是恒为"空"。

**同处另外两个缺陷**（本次一并修掉，否则保存仍会异常）：

| 位置 | 问题 | 后果 |
|---|---|---|
| `defUpdates` 计算 | `arrPath` / `keyField` / `compareLimit` / `decimalPlaces` 变量已不存在，仍被引用 | `ReferenceError`，保存中断 |
| 类型级 `field_mapping` diff | 通讯模式不再渲染类型级映射表 → `mapping` 恒为 `{}` | 会**把类型级映射覆盖成空**，抹掉兜底默认值 |

**修复**：

- 类型级的这四项**不在编辑弹窗里改动**（保留库中原值，继续作为实体级的兜底默认）
- 通讯模式跳过类型级的 `field_mapping` / `field_types` diff（`if (!isComm)` 包裹）
- 校验改为按 **「实体级 → 内置预设 → 类型级」三级**判断，三者都取不到才报错，
  且错误信息指明具体实体，例如：
  `实体「sensor.x_sms」的采集节点为空（实体级、内置预设、类型级均未设置）`

### 二、修正三条内置预设

按实际数据形状重写（v4.15.0 的猜测有几处不符）：

| | `*_calls` | `*_sms` | `*_traffic` |
|---|---|---|---|
| 采集节点 | `通话流水清单` | `短信记录` | `上网会话清单` |
| 唯一键 | `call_time` | `datetime` | `datetime` |
| 去重窗口 | 1000 | 1000 | 1000 |
| `channel` | **`语音`** | `短信` | `流量` |

**通话（13 项）**：

```python
phone_number -> party_number      number_location -> party_place
call_time    -> time              location        -> location
type         -> msg_type          call_type       -> call_type
duration     -> duration          fee             -> cost
location_coordinate -> location_coordinate
number_isp   -> party_isp         number_location_coordinate -> party_coordinate
=语音        -> channel
```

> 与 v4.15.0 的差异：`type`（呼叫/接听，是**方向**）应进 `msg_type`；
> 源字段里的 `call_type`（国内通话/漫游）才是 `call_type` 列。
> `channel` 由 `通话` 改为 `语音`。

**短信（9 项）**：`phone_number→party_number`、`datetime→time`、`type→msg_type`、
`fee→cost`、`number_isp→party_isp`、`number_location_coordinate→party_coordinate`、
`number_location→party_place`、`=短信→channel`

**流量（7 项）**：`datetime→time`、`duration_seconds→duration`、`fee→cost`、
`volume_mb→traffic_usage`、`business_type→traffic_type`、`=流量→channel`
（流量无对端号码，不含 `party_*`）

### 三、`my_number` 从实体 ID 自动提取

三条预设都会从实体 ID 里提取电话号码，生成 `my_number` 的**固定值**映射：

```
sensor.17792405320_sms  →  "=17792405320" -> my_number
```

- `extract_phone(entity_id)`：取实体 ID **末段**的 7~15 位连续数字
  （兼容手机号 `17792405320` 与带区号固话 `02988888888`）
- 提取不到（如 `sensor.temperature`）则不生成该项，映射里就没有 `my_number`
- `my_number` **不写进** `COMM_PRESETS` 常量 —— 保持前后端可做静态逐项比对，
  由 `preset_field_mapping(entity_id)` 运行时叠加

前端同样有 `commExtractPhone()` 与 `commPresetOf()` 的运行时叠加，行为一致。

### 四、验证

约 80 项断言，其中：

- **逐项核对用户给定的映射**：三条预设的每个源字段→目标列、唯一键、采集节点、
  `channel` 默认值、映射项数（13 / 9 / 7）全部逐条断言
- **目标列不重复**：同一列只能有一个来源（否则后者覆盖前者）
- **`my_number` 提取 6 种情形**（手机号 / 固话 / 无号码 / 空值 / 短数字不误取 / 无号码时不含该项）
- **目标列合法性**：三条预设的每个目标列都在 `COMM_COLUMNS` 白名单内、都含必填 `time`
- **前后端一致性**：前端镜像的 suffix / 节点 / 唯一键 / 全部映射项逐条比对
- **保存校验修复**：限定在 `saveAttrEdit` 函数体内断言（不再读已删元素、
  不再对类型级四项做 diff、不再报那两个错误）
- `_merge_entity_mapping` 实测：预设生效（节点 / 唯一键 / `my_number` 固定值 / `channel`）
- 全部 `<script>` 块语法检查

> 版本号 → `4.15.1`。需重启 HA 生效。

---

## 2026-09-30 — v4.15.0 通讯采集内置映射预设（按实体后缀开箱即用）

> 为 `shaobo_pocket_carrier` 集成的三类实体内置「采集节点 + 字段映射 + 唯一键」，
> 无需手工配置。

### 一、预设内容

按实体 ID **末段后缀**识别（取 `.` 之后的部分判断，避免误伤）：

| 后缀 | 采集节点 | 唯一键 | 字段映射 |
|---|---|---|---|
| `*_calls` | **`通话流水清单`** | `call_time` | `call_time→time`、`phone_number→party_number`、`type→call_type`、`duration→duration`、`location→location`、`location_coordinate→location_coordinate`、`number_location→party_place`、`number_isp→party_isp`、`number_location_coordinate→party_coordinate`、`fee→cost`、`=通话→channel` |
| `*_sms` | **`短信记录`** | `datetime` | `datetime→time`、`phone_number→party_number`、`type→msg_type`、`number_location→party_place`、`number_isp→party_isp`、`number_location_coordinate→party_coordinate`、`fee→cost`、`=短信→channel` |
| `*_traffic` | **`上网会话清单`** | `datetime` | `datetime→time`、`volume_mb→traffic_usage`、`business_type→traffic_type`、`duration_seconds→duration`、`fee→cost`、`=流量→channel` |

要点：

- 三个实体的属性里都有**多个数组**（`按天汇总` / `最近一条` / `排序选项` / `筛选选项` 等），
  预设一律选**明细清单**那个节点
- `type` 在通话里是「呼叫/接听」→ `call_type`；在短信里是「发送/接收」→ `msg_type`
- `duration`（通话，"9秒"）与 `duration_seconds`（流量，已是秒）都能被时长解析器处理
- `fee`（"0元" / 0.1）→ `cost`，采集时自动转数值
- `channel` 用**固定值**写法（`=通话`），既有信息量也保证映射非空
- 流量无对端号码，故**不含** `party_*` 映射

### 二、优先级

```
实体级配置  →  内置预设（按后缀）  →  类型级配置
```

- 已手工配过的实体不受影响（实体级非空即优先）
- 新加的 `*_sms` / `*_traffic` / `*_calls` 默认就能采到数据
- 想退回预设：清掉该实体的实体级配置

### 三、实现

**新增 `comm_presets.py`**：

- `COMM_PRESETS`：三条预设（后缀 / 名称 / 节点 / 唯一键 / 窗口 / 字段映射）
- `detect_preset(entity_id)`：`eid.rsplit(".", 1)[-1].lower().endswith(suffix)` ——
  只看**末段**，所以 `sensor.sms_gateway_temperature` 不会命中 `_sms`
- `preset_summary(entity_id)`：给前端的摘要（`available` / `label` / 映射…）

**采集链路**：

- `_merge_entity_mapping(row, mode="")` 新增 `mode` 参数；当 `mode == "comm"` 时
  插入预设层。`_get_all_attr_entities` 传入 `atd.mode`
- `_async_attr_event` 的 `cfg` 组装同样在「实体级 → 类型级」之间插入 `_preset`
- 预设不可用（导入失败等）时 `try/except` 兜底为空，**不影响采集**

**接口**：`AttrEntityMappingView` GET 在通讯模式下返回 `preset`（`preset_summary` 的结果）。

**前端**：内置一份镜像 `COMM_PRESETS` + `commPresetOf()`：

- 实体表格「字段映射」列在无实体级配置时显示绿色 **内置：通话记录**
- 点「编辑」打开映射面板：若实体级为空，**按预设预填**各行源字段，
  标题提示「内置预设：X，按实体 ID 后缀 `_xxx` 自动套用」，节点也显示预设值
- 反向定位源字段的顺序：实体级 → 预设 → 类型级
- 保存即把当前值固化为实体级配置（此后不再依赖预设）

### 四、验证

70 项断言，其中：

- **后缀识别 7 种**（三个命中 + 不相关 + 空值 + `sms_gateway_temperature` 不误伤 + 大小写）
- **目标列合法性**：三条预设的每个目标列都在 `COMM_COLUMNS` 白名单内、都含 `time`、都含 `channel`
- **对照真实结构 20 项**：逐项核对用户提供的三个实体属性，确认节点名与映射关系
- **前后端一致性**：前端镜像的 suffix / arrayPath / keyField / 全部映射项逐条比对
- **三层优先级**：实体级空→用预设；实体级配了→覆盖预设；后缀不匹配→用类型级；
  非通讯模式→不套预设
- 接线（`mode` 传参、`_preset` 兜底、接口返回、前端标识文案）

> 版本号 → `4.15.0`。需重启 HA 生效。

---

## 2026-09-30 — v4.14.1 修正「唯一键字段」的候选来源与语义说明

### 一、语义确认：填的是源字段名，不是数据库列名

`key_field` 在采集侧的两处用法：

```python
key_target_col = field_mapping.get(key_field, key_field)   # 该源字段映射到哪一列
key_value = _extract_nested_value(element, key_field)      # 从数组元素里取值
```

所以它必须是**该实体采集节点下数组元素里的字段名**（`call_time` / `b_when` / `net_time`…），
经字段映射落到某个目标列（如 `time`）后，去重键用的是**那一列**。

### 二、问题：候选是类型级的

实体表格该列绑定的是 `list="attrEditFieldList"`，而 `attrEditFieldList` 由
`_aeLoadEntityFields()` 用**类型级** `array_path` 探测出来 —— 对 `sensor.x_sms`
这一行，给出的是 `call_time` 这类**别的实体的字段名**，必须手工敲。

### 三、修复

| 项 | 改动 |
|---|---|
| datalist | 每行一个 `#aeKeyList_<i>`，输入框绑定各自的（不再共用全局） |
| 候选计算 | `_aeLoadEntityFields()` 为每个实体按其**自己的**采集节点（行内下拉优先 → `e.array_path` → 类型级）算 `ctx.entityFieldCands[entity_id]` |
| 填充 | `_aeUpdateDatalists()` 把候选写进各行 datalist |
| 联动 | 新函数 `aeOnEntityNodeChanged(sel)`：某行节点变化后 300ms 防抖重探并刷新候选（实体表格节点下拉已挂 `onchange`） |
| 文案 | 占位符改为「源字段名（该节点下）」；hint 明确「**不是数据库列名**」、去重取值方式，以及填错时的兜底（回退时间列 + 日志，见 v4.9.1） |

### 四、实测

用 node 跑 `_aeFieldCandidates` 验证三个节点各自返回各自的字段集：

| 节点 | 候选 |
|---|---|
| `通话流水清单` | `['call_time', 'callee', 'duration']` |
| `短信清单` | `['fee', 'peer', 'sms_time']` |
| `上网会话清单` | `['apn', 'net_time', 'volume']` |

互不混入（`call_time` 不出现在短信的候选里，反之亦然）；节点用错时按既有规则
退回顶层标量，不会静默给出错误候选。

> 版本号 → `4.14.1`。需重启 HA 生效。

---

## 2026-09-30 — v4.14.0 采集参数全部下移到实体级 + 编辑弹窗支持最大化

> 承接 v4.13.0：把 `key_field` / `compare_limit` / `decimal_places` 也做成实体级，
> 「采集参数」区随之清空；类型级通讯映射区移除；弹窗可最大化。

### 一、参数下移到实体级

三项都走 v4.8.1 建立的「实体级非空优先、否则回退类型级」机制：

| 参数 | 哨兵值（= 未设置） | 说明 |
|---|---|---|
| `key_field` | 空串 | 去重键字段 |
| `compare_limit` | `<= 0` | 去重窗口（回查最近 N 条） |
| `decimal_places` | `< -1`（即 -2） | 小数位数；`-1`（不限）与 `0~6` 都是**合法值**，故哨兵取 `-2` |

**后端**：

- `entity_configs` 新增三列（建表 + 迁移 + 重建表分支的列清单）。
  迁移的列定义列表改成 `(列名, DDL)` 元组，以容纳不同的默认值与类型
- `_merge_entity_mapping` 合并三项；数值项用上面的哨兵规则
- `_get_all_attr_entities` 查询增加三个别名；`_async_attr_event` 的 `cfg` 同样实体级优先
- `_attr_collect_for_entity` 的 `decimal_places` 改为 `cfg` 优先、未设置才查 `attr_type_defs`
- `AttrEntityMappingView`：GET 返回三项 + `type_*` 对应值；POST 接收；UPDATE 写入；
  `scope` 判断计入三项

**前端**：

- 「采集参数」区只剩类型名 / 模式（只读），其余全部移除（含节点选择面板 ——
  实体表格的节点下拉已经覆盖该能力）
- 实体表格扩到 10 列，三列输入框的 `placeholder` 显示继承值（`继承：call_time`）
- `_aeSyncEntityInputs` / `saveAttrEdit` / `aeSaveEntityMapping` 同步读写三项
- 新增实体时先 `EntityConfigView` 建行，再按需补一次 `AttrEntityMappingView`

### 二、移除类型级通讯映射区

`isComm` 分支不再渲染「通讯字段映射（目标列固定 16 列）」整块 —— 通讯模式一律在
每个实体行的「编辑」里配置。类型级 `field_mapping` 仍在库里作为回退默认值，
但界面上不再暴露。**非通讯模式**（fields / list / multi）的类型级映射区**保持不变**。

### 三、编辑弹窗最大化

标题栏新增 `⛶ 最大化 / ⤡ 还原` 按钮（`toggleAttrEditMax()`）：

- 最大化：`width/maxWidth = 99vw`、`height/maxHeight = 96vh`、内边距收窄
- 还原：回到 `90% / maxWidth 1040px / maxHeight 88vh`
- 配合 `#attrEditBody` 既有的 `overflow:auto; flex:1 1 auto` 正常滚动
- 状态保留在 `attrEditMaxed`，关闭弹窗不重置（下次打开仍是上次的尺寸）

### 四、兼容性

- 旧配置（三项只在类型级）行为不变 —— 实体级为空即回退
- 类型级 `array_path` / `key_field` 仍是必填（新建类型时），作为新实体的默认值

> 版本号 → `4.14.0`。需重启 HA 生效。

---

## 2026-09-30 — v4.13.0 采集节点改为「每个实体各自指定」

> `array_path` 从类型级扩展为「类型级默认 + 实体级覆盖」，与 `field_mapping` 同一套规则。
> 前端在实体表格里直接给出「采集节点」下拉列。

### 一、问题

`array_path`（要采集的数组节点）原先只在 `attr_type_defs`，同一类型下所有实体共用。
但实际上同一类型的各实体数据形状不同：

| 实体 | 需要采集的节点 |
|---|---|
| `sensor.xxx_traffic` | `data.traffic` |
| `sensor.xxx_sms` | `data.sms.list` |
| `sensor.xxx_calls` | `data.calls` |

只能把节点填成类型级 → 三个实体里有两个采不到数据。

### 二、后端

**表结构**：`entity_configs` 新增 `array_path TEXT NOT NULL DEFAULT ''`
（建表 + `_migrate_database` 逐列补齐 + 重建表分支的 `new_columns_def` / `copy_cols`）。

**采集链路**（复用 v4.8.1 的实体级覆盖机制）：

- `_merge_entity_mapping(row)` 增加合并 `ec_array_path`：
  `entity_configs.array_path` 非空 → 用它，否则回退 `attr_type_defs.array_path`
- `_get_all_attr_entities` 的查询增加 `ec.array_path AS ec_array_path`
- `_async_attr_event`（event 模式）的 `cfg` 组装同样改为实体级优先

**接口** `AttrEntityMappingView`：

| 项 | 变化 |
|---|---|
| GET | SELECT/返回 `array_path`，并新增 `type_array_path`；`scope` 计入节点 |
| POST | 接收 `array_path`（空 / `none` / `-` = 清除，回退类型级） |
| SQL | `UPDATE ... SET field_mapping = ?, field_types = ?, array_path = ?, updated_at = ?` |

### 三、前端（`db_viewer.html`）

**实体表格新增「采集节点（数组路径）」列**：

```
实体 | 房间 | 采集方式 | 间隔(分钟) | 采集节点（数组路径） | 字段映射 | 操作
```

- `_aeLoadEntityFields()` 逐实体探测属性树，收集 `ctx.entityNodes[entity_id] = [{path, length}]`
- 下拉选项 = 该实体的数组节点（带元素数）；首项 `（继承类型级：xxx）` 值为 `""`
- 当前值不在候选中时（实体不可用 / 手工填过），追加一个 `（当前，未探测到）` 选项避免丢值
- **移除了原来的 `if (ok >= 2) break;`** —— 每个实体的下拉都要各自的节点列表，
  少探测一个实体那一行就只能选「继承类型级」；该处已加注释说明

**点「编辑」→ 映射面板**：

- `aeOpenEntityMapping(idx)` 改为 `async`，标题显示该实体的采集节点
  （行内下拉优先，因为可能刚改还没保存）
- 新增 `aeLoadEntitySourceFields(e, node)`：按该实体 + 该节点调 `entity_state` 探测，
  用 `_aeFieldCandidates(attrs, node)` 取**该节点下元素的字段**，临时替换
  `#attrEditFieldList` 候选，并显示「源字段候选：N 个（来自节点 xxx）」
- `aeCloseEntityMapping()` 调 `_aeUpdateDatalists()` 还原类型级候选
- 新增 `_aeEntityRowNode(idx)` 读某行的节点下拉值；`aeSaveEntityMapping` 一并提交
  `array_path` 并同步回 `e.array_path`

**保存流程**：

- `_aeSyncEntityInputs()` 同步读 `select[data-role="array_path"]`
- `saveAttrEdit()` 收集 `arrPathEnt`；与 `orig.array_path` 不同则
  `entUpdates.push({column: 'array_path', ...})`
- 新增实体时 `entAdds` 带上 `arrayPath`；`EntityConfigView` 不管这项，
  建行后再补一次 `AttrEntityMappingView`（失败会明确报错）

### 四、兼容性

- 类型级 `array_path` 保留为默认值，旧的单实体配置行为不变
- 清空实体级节点即回退类型级；历史数据不受影响

> 版本号 → `4.13.0`。需重启 HA 生效。

---

## 2026-09-30 — v4.12.1 采集节点与字段映射候选联动

> 修正 v4.12.0 的方向：需求不是「能挑节点」，而是**指定节点后，映射时能选该节点下的字段**。

### 一、真正的缺口

| 环节 | v4.12.0 的情况 |
|---|---|
| 挑节点 | ✅ 有了面板与下拉 |
| 挑完节点 → 映射候选 | ❌ **没变**，仍是一堆无关字段 |

两处根因：

1. `_aeFieldCandidates(attrs, arrayPath)` 在指定节点时，除了该节点下元素的字段，
   **还会把顶层标量属性一并塞进候选** —— 映射时要在无关字段里翻
2. `_aeLoadEntityFields()` 用 `if (ctx.fieldCands.indexOf(c) < 0) push(c)` **累加**候选；
   而且**改 `array_path` 根本不会触发重新探测**，改了节点候选也不刷新

### 二、改动

**`_aeFieldCandidates` 改为「节点优先」**：

```js
if (arrayPath) {
  const arr = getNestedValue(attrs, arrayPath);
  if (Array.isArray(arr) && arr.length && arr[0] && typeof arr[0] === 'object') {
    Object.keys(arr[0]).forEach(push);
    return out;          // ← 直接返回，不再混入顶层标量
  }
}
// 未指定节点（或节点下无元素样本）才退回顶层标量
```

**`_aeLoadEntityFields` 改为整体重建**：候选先收集到局部数组，最后一次性赋值
`ctx.fieldCands = fieldCands`（不再累加），并额外统计 `ctx.nodeFieldCount`
（当前节点下元素的字段数，用于提示）。

**新增联动**：

| 新增 | 作用 |
|---|---|
| `aeOnArrayPathChanged()` | 数组路径变化 → 350ms 防抖 → 重新探测 → 重建候选 → 刷新 datalist / 提示 / 下拉 |
| `#aeArrayPathSelect` 下拉 | 列出属性树里**所有数组节点**（带元素数），选中即填入并联动 —— 对齐创建流程的 `#attrArrayPath` |
| `_aeSyncArrayPathSelect(tree)` | 填充/回显该下拉（按 `list` 类型过滤，按路径排序，选中值高亮） |
| `aePickArrayPathFromSelect(path)` | 下拉选择入口 |
| `_aeUpdateArrayPathHint()` | 提示行 `#attrEditArrayPathHint`：当前节点下 N 个字段、已并入候选 M 个 |
| `ctx.nodeFieldCount` | 节点下元素字段数 |

**三处入口都触发联动**：改输入框（`oninput`/`onchange`）、用下拉选节点、
点属性树面板里的「设为数组路径」。

**状态保持**：`_aeRender()` 末尾恢复下拉选中值与提示行（重渲染会重建 DOM）；
`refreshAttrEditFields()`、`aeProbeNodes()` 也同步下拉。

### 三、实测

用 node 直接跑抽出的纯函数验证联动语义：

| 场景 | 候选 |
|---|---|
| 未指定节点 | `['flat_a', 'flat_b']`（顶层标量） |
| 指定 `data.records` | `['duration', 'nested', 'party_number', 'time']` —— **只**该节点下字段 |
| 换成 `other.list` | `['bar', 'foo']` —— **整体改变**，不含上一个节点的字段 |
| 无效路径 | 退回 `['flat_a', 'flat_b']` |

> 版本号 → `4.12.1`。需重启 HA 生效。

---

## 2026-09-30 — v4.12.0 属性提取编辑界面新增「从实体属性选择节点」

> 补齐编辑流程缺失的节点选择能力：与创建流程的
> `populateArrayPathSelect` / `populateJsonNodes` 对齐。

### 一、问题

| 流程 | 数组路径 | JSON 节点 |
|---|---|---|
| **创建**（步骤 1 加载实体后） | `<select id="attrArrayPath">`，由 `populateArrayPathSelect(tree)` 只列 `type==='list'` 的节点 | `populateJsonNodes()` 复选框列表（dict/list） |
| **编辑**（`_aeRender`） | 纯 `<input list="attrEditNodeList">` —— 只有 datalist 候选，**看不到属性树** | 手输行 + datalist |

`_aeLoadEntityFields()` 虽会读实体属性补候选，但候选是**扁平字符串列表**，
用户无从知道哪个是数组、哪个嵌套在哪、元素多少个 —— 对通讯模式这种
`array_path` 必填的场景尤其难填。

### 二、前端改动（`db_viewer.html`）

**面板 HTML**（插在采集参数的数组路径输入行之后）：

```
🌳 从实体属性选择节点
┌───────────────────────────────────────────────────────────┐
│ 探测实体 [下拉: 该类型下启用中的实体] [🔍 探测属性树] [✕ 关闭] │
│ 实体 sensor.x 状态 on；属性节点 12 个（更新于 ...）           │
├───────────────────────────────────────────────────────────┤
│ data.records      list  [1000 个元素]  设为数组路径 | 加为 JSON 节点 | ... │
│ data.records.fee  float                设为唯一键 | 复制路径   │
└───────────────────────────────────────────────────────────┘
```

**新增 10 个函数**：

| 函数 | 作用 |
|---|---|
| `aeToggleNodePicker()` | 展开/收起面板；首次展开自动探测 |
| `aeCloseNodePicker()` | 收起 |
| `_aeFillNodePickerEntities()` | 填充实体下拉（过滤 `_disabled`，排除待停用行） |
| `aeProbeNodes()` | 调 `ATTR_STATE_API` 取 `attribute_tree` 并渲染 |
| `_aeNodeDepth(path)` | 按 `.` 计算层级，用于缩进 |
| `aeRenderNodePicker()` | 渲染树；按路径排序、按深度缩进；已选中的标 `✔` |
| `aePickNodeAsArrayPath(path)` | 填入 `#attrEditArrayPath` |
| `aePickNodeAsKeyField(path)` | 填入 `#attrEditKeyField` |
| `aePickNodeAsJsonNode(path)` | 追加到 `#attrEditNodesBody`（复用 `_aeNodeRowHtml`，先去重） |
| `aeCopyNodePath(path)` | 复制路径（`navigator.clipboard`，不可用时降级为提示） |

**状态保持**：

- `_aeRender()` 末尾：若 `aeNodePickerOpen`，重开后重新填充实体下拉并重绘树 ——
  因为编辑弹窗整体是 `innerHTML` 重建的，不处理会导致「点一下别的按钮面板就没了」
- `closeAttrEditModal()`：重置 `aeNodePickerOpen` / `aeNodeTree`，避免下次打开残留上个类型的树

### 三、与创建流程的能力对照

| 能力 | 创建流程 | 编辑流程（本次补齐） |
|---|---|---|
| 看属性树 | `renderAttrTree`（`#attrTreeStep`） | `aeRenderNodePicker`（可点选） |
| 选数组路径 | `#attrArrayPath` select | `aePickNodeAsArrayPath` |
| 选 JSON 节点 | `populateJsonNodes` 复选 | `aePickNodeAsJsonNode` |
| 选唯一键 | `#attrKeyField` | `aePickNodeAsKeyField` |

编辑流程额外提供「复制路径」，因为编辑时常常要把路径粘贴到别的输入框。

### 四、后端

**未改动**。复用既有 `GET /api/ha_data_store/entity_state`，其 `attribute_tree`
节点结构为：

```python
{"path": key, "type": type(value).__name__}
# list  → {"type": "list",  "length": n, "first_element": {子字段: 类型名}}
# dict  → {"type": "dict",  "keys": [...], 且子字段展开为 path.sub 节点（两级）}
# 标量  → {"type": "str"/"int"/"NoneType"..., "value": v}
```

> 版本号 → `4.12.0`。需重启 HA 生效。

---

## 2026-09-30 — v4.11.0 属性提取编辑界面支持增删实体（多实体配置）

> 修复「编辑已有类型时无法新增实体」。前端此前**完全没有 INSERT 分支**。

### 一、问题定位

| 环节 | 原行为 |
|---|---|
| 渲染 | `_aeRender()` 只遍历 `ctx.entities`（从 `entity_configs` 读到的已存在实体），**没有「添加」按钮** |
| 保存 | `saveAttrEdit()` 中 `if (!orig \|\| !orig._rowid) return;` —— 没有 `_rowid` 的行**直接跳过** |
| 结果 | 改 `entity_id` 输入框只能"换绑"已有行（UPDATE 同一行），无法真正新增 |

后端其实**一直**能插入（`EntityConfigView` 的 upsert），只是编辑弹窗从不调用它。

### 二、前端改动（`db_viewer.html`）

**实体表格**（`_aeRender` 的实体区块）：

- **始终渲染**表格（原来 0 个实体时只显示一句提示，没有入口）
- 新增「操作」列与「➕ 添加实体」「📋 批量添加」按钮
- 新行标 `data-new="1"`；停用行加 `opacity:.45` 并以「待停用 / 待新增」标签区分
- 操作列按状态显示 `🗑 删除` 或 `↩ 恢复`

**新增 4 个函数**：

| 函数 | 作用 |
|---|---|
| `_aeSyncEntityInputs()` | 把表格里未保存的输入收回 `ctx.entities`（在 `_aeRender()` 开头调用，防止重渲染丢失输入） |
| `aeAddEntityRow(entityId?)` | 追加一行，沿用上一行的房间 / 采集方式 / 间隔，并聚焦到新行的实体输入框 |
| `aeBatchAddEntities()` | 弹框粘贴多个 entity_id，按 `[\s,，;；]+` 切分，自动去重并提示添加 / 跳过数量 |
| `aeRemoveEntityRow(idx)` | 待新增行 `splice` 移除；已有行切换 `_disabled` 标记（可再点恢复） |

**`saveAttrEdit()`**：

- 新增 `entAdds` / `entDisables` 两个收集列表
- 循环里原来的 `if (!orig || !orig._rowid) return;` 改为
  `if (orig._isNew || !orig._rowid) { entAdds.push(...); return; }`
- 新增前用 `ATTR_STATE_API` 逐个校验实体存在（复用「更换实体」的校验思路）
- 「没有需要保存的改动」判断补上 `&& !entAdds.length && !entDisables.length`
- 确认框增加「➕ 新增实体」「🗑 停用实体」两段说明

**执行阶段**：

```js
// 新增：EntityConfigView 的 upsert（不需要 field_mapping）
await fetch('/api/ha_data_store/config', { body: JSON.stringify({
  entity_id, attr_type: ctx.typeName, category: 'attribute', enabled: 1,
  collect_mode, collect_interval, room }) });
// 停用：软删（只影响该实体的本类型配置）
await _aeCellUpdate('entity_configs', d.rowId, 'enabled', 0);
```

新增 `ATTR_ENTITY_CONFIG_API = '/api/ha_data_store/config'` 常量 —— 注意与既有的
`ATTR_CONFIG_API`（`/api/ha_data_store/attr_config`，`AttrConfigView`，**要求传
`field_mapping`**）是**两个不同的端点**，这里必须用前者。

### 三、为什么不用 `/attr_config` 加实体

`AttrConfigView` 也能 upsert 实体，但它在类型已存在时会校验 `field_mapping`
必须与已存的一致（否则报错）。给已有类型追加实体时，用 `EntityConfigView`
只需 `entity_id + attr_type + category`，**新实体的 `field_mapping` 留空即继承类型级映射**
（见 v4.8.1 的 `_merge_entity_mapping`），语义更干净。

### 四、兼容性

- 已有编辑流程（改房间 / 采集方式 / 间隔 / 更换实体）行为不变
- 「删除」是软删，不影响该实体在其它类型下的配置，也不动历史数据

> 版本号 → `4.11.0`。需重启 HA 生效。

---

## 2026-09-30 — v4.10.0 通讯表字段「自动填写」（归属地 / 运营商 / 坐标）

> 新增离线回填：用 `data/phone2region.zdb` 与 `data/city_coordinates.json`
> 补全通讯表中的空字段。前端入口在 **API 工具 → 📞 通讯数据查询** 页面下方。

### 一、回填规则

| 字段 | 来源 | 说明 |
|---|---|---|
| `party_place` | `party_number` → 归属地库 | 省+市；省市同名只留一个（北京\|北京 → 北京） |
| `party_isp` | `party_number` → 归属地库 | 标准化为 `中国移动` / `中国联通` / `中国电信` / `中国广电`（含"（虚拟）"） |
| `party_coordinate` | `party_place` → 坐标表 | `"经度,纬度"`，如 `116.4053,39.905` |
| `location_coordinate` | `location` → 坐标表 | 地名匹配支持"省+市"、带行政后缀、左侧剥离等回退 |

**只填空值**（默认）：已有内容的字段不动，因此可反复、分批执行且幂等。
勾选「覆盖已有值」后才连已有内容一并重算。

坐标表匹配（`city_geo.matched_key`）刻意**不做**任意位置子串匹配，避免
"北京路" 被误判成 "北京"；只做「整名归一化 → 按行政层级切段（右侧优先）→ 左侧逐字剥离」。

### 二、新增模块

**`phone_region.py`**（移植自同仓库的 `shaobo_pocket_carrier` 集成，去掉通话/短信/流量
字段整理逻辑，保留本地查询）：

- `RegionIndex`：解析 `.zdb`（ZIP 容器，内层 `phone2region.db`）并校验 CRC32
- `query(number)` → `{province, city, area_code, isp, isp_raw, location}`
- 支持手机号（11 位 1 开头）与固话（区号表由记录区扫描得出，约 321 条）
- 带文件 mtime+size 缓存，库文件被替换后自动重载
- `get_index()` 不依赖 hass；`async_prepare(hass)` 解析路径（用户库
  `config/ha_data_store/` 优先，其次集成内置 `data/`）
- 保留 `async_download_db(hass)`（从官方镜像更新库文件，校验后原子替换）

库格式（实测确认，小端序）：

```
头部 20 字节 : CRC32(4) + 版本号(4) + 记录区指针(4) + 二级索引指针(4) + 一级索引指针(4)
记录区       : [长度 1B][UTF-8 记录: 省|市|邮编|区号|运营商]
二级索引区   : 2736 × int32（块 → 一级索引区绝对偏移，每块固定 256 条）
一级索引区   : N × [号段低 8 位 1B][记录区偏移 int32]
手机号查询   : key = 手机号前 7 位 - 1300000；key>>8 定位块，块内按 key&0xFF 顺序扫描
```

**`city_geo.py`**（同样移植）：`coordinate_text(地名)` → `"经度,纬度"`；
`normalize_place` 剥离行政后缀（省/市/自治区/自治州/地区/盟/特别行政区）与民族修饰词
（单字民族词必须带"族"才剥离，避免误伤"北京/天水/西藏"）。

**`comm_backfill.py`**（新增）：

- `table_status(db_path, type_name)`：总行数 + 各字段空格数 + 待回填行数
- `backfill(db_path, type_name, only_empty, limit, dry_run)`：执行回填并返回统计
- 只 `SELECT` 需要的列；`limit` 分批；`dry_run` 只统计不写库
- 统计含 `scanned / updated / filled{字段:条数} / no_region / no_coord / samples`

### 三、接口

`comm.py` 新增 `CommBackfillView`（随 `comm.register_api_views` 一并注册）：

```
GET  /api/ha_data_store/comm/backfill?type_name=
       → {success, region:{...库状态}, coords:{...坐标表状态}, table:{...待回填统计}}
       受 master + db_viewer 开关校验；首次调用时 async_prepare 归属地库
POST /api/ha_data_store/comm/backfill
       Body: {type_name?, dry_run?, only_empty?, limit?}
       → 回填统计；受 master + db_edit 开关校验
```

`type_name` 留空时沿用 `comm.resolve_comm_type_name` 自动探测（v4.6.1）。

### 四、前端（`db_viewer.html`）

「API 工具 → 📞 通讯数据查询」的参数区下方新增回填面板：

- **📇 自动填写** / **🧪 预览** 两个按钮 + **覆盖已有值** 复选框 + **最多处理 N 行** 输入
- 状态行展示：归属地库版本与来源、坐标表城市数、目标表名、总行数与各字段空格数
- 切换到 `comm:*` 查询类型时自动刷新状态（`onApiTypeChange` 中调用 `loadCommBackfillStatus`）
- 结果用既有 `showToast` 提示 + 状态行展示明细

### 五、数据文件

| 文件 | 位置 |
|---|---|
| `phone2region.zdb` | 集成 `data/`（内置兜底）或 `config/ha_data_store/`（用户覆盖） |
| `city_coordinates.json` | 同上 |

> 版本号 → `4.10.0`。需重启 HA 生效。

---

## 2026-09-30 — v4.9.1 修复多实体各写各的映射时去重键不匹配导致整批跳过

> **不是**「time 一致导致互相覆盖」，而是 `key_field` 为类型级配置、与实体级映射不匹配时
> 该实体的数组元素被整批静默跳过（一条都不写）。

### 一、问题定位（复现过程）

复刻 `_attr_collect_for_entity` 的 list/multi/comm 分支做实验，四个场景：

| 场景 | 结果 | 结论 |
|---|---|---|
| A 手机用 `a_time`、B 手机用 `b_when`，两实体 `time` 相同 | A 写 1 行，**B 写 0 行** | ❌ B 被整批跳过 |
| 两实体 `time` 不同 | A 写 1 行，**B 仍写 0 行** | ❌ 与 time 是否相同无关 |
| 同一实体同一轮内两条 `key` 相同 | 2 行都插入 | ✅ 合理（同一秒的两条通话本就是两条） |
| 跨轮次同 `entity_id` + 同 `key`，内容变化 | 行数不变，内容 UPDATE | ✅ 合理（按 key 去重） |

根因：`key_field` 取自 `attr_type_defs`（**类型级**），采集时直接
`_extract_nested_value(element, key_field)`；当实体级映射改写了源字段名，B 的数组里
没有 `a_time` 这个键 → `key_value is None` → `continue` → **整批跳过，且无日志**。

### 二、为什么不覆盖

去重与更新都以 `entity_id` 为条件：

```sql
SELECT * FROM {tbl} WHERE entity_id = ? ORDER BY id DESC LIMIT ?     -- 读取已有行
UPDATE {tbl} SET ... WHERE entity_id = ? AND {key列} = ?             -- 更新已有行
```

因此**不同实体即使 `time` 完全相同，也各写各行、互不干扰**。这一点修复前后都成立，
本次只是补上了"不匹配的实体什么都不写"这个真正的缺陷。

### 三、修复：`_resolve_collect_key(field_mapping, key_field)`

返回 `(源字段, 目标列, 是否回退)`，回退顺序：

1. `key_field` 本身在本实体映射的源字段中 → 直接用（**不回退**，行为与旧版一致）
2. 本实体映射中有源字段指向 `time` 列 → 用该源字段（comm 模式 `time` 必填，必命中）
3. 其它 → 退化为映射中的第一个目标列（保证是表中真实存在的列，
   避免 `sqlite3.Row` 取不存在的列抛 `IndexError`）

第 3 种情况也置回退标记。发生回退时记录 warning：

```
[attr] key_field='a_time' 不在实体 sensor.phone_b 的字段映射中，
       已自动改用 'b_when'（映射到 time 列）；如需精确控制请在「属性提取」中调整该类型的 key_field
```

`_attr_collect_for_entity` 中原本的 `key_target_col = field_mapping.get(key_field, key_field)`
（回退值可能是表中不存在的列名）已由该函数统一给出。

### 四、兼容性

- 单实体，或所有实体映射一致（`key_field` 在映射中）→ 走第 1 种，**完全不变**
- 多实体各写各的映射 → 自动按 `time` 列去重，不再静默丢数据
- 类型级 `key_field` 仍建议配置成**该类型语义上的时间字段**（如 `time`），
  这样即使各实体源字段名不同也能自动适配

> 版本号 → `4.9.1`。需重启 HA 生效。

---

## 2026-09-30 — v4.9.0 通讯表新增「流量 / 流量类型」字段

> 通讯固定字段 16 → 18：新增 `traffic_usage`（流量，MB）与 `traffic_type`（流量类型）。
> 已有通讯表重启后自动补列，无需手工迁移。

### 一、字段定义（`const.py`）

```python
COMM_FIELDS = (
    ...,
    ("duration",      "INTEGER", "时长(秒)",  False),
    ("cost",          "REAL",    "金额(元)",  False),
    ("traffic_usage", "REAL",    "流量(MB)",  False),   # 新增
    ("traffic_type",  "TEXT",    "流量类型",  False),   # 新增
    ("content",       "TEXT",    "消息内容",  False),
    ...
)
```

放在 `cost` 之后，保持「时长 / 金额 / 流量」这组计量字段相邻。

`COMM_COLUMNS` / `COMM_COLUMN_TYPES` / `COMM_COLUMN_LABELS` / `COMM_REQUIRED_COLUMNS`
均由 `COMM_FIELDS` 派生，因此**白名单校验、建表 DDL、补列、列类型强制**全部自动包含新列，
无需逐处改动。

### 二、自动升级

- 建表（`_ensure_attr_table` 的 comm 分支）遍历 `COMM_FIELDS` → 新表直接含 18 列
- 补列（`_ensure_comm_columns`）同样遍历 `COMM_FIELDS` → 已有表采集时补列
- **启动时**（`_ensure_all_comm_tables`）为所有识别为通讯表（`mode=comm` 或含
  `my_number` / `party_number` / `time` 三列特征）的表补列，因此重启 HA 即完成升级

历史行新列为默认值（`traffic_usage=0`、`traffic_type=''`），原数据不受影响。

### 三、流量单位换算（`__init__._parse_comm_traffic`）

采集时对 `traffic_usage` 的源值做规范化（与 `cost` → `_comm_to_number`、
`duration` → `_parse_comm_duration` 并列，在 `_normalize_comm_row` 中调用）：

| 输入 | 输出(MB) |
|---|---|
| `123` / `123.5` | 123 / 123.5（无单位按 MB） |
| `512MB` / `512mb` / `512 MB` | 512 |
| `1.5GB` / `2G` | 1536 / 2048 |
| `1024KB` / `1M` | 1 / 1 |
| `1TB` / `1T` | 1048576 |
| `1048576B` | 1 |
| `abc` / `1.5XB` / `-5` / `1.5GB流量` | 0 |

规则：正则 `^([0-9]*\.?[0-9]+)\s*([a-z]*)$`（大小写不敏感、允许空格、不接受负号），
单位表 `_TRAFFIC_UNITS` 覆盖 B/K/KB/M/MB/G/GB/T/TB，结果保留 4 位小数。

### 四、查询接入（`comm.py`）

- `_OUTPUT_COLUMNS`：`traffic_usage` / `traffic_type` 加入明细返回列（`cost` 与 `content` 之间）
- `_SORT_COLUMNS`：`sort=traffic_usage` 可按流量排序
- `_metric_expr`：`by=traffic_usage` 按 `SUM(traffic_usage)` 排序（排行榜）
- `_shape_agg_row` / summary：`traffic_usage` 保留 4 位小数，summary 返回合计
- 表无该列时 `_sum_expr` 返回 `0`，不会报错（向后兼容）

### 五、前端（`db_viewer.html`）

- `COMM_FIELD_DEFS` 同步新增两行（该表与服务端 `COMM_FIELDS` 顺序一致，测试中断言完全相等）
- `COMM_SOURCE_ALIASES` 新增别名：`traffic_usage` → `traffic` / `data_usage` / `flow` /
  `usage` / `total_bytes` / `data_amount` / `net_usage`；`traffic_type` → `traffic_kind` /
  `net_type` / `network_type` / `data_network` / `net_mode` / `netmode`
- 「属性提取」通讯采集的说明文案由「16 个」改为「18 个」，并提示流量支持带单位写法
- **注意**：`channel`（数据来源）的别名已含 `data_type`，故 `traffic_type` **不含**该别名，
  避免同一源字段被两列争抢。若源数据的 `data_type` 表示流量类型，请手工映射。

### 六、兼容性

- 旧配置与旧数据不受影响；`field_mapping` 中未映射新列的实体，新列写默认值
- 实体级字段映射（v4.8.1）同样适用：各实体可把不同源字段映射到 `traffic_usage`

> 版本号 → `4.9.0`。需重启 HA 生效。

---

## 2026-09-30 — v4.8.1 通话记录支持多实体采集到同一张表（每实体独立字段映射）

> 字段映射从「类型级」扩展为「类型级默认 + 实体级覆盖」，多个实体可各用各的源字段结构，
> 写入同一张 `attr_<类型名>` 表。

### 一、问题

`field_mapping` 只存在于 `attr_type_defs`（类型级），同一类型下的所有实体共用一份映射。
两台手机的通话记录 JSON 字段名不同（`a_time` / `b_when`）时无法同时采集到一个类型。

### 二、设计

| 层级 | 存储 | 作用 |
|---|---|---|
| 类型级 | `attr_type_defs.field_mapping` | 该类型的**默认**映射（向后兼容） |
| 实体级 | `entity_configs.field_mapping`（**新增**） | 非空则覆盖类型级；留空 = 继承 |

解析规则：**实体级非空 → 用实体级，否则回退类型级**。因此旧配置行为不变。

**列结构**：各实体采集时都会调用 `_ensure_attr_table`（幂等补列），所以表列是各实体映射的
**并集**；某实体未映射的列，在它写入的行里保留列默认值。不同源字段名映射到同一目标列是允许的。

### 三、实现

**表结构**（`__init__.py`）：

- `entity_configs` 建表新增 `field_mapping` / `field_types`（`TEXT NOT NULL DEFAULT ''`）
- `_migrate_database` 中逐列补齐（含「重建表」分支的 `new_columns_def` 与 `copy_cols` 白名单）

**采集链路**：

- 新增 `_merge_entity_mapping(row)`：把查询行里的 `ec_field_mapping` / `ec_field_types`
  合并进 `row`（非空则覆盖类型级字段）
- `_get_all_attr_entities`（poll 轮询）查询增加
  `ec.field_mapping AS ec_field_mapping, ec.field_types AS ec_field_types`，
  结果过 `_merge_entity_mapping`
- `_async_attr_event`（event 模式）的 `cfg` 组装改为
  `info.get("field_mapping") or type_def.get("field_mapping")`
- `_ensure_attr_table` 新增 `field_types_override` 参数（实体级列类型优先于类型级定义），
  `_attr_collect_for_entity` 解析 `cfg["field_types"]` 后传入

**接口**（`http_api.py` 新增 `AttrEntityMappingView`，在 `__init__.py` 注册）：

- `GET  /api/ha_data_store/attr_entity_mapping?entity_id=&attr_type=`
  → 返回实体级映射 + 类型级映射 + `scope`（`entity` / `type`）
- `POST /api/ha_data_store/attr_entity_mapping`
  Body `{entity_id, attr_type, field_mapping}`；**空映射 = 清除实体级**（改回继承）
  通讯模式下校验目标列在 `COMM_COLUMNS` 白名单内、必填列（`COMM_REQUIRED_COLUMNS`）已映射，
  列类型强制为固定类型；实体配置不存在时报错提示先添加该实体
- 新增 `_json_or(raw, default)` 小工具（配置损坏时按「未设置」处理）

**前端**（`db_viewer.html`）：

- 「实体采集参数」表格新增「字段映射」列：显示 `继承类型级` / `自定义 N 项` + 「编辑」链接
- 新增内联编辑区（`#aeEntMappingBox`）：目标列由类型决定——通讯模式用 `COMM_FIELD_DEFS`
  的 16 个固定字段，其它模式取类型级映射的目标列；每行填源字段或 `=固定值`
- 新增 6 个函数：`aeEntTargets` / `aeOpenEntityMapping` / `aeCloseEntityMapping` /
  `_aeCollectEntityMapping` / `aeSaveEntityMapping` / `aeClearEntityMapping`
- 保存与清除都**即时写入**后端（不依赖弹窗的「保存」按钮），并同步本地状态后重渲染

### 四、兼容性

- 旧配置（`entity_configs.field_mapping` 为空）行为**完全不变**，仍走类型级映射
- 类型级的 `AttrConfigView` 未做任何改动

> 版本号 → `4.8.1`。需重启 HA 生效。

---

## 2026-09-30 — v4.8.0 属性提取编辑时支持更换「被采集的实体」

> 「实体采集参数」表格中的实体 ID 由只读文本改为可编辑输入框。

### 一、问题

编辑已有采集配置时，表格里只有房间 / 采集方式 / 间隔可改，**实体 ID 是纯文本**：

```javascript
h += '<td ...>' + escHtml(e.entity_id || '') + '</td>';   // 改不了
```

要更换被采集的实体，只能删掉配置重新创建。

### 二、改动（`db_viewer.html`）

| 位置 | 改动 |
|---|---|
| `_aeRender()` 实体行 | 实体列改为 `<input data-role="entity_id">`，并在提示中说明可改、数据不迁移 |
| `saveAttrEdit()` 采集差异 | 新增 `entRenames` 收集实体变更；校验**非空**与**同类型内不重复** |
| 同上（新增 6.1 步） | 保存前请求 `entity_state` API 预校验目标实体存在，不存在则拒绝保存 |
| 确认框 | 列出「旧实体 → 新实体」，提醒核对数组路径 / 唯一键 / 字段映射 |
| 执行阶段 | 对 `entity_id` 的更新单独捕获异常，主键冲突时提示「请先删除它的旧配置」 |

后端**无需改动**：`DBViewerUpdateView` 本就是通用单元格更新（校验表名与列名存在后执行
`UPDATE ... WHERE rowid = ?`），`entity_id` 作为普通列即可更新。

### 三、实现要点

`entity_configs` 的主键是 `(entity_id, attr_type)`。改 `entity_id` 等于换主键：

- SQLite 直接 `UPDATE` 主键列是允许的，冲突时抛 `IntegrityError`
- 行数据不重建（不删旧行、不增新行），`rowid` 保持不变，因此其它引用不受影响
- `attr_*` 数据表按 `type_name` 关联，与实体无关——**历史数据不会跟着搬走**

### 四、注意事项

- **旧数据保留在原表中，不迁移**；新实体从下一轮采集开始写入
- 新实体属性结构差异较大时，需一并核对「数组路径 / 唯一键字段 / 字段映射」，
  否则采集可能取不到数据（这种配置允许保存，不会报错）

> 版本号 → `4.8.0`。需重启 HA 生效。

---

## 2026-09-29 — v4.7.9 修复：通讯表字段升级在「配置记录丢失」时失效

> 现象：v4.7.8 新增的 3 个字段没有出现在已有通讯表里。

### 一、根因

字段升级靠 `_ensure_comm_columns()` 补列，而它**只在 `_ensure_attr_table()` 里被调用**——
该函数的入口条件是「保存采集配置」，且需要 `attr_type_defs` 中存在对应记录：

```python
type_row = conn.execute(f"SELECT mode, field_types FROM {TABLE_ATTR_TYPE_DEFS} "
                        f"WHERE type_name = ?", (type_name,)).fetchone()
is_comm = mode == ATTR_MODE_COMM
...
if is_comm:
    existing_cols |= _ensure_comm_columns(conn, tbl, existing_cols)
```

用户库里 `attr_type_defs` **是空的**（采集配置记录已丢失），但 `attr_chat_133`
表与 10 万行数据都还在。于是：

- 没有配置记录 → 永远不会走到 `_ensure_attr_table()` → 永远不会补列
- 表结构就永久停在旧版本

**与「用户自定义表名」无关**；自定义类型名本身是支持的（`attr_chat_133` 正是这类表）。

### 二、修复

新增 `_ensure_all_comm_tables(conn)`，在 `_init_database()` 里随 `_migrate_database()`
之后执行。它用**两条互补的途径**识别通讯表：

1. `attr_type_defs` 中 `mode=comm` 的 type_name —— 正常配置的表
2. **结构特征**：`attr_*` 表中含 `my_number` / `party_number` / `time` 三列的 ——
   即使配置记录丢失，只要表还在就能识别并升级

对识别出的每张表执行 `_ensure_comm_columns()` + `_ensure_comm_indexes()`（都幂等），
有新增列时写日志。非通讯表（如 `attr_ele_day`）不会被误改。

### 三、对用户库的实际处理

直接对该库执行了补列（`ALTER TABLE ADD COLUMN` 是 SQLite 的 O(1) 元数据操作，不重写数据）：

```
[补齐] attr_chat_133 += location_coordinate, party_isp, party_coordinate（现有 103990 行，数据不动）
attr_chat_133: 共 23 列，缺 无
```

### 四、今后

新装/重启时该逻辑自动运行，无需再手动补列。识别条件宽松（只要求三列同时存在），
不会把电费、燃气等其它 `attr_*` 表误判为通讯表。

> 版本号 → `4.7.9`。需重启 HA 生效。

---

## 2026-09-29 — v4.7.8 通讯表新增 3 个字段

> 通讯数据采集新增第 14~16 个固定字段：我的坐标 / 对方运营商 / 对方坐标。

### 一、字段定义

| 列 | 类型 | 标签 | 必填 |
|---|---|---|---|
| `location_coordinate` | TEXT | 我的坐标 | 否 |
| `party_isp` | TEXT | 对方运营商 | 否 |
| `party_coordinate` | TEXT | 对方坐标 | 否 |

### 二、改动

| 文件 | 改动 |
|---|---|
| `const.py` | `COMM_FIELDS` 追加 3 行 → 自动进入 `COMM_COLUMNS` / `COMM_COLUMN_TYPES` 白名单，`AttrConfigView` 的目标列校验自动放行 |
| `comm.py` | `_OUTPUT_COLUMNS` 加入 3 列，`type=records` 明细会返回；模块文档「16 列」同步 |
| `db_viewer.html` | `COMM_FIELD_DEFS` 加 3 行；`COMM_SOURCE_ALIASES` 加别名自动预选；两处「13 列/字段」文案改为 16 |
| `http_api.py` / `__init__.py` | 注释中的字段数同步为 16 |
| `README.md` | 字段表、功能描述、"13 个字段"→"16 个" |

### 三、表结构自动升级

**不需要重建表、不会丢数据。** `_ensure_comm_columns()` 是幂等的——重启后**保存一次采集配置**
（或等下一次采集触发）就会自动执行：

```sql
ALTER TABLE attr_comm_records ADD COLUMN "location_coordinate" TEXT NOT NULL DEFAULT ''
ALTER TABLE attr_comm_records ADD COLUMN "party_isp" TEXT NOT NULL DEFAULT ''
ALTER TABLE attr_comm_records ADD COLUMN "party_coordinate" TEXT NOT NULL DEFAULT ''
```

已有历史记录这三列为空，新采集的记录正常写入。

### 四、为什么追加在末尾

新字段**追加在 `COMM_FIELDS` 末尾**，而不是插到 `party_place` 附近（语义上更接近）。
原因：`ALTER TABLE ADD COLUMN` 只能追加到表末，若定义顺序与表实际顺序不一致，
新建表与升级表的列顺序就会不同——虽然不影响按列名读写，但会让 `PRAGMA table_info`
的输出、手工 SQL 排查产生无谓差异。保持"定义顺序 = 表列顺序"更省心。

### 五、别名预选

前端映射表会自动预选常见源字段名：

- **我的坐标**：`my_coordinate` / `my_coord` / `my_location` / `my_position` / `coordinate` /
  `coord` / `lonlat` / `lnglat` / `gps` / `geo` / `position` / `address_coordinate`
- **对方运营商**：`party_isp` / `isp` / `carrier` / `operator` / `party_carrier` /
  `party_operator` / `sim_isp` / `network` / `telecom` / `province_isp`
- **对方坐标**：`party_coordinate` / `party_coord` / `party_position` / `party_location` /
  `party_gps` / `party_geo` / `other_coordinate` / `peer_coordinate` / `target_coordinate`

> 版本号 → `4.7.8`。需重启 HA 生效。

---

## 2026-09-29 — v4.7.7 分页不再设上限 + limit=0 表示不限

> 移除所有查询类型的 `limit` 上限——只保留默认值，传多少返回多少。

### 一、行为

| 传值 | 行为 |
|---|---|
| 不传 | 默认条数（明细类 100） |
| `limit=0` 或负数 | **不限条数，一次返回全部** |
| 其它值 | 按该值返回，不再被钳制 |

响应里的 `limit_max` 恒为 `null`，表示无上限。

### 二、实现

- `onthisday.py` 新增两个辅助：
  - `_paging_limit(params, default)`：解析 limit，`≤0` 返回 `-1`（交给 SQL 表示不限）
  - `_page_slice(items, offset, limit)`：`limit < 0` 时只应用 offset
- 平铺明细的 SQL 传 `-1 if limit < 0 else limit + offset`（SQLite `LIMIT -1` 即不限）
- `comm.py` 的 `_query_records()` 内联同样处理
- 移除全部上限参数：明细（1000）、`dates`（2000）、`stats` / `trend`（5000）、
  `ranking`（500 / 200）、`crosstab`（200）、`parties` / `places`（1000）
  —— 各处保留原默认值，仅去掉 hi 参数

### 三、顺带修复 `comm._get_int()`

`comm.py` 有自己的一份 `_get_int()`（与 `onthisday.py` 的重复），仍是旧写法：

```python
val = int(str(params.get(key, "") or "").strip() or default)
```

`params.get(key)` 为整数 `0` 时，`0 or ""` 得到 `""`，被判定为「未提供」而套用默认值——
因此 `limit=0` 在 `records` 模式下会静默变成 100（本轮测试才发现）。
已与 `onthisday.py` 统一为显式判断 `None` 与空串。

> 版本号 → `4.7.7`。需重启 HA 生效。

---

## 2026-09-29 — v4.7.6 分页参数回显 limit_max

> `limit` 不是强制值而是默认值（缺省 100），此前无从得知上限，只能试错。

### 一、问题

请求 `/api/ha_data_store/comm?type=onthisday&mode=detail`（未带 `limit`）返回
`count: 100`，容易被理解为"被强制限制在 100 条"。实际上是**默认值**为 100，
可以调大——但响应里只有 `limit: 100`，看不出还能调到多少。

### 二、改动

| 项 | 说明 |
|---|---|
| 新增响应字段 `limit_max` | 该查询类型的 `limit` 上限（明细类为 1000） |
| `comm._query_records()` | 补上 `truncated` / `remaining`——v4.7.5 只给 `onthisday` 的 detail 加了这两个字段，`records` 遗漏 |
| 常量提取 | `onthisday._DETAIL_LIMIT_DEFAULT/_MAX`、`comm._RECORDS_LIMIT_DEFAULT/_MAX` |

### 三、各查询类型的 limit 默认值与上限

并不统一，以响应回显为准：

| 查询类型 | 默认 | 上限 |
|---|---|---|
| `records` / `onthisday&mode=detail`（含 env 聚合） | 100 | 1000 |
| `dates`（哪些日期有数据） | 400 | 2000 |
| `stats` / `trend`（时间桶） | 1000 | 5000 |
| `ranking`（排行榜） | 20 | 500 |
| `crosstab`（交叉汇总行数） | 30 | 200 |
| `parties` / `places`（联系人 / 地点清单） | 100 | 1000 |

### 四、用法

```bash
# 默认 100 条，响应会告知还能取多少
/api/ha_data_store/comm?type=onthisday&mode=detail&key=xxx
#   → count: 100, total: 502, truncated: true, remaining: 402, limit_max: 1000

# 一次拿全
/api/ha_data_store/comm?type=onthisday&mode=detail&limit=1000&key=xxx
#   → count: 502, truncated: false, remaining: 0

# 超出上限时会自动钳到上限；数据更多时用 offset 翻页
```

> 版本号 → `4.7.6`。需重启 HA 生效。

---

## 2026-09-29 — v4.7.5 明细分页新增 truncated / remaining 标记

> 背景：排查「环境明细少了 power 数据」——实际一条没丢，是 `limit` 分页截断被误读。

### 一、排查结论

直接读库确认（`storage/ha_data_store.db`）：

| 表 | 总行数 | 09-29 采样数 |
|---|---|---|
| `env_temperature` | 190,016 | 312 |
| `env_humidity` | — | 312 |
| `env_power` | — | 39 |
| `env_pm25` / `env_co2` / `env_sensor` | — | 0（确实没采过） |

响应里 `total: 663 = 312 + 312 + 39` 完全自洽。用户看到的"少 power"来自两点叠加：

1. `limit=100`，而聚合后的总行数 `group_count` 是 **351** —— 只返回了最近 100 组
2. **低频指标被高频指标挤出窗口**：温湿每批 12 条（12 个房间），`power` 每批只有 1 条
   （「全屋」总表）。时间倒序排列下，100 行很快被温湿占满，power 落到窗口之外

### 二、改动

明细响应新增两个字段（聚合与平铺模式都提供）：

| 字段 | 含义 |
|---|---|
| `truncated` | 是否还有未返回的行 |
| `remaining` | 未返回的行数 |

- 聚合模式：`truncated = offset + count < group_count`，`remaining = group_count - offset - count`
- 平铺模式：以 `total`（全部匹配行数）为基准做同样计算
- 既有字段不变：`count`（本页行数）/ `total`（采样总数）/ `group_count`（聚合后总行数）

### 三、使用建议

想看单个指标时**用 `env_metric` 筛选**，而不是靠翻页：

```
/api/ha_data_store/onthisday?source=env&mode=detail&env_metric=power&limit=1000&key=xxx
```

`limit` 上限为 1000；本例 `group_count` 为 351，调大后一次可取全。

> 版本号 → `4.7.5`。需重启 HA 生效。

---

## 2026-09-29 — v4.7.4 环境明细 API 默认返回「带指标类型」的格式

> v4.7.0 加的按房间聚合只对传感器生效，API 默认仍是平铺格式，导致明细里所有指标都叫
> `value`、无法分辨指标。

### 一、问题

请求 `/api/ha_data_store/onthisday?source=env&mode=detail` 得到：

```json
{"datetime": "...12:30:00", "name": "", "room": "次卧", "value": 24.8}
{"datetime": "...12:30:00", "name": "", "room": "次卧", "value": 72.0}
```

两条记录只有 `value` 不同，**看不出前者是温度、后者是湿度**。

### 二、修复

1. **`env_by_room` 默认值由 `0` 改为 `1`**：env 源明细默认按「房间 × 时间点」聚合，
   指标名直接成为字段名（`temperature` / `humidity` / `power` …）
2. **平铺模式补 `metric` 字段**：传 `env_by_room=0` 时每行附带指标名，
   即使指定了 `fields` 白名单也会附带——缺了它就无法分辨指标
3. 聚合模式下 `fields` / `drop_fields` 不适用（列由数据动态决定），会被静默忽略而非报错；
   `_q_detail` 的 docstring 与模块文档已注明

修复后的同一请求：

```json
{"room": "全屋", "datetime": "2026-09-29 12:40:00", "power": 4.383}
{"room": "次卧", "datetime": "2026-09-29 12:30:00", "temperature": 24.8, "humidity": 72.0}
```

### 三、实现

- `_q_detail()`：`env_by_room` 默认参数由 `0` 改为 `1`；文档串更新
- `_q_detail()` 平铺分支：逐表合并时若 `src.key == "env"`，给该表的每行补
  `metric = _env_metric_of(tbl)`
- 仅影响 env 源；comm / device 明细不加 `metric`、不受任何影响

> 版本号 → `4.7.4`。需重启 HA 生效。

---

## 2026-09-29 — v4.7.3 环境汇总列出「存在但无数据」的指标

> `summary.by_metric` 的键之前少于 `tables`，看起来像漏了指标；现在两者一一对应。

### 一、问题

`_env_metric_agg` 按表逐个聚合，但只在 `COUNT(*) > 0` 时写入结果：

```python
if row is not None and int(row["count"] or 0) > 0:
    out[metric] = _env_metric_row(row)     # 空指标被静默跳过
```

于是 `tables` 里 6 张表，`summary.by_metric` 里只有 3 个指标——用户看到的正是这种不一致，
容易误判为「漏统计了 pm25 / co2 / sensor」。

### 二、修复

空指标也写入占位：

```python
else:
    out[metric] = _env_metric_empty()   # {count: 0, avg_value: None, ...}
```

- 新增 `_env_metric_empty()`，聚合值用 `None` 而非 `0`——「没有采样」与「采样值恰好是 0」
  是两回事（温度 0℃ 是有效读数）
- `summary.by_metric` 的键 = 当前查询涉及的指标表（即 `tables`）——包括自定义 `env_metric`
  过滤后的子集
- 按年的 `years[].by_metric` **不补空**（只列该年实际有数据的指标），年份维度上补空没有意义
- 顶层 `count` 求和时占位的 0 不影响结果

### 三、排查提示

若某指标长期为 `count: 0`，说明该表在当前筛选（`SUBSTR(datetime,6,5)` 匹配月日）下确实没有数据。
注意本功能筛的是「**历年同月同日**」，不只是今天：

```sql
SELECT COUNT(*), MIN(datetime), MAX(datetime) FROM env_pm25;
```

> 版本号 → `4.7.3`。需重启 HA 生效。

---

## 2026-09-29 — v4.7.2 修复：API 未读取「通讯数据表类型名」设置

> 设置了 `text.ha_data_store_comm_type_name` 后，通讯查询 API 仍去找默认表 `attr_comm_records`。

### 一、根因

v4.6.1 引入设置实体后，**只有传感器路径读了它**：

| 路径 | 修复前 |
|---|---|
| `sensor.TodayInHistorySensor` | 读实体 → 传给查询 ✅ |
| `CommApiView._handle` | 只从请求参数取 `type_name`（前端 v4.6.2 起已不再发送该参数 → 恒为空）❌ |
| `OnThisDayView._handle` | 完全没处理 `type_name` ❌ |

两条 API 路径只能落到 `attr_type_defs` 自动探测；若库里没有 `mode=comm` 的登记记录，
就回退到 `COMM_DEFAULT_TYPE_NAME`，于是报「未找到通讯数据表 attr_comm_records」。

### 二、修复

- `comm.py` 新增 `read_comm_type_name_setting(hass)`：统一读取设置实体
  （须在事件循环线程调用；空值 / `unknown` / `unavailable` / `none` / `-` / `auto` → 空串）
- `CommApiView._handle`：参数为空时读取实体，再交给 `run_comm_query` 自动探测
- `OnThisDayView._handle`：同样补上（此前缺失）
- `sensor.py`：`_read_comm_type_name()` 改为调用公共函数，删掉重复的状态解析逻辑
- `run_comm_query`：结果中新增 `type_name`（**实际生效值**，含自动探测结果）；
  `CommApiView` 不再回显请求参数的原值（之前会把空串回显出去，误导排查）

三条路径现在共用同一套解析逻辑，不会再次出现口径漂移。

### 三、优先级（不变）

显式 `type_name` 参数 > 设置实体 > `attr_type_defs` 自动探测 > 默认 `comm_records`

> 版本号 → `4.7.2`。需重启 HA 生效。

---

## 2026-09-29 — v4.7.1 环境汇总也按指标分组

> 修复：环境 `summary` / `years` 跨指标混算，均值与极值无意义。

### 一、问题

v4.7.0 只改了明细，汇总仍走通用的 `_grouped_scan` / `_total_of`，把 6 张指标表的值
当成同一个量纲处理：

```yaml
summary:
  count: 535
  avg_value: 46.9853     # 温度 25 + 湿度 58 + CO₂ 800 … 的混合平均，无意义
  max_value: 91          # 那是 CO₂ 的值
  min_value: 0
```

### 二、修复后的结构

```yaml
summary:
  count: 535             # 采样总数（跨指标求和仍有意义：采了多少条）
  by_metric:
    temperature: {count: 90, avg_value: 24.5, max_value: 26.1, min_value: 19.8}
    humidity:    {count: 90, avg_value: 57.2, max_value: 68.0, min_value: 45.0}
    pm25:        {count: 90, avg_value: 35.1, max_value: 52.0, min_value: 18.0}
years:
  - {year: '2026', count: 400, by_metric: {temperature: {...}, humidity: {...}, ...}}
```

### 三、实现

- 新增 `_env_metric_agg(conn, src, where_sql, args, year_expr=None)`：
  逐表（即逐指标）各自 `COUNT / AVG / MAX / MIN`。不给 `year_expr` 返回
  `{metric: {...}}`，给了则返回 `{year: {metric: {...}}}`
- 新增 `_env_metric_row(row)`：单指标聚合行 → 统一输出结构
- 新增 `_append_env_year_summary()`：组装 env 的 `summary` 与 `years`
- `_append_year_summary()` 在 `src.key == "env"` 时转交上述实现；comm / device 沿用原逻辑
  （它们各表量纲一致，混算没有问题）

顶层 `count` 是跨指标求和（「采了多少条」这个语义仍然成立），
而 `avg_value` / `max_value` / `min_value` 只出现在各指标内部。

### 四、影响范围

**仅 env 源**。comm / device 的 `summary`（`{count, duration, cost}` / `{count, duration,
energy, avg_duration}`）与 `years` 结构完全不变；环境汇总的改动与明细是否聚合
（`env_by_room`）无关，两者独立。

> 版本号 → `4.7.1`。需重启 HA 生效。

---

## 2026-09-29 — v4.7.0 环境明细改为「一个房间多种数据」

> 环境明细不再逐条平铺（多指标混在同一个 `value` 列），改为按「房间 × 时间点」聚合，
> 每个指标独立成列。

### 一、问题

环境数据来自 6 张表（`env_temperature` / `env_humidity` / `env_pm25` / `env_co2` /
`env_power` / `env_sensor`），`_q_detail` 把它们合并成一个扁平列表后，所有指标的值都落在
同一个 `value` 字段上：

```yaml
- {datetime: '...10:00:00', room: 客厅, value: 25.0}   # 温度
- {datetime: '...10:00:00', room: 客厅, value: 58.0}   # 湿度 ← 二者无法区分
```

### 二、新的聚合格式（`env_by_room=1`）

```yaml
env:
  count: 535          # 采样总数（口径不变）
  group_count: 54     # 聚合后行数
  room_count: 10
  rooms: [...]
  metric_names: [temperature, humidity, pm25, co2]
  detail:
    - {room: 客厅, datetime: '2026-09-29 10:00:00', temperature: 25.0, humidity: 58.0, pm25: 35.0}
```

实现（`onthisday._q_detail_env_by_room`）：

- 逐表按 `(room, 时间桶)` 分组取 `AVG(value)` 与 `COUNT(*)`，指标名由表名反推
  （`_env_metric_of`：`env_temperature` → `temperature`）
- 再按 `(时间桶, room)` 在 Python 侧合并成一行，各指标写入同名键
- **时间桶**：`room_bucket`（分钟，默认 1）用 `SUBSTR(datetime, 1, 16)` 截到分钟，
  因此各指标表时间戳差几秒也能对齐到同一行；`0` = 精确到秒
- 同一房间的同类传感器取**平均**；`room` 为空的记录单独成行
- `datetime` 取组内**最早**一条（保留完整时间戳）
- 排序：时间倒序 → 同一时刻按房间名正序（两次稳定排序实现）
- 复用新抽出的 `_append_year_summary()`，因此 `summary` / `years` 仍基于全部列计算，
  与平铺模式**完全一致**（测试中逐项比对）

### 三、默认与开关

- API 默认**仍是平铺**（`env_by_room` 缺省为 0），向后兼容；传 `env_by_room=1` 得到聚合结果
- 传感器默认使用聚合（类常量 `DETAIL_ENV_BY_ROOM = True`），替代了上一版的环境字段白名单
  （`DETAIL_KEEP_FIELDS` 已移除——聚合格式的列是动态的，白名单不再适用）

### 四、顺带修复 `_get_int()`

```python
# 修复前
val = int(str(params.get(key, "") or "").strip() or default)
```

`params.get(key)` 返回整数 `0` 时，`0 or ""` 会得到 `""`，于是被判定为「未提供」而套用默认值。
URL 查询参数是字符串（`"0"`）不受影响，但 Python 侧（含传感器、自动化脚本）传 `0` 时会被
静默改写——例如 `room_bucket=0` 会变成 `1`。已改为显式判断 `None` 与空串。

> 版本号 → `4.7.0`。需重启 HA 生效。

---

## 2026-09-29 — v4.6.3 历史今日传感器精简明细字段

> 传感器三个节点的 `detail` 按数据源裁剪字段，减小状态属性体积。

### 一、裁剪规则

| 数据源 | 排序 / 索引字段 | 明细列 |
|---|---|---|
| `comm` | `time` | 剔除 `id` / `datetime` / `extra_json` / `name` / `room` / `updated_at`，保留全部通讯业务字段（含 `entity_id`） |
| `device` | `on_time` | 不裁剪（保留全部列） |
| `env` | `datetime` | **只保留** `datetime` / `room` / `value` |

### 二、实现

- `onthisday.py`：`_detail_select()` 新增 `drop` 参数，优先级为
  `fields`（白名单）> `drop_fields`（黑名单）> 全部列；新增 `_parse_drop_fields()`
- `onthisday.py`：`_q_detail()` 解析并传入 `drop_fields`
- `sensor.py`：新增类常量 `DETAIL_DROP_FIELDS`（通讯）与 `DETAIL_KEEP_FIELDS`（环境），
  在数据源循环里按源注入对应参数
- 排序字段本就是各源的 `time_col`（`comm.time` / `device.on_time` / `env.datetime`），
  本次未改动，只是确认口径一致

### 三、为什么通讯用黑名单、环境用白名单

- 通讯表带属性提取的通用元数据列（`id` / `datetime` / `name` / `room` / `extra_json` /
  `updated_at`），**将来可能新增列**——用黑名单可自动保留新列，只需在需要时补充排除项
- 环境表明确定义为「时间 + 房间 + 数值」，用白名单更直观地锁定输出形状

两者只影响 `detail`；`summary` / `years` 等汇总统计仍基于全部列计算，不受裁剪影响。

### 四、关于「环境数据只有今年」的说明

若环境明细只出现今年，**通常是环境表里确实只有今年的数据**（历年的 `MM-DD` 不存在），
而非筛选失效。验证方式：

```sql
-- 看库里有哪些「月日」，以及各有多少条
SELECT SUBSTR(datetime, 6, 5) AS d, COUNT(*) FROM env_temperature GROUP BY d ORDER BY d;
```

若该查询只返回当前的 `09-29` 一行，说明采集是近期才启用的，属正常现象。
另需注意 `datetime` 的格式必须形如 `YYYY-MM-DD HH:MM:SS`——筛选用的是
`SUBSTR(datetime, 6, 5)`，若历史数据的日期格式不同（如 `2025/09/29`）则无法命中。

> 版本号 → `4.6.3`。需重启 HA 生效。

---

## 2026-09-29 — v4.6.2 通讯 API 工具不再暴露「类型名」参数

> 类型名已由设置实体与自动探测决定，DB 浏览器「API 工具」中不再出现 `type_name` 输入项，
> 生成的 URL 也不带该参数。

### 一、前端 `db_viewer.html`

移除 7 处 `P('type_name', '类型名 type_name', '默认 comm_records')` 参数项：

| 位置 | 覆盖的查询类型 |
|---|---|
| `common` 数组首项 | 明细 / 日期 / 汇总 / 联系人清单 / 地点清单 / 时段分布 / 趋势 / 排行榜 |
| `stats` 分支 | 统计分析 |
| `onthisday` 的 `otdCommon` | 历史今日（5 个 mode 全部） |
| `crosstab` 分支 | 交叉汇总 |
| `compare` 分支 | 周期对比 |
| `contact` 分支 | 联系人档案 |
| `sourceFields.comm` | 历史今日设备 / 环境的 `comm` 数据源 |

由于 `comm:` 类选项的 URL 是**按参数区控件动态拼装**的（`#apiCommParamsBox [data-name]`），
移除参数项后 URL 自然不再带 `type_name`，无需改动拼装逻辑。

同时更新两处文案：

- 通讯查询提示区：说明类型名**自动探测**，如需指定请设置
  `text.ha_data_store_comm_type_name` 实体
- 「属性提取」通讯模式的默认类型名注释：改为「建议值；改用它名也不影响查询——查询侧会自动探测」

### 二、后端不变

`type_name` 参数**仍然保留**（`comm.run_comm_query` / `CommApiView` / `run_onthisday_query`
照旧接受），向后兼容——已有的显式传参调用、外部自动化、脚本分享的 URL 都不受影响。
本次只是前端不再引导用户填写。

`comm.py` 的模块文档同步更新：`type_name` 一项注明「一般无需填写」并列出自动解析顺序。

> 版本号 → `4.6.2`。需重启 HA 生效。

---

## 2026-09-29 — v4.6.1 通讯表类型名自动探测 + 新增设置实体

> 修复：「属性提取」里用了自定义通讯类型名时，历史今日 / 通讯查询会报
> 「未找到通讯数据表 attr_comm_records」。现在默认自动探测，无需手动配置。

### 一、问题根因

`onthisday._build_source` 的 comm 分支与 `CommApiView._handle` 都在 `type_name` 缺省时
**写死** `COMM_DEFAULT_TYPE_NAME`（`comm_records`）。用户若在「系统配置 → 属性提取」中
把通讯采集的类型名配成别的（如 `my_phone`，表为 `attr_my_phone`），查询就会找不到表。

### 二、解析优先级（新增 `comm.resolve_comm_type_name`）

| 优先级 | 来源 |
|---|---|
| 1 | 显式 `requested`（API 的 `type_name` 参数） |
| 2 | `text.ha_data_store_comm_type_name` 实体的设置值 |
| 3 | `attr_type_defs` 中 `mode='comm'` **且数据表已存在**的类型名（按 type_name 排序取首个） |
| 4 | `attr_type_defs` 中 `mode='comm'` 的类型名（表尚未建，至少给出正确的名字） |
| 5 | `COMM_DEFAULT_TYPE_NAME`（`comm_records`） |

第 3 步是本次修复的关键：只要属性提取里配过通讯采集，就能自动定位到正确的表。
排序取首个保证多张通讯表时结果稳定；需要明确指定时用参数或实体覆盖。
`mode` 比较用 `LOWER(IFNULL(mode, ''))`，大小写不敏感且兼容 NULL。

### 三、改动点

- `comm.py`：新增 `resolve_comm_type_name(db_path, requested)`；`run_comm_query` 在派发前
  解析类型名（因此所有 handler 自动受益）；`CommApiView._handle` 不再预填默认名
- `onthisday.py`：`run_onthisday_query` 入口在 `type_name` 为空时调用 `resolve_comm_type_name`
  （device / env 源会忽略该参数）；`comm` 源的响应新增 `type_name` 回显；
  表不存在时的报错追加 `_comm_types_hint()`，列出已登记的通讯类型名并提示可用参数 / 实体
- `const.py`：新增 `COMM_TYPE_NAME_ENTITY_ID` / `COMM_TYPE_NAME_DEFAULT`
- `text.py`：新增实体 `text.ha_data_store_comm_type_name`（「通讯数据表类型名」），
  留空 = 自动探测；校验规则为不含空白且 ≤50 字符；`auto` / `none` / `-` 视为留空；
  `RestoreEntity` 跨重启保持
- `sensor.py`：新增 `_read_comm_type_name()`，在 `_async_refresh` 中读取后传给 `_load_data`；
  属性新增 `comm_type_name`（优先回显实际生效值，含自动探测结果）；
  设置变化监听扩展到两个实体（时间范围 + 类型名）

### 四、影响范围

历史今日（`comm` 数据源）、历史今日传感器、通讯查询 API（`/api/ha_data_store/comm`）
的默认类型名。**已有的显式 `type_name` 传参行为完全不变**。

> 版本号 → `4.6.1`。需重启 HA 生效。

---

## 2026-09-29 — v4.6.0 历史今日新增「排除实体」配置

> 「历史今日」新增排除实体清单，**API 与传感器共用同一份设置**；
> db_viewer「系统配置」新增子选项卡 **📜 历史今日** 用于维护。

### 一、功能

| 项目 | 说明 |
|---|---|
| 设置项 | 排除实体（多值，`entity_id` 列表） |
| 存储 | `api_settings.today_in_history_exclude_entities`（JSON 数组，复用既有 KV 表） |
| 生效范围 | 历史今日的**全部数据源**（`comm` / `device` / `env`）× **全部 mode**（`stats` / `detail` / `ranking` / `crosstab`） |
| 消费方 | API `GET\|POST /api/ha_data_store/onthisday` ＋ 传感器 `sensor.ha_data_store_today_in_history` |
| 临时排除 | 查询参数 `exclude_entities`（多值，逗号分隔），与设置取**并集** |
| 回显 | 响应体 / 传感器属性含 `exclude_entities`、`exclude_count` |

### 二、后端

新建 `app_settings.py`——`api_settings` 键值表的通用「字符串列表」读写：

- `get_list(db_path, key)`：JSON 数组；兼容逗号 / 换行 / **全角逗号**纯文本；库不可读返回 `[]`
- `set_list(db_path, key, values)`：去重、去空、保持顺序后以 JSON 数组存回

`recent_devices.get_exclude_entities` / `set_exclude_entities` 改为调用它（签名不变，
行为一致，但顺带获得了全角逗号兼容），因此两处排除项读写共用同一套容错逻辑。

`onthisday.py`：

- 新增 `_exclude_ids(db_path, params)`：合并「设置」与「本次参数 `exclude_entities`」，去重
- `_filters()` 重构：comm 源（走 `comm._build_filters`）与非 comm 源统一在函数末尾追加排除条件，
  于是**三类数据源、所有 mode 自动生效**
- 排除条件写作 `IFNULL("entity_id", '') NOT IN (...)`。这里有个坑：SQLite 中
  `NULL NOT IN (...)` 的结果是 NULL 而非 TRUE，直接写 `"entity_id" NOT IN (...)` 会把
  `entity_id` 为空的历史记录一并排除；用 `IFNULL` 兜底后空记录得以保留
- `run_onthisday_query()` 在入口处注入排除项（`params` 先复制，避免污染调用方），
  并在响应中回显 `exclude_entities` / `exclude_count`
- 与 `entity_ids` / `entity_id` 是**叠加（取交集）**关系：先按正向条件收窄，再剔除排除项

`http_api.py` 新增两个 View：

- `OnThisDayExcludeView` → `GET|POST /api/ha_data_store/onthisday/exclude`
  （GET 读、POST 写；保留 `api` 开关与 `db_edit` 开关校验；保存后立即刷新历史今日传感器）
- `OnThisDayEntitiesView` → `GET /api/ha_data_store/onthisday/entities`
  （扫描 `device_history` / `env_*` / `attr_*` 的 `entity_id` 并集，带名称 / 房间 / 来源表）

两者在 `__init__.py` 中随 `RecentExcludeView` 一并注册；`sensor.py` 把历史今日传感器实例
挂到 `hass.data[DOMAIN]["today_in_history_sensor"]`，供配置端保存后主动触发刷新。

### 三、前端 `db_viewer.html`

「系统配置」子选项卡新增 **📜 历史今日**（紧随「🕘 近期使用设备」，两者交互完全一致）：

- 顶部说明当前时间范围的写法与生效范围
- 排除实体：chips 列表（点 ✕ 删除）＋ 手动输入文本域（逗号 / 换行）
- 候选实体：从 `device_history` / `env_*` / `attr_*` 拉取，支持按 entity_id / 名称 / 房间 / 来源表
  搜索，逐条 ➕ 排除 / ✔ 取消
- 选项卡角标显示 `-N`（排除项数量），页面加载时即拉取
- 保存后提示「排除实体已保存（N 个），传感器已刷新」

### 四、使用提示

`entity_ids`（正向筛选）与排除项同时给出时取交集，可用于「只看某几个实体、但去掉其中之一」。
若只想临时看某次结果，直接加 `exclude_entities` 参数即可，不必改动持久化设置。

> 版本号 → `4.6.0`。需重启 HA 生效。

---

## 2026-09-29 — v4.5.2 历史今日实体改为每整点刷新

> `sensor.ha_data_store_today_in_history` 的定时刷新由「每 5 分钟」改为「每整点」，
> 并显式关闭 HA 的轮询刷新。

### 一、刷新时机

| 时机 | 实现 |
|---|---|
| 每整点（每小时 0 分 0 秒） | `async_track_time_change(hass, cb, minute=0, second=0)` |
| 设置实体变化 | `EVENT_STATE_CHANGED` 回调，立即重算三个节点 |
| HA 启动后 | 延迟 5 秒首次刷新（不等下一个整点） |

### 二、实现要点

- 用 `async_track_time_change(minute=0, second=0)` 而非
  `async_track_time_interval(timedelta(hours=1))`：后者从注册时刻起算，
  若在 10:37 启动就会固定在每小时 37 分触发，不是「整点」
- `hour` 参数留空 → 每小时都触发
- 回调签名 `(now: datetime)` 与 `TodayInHistorySensor._async_refresh(now=None)` 兼容，无需适配
- 新增 `_attr_should_poll = False`，实体不再参与 HA 默认轮询
  （此前虽未定义 `async_update` 因而无实际额外查询，但语义上不明确）
- 其它实体的定时刷新（30 秒 / 60 秒）不受影响

### 三、使用提示

`now,N` 这类含 `now` 的设置，窗口随当前时刻滑动，整点刷新意味着窗口最久滞后 1 小时。
实时性要求高时建议改用固定时刻写法（如 `09,60`），或改完设置后手动触发一次
（设置变化本身就会立即刷新）。

> 版本号 → `4.5.2`。需重启 HA 生效。

---

## 2026-09-29 — v4.5.1 历史今日时间范围改为「时间,前后分钟」写法

> 设置实体 `text.ha_data_store_today_in_history_set` 的取值由「0~23 的小时数」改为
> **`<时间>,<前后分钟>`**，直接对应历史今日接口的 `at` + `window`。

### 一、新的取值格式

| 写法 | 含义 | 等价接口参数 |
|---|---|---|
| `01,80` | 01:00 前后 80 分钟 | `at=01:00&window=80` |
| `now,60` | 此刻前后 60 分钟 | `at=now&window=60` |
| `09:02,30` | 09:02 前后 30 分钟 | `at=09:02&window=30` |
| 留空 | 全部数据（不限定） | 不传 `at` |

### 二、实现

- 新增 `onthisday.parse_window_setting(raw)`：宽松解析，返回 `(at, window)` 或 `None`
  （`None` = 不限定；`unknown` / `unavailable` / `-` / 空串一律按此处理）
- 新增 `onthisday.describe_window_setting(parsed)`：生成可读文案（`全天（不限定）` /
  `01:00 ± 80 分钟`），供传感器属性使用
- **text 实体与传感器共用同一个解析函数**：`async_set_value()` 用非空即校验的规则拒绝非法输入，
  传感器 `_read_window()` 用同一函数读取，避免两处规则漂移
- 传感器属性调整：`hours` → `at` / `window` / `range` 三项，直接回显当前口径
- 移除了 `_attr_pattern` / `_attr_native_min` / `_attr_native_max`（整数值域不再适用）

格式校验规则：必须恰好两段（逗号，中英文均可）；时间须为 `now` 或 `HH` / `HH:MM`
（时 0~23、分 0~59，冒号中英文均可）；分钟须为非负整数且 ≤ 720。

### 三、联动

设置实体变化时**立即**重算传感器的 `comm` / `device` / `env` 三个节点，并写入日志：

```
[HDS] 历史今日时间范围已变更为 01,80，立即刷新传感器
```

回调 `_on_otd_range_changed` 监听 `EVENT_STATE_CHANGED` 并比对 `TODAY_IN_HISTORY_RANGE_ENTITY_ID`；
5 分钟定时刷新保留作为兜底（`at=now` / `now,N` 的窗口会随时间滑动）。

### 四、兼容性

- `entity_id` 未变（`text.ha_data_store_today_in_history_set`），固定不变
- 常量改名：`TODAY_IN_HISTORY_HOURS_ENTITY_ID` → `TODAY_IN_HISTORY_RANGE_ENTITY_ID`
  （`TODAY_IN_HISTORY_HOURS_DEFAULT/MIN/MAX` → `TODAY_IN_HISTORY_RANGE_DEFAULT` / `_EXAMPLE`）
- 旧的纯数字值（如 `5`）在新规则下无法解析，按「全部数据」处理；重新设置一次即可
- 顺带删除 `onthisday.py` 中未使用的 `_span()` 辅助函数

> 版本号 → `4.5.1`。需重启 HA 生效。

---

## 2026-09-29 — v4.5.0 新增「历史今日」传感器与时间范围设置实体

### 一、`sensor.ha_data_store_today_in_history`

只读传感器，状态值 = 三类数据在「历年今日」下的记录总数；状态属性为三个节点：

| 节点 | 内容 |
|---|---|
| `comm` | 通讯数据：`count` / `summary`（总额汇总）/ `years`（各年汇总）/ `detail`（明细） |
| `device` | 设备开关：同上，指标为次数 / 运行时长 / 用电 |
| `env` | 环境数据：同上，指标为采样数 / 均值 / 极值 |

顶层另有 `on_this_day` / `base_date` / `hours` / `window`（如 `09:00 ~ 11:00`）/ `total`，
以及 `warnings`（某类数据源报错时的说明）。

实现要点：

- **直接复用 `onthisday.py` 的 `run_onthisday_query`**，不重复任何查询逻辑，
  因此与 API 口径完全一致
- 每个数据源取 `mode=detail`，一次拿到明细 + 各年汇总 + 总体汇总
- 明细每类最多 10 条、内容截断 60 字，避免状态属性过大被 recorder 截断
- 某一类表不存在时该节点只返回 `error`，另两类照常，`warnings` 里列出原因
- `hass.states` 只能在事件循环访问，因此先在主线程读取设置值，再进 executor 查库
- 5 分钟定时刷新；启动后延迟 5 秒首刷

### 二、`text.ha_data_store_today_in_history_set`

时间范围设置实体（`TextEntity + RestoreEntity`）：

| 取值 | 含义 |
|---|---|
| `0` | 不限定（历年今日全天） |
| `1`~`23` | 从当前整点起最后 N 小时，等价于接口的 `at=now&align=hour&hours=N` |

- 只接受 0~23 的整数，非法输入直接 `raise ValueError`（状态保持不变）
- 带 `pattern` 与 `native_min` / `native_max`，前端表单也会做初步约束
- 设置变化时通过 `hass.bus.async_listen(EVENT_STATE_CHANGED)` 立即触发传感器刷新
  （沿用项目里"设置实体 → 监听状态变化 → 手动刷新"的既有模式）
- `RestoreEntity` 跨重启保持用户设置

两个实体的 `entity_id` 固定在 `const.py` 中定义
（`TODAY_IN_HISTORY_SENSOR_ID` / `TODAY_IN_HISTORY_RANGE_ENTITY_ID`），sensor 与 text 平台各自引用。
（注：设置实体的取值格式在 v4.5.1 中改为 `<时间>,<前后分钟>`。）

### 三、顺带修复：`date` 被当成「指定单日」

`onthisday` 里 comm 数据源的业务过滤直接调用了 `comm._build_filters` 并把 `date` 一并透传，
而那里 `date` 表示"**指定单日**"，于是「历史今日」被悄悄收窄成一天。

此前未暴露的原因：测试与前端默认都用 `09-29` 这种短格式，`_parse_dt` 解析不出日期、
恰好绕过了这段逻辑；一旦传入完整日期（如传感器传 `date=2026-09-29`）就会只返回当年的记录。

修复：comm 数据源在进入 `_build_filters` 前剔除
`date` / `month` / `year` / `start` / `end` / `period`，
时间口径统一由「历史今日」自己的 `date`（基准日）与 `years` / `min_year` / `max_year` 负责。

> 版本号 → `4.5.0`。需重启 HA 生效。

---

## 2026-09-29 — v4.4.1 修复环境数据源参数冲突与参数区串味

### 一、`metric` 参数冲突（导致环境查询直接失败）

**现象**：

```
/onthisday?source=env&mode=detail&…&metric=count&…
→ {"success": false, "error": "未找到任何环境数据表（env_<metric>）…"}
```

**原因**：`metric` 一词在两个语境下含义不同——环境数据源里是「环境指标」
（`temperature` / `humidity`…），交叉汇总里是「测度」（`count` / `duration`…）。
参数区把 crosstab 的 `metric=count` 也拼进了 URL，后端便用 `count` 去拼表名 `env_count`，
自然找不到。

**修复**：

- 环境指标改用 **`env_metric`**（别名 `env_metrics`）
- 兼容处理：`metric` 的取值**确实属于环境指标名**时仍然生效，避免旧链接失效；
  取 `count` 之类的测度值则忽略，退回"合并全部指标表"
- 非法 `env_metric` 会明确报错并列出可用值
- 错误信息里现在会列出「已检查了哪些表」，便于排查表未创建的情况

### 二、参数区串味（同名字段互相覆盖）

参数区原先一次性渲染**所有模式**的字段，于是 URL 里会出现多个同名参数：

```
…&limit=1000&…&limit=100&…&limit=20&…&limit=30
```

后端只取最后一个，用户设的"明细 100 条"被交叉汇总的 `limit=30` 覆盖。

**修复**：`_otdFieldSpecs()` 只返回当前模式的字段（`modeFields[mode]`），
切换「输出模式 mode」下拉会触发 `onOtdModeChange()` 重建参数区。各模式字段集合已验证
互不重叠、自身无重复。

### 三、交叉汇总的合计语义

`grand_total` / `row_metric` / `col_metric` 原先一律求和。对 `max_value` / `min_value`
这类极值指标，求和得到的"各格最大值之和"没有意义。

现在按指标语义合并：`max` 取最大、`min` 取最小、其余求和。
实测：两个格子的 `max_value` 为 27 与 60，`grand_total` 由 87 修正为 60。

> 版本号 → `4.4.1`。需重启 HA 生效。

---

## 2026-09-29 — v4.4.0 「历史今日」抽为独立模块，支持设备与环境

> 把「历史今日」从通讯专用扩展为通用查询：新增 `onthisday.py`，
> 一套代码同时服务 **通讯 / 设备 / 环境** 三类数据源。

### 一、新模块 `onthisday.py`

```
GET|POST /api/ha_data_store/onthisday?source=comm|device|env&...
```

| source | 数据表 | 时间列 | 指标 | 维度 |
|---|---|---|---|---|
| `comm` | `attr_<type_name>` | `time` | count / duration / cost | 对方姓名 / 号码、地点、归属地、消息类型、呼叫类型、来源、我方号码 |
| `device` | `device_history` | `on_time` | count / duration / energy / avg_duration | entity_id / name / room |
| `env` | `env_<metric>` | `datetime` | count / avg_value / max_value / min_value | entity_id / name / room |

模式沿用 `stats`（默认）/ `detail` / `ranking` / `crosstab`；参数沿用基准日 `date`、
年份范围（`years` / `min_year` / `max_year` / `exclude_current`）、时刻窗口
（`at` / `align` / `hours` / `minutes` / `window` / `hour`）、明细分页与 `fields` 字段点选。

### 二、设计要点

1. **数据源描述（`Source`）抹平差异**：每类数据源声明 `time_col`、`tables`、`dims`、
   `metrics`。指标用统一的五种聚合 `count / sum / avg / max / min` 表达，因此
   设备可以按 `duration`、`energy_consumed` 聚合，环境可以按 `value` 求均值 / 极值，
   而查询代码只有一份。
2. **跨表合并放在 Python 侧**：`env` 有 6 张表，`env_temperature.value` 与
   `env_humidity.value` 语义不同。合并时 `sum` 累加、`avg` 按采样数**加权**、
   `max`/`min` 取极值——不会出现"把两个指标的平均值再平均"的错误。
   明细则各表取数后统一排序分页。
3. **`SUBSTR(<时间列>, 6, 5) = 'MM-DD'`**：三类表的时间列都是
   `'YYYY-MM-DD HH:MM:SS'` 文本，所以月日匹配、年份限定、时刻窗口的实现完全共用，
   不需要为每类表写一套（也没有 Unix 时间戳转换问题）。
4. **聚合 SQL 动态生成**：只对实际存在且被指标引用的数值列生成
   `SUM/MAX/MIN/非空计数`，`value` 为 TEXT 时用 `CAST(... AS REAL)` 兜底。

### 三、通讯侧兼容

- `/api/ha_data_store/comm?type=onthisday&...` **继续可用**，内部委托到新模块
  （固定 `source=comm`），`comm.py` 的 `_query_onthisday` 变为 bridge
- 旧 `mode=records` → `detail`、`mode=parties` → `ranking`（`dimension=party_number`）自动映射
- `comm.py` 中原历史今日实现已删除（-231 行）；`_query_crosstab` 与 `_CT_DIMENSIONS`
  因独立 `type=crosstab` 仍在使用而保留

### 四、前端

「API 工具 → 查询类型」新增分组「📜 历史今日 · 设备 / 环境」，共 8 个入口
（设备 / 环境 × 汇总 / 明细 / 排行 / 交叉汇总）；参数区由 `_otdFieldSpecs()` 按数据源
生成（含 `mode` 下拉、维度 / 指标选项、`fields` 点选），渲染逻辑抽出
`_renderParamSpecs()` 供通讯查询与历史今日共用。

> 版本号 → `4.4.0`。需重启 HA 生效（新增模块与 View）。

---

## 2026-09-29 — v4.3.9 修复「历史今日」模式未随 URL 发送 + 明细支持点选返回字段

### 一、修复：`mode` 未进入请求 URL

**现象**：在 API 工具里选「历年今日 · 明细 / 详细明细 / 排行榜 / 联系人汇总 / 交叉汇总」，
生成的 URL 里**没有 `mode` 参数**，后端因此走默认 `stats`，用户看到的一直是按年汇总。

**原因**：前端把模式信息编码在下拉选项值里（`comm:onthisday:detail`），`generateApiUrl()`
取出第二段后**只用于渲染提示文案，没有拼进查询串**；而参数区里也没有 `mode` 字段，
于是该参数从未出现在 URL 中。这也解释了为什么「历史今日」的 5 个入口行为看起来完全一样。

**修复**：

- `otdCommon` 新增「输出模式 mode」下拉，默认值即当前入口的模式（`preset`），
  由 `renderCommParams` 统一渲染 → 带 `data-name="mode"` → 被 URL 收集逻辑正常拼入查询串
- 切换该下拉会触发 `onCommModeChange()` 重建参数区（各模式的字段集不同）
- 提示区改为读取参数区的实际 `mode` 值（而非仅预设），改了就同步显示

> 顺带核对：`stats` 系列入口不受此问题影响，因为它们的 `granularity` 本身就是参数区字段。

### 二、新增：`fields` 只返回指定列

- 后端新增 `_parse_fields()`，`_select_expr(cols, fields)` 支持按用户给定顺序输出指定列：
  自动小写归一、去重、忽略不存在的列名（不会拼进 SQL）
- 生效范围：`records`、`onthisday&mode=detail`（`rows` 部分）、`longest`、`contact`（`recent` 部分）；
  汇总类输出（`year_summaries` / `summary` / `total`）不受影响
- 排序字段可以不在 `fields` 内（`ORDER BY` 不要求出现在 `SELECT` 里）

```
?type=onthisday&mode=detail&fields=time,party_number,party_name,duration
?type=records&fields=time,party_name,content&limit=50
```

### 三、前端

- 参数渲染新增 `type: 'fields'`：渲染为**复选框组**（占满整行），字段清单来自
  `COMM_FIELD_DEFS`（`id` + 13 个通讯列）
- `generateApiUrl()` 收集勾选项并拼为 `&fields=a,b,c`；全不勾选则不带该参数
- `records` / `longest` /「历史今日」的 `detail`、`records` 参数区均加入该控件

> 版本号 → `4.3.9`。需重启 HA 生效。

---

## 2026-09-29 — v4.3.8 「历史今日 · detail」改为逐条明细

> 修正 `mode=detail` 的语义：应当返回**每一条历史今日的数据**，
> 而不是 v4.3.7 中按年嵌套的汇总档案。

### 现在的返回结构

```json
{
  "mode": "detail",
  "on_this_day": "09-29",
  "base_date": "2026-09-29",
  "count": 100,              // 本次返回的明细条数
  "total": 500,              // 匹配总数（不受分页影响）
  "limit": 100,
  "offset": 0,
  "years": ["2025", "2024", ...],
  "year_summaries": [ { "year": "2025", "count": 12, "duration": 3000, ... } ],
  "summary": { "count": 500, "duration": 121701, ... },
  "rows": [ { "id": .., "time": "2025-09-29 21:26:45", "party_number": .., "party_name": ..,
              "msg_type": .., "location": .., "duration": .., "cost": .., "content": ..,
              "image_path": .. } ]
}
```

- **`rows`**：跨年份的**扁平明细列表**，每条含完整字段（与 `records` 同一套列），按时间倒序
- **`total`**：匹配总数，`count` 是本次返回条数（分页用）
- **`year_summaries` / `years`**：各年汇总，供年度对比（`with_years=0` 可关闭）
- **`summary`**：总体汇总

### 参数调整

| 参数 | 说明 |
|---|---|
| `limit` / `offset` | 明细条数与偏移（默认 100 / 0，上限 1000） |
| `sort` / `order` | 排序字段与方向（默认 `time` / `desc`） |
| `content_len` | 内容截断长度（默认 0 = 不截断） |
| `with_years` | 是否附带各年汇总（默认 1） |
| `years_limit` | 最多汇总多少个年份（默认 20） |
| `at` / `align` / `hours` / `minutes` / `hour` | 沿用「历史今日」时刻筛选 |

原先用于按年嵌套的 `record_limit` / `party_limit` 已移除。

### 前端

「📜 历史上的今日」入口更名为「历年今日 · 详细明细（逐条记录 + 各年汇总对比）」，
参数区改为明细分页 / 排序 / 汇总开关，提示区说明 `rows` 即逐条记录。

> 版本号 → `4.3.8`。需重启 HA 生效。

---

## 2026-09-29 — v4.3.7 「历史上的今日」新增详细档案（detail）

> 「历史今日」原有 5 个模式各覆盖一个侧面（汇总 / 排行 / 联系人 / 此刻明细 / 交叉汇总），
> 缺少一个把某年今日"全部信息"打包的接口。新增 `mode=detail` 补齐。

### `mode=detail` — 历年今日详细档案

按年组织，**每年一条**，包含：

| 字段 | 内容 |
|---|---|
| `summary` | 该年今日的条数 / 时长 / 金额 / 去重联系人数 / 活跃天数 / 平均时长 / 首末时间 |
| `parties` | 该年今日的联系人 Top N（号码、姓名、条数、时长、金额、首末时间） |
| `by_hour` | 该年今日的时段分布（各小时条数与时长） |
| `records` | 该年今日的明细（时间倒序，内容可截断） |

顶层返回 `years`（年份倒序）、`count`、`on_this_day`、`base_date` 与规模回显。

专属参数：

| 参数 | 说明 |
|---|---|
| `limit` | 最多返回多少个年份（默认 10，上限 50） |
| `record_limit` | 每年返回的明细条数（默认 20，0 = 不返回明细） |
| `party_limit` | 每年返回的联系人数（默认 10） |
| `content_len` | 明细内容截断长度（默认 80，0 = 不截断） |
| `at` / `align` / `hours` / `minutes` / `hour` | 与「历史今日」一致，可只看某时刻 |

实现要点：汇总 / 联系人 / 时段分布各用**一条 SQL 一次性取出全部年份**，在 Python 侧按年
切分与截断；只有明细按年分别查询（保证每年都能拿到足够条数）。因此 N 年也只需 `N + 3` 次
查询，而非 `4N` 次。

### 前端

「📜 历史上的今日」分组新增第 6 个入口「历年今日 · 详细档案」，
`_commFieldSpecs('onthisday', 'detail')` 渲染专属参数区，提示区说明每年返回的四个部分。

> 版本号 → `4.3.7`。需重启 HA 生效（后端新增模式）。

---

## 2026-09-29 — v4.3.6 修复「历史上的今日」缺 type_name 参数

> 「历史今日」参数区漏了 `type_name`（类型名），当数据表不用默认的 `comm_records` 时
> 无法指定目标表，查询会报「未找到通讯数据表」。

- 在「历史今日」的公共参数组 `otdCommon` 中补上 `type_name`，其 5 个模式
  （汇总 / 排行榜 / 联系人汇总 / 明细 / 交叉汇总）全部生效
- 其余查询类型（明细 / 统计 / 交叉汇总 / 周期对比 / 联系人档案 / Top N / 质量 / 概览等）
  原本已提供该参数，无需改动

> 本改动仅涉及 `db_viewer.html`，**刷新浏览器即可生效**（无需重启 HA）；
> 版本号本身来自 `const.py`，需重启后才会更新显示。

---

## 2026-09-29 — v4.3.5 通讯查询新增 5 个分析接口 + 「此刻」时间粒度

> 新增 `compare` / `contact` / `longest` / `quality` / `meta` 五个查询类型；
> 「历史今日 · 此刻」支持按整小时 / x 小时 / x 分钟取区间。

### 一、新增查询类型

| type | 用途 | 关键参数 |
|---|---|---|
| `compare` | 周期对比：当前 vs 上一周期（环比）vs 去年同期（同比），含差值与增长率 | `period=day\|week\|month\|quarter\|year`、`date`、`compare=prev\|yoy\|both` |
| `contact` | 单联系人档案：总量 / 首末通讯 / 多久没联系 / 平均间隔 / 各维度分布 / 最近明细 | `party_number` 或 `party_name`、`recent` / `top_days` / `months` |
| `longest` | 单次 Top N：最长通话 / 最高金额 | `by=duration\|cost`、`order`、`limit` / `offset` |
| `quality` | 数据质量：空值 / 异常值 / 疑似重复 / 时间覆盖 | `dup_limit` |
| `meta` | 数据概览：表结构 / 总量 / 时间跨度 / 维度取值清单 | 任意过滤 |

实现要点：

- `compare` 用 `_shift_to_period()` 计算「当前 / 上一 / 去年同期」三个区间：上一周期由
  「当前起点 − 1 天」反解，同比用「基准日年份 − 1」（2 月 29 日回退 365 天）；
  `period=year` 时同比不适用，显式返回 `yoy: null`。
- `contact` 的 `days_since_last`（多久没联系）与 `avg_interval_days`（平均联系间隔 =
  跨度天数 / (活跃天数 − 1)）在 Python 侧计算，其余分布复用统一过滤。
- `quality` 的检查项以表驱动（`_QUALITY_CHECKS`）声明「依赖列 + 判定条件」，
  依赖列不存在时自动跳过，便于以后加项。
- `meta` 不排除 time 为空的行（概览要看全量），仅应用调用方过滤。

### 二、「历史今日 · 此刻」时间粒度

原 `at` + `window`（以某时刻为中心 ± N 分钟）保留，新增：

- `align`：起点**对齐**，`hour`（整点）/ `30` / `15` / `5` / `min`，支持中文别名「整点」
- `hours` / `minutes`：自对齐后的起点向后的**跨度**

于是「现在 09:02，查 09:00~10:00」写作 `at=now&align=hour`；
「最近 2 小时」写作 `at=now&align=hour&hours=2`；「09:02 起 30 分钟」写作 `at=09:02&minutes=30`。
优先级：`hours` / `minutes` > `align` 默认跨度（一个对齐单位）> `window`。

### 三、顺带修复与增强

- **修复 SQL 拼接缺陷**：`crosstab` 在「无任何过滤 + 指定 `cols`」时会拼出
  `FROM "tbl" AND (...)` 的非法 SQL（此前的测试用例都带了过滤，掩盖了该问题）。
  新增 `_and_sql()` 统一处理有无 `WHERE` 两种情形，并补了回归测试。
- **通用过滤新增单数别名**：`party_number` / `party_name` / `my_number` 现在等价于
  对应的多值参数，所有查询类型（含 `stats` / `crosstab` / `longest` …）统一支持。
- `invalid_time` 检查升级：除长度外还判定字段越界（如 `2026-13-01`）。

### 四、前端 `db_viewer.html`

「📞 通讯数据查询」分组新增 5 个入口（周期对比 / 联系人档案 / 单次 Top N / 数据质量 /
数据概览），`comm:` 选项共 25 个；「历史今日 · 明细」参数区新增 `align` / `hours` /
`minutes`，提示区给出「09:02 → 09:00~10:00」的写法说明。

> 版本号 → `4.3.5`。需重启 HA 生效（后端新增查询类型）。

---

## 2026-09-29 — v4.3.4 通讯查询新增「历史上的今日」与「交叉汇总」

> 新增 `onthisday`（历史上的今日）与 `crosstab`（交叉汇总）两个查询类型；
> API 工具新增独立分组「📜 历史上的今日」。

### 一、`onthisday` — 历史上的今日

按「月日相同」筛选历年同一天的记录，再按 `mode` 输出五种视角：

| mode | 用途 |
|---|---|
| `stats` | 历年今日汇总：`granularity=year`（每年今日）/ `month`（每年该月）/ `day`（每年该日），默认 `year` |
| `ranking` | 历年今日排行榜：`dimension` + `by` |
| `parties` | 历年今日联系人汇总：对方号码 / 姓名 + 条数 / 时长 / 金额 / 首末时间 |
| `records` | 历年今日明细：`at` / `window` / `hour` 限定时刻（`at=now` 即「此刻」） |
| `crosstab` | 历年今日交叉汇总：`rows` × `cols` |

专用参数：

| 参数 | 说明 |
|---|---|
| `date` | 基准日：`09-29` 或 `2026-09-29`；留空 = 今天 |
| `years` | 只看这些年份，多值：`2024,2025` |
| `min_year` / `max_year` | 年份范围 |
| `exclude_current=1` | 排除今年，只看往年 |
| `at` | 只看某时刻：`17:24` 或 `now`（此刻） |
| `window` | `at` 的分钟窗口：`at=17:24&window=30` → 17:24 ± 30 分钟 |
| `hour` | 按时段筛选：`9` / `9,10` / `9-18` |

实现要点：

- 复用机制：新增 `_collect_filters()`，允许通过内部键 `__extra_where` / `__extra_args` 向**所有**
  查询注入额外约束。`onthisday` 因此能直接把「月日相同 + 年份 + 时刻」条件注入 `stats` /
  `ranking` / `parties` / `records` / `crosstab`，无需为每种模式重复实现。
- `_prepare_onthisday_params()` 会把 `date` 语义改为「基准日」并移除 `month` / `start` / `end` /
  `period`，避免被通用时间过滤误用（否则「历年今日」会被收窄成单日）。
- 时刻过滤用「小时×60 + 分钟」的数值区间实现，`at` / `window` / `hour`（含 `9-18` 区间与
  多值）统一走同一比较表达式。
- `type=anniversary` 与 `type=onthisday` 等价。

### 二、`crosstab` — 交叉汇总

行维度 × 列维度的度量矩阵：

- `rows`：1~3 个维度（多维度用 ` | ` 拼接为键）
- `cols`：0~1 个维度（留空则只输出各行合计）
- `metric`：`count`（默认）/ `duration` / `cost`
- 可用维度：`msg_type` / `location` / `party_place` / `call_type` / `party_name` /
  `party_number` / `channel` / `my_number`（与排行榜 `_RANK_DIMENSIONS` 共用白名单）
- 返回 `row_keys` / `col_keys` / `matrix` / `row_metric` / `col_metric` / `grand_total`，
  行按度量降序取 Top N（`limit`），列按合计降序取 Top N（`col_limit`）
- 该类型也可独立使用（不限「历史今日」），对全部或指定范围的数据生效

### 三、前端 `db_viewer.html`

- 新增独立分组「📜 历史上的今日」，含 5 个入口（汇总 / 排行榜 / 联系人汇总 / 明细 / 交叉汇总），
  选项值形如 `comm:onthisday:records`，第二段即 `mode`，决定参数区的字段组合与提示
- 「📞 通讯数据查询」分组新增「交叉汇总」入口
- 新增 `COMM_DIMENSION_KEYS`（与后端维度白名单保持一致）与 `COMM_OTD_MODE_LABELS`
- 提示区会说明「历史今日」下 `month` / `start` / `end` 不生效，并列出可替代参数

> 版本号 → `4.3.4`。需重启 HA 生效（后端新增查询类型）。

---

## 2026-09-29 — v4.3.3 通讯查询新增「统计分析」（stats）

> 通讯查询新增 `stats` 类型：按 年 / 季度 / 月 / 周 / 日 / 小时 / 星期 分组汇总，
> 可叠加号码、姓名、地点等过滤条件；API 工具的「📞 通讯数据查询」分组同步新增 6 个入口。

### 一、后端 `comm.py`

| 能力 | 说明 |
|---|---|
| 分组汇总 | 按 `granularity` 分桶，每桶返回 `count` / `duration` / `cost` / `party_count`（去重联系人数）/ `active_days`（活跃天数）/ `avg_duration`（平均时长） |
| 合计与均值 | 响应含 `total`（**独立聚合一次**，去重指标不会因按桶累加而出错）与可选 `avg_per_bucket`（每桶均值） |
| 排序 | `sort=time`（默认按时间升序，时间线）/ `sort=value`（按 `by=count\|duration\|cost` 取 Top N，默认降序）；`order` 可显式覆盖 |
| 补全 | `fill=1` 时补全 年/季度/月/周/日 的空缺桶（无数据补 0，各指标同步置 0） |
| 粒度别名 | `granularity` 支持别名与中文：`年` / `月` / `日` / `季度` / `周` / `小时`、`yyyy` / `ym` / `ymd` / `date` 等 |
| 周期换算 | `period` 支持 `2026` / `2026-09` / `2026-09-01` / `2026-Q3` / `2026-W35`（后两者换算为 start+end 闭区间） |

三种常见统计的写法：

```
全部数据按年汇总      ?type=stats&granularity=year
全部数据按年月汇总    ?type=stats&granularity=month
指定年 → 按月汇总     ?type=stats&granularity=month&year=2026
指定年月 → 按日汇总   ?type=stats&granularity=day&month=2026-09
指定号码 → 按年汇总   ?type=stats&granularity=year&party_numbers=138…,139…
指定姓名 → 按月汇总   ?type=stats&granularity=month&party_names=张三,李四
```

顺带扩展：时间桶表达式新增 `quarter` / `week`，`trend` 也能用；`granularity` 解析统一走
新的 `_resolve_granularity()`（含别名与非法值校验）；`_fill_buckets()` 支持 周 / 季度 补全。

### 二、前端 `db_viewer.html`

「API 工具 → 查询类型 → 📞 通讯数据查询」新增 6 个入口：

- 统计分析 · 全部数据按年汇总
- 统计分析 · 全部数据按年月汇总
- 统计分析 · 指定年 → 按月汇总
- 统计分析 · 指定年月 → 按日汇总
- 统计分析 · 指定号码/姓名 → 按年/年月/年月日汇总
- 统计分析 · 自定义（任选粒度）

选项值形如 `comm:stats:month_in_year`，第二段是**预设**，只决定参数区默认粒度与提示文案，
所有参数仍可修改。统计参数区独立渲染（`_commFieldSpecs('stats', preset)`），含 28 个过滤 / 汇总项；
提示区会显示当前预设的填写指引。

> 版本号 → `4.3.3`。需重启 HA 生效（后端新增查询类型）。

---

## 2026-09-28 — v4.3.2 通讯表新增「图片路径」字段

> 通讯数据采集新增第 13 个固定字段 `image_path`（TEXT，非必填），
> 用于存储图片消息的文件路径 / URL。

### 一、字段定义

| 列 | 类型 | 标签 | 必填 |
|---|---|---|---|
| `image_path` | TEXT | 图片路径 | 否 |

- 加入 `const.py` 的 `COMM_FIELDS`，随之进入 `COMM_COLUMNS` / `COMM_COLUMN_TYPES` 白名单，
  `AttrConfigView` 的目标列白名单校验与前端固定字段表自动生效
- **已存在的表自动补列**：`_ensure_comm_columns()` 幂等，重启后保存一次采集配置
  （或等下一次采集触发）即执行 `ALTER TABLE ADD COLUMN`，**不需要重建表、不丢数据**
- 不参与索引（`COMM_INDEX_DEFS` 不变），不影响既有查询性能

### 二、前端（属性提取 → 通讯字段映射）

- 固定字段映射表新增一行「图片路径 image_path」，同样支持**选源字段**或**直接填固定值**
- 常见别名自动预选：`image` / `img` / `img_path` / `pic` / `picture` / `photo` /
  `image_url` / `img_url` / `file_path` / `path` / `local_path`

### 三、查询

- `comm.py` 的 `_OUTPUT_COLUMNS` 加入 `image_path`，`type=records` 明细会返回该列
- 排行榜 / 趋势 / 汇总等聚合接口不受影响

> 版本号 → `4.3.2`。需重启 HA 生效（后端字段定义变更）。

---

## 2026-09-27 — v4.3.1 数据库浏览器新增「清空表」

> 清空指定表的全部数据并把自增 ID 归零（等价于 `TRUNCATE TABLE`），需管理员密码。

### 一、后端新增 `ClearTableView`

```
POST /api/ha_data_store/clear_table
     body:  {"table": "xxx", "admin_password": "xxx", "vacuum": false}
     或 query: ?table=xxx&admin_password=xxx
```

- 校验链：主开关 → **核心表保护**（与「删表」同一份名单）→ 非空管理员密码 → `_verify_admin()` 密码比对；
  额外拒绝 `sqlite_*` 系统表
- 执行：`DELETE FROM "表"` + `DELETE FROM sqlite_sequence WHERE name='表'`
- **自增 ID 重置**：`AUTOINCREMENT` 表的序列记在 `sqlite_sequence`，删掉对应行后下一个 id 回到 1；
  普通 `INTEGER PRIMARY KEY` 表清空后本就会从 1 重新分配，因此两种表都覆盖
- 返回 `{success, table, deleted: 删除行数, reset_auto_increment: 是否重置了序列, message}`
- 可选 `vacuum: true` 顺带回收磁盘空间（默认 false，前端提示用户手动点「🗜 压缩」）
- 审计：操作写入本地日志（`_LOGGER.warning`）

### 二、前端（数据浏览页工具栏）

- 新增 **🧹 清空表** 按钮，位于「🗑 删表」右侧
- 交互：`prompt` 输入管理员密码（复用全局 `adminPw` 缓存）→ **二次 `confirm` 确认** → 执行
- 成功后刷新当前页与表清单，并提示「如需回收磁盘空间，可点「🗜 压缩」」

### 三、验证

以临时 SQLite 验证 **16 项** SQL 行为，全部通过：

- `AUTOINCREMENT` 表清空后 id 从 1 重新开始；连续两次清空同样生效
- 未清空时 id 正常递增（对照组）
- 空表清空返回 0 行、且无序列记录可重置
- 无 `AUTOINCREMENT` 的表清空后 id 同样从 1 开始
- 清空后 `sqlite_sequence` 中该表记录已移除

> 版本号 → `4.3.1`。需重启 HA 生效（后端改动）。

---

## 2026-09-27 — v4.3.0 数据导入 / 导出（CSV · JSON）

> 在数据浏览页把外部数据导入任意数据表，或把表数据导出为 CSV。
> 新增后端模块 `data_import.py`、前端导入面板与两个工具栏按钮。

### 一、新增后端模块 `data_import.py`

| 方法 | 路径 | 用途 | 鉴权 |
|---|---|---|---|
| POST | `/import/parse` | 解析 CSV / JSON 文本 → 列名 + 全部行 + 前 20 行预览 | 主开关 + 数据库浏览器开关 |
| POST | `/import` | 单批导入，支持 `dry_run` 试运行 | 上述 + **数据库修改开关** |
| GET | `/import/template?table=x` | 下载目标表空模板 CSV（仅列头） | 主开关 + 数据库浏览器开关 |
| GET | `/export/csv?table=x` | 导出表数据为 CSV（`limit` / `order_by` / `order_dir`） | 同上 |

鉴权刻意**不强制 API Key**（与 `AttrConfigView` 等 db_viewer 管理接口一致），
避免用户尚未创建任何密钥时管理页面被 403。

### 二、文本解析

- **CSV**：自动嗅探分隔符（`,` `;` `\t` `|`）、正确处理引号内的逗号与换行、剥离 BOM；
  支持无表头（列名回落为 `列1/列2/…`）；短行自动补空单元格对齐列数
- **JSON**：对象数组 / `{"data":[...]}` 包装 / 数组的数组 三种形态；列名取所有对象键的并集（保序）；
  嵌套 dict / list 值序列化为 JSON 串
- **流式解析**：CSV 走生成器逐行产出，内存只保留当前行；解析接口只回传
  **列名 + 总行数 + 前 20 行预览**，**不回传全量数据**（全量数据留在客户端，导入时按片提交）
- 单次提交文本上限 32 MB（客户端会先分片，单片约 200 万字符）
- 前端读取文件时按字节检测编码：UTF-8 解码出现替换字符则改用 GBK
  （Excel 导出的中文 CSV 默认 GBK），浏览器不支持 gbk 时保持 UTF-8

### 三、字段映射

```
mapping = { "源列名": "目标列", "=固定值": "目标列" }
```

- 命中数据源列名 → 取该列的值；以 `=` 开头 → 字面量固定值
- 前端在输入内容不匹配任何源列时**自动补 `=` 前缀**，也可手动强制
- 前端以**目标列为基准**渲染映射表（与属性提取的通讯模式同一套交互），边框绿=源列 / 橙=固定值
- 同一取值被多个目标列共用时前端直接拦截（`field_mapping` 的 key 天然唯一，否则会静默丢一个映射）

### 四、写入模式

| 模式 | 行为 |
|---|---|
| `append` | 全部插入新行 |
| `upsert` | 先把现有唯一键 → rowid 读进内存，再按批 `executemany` 分流插入与更新；**同一批内重复唯一键保留最后一条**；唯一键支持多列组合 |

- **流式分批**：解析与写入交织，每 2000 行处理一批（转换 → 判定 → 写入），
  内存占用与总行数无关（不再先把全部行转成任务列表）；批内再按 500 行一次 `executemany`，
  单事务提交；`dry_run` 时同样走完整流程但不写库
- `executemany` 整批失败时**降级为逐行重试**并记录失败行号，不因一行错误丢弃整批
- 空单元格：可空列 → NULL；`NOT NULL` 列 → 取 DDL 默认值（无默认值时按类型回退 `''` / `0`）

### 五、自动建表 / 补列

- 表不存在且勾选「自动建表」：按数据推断列类型（采样 1000 行，全整数 → INTEGER、
  全数值 → REAL、其余 TEXT）
- 目标表缺少列且勾选「自动补列」：按同一套推断 `ALTER TABLE ADD COLUMN`
- **`dry_run` 完全不改表结构**，仅在返回里说明「将会建哪些表列 / 补哪些列」

### 六、值转换（基础层，可分别关闭）

| 输入 | 输出 |
|---|---|
| `¥1,234.50` / `12.5元` | `1234.5` / `12.5`（去货币符号、千分位、常见单位后缀） |
| `2026/9/1 17:24` / `2026年9月1日` | `2026-09-01 17:24:00` / `2026-09-01 00:00:00` |
| `3分53秒` | **不转换**（本版按需求只做基础层，原样写入） |

### 七、安全

- 表名（`[A-Za-z_][A-Za-z0-9_]*`）与列名双重白名单校验，全部参数化写入，不拼接任何用户输入
- 核心配置表（`entity_configs` / `attr_type_defs` / `custom_routes` / `export_configs` /
  `file_source_configs` / `api_source_configs` / `api_keys` / `api_settings` /
  `vacuum_type_defs` / `vacuum_configs` / `push_targets`）直接拒绝导入
- SQLite 系统表（`sqlite_*`）拒绝导入；导入面板的表下拉也不再过滤内部表（由后端把关）

### 八、前端（数据浏览页）

- 工具栏新增 **📥 导入数据** 与 **⬇️ 导出CSV**
- 导入面板 5 步：数据源 → 目标表 → 字段映射 → 写入模式（唯一键多选）→
  执行（🧪 试运行 / ▶ 开始导入 / ⬇️ 下载模板）
- 结果报告：新增 / 更新 / 失败数、耗时、建表与补列明细、最多 50 条错误行（含行号）
- **大文件分片**：解析阶段不再回传全量数据，只取列名 / 总行数 / 前 20 行预览；
  导入阶段把文本按行切成每片约 200 万字符（每片自带表头、并保证不切断引号内的跨行字段），
  分多次提交，避免撞上 HA 的请求体上限（约 16 MB）。分片之间无会话状态，每片都是独立的完整 CSV
- 分片导入的进度按「第 N / M 次提交」显示，错误行号会按已处理行数偏移还原成整体行号
- 导出 CSV 使用 `utf-8-sig`（带 BOM），Excel 双击不乱码

### 九、验证

后端以真实代码 + 临时 SQLite 跑 **61 项**端到端测试：CSV / JSON / 无表头 / TSV 解析、
引号内逗号与**引号内跨行字段**、分隔符嗅探、值转换、固定值、追加、Upsert（批内去重、多列键）、
自动建表与类型推断、自动补列、`dry_run` 无副作用、8 类非法输入拦截、空值处理、
**5000 行流式分批落库**、**分片导入（每片自带表头独立提交）**、兼容路径（columns + rows）、模板与导出，
全部通过。

> 版本号 → `4.3.0`（`manifest.json` + `const.py`）。需重启 HA 生效。

---

## 2026-09-27 — v4.2.0 通讯数据采集模式 + 通讯数据查询 API

> 把手机端同步过来的通讯记录（通话 / 短信 / 微信 / QQ 等）落库并可查询。
> 采集复用「属性提取」链路（新增 `comm` 模式），查询为独立模块 `comm.py`。

### 一、属性提取新增 `comm` 采集模式

改动集中在三处（全项目 `mode` 判断只有 4 个位置）：

| 文件 | 改动 |
|---|---|
| `const.py` | 新增 `ATTR_MODE_COMM`、`COMM_FIELDS`（12 列固定定义）、`COMM_INDEX_DEFS`、`COMM_COLUMNS` / `COMM_COLUMN_TYPES` / `COMM_REQUIRED_COLUMNS`、`COMM_DEFAULT_TYPE_NAME` |
| `__init__.py` | `_ensure_attr_table` 读取 mode，comm 走**固定列分支**（12 列全建、类型固定）并补建索引；`_attr_collect_for_entity` 的数组展开分支纳入 comm |
| `http_api.py` | `AttrConfigView` mode 白名单加 comm；comm 额外校验目标列白名单、`time` 必填、列类型强制为约定类型、`compare_limit` 下限 100 |

- 复用列表展开（list）的全链路：配置、三路触发（poll / event / 手动）、去重
- 12 个固定字段与类型：`my_number` / `party_number` / `party_place` / `party_name` / `time`、
  `location` / `msg_type` / `channel` / `call_type`（以上 TEXT）、`duration`（INTEGER 秒）、
  `cost`（REAL 元）、`content`（TEXT）
- 建表自动补索引：`time`、`(party_number,time)`、`(party_name,time)`、`(location,time)`
- 因列类型固定，查询侧可直接 `SUM(duration)` / `SUM(cost)`，不会再出现把时长列勾成 TEXT 导致聚合失效

### 二、采集期值转换（`__init__.py`）

- `_normalize_comm_time()`：`2026-09-01T17:24:33+08:00` / `2026/9/1` / 毫秒时间戳 →
  `YYYY-MM-DD HH:MM:SS`
- `_parse_comm_duration()`：时长统一解析为秒，支持
  `27秒` / `3分53秒` / `1小时2分3秒` / `3分53`（省略"秒"）/
  `1小时30`（省略单位时按上一单位降一级推断）/ `3:53`（分:秒）/ `1:02:03`（时:分:秒）/
  `233`（纯数字秒）/ `27s` / `3m53s` / `1h2m3s`；**33 个用例**验证通过
- `_normalize_comm_row()`：写入前统一走上述转换，并把 `cost` 转为数值
- 固定值同样走转换（`=3:53` 也会被解析为 233）

### 三、新增查询模块 `comm.py`

`GET|POST /api/ha_data_store/comm?type=...`，8 类查询：

| type | 说明 |
|---|---|
| `records` | 明细列表（排序、分页、内容截断） |
| `dates` | 哪些日期有数据（每天条数/时长/金额/联系人数），支持任意过滤 |
| `ranking` | 排行榜：`granularity=year\|month\|day` + `period` 指定年/月/日后按维度排名；不填 period 即全部数据排名 |
| `trend` | 趋势序列：`day\|month\|year\|hour\|weekday` 分桶，`fill=1` 补零 |
| `summary` | 汇总统计 + 来源/消息类型/呼叫类型分布 + 活跃天数 |
| `parties` | 联系人清单（按对方号码聚合） |
| `places` | 通讯地点清单 |
| `heatmap` | 星期 × 小时分布 |

通用过滤：单日 / 单月 / 单年 / 自定义区间、多号码、多姓名（模糊）、多地点、多归属地、
多来源 / 消息类型 / 呼叫类型、内容关键词、时长与金额区间；多值参数逗号分隔。

### 四、修复（属性提取既有问题）

- **去重窗口全表加载**：数组展开模式的去重 lookup 原为
  `SELECT * FROM tbl WHERE entity_id=? ORDER BY datetime DESC`（**全表读入内存**，
  `compare_limit` 读了却没用）。通讯级数据量下每次采集都会拖死。
  改为按窗口查询（`compare_limit` 与 `2 × 本次条数` 取大，clamp 到 100～200000）。
  该修复对既有 `list` / `multi` 模式同样生效
- **采集参数改了不生效**：`AttrConfigView` 在类型已存在时只更新 `field_mapping` 与 `extra_fields`，
  `compare_limit` / `array_path` / `key_field` / `decimal_places` 不写库。
  通讯模式已放开更新；非通讯模式保持原行为
- **字段取值支持固定值**：新增 `_resolve_field_value()`，`=` 前缀即字面量，
  `list` / `multi` / `comm` 与 `fields` 模式全部生效，旧配置不受影响
  （没有字段名会以 `=` 开头）

### 五、前端（`db_viewer.html`）

- 「系统配置 → 📊 属性提取」新增第四种模式「通讯数据采集」：切换后显示固定字段映射表
  （12 行，目标列与类型只读）；源字段列改为**可输入 + 可下拉**（datalist），
  输入内容自动判定源字段（绿）/ 固定值（橙），支持 `=` 前缀强制；同值被多用时拦截
- 「对比最近条数」「小数位数」在通讯模式下不再隐藏（`compare_limit` 默认 1000、
  上限由 500 放宽到 200000），并补了参数语义说明
- 属性类型列表新增「通讯数据」模式标签
- API 工具「查询类型」新增 **📞 通讯数据查询** 分组（8 个 type），参数区按类型动态渲染
- 抽出公共函数 `resolveMapValue()`，通讯采集与数据导入共用同一套「源列 / 固定值」判定

### 六、验证

- 以真实代码 + 临时 SQLite 跑 **57 项**端到端测试：时间区间解析（含 `end` 传纯日期的边界）、
  records 13 项、dates、ranking、trend、summary、parties / places / heatmap、错误处理
- 单独验证 **33 项**时长解析用例
- 期间修复：`end` 传纯日期时被当作「当天 00:00:00 + 1 秒」，导致当天数据全被过滤（真 bug）；
  `summary` / `parties` / `places` 硬编码 `MIN(time)`/`MAX(time)`，缺 time 列时直接 SQL 报错
- 类型注解补齐（裸泛型 `-> dict` / `-> set` 等 48 处报告降到 2 处环境性的 import 提示）

> 版本号 → `4.2.0`。需重启 HA 生效（涉及 Python 改动）。

---

## 2026-09-26 — v4.1.0 定时精灵并入本集成（domain: timer_backend → ha_data_store）

> 目标：减少一个集成。原 `timer_backend`（定时精灵）的引擎、传感器、接口全部并入
> `ha_data_store`，持久化从 JSON 文件换成集成自有 SQLite。

### 一、引擎（`timer_elves.py`）

沿用此前已移植但**从未接线**的 `TimerElvesCoordinator`（与 `timer_backend/coordinator.py`
逐方法对等，61 个关键方法无缺失），本版把它真正装进集成，并补齐/修复：

| 项 | 说明 |
|---|---|
| 装配 | `__init__.py::async_setup_entry` 创建并 `async_setup()`；`hass.data["ha_data_store"]["timer_elves"]`；`async_unload_entry` 卸载 |
| 持久化 | 只用 `timer_tasks` 表（不再有 JSON 文件）。表在 `task_data`(JSON) 之外增加 12 个冗余查询列（entity_id/entity_name/task_type/status/action_desc/duration/repeat_type/created_at/end_time/next_execution/executed_at/execution_result）+ 2 个索引，便于历史查询与 db_viewer 直接查看；旧三列结构用 PRAGMA + ALTER 就地补列 |
| 强制落盘 | `save_tasks(force=True)` 绕开 5 秒节流；`async_unload` 用它，避免卸载丢最后 5 秒的变更 |
| 重启恢复修复 | 原移植版对未来任务一律注册 `execute_timer`，导致空调/窗帘定时器重启后丢 `restore_previous`；现按 `is_climate`/`is_cover` 分派对应执行器 |
| 兜底调度 | 新增每小时 `check_recurring_schedules()`（原 `timer_backend` 靠 __init__ 里 1 天间隔；本版收紧到 1 小时，防止睡死/漏触发后周期任务不再自愈） |
| 句柄回收 | bus 监听（`ha_data_store_timer_event`、`state_changed`）与兜底 tick 的取消句柄统一存 `_unsubs` / `_housekeeping_unsub`，卸载时全部回收 |
| 新增能力 | `create_task()`（API 创建入口，按调用前后任务差集判定成败）、`update_task()`（原地改时长/动作/周期并重排句柄）、`get_summary()`、`get_task()`、`get_history()`（SQL 分页 + 实体/状态/时间区间过滤） |

### 二、实体（`sensor.py`）

新增 **`sensor.ha_data_store_timer`**（唯一 id `ha_data_store_timer`，注册表预注册固定实体 ID）：

- 状态值 = 活跃任务数（一次性定时器 + 周期任务）
- 属性：`active_timers` / `active_schedules` / `active_tasks` / `total_tasks` /
  `current_task` / `successful_task` / `failed_task` / `today_task` / `all_task_list` /
  `time_zone` / `updated_at`
- 刷新：订阅 `TIMER_SIGNAL_UPDATE_SENSOR`（此前该信号**只发无人收**，现在有了消费者）
  + 30 秒轮询兜底 + 启动 3 秒后首刷

### 三、HTTP API（`timer_elves_api.py`，全部挂在 `/api/ha_data_store/timer`）

不注册任何 hass 服务（本集成服务体系保持只有 `generate_daily_summary`）。

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/timer` | 概览：活跃定时器/周期任务 + 统计 + 时区 |
| GET | `/timer/summary` | 纯统计 |
| GET | `/timer/tasks` | 任务列表（`?task_id= &entity_id= &status= &active=1`） |
| GET | `/timer/history` | 历史分页（`?limit= &offset= &entity_id= &status= &start= &end= &with_data=1`） |
| POST | `/timer` | 创建 `{kind: timer\|climate\|cover\|schedule, entity_id, duration?, repeat_type?, schedule_time?, weekdays?, month_days?, action_type?, action_data?}` |
| POST | `/timer/update` | 修改 `{task_id, patch:{duration?, action_type?, action_data?, repeat_type?, schedule_time?, weekdays?, month_days?}}` |
| POST | `/timer/cancel` | 取消 `{task_id\|schedule_id\|entity_id}` |
| DELETE | `/timer?id=xxx` | 取消（query 形式） |

- 鉴权：查询只受「API 访问」开关约束（db_viewer 可直接调）；写入在此之上再受「数据库修改」开关约束。
- 兼容旧式 `action` 调用（`create_timer` / `create_schedule` / `cancel_timer` / ... 字段与旧版一致）。
- 补齐旧版 API 的退化：`get_timers` 现在同时返回 schedules（旧版只返 timers）。

### 四、事件总线与前端

- 事件前缀统一为 `ha_data_store_timer_event`（前端 → 后端）与
  `ha_data_store_timer_response`（后端 → 前端）。
- 前端事件新增 `update_task` / `get_history` 两个 action 分支。
- `timer-control-card.js`:7 处改动并已发布到 `/config/www/`：
  3 处 `fire_event` 事件名、3 处响应事件监听、1 处历史数据源实体 ID
  （`sensor.timer_active_tasks` → `sensor.ha_data_store_timer`）。

### 五、边界与数据

- 与 `automations.py`（简单自动化引擎：定时/间隔/条件 + `automation_logs`）
  **独立并存**，两套调度互不干扰；定时精灵负责设备倒计时/周期任务与空调窗帘状态恢复。
- **不迁移**旧 `timer_backend` 的 JSON 历史数据（其最后记录停在 2026-03，且集成长期处于停用状态）。

### 六、修复：空调定时被降级为关机 + 线程安全告警

**问题 1：空调定时「执行不成功」**

- 现象：事件 `action_type: cool` + 顶层 `climate_mode: cool` / `temperature: 23`，实际执行的是
  `climate.turn_off`（任务 `action.description = "Turn off AC"`），设备本就 off 故状态无变化。
- 根因（两条叠加）：
  1. `generate_climate_action` 只认 `turn_off / set_temperature / set_mode / restore_previous / auto`，
     **不认识模式名 `cool`** → 走到最后的兜底分支退化为关机；
  2. `climate_mode` / `temperature` 位于事件**顶层**，而动作生成只读 `action_data` → 参数被整体丢弃。
- 修复：
  - 新增 `_normalize_action_data()`：把顶层参数（temperature / hvac_mode / mode / fan_mode /
    swing_mode / preset_mode / position / tilt_position / brightness / percentage / humidity …）并入
    `action_data`；兼容前端历史字段 `climate_mode` → `hvac_mode` / `mode`；temperature 归一为 float。
  - `generate_climate_action` 支持三类写法：① 原语义动作；② push_control 目录命名（`set_hvac_mode`）；
    ③ 直接给 HA 模式名（cool / heat / dry / fan_only / auto / heat_cool）——带温度时用
    `climate.set_temperature`（同时传 `hvac_mode`），只给模式时用 `climate.set_hvac_mode`。
  - 新增 `turn_on` / `set_fan_mode` / `set_swing_mode` / `set_preset_mode` 动作；
    设温度场景下的风速/摆风/预设走 `extra_calls`（HA 的 `climate.set_temperature` 不接受这些参数），
    由新增的 `_run_extra_calls()` 在主调用成功后依次下发。
  - `create_timer` / `create_climate_timer` / `create_cover_timer` / `create_schedule` / `update_task`
    统一走归一化，并把 `action_type` / `action_data` 落盘（供后续修改与状态还原）。
  - 未知动作名不再静默退化：先查动作目录，未命中记 warning 再明确退化为关机。

**问题 2：复用 push_control 的动作目录**

- `ACTION_CATALOG`（29 域 / 97 动作）现在被定时精灵复用为「动作名 + 参数名 + 服务映射」的**单一来源**：
  新增 `_generate_action_from_catalog()`，`generate_action` 与 `generate_climate_action` 在语义动作
  未命中时按目录生成调用。定时任务的 `action_type` 可直接写目录里的动作名（参数同名），例如
  `{"entity_id": "light.x", "action_type": "turn_on", "brightness": 50}`。
- 边界：push_control 的执行链（token 绑定单实体 → 立即 `hass.services.async_call`）**未复用**——
  定时精灵需要「延迟到点执行 + 状态恢复 + 附加调用」，两者执行模型不同，硬接需改造其安全模型。

**问题 3：线程安全告警（`async_create_task` from a thread other than the event loop）**

- 现象：`sensor.py` 信号回调报 `RuntimeError`，伴随
  `coroutine 'TimerElvesSensor._async_refresh' was never awaited`，日志线程名为 `SyncWorker_*`。
- 根因：`async_dispatcher_send` 与 `hass.bus.fire` 都**不切线程**——在哪个线程派发，回调就在哪个线程执行。
  引擎原先混用 `asyncio.run_coroutine_threadsafe(coro, hass.loop)` 与同步 `bus.fire`，一旦回调落在
  线程池，dispatcher 就在线程池同步调用订阅者 → 传感器实体里的 `async_create_task` 触发 HA 检测。
- 修复：
  - 定时执行入口（`execute_timer` / `execute_climate_timer` / `execute_cover_timer` /
    `_pre_capture_state` / `execute_recurring_schedule`）统一改用 HA 官方线程安全入口 `hass.add_job()`。
  - 新增 `_in_event_loop()`：`_update_sensor()` 的信号派发与 `_fire_event()` 在非 loop 线程时
    经 `loop.call_soon_threadsafe` 切回事件循环（总线事件同时由同步 `bus.fire` 改为 `bus.async_fire`）。
  - `sensor.py` 的信号回调改为 `hass.add_job(self._async_refresh, payload=payload)`。

### 七、db_viewer：「API 工具 → API 地址生成器」新增「⏱ 定时精灵」查询类型组

- 查询类型下拉新增静态组 **「⏱ 定时精灵」**，7 个条目：
  任务概览 / 统计摘要 / 任务列表 / 历史记录（GET）+ 创建任务 / 修改任务 / 取消任务（POST）。
- 参数区按类型动态渲染（`renderTimerParams` + `_timerFieldSpecs`）：
  - 读接口生成 `?key=&task_id=&entity_id=&status=&limit=&offset=&start=&end=&with_data=` 等 query；
  - 写接口只生成带 key 的 **POST 地址**，并在提示区给出 **curl + JSON body 示例**
    （`_timerRequestBody` 负责组装：temperature 数值化、weekdays / month_days 数组化、
    cancel 的 task_id / schedule_id / entity_id 择一）。
- 未沿用 media 那套 `_method=&_body=` 的 query 约定——定时精灵后端只解析 JSON body，
  写操作照实给出 POST 形式，避免生成"看似能 GET 调用"的误导地址；
  取消任务额外附 DELETE 等价形式（`DELETE /timer?id=<任务ID>`）。

### 八、修复：空调「设温度」模式未生效 + 信号回调 TypeError

**问题 1：设温度无效（空调"还是无法执行"）**

- 现象：日志里动作已正确生成为 `climate.set_temperature{temperature:23.0, hvac_mode:'cool'}`，
  但任务 `after_entity_state` 仍是 `off`，设备没动。
- 根因：本集成自带的虚拟空调 `virtual_devices.py:279 VirtualClimate.async_set_temperature()`
  只取 `temperature`、**忽略 `hvac_mode`**（HA 会把 hvac_mode 一并放进 kwargs 传给实体）。
  结果是只改了目标温度、运行模式仍为 off。**前端与动作生成都没有问题**。
- 修复（全在后端，`www/timer-control-card.js` 未改）：
  - `_climate_set_temperature()` 在带模式时生成 `pre_calls = [climate.set_hvac_mode]`，
    执行时**先切模式（顺带开机）再设温度**，兼容忽略 hvac_mode 的实体；
    对正常支持的实体该调用幂等，无副作用。
  - 执行链支持 `pre_calls` / `extra_calls` 两段（`_run_extra_calls(action, key)`），
    一次性任务、周期任务、窗帘执行三处统一。
  - 主调用与附加调用统一 `blocking=True`：服务参数非法 / 服务不存在会真实抛出并记入任务失败，
    不再出现「`execution_result: success` 但设备没动」的假成功。
  - `restore_previous`（空调/窗帘状态还原）同样改为阻塞执行，顺序调整为「先模式后温度」。

**问题 2：`TypeError: add_job() got an unexpected keyword argument 'payload'`**

- 根因：上一轮把传感器信号回调改成 `hass.add_job(self._async_refresh, payload=payload)`，
  而 HA 的 `HomeAssistant.add_job(target, *args)` **只接受位置参数**。
- 修复：新增 `TimerElvesSensor._async_apply_payload(payload)` 作为信号回调入口，
  改为 `hass.add_job(self._async_apply_payload, payload)`。

### 九、新增两个查询接口：多实体定时任务 / 指定月有数据的日期

均挂在 `/api/ha_data_store/timer` 之下，读接口（只需「API 访问」开关 + Key）：

| 接口 | 参数 | 返回 |
|---|---|---|
| `GET /timer/entity_tasks` | `entities=a,b`（多实体，逗号/中文逗号/分号/空格分隔，兼容 `entity_id`）、`include_history=1`、`active=1`、`is_recurring=1\|0`、`limit=` | `entities` / `total` / `count` / `tasks[]` / `by_entity[]`（每实体：`count`、`active`、`active_timers`、`active_schedules`、`tasks`） |
| `GET /timer/month_dates` | `month=YYYY-MM`（必填）、`entities=a,b`（可选）、`basis=created\|executed` | `month` / `basis` / `count`（有数据的日期数）/ `dates[]`（每日期：`count` + 各实体条数）/ `by_entity[]`（每实体：`count` + `days[]`） |

实现要点：

- `TimerElvesCoordinator.get_entity_tasks()`：默认只返回活跃任务，`include_history=1` 纳入历史；
  按实体分组并给出活跃定时器 / 周期任务计数，便于前端按设备展示。
- `TimerElvesCoordinator.get_month_dates()`：SQL 直接用 `timer_tasks` 的冗余查询列
  （`substr(created_at,1,7)` 滤月、`substr(...,1,10)` 分组），支持 `basis=executed`
  按实际执行时间统计（只计已执行，可跨日）；多实体用 `IN (...)` 过滤。
- View 新增 `_parse_entities()`：统一解析多实体参数（逗号 / 中文逗号 / 分号 / 竖线 / 空格，自动去重）。
- db_viewer「⏱ 定时精灵」组同步新增两项，参数区与地址生成已适配。

注：这两个接口在 Python 层实现（需重启 HA 生效），不属于「接口管理 ext」的免重启定义类接口。

**修复（同日）**：`entity_tasks` 的 `active=1` 未生效。

- 原因：过滤条件写成 `if not is_active and not (include_history or active_only): continue`，
  把 `active_only` 放在了「或」里取反——只给 `active=1`（未给 `include_history`）时条件恒为假，
  历史任务被放行，于是返回了完成/失败的任务。
- 现改为 `if not is_active and (active_only or not include_history): continue`，语义：
  `active=1` 强制只看活跃；`include_history=1` 才纳入历史；两者同时给出时以 `active` 为准。
- 已补 10 项参数组合回归自测（默认 / `active` / `include_history` / 两者叠加 / 多实体 /
  `is_recurring` / `limit`），全部通过——此前只测了单参数，组合未覆盖是该 bug 漏出的原因。

### 十、新增接口：指定实体 / 多实体在某天的定时任务

`GET /api/ha_data_store/timer/entity_daily`

| 参数 | 说明 |
|---|---|
| `date` | **必填**，`YYYY-MM-DD` |
| `entities` | 可选，多实体（逗号/中文逗号/分号/空格分隔，兼容 `entity_id`） |
| `basis` | `created`（任务创建日期，默认）/ `executed`（实际执行日期） |
| `status` | 可选：`active` / `completed` / `failed` / `cancelled` |
| `limit` | 默认 500，最大 2000 |

返回 `date` / `basis` / `status` / `entities` / `count` / `tasks[]`（解析后的完整任务数据）/
`by_entity[]`（每实体：`count`、`active`、`active_timers`、`active_schedules`、`tasks`）。

实现要点：

- 数据 = **SQLite 历史 ∪ 内存当前任务**：DB 侧覆盖已从内存裁剪掉的旧记录；
  内存侧补上刚创建、尚未落盘（`save_tasks` 有 5 秒节流）的任务，保证「今天的任务」不漏。
- 内存中仍存在的 `task_id` 会跳过 DB 结果，避免同一任务以「DB 旧状态 + 内存新状态」重复出现
  （例如刚取消的任务不会同时出现在 `status=cancelled` 与 `completed` 里）。
- db_viewer「⏱ 定时精灵」组同步新增该项（组内现为 10 项）。

### 十一、db_viewer「系统监控」新增「⏱ 定时精灵」概览卡片与详情区块

- 概览卡片区（`#monitorSummary`）新增手工卡片 `timerCard`（`data-target="timer"`）：
  - `.num` = 活跃任务数（一次性定时器 + 周期任务）
  - 第三行 = `活跃 x（定时 a · 周期 b）· 今日 c · 成功 x / 失败 y`
- 详情区块（`#monitorContent`）新增「⏱ 定时精灵」section（默认折叠，点卡片或标题展开）：
  - 上半：**活跃任务表** —— 实体 / 类型（定时·周期）/ 到点动作 / 时长 / 结束或下次执行 / 状态
  - 下半：**最近历史表** —— 最近 20 条（实体 / 类型 / 动作 / 结果徽标 / 执行时间），并标注总条数
- 数据来源：`GET /api/ha_data_store/timer`（概览 + `timers`/`schedules`）与
  `GET /api/ha_data_store/timer/history?limit=20`；两者只受「API 访问」主开关约束，
  故沿用监控页的裸 fetch 惯例、不拼 key。
- 复用既有联动机制：在 `MONITOR_SEC_LABELS` 登记 `timer` 后，
  卡片点击 → `expandMonitorSection('timer')` 展开定位；`syncSummarySub('timer')` 把区块标题
  右侧统计克隆到卡片第三行；折叠态与卡片高亮由既有函数自动处理，无其它联动代码改动。
- 顺带纠正一处认知：`xiaoaiCard / printerCard / recentCard / metricsCard` 并非手工卡片，
  而是 `makeCard()` 生成（`.num` 取自 `types[key].count`），本页实际只有 7 个手工卡片
  需要各自的 `loadMonitorXxx()` 回填。

### 十二、定时精灵写入本地日志（db_viewer「日志查看」页可见）

此前 `timer_elves` 只走标准 `_LOGGER`（HA 日志），而「日志查看」页读的是集成目录下
`logs/YYYY-MM-DD.log`（由 `ha_data_store_local` 这个 logger 写入），所以页面上看不到定时器记录。

- 在事件总出口 `_fire_event()` 中新增 `_log_event_locally()`，把事件转成中文日志行写入本地日志：

| 事件 | 级别 | 日志示例 |
|---|---|---|
| `timer_created` | INFO | `[定时精灵] 创建定时器 实体=测试 时长=00:30:00 到点动作=Turn off 结束时间=…` |
| `schedule_created` | INFO | `[定时精灵] 创建周期任务 实体=客厅插座 重复=daily 时刻=08:00:00 到点动作=关闭 下次执行=…` |
| `timer_completed` | INFO / WARNING | `[定时精灵] 执行成功 实体=… 到点动作=… 状态 on→off` / `执行失败 …` |
| `schedule_executed` | INFO / WARNING | `[定时精灵] 周期任务执行成功 实体=… 重复=weekly 状态 off→on` |
| `timer_cancelled` / `schedule_cancelled` | INFO | `[定时精灵] 已取消定时器 实体=…` |
| `error` | WARNING | `[定时精灵] 操作失败 环节=create_timer 错误=实体不存在: climate.bad` |

- 不走事件总线的三个场景单独补日志：**引擎启动摘要**（时区 / 任务总数 / 活跃定时器 / 活跃周期，
  便于重启后确认恢复情况）、**修改任务**、**清空历史**。
- 刻意不记录 `timers_list` / `schedules_list`（列表推送，前端每次刷新都会触发）与
  `history_result`（查询结果），避免噪声。
- 日志写入包在 `try/except` 中，失败绝不影响定时逻辑；写入走 `logger.py` 的线程安全实现。
- 「日志查看」页支持关键字过滤与级别着色，搜索 **定时精灵** 即可筛出全部记录。

### 十三、定时精灵缺陷修复（安全 / 数据增长 / 契约 / 健壮性）— 全部接口强制 API Key

#### 13.1 安全：所有定时接口强制校验 API Key

- `GET/POST/DELETE /api/ha_data_store/timer*` 原先只判「API 访问」+「数据库修改」两个**默认开启**的开关、
  **不校验 Key**，等于内网任意设备都能创建定时任务从而间接控制设备。
- 现统一改用 `_check_api_enabled(request)`（含 Key 校验，与项目其它接口一致）；
  写接口在其之上再要求「数据库修改」开关。Key 用 `?key=` 或 `Authorization: Bearer` 传入。
- db_viewer 监控页 `loadMonitorTimer()` 同步适配：用 `getPageKey()`（URL 的 key 或后端注入的首枚 key）
  拼接 `?key=`，页面无 key 时给出明确提示而非静默失败。

#### 13.2 功能：恢复卡片「空调定时」动作

- 卡片对空调一次性定时发送的是 `action_config`（`{set_mode:{…hvac_mode}, set_temperature:{…temperature}}`）
  且 `action_type` 固定为 `auto`；后端此前**未解析 action_config**，用户选的「制冷 26℃」被丢弃，
  退化成关机或静默无动作。
- 新增 `_parse_action_config()` / `_resolve_action_type()`：动作名优先级 =
  显式且非 auto 的 action_type > action_config 推导 > 模式名 > 默认；
  参数从 action_config 各动作的 `data` 并入 action_data。
- `create_timer` / `create_climate_timer` / `create_cover_timer` / `create_schedule` / `update_task`
  全部改用统一解析；**前端一行未改**。

#### 13.3 数据：历史不再无限增长

- `timer_tasks` 原先只有 upsert、**全库无一句 DELETE**，历史裁剪只删内存 →
  表与内存长期膨胀，且重启会把整表读回内存让体积"反弹"。
- 新增 `_prune_memory()` / `_delete_tasks_from_db()` / `_delete_all_history_in_db()` / `prune_history()`：
  - `_add_history_record` 裁剪内存时**同步删除库中对应行**；
  - `prune_history()` 每小时随兜底任务执行：内存超上限部分删库 + 库中非活跃且早于
    `TIMER_HISTORY_RETENTION_DAYS`（默认 30 天）的行清理；
  - `restore_tasks` 恢复完成后立即裁剪并按需删库；
  - `_clear_all_history` 同时清库（`DELETE WHERE status != 'active'`）。

#### 13.4 性能：传感器与信号负载瘦身

- 原先 `all_task_list` 把每条任务的**全字段**（含 action / previous_state / action_data / restore_data）
  塞进状态属性，100 条约 50-100KB，远超 HA recorder 的 **16384 字节**上限（该实体不会被记历史）。
- 新增 `_build_task_brief()`：只保留前端渲染必需的 15 个字段 + `id`，历史最多
  `TIMER_BRIEF_LIMIT`（20）条；实测 20 条序列化 **9320 B**。
- 完整任务数据由 API 提供（`/timer/tasks`、`/timer/history`、`/timer/entity_tasks` 等）。

#### 13.5 正确性：同实体周期任务不再被静默取消

- `entity_timers` 是单槽且只登记一次性定时器，但 `_cleanup_entity_timers` / `cancel_entity_timer`
  会把该实体**所有活跃任务（含周期）**标记 cancelled，且不注销周期句柄、不发事件。
- 现：清理逻辑只处理一次性任务；`cancel_entity_timer` 按类型分派
  （一次性 → `cancel_timer`，周期 → `cancel_schedule`，两者都会注销句柄并发事件）。

#### 13.6 其他修复

| 项 | 说明 |
|---|---|
| 事件 `action` 被覆盖 | `_fire_event` 的 `**data` 会覆盖主 action（`update_task`/`get_history` 的 error 事件变成 action=update_task）；现把内层 action 改名为 `source_action` 保留 |
| 过期任务句柄泄漏 | `send_all_timers` 过期分支只 `del self.timers[tid]`，底层定时器仍在 → 到点可能重复执行；现弹出并调用句柄 |
| `restore_previous` 静默无效 | 快照缺失时一个服务都不发却记 success；现改为 `noop`（执行器判为失败），并支持传入**任务内快照**（重启后仍可还原），空调/窗帘的周期任务与 `update_task` 均已接入 |
| 恢复过程无容错 | 单条坏数据 KeyError 会让整个定时精灵模块启动失败；现逐条 try 跳过并记 warning |
| 迟到任务无策略 | 新增 `LATE_EXECUTE_MAX_SECONDS`（300s）：超过则标记 `expired` 不补执行，避免重启瞬间集中操作设备 |
| 窗帘位置 0 误判 | `restore_previous` 用 `pos == 0` 判断"未记录"，而位置 0 是合法值；改用 `is None` |

> 评估后**保持原样**的一项：`save_tasks`(5s) 与 `send_all_timers`(10s) 两套节流冗余但非缺陷，
> 合并会改变落盘频率、收益低，暂不动。

#### 13.7 回归自测

26 项断言全部通过（临时脚本 + 桩模块 + 临时 SQLite，离线可跑）：

- 逻辑：`action_config` 4 种形态解析、动作名优先级、摘要体积与必需字段、事件 action 不被覆盖、取消分派；
- DB：内存裁剪 100 条上限、裁剪同步删库、30 天保留清理、清空历史；
- 恢复：坏数据跳过、未到点重排、迟到 10s 补执行、迟到 1h 标记 expired、恢复后裁剪；
- db_viewer：内联脚本 0 语法错误、监控调用已带 key、无 key 有提示。

### 六、动作生成：内置语义动作 + 复用 push_control 动作目录

- **来源说明**：定时任务执行的动作由 `timer_elves` 内置逻辑生成（硬编码 `climate.turn_off` /
  `climate.set_temperature` / `climate.set_hvac_mode` 等），**并非**从 `push_control.ACTION_CATALOG`
  读取；目录只在「内置未命中的动作名」时兜底（`_generate_action_from_catalog()`，
  调用点见 `generate_action()` / `generate_climate_action()` / `generate_cover_action()`）。
- **命名与参数已对齐**：`set_hvac_mode` / `set_temperature` / `set_fan_mode` / `set_swing_mode` /
  `open_cover` / `close_cover` / `stop_cover` / `set_cover_position` 均可直接作为 `action_type`，
  参数名同目录（`hvac_mode` / `temperature` / `fan_mode` / `swing_mode` / `position`）。
- 内置另支持**目录没有**的语义动作：模式名直传（cool / heat / dry / fan_only / heat_cool）、
  `restore_previous`（优先任务内快照）、`auto`（开着就关、关着就还原）、`set_preset_mode`、
  风速/摆风随设温下发（`extra_calls`）。
- **修复回归**：`_CLIMATE_MODE_NAMES` 曾包含 `"auto"`，使 `action_type="auto"`（卡片对空调的固定取值）
  被当作「切到 HA auto 模式」，而不是原来的「开着就关 / 关着就还原」。现移除 `"auto"`；
  需要 auto 模式时用 `set_mode` / `set_hvac_mode` + `hvac_mode=auto`。
- **窗帘域补齐**：新增别名（`open_cover`→`open`、`close_cover`→`close`、
  `set_cover_position`→`set_position`）、目录独有的 `stop`（`cover.stop_cover`）动作与末尾目录兜底；
  未知动作改为记 warning 后退化，不再静默。
- 自测 20 项全过：auto 双向语义、模式名、目录命名、extra_calls、未知动作退化、
  窗帘 4 种别名 + 停止 + 还原、light/counter 目录兜底。

## 2026-09-26 — v4.0.0 实体→网络「可控制」+ 整库备份 + API 工具整合

> 本版为当日全部改动的汇总版（原 3.7.0 / 3.8.0 / 3.8.1 / 3.8.2 / 3.8.3 五个版本合并为 4.0.0）。
> 每条小节末尾标注了它原本所属的版本号，便于对照历史讨论。

### 一、实体→网络：从「只读映射」升级为「可控映射」（原 3.7.0）

#### 1.1 背景

「系统配置 → 🌐 实体→网络」原先只能把实体**读**出去（`GET /push_data/{token}` 返回
state/attributes），可操作实体映射出去后无法控制。本版补上**写**通道，并保持
「只读」与「可写」两条链路的凭证、开关、审计完全分离。

#### 1.2 读写分离（安全模型）

| 维度 | 读（原有） | 写（新增） |
|---|---|---|
| 地址 | `GET /api/ha_data_store/push_data/{push_token}` | `POST /api/ha_data_store/push_control/{control_token}` |
| 凭证 | `push_token`（只读凭证） | `control_token`（写凭证，独立生成） |
| HTTP 方法 | 仅 GET | 仅 POST（GET/POST 到读地址均给出明确 403/405 提示） |
| 开关 | `api_enabled`（API 访问） | `api_enabled` **且** `push_control_enabled`（实体网络控制） |
| 目标开关 | `enabled` | `enabled` **且** `control_enabled` |
| 审计 | 无 | `control_logs` 表（保留 30 天） |

新增 HA 开关实体：设备「HA数据统一存储系统」下的开关 **「实体网络控制」**
（`switch.py` `HaDataStorePushControlSwitch`，唯一 id `ha_data_store_push_control_enabled`），
**首次安装默认关闭**，状态跨重启保留。关闭时所有控制请求 403 ——
这是一键掐断所有外部控制的「总闸」。

> 写操作强制 POST 的原因：GET 会被浏览器预取、被代理/日志记录、被爬虫扫到，
> 等于把「开锁」变成一个可被随机触发的动作。

#### 1.3 动作白名单（`push_control.py` 新增）

内置 **29 个域 / 97 个动作** 的目录 `ACTION_CATALOG`，每个动作映射到确定的 HA 服务与参数 schema，
**不接受任意 `domain.service`**：

| 域 | 动作（节选） |
|---|---|
| switch / input_boolean | turn_on / turn_off / toggle |
| light | turn_on(brightness/color_temp_kelvin/rgb_color/effect/transition) / turn_off / toggle |
| climate | set_hvac_mode / set_temperature / set_fan_mode / set_swing_mode / turn_on / turn_off |
| cover | open / close / stop / set_cover_position / set_cover_tilt_position / tilt 系列 |
| fan | turn_on(percentage/preset_mode) / set_percentage / set_preset_mode / oscillate / set_direction |
| humidifier / water_heater / media_player / vacuum / valve / lawn_mower / todo | 各自服务集 |
| number / input_number | set_value |
| select / input_select | select_option |
| text / input_text / counter / timer / button / input_button | 各自服务集 |
| scene / script / automation | turn_on / turn_off / trigger |
| **lock / alarm_control_panel / siren** | 标记 `high_risk`，UI 额外警告 |

**参数 schema 动态展开**：范围与枚举从实体当前属性实时读取 ——
`climate.temperature` 取 `min_temp/max_temp/target_temp_step`，
`hvac_mode` 取 `hvac_modes`，`select.option` 取 `options`，`number.value` 取 `min/max/step`。
动态枚举无值（实体不支持）时该参数直接从能力列表剔除，UI 不会显示不可用项。

**能力过滤**：`cover` 按 `supported_features` 位掩码过滤动作
（无 SET_POSITION 位就不出现 `set_cover_position`）。

只读域（sensor / binary_sensor / camera / weather 等 18 个）显式返回「不支持控制」的原因。

#### 1.4 参数控制两层设计

**第一层 · 调用方可传参**（运行时）：

```
POST /api/ha_data_store/push_control/{control_token}
{"action": "set_temperature", "params": {"temperature": 26}, "wait_state": true}
```

**第二层 · 配置端锁定**（设计时，`param_constraints` 字段）：

- `lock=value`：固定值，调用方传参**被忽略**（永远执行指定值）
- `lock=range`：限幅，超界自动裁剪（例如窗帘只允许 0~50，不能全开）

于是外部系统拿到的是「受限能力」而非「实体控制权」，可做到
「一个 token 只能关灯关空调」「一个 token 只能把空调设成 26 度」这类场景化授权。

#### 1.5 授权清单（`allowed_actions`）

- `["*"]` / 字段缺失 → 目录内全部动作
- `[]` → **不允许任何动作**（控制实际不可用，最安全；UI 会黄字提示）
- `["turn_on", "turn_off"]` → 仅这些动作，其余返回 403

#### 1.6 raw 逃生口（默认关闭）

针对自定义集成只有专有服务的场景，可对单个目标开启 `allow_raw_service`：

```
{"service": "climate.set_temperature", "data": {"temperature": 26}}
```

硬约束：**只允许调用实体自身域**（跨域 403）+ 禁止
`shell_command / python_script / hassio / homeassistant / recorder / notify` 等系统域。

#### 1.7 其他防护

- **限流**：`rate_limit_per_min`（默认 60，0=不限），超限 429；内存滑窗 + 定期清理过期桶
- **状态等待**：`wait_state: true` 时轮询最长 3 秒等 `last_updated` 变化，响应里给 `waited`
- **审计**：每次控制（成功/失败）写 `control_logs`，含 target_id / 实体 / 动作 / 服务 /
  实际参数 / 结果 / 错误 / 来源 IP
- **能力发现**：`GET /push_capabilities/{control_token}` 返回该 token 实际被授权的动作与参数
  schema，第三方无需硬编码

#### 1.8 数据库（原 3.7.0）

`push_targets` 新增列：`control_token` / `control_enabled` / `allowed_actions` /
`param_constraints` / `rate_limit_per_min` / `allow_raw_service`。

**放开 `entity_id` 唯一约束**（迁移 `_migrate_push_targets_drop_unique`）：
SQLite 不支持 DROP CONSTRAINT，采用「建新表 → 拷数据 → 换名」重建，唯一性改由
`push_token` / `control_token` 的**部分唯一索引**（`WHERE xxx != ''`，空串不参与）保证。
迁移幂等，已用离线脚本验证：数据保留 / 约束放开 / 字段默认值正确 / 重复执行不丢数据。

放开后同一实体可挂多套配置 —— 例如「A 系统只读」「B 系统只许关」「C 系统全控」三个 token 并存。

新增表 `control_logs`（+ `created_at DESC` 索引）。

### 二、子选项卡内嵌「📖 使用方法」（原 3.7.0）

「🌐 实体→网络」顶部新增可折叠使用说明（**默认折叠**，收起时保留一行摘要）：

| 区块 | 内容 |
|---|---|
| ⚡ 快速上手 | 4 步：开总闸 → 加载实体 → 选动作 → 调用 |
| 一、两类地址与开关 | 读/控制/能力三个地址 + 各自放行条件 |
| 二、调用示例 | 6 段可直接抄的 curl |
| 三、两种请求体 | 动作模式 vs raw 模式 |
| 四、参数两层控制 | 固定值 / 限幅 / 可传入对比 |
| 五、动作清单语义 | 全选 / 部分 / 一个不勾 |
| 六、返回与错误码 | 成功字段表 + 400/403/404/405/429/502 含义表 |
| 七、动作目录 | 「📋 加载动作目录」按钮，从后端实时拉取域与动作一览 |
| 八、安全与注意 | 高风险域、总闸、只认 POST 的原因、审计、限流、重启生效 |

### 三、整库备份（原 3.8.0 + 3.8.2）

#### 3.1 背景

集成此前没有库级备份 —— 整个数据库是单个 SQLite 文件，跑一年就是全部家当
（监控配置、历史记录、用电计量、操作记录、接口定义、实体→网络 token 都在里面）。
已有的导出/导入只覆盖「虚拟设备」和「辅助元素」两类实体，不是整库。

#### 3.2 备份方式

优先 `VACUUM INTO`（产物紧凑、无空闲页），失败回退 SQLite 官方在线备份 API
`Connection.backup()`。两者都能在数据库被 HA 正常读写时取得**一致快照**：
不需停机、不会丢掉 `-wal` 里尚未合并的事务、不引入第三方依赖。

#### 3.3 自动备份计划

| 项 | 取值 |
|---|---|
| 周期 | 关闭 / 每小时 / 每天 / 每周 |
| 执行时刻 | 小时 0-23（每天、每周用） |
| 星期 | 周一~周日（每周用） |
| 保留份数 | 1-100，默认 10（**只作用于自动备份**） |
| 目录 | 默认 `storage/ha_data_store_backups`，可填挂载后的绝对路径 |

调度实现：`__init__` 注册 **10 分钟**的 tick，每次按「<= now 的最近一个计划时刻」
与 `backup_last_at` 比较决定是否到期。好处：运行期改计划**不需要重新注册定时器**；
HA 在计划时刻没运行，启动后会自动补上当天那次（迟到补偿）；
保存设置时会**现场验证目录可写**，避免保存后才在半夜备份时失败。

#### 3.4 恢复：排队 + 启动时原子应用（本版最关键的设计）

运行期直接覆盖正在被写入的库，存在「半写状态」和「在途写入打进新库」两类风险。
因此恢复**不在运行期替换数据库**：

1. 「恢复」→ 服务端校验备份文件（`PRAGMA quick_check` + 必需表存在性）→ 复制为
   `ha_data_store.db.pending_restore` 并写下标记 `ha_data_store.db.restore_requested`
2. 用户**手动重启 HA**
3. 启动时、在任何连接打开数据库**之前**（`_init_database` 之前）：
   先给现有库自动做一份「恢复前快照」放进备份目录 → 清理旧库的
   `-wal` / `-shm` / `-journal`（避免旧日志被回放到新库）→ `os.replace()` 原子替换
4. 万一备份文件校验失败：**直接取消恢复、保留现有库**，坏文件留存为 `*.invalid` 供排查

即：恢复一定发生在「无人使用数据库」的时刻，且任何一步失败都能靠快照退回；
恢复前快照会出现在列表里，随时可以再恢复回去。

#### 3.5 网络共享（SMB / NFS）：拦截协议地址 + 本地暂存后搬移（原 3.8.2）

**不能直接填 `smb://host/share` 这类协议地址** —— 程序只能读写已挂载的目录。
更糟的是直接填会**静默出错**：

| 输入 | 实际解析结果 | 后果 |
|---|---|---|
| `smb://192.168.1.102/media` | Linux：`/config/smb:/192.168.1.102/media` | 在 HA 配置目录里造出垃圾目录，写盘测试还**通过**，界面显示"备份成功"，实际根本没到 NAS |
| `smb:\\192.168.1.102\media` | 同上 | 同上 |

因此新增 `looks_like_url()` 识别协议地址，命中即**显式报错**并按协议给出挂载指引：

- 命中：`smb://`、`smb:\\`、`nfs://`、`ftp://`、`webdav://` … 冒号后紧跟 `/` 或 `\` 的多字母协议名
- 放行：`/media/ha_backups`、相对路径、`Z:\backups`（盘符）、
  `\\192.168.1.102\media` 与 `//192.168.1.102/media`（Windows UNC，合法本机路径）、
  `mnt:backup/x`（冒号后无分隔符，普通目录名，不误判）

正确做法是先挂载再填挂载后的路径：

| 环境 | 挂载方式 | 填写的路径示例 |
|---|---|---|
| HA OS / Supervised | 设置 → 系统 → 存储 → 添加网络存储（用途选 media / share） | `/media/ha_backups`、`/share/backups` |
| HA Container / Core | 宿主机 `mount -t cifs //192.168.1.102/media /mnt/ha_backups -o username=用户,password=密码,uid=1000` | `/mnt/ha_backups` |

面向共享的写入额外加固：**先在本地生成并校验，再搬到共享**。
原因是把备份直接写到网络盘有两个问题：SQLite 会在目标旁边建 journal 并依赖文件锁
（官方明确不建议把数据库文件放网络盘）；网络中断会在共享上留下**半个损坏文件**，
而它会被当成"有效备份"列出来。

- 目标在本地时 `shutil.move` 等价于 `rename`，**零额外开销**
- 目标是挂载点时自动走 `copy2` 回退
- 配合 `finally` 清理 + 6 小时年龄阈值的残留扫描（`_sweep_stale_staging()`），
  进程被强杀留下的 `.ha_data_store_*.db.tmp` 会被清掉，且**绝不会误伤进行中的备份**
- 状态卡片对「目录在配置目录之外」给出网络挂载警示（挂载晚于集成启动会导致计划备份失败）

#### 3.6 其他

- **完整性检查**：`PRAGMA integrity_check`（只读 URI，绝不修改数据）
- **下载备份**：`web.FileResponse` 流式下载，不占内存
- **删除备份**：严格文件名校验（拒绝 `../`、子目录、非 `.db`、pending 标记文件）
- **设置存放**：复用 `api_settings` 键值表（`backup_*` 键），**不新增表** ——
  备份模块本身要能在数据库损坏时继续工作；表缺失时自动补建
- **不提供上传接口**：HA 的 aiohttp 默认请求体上限会让大库上传 413。
  把外部拿到的备份直接拷进备份目录、刷新列表即可恢复

### 四、「💾 数据备份」提升为顶层选项卡（原 3.8.0 内嵌 → 独立）

原本作为「系统配置」的子选项卡，后提升为顶层主选项卡（位置在「数据库浏览」与「日志查看」之间），
理由：它是运维操作、与数据库本体直接相关，放顶层更容易找到。

> 技术必要性：`.tab-panel { display: none }` 是 `display` 级隐藏，
> 面板若留在 `#tab-manage` 内，即使自身 `.active` 也不会显示，因此必须整体移出。

界面内容：待恢复横幅 / 状态卡片（库大小含 WAL、表数、journal 模式、备份目录、份数占用、
最近自动备份）/ 立即备份·完整性检查·清理旧备份 / 计划设置（周期联动字段）/
备份列表（下载·恢复·删除）/ 📖 关于备份（默认折叠）。

### 五、API 工具：新增 🌐 实体→网络 查询类型分组（原 3.8.1）

`API工具 → 🔗 API 地址生成器` 的「查询类型」下拉新增独立分组
`<optgroup label="🌐 实体→网络（token 鉴权）" id="apiPushGroup">`，
内容由 `pushLoadIntoApi()` 从 `/push_targets` **动态生成**：

- 每个已配置目标生成 1~2 个选项：`📥 读数据 · sensor.x（名称）`、`🎛 控制 · climate.y`
  （没有 `control_token` 就不出现控制项；未开启控制的会标注）
- 值为 `push:data:{id}` / `push:control:{id}`，与既有 `ext:{name}`、`custom:{path}` 前缀风格一致
- 刷新时**保留当前选中项**（配置被删除/停用则回落默认）

选中后生成地址，**刻意不附加 `?key=`** —— token 本身就是密钥、URL 自鉴权。

URL 下方新增「🌐 实体→网络 调用方式」区块：读给出 `curl "URL"`；
控制给出多行 `curl -X POST`（body 里的 `action` 取该目标**实际授权**的第一个动作），
列出当前授权动作清单，未开启控制时黄字提示会 404，并给出 `push_capabilities/{token}` 能力清单地址。

顺带收敛重复：`push` 与 `ext` 都不需要参数输入行，原 `ext` 分支内联写了 22 行隐藏代码，
抽出 `apiHideAllParamRows()` 供两者共用（隐藏清单与原来**逐项一致**，恢复行为不变）。

### 六、修复清单

| 版本 | 问题 | 处理 |
|---|---|---|
| 3.8.3 | 「🌐 实体→网络」分组**没有任何子项**。根因：`pushLoadIntoApi()` 只挂在 `switchApiSubTab('gen')` 上，而 **gen 是默认激活的子选项卡**，页面打开时不会触发它，函数从未被调用 | 在 `DOMContentLoaded` 补上调用（与 `extLoadIntoApi()` 并列，加注释说明原因）；再在 `switchTab('api')` 里刷新一次，新增目标后无需重载页面 |
| 3.8.2 | 备份同秒重复触发时追加的 `_2` 序号后缀不被文件名正则识别 → 这些备份**存在但列表里完全不可见**（无法下载/删除/清理） | 正则补 `(?:_(?P<seq>\d+))?` |
| 3.8.2 | 启动早期 `load_settings_sync()` 对不存在的库调用 `sqlite3.connect()`，会**凭空创建空库** | 先判 `os.path.isfile` 再连接；恢复流程中原库不存在时明确跳过快照 |
| 3.8.2 | 协议地址拦截的正则最初写作 `^scheme://?`，只覆盖 `smb://` 与 `smb:/`，**用户实际输入的 `smb:\\`（冒号后反斜杠）匹配不到**，仍会静默建垃圾目录 | 改为 `^scheme:[\\/]`，并补上「含冒号的普通目录名不得误判」的反向用例 |

### 七、涉及文件

| 文件 | 说明 |
|---|---|
| `push_control.py` | **新增** 约 1050 行：动作目录 + 参数校验/锁定 + 限流 + 审计 + 执行引擎 |
| `backup.py` | **新增** 约 800 行：备份/校验/列表/保留/到期判断/排队恢复/启动应用 + 2 个 API 视图 + `register_api_views` |
| `const.py` | 新增 `TABLE_CONTROL_LOGS` / `PUSH_CONTROL_SWITCH_KEY` / `PUSH_CONTROL_DEFAULT_RATE_LIMIT`；版本 3.6.11 → 4.0.0 |
| `__init__.py` | `control_logs` 建表、`push_targets` 新列迁移与 `_migrate_push_targets_drop_unique`、部分唯一索引、控制总开关默认值、注册 8 个新视图、启动前应用待恢复、10 分钟备份 tick 与 unload 清理、启动打印控制动作目录规模 |
| `http_api.py` | `PushTargetsView` 重写（id 更新 / 多 token / JSON 字段解析）、`PushControlView`、`PushCapabilitiesView`、`PushEntityCapabilitiesView`、`PushControlLogsView`、`_check_push_control_enabled`、monitor 输出控制字段 |
| `switch.py` | 新增 `HaDataStorePushControlSwitch`（实体网络控制，默认关闭） |
| `db_viewer.html` | 「🌐 实体→网络」4 步向导 + 动作/参数锁定 UI + 控制地址列 + 测试面板 + 控制日志面板 + 内嵌使用说明；新增顶层 `💾 数据备份` 选项卡；API 工具新增 push 查询类型分组与调用方式区块；修复子选项卡按钮/接线丢失 |
| `manifest.json` | 版本 4.0.0 |
| `README.md` | 结构整理（详见第十节）+ 功能总览补「实体→网络（读/控制）」「整库备份」「小爱对话」并修正过时的「推送目标」描述 + API 表、数据表、备份与网络共享说明 |

### 八、验证

三套离线自检全部通过，合计 **174 项**：

| 自检 | 项数 | 覆盖 |
|---|---|---|
| 控制引擎（stub HA） | 47 | 目录规模 / 能力发现与清单过滤 / 动态范围与枚举 / cover 位掩码 / 未知参数 / 缺必填 / 枚举越界 / 越界数值 / 授权清单（含 `*` 与空清单 403）/ 参数锁定（固定值覆盖传参、限幅双向裁剪）/ raw 跨域与禁用域 403 / 限流 429 |
| 备份模块（stub HA/aiohttp/http_api） | 91 | 文件名与路径安全 / 设置读写 / **启动早期不得凭空造库** / 校验（非数据库·过小·缺表·不存在）/ 保留策略（只清自动，手动与快照永不删）/ 到期判断 12 种组合 / 计划全链路 / 恢复（排队·回滚·陈旧 WAL 清理·快照·无残留）/ **原库不存在时的恢复** / **损坏备份不破坏现有库** / 取消排队 / 删除 |
| 备份加固（含 `EXDEV` 打桩） | 36 | 协议地址识别（`smb://`·`smb:\\`·大写·`nfs://`·`ftp://`·`webdav://`）/ 普通路径放行（含两种 UNC 与含冒号目录名）/ 保存拦截且**不建垃圾目录** / 三段式备份无 `.tmp` 残留 / 跨文件系统 `copy2` 回退 / 暂存清理不误伤 / 恢复流程回归 |

`db_viewer.html` 静态自检全通过：

```
OK   JS 语法 (505235 chars)
OK   内联事件引用 244 个函数均有定义
OK   HTML 标签全部配平
OK   JS 引用的 507 个元素 id 全部存在
OK   选项卡一致：顶层 8↔8，子 18↔18
OK   3 个分组填充函数均已接线: loadCustomRoutesIntoApi, pushLoadIntoApi, extLoadIntoApi
OK   动态分组容器齐全: apiCustomRoutesGroup, apiExtGroup, apiPushGroup
```

检查器随问题演进逐步加固，目前覆盖：JS 语法 / 内联事件函数定义 / HTML 标签配平 /
JS 引用的元素 id 存在 / 顶层与子选项卡入口双向一致 / **分组填充函数必须被真正调用**。
最后一条正是为「分组存在但永远为空」这类故障加的：此类 bug 语法检查与元素检查都抓不到。

### 九、过程教训

**并行编辑同一文件会互相覆盖。** 本次开发中出现 5 次「编辑报告成功但内容不在文件里」
（漏掉子选项卡按钮、`switchTab` 接线、`switchTab` 勾子、optgroup 容器、`DOMContentLoaded` 调用），
一度误判为编辑器覆盖。真实原因：把同一个文件的多个 `replace_in_file` 放在**同一批并行发出**，
它们各自基于同一份原始内容写入、互相覆盖，**每批的第一个编辑必然丢失**。

正确做法：**同一文件的多处修改必须串行**（一批只改一个文件的一处），或改用单次大范围替换。
另外，凡是「加了一个 UI 入口 / 挂了一个新函数」的改动，都应有一条**能自动验证接线**的检查，
否则"看起来加上了"和"真的生效"之间没有防线。

### 十、README 结构整理

README 累积了多次追加式修改，出现结构性问题，本次一并处理（**只调整位置与编号，不改技术内容**）：

| 问题 | 处理 |
|---|---|
| `### 0.`~`### 10.` 是三级标题，但 `## 11.`~`## 16.` 却是**二级** —— 同一套编号跨了两个层级 | 11~16 统一降为 `###`，功能详解现为连续的 `### 0.`~`### 20.` |
| **`16` 重复**（`## 16. 接口管理` 与 `## 16. 家庭洞察`） | 「家庭洞察」改为 17、「通用指标引擎」改为 18 |
| 「家庭洞察」「通用指标引擎」被丢在「技术栈」之后、更新日志之前，离同类章节约 1000 行 | 移回「功能详解」，成为 17 / 18 |
| `### 整库备份`、`### 实体→网络的地址生成` 被塞进 **「数据库表结构」**（它们不是表结构） | 移入「功能详解」成为 19 / 20，并补 `#### 20.1` 层级 |
| 原二级章节的子标题降级后与编号章节**同级** | 相关 10 个子标题整体再降一级（`###`→`####`、`####`→`#####`） |
| 目录漏项、锚点不全（缺 接口管理 / 通用指标 / 日志系统 / 技术栈 等） | **按最终标题树自动重建**，38 项锚点全部校验可解析 |
| 「更新日志」是 `docs/CHANGELOG.md` 的重复副本，占 209 行 | 精简为最新一条 + 指向 CHANGELOG 的链接（历史条目已确认全部存在于 CHANGELOG） |
| 开头一段 500+ 字单句，可读性差 | 改为一句提要 + 「环节 / 能力」表 |
| 「功能总览」缺 实体→网络 / 整库备份 / 小爱对话，且有一行 `📤 推送目标` 描述为「推送到外部 HTTP 端点」（与实际**拉取式**读 + POST 控制的语义不符） | 补齐三行、改写该行为准确的「实体→网络（读 / 控制）」 |

结果：1627 行 / 111866 字节 → **1489 行 / 90078 字节**（-19.5%），换行符保持 LF 未变。

校验方式：脚本按**标题定位块**（不依赖行号）做搬迁与重编号，并逐项断言；整理后校验
「二级标题无重复 / 功能详解编号连续 0~20 / 目录锚点全部可解析 / Markdown 结构体检 0 处问题」。
另附一次结构体检，覆盖三类隐性问题：**普通文本行紧跟 `---` 会被渲染成 setext 二级标题**、
标题前缺空行、表格列数不齐（后两类需排除围栏代码块与 `\|` 转义，否则全是误报）。

## 2026-09-19 — v3.6.11 元数据 + 通用指标引擎（`metrics_catalog`）

### 🧠 背景与目标

以往每加一种统计就要在 `QueryView` 里新增一个内建 `type=` 分支（`device_usage_*`、`aggregate_*`…），
接口数量只增不减。本版本引入**元数据驱动**的通用引擎：把「怎么查」从**代码**变成**一条数据**
（指标定义）。**新增 / 修改指标无需重启 HA**（定义存库），只有新增查询 `type` 才需要重启。

### 一、两层元数据

1. **schema 元数据**（`metrics.py`，不落表）：为每张表标注中文名 / 分组 / 时间列 / 实体列 /
   房间列 / 名称列 / 值列 / 时间粒度（`datetime` | `date`）。
   `env_*` 按 `VALID_METRICS` 生成，`attr_*` 按 `attr_type_defs` **动态生成**，未标注的表归入「自定义」。
   → 既是前端下拉的数据源，**也是引擎的列白名单**（安全边界）。
2. **指标定义**（新表 `metrics_catalog`，落库可增删改）：
   `metric_id / name / category / source_table / value_col / value_expr / agg / unit / icon /
    group_by / group_col / filters / enabled / builtin / sort_order / remark`。

### 二、占位符（定义写占位符，编译时按源表解析为真实列名）

`@time` `@entity` `@value` `@room` `@name` `@id`
→ 同一份定义可跨表复用；表结构变化时内置指标自动跟随。

### 三、内置指标（启动时 seed）

在 `_init_database` 中 `INSERT OR IGNORE` 同步，**不覆盖用户改动**，并按实际存在的表裁剪：

| 分类 | 指标 |
|---|---|
| 环境 | 温度/湿度/PM2.5/CO₂/功率 × **均值 / 最高 / 最低**（`env_sensor` 为采样数） |
| 设备 | 开启次数、运行时长合计、单次时长均值、用电合计、**运行中设备数**（`filters: off_time=''`） |
| 用电 | 用电量合计（按天）、日均用电量（按实体） |
| 健康 | 血压高压/低压/体重均值、记录条数（按成员） |
| 操作 | 用户操作次数（按设备）、**活跃用户数**（`count_distinct`，按天） |
| 自动化 | 执行次数、平均耗时 |
| 卡片上报 | 上报卡片数（`count_distinct`） |
| 属性提取 | 每个 `attr_*` 类型的记录数 + 其 REAL 列均值（每类型最多 5 列） |

新建属性类型 / 结构变更后，可点 db_viewer「🔄 同步内置指标」补齐（启动也会自动补）。

### 四、通用查询引擎

`compute_metrics_query_sync()`：读指标定义 → 编译 SQL → 执行。

| 能力 | 取值 |
|---|---|
| 聚合（白名单） | `avg` `sum` `max` `min` `count` `count_distinct` |
| 分组 | `none` `entity` `room` `name` `type` `day` `hour` `month` `year` |
| 时间 | `start/end` > `date` > `month` > `year` > `days`（最近 N 天），作用于源表**时间列** |
| 过滤 | 指标自带 `filters`（JSON 等值）+ 调用方 `filters` 合并；`entities`、`room` |
| 排序 / 分页 | 时间维度默认 `asc`、其余按值 `desc`；`limit/offset` |
| 附带字段 | `entity`/`name`/`type` 分组时顺带输出 `name` / `room` / `icon` |
| 安全 | 表名必须在 schema 白名单、列名必须存在于 `PRAGMA table_info`；标识符统一双引号，值一律 `?` |

`hour` 分组对仅到日期的表（如 `power_energy_daily`）会明确报错。

### 五、对外接口（API Key 鉴权）

| 接口 | 说明 |
|---|---|
| `GET /api/ha_data_store/query?type=metrics_catalog` | 指标目录（可按 `category` / `keyword` 过滤，默认只返回启用项） |
| `GET /api/ha_data_store/query?type=metrics_query&metric_id=xxx` | 执行指定指标 |

`metrics_query` 参数：`metric_id`（必填）、`start/end`、`date`、`month`、`year`、`days`、
`entities`、`room`、`group_by`、`agg`、`order`、`filters`（JSON 字符串）、`limit/offset`、
`detail`（附原始记录）、`sql`（返回生成的 SQL）。

返回：
```json
{"metric_id":"env_temperature_avg","name":"温度均值","unit":"°C","range":"最近 7 天",
 "group_by":"day","agg":"avg","row_count":842,"count":7,
 "summary":{"rows":7,"samples":842,"total_count":842,"value":162.4,"avg":23.2,"min":17,"max":26.4},
 "rows":[{"key":"2026-09-18","value":23.2,"count":120}],
 "series":{"labels":["2026-09-18"],"values":[23.2],"counts":[120]}}
```

### 六、管理接口（db_viewer 会话）

| 接口 | 说明 |
|---|---|
| `GET/POST/DELETE /api/ha_data_store/metrics` | 列表 / 新增修改 / 删除（**内置指标不可删，只可停用**） |
| `GET /api/ha_data_store/metrics_schema` | 表 + 列元数据（前端下拉） |
| `POST /api/ha_data_store/metrics_test` | 试运行（不保存定义，返回 SQL + 结果） |
| `POST /api/ha_data_store/metrics_sync` | 同步内置指标（`reset=true` 覆盖内置参数） |

### 七、db_viewer「📊 指标管理」子页 + 系统监控卡片

系统配置新增子页：指标列表（分类 / 关键字筛选，角标显示指标总数）、新建 / 编辑 / 复制 / 启停 /
删除、「🔄 同步内置指标」与「♻︎ 重置内置」，以及**试运行**（时间模式 + 实体 + 分组/聚合覆盖，
展示生成的 SQL、`summary` 与结果表）。
API 工具新增「📊 通用指标（元数据驱动）」分组（`metrics_catalog` / `metrics_query`；
后者自动加载 `metric_id` 下拉并生成 URL，选中后显示该指标的「源表 / 值 / 默认分组 / 聚合 / 单位」，
并带 🔄 手动刷新；指标管理页的增删改会使其缓存自动失效）。

**系统监控页新增「📊 指标管理」卡片与区块**：
- summary 卡片显示指标总数（点击展开下方对应区块），子选项卡角标同步为指标数；
- 区块标题右侧统计：`启用 N · 停用 M · 内置 B · 自定义 C`；
- 区块内容：**分类分布 chips** + 指标表格（`metric_id / 名称 / 分类 / 源表 / 值列或表达式 /
  聚合 / 默认分组 / 单位 / 启用状态`，内置指标带「内置」标记）；
- 顶部「⚙️ 前往『系统配置 → 📊 指标管理』」按钮（`gotoMetricsManage()`）一键跳转；
- 数据由 `EntityMonitorView` 新增的 `metrics` 节点提供（`total/enabled/disabled/builtin/custom/
  categories/items[前100条]`），并计入 `types.metrics`，统计失败只告警不影响监控页。

### 八、验证

SQLite 临时库跑**真实源码** 15 项：schema 分组、seed 幂等（首次 23 条 / 再次 0 条）、按天 / 小时 /
实体 / 房间 / 月分组、`filters` 判定运行中、`count_distinct`、整体聚合、覆盖 agg + 排序 + limit、
attr 动态指标、`detail`、6 类非法输入被拒（未知指标 / date 表按小时 / 非法 agg / 非法 metric_id /
未知表 / 不存在列）、upsert → 查询 → 删除、试运行不落库、停用指标拒绝执行 —— 全部通过；
db_viewer 内嵌 JS 通过 `node --check`。

**涉及文件**：新增 `metrics.py`；`const.py`（`TABLE_METRICS_CATALOG` + 版本号）、`__init__.py`
（建表 + 启动 seed + 注册 4 个视图）、`http_api.py`（2 个查询 type + 4 个管理视图）、
`db_viewer.html`（指标管理子页 + API 工具项）、`docs/CHANGELOG.md`、`README.md`。

> ⚠️ 新增查询 `type` 需**重启 HA** 一次（触发建表与内置指标 seed）；此后新增 / 修改指标**无需重启**。

## 2026-09-19 — v3.6.10 家庭洞察：统一事件流 `timeline` + 房间占用排行 `room_occupancy`

新增 `insights.py` 模块（纯计算，不新增业务表）与两个查询接口，均遵循「只提供数据、前端负责 UI」，
时间粒度以**天/日期/时间段/月/年**为界（`device_history` 已在午夜自动拆分，**不存在跨天记录**），
**不做分页**（返回 `count` + `truncated`）。

### 🕘 `GET /query?type=timeline`：统一事件流

把 7 类事件合并成一条按时间倒序的时间线，前端一套渲染逻辑即可展示全部来源：

| source | 表 | 时间列 | 事件 |
|---|---|---|---|
| `device` | `device_history` | `on_time` / `off_time` | **A 方案**：一条记录展开为 `on` + `off` 两个事件，运行中（`off_time` 空）只有 `on` |
| `user_action` | `user_actions` | `ts_text` | 「Alix 操作 大灯（toggle）」+ `state_log` |
| `automation` | `automation_logs` | `trigger_time`（回退 `created_at`） | 「自动化 夜间关灯 执行成功 · 0.8s」 |
| `vacuum` | `vacuum_history` | `datetime` | 轨迹点**只在 `state` 变化时**产出事件（避免一天几千条） |
| `xiaoai` | `xiaoai_conversations` | `conv_time` | 「小爱：今天天气…」+ 应答 |
| `health` | `health_records` | `date_time` | 「爸爸 记录（体温）· 体温 36.5」 |
| `printer` | `printer_daily` | `day` | 「打印机 HP 7730 当日作业 16 次」 |

参数：
| 参数 | 说明 |
|---|---|
| `date` / `start`+`end` / `month` / `year` / `today=1` | 时间窗（**默认今日**；优先级 date > start/end > month > year） |
| `sources` | 来源过滤（逗号分隔；空 = 全部） |
| `events` | 仅 `device`：`on` / `off`（空 = 两者） |
| `entities` | 实体过滤（device / user_action / vacuum / xiaoai） |
| `rooms` | 房间过滤（device.room / user_action.room_name） |
| `users` | 用户过滤（device.on_user·off_user / user_action.user_name） |
| `keyword` | 关键词（设备名 / 操作 / 自动化名·描述 / 扫地机 ID / 小爱文本 / 健康名称·类型·备注 / 打印机名） |
| `limit` | 单次上限（默认 500，上限 5000；超出时 `truncated=true`） |
| `detail` | `0` 不返回 `extra` 明细 |

统一条目结构（含 `ts_ms` 便于前端排序）：
```json
{ "ts": "2026-09-19 08:00:00", "ts_ms": 1789771200000, "source": "device", "event": "off",
  "title": "大灯 关闭", "entity_id": "light.keting_dadeng", "name": "大灯", "room": "客厅",
  "icon": "mdi:lightbulb", "user": "Alix", "detail": "运行 1.0 小时 · 0.500 kWh",
  "extra": { "duration": 3600, "duration_hour": 1.0, "energy": 0.5, "running": false } }
```

**来源能力收敛**：`entities`/`rooms`/`users` 是"实体维度"过滤，**不具备该维度的来源会被自动剔除**
（如 `entities=` 会剔除 automation / health / printer），被剔除项列入返回的 `skipped_sources`，
避免"只想查某实体却混进自动化/健康事件"的不可解释结果。

### 🏠 `GET /query?type=room_occupancy`：房间占用排行（严谨口径）

数据源 `device_history` 中 `name='人在'` 的记录（与今日家庭状态同一约定）。
**严谨口径**：同一房间的重叠区间先做**区间并集**再计时 —— 多个"人在"实体、抖动重复上报
都不会重复计时（返回同时给 `raw_duration_hour` 作为"未并集口径"对照）。

| 字段 | 说明 |
|---|---|
| `duration` / `duration_hour` | 并集后的有人时长（运行中记录按"当前时间 − on_time"补当前段） |
| `count` / `segments` | 原始记录数 / 并集后的连续段数 |
| `avg_hour` | 平均单次时长（时长 ÷ 次数） |
| `occupied` / `last_seen` | 当前是否有人（最新记录 `off_time` 为空）/ 最近有人时间 |
| `share` | 占全部房间合计时长的百分比（可直接画饼图/横条） |

参数：时间窗同上（默认今日）、`rooms`、`include_empty=1`（无数据房间也返回 0，清单取自
`entity_configs.room`）、`bucket=none|day|hour`（按日序列 / 24 小时分布）、`door=0`（不返回门户）、
`detail=0`（不返回并集区间明细）。

**门户事件**（`name='入户门'`）：`door.open_count`（开启次数）、`open_duration_hour`（开合累计时长）、
`last_open`、`open_now`、`events[]`（open/close 事件，可直接喂给 timeline 做"回家/外出"时间线）。

### ✨ db_viewer：API 工具新增「家庭洞察」分组

- 「查询类型」新增 optgroup **家庭洞察**：**🕘 统一事件流** / **🏠 房间占用排行**；
- 两个接口的时间模式为 **指定日 / 时间段 / 指定月 / 指定年**（不提供"全部时间"：缺省即"今日"），
  由 `MULTI_TIME_MODE_SETS` 按接口显隐选项并自动回落；
- 专属参数区：事件流可勾选来源、选设备事件（开启+关闭/仅开启/仅关闭）、关键词、条数上限；
  房间占用可填房间、选分解粒度、含无数据房间、门户事件开关。

**验证**（真实函数 + 临时 SQLite 库，17 组断言全通过）：
- timeline：`date` 当日 19 条、`by_source` 各源计数正确（扫地机轨迹点折叠为 2 条事件）；
  A 方案展开 **on=7 / off=5**（运行中只有 on，`detail` 以"已运行"开头）；
  `events=off`、`sources=device`、`entities`（含 `skipped_sources`）、`rooms`、`users`、`keyword`、
  `limit=5 + truncated + detail=0`、`start/end` 跨两天（含昨日记录）、`month` 全部符合预期；
- room_occupancy：客厅两条重叠记录（1h + 1.5h）**并集为 2.0h**（`raw_duration_hour=2.5` 对照）、
  `segments=1`、`share=80.0`、`top_room=客厅`；`include_empty` 补出"厨房 0"；
  `bucket=day` 序列、`bucket=hour`（07h=0.5、08h=09h=1.0、10h=0.0）；门户 `open_count=2`、
  `open_now=true`、事件 3 条；`rooms` 过滤生效。

**涉及文件**：`insights.py`（新增）、`http_api.py`（2 个接口 + 分发）、`db_viewer.html`（洞察分组与参数区）、
`const.py` + `manifest.json`（版本号）。

## 2026-09-19 — v3.6.9 近期使用设备：`all` 节点（device_history）+ `device_last_used` 接口 + 窗口天数/排除项设置

### 🕘 「近期使用设备」新增 `all` 节点（数据源 `device_history`）

`sensor.近期使用设备` 原有 `devices` 节点只覆盖**前端卡片埋点**（用户 × 设备）；新增 `all` 节点改从
`device_history` 取**全量设备**的最近使用情况（含自动化、定时开关等非卡片操作），每个 `entity_id`
一条，与 `devices` 互补。

规则（与今日家庭状态的设备明细同口径）：

| 字段 | 取值 |
|---|---|
| 记录选取 | 窗口内该实体**最新一条**（`on_time` 最大，并列取 `id` 最大） |
| `running` | 最新记录 `on_time` 有值且 `off_time` 为空 → `true` |
| `last_used_text` | 运行中 → **当前时刻**；否则 → 该记录 `off_time` |
| `on_time` / `off_time` | 最新记录的原始值 |
| `count` | 窗口内该实体的开关记录条数 |
| `duration` / `duration_hour` | 窗口内累计时长（秒 / 小时）；运行中记录按「当前时间 − `on_time`」计 |
| `energy` | 窗口内累计用电（kWh）；已关闭取 `energy_consumed`，运行中取 `now_kwh − on_power`，无来源记 0 |
| `name` / `room` / `icon` | 取最新记录（`icon` 即 v3.6.8 新增字段） |

- 排序：`last_used` 倒序（最近使用在最前）；
- 传感器额外属性：`all`（列表）、`total_all`（条数）、`all_range`（统计范围文案）、`exclude_count`（生效的排除项数）。

### 🔢 新增设置实体 `number.ha_data_store_recent_days`（近期使用天数）

- 范围 `1~365`，默认 `30`，单位「天」，可直接在仪表盘调整；
- 读取方：传感器（`devices` 窗口 + `all` 节点）与 `device_last_used` 接口的默认窗口；
- 缺失 / 非数字 / 超范围 → 一律回退 `30`；
- 该实体状态变化时通过 `EVENT_STATE_CHANGED` 监听**立即刷新**传感器（不必等 30 秒轮询）。

窗口过滤作用于 `on_time`：从「今天 − (N−1) 天 00:00:00」起算（含今天的 N 个自然日）。

### 🔌 新增接口 `GET /query?type=device_last_used`

| 参数 | 说明 |
|---|---|
| `entities` / `entity_id` | 逗号分隔实体（空 = 全部） |
| `start` / `end` | 时间段（作用 `on_time`） |
| `date` / `month` / `year` | 指定日 / 月 / 年 |
| `window_days` | 窗口天数（**仅在未传 start/end/date/month/year 时生效**；缺省读设置实体 `number.ha_data_store_recent_days`；**传 `0` = 不限窗口/全部历史**） |
| `filter` | **是否启用「排除项过滤」**，默认 `1`（启用，别名 `use_exclude`）；`0` = 不应用排除项（此时 `exclude` 参数也被忽略），返回全部设备 |
| `exclude` | 逗号分隔排除实体；**不传** = 用保存的排除项，显式传空 = 不排除 |
| `running` | `1` 只返回正在运行的设备 |
| `detail` | `0` 只返回 `entities` 列表，不返回 `items` 明细（默认 1） |
| `limit` / `offset` | 分页（0 = 不限） |

时间过滤优先级与其它接口一致：`start/end` > `date` > `month` > `year` > 窗口天数 ——
即**时间模式与窗口天数互斥**，选了时间段/指定日/月/年后 `window_days` 自动不生效。
`filter=0` 只关闭**排除项过滤**，不影响时间/实体/运行中等其它条件。

> 需要「全量、不做任何过滤」时：`?type=device_last_used&window_days=0&filter=0`。

返回：
```json
{ "range": "最近 30 天", "window_days": 30, "use_exclude": true, "exclude_count": 2,
  "total": 42, "count": 42,
  "entities": ["light.a", "switch.b"],
  "items": [ { "entity_id": "light.a", "name": "大灯", "room": "客厅", "icon": "mdi:lightbulb",
               "running": false, "on_time": "2026-09-19 07:00:00", "off_time": "2026-09-19 08:00:00",
               "last_used": 1789778169240, "last_used_text": "2026-09-19 08:00:00",
               "count": 6, "duration": 3600.0, "duration_hour": 1.0, "energy": 0.5 } ] }
```
`entities` 为去重后的 entity_id 列表（按最近使用时间倒序），即"返回 entity_id 唯一值"的用法。

### 🚫 排除项配置（db_viewer + `api_settings`）

- 存储：`api_settings.recent_exclude_entities`（JSON 数组，兼容逗号/换行文本），**不受实体字符数限制**；
- 接口：
  - `GET /api/ha_data_store/recent/exclude` → `{success, count, exclude[]}`
  - `POST /api/ha_data_store/recent/exclude`（Body `{"exclude": [...]}` 或 `{"text": "a,b\nc"}`）
    → 去重去空保存，并**立即刷新传感器**
  - `GET /api/ha_data_store/recent/entities` → `device_history` 内实体唯一值 `{entity_id, name, room}`（供选择）
- UI：db_viewer「系统配置 → 🕘 近期使用设备」子页：
  - 已选排除项标签（点 ✕ 移除）+ 手动输入框（逗号/换行）+ 保存/刷新按钮；
  - **实体选择器**：搜索框按 `entity_id / 名称 / 房间` 过滤，一键「➕ 排除 / ✔ 已排除（取消）」。

### ✨ db_viewer：API 工具新增查询项

「查询类型 → 设备类」新增 **🕘 近期使用设备（每个实体最近一次使用）**：多实体输入 + 时间模式
（**全部时间 / 最近 N 天（窗口）** / 时间段 / 指定日 / 指定月 / 指定年）+ 是否应用排除项过滤 +
只看正在运行 + 是否返回 `items` 明细。

**时间模式与窗口天数互斥（UI 重构）**：不再提供独立的「窗口天数」输入框，改为并入时间模式的
**「最近 N 天（窗口）」**选项：

- 该选项**仅在本接口可见**（切到其它多实体接口时自动 `hidden/disabled` 并回落为「时间段」），
  进入本接口时默认选中；
- 选中后在下方出现「天数（留空 = 用设置实体）」输入框，留空即沿用
  `number.ha_data_store_recent_days`，填数字则临时覆盖；
- 选「全部时间」= 真正不限窗口（前端显式传 `window_days=0`，避免"全部时间却只返回 30 天"的歧义）；
- 选时间段/指定日/月/年时不再携带 `window_days`，由后端按 `on_time` 过滤。

### 📊 系统监控新增「🕘 最近使用设备」卡片与区块

- `/api/ha_data_store/monitor` 返回新增 `recent` 节点：
  `{count, total, running, window_days, range, exclude_count, items[]}`（`items` 取前 100 台，
  避免响应过大）；窗口天数读设置实体、排除项取自 `api_settings`，与传感器 `all` 节点同源同口径；
  另写入 `types.recent = {count, ok, bad:0, health:"good", running}`（不产生异常角标）。
- 监控页 summary 新增卡片 **🕘 最近使用设备**（数字 = 统计到的设备数），点击卡片展开下方对应区块；
  区块表格列：**实体ID / 名称 / 房间 / 状态（运行中·已关闭）/ 最近使用 / 次数 / 时长(h) / 用电(kWh)**，
  标题右侧显示 `统计范围 · 运行中 N · 排除项 M`；超过 100 台时提示用 API 工具查看全部。

### 🏷️ 「🕘 近期使用设备」子标签角标 = 排除项数量的负值

`setSubTabBadge()` 改为支持负值（`count` 为 0 时不显示），「🕘 近期使用设备」角标显示
**排除项数量的负值**（如 `-5` 表示已排除 5 个实体）。数据来源：

- 页面加载时 `loadRecentBadge()` 取一次 `/recent/exclude` —— 不点开子面板也能看到角标；
- 监控页刷新（`loadMonitor`）时按 `types.recent.exclude_count` 同步；
- 配置页内增删排除项（`loadRecentExclude` / `syncRecentExcludeInput`）**即时反映**（未保存的本地改动也生效）。

### 🐞 修复：设置实体重启 HA 后丢失用户设置

`number.ha_data_store_recent_days`（近期使用天数）与 `text.ha_data_store_ele_list`（用电计量列表条数）
此前**未做状态持久化**，重启 HA 后一律回到默认值（`30` 天 / `"3,3,3"`）。现两个设置实体均改为继承
**`RestoreEntity`**（与「辅助元素」模块同一写法），在 `async_added_to_hass` 中读取上次持久化状态：

- 合法值 → 恢复（日志如 `[HDS] 近期使用天数已恢复上次设置: 7 天`）；
- 空值 / `unknown` / `unavailable` / 非法 / 超范围 → **保持默认**（`30` / `"3,3,3"`），不会写入坏值；
- 解析逻辑抽为模块级函数（`_parse_restored_days` / `_parse_restored_ele_list`），便于单独验证。

### 🐞 修复：`device_last_used` 缺省窗口未读设置实体

接口说明写明「`window_days` 缺省读 `number.ha_data_store_recent_days`」，但实现落到常量 30，
设置实体改了不生效。现改为：`window_days` 未传（或非法/超范围）时**读设置实体**
（缺失/超范围回退 30），`0` = 不限窗口，`1~365` = 临时覆盖。

**验证**（真实源码 + 临时 SQLite 库）：
- 设置实体：缺失 → 30；`15` → 15；`abc` / `999` → 回退 30；
- 排除项：去重去空、JSON 存取正确；
- 计算：运行中实体排第一且 `last_used_text` = 当前时刻、`running=true`；已关闭实体 `last_used_text` = `off_time`；
  窗口外实体不出现；排除项实体不出现；`count` 累计、`duration`（运行中按 now − on_time）、`energy`
  （0.5 + 2.34 = 2.84）均正确；
- `detail=0` 不返回 `items`；`running=1` 只返回运行中；`date` 过滤、`entities` 过滤、`limit` 分页、
  `window_days=1`、`exclude=[]`（显式传空忽略保存的排除项）全部符合预期；
- **过滤开关**：`filter=0` 关闭排除项过滤（`exclude_count=0`、被排除实体重新出现，显式 `exclude` 也被忽略）；
  `window_days=0` 不限窗口（窗口外的历史记录重新出现、`range` = "全部时间"）；
  `filter=0&window_days=0` = 全量无过滤；非法 `window_days`（如 999/-5）回退 30；
- **窗口语义（真实方法源码 + 假 `hass`）**：`window_days` 未传 → 读设置实体（设置 7 → `window_days=7`、
  `range`="最近 7 天"）；显式传 `3` → 临时覆盖；传 `date=10 天前` → 窗口被忽略、只返回该日记录（互斥）；
- **监控页与角标**：`/monitor` 返回新增 `recent`（含 `items`/`running`/`exclude_count`）；
  `setSubTabBadge` 负值行为用 node 模拟 DOM 验证（`-5` → 显示 `-5`，`0` → 隐藏，`3` → 正常显示）；
  db_viewer 接线检查（卡片/区块/角标/标签映射/`node --check`）全部通过；
- **重启恢复（真实类源码 + stub 基类，16 个用例）**：`number` 恢复 `7`→7、`365`→365、`1`→1，
  `999`/`0`/`abc`/`unknown`/`unavailable`/无历史值 → 30；`text` 恢复 `5,3,4`→原值，
  `abc`/`3,3`/`unknown`/无历史值 → `"3,3,3"`；两个类均确认继承 `RestoreEntity`。
- `db_viewer.html` 内联脚本 `node --check` 通过。

**涉及文件**：`recent_devices.py`（新增：计算与设置读写）、`number.py`（新增静态设置实体）、
`sensor.py`（`all` 节点 + 窗口天数 + 状态监听）、`http_api.py`（接口 + 2 个配置视图）、
`__init__.py`（视图注册）、`db_viewer.html`（排除项子页 + API 工具项）、`const.py`、`manifest.json`（版本号）。

## 2026-09-18 — v3.6.8 家庭状态设备明细新增实时字段 + `device_history` 新增 `icon`

### 🏠 `sections.devices` 逐台明细新增 5 个实时/最近字段

「今日家庭状态」`sections.devices` 下的 **`devices[]`**（`energy_top` / `times_top` 共享同一份 dict，
字段同步生效）在原有 `entity_id / name / room / times / duration / energy` 基础上新增：

| 字段 | 口径 |
|---|---|
| `running` | 该实体**当日最新一条** `device_history`（`id` 最大）`on_time` 非空且 `off_time` 为空 → `true`（正在运行） |
| `time` | 已关闭：该记录的 `off_time`（最近一次关闭时间）；运行中：`null` |
| `on_user` | 运行中：该记录的 `on_user`（开机操作人）；已关闭：`""` |
| `off_user` | 已关闭：该记录的 `off_user`（关机操作人）；运行中：`""` |
| `state` | HA **实时状态值**（`hass.states.get(entity_id).state`）；实体不在状态机中为 `null` |

**实现**：`_agg_devices(conn, day, hass)` 聚合时顺带记录每实体的「当日最新一条」（按 `id` 取最大），
最后统一补充上述字段——因 `devices` / `energy_top` / `times_top` 三个列表**共享同一个 dict 引用**，
补一次字段三处同时生效。`hass` 由 `TodayFamilyStatusSensor` 注入（缺省 `None` 时 `state` 恒为 `null`，
不影响其余统计）。

**注意**：`state` 是**实时值**，与 `running`（历史口径，来自 `device_history`）可能不一致
（例如实体当前已 `off`，但记录的关机事件尚未落库），二者分别适用于"看现状"和"看记录"。

### 🔄 家庭状态刷新频率：30 分钟 → 30 秒（修复定时刷新未实际写入）

`TodayFamilyStatusSensor` 原来只「启动后 1 分钟 + 每 30 分钟（整 30 分钟）」刷新，
设备 `running` / `state` / 操作用户等实时字段最长要等 30 分钟。

现改为**启动后 1 分钟生成一次 + 之后每 30 秒更新**（`async_track_time_interval`，
定时器句柄存于 `hass.data[DOMAIN]["cancel_daily_summary"]`）。

**修复**：此前 30 秒定时刷新走的是**内容签名去重**分支（`json.dumps` 排除 `generated_at`），
聚合内容未变化时**不写状态**，导致实体 `last_updated` / 属性 `generated_at` 长时间不动，
看起来"没有按 30 秒刷新"。现改为**默认强制写入**（新增模块常量
`sensor.FAMILY_STATUS_FORCE_WRITE = True`）：每 30 秒确实写入状态，
`generated_at` / `last_updated` 持续更新、刷新节奏可见；启动后首次生成也改为 `force=True`。

> 若需减轻 recorder 压力（每 30 秒会落一条大属性），把 `FAMILY_STATUS_FORCE_WRITE` 置为
> `False` 即恢复"内容无变化不写状态"的旧行为；按钮 / 服务手动触发始终强制写入。

### ✨ `device_history` 新增 `icon` 字段（随记录同步 + 历史回填）

`device_history` 表新增 `icon`（`TEXT NOT NULL DEFAULT ''`），用于记录该设备对应前端卡片的图标。
关联规则：**`device_history.entity_id = report_entities.entity_id`** 时，把 `report_entities.icon`
写入 `device_history.icon`；同一实体有多条上报时取 **`id` 最大（最新上报）** 的一条，**原样写入
（不过滤空值）**：

1. **建表**：`_init_database` 中 `CREATE TABLE` 加入该列；
2. **迁移**：旧库启动时 `PRAGMA table_info` 检测缺列 → `ALTER TABLE ... ADD COLUMN icon`；
3. **新增记录**：`_insert_device_on_record`（开机写入）与 `_do_midnight_splits`（午夜跨天拆分出的
   新记录）插入成功后，调用 `_lookup_report_entity_icon(conn, entity_id)` 取 `report_entities` 中该
   实体最新一条 `icon`（`WHERE entity_id = ? ORDER BY id DESC LIMIT 1`，走 `idx_report_entities_eid`
   索引）并 `UPDATE` 到新记录；无上报记录则保持空串，整段包在 `try/except` 中，不影响主流程；
4. **历史数据回填（覆盖更新）→ 改为按钮按需触发**：`_backfill_device_history_icon(conn)`
   （返回实际写入行数）按 `entity_id` 取该实体最新上报的 `icon`，**覆盖**写入该实体在
   `device_history` 中的全部记录（`WHERE entity_id = ? AND icon IS NOT ?`）—— **不管原有 icon
   是否有值都以本次结果为准**，可纠正此前误写其它来源/旧值的情况；`report_entities` 中无上报记录的
   实体保持原值不动。结果幂等（同来源重复执行第二次起 0 行变更）。

   **不再随 HA 启动自动执行**（历史回填属低频一次性操作：首次升级补列、前端批量改了图标需纠正历史，
   而每次启动全表扫描是纯浪费），改由**新增按钮 `button.ha_data_store_fill_device_icon`**
   （归入主设备「HA数据统一存储系统」）按需触发：
   - 点击 → `hass.data[DOMAIN]["async_fill_device_icon"]`（`_fill_device_icon_sync` 在 executor 中执行）；
   - 状态属性记录 **`写入行数`** / **`执行时间`**；
   - 日志：`[HDS] device_history.icon 回填完成：来源 report_entities（覆盖更新），实体 N 个，写入行数 M`；
   - 补列时（`ALTER TABLE ... ADD COLUMN icon`）日志会提示该按钮；
   - **新增记录时的 icon 同步不受影响**（仍自动写入）。

**前端手动执行等价 SQL**（db_viewer「前端执行 SQL」，一次一条）：
```sql
UPDATE device_history
SET icon = (
    SELECT r.icon FROM report_entities r
    WHERE r.entity_id = device_history.entity_id
    ORDER BY r.id DESC LIMIT 1
)
WHERE entity_id IN (SELECT entity_id FROM report_entities);
```

**验证**（SQLite 内存库 / 临时库文件，直接执行源码中的真实函数）：
- 覆盖语义：原有非空值（如 `mdi:keepme`、`mdi:old`）被覆盖为最新上报值；上报最新一条为空串时照样覆盖为空；
- 无上报记录的实体保持原值不动；`report_entities.icon` 更新后再次回填可同步纠正；
- 新增：新插入记录自动带上最新上报 icon（最新为空串则写入空串），无上报实体为空串；
- 幂等：来源未变时重复回填 0 行变更；
- 按钮链路：`_fill_device_icon_sync(db_path)` 首次返回写入行数 3、再点一次返回 0，数据正确。

> ⚠️ 需**重启 HA** 触发 `ALTER TABLE` 建列；建列后**不会**自动补历史 icon，
> 请点击 `button.ha_data_store_fill_device_icon`（或执行上面 SQL）完成历史回填。
> `icon` 暂未输出到家庭状态实体的 `sections.devices` 与 db_viewer，需要时另行接入。

**涉及文件**：`__init__.py`（建表/迁移/写入/回填）、`daily_summary.py`（设备明细字段）、
`sensor.py`（30 秒刷新）、`const.py` + `manifest.json`（版本号）。

## 2026-09-11 — v3.6.6 用电计量「取消登记」改为软删除（回收站），重新登记自动沿用历史数据

### ✨ `entities_dates`（多实体有数据日期）支持指定 年/月/日

「多实体有数据日期」接口的后端 `compute_entities_dates_sync` **本就透传** `date`/`month`/`year`
（经由 `_fetch_usage_rows` → `_usage_range_params`），但前端**没有对应输入控件**，无法使用。

**前端**（db_viewer）：
- 将 `entities_dates` 纳入「时间模式」作用范围：
  `isTimeModeApi = isMultiBucketApiType(qt) || isDevDetailApiType(qt) || qt === 'entities_dates'`
- 其 URL 分支改用 `multiTimeQuery()` 生成时间参数，并保留 `group` 分组方式

现在该接口可使用：**全部时间 / 时间段 / 指定日 / 指定月 / 指定年**。

**验证**：6 项可见性用例 + 5 种时间模式 URL 生成全部通过，例如：
```
[all  ] ?type=entities_dates&key=K&group=count
[range] ?type=entities_dates&key=K&start=2026-09-01&end=2026-09-30&group=count
[date ] ?type=entities_dates&key=K&date=2026-09-11&group=count
[year ] ?type=entities_dates&key=K&year=2026&group=count
[month] ?type=entities_dates&key=K&month=2026-09&group=count
```
`node --check` + 3 项接线检查 + 跨作用域扫描（321 函数 0 问题）通过。


### ✨ `entities_dates`（多实体有数据日期）新增 2 种分组方式

「分组方式」由原来的 2 种扩展为 **4 种**（`group` 参数支持语义化取值，同时兼容旧的 `0`/`1`）：

| group | 说明 | 新增字段 |
|---|---|---|
| `all`（默认，兼容 `0`） | 合并返回：去重日期总表 + 每实体日期 | `all_count`/`all_dates`/`entities[]` |
| `entity`（兼容 `1`） | 按实体分组 | `entities[]` |
| **`count`** ⭐ | **按日期数量返回**：把"当天有数据的实体数"相同的日期聚成一组 | `date_counts{date:实体数}`、`count_groups[{date_count, date_total, dates[]}]` |
| **`both`** ⭐ | **按日期数量 + 实体返回**：在 `count` 基础上，每组再带出当天具体实体 | 每组 `details[{date, entity_count, entities[]}]` 与 `entities[{entity_id, name, date_count}]`（去重，按出现天数降序）；顶层同时保留 `entities[]` |
| **`simple`** ⭐ | **极简返回**：直接给扁平数组，开箱即用 | `date_count`（有数据的天数）、`list:[{date, count}]`（`count` = 当天有数据的实体数，**日期倒序**） |

`simple` 示例（最省事的形态，无需再遍历对象）：
```json
{ "group": "simple", "date_count": 4,
  "list": [ { "date": "2026-09-05", "count": 2 },
            { "date": "2026-09-03", "count": 1 },
            { "date": "2026-09-02", "count": 2 },
            { "date": "2026-09-01", "count": 3 } ] }
```

**前端**：「分组方式」下拉新增 **⑤ 极简：[{date, count}]**。

**排序约定**：
- `count_groups` 按 `date_count` **升序**（从"只有 1 台"到"全部设备"），便于快速定位低活跃/高活跃日
- 组内 `dates` 按**日期倒序**（最新在前）
- `both` 的模式内 `entities` 按 `date_count` 降序

**实现**：`compute_entities_dates_sync(..., group="all")` 内部改为先建
`date → {entity: name}` 与 `entity → {name, dates}` 双向索引，再按 `group` 组装；
非法 `group` 值回落 `all`。视图 `_query_entities_dates` 按 group 裁剪返回字段。

**前端**（db_viewer）：「分组方式」下拉新增 **③ 按日期数量返回** 与 **④ 按日期数量 + 实体返回**，
通过 `data-for="entities_dates"` 标记，仅在该接口下显示（其它接口的 `group` 语义是"合并/按实体"，
故切换接口时自动隐藏并禁用；若当前选中被隐藏项则自动回落到"合并返回"）。

**验证**：10 项断言全通过 —— 4 种分组结构正确、`count_groups` 分组与排序正确
（3 台→1 天 / 2 台→2 天 / 1 台→1 天）、`both` 的 `details` 与去重实体统计正确、
旧写法 `0`/`1` 与非法值回落、时间段过滤 + `count` 组合、多实体过滤、空结果；
`node --check` + 8 项前端接线 + 跨作用域扫描（321 函数 0 问题）全通过。


### 🔧 精简：`device_usage_detail` 移除冗余的 `columns` 字段

`full=1` 时响应里的 `columns` 数组（列出 20 个列名）属冗余信息 —— 记录本身已包含全部字段，
调用方遍历首条记录即可得知可用键名。已**移除** `columns` 输出，同时删掉随之无用的
`PRAGMA table_info(...)` 查询（少一次 DB 往返）与 `cols` 变量。

**响应结构变化**（仅去掉一个键，其余不变）：
```
{ full, range, entity_count, total, returned, limit, offset, summary, records[] }
```

**验证**：顶层键集合精确匹配；`records[0]` 仍为**全 22 字段**（含 `state_attr`/`power_entity`/
`power_rating` 等派生列）；`summary` 数值不变（`count=4 / 8.0h / 3.0kWh / running_count=1`）；
精简模式与 `summary=0` 路径均正常。


### ✨ `device_usage_detail` 新增 `summary` 合计节点

多实体明细接口新增 `summary` 节点，含**全局总计**与**每实体合计**：

```json
"summary": {
  "totals": { "count": 6, "duration_hour": 10.0, "energy_kwh": 3.4,
              "entity_count": 2, "running_count": 1 },
  "entities": [
    { "entity_id": "switch.ac", "name": "空调", "room": "客厅",
      "count": 4, "duration_hour": 8.0, "energy_kwh": 3.0, "running_count": 1 },
    { "entity_id": "switch.light", "name": "灯", "room": "卧室",
      "count": 2, "duration_hour": 2.0, "energy_kwh": 0.4, "running_count": 0 }
  ]
}
```

**关键设计**：
- **`summary` 基于全量匹配记录（分页前）计算**，**不受 `limit`/`offset` 影响** ——
  分页只用于翻看明细，合计应始终反映整个查询范围。
- 新增 `running_count`（当前仍在运行的会话条数）：单条记录的 `running` 是布尔值，
  **无法直接累加**，故改用计数表达。
- `energy_kwh` 无用电来源时为 `null`（与 `device_history` 系列口径一致）；
  实体按 `duration_hour` 降序，多实体统计口径与 `device_usage_total` 交叉验证一致。

**新增参数** `summary=0`：不返回 `summary` 节点（纯明细场景可减小响应体积）；
默认返回。修复 `entity_count` 在 `summary=0` 时被误算为 0 的问题。

**前端**（db_viewer）：明细模式新增 **「返回合计节点（summary）」复选框（默认勾选）**，
取消勾选时 URL 追加 `&summary=0`。

**验证**：10 项断言全通过，含
全量口径正确（count=6 / 10.0h / 3.4kWh / running_count=1）、每实体合计、
`limit=1/2`+`offset=3/5` 各组合下 `summary` **恒定不变**、
`summary=0` 时 `entity_count` 仍为 2、`summary`+`full` 组合、
精简模式记录字段未变（向后兼容）、与 `device_usage_total` 同范围数值一致；
`node --check` + 4 项前端接线 + 跨作用域扫描（320 函数 0 问题）全通过。


### 🐛 修复：多维度聚合的「时间模式」UI 不显示（再次踩中跨作用域变量）

**现象**：`device_usage_multi` / `power_energy_multi` 下看不到「时间模式」下拉与年/月输入。

**根因**：`onApiTypeChange()` 中写成了 `if (isBucketApiMode)`，但该变量**只声明在 `generateApiUrl()` 内**。
JS 运行到此行抛 `ReferenceError`，导致 `onApiTypeChange()` **从此处起整体中断**（后续的 label/文案/scope/分页等 UI 全部不再更新）。

> 与上一轮「API URL 为空」是**同一类错误**（跨函数引用局部变量），只是这次发生方向相反：
> 上次是 `generateApiUrl` 引用 `onApiTypeChange` 的变量，这次是反过来。

**修复**：改用模块级 `isMultiBucketApiType(qt)`，并在代码内加注释标注该陷阱。

**加强防护**：新增**全文件跨作用域扫描器**，逐一收集 320 个顶层函数的局部声明，
检测「函数 A 引用了只在函数 B 中声明的 `is*`/`el*` 变量」→ 结果 **0 处**（修复前为 1 处）。

**验证**：迷你 DOM 模拟实跑 `onApiTypeChange` 可见性逻辑：
- 3 个聚合模式（含 `entities_period_agg`）→ 时间模式下拉显示 ✅
- 2 个非聚合模式（`device_usage_detail`/`device_usage_avg`）→ 隐藏且恢复原日期输入 ✅
- 五种模式切换 → 日期/年/月输入的显示组合全部正确（`all`/`range` 隐藏值区、`date` 只显日、`month` 显年+月、`year` 只显年）✅

### ✨ 多实体明细同样支持「全部/时间段/指定日/指定月/指定年」时间模式

**前端**（`db_viewer.html`）：
- 明细接口 `device_usage_detail` 由原先仅「开始/结束日期」改为**复用同一套「时间模式」下拉**
  （全部时间 / 时间段 / 指定日 / 指定月 / 指定年），与两个多维度聚合接口交互一致
- `onApiTypeChange` 的可见性条件由 `isMultiBucketApiType(qt)` 扩展为
  `isMultiBucketApiType(qt) || isDevDetailApiType(qt)`
- `generateApiUrl` 的明细分支改用 `multiTimeQuery()` 生成时间参数，分页（`limit`/`offset`）
  与 `full=1` 逻辑保持不变
- 下拉选项文案更新为「📋 多实体明细（不聚合，全部/时间段/指定日/指定月/指定年，支持分页）」

**验证**：
- 可见性：`device_usage_detail` / `device_usage_multi` / `entities_period_agg` → 显示；`device_usage_avg` → 隐藏
- 明细 URL 实测 5 种模式均正确且无 `undefined`：
  `all → &full=1`、`range → &start=..&end=..&full=1`、`date → &date=..&full=1`、
  `year → &year=..&full=1`、`month → &month=2026-09&full=1`
- 全文件跨作用域扫描：320 个顶层函数，**0 处**引用异常；`node --check` 通过

### ✨ 多维度聚合（用时/用电、用电量）新增「指定日/指定月/指定年」时间模式

**背景**：`device_usage_multi` 与 `power_energy_multi` 的**后端早已支持** `date`/`month`/`year`
（见 `_usage_range_params` 与视图透传），但 API 工具中**只有开始/结束日期**两个输入，无法传这三个参数。

**前端新增**（`db_viewer.html`）：
- 多维度聚合模式下新增 **「时间模式」下拉**：`全部时间` / `时间段` / `指定日` / `指定月` / `指定年`
- 按模式联动显示输入：
  - 时间段 → 开始/结束日期
  - 指定日 → 日期选择器
  - 指定月 → 年 + 月（月支持只填 `9`、或配年自动补零为 `2026-09`）
  - 指定年 → 年
  - 全部时间 → 不传任何时间参数
- `multiTimeQuery()` 统一生成时间参数片段；`onMultiTimeModeChange()` 负责 UI 联动
- 兼容：非聚合模式仍显示原有的开始/结束日期，行为不变

**参数优先级**（沿用 `_usage_range_params`）：`start`/`end` > `date` > `month` > `year`；全不传 = 全部时间。

**验证**：
- **后端过滤实测**：全部=4、`date=2026-09-01`=1、`month=2026-09`=2、`month=2025-09`=1、
  `year=2026`=3、`start~end`=2，优先级（range 覆盖 date）正确；`year=2026` + `bucket=month`
  组合返回 `2026-09:2 / 2026-08:1`
- **URL 生成实测 9 组**（Node）：全部→无参数、时间段（含单边）、指定日、指定年、
  指定月（补零 `2026-09` / 仅月 `9` / 缺月退化为年），均无 `undefined`
- `node --check` + 16 项接线检查（含跨作用域变量检测）全通过


### ✨ `device_usage_detail` 新增 `full=1`：返回全部字段

多实体明细接口增加 `full` 参数（API 工具中为**「返回详细数据（全部字段）」复选框，默认勾选**）。

| `full` | 返回 |
|---|---|
| 不传 / `0` | 精简字段（原行为，结构未变）：`entity_id/name/room/on_time/off_time/duration_hour/energy_kwh/running` |
| `1` | **`device_history` 全部列 + 计算字段**：`id`、`entity_id`、`name`、`on_time`、`off_time`、`on_power`、`off_power`、`energy_consumed`、`duration`(秒)、`cross_day`、`room`、`state_attr`(**已解析为 JSON 数组**)、`now_kwh`、`on_user`、`off_user`、`on_snapshot`、`off_snapshot`、`power_entity`、`power_rating`、`duration_hour`、`energy_kwh`、`running`；并附 `columns` 字段列出可用列名 |

**实现要点**：
- `full=1` 时用 `SELECT dh.*` 逐行原样返回（不做 GROUP BY），`room` 用 `COALESCE(dh.room, ec.room)` 合并；
  `power_entity`/`power_rating` **只存在于 `entity_configs`**，需以**原名**补出（`_row_running_usage` 依赖这两个键名，改用别名会导致取键报错）。
- `state_attr` 复用既有 `_parse_records_state_attr` 口径，字符串解析为 JSON 数组。
- 向后兼容：**`full` 缺省即原精简结构**，已有调用方无需改动。

**验证**：断言全字段存在性、运行中记录（`duration_hour=2.0` / `energy_kwh=None` / `on_user` 保留）、
`state_attr` 解析为数组、`power_entity`/`power_rating` 由配置表补齐、快照/`cross_day`/`id` 原始列保留、
`full=0` 结构未变、`full` 与 `date`/`limit` 组合正常；`node --check` + 6 项前端接线检查全通过。


### ✨ 新增 4 个设备类接口（`device_history` 多实体查询族）

| 接口 | 用途 | 关键参数 |
|---|---|---|
| `/query?type=device_usage_detail` | **① 明细（不聚合）** | `entities`、`start`/`end`/`date`/`month`/`year`、`limit`/`offset` |
| `/query?type=device_usage_total` | **② 合计（年/月/日/全部）** | `entities`、`scope=all\|year\|month\|date` + `year`/`month`/`date` |
| `/query?type=device_usage_history` | **③ 历史同期（历史今日/历史本月）** | `entities`、`scope=today\|month` |
| `/query?type=device_usage_avg` | **④ 平均指标** | `entities`、`start`/`end`/`date`/`month`/`year`（默认全部时间） |

**① 明细**：每条记录一行，按 `on_time` 倒序；`energy_kwh` 无来源为 `null`；返回 `total`/`returned` 便于分页。
**② 合计**：每实体一行 + 全局 `totals`；`scope` 宽容写法（`month` 支持 `9`（配 `year`）/`09`/`2026-09`）。
**③ 历史同期**：`scope=today` 取往年**同月同日**、`scope=month` 取往年**同月**（均**排除今年**），另附 `current`（今年同期）与 `by_year` 逐年合计，便于同比。
**④ 平均**：`avg_daily_count` 平均每日次数（次数/有数据天数）、`avg_daily_duration_hour` 平均每日时长、`avg_per_count_hour` 平均每次时长；全局按**加权**计算（总时长/总次数、总次数/天数并集），非各实体简单平均。

**口径统一**：四个接口与既有 `device_history` 系列共用 `_usage_range_params` / `_row_running_usage`，
运行中记录一律按「当前时间 − on_time」计时、无用电来源则 `energy_kwh = null`，避免同一设备在不同接口数值打架。

**API 工具（db_viewer）**：设备类分组新增上述 4 项；
新增「统计范围」下拉（自动切换 全部/年/月/日 或 历史今日/历史本月）与「条数上限/偏移」分页输入。

**验证**：4 接口共 16 项断言全通过（明细的倒序/分页/时间段、合计的 all/year/month/date 与 `YYYY-MM` 写法、
历史同期正确**排除今年**且 `by_year` 为 [2024, 2025]、平均值的逐实体与全局加权、运行中设备三接口口径一致、空结果不报错）；
`node --check` + 10 项前端接线检查（含跨作用域变量检测 = none）全通过。


### 🐛 修复：新增接口「生成的 API URL」为空（跨函数引用局部变量 → ReferenceError）

**现象**：API 工具选中 `power_energy_multi` / `device_usage_multi` 后，「📋 生成的 API URL」输入框**为空**。

**根因**：`isMultiBucketApi` / `isMultiEntitiesApi` / `isPowerMulti` 等判定变量被定义在 **`onApiTypeChange()` 内部**（局部作用域），却在另一个函数 **`generateApiUrl()`** 中被引用。JS 运行到该行抛 `ReferenceError: isMultiBucketApi is not defined`，函数中断 → URL 从未被赋值。

**修复**：
- 提升为**模块级常量 + 判定函数**，供所有函数安全复用：
  ```js
  const MULTI_BUCKET_API_TYPES = ['entities_period_agg','power_energy_multi','device_usage_multi'];
  function isMultiBucketApiType(qt) { ... }
  const MULTI_ENTITIES_API_TYPES = [...同族 6 项...];
  function isMultiEntitiesApiType(qt) { ... }
  ```
- `generateApiUrl()` 改用 `isMultiBucketApiType(qt)` / `isMultiEntitiesApiType(qt)` 判断
- `onApiTypeChange()` 也改为引用同一份清单（消除两处硬编码不一致的隐患）
- 代码内加注释说明该陷阱，防止后续再犯

**验证**：
- 静态检测：`generateApiUrl` 内 `is[A-Z]*` 变量引用 `MISSING = none`（此前为 3 处）
- Node 实跑 URL 拼接，5 组用例均**非空且参数正确**，例如：
  `/query?type=device_usage_multi&key=K&entities=switch.ac%2Cswitch.light&start=2026-09-01&end=2026-09-30&bucket=month&view=entity`
- `node --check` 通过


### ✨ 新增接口：`/query?type=device_usage_multi`（设备用时/用电多实体 × 多维度聚合）

**需求**：与 `power_energy_multi` 同款能力，但作用于**设备类**（`device_history`）。

**实现**：新增 `compute_device_usage_multi_sync(db_path, entity_ids, bucket, view, start, end, date, month, year, include_devices, now_dt)` + `QueryView._query_device_usage_multi`。

**参数**（与 `power_energy_multi` 完全对齐）：
`entities`（逗号分隔，空=全部）、`bucket=day|month|year`、`view=entity|date`、
`start`/`end`/`date`/`month`/`year`（优先级 start/end > date > month > year，全不传 = 全部时间）、`devices=0` 可省明细。

**指标**（沿用 `device_history` 系列既有口径）：
| 字段 | 说明 |
|---|---|
| `count` | 开启次数 |
| `duration_hour` | 时长（小时）。运行中记录按「当前时间 − on_time」计入 |
| `energy_kwh` | 用电量。已关闭取 `energy_consumed`；运行中 ① `now_kwh − on_power` ② 固定功率/1000×小时 ③ 无来源 → **null** |
| `running` | 该时间桶内是否存在运行中的记录 |

**返回结构**：与 `power_energy_multi` 同形（`view=entity` → `entities[].series[]`；`view=date` → `dates[].devices[]`），
另带 `room`（来自 `entity_configs`）。排序：entity 视图按 `duration_hour` 降序，date 视图时间倒序 + 桶内按时长降序。

**语义修正**：`view=date` 的 `totals.device_count` 表示**该时间桶内出现过的设备数**（`entity_count` 语义在桶维度下不成立，故改用 `device_count` 并保留 `entity_count` 于顶层 `totals`）。

**API 工具（db_viewer）**：**设备类**分组新增 **📊 多实体×多维度聚合（用时/用电，年/月/日，按实体或按时间）**，
复用同一套表单区（下拉文案随模式切换为「次数/时长/用电」）。

**验证**：14 项断言全通过（全部/day 汇总与排序、月聚合含跨月、年聚合、时间段+多实体、单实体、
指定日/月/年、两种 view 结构与排序、`devices=0`、**运行中设备**（时长=now−on_time / energy=null / running=true）、
空结果不报错）；`node --check` + 10 项前端接线检查全通过。


### ✨ 新增接口：`/query?type=power_energy_multi`（用电量多实体 × 多维度聚合）

**需求**：`power_energy_daily` 表支持查询**多实体**、**多维度**（指定时间/时间段/全部）、**聚合数据**（年/月/日）、**多种返回类型**（按实体/按时间）。

**实现**：新增模块级 `compute_power_energy_multi_sync(db_path, entity_ids, bucket, view, start, end, date, month, year, include_devices)` + `QueryView._query_power_energy_multi`。

| 参数 | 说明 |
|---|---|
| `entities` | 多个实体，逗号分隔（`entity_id` 亦可，空 = 全部实体） |
| `bucket` | 聚合维度：`day`（默认）/ `month` / `year` |
| `view` | 返回结构：`entity`（默认，实体→时间桶）/ `date`（时间桶→实体） |
| `start` / `end` | 日期区间，可单边 |
| `date` / `month` / `year` | 指定某日 / 某月 / 某年 |
| `devices` | `view=date` 时是否返回明细，`0` 关闭（默认 1） |

**时间过滤优先级**：`start`/`end` > `date` > `month` > `year`；**全不传 = 全部时间**。

**返回结构**：
```
view=entity（按实体）：
{ view, bucket, range:{start,end,date,month,year,label},
  entity_count, totals:{kwh, day_count, entity_count},
  entities:[{ entity_id, device_name, room, id_slug, daily_entity_id,
              totals:{kwh, day_count},
              series:[{key, kwh, day_count}] }] }   ← 按 kwh 降序

view=date（按时间）：
{ view, bucket, range:{...}, entity_count,
  totals:{kwh, day_count, bucket_count, entity_count},
  dates:[{ key, totals:{kwh, day_count, entity_count}, device_count,
           devices:[{entity_id, device_name, room, id_slug, daily_entity_id,
                     kwh, day_count}] }] }          ← 时间倒序，实体按 kwh 降序
```
- 元信息（device_name / room / id_slug / daily_entity_id）LEFT JOIN `power_meter_configs`，
  配置缺失时回退日表自带值；`device_name`/`room` 取该实体最新非空值
- 无数据时返回空数组结构（不报错）

**API 工具（db_viewer）**：「⚡ 用电计量」分组新增 **📊 多实体×多维度聚合（年/月/日，按实体或按时间）**，
复用「多实体 + 粒度 + 返回结构 + 时间段」表单区（下拉文案随模式切换），URL 形如：
```
/query?type=power_energy_multi&entities=a,b&bucket=month&view=entity&start=2026-01-01&end=2026-09-30
```

**验证**：11 项断言全通过（全部/day、月/年聚合值、时间段+多实体、单实体、指定日/月/年、
两种 view 的结构与排序、`devices=0`、空结果不报错）；`node --check` 通过，前端 9 项接线检查全通过。


### ⚠️ 修复：`device_registry.devices` 映射用法弃用告警（HA 2027.9 移除）

**告警**：`Detected that custom integration 'ha_data_store' uses device_registry.devices as a mapping... at http_api.py, line 11325: for device in list(drg.devices.values())`

**根因**：HA 已将 `DeviceRegistry.devices` 从 `dict[str, DeviceEntry]` 改为 `Collection[DeviceEntry]`（内部用 `_DeprecatedDeviceRegistryItemsView` 包装）。**直接迭代是支持的**，但把它当映射用（`.values()` / `.get()` / `.items()` / `[device_id]`）会触发弃用告警，计划 **2027.9** 移除。

**修复**（`DeviceCleanView._list_empty_devices`，设备清理功能）：
- `for device in list(drg.devices.values())` → **`for device in drg.devices`**（直接迭代，官方推荐路径）
- 全仓扫描确认无其它 `device_registry.devices` 映射用法
- 顺带补充注释：`entity_registry.entities` 的 `.values()` / `.items()` **未弃用**（HA 源码标准写法），仅有 device registry 受影响，避免后续误改

注：本次仅改 1 行逻辑 + 注释，功能行为完全不变（设备清理的判定规则与结果一致）。

### 🐛 修复：回收站永远显示「加载中...」（两处前端缺陷）

**缺陷 1 — 提前 `return` 跳过了回收站刷新**（核心原因）
`loadPowerMeters()` 中「无生效中登记」分支直接 `return`，导致其后的 `loadPowerArchived()` 永不执行。
当**所有登记都被取消**（`?type=configs` 返回 `[]`，唯一那条为 `enabled=0`）时必然命中该分支 —— 正是本场景（Network 里只看到 `?type=configs`，从未发出 `?type=archived` 请求）。

**修复**：把空列表分支改为 `if/else` 赋值，不再 `return`；`loadPowerArchived()` 移到函数最末无条件执行。

**缺陷 2 — 误加 key 导致参数重复（`key=x&key=x`）**
页面顶部**已存在全局 `window.fetch` 拦截器**，会自动为所有 `/api/` 请求追加 `key`。修复缺陷 1 时曾误加 `powerApiUrl()` 再拼一次 key，导致 URL 出现两个 `key` 参数（引发"查询无数据"）。现已**移除 `powerApiUrl()`**，15 处调用统一回到裸 `fetch(POWER_API + '?type=...')`，由全局拦截器统一鉴权（并在代码中加注释说明，避免再次误加）。

**验证**：`node --check` 通过；断言「power_energy 手动拼 key 的调用 = 无」；断言 `loadPowerArchived()` 位于 `loadPowerMeters()` 函数末尾（首个 `return` 已不存在）。

### 🐛 修复：db_viewer 页面被浏览器缓存导致"前端改动不生效"

**现象**：后端接口返回正确数据（如 `?type=archived` 返回归档项），但页面上该区块永远停在写死的「加载中...」——因为浏览器用的是**缓存的旧 HTML/JS**，新函数从未被调用。

**根因**：`DBViewerView` 返回整份内联 HTML（含全部 JS/CSS）时**未下发任何缓存控制头**，浏览器会自行缓存。此前"HTML 热重载"只解决了**服务端**重读文件，没解决**浏览器**使用旧副本的问题。

**修复**：`DBViewerView` 的页面与登录页响应统一加上
`Cache-Control: no-store, no-cache, must-revalidate, max-age=0` + `Pragma: no-cache` + `Expires: 0`，
确保每次都取最新页面，从根上杜绝此类"改了没生效"的假象。

**前端兜底**：回收站区块新增 **🔄 刷新回收站** 按钮；占位文案补充「若长时间不变，请 Ctrl+Shift+R 强制刷新」提示。

### ✨ 新增：孤儿电表识别（配置行已被旧版本物理删除的补救）

**背景**：旧版本的「取消登记」是**物理 `DELETE`** 配置行。若在此之前点过取消登记，`power_meter_configs` 里已无该行，回收站自然为空，且无法恢复配置 —— 但 `power_energy_daily` 的历史用电数据仍在。

**新增**：
- `PowerEnergyManager.list_orphan_meters()`：反查「日表有数据但配置表无登记」的实体，返回 `entity_id / device_name / room / first_date / last_date / day_count / total_kwh`
- API：`GET ?type=orphans` → 孤儿电表列表；`?type=archived` 的 `_diag` 附带 `orphan_count`
- 前端回收站区块下方新增**孤儿电表表格**（含数据区间/天数/累计 kWh），每行 **「重新登记」** 按钮一键回填 `entity_id / 设备名 / 房间` 到登记表单，用户补填原 ID 段即可接续历史数据
- `loadPowerArchived()` 增加「加载中」态与错误兜底，避免 fetch 异常时 UI 卡在"加载中..."


**根因**：`power_meter_configs.enabled` 在部分历史库中被建为 **TEXT** 类型。`archive_config` 执行 `SET enabled = 0` 时写入的是字符串 `'0'`，而 Python 侧 `bool('0')` 为 **True**，导致归档项被误判为"生效"，`archived` 过滤后**列表为空**。

**修复**：
- `ensure_tables` 新增 **enabled 列类型归一化迁移**：检测列亲和性，若非 `INTEGER` 则**重建表**（RENAME→CREATE→INSERT SELECT→DROP）彻底修正存储类型；已是 INTEGER 的库走轻量 `UPDATE ... CASE` 归一化
- 所有判定点（`load_configs` / `archive_config` / `restore_config` / `purge_all_archived`）统一改用 `COALESCE(CAST(enabled AS TEXT),'1') IN ('0','false','no','')` 判归档，兼容任何脏值
- 新增 `_is_enabled(row)` 宽容判定工具（True/1/'1'/'true'/'yes' → 生效；False/0/'0'/'false'/'no'/None → 归档）
- 新增 `load_archived_configs()` 供回收站列表使用

### ✨ 新增：回收站「一键还原」

- 后端 `restore_all_archived()` + `POST action=restore_all`：一次性还原回收站全部登记并重新注册日/月/年用电实体（DB 翻转在 executor，实体注册回事件循环）
- 前端回收站顶部新增 **♻️ 一键还原全部（N）** 按钮，行内保留单条「♻ 恢复」

### 🐛 修复：`daily_entity_id` 未写入 —— 语义纠正为「登记产物」

**根因**：`daily_entity_id` 被设计成前端可填的"可选输入项"（表单里叫「显示日用电量实体（可选）」），但**该实体本来就是登记后由本模块自动注册的**（`PowerDailySensor.entity_id` = `sensor.ha_data_store_{id_slug}_daily_ele`）。用户实际填的往往是"期望生成的名字"，与实际注册的实体 ID 不一致，导致字段混乱/为空；同时回填逻辑用「同表自引用子查询」，SQLite 对 UPDATE 目标表求值不稳定，实测 0 行。

**修复**：
- 新增 `daily_entity_id_of(id_slug)`：**唯一权威来源** = `sensor.ha_data_store_{id_slug}_daily_ele`
- `save_config` 改为**忽略外部传入**，一律按 `id_slug` 派生写入（保证与 `PowerDailySensor` 实际实体 ID 永远一致）
- `ensure_tables` 启动时按 `id_slug` **自动纠正**配置表与日表的历史脏值（空值 / 手填错值 / 遗留自定义名），幂等
- `set_daily_entity_id`（手填）→ 改为 `repair_daily_entity_ids()`：按派生值统一纠正两表；`backfill_daily_entity_ids()` 保留为别名
- API：移除 `action=set_deid`（手填语义不成立）；`action=backfill_deid` 语义改为"按 ID 段重新生成并纠正"
- 前端：**删除「显示日用电量实体」输入框**，改为只读预览（随 ID 段实时派生，`syncPmDailyEntityId()`），`daily_entity_id` 不再随创建请求提交；列表列改为只读，新增「月/年用电实体」列；按钮更名为 **🔧 按 ID 段纠正日用电实体**（带确认提示）

### ♻️ 背景与问题

旧实现中「取消登记」是**物理删除** `power_meter_configs` 行（日表 `power_energy_daily` 保留）。由此带来两个问题：

1. 重新登记同一功率实体时必须手动重填 `id_slug`，一旦填得不一样就会生成**新的实体 ID**，历史日用电数据"看不见了"（实际还在表里，但实体口径分叉）；
2. 误删后无法恢复配置，只能凭记忆重建。

### ♻️ 新机制：软删除归档 + 回收站

- `PowerEnergyManager.remove_config()` → **新增 `archive_config()`**：取消登记改为把配置行置为 `enabled=0`（归档），配置行保留在库中
- `load_configs()` 默认只返回 `enabled=1`（生效中）的登记；采样、`restore_all()`、`all_power` 统计均自动跳过归档项
- 新增 `find_config()` / `restore_config()` / `purge_config()` / `purge_all_archived()`
- **`save_config()` 智能沿用**：同一 `entity_id` 重新登记时保留原 `id`、`created_at`，并补齐未显式传入的 `id_slug` / `device_name` / `room` / `daily_entity_id`
- `power_energy_daily` 在任何路径下都不会被删除（含"彻底删除"）

### 🔌 API 变更（`/api/ha_data_store/power_energy`）

| 方法 | 参数 | 说明 |
|---|---|---|
| GET | `?type=configs` | 生效中的登记列表（不含归档） |
| GET | `?type=archived` | **新增**：回收站列表（已取消登记的配置） |
| GET | `?type=lookup&entity_id=` | **新增**：查单个登记（含已归档），供前端"带出原配置" |
| POST | `action=create` | 重新登记时自动沿用归档配置，返回消息提示「沿用原有配置与历史用电数据」；不再物理删除旧配置 |
| POST | `action=delete` | 语义改为**取消登记（归档）**，日表数据保留 |
| POST | `action=restore` | **新增**：从回收站恢复登记并重新注册三个用电实体 |
| POST | `action=purge` | **新增**：彻底删除登记（物理删配置行，日表保留） |
| POST | `action=purge_all` | **新增**：清空回收站 |

### 🖥 db_viewer 前端（⚡ 用电计量）

- 「已登记的功率计量」新增 **编辑** 按钮：一键把该登记的 `entity_id/设备名/房间/ID 段/日用电实体/单位` 回填到登记表单
- 行内「删除」→ **「取消登记」**；批量「删除选中」→ **「取消登记选中」**；确认弹窗文案改为说明"移入回收站、历史数据保留"
- 登记表单：**ID 段留空时自动沿用该实体的历史登记**（含已取消登记的），无需手填也能避免实体 ID 分叉
- 新增 **♻️ 回收站** 区块：列出已取消登记的配置（功率实体/设备名/房间/ID 段/日用电实体/取消时间）+ 恢复 / 彻底删除 / 清空回收站
- 切到「⚡ 用电计量」子页时自动刷新列表与回收站

涉及 `power_energy.py`、`http_api.py`、`db_viewer.html`、`const.py`、`manifest.json`；版本 → v3.6.6

## 2026-09-10 — v3.6.5 新增「接口管理」模块：新增/修改接口无需重启 HA

### 🧩 新接口管理模块（声明式定义 + 免重启）

**背景**：HA 的 `register_view()` 只在集成 setup 时注册路由且无公开注销 API，导致以往新增接口必须重启 HA。本版本引入"固定通配路由 + 定义存库运行时加载"机制，绕开该限制。

**新表 `api_endpoints`**（与旧 `custom_routes` 完全独立，含建表/索引/补列迁移）：
`name(主键) / title / description / query_def / enabled / cache_sql / max_rows / created_at / updated_at`

**新增 5 个视图**（`http_api.py`，注册于 `__init__.py`）：

| 方法 | 路径 | 用途 | 鉴权（复用现行逻辑，未新增开关） |
|---|---|---|---|
| GET/POST | `/api/ha_data_store/ext/{name}` | 新接口执行（每次请求实时读取定义 → 改完立即生效） | `_check_api_enabled` |
| GET | `/api/ha_data_store/ext_manage` | 接口列表（含 `query_def` 原文与参数 schema） | `_check_db_edit_enabled` |
| POST | `/api/ha_data_store/ext_manage` | 新增 / 更新 / 启停（仅传 `name`+`enabled` 为启停） | 同上 |
| POST | `/api/ha_data_store/ext_manage/delete` | 删除接口 | 同上 |
| POST | `/api/ha_data_store/ext_manage/test` | 试运行（校验 + 执行，返回 SQL/列/行） | 同上 |

- 定义复用**查询构造器 v2** 结构，保存/试运行前经 `_bv2_validate` 严格校验（表/列/操作符/聚合白名单），执行复用 `_bv2_execute`
- 执行参数从 query/body 自动收集（排除 `key/_debug/limit/offset` 等控制参数）；停用接口返回 403；`?_debug=1` 附带 SQL
- **旧接口（`custom_routes`、`/query` 等）完全保持原样，不纳入本模块**；鉴权行为与现有接口一致（`api_enabled` 关闭时 `/ext/*` 同样不可用）

**db_viewer 前端**：
- 「API 工具」新增子页 **🧩 接口管理（新接口）**：列表（名称/显示名/数据表/状态/最大行数/更新时间）、新建、编辑、启停、删除、**试运行**（自动渲染动态参数 + 展示 SQL 与结果）
- 「API 地址生成器」查询类型下拉新增 **🧩 新接口（ext·免重启）** 分组：自动列出已启用接口，选中后按定义自动渲染参数输入框并生成 `…/ext/{name}?参数=值` 地址

**使用流程**：管理页新建 → 填 `query_def`（可在查询构造器配好后复制）→ 试运行 → 保存 → **立即生效，无需重启**；后续修改/停用/删除同样即时生效。改 `.py` 逻辑（如新增内建计算函数）仍需重启。

涉及 `const.py`、`__init__.py`、`http_api.py`、`db_viewer.html`；版本 → v3.6.5

## 2026-09-10 — v3.6.4 db_viewer 体验改进：标题显示版本号 + HTML 热重载

### 🔁 db_viewer.html 热重载（改 HTML 无需重启 HA）

- `_load_db_viewer_html` 保留内存缓存，但**每次请求检查文件指纹**（`st_mtime_ns` + `st_size`）
- 指纹变化 → 重新读取 `db_viewer.html` 并刷新缓存；未变化 → 直接沿用缓存（几乎零开销）
- **只修改 HTML 的布局 / CSS / JS 时，保存后刷新浏览器页面（建议 Ctrl+F5 强刷）即可生效，无需重启 HA**
- 修改任何 `.py` 后端文件仍需**手动重启 HA**（Python 模块已加载，热重载不安全）
- 读取失败（文件缺失等）记 error 日志，若有旧缓存则继续返回旧内容，避免整页不可用
- 注：本次改动本身在 `.py` 中，**需重启一次 HA 才能启用热重载**，之后改 HTML 均不需重启

### 🏷️ 管理面板标题栏右侧显示版本号

- 服务端在返回页面 HTML 时注入 `window.__HDS_VERSION__`（复用已有的 API Key 注入机制，无新增接口/额外请求）
- 标题 `<h1>` 改为 flex 布局，右侧渲染 `vX.Y.Z` 徽标（圆角、次要文字色）
- 版本取自 `const.py` 的 `VERSION`，升级版本后自动同步显示，无需手改前端

涉及 `http_api.py`、`db_viewer.html`；版本 → v3.6.4

## 2026-09-10 — v3.6.3 实体时段分布泛化：星期几/月份/几号 × 小时（entities_weekday_hours）

### 📊 API：`entity_hour_dist` 的多实体网格版 `entities_weekday_hours` 泛化

多实体 × 时间段 ×「分组维度 × 24 小时」聚合网格（N×24，hours 固定输出 0-23 全 24 项，无数据为 0，前端格子图直接渲染）：

- **`dim=week`**（默认）：按星期几聚合（0=周一…6=周日）→ 返回兼容结构 `weekdays[{weekday, weekday_name, totals, hours[24], devices?}]`
- **`dim=month`**：按月份 1–12 聚合（跨年同名月合并）→ `cells[{index:0-11, label:"1月".."12月", totals, hours[24], devices?}]`
- **`dim=day`**：按几号 1–31 聚合（跨月同号合并）→ `cells[{index:0-30, label:"1号".."31号", totals, hours[24], devices?}]`
- `group=0` 每格聚合所有实体；`group=1` 每格另含按实体分组 `devices[{entity_id,name,totals,hours[24]}]`
- 参数：`entities`(逗号分隔，空=全部) + 可选时间段 `start/end`（兼容 `date/month/year`）
- 行级口径与其它用电接口一致：逐小时精确拆分（跨天/跨月段归其自然时刻的维度格+小时）；**运行中设备**（`on_time` 非空、`off_time` 空）以当前时间为结束计长，用电 ①`now_kwh−on_power` ②固定功率 `W/1000×A` ③无来源该实体能源为 null；分时用电按方案 A（各小时实际秒数占比均摊整段，闭合守恒）
- `count`（开启次数）按开机时刻所在维度格+小时归属
- db_viewer「设备类」选项更名为「📊 多实体时段分布（周几/月/几号 × 小时）」，新增「分组维度」下拉

实现：`http_api.py` 新增通用 `compute_entities_grid_hours_sync`（week 兼容包装 `compute_entities_weekday_hours_sync` 保留）；`db_viewer.html` 更新选项/维度下拉/URL。版本 → v3.6.3

## 2026-09-09 — v3.6.2 新增多实体统计接口（entities_period_agg / entities_dates / entities_hours_agg）

### 📊 API：多实体统计接口

三个多实体接口，`entities`（逗号分隔，留空 = 全部）＋ 可选时间段（`start/end`，也兼容 `date/month/year`）；**运行中设备**（`on_time` 非空、`off_time` 空）均按当前时间为结束计长，用电 ① `now_kwh−on_power` ② 固定功率 `W/1000×A` ③ 无来源该实体能源为 null：

- **`entities_period_agg`**：多实体按 `bucket=day|month|year` 汇聚 + 顶层 `totals{count,duration_hour,energy_kwh}`，支持两种返回结构（`view` 参数）：
  - `view=entity`（默认）→ 「实体→日期」：每实体 `series[{key,count,duration_hour,energy_kwh}]`
  - `view=date` → 「日期→实体」：`dates[]` 唯一（`key`），每 key 含汇总 `{count,duration_hour,energy_kwh}` 与 `devices[]`（每实体含 `count/duration_hour/energy_kwh/running`，**running** 表示该段存在运行中设备）
- **`entities_dates`**：哪些日期有开启数据；`group=0` 合并返回 `{all_count, all_dates}`；`group=1` 按实体 `{entities:[{entity_id,name,count,dates}]}`
- **`entities_hours_agg`**：时段分布（精确小时拆分、用电方案 A 均摊）；`group=0` 返回全部合并 `merged`；`group=1` 另含每实体分组 `entities[]`
- 口径与既有 `entity_daily_*`/`entity_hour_*` 完全一致（共用 `_fetch_usage_rows`/`_row_running_usage`/`_usage_range_params`）
- db_viewer「设备类」新增 3 项（多实体输入 + 时间段 + 汇聚粒度/返回结构/分组方式）

涉及 `http_api.py`（三个多实体计算函数与调度）、`db_viewer.html`；版本 → v3.6.2

## 2026-09-09 — v3.6.1 report_entities 新增 card_type + 实体上报复合查询接口

### 🗂️ report_entities 表新增 card_type（卡片类型）字段

- `report_entities` 建表与旧表迁移新增 **`card_type`** 列（`TEXT NOT NULL DEFAULT ''`）
- POST `/api/ha_data_store/report`（全量重置写入）：解析并存储每实体 `card_type`（未传为空串，兼容旧前端）
- GET `/api/ha_data_store/report` 与 `/report/auto_entities` 返回增加 `card_type`
- 前端可上报字段现为：`entity_id/name/icon/room_name/source/rooms/entity_type/entity_device/entity_area/card_type`
- 说明：`sensor.ha_data_store_all_entities` 分组实体条目暂未输出 `card_type`（待前端联调时按需扩展）

### 🔎 API：实体上报复合查询 `GET /api/ha_data_store/report/search`

按字段对 `report_entities` 做多条件查询，精确/模糊可混合：

- 条件用重复参数表达，每条件一组：`f=<字段>&op=<eq|like>&v=<值>&c=<and|or>`（`c` 为与上一条件的连接，首条忽略）
- **支持 AND / OR 自由组合**：服务端按出现顺序从左到右加括号组合（如 `(A AND B) OR C`），无运算符优先级歧义
- 可选：`limit`/`offset` 分页；`order_by`(白名单字段)+`order=asc|desc`，默认 `room_name, entity_id`
- 支持字段：`entity_id/name/icon/room_name/source/rooms/entity_type/entity_device/entity_area/card_type`
- 返回：`{success, data:{count, limit, offset, order, order_dir, rows[]}}`（`count`=满足条件总条数，不受分页影响）
- db_viewer「实体上报」分组新增「🔎 实体上报复合查询」：动态条件行（字段+匹配方式+值+且/或，可增删）+ limit/offset + 排序

涉及 `__init__.py`（建表/迁移/视图注册）、`http_api.py`（`ReportSearchView` 等）；版本 → v3.6.1

## 2026-09-09 — v3.6.0 新增设备小时开启日期接口 `entity_hour_dates`

### 🆕 API：查询设备「几点开启的都有哪些日期」

`entity_hour_dates`：给定实体与小时（0–23），返回该小时发生过开启的所有日期（去重、升序）。

参数：
```
entity_id=xxx（必填）  hour=0-23（必填）
可选范围（不传 = 全部历史；优先级 start/end > date > month > year）：
  year=YYYY | month=YYYY-MM | date=YYYY-MM-DD | start=YYYY-MM-DD&end=YYYY-MM-DD
```

返回：
```
{ entity_id, hour, range, count 命中天数, dates: ["YYYY-MM-DD", ...] }
```

口径：
- 按 `on_time` 所在小时精确匹配 `hour`（含运行中设备，其 on_time 即开启时刻）
- 同一天多次开启只记 1 个日期，日期升序
- db_viewer「设备类」新增「📅 设备小时开启日期（几点开过）」，实体 + 小时下拉(0–23) + 可选时间范围

实现：`http_api.py` 新增 `compute_entity_hour_dates_sync` + `_query_entity_hour_dates` + 调度分支；`db_viewer.html` 增加小时选择行/选项/URL 逻辑。版本 → v3.6.0

## 2026-09-09 — v3.5.9 新增实体时段分布接口 `entity_hour_dist`

### 🆕 API：单实体时段分布（几点最常使用 / 分时用电）

`entity_hour_dist`：返回指定实体按「小时段」分布（0–23）。

参数：
```
entity_id=xxx（必填）
可选范围（不传 = 全部历史；优先级 start/end > date > month > year）：
  year=YYYY | month=YYYY-MM | date=YYYY-MM-DD | start=YYYY-MM-DD&end=YYYY-MM-DD
```

返回：
```
{
  entity_id, range,
  totals: { count 次数, duration_hour 时长合计, energy_kwh 用电合计 },
  hours:  [{ hour, count, duration_hour, energy_kwh }]   // 仅含有数据时段，hour 0-23
}
```

口径：
- **时长精确拆分**：每次运行区间 `[on_time, 结束]` 按跨越的小时切段、逐小时按真实秒数累计；已关闭用 `off_time`，**运行中**（`on_time` 非空且 `off_time` 空）以**当前时间**为结束
- **运行中用电**（先算整段再分摊）：① 有用电传感器 `now_kwh−on_power` ② 无传感器但配固定功率 `W/1000×A(小时)` ③ 两者皆无 → 不计电
- **分时用电（方案 A）**：整段用电按各小时段实际秒数占比**均摊**，闭合守恒；固定功率设备等价于功率×时长
- `count`（开启次数）按开机时刻所在小时归属
- 实体全程无任何用电来源 → `hours[].energy_kwh` / `totals.energy_kwh` 为 null
- db_viewer「设备类」新增「📊 实体时段分布（几点使用/分时用电）」，可选实体 + 时间范围（none 全部 / 月 / 年 / 单日 / 起止）

实现：`http_api.py` 新增 `compute_entity_hour_dist_sync` + `_query_entity_hour_dist` + 调度分支；`db_viewer.html` 增加选项/参数/URL 逻辑。版本 → v3.5.9

## 2026-09-08 — v3.5.8 新增实体/全实体按日用电接口（entity_daily_*、entities_daily_*）

### 🆕 API：实体按日用电（设备开关记录分组）

两个新接口，返回**单实体**按日聚合（每天一行），计算规则与 `whole_house_usage` 行级一致：

- `entity_daily_by_year`：`entity_id`(必填) + `year`(YYYY，必填) → 该实体指定年每日聚合
- `entity_daily_all`：`entity_id`(必填) → 该实体全部历史每日聚合

返回结构：
```
{
  entity_id, year,
  totals: { count 次数合计, duration_hour 时长合计(小时), energy_kwh 用电量合计(kWh) },
  rows:   [{ date, count, duration_hour, energy_kwh, running }]  // 日期倒序
}
```

### 🆕 API：全部实体按月按日（同一计算口径，两种结构自选）

- `entities_daily_flat`：`month`(YYYY-MM，必填) → 日×设备**扁平行**
  ```
  { month, totals: {count,duration_hour,energy_kwh}, rows: [{date, entity_id, name, count, duration_hour, energy_kwh, running}] }
  ```
- `entities_daily_by_day`：`month`(YYYY-MM，必填) → **按日分组**
  ```
  { month, totals: {...}, days: [{date,
      summary: { device_count 当日设备数量, count 次数, duration_hour 总时长, energy_kwh 总用电 },
      devices: [{entity_id, name, count, duration_hour, energy_kwh, running}] }] }
  ```

行级/合计口径（两类接口共用，均含 `totals` 全月合计）：
- 已关闭记录：时长取 `duration`(秒)，用电取 `energy_consumed`(kWh)
- **运行中记录**（`on_time` 非空且 `off_time` 空）：当日行标 `running=true`；时长 A = 当前时间 − `on_time`（以当前时间为关闭时间）
  - ① 有用电传感器（`now_kwh`/`on_power` 均有）→ 用电 = `now_kwh − on_power`(kWh)
  - ② 无传感器但配置固定功率（`entity_configs.power_rating`，W）→ 用电 = `功率(W)/1000 × A(小时)`
  - ③ 既无用电传感器也未配置功率 → `energy_kwh` 返回 null（空值，含 `totals.energy_kwh`）
- 运行中记录按 `on_time` 归入当日（系统 0 点自动分割，库内无跨天记录）
- 实体名称取 `name` 字段（缺失回退 entity_id）
- API 工具「查询类型 → 设备开关记录」新增选项：
  `📈 实体按日用电（指定年）` / `📈 实体按日用电（全部）`（仅需填实体 ID，指定年另需年份）/
  `📈 全部实体按月按日（平铺）` / `📈 全部实体按月按日（按日分组）`（仅需选月份）

实现：`http_api.py` 新增 `compute_entity_daily_usage_sync`（单实体按日）、`compute_month_entities_daily_sync`（全实体按月，供两种结构复用）+ 对应 QueryView 方法 + 4 个调度分支；`db_viewer.html` 增加下拉选项与参数控制。版本 → v3.5.8

## 2026-09-08 — v3.5.7 全部用电量实体顶层 total 改为合计节点（含房间汇总）

### ⚡ `sensor.ha_data_store_all_power` attributes.total 结构调整

- **状态值**不变：仍为用电实体个数（去重）
- **`attributes.total` 由 int 改为对象**（原实体个数并入 `total.count`）：
  - `count` → 用电实体个数（与原值一致）
  - `power` → 当前全屋功率合计(W)
  - `today` / `month` / `year` → 今日/本月/本年用电合计(kWh)，直接对内存中所有启用 meter 求和
  - `room[]` → 按房间汇总节点，每项含 `room/count/power/today/month/year`；room 为空的 meter 归入「未分配」；按今日用电降序排列
- **不受条数限制**：`total` 合计（power/today/month/year/room）与 `text.ha_data_store_ele_list` 无关，只有明细 `entities[]` 的 daylist/monthlist/yearlist 仍受该条数限制
- **功率合计规则**：`unavailable/unknown`/非数值/**负值**一律不参与合计，仅有效读数(≥0)计入
- **兼容提醒**：`total` 由 int 变对象属破坏性改动，读旧值(当整数)的前端需改用 `total.count`
- 涉及 `sensor.py`；版本 → v3.5.7

## 2026-09-07 — v3.5.6 新增全屋实体传感器 `sensor.ha_data_store_all_entities`

### 🆕 新增传感器 `sensor.ha_data_store_all_entities`（全屋实体）

数据源：`report_entities` 表（room-elves-card 等前端实体上报，POST `/api/ha_data_store/report` 全量重置）。

- **状态值** = 去重后的实体个数（按 `entity_id` 去重；一个实体跨多个节点仍计 1）
- **状态属性**（按 `entity_type` 分组）：
  - `nodes` → `{节点名: [实体...]}`；节点名 = 全表 `entity_type` 拆分后去重
  - `type_list` → 全部节点名（排序，与 `nodes` 键一致）
  - `total` / `total_rows` → 去重实体个数 / 表原始行数
  - `updated_at` → 最近一次更新时间
- 每个实体字段：`entity/name/icon/room_name/rooms/entity_type/entity_device/entity_area`
- **`entity_type` 支持多值**：以 `,`（兼容中文逗号）拆分，一个实体可同时归属多个节点（属正常现象）；同一节点内同实体只保留一份
- **更新规则**：report_entities 表变化才更新——每分钟轻量比对（总行数 + 最后上报时间），变化才重建并写状态；POST `/report` 成功后也会即时触发；表不变则不更新（无定时轮询、无状态抖动）
- 实体 ID 固定：注册表自动强制 `sensor.ha_data_store_all_entities`
- 涉及 `sensor.py`、`http_api.py`；版本 → v3.5.6

## 2026-09-07 — v3.5.5 全屋用电/用时：API 三级查询增强 + 汇总传感器

### 🏠 新增查询类型 `whole_house_usage`（总计→房间→设备 三级）

`GET /api/ha_data_store/query?type=whole_house_usage&year=YYYY[&month=YYYY-MM][&date=YYYY-MM-DD]`

- `year/month/date` 按最近一级精确匹配：`date > month > year`，只查年则返回全年
- 三级结构：`total` → `rooms[]` → `rooms[].devices[]`；返回字段：
  - 每级：`count`(开启次数)、`duration_hour`(小时，2 位)、`energy_kwh`(kWh，4 位)、`running_count`
  - **设备数量统计**：顶层 `total.device_count`（范围内参与统计的设备总数）；每个房间 `room.device_count`（该房间设备数）
  - **顶层 `room_names`**：单纯房间名列表（按 `rooms` 同一顺序），如 `["客厅","餐厅",...]`；`room_count` = 房间数
- **运行中设备纳入统计**（`on_time` 非空且 `off_time` 空/空串），设备项/房间/总计带 `running` 标记：
  - 时长 A = 当前时间 − `on_time`（未真正关闭，用当前时间作截止）
  - 有用电表（`now_kwh` 与 `on_power` 均有）：用电 = `now_kwh − on_power`（kWh）
  - 无电表但有固定功率（`power_rating`，来自 `entity_configs`）：用电 = `power_rating(W)/1000 × A(小时)`
  - 两者皆无/缺数据：用电按 0
- 已关闭记录保持原口径：时长取 `duration`(秒)，用电取 `energy_consumed`(kWh)
- API 工具「设备类」新增分组「🏠 全屋用电/用时（年/月/日）」

### 🆕 新增传感器 `sensor.ha_data_store_all_room_usage`（全屋用电/用时汇总）

- **状态值** = 今日节点 `total.energy_kwh`（kWh，2 位小数），单位 kWh
- **状态属性** = 三个三级节点（与 `whole_house_usage` 同构）：
  - `yearly` → 本年（`period`=年份）
  - `monthly` → 本月（`period`=YYYY-MM）
  - `daily` → 今日（`period`=YYYY-MM-DD）
  - 每个节点均为 `{scope, period, total, rooms, room_count, room_names}`
  - `generated_at` → 本次计算时间（本地时区）
- **每 1 分钟刷新一次**；三个节点共用同一计算时刻，保证运行中设备口径一致
- 实体 ID 固定，注册表自动强制 `sensor.ha_data_store_all_room_usage`

### 🔧 内部重构

- `http_api.py`：`_query_whole_house_usage` 计算逻辑抽为模块级函数 `compute_whole_house_usage_sync(db_path, year, month, date, now_dt)`，HTTP 接口与传感器共用同一口径，后续增强一处生效
- 涉及 `http_api.py`、`sensor.py`、`const.py` / `manifest.json`；版本 → v3.5.5

## 2026-09-06 — v3.5.4 全部用电量实体支持列表条数设置（text.ha_data_store_ele_list）

- 新增文本设置实体 **`text.ha_data_store_ele_list`**（用电计量列表条数）：状态值格式 **“日,月,年”**，如 `5,3,4` 表示 daylist 显示 5 条、monthlist 显示 3 条、yearlist 显示 4 条
  - 默认值 `3,3,3`；仅后端校验格式（`^\d+,\d+,\d+$`），非法输入拒绝写入；读取方解析失败一律回退 `3,3,3`
  - 归入主设备「HA数据统一存储系统」
- **`sensor.ha_data_store_all_power`** 每个用电实体明细新增：
  - `period`：`daily | monthly | yearly`（与三个用电实体自身的 period 一致）
  - 对应列表：daily→`daylist`、monthly→`monthlist`、yearly→`yearlist`（升序保留最近 N 条；条数为 0 则不显示）
  - 列表条数实时取自 `text.ha_data_store_ele_list`
- 监听 `state_changed`：`text.ha_data_store_ele_list` 值变化 → 立即刷新 `all_power`（失败仅记录日志）
- **三个用电实体自身的 `daylist/monthlist/yearlist` 保持全量，不受限制**（老数据源不受影响）
- 涉及 `text.py`（新增 `EleListSettingText`）、`sensor.py`（`PowerAllSensor` 增加 `_ele_limits`/列表注入/监听）；版本 → v3.5.4

## 2026-09-06 — v3.5.3 新增数据库压缩按钮实体

- 新增按钮实体 **`button.ha_data_store_db_compress`**（数据库压缩）：
  - 点击即对集成 SQLite 数据库执行 `VACUUM` 压缩，操作放入 executor 执行，不阻塞事件循环
  - 压缩前先 `PRAGMA journal_mode=DELETE`，与「数据库维护」页面的压缩逻辑一致
  - 点击完成后刷新以下状态属性：
    | 属性 | 说明 | 示例 |
    |---|---|---|
    | `压缩前大小` | VACUUM 前数据库文件大小 | `12.34 MB` |
    | `压缩后大小` | VACUUM 后文件大小 | `5.00 MB` |
    | `压缩时间` | 本次压缩完成时间 | `2026-09-06 10:20:30` |
  - 实体归入主设备「HA数据统一存储系统」；压缩失败仅记录日志，不写入状态
- 涉及 `button.py`（新增 `DatabaseCompressButton` / `_fmt_size`）；版本 → v3.5.3

## 2026-09-05 — v3.5.2 操作记录上报链路幂等化（不丢不重）

### 🎯 背景

room-elves-card 前端埋点操作记录上报，偶发“部分操作未入库”。可能原因：

- 前端防抖窗口内关闭页面 / `sendBeacon` 无响应投递（无法确认服务端是否真的入库）
- 后端 SQLite 瞬时被其它采集任务锁库，写入失败即丢
- 页面关闭后本地暂存未能在下次打开时自动补发

### ✅ 后端改进

1. **`user_actions` 表新增幂等键 `op_id`（= 前端记录 `_id`）**
   - 建表/启动迁移自动补充 `op_id TEXT NOT NULL DEFAULT ''`；
   - 新增**部分唯一索引** `idx_user_actions_op_id ... WHERE op_id <> ''`（历史 `op_id=''` 行不受影响）。
2. **上报写入改为幂等 `INSERT OR IGNORE`**
   - `ActionLogView` 直写兜底路径与 `action_log_inbox` 收件箱迁入路径均带 `op_id` 去重：同一操作重复上报只写一行，`rowcount<=0` 视为重复，不重复计数、不重复关联 `device_history`。
3. **抗锁**：action_log 相关 SQLite 连接加 `PRAGMA busy_timeout=20000` + `connect(timeout=20)`，锁冲突在连接层等待，不再瞬时抛错丢数据。
4. **新增 JSON 收件箱（双保险）** `action_log_inbox.py`
   - POST `/api/ha_data_store/action_log` 先原子写入本地 `{config_dir}/storage/action_log_inbox.json` 并快速返回成功；
   - 后台定时（每 3 秒，及入队即触发）把小批次数据迁入 `user_actions`，迁移成功才从收件箱删除；
   - 迁移失败保留收件箱并进入 15 秒冷却后重试；HA 重启启动自动加载遗留文件补迁；损坏文件自动改名 `.bak` 备份；
   - 迁移成功后执行 `device_history` 用户关联与常用设备 `user_actions_sensor` 刷新（原在请求路径内，改到后台批量做，POST 响应更快）；
   - **后台搬运用 `async_track_time_interval` 定时回调驱动**（而非常驻无限 asyncio 任务），避免 HA 启动/bootstrap 等待超时、以及关停/重启被卡住。

### 🤝 前端协同（room-elves-card `action-log.js`）

- 上报节奏：操作停止约 5s → 约 1s（体感立即上报）；补上页面隐藏/关闭 `sendBeacon` 兜底、下次打开自动补报。
- 调试信息：上报结果回写到每条记录 `上报` 字段（成功/失败），调试卡片列表按“已上报游标”显示，避免显示滞后。
- **关键语义**：`sendBeacon` 属无响应投递，改为**不再推进游标、不再回写成功**，记录保持“未确认”；下次打开自动重发。重发即使重复，也被后端 `op_id` 唯一索引吸收 → **不丢也不重**。

### 📁 文件改动

| 文件 | 改动 |
|------|------|
| `__init__.py` | `user_actions` 表补 `op_id` 列 + 部分唯一索引迁移；创建/启动 `ActionLogInbox`，`async_unload_entry` 停止后台任务 |
| `http_api.py` | `ActionLogView.post` 优先入收件箱快速返回；直写兜底路径改 `INSERT OR IGNORE`（含 `op_id`），连接加 `busy_timeout` |
| `action_log_inbox.py` | 新增：JSON 收件箱 + 后台迁库（`busy_timeout`/锁冲突重试/冷却/重启补迁/`op_id` 幂等） |
| `const.py` / `manifest.json` | VERSION → 3.5.2 |

> **升级提示**：需重启 Home Assistant 完成建表迁移与收件箱补迁。重启后历史“未确认”记录会自动补发，重复数据由 `op_id` 唯一索引吸收。

## 2026-09-05 — v3.5.1 设备历史查询支持 start/end 时间区间
- 修复 `type=device_history` / `device_summary` 忽略 `start`/`end` 参数的问题：此前传 `start=YYYY-MM-DD&end=YYYY-MM-DD` 会落到"全部记录"兜底分支，现按 `on_time` 区间过滤并返回区间内汇总
- 支持三种形态：仅 `start`、仅 `end`、`start+end`；区间优先于 `date/month/year`
- 涉及 `http_api.py`（`_query_device_history` / `_query_device_summary`，新增 `_build_on_time_range` / `_calc_device_summary_by_range`）；版本 → v3.5.1

## 2026-09-05 — v3.5.0 可视化查询构造器 + 数据库新建表 + 自定义路由发布开关

### 🧩 可视化查询构造器（直观版，无需手写 SQL）
「API 工具」新增 **查询构造器** 子页，按直线流程配置一个可复用查询接口：
1. **选数据表**（目录按数据类别分组并显示行数）→ 可勾选返回字段（默认全部）
2. **配置动态参数**（即“运行时可传值的过滤条件”）：每行选字段 + 比较方式 + 值来源
   - 文本/ID：`=`；数值：`> ≥ < ≤`；文本类还支持 **模糊 LIKE / 多值 IN**、`为空`
   - 时间/日期字段内置 **「时间段(起,止)」单行 between**（自动生成 `start_time`/`end_time` 两参数），并带独立「时间字段」下拉辅助选列
   - 值可“前端传参”（动态）或“固定值”；动态参数填默认值/必填/说明
3. **排序字段 + 方向**
4. **汇总信息**：可选返回总条数 count，以及数值列的 合计/平均/最大/最小
5. **试运行并发布**：试运行结果表 + 生成的 SQL 预览，一键保存为路由
- 发布后调用：`GET /api/ha_data_store/custom/{route_path}?key=xx&参数=值`
  - 支持按参数类型动态传值；**可选参数缺省自动跳过该过滤，必填缺省返回 400**
  - 响应默认返回业务字段 `data/count/summary`，**不再夹带 SQL**；需要排查时加 `&_debug=1` 附带 `sql/columns`
- 定义以结构化 `query_def` 存入 `custom_routes` 表，**随数据库复制迁移不丢失**

### ➕ 数据库浏览「新建表」
- 「数据库浏览」工具栏新增 **➕ 新建表**：表名 + 可选自增主键 id + 可视化行式字段定义（类型 TEXT/INTEGER/REAL/NUMERIC/BOOLEAN/BLOB/DATE/DATETIME + 主键 + 默认值）
- 后端白名单校验（表名/字段名正则、类型枚举、表重名拒绝、主键约束）
- 表可见性策略从“白名单”改为**黑名单**：自建表即时出现在用户表下拉，并可直接被查询构造器使用

### 🔌 自定义路由能力增强（兼容旧手写 SQL）
- `custom_routes` 新增 `query_def / source / enabled / max_rows / param_schema` 列（启动幂等迁移）
- 路由列表显示**来源**（🧩 构造器 / ✍️ 手写）与**状态**（●启用 ○停用），支持一键启停；停用的路由外部调用返回 403
- **SQL 预览对 API Key 隐藏**：`GET /routes` 对外只返回 path/描述/状态等，完整定义仅 db_viewer 登录会话可见
- rev2 构造定义采用**运行时动态编译**（白名单表/字段/操作符/聚合校验 + 全参数绑定），手写 SQL 路由照旧可用；无 LIMIT 的手写 SQL 自动套用 `max_rows` 上限

### 版本
| 文件 | 改动 |
|------|------|
| `http_api.py` | 查询目录/试运行/新建表/路由增删改查与启停接口；rev2 运行时编译执行（动态过滤/汇总/默认值/多值 IN json_each）；`GET /routes` 对外收敛 SQL |
| `__init__.py` | `custom_routes` 扩展列迁移；注册 `QueryCatalogView` / `RoutesTestView` / `CreateTableView` |
| `db_viewer.html` | 「🧩 查询构造器」直观版 UI/JS；「➕ 新建表」弹窗；自定义路由列表来源/状态列；表列表黑名单化；API 地址生成器按 param_schema 渲染参数 |
| `const.py` / `manifest.json` | VERSION → 3.5.0 |

## 2026-09-03 — v3.4.1 用电计量实体状态属性新增历史列表（daylist / monthlist / yearlist）

### ⚡ 日/月/年用电实体状态属性新增历史列表
- **日用电实体** `sensor.ha_data_store_{id}_daily_ele` 的 attributes 新增 **`daylist`**（每日用电量列表）
- **月用电实体** `sensor.ha_data_store_{id}_monthly_ele` 的 attributes 新增 **`monthlist`**（每月用电量列表）
- **年用电实体** `sensor.ha_data_store_{id}_yearly_ele` 的 attributes 新增 **`yearlist`**（每年用电量列表）

格式与口径（列表按日期升序）：
```
daylist    [{day:"2026-09-01", usage:1.45}, ...]    — 来自 power_energy_daily 日表，每天一条
monthlist  [{month:"2026-08",  usage:55.451}, ...]  — 日表按月前缀 SUM
yearlist   [{year:"2025",      usage:658.371}, ...] — 日表按年前缀 SUM
```
- 全量、**不设上限**；usage 保留 3 位小数，方便前端直接画图/统计；
- **无数据日期不占位**（仅 `kwh>0` 的行计入），列表紧凑无空洞。

### 🕘 实时性与性能
- **今天 / 本月 / 当年并入实时值**：列表尾项用内存实时累计覆盖（与实体自身 state 完全一致），不受日表 60s 落盘延迟影响；
- **缓存**：`PowerEnergyManager` 内缓存三份列表，在 **60s 落盘时 / 跨日重置时**从日表重建一次；实体刷新只读缓存 + O(1) 覆盖当前周期尾项，不重复全量查库；
- **重启安全**：列表仅是派生缓存，随时可从 `power_energy_daily` 重建；缓存缺失时首次实体刷新**懒重建兜底**，无数据丢失。

### 🗂️ 无破坏性变更
- 仅改 `power_energy.py`：不新增表、不改库结构、不动积分/落盘/查询逻辑；
- 已有 `power_energy_daily` 数据零改动，实体 ID 与 unique_id 不变；
- 重载/重启集成后，三个用电实体的状态属性即出现对应历史列表。

### 版本
| 文件 | 改动 |
|------|------|
| `power_energy.py` | 新增模块级 `_merge_period_list` + Manager `_rebuild_lists`（日表聚合重建缓存）/`period_list_of`（缓存 + 实时并入）；`_persist_all` 落盘后重建缓存；三个传感器 `refresh_from_mgr` 输出 `daylist`/`monthlist`/`yearlist` |
| `const.py` / `manifest.json` | VERSION → 3.4.1 |

## 🎉 v3.4.0 发布说明（2026-09-03）

### ⚡ 新增「用电计量」
- 前台登记功率实体（填功率实体/设备名/房间/单位 W·kW），自动生成 3 个累计实体：`sensor.ha_data_store_{id}_daily_ele`（日）、`..._monthly_ele`（月）、`..._yearly_ele`（年），状态即当前用电量，按天入库、10 秒采样积分，跨机迁移/重启自动恢复。
- 全部实体归入统一设备「用电计量」。
- 汇总实体 `sensor.ha_data_store_all_power`：状态 = 用电实体个数，属性含每个实体的 id/名称/图标/房间/设备。

### 🧹 设备清理
- 系统配置新增「设备清理」页：一键扫描/清理本集成下"无实体"的空设备，安全不误删主设备。

### 🗂️ 数据管理增强
- 设备类/传感器类/辅助元素/用电计量列表支持**全选 + 批量删除**。
- 「数据浏览器」新增 `power_energy_daily` 用户表。
- 「API 工具」新增「⚡ 用电计量」分组（日/月/年/日期段/最新查询）。
- 系统监控新增用电计量汇总卡片与明细。
- 实体跨机迁移（虚拟设备/辅助元素）操作均写本地日志。

### 🐛 修复
- 修复建表常量未导入导致 `no such table` / 数据库迁移异常。
- 修复平台实体在子线程注册导致的启动报错。
- 修复新增登记不被采样统计的问题（统一使用全局实例）。

## 2026-09-03 — v3.4.0 功率→用电计量：登记功率实体自动生成日/月/年用电量实体

### ⚡ 用电计量模块
- 需求：根据功率自动计算日/月/年用电量并入库，支持自定义查询。
- 在 db_viewer「系统配置 → ⚡ 用电计量」子页登记功率实体（填：功率实体 ID、设备名称、房间、ID 段、单位 W/kW），保存后即时生成 3 个固定 ID 传感器：
  - `sensor.ha_data_store_{id}_daily_ele`    今日累计 (kWh)
  - `sensor.ha_data_store_{id}_monthly_ele`  本月累计（由日数据实时聚合）
  - `sensor.ha_data_store_{id}_yearly_ele`   本年累计（由日数据实时聚合）
- **`power_energy.py`**（新增模块）：`PowerEnergyManager` + 3 个传感器类。
  - 每 10 秒采样功率实体当前功率，按「时间差 × 功率」积分（功率单位支持 W/kW，登记单位优先、其次自动读实体 unit_of_measurement）；
  - `unavailable/unknown` 不累计并重置基线；采样间隔 >5 分钟丢弃该空窗（防 HA 停机误算）；
  - 每天一条落盘 `power_energy_daily`（每 60 秒内存累计写库，含跨日自动分账）；月/年不建表，由当日行 + 历史日 SUM 实时得出；
  - 重启自动恢复登记实体，内存累计从当日已落盘值续算；
  - 卸载前自动落盘。
- **`__init__.py`**：新增 `power_meter_configs`、`power_energy_daily` 建表；启动/卸载接入 `PowerEnergyManager`；注册 PowerEnergyView。
- **`http_api.py`**：`PowerEnergyView`（GET 查询 `type=configs|query` + `kind=daily|monthly|yearly|range|latest`，支持 entity_id/room/date/month/year/start/end 过滤；POST 登记/删除，含写日志）。
- **`db_viewer.html`**：新增「⚡ 用电计量」子页 —— 登记表单（含单位下拉）、已登记列表（可删）、用电量查询（最新/某日/某月/某年/日期段 + 房间筛选）。
- **`const.py`**：新增 2 个表名常量；**`manifest.json`** 版本 3.4.0。

### 🔧 v3.4.0 增强与修复（同日追加）
- **建表兜底**：修复 `_init_database` 引用 `TABLE_POWER_METER_CONFIGS`/`TABLE_POWER_ENERGY_DAILY` 未 import 导致 NameError；`power_energy.py` 新增 `ensure_tables()`/`_open_db()`，所有 DB 入口自动建表；http_api 查询前 ensure。
- **启动注册修复**：`async_start()` 不再把 `restore_all()` 放 executor（平台 add_cb 必须在事件循环线程），消除 `loop is not the running loop` / coroutine never awaited。
- **实体命名**：三个用电实体名称改为 `{device_name} 日用电 / 月用电 / 年用电`（不再都叫 device_name）。
- **kWh 精度**：实体值 `round(_,3)`，落盘 `power_energy_daily.kwh` 保留 3 位小数。
- **统一设备**：所有用电计量实体并入同一 HA 设备「用电计量」（原每登记一设备）。
- **统计传感器**：新增 `sensor.ha_data_store_all_power`（全部用电量统计，固定 ID）：状态 = 用电实体个数，attributes `entities[]` = 每个实体的 entity_id/name/icon/room/device/power_entity，30s 刷新。
- **全局实例修复**：PowerEnergyView POST 改用全局 `power_energy_manager`，新增登记立即可被采样与 all_power 统计（此前临时实例导致新增不被感知）。
- **数据浏览器**：`power_energy_daily` 加入用户表（默认可见）；`power_meter_configs` 不作为用户表。
- **API 工具**：查询类型新增「⚡ 用电计量」分组（登记列表/最新/某日/某月/某年/日期段），拼装 `/api/ha_data_store/power_energy` URL。
- **系统监控**：新增 ⚡ 用电计量汇总卡片与明细表 + 用电计量 sub-tab 角标；「系统配置」各列表（设备类/传感器类/辅助元素/用电计量）新增**全选 + 批量删除**。
- **🧹 设备清理**：新增 `DeviceCleanView`（GET 扫描/POST 清理，`/api/ha_data_store/devices/cleanup`），按 entity_registry device_id 计数判定"空设备"并排除 entry 主设备；删除前解除 config entry 关联、以最终是否仍在 registry 判定成功。UI 从系统监控移入「系统配置」新增「🧹 设备清理」sub-tab。

### 版本
| 文件 | 改动 |
|------|------|
| `power_energy.py` | 新增用电计量核心模块（积分/日表/3 实体/统一设备/ensure_tables） |
| `sensor.py` | 新增 `sensor.ha_data_store_all_power` 统计实体 |
| `http_api.py` | PowerEnergyView 查询/登记/删除（全局实例）+ DeviceCleanView 设备清理 |
| `__init__.py` | 建表 + 启停接入 + 注册 View + const import 修复 |
| `db_viewer.html` | ⚡ 用电计量子页 + 系统监控卡片 + 全选批量删除 + 🧹 设备清理 tab + 用户表 |
| `const.py` / `manifest.json` | VERSION → 3.4.0 |

## 2026-09-03 — v3.3.1 虚拟设备/辅助元素导出导入、实体跨机迁移与统计监控

> 本版本将 v3.2.0（虚拟设备导出/导入）与 v3.3.0（辅助元素功能）合并发布为 v3.3.1，包含自虚拟设备管理以来的一揽子新能力。

### 🔮 虚拟设备导出 / 导入（配置 + 状态）
- 背景：A 机器创建虚拟设备后复制数据库到 B 机器，常因 SQLite WAL 未合并、状态不随库迁移等导致 B 上实体缺失或失效。现提供受控的**配置 + 状态**导出导入，不动整库文件。
- **`virtual_devices.py`**：
  - `async_export_devices()`：配置从 DB（`virtual_devices` 表，恢复失败也不漏）读取，状态从 HA 状态机（含 climate 附属温度传感器）采集，组装 `{schema_version, devices:[{config, state_snapshot}]}`；
  - `async_import_devices(payload, mode)`：逐条校验并重建（复用 `create_device`）；`mode=skip` 跳过正在运行的相同实体，`mode=overwrite` 先删旧再建；返回 `{imported, skipped, failed[]}`；
  - `_apply_snapshot_fields()` + `_MEDIA_ATTR_MAP`：按 13 种设备类型把 `{state, attributes}` 快照回填实体内部 `_attr_*` 字段；状态回填在实体注册完成后异步执行（`_flush_snapshots`），写回后进入 HA restore_state，**目标机后续重启状态仍保持**。
- **`http_api.py`**：新增 `VirtualDeviceExportView`（GET `/api/ha_data_store/virtual_device/export`）、`VirtualDeviceImportView`（POST `/api/ha_data_store/virtual_device/import`，body `{mode, devices}`，受 db_edit 开关保护）。
- **`__init__.py`**：注册上述两个 View。
- **`db_viewer.html`**：🔮 虚拟设备页新增「导出 / 导入」区——导出下载 JSON、导入文件可勾选"覆盖同名"，完成后提示统计并自动刷新列表。

### 🧩 辅助元素功能（原生 HA helper 跨机迁移为本集成自管实体）
- 背景：HA 原生辅助元素（`input_*`/`counter`/`binary_sensor`）的配置存于 HA `.storage`，无法像虚拟设备那样由本集成直接管理。现提供「辅助元素」独立功能：A 机扫描原生 helper → 导出 JSON（配置 + 状态）→ B 机导入 → 转换为**本集成自管实体**（RestoreEntity，无需重启，状态自动回填并在重启后保持）。
- **域映射（前缀变化，后段保留原名）**：

  | 源 | 目标 | 保留 |
  |------|------|------|
  | `input_boolean` | `switch` | icon/name + on/off |
  | `input_number` | `number` | min/max/step/unit + 当前值 |
  | `counter` | `number` | min/max/step/initial + 计数 |
  | `input_select` | `select` | options + 当前选项 |
  | `input_button` | `button` | 配置（无状态） |
  | `input_text` | `text` | 文本值 |
  | `binary_sensor` | `binary_sensor` | on/off |

- **`helper_entities.py`**（新增模块）：
  - 7 个目标域实体类 `HelperSwitch/HelperBinarySensor/HelperNumber/HelperSelect/HelperButton/HelperText`（均 RestoreEntity，text 域依赖 HA text 组件）；
  - `HelperManager`：create/delete/落库(`helper_entities` 表)/启动恢复/导出(配置+状态快照)/导入(skip|overwrite，含冲突保护：其它来源同名真实实体不可覆盖)/延迟状态回填；
  - `async_scan_native_helpers()`：扫描原生 helper（input_boolean/input_number/counter/input_select/input_button/input_text，可加 binary_sensor），从 attributes 提取 min/max/step/options/icon 等参数。
- **平台补丁**：`button.py` 补 `async_add_button` 回调；新增 `text.py` 平台文件；`PLATFORMS` 加入 `"text"`。
- **`http_api.py`**：新增 `HelperScanView`（GET `/api/ha_data_store/helper/scan`）、`HelperExportView`（GET `/helper/export`）、`HelperImportView`（POST `/helper/import`）、`HelperView`（GET/**POST**/DELETE `/helper`，POST 支持前台新建）。
- **`__init__.py`**：新增 `helper_entities` 建表、启动恢复任务、注册 4 个 View。
- **`db_viewer.html`**：系统配置页新增「🧩 辅助元素」子页 —— 已导入列表(可删)、扫描并导出(可含 binary_sensor)、导出已导入项、导入文件(可覆盖同名)、**🆕 前台新建辅助元素**（选择类型/填写 ID/名称/图标/初始状态及参数，即时创建，无需重启）；新增 🧩 辅助元素 sub-tab 角标；系统监控页新增 🧩 辅助元素汇总卡片与明细表格。

### 📊 新增 `sensor.ha_data_store_helper`（辅助元素统计）
- 状态值 = 当前辅助元素个数；
- 状态属性 `entities[]` = 每个辅助元素的明细（entity_id/name/icon/source_type/source_entity_id），按 entity_id 去重并按名称排序；
- 强制固定实体 ID：`sensor.ha_data_store_helper`（迁移旧 ID 逻辑与 `ha_data_store_automation` 一致）；
- 每 30 秒自动刷新；新增 translations（zh-Hans/en `helper_summary`）。

### 🧾 操作日志
- 虚拟设备：创建（POST）、删除（DELETE）、导出（GET /export）、导入（POST /import）成功时写入本地日志文件；
- 辅助元素：扫描（GET /scan）、导出（GET /export）、导入（POST /import）、新建（POST）、删除（DELETE）成功时写入本地日志文件；
- 均含 key 实体/数量/mode 等明细，可在 db_viewer 日志查看器查看。

### 版本
| 文件 | 改动 |
|------|------|
| `virtual_devices.py` | 新增导出收集/状态快照应用/异步导入方法 |
| `helper_entities.py` | 新增辅助元素实体类 + HelperManager + 扫描函数 |
| `sensor.py` | 新增 `HelperSummarySensor` + 注册/固定 ID/30s 轮询 |
| `http_api.py` | 虚拟设备/辅助元素各 View + 操作写 `_log_local` |
| `__init__.py` | helper 建表、启动恢复、注册 Views、PLATFORMS 加 text |
| `button.py` / `text.py` | 补注册 `async_add_button` / 新增 text 平台 |
| `db_viewer.html` | 虚拟设备/辅助元素页 + 系统监控卡片 + 角标 |
| `const.py` / `manifest.json` | VERSION → 3.3.1 |

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
  - 内存：`mem_total_mb`；硬盘：`disk_total_mb`（仅统计 HA 运行盘——config 目录所在文件系统，不再累加所有分区，避免 ESXi 虚拟机/容器把宿主或多分区叠加偏大；`collect_usage` 磁盘已用率同样按运行盘口径）
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
