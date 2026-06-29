"""音乐文件元数据探测模块。

封装 mutagen 读取标签/封面/时长 + 同名 .lrc 歌词文件读取，
供媒体播放列表的 refresh_meta 接口和封面/歌词接口使用。

公共函数：
  resolve_media_path(hass, media_content_id) -> str | None
  probe_media_meta(full_path) -> dict
  extract_cover(full_path) -> tuple[str, bytes] | None
"""
from __future__ import annotations

import os
import logging
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 路径解析
# ---------------------------------------------------------------------------
def resolve_media_path(hass: Any, media_content_id: str) -> str | None:
    """将 media_content_id 解析为本地文件绝对路径。

    复用 virtual_devices.py 的解析逻辑：
      media-source://media_source/local/xxx/yy.mp3 → xxx/yy.mp3
    再在多个候选媒体目录下查找实际文件。
    """
    if not media_content_id:
        return None
    try:
        # 兼容 media-source://media_source/local/... 与 /local/... 两种形态
        raw = media_content_id
        if "/local/" in raw:
            path_part = raw.split("/local/", 1)[-1]
        elif "/media_source/local/" in raw:
            path_part = raw.split("/media_source/local/", 1)[-1]
        else:
            # 退化：取最后一段路径
            path_part = raw.rstrip("/").split("/")[-1]
        import urllib.parse
        path_part = urllib.parse.unquote(path_part)
        if not path_part:
            _LOGGER.warning("[media_meta] 路径解析失败(空path_part): %s", media_content_id)
            return None

        candidates: list[str] = []
        if hass is not None:
            try:
                p = hass.config.path("media")
                if p:
                    candidates.append(p)
            except Exception:
                pass
        candidates.append("/media")
        # 也尝试 config 目录下的 media
        if hass is not None:
            try:
                candidates.append(os.path.join(hass.config.config_dir, "media"))
            except Exception:
                pass

        tried = []
        for base in candidates:
            if not base:
                continue
            full = os.path.join(base, path_part)
            tried.append(full)
            if os.path.isfile(full):
                _LOGGER.info("[media_meta] 路径解析成功: %s → %s", media_content_id, full)
                return full
        _LOGGER.warning("[media_meta] 路径解析失败(文件不存在): id=%s path_part=%s tried=%s",
                        media_content_id, path_part, tried)
    except Exception:
        _LOGGER.warning("[media_meta] 路径解析异常: %s", media_content_id, exc_info=True)
    return None


# ---------------------------------------------------------------------------
# 标签读取（多格式统一）
# ---------------------------------------------------------------------------
def _read_tag_str(audio, *keys: str) -> str:
    """从 mutagen audio 对象按多个候选 key 取第一个字符串值。"""
    for k in keys:
        try:
            v = audio.get(k)
            if v:
                if isinstance(v, list):
                    v = v[0]
                return str(v).strip()
        except Exception:
            continue
    return ""


def _read_lyrics_embedded(audio) -> str:
    """读取内嵌歌词（ID3 USLT / FLAC&OGG LYRICS&UNSYNCEDLYRICS / MP4 ©lyr）。"""
    # ID3 USLT 帧
    try:
        uslt = audio.get("USLT::eng") or audio.get("USLT") or audio.get("USLT:")
        if uslt:
            text = getattr(uslt, "text", None) or (uslt[0] if isinstance(uslt, list) else None)
            if text:
                return str(text)
    except Exception:
        pass
    # 通用字符串字段
    for k in ("LYRICS", "UNSYNCEDLYRICS", "UNSYNCED LYRICS", "\xa9lyr", "©lyr"):
        try:
            v = audio.get(k)
            if v:
                if isinstance(v, list):
                    v = v[0]
                return str(v)
        except Exception:
            continue
    return ""


def _detect_has_cover(audio) -> bool:
    """检测是否存在内嵌封面图。"""
    try:
        # ID3 APIC
        if audio.get("APIC:") or any(k.startswith("APIC") for k in (audio.keys() if hasattr(audio, "keys") else [])):
            return True
        # FLAC/OGG pictures
        pics = getattr(audio, "pictures", None)
        if pics:
            return True
        # MP4 covr
        if audio.get("covr"):
            return True
    except Exception:
        pass
    return False


