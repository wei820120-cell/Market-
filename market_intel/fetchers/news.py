"""新聞與公告：公開資訊觀測站重大訊息、鉅亨網、Google 新聞（關鍵字，如漲價）、Yahoo、SEC 8-K。"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus

from .. import net
from ..utils import CACHE_DIR, TW_TZ, pick

log = logging.getLogger(__name__)


@dataclass
class NewsItem:
    source: str
    title: str
    url: str = ""
    published: str = ""
    summary: str = ""
    codes: list[str] = field(default_factory=list)
    score: float = 0.0
    tags: list[str] = field(default_factory=list)
    themes: list[str] = field(default_factory=list)  # 漲價新聞提到的產品所對應的受惠族群

    @property
    def key(self) -> str:
        return hashlib.md5(f"{self.source}|{self.url or self.title}".encode()).hexdigest()

    def to_dict(self) -> dict:
        return asdict(self)


# ---------- 公開資訊觀測站：重大訊息（最即時的公司公告） ----------

def parse_mops(rows: list[dict], market: str) -> list[NewsItem]:
    items = []
    for r in rows:
        code = str(pick(r, "公司代號", "SecuritiesCompanyCode", default="")).strip()
        name = str(pick(r, "公司名稱", "CompanyName", default="")).strip()
        subject = str(pick(r, "主旨", "Subject", default="")).strip()
        d = str(pick(r, "發言日期", default="")).strip()
        t = str(pick(r, "發言時間", default="")).strip()
        items.append(NewsItem(
            source=f"MOPS重大訊息({market})",
            title=f"{code} {name}：{subject}",
            url="https://mops.twse.com.tw/mops/web/t05sr01_1",
            published=f"{d} {t}".strip(),
            summary=str(pick(r, "說明", default="")).strip()[:500],
            codes=[code] if code else [],
        ))
    return items


TPEX_OPENAPI = "https://www.tpex.org.tw/openapi/v1"
TPEX_SWAGGER = "https://www.tpex.org.tw/openapi/swagger.json"
# 找不到 API 目錄時依序嘗試的候選路徑
TPEX_MATERIAL_CANDIDATES = ["/mopsfin_t187ap04_O", "/mopsfe_t187ap04_O", "/t187ap04_O"]
_tpex_material_path: str | None = None


def find_tpex_material_path(swagger: dict) -> str | None:
    """從櫃買中心 OpenAPI 目錄找出「上櫃公司每日重大訊息」的路徑（排除興櫃）。"""
    best = None
    for path, ops in (swagger.get("paths") or {}).items():
        op = (ops or {}).get("get") or {}
        text = f"{op.get('summary', '')} {op.get('description', '')} {' '.join(op.get('tags') or [])}"
        if "重大訊息" not in text or "興櫃" in text:
            continue
        if "上櫃" in text and "每日" in text:
            return path
        best = best or path
    return best


def _tpex_material_paths() -> list[str]:
    global _tpex_material_path
    if _tpex_material_path is None:
        try:
            _tpex_material_path = find_tpex_material_path(net.get_json(TPEX_SWAGGER)) or ""
            if _tpex_material_path:
                log.info("櫃買中心重大訊息介面：%s", _tpex_material_path)
        except Exception as e:  # noqa: BLE001
            log.debug("讀取櫃買中心 API 目錄失敗：%s", e)
            _tpex_material_path = ""
    found = [_tpex_material_path] if _tpex_material_path else []
    return found + [p for p in TPEX_MATERIAL_CANDIDATES if p not in found]


def fetch_mops() -> list[NewsItem]:
    items: list[NewsItem] = []
    try:
        items.extend(parse_mops(net.get_json("https://openapi.twse.com.tw/v1/opendata/t187ap04_L"), "上市"))
    except Exception as e:  # noqa: BLE001
        log.warning("重大訊息（上市）抓取失敗：%s", e)
    last_err = None
    for path in _tpex_material_paths():
        try:
            rows = net.get_json(f"{TPEX_OPENAPI}{path}", retries=1)
            if isinstance(rows, list):
                items.extend(parse_mops(rows, "上櫃"))
                log.info("重大訊息（上櫃）%s：%d 則", path, len(rows))
                break
        except Exception as e:  # noqa: BLE001
            last_err = e
    else:
        log.warning("重大訊息（上櫃）抓取失敗：%s", last_err)
    return items


# ---------- 鉅亨網 ----------

def parse_cnyes(payload: dict) -> list[NewsItem]:
    data = ((payload.get("items") or {}).get("data")) or payload.get("data") or []
    items = []
    for n in data:
        codes = []
        # 相關個股可能出現在 market（[{code: "2330"...}] 或 "2330-TW"）或 stock 欄位
        for m in (n.get("market") or []) + (n.get("stock") or []):
            c = str(m.get("code", "")) if isinstance(m, dict) else str(m)
            hit = re.search(r"(?<!\d)(\d{4,6})(?!\d)", c)
            if hit:
                codes.append(hit.group(1))
        published = n.get("publishAt", "")
        if isinstance(published, (int, float)) or str(published).isdigit():
            published = datetime.fromtimestamp(int(published), tz=TW_TZ).isoformat()
        items.append(NewsItem(
            source="鉅亨網",
            title=str(n.get("title", "")).strip(),
            url=f"https://news.cnyes.com/news/id/{n.get('newsId')}",
            published=str(published),
            summary=str(n.get("summary", "")).strip()[:300],
            codes=list(dict.fromkeys(codes)),
        ))
    return items


def fetch_cnyes(category: str = "tw_stock", limit: int = 30) -> list[NewsItem]:
    try:
        payload = net.get_json(f"https://api.cnyes.com/media/api/v1/newslist/category/{category}", params={"limit": limit})
        return parse_cnyes(payload)
    except Exception as e:  # noqa: BLE001
        log.warning("鉅亨網 %s 抓取失敗：%s", category, e)
        return []


# ---------- RSS（Google 新聞關鍵字、Yahoo 個股） ----------

def parse_rss(xml_text: str, source: str) -> list[NewsItem]:
    items = []
    root = ET.fromstring(xml_text)
    for it in root.iter("item"):
        pub = it.findtext("pubDate") or ""
        try:
            pub = parsedate_to_datetime(pub).isoformat()
        except (TypeError, ValueError):
            pass
        items.append(NewsItem(
            source=source,
            title=(it.findtext("title") or "").strip(),
            url=(it.findtext("link") or "").strip(),
            published=pub,
            summary=re.sub(r"<[^>]+>", "", it.findtext("description") or "").strip()[:300],
        ))
    return items


_google_last: dict[str, float] = {}
GOOGLE_MIN_INTERVAL = 180  # 同一個搜尋最快 3 分鐘查一次，避免被 Google 限流


def fetch_google_news(query: str, lang: str = "zh-TW") -> list[NewsItem]:
    now = time.monotonic()
    if now - _google_last.get(query, -1e9) < GOOGLE_MIN_INTERVAL:
        return []
    _google_last[query] = now
    if lang == "zh-TW":
        url = f"https://news.google.com/rss/search?q={quote_plus(query)}+when:1d&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    else:
        url = f"https://news.google.com/rss/search?q={quote_plus(query)}+when:1d&hl=en-US&gl=US&ceid=US:en"
    try:
        return parse_rss(net.get(url).text, f"Google新聞[{query}]")
    except Exception as e:  # noqa: BLE001
        log.warning("Google 新聞 %s 抓取失敗：%s", query, e)
        return []


def fetch_yahoo_rss(symbols: list[str]) -> list[NewsItem]:
    items = []
    for i in range(0, len(symbols), 10):
        chunk = ",".join(symbols[i:i + 10])
        try:
            text = net.get("https://feeds.finance.yahoo.com/rss/2.0/headline", params={"s": chunk, "region": "US", "lang": "en-US"}).text
            items.extend(parse_rss(text, "Yahoo Finance"))
        except Exception as e:  # noqa: BLE001
            log.warning("Yahoo RSS %s 抓取失敗：%s", chunk, e)
    return items


# ---------- SEC 8-K（美股重大事件申報） ----------

ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}


def parse_sec_atom(xml_text: str) -> list[NewsItem]:
    root = ET.fromstring(xml_text)
    items = []
    for e in root.findall("a:entry", ATOM_NS):
        link = e.find("a:link", ATOM_NS)
        items.append(NewsItem(
            source="SEC 8-K",
            title=(e.findtext("a:title", default="", namespaces=ATOM_NS)).strip(),
            url=link.get("href", "") if link is not None else "",
            published=e.findtext("a:updated", default="", namespaces=ATOM_NS),
            summary=re.sub(r"<[^>]+>", " ", e.findtext("a:summary", default="", namespaces=ATOM_NS)).strip()[:300],
        ))
    return items


def fetch_sec_8k(count: int = 100) -> list[NewsItem]:
    url = "https://www.sec.gov/cgi-bin/browse-edgar"
    params = {"action": "getcurrent", "type": "8-K", "owner": "include", "count": count, "output": "atom"}
    try:
        return parse_sec_atom(net.get(url, params=params).text)
    except Exception as e:  # noqa: BLE001
        log.warning("SEC 8-K 抓取失敗：%s", e)
        return []


# ---------- 去重：只回傳沒看過的 ----------

SEEN_PATH = CACHE_DIR / "seen_news.json"
SIMILAR = 0.6  # 標題字元雙字組相似度 >= 此值視為同一則新聞


def title_key(title: str) -> str:
    """標題正規化：去掉結尾「 - 媒體名稱」、「／ 分類」、括號與標點，用來判斷是不是同一則新聞。"""
    t = re.sub(r"\s+-\s+[^-]{1,30}$", "", title)       # Google：「標題 - 鉅亨網」
    t = re.sub(r"[／|｜]\s*[^／|｜]{1,12}$", "", t)       # 「標題／ 台股」「標題| 科技」
    t = re.sub(r"(-TW|-US)\b", "", t)
    return re.sub(r"[\s　，。！？、：:；;,.!?「」『』《》【】()（）\[\]*＊~～…\-—_/／|｜'\"“”‘’]", "", t).lower()


def _bigrams(s: str) -> set[str]:
    return {s[i:i + 2] for i in range(len(s) - 1)} or {s}


def _key(it: NewsItem) -> str:
    # 重大訊息每則都是不同公告，同公司標題開頭很像，只比完全相同、不做相似比對
    return ("mops:" if it.source.startswith("MOPS") else "") + title_key(it.title)


def similar(a: str, b: str, threshold: float = SIMILAR) -> bool:
    if not a or not b:
        return False
    if a.startswith("mops:") or b.startswith("mops:"):
        return a == b
    if a == b or (min(len(a), len(b)) >= 12 and (a in b or b in a)):
        return True
    ga, gb = _bigrams(a), _bigrams(b)
    return len(ga & gb) / len(ga | gb) >= threshold


def only_new(items: list[NewsItem], max_keep: int = 3000) -> list[NewsItem]:
    """只回傳還沒推過的新聞。用「標題」判斷，不管是鉅亨網、Google 哪個搜尋或哪家媒體轉載，同一則只推一次。"""
    seen: list[str] = []
    if SEEN_PATH.exists():
        try:
            seen = [k for k in json.loads(SEEN_PATH.read_text(encoding="utf-8")) if isinstance(k, str)]
        except (ValueError, OSError):
            seen = []
    seen_set = set(seen)
    recent = seen[-1500:]  # 近期標題做相似度比對
    fresh = []
    for it in items:
        k = _key(it)
        if not k or k in seen_set or any(similar(k, r) for r in recent):
            continue
        seen_set.add(k)
        seen.append(k)
        recent.append(k)
        fresh.append(it)
    SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    SEEN_PATH.write_text(json.dumps(seen[-max_keep:], ensure_ascii=False), encoding="utf-8")
    return fresh


def dedupe(items: list[NewsItem]) -> list[NewsItem]:
    """同一批新聞內去重：標題相同或高度相似只留第一則（MOPS、鉅亨網排在 Google 前面，優先保留）。"""
    out: list[NewsItem] = []
    keys: list[str] = []
    for it in items:
        k = _key(it)
        if k and not any(similar(k, x) for x in keys):
            keys.append(k)
            out.append(it)
    return out
