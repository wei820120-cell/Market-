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
    pyramid: bool = False        # 試單＋加減碼模式
    trial_risk: float = 0.01     # 試單風險（權益比例）
    add_levels: tuple = (1.0, 2.0)  # 漲到 +1R、+2R 各加碼一次（口數同試單），停損拉到成本、+1R
    reduce_ma: str = "ma10"      # 加減碼模式：跌破這條線先減碼一半，跌破 exit_ma 全出（空字串＝不減碼）
    add_stops: tuple = (0.0, 1.0)  # 第 k 次加碼後停損＝試單價 + add_stops[k-1]×R（-1＝維持原停損）
    ma_exit_from: int = 2        # 持有第幾天起才檢查「收盤跌破均線」（2＝進場隔天起；1＝進場當天就檢查）
    min_score: int = 0           # 進場門檻：品質分數（0～4）低於此值的訊號不做
    one_lot_risk: float = 0.0    # 例外：2% 算不出 1 口時，只要 1 口的停損虧損 ≤ 權益 × 此比例，仍允許做 1 口（0＝不允許）
    one_lot_min_score: int = 0   # 例外只給品質分數 ≥ 此值的標的
    tier_risk: tuple = ()        # 依品質分數（0～4）決定每筆風險；空＝一律用 risk_pct。分數＝離52週高10%內＋站上向上200日線＋相對強度前20%＋大盤強勢
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
    adds: int = 0            # 已加碼次數
    unit: int = 0            # 每次加碼口數（＝試單口數）
    base: float = 0.0        # 試單進場價（算加碼價位用）
    reduced: bool = False    # 已減碼一半
    reduce_next: bool = False
    feat: dict = field(default_factory=dict)  # 進場前一天的品質條件（只用來事後分析，不影響進出場）


FACTORS = {
    "f_hi": "離 52 週高點 10% 內",
    "f_200": "站上 200 日線且 200 日線向上",
    "f_rs": "近 120 日漲幅排名前 20%",
    "f_vcp": "波動收斂（10 日均振幅 < 50 日均振幅 80%）",
    "f_vol2": "突破量比 ≥ 2",
    "f_tight": "低波動（ATR < 股價 3.5%）",
    "f_mkt": "大盤強勢（站上 20 日線且 20 日線上升）",
}


QUALITY = ("f_hi", "f_200", "f_rs", "f_mkt")  # 回測證明有加分的四項


def prepare(df: pd.DataFrame, p: Params) -> pd.DataFrame:
    d = df.copy()
    d["ma10"], d["ma20"], d["ma60"] = sma(d["close"], 10), sma(d["close"], 20), sma(d["close"], 60)
    d["atr"] = atr(d)
    d["hh"] = d["high"].shift(1).rolling(p.breakout_n).max()
    d["vr"] = d["volume"] / d["volume"].shift(1).rolling(20).mean()
    new_high = d["close"] > d["hh"]
    tr = pd.concat([d["high"] - d["low"], (d["high"] - d["close"].shift(1)).abs(),
                    (d["low"] - d["close"].shift(1)).abs()], axis=1).max(axis=1)
    ma200 = sma(d["close"], 200)
    d["f_hi"] = d["close"] >= 0.9 * d["high"].rolling(252, min_periods=200).max()
    d["f_200"] = (d["close"] > ma200) & (ma200 > ma200.shift(20))
    d["f_vcp"] = tr.rolling(10).mean() < 0.8 * tr.rolling(50).mean()
    d["f_vol2"] = d["vr"] >= 2.0
    d["f_tight"] = d["atr"] < 0.035 * d["close"]
    d["ret120"] = d["close"] / d["close"].shift(120) - 1
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
        if qty < 1 and p.one_lot_risk and dist * mult <= equity * p.one_lot_risk:
            qty = 1
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
    ic = index_df["close"]
    mkt_strong = (ic > sma(ic, 20)) & (sma(ic, 20) > sma(ic, 20).shift(10))
    rs_rank = pd.DataFrame({c: d["ret120"] for c, d in prepped.items()}).rank(axis=1, pct=True)
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
            prev = d.shift(1).loc[day]
            feat = {k: bool(prev[k]) for k in ("f_hi", "f_200", "f_vcp", "f_vol2", "f_tight")}
            feat["f_rs"] = bool(rs_rank[code].shift(1).get(day, 0) >= 0.8)
            feat["f_mkt"] = bool(mkt_strong.shift(1).get(day, False))
            if p.min_score and sum(feat[k] for k in QUALITY) < p.min_score:
                continue
            pp = Params(**{**p.__dict__, "risk_pct": p.trial_risk}) if p.pyramid else p
            if p.tier_risk:
                score = sum(feat[k] for k in QUALITY)
                pp = Params(**{**pp.__dict__, "risk_pct": p.tier_risk[min(score, len(p.tier_risk) - 1)]})
            if p.one_lot_risk:
                ok = sum(feat[k] for k in QUALITY) >= p.one_lot_min_score
                pp = Params(**{**pp.__dict__, "one_lot_risk": p.one_lot_risk if ok else 0.0})
            contract, mult, qty = size(equity, entry, stop, futures.get(code, {}), pp,
                                       rates.get(code, p.margin_rate), used)
            if not qty:
                continue
            entry_cost = cost(entry, mult, qty)  # 記在這筆的損益裡，出場時一起結算
            open_t.append(Trade(code, setup, contract, mult, qty, str(day.date()), entry, stop,
                                realized=-entry_cost, r0=entry - stop, qty0=qty, unit=qty, base=entry, feat=feat))
        pending = []
        # 2) 出場：開盤執行前一天收盤觸發的出場 → 盤中停損 → +2R 先出一半 → 收盤檢查均線、時間停損
        for t in list(open_t):
            d = prepped[t.code]
            if day not in d.index:
                continue
            row = d.loc[day]
            t.days += 1
            px, reason = None, ""
            if p.pyramid:
                px, reason = _pyramid_day(t, row, p, rates, open_t, equity)
            elif t.exit_next:
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
                if t.days >= p.ma_exit_from and row["close"] < row[p.exit_ma]:
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


