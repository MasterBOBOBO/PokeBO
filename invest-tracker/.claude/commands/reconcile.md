---
description: 和券商庫存對帳（可直接貼券商 App 截圖）
---
1. 從使用者提供的券商庫存截圖或 CSV 讀出每檔的 symbol、shares，以及能取得的總成本（cost）或均價（avg_cost）。
   - 台股截圖只有「損益 + 報酬率」時，cost 留空，不要反推（反推等於拿帳本自己對自己）。
   - 讀出後先列表請使用者確認，避免把截圖讀錯。
2. 寫入 `data/reconcile/<今天日期>.csv`（欄位：symbol,shares,cost,avg_cost,note），執行
   `python3 invest.py reconcile data/reconcile/<今天日期>.csv`。
3. 以繁體中文回覆對帳結果：
   - ❌ 股數差異：列出差幾股與可能原因，詢問實際成交後用 /buy 補記
   - ⚠️ 成本差異：說明差額，**先問使用者同意**，再加上 `--apply-cost` 重跑
   - ✅ 全部一致：告知下次對帳期限（95 天內）
$ARGUMENTS
