"""股票期貨波段策略回測（只做多）。

規則（和進出場機器人用同一套）：
- 大盤濾網：加權指數收盤 > 60 日均線，且 20 日均線 > 60 日均線 → 多頭，才開新倉；否則「空頭／盤整，先不要下單」
- 標的：有股票期貨或小型股票期貨的股票（優先用小型股期，口數算得出來才做）
- 進場型態（訊號日收盤判斷，隔天開盤進場）
  A 突破：收盤創 20 日新高、量比（今日量 ÷ 前 20 日均量）≥ 1.5、收盤 > 60 日均線
  B 拉回：20 日均線 > 60 日均線且向上，近 10 日內創過 20 日新高，今日最低碰到 20 日均線 ±1% 內且收在均線之上
- 停損：進場價 − 1.5 × ATR(14)
- 出場：漲到 +2R 先出一半，停損移到成本；剩下收盤跌破 10 日均線隔天開盤出場；10 個交易日內沒到 +1R 就出場
- 資金：每筆風險 2% 權益；最多同時 3 檔；口數 ＝ 風險金額 ÷（停損距離 × 每口股數）；
  每口準備 3 倍原始保證金，所有持倉的準備金合計不得超過權益（口數會再依此往下調）
- 成本：手續費每口單邊 50 元、期交稅 十萬分之二（單邊）、滑價 0.1%（單邊）

注意：用股票價格代替期貨價格（忽略正逆價差、轉倉）；股期標的用目前清單（有存活者偏差）。結果只是參考。
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..analysis.indicators import atr, sma

log = logging.getLogger(__name__)

STD, MINI = 2000, 100
FEE, TAX, SLIP = 50.0, 0.00002, 0.001


@dataclass
class Params:
    capital: float = 100_000
    risk_pct: float = 0.02
    max_pos: int = 3
    atr_mult: float = 1.5
    breakout_n: int = 20
    vol_ratio: float = 1.5
    take_r: float = 2.0
    time_stop: int = 10
    margin_rate: float = 0.2025  # 沒有個別比例時用最高級距估
    margin_mult: float = 3.0     # 波段單每口準備 3 倍原始保證金（承擔波動），準備金合計不得超過權益
    exit_ma: str = "ma10"        # 移動停利均線：ma10 / ma20
    setups: tuple = ("突破", "拉回")
    trend_filter: bool = False   # 個股也要 20MA > 60MA 才做突破


@dataclass
class Trade:
    code: str
    setup: str
    contract: str
    mult: int
    qty: int
    entry_date: str
    entry: float
    stop: float
    exit_date: str = ""
    exit: float = 0.0
    pnl: float = 0.0
    r: float = 0.0
    reason: str = ""
    half_done: bool = False
    realized: float = 0.0
    days: int = 0
    r0: float = 0.0          # 每股初始風險（進場價 − 初始停損）
    qty0: int = 0            # 初始口數
    exit_next: str = ""      # 收盤觸發、隔天開盤要出場的原因


def prepare(df: pd.DataFrame, p: Params) -> pd.DataFrame:
    d = df.copy()
    d["ma10"], d["ma20"], d["ma60"] = sma(d["close"], 10), sma(d["close"], 20), sma(d["close"], 60)
    d["atr"] = atr(d)
    d["hh"] = d["high"].shift(1).rolling(p.breakout_n).max()
    d["vr"] = d["volume"] / d["volume"].shift(1).rolling(20).mean()
    new_high = d["close"] > d["hh"]
    d["sig_a"] = new_high & (d["vr"] >= p.vol_ratio) & (d["close"] > d["ma60"])
    recent_high = new_high.rolling(10).max().fillna(0).astype(bool)
    d["sig_b"] = ((d["ma20"] > d["ma60"]) & (d["ma20"] > d["ma20"].shift(5)) & recent_high
                  & (d["low"] <= d["ma20"] * 1.01) & (d["close"] > d["ma20"]) & ~new_high)
    return d


def market_ok(index_df: pd.DataFrame) -> pd.Series:
    c = index_df["close"]
    return (c > sma(c, 60)) & (sma(c, 20) > sma(c, 60))


def size(equity: float, entry: float, stop: float, info: dict, p: Params, rate: float | None = None,
         reserved: float = 0.0) -> tuple[str, int, int]:
    """回傳（契約, 每口股數, 口數）。先試小型股期，再試一般股期；算不出 1 口就不做。

    口數同時受兩個限制：風險（停損虧損 ≤ 權益 2%）、準備金（每口 3 倍原始保證金，加上已持倉的準備金 ≤ 權益）。
    """
    risk = equity * p.risk_pct
    dist = entry - stop
    rate = p.margin_rate if rate is None else rate
    room = equity - reserved
    for key, mult in (("mini", MINI), ("std", STD)):
        c = info.get(key)
        if not c or dist <= 0:
            continue
        qty = int(risk // (dist * mult))
        per = entry * mult * rate * p.margin_mult
        if per > 0:
            qty = min(qty, int(room // per))
        if qty >= 1:
            return c, mult, qty
    return "", 0, 0


def reserve(entry: float, mult: int, qty: int, rate: float, p: Params) -> float:
    """這筆持倉要準備的資金：原始保證金 × 3。"""
    return entry * mult * qty * rate * p.margin_mult


def cost(price: float, mult: int, qty: int) -> float:
    return qty * (FEE + price * mult * (TAX + SLIP))


def run(data: dict[str, pd.DataFrame], futures: dict[str, dict], index_df: pd.DataFrame,
        p: Params | None = None, rates: dict[str, float] | None = None) -> dict:
    p = p or Params()
    rates = rates or {}
    prepped = {c: prepare(df, p) for c, df in data.items() if len(df) > 80}
    mkt = market_ok(index_df)
    dates = sorted(set().union(*[set(d.index) for d in prepped.values()]) & set(mkt.index))
    equity = p.capital
    open_t: list[Trade] = []
    closed: list[Trade] = []
    curve = []
    pending: list[tuple[str, str]] = []  # 隔天開盤要進場的（代號, 型態）
    for i, day in enumerate(dates):
        # 1) 開盤進場
        for code, setup in pending:
            if len(open_t) >= p.max_pos or any(t.code == code for t in open_t):
                continue
            d = prepped[code]
            if day not in d.index:
                continue
            row = d.loc[day]
            entry = float(row["open"])
            a = float(d["atr"].shift(1).get(day, np.nan))
            if not entry or math.isnan(a):
                continue
            stop = entry - p.atr_mult * a
            used = sum(reserve(t.entry, t.mult, t.qty, rates.get(t.code, p.margin_rate), p) for t in open_t)
            contract, mult, qty = size(equity, entry, stop, futures.get(code, {}), p,
                                       rates.get(code, p.margin_rate), used)
            if not qty:
                continue
            entry_cost = cost(entry, mult, qty)  # 記在這筆的損益裡，出場時一起結算
            open_t.append(Trade(code, setup, contract, mult, qty, str(day.date()), entry, stop,
                                realized=-entry_cost, r0=entry - stop, qty0=qty))
        pending = []
        # 2) 出場：開盤執行前一天收盤觸發的出場 → 盤中停損 → +2R 先出一半 → 收盤檢查均線、時間停損
        for t in list(open_t):
            d = prepped[t.code]
            if day not in d.index:
                continue
            row = d.loc[day]
            t.days += 1
            px, reason = None, ""
            if t.exit_next:
                px, reason = float(row["open"]), t.exit_next
            elif row["low"] <= t.stop:
                px, reason = min(float(row["open"]), t.stop), ("保本出場" if t.half_done else "停損")
            else:
                if not t.half_done and row["high"] >= t.entry + p.take_r * t.r0:
                    target = max(float(row["open"]), t.entry + p.take_r * t.r0)
                    half = t.qty // 2
                    if half >= 1:
                        t.realized += (target - t.entry) * t.mult * half - cost(target, t.mult, half)
                        t.qty -= half
                    t.half_done, t.stop = True, t.entry
                if t.days > 1 and row["close"] < row[p.exit_ma]:
                    t.exit_next = "跌破均線"
                elif t.days >= p.time_stop and not t.half_done and row["close"] < t.entry + t.r0:
                    t.exit_next = "時間停損"
            if px is not None:
                t.pnl = t.realized + (px - t.entry) * t.mult * t.qty - cost(px, t.mult, t.qty)
                t.r = t.pnl / (t.r0 * t.mult * t.qty0) if t.r0 > 0 else 0
                t.exit, t.reason, t.exit_date = px, reason, str(day.date())
                equity += t.pnl
                open_t.remove(t)
                closed.append(t)
        # 3) 收盤找訊號（大盤多頭才開新倉）
        if bool(mkt.get(day, False)):
            cands = []
            for code, d in prepped.items():
                if day in d.index:
                    row = d.loc[day]
                    if row["sig_a"] and "突破" in p.setups and (not p.trend_filter or row["ma20"] > row["ma60"]):
                        cands.append((float(row["vr"]), code, "突破"))
                    elif row["sig_b"] and "拉回" in p.setups:
                        cands.append((float(row["vr"]) * 0.5, code, "拉回"))
            pending = [(c, s) for _, c, s in sorted(cands, reverse=True)[:p.max_pos * 2]]
        mtm = sum((float(prepped[t.code]["close"].get(day, t.entry)) - t.entry) * t.mult * t.qty + t.realized
                  for t in open_t)
        curve.append((day, equity + mtm))
    return summarize(closed, curve, p, mkt)


def summarize(closed: list[Trade], curve: list, p: Params, mkt: pd.Series) -> dict:
    eq = pd.Series([v for _, v in curve], index=[d for d, _ in curve])
    dd = (eq / eq.cummax() - 1).min() if len(eq) else 0
    wins = [t for t in closed if t.pnl > 0]
    by_setup = {}
    for s in ("突破", "拉回"):
        ts = [t for t in closed if t.setup == s]
        if ts:
            by_setup[s] = {"筆數": len(ts), "勝率": round(sum(t.pnl > 0 for t in ts) / len(ts) * 100, 1),
                           "平均R": round(float(np.mean([t.r for t in ts])), 2),
                           "總損益": round(sum(t.pnl for t in ts))}
    years = max((eq.index[-1] - eq.index[0]).days / 365.25, 0.1) if len(eq) else 1
    double = next((d for d, v in eq.items() if v >= p.capital * 2), None)
    return {
        "期間": f"{eq.index[0].date()} ~ {eq.index[-1].date()}" if len(eq) else "",
        "起始資金": p.capital, "期末權益": round(float(eq.iloc[-1])) if len(eq) else p.capital,
        "報酬率%": round((float(eq.iloc[-1]) / p.capital - 1) * 100, 1) if len(eq) else 0,
        "年化%": round(((float(eq.iloc[-1]) / p.capital) ** (1 / years) - 1) * 100, 1) if len(eq) else 0,
        "最大回撤%": round(float(dd) * 100, 1),
        "交易筆數": len(closed), "勝率%": round(len(wins) / len(closed) * 100, 1) if closed else 0,
        "平均R": round(float(np.mean([t.r for t in closed])), 2) if closed else 0,
        "平均持有天數": round(float(np.mean([t.days for t in closed])), 1) if closed else 0,
        "翻倍日期": str(double.date()) if double is not None else "未翻倍",
        "大盤多頭天數比例%": round(float(mkt.reindex(eq.index).fillna(False).mean()) * 100, 1) if len(eq) else 0,
        "各型態": by_setup,
        "出場原因": pd.Series([t.reason for t in closed]).value_counts().to_dict() if closed else {},
        "trades": [t.__dict__ for t in closed],
        "curve": [(str(d.date()), round(v)) for d, v in curve],
    }


def save(result: dict, path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
