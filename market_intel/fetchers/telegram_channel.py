"""公開 Telegram 頻道（例如股癌 t.me/Gooaye）：讀網頁版 https://t.me/s/<頻道>，不需登入。

只抓公開頻道、只推給自己看，不轉貼。
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

# 已推過的貼文編號存在 repo 裡（GitHub Actions 每次都是全新環境，要靠 commit 保存）
STATE_PATH = ROOT / "state" / "telegram_seen.json"


@dataclass
class ChannelPost:
    channel: str      # 頻道帳號，例如 Gooaye
    post_id: int
    text: str
    published: str    # ISO 時間
    url: str


def _clean(fragment: str) -> str:
    t = re.sub(r"<br\s*/?>", "\n", fragment, flags=re.I)
    t = re.sub(r"<[^>]+>", "", t)
    return html.unescape(t).strip()


def parse_channel_html(text: str, channel: str) -> list[ChannelPost]:
    posts = []
    # 每則貼文是一個 data-post="頻道/編號" 的區塊
    blocks = re.split(r'(?=<div class="tgme_widget_message_wrap)', text)
    for b in blocks:
        m = re.search(r'data-post="([^"/]+)/(\d+)"', b)
        if not m:
            continue
        body = re.search(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', b, flags=re.S)
        when = re.search(r'<time[^>]*datetime="([^"]+)"', b)
        content = _clean(body.group(1)) if body else ""
        if not content:
            continue  # 純圖片／貼圖沒有文字就略過
        pid = int(m.group(2))
        posts.append(ChannelPost(channel=m.group(1), post_id=pid, text=content,
                                 published=when.group(1) if when else "", url=f"https://t.me/{m.group(1)}/{pid}"))
    return sorted(posts, key=lambda p: p.post_id)


def fetch_channel(channel: str) -> list[ChannelPost]:
    return parse_channel_html(net.get(f"https://t.me/s/{channel}", timeout=20).text, channel)


def load_state() -> dict[str, int]:
    if STATE_PATH.exists():
        try:
            return {k: int(v) for k, v in json.loads(STATE_PATH.read_text(encoding="utf-8")).items()}
        except (ValueError, OSError):
            pass
    return {}


def save_state(state: dict[str, int]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def new_posts(channel: str, posts: list[ChannelPost], state: dict[str, int]) -> list[ChannelPost]:
    """回傳比上次記錄更新的貼文，並更新 state。第一次看到這個頻道時只記錄位置、不推播。"""
    if not posts:
        return []
    latest = max(p.post_id for p in posts)
    last = state.get(channel)
    state[channel] = max(latest, last or 0)
    if last is None:
        log.info("頻道 %s 第一次讀取，記錄到第 %d 則，之後只推新貼文", channel, latest)
        return []
    return [p for p in posts if p.post_id > last]


def is_ad(text: str, patterns: list[str]) -> bool:
    """業配／廣告貼文（config/sources.yaml 的 skip_patterns）。"""
    return any(re.search(p, text or "") for p in patterns or [])
