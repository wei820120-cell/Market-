"""指令入口。

  python -m market_intel daily              盤後總報告（族群資金流向、法人、期權、目標價、新聞）→ reports/
  python -m market_intel realtime           盤中即時監控：族群資金流向排行、新聞/重大訊息、目標價/停損警示
  python -m market_intel realtime --once    只跑一次即時快照
  python -m market_intel news               掃描一次新聞與公告（漲價、缺貨、擴產…）
  python -m market_intel target 2330 NVDA   計算個股波段目標價
  python -m market_intel us                 美股類股 / 族群資金流向
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import time
from datetime import datetime, time as dtime

import pandas as pd

from . import ai, config, notify, research, theme_card
from .analysis import news_signals, picks, sector_flow, target_price
from .fetchers import news, stock_futures, taifex, telegram_channel, tw_daily, tw_realtime, yahoo
from .report import md_table, news_table
from .utils import CACHE_DIR, REPORT_DIR, ensure_dirs, now_tw

log = logging.getLogger("market_intel")


# ---------------------------------------------------------------- 共用

def _listings() -> dict[str, dict]:
    listings = tw_daily.load_listings()
    if not listings:
        log.warning("無法取得上市櫃清單，代號會同時查詢上市與上櫃")
    return listings


# 公司簡稱同時是常用詞，用名稱比對會誤判（仍可用代號比對，例如「全新(2455)」）
AMBIGUOUS_NAMES = {"全新", "大同", "統一", "中華", "聯合", "新光", "大眾", "正新", "台灣", "國產", "如興", "精星"}


def _name_to_code(listings: dict[str, dict]) -> dict[str, str]:
    """新聞比對用的「名稱 → 代號」。2 個字的短名稱容易誤判，只保留自選股與族群成分股。"""
    focus = set(config.tw_watch_codes()) | set(config.all_tw_theme_codes())
    out = {}
    for code, info in listings.items():
        name = (info.get("name") or "").strip()
        if not name or not code.isdigit() or name in AMBIGUOUS_NAMES:
            continue
        if len(name) >= 3 or code in focus:
            out[name] = code
    return out


def _us_symbols() -> set[str]:
    syms = set(config.us_watch_symbols())
    for members in (config.themes().get("us") or {}).values():
        syms.update(members)
    return syms


def collect_news(full: bool = True) -> list[news.NewsItem]:
    kw = config.news_keywords()
    items: list[news.NewsItem] = []
    items += news.fetch_mops()
    for cat in kw.get("cnyes_categories", ["tw_stock", "headline"]):
        items += news.fetch_cnyes(cat)
    for q in kw.get("google_queries_tw", []):
        items += news.fetch_google_news(q, "zh-TW")
    if full:
        for q in kw.get("google_queries_us", []):
            items += news.fetch_google_news(q, "en")
        items += news.fetch_yahoo_rss(sorted(_us_symbols()))
        items += news.fetch_sec_8k()
    return news.dedupe(items)


def _yahoo_histories(codes: list[str], listings: dict[str, dict], range_: str = "3mo") -> dict[str, pd.DataFrame]:
    sym_to_code = {yahoo.yahoo_symbol(c, (listings.get(c) or {}).get("market") or "tse"): c for c in codes}
    hist = yahoo.fetch_many(list(sym_to_code), range_=range_)
    return {sym_to_code[s]: df for s, df in hist.items()}


def compute_targets(listings: dict[str, dict], valuation: pd.DataFrame | None = None) -> pd.DataFrame:
    rows = []
    pe_map = {}
    if valuation is not None and not valuation.empty:
        pe_map = dict(zip(valuation["code"], valuation["pe"]))
    tw_items = [(c, yahoo.yahoo_symbol(c, (listings.get(c) or {}).get("market") or "tse")) for c in config.tw_watch_codes()]
    us_items = [(s, s) for s in config.us_watch_symbols()]
    targets_cache = {}
    for code, sym in tw_items + us_items:
        try:
            df, _ = yahoo.fetch_chart(sym, range_="1y")
            item = config.watch_item(code)
            eps = item.get("eps")
            if eps is None and pe_map.get(code):
                eps = float(df["close"].iloc[-1]) / pe_map[code]  # 由本益比反推近四季 EPS
            res = target_price.compute(df, symbol=code, eps=eps, target_pe=item.get("target_pe"))
            name = (listings.get(code) or {}).get("name", "")
            row = res.as_row()
            row["代號"] = f"{code} {name}".strip()
            rows.append(row)
            targets_cache[code] = {k: (round(v, 2) if v is not None else None) for k, v in
                                  {"target": res.conservative, "aggressive": res.aggressive, "stop": res.stop}.items()}
        except Exception as e:  # noqa: BLE001
            log.warning("目標價 %s 計算失敗：%s", code, e)
    (CACHE_DIR / "targets.json").write_text(json.dumps(targets_cache, ensure_ascii=False))
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- daily

def cmd_daily(args) -> None:
    ensure_dirs()
    today = now_tw()
    sections: list[str] = [f"# 盤後情報 {today:%Y-%m-%d}\n", f"_產生時間 {today:%Y-%m-%d %H:%M} (台北)_\n"]
    themes_tw = config.themes().get("tw") or {}

    day = tw_daily.fetch_day_all()
    listings = _listings()
    closes = dict(zip(day["code"], day["close"])) if not day.empty else {}
    names = {c: (v.get("name") or c) for c, v in listings.items()}

    # 1. 族群資金流向（台股）
    log.info("計算台股族群資金流向…")
    hist = _yahoo_histories(config.all_tw_theme_codes(), listings)
    sections.append("## 1. 台股族群資金流向（今日成交金額 vs 前 5 日平均）\n")
    theme_df = sector_flow.theme_flow_daily(hist, themes_tw, names)
    sections.append(md_table(theme_df))

    log.info("抓取官方類股成交…")
    sections.append("\n## 2. 官方產業類股成交比重變化（資金在產業間的移動）\n")
    sections.append(md_table(sector_flow.official_sector_flow(tw_daily.fetch_sector_turnover_history())))

    # 3. 三大法人
    log.info("抓取三大法人買賣超…")
    inst_date, inst = tw_daily.fetch_latest_institutional()
    sections.append(f"\n## 3. 三大法人買賣超（上市，{inst_date or '無資料'}）\n")
    sections.append("### 依族群加總\n")
    inst_theme = sector_flow.institutional_by_theme(inst, closes, themes_tw)
    sections.append(md_table(inst_theme))
    if not inst.empty and closes:
        inst = inst.copy()
        inst["close"] = inst["code"].map(closes)
        for col, label in (("foreign", "外資"), ("trust", "投信")):
            inst[f"{label}(億)"] = (inst[col] * inst["close"] / 1e8).round(2)
        for label in ("外資", "投信"):
            top = inst.dropna(subset=[f"{label}(億)"]).sort_values(f"{label}(億)", ascending=False)
            sections.append(f"\n### {label}買超前 15 名（金額）\n")
            sections.append(md_table(top[["code", "name", f"{label}(億)"]].rename(columns={"code": "代號", "name": "名稱"}), 15))
            sections.append(f"\n### {label}賣超前 10 名（金額）\n")
            sections.append(md_table(top.iloc[::-1][["code", "name", f"{label}(億)"]].rename(columns={"code": "代號", "name": "名稱"}), 10))

    # 4. 期貨選擇權
    log.info("抓取期交所資料…")
    fx = taifex.summarize(taifex.fetch_all())
    sections.append("\n## 4. 期貨 / 選擇權籌碼\n")
    if fx.get("pcr_oi") is not None:
        sections.append(f"- 臺指選擇權 Put/Call Ratio（未平倉）：**{fx['pcr_oi']}%**（成交量：{fx.get('pcr_volume')}%，{fx.get('pcr_date')}）\n")
    for who, v in (fx.get("tx_institutional_net_oi") or {}).items():
        sections.append(f"- 台指期 {who} 淨未平倉：**{v:,.0f} 口**\n")
    if len(fx) == 0:
        sections.append("_（期交所資料抓取失敗，原始資料見 data/taifex/）_\n")

    # 5. 市場成交金額前 20
    if not day.empty:
        sections.append("\n## 5. 成交金額前 20 名（資金最集中的個股）\n")
        top = day.dropna(subset=["value"]).sort_values("value", ascending=False).head(20).copy()
        top["成交金額(億)"] = (top["value"] / 1e8).round(2)
        top["漲跌%"] = top["pct"].round(2)
        sections.append(md_table(top[["code", "name", "close", "漲跌%", "成交金額(億)"]].rename(
            columns={"code": "代號", "name": "名稱", "close": "收盤"})))

    # 6. 美股
    sections.append("\n## 6. 美股類股 ETF 資金流向\n")
    sections.append(_us_section())

    # 7. 目標價
    log.info("計算自選股目標價…")
    valuation = None
    try:
        valuation = tw_daily.fetch_valuation()
    except Exception as e:  # noqa: BLE001
        log.warning("本益比資料抓取失敗：%s", e)
    sections.append("\n## 7. 自選股波段目標價\n")
    sections.append("_保守目標＝最近的上方目標，積極目標＝最遠的上方目標，停損＝最近的下方支撐；風報比＝(保守目標−現價)/(現價−停損)。僅供參考。_\n\n")
    sections.append(md_table(compute_targets(listings, valuation), 100))

    # 8. 新聞
    log.info("掃描新聞…")
    ranked = news_signals.rank(collect_news(), config.news_keywords(), _name_to_code(listings), _us_symbols(),
                               all_codes=set(listings))
    letters = [it for it in ranked if news_signals.is_price_letter(it)]
    sections.append("\n## 8. 新聞與公告訊號（漲價、缺貨、擴產、財測…）\n")
    sections.append(f"### 🚨 漲價信／公司公告調價（{len(letters)} 則）\n")
    sections.append(news_table(letters, 30))
    sections.append("\n### 全部新聞訊號\n")
    sections.append(news_table(ranked, 60))

    # 0. 強勢標的（放在報告最前面）
    log.info("計算強勢標的…")
    picks_df = daily_picks(day, listings, hist, inst, closes, theme_df, themes_tw, ranked)
    sections[2:2] = [
        "## 0. 今日強勢標的（資金流入＋題材＋漲價，附股票期貨／小型股票期貨）\n",
        "_分數＝量比×2（上限 10）＋法人買超每 5 億 1 分＋新聞題材分數＋漲價 4 分＋所屬族群資金流入 2 分；當天下跌打五折。"
        "股票期貨欄：CDF＝一般股票期貨代碼、小型＝小型股票期貨、夜盤＝有盤後交易時段。_\n\n",
        md_table(picks_df, 20) + "\n",
    ]

    path = REPORT_DIR / f"{today:%Y-%m-%d}.md"
    path.write_text("\n".join(sections), encoding="utf-8")
    (REPORT_DIR / "latest.md").write_text("\n".join(sections), encoding="utf-8")
    notify.send(build_summary(today, theme_df, inst_theme, fx))
    if ranked:
        notify.send(build_news_digest(today, ranked), channel="news")
    if not picks_df.empty:
        notify.send(picks.picks_message(picks_df, f"🎯 盤後強勢標的 {today:%m/%d}", n=10)
                    + f"\n\n完整報告：{_report_url(today)}", channel="picks")
    try:  # 全市場強勢股、營收爆發股，不在任何族群的 → 排入研究
        scan_new_themes(day, listings, stock_futures.load_stock_futures())
    except Exception as e:  # noqa: BLE001
        log.warning("強勢股／營收爆發掃描失敗：%s", e)
    try:  # 資金流入族群 → 研究機器人：題材卡（圖片、目標價、股票期貨）＋排入研究
        push_theme_cards(theme_df, picks_df, listings, "盤後")
    except Exception as e:  # noqa: BLE001
        log.warning("盤後題材卡推播失敗：%s", e)


def daily_picks(day: pd.DataFrame, listings: dict, hist: dict, inst: pd.DataFrame, closes: dict,
                theme_df: pd.DataFrame, themes_tw: dict, ranked: list, n_top_value: int = 150) -> pd.DataFrame:
    """盤後強勢標的：成交金額前 N 名＋族群成分股＋有正面新聞的個股，綜合評分。"""
    futures = stock_futures.load_stock_futures()
    nb = picks.news_by_code(ranked, themes_tw)
    theme_map = picks.themes_of(themes_tw)
    inflow = set()
    if theme_df is not None and not theme_df.empty:
        inflow = set(theme_df[theme_df["判讀"].str.contains("資金流入")]["族群"])
    codes = set(theme_map) | {c for c in nb if c in listings}
    if not day.empty:
        stocks = day[~day["code"].str.startswith("0")].dropna(subset=["value"])
        codes |= set(stocks.sort_values("value", ascending=False).head(n_top_value)["code"])
    missing = [c for c in codes if c not in hist]
    hist = {**hist, **_yahoo_histories(missing, listings, range_="1mo")} if missing else hist
    pct_map = dict(zip(day["code"], day["pct"])) if not day.empty else {}
    inst_map = {}
    if inst is not None and not inst.empty:
        for r in inst.itertuples():
            px = closes.get(r.code)
            if px and r.foreign is not None and r.trust is not None:
                inst_map[r.code] = (r.foreign + r.trust) * px / 1e8
    cands = []
    for c in codes:
        ratio = None
        h = hist.get(c)
        if h is not None and len(h) >= 6:
            v = h["close"] * h["volume"]
            base = float(v.iloc[-6:-1].mean())
            ratio = float(v.iloc[-1]) / base if base else None
            # 證交所盤後資料少數個股（如有 * 註記者）漲跌欄位不可靠，有日K時改用日K計算
            pct_map[c] = float(h["close"].iloc[-1] / h["close"].iloc[-2] - 1) * 100
        n = nb.get(c, {})
        ths = theme_map.get(c, [])
        pct = pct_map.get(c)
        cands.append(picks.Candidate(
            code=c, name=(listings.get(c) or {}).get("name", c),
            pct=None if pct is None or pd.isna(pct) else float(pct), ratio=ratio, inst=inst_map.get(c),
            news_score=n.get("score", 0.0), price_hike=n.get("hike", False), headline=n.get("headline", ""),
            price_letter=n.get("letter", False), theme_hike=n.get("theme_hike", False),
            themes=ths, theme_inflow=any(t in inflow for t in ths),
        ))
    return picks.rank_picks(cands, futures)


def _report_url(today: datetime) -> str:
    """在 GitHub Actions 上執行時，產生報告在 GitHub 上的網址。"""
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        return str(REPORT_DIR / f"{today:%Y-%m-%d}.md")
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    return f"{server}/{repo}/blob/main/reports/{today:%Y-%m-%d}.md"


def build_summary(today: datetime, theme_df: pd.DataFrame, inst_theme: pd.DataFrame, fx: dict) -> str:
    """手機推播用的盤後重點摘要。"""
    lines = [f"📊 盤後情報 {today:%m/%d}"]
    if theme_df is not None and not theme_df.empty:
        inflow = theme_df[theme_df["資金增減(億)"] > 0].head(3).to_dict("records")
        outflow = theme_df[theme_df["資金增減(億)"] < 0].tail(2).iloc[::-1].to_dict("records")
        if inflow:
            lines.append("\n🔥 資金流入族群")
            for r in inflow:
                lines.append(f"・{r['族群']} 量比{r['量比(對5日均)']} {r['加權漲跌%']:+.1f}% {r['判讀']}")
        if outflow:
            lines.append("❄️ 資金流出：" + "、".join(f"{r['族群']}({r['資金增減(億)']:+.0f}億)" for r in outflow))
    if inst_theme is not None and not inst_theme.empty:
        lines.append("\n🏦 法人買超族群")
        for r in inst_theme.head(3).to_dict("records"):
            lines.append(f"・{r['族群']} {r['三大法人合計(億)']:+.1f}億（外資{r['外資(億)']:+.1f} 投信{r['投信(億)']:+.1f}）")
    fx_lines = []
    if fx.get("pcr_oi") is not None:
        fx_lines.append(f"P/C Ratio {fx['pcr_oi']}%")
    for who, v in (fx.get("tx_institutional_net_oi") or {}).items():
        if "外資" in who:
            fx_lines.append(f"外資台指期 {v:+,.0f}口")
    if fx_lines:
        lines.append("\n📈 " + "｜".join(fx_lines))
    lines.append(f"\n完整報告：{_report_url(today)}")
    return "\n".join(lines)


def price_letter_message(it, listings: dict, futures: dict, themes_tw: dict) -> str:
    """漲價信推播：標題、相關個股（附股票期貨）、受惠族群。"""
    lines = [f"🚨 {it.tags[0]}", it.title]
    for c in [c for c in it.codes if c[:1].isdigit()][:5]:
        name = (listings.get(c) or {}).get("name", "")
        lines.append(f"・{c} {name}｜股期 {stock_futures.label(futures.get(c)) if futures else '未知'}")
    for th in it.themes:
        members = [str(c) for c in themes_tw.get(th, [])]
        with_fut = [c for c in members if futures and stock_futures.label(futures.get(c)) != "無"]
        lines.append(f"受惠族群【{th}】{len(members)} 檔，其中 {len(with_fut)} 檔有股票期貨")
    if it.url:
        lines.append(it.url)
    return "\n".join(lines)


def build_news_digest(today: datetime, ranked: list, n: int = 10) -> str:
    """新聞機器人用的盤後新聞整理：漲價信優先，再列分數最高的 N 則，附連結。"""
    lines = [f"📰 今日重點新聞 {today:%m/%d}"]
    letters = [it for it in ranked if news_signals.is_price_letter(it)]
    if letters:
        lines.append(f"\n🚨 漲價信／公司公告調價 {len(letters)} 則")
        for it in letters[:10]:
            codes = f"[{'、'.join(it.codes[:3])}] " if it.codes else ""
            ths = f"（受惠：{'、'.join(it.themes)}）" if it.themes else ""
            lines.append(f"・{codes}{it.title[:70]}{ths}\n{it.url}")
        lines.append("\n📰 其他重點新聞")
    for it in [x for x in ranked if x not in letters][:n]:
        codes = f"[{'、'.join(it.codes[:3])}] " if it.codes else ""
        lines.append(f"\n{it.score:+g} {codes}{it.title[:80]}\n{it.url}")
    return "\n".join(lines)


def _us_section() -> str:
    th = config.themes()
    etfs = th.get("us_sector_etfs") or {}
    out = []
    hist = yahoo.fetch_many(list(etfs), range_="3mo")
    out.append(md_table(sector_flow.us_etf_flow(hist, etfs)))
    us_themes = th.get("us") or {}
    if us_themes:
        syms = sorted({s for m in us_themes.values() for s in m})
        out.append("\n### 美股族群（成交額 vs 前 5 日平均）\n")
        out.append(md_table(sector_flow.theme_flow_daily(yahoo.fetch_many(syms, range_="1mo"), us_themes)))
    return "\n".join(out)


def cmd_us(args) -> None:
    print(_us_section())


# ---------------------------------------------------------------- realtime

def _in_session(now: datetime) -> bool:
    return now.weekday() < 5 and dtime(8, 59) <= now.time() <= dtime(13, 31)


def _flow_message(flow: pd.DataFrame, now: datetime, n: int = 5) -> str:
    lines = [f"⏱ {now:%H:%M} 族群資金流向"]
    for r in flow.head(n).to_dict("records"):
        lines.append(f"・{r['族群']} 步調{r['量能步調']} {r['加權漲跌%']:+.1f}% {r['判讀']}｜{r['領漲']}")
    out = flow[flow["超額資金(億)"].fillna(0) < 0].tail(2).iloc[::-1]
    if not out.empty:
        lines.append("❄️ 流出：" + "、".join(f"{r['族群']}({r['超額資金(億)']:+.0f}億)" for r in out.to_dict("records")))
    return "\n".join(lines)


def intraday_picks(quotes: dict, listings: dict, flow: pd.DataFrame, theme_map: dict, news_map: dict,
                   futures: dict, now: datetime) -> pd.DataFrame:
    """盤中強勢標的：個股量能步調＋漲跌＋今日新聞題材／漲價＋所屬族群是否資金流入。"""
    frac = sector_flow.expected_fraction(now)
    inflow = set()
    if flow is not None and not flow.empty:
        inflow = set(flow[flow["判讀"].str.contains("資金流入")]["族群"])
    cands = []
    for code, q in quotes.items():
        prev = (listings.get(code) or {}).get("prev_value") or 0
        ratio = q.turnover / (prev * frac) if prev and frac > 0 and q.turnover else None
        n = news_map.get(code, {})
        ths = theme_map.get(code, [])
        cands.append(picks.Candidate(
            code=code, name=q.name, pct=q.change_pct, ratio=ratio,
            news_score=n.get("score", 0.0), price_hike=n.get("hike", False), headline=n.get("headline", ""),
            price_letter=n.get("letter", False), theme_hike=n.get("theme_hike", False),
            themes=ths, theme_inflow=any(t in inflow for t in ths),
        ))
    return picks.rank_picks(cands, futures, min_ratio=1.5)


def cmd_realtime(args) -> None:
    ensure_dirs()
    st = (config.settings().get("realtime") or {})
    interval = args.interval or st.get("interval_seconds", 20)
    news_interval = st.get("news_interval_seconds", 60)
    news_min = st.get("news_min_score", 3)
    top_n = st.get("top_themes", 12)
    alert_pace = st.get("alert_theme_pace", 2.0)
    alert_pct = st.get("alert_stock_pct", 7.0)
    flow_times = sorted(str(t) for t in st.get("flow_push_times", []))
    until = dtime.fromisoformat(args.until) if args.until else None

    listings = _listings()
    themes_tw = config.themes().get("tw") or {}
    codes = list(dict.fromkeys(config.all_tw_theme_codes() + config.tw_watch_codes()))
    name_to_code = _name_to_code(listings)
    targets_path = CACHE_DIR / "targets.json"
    targets = json.loads(targets_path.read_text()) if targets_path.exists() else {}
    if not targets:
        log.info("尚無目標價快取，先計算自選股目標價…")
        try:
            compute_targets(listings)
            targets = json.loads(targets_path.read_text())
        except Exception as e:  # noqa: BLE001
            log.warning("目標價計算失敗，停用到價推播：%s", e)

    futures = stock_futures.load_stock_futures()
    theme_map = picks.themes_of(themes_tw)
    trade_rt = None
    try:
        trade_rt = _trade_intraday_setup(listings, futures)
    except Exception as e:  # noqa: BLE001
        log.warning("盤中進場訊號準備失敗：%s", e)
    if trade_rt:
        codes = list(dict.fromkeys(codes + list(trade_rt["levels"])))
    news_map: dict[str, dict] = {}  # 今天看過的新聞依個股彙總（題材、漲價）
    alerted: set[str] = set()
    pushed_times: set[str] = set()
    last_news = 0.0
    first_news = True
    checked_open = False
    while True:
        now = now_tw()
        if until and now.time() > until:
            log.info("已到結束時間 %s，停止監控。", args.until)
            break
        if not args.once and not args.force and not _in_session(now):
            log.info("非台股交易時段（09:00–13:30），只監控新聞。")
        else:
            quotes = tw_realtime.fetch_quotes(codes, listings)
            idx = tw_realtime.fetch_indices()
            if not checked_open and not args.force and idx and now.time() >= dtime(9, 5):
                checked_open = True
                taiex = idx.get("加權指數")
                if taiex and taiex.date and taiex.date != f"{now:%Y%m%d}":
                    log.info("加權指數資料日期 %s 不是今天，今天休市，停止監控。", taiex.date)
                    break
            if trade_rt and _in_session(now):
                try:
                    _trade_intraday_check(trade_rt, quotes, futures, now)
                except Exception as e:  # noqa: BLE001
                    log.warning("盤中進場訊號失敗：%s", e)
            flow = sector_flow.theme_flow_intraday(quotes, themes_tw, listings, now)
            head = " ｜ ".join(f"{k} {q.price:,.2f} ({q.change_pct:+.2f}%)" for k, q in idx.items()
                               if q.price is not None and q.change_pct is not None)
            print(f"\n===== {now:%H:%M:%S}  {head}  （同時段正常量比例 {sector_flow.expected_fraction(now):.0%}）=====")
            if not flow.empty:
                print(flow.head(top_n).to_string(index=False))
                picks_df = intraday_picks(quotes, listings, flow, theme_map, news_map, futures, now)
                due = [t for t in flow_times if t <= f"{now:%H:%M}" and t not in pushed_times]
                if due:
                    pushed_times.update(due)
                    notify.send((f"{head}\n" if head else "") + _flow_message(flow, now))
                    if not picks_df.empty:
                        notify.send(picks.picks_message(picks_df, f"🎯 {now:%H:%M} 強勢標的"), channel="picks")
                # 漲價題材＋資金湧入＋上漲：立即推播（每檔每天一次）
                for r in picks_df.to_dict("records") if not picks_df.empty else []:
                    key = f"hike:{r['代號']}:{now:%Y%m%d}"
                    if r["漲價"] and (r["量比"] or 0) >= 1.5 and (r["漲跌%"] or 0) > 0 and key not in alerted:
                        alerted.add(key)
                        fut = {"無": "無股票期貨", "未知": "股票期貨未知"}.get(r["股票期貨"], f"股票期貨 {r['股票期貨']}")
                        notify.send(f"🎯 漲價＋資金湧入：{r['代號']} {r['名稱']} {r['漲跌%']:+.2f}% 步調{r['量比']}倍｜{fut}\n{r['新聞']}",
                                    channel="picks")
                surged: list[str] = []
                for r in flow.to_dict("records"):
                    key = f"theme:{r['族群']}:{now:%Y%m%d}"
                    if (r["量能步調"] or 0) >= alert_pace and (r["加權漲跌%"] or 0) > 0 and key not in alerted:
                        alerted.add(key)
                        notify.send(f"🔥 資金湧入【{r['族群']}】量能步調 {r['量能步調']} 倍，加權漲 {r['加權漲跌%']}%，領漲：{r['領漲']}")
                        surged.append(r["族群"])
                if surged:  # 資金湧入的族群 → 研究機器人推題材卡
                    try:
                        push_theme_cards(flow[flow["族群"].isin(surged)], None, listings, "盤中", futures=futures)
                    except Exception as e:  # noqa: BLE001
                        log.warning("盤中題材卡推播失敗：%s", e)
            stock_msgs = []
            for code, q in quotes.items():
                if q.price is None:
                    continue
                t = targets.get(code) or {}
                checks = [
                    ("target", t.get("target") and q.price >= t["target"], f"🎯 {code} {q.name} 觸及保守目標 {t.get('target')}（現價 {q.price}）"),
                    ("stop", t.get("stop") and q.price <= t["stop"], f"🛑 {code} {q.name} 跌破停損 {t.get('stop')}（現價 {q.price}）"),
                    ("surge", q.change_pct is not None and q.change_pct >= alert_pct, f"🚀 {code} {q.name} 大漲 {q.change_pct:+.2f}%" if q.change_pct is not None else ""),
                ]
                for kind, cond, msg in checks:
                    key = f"{kind}:{code}:{now:%Y%m%d}"
                    if cond and key not in alerted:
                        alerted.add(key)
                        stock_msgs.append(msg)
            if stock_msgs:  # 同一輪的個股警示合併成一則，避免手機被洗版
                notify.send("\n".join(stock_msgs))

        if time.monotonic() - last_news >= news_interval:
            last_news = time.monotonic()
            fresh = news.only_new(collect_news(full=False))
            scored = news_signals.rank(fresh, config.news_keywords(), name_to_code, _us_symbols(),
                                       min_score=1, all_codes=set(listings))
            for c, d in picks.news_by_code(scored, themes_tw).items():
                m = news_map.setdefault(c, {"score": 0.0, "hike": False, "letter": False, "theme_hike": False,
                                            "headline": d["headline"]})
                m["score"] += d["score"]
                for k in ("hike", "letter", "theme_hike"):
                    m[k] = m[k] or d[k]
                if c in listings and c not in codes:
                    codes.append(c)  # 有題材的個股加入盤中報價監控
            if first_news:
                first_news = False
                log.info("已記錄 %d 則既有新聞（新聞推播由常駐監看 watch.yml 負責）", len(fresh))

        if args.once:
            break
        time.sleep(interval)


# ---------------------------------------------------------------- news / target

def push_news(listings: dict, min_score: float) -> None:
    """新聞推播（常駐監看每 2 分鐘，假日也跑）：漲價信全部推，其他分數夠高的每輪最多 5 則。

    已推過的記錄存在 state/seen_news.json（會存回 GitHub，換一輪監看也不會重複推）；第一次只記錄不推。
    """
    news.SEEN_PATH = research.ROOT / "state" / "seen_news.json"
    first = not news.SEEN_PATH.exists()
    news.SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    fresh = news.only_new(collect_news(full=False))
    if first:
        log.info("第一次掃描，記錄 %d 則既有新聞，之後只推新的", len(fresh))
        return
    themes_tw = config.themes().get("tw") or {}
    futures = stock_futures.load_stock_futures()
    scored = news_signals.rank(fresh, config.news_keywords(), _name_to_code(listings), _us_symbols(),
                               min_score=1, all_codes=set(listings))
    letters = [it for it in scored if news_signals.is_price_letter(it)]
    for it in letters:  # 漲價信：全部立即推播（只推新聞機器人；選股機器人只放選股結果）
        notify.send(price_letter_message(it, listings, futures, themes_tw), channel="news")
    # 重訊不限分數（rank 已替 fresh 裡每則打好分數、標好個股）
    mops = [it for it in fresh if it.source.startswith("MOPS") and it not in letters]
    hits = [it for it in scored if abs(it.score) >= min_score and it not in letters and it not in mops]
    for it in hits[:5]:
        notify.send(f"📰 [{it.score:+g}] {'、'.join(it.tags)} {'、'.join(it.codes)}\n{it.title}\n{it.url}", channel="news")
    try:
        supply_shocks(fresh, listings, futures)
    except Exception as e:  # noqa: BLE001
        log.warning("供需失衡偵測失敗：%s", e)
    msgs = [mops_message(it, listings, futures) for it in mops_to_push(mops, listings, futures)]
    for i in range(0, len(msgs), 8):  # 重訊每 8 則合併成一則，避免洗版
        notify.send("📢 公司重大訊息\n\n" + "\n\n".join(msgs[i:i + 8]), channel="news")


def shock_cause(text: str, causes: dict) -> str | None:
    """新聞是否為供需失衡事件，回傳原因（天災意外、政策管制…）。"""
    for cause, pats in (causes or {}).items():
        if any(re.search(p, text, flags=re.I) for p in pats):
            return cause
    return None


def shock_topic(it, listings: dict) -> str:
    """供需事件要研究的題材：先找產品（MLCC、DRAM…），其次個股，最後用標題。"""
    kw = config.news_keywords()
    text = f"{it.title} {it.summary}"
    products = (kw.get("price_letter") or {}).get("product_themes") or {}
    for product in sorted(products, key=len, reverse=True):
        if product.lower() in text.lower():
            return f"{product}供需"
    codes = [c for c in it.codes if c in listings]
    if codes:
        return f"{listings[codes[0]].get('name', codes[0])}（{codes[0]}）題材與產業"
    title = re.sub(r"\s*[-|｜].*$", "", it.title.split("：", 1)[-1]).strip()
    return f"供需事件：{title[:24]}"


def supply_shocks(items: list, listings: dict, futures: dict) -> list[str]:
    """供需失衡訊號 → 排入研究佇列＋推研究機器人（每天最多 N 個新題材）。"""
    kw = config.news_keywords()
    conf = kw.get("supply_shock") or {}
    exclude = kw.get("exclude_words") or []
    path = research.ROOT / "state" / "supply_shock.json"
    today = f"{now_tw():%Y-%m-%d}"
    st = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    count = st.get(today, 0)
    lines, queued = [], []
    for it in items:
        text = f"{it.title} {it.summary}"
        if any(w in it.title for w in exclude) or it.score < 0 and "減產" not in text:
            continue
        cause = shock_cause(text, conf.get("causes") or {})
        if not cause or count >= int(conf.get("max_per_day", 4)):
            continue
        topic = shock_topic(it, listings)
        if not research.queue_research(topic, f"{today} 供需失衡（{cause}）：{it.title}\n{it.summary[:300]}\n{it.url}"):
            continue
        count += 1
        queued.append(topic)
        stocks = "、".join(f"{c} {(listings.get(c) or {}).get('name', '')}｜{theme_card.fut_text((futures or {}).get(c), known=bool(futures))}"
                          for c in it.codes[:3] if c[:1].isdigit())
        lines.append(f"・【{cause}】{it.title[:80]}" + (f"\n  相關：{stocks}" if stocks else "")
                     + f"\n  → 研究題材：{topic}" + (f"\n  {it.url}" if it.url and "mops" not in it.url else ""))
    if lines:
        notify.send("🧭 供需失衡訊號（已排入研究，研究完成會推研究圖表）\n\n" + "\n\n".join(lines), channel="research")
        st = {today: count}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    return queued


def _focus_codes(futures: dict) -> set[str]:
    """重訊要推的股票：自選股、族群成分股、研究過的股票、股票期貨標的。"""
    codes = set(config.tw_watch_codes()) | set(config.all_tw_theme_codes()) | set(futures or {})
    for e in research.load_index().values():
        codes.update(e.get("codes", []))
    return codes


def mops_to_push(items: list, listings: dict, futures: dict) -> list:
    """公司重大訊息：略過例行公告；重點股票全推，其他股票有關鍵字訊號（漲價、擴產、上修、利空…）才推。"""
    skip = [re.compile(p) for p in (config.news_keywords().get("mops_skip") or [])]
    focus = _focus_codes(futures)
    out = []
    for it in items:
        subject = it.title.split("：", 1)[-1]
        if any(p.search(subject) for p in skip):
            continue
        if any(c in focus for c in it.codes) or it.score != 0:
            out.append(it)
    return out


def mops_message(it, listings: dict, futures: dict) -> str:
    code = it.codes[0] if it.codes else ""
    themes = picks.themes_of(config.themes().get("tw") or {}).get(code, [])
    fut = theme_card.fut_text((futures or {}).get(code), known=bool(futures))
    head = f"{'🔺' if it.score > 0 else ('🔻' if it.score < 0 else '・')} {it.title}"
    extra = [fut if fut not in ("無", "未知") else f"股票期貨：{fut}"]
    if themes:
        extra.append("族群：" + "、".join(themes[:3]))
    if it.tags:
        extra.append("訊號：" + "、".join(it.tags))
    body = re.sub(r"\s+", " ", it.summary or "")[:120]
    return "\n".join([head, "  " + "｜".join(extra)] + ([f"  {body}…"] if body else []))


def cmd_news(args) -> None:
    listings = _listings()
    if args.push:
        push_news(listings, (config.settings().get("realtime") or {}).get("news_min_score", 3))
        return
    ranked = news_signals.rank(collect_news(), config.news_keywords(), _name_to_code(listings), _us_symbols(),
                               min_score=args.min_score, all_codes=set(listings))
    for it in ranked[: args.limit]:
        print(f"[{it.score:+g}] {'、'.join(it.tags)} | {'、'.join(it.codes)} | {it.title} ({it.source}) {it.url}")


def cmd_target(args) -> None:
    listings = _listings() if any(s.isdigit() for s in args.symbols) else {}
    rows = []
    for s in args.symbols:
        sym = yahoo.yahoo_symbol(s, (listings.get(s) or {}).get("market") or "tse") if s.isdigit() else s
        df, _ = yahoo.fetch_chart(sym, range_="1y")
        res = target_price.compute(df, symbol=s, eps=args.eps, target_pe=args.pe)
        rows.append(res.as_row())
        print(f"\n{s}  現價 {res.last:.2f}  趨勢 {res.trend}  ATR {res.atr:.2f}")
        for k, v in res.targets.items():
            print(f"  目標 {k:<12} {v:>10.2f}  ({(v / res.last - 1) * 100:+.1f}%)")
        for k, v in res.stops.items():
            print(f"  停損 {k:<12} {v:>10.2f}  ({(v / res.last - 1) * 100:+.1f}%)")
    print()
    print(pd.DataFrame(rows).to_string(index=False))


def topic_themes_of(text: str) -> list[str]:
    """貼文提到的關鍵字 → 相關族群（config/sources.yaml 的 topic_themes）。"""
    out: list[str] = []
    lower = text.lower()
    for kw, ths in (config.sources().get("topic_themes") or {}).items():
        if str(kw).lower() in lower:
            for th in ([ths] if isinstance(ths, str) else ths):
                if th not in out:
                    out.append(th)
    return out


def channel_post_message(name: str, post, listings: dict, futures: dict, themes_tw: dict,
                         name_to_code: dict, max_chars: int = 1500) -> str:
    """頻道貼文推播：原文（截斷）＋相關個股／股票期貨＋相關族群＋訊號標籤。"""
    item = news.NewsItem(source=f"Telegram[{name}]", title=post.text, url=post.url)
    news_signals.tag_codes(item, name_to_code, _us_symbols(), set(listings))
    news_signals.score_item(item, config.news_keywords())
    themes = list(dict.fromkeys(topic_themes_of(post.text) + item.themes))
    when = post.published[11:16] if len(post.published) >= 16 else ""
    text = post.text if len(post.text) <= max_chars else post.text[:max_chars] + "…（完整內容見連結）"
    lines = [f"📣 {name}　{post.published[5:10].replace('-', '/')} {when} UTC".strip(), "", text, "", "—"]
    tw_codes = [c for c in item.codes if c[:1].isdigit()][:8]
    if tw_codes:
        lines.append("🔎 提到的個股")
        for c in tw_codes:
            lines.append(f"・{c} {(listings.get(c) or {}).get('name', '')}｜股期 "
                         f"{stock_futures.label(futures.get(c)) if futures else '未知'}")
    us = [c for c in item.codes if not c[:1].isdigit()]
    if us:
        lines.append("🇺🇸 " + "、".join(us))
    for th in themes:
        members = [str(c) for c in themes_tw.get(th, [])]
        if not members:
            continue
        with_fut = [f"{c}{(listings.get(c) or {}).get('name', '')}" for c in members
                    if futures and stock_futures.label(futures.get(c)) != "無"]
        lines.append(f"🧭 族群【{th}】{len(members)} 檔；有股期：{'、'.join(with_fut[:8]) or '無'}")
    if item.tags:
        lines.append("🏷 " + "、".join(item.tags))
    lines.append(post.url)
    return "\n".join(lines)


def cmd_channels(args) -> None:
    """檢查公開 Telegram 頻道新貼文並推播（config/sources.yaml）。"""
    channels = config.sources().get("telegram_channels") or []
    if not channels:
        log.info("config/sources.yaml 沒有設定 telegram_channels")
        return
    listings = _listings()
    name_to_code = _name_to_code(listings)
    futures = stock_futures.load_stock_futures()
    themes_tw = config.themes().get("tw") or {}
    state = telegram_channel.load_state()
    skip = config.sources().get("skip_patterns") or []
    for ch in channels:
        handle, name = ch["handle"], ch.get("name", ch["handle"])
        try:
            posts = telegram_channel.fetch_channel(handle)
        except Exception as e:  # noqa: BLE001
            log.warning("Telegram 頻道 %s 抓取失敗：%s", handle, e)
            continue
        log.info("頻道 %s：讀到 %d 則貼文", handle, len(posts))
        if args.latest:
            fresh = posts[-args.latest:]  # 測試用：推最新 N 則，不更新記錄
        else:
            fresh = telegram_channel.new_posts(handle, posts, state)
        for p in fresh:
            if telegram_channel.is_ad(p.text, skip):
                log.info("頻道 %s #%s 是業配／廣告，略過", handle, p.post_id)
                continue
            notify.send(channel_post_message(name, p, listings, futures, themes_tw, name_to_code),
                        channel=ch.get("push_to", "news"))
            if not args.latest and ch.get("research", True):
                research.add_to_inbox(handle, p.post_id, p.text, p.url)  # 交給題材研究員判斷要不要研究
    if not args.latest:
        telegram_channel.save_state(state)


AUTO_SLOTS = ("14:05", "21:05")  # 每天從新聞自動偵測新題材的時間（台北）


def _auto_due() -> bool:
    """監看迴圈用：到了 14:05／21:05 且這個時段還沒跑過。"""
    path = research.ROOT / "state" / "research_auto.json"
    now = now_tw()
    done = json.loads(path.read_text(encoding="utf-8")).get("done", []) if path.exists() else []
    done = [k for k in done if k.startswith(f"{now:%Y-%m-%d}")]
    due = [s for s in AUTO_SLOTS if f"{now:%H:%M}" >= s and f"{now:%Y-%m-%d} {s}" not in done]
    if not due:
        return False
    done += [f"{now:%Y-%m-%d} {s}" for s in due]  # 錯過的時段只補跑一次
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"done": done}, ensure_ascii=False), encoding="utf-8")
    return True


def _send_card_for_topic(topic: str, listings: dict, push: str, context: str) -> None:
    try:
        theme_card.send(theme_card.build(theme_card.from_research(topic), listings, context=context), channel=push)
    except Exception as e:  # noqa: BLE001
        log.warning("題材卡 %s 產生失敗：%s", topic, e)
    codes = (research.load_index().get(topic) or {}).get("codes", [])
    push_trade_candidates(f"{topic}（研究完成）", codes, listings, "🔬 研究完成，以下是研究裡有股期的標的")


def push_trade_candidates(title: str, codes: list[str], listings: dict, context: str = "",
                          futures: dict | None = None) -> None:
    """可卡位標的 → 進出場機器人（沒設定進出場機器人就略過）。"""
    if not os.environ.get("TELEGRAM_TRADE_BOT_TOKEN") or not codes:
        return
    try:
        from .trade import backtest as bt
        from .trade import candidates, real
        futures = futures if futures is not None else stock_futures.load_stock_futures()
        codes = [c for c in codes if (futures.get(c) or {}).get("std") or (futures.get(c) or {}).get("mini")]
        if not codes:
            notify.send(f"🎯 {title}：題材裡沒有股票期貨標的", channel="trade")
            return
        hist = _yahoo_histories(codes, listings, range_="1y")
        index_df, _ = yahoo.fetch_chart("^TWII", range_="1y")
        names = {c: (listings.get(c) or {}).get("name", c) for c in codes}
        rs = candidates.rows(codes, hist, futures, names, real.load()["equity"], _margin_rates())
        mkt = bool(bt.market_ok(index_df).iloc[-1]) if len(index_df) > 60 else False
        notify.send(candidates.message(title, context, rs, mkt), channel="trade")
    except Exception as e:  # noqa: BLE001
        log.warning("可卡位標的 %s 產生失敗：%s", title, e)


def cmd_research(args) -> None:
    """題材研究員：手動題材、Telegram 指令、頻道貼文收件匣、資金流入題材佇列、每日新聞自動偵測。"""
    st = ai.settings()
    push = st.get("push_to", "research")
    queue: list[tuple[str, str, bool]] = []  # (題材, 觸發來源, 是否自動)
    listings_cache: dict = {}

    def listings() -> dict:
        if not listings_cache:
            listings_cache.update(_listings())
        return listings_cache

    if args.topic:
        queue.append((args.topic, "手動指定", False))

    if args.commands:
        token = notify.bot_token(push)
        offset = research.load_offset(token)
        reqs, offset = research.pending_requests(notify.get_updates(push, offset), notify.owner_chat(push), offset)
        research.save_offset(token, offset)
        for kind, text in reqs:
            if kind == "card":
                if not theme_card.send_query(text, listings(), context=f"📩 查詢：{text}", channel=push):
                    names = list(research.load_index()) + list((config.themes().get("tw") or {}))[:30]
                    notify.send(f"找不到題材「{text}」。可以查：{'、'.join(names[:40])}", channel=push)
                continue
            if not ai.available():
                found = theme_card.find(text)
                if found:
                    notify.send(f"⚠️ 還沒設定 ANTHROPIC_API_KEY，無法重新研究「{text}」；先附上已有的資料。", channel=push)
                    theme_card.send(theme_card.build(found, listings(), context=f"📩 查詢：{text}"), channel=push)
                else:
                    notify.send(f"⚠️ 收到「研究 {text}」，但還沒設定 ANTHROPIC_API_KEY，無法進行 AI 研究。", channel=push)
                continue
            notify.send(f"🔬 收到，開始研究「{text}」，約 3～8 分鐘後回報。", channel=push)
            queue.append((text, "Telegram 指令", False))

    auto_texts: list[str] = []
    triggers: dict[str, str] = {}
    if args.inbox:
        items = research.read_inbox()
        for _, d in items:
            auto_texts.append(d["text"][:600])
            triggers[d["text"][:600]] = f"{d['channel']}：{d['text'][:1500]}\n{d.get('url', '')}"
        for p, _ in items:  # 讀過就刪，避免重複偵測（沒有金鑰也刪，免得越積越多）
            p.unlink(missing_ok=True)
    if args.auto_slots and _auto_due():
        args.auto = True
    if args.auto:
        listings_ = listings()
        ranked = news_signals.rank(collect_news(), config.news_keywords(), _name_to_code(listings_), _us_symbols(),
                                   min_score=3, all_codes=set(listings_))
        auto_texts += [it.title for it in ranked[:80]]

    cap = int(st.get("max_per_day", 4)) - research.researched_today()
    if args.inbox and ai.available():  # 資金流入但還沒研究過的題材（盤中、盤後排入）
        for p, d in research.read_queue():
            if cap <= 0:
                break
            queue.append((d["topic"], d.get("trigger", "資金流入"), True))
            p.unlink(missing_ok=True)
            cap -= 1

    if auto_texts and ai.available():
        if cap <= 0:
            log.info("今天自動研究已達上限 %s 個", st.get("max_per_day", 4))
        else:
            try:
                found = research.detect_topics(auto_texts)
            except ai.AIUnavailable as e:
                log.warning("題材偵測失敗：%s", e)
                found = []
            min_imp = int(st.get("min_importance", 3))
            for t in [x for x in found if x["importance"] >= min_imp][:cap]:
                src = next((v for k, v in triggers.items() if t["topic"] in k), "")
                queue.append((t["topic"], f"自動偵測（重要性 {t['importance']}）：{t['reason']}\n{src}", True))
    elif auto_texts:
        log.info("沒有 ANTHROPIC_API_KEY，略過自動題材偵測")

    pending = research.PENDING_DIR.exists() and any(research.PENDING_DIR.glob("*.json"))
    if not queue and not pending:
        return
    futures = stock_futures.load_stock_futures()
    for out in research.publish_pending(listings(), futures) if pending else []:
        notify.send(out["message"], channel=push)
        _send_card_for_topic(out["data"]["topic"], listings(), push, "🔬 研究完成")
    for topic, trigger, is_auto in queue:
        try:
            out = research.research_topic(topic, trigger, listings(), futures, auto=is_auto)
            notify.send(out["message"], channel=push)
            why = trigger.splitlines()[0][:80] if trigger else ""
            _send_card_for_topic(out["data"]["topic"], listings(), push, f"🔬 研究完成｜{why}" if why else "🔬 研究完成")
        except ai.AIUnavailable as e:
            log.warning("研究「%s」失敗：%s", topic, e)
            notify.send(f"⚠️ 研究「{topic}」沒有完成：{e}", channel=push)
            if "預算" in str(e) or "ANTHROPIC_API_KEY" in str(e):
                break


def cmd_card(args) -> None:
    """產生並推播題材卡（圖片＋目標價＋股票期貨），例如：python -m market_intel card 玻纖布"""
    if not theme_card.send_query(args.query, _listings(), context="", channel=args.channel):
        print(f"找不到題材：{args.query}")


def cmd_inflow(args) -> None:
    """盤後資金流入族群 → 研究機器人題材卡（不跑完整盤後報告）。"""
    listings = _listings()
    themes_tw = config.themes().get("tw") or {}
    names = {c: (v.get("name") or c) for c, v in listings.items()}
    hist = _yahoo_histories(config.all_tw_theme_codes(), listings)
    flow = sector_flow.theme_flow_daily(hist, themes_tw, names)
    print(flow.head(15).to_string(index=False) if not flow.empty else "沒有族群資料")
    push_theme_cards(flow, None, listings, "盤後", top=args.top)


SCAN_LIMIT_PCT, SCAN_MIN_VALUE = 9.5, 2e8         # 漲停（≥9.5%）且成交金額 ≥ 2 億
REV_MIN_YOY, REV_MIN_K = 100.0, 100_000           # 月營收年增 ≥ 100% 且當月營收 ≥ 1 億（千元）
SCAN_MAX_PER_DAY = 3


def scan_new_themes(day: pd.DataFrame, listings: dict, futures: dict) -> list[str]:
    """全市場掃描：漲停大量股、營收爆發股，不在任何族群／研究裡的排入研究（每天最多 3 檔），並推研究機器人。

    抓的是「還沒被歸類的新題材」，例如高明鐵這種中小型股。
    """
    known = set(_trade_theme_map())
    path = research.ROOT / "state" / "scan_seen.json"
    seen = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    today = f"{now_tw():%Y-%m-%d}"
    picks_: list[tuple[str, str, str]] = []  # (代號, 名稱, 原因)
    if day is not None and not day.empty:
        hot = day[(day["pct"] >= SCAN_LIMIT_PCT) & (day["value"] >= SCAN_MIN_VALUE)
                  & ~day["code"].astype(str).str.startswith("0")].sort_values("value", ascending=False)
        for r in hot.itertuples():
            if r.code not in known:
                picks_.append((r.code, r.name, f"漲停 {r.pct:+.1f}%、成交 {r.value / 1e8:.1f} 億"))
    revenue = tw_daily.load_revenue()
    for code, rv in sorted(revenue.items(), key=lambda kv: -(kv[1].get("yoy") or 0)):
        if code in known or code not in listings or (rv.get("yoy") or 0) < REV_MIN_YOY or (rv.get("rev") or 0) < REV_MIN_K:
            continue
        key = f"rev:{code}:{rv.get('ym')}"
        if key in seen:
            continue
        seen[key] = today
        picks_.append((code, listings[code].get("name", code),
                       f"{rv.get('ym')} 營收年增 {rv['yoy']:+.0f}%（{(rv.get('rev') or 0) / 1e5:.1f} 億）"))
    lines, queued = [], []
    for code, name, why in picks_:
        if len(queued) >= SCAN_MAX_PER_DAY:
            break
        topic = f"{name}（{code}）題材與產業"
        if not research.queue_research(topic, f"{today} 不在任何族群的強勢股：{code} {name}｜{why}"):
            continue
        queued.append(topic)
        fut = theme_card.fut_text(futures.get(code), known=bool(futures))
        lines.append(f"・{code} {name}｜{why}｜{fut if fut not in ('無', '未知') else '股票期貨：' + fut}")
    if lines:
        notify.send("🚀 不在任何族群的強勢股（已排入研究，找出題材後推研究圖表）\n" + "\n".join(lines), channel="research")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({k: v for k, v in seen.items() if v >= f"{now_tw().year - 1}"}, ensure_ascii=False),
                    encoding="utf-8")
    return queued


def push_theme_cards(flow: pd.DataFrame, picks_df: pd.DataFrame | None, listings: dict, source: str,
                     futures: dict | None = None, top: int = 3, channel: str = "research") -> None:
    """資金流入的族群／個股 → 研究細項產業 → 題材卡。

    （資金流入本身已由盤勢機器人、新聞機器人推過，這裡只負責「研究」與「題材卡」。）
    - 研究過的題材：直接推題材卡（細項產業分層＋目標價＋股票期貨），研究超過 30 天另排入更新
    - 還沒研究過的族群、不在任何族群的強勢股：排入研究佇列，研究完成後才推題材卡
    flow：盤後 theme_flow_daily 或盤中 theme_flow_intraday 的結果（要有「族群」「判讀」欄）。
    """
    themes_tw = config.themes().get("tw") or {}
    queued: list[str] = []
    if flow is not None and not flow.empty:
        money_col = "資金增減(億)" if "資金增減(億)" in flow else "超額資金(億)"
        ratio_col = "量比(對5日均)" if "量比(對5日均)" in flow else "量能步調"
        ratio_name = "量比" if "量比" in ratio_col else ratio_col
        inflow = flow[flow["判讀"].astype(str).str.contains("資金流入") & (flow[money_col].fillna(0) > 0)]
        for r in inflow.head(top).to_dict("records"):
            name = r["族群"]
            codes = [str(c) for c in themes_tw.get(name, [])]
            context = (f"🔥 {source}資金流入：{ratio_name} {r[ratio_col]}、資金 {r[money_col]:+.1f} 億、"
                       f"加權 {r['加權漲跌%']:+.1f}%")
            topic = research.topic_for_theme(name, codes)
            key_trade = f"trade:{source}:{name}"
            if not theme_card.already_sent(key_trade):  # 一條龍第一步：不等研究，先推可卡位標的
                push_trade_candidates(f"{name}", codes + list((research.load_index().get(topic) or {}).get("codes", [])),
                                      listings, context + ("" if topic else "｜研究中"), futures)
                theme_card.mark_sent(key_trade)
            if not topic:
                if research.queue_research(name, f"{now_tw():%Y-%m-%d} {context}\n成分股：{'、'.join(codes)}"):
                    queued.append(f"{name}（{context[2:]}）")
                continue
            if research.research_age_days(topic) > 30:
                research.queue_research(topic, f"{now_tw():%Y-%m-%d} 更新研究｜{context}")
            key = f"{source}:{name}"
            if theme_card.already_sent(key):
                continue
            try:
                found = theme_card.from_research(topic)
                if topic != name:
                    found["title"] = f"{name}（研究：{topic}）"
                theme_card.send(theme_card.build(found, listings, context=context, futures=futures), channel=channel)
                theme_card.mark_sent(key)
            except Exception as e:  # noqa: BLE001
                log.warning("題材卡 %s 產生失敗：%s", name, e)
    if picks_df is not None and not picks_df.empty and "族群" in picks_df:
        # 資金流入但不在任何族群：研究它屬於哪個細項產業、有什麼題材
        loose = picks_df[(picks_df["族群"].fillna("") == "") & (picks_df["分數"] >= 6)].head(2)
        for r in loose.to_dict("records"):
            topic = f"{r['名稱']}（{r['代號']}）題材與產業"
            if research.queue_research(topic, f"{now_tw():%Y-%m-%d} {source}資金流入、不在任何族群："
                                              f"{r['代號']} {r['名稱']}｜{r['理由']}｜{r.get('新聞', '')}"):
                queued.append(f"{r['代號']} {r['名稱']}（不在任何族群：{r['理由']}）")
    if queued:
        msg = "🔬 排入研究（拆解細項產業，完成後推研究圖表）：\n" + "\n".join(f"・{q}" for q in queued)
        if not ai.available():
            msg += "\n\n⚠️ 還沒設定 ANTHROPIC_API_KEY，不會自動研究；可以請 Claude 先研究這些題材。"
        notify.send(msg, channel=channel)


def _probe_sources() -> None:
    """資料檢查：找出期交所保證金、股期行情、月營收、處置／注意股的 OpenAPI 路徑與欄位。"""
    from . import net
    specs = [
        ("TAIFEX", "https://openapi.taifex.com.tw/swagger.json", "https://openapi.taifex.com.tw/v1",
         ("保證金", "Margin", "股票期貨", "個股")),
        ("TWSE", "https://openapi.twse.com.tw/v1/swagger.json", "https://openapi.twse.com.tw/v1",
         ("處置", "注意", "營業收入", "營收")),
        ("TPEX", "https://www.tpex.org.tw/openapi/swagger.json", "https://www.tpex.org.tw/openapi/v1",
         ("處置", "注意", "營業收入", "營收")),
    ]
    for name, sw_url, base, words in specs:
        try:
            sw = net.get_json(sw_url)
        except Exception as e:  # noqa: BLE001
            print(f"\n{name} swagger 失敗：{e}")
            continue
        for path, ops in (sw.get("paths") or {}).items():
            op = (ops or {}).get("get") or {}
            text = f"{path} {op.get('summary', '')} {op.get('description', '')}"
            if not any(w in text for w in words):
                continue
            print(f"\n{name} {path}：{op.get('summary', '')}")
            try:
                rows = net.get_json(base + path)
                if isinstance(rows, list):
                    print(f"  {len(rows)} 列；前 2 列：{json.dumps(rows[:2], ensure_ascii=False)[:700]}")
            except Exception as e:  # noqa: BLE001
                print(f"  讀取失敗：{e}")
    try:
        rows = taifex.fetch("futures_daily")
        stock = [r for r in rows if str(r.get("Contract", "")).startswith(("CD", "QF", "KU", "LY", "QS"))]
        print(f"\nDailyMarketReportFut：{len(rows)} 列；股期樣本：{json.dumps(stock[:4], ensure_ascii=False)[:1500]}")
        print("Contract 種類：", sorted({str(r.get('Contract')) for r in rows})[:400])
    except Exception as e:  # noqa: BLE001
        print(f"DailyMarketReportFut 失敗：{e}")
    for url in ("https://www.taifex.com.tw/cht/5/stockMargining", "https://www.taifex.com.tw/cht/5/indexMarging"):
        try:
            text = net.get(url).text
            rows = re.findall(r"<tr[^>]*>(.*?)</tr>", text, flags=re.S | re.I)
            print(f"\n{url}：{len(text)} 字，{len(rows)} 列")
            for r in rows[:6]:
                print("  ", re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "|", r))[:300])
        except Exception as e:  # noqa: BLE001
            print(f"{url} 失敗：{e}")


def _verify_margin() -> None:
    """保證金核對：期交所規則頁、原始比例資料、用期貨結算價重算幾檔。"""
    from . import net
    for url in ("https://www.taifex.com.tw/cht/5/margingReqSSF", "https://www.taifex.com.tw/cht/5/margingCal"):
        try:
            text = re.sub(r"\s+", " ", re.sub(r"<script.*?</script>|<style.*?</style>|<[^>]+>", " ",
                                              net.get(url).text, flags=re.S))
            i = text.find("保證金")
            print(f"\n== {url}\n{text[i:i + 2500]}")
        except Exception as e:  # noqa: BLE001
            print(f"{url} 失敗：{e}")
    rows = net.get_json("https://openapi.taifex.com.tw/v1/SingleStockFuturesMargining")
    quotes = {}
    for r in taifex.fetch("futures_daily"):
        m = str(r.get("ContractMonth(Week)", ""))
        if re.fullmatch(r"\d{6}", m) and "一般" in str(r.get("TradingSession", "")):
            c = r.get("Contract")
            if c not in quotes or m < quotes[c][0]:
                quotes[c] = (m, r.get("SettlementPrice"))
    print(f"\nSingleStockFuturesMargining {len(rows)} 列；契約有：{sorted({r.get('Contract') for r in rows})[:400]}")
    for code in ("1802", "2383", "2455", "8046", "2327", "6173", "2330", "3081"):
        for r in [r for r in rows if r.get("UnderlyingSecurityCode") == code]:
            c = r["Contract"]
            m, settle = quotes.get(c, (None, None))
            mult = 100 if c.startswith(("Q", "S", "U", "V", "P")) and r.get("ContractName", "").startswith("小型") else 2000
            rate = float(str(r.get("InitialMarginRate", "0")).replace("%", "")) / 100
            amt = float(settle) * mult * rate if settle not in (None, "", "-") else None
            print(json.dumps(r, ensure_ascii=False), "| 近月", m, "結算價", settle, "| 乘數", mult,
                  "| 原始保證金≈", round(amt) if amt else None)


def _backtest_factors(data, futures, index_df, rates, capital: float) -> None:
    """哪些「看對的條件」真的讓交易更賺：同一批交易，依進場前的條件分組比平均 R、勝率。"""
    from .trade import backtest as bt
    res = bt.run(data, futures, index_df, bt.Params(capital=capital, exit_ma="ma20"), rates)
    tr = pd.DataFrame(res["trades"])
    tr = tr[tr["feat"].apply(bool)]
    feats = pd.DataFrame(tr["feat"].tolist(), index=tr.index)
    tr = tr.join(feats)
    rows = []

    def stat(label, m):
        sub = tr[m]
        if len(sub):
            rows.append({"條件": label, "筆數": len(sub), "勝率%": round((sub["pnl"] > 0).mean() * 100, 1),
                         "平均R": round(float(sub["r"].mean()), 2), "總損益": round(float(sub["pnl"].sum()))})

    stat("全部交易", tr["pnl"].notna())
    for k, name in bt.FACTORS.items():
        stat(f"有：{name}", tr[k])
        stat(f"無：{name}", ~tr[k])
    n = feats.sum(axis=1)
    for lo, hi in ((0, 2), (3, 3), (4, 4), (5, 7)):
        stat(f"條件數 {lo}～{hi} 個" if lo != hi else f"條件數 {lo} 個", (n >= lo) & (n <= hi))
    for setup in ("突破", "拉回"):
        stat(f"型態：{setup}", tr["setup"] == setup)
    table = pd.DataFrame(rows)
    print(table.to_string(index=False))
    out = research.RESEARCH_DIR / "backtest"
    out.mkdir(parents=True, exist_ok=True)
    (out / "factors.json").write_text(table.to_json(orient="records", force_ascii=False, indent=1), encoding="utf-8")


def _backtest_speed(data, futures, index_df, rates, capital: float) -> None:
    """翻倍速度：同一套策略，不同風險／持倉數，從不同起點開始，看多久翻倍、中途最多回撤多少。"""
    from .trade import backtest as bt
    grid = [(r, m) for r in (0.02, 0.03, 0.04, 0.05) for m in (3, 4)] + [(0.06, 3)]
    starts = list(pd.date_range(index_df.index[0] + pd.Timedelta(days=100), index_df.index[-1] - pd.Timedelta(days=300),
                                freq="75D"))
    rows = []
    for risk, mp in grid:
        days, dds, finals = [], [], []
        for s0 in starts:
            warm = s0 - pd.Timedelta(days=120)
            res = bt.run({c: d[d.index >= warm] for c, d in data.items()}, futures, index_df[index_df.index >= warm],
                         bt.Params(capital=capital, exit_ma="ma20", risk_pct=risk, max_pos=mp), rates)
            s0n = s0.tz_localize(None) if s0.tzinfo else s0  # 曲線日期是不帶時區的字串日期
            eq = pd.Series([v for _, v in res["curve"]], index=pd.to_datetime([d for d, _ in res["curve"]]))
            eq = eq[eq.index >= s0n]
            if len(eq) < 20:
                continue
            ratio = eq / float(eq.iloc[0])
            hit = ratio[ratio >= 2.0]
            days.append((hit.index[0] - s0n).days if len(hit) else None)
            dds.append(float((ratio / ratio.cummax() - 1).min()) * 100)
            finals.append(float(ratio.iloc[-1]))
        ok = [d for d in days if d is not None]
        rows.append({"單筆風險%": risk * 100, "最多持倉": mp, "起點數": len(days),
                     "翻倍比例%": round(len(ok) / len(days) * 100) if days else 0,
                     "翻倍天數中位": int(pd.Series(ok).median()) if ok else None,
                     "最快": min(ok) if ok else None, "最慢": max(ok) if ok else None,
                     "一年內翻倍%": round(sum(d <= 365 for d in ok) / len(days) * 100) if days else 0,
                     "最大回撤中位%": round(float(pd.Series(dds).median()), 1) if dds else None,
                     "最大回撤最差%": round(min(dds), 1) if dds else None,
                     "最終倍數中位": round(float(pd.Series(finals).median()), 2) if finals else None})
        log.info("風險 %.0f%% 持倉 %d：%s", risk * 100, mp, rows[-1])
    table = pd.DataFrame(rows)
    print(table.to_string(index=False))
    out = research.RESEARCH_DIR / "backtest"
    out.mkdir(parents=True, exist_ok=True)
    (out / "speed.json").write_text(table.to_json(orient="records", force_ascii=False, indent=1), encoding="utf-8")


def cmd_backtest(args) -> None:
    """股票期貨波段策略回測（只做多），結果存到 research/backtest/。"""
    from . import net
    from .trade import backtest as bt
    listings = _listings()
    futures = stock_futures.load_stock_futures()
    codes = [c for c, v in futures.items() if c in listings and not c.startswith("00") and (v.get("std") or v.get("mini"))]
    log.info("回測標的：%d 檔（有股票期貨或小型股期）", len(codes))
    data = _yahoo_histories(codes, listings, range_=args.range)
    index_df, _ = yahoo.fetch_chart("^TWII", range_=args.range)
    rates = {}
    try:
        for r in net.get_json("https://openapi.taifex.com.tw/v1/SingleStockFuturesMargining"):
            rate = str(r.get("InitialMarginRate", "")).replace("%", "")
            if rate:
                rates[str(r.get("UnderlyingSecurityCode"))] = float(rate) / 100
    except Exception as e:  # noqa: BLE001
        log.warning("保證金比例抓取失敗，用 20.25%% 估：%s", e)
    if args.factors:
        _backtest_factors(data, futures, index_df, rates, args.capital)
        return
    if args.speed:
        _backtest_speed(data, futures, index_df, rates, args.capital)
        return
    if args.variants:
        variants = {
            "B 現行（3倍保證金）": {"exit_ma": "ma20"},
            "P1 試單1%＋加碼2次＋停損拉近＋減碼": {"exit_ma": "ma20", "pyramid": True},
            "P4 試單1%＋+1R加碼1次、停損不動、不減碼": {"exit_ma": "ma20", "pyramid": True, "add_levels": (1.0,),
                                                  "add_stops": (-1.0,), "reduce_ma": ""},
            "P5 試單1%＋+1R/+2R加碼、第2次才保本、不減碼": {"exit_ma": "ma20", "pyramid": True,
                                                     "add_stops": (-1.0, 0.0), "reduce_ma": ""},
            "P6 試單1.5%＋+1R/+2R加碼、第2次才保本、不減碼": {"exit_ma": "ma20", "pyramid": True, "trial_risk": 0.015,
                                                       "add_stops": (-1.0, 0.0), "reduce_ma": ""},
            "P7 同P5＋跌破10日線減碼": {"exit_ma": "ma20", "pyramid": True, "add_stops": (-1.0, 0.0)},
            "T1 品質分級 風險 2/2/2/3/4%（分數0~4）": {"exit_ma": "ma20", "tier_risk": (0.02, 0.02, 0.02, 0.03, 0.04)},
            "T2 品質分級 風險 1.5/1.5/2/3/4%": {"exit_ma": "ma20", "tier_risk": (0.015, 0.015, 0.02, 0.03, 0.04)},
            "T3 品質分級 風險 2/2/2/3/3%": {"exit_ma": "ma20", "tier_risk": (0.02, 0.02, 0.02, 0.03, 0.03)},
            "T4 品質分級 風險 2/2/3/4/5%": {"exit_ma": "ma20", "tier_risk": (0.02, 0.02, 0.03, 0.04, 0.05)},
            "P8 滿額2%試單＋+1R/+2R各加同口數、停損保本→+1R、不減碼": {
                "exit_ma": "ma20", "pyramid": True, "trial_risk": 0.02, "add_stops": (0.0, 1.0), "reduce_ma": ""},
            "P9 滿額2%試單＋只在+1R加一次、停損保本": {
                "exit_ma": "ma20", "pyramid": True, "trial_risk": 0.02, "add_levels": (1.0,), "add_stops": (0.0,),
                "reduce_ma": ""},
            "P10 試單1.5%＋+1R/+2R加碼、停損保本→+1R、不減碼": {
                "exit_ma": "ma20", "pyramid": True, "trial_risk": 0.015, "add_stops": (0.0, 1.0), "reduce_ma": ""},
        }
        cut = index_df.index[len(index_df) // 2]
        rows = []
        for name, kw in variants.items():
            full = bt.run(data, futures, index_df, bt.Params(capital=args.capital, **kw), rates)
            first = bt.run({c: d[d.index < cut] for c, d in data.items()}, futures, index_df[index_df.index < cut],
                           bt.Params(capital=args.capital, **kw), rates)
            second = bt.run({c: d[d.index >= cut - pd.Timedelta(days=120)] for c, d in data.items()}, futures,
                            index_df[index_df.index >= cut - pd.Timedelta(days=120)],
                            bt.Params(capital=args.capital, **kw), rates)
            rows.append({"策略": name, "報酬%": full["報酬率%"], "年化%": full["年化%"], "最大回撤%": full["最大回撤%"],
                         "筆數": full["交易筆數"], "勝率%": full["勝率%"], "平均R": full["平均R"],
                         "前半段%": first["報酬率%"], "後半段%": second["報酬率%"], "翻倍": full["翻倍日期"]})
        table = pd.DataFrame(rows)
        print(table.to_string(index=False))
        out = research.RESEARCH_DIR / "backtest"
        out.mkdir(parents=True, exist_ok=True)
        (out / "variants.json").write_text(table.to_json(orient="records", force_ascii=False, indent=1),
                                           encoding="utf-8")
        return
    for name, data_slice in (("全期間", None),):
        res = bt.run(data, futures, index_df, bt.Params(capital=args.capital), rates)
        out = research.RESEARCH_DIR / "backtest"
        bt.save(res, out / "latest.json")
        summary = {k: v for k, v in res.items() if k not in ("trades", "curve")}
        print(json.dumps(summary, ensure_ascii=False, indent=1))
        trades = pd.DataFrame(res["trades"])
        if not trades.empty:
            print(trades.sort_values("pnl").tail(8)[["code", "setup", "contract", "qty", "entry_date", "entry",
                                                     "exit_date", "exit", "pnl", "r", "reason"]].to_string())
            print(trades.groupby(trades["entry_date"].str[:7])["pnl"].sum().round().to_string())
        try:
            import matplotlib.pyplot as plt
            from . import charts
            charts.setup_font()
            eq = pd.Series([v for _, v in res["curve"]], index=pd.to_datetime([d for d, _ in res["curve"]]))
            fig, ax = plt.subplots(figsize=(8.6, 4), dpi=130)
            ax.plot(eq.index, eq.values, color="#24364f")
            ax.axhline(args.capital * 2, color="#d0312d", ls="--", lw=0.8)
            ax.set_title(f"回測權益曲線（起始 {args.capital:,.0f}）", loc="left")
            fig.tight_layout()
            fig.savefig(out / "equity.png")
        except Exception as e:  # noqa: BLE001
            log.warning("權益曲線圖失敗：%s", e)


TRADE_PRE, TRADE_POST, TRADE_POST_GIVEUP = "08:30", "15:30", "18:30"


def _trade_theme_map() -> dict[str, str]:
    """{代號: 題材}：族群成分股＋研究過的股票。"""
    themes = {}
    for name, members in (config.themes().get("tw") or {}).items():
        for c in map(str, members):
            themes.setdefault(c, name.replace("研究:", ""))
    for topic, e in research.load_index().items():
        for c in e.get("codes", []):
            themes[c] = topic
    return themes


def _margin_rates() -> dict[str, float]:
    from . import net
    rates = {}
    try:
        for r in net.get_json("https://openapi.taifex.com.tw/v1/SingleStockFuturesMargining"):
            rate = str(r.get("InitialMarginRate", "")).replace("%", "")
            if rate:
                rates[str(r.get("UnderlyingSecurityCode"))] = float(rate) / 100
    except Exception as e:  # noqa: BLE001
        log.warning("保證金比例抓取失敗：%s", e)
    return rates


def _trade_intraday_setup(listings: dict, futures: dict) -> dict | None:
    """盤中進場訊號的準備：股期標的的昨日關卡、大盤濾網、題材、保證金比例、權益。沒設定進出場機器人就不啟用。"""
    if not os.environ.get("TELEGRAM_TRADE_BOT_TOKEN"):
        return None
    from .trade import backtest as bt
    from .trade import intraday, real
    today = f"{now_tw():%Y-%m-%d}"

    def upto_yesterday(df):
        idx = df.index.tz_convert("Asia/Taipei") if df.index.tz is not None else df.index
        return df[[f"{d:%Y-%m-%d}" < today for d in idx]]

    codes = [c for c, v in futures.items() if c in listings and not c.startswith("00") and (v.get("std") or v.get("mini"))]
    hist = {c: upto_yesterday(df) for c, df in _yahoo_histories(codes, listings, range_="6mo").items()}
    index_df, _ = yahoo.fetch_chart("^TWII", range_="1y")
    index_df = upto_yesterday(index_df)
    lv = intraday.levels(hist)
    log.info("盤中進場訊號：監控 %d 檔股期標的", len(lv))
    return {"levels": lv, "market_ok": bool(bt.market_ok(index_df).iloc[-1]) if len(index_df) > 60 else False,
            "themes": _trade_theme_map(), "rates": _margin_rates(), "equity": real.load()["equity"],
            "alerted": set(), "confirmed": False}


def _trade_intraday_check(tr: dict, quotes: dict, futures: dict, now: datetime) -> None:
    from .trade import intraday
    frac = sector_flow.expected_fraction(now)
    sigs = intraday.check(quotes, tr["levels"], frac)
    confirm = not tr["confirmed"] and f"{now:%H:%M}" >= intraday.CONFIRM_AT
    from .trade import backtest as bt
    from .trade.paper import PARAMS
    for s in sigs:
        if s["code"] in tr["alerted"] or len(tr["alerted"]) >= 12:
            continue
        info = futures.get(s["code"], {})
        stop = s["price"] - PARAMS.atr_mult * s["atr"]
        doable = bt.size(tr["equity"], s["price"], stop, info, PARAMS, tr["rates"].get(s["code"], 0.2025))[2] > 0
        if s["code"] not in tr["themes"] and not doable:
            continue  # 沒題材、規則也做不了的不推，避免洗版
        tr["alerted"].add(s["code"])
        notify.send(intraday.message(s, futures.get(s["code"], {}), tr["themes"].get(s["code"], ""), tr["equity"],
                                     tr["rates"].get(s["code"], 0.2025), tr["market_ok"], "alert", now), channel="trade")
    if confirm:
        tr["confirmed"] = True
        ok = [s for s in sigs if s["code"] in tr["alerted"]]
        if not ok:
            notify.send(f"📋 {now:%H:%M} 收盤前確認：今天沒有仍站穩 20 日高的突破標的", channel="trade")
        for s in ok[:8]:
            notify.send(intraday.message(s, futures.get(s["code"], {}), tr["themes"].get(s["code"], ""), tr["equity"],
                                         tr["rates"].get(s["code"], 0.2025), tr["market_ok"], "confirm", now),
                        channel="trade")


def cmd_trade(args) -> None:
    """進出場機器人（模擬）。--auto：常駐監看用，平日 08:30 推盤前計劃、15:30 後盤後結算（每天各一次）。"""
    from . import net
    from .trade import paper
    path = research.ROOT / "state" / "trade" / "slots.json"
    done = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    now = now_tw()
    today = f"{now:%Y-%m-%d}"

    def mark(key):
        done[key] = today
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(done, ensure_ascii=False), encoding="utf-8")

    if args.commands:
        _trade_commands()
    if args.explain:
        listings = _listings()
        print(_trade_explain(args.explain, listings, stock_futures.load_stock_futures(), 100_000))
        return
    want_pre = args.pre or (args.auto and paper.is_weekday(now) and TRADE_PRE <= f"{now:%H:%M}" < "09:00"
                            and done.get("pre") != today)
    want_post = args.post or (args.auto and paper.is_weekday(now) and f"{now:%H:%M}" >= TRADE_POST
                              and done.get("post") != today)
    if want_pre:
        from .trade import real
        paper.push(paper.run_pre() + "\n\n" + real.positions_text(real.load()))
        mark("pre")
    if not want_post:
        return
    listings = _listings()
    futures = stock_futures.load_stock_futures()
    codes = [c for c, v in futures.items() if c in listings and not c.startswith("00") and (v.get("std") or v.get("mini"))]
    data = _yahoo_histories(codes, listings, range_="1y")
    index_df, _ = yahoo.fetch_chart("^TWII", range_="1y")
    rates = {}
    try:
        for r in net.get_json("https://openapi.taifex.com.tw/v1/SingleStockFuturesMargining"):
            rate = str(r.get("InitialMarginRate", "")).replace("%", "")
            if rate:
                rates[str(r.get("UnderlyingSecurityCode"))] = float(rate) / 100
    except Exception as e:  # noqa: BLE001
        log.warning("保證金比例抓取失敗：%s", e)
    names = {c: (v.get("name") or c) for c, v in listings.items()}
    if args.post and not index_df.empty:
        today = paper.last_date(index_df)  # 手動執行：用最近一個交易日結算
    themes = {}
    for name, members in (config.themes().get("tw") or {}).items():
        for c in map(str, members):
            themes.setdefault(c, name.replace("研究:", ""))
    for topic, e in research.load_index().items():
        for c in e.get("codes", []):
            themes[c] = topic
    msg = paper.run_post(data, index_df, futures, names, rates, tw_daily.load_alerts(), today, themes)
    if msg is None and f"{now:%H:%M}" < TRADE_POST_GIVEUP and not args.post:
        return  # 日 K 可能還沒更新，下一輪再試
    if msg is not None:  # 實單持倉：盤後檢查停損與出場
        from .trade import backtest as bt
        from .trade import real
        rst = real.load()
        prepped = {x["code"]: bt.prepare(data[x["code"]], paper.PARAMS) for x in rst["positions"] if x["code"] in data}
        day = index_df.index[-1]
        alerts = real.daily_check(rst, day, prepped)
        real.save(rst)
        closes = {c: float(d["close"].iloc[-1]) for c, d in prepped.items()}
        section = real.positions_text(rst, closes) + ("\n\n【出場提醒】\n" + "\n".join(alerts) if alerts else "")
        msg = section + "\n\n━━━━ 以下為模擬帳戶（對照用）━━━━\n" + (msg or "")
    paper.push(msg)
    mark("post")


def _trade_explain(query: str, listings: dict, futures: dict, equity: float) -> str:
    from . import net
    from .trade import backtest as bt
    from .trade import real
    hit = real.resolve(query, listings, futures)
    if not hit:
        return f"找不到「{query}」的股票期貨"
    code, name, _, _ = hit
    hist = _yahoo_histories([code], listings, range_="1y").get(code)
    index_df, _ = yahoo.fetch_chart("^TWII", range_="1y")
    if hist is None or len(hist) < 80:
        return f"{name} 歷史資料不足"
    rate = 0.2025
    try:
        for r in net.get_json("https://openapi.taifex.com.tw/v1/SingleStockFuturesMargining"):
            if str(r.get("UnderlyingSecurityCode")) == code:
                rate = float(str(r.get("InitialMarginRate", "20.25")).replace("%", "")) / 100
                break
    except Exception as e:  # noqa: BLE001
        log.warning("保證金比例抓取失敗：%s", e)
    return real.explain(code, name, hist, futures.get(code, {}), bool(bt.market_ok(index_df).iloc[-1]), equity, rate)


def _trade_commands() -> None:
    """讀進出場機器人收到的成交回報（只接受自己的訊息），記帳並回覆停損、目標與風險檢查。"""
    from . import net
    from .trade import paper, real
    token = os.environ.get("TELEGRAM_TRADE_BOT_TOKEN")
    if not token:
        return
    offset = research.load_offset(token)
    updates = notify.get_updates("trade", offset)
    owner = notify.owner_chat("trade")
    msgs = []
    for u in updates:
        offset = max(offset or 0, int(u.get("update_id", 0)) + 1)
        m = u.get("message") or {}
        if owner and str((m.get("chat") or {}).get("id")) != str(owner):
            continue
        if m.get("text"):
            msgs.append(m["text"])
    research.save_offset(token, offset)
    if not msgs:
        return
    listings = _listings()
    futures = stock_futures.load_stock_futures()
    st = real.load()
    for text in msgs:
        cmd = real.parse(text)
        if not cmd:
            notify.send("看不懂這則回報。格式例如：亞泥 35.7 1口、賣 亞泥 36.5 1口、持倉、說明", channel="trade")
            continue
        if cmd["cmd"] == "help":
            notify.send(real.HELP, channel="trade")
            continue
        if cmd["cmd"] == "positions":
            notify.send(real.positions_text(st), channel="trade")
            continue
        if cmd["cmd"] == "explain":
            notify.send(_trade_explain(cmd["query"], listings, futures, st["equity"]), channel="trade")
            continue
        hit = real.resolve(cmd["query"], listings, futures, cmd["mini"])
        if not hit:
            notify.send(f"找不到「{cmd['query']}」的股票期貨，請打股票名稱、代號或契約代碼（例如 亞泥、1102、DYF）",
                        channel="trade")
            continue
        code, name, contract, mult = hit
        if cmd["side"] == "sell":
            notify.send(real.sell(st, contract, name, cmd["price"], cmd["qty"]), channel="trade")
            continue
        hist = _yahoo_histories([code], listings, range_="6mo").get(code)
        from .analysis.indicators import atr as _atr
        a = float(_atr(hist).iloc[-1]) if hist is not None and len(hist) > 20 else cmd["price"] * 0.03
        rate = paper.PARAMS.margin_rate
        try:
            for r in net.get_json("https://openapi.taifex.com.tw/v1/SingleStockFuturesMargining"):
                if r.get("Contract") == contract or (r.get("UnderlyingSecurityCode") == code and rate == paper.PARAMS.margin_rate):
                    rate = float(str(r.get("InitialMarginRate", "20.25")).replace("%", "")) / 100
        except Exception as e:  # noqa: BLE001
            log.warning("保證金比例抓取失敗：%s", e)
        notify.send(real.buy(st, code, name, contract, mult, cmd["price"], cmd["qty"], a, rate), channel="trade")
    real.save(st)


def cmd_check(args) -> None:
    """資料檢查：印出各資料來源的原始欄位，不推播。"""
    _verify_margin()
    return
    print(json.dumps(stock_futures.raw_samples(), ensure_ascii=False, indent=1)[:6000])
    data = stock_futures.load_stock_futures()
    print(f"\n股票期貨標的：{len(data)} 檔；有小型：{sum(1 for v in data.values() if v.get('mini'))} 檔；"
          f"有夜盤：{sum(1 for v in data.values() if v.get('night'))} 檔")
    for code in ["2330", "2317", "2454", "2327", "2303", "3017", "0050", "6488"]:
        print(f"  {code}：{stock_futures.label(data.get(code))}")
    for ch in config.sources().get("telegram_channels") or []:
        try:
            posts = telegram_channel.fetch_channel(ch["handle"])
            print(f"\nTelegram {ch['handle']}：{len(posts)} 則有文字的貼文")
            for p in posts[-3:]:
                print(f"  #{p.post_id} {p.published} {p.text[:80]!r}")
        except Exception as e:  # noqa: BLE001
            print(f"\nTelegram {ch['handle']} 抓取失敗：{e}")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="market_intel", description="台美股 / 期權 即時情報與目標價")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("daily", help="盤後總報告").set_defaults(func=cmd_daily)
    sub.add_parser("us", help="美股類股資金流向").set_defaults(func=cmd_us)
    sub.add_parser("check", help="資料檢查：印出資料來源原始欄位（不推播）").set_defaults(func=cmd_check)
    rs = sub.add_parser("research", help="題材研究員（AI 供應鏈研究）")
    rs.add_argument("--topic", help="直接研究這個題材，例如：玻纖布")
    rs.add_argument("--commands", action="store_true", help="處理 Telegram「研究 XXX」指令")
    rs.add_argument("--inbox", action="store_true", help="從頻道新貼文偵測新題材並研究")
    rs.add_argument("--auto", action="store_true", help="從今天的重點新聞偵測新題材並研究")
    rs.add_argument("--auto-slots", action="store_true", help="監看用：到 14:05、21:05 時自動加上 --auto")
    rs.set_defaults(func=cmd_research)
    cd = sub.add_parser("card", help="推播題材卡（圖片＋目標價＋股票期貨），例如：card 玻纖布")
    cd.add_argument("query")
    cd.add_argument("--channel", default="research")
    cd.set_defaults(func=cmd_card)
    bk = sub.add_parser("backtest", help="股票期貨波段策略回測（只做多）")
    bk.add_argument("--capital", type=float, default=100_000)
    bk.add_argument("--range", default="3y")
    bk.add_argument("--variants", action="store_true", help="比較多組參數（含前後半段樣本外檢查）")
    bk.add_argument("--factors", action="store_true", help="看對的條件：依進場前條件分組比較勝率與平均 R")
    bk.add_argument("--speed", action="store_true", help="翻倍速度：不同風險／持倉數、不同起點，多久翻倍與回撤")
    bk.set_defaults(func=cmd_backtest)
    tr = sub.add_parser("trade", help="進出場機器人（模擬）")
    tr.add_argument("--auto", action="store_true", help="常駐監看用：到時間才執行")
    tr.add_argument("--pre", action="store_true", help="立即推盤前計劃")
    tr.add_argument("--post", action="store_true", help="立即盤後結算")
    tr.add_argument("--commands", action="store_true", help="處理機器人收到的成交回報")
    tr.add_argument("--explain", help="檢查某檔股票的訊號與口數，例如：--explain 國巨")
    tr.set_defaults(func=cmd_trade)
    fl = sub.add_parser("inflow", help="盤後資金流入族群 → 研究機器人題材卡")
    fl.add_argument("--top", type=int, default=3)
    fl.set_defaults(func=cmd_inflow)
    ch = sub.add_parser("channels", help="檢查公開 Telegram 頻道新貼文並推播")
    ch.add_argument("--latest", type=int, default=0, help="測試：直接推最新 N 則（不更新已讀記錄）")
    ch.set_defaults(func=cmd_channels)

    rt = sub.add_parser("realtime", help="盤中即時監控")
    rt.add_argument("--once", action="store_true", help="只跑一次")
    rt.add_argument("--force", action="store_true", help="非交易時段也抓報價")
    rt.add_argument("--interval", type=int, help="報價更新秒數")
    rt.add_argument("--until", help="到這個時間（HH:MM，台北時間）自動結束，例如 13:35")
    rt.set_defaults(func=cmd_realtime)

    nw = sub.add_parser("news", help="掃描新聞與公告")
    nw.add_argument("--limit", type=int, default=50)
    nw.add_argument("--min-score", type=float, default=None)
    nw.add_argument("--push", action="store_true", help="推播新出現的新聞與漲價信（常駐監看用）")
    nw.set_defaults(func=cmd_news)

    tg = sub.add_parser("target", help="計算目標價")
    tg.add_argument("symbols", nargs="+", help="台股代號（2330）或美股代號（NVDA）")
    tg.add_argument("--eps", type=float, help="EPS（用來算本益比目標價）")
    tg.add_argument("--pe", type=float, help="目標本益比")
    tg.set_defaults(func=cmd_target)

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        args.func(args)
    except KeyboardInterrupt:
        print("\n已停止")
