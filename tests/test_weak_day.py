"""大跌日逆勢榜（analysis/picks.py 的 is_weak_day、weak_day_leaders 與 cli.weak_day_markdown）。

背景：2026-11-25 加權指數 −4.80%，逆勢收紅的個股有獲利兌現型（金像電、川湖）與事件輪動型（資安股漲停）。
名單只列出來供觀察、附類型，不影響 picks.score 的分數。"""
from __future__ import annotations

import numpy as np
import pandas as pd

from market_intel import cli
from market_intel.analysis import picks

IDX = -4.8


def day_df(rows):
    """rows: (代號, 名稱, 漲跌%, 成交值〔元〕)。"""
    return pd.DataFrame([{"code": c, "name": n, "pct": p, "value": v} for c, n, p, v in rows])


def sample_day():
    return day_df([
        ("2368", "金像電", 5.23, 50e8),       # 逆勢紅盤，營收有成長，前一天大漲後沒回吐
        ("6666", "測試股", 6.0, 8e8),          # 逆勢紅盤，營收有成長，處置中
        ("1234", "輪動股", 10.0, 20e8),        # 漲停，營收沒跟上
        ("5678", "抗跌股", -1.0, 10e8),        # 跌幅不到大盤一半
        ("9999", "跌太多", -3.0, 30e8),        # 跌幅超過大盤一半 → 不列
        ("4444", "小成交", 3.0, 1e8),          # 成交值不到 3 億 → 不列
        ("0050", "ETF", 2.0, 90e8),            # ETF → 不列
        ("2882A", "特別股", 2.0, 9e8),         # 特別股 → 不列
        ("7777", "無營收", 1.0, 5e8),          # 沒有營收資料
        ("8888", "壞資料", float("nan"), 5e8),  # 漲跌欄位缺值 → 略過
    ])


def leaders(**kw):
    args = dict(prev_pct={"2368": 10.0}, ratio={"2368": 1.64}, inst={"2368": -1.4, "6666": 2.6},
                revenue={"2368": {"yoy": 80.0}, "6666": {"yoy": 50.0}, "1234": {"yoy": 5.0}, "5678": {"yoy": 3.0}},
                alerts={"6666": ["處置 11/20~11/30"]}, themes={"2368": ["PCB供需", "CCL供需", "A", "B"]})
    args.update(kw)
    return picks.weak_day_leaders(sample_day(), IDX, **args)


def test_is_weak_day_threshold_and_missing_index():
    assert picks.is_weak_day(-2.0) and picks.is_weak_day(-4.8)
    assert not picks.is_weak_day(-1.99) and not picks.is_weak_day(0.5)
    assert not picks.is_weak_day(None) and not picks.is_weak_day(float("nan"))


def test_weak_day_leaders_filters_and_types():
    df = leaders()
    assert list(df["代號"]) == ["6666", "2368", "1234", "7777", "5678"]      # 營收有成長在前，其次相對大盤強度
    t = dict(zip(df["代號"], df["類型"]))
    assert t["2368"] == picks.TYPE_REV and t["6666"] == picks.TYPE_REV
    assert t["1234"] == picks.TYPE_LIMIT                                      # 漲停但營收沒跟上 → 輪動
    assert t["5678"] == picks.TYPE_OTHER and t["7777"] == picks.TYPE_OTHER   # 營收未達標／沒有資料
    assert {"9999", "4444", "0050", "2882A", "8888"}.isdisjoint(df["代號"])


def test_weak_day_leaders_limit_up_with_revenue_gets_its_own_type():
    df = picks.weak_day_leaders(day_df([("3000", "漲停有營收", 10.0, 9e8)]), IDX, revenue={"3000": {"yoy": 60.0}})
    assert df.loc[0, "類型"] == picks.TYPE_LIMIT_REV


def test_weak_day_leaders_columns_values_and_notes():
    df = leaders().set_index("代號")
    assert list(leaders().columns) == picks.WEAK_DAY_COLUMNS
    r = df.loc["2368"]
    assert r["漲跌%"] == 5.23 and r["相對大盤(pp)"] == 10.03                 # 5.23 − (−4.8)
    assert r["前日漲跌%"] == 10.0 and r["量比"] == 1.64 and r["成交(億)"] == 50.0
    assert r["法人(億)"] == -1.4 and r["月營收年增%"] == 80.0
    assert "守住前日漲幅" in r["備註"] and "量1.6倍" in r["備註"]
    assert r["族群"] == "PCB供需、CCL供需、A"                                # 最多 3 個
    assert "⚠處置 11/20~11/30" in df.loc["6666", "備註"] and "法人買3億" in df.loc["6666", "備註"]
    assert pd.isna(df.loc["7777", "月營收年增%"]) and pd.isna(df.loc["7777", "前日漲跌%"])
    assert df.loc["1234", "備註"] == ""


