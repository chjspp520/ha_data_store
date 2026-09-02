"""ha_data_store 系统资源采集与资源占用传感器。

提供两类能力：
1. collect_system_info(hass) —— 采集系统/HA 基本信息（CPU/内存/硬盘/HA版本等），
   由 DbViewerUrlSensor 等低频实体在 attributes 中携带展示（每 10 分钟刷新）。
2. CpuUsageSensor / MemoryUsageSensor / DiskUsageSensor —— 三个独立的资源占用传感器，
   主值均为使用率百分比，attributes 附带已用/总量详情（每 30 秒刷新）。

psutil 为标准 Home Assistant 环境自带依赖；个别精简环境缺失时自动降级，
相关字段返回 None，不影响集成其它功能。
"""
from __future__ import annotations

import importlib.metadata
import logging
import os
import platform
import time
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.system_info import async_get_system_info

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

try:  # psutil 为标准 HA 环境自带；精简环境缺失时降级
    import psutil
except Exception:  # pragma: no cover
    psutil = None


# ---------------------------------------------------------------------------
# 磁盘整体大小：汇总系统中所有真实本地磁盘分区
# ---------------------------------------------------------------------------
def _disk_total_usage() -> dict | None:
    """汇总所有本地磁盘分区的总容量与已用，返回 (total_bytes, used_bytes)。"""
    if psutil is None:
        return None
    try:
        total = 0
        used = 0
        for part in psutil.disk_partitions(all=False):
            # 跳过伪文件系统 / 特殊挂载点 / loop 设备，避免重复与虚报
            fs = (part.fstype or "").lower()
            dev = part.device or ""
            if fs in ("tmpfs", "squashfs", "overlay", "proc", "sysfs", "devpts", "devtmpfs"):
                continue
            if fs.startswith("fuse") or dev.startswith("/dev/loop"):
                continue
            try:
                u = psutil.disk_usage(part.mountpoint)
                total += u.total
                used += u.used
            except Exception:
                continue
        if total <= 0:
            return None
        return {"total_bytes": total, "used_bytes": used}
    except Exception as e:
        _LOGGER.warning("[HDS] 汇总磁盘用量失败: %s", e)
        return None


def _cpu_model() -> str | None:
    """读取 CPU 型号（Linux /proc/cpuinfo，其余用 platform.processor()）。"""
    try:
        if os.path.exists("/proc/cpuinfo"):
            with open("/proc/cpuinfo", "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if line.lower().startswith("model name") or line.lower().startswith("hardware"):
                        val = line.split(":", 1)[1].strip()
                        if val:
                            return val
        p = platform.processor()
        return p if p else None
    except Exception:
        try:
            return platform.processor() or None
        except Exception:
            return None


def _uptime() -> dict | None:
    """系统开机时长：返回 (秒, 可读文本)；psutil 不可用时返回 None。"""
    try:
        if psutil is None:
            return None
        boot = psutil.boot_time()
        up = max(0, int(time.time() - boot))
        return {"uptime_seconds": up, "uptime_text": _fmt_duration(up)}
    except Exception:
        return None


def _fmt_duration(seconds: int) -> str:
    """把秒格式化为 X天X小时X分X秒。"""
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}天")
    if hours:
        parts.append(f"{hours}小时")
    if minutes:
        parts.append(f"{minutes}分")
    parts.append(f"{secs}秒")
    return "".join(parts)


def _frontend_version() -> str | None:
    """读取 HA 前端（home-assistant-frontend）Python 包版本。"""
    try:
        return importlib.metadata.version("home-assistant-frontend")
    except Exception:
        return None


def _install_time(config_dir: str) -> str | None:
    """近似安装时间：取 config 目录 configuration.yaml 的 mtime；无则取目录本身。"""
    try:
        target = config_dir
        if config_dir:
            yaml_path = os.path.join(config_dir, "configuration.yaml")
            if os.path.exists(yaml_path):
                target = yaml_path
            elif not os.path.exists(target):
                return None
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(target)))
    except Exception:
        return None


def _ha_core_version_file(config_dir: str) -> str | None:
    """读取 config 目录 .HA_VERSION 文件中的 Home Assistant 核心版本号（兜底方案）。"""
    try:
        if not config_dir:
            return None
        with open(os.path.join(config_dir, ".HA_VERSION"), "r", encoding="utf-8", errors="ignore") as f:
            ver = f.read().strip()
            return ver or None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 对外采集接口
