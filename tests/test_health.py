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
    assert len(sent) == 1 and sent[0][0] == "futures" and "+12,730 元" in sent[0][1]


# ---------------------------------------------------------------- 微台機器人：頻道退回規則與指令回覆

def test_futures_channel_falls_back_to_trade_then_own_bot(monkeypatch):
    from market_intel import notify
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_TRADE_BOT_TOKEN", "TELEGRAM_TRADE_CHAT_ID",
              "TELEGRAM_FUTURES_BOT_TOKEN", "TELEGRAM_FUTURES_CHAT_ID"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "market-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "111")
    assert notify._telegram("futures") == ("market-token", "111")              # 什麼都沒設：盤勢機器人
    monkeypatch.setenv("TELEGRAM_TRADE_BOT_TOKEN", "trade-token")
    assert notify._telegram("futures") == ("trade-token", "111")               # 有進出場機器人：退回它（Chat ID 沿用）
    monkeypatch.setenv("TELEGRAM_FUTURES_BOT_TOKEN", "futures-token")
    assert notify._telegram("futures") == ("futures-token", "111")             # 有微台機器人：用它，Chat ID 沒填沿用 TELEGRAM_CHAT_ID
    monkeypatch.setenv("TELEGRAM_FUTURES_CHAT_ID", "222")
    assert notify._telegram("futures") == ("futures-token", "222")
    assert notify._telegram("trade") == ("trade-token", "111")                  # 進出場機器人不受影響


def test_futures_reply_commands():
    from market_intel.trade import futures_log as fl
    ev = [{"ts": None, "time": "09:20", "kind": "進場", "price": 44000.0, "text": "🔻 做空試單", "lots_after": 1, "pts_after": 0.0},
          {"ts": None, "time": "09:40", "kind": "加碼", "price": 43800.0, "text": "➕ 順勢加碼", "lots_after": 2, "pts_after": 200.0}]
    data = {"sessions": [{**_sess("2026-11-13", "day", -2760), "events": []},
                         {**_sess("2026-11-16", "day", 59570, direction="short"), "events": ev}]}
    now = tw("2026-11-16 14:30")
    assert "微台機器人指令" in fl.reply("說明", data, now) and fl.reply("/start", data, now) == fl.HELP
    assert "微台計畫帳績效" in fl.reply("績效", data, now) and fl.reply(" 微台 ", data, now) == fl.reply("績效", data, now)
    today = fl.reply("今天", data, now)
    assert "今天的場次" in today and "11/16 日盤 做空 +5,957 點（+59,570 元）" in today and "11/13" not in today
    detail = fl.reply("明細", data, now)
    assert "11/16 日盤 觸發紀錄" in detail and "09:20 44,000｜🔻 做空試單" in detail and "09:40 43,800｜➕ 順勢加碼" in detail
    assert "看不懂" in fl.reply("買一口台積電", data, now)                      # 不會把亂打的字當成下單
    assert "還沒有進場紀錄" in fl.reply("今天", {"sessions": [_sess("2026-11-13", "day", 1)]}, now)
    assert fl.reply("明細", {"sessions": []}, now) == "還沒有觸發紀錄。"


def test_futures_commands_ignore_other_chats_and_reply_to_owner(monkeypatch):
    from market_intel import cli, notify, research
    from market_intel.trade import futures_log
    sent, saved = [], {}
    monkeypatch.setenv("TELEGRAM_FUTURES_BOT_TOKEN", "tok")
    monkeypatch.setattr(research, "load_offset", lambda token: 10)
    monkeypatch.setattr(research, "save_offset", lambda token, off: saved.update({token: off}))
    monkeypatch.setattr(notify, "owner_chat", lambda ch: "111")
    monkeypatch.setattr(notify, "get_updates", lambda ch, off: [
        {"update_id": 10, "message": {"chat": {"id": 999}, "text": "績效"}},           # 別人的訊息：不回
        {"update_id": 11, "message": {"chat": {"id": 111}, "text": "說明"}}])
    monkeypatch.setattr(notify, "send", lambda text, channel="market": sent.append((channel, text)))
    monkeypatch.setattr(futures_log, "load", lambda path=None: {"sessions": []})
    monkeypatch.setattr(cli, "_futures_live", lambda: [])
    cli._futures_commands()
    assert saved == {"tok": 12} and len(sent) == 1 and sent[0][0] == "futures" and "微台機器人指令" in sent[0][1]
    monkeypatch.delenv("TELEGRAM_FUTURES_BOT_TOKEN")
    monkeypatch.setattr(notify, "get_updates", lambda ch, off: (_ for _ in ()).throw(AssertionError("沒有金鑰不該去讀訊息")))
    cli._futures_commands()                                                         # 沒有專屬機器人金鑰：直接略過


