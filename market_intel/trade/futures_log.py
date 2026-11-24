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
from zoneinfo import ZoneInfo

from . import futures_day

log = logging.getLogger(__name__)

LOG_PATH = futures_day.STATE_PATH.parent / "futures_log.json"
MD_PATH = futures_day.STATE_PATH.parent / "futures_log.md"
YEARLY_PATH = futures_day.STATE_PATH.parent / "futures_yearly.md"
FIRST_SETTLE_MONTH = "2026-11"      # 日盤 11/4 上線；更早只有一筆 10/30 的測試紀錄（0 點），不做月結算
TZ = ZoneInfo("Asia/Taipei")


# ---------------------------------------------------------------- 讀寫

def _paths(path: Path | None) -> tuple[Path, Path, Path]:
    """測試時給別的 state 檔路徑，紀錄就跟著放在同一個資料夾，不會寫到真的 state/。"""
    if path is None:
        return LOG_PATH, MD_PATH, YEARLY_PATH
    return path.parent / "futures_log.json", path.parent / "futures_log.md", path.parent / "futures_yearly.md"


def load(path: Path | None = None) -> dict:
    jp = _paths(path)[0]
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
    jp, mp, yp = _paths(path)
    data["sessions"].sort(key=_sort_key)
    jp.parent.mkdir(parents=True, exist_ok=True)
    jp.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    mp.write_text(render_md(data), encoding="utf-8")
    yp.write_text(render_yearly_md(data, datetime.now(TZ)), encoding="utf-8")


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
    s.setdefault("rules", futures_day.rules_snapshot(cfg))          # 這一場用的規則參數：中途改過，報表會標出來
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
            s.setdefault("rules", futures_day.rules_snapshot(cfg))
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


def rule_changes(data: dict) -> list[dict]:
    """規則參數（停損、合格線、加碼、口數…）在同一種場次（日盤、夜盤各比各的：夜盤本來就用不同的時段與門檻）之間改過幾次：
    每次改動回（從哪一場起、哪些參數 舊→新）。只比對有記錄 rules 的場次（2026-11-23 起才記；更早的場次沒記，當時用的規則見 git 紀錄與 knowledge/methods）。"""
    out, prev = [], {}
    for s in data["sessions"]:
        r = s.get("rules")
        if not r:
            continue
        kind = s["session"]
        if kind in prev and r != prev[kind]:
            out.append({"date": _day_label(s), "session": _name(s),
                        "changes": [(k, prev[kind].get(k), r.get(k)) for k in sorted(set(prev[kind]) | set(r)) if prev[kind].get(k) != r.get(k)]})
        prev[kind] = r
    return out


def _changes_text(changes: list[tuple]) -> str:
    return "、".join(f"{k} {old}→{new}" for k, old, new in changes)


def segments(data: dict) -> list[dict]:
    """依規則改動的日期把有進場的場次切成幾段（日盤、夜盤一起算，同一天改的算同一個切點；沒記規則的舊場次歸第一段），各段小計。
    沒改過規則只有一段。"""
    cuts = sorted({c["date"] for c in rule_changes(data)})
    groups: dict[int, list[dict]] = {}
    for s in data["sessions"]:
        if (s.get("summary") or {}).get("entries"):
            groups.setdefault(sum(1 for d in cuts if d <= _day_label(s)), []).append(s)
    out = []
    for k in sorted(groups):
        rows = groups[k]
        out.append({"from": cuts[k - 1] if k else _day_label(rows[0]), "rows": rows, **stats({"sessions": rows})})
    return out


def _rules_lines(data: dict) -> list[str]:
    """報表裡的規則改動提醒（沒改過規則回空：報表和以前逐字相同）。"""
    ch = rule_changes(data)
    if not ch:
        return []
    out = ["> ⚠️ **規則參數在中途改過**：下面的累計損益、勝率跨越不同規則，不能直接相加比較；請看各段小計。（沒記規則的舊場次歸第一段）"]
    out += [f"> - {c['date']} {c['session']}起：{_changes_text(c['changes'])}" for c in ch]
    out += ["", "**各段小計**："]
    for i, g in enumerate(segments(data), 1):
        out.append(f"- 第 {i} 段（{g['from']} 起）：{g['n']} 場有進場，賺 {g['win']}／賠 {g['lose']}，合計 {g['pts']:+,.0f} 點（{g['twd']:+,} 元）")
    return out + [""]


