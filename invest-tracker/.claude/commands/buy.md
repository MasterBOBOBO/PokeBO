---
description: 用自然語言記錄一筆成交（含定額扣款、股息再投資）
---
依 CLAUDE.md「對話記帳規則」記錄以下成交：
- 使用者提到「股息再投資」「股利再投入」時，加上 `--source drip`。
- 使用者貼的是券商成交截圖時，逐筆讀出標的、日期、股數、成交價、手續費後記錄，記錄前先列出讀到的內容。
記完執行 `python3 invest.py report --json`，回覆這筆交易、最新總損益，以及剩下的漏記項目（JSON `missing`）。
$ARGUMENTS
