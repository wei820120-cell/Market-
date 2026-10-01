"""台股盤後資料：全市場收盤行情、類股成交、三大法人、本益比。

來源：
- 證交所 OpenAPI  https://openapi.twse.com.tw/
- 證交所 rwd 報表  https://www.twse.com.tw/rwd/zh/...
- 櫃買中心 OpenAPI https://www.tpex.org.tw/openapi/
"""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta

import pandas as pd

from .. import net
from ..utils import CACHE_DIR, find_col, now_tw, pick, to_float

log = logging.getLogger(__name__)

TWSE_OPENAPI = "https://openapi.twse.com.tw/v1"
TWSE_RWD = "https://www.twse.com.tw/rwd/zh"
TPEX_OPENAPI = "https://www.tpex.org.tw/openapi/v1"

DAY_COLUMNS = ["code", "name", "market", "open", "high", "low", "close", "change", "volume", "value"]


# ---------- 全市場收盤行情 ----------

def parse_twse_day_all(rows: list[dict]) -> pd.DataFrame:
    out = []
    for r in rows:
        out.append({
            "code": str(pick(r, "Code", "證券代號")).strip(),
            "name": str(pick(r, "Name", "證券名稱")).strip(),
            "market": "tse",
            "open": to_float(pick(r, "OpeningPrice", "開盤價")),
            "high": to_float(pick(r, "HighestPrice", "最高價")),
            "low": to_float(pick(r, "LowestPrice", "最低價")),
            "close": to_float(pick(r, "ClosingPrice", "收盤價")),
            "change": to_float(pick(r, "Change", "漲跌價差")),
            "volume": to_float(pick(r, "TradeVolume", "成交股數")),
            "value": to_float(pick(r, "TradeValue", "成交金額")),
        })
    return pd.DataFrame(out, columns=DAY_COLUMNS)


def parse_tpex_day_all(rows: list[dict]) -> pd.DataFrame:
    out = []
    for r in rows:
        out.append({
            "code": str(pick(r, "SecuritiesCompanyCode", "代號")).strip(),
            "name": str(pick(r, "CompanyName", "名稱")).strip(),
            "market": "otc",
            "open": to_float(pick(r, "Open", "開盤")),
            "high": to_float(pick(r, "High", "最高")),
            "low": to_float(pick(r, "Low", "最低")),
            "close": to_float(pick(r, "Close", "收盤")),
            "change": to_float(pick(r, "Change", "漲跌")),
            "volume": to_float(pick(r, "TradingShares", "成交股數")),
            "value": to_float(pick(r, "TransactionAmount", "成交金額")),
        })
    return pd.DataFrame(out, columns=DAY_COLUMNS)


def fetch_day_all() -> pd.DataFrame:
    """上市 + 上櫃最近一個交易日的全部個股行情。"""
    frames = []
    try:
        frames.append(parse_twse_day_all(net.get_json(f"{TWSE_OPENAPI}/exchangeReport/STOCK_DAY_ALL")))
    except Exception as e:  # noqa: BLE001
        log.warning("上市行情抓取失敗：%s", e)
    try:
        frames.append(parse_tpex_day_all(net.get_json(f"{TPEX_OPENAPI}/tpex_mainboard_daily_close_quotes")))
    except Exception as e:  # noqa: BLE001
        log.warning("上櫃行情抓取失敗：%s", e)
    if not frames:
        return pd.DataFrame(columns=DAY_COLUMNS)
    df = pd.concat(frames, ignore_index=True)
    df["pct"] = df["change"] / (df["close"] - df["change"]) * 100
    return df


# ---------- 代號 → 市場別（上市 tse / 上櫃 otc） ----------

def load_listings(max_age_hours: float = 20) -> dict[str, dict]:
    """回傳 {代號: {"market": "tse"/"otc", "name": 名稱, "prev_value": 前一日成交金額}}，有快取。"""
    path = CACHE_DIR / "listings.json"
    if path.exists():
        age_h = (now_tw().timestamp() - path.stat().st_mtime) / 3600
        if age_h < max_age_hours:
            return json.loads(path.read_text(encoding="utf-8"))
    df = fetch_day_all()
    if df.empty:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    listings = {
        r.code: {"market": r.market, "name": r.name, "prev_value": r.value or 0.0, "prev_close": r.close}
        for r in df.itertuples()
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(listings, ensure_ascii=False), encoding="utf-8")
    return listings


# ---------- 類股成交（官方產業別資金流向） ----------

def parse_rwd_table(payload: dict) -> tuple[list[str], list[list]]:
    """證交所 rwd 報表有 fields/data 或 tables[...] 兩種格式。"""
    if payload.get("fields") and payload.get("data") is not None:
        return payload["fields"], payload["data"]
    for t in payload.get("tables") or []:
        if t.get("fields") and t.get("data"):
            return t["fields"], t["data"]
    return [], []


