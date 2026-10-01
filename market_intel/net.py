"""HTTP 共用層：統一 User-Agent、重試、各主機節流（避免被證交所封鎖 IP）。"""
from __future__ import annotations

import logging
import os
import threading
import time
from urllib.parse import urlparse

import requests

log = logging.getLogger(__name__)

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# 每個主機兩次請求之間的最短間隔（秒）。證交所過於頻繁會暫時封鎖 IP。
HOST_MIN_INTERVAL = {
    "www.twse.com.tw": 2.5,
    "mis.twse.com.tw": 1.0,
    "www.tpex.org.tw": 1.0,
    "www.sec.gov": 0.2,
}

_session: requests.Session | None = None
_last_call: dict[str, float] = {}
_lock = threading.Lock()


def session() -> requests.Session:
    global _session
    if _session is None:
        s = requests.Session()
        s.headers.update({"User-Agent": BROWSER_UA, "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8"})
        _session = s
    return _session


def _throttle(host: str) -> None:
    interval = HOST_MIN_INTERVAL.get(host, 0.0)
    if not interval:
        return
    with _lock:
        wait = _last_call.get(host, 0.0) + interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_call[host] = time.monotonic()


def get(url: str, params=None, headers=None, timeout: float = 15, retries: int = 3) -> requests.Response:
    host = urlparse(url).netloc
    if host == "www.sec.gov":
        # SEC 規定 User-Agent 要帶聯絡 email
        headers = {**(headers or {}), "User-Agent": os.environ.get("SEC_USER_AGENT", "market-intel research admin@example.com")}
    last_exc: Exception | None = None
    for attempt in range(retries):
        _throttle(host)
        try:
            r = session().get(url, params=params, headers=headers, timeout=timeout)
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            last_exc = e
            log.debug("GET %s 失敗（第 %d 次）：%s", url, attempt + 1, e)
            time.sleep(2 ** attempt)
    assert last_exc is not None
    raise last_exc


def get_json(url: str, params=None, headers=None, timeout: float = 15, retries: int = 3):
    return get(url, params=params, headers=headers, timeout=timeout, retries=retries).json()
