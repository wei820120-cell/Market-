"""事件族群進場規則（試行）：族群同日多檔拉升＋有事件或研究 → 列出有股期、沒鎖死漲停的補漲／可追標的，口數照 2% 規則。

背景（2026-11-26 低軌衛星族群集體漲停）：現行進場（突破 20 日高＋站上 60 日線＋量能步調 ≥ 1.5）抓不到「大跌後反彈、事件帶動整組同日拉開」的行情；
族群 8 檔裡只有 3 檔有股期，那 3 檔漲停價還低於前高，於是整天沒有任何進場候選。這支把「整組在動」當成訊號：

  1. 族群 ≥3 檔（大族群 ≥25%）同日漲 ≥5%（analysis/group_surge.py 偵測）
  2. 有事件或研究撐腰：族群有對應研究，或今天有命中的重大事件（config/major_events.yaml）
  3. 候選＝族群成分股裡有股票期貨、沒有鎖死漲停（買得到）的：
       補漲＝還沒漲到 5%（−1% ～ +5%）；追價＝已漲 ≥5% 但沒鎖死（追高風險大）
  4. 停損＝現價 − 1.5ATR，口數照 2% 規則（trade/backtest.size）；大盤空頭／盤整時只標示風險，名單照列（不擋）

回測：python -m market_intel backtest --eventgroup（GitHub 上跑，結果在 research/event_group_backtest.md）。
先前的回測已經發現「同題材補漲單獨做不賺錢」，所以這支是『列名單＋口數』，不自動進場，要不要進場自己決定。
"""
from __future__ import annotations

import math
from collections import defaultdict

import pandas as pd

from . import backtest as bt
from .paper import PARAMS

SURGE_PCT = 5.0        # 與 analysis/group_surge.py 一致
LAG_FLOOR = -1.0       # 補漲候選的跌幅下限（%）：不跌的才算「還沒跟上」，跌的多半是別的原因
TIER_LAG, TIER_CHASE = "補漲", "追價"
NOTE = ("試行規則：回測結果見 research/event_group_backtest.md（先前回測同題材補漲單獨做不賺錢，要看相對強度）；"
        "只列名單與 2% 口數，不自動進場，鎖死漲停的買不到所以不列。")


# ---------------------------------------------------------------- 事件或研究撐腰

def events_for_theme(theme: str, major_state: dict, events_cfg: dict, today: str) -> list[str]:
    """今天已推過的重大事件裡，哪些把這個族群列為相關（major_state 的鍵是『事件名|日期』）。"""
    out = []
    for name, spec in (events_cfg or {}).items():
        if int(major_state.get(f"{name}|{today}") or 0) > 0 and theme in (spec.get("themes") or {}):
            out.append(name)
    return out


def backing(topic: str | None, topic_date: str, events: list[str]) -> list[str]:
    """撐腰說明：有研究（題材名稱、日期）、今天有重大事件；兩者都沒有就回空清單（規則不成立）。"""
    out = []
    if topic:
        out.append(f"研究《{topic}》{topic_date}".strip())
    out += [f"重大事件｜{e}" for e in events]
    return out


# ---------------------------------------------------------------- 候選與口數

def tier_of(pct: float, surge_pct: float = SURGE_PCT, lag_floor: float = LAG_FLOOR) -> str:
    if pct >= surge_pct:
        return TIER_CHASE
    return TIER_LAG if pct >= lag_floor else ""


