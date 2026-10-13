"""Claude API 呼叫：網路搜尋研究、結構化擷取。需要環境變數 ANTHROPIC_API_KEY。

- 模型、研究深度、搜尋次數、預算都在 config/settings.yaml 的 research 區塊設定
- 每次呼叫的 token 用量會累計到 state/research_usage.json，超過每月預算就停止
"""
from __future__ import annotations

import base64
import json
import logging
import os
from dataclasses import dataclass, field

from . import config
from .utils import ROOT, now_tw

log = logging.getLogger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"  # 模型因安全分類拒答時，伺服器端自動改用建議的備援模型
USAGE_PATH = ROOT / "state" / "research_usage.json"


class AIUnavailable(RuntimeError):
    """沒有 API 金鑰、超過預算或模型拒答。"""


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    web_searches: int = 0
    calls: list[str] = field(default_factory=list)
    price: dict | None = None  # 這次用的模型單價（美元／百萬 token）；沒給就用 settings 的 price_per_mtok

    def add(self, msg, label: str) -> None:
        u = getattr(msg, "usage", None)
        if u is None:
            return
        self.input_tokens += (getattr(u, "input_tokens", 0) or 0) + (getattr(u, "cache_read_input_tokens", 0) or 0) \
            + (getattr(u, "cache_creation_input_tokens", 0) or 0)
        self.output_tokens += getattr(u, "output_tokens", 0) or 0
        stu = getattr(u, "server_tool_use", None)
        self.web_searches += (getattr(stu, "web_search_requests", 0) or 0) if stu else 0
        self.calls.append(label)

    def usd(self) -> float:
        st = settings()
        price = self.price or st.get("price_per_mtok") or {}
        return (self.input_tokens * float(price.get("input", 0)) + self.output_tokens * float(price.get("output", 0))) / 1e6 \
            + self.web_searches * float(st.get("web_search_usd_per_1k", 0)) / 1000


def settings() -> dict:
    return config.settings().get("research") or {}


def available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


# ---------- 預算 ----------

