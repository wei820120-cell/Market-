"""2026-11-27 方案 1／2／3：開盤前研究（重大事件、美股領先）、族群拉升快訊、選股價量確認、事件族群進場規則。

起因：11/26 低軌衛星族群集體漲停，卻（1）開盤前沒有事件推播與研究、（2）選股組前 5 名被題材／漲價標籤占滿、
（3）進場規則只看有股期的 3 檔且要突破前高，整天沒有任何進場候選。"""
from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from market_intel import config
from market_intel.analysis import group_surge, picks
from market_intel.fetchers.tw_realtime import Quote
from market_intel.trade import event_group


def q(code, name, price, prev, ask="auto", volume=1000.0):
    """報價：ask='auto' 時有賣單（賣價＝現價），None＝沒有賣單。"""
    return Quote(code=code, name=name, market="tse", price=price, prev_close=prev, open=prev, high=price, low=prev,
                 volume_lots=volume, time="10:00:00", bid=price, ask=(price if ask == "auto" else ask))


# ---------------------------------------------------------------- 漲停價與鎖死

def test_limit_up_price_follows_tick_sizes():
    assert group_surge.limit_up_price(100) == 110.0
    assert group_surge.limit_up_price(152) == 167.0       # 167.2 → 升降單位 0.5 → 167.0
    assert group_surge.limit_up_price(9.5) == 10.45       # 浮點誤差不能讓 10.45 變 10.40
    assert group_surge.limit_up_price(1000) == 1100.0
    assert group_surge.limit_up_price(1234) == 1355.0     # 1357.4 → 升降單位 5 → 1355
    assert group_surge.limit_up_price(None) is None and group_surge.limit_up_price(0) is None


def test_locked_limit_up_needs_no_sellers():
    lock = q("1", "鎖死", 167.0, 152.0, ask=None)          # 在漲停價、沒有賣單
    sell = q("2", "有賣盤", 167.0, 152.0)                  # 在漲停價、還有賣單 → 買得到
    near = q("3", "差一檔", 166.5, 152.0, ask=None)        # 還沒到漲停價
    assert group_surge.is_locked(lock) and group_surge.at_limit_up(lock)
    assert group_surge.at_limit_up(sell) and not group_surge.is_locked(sell)
    assert not group_surge.at_limit_up(near) and not group_surge.is_locked(near)


# ---------------------------------------------------------------- 偵測

def sat_quotes():
    return {
        "2313": q("2313", "華通", 167.0, 152.0, ask=None),    # +9.9% 鎖死
        "3491": q("3491", "昇達科", 1380.0, 1255.0),           # +9.96% 漲停但有賣單
        "6285": q("6285", "啟碁", 193.0, 175.5),               # +10.0%
        "3105": q("3105", "穩懋", 444.5, 430.0),               # +3.4%（還沒跟上）
        "7717": q("7717", "萊德光電", 100.0, 99.0),            # +1.0%
        "5243": q("5243", "乙盛-KY", 50.0, 51.0),              # −2.0%
    }


def test_detect_group_surge_counts_and_flags():
    themes = {"低軌衛星": ["2313", "3491", "6285", "3105", "7717", "5243"]}
    groups = group_surge.detect(sat_quotes(), themes)
    assert len(groups) == 1
    g = groups[0]
    assert (g["theme"], g["n"], g["n_up"], g["n_locked"], g["level"]) == ("低軌衛星", 6, 3, 1, 1)
    by = {m["code"]: m for m in g["members"]}
    assert by["2313"]["locked"] and not by["3491"]["locked"] and by["3491"]["limit_up"]
    assert [m["code"] for m in g["members"]] == ["6285", "3491", "2313", "3105", "7717", "5243"]     # 依漲幅由高到低
    assert set(g["up_codes"]) == {"2313", "3491", "6285"} and g["avg_up"] == pytest.approx((9.87 + 9.96 + 9.97) / 3, abs=0.02)


