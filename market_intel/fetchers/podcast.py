"""Podcast：節目 RSS → 新集數 → 下載音檔 → 語音轉文字（faster-whisper，CPU）→ 段落。

要「聽」就得先轉成文字：轉文字很吃時間（一集 1～2 小時、CPU 約幾十分鐘），所以放在獨立的 podcast.yml，
不在每 2 分鐘的監看迴圈裡做。faster-whisper 與 OpenCC 不在 requirements.txt，只有 podcast.yml 安裝；
沒裝就略過、不影響其他監看。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

from .. import inbox, net
from ..utils import ROOT

log = logging.getLogger(__name__)

ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"
SEEN_PATH = ROOT / "state" / "podcasts_seen.json"
TRANSCRIPT_DIR = ROOT / "state" / "podcasts"
INBOX_DIR = ROOT / "state" / "podcast_inbox"


@dataclass
class Episode:
    show: str
    guid: str
    title: str
    audio_url: str
    link: str = ""
    published: str = ""      # ISO（UTC）
    duration_s: int = 0
    size: int = 0
    summary: str = ""

    @property
    def key(self) -> str:
        return hashlib.md5((self.guid or self.audio_url).encode()).hexdigest()[:12]


@dataclass
class Segment:
    start: float
    end: float
    text: str


def _duration(text: str | None) -> int:
    """itunes:duration：秒數、mm:ss 或 hh:mm:ss。"""
    if not text:
        return 0
    parts = text.strip().split(":")
    try:
        nums = [int(float(p)) for p in parts]
    except ValueError:
        return 0
    sec = 0
    for n in nums:
        sec = sec * 60 + n
    return sec


def parse_feed(xml_text: str, show: str) -> list[Episode]:
    """節目 RSS → 集數（新的在前）。沒有音檔連結（enclosure）的項目略過。"""
    root = ET.fromstring(xml_text)
    out = []
    for it in root.iter("item"):
        enc = it.find("enclosure")
        if enc is None or not enc.get("url"):
            continue
        pub = it.findtext("pubDate") or ""
        try:
            published = parsedate_to_datetime(pub).astimezone(timezone.utc).isoformat(timespec="seconds")
        except (TypeError, ValueError):
            published = ""
        summary = re.sub(r"<[^>]+>", " ", it.findtext("description") or "")
        out.append(Episode(
            show=show, guid=(it.findtext("guid") or enc.get("url")).strip(), title=(it.findtext("title") or "").strip(),
            audio_url=enc.get("url").strip(), link=(it.findtext("link") or "").strip(), published=published,
            duration_s=_duration(it.findtext(f"{ITUNES}duration")), size=int(enc.get("length") or 0) if (enc.get("length") or "").isdigit() else 0,
            summary=re.sub(r"\s+", " ", summary).strip()[:600]))
    return sorted(out, key=lambda e: e.published, reverse=True)


def resolve_feed(show: dict) -> str:
    """節目設定 → RSS 網址：直接給 feed；或給 Apple Podcasts 節目編號 apple_id（網址最後 id 後面的數字）；
    或給 search（節目名稱關鍵字），用 iTunes 搜尋，取節目名稱包含 name 的第一個。"""
    if show.get("feed"):
        return show["feed"]
    if show.get("apple_id"):
        results = net.get_json(f"https://itunes.apple.com/lookup?id={show['apple_id']}&country=tw", timeout=20).get("results") or []
    else:
        term = show.get("search") or show["name"]
        found = net.get_json("https://itunes.apple.com/search", params={"media": "podcast", "entity": "podcast", "country": "tw", "limit": 8, "term": term},
                             timeout=20).get("results") or []
        results = [r for r in found if show["name"].lower() in (r.get("collectionName") or "").lower()]
    if not results or not results[0].get("feedUrl"):
        raise ValueError(f"iTunes 查不到 {show.get('name')} 的 RSS（apple_id={show.get('apple_id')}, search={show.get('search')}）")
    return results[0]["feedUrl"]


def fetch_episodes(show: dict) -> list[Episode]:
    return parse_feed(net.get(resolve_feed(show), timeout=30).text, show["name"])


def download(url: str, dest: Path, max_mb: int = 300) -> Path:
    """串流下載音檔（超過 max_mb 就放棄）。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    got = 0
    with net.session().get(url, stream=True, timeout=60, allow_redirects=True) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                got += len(chunk)
                if got > max_mb << 20:
                    raise ValueError(f"音檔超過 {max_mb} MB，略過")
                f.write(chunk)
    return dest


def to_traditional(text: str) -> str:
    """語音轉文字常輸出簡體；有 OpenCC 就轉成台灣用字，沒有就原樣。"""
    try:
        from opencc import OpenCC
    except ImportError:
        return text
    global _CC
    if "_CC" not in globals():
        _CC = OpenCC("s2twp")
    return _CC.convert(text)


