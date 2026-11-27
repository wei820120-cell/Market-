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
from dataclasses import replace
from pathlib import Path
from datetime import datetime, time as dtime, timedelta

import pandas as pd

from . import ai, config, net, notify, overnight, prefetch, research, signals, theme_card
from .analysis import news_signals, picks, sector_flow, target_price
from .fetchers import macro, news, podcast, ptt, stock_futures, taifex, telegram_channel, threads, tw_daily, tw_realtime, txf, yahoo
from .report import md_table, news_table
from .utils import CACHE_DIR, REPORT_DIR, ensure_dirs, now_tw

log = logging.getLogger("market_intel")


# ---------------------------------------------------------------- 共用

def _listings() -> dict[str, dict]:
    listings = tw_daily.load_listings()
    if not listings:
        log.warning("無法取得上市櫃清單，代號會同時查詢上市與上櫃")
    return listings


FUTURES_CHANNEL = "futures"   # 微台（台指期）訊號、收盤彙總、績效推播的頻道；沒設定專屬機器人時 notify 會退回進出場機器人


# 公司簡稱同時是常用詞，用名稱比對會誤判（仍可用代號比對，例如「全新(2455)」）
AMBIGUOUS_NAMES = {"全新", "大同", "統一", "中華", "聯合", "新光", "大眾", "正新", "台灣", "國產", "如興", "精星", "世界", "數字", "互動", "大量", "新天地", "巨大", "大中"}


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
    weak_md = ""
    try:  # 大盤大跌的日子：多列一節「逆勢榜」（只列名單供觀察，不影響上面的分數）
        weak_md = weak_day_markdown(day, _index_pct(), hist, inst, closes, themes_tw)
    except Exception as e:  # noqa: BLE001
        log.warning("大跌日逆勢榜產生失敗：%s", e)
    sections[2:2] = [
        "## 0. 今日強勢標的（資金流入＋題材＋漲價，附股票期貨／小型股票期貨）\n",
        "_分數＝量比×2（上限 10）＋法人買超每 5 億 1 分＋新聞題材分數＋漲價 4 分＋所屬族群資金流入 2 分；當天下跌打五折。"
        "股票期貨欄：CDF＝一般股票期貨代碼、小型＝小型股票期貨、夜盤＝有盤後交易時段。_\n\n",
        md_table(picks_df, 20) + "\n",
    ] + ([weak_md] if weak_md else [])

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


def _index_pct() -> float | None:
    """加權指數最近一個交易日的漲跌幅（%），抓不到回傳 None。"""
    try:
        index_df, _ = yahoo.fetch_chart("^TWII", range_="1mo")
    except Exception as e:  # noqa: BLE001
        log.warning("加權指數漲跌幅抓取失敗：%s", e)
        return None
    if index_df is None or len(index_df) < 2:
        return None
    return float(index_df["close"].iloc[-1] / index_df["close"].iloc[-2] - 1) * 100


def weak_day_markdown(day: pd.DataFrame, idx_pct, hist: dict, inst: pd.DataFrame, closes: dict, themes_tw: dict) -> str:
    """盤後報告的「大跌日逆勢榜」；加權指數跌幅沒達門檻（或抓不到指數）回傳空字串。只列名單，不影響強勢標的分數。"""
    if not picks.is_weak_day(idx_pct):
        return ""
    prev_pct: dict[str, float] = {}
    ratio: dict[str, float] = {}
    for c, h in (hist or {}).items():
        if h is None or len(h) < 3:
            continue
        prev_pct[c] = float(h["close"].iloc[-2] / h["close"].iloc[-3] - 1) * 100
        if len(h) >= 6:
            v = h["close"] * h["volume"]
            base = float(v.iloc[-6:-1].mean())
            if base:
                ratio[c] = float(v.iloc[-1]) / base
    inst_map: dict[str, float] = {}
    if inst is not None and not inst.empty:
        for r in inst.itertuples():
            px = closes.get(r.code)
            if px and r.foreign is not None and r.trust is not None:
                inst_map[r.code] = (r.foreign + r.trust) * px / 1e8
    df = picks.weak_day_leaders(day, idx_pct, prev_pct, ratio, inst_map, tw_daily.load_revenue(), tw_daily.load_alerts(),
                                picks.themes_of(themes_tw))
    return (f"\n## 0b. 大跌日逆勢榜（加權指數 {idx_pct:+.2f}%）\n\n"
            "_只在加權指數跌幅 ≥ 2% 的日子出現。名單＝收紅，或跌幅不到大盤一半、成交值 ≥ 3 億的一般股；"
            "**只供觀察，不影響上面強勢標的的分數**（要不要加分，先回測）。_\n\n"
            "_類型：營收有成長＝最新月營收年增 ≥ 20%（獲利兌現型候選）；漲停・營收沒跟上＝多半是題材或內需輪動，持續性要打折。"
            "備註：守住前日漲幅＝前一天漲 5% 以上、今天沒回吐；⚠＝處置／注意股（分盤撮合，進出要小心）。不是買進建議。_\n\n"
            + md_table(df, 30) + "\n")


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


GROUP_SURGE_DEFAULTS = {"enabled": True, "start": "09:05", "pct": 5.0, "min_stocks": 3, "min_share": 0.25, "max_per_day": 8}


def _group_surge_setup() -> dict | None:
    """族群拉升快訊（analysis/group_surge.py）與事件族群進場候選（trade/event_group.py）的盤中狀態。"""
    conf = {**GROUP_SURGE_DEFAULTS, **((config.settings().get("realtime") or {}).get("group_surge") or {})}
    if not conf["enabled"]:
        return None
    return {"conf": conf, "covered": set(), "themes": set(), "sent_today": 0}


def _group_surge_check(gs: dict, quotes: dict, themes_tw: dict, listings: dict, futures: dict, trade_rt: dict | None,
                       now: datetime, idx: dict | None = None) -> None:
    """同一族群 ≥3 檔同時漲 ≥5% → 選股機器人整組推一則（漲幅、鎖死漲停、股期、2% 規則）；
    有研究或今天的重大事件撐腰、而且有進出場機器人時，再推『事件族群』進場候選。每個族群每級每天各一次（跨重啟用 cards_sent 去重）。"""
    from .analysis import group_surge
    from .trade import event_group
    conf = gs["conf"]
    if f"{now:%H:%M}" < conf["start"]:
        return
    groups = group_surge.detect(quotes, themes_tw, pct=conf["pct"], min_n=conf["min_stocks"], min_share=conf["min_share"])
    gs["covered"] |= group_surge.covered_codes(groups)
    gs["themes"] |= {g["theme"] for g in groups}
    taiex = (idx or {}).get("加權指數")
    market = (f"大盤：加權 {taiex.price:,.0f}（{taiex.change_pct:+.2f}%）"
              if taiex and taiex.price is not None and taiex.change_pct is not None else "")
    today = f"{now:%Y-%m-%d}"
    try:
        major_state = json.loads(MAJOR_PATH.read_text(encoding="utf-8")) if MAJOR_PATH.exists() else {}
    except (ValueError, OSError):
        major_state = {}
    events_cfg = config.major_events().get("events") or {}
    for g in groups:
        key = f"grpsurge:{g['theme']}:{g['level']}"
        if theme_card.already_sent(key) or gs["sent_today"] >= int(conf["max_per_day"]):
            continue
        theme_card.mark_sent(key)       # 先記再推：寧可少一則，不要重複洗版
        gs["sent_today"] += 1
        codes = [str(c) for c in themes_tw.get(g["theme"], [])]
        topic = research.topic_for_theme(g["theme"], codes)
        topic_date = (research.load_index().get(topic) or {}).get("date", "") if topic else ""
        events = event_group.events_for_theme(g["theme"], major_state, events_cfg, today)
        if topic:
            research_line = f"📚 研究：《{topic}》{topic_date}"
        elif not g["theme"].startswith("研究:") and research.queue_research(
                g["theme"], f"{today} 族群拉升：{g['n_up']}/{g['n']} 檔漲 ≥{conf['pct']:g}%｜成分股：{'、'.join(codes)}"):
            research_line = "📚 這組還沒有研究，已排入研究"
        else:
            research_line = "📚 這組還沒有研究"
        if events:
            research_line += "\n🌐 今天的重大事件：" + "、".join(events)
        fut_labels = {m["code"]: stock_futures.label(futures.get(m["code"])) if futures else "未知" for m in g["members"]}
        cands, sizing = [], {}
        if trade_rt:
            cands = event_group.candidates(g, futures, trade_rt["levels"], trade_rt["equity"], trade_rt["rates"])
            sizing = {r["code"]: event_group.size_text(r) for r in cands if r["tier"] == event_group.TIER_CHASE}
        notify.send(group_surge.message(g, now, fut_labels, sizing, research_line, market), channel="picks")
        proof = event_group.backing(topic, topic_date, events)
        if trade_rt and proof:
            notify.send(event_group.message(g, cands, proof, trade_rt["market_ok"], now), channel="trade")


VOLPRICE_SEEN = research.ROOT / "state" / "volprice_seen.json"
VOLPRICE_NOTE = ("回測（近 2 年、343 檔）：這類股票之後 10 日內出現放量突破的機率約 41%（沒放量約 27%），進場價比突破日低約 6%；"
                 "但勝率只有 42%、平均 R≈0，同題材有人放量上漲也沒有加分。所以是『比突破更早盯上』的觀察名單，不是進場訊號。")


def _upto_yesterday(df: pd.DataFrame) -> pd.DataFrame:
    today = f"{now_tw():%Y-%m-%d}"
    idx = df.index.tz_convert("Asia/Taipei") if df.index.tz is not None else df.index
    return df[[f"{d:%Y-%m-%d}" < today for d in idx]]


def _volprice_setup(listings: dict, themes_tw: dict) -> dict | None:
    """題材群量增價未漲預警的準備：題材成分股＋近期研究過的題材整群，到昨天為止的 20 日高、60 日線、均量。"""
    conf = (config.settings().get("realtime") or {}).get("volprice_watch") or {}
    if not conf.get("enabled", True):
        return None
    from .trade import intraday
    groups = {k: [str(c) for c in v] for k, v in themes_tw.items()}
    cutoff = (now_tw() - timedelta(days=int(conf.get("study_days", 30)))).strftime("%Y-%m-%d")
    for name, meta in research.load_index().items():
        cs = [str(c) for c in meta.get("codes", [])]
        if meta.get("date", "") >= cutoff and len(cs) >= 3:
            groups.setdefault(f"研究:{name}", cs)
    codes = sorted({c for v in groups.values() for c in v if c in listings and not c.startswith("00")})
    hist = {c: _upto_yesterday(df) for c, df in _yahoo_histories(codes, listings, range_="6mo").items()}
    lv = intraday.levels(hist)
    log.info("量增價未漲預警：監控 %d 檔（%d 個題材）", len(lv), len(groups))
    seen = {}
    if VOLPRICE_SEEN.exists():
        try:
            seen = json.loads(VOLPRICE_SEEN.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            seen = {}
    return {"levels": lv, "groups": groups, "conf": conf, "seen": seen, "today": 0, "queued": 0}


def _volprice_check(vp: dict, quotes: dict, listings: dict, now: datetime) -> None:
    from .trade import volprice
    conf = vp["conf"]
    if f"{now:%H:%M}" < str(conf.get("start", "10:00")) or vp["today"] >= int(conf.get("max_per_day", 6)):
        return
    cands = volprice.watch_candidates(quotes, vp["levels"], vp["groups"], sector_flow.expected_fraction(now),
                                      min_pace=float(conf.get("min_pace", 3.0)), max_pct=float(conf.get("max_pct", 3.0)),
                                      min_pct=float(conf.get("min_pct", -1.0)))
    today = f"{now:%Y-%m-%d}"
    repeat = timedelta(days=int(conf.get("repeat_days", 3)))
    fresh = []
    for s in cands:
        last = vp["seen"].get(s["code"])
        if last and (datetime.strptime(today, "%Y-%m-%d") - datetime.strptime(last, "%Y-%m-%d")) < repeat:
            continue
        fresh.append(s)
    fresh = fresh[: int(conf.get("max_per_day", 6)) - vp["today"]]
    if not fresh:
        return
    lines = []
    for s in fresh:
        vp["seen"][s["code"]] = today
        vp["today"] += 1
        themes = "、".join(t.replace("研究:", "")[:14] for t in s["themes"][:2])
        studied = research.find_for_codes([s["code"]], min_overlap=1)
        status = f"已有研究：{studied}" if studied else "還沒研究過"
        if not studied and conf.get("queue_research", True) and vp["queued"] < int(conf.get("queue_max_per_day", 3)):
            if research.queue_research(f"{s['name']}（{s['code']}）題材與產業",
                                       f"{today} 量增價未漲（觀察）：{s['code']} {s['name']}｜量能步調 {s['pace']:.1f} 倍、漲幅 {s['pct']:+.1f}%｜題材：{themes}",
                                       priority=2):
                vp["queued"] += 1
                status = "還沒研究過，已排入研究"
        lines.append(f"・{s['code']} {s['name']}〔{themes}〕 {s['price']:,.2f}（{s['pct']:+.1f}%）量能步調 {s['pace']:.1f} 倍｜"
                     f"離 20 日高還差 {s['gap'] * 100:.1f}%｜{status}")
    VOLPRICE_SEEN.parent.mkdir(parents=True, exist_ok=True)
    cut = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=30)).strftime("%Y-%m-%d")
    VOLPRICE_SEEN.write_text(json.dumps({k: v for k, v in vp["seen"].items() if v >= cut}, ensure_ascii=False), encoding="utf-8")
    notify.send(f"👀 {now:%H:%M} 題材群量增價未漲（觀察，不是進場訊號）\n\n" + "\n".join(lines) + "\n\n" + VOLPRICE_NOTE, channel="research")


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
    vp = None
    try:
        vp = _volprice_setup(listings, themes_tw)
    except Exception as e:  # noqa: BLE001
        log.warning("量增價未漲預警準備失敗：%s", e)
    if vp:
        codes = list(dict.fromkeys(codes + list(vp["levels"])))
    fd = None
    try:
        fd = _futures_day_setup()
    except Exception as e:  # noqa: BLE001
        log.warning("台指期日內訊號準備失敗：%s", e)
    gs = None
    try:
        gs = _group_surge_setup()
    except Exception as e:  # noqa: BLE001
        log.warning("族群拉升快訊準備失敗：%s", e)
    news_map: dict[str, dict] = {}  # 今天看過的新聞依個股彙總（題材、漲價）
    alerted: set[str] = set()
    pushed_times: set[str] = set()
    env_done = False
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
            if vp and _in_session(now):
                try:
                    _volprice_check(vp, quotes, listings, now)
                except Exception as e:  # noqa: BLE001
                    log.warning("量增價未漲預警失敗：%s", e)
            if fd and _in_session(now):
                try:
                    _futures_day_check(fd, now)
                except Exception as e:  # noqa: BLE001
                    log.warning("台指期日內訊號失敗：%s", e)
            if gs and _in_session(now):
                try:
                    _group_surge_check(gs, quotes, themes_tw, listings, futures, trade_rt, now, idx)
                except Exception as e:  # noqa: BLE001
                    log.warning("族群拉升快訊失敗：%s", e)
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
                    env_line = ""
                    if not env_done:  # 隔夜環境一天只放在選股機器人第一則（約 09:15）
                        env_done = True
                        env_line = _overnight_line(idx)
                    if not picks_df.empty:
                        body = picks.picks_message(picks_df, f"🎯 {now:%H:%M} 強勢標的")
                        notify.send((env_line + "\n\n" if env_line else "") + body, channel="picks")
                    elif env_line and "🟢" not in env_line:  # 沒有強勢標的時，只有黃燈／紅燈才單獨提醒
                        notify.send(env_line, channel="picks")
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
                if surged and gs:   # 拉升的股票已由族群拉升快訊整組推過 → 重疊過半的小族群（多半是研究自動產生的）不再各推一張卡
                    from .analysis import group_surge
                    surged = [t for t in surged if t in gs["themes"] or not group_surge.is_covered(themes_tw.get(t, []), gs["covered"])]
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
    if fd:
        try:
            _futures_day_close(fd)
        except Exception as e:  # noqa: BLE001
            log.warning("台指期日內收盤記錄失敗：%s", e)


def _futures_signal_text(sigs: list[dict], st: dict, cfg: dict, session: str) -> str:
    """微台訊號推播全文。累計損益（本場、本月、今年）只在全數出場時附；進場、加碼、分批出場、盤面通知都不附。"""
    from .trade import futures_day, futures_log
    pnl = futures_log.pnl_line(st, cfg, session) if futures_day.is_full_exit(sigs, st) else ""
    return futures_day.format_message(sigs, st, cfg, pnl)


def _futures_day_setup() -> dict | None:
    """台指期日內順勢突破的訊號（trade/futures_day.py）：設定、今天已記的狀態、前幾天的日收盤。"""
    from .trade import futures_day
    cfg = futures_day.settings()
    if not cfg["enabled"]:
        return None
    day = f"{now_tw():%Y-%m-%d}"
    return {"cfg": cfg, "st": futures_day.load_state(day), "closes": futures_day.load_closes(day)}


def _futures_day_close(fd: dict) -> None:
    """日盤收盤：最後一次補發之前推播失敗的訊號、記日收盤、推計畫帳彙總（附推播狀況）、月初結算。"""
    from .trade import futures_day, futures_log
    st, cfg = fd["st"], fd["cfg"]
    futures_day.outbox_flush(st, _futures_send)          # 收盤前最後一次補發之前推播失敗的訊號
    summary = futures_day.finish_day(st, cfg)
    note = futures_day.delivery_note(st)
    stamp = now_tw().isoformat(timespec="seconds")
    if summary:
        futures_log.safe(futures_log.refresh, st, cfg, "day")
        # 累計損益只在「全數出場」那則附一次；收盤時已經空手就不再重複，還有未平倉（沒有全出訊號）才附
        pnl = futures_log.pnl_line(st, cfg, "day") if st["lots"] else ""
        futures_day.outbox_push(st, f"📈 台指期日內計畫帳收盤\n{summary}" + (f"\n{pnl}" if pnl else "") + (f"\n{note}" if note else ""), stamp, _futures_send)
    elif note:          # 沒跑到收盤附近、沒有彙總，但有訊號曾推播失敗：至少講一聲
        futures_day.outbox_push(st, f"📈 台指期日內訊號推播狀況\n{note}", stamp, _futures_send)
    futures_day.save_state(st)                           # 收盤彙總若也沒送出，排隊狀態留在存檔裡
    _futures_settle()          # 當天沒進場也檢查：月初第一個收盤就把上個月結算掉


def _futures_send(text: str) -> bool:
    """微台頻道推播，回傳有沒有送到（notify.send 已重試過；舊版或測試替身沒回傳值就當作送到）。
    推播丟出任何例外都當作失敗（排隊補發），不能讓例外跳過後面的計畫帳紀錄與存檔。"""
    try:
        return notify.send(text, channel=FUTURES_CHANNEL) is not False
    except Exception as e:  # noqa: BLE001
        log.warning("微台推播丟出例外（當作送失敗、排隊補發）：%s", type(e).__name__)
        return False