def test_detect_needs_three_and_share_for_big_groups():
    two = {"A": ["2313", "3491", "3105"]}
    assert group_surge.detect({k: v for k, v in sat_quotes().items() if k in ("2313", "3491", "3105")}, two) == []   # 只有 2 檔 ≥5%
    def grid(total, up):
        return {f"{i:04d}": q(f"{i:04d}", f"股{i}", 105.0 if i <= up else 100.0, 100.0) for i in range(1, total + 1)}
    sixteen_3 = grid(16, 3)                                   # 16 檔、3 檔漲 5% → 需要 max(3, ceil(0.25×16)=4)=4 檔 → 不算
    assert group_surge.detect(sixteen_3, {"大族群": list(sixteen_3)}) == []
    sixteen_4 = grid(16, 4)                                   # 4 檔 → 達標
    assert [g["n_up"] for g in group_surge.detect(sixteen_4, {"大族群": list(sixteen_4)})] == [4]
    big2 = grid(24, 5)                                        # 24 檔要 6 檔：5 檔不算、6 檔算（第二級）
    assert group_surge.detect(big2, {"大族群": list(big2)}) == []
    big3 = grid(24, 6)
    assert [(g["n_up"], g["level"]) for g in group_surge.detect(big3, {"大族群": list(big3)})] == [(6, 2)]


def test_detect_dedupes_overlapping_themes_and_prefers_curated():
    quotes = sat_quotes()
    themes = {"研究:SpaceX FCC": ["2313", "3491", "6285", "3105"], "低軌衛星": ["2313", "3491", "6285", "3105", "7717", "5243"]}
    groups = group_surge.detect(quotes, themes)
    assert [g["theme"] for g in groups] == ["低軌衛星"]                     # 拉升股票相同 → 只留策展族群
    assert group_surge.covered_codes(groups) == {"2313", "3491", "6285"}
    assert group_surge.is_covered(["2313", "3491", "9999"], group_surge.covered_codes(groups))
    assert not group_surge.is_covered(["3105", "7717", "5243"], group_surge.covered_codes(groups))


def test_group_message_lists_up_members_flags_and_sizing():
    g = group_surge.detect(sat_quotes(), {"低軌衛星": ["2313", "3491", "6285", "3105", "7717", "5243"]})[0]
    text = group_surge.message(g, datetime(2026, 11, 27, 10, 12), {"2313": "FTF", "3491": "無", "6285": "未知"},
                               {"2313": "🟡 一口虧約 9,000 超過 2% 上限，需本金約 450,000"}, "📚 研究：《SpaceX》11/26", "大盤：加權 28,000（+1.20%）")
    assert "🚀 10:12 族群拉升｜低軌衛星：6 檔中 3 檔漲 ≥5%（漲停鎖死 1 檔，買不到）" in text
    assert "・2313 華通 +9.9% 🔒鎖死漲停｜股期FTF｜🟡" in text
    assert "・3491 昇達科" in text and "漲停有賣盤" in text and "無股期" in text and "股期未知" in text
    assert "還沒跟上：穩懋+3.4%" in text and "📚 研究：《SpaceX》11/26" in text and "大盤：加權 28,000" in text


# ---------------------------------------------------------------- 選股：價量確認與保留名額

