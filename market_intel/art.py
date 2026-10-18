"""研究資訊圖的主視覺：用 Gemini 圖像模型畫「不含文字」的插畫背景。

為什麼只畫背景：AI 生圖的中文字與數字常常寫錯（錯字、錯代號、錯百分比），財經圖不能冒這個險。
所以分工是：Gemini 只畫插畫（提示詞明確禁止文字、數字、商標），所有文字、數字、表格、股票代號都由 HTML 疊上去（infographic.py）。
就算 AI 畫出奇怪的東西，也只是背景不好看，不會誤導資料。

需要環境變數 GEMINI_API_KEY（或 GOOGLE_API_KEY）。沒有金鑰、模型出錯或被擋，一律回傳 None，資訊圖會自動改用純版面。
每個題材只畫一次，存到 research/art/<題材>.jpg 重複使用（想重畫加 --regen-art）。
模型名稱在 config/settings.yaml 的 infographic.art_model；不確定可用哪些模型時跑 `python -m market_intel infographic --list-models`。
"""
from __future__ import annotations

import base64
import io
import logging
import os
import time
from pathlib import Path

import requests

from . import config, research

log = logging.getLogger(__name__)

API = "https://generativelanguage.googleapis.com/v1beta"
ART_DIR = research.RESEARCH_DIR / "art"
RATIO = 3.0          # 主視覺寬高比（橫幅）
OUT_WIDTH = 1620     # 輸出寬度（頁面 1080 × 1.5 倍解析度）
DEFAULT_MODEL = "gemini-2.5-flash-image"


def settings() -> dict:
    st = config.settings().get("infographic") or {}
    return {"art": bool(st.get("art", True)), "art_model": str(st.get("art_model") or DEFAULT_MODEL)}


def api_key() -> str | None:
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or None


def art_path(topic: str) -> Path:
    return ART_DIR / f"{research.slug(topic)}.jpg"


def prompt(d: dict, palette_desc: str) -> str:
    """英文提示詞（英文效果比較穩）；內容用題材名稱、關鍵字與一句話摘要，嚴格禁止任何文字。"""
    kws = "、".join(str(k) for k in (d.get("keywords") or [])[:8])
    return (
        "Create a wide banner illustration (aspect ratio 3:1) for an industry research report on the Taiwan stock market.\n"
        f"Topic: {d.get('topic', '')}\nKeywords: {kws}\nContext: {str(d.get('one_line') or '')[:160]}\n"
        "Style: clean, modern, editorial flat-vector / isometric illustration of the key physical objects or industrial concept "
        "for this topic (for example chips, wafers, circuit boards, servers, optical fiber, factories, ships, materials — "
        "choose what fits), with subtle glowing lines and soft depth. "
        f"Color palette: {palette_desc}, on a dark background with a gentle gradient.\n"
        "Composition: the main subject sits on the RIGHT half; the LEFT half stays calm and dark so white text can be overlaid later.\n"
        "STRICT RULES: absolutely no text, no letters, no numbers, no Chinese characters, no logos, no brand names, "
        "no watermark, no charts or graphs, no human faces."
    )


def _image_bytes(resp: dict) -> bytes | None:
    for cand in resp.get("candidates") or []:
        for part in (cand.get("content") or {}).get("parts") or []:
            blob = part.get("inlineData") or part.get("inline_data")
            if blob and blob.get("data"):
                return base64.b64decode(blob["data"])
    return None


def _call(model: str, text: str, key: str, with_ratio: bool = True) -> requests.Response:
    body = {"contents": [{"parts": [{"text": text}]}], "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]}}
    if with_ratio:
        body["generationConfig"]["imageConfig"] = {"aspectRatio": "16:9"}
    return requests.post(f"{API}/models/{model}:generateContent", json=body, headers={"x-goog-api-key": key}, timeout=120)


def _finish(raw: bytes, out: Path) -> Path:
    """裁成 3:1 橫幅、縮到 OUT_WIDTH、存 JPEG。"""
    from PIL import Image
    with Image.open(io.BytesIO(raw)) as im:
        im = im.convert("RGB")
        w, h = im.size
        target_h = int(w / RATIO)
        if target_h < h:
            top = (h - target_h) // 2
            im = im.crop((0, top, w, top + target_h))
        im = im.resize((OUT_WIDTH, int(OUT_WIDTH / RATIO)))
        out.parent.mkdir(parents=True, exist_ok=True)
        im.save(out, "JPEG", quality=86)
    return out


def generate(d: dict, palette_desc: str, force: bool = False) -> Path | None:
    """這個題材的主視覺（有快取就直接用）；沒金鑰、關閉、或任何錯誤都回傳 None。"""
    cfg = settings()
    out = art_path(d["topic"])
    if out.exists() and not force:
        return out
    key = api_key()
    if not cfg["art"] or not key:
        return None
    text = prompt(d, palette_desc)
    try:
        for attempt in range(3):
            r = _call(cfg["art_model"], text, key)
            if r.status_code == 400 and "imageConfig" in (r.text or ""):  # 這個模型不支援指定比例：不指定再試
                r = _call(cfg["art_model"], text, key, with_ratio=False)
            if r.status_code in (429, 500, 502, 503) and attempt < 2:
                time.sleep(3 * (attempt + 1))
                continue
            break
        if r.status_code != 200:
            log.warning("主視覺生成失敗（HTTP %s，模型 %s）：%s", r.status_code, cfg["art_model"], (r.text or "")[:200])
            return None
        raw = _image_bytes(r.json())
        if not raw:
            log.warning("主視覺生成沒有回傳圖片（可能被安全機制擋下）：%s", (r.text or "")[:200])
            return None
        return _finish(raw, out)
    except Exception as e:  # noqa: BLE001 — 主視覺只是裝飾，任何錯誤都不能影響資訊圖
        log.warning("主視覺生成錯誤：%s", e)
        return None


def list_models() -> list[str]:
    """列出這把金鑰可用、名稱含 image 的模型（確認 art_model 要填什麼）。"""
    key = api_key()
    if not key:
        raise RuntimeError("沒有 GEMINI_API_KEY（或 GOOGLE_API_KEY）")
    r = requests.get(f"{API}/models", params={"pageSize": 200}, headers={"x-goog-api-key": key}, timeout=30)
    r.raise_for_status()
    out = []
    for m in r.json().get("models") or []:
        name = str(m.get("name", "")).replace("models/", "")
        if "image" in name or "imagen" in name:
            out.append(f"{name}｜{','.join(m.get('supportedGenerationMethods') or [])}｜{m.get('displayName', '')}")
    return out
