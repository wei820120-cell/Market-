"""題材卡：把一個題材（族群）的完整交易資料整理成圖片＋文字，推到研究機器人。

每檔股票：
- 價量：收盤、漲跌（1／5／20 日）、量比（今日成交金額 ÷ 前 5 日平均）、趨勢
- 波段目標價：保守目標、積極目標、停損、風報比（analysis/target_price.py）
- 股票期貨／小型股票期貨：契約代碼、每口股數、一口契約價值、近月期貨價、期現價差、成交量、未平倉、夜盤
- 月營收：最新月份年增、月增
- 處置股、注意股警示
- 研究後漲跌（題材研究過的話）

圖片：
1. 供應鏈地圖（研究過的題材才有分層；沒有研究的族群只列成分股）
2. 目標價總表
3. 股票期貨明細表
4. 個股走勢小圖（標出目標價與停損）

觸發：盤中族群資金湧入、盤後資金流入前幾名族群、題材研究完成、Telegram 指令「題材 玻纖布」。
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
from .fetchers import futures_detail, stock_futures, tw_daily, yahoo
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
            "catalysts": d.get("catalysts", []), "risks": d.get("risks", [])}


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


def stock_row(code: str, df: pd.DataFrame | None, futures: dict, detail: dict, revenue: dict, alerts: dict,
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
    info = futures.get(code) if futures else None
    d["futures"] = futures_detail.contracts(code, d.get("close"), info, detail)
    d["fut_label"] = stock_futures.label(info) if futures else "未知"
    std = next((c for c in d["futures"] if c["kind"] == "一般"), None)
    mini = next((c for c in d["futures"] if c["kind"] == "小型"), None)
    d["fut_short"] = "／".join(filter(None, [f"股期 {std['contract']}" if std else "",
                                              f"小型 {mini['contract']}" if mini else ""])) or "無股期"
    d["revenue"] = revenue.get(code)
    d["alerts"] = alerts.get(code, [])
    return d


def build(found: dict, listings: dict, context: str = "", futures: dict | None = None) -> Card:
    card = Card(title=found["title"], subtitle=found.get("subtitle", ""), context=context,
                research_date=found.get("research_date"), report_url=found.get("report_url", ""),
                catalysts=found.get("catalysts", []), risks=found.get("risks", []))
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
    detail = futures_detail.load_detail()
    revenue = tw_daily.load_revenue()
    alerts = tw_daily.load_alerts()
    sym = {yahoo.yahoo_symbol(c, (listings.get(c) or {}).get("market") or "tse"): c for c in codes}
    hist = {sym[s]: df for s, df in yahoo.fetch_many(list(sym), range_="1y").items()}
    for layer in layers:
        for s in layer["tw_stocks"]:
            row = stock_row(s["code"], hist.get(s["code"]), futures, detail, revenue, alerts, card.research_date)
            row.update({"name": s["name"], "layer": layer.get("layer", ""), "role": s.get("role", ""),
                        "impact": s.get("impact", "中性"), "df": hist.get(s["code"])})
            card.stocks[s["code"]] = row
    return card


# ---------------------------------------------------------------- 輸出

def _f(v, nd=1, sign=False, suffix="") -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    return (f"{v:+.{nd}f}" if sign else f"{v:,.{nd}f}") + suffix


def _ordered(card: Card) -> list[dict]:
    order = {"受惠": 0, "中性": 1, "受傷": 2}
    return sorted(card.stocks.values(), key=lambda d: (order.get(d.get("impact"), 1), -(d.get("vol_ratio") or 0)))


def render(card: Card) -> list[Path]:
    """產生圖片，回傳檔案路徑。"""
    out_dir = CARD_DIR / f"{now_tw():%Y%m%d-%H%M%S}-{research.slug(card.title)}"
    paths = []
    rows = _ordered(card)
    try:
        if card.layers:
            paths.append(charts.supply_chain_png(out_dir / "1_supply_chain.png", card.title,
                                                 card.subtitle or card.context, card.layers, card.stocks))
        header = ["股票", "收盤", "漲跌", "5日", "量比", "營收年增", "保守目標", "積極目標", "停損", "風報比"]
        trs = []
        for d in rows:
            rev = d.get("revenue") or {}
            name = f"{d['code']} {d['name']}" + (" ⚠" if d.get("alerts") else "")
            trs.append([name, _f(d.get("close")), _f(d.get("pct1"), 1, True, "%"), _f(d.get("pct5"), 1, True, "%"),
                        _f(d.get("vol_ratio"), 1), _f(rev.get("yoy"), 0, True, "%"),
                        f"{_f(d.get('target'), 0)}({_f(d.get('target_pct'), 0, True)}%)" if d.get("target") else "—",
                        f"{_f(d.get('aggressive'), 0)}({_f(d.get('aggressive_pct'), 0, True)}%)" if d.get("aggressive") else "—",
                        f"{_f(d.get('stop'), 0)}({_f(d.get('stop_pct'), 0, True)}%)" if d.get("stop") else "—",
                        _f(d.get("rr"), 1)])
        paths.append(charts.table_png(out_dir / "2_targets.png", f"{card.title}｜波段目標價", header, trs,
                                      [1.55, 0.75, 0.7, 0.7, 0.55, 0.75, 1.15, 1.15, 1.1, 0.6],
                                      note="保守目標＝最近的上方目標，積極目標＝最遠的上方目標，停損＝最近支撐；"
                                           "風報比＝(保守目標−收盤)/(收盤−停損)。⚠＝處置／注意股。僅供參考。",
                                      colorize={2: 2, 3: 3, 5: 5}))
        fh = ["股票", "契約", "每口", "一口價值", "近月期價", "期現價差", "成交量", "未平倉", "夜盤"]
        frs = []
        for d in rows:
            for c in d.get("futures") or []:
                frs.append([f"{d['code']} {d['name']}", f"{c['kind']} {c['contract']}", f"{c['shares']:,}股",
                            futures_detail.wan(c.get("value")), _f(c.get("last")), _f(c.get("basis"), 1, True),
                            _f(c.get("volume"), 0), _f(c.get("oi"), 0), "有" if c.get("night") else "—"])
        no_fut = [f"{d['code']} {d['name']}" for d in rows if not d.get("futures")]
        if frs:
            paths.append(charts.table_png(out_dir / "3_futures.png", f"{card.title}｜股票期貨／小型股票期貨", fh, frs,
                                          [1.5, 1.05, 0.75, 0.95, 0.9, 0.9, 0.8, 0.8, 0.55],
                                          note=("沒有股票期貨：" + "、".join(no_fut) + "。" if no_fut else "")
                                               + "一口價值＝收盤×每口股數；期現價差＝近月期價−收盤（正＝正價差）。",
                                          colorize={5: 5}))
        items = [{"label": f"{d['code']} {d['name']}", "df": d["df"], "target": d.get("target"),
                  "aggressive": d.get("aggressive"), "stop": d.get("stop")} for d in rows if d.get("df") is not None]
        if items:
            paths.append(charts.price_grid_png(out_dir / "4_charts.png", f"{card.title}｜走勢與目標價", items))
    except Exception as e:  # noqa: BLE001
        log.warning("題材卡圖片產生失敗：%s", e)
    return paths


def message(card: Card) -> str:
    """文字版（圖片之外，方便複製與搜尋）。"""
    lines = [f"📊 題材卡：{card.title}"]
    if card.context:
        lines.append(card.context)
    if card.research_date:
        lines.append(f"🔬 研究日期 {card.research_date}")
    if card.subtitle:
        lines += ["", card.subtitle]
    for d in _ordered(card):
        mark = {"受惠": "▲", "受傷": "▼"}.get(d.get("impact"), "●")
        head = f"\n{mark} {d['code']} {d['name']} {_f(d.get('close'))}（{_f(d.get('pct1'), 1, True, '%')}）"
        if d.get("vol_ratio"):
            head += f" 量比{d['vol_ratio']:.1f}"
        lines.append(head)
        if d.get("target"):
            lines.append(f"  目標 {_f(d['target'], 1)}（{_f(d.get('target_pct'), 0, True)}%）／"
                         f"{_f(d.get('aggressive'), 1)}（{_f(d.get('aggressive_pct'), 0, True)}%）"
                         f"｜停損 {_f(d.get('stop'), 1)}（{_f(d.get('stop_pct'), 0, True)}%）｜風報比 {_f(d.get('rr'), 1)}")
        futs = d.get("futures") or []
        if futs:
            for c in futs:
                lines.append(f"  {c['kind']}股期 {c['contract']}：一口 {c['shares']:,} 股≈{futures_detail.wan(c.get('value'))}"
                             f"｜期價 {_f(c.get('last'))}（價差 {_f(c.get('basis'), 1, True)}）"
                             f" 量 {_f(c.get('volume'), 0)} 未平倉 {_f(c.get('oi'), 0)}"
                             + ("｜夜盤" if c.get("night") else ""))
        else:
            lines.append(f"  股票期貨：{d.get('fut_label', '無')}")
        extra = []
        rev = d.get("revenue") or {}
        if rev.get("yoy") is not None:
            extra.append(f"{rev.get('ym', '')} 營收年增 {rev['yoy']:+.0f}%、月增 {_f(rev.get('mom'), 0, True)}%")
        if d.get("since_research") is not None:
            extra.append(f"研究後 {d['since_research']:+.1f}%")
        if d.get("alerts"):
            extra.append("⚠️ " + "、".join(d["alerts"]))
        if extra:
            lines.append("  " + "｜".join(extra))
    if card.catalysts:
        lines += ["", "📅 催化劑：" + "；".join(card.catalysts[:3])]
    if card.risks:
        lines.append("⚠️ 風險：" + "；".join(card.risks[:3]))
    if card.report_url:
        lines += ["", f"完整研究：{card.report_url}"]
    lines.append("\n目標價依歷史價量推算，僅供參考")
    return "\n".join(lines)


def send(card: Card, channel: str = "research") -> None:
    paths = render(card)
    caption = f"📊 {card.title}" + (f"\n{card.context}" if card.context else "")
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
