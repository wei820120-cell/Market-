"""題材卡（研究圖表）：把研究過的題材依「細項產業」整理成圖片＋文字，推到研究機器人。

（進出場訊號：目標價、停損等，之後由另一個「進出場機器人」負責，不放在研究圖表裡。）

每檔股票：
- 所屬細項產業、角色、受惠／受傷
- 價量：收盤、漲跌（1／5／20 日）、量比（今日成交金額 ÷ 前 5 日平均）
- 月營收：最新月份年增、月增
- 有沒有股票期貨、小型股票期貨（契約代碼）
- 處置股、注意股警示；研究後漲跌

圖片：
1. 供應鏈地圖：依細項產業分層
2. 細項產業表現：各細項等權走勢比較＋5 日／20 日漲跌與量比（看資金流向哪個細項）
3. 研究資料表

觸發：題材研究完成、研究過的族群資金流入（盤中湧入、盤後前幾名）、Telegram 指令「題材 玻纖布」。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

import pandas as pd

from . import charts, config, notify, research
from .analysis import target_price
from .fetchers import stock_futures, tw_daily, yahoo
from .utils import CACHE_DIR, now_tw

log = logging.getLogger(__name__)

CARD_DIR = CACHE_DIR / "cards"
SENT_PATH = Path(research.ROOT) / "state" / "cards_sent.json"
MAX_STOCKS = 18


@dataclass
class Card:
    title: str
    subtitle: str = ""
    layers: list[dict] = field(default_factory=list)       # 供應鏈分層（研究資料）
    stocks: dict[str, dict] = field(default_factory=dict)  # 代號 → 每檔資料
    context: str = ""                                      # 觸發原因，例如「盤中資金湧入」
    research_date: str | None = None
    report_url: str = ""
    catalysts: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    spec_tables: list[dict] = field(default_factory=list)  # 技術細項：規格比較表
    evolution: list[dict] = field(default_factory=list)    # 世代演進
    concepts: list[dict] = field(default_factory=list)     # 關鍵概念解說
    conclusion: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- 找題材

def _norm(s: str) -> str:
    return re.sub(r"[\s（）()／/、,，:：\-]", "", str(s or "")).lower()


def research_data(topic: str) -> dict | None:
    p = research.data_dir() / f"{research.slug(topic)}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def find(query: str) -> dict | None:
    """依名稱找題材：研究過的題材優先，其次 themes.yaml 族群。

    回傳 {"title", "layers", "subtitle", "research_date", "report_url", "catalysts", "risks"}。
    """
    q = _norm(query)
    if not q:
        return None
    index = research.load_index()
    hits = [t for t in index if q in _norm(t) or _norm(t) in q]
    hits += [t for t, e in index.items() if t not in hits and any(_norm(k) == q for k in e.get("keywords", []))]
    if hits:
        return from_research(hits[0])
    themes_tw = config.themes().get("tw") or {}
    names = [t for t in themes_tw if _norm(t) == q] or [t for t in themes_tw if q in _norm(t) or _norm(t) in q]
    if names:
        return from_theme(names[0], themes_tw[names[0]])
    return None


def from_research(topic: str) -> dict:
    e = research.load_index().get(topic, {})
    d = research_data(topic) or {}
    layers = d.get("layers") or [{"layer": "相關個股", "global_players": [],
                                  "tw_stocks": [{"code": c, "name": "", "role": "", "impact": "受惠"}
                                                for c in e.get("codes", [])]}]
    return {"title": topic, "layers": layers, "subtitle": d.get("one_line", ""), "research_date": e.get("date"),
            "report_url": research.report_url(e["file"]) if e.get("file") else "",
            "catalysts": d.get("catalysts", []), "risks": d.get("risks", []),
            "spec_tables": d.get("spec_tables") or [], "evolution": d.get("evolution") or [],
            "concepts": d.get("concepts") or [], "conclusion": d.get("conclusion") or []}


def from_theme(name: str, codes: list) -> dict:
    """族群（themes.yaml）。名稱是「研究:XXX」時改用研究資料。"""
    if name.startswith("研究:"):
        topic = next((t for t in research.load_index() if f"研究:{t}"[:20] == name), None)
        if topic:
            return from_research(topic)
    linked = research.find_for_codes([str(c) for c in codes])
    if linked:
        out = from_research(linked)
        out["title"] = f"{name}（研究：{linked}）"
        return out
    return {"title": name, "subtitle": "", "research_date": None, "report_url": "", "catalysts": [], "risks": [],
            "layers": [{"layer": f"{name} 成分股", "global_players": [],
                        "tw_stocks": [{"code": str(c), "name": "", "role": "", "impact": "中性"} for c in codes]}]}


# ---------------------------------------------------------------- 資料

def _pct(a, b) -> float | None:
    try:
        return (float(a) / float(b) - 1) * 100 if a and b else None
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def fut_text(info: dict | None, known: bool = True) -> str:
    """「股期 LXF／小型 QEF」、「股期 HBF」、「無」；清單抓不到時「未知」。"""
    if not known:
        return "未知"
    if not info or not (info.get("std") or info.get("mini")):
        return "無"
    return "／".join(filter(None, [f"股期 {info['std']}" if info.get("std") else "",
                                   f"小型 {info['mini']}" if info.get("mini") else ""]))


def stock_row(code: str, df: pd.DataFrame | None, futures: dict, revenue: dict, alerts: dict,
              research_date: str | None) -> dict:
    d: dict = {"code": code}
    if df is not None and len(df) >= 2:
        close = df["close"]
        d["close"] = float(close.iloc[-1])
        d["pct1"] = _pct(close.iloc[-1], close.iloc[-2])
        d["pct5"] = _pct(close.iloc[-1], close.iloc[-6]) if len(close) > 5 else None
        d["pct20"] = _pct(close.iloc[-1], close.iloc[-21]) if len(close) > 20 else None
        if "volume" in df and len(df) >= 6:
            v = df["close"] * df["volume"]
            base = float(v.iloc[-6:-1].mean())
            d["value"] = float(v.iloc[-1])
            d["vol_ratio"] = float(v.iloc[-1]) / base if base else None
        if research_date:  # 研究日之後有新的交易日才算「研究後漲跌」
            idx = close.index.tz_convert("Asia/Taipei").tz_localize(None) if close.index.tz is not None else close.index
            cut = pd.Timestamp(research_date) + pd.Timedelta(days=1)
            before = close[idx < cut]
            if len(before) and idx[-1] >= cut:
                d["since_research"] = _pct(close.iloc[-1], before.iloc[-1])
        try:
            res = target_price.compute(df, symbol=code)
            d.update({"trend": res.trend, "target": res.conservative, "aggressive": res.aggressive,
                      "stop": res.stop, "rr": res.risk_reward, "rsi": res.rsi14, "notes": res.notes})
            for k in ("target", "aggressive", "stop"):
                d[f"{k}_pct"] = _pct(d.get(k), d["close"])
        except ValueError as e:
            log.info("目標價 %s：%s", code, e)
    d["fut"] = fut_text(futures.get(code) if futures else None, known=bool(futures))
    d["fut_short"] = "無股期" if d["fut"] == "無" else d["fut"]
    d["revenue"] = revenue.get(code)
    d["alerts"] = alerts.get(code, [])
    return d


def build(found: dict, listings: dict, context: str = "", futures: dict | None = None) -> Card:
    card = Card(title=found["title"], subtitle=found.get("subtitle", ""), context=context,
                research_date=found.get("research_date"), report_url=found.get("report_url", ""),
                catalysts=found.get("catalysts", []), risks=found.get("risks", []),
                spec_tables=found.get("spec_tables") or [], evolution=found.get("evolution") or [],
                concepts=found.get("concepts") or [], conclusion=found.get("conclusion") or [])
    seen: set[str] = set()
    layers = []
    for layer in found["layers"]:
        stocks = []
        for s in layer.get("tw_stocks", []):
            code = str(s.get("code"))
            if code in seen or code not in listings or len(seen) >= MAX_STOCKS:
                continue
            seen.add(code)
            stocks.append({**s, "code": code, "name": (listings.get(code) or {}).get("name") or s.get("name", "")})
        if stocks:
            layers.append({**layer, "tw_stocks": stocks})
    card.layers = layers
    codes = [s["code"] for l in layers for s in l["tw_stocks"]]
    futures = futures if futures is not None else stock_futures.load_stock_futures()
    revenue = tw_daily.load_revenue()
    alerts = tw_daily.load_alerts()
    sym = {yahoo.yahoo_symbol(c, (listings.get(c) or {}).get("market") or "tse"): c for c in codes}
    hist = {sym[s]: df for s, df in yahoo.fetch_many(list(sym), range_="1y").items()}
    for layer in layers:
        for s in layer["tw_stocks"]:
            row = stock_row(s["code"], hist.get(s["code"]), futures, revenue, alerts, card.research_date)
            row.update({"name": s["name"], "layer": layer.get("layer", ""), "role": s.get("role", ""),
                        "impact": s.get("impact", "中性"), "df": hist.get(s["code"])})
            card.stocks[s["code"]] = row
    return card


# ---------------------------------------------------------------- 輸出

def _f(v, nd=1, sign=False, suffix="") -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    if round(v, nd) == 0:
        v = 0.0  # 避免顯示 -0%
    return (f"{v:+.{nd}f}" if sign else f"{v:,.{nd}f}") + suffix


def _by_layer(card: Card) -> list[tuple[str, list[dict]]]:
    order = {"受惠": 0, "中性": 1, "受傷": 2}
    out = []
    for layer in card.layers:
        rows = [card.stocks[s["code"]] for s in layer["tw_stocks"] if s["code"] in card.stocks]
        rows.sort(key=lambda d: (order.get(d.get("impact"), 1), -(d.get("vol_ratio") or 0)))
        if rows:
            out.append((layer.get("layer", ""), rows))
    return out


def render(card: Card) -> list[Path]:
    """產生研究圖表，回傳檔案路徑。"""
    out_dir = CARD_DIR / f"{now_tw():%Y%m%d-%H%M%S}-{research.slug(card.title)}"
    paths = []
    groups = _by_layer(card)
    topic = re.split(r"[（(]研究", card.title)[0]
    # 技術細項解說（像產業懶人包）：規格比較表 → 世代演進 → 關鍵概念與結論
    for i, t in enumerate(card.spec_tables[:3]):
        try:
            paths.append(charts.spec_table_slide_png(out_dir / f"0{i}_spec.png", topic, t))
        except Exception as e:  # noqa: BLE001
            log.warning("規格表產生失敗：%s", e)
    for name, fn, args in (("evolution", charts.evolution_slide_png, (card.evolution,)),
                           ("concepts", charts.concepts_slide_png, (card.concepts, card.conclusion))):
        if args[0] or (name == "concepts" and card.conclusion):
            try:
                paths.append(fn(out_dir / f"0{5 if name == 'evolution' else 6}_{name}.png", topic, *args))
            except Exception as e:  # noqa: BLE001
                log.warning("%s 產生失敗：%s", name, e)
    try:
        if card.layers:
            paths.append(charts.supply_chain_png(out_dir / "1_supply_chain.png", card.title,
                                                 card.subtitle or card.context, card.layers, card.stocks))
    except Exception as e:  # noqa: BLE001
        log.warning("供應鏈地圖產生失敗：%s", e)
    try:
        paths.append(charts.sector_perf_png(out_dir / "2_sectors.png", card.title,
                                            [(name, [d.get("df") for d in rows]) for name, rows in groups]))
    except Exception as e:  # noqa: BLE001
        log.warning("細項產業表現圖產生失敗：%s", e)
    try:
        header = ["細項產業", "股票", "角色", "影響", "收盤", "漲跌", "20日", "量比", "營收年增", "月增", "股票期貨"]
        trs = []
        for name, rows in groups:
            for d in rows:
                rev = d.get("revenue") or {}
                trs.append([re.split(r"[（(]", name)[0][:8], f"{d['code']} {d['name']}" + (" ⚠" if d.get("alerts") else ""),
                            (d.get("role") or "")[:11], d.get("impact", "中性"), _f(d.get("close")),
                            _f(d.get("pct1"), 1, True, "%"), _f(d.get("pct20"), 1, True, "%"),
                            _f(d.get("vol_ratio"), 1), _f(rev.get("yoy"), 0, True, "%"), _f(rev.get("mom"), 0, True, "%"),
                            d["fut"].replace("股期 ", "").replace("小型 ", "小型")])
        ym = next((d["revenue"]["ym"] for _, rows in groups for d in rows if d.get("revenue")), "")
        paths.append(charts.table_png(out_dir / "3_table.png", f"{card.title}｜研究資料", header, trs,
                                      [1.3, 1.35, 1.75, 0.5, 0.75, 0.65, 0.7, 0.5, 0.75, 0.6, 1.15],
                                      note=f"營收＝{ym} 月營收；量比＝今日成交金額÷前 5 日平均；股票期貨欄＝一般股期代碼／小型股期代碼；"
                                           "⚠＝處置／注意股。研究內容僅供參考。",
                                      colorize={5: 5, 6: 6, 8: 8, 9: 9}, width=10.4))
    except Exception as e:  # noqa: BLE001
        log.warning("研究資料表產生失敗：%s", e)
    return paths


def message(card: Card) -> str:
    """文字版（圖片之外，方便複製與搜尋），依細項產業分組。"""
    lines = [f"🔬 研究圖表：{card.title}"]
    if card.context:
        lines.append(card.context)
    if card.research_date:
        lines.append(f"研究日期 {card.research_date}")
    if card.subtitle:
        lines += ["", card.subtitle]
    for name, rows in _by_layer(card):
        lines.append(f"\n【{name}】")
        for d in rows:
            mark = {"受惠": "▲", "受傷": "▼"}.get(d.get("impact"), "●")
            head = f"{mark} {d['code']} {d['name']} {_f(d.get('close'))}（{_f(d.get('pct1'), 1, True, '%')}）"
            if d.get("vol_ratio"):
                head += f" 量比{d['vol_ratio']:.1f}"
            lines.append(head)
            info = []
            if d.get("role"):
                info.append(d["role"])
            rev = d.get("revenue") or {}
            if rev.get("yoy") is not None:
                info.append(f"營收年增 {rev['yoy']:+.0f}%")
            info.append(d["fut"] if d["fut"] not in ("無", "未知") else f"股票期貨：{d['fut']}")
            if d.get("since_research") is not None:
                info.append(f"研究後 {d['since_research']:+.1f}%")
            if d.get("alerts"):
                info.append("⚠️ " + "、".join(d["alerts"]))
            lines.append("  " + "｜".join(info))
    if card.catalysts:
        lines += ["", "📅 催化劑：" + "；".join(card.catalysts[:3])]
    if card.risks:
        lines.append("⚠️ 風險：" + "；".join(card.risks[:3]))
    if card.report_url:
        lines += ["", f"完整研究：{card.report_url}"]
    return "\n".join(lines)


def send(card: Card, channel: str = "research") -> None:
    paths = render(card)
    caption = f"🔬 {card.title}" + (f"\n{card.context}" if card.context else "")
    notify.send_photos(paths, caption, channel=channel)
    notify.send(message(card), channel=channel)


def send_query(query: str, listings: dict, context: str = "", channel: str = "research") -> bool:
    found = find(query)
    if not found:
        return False
    send(build(found, listings, context=context), channel=channel)
    return True


# ---------------------------------------------------------------- 一天推一次

def _sent() -> dict:
    return json.loads(SENT_PATH.read_text(encoding="utf-8")) if SENT_PATH.exists() else {}


def already_sent(key: str, days: int = 1) -> bool:
    last = _sent().get(key)
    return bool(last) and last >= (now_tw() - timedelta(days=days - 1)).strftime("%Y-%m-%d")


def mark_sent(key: str) -> None:
    data = {k: v for k, v in _sent().items() if v >= (now_tw() - timedelta(days=30)).strftime("%Y-%m-%d")}
    data[key] = now_tw().strftime("%Y-%m-%d")
    SENT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SENT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
