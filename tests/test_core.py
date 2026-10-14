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
    assert [p.post_id for p in posts] == [1001, 1002, 1003]  # 純圖片貼文也要留著
    assert posts[1].text == "" and posts[1].has_media and posts[1].images == []  # 沒抓到圖片網址也不能漏掉
    assert "3.2T" in posts[0].text and "\n" in posts[0].text
    assert posts[2].text == "國巨 & 華新科 MLCC 喊漲" and posts[0].url == "https://t.me/Gooaye/1001"
    state: dict = {}
    assert tg.new_posts("Gooaye", posts, state) == [] and state["Gooaye"] == 1003  # 第一次只記錄
    state["Gooaye"] = 1001
    assert [p.post_id for p in tg.new_posts("Gooaye", posts, state)] == [1002, 1003]
    assert state["Gooaye"] == 1003


TG_IMG_HTML = """
<div class="tgme_widget_message_wrap js-widget_message_wrap"><div class="tgme_widget_message js-widget_message" data-post="Gooaye/2001">
<a class="tgme_widget_message_photo_wrap 5xx" href="https://t.me/Gooaye/2001" style="width:800px;background-image:url('https://cdn1.example/file/a.jpg')"><div class="tgme_widget_message_photo" style="padding-top:75%"></div></a>
<a class="tgme_widget_message_photo_wrap 6xx" href="https://t.me/Gooaye/2001?single" style="background-image:url(&#39;https://cdn1.example/file/b.jpg&#39;)"></a>
<time datetime="2026-10-12T08:00:00+00:00"></time></div></div>
<div class="tgme_widget_message_wrap js-widget_message_wrap"><div class="tgme_widget_message js-widget_message" data-post="Gooaye/2002">
<div class="tgme_widget_message_sticker_wrap"></div><time datetime="2026-10-12T08:05:00+00:00"></time></div></div>
<div class="tgme_widget_message_wrap js-widget_message_wrap"><div class="tgme_widget_message js-widget_message" data-post="Gooaye/2003">
<i class="tgme_widget_message_video_thumb" style="background-image:url('https://cdn1.example/file/v.jpg')"></i>
<div class="tgme_widget_message_text js-message_text" dir="auto">影片附註</div><time datetime="2026-10-12T08:10:00+00:00"></time></div></div>
"""


def test_telegram_channel_image_posts():
    from market_intel.fetchers import telegram_channel as tg
    posts = tg.parse_channel_html(TG_IMG_HTML, "Gooaye")
    assert [p.post_id for p in posts] == [2001, 2003]  # 貼圖（沒文字沒圖片）略過，相簿與影片縮圖留著
    assert posts[0].text == "" and posts[0].images == ["https://cdn1.example/file/a.jpg", "https://cdn1.example/file/b.jpg"]
    assert not posts[0].has_video and posts[1].has_video and posts[1].text == "影片附註"


def test_channels_image_post_flow(tmp_path, monkeypatch):
    """圖片貼文：先推圖片，再把 AI 辨識出的文字接在貼文後面推播，並送去題材偵測收件匣。"""
    import argparse
    from market_intel import ai, cli, notify, research
    from market_intel.fetchers import telegram_channel as tg
    post = tg.parse_channel_html(TG_IMG_HTML, "Gooaye")[0]
    monkeypatch.setattr(config_mod := __import__("market_intel.config", fromlist=["x"]), "sources",
                        lambda: {"telegram_channels": [{"name": "股癌", "handle": "Gooaye", "push_to": "research"}]})
    monkeypatch.setattr(cli, "_listings", lambda: {"3081": {"name": "聯亞"}})
    monkeypatch.setattr(cli, "_name_to_code", lambda l: {"聯亞": "3081"})
    monkeypatch.setattr(cli.stock_futures, "load_stock_futures", lambda: {})
    monkeypatch.setattr(tg, "STATE_PATH", tmp_path / "seen.json")
    tg.save_state({"Gooaye": 2000})
    monkeypatch.setattr(tg, "fetch_channel", lambda h: [post])
    img = tmp_path / "a.jpg"
    img.write_bytes(b"\xff\xd8fake")
    monkeypatch.setattr(tg, "download_images", lambda p, **k: [img])
    monkeypatch.setattr(ai, "available", lambda: True)
    monkeypatch.setattr(ai, "budget_left", lambda: True)
    monkeypatch.setattr(ai, "read_images", lambda images, prompt, usage: usage.calls.append("image") or "聯亞(3081) 目標價 500")
    monkeypatch.setattr(ai, "record_usage", lambda usage, topic: None)
    sent, photos, inbox = [], [], []
    monkeypatch.setattr(notify, "send", lambda text, channel="market": sent.append(text))
    monkeypatch.setattr(notify, "send_photos", lambda paths, caption="", channel="market": photos.append((paths, caption)))
    monkeypatch.setattr(research, "add_to_inbox", lambda *a: inbox.append(a))
    cli.cmd_channels(argparse.Namespace(latest=0, research=False))
    assert photos and photos[0][0] == [img] and "圖片貼文" in photos[0][1]
    assert "圖片文字（AI 辨識" in sent[0] and "聯亞(3081) 目標價 500" in sent[0] and "3081 聯亞" in sent[0]
    assert "聯亞(3081) 目標價 500" in inbox[0][2]
    # --latest 預設不送研究收件匣（測試用）；加 --research 補漏才送
    inbox.clear()
    cli.cmd_channels(argparse.Namespace(latest=1, research=False))
    assert not inbox
    cli.cmd_channels(argparse.Namespace(latest=1, research=True))
    assert inbox and "聯亞(3081) 目標價 500" in inbox[0][2]
    # 沒有金鑰：圖片照推，文字訊息提示只有圖片
    monkeypatch.setattr(ai, "available", lambda: False)
    sent.clear(); photos.clear(); inbox.clear()
    tg.save_state({"Gooaye": 2000})
    cli.cmd_channels(argparse.Namespace(latest=0, research=False))
    assert photos and "只有圖片" in sent[0]


def test_read_post_images_never_breaks_channel_push(tmp_path, monkeypatch):
    from market_intel import ai, cli
    img = tmp_path / "a.png"
    img.write_bytes(b"x")
    monkeypatch.setattr(ai, "available", lambda: True)
    monkeypatch.setattr(ai, "budget_left", lambda: True)

    def boom(*a, **k):
        raise RuntimeError("400 invalid_request_error")  # 例如模型不支援某個參數

    monkeypatch.setattr(ai, "read_images", boom)
    assert cli.read_post_images([img]) == ""  # 失敗就當沒辨識到，圖片照推
    monkeypatch.setattr(ai, "settings", lambda: {"read_channel_images": False})
    assert cli.read_post_images([img]) == ""


def test_read_images_uses_cheap_price(monkeypatch):
    from types import SimpleNamespace as NS
    from market_intel import ai
    seen = {}

    class FakeMessages:
        def create(self, **kw):
            seen.update(kw)
            return NS(content=[NS(type="text", text="抄錄結果")], stop_reason="end_turn",
                      usage=NS(input_tokens=2000, output_tokens=500))

    monkeypatch.setattr(ai, "_client", lambda: NS(messages=FakeMessages()))
    monkeypatch.setattr(ai, "check_budget", lambda: None)
    usage = ai.image_usage()
    assert ai.read_images([(b"abc", "image/png")], "請抄錄", usage) == "抄錄結果"
    block = seen["messages"][0]["content"][0]
    assert block["type"] == "image" and block["source"]["media_type"] == "image/png" and seen["model"].startswith("claude-haiku")
    assert 0 < usage.usd() < 0.01  # 用便宜模型單價，不是研究模型（Sonnet）的單價


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


