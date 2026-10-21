"""事件去重：同一個事件（不同標題、不同媒體）在 N 天內只研究、推播一次。

為什麼需要
- 研究題材的名稱是用新聞標題取的（供需事件：光纖缺貨喊漲…），同一件事不同媒體標題不同，就會被當成不同題材，
  幾小時或幾天內又研究一次、又推一次。原本的去重只比題材名稱（完全相同或互相包含）。

做法（設定在 config/news_keywords.yaml 的 event_dedupe）
1. 主題（subjects）：列出別名，DDR＝DRAM＝記憶體、銅箔基板＝CCL…，標題或題材名出現其中任一個詞就屬於該主題。
   比對時長詞優先（「銅箔基板」不會再被算成「銅箔」）。
2. 事件家族（families）：供需（需求爆發、產能吃緊、原料短缺）、事故（天災意外、減產停產）、政策（政策管制、進口管制公告）。
   同主題、同家族、N 天內 → 同一事件；不同家族（例如光纖缺貨之後出現光纖出口管制）算新事件。
3. 比對對象：研究庫近 N 天的「供需」類研究（名稱），以及研究佇列裡已排隊的事件。
4. 對不到主題別名時，用標題裡的詞（扣掉 generic_words 太籠統的字）和既有名稱互相包含來比對。

只處理「事件類」題材（供需事件：…、XXX供需）。個股題材（XXX（代號）題材與產業）不在這裡處理。
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from . import config

PREFIX = "供需事件："
DEFAULT_DAYS = 14
FAMILY_DEFAULT = "供需"


def settings() -> dict:
    return config.news_keywords().get("event_dedupe") or {}


def is_event_topic(topic: str) -> bool:
    t = (topic or "").strip()
    return t.startswith(PREFIX) or t.endswith("供需")


# ---------------------------------------------------------------- 主題與家族

def subjects_of(text: str, groups: dict | None = None) -> set[str]:
    """文字裡出現的主題（依別名）。長詞優先、重疊的短詞不算（「銅箔基板」不會再算成「銅箔」）。"""
    groups = settings().get("subjects") or {} if groups is None else groups
    t = (text or "").lower()
    aliases = sorted(((str(a).lower(), sid) for sid, als in groups.items() for a in (als or [])), key=lambda x: -len(x[0]))
    found: set[str] = set()
    spans: list[tuple[int, int]] = []
    for alias, sid in aliases:
        if not alias:
            continue
        start = 0
        while True:
            i = t.find(alias, start)
            if i < 0:
                break
            j = i + len(alias)
            if not any(i < b and a < j for a, b in spans):
                found.add(sid)
                spans.append((i, j))
            start = i + 1
    return found


def family_of(text: str, cause: str | None = None, causes: dict | None = None, families: dict | None = None) -> str:
    """事件家族。有明確原因（供需失衡偵測到的 cause）就用它，否則用原因的關鍵字比對文字，都沒有就算「供需」。"""
    cfg = settings()
    families = cfg.get("families") or {} if families is None else families
    if cause:
        return families.get(cause, FAMILY_DEFAULT)
    causes = (config.news_keywords().get("supply_shock") or {}).get("causes") or {} if causes is None else causes
    for name, pats in causes.items():
        if name in families and any(re.search(p, text or "", flags=re.I) for p in pats or []):
            return families[name]
    return FAMILY_DEFAULT


def auto_terms(text: str, generic: list[str] | None = None) -> set[str]:
    """沒有主題別名可用時的備援：把標題切成詞（扣掉籠統字眼、純數字），之後用「互相包含」比對。"""
    generic = settings().get("generic_words") or [] if generic is None else generic
    t = re.sub(r"[\s　]+", "", text or "")
    for w in sorted((str(g) for g in generic), key=len, reverse=True):
        t = t.replace(w, "|")
    terms = set()
    for p in re.split(r"[^一-鿿A-Za-z0-9.+*]+|\|+", t):
        if len(p) < 2 or re.fullmatch(r"[\d.%+*]+", p) or re.match(r"\d", p):
            continue
        if len(p) <= 8:
            terms.add(p.lower())
    return terms


def _terms_overlap(a_terms: set[str], a_text: str, b_terms: set[str], b_text: str) -> str | None:
    a_low, b_low = (a_text or "").lower(), (b_text or "").lower()
    for t in sorted(a_terms, key=len, reverse=True):
        if t in b_low:
            return t
    for t in sorted(b_terms, key=len, reverse=True):
        if t in a_low:
            return t
    return None


# ---------------------------------------------------------------- 找重複

def _title_of(trigger: str) -> str:
    """觸發文字（日期 原因：標題\\n摘要\\n網址）裡的標題部分。"""
    first = (trigger or "").split("\n", 1)[0]
    return re.sub(r"^\d{4}-\d{2}-\d{2}\s*[^：:]{0,12}[：:]", "", first).strip() or first


def same_event(a_name: str, a_text: str, a_family: str, b_name: str, b_text: str, b_family: str,
               cfg: dict | None = None) -> str | None:
    """兩個事件是不是同一件事。回傳比對到的主題或詞（None＝不同事件）。"""
    cfg = settings() if cfg is None else cfg
    if a_family != b_family:
        return None
    groups = cfg.get("subjects") or {}
    shared = subjects_of(f"{a_name} {a_text}", groups) & subjects_of(f"{b_name} {b_text}", groups)
    if shared:
        return sorted(shared)[0]
    # 對不到別名：標題詞互相包含（只用名稱＋標題，不用摘要，避免摘要裡的泛稱造成誤判）
    generic = cfg.get("generic_words") or []
    return _terms_overlap(auto_terms(a_name, generic) | auto_terms(_title_of(a_text), generic), f"{a_name} {_title_of(a_text)}",
                          auto_terms(b_name, generic) | auto_terms(_title_of(b_text), generic), f"{b_name} {_title_of(b_text)}")


def find_duplicate(topic: str, trigger: str, index: dict, queued: list[dict], *, cause: str | None = None,
                   now: datetime | None = None, days: int | None = None, cfg: dict | None = None) -> dict | None:
    """這個事件題材是不是近期已研究、或已在佇列裡排隊的同一件事。

    回傳 {"kind": "study"|"queue", "name": 既有題材名稱, "by": 比對到的主題或詞}，沒有重複回傳 None。
    """
    cfg = settings() if cfg is None else cfg
    if not cfg.get("enabled", True) or not is_event_topic(topic):
        return None
    days = int(cfg.get("days", DEFAULT_DAYS)) if days is None else days
    now = now or datetime.now()
    cutoff = (now - timedelta(days=days)).strftime("%Y-%m-%d")
    fam = family_of(f"{topic} {trigger}", cause)
    for name, meta in index.items():
        if not is_event_topic(name) or (meta or {}).get("date", "") < cutoff or name.strip() == topic.strip():
            continue
        by = same_event(topic, trigger, fam, name, "", family_of(name), cfg)
        if by:
            return {"kind": "study", "name": name, "by": by}
    for q in queued:
        name = q.get("topic", "")
        if not is_event_topic(name) or name.strip() == topic.strip():
            continue
        by = same_event(topic, trigger, fam, name, q.get("trigger", ""), family_of(f"{name} {q.get('trigger', '')}", q.get("cause")), cfg)
        if by:
            return {"kind": "queue", "name": name, "by": by}
    return None


# ---------------------------------------------------------------- 記錄「被視為重複」的新聞

def note_related(path: Path, name: str, trigger: str, today: str, keep: int = 30) -> None:
    """把被略過的相似新聞記在 state/research_related.json（既有研究的『後續報導』），之後要補研究可以參考。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (ValueError, OSError):
        data = {}
    lines = (trigger or "").split("\n")
    url = next((x.strip() for x in reversed(lines) if x.strip().startswith("http")), "")
    row = {"date": today, "title": _title_of(trigger)[:120], "url": url}
    rows = [r for r in data.get(name, []) if r.get("title") != row["title"]] + [row]
    data[name] = rows[-keep:]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
