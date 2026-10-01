"""新聞訊號：依 config/news_keywords.yaml 的關鍵字（漲價、缺貨、擴產、下修…）幫新聞打分數並標出相關個股。"""
from __future__ import annotations

import re

from ..fetchers.news import NewsItem


def score_item(item: NewsItem, keywords: dict) -> NewsItem:
    text = f"{item.title} {item.summary}"
    lower = text.lower()
    score, tags = 0.0, []
    # 排除字（凍漲、民生物價等跟個股無關的新聞）
    if any(str(w).lower() in item.title.lower() for w in keywords.get("exclude_words", [])):
        item.score, item.tags = 0.0, []
        return item
    for cat, spec in (keywords.get("categories") or {}).items():
        weight = float(spec.get("weight", 1))
        hit = [w for w in spec.get("words", []) if str(w).lower() in lower]
        if hit:
            score += weight
            tags.append(f"{cat}({hit[0]})")
    spec = keywords.get("price_letter") or {}
    letter = detect_price_letter(item, spec)
    if letter or any(t.startswith("漲價") for t in tags):
        item.themes = hike_themes(text, spec)
    # 漲價信要對得到個股或產業族群才算（排除手機、天然氣這類民生漲價）
    if letter and (item.codes or item.themes or item.source.startswith("MOPS")):
        tag, weight = letter
        score += weight
        tags.insert(0, tag)
    item.score = score
    item.tags = tags
    return item


def detect_price_letter(item: NewsItem, spec: dict) -> tuple[str, float] | None:
    """偵測漲價信：公開資訊觀測站正式公告優先，其次是媒體報導公司發漲價信。"""
    if not spec:
        return None
    text = f"{item.title} {item.summary}"
    if item.source.startswith("MOPS"):
        for pat in spec.get("official_patterns", []):
            m = re.search(pat, text)
            if m and not re.search(r"(不|未|無|暫不|沒有)(調整|調漲|調升)", text):
                return f"公司公告漲價({m.group(0)[:12]})", float(spec.get("official_weight", 6))
    for pat in spec.get("patterns", []):
        m = re.search(pat, text, flags=re.I)
        if m:
            return f"漲價信({m.group(0)[:12]})", float(spec.get("weight", 5))
    return None


def hike_themes(text: str, spec: dict) -> list[str]:
    """漲價新聞提到哪些產品 → 對應的受惠族群。"""
    out = []
    lower = text.lower()
    for kw, theme in (spec.get("product_themes") or {}).items():
        if str(kw).lower() in lower and theme not in out:
            out.append(theme)
    return out


def is_price_letter(item: NewsItem) -> bool:
    return any(t.startswith(("漲價信", "公司公告漲價")) for t in item.tags)


def tag_codes(item: NewsItem, name_to_code: dict[str, str], us_symbols: set[str] | None = None,
              all_codes: set[str] | None = None) -> NewsItem:
    """在標題中找出台股名稱/代號、美股代號。"""
    text = item.title
    codes = list(item.codes)
    valid = set(all_codes or ()) | set(name_to_code.values())
    # 只認「(2330)」「2330-TW」「2330 台積電」這類寫法，避免把年份 2027 當成代號
    for m in re.finditer(r"[(（](\d{4,6})[)）\-]|(?<!\d)(\d{4,6})-TW|(?<![\d.])(\d{4,6})\s+(\S{2,})", text):
        code = m.group(1) or m.group(2) or m.group(3)
        if code not in valid:
            continue
        if m.group(3) and not any(n in text for n, c in name_to_code.items() if c == code):
            continue  # 「2027 財年」這種數字後面不是公司名稱，不算
        codes.append(code)
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
         min_score: float | None = None, all_codes: set[str] | None = None) -> list[NewsItem]:
    for it in items:
        tag_codes(it, name_to_code, us_symbols, all_codes)  # 先標個股，漲價信判斷需要
        score_item(it, keywords)
    threshold = keywords.get("min_score", 1) if min_score is None else min_score
    hits = [it for it in items if abs(it.score) >= threshold]
    return sorted(hits, key=lambda it: (abs(it.score), bool(it.codes)), reverse=True)