def _futures_day_check(fd: dict, now: datetime) -> None:
    """日盤：抓一筆報價、餵給訊號規則。訊號推播失敗會排進 st["outbox"] 補發（報價中斷時也照樣補發）；日盤報價中斷超過 20 分鐘提醒一次。"""
    from .trade import futures_day, futures_log
    st = fd["st"]
    n_before = (len(st.get("outbox") or []), st.get("resent", 0))
    futures_day.outbox_flush(st, _futures_send)          # 之前推播失敗的訊號先補發（順序不變）
    try:
        q = txf.fetch()
    except Exception as e:  # noqa: BLE001
        log.warning("日盤報價抓取失敗：%s", e)
        q = None
    ts = q and q["ts"]
    if not q or not ts or ts.date() != now.date() or (now - ts).total_seconds() > 180:
        # 沒資料、或報價時間太舊（休市、資料沒更新）：不拿舊價發訊號；中斷超過 20 分鐘要講一聲，持倉沒人看管更要講
        note = futures_day.stale_notice(st, now, "日盤")
        if note:
            futures_day.outbox_push(st, note, now.isoformat(timespec="seconds"), _futures_send)
        if note or n_before != (len(st.get("outbox") or []), st.get("resent", 0)):
            futures_day.save_state(st)
        return
    if st.get("stale_alerted"):
        st["stale_alerted"] = False
        last = st["ticks"][-1][0][11:16] if st["ticks"] else ""
        futures_day.outbox_push(st, f"✅ 日盤報價恢復（中斷前最後一筆在 {last}），訊號判斷已接續。", now.isoformat(timespec="seconds"), _futures_send)
    sigs = futures_day.step(st, now, q["price"], fd["cfg"], fd["closes"], q["high"], q["low"])
    if sigs:
        text = _futures_signal_text(sigs, st, fd["cfg"], "day")
        futures_log.safe(futures_log.record, st, sigs, fd["cfg"], "day")      # 先記計畫帳（它才是事實），再推播；推播失敗只會排隊補發
        futures_day.outbox_push(st, text, sigs[0]["ts"], _futures_send)
    shock = futures_day.shock_alert(st, fd["cfg"])        # 急拉急殺快訊：只看價格、不看新聞（不是進出場訊號，不進計畫帳）
    if shock:
        futures_day.outbox_push(st, shock, now.isoformat(timespec="seconds"), _futures_send)
    futures_day.save_state(st)


def cmd_futures_night(args) -> None:
    """台指期夜盤（15:00–隔天 05:00）日內順勢突破訊號：和日盤同一套規則（trade/futures_day.py），只發訊號、不下單。

    一次最多跑 --minutes 分鐘（GitHub 單次執行有時間上限，realtime-night.yml 分段接續）；狀態存在 state/futures_day.json 的 night。
    結束碼：0＝這一段跑完、這一場還沒結束（要接續）；10＝這一場已結束、或今晚沒有夜盤（不用接續）。
    --status 只印 run／skip（要不要啟動夜盤監控），--no-push 不推播、改印在畫面上（測試用）。
    """
    from .trade import futures_day, futures_log
    ensure_dirs()
    cfg = futures_day.settings("night")
    now = now_tw()
    sid, start, end = futures_day.night_session(now)
    st = futures_day.load_state(sid, session="night")
    closes = futures_day.load_closes(sid)
    over = end + timedelta(minutes=2)
    if args.status:
        live = cfg["enabled"] and not st.get("finished") and now < over and start.weekday() < 5
        print("run" if live or (args.force and cfg["enabled"]) else "skip")
        return
    if not cfg["enabled"]:
        print("夜盤訊號已停用（settings.yaml 的 futures_night.enabled）")
        raise SystemExit(10)
    if not args.force and (st.get("finished") or now >= over or start.weekday() >= 5):
        print(f"{sid} 這一場已結束或沒有夜盤，不啟動。")
        raise SystemExit(10)

    def send(msg: str) -> bool:
        if args.no_push:
            print(msg)
            return True
        return _futures_send(msg)

    interval = args.interval or 20
    deadline = time.monotonic() + args.minutes * 60 if args.minutes else None
    rc = 0
    while True:
        now = now_tw()
        if st.get("outbox"):
            futures_day.outbox_flush(st, send)          # 之前推播失敗的夜盤訊號先補發（順序不變）；報價中斷時也照樣補發
            futures_day.save_state(st, session="night")
        if not args.force and now >= over:
            rc = 10
            break
        if not args.force and now < start:
            if start - now > timedelta(minutes=30):
                rc = 10
                break
            time.sleep(min(interval, 20))
            continue
        try:
            q = txf.fetch(night=True)
        except Exception as e:  # noqa: BLE001
            log.warning("夜盤報價抓取失敗：%s", e)
            q = None
        ts = futures_day.night_quote_ts(q and q["ts"], start)       # 過了午夜 MIS 的日期欄位不換日，要補一天
        fresh = bool(q and ts and abs((now - ts).total_seconds()) <= 180
                     and start - timedelta(minutes=5) <= ts <= end + timedelta(minutes=5))      # 沒資料、或報價太舊：不拿舊價發訊號
        if args.no_push:       # 測試模式：把這一筆報價印出來，方便確認欄位與時間
            print(f"{now:%H:%M:%S} 夜盤報價 {q and q['symbol']} {q and q['price']}（本場高 {q and q['high']}、低 {q and q['low']}、報價時間 {ts and f'{ts:%m/%d %H:%M:%S}'}）"
                  f"{'有效' if fresh else '過期或無資料，略過'}")
        if fresh:
            if st.get("stale_alerted"):
                last_hm = st["ticks"][-1][0][11:16] if st["ticks"] else ""
                futures_day.outbox_push(st, f"✅ 夜盤報價恢復（中斷前最後一筆在 {last_hm}），訊號判斷已接續。", now.isoformat(timespec="seconds"), send)
            sigs = futures_day.step(st, now, q["price"], cfg, closes, q["high"], q["low"])
            if sigs:
                text = _futures_signal_text(sigs, st, cfg, "night")
                futures_log.safe(futures_log.record, st, sigs, cfg, "night")      # 先記計畫帳，再推播
                futures_day.outbox_push(st, text, sigs[0]["ts"], send)
            shock = futures_day.shock_alert(st, cfg)        # 急拉急殺快訊：只看價格、不看新聞（不是進出場訊號，不進計畫帳）
            if shock:
                futures_day.outbox_push(st, shock, now.isoformat(timespec="seconds"), send)
            st["stale_alerted"] = False
            futures_day.save_state(st, session="night")
        elif not args.force and now < end and (note := futures_day.stale_notice(st, now, "夜盤")):
            # 夜盤進行中卻超過 20 分鐘沒有有效報價：不要靜悄悄（2026-11-13 午夜報價被擋了一個多小時沒人知道），發一次提醒；有持倉要講停損沒人看
            futures_day.outbox_push(st, note, now.isoformat(timespec="seconds"), send)
            futures_day.save_state(st, session="night")
        elif not st["ticks"] and not args.force and now >= start + timedelta(minutes=45):
            print("夜盤開盤 45 分鐘仍沒有報價：今晚沒有夜盤（休市日）。")
            st["finished"] = True
            futures_day.save_state(st, session="night")
            rc = 10
            break
        if args.once:
            break
        if deadline and time.monotonic() >= deadline:
            break
        time.sleep(interval)
    if rc == 10 and not st.get("finished"):
        futures_day.outbox_flush(st, send)          # 收盤前最後一次補發之前推播失敗的訊號
        summary = futures_day.finish_night(st, cfg)
        futures_log.safe(futures_log.refresh, st, cfg, "night")
        note = futures_day.delivery_note(st)
        stamp = now_tw().isoformat(timespec="seconds")
        if summary:
            pnl = futures_log.pnl_line(st, cfg, "night") if st["lots"] else ""      # 已空手＝全數出場時附過了，不重複
            futures_day.outbox_push(st, f"🌙 台指期夜盤計畫帳收盤\n{summary}" + (f"\n{pnl}" if pnl else "") + (f"\n{note}" if note else ""), stamp, send)
        elif note:
            futures_day.outbox_push(st, f"🌙 台指期夜盤訊號推播狀況\n{note}", stamp, send)
        futures_day.save_state(st, session="night")
        if not args.no_push:
            _futures_settle()
    if rc:
        raise SystemExit(rc)


def cmd_research_scoreboard(args) -> None:
    """研究成績單：每篇研究點名的個股，研究日之後相對加權指數的表現（依研究類型、方向標註、狀態分組）。

    寫到 research/scoreboard.md 並印出來。價格用 Yahoo 日 K（近 6 個月）；研究都是近幾週的，所以窗口很短，只是初步線索。
    """
    from .analysis import research_scoreboard as sb
    from .fetchers import yahoo
    studies = sb.load_studies(research.ROOT)
    listings = _listings()
    codes = sorted({c for s in studies for c in s["picks"]})
    syms = {c: yahoo.yahoo_symbol(c, (listings.get(c) or {}).get("market") or "tse") for c in codes}
    hist = yahoo.fetch_many(list(syms.values()), range_="6mo")
    prices = {c: hist[s]["close"] for c, s in syms.items() if s in hist}
    bench_df = yahoo.fetch_many([sb.BENCH], range_="6mo").get(sb.BENCH)
    bench = bench_df["close"] if bench_df is not None else None
    if bench is None:
        log.warning("抓不到加權指數，超額報酬無法計算")
    control = None
    if args.control > 0:      # 對照組：沒被研究點名的上市櫃股票固定隨機樣本，等權重
        ctl_codes = sb.pick_control(listings, set(codes), n=args.control)
        ctl_syms = {c: yahoo.yahoo_symbol(c, (listings.get(c) or {}).get("market") or "tse") for c in ctl_codes}
        ctl_hist = yahoo.fetch_many(list(ctl_syms.values()), range_="6mo")
        control = {c: ctl_hist[s]["close"] for c, s in ctl_syms.items() if s in ctl_hist}
        print(f"對照組有價格 {len(control)}／{len(ctl_codes)} 檔")
    rows = sb.build_rows(studies, prices, bench, control=control)
    md = sb.render(rows, studies, now_tw().replace(tzinfo=None), control_n=len(control or {}))
    out = Path(args.out) if args.out else research.ROOT / "research" / "scoreboard.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    print(f"個股有價格 {len(prices)}／{len(codes)} 檔、{len(rows)} 列；已寫入 {out}")
    print(md)


def cmd_health(args) -> None:
    """系統健康日報：各排程昨天有沒有跑／失敗、資料是否過期、夜盤報價有沒有中斷；每天推一則（需要 gh 與 GH_TOKEN，在 GitHub Actions 上跑）。"""
    from . import health
    now = now_tw()
    text = health.render(health.collect(research.ROOT, now), now)
    print(text)
    if not args.dry:
        notify.send(text, channel=args.channel)


def cmd_market_stress(args) -> None:
    """市場流動性／恐慌指標（跌停家數、漲停家數、上漲／下跌家數、成交值相對 20 日均）：更新 state/market_stress.json 並印出。"""
    from .analysis import market_stress as ms
    df = tw_daily.fetch_day_all()
    if df.empty:
        print("抓不到全市場日行情")
        return
    day = args.day or df.attrs.get("date")        # 行情資料自己帶的日期最準；沒有才用加權指數最近一個交易日
    if not day:
        index_df, _ = yahoo.fetch_chart("^TWII", range_="1mo")
        day = f"{index_df.index[-1].tz_convert('Asia/Taipei'):%Y-%m-%d}" if not index_df.empty else f"{now_tw():%Y-%m-%d}"
    r = ms.record(day, ms.day_stats(df))
    print(f"{day}：" + ms.text(r))


def cmd_panic_watch(args) -> None:
    """恐慌日研究清單：研究過、標註受惠、現價比研究時更低的個股，附研究的催化與風險（寫 research/panic_watch.md）。不是買進建議。"""
    from .analysis import market_stress as ms
    from .analysis import panic_watch as pw
    from .analysis import research_scoreboard as sb
    from .fetchers import yahoo
    studies = sb.load_studies(research.ROOT)
    listings = _listings()
    codes = sorted({c for s in studies for c, imp in s["picks"].items() if imp == "受惠"})
    syms = {c: yahoo.yahoo_symbol(c, (listings.get(c) or {}).get("market") or "tse") for c in codes}
    hist = yahoo.fetch_many(list(syms.values()), range_="6mo")
    prices = {c: hist[s]["close"] for c, s in syms.items() if s in hist}
    details = pw.load_details(research.ROOT, sorted({s["topic"] for s in studies}))
    names = {c: (v.get("name") or "") for c, v in listings.items()}
    revenue = tw_daily.load_revenue()
    rows = pw.candidates(studies, prices, details, names, drop_pct=args.drop, revenue=revenue, min_yoy=args.min_yoy)
    total = len(rows)
    rows = rows[:args.top]
    days = ms.load()
    level = days[-1]["level"] if days else None
    md = pw.render(rows, level, f"{now_tw():%Y-%m-%d}", args.drop, total=total, min_yoy=args.min_yoy)
    out = Path(args.out) if args.out else research.ROOT / "research" / "panic_watch.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md, encoding="utf-8")
    print(f"受惠標的有價格 {len(prices)}／{len(codes)} 檔，符合 {len(rows)} 檔；已寫入 {out}")
    print(md)


def cmd_futures_log(args) -> None:
    """印出微台計畫帳觸發紀錄（state/futures_log.md；有 --rebuild 就先用 state/futures_log.json 重排一次）。"""
    from .trade import futures_log
    if args.rebuild:
        futures_log.save(futures_log.load())
    print(futures_log.render_md(futures_log.load()))


def _futures_settle() -> None:
    """每月結算：上個月（含）以前還沒結算的整月，推一則月結算並記進 state/futures_log.json 的 settled（只推一次）。
    在日盤收盤、夜盤收盤時檢查；失敗只留警告，不影響收盤推播。"""
    from .trade import futures_log
    try:
        data = futures_log.load()
        texts = futures_log.settle(data, now_tw())
        if not texts:
            return
        futures_log.save(data)          # 先記下已結算，再推：寧可偶爾少一則，也不要每場重複推
        for t in texts:
            notify.send(t, channel=FUTURES_CHANNEL)
    except Exception as e:  # noqa: BLE001
        log.warning("微台月結算失敗：%s", e)


def cmd_futures_report(args) -> None:
    """微台計畫帳績效推播：累計損益、勝率、最近幾場、進行中的場次（微台機器人）。--dry 只印不推。
    --view month／year：改推每月結算、年度績效表。"""
    from .trade import futures_log
    view = getattr(args, "view", "perf")
    if view == "month":
        text = futures_log.monthly_text(futures_log.load(), now_tw())
    elif view == "year":
        text = futures_log.yearly_text(futures_log.load(), now_tw())
    else:
        text = futures_log.performance_text(futures_log.load(), now_tw(), _futures_live(), last_n=args.last)
    print(text)
    if not args.dry:
        token, chat = notify._telegram(FUTURES_CHANNEL)          # 沒設定金鑰時 notify.send 只會印出、不會報錯：這裡明講，免得以為推出去了
        print(f"[推播] 微台頻道 Telegram：{'已設定，送出' if token and chat else '沒有金鑰或 Chat ID，只印在日誌、不會送出'}")
        print(f"[金鑰檢查] {notify.token_hint(FUTURES_CHANNEL)}")        # 只印長度、格式、機器人編號，不印金鑰本身
        notify.send(text, channel=FUTURES_CHANNEL)


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
    # 盤勢快訊：鉅亨網〈美股盤後〉〈台股盤後〉這類大盤綜述標題沒有個股利多利空字詞、分數是 0，上面不會推，這裡依標題格式補推
    wrap_spec = config.news_keywords().get("market_wrap") or {}
    wraps = [it for it in fresh if news_signals.is_market_wrap(it, wrap_spec) and it not in letters and it not in hits[:5]]
    for it in wraps[:int(wrap_spec.get("max_per_round", 3))]:
        notify.send(f"📊 盤勢快訊\n{it.title}\n{it.url}", channel="news")
    try:  # 媒體標題裡的供應鏈換料、轉單、淘汰（現有詞表抓不到）
        signals.process([signals.Item(it.source, f"{it.title}\n{it.summary}", it.url, "媒體") for it in fresh
                         if not it.source.startswith("MOPS")], listings, signals.signal_names(listings, AMBIGUOUS_NAMES),
                        skip_patterns=config.sources().get("skip_patterns") or [])
    except Exception as e:  # noqa: BLE001
        log.warning("早期訊號偵測失敗：%s", e)
    try:
        supply_shocks(fresh, listings, futures)
    except Exception as e:  # noqa: BLE001
        log.warning("供需失衡偵測失敗：%s", e)
    try:
        major_events(fresh, listings)
    except Exception as e:  # noqa: BLE001
        log.warning("重大事件偵測失敗：%s", e)
    for it in fresh:  # 新聞點名多檔個股的供應鏈名單：全部先排進研究
        if it.source.startswith("MOPS") or len(it.codes) < 2:
            continue
        try:
            prefetch.from_news(it.title, it.codes, listings, it.url)
        except Exception as e:  # noqa: BLE001
            log.warning("新聞連動預研失敗：%s", e)
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
    cap = int(conf.get("max_per_day", 4))
    over_key = f"{today}|over"
    over_n = int(st.get(over_key, 0))
    lines, queued, over_lines = [], [], []
    for it in items:
        text = f"{it.title} {it.summary}"
        if any(w in it.title for w in exclude) or it.score < 0 and "減產" not in text:
            continue
        if any(w in it.title for w in conf.get("skip_title_words") or []):
            continue  # 已被駁斥、澄清的傳聞不是供需事件
        cause = shock_cause(text, conf.get("causes") or {})
        if not cause:
            continue
        topic = shock_topic(it, listings)
        need = set(conf.get("need_relevance") or [])
        if (("all" in need or cause in need) and not it.codes and topic.startswith("供需事件：")
                and not re.search(conf.get("relevance_words") or "(?!)", text, flags=re.I)):
            continue  # 對不到台股標的、產品題材或關鍵原料的新聞（國際政治、民生、地震…）不研究、不推
        trigger = f"{today} 供需失衡（{cause}）：{it.title}\n{it.summary[:300]}\n{it.url}"
        if count >= cap:
            # 今日研究額度用完：不排研究，但不能靜悄悄丟掉（11/16、17、18、20、21、23 都在傍晚前用完額度，之後的事件整天沒人知道）。
            # 仍是新事件（沒研究過、不在佇列）就通知一則，每天最多 max_overflow_per_day 則，讓使用者可以回「研究 XXX」。
            if over_n < int(conf.get("max_overflow_per_day", 5)) and research.queue_research(topic, trigger, cause=cause, dry_run=True):
                over_n += 1
                over_lines.append(f"・【{cause}】{it.title[:80]}\n  → 題材：{topic}" + (f"\n  {it.url}" if it.url and "mops" not in it.url else ""))
            continue
        if not research.queue_research(topic, trigger, cause=cause):
            continue
        count += 1
        queued.append(topic)
        stocks = "、".join(f"{c} {(listings.get(c) or {}).get('name', '')}｜{theme_card.fut_text((futures or {}).get(c), known=bool(futures))}"
                          for c in it.codes[:3] if c[:1].isdigit())
        lines.append(f"・【{cause}】{it.title[:80]}" + (f"\n  相關：{stocks}" if stocks else "")
                     + f"\n  → 研究題材：{topic}" + (f"\n  {it.url}" if it.url and "mops" not in it.url else ""))
    if lines:
        notify.send("🧭 供需失衡訊號（已排入研究，研究完成會推研究圖表）\n\n" + "\n\n".join(lines), channel="research")
    if over_lines:
        notify.send(f"🧭 供需失衡訊號（今天 {cap} 個研究額度已滿，只通知、沒有排入研究；想研究請回「研究 題材名稱」）\n\n" + "\n\n".join(over_lines), channel="research")
    if lines or over_lines:
        st = {today: count, over_key: over_n}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    return queued


MAJOR_PATH = research.ROOT / "state" / "major_events.json"


