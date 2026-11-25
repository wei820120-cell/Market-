# 題材研究：玻璃基板與TGV（Threads說法求證）

> 🤖 AI 研究（2026-11-25 18:32），僅供參考，台股代號已和上市櫃清單核對。
> 狀態：**混合**｜時間軸：**長期（1年以上）**

**一句話**：@insightlab.tw 貼文稱：玻璃有三種、TGV 講的是核心基板；CoPoS 先用玻璃載板；台積電光罩倍數 5.5 倍→2028 年 14 倍，輝達封裝面積 Hopper 三千多→Feynman 一萬八 mm²；有機核心 13～20 ppm 對矽 2.6 而翹曲；CoWoS-L 四顆 die 壓不住、Rubin Ultra 改回兩顆。核對：5.5 倍→14 倍是 TSMC 路線圖（5.5 倍已量產、良率 98% 以上）、CTE 不匹配造成翹曲是共識、矽約 2.6 ppm 對得上；有機核心查到 11～17、約 16 ppm（貼文 13～20 略有出入）；『CoPoS 先用玻璃載板』與 Hopper／Feynman 面積沒找到佐證（18,000 mm² 剛好是 9.5 倍光罩的基板尺寸，算術，可能是不同口徑）；『Rubin Ultra 改回兩顆』報導互相矛盾，未證實。時程：CoPoS 量產 2H 2028（報導）、玻璃核心商業化不早於 2030（標題）、郭明錤估 4Q28～1Q29；SKC／Absolics 一再延後、三星電機兩個日期衝突；欣興、群創、TPK-KY 等只有驗證與試產，欣興自稱 2030 年前商業化量產仍有難度。技術路線與時程題材，不是當下供需缺口；台廠一律標中性。內容來自搜尋摘要與標題，沒讀到 TSMC 簡報原件與法說資料。

## 台股供應鏈總表

| 環節 | 代號 | 名稱 | 角色 | 影響 | 股票期貨 |
|---|---|---|---|---|---|
| 台廠進度（驗證與試產） | 3037 | 欣興 | ABF 載板龍頭；玻璃核心基板處於樣品驗證規劃階段，早期稱最快 2027～2028 量產，7 月法說稱 2030 年前商業化量產仍有難度；沒有訂單資料 | 中性 | IRF |
| 台廠進度（驗證與試產） | 8046 | 南電 | ABF 載板；具高階增層與載板製程能力，尚未公布 Glass Core 商業化時程 | 中性 | LYF／小型QSF |
| 台廠進度（驗證與試產） | 3189 | 景碩 | ABF／BT 載板；尚未公布 Glass Core 商業化時程 | 中性 | IXF |
| 台廠進度（驗證與試產） | 3481 | 群創 | 面板級封裝（FOPLP／CoPoS）與 TGV；據未具名供應鏈消息與台積電、揖斐電共同驗證玻璃核心載板；官方已把 TGV 列入技術藍圖 | 中性 | DQF |
| 台廠進度（驗證與試產） | 3673 | TPK-KY | TGV 專用試產線原規劃 2026 年第三季完成並送樣；仍處於研發與客戶驗證階段，完成與否未查 | 中性 | JNF |

<details><summary>觸發來源</summary>

2026-11-25 待檢視清單（未證實）：Threads @insightlab.tw（score 5，seen_at 2026-11-25 16:57，https://www.threads.com/@insightlab.tw/post/DgKGQkGD1-Q）整理玻璃基板與 TGV：稱新聞常把三種玻璃混在一起（玻璃載板、玻璃核心基板、玻璃中介層），CoPoS 第一階段先用玻璃載板、之後才會上玻璃基板；台積電光罩倍數路線圖從現在的 5.5 倍到 2028 年的 14 倍，輝達封裝面積從 Hopper 三千多平方毫米到 Feynman 一萬八；有機核心約 13～20 ppm、矽 2.6 ppm，封裝變大回焊就翹；業界傳 CoWoS-L 做到四顆 die 壓不住、Rubin Ultra 才改回兩顆——個人帳號說法，未證實。

</details>

---

