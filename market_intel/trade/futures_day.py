"""台指期日內順勢突破：照 @pingyunhuang 的做法（knowledge/methods/pingyunhuang-台指期突破加碼.md）算「何時進場、加碼、出場」。

只在盤面合格時叫你動作；只發訊號、不下單。規則（原文點數是十幾年前的位階，這裡全部換成「占價格的 %」，預設值按當年加權約 7,500 點換算，等交易明細校準）：
- 一天只做一個方向：價格突破「今日高點」→ 多方、跌破「今日低點」→ 空方；第一次突破先試單（1 口）。
- 加碼：只有「盤面合格」（日內高低差已達 active_range_pct，真正行情）、而且已經獲利（價格在平均成本有利的一邊）、
  剛收完一根 5 分 K 拉回（做多＝收黑 K）、那根 K 沒碰到平均成本（支撐在成本之上）、口數還沒到上限 → 加碼 1 口。賠錢時絕不加碼。
- 停損：跌破（做空則突破）平均成本一點點（stop_pct）就全出；當日虧損達資金的 daily_loss_pct 也全出、當天不再做。
- 出場：預設抱到快收盤才分批（exit_times：第一次出一半、第二次全出）；日線連漲／連跌 3 天以上的日子，
  5 分 K 振幅過大（bar_range_pct）或跳動過大（tick_jump_pct）就提早全出。出場一律用市價（範圍市價），不要掛限價。
資料是每約 20 秒一筆的報價（期交所 MIS），5 分 K 由這些報價自己組。"""
from __future__ import annotations

import json
import logging
from datetime import datetime
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
    "active_range_pct": 1.6,        # 日內高低差 ≥ 此 % 才算「真正行情」、才准加碼（原文 120 點）
    "early_trend_pct": 1.33,        # 09:30 前就達到 → 訊息標示「早盤動能強」（原文 9 點半超過 100 點）
    "bar_range_pct": 1.07,          # 斷頭日型：5 分 K 高低差 ≥ 此 %（原文 80 點）
    "tick_jump_pct": 0.27,          # 斷頭日型：相鄰兩筆報價跳動 ≥ 此 %（原文 20 點）
    "stop_pct": 0.15,               # 跌破平均成本多少 % 全出（原文只說依均價設停損，沒給數字：待交易明細校準）
    "daily_loss_pct": 3.0,          # 當日虧損上限（占資金 %）：原文 3～5%
    "entry_start": "09:00",         # 開盤前 15 分鐘先建立今日高低點
    "entry_until": "12:30",         # 這之後的突破不進場（原文沒說，保守設定）
    "exit_times": ["13:10", "13:25"],   # 第一次出一半、第二次全出（盤中監控約 13:35 結束）
    "max_entries": 2,               # 一天最多進場（含停損後重進）幾次
    "min_open_range_pct": 0.3,      # 進場前日內高低差至少 % ，避免開盤後一直平盤卻被小突破觸發
}


def settings() -> dict:
    st = (config.settings().get("futures_day") or {})
    return {k: (type(v)(st[k]) if k in st and not isinstance(v, (list, bool)) else st.get(k, v)) for k, v in DEFAULTS.items()}


def _hm(s: str) -> tuple[int, int]:
    h, m = str(s).split(":")
    return int(h), int(m)


def _after(now: datetime, hm: str) -> bool:
    h, m = _hm(hm)
    return (now.hour, now.minute) >= (h, m)


