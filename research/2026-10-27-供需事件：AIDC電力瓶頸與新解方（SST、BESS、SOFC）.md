# 題材研究：供需事件：AIDC電力瓶頸與新解方（SST、BESS、SOFC）

> 🤖 AI 研究（2026-10-27 22:06），僅供參考，台股代號已和上市櫃清單核對。
> 狀態：**混合**｜時間軸：**中長期（6-24個月）**

**一句話**：AI 資料中心的限制已從晶片轉到電力：台達電董事長於 COMPUTEX 2026 引市場估計，至 2028 年全球資料中心新增約 44GW 電力需求，供電成長可能跟不上；解方分三條——SST（固態變壓器，台達稱效率 98.5%、把高壓直接轉 800VDC，美國客戶驗證中，最快 2027 年少量出貨）、SOFC（固態氧化物燃料電池，效率約 60%、餘熱回收可到 85%，出貨時程有『今年底』與『2027 年 30–50MW 試產』兩種說法）、BESS（儲能，這次沒查到專門資料）。電網側瓶頸更硬：傳聞大型變壓器交期從 24–30 個月拉到最長 5 年（單一說法）、燃氣渦輪訂單排到 2029–2030、美國 2026 年宣布的 12GW 資料中心只有 5GW 動工。觸發是 Threads 個人帳號的觀點貼文，未證實；本研究用公開報導交叉整理。

## 台股供應鏈總表

| 環節 | 代號 | 名稱 | 角色 | 影響 | 股票期貨 |
|---|---|---|---|---|---|
| 電網側設備（變壓器、配電盤） | 1519 | 華城 | 變壓器，既有研究已涵蓋；個股角色為推論 | 受惠 | VMF／小型VNF |
| 電網側設備（變壓器、配電盤） | 1503 | 士電 | 重電設備，既有研究已涵蓋；個股角色為推論 | 受惠 | UAF |
| 電網側設備（變壓器、配電盤） | 1513 | 中興電 | 重電與配電設備，既有研究已涵蓋；個股角色為推論 | 受惠 | SDF |
| SST／800VDC 電源與功率元件 | 2308 | 台達電 | SST、微電網、SOFC、液冷，『電網到晶片』布局；既有研究已涵蓋 | 受惠 | FRF／小型RVF |
| SOFC 現場發電 | 2308 | 台達電 | SOFC 與 800VDC 整合方案，出貨時程說法不一 | 受惠 | FRF／小型RVF |

<details><summary>觸發來源</summary>

2026-10-27 Threads 貼文（未證實，來源：@_77luka77_｜https://www.threads.com/@_77luka77_/post/De_mN_Dieso）：分享 AIDC（AI 資料中心）電力端瓶頸與新解方 SST、BESS、SOFC；說法為個人帳號觀點（推論：訓練轉推論、GW 級耗電、電力設備交期兩年起跳、併網時間長），未證實。

</details>

---

# 供需事件：AIDC電力瓶頸與新解方（SST、BESS、SOFC）

> 觸發：2026-10-27 Threads 貼文（未證實，來源：@_77luka77_｜https://www.threads.com/@_77luka77_/post/De_mN_Dieso）：分享 AIDC（AI 資料中心）電力端瓶頸與新解方 SST、BESS、SOFC；說法為個人帳號觀點（推論：訓練轉推論、GW 級耗電、電力設備交期兩年起跳、併網時間長），未證實。

## 重點
AI 資料中心的限制已從晶片轉到電力：台達電董事長於 COMPUTEX 2026 引市場估計，至 2028 年全球資料中心新增約 44GW 電力需求，供電成長可能跟不上；解方分三條——SST（固態變壓器，台達稱效率 98.5%、把高壓直接轉 800VDC，美國客戶驗證中，最快 2027 年少量出貨）、SOFC（固態氧化物燃料電池，效率約 60%、餘熱回收可到 85%，出貨時程有『今年底』與『2027 年 30–50MW 試產』兩種說法）、BESS（儲能，這次沒查到專門資料）。電網側瓶頸更硬：傳聞大型變壓器交期從 24–30 個月拉到最長 5 年（單一說法）、燃氣渦輪訂單排到 2029–2030、美國 2026 年宣布的 12GW 資料中心只有 5GW 動工。觸發是 Threads 個人帳號的觀點貼文，未證實；本研究用公開報導交叉整理。

