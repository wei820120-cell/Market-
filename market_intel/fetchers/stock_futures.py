"""股票期貨標的清單：每檔股票有沒有「股票期貨」、「小型股票期貨」、有沒有夜盤，以及契約代碼。

來源：
1. 期交所網站「股票期貨、選擇權商品標的」頁面 https://www.taifex.com.tw/cht/2/stockLists
   每列有商品代碼（如 CD → 期貨 CDF）、證券代號、是否為股票期貨標的、每口股數、盤後交易時段。
   每口 2,000 股＝一般股票期貨、100 股＝小型股票期貨（ETF：10,000／1,000 單位）。
2. 網頁抓不到時，改用期交所 OpenAPI /SSFLists（只有一般股票期貨，沒有小型與夜盤資訊）。
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
CACHE = CACHE_DIR / "stock_futures_v2.json"


def find_openapi_path(swagger: dict) -> str | None:
    for path, ops in (swagger.get("paths") or {}).items():
        op = (ops or {}).get("get") or {}
        text = f"{op.get('summary', '')} {op.get('description', '')}"
        if "股票期貨" in text and "標的" in text:
            return path
    return None


MINI_SHARES = {100, 1000}  # 小型個股期貨 100 股、小型 ETF 期貨 1,000 單位


def parse_openapi_rows(rows: list[dict]) -> dict[str, dict]:
    """OpenAPI /SSFLists：{"Contract": "CDF", "StockCode": "2330", ...}，只有一般股票期貨。"""
    out: dict[str, dict] = {}
    for r in rows:
        code = str(r.get("StockCode") or pick_contains(r, "證券代號") or "").strip()
        fut = str(r.get("Contract") or pick_contains(r, "代碼") or "").strip()
        if re.fullmatch(r"\d{4,6}[A-Z]?", code) and re.fullmatch(r"[A-Z0-9]{2,4}", fut):
            out.setdefault(code, {"std": None, "mini": None, "night": False})["std"] = fut
    return out


def _cells(row_html: str) -> list[str]:
    return [re.sub(r"\s+", "", html.unescape(re.sub(r"<[^>]+>", "", c)))
            for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row_html, flags=re.S | re.I)]


def parse_stock_lists_html(text: str) -> dict[str, dict]:
    """解析期交所標的頁：回傳 {證券代號: {"std": 一般股期代碼, "mini": 小型股期代碼, "night": 是否有夜盤}}。"""
    out: dict[str, dict] = {}
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", text, flags=re.S | re.I):
        cells = _cells(row)
        if len(cells) < 4 or not re.fullmatch(r"[A-Z0-9]{2,3}", cells[0]):
            continue
        stock = next((c for c in cells[1:4] if re.fullmatch(r"\d{4,6}[A-Z]?", c)), None)
        if not stock or not any("是股票期貨標的" in c for c in cells):
            continue
        shares = next((int(c.replace(",", "")) for c in cells[4:] if re.fullmatch(r"\d{1,3}(,\d{3})*", c)), None)
        night = any(re.search(r"\d{1,2}:\d{2}~次日", c) for c in cells)
        info = out.setdefault(stock, {"std": None, "mini": None, "night": False})
        fut = cells[0] + "F"
        if shares in MINI_SHARES:
            info["mini"] = fut
        else:
            info["std"] = fut
        info["night"] = info["night"] or night
    return out


def label(info: dict | None) -> str:
    """表格／推播用文字，例如「CDF／小型QFF／夜盤」，沒有股票期貨回傳「無」。"""
    if not info or not (info.get("std") or info.get("mini")):
        return "無"
    parts = []
    if info.get("std"):
        parts.append(info["std"])
    if info.get("mini"):
        parts.append(f"小型{info['mini']}")
    if info.get("night"):
        parts.append("夜盤")
    return "／".join(parts)


def raw_samples() -> dict:
    """資料檢查用：回傳期交所兩個來源的原始樣本（欄位名稱、前幾列）。"""
    out: dict = {}
    try:
        sw = net.get_json(TAIFEX_SWAGGER)
        path = find_openapi_path(sw)
        out["openapi_path"] = path
        out["openapi_candidates"] = [p for p, ops in (sw.get("paths") or {}).items()
                                     if "股票" in str(((ops or {}).get("get") or {}).get("summary", ""))]
        if path:
            rows = net.get_json(f"{TAIFEX_OPENAPI}{path}")
            out["openapi_count"] = len(rows) if isinstance(rows, list) else None
            out["openapi_rows"] = rows[:5] if isinstance(rows, list) else rows
    except Exception as e:  # noqa: BLE001
        out["openapi_error"] = str(e)
    try:
        text = net.get(STOCK_LISTS_URL).text
        out["html_length"] = len(text)
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", text, flags=re.S | re.I)
        out["html_rows"] = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "|", r))[:300] for r in rows[:8]]
    except Exception as e:  # noqa: BLE001
        out["html_error"] = str(e)
    return out


def load_stock_futures() -> dict[str, dict]:
    """回傳 {股票代號: {"std": 一般股期代碼, "mini": 小型股期代碼, "night": 是否有夜盤}}。"""
    today = now_tw().strftime("%Y-%m-%d")
    if CACHE.exists():
        cached = json.loads(CACHE.read_text(encoding="utf-8"))
        if cached.get("date") == today and cached.get("data"):
            return cached["data"]
    data: dict[str, dict] = {}
    try:
        data = parse_stock_lists_html(net.get(STOCK_LISTS_URL).text)
        n_mini = sum(1 for v in data.values() if v.get("mini"))
        log.info("股票期貨標的（期交所網頁）：%d 檔，其中 %d 檔有小型股票期貨", len(data), n_mini)
    except Exception as e:  # noqa: BLE001
        log.warning("期交所股票期貨標的網頁抓取失敗：%s", e)
    if not data:
        try:
            path = find_openapi_path(net.get_json(TAIFEX_SWAGGER)) or "/SSFLists"
            data = parse_openapi_rows(net.get_json(f"{TAIFEX_OPENAPI}{path}"))
            log.info("股票期貨標的（OpenAPI %s，無小型資訊）：%d 檔", path, len(data))
        except Exception as e:  # noqa: BLE001
            log.warning("股票期貨標的清單抓取失敗：%s", e)
    if data:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps({"date": today, "data": data}, ensure_ascii=False), encoding="utf-8")
    elif CACHE.exists():
        data = json.loads(CACHE.read_text(encoding="utf-8")).get("data", {})
    if not data:
        log.warning("沒有股票期貨清單，強勢標的的「股票期貨」欄會顯示「未知」")
    return data
