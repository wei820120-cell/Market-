"""台指期日內順勢突破訊號（market_intel/trade/futures_day.py）：用模擬的一整天報價重播。"""
from __future__ import annotations

from datetime import datetime

from market_intel.trade import futures_day as fd
from market_intel.utils import TW_TZ

CFG = dict(fd.DEFAULTS)     # 預設：1.6% 合格、停損 0.15%、日虧 3%（資金 100 萬、小台）、13:10 出一半／13:25 全出
DAY = (2026, 10, 30)


def at(hm: str) -> datetime:
    h, m = hm.split(":")
    return datetime(*DAY, int(h), int(m), tzinfo=TW_TZ)


def path(points: list[tuple[str, float]], step: int = 1) -> list[tuple[datetime, float]]:
    """依幾個（時間, 價格）轉折點，每分鐘一筆線性內插。"""
    out = []
    for (t0, p0), (t1, p1) in zip(points, points[1:]):
        a, b = at(t0), at(t1)
        n = int((b - a).total_seconds() // 60)
        for i in range(0, n, step):
            out.append((at(f"{(a.hour * 60 + a.minute + i) // 60}:{(a.hour * 60 + a.minute + i) % 60}"), p0 + (p1 - p0) * i / n))
    out.append((at(points[-1][0]), points[-1][1]))
    return out


def kinds(sigs: list[dict]) -> list[str]:
    return [s["kind"] for s in sigs]


# 多方趨勢日：開盤 20000，09:00 前區間 19960～20040，之後一路墊高、中間有兩次拉回，收盤前出場
TREND = path([("08:46", 20000), ("08:58", 19960), ("09:00", 20040), ("09:08", 20120), ("09:20", 20260), ("09:26", 20230),
              ("09:40", 20330), ("09:46", 20300), ("10:10", 20420), ("13:00", 20500), ("13:30", 20520)])


def test_streak_counts_consecutive_same_direction_days():
    assert fd.streak([100, 101, 102, 103]) == 3
    assert fd.streak([100, 99, 98, 97, 98]) == 1
    assert fd.streak([100, 99, 98, 97]) == -3
    assert fd.streak([100, 101, 101, 102]) == 1      # 平盤中斷連續
    assert fd.streak([100]) == 0 and fd.streak([]) == 0


def test_bars_group_ticks_into_five_minute_candles():
    ticks = [[at("09:00").isoformat(), 10], [at("09:03").isoformat(), 14], [at("09:04").isoformat(), 9],
             [at("09:05").isoformat(), 11], [at("09:09").isoformat(), 12]]
    b = fd.bars(ticks)
    assert [(x["key"][-4:], x["open"], x["high"], x["low"], x["close"]) for x in b] == [("0900", 10, 14, 9, 9), ("0905", 11, 12, 11, 12)]


def test_trend_day_enters_adds_on_pullback_and_scales_out_near_close():
    sigs, st = fd.replay(TREND, CFG)
    ks = kinds(sigs)
    assert ks[0] == "進場" and "做多試單" in sigs[0]["text"] and "先只試單" in sigs[0]["text"]       # 進場時盤面還沒合格 → 只試單
    assert "盤面" in ks and ks.index("盤面") > 0 and "盤面合格" in sigs[ks.index("盤面")]["text"]
    q_time = sigs[ks.index("盤面")]["time"]
    assert ks.count("加碼") >= 1 and all(s["time"] >= q_time for s in sigs if s["kind"] == "加碼")      # 盤面合格之後才加碼
    assert ks[-1] == "出場" and sigs[-1]["time"] == "13:25"
    assert st["lots"] == 0 and st["dir"] == "long" and st["realized_pts"] > 0 and st["entries"] == 1
    assert st["adds"] <= CFG["max_lots"] - 1
    assert not any(s["kind"] == "停損" for s in sigs)


def test_add_never_happens_when_losing_and_stop_exits_all():
    # 突破試單後立刻失敗：跌回成本以下 → 停損；沒有任何加碼
    t = path([("08:46", 20000), ("08:58", 19960), ("09:00", 20040), ("09:06", 20070), ("09:12", 20000), ("09:30", 19900), ("10:00", 19900)])
    sigs, st = fd.replay(t, CFG)
    assert kinds(sigs) == ["進場", "停損"] and st["lots"] == 0 and st["realized_pts"] < 0 and "加碼" not in kinds(sigs)
    assert "市價" in sigs[-1]["text"]


def test_short_side_is_mirrored_and_one_direction_per_day():
    t = path([("08:46", 20000), ("08:58", 20040), ("09:00", 19960), ("09:08", 19880), ("09:20", 19740), ("09:26", 19770),
              ("09:40", 19690), ("09:46", 19700), ("10:10", 19600), ("13:00", 19500), ("13:30", 19480)])
    sigs, st = fd.replay(t, CFG)
    assert sigs[0]["kind"] == "進場" and "做空" in sigs[0]["text"] and st["dir"] == "short" and st["realized_pts"] > 0
    assert "加碼" in kinds(sigs)


def test_no_entry_before_start_after_cutoff_or_in_a_flat_market():
    flat = path([("08:46", 20000), ("08:58", 19990), ("09:00", 20010), ("09:08", 20020), ("11:00", 20030), ("13:30", 20030)])
    assert fd.replay(flat, CFG)[0] == []                                     # 振幅不到 0.3%：小突破不算
    early = path([("08:46", 20000), ("08:50", 19900), ("08:55", 20100), ("08:59", 20200), ("09:01", 20201)])
    assert all(s["time"] >= "09:00" for s in fd.replay(early, CFG)[0] if s["kind"] == "進場")        # 09:00 之前的新高不進場
    late = path([("08:46", 20000), ("08:58", 19900), ("09:00", 20100), ("12:20", 20100), ("12:40", 20200), ("13:00", 20250)])
    sigs, _ = fd.replay(late, CFG)
    assert all(s["time"] < "12:30" for s in sigs if s["kind"] == "進場")


def test_blowoff_day_exits_on_big_jump_only_after_three_day_streak():
    t = path([("08:46", 20000), ("08:58", 19960), ("09:00", 20040), ("09:08", 20120), ("09:20", 20260), ("09:40", 20330)]) + [(at("09:41"), 20480)]
    up3 = [19500, 19700, 19900, 20000]            # 連漲 3 天
    sigs, st = fd.replay(t, CFG, up3)
    assert sigs[-1]["kind"] == "出場" and "連漲 3 天" in sigs[-1]["text"] and "不要掛限價" in sigs[-1]["text"] and st["lots"] == 0
    sigs2, st2 = fd.replay(t, CFG, [19500, 19700, 19600, 20000])     # 沒有連漲 3 天：不提早出場
    assert not any("提早出場" in s["text"] for s in sigs2) and st2["lots"] >= 1


def test_daily_loss_cap_flattens_and_halts_the_day():
    cfg = dict(CFG, capital=200_000, daily_loss_pct=0.5, stop_pct=5.0, contract_multiplier=50)     # 上限 1,000 元＝小台 20 點；停損拉遠，讓虧損上限先到
    t = path([("08:46", 20000), ("08:58", 19960), ("09:00", 20040), ("09:06", 20100), ("09:30", 20010), ("09:40", 20010)])
    sigs, st = fd.replay(t, cfg)
    assert sigs[-1]["kind"] == "停損" and "虧損已達上限" in sigs[-1]["text"] and st["halted"] and st["lots"] == 0
    assert fd.step(dict(st), at("11:00"), 20500, cfg) == []                    # halted：之後不再發任何訊號


def test_message_has_board_risk_and_calibration_note():
    sigs, st = fd.replay(TREND[:30], CFG)
    msg = fd.format_message(sigs, st, CFG)
    assert "盤面：日內振幅" in msg and "當日虧損上限 30,000 元" in msg and "微台每點 10 元" in msg
    assert msg.endswith("\n（待校準）")        # 結尾括號只留「待校準」
    assert "計畫帳" in fd.summary(st, CFG)


def test_state_roundtrip_finish_day_records_close_once(tmp_path):
    p = tmp_path / "futures_day.json"
    _, st = fd.replay(TREND, CFG)
    fd.save_state(st, p)
    assert fd.load_state("2026-10-30", p)["entries"] == 1 and fd.load_state("2026-10-31", p)["entries"] == 0   # 隔天不接續
    assert fd.finish_day(st, CFG, p)                                          # 今天有進場 → 有摘要
    assert fd.finish_day(st, CFG, p)                                          # 重跑同一天：覆蓋，不重複
    assert fd.load_closes("2026-10-31", p) == [20520.0] and fd.load_closes("2026-10-30", p) == []
    mid = fd.new_day("2026-10-30")
    mid["ticks"] = [[at("11:00").isoformat(), 20400]]
    mid["last_price"] = 20400
    assert fd.finish_day(mid, CFG, p) is None and fd.load_closes("2026-10-31", p) == [20520.0]   # 沒跑到收盤附近：不記日收盤


def test_txf_parse_picks_front_month_by_volume_and_skips_spreads():
    from market_intel.fetchers import txf
    rows = [{"SymbolID": "TXFK4-F", "CLastPrice": "20100", "CTotalVolume": "90000", "CHighPrice": "20150", "CLowPrice": "19950",
             "COpenPrice": "20000", "CDate": "20261030", "CTime": "101500"},
            {"SymbolID": "TXFL4-F", "CLastPrice": "20080", "CTotalVolume": "800", "CDate": "20261030", "CTime": "101500"},
            {"SymbolID": "TXFK4/L4-F", "CLastPrice": "-20", "CTotalVolume": "99999", "CDate": "20261030", "CTime": "101500"}]
    q = txf.parse(rows)
    assert q["symbol"] == "TXFK4-F" and q["price"] == 20100 and q["high"] == 20150 and q["low"] == 19950 and q["ts"].hour == 10
    assert txf.parse([]) is None and txf.parse([{"SymbolID": "MXFK4-F", "CLastPrice": "1"}]) is None


def test_cli_check_ignores_stale_quotes_and_pushes_to_futures_channel(tmp_path, monkeypatch):
    from market_intel import cli, notify
    from market_intel.fetchers import txf
    monkeypatch.setattr(fd, "STATE_PATH", tmp_path / "futures_day.json")
    sent = []
    monkeypatch.setattr(notify, "send", lambda text, channel="market": sent.append((channel, text)))
    st = fd.new_day("2026-10-30")
    box = {"cfg": CFG, "st": st, "closes": []}
    now = at("09:30")
    monkeypatch.setattr(txf, "fetch", lambda: {"price": 20100, "high": 20100, "low": 19960, "ts": at("09:10"), "symbol": "X", "open": 20000, "volume": 1})
    cli._futures_day_check(box, now)                       # 報價停在 20 分鐘前：不拿舊價發訊號
    assert st["ticks"] == [] and not sent
    monkeypatch.setattr(txf, "fetch", lambda: {"price": 20100, "high": 20100, "low": 19960, "ts": at("09:29"), "symbol": "X", "open": 20000, "volume": 1})
    cli._futures_day_check(box, now)                       # 第一筆：只記錄參考點（交易所今日高低），不進場
    assert len(st["ticks"]) == 1 and not sent
    monkeypatch.setattr(txf, "fetch", lambda: {"price": 20110, "high": 20110, "low": 19960, "ts": at("09:30"), "symbol": "X", "open": 20000, "volume": 1})
    cli._futures_day_check(box, now)                       # 第二筆：突破今日高點 → 試單，推到微台頻道（沒設定專屬機器人時 notify 會退回進出場機器人）
    assert sent and sent[0][0] == "futures" and "做多試單" in sent[0][1]
    assert fd.load_state("2026-10-30")["lots"] == 1


def test_micro_contract_defaults_and_unit_lots_scale_entries_adds_and_half_exit():
    assert CFG["contract"] == "微台" and CFG["contract_multiplier"] == 10 and CFG["unit_lots"] == 1
    cfg = dict(CFG, unit_lots=3, max_lots=9)          # 每次 3 口微台、滿倉最多 9 口
    sigs, st = fd.replay(TREND, cfg)
    assert "試單 3 口微台" in sigs[0]["text"] and "這一單停損約虧" in sigs[0]["text"]
    adds = [s for s in sigs if s["kind"] == "加碼"]
    assert adds and "加碼 3 口微台" in adds[0]["text"]
    half = [s for s in sigs if s["kind"] == "出場" and "分批" in s["text"]]
    assert half and "出 3 口" in half[0]["text"] or "出 6 口" in half[0]["text"]       # 以單位（3 口）為準出一半
    assert st["lots"] == 0 and st["realized_pts"] > 0
    # 損益用每點 10 元換算：訊息裡的「約 N 元」＝點數 × 口數 × 10
    ten = fd.summary(st, cfg)
    assert f"{st['realized_pts'] * 10:+,.0f} 元" in ten


# ---- 案例：使用者給的成交明細（日期不明）（大台 202611，帳號姓名已遮蔽，無法核對是誰）。
# 成交點位是回報的實際數字；走勢線是依截圖大略描繪的近似線（開 47,187／高 47,218／低 46,310／收 46,577）。
CASE_FILLS = [("09:36:41", 46757), ("09:37:43", 46735), ("09:38:15", 46721), ("09:38:35", 46666), ("09:42:29", 46667), ("09:43:20", 46648), ("09:45:55", 46628)]
CASE_PATH = [("08:46", 47187), ("08:50", 47218), ("09:00", 47170), ("09:10", 47120), ("09:15", 47060), ("09:25", 46900), ("09:36", 46757), ("09:45", 46628),
             ("09:52", 46540), ("10:00", 46500), ("10:08", 46560), ("10:15", 46510), ("10:22", 46625), ("10:30", 46560), ("10:40", 46450), ("10:48", 46540),
             ("11:00", 46500), ("11:08", 46560), ("11:20", 46420), ("11:30", 46320), ("11:31", 46310), ("11:42", 46420), ("11:50", 46460), ("12:00", 46440),
             ("12:10", 46380), ("12:20", 46360), ("12:35", 46440), ("12:50", 46500), ("13:00", 46560), ("13:15", 46660), ("13:22", 46590), ("13:30", 46540),
             ("13:45", 46577)]


def _every_20s(pts):
    from datetime import timedelta
    base = path(pts)
    out = []
    for (a, pa), (b, pb) in zip(base, base[1:]):
        out += [(a + timedelta(seconds=20 * k), pa + (pb - pa) * k / 3) for k in range(3)]
    return out + [base[-1]]


def test_case_fills_match_the_chart_numbers():
    avg = sum(p for _, p in CASE_FILLS) / len(CASE_FILLS)
    assert round(avg) == 46689 and round(avg - 46320) == 369              # 圖上的均價 46,689、+369 點／口
    assert round((47218 - 46757) / 47187 * 100, 2) == 0.98 and round((47218 - 46735) / 47187 * 100, 2) == 1.02   # 第一筆時振幅 0.98%，第二筆起 ≥ 1.0%


def test_case_day_replay_builds_the_short_pyramid_on_the_way_down_like_the_real_fills():
    cfg = dict(CFG, max_lots=7)                                           # 他是 7 口；微台預設上限只有 4
    sigs, st = fd.replay(_every_20s(CASE_PATH), cfg)
    adds = [s for s in sigs if s["kind"] == "加碼"]
    assert sigs[0]["kind"] == "進場" and "做空" in sigs[0]["text"]
    assert len(adds) == 6 and st["dir"] == "short" and st["entries"] == 1
    assert all("09:3" <= s["time"][:4] <= "09:4" or s["time"] < "09:46" for s in adds)       # 加碼集中在 09:37～09:45，和他的 09:36～09:46 一致
    assert [s["price"] for s in adds] == sorted((s["price"] for s in adds), reverse=True)      # 一路順跌加空：每筆都比前一筆低
    assert all(s["price"] > 46600 for s in adds)
    assert "停損" not in kinds(sigs) and st["lots"] == 0 and st["realized_pts"] > 0        # 反彈沒有超過平均成本，不會被停損


def test_extension_add_needs_gap_distance_qualified_and_profit():
    cfg = dict(CFG, max_lots=6)
    t = path([("08:46", 20000), ("08:58", 19960), ("09:00", 20040), ("09:06", 20100), ("09:14", 20240)])    # 試單後一路墊高，合格後順勢加碼
    sigs, st = fd.replay(t, cfg)
    adds = [s for s in sigs if s["kind"] == "加碼"]
    assert adds and all("順勢加碼" in s["text"] or "拉回" in s["text"] for s in adds) and st["lots"] <= 6
    # 同一價位再來一筆（沒有多走 0.04%）或間隔不到 15 秒：不加
    st2 = fd.new_day("2026-10-30")
    for ts, p in t[:9]:
        fd.step(st2, ts, p, cfg)
    lots = st2["lots"]
    last = datetime.fromisoformat(st2["ticks"][-1][0])
    assert fd.step(st2, last.replace(second=last.second + 5 if last.second < 50 else 59), st2["last_price"], cfg) == [] and st2["lots"] == lots


# ---------------------------------------------------------------- 夜盤（15:00–隔天 05:00，跨午夜）

NIGHT_CFG = dict(fd.DEFAULTS)
NIGHT_CFG.update(fd.NIGHT)


def n_at(hm: str, next_day: bool = False) -> datetime:
    h, m = hm.split(":")
    return datetime(2026, 10, 30 + (1 if next_day else 0), int(h), int(m), tzinfo=TW_TZ)


def n_path(points: list[tuple[str, float]]) -> list[tuple[datetime, float]]:
    """夜盤的（時間, 價格）轉折點，每分鐘一筆內插；時間小於 15:00 視為隔天凌晨。"""
    pts = [(n_at(t, int(t.split(":")[0]) < 15), p) for t, p in points]
    out = []
    for (a, p0), (b, p1) in zip(pts, pts[1:]):
        n = int((b - a).total_seconds() // 60)
        for i in range(n):
            out.append((a + (b - a) * i / n, p0 + (p1 - p0) * i / n))
    out.append(pts[-1])
    return out


def test_after_compares_across_midnight_with_session_start():
    base = "15:00"
    assert fd._after(n_at("23:30"), "15:15", base) and not fd._after(n_at("15:10"), "15:15", base)
    assert fd._after(n_at("01:00", True), "23:30", base)                 # 凌晨 01:00 已經晚於 23:30
    assert not fd._after(n_at("01:00", True), "03:30", base)             # 但還沒到 03:30
    assert fd._after(n_at("04:40", True), "04:35", base) and not fd._after(n_at("04:30", True), "04:35", base)
    assert fd._after(n_at("10:00"), "09:30") and not fd._after(n_at("09:00"), "09:30")   # 日盤（base 00:00）行為不變


def test_night_session_ids_and_window():
    sid, start, end = fd.night_session(datetime(2026, 11, 13, 2, 0, tzinfo=TW_TZ))        # 週五凌晨 → 週四開的那一場
    assert sid == "2026-11-12N" and start.hour == 15 and end == datetime(2026, 11, 13, 5, 0, tzinfo=TW_TZ)
    assert fd.night_session(datetime(2026, 11, 12, 15, 1, tzinfo=TW_TZ))[0] == "2026-11-12N"
    assert fd.night_session(datetime(2026, 11, 12, 14, 40, tzinfo=TW_TZ))[0] == "2026-11-12N"   # 開盤前 30 分鐘內算今晚
    assert fd.night_session(datetime(2026, 11, 12, 10, 0, tzinfo=TW_TZ))[0] == "2026-11-11N"    # 白天算前一晚（已收）


def test_night_settings_override_hours_and_wording_but_share_risk_settings():
    day, night = fd.settings(), fd.settings("night")
    assert day["label"] == "日內" and night["label"] == "夜盤" and night["session_start"] == "15:00"
    assert night["entry_start"] == "15:15" and night["exit_times"] == ["04:35", "04:50"]
    assert night["capital"] == day["capital"] and night["stop_pct"] == day["stop_pct"] and night["contract"] == "微台"   # 資金、停損、契約共用


def test_night_trend_enters_adds_and_scales_out_across_midnight():
    # 夜盤開 20000：15:00–15:15 區間 19960～20040；之後多方趨勢（跨午夜）、有拉回，04:35 以後分批出場
    ticks = n_path([("15:00", 20000), ("15:10", 19960), ("15:15", 20040), ("15:25", 20110), ("15:45", 20230), ("15:55", 20200),
                    ("16:30", 20260), ("16:45", 20230), ("19:00", 20300), ("23:00", 20320), ("02:00", 20340), ("04:30", 20350), ("04:55", 20360)])
    sigs, st = fd.replay(ticks, NIGHT_CFG)
    ks = kinds(sigs)
    assert ks[0] == "進場" and "做多試單" in sigs[0]["text"] and "本場只做這個方向" in sigs[0]["text"]
    assert "突破本場高點" in sigs[0]["text"]
    assert "出場" in ks and st["lots"] == 0                                   # 04:35／04:50 的分批與收盤前全出
    assert all(s["time"] >= "15:15" or s["time"] < "05:00" for s in sigs)
    msg = fd.format_message(sigs[:1], st, NIGHT_CFG)
    assert msg.startswith("📈 台指期夜盤訊號") and "盤面：夜盤振幅" in msg and "本場虧損上限" in msg and msg.endswith("（待校準）")


def test_night_no_new_entries_after_entry_until_or_before_entry_start():
    cfg = dict(NIGHT_CFG)
    early = n_path([("15:00", 20000), ("15:05", 20000), ("15:10", 20100), ("15:12", 20100)])     # 15:15 以前的突破不進場
    sigs, st = fd.replay(early, cfg)
    assert st["entries"] == 0 and not any(s["kind"] == "進場" for s in sigs)
    late = n_path([("15:00", 20000), ("15:15", 20010), ("03:29", 20012), ("03:31", 20400)])       # 03:30 以後的突破不進場
    sigs, st = fd.replay(late, cfg)
    assert st["entries"] == 0


def test_state_save_keeps_both_sessions(tmp_path):
    p = tmp_path / "futures_day.json"
    day = fd.new_day("2026-11-12")
    day["ticks"].append([at("09:00").isoformat(), 100.0]); day["last_price"] = 100.0
    night = fd.new_day("2026-11-12N")
    night["ticks"].append([n_at("15:01").isoformat(), 101.0]); night["last_price"] = 101.0
    fd.save_state(night, p, session="night")
    fd.save_state(day, p)                                                    # 存日盤不能蓋掉夜盤
    assert fd.load_state("2026-11-12N", p, "night")["last_price"] == 101.0 and fd.load_state("2026-11-12", p)["last_price"] == 100.0
    assert fd.load_state("2026-11-13N", p, "night")["ticks"] == []           # 不同場次不接續
    summary = fd.finish_night(night, NIGHT_CFG, p)
    assert summary is None and fd.load_state("2026-11-12N", p, "night")["finished"] is True
    assert fd.load_closes("2026-11-13", p) == []                             # 夜盤不記日收盤


# ---------------------------------------------------------------- 夜盤跨午夜的報價日期（2026-11-13 實測：MIS 的 CDate 過了午夜仍是開盤那天）

def test_night_quote_ts_adds_a_day_after_midnight_only():
    start = datetime(2026, 11, 12, 15, 0, tzinfo=TW_TZ)
    assert fd.night_quote_ts(datetime(2026, 11, 12, 1, 8, 0, tzinfo=TW_TZ), start) == datetime(2026, 11, 13, 1, 8, 0, tzinfo=TW_TZ)   # CDate 沒換日
    assert fd.night_quote_ts(datetime(2026, 11, 13, 1, 8, 0, tzinfo=TW_TZ), start) == datetime(2026, 11, 13, 1, 8, 0, tzinfo=TW_TZ)   # 萬一哪天換日了，不重複加
    assert fd.night_quote_ts(datetime(2026, 11, 12, 16, 30, tzinfo=TW_TZ), start) == datetime(2026, 11, 12, 16, 30, tzinfo=TW_TZ)     # 午夜前不動
    assert fd.night_quote_ts(datetime(2026, 11, 12, 14, 58, tzinfo=TW_TZ), start) == datetime(2026, 11, 12, 14, 58, tzinfo=TW_TZ)     # 開盤前 5 分鐘內的報價不動
    assert fd.night_quote_ts(None, start) is None


def _night_cli(monkeypatch, tmp_path, now, quote_ts, ticks=None, send_results=None):
    """跑一次 cmd_futures_night（--once）：報價與時間都是假的，狀態與紀錄都寫在 tmp。"""
    import argparse
    from market_intel import cli, notify
    from market_intel.trade import futures_log
    monkeypatch.setattr(fd, "STATE_PATH", tmp_path / "futures_day.json")
    monkeypatch.setattr(futures_log, "LOG_PATH", tmp_path / "futures_log.json")
    monkeypatch.setattr(futures_log, "MD_PATH", tmp_path / "futures_log.md")
    monkeypatch.setattr(cli, "now_tw", lambda: now)
    monkeypatch.setattr(cli.txf, "fetch", lambda night=False: {"symbol": "TXFK6-M", "price": 43588.0, "high": 44034.0, "low": 42371.0,
                                                           "open": 43180.0, "volume": 74520.0, "ts": quote_ts})
    sent, results = [], list(send_results or [])

    def fake_send(text, channel="market"):
        sent.append(text)
        return results.pop(0) if results else True          # send_results：依序決定每則推播成功（True）或失敗（False）
    monkeypatch.setattr(notify, "send", fake_send)
    sid, _, _ = fd.night_session(now)
    if ticks:
        st = fd.new_day(sid)
        st["ticks"], st["last_price"] = ticks, ticks[-1][1]
        fd.save_state(st, session="night")
    cli.cmd_futures_night(argparse.Namespace(status=False, force=False, no_push=False, once=True, minutes=0, interval=1))
    return fd.load_state(sid, session="night"), sent


def test_night_cli_records_ticks_after_midnight_when_mis_date_has_not_rolled(monkeypatch, tmp_path):
    now = datetime(2026, 11, 13, 1, 8, 5, tzinfo=TW_TZ)
    st, sent = _night_cli(monkeypatch, tmp_path, now, datetime(2026, 11, 12, 1, 8, 0, tzinfo=TW_TZ))   # CDate 20261112、CTime 010800
    assert st["date"] == "2026-11-12N" and len(st["ticks"]) == 1 and st["last_price"] == 43588.0
    assert not any("超過 20 分鐘" in t for t in sent)


def test_night_cli_warns_once_when_quotes_stall_for_20_minutes(monkeypatch, tmp_path):
    now = datetime(2026, 11, 13, 1, 30, 0, tzinfo=TW_TZ)
    old = [["2026-11-12T23:59:55+08:00", 43891.0]]
    stale_quote = datetime(2026, 11, 12, 0, 5, 0, tzinfo=TW_TZ)        # 補一天也還是 25 小時前 → 無效報價
    st, sent = _night_cli(monkeypatch, tmp_path, now, stale_quote, ticks=old)
    assert any("超過 20 分鐘沒有更新" in t and "23:59" in t for t in sent) and st["stale_alerted"] is True
    st, sent = _night_cli(monkeypatch, tmp_path, now, stale_quote)       # 再跑一次：已提醒過，不重複
    assert not any("超過 20 分鐘" in t for t in sent)


# ---------------------------------------------------------------- 2026-11-23 修正：推播保證、流水號、停損文字與寬度、日盤報價中斷提醒、報價存檔
# 事件：日盤 2 口多單的停損出場，計畫帳已記、聊天室卻沒有那則訊息（notify.send 失敗只記警告、不重送，帳照存）；
# 另外 11:25 做多試單 36,849、同一分鐘停損 36,788（停損 55 點，比報價雜訊還窄）。

def _fake_cli(monkeypatch, tmp_path):
    """cli 測試共用：狀態與紀錄寫在 tmp，推播換成假的。回傳 (sent, results)；results 依序決定每則推播成功（True）或失敗（False），沒給就成功。"""
    from market_intel import notify
    from market_intel.trade import futures_log
    monkeypatch.setattr(fd, "STATE_PATH", tmp_path / "futures_day.json")
    monkeypatch.setattr(futures_log, "LOG_PATH", tmp_path / "futures_log.json")
    monkeypatch.setattr(futures_log, "MD_PATH", tmp_path / "futures_log.md")
    sent, results = [], []

    def send(text, channel="market"):
        sent.append(text)
        return results.pop(0) if results else True
    monkeypatch.setattr(notify, "send", send)
    return sent, results


def _quote(monkeypatch, price, hm, high=None, low=19960):
    from market_intel.fetchers import txf
    monkeypatch.setattr(txf, "fetch", lambda: {"price": price, "high": high or price, "low": low, "ts": at(hm), "symbol": "X", "open": 20000, "volume": 1})


def test_failed_push_is_queued_then_resent_first_with_a_resend_tag(monkeypatch, tmp_path):
    from market_intel import cli
    sent, results = _fake_cli(monkeypatch, tmp_path)
    st = fd.new_day("2026-10-30")
    box = {"cfg": CFG, "st": st, "closes": []}
    _quote(monkeypatch, 20100, "09:29"); cli._futures_day_check(box, at("09:29"))      # 第一筆：只記參考點
    results.append(False)                                                               # 下一則推播失敗
    _quote(monkeypatch, 20110, "09:30"); cli._futures_day_check(box, at("09:30"))      # 突破 → 試單：計畫帳動了，但推播失敗
    assert st["lots"] == 1 and len(st["outbox"]) == 1 and st["failed"] == 1 and "做多試單" in sent[-1]
    assert fd.load_state("2026-10-30")["outbox"], "排隊中的訊號要跟著存檔（換執行也不能丟）"
    _quote(monkeypatch, 20112, "09:31"); cli._futures_day_check(box, at("09:31"))      # 下一輪：先補發，標原本的時間
    head = sent[-1].split("\n")[0]
    assert head.startswith("🔁 補發") and "09:30" in head and "做多試單" in sent[-1]
    assert st["outbox"] == [] and st["resent"] == 1


def test_resend_keeps_order_and_new_signals_never_jump_the_queue():
    st = fd.new_day("2026-10-30")
    log, ok = [], {"v": False}

    def send(text):
        log.append(text)
        return ok["v"]
    assert fd.outbox_push(st, "A", "2026-10-30T09:30:00+08:00", send) is False
    assert fd.outbox_push(st, "B", "2026-10-30T09:31:00+08:00", send) is False       # A 還沒送出：B 排在後面，不插隊
    ok["v"] = True
    assert fd.outbox_push(st, "C", "2026-10-30T09:32:00+08:00", send) is True
    assert [t.split("\n")[-1] for t in log[-3:]] == ["A", "B", "C"] and log[-3].startswith("🔁 補發") and log[-1] == "C"
    assert st["outbox"] == [] and st["failed"] == 2 and st["resent"] == 2 and fd.delivery_note(st).count("已補發 2 則") == 1


def test_a_message_that_never_goes_out_is_dropped_loudly_not_blocking_the_queue(monkeypatch):
    monkeypatch.setattr(fd, "OUTBOX_MAX_TRIES", 2)
    st = fd.new_day("2026-10-30")
    assert fd.outbox_push(st, "A", "2026-10-30T09:30:00+08:00", lambda t: False) is False
    fd.outbox_flush(st, lambda t: False)
    fd.outbox_flush(st, lambda t: False)
    assert st["outbox"] == [] and st["dropped"] == 1                                  # 放棄（log.error），不讓一則永遠失敗的擋住後面所有訊號
    assert "放棄" in fd.delivery_note(st) and fd.delivery_note(fd.new_day("2026-10-31")) == ""
    assert fd.outbox_push(fd.new_day("2026-10-30"), "A", "2026-10-30T09:30:00+08:00", lambda t: None) is True      # 舊的替身沒回傳值：當作送到了


def test_signals_carry_a_running_number_so_a_missing_one_is_visible():
    sigs, st = fd.replay(TREND, CFG)
    assert [s["seq"] for s in sigs] == list(range(1, len(sigs) + 1))
    assert "（第 1 則）" in fd.format_message(sigs[:1], st, CFG)
    assert "（第 1～2 則）" in fd.format_message(sigs[:2], st, CFG)


def test_stop_message_names_the_direction_without_a_leftover_parenthesis():
    long_loss = path([("08:46", 20000), ("08:58", 19960), ("09:00", 20040), ("09:06", 20070), ("09:12", 20000), ("09:30", 19900), ("10:00", 19900)])
    stop = [s for s in fd.replay(long_loss, CFG)[0] if s["kind"] == "停損"][0]["text"]
    assert "跌破平均成本" in stop and "（突破）" not in stop and "多滑" not in stop
    short_loss = path([("08:46", 20000), ("08:58", 20040), ("09:00", 19960), ("09:06", 19930), ("09:12", 20000), ("09:30", 20100), ("10:00", 20100)])
    stop = [s for s in fd.replay(short_loss, CFG)[0] if s["kind"] == "停損"][0]["text"]
    assert "突破平均成本" in stop and "跌破（" not in stop


def test_a_stop_that_the_price_gapped_through_reports_the_slippage():
    cfg = dict(CFG, stop_noise_mult=0.0)
    _, st = fd.replay(path([("08:46", 20000), ("08:58", 19960), ("09:00", 20040), ("09:06", 20070)]), cfg)
    assert st["lots"] == 1
    sigs = fd.step(st, at("09:07"), 19900, cfg)          # 停損約 20,009.9，下一筆報價直接跳到 19,900
    assert sigs[0]["kind"] == "停損" and "比停損價多滑 110 點" in sigs[0]["text"] and "每 20～60 秒一筆" in sigs[0]["text"]


def _noisy_entry(cfg):
    """08:55～09:05 每 20 秒一筆、價格在 20,000 與 20,080 之間來回跳（平均每筆跳約 78 點），再來一筆 20,100 突破 → 試單。"""
    from datetime import timedelta
    st, t0, sigs = fd.new_day("2026-10-30"), at("08:55"), []
    for i in range(30):
        sigs += fd.step(st, t0 + timedelta(seconds=20 * i), 20000 if i % 2 == 0 else 20080, cfg)
    sigs += fd.step(st, t0 + timedelta(seconds=20 * 30), 20100, cfg)
    return st, [s for s in sigs if s["kind"] == "進場"]


def test_stop_is_never_narrower_than_the_quote_noise():
    st, entry = _noisy_entry(CFG)
    assert entry and "比 0.15% 寬" in entry[0]["text"] and 200 < st["stop_dist"] < 260       # 約 3 × 78 點，而不是 0.15%＝30 點
    assert fd.step(st, at("09:06"), 20020, CFG) == []                                         # 回檔 80 點＝一筆報價的雜訊：不被洗出去
    off = dict(CFG, stop_noise_mult=0.0)                                                      # 關掉：舊行為，窄停損一筆就被洗出去
    st0, entry0 = _noisy_entry(off)
    assert st0["stop_dist"] is None and "比 0.15% 寬" not in entry0[0]["text"]
    assert fd.step(st0, at("09:06"), 20020, off)[0]["kind"] == "停損"


def test_calm_market_keeps_the_plain_percentage_stop():
    sigs, st = fd.replay(TREND, CFG)                      # 趨勢日（報價平滑）：雜訊下限不介入，停損仍是平均成本外 0.15%
    assert st["stop_dist"] is None and "，0.15%" in sigs[0]["text"] and "快市場會滑價" in sigs[0]["text"]


def test_day_check_warns_once_when_quotes_stall_with_a_position_and_announces_recovery(monkeypatch, tmp_path):
    from market_intel import cli
    sent, _ = _fake_cli(monkeypatch, tmp_path)
    _, st = fd.replay(path([("08:46", 20000), ("08:58", 19960), ("09:00", 20040), ("09:06", 20070)]), dict(CFG, stop_noise_mult=0.0))
    assert st["lots"] == 1
    box = {"cfg": CFG, "st": st, "closes": []}
    _quote(monkeypatch, 20070, "09:06")                                                   # 報價一直停在 09:06
    cli._futures_day_check(box, at("09:20"))                                              # 14 分鐘：還不提醒
    assert not sent
    cli._futures_day_check(box, at("09:27"))                                              # 21 分鐘：提醒一次；有持倉要講停損沒人在看
    assert len(sent) == 1 and "日盤報價超過 20 分鐘沒有更新" in sent[0] and "持有 1 口" in sent[0] and "沒有人在判斷" in sent[0]
    cli._futures_day_check(box, at("09:40"))                                              # 同一段中斷不重複提醒
    assert len(sent) == 1
    _quote(monkeypatch, 20075, "09:44"); cli._futures_day_check(box, at("09:45"))
    assert len(sent) == 2 and sent[1].startswith("✅ 日盤報價恢復") and st["stale_alerted"] is False


def test_day_check_survives_a_quote_fetch_error_and_still_resends(monkeypatch, tmp_path):
    from market_intel import cli
    from market_intel.fetchers import txf
    sent, results = _fake_cli(monkeypatch, tmp_path)
    st = fd.new_day("2026-10-30")
    st["outbox"] = [{"text": "📈 台指期日內訊號 09:30（第 1 則）\n🔺 做多試單", "ts": at("09:30").isoformat(), "tries": 1}]
    st["failed"] = 1

    def boom():
        raise OSError("MIS 連線失敗")
    monkeypatch.setattr(txf, "fetch", boom)
    cli._futures_day_check({"cfg": CFG, "st": st, "closes": []}, at("09:31"))      # 抓報價出錯：不拋例外，之前失敗的訊號照樣補發
    assert sent and sent[0].startswith("🔁 補發") and st["outbox"] == []


def test_night_cli_queues_a_failed_push_and_resends_it_on_the_next_run(monkeypatch, tmp_path):
    now = datetime(2026, 11, 13, 1, 30, 0, tzinfo=TW_TZ)
    old = [["2026-11-12T23:59:55+08:00", 43891.0]]
    stale_quote = datetime(2026, 11, 12, 0, 5, 0, tzinfo=TW_TZ)
    st, sent = _night_cli(monkeypatch, tmp_path, now, stale_quote, ticks=old, send_results=[False])      # 夜盤中斷提醒推播失敗
    assert len(st["outbox"]) == 1 and st["failed"] == 1 and "超過 20 分鐘沒有更新" in st["outbox"][0]["text"]
    st, sent = _night_cli(monkeypatch, tmp_path, now, stale_quote)                                        # 下一次執行（換執行也接續）：先補發
    assert len(sent) == 1 and sent[0].startswith("🔁 補發") and "超過 20 分鐘沒有更新" in sent[0] and st["outbox"] == [] and st["resent"] == 1


def test_finish_day_archives_the_ticks_with_exchange_high_low_and_replays_the_same(tmp_path):
    import json
    p = tmp_path / "futures_day.json"
    st = fd.new_day("2026-10-30")
    for ts, price in TREND:
        fd.step(st, ts, price, CFG, None, price + 5, price - 5)
    fd.finish_day(st, CFG, p)
    a = json.loads((tmp_path / "futures_ticks" / "2026-10-30.json").read_text(encoding="utf-8"))
    assert a["date"] == "2026-10-30" and len(a["ticks"]) == len(TREND) == len(a["exch"]) and a["exch"][0] == [TREND[0][1] + 5, TREND[0][1] - 5]
    sigs, st2 = fd.replay([(datetime.fromisoformat(i), x) for i, x in a["ticks"]], CFG, None, a["exch"])      # 用存下來的資料重播，結果要一樣
    assert (st2["entries"], st2["adds"], st2["realized_pts"]) == (st["entries"], st["adds"], st["realized_pts"])


def test_a_crashing_push_is_treated_as_a_failed_send_and_never_skips_the_ledger(monkeypatch, tmp_path):
    import json
    from market_intel import cli, notify
    from market_intel.trade import futures_log
    _fake_cli(monkeypatch, tmp_path)

    def crash(text, channel="market"):
        raise RuntimeError("推播層壞掉")
    monkeypatch.setattr(notify, "send", crash)
    st = fd.new_day("2026-10-30")
    box = {"cfg": CFG, "st": st, "closes": []}
    _quote(monkeypatch, 20100, "09:29"); cli._futures_day_check(box, at("09:29"))
    _quote(monkeypatch, 20110, "09:30"); cli._futures_day_check(box, at("09:30"))      # 突破 → 試單：推播丟例外
    assert st["lots"] == 1 and len(st["outbox"]) == 1 and st["failed"] == 1             # 排隊補發，沒有往外丟
    ledger = json.loads((tmp_path / "futures_log.json").read_text(encoding="utf-8"))
    assert any(e["kind"] == "進場" for s in ledger["sessions"] for e in s["events"])    # 計畫帳照樣記下來
    assert fd.load_state("2026-10-30")["outbox"], "排隊中的訊號有存檔"


def test_day_close_resends_pending_signals_then_sends_the_summary_with_the_delivery_note(monkeypatch, tmp_path):
    from market_intel import cli
    sent, results = _fake_cli(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "_futures_settle", lambda: None)
    _, st = fd.replay(TREND, CFG)                          # 一整天：有進場、收盤前全數出場
    st["outbox"] = [{"text": "📈 台指期日內訊號 13:25（第 9 則）\n⏰ 收盤前出場", "ts": at("13:25").isoformat(), "tries": 1}]
    st["failed"] = 1
    cli._futures_day_close({"cfg": CFG, "st": st, "closes": []})
    assert sent[0].startswith("🔁 補發") and "收盤前出場" in sent[0]                  # 先補發排隊中的
    assert "計畫帳收盤" in sent[1] and "這一場有 1 則訊號曾推播失敗：已補發 1 則" in sent[1] and st["outbox"] == []
    assert fd.load_closes("2026-10-31") == [TREND[-1][1]]                              # 日收盤照記


def test_day_close_says_so_when_the_summary_itself_cannot_be_delivered(monkeypatch, tmp_path):
    from market_intel import cli
    sent, results = _fake_cli(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "_futures_settle", lambda: None)
    _, st = fd.replay(TREND, CFG)
    results.extend([False] * 10)                           # 收盤彙總也送不出去
    cli._futures_day_close({"cfg": CFG, "st": st, "closes": []})
    assert len(st["outbox"]) == 1 and "計畫帳收盤" in st["outbox"][0]["text"] and fd.load_state("2026-10-30")["outbox"]      # 留在存檔，不是默默消失


def test_night_close_resends_pending_signals_and_appends_the_delivery_note(monkeypatch, tmp_path):
    now = datetime(2026, 11, 13, 5, 3, 0, tzinfo=TW_TZ)        # 夜盤 05:00 收盤後
    sid, start, end = fd.night_session(datetime(2026, 11, 12, 20, 0, tzinfo=TW_TZ))
    ticks = [["2026-11-12T23:59:55+08:00", 43891.0], ["2026-11-13T04:59:50+08:00", 43900.0]]
    st0 = fd.new_day(sid)
    st0.update(ticks=ticks, last_price=43900.0, entries=1, realized_pts=-30.0, failed=1,
               outbox=[{"text": "🌙 舊訊號", "ts": "2026-11-13T04:40:00+08:00", "tries": 1}])
    st, sent = None, None
    import argparse
    from market_intel import cli, notify
    from market_intel.trade import futures_log
    monkeypatch.setattr(fd, "STATE_PATH", tmp_path / "futures_day.json")
    monkeypatch.setattr(futures_log, "LOG_PATH", tmp_path / "futures_log.json")
    monkeypatch.setattr(futures_log, "MD_PATH", tmp_path / "futures_log.md")
    times = iter([datetime(2026, 11, 13, 5, 0, 30, tzinfo=TW_TZ)])           # 啟動時還沒過收盤（05:02），進了迴圈才過
    monkeypatch.setattr(cli, "now_tw", lambda: next(times, now))
    monkeypatch.setattr(cli, "_futures_settle", lambda: None)
    monkeypatch.setattr(cli.txf, "fetch", lambda night=False: None)
    sent = []
    monkeypatch.setattr(notify, "send", lambda text, channel="market": sent.append(text) or True)
    fd.save_state(st0, session="night")
    try:
        cli.cmd_futures_night(argparse.Namespace(status=False, force=False, no_push=False, once=False, minutes=0, interval=1))
    except SystemExit as e:
        assert e.code == 10                                    # 這一場已結束
    assert sent[0].startswith("🔁 補發") and "舊訊號" in sent[0]
    assert "夜盤計畫帳收盤" in sent[1] and "已補發 1 則" in sent[1]
    assert fd.load_state(sid, session="night")["finished"] is True
