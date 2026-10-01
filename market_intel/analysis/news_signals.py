"""新聞訊號：依 config/news_keywords.yaml 的關鍵字（漲價、缺貨、擴產、下修…）幫新聞打分數並標出相關個股。"""
from __future__ import annotations

import re

from ..fetchers.news import NewsItem


def score_item(item: NewsItem, keywords: dict) -> NewsItem:
    text = f"{item.title} {item.summary}"
    lower = text.lower()
    score, tags = 0.0, []
    for cat, spec in (keywords.get("categories") or {}).items():
        weight = float(spec.get("weight", 1))
        hit = [w for w in spec.get("words", []) if str(w).lower() in lower]
        if hit:
            score += weight
            tags.append(f"{cat}({hit[0]})")
    item.score = score
    item.tags = tags
    return item


def tag_codes(item: NewsItem, name_to_code: dict[str, str], us_symbols: set[str] | None = None) -> NewsItem:
    """在標題中找出台股名稱/代號、美股代號。"""
    text = item.title
    codes = list(item.codes)
    for m in re.finditer(r"(?<!\d)(\d{4})(?!\d)", text):
        if m.group(1) in name_to_code.values():
            codes.append(m.group(1))
    for name, code in name_to_code.items():
        if len(name) >= 2 and name in text:
            codes.append(code)
    if us_symbols:
        for tok in re.findall(r"\b[A-Z]{1,5}\b", text):
            if tok in us_symbols:
                codes.append(tok)
    item.codes = list(dict.fromkeys(codes))
    return item


def rank(items: list[NewsItem], keywords: dict, name_to_code: dict[str, str], us_symbols: set[str] | None = None,
         min_score: float | None = None) -> list[NewsItem]:
    for it in items:
        score_item(it, keywords)
        tag_codes(it, name_to_code, us_symbols)
    threshold = keywords.get("min_score", 1) if min_score is None else min_score
    hits = [it for it in items if abs(it.score) >= threshold]
    return sorted(hits, key=lambda it: (abs(it.score), bool(it.codes)), reverse=True)
