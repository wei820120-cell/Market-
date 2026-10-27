"""待檢視清單：Threads 貼文、Podcast 段落這類「不推播、由研究員決定要不要研究」的內容。
每天一個檔（<資料夾>/日期.json），只保留最近幾天；每筆有 id（同一筆不重複放）。"""
from __future__ import annotations

import json
from pathlib import Path


def load(directory: Path, day: str) -> list[dict]:
    path = directory / f"{day}.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8")).get("items", [])
        except (ValueError, OSError):
            pass
    return []


def add(directory: Path, entries: list[dict], day: str, keep_days: int = 3) -> int:
    """放進新的幾筆（id 已存在的略過），回傳新增幾筆；順便刪掉太舊的檔。"""
    items = load(directory, day)
    have = {it["id"] for it in items}
    new = [e for e in entries if e["id"] not in have]
    if new:
        items += new
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{day}.json").write_text(json.dumps({"day": day, "items": items}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    if directory.exists():
        for f in sorted(directory.glob("*.json"))[:-keep_days]:
            f.unlink()
    return len(new)
