"""波段目標價計算。

同時用幾種方法算，再取「保守 / 積極」兩個目標與一個停損：
1. ATR 波動法：現價 + 3×ATR，停損 現價 − 1.5×ATR
2. 箱型突破等幅測距：突破箱頂後，目標 = 箱頂 + 箱高
3. 費波納契延伸：上升波段 1.272 / 1.618 延伸；下跌波段則看 0.382 / 0.618 反彈
4. 本益比估值：EPS × 目標本益比（有提供 EPS 或本益比時才算）

這些只是依歷史價格與假設推算的參考價位，不保證會到達。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from .indicators import atr, rsi, sma, volume_ratio


@dataclass
class TargetResult:
    symbol: str
    last: float
    trend: str
    atr: float
    ma20: float | None
    ma60: float | None
    rsi14: float | None
    vol_ratio: float | None
    targets: dict[str, float] = field(default_factory=dict)
    stops: dict[str, float] = field(default_factory=dict)
    conservative: float | None = None
    aggressive: float | None = None
    stop: float | None = None
    risk_reward: float | None = None
    notes: list[str] = field(default_factory=list)

    def as_row(self) -> dict:
        pct = lambda x: None if x is None else round((x / self.last - 1) * 100, 1)  # noqa: E731
        return {
            "代號": self.symbol,
            "現價": round(self.last, 2),
            "趨勢": self.trend,
            "保守目標": _r(self.conservative),
            "保守%": pct(self.conservative),
            "積極目標": _r(self.aggressive),
            "積極%": pct(self.aggressive),
            "停損": _r(self.stop),
            "停損%": pct(self.stop),
            "風報比": _r(self.risk_reward),
            "RSI": _r(self.rsi14, 0),
            "量比": _r(self.vol_ratio),
            "備註": "；".join(self.notes),
        }


def _r(x, n: int = 2):
    return None if x is None or pd.isna(x) else round(float(x), n)


def _last(s: pd.Series) -> float | None:
    v = s.iloc[-1] if len(s) else None
    return None if v is None or pd.isna(v) else float(v)


def compute(
    df: pd.DataFrame,
    symbol: str = "",
    lookback: int = 60,
    eps: float | None = None,
    target_pe: float | None = None,
) -> TargetResult:
    if len(df) < 25:
        raise ValueError(f"{symbol} 歷史資料不足（{len(df)} 根 K 棒）")
    df = df.dropna(subset=["high", "low", "close"])
    close = df["close"]
    last = float(close.iloc[-1])
    a = _last(atr(df)) or 0.0
    ma20, ma60 = _last(sma(close, 20)), _last(sma(close, 60))

    if ma20 and ma60 and last > ma20 > ma60:
        trend = "多頭"
    elif ma20 and ma60 and last < ma20 < ma60:
        trend = "空頭"
    else:
        trend = "盤整"

    res = TargetResult(
        symbol=symbol, last=last, trend=trend, atr=a, ma20=ma20, ma60=ma60,
        rsi14=_last(rsi(close)), vol_ratio=_last(volume_ratio(df)) if "volume" in df else None,
    )

    # 1. ATR
    if a:
        res.targets["ATR×3"] = last + 3 * a
        res.stops["ATR×1.5"] = last - 1.5 * a

    # 2. 箱型（不含今天的前 N 日區間）
    win = df.iloc[-(lookback + 1):-1]
    box_hi, box_lo = float(win["high"].max()), float(win["low"].min())
    height = box_hi - box_lo
    if last > box_hi:
        res.targets["箱型突破測距"] = box_hi + height
        res.stops["跌回箱頂"] = box_hi * 0.97
        res.notes.append(f"突破 {lookback} 日箱頂 {box_hi:.2f}")
    else:
        res.targets["箱頂壓力"] = box_hi
        res.stops["箱底"] = box_lo
        if height and (last - box_lo) / height < 0.2:
            res.notes.append("接近箱底")

    # 3. 費波納契
    w = df.iloc[-lookback:]
    i_hi, i_lo = w["high"].values.argmax(), w["low"].values.argmin()
    sw_hi, sw_lo = float(w["high"].iloc[i_hi]), float(w["low"].iloc[i_lo])
    rng = sw_hi - sw_lo
    if rng > 0:
        if i_lo < i_hi:  # 先低後高：上升波
            res.targets["Fib 1.272"] = sw_hi + 0.272 * rng
            res.targets["Fib 1.618"] = sw_hi + 0.618 * rng
            res.stops["Fib 0.618回檔"] = sw_hi - 0.618 * rng
        else:  # 先高後低：下跌波的反彈目標
            res.targets["反彈 0.382"] = sw_lo + 0.382 * rng
            res.targets["反彈 0.618"] = sw_lo + 0.618 * rng

    # 4. 本益比
    if eps and target_pe:
        res.targets[f"本益比 {target_pe:g} 倍"] = eps * target_pe

    ups = sorted(v for v in res.targets.values() if v > last * 1.005)
    downs = sorted(v for v in res.stops.values() if v < last)
    res.conservative = ups[0] if ups else None
    res.aggressive = ups[-1] if ups else None
    res.stop = downs[-1] if downs else None  # 最近的停損位
    if res.conservative and res.stop:
        res.risk_reward = (res.conservative - last) / (last - res.stop)

    if res.rsi14 and res.rsi14 > 80:
        res.notes.append("RSI 過熱")
    if res.vol_ratio and res.vol_ratio > 2:
        res.notes.append(f"爆量 {res.vol_ratio:.1f} 倍")
    if trend == "空頭":
        res.notes.append("空頭排列，目標價僅供反彈參考")
    return res