# 玻璃基板與 TGV（Threads 說法求證）——機制與路線圖屬實，『Rubin Ultra 改回兩顆』未證實，台廠仍在驗證與試產（2026-11-25）

> 資料說明：寫於 **2026-11-25 傍晚（台北時間）**，貼文原文有取得（Threads 個人帳號 @insightlab.tw）；其餘內容來自搜尋摘要與標題（TrendForce、Tom's Hardware、DNP、郭明錤 X 貼文、IEEE 簡報、各台灣媒體與券商文章）；**沒有讀到 TSMC 簡報原件、各廠法說資料與專利全文**。不是投資建議。

## 0. 一句話
**一句話**：貼文說：玻璃有三種（載板、核心基板、中介層），TGV 講的是核心基板；CoPoS 第一階段先用玻璃載板；台積電光罩倍數從 5.5 倍做到 2028 年 14 倍，輝達封裝面積從 Hopper 三千多到 Feynman 一萬八平方毫米；有機核心 13～20 ppm 對矽 2.6，封裝一大就翹；業界傳 CoWoS-L 四顆 die 壓不住、Rubin Ultra 改回兩顆。逐項核對：

- **屬實或合理**：5.5 倍→2028 年 14 倍是 TSMC 自己的路線圖（5.5 倍已量產、良率 98% 以上）；CTE 不匹配造成翹曲是業界共識，矽約 2.6 ppm 對得上；三種玻璃的區分合理（推論）。
- **數字略有出入**：有機核心 CTE 查到約 11～17、約 16 ppm，貼文是 13～20；對矽的倍數約 4～8 倍（算術）。
- **未證實**：『CoPoS 第一階段先用玻璃載板』沒找到佐證；『Hopper 三千多→Feynman 一萬八』沒找到佐證，而 120×150 mm＝18,000 mm² 剛好是 9.5 倍光罩的基板尺寸（算術），可能是不同口徑（推論）；『Rubin Ultra 改回兩顆』報導互相矛盾（放棄四 die vs 兩種版本並存），NVIDIA 與 TSMC 都沒確認。
- **時程**：玻璃核心基板仍在驗證與試產：TSMC CoPoS 量產 2H 2028（報導）、玻璃核心商業化不早於 2030（標題）、郭明錤估 4Q28～1Q29、業界共識放量 2027 年中至 2028 年（一則摘要）；SKC／Absolics 一再延後、三星電機兩個日期衝突；台廠欣興、群創、TPK-KY 等都在驗證，沒有公開量產時程，欣興自己稱 2030 年前商業化量產仍有難度。
- **標記**：貼文被標記『光罩（2338）』，是『倍光罩』用詞的疑似誤判（推論），與台灣光罩公司無關。

這是**技術路線與時程題材，不是當下的供需缺口**（近期缺口仍在 ABF 載板與 T-glass，見《載板供需》）；台廠一律標中性。