def render_md(data: dict) -> str:
    sess = data["sessions"]
    st = stats(data)
    lines = ["# 微台計畫帳 — 觸發紀錄（模擬、不下單）", "",
             "> 日盤＋夜盤每次觸發（進場、加碼、出場、停損）與每場損益，累積存放。元 ＝ 點數 × 每點 10 元（微台）。",
             "> 「補錄」＝記錄功能上線前的場次（日盤 11/4～11/12、夜盤 11/12 前半場）：當時沒存訊號全文，只剩觸發順序與損益（損益是當時存檔的數字）；事件時間只在代號帶得出來時才寫。",
             "> 另外 11/16 日盤也是補錄：當天的存檔步驟失敗（已修正），只剩收盤推播的彙總數字（進場、加碼次數與損益），沒有逐筆訊號與方向。",
             "> 夜盤的門檻是暫定值，尚未校準；以上只是模擬計畫帳，不是真實成交，也不是投資建議。",
             "> 每月結算與年度績效表：state/futures_yearly.md。", ""]
    if not st["n"]:
        return "\n".join(lines + ["（還沒有觸發紀錄）", ""])
    lines += [f"**累計**：{st['n']} 場有進場，賺 {st['win']}／賠 {st['lose']}，合計 {st['pts']:+,.0f} 點（{st['twd']:+,} 元）；"
              f"累計損益曲線最大回落 {st['max_dd']:,} 元。", ""]
    lines += _rules_lines(data)
    lines += ["## 總表", "", "| 日期 | 場次 | 方向 | 進場 | 加碼 | 點數 | 元 | 累計元 | 備註 |", "|---|---|---|---|---|---:|---:|---:|---|"]
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
    changes = rule_changes({"sessions": sess})
    if changes:
        c = changes[-1]
        lines.append(f"⚠️ 規則參數在 {c['date'][5:].replace('-', '/')} {c['session']}起改過（{_changes_text(c['changes'])}），上面的累計跨越不同規則。")
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
        lines += ["", f"註：{n_fill} 場標「補錄」（記錄功能上線前，或當天存檔失敗、只剩收盤彙總數字的場次）。"]
    lines.append("只是模擬計畫帳，不是真實成交，也不是投資建議。")
    return "\n".join(lines)


# ---------------------------------------------------------------- 損益總結（每則推播附上）、每月結算、年度績效表
# 場次歸月份＝場次代號的日期（夜盤算開盤那天，例：2026-11-30 夜盤跨到 12/1 凌晨，仍算 11 月）。只算有進場的場次。

def _month(s: dict) -> str:
    return _day_label(s)[:7]


def _entered(sessions: list[dict]) -> list[dict]:
    return [s for s in sessions if (s.get("summary") or {}).get("entries")]


def _sum(sessions: list[dict]) -> tuple[float, int]:
    rows = _entered(sessions)
    return round(sum(s["summary"]["pts"] for s in rows), 1), sum(s["summary"]["twd"] for s in rows)


def _money(pts: float, twd: int) -> str:
    return f"{pts:+,.0f} 點（{twd:+,} 元）"


def _tag(s: dict) -> str:
    return f"{_day_label(s)[5:].replace('-', '/')} {_name(s)}"


def pnl_line(st: dict, cfg: dict, session: str = "day", path: Path | None = None) -> str:
    """每則推播附的損益總結：這一場（含未平倉浮動）、本月累計、今年累計。拿紀錄檔裡別場的數字，這一場用目前存檔的最新快照。
    出錯回空字串（損益總結不能擋住進出場訊號）。"""
    try:
        snap = futures_day.snapshot(st, cfg)
        sess = [s for s in load(path)["sessions"] if not (s["date"] == st["date"] and s["session"] == session)]
        sess.append({"date": st["date"], "session": session, "summary": snap, "events": []})
        month = st["date"].rstrip("N")[:7]
        year = month[:4]
        m_pts, m_twd = _sum([s for s in sess if _month(s) == month])
        y_pts, y_twd = _sum([s for s in sess if _month(s)[:4] == year])
        unreal = f"（含未平 {snap['lots_left']} 口浮動 {snap['open_pts']:+,.0f} 點）" if snap["lots_left"] else ""
        return (f"💰 損益總結（模擬）：本場 {_money(snap['pts'], snap['twd'])}{unreal}\n"
                f"本月累計 {_money(m_pts, m_twd)}｜今年累計 {_money(y_pts, y_twd)}")
    except Exception as e:  # noqa: BLE001
        log.warning("微台損益總結計算失敗：%s", e)
        return ""


