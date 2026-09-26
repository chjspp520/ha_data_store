"""整库备份 / 恢复。

设计要点
--------
1. **备份方式**：优先 `VACUUM INTO`（产物紧凑、无空闲页），失败回退 SQLite 官方
   在线备份 API `Connection.backup()`。两者都能在数据库被 HA 正常使用时取得一致快照，
   不需要停机，也不会把 `-wal` 的未合并数据丢掉。

2. **恢复不在运行期替换库**。运行期直接覆盖正在被写入的库，存在「半写状态」与
   「在途写入打进新库」两类风险。因此恢复采用**排队 + 启动时应用**：
     · `queue_restore_sync()` 校验备份文件后，复制为 `ha_data_store.db.pending_restore`
       并写下标记文件 `ha_data_store.db.restore_requested`；
     · 下次 HA 启动、任何连接都还没打开数据库时，`apply_pending_restore_sync()`
       先给现有库做一份快照，再 `os.replace()` 原子替换。
   好处是替换发生在「无人使用」的时刻，且失败也能靠快照回退。

3. **定时备份**由 `__init__` 的 10 分钟 tick 驱动，按「最近一次计划时刻」与
   `backup_last_at` 比较判断是否到期。这样运行期改计划不需要重新注册定时器，
   也不会因为 HA 中途重启而漏掉当天的备份。

4. 自动备份按 `backup_keep` 滚动保留；**手动备份与恢复前快照永不自动删除**
   （后者是恢复失败时的唯一退路）。

配置文件只保存在 `api_settings`（键值表），不新增数据库表 —— 备份模块本身要能在
数据库损坏时继续工作。

5. **网络共享（SMB / NFS）必须挂载后再填路径**。本模块是普通文件系统程序，
   无法直接读写 `smb://host/share` 这类协议地址；而把它当成路径交给 `os.path`
   会在 HA 配置目录里静默造出 `smb:/host/share` 垃圾目录（备份"成功"但没到 NAS）。
   因此 `save_settings_sync()` 会识别协议地址并直接报错，并在错误里给出挂载指引。
   挂载点填进来后，备份会**先在本地生成并校验，再搬到共享**，避免 SQLite 直接在
   网络盘上做 journal / 依赖文件锁，也避免网络中断在共享上留下半个损坏文件。
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from aiohttp import web
from homeassistant.core import HomeAssistant

from .const import DEFAULT_TIMEZONE, DOMAIN
from .http_api import _BaseDBView
from .logger import get_logger

_LOGGER = logging.getLogger(__name__)

# =========================================================================== #
#  常量                                                                        #
# =========================================================================== #
# 默认备份目录（相对 HA 配置目录，与 export_entities 的存放习惯保持一致）
DEFAULT_BACKUP_DIR = os.path.join("storage", "ha_data_store_backups")

# 待恢复文件 / 标记文件后缀（紧邻数据库，便于启动时定位）
PENDING_SUFFIX = ".pending_restore"
MARKER_SUFFIX = ".restore_requested"

# 调度检查周期（分钟）—— __init__ 用同一值注册 tick
TICK_MINUTES = 10

# 备份文件命名
FILE_PREFIX = "ha_data_store"
TAG_AUTO = "auto"
TAG_MANUAL = "manual"
TAG_PRERESTORE = "prerestore"
TAG_UPLOADED = "uploaded"
TAG_LABELS = {
    TAG_AUTO: "自动",
    TAG_MANUAL: "手动",
    TAG_PRERESTORE: "恢复前快照",
    TAG_UPLOADED: "外部导入",
}
# 末段 _N 是同一秒内重复备份时追加的序号（见 create_backup_sync），
# 必须一并匹配，否则这些备份会「存在但列表里看不到」，既无法下载也无法清理。
_FILE_RE = re.compile(
    r"^" + re.escape(FILE_PREFIX) + r"(?:_(?P<tag>auto|manual|prerestore|uploaded))?"
    r"(?:_(?P<stamp>\d{8}_\d{6}))?(?:_(?P<seq>\d+))?\.db$"
)

# api_settings 键
SKEY_ENABLED = "backup_enabled"
SKEY_SCHEDULE = "backup_schedule"
SKEY_HOUR = "backup_hour"
SKEY_WEEKDAY = "backup_weekday"
SKEY_KEEP = "backup_keep"
SKEY_DIR = "backup_dir"
SKEY_LAST_AT = "backup_last_at"

SCHEDULES = ("off", "hourly", "daily", "weekly")
SCHEDULE_LABELS = {
    "off": "关闭",
    "hourly": "每小时",
    "daily": "每天",
    "weekly": "每周",
}

DEFAULT_SETTINGS = {
    "enabled": False,
    "schedule": "daily",
    "hour": 3,
    "weekday": 0,          # 0 = 周一
    "keep": 10,
    "dir": DEFAULT_BACKUP_DIR,
    "last_at": "",
}

# 恢复时至少应存在的表（缺一说明不是本集成的库）
REQUIRED_TABLES = ("entity_configs", "device_history")

# 单文件大小上限校验用：小于该值基本不可能是有效 SQLite 库
MIN_VALID_SIZE = 8 * 1024

# 备份暂存文件：先在数据库所在目录生成，校验通过后再搬到备份目录
STAGING_SUFFIX = ".tmp"
STAGING_MAX_AGE = 6 * 3600  # 秒；超过该年龄的残留暂存文件会被清理


# =========================================================================== #
#  小工具                                                                      #
# =========================================================================== #
def _now_local(tz_offset: int | None = None) -> datetime:
    """返回本地（东八区）朴素时间，与库内时间字符串口径一致。"""
    offset = DEFAULT_TIMEZONE if tz_offset is None else tz_offset
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=offset)


def _fmt_dt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _parse_dt(text: str) -> datetime | None:
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text.strip()[:19], fmt)
        except ValueError:
            continue
    return None


def _human_size(num: int) -> str:
    size = float(num or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def _stamp(dt: datetime | None = None) -> str:
    return (dt or _now_local()).strftime("%Y%m%d_%H%M%S")


def _ro_uri(path: str) -> str:
    """构造 SQLite 只读 URI。

    必须用 URI 才能强制 `mode=ro`（否则校验损坏文件时可能产生写入）。
    Windows 盘符 / 反斜杠 / 中文与空格路径都要正确转义，故统一转成正斜杠并百分号编码。
    """
    raw = os.path.abspath(path).replace("\\", "/")
    if raw.startswith("//"):  # UNC：\\server\share\...
        return "file://" + quote(raw[2:].lstrip("/"), safe="/:") + "?mode=ro"
    return "file:///" + quote(raw.lstrip("/"), safe="/:") + "?mode=ro"


def tag_of(filename: str) -> str:
    """从文件名推断备份类型。"""
    m = _FILE_RE.match(filename)
    if not m:
        return TAG_MANUAL
    return m.group("tag") or TAG_MANUAL


def build_filename(tag: str, dt: datetime | None = None) -> str:
    if tag and tag != TAG_MANUAL:
        return f"{FILE_PREFIX}_{tag}_{_stamp(dt)}.db"
    return f"{FILE_PREFIX}_{_stamp(dt)}.db"


def is_safe_backup_name(name: str) -> bool:
    """备份文件名校验：禁止路径穿越与非法字符。"""
    if not name or name != os.path.basename(name):
        return False
    return bool(_FILE_RE.match(name))


# =========================================================================== #
#  设置读写（api_settings 键值表；表缺失时自行补建）                              #
# =========================================================================== #
def _ensure_settings_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS api_settings ("
        "skey TEXT PRIMARY KEY, svalue TEXT NOT NULL DEFAULT '')"
    )


def _read_raw_settings(conn: sqlite3.Connection) -> dict:
    _ensure_settings_table(conn)
    rows = conn.execute(
        "SELECT skey, svalue FROM api_settings WHERE skey LIKE 'backup_%'"
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def _to_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in ("1", "true", "on", "yes", "是", "启用")


def load_settings_sync(db_path: str, config_dir: str) -> dict:
    """读取备份设置（缺省值兜底 + 类型归一化）。数据库不可读时返回默认值。

    注意先判存在再连接：`sqlite3.connect()` 对不存在的路径会**创建空文件**，
    本函数会在启动早期被调用，绝不能凭空造出一个空库。
    """
    raw: dict = {}
    if not os.path.isfile(db_path):
        return _finalize_settings(raw, config_dir)
    try:
        conn = sqlite3.connect(db_path, timeout=10)
        try:
            raw = _read_raw_settings(conn)
        finally:
            conn.close()
    except Exception as exc:
        _LOGGER.warning("[backup] 读取备份设置失败，使用默认值: %s", exc)
    return _finalize_settings(raw, config_dir)


def _finalize_settings(raw: dict, config_dir: str) -> dict:
    """把原始键值对归一化成完整设置（缺省值兜底）。"""

    out = dict(DEFAULT_SETTINGS)
    out["enabled"] = _to_bool(raw.get(SKEY_ENABLED), DEFAULT_SETTINGS["enabled"])
    schedule = str(raw.get(SKEY_SCHEDULE) or DEFAULT_SETTINGS["schedule"]).strip().lower()
    out["schedule"] = schedule if schedule in SCHEDULES else DEFAULT_SETTINGS["schedule"]
    for key, skey, lo, hi in (
        ("hour", SKEY_HOUR, 0, 23),
        ("weekday", SKEY_WEEKDAY, 0, 6),
        ("keep", SKEY_KEEP, 1, 100),
    ):
        try:
            out[key] = max(lo, min(hi, int(raw.get(skey, DEFAULT_SETTINGS[key]))))
        except (TypeError, ValueError):
            out[key] = DEFAULT_SETTINGS[key]
    out["dir"] = str(raw.get(SKEY_DIR) or "").strip() or DEFAULT_SETTINGS["dir"]
    out["last_at"] = str(raw.get(SKEY_LAST_AT) or "")
    out["dir"] = resolve_backup_dir(config_dir, out["dir"])
    out["dir_is_default"] = os.path.normcase(out["dir"]) == os.path.normcase(
        resolve_backup_dir(config_dir, DEFAULT_SETTINGS["dir"]))
    return out


def looks_like_url(text: str) -> bool:
    """判断是否是「协议地址」（smb://、smb:\\\\、nfs://、ftp:// 等）而不是文件系统路径。

    这类值绝不能直接交给 os.path / os.makedirs：Linux 下
    `smb://192.168.1.102/media` 会被当成相对路径，最终在 HA 配置目录里
    静默创建出 `smb:/192.168.1.102/media` 这样的垃圾目录 ——
    备份"成功"了却没到 NAS，属于最难排查的失败模式。

    放行 Windows UNC（\\\\server\\share 与 //server/share）和盘符（Z:\\）。
    """
    text = (text or "").strip()
    if not text:
        return False
    if text.startswith("\\\\") or text.startswith("//"):
        return False  # UNC / 正斜杠 UNC，是合法的 Windows 路径
    # 冒号后必须紧跟分隔符，才认定为协议地址：
    #   smb://host/share、nfs://host/export、smb:\\host\share（Windows 风格反斜杠）
    # 这样既覆盖各种写法，又不会把 "mnt:backup" 这类含冒号的普通目录名误判。
    match = re.match(r"^([A-Za-z][A-Za-z0-9+.\-]*):[\\/]", text)
    if not match:
        return False
    scheme = match.group(1)
    if len(scheme) == 1:
        return False  # Windows 盘符，如 Z:\backup
    return True


# 协议地址 -> 该协议在本机的挂载方式建议
_MOUNT_HINTS = {
    "smb": "HA OS / Supervised：设置 → 系统 → 存储 → 添加网络存储，填 SMB 共享与账号，"
           "挂载后把「用途」选成 media 或 share，再把这里的路径填成 /media/<名字> 或 /share/<名字>；"
           "HA Container / Core：在宿主机执行 "
           "mount -t cifs //192.168.1.102/media /mnt/ha_backups -o username=用户,password=密码,uid=1000",
    "cifs": "在宿主机挂载：mount -t cifs //192.168.1.102/media /mnt/ha_backups "
            "-o username=用户,password=密码,uid=1000",
    "nfs": "HA OS / Supervised：设置 → 系统 → 存储 → 添加网络存储（选 NFS）；"
           "或在宿主机执行 mount -t nfs 192.168.1.102:/export /mnt/ha_backups",
}


def save_settings_sync(db_path: str, config_dir: str, payload: dict) -> dict:
    """保存备份设置。返回归一化后的设置。dir 不合法会抛 ValueError。"""
    schedule = str(payload.get("schedule") or DEFAULT_SETTINGS["schedule"]).strip().lower()
    if schedule not in SCHEDULES:
        raise ValueError(f"不支持的备份周期: {schedule}")

    raw_dir = str(payload.get("dir") or "").strip() or DEFAULT_SETTINGS["dir"]
    if looks_like_url(raw_dir):
        scheme = re.match(r"^([A-Za-z][A-Za-z0-9+.\-]*)", raw_dir).group(1).lower()
        hint = _MOUNT_HINTS.get(scheme) or (
            "请先在操作系统里把它挂载成普通目录，再填挂载后的路径")
        raise ValueError(
            f"「{raw_dir}」是协议地址，不是文件系统路径，程序无法直接读写共享。\n"
            f"请先挂载再填挂载后的本地路径。{hint}"
        )

    target = resolve_backup_dir(config_dir, raw_dir)
    # 现场验证目录可用，避免保存后才在半夜备份时失败
    try:
        os.makedirs(target, exist_ok=True)
        probe = os.path.join(target, ".hds_write_test")
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
        os.remove(probe)
    except Exception as exc:
        raise ValueError(f"备份目录不可写: {target}（{exc}）") from exc

    def _int(key: str, lo: int, hi: int) -> int:
        try:
            return max(lo, min(hi, int(payload.get(key, DEFAULT_SETTINGS[key]))))
        except (TypeError, ValueError):
            return DEFAULT_SETTINGS[key]

    values = {
        SKEY_ENABLED: "1" if _to_bool(payload.get("enabled")) else "0",
        SKEY_SCHEDULE: schedule,
        SKEY_HOUR: str(_int("hour", 0, 23)),
        SKEY_WEEKDAY: str(_int("weekday", 0, 6)),
        SKEY_KEEP: str(_int("keep", 1, 100)),
        SKEY_DIR: raw_dir,
    }
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        _ensure_settings_table(conn)
        for skey, svalue in values.items():
            conn.execute(
                "INSERT INTO api_settings (skey, svalue) VALUES (?, ?) "
                "ON CONFLICT(skey) DO UPDATE SET svalue = excluded.svalue",
                (skey, svalue),
            )
        conn.commit()
    finally:
        conn.close()
    return load_settings_sync(db_path, config_dir)


def _set_last_at(db_path: str, text: str) -> None:
    try:
        conn = sqlite3.connect(db_path, timeout=30)
        try:
            _ensure_settings_table(conn)
            conn.execute(
                "INSERT INTO api_settings (skey, svalue) VALUES (?, ?) "
                "ON CONFLICT(skey) DO UPDATE SET svalue = excluded.svalue",
                (SKEY_LAST_AT, text),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        _LOGGER.warning("[backup] 记录上次备份时间失败: %s", exc)


def resolve_backup_dir(config_dir: str, raw: str) -> str:
    """把设置里的目录解析为绝对路径（相对路径按 HA 配置目录展开）。"""
    text = (raw or "").strip() or DEFAULT_BACKUP_DIR
    path = text if os.path.isabs(text) else os.path.join(config_dir, text)
    return os.path.normpath(path)


# =========================================================================== #
#  备份文件读写                                                                 #
# =========================================================================== #
def _copy_database(src_path: str, dst_path: str) -> None:
    """把 src 库复制为 dst（dst 必须不存在或可被覆盖）。

    优先 VACUUM INTO（产物紧凑），不支持或失败时回退在线备份 API。
    """
    if os.path.exists(dst_path):
        os.remove(dst_path)
    parent = os.path.dirname(dst_path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    src = sqlite3.connect(src_path, timeout=30)
    try:
        if sqlite3.sqlite_version_info >= (3, 27, 0):
            try:
                src.execute("VACUUM INTO ?", (dst_path,))
                if os.path.isfile(dst_path) and os.path.getsize(dst_path) > 0:
                    return
            except sqlite3.Error as exc:
                _LOGGER.debug("[backup] VACUUM INTO 不可用，回退在线备份: %s", exc)
        dst = sqlite3.connect(dst_path, timeout=30)
        try:
            src.backup(dst)
            dst.commit()
        finally:
            dst.close()
    finally:
        src.close()


def validate_backup_sync(path: str) -> tuple[bool, str]:
    """只读校验备份文件：是否为 SQLite 库 + 完整性 + 必需表。"""
    if not os.path.isfile(path):
        return False, "文件不存在"
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        return False, f"无法读取文件: {exc}"
    if size < MIN_VALID_SIZE:
        return False, f"文件过小（{_human_size(size)}），不像是有效的数据库"

    conn = None
    try:
        conn = sqlite3.connect(_ro_uri(path), uri=True, timeout=10)
        row = conn.execute("PRAGMA quick_check").fetchone()
        result = str(row[0]).strip().lower() if row else "unknown"
        if result != "ok":
            return False, f"完整性校验未通过: {row[0] if row else '未知'}"
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = [t for t in REQUIRED_TABLES if t not in names]
        if missing:
            return False, f"缺少本集成必需的表: {', '.join(missing)}"
        return True, ""
    except sqlite3.DatabaseError as exc:
        return False, f"不是有效的 SQLite 数据库: {exc}"
    except Exception as exc:
        return False, f"校验失败: {exc}"
    finally:
        if conn is not None:
            conn.close()


def _sweep_stale_staging(staging_dir: str) -> None:
    """清理本模块遗留的临时备份文件。

    正常路径下 finally 一定会删掉；只有进程被强杀才会残留。
    只删 6 小时前的文件，绝不会碰到正在进行中的那一个。
    """
    try:
        names = os.listdir(staging_dir)
    except OSError:
        return
    now = time.time()
    for name in names:
        if not (name.startswith("." + FILE_PREFIX) and name.endswith(STAGING_SUFFIX)):
            continue
        path = os.path.join(staging_dir, name)
        try:
            if now - os.stat(path).st_mtime > STAGING_MAX_AGE:
                os.remove(path)
                _LOGGER.info("[backup] 已清理残留临时备份 %s", name)
        except OSError:
            pass


def create_backup_sync(db_path: str, backup_dir: str, tag: str = TAG_MANUAL,
                       dt: datetime | None = None) -> dict:
    """生成一份备份，返回文件信息。

    先在**数据库所在目录（本地盘）**生成，校验通过后再搬到目标目录。
    这么做有两个原因（备份目录指向 NAS/共享挂载点时尤其重要）：

    1. SQLite 更倾向把目标当成一个正常数据库来写 —— `VACUUM INTO` 会在目标旁边
       建 journal 并依赖文件锁，在网络文件系统上并不可靠；
    2. 搬到目标目录的是**已经写完并校验过**的成品文件，网络中途断开也不会在共享上
       留下半个损坏的备份（否则它会被当成"有效备份"列出来）。
    """
    name = build_filename(tag, dt)
    path = os.path.join(backup_dir, name)
    # 极端情况下同一秒内重复触发：加序号避免覆盖
    seq = 1
    while os.path.exists(path):
        seq += 1
        base, ext = os.path.splitext(build_filename(tag, dt))
        path = os.path.join(backup_dir, f"{base}_{seq}{ext}")

    staging_dir = os.path.dirname(db_path) or backup_dir
    _sweep_stale_staging(staging_dir)
    staging = os.path.join(staging_dir, f".{name}{STAGING_SUFFIX}")
    try:
        _copy_database(db_path, staging)
        ok, msg = validate_backup_sync(staging)
        if not ok:
            raise RuntimeError(f"备份生成后校验失败：{msg}")
        os.makedirs(backup_dir, exist_ok=True)
        # 同一文件系统时 shutil.move 等价于 rename（零成本）；
        # 跨文件系统（共享挂载点）时是一次顺序写入。
        shutil.move(staging, path)
    finally:
        if os.path.exists(staging):
            try:
                os.remove(staging)
            except OSError:
                pass

    st = os.stat(path)
    return {
        "name": os.path.basename(path),
        "size": st.st_size,
        "size_h": _human_size(st.st_size),
        "created_at": _fmt_dt(datetime.fromtimestamp(st.st_mtime)),
        "tag": tag,
        "tag_label": TAG_LABELS.get(tag, tag),
    }


def list_backups_sync(backup_dir: str) -> list[dict]:
    """列出备份目录中的备份（按时间倒序）。"""
    if not os.path.isdir(backup_dir):
        return []
    out: list[dict] = []
    try:
        names = os.listdir(backup_dir)
    except OSError:
        return []
    for name in names:
        if not name.endswith(".db") or not is_safe_backup_name(name):
            continue
        path = os.path.join(backup_dir, name)
        try:
            st = os.stat(path)
        except OSError:
            continue
        tag = tag_of(name)
        out.append({
            "name": name,
            "size": st.st_size,
            "size_h": _human_size(st.st_size),
            "mtime": st.st_mtime,
            "created_at": _fmt_dt(datetime.fromtimestamp(st.st_mtime)),
            "tag": tag,
            "tag_label": TAG_LABELS.get(tag, tag),
            "auto": tag == TAG_AUTO,
            "protected": tag in (TAG_MANUAL, TAG_PRERESTORE),
        })
    out.sort(key=lambda item: item["mtime"], reverse=True)
    return out


def delete_backup_sync(backup_dir: str, name: str) -> bool:
    """删除指定备份。仅接受安全文件名，且必须位于备份目录内。"""
    if not is_safe_backup_name(name):
        raise ValueError("备份文件名非法")
    path = os.path.join(backup_dir, name)
    if not os.path.isfile(path):
        return False
    os.remove(path)
    return True


def prune_auto_backups_sync(backup_dir: str, keep: int) -> list[str]:
    """只清理自动备份，保留最近 keep 份。手动 / 恢复前快照永不自动删除。"""
    keep = max(1, int(keep or 1))
    autos = [b for b in list_backups_sync(backup_dir) if b["auto"]]
    removed: list[str] = []
    for item in autos[keep:]:
        try:
            os.remove(os.path.join(backup_dir, item["name"]))
            removed.append(item["name"])
        except OSError as exc:
            _LOGGER.warning("[backup] 清理旧备份失败 %s: %s", item["name"], exc)
    return removed


# =========================================================================== #
#  调度：判断是否到期                                                            #
# =========================================================================== #
def _latest_slot(now: datetime, schedule: str, hour: int, weekday: int) -> datetime | None:
    """返回 <= now 的最近一个计划时刻。"""
    if schedule == "hourly":
        return now.replace(minute=0, second=0, microsecond=0)
    if schedule == "daily":
        slot = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if slot > now:
            slot -= timedelta(days=1)
        return slot
    if schedule == "weekly":
        slot = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        slot -= timedelta(days=(slot.weekday() - weekday) % 7)
        if slot > now:
            slot -= timedelta(days=7)
        return slot
    return None


def is_backup_due(settings: dict, now: datetime) -> bool:
    """按「最近一次计划时刻」判断是否该备份（迟到也算到期，避免 HA 中途重启漏备）。"""
    if not settings.get("enabled"):
        return False
    schedule = settings.get("schedule") or "off"
    if schedule == "off":
        return False
    slot = _latest_slot(now, schedule, int(settings.get("hour", 3)),
                        int(settings.get("weekday", 0)))
    if slot is None:
        return False
    last = _parse_dt(settings.get("last_at", ""))
    if last is None:
        return True
    return last < slot


def run_scheduled_backup_sync(db_path: str, config_dir: str,
                              now: datetime | None = None) -> dict:
    """执行一次计划备份（含保留清理与 last_at 记录）。"""
    settings = load_settings_sync(db_path, config_dir)
    backup_dir = settings["dir"]
    info = create_backup_sync(db_path, backup_dir, TAG_AUTO, now)
    removed = prune_auto_backups_sync(backup_dir, settings.get("keep", 10))
    _set_last_at(db_path, _fmt_dt(now or _now_local()))
    info["pruned"] = removed
    return info


async def async_maybe_scheduled_backup(hass: HomeAssistant, db_path: str,
                                       config_dir: str) -> dict | None:
    """由 __init__ 的 tick 调用：到期才真正备份。返回备份信息或 None。"""
    settings = await hass.async_add_executor_job(load_settings_sync, db_path, config_dir)
    if not settings.get("enabled") or settings.get("schedule") == "off":
        return None

    tz = hass.data.get(DOMAIN, {}).get("timezone", DEFAULT_TIMEZONE)
    now = _now_local(tz)
    if not is_backup_due(settings, now):
        return None

    def _run() -> dict:
        return run_scheduled_backup_sync(db_path, config_dir, now)

    info = await hass.async_add_executor_job(_run)
    local_logger = get_logger()
    if local_logger:
        local_logger.info(
            "[backup] 自动备份完成 file=%s size=%s 清理旧备份=%d",
            info["name"], info["size_h"], len(info.get("pruned") or []),
        )
    return info


# =========================================================================== #
#  恢复：排队 + 启动时原子应用                                                    #
# =========================================================================== #
def pending_restore_info_sync(db_path: str) -> dict:
    """查询是否有已排队待应用的恢复。"""
    pending = db_path + PENDING_SUFFIX
    marker = db_path + MARKER_SUFFIX
    queued = os.path.isfile(marker)
    info: dict = {
        "queued": queued,
        "pending_exists": os.path.isfile(pending),
        "name": "",
        "queued_at": "",
        "size": 0,
        "size_h": "0 B",
    }
    if os.path.isfile(pending):
        try:
            st = os.stat(pending)
            info["size"] = st.st_size
            info["size_h"] = _human_size(st.st_size)
        except OSError:
            pass
    if queued:
        try:
            with open(marker, encoding="utf-8") as fh:
                meta = json.load(fh)
            info["name"] = str(meta.get("name") or "")
            info["queued_at"] = str(meta.get("queued_at") or "")
        except Exception:
            info["name"] = "（标记文件不可读）"
    return info


def queue_restore_sync(db_path: str, backup_dir: str, name: str,
                       tz_offset: int | None = None) -> dict:
    """校验并把备份排入待恢复队列，下次 HA 启动时应用。"""
    if not is_safe_backup_name(name):
        raise ValueError("备份文件名非法")
    src = os.path.join(backup_dir, name)
    if not os.path.isfile(src):
        raise FileNotFoundError(f"备份文件不存在: {name}")

    ok, msg = validate_backup_sync(src)
    if not ok:
        raise ValueError(f"备份文件校验失败: {msg}")

    pending = db_path + PENDING_SUFFIX
    marker = db_path + MARKER_SUFFIX
    try:
        os.remove(pending)
    except FileNotFoundError:
        pass
    shutil.copy2(src, pending)
    meta = {
        "name": name,
        "queued_at": _fmt_dt(_now_local(tz_offset)),
        "size": os.path.getsize(pending),
    }
    with open(marker, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=2)
    return dict(meta, size_h=_human_size(meta["size"]))


def cancel_pending_restore_sync(db_path: str) -> bool:
    """取消排队中的恢复。"""
    removed = False
    for path in (db_path + PENDING_SUFFIX, db_path + MARKER_SUFFIX):
        try:
            os.remove(path)
            removed = True
        except FileNotFoundError:
            pass
    return removed


def apply_pending_restore_sync(db_path: str, config_dir: str) -> dict | None:
    """启动时应用已排队的恢复。**必须在任何连接打开数据库之前调用。**

    流程：校验待恢复文件 → 给现有库做快照 → 清 -wal/-shm → os.replace 原子替换。
    返回 None 表示没有排队的恢复。
    """
    marker = db_path + MARKER_SUFFIX
    pending = db_path + PENDING_SUFFIX
    if not os.path.isfile(marker):
        return None

    info: dict = {"applied": False, "snapshot": "", "name": "", "reason": ""}
    try:
        with open(marker, encoding="utf-8") as fh:
            meta = json.load(fh)
        info["name"] = str(meta.get("name") or os.path.basename(pending))
    except Exception:
        info["name"] = os.path.basename(pending)

    if not os.path.isfile(pending):
        os.remove(marker)
        info["reason"] = "待恢复文件不存在，已取消"
        _LOGGER.error("[backup] 恢复取消：待恢复文件缺失")
        return info

    ok, msg = validate_backup_sync(pending)
    if not ok:
        invalid = pending + ".invalid"
        try:
            shutil.move(pending, invalid)
        except OSError:
            pass
        os.remove(marker)
        info["reason"] = f"待恢复文件校验失败（{msg}），已取消并保留为 {os.path.basename(invalid)}"
        _LOGGER.error("[backup] 恢复取消：%s", msg)
        return info

    # 1) 现有库先做快照（恢复出错时的退路）；库不存在则跳过（不能凭空造快照）
    settings = load_settings_sync(db_path, config_dir)
    backup_dir = settings["dir"]
    snapshot = ""
    if os.path.isfile(db_path):
        try:
            os.makedirs(backup_dir, exist_ok=True)
            snapshot = create_backup_sync(db_path, backup_dir, TAG_PRERESTORE)["name"]
            info["snapshot"] = snapshot
        except Exception as exc:
            info["snapshot_error"] = str(exc)
            _LOGGER.exception("[backup] 恢复前快照失败（继续执行恢复）: %s", exc)
    else:
        info["snapshot_error"] = "原数据库不存在，未生成快照"

    # 2) 清理旧库的 WAL/SHM，避免旧日志被回放到新库上
    for suffix in ("-wal", "-shm", "-journal"):
        try:
            os.remove(db_path + suffix)
        except FileNotFoundError:
            pass
        except OSError as exc:
            _LOGGER.warning("[backup] 清理 %s 失败: %s", suffix, exc)

    # 3) 原子替换
    os.replace(pending, db_path)
    if os.path.isfile(marker):
        os.remove(marker)
    info["applied"] = True
    _LOGGER.warning("[backup] 已应用排队的库恢复：%s（恢复前快照 %s）",
                    info["name"], snapshot or "无")
    return info


def database_info_sync(db_path: str) -> dict:
    """当前数据库大小 / 行数概要 / 完整性（用于状态卡片）。"""
    info = {"path": db_path, "exists": os.path.isfile(db_path), "size": 0, "size_h": "0 B"}
    if not info["exists"]:
        return info
    try:
        info["size"] = os.path.getsize(db_path)
        info["size_h"] = _human_size(info["size"])
    except OSError:
        pass
    # WAL/SHM 也算进实际占用
    extra = 0
    for suffix in ("-wal", "-shm"):
        try:
            extra += os.path.getsize(db_path + suffix)
        except OSError:
            pass
    info["extra_size"] = extra
    info["extra_size_h"] = _human_size(extra)
    info["total_size"] = info["size"] + extra
    info["total_size_h"] = _human_size(info["total_size"])
    try:
        conn = sqlite3.connect(_ro_uri(db_path), uri=True, timeout=10)
        try:
            try:
                info["journal_mode"] = conn.execute("PRAGMA journal_mode").fetchone()[0]
            except Exception:
                info["journal_mode"] = ""  # 只读连接下个别版本不允许读取，忽略
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            info["table_count"] = len(tables)
        finally:
            conn.close()
    except Exception as exc:
        info["error"] = str(exc)
    return info


def integrity_check_sync(db_path: str) -> dict:
    """对运行中的库做完整性检查（可能耗时，放线程池调用）。"""
    try:
        conn = sqlite3.connect(_ro_uri(db_path), uri=True, timeout=20)
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
            result = str(row[0]) if row else "unknown"
        finally:
            conn.close()
        ok = result.strip().lower() == "ok"
        return {"ok": ok, "message": "数据库完整性正常" if ok else result}
    except Exception as exc:
        return {"ok": False, "message": f"检查失败: {exc}"}


# =========================================================================== #
#  HTTP API                                                                   #
# =========================================================================== #
def _config_dir(request: web.Request) -> str:
    hass: HomeAssistant = request.app["hass"]
    return hass.config.config_dir


class BackupView(_BaseDBView):
    """整库备份管理。

    GET    /api/ha_data_store/backup             状态 + 设置 + 备份列表
    POST   /api/ha_data_store/backup             action: create | save_settings |
                                                 queue_restore | cancel_restore |
                                                 integrity | prune
    DELETE /api/ha_data_store/backup?name=xxx    删除指定备份
    """

    url = "/api/ha_data_store/backup"
    name = "api:ha_data_store:backup"

    async def get(self, request: web.Request) -> web.Response:
        db_path = self._db_path
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_master_switch(hass)):
            return resp
        if (resp := self._check_db_viewer_enabled(hass)):
            return resp
        config_dir = _config_dir(request)

        def _query() -> dict:
            settings = load_settings_sync(db_path, config_dir)
            backups = list_backups_sync(settings["dir"])
            total = sum(b["size"] for b in backups)
            dir_ok, dir_msg = True, ""
            try:
                os.makedirs(settings["dir"], exist_ok=True)
                if not os.access(settings["dir"], os.W_OK):
                    dir_ok, dir_msg = False, "目录不可写（检查权限或改用其他路径）"
            except Exception as exc:
                dir_ok, dir_msg = False, f"目录不可用: {exc}"
            warnings: list[str] = []
            if not dir_ok:
                warnings.append(dir_msg)
            dir_in_storage = os.path.normcase(settings["dir"]).startswith(
                os.path.normcase(os.path.join(config_dir, "storage")))
            if dir_in_storage:
                warnings.append("备份文件与数据库同在 storage 目录，能防误操作但防不了磁盘故障；"
                                "建议改到 NAS 挂载路径并配合 HA 自带备份。")
            elif not os.path.normcase(settings["dir"]).startswith(
                    os.path.normcase(config_dir)):
                # 配置目录之外：多半是网络挂载点（NAS/SMB/NFS）
                warnings.append("备份目录在 HA 配置目录之外（可能是 NAS / SMB / NFS 挂载点）。"
                                "请确认挂载在 HA 启动时已就绪：挂载晚于集成启动会导致计划备份失败；"
                                "另外备份会先在本地生成、校验通过后再写入共享，"
                                "写入期间网络中断不会在共享上留下半个损坏文件。")
            if settings["enabled"] and settings["schedule"] != "off" and not dir_ok:
                warnings.append("自动备份已开启，但目录不可写，计划备份会失败。")
            if not settings["enabled"] or settings["schedule"] == "off":
                warnings.append("自动备份未开启，当前只能手动备份。")
            return {
                "settings": settings,
                "schedule_labels": SCHEDULE_LABELS,
                "backups": backups,
                "total_size": total,
                "total_size_h": _human_size(total),
                "count": len(backups),
                "auto_count": sum(1 for b in backups if b["auto"]),
                "dir_ok": dir_ok,
                "dir_message": dir_msg,
                "database": database_info_sync(db_path),
                "pending": pending_restore_info_sync(db_path),
                "warnings": warnings,
                "tick_minutes": TICK_MINUTES,
            }

        try:
            data = await self._exec_in_executor(hass, _query)
            return self.json({"success": True, "data": data})
        except Exception as exc:
            _LOGGER.exception("读取备份状态失败")
            return self.json({"success": False, "error": str(exc)}, status_code=500)

    async def post(self, request: web.Request) -> web.Response:
        db_path = self._db_path
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_master_switch(hass)):
            return resp
        if (resp := self._check_db_edit_enabled(hass)):
            return resp

        try:
            body = await request.json()
        except Exception:
            return self.json({"success": False, "error": "请求体不是合法的 JSON"},
                             status_code=400)
        if not isinstance(body, dict):
            return self.json({"success": False, "error": "请求体必须是 JSON 对象"},
                             status_code=400)

        action = str(body.get("action") or "create").strip().lower()
        config_dir = _config_dir(request)
        tz = hass.data.get(DOMAIN, {}).get("timezone", DEFAULT_TIMEZONE)

        try:
            if action == "create":
                def _create() -> dict:
                    settings = load_settings_sync(db_path, config_dir)
                    result = create_backup_sync(db_path, settings["dir"], TAG_MANUAL)
                    result["pruned"] = prune_auto_backups_sync(
                        settings["dir"], settings.get("keep", 10))
                    return result

                info = await self._exec_in_executor(hass, _create)
                return self.json({
                    "success": True,
                    "message": f"备份已生成：{info['name']}（{info['size_h']}）",
                    "data": info,
                })

            if action == "save_settings":
                payload = body.get("settings") if isinstance(body.get("settings"), dict) else body

                def _save() -> dict:
                    return save_settings_sync(db_path, config_dir, payload)

                settings = await self._exec_in_executor(hass, _save)
                return self.json({"success": True, "message": "备份设置已保存",
                                  "data": settings})

            if action == "queue_restore":
                name = str(body.get("name") or "").strip()
                if not name:
                    return self.json({"success": False, "error": "缺少 name 参数"},
                                     status_code=400)
                if str(body.get("confirm") or "").strip().upper() != "RESTORE":
                    return self.json(
                        {"success": False,
                         "error": "恢复需要确认：请求体中需包含 confirm=\"RESTORE\""},
                        status_code=400,
                    )

                def _queue() -> dict:
                    settings = load_settings_sync(db_path, config_dir)
                    return queue_restore_sync(db_path, settings["dir"], name, tz)

                meta = await self._exec_in_executor(hass, _queue)
                return self.json({
                    "success": True,
                    "message": f"已排队恢复 {name}，请重启 Home Assistant 后生效",
                    "data": meta,
                })

            if action == "cancel_restore":
                removed = await self._exec_in_executor(
                    hass, cancel_pending_restore_sync, db_path)
                return self.json({
                    "success": True,
                    "message": "已取消排队中的恢复" if removed else "当前没有排队中的恢复",
                })

            if action == "integrity":
                result = await self._exec_in_executor(hass, integrity_check_sync, db_path)
                return self.json({"success": True, "data": result,
                                  "message": result["message"]})

            if action == "prune":

                def _prune() -> dict:
                    settings = load_settings_sync(db_path, config_dir)
                    removed = prune_auto_backups_sync(settings["dir"], settings.get("keep", 10))
                    return {"removed": removed}

                result = await self._exec_in_executor(hass, _prune)
                n = len(result["removed"])
                return self.json({
                    "success": True,
                    "message": f"已清理 {n} 份旧自动备份" if n else "没有需要清理的自动备份",
                    "data": result,
                })

            return self.json({"success": False, "error": f"未知操作: {action}"},
                             status_code=400)
        except (ValueError, FileNotFoundError) as exc:
            return self.json({"success": False, "error": str(exc)}, status_code=400)
        except Exception as exc:
            _LOGGER.exception("备份操作失败 action=%s", action)
            return self.json({"success": False, "error": str(exc)}, status_code=500)

    async def delete(self, request: web.Request) -> web.Response:
        db_path = self._db_path
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_master_switch(hass)):
            return resp
        if (resp := self._check_db_edit_enabled(hass)):
            return resp

        name = (request.query.get("name") or "").strip()
        if not name:
            return self.json({"success": False, "error": "缺少 name 参数"}, status_code=400)
        config_dir = _config_dir(request)

        def _delete() -> bool:
            settings = load_settings_sync(db_path, config_dir)
            return delete_backup_sync(settings["dir"], name)

        try:
            removed = await self._exec_in_executor(hass, _delete)
            if not removed:
                return self.json({"success": False, "error": f"备份不存在: {name}"},
                                 status_code=404)
            return self.json({"success": True, "message": f"已删除 {name}"})
        except ValueError as exc:
            return self.json({"success": False, "error": str(exc)}, status_code=400)
        except Exception as exc:
            _LOGGER.exception("删除备份失败")
            return self.json({"success": False, "error": str(exc)}, status_code=500)


class BackupDownloadView(_BaseDBView):
    """下载备份文件：GET /api/ha_data_store/backup/download?name=xxx"""

    url = "/api/ha_data_store/backup/download"
    name = "api:ha_data_store:backup_download"

    async def get(self, request: web.Request) -> web.Response:
        db_path = self._db_path
        hass: HomeAssistant = request.app["hass"]
        if (resp := self._check_master_switch(hass)):
            return resp
        if (resp := self._check_db_viewer_enabled(hass)):
            return resp

        name = (request.query.get("name") or "").strip()
        if not is_safe_backup_name(name):
            return self.json({"success": False, "error": "备份文件名非法"}, status_code=400)

        settings = await self._exec_in_executor(
            hass, load_settings_sync, db_path, _config_dir(request))
        path = os.path.join(settings["dir"], name)
        if not os.path.isfile(path):
            return self.json({"success": False, "error": f"备份不存在: {name}"},
                             status_code=404)
        return web.FileResponse(
            path,
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )


# =========================================================================== #
#  注册入口                                                                     #
# =========================================================================== #
def register_api_views(hass: HomeAssistant, db_path: str) -> None:
    """注册备份相关 API View。由 __init__._register_api_views 调用。"""
    hass.http.register_view(BackupView(db_path))
    hass.http.register_view(BackupDownloadView(db_path))
    _LOGGER.info("[backup] API 已注册：/api/ha_data_store/backup")
