"""題材研究員：一出現新題材，就用 AI＋網路搜尋研究整條供應鏈。

流程
1. 找題材：股癌等頻道新貼文、當天重點新聞（AI 判斷哪些是「還沒研究過的新題材」），或你在 Telegram 傳「研究 XXX」
2. 研究：Claude 搜尋網路，寫出供應鏈報告（是什麼、為什麼現在、上中下游、台股受惠／受傷、時間軸、風險、來源）
3. 整理：再擷取成結構化資料；台股代號逐一和上市櫃清單比對，不存在的剔除，並標上一般／小型股票期貨
4. 輸出：報告存到 research/、推播摘要；受惠股自動加入族群監控（research/auto_themes.yaml）

研究結果是 AI 產出，可能有錯，僅供參考。
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta

import yaml

from . import ai, config
from .fetchers import stock_futures
from .utils import ROOT, now_tw

log = logging.getLogger(__name__)

RESEARCH_DIR = ROOT / "research"
INDEX_PATH = RESEARCH_DIR / "index.json"
AUTO_THEMES_PATH = RESEARCH_DIR / "auto_themes.yaml"
INBOX_DIR = ROOT / "state" / "inbox"
QUEUE_DIR = ROOT / "state" / "research_queue"  # 資金流入但還沒研究過的題材，等有 API 金鑰時研究
PENDING_DIR = RESEARCH_DIR / "pending"  # 已寫好、等待發布的研究（JSON：REPORT_SCHEMA 欄位＋report＋trigger）

SYSTEM = """你是台股產業研究員，專門把一個新題材拆解成完整的供應鏈地圖，給做波段的投資人參考。
原則：
- 一律使用繁體中文。
- 先用網路搜尋查證，優先引用公司公告、法說會、產業研究機構（TrendForce、DIGITIMES 等）與主要財經媒體。
- 台股公司一定要寫「代號 名稱」，只寫你確定是台灣上市櫃的公司；不確定的寫「（待確認）」，不要猜代號。
- 分清楚「已證實」（公司公告、法說會、出貨數據）和「傳聞」（媒體消息、券商推測、社群）。
- 同一家公司可能同時受惠與受傷（例如新技術取代舊材料），要講清楚。
- 不給買賣建議，只做產業與供應鏈分析。"""

REPORT_TEMPLATE = """請研究這個題材：「{topic}」

觸發來源（可能是新聞或社群貼文，僅供理解背景）：
{trigger}

{knowledge}

