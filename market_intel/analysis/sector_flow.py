"""資金流向：找出資金正在流入／流出哪些族群。

- 盤中（即時）：族群成交金額「步調」＝ 目前累積成交金額 ÷（昨日全日成交金額 × 此時段正常應有的比例）
  步調 > 1 代表比平常更多錢湧入；配合漲跌判斷是「流入（價漲量增）」或「出貨（價跌量增）」。
- 盤後：族群今日成交金額 vs 前 5 日平均、官方類股成交比重變化、三大法人買賣超金額依族群加總。
- 美股：類股 ETF 成交金額 vs 20 日均額、漲跌幅。
"""
from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

# 台股一般盤 09:00–13:30 共 270 分鐘的「累積成交量比例」經驗曲線（開盤、尾盤量大）。
# 用來把盤中累積量換算成「與平常同時段相比」的倍數，數值為近似值，可依自己的統計調整。
VOLUME_CURVE = [(0, 0.0), (5, 0.08), (30, 0.25), (60, 0.37), (120, 0.55), (180, 0.69), (240, 0.81), (265, 0.88), (270, 1.0)]


def expected_fraction(now: datetime) -> float:
    minutes = (now.hour - 9) * 60 + now.minute + now.second / 60
    minutes = min(max(minutes, 0), 270)
    xs, ys = zip(*VOLUME_CURVE)
    return float(np.interp(minutes, xs, ys))


FLAT_PCT = 0.3  # 漲跌幅在 ±0.3% 內視為平盤，避免小跌就判成放量下跌


def label(pace: float | None, chg: float | None) -> str:
    if pace is None or chg is None:
        return "—"
    if pace >= 1.3 and chg > FLAT_PCT:
        return "資金流入🔥" if pace >= 2 else "資金流入"
    if pace >= 1.3 and chg < -FLAT_PCT:
        return "放量下跌⚠️"
    if pace >= 1.3:
        return "放量平盤"
    if pace < 0.7 and chg < -FLAT_PCT:
        return "量縮下跌"
    if pace < 0.7:
        return "量縮"
    return "持平"


def theme_flow_intraday(quotes: dict, themes: dict[str, list], listings: dict[str, dict], now: datetime) -> pd.DataFrame:
    frac = expected_fraction(now)
    rows = []
    for theme, codes in themes.items():
        qs = [quotes[str(c)] for c in codes if str(c) in quotes]
        if not qs:
            continue
        turnover = sum(q.turnover for q in qs)
        prev = sum((listings.get(q.code) or {}).get("prev_value") or 0 for q in qs)
        pace = turnover / (prev * frac) if prev and frac > 0 else None
        chgs = [q.change_pct for q in qs if q.change_pct is not None]
        # 以成交金額加權的漲跌幅，大型股影響較大
        w = [(q.change_pct, q.turnover) for q in qs if q.change_pct is not None and q.turnover]
        wchg = sum(c * t for c, t in w) / sum(t for _, t in w) if w else (float(np.mean(chgs)) if chgs else None)
        leaders = sorted((q for q in qs if q.change_pct is not None), key=lambda q: q.change_pct, reverse=True)[:3]
        rows.append({
            "族群": theme,
            "檔數": len(qs),
            "成交金額(億)": round(turnover / 1e8, 2),
            "量能步調": round(pace, 2) if pace is not None else None,
            "超額資金(億)": round((turnover - prev * frac) / 1e8, 2) if prev else None,
            "加權漲跌%": round(wchg, 2) if wchg is not None else None,
            "上漲/下跌": f"{sum(c > 0 for c in chgs)}/{sum(c < 0 for c in chgs)}",
            "領漲": "、".join(f"{q.name}{q.change_pct:+.1f}%" for q in leaders),
            "判讀": label(pace, wchg),
        })
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    return df.sort_values(["超額資金(億)", "加權漲跌%"], ascending=False, na_position="last").reset_index(drop=True)