def parse_sector_turnover(payload: dict) -> pd.DataFrame:
    fields, data = parse_rwd_table(payload)
    if not fields:
        return pd.DataFrame(columns=["sector", "value", "volume"])
    i_name = find_col(fields, "名稱") if find_col(fields, "名稱") is not None else 0
    i_value = find_col(fields, "成交金額")
    i_vol = find_col(fields, "成交股數")
    rows = []
    for d in data:
        name = str(d[i_name]).strip()
        rows.append({
            "sector": name.replace("類指數", "").replace("指數", ""),
            "value": to_float(d[i_value]) if i_value is not None else None,
            "volume": to_float(d[i_vol]) if i_vol is not None else None,
        })
    return pd.DataFrame(rows)


def fetch_sector_turnover(d: date) -> pd.DataFrame:
    """各類指數日成交量值（BFIAMU）。非交易日回傳空表。"""
    payload = net.get_json(f"{TWSE_RWD}/afterTrading/BFIAMU", params={"date": d.strftime("%Y%m%d"), "response": "json"})
    if payload.get("stat") not in (None, "OK"):
        return pd.DataFrame(columns=["sector", "value", "volume"])
    return parse_sector_turnover(payload)


def fetch_sector_turnover_history(days: int = 6, max_lookback: int = 14) -> list[tuple[date, pd.DataFrame]]:
    """往回抓最近 N 個交易日的類股成交，最新的在前。"""
    out = []
    d = now_tw().date()
    for _ in range(max_lookback):
        if d.weekday() < 5:
            try:
                df = fetch_sector_turnover(d)
                if not df.empty:
                    out.append((d, df))
            except Exception as e:  # noqa: BLE001
                log.warning("類股成交 %s 抓取失敗：%s", d, e)
        if len(out) >= days:
            break
        d -= timedelta(days=1)
    return out


# ---------- 三大法人買賣超 ----------

def parse_institutional(payload: dict) -> pd.DataFrame:
    fields, data = parse_rwd_table(payload)
    cols = ["code", "name", "foreign", "trust", "dealer", "total"]
    if not fields:
        return pd.DataFrame(columns=cols)
    i_code = find_col(fields, "代號")
    i_name = find_col(fields, "名稱")
    i_foreign = find_col(fields, "外陸資買賣超股數")
    if i_foreign is None:
        i_foreign = find_col(fields, "外資", "買賣超")
    i_trust = find_col(fields, "投信買賣超股數")
    i_dealer = find_col(fields, "自營商買賣超股數")
    i_total = find_col(fields, "三大法人買賣超股數")
    rows = []
    for d in data:
        row = {
            "code": str(d[i_code]).strip(),
            "name": str(d[i_name]).strip() if i_name is not None else "",
            "foreign": to_float(d[i_foreign]) if i_foreign is not None else None,
            "trust": to_float(d[i_trust]) if i_trust is not None else None,
            "dealer": to_float(d[i_dealer]) if i_dealer is not None else None,
            "total": to_float(d[i_total]) if i_total is not None else None,
        }
        # 找不到自營商欄位時，用 三大法人合計 − 外資 − 投信 推算
        if row["dealer"] is None and None not in (row["total"], row["foreign"], row["trust"]):
            row["dealer"] = row["total"] - row["foreign"] - row["trust"]
        rows.append(row)
    return pd.DataFrame(rows, columns=cols)


def fetch_institutional(d: date) -> pd.DataFrame:
    """上市個股三大法人買賣超（股數）。"""
    payload = net.get_json(
        f"{TWSE_RWD}/fund/T86",
        params={"date": d.strftime("%Y%m%d"), "selectType": "ALLBUT0999", "response": "json"},
    )
    if payload.get("stat") not in (None, "OK"):
        return pd.DataFrame(columns=["code", "name", "foreign", "trust", "dealer", "total"])
    return parse_institutional(payload)


def fetch_latest_institutional(max_lookback: int = 7) -> tuple[date | None, pd.DataFrame]:
    d = now_tw().date()
    for _ in range(max_lookback):
        if d.weekday() < 5:
            try:
                df = fetch_institutional(d)
                if not df.empty:
                    return d, df
            except Exception as e:  # noqa: BLE001
                log.warning("三大法人 %s 抓取失敗：%s", d, e)
        d -= timedelta(days=1)
    return None, pd.DataFrame()


# ---------- 本益比 / 殖利率 / 股價淨值比 ----------

def fetch_valuation() -> pd.DataFrame:
    rows = net.get_json(f"{TWSE_OPENAPI}/exchangeReport/BWIBBU_ALL")
    out = [{
        "code": str(pick(r, "Code", "證券代號")).strip(),
        "pe": to_float(pick(r, "PEratio", "本益比")),
        "dividend_yield": to_float(pick(r, "DividendYield", "殖利率")),
        "pb": to_float(pick(r, "PBratio", "股價淨值比")),
    } for r in rows]
    return pd.DataFrame(out)