def new_day(day: str) -> dict:
    return {"date": day, "ticks": [], "open": None, "day_high": None, "day_low": None, "dir": None, "lots": 0, "avg": None,
            "entries": 0, "adds": 0, "last_add_bar": None, "entry_bar": "", "halted": False, "realized_pts": 0.0, "exits_done": [],
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
    out.append({"kind": kind, "time": f"{now:%H:%M}", "price": price, "text": text})


def _board(st: dict, cfg: dict) -> str:
    r = _range_pct(st)
    ok = "合格" if r >= cfg["active_range_pct"] else "未達"
    return f"盤面：日內振幅 {r:.2f}%（加碼合格線 {cfg['active_range_pct']}%，{ok}）"


def _risk_line(cfg: dict) -> str:
    cap = cfg["capital"] * cfg["daily_loss_pct"] / 100
    return (f"風控：{cfg['contract']}每點 {cfg['contract_multiplier']:g} 元；當日虧損上限 {cap:,.0f} 元"
            f"（資金 {cfg['capital']:,.0f} 的 {cfg['daily_loss_pct']}%），賠錢不加碼。")


def step(st: dict, now: datetime, price: float, cfg: dict, closes: list[float] | None = None,
         exch_high: float | None = None, exch_low: float | None = None) -> list[dict]:
    """餵一筆報價，回傳這一筆觸發的訊號（0 到多則）。st 會被就地更新（可存檔、換執行也能接續）。"""
    out: list[dict] = []
    st.setdefault("entry_bar", "")
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
        early = "，早盤動能強" if not _after(now, "09:30") and rng >= cfg["early_trend_pct"] else ""
        _alert(st, "qualified", "盤面", now, price,
               f"🟢 盤面合格{early}：日內振幅 {rng:.2f}% ≥ {cfg['active_range_pct']}%（真正行情），之後依訊號進場／加碼。", out)
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
                  f"🛑 當日虧損已達上限 {cap:,.0f} 元：全出、今天不再做。出場用市價（範圍市價）。")
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
        if _after(now, cfg["exit_times"][-1]) and "final" not in done:
            done.append("final")
            _flat(st, price, "exit_final", "出場", now, out, f"⏰ 收盤前出場 {price:,.0f}：剩下的全部出場（抱到快收盤、獲利與持有時間成正比）。用市價（範圍市價）。")
            return out
        unit = max(1, int(cfg["unit_lots"]))
        if len(cfg["exit_times"]) > 1 and _after(now, cfg["exit_times"][0]) and "half" not in done and st["lots"] > unit:
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
                _alert(st, f"add:{b['key']}", "加碼", now, price,
                       f"➕ 加碼 {unit} 口{cfg['contract']}（{'多' if sign > 0 else '空'}）{price:,.0f}：{b['key'][-4:-2]}:{b['key'][-2:]} 的 5 分 K 拉回收{'黑' if sign > 0 else '紅'}、"
                       f"沒碰到成本、目前獲利。累計 {st['lots']} 口，平均成本 {new_avg:,.1f}（上限 {cfg['max_lots']} 口）。第一次要突破買、第二次要拉回買。", out)
        return out

    # ---- 空手：突破今日高低點 → 試單（一天一個方向）
    if (st["entries"] < cfg["max_entries"] and _after(now, cfg["entry_start"]) and not _after(now, cfg["entry_until"])
            and prev_high is not None and rng >= cfg["min_open_range_pct"]
            and not _after(now, cfg["exit_times"][-1])):
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
            sgn = 1 if want == "long" else -1
            stop_px = price * (1 - sgn * cfg["stop_pct"] / 100)
            risk = abs(price - stop_px) * unit * cfg["contract_multiplier"]
            tail = ("盤面已合格，後續拉回可依訊號加碼。" if st["qualified"] else
                    f"盤面還沒到真正行情（振幅未達 {cfg['active_range_pct']}%），先只試單、不要太積極，也不要加碼。")
            _alert(st, f"enter{st['entries']}", "進場", now, price,
                   f"{'🔺 做多' if want == 'long' else '🔻 做空'}試單 {unit} 口{cfg['contract']} {price:,.0f}：{'突破今日高點' if want == 'long' else '跌破今日低點'} {prev_high if want == 'long' else prev_low:,.0f}。"
                   f"停損 {stop_px:,.1f}（平均成本外 {cfg['stop_pct']}%，這一單停損約虧 {risk:,.0f} 元）。{tail}今天只做這個方向。", out)
    return out


