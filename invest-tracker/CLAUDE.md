# invest-tracker — 長期資產配置追蹤器（Claude Code 說明）

長期定期定額組合的追蹤、健檢與提醒系統。只做**追蹤與提醒**，不下單、不接券商 API。

- 個人設定與紀錄在 `CLAUDE.local.md`（如果有）、`private/config.json`、`private/positions.json`。**這些都不會上傳，也不要把內容寫進這個檔案或任何會被追蹤的檔案。**
- 回覆時：結論先行，附上風險與取捨；**不提供個人化的買賣建議**，技術面判讀和配置決策要分開說明。
- 投資政策門檻集中在 `private/config.json > policy`。門檻代表使用者的投資政策，不能為了讓檢查變成通過而調整。

## 指令
```bash
python3 invest.py update                 # 增量更新股價/配息/匯率（FinMind，T+1，收盤後 14:30 以後）
python3 invest.py report                 # 文字報告
python3 invest.py report --json          # 完整 JSON（給 Claude 分析用，含 health/risk/dividend_outlook）
python3 invest.py html                   # 月報 HTML → reports/YYYY-MM.html
python3 invest.py simulate private/scenarios/example.json   # 模擬調整方案後的 Health Check（不寫入帳本）
python3 invest.py serve                  # 我的組合 http://127.0.0.1:8765/ ；技術分析 /ta
python3 invest.py ta 2330                # 個股技術分析離線報告 → reports/ta/2330_YYYY-MM-DD.html
python3 invest.py quota                  # FinMind API 本小時已用次數（官方）+ 本機各資料集呼叫明細
python3 invest.py buy 0050 176 113.2 --date 2026-10-27 --fee 28   # 記錄實際成交
python3 invest.py buy VOO 0.0565 707.5 --date 2026-10-26           # 美股碎股
python3 invest.py buy 2882 1000 70 --date 2026-11-03 --source opening   # 補建期初部位（price=平均成本）
```

## 記帳完整性（漏記偵測）
- `tracking_start` 之後，扣款日（遇假日順延）已有交易資料、但帳本沒有該月成交 → 判定漏記定額扣款。
- `plans.*.drip`：除息超過 35 天仍沒有 `--source drip` 的買進紀錄 → 判定漏記股息再投資。
- 每日排程發現漏記會通知提醒，之後每 7 天再提醒一次，補記後自動停止；健檢「扣款記帳」也會列出漏記項目。
- 股息再投資的記法：`python3 invest.py buy 0056 85 57.2 --date 2026-11-20 --source drip`

## 券商對帳（/reconcile）
- 券商庫存寫成 `private/reconcile/YYYY-MM-DD.csv`（symbol,shares,cost,avg_cost,note），執行 `invest.py reconcile <檔案>`。
- 股數差異只列出可能原因，不會自動修正（要用 buy 補記）；成本差異可以用 `--apply-cost` 校正期初均價，**執行前要先問使用者**。
- 台股截圖沒有均價時，cost 欄留空，不要反推。
- 健檢「券商對帳」：超過 95 天沒對帳，或有差異未處理時，顯示 ⚠️ 風險。

## 對話記帳規則
使用者說「0050 這個月扣款成交 176 股、均價 113.2、手續費 28」時：
1. 執行 `buy`，日期沒講就用該計畫本月扣款日之後第一個交易日（查 data/prices/）。
2. `buy` 會自動取代同一扣款月份（plan_month）的估算交易，不需手動刪除。
3. 記完執行 `report`，回覆該筆交易與最新總損益。
- 賣出：shares 用負數。
- 成交日早於扣款日時，系統會將它歸到上個月的扣款（遇假日順延）；需要時可用 `--month` 指定。

## 資料
- `data/prices/{TW|US}_{symbol}.csv`：日 K（未還原，股利另計）
- `data/dividends/TW_{symbol}.csv`：台股現金股利（除息日、每股金額）
- `data/fx/USD.csv`：台銀即期匯率；美股成本以買入日 spot_sell 換算，市值以最新 spot_buy 換算
- `private/positions.json`：交易帳本，`source` 欄：`opening`（期初建檔）、`actual`（實際成交）、`drip`（股息再投資）、`estimated`（估算）
- 憑證：`.env.local` 的 `FINMIND_TOKEN`（勿印出、勿提交）

