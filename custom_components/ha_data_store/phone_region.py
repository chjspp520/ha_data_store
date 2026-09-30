# -*- coding: utf-8 -*-
"""手机号 / 固话 归属地离线查询（phone2region 本地库）。

> 本模块移植自同仓库的 `shaobo_pocket_carrier` 集成（同一份数据格式与解析实现），
> 仅保留「本地查询」相关部分，去掉了通话/短信/流量记录的字段整理逻辑。

数据来源：https://github.com/ALI1416/phone2region (Apache-2.0)
本模块只做**本地查询**，不联网、不外发号码。

库格式（已实测确认，小端序；`.zdb` 实际是 ZIP 容器，内层 `phone2region.db`）：

    头部 20 字节: CRC32(4) + 版本号(4) + 记录区指针(4)
                  + 二级索引区指针(4) + 一级索引区指针(4)
    记录区      : [长度 1B][UTF-8 记录: 省|市|邮编|区号|运营商]
    二级索引区  : 2736 × int32（指向一级索引区的绝对偏移，每块固定 256 条）
    一级索引区  : N × [号段低 8 位 1B][记录区偏移 int32]

手机号查询: `key = 手机号前 7 位 - 1300000`；`key >> 8` 定位块，块内按
`low = key & 0xFF` 顺序扫描（条目按 low 升序，可提前退出）。
固话查询  : 区号表由记录区扫描得出（约 321 条地级市），如 `0914` -> 陕西 商洛。
"""
import io
import logging
import os
import re
import struct
import threading
import time
import zipfile
import zlib
from typing import Any, Dict, List, Optional, Tuple

_LOGGER = logging.getLogger(__name__)

# 号段索引基准：库内以 1300000 为起点，容量 700000（与官方 700000>>8 设计一致）
_PREFIX_BASE = 1300000
_PREFIX_LIMIT = 700000
_HEADER_SIZE = 20
_ZIP_MAGIC = b"PK\x03\x04"

# 库文件目录名（HA 配置目录下）与候选文件名（用户可手动替换）
DB_DIR_NAME = "ha_data_store"
DB_FILENAMES = ("phone2region.zdb", "phone2region.db")
# 集成自带的库文件（本目录 data/ 下）
BUNDLED_DB = os.path.join(os.path.dirname(__file__), "data", "phone2region.zdb")

# 下载镜像（按顺序尝试：GitHub raw -> jsDelivr -> 官方 CDN）
DOWNLOAD_URLS = (
    "https://raw.githubusercontent.com/ALI1416/phone2region/master/data/phone2region.zdb",
    "https://cdn.jsdelivr.net/gh/ALI1416/phone2region@master/data/phone2region.zdb",
    "https://www.404z.cn/files/phone2region/v2.4.0/data/phone2region.zdb",
)

# 自动更新间隔（天）
AUTO_UPDATE_INTERVAL_DAYS = 7

# 运营商取值标准化（库里为 电信/电信虚拟/联通/联通虚拟/移动/移动虚拟/广电/未知）
ISP_NAMES = {
    "电信": "中国电信",
    "电信虚拟": "中国电信（虚拟）",
    "联通": "中国联通",
    "联通虚拟": "中国联通（虚拟）",
    "移动": "中国移动",
    "移动虚拟": "中国移动（虚拟）",
    "广电": "中国广电",
}


class PhoneRegionError(Exception):
    """归属地库缺失 / 损坏 / 格式不符。"""


def digits_of(number: Any) -> str:
    """提取号码中的数字（去掉 +86 / 空格 / 连字符 等）。"""
    text = str(number or "").strip()
    if not text:
        return ""
    if text.startswith("+86"):
        text = text[3:]
    elif text.startswith("0086"):
        text = text[4:]
    return re.sub(r"\D", "", text)


# ---------------------------------------------------------------- 库文件定位
def user_db_dir(hass) -> str:
    """用户可手动更新的库文件目录（HA 配置目录下）。"""
    try:
        return hass.config.path(DB_DIR_NAME)
    except Exception:
        return ""


def user_db_path(hass) -> str:
    """用户目录下首选的目标文件路径（下载 / 手动更新都写这里）。"""
    directory = user_db_dir(hass)
    return os.path.join(directory, DB_FILENAMES[0]) if directory else ""


