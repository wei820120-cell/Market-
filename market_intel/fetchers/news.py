"""新聞與公告：公開資訊觀測站重大訊息、鉅亨網、Google 新聞（關鍵字，如漲價）、Yahoo、SEC 8-K。"""
from __future__ import annotations

import hashlib
import json
import logging
import re
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


def fetch_mops() -> list[NewsItem]:
    items: list[NewsItem] = []
    sources = [
        ("上市", "https://openapi.twse.com.tw/v1/opendata/t187ap04_L"),
        ("上櫃", "https://www.tpex.org.tw/openapi/v1/mopsfe_t187ap04_O"),
    ]
    for market, url in sources:
        try:
            items.extend(parse_mops(net.get_json(url), market))
        except Exception as e:  # noqa: BLE001
            log.warning("重大訊息（%s）抓取失敗：%s", market, e)
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


def fetch_google_news(query: str, lang: str = "zh-TW") -> list[NewsItem]:
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


def only_new(items: list[NewsItem], max_keep: int = 20000) -> list[NewsItem]:
    seen: list[str] = json.loads(SEEN_PATH.read_text()) if SEEN_PATH.exists() else []
    seen_set = set(seen)
    fresh = []
    for it in items:
        if it.key not in seen_set:
            seen_set.add(it.key)
            seen.append(it.key)
            fresh.append(it)
    SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    SEEN_PATH.write_text(json.dumps(seen[-max_keep:]))
    return fresh


def dedupe(items: list[NewsItem]) -> list[NewsItem]:
    out, keys = [], set()
    for it in items:
        k = re.sub(r"\s+", "", it.title)[:60]
        if k and k not in keys:
            keys.add(k)
            out.append(it)
    return out
