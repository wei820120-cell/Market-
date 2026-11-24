"""推播：一定會印在終端機；有設定環境變數時同步推到 Telegram / Discord。

分成三個頻道，各用一個 Telegram 機器人，手機上比較不亂：
  market（預設）：族群資金流向、到價、大漲、盤後摘要
  news          ：新聞、漲價信、重大訊息
  picks         ：強勢標的（資金流入＋題材＋漲價，附股票期貨）
  research      ：題材研究員（AI 供應鏈研究）；也從這個機器人接收「研究 XXX」指令
  trade         ：進出場機器人（實單記帳、盤前計劃、盤後結算）
  futures       ：微台機器人（日盤／夜盤訊號、收盤彙總、績效；也接收「績效」「今天」「明細」指令）；沒設定時改用進出場機器人

環境變數（不要寫進程式或上傳到 GitHub）：
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID                盤勢機器人
  TELEGRAM_NEWS_BOT_TOKEN, TELEGRAM_NEWS_CHAT_ID      新聞機器人（選用）
  TELEGRAM_PICKS_BOT_TOKEN, TELEGRAM_PICKS_CHAT_ID    選股機器人（選用）
  TELEGRAM_RESEARCH_BOT_TOKEN, TELEGRAM_RESEARCH_CHAT_ID  研究機器人（選用，沒設定時用新聞機器人）
  TELEGRAM_FUTURES_BOT_TOKEN, TELEGRAM_FUTURES_CHAT_ID    微台機器人（選用，沒設定時用進出場機器人；CHAT_ID 沒填沿用 TELEGRAM_CHAT_ID）
  DISCORD_WEBHOOK_URL, DISCORD_NEWS_WEBHOOK_URL, DISCORD_PICKS_WEBHOOK_URL
沒設定新聞／選股機器人時，改由盤勢機器人送出。
CHAT_ID 沒填時沿用 TELEGRAM_CHAT_ID（同一個人跟不同機器人的私訊 Chat ID 相同）。
"""
from __future__ import annotations

import json
import logging
import os
import re
import time

import requests

log = logging.getLogger(__name__)


FALLBACK_CHANNEL = {"research": "news", "futures": "trade"}  # 研究機器人沒設定時改用新聞機器人；微台機器人沒設定時改用進出場機器人


def _clean(v: str | None) -> str | None:
    """金鑰、Chat ID 常見的貼上錯誤：前後多了空白或換行、包著引號、多貼了網址裡的 bot 前綴。
    先清掉再用；填錯時 Telegram 只會回 404 Not Found，很難從訊息看出原因。"""
    if v is None:
        return None
    v = v.strip().strip("\"'“”‘’").strip()
    if v[:3].lower() == "bot" and v[3:4].isdigit():
        v = v[3:]
    return v or None


_TOKEN_SHAPE = re.compile(r"\d{6,12}:[A-Za-z0-9_-]{34,35}(?![A-Za-z0-9_-])")      # 金鑰冒號後面是 34～35 個英數字（新機器人 35 個）


# 金鑰後面直接黏著訊息時間（例：…xyz15:55）：在 Telegram 網頁版用滑鼠選取金鑰時，會把訊息右下角的時間一起複製進來，中間沒有空白。
# 2026-11-16 的實例：結構是「數字×10 冒號 英數×37 冒號 英數×2」＝ 35 個金鑰字元＋「15」＋「:55」。只處理 35 個字元的金鑰（新機器人都是）。
_TOKEN_TIME_TAIL = re.compile(r"(\d{6,12}:[A-Za-z0-9_-]{35})\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AaPp]\.?[Mm]\.?)?")


def _clean_token(v: str | None) -> str | None:
    """機器人金鑰：先做 _clean；格式還是不對時，試著把真正的金鑰擷取出來：
    1. 金鑰後面黏著訊息時間（…15:55）→ 去掉時間；
    2. 前後連著別的文字（整句 BotFather 回覆、網址）→ 取出「數字:34～35 個英數字」。
    擷取不到就原樣交回（讓 Telegram 回 404、由日誌的金鑰檢查指出結構）。"""
    v = _clean(v)
    if v and not re.fullmatch(r"\d{6,12}:[A-Za-z0-9_-]{30,}", v):
        m = _TOKEN_TIME_TAIL.fullmatch(v)
        if m:
            return m.group(1)
        m = _TOKEN_SHAPE.search(v)
        if m:
            return m.group(0)
    return v


