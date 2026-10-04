"""進出場機器人（實單記帳）：你下單後在機器人回報成交，機器人記帳並依策略 B 提醒停損與出場。

回報格式（順序可以換，頓號、逗號、空白都可以）：
  亞泥 35.7 1口          → 買進（沒寫買賣就當買進）
  買 DYF 35.7 2口        → 契約代碼、股票代號、股票名稱都可以
  亞泥 小型 35.7 1口     → 同時有一般／小型股期時，寫「小型」指定小型
  賣 亞泥 36.5 1口       → 賣出（出場）
  持倉                    → 目前持倉、損益、權益
  說明                    → 指令說明

規則（和回測選定的策略 B 相同）：停損＝進場價 − 1.5×ATR(14)、+2R 建議先出一半並把停損移到成本、
收盤跌破 20 日線隔天開盤出場、持有 10 個交易日未達 +1R 出場；每筆最大虧損 2% 權益、最多 3 檔；
波段單每口準備 3 倍原始保證金，所有持倉的準備金合計不超過權益。
機器人只提醒，不會自動下單。帳戶記錄在 state/trade/real.json。
"""
from __future__ import annotations

import json
import re

import pandas as pd

from ..utils import ROOT, now_tw
from . import backtest as bt
from .paper import PARAMS, TARGET

STATE = ROOT / "state" / "trade" / "real.json"
SELL_WORDS = ("賣出", "賣", "平倉", "出場", "停損", "停利")
BUY_WORDS = ("買入", "買進", "買", "做多", "進場", "多")


def load() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {"capital": PARAMS.capital, "equity": PARAMS.capital, "positions": [], "closed": []}