def candidate_paths(hass=None) -> List[str]:
    """候选库文件路径（用户文件优先，其次集成内置的兜底库）。

    额外兼容：把库放在集成自己的 `data/` 目录（本集成的默认位置）。
    """
    paths: List[str] = []
    directory = user_db_dir(hass) if hass is not None else ""
    if directory:
        paths.extend(os.path.join(directory, name) for name in DB_FILENAMES)
    paths.append(BUNDLED_DB)
    return paths


def resolve_db_path(hass=None) -> Optional[str]:
    """返回实际使用的库文件路径（都找不到时返回 None）。"""
    for path in candidate_paths(hass):
        if path and os.path.isfile(path) and os.path.getsize(path) > 1024:
            return path
    return None


# ---------------------------------------------------------------- 索引实现
class RegionIndex:
    """解析后的归属地索引（只读，可多线程并发查询）。"""

    __slots__ = (
        "raw",
        "version",
        "record_ptr",
        "vector2_ptr",
        "vector_ptr",
        "source",
        "_area_map",
        "_area_lock",
    )

    def __init__(self, raw: bytes, source: str = "") -> None:
        self.raw = raw
        self.source = source
        self._area_map: Optional[Dict[str, Tuple[str, str]]] = None
        self._area_lock = threading.Lock()

        if len(raw) < _HEADER_SIZE:
            raise PhoneRegionError("库文件过小，不是有效的归属地库")
        stored_crc, version, record_ptr, vector2_ptr, vector_ptr = struct.unpack_from(
            "<IIIII", raw, 0
        )
        if zlib.crc32(raw[4:]) & 0xFFFFFFFF != stored_crc:
            raise PhoneRegionError("库文件 CRC32 校验失败（文件可能损坏或未完整下载）")

        if not (
            _HEADER_SIZE <= record_ptr < vector2_ptr < vector_ptr < len(raw)
            and (vector_ptr - vector2_ptr) % 4 == 0
            and (len(raw) - vector_ptr) % 5 == 0
        ):
            raise PhoneRegionError("库文件索引区指针异常，格式不符")

        self.version = version
        self.record_ptr = record_ptr
        self.vector2_ptr = vector2_ptr
        self.vector_ptr = vector_ptr

    # ------------------------------------------------------------ 构造
    @classmethod
    def from_bytes(cls, data: bytes, source: str = "") -> "RegionIndex":
        """从 .zdb（ZIP 容器）或内层 .db 原始数据构建索引。"""
        if len(data) < _HEADER_SIZE:
            raise PhoneRegionError("库文件为空或过小")
        if data[:4] == _ZIP_MAGIC:
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as zf:
                    names = [n for n in zf.namelist() if not n.endswith("/")]
                    if not names:
                        raise PhoneRegionError("ZIP 内没有数据文件")
                    inner = zf.read(names[0])
            except PhoneRegionError:
                raise
            except Exception as err:
                raise PhoneRegionError(f"解压库文件失败: {err}") from err
            return cls(inner, source or names[0])
        return cls(data, source)

    @classmethod
    def load(cls, path: str) -> "RegionIndex":
        """从文件加载（自动处理 ZIP 容器）。"""
        try:
            with open(path, "rb") as fp:
                data = fp.read()
        except OSError as err:
            raise PhoneRegionError(f"读取库文件失败: {err}") from err
        return cls.from_bytes(data, source=path)

    @property
    def version_text(self) -> str:
        v = str(self.version)
        return f"{v[:4]}-{v[4:6]}-{v[6:8]}" if len(v) == 8 and v.isdigit() else v

    # ------------------------------------------------------------ 查询
    def _record_at(self, offset: int) -> str:
        length = self.raw[offset]
        return self.raw[offset + 1: offset + 1 + length].decode("utf-8", "replace")

    def _mobile_record(self, digits: str) -> Optional[str]:
        key = int(digits[:7]) - _PREFIX_BASE
        if key < 0 or key >= _PREFIX_LIMIT:
            return None
        block = self.vector2_ptr + (key >> 8) * 4
        if block + 4 > len(self.raw):
            return None
        cursor = struct.unpack_from("<I", self.raw, block)[0]
        low = key & 0xFF
        while self.vector_ptr <= cursor + 5 <= len(self.raw):
            number = self.raw[cursor]
            if number == low:
                record_ptr = struct.unpack_from("<I", self.raw, cursor + 1)[0]
                if self.record_ptr <= record_ptr < self.vector2_ptr:
                    return self._record_at(record_ptr)
                return None
            if number > low:
                return None
            cursor += 5
        return None

    def area_codes(self) -> Dict[str, Tuple[str, str]]:
        """固话区号 -> (省, 市)，由记录区扫描得出（首次调用时构建）。"""
        if self._area_map is not None:
            return self._area_map
        with self._area_lock:
            if self._area_map is not None:
                return self._area_map
            mapping: Dict[str, Tuple[str, str]] = {}
            cursor = self.record_ptr
            while cursor < self.vector2_ptr:
                try:
                    record = self._record_at(cursor)
                except Exception:
                    break
                parts = record.split("|")
                if len(parts) >= 5 and parts[3] and parts[3] not in mapping:
                    mapping[parts[3]] = (parts[0], parts[1])
                cursor += 1 + self.raw[cursor]
            self._area_map = mapping
            _LOGGER.debug("归属地库区号表构建完成: %d 条", len(mapping))
            return mapping

    def query(self, number: Any) -> Optional[Dict[str, str]]:
        """查询号码归属地。

        返回 {"province","city","area_code","isp","isp_raw","location"}；
        查不到（服务号 / 短号 / 国际号 / 未收录号段）返回 None。
        """
        digits = digits_of(number)
        if not digits:
            return None

        record: Optional[str] = None
        area_code = ""
        if len(digits) == 11 and digits.startswith("1"):
            record = self._mobile_record(digits)
        elif len(digits) >= 10 and digits.startswith("0"):
            mapping = self.area_codes()
            for length in (4, 3):
                code = digits[:length]
                if code in mapping:
                    province, city = mapping[code]
                    record = f"{province}|{city}||{code}|"
                    area_code = code
                    break
        if not record:
            return None

        parts = record.split("|")
        province = parts[0] if len(parts) > 0 else ""
        city = parts[1] if len(parts) > 1 else ""
        if len(parts) > 3 and parts[3]:
            area_code = parts[3]
        isp_raw = parts[4] if len(parts) > 4 else ""
        isp = ISP_NAMES.get(isp_raw, isp_raw)
        return {
            "province": province,
            "city": city,
            "area_code": area_code,
            "isp": isp,
            "isp_raw": isp_raw,
            "location": city or province,
        }

    def describe(self, number: Any) -> Tuple[str, str]:
        """返回 (归属地文本, 运营商文本)；查不到返回 ("", "")。"""
        info = self.query(number)
        if not info:
            return "", ""
        return info.get("location", ""), info.get("isp", "")