# ---------------------------------------------------------------- 微台損益總結、每月結算、年度績效表

def _live_state(date, realized=0.0, entries=1, lots=0, avg=None, last=None, direction="long"):
    from market_intel.trade import futures_day
    st = futures_day.new_day(date)
    st.update(entries=entries, realized_pts=realized, lots=lots, avg=avg, dir=direction, last_price=last)
    return st


def test_pnl_line_totals_replace_current_session_and_split_month_year(tmp_path):
    from market_intel.trade import futures_day, futures_log as fl
    cfg = futures_day.settings()
    path = tmp_path / "futures_day.json"
    data = {"sessions": [_sess("2025-12-30", "day", 1000), _sess("2026-10-30", "day", 0),
                         _sess("2026-11-12", "day", -750), _sess("2026-11-13N", "night", -2250),
                         _sess("2026-11-17", "day", 9999, finished=False)]}                       # 這一場紀錄裡過期的 9999：不能再算一次
    fl.save(data, path)
    line = fl.pnl_line(_live_state("2026-11-17", realized=50.0), cfg, "day", path=path)
    assert "💰 損益總結（模擬）：本場 +50 點（+500 元）" in line and "9,999" not in line
    assert "本月累計 -250 點（-2,500 元）" in line                 # 11 月：-750 -2250 +500
    assert "今年累計 -250 點（-2,500 元）" in line                 # 2026 年：10/30 的 0 與去年 12 月的 +1000 不混進來
    assert "含未平" not in line
    openl = fl.pnl_line(_live_state("2026-11-17", lots=2, avg=44000.0, last=44100.0), cfg, "day", path=path)
    assert "含未平 2 口浮動" in openl
    night = fl.pnl_line(_live_state("2026-11-17N", realized=-10.0, direction="short"), cfg, "night", path=path)
    assert "本場 -10 點（-100 元）" in night                       # 夜盤是另一場：日盤那筆（紀錄裡的 9999）照算，不被夜盤蓋掉
    assert fl.pnl_line({"date": "bad"}, cfg, "day", path=path) == ""     # 算不出來：空字串，不擋訊號


def test_format_message_puts_pnl_between_board_and_risk():
    from market_intel.trade import futures_day
    cfg = futures_day.settings()
    sig = [{"time": "09:20", "text": "🔺 做多試單", "kind": "進場", "price": 44000.0}]
    st = _live_state("2026-11-17")
    plain = futures_day.format_message(sig, st, cfg)
    withp = futures_day.format_message(sig, st, cfg, "💰 損益總結（模擬）：本場 +1 點（+10 元）")
    assert "損益總結" not in plain
    lines = withp.splitlines()
    assert lines[-4].startswith("盤面：") and lines[-3].startswith("💰 損益總結") and lines[-2].startswith("風控：") and lines[-1] == "（待校準）"


def _month_data():
    return {"sessions": [_sess("2026-10-30", "day", 0), _sess("2026-11-09", "day", 22540, source="補錄"),
                         _sess("2026-11-10", "day", -1380, source="補錄"), _sess("2026-11-12", "day", -750),
                         _sess("2026-11-13N", "night", 0, entries=0),
                         _sess("2026-12-01", "day", 1000)]}


def test_monthly_and_yearly_rows_and_md():
    from market_intel.trade import futures_log as fl
    now = tw("2026-12-02 10:00")
    rows = {m["month"]: m for m in fl.monthly(_month_data(), now)}
    nov = rows["2026-11"]
    assert (nov["n"], nov["win"], nov["lose"], nov["flat"], nov["twd"]) == (3, 1, 2, 0, 20410) and nov["pts"] == 2041.0
    assert nov["max_dd"] == -2130 and nov["best"] == {"tag": "11/09 日盤", "twd": 22540} and nov["worst"] == {"tag": "11/10 日盤", "twd": -1380}
    assert rows["2026-10"]["flat"] == 1 and rows["2026-12"]["twd"] == 1000          # 沒進場的 11/13 夜盤不算場次
    y = fl.yearly(_month_data(), now)[0]
    assert y["year"] == "2026" and y["n"] == 5 and y["twd"] == 21410 and [m["month"] for m in y["months"]] == ["2026-10", "2026-11", "2026-12"]
    assert y["best_month"] == {"month": "2026-11", "twd": 20410} and y["worst_month"] == {"month": "2026-10", "twd": 0}
    md = fl.render_yearly_md(_month_data(), now)
    assert "## 年度績效表" in md and "| 2026 | 5 |" in md and "## 2026 年每月結算" in md
    assert "| 2026-11 | 3 | 1／2／0 | 33% |" in md and "已結算" in md and "進行中（尚未結算）" in md          # 12 月還在進行
    assert "還沒有已結束" in fl.render_yearly_md({"sessions": []}, now)
    unfinished = fl.monthly({"sessions": [_sess("2026-12-01", "day", 500, finished=False)]}, tw("2026-12-01 11:00"))
    assert unfinished == []                                                          # 進行中的場次收盤後才計入月結


