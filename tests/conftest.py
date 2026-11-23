"""所有測試共用：微台計畫帳的存檔與紀錄一律寫到暫存資料夾，不碰真的 state/。

以前沒有這層保護：測試跑 cli 的日盤檢查會把 2026-10-30 的測試場次寫進真的 state/futures_log.json（那筆 0 點的「測試紀錄」
就是這樣進到計畫帳報表、把累計場數多算一場），也會改寫 state/futures_yearly.md，每次跑完測試都要手動 git checkout 還原。
個別測試自己 monkeypatch 路徑時以它為準（後設定的蓋掉這裡的）。"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _futures_state_in_tmp(tmp_path_factory, monkeypatch):
    from market_intel.trade import futures_day, futures_log
    d = tmp_path_factory.mktemp("state_guard")
    monkeypatch.setattr(futures_day, "STATE_PATH", d / "futures_day.json")
    monkeypatch.setattr(futures_log, "LOG_PATH", d / "futures_log.json")
    monkeypatch.setattr(futures_log, "MD_PATH", d / "futures_log.md")
    monkeypatch.setattr(futures_log, "YEARLY_PATH", d / "futures_yearly.md")