## FinMind API 額度
- 免費方案每小時 600 次。官方用量來自 `https://api.web.finmindtrade.com/v2/user_info`（查詢本身不算次數，但數字會延遲數分鐘更新）。
- 每次呼叫都記錄在 data/state/api_calls.log（只保留 2 天），用來看是哪個資料集在消耗額度。
- 官方用量達 98% 時，`data.fetch` 會丟出 QuotaError 並暫停呼叫；FinMind 回傳 402 或 429 時也會轉成 QuotaError。
- 儀表板頂部顯示用量（每 60 秒更新，50% 以上轉黃、80% 以上轉紅）；每日排程發現用量 ≥ 80% 時會通知。
- 用量估算：分析一檔台股個股約 9 次呼叫（含基本面 2 次；同一天會用快取）、大盤每天 3 次、每日排程約 45 次（含已公告配息、市場溫度計）。

## 計算方式
- 股利：台股來自 FinMind TaiwanStockDividendResult；美股 FinMind 沒有配息資料，由 Adj_Close/Close 比值的跳動反推。
  台股單筆 ≥ NT$20,000 扣 2.11% 二代健保，美股扣 30% 預扣稅（`private/config.json > tax`）。
- 風險（lib/risk.py）：以目前股數不變回測 5 年含息總報酬，計算最大回撤、回到前高的日期和年化波動度。
  上市未滿回測期的標的用 `backtest_proxies` 指定的代理標的往前延伸（例如新上市的高股息 ETF 用 0056 代替），沒有代理的標的就排除。
- Health Check 規則在 `lib/report.py > health_checks`，報告和月報共用。

## 已知限制
- 台股股利未扣匯費；美股股利是由還原價反推，可能和實際入帳略有差異。
- 估算交易假設以扣款日收盤價成交，與實際成交價會有小差異。
- XIRR 從建檔日起算（期初部位以建檔日市值當作投入），追蹤滿 90 天才會顯示。
- 報告的市值用 FinMind 的 T+1 收盤價，和券商 App 的盤中市值會有差異。

## 情境模擬（private/scenarios/）
情境檔格式見 `lib/scenario.py` 開頭的說明；執行後只顯示比較結果，不寫入帳本。

## 自動化（launchd，用 `python3 invest.py launchd --install` 產生並安裝）
| Label | 時間 | 內容 |
|---|---|---|
| <launchd_prefix>.daily | 週一到週五 22:00 | 更新行情 → Health Check 狀態有變化才通知 → git 備份 |
| <launchd_prefix>.news | 每天 22:05 | 抓重大訊息與持股新聞 → 持股有新的重大訊息（或美股個股 8-K）才推播，同一則只推一次 |
| <launchd_prefix>.weekly | 每週六 09:00 | 更新行情 → 每週摘要：本機存完整版 reports/weekly/，通知只送不含金額的版本 |
| <launchd_prefix>.monthly | 每月 28 日 22:15 | 更新行情 → 產出 reports/YYYY-MM.html → 通知摘要 |
| <launchd_prefix>.catchup | 登入（開機）時 | 補跑關機期間錯過的排程（依 data/state/job_runs.json）；睡眠中錯過的由 launchd 醒來後自動補 |
| <launchd_prefix>.dashboard | 登入時啟動、常駐 | 我的組合 http://127.0.0.1:8765/ 、技術分析 /ta |
- 排程時間集中在 `lib/jobs.py > SCHEDULE`，改時間後執行 `invest.py launchd --install`。
- 手動執行：`python3 invest.py job daily|news|weekly|monthly|catchup`；log 在 logs/jobs.log。預覽每週摘要：`python3 invest.py weekly [--amounts]`。
- 通知：預設用 macOS 通知中心，且不顯示金額（`private/config.json > notify.include_amounts=false`）。
  Telegram 是選用功能：在 .env.local 加上 TELEGRAM_BOT_TOKEN，傳一則訊息給 bot 後執行 `invest.py telegram-test`
  （自動寫入 TELEGRAM_CHAT_ID 並送測試訊息），再設定 notify.telegram=true。Telegram 是外部服務：預設只送百分比、狀態與日期，不送金額。