def monthly(data: dict, now: datetime | None = None) -> list[dict]:
    """每月結算：每個月一列（點數、元、賺賠場數、勝率、當月最大回落、最佳／最差一場）。給 now 就只算已結束的場次。"""
    sess = sorted((s for s in data["sessions"] if now is None or _is_done(s, now)), key=_sort_key)
    rows_all = _entered(sess)
    out = []
    for m in sorted({_month(s) for s in rows_all}):
        rows = [s for s in rows_all if _month(s) == m]
        st = stats({"sessions": rows})
        best = max(rows, key=lambda s: s["summary"]["twd"])
        worst = min(rows, key=lambda s: s["summary"]["twd"])
        out.append({"month": m, "n": st["n"], "win": st["win"], "lose": st["lose"], "flat": st["n"] - st["win"] - st["lose"],
                    "pts": st["pts"], "twd": st["twd"], "max_dd": st["max_dd"],
                    "best": {"tag": _tag(best), "twd": best["summary"]["twd"]},
                    "worst": {"tag": _tag(worst), "twd": worst["summary"]["twd"]}})
    return out


def _rate(win: int, lose: int) -> str:
    return f"{win / (win + lose) * 100:.0f}%" if win + lose else "—"


def yearly(data: dict, now: datetime | None = None) -> list[dict]:
    """年度績效：每年一列（含各月、全年最大回落、最佳／最差月）。"""
    sess = sorted((s for s in data["sessions"] if now is None or _is_done(s, now)), key=_sort_key)
    out = []
    for y in sorted({_month(s)[:4] for s in _entered(sess)}):
        rows = [s for s in sess if _month(s)[:4] == y]
        st = stats({"sessions": rows})
        months = monthly({"sessions": rows})
        best = max(months, key=lambda m: m["twd"])
        worst = min(months, key=lambda m: m["twd"])
        out.append({"year": y, "n": st["n"], "win": st["win"], "lose": st["lose"], "flat": st["n"] - st["win"] - st["lose"],
                    "pts": st["pts"], "twd": st["twd"], "max_dd": st["max_dd"], "months": months,
                    "best_month": {"month": best["month"], "twd": best["twd"]}, "worst_month": {"month": worst["month"], "twd": worst["twd"]}})
    return out


def render_yearly_md(data: dict, now: datetime) -> str:
    """state/futures_yearly.md：每月結算表＋年度績效表（只算已結束、有進場的場次；進行中的場次收盤後才計入）。"""
    lines = ["# 微台計畫帳 — 每月結算與年度績效表（模擬、不下單）", "",
             "> 由 state/futures_log.json 自動排出，每次紀錄更新就重寫。只算已結束、有進場的場次；元 ＝ 點數 × 每點 10 元（微台）。",
             "> 場次歸月份＝場次代號的日期（夜盤算開盤那天）。含「補錄」場次（記錄功能上線前或當天存檔失敗，只剩收盤彙總數字）。",
             "> 只是模擬計畫帳，不是真實成交，也不是投資建議；門檻是暫定值，尚未校準。", ""]
    years = yearly(data, now)
    if not years:
        return "\n".join(lines + ["（還沒有已結束且有進場的場次）", ""])
    cur = f"{now:%Y-%m}"
    lines += ["## 年度績效表", "", "| 年度 | 場次 | 賺／賠／平 | 勝率 | 點數 | 元 | 年內最大回落（元） | 最佳月 | 最差月 |", "|---|---:|---|---:|---:|---:|---:|---|---|"]
    for y in years:
        lines.append(f"| {y['year']} | {y['n']} | {y['win']}／{y['lose']}／{y['flat']} | {_rate(y['win'], y['lose'])} | {y['pts']:+,.0f} | {y['twd']:+,} | {y['max_dd']:,} | "
                     f"{y['best_month']['month']}（{y['best_month']['twd']:+,}） | {y['worst_month']['month']}（{y['worst_month']['twd']:+,}） |")
    for y in years:
        lines += ["", f"## {y['year']} 年每月結算", "",
                  "| 月份 | 場次 | 賺／賠／平 | 勝率 | 點數 | 元 | 累計元 | 當月最大回落（元） | 最佳一場 | 最差一場 | 備註 |",
                  "|---|---:|---|---:|---:|---:|---:|---:|---|---|---|"]
        cum = 0
        for m in y["months"]:
            cum += m["twd"]
            note = "進行中（尚未結算）" if m["month"] >= cur else ("已結算" if m["month"] >= FIRST_SETTLE_MONTH else "")
            lines.append(f"| {m['month']} | {m['n']} | {m['win']}／{m['lose']}／{m['flat']} | {_rate(m['win'], m['lose'])} | {m['pts']:+,.0f} | {m['twd']:+,} | {cum:+,} | "
                         f"{m['max_dd']:,} | {m['best']['tag']}（{m['best']['twd']:+,}） | {m['worst']['tag']}（{m['worst']['twd']:+,}） | {note} |")
    return "\n".join(lines).rstrip() + "\n"