def test_settle_once_skips_early_and_current_months_and_waits_for_running_night():
    from market_intel.trade import futures_log as fl
    data = _month_data()
    assert fl.settle(data, tw("2026-11-30 20:00")) == [] and "settled" not in data          # 還在 11 月：不結
    texts = fl.settle(data, tw("2026-12-02 10:00"))
    assert len(texts) == 1 and "2026 年 11 月結算" in texts[0] and "共 3 場有進場：賺 1／賠 2／平 0，勝率 33%" in texts[0]
    assert "+2,041 點（+20,410 元）" in texts[0] and "最佳一場 11/09 日盤 +22,540 元" in texts[0] and "最差一場 11/10 日盤 -1,380 元" in texts[0]
    assert "今年累計" in texts[0] and "不是投資建議" in texts[0]
    assert data["settled"] == ["2026-11"] and fl.settle(data, tw("2026-12-02 10:00")) == []   # 10 月（測試紀錄）不結、11 月只結一次
    late = {"sessions": [_sess("2026-11-09", "day", 100), _sess("2026-11-30N", "night", 50, finished=False)]}
    assert fl.settle(late, tw("2026-12-01 04:00")) == []                              # 月底夜盤還沒收（12/1 05:00 才結束）：先不結
    assert len(fl.settle(late, tw("2026-12-01 05:30"))) == 1


def test_futures_replies_for_month_and_year():
    from market_intel.trade import futures_log as fl
    now = tw("2026-12-02 10:00")
    month = fl.reply("月結", _month_data(), now)
    assert "每月結算" in month and "2026-12（進行中）" in month and "2026-11：3 場" in month and "+20,410 元" in month
    year = fl.reply("年度", _month_data(), now)
    assert "2026 年：5 場" in year and "月別：10 月 +0；11 月 +20,410；12 月 +1,000" in year and "最佳月 2026-11" in year
    assert fl.reply("年報", _month_data(), now) == year and fl.reply("月報", _month_data(), now) == month
    assert "還沒有已結束" in fl.reply("年度", {"sessions": []}, now) and "月結" in fl.HELP and "年度" in fl.HELP


def test_cmd_futures_settle_pushes_once_and_report_views(monkeypatch, capsys):
    import argparse
    from market_intel import cli, notify
    from market_intel.trade import futures_day, futures_log
    data, sent = _month_data(), []
    monkeypatch.setattr(futures_log, "load", lambda path=None: data)
    monkeypatch.setattr(futures_log, "save", lambda d, path=None: None)
    monkeypatch.setattr(notify, "send", lambda text, channel="market": sent.append((channel, text)))
    monkeypatch.setattr(cli, "now_tw", lambda: tw("2026-12-02 14:00"))
    cli._futures_settle()
    assert len(sent) == 1 and sent[0][0] == "futures" and "11 月結算" in sent[0][1]
    cli._futures_settle()
    assert len(sent) == 1                                                            # 已記在 settled，不會重複推
    monkeypatch.setattr(futures_day, "_read_all", lambda path: {})
    cli.cmd_futures_report(argparse.Namespace(dry=True, last=8, view="year"))
    assert "年度績效" in capsys.readouterr().out
    cli.cmd_futures_report(argparse.Namespace(dry=False, last=8, view="month"))
    assert len(sent) == 2 and sent[1][0] == "futures" and "每月結算" in sent[1][1]


