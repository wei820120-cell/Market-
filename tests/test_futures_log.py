"""微台計畫帳觸發紀錄（market_intel/trade/futures_log.py）：即時記錄、歷史補錄、排成人看的表、不碰真的 state/。"""
from __future__ import annotations

import json

from market_intel.trade import futures_day as fd
from market_intel.trade import futures_log as fl

from test_futures_day import CFG, NIGHT_CFG, TREND, at   # noqa: E402  同資料夾的測試共用模擬報價


def _run(session="night"):
    """把多方趨勢日一筆筆餵進 step，回傳（狀態、每筆觸發的訊號清單）。"""
    cfg = dict(CFG)
    st = fd.new_day("2026-10-30")
    batches = []
    for ts, p in TREND:
        sigs = fd.step(st, ts, p, cfg, [])
        if sigs:
            batches.append(sigs)
    return st, batches, cfg


def test_alert_carries_full_timestamp():
    _, batches, _ = _run()
    assert all("ts" in s and s["ts"].startswith("2026-10-30T") for b in batches for s in b)


def test_record_appends_events_with_running_pnl_and_dedupes(tmp_path):
    st, batches, cfg = _run()
    p = tmp_path / "futures_day.json"
    for b in batches:
        fl.record(st, b, cfg, "day", p)
    fl.record(st, batches[0], cfg, "day", p)            # 同一筆重送：不重複記
    d = fl.load(p)
    assert len(d["sessions"]) == 1
    s = d["sessions"][0]
    assert s["date"] == "2026-10-30" and s["session"] == "day" and s["source"] == "即時"
    assert len(s["events"]) == sum(len(b) for b in batches)
    assert s["events"][0]["kind"] in ("進場", "盤面") and s["events"][0]["ts"]
    assert s["summary"]["entries"] == st["entries"] and s["summary"]["twd"] == round(fd.snapshot(st, cfg)["pts"] * cfg["contract_multiplier"])
    assert (tmp_path / "futures_log.md").exists()


def test_record_does_not_touch_real_state_dir(tmp_path):
    before = fl.LOG_PATH.read_bytes() if fl.LOG_PATH.exists() else None
    st, batches, cfg = _run()
    fl.record(st, batches[0], cfg, "night", tmp_path / "futures_day.json")
    after = fl.LOG_PATH.read_bytes() if fl.LOG_PATH.exists() else None
    assert before == after


def test_refresh_updates_final_pnl_only_for_known_sessions(tmp_path):
    st, batches, cfg = _run()
    p = tmp_path / "futures_day.json"
    fl.refresh(st, cfg, "day", True, p)                 # 這場沒觸發過：不建立空紀錄
    assert fl.load(p)["sessions"] == []
    fl.record(st, batches[0], cfg, "day", p)
    fl.refresh(st, cfg, "day", True, p)
    s = fl.load(p)["sessions"][0]
    assert s["finished"] is True and s["summary"]["pts"] == fd.snapshot(st, cfg)["pts"]


def test_backfill_uses_stored_alert_keys_and_skips_days_without_entries(tmp_path):
    cfg = dict(CFG)
    st = fd.new_day("2026-11-05")
    st.update(dir="short", entries=2, adds=2, realized_pts=-382.0, open=42623.0, day_high=43114.0, day_low=41666.0, last_price=41696.0,
              entry_bar="20261105-1200", qualified=True,
              alerts=["qualified", "enter1", "add:20261105-0920", "stop1", "enter2", "ext:2:2", "stop2"])
    p = tmp_path / "futures_day.json"
    assert fl.backfill(st, cfg, "day", p) is True
    assert fl.backfill(st, cfg, "day", p) is False      # 已經有了：不重複補
    flat = fd.new_day("2026-11-06")
    assert fl.backfill(flat, cfg, "day", p) is False    # 沒進場 → 沒有觸發，不補
    s = fl.load(p)["sessions"][0]
    assert s["source"] == "補錄" and s["summary"]["twd"] == -3820
    assert [e["kind"] for e in s["events"]] == ["盤面", "進場", "加碼", "停損", "進場", "加碼", "停損"]
    assert s["events"][2]["time"] == "約 09:25"                   # 加碼代號自帶 5 分 K 時間
    assert s["events"][4]["time"].startswith("約 12:00～12:05")   # 最後一次進場用 entry_bar
    assert s["events"][1]["time"] == ""                           # 沒留存的時間不亂編


def test_render_md_has_totals_cumulative_and_overnight_label(tmp_path):
    cfg = dict(CFG)
    p = tmp_path / "futures_day.json"
    for date, pts in (("2026-11-04", -220.0), ("2026-11-05", 300.0)):
        st = fd.new_day(date)
        st.update(dir="long", entries=1, adds=0, realized_pts=pts, open=1.0, day_high=2.0, day_low=0.5, last_price=1.5, alerts=["enter1"])
        fl.backfill(st, cfg, "day", p)
    st, batches, ncfg = _run()
    st["date"] = "2026-11-06N"
    night = [dict(s, ts="2026-11-07T01:20:00+08:00", time="01:20") for s in batches[0]]
    fl.record(st, night, ncfg, "night", p)
    md = (tmp_path / "futures_log.md").read_text(encoding="utf-8")
    assert "| 2026-11-04 | 日盤 | 做多 | 1 | 0 | -220 | -2,200 | -2,200 | 補錄 |" in md
    assert "| 2026-11-05 | 日盤 | 做多 | 1 | 0 | +300 | +3,000 | +800 | 補錄 |" in md
    assert "隔天 01:20" in md and "夜盤" in md
    assert fl.stats(fl.load(p))["n"] == 3