def test_story_points_need_price_or_volume_confirmation():
    hike_up = picks.Candidate("1", "漲價且漲", pct=6.0, ratio=2.0, news_score=8, price_hike=True, price_letter=True)
    hike_flat = picks.Candidate("2", "漲價沒動", pct=0.5, ratio=0.8, news_score=8, price_hike=True, price_letter=True)
    hike_vol = picks.Candidate("3", "只有量", pct=0.5, ratio=2.0, news_score=8, price_hike=True, price_letter=True)
    s1, _ = picks.score(hike_up)
    s2, why2 = picks.score(hike_flat)
    s3, why3 = picks.score(hike_vol)
    assert s1 == 4 + 14 and picks.confirmations(hike_up) == 2        # 量 2×2＝4，故事分 8+4+2＝14 全給
    assert s2 == round(0.8 * 2 + 14 * 0.3, 1) and "未價量確認" in why2    # 都沒確認 ×0.3
    assert s3 == round(2.0 * 2 + 14 * 0.6, 1) and "未價量確認" in why3    # 只有量 ×0.6
    assert picks.confirmations(picks.Candidate("4", "沒資料")) is None
    assert picks.score(picks.Candidate("4", "沒資料", news_score=8, price_hike=True))[0] == 12   # 沒有價量資料不打折


def test_limit_up_stock_beats_unmoved_price_letter_stock():
    """11/26 的情況：有漲價信但沒動的股票，不該贏過放量漲停的股票。"""
    flat = picks.Candidate("2368", "沒動", pct=0.5, ratio=1.2, news_score=8, price_hike=True, price_letter=True, theme_hike=True)
    lim = picks.Candidate("2313", "漲停", pct=9.9, ratio=4.0, themes=["低軌衛星"], theme_inflow=True)
    assert picks.score(lim)[0] > picks.score(flat)[0]


def test_reserve_slots_for_strongest_price_volume():
    story = [picks.Candidate(f"{i:04d}", f"題材{i}", pct=1.0, ratio=1.6, news_score=8, price_hike=True, price_letter=True,
                             inst=30) for i in range(1, 8)]                                     # 7 檔題材股：有量沒大漲，分數 17.6
    strong = [picks.Candidate("9001", "漲停甲", pct=9.9, ratio=2.0), picks.Candidate("9002", "漲停乙", pct=9.0, ratio=1.6),
              picks.Candidate("9003", "漲停丙", pct=6.0, ratio=1.6)]                           # 量價強但沒有題材標籤，分數只有 3～4
    cands = story + strong
    plain = picks.rank_picks(cands, {}, min_ratio=1.5, slots=5, reserve=0)
    assert not {"9001", "9002", "9003"} & set(plain["代號"].head(5))                            # 沒有保留名額時全被擠出前 5
    df = picks.rank_picks(cands, {}, min_ratio=1.5, slots=5, reserve=2)
    top5 = list(df["代號"].head(5))
    assert {"9001", "9002"} <= set(top5) and "9003" not in top5 and len(set(top5)) == 5          # 只保留 2 檔：漲幅最大的兩檔
    assert sum(c.startswith("00") for c in top5) == 3 and all(c[0] == "0" or c.startswith("90") for c in top5)
    reasons = dict(zip(df["代號"], df["理由"]))
    assert "量價最強" in reasons["9001"] and "量價最強" in reasons["9002"] and "量價最強" not in reasons["0001"]
    assert picks.picks_message(df, "🎯").count("\n") == 5                                       # 標題＋前 5 名
    # 沒有強勢股、或強勢股本來就在前 5 名時，順序不變
    quiet = picks.rank_picks(story, {}, min_ratio=1.5)
    assert list(quiet["代號"]) == list(picks.rank_picks(story, {}, min_ratio=1.5, reserve=0)["代號"])
    top_already = [picks.Candidate("9001", "漲停甲", pct=9.9, ratio=4.0, inst=40, news_score=8, price_hike=True)] + story
    assert list(picks.rank_picks(top_already, {}, min_ratio=1.5)["代號"]) == list(
        picks.rank_picks(top_already, {}, min_ratio=1.5, reserve=0)["代號"])


# ---------------------------------------------------------------- 開盤前：重大事件與美股領先