def settle_text(m: dict, year_rows: list[dict]) -> str:
    """月結算推播：這個月的成績＋今年累計。"""
    mon = int(m["month"][5:])
    ytd = [r for r in year_rows if r["month"] <= m["month"] and r["month"][:4] == m["month"][:4]]
    y_pts, y_twd = round(sum(r["pts"] for r in ytd), 1), sum(r["twd"] for r in ytd)
    return "\n".join([
        f"🗓 微台計畫帳 {m['month'][:4]} 年 {mon} 月結算（模擬、不下單）",
        f"共 {m['n']} 場有進場：賺 {m['win']}／賠 {m['lose']}／平 {m['flat']}，勝率 {_rate(m['win'], m['lose'])}",
        f"本月損益 {_money(m['pts'], m['twd'])}｜當月累計最大回落 {m['max_dd']:,} 元",
        f"最佳一場 {m['best']['tag']} {m['best']['twd']:+,} 元｜最差一場 {m['worst']['tag']} {m['worst']['twd']:+,} 元",
        f"今年累計（{ytd[0]['month'][5:].lstrip('0') or '1'}～{mon} 月）{_money(y_pts, y_twd)}",
        "年度績效表：傳「年度」看，或 state/futures_yearly.md。",
        "只是模擬計畫帳，不是真實成交，也不是投資建議。"])


def settle(data: dict, now: datetime) -> list[str]:
    """還沒結算的整月（FIRST_SETTLE_MONTH 起、早於本月、有已結束的進場場次）：回傳月結算文字，並記進 data["settled"]（呼叫端要 save）。"""
    rows = monthly(data, now)
    done = set(data.get("settled", []))
    cur = f"{now:%Y-%m}"
    running = {_month(s) for s in _entered(data["sessions"]) if not _is_done(s, now)}      # 還有場次沒結束（例：月底夜盤跨到下個月凌晨）的月份先不結
    texts = []
    for m in rows:
        if FIRST_SETTLE_MONTH <= m["month"] < cur and m["month"] not in done and m["month"] not in running:
            texts.append(settle_text(m, rows))
            done.add(m["month"])
    if texts:
        data["settled"] = sorted(done)
    return texts


def monthly_text(data: dict, now: datetime) -> str:
    rows = monthly(data, now)
    if not rows:
        return "還沒有已結束且有進場的場次，沒有月結算。"
    cur = f"{now:%Y-%m}"
    lines = ["🗓 微台計畫帳每月結算（模擬、不下單）"]
    for m in reversed(rows[-6:]):
        lines.append(f"{m['month']}{'（進行中）' if m['month'] >= cur else ''}：{m['n']} 場，賺 {m['win']}／賠 {m['lose']}／平 {m['flat']}，勝率 {_rate(m['win'], m['lose'])}，"
                     f"{_money(m['pts'], m['twd'])}，最大回落 {m['max_dd']:,} 元")
    lines.append("只是模擬計畫帳，不是真實成交，也不是投資建議。")
    return "\n".join(lines)


