"""推播：一定會印在終端機；有設定環境變數時同步推到 Telegram / Discord。

分成三個頻道，各用一個 Telegram 機器人，手機上比較不亂：
  market（預設）：族群資金流向、到價、大漲、盤後摘要
  news          ：新聞、漲價信、重大訊息
  picks         ：強勢標的（資金流入＋題材＋漲價，附股票期貨）
  research      ：題材研究員（AI 供應鏈研究）；也從這個機器人接收「研究 XXX」指令

環境變數（不要寫進程式或上傳到 GitHub）：
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID                盤勢機器人
  TELEGRAM_NEWS_BOT_TOKEN, TELEGRAM_NEWS_CHAT_ID      新聞機器人（選用）
  TELEGRAM_PICKS_BOT_TOKEN, TELEGRAM_PICKS_CHAT_ID    選股機器人（選用）
  TELEGRAM_RESEARCH_BOT_TOKEN, TELEGRAM_RESEARCH_CHAT_ID  研究機器人（選用，沒設定時用新聞機器人）
  DISCORD_WEBHOOK_URL, DISCORD_NEWS_WEBHOOK_URL, DISCORD_PICKS_WEBHOOK_URL
沒設定新聞／選股機器人時，改由盤勢機器人送出。
CHAT_ID 沒填時沿用 TELEGRAM_CHAT_ID（同一個人跟不同機器人的私訊 Chat ID 相同）。
"""
from __future__ import annotations

import logging
import os

import requests

log = logging.getLogger(__name__)


FALLBACK_CHANNEL = {"research": "news"}  # 研究機器人沒設定時改用新聞機器人


def _telegram(channel: str) -> tuple[str | None, str | None]:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    for ch in (channel, FALLBACK_CHANNEL.get(channel)):
        if not ch or ch == "market":
            continue
        prefix = f"TELEGRAM_{ch.upper()}_"
        if os.environ.get(prefix + "BOT_TOKEN"):
            return os.environ[prefix + "BOT_TOKEN"], os.environ.get(prefix + "CHAT_ID") or chat
    return token, chat


def get_updates(channel: str, offset: int | None) -> list[dict]:
    """讀取使用者傳給機器人的訊息（Telegram getUpdates）。"""
    token, _ = _telegram(channel)
    if not token:
        return []
    params = {"timeout": 0, "allowed_updates": '["message"]'}
    if offset is not None:
        params["offset"] = offset
    try:
        r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", params=params, timeout=15)
        data = r.json()
    except (requests.RequestException, ValueError) as e:
        log.warning("讀取 Telegram 指令失敗：%s", e)
        return []
    return data.get("result", []) if data.get("ok") else []


def owner_chat(channel: str) -> str | None:
    return _telegram(channel)[1]


def _discord(channel: str) -> str | None:
    key = f"DISCORD_{channel.upper()}_WEBHOOK_URL"
    if channel != "market" and os.environ.get(key):
        return os.environ[key]
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
