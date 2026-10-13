"""提前進場訊號：在「突破 20 日高」之前就先盯上的方法。回測和實際推播共用同一套定義。

現行進場（trade/backtest.py）是收盤創 20 日新高＋放量才隔天進場，抓到時常常已經漲了一段。這裡是幾種更早的做法：

- 領先（美股）：前一晚美股的領先指標（config/us_lead.yaml）大漲，台股對應族群裡「還沒跟上」的股票，隔天開盤進場。
- 補漲（題材）：同一個族群裡有龍頭剛放量突破，族群裡其他「還沒漲」的股票（站上 60 日線、近 20 日漲幅小）。
- 預突破：離 20 日高不到 3%、相對強度前 20%、均線多頭排列，但還沒突破。
- RS 新高：股價相對大盤的強度已創 60 日新高，價格卻還沒突破 20 日高（強度先於價格）。
- 盤中突破進場：不等收盤確認，價格碰到前 20 日高就進（日 K 近似，在 backtest.run 的 intraday 模式）。

訊號都是「訊號日收盤產生 → 隔天開盤進場」（領先訊號是美股收盤後、台股開盤前就知道），用同一套停損與出場。
注意：題材成分股、領先對照是用「現在的清單」回頭套用，有後見之明（存活者偏差），回測只是參考。
"""
from __future__ import annotations

from collections import defaultdict

import numpy as np
import pandas as pd

from . import backtest as bt

# 實際推播的門檻（回測用同一組預設）
LEAD_MAX_RET5, LEAD_MAX_RET20 = 0.06, 0.20     # 台股對應股：近 5 日漲幅 ≤ 6%、近 20 日 ≤ 20% 才算「還沒跟上」
LAG_DAYS, LAG_MAX_RET20, LAG_MAX_GAP = 3, 0.10, 0.10   # 龍頭 3 日內突破；補漲股近 20 日漲幅 ≤ 10%、離 20 日高 ≤ 10%
LAG_MIN_VR = 0.8
LEAD_NOTE = ("說明：這是觀察名單，不是進場訊號。回測（近 3 年）這類領先訊號隔天開盤買進，進場時平均只漲 4%（現行突破已漲約 20%），"
             "單獨做每筆平均約 +0.6R、勝率約 45%；放在 3 檔之外「另加 1 檔」專用時每筆約 +0.4～0.8R。"
             "但只有 3 年、成分股是用現在的清單回頭套，僅供參考。實單仍照策略 B（2% 風險、大盤濾網、最多 3 檔），要不要多開 1 檔請自己決定。")
LAG_NOTE = "（回測：同題材補漲單獨做不賺錢，要搭配相對強度前段班才有效；當觀察名單，停損與口數照 2% 規則。）"


# ---------------------------------------------------------------- 寬表

