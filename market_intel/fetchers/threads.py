"""Threads 個人帳號（公開貼文）。

網頁版只回一個空殼，貼文要靠 JavaScript 才會載入，所以用無頭瀏覽器（Playwright）讀；
沒登入時每個分頁只看得到最新的幾則（約 5～7 則），之後是登入牆，拿來追「新貼文」夠用。
Playwright 不在 requirements.txt（每次安裝太慢），只有要用到的 workflow 才安裝；沒裝就略過、不影響其他監看。

每個帳號讀兩個分頁：串文（本人發的）和回覆（本人回覆的，連同被回覆的原貼文一起出現——
例如有人貼傳聞、被追蹤的人回「這是真的」，兩則都會讀到）。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .. import net
from ..utils import ROOT

log = logging.getLogger(__name__)

BASE = "https://www.threads.com"
STATE_PATH = ROOT / "state" / "threads_seen.json"

# 在頁面裡跑：找出每個貼文連結，往上找到包住貼文文字與時間的區塊
_JS = """
() => {
  const posts = [];
  const seen = new Set();
  for (const a of document.querySelectorAll('a[href*="/post/"]')) {
    const href = a.getAttribute('href');
    if (seen.has(href)) continue;
    seen.add(href);
    let el = a;
    for (let i = 0; i < 8 && el.parentElement; i++) {
      el = el.parentElement;
      if (el.querySelector('time') && el.innerText.length > 40) break;
    }
    const t = el.querySelector('time');
    posts.push({href, text: el.innerText.slice(0, 1500), time: t ? t.getAttribute('datetime') : null});
  }
  return {title: document.title, url: location.href, text: document.body.innerText.slice(0, 2500), posts};
}
"""

_HREF_RE = re.compile(r"/@([^/]+)/post/([^/?#]+)")
_REL_RE = re.compile(r"^(\d+\s*(秒|分鐘|分|小時|天|週|周|個月)|\d{4}-\d{1,2}-\d{1,2})$")
_NUM_RE = re.compile(r"^[\d,\.]+(\s*[萬千])?$")


@dataclass
class ThreadsPost:
    author: str
    code: str
    url: str
    text: str
    posted_at: str = ""    # ISO（UTC）
    reply_to: str = ""     # 回覆誰（沒有就空白）


def parse_post(raw: dict) -> ThreadsPost | None:
    """頁面抓到的一個貼文區塊 → ThreadsPost。文字格式：作者／相對時間或日期／（正在回覆@某人）／內文／翻譯／讚、回覆、轉發、分享數。"""
    m = _HREF_RE.search(raw.get("href") or "")
    if not m:
        return None
    author, code = m.group(1), m.group(2)
    lines = [ln.replace("\xa0", " ").strip() for ln in (raw.get("text") or "").split("\n")]
    lines = [ln for ln in lines if ln]
    if lines and lines[0] == author:
        lines = lines[1:]
    if lines and _REL_RE.match(lines[0]):
        lines = lines[1:]
    reply_to = ""
    if lines and lines[0].startswith("正在回覆"):
        reply_to = lines[0].replace("正在回覆", "").strip().lstrip("@")
        lines = lines[1:]
    if "翻譯" in lines:
        lines = lines[:lines.index("翻譯")]
    else:
        while lines and _NUM_RE.match(lines[-1]):
            lines.pop()
    body = "\n".join(lines).strip()
    if not body:
        return None
    return ThreadsPost(author=author, code=code, url=f"{BASE}/@{author}/post/{code}", text=body,
                       posted_at=raw.get("time") or "", reply_to=reply_to)


def parse_page(data: dict) -> list[ThreadsPost]:
    """一個分頁 → 貼文（同一則的 /media 連結只留一個）。"""
    out: dict[tuple[str, str], ThreadsPost] = {}
    for raw in data.get("posts") or []:
        p = parse_post(raw)
        if p and (p.author, p.code) not in out:
            out[(p.author, p.code)] = p
    return list(out.values())


def render_pages(urls: list[str], wait_ms: int = 6000, scrolls: int = 1) -> dict[str, dict | None]:
    """用同一個無頭瀏覽器依序打開網址；讀不到的網址對應 None。"""
    from playwright.sync_api import sync_playwright

    out: dict[str, dict | None] = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        try:
            ctx = browser.new_context(locale="zh-TW", user_agent=net.BROWSER_UA, viewport={"width": 1280, "height": 2000})
            page = ctx.new_page()
            for u in urls:
                try:
                    page.goto(u, wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(wait_ms)
                    for _ in range(scrolls):
                        page.mouse.wheel(0, 2500)
                        page.wait_for_timeout(1500)
                    out[u] = page.evaluate(_JS)
                except Exception as e:  # noqa: BLE001 - 單一網址失敗不影響其他帳號
                    log.warning("Threads %s 讀取失敗：%s", u, e)
                    out[u] = None
        finally:
            browser.close()
    return out


def render(url: str, wait_ms: int = 6000, scrolls: int = 2) -> dict:
    """單一網址（測試用）：回傳 {title, url, text, posts: [{href, text, time}]}。"""
    return render_pages([url], wait_ms, scrolls)[url] or {"title": "", "url": url, "text": "", "posts": []}


def fetch_accounts(handles: list[str]) -> dict[str, list[ThreadsPost]]:
    """每個帳號的串文＋回覆分頁 → 貼文（舊的在前）。整個帳號都讀不到時不放進結果。"""
    handles = [h.lstrip("@") for h in handles]
    urls = [f"{BASE}/@{h}{tab}" for h in handles for tab in ("", "/replies")]
    pages = render_pages(urls)
    out: dict[str, list[ThreadsPost]] = {}
    for h in handles:
        got: dict[tuple[str, str], ThreadsPost] = {}
        ok = False
        for tab in ("", "/replies"):
            data = pages.get(f"{BASE}/@{h}{tab}")
            if data is None:
                continue
            ok = True
            for p in parse_page(data):
                got.setdefault((p.author, p.code), p)
        if ok:
            out[h] = sorted(got.values(), key=lambda p: p.posted_at)
        else:
            log.warning("Threads @%s 兩個分頁都讀不到", h)
    return out


def load_seen() -> dict[str, list[str]]:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
    return {}


def save_seen(seen: dict[str, list[str]], keep: int = 400) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps({k: v[-keep:] for k, v in seen.items()}, ensure_ascii=False) + "\n", encoding="utf-8")


def new_posts(handle: str, posts: list[ThreadsPost], seen: dict[str, list[str]], max_age_hours: int = 72,
              now: datetime | None = None) -> list[ThreadsPost]:
    """還沒看過、而且不太舊的貼文，並更新 seen。第一次看這個帳號只記錄、不處理（避免一次推一堆舊貼文）。"""
    ids = seen.get(handle)
    known = ids or []
    seen[handle] = known + [p.code for p in posts if p.code not in known]
    if ids is None:
        log.info("Threads @%s 第一次讀取，記錄 %d 則，之後只看新貼文", handle, len(posts))
        return []
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(hours=max_age_hours)

    def recent(p: ThreadsPost) -> bool:
        try:
            return datetime.fromisoformat(p.posted_at.replace("Z", "+00:00")) >= cutoff
        except ValueError:
            return True  # 沒有時間就當新的
    return [p for p in posts if p.code not in known and recent(p)]