def major_events(items: list, listings: dict, macro_rows: list | None = None) -> list[str]:
    """重大總經／地緣事件（config/major_events.yaml）：同一事件一天推一次到新聞機器人，並排入研究佇列。

    只推清單上的事件；族群的方向是初步推論，研究完成才算數。
    macro_rows：國際行情快照（預設讀 state/macro_snapshot.json，太舊就不附），事件相關資產的即時漲跌附在訊息與研究觸發裡。
    """
    events = config.major_events().get("events") or {}
    if not events or not items:
        return []
    today = f"{now_tw():%Y-%m-%d}"
    cutoff = f"{now_tw() - timedelta(days=7):%Y-%m-%d}"
    st = json.loads(MAJOR_PATH.read_text(encoding="utf-8")) if MAJOR_PATH.exists() else {}
    st = {k: v for k, v in st.items() if k.rsplit("|", 1)[-1] >= cutoff}
    themes_tw = config.themes().get("tw") or {}
    macro_rows = macro.fresh() if macro_rows is None else macro_rows
    sent = []
    for name, spec in events.items():
        key = f"{name}|{today}"
        used = int(st.get(key) or 0)
        if used >= int(spec.get("max_per_day", 1)):       # 預設同一事件一天一次；會一再反轉的事件（龍頭復職）可在 yaml 設 max_per_day
            continue
        pats = [re.compile(p, flags=re.I) for p in spec.get("patterns", [])]
        excl = re.compile("|".join(spec["exclude"]), flags=re.I) if spec.get("exclude") else None     # 標題含這些字（呼籲、傳聞、機率…）＝還沒發生，不算
        title_only = bool(spec.get("title_only"))        # 只看標題：摘要裡提到別的事不會誤觸發
        hits = [it for it in items if any(p.search(it.title if title_only else f"{it.title} {it.summary}") for p in pats)
                and not (excl and excl.search(it.title))]
        if not hits:
            continue
        st[key] = used + 1
        sent.append(name)
        lines = [f"🌐 重大事件｜{name}"]
        for it in hits[:3]:
            lines.append(f"・{it.title[:90]}" + (f"\n  {it.url}" if it.url and "mops" not in it.url else ""))
        tag = {"+": "受惠", "-": "受傷", "?": "看細節"}
        rows = []
        names_max = int(spec.get("names_max", 4))      # 族群名單列幾檔（預設 4；低軌衛星這類整組都要看的設 8）
        theme_codes: list[str] = []
        for th, (d, why) in (spec.get("themes") or {}).items():
            members = [str(c) for c in (themes_tw.get(th) or [])]
            theme_codes += members
            names = "、".join(f"{c} {(listings.get(c) or {}).get('name', '')}" for c in members[:names_max])
            rows.append(f"・{th}｜{tag.get(d, d)}：{why}" + (f"\n  {names}" if names else ""))
        if rows:
            lines += ["", "初步推論（未驗證，研究完成才算數）："] + rows
        if spec.get("with_research") and theme_codes:      # 附既有研究：開盤前就能看到「這組之前研究過什麼」
            related = research.related_for_codes(list(dict.fromkeys(theme_codes)), limit=5)
            if related:
                lines += ["", "📚 既有研究（成分股相關）："] + [f"・《{t}》{d}" for t, d in related]
        market = macro.line(macro_rows, spec.get("watch") or [])
        if market:
            lines += ["", f"📊 國際行情：{market}"]
        topic = f"供需事件：{spec.get('topic') or name}"
        context = f"\n國際行情快照（Yahoo，延遲報價）：\n{macro.table_text(macro_rows)}" if macro_rows else ""
        queued = research.queue_research(topic, f"{today} 重大事件：{hits[0].title}\n{hits[0].summary[:300]}\n{hits[0].url}{context}",
                                         dedupe=False)
        lines.append(f"\n→ {'已排入研究' if queued else '近期研究過或已在佇列'}：{topic}")
        notify.send("\n".join(lines), channel="news")
    if sent:
        MAJOR_PATH.parent.mkdir(parents=True, exist_ok=True)
        MAJOR_PATH.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    return sent


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
    if not text:
        text = "（只有圖片，沒有文字；圖片見上一則，或看連結）"
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


IMAGE_OCR_PROMPT = ("這是投資 Podcast／社群頻道貼文的圖片。請用繁體中文、純文字回答（不要用 Markdown、不要標題或粗體、不要編號）：\n"
                    "先逐字抄錄圖中所有文字（保留數字、股票代號、單位）；如果是圖表或表格，再用兩三句話說明重點（標題、趨勢、關鍵數字）。"
                    "如果只是聊天截圖、迷因或與投資無關的內容，抄錄文字後加一句「（與投資無關）」就好。"
                    "不要加評論、不要編造看不清楚的字，看不清楚就寫「（看不清楚）」。")
_MEDIA_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif"}


def read_post_images(paths: list) -> str:
    """頻道圖片貼文：用便宜的視覺模型把圖中文字抄下來。沒有金鑰／預算用完／失敗都回空字串（圖片照樣推播）。"""
    if not paths or not ai.settings().get("read_channel_images", True) or not ai.available() or not ai.budget_left():
        return ""
    images = []
    for p in paths:
        raw = Path(p).read_bytes()
        if len(raw) <= 5_000_000:  # API 單張上限 5MB
            images.append((raw, _MEDIA_TYPES.get(Path(p).suffix.lower(), "image/jpeg")))
    if not images:
        return ""
    usage = ai.image_usage()
    try:
        return ai.read_images(images, IMAGE_OCR_PROMPT, usage).strip()
    except Exception as e:  # noqa: BLE001  額度、模型名稱、網路等任何問題都不能讓頻道推播中斷
        log.warning("頻道圖片辨識失敗：%s", e)
        return ""
    finally:
        if usage.calls:
            ai.record_usage(usage, "頻道圖片")


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
    sig_names = signals.signal_names(listings, AMBIGUOUS_NAMES)
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
            push_to = ch.get("push_to", "news")
            if p.images:  # 圖片貼文：先推圖片本身，再把圖中文字抄成文字（AI 辨識）接在貼文後面，一起送去題材偵測
                shots = telegram_channel.download_images(p)
                if shots:
                    notify.send_photos(shots, caption=f"📣 {name}　圖片貼文{'（影片縮圖）' if p.has_video else ''}\n{p.url}",
                                       channel=push_to)
                seen = read_post_images(shots)
                if seen:
                    p = replace(p, text=(f"{p.text}\n\n" if p.text else "") + f"【圖片文字（AI 辨識，可能有誤）】\n{seen}")
                    if telegram_channel.is_ad(p.text, skip):
                        log.info("頻道 %s #%s 圖片內容是業配／廣告，略過", handle, p.post_id)
                        continue
            notify.send(channel_post_message(name, p, listings, futures, themes_tw, name_to_code), channel=push_to)
            if ch.get("signal", True) and not args.latest:  # 供應鏈換料、轉單、淘汰這類早期訊號：立刻推＋排求證
                try:
                    signals.process([signals.Item(f"頻道 {name}", p.text, p.url, "頻道")], listings, sig_names, skip_patterns=skip)
                except Exception as e:  # noqa: BLE001
                    log.warning("頻道早期訊號偵測失敗：%s", e)
            if (not args.latest or args.research) and ch.get("research", True):
                research.add_to_inbox(handle, p.post_id, p.text, p.url)  # 交給題材研究員判斷要不要研究
    if not args.latest:
        telegram_channel.save_state(state)


def _ptt_items(boards: list, name_map: dict[str, str], dry: bool, latest: int, max_fetch: int = 12) -> list:
    """PTT 看板新文章 → 標題像供應鏈換料／轉單的才抓內文 → 早期訊號偵測用的文字。"""
    cfg = signals.settings()
    seen = ptt.load_seen()
    items = []
    for b in boards:
        board, pages = b["board"], int(b.get("pages", 2))
        try:
            posts = ptt.fetch_index(board, pages)
        except Exception as e:  # noqa: BLE001
            log.warning("PTT %s 網頁版抓取失敗（%s），改試 Atom 摘要", board, e)
            try:
                for it in ptt.fetch_atom(board)[-max(latest, 40):]:
                    items.append(signals.Item(f"PTT {board}", f"{it.title}\n{it.summary}", it.url, "論壇"))
            except Exception as e2:  # noqa: BLE001
                log.warning("PTT %s Atom 也抓取失敗：%s", board, e2)
            continue
        fresh = posts[-latest:] if latest else ptt.new_posts(board, posts, seen)
        ranked = sorted(((signals.title_candidate(p.title, name_map, cfg), p) for p in fresh), key=lambda x: -x[0])
        for score, p in [x for x in ranked if x[0] >= 2][:max_fetch]:
            try:
                body = ptt.fetch_article(p.url)
            except Exception as e:  # noqa: BLE001
                log.warning("PTT 文章 %s 抓取失敗：%s", p.url, e)
                body = ""
            items.append(signals.Item(f"PTT {board}", f"{p.title}\n{body}", p.url, "論壇"))
        log.info("PTT %s：讀到 %d 篇，新文章 %d 篇，標題像供應鏈訊號 %d 篇", board, len(posts), len(fresh), len(items))
    if not dry and not latest:
        ptt.save_seen(seen)
    return items


def _probe(urls: list[str], browser: bool = False) -> None:
    """測試：雲端機房讀得到哪些網址（狀態碼、網頁標題、og:description、有沒有出現關鍵字）；browser＝再用無頭瀏覽器讀一次。"""
    import re as _re
    for u in urls:
        try:
            r = net.session().get(u, timeout=20, allow_redirects=True)
        except Exception as e:  # noqa: BLE001 - 只要看連不連得上
            print(f"{u} → 連線失敗：{e}")
            continue
        body = r.text
        title = _re.search(r"<title[^>]*>(.*?)</title>", body, flags=_re.S)
        desc = _re.search(r'<meta[^>]+property="og:description"[^>]+content="([^"]*)"', body)
        hits = [k for k in ("南亞", "台燿", "AWS", "Trainium", "thread_items", "data-sjs", "bigmanj87", "\\u53f0\\u71ff") if k in body]
        print(f"{u} → {r.status_code}（最後網址 {r.url}，{len(body)} 字）")
        print(f"  標題：{(title.group(1).strip() if title else '')[:80]}")
        print(f"  og:description：{(desc.group(1) if desc else '')[:200]}")
        print(f"  出現關鍵字：{hits}")
        links = []
        for m in _re.finditer(r'"url":\s*"(https?:[^"]+)"|href="(https?://[^"]+)"', body):
            link = (m.group(1) or m.group(2)).replace("\\u002F", "/").replace("\\/", "/")
            if link not in links and "linktr.ee/s/" not in link and "static" not in link:
                links.append(link)
        if "feeds/videos.xml" in u or body.lstrip().startswith("<?xml"):   # RSS／Atom：印最新幾筆的標題與時間
            titles = _re.findall(r"<title>([^<]+)</title>", body)[1:11]
            dates = _re.findall(r"<published>([^<]+)</published>", body)[:10]
            print("  最新項目：" + " | ".join(f"{d[:16]} {t[:40]}" for d, t in zip(dates, titles)))
        elif links:
            print("  網頁裡的連結：" + " | ".join(links[:40]))
        if browser:
            try:
                d = threads.render(u)
            except Exception as e:  # noqa: BLE001
                print(f"  瀏覽器讀取失敗：{e}")
                continue
            print(f"  [瀏覽器] 標題：{d['title']}（{d['url']}），貼文連結 {len(d['posts'])} 個")
            print("  [瀏覽器] 頁面文字：" + d["text"][:800].replace("\n", " / "))
            for post in d["posts"][:6]:
                print(f"  [貼文] {post['href']} {post['time']}：{post['text'][:300]!r}")


def cmd_signals(args) -> None:
    """早期訊號：論壇、RSS／Atom 來源、供應鏈關鍵字的 Google 新聞 → 偵測供應鏈換料、轉單、淘汰 → 推播＋排求證研究。"""
    if args.probe:
        _probe(args.probe, args.browser)
        return
    cfg = signals.settings()
    if not cfg.get("enabled", True):
        return
    listings = _listings()
    names = signals.signal_names(listings, AMBIGUOUS_NAMES)
    src = config.sources()
    skip = src.get("skip_patterns") or []
    if args.text:  # 偵錯：直接檢查一段文字
        sigs = signals.process([signals.Item("手動文字", args.text, kind="使用者轉傳" if args.relay else "頻道")], listings, names,
                               dry=True, skip_patterns=skip)
        if not sigs:
            print("沒有偵測到供應鏈訊號")
        return
    st = signals.load_state()
    last = st.get("last_run")
    if last and not (args.dry or args.latest):
        try:
            if now_tw() - datetime.fromisoformat(last) < timedelta(minutes=args.every):
                return
        except ValueError:
            pass
    items: list = []
    items += _ptt_items(src.get("ptt_boards") or [], names, args.dry, args.latest)
    for f in src.get("signal_feeds") or []:
        for it in news.fetch_feed(f["url"], f.get("name", f["url"]))[:30]:
            items.append(signals.Item(f.get("name", "來源"), f"{it.title}\n{it.summary}", it.url, "媒體"))
    for q in src.get("signal_queries") or []:
        for it in news.fetch_google_news(q, "zh-TW")[:15]:
            items.append(signals.Item("Google新聞搜尋", f"{it.title}\n{it.summary}", it.url, "媒體"))
    log.info("早期訊號：共 %d 段文字待檢查", len(items))
    signals.process(items, listings, names, dry=args.dry, skip_patterns=skip)
    if not args.dry:
        st = signals.load_state()
        st["last_run"] = now_tw().isoformat(timespec="seconds")
        signals.save_state(st)


def cmd_threads(args) -> None:
    """追蹤指定的 Threads 帳號（sources.yaml 的 threads_accounts）。他們發的東西不推播：
    供應鏈訊號只排入求證研究（研究好了才推），其他值得看的貼文放進 state/threads_inbox/，由研究員每小時檢視、決定要不要研究。"""
    cfg = signals.settings()
    if not cfg.get("enabled", True):
        return
    src = config.sources()
    handles = [h.lstrip("@") for h in (args.accounts or src.get("threads_accounts") or [])]
    learn = {h.lstrip("@").lower() for h in (src.get("threads_learn_accounts") or [])}
    if not args.accounts:
        handles += sorted(learn - {h.lower() for h in handles})
    handles = handles[:120]
    if not handles:
        return
    st = signals.load_state()
    last = st.get("threads_last_run")
    if last and not (args.dry or args.latest):
        try:
            if now_tw() - datetime.fromisoformat(last) < timedelta(minutes=args.every):
                return
        except ValueError:
            pass
    started = time.monotonic()
    try:
        got = threads.fetch_accounts(handles)
    except ImportError:
        log.warning("Threads：沒有安裝 playwright，略過（watch.yml 會安裝；本機要用請先 pip install playwright && playwright install chromium）")
        return
    seen = threads.load_seen()
    listings = _listings()
    names = signals.signal_names(listings, AMBIGUOUS_NAMES)
    items, entries = [], []
    log.info("Threads：%d／%d 個帳號讀到貼文（%d 則），耗時 %.0f 秒", len(got), len(handles), sum(len(v) for v in got.values()), time.monotonic() - started)
    if args.dry:
        print(f"Threads：{len(got)}／{len(handles)} 個帳號讀到貼文（共 {sum(len(v) for v in got.values())} 則），耗時 {time.monotonic() - started:.0f} 秒")
    now = now_tw()
    for h, posts in got.items():
        first = h not in seen
        fresh = posts[-args.latest:] if args.latest else threads.new_posts(h, posts, seen, now=now)
        # 第一次讀到的帳號：不做訊號偵測，但最近 24 小時內值得看的貼文先放進待檢視清單
        review = fresh if not first or args.latest else threads.recent_posts(posts, 24, now=now)
        if args.dry:
            print(f"@{h}：讀到 {len(posts)} 則，要偵測 {len(fresh)} 則，待檢視 {len(review)} 則")
        for p in review:
            tags, score = signals.post_tags(p.text, names, listings, cfg)
            mine = h.lower() in learn and p.author.lower() == h.lower()
            if mine:   # 學習帳號自己的貼文：手法、心得不會提到公司，也一律進待檢視清單
                tags, score = ["學習"] + tags, max(score, 5)
            if score >= 2:
                entries.append({"id": f"{p.author}/{p.code}", "author": p.author, "url": p.url, "at": p.posted_at,
                                "seen_at": now.isoformat(timespec="seconds"), "reply_to": p.reply_to,
                                "text": p.text[:2000 if mine else 500], "tags": tags, "score": score})
                if args.dry:
                    print(f"  ＋待檢視（{score} 分）@{p.author}：{p.text[:50]!r} {tags}")
        for p in fresh:
            items.append(signals.Item(f"Threads @{p.author}", p.text, p.url, "論壇"))
    if not args.dry and not args.latest:
        threads.save_seen(seen)
    if entries and not args.dry:
        added = threads.add_to_inbox(entries, f"{now:%Y-%m-%d}")
        log.info("Threads：%d 則放進待檢視清單", added)
    # 個人帳號的貼文不推播原文：偵測到供應鏈訊號只排入求證研究，研究好了才推
    signals.process(items, listings, names, dry=args.dry, skip_patterns=src.get("skip_patterns") or [], push=False)
    if not args.dry:
        st = signals.load_state()
        st["threads_last_run"] = now_tw().isoformat(timespec="seconds")
        signals.save_state(st)


def _podcast_episode(ep, src: dict, args, names: dict, listings: dict, cfg: dict) -> None:
    """一集：下載 → 轉文字 → 逐字稿存檔 → 值得看的段落放進待檢視清單；供應鏈訊號只排求證研究（不推播）。"""
    import shutil
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="podcast_"))
    try:
        t0 = time.monotonic()
        audio = podcast.download(ep.audio_url, tmp / "audio", max_mb=int(src.get("podcast_max_mb", 300)))
        t1 = time.monotonic()
        segs = podcast.transcribe(audio, args.model or src.get("podcast_model", "small"), args.max_minutes,
                                  src.get("podcast_prompt", ""), args.threads, args.from_minutes)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    t2 = time.monotonic()
    fixes = src.get("podcast_fixes") or []
    paras = [(t, podcast.apply_fixes(text, fixes)) for t, text in podcast.paragraphs(segs)]
    covered = ((segs[-1].end if segs else 0) - args.from_minutes * 60) / 60
    print(f"{ep.show}｜{ep.title}：下載 {t1 - t0:.0f} 秒、轉文字 {t2 - t1:.0f} 秒（{covered:.0f} 分鐘的內容，約 {covered * 60 / max(t2 - t1, 1):.1f} 倍速）、{len(paras)} 段")
    now = now_tw()
    skip = config.sources().get("skip_patterns") or []
    entries = []
    for t, text in paras:
        if any(re.search(p, text) for p in skip):
            continue  # 業配、優惠碼這類段落不看
        tags, score = signals.post_tags(text, names, listings, cfg)
        if score >= 3:
            entries.append({"id": f"{ep.show}/{ep.key}/{int(t)}", "show": ep.show, "episode": ep.title, "url": ep.link, "at": ep.published,
                            "seen_at": now.isoformat(timespec="seconds"), "t": podcast.mmss(t), "text": text[:600], "tags": tags, "score": score})
    entries = sorted(entries, key=lambda e: -e["score"])[:25]
    # 每段用不同的網址（#t=開始秒數）：偵測的「看過了」記錄是以網址為準，同一集的段落共用網址會只看到第一段
    items = [signals.Item(f"Podcast {ep.show}", text, f"{ep.link or ep.audio_url}#t={int(t)}", "頻道") for t, text in paras]
    if args.dry:
        print("逐字稿開頭：" + "".join(t for _, t in paras)[:1600].replace("\n", " "))
        for e in entries[:8]:
            print(f"  ＋待檢視（{e['score']} 分，{e['t']}）{e['tags']}：{e['text'][:80]!r}")
        signals.process(items, listings, names, dry=True, skip_patterns=skip, push=False)
        return
    path = podcast.save_transcript(ep, paras)
    _archive_episode(ep, paras, names, listings, cfg, skip, args.model or src.get("podcast_model", "small"))   # 新集數也進學習用的歷史記錄
    added = podcast.inbox.add(podcast.INBOX_DIR, [{**e, "transcript": str(path.relative_to(research.ROOT))} for e in entries], f"{now:%Y-%m-%d}")
    log.info("Podcast %s：逐字稿 %s，%d 段放進待檢視清單", ep.show, path, added)
    signals.process(items, listings, names, skip_patterns=skip, push=False)


