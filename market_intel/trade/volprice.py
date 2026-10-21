"""題材群「量增價未漲」預警：能不能比放量突破更早一步盯上第一波？

背景：現行進場要等「收盤創 20 日新高＋放量」，這類小型題材股第一波常常已經漲停鎖死、買不到。
這裡檢驗另一種更早的訊號：題材成分股裡，某一檔「成交量突然放大，但股價還沒怎麼漲、也還沒突破」。
（量先價後：有人在默默買，價格還沒反映。）

定義（全部用 t 日收盤的資訊，t+1 開盤買進；回測和實際盤中預警共用門檻）
- 範圍：題材成分股（config/themes.yaml＋研究自動加入的受惠股；成分 ≥ 3 檔的題材），大盤多頭的日子。
- 基本條件 F0：站上 60 日線、近 20 日漲幅 ≤ 15%（還沒漲一段）、離 20 日高還有距離（今天沒有突破）。
- 訊號：F0 且 量比 ≥ a（今日量 ÷ 前 20 日均量）且 今日漲幅在 −1%～+b%（價格沒怎麼動）。
- 對照：F0 且 量比 < 1.2 且同樣的漲幅區間（沒放量）→ 量真的有資訊嗎？
- 題材確認：訊號 ＋ 同題材其他成分股近 3 日有放量上漲的 ≥ 1 檔。

評估（和 launch.py 同一套標籤）：隔天開盤買進，5 個交易日內先碰到 +7%（A）或 +2R（B，R＝1.5×ATR）、且之前沒碰到停損；平均 R；
另外看「進場後 10 日內有沒有出現現行的放量突破、從進場到突破日已經漲多少」（＝比突破追進場早多少）。
注意：同一天同題材的樣本彼此相關，有效獨立樣本比筆數少；題材成分是用現在的清單回頭套（有後見之明）；日 K 只能近似盤中預警。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

LOOKAHEAD = 10                       # 進場後幾天內有沒有出現現行放量突破
GRID = ((2.0, 0.03), (2.0, 0.05), (3.0, 0.03), (3.0, 0.05))   # （量比下限 a，漲幅上限 b）
MIN_MEMBERS, MAX_RET20, CTRL_VR, MIN_RET1 = 3, 0.15, 1.2, -0.01
PEER_VR, PEER_DAYS = 1.5, 3


def members(themes_tw: dict, cols, min_members: int = MIN_MEMBERS) -> dict[str, list[str]]:
    have = set(map(str, cols))
    out = {}
    for t, v in themes_tw.items():
        ms = list(dict.fromkeys(str(c) for c in v if str(c) in have))
        if len(ms) >= min_members:
            out[t] = ms
    return out


def peer_hot(fr: dict[str, pd.DataFrame], mem: dict[str, list[str]], min_vr: float = PEER_VR, days: int = PEER_DAYS) -> pd.DataFrame:
    """每檔股票：同題材『其他』成分股近 days 日有放量上漲（量比 ≥ min_vr 且收紅）的檔數（取各題材最大值）。"""
    ret1 = fr["close"] / fr["close"].shift(1) - 1
    hot = ((fr["vr"] >= min_vr) & (ret1 > 0)).astype(float).rolling(days, min_periods=1).max()
    out = pd.DataFrame(0.0, index=hot.index, columns=hot.columns)
    for ms in mem.values():
        tot = hot[ms].sum(axis=1)
        for c in ms:
            out[c] = np.maximum(out[c], tot - hot[c])
    return out


def base_mask(fr: dict[str, pd.DataFrame], mkt: pd.Series, uni_cols: list[str], max_ret20: float = MAX_RET20) -> pd.DataFrame:
    cols = list(fr["close"].columns)
    f0 = (fr["close"] > fr["ma60"]) & (fr["ret20"] <= max_ret20) & (fr["gap"] > 0)
    have = set(uni_cols)
    uni = np.array([c in have for c in cols])[None, :]
    okm = mkt.reindex(fr["close"].index).fillna(False).to_numpy(dtype=bool)[:, None]
    return f0 & uni & okm


def signal_mask(fr: dict[str, pd.DataFrame], f0: pd.DataFrame, min_vr: float, max_ret1: float) -> pd.DataFrame:
    ret1 = fr["close"] / fr["close"].shift(1) - 1
    return f0 & (fr["vr"] >= min_vr) & (ret1 >= MIN_RET1) & (ret1 <= max_ret1)


def control_mask(fr: dict[str, pd.DataFrame], f0: pd.DataFrame, max_ret1: float) -> pd.DataFrame:
    ret1 = fr["close"] / fr["close"].shift(1) - 1
    return f0 & (fr["vr"] < CTRL_VR) & (ret1 >= MIN_RET1) & (ret1 <= max_ret1)


def _arr(df: pd.DataFrame) -> np.ndarray:
    return df.to_numpy(dtype=float)


def breakout_follow(fr: dict[str, pd.DataFrame], mask: pd.DataFrame, lookahead: int = LOOKAHEAD) -> dict:
    """訊號之後 lookahead 日內有沒有出現現行的放量突破（sig_a）、從隔天開盤進場到突破日收盤已經漲多少。"""
    sig_a = fr["sig_a"].to_numpy(dtype=bool)
    op, cl = _arr(fr["open"]), _arr(fr["close"])
    ii, jj = np.nonzero(mask.to_numpy(dtype=bool))
    n = len(ii)
    hit, prem = 0, []
    for i, j in zip(ii, jj):
        if i + 1 >= len(op) or not op[i + 1, j] > 0:
            n -= 1
            continue
        for k in range(i + 1, min(i + 1 + lookahead, len(op))):
            if sig_a[k, j]:
                hit += 1
                if cl[k, j] > 0:
                    prem.append(cl[k, j] / op[i + 1, j] - 1)
                break
    return {"後續突破比例%": round(hit / n * 100, 1) if n else None,
            "進場到突破日已漲%": round(float(np.mean(prem)) * 100, 1) if prem else None}


def stats(mask: pd.DataFrame, labels: dict, fr: dict[str, pd.DataFrame]) -> dict:
    """一組訊號的成績。labels：launch.label_frame 的 hit／hit_r／rr／fwd。"""
    m = mask.to_numpy(dtype=bool) & ~np.isnan(_arr(labels["hit"]))
    out = {"筆數": int(m.sum()), "天數": int(m.any(axis=1).sum())}
    if not m.any():
        return out
    pick = lambda df: _arr(df)[m]  # noqa: E731
    fwd = pick(labels["fwd"])
    out.update({"發動率A%": round(float(np.nanmean(pick(labels["hit"]))) * 100, 1),
                "發動率B%": round(float(np.nanmean(pick(labels["hit_r"]))) * 100, 1),
                "平均R": round(float(np.nanmean(pick(labels["rr"]))), 2),
                "平均5日%": round(float(np.nanmean(fwd)) * 100, 2),
                "5日勝率%": round(float(np.nanmean(fwd > 0)) * 100, 1),
                "近20日已漲%": round(float(np.nanmean(_arr(fr["ret20"])[m])) * 100, 1)})
    return out


def half_masks(idx: pd.DatetimeIndex, embargo_days: int = 5) -> tuple[np.ndarray, np.ndarray]:
    """時間切半（前半段尾巴空 embargo 天，避免標籤重疊）。"""
    cut = len(idx) // 2
    first = np.zeros(len(idx), dtype=bool)
    second = np.zeros(len(idx), dtype=bool)
    first[: max(cut - embargo_days, 0)] = True
    second[cut:] = True
    return first, second


def run(fr: dict[str, pd.DataFrame], mkt: pd.Series, themes_tw: dict, labels: dict, grid=GRID) -> dict:
    mem = members(themes_tw, fr["close"].columns)
    uni = sorted({c for ms in mem.values() for c in ms})
    f0 = base_mask(fr, mkt, uni)
    peers = peer_hot(fr, mem)
    first, second = half_masks(fr["close"].index)
    res = {"題材數": len(mem), "成分股數": len(uni), "基準F0": stats(f0, labels, fr), "組合": [], "前後半": []}
    base_a = res["基準F0"].get("發動率A%")
    cases = []
    for a, b in grid:
        sig = signal_mask(fr, f0, a, b)
        cases.append((f"量比≥{a:g}、漲幅≤{b:.0%}", sig))
        cases.append((f"　＋同題材近3日有放量上漲≥1檔", sig & (peers >= 1)))
        cases.append((f"對照：量比<{CTRL_VR:g}、漲幅≤{b:.0%}（沒放量）", control_mask(fr, f0, b)))
    for name, mask in cases:
        row = {"訊號": name, **stats(mask, labels, fr), **breakout_follow(fr, mask)}
        if base_a and row.get("發動率A%") is not None:
            row["倍數(對F0)"] = round(row["發動率A%"] / base_a, 2)
        res["組合"].append(row)
        for tag, hm in (("前半", first), ("後半", second)):
            sub = mask.copy()
            sub.iloc[~hm] = False
            res["前後半"].append({"訊號": name, "段": tag, **stats(sub, labels, fr)})
    return res


# ---------------------------------------------------------------- 實際盤中預警

def watch_candidates(quotes: dict, lv_map: dict, themes_tw: dict, frac: float, min_pace: float = 2.0, max_pct: float = 3.0,
                     min_pct: float = -1.0, max_ret20: float = MAX_RET20, min_members: int = 2) -> list[dict]:
    """盤中預警：題材成分股「量能步調夠大、價格還沒怎麼動、還沒突破 20 日高」。

    quotes：{代號: 即時報價（price／change_pct／volume_lots／name）}；lv_map：intraday.levels() 的每檔關卡（hh20、ma60、avg_lots、ret20）。
    frac：到目前為止一般交易日累積成交量的正常比例（sector_flow.expected_fraction）。
    """
    mem = members(themes_tw, lv_map.keys(), min_members=min_members)
    theme_of: dict[str, list[str]] = {}
    for t, ms in mem.items():
        for c in ms:
            theme_of.setdefault(c, []).append(t)
    out = []
    for code, lv in lv_map.items():
        q = quotes.get(code)
        if not q or not q.price or code not in theme_of or not lv.get("avg_lots") or frac <= 0:
            continue
        if not q.volume_lots:
            continue
        pace = q.volume_lots / (lv["avg_lots"] * frac)
        pct = q.change_pct if q.change_pct is not None else 0.0
        if pace < min_pace or not (min_pct <= pct <= max_pct):
            continue
        if q.price >= lv["hh20"] or q.price <= lv["ma60"]:                     # 已經突破（別的訊號負責）或趨勢沒站上 60 日線
            continue
        if lv.get("ret20") is not None and lv["ret20"] > max_ret20:           # 已經漲一段
            continue
        out.append({"code": code, "name": q.name, "price": q.price, "pct": pct, "pace": pace, "themes": theme_of[code],
                    "gap": lv["hh20"] / q.price - 1})
    return sorted(out, key=lambda s: -s["pace"])
