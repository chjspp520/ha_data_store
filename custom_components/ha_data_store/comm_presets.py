# -*- coding: utf-8 -*-
"""通讯采集的**内置映射预设**（按实体 ID 后缀识别，开箱即用）。

针对同仓库 `shaobo_pocket_carrier` 集成产生的三类实体，内置「采集节点 + 字段映射 +
唯一键」，使它们无需手工配置即可采集。

=====================  ==================  ==========  ========================================
实体后缀               采集节点             唯一键       说明
=====================  ==================  ==========  ========================================
`*_calls`              `通话流水清单`        `call_time`  逐条通话流水
`*_sms`                `短信记录`            `datetime`   逐条短信明细
`*_traffic`            `上网会话清单`        `datetime`   逐条上网会话
=====================  ==================  ==========  ========================================

**优先级**：实体级配置（`entity_configs`）→ 内置预设（按实体 ID 后缀）→ 类型级配置
（`attr_type_defs`）。因此已手工配过的实体不受影响，新加的这三类实体默认就能采到数据。

**`my_number`（我方号码）** 由 `extract_phone(entity_id)` 从实体 ID 里的电话号码动态生成，
以 `=<号码>` 的固定值形式写进映射，例如 `sensor.17792405320_sms` → `=17792405320`。
`COMM_PRESETS` 里不带这一项（保持静态可比对），由 `detect_preset()` 运行时叠加。

字段映射里以 `=` 开头的键表示**固定值**（由 `_resolve_field_value` 处理）。
"""
import re
from typing import Any, Dict, Optional, Tuple

# 从实体 ID 末段提取电话号码：宽松取 7~15 位连续数字
# （兼容手机号 17792405320 与带区号的固话；实体 ID 里的其它数字段不会误取，
#   因为它要求是一段连续数字，而 `sensor.` 前缀与 `_sms` 后缀都在点/下划线之外）
_PHONE_RE = re.compile(r"(\d{7,15})")

# 每条预设：后缀 / 名称 / 采集节点 / 唯一键 / 去重窗口 / 字段映射（源字段 → 目标列）
COMM_PRESETS: Tuple[Dict[str, Any], ...] = (
    {
        "suffix": "_calls",
        "label": "通话记录",
        "array_path": "通话流水清单",
        "key_field": "call_time",
        "compare_limit": 1000,
        "field_mapping": {
            "phone_number": "party_number",
            "number_location": "party_place",
            "call_time": "time",
            "location": "location",
            # 通话的 "type" 是 呼叫/接听（方向），放进 msg_type；
            # 源字段 "call_type"（国内通话/漫游…）才是 call_type 列
            "type": "msg_type",
            "call_type": "call_type",
            "duration": "duration",
            "fee": "cost",
            "location_coordinate": "location_coordinate",
            "number_isp": "party_isp",
            "number_location_coordinate": "party_coordinate",
            "=语音": "channel",
        },
    },
    {
        "suffix": "_sms",
        "label": "短信记录",
        "array_path": "短信记录",
        "key_field": "datetime",
        "compare_limit": 1000,
        "field_mapping": {
            "phone_number": "party_number",
            "datetime": "time",
            # 短信的 "type" 是 发送/接收（方向）
            "type": "msg_type",
            "fee": "cost",
            "number_isp": "party_isp",
            "number_location_coordinate": "party_coordinate",
            "number_location": "party_place",
            "=短信": "channel",
        },
    },
    {
        "suffix": "_traffic",
        "label": "上网流量",
        "array_path": "上网会话清单",
        "key_field": "datetime",
        "compare_limit": 1000,
        "field_mapping": {
            # 流量没有对端号码：party_* 留空
            "datetime": "time",
            "duration_seconds": "duration",
            "fee": "cost",
            "volume_mb": "traffic_usage",
            "business_type": "traffic_type",
            "=流量": "channel",
        },
    },
)


def extract_phone(entity_id: Any) -> str:
    """从实体 ID 末段提取电话号码；提取不到返回空串。

    例：`sensor.17792405320_sms` → `17792405320`
    """
    tail = str(entity_id or "").strip().rsplit(".", 1)[-1]
    if not tail:
        return ""
    match = _PHONE_RE.search(tail)
    return match.group(1) if match else ""


def preset_field_mapping(entity_id: Any) -> Dict[str, str]:
    """预设的完整字段映射（含按实体 ID 动态生成的 `my_number`）。"""
    preset = detect_preset(entity_id)
    if preset is None:
        return {}
    mapping: Dict[str, str] = dict(preset["field_mapping"])
    phone = extract_phone(entity_id)
    if phone:
        # 固定值写法：`=<号码>` → 写入 my_number 列
        mapping[f"={phone}"] = "my_number"
    return mapping


def detect_preset(entity_id: Any) -> Optional[Dict[str, Any]]:
    """按实体 ID 后缀匹配内置预设；没有匹配返回 None。

    匹配规则：取实体 ID **末段**（最后一段，即去掉 `sensor.` 前缀后的部分）做后缀判断，
    避免误伤（例如 `sensor.sms_gateway_temperature` 不应命中 `_sms`）。
    """
    eid = str(entity_id or "").strip()
    if not eid:
        return None
    tail = eid.rsplit(".", 1)[-1].lower()
    for preset in COMM_PRESETS:
        if tail.endswith(preset["suffix"]):
            return preset
    return None


def preset_summary(entity_id: Any) -> Dict[str, Any]:
    """给前端用的预设摘要（没有匹配时 available=False）。"""
    preset = detect_preset(entity_id)
    if preset is None:
        return {"available": False}
    return {
        "available": True,
        "label": preset["label"],
        "suffix": preset["suffix"],
        "array_path": preset["array_path"],
        "key_field": preset["key_field"],
        "compare_limit": preset["compare_limit"],
        "field_mapping": preset_field_mapping(entity_id),
        "my_number": extract_phone(entity_id),
    }
