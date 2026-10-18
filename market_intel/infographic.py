"""研究資訊圖：把一份題材研究（research/data/*.json）排成手機好讀的長圖（HTML → 無頭瀏覽器截圖）。

版面、文字、數字、表格、股票代號全部由 HTML 排（精確，不經過 AI 畫圖，不會有寫錯字或錯數字的風險）。
內容全部來自研究檔的既有欄位（one_line、evolution、spec_tables、concepts、layers、catalysts、risks、conclusion），不需要額外呼叫 AI 寫字。
每個題材依名稱固定選一組色系（藍、青綠、紫、橘、綠），不同題材看起來不一樣，同一題材三張圖一致。

瀏覽器：優先用環境變數 CHROME_BIN，其次系統的 google-chrome／chromium（GitHub Actions 的 ubuntu 映像檔內建 Chrome）。
"""
from __future__ import annotations

import glob
import html
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from string import Template

from .utils import now_tw

log = logging.getLogger(__name__)

WIDTH = 1080
FONT = "'Noto Sans CJK TC','Noto Sans TC','PingFang TC','Microsoft JhengHei',sans-serif"
IMPACT = {"受惠": ("#1e8a4c", "#e3f5ea"), "受傷": ("#c0392b", "#fbe8e5"), "中性": ("#667085", "#eef0f3")}
PALETTES = [  # (名稱, 深色, 主色, 給圖像模型的色彩描述)
    ("blue", "#0b2447", "#2563b8", "deep navy and electric blue with cyan highlights"),
    ("teal", "#073b3a", "#0f8f88", "deep teal and mint green with soft white highlights"),
    ("violet", "#261552", "#6a46c4", "deep indigo and violet with magenta highlights"),
    ("amber", "#43210a", "#c2601d", "dark brown and warm amber copper with golden highlights"),
    ("green", "#0e3a24", "#22974f", "deep forest green and fresh lime with white highlights"),
]

