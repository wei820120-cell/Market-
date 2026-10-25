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
ARCHIVE_DIR = ROOT / "state" / "podcast_archive"      # 歷史集數的逐字稿（加密，公開 repo 不放原文）
DIGEST_DIR = ROOT / "research" / "podcasts"           # 歷史集數的摘要（只有計數與時間點，沒有原文）
PUBKEY_PATH = ROOT / "config" / "podcast_archive_pub.pem"

# 方法論相關的詞：每集數一數出現次數，之後挑要細讀的集數用
LOGIC_TERMS = ["停損", "停利", "部位", "資金控管", "槓桿", "融資", "籌碼", "估值", "本益比", "題材", "缺貨", "漲價", "庫存", "財報", "法說",
               "Earnings", "指數", "回檔", "反彈", "複盤", "FOMO", "紀律", "風險", "資金效益", "Bottleneck", "Sell the news", "利多出盡",
               "降息", "升息", "通膨", "流動性", "殖利率", "CapEx", "泡沫", "護城河", "高原", "趨勢", "均線", "加碼", "減碼", "放空",
               "現金", "選擇權", "期貨", "ETF", "心態", "貪婪", "恐懼", "停損點", "基本面", "技術面", "產業", "供應鏈", "報價", "毛利"]


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


def episode_no(title: str) -> int | None:
    """標題裡的集數：「EP708 | 🦷」→ 708。"""
    m = re.search(r"EP\s*(\d+)", title or "", re.I)
    return int(m.group(1)) if m else None


def episode_name(ep: Episode) -> str:
    """歷史檔案用的檔名：EP0708；沒有集數的用發布日期＋key。"""
    n = episode_no(ep.title)
    return f"EP{n:04d}" if n is not None else f"{ep.published[:10]}_{ep.key}"


def digest_path(ep: Episode) -> Path:
    return DIGEST_DIR / ep.show / f"{episode_name(ep)}.json"


def archive_path(ep: Episode) -> Path:
    return ARCHIVE_DIR / ep.show / f"{episode_name(ep)}.bin"


def channel_info(show: dict) -> dict:
    """節目層級的資訊（標題、連結、簡介裡的網址）：用來找主持人的其他管道。"""
    root = ET.fromstring(net.get(resolve_feed(show), timeout=30).text)
    ch = root.find("channel")
    if ch is None:
        return {}
    desc = re.sub(r"<[^>]+>", " ", (ch.findtext("description") or "") + " " + (ch.findtext(f"{ITUNES}summary") or ""))
    return {"title": (ch.findtext("title") or "").strip(), "link": (ch.findtext("link") or "").strip(),
            "description": re.sub(r"\s+", " ", desc).strip()[:800],
            "urls": sorted(set(re.findall(r"https?://[^\s<>\"')]+", ET.tostring(ch, encoding="unicode"))))[:30]}


def encrypt_text(text: str, pubkey_pem: bytes | None = None) -> bytes:
    """RSA-OAEP 包 AES-GCM 金鑰；公開 repo 裡只放這種加密檔，私鑰不在 repo。沒有公鑰就拒絕存檔。"""
    import zlib
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    pem = pubkey_pem if pubkey_pem is not None else PUBKEY_PATH.read_bytes()
    pub = serialization.load_pem_public_key(pem)
    key, nonce = AESGCM.generate_key(256), os.urandom(12)
    body = AESGCM(key).encrypt(nonce, zlib.compress(text.encode("utf-8"), 9), b"GY1")
    wrapped = pub.encrypt(key, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
    return b"GY1" + len(wrapped).to_bytes(2, "big") + wrapped + nonce + body


def decrypt_text(blob: bytes, privkey_pem: bytes) -> str:
    import zlib
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if blob[:3] != b"GY1":
        raise ValueError("不是逐字稿加密檔")
    n = int.from_bytes(blob[3:5], "big")
    wrapped, nonce, body = blob[5:5 + n], blob[5 + n:17 + n], blob[17 + n:]
    priv = serialization.load_pem_private_key(privkey_pem, password=None)
    key = priv.decrypt(wrapped, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
    return zlib.decompress(AESGCM(key).decrypt(nonce, body, b"GY1")).decode("utf-8")


def transcript_text(ep: Episode, paras: list[tuple[float, str]]) -> str:
    head = [f"節目：{ep.show}", f"標題：{ep.title}", f"發布：{ep.published}", f"連結：{ep.link or ep.audio_url}",
            f"長度：{ep.duration_s // 60} 分鐘", "（語音轉文字，可能有同音錯字，公司名與數字請對照原節目）", ""]
    return "\n".join(head + [f"[{mmss(t)}] {text}" for t, text in paras]) + "\n"


def build_digest(ep: Episode, paras: list[tuple[float, str]], ads: list[bool], tags: list[list[str]], model: str = "") -> dict:
    """一集的摘要（只有計數與時間點，不含原文）：市場話題與問答從哪裡開始、提到的公司與主題、方法論詞的出現次數。"""
    market_start = qa_start = None
    for i, (t, text) in enumerate(paras):
        if market_start is None and re.search(r"市場話題|進入市場|市場的部分|來聊市場", text):
            market_start = t
        elif market_start is not None and qa_start is None and re.search(r"QQ|Q&A|QA的部分|聽眾(的)?問答|進入Q", text):
            qa_start = t
    body = [text for (t, text), ad in zip(paras, ads) if not ad]
    joined = "\n".join(body)
    terms = {w: joined.count(w) for w in LOGIC_TERMS if joined.count(w)}
    tag_counts: dict[str, int] = {}
    for tl, ad in zip(tags, ads):
        if not ad:
            for tg in tl:
                tag_counts[tg] = tag_counts.get(tg, 0) + 1
    top = dict(sorted(tag_counts.items(), key=lambda kv: -kv[1])[:60])
    return {"show": ep.show, "ep": episode_no(ep.title), "title": ep.title, "published": ep.published, "key": ep.key,
            "duration_min": ep.duration_s // 60, "paras": len(paras), "ad_paras": sum(ads), "chars": len(joined),
            "market_start": mmss(market_start) if market_start is not None else "", "qa_start": mmss(qa_start) if qa_start is not None else "",
            "tags": top, "terms": terms, "model": model, "transcribed_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


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
