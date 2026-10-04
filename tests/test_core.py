"""離線測試：用樣本資料驗證解析與計算邏輯（不連網）。"""
from datetime import datetime

import json
import numpy as np
import pandas as pd
import pytest

from market_intel.analysis import news_signals, sector_flow, target_price
from market_intel.fetchers import news, taifex, tw_daily, tw_realtime, yahoo
from market_intel.utils import TW_TZ, pick, to_float


def test_to_float():
    assert to_float("1,234.5") == 1234.5
    assert to_float("+3.20") == 3.2
    assert to_float("-0.5") == -0.5
    assert to_float("--") is None
    assert to_float("X0.00") == 0.0
    assert to_float(None) is None


def test_pick_strips_keys():
    assert pick({"主旨 ": "漲價"}, "主旨") == "漲價"
    assert pick({"PutCallOIRatio%": 1}, "OIRatio") == 1


def test_parse_mis():
    payload = {"msgArray": [
        {"c": "2330", "n": "台積電", "ex": "tse", "z": "1005.00", "y": "1000.00", "o": "1001", "h": "1010",
         "l": "998", "v": "20000", "t": "10:30:00", "b": "1004.00_1003.00_", "a": "1005.00_1006.00_"},
        {"c": "6488", "n": "環球晶", "ex": "otc", "z": "-", "y": "400", "v": "1000", "t": "10:30:00",
         "b": "399.5_399_", "a": "400.5_401_"},
    ]}
    q = {x.code: x for x in tw_realtime.parse_mis(payload)}
    assert q["2330"].price == 1005.0
    assert q["2330"].change_pct == pytest.approx(0.5)
    assert q["2330"].turnover == pytest.approx(20000 * 1000 * 1005)
    assert q["6488"].price == 400.0  # 無成交時取買賣中價


def test_channels_for():
    ch = tw_realtime.channels_for(["2330", "6488", "9999"], {"2330": {"market": "tse"}, "6488": {"market": "otc"}})
    assert ch == ["tse_2330.tw", "otc_6488.tw", "tse_9999.tw", "otc_9999.tw"]


def test_parse_twse_and_tpex_day_all():
    twse = tw_daily.parse_twse_day_all([{"Code": "2330", "Name": "台積電", "TradeVolume": "30,000,000",
                                        "TradeValue": "30,000,000,000", "OpeningPrice": "990", "HighestPrice": "1010",
                                        "LowestPrice": "985", "ClosingPrice": "1000.00", "Change": "+10.00", "Transaction": "1"}])
    assert twse.iloc[0]["close"] == 1000 and twse.iloc[0]["change"] == 10 and twse.iloc[0]["market"] == "tse"
    tpex = tw_daily.parse_tpex_day_all([{"SecuritiesCompanyCode": "6488", "CompanyName": "環球晶", "Close": "400",
                                        "Change": "-2.5", "Open": "402", "High": "405", "Low": "398",
                                        "TradingShares": "1,000,000", "TransactionAmount": "400,000,000"}])
    assert tpex.iloc[0]["value"] == 4e8 and tpex.iloc[0]["market"] == "otc"


def test_parse_institutional_and_sector():
    t86 = {"stat": "OK", "fields": ["證券代號", "證券名稱", "外陸資買進股數(不含外資自營商)", "外陸資賣出股數(不含外資自營商)",
                                    "外陸資買賣超股數(不含外資自營商)", "投信買賣超股數", "自營商買賣超股數", "三大法人買賣超股數"],
           "data": [["2330  ", "台積電", "10", "5", "1,000,000", "-20,000", "5,000", "985,000"]]}
    df = tw_daily.parse_institutional(t86)
    assert df.iloc[0]["code"] == "2330" and df.iloc[0]["foreign"] == 1_000_000 and df.iloc[0]["trust"] == -20_000
    bf = {"stat": "OK", "fields": ["分類指數名稱", "成交股數", "成交金額", "成交筆數", "漲跌指數"],
          "data": [["半導體類指數", "1,000", "300,000,000,000", "1", "10"], ["航運類指數", "1,000", "20,000,000,000", "1", "-1"]]}
    s = tw_daily.parse_sector_turnover(bf)
    assert list(s["sector"]) == ["半導體", "航運"] and s.iloc[0]["value"] == 3e11


def _ohlc(closes, vol=1000):
    c = pd.Series(closes, dtype=float)
    idx = pd.date_range("2026-01-01", periods=len(c), freq="B")
    return pd.DataFrame({"open": c.values, "high": (c * 1.01).values, "low": (c * 0.99).values,
                         "close": c.values, "volume": vol}, index=idx)


def test_target_breakout():
    closes = list(np.linspace(100, 110, 50)) + [100 + (i % 5) for i in range(30)] + [115]
    res = target_price.compute(_ohlc(closes), "TEST")
    assert res.last == 115
    assert "箱型突破測距" in res.targets
    assert res.conservative and res.conservative > 115
    assert res.aggressive >= res.conservative
    assert res.stop and res.stop < 115
    assert res.risk_reward and res.risk_reward > 0


def test_target_downtrend_and_pe():
    closes = list(np.linspace(200, 120, 80))
    res = target_price.compute(_ohlc(closes), "DOWN", eps=10, target_pe=15)
    assert res.trend == "空頭"
    assert "反彈 0.382" in res.targets
    assert res.targets["本益比 15 倍"] == 150


def test_target_needs_history():
    with pytest.raises(ValueError):
        target_price.compute(_ohlc([1, 2, 3]), "X")


def test_expected_fraction():
    assert sector_flow.expected_fraction(datetime(2026, 10, 1, 8, 30, tzinfo=TW_TZ)) == 0
    assert sector_flow.expected_fraction(datetime(2026, 10, 1, 9, 30, tzinfo=TW_TZ)) == pytest.approx(0.25)
    assert sector_flow.expected_fraction(datetime(2026, 10, 1, 14, 0, tzinfo=TW_TZ)) == 1.0


