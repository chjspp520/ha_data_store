# -*- coding: utf-8 -*-
"""城市坐标查询（内置城市中心坐标表）。

> 本模块移植自同仓库的 `shaobo_pocket_carrier` 集成，逻辑保持一致。

数据来源：阿里 DataV `areas_v3` 分省 GeoJSON 的 `center` 字段汇总生成，仅供展示用途。

- `data/city_coordinates.json`：420 个城市/省级中心坐标（归一化地名 -> [经度, 纬度]）

用途：给通讯记录补充「对方归属地坐标」与「通话地坐标」（`"经度,纬度"` 字符串）。
纯本地读取，不联网。
"""
import json
import logging
import os
import re
import threading
from typing import Any, Dict, List, Optional

_LOGGER = logging.getLogger(__name__)

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
BUNDLED_COORDS = os.path.join(DATA_DIR, "city_coordinates.json")
# 用户可放在配置目录下覆盖内置文件（可选）
USER_DIR_NAME = "ha_data_store"

# 地名归一化：去行政后缀与民族修饰词（与生成坐标表时保持一致）
_ETHNIC_MULTI = (
    "维吾尔", "蒙古", "土家", "哈尼", "傈僳", "景颇", "朝鲜", "哈萨克", "柯尔克孜", "锡伯",
    "布依", "仡佬", "拉祜", "德昂", "阿昌", "普米", "独龙", "基诺", "门巴", "珞巴", "毛南",
    "塔吉克", "乌孜别克", "俄罗斯", "鄂温克", "鄂伦春", "赫哲", "达斡尔", "保安", "撒拉",
    "裕固", "东乡",
)
# 单字民族词必须带"族"才能剥离，否则会误伤地名（北京/天水/西藏）
_ETHNIC_SINGLE = ("回", "藏", "苗", "彝", "壮", "傣", "白", "侗", "瑶", "羌", "黎", "畲", "满", "水", "佤", "怒", "京", "土")
# 允许剥离的行政后缀。注意：**不要**加入"区/县/旗" —— 坐标表里本身就有
# "临高县 / 乐东黎族自治县 / 神农架林区" 这类键，剥掉反而会让它们查不到；
# 带区县后缀的地名（如 "西安市碑林区"）交给下面的回退匹配处理。
_SUFFIX = re.compile(r"(特别行政区|自治州|自治区|地区|盟|省|市)$")

_LOCK = threading.Lock()
_CACHE: Dict[str, Any] = {"coords": None, "source": ""}
# 地名 -> 命中的坐标表键（"" 表示确认查不到），避免回退匹配反复扫描
_RESOLVE_CACHE: Dict[str, str] = {}

# 这些名字本身就是完整地名/省级名称，不允许再剥离民族词
_PROTECTED = {"内蒙古", "西藏", "广西", "宁夏", "新疆", "北京", "南京", "天水"}

# 行政层级切分点：在这些词之后断开，得到 ["广东省", "深圳市", "南山区"] 这样的段
_SEGMENT_SPLIT = re.compile(r"(?<=特别行政区)|(?<=自治区)|(?<=自治州)|(?<=地区)|(?<=省)|(?<=市)|(?<=盟)")


def _strip_ethnic(text: str) -> str:
    """剥离结尾的民族修饰词。

    - 多字词可省略"族"（巴音郭楞蒙古自治州），单字词必须带"族"（回族）
    - 剥离后必须仍留有 ≥2 个字，否则跳过（避免"内蒙古"被剥成"内"）
    """
    if text in _PROTECTED:
        return text
    for token in _ETHNIC_MULTI:
        for suffix in (f"{token}族", token):
            if text.endswith(suffix) and len(text) - len(suffix) >= 2:
                return text[: -len(suffix)]
    for token in _ETHNIC_SINGLE:
        suffix = f"{token}族"
        if text.endswith(suffix) and len(text) - len(suffix) >= 2:
            return text[: -len(suffix)]
    return text


def normalize_place(name: Any) -> str:
    """归一化地名，用于与坐标表匹配。

    例：巴音郭楞蒙古自治州 -> 巴音郭楞；恩施土家族苗族自治州 -> 恩施；宝鸡市 -> 宝鸡

    注意：只剥离 省/市/自治区/自治州/地区/盟/特别行政区 与民族修饰词；
    "县/区/旗" 保留原样（坐标表里存在 "临高县 / 神农架林区" 这类键）。
    """
    text = str(name or "").strip()
    if not text:
        return ""
    previous = None
    while previous != text:
        previous = text
        text = _SUFFIX.sub("", text)
        text = _strip_ethnic(text)
    return text