## 1. 說法逐項核對
| 說法 | 查到的資料 | 來源 | 判讀 |
|---|---|---|---|
| 『新聞常把三種玻璃混在一起：玻璃載板（暫時撐住晶片、不打洞）、玻璃核心基板（取代有機核心，TGV 講的是這個）、玻璃中介層（取代矽中介層，更後面）』 | 搜尋到的文章談 TSMC CoPoS、玻璃核心驗證與 TGV 時，主要都是『玻璃核心基板』；一篇標題把 GCS（玻璃核心基板）與 TGV 並列講解；沒有逐項對照三種玻璃的定義 | 豐雲學堂標題、商業周刊、Leverage（搜尋摘要） | 概念區分合理（推論），查不到反證 |
| 『CoPoS 第一階段先用玻璃載板，之後才上玻璃基板』 | 沒有找到明確佐證。查到：CoPoS（面板級封裝）設備驗證 2026、試產 2027、量產 2H 2028（報導）；玻璃核心商業化不會早於 2030（標題，董事長稱『沒有捷徑』）；郭明錤估玻璃核心量產 4Q28～1Q29；台積電與揖斐電、群創合作驗證玻璃核心（報導） | Dopodomani 標題、郭明錤 X、搜尋摘要 | 時間線方向一致（玻璃核心晚於 CoPoS 起步）；『第一階段是載板』未證實 |
| 『台積電路線圖：現在 5.5 倍光罩，2028 年 14 倍』 | TSMC 已量產 5.5 倍 CoWoS、良率 98% 以上（TrendForce 標題稱 99% 以上）；2027 年 9.5 倍；2028 年 14 倍（多數來源；少數稱 2029），2029 年超過 14 倍 | TrendForce、Tom's Hardware、TradingKey（搜尋摘要） | 屬實；倍數是 TSMC 自己的路線圖，不是獨立量測 |
| 『輝達封裝面積：Hopper 三千多平方毫米、Feynman 一萬八』 | 沒有找到 Hopper 或 Feynman 封裝面積的佐證。一則報導稱 9.5 倍光罩需要 120×150 mm 基板（＝18,000 mm²，算術） | Tom's Hardware（搜尋摘要） | 未證實；『一萬八』與 9.5 倍的基板尺寸同數字，可能是不同口徑（推論，見表三） |
| 『有機核心約 13～20 ppm、矽 2.6，封裝一大回焊就翹』 | CTE（熱膨脹係數）數字見表三：有機核心與矽差 4～8 倍，機制（CTE 不匹配造成翹曲）是業界共識 | IEEE 簡報、AAIG Substack（搜尋摘要） | 機制屬實；13～20 的區間與查到的 11～17、約 16 略有出入 |
| 『業界傳 CoWoS-L 做到四顆 die 就壓不住，Rubin Ultra 才改回兩顆』 | 報導互相矛盾（見表四）：有的稱 NVIDIA 已放棄四 die 改雙 die、歸因基板翹曲；有的稱 Rubin Ultra 可能有 2 晶片與 4 晶片兩種版本 | TechPowerUp 標題、AtlasPCB、搜尋摘要（UBS 轉述） | 傳聞，未證實；NVIDIA 與 TSMC 都沒有確認 |
| 貼文被標記『光罩（2338）』 | 文中『倍光罩』是 reticle（曝光光罩）尺寸的用詞，與台灣光罩公司（2338）無關 | 貼文標記 | 疑似標記誤判（推論），不採用 |

## 2. 全球玻璃核心基板時程對照
| 公司／估計 | 目標或狀態 | 來源 | 備註 |
|---|---|---|---|
| TSMC CoPoS | 設備驗證 2026、試產 2027、量產 2H 2028（報導）；玻璃核心商業化不會早於 2030（標題） | Dopodomani 標題、搜尋摘要 | 兩個說法並列：CoPoS 量產 ≠ 玻璃核心商業化 |
| 郭明錤 | 玻璃核心量產 4Q28～1Q29（產業查訪）；TSMC 6/11 JPCA Show 簡報有玻璃核心基板一頁 | X 貼文（搜尋摘要） | 個人研判 |
| Intel | 沒有找到明確量產日期；有可運作的測試載具，玻璃基板在長路線圖的後段 | 搜尋摘要 | — |
| 三星電機 | 2026 年送樣；2027 下半年量產（一則）vs 2028 年全面量產（7 月法說的報導） | BigGo、搜尋摘要 | 兩個日期互相衝突 |
| SKC／Absolics | 目標由 2025 年底→2026 年初→2027 年；客戶驗證中；韓媒標題稱再延後 | Tom's Hardware、搜尋摘要 | 一再延後 |
| DNP | Kuki 試產線 2025-12 啟動；樣品 2026 年初；量產 FY2028 | DNP、TrendForce | 公司新聞稿層級 |
| AVACO | 515×510 mm TGV 試產線；2026 年底前量產樣品 | AtlasPCB | 樣品，不是量產 |
| LG Innotek | 只在韓媒標題中提到追趕三星與 SKC；沒有時程 | 搜尋摘要 | 資料缺口 |
| ABF 載板缺口 | 標題稱 ABF 缺口一路到 2028；TrendForce 標題稱 5.5 倍 CoWoS 良率 99% 以上、但記憶體與 ABF 是 AI 瓶頸 | 新光證券、TrendForce（標題） | 玻璃是『下一代』，近期缺口仍在 ABF（見《載板供需》《ABF供需》） |

