"""台股盤中即時報價（證交所基本市況報導 MIS，約 5 秒更新一次）。

注意：MIS 是公開網頁背後的介面，不是正式 API。要更快、更穩定的逐筆資料，
請改接券商 API（例如富果 Fugle、永豐 Shioaji），再把結果轉成這裡的 Quote 格式即可。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from .. import net
from ..utils import to_float

log = logging.getLogger(__name__)

MIS_HOME = "https://mis.twse.com.tw/stock/index.jsp"
MIS_API = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
BATCH = 50

# 大盤指數代碼
INDEX_CHANNELS = {"加權指數": "tse_t00.tw", "櫃買指數": "otc_o00.tw"}


@dataclass
class Quote:
    code: str
    name: str
    market: str
    price: float | None
    prev_close: float | None
    open: float | None
    high: float | None
    low: float | None
    volume_lots: float | None  # 累積成交量（張；指數為成交金額百萬元）
    time: str
    bid: float | None = None
    ask: float | None = None

    @property
    def change_pct(self) -> float | None:
        if self.price is None or not self.prev_close:
            return None
        return (self.price / self.prev_close - 1) * 100

    @property
    def turnover(self) -> float:
        """估算累積成交金額（元）＝ 累積張數 × 1000 × 現價。"""
        if not self.volume_lots or self.price is None:
            return 0.0
        return self.volume_lots * 1000 * self.price


def _first_level(s) -> float | None:
    """五檔字串 '1000.00_999.00_...' 取第一檔。"""
    if not s:
        return None
    return to_float(str(s).split("_")[0])


def parse_mis(payload: dict) -> list[Quote]:
    quotes = []
    for m in payload.get("msgArray") or []:
        bid, ask = _first_level(m.get("b")), _first_level(m.get("a"))
        price = to_float(m.get("z"))
        if price is None:  # 這一盤沒有成交，用買賣中價或單邊價
            if bid and ask:
                price = round((bid + ask) / 2, 2)
            else:
                price = bid or ask
        quotes.append(Quote(
            code=str(m.get("c", "")).strip(),
            name=str(m.get("n", "")).strip(),
            market=str(m.get("ex", "")),
            price=price,
            prev_close=to_float(m.get("y")),
            open=to_float(m.get("o")),
            high=to_float(m.get("h")),
            low=to_float(m.get("l")),
            volume_lots=to_float(m.get("v")),
            time=str(m.get("t", "")),
            bid=bid,
            ask=ask,
        ))
    return quotes


_warmed = False


def _warm_up() -> None:
    """MIS 有時需要先拿到首頁 cookie 才會回資料。"""
    global _warmed
    if not _warmed:
        try:
            net.get(MIS_HOME, timeout=10, retries=1)
        except Exception:  # noqa: BLE001
            pass
        _warmed = True


def channels_for(codes: list[str], listings: dict[str, dict]) -> list[str]:
    """代號轉 MIS 頻道；不知道上市或上櫃時兩個都查。"""
    out = []
    for c in codes:
        market = (listings.get(c) or {}).get("market")
        if market in ("tse", "otc"):
            out.append(f"{market}_{c}.tw")
        else:
            out.extend([f"tse_{c}.tw", f"otc_{c}.tw"])
    return out


def fetch_channels(channels: list[str]) -> list[Quote]:
    _warm_up()
    quotes: list[Quote] = []
    for i in range(0, len(channels), BATCH):
        chunk = channels[i:i + BATCH]
        try:
            payload = net.get_json(
                MIS_API,
                params={"ex_ch": "|".join(chunk), "json": 1, "delay": 0, "_": int(time.time() * 1000)},
                headers={"Referer": MIS_HOME},
                timeout=10,
            )
            quotes.extend(parse_mis(payload))
        except Exception as e:  # noqa: BLE001
            log.warning("MIS 即時報價失敗（%d 檔）：%s", len(chunk), e)
    return quotes


def fetch_quotes(codes: list[str], listings: dict[str, dict]) -> dict[str, Quote]:
    quotes = fetch_channels(channels_for(codes, listings))
    return {q.code: q for q in quotes if q.code}


def fetch_indices() -> dict[str, Quote]:
    quotes = fetch_channels(list(INDEX_CHANNELS.values()))
    by_ch = {f"{q.market}_{q.code}.tw": q for q in quotes}
    return {name: by_ch[ch] for name, ch in INDEX_CHANNELS.items() if ch in by_ch}
