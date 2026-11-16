"""系統健康日報（market_intel/health.py）：用合成的執行紀錄與狀態驗證各項檢查與推播文字。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from market_intel import health
from market_intel.utils import TW_TZ


def tw(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=TW_TZ)


def run(created: str, status="completed", conclusion="success") -> dict:
    """created：台北時間 'YYYY-MM-DD HH:MM'，轉成 GitHub 用的 UTC ISO。"""
    return {"status": status, "conclusion": conclusion if status == "completed" else None,
            "createdAt": tw(created).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "event": "schedule"}


NOW = tw("2026-11-14 08:20")      # 週六早上：上一個平日是週五 11/13


def test_prev_weekday_skips_weekend():
    assert health.prev_weekday(tw("2026-11-16 08:20")).strftime("%Y-%m-%d") == "2026-11-13"      # 週一 → 上週五
    assert health.prev_weekday(tw("2026-11-14 08:20")).strftime("%Y-%m-%d") == "2026-11-13"      # 週六 → 週五
    assert health.prev_weekday(tw("2026-11-17 08:20")).strftime("%Y-%m-%d") == "2026-11-16"


def test_interval_workflow_ok_running_and_stale():
    spec = {"name": "watch", "label": "常駐監看", "kind": "interval", "max_age_h": 3}
    assert health.check_workflow(spec, [run("2026-11-14 07:00")], NOW)["level"] == "ok"
    old = [run("2026-11-14 02:00")]                                                  # 6 小時前、沒有正在跑的 → 沒在跑
    r = health.check_workflow(spec, old, NOW)
    assert r["level"] == "bad" and "沒有在跑" in r["note"] and "6.3" in r["note"]
    assert health.check_workflow(spec, [run("2026-11-14 02:00", status="in_progress")], NOW)["level"] == "ok"   # 長時間執行中不算沒在跑


def test_failed_latest_run_is_bad_recovered_is_warn():
    spec = {"name": "daily-report", "label": "盤後報告", "kind": "interval", "max_age_h": 100}
    bad = health.check_workflow(spec, [run("2026-11-14 00:10", conclusion="failure"), run("2026-11-13 00:10")], NOW)
    assert bad["level"] == "bad" and "最新一次執行失敗" in bad["note"]
    rec = health.check_workflow(spec, [run("2026-11-14 06:00"), run("2026-11-14 00:10", conclusion="failure")], NOW)
    assert rec["level"] == "warn" and "失敗 1 次" in rec["note"] and "已恢復" in rec["note"]
    cancelled = health.check_workflow(spec, [run("2026-11-14 06:00", conclusion="cancelled")], NOW)
    assert cancelled["level"] == "ok"                                                # 取消不算失敗


def test_weekday_workflow_needs_run_on_previous_weekday_with_delay_tolerance():
    spec = {"name": "daily-report", "label": "盤後報告", "kind": "weekday"}
    assert health.check_workflow(spec, [run("2026-11-13 23:38")], NOW)["level"] == "ok"          # 15:40 的排程延遲到 23:38 仍算
    assert health.check_workflow(spec, [run("2026-11-14 00:10")], NOW)["level"] == "ok"          # 延遲過午夜（台北隔天）也算
    miss = health.check_workflow(spec, [run("2026-11-12 15:40")], NOW)                           # 只有 11/12 → 11/13 沒跑；現在還在延遲容許內
    assert miss["level"] == "warn" and "11/13 沒有執行紀錄" in miss["note"] and "延遲" in miss["note"]
    sunday = tw("2026-11-15 08:20")                                                              # 週日：已過 11/14 12:00 → 判定真的沒跑
    assert health.check_workflow(spec, [run("2026-11-12 15:40")], sunday)["level"] == "bad"


def test_market_stress_lagging_and_ok():
    assert health.check_market_stress([{"d": "2026-11-13"}], NOW)["level"] == "ok"
    r = health.check_market_stress([{"d": "2026-11-11"}], NOW)
    assert r["level"] == "warn" and "11/13" in r["note"] and "休市" in r["note"]
    assert health.check_market_stress([], NOW)["level"] == "warn"


def test_research_queue_counts_only_unresearched_and_flags_long_wait():
    q = [{"topic": "甲", "time": "2026-11-14 07:00"}, {"topic": "乙", "time": "2026-11-13 10:00"}, {"topic": "丙", "time": "2026-11-14 01:00"}]
    assert [x["topic"] for x in health.pending_topics(q, {"甲"})] == ["乙", "丙"]
    assert health.check_research_queue(q, {"甲", "乙", "丙"}, NOW) == {"level": "ok", "name": "研究佇列", "note": "沒有待研究題材"}
    r = health.check_research_queue(q, {"甲"}, NOW)                                              # 乙等了 22 小時
    assert r["level"] == "warn" and "2 題待研究" in r["note"] and "22 小時" in r["note"]
    r2 = health.check_research_queue(q[:1], set(), NOW)
    assert r2["level"] == "ok" and "1 題待研究" in r2["note"]


def test_commit_age():
    assert health.check_commit_age(NOW - timedelta(minutes=10), NOW)["level"] == "ok"
    r = health.check_commit_age(NOW - timedelta(hours=5), NOW)
    assert r["level"] == "bad" and "5.0 小時" in r["note"]
    assert health.check_commit_age(None, NOW)["level"] == "warn"


def ticks(*pairs) -> list:
    return [[tw(t).isoformat(timespec="seconds"), p] for t, p in pairs]


def test_find_gaps_over_twenty_minutes():
    t = ticks(("2026-11-13 23:50", 1), ("2026-11-13 23:55", 1), ("2026-11-14 00:30", 1), ("2026-11-14 00:35", 1))   # 夜盤跨午夜卡 35 分鐘
    g = health.find_gaps(t)
    assert len(g) == 1 and g[0][0].strftime("%H:%M") == "23:55" and g[0][1].strftime("%H:%M") == "00:30"
    assert health.find_gaps(ticks(("2026-11-13 09:00", 1), ("2026-11-13 09:20", 1))) == []                           # 剛好 20 分鐘不算


def test_session_ticks_gap_end_and_date_handling():
    end = tw("2026-11-14 05:00")
    ok = {"date": "2026-11-13N", "ticks": ticks(("2026-11-13 15:00", 1), ("2026-11-13 15:05", 1), ("2026-11-14 04:55", 1))}
    # 15:05 → 04:55 隔很久，視為中斷
    r = health.check_session_ticks("夜盤", ok, "2026-11-13", end)
    assert r["level"] == "warn" and "報價中斷 1 次" in r["note"]
    full = {"date": "2026-11-13N", "ticks": ticks(*[(f"2026-11-13 {h:02d}:{m:02d}", 1) for h in range(15, 24) for m in range(0, 60, 10)]
                                                  + [(f"2026-11-14 {h:02d}:{m:02d}", 1) for h in range(0, 5) for m in range(0, 60, 10)])}
    r2 = health.check_session_ticks("夜盤", full, "2026-11-13", end)
    assert r2["level"] == "ok" and "筆報價" in r2["note"]
    early = {"date": "2026-11-13N", "ticks": ticks(("2026-11-13 15:00", 1), ("2026-11-13 15:10", 1), ("2026-11-14 00:00", 1))}   # 00:00 就停了
    assert health.check_session_ticks("夜盤", early, "2026-11-13", end)["level"] in ("bad", "warn")
    stopped = {"date": "2026-11-13N", "ticks": ticks(("2026-11-13 15:00", 1), ("2026-11-13 15:10", 1))}
    r3 = health.check_session_ticks("夜盤", stopped, "2026-11-13", end)
    assert r3["level"] == "bad" and "離收盤" in r3["note"]
    assert health.check_session_ticks("夜盤", {**stopped, "finished": True}, "2026-11-13", end)["level"] == "ok"        # 已正常收盤
    assert health.check_session_ticks("夜盤", {"date": "2026-11-14N", "ticks": []}, "2026-11-13", end)["level"] == "ok"  # 新場次已開始 → 略過
    assert health.check_session_ticks("夜盤", {"date": "2026-11-12N", "ticks": ticks(("2026-11-12 15:00", 1))}, "2026-11-13", end)["level"] == "warn"
    assert health.check_session_ticks("夜盤", None, "2026-11-13", end)["level"] == "warn"
    assert health.check_session_ticks("夜盤", {"date": "2026-11-13N", "ticks": []}, "2026-11-13", end)["level"] == "bad"


def test_render_all_ok_and_problem_lists():
    ok = [{"level": "ok", "name": "甲", "note": "正常"}, {"level": "ok", "name": "乙", "note": "正常"}]
    assert health.render(ok, NOW) == "🩺 系統健康日報 11/14（週六）\n✅ 2 項檢查全部正常"
    bad = health.render(ok + [{"level": "bad", "name": "盤後報告", "note": "最新一次執行失敗"}, {"level": "warn", "name": "研究佇列", "note": "2 題待研究"}], NOW)
    lines = bad.splitlines()
    assert lines[1] == "❌ 1 項異常、⚠️ 1 項注意、✅ 2 項正常"
    assert lines[2] == "❌ 盤後報告：最新一次執行失敗" and lines[3] == "⚠️ 研究佇列：2 題待研究" and lines[4] == "✅ 正常：甲、乙"       # 異常在前、正常收成一行
    only_warn = health.render(ok + [{"level": "warn", "name": "丙", "note": "x"}], NOW)
    assert only_warn.splitlines()[1] == "⚠️ 1 項注意、✅ 2 項正常"


def test_collect_with_fake_runs_and_state_files(tmp_path):
    (tmp_path / "state" / "research_queue").mkdir(parents=True)
    (tmp_path / "research").mkdir()
    (tmp_path / "state" / "market_stress.json").write_text(json.dumps({"days": [{"d": "2026-11-13"}]}), encoding="utf-8")
    (tmp_path / "research" / "index.json").write_text(json.dumps({"已研究": {}}), encoding="utf-8")
    (tmp_path / "state" / "research_queue" / "a.json").write_text(json.dumps({"topic": "已研究", "time": "2026-11-14 01:00"}), encoding="utf-8")
    (tmp_path / "state" / "research_queue" / "b.json").write_text(json.dumps({"topic": "新題材", "time": "2026-11-14 07:00"}), encoding="utf-8")
    (tmp_path / "state" / "futures_day.json").write_text(json.dumps({"today": None, "night": None}), encoding="utf-8")
    runs = {"watch": [run("2026-11-14 07:30")], "podcast": [run("2026-11-14 06:00")], "realtime": [run("2026-11-13 08:36")],
            "realtime-night": [run("2026-11-13 14:50")], "daily-report": [run("2026-11-14 00:10", conclusion="failure")]}
    checks = health.collect(tmp_path, NOW, runs_fn=lambda n: runs[n], commit_fn=lambda root: NOW - timedelta(minutes=5))
    by = {c["name"]: c for c in checks}
    assert by["盤後報告"]["level"] == "bad" and by["常駐監看"]["level"] == "ok" and by["日盤即時"]["level"] == "ok"
    assert by["研究佇列"]["note"].startswith("1 題待研究") and by["市場流動性紀錄"]["level"] == "ok"
    assert by["期貨日盤報價"]["level"] == "warn" and by["監看存檔"]["level"] == "ok"                      # 沒有場次存檔 → 提醒
    empty = health.collect(tmp_path, NOW, runs_fn=lambda n: [], commit_fn=lambda root: None)
    assert any("查不到執行紀錄" in c["note"] for c in empty)                                              # gh 失敗不崩，標查不到


def test_cmd_health_dry_prints_and_does_not_send(tmp_path, monkeypatch, capsys):
    import argparse
    from market_intel import cli, notify
    sent = []
    monkeypatch.setattr(notify, "send", lambda text, channel="market": sent.append((channel, text)))
    monkeypatch.setattr(health, "collect", lambda root, now: [{"level": "ok", "name": "甲", "note": "正常"}])
    cli.cmd_health(argparse.Namespace(dry=True, channel="market"))
    assert "全部正常" in capsys.readouterr().out and sent == []
    cli.cmd_health(argparse.Namespace(dry=False, channel="market"))
    assert len(sent) == 1 and sent[0][0] == "market" and "系統健康日報" in sent[0][1]


# ---------------------------------------------------------------- 微台績效推播（futures_log.performance_text / cmd_futures_report）

def _sess(date, session, twd, entries=1, source="即時", finished=True, direction="long", lots_left=0):
    return {"date": date, "session": session, "source": source, "finished": finished, "events": [],
            "summary": {"dir": direction, "entries": entries, "adds": 0, "lots_left": lots_left, "pts": twd / 10, "twd": twd}}


def test_performance_text_counts_done_sessions_and_lists_recent():
    from market_intel.trade import futures_log as fl
    data = {"sessions": [_sess("2026-11-09", "day", 22540, source="補錄"), _sess("2026-11-10", "day", -1380, source="補錄"),
                         _sess("2026-11-11", "day", 0), _sess("2026-11-12", "day", -750),
                         _sess("2026-11-13", "night", -2250, finished=False)]}       # 夜盤沒標收盤，但 11/14 05:00 已過 → 算結束
    text = fl.performance_text(data, tw("2026-11-16 14:30"), last_n=3)
    assert "累計 5 場有進場：賺 1／賠 3／平 1" in text and "合計 +1,816 點（+18,160 元）" in text
    assert "勝率 25%" in text and "平均賺 +22,540 元" in text and "平均賠 -1,460 元" in text
    assert "最近 3 場：" in text and text.index("11/13 夜盤") < text.index("11/12 日盤") < text.index("11/11 日盤")
    assert "11/10" not in text.split("最近 3 場：")[1].split("註：")[0]            # 只列最近 3 場
    assert "註：2 場標「補錄」" in text and "不是真實成交" in text


def test_performance_text_running_session_and_live_snapshot_replace():
    from market_intel.trade import futures_log as fl
    data = {"sessions": [_sess("2026-11-13", "day", -2760), _sess("2026-11-16", "day", 100, finished=False, lots_left=2)]}
    live = [{"date": "2026-11-16", "session": "day", "summary": {"dir": "short", "entries": 2, "adds": 1, "lots_left": 3, "pts": 85.0, "twd": 850}}]
    text = fl.performance_text(data, tw("2026-11-16 11:00"), live)                 # 11:00：日盤還沒結束
    assert "累計 1 場有進場：賺 0／賠 1" in text and "進行中：" in text
    assert "11/16 日盤 做空 +85 點（+850 元），未平 3 口" in text                    # 用最新快照，不是紀錄裡過期的 +100
    assert "+100 點" not in text
    after = fl.performance_text(data, tw("2026-11-16 14:00"), live)                # 13:35 後算結束 → 進入累計
    assert "累計 2 場有進場" in after and "進行中" not in after
    extra = fl.performance_text({"sessions": []}, tw("2026-11-16 14:00"), [{"date": "2026-11-16", "session": "night", "summary": {"dir": "long", "entries": 1, "adds": 0, "lots_left": 1, "pts": 10.0, "twd": 100}}])
    assert "還沒有已結束且有進場的場次" in extra                                    # 紀錄裡沒有、存檔有進場的進行中場次：不算累計


def test_cmd_futures_report_dry_and_send(monkeypatch, capsys):
    import argparse
    from market_intel import cli, notify
    from market_intel.trade import futures_day, futures_log
    sent = []
    monkeypatch.setattr(notify, "send", lambda text, channel="market": sent.append((channel, text)))
    monkeypatch.setattr(futures_log, "load", lambda path=None: {"sessions": [_sess("2026-11-12", "day", 12730)]})
    monkeypatch.setattr(futures_day, "_read_all", lambda path: {})                   # 沒有進行中的場次
    cli.cmd_futures_report(argparse.Namespace(dry=True, last=8))
    assert "微台計畫帳績效" in capsys.readouterr().out and sent == []
    cli.cmd_futures_report(argparse.Namespace(dry=False, last=8))
    assert len(sent) == 1 and sent[0][0] == "trade" and "+12,730 元" in sent[0][1]
