"""ha_data_store 本地文件日志模块。

特性：
  - 每日一个日志文件，保存在集成目录 logs/ 下
  - 自动保留最近 7 天日志，旧日志自动清理
  - 线程安全，支持从线程池调用
  - 提供日志文件列表读取 API（供前台查看）
"""
from __future__ import annotations

import glob
import logging
import os
import threading
from datetime import datetime, timedelta


# =========================================================================== #
#  每日滚动日志记录器                                                            #
# =========================================================================== #
class DailyRotatingLogger:
    """线程安全的每日滚动日志记录器。

    每次写入时自动检测日期是否变更，若变更则切换到新日期的日志文件。
    """

    def __init__(self, log_dir: str, keep_days: int = 7) -> None:
        self._log_dir = log_dir
        self._keep_days = keep_days
        self._lock = threading.Lock()
        self._current_date: str = ""
        self._handler: logging.FileHandler | None = None

        self._logger = logging.getLogger("ha_data_store_local")
        self._logger.setLevel(logging.DEBUG)
        self._logger.handlers.clear()

        os.makedirs(log_dir, exist_ok=True)
        self._ensure_handler()
        self._cleanup()

    # ------------------------------------------------------------------ #
    #  内部：确保 handler 指向今天的文件                                     #
    # ------------------------------------------------------------------ #
    def _ensure_handler(self) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        if today == self._current_date and self._handler is not None:
            # 检查文件是否被外部删除（如清除全部日志），若被删则重建
            if os.path.exists(os.path.join(self._log_dir, f"{today}.log")):
                return
            # 文件已删除，移除旧 handler
            self._logger.removeHandler(self._handler)
            self._handler.close()
            self._handler = None

        if self._handler is not None:
            self._logger.removeHandler(self._handler)
            self._handler.close()

        log_file = os.path.join(self._log_dir, f"{today}.log")
        self._handler = logging.FileHandler(log_file, encoding="utf-8")
        self._handler.setLevel(logging.DEBUG)
        self._handler.setFormatter(logging.Formatter(
            "[%(asctime)s] [%(levelname)-7s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        self._logger.addHandler(self._handler)
        self._current_date = today

    # ------------------------------------------------------------------ #
    #  内部：清理过期日志                                                    #
    # ------------------------------------------------------------------ #
    def _cleanup(self) -> None:
        cutoff = datetime.now() - timedelta(days=self._keep_days)
        for filepath in glob.glob(os.path.join(self._log_dir, "*.log")):
            try:
                date_str = os.path.basename(filepath).replace(".log", "")
                file_date = datetime.strptime(date_str, "%Y-%m-%d")
                if file_date < cutoff:
                    os.remove(filepath)
            except (ValueError, OSError):
                pass

    # ------------------------------------------------------------------ #
    #  公开写方法                                                          #
    # ------------------------------------------------------------------ #
    def debug(self, msg: str, *args) -> None:
        with self._lock:
            self._ensure_handler()
            self._logger.debug(msg, *args)

    def info(self, msg: str, *args) -> None:
        with self._lock:
            self._ensure_handler()
            self._logger.info(msg, *args)

    def warning(self, msg: str, *args) -> None:
        with self._lock:
            self._ensure_handler()
            self._logger.warning(msg, *args)

    def error(self, msg: str, *args) -> None:
        with self._lock:
            self._ensure_handler()
            self._logger.error(msg, *args)

    def exception(self, msg: str, *args) -> None:
        with self._lock:
            self._ensure_handler()
            self._logger.exception(msg, *args)

    # ------------------------------------------------------------------ #
    #  日志文件查询（供日志查看器 API 使用）                                   #
    # ------------------------------------------------------------------ #
    @property
    def log_dir(self) -> str:
        return self._log_dir

    def get_log_files(self) -> list[dict]:
        """返回日志文件列表，按日期降序。"""
        files: list[dict] = []
        for filepath in sorted(
            glob.glob(os.path.join(self._log_dir, "*.log")), reverse=True,
        ):
            fname = os.path.basename(filepath)
            try:
                date_str = fname.replace(".log", "")
                size = os.path.getsize(filepath)
                files.append({
                    "date": date_str,
                    "filename": fname,
                    "size": size,
                    "size_str": f"{size / 1024:.1f} KB" if size >= 1024 else f"{size} B",
                })
            except (ValueError, OSError):
                pass
        return files

    def read_log_content(self, date_str: str, tail_bytes: int = 200 * 1024) -> str | None:
        """读取指定日期日志内容，默认取末尾 200KB。"""
        filepath = os.path.join(self._log_dir, f"{date_str}.log")
        if not os.path.exists(filepath):
            return None
        try:
            file_size = os.path.getsize(filepath)
            if file_size <= tail_bytes:
                with open(filepath, "r", encoding="utf-8") as f:
                    return f.read()
            # 只读末尾 tail_bytes，跳过首行（可能不完整）
            with open(filepath, "r", encoding="utf-8") as f:
                f.seek(file_size - tail_bytes)
                f.readline()  # skip partial first line
                return f.read()
        except OSError:
            return None


# =========================================================================== #
#  全局单例                                                                    #
# =========================================================================== #
_LOCAL_LOGGER: DailyRotatingLogger | None = None


def setup_local_logger(log_dir: str, keep_days: int = 7) -> DailyRotatingLogger:
    """初始化并返回全局本地日志记录器（阻塞函数，请在 executor 中调用）。"""
    global _LOCAL_LOGGER
    _LOCAL_LOGGER = DailyRotatingLogger(log_dir, keep_days)
    return _LOCAL_LOGGER


async def async_setup_local_logger(hass, log_dir: str, keep_days: int = 7) -> DailyRotatingLogger:
    """异步初始化本地日志记录器，避免阻塞事件循环。"""
    return await hass.async_add_executor_job(setup_local_logger, log_dir, keep_days)


def get_logger() -> DailyRotatingLogger | None:
    """获取全局本地日志记录器（可能为 None，若尚未调用 setup_logger）。"""
    return _LOCAL_LOGGER