CSS = Template("""
:root{--dark:$dark;--mid:$mid;--s1:$s1;--s2:$s2;--s3:$s3;--s4:$s4;--s5:$s5;--s6:$s6;--ink:#1b2a41;--sub:#46607f}
*{box-sizing:border-box;margin:0;padding:0}
body{width:${w}px;background:#eef2f7;font-family:$font;color:var(--ink);font-size:22px;line-height:1.45}
.hero{position:relative;color:#fff;padding:42px 44px 52px;overflow:hidden;background:linear-gradient(135deg,var(--dark),var(--mid));background-size:cover;background-position:center}
.hero.compact{padding:26px 44px 36px}
.deco{position:absolute;inset:0;background-image:linear-gradient(rgba(255,255,255,.05) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,.05) 1px,transparent 1px);background-size:38px 38px}
.glow{position:absolute;right:-90px;top:-130px;width:460px;height:460px;border-radius:50%;background:radial-gradient(circle,rgba(255,255,255,.20),transparent 68%)}
.glow2{position:absolute;right:120px;bottom:-200px;width:380px;height:380px;border-radius:50%;background:radial-gradient(circle,rgba(255,255,255,.10),transparent 70%)}
.hero-in{position:relative;z-index:1;max-width:800px}
.kicker{font-size:18px;letter-spacing:5px;color:rgba(255,255,255,.78);font-weight:800}
.kicker i{display:inline-block;width:38px;height:4px;background:#fff;border-radius:2px;vertical-align:middle;margin-right:12px;opacity:.85}
.hero h1{font-size:52px;font-weight:900;line-height:1.16;margin-top:10px;text-shadow:0 2px 10px rgba(0,0,0,.25)}
.hero.compact h1{font-size:42px}
.sub{font-size:23px;color:#e6eefa;margin-top:14px;line-height:1.55}
.tags{margin-top:18px;display:flex;gap:10px;flex-wrap:wrap}
.tag{background:rgba(255,255,255,.16);border:1px solid rgba(255,255,255,.4);border-radius:999px;padding:3px 18px;font-size:19px;font-weight:600}
.stats{position:relative;z-index:2;display:flex;gap:12px;margin:-30px 28px 0}
.stat{flex:1;background:#fff;border-radius:14px;box-shadow:0 4px 14px rgba(15,40,80,.14);padding:10px 8px 9px;text-align:center;border-top:5px solid var(--mid)}
.stat b{display:block;font-size:40px;line-height:1.1;font-weight:900;color:var(--dark)}
.stat span{font-size:18px;color:var(--sub);font-weight:600}
.wrap{padding:26px 28px 24px}
.sec{margin-top:28px}
.sec:first-child{margin-top:0}
.sec-h{display:flex;align-items:center;gap:12px;margin-bottom:14px}
.num{width:46px;height:46px;border-radius:14px;background:linear-gradient(135deg,var(--mid),var(--dark));color:#fff;font-weight:900;font-size:25px;display:flex;align-items:center;justify-content:center;flex:none;box-shadow:0 3px 8px rgba(15,40,80,.25)}
.sec-h h2{font-size:32px;font-weight:900;color:var(--dark)}
.sec-h .hint{font-size:18px;color:#667085;margin-left:auto}
.grid{display:grid;gap:14px}
.g3{grid-template-columns:repeat(3,1fr)}
.g2{grid-template-columns:repeat(2,1fr)}
.card{background:#fff;border-radius:14px;box-shadow:0 2px 10px rgba(20,50,90,.10);overflow:hidden}
.card-h{color:#fff;padding:11px 16px;font-weight:800;font-size:23px;display:flex;align-items:center;gap:10px}
.chev{clip-path:polygon(0 0,calc(100% - 20px) 0,100% 50%,calc(100% - 20px) 100%,0 100%);padding-right:30px;min-height:68px;line-height:1.25}
.card-h .n{background:rgba(255,255,255,.28);border-radius:50%;width:34px;height:34px;display:flex;align-items:center;justify-content:center;font-size:20px;flex:none}
.card-b{padding:12px 16px 14px}
.lab{display:inline-block;font-size:17px;font-weight:700;color:#fff;background:var(--dark);border-radius:6px;padding:1px 10px;margin:6px 0 4px}
.lab.app{background:#2f7d5b}
.card-b p{font-size:20px;color:#243b5a}
.key{color:#a3441a;font-weight:800;margin-top:4px}
ul{list-style:none}
li{position:relative;padding-left:20px;font-size:20px;color:#243b5a;margin:5px 0}
li:before{content:"";position:absolute;left:3px;top:.62em;width:9px;height:9px;border-radius:3px;background:var(--mid);transform:rotate(45deg)}
table{width:100%;border-collapse:collapse;background:#fff;border-radius:12px;overflow:hidden;box-shadow:0 2px 10px rgba(20,50,90,.10)}
th{background:var(--dark);color:#fff;font-size:19px;padding:10px 8px;text-align:center}
td{font-size:19px;padding:10px 8px;text-align:center;border-top:1px solid #e3eaf3}
td:first-child{font-weight:800;background:#f0f5fb;color:var(--dark)}
tr:nth-child(even) td{background:#f8fafd}
tr:nth-child(even) td:first-child{background:#e8f0f9}
.note{font-size:17px;color:#667085;margin-top:6px}
.panel{border-radius:14px;padding:14px 18px 12px}
.panel h3{font-size:26px;font-weight:900;margin-bottom:6px}
.pos{background:#e8f6ee;border:1px solid #bfe3cd}.pos h3{color:#1e8a4c}.pos li:before{background:#1e8a4c}
.neg{background:#fdeceb;border:1px solid #f2c4bf}.neg h3{color:#c0392b}.neg li:before{background:#c0392b}
.concl{background:#fff;border:2px solid var(--mid);border-radius:14px;padding:16px 20px;box-shadow:0 3px 12px rgba(20,50,90,.10)}
.concl h3{font-size:29px;font-weight:900;color:var(--dark);margin-bottom:8px}
.concl li{padding-left:36px;font-size:21px}
.concl li:before{content:"✓";background:none;width:auto;height:auto;color:#1e8a4c;font-weight:900;font-size:23px;left:6px;top:0;transform:none}
.flow{position:relative;padding-left:70px}
.flow:before{content:"";position:absolute;left:26px;top:22px;bottom:30px;width:5px;border-radius:3px;background:linear-gradient(var(--s1),var(--s6))}
.node{position:relative;background:#fff;border-radius:14px;box-shadow:0 2px 10px rgba(20,50,90,.10);margin-bottom:14px;padding:12px 16px 13px}
.node:last-child{margin-bottom:0}
.dot{position:absolute;left:-66px;top:10px;width:48px;height:48px;border-radius:50%;color:#fff;font-weight:900;font-size:25px;display:flex;align-items:center;justify-content:center;border:4px solid #eef2f7;box-shadow:0 2px 6px rgba(15,40,80,.25)}
.node h4{font-size:24px;font-weight:900;color:var(--dark)}
.node .desc{font-size:18.5px;color:var(--sub);margin-top:3px}
.chips{display:flex;flex-wrap:wrap;gap:7px;margin-top:9px}
.chip{border-radius:8px;padding:3px 11px;font-size:19px;font-weight:700}
.chip small{font-weight:500;font-size:15.5px;margin-left:6px;opacity:.85}
.foot{padding:12px 40px 24px;font-size:17px;color:#667085;text-align:center}
""")


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def clip(s, n: int) -> str:
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s if len(s) <= n else s[:n - 1] + "…"