def test_weak_day_leaders_held_gain_needs_big_prior_rally_and_no_loss_today():
    day = day_df([("1111", "甲", 0.5, 9e8), ("2222", "乙", -1.0, 9e8), ("3333", "丙", 0.5, 9e8)])
    df = picks.weak_day_leaders(day, IDX, prev_pct={"1111": 6.0, "2222": 8.0, "3333": 2.0}).set_index("代號")
    assert "守住前日漲幅" in df.loc["1111", "備註"]            # 前一天 +6%、今天收紅
    assert "守住前日漲幅" not in df.loc["2222", "備註"]        # 今天收黑（雖然抗跌）
    assert "守住前日漲幅" not in df.loc["3333", "備註"]        # 前一天漲幅不夠大


def test_weak_day_leaders_top_and_empty_inputs():
    assert len(leaders(top=2)) == 2
    for bad in (None, pd.DataFrame()):
        assert picks.weak_day_leaders(bad, IDX).empty
    assert picks.weak_day_leaders(sample_day(), None).empty
    assert list(picks.weak_day_leaders(sample_day(), None).columns) == picks.WEAK_DAY_COLUMNS


def test_score_and_rank_picks_are_unchanged_by_weak_day_feature():
    c = picks.Candidate(code="2368", name="金像電", pct=5.2, ratio=1.64, inst=-1.4, themes=["PCB供需"], theme_inflow=True)
    assert picks.score(c) == (5.0, ["量1.6倍", "族群資金流入"])        # 1.64×2 − 1.4÷5 + 2 = 5.0（沒有任何逆勢加分）
    down = picks.Candidate(code="1101", name="跌的", pct=-3.0, ratio=2.0)
    assert picks.rank_picks([down], {}).empty                           # 跌超過 1% 照舊排除


def _hist(closes, volumes):
    idx = pd.date_range("2026-11-17", periods=len(closes), freq="B")
    return pd.DataFrame({"close": closes, "volume": volumes}, index=idx)


def test_weak_day_markdown_only_on_weak_days_and_builds_prev_and_ratio(monkeypatch):
    monkeypatch.setattr(cli.tw_daily, "load_revenue", lambda: {"2368": {"yoy": 80.0}})
    monkeypatch.setattr(cli.tw_daily, "load_alerts", lambda: {})
    day = day_df([("2368", "金像電", 5.23, 50e8), ("2330", "台積電", -3.6, 900e8)])
    hist = {"2368": _hist([800, 780, 860, 905, 1000, 1050], [1000] * 5 + [2000]),    # 前一天 +10.5%、今天量 2.4 倍
            "2330": _hist([2200, 2210, 2220, 2230, 2240, 2150], [1000] * 6)}
    inst = pd.DataFrame([{"code": "2368", "foreign": 4_000_000, "trust": 1_000_000}])  # 500 萬股 × 1000 = 50 億
    md = cli.weak_day_markdown(day, IDX, hist, inst, {"2368": 1000.0}, {"PCB供需": ["2368"]})
    assert "## 0b. 大跌日逆勢榜（加權指數 -4.80%）" in md and "不影響上面強勢標的的分數" in md
    row = [ln for ln in md.splitlines() if ln.startswith("| 2368")][0]
    assert "營收有成長" in row and "PCB供需" in row and "法人買50億" in row and "量2.4倍" in row and "守住前日漲幅" in row
    assert "2330" not in md                                              # 跌 3.6% > 大盤一半（2.4%）→ 不列
    assert cli.weak_day_markdown(day, -1.5, hist, inst, {}, {}) == ""     # 不是大跌日 → 不產生這一節
    assert cli.weak_day_markdown(day, None, hist, inst, {}, {}) == ""     # 抓不到指數 → 不產生


def test_weak_day_markdown_handles_nan_inst_and_missing_hist(monkeypatch):
    monkeypatch.setattr(cli.tw_daily, "load_revenue", lambda: {})
    monkeypatch.setattr(cli.tw_daily, "load_alerts", lambda: {})
    day = day_df([("2368", "金像電", 5.23, 50e8)])
    inst = pd.DataFrame([{"code": "2368", "foreign": np.nan, "trust": 1.0}])
    md = cli.weak_day_markdown(day, IDX, {}, inst, {"2368": 900.0}, {})
    assert "| 2368 | 金像電 | 5.23 |" in md and "營收未達標／無資料" in md
