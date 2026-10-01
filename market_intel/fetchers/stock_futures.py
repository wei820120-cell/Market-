"""股票期貨標的清單：哪些股票有股票期貨，以及契約代碼（例如 台積電 2330 → CDF）。

來源（依序嘗試）：
1. 期交所 OpenAPI 目錄中「股票期貨…標的」的資料集
2. 期交所網站「股票期貨標的」頁面 https://www.taifex.com.tw/cht/2/stockLists
每天快取一次。
"""
from __future__ import annotations

import html
import json
import logging
import re

from .. import net
from ..utils import CACHE_DIR, now_tw, pick_contains

log = logging.getLogger(__name__)

TAIFEX_SWAGGER = "https://openapi.taifex.com.tw/swagger.json"
TAIFEX_OPENAPI = "https://openapi.taifex.com.tw/v1"
STOCK_LISTS_URL = "https://www.taifex.com.tw/cht/2/stockLists"
CACHE = CACHE_DIR / "stock_futures.json"


def find_openapi_path(swagger: dict) -> str | None:
    for path, ops in (swagger.get("paths") or {}).items():
        op = (ops or {}).get("get") or {}
        text = f"{op.get('summary', '')} {op.get('description', '')}"
        if "股票期貨" in text and "標的" in text:
            return path
    return None


def parse_openapi_rows(rows: list[dict]) -> dict[str, str]:
    out = {}
    for r in rows:
        code = str(pick_contains(r, "證券代號") or pick_contains(r, "Underlying") or pick_contains(r, "StockCode") or "").strip()
        fut = str(pick_contains(r, "股票期貨", "代") or pick_contains(r, "Futures", "Code") or pick_contains(r, "Contract") or "").strip()
        if re.fullmatch(r"\d{4,6}[A-Z]?", code) and re.fullmatch(r"[A-Z0-9]{2,4}", fut):
            out[code] = fut
    return out


def parse_stock_lists_html(text: str) -> dict[str, str]:
    """解析期交所股票期貨標的頁面：每列第一格是證券代號，取該列第一個以 F 結尾的英文代碼當期貨契約代碼。"""
    out = {}
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", text, flags=re.S | re.I):
        cells = [html.unescape(re.sub(r"<[^>]+>", "", c)).strip()
                 for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, flags=re.S | re.I)]
        if not cells or not re.fullmatch(r"\d{4,6}[A-Z]?", cells[0]):
            continue
        fut = next((c for c in cells[1:] if re.fullmatch(r"[A-Z]{2,3}F", c)), None)
        if fut:
            out[cells[0]] = fut
    return out


def load_stock_futures() -> dict[str, str]:
    """回傳 {股票代號: 股票期貨契約代碼}。"""
    today = now_tw().strftime("%Y-%m-%d")
    if CACHE.exists():
        cached = json.loads(CACHE.read_text(encoding="utf-8"))
        if cached.get("date") == today and cached.get("data"):
            return cached["data"]
    data: dict[str, str] = {}
    try:
        path = find_openapi_path(net.get_json(TAIFEX_SWAGGER))
        if path:
            data = parse_openapi_rows(net.get_json(f"{TAIFEX_OPENAPI}{path}"))
            log.info("股票期貨標的（OpenAPI %s）：%d 檔", path, len(data))
    except Exception as e:  # noqa: BLE001
        log.debug("期交所 OpenAPI 股票期貨清單失敗：%s", e)
    if not data:
        try:
            data = parse_stock_lists_html(net.get(STOCK_LISTS_URL).text)
            log.info("股票期貨標的（網頁）：%d 檔", len(data))
        except Exception as e:  # noqa: BLE001
            log.warning("股票期貨標的清單抓取失敗：%s", e)
    if data:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps({"date": today, "data": data}, ensure_ascii=False), encoding="utf-8")
    elif CACHE.exists():
        data = json.loads(CACHE.read_text(encoding="utf-8")).get("data", {})
    return data