def _record_entry_prices(out: dict, listings: dict) -> None:
    """剛發布的研究：記下點名個股與加權指數的最新價（成績單的基準價）。只在 GitHub Actions 上做（沙盒連不到 Yahoo），失敗只留警告。"""
    if not os.environ.get("GITHUB_ACTIONS"):
        return
    try:
        research.record_entry_prices(out["data"]["topic"], out["layers"], listings)
    except Exception as e:  # noqa: BLE001
        log.warning("記錄研究基準價失敗：%s", e)


def _git_publish(message: str, paths: list[str]) -> None:
    """在 GitHub Actions 裡把剛產生的檔案立刻 commit＋push（多片同時跑時用 pull --rebase 重試），一集一集存，中途被中斷也不會白跑。"""
    import random
    import subprocess
    if not os.environ.get("GITHUB_ACTIONS"):
        return
    root = str(research.ROOT)

    def run(*cmd: str) -> int:
        return subprocess.run(["git", *cmd], cwd=root, capture_output=True, text=True).returncode

    run("config", "user.name", "github-actions[bot]")
    run("config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
    run("add", "-A", *paths)
    if run("diff", "--cached", "--quiet") == 0:
        return
    run("commit", "-q", "-m", message)
    for _ in range(10):
        if run("pull", "--rebase", "-q") == 0 and run("push", "-q") == 0:
            return
        time.sleep(random.uniform(3, 15))
    log.warning("歷史逐字稿 push 失敗（%s），留給最後的存檔步驟", message)


def _archive_episode(ep, paras, names: dict, listings: dict, cfg: dict, skip: list, model: str) -> dict | None:
    """學習用的歷史記錄：加密逐字稿（state/podcast_archive）＋摘要（research/podcasts）。沒有公鑰就不存（公開 repo 不放原文）。回傳摘要。"""
    if not podcast.PUBKEY_PATH.exists():
        return None
    ads = [any(re.search(pt, text) for pt in skip) for _, text in paras]
    tags = [signals.post_tags(text, names, listings, cfg)[0] if not ad else [] for (_, text), ad in zip(paras, ads)]
    digest = podcast.build_digest(ep, paras, ads, tags, model)
    digest["archive_key"] = podcast.pubkey_id()   # 記下用哪把公鑰加密（換金鑰時用來判斷哪些集數要重轉）
    apath, dpath = podcast.archive_path(ep), podcast.digest_path(ep)
    apath.parent.mkdir(parents=True, exist_ok=True)
    dpath.parent.mkdir(parents=True, exist_ok=True)
    apath.write_bytes(podcast.encrypt_text(podcast.transcript_text(ep, paras)))
    dpath.write_text(json.dumps(digest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return digest


def _parse_ep_ranges(spec: str) -> set[int]:
    """「428-473,475,500-503」→ 集數集合；空字串＝沒有指定。"""
    out: set[int] = set()
    for part in re.split(r"[,\s]+", (spec or "").strip()):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
        if not m:
            raise SystemExit(f"--eps 格式錯誤：{part}（例：428-473,475,500-503）")
        lo, hi = int(m.group(1)), int(m.group(2) or m.group(1))
        out.update(range(min(lo, hi), max(lo, hi) + 1))
    return out


def _podcast_backfill(show: dict, src: dict, args) -> None:
    """歷史集數：由舊到新轉成『加密逐字稿＋摘要』。公開 repo 不放逐字稿原文：原文用 config/podcast_archive_pub.pem 加密，
    私鑰只在研究員手上；摘要（research/podcasts/）只有計數、時間點與提到的公司。已有摘要的集數略過，可重複執行。"""
    t_start = time.monotonic()
    eps = sorted(podcast.fetch_episodes(show), key=lambda e: e.published)
    shards = max(args.shards, 1)
    min_ep = getattr(args, "min_ep", 0) or 0
    pool = [e for e in eps if (podcast.episode_no(e.title) or 0) >= min_ep] if min_ep else eps
    targets = _parse_ep_ranges(getattr(args, "eps", "") or "")   # 指定集數（換金鑰後只重轉這些）
    if targets:
        pool = [e for e in pool if podcast.episode_no(e.title) in targets]
    cur_key = podcast.pubkey_id() if podcast.PUBKEY_PATH.exists() else ""

    def is_done(ep) -> bool:
        """一般模式：有摘要就算處理過。指定集數（--eps）：摘要記的公鑰指紋要等於現在這把、而且加密檔在，才算處理過。"""
        dp = podcast.digest_path(ep)
        if not dp.exists():
            return False
        if not targets:
            return True
        try:
            d = json.loads(dp.read_text(encoding="utf-8"))
            if d.get("skipped") and d.get("archive_key") == cur_key:
                return True   # 太長而略過的集數：不會有加密檔，記錄在就不再重試
            return d.get("archive_key") == cur_key and podcast.archive_path(ep).exists()
        except (ValueError, OSError):
            return False

    mine = [e for i, e in enumerate(pool) if i % shards == args.shard]
    todo = [e for e in mine if not is_done(e)]
    if getattr(args, "newest_first", False):
        todo.reverse()   # 從最新往回轉：和『由舊到新』的片在中間會合，不用等舊的片慢慢爬
    if args.plan:
        hours = sum(e.duration_s for e in eps) / 3600
        by_year: dict[str, list[int]] = {}
        for e in eps:
            by_year.setdefault(e.published[:4], []).append(e.duration_s)
        print(f"{show['name']}：RSS 共 {len(eps)} 集、約 {hours:.0f} 小時；最舊 {eps[0].published[:10]} {eps[0].title[:30]}，最新 {eps[-1].published[:10]} {eps[-1].title[:30]}")
        for y, ds in sorted(by_year.items()):
            print(f"  {y}：{len(ds)} 集、{sum(ds) / 3600:.0f} 小時、平均 {sum(ds) / len(ds) / 60:.0f} 分鐘")
        done = sum(1 for e in eps if podcast.digest_path(e).exists())
        print(f"已處理 {done} 集；第 {args.shard}/{shards} 片還有 {len(todo)} 集（約 {sum(e.duration_s for e in todo) / 3600:.0f} 小時音檔）")
        try:
            info = podcast.channel_info(show)
            print(f"節目：{info.get('title')}｜{info.get('link')}\n簡介：{info.get('description')}\n簡介裡的網址：{info.get('urls')}")
        except Exception as e:  # noqa: BLE001
            print(f"節目資訊讀不到：{e}")
        return
    if not podcast.PUBKEY_PATH.exists():
        raise SystemExit("缺少 config/podcast_archive_pub.pem：沒有公鑰不存逐字稿（公開 repo 不放原文）")
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        raise SystemExit("沒有安裝 faster-whisper")
    listings = _listings()
    names = signals.signal_names(listings, AMBIGUOUS_NAMES)
    cfg = signals.settings()
    skip = config.sources().get("skip_patterns") or []
    fixes = src.get("podcast_fixes") or []
    model = args.model or src.get("podcast_model", "small")
    print(f"{show['name']}：第 {args.shard}/{shards} 片，{len(mine)} 集裡還有 {len(todo)} 集沒處理，模型 {model}，時間預算 {args.budget_min or '不限'} 分鐘")
    done_n = 0
    for ep in todo[:args.max_episodes] if args.max_episodes else todo:
        if done_n and os.environ.get("GITHUB_ACTIONS"):
            import subprocess
            subprocess.run(["git", "pull", "--rebase", "-q"], cwd=str(research.ROOT), capture_output=True)   # 別片剛轉好的集數會同步進來，下一行就能略過
        if is_done(ep):
            continue
        left = args.budget_min - (time.monotonic() - t_start) / 60 if args.budget_min else 1e9
        need = ep.duration_s / 60 / 1.8 + 4   # 保守估：約 1.8 倍速加下載
        if left < need:
            print(f"剩 {left:.0f} 分鐘，{ep.title[:20]} 約需 {need:.0f} 分鐘，收工")
            break
        if ep.duration_s > int(src.get("podcast_max_minutes", 180)) * 60:
            log.warning("%s 太長，略過", ep.title)
            dpath = podcast.digest_path(ep)
            dpath.parent.mkdir(parents=True, exist_ok=True)
            dpath.write_text(json.dumps({"show": ep.show, "ep": podcast.episode_no(ep.title), "title": ep.title, "skipped": "太長，沒有轉文字", "archive_key": cur_key}, ensure_ascii=False) + "\n", encoding="utf-8")
            continue
        try:
            import shutil
            import tempfile
            tmp = Path(tempfile.mkdtemp(prefix="podcast_"))
            try:
                t0 = time.monotonic()
                audio = podcast.download(ep.audio_url, tmp / "audio", max_mb=int(src.get("podcast_max_mb", 300)))
                t1 = time.monotonic()
                segs = podcast.transcribe(audio, model, 0, src.get("podcast_prompt", ""), args.threads, 0)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            t2 = time.monotonic()
            paras = [(t, podcast.apply_fixes(text, fixes)) for t, text in podcast.paragraphs(segs)]
            digest = _archive_episode(ep, paras, names, listings, cfg, skip, model)
            apath, dpath = podcast.archive_path(ep), podcast.digest_path(ep)
            print(f"{ep.title[:24]}｜{ep.published[:10]}｜{ep.duration_s // 60} 分：下載 {t1 - t0:.0f} 秒、轉文字 {t2 - t1:.0f} 秒（{ep.duration_s / max(t2 - t1, 1):.1f} 倍速）、{len(paras)} 段、市場話題 {digest['market_start'] or '?'}、問答 {digest['qa_start'] or '?'}")
            done_n += 1
            _git_publish(f"歷史逐字稿 {show['name']} {podcast.episode_name(ep)}（加密）與摘要", [str(apath.relative_to(research.ROOT)), str(dpath.relative_to(research.ROOT))])
        except Exception as e:  # noqa: BLE001 - 一集失敗不影響其他集數，下次再試
            log.warning("%s 處理失敗：%s", ep.title, e)
    left_n = sum(1 for e in mine if not is_done(e))
    print(f"這次處理 {done_n} 集，耗時 {(time.monotonic() - t_start) / 60:.0f} 分鐘；這一片還剩 {left_n} 集")
    if done_n and left_n:
        print(f"接力：還有 {left_n} 集（workflow 會再啟動同一片）")


def cmd_podcasts(args) -> None:
    """Podcast：新集數下載、轉成逐字稿（faster-whisper）→ 值得看的段落放進 state/podcast_inbox/ 讓研究員檢視；
    供應鏈訊號只排求證研究。不推播。轉文字很慢，由 podcast.yml 執行，不在 2 分鐘監看迴圈裡。"""
    src = config.sources()
    shows = [s for s in (src.get("podcasts") or []) if not args.show or args.show in s["name"]]
    if not shows:
        return
    if getattr(args, "backfill", False) or getattr(args, "plan", False):
        for sh in shows:
            _podcast_backfill(sh, src, args)
        return
    seen = podcast.load_seen()
    todo = []
    for sh in shows:
        try:
            eps = podcast.fetch_episodes(sh)
        except Exception as e:  # noqa: BLE001 - 一個節目讀不到不影響其他
            log.warning("Podcast %s 讀取失敗：%s", sh.get("name"), e)
            continue
        if args.list:
            print(f"{sh['name']}：RSS 共 {len(eps)} 集，最新 5 集：")
            for e in eps[:5]:
                print(f"  {e.published[:16]}  {e.duration_s // 60:>4} 分  {e.size / 1e6:5.1f}MB  {e.title[:50]}")
            continue
        todo += eps[:1] if args.force else podcast.pick_new(sh["name"], eps, seen, first=args.first, max_age_days=int(src.get("podcast_max_age_days", 7)))
    if args.list or not todo:
        if not args.dry and not args.list:
            podcast.save_seen(seen)
        return
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        log.warning("Podcast：沒有安裝 faster-whisper，略過（podcast.yml 會安裝）")
        return
    listings = _listings()
    names = signals.signal_names(listings, AMBIGUOUS_NAMES)
    cfg = signals.settings()
    for ep in todo[:(args.max_episodes or 2)]:
        if ep.duration_s > int(src.get("podcast_max_minutes", 180)) * 60:
            log.warning("Podcast %s｜%s 長 %d 分鐘，超過上限，略過", ep.show, ep.title, ep.duration_s // 60)
        else:
            try:
                _podcast_episode(ep, src, args, names, listings, cfg)
            except Exception as e:  # noqa: BLE001 - 失敗的集數不標記已處理，下次再試（連結壞掉、音檔太大則下面標記略過）
                log.warning("Podcast %s｜%s 處理失敗：%s", ep.show, ep.title, e)
                if isinstance(e, ValueError):
                    seen.setdefault(ep.show, []).append(ep.key)
                continue
        seen.setdefault(ep.show, []).append(ep.key)
    if not args.dry and not args.force:
        podcast.save_seen(seen)
        podcast.prune_transcripts()


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
    done_key = f"trade:研究完成:{topic}"
    if theme_card.already_sent(done_key):  # 同一個題材一天只推一次研究完成的可卡位標的
        return
    push_trade_candidates(f"{topic}（研究完成）", codes, listings, "🔬 研究完成，以下是研究裡有股期的標的")
    theme_card.mark_sent(done_key)


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
        text = candidates.message(title, context, rs, mkt)
        try:  # 題材補漲：龍頭剛放量突破時，同題材還沒漲的受惠股另列一段（失敗不影響主訊息）
            from .trade import early
            from .trade.paper import PARAMS
            states = {c: early.last_state(bt.prepare(df, PARAMS)) for c, df in hist.items() if df is not None and len(df) >= 80}
            lead, lag = early.laggard_picks(codes, {c: s for c, s in states.items() if s})
            text += early.laggard_section(lead, lag, {x["code"]: x for x in rs}, names)
        except Exception as e:  # noqa: BLE001
            log.warning("補漲候選 %s 產生失敗：%s", title, e)
        notify.send(text, channel="trade")
    except Exception as e:  # noqa: BLE001
        log.warning("可卡位標的 %s 產生失敗：%s", title, e)


def push_lead_list(listings: dict, futures: dict, channel: str = "trade", force: bool = False) -> str | None:
    """盤前領先名單：美股昨夜領先指標（config/us_lead.yaml）大漲 → 台股對應族群裡還沒跟上的股票（有股票期貨者）。
    沒有領先指標達門檻就不推（force＝手動執行時仍回一則「昨夜沒有」）。回傳推播內容（沒推就回 None）。"""
    from .trade import backtest as bt
    from .trade import candidates, early, real
    from .trade.paper import PARAMS
    lead_map = config.us_lead()
    if not lead_map:
        return None
    us_hist = yahoo.fetch_many(sorted(lead_map), range_="3mo")
    moves = early.us_moves(us_hist, lead_map)
    if not moves:
        near = early.us_moves(us_hist, lead_map, floor_ratio=-100)[:3]
        info = "、".join(f"{m['name']} {m['sym']} {m['pct']:+.1f}%（門檻 {m['thr']:.1f}%）" for m in near)
        log.info("美股領先指標：昨夜沒有達門檻的大漲（%d/%d 檔有資料；最接近：%s）", len(us_hist), len(lead_map), info)
        if force:
            text = f"🌎 盤前領先名單 {now_tw():%m/%d}｜昨夜美股領先指標沒有達門檻的大漲 → 今天沒有領先名單\n最接近的：{info}"
            notify.send(text, channel=channel)
            return text
        return None
    themes_tw = {k: [str(c) for c in v] for k, v in (config.themes().get("tw") or {}).items()}
    codes = [c for m in moves for th in m["themes"] for c in map(str, themes_tw.get(th, []))
             if (futures.get(c) or {}).get("std") or (futures.get(c) or {}).get("mini")]
    codes = list(dict.fromkeys(codes))
    if not codes:
        return None
    hist = _yahoo_histories(codes, listings, range_="1y")
    states = {c: early.last_state(bt.prepare(df, PARAMS)) for c, df in hist.items() if df is not None and len(df) >= 80}
    picks_ = early.lead_picks(moves, themes_tw, {c: s for c, s in states.items() if s}, futures)
    index_df, _ = yahoo.fetch_chart("^TWII", range_="1y")
    mkt = bool(bt.market_ok(index_df).iloc[-1]) if len(index_df) > 60 else False
    names = {c: (listings.get(c) or {}).get("name", c) for c in codes}
    rows = {x["code"]: x for x in candidates.rows([p["code"] for p in picks_], hist, futures, names,
                                                  real.load()["equity"], _margin_rates())}
    text = early.lead_message(f"{now_tw():%m/%d}", moves, picks_, rows, names, mkt, note=early.LEAD_NOTE)
    try:  # 模擬帳戶 L（B 3 檔＋領先專用 1 檔）：把領先名單排進今天開盤的計劃，盤後 15:30 用開盤價成交
        text += "\n\n" + _lead_paper_plan(picks_, hist, futures, listings, mkt)
    except Exception as e:  # noqa: BLE001
        log.warning("模擬帳戶 L 領先計劃失敗：%s", e)
    notify.send(text, channel=channel)
    return text


def _lead_paper_plan(picks_: list[dict], hist: dict, futures: dict, listings: dict, mkt: bool) -> str:
    """有「額外名額」的模擬帳戶（L）收到領先名單：取第一檔算得出口數的排進今天開盤的計劃。回傳要附在推播後面的說明。"""
    from .trade import backtest as bt
    from .trade import paper
    prepped = {c: bt.prepare(df, paper.PARAMS) for c, df in hist.items() if df is not None and len(df) > 80}
    names = {c: (listings.get(c) or {}).get("name", c) for c in prepped}
    try:
        alerts = tw_daily.load_alerts()
    except Exception as e:  # noqa: BLE001
        log.warning("處置／注意股清單抓取失敗，領先計劃不排除處置股：%s", e)
        alerts = {}
    lines = []
    for name, (label, p) in paper.VARIANTS.items():
        if not p.extra_slots:
            continue
        st = paper.load(name)
        added = paper.add_lead_plan(st, picks_, prepped, futures, names, _margin_rates(), alerts, mkt, p)
        paper.save(st, name)
        if added:
            lines.append(f"🧪 模擬帳戶 {name} 今天開盤計劃（名額 {p.extra_slots} 檔，先成交的佔位）：" + "、".join(
                f"{x['code']} {x['name']} {x['contract']} {x['qty']} 口（停損約 {x['stop_est']:,.1f}）" for x in added))
        else:
            lines.append(f"🧪 模擬帳戶 {name}：今天沒有領先計劃（大盤空頭、領先位已有持倉，或算不出口數）")
    return "\n".join(lines)


def _research_trial(topic: str, trigger: str) -> None:
    """試跑：用目前的研究模型（可用環境變數 RESEARCH_MODEL 換）研究一個題材，報告與擷取結果只印在紀錄裡。
    不存報告、不推播、不動研究索引與佇列；花費照樣記進每月預算。拿來比較不同模型的品質與花費。"""
    ai.check_budget()
    usage = ai.research_usage()
    prompt = research.REPORT_TEMPLATE.format(topic=topic, trigger=trigger[:3000] or "（試跑）",
                                             knowledge=research.relevant_knowledge(topic, research.load_index()))
    log.info("試跑研究：%s（模型 %s）", topic, ai._model())
    try:
        report = ai.web_research(research.SYSTEM, prompt, usage)
        data = ai.extract_json(research.EXTRACT_INSTRUCTION, report, research.REPORT_SCHEMA, usage)
    finally:
        if usage.calls:
            ai.record_usage(usage, f"試跑 {topic}")
    print(f"\n===== 試跑結果｜模型 {ai._model()}｜約 US${usage.usd():.2f}｜搜尋 {usage.web_searches} 次 =====\n")
    print(report)
    print("\n===== 擷取出的結構化資料 =====\n")
    print(json.dumps(data, ensure_ascii=False, indent=1))


def cmd_research(args) -> None:
    """題材研究員：手動題材、Telegram 指令、頻道貼文收件匣、資金流入題材佇列、每日新聞自動偵測。"""
    if getattr(args, "trial", None):
        _research_trial(args.trial, getattr(args, "trial_trigger", ""))
        return
    st = ai.settings()
    push = st.get("push_to", "research")
    queue: list[tuple] = []  # (題材, 觸發來源, 是否自動, 佇列檔)；佇列檔研究成功才刪，額度不足時保留給下一次
    listings_cache: dict = {}

    def listings() -> dict:
        if not listings_cache:
            listings_cache.update(_listings())
        return listings_cache

    if args.topic:
        queue.append((args.topic, "手動指定", False, None))

    if args.commands:
        token = notify.bot_token(push)
        offset = research.load_offset(token)
        reqs, offset = research.pending_requests(
            notify.get_updates(push, offset), notify.owner_chat(push), offset,
            free_text=lambda t: len(t or "") >= 8 and signals.looks_like_intel(t, signals.signal_names(listings(), AMBIGUOUS_NAMES)))
        research.save_offset(token, offset)
        for kind, text in reqs:
            if kind == "intel":  # 使用者轉傳的情報：不需要 AI 金鑰，立刻偵測、推播、排求證
                reply = signals.handle_relay(text, listings(), signals.signal_names(listings(), AMBIGUOUS_NAMES), channel=push,
                                             skip_patterns=config.sources().get("skip_patterns") or [])
                if reply:
                    notify.send(reply, channel=push)
                continue
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
            queue.append((text, "Telegram 指令", False, None))

    auto_texts: list[str] = []  # 當天重點新聞標題：偵測出的題材受 max_per_day 限制
    chan_texts: list[str] = []  # 追蹤頻道（股癌等）貼文：偵測出的題材不受 max_per_day 限制（仍受每月預算）
    triggers: dict[str, str] = {}
    inbox_files: list = []
    if args.inbox:
        items = research.read_inbox()
        for p, d in items:
            chan_texts.append(d["text"][:600])
            triggers[d["text"][:600]] = f"{d['channel']}：{d['text'][:1500]}\n{d.get('url', '')}"
            inbox_files.append(p)
    if args.auto_slots and _auto_due():
        args.auto = True
    if args.auto:
        listings_ = listings()
        ranked = news_signals.rank(collect_news(), config.news_keywords(), _name_to_code(listings_), _us_symbols(),
                                   min_score=3, all_codes=set(listings_))
        auto_texts += [it.title for it in ranked[:80]]

    cap = int(st.get("max_per_day", 4)) - research.researched_today()
    if args.inbox and ai.available() and ai.budget_left():  # 資金流入但還沒研究過的題材（盤中、盤後排入）
        for p, d in research.read_queue():
            if cap <= 0:
                break
            queue.append((d["topic"], d.get("trigger", "資金流入"), True, p))
            cap -= 1

    min_imp = int(st.get("min_importance", 3))
    if chan_texts:
        # 股癌等追蹤頻道的貼文：該研究就研究，不看每天上限（is_auto=False 也不計入上限）；偵測沒做成就先留著，下一輪再試
        detected = False
        if ai.available() and ai.budget_left():
            try:
                found = research.detect_topics(chan_texts)
                detected = True
            except ai.AIUnavailable as e:
                log.warning("頻道貼文題材偵測失敗：%s", e)
                found = []
            for t in [x for x in found if x["importance"] >= min_imp]:
                if any(q[0] == t["topic"] for q in queue):
                    continue
                src = next((v for k, v in triggers.items() if t["topic"] in k), "")
                queue.append((t["topic"], f"頻道貼文偵測（重要性 {t['importance']}）：{t['reason']}\n{src}", False, None))
        if detected:
            for p in inbox_files:  # 偵測完才刪，避免重複偵測
                p.unlink(missing_ok=True)
        else:
            log.info("頻道貼文 %d 則暫時無法偵測題材（沒有金鑰、預算用完或失敗），先留在收件匣", len(inbox_files))
            for p in sorted(inbox_files, key=lambda f: (len(f.name), f.name))[:-60]:  # 只留最新 60 則，免得越積越多
                p.unlink(missing_ok=True)

    if auto_texts and ai.available():
        if cap <= 0:
            log.info("今天自動研究已達上限 %s 個", st.get("max_per_day", 4))
        else:
            try:
                found = research.detect_topics(auto_texts)
            except ai.AIUnavailable as e:
                log.warning("題材偵測失敗：%s", e)
                found = []
            for t in [x for x in found if x["importance"] >= min_imp and not any(q[0] == x["topic"] for q in queue)][:cap]:
                queue.append((t["topic"], f"自動偵測（重要性 {t['importance']}）：{t['reason']}", True, None))
    elif auto_texts:
        log.info("沒有 ANTHROPIC_API_KEY，略過自動題材偵測")

    pending = research.PENDING_DIR.exists() and any(research.PENDING_DIR.glob("*.json"))
    if not queue and not pending:
        return
    futures = stock_futures.load_stock_futures()
    for out in research.publish_pending(listings(), futures) if pending else []:
        _record_entry_prices(out, listings())
        notify.send(out["message"], channel=push)
        _send_card_for_topic(out["data"]["topic"], listings(), push, "🔬 研究完成")
    for i, (topic, trigger, is_auto, qfile) in enumerate(queue):
        try:
            out = research.research_topic(topic, trigger, listings(), futures, auto=is_auto)
            _record_entry_prices(out, listings())
            if qfile:
                qfile.unlink(missing_ok=True)
            notify.send(out["message"], channel=push)
            why = trigger.splitlines()[0][:80] if trigger else ""
            _send_card_for_topic(out["data"]["topic"], listings(), push, f"🔬 研究完成｜{why}" if why else "🔬 研究完成")
        except ai.AIUnavailable as e:
            log.warning("研究「%s」失敗：%s", topic, e)
            if "預算" in str(e) or "ANTHROPIC_API_KEY" in str(e) or "餘額" in str(e):
                # 額度問題：題材全部留在佇列（Telegram 指令的也補排進去），之後額度恢復或由 Claude 排程接手；通知一天一次就好
                for t, trg, _, qf in queue[i:]:
                    if qf is None:
                        research.queue_research(t, trg)
                if not theme_card.already_sent("warn:ai_unavailable"):
                    notify.send(f"⚠️ AI 研究暫停：{e}。題材已留在研究佇列，不會遺失。", channel=push)
                    theme_card.mark_sent("warn:ai_unavailable")
                break
            if qfile:
                qfile.unlink(missing_ok=True)  # 模型拒答之類：不重試，避免每 2 分鐘重跑
            notify.send(f"⚠️ 研究「{topic}」沒有完成：{e}", channel=push)


def cmd_card(args) -> None:
    """產生並推播題材卡（圖片＋目標價＋股票期貨），例如：python -m market_intel card 玻纖布"""
    if not theme_card.send_query(args.query, _listings(), context="", channel=args.channel):
        print(f"找不到題材：{args.query}")


def _overnight_line(idx: dict | None) -> str:
    """選股機器人第一則的隔夜環境一行（夜盤、美股、亞股＋加權現在漲跌）；任何錯誤都回傳空字串，不影響選股推播。"""
    try:
        p = overnight.parts()
        if not p:
            return ""
        taiex = (idx or {}).get("加權指數")
        return overnight.short_line(*p, getattr(taiex, "change_pct", None))
    except Exception as e:  # noqa: BLE001
        log.warning("隔夜環境一行失敗：%s", e)
        return ""


def cmd_infographic(args) -> None:
    """研究資訊圖（HTML 排版截圖）：只輸出圖檔，不推播。例如：python -m market_intel infographic --topic 載板供需 --out /tmp/info"""
    from . import infographic
    index = research.load_index()
    names = [t for t in index if args.topic and args.topic in t]
    if not names:
        raise SystemExit(f"找不到研究題材「{args.topic}」（research/index.json 的名稱，可只填部分文字）")
    topic = names[0]
    data = json.loads((research.data_dir() / f"{research.slug(topic)}.json").read_text(encoding="utf-8"))
    data.setdefault("topic", topic)
    print(topic)
    for f in infographic.render_all(data, Path(args.out) / research.slug(topic)):
        print(f)


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
    picks_: list[tuple[str, str, str, str]] = []  # (代號, 名稱, 原因, 觸發說明)
    stale_: list[tuple[str, str, str, str]] = []
    flows: dict[str, float] = {}  # 當天漲跌幅：連動預研時，已經動起來的相關個股排前面
    index = research.load_index()
    if day is not None and not day.empty:
        flows = {str(r.code): float(r.pct) for r in day.itertuples()}
        hot = day[(day["pct"] >= SCAN_LIMIT_PCT) & (day["value"] >= SCAN_MIN_VALUE)
                  & ~day["code"].astype(str).str.startswith("0")].sort_values("value", ascending=False)
        for r in hot.itertuples():
            why = f"漲停 {r.pct:+.1f}%、成交 {r.value / 1e8:.1f} 億"
            if r.code not in known:
                picks_.append((r.code, r.name, why, "不在任何族群的強勢股"))
            elif prefetch.is_stale(r.code, index):  # 已知族群，但最近沒有任何研究提到它（新敘事可能還沒研究）
                stale_.append((r.code, r.name, why, "已知族群但近期沒研究過的強勢股"))
    revenue = tw_daily.load_revenue()
    for code, rv in sorted(revenue.items(), key=lambda kv: -(kv[1].get("yoy") or 0)):
        if code in known or code not in listings or (rv.get("yoy") or 0) < REV_MIN_YOY or (rv.get("rev") or 0) < REV_MIN_K:
            continue
        key = f"rev:{code}:{rv.get('ym')}"
        if key in seen:
            continue
        seen[key] = today
        picks_.append((code, listings[code].get("name", code),
                       f"{rv.get('ym')} 營收年增 {rv['yoy']:+.0f}%（{(rv.get('rev') or 0) / 1e5:.1f} 億）",
                       "不在任何族群的強勢股"))
    picks_ += stale_  # 全新的先排，已知族群但沒研究過的排後面（共用每天上限）
    lines, queued = [], []
    for code, name, why, label in picks_:
        if len(queued) >= SCAN_MAX_PER_DAY:
            break
        topic = f"{name}（{code}）題材與產業"
        if not research.queue_research(topic, f"{today} {label}：{code} {name}｜{why}"):
            continue
        queued.append(topic)
        fut = theme_card.fut_text(futures.get(code), known=bool(futures))
        lines.append(f"・{code} {name}｜{why}｜{fut if fut not in ('無', '未知') else '股票期貨：' + fut}")
        try:  # 看到 A 就把同題材的 B、C 和上下游一起排進研究
            near = prefetch.expand_known(code, listings, flows=flows)
        except Exception as e:  # noqa: BLE001
            log.warning("連動預研失敗：%s", e)
            near = []
        if near:
            lines.append("  ↳ 連動預研：" + "、".join(t.split("（")[0] for t in near))
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
        # 同一份研究的別名族群（例如『光學鏡頭』與研究自動產生的『研究:光學鏡頭』）只算一個，不要各推一次
        picked, seen_topics = [], set()
        for r in inflow.to_dict("records"):
            name = r["族群"]
            codes = [str(c) for c in themes_tw.get(name, [])]
            topic = research.topic_for_theme(name, codes)
            canon = topic or name
            if canon in seen_topics:
                continue
            seen_topics.add(canon)
            picked.append((r, name, codes, topic, canon))
            if len(picked) >= top:
                break
        for r, name, codes, topic, canon in picked:
            context = (f"🔥 {source}資金流入：{ratio_name} {r[ratio_col]}、資金 {r[money_col]:+.1f} 億、"
                       f"加權 {r['加權漲跌%']:+.1f}%")
            key_trade = f"trade:{source}:{canon}"
            # 研究剛完成時已經推過同一份可卡位標的，就不重複推
            if not theme_card.already_sent(key_trade) and not (topic and theme_card.already_sent(f"trade:研究完成:{topic}")):
                # 一條龍第一步：不等研究，先推可卡位標的
                pool = list(dict.fromkeys(codes + list((research.load_index().get(topic) or {}).get("codes", []))))
                push_trade_candidates(f"{name}", pool, listings, context + ("" if topic else "｜研究中"), futures)
                theme_card.mark_sent(key_trade)
            if not topic:
                if research.queue_research(name, f"{now_tw():%Y-%m-%d} {context}\n成分股：{'、'.join(codes)}"):
                    queued.append(f"{name}（{context[2:]}）")
                continue
            if research.research_age_days(topic) > 30:
                research.queue_research(topic, f"{now_tw():%Y-%m-%d} 更新研究｜{context}")
            key = f"{source}:{canon}"
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
        cap = int(ai.settings().get("max_per_day", 4))
        msg += (f"\n\n常駐監看會自動研究（金鑰只放在監看那邊）；今天已自動研究 {research.researched_today()}／{cap} 個，"
                "超過的排到明天，也可以請 Claude 先研究。")
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


def _backtest_maexit(data, futures, index_df, rates, capital: float) -> None:
    """跌破 20 日線出場從持有第幾天開始檢查：進場當天就查 vs 隔天起（現行）vs 更晚。拉回型另外列出。"""
    from .trade import backtest as bt
    cut = index_df.index[len(index_df) // 2]
    first = ({c: d[d.index < cut] for c, d in data.items()}, index_df[index_df.index < cut])
    warm = cut - pd.Timedelta(days=300)
    second = ({c: d[d.index >= warm] for c, d in data.items()}, index_df[index_df.index >= warm])
    rows = []
    for k, label in ((1, "進場當天就檢查"), (2, "進場隔天起（現行）"), (3, "進場後第 3 天起")):
        par = bt.Params(capital=capital, exit_ma="ma20", ma_exit_from=k)
        full = bt.run(data, futures, index_df, par, rates)
        a = bt.run(first[0], futures, first[1], par, rates)
        b = bt.run(second[0], futures, second[1], par, rates)
        pb = full["各型態"].get("拉回", {})
        rows.append({"20日線出場": label, "報酬%": full["報酬率%"], "最大回撤%": full["最大回撤%"], "筆數": full["交易筆數"],
                     "勝率%": full["勝率%"], "平均R": full["平均R"], "前半段%": a["報酬率%"], "後半段%": b["報酬率%"],
                     "拉回型筆數": pb.get("筆數"), "拉回型平均R": pb.get("平均R"), "拉回型總損益": pb.get("總損益")})
    table = pd.DataFrame(rows)
    print(table.to_string(index=False))
    out = research.RESEARCH_DIR / "backtest"
    out.mkdir(parents=True, exist_ok=True)
    (out / "maexit.json").write_text(table.to_json(orient="records", force_ascii=False, indent=1), encoding="utf-8")


def _backtest_highwin(data, futures, index_df, rates, capital: float) -> None:
    """高勝率版本：停損放寬、提早停利、只做拉回、品質分數門檻、縮短持有期，與現行 B 比。"""
    from .trade import backtest as bt
    cut = index_df.index[len(index_df) // 2]
    first = ({c: d[d.index < cut] for c, d in data.items()}, index_df[index_df.index < cut])
    warm = cut - pd.Timedelta(days=300)
    second = ({c: d[d.index >= warm] for c, d in data.items()}, index_df[index_df.index >= warm])
    cases = {"B 現行": {}}
    for am in (2.0, 2.5, 3.0):
        cases[f"停損 {am}ATR"] = {"atr_mult": am}
    for tr in (1.0, 1.5):
        cases[f"+{tr}R 先出一半"] = {"take_r": tr}
    cases["停損2.5ATR＋1R出一半"] = {"atr_mult": 2.5, "take_r": 1.0}
    cases["停損3ATR＋1R出一半"] = {"atr_mult": 3.0, "take_r": 1.0}
    cases["只做拉回"] = {"setups": ("拉回",)}
    cases["只做拉回＋停損2.5ATR"] = {"setups": ("拉回",), "atr_mult": 2.5}
    cases["品質分數≥3"] = {"min_score": 3}
    cases["品質≥3＋停損2.5ATR＋1R出一半"] = {"min_score": 3, "atr_mult": 2.5, "take_r": 1.0}
    for ts in (5, 7, 15):
        cases[f"時間停損 {ts} 天"] = {"time_stop": ts}
    rows = []
    for name, kw in cases.items():
        par = bt.Params(capital=capital, exit_ma="ma20", **kw)
        full = bt.run(data, futures, index_df, par, rates)
        a = bt.run(first[0], futures, first[1], par, rates)
        b = bt.run(second[0], futures, second[1], par, rates)
        rows.append({"策略": name, "勝率%": full["勝率%"], "報酬%": full["報酬率%"], "年化%": full["年化%"],
                     "最大回撤%": full["最大回撤%"], "筆數": full["交易筆數"], "平均R": full["平均R"],
                     "前半段%": a["報酬率%"], "後半段%": b["報酬率%"], "翻倍": full["翻倍日期"]})
    table = pd.DataFrame(rows)
    print(table.to_string(index=False))
    out = research.RESEARCH_DIR / "backtest"
    out.mkdir(parents=True, exist_ok=True)
    (out / "highwin.json").write_text(table.to_json(orient="records", force_ascii=False, indent=1), encoding="utf-8")


def _backtest_gate(data, futures, index_df, rates, capital: float) -> None:
    """進場門檻：品質分數（離52週高、200日線、相對強度、大盤強勢）太低的訊號乾脆不做。"""
    from .trade import backtest as bt
    cut = index_df.index[len(index_df) // 2]
    first = ({c: d[d.index < cut] for c, d in data.items()}, index_df[index_df.index < cut])
    warm = cut - pd.Timedelta(days=300)
    second = ({c: d[d.index >= warm] for c, d in data.items()}, index_df[index_df.index >= warm])
    rows = []
    for ms in (0, 1, 2, 3, 4):
        par = bt.Params(capital=capital, exit_ma="ma20", min_score=ms)
        full = bt.run(data, futures, index_df, par, rates)
        a = bt.run(first[0], futures, first[1], par, rates)
        b = bt.run(second[0], futures, second[1], par, rates)
        rows.append({"品質分數門檻": f"≥{ms}" if ms else "不設門檻（現行）", "報酬%": full["報酬率%"], "年化%": full["年化%"],
                     "最大回撤%": full["最大回撤%"], "筆數": full["交易筆數"], "勝率%": full["勝率%"], "平均R": full["平均R"],
                     "前半段%": a["報酬率%"], "後半段%": b["報酬率%"], "翻倍": full["翻倍日期"]})
    table = pd.DataFrame(rows)
    print(table.to_string(index=False))
    out = research.RESEARCH_DIR / "backtest"
    out.mkdir(parents=True, exist_ok=True)
    (out / "gate.json").write_text(table.to_json(orient="records", force_ascii=False, indent=1), encoding="utf-8")


def _backtest_exceptions(data, futures, index_df, rates, capital: float) -> None:
    """2% 算不出 1 口的強勢股，放寬到「1 口最多虧權益多少 %」會不會更好（可選只給品質分數高的）。"""
    from .trade import backtest as bt
    cut = index_df.index[len(index_df) // 2]
    first = ({c: d[d.index < cut] for c, d in data.items()}, index_df[index_df.index < cut])
    warm = cut - pd.Timedelta(days=300)
    second = ({c: d[d.index >= warm] for c, d in data.items()}, index_df[index_df.index >= warm])
    cases = {"B 現行（嚴守 2%）": {}}
    for r in (0.03, 0.04, 0.05, 0.06):
        cases[f"E 1口例外 ≤{r:.0%}"] = {"one_lot_risk": r}
    for r, sc in ((0.05, 2), (0.05, 3), (0.06, 3)):
        cases[f"E 1口例外 ≤{r:.0%}、品質分數≥{sc}"] = {"one_lot_risk": r, "one_lot_min_score": sc}
    rows = []
    for name, kw in cases.items():
        par = bt.Params(capital=capital, exit_ma="ma20", **kw)
        full = bt.run(data, futures, index_df, par, rates)
        a = bt.run(*first[:1], futures, first[1], par, rates)
        b = bt.run(*second[:1], futures, second[1], par, rates)
        big = [t for t in full["trades"] if (t["entry"] - t["stop"]) * t["mult"] * t["qty0"] > capital * 0.0205]
        rows.append({"策略": name, "報酬%": full["報酬率%"], "年化%": full["年化%"], "最大回撤%": full["最大回撤%"],
                     "筆數": full["交易筆數"], "勝率%": full["勝率%"], "平均R": full["平均R"],
                     "前半段%": a["報酬率%"], "後半段%": b["報酬率%"], "超過2%的筆數": len(big),
                     "其中平均R": round(sum(t["r"] for t in big) / len(big), 2) if big else None, "翻倍": full["翻倍日期"]})
    table = pd.DataFrame(rows)
    print(table.to_string(index=False))
    out = research.RESEARCH_DIR / "backtest"
    out.mkdir(parents=True, exist_ok=True)
    (out / "exceptions.json").write_text(table.to_json(orient="records", force_ascii=False, indent=1), encoding="utf-8")


def _backtest_daytrade(data, futures, index_df, rates, capital: float) -> None:
    """當沖（日 K 近似）vs 現行波段：全期間與前後半段。"""
    from .trade import backtest as bt
    cut = index_df.index[len(index_df) // 2]
    first = ({c: d[d.index < cut] for c, d in data.items()}, index_df[index_df.index < cut])
    warm = cut - pd.Timedelta(days=300)
    second = ({c: d[d.index >= warm] for c, d in data.items()}, index_df[index_df.index >= warm])
    cases = {
        "B 現行波段（不當沖）": lambda d, i: bt.run(d, futures, i, bt.Params(capital=capital, exit_ma="ma20"), rates),
        "D1 當沖、收盤全出": lambda d, i: bt.run_daytrade(d, futures, i, bt.Params(capital=capital), rates, hold_locked=False),
        "D2 當沖、漲停鎖住才留倉": lambda d, i: bt.run_daytrade(d, futures, i, bt.Params(capital=capital), rates),
        "D3 同D2、品質分數≥2": lambda d, i: bt.run_daytrade(d, futures, i, bt.Params(capital=capital), rates, min_quality=2),
        "D4 同D2、品質分數≥3": lambda d, i: bt.run_daytrade(d, futures, i, bt.Params(capital=capital), rates, min_quality=3),
        "D5 同D3、停損 1.5ATR": lambda d, i: bt.run_daytrade(d, futures, i, bt.Params(capital=capital), rates,
                                                       min_quality=2, stop_atr=1.5),
    }
    rows = []
    for name, fn in cases.items():
        full = fn(data, index_df)
        a = fn(*first)
        b = fn(*second)
        reasons = full["出場原因"]
        rows.append({"策略": name, "報酬%": full["報酬率%"], "年化%": full["年化%"], "最大回撤%": full["最大回撤%"],
                     "筆數": full["交易筆數"], "勝率%": full["勝率%"], "平均R": full["平均R"],
                     "前半段%": a["報酬率%"], "後半段%": b["報酬率%"],
                     "漲停留倉筆數": reasons.get("漲停鎖住留倉，隔天開盤出", 0), "翻倍": full["翻倍日期"]})
    table = pd.DataFrame(rows)
    print(table.to_string(index=False))
    out = research.RESEARCH_DIR / "backtest"
    out.mkdir(parents=True, exist_ok=True)
    (out / "daytrade.json").write_text(table.to_json(orient="records", force_ascii=False, indent=1), encoding="utf-8")


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


def _backtest_early(data, futures, index_df, rates, capital: float, range_: str) -> None:
    """提前進場訊號比較：美股領先、題材補漲，以及預突破、RS 新高、盤中突破進場，對照現行（收盤突破／拉回）。

    先比「訊號品質」（不受口數、持倉數限制：隔天開盤買進後 1／3／5／10 日平均報酬，以及訊號當下近 20 日已經漲了多少），
    再比「組合回測」（同一套停損、出場、2% 風險、最多 3 檔、前後半段檢查）。
    """
    from .trade import backtest as bt
    from .trade import early
    base = bt.Params(capital=capital, exit_ma="ma20")  # 策略 B
    themes_tw = {k: [str(c) for c in v] for k, v in (config.load("themes").get("tw") or {}).items()}  # 只用手動族群，避免研究題材的後見之明
    lead_map = config.us_lead()
    us_hist = yahoo.fetch_many(sorted(lead_map), range_=range_)
    log.info("美股領先指標：%d/%d 檔有資料", len(us_hist), len(lead_map))
    prepped = {c: bt.prepare(d, base) for c, d in data.items() if len(d) > 80}
    fr = early.frames(prepped)
    mkt = bt.market_ok(index_df)
    lead = early.lead_signals(us_hist, lead_map, themes_tw, fr)
    lag = early.laggard_signals(themes_tw, fr)
    pre = early.prebreak_signals(fr)
    rsl = early.rsline_signals(fr, index_df["close"])
    count = lambda sig: sum(len(v) for v in sig.values())  # noqa: E731
    log.info("訊號數：領先 %d、補漲 %d、預突破 %d、RS新高 %d", count(lead), count(lag), count(pre), count(rsl))

    quality = early.forward_stats(early.merge(early.base_signals(fr), lead, lag, pre, rsl), fr, mkt)
    print("【訊號品質：隔天開盤買進後的平均報酬（不受口數、持倉數限制）】")
    print(quality.to_string(index=False))
    out = research.RESEARCH_DIR / "backtest"
    out.mkdir(parents=True, exist_ok=True)
    (out / "early_signals.json").write_text(quality.to_json(orient="records", force_ascii=False, indent=1), encoding="utf-8")

    lead_lo = early.lead_signals(us_hist, lead_map, themes_tw, fr, pct_scale=0.7)
    lead_hi = early.lead_signals(us_hist, lead_map, themes_tw, fr, pct_scale=1.5)
    lag_rs = early.laggard_signals(themes_tw, fr, min_rs=0.5)
    cases = {
        "B 現行（收盤突破＋拉回）": ({}, None),
        "B＋美股領先": ({}, lead),
        "B＋美股領先（門檻×0.7）": ({}, lead_lo),
        "B＋美股領先（門檻×1.5）": ({}, lead_hi),
        "B＋題材補漲": ({}, lag),
        "B＋題材補漲（相對強度≥50%）": ({}, lag_rs),
        "B＋領先＋補漲": ({}, early.merge(lead, lag)),
        "B＋預突破": ({}, pre),
        "B＋RS新高": ({}, rsl),
        "B＋全部提前訊號": ({}, early.merge(lead, lag, pre, rsl)),
        "領先排在突破前面（B＋領先＋補漲）": ({"extra_first": True}, early.merge(lead, lag)),
        "只做美股領先": ({"setups": ()}, lead),
        "只做題材補漲": ({"setups": ()}, lag),
        "只做預突破": ({"setups": ()}, pre),
        "只做RS新高": ({"setups": ()}, rsl),
        "盤中突破取代收盤突破（拉回照做）": ({"setups": ("拉回",), "intraday": True}, None),
        "盤中突破（前一天相對強度前20%）": ({"setups": ("拉回",), "intraday": True, "intraday_min_rs": 0.8}, None),
        "盤中突破（前一天相對強度前20%）＋領先＋補漲": ({"setups": ("拉回",), "intraday": True, "intraday_min_rs": 0.8},
                                         early.merge(lead, lag)),
    }
    rows = _early_table(cases, base, data, futures, index_df, rates)
    table = pd.DataFrame(rows)
    print("\n【組合回測】")
    print(table.drop(columns=["各型態"]).to_string(index=False))
    for r in rows:
        print(f"・{r['策略']}：{r['各型態']}")
    (out / "early.json").write_text(table.to_json(orient="records", force_ascii=False, indent=1), encoding="utf-8")


def _backtest_eventgroup(data, futures, index_df, rates, capital: float, range_: str) -> None:
    """事件族群進場規則回測（trade/event_group.py；使用者 2026-11-27 要求，上線前先回測）。

    事件日＝同一族群（手動族群）同日 ≥3 檔（大族群 ≥25%）漲 ≥5%，收盤產生訊號、隔天開盤進場，只對有股期的股票出訊號：
      補漲＝當天漲幅 −1%～+5%（還沒跟上）、追價＝當天漲幅 ≥5% 且沒有（日 K 近似的）鎖死漲停。
    1) 事件日統計  2) 訊號品質（隔天開盤買進後 1／3／5／10 日；大盤多頭日 vs 全部日；對照『全部股票』基準）
    3) 組合回測（同一套停損、2% 風險、最多 3 檔、前後半段）  4) 低軌衛星個案（5/18 前後、6/12 SpaceX 掛牌前後的逐日漲幅與事件日明細）
    寫到 research/event_group_backtest.md 與 research/backtest/event_group*.json。「有事件或研究」這一條無法回頭重現，所以回測只驗證價量部分。
    """
    from .trade import backtest as bt
    from .trade import early, event_group
    base = bt.Params(capital=capital, exit_ma="ma20")  # 策略 B
    themes_tw = {k: [str(c) for c in v] for k, v in (config.load("themes").get("tw") or {}).items()}
    prepped_all = {c: bt.prepare(d, base) for c, d in data.items() if len(d) > 80}
    fr = early.frames(prepped_all)
    mkt = bt.market_ok(index_df)
    all_days = pd.Series(True, index=fr["close"].index)
    # 組合回測只放有股期的股票（和其他回測的標的一致）；事件偵測用整個族群
    fut_codes = {c for c in data if (futures.get(c) or {}).get("std") or (futures.get(c) or {}).get("mini")}
    data_fut = {c: d for c, d in data.items() if c in fut_codes}
    sig = {
        "補漲": event_group.signals(themes_tw, fr, futures, tiers=(event_group.TIER_LAG,)),
        "追價": event_group.signals(themes_tw, fr, futures, tiers=(event_group.TIER_CHASE,)),
        "補漲＋站上60日線": event_group.signals(themes_tw, fr, futures, tiers=(event_group.TIER_LAG,), trend=True),
        "追價＋站上60日線": event_group.signals(themes_tw, fr, futures, tiers=(event_group.TIER_CHASE,), trend=True),
    }
    count = lambda x: sum(len(v) for v in x.values())  # noqa: E731
    days_n = {k: len(v) for k, v in sig.items()}
    log.info("事件族群訊號數：%s；事件日 %s", {k: count(v) for k, v in sig.items()}, days_n)

    both = early.merge(sig["補漲"], sig["追價"])
    both_tr = early.merge(sig["補漲＋站上60日線"], sig["追價＋站上60日線"])
    # 對照組：同樣『有股期、漲 ≥5%、沒鎖死』但不在拉升族群裡（單獨一檔在漲）→ 看『族群一起動』有沒有額外價值
    solo = event_group.single_signals(themes_tw, fr, futures)
    solo_tr = event_group.single_signals(themes_tw, fr, futures, trend=True)
    log.info("單檔追價（對照）訊號數：%d；站上60日線 %d", count(solo), count(solo_tr))
    lines = [f"# 事件族群進場規則回測（{now_tw():%Y-%m-%d %H:%M}，資料範圍 {range_}）", "",
             "規則（trade/event_group.py）：族群同日 ≥3 檔（大族群 ≥25%）漲 ≥5% → 當天收盤產生訊號、隔天開盤進場；只對有股期的股票出訊號。"
             "補漲＝當天漲幅 −1%～+5%；追價＝當天漲幅 ≥5% 且不是鎖死漲停（日 K 近似：漲幅 ≥9.4% 且收在最高價）。"
             "停損 −1.5ATR、跌破 20 日線出場、2% 風險、最多 3 檔（策略 B）。", "",
             "限制：只驗證價量部分，『有事件或研究撐腰』無法回頭重現；族群成分股是用現在的清單回頭套（存活者偏差）；日 K 看不到盤中順序，"
             "實際是盤中就推、不一定等到收盤；樣本只有近 2 年、事件日有同一天多族群重疊的問題。", ""]
    quality = {}
    for title, mk in (("大盤多頭日", mkt), ("全部日子（含空頭／盤整）", all_days)):
        q = early.forward_stats(event_group.combine(both, solo), fr, mk)
        q_tr = early.forward_stats(event_group.combine(both_tr, solo_tr), fr, mk)
        q_tr["訊號"] = q_tr["訊號"].where(q_tr["訊號"].str.contains("基準"), q_tr["訊號"] + "（站上60日線）")
        q = pd.concat([q, q_tr[~q_tr["訊號"].str.contains("基準")]], ignore_index=True)
        quality[title] = q
        print(f"【訊號品質｜{title}：隔天開盤買進後的平均報酬（不受口數、持倉數限制）】")
        print(q.to_string(index=False))
        lines += [f"## 訊號品質｜{title}", "", _md_table(q), ""]
    out = research.RESEARCH_DIR / "backtest"
    out.mkdir(parents=True, exist_ok=True)
    (out / "event_group_signals.json").write_text(
        json.dumps({k: json.loads(v.to_json(orient="records", force_ascii=False)) for k, v in quality.items()},
                   ensure_ascii=False, indent=1), encoding="utf-8")

    # 現行每天約 7 檔突破訊號，額外訊號『排後面、只補空位』幾乎進不去（early2 已發現）→ 共用名額的列只留一個代表；
    # 真正的增量用『專用名額』（另外加 1 檔，不和突破搶位）來比，並附『B 4 檔』當對照（多一個位置本身的效果）。
    plus = {"extra_slots": 1}
    cases = {
        "B 現行（收盤突破＋拉回）": ({}, None),
        "B 4 檔（對照：多一個位置本身的效果）": ({"max_pos": 4}, None),
        "B＋事件族群（共用名額，只補空位）": ({}, both),
        "B 3 檔＋事件族群專用 1 檔（補漲）": (plus, sig["補漲"]),
        "B 3 檔＋事件族群專用 1 檔（追價）": (plus, sig["追價"]),
        "B 3 檔＋事件族群專用 1 檔（補漲＋追價）": (plus, both),
        "B 3 檔＋事件族群專用 1 檔（追價，站上60日線）": (plus, sig["追價＋站上60日線"]),
        "B 3 檔＋事件族群專用 1 檔（補漲＋追價，站上60日線）": (plus, both_tr),
        "B 3 檔＋單檔追價專用 1 檔（對照：不在拉升族群）": (plus, solo),
        "只做事件族群補漲": ({"setups": ()}, sig["補漲"]),
        "只做事件族群追價": ({"setups": ()}, sig["追價"]),
        "只做事件族群（補漲＋追價）": ({"setups": ()}, both),
        "只做事件族群（站上60日線）": ({"setups": ()}, both_tr),
        "只做單檔追價（對照：不在拉升族群）": ({"setups": ()}, solo),
        "事件族群排在突破前面": ({"extra_first": True}, both),
    }
    rows = _early_table(cases, base, data_fut, futures, index_df, rates)
    table = pd.DataFrame(rows)
    print("\n【組合回測】")
    print(table.drop(columns=["各型態"]).to_string(index=False))
    for r in rows:
        print(f"・{r['策略']}：{r['各型態']}")
    (out / "event_group.json").write_text(table.to_json(orient="records", force_ascii=False, indent=1), encoding="utf-8")
    lines += ["## 組合回測（同一套停損、2% 風險、最多 3 檔；前後半段各自回測）", "",
              "讀法：『共用名額，只補空位』那列和 B 現行完全一樣，是因為現行每天約 7 檔突破訊號，排在後面的額外訊號根本進不去，"
              "不代表事件族群沒效果；要看增量請看『專用 1 檔』列，對照『B 4 檔』（多一個位置本身的效果）；"
              "『單檔追價』是對照組（同樣有股期、漲 ≥5%、沒鎖死，但不在拉升族群內）。", "",
              _md_table(table.drop(columns=["各型態"])), ""]
    lines += [f"- {r['策略']}：{r['各型態']}" for r in rows] + [""]

    sat = themes_tw.get("低軌衛星") or []
    if sat:
        names = {c: (listings_name(c)) for c in sat}
        evs = event_group.theme_events("低軌衛星", sat, fr, futures)
        win = []
        for start, end, label in (("2026-05-14", "2026-05-22", "5/18 前後"), ("2026-06-10", "2026-06-18", "6/12 SpaceX 掛牌前後"),
                                  ("2026-11-20", "2026-11-27", "11/25～11/26 FCC 批准前後")):
            w = event_group.window_table(sat, fr, start, end)
            if w:
                win.append((label, w))
        lines += ["## 低軌衛星個案（8 檔：" + "、".join(f"{c}{names[c]}" for c in sat) + "）", ""]
        for label, w in win:
            lines += [f"### {label}：成分股逐日漲幅（%）與規則是否觸發", "", _md_table(pd.DataFrame(w)), ""]
            print(f"\n【低軌衛星｜{label}】")
            print(pd.DataFrame(w).to_string(index=False))
        summ = event_group.summarize_events(evs) if evs else []
        if summ:
            lines += ["### 低軌衛星事件日統計：有股期的成分股隔天開盤買進（沒扣成本、也沒扣『鎖死漲停買不到』）", "",
                      _md_table(pd.DataFrame(summ)), "",
                      "『每一波第一天』＝與前一個事件日相隔超過 7 個日曆天才算新的一波；連續幾天的事件日高度重疊，全部平均會把同一波算很多次。", ""]
            print("\n【低軌衛星｜事件日統計】")
            print(pd.DataFrame(summ).to_string(index=False))
            (out / "event_group_satellite.json").write_text(json.dumps({"統計": summ, "事件日": evs}, ensure_ascii=False, indent=1),
                                                            encoding="utf-8")
        lines += ["### 事件日明細（≥3 檔漲 ≥5%）：有股期的成分股隔天開盤買進後的平均報酬", "",
                  (_md_table(pd.DataFrame(evs)) if evs else "沒有符合的事件日。"), ""]
        print("\n【低軌衛星｜事件日明細】")
        print(pd.DataFrame(evs).to_string(index=False) if evs else "沒有符合的事件日")
    p = research.ROOT / "research" / "event_group_backtest.md"
    p.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n已寫入 {p}")


def _md_table(df: pd.DataFrame) -> str:
    """DataFrame → Markdown 表格（不依賴 tabulate）。"""
    cols = [str(c) for c in df.columns]

    def cell(v) -> str:
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return ""
        return str(v).replace("|", "／").replace("\n", " ")
    rows = ["| " + " | ".join(cols) + " |", "|" + "|".join(["---"] * len(cols)) + "|"]
    rows += ["| " + " | ".join(cell(v) for v in r) + " |" for r in df.itertuples(index=False)]
    return "\n".join(rows)


def listings_name(code: str) -> str:
    """回測報告用的股票名稱（抓不到就空字串）。"""
    try:
        return (_listings().get(code) or {}).get("name", "")
    except Exception:  # noqa: BLE001
        return ""


def _early_table(cases: dict, base, data, futures, index_df, rates) -> list[dict]:
    """逐組情境回測：全期間＋前後半段。cases：{名稱: (Params 覆寫, 額外訊號)}。"""
    from .trade import backtest as bt
    cut = index_df.index[len(index_df) // 2]
    warm = cut - pd.Timedelta(days=300)
    first = ({c: d[d.index < cut] for c, d in data.items()}, index_df[index_df.index < cut])
    second = ({c: d[d.index >= warm] for c, d in data.items()}, index_df[index_df.index >= warm])
    rows = []
    for name, (kw, ex) in cases.items():
        par = bt.Params(**{**base.__dict__, **kw})
        full = bt.run(data, futures, index_df, par, rates, extra=ex)
        a = bt.run(first[0], futures, first[1], par, rates, extra=ex)
        b = bt.run(second[0], futures, second[1], par, rates, extra=ex)
        by = "；".join(f"{k} {v['筆數']}筆 勝率{v['勝率']:.0f}% R{v['平均R']:+.2f}" for k, v in full["各型態"].items())
        rows.append({"策略": name, "報酬%": full["報酬率%"], "年化%": full["年化%"], "最大回撤%": full["最大回撤%"],
                     "筆數": full["交易筆數"], "勝率%": full["勝率%"], "平均R": full["平均R"],
                     "前半段%": a["報酬率%"], "後半段%": b["報酬率%"], "翻倍": full["翻倍日期"], "各型態": by})
        log.info("%s：%s", name, rows[-1])
    return rows


def _backtest_early2(data, futures, index_df, rates, capital: float, range_: str) -> None:
    """提前進場第二輪：第一輪發現「現行每天約 7 檔突破訊號，額外訊號只補空位根本進不去」，所以改用
    ① 額外訊號專用名額（另外加 1 檔，不和突破搶位）② 加相對強度／52 週高點濾網 ③ 盤中突破加強度門檻 來比。"""
    from .trade import backtest as bt
    from .trade import early
    base = bt.Params(capital=capital, exit_ma="ma20")
    themes_tw = {k: [str(c) for c in v] for k, v in (config.load("themes").get("tw") or {}).items()}
    lead_map = config.us_lead()
    us_hist = yahoo.fetch_many(sorted(lead_map), range_=range_)
    prepped = {c: bt.prepare(d, base) for c, d in data.items() if len(d) > 80}
    fr = early.frames(prepped)
    L = lambda **kw: early.lead_signals(us_hist, lead_map, themes_tw, fr, **kw)  # noqa: E731
    lead, lead50, lead80 = L(), L(min_rs=0.5), L(min_rs=0.8)
    lead_hi = L(need_hi=True)
    lead_rs50 = L(min_rs=0.5, score_rs=True)
    lag50, lag80 = early.laggard_signals(themes_tw, fr, min_rs=0.5), early.laggard_signals(themes_tw, fr, min_rs=0.8)
    plus = {"extra_slots": 1}
    cases = {
        "B 現行（3 檔）": ({}, None),
        "B 4 檔（對照：多一個位置本身的效果）": ({"max_pos": 4}, None),
        "只做領先": ({"setups": ()}, lead),
        "只做領先（相對強度≥50%）": ({"setups": ()}, lead50),
        "只做領先（相對強度≥80%）": ({"setups": ()}, lead80),
        "只做領先（離52週高10%內）": ({"setups": ()}, lead_hi),
        "只做領先（相對強度≥50%、依自身強度排序）": ({"setups": ()}, lead_rs50),
        "只做補漲（相對強度≥50%）": ({"setups": ()}, lag50),
        "只做補漲（相對強度≥80%）": ({"setups": ()}, lag80),
        "B 3 檔＋領先專用 1 檔": (plus, lead),
        "B 3 檔＋領先專用 1 檔（相對強度≥50%）": (plus, lead50),
        "B 3 檔＋領先專用 1 檔（相對強度≥80%）": (plus, lead80),
        "B 3 檔＋領先專用 1 檔（離52週高10%內）": (plus, lead_hi),
        "B 3 檔＋補漲專用 1 檔（相對強度≥50%）": (plus, lag50),
        "B 3 檔＋領先＋補漲專用 1 檔（相對強度≥50%）": (plus, early.merge(lead50, lag50)),
        "B＋盤中突破（前一天相對強度前20%）": ({"intraday": True, "intraday_min_rs": 0.8}, None),
        "B＋盤中突破（前一天相對強度前10%）": ({"intraday": True, "intraday_min_rs": 0.9}, None),
    }
    rows = _early_table(cases, base, data, futures, index_df, rates)
    table = pd.DataFrame(rows)
    print(table.drop(columns=["各型態"]).to_string(index=False))
    for r in rows:
        print(f"・{r['策略']}：{r['各型態']}")
    out = research.RESEARCH_DIR / "backtest"
    out.mkdir(parents=True, exist_ok=True)
    (out / "early2.json").write_text(table.to_json(orient="records", force_ascii=False, indent=1), encoding="utf-8")


def _backtest_launch(data, futures, index_df, rates, capital: float, range_: str) -> None:
    """短線發動機率研究（trade/launch.py）：條件分組發動率、既有訊號對照、合成分數的樣本外提升倍數。"""
    from .trade import backtest as bt
    from .trade import early, launch
    base = bt.Params(capital=capital, exit_ma="ma20")
    themes_tw = {k: [str(c) for c in v] for k, v in (config.load("themes").get("tw") or {}).items()}
    lead_map = config.us_lead()
    us_hist = yahoo.fetch_many(sorted(lead_map), range_=range_)
    prepped = {c: bt.prepare(d, base) for c, d in data.items() if len(d) > 80}
    fr = early.frames(prepped)
    mkt = bt.market_ok(index_df)
    lead = early.lead_signals(us_hist, lead_map, themes_tw, fr)
    df = launch.dataset(fr, launch.feature_frames(fr, index_df, themes_tw, lead), launch.label_frame(fr), mkt)
    log.info("樣本 %d 列（%d 檔、%d 天），基準發動率 A %.1f%%、B %.1f%%", len(df), df["code"].nunique(), df["date"].nunique(),
             df["hit"].mean() * 100, df["hit_r"].mean() * 100)
    labels = {"A": ("固定 +7%", "hit"), "B": ("+2R（R＝1.5×ATR，已扣掉波動大小）", "hit_r")}
    res = {"定義": f"t 日收盤後的條件 → t+1 開盤買進，{launch.HORIZON} 個交易日內先碰到目標、且之前沒碰到停損"
                   f"（開盤價 − {launch.ATR_MULT}×ATR；同日兩者都碰到算沒發動）。只算大盤多頭。A＝目標 +{launch.UP:.0%}；B＝目標 +2R。"
                   "平均R＝先到目標 +2、先停損 −1、都沒到依收盤折算（−1～+2）。",
           "樣本數": int(len(df)), "股票數": int(df["code"].nunique()), "交易日數": int(df["date"].nunique()),
           "基準平均5日報酬%": round(float(df["fwd"].mean()) * 100, 2), "基準平均R": round(float(df["rr"].mean()), 2), "標籤": {}}
    for key, (title, col) in labels.items():
        res["標籤"][key] = {"說明": title, "基準發動率%": round(float(df[col].mean()) * 100, 1),
                          "單一條件": launch.univariate(df, col=col), "既有訊號": launch.signal_rates(df, col=col),
                          "評估": launch.evaluate(df, col=col)}
    out = research.RESEARCH_DIR / "backtest"
    out.mkdir(parents=True, exist_ok=True)
    (out / "launch.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(res["定義"])
    print(f"樣本 {res['樣本數']:,} 列｜{res['股票數']} 檔｜{res['交易日數']} 天｜基準平均5日 {res['基準平均5日報酬%']}%｜基準平均R {res['基準平均R']}")
    for key, r in res["標籤"].items():
        print(f"\n==================== 標籤 {key}：{r['說明']}｜基準發動率 {r['基準發動率%']}% ====================")
        print("【單一條件：分 5 等分（由差異大到小）】")
        print(pd.DataFrame(r["單一條件"]).drop(columns=["各組下限"]).to_string(index=False))
        print("\n【既有訊號】")
        print(pd.DataFrame(r["既有訊號"]).to_string(index=False))
        ev = r["評估"]
        print(f"\n【合成分數（切點 {ev['切點']}；前半 {ev['前半段筆數']:,} 列、後半 {ev['後半段筆數']:,} 列；基準發動率 "
              f"{ev['前半段基準發動率%']}% / {ev['後半段基準發動率%']}%；基準平均R {ev['前半段基準平均R']} / {ev['後半段基準平均R']}）】")
        for kind, runs in ev["模型"].items():
            for name, x in runs.items():
                print(f"\n▶ {kind}｜{name}")
                print(pd.DataFrame(x["提升表"]).to_string(index=False))
                if "現行突破群的提升表" in x:
                    print("  （只看現行放量突破那群）")
                    print(pd.DataFrame(x["現行突破群的提升表"]).to_string(index=False))
                print(pd.DataFrame(x["校準"]).to_string(index=False))


def _backtest_volprice(data, futures, index_df, rates, capital: float, range_: str) -> None:
    """題材群「量增價未漲」預警回測（trade/volprice.py）：比放量突破早一步盯上第一波，值不值得？"""
    from .trade import backtest as bt
    from .trade import early, launch, volprice
    base = bt.Params(capital=capital, exit_ma="ma20")
    themes_tw = {k: [str(c) for c in v] for k, v in (config.load("themes").get("tw") or {}).items()}
    for name, meta in research.load_index().items():  # 研究完的題材整群納入（成分＝該研究列出的台股）
        cs = [str(x) for x in meta.get("codes", [])]
        if len(cs) >= 3:
            themes_tw.setdefault(f"研究:{name}", cs)
    prepped = {c: bt.prepare(d, base) for c, d in data.items() if len(d) > 80}
    fr = early.frames(prepped)
    labels = launch.label_frame(fr)
    mkt = bt.market_ok(index_df)
    res = volprice.run(fr, mkt, themes_tw, labels)
    res["定義"] = volprice.__doc__.strip()
    out = research.RESEARCH_DIR / "backtest"
    out.mkdir(parents=True, exist_ok=True)
    (out / "volprice.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"題材 {res['題材數']} 個、成分股 {res['成分股數']} 檔｜基準 F0（站上60日線、近20日≤15%、未突破、大盤多頭）：{res['基準F0']}")
    print("\n【訊號 vs 對照（沒放量）vs 基準】")
    print(pd.DataFrame(res["組合"]).to_string(index=False))
    print("\n【前半段／後半段（時間切半）】")
    print(pd.DataFrame(res["前後半"]).to_string(index=False))


def _backtest_launch2(data, futures, index_df, rates, capital: float, range_: str) -> None:
    """把『發動機率分數』放進策略：逐季向前訓練（不偷看未來），每天取分數最高的前幾檔當進場訊號，
    和現行策略 B、B＋領先專用位做組合回測（只比有分數的後段期間）。回答：分數高的那群，實際交易會不會比較賺。"""
    from .trade import backtest as bt
    from .trade import early, launch
    base = bt.Params(capital=capital, exit_ma="ma20")
    themes_tw = {k: [str(c) for c in v] for k, v in (config.load("themes").get("tw") or {}).items()}
    lead_map = config.us_lead()
    us_hist = yahoo.fetch_many(sorted(lead_map), range_=range_)
    prepped = {c: bt.prepare(d, base) for c, d in data.items() if len(d) > 80}
    fr = early.frames(prepped)
    mkt = bt.market_ok(index_df)
    lead = early.lead_signals(us_hist, lead_map, themes_tw, fr)
    lead80 = early.lead_signals(us_hist, lead_map, themes_tw, fr, min_rs=0.8)
    df = launch.dataset(fr, launch.feature_frames(fr, index_df, themes_tw, lead), launch.label_frame(fr), mkt)
    sc_r = launch.walk_forward_scores(df, "hit_r", "linear", 5.0)
    sc_a = launch.walk_forward_scores(df, "hit", "linear", 5.0)
    if sc_r.empty:
        print("資料不足，無法逐季向前訓練")
        return
    t_start = pd.Timestamp(sc_r["date"].min())
    log.info("逐季向前分數：%d 列，自 %s 起", len(sc_r), t_start.date())
    ex = {"R": {n: launch.top_signals(sc_r, n) for n in (1, 3)}, "A": {3: launch.top_signals(sc_a, 3)}}
    warm = t_start - pd.Timedelta(days=300)
    d2 = {c: d[d.index >= warm] for c, d in data.items()}
    i2 = index_df[index_df.index >= warm]
    plus = {"extra_slots": 1}
    cases = {
        "B 現行（3 檔）": ({}, None),
        "B 4 檔（對照）": ({"max_pos": 4}, None),
        "只做高分（+2R 模型，每天前 1 檔）": ({"setups": ()}, ex["R"][1]),
        "只做高分（+2R 模型，每天前 3 檔）": ({"setups": ()}, ex["R"][3]),
        "只做高分（+7% 模型，每天前 3 檔）": ({"setups": ()}, ex["A"][3]),
        "B 3 檔＋高分專用 1 檔（+2R 模型，前 3 檔）": (plus, ex["R"][3]),
        "B 3 檔＋高分專用 1 檔（+7% 模型，前 3 檔）": (plus, ex["A"][3]),
        "B 3 檔＋領先專用 1 檔（對照，同期間）": (plus, lead),
        "B 3 檔＋領先專用 1 檔（相對強度≥80%，對照）": (plus, lead80),
    }
    rows = []
    for name, (kw, e) in cases.items():
        par = bt.Params(**{**base.__dict__, **kw})
        full = bt.run(d2, futures, i2, par, rates, extra=e)
        # 只統計 t_start 之後進場的交易，並用權益曲線從 t_start 起算
        eq = pd.Series([v for _, v in full["curve"]], index=pd.to_datetime([d for d, _ in full["curve"]]))
        eq = eq[eq.index >= t_start.tz_localize(None) if t_start.tzinfo else eq.index >= t_start]
        ratio = eq / float(eq.iloc[0]) if len(eq) else eq
        trades = [x for x in full["trades"] if x["entry_date"] >= str(t_start.date())]
        by: dict[str, list] = {}
        for x in trades:
            by.setdefault(x["setup"], []).append(x)
        det = "；".join(f"{k} {len(v)}筆 勝率{sum(y['pnl'] > 0 for y in v) / len(v):.0%} R{sum(y['r'] for y in v) / len(v):+.2f}"
                       for k, v in by.items())
        rows.append({"策略": name, "期間報酬%": round((float(ratio.iloc[-1]) - 1) * 100, 1) if len(ratio) else None,
                     "最大回撤%": round(float((ratio / ratio.cummax() - 1).min()) * 100, 1) if len(ratio) else None,
                     "筆數": len(trades), "平均R": round(sum(x["r"] for x in trades) / len(trades), 2) if trades else None,
                     "勝率%": round(sum(x["pnl"] > 0 for x in trades) / len(trades) * 100, 1) if trades else None, "各型態": det})
        log.info("%s：%s", name, rows[-1])
    table = pd.DataFrame(rows)
    print(f"比較期間：{t_start.date()} 起（逐季向前訓練的分數）")
    print(table.drop(columns=["各型態"]).to_string(index=False))
    for r in rows:
        print(f"・{r['策略']}：{r['各型態']}")
    out = research.RESEARCH_DIR / "backtest"
    out.mkdir(parents=True, exist_ok=True)
    (out / "launch2.json").write_text(table.to_json(orient="records", force_ascii=False, indent=1), encoding="utf-8")


def cmd_backtest(args) -> None:
    """股票期貨波段策略回測（只做多），結果存到 research/backtest/。"""
    from . import net
    from .trade import backtest as bt
    listings = _listings()
    futures = stock_futures.load_stock_futures()
    codes = [c for c, v in futures.items() if c in listings and not c.startswith("00") and (v.get("std") or v.get("mini"))]
    if getattr(args, "eventgroup", False):  # 事件族群：要看整個族群同日拉升（很多成分股沒有股期），只用手動族群、避免研究題材的後見之明
        extra = {str(c) for v in (config.load("themes").get("tw") or {}).values() for c in v}
        codes = sorted(set(codes) | {x for x in extra if x in listings and not x.startswith("00")})
    if getattr(args, "volprice", False):  # 量增價未漲：標的要涵蓋題材成分股和所有研究過的股票（很多小型題材股沒有股票期貨）
        extra = set(config.all_tw_theme_codes())
        for e in research.load_index().values():
            extra.update(str(x) for x in e.get("codes", []))
        codes = sorted(set(codes) | {x for x in extra if x in listings and not x.startswith("00")})
    log.info("回測標的：%d 檔（有股票期貨或小型股期%s）", len(codes), "；加上題材成分股與研究過的股票" if getattr(args, "volprice", False) else "")
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
    if args.highwin:
        _backtest_highwin(data, futures, index_df, rates, args.capital)
        return
    if args.maexit:
        _backtest_maexit(data, futures, index_df, rates, args.capital)
        return
    if args.gate:
        _backtest_gate(data, futures, index_df, rates, args.capital)
        return
    if args.exceptions:
        _backtest_exceptions(data, futures, index_df, rates, args.capital)
        return
    if args.daytrade:
        _backtest_daytrade(data, futures, index_df, rates, args.capital)
        return
    if args.factors:
        _backtest_factors(data, futures, index_df, rates, args.capital)
        return
    if args.speed:
        _backtest_speed(data, futures, index_df, rates, args.capital)
        return
    if args.early:
        _backtest_early(data, futures, index_df, rates, args.capital, args.range)
        return
    if args.early2:
        _backtest_early2(data, futures, index_df, rates, args.capital, args.range)
        return
    if getattr(args, "eventgroup", False):
        _backtest_eventgroup(data, futures, index_df, rates, args.capital, args.range)
        return
    if args.launch:
        _backtest_launch(data, futures, index_df, rates, args.capital, args.range)
        return
    if args.volprice:
        _backtest_volprice(data, futures, index_df, rates, args.capital, args.range)
        return
    if args.launch2:
        _backtest_launch2(data, futures, index_df, rates, args.capital, args.range)
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
        _futures_commands()
    if args.explain:
        listings = _listings()
        print(_trade_explain(args.explain, listings, stock_futures.load_stock_futures(), 100_000))
        return
    want_pre = args.pre or (args.auto and paper.is_weekday(now) and TRADE_PRE <= f"{now:%H:%M}" < "09:00"
                            and done.get("pre") != today)
    want_post = args.post or (args.auto and paper.is_weekday(now) and f"{now:%H:%M}" >= TRADE_POST
                              and done.get("post") != today)
    if args.lead:
        push_lead_list(_listings(), stock_futures.load_stock_futures(), force=True)
        return
    if args.overnight:
        env_text, _ = overnight.build()
        print(env_text or "開盤前環境已關閉（config/settings.yaml 的 premarket.enabled）")
        print(overnight.diagnose())  # 資料診斷只印在日誌，不推播
        paper.push(env_text)
        return
    if want_pre:
        from .trade import real
        paper.push(paper.run_pre(overnight.build()) + "\n\n" + real.positions_text(real.load()))
        mark("pre")
        try:  # 美股昨夜領先指標 → 台股還沒跟上的對應股（失敗不影響盤前計劃）
            push_lead_list(_listings(), stock_futures.load_stock_futures())
        except Exception as e:  # noqa: BLE001
            log.warning("盤前領先名單失敗：%s", e)
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
        section += _market_stress_section(rst, closes, today)
        msg = section + "\n\n━━━━ 以下為模擬帳戶（對照用）━━━━\n" + (msg or "")
    paper.push(msg)
    mark("post")


def _market_stress_section(rst: dict, closes: dict, today: str) -> str:
    """盤後報告附的「市場流動性＋槓桿檢查」（巨人傑的做法；提醒用、不下單）。失敗只留警告，不影響其他推播。"""
    try:
        from .analysis import market_stress as ms
        from .trade import leverage as lv
        if not (ms.settings()["enabled"] and lv.settings()["enabled"]):
            return ""
        df = tw_daily.fetch_day_all()
        data_day = df.attrs.get("date") or today        # 資料自己的日期；若和今天不同（交易所還沒更新），照資料日期記，並在訊息裡標出來
        sday = ms.record(data_day, ms.day_stats(df))
        if data_day != today:
            sday["reasons"] = [f"⚠️ 行情資料日期是 {data_day}（不是 {today}，交易所可能還沒更新）"] + list(sday.get("reasons", []))
        eq = float(rst["equity"])
        peak = lv.update_peak(eq, float(rst["capital"]))
        notional, lev = lv.exposure(rst["positions"], closes, eq)
        lad = lv.ladder(eq, float(rst["capital"]), peak, sday["level"])
        return "\n\n" + ms.text(sday) + "\n\n" + lv.text(lad, eq, notional, lev)
    except Exception as e:  # noqa: BLE001
        log.warning("市場流動性／槓桿檢查失敗：%s", e)
        return ""


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


def _futures_live() -> list[dict]:
    """存檔裡進行中（或剛結束但還沒寫進紀錄）的日盤／夜盤場次的最新快照。讀不到就略過。"""
    from .trade import futures_day
    live = []
    for session in ("day", "night"):
        try:
            raw = futures_day._read_all(futures_day.STATE_PATH).get("today" if session == "day" else "night") or {}
            if raw.get("entries"):
                live.append({"date": raw["date"], "session": session,
                             "summary": futures_day.snapshot(futures_day.load_state(raw["date"], session=session), futures_day.settings(session))})
        except Exception as e:  # noqa: BLE001
            log.warning("讀取 %s 即時狀態失敗：%s", session, e)
    return live


def _futures_commands() -> None:
    """微台機器人收到的文字（只接受自己的訊息）：績效、今天、明細、說明。沒有設定專屬的微台機器人金鑰就不處理。"""
    from .trade import futures_log
    token = notify._clean_token(os.environ.get("TELEGRAM_FUTURES_BOT_TOKEN"))
    if not token:
        return
    offset = research.load_offset(token)
    updates = notify.get_updates(FUTURES_CHANNEL, offset)
    owner = notify.owner_chat(FUTURES_CHANNEL)
    texts = []
    for u in updates:
        offset = max(offset or 0, int(u.get("update_id", 0)) + 1)
        m = u.get("message") or {}
        if owner and str((m.get("chat") or {}).get("id")) != str(owner):
            continue
        if m.get("text"):
            texts.append(m["text"])
    research.save_offset(token, offset)
    if updates:       # 有收到才記一行，方便從日誌確認機器人有沒有讀到訊息（沒訊息的輪次不洗日誌）
        log.info("微台機器人：讀到 %d 則更新，其中 %d 則是本人的文字", len(updates), len(texts))
    if not texts:
        return
    data, now, live = futures_log.load(), now_tw(), _futures_live()
    for text in texts:
        notify.send(futures_log.reply(text, data, now, live), channel=FUTURES_CHANNEL)


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
    for text in [t for raw in msgs for t in real.expand(raw)]:
        cmd = real.parse(text)
        if not cmd:
            notify.send("看不懂這則回報。格式例如：亞泥 35.7 1口、小型金像電 1145 1口、賣 亞泥 36.5 1口、持倉、說明",
                        channel="trade")
            continue
        if cmd["cmd"] == "need_qty":
            notify.send(f"收到 {cmd['query']} 價位 {cmd['price']:,g}，但沒看到口數，請再回報一次，"
                        f"例如：{cmd['query']} {cmd['price']:,g} 1口", channel="trade")
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
        if cmd["cmd"] == "remove":
            hit = real.resolve(cmd["query"], listings, futures)
            typed_contract = cmd["query"].upper() if hit and hit[2] == cmd["query"].upper() else None
            _, msg = real.remove(st, hit[0], contract=typed_contract) if hit else (False, None)
            notify.send(msg or f"持倉裡找不到「{cmd['query']}」，沒有刪除任何記錄", channel="trade")
            continue
        hit = real.resolve(cmd["query"], listings, futures, cmd["mini"], cmd.get("std", False))
        if not hit:
            notify.send(f"找不到「{cmd['query']}」的股票期貨，請打股票名稱、代號或契約代碼（例如 亞泥、1102、DYF；小型契約：小型亞泥）",
                        channel="trade")
            continue
        code, name, contract, mult = hit
        if cmd["side"] == "sell":
            held = next((x for x in st["positions"] if x["code"] == code), None)
            if held and not any(x["contract"] == contract for x in st["positions"]):
                contract = held["contract"]  # 賣的時候用實際持有的契約，不必再分一般／小型
            notify.send(real.sell(st, contract, name, cmd["price"], cmd["qty"]), channel="trade")
            continue
        if cmd.get("replace"):  # 更正：先刪掉今天記錯的那筆，再記新的
            removed, gone = real.remove(st, code, today_only=True, prefer_not=contract)
            notify.send(gone or "（今天沒有這檔的舊記錄可刪，直接記新的）", channel="trade")
            if gone and not removed:  # 同一檔有多個契約又判斷不出來：不刪也不記，請使用者指定
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
        out = real.buy(st, code, name, contract, mult, cmd["price"], cmd["qty"], a, rate)
        info = futures.get(code) or {}
        if contract == info.get("mini") and info.get("std") and not cmd["mini"] and not cmd.get("std"):
            out += f"\n（{name} 有小型契約，預設記成小型 {contract}；要記一般契約 {info['std']} 請打「一般{name} 價位 口數」）"
        notify.send(out, channel="trade")
    real.save(st)


def cmd_macro(args) -> None:
    """國際原物料、匯率、利率與風險指標：更新快照（約每 15 分鐘）、單日大幅異動推新聞機器人。"""
    snap = macro.load()
    if not args.force and snap.get("fetched"):
        try:
            t = datetime.strptime(snap["fetched"], "%Y-%m-%d %H:%M").replace(tzinfo=now_tw().tzinfo)
            if now_tw() - t < timedelta(minutes=args.every):
                return
        except ValueError:
            pass
    rows = macro.snapshot()
    if not rows:
        log.warning("國際行情一筆都沒抓到")
        return
    changed = macro.save_if_changed(rows)
    print(macro.table_text(rows))
    if not changed and snap:
        return
    if not args.no_push and snap:  # 第一次只存快照，不推（避免一開始就把今天已發生的異動全推一遍）
        for m in macro.alerts(rows):
            notify.send(m, channel="news")


def cmd_check(args) -> None:
    """資料檢查：印出各資料來源的原始欄位，不推播。"""
    try:
        print(txf.probe())
    except Exception as e:  # noqa: BLE001
        print(f"台指期日盤報價檢查失敗：{e}")
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
    rs.add_argument("--trial", help="試跑：研究這個題材但只印在紀錄裡（不存檔不推播），搭配環境變數 RESEARCH_MODEL 比較模型")
    rs.add_argument("--trial-trigger", default="", help="試跑時給的觸發新聞文字（和正式研究一樣放進提示）")
    rs.add_argument("--commands", action="store_true", help="處理 Telegram「研究 XXX」指令")
    rs.add_argument("--inbox", action="store_true", help="從頻道新貼文偵測新題材並研究")
    rs.add_argument("--auto", action="store_true", help="從今天的重點新聞偵測新題材並研究")
    rs.add_argument("--auto-slots", action="store_true", help="監看用：到 14:05、21:05 時自動加上 --auto")
    rs.set_defaults(func=cmd_research)
    cd = sub.add_parser("card", help="推播題材卡（圖片＋目標價＋股票期貨），例如：card 玻纖布")
    cd.add_argument("query")
    cd.add_argument("--channel", default="research")
    cd.set_defaults(func=cmd_card)
    ig = sub.add_parser("infographic", help="研究資訊圖（HTML 排版截圖，只輸出圖檔）")
    ig.add_argument("--topic", required=True, help="研究題材名稱（可只填部分文字）")
    ig.add_argument("--out", default="infographic_out", help="輸出資料夾")
    ig.set_defaults(func=cmd_infographic)
    bk = sub.add_parser("backtest", help="股票期貨波段策略回測（只做多）")
    bk.add_argument("--capital", type=float, default=100_000)
    bk.add_argument("--range", default="3y")
    bk.add_argument("--variants", action="store_true", help="比較多組參數（含前後半段樣本外檢查）")
    bk.add_argument("--highwin", action="store_true", help="高勝率版本比較")
    bk.add_argument("--maexit", action="store_true", help="20日線出場從第幾天開始檢查")
    bk.add_argument("--gate", action="store_true", help="進場品質門檻回測")
    bk.add_argument("--exceptions", action="store_true", help="強勢股 1 口例外（風險上限放寬）回測")
    bk.add_argument("--daytrade", action="store_true", help="當沖（日K近似）vs 現行波段")
    bk.add_argument("--factors", action="store_true", help="看對的條件：依進場前條件分組比較勝率與平均 R")
    bk.add_argument("--early", action="store_true", help="提前進場訊號：美股領先、題材補漲、預突破、RS 新高、盤中突破 vs 現行")
    bk.add_argument("--volprice", action="store_true", help="題材群量增價未漲預警：比放量突破早一步盯上第一波的發動率與平均 R")
    bk.add_argument("--launch2", action="store_true", help="發動機率分數放進策略：逐季向前訓練、每天取前幾檔，和 B 比組合回測")
    bk.add_argument("--launch", action="store_true", help="短線發動機率研究：條件分組發動率、合成分數的樣本外提升倍數")
    bk.add_argument("--early2", action="store_true", help="提前進場第二輪：專用名額、強度濾網、盤中突破強度門檻")
    bk.add_argument("--eventgroup", action="store_true", help="事件族群進場規則：族群同日 ≥3 檔漲 ≥5% 後的補漲／追價，訊號品質、組合回測、低軌衛星個案")
    bk.add_argument("--speed", action="store_true", help="翻倍速度：不同風險／持倉數、不同起點，多久翻倍與回撤")
    bk.set_defaults(func=cmd_backtest)
    tr = sub.add_parser("trade", help="進出場機器人（模擬）")
    tr.add_argument("--auto", action="store_true", help="常駐監看用：到時間才執行")
    tr.add_argument("--pre", action="store_true", help="立即推盤前計劃")
    tr.add_argument("--lead", action="store_true", help="立即推盤前領先名單（美股昨夜大漲 → 台股還沒跟上的對應股）")
    tr.add_argument("--overnight", action="store_true", help="立即推開盤前環境（台指期夜盤、美股收盤、那指期貨、亞股早盤＋風險燈號）")
    tr.add_argument("--post", action="store_true", help="立即盤後結算")
    tr.add_argument("--commands", action="store_true", help="處理機器人收到的成交回報")
    tr.add_argument("--explain", help="檢查某檔股票的訊號與口數，例如：--explain 國巨")
    tr.set_defaults(func=cmd_trade)
    fl = sub.add_parser("inflow", help="盤後資金流入族群 → 研究機器人題材卡")
    fl.add_argument("--top", type=int, default=3)
    fl.set_defaults(func=cmd_inflow)
    sg = sub.add_parser("signals", help="早期訊號：論壇／RSS／供應鏈關鍵字 → 偵測供應鏈換料、轉單、淘汰 → 推播＋排求證研究")
    sg.add_argument("--dry", action="store_true", help="只印出偵測結果，不推播、不存記錄、不排研究")
    sg.add_argument("--latest", type=int, default=0, help="測試：PTT 每個看板直接看最新 N 篇（不看已讀記錄）")
    sg.add_argument("--every", type=int, default=10, help="距離上次掃描幾分鐘內就略過")
    sg.add_argument("--text", help="偵錯：直接檢查一段文字（不連網、不推播）")
    sg.add_argument("--relay", action="store_true", help="搭配 --text：當成使用者轉傳（放寬條件）")
    sg.add_argument("--probe", nargs="+", metavar="URL", help="測試：只看這些網址雲端機房讀不讀得到（不偵測、不推播）")
    sg.add_argument("--browser", action="store_true", help="搭配 --probe：再用無頭瀏覽器（Playwright）讀一次")
    sg.set_defaults(func=cmd_signals)
    pcp = sub.add_parser("podcasts", help="Podcast：新集數 → 下載、轉成逐字稿 → 值得看的段落放進待檢視清單（不推播）")
    pcp.add_argument("--list", action="store_true", help="只列出各節目 RSS 最新幾集（不下載）")
    pcp.add_argument("--dry", action="store_true", help="下載並轉文字，但只印出結果，不存逐字稿、不放待檢視清單、不排研究")
    pcp.add_argument("--show", help="只處理名稱含這段文字的節目")
    pcp.add_argument("--max-episodes", type=int, default=None, help="這次最多轉幾集（一般模式預設 2；--backfill 預設不限，靠 --budget-min 收工）")
    pcp.add_argument("--max-minutes", type=int, default=0, help="只轉到第幾分鐘為止（測試用）")
    pcp.add_argument("--from-minutes", type=int, default=0, help="搭配 --max-minutes：從第幾分鐘開始轉（測試用）")
    pcp.add_argument("--first", type=int, default=1, help="第一次看到的節目，最新幾集要處理（其他舊集數略過）")
    pcp.add_argument("--force", action="store_true", help="不看已處理記錄，直接處理每個節目最新一集（測試用，不更新記錄）")
    pcp.add_argument("--model", help="語音辨識模型（預設用設定檔 podcast_model）")
    pcp.add_argument("--threads", type=int, default=0, help="辨識用的 CPU 執行緒數（0＝全部）")
    pcp.add_argument("--plan", action="store_true", help="歷史集數：印出總集數、總時數、各年份與節目簡介裡的網址（不下載）")
    pcp.add_argument("--backfill", action="store_true", help="歷史集數：由舊到新轉成加密逐字稿＋摘要（已有摘要的略過，可重複執行）")
    pcp.add_argument("--shard", type=int, default=0, help="--backfill：這是第幾片（從 0 開始）")
    pcp.add_argument("--shards", type=int, default=1, help="--backfill：總共分幾片（依集數順序輪流分配）")
    pcp.add_argument("--budget-min", type=int, default=0, help="--backfill：最多跑幾分鐘，剩下的時間不夠轉下一集就收工（0＝不限）")
    pcp.add_argument("--min-ep", type=int, default=0, help="--backfill：只處理第幾集（含）以後的集數，分片也只在這些集數裡分")
    pcp.add_argument("--eps", default="", help="--backfill：只處理這些集數（例 428-473,475,500-503）；已有摘要但記的公鑰指紋和現在這把不同的也會重轉（換金鑰時用）")
    pcp.add_argument("--newest-first", action="store_true", help="--backfill：這片由新到舊轉（搭配 --min-ep，另開一批片加速）")
    pcp.set_defaults(func=cmd_podcasts)
    th = sub.add_parser("threads", help="追蹤指定 Threads 帳號的新貼文（與他們回覆的原貼文）：不推播，供應鏈訊號排求證研究，值得看的貼文放進待檢視清單")
    th.add_argument("--dry", action="store_true", help="只印出讀到的貼文與偵測結果，不推播、不存記錄、不排研究")
    th.add_argument("--latest", type=int, default=0, help="測試：每個帳號直接看最新 N 則（不看已讀記錄）")
    th.add_argument("--every", type=int, default=5, help="距離上次掃描幾分鐘內就略過")
    th.add_argument("--accounts", nargs="+", metavar="HANDLE", help="覆蓋設定檔的帳號（測試用）")
    th.set_defaults(func=cmd_threads)
    ch = sub.add_parser("channels", help="檢查公開 Telegram 頻道新貼文並推播")
    ch.add_argument("--latest", type=int, default=0, help="測試：直接推最新 N 則（不更新已讀記錄）")
    ch.add_argument("--research", action="store_true", help="搭配 --latest：這幾則也送進題材研究收件匣（補漏用）")
    ch.set_defaults(func=cmd_channels)

    rt = sub.add_parser("realtime", help="盤中即時監控")
    rt.add_argument("--once", action="store_true", help="只跑一次")
    rt.add_argument("--force", action="store_true", help="非交易時段也抓報價")
    rt.add_argument("--interval", type=int, help="報價更新秒數")
    rt.add_argument("--until", help="到這個時間（HH:MM，台北時間）自動結束，例如 13:35")
    rt.set_defaults(func=cmd_realtime)

    fn = sub.add_parser("futures-night", help="台指期夜盤（15:00–隔天 05:00）日內順勢突破訊號（分段執行，狀態存 state/）")
    fn.add_argument("--minutes", type=float, default=0, help="這一段最多跑幾分鐘（0＝跑到這一場收盤）")
    fn.add_argument("--interval", type=int, help="報價更新秒數（預設 20）")
    fn.add_argument("--once", action="store_true", help="只抓一次")
    fn.add_argument("--force", action="store_true", help="不管時段與是否已收盤都執行（測試用）")
    fn.add_argument("--no-push", action="store_true", help="不推播，改印在畫面上")
    fn.add_argument("--status", action="store_true", help="只印 run／skip：現在該不該啟動夜盤監控")
    fn.set_defaults(func=cmd_futures_night)
    fl = sub.add_parser("futures-log", help="印出微台計畫帳（日盤＋夜盤）觸發紀錄與每場損益（state/futures_log.md）")
    fl.add_argument("--rebuild", action="store_true", help="用 state/futures_log.json 重排 md 再印")
    fl.set_defaults(func=cmd_futures_log)
    sb_ = sub.add_parser("research-scoreboard", help="研究成績單：每篇研究點名的個股、研究日之後相對大盤的表現（寫 research/scoreboard.md）")
    sb_.add_argument("--out", default="", help="輸出檔（預設 research/scoreboard.md）")
    sb_.add_argument("--control", type=int, default=150, help="等權重對照組的股票檔數（沒被研究點名的上市櫃隨機樣本，0＝不算）")
    sb_.set_defaults(func=cmd_research_scoreboard)
    fr_ = sub.add_parser("futures-report", help="微台計畫帳績效推播（累計損益、勝率、最近幾場；交易機器人）")
    fr_.add_argument("--dry", action="store_true", help="只印出、不推播")
    fr_.add_argument("--last", type=int, default=8, help="列出最近幾場（預設 8）")
    fr_.add_argument("--view", choices=["perf", "month", "year"], default="perf", help="perf＝累計績效（預設）、month＝每月結算、year＝年度績效表")
    fr_.set_defaults(func=cmd_futures_report)
    hl_ = sub.add_parser("health", help="系統健康日報：排程有沒有跑、資料是否過期、夜盤有沒有中斷（推播一則）")
    hl_.add_argument("--dry", action="store_true", help="只印出、不推播")
    hl_.add_argument("--channel", default="market", help="推播頻道（預設 market）")
    hl_.set_defaults(func=cmd_health)
    ms_ = sub.add_parser("market-stress", help="市場流動性／恐慌指標（跌停家數、量縮…），更新 state/market_stress.json")
    ms_.add_argument("--day", default="", help="日期 YYYY-MM-DD（預設用加權指數最近一個交易日）")
    ms_.set_defaults(func=cmd_market_stress)
    pw_ = sub.add_parser("panic-watch", help="恐慌日研究清單：研究過、受惠、價格回落的個股＋催化與風險（寫 research/panic_watch.md）")
    pw_.add_argument("--drop", type=float, default=20.0, help="相對研究日或研究後高點回落多少 %% 才列入（預設 20）")
    pw_.add_argument("--min-yoy", type=float, default=20.0, help="最新月營收年增至少多少 %% 才列入（股價回落但營收仍成長；預設 20，填 -999 不篩）")
    pw_.add_argument("--top", type=int, default=40, help="最多列幾檔（依回落幅度排序，預設 40）")
    pw_.add_argument("--out", default="", help="輸出檔（預設 research/panic_watch.md）")
    pw_.set_defaults(func=cmd_panic_watch)

    nw = sub.add_parser("news", help="掃描新聞與公告")
    nw.add_argument("--limit", type=int, default=50)
    nw.add_argument("--min-score", type=float, default=None)
    nw.add_argument("--push", action="store_true", help="推播新出現的新聞與漲價信（常駐監看用）")
    nw.set_defaults(func=cmd_news)

    mc = sub.add_parser("macro", help="國際原物料、匯率、利率與風險指標（快照＋異動推播）")
    mc.add_argument("--every", type=int, default=15, help="距離上次更新幾分鐘內就略過")
    mc.add_argument("--force", action="store_true")
    mc.add_argument("--no-push", action="store_true")
    mc.set_defaults(func=cmd_macro)

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
