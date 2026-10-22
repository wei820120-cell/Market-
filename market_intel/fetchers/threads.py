"""Threads 個人頁（公開貼文）。

網頁版只回一個空殼，貼文要靠 JavaScript 才會載入，所以用無頭瀏覽器（Playwright）讀；
Playwright 不在 requirements.txt（每次監看都裝太慢），只有要用到的 workflow 才安裝。
"""
from __future__ import annotations

import logging

from . import news  # noqa: F401  (保持與其他 fetcher 相同的載入順序)
from .. import net

log = logging.getLogger(__name__)

BASE = "https://www.threads.com"

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


def render(url: str, wait_ms: int = 6000, scrolls: int = 2) -> dict:
    """用無頭瀏覽器打開網址，回傳 {title, url, text, posts: [{href, text, time}]}。"""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--no-sandbox"])
        try:
            ctx = browser.new_context(locale="zh-TW", user_agent=net.BROWSER_UA, viewport={"width": 1280, "height": 2000})
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(wait_ms)
            for _ in range(scrolls):
                page.mouse.wheel(0, 2500)
                page.wait_for_timeout(1500)
            return page.evaluate(_JS)
        finally:
            browser.close()
