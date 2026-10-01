"""推播：一定會印在終端機；有設定環境變數時同步推到 Telegram / Discord。

環境變數（不要寫進程式或上傳到 GitHub）：
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
  DISCORD_WEBHOOK_URL
"""
from __future__ import annotations

import logging
import os

import requests

log = logging.getLogger(__name__)


def send(text: str) -> None:
    print(text, flush=True)
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat:
        try:
            requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": text[:4000], "disable_web_page_preview": True}, timeout=10)
        except requests.RequestException as e:
            log.warning("Telegram 推播失敗：%s", e)
    hook = os.environ.get("DISCORD_WEBHOOK_URL")
    if hook:
        try:
            requests.post(hook, json={"content": text[:1900]}, timeout=10)
        except requests.RequestException as e:
            log.warning("Discord 推播失敗：%s", e)
