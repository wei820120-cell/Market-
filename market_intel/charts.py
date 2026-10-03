"""推播用圖片：供應鏈地圖、目標價／股票期貨表、個股走勢小圖（matplotlib，輸出 PNG）。

手機直式閱讀：寬約 1100 像素，高度依內容調整。
中文字型：自動找 Noto Sans CJK／思源黑體等，GitHub Actions 由 workflow 安裝 fonts-noto-cjk。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402
from matplotlib.patches import FancyBboxPatch  # noqa: E402

log = logging.getLogger(__name__)

FONT_CANDIDATES = ["Noto Sans CJK TC", "Noto Sans CJK JP", "Noto Sans CJK SC", "Noto Sans TC", "Source Han Sans TW",
                   "Microsoft JhengHei", "PingFang TC", "Heiti TC", "WenQuanYi Zen Hei"]
COLORS = {"受惠": "#d9f2e3", "受傷": "#fbdcdc", "中性": "#eceff3"}
EDGE = {"受惠": "#1e8e4e", "受傷": "#c0392b", "中性": "#8a94a6"}
UP, DOWN = "#d0312d", "#1e8e4e"  # 台股：漲紅跌綠
DPI = 130
WIDTH = 8.6  # 英寸，約 1120 像素

_font_ready = False


def setup_font() -> str | None:
    global _font_ready
    names = {f.name for f in font_manager.fontManager.ttflist}
    for name in FONT_CANDIDATES:
        if name in names:
            plt.rcParams["font.family"] = [name, "DejaVu Sans"]
            plt.rcParams["axes.unicode_minus"] = False
            _font_ready = True
            return name
    if not _font_ready:
        log.warning("找不到中文字型，圖片中文會變方塊；請安裝 fonts-noto-cjk")
    return None


def _wrap(text: str, width: int, lines: int = 2) -> str:
    """依字寬換行（中文 1、英數 0.55），不拆英文單字。"""
    words = re.findall(r"[A-Za-z0-9.+\-/%]+|\s+|.", str(text or ""))
    rows, cur, w = [], "", 0.0
    for tok in words:
        tw = sum(0.55 if ord(ch) < 128 else 1 for ch in tok)
        if w + tw > width and cur:
            rows.append(cur.rstrip())
            cur, w = "", 0.0
            if tok.isspace():
                continue
        cur += tok
        w += tw
    if cur.strip():
        rows.append(cur.rstrip())
    if len(rows) > lines:
        rows = rows[:lines]
        rows[-1] = rows[-1][:-1] + "…"
    return "\n".join(rows)


def _clean(text: str) -> str:
    """去掉字型沒有的表情符號（🔥🔬📊 等），避免圖片出現方塊。"""
    return re.sub(r"[\U00010000-\U0010FFFF\ufe0f]", "", str(text or "")).strip()


def _pct_color(v) -> str:
    if v is None:
        return "#333333"
    return UP if v > 0 else (DOWN if v < 0 else "#333333")


def _fmt(v, nd: int = 2, sign: bool = False) -> str:
    if v is None:
        return "—"
    s = f"{v:+.{nd}f}" if sign else f"{v:,.{nd}f}"
    return s


def supply_chain_png(path: Path, title: str, subtitle: str, layers: list[dict], stocks: dict[str, dict]) -> Path:
    """供應鏈地圖：由上游到下游一層一層往下排，每檔股票一張卡（綠＝受惠、紅＝受傷、灰＝中性）。

    layers：[{"layer", "global_players", "tw_stocks": [{"code", "name", "role", "impact"}]}]
    stocks：{代號: 卡片資料}（收盤、漲跌、目標價、股期），用來在卡片上標目標價與股票期貨。
    """
    setup_font()
    cols = 3
    card_h, head_h, gap = 1.25, 0.75, 0.35
    rows_per_layer = [max(1, -(-len(l["tw_stocks"]) // cols)) for l in layers]
    height = 1.6 + sum(head_h + r * card_h + gap for r in rows_per_layer) + 0.4
    fig = plt.figure(figsize=(WIDTH, height), dpi=DPI)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(0, cols)
    ax.set_ylim(height, 0)
    ax.axis("off")
    fig.patch.set_facecolor("white")

    ax.text(0.12, 0.45, _clean(title), fontsize=17, weight="bold", va="center")
    ax.text(0.12, 1.0, _wrap(_clean(subtitle), 46, 2), fontsize=9.5, color="#444", va="center")
    y = 1.6
    for li, (layer, nrows) in enumerate(zip(layers, rows_per_layer)):
        ax.add_patch(FancyBboxPatch((0.08, y + 0.05), cols - 0.16, head_h - 0.15, boxstyle="round,pad=0.02",
                                    fc="#24364f", ec="none"))
        ax.text(0.18, y + 0.3, f"{'↓ ' if li else ''}{layer.get('layer', '')}", fontsize=12, color="white",
                weight="bold", va="center")
        players = "、".join(layer.get("global_players") or [])
        if players:
            ax.text(cols - 0.15, y + 0.3, _wrap("全球：" + players, 34, 1), fontsize=8.5, color="#cfd8e3",
                    va="center", ha="right")
        y += head_h
        for i, s in enumerate(layer["tw_stocks"]):
            r, c = divmod(i, cols)
            x0, y0 = c + 0.08, y + r * card_h + 0.06
            imp = s.get("impact", "中性")
            ax.add_patch(FancyBboxPatch((x0, y0), 0.84, card_h - 0.16, boxstyle="round,pad=0.02",
                                        fc=COLORS.get(imp, COLORS["中性"]), ec=EDGE.get(imp, EDGE["中性"]), lw=1.2))
            d = stocks.get(s["code"], {})
            mark = {"受惠": "▲", "受傷": "▼"}.get(imp, "●")
            ax.text(x0 + 0.05, y0 + 0.2, f"{mark} {s['code']} {s.get('name', '')}", fontsize=10.5, weight="bold",
                    color=EDGE.get(imp, "#333"), va="center")
            price = d.get("close")
            if price is not None:
                ax.text(x0 + 0.8, y0 + 0.2, f"{price:,.1f} {_fmt(d.get('pct1'), 1, True)}%", fontsize=8.5,
                        color=_pct_color(d.get("pct1")), va="center", ha="right")
            ax.text(x0 + 0.05, y0 + 0.5, _wrap(s.get("role", ""), 15, 2), fontsize=8, color="#333", va="center",
                    linespacing=1.2)
            fut = d.get("fut_short") or "無股期"
            tgt = d.get("target")
            tline = f"目標 {tgt:,.0f}（{d.get('target_pct', 0):+.0f}%）" if tgt else ""
            ax.text(x0 + 0.05, y0 + 0.88, fut, fontsize=8, color="#7a3e00" if fut != "無股期" else "#999",
                    weight="bold" if fut != "無股期" else "normal", va="center")
            if tline:
                ax.text(x0 + 0.8, y0 + 0.88, tline, fontsize=8, color="#24364f", va="center", ha="right")
        y += nrows * card_h + gap
    ax.text(0.12, height - 0.2, "綠＝受惠　紅＝受傷　灰＝中性｜目標＝波段保守目標｜僅供參考", fontsize=8, color="#888",
            va="center")
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=DPI)
    plt.close(fig)
    return path


def table_png(path: Path, title: str, header: list[str], rows: list[list], col_widths: list[float],
              note: str = "", colorize: dict[int, int] | None = None, width: float = WIDTH) -> Path:
    """一般表格圖。colorize：{要上色的欄: 依哪一欄的數字正負上色}。"""
    setup_font()
    colorize = colorize or {}
    row_h = 0.42
    height = 1.0 + row_h * (len(rows) + 1) + (0.5 if note else 0.2)
    fig = plt.figure(figsize=(width, height), dpi=DPI)
    ax = fig.add_axes([0, 0, 1, 1])
    total = sum(col_widths)
    ax.set_xlim(0, total)
    ax.set_ylim(height, 0)
    ax.axis("off")
    ax.text(0.1, 0.45, _clean(title), fontsize=14, weight="bold", va="center")
    y = 0.9
    ax.add_patch(plt.Rectangle((0, y), total, row_h, fc="#24364f", ec="none"))
    x = 0.0
    for h, w in zip(header, col_widths):
        ax.text(x + w / 2, y + row_h / 2, h, fontsize=8.5, color="white", ha="center", va="center", weight="bold")
        x += w
    for i, row in enumerate(rows):
        y += row_h
        if i % 2:
            ax.add_patch(plt.Rectangle((0, y), total, row_h, fc="#f3f5f8", ec="none"))
        x = 0.0
        for j, (v, w) in enumerate(zip(row, col_widths)):
            color = "#222"
            if j in colorize:
                ref = row[colorize[j]]
                num = ref if isinstance(ref, (int, float)) else None
                if num is None and isinstance(ref, str):
                    try:
                        num = float(ref.replace("%", "").replace("+", "").split("（")[0].split("(")[0])
                    except ValueError:
                        num = None
                color = _pct_color(num)
            ax.text(x + w / 2, y + row_h / 2, "—" if v is None else str(v), fontsize=8.5, color=color,
                    ha="center", va="center", weight="bold" if j == 0 else "normal")
            x += w
    if note:
        ax.text(0.1, height - 0.28, _wrap(note, int(70 * width / WIDTH), 2), fontsize=7.5, color="#777", va="center")
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=DPI)
    plt.close(fig)
    return path


def price_grid_png(path: Path, title: str, items: list[dict], days: int = 120) -> Path:
    """個股走勢小圖：收盤價＋20 日均線，標出保守／積極目標與停損。

    items：[{"label": "1815 富喬", "df": 日K, "target", "aggressive", "stop"}]，最多 12 檔。
    """
    setup_font()
    items = items[:12]
    cols = 3
    nrows = max(1, -(-len(items) // cols))
    fig, axes = plt.subplots(nrows, cols, figsize=(WIDTH, 0.6 + 2.3 * nrows), dpi=DPI, squeeze=False)
    fig.suptitle(_clean(title), fontsize=13, weight="bold", x=0.02, ha="left")
    for ax in axes.flat:
        ax.axis("off")
    for ax, it in zip(axes.flat, items):
        ax.axis("on")
        df = it["df"].tail(days)
        close = df["close"]
        ax.plot(range(len(close)), close.values, color="#24364f", lw=1.2)
        ax.plot(range(len(close)), close.rolling(20).mean().values, color="#e67e22", lw=0.9)
        for key, color, ls in (("target", UP, "--"), ("aggressive", UP, ":"), ("stop", DOWN, "--")):
            v = it.get(key)
            if v:
                ax.axhline(v, color=color, ls=ls, lw=0.9)
                ax.text(len(close) - 1, v, f"{v:,.0f}", fontsize=6.5, color=color, va="bottom", ha="right")
        last = float(close.iloc[-1])
        pct = float(close.iloc[-1] / close.iloc[-2] - 1) * 100 if len(close) > 1 else 0
        ax.set_title(f"{it['label']}  {last:,.1f} ({pct:+.1f}%)", fontsize=8.5, color=_pct_color(pct), loc="left")
        ax.tick_params(labelsize=6)
        ax.set_xticks([])
        for sp in ("top", "right"):
            ax.spines[sp].set_visible(False)
    fig.text(0.02, 0.005, "藍＝收盤　橘＝20日均線　紅虛線＝保守目標　紅點線＝積極目標　綠虛線＝停損", fontsize=7, color="#777")
    fig.tight_layout(rect=(0, 0.02, 1, 0.97))
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=DPI)
    plt.close(fig)
    return path
