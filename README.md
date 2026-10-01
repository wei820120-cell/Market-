# 市場即時情報系統（台股 / 美股 / 期貨 / 選擇權）

這個工具會：

- 第一時間抓取市場資訊與公司新聞
- 找出資金正在流入哪個族群
- 計算波段目標價與停損

## 資料夾結構

```
config/                  ← 你會常改的設定
  watchlist.yaml           自選股（計算目標價、到價推播）
  themes.yaml              族群成分股（資金流向依這裡分組）
  news_keywords.yaml       新聞關鍵字：漲價、缺貨、擴產、下修…
  settings.yaml            即時監控頻率、推播門檻
market_intel/            ← 程式
  fetchers/                抓資料
    tw_realtime.py           台股盤中即時報價（證交所 MIS）
    tw_daily.py              台股盤後：全市場行情、類股成交、三大法人、本益比
    taifex.py                期交所：期貨/選擇權行情、法人未平倉、Put/Call Ratio、大額交易人
    yahoo.py                 美股與台股日 K
    news.py                  重大訊息（公開資訊觀測站）、鉅亨網、Google 新聞、Yahoo、SEC 8-K
  analysis/                分析
    sector_flow.py           族群資金流向（盤中量能步調、盤後量比、類股比重、法人金額）
    target_price.py          波段目標價（ATR、箱型突破、費波納契、本益比）
    news_signals.py          新聞打分數、標出相關個股
    indicators.py            均線、ATR、RSI、量比
  cli.py                   指令入口
data/                    ← 抓下來的原始資料（不上傳 GitHub）
reports/                 ← 每日盤後報告（Markdown）
tests/                   ← 自動測試
.github/workflows/       ← 每天收盤後自動產生報告
```

## 安裝（第一次）

需要 Python 3.10 以上。

```bash
git clone https://github.com/wei820120-cell/-.git market-intel
cd market-intel
pip install -r requirements.txt
```

## 使用方式

| 指令 | 用途 | 什麼時候跑 |
|---|---|---|
| `python -m market_intel daily` | 盤後總報告，輸出到 `reports/日期.md` | 每天 15:00 後 |
| `python -m market_intel realtime` | 盤中即時監控，見下方說明 | 08:55 開著不關 |
| `python -m market_intel realtime --once --force` | 只抓一次即時快照 | 隨時 |
| `python -m market_intel news` | 掃一次新聞和公告 | 隨時 |
| `python -m market_intel target 2330 3017 NVDA` | 算個股目標價 | 隨時 |
| `python -m market_intel target 2330 --eps 60 --pe 22` | 加上本益比估值目標 | 隨時 |
| `python -m market_intel us` | 美股類股與族群資金流向 | 美股盤後 |

`realtime` 盤中即時監控的內容：

- 每 20 秒更新一次族群資金流向排行
- 族群資金湧入時推播
- 自選股到目標價或停損時推播，個股大漲時推播
- 每 60 秒掃一次重大訊息和漲價新聞

建議第一次先跑 `daily`。它會建立目標價快取，`realtime` 才能做到價推播。

## 盤後報告內容

0. **今日強勢標的**：資金流入（量比、法人）＋題材新聞＋漲價，並標出有沒有股票期貨
1. **台股族群資金流向**：族群今日成交金額 vs 前 5 日平均，判讀「資金流入／放量下跌／量縮」
2. **官方產業類股成交比重變化**：資金在產業之間的移動
3. **三大法人**：依族群加總的買賣超金額，外資和投信的買超、賣超排行
4. **期權籌碼**：Put/Call Ratio、台指期法人淨未平倉
5. **成交金額前 20 名**：資金最集中的個股
6. **美股**：類股 ETF 與族群的成交額比、漲跌
7. **自選股波段目標價**：保守目標、積極目標、停損、風報比
8. **新聞訊號**：漲價、缺貨、接單擴產、上修，以及利空，並標出相關個股

## 「資金流入族群」怎麼算

**盤中：量能步調**

- 族群目前累積成交金額 ÷（昨日全日成交金額 × 這個時間點正常應有的比例）
- 開盤和尾盤的量本來就大，所以用累積量曲線校正，不是單純用時間比例
- 步調 > 1.3 且加權上漲，判讀為「資金流入」；≥ 2 加 🔥 並推播
- 放量但下跌，判讀為「放量下跌⚠️」，可能是出貨

