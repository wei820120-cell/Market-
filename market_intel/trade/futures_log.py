"""微台計畫帳的「觸發紀錄」：日盤＋夜盤每一次觸發（進場／加碼／出場／停損…）和每一場的損益，累積存一份，跟著 state/ 一起 commit。

- state/futures_log.json：原始紀錄（機器讀）。每一場一筆：events＝這場所有觸發（時間、價格、訊號全文）、summary＝這場目前的點數與元。
- state/futures_log.md：同一份資料排成人看的表（總表＋逐日明細），每次有新觸發就重寫。
- 即時記錄：cli 的日盤、夜盤每次 step 有觸發就 record()，收盤再 refresh() 一次（把最後的損益寫進去）。
- 歷史補錄：日盤上線（2026-11-04）到夜盤加入之間，當天沒把訊號全文存下來，只剩當天存檔裡的觸發代號與損益，
  所以補錄的事件只有「順序＋能從代號推回的時間」，標 source=補錄；損益是當時存檔的數字（準）。

只是模擬計畫帳的紀錄，不是真實成交，也不是投資建議。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

from . import futures_day

log = logging.getLogger(__name__)

LOG_PATH = futures_day.STATE_PATH.parent / "futures_log.json"
MD_PATH = futures_day.STATE_PATH.parent / "futures_log.md"


# ---------------------------------------------------------------- 讀寫

def _paths(path: Path | None) -> tuple[Path, Path]:
    """測試時給別的 state 檔路徑，紀錄就跟著放在同一個資料夾，不會寫到真的 state/。"""
    if path is None:
        return LOG_PATH, MD_PATH
    return path.parent / "futures_log.json", path.parent / "futures_log.md"


def load(path: Path | None = None) -> dict:
    jp, _ = _paths(path)
    try:
        d = json.loads(jp.read_text(encoding="utf-8"))
        if isinstance(d, dict) and isinstance(d.get("sessions"), list):
            return d
    except (OSError, ValueError):
        pass
    return {"sessions": []}


def _sort_key(s: dict) -> tuple:
    return (s["date"].rstrip("N"), 1 if s["session"] == "night" else 0)


def save(data: dict, path: Path | None = None) -> None:
    jp, mp = _paths(path)
    data["sessions"].sort(key=_sort_key)
    jp.parent.mkdir(parents=True, exist_ok=True)
    jp.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    mp.write_text(render_md(data), encoding="utf-8")


def _session(data: dict, st: dict, session: str) -> dict:
    for s in data["sessions"]:
        if s["date"] == st["date"] and s["session"] == session:
            return s
    s = {"date": st["date"], "session": session, "source": "即時", "events": [], "summary": {}}
    data["sessions"].append(s)
    return s


# ---------------------------------------------------------------- 即時記錄

def record(st: dict, sigs: list[dict], cfg: dict, session: str = "day", path: Path | None = None) -> None:
    """這一筆報價觸發了訊號：把訊號和這一場目前的損益記下來。重複送同一筆不會重複記。"""
    if not sigs:
        return
    data = load(path)
    s = _session(data, st, session)
    seen = {(e.get("ts"), e["kind"], e.get("price")) for e in s["events"]}
    snap = futures_day.snapshot(st, cfg)
    for g in sigs:
        k = (g.get("ts"), g["kind"], g.get("price"))
        if k in seen:
            continue
        seen.add(k)
        s["events"].append({"ts": g.get("ts"), "time": g["time"], "kind": g["kind"], "price": g["price"], "text": g["text"],
                            "lots_after": st["lots"], "pts_after": snap["pts"]})
    s["summary"] = snap
    save(data, path)


def refresh(st: dict, cfg: dict, session: str = "day", finished: bool = True, path: Path | None = None) -> None:
    """收盤（或想更新損益）時：只更新這一場的損益數字。這一場一次觸發都沒有就不記。"""
    data = load(path)
    for s in data["sessions"]:
        if s["date"] == st["date"] and s["session"] == session:
            s["summary"] = futures_day.snapshot(st, cfg)
            s["finished"] = finished
            save(data, path)
            return


def safe(fn, *a, **kw) -> None:
    """記錄失敗不能影響訊號發送，只留一行警告。"""
    try:
        fn(*a, **kw)
    except Exception as e:  # noqa: BLE001
        log.warning("微台觸發紀錄寫入失敗：%s", e)


# ---------------------------------------------------------------- 歷史補錄（只有觸發代號、沒有訊號全文）

def _bar_time(key: str) -> str:
    """5 分 K 代號 '20261105-0920' → 這根 K 收完的時間 '09:25'。"""
    hh, mm = int(key[-4:-2]), int(key[-2:])
    mm += 5
    return f"{hh + mm // 60:02d}:{mm % 60:02d}"


def events_from_keys(st: dict, cfg: dict) -> list[dict]:
    """用當天存檔裡的觸發代號（依發生順序）推回事件清單。價格與多數時間沒留下來，留空；只在代號本身帶時間時填「約」。"""
    word = {"long": "多", "short": "空"}.get(st["dir"], "")
    out: list[dict] = []
    for key in st["alerts"]:
        e: dict = {"ts": None, "time": "", "kind": "", "price": None, "text": ""}
        if key == "qualified":
            e.update(kind="盤面", text="🟢 盤面合格（振幅達加碼合格線）")
        elif key.startswith("enter"):
            n = int(key[5:])
            e.update(kind="進場", text=f"{'🔺 做多' if st['dir'] == 'long' else '🔻 做空'}試單（第 {n} 次進場）")
            if n == st["entries"] and st.get("entry_bar"):
                e["time"] = f"約 {st['entry_bar'][-4:-2]}:{st['entry_bar'][-2:]}～{_bar_time(st['entry_bar'])}（那根 5 分 K 內）"
        elif key.startswith("add:"):
            e.update(kind="加碼", time="約 " + _bar_time(key[4:]), text=f"➕ 拉回加碼（{key[-4:-2]}:{key[-2:]} 的 5 分 K 收完後）")
        elif key.startswith("ext:"):
            lots = int(key.split(":")[2])
            e.update(kind="加碼", text=f"➕ 順勢加碼（做{word}，累計 {lots} 口）")
        elif key.startswith("exit_half"):
            e.update(kind="出場", text="⏰ 分批出場（出一半）")
        elif key == "exit_final":
            e.update(kind="出場", text="⏰ 收盤前出場（剩下全出）")
        elif key == "blowoff":
            e.update(kind="出場", text="⚠️ 提早出場（連漲／連跌日型、行情亂跳）")
        elif key == "dailyloss":
            e.update(kind="停損", text="🛑 當日虧損達上限，全出、不再做")
        elif key.startswith("stop"):
            e.update(kind="停損", text="🛑 停損全出")
        else:
            continue
        out.append(e)
    return out


def backfill(st: dict, cfg: dict, session: str = "day", path: Path | None = None, finished: bool = True) -> bool:
    """把一天已存的最終狀態補進紀錄（只補紀錄裡還沒有的場次、而且要有進場才算「觸發」）。回傳有沒有補。"""
    if not st.get("entries"):
        return False
    data = load(path)
    if any(s["date"] == st["date"] and s["session"] == session for s in data["sessions"]):
        return False
    data["sessions"].append({"date": st["date"], "session": session, "source": "補錄", "finished": finished,
                             "events": events_from_keys(st, cfg), "summary": futures_day.snapshot(st, cfg)})
    save(data, path)
    return True


# ---------------------------------------------------------------- 排成人看的

def _name(s: dict) -> str:
    return "夜盤" if s["session"] == "night" else "日盤"


def _day_label(s: dict) -> str:
    return s["date"].rstrip("N")


def stats(data: dict) -> dict:
    """總計（只算有進場的場次）。"""
    rows = [s for s in data["sessions"] if s.get("summary", {}).get("entries")]
    twd = [s["summary"]["twd"] for s in rows]
    cum, peak, dd = 0, 0, 0
    for x in twd:
        cum += x
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
    return {"n": len(rows), "win": sum(1 for x in twd if x > 0), "lose": sum(1 for x in twd if x < 0),
            "pts": round(sum(s["summary"]["pts"] for s in rows), 1), "twd": cum, "max_dd": dd}


def render_md(data: dict) -> str:
    sess = data["sessions"]
    st = stats(data)
    lines = ["# 微台計畫帳 — 觸發紀錄（模擬、不下單）", "",
             "> 日盤＋夜盤每次觸發（進場、加碼、出場、停損）與每場損益，累積存放。元 ＝ 點數 × 每點 10 元（微台）。",
             "> 「補錄」＝記錄功能上線前的場次（日盤 11/4～11/12、夜盤 11/12 前半場）：當時沒存訊號全文，只剩觸發順序與損益（損益是當時存檔的數字）；事件時間只在代號帶得出來時才寫。",
             "> 夜盤的門檻是暫定值，尚未校準；以上只是模擬計畫帳，不是真實成交，也不是投資建議。", ""]
    if not st["n"]:
        return "\n".join(lines + ["（還沒有觸發紀錄）", ""])
    lines += [f"**累計**：{st['n']} 場有進場，賺 {st['win']}／賠 {st['lose']}，合計 {st['pts']:+,.0f} 點（{st['twd']:+,} 元）；"
              f"累計損益曲線最大回落 {st['max_dd']:,} 元。", "",
              "## 總表", "", "| 日期 | 場次 | 方向 | 進場 | 加碼 | 點數 | 元 | 累計元 | 備註 |", "|---|---|---|---|---|---:|---:|---:|---|"]
    cum = 0
    for s in sess:
        m = s.get("summary") or {}
        if not m.get("entries"):
            continue
        cum += m["twd"]
        note = []
        if s.get("source") == "補錄":
            note.append("補錄")
        if not s.get("finished"):
            note.append("進行中（含未平倉）" if m.get("lots_left") else "進行中")
        d = {"long": "做多", "short": "做空"}.get(m.get("dir"), "—")
        lines.append(f"| {_day_label(s)} | {_name(s)} | {d} | {m['entries']} | {m['adds']} | {m['pts']:+,.0f} | {m['twd']:+,} | {cum:+,} | {'、'.join(note)} |")
    lines += ["", "## 逐場明細（新到舊）", ""]
    for s in reversed(sess):
        m = s.get("summary") or {}
        if not s["events"] and not m.get("entries"):
            continue
        d = {"long": "做多", "short": "做空"}.get(m.get("dir"), "")
        head = f"### {_day_label(s)} {_name(s)}" + (f"｜{d}" if d else "")
        if m:
            head += f"｜{m.get('pts', 0):+,.0f} 點（{m.get('twd', 0):+,} 元）"
        lines += [head, ""]
        if m and m.get("open") is not None:
            lines.append(f"開 {m['open']:,.0f}、高 {m['high']:,.0f}、低 {m['low']:,.0f}、收／最新 {m['close']:,.0f}，振幅 {m['range_pct']:.2f}%"
                         f"{'；來源：補錄' if s.get('source') == '補錄' else ''}")
            lines.append("")
        for e in s["events"]:
            t = e["time"] or "時間未留存"
            if e.get("ts") and e["ts"][:10] != _day_label(s):
                t = "隔天 " + t            # 夜盤跨午夜
            px = f" {e['price']:,.0f}" if e.get("price") is not None else ""
            lines.append(f"- {t}{px}｜{e['text']}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------- 績效摘要（推播用）

def _is_done(s: dict, now: datetime) -> bool:
    """這一場結束了嗎：存檔有收盤旗標、或已過收盤時間（日盤 13:35、夜盤隔天 05:00）。"""
    if s.get("finished"):
        return True
    d = datetime.strptime(_day_label(s), "%Y-%m-%d").replace(tzinfo=now.tzinfo)
    end = d.replace(hour=13, minute=35) if s["session"] != "night" else (d + timedelta(days=1)).replace(hour=5, minute=0)
    return now >= end


def _line(s: dict) -> str:
    m = s["summary"]
    d = {"long": "做多", "short": "做空"}.get(m.get("dir"), "—")
    day = _day_label(s)[5:].replace("-", "/")
    tail = "（補錄）" if s.get("source") == "補錄" else ""
    return f"{day} {_name(s)} {d} {m['pts']:+,.0f} 點（{m['twd']:+,} 元）{tail}"


def performance_text(data: dict, now: datetime, live: list[dict] | None = None, last_n: int = 8) -> str:
    """微台計畫帳績效：累計（只算已結束、有進場的場次）、勝率與平均賺賠、最近幾場、進行中的場次。
    live：存檔裡正在進行的場次的最新快照（[{date, session, summary}]），會取代紀錄裡同一場過期的數字。
    只是模擬計畫帳，不是真實成交，也不是投資建議。"""
    sess = [dict(s) for s in data["sessions"]]
    for lv in live or []:
        for s in sess:
            if s["date"] == lv["date"] and s["session"] == lv["session"]:
                s["summary"] = lv["summary"]
                break
        else:
            if lv["summary"].get("entries"):
                sess.append({**lv, "events": [], "source": "即時"})
    sess.sort(key=_sort_key)
    with_entry = [s for s in sess if (s.get("summary") or {}).get("entries")]
    done = [s for s in with_entry if _is_done(s, now)]
    running = [s for s in with_entry if not _is_done(s, now)]
    st = stats({"sessions": done})
    head = "📊 微台計畫帳績效（模擬、不下單）"
    if not st["n"]:
        return head + "\n還沒有已結束且有進場的場次。"
    twd = [s["summary"]["twd"] for s in done]
    wins = [x for x in twd if x > 0]
    losses = [x for x in twd if x < 0]
    flat = st["n"] - len(wins) - len(losses)
    lines = [head,
             f"累計 {st['n']} 場有進場：賺 {len(wins)}／賠 {len(losses)}" + (f"／平 {flat}" if flat else "") +
             f"，合計 {st['pts']:+,.0f} 點（{st['twd']:+,} 元）",
             f"累計損益曲線最大回落 {st['max_dd']:,} 元"]
    if wins and losses:
        lines.append(f"勝率 {len(wins) / (len(wins) + len(losses)) * 100:.0f}%（賺÷賺賠場數）｜平均賺 {sum(wins) / len(wins):+,.0f} 元、平均賠 {sum(losses) / len(losses):+,.0f} 元")
    lines += ["", f"最近 {min(last_n, len(done))} 場："] + [_line(s) for s in reversed(done[-last_n:])]
    if running:
        lines += ["", "進行中："]
        for s in running:
            m = s["summary"]
            lines.append(_line(s) + f"，未平 {m.get('lots_left', 0)} 口")
    n_fill = sum(1 for s in done if s.get("source") == "補錄")
    if n_fill:
        lines += ["", f"註：{n_fill} 場標「補錄」（記錄功能上線前的場次，損益是當時存檔的數字）。"]
    lines.append("只是模擬計畫帳，不是真實成交，也不是投資建議。")
    return "\n".join(lines)