## 3. 尺寸與熱膨脹的數字核對
| 項目 | 數字 | 來源 | 算術與備註 |
|---|---|---|---|
| 光罩倍數與面積 | 1 倍光罩約 26×33 mm＝858 mm²（常識值，本次未在搜尋中核對） | — | 5.5 倍≈4,719 mm²；9.5 倍≈8,151 mm²；14 倍≈12,012 mm²（算術） |
| 9.5 倍的基板 | 一則報導稱需要 120×150 mm 基板、支援 12 顆 HBM5 | Tom's Hardware（搜尋摘要） | 120×150＝18,000 mm²（算術），與貼文『Feynman 一萬八』同數字 |
| 貼文：Hopper 三千多→Feynman 一萬八 mm² | 沒有找到佐證 | — | 18,000÷858≈21 倍光罩，高於 TSMC 2028 年 14 倍（約 12,012 mm²）；倍數量的是中介層與整合面積，若貼文的『封裝面積』指基板則口徑不同（推論） |
| 有機核心 CTE | 貼文 13～20 ppm；CCL 核心約 11～17 ppm/°C（Substack）；PCB 約 16 ppm（IEEE 簡報） | AAIG Substack、IEEE（搜尋摘要） | 對矽 2.6 ppm：貼文區間 5.0～7.7 倍；查到的 11～17 為 4.2～6.5 倍（算術） |
| 矽 CTE | 貼文 2.6；一個來源約 2.6、另一約 3 | 搜尋摘要 | 2.6 對得上 |
| 玻璃 CTE | 一件專利稱一般玻璃約 3.2 ppm（摘要未指明哪一件）；IEEE 演講稱玻璃 CTE 可調 2.7～9 | 搜尋摘要 | 3.2÷2.6≈1.2 倍；有機核心約 16÷2.6≈6.2 倍（算術）——玻璃的賣點是與矽接近 |

## 4. Rubin Ultra『四顆 die 改兩顆』說法追蹤
| 來源 | 說法 | 性質 | 判讀 |
|---|---|---|---|
| TechPowerUp（標題） | NVIDIA 的 Rubin Ultra 據報在 CoWoS-L 封裝遇到問題 | 媒體標題 | 只看到標題 |
| AtlasPCB（二手部落格） | 稱 NVIDIA 已放棄四 die 版本、改為雙 die，原因是 CoWoS-L 無法控制四 die 佈局的基板翹曲 | 二手部落格 | 不是 NVIDIA 或 TSMC 證實 |
| UBS（經搜尋摘要轉述） | Rubin Ultra 可能有兩種形式：2 晶片版用 CoWoS-L、4 晶片版可能走 Intel EMIB-T | 券商說法（轉述） | 與『放棄四顆』矛盾 |
| 另一則摘要 | NVIDIA 評估由四 die 縮為『2+2』板級設計，基板翹曲是可見症狀 | 報導（出處未標明） | 又一種版本 |
| 研究庫相鄰題材 | 《傳輝達 Vera-Rubin 延後放量（未證實）》（11/16）、《EMIB-T 基板良率瓶頸》（10/19） | 另案 | 同一類傳聞的前案 |

## 5. 產業鏈因果（垂直）

**一行鏈**：AI 晶片封裝變大（CoWoS 5.5 倍→9.5 倍→14 倍）→ 有機核心與矽的熱膨脹係數（CTE）差距造成回焊翹曲與焊點對位問題 → 需要與矽更接近的核心材料（玻璃）與更細密的通孔（TGV）→ 玻璃核心基板驗證（TSMC、揖斐電、群創等）→ 載板廠與設備、材料廠的卡位 → 量產時程（2027 年中～2029 年，說法不一）。

- **為什麼要換**：封裝越大，有機核心與矽的 CTE 倍數差（約 4～8 倍）越難壓住翹曲；玻璃 CTE 約 3.2 ppm、可調（一般玻璃對矽約 1.2 倍，算術）。
- **TGV 的難點**：孔又深又細，電鍍容易有空洞、速度慢；插入預製銅柱是另一條路（貼文的另一串，見《創新服務（7828）》）。
- **誰先動**：TSMC 與揖斐電、群創驗證玻璃核心（報導）；海外 SKC／Absolics、三星電機、DNP 皆在試產或送樣。

