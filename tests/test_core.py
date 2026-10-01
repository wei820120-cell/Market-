"""離線測試：用樣本資料驗證解析與計算邏輯（不連網）。"""
from datetime import datetime

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
    html_text = """<table><tr><th>證券代號</th><th>名稱</th><th>股票期貨</th><th>股票選擇權</th></tr>
      <tr><td>2330</td><td>台積電</td><td>CDF</td><td>CDO</td></tr>
      <tr><td>2327</td><td>國巨</td><td>DHF</td><td></td></tr>
      <tr><td>9999</td><td>只有選擇權</td><td></td><td>ZZO</td></tr></table>"""
    assert stock_futures.parse_stock_lists_html(html_text) == {"2330": "CDF", "2327": "DHF"}
    rows = [{"證券代號": "2330", "股票期貨英文代碼": "CDF"}]
    assert stock_futures.parse_openapi_rows(rows) == {"2330": "CDF"}
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
    df = picks.rank_picks(cands, {"2327": "DHF"})
    assert list(df["代號"]) == ["2327", "2330"]
    assert df.iloc[0]["股票期貨"] == "DHF" and df.iloc[1]["股票期貨"] == "無"
    assert "漲價" in df.iloc[0]["理由"]
    msg = picks.picks_message(df, "🎯")
    assert "股期DHF" in msg and "無股期" in msg


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