def transcribe(path: Path, model_name: str = "small", max_minutes: int = 0, prompt: str = "", threads: int = 0,
               from_minutes: int = 0) -> list[Segment]:
    """faster-whisper（CPU、int8）。max_minutes>0 只轉前幾分鐘、from_minutes>0 從第幾分鐘開始（測試用）。"""
    from faster_whisper import WhisperModel

    model = WhisperModel(model_name, device="cpu", compute_type="int8", cpu_threads=threads or (os.cpu_count() or 4))
    kwargs = {"clip_timestamps": f"{from_minutes * 60},{max_minutes * 60}"} if from_minutes and max_minutes else {}
    segments, info = model.transcribe(str(path), language="zh", beam_size=1, vad_filter=True,
                                      initial_prompt=prompt or None, condition_on_previous_text=False, **kwargs)
    out = []
    for s in segments:
        if max_minutes and s.start > max_minutes * 60:
            break
        text = to_traditional(s.text.strip())
        if text:
            out.append(Segment(round(s.start, 1), round(s.end, 1), text))
    log.info("轉文字完成：%d 段，音檔長度 %.0f 分鐘", len(out), (getattr(info, "duration", 0) or 0) / 60)
    return out


def apply_fixes(text: str, fixes: list[dict]) -> str:
    """語音轉文字的固定同音錯字更正（設定檔 podcast_fixes）。每條 {from, to, when}：when 是正規表示式，段落裡要有它才更正
    （例如『新興』在談載板、良率的段落多半是『欣興』，但在談航運的段落就是新興航運）。"""
    for f in fixes or []:
        if f.get("when") and not re.search(f["when"], text):
            continue
        text = re.sub(f["from"], f["to"], text)
    return text


def paragraphs(segments: list[Segment], max_chars: int = 500) -> list[tuple[float, str]]:
    """把連續的短句併成段落（約 max_chars 字）：寫到 max_chars 之後，在句尾（。！？）斷開；
    語音轉文字常常整段沒有句號，所以超過 1.6 倍 max_chars 就算沒有句號也在句子邊界斷開。回傳（開始秒數, 文字）。"""
    out: list[tuple[float, str]] = []
    start, buf = 0.0, ""
    for s in segments:
        if not buf:
            start = s.start
        buf += s.text
        if len(buf) >= max_chars and (re.search(r"[。！？!?…]$", s.text) or len(buf) >= max_chars * 1.6):
            out.append((start, buf))
            buf = ""
    if buf:
        out.append((start, buf))
    return out


def mmss(sec: float) -> str:
    sec = int(sec)
    return f"{sec // 3600:d}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60:02d}:{sec % 60:02d}"


def transcript_path(ep: Episode) -> Path:
    day = (ep.published or "0000-00-00")[:10]
    return TRANSCRIPT_DIR / re.sub(r"[^\w\-]", "_", ep.show) / f"{day}_{ep.key}.txt"


def save_transcript(ep: Episode, paras: list[tuple[float, str]]) -> Path:
    path = transcript_path(ep)
    path.parent.mkdir(parents=True, exist_ok=True)
    head = [f"節目：{ep.show}", f"標題：{ep.title}", f"發布：{ep.published}", f"連結：{ep.link or ep.audio_url}",
            f"長度：{ep.duration_s // 60} 分鐘", "（語音轉文字，可能有同音錯字，公司名與數字請對照原節目）", ""]
    path.write_text("\n".join(head + [f"[{mmss(t)}] {text}" for t, text in paras]) + "\n", encoding="utf-8")
    return path


def prune_transcripts(keep_days: int = 14) -> int:
    """刪掉超過 keep_days 天的逐字稿（檔名開頭是發布日期）。"""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).strftime("%Y-%m-%d")
    n = 0
    if TRANSCRIPT_DIR.exists():
        for f in TRANSCRIPT_DIR.glob("*/*.txt"):
            if f.name[:10] < cutoff:
                f.unlink()
                n += 1
    return n


def load_seen() -> dict[str, list[str]]:
    if SEEN_PATH.exists():
        try:
            return json.loads(SEEN_PATH.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
    return {}


def save_seen(seen: dict[str, list[str]], keep: int = 300) -> None:
    SEEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    SEEN_PATH.write_text(json.dumps({k: v[-keep:] for k, v in seen.items()}, ensure_ascii=False) + "\n", encoding="utf-8")


def pick_new(show: str, episodes: list[Episode], seen: dict[str, list[str]], first: int = 1, max_age_days: int = 7,
             now: datetime | None = None) -> list[Episode]:
    """還沒處理過、而且不太舊的集數（新的在前）。太舊的與（第一次看這個節目時）最新 first 集以外的舊集數直接標記為已處理；
    要處理的集數由呼叫端處理完才標記（失敗下次會重試）。第一次看這個節目只處理最新 first 集，避免一口氣轉一大堆。"""
    ids = seen.get(show)
    known = set(ids or [])
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=max_age_days)

    def recent(e: Episode) -> bool:
        try:
            return datetime.fromisoformat(e.published) >= cutoff
        except ValueError:
            return True

    fresh = [e for e in episodes if e.key not in known and recent(e)]
    if ids is None:
        todo = fresh[:first]
        seen[show] = [e.key for e in episodes if e not in todo]
        return todo
    seen[show] = list(ids) + [e.key for e in episodes if e.key not in known and not recent(e)]
    return fresh