- 停用排程：`launchctl bootout gui/$(id -u)/<launchd_prefix>.<name>`。
- 備份：`python3 invest.py backup`（buy、daily、monthly 會自動執行），commit 到 private/ 的本機 git（不推送）。
- 公開 repo（GitHub）只有程式碼；個人資料都在 private/、CLAUDE.local.md、data/、reports/，全部被 .gitignore 排除。
  舊的混合歷史封存在 private/legacy-history.git，**絕對不要推送**。
- 測試：`python3 -m unittest discover tests`。

## 穿透曝險（config.json > look_through）
- ETF 成分權重是手動維護的（請註明來源與資料日），超過 120 天沒更新，Health Check 會顯示 Risk 提醒更新。
- ETF 內含的台股公司（例如 SMH 裡的台積電 ADR）可以用台股代號（2330）計入穿透曝險。QQQM（Nasdaq-100）和 VOO（S&P 500）都不含台積電。
- 單一公司曝險 = 直接持股 + ETF 市值 × 成分權重。只涵蓋設定中有權重的 ETF（目前只有 0050），高股息 ETF 和 SMH 裡的台積電沒有計入。

## 我的組合（長期持有者首頁：lib/home.py、lib/divcal.py、web/home.html）
- 本機 `serve` 的首頁 `/`，資料來自 `/api/home`；技術分析移到 `/ta`（舊網址 `/?id=` 會轉址）。網頁版沒有持倉，打開 home.html 只顯示提示。
- 卡片：總覽數字、配置 vs 投資政策（狀態沿用健檢結果，不另算）、持倉配置、累積投入與市值、和大盤比較、回撤、定期定額、股利行事曆、健檢。
- 和大盤比較（`home.benchmark`）：每一筆投入改在同一天買進 `config.json > benchmarks`（預設 0050、VOO）的含息總報酬指數。
  期初部位以建檔日市值當作投入，和 XIRR 一致；美股用買入日 spot_sell 換匯、最新 spot_buy 計值。
- 美國遺產稅倒數：依目前美股每月定額、不計漲跌，推估多久達到免稅額和警示線。
- 股利行事曆（divcal）：已公告（TaiwanStockDividend，`data/dividends/TW_{symbol}_announce.csv`，每天最多抓一次）＞日期已公告、金額用最近一次配息推估＞依去年同期推估。
  已除息、尚未入帳的列為「待入帳」，期初部位也算。股數用目前持股，不計入之後的扣款與再投資。
  美股入帳日用除息日 + 5 天推估；台股沒有歷史發放日時用 + 25 天。
- 只呈現事實與政策比較，不產生買賣建議。
- 近期事件（lib/events.py，未來 60 天）：除息／入帳、定額扣款日、台股個股月營收期限（每月 10 日）、
  財報法定期限（一般公司 3/31、5/15、8/14、11/14；金融業 3/31、5/30、8/31、11/29）、FOMC 利率決議。法說會沒有免費來源，不提供。
- 市場溫度計（lib/market.py）：國發會景氣對策信號（data.gov.tw 開放資料 6099，每 7 天最多下載一次）、加權指數 240 日乖離百分位、
  整體融資餘額 20 日變化百分位、VIX（FinMind USStockPrice ^VIX）。FOMC 日期解析聯準會行事曆頁面。
  百分位 > 80 偏熱、< 20 偏冷（VIX 相反）；只描述位置，不是進出場訊號。資料在 update／每日排程更新，首頁只讀快取。
  國發會、聯準會沒有 CORS，只在本機版使用。
- 每週摘要（lib/weekly.py）：組合與大盤同期報酬、漲跌貢獻（百分點）、健檢、未來兩週事件、溫度計、漏記提醒。
  起算日和組合報酬一致（建檔不滿 5 個交易日時從建檔日起算）。