def _telegram(channel: str) -> tuple[str | None, str | None]:
    token, chat = _clean_token(os.environ.get("TELEGRAM_BOT_TOKEN")), _clean(os.environ.get("TELEGRAM_CHAT_ID"))
    for ch in (channel, FALLBACK_CHANNEL.get(channel)):
        if not ch or ch == "market":
            continue
        prefix = f"TELEGRAM_{ch.upper()}_"
        own = _clean_token(os.environ.get(prefix + "BOT_TOKEN"))
        if own:
            return own, _clean(os.environ.get(prefix + "CHAT_ID")) or chat
    return token, chat


def token_hint(channel: str) -> str:
    """金鑰有沒有填對的線索，給日誌看（不印金鑰本身）：有沒有設定、長度、格式、機器人編號（冒號前的數字）、前後有沒有空白。"""
    name = f"TELEGRAM_{channel.upper()}_BOT_TOKEN"
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        fb = FALLBACK_CHANNEL.get(channel)
        return f"{name} 沒有設定或是空的" + (f"，改用 {fb} 機器人的金鑰" if fb else "")
    clean = _clean_token(raw) or ""
    ok = re.fullmatch(r"\d{6,12}:[A-Za-z0-9_-]{30,}", clean) is not None
    bot_id = clean.split(":")[0] if ":" in clean else "（沒有冒號）"
    extra = len(raw.strip()) - len(clean)
    return (f"{name}：原始長度 {len(raw)}、清理後 {len(clean)}（正常 46）｜格式{'正確' if ok else '不對（應為「數字:35 個英數字」）'}"
            f"｜機器人編號 {bot_id}｜前後空白 {'有（已自動去掉）' if raw != raw.strip() else '無'}"
            + (f"｜多出 {extra} 個字元（已自動去掉）" if ok and extra > 0 else "")
            + ("" if ok and not extra else f"｜結構：{_structure(raw)}"))


_ODD_NAMES = {" ": "空白", "\n": "換行", "\r": "換行CR", "\t": "Tab", " ": "不換行空白", "　": "全形空白", "​": "零寬空白",
              "‌": "零寬字元", "‍": "零寬字元", "﻿": "BOM", "：": "全形冒號", "/": "斜線", "@": "小老鼠", ".": "句點",
              ",": "逗號", "=": "等號", "`": "反引號", "\"": "引號", "'": "引號", "*": "星號", "…": "刪節號"}


def _structure(s: str) -> str:
    """字串的結構，不含內容（給日誌判斷金鑰貼錯在哪）：連續的數字、連續的英數字（含 - _）只報幾個，冒號照寫，
    其他字元（本來就不該出現在金鑰裡的）報 Unicode 編號與名稱。冒號之後的數字併進英數字一起算，不洩漏金鑰內容。"""
    out: list[str] = []
    lead = re.match(r"\d+(?=:)", s)          # 開頭緊接冒號的那串數字＝機器人編號（不是秘密），單獨報；其餘一律算英數字
    if lead:
        out.append(f"數字×{lead.end()}")
        s = s[lead.end():]
    run = 0
    for ch in s:
        if ch.isascii() and (ch.isalnum() or ch in "_-"):
            run += 1
            continue
        if run:
            out.append(f"英數×{run}")
            run = 0
        out.append("冒號" if ch == ":" else f"〔U+{ord(ch):04X}{' ' + _ODD_NAMES[ch] if ch in _ODD_NAMES else ''}〕")
    if run:
        out.append(f"英數×{run}")
    return " ".join(out)


def _fallbacks(channel: str) -> list[tuple[str, str]]:
    """第一順位機器人推播被拒（金鑰無效，Telegram 回 401／404）時，依序改用的（金鑰, Chat ID）：退回用的機器人、盤勢機器人。"""
    first = _telegram(channel)[0]
    default_chat = _clean(os.environ.get("TELEGRAM_CHAT_ID"))
    out: list[tuple[str | None, str | None]] = []
    fb = FALLBACK_CHANNEL.get(channel)
    if fb and fb != "market":
        prefix = f"TELEGRAM_{fb.upper()}_"
        out.append((_clean_token(os.environ.get(prefix + "BOT_TOKEN")), _clean(os.environ.get(prefix + "CHAT_ID")) or default_chat))
    out.append((_clean_token(os.environ.get("TELEGRAM_BOT_TOKEN")), default_chat))
    return [(t, c) for t, c in out if t and c and t != first]


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
        log.warning("讀取 Telegram 指令失敗（%s）：%s", channel, type(e).__name__)      # 不印例外內容：連線錯誤的訊息會帶出含金鑰的網址
        return []
    if not data.get("ok"):
        # 金鑰填錯或已失效（401）、機器人設了 webhook（409）等：以前靜默回空，看不出為什麼收不到指令
        log.warning("讀取 Telegram 指令被拒（%s）：%s %s", channel, data.get("error_code"), data.get("description"))
        return []
    return data.get("result", [])