@pytest.fixture
def isolated(tmp_path, monkeypatch):
    from market_intel import cli, notify, research, theme_card
    monkeypatch.setattr(research, "ROOT", tmp_path)
    monkeypatch.setattr(research, "QUEUE_DIR", tmp_path / "queue")
    monkeypatch.setattr(research, "INDEX_PATH", tmp_path / "index.json")
    monkeypatch.setattr(cli, "MAJOR_PATH", tmp_path / "major.json")
    monkeypatch.setattr(theme_card, "SENT_PATH", tmp_path / "cards_sent.json")
    sent = []
    monkeypatch.setattr(notify, "send", lambda text, channel="market": sent.append((channel, text)))
    return sent


def test_major_event_satellite_catches_fcc_approval_and_lists_all_names(isolated, tmp_path):
    import json
    from market_intel import cli, research
    from market_intel.fetchers.news import NewsItem
    (tmp_path / "index.json").write_text(json.dumps({
        "SpaceX FCC 批准百萬顆衛星說法求證": {"date": "2026-11-26", "codes": ["2313", "3491", "6285", "3105"], "file": "x.md"},
        "亞泰金屬（6727）題材與產業": {"date": "2026-11-27", "codes": ["6727", "2383"], "file": "y.md"}}), encoding="utf-8")
    quiet = [NewsItem(source="Google", title="衛星電視頻道費率調整", summary="", url="http://x/0"),
             NewsItem(source="Google", title="傳SpaceX概念股誰會漲 市場傳聞滿天飛", summary="", url="http://x/9")]
    assert cli.major_events(quiet, {}, macro_rows=[]) == []                              # 衛星電視、傳聞不算
    ev = [NewsItem(source="Google", title="FCC有條件批准SpaceX部署百萬顆太空資料中心衛星", summary="", url="http://x/1")]
    listings = {c: {"name": n} for c, n in (("2313", "華通"), ("3491", "昇達科"), ("6285", "啟碁"), ("3105", "穩懋"),
                                           ("7717", "萊德光電-KY"), ("5243", "乙盛-KY"), ("6190", "萬泰科"), ("3138", "耀登"))}
    assert cli.major_events(ev, listings, macro_rows=[]) == ["低軌衛星與SpaceX"]
    ch, text = isolated[0]
    assert ch == "news" and "🌐 重大事件｜低軌衛星與SpaceX" in text
    for name in ("華通", "昇達科", "啟碁", "穩懋", "萊德光電-KY", "乙盛-KY", "萬泰科", "耀登"):    # 8 檔名單都要列
        assert name in text
    assert "📚 既有研究（成分股相關）" in text and "《SpaceX FCC 批准百萬顆衛星說法求證》2026-11-26" in text
    assert "供需事件：低軌衛星與SpaceX事件對台股供應鏈的影響" in text
    assert list((tmp_path / "queue").glob("*.json"))                                     # 排入研究
    assert cli.major_events(ev, listings, macro_rows=[]) == ["低軌衛星與SpaceX"]         # 一天最多 2 則
    assert cli.major_events(ev, listings, macro_rows=[]) == []


def test_us_lead_has_space_names_with_valid_themes_and_verified_spacex_ticker():
    lead = config.us_lead()
    for sym in ("SPCX", "ASTS", "RKLB", "SATS", "IRDM", "UFO"):
        assert sym in lead and lead[sym]["themes"] == ["低軌衛星"] and lead[sym]["pct"] > 0
    assert lead["SPCX"]["name"] == "SpaceX" and "GSAT" not in lead                       # 被併購標的不列
    themes = config.themes().get("tw") or {}
    unknown = [(s, t) for s, v in lead.items() for t in v["themes"] if t not in themes and not t.startswith("研究:")]
    assert unknown == []
    from market_intel.fetchers import macro
    assert macro.assets()["SPCX"]["name"] == "SpaceX" and macro.assets()["SPCX"]["alert_pct"] == 8


