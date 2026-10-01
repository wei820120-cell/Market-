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

from . import config, notify
from .analysis import news_signals, sector_flow, target_price
from .fetchers import news, taifex, tw_daily, tw_realtime, yahoo
from .report import md_table, news_table
from .utils import CACHE_DIR, REPORT_DIR, ensure_dirs, now_tw

log = logging.getLogger("market_intel")


# ---------------------------------------------------------------- 共用

def _listings() -> dict[str, dict]:
    listings = tw_daily.load_listings()
    if not listings:
        log.warning("無法取得上市櫃清單，代號會同時查詢上市與上櫃")
    return listings


def _name_to_code(listings: dict[str, dict]) -> dict[str, str]:
    """新聞比對用的「名稱 → 代號」。2 個字的短名稱容易誤判，只保留自選股與族群成分股。"""
    focus = set(config.tw_watch_codes()) | set(config.all_tw_theme_codes())
    out = {}
    for code, info in listings.items():
        name = (info.get("name") or "").strip()
        if not name or not code.isdigit():
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
    sections.append("\n## 8. 新聞與公告訊號（漲價、缺貨、擴產、財測…）\n")
    sections.append(news_table(ranked, 60))

    path = REPORT_DIR / f"{today:%Y-%m-%d}.md"
    path.write_text("\n".join(sections), encoding="utf-8")
    (REPORT_DIR / "latest.md").write_text("\n".join(sections), encoding="utf-8")
    notify.send(build_summary(today, theme_df, inst_theme, fx, ranked))


def _report_url(today: datetime) -> str:
    """在 GitHub Actions 上執行時，產生報告在 GitHub 上的網址。"""
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        return str(REPORT_DIR / f"{today:%Y-%m-%d}.md")
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    return f"{server}/{repo}/blob/main/reports/{today:%Y-%m-%d}.md"


def build_summary(today: datetime, theme_df: pd.DataFrame, inst_theme: pd.DataFrame, fx: dict,
                  ranked: list, n_news: int = 5) -> str:
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
    if ranked:
        lines.append("\n📰 重點新聞")
        for it in ranked[:n_news]:
            codes = f"[{'、'.join(it.codes[:3])}] " if it.codes else ""
            lines.append(f"・{it.score:+g} {codes}{it.title[:60]}")
    lines.append(f"\n完整報告：{_report_url(today)}")
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


def cmd_realtime(args) -> None:
    ensure_dirs()
    st = (config.settings().get("realtime") or {})
    interval = args.interval or st.get("interval_seconds", 20)
    news_interval = st.get("news_interval_seconds", 60)
    top_n = st.get("top_themes", 12)
    alert_pace = st.get("alert_theme_pace", 2.0)
    alert_pct = st.get("alert_stock_pct", 7.0)

    listings = _listings()
    themes_tw = config.themes().get("tw") or {}
    codes = list(dict.fromkeys(config.all_tw_theme_codes() + config.tw_watch_codes()))
    name_to_code = _name_to_code(listings)
    targets_path = CACHE_DIR / "targets.json"
    targets = json.loads(targets_path.read_text()) if targets_path.exists() else {}
    if not targets:
        log.info("尚無目標價快取，先執行 `python -m market_intel daily` 可啟用目標價/停損警示")

    alerted: set[str] = set()
    last_news = 0.0
    while True:
        now = now_tw()
        if not args.once and not args.force and not _in_session(now):
            log.info("非台股交易時段（09:00–13:30），只監控新聞。")
        else:
            quotes = tw_realtime.fetch_quotes(codes, listings)
            idx = tw_realtime.fetch_indices()
            flow = sector_flow.theme_flow_intraday(quotes, themes_tw, listings, now)
            head = " ｜ ".join(f"{k} {q.price:,.2f} ({q.change_pct:+.2f}%)" for k, q in idx.items()
                               if q.price is not None and q.change_pct is not None)
            print(f"\n===== {now:%H:%M:%S}  {head}  （同時段正常量比例 {sector_flow.expected_fraction(now):.0%}）=====")
            if not flow.empty:
                print(flow.head(top_n).to_string(index=False))
                for r in flow.to_dict("records"):
                    key = f"theme:{r['族群']}:{now:%Y%m%d}"
                    if (r["量能步調"] or 0) >= alert_pace and (r["加權漲跌%"] or 0) > 0 and key not in alerted:
                        alerted.add(key)
                        notify.send(f"🔥 資金湧入【{r['族群']}】量能步調 {r['量能步調']} 倍，加權漲 {r['加權漲跌%']}%，領漲：{r['領漲']}")
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
                        notify.send(msg)

        if time.monotonic() - last_news >= news_interval:
            last_news = time.monotonic()
            fresh = news.only_new(collect_news(full=False))
            for it in news_signals.rank(fresh, config.news_keywords(), name_to_code, _us_symbols(), all_codes=set(listings)):
                notify.send(f"📰 [{it.score:+g}] {'、'.join(it.tags)} {'、'.join(it.codes)}\n{it.title}\n{it.url}")

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


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="market_intel", description="台美股 / 期權 即時情報與目標價")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("daily", help="盤後總報告").set_defaults(func=cmd_daily)
    sub.add_parser("us", help="美股類股資金流向").set_defaults(func=cmd_us)

    rt = sub.add_parser("realtime", help="盤中即時監控")
    rt.add_argument("--once", action="store_true", help="只跑一次")
    rt.add_argument("--force", action="store_true", help="非交易時段也抓報價")
    rt.add_argument("--interval", type=int, help="報價更新秒數")
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