def run_daytrade(data: dict[str, pd.DataFrame], futures: dict[str, dict], index_df: pd.DataFrame,
                 p: Params | None = None, rates: dict[str, float] | None = None, hold_locked: bool = True,
                 min_quality: int = 0, stop_atr: float = 1.0) -> dict:
    """當沖版（日 K 近似）：盤中突破前 20 日高就進場，收盤全出；收盤漲停鎖住（近似：漲幅 ≥ 9.5% 且收在最高附近）才留倉，隔天開盤出。

    近似與限制：只有日 K，看不到盤中順序，同一天碰到停損就當作先停損；成交價假設在突破價（跳空就用開盤價，開盤高過突破價 3% 以上不追）；
    不用當天的量（盤中看不到全天量），改用前一天的條件（品質分數、站上 60 日線、大盤多頭）。當沖只準備 1 倍原始保證金。
    """
    p = p or Params()
    rates = rates or {}
    pp = Params(**{**p.__dict__, "margin_mult": 1.0})
    prepped = {c: prepare(df, p) for c, df in data.items() if len(df) > 80}
    prev = {c: d.shift(1) for c, d in prepped.items()}
    mkt = market_ok(index_df).shift(1)
    ic = index_df["close"]
    mkt_strong = ((ic > sma(ic, 20)) & (sma(ic, 20) > sma(ic, 20).shift(10))).shift(1)
    rs_rank = pd.DataFrame({c: d["ret120"] for c, d in prepped.items()}).rank(axis=1, pct=True).shift(1)
    dates = sorted(set().union(*[set(d.index) for d in prepped.values()]) & set(index_df.index))
    equity = p.capital
    held: list[Trade] = []
    closed: list[Trade] = []
    curve = []

    def settle(t: Trade, px: float, day, reason: str):
        nonlocal equity
        t.pnl = t.realized + (px - t.entry) * t.mult * t.qty - cost(px, t.mult, t.qty)
        t.r = t.pnl / (t.r0 * t.mult * t.qty0) if t.r0 > 0 else 0
        t.exit, t.reason, t.exit_date = px, reason, str(day.date())
        equity += t.pnl
        closed.append(t)

    for day in dates:
        for t in list(held):  # 昨天留倉的，今天開盤出
            d = prepped[t.code]
            if day in d.index:
                t.days += 1
                settle(t, float(d.loc[day, "open"]), day, "漲停鎖住留倉，隔天開盤出")
                held.remove(t)
        if not bool(mkt.get(day, False)):
            curve.append((day, equity))
            continue
        cands = []
        for code, d in prepped.items():
            if day not in d.index:
                continue
            row, pv = d.loc[day], prev[code].loc[day]
            hh = float(row["hh"]) if not math.isnan(row["hh"]) else None
            if hh is None or math.isnan(pv["atr"]) or not pv["close"] > pv["ma60"] or row["high"] <= hh:
                continue
            entry = max(float(row["open"]), hh)
            if entry > hh * 1.03:
                continue
            feat = {"f_hi": bool(pv["f_hi"]), "f_200": bool(pv["f_200"]),
                    "f_rs": bool(rs_rank[code].get(day, 0) >= 0.8), "f_mkt": bool(mkt_strong.get(day, False))}
            q = sum(feat.values())
            if q < min_quality:
                continue
            cands.append((float(rs_rank[code].get(day, 0) or 0), q, code, entry, feat))
        for _, q, code, entry, feat in sorted(cands, reverse=True)[:p.max_pos]:
            d, row = prepped[code], prepped[code].loc[day]
            stop = entry - stop_atr * float(prev[code].loc[day, "atr"])
            contract, mult, qty = size(equity, entry, stop, futures.get(code, {}), pp, rates.get(code, p.margin_rate), 0.0)
            if not qty:
                continue
            t = Trade(code, "當沖突破", contract, mult, qty, str(day.date()), entry, stop,
                      realized=-cost(entry, mult, qty), r0=entry - stop, qty0=qty, unit=qty, base=entry, feat=feat)
            if row["low"] <= stop:
                settle(t, stop, day, "當沖停損")
                continue
            pc = float(prev[code].loc[day, "close"])
            locked = row["close"] >= pc * 1.095 and row["close"] >= row["high"] * 0.995
            if locked and hold_locked:
                held.append(t)
            else:
                settle(t, float(row["close"]), day, "當沖收盤出")
        mtm = sum((float(prepped[t.code]["close"].get(day, t.entry)) - t.entry) * t.mult * t.qty + t.realized for t in held)
        curve.append((day, equity + mtm))
    return summarize(closed, curve, p, market_ok(index_df))