def test_research_related_for_codes_orders_by_overlap_then_date(isolated, tmp_path):
    import json
    from market_intel import research
    (tmp_path / "index.json").write_text(json.dumps({
        "A": {"date": "2026-10-01", "codes": ["1", "2", "3"]}, "B": {"date": "2026-11-01", "codes": ["1", "2"]},
        "C": {"date": "2026-11-20", "codes": ["1", "2"]}, "D": {"date": "2026-11-26", "codes": ["9"]}}), encoding="utf-8")
    assert research.related_for_codes(["1", "2", "3"], limit=3) == [("A", "2026-10-01"), ("C", "2026-11-20"), ("B", "2026-11-01")]
    assert research.related_for_codes(["8"]) == []


# ---------------------------------------------------------------- 事件族群進場規則

FUT = {"2313": {"std": "FTF", "mini": "QTF"}, "6285": {"std": "FXF", "mini": ""}, "3105": {"std": "", "mini": "MWF"}}


def test_event_group_candidates_tiers_locked_and_two_percent_sizing():
    g = group_surge.detect(sat_quotes(), {"低軌衛星": ["2313", "3491", "6285", "3105", "7717", "5243"]})[0]
    lv = {"2313": {"atr": 6.0}, "6285": {"atr": 7.0}, "3105": {"atr": 15.0}}
    rows = event_group.candidates(g, FUT, lv, 1_000_000, {})
    by = {r["code"]: r for r in rows}
    assert "2313" not in by                                   # 鎖死漲停買不到，不列
    assert by["6285"]["tier"] == "追價" and by["3105"]["tier"] == "補漲"
    assert [r["tier"] for r in rows] == ["補漲", "追價"]       # 補漲排在前面
    assert by["3105"]["stop"] == pytest.approx(444.5 - 1.5 * 15.0) and by["3105"]["qty"] >= 1
    assert by["3105"]["contract"] == "MWF" and "可做" in event_group.size_text(by["3105"])
    tight = event_group.candidates(g, FUT, lv, 20_000, {})     # 本金太小：2% 規則做不了，要列需要的本金
    assert not any(r["qty"] for r in tight) and "超過 2% 上限" in event_group.size_text({r["code"]: r for r in tight}["6285"])
    nolv = event_group.candidates(g, FUT, {}, 1_000_000, {})
    assert "沒有 ATR" in event_group.size_text(nolv[0])
    assert event_group.tier_of(-1.5) == "" and event_group.tier_of(-0.5) == "補漲" and event_group.tier_of(5.0) == "追價"


def test_event_group_backing_events_and_message():
    state = {"低軌衛星與SpaceX|2026-11-27": 1, "俄烏停火與能源|2026-11-27": 1, "中東與航道|2026-11-26": 1}
    cfg = config.major_events()["events"]
    assert event_group.events_for_theme("低軌衛星", state, cfg, "2026-11-27") == ["低軌衛星與SpaceX"]
    assert event_group.events_for_theme("航運", state, cfg, "2026-11-27") == ["俄烏停火與能源"]      # 昨天的中東事件不算
    assert event_group.events_for_theme("低軌衛星", {}, cfg, "2026-11-27") == []
    assert event_group.backing(None, "", []) == []
    proof = event_group.backing("SpaceX FCC", "2026-11-26", ["低軌衛星與SpaceX"])
    assert proof == ["研究《SpaceX FCC》2026-11-26", "重大事件｜低軌衛星與SpaceX"]
    g = group_surge.detect(sat_quotes(), {"低軌衛星": ["2313", "3491", "6285", "3105", "7717", "5243"]})[0]
    rows = event_group.candidates(g, FUT, {"6285": {"atr": 7.0}, "3105": {"atr": 15.0}}, 1_000_000, {})
    bull = event_group.message(g, rows, proof, True, datetime(2026, 11, 27, 10, 30))
    bear = event_group.message(g, rows, proof, False, datetime(2026, 11, 27, 10, 30))
    assert "🧩 10:30 事件族群進場候選｜低軌衛星" in bull and "補漲（還沒漲到 5%" in bull and "追價（已漲 ≥5%" in bull
    assert "鎖死漲停 1 檔，買不到、不列" in bull and "🟢 多頭" in bull and "撐腰：研究《SpaceX FCC》" in bull
    assert "🔴 空頭／盤整——依規則不開新倉，名單只供觀察" in bear and "穩懋" in bear          # 空頭：只標示風險，名單照列
    assert "試行規則" in bull and "event_group_backtest.md" in bull
    assert "沒有進場候選" in event_group.message(g, [], proof, True, datetime(2026, 11, 27, 10, 30))