請寫一份研究報告（Markdown），包含：
# {topic}
## 一句話重點
## 這是什麼？為什麼現在？
（技術或產品是什麼、解決什麼問題、這次被討論的契機）
## 技術細項（要像產業懶人包一樣具體）
- 規格比較表：關鍵材料／產品的規格數字比較（例如 PCB 材料的 Dk、Df、Tg、成本；光模組的速率、功耗、雷射種類），附單位與數值範圍
- 世代演進：每一代的組成、關鍵規格、典型應用（例如 FR-4 → 改性 PPO → M8 → M9 → 無布 HC／Hybrid PTFE）
- 關鍵概念解說：2～4 個讀者一定要懂的技術問題（例如「為什麼要拿掉玻纖布：Glass Weave Effect」），每個寫 3～5 點，講原因、影響、代價
- 結論：怎麼判斷誰勝出（不是單看一個規格，而是性能、成本、良率、量產可行性的平衡）
## 細項產業與供應鏈地圖
（把題材拆成細項產業，由上游到下游排列。例如「被動元件」拆成：上游材料（陶瓷粉、電極漿料）→ MLCC → 晶片電阻 → 電感 → 鉭質／鋁質電容 → 通路。
每個細項產業寫：這段做什麼、現在的景氣與報價（漲價、缺貨、稼動率）、全球主要廠商、台股公司（代號 名稱：在這個細項的角色、受惠或受傷、理由）。
同一家公司橫跨多個細項時，放在營收占比最高或受惠最大的那一項。）
## 受惠與受傷總表
## 時間軸與催化劑
（何時量產、何時放量、接下來要看的事件）
## 風險與反方觀點
## 已證實 vs 傳聞
## 資料來源
（列出標題與網址）"""

REPORT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["topic", "one_line", "status", "horizon", "layers", "keywords", "catalysts", "risks",
                 "spec_tables", "evolution", "concepts", "conclusion", "sources"],
    "properties": {
        "topic": {"type": "string"},
        "one_line": {"type": "string"},
        "status": {"type": "string", "enum": ["已證實", "傳聞", "混合"]},
        "horizon": {"type": "string", "enum": ["短期（3個月內）", "中期（3-12個月）", "長期（1年以上）"]},
        "layers": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["layer", "description", "global_players", "tw_stocks"],
                "properties": {
                    "layer": {"type": "string"},
                    "description": {"type": "string"},
                    "global_players": {"type": "array", "items": {"type": "string"}},
                    "tw_stocks": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["code", "name", "role", "impact"],
                            "properties": {
                                "code": {"type": "string"},
                                "name": {"type": "string"},
                                "role": {"type": "string"},
                                "impact": {"type": "string", "enum": ["受惠", "受傷", "中性"]},
                            },
                        },
                    },
                },
            },
        },
        "keywords": {"type": "array", "items": {"type": "string"}},
        "catalysts": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
        "spec_tables": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["title", "columns", "rows", "note"],
                "properties": {
                    "title": {"type": "string"},
                    "columns": {"type": "array", "items": {"type": "string"}},
                    "rows": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}},
                    "note": {"type": "string"},
                },
            },
        },
        "evolution": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "composition", "spec", "application"],
                "properties": {"name": {"type": "string"}, "composition": {"type": "string"},
                               "spec": {"type": "string"}, "application": {"type": "string"}},
            },
        },
        "concepts": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["title", "points"],
                "properties": {"title": {"type": "string"}, "points": {"type": "array", "items": {"type": "string"}}},
            },
        },
        "conclusion": {"type": "array", "items": {"type": "string"}},
        "sources": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["title", "url"],
                "properties": {"title": {"type": "string"}, "url": {"type": "string"}},
            },
        },
    },
}

EXTRACT_INSTRUCTION = """把下面這份研究報告整理成 JSON。
- layers 依報告的細項產業分層，由上游到下游（layer 寫細項產業名稱，description 寫這段的景氣與報價）；
  tw_stocks 只放報告中明確寫出代號的台股，代號只要數字（例如 "2383"），同一檔只放一次。