def test_theme_flow_intraday_ranks_inflow_first():
    Q = tw_realtime.Quote
    quotes = {
        "A": Q("A", "甲", "tse", 110, 100, 100, 111, 99, 3000, "10:00"),   # 族群一：大漲爆量
        "B": Q("B", "乙", "tse", 55, 50, 50, 56, 49, 2000, "10:00"),
        "C": Q("C", "丙", "tse", 99, 100, 100, 101, 98, 100, "10:00"),     # 族群二：量縮小跌
    }
    listings = {"A": {"prev_value": 100e6}, "B": {"prev_value": 50e6}, "C": {"prev_value": 100e6}}
    now = datetime(2026, 10, 1, 10, 0, tzinfo=TW_TZ)
    df = sector_flow.theme_flow_intraday(quotes, {"熱門": ["A", "B"], "冷門": ["C"]}, listings, now)
    assert df.iloc[0]["族群"] == "熱門"
    assert df.iloc[0]["判讀"].startswith("資金流入")
    assert df.iloc[1]["判讀"] in ("量縮下跌", "量縮")


def test_theme_flow_daily_and_us():
    up = _ohlc(list(np.linspace(100, 110, 30)), vol=1000)
    up.loc[up.index[-1], "volume"] = 5000
    flat = _ohlc([50.0] * 30, vol=1000)
    df = sector_flow.theme_flow_daily({"A": up, "B": flat}, {"強": ["A"], "弱": ["B"]})
    assert df.iloc[0]["族群"] == "強" and df.iloc[0]["量比(對5日均)"] > 4
    us = sector_flow.us_etf_flow({"SMH": up, "XLU": flat}, {"SMH": "半導體", "XLU": "公用事業"})
    assert us.iloc[0]["ETF"] == "SMH"


def test_official_sector_flow():
    mk = lambda a, b: pd.DataFrame({"sector": ["半導體", "航運"], "value": [a, b], "volume": [1, 1]})
    out = sector_flow.official_sector_flow([("d0", mk(80, 20)), ("d1", mk(60, 40)), ("d2", mk(60, 40))])
    assert out.iloc[0]["類股"] == "半導體" and out.iloc[0]["比重變化(百分點)"] == 20


def test_institutional_by_theme():
    inst = pd.DataFrame([{"code": "A", "name": "甲", "foreign": 1e6, "trust": 0, "dealer": 0, "total": 1e6}])
    df = sector_flow.institutional_by_theme(inst, {"A": 100.0}, {"T": ["A"]})
    assert df.iloc[0]["外資(億)"] == 1.0


def test_news_parsers_and_signals():
    rss = """<rss><channel><item><title>台積電 3奈米 喊漲 10% 漲價信已發出</title><link>http://x/1</link>
             <pubDate>Wed, 01 Oct 2026 01:00:00 GMT</pubDate><description>&lt;b&gt;摘要&lt;/b&gt;</description></item>
             <item><title>某公司 砍單 下修財測</title><link>http://x/2</link></item></channel></rss>"""
    items = news.parse_rss(rss, "test")
    assert len(items) == 2 and items[0].summary == "摘要"
    ranked = news_signals.rank(items, {"min_score": 1, "categories": {
        "漲價": {"weight": 3, "words": ["漲價", "喊漲"]}, "利空": {"weight": -2, "words": ["砍單", "下修"]}}},
        {"台積電": "2330"})
    assert ranked[0].codes == ["2330"] and ranked[0].score == 3
    assert ranked[1].score == -2

    cn = news.parse_cnyes({"items": {"data": [{"newsId": 1, "title": "t", "publishAt": 1790000000,
                                                "market": [{"code": "2330", "name": "台積電"}], "stock": ["3017-TW"]}]}})
    assert cn[0].codes == ["2330", "3017"] and cn[0].published.startswith("2026")

    mops = news.parse_mops([{"公司代號": "2330", "公司名稱": "台積電", "主旨 ": "公告調漲報價", "發言日期": "1151001", "發言時間": "083000"}], "上市")
    assert mops[0].codes == ["2330"] and "調漲報價" in mops[0].title

    atom = """<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>8-K - NVIDIA CORP (0001045810)</title>
              <link href="https://sec.gov/x"/><updated>2026-10-01T10:00:00-04:00</updated><summary>Item 2.02</summary></entry></feed>"""
    sec = news.parse_sec_atom(atom)
    assert sec[0].url == "https://sec.gov/x"


def test_yahoo_parse_chart():
    payload = {"chart": {"result": [{"meta": {"regularMarketPrice": 3}, "timestamp": [1790000000, 1790086400],
                                     "indicators": {"quote": [{"open": [1, 2], "high": [2, 3], "low": [1, 2],
                                                               "close": [None, 3], "volume": [10, 20]}]}}]}}
    df, meta = yahoo.parse_chart(payload)
    assert len(df) == 1 and df["close"].iloc[0] == 3 and meta["regularMarketPrice"] == 3
    assert yahoo.yahoo_symbol("6488", "otc") == "6488.TWO" and yahoo.yahoo_symbol("NVDA", None) == "NVDA"


def test_taifex_summary_tolerant_keys():
    data = {
        "put_call_ratio": [{"Date": "20260930", "PutCallVolumeRatio%": "95.1", "PutCallOIRatio%": "120.5"},
                           {"Date": "20261001", "PutCallVolumeRatio%": "90.0", "PutCallOIRatio%": "130.2"}],
        "institutional_futures": [{"Date": "20261001", "ContractCode": "TX", "Item": "外資",
                                   "OpenInterest(Net)(Contracts)": "-25,000"}],
    }
    s = taifex.summarize(data)
    assert s["pcr_oi"] == 130.2 and s["pcr_volume"] == 90.0
    assert s["tx_institutional_net_oi"]["外資"] == -25000