def yearly_text(data: dict, now: datetime) -> str:
    years = yearly(data, now)
    if not years:
        return "還沒有已結束且有進場的場次，沒有年度績效。"
    lines = ["📆 微台計畫帳年度績效（模擬、不下單）"]
    for y in reversed(years):
        lines += ["", f"{y['year']} 年：{y['n']} 場，賺 {y['win']}／賠 {y['lose']}／平 {y['flat']}，勝率 {_rate(y['win'], y['lose'])}，{_money(y['pts'], y['twd'])}",
                  f"年內累計最大回落 {y['max_dd']:,} 元｜最佳月 {y['best_month']['month']} {y['best_month']['twd']:+,} 元｜最差月 {y['worst_month']['month']} {y['worst_month']['twd']:+,} 元",
                  "月別：" + "；".join(f"{int(m['month'][5:])} 月 {m['twd']:+,}" for m in y["months"])]
    lines += ["", "完整表格：state/futures_yearly.md。只是模擬計畫帳，不是真實成交，也不是投資建議。"]
    return "\n".join(lines)


# ---------------------------------------------------------------- 微台機器人的指令回覆

HELP = """微台機器人指令（直接傳文字）：
績效（或：微台）：累計損益、勝率、平均賺賠、最近 8 場
今天（或：進行中）：今天各場次的損益
明細（或：紀錄）：最近一場的觸發紀錄（時間、價格、訊號）
月結（或：月報）：每月結算（最近 6 個月）
年度（或：年報）：年度績效表
說明：這份說明
日盤／夜盤的進出場訊號、收盤彙總也都從這個機器人推送。
注意：進行中的場次要等收盤後才會存進紀錄，盤中看到的是上一場結束時的數字。
只是模擬計畫帳，不是真實成交，也不是投資建議。"""

_PERF = {"績效", "微台", "績效報告", "報告", "performance", "perf", "累計"}
_TODAY = {"今天", "今日", "進行中", "狀態", "今天績效"}
_DETAIL = {"明細", "紀錄", "記錄", "最近", "訊號"}
_MONTH = {"月結", "月報", "月結算", "每月", "本月", "月績效"}
_YEAR = {"年度", "年報", "年度績效", "今年", "年績效", "年表"}
_HELP = {"說明", "help", "/help", "/start", "start", "?", "？", "指令", "怎麼用"}


def reply(text: str, data: dict, now: datetime, live: list[dict] | None = None) -> str:
    """微台機器人收到一則文字的回覆（績效、今天、明細、說明）。看不懂回說明的提示，不會當成下單。"""
    t = (text or "").strip().lower().replace("　", " ")
    if t in _HELP:
        return HELP
    if t in _PERF:
        return performance_text(data, now, live)
    if t in _MONTH:
        return monthly_text(data, now)
    if t in _YEAR:
        return yearly_text(data, now)
    if t in _TODAY:
        today = f"{now:%Y-%m-%d}"
        yday = f"{now - timedelta(days=1):%Y-%m-%d}"
        sess = [s for s in data["sessions"] if (s["session"] != "night" and s["date"] == today)
                or (s["session"] == "night" and s["date"].rstrip("N") in (today, yday))]
        for lv in live or []:
            for s in sess:
                if s["date"] == lv["date"] and s["session"] == lv["session"]:
                    s["summary"] = lv["summary"]
        sess = [s for s in sess if (s.get("summary") or {}).get("entries")]
        if not sess:
            return f"今天（{now:%m/%d}）到目前還沒有進場紀錄（進行中的場次要等收盤後才會存進紀錄）。"
        return "📌 今天的場次\n" + "\n".join(_line(s) + ("" if _is_done(s, now) else "（進行中）") for s in sess)
    if t in _DETAIL:
        ev = [s for s in data["sessions"] if s.get("events")]
        if not ev:
            return "還沒有觸發紀錄。"
        s = ev[-1]
        head = f"🧾 {_day_label(s)[5:].replace('-', '/')} {_name(s)} 觸發紀錄"
        rows = []
        for e in s["events"][-12:]:
            when = e.get("time") or "時間未留存"
            px = f" {e['price']:,.0f}" if e.get("price") is not None else ""
            rows.append(f"- {when}{px}｜{e['text']}")
        return head + "\n" + "\n".join(rows)
    return "看不懂這則。傳「績效」看累計、「今天」看今天、「月結」看每月結算、「年度」看年度績效、「明細」看最近一場、「說明」看全部指令。"