def palette_for(topic: str) -> tuple[str, str, str, str]:
    """依題材名稱固定選色系（同一題材永遠同一組，不同題材分散開）。"""
    return PALETTES[sum(ord(c) for c in str(topic)) % len(PALETTES)]


def _mix(a: str, b: str, t: float) -> str:
    """a 與 b 兩個 #rrggbb 依比例 t 混合（t=0 是 a、t=1 是 b）。"""
    ca = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    cb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(ca, cb))


def _steps(pal: tuple) -> list[str]:
    """由淺到深 6 階的主色（時間軸、編號圓點、世代卡片標題用）。"""
    _, dark, mid, _ = pal
    return [_mix(mid, "#ffffff", 0.18), mid, _mix(mid, dark, 0.25), _mix(mid, dark, 0.5), _mix(mid, dark, 0.75), dark]


def css(pal: tuple) -> str:
    s = _steps(pal)
    return CSS.substitute(w=WIDTH, font=FONT, dark=pal[1], mid=pal[2], s1=s[0], s2=s[1], s3=s[2], s4=s[3], s5=s[4], s6=s[5])


def _banner(d: dict, subtitle: str = "", tag: str = "", compact: bool = False) -> str:
    tags = "".join(f'<span class="tag">{esc(t)}</span>' for t in (d.get("status"), clip(d.get("horizon"), 22), tag) if t)
    sub = "" if compact else f'<div class="sub">{esc(clip(subtitle or d.get("one_line"), 150))}</div>'
    return (f'<div class="hero{" compact" if compact else ""}"><div class="deco"></div><div class="glow"></div><div class="glow2"></div>'
            f'<div class="hero-in"><div class="kicker"><i></i>題材研究</div><h1>{esc(d["topic"])}</h1>{sub}'
            f'<div class="tags">{tags}</div></div></div>')


def _stats(d: dict) -> str:
    layers = d.get("layers") or []
    gain = {s.get("code") for l in layers for s in (l.get("tw_stocks") or []) if s.get("impact") == "受惠"}
    items = [(len(layers), "供應鏈層級"), (len(gain), "受惠個股"), (len(d.get("catalysts") or []), "催化因素"),
             (len(d.get("risks") or []), "風險提醒")]
    cells = "".join(f'<div class="stat"><b>{n}</b><span>{esc(label)}</span></div>' for n, label in items if n)
    return f'<div class="stats">{cells}</div>' if cells else ""


def _sec(n: int | None, title: str, body: str, hint: str = "") -> str:
    badge = f'<div class="num">{n}</div>' if n else ""
    return (f'<div class="sec"><div class="sec-h">{badge}<h2>{esc(title)}</h2>'
            f'{f"<span class=hint>{esc(hint)}</span>" if hint else ""}</div>{body}</div>')


