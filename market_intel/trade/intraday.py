"""盤中進場訊號（第一時間）：股票期貨標的盤中突破 20 日高點且量能步調夠，立即推進出場機器人。

兩段式：
1. ⚡ 盤中提醒：現價 > 前 20 日最高、站上 60 日線、量能步調（累積量 ÷ 20 日均量 × 同時段正常比例）≥ 1.5
2. ✅ 13:20 收盤前確認：盤中提醒過的，13:20 仍站在 20 日高之上且量能步調 ≥ 1.5，再推一次（最接近回測規則）

每則都附：題材、一般／小型股期、停損參考（現價 − 1.5×ATR）、一口停損虧損、2% 規則能不能做（不能做也推，附需要本金）。
大盤濾網不符（空頭／盤整）時只推「不建議進場」。回測規則是收盤確認、隔天開盤進場；盤中突破可能收盤拉回（假突破）。
"""
from __future__ import annotations

import logging
from datetime import datetime

import pandas as pd

from ..analysis.indicators import atr, sma
from . import backtest as bt
from .paper import PARAMS

log = logging.getLogger(__name__)

CONFIRM_AT = "13:20"


def levels(hist: dict[str, pd.DataFrame]) -> dict[str, dict]:
    """用到昨天為止的日 K 算每檔的關卡：20 日高、60 日線、ATR、20 日均量（張）。"""
    out = {}
    for code, df in hist.items():
        if df is None or len(df) < 61:
            continue
        out[code] = {"hh20": float(df["high"].tail(20).max()), "ma60": float(sma(df["close"], 60).iloc[-1]),
                     "atr": float(atr(df).iloc[-1]), "avg_lots": float(df["volume"].tail(20).mean()) / 1000}
    return out


def pace(q, lv: dict, frac: float) -> float | None:
    if not q.volume_lots or not lv.get("avg_lots") or frac <= 0:
        return None
    return q.volume_lots / (lv["avg_lots"] * frac)


def check(quotes: dict, lv_map: dict, frac: float) -> list[dict]:
    """現在符合突破條件的標的。"""
    out = []
    for code, lv in lv_map.items():
        q = quotes.get(code)
        if not q or not q.price:
            continue
        pc = pace(q, lv, frac)
        if q.price > lv["hh20"] and q.price > lv["ma60"] and pc is not None and pc >= PARAMS.vol_ratio:
            out.append({"code": code, "name": q.name, "price": q.price, "pace": pc, "pct": q.change_pct, **lv})
    return sorted(out, key=lambda s: s["pace"], reverse=True)


def message(sig: dict, info: dict, theme: str, equity: float, rate: float, market_ok: bool, kind: str,
            now: datetime) -> str:
    p = PARAMS
    price = sig["price"]
    stop = price - p.atr_mult * sig["atr"]
    dist = price - stop
    head = {"alert": f"⚡ {now:%H:%M} 盤中突破", "confirm": f"✅ {now:%H:%M} 收盤前確認突破"}[kind]
    lines = [f"{head}：{sig['code']} {sig['name']}" + (f"〔{theme}〕" if theme else ""),
             f"現價 {price:,.2f}（{(sig['pct'] or 0):+.1f}%）突破 20 日高 {sig['hh20']:,.2f}｜量能步調 {sig['pace']:.1f} 倍",
             f"停損參考 {stop:,.2f}（−1.5ATR，距離 {dist:,.2f}）｜+2R 約 {price + p.take_r * dist:,.2f}"]
    contract, mult, qty = bt.size(equity, price, stop, info, p, rate)
    fut = []
    for key, m, label in (("mini", bt.MINI, "小型"), ("std", bt.STD, "一般")):
        if info.get(key):
            fut.append(f"{label} {info[key]} 一口停損虧約 {dist * m:,.0f}")
    lines.append("｜".join(fut) if fut else "股票期貨：無")
    if not market_ok:
        lines.append("🔴 大盤空頭／盤整：依規則不開新倉，先不要下單")
    elif qty:
        lines.append(f"🟢 2% 規則可做：{contract} {qty} 口（最大虧損約 {dist * mult * qty:,.0f}｜"
                     f"3 倍保證金約 {bt.reserve(price, mult, qty, rate, p):,.0f}）")
    else:
        m = bt.MINI if info.get("mini") else bt.STD
        lines.append(f"🟡 超過 2% 規則（上限 {equity * p.risk_pct:,.0f}），要做 1 口需本金約 {dist * m / p.risk_pct:,.0f}")
    if kind == "alert":
        lines.append("提醒：盤中突破可能收盤拉回；13:20 仍站穩會再推確認")
    lines.append(f"成交後回報：{sig['name']} 價位 口數")
    return "\n".join(lines)