def test_news_code_tagging_ignores_years_and_excludes():
    n2c = {"國巨": "2327", "大成鋼": "2027", "台積電": "2330"}
    it = news_signals.tag_codes(news.NewsItem("t", "國巨(2327)漲停！AI大單鎖定2027年產能"), n2c)
    assert it.codes == ["2327"]
    it = news_signals.tag_codes(news.NewsItem("t", "美光預測 2027 財年再創新高"), n2c)
    assert it.codes == []
    it = news_signals.tag_codes(news.NewsItem("t", "2330 台積電 法說"), n2c)
    assert it.codes == ["2330"]
    kw = {"exclude_words": ["凍漲"], "categories": {"漲價": {"weight": 3, "words": ["漲價"]}}}
    assert news_signals.score_item(news.NewsItem("t", "瓦斯不漲價！中油宣布凍漲"), kw).score == 0


def test_dedupe_google_suffix():
    items = [news.NewsItem("鉅亨網", "國巨(2327)今日漲停、被動元件噴出！"),
             news.NewsItem("Google新聞[漲價]", "國巨(2327)今日漲停、被動元件噴出！ - news.cnyes.com"),
             news.NewsItem("Google新聞[漲價]", "國巨(2327)今日漲停、被動元件噴出！ - 鉅亨網")]
    assert len(news.dedupe(items)) == 1


def test_institutional_dealer_fallback():
    t86 = {"fields": ["證券代號", "證券名稱", "外陸資買賣超股數(不含外資自營商)", "投信買賣超股數", "三大法人買賣超股數"],
           "data": [["2330", "台積電", "1,000", "200", "1,500"]]}
    assert tw_daily.parse_institutional(t86).iloc[0]["dealer"] == 300


def test_label_flat_band():
    assert sector_flow.label(1.5, -0.2) == "放量平盤"
    assert sector_flow.label(1.5, 1.0) == "資金流入"


def test_dealer_not_confused_with_foreign_dealer():
    t86 = {"fields": ["證券代號", "證券名稱", "外陸資買賣超股數(不含外資自營商)", "外資自營商買賣超股數",
                      "投信買賣超股數", "自營商買賣超股數", "三大法人買賣超股數"],
           "data": [["2330", "台積電", "1,000", "0", "200", "300", "1,500"]]}
    assert tw_daily.parse_institutional(t86).iloc[0]["dealer"] == 300


def test_tag_codes_all_listed_codes():
    it = news_signals.tag_codes(news.NewsItem("t", "正淩(8147)AI機櫃供不應求"), {}, all_codes={"8147"})
    assert it.codes == ["8147"]


def test_build_summary(monkeypatch):
    from market_intel import cli
    monkeypatch.setenv("GITHUB_REPOSITORY", "me/repo")
    theme = pd.DataFrame([{"族群": "被動元件", "量比(對5日均)": 2.33, "加權漲跌%": 9.42, "判讀": "資金流入🔥", "資金增減(億)": 442.1},
                          {"族群": "IC設計", "量比(對5日均)": 0.55, "加權漲跌%": 0.57, "判讀": "量縮", "資金增減(億)": -280.8}])
    inst = pd.DataFrame([{"族群": "被動元件", "三大法人合計(億)": 209.76, "外資(億)": 189.03, "投信(億)": 8.16}])
    fx = {"pcr_oi": 80.57, "tx_institutional_net_oi": {"外資及陸資": -78151.0, "投信": 73839.0}}
    items = [news.NewsItem("t", "國巨漲價", "http://x/1", codes=["2327"], score=5)]
    msg = cli.build_summary(datetime(2026, 10, 1, tzinfo=TW_TZ), theme, inst, fx)
    assert "被動元件" in msg and "-78,151口" in msg and "國巨漲價" not in msg
    digest = cli.build_news_digest(datetime(2026, 10, 1, tzinfo=TW_TZ), items)
    assert "[2327] 國巨漲價" in digest and "http://x/1" in digest
    assert "https://github.com/me/repo/blob/main/reports/2026-10-01.md" in msg


def test_find_tpex_material_path():
    swagger = {"paths": {
        "/mopsfin_t187ap03_O": {"get": {"summary": "上櫃公司基本資料"}},
        "/mopsfin_t187ap04_R": {"get": {"summary": "興櫃公司每日重大訊息"}},
        "/mopsfin_t187ap04_O": {"get": {"summary": "上櫃公司每日重大訊息"}},
    }}
    assert news.find_tpex_material_path(swagger) == "/mopsfin_t187ap04_O"
    assert news.find_tpex_material_path({"paths": {}}) is None


def test_intraday_pace_ignores_members_without_prev_value():
    Q = tw_realtime.Quote
    quotes = {"A": Q("A", "甲", "tse", 100, 100, 100, 100, 100, 1000, "10:00"),
              "B": Q("B", "乙", "otc", 100, 100, 100, 100, 100, 9000, "10:00")}
    now = datetime(2026, 10, 1, 13, 30, tzinfo=TW_TZ)
    df = sector_flow.theme_flow_intraday(quotes, {"T": ["A", "B"]}, {"A": {"prev_value": 100e6}}, now)
    assert df.iloc[0]["量能步調"] == 1.0


def test_notify_channels(monkeypatch):
    from market_intel import notify
    sent = []

    class R:
        ok = True

    monkeypatch.setattr(notify.requests, "post", lambda url, json, timeout: sent.append((url, json["chat_id"])) or R())
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "MAIN")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "111")
    monkeypatch.delenv("TELEGRAM_NEWS_BOT_TOKEN", raising=False)
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    notify.send("x", channel="news")
    assert "botMAIN" in sent[-1][0]  # 沒設新聞機器人 → 走主機器人
    monkeypatch.setenv("TELEGRAM_NEWS_BOT_TOKEN", "NEWS")
    notify.send("x", channel="news")
    assert "botNEWS" in sent[-1][0] and sent[-1][1] == "111"
    notify.send("x")
    assert "botMAIN" in sent[-1][0]


