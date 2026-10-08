# invest-tracker

個人用的**長期資產配置追蹤器**，加上**個股技術分析儀表板**。適合定期定額、長期持有台股與美股 ETF 的投資人。

- 只做追蹤、健檢與提醒，**不下單、不串券商**。
- 只使用 Python 標準函式庫，不必安裝任何套件。
- 資料來源：[FinMind](https://finmindtrade.com/)（台股、美股、匯率、配息、營收、本益比、VIX）、FINRA（美股放空）、SEC EDGAR（美股內部人交易）、
  國發會開放資料（景氣燈號）、聯準會（FOMC 行事曆）、證交所與櫃買中心 OpenAPI（重大訊息）。

> ⚠️ 本專案是個人工具，**不構成投資建議**。所有分數、燈號和「投資參考」都是依你自己設定的投資政策，用規則計算出來的，不是持牌投顧的建議。技術訊號經回測，預測力有限。

## 功能

| 模組 | 內容 |
|---|---|
| 我的組合（首頁） | 配置相對投資政策、累積投入與市值、和 0050／VOO 比較（同樣的投入時間與金額）、回撤、定期定額進度 |
| 股利行事曆 | 未來 12 個月每筆股利的除息日、入帳日、補充保費／預扣稅與淨額；區分已公告、日期已公告、推估 |
| 近期事件 | 未來 60 天的除息、入帳、扣款日、月營收與財報期限、FOMC 利率決議 |
| 市場溫度計 | 景氣燈號、加權指數年線乖離、融資餘額變化、VIX，各自相對歷史的百分位 |
| 基本面 | 台股個股月營收（年增、月增、累計）與本益比／淨值比／殖利率的 5 年百分位（儀表板卡片，網頁版也有） |
| 新聞與重大訊息 | 個股新聞標題（合併重複）與證交所／櫃買中心重大訊息；持股有新的重大訊息時每晚推到 Telegram |
| 每週摘要 | 每週五自動產生：本週表現、漲跌貢獻、健檢、未來兩週事件；可選擇用 Telegram 推送（預設不含金額） |
| 帳本 | 用對話或 CLI 記錄成交、定期定額、股息再投資；扣款遇假日順延、股票分割都會自動處理 |
| 健檢 | 依 `policy` 的門檻檢查：地區分散、高股息占比、單一個股、單一公司穿透曝險、定額偏離、歷史回撤、美國遺產稅、漏記、券商對帳 |
| 月報 | HTML 月報：5 年含息回測、最大回撤、股利現金流（含二代健保與美股預扣稅）、配置圖 |
| 情境模擬 | 輸入調整方案，比較調整前後的健檢結果，不會寫進帳本 |
| 技術分析儀表板 | 18 格綜合分析（K 線、KD、MACD、價量分布、法人、當沖、訊號歷史勝率等），每個分數都有公式說明 |
| 美股 | 美股大盤、匯率影響、FINRA 放空比例與放空餘額、SEC Form 4 內部人交易 |
| 自動化 | macOS launchd：每晚更新並通知、每晚新聞、每週摘要、每月月報、開機補跑漏掉的排程、儀表板常駐 |
| 手機 | 儀表板和月報都支援手機版面；`serve --lan` 讓同一個 Wi-Fi 的手機用通行碼瀏覽 |

## 線上網頁版（不用安裝）

**https://masterbobobo.github.io/PokeBO/invest-tracker/web/**

在瀏覽器裡直接執行和本機版相同的 Python 分析程式（使用 [Pyodide](https://pyodide.org/)），手機也能用。
第一次打開要下載約 10MB，之後瀏覽器會快取；抓到的資料存在瀏覽器的 IndexedDB 裡。

- 網站內建一組共用的 FinMind token，打開就能用；額度由所有訪客共用（每小時 600 次）。
- 常用的話，建議按「設定 Token」改貼**你自己的** FinMind token（免費註冊），只會存在你的瀏覽器（localStorage），並且優先於共用 token。
- 網頁版提供：個股技術分析的所有分頁、台股法人／融資／當沖、美股大盤、匯率影響、FINRA 每日放空比例（近 20 日）。
- 網頁版也提供台股個股的基本面卡片（月營收、估值百分位）和近 3 天新聞；重大訊息只在本機版。
- 只在本機版提供：我的組合首頁、股利行事曆、近期事件、市場溫度計、每週摘要、持倉、記帳、月報、健檢、依投資政策的白話結論、FINRA 放空餘額、SEC 內部人交易（後兩項受瀏覽器跨網域限制）。

## 快速開始（本機版）

```bash
git clone <this repo> && cd invest-tracker

# 1. FinMind token（免費註冊：https://finmindtrade.com/）
echo "FINMIND_TOKEN=你的token" > .env.local && chmod 600 .env.local

# 2. 建立個人設定（private/ 不會被 git 追蹤）
mkdir -p private && cp config.example.json private/config.json
#    修改 private/config.json：plans（定期定額）、policy（投資政策門檻）、sec_user_agent（填你的 email，SEC 規定）

# 3. 抓資料、記錄持股
python3 invest.py update
python3 invest.py buy 0050 1000 60.5 --date 2026-01-02 --source opening   # 期初部位（price 填平均成本）
python3 invest.py report

# 4. 儀表板與月報
python3 invest.py serve        # 我的組合 http://127.0.0.1:8765/ ，技術分析 /ta
python3 invest.py html         # reports/YYYY-MM.html

# 5.（選用）macOS 自動排程
python3 invest.py launchd --install

# 6.（選用）用手機看：同一個 Wi-Fi 下開啟區域網路模式（需要通行碼，只在家裡等可信任網路使用）
python3 invest.py serve --lan
```

需要 Python 3.10 以上。macOS 使用 python.org 版本時，程式會自動改用系統的 `/etc/ssl/cert.pem` 憑證。

## 常用指令

```bash
python3 invest.py update                      # 增量更新股價、配息、匯率
python3 invest.py report [--json]             # 投資組合報告
python3 invest.py buy SYMBOL SHARES PRICE --date YYYY-MM-DD [--fee N] [--source actual|opening|drip]
python3 invest.py simulate private/scenarios/xxx.json   # 情境模擬
python3 invest.py reconcile private/reconcile/YYYY-MM-DD.csv [--apply-cost]   # 和券商庫存對帳
python3 invest.py ta 2330                     # 個股技術分析離線報告
python3 invest.py quota                       # FinMind API 本小時用量
python3 invest.py backup                      # 把 private/ 的變動 commit 到本機 git
python3 -m unittest discover tests            # 測試
```

搭配 [Claude Code](https://claude.com/claude-code) 使用時，可以用 `/portfolio`、`/buy`、`/reconcile`、`/ta` 這些快捷指令，直接用口語記帳和健檢。

## 個人資料放在哪裡

| 路徑 | 內容 | 會上傳 GitHub 嗎 |
|---|---|---|
| `private/` | 個人設定、持倉帳本、情境、對帳檔（本機獨立 git 備份） | ❌ |
| `CLAUDE.local.md` | 給 Claude Code 讀的個人說明 | ❌ |
| `.env.local` | API token | ❌ |
| `data/`、`reports/`、`logs/` | 快取、月報、日誌 | ❌ |
| 其他 | 程式碼、範例設定、文件 | ✅ |

## 資料來源與限制

- FinMind 免費方案每小時 600 次；資料為 T+1，**沒有盤中即時資料**。券商分點和股權分散是付費資料，所以本專案不提供。
- 美股配息由還原價反推，可能和實際入帳有些微差異。
- FINRA 每日放空比例包含造市商的避險放空，平常就有 40–60%，要和這檔股票自己的歷史相比才有意義。
- 13F 機構持股、ETF 成分股的自動更新需要付費 API；ETF 成分權重目前在 `config.json > look_through` 手動維護。
- 使用各資料來源時，請遵守其服務條款。

## 授權

MIT License，詳見 [LICENSE](LICENSE)。