def _flat(st: dict, price: float, key: str, kind: str, now: datetime, out: list, text: str) -> None:
    sign = 1 if st["dir"] == "long" else -1
    pts = (price - st["avg"]) * sign * st["lots"]
    st["realized_pts"] += pts
    lots = st["lots"]
    st["lots"], st["avg"] = 0, None
    _alert(st, key, kind, now, price, text + f"（{lots} 口，約 {pts:+,.0f} 點）", out)


def format_message(sigs: list[dict], st: dict, cfg: dict) -> str:
    head = f"📈 台指期日內訊號 {sigs[0]['time']}"
    body = "\n".join(s["text"] for s in sigs)
    return f"{head}\n{body}\n{_board(st, cfg)}\n{_risk_line(cfg)}\n（依 @pingyunhuang 手法的計畫帳，只是提醒、不是下單；點數已換成 %，待校準）"


def summary(st: dict, cfg: dict) -> str:
    pts = st["realized_pts"] + (_unreal(st, st["last_price"]) if st["lots"] and st["last_price"] else 0)
    return (f"計畫帳：{st['entries']} 次進場、加碼 {st['adds']} 次、"
            f"{'持倉 ' + str(st['lots']) + ' 口' if st['lots'] else '空手'}、損益約 {pts:+,.0f} 點（{pts * cfg['contract_multiplier']:+,.0f} 元）；"
            f"振幅 {_range_pct(st):.2f}%。")


def replay(ticks: list[tuple[datetime, float]], cfg: dict | None = None, closes: list[float] | None = None) -> tuple[list[dict], dict]:
    """用一整天的報價重播，回傳（所有訊號、最後狀態）：拿來測試，之後也能用交易明細對照校準。"""
    cfg = cfg or settings()
    st = new_day(f"{ticks[0][0]:%Y-%m-%d}")
    sigs: list[dict] = []
    for ts, p in ticks:
        sigs += step(st, ts, p, cfg, closes)
    return sigs, st


# ---------------------------------------------------------------- 存檔（跟著 state/ 一起 commit，換執行也能接續）

def load_state(day: str, path: Path | None = None) -> dict:
    path = path or STATE_PATH
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        if d.get("today", {}).get("date") == day:
            return d["today"]
    except (OSError, ValueError, AttributeError):
        pass
    return new_day(day)


def _load_closes_raw(path: Path) -> list[dict]:
    try:
        return [c for c in json.loads(path.read_text(encoding="utf-8")).get("closes", []) if isinstance(c, dict)]
    except (OSError, ValueError, AttributeError):
        return []


def load_closes(day: str, path: Path | None = None) -> list[float]:
    """今天以前的日收盤（由舊到新），給「連漲／連跌 3 天」的判斷用；每天收盤後由 finish_day 記一筆。"""
    return [float(c["c"]) for c in _load_closes_raw(path or STATE_PATH) if c.get("d") != day]


def save_state(st: dict, path: Path | None = None, keep_ticks: int = 4000) -> None:
    path = path or STATE_PATH
    slim = dict(st)
    slim["ticks"] = st["ticks"][-keep_ticks:]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"closes": _load_closes_raw(path), "today": slim}, ensure_ascii=False) + "\n", encoding="utf-8")


def finish_day(st: dict, cfg: dict, path: Path | None = None) -> str | None:
    """收盤後：把今天最後一筆報價記成日收盤（重跑同一天只會覆蓋），回傳計畫帳摘要（今天有進場才有）。"""
    path = path or STATE_PATH
    if not st["ticks"] or datetime.fromisoformat(st["ticks"][-1][0]).hour * 60 + datetime.fromisoformat(st["ticks"][-1][0]).minute < 13 * 60 + 15:
        return None   # 沒跑到收盤附近（中途停掉）：不記日收盤，免得拿盤中價當收盤
    closes = [c for c in _load_closes_raw(path) if c.get("d") != st["date"]] + [{"d": st["date"], "c": st["last_price"]}]
    slim = dict(st)
    slim["ticks"] = st["ticks"][-4000:]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"closes": closes[-10:], "today": slim}, ensure_ascii=False) + "\n", encoding="utf-8")
    return summary(st, cfg) if st["entries"] else None