def test_stock_futures_parsers():
    from market_intel.fetchers import stock_futures
    row = lambda *cells: "<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>"  # noqa: E731
    html_text = "<table>" + "".join([
        "<tr><th>商品代碼</th><th>標的證券</th><th>證券代號</th></tr>",
        row("CD", "台灣積體電路製造股份有限公司", "2330", "台積電", "<span>●</span>", "是股票期貨標的", "●", "是股票選擇權標的",
            "2,000", "8:45~13:45", "17:25~次日05:00"),
        row("QF", "台灣積體電路製造股份有限公司", "2330", "台積電", "●", "是股票期貨標的", "", "", "100", "8:45~13:45", "-"),
        row("DH", "國巨股份有限公司", "2327", "國巨", "●", "是股票期貨標的", "", "", "2,000", "8:45~13:45", "-"),
        row("ZZ", "只有選擇權", "9999", "某股", "", "", "●", "是股票選擇權標的", "2,000", "8:45~13:45", "-"),
    ]) + "</table>"
    data = stock_futures.parse_stock_lists_html(html_text)
    assert data["2330"] == {"std": "CDF", "mini": "QFF", "night": True}
    assert data["2327"] == {"std": "DHF", "mini": None, "night": False}
    assert "9999" not in data
    assert stock_futures.label(data["2330"]) == "CDF／小型QFF／夜盤"
    assert stock_futures.label(data["2327"]) == "DHF"
    assert stock_futures.label(None) == "無"
    rows = [{"Contract": "CDF", "StockCode": "2330", "StockName": "台積電"}]
    assert stock_futures.parse_openapi_rows(rows)["2330"]["std"] == "CDF"
    sw = {"paths": {"/A": {"get": {"summary": "期貨每日行情"}}, "/B": {"get": {"summary": "股票期貨及選擇權交易標的"}}}}
    assert stock_futures.find_openapi_path(sw) == "/B"


def test_rank_picks():
    from market_intel.analysis import picks
    items = [news.NewsItem("t", "國巨漲價", codes=["2327"], score=5, tags=["漲價(漲價)"]),
             news.NewsItem("t", "某公司砍單", codes=["1111"], score=-2, tags=["利空(砍單)"])]
    nb = picks.news_by_code(items)
    assert nb["2327"]["hike"] and "1111" not in nb
    cands = [
        picks.Candidate("2327", "國巨", pct=9.9, ratio=3.0, inst=130, news_score=5, price_hike=True,
                        themes=["被動元件"], theme_inflow=True),
        picks.Candidate("2330", "台積電", pct=0.2, ratio=0.9, inst=90),
        picks.Candidate("1111", "弱勢", pct=-3, ratio=2.0),          # 大跌排除
        picks.Candidate("2222", "沒量沒題材", pct=1, ratio=1.0),     # 無資金無題材排除
    ]
    df = picks.rank_picks(cands, {"2327": {"std": "DHF", "mini": "QHF", "night": False}})
    assert list(df["代號"]) == ["2327", "2330"]
    assert df.iloc[0]["股票期貨"] == "DHF／小型QHF" and df.iloc[1]["股票期貨"] == "無"
    assert "漲價" in df.iloc[0]["理由"]
    msg = picks.picks_message(df, "🎯")
    assert "股期DHF／小型QHF" in msg and "無股期" in msg
    # 清單抓不到時標「未知」，不能誤標成「無」
    assert picks.rank_picks(cands, {}).iloc[0]["股票期貨"] == "未知"


def test_notify_picks_channel(monkeypatch):
    from market_intel import notify
    sent = []

    class R:
        ok = True

    monkeypatch.setattr(notify.requests, "post", lambda url, json, timeout: sent.append(url) or R())
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "MAIN")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "111")
    monkeypatch.setenv("TELEGRAM_PICKS_BOT_TOKEN", "PICKS")
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    notify.send("x", channel="picks")
    assert "botPICKS" in sent[-1]


def test_news_by_code_dilutes_roundups():
    from market_intel.analysis import picks
    roundup = news.NewsItem("t", "漲價潮整理", codes=["1111", "2222", "3333", "4444", "5555", "6666"],
                            score=3, tags=["漲價(漲價)"])
    single = news.NewsItem("t", "國巨漲價", codes=["2327"], score=3, tags=["漲價(漲價)"])
    nb = picks.news_by_code([roundup, single])
    assert nb["1111"]["score"] == 1.5 and not nb["1111"]["hike"]
    assert nb["2327"]["score"] == 3 and nb["2327"]["hike"]


def test_price_letter_detection_and_theme_hike():
    import yaml
    from market_intel.analysis import picks
    from market_intel.utils import ROOT
    kw = yaml.safe_load(open(ROOT / "config" / "news_keywords.yaml", encoding="utf-8"))
    media = news_signals.score_item(news.NewsItem("鉅亨網", "國巨(2327)發漲價信 MLCC 調漲報價10%", codes=["2327"]), kw)
    assert media.tags[0].startswith("漲價信") and media.score >= 8
    assert "被動元件" in media.themes
    official = news_signals.score_item(
        news.NewsItem("MOPS重大訊息(上市)", "2327 國巨：公告調整產品價格", codes=["2327"]), kw)
    assert official.tags[0].startswith("公司公告漲價") and news_signals.is_price_letter(official)
    no_hike = news_signals.score_item(news.NewsItem("MOPS重大訊息(上市)", "1234 某公司：本公司產品價格暫不調整"), kw)
    assert not news_signals.is_price_letter(no_hike)
    upstream = news_signals.score_item(news.NewsItem("Google新聞", "三星 DRAM 全面漲價 合約價調漲15%"), kw)
    assert news_signals.is_price_letter(upstream) and "記憶體" in upstream.themes

    media.codes = ["2327"]
    nb = picks.news_by_code([media, upstream], {"被動元件": ["2327", "2492"], "記憶體": ["2408"]})
    assert nb["2327"]["letter"] and nb["2327"]["hike"]
    assert nb["2492"]["theme_hike"] and not nb["2492"]["hike"]
    assert nb["2408"]["theme_hike"]
    df = picks.rank_picks([
        picks.Candidate("2327", "國巨", pct=5, ratio=2, news_score=8, price_hike=True, price_letter=True),
        picks.Candidate("2408", "南亞科", pct=1, ratio=1.3, theme_hike=True),
    ], {})
    assert df.iloc[0]["漲價"] == "漲價信" and "漲價信" in df.iloc[0]["理由"]
    assert df.iloc[1]["漲價"] == "族群" and "族群漲價" in df.iloc[1]["理由"]


