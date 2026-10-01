"""Yahoo Finance 日 K / 盤中價（美股、台股都適用）。

台股代號：上市加 .TW（2330.TW），上櫃加 .TWO（6488.TWO）。
美股報價在非付費來源通常有延遲，盤中請以券商報價為準。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import pandas as pd

from .. import net

log = logging.getLogger(__name__)

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"


def yahoo_symbol(code: str, market: str | None) -> str:
    if market == "otc":
        return f"{code}.TWO"
    if market == "tse":
        return f"{code}.TW"
    return code


def parse_chart(payload: dict) -> tuple[pd.DataFrame, dict]:
    result = ((payload.get("chart") or {}).get("result") or [None])[0]
    if not result:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"]), {}
    ts = result.get("timestamp") or []
    q = ((result.get("indicators") or {}).get("quote") or [{}])[0]
    df = pd.DataFrame({
        "open": q.get("open"),
        "high": q.get("high"),
        "low": q.get("low"),
        "close": q.get("close"),
        "volume": q.get("volume"),
    }, index=pd.to_datetime([datetime.fromtimestamp(t, tz=timezone.utc) for t in ts]))
    df = df.dropna(subset=["close"])
    return df, result.get("meta") or {}


def fetch_chart(symbol: str, range_: str = "1y", interval: str = "1d") -> tuple[pd.DataFrame, dict]:
    payload = net.get_json(CHART_URL.format(symbol=symbol), params={"range": range_, "interval": interval})
    return parse_chart(payload)


def _alternate(symbol: str) -> str | None:
    """台股上市／上櫃代號互換（市場別不確定時用）。"""
    if symbol.endswith(".TWO"):
        return symbol[:-4] + ".TW"
    if symbol.endswith(".TW"):
        return symbol + "O"
    return None


def fetch_many(symbols: list[str], range_: str = "3mo") -> dict[str, pd.DataFrame]:
    """回傳 {原始代號: 日K}；台股代號查不到時自動改試上市／上櫃另一邊。"""
    out = {}
    for s in symbols:
        for sym in filter(None, [s, _alternate(s)]):
            try:
                df, _ = fetch_chart(sym, range_=range_)
                if not df.empty:
                    out[s] = df
                    break
            except Exception as e:  # noqa: BLE001
                log.debug("Yahoo %s 抓取失敗：%s", sym, e)
        else:
            log.warning("Yahoo %s 抓不到日K", s)
    return out
