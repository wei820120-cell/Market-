# 題材研究：供需事件：AI算力價格與供過於求風險（Ken Griffin：GPU像航空座位）

> 🤖 AI 研究（2026-10-24 23:15），僅供參考，台股代號已和上市櫃清單核對。
> 狀態：**混合**｜時間軸：**中期（3-12個月）**

**一句話**：Citadel 創辦人 Ken Griffin 在 10/20 的 Citadel 會議上說，AI『幾乎肯定』是泡沫但他不建議賣出 AI 股，並用航空座位類比 GPU：固定成本高、邊際成本低，一旦建置量超過需求，價格會自由落體（類比 2000 年電信光纖鋪太多）；他也提到記憶體廠毛利『極高』但投資人開始追問能撐多久。股癌 EP708（10/24）轉述了這些，並說自己追蹤的『算力價格』目前還在漲。公開數據支持『現在還沒轉弱』：H100 租金中位數約 4.20 美元／GPU 小時（10/7），Nebius 10/1 把 H100 從 3.85 調到 4.50、Runpod 從 3.29 調到 3.99；四大雲端 2026 年資本支出合計約 7,250～7,500 億美元（較 2025 年約 4,100 億成長約 77%），但其中有一部分是零組件漲價撐大的。所以這是一個『要盯哪些指標』的題材，不是已經發生的供過於求；最先受傷的會是算力租賃與資料中心，最後才是零組件廠。

## 台股供應鏈總表

| 環節 | 代號 | 名稱 | 角色 | 影響 | 股票期貨 |
|---|---|---|---|---|---|
| 算力租賃與資料中心（價格最先反映） | 3045 | 台灣大 | AIDC 與算力即服務（龜山預售完售）；暴露在租金走向 | 中性 | LCF |
| 算力租賃與資料中心（價格最先反映） | 2412 | 中華電 | AIDC（崙坪、蘆竹最高 36MW）與企業 ICT | 中性 | DLF |
| AI 伺服器與系統 ODM | 2382 | 廣達 | AI 伺服器 ODM | 中性 | DKF |
| AI 伺服器與系統 ODM | 6669 | 緯穎 | 雲端大廠伺服器 | 中性 | PVF／小型PWF |
| AI 伺服器與系統 ODM | 3231 | 緯創 | AI 伺服器代工 | 中性 | DXF |
| AI 伺服器與系統 ODM | 2317 | 鴻海 | AI 伺服器與 GPU 系統 | 中性 | DHF |
| AI 伺服器與系統 ODM | 2376 | 技嘉 | AI 伺服器 | 中性 | GHF／小型SLF |
| 記憶體（DRAM／HBM） | 2408 | 南亞科 | DRAM 報價與毛利的直接受益者 | 中性 | CYF |
| 記憶體（DRAM／HBM） | 2344 | 華邦電 | 利基型 DRAM／快閃記憶體 | 中性 | FZF |
| 記憶體（DRAM／HBM） | 2337 | 旺宏 | 快閃記憶體 | 中性 | DIF |
| 記憶體（DRAM／HBM） | 6770 | 力積電 | 記憶體與晶圓代工 | 中性 | QZF |
| 載板與 PCB 材料（缺貨漲價） | 3037 | 欣興 | ABF 載板龍頭 | 中性 | IRF |
| 載板與 PCB 材料（缺貨漲價） | 8046 | 南電 | ABF 載板 | 中性 | LYF／小型QSF |
| 載板與 PCB 材料（缺貨漲價） | 3189 | 景碩 | 載板 | 中性 | IXF |
| 載板與 PCB 材料（缺貨漲價） | 4958 | 臻鼎-KY | PCB 與載板 | 中性 | LUF |
| 被動元件（MLCC，股癌轉述但未見於 Griffin 報導） | 2327 | 國巨* | MLCC 與被動元件龍頭 | 中性 | LXF／小型QEF |
| 被動元件（MLCC，股癌轉述但未見於 Griffin 報導） | 2492 | 華新科 | MLCC、電阻、電感 | 中性 | HBF |

<details><summary>觸發來源</summary>

2026-10-24 股癌 EP708（逐字稿約 20:48～25:00，未證實：節目主持人轉述 Ken Griffin 訪談）：GPU 像航空座位、毛利已高難再上、算力價格仍在漲。已用公開報導交叉查證 Griffin 10/20 的談話。

