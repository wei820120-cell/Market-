"""股票期貨完整資料：保證金比例、近月行情、成交量、未平倉量。

來源：期交所 OpenAPI
- /SingleStockFuturesMargining       股票類保證金比例（原始／維持／結算，依級距）
- /SingleStockFuturesETFMargining    ETF 期貨保證金（固定金額）
- /DailyMarketReportFut              期貨每日行情（含各股票期貨、一般／盤後時段）
每天快取一次（盤後更新）。保證金＝契約價值 × 比例，契約價值＝股價 × 每口股數。
"""
from __future__ import annotations

import json
import logging
import re

from .. import net
from ..utils import CACHE_DIR, now_tw, to_float

log = logging.getLogger(__name__)

OPENAPI = "https://openapi.taifex.com.tw/v1"
CACHE = CACHE_DIR / "futures_detail.json"
STD_SHARES, MINI_SHARES = 2000, 100
ETF_SHARES, ETF_MINI_SHARES = 10000, 1000


def _rate(v) -> float | None:
    x = to_float(str(v or "").replace("%", ""))
    return None if x is None else x / 100


def parse_margins(stock_rows: list[dict], etf_rows: list[dict]) -> dict:
    """回傳 {"rate": {證券代號: {"level", "initial", "maint"}}, "etf": {契約代碼: {"initial", "maint"}}}。"""
    rate: dict[str, dict] = {}
    for r in stock_rows or []:
        code = str(r.get("UnderlyingSecurityCode") or "").strip()
        init, maint = _rate(r.get("InitialMarginRate")), _rate(r.get("MaintenanceMarginRate"))
        if code and init:
            rate[code] = {"level": str(r.get("GroupLevel") or ""), "initial": init, "maint": maint}
    etf: dict[str, dict] = {}
    for r in etf_rows or []:
        c = str(r.get("Contract") or "").strip()
        if c:
            etf[c] = {"initial": to_float(r.get("InitialMargin")), "maint": to_float(r.get("MaintenanceMargin"))}
    return {"rate": rate, "etf": etf}


def parse_quotes(rows: list[dict]) -> dict[str, dict]:
    """期貨每日行情 → {契約代碼: {"month", "last", "settle", "volume", "oi", "night_volume"}}。

    近月＝一般時段最早的單一月份（跨月價差如 202610/202611 不算）；成交量、未平倉加總所有月份。
    """
    out: dict[str, dict] = {}
    for r in rows or []:
        c = str(r.get("Contract") or "").strip()
        month = str(r.get("ContractMonth(Week)") or "").strip()
        if not c or not re.fullmatch(r"\d{6}", month):
            continue
        q = out.setdefault(c, {"month": None, "last": None, "settle": None, "volume": 0, "oi": 0,
                               "night_volume": 0, "date": str(r.get("Date") or "")})
        vol = to_float(r.get("Volume")) or 0
        if "盤後" in str(r.get("TradingSession") or ""):
            q["night_volume"] += int(vol)
            continue
        q["volume"] += int(vol)
        q["oi"] += int(to_float(r.get("OpenInterest")) or 0)
        if q["month"] is None or month < q["month"]:
            q["month"] = month
            q["last"] = to_float(r.get("Last"))
            q["settle"] = to_float(r.get("SettlementPrice"))
    return out


def load_detail() -> dict:
    today = now_tw().strftime("%Y-%m-%d")
    if CACHE.exists():
        cached = json.loads(CACHE.read_text(encoding="utf-8"))
        if cached.get("date") == today and cached.get("quotes"):
            return cached
    out: dict = {"date": today, "rate": {}, "etf": {}, "quotes": {}}
    try:
        out.update(parse_margins(net.get_json(f"{OPENAPI}/SingleStockFuturesMargining"),
                                 net.get_json(f"{OPENAPI}/SingleStockFuturesETFMargining")))
    except Exception as e:  # noqa: BLE001
        log.warning("股票期貨保證金抓取失敗：%s", e)
    try:
        out["quotes"] = parse_quotes(net.get_json(f"{OPENAPI}/DailyMarketReportFut"))
    except Exception as e:  # noqa: BLE001
        log.warning("期貨每日行情抓取失敗：%s", e)
    if out["quotes"] or out["rate"]:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    elif CACHE.exists():
        return json.loads(CACHE.read_text(encoding="utf-8"))
    return out


def contracts(code: str, price: float | None, info: dict | None, detail: dict) -> list[dict]:
    """一檔股票的股票期貨契約明細（一般、小型各一筆）。沒有股票期貨回傳空清單。

    每筆：{"kind": "一般"/"小型", "contract", "shares", "value", "initial", "maint", "level",
           "last", "volume", "oi", "night_volume", "night"}
    """
    if not info:
        return []
    is_etf = code.startswith("00")
    out = []
    for kind, contract in (("一般", info.get("std")), ("小型", info.get("mini"))):
        if not contract:
            continue
        if is_etf:
            shares = ETF_SHARES if kind == "一般" else ETF_MINI_SHARES
        else:
            shares = STD_SHARES if kind == "一般" else MINI_SHARES
        value = price * shares if price else None
        init = maint = None
        level = ""
        if is_etf and contract in (detail.get("etf") or {}):
            m = detail["etf"][contract]
            init, maint = m.get("initial"), m.get("maint")
        elif code in (detail.get("rate") or {}) and value:
            m = detail["rate"][code]
            init, maint, level = value * m["initial"], value * (m.get("maint") or 0), m.get("level", "")
        q = (detail.get("quotes") or {}).get(contract) or {}
        out.append({"kind": kind, "contract": contract, "shares": shares, "value": value,
                    "initial": init, "maint": maint, "level": level, "last": q.get("last"),
                    "volume": q.get("volume"), "oi": q.get("oi"), "night_volume": q.get("night_volume"),
                    "night": bool(info.get("night")) and kind == "一般"})
    return out


def wan(v: float | None) -> str:
    """金額以「萬」顯示：123456 → 12.3萬。"""
    if v is None:
        return "—"
    return f"{v / 1e4:,.1f}萬" if v < 1e8 else f"{v / 1e8:,.2f}億"