def probe_media_meta(full_path: str) -> dict:
    """探测单个音乐文件元数据。

    返回字典字段：
      title, artist, album, duration(秒|int|None),
      has_cover(bool), has_lyrics(bool), lyrics(str)
    任意字段探测失败返回安全默认值，不抛异常。
    """
    result: dict[str, Any] = {
        "title": "",
        "artist": "",
        "album": "",
        "duration": None,
        "has_cover": False,
        "has_lyrics": False,
        "lyrics": "",
    }
    # ---- 歌词：同名 .lrc 文件优先 ----
    lrc_path = Path(full_path).with_suffix(".lrc")
    lrc_text = ""
    if lrc_path.is_file():
        for enc in ("utf-8", "gb18030", "gbk", "utf-16"):
            try:
                lrc_text = lrc_path.read_text(encoding=enc, errors="strict")
                break
            except (UnicodeDecodeError, OSError):
                continue
        else:
            try:
                lrc_text = lrc_path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                lrc_text = ""
    if lrc_text:
        result["has_lyrics"] = True
        result["lyrics"] = lrc_text

    # ---- mutagen 读取标签 + 时长 + 内嵌歌词兜底 ----
    audio = None
    try:
        from mutagen import File as MFile
        audio = MFile(full_path)
    except ImportError:
        _LOGGER.warning("[media_meta] mutagen 未安装，跳过标签探测: %s", full_path)
    except Exception:
        _LOGGER.warning("[media_meta] mutagen 读取失败: %s", full_path, exc_info=True)

    if audio is not None:
        # 时长
        try:
            info = getattr(audio, "info", None)
            if info is not None:
                length = getattr(info, "length", None)
                if length:
                    result["duration"] = int(float(length))
        except Exception:
            pass
        # 标签（多格式候选 key）
        result["title"] = _read_tag_str(audio, "TIT2", "title", "\xa9nam", "©nam", "TITLE")
        result["artist"] = _read_tag_str(audio, "TPE1", "artist", "\xa9ART", "©ART", "ARTIST")
        result["album"] = _read_tag_str(audio, "TALB", "album", "\xa9alb", "©alb", "ALBUM")
        # 封面标志位
        result["has_cover"] = _detect_has_cover(audio)
        # 内嵌歌词兜底（仅当 .lrc 文件没有时）
        if not result["has_lyrics"]:
            embedded = _read_lyrics_embedded(audio)
            if embedded:
                result["has_lyrics"] = True
                result["lyrics"] = embedded

    # title 缺失则用文件名（去扩展名）
    if not result["title"]:
        try:
            result["title"] = Path(full_path).stem
        except Exception:
            result["title"] = os.path.basename(full_path).rsplit(".", 1)[0]

    return result


# ---------------------------------------------------------------------------
# 封面提取
# ---------------------------------------------------------------------------
def extract_cover(full_path: str) -> tuple[str, bytes] | None:
    """提取内嵌封面图，返回 (mime_type, image_bytes)，无封面返回 None。"""
    try:
        from mutagen import File as MFile
        audio = MFile(full_path)
    except ImportError:
        _LOGGER.warning("[media_meta] mutagen 未安装，无法提取封面")
        return None
    except Exception:
        return None

    if audio is None:
        return None

    # ID3 APIC
    try:
        for k in audio.keys() if hasattr(audio, "keys") else []:
            if k.startswith("APIC"):
                apic = audio[k]
                data = getattr(apic, "data", None)
                mime = getattr(apic, "mime", "image/jpeg")
                if data:
                    return (mime or "image/jpeg", bytes(data))
    except Exception:
        pass

    # FLAC/OGG pictures
    try:
        pics = getattr(audio, "pictures", None)
        if pics:
            p = pics[0]
            data = getattr(p, "data", None)
            mime = getattr(p, "mime", "image/jpeg")
            if data:
                return (mime or "image/jpeg", bytes(data))
    except Exception:
        pass

    # MP4 covr
    try:
        covr = audio.get("covr")
        if covr:
            item = covr[0]
            data = bytes(item) if not isinstance(item, (bytes, bytearray)) else bytes(item)
            # MP4 封面格式：0=JPEG, 1=PNG ...
            fmt = getattr(item, "imageformat", None)
            mime = "image/png" if fmt == 1 else "image/jpeg"
            return (mime, data)
    except Exception:
        pass

    return None