def candidates(group: dict, futures: dict, levels: dict, equity: float, rates: dict, p=PARAMS) -> list[dict]:
    """族群裡有股期、沒鎖死漲停的成分股，附停損與 2% 規則口數。group＝analysis/group_surge.detect 的一筆。
    levels＝{代號: {"atr": …}}（trade/intraday.levels）；沒有 ATR 的列出但不算口數。"""
    out = []
    for m in group["members"]:
        info = futures.get(m["code"]) or {}
        if not (info.get("std") or info.get("mini")) or m["locked"]:
            continue
        tier = tier_of(m["pct"])
        if not tier:
            continue
        row = {"code": m["code"], "name": m["name"], "pct": m["pct"], "price": m["price"], "tier": tier,
               "limit_up": m["limit_up"], "info": info, "qty": 0, "contract": "", "stop": None, "dist": None,
               "risk": None, "need": None}
        lv = levels.get(m["code"]) or {}
        if lv.get("atr"):
            stop = m["price"] - p.atr_mult * lv["atr"]
            dist = m["price"] - stop
            contract, mult, qty = bt.size(equity, m["price"], stop, info, p, rates.get(m["code"], p.margin_rate))
            unit = bt.MINI if info.get("mini") else bt.STD
            row.update(stop=stop, dist=dist, contract=contract, qty=qty,
                       risk=dist * mult * qty if qty else dist * unit, need=dist * unit / p.risk_pct)
        out.append(row)
    rank = {TIER_LAG: 0, TIER_CHASE: 1}
    return sorted(out, key=lambda r: (rank[r["tier"]], 0 if r["qty"] else 1, -r["pct"]))


def size_text(r: dict, p=PARAMS) -> str:
    """一檔的 2% 規則說明（給族群拉升快訊與進場候選共用）。"""
    if r.get("stop") is None:
        return "2% 規則：沒有 ATR 資料，無法算口數"
    if r["qty"]:
        return f"🟢 可做 {r['contract']} {r['qty']} 口｜停損 {r['stop']:,.2f}｜最大虧損 {r['risk']:,.0f}"
    return f"🟡 一口虧約 {r['risk']:,.0f} 超過 2% 上限，需本金約 {r['need']:,.0f}"


def message(group: dict, cands: list[dict], proof: list[str], market_ok: bool, now, limit: int = 8) -> str:
    """進出場機器人的『事件族群』訊息。proof＝backing() 的結果。"""
    lines = [f"🧩 {now:%H:%M} 事件族群進場候選｜{group['theme']}",
             f"族群 {group['n_up']}/{group['n']} 檔同日漲 ≥{SURGE_PCT:g}%"
             + (f"（鎖死漲停 {group['n_locked']} 檔，買不到、不列）" if group["n_locked"] else ""),
             "撐腰：" + "；".join(proof),
             "大盤：" + ("🟢 多頭" if market_ok else "🔴 空頭／盤整——依規則不開新倉，名單只供觀察，風險自負")]
    if not cands:
        return "\n".join(lines + ["族群裡沒有『有股期、沒鎖死漲停』的標的 → 今天沒有進場候選"])
    shown = 0
    for tier, title in ((TIER_LAG, "補漲（還沒漲到 5%，族群在動它還沒動）"),
                        (TIER_CHASE, "追價（已漲 ≥5% 但沒鎖死；追高風險大，停損要守）")):
        rows = [r for r in cands if r["tier"] == tier][: max(limit - shown, 0)]
        if not rows:
            continue
        lines.append(f"\n{title}：")
        for r in rows:
            shown += 1
            fut = "／".join(filter(None, [f"一般 {r['info']['std']}" if r["info"].get("std") else "",
                                           f"小型 {r['info']['mini']}" if r["info"].get("mini") else ""]))
            lines.append(f"・{r['code']} {r['name']} {r['price']:,.2f}（{r['pct']:+.1f}%）{fut}"
                         + ("｜漲停有賣盤" if r["limit_up"] else ""))
            lines.append(f"  {size_text(r)}")
    lines.append("\n" + NOTE)
    return "\n".join(lines)


# ---------------------------------------------------------------- 回測用訊號（日 K 近似）

