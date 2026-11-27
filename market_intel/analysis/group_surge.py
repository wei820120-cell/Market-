"""族群拉升快訊：同一族群同時有 ≥3 檔漲 ≥5%，就整組推一則（漲幅、是否鎖死漲停、有沒有股期、2% 規則能不能做）。

背景（2026-11-26 低軌衛星族群集體漲停）：選股組分數只看個股、每次只列前 5 名，研究卡又是一檔一張，
「整組一起動」在手機上看不出來；這支只做偵測與文字，不下單。進場規則在 trade/event_group.py（要先回測）。

偵測規則：
  - 族群成分股（至少 3 檔有報價）中，漲幅 ≥ pct（預設 5%）的檔數 ≥ max(3, 成分股數 × min_share)；
    min_share 是給大族群用的（24 檔的族群要 6 檔，不然天天觸發）。
  - 漲停鎖死：現價到漲停價且沒有賣單（買不到）；漲停價依台股升降單位從昨收算。
  - 研究自動產生的『研究:…』族群常和策展族群高度重疊，同一批股票只推一則（策展族群優先）。
"""
from __future__ import annotations

import math
from datetime import datetime

SURGE_PCT = 5.0        # 單檔漲幅達這個值算「拉升」
MIN_STOCKS = 3         # 同族群至少幾檔同時拉升
MIN_SHARE = 0.25       # 大族群還要占成分股的這個比例
LOCK_PCT = 9.4         # 沒有昨收可算漲停價時，用漲幅近似
OVERLAP = 0.6          # 兩個族群拉升的股票重疊達這個比例就只推一個
BIG_LEVEL = 6          # 拉升檔數達這個值算第二級（再推一次）


def _tick(price: float) -> float:
    """台股升降單位（元）。"""
    if price < 10:
        return 0.01
    if price < 50:
        return 0.05
    if price < 100:
        return 0.1
    if price < 500:
        return 0.5
    if price < 1000:
        return 1.0
    return 5.0


def limit_up_price(prev_close: float | None) -> float | None:
    """漲停價：昨收 ×1.1，無條件捨去到升降單位。"""
    if not prev_close or prev_close <= 0:
        return None
    raw = prev_close * 1.1
    t = _tick(raw)
    return round(math.floor(raw / t + 1e-9) * t, 2)


def at_limit_up(q) -> bool:
    """現價已到漲停價（有昨收就用漲停價，沒有就用漲幅近似）。"""
    if q.price is None:
        return False
    lu = limit_up_price(q.prev_close)
    if lu is not None:
        return q.price >= lu - 1e-6
    pct = q.change_pct
    return pct is not None and pct >= LOCK_PCT


def is_locked(q) -> bool:
    """漲停鎖死＝在漲停價而且沒有賣單（最佳一檔賣價是空的）；漲停但還有賣單的買得到。"""
    return at_limit_up(q) and getattr(q, "ask", None) is None


def detect(quotes: dict, themes: dict, pct: float = SURGE_PCT, min_n: int = MIN_STOCKS,
           min_share: float = MIN_SHARE) -> list[dict]:
    """目前達標的族群（已去重）。quotes＝{代號: Quote}，themes＝{族群: [代號]}。"""
    found = []
    for theme, codes in themes.items():
        members = []
        for c in dict.fromkeys(str(c) for c in codes):
            q = quotes.get(c)
            if q is None or q.price is None or q.change_pct is None:
                continue
            members.append({"code": c, "name": q.name or c, "pct": float(q.change_pct), "price": float(q.price),
                            "locked": is_locked(q), "limit_up": at_limit_up(q)})
        if len(members) < min_n:
            continue
        need = max(min_n, math.ceil(min_share * len(members)))
        for m in members:
            m["up"] = m["pct"] >= pct
        up = [m for m in members if m["up"]]
        if len(up) < need:
            continue
        members.sort(key=lambda m: m["pct"], reverse=True)
        found.append({"theme": theme, "n": len(members), "n_up": len(up), "need": need,
                      "n_locked": sum(m["locked"] for m in up),
                      "avg_up": sum(m["pct"] for m in up) / len(up), "members": members,
                      "up_codes": [m["code"] for m in up],
                      "level": 2 if len(up) >= BIG_LEVEL else 1})
    return dedupe(found)


def dedupe(groups: list[dict], overlap: float = OVERLAP) -> list[dict]:
    """拉升的股票重疊過半的族群只留一個：檔數多的在前，同檔數時策展族群（不是『研究:』開頭）優先。"""
    ordered = sorted(groups, key=lambda g: (-g["n_up"], g["theme"].startswith("研究:"), -g["avg_up"], g["theme"]))
    kept: list[dict] = []
    for g in ordered:
        a = set(g["up_codes"])
        if any(len(a & set(k["up_codes"])) >= overlap * min(len(a), len(k["up_codes"])) for k in kept):
            continue
        kept.append(g)
    return kept


def covered_codes(groups: list[dict]) -> set[str]:
    """已由族群拉升快訊涵蓋的股票（這些股票所屬的其他小族群不必再各推一張題材卡）。"""
    return {c for g in groups for c in g["up_codes"]}


def is_covered(codes: list, covered: set[str], share: float = 0.5) -> bool:
    """這個族群的成分股有一半以上已被快訊涵蓋 → 不必另外推卡。"""
    cs = [str(c) for c in codes]
    return bool(cs) and sum(c in covered for c in cs) >= share * len(cs)


def _fut_text(label: str | None) -> str:
    return {"無": "無股期", "未知": "股期未知", None: "股期未知", "": "股期未知"}.get(label, f"股期{label}")


def message(g: dict, now: datetime, fut_labels: dict | None = None, sizing: dict | None = None,
            research_line: str = "", market_line: str = "") -> str:
    """推播文字。fut_labels＝{代號: 股期標籤（無／未知／代號）}；sizing＝{代號: 2% 規則說明文字}（沒有就不列）。"""
    fut_labels, sizing = fut_labels or {}, sizing or {}
    head = f"🚀 {now:%H:%M} 族群拉升｜{g['theme']}：{g['n']} 檔中 {g['n_up']} 檔漲 ≥{SURGE_PCT:g}%"
    if g["n_locked"]:
        head += f"（漲停鎖死 {g['n_locked']} 檔，買不到）"
    lines = [head]
    for m in g["members"]:
        if not m["up"]:
            continue
        flag = "🔒鎖死漲停" if m["locked"] else ("漲停有賣盤" if m["limit_up"] else "")
        line = f"・{m['code']} {m['name']} {m['pct']:+.1f}%" + (f" {flag}" if flag else "")
        line += f"｜{_fut_text(fut_labels.get(m['code']))}"
        if sizing.get(m["code"]):
            line += f"｜{sizing[m['code']]}"
        lines.append(line)
    rest = [m for m in g["members"] if not m["up"]]
    if rest:
        lines.append("還沒跟上：" + "、".join(f"{m['name']}{m['pct']:+.1f}%" for m in rest[:6]))
    for extra in (research_line, market_line):
        if extra:
            lines.append(extra)
    lines.append("這是偵測不是進場訊號；進場候選（補漲／可追、2% 口數）看進出場機器人的『事件族群』訊息。")
    return "\n".join(lines)
