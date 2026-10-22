"""早期訊號：供應鏈換料、轉單、淘汰、認證這類「還沒上媒體」的消息。

為什麼需要：現有新聞流程只看媒體標題，詞表也只有漲價、缺貨、接單、升評、利空這幾類，
「台燿掉 AWS、訂單去南亞」、「南亞 M9 遭 AWS 淘汰、轉向台燿」這種供應鏈傳聞（先出現在頻道、論壇、法人圈，隔幾天才上媒體）完全抓不到。

做法（不需要 AI 金鑰，規則式）：
1. 來源：Telegram 頻道、PTT 股版、RSS／Atom 來源、Google 新聞的供應鏈關鍵字搜尋、媒體標題，加上使用者直接傳給機器人的「情報 …」。
2. 偵測：文字裡有上市櫃公司、客戶或技術詞，加上換料／轉單／淘汰／認證的說法，並解析誰受惠、誰受傷。
3. 立刻推播（標「未證實」），同時排入求證研究（優先順序最高）；方向相反的訊號特別標出來。
4. 同一件事一天只推一次；記錄存在 state/signals.json。

推論與傳聞一律標示未證實；研究佇列的觸發文字附求證要點，研究完成才算數。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import timedelta

from . import config, notify, research
from .utils import ROOT, now_tw

log = logging.getLogger(__name__)

STATE_PATH = ROOT / "state" / "signals.json"
SENTENCE_END = re.compile(r"[。！？!?\n]+")


def settings() -> dict:
    return config.load("signals")


# ---------------------------------------------------------------- 資料結構

@dataclass
class Item:
    """一段要檢查的文字。kind：頻道、論壇、媒體、使用者轉傳。"""
    source: str
    text: str
    url: str = ""
    kind: str = "頻道"
    published: str = ""


@dataclass
class Signal:
    customers: list[str] = field(default_factory=list)
    winners: list[str] = field(default_factory=list)   # 代號
    losers: list[str] = field(default_factory=list)
    mentioned: list[str] = field(default_factory=list)  # 文字裡出現的所有公司代號
    products: list[str] = field(default_factory=list)
    strong: bool = False
    denied: bool = False   # 文字裡有「傳言…非事實／澄清」：公司或媒體已否認這個傳聞
    text: str = ""
    source: str = ""
    url: str = ""
    kind: str = ""

    @property
    def label(self) -> str:
        if self.denied:
            return "傳聞已被否認"
        if self.winners and self.losers:
            return "轉單"
        if self.losers:
            return "掉單／淘汰"
        if self.winners:
            return "接單／認證"
        return "供應鏈變動"

    @property
    def key(self) -> str:
        raw = json.dumps({"c": sorted(self.customers), "w": sorted(self.winners), "l": sorted(self.losers), "d": self.denied,
                          "m": [] if (self.winners or self.losers) else sorted(self.mentioned)}, ensure_ascii=False)
        return hashlib.md5(raw.encode()).hexdigest()[:12]


# ---------------------------------------------------------------- 找公司、客戶

def signal_names(listings: dict[str, dict], ambiguous: set[str] | None = None) -> dict[str, str]:
    """訊號比對用的「名稱 → 代號」：名稱 2 字以上、不是容易誤判的常用詞。"""
    amb = ambiguous or set()
    out = {}
    for code, info in listings.items():
        name = (info.get("name") or "").strip()
        if code.isdigit() and len(name) >= 2 and name not in amb:
            out[name] = code
    return out


def _terms(name_map: dict[str, str], aliases: dict[str, str]) -> dict[str, str]:
    terms = dict(name_map)
    for alias, official in (aliases or {}).items():
        code = name_map.get(official)
        if code:
            terms[alias] = code
    return terms


def _find_all(text: str, term: str, used: list[bool]):
    """term 在 text 的所有位置（英數字詞要整詞比對、不分大小寫），略過已被較長詞佔用的區段。"""
    if re.fullmatch(r"[A-Za-z0-9.\- ]+", term):
        pat = re.compile(r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])", re.I)
        starts = [m.start() for m in pat.finditer(text)]
    else:
        starts, i = [], text.find(term)
        while i >= 0:
            starts.append(i)
            i = text.find(term, i + 1)
    for s in starts:
        if not any(used[s:s + len(term)]):
            yield s


def find_terms(text: str, terms: dict[str, str]) -> list[tuple[int, int, str]]:
    """（起, 迄, 值）。長詞優先、不重疊。"""
    used = [False] * len(text)
    out = []
    for term in sorted(terms, key=len, reverse=True):
        for s in _find_all(text, term, used):
            for i in range(s, s + len(term)):
                used[i] = True
            out.append((s, s + len(term), terms[term]))
    return sorted(out)


def find_companies(text: str, name_map: dict[str, str], aliases: dict[str, str], customer_words: set[str]) -> list[tuple[int, int, str]]:
    terms = {t: c for t, c in _terms(name_map, aliases).items() if t not in customer_words}
    out = find_terms(text, terms)
    taken = [False] * len(text)
    for s, e, _ in out:
        for i in range(s, e):
            taken[i] = True
    for m in re.finditer(r"[(（]\s*(\d{4,5})\s*[)）]", text):  # 「台燿（6274）」
        code = m.group(1)
        if code in set(name_map.values()) and not any(taken[m.start():m.end()]):
            out.append((m.start(), m.end(), code))
    return sorted(out)


def find_customers(text: str, customers: dict[str, list[str]]) -> list[tuple[int, int, str]]:
    terms = {w: name for name, words in (customers or {}).items() for w in words}
    return find_terms(text, terms)


# ---------------------------------------------------------------- 解析誰受惠、誰受傷

def _nearest_before(pos: int, comps: list, gap: int, blockers: list | None = None):
    best = None
    for c in comps:
        if c[1] <= pos and pos - c[1] <= gap:
            if blockers and any(c[1] <= b[0] < pos for b in blockers):
                continue
            best = c
    return best


def _nearest_after(pos: int, comps: list, gap: int):
    for c in comps:
        if c[0] >= pos and c[0] - pos <= gap:
            return c
    return None


def parse_roles(text: str, comps: list, custs: list, cfg: dict | None = None) -> tuple[list[str], list[str]]:
    """（受惠代號, 受傷代號）。依句子逐一套規則：被動淘汰、主動淘汰、掉單、轉單去向、拿下／通過認證。"""
    cfg = cfg or settings()
    winners: list[str] = []
    losers: list[str] = []

    def add(lst: list[str], comp) -> None:
        if comp and comp[2] not in lst:
            lst.append(comp[2])

    elim = sorted(cfg.get("elim_verbs") or [], key=len, reverse=True)
    loss_cues = sorted(cfg.get("loss_subject_cues") or [], key=len, reverse=True)
    dest_cues = sorted(cfg.get("win_dest_cues") or [], key=len, reverse=True)
    win_cues = sorted(cfg.get("win_subject_cues") or [], key=len, reverse=True)
    markers = cfg.get("passive_markers") or ["遭", "被"]
    cust_words = [w for words in (cfg.get("customers") or {}).values() for w in words]
    cust_alt = "|".join(re.escape(w) for w in sorted(cust_words, key=len, reverse=True)) or "(?!)"
    elim_alt = "|".join(map(re.escape, elim)) or "(?!)"
    cursor = 0
    sentences = []
    for m in SENTENCE_END.finditer(text):
        sentences.append((cursor, m.start()))
        cursor = m.end()
    sentences.append((cursor, len(text)))
    for s0, s1 in sentences:
        sc = [c for c in comps if s0 <= c[0] < s1]
        if not sc:
            continue
        sk = [k for k in custs if s0 <= k[0] < s1]
        sent = text[s0:s1]
        consumed: list[tuple[int, int]] = []
        # 1) 被動：「南亞 M9 遭 AWS 淘汰」「台燿被南亞取代」
        for mk in markers:
            for m in re.finditer(re.escape(mk), sent):
                if sent[m.end():m.end() + 1] == "動":
                    continue  # 「被動元件」
                tail = sent[m.end():m.end() + 16]
                v = re.search(elim_alt + "|" + "|".join(map(re.escape, loss_cues)) if loss_cues else elim_alt, tail)
                if not v:
                    continue
                p = s0 + m.start()
                add(losers, _nearest_before(p, sc, 14))
                between = [c for c in sc if p < c[0] < s0 + m.end() + v.start()]
                for c in between:  # 被誰取代
                    add(winners, c)
                consumed.append((s0 + m.end() + v.start(), s0 + m.end() + v.end()))
        # 2) 主動淘汰：「AWS 逐步淘汰南亞…」「A 取代 B」
        for m in re.finditer(elim_alt, sent):
            a, b = s0 + m.start(), s0 + m.end()
            if any(c0 <= a < c1 for c0, c1 in consumed):
                continue
            obj = _nearest_after(b, [c for c in sc], 4)
            subj = _nearest_before(a, sc, 6, blockers=sk)
            add(losers, obj)
            if obj and subj and m.group(0) in ("取代", "擠下", "頂替"):
                add(winners, subj)
        # 3) 掉單：「台燿掉 AWS」「X 認證失敗」
        for m in re.finditer(r"(掉|丟|失去|丟失|流失)了?(?=單|訂單|客戶|" + cust_alt + ")", sent):
            add(losers, _nearest_before(s0 + m.start(), sc, 6))
        for cue in loss_cues:
            for m in re.finditer(re.escape(cue), sent):
                add(losers, _nearest_before(s0 + m.start(), sc, 12))
        # 4) 去向：「訂單去南亞」「轉向其他供應商，其中包括 TUC（台燿）」
        has_dest = any(c in sent for c in ("轉向", "轉由", "改由", "改用", "改採", "轉給", "交由", "訂單去", "訂單轉", "轉單"))
        has_loss = bool(losers) or any(c in sent for c in loss_cues) or bool(re.search(elim_alt, sent))
        for cue in dest_cues:
            for m in re.finditer(re.escape(cue), sent):
                if cue in ("包括", "其中包括", "新增供應商", "第二供應商", "二供") and not (has_dest or has_loss):
                    continue
                end = s0 + m.end()
                limit = 6 if cue.startswith("訂單") else 40
                for c in sc:
                    if end <= c[0] <= end + limit and c[2] not in losers:
                        add(winners, c)
        # 5) 拿下／通過認證：主詞在前
        for cue in win_cues:
            for m in re.finditer(re.escape(cue), sent):
                add(winners, _nearest_before(s0 + m.start(), sc, 10, blockers=sk))
        for m in re.finditer(r"通過.{0,6}(認證|驗證)", sent):
            add(winners, _nearest_before(s0 + m.start(), sc, 8))
        for pat in cfg.get("win_result_patterns") or []:
            for m in re.finditer(pat, sent):
                add(winners, _nearest_before(s0 + m.start(), sc, 14))
    both = [c for c in winners if c in losers]
    return [c for c in winners if c not in both], [c for c in losers if c not in both]


# ---------------------------------------------------------------- 偵測

def detect(item: Item, name_map: dict[str, str], cfg: dict | None = None, skip_patterns: list[str] | None = None) -> Signal | None:
    """item 是不是供應鏈訊號。使用者轉傳放寬：只要有公司與換料／轉單的說法就算（強度標弱）。"""
    cfg = cfg or settings()
    text = (item.text or "").strip()
    if len(text) < int(cfg.get("min_text_len", 8)):
        return None
    if any(re.search(p, text) for p in skip_patterns or []):
        return None
    customers = cfg.get("customers") or {}
    cust_words = {w for words in customers.values() for w in words}
    comps = find_companies(text, name_map, cfg.get("aliases") or {}, cust_words)
    if not comps:
        return None
    custs = find_customers(text, customers)
    products = [p for p in cfg.get("product_terms") or [] if re.search(r"(?<![A-Za-z0-9])" + re.escape(p) + r"(?![A-Za-z0-9])", text, re.I)]
    winners, losers = parse_roles(text, comps, custs, cfg)
    mentioned = list(dict.fromkeys(c[2] for c in comps))
    has_change = any(w in text for w in cfg.get("change_words") or [])
    rumor = any(w in text for w in (cfg.get("rumor_words") or []) + (cfg.get("denial_words") or []))
    denied = any(w in text for w in cfg.get("denial_words") or []) and rumor
    strong = bool((custs or products) and (winners or losers)) or bool(rumor and has_change and comps)
    relay = item.kind == "使用者轉傳"
    if not strong and not (relay and (has_change or winners or losers)):
        return None
    return Signal(customers=list(dict.fromkeys(k[2] for k in custs)), winners=winners, losers=losers, mentioned=mentioned,
                  products=products, strong=strong, denied=denied, text=text, source=item.source, url=item.url, kind=item.kind)


def title_candidate(title: str, name_map: dict[str, str], cfg: dict | None = None) -> int:
    """論壇標題值不值得點進去看內文：換料／轉單說法 2 分、有公司 +1、有客戶 +1；2 分以上才抓內文。"""
    cfg = cfg or settings()
    score = 2 if any(w in title for w in cfg.get("change_words") or []) else 0
    customers = cfg.get("customers") or {}
    cust_words = {w for words in customers.values() for w in words}
    if find_companies(title, name_map, cfg.get("aliases") or {}, cust_words):
        score += 1
    if find_customers(title, customers):
        score += 1
    sig = detect(Item("標題", title), name_map, cfg)
    if sig and sig.strong:
        score += 3  # 標題本身就是完整的訊號（台燿掉AWS，訂單去南亞）
    return score


# ---------------------------------------------------------------- 狀態

def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
    return {}


def save_state(st: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def _prune(st: dict, keep_days: int) -> None:
    cutoff = f"{now_tw() - timedelta(days=keep_days):%Y-%m-%d}"
    st["seen"] = {k: v for k, v in (st.get("seen") or {}).items() if v.get("date", "") >= cutoff}
    st["counts"] = {k: v for k, v in (st.get("counts") or {}).items() if k >= cutoff}


def find_conflict(sig: Signal, seen: dict) -> dict | None:
    """近期有沒有同一批公司、角色相反的訊號（先前說 A 受傷 B 受惠，現在說 B 受傷 A 受惠）。"""
    if sig.denied:
        return None  # 否認的是傳聞本身，不是方向相反的新說法
    for key, e in seen.items():
        if e.get("d"):
            continue
        if e.get("c") and sig.customers and not (set(e["c"]) & set(sig.customers)):
            continue
        if (set(sig.winners) & set(e.get("l", []))) or (set(sig.losers) & set(e.get("w", []))):
            return {"key": key, **e}
    return None


# ---------------------------------------------------------------- 推播與求證

def coverage(codes: list[str], listings: dict, limit: int = 4) -> list[str]:
    """這些公司已有的研究（題材名稱＋日期），讓訊號一出來就知道上下文。"""
    out = []
    index = research.load_index()
    for code in codes:
        name = (listings.get(code) or {}).get("name", code)
        for topic, e in index.items():
            if f"（{code}）" in topic or (topic.startswith(name) and "題材與產業" in topic):
                out.append(f"{topic}（{str(e.get('date', ''))[5:]}）")
                break
    return out[:limit]


def _name(code: str, listings: dict) -> str:
    return f"{(listings.get(code) or {}).get('name', code)}（{code}）"


def _names(codes: list[str], listings: dict) -> str:
    return "、".join(_name(c, listings) for c in codes) or "—"


def build_topic(sig: Signal, listings: dict) -> str:
    short = lambda codes: "、".join((listings.get(c) or {}).get("name", c) for c in codes[:2])  # noqa: E731
    cust = sig.customers[0] if sig.customers else ""
    if sig.denied and not (sig.winners or sig.losers):
        return f"供需事件：{short(sig.mentioned)}{cust}認證傳聞與澄清（傳聞求證）"
    if sig.losers and sig.winners:
        return f"供需事件：{short(sig.losers)}掉{cust}訂單 {short(sig.winners)}接單（傳聞求證）"
    if sig.losers:
        return f"供需事件：{short(sig.losers)}{cust}掉單或遭淘汰（傳聞求證）"
    if sig.winners:
        return f"供需事件：{short(sig.winners)}拿下{cust}訂單或認證（傳聞求證）"
    return f"供需事件：{short(sig.mentioned)}{cust}供應鏈變動（傳聞求證）"


VERIFY_HINT = (
    "【求證要點】這是未證實的早期訊號，不要直接當事實：(1) 搜尋公開報導、法說會、重大訊息、月營收、外資報告、客戶官方發布，"
    "找佐證或反證；(2) 查客戶端的平台與世代（例如 AWS Trainium 3／4、M8／M9）與各家認證進度；"
    "(3) 研究檔 status 填 傳聞／混合／已證實，layers 與 concepts 列出『哪一邊有什麼證據、哪裡互相矛盾』；"
    "(4) 若與既有研究的結論衝突（例如南亞 M9 認證完成、台燿 M9 出貨進度），在 risks 與 conclusion 明講。"
)


def message(sig: Signal, listings: dict, conflict: dict | None, topic: str | None, queued: bool) -> str:
    lines = [f"⚡ 早期訊號（未證實）｜{sig.label}｜來源：{sig.source}"]
    if sig.customers:
        lines.append(f"客戶：{'、'.join(sig.customers)}")
    if sig.denied:
        lines.append("這則是在說某個傳聞「已被否認／澄清」，不是新的消息；傳聞本身仍未證實，要看原文裡誰在否認、否認什麼。")
    if sig.winners or sig.losers:
        lines.append(("傳聞內容——" if sig.denied else "") + f"受傷：{_names(sig.losers, listings)}　受惠：{_names(sig.winners, listings)}")
    else:
        lines.append(f"提到：{_names(sig.mentioned[:6], listings)}（方向看不出來，請看原文）")
    lines.append("原文：" + re.sub(r"\s+", " ", sig.text)[:260])
    if sig.url:
        lines.append(sig.url)
    if conflict:
        lines.append(f"⚠️ 與 {conflict.get('date', '')[5:]} 的訊號方向相反（先前：{conflict.get('source', '')}說 "
                     f"受傷 {_names(conflict.get('l', []), listings)}、受惠 {_names(conflict.get('w', []), listings)}）。"
                     "兩邊都沒證實，先別當事實。")
    cov = coverage(sig.winners + sig.losers + sig.mentioned, listings)
    if cov:
        lines.append("既有研究：" + "｜".join(cov))
    if topic:
        lines.append(("→ 已排入求證研究：" if queued else "→ 求證研究（先前已排入）：") + topic)
    return "\n".join(lines)


def process(items: list[Item], listings: dict, name_map: dict[str, str], *, dry: bool = False,
            channel: str = "research", skip_patterns: list[str] | None = None) -> list[Signal]:
    """偵測、去重、推播、排入求證研究。回傳這次新推的訊號。dry=True 只印出不推、不存、不排。"""
    cfg = settings()
    if not cfg.get("enabled", True):
        return []
    st = load_state()
    _prune(st, int(cfg.get("keep_days", 14)))
    seen = st.setdefault("seen", {})
    counts = st.setdefault("counts", {})
    today = f"{now_tw():%Y-%m-%d}"
    day = counts.setdefault(today, {"push": 0, "queue": 0})
    fresh: list[Signal] = []
    done = st.setdefault("items", [])
    for it in items:
        relay = it.kind == "使用者轉傳"
        ik = hashlib.md5((it.url or it.text[:200]).encode()).hexdigest()[:12]
        if ik in done and not relay:
            continue  # 這段文字看過了
        done.append(ik)
        sig = detect(it, name_map, cfg, skip_patterns)
        if not sig:
            continue
        old = seen.get(sig.key)
        if old and not relay:
            continue  # 同一件事已經推過
        if day["push"] >= int(cfg.get("max_push_per_day", 12)) and not relay:
            log.info("早期訊號今天已推 %d 則，略過：%s", day["push"], sig.text[:40])
            continue
        conflict = find_conflict(sig, {k: v for k, v in seen.items() if k != sig.key})
        topic, queued = None, False
        if (sig.winners or sig.losers or relay) and (day["queue"] < int(cfg.get("max_queue_per_day", 6)) or relay):
            topic = build_topic(sig, listings)
            trigger = (f"{today} 早期訊號（未證實，來源：{sig.source}{'｜' + sig.url if sig.url else ''}）\n"
                       f"原文：{sig.text[:700]}\n"
                       f"解析：客戶 {'、'.join(sig.customers) or '—'}；受惠 {_names(sig.winners, listings)}；受傷 {_names(sig.losers, listings)}"
                       + (f"\n注意：與 {conflict['date']} 的訊號方向相反（{conflict.get('text', '')[:120]}）" if conflict else "")
                       + f"\n{VERIFY_HINT}")
            if conflict and conflict.get("topic") and (research.QUEUE_DIR / f"{research.slug(conflict['topic'])}.json").exists():
                topic = conflict["topic"]  # 同一件事的相反說法：併進原本的求證，不另開題材
                if not dry:
                    research._add_related_to_queue(topic, f"⚠️ 方向相反的新訊號（{sig.source}）：{sig.text[:100]}\n{sig.url}")
            elif not dry:
                queued = research.queue_research(topic, trigger, priority=3, cause="供應鏈轉單", dedupe=False)
                day["queue"] += 1 if queued else 0
        text = message(sig, listings, conflict, topic, queued or dry)
        if dry:
            print(text + "\n" + "-" * 40)
        else:
            notify.send(text, channel=channel)
            day["push"] += 1
            seen[sig.key] = {"date": today, "c": sig.customers, "w": sig.winners, "l": sig.losers, "d": sig.denied,
                             "source": sig.source, "text": sig.text[:300], "topic": topic or ""}
        fresh.append(sig)
    if not dry:
        st["items"] = done[-3000:]
        save_state(st)
    return fresh


# ---------------------------------------------------------------- 使用者轉傳

INTEL_RE = re.compile(r"^\s*/?(情報|消息|傳聞|爆料|intel)\s*[:：]?\s*(.+)$", re.I | re.S)


def parse_intel(text: str) -> str | None:
    """「情報 台燿掉AWS，訂單去南亞」→ 後面那段文字。"""
    m = INTEL_RE.match(text or "")
    return m.group(2).strip() if m else None


def looks_like_intel(text: str, name_map: dict[str, str], cfg: dict | None = None) -> bool:
    """沒有加「情報」兩個字的訊息，有公司名加上換料／轉單說法也當成情報（只用在主人自己的聊天室）。"""
    sig = detect(Item("使用者轉傳", text, kind="使用者轉傳"), name_map, cfg)
    return bool(sig and (sig.winners or sig.losers))


def handle_relay(text: str, listings: dict, name_map: dict[str, str], *, source: str = "使用者轉傳",
                 channel: str = "research", skip_patterns: list[str] | None = None) -> str:
    """使用者轉傳的消息：照一般流程偵測、推播、排求證。回傳給使用者的簡短回覆。"""
    sigs = process([Item(source, text, kind="使用者轉傳")], listings, name_map, channel=channel, skip_patterns=skip_patterns)
    if sigs:
        return ""  # process 已經推了完整訊息
    return "收到，但文字裡找不到上市櫃公司，沒有排求證。請附上公司名或代號。"