**盤後**：今日成交金額 vs 前 5 日平均，加上官方類股成交比重的變化，以及法人買賣超金額。

## 推播到手機（選用）

設定環境變數即可，**不要把金鑰寫進程式或上傳到 GitHub**，這個儲存庫是公開的：

三個 Telegram 機器人分開推播，手機上比較不亂：

| 機器人 | Secret 名稱 | 推播內容 |
|---|---|---|
| 盤勢 | `TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID` | 族群資金流向、資金湧入、到價、停損、大漲、盤後摘要 |
| 新聞（選用） | `TELEGRAM_NEWS_BOT_TOKEN` | 新聞、漲價信、重大訊息、盤後新聞整理 |
| 選股（選用） | `TELEGRAM_PICKS_BOT_TOKEN` | 強勢標的（資金流入＋題材＋漲價，附股票期貨代碼）、「漲價＋資金湧入」即時警示 |

- 新聞、選股機器人沒設定時，改由盤勢機器人送出。
- Chat ID 會沿用 `TELEGRAM_CHAT_ID`。要推到別的聊天室，才需要另設 `TELEGRAM_NEWS_CHAT_ID` 或 `TELEGRAM_PICKS_CHAT_ID`。
- 新建的機器人要先在 Telegram 對它按 **Start**，它才能傳訊息給你。

- Discord：`DISCORD_WEBHOOK_URL`

要讓 GitHub 自動排程也能推播，到儲存庫的 **Settings → Secrets and variables → Actions** 新增同名的 Secret。

## 盤中即時推播（雲端自動執行，不用開電腦）

`.github/workflows/realtime.yml` 會在週一到週五 08:35 自動啟動，在 GitHub 雲端監控到 13:35。休市日會自動偵測並結束。

推播內容（設定在 `config/settings.yaml`）：

- **定時族群排行**：09:15、10:00、11:00、12:00、13:00、13:25 推送資金流向前 5 名族群
- **資金湧入**：族群量能步調 ≥ 2 倍且上漲時立即推播（每個族群每天一次）
- **個股警示**：自選股觸及目標價或停損、族群個股漲幅 ≥ 7%，同一輪合併成一則
- **新聞**：新出現且分數 ≥ 3 的新聞或重大訊息（例如漲價信）

想在非交易時段測試，可以到 **Actions → realtime → Run workflow**，勾選「測試模式」後執行。

## 自動盤後報告

`.github/workflows/daily-report.yml` 會在週一到週五台北時間 15:40 自動執行 `daily`，並把報告存進 `reports/`。

- 也可以到 **Actions → daily-report → Run workflow** 手動執行
- 國定假日沒有新資料，報告會沿用最近一個交易日

## 資料來源與限制

| 資料 | 來源 | 即時性 |
|---|---|---|
| 台股盤中報價 | 證交所 MIS（公開網頁介面） | 約 5 秒，非逐筆 |
| 台股盤後 / 三大法人 / 類股成交 | 證交所、櫃買中心 OpenAPI | 收盤後 |
| 期貨 / 選擇權籌碼 | 期交所 OpenAPI | 收盤後 |
| 美股 / 台股日 K | Yahoo Finance | 美股非付費報價有延遲 |
| 重大訊息 | 公開資訊觀測站 OpenAPI | 公告後數分鐘 |
| 新聞 | 鉅亨網、Google 新聞、Yahoo、SEC 8-K | 分鐘級 |

- **要真正逐筆即時，以及盤中期貨／選擇權報價**：需要接券商 API（例如富果 Fugle、永豐 Shioaji）。`tw_realtime.py` 已預留轉接的位置。
- 證交所請求太頻繁會暫時封鎖 IP，程式已內建節流，請不要把間隔調得太短。
- 這些都是公開網站的非正式介面，格式可能改變。程式用「關鍵字找欄位」的方式盡量容錯，抓取失敗只會記錄警告，不會中斷。

## 測試

```bash
python -m pytest -q
```

測試使用離線樣本資料，驗證解析與計算邏輯。

---

⚠️ 目標價與訊號是依歷史價格、成交量與關鍵字推算的參考值，不保證會到達。投資請自行判斷並控管風險。