## 三條解方
- **SST**：台達稱效率 98.5%、可把高壓直接轉 800VDC，已進入美國客戶驗證；2027 年 6–10MW 樣機，法人估最快 2027 年少量出貨（[科技新報](https://finance.technews.tw/2026/07/16/power-rack-sst-ai-infrastucre/)）。
- **SOFC**：效率約 60%、餘熱回收可到 85%；出貨時程有『今年底』與『2027 年 30–50MW 試產』兩種說法。Bloom 與 Oracle 簽 2.8GW 框架協議（[長橋](https://longportapp.cn/news/283010996)）。
- **BESS**：這次沒有查到專門資料。

## 電網側瓶頸（單一或轉引說法）
- 大型變壓器交期傳最長 5 年（單一 newsletter）；HSBC 引述高壓變電站 3–5 年（[Tech Investments](https://techinvestments.io/p/power-bottlenecks-and-the-ai-data)）。
- 燃氣渦輪訂單排到 2029–2030；美國 2026 年宣布的資料中心 12GW 只有 5GW 動工（[Dolphin Research／長橋](https://longportapp.cn/news/294144287)）。

## 誰受惠、誰受傷
- 受惠（推論）：台達電、華城、士電、中興電、Bloom Energy、英飛凌／Navitas。
- 承壓：電力取得時間與成本；800V 延後時 SST 需求認列延後。

## 風險與未證實
- 觸發為個人帳號的觀點貼文，貼文內容與數字未證實；本研究的數字來自媒體與投研評論，多為預估或公司說法
- SOFC 出貨時程說法互相矛盾（今年底起出貨 vs 2027 年 30–50MW 試產）
- 變壓器『5 年交期』是單一 newsletter 的病毒式說法，HSBC 報告引述為高壓變電站 3–5 年，中壓較不嚴重——不能當成所有變壓器的交期
- Bloom 的『55–90 天交付』是公司自己的 pilot 說法；Bloom 的訂單多為框架協議最大值，不等於確定出貨
- BESS 儲能這次沒有找到專門資料；台灣 SST／BESS／SOFC 受惠股清單沒有可靠來源，個股角色多為推論
- 800V HVDC 若延後到 2028 年以後，SST 與高壓元件的需求會晚於股價反應

## 資料來源
- [科技新報：AI power shelf 1MW（2026-06-02，搜尋命中）](https://technews.tw/2026/06/02/ai-power-shelf-1mw/)
- [科技新報：Power rack、SST 與 AI 基礎建設（2026-07-16）](https://finance.technews.tw/2026/07/16/power-rack-sst-ai-infrastucre/)
- [科技新報：英飛凌擴充產能與技術升級掌握成長機會（2026-03-31）](https://finance.technews.tw/2026/03/31/infineon-seizes-market-growth-opportunities-through-capacity-expansion-and-technological-upgrades/)
- [工商時報：台達電電力布局報導（2026-06-08，搜尋命中，標題未取得）](https://www.ctee.com.tw/news/20260608700165-430502)
- [Dolphin Research／長橋：Bloom Energy 無懼延期疑慮、吃透 AI 缺電紅利？](https://longportapp.cn/news/294144287)
- [長橋：甲骨文點燃 SOFC 產業——2.8GW 大單落地](https://longportapp.cn/news/283010996)
- [Tech Investments：電力瓶頸與 AI 資料中心](https://techinvestments.io/p/power-bottlenecks-and-the-ai-data)