def _pyramid_day(t: Trade, row, p: Params, rates: dict, open_t: list, equity: float) -> tuple:
    """加減碼模式的一天：開盤先執行前一天的減碼／出場 → 盤中停損 → 加碼 → 收盤檢查均線。回傳（出場價, 原因）。"""
    if t.exit_next:
        return float(row["open"]), t.exit_next
    if t.reduce_next and t.qty >= 2:
        half = t.qty // 2
        px = float(row["open"])
        t.realized += (px - t.entry) * t.mult * half - cost(px, t.mult, half)
        t.qty -= half
        t.reduced, t.reduce_next = True, False
    if row["low"] <= t.stop:
        return min(float(row["open"]), t.stop), ("停損" if t.adds == 0 else "加碼後停損")
    for k, lvl in enumerate(p.add_levels, start=1):
        if t.adds >= k or t.reduced:
            continue
        price = t.base + lvl * t.r0
        if row["high"] < price:
            break
        fill = max(float(row["open"]), price)
        rate = rates.get(t.code, p.margin_rate)
        used = sum(reserve(x.entry, x.mult, x.qty, rates.get(x.code, p.margin_rate), p) for x in open_t)
        add = min(t.unit, int((equity - used) // max(1e-9, fill * t.mult * rate * p.margin_mult)))
        if add < 1:
            break
        t.entry = (t.entry * t.qty + fill * add) / (t.qty + add)
        t.qty += add
        t.qty0 += add
        t.realized -= cost(fill, t.mult, add)
        t.adds = k
        t.stop = max(t.stop, t.base + p.add_stops[min(k, len(p.add_stops)) - 1] * t.r0)
    if t.days > 1 and row["close"] < row[p.exit_ma]:
        t.exit_next = "跌破20日線"
    elif p.reduce_ma and t.days > 1 and not t.reduced and t.adds >= 1 and row["close"] < row[p.reduce_ma]:
        t.reduce_next = True
    elif t.days >= p.time_stop and t.adds == 0 and row["close"] < t.base + t.r0:
        t.exit_next = "時間停損"
    return None, ""


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
