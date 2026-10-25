# 股癌（Gooaye）學習筆記

目的：從 EP1（2020-02-27）一路讀到最新一集，把他的觀念與邏輯整理成可以套用的方法，並對照後來的走勢檢驗。

- 逐字稿是語音轉文字（faster-whisper），原文用 `config/podcast_archive_pub.pem` 加密存在 `state/podcast_archive/`（公開 repo 不放原文），私鑰只在研究員手上。
- `research/podcasts/股癌/EPxxxx.json` 是每集的摘要（提到的公司、方法論詞出現次數、市場話題與問答的時間點）。
- 這裡的筆記是研究員讀完之後的**自己的整理**（改寫，不抄原文）：`principles.md` 是依主題歸納的原則，`episodes/<年份>.md` 是逐集重點與當時的市場背景。
- 他的說法是個人觀點，不是投資建議；筆記會標出他自己說的前提、後來的驗證結果，以及我不確定的地方。

## 閱讀進度
| 年份 | 已轉文字 | 已讀並做筆記 |
|---|---|---|
| 2020 | 見 `research/podcasts/股癌/` | EP1～9、11～13、15（EP15 後半未讀） |

## 怎麼繼續（給之後接手的研究員）
- 轉文字：開發分支上的 `research.yml` 用 probe 啟動 `cmd:podcasts --backfill --show 股癌 --shard N --shards 8 --budget-min 330 --model medium`（N=0～7，每一片轉完會自己再啟動，直到那一片全部處理完）。進度看 `research/podcasts/股癌/` 的檔案數（總共 708 集，約 559 小時音檔）。
- 閱讀：`tools/gy.py`（需要私鑰，放在研究員容器的 `/root/.claude/podcast_archive_priv.pem`；私鑰不在 repo，容器被回收就要重新轉一次逐字稿）。
  - `gy.py list` 已處理的集數；`gy.py brief N N2 …` 略過業配與閒聊的精簡版；`gy.py show N --full` 整集；`gy.py grep 關鍵字 --eps 1-100` 跨集搜尋；`gy.py terms`、`gy.py tags` 依年份統計。
- 讀法：由舊到新，每讀一批就更新 `episodes/<年份>.md`（逐集重點）與 `principles.md`（原則，標出處集數），再 commit & push。
- 其他管道：Telegram 頻道 `t.me/Gooaye` 早就在 `config/sources.yaml` 的 `telegram_channels` 裡由監看迴圈追蹤（貼文轉給研究機器人並做早期訊號偵測）。YouTube 頻道（`UC23rnlQU_qE3cec9x709peA`）只是同一批集數、比 Podcast 晚發布，沒有額外追蹤的價值。Facebook（fb.com/Gooaye）與 Instagram 沒有登入就讀不到。