# ---------------------------------------------------------------------------
async def _async_ha_info(hass) -> dict:
    """读取 Home Assistant 核心版本与安装方式（async，使用官方 system_info API）。

    返回 { ha_version, installation_type }；失败时用 .HA_VERSION 文件兜底版本。
    """
    config_dir = getattr(hass.config, "config_dir", None)
    result: dict[str, Any] = {}
    # 安装方式 / 核心版本：官方 system_info（含 hassio/container/os 判定）
    try:
        sysinfo = await async_get_system_info(hass)
        if sysinfo:
            ver = sysinfo.get("version")
            result["ha_version"] = str(ver) if ver is not None else None
            it = sysinfo.get("installation_type")
            result["installation_type"] = it or None
    except Exception as e:
        _LOGGER.warning("[HDS] 读取 HA 系统信息失败: %s", e)
    # 核心版本兜底：读 .HA_VERSION 文件（executor 内执行，避免阻塞事件循环）
    if not result.get("ha_version"):
        result["ha_version"] = await hass.async_add_executor_job(
            _ha_core_version_file, config_dir)
    return result


def _collect_hardware_sync(hass) -> dict:
    """采集硬件/静态系统信息（同步，供 executor 调用）。"""
    config_dir = getattr(hass.config, "config_dir", None)
    info: dict[str, Any] = {}

    info["install_time"] = _install_time(config_dir)
    info["config_dir"] = config_dir
    # 前端版本（读 home-assistant-frontend 包：文件系统操作，随硬件采集放 executor 内执行）
    info["frontend_version"] = _frontend_version()

    # 运行时长
    up = _uptime()
    if up:
        info["uptime_seconds"] = up["uptime_seconds"]
        info["uptime_text"] = up["uptime_text"]

    # CPU
    info["cpu_model"] = _cpu_model()
    if psutil is not None:
        try:
            info["cpu_physical_cores"] = psutil.cpu_count(logical=False)
            info["cpu_logical_cores"] = psutil.cpu_count(logical=True)
        except Exception:
            info["cpu_physical_cores"] = None
            info["cpu_logical_cores"] = None
        try:
            freq = psutil.cpu_freq()
            info["cpu_freq_mhz"] = round(freq.max, 1) if freq and getattr(freq, "max", None) else None
        except Exception:
            info["cpu_freq_mhz"] = None
    else:
        try:
            info["cpu_physical_cores"] = os.cpu_count()
            info["cpu_logical_cores"] = os.cpu_count()
        except Exception:
            info["cpu_physical_cores"] = None
            info["cpu_logical_cores"] = None

    # 内存总大小
    if psutil is not None:
        try:
            vm = psutil.virtual_memory()
            info["mem_total_mb"] = round(vm.total / 1048576)
        except Exception:
            info["mem_total_mb"] = None
    else:
        info["mem_total_mb"] = None

    # 硬盘整体大小
    disk = _disk_total_usage()
    if disk:
        info["disk_total_mb"] = round(disk["total_bytes"] / 1048576)
    else:
        info["disk_total_mb"] = None

    # 当前系统时间（采集时间）
    info["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

    return info


async def async_collect_system_info(hass) -> dict:
    """采集系统/HA 完整信息（async，供 DbViewerUrlSensor 调用）。

    返回包含 ha_version / installation_type / frontend_version 与硬件静态信息的 dict。
    """
    info: dict[str, Any] = {}
    # HA 核心版本 / 安装方式（async，官方 system_info）
    info.update(await _async_ha_info(hass))
    # 硬件静态信息 + 前端版本（executor 内执行，避免阻塞事件循环）
    hw = await hass.async_add_executor_job(_collect_hardware_sync, hass)
    info.update(hw)
    return info


def collect_usage() -> dict | None:
    """采集三类资源占用率（同步，供 executor 调用）。

    返回 { cpu_percent, mem_percent, mem_used_mb, mem_total_mb,
            disk_percent, disk_used_mb, disk_total_mb }；psutil 缺失时返回 None。
    """
    if psutil is None:
        return None
    result: dict[str, Any] = {}
    try:
        result["cpu_percent"] = round(psutil.cpu_percent(interval=None), 1)
    except Exception:
        result["cpu_percent"] = None
    try:
        vm = psutil.virtual_memory()
        result["mem_percent"] = round(vm.percent, 1)
        result["mem_used_mb"] = round(vm.used / 1048576)
        result["mem_total_mb"] = round(vm.total / 1048576)
    except Exception:
        result["mem_percent"] = None
        result["mem_used_mb"] = None
        result["mem_total_mb"] = None
    try:
        disk = _disk_total_usage()
        if disk:
            total = disk["total_bytes"]
            used = disk["used_bytes"]
            result["disk_percent"] = round(used / total * 100, 1) if total else None
            result["disk_used_mb"] = round(used / 1048576)
            result["disk_total_mb"] = round(total / 1048576)
        else:
            result["disk_percent"] = None
            result["disk_used_mb"] = None
            result["disk_total_mb"] = None
    except Exception:
        result["disk_percent"] = None
        result["disk_used_mb"] = None
        result["disk_total_mb"] = None
    return result


# ---------------------------------------------------------------------------
# 三个资源占用传感器
# ---------------------------------------------------------------------------
class _SystemUsageSensor(SensorEntity):
    """资源占用传感器基类：主值=使用率百分比，30 秒刷新。"""

    _attr_has_entity_name = True
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "%"

    def __init__(self, hass, device_info):
        self._hass = hass
        self._attr_device_info = device_info
        self._attr_unique_id = f"{DOMAIN}_{self._metric}_usage"
        self._attr_native_value = None
        self._attr_extra_state_attributes = {}
        # 预热 cpu_percent，避免首次返回 0
        try:
            if psutil is not None:
                psutil.cpu_percent(interval=None)
        except Exception:
            pass

    @property
    def _metric(self) -> str:
        raise NotImplementedError

    def _extract(self, usage: dict) -> None:
        """子类实现：从 usage 提取自身主值与 attributes。"""
        raise NotImplementedError

    def _load(self) -> None:
        usage = collect_usage()
        if not usage:
            # psutil 缺失：标记不可用
            self._attr_native_value = None
            self._attr_extra_state_attributes = {"error": "psutil 不可用"}
            return
        self._extract(usage)

    async def _async_refresh(self, now=None):
        try:
            await self._hass.async_add_executor_job(self._load)
            self.async_write_ha_state()
        except Exception as e:
            _LOGGER.error("[HDS] %s 刷新失败: %s", self.entity_id, e)


class CpuUsageSensor(_SystemUsageSensor):
    """CPU 占用率传感器。主值=使用率百分比。"""

    _attr_translation_key = "cpu_usage"
    _attr_icon = "mdi:cpu-64-bit"

    @property
    def _metric(self):
        return "cpu"

    def _extract(self, usage):
        self._attr_native_value = usage.get("cpu_percent")
        self._attr_extra_state_attributes = {"type": "cpu", "percent": usage.get("cpu_percent")}


class MemoryUsageSensor(_SystemUsageSensor):
    """内存占用传感器。主值=使用率百分比，attributes 附带已用/总量。"""

    _attr_translation_key = "memory_usage"
    _attr_icon = "mdi:memory"

    @property
    def _metric(self):
        return "memory"

    def _extract(self, usage):
        self._attr_native_value = usage.get("mem_percent")
        self._attr_extra_state_attributes = {
            "type": "memory",
            "percent": usage.get("mem_percent"),
            "used_mb": usage.get("mem_used_mb"),
            "total_mb": usage.get("mem_total_mb"),
        }


class DiskUsageSensor(_SystemUsageSensor):
    """硬盘已用传感器。主值=使用率百分比，attributes 附带已用/总量。"""

    _attr_translation_key = "disk_usage"
    _attr_icon = "mdi:harddisk"

    @property
    def _metric(self):
        return "disk"

    def _extract(self, usage):
        self._attr_native_value = usage.get("disk_percent")
        self._attr_extra_state_attributes = {
            "type": "disk",
            "percent": usage.get("disk_percent"),
            "used_mb": usage.get("disk_used_mb"),
            "total_mb": usage.get("disk_total_mb"),
        }