def _evolution(steps: list[dict], pal: tuple) -> str:
    colors = _steps(pal)
    cards = []
    for i, s in enumerate(steps[:6]):
        body = ""
        if s.get("composition"):
            body += f'<span class="lab">特性</span><p>{esc(clip(s["composition"], 60))}</p>'
        if s.get("spec"):
            body += f'<p class="key">{esc(clip(s["spec"], 60))}</p>'
        if s.get("application"):
            body += f'<span class="lab app">典型應用</span><p>{esc(clip(s["application"], 60))}</p>'
        cards.append(f'<div class="card"><div class="card-h chev" style="background:{colors[min(i, 5)]}"><span class="n">{i + 1}</span>'
                     f'{esc(clip(s.get("name"), 20))}</div><div class="card-b">{body}</div></div>')
    return f'<div class="grid g3">{"".join(cards)}</div>'


def _table(t: dict) -> str:
    cols, rows = t.get("columns") or [], t.get("rows") or []
    head = "".join(f"<th>{esc(clip(c, 16))}</th>" for c in cols)
    body = "".join("<tr>" + "".join(f"<td>{esc(clip(c, 34))}</td>" for c in r) + "</tr>" for r in rows[:9])
    note = f'<div class="note">註：{esc(clip(t.get("note"), 140))}</div>' if t.get("note") else ""
    return f"<table><tr>{head}</tr>{body}</table>{note}"


def _concepts(concepts: list[dict], pal: tuple) -> str:
    colors = _steps(pal)
    cards = []
    for i, c in enumerate(concepts[:4]):
        pts = "".join(f"<li>{esc(clip(p, 78))}</li>" for p in (c.get("points") or [])[:5])
        cards.append(f'<div class="card"><div class="card-h" style="background:{colors[(i * 2) % 6 if i < 3 else 5]}">'
                     f'<span class="n">{i + 1}</span>{esc(clip(c.get("title"), 22))}</div><div class="card-b"><ul>{pts}</ul></div></div>')
    return f'<div class="grid g2">{"".join(cards)}</div>'


def _layers(layers: list[dict], pal: tuple) -> str:
    colors = _steps(pal)
    out = []
    for i, l in enumerate(layers[:7]):
        chips = ""
        for s in (l.get("tw_stocks") or [])[:8]:
            fg, bg = IMPACT.get(s.get("impact"), IMPACT["中性"])
            chips += (f'<span class="chip" style="color:{fg};background:{bg}">{esc(s.get("code"))} {esc(s.get("name"))}'
                      f'<small>{esc(s.get("impact", ""))}</small></span>')
        out.append(f'<div class="node"><div class="dot" style="background:{colors[min(i, 5)]}">{i + 1}</div>'
                   f'<h4>{esc(clip(l.get("layer"), 40))}</h4><div class="desc">{esc(clip(l.get("description"), 120))}</div>'
                   f'<div class="chips">{chips}</div></div>')
    return f'<div class="flow">{"".join(out)}</div>'


def _panel(kind: str, title: str, items: list[str], n: int = 5, width: int = 70) -> str:
    li = "".join(f"<li>{esc(clip(x, width))}</li>" for x in items[:n])
    return f'<div class="panel {kind}"><h3>{esc(title)}</h3><ul>{li}</ul></div>'


def _page(inner: str, foot: str, pal: tuple) -> str:
    return (f'<!doctype html><html><head><meta charset="utf-8"><style>{css(pal)}</style></head><body>{inner}'
            f'<div class="foot">{esc(foot)}</div></body></html>')


