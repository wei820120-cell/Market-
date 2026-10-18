"""開盤前環境：前一晚的台指期夜盤、美股收盤、那指期貨、亞股早盤 → 一個開盤風險燈號（🟢／🟡／🔴）。

為什麼需要：現行「大盤多頭／空頭」只看加權日線均線，是落後指標；週五夜盤殺下去、美股重挫，
週一開盤的跳空它完全看不到，08:30 盤前計劃還是會寫「可以依計劃進場」。
這裡只做「參考」：在盤前計劃最上面加一段環境摘要與燈號，不會自動擋單；
有沒有用要等回測（美股隔夜報酬對照隔天台股跳空與突破勝率）再決定要不要變成規則。

資料來源（任何一項抓不到就略過，不影響其他）：
- 台指期夜盤：期交所 OpenAPI DailyMarketReportFut，TX 近月、交易時段含「盤後」。夜盤的交易日是「下一個營業日」
  （週五夜盤屬於週一），所以會核對資料日期，日期不符（可能還沒更新）就不採用，免得拿上一晚的數字誤導。
- 美股收盤、那指期貨、日經、韓國綜合、VIX：Yahoo Finance（macro.fetch_one，延遲報價）。
  早上 08:30 日經、韓股已開盤 30 分鐘，看得到「週一早上亞洲盤」；那指期貨連週末／亞洲時段的消息都反映。
"""
from __future__ import annotations

import logging
import re
from datetime import timedelta

from . import config
from .utils import now_tw, pick, to_float

log = logging.getLogger(__name__)

QUOTES = {  # key: (Yahoo 代號, 顯示名稱)
    "nasdaq": ("^IXIC", "那指"),
    "sox": ("^SOX", "費半"),
    "spx": ("^GSPC", "標普"),
    "nq": ("NQ=F", "那指期貨"),
    "n225": ("^N225", "日經"),
    "kospi": ("^KS11", "韓國"),
    "vix": ("^VIX", "VIX"),
}
DEFAULTS = {"enabled": True, "night_yellow": -0.8, "night_red": -1.5, "nasdaq_yellow": -1.2, "nasdaq_red": -2.0,
            "sox_yellow": -2.0, "sox_red": -3.0, "asia_yellow": -1.0, "asia_red": -2.0, "vix_yellow": 25.0,
            "vix_red": 30.0, "bull_night": 0.8}
ADVICE = {
    "red": "開盤跳空下殺風險高：今天不追開盤價；新單先等開盤 30 分鐘站穩再評估，持倉照原停損。",
    "yellow": "留意開盤跳空：進場不追開盤價，改看開盤 15 分鐘後是否守住。",
    "green": "沒有明顯的隔夜風險。",
}
ICON = {"red": "🔴 風險偏高", "yellow": "🟡 留意跳空", "green": "🟢 風險不高"}


def settings() -> dict:
    st = config.settings().get("premarket") or {}
    return {k: type(v)(st.get(k, v)) for k, v in DEFAULTS.items()}


def expected_night_date(now=None) -> str:
    """最近一場夜盤的交易日（YYYYMMDD）：平日 15:00 前＝今天（昨晚開盤的那場），15:00 後／週末＝下一個營業日。
    不處理國定假日（假日資料日期對不上就當沒資料，下面會說明）。"""
    now = now or now_tw()
    day = now.date()
    if now.weekday() < 5 and now.hour < 15:
        return f"{day:%Y%m%d}"
    day += timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return f"{day:%Y%m%d}"


def _digits(v) -> str:
    return re.sub(r"\D", "", str(v or ""))[:8]


def _month(r: dict) -> str:
    m = _digits(pick(r, "ContractMonth(Week)", "到期月份", default=""))
    return m if len(m) == 6 else ""  # 週契約是 6 碼加週別，月契約剛好 6 碼


def tx_night(rows: list[dict], now=None) -> dict | None:
    """台指期（TX）近月夜盤：{last, pct, date, month, expected, stale}；沒有夜盤資料回傳 None。
    漲跌幅用該筆的「漲跌」與「收盤價」換算（相對前一日結算價，和期交所顯示一致）；缺欄位才退回「%」欄。"""
    night = [r for r in rows
             if str(pick(r, "Contract", "契約", default="")).strip() == "TX"
             and "盤後" in str(pick(r, "TradingSession", "交易時段", default="")) and _month(r)]
    if not night:
        return None
    latest = max(_digits(pick(r, "Date", "日期")) for r in night)
    r = min((r for r in night if _digits(pick(r, "Date", "日期")) == latest), key=_month)
    last = to_float(pick(r, "Last", "Close", "收盤價"))
    chg = to_float(pick(r, "Change", "漲跌價"))
    if not last:
        return None
    if chg is not None and last - chg:
        pct = chg / (last - chg) * 100
    else:
        pct = to_float(pick(r, "%", "漲跌%"))
    if pct is None:
        return None
    expected = expected_night_date(now)
    return {"last": last, "pct": round(pct, 2), "date": latest, "month": _month(r), "expected": expected,
            "stale": latest != expected}