# ---------------------------------------------------------------- 盤中整合

def test_group_surge_check_pushes_once_and_marks_covered(isolated, tmp_path, monkeypatch):
    import json
    from market_intel import cli, research
    (tmp_path / "index.json").write_text(json.dumps({
        "SpaceX FCC": {"date": "2026-11-26", "codes": ["2313", "3491", "6285", "3105"], "file": "x.md"}}), encoding="utf-8")
    (tmp_path / "major.json").write_text(json.dumps({"低軌衛星與SpaceX|2026-11-27": 1}), encoding="utf-8")
    themes = {"低軌衛星": ["2313", "3491", "6285", "3105", "7717", "5243"], "研究:小族群": ["2313", "3491", "6285"]}
    gs = cli._group_surge_setup()
    assert gs and gs["conf"]["pct"] == 5.0 and gs["conf"]["min_stocks"] == 3
    trade_rt = {"levels": {"6285": {"atr": 7.0}, "3105": {"atr": 15.0}}, "equity": 1_000_000, "rates": {}, "market_ok": False}
    quotes = sat_quotes()
    idx = {"加權指數": Quote("t00", "加權指數", "tse", 28000.0, 27668.0, 27668.0, 28000.0, 27668.0, 1.0, "10:00:00")}
    early = datetime(2026, 11, 27, 9, 2)
    cli._group_surge_check(gs, quotes, themes, {}, FUT, trade_rt, early, idx)
    assert isolated == []                                                  # 09:05 以前不看
    now = datetime(2026, 11, 27, 10, 12)
    cli._group_surge_check(gs, quotes, themes, {}, FUT, trade_rt, now, idx)
    channels = [c for c, _ in isolated]
    assert channels == ["picks", "trade"]
    picks_text, trade_text = isolated[0][1], isolated[1][1]
    assert "族群拉升｜低軌衛星" in picks_text and "研究：《SpaceX FCC》2026-11-26" in picks_text and "低軌衛星與SpaceX" in picks_text
    assert "大盤：加權 28,000（+1.20%）" in picks_text and "🔒鎖死漲停" in picks_text
    assert "事件族群進場候選｜低軌衛星" in trade_text and "空頭／盤整" in trade_text
    assert gs["covered"] == {"2313", "3491", "6285"} and "低軌衛星" in gs["themes"]
    cli._group_surge_check(gs, quotes, themes, {}, FUT, trade_rt, datetime(2026, 11, 27, 10, 14), idx)
    assert len(isolated) == 2                                              # 同一族群同一級一天只推一次
    # 沒有進出場機器人：只推選股機器人；沒有研究也沒事件：不推進場候選
    isolated.clear()
    (tmp_path / "major.json").write_text("{}", encoding="utf-8")
    (tmp_path / "index.json").write_text("{}", encoding="utf-8")
    from market_intel import theme_card
    monkeypatch.setattr(theme_card, "SENT_PATH", tmp_path / "cards2.json")
    gs2 = cli._group_surge_setup()
    cli._group_surge_check(gs2, quotes, {"低軌衛星": themes["低軌衛星"]}, {}, FUT, trade_rt, now, idx)
    assert [c for c, _ in isolated] == ["picks"] and "已排入研究" in isolated[0][1]
    assert list((tmp_path / "queue").glob("*.json"))


