"""FinMind 抓取 + 本地 CSV 快取（增量更新）。"""
import csv
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

try:                     # 瀏覽器（Pyodide）環境預設沒有 ssl 模組
    import ssl
except ImportError:
    ssl = None

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"                 # 快取（可重新下載）
PRIVATE = ROOT / "private"           # 個人設定與帳本：不上傳 GitHub，另以本機 git 備份
API = "https://api.finmindtrade.com/api/v4/data"
# python.org 版 Python 在 macOS 沒有根憑證，改用系統內建的
_CA = "/etc/ssl/cert.pem"
SSL_CTX = ssl.create_default_context(cafile=_CA if os.path.exists(_CA) else None) if ssl else None

# 網頁版（GitHub Pages + Pyodide）會把 WEB 設為 True，並用 set_transport() 換成瀏覽器的 XHR：
# - FinMind token 改用網址參數（帶 Authorization 標頭會觸發預檢，而 FinMind 預檢回 400）
# - 不能自訂 User-Agent、沒有執行緒
WEB = False


def _urllib_request(method, url, body=None, headers=None, timeout=30):
    req = urllib.request.Request(url, body, headers or {}, method=method)
    with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
        return r.read()


_transport = _urllib_request


def set_transport(fn):
    """fn(method, url, body, headers, timeout) -> bytes；HTTP 錯誤要丟 urllib.error.HTTPError。"""
    global _transport
    _transport = fn


def http_get(url, headers=None, timeout=30):
    return _transport("GET", url, None, headers, timeout)


def http_post(url, body, headers=None, timeout=30):
    return _transport("POST", url, body, headers, timeout)


def auth(url):
    """回傳 (url, headers)：本機用 Authorization 標頭，網頁版改用 token 網址參數。"""
    if WEB:
        return f"{url}{'&' if '?' in url else '?'}token={urllib.parse.quote(_token())}", {}
    return url, {"Authorization": f"Bearer {_token()}"}


def pmap(fn, items, workers=6):
    """平行處理；瀏覽器沒有執行緒時改成依序處理。"""
    items = list(items)
    if WEB:
        return [fn(x) for x in items]
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(fn, items))


def load_config():
    """個人設定在 private/config.json；還沒建立時使用 config.example.json。"""
    path = PRIVATE / "config.json"
    if not path.exists():
        path = ROOT / "config.example.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _token():
    token = os.environ.get("FINMIND_TOKEN")
    env = ROOT / ".env.local"
    if not token and env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("FINMIND_TOKEN="):
                token = line.split("=", 1)[1].strip()
    if not token:
        raise RuntimeError("找不到 FINMIND_TOKEN：本機請寫入 .env.local；網頁版請在頁面上設定 token")
    return token


# ---------- API 額度 ----------

USER_INFO = "https://api.web.finmindtrade.com/v2/user_info"
CALL_LOG = DATA / "state" / "api_calls.log"
_quota_cache = {"t": 0, "v": None}
QUOTA_BLOCK = 0.98        # 官方已用量達上限 98% 就先擋下，避免被 FinMind 拒絕


class QuotaError(RuntimeError):
    pass


def quota(max_age=30):
    """FinMind 官方回報的本小時已用次數（快取 max_age 秒）。查詢失敗時回傳 None。"""
    import time
    if _quota_cache["v"] and time.time() - _quota_cache["t"] < max_age:
        return _quota_cache["v"]
    try:
        u = json.loads(http_get(*auth(USER_INFO), timeout=15))
        limit = u.get("api_request_limit_hour") or u.get("api_request_limit")
        v = {"used": u["user_count"], "limit": limit, "level": u.get("level_title"),
             "pct": u["user_count"] / limit if limit else None, "source": "FinMind 官方"}
    except Exception:
        v = None
    _quota_cache.update(t=time.time(), v=v)
    return v


def _log_call(dataset, data_id):
    from datetime import datetime
    CALL_LOG.parent.mkdir(parents=True, exist_ok=True)
    with CALL_LOG.open("a", encoding="utf-8") as f:
        f.write(f"{datetime.now().isoformat(timespec='seconds')}\t{dataset}\t{data_id}\n")


def local_usage(minutes=60):
    """本機近 N 分鐘的呼叫紀錄（依資料集分類），用來看哪個功能在消耗額度。"""
    from datetime import datetime, timedelta
    if not CALL_LOG.exists():
        return {"total": 0, "by_dataset": {}}
    since = (datetime.now() - timedelta(minutes=minutes)).isoformat(timespec="seconds")
    lines = CALL_LOG.read_text(encoding="utf-8").splitlines()
    recent = [ln.split("\t") for ln in lines if ln[:19] >= since]
    by = {}
    for _, ds, _ in recent:
        by[ds] = by.get(ds, 0) + 1
    # 只保留最近 2 天的紀錄
    if len(lines) > 5000:
        keep = (datetime.now() - timedelta(days=2)).isoformat(timespec="seconds")
        CALL_LOG.write_text("\n".join(ln for ln in lines if ln[:19] >= keep) + "\n", encoding="utf-8")
    return {"total": len(recent), "by_dataset": dict(sorted(by.items(), key=lambda x: -x[1]))}