def signals(themes_tw: dict, fr: dict[str, pd.DataFrame], futures: dict, pct: float = SURGE_PCT, min_n: int = 3,
            min_share: float = 0.25, lag_floor: float = LAG_FLOOR, lock_pct: float = 9.4, trend: bool = False,
            tiers: tuple = (TIER_LAG, TIER_CHASE)) -> dict:
    """事件日（族群同日 ≥min_n 檔漲 ≥pct）收盤產生、隔天開盤進場的訊號：{日期: [(分數, 代號, 型態)]}。

    fr＝trade/early.frames 的寬表。日 K 近似：漲幅 ≥9.4% 且收在最高價視為鎖死漲停（買不到）。
    只對有股票期貨的股票出訊號（和實際進場一致）。分數：補漲＝相對強度（0～1）、追價＝當天漲幅。
    """
    close, high = fr["close"], fr["high"]
    day = close / close.shift(1) - 1
    locked = (day >= lock_pct / 100) & (close >= high - 1e-9)
    cols = set(close.columns)
    fut_ok = {c for c in cols if (futures.get(c) or {}).get("std") or (futures.get(c) or {}).get("mini")}
    out: dict = defaultdict(list)
    best: dict = {}
    for theme, members in themes_tw.items():
        ms = [c for c in dict.fromkeys(str(c) for c in members) if c in cols]
        if len(ms) < min_n:
            continue
        need = max(min_n, math.ceil(min_share * len(ms)))
        n_up = (day[ms] >= pct / 100).sum(axis=1)
        for ts in n_up[n_up >= need].index:
            for c in ms:
                if c not in fut_ok:
                    continue
                v = day.at[ts, c]
                if pd.isna(v) or locked.at[ts, c]:
                    continue
                if trend and not close.at[ts, c] > fr["ma60"].at[ts, c]:
                    continue
                if v >= pct / 100:
                    tier, sc = TIER_CHASE, float(v)
                elif v >= lag_floor / 100:
                    rs = fr["rs"].at[ts, c]
                    tier, sc = TIER_LAG, 0.0 if pd.isna(rs) else float(rs)
                else:
                    continue
                if tier in tiers:
                    best[(ts, c, tier)] = max(best.get((ts, c, tier), -1.0), sc)
    for (ts, c, tier), sc in best.items():
        out[ts].append((sc, c, tier))
    return out


def combine(*sigs: dict) -> dict:
    """把幾種訊號直接疊在一起（不重新排名；forward_stats 只看型態名稱，不看分數）。"""
    out: dict = defaultdict(list)
    for s in sigs:
        for ts, items in s.items():
            out[ts] += list(items)
    return out


def single_signals(themes_tw: dict, fr: dict[str, pd.DataFrame], futures: dict, pct: float = SURGE_PCT,
                   min_n: int = 3, min_share: float = 0.25, lock_pct: float = 9.4, trend: bool = False,
                   name: str = "單檔追價") -> dict:
    """對照組：一樣是『有股期、當天漲 ≥pct、沒鎖死漲停』，但不是事件族群的追價（當天它所屬的族群沒有達到事件條件，單獨一檔在漲）。
    用來分辨『族群一起動』有沒有額外價值，還是只是『強勢股追價』本身的動能效果。"""
    ev = signals(themes_tw, fr, futures, pct, min_n, min_share, lock_pct=lock_pct, trend=trend, tiers=(TIER_CHASE,))
    in_event = {(ts, c) for ts, items in ev.items() for _, c, _ in items}
    close, high = fr["close"], fr["high"]
    day = close / close.shift(1) - 1
    locked = (day >= lock_pct / 100) & (close >= high - 1e-9)
    fut_ok = [c for c in close.columns if (futures.get(c) or {}).get("std") or (futures.get(c) or {}).get("mini")]
    hit = (day[fut_ok] >= pct / 100) & ~locked[fut_ok]
    if trend:
        hit &= close[fut_ok] > fr["ma60"][fut_ok]
    out: dict = defaultdict(list)
    for ts in hit.index[hit.any(axis=1)]:
        for c in hit.columns[hit.loc[ts].to_numpy()]:
            if (ts, c) not in in_event:
                out[ts].append((float(day.at[ts, c]), c, name))
    return out