def _candidate_paths(hass=None) -> List[str]:
    paths: List[str] = []
    if hass is not None:
        try:
            paths.append(hass.config.path(USER_DIR_NAME, "city_coordinates.json"))
        except Exception:
            pass
    paths.append(BUNDLED_COORDS)
    return paths


def load_coordinates(hass=None, force_reload: bool = False) -> Dict[str, List[float]]:
    """加载坐标表（用户文件优先，其次内置；带缓存）。"""
    with _LOCK:
        if _CACHE["coords"] is not None and not force_reload:
            return _CACHE["coords"]  # type: ignore[return-value]
        if force_reload:
            _RESOLVE_CACHE.clear()  # 坐标表可能已换，名称匹配结果一并失效
        for path in _candidate_paths(hass):
            if not path or not os.path.isfile(path):
                continue
            try:
                with open(path, encoding="utf-8") as fp:
                    payload = json.load(fp)
            except Exception as err:
                _LOGGER.warning("读取城市坐标表失败 (%s): %s", path, err)
                continue
            coords = payload.get("coordinates") if isinstance(payload, dict) else payload
            if not isinstance(coords, dict) or not coords:
                _LOGGER.warning("城市坐标表格式不符: %s", path)
                continue
            _CACHE["coords"] = coords
            _CACHE["source"] = path
            _LOGGER.info("城市坐标表已加载: %d 条 (%s)", len(coords), os.path.basename(path))
            return coords
        _CACHE["coords"] = {}
        _CACHE["source"] = ""
        return {}


async def async_prepare(hass) -> int:
    """异步加载坐标表（执行器线程读盘），返回条目数。"""
    coords = await hass.async_add_executor_job(lambda: load_coordinates(hass, True))
    return len(coords)


def matched_key(name: Any) -> str:
    """地名 -> 坐标表中的键（查不到返回 ""，结果缓存）。

    匹配顺序（逐个尝试，命中即止）：

    1. 整名归一化后直接查表："西安市" -> "西安"、"巴音郭楞蒙古自治州" -> "巴音郭楞"
    2. 按行政层级切段，**从右往左**取更具体的段：
       "广东省深圳市南山区" -> ["广东省","深圳市","南山区"] -> "南山区"查不到 -> "深圳" ✓
       "陕西省西安市" -> ["陕西省","西安市"] -> "西安" ✓
    3. 左侧逐字剥离（去掉省/市前缀后仍是城市名，剩余 ≥2 字）：
       "陕西西安" -> "西安"、"中国香港特别行政区" -> "香港" ✓

    刻意**不做**任意位置/右侧的子串匹配，避免误判：
    "北京路" 不会被算成 "北京"（步骤 2 只在行政词处切段，步骤 3 只去前缀）。
    """
    raw = str(name or "").strip()
    if not raw:
        return ""
    cached = _RESOLVE_CACHE.get(raw)
    if cached is not None:
        return cached

    key = normalize_place(raw)
    coords = load_coordinates()
    found = ""
    if key and key in coords:
        found = key
    elif key:
        # 2) 按行政层级切段，右侧（更具体）优先
        for segment in reversed(_SEGMENT_SPLIT.split(key)):
            candidate = normalize_place(segment)
            if candidate and candidate in coords:
                found = candidate
                break
        # 3) 左侧逐字剥离（剩余 ≥2 字）
        if not found:
            for cut in range(1, len(key) - 1):
                candidate = normalize_place(key[cut:])
                if candidate and candidate in coords:
                    found = candidate
                    break
    if found and found != key:
        _LOGGER.debug("地名坐标匹配: %r -> %r (回退匹配)", raw, found)
    _RESOLVE_CACHE[raw] = found
    return found


def coordinate_of(name: Any) -> Optional[List[float]]:
    """按地名取 [经度, 纬度]；查不到返回 None。"""
    key = matched_key(name)
    if not key:
        return None
    value = load_coordinates().get(key)
    if isinstance(value, (list, tuple)) and len(value) == 2:
        try:
            return [float(value[0]), float(value[1])]
        except (TypeError, ValueError):
            return None
    return None


def _format_number(value: float) -> str:
    """坐标数值转字符串：去掉多余的 0（108.9480 -> 108.948）。"""
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return text or "0"


def coordinate_text(name: Any) -> str:
    """按地名取 `"经度,纬度"` 字符串；查不到返回空字符串。

    例："西安" -> "108.948,34.2632"
    """
    value = coordinate_of(name)
    if not value:
        return ""
    return f"{_format_number(value[0])},{_format_number(value[1])}"


def status(hass=None) -> Dict[str, Any]:
    """坐标表状态（供实体属性 / API 展示）。"""
    coords = load_coordinates()
    return {
        "cities": len(coords),
        "source": _CACHE.get("source", ""),
    }