def test_get_updates_logs_why_telegram_refused_without_printing_token(monkeypatch, caplog):
    import logging
    import requests
    from market_intel import notify

    class Resp:
        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    monkeypatch.setenv("TELEGRAM_FUTURES_BOT_TOKEN", "123:SECRET")
    monkeypatch.setattr(requests, "get", lambda *a, **k: Resp({"ok": False, "error_code": 401, "description": "Unauthorized"}))
    with caplog.at_level(logging.WARNING):
        assert notify.get_updates("futures", None) == []
    assert "401 Unauthorized" in caplog.text and "SECRET" not in caplog.text              # 金鑰失效：日誌看得到原因
    monkeypatch.setattr(requests, "get", lambda *a, **k: Resp({"ok": True, "result": [{"update_id": 1}]}))
    assert notify.get_updates("futures", None) == [{"update_id": 1}]

    def boom(*a, **k):
        raise requests.ConnectionError("https://api.telegram.org/bot123:SECRET/getUpdates failed")

    caplog.clear()
    monkeypatch.setattr(requests, "get", boom)
    with caplog.at_level(logging.WARNING):
        assert notify.get_updates("futures", None) == []
    assert "ConnectionError" in caplog.text and "SECRET" not in caplog.text               # 連線錯誤不把含金鑰的網址寫進日誌


def test_clean_token_strips_paste_mistakes(monkeypatch):
    from market_intel import notify
    for raw in ("123:ABC", " 123:ABC\n", '"123:ABC"', "'123:ABC' ", "bot123:ABC", "“123:ABC”"):
        assert notify._clean(raw) == "123:ABC", raw
    assert notify._clean("   ") is None and notify._clean(None) is None and notify._clean("bottle:1") == "bottle:1"
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_TRADE_BOT_TOKEN", "TELEGRAM_TRADE_CHAT_ID",
              "TELEGRAM_FUTURES_BOT_TOKEN", "TELEGRAM_FUTURES_CHAT_ID"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:market\n")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", " 111 ")
    monkeypatch.setenv("TELEGRAM_FUTURES_BOT_TOKEN", " 3:futures ")
    assert notify._telegram("futures") == ("3:futures", "111")                           # 前後空白、換行不會讓金鑰變成 404


def test_token_hint_reports_shape_without_leaking_secret(monkeypatch):
    from market_intel import notify
    monkeypatch.delenv("TELEGRAM_FUTURES_BOT_TOKEN", raising=False)
    assert "沒有設定或是空的" in notify.token_hint("futures") and "trade" in notify.token_hint("futures")
    good = "8702087270:" + "A" * 35
    monkeypatch.setenv("TELEGRAM_FUTURES_BOT_TOKEN", good)
    hint = notify.token_hint("futures")
    assert "格式正確" in hint and "機器人編號 8702087270" in hint and "原始長度 46" in hint and "前後空白 無" in hint
    assert "AAAA" not in hint                                                               # 只印長度與機器人編號，不印金鑰內容
    monkeypatch.setenv("TELEGRAM_FUTURES_BOT_TOKEN", good + "\n")
    assert "格式正確" in notify.token_hint("futures") and "前後空白 有" in notify.token_hint("futures")
    monkeypatch.setenv("TELEGRAM_FUTURES_BOT_TOKEN", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    assert "格式不對" in notify.token_hint("futures") and "沒有冒號" in notify.token_hint("futures")


def test_send_falls_back_when_bot_token_rejected(monkeypatch, caplog):
    import logging
    from market_intel import notify

    class Resp:
        def __init__(self, code, text=""):
            self.status_code, self.text, self.ok = code, text, code < 400

    calls = []

    def fake_post(url, json=None, timeout=None):
        tok = url.split("/bot")[1].split("/")[0]
        calls.append((tok, json["chat_id"]))
        return Resp(404, '{"description":"Not Found"}') if tok == "3:bad" else Resp(200)

    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_TRADE_BOT_TOKEN", "TELEGRAM_TRADE_CHAT_ID",
              "TELEGRAM_FUTURES_BOT_TOKEN", "TELEGRAM_FUTURES_CHAT_ID"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:market")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "111")
    monkeypatch.setenv("TELEGRAM_TRADE_BOT_TOKEN", "2:trade")
    monkeypatch.setenv("TELEGRAM_FUTURES_BOT_TOKEN", "3:bad")
    monkeypatch.setattr(notify.requests, "post", fake_post)
    with caplog.at_level(logging.WARNING):
        notify.send("📈 訊號", channel="futures")
    assert calls == [("3:bad", "111"), ("2:trade", "111")]                                # 微台機器人金鑰無效：改用進出場機器人，訊號不掉
    assert "金鑰無效" in caplog.text and "TELEGRAM_FUTURES_BOT_TOKEN" in caplog.text and "3:bad" not in caplog.text
    calls.clear()
    monkeypatch.setattr(notify.requests, "post", lambda url, json=None, timeout=None: (calls.append(url) or Resp(400, "chat not found")))
    notify.send("x", channel="futures")
    assert len(calls) == 1                                                                 # 400（Chat ID 錯）不是金鑰問題：不換機器人重送
