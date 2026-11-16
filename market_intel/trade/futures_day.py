"""台指期日內順勢突破：照 @pingyunhuang 的做法（knowledge/methods/pingyunhuang-台指期突破加碼.md）算「何時進場、加碼、出場」。

只在盤面合格時叫你動作；只發訊號、不下單。規則（原文點數是十幾年前的位階，這裡全部換成「占價格的 %」，預設值按當年加權約 7,500 點換算，等交易明細校準）：
- 一天只做一個方向：價格突破「今日高點」→ 多方、跌破「今日低點」→ 空方；第一次突破先試單（1 口）。
- 加碼：只有「盤面合格」（日內高低差已達 active_range_pct，真正行情）、而且已經獲利（價格在平均成本有利的一邊）、口數還沒到上限，
  兩種時機：①剛收完一根 5 分 K 拉回（做多＝收黑 K）而且那根 K 沒碰到平均成本（拉回加碼）；②價格又創日內新高／新低、比上一筆成交再多走 ext_add_pct
  （順勢加碼：使用者給的成交明細就是順著跌勢約每 14～55 點加空 1 口）。賠錢時絕不加碼。
- 停損：跌破（做空則突破）平均成本一點點（stop_pct）就全出；當日虧損達資金的 daily_loss_pct 也全出、當天不再做。
- 出場：預設抱到快收盤才分批（exit_times：第一次出一半、第二次全出）；日線連漲／連跌 3 天以上的日子，
  5 分 K 振幅過大（bar_range_pct）或跳動過大（tick_jump_pct）就提早全出。出場一律用市價（範圍市價），不要掛限價。
資料是每約 20 秒一筆的報價（期交所 MIS），5 分 K 由這些報價自己組。"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

from .. import config

log = logging.getLogger(__name__)

STATE_PATH = Path(__file__).resolve().parents[2] / "state" / "futures_day.json"
DEFAULTS = {
    "enabled": True,
    "contract": "微台",              # 操作的契約名稱（只用在訊息文字）；報價一律用成交量最大的台指期近月（TXF）當訊號來源，微台與它同價同步
    "contract_multiplier": 10,      # 每點多少元：微台 10、小台 50、大台 200（用在損益與「當日虧損上限」的換算）
    "capital": 1_000_000,           # 台指期的操作資金（原文：至少 100 萬）
    "unit_lots": 1,                 # 每次進場／加碼幾口（微台每口只有大台的 1/20，想放大請調這裡）
    "max_lots": 4,                  # 滿倉口數上限（原文：單筆 1 口，新手從 1:4 開始，他自己 1:120；微台一口很小，要放大比值就調高）
    "active_range_pct": 1.0,        # 日內高低差 ≥ 此 % 才算「真正行情」、才准加碼（原文 120 點；依使用者給的成交明細（日期不明）校準：第一筆後約在振幅 1.0% 才開始連續加碼）
    "early_trend_pct": 0.9,         # 09:30 前就達到 → 訊息標示「早盤動能強」（原文 9 點半超過 100 點）
    "ext_add_pct": 0.04,            # 順勢加碼：價格再創日內新高／新低、且比上一筆成交又多走這麼多 %（他的實際成交：每約 14～55 點加 1 口，約 0.03～0.12%）
    "ext_add_gap_sec": 15,          # 順勢加碼：兩次加碼至少隔幾秒（他最密的兩筆相隔 20 秒）
    "bar_range_pct": 1.07,          # 斷頭日型：5 分 K 高低差 ≥ 此 %（原文 80 點）
    "tick_jump_pct": 0.27,          # 斷頭日型：相鄰兩筆報價跳動 ≥ 此 %（原文 20 點）
    "stop_pct": 0.15,               # 跌破平均成本多少 % 全出（原文只說依均價設停損，沒給數字：待交易明細校準）
    "daily_loss_pct": 3.0,          # 當日虧損上限（占資金 %）：原文 3～5%
    "entry_start": "09:00",         # 開盤前 15 分鐘先建立今日高低點
    "entry_until": "12:30",         # 這之後的突破不進場（原文沒說，保守設定）
    "exit_times": ["13:10", "13:25"],   # 第一次出一半、第二次全出（盤中監控約 13:35 結束）
    "max_entries": 2,               # 一天最多進場（含停損後重進）幾次
    "min_open_range_pct": 0.3,      # 進場前日內高低差至少 % ，避免開盤後一直平盤卻被小突破觸發
    # 時段與用詞（日盤預設；夜盤用 NIGHT 覆蓋）
    "session": "day",
    "session_start": "00:00",       # 一場的起點：時間比較以此為 0（夜盤 15:00 開、跨午夜，所以用「離開盤幾分鐘」比較）
    "early_until": "09:30",         # 在這之前就達到 early_trend_pct → 標示動能強
    "label": "日內", "period": "今天", "today_word": "今日", "loss_word": "當日", "early_word": "早盤動能強",
}

# 夜盤（15:00 開、隔天 05:00 收）：沿用日盤規則，只換時間窗、用詞，並把和振幅有關的門檻先縮小（夜盤波動較小）。
# 這些門檻都是沒有夜盤成交明細可校準的暫定值，以 config/settings.yaml 的 futures_night 區塊覆蓋，待校準。
NIGHT = {
    "session": "night", "session_start": "15:00", "early_until": "15:30",
    "label": "夜盤", "period": "本場", "today_word": "本場", "loss_word": "本場", "early_word": "開盤動能強",
    "entry_start": "15:15",         # 開盤後 15 分鐘先建立本場高低點
    "entry_until": "03:30",         # 這之後不再進場（05:00 收盤）
    "exit_times": ["04:35", "04:50"],   # 第一次出一半、第二次全出
    "active_range_pct": 0.7, "early_trend_pct": 0.6, "bar_range_pct": 0.75, "tick_jump_pct": 0.2, "min_open_range_pct": 0.2,
}
NIGHT_START, NIGHT_END = "15:00", "05:00"
STATE_KEY = {"day": "today", "night": "night"}


def _merge(base: dict, over: dict) -> dict:
    return {k: (type(v)(over[k]) if k in over and not isinstance(v, (list, bool)) else over.get(k, v)) for k, v in base.items()}


def settings(session: str = "day") -> dict:
    """設定：日盤讀 futures_day；夜盤先套 futures_day（資金、口數、停損等共用），再套 NIGHT 預設與 futures_night 的覆蓋。"""
    all_cfg = config.settings()
    cfg = _merge(DEFAULTS, all_cfg.get("futures_day") or {})
    if session == "night":
        night_cfg = all_cfg.get("futures_night") or {}
        over = _merge({**DEFAULTS, **NIGHT}, {k: v for k, v in night_cfg.items() if k in {**DEFAULTS, **NIGHT} and k != "enabled"})
        cfg.update({k: over[k] for k in NIGHT})
        cfg.update({k: over[k] for k in DEFAULTS if k in night_cfg and k not in NIGHT})
        cfg["enabled"] = bool(cfg["enabled"]) and bool(night_cfg.get("enabled", True))
    return cfg


def _hm(s: str) -> tuple[int, int]:
    h, m = str(s).split(":")
    return int(h), int(m)


def _rel(h: int, m: int, base: str) -> int:
    bh, bm = _hm(base)
    return (h * 60 + m - (bh * 60 + bm)) % 1440


def _after(now: datetime, hm: str, base: str = "00:00") -> bool:
    """now 是否已到 hm；以 base 為一場的起點（夜盤 15:00 開、跨午夜）比較。base＝00:00 時就是一般的鐘點比較。"""
    h, m = _hm(hm)
    return _rel(now.hour, now.minute, base) >= _rel(h, m, base)


def new_day(day: str) -> dict:
    return {"date": day, "ticks": [], "open": None, "day_high": None, "day_low": None, "dir": None, "lots": 0, "avg": None,
            "entries": 0, "adds": 0, "last_add_bar": None, "entry_bar": "", "last_fill": None, "last_fill_ts": None, "halted": False, "realized_pts": 0.0, "exits_done": [],
            "qualified": False, "alerts": [], "last_price": None}


def _bar_key(ts: datetime) -> str:
    return f"{ts:%Y%m%d}-{ts.hour:02d}{ts.minute // 5 * 5:02d}"


def bars(ticks: list) -> list[dict]:
    """把 [[ISO 時間, 價格], …] 組成 5 分 K（開高低收）；最後一根是還沒收完的那根。"""
    out: list[dict] = []
    for iso, p in ticks:
        k = _bar_key(datetime.fromisoformat(iso))
        if out and out[-1]["key"] == k:
            b = out[-1]
            b["high"], b["low"], b["close"] = max(b["high"], p), min(b["low"], p), p
        else:
            out.append({"key": k, "open": p, "high": p, "low": p, "close": p})
    return out


def streak(closes: list[float]) -> int:
    """日線連續同方向的天數（正＝連漲、負＝連跌）；closes 由舊到新。"""
    n = 0
    for a, b in zip(closes[-2::-1], closes[:0:-1]):   # 從最新往前比
        d = (b > a) - (b < a)
        if d == 0 or (n and (d > 0) != (n > 0)):
            break
        n += d
    return n


def _range_pct(st: dict) -> float:
    base = st["open"] or 0
    return (st["day_high"] - st["day_low"]) / base * 100 if base else 0.0


def _unreal(st: dict, price: float) -> float:
    if not st["lots"] or st["avg"] is None:
        return 0.0
    sign = 1 if st["dir"] == "long" else -1
    return (price - st["avg"]) * sign * st["lots"]


def _alert(st: dict, key: str, kind: str, now: datetime, price: float, text: str, out: list) -> None:
    if key in st["alerts"]:
        return
    st["alerts"].append(key)
    out.append({"kind": kind, "time": f"{now:%H:%M}", "ts": now.isoformat(timespec="seconds"), "price": price, "text": text})


def _board(st: dict, cfg: dict) -> str:
    r = _range_pct(st)
    ok = "合格" if r >= cfg["active_range_pct"] else "未達"
    return f"盤面：{cfg['label']}振幅 {r:.2f}%（加碼合格線 {cfg['active_range_pct']}%，{ok}）"


def _risk_line(cfg: dict) -> str:
    cap = cfg["capital"] * cfg["daily_loss_pct"] / 100
    return (f"風控：{cfg['contract']}每點 {cfg['contract_multiplier']:g} 元；{cfg['loss_word']}虧損上限 {cap:,.0f} 元"
            f"（資金 {cfg['capital']:,.0f} 的 {cfg['daily_loss_pct']}%），賠錢不加碼。")


def step(st: dict, now: datetime, price: float, cfg: dict, closes: list[float] | None = None,
         exch_high: float | None = None, exch_low: float | None = None) -> list[dict]:
    """餵一筆報價，回傳這一筆觸發的訊號（0 到多則）。st 會被就地更新（可存檔、換執行也能接續）。"""
    out: list[dict] = []
    base = cfg["session_start"]
    st.setdefault("entry_bar", "")
    st.setdefault("last_fill", None)
    st.setdefault("last_fill_ts", None)
    first = not st["ticks"]
    prev_high, prev_low = st["day_high"], st["day_low"]
    last = st["last_price"]
    st["ticks"].append([now.isoformat(timespec="seconds"), price])
    st["last_price"] = price
    if first:
        st["open"] = price
    hi = max(x for x in (price, exch_high) if x is not None)
    lo = min(x for x in (price, exch_low) if x is not None)
    st["day_high"] = hi if prev_high is None else max(prev_high, hi)
    st["day_low"] = lo if prev_low is None else min(prev_low, lo)
    if first:
        prev_high, prev_low = exch_high, exch_low    # 中途才啟動：交易所的今日高低就是參考點（沒有就等下一筆）
    rng = _range_pct(st)
    if not st["qualified"] and not st["halted"] and rng >= cfg["active_range_pct"]:
        st["qualified"] = True
        early = f"，{cfg['early_word']}" if not _after(now, cfg["early_until"], cfg["session_start"]) and rng >= cfg["early_trend_pct"] else ""
        _alert(st, "qualified", "盤面", now, price,
               f"🟢 盤面合格{early}：{cfg['label']}振幅 {rng:.2f}% ≥ {cfg['active_range_pct']}%（真正行情），之後依訊號進場／加碼。", out)
    if st["halted"]:
        return out
    sign = 1 if st["dir"] == "long" else -1

    # ---- 持倉中：停損／虧損上限 → 斷頭日型 → 加碼 → 分批出場
    if st["lots"]:
        stop_px = st["avg"] * (1 - sign * cfg["stop_pct"] / 100)
        loss_twd = -(st["realized_pts"] + _unreal(st, price)) * cfg["contract_multiplier"]
        cap = cfg["capital"] * cfg["daily_loss_pct"] / 100
        if (price - stop_px) * sign <= 0:
            _flat(st, price, f"stop{st['entries']}", "停損", now, out,
                  f"🛑 停損全出 {price:,.0f}：跌破（突破）平均成本 {st['avg']:,.1f} 的停損 {stop_px:,.1f}。寧可小賠、不要小賺或打平出場；出場用市價（範圍市價），不要掛限價。")
            return out
        if loss_twd >= cap:
            _flat(st, price, "dailyloss", "停損", now, out,
                  f"🛑 {cfg['loss_word']}虧損已達上限 {cap:,.0f} 元：全出、{cfg['period']}不再做。出場用市價（範圍市價）。")
            st["halted"] = True
            return out
        if closes and abs(streak(closes)) >= 3 and last is not None:
            jump = abs(price - last) / last * 100
            cur = bars(st["ticks"])[-1]
            brange = (cur["high"] - cur["low"]) / st["open"] * 100
            if jump >= cfg["tick_jump_pct"] or brange >= cfg["bar_range_pct"]:
                days = streak(closes)
                _flat(st, price, "blowoff", "出場", now, out,
                      f"⚠️ 提早出場 {price:,.0f}：日線已{'連漲' if days > 0 else '連跌'} {abs(days)} 天（期貨商容易斷頭的日型），"
                      f"現在{'單筆跳動' if jump >= cfg['tick_jump_pct'] else '5 分 K 振幅'}過大（{max(jump, brange):.2f}%）、行情亂跳，這通常是出場點。用市價（範圍市價），千萬不要掛限價，因為常常成交不了。")
                return out
        done = st["exits_done"]
        if _after(now, cfg["exit_times"][-1], base) and "final" not in done:
            done.append("final")
            _flat(st, price, "exit_final", "出場", now, out, f"⏰ 收盤前出場 {price:,.0f}：剩下的全部出場（抱到快收盤、獲利與持有時間成正比）。用市價（範圍市價）。")
            return out
        unit = max(1, int(cfg["unit_lots"]))
        if len(cfg["exit_times"]) > 1 and _after(now, cfg["exit_times"][0], base) and "half" not in done and st["lots"] > unit:
            done.append("half")
            n = max(unit, st["lots"] // 2 // unit * unit)       # 以「單位」為準出一半
            pts = (price - st["avg"]) * sign * n
            st["realized_pts"] += pts
            st["lots"] -= n
            _alert(st, f"exit_half{st['entries']}", "出場", now, price,
                   f"⏰ 分批出場：出 {n} 口（約 {pts * cfg['contract_multiplier']:+,.0f} 元），剩 {st['lots']} 口。快收盤了，獲利與持有時間成正比，其餘等最後一次。用市價（範圍市價）。", out)
        # 加碼：剛收完一根 5 分 K 拉回、獲利中、那根 K 沒碰到成本、盤面合格、口數沒滿
        bs = bars(st["ticks"])
        if len(bs) >= 2 and st["qualified"] and st["lots"] + unit <= cfg["max_lots"] and "half" not in done:
            b = bs[-2]                     # 最近一根已收完的 K
            pullback = (b["close"] < b["open"]) if st["dir"] == "long" else (b["close"] > b["open"])
            above_cost = (b["low"] > st["avg"]) if st["dir"] == "long" else (b["high"] < st["avg"])
            profitable = (price - st["avg"]) * sign > 0
            if pullback and above_cost and profitable and b["key"] != st["last_add_bar"] and b["key"] > st["entry_bar"]:
                st["last_add_bar"] = b["key"]
                st["adds"] += 1
                new_avg = (st["avg"] * st["lots"] + price * unit) / (st["lots"] + unit)
                st["lots"] += unit
                st["avg"] = new_avg
                st["last_fill"], st["last_fill_ts"] = price, now.isoformat(timespec="seconds")
                _alert(st, f"add:{b['key']}", "加碼", now, price,
                       f"➕ 加碼 {unit} 口{cfg['contract']}（{'多' if sign > 0 else '空'}）{price:,.0f}：{b['key'][-4:-2]}:{b['key'][-2:]} 的 5 分 K 拉回收{'黑' if sign > 0 else '紅'}、"
                       f"沒碰到成本、目前獲利。累計 {st['lots']} 口，平均成本 {new_avg:,.1f}（上限 {cfg['max_lots']} 口）。第一次要突破買、第二次要拉回買。", out)
                return out
        # 順勢加碼：創日內新高／新低、比上一筆成交又多走 ext_add_pct、離上一筆至少 ext_add_gap_sec 秒
        lf = st["last_fill"]
        if (lf is not None and st["qualified"] and st["lots"] + unit <= cfg["max_lots"] and "half" not in done
                and (price - st["avg"]) * sign > 0 and (price - lf) * sign >= lf * cfg["ext_add_pct"] / 100
                and (price >= st["day_high"] if sign > 0 else price <= st["day_low"])
                and (now - datetime.fromisoformat(st["last_fill_ts"])).total_seconds() >= cfg["ext_add_gap_sec"]):
            new_avg = (st["avg"] * st["lots"] + price * unit) / (st["lots"] + unit)
            st["lots"] += unit
            st["avg"], st["adds"] = new_avg, st["adds"] + 1
            st["last_fill"], st["last_fill_ts"] = price, now.isoformat(timespec="seconds")
            _alert(st, f"ext:{st['entries']}:{st['lots']}", "加碼", now, price,
                   f"➕ 順勢加碼 {unit} 口{cfg['contract']}（{'多' if sign > 0 else '空'}）{price:,.0f}：{'創' + cfg['label'] + '新高' if sign > 0 else '創' + cfg['label'] + '新低'}，比上一筆成交 {lf:,.0f} 又多走 {abs(price - lf):,.0f} 點、目前獲利。"
                   f"累計 {st['lots']} 口，平均成本 {new_avg:,.1f}（上限 {cfg['max_lots']} 口）。", out)
        return out

    # ---- 空手：突破今日高低點 → 試單（一天一個方向）
    if (st["entries"] < cfg["max_entries"] and _after(now, cfg["entry_start"], base) and not _after(now, cfg["entry_until"], base)
            and prev_high is not None and rng >= cfg["min_open_range_pct"]
            and not _after(now, cfg["exit_times"][-1], base)):
        want = None
        if price > prev_high and st["dir"] in (None, "long"):
            want = "long"
        elif price < prev_low and st["dir"] in (None, "short"):
            want = "short"
        if want:
            st["dir"] = want
            unit = max(1, int(cfg["unit_lots"]))
            st["lots"], st["avg"], st["adds"] = unit, price, 0
            st["entries"] += 1
            st["entry_bar"] = _bar_key(now)
            st["last_fill"], st["last_fill_ts"] = price, now.isoformat(timespec="seconds")
            sgn = 1 if want == "long" else -1
            stop_px = price * (1 - sgn * cfg["stop_pct"] / 100)
            risk = abs(price - stop_px) * unit * cfg["contract_multiplier"]
            tail = ("盤面已合格，後續拉回可依訊號加碼。" if st["qualified"] else
                    f"盤面還沒到真正行情（振幅未達 {cfg['active_range_pct']}%），先只試單、不要太積極，也不要加碼。")
            _alert(st, f"enter{st['entries']}", "進場", now, price,
                   f"{'🔺 做多' if want == 'long' else '🔻 做空'}試單 {unit} 口{cfg['contract']} {price:,.0f}：{'突破' + cfg['today_word'] + '高點' if want == 'long' else '跌破' + cfg['today_word'] + '低點'} {prev_high if want == 'long' else prev_low:,.0f}。"
                   f"停損 {stop_px:,.1f}（平均成本外 {cfg['stop_pct']}%，這一單停損約虧 {risk:,.0f} 元）。{tail}{cfg['period']}只做這個方向。", out)
    return out


def _flat(st: dict, price: float, key: str, kind: str, now: datetime, out: list, text: str) -> None:
    sign = 1 if st["dir"] == "long" else -1
    pts = (price - st["avg"]) * sign * st["lots"]
    st["realized_pts"] += pts
    lots = st["lots"]
    st["lots"], st["avg"] = 0, None
    _alert(st, key, kind, now, price, text + f"（{lots} 口，約 {pts:+,.0f} 點）", out)


EXIT_KINDS = ("停損", "出場")


def is_full_exit(sigs: list[dict], st: dict) -> bool:
    """這一批訊號裡有停損或出場，而且處理完已經空手＝全數出場。分批出場（出一半、還有部位）、進場、加碼、盤面通知都不算。
    推播只在全數出場時附累計損益（本場、本月、今年）。"""
    return st["lots"] == 0 and any(s["kind"] in EXIT_KINDS for s in sigs)


def format_message(sigs: list[dict], st: dict, cfg: dict, pnl: str = "") -> str:
    """pnl：損益總結（本場／本月／今年累計，futures_log.pnl_line 算的），有就放在盤面和風控之間。"""
    head = f"📈 台指期{cfg['label']}訊號 {sigs[0]['time']}"
    body = "\n".join(s["text"] for s in sigs)
    mid = f"{pnl}\n" if pnl else ""
    return f"{head}\n{body}\n{_board(st, cfg)}\n{mid}{_risk_line(cfg)}\n（待校準）"


def summary(st: dict, cfg: dict) -> str:
    pts = st["realized_pts"] + (_unreal(st, st["last_price"]) if st["lots"] and st["last_price"] else 0)
    return (f"計畫帳：{st['entries']} 次進場、加碼 {st['adds']} 次、"
            f"{'持倉 ' + str(st['lots']) + ' 口' if st['lots'] else '空手'}、損益約 {pts:+,.0f} 點（{pts * cfg['contract_multiplier']:+,.0f} 元）；"
            f"振幅 {_range_pct(st):.2f}%。")


def snapshot(st: dict, cfg: dict) -> dict:
    """這一場目前的計畫帳數字（給觸發紀錄用）：點數＝已實現＋未平倉浮動，元＝點數 × 每點元數。"""
    last = st.get("last_price")
    open_pts = _unreal(st, last) if st["lots"] and last else 0.0
    pts = st["realized_pts"] + open_pts
    return {"dir": st["dir"], "entries": st["entries"], "adds": st["adds"], "lots_left": st["lots"],
            "realized_pts": round(st["realized_pts"], 1), "open_pts": round(open_pts, 1),
            "pts": round(pts, 1), "twd": round(pts * cfg["contract_multiplier"]),
            "open": st.get("open"), "high": st.get("day_high"), "low": st.get("day_low"), "close": last,
            "range_pct": round(_range_pct(st), 2), "qualified": bool(st["qualified"]), "halted": bool(st["halted"])}


def replay(ticks: list[tuple[datetime, float]], cfg: dict | None = None, closes: list[float] | None = None) -> tuple[list[dict], dict]:
    """用一整天的報價重播，回傳（所有訊號、最後狀態）：拿來測試，之後也能用交易明細對照校準。"""
    cfg = cfg or settings()
    st = new_day(f"{ticks[0][0]:%Y-%m-%d}")
    sigs: list[dict] = []
    for ts, p in ticks:
        sigs += step(st, ts, p, cfg, closes)
    return sigs, st


# ---------------------------------------------------------------- 存檔（跟著 state/ 一起 commit，換執行也能接續）
# 一個檔三份資料：closes＝前幾天的日收盤、today＝日盤這一場的狀態、night＝夜盤這一場的狀態（存的時候互相保留，不會蓋掉對方）。

def _read_all(path: Path) -> dict:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_all(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False) + "\n", encoding="utf-8")


def load_state(day: str, path: Path | None = None, session: str = "day") -> dict:
    path = path or STATE_PATH
    try:
        d = _read_all(path).get(STATE_KEY[session], {})
        if d.get("date") == day:
            return d
    except AttributeError:
        pass
    return new_day(day)


def _load_closes_raw(path: Path) -> list[dict]:
    return [c for c in _read_all(path).get("closes", []) if isinstance(c, dict)]


def load_closes(day: str, path: Path | None = None) -> list[float]:
    """今天以前的日收盤（由舊到新），給「連漲／連跌 3 天」的判斷用；每天收盤後由 finish_day 記一筆。"""
    return [float(c["c"]) for c in _load_closes_raw(path or STATE_PATH) if c.get("d") != day]


def save_state(st: dict, path: Path | None = None, keep_ticks: int = 4000, session: str = "day") -> None:
    path = path or STATE_PATH
    data = _read_all(path)
    slim = dict(st)
    slim["ticks"] = st["ticks"][-keep_ticks:]
    data["closes"] = _load_closes_raw(path)
    data[STATE_KEY[session]] = slim
    _write_all(path, data)


def finish_day(st: dict, cfg: dict, path: Path | None = None) -> str | None:
    """收盤後：把今天最後一筆報價記成日收盤（重跑同一天只會覆蓋），回傳計畫帳摘要（今天有進場才有）。"""
    path = path or STATE_PATH
    if not st["ticks"] or datetime.fromisoformat(st["ticks"][-1][0]).hour * 60 + datetime.fromisoformat(st["ticks"][-1][0]).minute < 13 * 60 + 15:
        return None   # 沒跑到收盤附近（中途停掉）：不記日收盤，免得拿盤中價當收盤
    data = _read_all(path)
    closes = [c for c in _load_closes_raw(path) if c.get("d") != st["date"]] + [{"d": st["date"], "c": st["last_price"]}]
    slim = dict(st)
    slim["ticks"] = st["ticks"][-4000:]
    data["closes"], data["today"] = closes[-10:], slim
    _write_all(path, data)
    return summary(st, cfg) if st["entries"] else None


# ---------------------------------------------------------------- 夜盤（15:00–隔天 05:00）

def night_session(now: datetime) -> tuple[str, datetime, datetime]:
    """now 所屬的那一場夜盤：（場次代號、開盤時間、收盤時間）。14:30 以後算今天開的那場，凌晨算前一天開的那場。"""
    h, m = _hm(NIGHT_START)
    start = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if now < start - timedelta(minutes=30):      # 開盤前 30 分鐘內算今晚這一場（先啟動等開盤），再早就是前一晚那一場
        start -= timedelta(days=1)
    eh, em = _hm(NIGHT_END)
    end = (start + timedelta(days=1)).replace(hour=eh, minute=em)
    return f"{start:%Y-%m-%d}N", start, end


def night_quote_ts(ts: datetime | None, start: datetime) -> datetime | None:
    """MIS 夜盤報價的日期欄位過了午夜不會換日（00:00 之後仍是開盤那天的日期，只有時間欄位歸零重算；2026-11-13 01:08 實測 CDate 還是 20261112）。
    所以報價時間比開盤時間早很多，就是已經過了午夜，要補一天，不然會被當成 24 小時前的舊報價、整個午夜後都沒有訊號。"""
    if ts is not None and ts < start - timedelta(minutes=5):
        return ts + timedelta(days=1)
    return ts


def finish_night(st: dict, cfg: dict, path: Path | None = None) -> str | None:
    """夜盤收盤：標記這一場結束（不再接續）、存檔，回傳計畫帳摘要（有進場才有）。夜盤不記日收盤。"""
    st["finished"] = True
    save_state(st, path, session="night")
    return summary(st, cfg) if st["entries"] else None