def save(st: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(st, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


def parse(text: str) -> dict | None:
    """解析成交回報：{"side", "query", "price", "qty", "mini"}；看不懂回傳 None。"""
    t = (text or "").strip().replace("，", " ").replace("、", " ").replace(",", " ")
    if not t:
        return None
    if t in ("持倉", "帳戶", "部位", "/positions"):
        return {"cmd": "positions"}
    if t in ("說明", "help", "/help", "/start"):
        return {"cmd": "help"}
    m = re.match(r"^\s*(檢查|為什麼|為何|check)\s*(.+)$", t)
    if m:
        return {"cmd": "explain", "query": m.group(2).strip().split()[0]}
    qty_m = re.search(r"(\d+)\s*口", t)
    if not qty_m:
        return None
    rest = t[:qty_m.start()] + " " + t[qty_m.end():]
    side = "sell" if any(w in rest for w in SELL_WORDS) else "buy"
    for w in SELL_WORDS + BUY_WORDS + ("價位", "價格", "元", "@"):
        rest = rest.replace(w, " ")
    mini = "小型" in rest
    rest = rest.replace("小型", " ")
    price = None
    tokens = []
    for tok in rest.split():
        if re.fullmatch(r"\d+(\.\d+)?", tok) and not re.fullmatch(r"\d{4,6}", tok):
            price = float(tok)
        elif re.fullmatch(r"\d{4,6}", tok) and price is None and tokens:
            price = float(tok)  # 例如「台積電 2520 1口」
        else:
            tokens.append(tok)
    if price is None or not tokens:
        return None
    return {"cmd": "fill", "side": side, "query": tokens[0], "price": price, "qty": int(qty_m.group(1)), "mini": mini}


def resolve(query: str, listings: dict, futures: dict, mini: bool = False) -> tuple[str, str, str, int] | None:
    """股票名稱／代號／股期契約代碼 → (代號, 名稱, 契約, 每口股數)。"""
    q = query.strip().upper()
    for code, info in futures.items():
        if q in (info.get("std"), info.get("mini")):
            mult = bt.MINI if q == info.get("mini") else bt.STD
            return code, (listings.get(code) or {}).get("name", code), q, mult
    code = q if q in futures else next((c for c, v in listings.items()
                                        if (v.get("name") or "").replace("*", "") == query.replace("*", "")), None)
    if not code or code not in futures:
        return None
    info = futures[code]
    if (mini and info.get("mini")) or not info.get("std"):
        return code, (listings.get(code) or {}).get("name", code), info["mini"], bt.MINI
    return code, (listings.get(code) or {}).get("name", code), info["std"], bt.STD


def buy(st: dict, code: str, name: str, contract: str, mult: int, price: float, qty: int, atr: float,
        rate: float) -> str:
    p = PARAMS
    stop = price - p.atr_mult * atr
    risk = (price - stop) * mult * qty
    same = next((x for x in st["positions"] if x["contract"] == contract), None)
    warn = []
    if risk > st["equity"] * p.risk_pct * 1.05:
        warn.append(f"⚠️ 這筆最大虧損約 {risk:,.0f}，超過 2% 規則（{st['equity'] * p.risk_pct:,.0f}）")
    if not same and len(st["positions"]) >= p.max_pos:
        warn.append(f"⚠️ 持倉已超過 {p.max_pos} 檔上限")
    margin = price * mult * qty * rate
    need = margin * p.margin_mult
    used = sum(bt.reserve(x["entry"], x["mult"], x["qty"], x.get("rate", rate), p)
               for x in st["positions"] if x is not same)
    if used + need > st["equity"]:
        warn.append(f"⚠️ 3 倍保證金準備：這筆要 {need:,.0f}，加上其他持倉共 {used + need:,.0f}，超過權益 {st['equity']:,.0f}")
    if same:  # 加碼：平均成本，停損用新的平均成本重算
        total = same["qty"] + qty
        same["entry"] = (same["entry"] * same["qty"] + price * qty) / total
        same["qty"] = same["qty0"] = total
        same["stop"] = same["entry"] - p.atr_mult * atr
        same["r0"] = same["entry"] - same["stop"]
        x = same
        head = f"➕ 加碼 {name} {contract} {qty} 口 @ {price:,.2f}，平均成本 {x['entry']:,.2f}，共 {total} 口"
    else:
        x = {"code": code, "name": name, "contract": contract, "mult": mult, "qty": qty, "qty0": qty,
             "entry": price, "stop": stop, "r0": price - stop, "rate": rate, "entry_date": f"{now_tw():%Y-%m-%d}",
             "days": 0, "half_done": False, "realized": 0.0, "alerts": []}
        st["positions"].append(x)
        head = f"🟢 已記錄買進 {name}（{code}）{contract} {qty} 口 @ {price:,.2f}"
    lines = [head,
             f"停損 {x['stop']:,.2f}（−1.5ATR）｜+2R 目標 {x['entry'] + p.take_r * x['r0']:,.2f}",
             f"最大虧損約 {(x['entry'] - x['stop']) * mult * x['qty']:,.0f}｜原始保證金約 {margin:,.0f}，"
             f"建議準備 3 倍 {need:,.0f}",
             "出場規則：碰停損出場；到 +2R 先出一半、停損移到成本；收盤跌破 20 日線隔天出場；10 天未達 +1R 出場"]
    return "\n".join(lines + warn)


def sell(st: dict, contract: str, name: str, price: float, qty: int) -> str:
    x = next((x for x in st["positions"] if x["contract"] == contract), None)
    if not x:
        return f"找不到 {name} {contract} 的持倉，請確認契約（打「持倉」查看）"
    qty = min(qty, x["qty"])
    cost = bt.FEE * qty * 2 + (x["entry"] + price) * x["mult"] * qty * bt.TAX
    pnl = (price - x["entry"]) * x["mult"] * qty - cost
    x["realized"] += pnl
    x["qty"] -= qty
    st["equity"] += pnl
    r = pnl / (x["r0"] * x["mult"] * qty) if x["r0"] > 0 else 0
    msg = [f"{'✅' if pnl >= 0 else '🔴'} 已記錄賣出 {name} {contract} {qty} 口 @ {price:,.2f}｜損益 {pnl:+,.0f}（{r:+.1f}R，已扣手續費與期交稅）"]
    if x["qty"] <= 0:
        st["positions"].remove(x)
        st["closed"].append({**x, "exit": price, "exit_date": f"{now_tw():%Y-%m-%d}", "pnl": round(x["realized"])})
        msg.append(f"{name} 全部出場，這筆合計 {x['realized']:+,.0f}")
    else:
        if not x["half_done"] and price >= x["entry"] + PARAMS.take_r * x["r0"] * 0.95:
            x["half_done"], x["stop"] = True, x["entry"]
            msg.append(f"剩 {x['qty']} 口，停損移到成本 {x['entry']:,.2f}")
        else:
            msg.append(f"剩 {x['qty']} 口")
    msg.append(equity_line(st))
    return "\n".join(msg)


def equity_line(st: dict) -> str:
    eq = st["equity"]
    return (f"💰 實單權益（已實現）{eq:,.0f}｜報酬 {eq / st['capital'] - 1:+.1%}｜目標 {TARGET:,.0f}"
            f" 還差 {max(0, TARGET - eq):,.0f}")


def positions_text(st: dict, closes: dict | None = None) -> str:
    closes = closes or {}
    if not st["positions"]:
        return "📒 實單持倉：無\n" + equity_line(st)
    lines = ["📒 實單持倉"]
    for x in st["positions"]:
        c = closes.get(x["code"])
        u = f"｜現價 {c:,.2f} 未實現 {(c - x['entry']) * x['mult'] * x['qty']:+,.0f}" if c else ""
        lines.append(f"・{x['name']} {x['contract']} {x['qty']} 口｜成本 {x['entry']:,.2f}｜停損 {x['stop']:,.2f}{u}")
    used = sum(bt.reserve(x["entry"], x["mult"], x["qty"], x.get("rate", PARAMS.margin_rate), PARAMS)
               for x in st["positions"])
    lines.append(f"🧱 3 倍保證金準備：已用 {used:,.0f}／權益 {st['equity']:,.0f}，還可用 {max(0, st['equity'] - used):,.0f}")
    return "\n".join(lines + [equity_line(st)])


def daily_check(st: dict, day: pd.Timestamp, prepped: dict) -> list[str]:
    """盤後用當天日 K 檢查實單持倉，產生出場提醒（不會自動平倉，等你回報賣出）。"""
    p = PARAMS
    out = []
    for x in st["positions"]:
        d = prepped.get(x["code"])
        if d is None or day not in d.index:
            continue
        row = d.loc[day]
        if x.get("last_check") == str(day.date()):
            continue
        x["last_check"] = str(day.date())
        if str(day.date()) > x["entry_date"]:
            x["days"] += 1
        if row["low"] <= x["stop"]:
            out.append(f"🔴 {x['name']} {x['contract']} 今天碰到停損 {x['stop']:,.2f}（最低 {row['low']:,.2f}），請出場")
        elif not x["half_done"] and row["high"] >= x["entry"] + p.take_r * x["r0"]:
            out.append(f"🎯 {x['name']} {x['contract']} 到 +2R（{x['entry'] + p.take_r * x['r0']:,.2f}），"
                       f"建議先出一半（{max(1, x['qty'] // 2)} 口），剩下停損移到成本 {x['entry']:,.2f}")
        if row["close"] < row[p.exit_ma] and x["days"] >= 1:
            out.append(f"⚠️ {x['name']} {x['contract']} 收盤 {row['close']:,.2f} 跌破 20 日線 {row[p.exit_ma]:,.2f}，明天開盤出場")
        elif x["days"] >= p.time_stop and not x["half_done"] and row["close"] < x["entry"] + x["r0"]:
            out.append(f"⏰ {x['name']} {x['contract']} 持有 {x['days']} 天未達 +1R，明天開盤出場")
    return out


HELP = """📖 進出場機器人指令
・買進：亞泥 35.7 1口（也可以：買 DYF 35.7 1口、亞泥 小型 35.7 1口）
・賣出：賣 亞泥 36.5 1口
・持倉：查看持倉、損益、權益
・檢查：檢查 國巨（看今天有沒有訊號、停損距離、一口虧多少、為什麼能做或不能做）
回報後機器人會算停損、+2R 目標、最大虧損，並在每天盤後提醒出場。機器人不會自動下單。"""


def explain(code: str, name: str, df: pd.DataFrame, info: dict, market_ok: bool, equity: float, rate: float) -> str:
    """說明某檔股票在最新一天的訊號與口數計算（能不能做、卡在哪條規則）。"""
    p = PARAMS
    d = bt.prepare(df, p)
    row = d.iloc[-1]
    day = d.index[-1]
    close, a = float(row["close"]), float(row["atr"])
    stop = close - p.atr_mult * a
    dist = close - stop
    lines = [f"🔎 {name}（{code}）{day:%m/%d} 收盤 {close:,.2f}",
             f"大盤：{'🟢 多頭' if market_ok else '🔴 空頭／盤整（不開新倉）'}"]
    hh, vr, ma60 = float(row["hh"]), float(row["vr"]), float(row["ma60"])
    a_ok = bool(row["sig_a"])
    lines.append(f"突破訊號：{'✅' if a_ok else '❌'}（收盤 {close:,.1f} vs 前 20 日高 {hh:,.1f}｜量比 {vr:.1f}，需 ≥{p.vol_ratio}"
                 f"｜60 日線 {ma60:,.1f}）")
    lines.append(f"拉回訊號：{'✅' if bool(row['sig_b']) else '❌'}（20 日線 {float(row['ma20']):,.1f}）")
    lines.append(f"停損距離：1.5×ATR = 1.5×{a:,.1f} = {dist:,.1f}（停損約 {stop:,.1f}）")
    risk_cap = equity * p.risk_pct
    lines.append(f"每筆風險上限：權益 {equity:,.0f} × 2% = {risk_cap:,.0f}")
    for key, mult, label in (("mini", bt.MINI, "小型"), ("std", bt.STD, "一般")):
        c = info.get(key)
        if not c:
            lines.append(f"{label}股期：無")
            continue
        one = dist * mult
        res = close * mult * rate * p.margin_mult
        ok = one <= risk_cap and res <= equity
        why = "可做 1 口以上" if ok else ("停損一口虧損超過上限" if one > risk_cap else "3 倍保證金超過權益")
        lines.append(f"{label}股期 {c}：一口停損約虧 {one:,.0f}｜3 倍保證金 {res:,.0f}｜{'✅' if ok else '❌'} {why}")
    contract, mult, qty = bt.size(equity, close, stop, info, p, rate)
    verdict = ("✅ 規則允許：" + f"{contract} {qty} 口") if qty and market_ok and (a_ok or bool(row["sig_b"])) else \
        ("❌ 今天沒有進場訊號" if not (a_ok or bool(row["sig_b"])) else
         ("❌ 大盤空頭，不開新倉" if not market_ok else
          f"❌ 有訊號但 10 萬本金規則做不了，要做 1 口需本金約 {dist * (bt.MINI if info.get('mini') else bt.STD) / p.risk_pct:,.0f}"))
    lines += ["", verdict]
    return "\n".join(lines)