def owner_chat(channel: str) -> str | None:
    return _telegram(channel)[1]


def bot_token(channel: str) -> str | None:
    return _telegram(channel)[0]


def _discord(channel: str) -> str | None:
    key = f"DISCORD_{channel.upper()}_WEBHOOK_URL"
    if channel != "market" and os.environ.get(key):
        return os.environ[key]
    return os.environ.get("DISCORD_WEBHOOK_URL")


def _chunks(text: str, limit: int = 4000) -> list[str]:
    """長訊息依換行切成多則（Telegram 一則上限 4096 字）。"""
    out, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if cur:
                out.append(cur)
                cur = ""
            out.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) + 1 > limit:
            out.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out or [""]


def _post_message(token: str, chat: str, part: str) -> requests.Response:
    return requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                         json={"chat_id": chat, "text": part, "disable_web_page_preview": True}, timeout=10)


_sleep = time.sleep           # 測試時換掉，不要真的等
RETRY_DELAYS = (2.0, 6.0)     # 暫時性錯誤（逾時、連線失敗、429、5xx）最多再試 2 次，每次之前等幾秒
MAX_RETRY_AFTER = 20.0        # Telegram 429 指定要等的秒數超過這個就不在這裡卡住：回報失敗、交給呼叫端稍後重送


def _retry_after(r: requests.Response) -> float | None:
    """Telegram 429 會在 JSON 的 parameters.retry_after 指定要等幾秒。"""
    try:
        v = (r.json().get("parameters") or {}).get("retry_after")
        return float(v) if v is not None else None
    except (ValueError, TypeError, AttributeError):
        return None


def _post_retry(token: str, chat: str, part: str, channel: str) -> requests.Response | None:
    """送一則訊息。暫時性錯誤（逾時、連線失敗、429、5xx）依 RETRY_DELAYS 重試，不再「一次失敗就丟掉」
    （2026-11-23 日盤微台的停損訊息就是這樣不見的：帳已經動了、使用者沒收到）。
    回傳最後一次的 Response；每次都連線失敗回 None。429 指定的等待超過 MAX_RETRY_AFTER 就不等、直接回傳。"""
    r = None
    for i in range(len(RETRY_DELAYS) + 1):
        wait = RETRY_DELAYS[i] if i < len(RETRY_DELAYS) else None       # None＝這是最後一次嘗試
        try:
            r = _post_message(token, chat, part)
        except requests.RequestException as e:
            r = None
            log.warning("Telegram（%s）推播失敗：%s（第 %d 次%s）", channel, type(e).__name__, i + 1,
                        "，稍後重試" if wait is not None else "，放棄")      # 不印例外內容：會帶出含金鑰的網址
        else:
            if r.ok or not (r.status_code == 429 or r.status_code >= 500):
                return r
            ra = _retry_after(r) if r.status_code == 429 else None
            if ra is not None and ra > MAX_RETRY_AFTER:
                log.warning("Telegram（%s）要求等 %.0f 秒（429），超過 %.0f 秒不在這裡等", channel, ra, MAX_RETRY_AFTER)
                return r
            if ra is not None and wait is not None:
                wait = ra + 1
            log.warning("Telegram（%s）暫時失敗（%s，第 %d 次%s）", channel, r.status_code, i + 1,
                        "，稍後重試" if wait is not None else "，放棄")
        if wait is None:
            break
        _sleep(wait)
    return r


