"""共用小工具：數字轉換、欄位挑選、時間。"""
from __future__ import annotations

import re
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

TW_TZ = ZoneInfo("Asia/Taipei")
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"
REPORT_DIR = ROOT / "reports"

TW_OPEN = time(9, 0)
TW_CLOSE = time(13, 30)


def now_tw() -> datetime:
    return datetime.now(TW_TZ)


def to_float(value) -> float | None:
    """'1,234.5'、'+3.2'、'--'、'X0.00' 之類的字串轉成 float，無法轉換回傳 None。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace(",", "")
    if s in ("", "-", "--", "---", "N/A", "null", "None"):
        return None
    m = re.search(r"[-+]?\d+(?:\.\d+)?", s)
    if not m:
        return None
    try:
        return float(m.group())
    except ValueError:
        return None


def pick(row: dict, *names, default=None):
    """依序找欄位名稱（忽略前後空白），找不到再用「包含」比對。"""
    stripped = {str(k).strip(): v for k, v in row.items()}
    for n in names:
        if n in stripped:
            return stripped[n]
    for n in names:
        for k, v in stripped.items():
            if n in k:
                return v
    return default


def pick_contains(row: dict, *parts, default=None):
    """找第一個欄位名稱同時包含所有 parts 的值。"""
    for k, v in row.items():
        key = str(k)
        if all(p in key for p in parts):
            return v
    return default


def find_col(fields: list[str], *parts) -> int | None:
    """在欄位清單中找同時包含所有 parts 的欄位索引。"""
    for i, f in enumerate(fields):
        if all(p in str(f) for p in parts):
            return i
    return None


def ensure_dirs() -> None:
    for d in (DATA_DIR, CACHE_DIR, REPORT_DIR):
        d.mkdir(parents=True, exist_ok=True)
