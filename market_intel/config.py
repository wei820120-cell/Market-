"""讀取 config/ 底下的 YAML 設定。"""
from __future__ import annotations

from functools import lru_cache

import yaml

from .utils import ROOT

CONFIG_DIR = ROOT / "config"


@lru_cache(maxsize=None)
def load(name: str) -> dict:
    path = CONFIG_DIR / f"{name}.yaml"
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def settings() -> dict:
    return load("settings")


def watchlist() -> dict:
    return load("watchlist")


def _auto_research() -> dict:
    """題材研究員自動產生的族群與關鍵字（research/auto_themes.yaml）。"""
    path = ROOT / "research" / "auto_themes.yaml"
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


@lru_cache(maxsize=None)
def themes() -> dict:
    base = dict(load("themes"))
    auto = _auto_research().get("tw") or {}
    if auto:
        base["tw"] = {**auto, **(base.get("tw") or {})}  # 手動設定優先
    return base


def news_keywords() -> dict:
    return load("news_keywords")


@lru_cache(maxsize=None)
def sources() -> dict:
    base = dict(load("sources"))
    auto = _auto_research().get("topic_themes") or {}
    if auto:
        merged = {k: list(v) for k, v in auto.items()}
        for k, v in (base.get("topic_themes") or {}).items():
            merged[k] = list(dict.fromkeys(([v] if isinstance(v, str) else list(v)) + merged.get(k, [])))
        base["topic_themes"] = merged
    return base


def all_tw_theme_codes() -> list[str]:
    codes: list[str] = []
    for members in (themes().get("tw") or {}).values():
        codes.extend(str(c) for c in members)
    return list(dict.fromkeys(codes))


def tw_watch_codes() -> list[str]:
    return [str(item["code"]) if isinstance(item, dict) else str(item) for item in (watchlist().get("tw") or [])]


def us_watch_symbols() -> list[str]:
    return [item["symbol"] if isinstance(item, dict) else str(item) for item in (watchlist().get("us") or [])]


def watch_item(code: str) -> dict:
    for item in (watchlist().get("tw") or []) + (watchlist().get("us") or []):
        if isinstance(item, dict) and str(item.get("code") or item.get("symbol")) == code:
            return item
    return {}
