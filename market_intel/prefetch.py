"""連動預研：看到 A 就把 B、C 和上下游一起排進研究，推播時才不會慢半拍。

只負責「排進研究佇列」，不自己研究。三個入口：
- 研究完成後（research._save_report）：題材裡的同層個股、相鄰上下游、「待確認」的個股 → expand()
- 新聞點名多檔個股的供應鏈名單（例如輝達看板列出台郡、台虹）→ from_news()
- 市場掃描：已知族群的個股漲停，但最近沒有任何研究提到它 → is_stale() ＋ expand_known()

限制（config/settings.yaml 的 research.prefetch）：每份研究最多 per_topic 檔、每天最多 per_day 檔、
最多往外擴 max_depth 層（A→B→C），免得一直擴下去。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta

from . import config, research
from .utils import now_tw

log = logging.getLogger(__name__)

TOPIC_RE = re.compile(r"[（(](\d{4,6})[）)]題材與產業$")
CHAIN_RE = re.compile(r"入列|供應鏈|名單|夥伴|看板|打入|拿下|送樣|通過.{0,6}認證|概念股|合作")
DEBT_WORDS = ("待確認", "未查到", "未取得", "本次未")  # 研究裡標示「沒查到」的個股：欠一份專門研究
IMPACT_RANK = {"受惠": 0, "中性": 1}  # 受傷的不預研（只關心可能出現機會的）
HOT_PCT = 5.0  # 當天已經動起來（漲跌 ≥ 5%）的相關個股，預研排在前面


def settings() -> dict:
    st = (config.settings().get("research") or {}).get("prefetch") or {}
    return {"enabled": st.get("enabled", True), "per_topic": int(st.get("per_topic", 3)),
            "per_day": int(st.get("per_day", 8)), "max_depth": int(st.get("max_depth", 2)),
            "stale_days": int(st.get("stale_days", 3)), "expire_days": int(st.get("expire_days", 3))}


def stock_topic(code: str, name: str) -> str:
    return f"{name}（{code}）題材與產業"


def origin_codes(topic: str) -> set[str]:
    m = TOPIC_RE.search(topic or "")
    return {m.group(1)} if m else set()


def dedicated_codes(index: dict, days: int = 14) -> set[str]:
    """最近 days 天內已經有「個股專屬研究」的代號。"""
    cutoff = (now_tw() - timedelta(days=days)).strftime("%Y-%m-%d")
    out = set()
    for name, meta in index.items():
        m = TOPIC_RE.search(name)
        if m and (meta or {}).get("date", "") >= cutoff:
            out.add(m.group(1))
    return out


def last_mention(code: str, index: dict) -> str:
    """最近一份提到這檔個股的研究日期（沒有就回空字串）。"""
    dates = [(meta or {}).get("date", "") for meta in index.values() if code in ((meta or {}).get("codes") or [])]
    return max(dates, default="")


def is_stale(code: str, index: dict, days: int | None = None) -> bool:
    """已知族群的個股，但近期沒有專屬研究、也沒有任何研究在 days 天內提到它 → 該重新看一眼。"""
    days = settings()["stale_days"] if days is None else days
    if code in dedicated_codes(index):
        return False
    cutoff = (now_tw() - timedelta(days=days)).strftime("%Y-%m-%d")
    return last_mention(code, index) < cutoff


# ---------- 每天上限 ----------

def _state_path():
    return research.QUEUE_DIR.parent / "prefetch.json"  # 和研究佇列放同一個 state 目錄


def used_today() -> int:
    try:
        st = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    return int(st.get("n", 0)) if st.get("date") == f"{now_tw():%Y-%m-%d}" else 0


def _bump(n: int) -> None:
    if n <= 0:
        return
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"date": f"{now_tw():%Y-%m-%d}", "n": used_today() + n}), encoding="utf-8")


# ---------- 找相關個股 ----------

def candidates(layers: list[dict], origins: set[str]) -> list[dict]:
    """研究分層裡的相關個股，依「離 A 多近」排序：同層（B、C）→ 相鄰上下游 → 其他；同距離先排受惠、待確認的。"""
    origin_layers = {i for i, l in enumerate(layers) for s in l.get("tw_stocks", []) if s.get("code") in origins}
    best: dict[str, tuple] = {}
    info: dict[str, dict] = {}
    for i, layer in enumerate(layers):
        for s in layer.get("tw_stocks", []):
            code, impact = s.get("code"), s.get("impact")
            if not code or code in origins or impact not in IMPACT_RANK:
                continue
            if not origin_layers:
                dist = 0
            elif i in origin_layers:
                dist = 0
            elif any(abs(i - j) == 1 for j in origin_layers):
                dist = 1
            else:
                dist = 2
            debt = any(w in (s.get("role") or "") for w in DEBT_WORDS)
            key = (dist, IMPACT_RANK[impact], 0 if debt else 1, i)
            if code not in best or key < best[code]:
                best[code] = key
                info[code] = {"code": code, "name": s.get("name") or code, "layer": layer.get("layer", ""),
                              "role": s.get("role", ""), "dist": dist, "debt": debt}
    return [info[c] for c in sorted(best, key=best.get)]


def _enqueue(cands: list[dict], listings: dict, *, origin: str, depth: int, why: str, flows: dict | None,
             limit: int, base: int = 0) -> list[str]:
    index = research.load_index()
    done = dedicated_codes(index)
    if flows:  # 當天已經在動的先排
        cands = sorted(cands, key=lambda c: -(abs((flows or {}).get(c["code"], 0)) >= HOT_PCT))
    queued: list[str] = []
    for c in cands:
        if len(queued) >= limit:
            break
        code = c["code"]
        if code not in listings or code.startswith("0") or code in done:
            continue
        topic = stock_topic(code, listings[code].get("name") or c["name"])
        hot = abs((flows or {}).get(code, 0)) >= HOT_PCT
        detail = f"（{c['layer']}：{c['role']}）" if c.get("layer") else ""
        trigger = f"{now_tw():%Y-%m-%d} 連動預研：{why}{detail}"[:400]
        if research.queue_research(topic, trigger, depth=depth + 1, origin=origin, priority=2 if hot else base):
            queued.append(topic)
    _bump(len(queued))
    return queued


def _budget(limit: int | None = None) -> int:
    st = settings()
    return max(0, min(limit if limit is not None else st["per_topic"], st["per_day"] - used_today()))


def expand(topic: str, layers: list[dict], listings: dict, *, depth: int = 0,
           flows: dict | None = None) -> list[str]:
    """研究完成後，把題材裡相關的個股排進研究。回傳新排入的題材名稱。"""
    st = settings()
    if not st["enabled"] or depth >= st["max_depth"] or not layers:
        return []
    n = _budget()
    if n <= 0:
        return []
    return _enqueue(candidates(layers, origin_codes(topic)), listings, origin=topic, depth=depth,
                    why=f"{topic} 研究完成，相關個股先研究起來", flows=flows, limit=n)


def expand_known(code: str, listings: dict, *, depth: int = 0, flows: dict | None = None) -> list[str]:
    """A（已知族群的個股）剛被排進研究：用既有研究的分層找出它的同層個股與上下游，一起排進去。"""
    st = settings()
    if not st["enabled"] or depth >= st["max_depth"]:
        return []
    n = _budget()
    if n <= 0:
        return []
    index = research.load_index()
    topics = sorted((t for t, meta in index.items() if code in ((meta or {}).get("codes") or [])),
                    key=lambda t: index[t].get("date", ""), reverse=True)
    name = (listings.get(code) or {}).get("name") or code
    for t in topics:  # 最近的一份有分層資料的研究
        p = research.data_dir() / f"{research.slug(t)}.json"
        try:
            layers = json.loads(p.read_text(encoding="utf-8")).get("layers") or []
        except (OSError, ValueError):
            continue
        cands = candidates(layers, {code})
        if cands:
            return _enqueue(cands, listings, origin=stock_topic(code, name), depth=depth, flows=flows, limit=n,
                            why=f"{name}（{code}）動起來了，同題材「{t}」的相關個股先研究")
    return []


def from_news(title: str, codes: list[str], listings: dict, url: str = "") -> list[str]:
    """新聞點名 2 檔以上個股的供應鏈名單（入列、夥伴、看板、拿下…）→ 全部排進研究。"""
    st = settings()
    codes = [c for c in dict.fromkeys(codes) if c in listings and not c.startswith("0")]
    if not st["enabled"] or len(codes) < 2 or not CHAIN_RE.search(title):
        return []
    n = _budget(max(st["per_topic"], 4))
    if n <= 0:
        return []
    cands = [{"code": c, "name": listings[c].get("name") or c, "layer": "", "role": "", "dist": 0, "debt": True}
             for c in codes]
    return _enqueue(cands, listings, origin=f"新聞：{title[:40]}", depth=0, flows=None, limit=n, base=1,
                    why=f"新聞點名供應鏈名單：{title[:80]} {url}".strip())
