#!/usr/bin/env python3
"""股癌歷史逐字稿閱讀工具（只在研究員的容器裡用；私鑰不在 repo）。
用法：
  gy.py list                      已處理的集數與摘要重點
  gy.py show N [--full]           印出第 N 集的市場話題段（--full 印整集）
  gy.py brief N [N2 ...] [--cap 14000]  精簡版：略過業配與閒聊，市場話題段全印、問答段只印談到投資的段落
  gy.py grep 關鍵字 [--eps a-b] [--ctx 160] [--max 40]   跨集搜尋，印集數、時間點與前後文
  gy.py terms [--by year]         方法論詞的出現次數（依年份）
  gy.py tags [--by year] [--top 30]  提到的公司／主題（依年份）
"""
import json, re, sys, glob, os, collections, argparse

WT = "/tmp/claude-0/wt_r"
PRIV = "/root/.claude/podcast_archive_priv.pem"
if not os.path.exists(PRIV):
    PRIV = "/tmp/claude-0/-home-user-TW-strong/ec09799e-0f59-5bea-8762-1d7df15a140c/scratchpad/podcast_archive_priv.pem"
sys.path.insert(0, WT)
from market_intel.fetchers import podcast as pc  # noqa: E402

SHOW = "股癌"
TERMS = pc.LOGIC_TERMS + ["股票", "股價", "公司", "產業", "市場", "投資", "操作", "持股", "漲", "跌"]
CACHE = "/tmp/claude-0/gy_cache"
os.makedirs(CACHE, exist_ok=True)


def digests():
    out = {}
    for f in sorted(glob.glob(f"{WT}/research/podcasts/{SHOW}/EP*.json")):
        d = json.load(open(f, encoding="utf-8"))
        if d.get("ep") is not None and not d.get("skipped"):
            out[d["ep"]] = d
    return out


def text(n):
    cp = f"{CACHE}/EP{n:04d}.txt"
    if os.path.exists(cp):
        return open(cp, encoding="utf-8").read()
    bp = f"{WT}/state/podcast_archive/{SHOW}/EP{n:04d}.bin"
    t = pc.decrypt_text(open(bp, "rb").read(), open(PRIV, "rb").read())
    open(cp, "w", encoding="utf-8").write(t)
    return t


def tsec(s):
    p = [int(x) for x in s.split(":")]
    return p[0] * 60 + p[1] if len(p) == 2 else p[0] * 3600 + p[1] * 60 + p[2]


def market_part(n, d):
    lines = text(n).splitlines()
    head, body = [l for l in lines if not l.startswith("[")], [l for l in lines if l.startswith("[")]
    ms, qs = d.get("market_start"), d.get("qa_start")
    out = []
    for l in body:
        m = re.match(r"\[([\d:]+)\]", l)
        t = tsec(m.group(1)) if m else 0
        if ms and t < tsec(ms):
            continue
        if qs and t >= tsec(qs):
            continue
        out.append(l)
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd")
    ap.add_argument("arg", nargs="?")
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--eps")
    ap.add_argument("--ctx", type=int, default=160)
    ap.add_argument("--max", type=int, default=40)
    ap.add_argument("--by", default="year")
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--cap", type=int, default=14000)
    ap.add_argument("more", nargs="*")
    a = ap.parse_args()
    ds = digests()
    if a.cmd == "list":
        for n, d in sorted(ds.items()):
            top = ", ".join(f"{k}{v}" for k, v in list(d["tags"].items())[:5])
            print(f"EP{n:<4} {d['published'][:10]} {d['duration_min']:>3}分 市場{d['market_start'] or '?':>6} 問答{d['qa_start'] or '?':>6} {top}")
    elif a.cmd == "show":
        n = int(a.arg)
        d = ds.get(n) or {}
        print(text(n) if a.full or not d.get("market_start") else f"# EP{n} 市場話題段（{d.get('market_start')}～{d.get('qa_start') or '結尾'}）\n" + market_part(n, d))
    elif a.cmd == "brief":
        cap = a.cap
        for n in [int(a.arg)] + [int(x) for x in a.more]:
            d = ds.get(n) or {}
            lines = [l for l in text(n).splitlines() if l.startswith("[")]
            head = f"===== EP{n} {d.get('published','')[:10]} {d.get('title','')[:50]}（{d.get('duration_min')}分）"
            print(head)
            ms, qs = d.get("market_start"), d.get("qa_start")
            out = []
            for l in lines:
                m = re.match(r"\[([\d:]+)\] (.*)", l)
                t = tsec(m.group(1))
                body = m.group(2)
                if d.get("ad_paras") and re.search(r"贊助|優惠碼|折扣碼|業配", body[:300]):
                    continue
                if ms and t < tsec(ms):
                    continue            # 閒聊開場
                if qs and t >= tsec(qs) and not any(w in body for w in TERMS):
                    continue            # 問答裡與投資無關的段落
                out.append(l)
            txt = "\n".join(out)
            print(txt[:cap] + ("…（截斷）" if len(txt) > cap else ""))
    elif a.cmd == "grep":
        lo, hi = (int(x) for x in a.eps.split("-")) if a.eps else (0, 10**6)
        pat = re.compile(a.arg)
        shown = 0
        for n in sorted(ds):
            if not lo <= n <= hi:
                continue
            for l in text(n).splitlines():
                m = re.match(r"\[([\d:]+)\] (.*)", l)
                if not m:
                    continue
                for mm in pat.finditer(m.group(2)):
                    s, e = max(0, mm.start() - a.ctx), min(len(m.group(2)), mm.end() + a.ctx)
                    print(f"EP{n} [{m.group(1)}] …{m.group(2)[s:e]}…")
                    shown += 1
                    break
                if shown >= a.max:
                    return
    elif a.cmd == "terms":
        agg = collections.defaultdict(collections.Counter)
        cnt = collections.Counter()
        for n, d in ds.items():
            y = d["published"][:4]
            cnt[y] += 1
            agg[y].update(d["terms"])
        for y in sorted(agg):
            print(y, f"({cnt[y]}集)", " ".join(f"{k}{v}" for k, v in agg[y].most_common(25)))
    elif a.cmd == "tags":
        agg = collections.defaultdict(collections.Counter)
        for n, d in ds.items():
            agg[d["published"][:4]].update(d["tags"])
        for y in sorted(agg):
            print(y, " ".join(f"{k}{v}" for k, v in agg[y].most_common(a.top)))


main()