def test_price_letter_needs_stock_or_theme():
    import yaml
    from market_intel.utils import ROOT
    kw = yaml.safe_load(open(ROOT / "config" / "news_keywords.yaml", encoding="utf-8"))
    phone = news_signals.score_item(news.NewsItem("Google新聞", "三星Galaxy售價全面調漲 最高貴8千"), kw)
    assert not news_signals.is_price_letter(phone)
    gas = news_signals.score_item(news.NewsItem("Google新聞", "中油：10月電業用戶天然氣調漲9.68%"), kw)
    assert gas.score == 0


def test_yahoo_alternate_symbol(monkeypatch):
    calls = []

    def fake(sym, range_="3mo", interval="1d"):
        calls.append(sym)
        if sym.endswith(".TW"):
            raise RuntimeError("404")
        return pd.DataFrame({"close": [1.0]}), {}

    monkeypatch.setattr(yahoo, "fetch_chart", fake)
    out = yahoo.fetch_many(["6488.TW"])
    assert "6488.TW" in out and calls == ["6488.TW", "6488.TWO"]


def test_only_new_dedupes_across_sources_and_scans(tmp_path, monkeypatch):
    monkeypatch.setattr(news, "SEEN_PATH", tmp_path / "seen.json")
    first = [news.NewsItem("鉅亨網", "國巨(2327)今日漲停、被動元件噴出！AI大單提前鎖定產能", "https://cnyes/1")]
    assert len(news.only_new(first)) == 1
    later = [
        # 同一則，被 Google 不同搜尋抓到、標題多了媒體名稱
        news.NewsItem("Google新聞[漲價 股]", "國巨(2327)今日漲停、被動元件噴出！AI大單提前鎖定產能 - news.cnyes.com", "https://g/1"),
        news.NewsItem("Google新聞[漲價信]", "國巨（2327）今日漲停、被動元件噴出！AI大單提前鎖定產能 - 鉅亨網", "https://g/2"),
        # 真的不同的新聞
        news.NewsItem("Google新聞[漲價信]", "ABF吃緊喊漲 南電創高 景碩破千", "https://g/3"),
    ]
    fresh = news.only_new(later)
    assert [it.url for it in fresh] == ["https://g/3"]
    assert news.only_new(later) == []  # 下一輪再出現也不會再推


def test_dedupe_similar_titles():
    items = [news.NewsItem("Google新聞[a]", "《價值型投資 最新產業研究報告》國巨 (2327-TW) 高階電容需求續強，漲價與轉單接力 - news.cnyes.com"),
             news.NewsItem("Google新聞[b]", "《價值型投資最新產業研究報告》國巨（2327） 高階電容需求續強，漲價與轉單接力／ 台股 - 鉅亨號"),
             news.NewsItem("Google新聞[c]", "美光第四季營收創歷史新高")]
    assert len(news.dedupe(items)) == 2


def test_mops_announcements_not_merged():
    items = [news.NewsItem("MOPS重大訊息(上市)", "2327 國巨：公告本公司董事會決議發放現金股利"),
             news.NewsItem("MOPS重大訊息(上市)", "2327 國巨：公告本公司董事會決議調整產品價格")]
    assert len(news.dedupe(items)) == 2


TG_HTML = """
<div class="tgme_widget_message_wrap js-widget_message_wrap"><div class="tgme_widget_message text_not_supported_wrap js-widget_message" data-post="Gooaye/1001">
<div class="tgme_widget_message_text js-message_text" dir="auto">MS 今日的光通 memo<br/><br/>- 3.2T 會是對中政策施力點<br/>- 如果被擠壓到，中國可能控制 inp 基板出口反制，聯亞(3081)</div>
<a class="tgme_widget_message_date" href="https://t.me/Gooaye/1001"><time datetime="2026-10-02T03:21:00+00:00" class="time">11:21</time></a></div></div>
<div class="tgme_widget_message_wrap js-widget_message_wrap"><div class="tgme_widget_message js-widget_message" data-post="Gooaye/1002">
<a class="tgme_widget_message_photo_wrap"></a><time datetime="2026-10-02T03:30:00+00:00"></time></div></div>
<div class="tgme_widget_message_wrap js-widget_message_wrap"><div class="tgme_widget_message js-widget_message" data-post="Gooaye/1003">
<div class="tgme_widget_message_text js-message_text" dir="auto">國巨 &amp; 華新科 MLCC 喊漲</div><time datetime="2026-10-02T04:00:00+00:00"></time></div></div>
"""


def test_telegram_channel_parse_and_state():
    from market_intel.fetchers import telegram_channel as tg
    posts = tg.parse_channel_html(TG_HTML, "Gooaye")
    assert [p.post_id for p in posts] == [1001, 1003]  # 純圖片貼文略過
    assert "3.2T" in posts[0].text and "\n" in posts[0].text
    assert posts[1].text == "國巨 & 華新科 MLCC 喊漲" and posts[0].url == "https://t.me/Gooaye/1001"
    state: dict = {}
    assert tg.new_posts("Gooaye", posts, state) == [] and state["Gooaye"] == 1003  # 第一次只記錄
    state["Gooaye"] = 1001
    assert [p.post_id for p in tg.new_posts("Gooaye", posts, state)] == [1003]
    assert state["Gooaye"] == 1003


