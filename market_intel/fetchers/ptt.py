"""PTT 看板（預設股版 Stock）：公開網頁版 https://www.ptt.cc/bbs/<看板>/，需要帶 over18 cookie。

用途：供應鏈傳聞、法人圈消息常先出現在論壇（[情報]、[標的]、[新聞] 文章與推文），比媒體早；
這裡只讀標題與內文做早期訊號偵測（signals.py），不轉貼。
"""
from __future__ import annotations

import html
import json
import logging
import re
from dataclasses import dataclass

from .. import net
from ..utils import ROOT

log = logging.getLogger(__name__)

BASE = "https://www.ptt.cc"
COOKIES = {"over18": "1"}
STATE_PATH = ROOT / "state" / "ptt_seen.json"


@dataclass
class PttPost:
    board: str
    post_id: str      # 例如 M.1761120000.A.1B2
    title: str
    url: str
    nrec: str = ""    # 推文數（爆、數字、X 開頭）
    date: str = ""


def _clean(fragment: str) -> str:
    t = re.sub(r"<br\s*/?>", "\n", fragment, flags=re.I)
    t = re.sub(r"<[^>]+>", "", t)
    return html.unescape(t).strip()


def parse_index(text: str, board: str) -> tuple[list[PttPost], str | None]:
    """看板列表頁 → （文章們, 上一頁網址）。已刪除的文章（沒有連結）略過。"""
    posts = []
    for block in re.split(r'(?=<div class="r-ent">)', text):
        m = re.search(r'<div class="title">\s*<a href="(/bbs/[^"]+/(M\.[^"/]+)\.html)">(.*?)</a>', block, flags=re.S)
        if not m:
            continue
        nrec = re.search(r'<div class="nrec">(.*?)</div>', block, flags=re.S)
        date = re.search(r'<div class="date">\s*([^<]+?)\s*</div>', block)
        posts.append(PttPost(board=board, post_id=m.group(2), title=_clean(m.group(3)), url=BASE + m.group(1),
                             nrec=_clean(nrec.group(1)) if nrec else "", date=date.group(1) if date else ""))
    prev = re.search(r'<a class="btn wide" href="(/bbs/[^"]+/index\d+\.html)">[^<]*上頁', text)
    return posts, (BASE + prev.group(1)) if prev else None


def parse_article(text: str, limit: int = 3000) -> str:
    """文章頁 → 內文（去掉作者／標題／時間列與簽名檔、發信站；不含推文）。"""
    m = re.search(r'<div id="main-content"[^>]*>(.*)', text, flags=re.S)
    body = re.split(r'<div class="push">', m.group(1))[0] if m else ""
    body = re.sub(r'<div class="article-metaline[^"]*">.*?</div>\s*', "", body, flags=re.S)
    body = re.sub(r'<span class="f2">.*', "", body, flags=re.S)  # 發信站、文章網址、編輯行
    out = _clean(body)
    out = re.split(r"\n--\s*(?:\n|$)", out)[0]
    return out[:limit].strip()


def fetch_index(board: str = "Stock", pages: int = 2) -> list[PttPost]:
    """最新 pages 頁的文章（新的在後面）。"""
    url = f"{BASE}/bbs/{board}/index.html"
    out: list[PttPost] = []
    for _ in range(max(1, pages)):
        posts, prev = parse_index(net.get(url, cookies=COOKIES, timeout=20).text, board)
        out = posts + out
        if not prev:
            break
        url = prev
    return out


def fetch_article(url: str) -> str:
    return parse_article(net.get(url, cookies=COOKIES, timeout=20).text)


def load_seen() -> dict[str, list[str]]:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
    return {}


def save_seen(seen: dict[str, list[str]], keep: int = 600) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps({k: v[-keep:] for k, v in seen.items()}, ensure_ascii=False) + "\n", encoding="utf-8")


def new_posts(board: str, posts: list[PttPost], seen: dict[str, list[str]]) -> list[PttPost]:
    """還沒看過的文章，並更新 seen。第一次看這個看板只記錄、不處理（避免一次推一堆舊文）。"""
    ids = seen.get(board)
    seen[board] = (ids or []) + [p.post_id for p in posts if p.post_id not in (ids or [])]
    if ids is None:
        log.info("PTT %s 第一次讀取，記錄 %d 篇，之後只看新文章", board, len(posts))
        return []
    return [p for p in posts if p.post_id not in ids]