def send(text: str, channel: str = "market") -> bool:
    """推播；一定會印在終端機。回傳 True＝送到了（或根本沒設定要送去哪）、False＝有設定的管道送失敗（已重試過）。

    有設定 Telegram 就只看 Telegram 的結果；沒設定 Telegram 才看 Discord（免得 Discord 失敗讓呼叫端重送、Telegram 收到兩次）。
    一則訊息切成多段時只要有一段失敗就回 False；重送會讓前面成功的段落重複，所以要靠重送保證送達的訊息請不要超過 4000 字。"""
    print(text, flush=True)
    ok: bool | None = None                      # None＝沒有任何管道可送
    token, chat = _telegram(channel)
    if token and chat:
        ok = True
        alts = _fallbacks(channel)
        for part in _chunks(text):
            r = _post_retry(token, chat, part, channel)
            while r is not None and not r.ok and r.status_code in (401, 404) and alts:
                # 金鑰無效（404＝金鑰格式不對或機器人不存在，401＝金鑰已被 revoke）：改用備援機器人送，訊號不要掉
                log.warning("Telegram（%s）金鑰無效（%s %s），這則改用備援機器人送出——請檢查 TELEGRAM_%s_BOT_TOKEN 這個 Secret",
                            channel, r.status_code, r.text[:80], channel.upper())
                token, chat = alts.pop(0)
                r = _post_retry(token, chat, part, channel)
            if r is None:
                ok = False                      # 連線一直失敗（警告已記）
            elif not r.ok:
                # 常見原因：Chat ID 填錯、還沒對機器人按 Start
                log.warning("Telegram（%s）推播被拒：%s %s", channel, r.status_code, r.text[:200])
                ok = False
    hook = _discord(channel)
    if hook:
        discord_ok = True
        for part in _chunks(text, 1900):
            try:
                requests.post(hook, json={"content": part}, timeout=10)
            except requests.RequestException as e:
                log.warning("Discord 推播失敗：%s", e)
                discord_ok = False
        if ok is None:
            ok = discord_ok
    return ok is not False


def _photo_ok(path) -> bool:
    """Telegram 相片限制：寬＋高 ≤ 10000、長寬比 ≤ 20；超過改用檔案傳送（不壓縮）。"""
    try:
        from PIL import Image
        with Image.open(path) as im:
            w, h = im.size
        return w + h <= 10000 and max(w, h) / max(1, min(w, h)) <= 20
    except Exception:  # noqa: BLE001
        return True


def send_photos(paths: list, caption: str = "", channel: str = "market") -> None:
    """推播圖片（最多 10 張一組相簿）；caption 放在第一張（上限 1024 字）。"""
    paths = [p for p in paths if p]
    if not paths:
        return
    print(f"[圖片] {caption[:80]} {[str(p) for p in paths]}", flush=True)
    token, chat = _telegram(channel)
    if token and chat:
        photos = [p for p in paths if _photo_ok(p)]
        docs = [p for p in paths if p not in photos]
        try:
            for i in range(0, len(photos), 10):
                group = photos[i:i + 10]
                files = {f"p{j}": open(p, "rb") for j, p in enumerate(group)}
                try:
                    if len(group) == 1:
                        r = requests.post(f"https://api.telegram.org/bot{token}/sendPhoto",
                                          data={"chat_id": chat, "caption": caption[:1024] if i == 0 else ""},
                                          files={"photo": files["p0"]}, timeout=60)
                    else:
                        media = [{"type": "photo", "media": f"attach://p{j}"} for j in range(len(group))]
                        if i == 0 and caption:
                            media[0]["caption"] = caption[:1024]
                        r = requests.post(f"https://api.telegram.org/bot{token}/sendMediaGroup",
                                          data={"chat_id": chat, "media": json.dumps(media, ensure_ascii=False)},
                                          files=files, timeout=60)
                finally:
                    for f in files.values():
                        f.close()
                if not r.ok:
                    log.warning("Telegram（%s）圖片推播被拒：%s %s", channel, r.status_code, r.text[:200])
            for p in docs:
                with open(p, "rb") as f:
                    r = requests.post(f"https://api.telegram.org/bot{token}/sendDocument",
                                      data={"chat_id": chat, "caption": "" if photos else caption[:1024]},
                                      files={"document": f}, timeout=60)
                if not r.ok:
                    log.warning("Telegram（%s）檔案推播被拒：%s %s", channel, r.status_code, r.text[:200])
        except (requests.RequestException, OSError) as e:
            log.warning("Telegram（%s）圖片推播失敗：%s", channel, e)
    hook = _discord(channel)
    if hook:
        for p in paths[:10]:
            try:
                with open(p, "rb") as f:
                    requests.post(hook, data={"content": caption[:1900] if p == paths[0] else ""},
                                  files={"file": f}, timeout=60)
            except (requests.RequestException, OSError) as e:
                log.warning("Discord 圖片推播失敗：%s", e)