def test_load_tolerates_missing_or_broken_file(tmp_path):
    p = tmp_path / "futures_day.json"
    assert fl.load(p) == {"sessions": []}
    (tmp_path / "futures_log.json").write_text("{壞掉", encoding="utf-8")
    assert fl.load(p) == {"sessions": []}
    assert json.loads(json.dumps(fl.load(p))) == {"sessions": []}


# ---------------------------------------------------------------- 規則參數隨場次記錄；中途改規則，報表要標出來（累計損益不能默默跨越兩套規則）

def _record_day(p, day, cfg, finished=True):
    st, batches, _ = _run()
    st["date"] = day
    for b in batches:
        fl.record(st, b, cfg, "day", p)
    fl.refresh(st, cfg, "day", finished, p)
    return st


def test_sessions_record_the_rule_parameters_they_ran_with(tmp_path):
    p = tmp_path / "futures_day.json"
    _record_day(p, "2026-10-30", dict(CFG))
    r = fl.load(p)["sessions"][0]["rules"]
    assert r["stop_pct"] == 0.15 and r["stop_noise_mult"] == 0 and r["exit_times"] == ["13:10", "13:25"] and r["max_entries"] == 2


def test_report_is_untouched_when_the_rules_never_change(tmp_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    p = tmp_path / "futures_day.json"
    _record_day(p, "2026-10-30", dict(CFG))
    _record_day(p, "2026-11-02", dict(CFG))
    data = fl.load(p)
    assert fl.rule_changes(data) == [] and len(fl.segments(data)) == 1
    md = (tmp_path / "futures_log.md").read_text(encoding="utf-8")
    assert "規則參數" not in md and "各段小計" not in md
    assert "規則參數" not in fl.performance_text(data, datetime(2026, 11, 3, tzinfo=ZoneInfo("Asia/Taipei")))
    legacy = {"sessions": [{"date": "2026-11-04", "session": "day", "source": "補錄", "events": [], "summary": {"entries": 1, "pts": -10, "twd": -100}},
                           {"date": "2026-11-05", "session": "day", "events": [], "summary": {"entries": 1, "pts": 5, "twd": 50}, "rules": {"stop_pct": 0.15}}]}
    assert fl.rule_changes(legacy) == []          # 舊場次沒記規則、之後只有一種記錄的規則：不憑空判定「改過」


def test_report_flags_a_mid_stream_rule_change_and_splits_the_subtotals(tmp_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    p = tmp_path / "futures_day.json"
    _record_day(p, "2026-10-30", dict(CFG))
    _record_day(p, "2026-11-02", dict(CFG, stop_noise_mult=3.0))
    data = fl.load(p)
    ch = fl.rule_changes(data)
    assert len(ch) == 1 and ch[0]["date"] == "2026-11-02" and ch[0]["changes"] == [("stop_noise_mult", 0.0, 3.0)]
    segs = fl.segments(data)
    assert [g["from"] for g in segs] == ["2026-10-30", "2026-11-02"] and sum(g["n"] for g in segs) == fl.stats(data)["n"] == 2
    md = (tmp_path / "futures_log.md").read_text(encoding="utf-8")
    assert "規則參數在中途改過" in md and "2026-11-02 日盤起：stop_noise_mult 0.0→3.0" in md
    assert "第 1 段（2026-10-30 起）" in md and "第 2 段（2026-11-02 起）" in md
    txt = fl.performance_text(data, datetime(2026, 11, 3, tzinfo=ZoneInfo("Asia/Taipei")))
    assert "規則參數在 11/02 日盤起改過（stop_noise_mult 0.0→3.0）" in txt


def _record_night(p, day_id, cfg):
    st, batches, _ = _run()
    st["date"] = day_id
    for b in batches:
        fl.record(st, b, cfg, "night", p)
    fl.refresh(st, cfg, "night", True, p)


def test_day_and_night_have_different_rules_by_design_and_are_never_flagged_against_each_other(tmp_path):
    p = tmp_path / "futures_day.json"
    for day in ("2026-10-30", "2026-11-02", "2026-11-03"):
        _record_day(p, day, dict(CFG))                                   # 日盤規則
        _record_night(p, day + "N", dict(NIGHT_CFG))                     # 夜盤本來就用不同的時段與門檻
    data = fl.load(p)
    assert data["sessions"][0]["rules"] != data["sessions"][1]["rules"]    # 日盤與夜盤的規則參數確實不同
    assert fl.rule_changes(data) == [] and len(fl.segments(data)) == 1
    assert "規則參數" not in (tmp_path / "futures_log.md").read_text(encoding="utf-8")
    _record_day(p, "2026-11-04", dict(CFG, stop_pct=0.3))                # 只有日盤改了規則
    _record_night(p, "2026-11-04N", dict(NIGHT_CFG))
    ch = fl.rule_changes(fl.load(p))
    assert [(c["date"], c["session"]) for c in ch] == [("2026-11-04", "日盤")] and ch[0]["changes"] == [("stop_pct", 0.15, 0.3)]
    segs = fl.segments(fl.load(p))
    assert [g["from"] for g in segs] == ["2026-10-30", "2026-11-04"] and [g["n"] for g in segs] == [6, 2]      # 切點日當天的日盤與夜盤都歸新的一段
