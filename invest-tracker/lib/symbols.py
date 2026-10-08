"""搜尋框的股票清單：台股上市、上櫃（含 ETF）＋美股，讓使用者輸入代號前幾碼或公司名稱就能選。

- 台股：沿用 ta.stock_info 的 data/stock_info.csv（FinMind TaiwanStockInfo，7 天更新一次）；興櫃沒有日 K，排除。
- 美股：FinMind USStockInfo（約 3MB），整理成 data/us_symbols.csv，7 天更新一次；只留近 400 天仍有資料的代號。
回傳精簡格式 [[代號, 名稱, 市場, 類別]]，前端自行比對。
"""
import csv
import re
from datetime import date, timedelta

from . import data, ta

US_PATH = data.DATA / "us_symbols.csv"
_NAME_NOISE = re.compile(r"\s+(Class [A-Z] )?(Common Stock|Ordinary Shares|Common Shares|American Depositary Shares)\b.*$", re.I)


def _stale(path, days=7):
    return not path.exists() or date.fromtimestamp(path.stat().st_mtime) < date.today() - timedelta(days=days)


def tw_list():
    ta.stock_info("2330")                       # 觸發 7 天一次的更新
    with ta.INFO.open(encoding="utf-8") as f:
        return [[r["stock_id"], r["stock_name"], "TW", "ETF" if r["stock_id"].startswith("00") else r["industry_category"]]
                for r in csv.DictReader(f) if r["type"] in ("twse", "tpex")]


def update_us():
    rows = {}
    for r in data.fetch("USStockInfo", "", "2020-01-01"):
        if r["stock_id"] not in rows or r["date"] > rows[r["stock_id"]]["date"]:
            rows[r["stock_id"]] = r
    cutoff = (date.today() - timedelta(days=400)).isoformat()
    out = [{"symbol": s, "name": _NAME_NOISE.sub("", (r.get("stock_name") or "").strip()) or s,
            "sector": r.get("Subsector") or ""}
           for s, r in sorted(rows.items()) if r["date"] >= cutoff and re.fullmatch(r"[A-Z0-9.^-]{1,10}", s)]
    data._write(US_PATH, out, ["symbol", "name", "sector"])
    return len(out)


def us_list():
    if _stale(US_PATH):
        update_us()
    return [[r["symbol"], r["name"], "US", "ETF" if r["sector"] == "ETF" else ""] for r in data._read(US_PATH)]


def all_symbols():
    out = tw_list()
    try:
        out += us_list()
    except data.QuotaError:
        raise
    except Exception:              # 美股清單抓不到時，台股照常可以搜尋
        pass
    return out