def fetch(dataset, data_id, start, end=None):
    q = _quota_cache["v"]
    if q and q["pct"] is not None and q["pct"] >= QUOTA_BLOCK:
        q = quota(max_age=0)      # 重新確認，可能已經進入下一個小時
        if q and q["pct"] >= QUOTA_BLOCK:
            raise QuotaError(f"FinMind 本小時已用 {q['used']}/{q['limit']} 次，已暫停呼叫，請稍後再試")
    params = {"dataset": dataset, "data_id": data_id, "start_date": start}
    if end:
        params["end_date"] = end
    url, headers = auth(f"{API}?{urllib.parse.urlencode(params)}")
    try:
        body = json.loads(http_get(url, headers, timeout=30))
    except urllib.error.HTTPError as e:
        if e.code in (402, 429):
            raise QuotaError("FinMind 回報已達每小時呼叫上限，請稍後再試") from e
        raise
    finally:
        _log_call(dataset, data_id)
        if _quota_cache["v"]:
            _quota_cache["v"]["used"] += 1          # 本地先累加，下次查官方時校正
            lim = _quota_cache["v"]["limit"]
            _quota_cache["v"]["pct"] = _quota_cache["v"]["used"] / lim if lim else None
    if body.get("status") != 200:
        raise RuntimeError(f"FinMind {dataset}/{data_id}: {body.get('msg')}")
    return body["data"]


# ---------- CSV 快取 ----------

def _read(path):
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _write(path, rows, fields):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def _merge(path, new_rows, fields):
    rows = {r["date"]: r for r in _read(path)}
    for r in new_rows:
        rows[r["date"]] = r
    _write(path, [rows[d] for d in sorted(rows)], fields)
    return len(new_rows)


def _next_start(path, default):
    rows = _read(path)
    if not rows:
        return default
    return (date.fromisoformat(rows[-1]["date"]) + timedelta(days=1)).isoformat()


PRICE_FIELDS = ["date", "open", "high", "low", "close", "volume"]
US_FIELDS = PRICE_FIELDS + ["adj_close"]


def price_path(market, symbol):
    return DATA / "prices" / f"{market}_{symbol}.csv"


def update_prices(market, symbol, history_start):
    path = price_path(market, symbol)
    start = _next_start(path, history_start)
    if start > date.today().isoformat():
        return 0
    if market == "TW":
        raw = fetch("TaiwanStockPrice", symbol, start)
        rows = [{"date": r["date"], "open": r["open"], "high": r["max"], "low": r["min"],
                 "close": r["close"], "volume": r["Trading_Volume"]} for r in raw]
    else:
        raw = fetch("USStockPrice", symbol, start)
        rows = [{"date": r["date"], "open": r["Open"], "high": r["High"], "low": r["Low"],
                 "close": r["Close"], "volume": r["Volume"], "adj_close": r["Adj_Close"]}
                for r in raw]
        return _merge(path, rows, US_FIELDS)
    # 暫停交易日 FinMind 會回傳 0 價格，略過
    return _merge(path, [r for r in rows if r["close"] > 0], PRICE_FIELDS)


FX_PATH = DATA / "fx" / "USD.csv"


def update_fx(history_start):
    start = _next_start(FX_PATH, history_start)
    if start > date.today().isoformat():
        return 0
    raw = fetch("TaiwanExchangeRate", "USD", start)
    rows = [{"date": r["date"], "spot_buy": r["spot_buy"], "spot_sell": r["spot_sell"]}
            for r in raw if r["spot_buy"] and r["spot_buy"] > 0]
    return _merge(FX_PATH, rows, ["date", "spot_buy", "spot_sell"])


def dividend_path(symbol):
    return DATA / "dividends" / f"TW_{symbol}.csv"


def update_tw_dividends(symbol, history_start):
    """配息資料量小，每次全量重抓。"""
    raw = fetch("TaiwanStockDividendResult", symbol, history_start)
    rows = [{"date": r["date"], "cash": r["stock_and_cache_dividend"]}
            for r in raw if r["stock_or_cache_dividend"] == "息"]
    _write(dividend_path(symbol), rows, ["date", "cash"])
    return len(rows)


def us_dividends(symbol):
    """FinMind 沒有美股配息資料集，改由 Adj_Close/Close 比值的跳動反推每股配息。"""
    rows = _read(price_path("US", symbol))
    out = []
    for prev, cur in zip(rows, rows[1:]):
        if not prev.get("adj_close") or not cur.get("adj_close"):
            continue
        f_prev = float(prev["adj_close"]) / float(prev["close"])
        f_cur = float(cur["adj_close"]) / float(cur["close"])
        d = float(prev["close"]) * (1 - f_prev / f_cur)
        # 調整價只到小數兩位，門檻過濾捨入雜訊
        if d > 0 and d / float(prev["close"]) > 0.0005:
            out.append((cur["date"], round(d, 4)))
    return out


# ---------- 讀取 ----------

def load_series(path, col):
    """回傳 [(date, float)] 依日期排序。"""
    return [(r["date"], float(r[col])) for r in _read(path)]


def on_or_after(series, d):
    for sd, v in series:
        if sd >= d:
            return sd, v
    return None


def on_or_before(series, d):
    hit = None
    for sd, v in series:
        if sd > d:
            break
        hit = (sd, v)
    return hit