def frames(prepped: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """把每檔的 prepare() 結果攤成「日期 × 代號」的寬表，方便一次算全部股票。"""
    def wide(col: str) -> pd.DataFrame:
        return pd.DataFrame({c: d[col] for c, d in prepped.items()})

    f = {k: wide(k) for k in ("open", "high", "low", "close", "ma20", "ma60", "hh", "vr", "ret120", "atr")}
    f["sig_a"] = wide("sig_a").fillna(False).astype(bool)
    f["sig_b"] = wide("sig_b").fillna(False).astype(bool)
    f["f_hi"] = wide("f_hi").fillna(False).astype(bool)
    f["ret5"] = f["close"] / f["close"].shift(5) - 1
    f["ret20"] = f["close"] / f["close"].shift(20) - 1
    f["gap"] = f["hh"] / f["close"] - 1          # 離 20 日高還差幾 %（≤0＝已經在高點之上）
    f["rs"] = f["ret120"].rank(axis=1, pct=True)  # 近 120 日漲幅排名（0～1）
    return f


# ---------------------------------------------------------------- 訊號

def base_signals(fr: dict[str, pd.DataFrame]) -> dict:
    """現行的突破／拉回訊號（和 backtest.run 同一個定義），用來和新方法放在一起比。"""
    out: dict = defaultdict(list)
    for name, key, w in (("突破", "sig_a", 1.0), ("拉回", "sig_b", 0.5)):
        sig = fr[key] if name == "突破" else fr[key] & ~fr["sig_a"]
        for ts, row in sig.iterrows():
            for c in row[row].index:
                out[ts].append((float(fr["vr"].at[ts, c]) * w, c, name))
    return out


def _norm_index(idx: pd.DatetimeIndex) -> dict:
    return {ts.normalize(): ts for ts in idx}


def lead_signals(us_hist: dict[str, pd.DataFrame], lead_map: dict, themes_tw: dict, fr: dict[str, pd.DataFrame],
                 max5: float = LEAD_MAX_RET5, max20: float = LEAD_MAX_RET20, pct_scale: float = 1.0,
                 min_vr: float = 0.0, require_trend: bool = True, min_rs: float = 0.0, need_hi: bool = False,
                 score_rs: bool = False) -> dict:
    """美股領先：美股某個交易日的漲幅達門檻 → 同一個日曆日的台股收盤後產生訊號、隔天開盤進場。

    台股對應股要「還沒跟上」：近 5 日、20 日漲幅不大，且站上 60 日線（趨勢沒壞）。
    """
    out: dict = defaultdict(list)
    cols = set(fr["close"].columns)
    ok = (fr["ret5"] <= max5) & (fr["ret20"] <= max20)
    if require_trend:
        ok &= fr["close"] > fr["ma60"]
    if min_rs:
        ok &= fr["rs"].fillna(0) >= min_rs      # 自己的相對強度（近 120 日漲幅排名）夠高
    if need_hi:
        ok &= fr["f_hi"]                         # 離 52 週高點 10% 內
    tw_days = _norm_index(fr["close"].index)
    for sym, spec in lead_map.items():
        df = us_hist.get(sym)
        if df is None or len(df) < 25:
            continue
        pct = df["close"].pct_change() * 100
        vr = df["volume"] / df["volume"].shift(1).rolling(20).mean()
        trig = (pct >= float(spec.get("pct", 3.0)) * pct_scale) & (vr.fillna(0) >= min_vr)
        members = [str(c) for th in spec.get("themes", []) for c in themes_tw.get(th, []) if str(c) in cols]
        members = list(dict.fromkeys(members))
        if not members:
            continue
        for uts in trig[trig].index:
            ts = tw_days.get(uts.normalize())
            if ts is None:
                continue
            row = ok.loc[ts, members]
            for c in row[row].index:
                us_score = float(pct.loc[uts]) / float(spec.get("pct", 3.0))  # 分數＝漲幅 ÷ 門檻
                rs = fr["rs"].at[ts, c]
                out[ts].append((0.0 if score_rs and pd.isna(rs) else (float(rs) if score_rs else us_score), c, "領先"))
    return out


def leaders(fr: dict[str, pd.DataFrame], days: int = LAG_DAYS) -> pd.DataFrame:
    """每天、每檔：最近 days 天內有沒有放量突破（sig_a）。"""
    return fr["sig_a"].astype(float).rolling(days, min_periods=1).max().astype(bool)


def laggard_signals(themes_tw: dict, fr: dict[str, pd.DataFrame], days: int = LAG_DAYS, max20: float = LAG_MAX_RET20,
                    max_gap: float = LAG_MAX_GAP, min_vr: float = LAG_MIN_VR, min_members: int = 3,
                    min_rs: float = 0.0) -> dict:
    """題材補漲：族群裡有龍頭剛放量突破，其他還沒漲的成分股（站上 60 日線、近 20 日漲幅小、離 20 日高不遠）。"""
    out: dict = defaultdict(list)
    cols = set(fr["close"].columns)
    lead = leaders(fr, days)
    ok = ((fr["close"] > fr["ma60"]) & (fr["ret20"] <= max20) & (fr["gap"] <= max_gap) & (fr["vr"] >= min_vr)
          & (fr["rs"].fillna(0) >= min_rs))
    best: dict = {}
    for theme, members in themes_tw.items():
        ms = list(dict.fromkeys(str(c) for c in members if str(c) in cols))
        if len(ms) < min_members:
            continue
        on = lead[ms].any(axis=1)
        elig = ok[ms] & ~lead[ms]
        for ts in on[on].index:
            row = elig.loc[ts]
            for c in row[row].index:
                rs = fr["rs"].at[ts, c]
                best[(ts, c)] = max(best.get((ts, c), -1.0), 0.0 if pd.isna(rs) else float(rs))
    for (ts, c), sc in best.items():
        out[ts].append((sc, c, "補漲"))
    return out


def prebreak_signals(fr: dict[str, pd.DataFrame], max_gap: float = 0.03, min_rs: float = 0.8) -> dict:
    """預突破：離 20 日高 0～3%、相對強度前 20%、收盤在 20 日線之上且 20 日線 > 60 日線，今天還沒放量突破。"""
    cond = ((fr["gap"] > 0) & (fr["gap"] <= max_gap) & (fr["close"] > fr["ma20"]) & (fr["ma20"] > fr["ma60"])
            & (fr["rs"].fillna(0) >= min_rs) & ~fr["sig_a"])
    out: dict = defaultdict(list)
    for ts, row in cond.iterrows():
        for c in row[row].index:
            out[ts].append((float(fr["rs"].at[ts, c]), c, "預突破"))
    return out


def rsline_signals(fr: dict[str, pd.DataFrame], index_close: pd.Series, window: int = 60, max_gap: float = 0.05) -> dict:
    """RS 新高：股價 ÷ 大盤（相對強度線）創 window 日新高，價格離 20 日高 0～5%、還沒突破。強度先於價格。"""
    idx = index_close.reindex(fr["close"].index).ffill()
    ratio = fr["close"].div(idx, axis=0)
    newhigh = ratio >= ratio.rolling(window, min_periods=window).max()
    cond = (newhigh & (fr["gap"] > 0) & (fr["gap"] <= max_gap) & (fr["close"] > fr["ma60"])
            & (fr["ma20"] > fr["ma60"]) & ~fr["sig_a"])
    out: dict = defaultdict(list)
    for ts, row in cond.iterrows():
        for c in row[row].index:
            rs = fr["rs"].at[ts, c]
            out[ts].append((0.0 if pd.isna(rs) else float(rs), c, "RS新高"))
    return out


def merge(*sigs: dict) -> dict:
    """合併多種訊號。每種訊號的分數單位不同（美股漲幅、相對強度…），所以合併時依「各自當天的名次」輪流排：
    每種的第 1 名、再每種的第 2 名…，避免某一種訊號量大就把其他種擠掉。"""
    out: dict = defaultdict(list)
    for ts in set().union(*[set(s) for s in sigs]) if sigs else ():
        queues = [sorted(s.get(ts, []), reverse=True) for s in sigs]
        for r in range(max(len(q) for q in queues)):
            for ti, q in enumerate(queues):
                if r < len(q):
                    _, c, name = q[r]
                    out[ts].append((1.0 / (1 + r) - ti * 1e-6, c, name))
    return out


# ---------------------------------------------------------------- 不受口數、持倉數限制的「訊號品質」比較

HORIZONS = (1, 3, 5, 10)


def forward_stats(signals: dict, fr: dict[str, pd.DataFrame], mkt: pd.Series, horizons=HORIZONS) -> pd.DataFrame:
    """每種訊號：隔天開盤買進後，持有 1／3／5／10 日（收盤）的平均報酬，以及訊號當下「已經漲了多少」。

    只算大盤多頭的日子（和進場規則一致）。「全部股票」是基準：同一批股票、同樣日子隨便買的平均。
    """
    idx = fr["close"].index
    pos = {ts: i for i, ts in enumerate(idx)}
    op, cl = fr["open"].to_numpy(), fr["close"].to_numpy()
    cols = {c: j for j, c in enumerate(fr["close"].columns)}
    rows = []

    def fwd(i: int, j: int, h: int) -> float:
        k = i + 1 + h - 1
        if k >= len(idx) or np.isnan(op[i + 1, j]) or np.isnan(cl[k, j]) or op[i + 1, j] <= 0:
            return np.nan
        return cl[k, j] / op[i + 1, j] - 1

    by: dict[str, list] = defaultdict(list)
    for ts, items in signals.items():
        if ts not in pos or not bool(mkt.get(ts, False)) or pos[ts] + 1 >= len(idx):
            continue
        for _, c, name in items:
            by[name].append((pos[ts], cols[c], c, ts))
    for name, lst in by.items():
        rec = {"訊號": name, "筆數": len(lst)}
        for h in horizons:
            v = np.array([fwd(i, j, h) for i, j, _, _ in lst], dtype=float)
            v = v[~np.isnan(v)]
            rec[f"{h}日平均%"] = round(float(v.mean()) * 100, 2) if len(v) else None
        v5 = np.array([fwd(i, j, 5) for i, j, _, _ in lst], dtype=float)
        v5 = v5[~np.isnan(v5)]
        rec["5日勝率%"] = round(float((v5 > 0).mean()) * 100, 1) if len(v5) else None
        rec["5日中位%"] = round(float(np.median(v5)) * 100, 2) if len(v5) else None
        r20 = np.array([fr["ret20"].at[ts, c] for _, _, c, ts in lst], dtype=float)
        rec["訊號時近20日已漲%"] = round(float(np.nanmean(r20)) * 100, 1) if np.isfinite(r20).any() else None
        rows.append(rec)
    # 基準：大盤多頭日、全部股票
    base = {"訊號": "全部股票（基準）", "筆數": 0}
    days = [i for i, ts in enumerate(idx) if bool(mkt.get(ts, False)) and i + 1 < len(idx)]
    for h in horizons:
        vals = []
        for i in days:
            k = i + 1 + h - 1
            if k >= len(idx):
                continue
            o, c_ = op[i + 1], cl[k]
            r = np.where(o > 0, c_ / o - 1, np.nan)
            vals.append(r[~np.isnan(r)])
        v = np.concatenate(vals) if vals else np.array([])
        base[f"{h}日平均%"] = round(float(v.mean()) * 100, 2) if len(v) else None
        if h == 5 and len(v):
            base["5日勝率%"] = round(float((v > 0).mean()) * 100, 1)
            base["5日中位%"] = round(float(np.median(v)) * 100, 2)
            base["筆數"] = int(len(v))
    r20 = fr["ret20"].to_numpy()[days]
    base["訊號時近20日已漲%"] = round(float(np.nanmean(r20)) * 100, 1)
    rows.append(base)
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- 實際推播用（單日、單一時點）

def last_state(d: pd.DataFrame) -> dict | None:
    """prepare() 之後的日 K 最後一天：近 5／20 日漲幅、離 20 日高、量比、有沒有站上 60 日線、龍頭條件。"""
    if d is None or len(d) < 80:
        return None
    r, c = d.iloc[-1], float(d["close"].iloc[-1])
    if not c or pd.isna(r["ma60"]) or pd.isna(r["hh"]):
        return None
    return {"close": c, "ret5": c / float(d["close"].iloc[-6]) - 1, "ret20": c / float(d["close"].iloc[-21]) - 1,
            "ret120": c / float(d["close"].iloc[-121]) - 1 if len(d) > 121 else None,
            "gap": float(r["hh"]) / c - 1, "vr": float(r["vr"]) if not pd.isna(r["vr"]) else 0.0,
            "above60": c > float(r["ma60"]), "breakout": bool(d["sig_a"].tail(LAG_DAYS).any())}


def us_moves(us_hist: dict[str, pd.DataFrame], lead_map: dict, floor_ratio: float = 1.0) -> list[dict]:
    """最新一個美股交易日漲幅達門檻的領先指標（由強到弱，以「漲幅 ÷ 門檻」排序）。
    floor_ratio：漲幅達門檻的幾倍才算（1.0＝達門檻；設很小的負數可以列出全部，用來看「最接近門檻的是誰」）。"""
    out = []
    for sym, spec in lead_map.items():
        df = us_hist.get(sym)
        if df is None or len(df) < 22:
            continue
        c = df["close"]
        pct = float(c.iloc[-1] / c.iloc[-2] - 1) * 100
        thr = float(spec.get("pct", 3.0))
        if pct >= thr * floor_ratio:
            vol = df["volume"]
            vr = float(vol.iloc[-1] / vol.iloc[-21:-1].mean()) if vol.iloc[-21:-1].mean() else None
            out.append({"sym": sym, "name": spec.get("name", sym), "pct": pct, "thr": thr, "vr": vr,
                        "date": str(df.index[-1].date()), "themes": list(spec.get("themes", []))})
    return sorted(out, key=lambda m: -m["pct"] / m["thr"])


def lead_picks(moves: list[dict], themes_tw: dict, states: dict[str, dict], futures: dict,
               max5: float = LEAD_MAX_RET5, max20: float = LEAD_MAX_RET20) -> list[dict]:
    """領先指標對應族群裡、有股票期貨、而且還沒跟上的股票。多個領先指標指到同一檔時合併理由。"""
    found: dict[str, dict] = {}
    for m in moves:
        for th in m["themes"]:
            for c in map(str, themes_tw.get(th, [])):
                st = states.get(c)
                info = futures.get(c) or {}
                if not st or not (info.get("std") or info.get("mini")):
                    continue
                if not (st["above60"] and st["ret5"] <= max5 and st["ret20"] <= max20):
                    continue
                e = found.setdefault(c, {"code": c, "why": [], "themes": [], "score": 0.0, **st})
                tag = f"{m['name']} {m['pct']:+.1f}%"
                if tag not in e["why"]:
                    e["why"].append(tag)
                if th not in e["themes"]:
                    e["themes"].append(th)
                e["score"] = max(e["score"], m["pct"] / m["thr"])
    return sorted(found.values(), key=lambda e: -e["score"])


def _r120(x: dict) -> str:
    return f"120日 {x['ret120'] * 100:+.0f}%｜" if x.get("ret120") is not None else ""


def lead_message(day: str, moves: list[dict], picks: list[dict], rows: dict[str, dict], names: dict, market_ok: bool,
                 limit: int = 10, note: str = "") -> str:
    """盤前領先名單。rows：candidates.rows() 的結果（口數、停損），用代號查。"""
    lines = [f"🌎 盤前領先名單 {day}｜美股昨夜領先指標大漲，台股對應族群裡還沒跟上的",
             "大盤：" + ("🟢 多頭" if market_ok else "🔴 空頭／盤整，依規則先不要下新單"),
             "昨夜美股：" + "、".join(f"{m['name']} {m['sym']} {m['pct']:+.1f}%" for m in moves[:8])]
    if not picks:
        return "\n".join(lines + ["對應的台股都已經漲過頭、跌破 60 日線，或沒有股票期貨 → 今天沒有領先名單"])
    for x in picks[:limit]:
        r = rows.get(x["code"]) or {}
        info = r.get("info") or {}
        fut = "／".join(filter(None, [f"一般 {info['std']}" if info.get("std") else "",
                                       f"小型 {info['mini']}" if info.get("mini") else ""]))
        lines.append(f"\n{x['code']} {names.get(x['code'], x['code'])} 昨收 {x['close']:,.2f}"
                     f"（5日 {x['ret5'] * 100:+.1f}%｜20日 {x['ret20'] * 100:+.1f}%｜{_r120(x)}離20日高 {x['gap'] * 100:.1f}%）"
                     + (f"｜{fut}" if fut else ""))
        lines.append(f"  因：{'、'.join(x['why'][:3])}｜族群：{'、'.join(x['themes'][:2])}")
        if r.get("qty"):
            lines.append(f"  🟢 可做 {r['contract']} {r['qty']} 口｜停損參考 {r['stop']:,.2f}｜最大虧損 {r['risk']:,.0f}")
        elif r:
            lines.append(f"  🟡 一口虧 {r['risk']:,.0f} 超過 2% 上限，需本金約 {r['need']:,.0f}")
    if note:
        lines.append("\n" + note)
    return "\n".join(lines)


def laggard_picks(codes: list[str], states: dict[str, dict], days_ok: bool = True) -> tuple[list[str], list[dict]]:
    """題材補漲：回傳（剛放量突破的龍頭代號, 還沒漲的補漲候選）。候選＝站上 60 日線、近 20 日漲幅小、離 20 日高不遠、量沒縮。"""
    lead = [c for c in codes if (states.get(c) or {}).get("breakout")]
    if not lead:
        return [], []
    out = []
    for c in codes:
        st = states.get(c)
        if not st or c in lead:
            continue
        if st["above60"] and st["ret20"] <= LAG_MAX_RET20 and st["gap"] <= LAG_MAX_GAP and st["vr"] >= LAG_MIN_VR:
            out.append({"code": c, **st})
    return lead, sorted(out, key=lambda x: x["ret20"])


def laggard_section(lead: list[str], picks: list[dict], rows: dict[str, dict], names: dict, limit: int = 5) -> str:
    if not picks:
        return ""
    lines = [f"\n📈 補漲候選｜龍頭剛放量突破：{'、'.join(f'{c} {names.get(c, c)}' for c in lead[:4])}；同題材這幾檔還沒跑",
             LAG_NOTE]
    for x in picks[:limit]:
        r = rows.get(x["code"]) or {}
        info = r.get("info") or {}
        fut = "／".join(filter(None, [f"一般 {info['std']}" if info.get("std") else "",
                                       f"小型 {info['mini']}" if info.get("mini") else ""]))
        lines.append(f"・{x['code']} {names.get(x['code'], x['code'])} {x['close']:,.2f}"
                     f"（20日 {x['ret20'] * 100:+.1f}%｜{_r120(x)}離20日高 {x['gap'] * 100:.1f}%｜量比 {x['vr']:.1f}）"
                     + (f"｜{fut}" if fut else ""))
        if r.get("qty"):
            lines.append(f"  🟢 可做 {r['contract']} {r['qty']} 口｜停損參考 {r['stop']:,.2f}｜最大虧損 {r['risk']:,.0f}")
        elif r:
            lines.append(f"  🟡 一口虧 {r['risk']:,.0f} 超過 2% 上限，需本金約 {r['need']:,.0f}")
    return "\n".join(lines)