# ---------------------------------------------------------------- 回測訊號（日 K 近似）

def synth_frames(n=40, seed=1):
    """6 檔：A1 A2 A3 在第 30 天同日漲 6%（A3 收漲停鎖在最高），A4 沒漲、A5 小跌、A6 沒有股期。"""
    idx = pd.date_range("2026-01-01", periods=n, freq="B")
    rng = np.random.default_rng(seed)
    base = 100 + np.cumsum(rng.normal(0, 0.1, n))
    close, high, openp = {}, {}, {}
    for c in ("A1", "A2", "A3", "A4", "A5", "A6"):
        s = pd.Series(base.copy(), index=idx)
        close[c], high[c], openp[c] = s, s * 1.001, s.shift(1).fillna(s.iloc[0])
    day = 30
    for c, up in (("A1", 0.06), ("A2", 0.06)):
        close[c].iloc[day:] = close[c].iloc[day - 1] * (1 + up)
        high[c].iloc[day:] = close[c].iloc[day:] * 1.001
    close["A3"].iloc[day:] = close["A3"].iloc[day - 1] * 1.099
    high["A3"].iloc[day:] = close["A3"].iloc[day:]                # 收在最高＝鎖死
    close["A5"].iloc[day:] = close["A5"].iloc[day - 1] * 0.995
    fr = {"close": pd.DataFrame(close), "high": pd.DataFrame(high), "open": pd.DataFrame(openp)}
    fr["ma60"] = fr["close"] * 0.9
    fr["rs"] = pd.DataFrame({c: pd.Series(0.5, index=idx) for c in close})
    return fr, idx[day]


def test_event_group_signals_split_tiers_and_skip_locked_and_no_futures():
    fr, day = synth_frames()
    futures = {c: {"std": "XXF", "mini": ""} for c in ("A1", "A2", "A3", "A4", "A5")}      # A6 沒有股期
    themes = {"T": ["A1", "A2", "A3", "A4", "A5", "A6"]}
    sig = event_group.signals(themes, fr, futures)
    got = {(c, t) for _, c, t in sig[day]}
    assert got == {("A1", "追價"), ("A2", "追價"), ("A4", "補漲"), ("A5", "補漲")}           # A3 收漲停鎖死、A6 沒股期都不出訊號
    lag_only = {(c, t) for _, c, t in event_group.signals(themes, fr, futures, tiers=("補漲",))[day]}
    assert lag_only == {("A4", "補漲"), ("A5", "補漲")}
    trend = {(c, t) for _, c, t in event_group.signals(themes, fr, futures, trend=True)[day]}
    assert trend == got                                                                     # 全部站上 60 日線 → 一樣
    below = dict(fr, ma60=fr["close"] * 1.2)                                                # 全部跌破 60 日線 → 趨勢濾網擋掉全部
    assert event_group.signals(themes, below, futures, trend=True) == {}
    assert event_group.signals({"T": ["A1", "A2"]}, fr, futures) == {}                       # 只有 2 檔


def test_theme_events_and_window_table_report_case_days():
    fr, day = synth_frames()
    futures = {c: {"std": "XXF", "mini": ""} for c in ("A1", "A2", "A3", "A4", "A5")}
    ev = event_group.theme_events("T", ["A1", "A2", "A3", "A4", "A5", "A6"], fr, futures)
    assert len(ev) >= 1 and ev[0]["日期"] == str(day.date()) and ev[0]["拉升檔數"] == 3
    assert "A1+6.0%" in ev[0]["拉升"]
    w = event_group.window_table(["A1", "A2", "A3"], fr, str(day.date()), str(day.date()))
    assert w == [{"日期": str(day.date()), "拉升檔數": 3, "規則觸發": "是", "A1": 6.0, "A2": 6.0, "A3": 9.9}]