## 6. 相關推進力（水平）
- **ABF 載板與 T-glass 缺口**：近期瓶頸在 ABF 與玻纖布（另案《載板供需》《ABF供需》）。
- **EMIB-T 與 CoWoS-L 的路線之爭**：Rubin Ultra 版本之爭（另案《EMIB-T 基板良率瓶頸》《傳輝達 Vera-Rubin 延後放量（未證實）》）。
- **CoPoS／面板級封裝**：群創的 FOPLP 與 CoPoS（另案《群創（3481）》）。
- **探針卡與銅柱巨轉設備**：TGV 周邊設備（另案《創新服務（7828）》）。

## 7. 慣性、痛點與限制（思辯）

**供需缺口怎麼來的**：本題的缺口是**技術缺口**——封裝尺寸成長快於有機核心能承受的翹曲；解法（玻璃核心）自己還沒量產，所以近期仍靠 ABF 載板撐住，玻璃是 2027～2029 年的卡位題。

**慣性**：把『CoPoS 導入玻璃』讀成『玻璃基板要量產』；把公司目標當量產確認；把一則傳聞（Rubin Ultra 改兩顆）當事實；把台廠『有在驗證』讀成『有訂單』；把題材股的漲停當成需求。

**痛點與最快的解法**（標時點與卡點）：
- **TGV 良率與空洞**：卡點在電鍍速度與深孔填充；解法之一是預製銅柱（尚在早期）。
- **玻璃易碎與製程**：搬運、切割、翹曲；韓廠稱製程時間由半天縮到幾分鐘（標題）。
- **客戶驗證**：Absolics 一再延後；欣興稱 2030 年前商業化量產仍有難度。
- **Rubin Ultra 版本**：要等 NVIDIA 與 TSMC 的正式說法。

**限制**：沒有 TSMC 簡報原件與各廠法說資料；時程多為公司目標；貼文的 Hopper 與 Feynman 面積無佐證。

**擴產與稼動檢核**：見第 8 節表五。

**敘事陷阱自查**：
1. **『CoPoS 導入玻璃＝玻璃基板量產』**：CoPoS 量產（2H 2028）與玻璃核心商業化（不早於 2030 之說）是兩件事。
2. **『公司目標＝量產』**：Absolics、三星電機的日期一再改。
3. **『Rubin Ultra 改回兩顆』**：版本互相矛盾，未證實。
4. **『一萬八平方毫米＝Feynman』**：同數字也是 9.5 倍光罩的基板尺寸。
5. **『CTE 數字精確』**：不同來源 11～20 ppm，隨材料與測試條件。
6. **『台廠進度＝訂單』**：只有驗證與試產，沒有量產與出貨資料。
7. **『光罩（2338）』標記**：與公司無關。

## 8. 擴產與稼動檢核
| 公司 | 瓶頸環節 | 現況稼動 | 擴產與資本支出 | 投產或放量時點 | 主要限制 | 證據等級 |
|---|---|---|---|---|---|---|
| 欣興（3037） | 玻璃核心基板驗證、TGV | 未揭露（樣品驗證規劃階段） | 與 2～3 家客戶做客製化專案研發（搜尋摘要） | 早期說法最快 2027～2028 年量產；2026 年 7 月法說稱 2030 年前商業化量產仍有難度 | TGV 良率、翹曲、客戶驗證；前後說法不一致 | 公司說法（搜尋摘要） |
| 南電（8046）／景碩（3189） | 高階增層與載板製程 | 未揭露 | 未公布 Glass Core 商業化時程 | 未揭露 | 資料缺口 | 搜尋摘要 |
| 群創（3481） | TGV、玻璃核心載板驗證 | 未揭露 | 官方已把 TGV 列入半導體事業技術藍圖 | 未揭露 | 與台積電、揖斐電共同驗證的說法來自未具名供應鏈消息 | 未具名消息 |
| TPK-KY（3673） | TGV 專用試產線 | 未揭露（研發與客戶驗證） | 原規劃 2026 年第三季完成 TGV 專用試產線並送樣 | 未揭露 | 是否如期完成未查 | 搜尋摘要 |
| 晶呈科 | TGV 產線 | 未揭露 | 2026 年底規劃 2 條完整線、2027 年底 12 條線（單一資料） | 2026～2027 | 單一資料，未核對 | 單一來源 |
| SKC／Absolics | 玻璃核心基板客戶驗證 | 客戶驗證中 | 未揭露 | 目標由 2025 年底→2026 年初→2027 年 | 客戶驗證與良率 | 媒體報導（搜尋摘要） |
| 三星電機 | 玻璃核心基板 | 未揭露 | 2026 年送樣 | 2027 下半年（一則）vs 2028 年（7 月法說的報導） | 兩個日期互相衝突 | 媒體報導（衝突） |

