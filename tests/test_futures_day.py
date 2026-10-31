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


def test_message_has_board_risk_and_no_order_disclaimer():
    sigs, st = fd.replay(TREND[:30], CFG)
    msg = fd.format_message(sigs, st, CFG)
    assert "盤面：日內振幅" in msg and "當日虧損上限 30,000 元" in msg and "不是下單" in msg and "微台每點 10 元" in msg
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


def test_cli_check_ignores_stale_quotes_and_pushes_to_trade_channel(tmp_path, monkeypatch):
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
    cli._futures_day_check(box, now)                       # 第二筆：突破今日高點 → 試單，推到出進場機器人
    assert sent and sent[0][0] == "trade" and "做多試單" in sent[0][1]
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


# ---- 案例：使用者給的 10/30 成交明細（大台 202611，帳號姓名已遮蔽，無法核對是誰）。
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