- spec_tables：報告裡的規格比較表，原樣搬過來（columns 是欄名，rows 每列是字串陣列，數字帶單位）。
- evolution：世代演進，由舊到新；concepts：關鍵概念解說，每個 3～5 點；conclusion：結論 3～6 點。
- keywords 放 5-15 個之後在新聞中辨識這個題材用的關鍵字（中英文都可，例如 M9、石英布、Q布、Low-Dk）。
- 報告沒寫的欄位給空陣列，不要自己補。"""

TOPICS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["topics"],
    "properties": {
        "topics": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["topic", "reason", "importance"],
                "properties": {
                    "topic": {"type": "string"},
                    "reason": {"type": "string"},
                    "importance": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
                },
            },
        }
    },
}

TOPICS_INSTRUCTION = """以下是今天的新聞標題與產業社群貼文。請找出值得深入研究的「投資題材」：
有產業或供應鏈意義的技術、產品、材料、規格、政策或報價變化（例如「玻纖布 Q布缺貨」「1.6T 光模組」「CoWoP」「HBM4」），
不要列單一公司的一般新聞、大盤行情或人事消息。
已經研究過或已在追蹤的題材如下，相同或高度重疊的不要再列：
{known}
每個題材用簡短名稱（10 字內），附上為什麼值得研究，importance 1-5（5 = 可能影響多家台股、具波段機會）。最多列 5 個，沒有就回傳空陣列。"""


# ---------- 研究索引 ----------

def data_dir():
    """每份研究的結構化資料（供應鏈分層），題材卡畫圖用。"""
    return RESEARCH_DIR / "data"



def load_index() -> dict:
    if INDEX_PATH.exists():
        try:
            return json.loads(INDEX_PATH.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
    return {}


def save_index(index: dict) -> None:
    RESEARCH_DIR.mkdir(parents=True, exist_ok=True)
    INDEX_PATH.write_text(json.dumps(index, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def slug(topic: str) -> str:
    s = re.sub(r"[\\/:*?\"<>|\s]+", "-", topic.strip()).strip("-")
    return s[:40] or "topic"


def recently_researched(topic: str, index: dict, days: int = 14) -> bool:
    key = topic.strip().lower()
    cutoff = (now_tw() - timedelta(days=days)).strftime("%Y-%m-%d")
    for name, meta in index.items():
        if (name.lower() == key or key in name.lower() or name.lower() in key) and meta.get("date", "") >= cutoff:
            return True
    return False


def relevant_knowledge(topic: str, index: dict, max_chars: int = 12000) -> str:
    """把研究庫裡和這個題材相關的筆記附進提示，讓新研究建立在既有知識上。"""
    words = {w.lower() for w in re.findall(r"[A-Za-z0-9.]+|[一-鿿]{2,}", topic)}
    picked = []
    for name, meta in index.items():
        kws = {k.lower() for k in meta.get("keywords", [])} | {name.lower()}
        if words & kws or any(w in k for w in words for k in kws):
            path = RESEARCH_DIR / meta.get("file", "")
            if path.exists():
                picked.append(path.read_text(encoding="utf-8"))
    if not picked:
        return ""
    body = "\n\n---\n\n".join(picked)[:max_chars]
    return f"研究庫中相關的既有筆記（可參考、補充或修正）：\n<knowledge>\n{body}\n</knowledge>"


# ---------- 驗證台股代號、標股期 ----------

def validate_layers(data: dict, listings: dict, futures: dict) -> tuple[list[dict], list[str]]:
    """逐一核對代號是否為上市櫃公司；名稱以官方清單為準。回傳（整理後的分層, 被剔除的項目）。"""
    dropped: list[str] = []
    layers = []
    for layer in data.get("layers", []):
        stocks = []
        for s in layer.get("tw_stocks", []):
            code = re.sub(r"\D", "", str(s.get("code", "")))[:6]
            info = listings.get(code) if code else None
            if not info:
                dropped.append(f"{s.get('code')} {s.get('name')}")
                continue
            stocks.append({
                "code": code,
                "name": info.get("name") or s.get("name", ""),
                "ai_name": s.get("name", ""),
                "role": s.get("role", ""),
                "impact": s.get("impact", "中性"),
                "futures": stock_futures.label(futures.get(code)) if futures else "未知",
            })
        layers.append({**layer, "tw_stocks": stocks})
    return layers, dropped


# ---------- 輸出 ----------

def render_markdown(data: dict, layers: list[dict], dropped: list[str], report: str, trigger: str,
                    usage: ai.Usage) -> str:
    lines = [
        f"# 題材研究：{data.get('topic')}",
        "",
        f"> 🤖 AI 研究（{now_tw():%Y-%m-%d %H:%M}），僅供參考，台股代號已和上市櫃清單核對。",
        f"> 狀態：**{data.get('status')}**｜時間軸：**{data.get('horizon')}**",
        "",
        f"**一句話**：{data.get('one_line')}",
        "",
        "## 台股供應鏈總表",
        "",
        "| 環節 | 代號 | 名稱 | 角色 | 影響 | 股票期貨 |",
        "|---|---|---|---|---|---|",
    ]
    for layer in layers:
        for s in layer["tw_stocks"]:
            lines.append(f"| {layer['layer']} | {s['code']} | {s['name']} | {s['role']} | {s['impact']} | {s['futures']} |")
    if dropped:
        lines += ["", f"_以下 AI 列出的代號不在上市櫃清單，已剔除：{'、'.join(dropped)}_"]
    if trigger:
        lines += ["", "<details><summary>觸發來源</summary>", "", trigger[:2000], "", "</details>"]
    lines += ["", "---", "", report]
    if usage.calls:
        lines += ["", "---", f"_用量：輸入 {usage.input_tokens:,} tokens、輸出 {usage.output_tokens:,} tokens、"
                             f"網路搜尋 {usage.web_searches} 次，約 US${usage.usd():.2f}_"]
    return "\n".join(lines) + "\n"


def summary_message(data: dict, layers: list[dict], url: str) -> str:
    lines = [f"🔬 題材研究：{data.get('topic')}", f"{data.get('status')}｜{data.get('horizon')}", "",
             data.get("one_line", ""), ""]
    for layer in layers:
        gain = [s for s in layer["tw_stocks"] if s["impact"] == "受惠"]
        hurt = [s for s in layer["tw_stocks"] if s["impact"] == "受傷"]
        if not gain and not hurt:
            continue
        lines.append(f"【{layer['layer']}】")
        for s in gain[:6]:
            fut = "" if s["futures"] in ("無", "未知") else f"｜股期 {s['futures']}"
            lines.append(f"  ▲ {s['code']} {s['name']}：{s['role'][:40]}{fut}")
        for s in hurt[:3]:
            fut = "" if s["futures"] in ("無", "未知") else f"｜股期 {s['futures']}"
            lines.append(f"  ▼ {s['code']} {s['name']}：{s['role'][:40]}{fut}")
    if data.get("catalysts"):
        lines += ["", "📅 催化劑：" + "；".join(data["catalysts"][:3])]
    if data.get("risks"):
        lines.append("⚠️ 風險：" + "；".join(data["risks"][:3]))
    lines += ["", "🤖 AI 研究，僅供參考", url]
    return "\n".join(lines)


def report_url(fname: str) -> str:
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        return str(RESEARCH_DIR / fname)
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    return f"{server}/{repo}/blob/main/research/{fname}"


def update_auto_themes(topic: str, layers: list[dict], keywords: list[str]) -> None:
    """受惠股加入族群監控，關鍵字加入頻道／新聞的族群對應。"""
    data = {}
    if AUTO_THEMES_PATH.exists():
        data = yaml.safe_load(AUTO_THEMES_PATH.read_text(encoding="utf-8")) or {}
    data.setdefault("tw", {})
    data.setdefault("topic_themes", {})
    name = f"研究:{topic}"[:20]
    codes = list(dict.fromkeys(s["code"] for layer in layers for s in layer["tw_stocks"] if s["impact"] == "受惠"))
    if codes:
        data["tw"][name] = codes
        for kw in keywords[:15]:
            if len(kw) >= 2:
                data["topic_themes"].setdefault(kw, [])
                if name not in data["topic_themes"][kw]:
                    data["topic_themes"][kw].append(name)
    RESEARCH_DIR.mkdir(parents=True, exist_ok=True)
    AUTO_THEMES_PATH.write_text(
        "# 題材研究員自動產生：研究過的題材的受惠股（族群監控）與辨識關鍵字。可手動修改或刪除。\n"
        + yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


# ---------- 主流程 ----------

def _save_report(topic: str, data: dict, report: str, trigger: str, listings: dict, futures: dict,
                 usage: ai.Usage, auto: bool) -> dict:
    """核對代號、存報告、更新索引與族群監控，回傳 {data, layers, file, message}。"""
    data["topic"] = data.get("topic") or topic
    layers, dropped = validate_layers(data, listings, futures)
    fname = f"{now_tw():%Y-%m-%d}-{slug(topic)}.md"
    RESEARCH_DIR.mkdir(parents=True, exist_ok=True)
    (RESEARCH_DIR / fname).write_text(render_markdown(data, layers, dropped, report, trigger, usage), encoding="utf-8")
    index = load_index()
    index[topic] = {"file": fname, "date": f"{now_tw():%Y-%m-%d}", "keywords": data.get("keywords", []),
                    "codes": [s["code"] for layer in layers for s in layer["tw_stocks"]],
                    "status": data.get("status"), "usd": round(usage.usd(), 3), "auto": auto}
    save_index(index)
    update_auto_themes(topic, layers, data.get("keywords", []))
    (QUEUE_DIR / f"{slug(topic)}.json").unlink(missing_ok=True)  # 研究完成就移出佇列
    data_dir().mkdir(parents=True, exist_ok=True)
    keep = ("topic", "one_line", "status", "horizon", "keywords", "catalysts", "risks", "spec_tables", "evolution",
            "concepts", "conclusion", "sources")
    (data_dir() / f"{slug(topic)}.json").write_text(json.dumps(
        {**{k: data.get(k) for k in keep}, "layers": [
            {"layer": l.get("layer"), "description": l.get("description", ""),
             "global_players": l.get("global_players", []),
             "tw_stocks": [{k: s[k] for k in ("code", "name", "role", "impact")} for s in l["tw_stocks"]]}
            for l in layers]}, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"data": data, "layers": layers, "file": fname,
            "message": summary_message(data, layers, report_url(fname))}


def research_topic(topic: str, trigger: str, listings: dict, futures: dict, auto: bool = False) -> dict:
    """研究一個題材，存檔並回傳 {data, layers, file, message}。失敗時丟出 ai.AIUnavailable。"""
    ai.check_budget()
    usage = ai.Usage()
    prompt = REPORT_TEMPLATE.format(topic=topic, trigger=trigger[:3000] or "（手動指定）",
                                    knowledge=relevant_knowledge(topic, load_index()))
    log.info("開始研究題材：%s", topic)
    try:
        report = ai.web_research(SYSTEM, prompt, usage)
        data = ai.extract_json(EXTRACT_INSTRUCTION, report, REPORT_SCHEMA, usage)
    finally:
        if usage.calls:
            ai.record_usage(usage, topic)
    out = _save_report(topic, data, report, trigger, listings, futures, usage, auto)
    log.info("研究完成：%s（約 US$%.2f）", out["file"], usage.usd())
    return out


def publish_pending(listings: dict, futures: dict) -> list[dict]:
    """發布 research/pending/ 裡已寫好的研究（不需要 API 金鑰），發布後刪除該檔。"""
    outs = []
    for p in sorted(PENDING_DIR.glob("*.json")) if PENDING_DIR.exists() else []:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            report, trigger = d.pop("report", ""), d.pop("trigger", "")
            outs.append(_save_report(d["topic"], d, report, trigger, listings, futures, ai.Usage(), False))
        except (ValueError, KeyError) as e:
            log.warning("待發布研究 %s 格式錯誤：%s", p.name, e)
            continue
        p.unlink()
    return outs


def detect_topics(texts: list[str]) -> list[dict]:
    """從新聞標題／貼文找出還沒研究過的新題材（依重要性排序）。"""
    if not texts:
        return []
    ai.check_budget()
    index = load_index()
    known = sorted(set(index) | set((config.themes().get("tw") or {}).keys()))
    usage = ai.Usage()
    try:
        out = ai.extract_json(TOPICS_INSTRUCTION.format(known="、".join(known) or "（無）"),
                              "\n".join(f"- {t[:300]}" for t in texts[:120]), TOPICS_SCHEMA, usage, "detect")
    finally:
        if usage.calls:
            ai.record_usage(usage, "題材偵測")
    topics = [t for t in out.get("topics", []) if not recently_researched(t["topic"], index)]
    return sorted(topics, key=lambda t: t["importance"], reverse=True)


def researched_today() -> int:
    today = f"{now_tw():%Y-%m-%d}"
    return sum(1 for meta in load_index().values() if meta.get("date") == today and meta.get("auto"))


# ---------- 頻道貼文收件匣（channels 寫入、research 處理） ----------

def add_to_inbox(channel: str, post_id: int, text: str, url: str) -> None:
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    (INBOX_DIR / f"{channel}-{post_id}.json").write_text(
        json.dumps({"channel": channel, "text": text, "url": url}, ensure_ascii=False), encoding="utf-8")


def read_inbox() -> list[tuple[os.PathLike, dict]]:
    if not INBOX_DIR.exists():
        return []
    out = []
    for p in sorted(INBOX_DIR.glob("*.json")):
        try:
            out.append((p, json.loads(p.read_text(encoding="utf-8"))))
        except (ValueError, OSError):
            p.unlink(missing_ok=True)
    return out


# ---------- Telegram 指令：「研究 玻纖布」 ----------

COMMAND_STATE = ROOT / "state" / "telegram_commands.json"
COMMAND_RE = re.compile(r"^\s*/?(研究|research)\s*[:：]?\s*(.+)$", re.I | re.S)


def parse_command(text: str) -> str | None:
    m = COMMAND_RE.match(text or "")
    if not m:
        return None
    topic = m.group(2).strip().splitlines()[0].strip()
    return topic[:40] or None


CARD_RE = re.compile(r"^\s*/?(題材|族群|卡片?|目標價|card)\s*[:：]?\s*(.+)$", re.I)


def parse_request(text: str) -> tuple[str, str] | None:
    """「研究 XXX」→ ("research", XXX)；「題材 XXX」「族群 XXX」「目標價 XXX」→ ("card", XXX)。"""
    t = parse_command(text)
    if t:
        return "research", t
    m = CARD_RE.match(text or "")
    if m:
        q = m.group(2).strip().splitlines()[0].strip()[:40]
        return ("card", q) if q else None
    return None


def pending_requests(updates: list[dict], owner_chat: str | None,
                     offset: int | None) -> tuple[list[tuple[str, str]], int | None]:
    """從 getUpdates 取出主人傳的指令，回傳（[(種類, 內容)], 下一次的 offset）。"""
    out = []
    for u in updates:
        offset = max(offset or 0, int(u.get("update_id", 0)) + 1)
        msg = u.get("message") or {}
        if owner_chat and str((msg.get("chat") or {}).get("id")) != str(owner_chat):
            continue
        req = parse_request(msg.get("text", ""))
        if req:
            out.append(req)
    return out, offset


def find_for_codes(codes: list[str], min_overlap: int = 2) -> str | None:
    """族群成分股和哪一份研究重疊最多（至少 2 檔、且過半），回傳研究題材名稱。"""
    best, best_n = None, 0
    want = set(codes)
    for topic, e in load_index().items():
        n = len(want & set(e.get("codes", [])))
        if n >= min_overlap and n * 2 >= len(want) and n > best_n:
            best, best_n = topic, n
    return best


def pending_commands(updates: list[dict], owner_chat: str | None, offset: int | None) -> tuple[list[str], int | None]:
    """從 getUpdates 結果取出主人傳的「研究 XXX」指令，回傳（題材清單, 下一次的 offset）。"""
    topics = []
    for u in updates:
        offset = max(offset or 0, int(u.get("update_id", 0)) + 1)
        msg = u.get("message") or {}
        if owner_chat and str((msg.get("chat") or {}).get("id")) != str(owner_chat):
            continue  # 只接受自己的指令
        topic = parse_command(msg.get("text", ""))
        if topic:
            topics.append(topic)
    return topics, offset


def _bot_key(token: str | None) -> str:
    # 每個機器人的訊息編號各自獨立，記錄要分開；只用 token 冒號前的機器人編號，不存密鑰
    return (token or "none").split(":")[0]


def load_offset(token: str | None) -> int | None:
    if COMMAND_STATE.exists():
        try:
            return json.loads(COMMAND_STATE.read_text(encoding="utf-8")).get(_bot_key(token))
        except (ValueError, OSError):
            pass
    return None


def save_offset(token: str | None, offset: int | None) -> None:
    if offset is None:
        return
    data = {}
    if COMMAND_STATE.exists():
        try:
            data = json.loads(COMMAND_STATE.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            data = {}
    data[_bot_key(token)] = offset
    COMMAND_STATE.parent.mkdir(parents=True, exist_ok=True)
    COMMAND_STATE.write_text(json.dumps(data) + "\n", encoding="utf-8")


# ---------- 研究佇列（資金流入的題材） ----------

def queue_research(topic: str, trigger: str) -> bool:
    """把題材排入研究佇列；14 天內研究過或已在佇列就略過。回傳是否新排入。"""
    if recently_researched(topic, load_index()):
        return False
    p = QUEUE_DIR / f"{slug(topic)}.json"
    if p.exists():
        return False
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"topic": topic, "trigger": trigger, "time": f"{now_tw():%Y-%m-%d %H:%M}"},
                            ensure_ascii=False), encoding="utf-8")
    return True


def read_queue() -> list[tuple[os.PathLike, dict]]:
    """待研究的題材；14 天內已研究過的直接移出。"""
    out = []
    index = load_index()
    for p in sorted(QUEUE_DIR.glob("*.json")) if QUEUE_DIR.exists() else []:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            p.unlink(missing_ok=True)
            continue
        if recently_researched(d.get("topic", ""), index):
            p.unlink(missing_ok=True)
            continue
        out.append((p, d))
    return out


def topic_for_theme(theme: str, codes: list[str]) -> str | None:
    """族群對應的研究題材：同名、或成分股和某份研究重疊過半。"""
    index = load_index()
    if theme in index:
        return theme
    if theme.startswith("研究:"):
        hit = next((t for t in index if f"研究:{t}"[:20] == theme), None)
        if hit:
            return hit
    return find_for_codes(codes)


def research_age_days(topic: str) -> int:
    d = (load_index().get(topic) or {}).get("date")
    try:
        return (now_tw().date() - datetime.strptime(d, "%Y-%m-%d").date()).days
    except (TypeError, ValueError):
        return 999


def status_of(theme: str, codes: list[str]) -> str:
    """族群的研究狀態：已研究（日期）／排隊中／未研究。"""
    index = load_index()
    topic = theme if theme in index else find_for_codes(codes)
    if topic:
        return f"已研究 {index[topic].get('date', '')}"
    if (QUEUE_DIR / f"{slug(theme)}.json").exists():
        return "排隊研究中"
    return "未研究"
