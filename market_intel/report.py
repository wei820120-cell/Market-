"""把結果輸出成 Markdown 表格與報告。"""
from __future__ import annotations

import pandas as pd


def md_table(df: pd.DataFrame, max_rows: int = 30) -> str:
    if df is None or df.empty:
        return "_（無資料）_\n"
    df = df.head(max_rows)
    cols = [str(c) for c in df.columns]
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join(["---"] * len(cols)) + "|"]
    for row in df.itertuples(index=False):
        cells = ["" if v is None or (isinstance(v, float) and pd.isna(v)) else str(v) for v in row]
        lines.append("| " + " | ".join(c.replace("|", "／") for c in cells) + " |")
    return "\n".join(lines) + "\n"


def news_table(items, max_rows: int = 40) -> str:
    if not items:
        return "_（無符合關鍵字的新聞）_\n"
    rows = []
    for it in items[:max_rows]:
        title = it.title.replace("|", "／")
        link = f"[{title}]({it.url})" if it.url else title
        rows.append(f"| {it.score:+g} | {'、'.join(it.tags)} | {'、'.join(it.codes)} | {link} | {it.source} | {it.published} |")
    head = "| 分數 | 訊號 | 相關個股 | 標題 | 來源 | 時間 |\n|---|---|---|---|---|---|\n"
    return head + "\n".join(rows) + "\n"
