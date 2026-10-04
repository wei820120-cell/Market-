"""進出場機器人（模擬模式）：每天依回測選定的規則（策略 B）產生計劃、模擬成交與記帳。

每天兩次（由常駐監看觸發）：
- 08:30 盤前：推「今日計劃」（大盤多空、候選標的、契約、口數、預估停損、保證金）
- 15:30 盤後：用今天的日 K 結算：開盤成交昨天的計劃 → 停損／+2R 先出一半 → 收盤檢查 20 日線與時間停損
             → 產生明天的計劃 → 推「盤後報告」（成交、持倉、權益、離目標還差多少）

規則與 trade/backtest.py 相同（只做多、大盤濾網、突破／拉回、1.5ATR 停損、+2R 出一半、跌破 20 日線出場、
10 天時間停損、每筆風險 2%、最多 3 檔、小型股期優先、保證金合計不超過權益），另外排除處置股。
帳戶記錄在 state/trade/paper.json。模擬用股價代替期貨價，實際成交以期貨價為準。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

import pandas as pd

from .. import notify
from ..utils import ROOT, now_tw
from . import backtest as bt

log = logging.getLogger(__name__)

STATE = ROOT / "state" / "trade" / "paper.json"
CHANNEL = "trade"
PARAMS = bt.Params(exit_ma="ma20")  # 回測選定：策略 B
TARGET = 200_000


def load() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {"start": f"{now_tw():%Y-%m-%d}", "capital": PARAMS.capital, "equity": PARAMS.capital,
            "positions": [], "closed": [], "plan": [], "last_date": None, "regime": None}


def save(st: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


def margin(price: float, mult: int, qty: int, rate: float) -> float:
    return price * mult * qty * rate


def _cost(price, mult, qty) -> float:
    return bt.cost(price, mult, qty)


def settle(st: dict, day: pd.Timestamp, prepped: dict, rates: dict) -> list[str]:
    """用當天日 K 結算：成交昨天的計劃、處理出場。回傳要推播的事件。"""
    p = PARAMS
    events = []
    # 1) 開盤成交昨天的計劃
    for plan in st.get("plan", []):
        if len(st["positions"]) >= p.max_pos or any(x["code"] == plan["code"] for x in st["positions"]):
            continue
        d = prepped.get(plan["code"])
        if d is None or day not in d.index:
            continue
        entry = float(d.loc[day, "open"])
        stop = entry - p.atr_mult * plan["atr"]
        contract, mult, qty = bt.size(st["equity"], entry, stop, plan["info"], p)
        if not qty:
            events.append(f"⏭ {plan['code']} {plan['name']} 開盤 {entry:,.1f}，停損距離太大、算不出 1 口，放棄")
            continue
        rate = rates.get(plan["code"], p.margin_rate)
        used = sum(margin(x["entry"], x["mult"], x["qty"], rates.get(x["code"], p.margin_rate)) for x in st["positions"])
        if used + margin(entry, mult, qty, rate) > st["equity"]:
            events.append(f"⏭ {plan['code']} {plan['name']} 保證金不足，放棄")
            continue
        c = _cost(entry, mult, qty)
        st["positions"].append({"code": plan["code"], "name": plan["name"], "setup": plan["setup"],
                                "contract": contract, "mult": mult, "qty": qty, "qty0": qty,
                                "entry_date": str(day.date()), "entry": entry, "stop": stop,
                                "r0": entry - stop, "realized": -c, "half_done": False, "days": 0, "exit_next": ""})
        events.append(f"🟢 模擬進場 {plan['code']} {plan['name']}｜{contract} {qty} 口 @ {entry:,.1f}"
                      f"｜停損 {stop:,.1f}｜{plan['setup']}")
    st["plan"] = []
    # 2) 出場
    for x in list(st["positions"]):
        d = prepped.get(x["code"])
        if d is None or day not in d.index:
            continue
        row = d.loc[day]
        x["days"] += 1
        px, reason = None, ""
        if x["exit_next"]:
            px, reason = float(row["open"]), x["exit_next"]
        elif row["low"] <= x["stop"]:
            px, reason = min(float(row["open"]), x["stop"]), ("保本出場" if x["half_done"] else "停損")
        else:
            target = x["entry"] + p.take_r * x["r0"]
            if not x["half_done"] and row["high"] >= target:
                fill = max(float(row["open"]), target)
                half = x["qty"] // 2
                if half >= 1:
                    x["realized"] += (fill - x["entry"]) * x["mult"] * half - _cost(fill, x["mult"], half)
                    x["qty"] -= half
                    events.append(f"🎯 {x['code']} {x['name']} 到 +2R {fill:,.1f}，先出 {half} 口，停損移到成本")
                else:
                    events.append(f"🎯 {x['code']} {x['name']} 到 +2R {fill:,.1f}，只有 1 口不分批，停損移到成本")
                x["half_done"], x["stop"] = True, x["entry"]
            if x["days"] > 1 and row["close"] < row[p.exit_ma]:
                x["exit_next"] = "跌破20日線"
                events.append(f"⚠️ {x['code']} {x['name']} 收盤跌破 20 日線，明天開盤出場")
            elif x["days"] >= p.time_stop and not x["half_done"] and row["close"] < x["entry"] + x["r0"]:
                x["exit_next"] = "時間停損"
                events.append(f"⚠️ {x['code']} {x['name']} 持有 {x['days']} 天未達 +1R，明天開盤出場")
        if px is not None:
            pnl = x["realized"] + (px - x["entry"]) * x["mult"] * x["qty"] - _cost(px, x["mult"], x["qty"])
            r = pnl / (x["r0"] * x["mult"] * x["qty0"]) if x["r0"] > 0 else 0
            st["equity"] += pnl
            st["positions"].remove(x)
            st["closed"].append({**x, "exit_date": str(day.date()), "exit": px, "pnl": round(pnl), "r": round(r, 2),
                                 "reason": reason})
            events.append(f"{'🔴' if pnl < 0 else '✅'} 模擬出場 {x['code']} {x['name']} @ {px:,.1f}（{reason}）"
                          f"｜損益 {pnl:+,.0f}（{r:+.1f}R）")
    return events


def make_plan(st: dict, day: pd.Timestamp, prepped: dict, futures: dict, names: dict, rates: dict,
              alerts: dict, market_ok: bool) -> list[dict]:
    """今天收盤的訊號 → 明天的計劃（依量比排序，扣掉已持有、處置股，最多補滿 3 檔）。"""
    p = PARAMS
    if not market_ok:
        return []
    cands = []
    for code, d in prepped.items():
        if day not in d.index or code in [x["code"] for x in st["positions"]]:
            continue
        if any("處置" in a for a in alerts.get(code, [])):
            continue
        row = d.loc[day]
        setup = "突破" if row["sig_a"] else ("拉回" if row["sig_b"] else "")
        if not setup:
            continue
        close, a = float(row["close"]), float(row["atr"])
        stop = close - p.atr_mult * a
        contract, mult, qty = bt.size(st["equity"], close, stop, futures.get(code, {}), p)
        if not qty:
            continue
        cands.append({"code": code, "name": names.get(code, code), "setup": setup, "atr": a, "close": close,
                      "vr": float(row["vr"]), "info": futures.get(code, {}), "contract": contract, "mult": mult,
                      "qty": qty, "stop_est": stop, "target_est": close + p.take_r * (close - stop),
                      "margin_est": margin(close, mult, qty, rates.get(code, p.margin_rate)),
                      "risk_est": (close - stop) * mult * qty})
    cands.sort(key=lambda c: (c["setup"] == "突破", c["vr"]), reverse=True)
    return cands[:max(0, p.max_pos - len(st["positions"]))]


def report(st: dict, day: pd.Timestamp, events: list[str], market_ok: bool, prepped: dict) -> str:
    eq = st["equity"]
    mtm = 0.0
    lines = [f"📒 進出場（模擬）盤後 {day:%m/%d}",
             f"大盤：{'🟢 多頭，可以依計劃進場' if market_ok else '🔴 空頭／盤整，明天先不要下新單'}"]
    if events:
        lines += ["", "【今天】"] + events
    if st["positions"]:
        lines += ["", "【持倉】"]
        for x in st["positions"]:
            close = float(prepped[x["code"]]["close"].get(day, x["entry"])) if x["code"] in prepped else x["entry"]
            u = (close - x["entry"]) * x["mult"] * x["qty"] + x["realized"]
            mtm += u
            lines.append(f"・{x['code']} {x['name']} {x['contract']} {x['qty']} 口｜成本 {x['entry']:,.1f} 現價 {close:,.1f}"
                         f"｜未實現 {u:+,.0f}｜停損 {x['stop']:,.1f}｜第 {x['days']} 天")
    total = eq + mtm
    wins = [c for c in st["closed"] if c["pnl"] > 0]
    lines += ["", f"💰 權益 {total:,.0f}（已實現 {eq:,.0f}）｜起始 {st['capital']:,.0f}｜報酬 {total / st['capital'] - 1:+.1%}",
              f"🎯 目標 {TARGET:,.0f}，還差 {max(0, TARGET - total):,.0f}｜進度 {(total - st['capital']) / (TARGET - st['capital']):.0%}",
              f"已平倉 {len(st['closed'])} 筆，勝率 {len(wins) / len(st['closed']):.0%}" if st["closed"] else "已平倉 0 筆"]
    lines += ["", plan_text(st, market_ok, "明天")]
    return "\n".join(lines)


def plan_text(st: dict, market_ok: bool, when: str = "今天") -> str:
    if not market_ok:
        return f"📋 {when}計劃：大盤空頭／盤整，不開新倉（持倉照停損與出場規則處理）"
    if not st.get("plan"):
        return f"📋 {when}計劃：沒有符合條件的標的（或持倉已滿 {PARAMS.max_pos} 檔）"
    lines = [f"📋 {when}計劃（開盤進場；停損＝進場價 − 1.5×ATR，依實際開盤價重算口數）"]
    for c in st["plan"]:
        lines.append(f"・{c['code']} {c['name']}｜{c['setup']}｜{c['contract']} {c['qty']} 口"
                     f"｜參考價 {c['close']:,.1f}｜停損約 {c['stop_est']:,.1f}｜+2R 約 {c['target_est']:,.1f}"
                     f"｜最大虧損約 {c['risk_est']:,.0f}｜保證金約 {c['margin_est']:,.0f}")
    return "\n".join(lines)


def run_post(data: dict, index_df: pd.DataFrame, futures: dict, names: dict, rates: dict, alerts: dict,
             today: str) -> str | None:
    """盤後：今天有新的日 K 才結算（休市日不動作）。回傳推播文字。"""
    st = load()
    prepped = {c: bt.prepare(df, PARAMS) for c, df in data.items() if len(df) > 80}
    if index_df.empty or last_date(index_df) != today:
        log.info("加權指數還沒有今天的日 K（休市或資料未更新），不結算")
        return None
    day = index_df.index[-1]
    if st.get("last_date") == today:
        log.info("今天已結算過")
        return ""
    mkt = bool(bt.market_ok(index_df).iloc[-1])
    events = settle(st, day, prepped, rates) if st.get("last_date") else []
    st["plan"] = make_plan(st, day, prepped, futures, names, rates, alerts, mkt)
    st["last_date"], st["regime"] = today, "多頭" if mkt else "空頭／盤整"
    msg = report(st, day, events, mkt, prepped)
    save(st)
    return msg


def last_date(df: pd.DataFrame) -> str:
    ts = df.index[-1]
    if ts.tzinfo is not None:
        ts = ts.tz_convert("Asia/Taipei")
    return f"{ts:%Y-%m-%d}"


def run_pre() -> str:
    st = load()
    mkt = st.get("regime") == "多頭"
    head = f"🌅 進出場（模擬）盤前 {now_tw():%m/%d}｜大盤：{'🟢 多頭' if mkt else '🔴 空頭／盤整，今天先不要下新單'}"
    pos = "、".join(f"{x['code']} {x['name']}（停損 {x['stop']:,.1f}" + ("，開盤出場" if x["exit_next"] else "") + "）"
                    for x in st.get("positions", []))
    return "\n".join([head, f"持倉：{pos}" if pos else "持倉：無", "", plan_text(st, mkt)])


def push(text: str | None) -> None:
    if text:
        notify.send(text, channel=CHANNEL)


def is_weekday(dt: datetime | None = None) -> bool:
    return (dt or now_tw()).weekday() < 5
