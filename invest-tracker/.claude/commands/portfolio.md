---
description: 更新行情並產出投資組合健檢與月報
---
1. 執行 `python3 invest.py update`，再執行 `python3 invest.py report --json` 和 `python3 invest.py html`。
2. 以繁體中文回覆，格式：
   - **重點摘要**：市值、總損益、預估年股利淨額、回測最大回撤（各一句話）
   - **健檢**：引用 JSON `health` 欄位（✅ 通過 / ⚠️ 風險 / ❌ 問題 / ℹ️ 資訊），只補充有變化的項目
   - **待記帳**：列出 JSON `missing` 的漏記項目（定額扣款、股息再投資），沒有就寫「無」
   - **本月變化**：和上一份 reports/*.html 相比的重點（沒有上一份就略過）
   - **下一步**：只談紀律（記帳、扣款、對帳、是否再平衡、稅務），不給短線買賣建議
   - 最後附上月報檔案路徑
$ARGUMENTS