def test_channel_topics_ignore_daily_cap(tmp_path, monkeypatch):
    """股癌等追蹤頻道貼文偵測出的題材不受每天上限；新聞偵測的題材照舊受限。偵測沒做成就把貼文留著。"""
    import argparse
    from types import SimpleNamespace as NS
    from market_intel import ai, cli, research
    monkeypatch.setattr(research, "RESEARCH_DIR", tmp_path / "research")
    monkeypatch.setattr(research, "INDEX_PATH", tmp_path / "research" / "index.json")
    monkeypatch.setattr(research, "QUEUE_DIR", tmp_path / "queue")
    monkeypatch.setattr(research, "INBOX_DIR", tmp_path / "inbox")
    monkeypatch.setattr(research, "PENDING_DIR", tmp_path / "pending")
    monkeypatch.setattr(cli, "_listings", lambda: {})
    monkeypatch.setattr(cli, "_auto_due", lambda: False)
    monkeypatch.setattr(cli.stock_futures, "load_stock_futures", lambda: {})
    monkeypatch.setattr(cli.notify, "send", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_send_card_for_topic", lambda *a, **k: None)
    monkeypatch.setattr(ai, "settings", lambda: {"max_per_day": 4, "min_importance": 3})
    monkeypatch.setattr(ai, "available", lambda: True)
    monkeypatch.setattr(ai, "budget_left", lambda: True)
    monkeypatch.setattr(research, "researched_today", lambda: 99)  # 今天的自動研究額度早就用完
    done, seen = [], []

    def fake_detect(texts):
        seen.append(list(texts))
        if texts == ["新聞標題"]:
            return [{"topic": "新聞題材", "reason": "r", "importance": 5}]
        return [{"topic": "CPO光學", "reason": "股癌提到", "importance": 4}, {"topic": "低分", "reason": "x", "importance": 2}]

    monkeypatch.setattr(research, "detect_topics", fake_detect)
    monkeypatch.setattr(research, "research_topic", lambda topic, trigger, l, f, auto=False: done.append((topic, auto)) or {"message": "ok", "data": {"topic": topic}})
    args = argparse.Namespace(topic=None, commands=False, inbox=True, auto=False, auto_slots=False)
    research.add_to_inbox("Gooaye", 1, "某則貼文", "u")
    cli.cmd_research(args)
    assert done == [("CPO光學", False)]  # 不計入每天上限（auto=False），重要性不夠的不研究
    assert not list((tmp_path / "inbox").glob("*.json"))  # 偵測完才刪
    # 新聞偵測：額度用完就不做
    done.clear()
    monkeypatch.setattr(cli.news_signals, "rank", lambda *a, **k: [NS(title="新聞標題")])
    monkeypatch.setattr(cli, "collect_news", lambda: [])
    monkeypatch.setattr(cli, "_name_to_code", lambda l: {})
    monkeypatch.setattr(cli, "_us_symbols", lambda: set())
    cli.cmd_research(argparse.Namespace(topic=None, commands=False, inbox=False, auto=True, auto_slots=False))
    assert done == []
    # 偵測失敗／沒有金鑰：貼文留著，下一輪再試
    research.add_to_inbox("Gooaye", 2, "另一則貼文", "u")
    monkeypatch.setattr(research, "detect_topics", lambda texts: (_ for _ in ()).throw(ai.AIUnavailable("預算用完")))
    cli.cmd_research(args)
    assert len(list((tmp_path / "inbox").glob("*.json"))) == 1 and done == []
    monkeypatch.setattr(ai, "available", lambda: False)
    cli.cmd_research(args)
    assert len(list((tmp_path / "inbox").glob("*.json"))) == 1


def _fake_research_client(calls):
    from types import SimpleNamespace as NS

    class Stream:
        def __init__(self, kw):
            calls.append(("stream", kw))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get_final_message(self):
            return NS(content=[NS(type="text", text="報告")], stop_reason="end_turn",
                      usage=NS(input_tokens=1_000_000, output_tokens=10_000))

    class Msgs:
        def stream(self, **kw):
            return Stream(kw)

    return NS(messages=Msgs(), beta=NS(messages=Msgs()))


def test_research_model_switch_tools_and_price(monkeypatch):
    """換成 Haiku：用舊版網頁工具、走一般 messages（不帶備援 beta）、用 Haiku 單價；Sonnet 維持新版工具。"""
    from market_intel import ai
    for model, modern in (("claude-sonnet-5-5", True), ("claude-haiku-5-5", False)):
        calls = []
        monkeypatch.setenv("RESEARCH_MODEL", model)
        monkeypatch.setattr(ai, "_client", lambda calls=calls: _fake_research_client(calls))
        usage = ai.research_usage()
        assert ai.web_research("sys", "prompt", usage) == "報告"
        kw = calls[0][1]
        types = [t["type"] for t in kw["tools"]]
        assert kw["model"] == model
        assert (types == ["web_search_20260209", "web_fetch_20260209"]) == modern
        assert ("fallbacks" in kw and "betas" in kw) == modern
        expect = 1_000_000 * (2 if modern else 0.1) / 1e6 + 10_000 * (10 if modern else 0.5) / 1e6
        assert abs(usage.usd() - expect) < 1e-6
    monkeypatch.delenv("RESEARCH_MODEL")


def test_research_trial_writes_nothing(tmp_path, monkeypatch, capsys):
    """試跑只印在紀錄裡：不存報告、不動研究索引與佇列，花費照記。"""
    from market_intel import ai, cli, research
    monkeypatch.setattr(research, "RESEARCH_DIR", tmp_path / "research")
    monkeypatch.setattr(research, "INDEX_PATH", tmp_path / "research" / "index.json")
    monkeypatch.setattr(research, "QUEUE_DIR", tmp_path / "queue")
    monkeypatch.setattr(ai, "check_budget", lambda: None)
    recorded = []
    monkeypatch.setattr(ai, "record_usage", lambda usage, topic: recorded.append(topic))
    monkeypatch.setattr(ai, "web_research", lambda system, prompt, usage: usage.calls.append("r") or "試跑報告全文")
    monkeypatch.setattr(ai, "extract_json", lambda *a, **k: {"topic": "和碩", "layers": []})
    monkeypatch.setenv("RESEARCH_MODEL", "claude-haiku-5-5")
    import argparse
    cli.cmd_research(argparse.Namespace(topic=None, trial="和碩", trial_trigger="觸發", commands=False, inbox=False,
                                        auto=False, auto_slots=False))
    out = capsys.readouterr().out
    assert "claude-haiku-5-5" in out and "試跑報告全文" in out and '"topic": "和碩"' in out
    assert recorded == ["試跑 和碩"]
    assert not (tmp_path / "research").exists() and not (tmp_path / "queue").exists()


def test_save_report_keeps_queued_topic_name(tmp_path, monkeypatch):
    """模型把題材名稱改寫（例如加上『供應鏈』）時，仍以佇列／指令的名稱為準，索引、檔名與推播才對得上。"""
    from market_intel import ai, research
    monkeypatch.setattr(research, "RESEARCH_DIR", tmp_path / "research")
    monkeypatch.setattr(research, "INDEX_PATH", tmp_path / "research" / "index.json")
    monkeypatch.setattr(research, "AUTO_THEMES_PATH", tmp_path / "research" / "auto_themes.yaml")
    monkeypatch.setattr(research, "QUEUE_DIR", tmp_path / "queue")
    data = {"topic": "和碩（4938）AI 伺服器整機櫃題材與產業供應鏈", "one_line": "x", "status": "混合", "horizon": "中期（3-12個月）",
            "keywords": [], "catalysts": [], "risks": [], "spec_tables": [], "evolution": [], "concepts": [],
            "conclusion": [], "sources": [], "layers": []}
    out = research._save_report("和碩（4938）題材與產業", data, "報告", "觸發", {}, {}, ai.Usage(), True)
    assert out["data"]["topic"] == "和碩（4938）題材與產業"
    assert "和碩（4938）題材與產業" in research.load_index()
    assert (tmp_path / "research" / "data" / f"{research.slug('和碩（4938）題材與產業')}.json").exists()


def test_theme_alias_pushes_candidates_once(tmp_path, monkeypatch):
    """『光學鏡頭』與研究自動產生的『研究:光學鏡頭』是同一份研究，資金流入時可卡位標的與題材卡只推一次；
    研究剛完成已推過的，資金流入不再重複推；研究完成的推播一天一次。"""
    import pandas as pd
    from market_intel import cli, research, theme_card
    monkeypatch.setattr(research, "RESEARCH_DIR", tmp_path / "research")
    monkeypatch.setattr(research, "INDEX_PATH", tmp_path / "research" / "index.json")
    monkeypatch.setattr(research, "QUEUE_DIR", tmp_path / "queue")
    monkeypatch.setattr(theme_card, "SENT_PATH", tmp_path / "sent.json")
    research.save_index({"光學鏡頭": {"file": "x.md", "date": "2026-10-12", "keywords": [], "codes": ["3008", "3406", "3441"],
                                    "status": "混合", "usd": 0, "auto": False}})
    monkeypatch.setattr(cli.config, "themes", lambda: {"tw": {"光學鏡頭": ["3008", "3406"], "研究:光學鏡頭": ["3008", "3406", "3441"],
                                                           "航運": ["2603"]}})
    trade, cards = [], []
    monkeypatch.setattr(cli, "push_trade_candidates", lambda title, codes, listings, context="", futures=None: trade.append((title, codes)))
    monkeypatch.setattr(theme_card, "from_research", lambda topic: {"title": topic})
    monkeypatch.setattr(theme_card, "build", lambda found, listings, context="", futures=None: found)
    monkeypatch.setattr(theme_card, "send", lambda card, channel="research": cards.append(card["title"]))
    monkeypatch.setattr(cli.notify, "send", lambda *a, **k: None)
    flow = pd.DataFrame({"族群": ["光學鏡頭", "研究:光學鏡頭", "航運"], "判讀": ["資金流入🔥"] * 3,
                         "資金增減(億)": [300.0, 280.0, 50.0], "量比(對5日均)": [2.8, 2.7, 1.9], "加權漲跌%": [8.6, 8.1, 3.0]})
    cli.push_theme_cards(flow, None, {}, "盤後", top=3)
    assert [t[0] for t in trade] == ["光學鏡頭", "航運"]  # 別名只算一個，航運照推
    assert len(set(trade[0][1])) == len(trade[0][1])      # 代號不重複
    assert cards.count("光學鏡頭（研究:光學鏡頭）") + cards.count("光學鏡頭") == 1
    n = len(trade)
    cli.push_theme_cards(flow, None, {}, "盤後", top=3)  # 同一天再跑一次也不重推
    assert len(trade) == n
    # 研究完成的推播一天一次；已經推過就不重複
    monkeypatch.setattr(theme_card, "SENT_PATH", tmp_path / "sent2.json")
    trade.clear()
    cli._send_card_for_topic("光學鏡頭", {}, "research", "🔬 研究完成")
    cli._send_card_for_topic("光學鏡頭", {}, "research", "🔬 研究完成")
    assert len(trade) == 1
    cli.push_theme_cards(flow[flow["族群"] == "光學鏡頭"], None, {}, "盤後", top=3)  # 研究剛完成推過 → 資金流入不再推可卡位標的
    assert len(trade) == 1


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


def test_paper_trade_cycle(tmp_path, monkeypatch):
    import numpy as np
    import pandas as pd
    from market_intel.trade import paper
    monkeypatch.setattr(paper, "STATE", tmp_path / "paper.json")
    idx = pd.date_range("2025-01-01", periods=200, freq="B", tz="UTC")
    close = pd.Series(np.linspace(100, 160, 200), index=idx)
    close.iloc[-1] = close.iloc[-2] * 1.04  # 最後一天突破
    vol = pd.Series(1e6, index=idx)
    vol.iloc[-1] = 3e6
    df = pd.DataFrame({"open": close * 0.995, "high": close * 1.01, "low": close * 0.99, "close": close, "volume": vol})
    data = {"8046": df}
    fut = {"8046": {"std": "LYF", "mini": "QSF"}}
    today = f"{idx[-1]:%Y-%m-%d}"
    msg = paper.run_post(data, df, fut, {"8046": "南電"}, {"8046": 0.216}, {}, today)
    st = paper.load()
    assert "多頭" in msg and st["plan"] and st["plan"][0]["contract"] == "QSF"
    assert "並排模擬" in msg and "T3 品質分級" in msg and (tmp_path / "paper_A.json").exists()
    assert paper.load("T3")["plan"][0]["risk_pct"] in (0.02, 0.03)  # 對照帳戶也有計劃，風險依品質分數
    assert "Q3 高勝率" in msg and (tmp_path / "paper_Q3.json").exists()
    assert paper.run_post(data, df, fut, {"8046": "南電"}, {}, {}, today) == ""  # 同一天不重算
    # 隔天：開盤成交
    nxt = idx[-1] + pd.Timedelta(days=1)
    row = pd.DataFrame({"open": [close.iloc[-1] * 1.01], "high": [close.iloc[-1] * 1.03],
                        "low": [close.iloc[-1] * 1.0], "close": [close.iloc[-1] * 1.02], "volume": [2e6]}, index=[nxt])
    df2 = pd.concat([df, row])
    msg2 = paper.run_post({"8046": df2}, df2, fut, {"8046": "南電"}, {"8046": 0.216}, {}, f"{nxt:%Y-%m-%d}")
    st = paper.load()
    assert "模擬進場" in msg2 and st["positions"] and st["positions"][0]["contract"] == "QSF"
    assert paper.load("A")["positions"] and paper.load("T3")["positions"]  # 對照帳戶同一天成交
    assert "盤前" in paper.run_pre()
    # 休市（沒有今天的日 K）不結算
    assert paper.run_post({"8046": df2}, df2, fut, {}, {}, {}, "2099-01-01") is None


def test_supply_shock(tmp_path, monkeypatch):
    from market_intel import cli, config, notify, research
    from market_intel.fetchers.news import NewsItem
    monkeypatch.setattr(research, "ROOT", tmp_path)
    monkeypatch.setattr(research, "QUEUE_DIR", tmp_path / "queue")
    monkeypatch.setattr(research, "INDEX_PATH", tmp_path / "index.json")
    kw = config.news_keywords()
    sent = []
    monkeypatch.setattr(notify, "send", lambda text, channel="market": sent.append((channel, text)))
    items = [NewsItem(source="鉅亨網", title="日本 MLCC 大廠工廠火災 停工兩週", summary="", codes=["2327"]),
             NewsItem(source="鉅亨網", title="中國宣布鎵、鍺出口管制", summary=""),
             NewsItem(source="鉅亨網", title="台積電法說會", summary="")]
    q = cli.supply_shocks(items, {"2327": {"name": "國巨"}}, {"2327": {"std": "LXF", "mini": "QEF"}})
    assert q[0] == "MLCC供需" and q[1].startswith("供需事件：") and len(q) == 2
    assert sent and sent[0][0] == "research" and "天災意外" in sent[0][1] and "政策管制" in sent[0][1]
    assert cli.shock_cause("一般新聞", kw["supply_shock"]["causes"]) is None
    assert cli.supply_shocks(items, {"2327": {"name": "國巨"}}, {}) == []  # 已在佇列不重複


def test_real_trade_parse_and_book(tmp_path, monkeypatch):
    from market_intel.trade import real
    monkeypatch.setattr(real, "STATE", tmp_path / "real.json")
    assert real.parse("亞泥 35.7 1口") == {"cmd": "fill", "side": "buy", "query": "亞泥", "price": 35.7, "qty": 1, "mini": False}
    assert real.parse("亞泥、35.7、1口")["price"] == 35.7
    assert real.parse("買 DYF 35.7 2口")["query"] == "DYF"
    assert real.parse("賣 亞泥 36.5 1口")["side"] == "sell"
    assert real.parse("南電 小型 1468 1口") == {"cmd": "fill", "side": "buy", "query": "南電", "price": 1468.0,
                                               "qty": 1, "mini": True}
    assert real.parse("持倉") == {"cmd": "positions"} and real.parse("你好") is None
    listings = {"1102": {"name": "亞泥"}, "8046": {"name": "南電"}}
    futures = {"1102": {"std": "DYF"}, "8046": {"std": "LYF", "mini": "QSF"}}
    assert real.resolve("亞泥", listings, futures) == ("1102", "亞泥", "DYF", 2000)
    assert real.resolve("DYF", listings, futures)[2] == "DYF"
    assert real.resolve("南電", listings, futures, mini=True) == ("8046", "南電", "QSF", 100)
    assert real.resolve("台積電", listings, futures) is None
    # 使用者實際打的格式：小金像電、1145、1口／買小型金像電 1145 口（漏打口數）
    listings["2368"], futures["2368"] = {"name": "金像電"}, {"std": "XYF", "mini": "QXF"}
    c = real.parse("小金像電、1145、1口")
    assert c["query"] == "小金像電" and c["price"] == 1145 and c["qty"] == 1
    assert real.resolve(c["query"], listings, futures, c["mini"]) == ("2368", "金像電", "QXF", 100)
    assert real.resolve("小型金像電", listings, futures) == ("2368", "金像電", "QXF", 100)
    assert real.parse("買小型金像電 1145 口") == {"cmd": "need_qty", "query": "金像電", "price": 1145.0, "mini": True}
    assert real.parse("金像電 1145")["cmd"] == "need_qty" and real.parse("Wvgf") is None
    # 有小型契約預設用小型；「一般」才用一般；更正與取消
    listings["1477"], futures["1477"] = {"name": "聚陽"}, {"std": "KSF", "mini": "SCF"}
    assert real.resolve("聚陽", listings, futures)[2] == "SCF"
    assert real.resolve("聚陽", listings, futures, std=True)[2] == "KSF"
    assert real.resolve("小聚陽", listings, futures)[2] == "SCF"
    f = real.parse("更正：小聚陽 206 1口")
    assert f["cmd"] == "fill" and f["replace"] and f["query"] == "小聚陽" and f["price"] == 206
    assert real.parse("一般聚陽 206 1口")["std"] is True
    assert real.parse("取消 聚陽") == {"cmd": "remove", "query": "聚陽"}
    st = real.load()
    real.buy(st, "1477", "聚陽", "KSF", 2000, 206.0, 1, 4.3, 0.2)
    ok, msg = real.remove(st, "1477", today_only=True)
    assert ok and msg.startswith("🗑") and not st["positions"]
    assert real.remove(st, "1477") == (False, None)
    # 同一檔一般與小型都有：更正時刪「契約不同於新記錄」的那筆；沒辦法判斷時不亂刪
    real.buy(st, "1477", "聚陽", "KSF", 2000, 206.0, 1, 4.3, 0.2)
    real.buy(st, "1477", "聚陽", "SCF", 100, 204.5, 2, 4.3, 0.2)
    ok, msg = real.remove(st, "1477", today_only=True)
    assert not ok and "KSF" in msg and "SCF" in msg and len(st["positions"]) == 2
    ok, msg = real.remove(st, "1477", today_only=True, prefer_not="SCF")
    assert ok and "KSF" in msg and [x["contract"] for x in st["positions"]] == ["SCF"]
    st = real.load()
    msg = real.buy(st, "1102", "亞泥", "DYF", 2000, 35.7, 1, 0.6, 0.135)
    assert "停損 34.80" in msg and st["positions"][0]["stop"] == 35.7 - 0.9
    msg = real.sell(st, "DYF", "亞泥", 36.5, 1)
    assert "損益 +1,4" in msg and not st["positions"] and st["equity"] > 100000


def test_size_three_times_margin():
    from market_intel.trade import backtest as bt
    p = bt.Params()
    # 南電 1468、停損 20 元：風險上限 2000/(20*100)=1 口；3 倍保證金 1468*100*0.216*3≈95,126 ≤ 10 萬 → 1 口
    assert bt.size(100_000, 1468, 1448, {"std": "LYF", "mini": "QSF"}, p, 0.216) == ("QSF", 100, 1)
    # 已有 1 檔佔用 8 萬準備金 → 剩 2 萬不夠再開
    assert bt.size(100_000, 1468, 1448, {"std": "LYF", "mini": "QSF"}, p, 0.216, 80_000) == ("", 0, 0)
    # 亞泥 35.6：風險可做 1 口；3 倍保證金 35.6*2000*0.135*3≈28,836
    assert bt.size(100_000, 35.6, 34.7, {"std": "DYF"}, p, 0.135) == ("DYF", 2000, 1)


def test_plan_theme_priority_and_blocked():
    import numpy as np
    import pandas as pd
    from market_intel.trade import backtest as bt, paper
    idx = pd.date_range("2025-01-01", periods=200, freq="B", tz="UTC")

    def mk(base, spread=0.004):
        close = pd.Series(np.linspace(base, base * 1.6, 200), index=idx)
        close.iloc[-1] = close.iloc[-2] * 1.04
        vol = pd.Series(1e6, index=idx)
        vol.iloc[-1] = 3e6
        return bt.prepare(pd.DataFrame({"open": close, "high": close * (1 + spread), "low": close * (1 - spread),
                                        "close": close,
                                        "volume": vol}), paper.PARAMS)
    prepped = {"2368": mk(700, 0.03), "1102": mk(25), "2002": mk(20)}
    fut = {"2368": {"std": "RKF", "mini": "VGF"}, "1102": {"std": "DYF"}, "2002": {"std": "CBF"}}
    st = {"equity": 100_000, "positions": []}
    plan = paper.make_plan(st, idx[-1], prepped, fut, {"2368": "金像電", "1102": "亞泥", "2002": "中鋼"}, {}, {},
                           True, {"2002": "鋼鐵", "2368": "AI伺服器PCB材料"})
    assert plan[0]["code"] == "2002" and plan[0]["theme"] == "鋼鐵"  # 題材股排前面
    assert st["blocked"] and st["blocked"][0]["code"] == "2368" and st["blocked"][0]["contract"] == "VGF"
    assert "要做 1 口需本金約" in paper.plan_text({**st, "plan": plan}, True)


def test_trade_explain():
    import numpy as np
    import pandas as pd
    from market_intel.trade import real
    assert real.parse("檢查 國巨") == {"cmd": "explain", "query": "國巨"}
    idx = pd.date_range("2025-01-01", periods=200, freq="B", tz="UTC")
    close = pd.Series(np.linspace(400, 600, 200), index=idx)
    close.iloc[-1] = close.iloc[-2] * 1.05
    vol = pd.Series(1e6, index=idx)
    vol.iloc[-1] = 3e6
    df = pd.DataFrame({"open": close, "high": close * 1.03, "low": close * 0.97, "close": close, "volume": vol})
    msg = real.explain("2327", "國巨", df, {"std": "LXF", "mini": "QEF"}, True, 100_000, 0.2025)
    assert "突破訊號：✅" in msg and "小型股期 QEF" in msg and "要做 1 口需本金約" in msg


def test_intraday_breakout_signal():
    import numpy as np
    import pandas as pd
    from datetime import datetime
    from market_intel.fetchers.tw_realtime import Quote
    from market_intel.trade import intraday
    idx = pd.date_range("2025-01-01", periods=120, freq="B", tz="UTC")
    close = pd.Series(np.linspace(30, 44, 120), index=idx)
    df = pd.DataFrame({"open": close, "high": close * 1.01, "low": close * 0.99, "close": close,
                       "volume": 2_000_000.0})
    lv = intraday.levels({"2367": df})
    assert round(lv["2367"]["avg_lots"]) == 2000
    q = Quote("2367", "燿華", "tse", 46.0, 44.0, 44.5, 46.2, 44.3, 1500, "10:00")
    sigs = intraday.check({"2367": q}, lv, 0.5)  # 半天成交 1500 張 vs 正常 1000 張 → 步調 1.5
    assert sigs and sigs[0]["code"] == "2367" and abs(sigs[0]["pace"] - 1.5) < 0.01
    msg = intraday.message(sigs[0], {"std": "VBF"}, "PCB供需", 100_000, 0.216, True, "alert",
                           datetime(2026, 10, 5, 10, 0))
    assert "⚡ 10:00 盤中突破：2367 燿華〔PCB供需〕" in msg and "一般 VBF" in msg
    q.price = 44.0
    assert intraday.check({"2367": q}, lv, 0.5) == []  # 沒突破


def test_scan_new_themes(tmp_path, monkeypatch):
    import pandas as pd
    from market_intel import cli, notify, research
    from market_intel.fetchers import tw_daily
    monkeypatch.setattr(research, "ROOT", tmp_path)
    monkeypatch.setattr(research, "QUEUE_DIR", tmp_path / "queue")
    monkeypatch.setattr(research, "INDEX_PATH", tmp_path / "index.json")
    monkeypatch.setattr(cli, "_trade_theme_map", lambda: {"2327": "被動元件"})
    monkeypatch.setattr(tw_daily, "load_revenue", lambda: {"4573": {"ym": "2026/08", "yoy": 337.8, "rev": 220_000},
                                                          "1234": {"ym": "2026/08", "yoy": 150.0, "rev": 5_000}})
    sent = []
    monkeypatch.setattr(notify, "send", lambda text, channel="market": sent.append((channel, text)))
    day = pd.DataFrame([{"code": "2327", "name": "國巨", "pct": 9.9, "value": 9e9},
                        {"code": "3324", "name": "雙鴻", "pct": 10.0, "value": 5e8},
                        {"code": "8888", "name": "小股", "pct": 10.0, "value": 1e7}])
    listings = {"4573": {"name": "高明鐵"}, "1234": {"name": "X"}}
    q = cli.scan_new_themes(day, listings, {})
    assert q == ["雙鴻（3324）題材與產業", "高明鐵（4573）題材與產業"]
    assert sent[0][0] == "research" and "營收年增 +338%" in sent[0][1]
    assert cli.scan_new_themes(day, listings, {}) == []  # 已排入、營收同月份不重複


def test_trade_candidates_rows():
    import numpy as np
    import pandas as pd
    from market_intel.trade import candidates
    idx = pd.date_range("2025-01-01", periods=150, freq="B", tz="UTC")

    def mk(base, last_jump, spread):
        c = pd.Series(np.linspace(base, base * 1.3, 150), index=idx)
        c.iloc[-1] = c.iloc[-2] * last_jump
        v = pd.Series(1e6, index=idx)
        v.iloc[-1] = 3e6
        return pd.DataFrame({"open": c, "high": c * (1 + spread), "low": c * (1 - spread), "close": c, "volume": v})
    hist = {"1102": mk(30, 1.04, 0.004), "2327": mk(500, 1.04, 0.03), "1101": mk(40, 0.99, 0.004), "9999": mk(10, 1, 0.01)}
    fut = {"1102": {"std": "DYF"}, "2327": {"std": "LXF", "mini": "QEF"}, "1101": {"std": "DFF"}}
    rs = candidates.rows(list(hist), hist, fut, {"1102": "亞泥", "2327": "國巨", "1101": "台泥"}, 100_000, {})
    assert [r["code"] for r in rs][:2] == ["1102", "2327"]  # 可做的突破優先，其次做不了的突破；9999 沒股期不列
    assert rs[0]["qty"] >= 1 and rs[1]["qty"] == 0 and "9999" not in [r["code"] for r in rs]
    msg = candidates.message("被動元件", "⚡ 盤中資金湧入", rs, True)
    assert "🟢 可做 DYF" in msg and "小型 QEF" in msg and "需本金約" in msg


def test_gov_import_ban_is_supply_shock():
    from market_intel import config
    from market_intel.cli import shock_cause
    causes = config.news_keywords()["supply_shock"]["causes"]
    assert shock_cause("中國製玻璃纖維矽質套管 經部今公告即日起停止輸入", causes) == "進口管制公告"


def test_mini_contract_shows_mini_name(tmp_path, monkeypatch):
    """持倉是小型股期時，推播要顯示「小型金像電」，不能只寫「金像電」讓人以為是現股或一般契約。"""
    import pandas as pd
    from market_intel.trade import backtest as bt, paper, real
    monkeypatch.setattr(real, "STATE_PATH", tmp_path / "real.json", raising=False)
    assert bt.label("金像電", bt.MINI) == "小型金像電" and bt.label("小型金像電", bt.MINI) == "小型金像電"
    assert bt.label("陽明", bt.STD) == "陽明"
    st = {"capital": 100000, "equity": 100000, "positions": [], "closed": []}
    msg = real.buy(st, "2368", "金像電", "VGF", bt.MINI, 1145.0, 1, 54.0, 0.2025)
    assert "已記錄買進 小型金像電（2368）VGF" in msg
    msg = real.buy(st, "2609", "陽明", "DAF", bt.STD, 61.6, 1, 1.9, 0.162)
    assert "已記錄買進 陽明（2609）DAF" in msg
    text = real.positions_text(st)
    assert "・小型金像電 VGF 1 口" in text and "・陽明 DAF 1 口" in text
    day = pd.Timestamp("2026-10-12")
    prepped = {"2368": pd.DataFrame({"low": [1000.0], "high": [1200.0], "close": [1010.0], paper.PARAMS.exit_ma: [1100.0]},
                                    index=[day])}
    alerts = real.daily_check(st, day, prepped)
    assert alerts and all("小型金像電 VGF" in a for a in alerts)
    assert "已記錄賣出 小型金像電 VGF 1 口" in real.sell(st, "VGF", "金像電", 1100.0, 1)


def test_real_chinese_qty_and_multi_price_sell():
    from market_intel.trade import real
    assert real.parse("賣出小聚陽 204.5 一口")["qty"] == 1
    assert real.parse("小聚陽 206 兩口")["qty"] == 2
    assert real.parse("賣 亞泥 36.5 十二口")["qty"] == 12
    parts = real.expand("賣出 小聚陽 204.5、204、203.5各一口共三口")
    assert [real.parse(p)["price"] for p in parts] == [204.5, 204.0, 203.5]
    assert all(real.parse(p)["side"] == "sell" and real.parse(p)["qty"] == 1 for p in parts)
    assert real.expand("亞泥 35.7 1口") == ["亞泥 35.7 1口"]


def test_broker_rating_and_target_price_news():
    from market_intel import config
    from market_intel.analysis import news_signals as ns
    from market_intel.fetchers.news import NewsItem
    kw = config.news_keywords()

    def score(title):
        it = NewsItem(source="t", title=title)
        return ns.score_item(it, kw).score
    assert score("高盛調升大立光目標價至1.1萬元 看好FAU切入CPO") >= 3
    assert score("外資升評台積電 目標價上調至2,750元") >= 3
    assert score("瑞銀降評 喊賣大立光 目標價維持5,000元") <= -3
    assert score("大摩調降台積電目標價") <= -3
    assert score("大立光董事會通過配息") == 0

def test_supply_shock_skips_unrelated_politics_and_major_events(tmp_path, monkeypatch):
    from market_intel import cli, notify, research
    from market_intel.fetchers.news import NewsItem
    monkeypatch.setattr(research, "ROOT", tmp_path)
    monkeypatch.setattr(research, "QUEUE_DIR", tmp_path / "queue")
    monkeypatch.setattr(research, "INDEX_PATH", tmp_path / "index.json")
    monkeypatch.setattr(cli, "MAJOR_PATH", tmp_path / "major.json")
    sent = []
    monkeypatch.setattr(notify, "send", lambda text, channel="market": sent.append((channel, text)))
    politics = [NewsItem(source="Google", title="美國擬制裁國際刑事法院 8國外長齊聲反對", summary=""),
                NewsItem(source="Google", title="被指對俄羅斯運柴油助緩油荒 南韓：未列出口管制", summary="")]
    assert cli.supply_shocks(politics, {}, {}) == []  # 對不到台股標的、產品或關鍵原料，不推
    ev = [NewsItem(source="Google", title="美國總統特朗普：即刻生效，俄烏戰爭達成能源停火", summary="", url="http://x/1")]
    assert cli.major_events(ev, {"2618": {"name": "長榮航"}}) == ["俄烏停火與能源"]
    assert sent and sent[0][0] == "news" and "初步推論" in sent[0][1] and "供需事件：俄烏停火與能源價格" in sent[0][1]
    assert (tmp_path / "queue").exists() and list((tmp_path / "queue").glob("*.json"))
    assert cli.major_events(ev, {}) == []  # 同一事件一天只推一次


def test_macro_snapshot_alerts_and_major_event_context(tmp_path, monkeypatch):
    import pandas as pd
    from market_intel import cli, notify, research
    from market_intel.fetchers import macro, yahoo
    from market_intel.fetchers.news import NewsItem
    monkeypatch.setattr(macro, "SNAP_PATH", tmp_path / "snap.json")
    monkeypatch.setattr(macro, "ALERT_PATH", tmp_path / "alerts.json")
    idx = pd.date_range("2026-09-01", periods=25, freq="D", tz="UTC")

    def fake_chart(sym, range_="1mo", interval="1d"):
        closes = [80.0] * 24 + [75.0] if sym == "BZ=F" else [100.0] * 25  # 布蘭特單日 -6.25%
        return pd.DataFrame({"close": closes, "open": closes, "high": closes, "low": closes, "volume": 1}, index=idx), {"regularMarketTime": 1789000000}
    monkeypatch.setattr(yahoo, "fetch_chart", fake_chart)
    rows = macro.snapshot()
    brent = next(r for r in rows if r["symbol"] == "BZ=F")
    assert brent["d1_pct"] == -6.25 and brent["d20_pct"] == -6.25 and brent["name"] == "布蘭特原油"
    assert macro.save_if_changed(rows) and not macro.save_if_changed(rows)  # 沒變不重寫
    assert macro.fresh()
    msgs = macro.alerts(rows)
    assert any("布蘭特原油" in m and "國際行情異動" in m for m in msgs)
    assert macro.alerts(rows) == []  # 同方向一天一次
    # 重大事件訊息附上國際行情，研究觸發附快照
    monkeypatch.setattr(research, "ROOT", tmp_path)
    monkeypatch.setattr(research, "QUEUE_DIR", tmp_path / "queue")
    monkeypatch.setattr(research, "INDEX_PATH", tmp_path / "index.json")
    monkeypatch.setattr(cli, "MAJOR_PATH", tmp_path / "major.json")
    sent = []
    monkeypatch.setattr(notify, "send", lambda text, channel="market": sent.append((channel, text)))
    ev = [NewsItem(source="Google", title="特朗普：俄烏達成能源停火", summary="", url="http://x/1")]
    assert cli.major_events(ev, {}, macro_rows=rows) == ["俄烏停火與能源"]
    assert "📊 國際行情：布蘭特原油" in sent[0][1]
    q = next((tmp_path / "queue").glob("*.json")).read_text(encoding="utf-8")
    assert "國際行情快照" in q and "BZ=F" in q


# ---------------------------------------------------------------- 提前進場訊號

def _early_stock(n=160, last=None, spike=None, vol_last=None, base=(100, 112), spread=0.004):
    import numpy as np
    import pandas as pd
    idx = pd.date_range("2025-01-01", periods=n, freq="B", tz="UTC")
    close = pd.Series(np.linspace(base[0], base[1], n), index=idx)
    if spike:  # (從第幾根起, 漲到多少)：已經先漲一段
        k, v = spike
        close.iloc[k:] = np.linspace(close.iloc[k - 1], v, n - k)
    vol = pd.Series(1e6, index=idx)
    if last is not None:
        close.iloc[-1] = last
        vol.iloc[-1] = vol_last or 3e6
    return pd.DataFrame({"open": close.shift(1).fillna(close.iloc[0]), "high": close * (1 + spread),
                         "low": close * (1 - spread), "close": close, "volume": vol})


def test_early_lead_signals_skip_stocks_that_already_ran():
    import pandas as pd
    from market_intel.trade import backtest as bt, early
    quiet, ran = _early_stock(), _early_stock(spike=(120, 190))
    prepped = {c: bt.prepare(d, bt.Params()) for c, d in {"1111": quiet, "2222": ran}.items()}
    fr = early.frames(prepped)
    day = quiet.index[-3]
    uidx = pd.DatetimeIndex([ts + pd.Timedelta(hours=13, minutes=30) for ts in quiet.index])  # 美股時間戳同一個日曆日
    us_close = pd.Series(100.0, index=uidx)
    us_close.iloc[-3] = 106.0  # +6%
    us = pd.DataFrame({"close": us_close, "volume": 1e6}, index=uidx)
    sig = early.lead_signals({"MU": us}, {"MU": {"pct": 4.0, "themes": ["記憶體"]}}, {"記憶體": ["1111", "2222"]}, fr)
    assert [(c, n) for _, c, n in sig[day]] == [("1111", "領先")]  # 已經大漲過的 2222 不算
    assert early.lead_signals({"MU": us}, {"MU": {"pct": 8.0, "themes": ["記憶體"]}}, {"記憶體": ["1111"]}, fr) == {}  # 沒到門檻


def test_early_merge_interleaves_signal_types():
    import pandas as pd
    from market_intel.trade import early
    ts = pd.Timestamp("2026-01-05", tz="UTC")
    lead = {ts: [(5.0, "A", "領先"), (4.0, "B", "領先"), (3.0, "C", "領先")]}
    lag = {ts: [(0.9, "D", "補漲"), (0.2, "E", "補漲")]}
    got = [(c, n) for _, c, n in sorted(early.merge(lead, lag)[ts], reverse=True)]
    assert got == [("A", "領先"), ("D", "補漲"), ("B", "領先"), ("E", "補漲"), ("C", "領先")]  # 輪流，不被量大的一種擠掉


def test_early_laggard_signals_need_a_leader_breakout():
    from market_intel.trade import backtest as bt, early
    leader = _early_stock(last=140)  # 最後一天放量創高
    prepped = {"1111": bt.prepare(leader, bt.Params()), "2222": bt.prepare(_early_stock(), bt.Params()),
               "3333": bt.prepare(_early_stock(spike=(120, 190)), bt.Params())}
    fr = early.frames(prepped)
    assert bool(fr["sig_a"]["1111"].iloc[-1])
    sig = early.laggard_signals({"光通訊": ["1111", "2222", "3333"]}, fr)
    day = leader.index[-1]
    assert [(c, n) for _, c, n in sig[day]] == [("2222", "補漲")]  # 龍頭本身、已經漲一大段的不算
    assert early.laggard_signals({"光通訊": ["2222", "3333"]}, fr, min_members=3) == {}  # 沒人突破、成分股太少都不產生


def test_backtest_extra_signal_enters_next_open_and_intraday_fills_at_breakout():
    import numpy as np
    import pandas as pd
    from market_intel.trade import backtest as bt
    d = _early_stock(spread=0.004)
    for col in ("open", "high", "low", "close"):  # 最後 5 天崩跌，讓持倉被停損出場（回測只回報已出場的交易）
        d.iloc[-5:, d.columns.get_loc(col)] = 80.0
    idx = d.index
    index_df = pd.DataFrame({"close": np.linspace(100, 140, len(idx))}, index=idx)
    fut = {"1111": {"mini": "QSF"}}
    p = bt.Params(setups=(), exit_ma="ma20")
    x = idx[-10]
    res = bt.run({"1111": d}, fut, index_df, p, extra={x: [(1.0, "1111", "領先")]})
    t = res["trades"][0]
    assert t["setup"] == "領先" and t["entry_date"] == str(idx[-9].date()) and t["entry"] == float(d["open"].iloc[-9])
    assert res["各型態"]["領先"]["筆數"] == 1
    # 盤中突破：碰到前 20 日高就用「前高或開盤價」成交，當天進場（用 100／101 來回震盪的股票，平常不會創高）
    d2 = _early_stock(spread=0.004)
    flat = pd.Series([100.0 if i % 2 == 0 else 101.0 for i in range(len(idx))], index=idx)
    for col in ("open", "close"):
        d2[col] = flat
    d2["high"], d2["low"] = flat * 1.004, flat * 0.996
    hh = float(d2["high"].iloc[-30:-10].max())
    d2.iloc[-10, d2.columns.get_loc("high")] = hh * 1.02
    d2.iloc[-10, d2.columns.get_loc("close")] = hh * 1.01
    for col in ("open", "high", "low", "close"):
        d2.iloc[-5:, d2.columns.get_loc(col)] = 80.0
    res2 = bt.run({"1111": d2}, fut, index_df, bt.Params(setups=(), exit_ma="ma20", intraday=True), None)
    t2 = res2["trades"][0]
    assert t2["setup"] == "盤中突破" and t2["entry_date"] == str(x.date())
    assert abs(t2["entry"] - max(float(d2["open"].iloc[-10]), hh)) < 1e-6


def test_early_forward_stats_compares_signal_types():
    import numpy as np
    import pandas as pd
    from market_intel.trade import backtest as bt, early
    d = _early_stock(n=200)
    prepped = {"1111": bt.prepare(d, bt.Params())}
    fr = early.frames(prepped)
    mkt = pd.Series(True, index=d.index)
    sig = {d.index[100]: [(1.0, "1111", "領先")]}
    tab = early.forward_stats(sig, fr, mkt)
    row = tab[tab["訊號"] == "領先"].iloc[0]
    assert row["筆數"] == 1 and row["5日平均%"] > 0 and "全部股票（基準）" in set(tab["訊號"])


def test_early_live_lead_list_and_laggard_section(monkeypatch, tmp_path):
    import pandas as pd
    from market_intel import cli, config, notify
    from market_intel.fetchers import stock_futures, tw_daily, yahoo
    from market_intel.trade import backtest as bt, early, paper, real
    # 美股：MU 昨夜 +6%（門檻 4）、NVDA +1%（沒到）
    uidx = pd.date_range("2026-09-01", periods=30, freq="B", tz="UTC") + pd.Timedelta(hours=13, minutes=30)
    mu = pd.DataFrame({"close": [100.0] * 29 + [106.0], "volume": 1e6}, index=uidx)
    nv = pd.DataFrame({"close": [100.0] * 29 + [101.0], "volume": 1e6}, index=uidx)
    lead_map = {"MU": {"name": "美光", "pct": 4.0, "themes": ["記憶體"]}, "NVDA": {"name": "輝達", "pct": 3.0, "themes": ["AI伺服器"]}}
    moves = early.us_moves({"MU": mu, "NVDA": nv}, lead_map)
    assert [m["sym"] for m in moves] == ["MU"] and round(moves[0]["pct"], 1) == 6.0
    near = early.us_moves({"MU": mu, "NVDA": nv}, lead_map, floor_ratio=-100)  # 沒達門檻時用來看「最接近的是誰」
    assert [m["sym"] for m in near] == ["MU", "NVDA"]
    quiet, ran = _early_stock(), _early_stock(spike=(120, 190))
    states = {c: early.last_state(bt.prepare(d, paper.PARAMS)) for c, d in {"1111": quiet, "2222": ran}.items()}
    fut = {"1111": {"mini": "QSF"}, "2222": {"std": "XXF"}}
    picks = early.lead_picks(moves, {"記憶體": ["1111", "2222", "3333"], "AI伺服器": ["1111"]}, states, fut)
    assert [p["code"] for p in picks] == ["1111"] and picks[0]["why"] == ["美光 +6.0%"] and picks[0]["themes"] == ["記憶體"]
    # 整個流程：推播到 trade 頻道
    sent = []
    monkeypatch.setenv("TELEGRAM_TRADE_BOT_TOKEN", "x")
    monkeypatch.setattr(notify, "send", lambda text, channel="market": sent.append((channel, text)))
    monkeypatch.setattr(config, "us_lead", lambda: lead_map)
    monkeypatch.setattr(config, "themes", lambda: {"tw": {"記憶體": ["1111", "2222"], "AI伺服器": ["1111"]}})
    monkeypatch.setattr(yahoo, "fetch_many", lambda syms, range_="3mo": {"MU": mu, "NVDA": nv})
    monkeypatch.setattr(yahoo, "fetch_chart", lambda sym, range_="1y", interval="1d": (pd.DataFrame(
        {"close": [100 + i for i in range(200)]}, index=pd.date_range("2026-01-01", periods=200, freq="B", tz="UTC")), {}))
    monkeypatch.setattr(cli, "_yahoo_histories", lambda codes, listings, range_="1y": {"1111": quiet, "2222": ran})
    monkeypatch.setattr(cli, "_margin_rates", lambda: {})
    monkeypatch.setattr(real, "load", lambda: {"equity": 100_000, "positions": []})
    monkeypatch.setattr(paper, "STATE", tmp_path / "paper.json")  # 模擬帳戶 L 的狀態檔寫到暫存資料夾
    monkeypatch.setattr(tw_daily, "load_alerts", lambda: {})
    text = cli.push_lead_list({"1111": {"name": "甲"}, "2222": {"name": "乙"}}, fut)
    assert sent and sent[0][0] == "trade" and "美光 MU +6.0%" in text and "1111 甲" in text and "2222" not in text
    assert "模擬帳戶 L" in text and [x["code"] for x in paper.load("L")["plan"]] == ["1111"]  # 領先名單同時排進 L 的開盤計劃
    monkeypatch.setattr(yahoo, "fetch_many", lambda syms, range_="3mo": {"NVDA": nv})  # 昨夜沒有達門檻的
    sent.clear()
    assert cli.push_lead_list({}, fut) is None and not sent  # 自動執行：沒有就不推
    assert "沒有達門檻" in cli.push_lead_list({}, fut, force=True) and sent  # 手動：回一則說明
    # 題材補漲：龍頭放量突破 + 同題材還沒漲
    leader = _early_stock(last=140)
    st2 = {c: early.last_state(bt.prepare(d, paper.PARAMS)) for c, d in
           {"1111": leader, "2222": _early_stock(), "3333": _early_stock(spike=(120, 190))}.items()}
    lead, lag = early.laggard_picks(["1111", "2222", "3333"], st2)
    assert lead == ["1111"] and [x["code"] for x in lag] == ["2222"]
    sec = early.laggard_section(lead, lag, {}, {"1111": "龍頭", "2222": "補漲股"})
    assert "補漲候選" in sec and "1111 龍頭" in sec and "2222 補漲股" in sec
    assert early.laggard_picks(["2222", "3333"], st2) == ([], [])  # 沒有龍頭突破就不列


def test_backtest_extra_slots_do_not_compete_with_base_positions():
    import numpy as np
    import pandas as pd
    from market_intel.trade import backtest as bt
    ds = {c: _early_stock(spread=0.03) for c in ("1111", "2222")}  # 振幅大 → 口數小，兩檔的保證金都放得下
    for d in ds.values():
        for col in ("open", "high", "low", "close"):
            d.iloc[-5:, d.columns.get_loc(col)] = 80.0
    idx = ds["1111"].index
    index_df = pd.DataFrame({"close": np.linspace(100, 140, len(idx))}, index=idx)
    fut = {"1111": {"mini": "QSF"}, "2222": {"mini": "QSF"}}
    x = idx[-10]
    ex = {x: [(1.0, "1111", "領先"), (0.9, "2222", "補漲")]}
    one = bt.run(ds, fut, index_df, bt.Params(setups=(), max_pos=1, exit_ma="ma20", extra_slots=1), None, extra=ex)
    two = bt.run(ds, fut, index_df, bt.Params(setups=(), max_pos=1, exit_ma="ma20", extra_slots=2), None, extra=ex)
    assert len(one["trades"]) == 1 and len(two["trades"]) == 2  # 額外名額 1 → 只進分數高的；2 → 兩檔都進
    shared = bt.run(ds, fut, index_df, bt.Params(setups=(), max_pos=1, exit_ma="ma20"), None, extra=ex)
    assert len(shared["trades"]) == 1  # 沒設額外名額 → 和 max_pos 共用


def test_paper_account_l_has_dedicated_lead_slot():
    from market_intel.trade import backtest as bt, paper
    label, p = paper.VARIANTS["L"]
    assert p.extra_slots == 1 and p.max_pos == 3 and "領先" in label

    def pos(code, setup):
        return {"code": code, "name": code, "setup": setup, "contract": "QSF", "mult": 100, "qty": 1, "qty0": 1,
                "entry_date": "2026-10-01", "entry": 100.0, "stop": 90.0, "r0": 10.0, "realized": 0.0,
                "half_done": False, "days": 1, "exit_next": ""}
    st = {"equity": 100_000, "positions": [pos("9001", "突破"), pos("9002", "突破"), pos("9003", "拉回")], "plan": []}
    assert paper.slot_full(st, "突破", p) and not paper.slot_full(st, "領先", p)  # 3 檔滿了，領先位仍空著
    assert paper.slot_full(st, "突破", paper.VARIANTS["A"][1]) is False            # A（4 檔）共用名額
    # 領先計劃：進到 4 檔，領先位佔掉後不再加；大盤空頭、處置股不加；重算會取代舊的領先計劃
    ds = {c: _early_stock(spread=0.03) for c in ("1111", "2222")}
    prepped = {c: bt.prepare(d, paper.PARAMS) for c, d in ds.items()}
    fut = {"1111": {"mini": "QSF"}, "2222": {"mini": "QSF"}}
    names = {"1111": "甲", "2222": "乙"}
    picks = [{"code": "1111", "themes": ["記憶體"], "why": ["美光 +6.0%"], "score": 1.5},
             {"code": "2222", "themes": ["記憶體"], "why": ["美光 +6.0%"], "score": 1.2}]
    assert paper.add_lead_plan(st, picks, prepped, fut, names, {}, {}, False, p) == []   # 大盤空頭
    got = paper.add_lead_plan(st, picks, prepped, fut, names, {}, {"1111": ["處置股"]}, True, p)
    assert [x["code"] for x in got] == ["2222"] and got[0]["setup"] == "領先"           # 處置股跳過
    got = paper.add_lead_plan(st, picks, prepped, fut, names, {}, {}, True, p)
    assert [x["code"] for x in got] == ["1111", "2222"] and len(st["plan"]) == 2          # 取代舊的，不重複
    day = ds["1111"].index[-1]
    events = paper.settle(st, day, prepped, {}, p)
    held = [x["code"] for x in st["positions"] if x["setup"] == "領先"]
    assert held == ["1111"] and any("模擬進場 1111" in e and "領先" in e for e in events)  # 領先位只佔 1 檔，第二個被名額擋掉
    assert len(st["positions"]) == 4 and paper.add_lead_plan(st, picks, prepped, fut, names, {}, {}, True, p) == []


def test_launch_labels_features_and_model():
    import numpy as np
    import pandas as pd
    from market_intel.trade import backtest as bt, early, launch
    # 兩檔：甲在第 100 天後 5 日內先漲 10%（發動），乙一路下跌（碰停損）
    up, down = _early_stock(n=160, spread=0.01), _early_stock(n=160, base=(120, 100), spread=0.01)
    up.iloc[103, up.columns.get_loc("high")] = float(up["open"].iloc[101]) * 1.09   # 進場後第 3 天最高衝到 +9%
    prepped = {"1111": bt.prepare(up, bt.Params()), "2222": bt.prepare(down, bt.Params())}
    fr = early.frames(prepped)
    lab = launch.label_frame(fr)
    day = up.index[100]
    assert lab["hit"].at[day, "1111"] == 1.0                       # 隔天開盤起 5 日內碰到 +7%
    assert lab["hit_r"].at[day, "1111"] in (0.0, 1.0) and -1.0 <= lab["rr"].at[day, "1111"] <= 2.0
    assert lab["hit"].at[down.index[100], "2222"] == 0.0
    assert np.isnan(lab["hit"].iloc[-1]["1111"])                   # 最後幾天未來資料不足 → 不算
    index_df = pd.DataFrame({"close": np.linspace(100, 140, 160)}, index=up.index)
    feats = launch.feature_frames(fr, index_df, {"記憶體": ["1111", "2222", "9999"]}, {})
    assert set(launch.FEATURES) <= set(feats) and feats["rs120"].at[up.index[150], "1111"] > feats["rs120"].at[up.index[150], "2222"]
    df = launch.dataset(fr, feats, lab, pd.Series(True, index=up.index))
    assert {"date", "code", "hit", "fwd", "mfe"} <= set(df.columns) and len(df) > 30
    # 邏輯迴歸能學到簡單規則：特徵 ret5 越高越容易 hit
    rng = np.random.default_rng(0)
    n = 4000
    syn = pd.DataFrame({f: rng.normal(size=n) for f in launch.FEATURES})
    syn["hit"] = (rng.random(n) < 1 / (1 + np.exp(-(syn["ret5"] * 1.5 - 1.5)))).astype(float)
    syn["fwd"], syn["mfe"], syn["sig_a"], syn["sig_b"] = 0.0, 0.0, 0.0, 0.0
    syn["rr"] = np.where(syn["hit"] > 0, 2.0, -1.0)
    for kind in ("linear", "binned"):
        p = launch.Model(kind).fit(syn).predict(syn)
        top = launch.lift_table(syn, p)[1]                          # 前 10%
        assert top["倍數"] > 2.0, (kind, top)
    tab = launch.univariate(syn)
    assert tab[0]["特徵"] == launch.LABELS["ret5"] and tab[0]["最高組發動率%"] > tab[0]["最低組發動率%"]
    assert len(launch.calibration(syn, p)) == 10


def test_launch_walk_forward_scores_only_use_the_past():
    import numpy as np
    import pandas as pd
    from market_intel.trade import launch
    rng = np.random.default_rng(1)
    dates = pd.date_range("2024-01-01", periods=600, freq="B", tz="UTC")
    rows = []
    for d in dates:
        for code in ("1111", "2222", "3333", "4444", "5555", "6666"):
            f = {k: rng.normal() for k in launch.FEATURES}
            rows.append({"date": d, "code": code, **f})
    df = pd.DataFrame(rows)
    df["hit_r"] = (rng.random(len(df)) < 1 / (1 + np.exp(-(df["ret5"] * 1.2 - 1.5)))).astype(float)
    df["hit"], df["rr"], df["fwd"], df["mfe"], df["sig_a"], df["sig_b"] = df["hit_r"], df["hit_r"], 0.0, 0.0, 0.0, 0.0
    sc = launch.walk_forward_scores(df, "hit_r", "linear", min_train_days=250, step_days=90)
    assert sc["date"].min() >= dates[0] + pd.Timedelta(days=250) and len(sc) > 1000   # 前一年沒有分數
    merged = sc.merge(df[["date", "code", "ret5", "hit_r"]], on=["date", "code"])
    assert np.corrcoef(merged["score"], merged["ret5"])[0, 1] > 0.5                    # 向前訓練仍學得到 ret5 的關係
    sig = launch.top_signals(sc, n=2)
    day = next(iter(sig))
    assert len(sig[day]) == 2 and sig[day][0][2] == "高分" and sig[day][0][0] >= sig[day][1][0]