def pages(d: dict) -> list[tuple[str, str]]:
    """一份研究 → 最多 3 張長圖的 HTML：（檔名, html）。內容不足的頁面略過。"""
    pal = palette_for(d["topic"])
    foot = f"資料來源 {len(d.get('sources') or [])} 篇｜AI 整理，僅供研究參考，非投資建議｜{now_tw():%Y-%m-%d}"
    out: list[tuple[str, str]] = []
    # 第 1 張：是什麼、怎麼演進、規格比較
    body, n = "", 0
    if d.get("evolution"):
        n += 1
        body += _sec(n, "世代演進", _evolution(d["evolution"], pal), "由左到右、由舊到新")
    for t in (d.get("spec_tables") or [])[:2]:
        n += 1
        body += _sec(n, clip(t.get("title"), 26) or "規格比較", _table(t))
    if body:
        out.append(("1_overview", _page(_banner(d) + _stats(d) + f'<div class="wrap">{body}</div>', foot, pal)))
    # 第 2 張：關鍵概念與供應鏈、誰受惠
    body, n = "", 0
    if d.get("concepts"):
        n += 1
        body += _sec(n, "關鍵概念", _concepts(d["concepts"], pal))
    if d.get("layers"):
        n += 1
        body += _sec(n, "供應鏈與受惠股", _layers(d["layers"], pal), "綠＝受惠　灰＝中性　紅＝受傷")
    if body:
        out.append(("2_chain", _page(_banner(d, tag="供應鏈", compact=True) + f'<div class="wrap">{body}</div>', foot, pal)))
    # 第 3 張：結論、催化與風險
    body, n = "", 0
    if d.get("conclusion"):
        li = "".join(f"<li>{esc(clip(x, 90))}</li>" for x in d["conclusion"][:5])
        body += f'<div class="sec"><div class="concl"><h3>關鍵結論</h3><ul>{li}</ul></div></div>'
    if d.get("catalysts") or d.get("risks"):
        body += ('<div class="sec"><div class="grid g2">'
                 + _panel("pos", "催化／優勢", d.get("catalysts") or [], 5, 62)
                 + _panel("neg", "風險／挑戰", d.get("risks") or [], 5, 62) + "</div></div>")
    if body:
        out.append(("3_conclusion", _page(_banner(d, tag="結論", compact=True) + f'<div class="wrap">{body}</div>', foot, pal)))
    return out


# ---------------------------------------------------------------- 截圖

def find_browser() -> str | None:
    env = os.environ.get("CHROME_BIN")
    if env and Path(env).exists():
        return env
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"):
        p = shutil.which(name)
        if p:
            return p
    for pat in ("/opt/pw-browsers/chromium-*/chrome-linux/chrome", "/opt/pw-browsers/chromium/chrome-linux/chrome"):
        hits = sorted(glob.glob(pat))
        if hits:
            return hits[-1]
    return None


def _run(browser: str, args: list[str], timeout: int = 90) -> subprocess.CompletedProcess:
    cmd = [browser, "--headless", "--disable-gpu", "--no-sandbox", "--hide-scrollbars", "--disable-dev-shm-usage", *args]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def render(html_text: str, out: Path, scale: float = 1.5) -> Path:
    """HTML → PNG（寬 WIDTH，高度依內容自動）。找不到瀏覽器或失敗就丟出 RuntimeError。"""
    browser = find_browser()
    if not browser:
        raise RuntimeError("找不到 Chrome／Chromium，無法產生資訊圖")
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        page = Path(td) / "p.html"
        # 先量高度：頁面腳本把內容高度寫進 <title>，用 --dump-dom 讀回來
        probe = html_text.replace("</body>", "<script>document.title=Math.ceil(document.body.getBoundingClientRect().bottom)+'|'+window.innerHeight</script></body>")
        page.write_text(probe, encoding="utf-8")
        r = _run(browser, ["--virtual-time-budget=4000", "--dump-dom", f"--window-size={WIDTH},2000", page.as_uri()])
        m = re.search(r"<title>(\d+)\|(\d+)</title>", r.stdout or "")
        # headless 視窗高度會比可視區高（工具列佔位），把差額補回去，截圖才不會被切掉底部
        height, chrome = (int(m.group(1)), 2000 - int(m.group(2))) if m else (3000, 0)
        page.write_text(html_text, encoding="utf-8")
        r = _run(browser, [f"--window-size={WIDTH},{height + max(chrome, 0)}", f"--force-device-scale-factor={scale}",
                           "--virtual-time-budget=4000", f"--screenshot={out}", page.as_uri()])
    if not out.exists():
        raise RuntimeError(f"資訊圖截圖失敗：{(r.stderr or '')[-200:]}")
    if chrome > 0:  # 截圖會多出視窗工具列那一段空白，裁掉
        from PIL import Image
        with Image.open(out) as im:
            im = im.crop((0, 0, im.width, min(im.height, round(height * scale))))
            im.save(out)
    return out


def render_all(d: dict, out_dir: Path) -> list[Path]:
    """一份研究 → 最多 3 張長圖 PNG（找不到 Chrome 或截圖失敗會丟出 RuntimeError，呼叫端自行退回舊版圖表）。"""
    out_dir = Path(out_dir)
    return [render(h, out_dir / f"{name}.png") for name, h in pages(d)]