def test_channel_post_message(monkeypatch):
    from market_intel import cli
    from market_intel.fetchers import telegram_channel as tg
    post = tg.parse_channel_html(TG_HTML, "Gooaye")[0]
    listings = {"3081": {"name": "聯亞"}, "4971": {"name": "英特磊"}, "2455": {"name": "全新"}}
    futures = {"3081": {"std": "XXF", "mini": None, "night": False}}
    themes = {"光通訊雷射InP": ["3081", "4971", "2455"]}
    msg = cli.channel_post_message("股癌", post, listings, futures, themes, {"聯亞": "3081", "英特磊": "4971"})
    assert "📣 股癌" in msg and "3081 聯亞｜股期 XXF" in msg
    assert "族群【光通訊雷射InP】3 檔；有股期：3081聯亞" in msg
    assert msg.rstrip().endswith("https://t.me/Gooaye/1001")
    # 「全新」是常用詞，不用名稱比對
    assert "全新" in cli.AMBIGUOUS_NAMES


def test_research_command_parsing():
    from market_intel import research
    assert research.parse_command("研究 玻纖布") == "玻纖布"
    assert research.parse_command("/研究：CoWoP 封裝") == "CoWoP 封裝"
    assert research.parse_command("/research HBM4") == "HBM4"
    assert research.parse_command("今天天氣不錯") is None
    updates = [
        {"update_id": 10, "message": {"chat": {"id": 111}, "text": "研究 石英布"}},
        {"update_id": 11, "message": {"chat": {"id": 999}, "text": "研究 別人的"}},  # 不是主人
        {"update_id": 12, "message": {"chat": {"id": 111}, "text": "hi"}},
    ]
    topics, offset = research.pending_commands(updates, "111", None)
    assert topics == ["石英布"] and offset == 13


def test_research_topic_end_to_end(tmp_path, monkeypatch):
    from market_intel import ai, research
    monkeypatch.setattr(research, "RESEARCH_DIR", tmp_path / "research")
    monkeypatch.setattr(research, "INDEX_PATH", tmp_path / "research" / "index.json")
    monkeypatch.setattr(research, "AUTO_THEMES_PATH", tmp_path / "research" / "auto_themes.yaml")
    monkeypatch.setattr(ai, "USAGE_PATH", tmp_path / "usage.json")
    monkeypatch.setattr(ai, "check_budget", lambda: None)

    def fake_research(system, prompt, usage):
        assert "石英布" in prompt and "台股公司一定要寫" in system
        usage.input_tokens, usage.output_tokens, usage.web_searches = 50000, 8000, 10
        usage.calls.append("research")
        return "# 石英布\n## 一句話重點\nM9 需要石英布，供不應求。"

    def fake_extract(instruction, text, schema, usage, label="extract"):
        usage.calls.append(label)
        return {"topic": "石英布", "one_line": "M9 板材需要石英布，供不應求", "status": "混合", "horizon": "中期（3-12個月）",
                "layers": [
                    {"layer": "玻纖布", "description": "石英布／Low-Dk 布", "global_players": ["Nittobo"],
                     "tw_stocks": [{"code": "1815", "name": "富喬", "role": "Low-Dk 玻纖布", "impact": "受惠"},
                                   {"code": "9999", "name": "不存在", "role": "?", "impact": "受惠"}]},
                    {"layer": "CCL", "description": "M9 板材", "global_players": [],
                     "tw_stocks": [{"code": "2383", "name": "台光電", "role": "M9 CCL", "impact": "受惠"}]}],
                "keywords": ["石英布", "Q布", "M9"], "catalysts": ["石英布擴產"], "risks": ["無布 HC 取代"],
                "sources": [{"title": "x", "url": "https://x"}]}

    monkeypatch.setattr(ai, "web_research", fake_research)
    monkeypatch.setattr(ai, "extract_json", fake_extract)
    listings = {"1815": {"name": "富喬"}, "2383": {"name": "台光電"}}
    futures = {"2383": {"std": "XXF", "mini": "YYF", "night": False}}
    out = research.research_topic("石英布", "手動指定", listings, futures)
    md = (tmp_path / "research" / out["file"]).read_text(encoding="utf-8")
    assert "| 玻纖布 | 1815 | 富喬 |" in md and "9999 不存在" in md  # 不存在的代號被剔除並註明
    assert "XXF／小型YYF" in md and "AI 研究" in md
    assert "▲ 2383 台光電" in out["message"] and "股期 XXF／小型YYF" in out["message"]
    idx = research.load_index()
    assert idx["石英布"]["codes"] == ["1815", "2383"]
    import yaml
    auto = yaml.safe_load((tmp_path / "research" / "auto_themes.yaml").read_text(encoding="utf-8"))
    assert auto["tw"]["研究:石英布"] == ["1815", "2383"] and "研究:石英布" in auto["topic_themes"]["Q布"]
    assert research.recently_researched("石英布", idx)
    usage = __import__("json").loads((tmp_path / "usage.json").read_text(encoding="utf-8"))
    assert list(usage.values())[0]["runs"] == 1


def test_research_budget_and_knowledge(tmp_path, monkeypatch):
    from market_intel import ai, research
    monkeypatch.setattr(ai, "USAGE_PATH", tmp_path / "usage.json")
    u = ai.Usage(input_tokens=1_000_000, output_tokens=1_000_000, web_searches=1000)
    assert u.usd() > 0
    ai.record_usage(u, "x")
    monkeypatch.setattr(ai, "settings", lambda: {"monthly_budget_usd": 1, "price_per_mtok": {"input": 4, "output": 20}})
    with pytest.raises(ai.AIUnavailable):
        ai.check_budget()
    # 研究庫的 PCB 材料筆記會被附進「玻纖布」的研究提示
    idx = research.load_index()
    assert "AI伺服器PCB材料" in idx
    assert "Glass Weave" in research.relevant_knowledge("玻纖布", idx)
    assert research.relevant_knowledge("寵物食品", idx) == ""


