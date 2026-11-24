"""推播（market_intel/notify.py）：暫時性錯誤要重試、回傳有沒有送到。

2026-11-23 日盤微台的停損訊息不見了：舊版 send 一次失敗只記警告、不重送，呼叫端又不知道失敗，帳照存。"""
from __future__ import annotations

import requests

from market_intel import notify

TOKEN = "123456789:" + "A" * 35
ALT_TOKEN = "987654321:" + "B" * 35


class Resp:
    def __init__(self, status: int = 200, body: dict | None = None):
        self.status_code, self.ok, self._body, self.text = status, status < 400, body or {}, str(body or {})

    def json(self):
        return self._body


def _setup(monkeypatch, script):
    """script：依序回傳（或丟出）的結果；回傳 (calls, waits)。calls 記錄每次嘗試用的金鑰。"""
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_FUTURES_BOT_TOKEN", "TELEGRAM_FUTURES_CHAT_ID", "TELEGRAM_TRADE_BOT_TOKEN",
              "TELEGRAM_TRADE_CHAT_ID", "DISCORD_WEBHOOK_URL", "DISCORD_FUTURES_WEBHOOK_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    calls, waits, it = [], [], iter(script)
    monkeypatch.setattr(notify, "_sleep", waits.append)

    def post(token, chat, part):
        calls.append(token)
        r = next(it)
        if isinstance(r, Exception):
            raise r
        return r
    monkeypatch.setattr(notify, "_post_message", post)
    return calls, waits


def test_nothing_configured_counts_as_delivered(monkeypatch):
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "DISCORD_WEBHOOK_URL"):
        monkeypatch.delenv(k, raising=False)
    assert notify.send("沒設定任何管道") is True       # 開發環境只印在終端機，不算失敗、也不該被當成要重送


def test_timeouts_are_retried_until_success(monkeypatch):
    calls, waits = _setup(monkeypatch, [requests.ConnectTimeout(), requests.ConnectionError(), Resp(200)])
    assert notify.send("訊號") is True
    assert len(calls) == 3 and waits == list(notify.RETRY_DELAYS)


def test_gives_up_after_all_retries_and_says_so(monkeypatch):
    calls, waits = _setup(monkeypatch, [requests.ReadTimeout()] * 3)
    assert notify.send("訊號") is False                  # 呼叫端據此排進補發佇列
    assert len(calls) == len(notify.RETRY_DELAYS) + 1


def test_429_waits_for_retry_after_then_succeeds(monkeypatch):
    calls, waits = _setup(monkeypatch, [Resp(429, {"parameters": {"retry_after": 3}}), Resp(200)])
    assert notify.send("訊號") is True
    assert len(calls) == 2 and waits == [4.0]            # 照 Telegram 指定的秒數多等 1 秒


def test_429_with_a_long_retry_after_does_not_block_the_loop(monkeypatch):
    calls, waits = _setup(monkeypatch, [Resp(429, {"parameters": {"retry_after": 300}})])
    assert notify.send("訊號") is False
    assert len(calls) == 1 and waits == []                # 不在這裡卡 5 分鐘，交給呼叫端下一輪補發


def test_server_errors_are_retried_but_client_errors_are_not(monkeypatch):
    calls, waits = _setup(monkeypatch, [Resp(502), Resp(200)])
    assert notify.send("訊號") is True and len(calls) == 2
    calls, waits = _setup(monkeypatch, [Resp(400, {"description": "chat not found"})])
    assert notify.send("訊號") is False and len(calls) == 1 and waits == []      # Chat ID 填錯之類：重試沒用


def test_invalid_futures_token_falls_back_to_the_default_bot(monkeypatch):
    calls, waits = _setup(monkeypatch, [Resp(404), Resp(200)])
    monkeypatch.setenv("TELEGRAM_FUTURES_BOT_TOKEN", ALT_TOKEN)
    assert notify.send("訊號", channel="futures") is True
    assert calls == [ALT_TOKEN, TOKEN]                   # 專屬機器人金鑰無效 → 改用盤勢機器人，訊號不掉


def test_discord_failure_does_not_make_the_caller_resend_to_telegram(monkeypatch):
    calls, waits = _setup(monkeypatch, [Resp(200)])
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.invalid/hook")

    def boom(*a, **kw):
        raise requests.ConnectionError()
    monkeypatch.setattr(notify.requests, "post", boom)
    assert notify.send("訊號") is True                   # Telegram 送到了就算送到，否則補發會讓 Telegram 收到兩次
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(k, raising=False)
    assert notify.send("訊號") is False                  # 只剩 Discord 可送、又失敗 → 才算失敗