def test_backtest_eventgroup_runs_end_to_end_on_synthetic_data(tmp_path, monkeypatch):
    from market_intel import cli, research
    n = 320
    idx = pd.date_range("2025-01-01", periods=n, freq="B")
    rng = np.random.default_rng(7)

    def bars(drift, jump_days=()):
        r = rng.normal(drift, 0.012, n)
        for d in jump_days:
            r[d] += 0.06
        c = 100 * np.cumprod(1 + r)
        o = np.r_[c[0], c[:-1]]
        return pd.DataFrame({"open": o, "high": np.maximum(o, c) * 1.005, "low": np.minimum(o, c) * 0.995, "close": c,
                             "volume": rng.integers(2_000_000, 5_000_000, n).astype(float)}, index=idx)
    jumps = (120, 200, 260)
    codes = ["1101", "1102", "1103", "1104", "1105", "1106"]
    data = {c: bars(0.001, jumps if i < 3 else ()) for i, c in enumerate(codes)}
    index_df = bars(0.0008)
    futures = {c: {"std": f"{c[-2:]}F", "mini": ""} for c in codes[:5]}
    monkeypatch.setattr(cli.config, "load", lambda name: {"tw": {"低軌衛星": codes}} if name == "themes" else {})
    monkeypatch.setattr(cli, "_listings", lambda: {c: {"name": f"股{c[-2:]}"} for c in codes})
    monkeypatch.setattr(research, "ROOT", tmp_path)
    monkeypatch.setattr(research, "RESEARCH_DIR", tmp_path / "research")
    (tmp_path / "research").mkdir()
    cli._backtest_eventgroup(data, futures, index_df, {}, 100_000, "2y")
    md = (tmp_path / "research" / "event_group_backtest.md").read_text(encoding="utf-8")
    for needle in ("# 事件族群進場規則回測", "## 訊號品質｜大盤多頭日", "## 訊號品質｜全部日子", "## 組合回測", "B＋事件族群（補漲＋追價）",
                   "## 低軌衛星個案", "事件日明細"):
        assert needle in md
    assert (tmp_path / "research" / "backtest" / "event_group.json").exists()


def test_premarket_lead_list_picks_satellite_names_with_futures_when_spcx_jumps():
    """盤前領先名單：SpaceX（SPCX）前一晚漲超過門檻 → 低軌衛星族群裡有股期、還沒跟上的股票（華通、啟碁、穩懋）排進名單。"""
    from market_intel.trade import early
    idx = pd.date_range("2026-10-01", periods=30, freq="B")
    close = pd.Series(100.0, index=idx)
    close.iloc[-1] = 106.5                                                  # 前一晚 +6.5%（門檻 5%）
    df = pd.DataFrame({"close": close, "volume": 1e6}, index=idx)
    calm = df.assign(close=100.0)                                           # 其他太空股沒動
    moves = early.us_moves({"SPCX": df, "ASTS": calm, "RKLB": calm}, config.us_lead())
    assert [m["sym"] for m in moves] == ["SPCX"] and moves[0]["themes"] == ["低軌衛星"] and moves[0]["name"] == "SpaceX"
    themes_tw = {k: [str(c) for c in v] for k, v in config.themes()["tw"].items()}
    state = {"close": 100.0, "ret5": 0.0, "ret20": 0.0, "ret120": None, "gap": 0.02, "vr": 1.0, "above60": True, "breakout": False}
    states = {c: dict(state) for c in themes_tw["低軌衛星"]}
    picks_ = early.lead_picks(moves, themes_tw, states, FUT)                # FUT：只有華通、啟碁、穩懋有股期
    assert {p["code"] for p in picks_} == {"2313", "6285", "3105"}
    states["2313"]["ret5"] = 0.12                                           # 已經漲過頭的不列
    assert {p["code"] for p in early.lead_picks(moves, themes_tw, states, FUT)} == {"6285", "3105"}
    assert early.us_moves({"SPCX": calm}, config.us_lead()) == []           # 沒達門檻就沒有領先名單
