"""台灣期交所盤後資料：期貨/選擇權行情、三大法人未平倉、Put/Call Ratio、大額交易人。

來源：期交所 OpenAPI https://openapi.taifex.com.tw/
欄位名稱以期交所實際回傳為準，這裡用「包含關鍵字」方式挑欄位，格式小改也不會壞。
盤中即時期權報價請接券商 API。
"""
from __future__ import annotations

import json
import logging

from .. import net
from ..utils import DATA_DIR, now_tw, pick, pick_contains, to_float

log = logging.getLogger(__name__)

OPENAPI = "https://openapi.taifex.com.tw/v1"

ENDPOINTS = {
    "futures_daily": "DailyMarketReportFut",
    "options_daily": "DailyMarketReportOpt",
    "put_call_ratio": "PutCallRatio",
    "institutional_futures": "MarketDataOfMajorInstitutionalTradersDetailsOfFuturesContractsBytheDate",
    "institutional_options": "MarketDataOfMajorInstitutionalTradersDetailsOfCallsAndPutsBytheDate",
    "large_traders_futures": "OpenInterestOfLargeTradersFutures",
}


def fetch(name: str) -> list[dict]:
    rows = net.get_json(f"{OPENAPI}/{ENDPOINTS[name]}")
    return rows if isinstance(rows, list) else []


def fetch_all(save: bool = True) -> dict[str, list[dict]]:
    out = {}
    for name in ENDPOINTS:
        try:
            out[name] = fetch(name)
        except Exception as e:  # noqa: BLE001
            log.warning("期交所 %s 抓取失敗：%s", name, e)
            out[name] = []
    if save:
        d = DATA_DIR / "taifex" / now_tw().strftime("%Y-%m-%d")
        d.mkdir(parents=True, exist_ok=True)
        for name, rows in out.items():
            (d / f"{name}.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def summarize(data: dict[str, list[dict]]) -> dict:
    """整理出波段最常看的幾個數字。"""
    s: dict = {}

    pcr = data.get("put_call_ratio") or []
    if pcr:
        latest = max(pcr, key=lambda r: str(pick(r, "Date", "日期", default="")))
        s["pcr_date"] = pick(latest, "Date", "日期")
        s["pcr_oi"] = to_float(pick_contains(latest, "OI", "Ratio") or pick_contains(latest, "未平倉", "比率"))
        s["pcr_volume"] = to_float(pick_contains(latest, "Volume", "Ratio") or pick_contains(latest, "成交量", "比率"))

    # 三大法人台指期淨未平倉（口）
    inst = data.get("institutional_futures") or []
    tx = {}
    for r in inst:
        contract = str(pick(r, "ContractCode", "Contract", "商品名稱", default=""))
        if not any(k in contract for k in ("TX", "臺股期貨", "台股期貨")) or "小" in contract:
            continue
        who = str(pick(r, "Item", "Identity", "身份別", default=""))
        net_oi = to_float(
            pick_contains(r, "OpenInterest", "Net", "Volume")
            or pick_contains(r, "OpenInterest", "Net")
            or pick_contains(r, "未平倉", "淨額", "口數")
        )
        if who and net_oi is not None:
            tx[who] = net_oi
    if tx:
        s["tx_institutional_net_oi"] = tx

    # 前五/前十大交易人台指期淨部位
    large = data.get("large_traders_futures") or []
    for r in large:
        contract = str(pick(r, "Contract", "契約", "商品", default=""))
        if any(k in contract for k in ("TX", "臺股期貨", "台股期貨")) and "小" not in contract:
            s["tx_large_traders_raw"] = r
            break
    return s
