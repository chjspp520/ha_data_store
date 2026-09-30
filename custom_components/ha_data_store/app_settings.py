"""api_settings（系统设置键值表）的通用读写。

db_viewer 的「系统配置」把各类配置项存进 `api_settings`（`skey` 主键 + `svalue` 文本），
本模块提供最常用的「字符串列表」形态：

- 读：JSON 数组；兼容逗号 / 换行（含全角逗号）分隔的纯文本，便于前端直接手输
- 写：去重、去空后以 JSON 数组存回

供 recent_devices（近期使用设备排除项）、onthisday（历史今日排除实体）等模块复用，
避免各自重复实现同一套读写与容错逻辑。
"""
from __future__ import annotations

import json
import sqlite3

from .const import TABLE_API_SETTINGS


def get_list(db_path: str, key: str) -> list[str]:
    """读取列表设置；记录缺失 / JSON 非法 / 库不可读 → 一律返回空列表。"""
    try:
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                f"SELECT svalue FROM {TABLE_API_SETTINGS} WHERE skey = ?", (key,)
            ).fetchone()
        finally:
            conn.close()
    except Exception:
        return []
    raw = (row[0] if row else "") or ""
    if not raw:
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return [str(x).strip() for x in data if str(x).strip()]
    except Exception:
        pass
    # 兼容纯文本写法（逗号 / 换行 / 全角逗号分隔）
    text = raw.replace("\n", ",").replace("，", ",")
    return [p.strip() for p in text.split(",") if p.strip()]


def set_list(db_path: str, key: str, values) -> list[str]:
    """保存列表设置（去重、去空、保持原顺序），返回实际保存的列表。"""
    out: list[str] = []
    seen: set[str] = set()
    for x in values or []:
        s = str(x or "").strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            f"INSERT OR REPLACE INTO {TABLE_API_SETTINGS} (skey, svalue) VALUES (?, ?)",
            (key, json.dumps(out, ensure_ascii=False)),
        )
        conn.commit()
    finally:
        conn.close()
    return out