def assess(d: dict, cfg: dict | None = None) -> dict:
    """d：{night, nasdaq, sox, nq, n225, kospi（皆為漲跌 %）, vix（指數）}，缺的略過。回傳 {level, reasons, bull}。"""
    cfg = cfg or settings()
    red, yellow = [], []

    def check(label: str, v, key: str, fmt: str = "{:+.1f}%") -> None:
        if v is None:
            return
        if v <= cfg[f"{key}_red"]:
            red.append(f"{label} {fmt.format(v)}")
        elif v <= cfg[f"{key}_yellow"]:
            yellow.append(f"{label} {fmt.format(v)}")

    check("夜盤", d.get("night"), "night")
    check("那指", d.get("nasdaq"), "nasdaq")
    check("那指期貨", d.get("nq"), "nasdaq")
    check("費半", d.get("sox"), "sox")
    asia = [d[k] for k in ("n225", "kospi") if d.get(k) is not None]
    if asia:
        check("亞股均", sum(asia) / len(asia), "asia")
    vix = d.get("vix")
    if vix is not None:
        if vix >= cfg["vix_red"]:
            red.append(f"VIX {vix:.1f}")
        elif vix >= cfg["vix_yellow"]:
            yellow.append(f"VIX {vix:.1f}")
    level = "red" if red else "yellow" if yellow else "green"
    bull = level == "green" and ((d.get("night") or 0) >= cfg["bull_night"] or (d.get("nasdaq") or 0) >= 1.2)
    return {"level": level, "reasons": red + yellow, "bull": bull}


def _q(quotes: dict, key: str, field: str = "d1_pct"):
    return (quotes.get(key) or {}).get(field)


def text(night: dict | None, quotes: dict, ass: dict) -> str:
    """推播用文字。"""
    head = f"🌙 開盤前環境｜{ICON[ass['level']]}" + (f"（{'、'.join(ass['reasons'])}）" if ass["reasons"] else "")
    if ass.get("bull"):
        head += "，隔夜偏多"
    lines = [head]
    if night:
        if night["stale"]:
            lines.append(f"  台指期夜盤：資料日期 {night['date']} 與預期 {night['expected']} 不符，未採用（可能還沒更新）")
        else:
            lines.append(f"  台指期夜盤 {night['last']:,.0f}（{night['pct']:+.2f}%，{night['date'][4:6]}/{night['date'][6:]} 交易日）")
    us = [f"{QUOTES[k][1]} {_q(quotes, k):+.1f}%" for k in ("nasdaq", "sox", "spx") if _q(quotes, k) is not None]
    if _q(quotes, "vix", "last") is not None:
        us.append(f"VIX {_q(quotes, 'vix', 'last'):.1f}")
    if us:
        asof = (quotes.get("nasdaq") or {}).get("asof")
        lines.append("  美股收盤：" + "｜".join(us) + (f"（{asof}）" if asof else ""))
    other = [f"{QUOTES[k][1]} {_q(quotes, k):+.1f}%" for k in ("nq", "n225", "kospi") if _q(quotes, k) is not None]
    if other:
        lines.append("  " + "｜".join(other))
    lines.append("  " + ADVICE[ass["level"]])
    return "\n".join(lines)


def gather(now=None, fetch_one=None, futures_rows=None) -> tuple[dict | None, dict]:
    """抓夜盤與各報價；單項失敗只記錄、不影響其他。fetch_one、futures_rows 可注入（測試用）。"""
    if fetch_one is None:
        from .fetchers import macro
        fetch_one = macro.fetch_one
    if futures_rows is None:
        from .fetchers import taifex

        def futures_rows():
            return taifex.fetch("futures_daily")
    night = None
    try:
        night = tx_night(futures_rows(), now)
    except Exception as e:  # noqa: BLE001
        log.warning("台指期夜盤抓取失敗：%s", e)
    quotes = {}
    for key, (symbol, _name) in QUOTES.items():
        try:
            r = fetch_one(symbol)
        except Exception as e:  # noqa: BLE001
            log.warning("開盤前環境 %s 抓取失敗：%s", symbol, e)
            continue
        if r:
            quotes[key] = {"d1_pct": r.get("d1_pct"), "last": r.get("last"), "asof": r.get("asof")}
    return night, quotes


def parts(now=None, fetch_one=None, futures_rows=None) -> tuple[dict | None, dict, dict] | None:
    """（夜盤, 各報價, 燈號評估）；功能關閉或整個流程出錯回傳 None。"""
    cfg = settings()
    if not cfg["enabled"]:
        return None
    try:
        night, quotes = gather(now, fetch_one, futures_rows)
    except Exception as e:  # noqa: BLE001
        log.warning("開盤前環境失敗：%s", e)
        return None
    d = {"night": None if (not night or night["stale"]) else night["pct"],
         **{k: _q(quotes, k) for k in ("nasdaq", "sox", "nq", "n225", "kospi")}, "vix": _q(quotes, "vix", "last")}
    return night, quotes, assess(d, cfg)


def build(now=None, fetch_one=None, futures_rows=None) -> tuple[str, str]:
    """（推播文字, 燈號 green／yellow／red）。關閉、或完全抓不到資料時回傳空燈號，不影響盤前計劃。"""
    p = parts(now, fetch_one, futures_rows)
    if p is None:
        return "", ""
    night, quotes, ass = p
    if not night and not quotes:
        return "🌙 開盤前環境：夜盤與國際行情都沒抓到（來源連線失敗），今天請自行查看夜盤與那指。", ""
    return text(night, quotes, ass), ass["level"]


def short_line(night: dict | None, quotes: dict, ass: dict, taiex_pct: float | None = None) -> str:
    """一行版（放在選股機器人 09:15 第一則）：燈號＋夜盤、美股、亞股漲跌；有加權即時漲跌就一起列，對照開盤有沒有照夜盤走。"""
    segs = []
    if night and not night["stale"]:
        segs.append(f"夜盤 {night['pct']:+.1f}%")
    segs += [f"{QUOTES[k][1]} {_q(quotes, k):+.1f}%" for k in ("nasdaq", "sox", "n225", "kospi") if _q(quotes, k) is not None]
    if not segs:
        return ""
    if taiex_pct is not None:
        segs.append(f"加權現在 {taiex_pct:+.2f}%")
    return f"🌙 隔夜環境 {ICON[ass['level']]}｜" + "｜".join(segs)
