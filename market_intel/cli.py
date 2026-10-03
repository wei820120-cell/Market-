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
import time
from datetime import datetime, time as dtime

import pandas as pd

from . import ai, config, notify, research
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
                for r in flow.to_dict("records"):
                    key = f"theme:{r['族群']}:{now:%Y%m%d}"
                    if (r["量能步調"] or 0) >= alert_pace and (r["加權漲跌%"] or 0) > 0 and key not in alerted:
                        alerted.add(key)
                        notify.send(f"🔥 資金湧入【{r['族群']}】量能步調 {r['量能步調']} 倍，加權漲 {r['加權漲跌%']}%，領漲：{r['領漲']}")
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
            if first_news and not args.once:
                # 第一次掃描只記錄已經存在的新聞，避免一啟動就把舊新聞全部推出去
                first_news = False
                log.info("已記錄 %d 則既有新聞，之後只推播新出現的。", len(fresh))
            else:
                letters = [it for it in scored if news_signals.is_price_letter(it)]
                for it in letters:  # 漲價信：全部立即推播，不受每輪 5 則上限
                    msg = price_letter_message(it, listings, futures, themes_tw)
                    notify.send(msg, channel="news")
                    if it.codes or it.themes:
                        notify.send(msg, channel="picks")
                hits = [it for it in scored if abs(it.score) >= news_min and it not in letters]
                for it in hits[:5]:
                    notify.send(f"📰 [{it.score:+g}] {'、'.join(it.tags)} {'、'.join(it.codes)}\n{it.title}\n{it.url}", channel="news")

        if args.once:
            break
        time.sleep(interval)


# ---------------------------------------------------------------- news / target

def cmd_news(args) -> None:
    listings = _listings()
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
            notify.send(channel_post_message(name, p, listings, futures, themes_tw, name_to_code),
                        channel=ch.get("push_to", "news"))
            if not args.latest and ch.get("research", True):
                research.add_to_inbox(handle, p.post_id, p.text, p.url)  # 交給題材研究員判斷要不要研究
    if not args.latest:
        telegram_channel.save_state(state)


def cmd_research(args) -> None:
    """題材研究員：手動題材、Telegram「研究 XXX」指令、頻道貼文收件匣、每日新聞自動偵測。"""
    st = ai.settings()
    push = st.get("push_to", "research")
    queue: list[tuple[str, str, bool]] = []  # (題材, 觸發來源, 是否自動)

    if args.topic:
        queue.append((args.topic, "手動指定", False))

    if args.commands:
        offset = research.load_offset()
        topics, offset = research.pending_commands(notify.get_updates(push, offset), notify.owner_chat(push), offset)
        research.save_offset(offset)
        for t in topics:
            if not ai.available():
                notify.send(f"⚠️ 收到「研究 {t}」，但還沒設定 ANTHROPIC_API_KEY，無法進行 AI 研究。", channel=push)
                continue
            notify.send(f"🔬 收到，開始研究「{t}」，約 3～8 分鐘後回報。", channel=push)
            queue.append((t, "Telegram 指令", False))

    auto_texts: list[str] = []
    triggers: dict[str, str] = {}
    if args.inbox:
        items = research.read_inbox()
        for _, d in items:
            auto_texts.append(d["text"][:600])
            triggers[d["text"][:600]] = f"{d['channel']}：{d['text'][:1500]}\n{d.get('url', '')}"
        for p, _ in items:  # 讀過就刪，避免重複偵測（沒有金鑰也刪，免得越積越多）
            p.unlink(missing_ok=True)
    if args.auto:
        listings_ = _listings()
        ranked = news_signals.rank(collect_news(), config.news_keywords(), _name_to_code(listings_), _us_symbols(),
                                   min_score=3, all_codes=set(listings_))
        auto_texts += [it.title for it in ranked[:80]]

    if auto_texts and ai.available():
        cap = int(st.get("max_per_day", 4)) - research.researched_today()
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

    if not queue:
        return
    listings = _listings()
    futures = stock_futures.load_stock_futures()
    for topic, trigger, is_auto in queue:
        try:
            out = research.research_topic(topic, trigger, listings, futures, auto=is_auto)
            notify.send(out["message"], channel=push)
        except ai.AIUnavailable as e:
            log.warning("研究「%s」失敗：%s", topic, e)
            notify.send(f"⚠️ 研究「{topic}」沒有完成：{e}", channel=push)
            if "預算" in str(e) or "ANTHROPIC_API_KEY" in str(e):
                break


def cmd_check(args) -> None:
    """資料檢查：印出各資料來源的原始欄位，不推播。"""
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
    rs.set_defaults(func=cmd_research)
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