def summarize_events(evs: list[dict], horizons: tuple = (1, 3, 5), gap_days: int = 7) -> list[dict]:
    """事件日明細的統計：全部事件日、以及『每一波只算第一天』（與前一個事件日相隔超過 gap_days 個日曆天才算新的一波，
    連續幾天的事件日高度重疊，全部平均會把同一波算很多次）。報酬＝有股期成分股隔天開盤買進（沒扣成本、沒扣鎖死買不到）。"""
    import statistics
    from datetime import date as _date
    firsts, prev = [], None
    for e in evs:
        d = _date.fromisoformat(e["日期"])
        if prev is None or (d - prev).days > gap_days:
            firsts.append(e)
        prev = d
    out = []
    for label, rows in (("全部事件日", evs), ("每一波第一天", firsts)):
        rec: dict = {"範圍": label, "事件數": len(rows)}
        for h in horizons:
            v = [r[f"有股期成分股隔天開盤買進{h}日%"] for r in rows if r.get(f"有股期成分股隔天開盤買進{h}日%") is not None]
            rec[f"{h}日平均%"] = round(sum(v) / len(v), 2) if v else None
            rec[f"{h}日中位%"] = round(statistics.median(v), 2) if v else None
            rec[f"{h}日勝率%"] = round(sum(x > 0 for x in v) / len(v) * 100, 1) if v else None
        out.append(rec)
    return out


def theme_events(theme: str, members: list, fr: dict[str, pd.DataFrame], futures: dict, pct: float = SURGE_PCT,
                 min_n: int = 3, horizons: tuple = (1, 3, 5)) -> list[dict]:
    """單一族群的事件日明細（給『低軌衛星』這類個案看）：哪天幾檔漲 ≥pct、有股期的候選隔天開盤買進後 1／3／5 日報酬。"""
    close, openp = fr["close"], fr["open"]
    cols = [c for c in dict.fromkeys(str(c) for c in members) if c in close.columns]
    if not cols:
        return []
    day = close[cols] / close[cols].shift(1) - 1
    idx = list(close.index)
    rows = []
    for ts in day.index[(day >= pct / 100).sum(axis=1) >= min_n]:
        i = idx.index(ts)
        up = [c for c in cols if day.at[ts, c] >= pct / 100]
        rec = {"日期": str(ts.date()), "拉升檔數": len(up), "成分股": len(cols),
               "拉升": "、".join(f"{c}{day.at[ts, c] * 100:+.1f}%" for c in up)}
        for h in horizons:
            vals = []
            for c in cols:
                info = futures.get(c) or {}
                if not (info.get("std") or info.get("mini")) or i + 1 >= len(idx) or i + h >= len(idx):
                    continue
                o, k = openp.at[idx[i + 1], c], close.at[idx[i + h], c]
                if o and o > 0 and not pd.isna(k):
                    vals.append(k / o - 1)
            rec[f"有股期成分股隔天開盤買進{h}日%"] = round(sum(vals) / len(vals) * 100, 2) if vals else None
        rows.append(rec)
    return rows


def window_table(members: list, fr: dict[str, pd.DataFrame], start: str, end: str, pct: float = SURGE_PCT,
                 min_n: int = 3) -> list[dict]:
    """某段日期內，族群成分股逐日漲幅（%）與規則是否觸發（≥min_n 檔漲 ≥pct）。給個案檢查用。"""
    close = fr["close"]
    cols = [c for c in dict.fromkeys(str(c) for c in members) if c in close.columns]
    if not cols:
        return []
    day = (close[cols] / close[cols].shift(1) - 1) * 100
    rows = []
    for ts in day.loc[start:end].index:
        up = int((day.loc[ts] >= pct).sum())
        rows.append({"日期": str(ts.date()), "拉升檔數": up, "規則觸發": "是" if up >= min_n else "否",
                     **{c: (None if pd.isna(day.at[ts, c]) else round(float(day.at[ts, c]), 1)) for c in cols}})
    return rows
