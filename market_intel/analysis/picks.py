"""強勢標的：同時看「資金流入」、「題材 / 新聞」、「漲價」、「法人買超」，並標示有沒有股票期貨。

分數（越高越強，每一項都列在表格裡，方便自己判斷）：
  量能   min(量比, 5) × 2          量比＝今日成交金額 ÷ 前 5 日均額（盤中用同時段步調）
  法人   外資＋投信買超金額每 5 億 1 分（-3 ~ +6）
  新聞   該股正面新聞分數加總（最多 8）
  漲價   有漲價相關新聞 +4；公司漲價信／正式公告再 +2
  族群漲價 上游漲價、所屬族群受惠 +2
  族群   所屬族群今天判讀為資金流入 +2
  當天下跌時總分打五折。

價量確認（2026-11-27）：新聞、漲價、漲價信、族群漲價這四項「故事分」要有價格（漲幅 ≥ 2%）或量能（量比 ≥ 1.5）支持才給足；
價量都有 ×1.0、只有一項 ×0.6、都沒有 ×0.3。原因：11/26 低軌衛星族群集體漲停，前 5 名卻被整天不變的題材／漲價標籤占滿，
有漲價信的股票不用漲也贏過漲停股。另外前 5 名保留名額給「漲幅 ≥ 5% 且量能達標」最強的（rank_picks 的 reserve）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from ..fetchers.stock_futures import label as futures_label


CONFIRM_PCT = 2.0       # 漲幅達這個值算價格確認
CONFIRM_RATIO = 1.5     # 量比／量能步調達這個值算量能確認
STORY_FACTOR = {2: 1.0, 1: 0.6, 0: 0.3}   # 價量確認幾項 → 故事分（新聞、漲價、漲價信、族群漲價）的係數
STRONG_PCT = 5.0        # 保留名額的門檻：漲幅 ≥ 5%（量比要達 rank_picks 的 min_ratio）
SLOTS = 5               # 推播列前幾名（picks_message 的預設）
RESERVE = 2             # 前 SLOTS 名裡最多保留幾個給量價最強的


@dataclass
class Candidate:
    code: str
    name: str
    pct: float | None = None
    ratio: float | None = None        # 量比（盤後）或量能步調（盤中）
    inst: float | None = None         # 外資＋投信買超（億）
    news_score: float = 0.0
    price_hike: bool = False
    price_letter: bool = False        # 公司發漲價信／正式公告調價
    theme_hike: bool = False          # 上游漲價，所屬族群受惠
    themes: list[str] = field(default_factory=list)
    theme_inflow: bool = False
    headline: str = ""


def news_by_code(ranked: list, themes: dict[str, list] | None = None) -> dict[str, dict]:
    """把新聞依個股彙總：正面分數加總、是否有漲價／漲價信、族群是否受惠於漲價、代表標題。"""
    out: dict[str, dict] = {}

    def entry(c: str) -> dict:
        return out.setdefault(c, {"score": 0.0, "hike": False, "letter": False, "theme_hike": False, "headline": ""})

    for it in ranked:
        if it.score <= 0:
            continue
        codes = [c for c in it.codes if c[:1].isdigit()]  # 只彙總台股
        # 一篇文章列了很多檔（盤勢整理、族群懶人包）時分數平均分攤，也不算該股本身的漲價消息
        broad = len(codes) > 3
        weight = 3 / len(codes) if broad else 1.0
        letter = any(t.startswith(("漲價信", "公司公告漲價")) for t in it.tags)
        hike = letter or any(t.startswith("漲價") for t in it.tags)
        for c in codes:
            d = entry(c)
            d["score"] += it.score * weight
            if not broad and hike:
                d["hike"] = True
                d["letter"] = d["letter"] or letter
            if not d["headline"] or (not broad and d.get("broad")):
                d["headline"] = it.title[:40]
                d["broad"] = broad
        # 上游漲價：所屬族群的成分股標記「族群漲價」
        if hike and themes:
            for th in getattr(it, "themes", []) or []:
                for c in map(str, themes.get(th, [])):
                    d = entry(c)
                    d["theme_hike"] = True
                    if not d["headline"]:
                        d["headline"] = it.title[:40]
                        d["broad"] = True
    return out


def confirmations(c: Candidate) -> int | None:
    """價量確認幾項（價格、量能各一項）；漲幅和量比都沒有資料時回 None（不打折）。"""
    if c.pct is None and c.ratio is None:
        return None
    return int(c.pct is not None and c.pct >= CONFIRM_PCT) + int(c.ratio is not None and c.ratio >= CONFIRM_RATIO)


def score(c: Candidate) -> tuple[float, list[str]]:
    s, why = 0.0, []
    k = confirmations(c)
    f = 1.0 if k is None else STORY_FACTOR[k]     # 故事分的價量確認係數
    story = 0.0
    if c.ratio is not None:
        s += min(max(c.ratio, 0), 5) * 2
        if c.ratio >= 1.5:
            why.append(f"量{c.ratio:.1f}倍")
    if c.inst is not None:
        s += min(max(c.inst / 5, -3), 6)
        if c.inst >= 1:
            why.append(f"法人買{c.inst:.0f}億")
    if c.news_score > 0:
        story += min(c.news_score, 8)
        why.append("題材")
    if c.price_hike:
        story += 4
        why.append("漲價")
    if c.price_letter:
        story += 2
        why.append("漲價信")
    if c.theme_hike and not c.price_hike:
        story += 2
        why.append("族群漲價")
    s += story * f
    if story and f < 1.0:
        why.append("未價量確認")
    if c.theme_inflow:
        s += 2
        why.append("族群資金流入")
    if c.pct is not None and c.pct < 0:
        s *= 0.5
    return round(s, 1), why


def reserve_strong(df: pd.DataFrame, slots: int = SLOTS, reserve: int = RESERVE, min_ratio: float = 1.2) -> pd.DataFrame:
    """前 slots 名保留名額給量價最強的（漲幅 ≥ STRONG_PCT 且量比 ≥ min_ratio，依漲幅、量比排序取前 reserve 檔）。
    這些股票若沒進前 slots 名，就換掉前 slots 名裡分數最低、又不是量價最強的幾檔；換進來的理由標「量價最強」。"""
    if df.empty or reserve <= 0 or len(df) <= slots:
        return df
    pct = pd.to_numeric(df["漲跌%"], errors="coerce").fillna(-99)
    ratio = pd.to_numeric(df["量比"], errors="coerce").fillna(0)
    strong = df[(pct >= STRONG_PCT) & (ratio >= min_ratio)].assign(_p=pct, _r=ratio)
    keep = list(strong.sort_values(["_p", "_r"], ascending=False).head(reserve).index)
    head = list(df.index[:slots])
    missing = [i for i in keep if i not in head]
    if not missing:
        return df
    moved = []
    for i in reversed(head[:]):
        if not missing:
            break
        if i in keep:
            continue
        head.remove(i)
        take = missing.pop(0)
        head.append(take)
        moved.append(take)
    head.sort(key=lambda i: -float(df.at[i, "分數"]))
    rest = [i for i in df.index if i not in head]
    out = df.loc[head + rest].copy()
    for i in moved:
        why = str(out.at[i, "理由"])
        out.at[i, "理由"] = (why + "、" if why else "") + "量價最強"
    return out.reset_index(drop=True)


def rank_picks(cands: list[Candidate], futures: dict[str, dict], top: int = 20,
               min_ratio: float = 1.2, slots: int = SLOTS, reserve: int = RESERVE) -> pd.DataFrame:
    rows = []
    for c in cands:
        # 至少要有資金（量增或法人買）或題材，且當天沒有明顯下跌
        has_money = (c.ratio or 0) >= min_ratio or (c.inst or 0) >= 1
        if not (has_money or c.news_score > 0 or c.price_letter) or (c.pct is not None and c.pct < -1):
            continue
        sc, why = score(c)
        rows.append({
            "代號": c.code,
            "名稱": c.name,
            "漲跌%": None if c.pct is None else round(c.pct, 2),
            "量比": None if c.ratio is None else round(c.ratio, 2),
            "法人(億)": None if c.inst is None else round(c.inst, 1),
            "族群": "、".join(c.themes),
            "漲價": "漲價信" if c.price_letter else ("✅" if c.price_hike else ("族群" if c.theme_hike else "")),
            # 清單抓不到時標「未知」，不要誤標成「無」而錯過機會
            "股票期貨": futures_label(futures.get(c.code)) if futures else "未知",
            "分數": sc,
            "理由": "、".join(why),
            "新聞": c.headline,
        })
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df = df.sort_values("分數", ascending=False).reset_index(drop=True)
    return reserve_strong(df, slots, reserve, min_ratio).head(top).reset_index(drop=True)


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


# ---------- 大跌日逆勢榜（只列名單供觀察，不影響上面的分數；要不要加分先回測） ----------
# 2026-11-25 加權指數 −4.80%，逆勢收紅的個股至少有三種來源：獲利兌現（金像電、川湖）、事件／內需輪動（資安股漲停）、
# 反彈後沒回吐。三種的持續性不同，所以名單要附「類型」，不要混在一起加分。

WEAK_DAY_PCT = -2.0     # 加權指數跌幅達這個門檻 → 大跌日
LIMIT_UP_PCT = 9.4      # 漲幅達這個值視為漲停（一般股 ±10%）
HELD_PCT = 5.0          # 前一天漲幅達這個值、大跌日還沒回吐 → 守住前日漲幅
MIN_VALUE = 3e8         # 成交值至少 3 億元（太小的沒有參考性，也不好進出）
MIN_YOY = 20.0          # 最新月營收年增達這個值 → 營收有成長（獲利兌現型候選）

TYPE_REV = "營收有成長"
TYPE_LIMIT_REV = "漲停・營收有成長"
TYPE_LIMIT = "漲停・營收沒跟上（輪動？）"
TYPE_OTHER = "營收未達標／無資料"
WEAK_DAY_COLUMNS = ["代號", "名稱", "漲跌%", "相對大盤(pp)", "前日漲跌%", "量比", "成交(億)", "法人(億)",
                    "月營收年增%", "類型", "備註", "族群"]


def _num(v) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(f) else f


def is_weak_day(idx_pct, thr: float = WEAK_DAY_PCT) -> bool:
    """大跌日：加權指數當天跌幅達門檻（抓不到指數就當不是，不要亂列名單）。"""
    v = _num(idx_pct)
    return v is not None and v <= thr


def weak_day_leaders(day: pd.DataFrame, idx_pct: float, prev_pct: dict | None = None, ratio: dict | None = None,
                     inst: dict | None = None, revenue: dict | None = None, alerts: dict | None = None,
                     themes: dict | None = None, min_value: float = MIN_VALUE, min_yoy: float = MIN_YOY,
                     top: int = 30) -> pd.DataFrame:
    """大跌日逆勢榜：收紅，或跌幅不到大盤一半（抗跌）、成交值夠大的一般股（4 碼，不含 ETF、權證、特別股）。

    day     全市場日行情（code、name、pct、value〔元〕）
    prev_pct／ratio／inst  {代號: 前一天漲跌%／量比／外資＋投信買超億元}，沒有的欄位留空
    revenue {代號: {"yoy": 最新月營收年增%}}（tw_daily.load_revenue）　alerts {代號: [處置／注意說明]}
    排序：營收有成長的在前，其次相對大盤強度、成交值。每檔附類型與備註，不評分。"""
    idx = _num(idx_pct)
    if day is None or day.empty or idx is None:
        return pd.DataFrame(columns=WEAK_DAY_COLUMNS)
    prev_pct, ratio, inst = prev_pct or {}, ratio or {}, inst or {}
    revenue, alerts, themes = revenue or {}, alerts or {}, themes or {}
    half = idx / 2
    rows = []
    for r in day.itertuples():
        code = str(r.code)
        if not (code.isdigit() and len(code) == 4) or code.startswith("0"):
            continue
        pct, value = _num(getattr(r, "pct", None)), _num(getattr(r, "value", None))
        if pct is None or value is None or value < min_value:
            continue
        if not (pct > 0 or pct >= half):          # 收紅，或跌幅不到大盤的一半
            continue
        yoy = _num((revenue.get(code) or {}).get("yoy"))
        strong_rev = yoy is not None and yoy >= min_yoy
        limit_up = pct >= LIMIT_UP_PCT
        kind = (TYPE_LIMIT_REV if strong_rev else TYPE_LIMIT) if limit_up else (TYPE_REV if strong_rev else TYPE_OTHER)
        prev = _num(prev_pct.get(code))
        notes = []
        if prev is not None and prev >= HELD_PCT and pct >= 0:
            notes.append("守住前日漲幅")
        ratio_v = _num(ratio.get(code))
        if ratio_v is not None and ratio_v >= 1.5:
            notes.append(f"量{ratio_v:.1f}倍")
        inst_v = _num(inst.get(code))
        if inst_v is not None and inst_v >= 1:
            notes.append(f"法人買{inst_v:.0f}億")
        if alerts.get(code):
            notes.append("⚠" + "、".join(alerts[code]))
        rows.append({
            "代號": code, "名稱": getattr(r, "name", "") or code, "漲跌%": round(pct, 2),
            "相對大盤(pp)": round(pct - idx, 2),
            "前日漲跌%": None if prev is None else round(prev, 2),
            "量比": None if ratio_v is None else round(ratio_v, 2),
            "成交(億)": round(value / 1e8, 1),
            "法人(億)": None if inst_v is None else round(inst_v, 1),
            "月營收年增%": None if yoy is None else round(yoy, 1),
            "類型": kind, "備註": "　".join(notes), "族群": "、".join((themes.get(code) or [])[:3]),
            "_rank": (0 if strong_rev else 1, -(pct - idx), -value),
        })
    if not rows:
        return pd.DataFrame(columns=WEAK_DAY_COLUMNS)
    rows.sort(key=lambda x: x["_rank"])
    return pd.DataFrame(rows).drop(columns="_rank").head(top).reset_index(drop=True)[WEAK_DAY_COLUMNS]