def month_spent() -> float:
    if not USAGE_PATH.exists():
        return 0.0
    try:
        data = json.loads(USAGE_PATH.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return 0.0
    return float(data.get(now_tw().strftime("%Y-%m"), {}).get("usd", 0.0))


def record_usage(usage: Usage, topic: str) -> None:
    data = {}
    if USAGE_PATH.exists():
        try:
            data = json.loads(USAGE_PATH.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            data = {}
    m = data.setdefault(now_tw().strftime("%Y-%m"), {"usd": 0.0, "runs": 0, "input_tokens": 0, "output_tokens": 0,
                                                    "web_searches": 0})
    m["usd"] = round(m["usd"] + usage.usd(), 4)
    m["runs"] += 1
    m["input_tokens"] += usage.input_tokens
    m["output_tokens"] += usage.output_tokens
    m["web_searches"] += usage.web_searches
    m["last"] = f"{now_tw():%m/%d %H:%M} {topic}"
    USAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
    USAGE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def budget_left() -> bool:
    return month_spent() < float(settings().get("monthly_budget_usd", 20))


def _translate(e: Exception) -> Exception:
    """Anthropic 帳戶餘額不足（400 invalid_request）等，轉成 AIUnavailable，讓呼叫端可以保留題材、不要整個程式崩潰。"""
    text = str(e).lower()
    if "credit balance" in text or "plans & billing" in text:
        return AIUnavailable("Anthropic 帳戶餘額不足，請到 Console 的 Billing 儲值（預算額度與帳戶餘額是兩回事）")
    return e


def check_budget() -> None:
    budget = float(settings().get("monthly_budget_usd", 20))
    spent = month_spent()
    if spent >= budget:
        raise AIUnavailable(f"本月 AI 研究花費約 US${spent:.2f}，已達預算上限 US${budget:.0f}")


# ---------- 呼叫 ----------

def _client():
    if not available():
        raise AIUnavailable("尚未設定 ANTHROPIC_API_KEY")
    import anthropic  # 只有用到 AI 時才需要

    return anthropic.Anthropic(max_retries=3)


# 研究模型的單價（美元／百萬 token）；沒列在這裡的模型用 settings 的 price_per_mtok
MODEL_PRICES = {"claude-haiku-5-5": {"input": 0.1, "output": 0.5}}


def _model() -> str:
    return os.environ.get("RESEARCH_MODEL") or settings().get("model", "claude-opus-5-5")  # 環境變數：試跑其他模型用


def _modern(model: str | None = None) -> bool:
    """新版（動態過濾）網頁工具與伺服器端備援只有 Opus／Sonnet／Fable 支援；Haiku 改用舊版工具、不帶備援。"""
    return (model or _model()).startswith(("claude-opus-", "claude-sonnet-", "claude-fable-"))


def research_usage() -> Usage:
    """研究用的用量計價：依目前模型的單價，換模型時估價才會對。"""
    return Usage(price=MODEL_PRICES.get(_model()))


def _check_refusal(msg) -> None:
    if getattr(msg, "stop_reason", None) == "refusal":
        details = getattr(msg, "stop_details", None)
        raise AIUnavailable(f"模型拒絕回答（{getattr(details, 'category', None)}）")


def _text(msg) -> str:
    return "\n".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()


def web_research(system: str, prompt: str, usage: Usage) -> str:
    """讓 Claude 用網路搜尋研究，回傳 Markdown 報告。"""
    st = settings()
    client = _client()
    model = _model()
    modern = _modern(model)
    tools = [
        {"type": "web_search_20260209" if modern else "web_search_20250305", "name": "web_search",
         "max_uses": int(st.get("max_searches", 15))},
        {"type": "web_fetch_20260209" if modern else "web_fetch_20250910", "name": "web_fetch",
         "max_uses": int(st.get("max_fetches", 8))},
    ]
    api = client.beta.messages if modern else client.messages
    extra = {"betas": [FALLBACK_BETA], "fallbacks": "default"} if modern else {}
    messages = [{"role": "user", "content": prompt}]
    msg = None
    for _ in range(6):  # 伺服器端工具跑太久會 pause_turn，原封不動送回即可續跑
        try:
            with api.stream(
                model=model,
                max_tokens=64000,
                system=system,
                messages=messages,
                tools=tools,
                output_config={"effort": st.get("effort", "high")},
                **extra,
            ) as stream:
                msg = stream.get_final_message()
        except Exception as e:  # noqa: BLE001
            raise _translate(e) from e
        usage.add(msg, "research")
        _check_refusal(msg)
        if msg.stop_reason != "pause_turn":
            break
        messages = [{"role": "user", "content": prompt}, {"role": "assistant", "content": msg.content}]
    if msg is None or not _text(msg):
        raise AIUnavailable("研究沒有產出內容")
    if msg.stop_reason == "max_tokens":
        log.warning("研究報告達到長度上限，內容可能被截斷")
    return _text(msg)


def image_usage() -> Usage:
    """看圖用的便宜模型（預設 Haiku 5.5）的用量計價，不要套研究模型的單價。"""
    return Usage(price=settings().get("image_price_per_mtok") or {"input": 0.1, "output": 0.5})


def read_images(images: list[tuple[bytes, str]], instruction: str, usage: Usage) -> str:
    """讓便宜的視覺模型讀圖（頻道圖片貼文：抄錄圖中文字、描述圖表重點）。images = [(圖片位元組, media_type)]。"""
    check_budget()
    client = _client()
    content: list[dict] = [{"type": "image", "source": {"type": "base64", "media_type": mt,
                                                         "data": base64.b64encode(raw).decode("ascii")}}
                           for raw, mt in images]
    content.append({"type": "text", "text": instruction})
    try:
        msg = client.messages.create(
            model=settings().get("image_model", "claude-haiku-5-5"),
            max_tokens=8000,
            messages=[{"role": "user", "content": content}],
            output_config={"effort": "low"},
        )
    except Exception as e:  # noqa: BLE001
        raise _translate(e) from e
    usage.add(msg, "image")
    _check_refusal(msg)
    return _text(msg)


def extract_json(instruction: str, text: str, schema: dict, usage: Usage, label: str = "extract") -> dict:
    """用結構化輸出（JSON schema）從文字中擷取資料。"""
    client = _client()
    modern = _modern()
    try:
        msg = (client.beta.messages if modern else client.messages).create(
            model=_model(),
            max_tokens=16000,
            messages=[{"role": "user", "content": f"{instruction}\n\n<content>\n{text}\n</content>"}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
            **({"betas": [FALLBACK_BETA], "fallbacks": "default"} if modern else {}),
        )
    except Exception as e:  # noqa: BLE001
        raise _translate(e) from e
    usage.add(msg, label)
    _check_refusal(msg)
    raw = _text(msg)
    try:
        return json.loads(raw)
    except ValueError as e:
        raise AIUnavailable(f"AI 回傳的 JSON 無法解析：{e}") from e