## 新聞與重大訊息（lib/news.py）
- 新聞：FinMind TaiwanStockNews，**每次呼叫只回傳 start_date 當天**（不支援 end_date），所以按日期分檔快取
  `data/news/{代號}/{日期}.json`：過去的日子抓過就不再抓，今天 3 小時內用快取。時間是 UTC，顯示時 +8。同標題合併來源。
- 重大訊息：證交所 t187ap04_L（上市）＋櫃買中心 mopsfin_t187ap04_O（上櫃），OpenAPI 只給最近一天，每晚累積到
  `data/news/material.csv`（保留 90 天）。兩個網域的憑證缺 Subject Key Identifier，`data.LENIENT_HOSTS` 只對它們關掉 VERIFY_X509_STRICT。
  沒有 CORS，只在本機版。美股個股：SEC 8-K（ETF 沒有）。
- 技術分析頁「新聞與公告」卡片走獨立的 `/api/news`（網頁版是 worker 的 news 指令），不跟技術分析的每日快取。
- 首頁「持股新聞」只讀快取（cached_only），資料由每晚 news 排程預先抓好。推播紀錄在 data/state/news_sent.json。
- 只列標題、來源、連結，不做利多利空判讀。

## 個股技術分析儀表板（lib/ta.py、lib/server.py、web/dashboard.html）
- 只呈現能從資料直接計算的指標：MA5/10/20/60、KD(9,3,3)、MACD(12,26,9)、RSI(14)、布林(20,2)、ATR(14)、
  20 日成交均價、支撐壓力、52 週高低、三大法人、融資融券。**不加入推估或模擬的「AI 分數」**。
- 趨勢檢核是規則式（台股 8 項、美股 5 項），技術警示和訊號時間軸由 `warnings()` / `signals()` 產生。
- 伺服器只綁 127.0.0.1，FinMind token 不會傳到瀏覽器；同一天的分析結果快取在 data/ta_cache/。
- 定期定額標的會顯示提醒：技術訊號只當參考，不影響扣款紀律。
- 網址參數：`?id=2330&tab=macd`（分頁：overview / alerts / kd / macd / chips / raw）。
- 「綜合分析」分頁有 18 格（lib/ta_plus.py）：主 K 線（含支撐／壓力／均價／外資估算成本線）、決策摘要、多維度雷達、
  價量分布、風險雷達、10 日歷史區間（歷史分布，不是預測）、成本結構、法人行為、當沖與週轉、多空能量、健康指標、
  信號燈、大盤狀態、訊號歷史勝率（本檔約 3 年回測，對照基準）、趨勢檢核、關鍵價位、我的持倉、總評。
  每個分數都有 ⓘ 可以看公式和原始數值。
- 判讀用語一律是「技術面偏強／中性／偏弱」，**不用「偏多／偏空」**（避免被誤解成加減碼建議）。
  決策摘要和總評都附「這不是買賣建議」說明：加減碼依月報健檢的配置政策，定期定額照原設定扣款。
  回答使用者時也要維持這個區分。
- 總評的「投資參考（依你的投資政策）」（`ta_plus.guidance`）：依 config.json 的投資政策和目前持倉，由規則產生
  （定額照扣、類別或個股相對政策上下限、台積電穿透曝險、一次性買入分批）。**不寫買進／賣出數量或價格**，
  並附上「不是持牌投顧個人化建議」的聲明。持倉占比來自 `report.snapshot()`（data/state/portfolio_snapshot.json，每天計算一次）。
- 「💬 白話結論」（`ta_plus.plain_summary`）：把投資參考翻成一句白話，每個結論都直接對應一條政策規則
  （定額照扣、超標不加碼、低於下限是候選、台積電曝險、分批執行）。**不依股價走勢下「現在買／該賣」的結論**：
  技術訊號經回測沒有預測力，而且這是個人化的投資建議。使用者要求買賣建議時，要說明這個界線。
