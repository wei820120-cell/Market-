"""Claude API 呼叫：網路搜尋研究、結構化擷取。需要環境變數 ANTHROPIC_API_KEY。

- 模型、研究深度、搜尋次數、預算都在 config/settings.yaml 的 research 區塊設定
- 每次呼叫的 token 用量會累計到 state/research_usage.json，超過每月預算就停止
"""
from __future__ import annotations

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
        price = st.get("price_per_mtok") or {}
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


def _model() -> str:
    return settings().get("model", "claude-opus-5-5")


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
    tools = [
        {"type": "web_search_20260209", "name": "web_search", "max_uses": int(st.get("max_searches", 15))},
        {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": int(st.get("max_fetches", 8))},
    ]
    messages = [{"role": "user", "content": prompt}]
    msg = None
    for _ in range(6):  # 伺服器端工具跑太久會 pause_turn，原封不動送回即可續跑
        with client.beta.messages.stream(
            model=_model(),
            max_tokens=64000,
            system=system,
            messages=messages,
            tools=tools,
            output_config={"effort": st.get("effort", "high")},
            betas=[FALLBACK_BETA],
            fallbacks="default",
        ) as stream:
            msg = stream.get_final_message()
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


def extract_json(instruction: str, text: str, schema: dict, usage: Usage, label: str = "extract") -> dict:
    """用結構化輸出（JSON schema）從文字中擷取資料。"""
    client = _client()
    msg = client.beta.messages.create(
        model=_model(),
        max_tokens=16000,
        messages=[{"role": "user", "content": f"{instruction}\n\n<content>\n{text}\n</content>"}],
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
        betas=[FALLBACK_BETA],
        fallbacks="default",
    )
    usage.add(msg, label)
    _check_refusal(msg)
    raw = _text(msg)
    try:
        return json.loads(raw)
    except ValueError as e:
        raise AIUnavailable(f"AI 回傳的 JSON 無法解析：{e}") from e