# ---------------------------------------------------------------- 索引缓存
_CACHE_LOCK = threading.Lock()
_CACHE: Dict[str, Any] = {"key": None, "index": None, "error": ""}
# 当前使用的库文件路径（由 async_prepare 解析：用户库优先，其次内置库）
_ACTIVE_PATH: Optional[str] = None


def active_path() -> Optional[str]:
    """当前使用的库文件路径（未解析时为内置库）。"""
    return _ACTIVE_PATH or (BUNDLED_DB if os.path.isfile(BUNDLED_DB) else None)


def set_active_path(path: Optional[str]) -> None:
    """设置当前库文件路径并使缓存失效。"""
    global _ACTIVE_PATH
    _ACTIVE_PATH = path
    invalidate_cache()


def _cache_key(path: str) -> Tuple[str, int, int]:
    try:
        stat = os.stat(path)
        return (path, int(stat.st_mtime_ns), int(stat.st_size))
    except OSError:
        return (path, 0, 0)


def get_index(force_reload: bool = False) -> Optional[RegionIndex]:
    """同步获取索引（带缓存，文件变化自动重载）；库缺失 / 损坏返回 None。

    不依赖 hass：路径由 async_prepare / set_active_path 提供，未提供时使用内置库。
    调用方通常在 executor 线程里直接调用，查询本身是纯内存操作。
    """
    path = active_path()
    if not path:
        return None

    key = _cache_key(path)
    with _CACHE_LOCK:
        if not force_reload and _CACHE["key"] == key and _CACHE["index"] is not None:
            return _CACHE["index"]
        try:
            index = RegionIndex.load(path)
        except PhoneRegionError as err:
            if _CACHE.get("error") != f"{key}:{err}":
                _LOGGER.warning("归属地库不可用 (%s): %s", path, err)
                _CACHE["error"] = f"{key}:{err}"
            _CACHE["key"] = None
            _CACHE["index"] = None
            return None
        _CACHE.update({"key": key, "index": index, "error": ""})
        _LOGGER.info(
            "归属地库已加载: 版本 %s (%s, %.0f KB)",
            index.version_text,
            "用户库" if path != BUNDLED_DB else "内置库",
            os.path.getsize(path) / 1024,
        )
        return index