def theme_flow_daily(histories: dict[str, pd.DataFrame], themes: dict[str, list], names: dict[str, str] | None = None) -> pd.DataFrame:
    """histories：{代號: 日K DataFrame}，以收盤 × 成交量估算成交金額。"""
    names = names or {}
    rows = []
    for theme, codes in themes.items():
        today_v = base_v = 0.0
        chg_w, ret5, members = [], [], []
        for c in map(str, codes):
            df = histories.get(c)
            if df is None or len(df) < 7:
                continue
            value = df["close"] * df["volume"]
            tv, bv = float(value.iloc[-1]), float(value.iloc[-6:-1].mean())
            pct = float(df["close"].iloc[-1] / df["close"].iloc[-2] - 1) * 100
            today_v += tv
            base_v += bv
            chg_w.append((pct, tv))
            ret5.append(float(df["close"].iloc[-1] / df["close"].iloc[-6] - 1) * 100)
            members.append((names.get(c, c), pct))
        if not members:
            continue
        ratio = today_v / base_v if base_v else None
        wchg = sum(p * v for p, v in chg_w) / sum(v for _, v in chg_w) if sum(v for _, v in chg_w) else None
        top = sorted(members, key=lambda m: m[1], reverse=True)[:3]
        rows.append({
            "族群": theme,
            "檔數": len(members),
            "成交金額(億)": round(today_v / 1e8, 2),
            "量比(對5日均)": round(ratio, 2) if ratio else None,
            "資金增減(億)": round((today_v - base_v) / 1e8, 2),
            "加權漲跌%": round(wchg, 2) if wchg is not None else None,
            "5日漲跌%": round(float(np.mean(ret5)), 2),
            "領漲": "、".join(f"{n}{p:+.1f}%" for n, p in top),
            "判讀": label(ratio, wchg),
        })
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    return df.sort_values(["資金增減(億)"], ascending=False).reset_index(drop=True)


def official_sector_flow(history: list[tuple]) -> pd.DataFrame:
    """history：[(日期, 類股成交 DataFrame), ...] 最新在前。比較今日成交比重與前幾日平均比重。"""
    if not history:
        return pd.DataFrame()
    def shares(df: pd.DataFrame) -> pd.Series:
        s = df.dropna(subset=["value"]).set_index("sector")["value"]
        s = s[~s.index.str.contains("發行量加權|未含|報酬")]  # 排除大盤總計類指數
        return s / s.sum() * 100, s
    today_share, today_value = shares(history[0][1])
    prev = [shares(df) for _, df in history[1:]]
    out = pd.DataFrame({"類股": today_share.index, "成交金額(億)": (today_value / 1e8).round(2).values, "成交比重%": today_share.round(2).values})
    if prev:
        avg_share = pd.concat([p[0] for p in prev], axis=1).mean(axis=1)
        avg_value = pd.concat([p[1] for p in prev], axis=1).mean(axis=1)
        out["比重變化(百分點)"] = (today_share - avg_share.reindex(today_share.index)).round(2).values
        out["量比(對前幾日均)"] = (today_value / avg_value.reindex(today_value.index)).round(2).values
        out = out.sort_values("比重變化(百分點)", ascending=False)
    return out.reset_index(drop=True)


def institutional_by_theme(inst: pd.DataFrame, closes: dict[str, float], themes: dict[str, list]) -> pd.DataFrame:
    """三大法人買賣超（股數）× 收盤價 → 依族群加總成金額（億）。"""
    if inst.empty:
        return pd.DataFrame()
    inst = inst.drop_duplicates("code").set_index("code")
    rows = []
    for theme, codes in themes.items():
        agg = {"foreign": 0.0, "trust": 0.0, "dealer": 0.0, "total": 0.0}
        n = 0
        for c in map(str, codes):
            if c not in inst.index or c not in closes or closes[c] is None:
                continue
            n += 1
            for k in agg:
                v = inst.at[c, k]
                if v is not None and not pd.isna(v):
                    agg[k] += float(v) * closes[c]
        if n:
            rows.append({
                "族群": theme, "檔數": n,
                "外資(億)": round(agg["foreign"] / 1e8, 2),
                "投信(億)": round(agg["trust"] / 1e8, 2),
                "自營商(億)": round(agg["dealer"] / 1e8, 2),
                "三大法人合計(億)": round(agg["total"] / 1e8, 2),
            })
    df = pd.DataFrame(rows)
    return df.sort_values("三大法人合計(億)", ascending=False).reset_index(drop=True) if not df.empty else df


def us_etf_flow(histories: dict[str, pd.DataFrame], labels: dict[str, str]) -> pd.DataFrame:
    rows = []
    for sym, df in histories.items():
        if len(df) < 22:
            continue
        dv = df["close"] * df["volume"]
        ratio = float(dv.iloc[-1] / dv.iloc[-21:-1].mean())
        chg = float(df["close"].iloc[-1] / df["close"].iloc[-2] - 1) * 100
        ret5 = float(df["close"].iloc[-1] / df["close"].iloc[-6] - 1) * 100
        rows.append({
            "ETF": sym, "類股": labels.get(sym, ""),
            "漲跌%": round(chg, 2), "5日%": round(ret5, 2),
            "成交額(百萬美元)": round(float(dv.iloc[-1]) / 1e6, 1),
            "額比(對20日均)": round(ratio, 2),
            "判讀": label(ratio, chg),
        })
    df = pd.DataFrame(rows)
    return df.sort_values(["額比(對20日均)"], ascending=False).reset_index(drop=True) if not df.empty else df