- 美股籌碼（lib/us_chips.py，官方免費資料，不計入 FinMind 額度）：
  - FINRA 每日放空成交量比例（cdn.finra.org，要帶 User-Agent，否則回傳 403）：看近 5 日相對近 60 日的 z 值，不要看絕對值（造市商讓基準落在 40–60%）。
  - FINRA 放空餘額與回補天數（api.finra.org consolidatedShortInterest，每月兩次）。
  - SEC Form 4 內部人交易（要帶 `private/config.json > sec_user_agent`，請填入自己的 email）；只有 P／S 有參考價值，ETF 不適用。
  - FMP 免費金鑰（.env.local 的 FMP_API_KEY）只能用 profile（Beta、市值、52 週區間）；ETF 成分股、機構持股、內部人統計都回傳 402（付費方案限定）。
    所以 13F 機構持股仍未提供；ETF 成分權重改成手動維護。
  - 每日排程會預先抓取持有美股的資料；FINRA 每日檔案快取在 data/finra/daily（保留 120 天）。
- 基本面卡片（lib/fundamentals.py，台股個股才有，ETF 和美股顯示不適用）：月營收年增／月增／累計年增、下次公布期限；
  本益比、股價淨值比、殖利率的近 5 年百分位（虧損期間沒有本益比，不列入）。網頁版也有（已加入 worker.js 的 MODULES）。
- 搜尋框提示（lib/symbols.py，`/api/symbols`，網頁版是 worker 的 symbols 指令）：台股上市櫃（含 ETF，排除興櫃）＋美股
  （FinMind USStockInfo 整理成 data/us_symbols.csv，7 天更新）。輸入代號前幾碼或名稱即列出，排序：完全相符 → 代號開頭 → 名稱開頭 → 名稱包含，
  同分時持股優先、台股優先。第一次點搜尋框才載入清單。
- X 軸是類別軸，標籤要唯一：`lbl()` 在區間超過約 11 個月時改用「年/月/日」，否則去年和今年同一天會畫在同一格。
- 網站圖示：我的組合、月報、報告頁用金色箭頭（web/favicon-16/32.png 只裁主箭頭、icon-192.png、apple-touch-icon.png）；
  技術分析頁用放大鏡（web/ta-*.png）。本機伺服器另外提供 /favicon.ico。
  apple-touch-icon 必須是**不透明、滿版正方形、主圖內縮留邊**：iOS 會自己切圓角並把透明處補黑，
  圓形或自帶圓角的圖貼邊會被切到（看起來爆框）。
- 天數篩選：1 / 5 / 7 / 60 / 120 / 250 日，只影響圖表顯示範圍；指標和評分都以完整歷史計算。沒有盤中分時資料。
- **刻意不做**：券商分點（主力、隔日沖）和股權分散（大戶／散戶）是 FinMind 付費資料，不估算、不顯示假數字。
- 效能：圖表 responsive 關閉，改用 ResizeObserver 只在寬度改變時重排；捲動時暫停圖表 hover；分頁延遲繪製。
- 分析快取檔名含 `CACHE_VERSION`，分析輸出的欄位有變動時要加 1。

## Health Check 重點（/portfolio）
台股和美股比例、高股息 ETF 占比、台積電實際曝險（0050 成分股 + 2330 個股）、金融股集中度、
美股定額偏離、SMH 的半導體集中度，以及資料是否為最新交易日。

## 網頁版（web/worker.js + Pyodide）
- `web/dashboard.html` 會自動判斷模式：能連到 `/api/quota` 就是 server 模式（本機伺服器），否則是 web 模式（GitHub Pages）；內嵌資料的是 offline 模式。
- web 模式由 `web/worker.js` 在 Web Worker 裡載入 Pyodide，從 `../lib/*.py` 讀進同一套 Python 程式。
  `data.WEB = True` 時：FinMind token 改用網址參數（帶 Authorization 標頭會觸發預檢，FinMind 預檢回 400）、
  HTTP 改用同步 XHR（`data.set_transport`）、沒有執行緒（`data.pmap` 改成依序處理）、不產生個人化的投資參考。
- 新增網路呼叫時，一律使用 `data.http_get` / `data.http_post` / `data.auth`，不要直接用 urllib，否則網頁版會壞掉。
- 瀏覽器跨網域限制：FINRA 放空餘額（POST 沒有 CORS）、SEC（代號對照表回 403）在網頁版停用。
- `web/config.js` 設定 `window.DEFAULT_FINMIND_TOKEN`（公開的共用 token，請用獨立帳號，不要和本機版共用同一把）。