## 9. 誰受惠、誰受傷
- **台廠一律標中性**：欣興 3037、南電 8046、景碩 3189、群創 3481、TPK-KY 3673 都只有驗證、試產或研發資料，沒有訂單、稼動或出貨資料；題材先行。
- **相對受惠（情境，推論）**：若玻璃核心取代部分有機核心，載板、TGV 設備與材料廠會重新洗牌；時點在 2027 年中以後，且公司自己稱商業化仍有難度。
- **相對受傷（情境，推論）**：押注『玻璃很快量產』而沒有驗證證據的題材股。

## 10. 怎麼驗證
1. **TSMC 簡報與法說**：CoPoS 與玻璃核心的時程；是否有『載板先、核心後』的說法。
2. **NVIDIA 與 TSMC 對 Rubin Ultra 封裝版本的正式說法**。
3. **欣興、南電、景碩、群創、TPK-KY 法說會**：是否揭露客戶、送樣、驗證結果與資本支出。
4. **SKC／Absolics、三星電機、DNP 的量產公告**。
5. **TSMC 與輝達的封裝面積數字**（Hopper、Feynman）；CoWoS 14 倍是 2028 或 2029 年。
6. **TGV 良率與銅柱插入技術的驗證進度**。

## 11. 舉一反三：和研究庫相鄰題材對照
- **《載板供需》（10/13）與《ABF供需》（10/30）**：近期缺口與玻璃核心的關係。
- **《EMIB-T 基板良率瓶頸》（10/19）**：Rubin Ultra 另一條封裝路線。
- **《欣興（3037）》（10/21）、《南電（8046）》（10/17）、《景碩（3189）》（10/21）**：載板三雄的產能與漲價。
- **《群創（3481）》（10/28）**：FOPLP、CoPoS 與 TGV。
- **《創新服務（7828）》（11/21）**：銅柱巨轉模組與 TGV-ICP。
- **《供需事件：傳輝達 Vera-Rubin 延後放量（未證實）》（11/16）**：同類傳聞的前案。

## 12. 傳聞與未證實
- **『CoPoS 第一階段先用玻璃載板』**：沒找到佐證。
- **『Hopper 三千多→Feynman 一萬八平方毫米』**：沒找到佐證。
- **『CoWoS-L 四顆 die 壓不住、Rubin Ultra 改回兩顆』**：報導互相矛盾，未證實。
- **『欣興 2027～2028 年量產』**：早期說法，後續法說稱 2030 年前商業化量產仍有難度。
- **『群創與台積電、揖斐電共同驗證』**：來自未具名供應鏈消息。
- **『晶呈科 2026 年底 2 條線、2027 年底 12 條線』**：單一資料。

## 13. 風險與矛盾
- 全是搜尋摘要與標題；TSMC 簡報原件、法說資料與專利全文沒讀到。
- 時程互相衝突：三星電機 2027 下半年 vs 2028；欣興早期說法 vs 7 月法說；CoWoS 14 倍 2028 vs 2029。
- CTE 數字各來源不同（11～20 ppm），玻璃 CTE 一說約 3.2 ppm、一說可調 2.7～9。
- 1 倍光罩 858 mm² 為常識值，本次未在搜尋中核對。
- 光罩倍數量的是整合面積，與『封裝面積』口徑可能不同。

