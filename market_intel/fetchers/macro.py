"""國際原物料、匯率、利率與風險指標（Yahoo Finance）。

用途：
- 常駐監看每 15 分鐘更新 state/macro_snapshot.json，Claude 研究題材時可以直接讀（不用另外查價格）
- 單日漲跌超過門檻時推新聞機器人（同方向一天一次）
- 重大事件推播時附上相關資產的即時漲跌
Yahoo 的期貨代號是連續近月合約，換月時價格可能跳動；數字是延遲報價，盤中請以券商為準。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from .. import config
from ..utils import ROOT, now_tw
from . import yahoo

log = logging.getLogger(__name__)

SNAP_PATH = ROOT / "state" / "macro_snapshot.json"
ALERT_PATH = ROOT / "state" / "macro_alerts.json"


def assets() -> dict:
    return config.load("macro_assets").get("assets") or {}


def fetch_one(symbol: str) -> dict | None:
    df, meta = yahoo.fetch_chart(symbol, range_="1mo", interval="1d")
    if df.empty or len(df) < 2:
        return None
    c = df["close"].astype(float)
    last, prev = float(c.iloc[-1]), float(c.iloc[-2])

    def pct(n: int) -> float | None:
        return round((last / float(c.iloc[-1 - n]) - 1) * 100, 2) if len(c) > n and float(c.iloc[-1 - n]) else None

    ts = meta.get("regularMarketTime")
    asof = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(now_tw().tzinfo).strftime("%m/%d %H:%M") if ts else ""
    return {"symbol": symbol, "last": round(last, 4), "prev": round(prev, 4), "d1_pct": pct(1), "d1_abs": round(last - prev, 4),
            "d5_pct": pct(5), "d20_pct": pct(20), "asof": asof}


def snapshot() -> list[dict]:
    rows = []
    for sym, spec in assets().items():
        try:
            r = fetch_one(sym)
        except Exception as e:  # noqa: BLE001 — 單一代號失敗不影響其他
            log.warning("國際行情 %s 抓取失敗：%s", sym, e)
            continue
        if r:
            rows.append({**r, "name": spec.get("name", sym), "group": spec.get("group", "")})
    return rows


def load() -> dict:
    if SNAP_PATH.exists():
        try:
            return json.loads(SNAP_PATH.read_text(encoding="utf-8"))
        except ValueError:
            pass
    return {}


def save_if_changed(rows: list[dict]) -> bool:
    """價格或報價時間有變才寫檔（週末休市不會一直產生 commit）。"""
    old = {r["symbol"]: (r["last"], r["asof"]) for r in load().get("rows", [])}
    new = {r["symbol"]: (r["last"], r["asof"]) for r in rows}
    if old == new or not rows:
        return False
    SNAP_PATH.parent.mkdir(parents=True, exist_ok=True)
    SNAP_PATH.write_text(json.dumps({"fetched": f"{now_tw():%Y-%m-%d %H:%M}", "rows": rows}, ensure_ascii=False, indent=1) + "\n",
                         encoding="utf-8")
    return True


def fresh(max_hours: float = 8) -> list[dict]:
    """快照還新才用（太舊的數字會誤導研究）。"""
    snap = load()
    try:
        t = datetime.strptime(snap.get("fetched", ""), "%Y-%m-%d %H:%M").replace(tzinfo=now_tw().tzinfo)
    except ValueError:
        return []
    return snap.get("rows", []) if now_tw() - t <= timedelta(hours=max_hours) else []


def fmt_row(r: dict) -> str:
    d = r.get("d1_pct")
    chg = f"{d:+.1f}%" if d is not None else "—"
    return f"{r['name']} {r['last']:,.2f}（{chg}）"


def line(rows: list[dict], symbols: list[str]) -> str:
    by = {r["symbol"]: r for r in rows}
    return "｜".join(fmt_row(by[s]) for s in symbols if s in by)


def table_text(rows: list[dict]) -> str:
    """給 AI／研究員看的行情快照（含5日、20日）。"""
    out = []
    for r in rows:
        def f(v):
            return f"{v:+.1f}%" if v is not None else "—"
        out.append(f"{r['group']}｜{r['name']}（{r['symbol']}）{r['last']:,.2f}｜日 {f(r['d1_pct'])}｜5日 {f(r['d5_pct'])}｜20日 {f(r['d20_pct'])}｜{r['asof']}")
    return "\n".join(out)


def alerts(rows: list[dict]) -> list[str]:
    """異動訊息（尚未推過的）；呼叫端推播後用 mark_alerted 記錄。"""
    cfg = assets()
    sent = json.loads(ALERT_PATH.read_text(encoding="utf-8")) if ALERT_PATH.exists() else {}
    today = f"{now_tw():%Y-%m-%d}"
    msgs = []
    for r in rows:
        spec = cfg.get(r["symbol"]) or {}
        d1, dabs = r.get("d1_pct"), r.get("d1_abs") or 0.0
        why = None
        if spec.get("alert_pct") and d1 is not None and abs(d1) >= float(spec["alert_pct"]):
            why = ("up" if d1 > 0 else "down", f"單日 {d1:+.1f}%")
        elif spec.get("alert_abs") and abs(dabs) >= float(spec["alert_abs"]):
            why = ("up" if dabs > 0 else "down", f"單日變動 {dabs:+.2f}")
        elif spec.get("alert_level") and r["last"] >= float(spec["alert_level"]) > r["prev"]:
            why = ("up", f"升破 {spec['alert_level']}")
        if not why or f"{r['symbol']}|{today}|{why[0]}" in sent:
            continue
        sent[f"{r['symbol']}|{today}|{why[0]}"] = 1
        extra = "｜".join(f"{k} {r[v]:+.1f}%" for k, v in (("5日", "d5_pct"), ("20日", "d20_pct")) if r.get(v) is not None)
        msgs.append(f"📈 國際行情異動｜{r['name']} {r['last']:,.2f}（{why[1]}）" + (f"\n  {extra}" if extra else "")
                    + (f"\n  {spec['note']}" if spec.get("note") else ""))
    if msgs:
        keep = {k: v for k, v in sent.items() if k.split("|")[1] >= f"{now_tw() - timedelta(days=3):%Y-%m-%d}"}
        ALERT_PATH.parent.mkdir(parents=True, exist_ok=True)
        ALERT_PATH.write_text(json.dumps(keep, ensure_ascii=False), encoding="utf-8")
    return msgs