def test_command_offset_per_bot(tmp_path, monkeypatch):
    from market_intel import research
    monkeypatch.setattr(research, "COMMAND_STATE", tmp_path / "cmd.json")
    research.save_offset("111:SECRET", 900)
    research.save_offset("222:OTHER", 5)
    assert research.load_offset("111:SECRET") == 900 and research.load_offset("222:OTHER") == 5
    assert research.load_offset("333:NEW") is None  # 新機器人從頭讀，不沿用別的機器人的記錄
    assert "SECRET" not in (tmp_path / "cmd.json").read_text()


def test_publish_pending(tmp_path, monkeypatch):
    import json
    from market_intel import research
    monkeypatch.setattr(research, "RESEARCH_DIR", tmp_path)
    monkeypatch.setattr(research, "PENDING_DIR", tmp_path / "pending")
    monkeypatch.setattr(research, "INDEX_PATH", tmp_path / "index.json")
    monkeypatch.setattr(research, "AUTO_THEMES_PATH", tmp_path / "auto_themes.yaml")
    monkeypatch.setattr(research, "PUBLISHED_PATH", tmp_path / "published.json")
    monkeypatch.setattr(research, "QUEUE_DIR", tmp_path / "queue")
    (tmp_path / "pending").mkdir()
    d = {"topic": "測試題材", "one_line": "x", "status": "混合", "horizon": "中期（3-12個月）",
         "layers": [{"layer": "上游", "description": "", "global_players": [],
                     "tw_stocks": [{"code": "1815", "name": "富喬", "role": "紗布", "impact": "受惠"},
                                   {"code": "9999", "name": "假的", "role": "", "impact": "受惠"}]}],
         "keywords": ["Q布"], "catalysts": [], "risks": [], "sources": [], "report": "# 報告"}
    (tmp_path / "pending" / "a.json").write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    outs = research.publish_pending({"1815": {"name": "富喬"}}, {})
    assert len(outs) == 1 and "1815 富喬" in outs[0]["message"]
    assert not list((tmp_path / "pending").glob("*.json"))
    md = (tmp_path / outs[0]["file"]).read_text(encoding="utf-8")
    assert "9999" in md and "用量" not in md
    # 同一份研究檔（例如每輪從開發分支複製過來）只發布一次
    (tmp_path / "pending" / "a.json").write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    assert research.publish_pending({"1815": {"name": "富喬"}}, {}) == []
    assert not list((tmp_path / "pending").glob("*.json"))


def test_parse_request_kinds():
    from market_intel import research
    assert research.parse_request("研究 玻纖布") == ("research", "玻纖布")
    assert research.parse_request("題材 玻纖布") == ("card", "玻纖布")
    assert research.parse_request("/族群：CCL高速板材") == ("card", "CCL高速板材")
    assert research.parse_request("目標價 光通訊") == ("card", "光通訊")
    assert research.parse_request("早安") is None


def test_card_fut_text():
    from market_intel import theme_card
    assert theme_card.fut_text({"std": "LXF", "mini": "QEF", "night": False}) == "股期 LXF／小型 QEF"
    assert theme_card.fut_text({"std": "HBF", "mini": None, "night": True}) == "股期 HBF"
    assert theme_card.fut_text(None) == "無"
    assert theme_card.fut_text(None, known=False) == "未知"


def test_revenue_and_alerts_parse():
    from market_intel.fetchers import tw_daily
    rev = tw_daily.parse_revenue([{"資料年月": "11508", "公司代號": "1815", "營業收入-當月營收": "500000",
                                   "營業收入-去年同月增減(%)": "85.2", "營業收入-上月比較增減(%)": "6.1",
                                   "累計營業收入-前期比較增減(%)": "40"}])
    assert rev["1815"]["ym"] == "2026/08" and rev["1815"]["yoy"] == 85.2
    al = tw_daily.parse_alerts([{"Code": "2030", "DispositionPeriod": "115/10/01～115/10/07"}],
                               [{"SecuritiesCompanyCode": "8084", "DispositionPeriod": "1151002~1151008"}],
                               [{"Code": ""}], [{"SecuritiesCompanyCode": "3163"}], [{"Code": "2033"}], [])
    assert al["2030"] == ["處置 10/01~10/07"] and al["8084"] == ["處置 10/02~10/08"]
    assert al["3163"] == ["注意股"] and al["2033"] == ["注意累計將達處置"] and "" not in al


def test_notify_chunks_and_ads():
    from market_intel import notify
    from market_intel.fetchers import telegram_channel
    parts = notify._chunks("\n".join(["一二三四五"] * 2000), 4000)
    assert all(len(p) <= 4000 for p in parts) and len(parts) >= 3
    assert telegram_channel.is_ad("詳情請看資訊欄：立即領取1888幣", ["詳情請看資訊欄"])
    assert not telegram_channel.is_ad("MS 今日的光通 memo", ["詳情請看資訊欄"])


def test_find_for_codes(tmp_path, monkeypatch):
    from market_intel import research
    idx = tmp_path / "index.json"
    idx.write_text(json.dumps({"玻纖布": {"codes": ["1802", "1815", "5340", "5475", "2383"]}}), encoding="utf-8")
    monkeypatch.setattr(research, "INDEX_PATH", idx)
    assert research.find_for_codes(["1802", "1815", "5340", "5475"]) == "玻纖布"
    assert research.find_for_codes(["2330", "2454", "1802"]) is None


def test_chart_wrap():
    from market_intel.charts import _wrap
    out = _wrap("Low-Dk1/2、Low-CTE 認證，產線 4→12 條", 15, 2)
    assert "Low-\n" not in out and len(out.splitlines()) <= 2