## 資料來源
- [Threads：@insightlab.tw 貼文（2026-11-25；score 5；個人帳號說法，未證實）](https://www.threads.com/@insightlab.tw/post/DgKGQkGD1-Q)
- [Glass substrate roadmaps examined — Absolics in final qualification and a first product that keeps slipping（Tom's Hardware；僅搜尋摘要）](https://www.tomshardware.com/tech-industry/manufacturing/glass-substrate-roadmap-examined&gsessionid=rIa50mwpG33gW8A8xeE9S68Kbytb0CDQ2l9ODdWJi6P_1sUaKQMkhg)
- [[News] DNP Advances TGV Glass Substrates, Eyes Early-2026 Sample Shipments, FY2028 Mass Production（TrendForce，2025-12-22；僅搜尋摘要）](https://www.trendforce.com/news/2025/12/22/news-dnp-accelerates-tgv-glass-substrate-push-with-late-2025-pilot-line-early-2026-sample-shipments/)
- [DNP to Launch New TGV Glass core Substrate Pilot Line at Kuki Plant（DNP，2025-12；僅搜尋摘要）](https://www.global.dnp/en/news/detail/2025/12/1216_20177773/)
- [郭明錤｜Ming-Chi Kuo on X：Breaking down TSMC's glass core substrate slide（JPCA Show 2026 簡報；僅搜尋摘要）](https://x.com/mingchikuo/status/2067438616188739960)
- [What Is a Glass Substrate? Is It in Mass Production Yet? The Next-Gen Material for AI Packaging and Taiwan's Supply Chain Explained（PenChan；僅搜尋摘要）](https://penchan.co/en/market/ai/supply-chain/glass-substrate/)
- [Glass Substrates for AI Chips: 5 Hurdles Before Mass Production（Silicon to Software；僅搜尋摘要）](https://www.silicontosoftware.com/glass-substrates-for-ai-chips/)
- [Korea’s Glass Substrate Value Chain — TGV, Materials and Timelines（Kotaro Invest，2026-09-01；僅搜尋摘要）](https://kotaroinvest.com/2026/09/01/koreas-glass-substrate-value-chain/)
- [Glass substrate mass production race heats up: Samsung Electro-Mechanics debuts at SEMICON Taiwan as South Korean supply chain targets first deliveries in 2026 — BigGo Finance（僅搜尋摘要）](https://finance.biggo.com/news/317836a1-dcfa-4868-b6e1-2d9916bc90d2)
- [AVACO Launches 515x510mm TGV Glass Substrate Pilot Line, Accelerating Next-Generation Semiconductor Packaging — AtlasPCB（僅搜尋摘要）](https://www.atlaspcb.com/news/news-avaco-tgv-glass-substrate-pilot-line-2026/)
- [TGV Still Faces Technical Barriers: TSMC’s CoPoS Glass Substrate Unlikely to Reach Mass Commercialization Before 2030, Chairman Says “No Shortcuts”（Dopodomani；僅搜尋摘要）](https://dopodomani.biz/en/f128ebc5a08c/)
- [2026玻璃基板概念股盤點：14檔台股誰已試產？TGV與量產進度一次看|數位時代 BusinessNext（僅搜尋摘要）](https://www.bnext.com.tw/article/91547/glass-substrate-concept-stocks-tgv)
- [玻璃基板是什麼？AI 晶片都需要它！台灣 14 檔「玻璃基板概念股」一次看|經理人（僅搜尋摘要）](https://www.managertoday.com.tw/articles/view/72588)
- [ABF 缺口一路到 2028，輝達為什麼還要玻璃基板？台廠誰接得到？｜新光證券（只看到標題）](https://www.skis.com.tw/Report/industry/20261005.html)
- [玻璃基板是什麼？概念股有哪些？台灣供應鏈、量產時程一次看 - 商業周刊（僅搜尋摘要）](https://www.businessweekly.com.tw/business/blog/3021521)
- [GCS和TGV是什麼？玻璃基板熱潮解析，看懂技術卡關點，與2027年商業化的關鍵倒數｜產業熱話｜豐雲學堂 2026 年 10 月（僅搜尋摘要）](https://www.sinotrade.com.tw/richclub/industry/GCS%E5%92%8CTGV%E6%98%AF%E4%BB%80%E9%BA%BC-%E7%8E%BB%E7%92%83%E5%9F%BA%E6%9D%BF%E7%86%B1%E6%BD%AE%E8%A7%A3%E6%9E%90-%E7%9C%8B%E6%87%82%E6%8A%80%E8%A1%93%E5%8D%A1%E9%97%9C%E9%BB%9E-%E8%88%872027%E5%B9%B4%E5%95%86%E6%A5%AD%E5%8C%96%E7%9A%84%E9%97%9C%E9%8D%B5%E5%80%92%E6%95%B8-%E7%94%A2%E6%A5%AD%E7%86%B1%E8%A9%B1-69cb3939435757638ebc6183)
- [TGV 概念股有哪些？玻璃通孔技術與四大量產瓶頸一次看懂（Leverage；僅搜尋摘要）](https://leveragetw.com/glass-core-substrate-tgv/)
- [玻璃基板製程突破 韓廠半天縮為幾分鐘！台廠進度曝（Yahoo奇摩；僅搜尋摘要）](https://tw.news.yahoo.com/%E7%8E%BB%E7%92%83%E5%9F%BA%E6%9D%BF%E8%A3%BD%E7%A8%8B%E7%AA%81%E7%A0%B4-%E9%9F%93%E5%BB%A0%E5%8D%8A%E5%A4%A9%E7%B8%AE%E7%82%BA%E5%B9%BE%E5%88%86%E9%90%98-%E5%8F%B0%E5%BB%A0%E9%80%B2%E5%BA%A6%E6%9B%9D-191500394.html)
- [[News] TSMC’s 5.5-Reticle CoWoS Reportedly Tops 99% Yield; Flags Memory, ABF as AI Bottlenecks（TrendForce，2026-08-11；僅搜尋摘要）](https://www.trendforce.com/news/2026/08/11/news-tsmcs-5-5-reticle-cowos-reportedly-tops-99-yield-flags-memory-abf-as-ai-bottlenecks/)
- [TSMC's details next-gen CoWoS roadmap: over 14-reticle packages and 48x leap in compute power expected by 2029 — massive size enables 24 HBM5E stacks and additional memory bandwidth jump（Tom's Hardware；僅搜尋摘要）](https://www.tomshardware.com/tech-industry/semiconductors/tsmcs-details-next-gen-cowos-roadmap-over-14-reticle-packages-and-48x-leap-in-compute-power-expected-by-2029-massive-size-enables-24-hbm5e-stacks-and-additional-memory-bandwidth-jump)
- [TSMC CoWoS Yield Tops 98% for Some Lines, Targets 14x Reticle Size by 2029（TradingKey；僅搜尋摘要）](https://www.tradingkey.com/analysis/stocks/us-stocks/262101043-tsmc-cowos-yield-breakthrough-14x-reticle-roadmap-tradingkey)
- [NVIDIA's "Rubin Ultra" Reportedly Faces Issues With CoWoS-L Packaging（TechPowerUp；只看到標題）](https://www.techpowerup.com/347889/nvidias-rubin-ultra-reportedly-faces-issues-with-cowos-l-packaging)
- [NVIDIA Abandons Quad-Die Rubin Ultra GPU as Substrate Warpage Defeats Advanced Packaging — AtlasPCB（二手部落格；僅搜尋摘要）](https://www.atlaspcb.com/news/news-nvidia-rubin-ultra-substrate-warpage-cowos-packaging-limits/)
- [Glass Core Substrate Sector Research: Part 2（AAIG Substack；僅搜尋摘要）](https://aaig.substack.com/p/glass-core-substrate-sector-research-c2b)
- [IEEE Build-Up Substrate Symposium (BUSS’25) May 8, 2025 attend.ieee.org/buss 1（IEEE 簡報；僅搜尋摘要）](https://attend.ieee.org/buss/wp-content/uploads/sites/693/T02-Vardaman.pdf)