</details>

---

# 供需事件：AI 算力價格與供過於求風險（Ken Griffin：GPU 像航空座位）

> 觸發：股癌 EP708（10/24）轉述 Ken Griffin 訪談。節目是個人說法、未證實；我用公開報導查證了 Griffin 10/20 的談話與目前的價格數據。

## 先講結論
- Griffin 說 AI『幾乎肯定』是泡沫，但他不建議賣出 AI 股；他拿航空座位類比 GPU：固定成本高、邊際成本低，建置量超過需求時價格會自由落體（類比 2000 年電信光纖鋪太多）。
- 公開數據目前還沒轉弱：H100 租金中位數約 4.20 美元／GPU 小時，Nebius、Runpod 在 10 月調漲；四大雲端 2026 年 capex 約 7,250～7,500 億美元（較 2025 年約 +77%），但其中一部分是零組件漲價撐大的。
- 傳導順序是（我的推演）算力租賃與資料中心先受傷，ODM 與零組件最後；要盯的是 GPU 租金、雲端 capex 指引、折舊年限與記憶體毛利。

## 要小心的地方
- 這是條件式論點，不是已發生的供過於求。
- 股癌轉述的 MLCC 說法與 Meta 財報反應我沒有查證到；『基底』可能是同音誤聽。
- 資本支出與 GPU 租金資料有互相矛盾的來源；沒有查到台股融資餘額與最新股價；不是選股結論。

## 接下來看什麼
雲端大廠 Q3 財報與 2027 capex 指引、GPU 租金走勢、輝達財報、記憶體報價與毛利、台灣 10 月營收。

## 來源
- [Decrypt：Ken Griffin Says AI Is 'Almost Certainly' in a Bubble—And Compares GPUs to Airline Seats](https://decrypt.co/381578/ken-griffin-ai-bubble-gpus-airline-seats)
- [Mitrade（2026/10/21）：Ken Griffin Compares AI Chips to Airline Seats, Says Glut Could Send Pricing Into Freefall](https://www.mitrade.com/insights/news/live-news/article-3-2179426-20261021)
- [BeInCrypto：Ken Griffin AI chip glut pricing](https://beincrypto.com/ken-griffin-ai-chip-glut-pricing/)
- [iTiger：AI Stocks are Probably in a Bubble - but Might be Worth Holding Onto, Says Citadel's Ken Griffin](https://www.itiger.com/news/2677621551)
- [IDN Financials：Citadel CEO Ken Griffin: AI has not been proven to boost productivity（2026/1 Davos）](https://www.idnfinancials.com/news/60718/citadel-ceo-ken-griffin-ai-has-not-been-proven-to-boost-productivity)
- [Uvik：GPU price index（2026/10/7 中位數租金）](https://uvik.net/blog/gpu-price-index/)
- [IntuitionLabs：H100 Rental Prices Compared（Runpod、Nebius 調價）](https://intuitionlabs.ai/articles/h100-rental-prices-cloud-comparison)
- [IntuitionLabs：Data center GPU pricing 2026](https://intuitionlabs.ai/articles/data-center-gpu-pricing-2026)
- [DatacenterDynamics：Moody's hyperscaler capex forecasts marked up by $85bn to close in on $1trn by 2027](https://datacenterdynamics.com/en/news/moodys-hyperscaler-capex-forecasts-marked-up-by-85bn-to-close-in-on-1trn-by-2027/)
- [Yahoo Finance：'Magnificent 7' earnings rush reveals AI spending surge, hyperscaler capex set to reach $725 billion in 2026](https://finance.yahoo.com/markets/article/magnificent-7-earnings-rush-reveals-ai-spending-surge-with-hyperscaler-capex-set-to-reach-725-billion-in-2026-224901707.html)
- [Tradersunion：Software stocks regain favor after strong earnings](https://tradersunion.com/news/stocks/show/3381454-software-stocks-regain-favor/)
- [Yahoo Finance：CrowdStrike, Cisco, and Palo Alto hit records as cyber stocks outrun chips](https://finance.yahoo.com/markets/article/cybersecurity-stocks-are-hitting-records-again-as-chips-play-catch-up-chart-of-the-day-130252672.html)
