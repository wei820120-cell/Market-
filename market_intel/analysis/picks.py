"""強勢標的：同時看「資金流入」、「題材 / 新聞」、「漲價」、「法人買超」，並標示有沒有股票期貨。

分數（越高越強，每一項都列在表格裡，方便自己判斷）：
  量能   min(量比, 5) × 2          量比＝今日成交金額 ÷ 前 5 日均額（盤中用同時段步調）
  法人   外資＋投信買超金額每 5 億 1 分（-3 ~ +6）
  新聞   該股正面新聞分數加總（最多 8）
  漲價   有漲價相關新聞 +4
  族群   所屬族群今天判讀為資金流入 +2
  當天下跌時總分打五折。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from ..fetchers.stock_futures import label as futures_label


@dataclass
class Candidate:
    code: str
    name: str
    pct: float | None = None
    ratio: float | None = None        # 量比（盤後）或量能步調（盤中）
    inst: float | None = None         # 外資＋投信買超（億）
    news_score: float = 0.0
    price_hike: bool = False
    themes: list[str] = field(default_factory=list)
    theme_inflow: bool = False
    headline: str = ""


def news_by_code(ranked: list) -> dict[str, dict]:
    """把新聞依個股彙總：正面分數加總、是否有漲價、代表標題。"""
    out: dict[str, dict] = {}
    for it in ranked:
        if it.score <= 0:
            continue
        codes = [c for c in it.codes if c[:1].isdigit()]  # 只彙總台股
        # 一篇文章列了很多檔（盤勢整理、族群懶人包）時分數平均分攤，也不算該股本身的漲價消息
        broad = len(codes) > 3
        weight = 3 / len(codes) if broad else 1.0
        for c in codes:
            d = out.setdefault(c, {"score": 0.0, "hike": False, "headline": ""})
            d["score"] += it.score * weight
            if not broad and any(t.startswith("漲價") for t in it.tags):
                d["hike"] = True
            if not d["headline"] or (not broad and d.get("broad")):
                d["headline"] = it.title[:40]
                d["broad"] = broad
    return out


def score(c: Candidate) -> tuple[float, list[str]]:
    s, why = 0.0, []
    if c.ratio is not None:
        s += min(max(c.ratio, 0), 5) * 2
        if c.ratio >= 1.5:
            why.append(f"量{c.ratio:.1f}倍")
    if c.inst is not None:
        s += min(max(c.inst / 5, -3), 6)
        if c.inst >= 1:
            why.append(f"法人買{c.inst:.0f}億")
    if c.news_score > 0:
        s += min(c.news_score, 8)
        why.append("題材")
    if c.price_hike:
        s += 4
        why.append("漲價")
    if c.theme_inflow:
        s += 2
        why.append("族群資金流入")
    if c.pct is not None and c.pct < 0:
        s *= 0.5
    return round(s, 1), why


def rank_picks(cands: list[Candidate], futures: dict[str, dict], top: int = 20,
               min_ratio: float = 1.2) -> pd.DataFrame:
    rows = []
    for c in cands:
        # 至少要有資金（量增或法人買）或題材，且當天沒有明顯下跌
        has_money = (c.ratio or 0) >= min_ratio or (c.inst or 0) >= 1
        if not (has_money or c.news_score > 0) or (c.pct is not None and c.pct < -1):
            continue
        sc, why = score(c)
        rows.append({
            "代號": c.code,
            "名稱": c.name,
            "漲跌%": None if c.pct is None else round(c.pct, 2),
            "量比": None if c.ratio is None else round(c.ratio, 2),
            "法人(億)": None if c.inst is None else round(c.inst, 1),
            "族群": "、".join(c.themes),
            "漲價": "✅" if c.price_hike else "",
            # 清單抓不到時標「未知」，不要誤標成「無」而錯過機會
            "股票期貨": futures_label(futures.get(c.code)) if futures else "未知",
            "分數": sc,
            "理由": "、".join(why),
            "新聞": c.headline,
        })
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    return df.sort_values("分數", ascending=False).head(top).reset_index(drop=True)


def themes_of(themes: dict[str, list]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for theme, codes in themes.items():
        for c in map(str, codes):
            out.setdefault(c, []).append(theme)
    return out


def picks_message(df: pd.DataFrame, title: str, n: int = 5) -> str:
    lines = [title]
    for r in df.head(n).to_dict("records"):
        fut = {"無": "無股期", "未知": "股期未知"}.get(r["股票期貨"], f"股期{r['股票期貨']}")
        pct = "" if r["漲跌%"] is None else f"{r['漲跌%']:+.1f}% "
        lines.append(f"・{r['代號']} {r['名稱']} {pct}{fut}｜{r['理由']}")
    return "\n".join(lines)
