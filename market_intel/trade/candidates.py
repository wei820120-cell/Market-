"""可卡位標的：把一個族群／研究題材裡有股票期貨的股票，逐一檢查訊號與口數，推進出場機器人。

用在三個時機（一條龍）：
1. 族群資金湧入當下（還沒研究也先推）
2. 盤後資金流入前幾名族群
3. 題材研究完成時

每檔：現價與漲跌、離 20 日高、量比、訊號（突破／拉回／接近突破）、停損參考、一般／小型股期一口停損虧損、2% 規則能不能做。
排序：可做且有訊號 → 有訊號但做不了 → 接近突破（距 20 日高 3% 內）→ 其他。
"""
from __future__ import annotations

import pandas as pd

from . import backtest as bt
from .paper import PARAMS


def rows(codes: list[str], hist: dict[str, pd.DataFrame], futures: dict, names: dict, equity: float,
         rates: dict) -> list[dict]:
    p = PARAMS
    out = []
    for code in dict.fromkeys(codes):
        info = futures.get(code) or {}
        df = hist.get(code)
        if not (info.get("std") or info.get("mini")) or df is None or len(df) < 80:
            continue
        d = bt.prepare(df, p)
        r = d.iloc[-1]
        close, a = float(r["close"]), float(r["atr"])
        stop = close - p.atr_mult * a
        contract, mult, qty = bt.size(equity, close, stop, info, p, rates.get(code, p.margin_rate))
        hh = float(r["hh"])
        gap = (hh / close - 1) * 100 if close else None  # 距 20 日高還差幾 %（負數＝已突破）
        signal = "突破" if r["sig_a"] else ("拉回" if r["sig_b"] else ("接近突破" if gap is not None and 0 < gap <= 3 else ""))
        m = bt.MINI if info.get("mini") else bt.STD
        out.append({"code": code, "name": names.get(code, code), "close": close,
                    "pct": float(close / d["close"].iloc[-2] - 1) * 100, "vr": float(r["vr"]), "gap": gap,
                    "signal": signal, "stop": stop, "dist": close - stop, "info": info, "contract": contract,
                    "qty": qty, "risk": (close - stop) * mult * qty if qty else (close - stop) * m,
                    "need": (close - stop) * m / p.risk_pct})
    rank = {"突破": 0, "拉回": 0, "接近突破": 2, "": 3}
    return sorted(out, key=lambda x: (rank[x["signal"]] + (0 if x["qty"] else 1) * (x["signal"] in ("突破", "拉回")),
                                      -x["vr"]))


def message(title: str, context: str, rs: list[dict], market_ok: bool, limit: int = 10) -> str:
    p = PARAMS
    lines = [f"🎯 可卡位標的：{title}"] + ([context] if context else [])
    lines.append("大盤：" + ("🟢 多頭" if market_ok else "🔴 空頭／盤整，依規則先不要下新單"))
    if not rs:
        return "\n".join(lines + ["這個題材裡沒有股票期貨標的"])
    for x in rs[:limit]:
        fut = "／".join(filter(None, [f"一般 {x['info']['std']}" if x["info"].get("std") else "",
                                       f"小型 {x['info']['mini']}" if x["info"].get("mini") else ""]))
        tag = {"突破": "🔥突破", "拉回": "↩️拉回", "接近突破": f"👀差 {x['gap']:.1f}% 突破"}.get(x["signal"], "—")
        lines.append(f"\n{tag} {x['code']} {x['name']} {x['close']:,.2f}（{x['pct']:+.1f}%）量比 {x['vr']:.1f}｜{fut}")
        if x["qty"]:
            lines.append(f"  🟢 可做 {x['contract']} {x['qty']} 口｜停損 {x['stop']:,.2f}｜最大虧損 {x['risk']:,.0f}")
        else:
            lines.append(f"  🟡 停損 {x['stop']:,.2f}｜一口虧 {x['risk']:,.0f} 超過 2% 上限，需本金約 {x['need']:,.0f}")
    lines.append(f"\n規則：只做多、停損 −{p.atr_mult}ATR、+2R 出一半、跌破 20 日線出場｜成交後回報：名稱 價位 口數")
    return "\n".join(lines)
