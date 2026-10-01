"""推播：一定會印在終端機；有設定環境變數時同步推到 Telegram / Discord。

分成兩個頻道，各用一個 Telegram 機器人，手機上比較不亂：
  market（預設）：族群資金流向、到價、大漲、盤後摘要
  news          ：新聞、漲價信、重大訊息

環境變數（不要寫進程式或上傳到 GitHub）：
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID            盤勢機器人
  TELEGRAM_NEWS_BOT_TOKEN, TELEGRAM_NEWS_CHAT_ID  新聞機器人（選用）
  DISCORD_WEBHOOK_URL, DISCORD_NEWS_WEBHOOK_URL
沒設定新聞機器人時，新聞改由盤勢機器人送出。
新聞機器人的 CHAT_ID 沒填時沿用 TELEGRAM_CHAT_ID（同一個人跟不同機器人的私訊 Chat ID 相同）。
"""
from __future__ import annotations

import logging
import os

import requests

log = logging.getLogger(__name__)


def _telegram(channel: str) -> tuple[str | None, str | None]:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if channel == "news" and os.environ.get("TELEGRAM_NEWS_BOT_TOKEN"):
        token = os.environ["TELEGRAM_NEWS_BOT_TOKEN"]
        chat = os.environ.get("TELEGRAM_NEWS_CHAT_ID") or chat
    return token, chat


def _discord(channel: str) -> str | None:
    if channel == "news" and os.environ.get("DISCORD_NEWS_WEBHOOK_URL"):
        return os.environ["DISCORD_NEWS_WEBHOOK_URL"]
    return os.environ.get("DISCORD_WEBHOOK_URL")


def send(text: str, channel: str = "market") -> None:
    print(text, flush=True)
    token, chat = _telegram(channel)
    if token and chat:
        try:
            r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                              json={"chat_id": chat, "text": text[:4000], "disable_web_page_preview": True}, timeout=10)
            if not r.ok:
                # 常見原因：Chat ID 填錯、還沒對機器人按 Start
                log.warning("Telegram（%s）推播被拒：%s %s", channel, r.status_code, r.text[:200])
        except requests.RequestException as e:
            log.warning("Telegram（%s）推播失敗：%s", channel, e)
    hook = _discord(channel)
    if hook:
        try:
            requests.post(hook, json={"content": text[:1900]}, timeout=10)
        except requests.RequestException as e:
            log.warning("Discord 推播失敗：%s", e)