async def async_prepare(hass, force_reload: bool = False) -> Optional[RegionIndex]:
    """异步准备索引：解析库路径并在执行器线程里加载（集成 setup 时调用一次）。"""

    def _load() -> Optional[RegionIndex]:
        set_active_path(resolve_db_path(hass))
        return get_index(force_reload)

    return await hass.async_add_executor_job(_load)


def invalidate_cache() -> None:
    """使缓存失效（库文件被替换 / 更新后调用）。"""
    with _CACHE_LOCK:
        _CACHE["key"] = None
        _CACHE["index"] = None
        _CACHE["error"] = ""


# ---------------------------------------------------------------- 下载更新
def _write_atomic(path: str, data: bytes) -> None:
    """原子写入（先写临时文件再替换，避免半截文件）。"""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "wb") as fp:
        fp.write(data)
        fp.flush()
        os.fsync(fp.fileno())
    os.replace(tmp, path)


def _download_urllib(url: str, timeout: int) -> Optional[bytes]:
    """无 aiohttp 时的兜底下载（在 executor 线程中调用）。"""
    import urllib.request

    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
        return resp.read()


async def async_download_db(hass, timeout: int = 60) -> Tuple[bool, str]:
    """从镜像下载最新归属地库到用户目录（校验 CRC / 格式后原子替换）。

    返回 (是否成功, 结果说明)。所有镜像失败时返回 False，用户仍可手动放置库文件。
    """
    target = user_db_path(hass)
    if not target:
        return False, "无法确定库文件目录（config 路径不可用）"

    session = None
    try:
        from homeassistant.helpers.aiohttp_client import async_get_clientsession

        session = async_get_clientsession(hass)
    except Exception as err:  # 极端情况下退回 urllib
        _LOGGER.debug("获取 aiohttp 会话失败，改用 urllib: %s", err)

    errors: List[str] = []
    for url in DOWNLOAD_URLS:
        data: Optional[bytes] = None
        try:
            if session is not None:
                import aiohttp

                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=timeout)
                ) as resp:
                    if resp.status != 200:
                        errors.append(f"{url} -> HTTP {resp.status}")
                        continue
                    data = await resp.read()
            else:
                data = await hass.async_add_executor_job(_download_urllib, url, timeout)
        except Exception as err:
            errors.append(f"{url} -> {err}")
            continue

        if not data:
            errors.append(f"{url} -> 空响应")
            continue

        try:
            index = RegionIndex.from_bytes(data, source=url)
        except PhoneRegionError as err:
            errors.append(f"{url} -> {err}")
            continue

        try:
            await hass.async_add_executor_job(_write_atomic, target, data)
        except Exception as err:
            errors.append(f"{url} -> 写入失败 {err}")
            continue

        # 新文件立即生效（重新解析路径 + 加载索引）
        await async_prepare(hass, force_reload=True)
        _LOGGER.info("归属地库更新成功: 版本 %s -> %s", index.version_text, target)
        return True, f"已更新归属地库（版本 {index.version_text}, {len(data) / 1024:.0f} KB）"

    detail = "; ".join(errors[:3]) if errors else "无可用镜像"
    _LOGGER.warning("归属地库更新失败: %s", detail)
    return False, f"更新失败：{detail}"


def db_age_days() -> Optional[float]:
    """当前库文件的年龄（天）；库不存在返回 None。

    直接以文件修改时间为准（下载或手动替换都会刷新），
    因此不需要额外持久化"上次更新时间"。
    """
    path = active_path()
    if not path or not os.path.isfile(path):
        return None
    try:
        return (time.time() - os.path.getmtime(path)) / 86400
    except OSError:
        return None


def db_status() -> Dict[str, Any]:
    """当前库状态（供实体属性 / API 展示，不依赖 hass）。"""
    path = active_path()
    if not path:
        return {
            "available": False, "source": "", "path": "",
            "version": "", "size_kb": 0, "age_days": None,
        }
    index = get_index()
    return {
        "available": index is not None,
        "source": ("用户库 (config/%s)" % DB_DIR_NAME) if path != BUNDLED_DB else "集成内置库",
        "path": path,
        "version": index.version_text if index else "",
        "size_kb": round(os.path.getsize(path) / 1024),
        "age_days": (round(db_age_days() or 0, 1) if db_age_days() is not None else None),
    }