def test_card_stock_row_since_research():
    import pandas as pd
    from market_intel import theme_card
    idx = pd.date_range("2026-06-01", periods=90, freq="B", tz="Asia/Taipei").tz_convert("UTC")
    close = pd.Series(range(100, 190), index=idx, dtype=float)
    df = pd.DataFrame({"open": close, "high": close * 1.01, "low": close * 0.99, "close": close, "volume": 1e6})
    last_day = f"{idx[-1].tz_convert('Asia/Taipei'):%Y-%m-%d}"
    row = theme_card.stock_row("8046", df, {"8046": {"std": "LYF", "mini": "QSF"}}, {}, {}, last_day)
    assert "since_research" not in row and row["close"] == 189.0 and row["target"]
    row = theme_card.stock_row("8046", df, {}, {}, {"8046": ["注意股"]},
                               f"{idx[-11].tz_convert('Asia/Taipei'):%Y-%m-%d}")
    assert round(row["since_research"], 1) == round((189 / 179 - 1) * 100, 1) and row["alerts"] == ["注意股"]
    assert row["fut"] == "未知"


def test_push_theme_cards_research_first(tmp_path, monkeypatch):
    import pandas as pd
    from market_intel import cli, config, notify, research, theme_card
    idx = tmp_path / "index.json"
    idx.write_text(json.dumps({"玻纖布與石英布": {"date": "2026-10-03", "codes": ["1802", "1815", "5340", "5475"],
                                                 "file": "x.md"}}), encoding="utf-8")
    monkeypatch.setattr(research, "INDEX_PATH", idx)
    monkeypatch.setattr(research, "QUEUE_DIR", tmp_path / "queue")
    monkeypatch.setattr(theme_card, "SENT_PATH", tmp_path / "sent.json")
    monkeypatch.setattr(config, "themes", lambda: {"tw": {"玻纖布": ["1802", "1815", "5340", "5475"],
                                                          "被動元件": ["2327", "2492"]}})
    monkeypatch.setattr(cli.ai, "available", lambda: False)
    cards, msgs = [], []
    monkeypatch.setattr(theme_card, "build", lambda found, listings, context="", futures=None: (found, context))
    monkeypatch.setattr(theme_card, "send", lambda card, channel="research": cards.append(card))
    monkeypatch.setattr(notify, "send", lambda text, channel="market": msgs.append((channel, text)))
    flow = pd.DataFrame([
        {"族群": "被動元件", "量比(對5日均)": 2.46, "資金增減(億)": 584.0, "加權漲跌%": 4.3, "判讀": "資金流入🔥"},
        {"族群": "玻纖布", "量比(對5日均)": 1.5, "資金增減(億)": 21.7, "加權漲跌%": 3.7, "判讀": "資金流入"},
        {"族群": "AI伺服器", "量比(對5日均)": 1.0, "資金增減(億)": 22.0, "加權漲跌%": -1.0, "判讀": "持平"},
    ])
    picks_df = pd.DataFrame([{"代號": "3163", "名稱": "波若威", "族群": "", "分數": 9.0, "理由": "量比6倍", "新聞": ""},
                             {"代號": "2330", "名稱": "台積電", "族群": "", "分數": 2.0, "理由": "", "新聞": ""}])
    cli.push_theme_cards(flow, picks_df, {}, "盤後")
    # 研究過的玻纖布 → 直接推題材卡（細項產業來自研究）；沒研究過的被動元件 → 只排入研究
    assert len(cards) == 1 and cards[0][0]["title"] == "玻纖布（研究：玻纖布與石英布）"
    queued = sorted(p.stem for p in (tmp_path / "queue").glob("*.json"))
    assert queued == sorted([research.slug("被動元件"), research.slug("波若威（3163）題材與產業")])
    assert msgs and msgs[0][0] == "research" and "被動元件" in msgs[0][1] and "波若威" in msgs[0][1]
    cli.push_theme_cards(flow, picks_df, {}, "盤後")  # 同一天不重複
    assert len(cards) == 1


def test_detail_slides(tmp_path):
    from market_intel import charts
    t = {"title": "比較", "columns": ["材料", "Dk", "定位"],
         "rows": [["FR-4", "4.0–4.5", "成熟便宜、強度高；高頻損耗大，用於一般電子產品"], ["PTFE", "2.0", "電性天花板"]],
         "note": "Dk 越低越好"}
    assert charts.spec_table_slide_png(tmp_path / "a.png", "PCB", t).stat().st_size > 1000
    evo = [{"name": "M8", "composition": "PPO", "spec": "Dk 3.3", "application": "AI"},
           {"name": "M9", "composition": "Q布", "spec": "Dk 3.1", "application": "Rubin"}]
    assert charts.evolution_slide_png(tmp_path / "b.png", "PCB", evo).stat().st_size > 1000
    assert charts.concepts_slide_png(tmp_path / "c.png", "PCB", [{"title": "為什麼", "points": ["一", "二"]}],
                                     ["結論一"]).stat().st_size > 1000


def test_mops_push_filter(monkeypatch):
    from market_intel import cli, config, research
    from market_intel.fetchers.news import NewsItem
    monkeypatch.setattr(config, "news_keywords", lambda: {"mops_skip": ["代.{0,4}子公司", "背書保證"]})
    monkeypatch.setattr(config, "tw_watch_codes", lambda: [])
    monkeypatch.setattr(config, "all_tw_theme_codes", lambda: ["2327"])
    monkeypatch.setattr(config, "themes", lambda: {"tw": {"被動元件": ["2327"]}})
    monkeypatch.setattr(research, "load_index", lambda: {})
    mk = lambda code, subj, score=0.0: NewsItem(source="MOPS重大訊息(上市)", title=f"{code} X：{subj}", codes=[code],
                                               score=score, summary="說明內容")
    items = [mk("2327", "董事會通過擴產案"), mk("2327", "代子公司公告取得設備"), mk("9999", "法說會"),
             mk("9999", "調升財測", 2.0), mk("2330", "背書保證")]
    out = cli.mops_to_push(items, {}, {"2330": {"std": "CDF"}})
    assert [it.title for it in out] == ["2327 X：董事會通過擴產案", "9999 X：調升財測"]
    msg = cli.mops_message(out[0], {}, {"2327": {"std": "LXF", "mini": "QEF"}})
    assert "股期 LXF／小型 QEF" in msg and "被動元件" in msg
