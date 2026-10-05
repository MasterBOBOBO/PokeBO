"""美股籌碼：FINRA 每日放空比例、FINRA 放空餘額、SEC Form 4 內部人交易。

全部是官方公開資料，免金鑰；不計入 FinMind 額度。
- FINRA 每日放空成交量包含造市商的避險放空，平常就有 40–60%，要和本檔自己的歷史比較才有意義。
- 放空餘額每月兩次（月中、月底結算），約 7 個工作天後公布。
- SEC 規定請求必須附上聯絡 email（config.json > sec_user_agent）。
"""
import json
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, timedelta

from . import data

FINRA_DIR = data.DATA / "finra" / "daily"
CACHE = data.DATA / "ta_cache"
SEC_TICKERS = data.DATA / "sec_tickers.json"
FINRA_DAILY = "https://cdn.finra.org/equity/regsho/daily/CNMSshvol{d}.txt"
FINRA_SI = "https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest"


def _get(url, headers=None, body=None, timeout=30):
    if body is not None:
        return data.http_post(url, body, headers, timeout)
    return data.http_get(url, headers, timeout)


def _ua():
    return {"User-Agent": data.load_config().get("sec_user_agent", "invest-tracker"),
            "Accept-Encoding": "identity"}


# ---------- FINRA 每日放空比例 ----------

def _daily_file(d):
    """下載並快取某一天的 FINRA 放空檔（約 0.5MB，所有標的共用）。"""
    path = FINRA_DIR / f"{d}.txt"
    if path.exists():
        return path.read_text(encoding="utf-8")
    try:
        txt = _get(FINRA_DAILY.format(d=d), _ua(), timeout=30).decode("utf-8")   # 沒有 User-Agent 會被 CDN 擋（403）
    except urllib.error.HTTPError:
        return None          # 休市日或尚未公布
    FINRA_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(txt, encoding="utf-8")
    return txt


def prune_daily(keep_days=120):
    if not FINRA_DIR.exists():
        return
    cutoff = (date.today() - timedelta(days=keep_days)).strftime("%Y%m%d")
    for p in FINRA_DIR.glob("*.txt"):
        if p.stem < cutoff:
            p.unlink()


def short_volume(symbol, dates):
    """dates：本檔的交易日（YYYY-MM-DD）。回傳每日放空成交量比例。"""
    ds = [d.replace("-", "") for d in dates]
    files = dict(zip(ds, data.pmap(_daily_file, ds, workers=6)))
    out_d, out_r = [], []
    key = f"|{symbol}|"
    for d in ds:
        txt = files.get(d)
        if not txt:
            continue
        line = next((ln for ln in txt.splitlines() if key in ln[:20]), None)
        if not line:
            continue
        f = line.split("|")
        sv, tv = float(f[2]), float(f[4])
        if tv > 0:
            out_d.append(f"{d[:4]}-{d[4:6]}-{d[6:]}")
            out_r.append(round(sv / tv, 4))
    if not out_r:
        return None
    avg = lambda xs: sum(xs) / len(xs) if xs else None
    a20, a60 = avg(out_r[-20:]), avg(out_r[-60:])
    sd = (sum((x - a60) ** 2 for x in out_r[-60:]) / len(out_r[-60:])) ** 0.5 if len(out_r) > 5 else None
    prune_daily()
    return {"date": out_d, "ratio": out_r, "last": out_r[-1], "avg5": avg(out_r[-5:]), "avg20": a20, "avg60": a60,
            "z": (avg(out_r[-5:]) - a60) / sd if sd else None,
            "explain": "FINRA 每日放空成交量 ÷ 總成交量（FINRA 申報場所，不含交易所自身的成交）。"
                       "這個比例包含造市商提供流動性時的放空，平常就有 40–60%，"
                       "所以重點是『近 5 日平均』相對『近 60 日平均』的偏離（z 值）：z > 1 代表放空明顯增加，z < −1 代表明顯減少。"}


# ---------- FINRA 放空餘額 ----------

def short_interest(symbol, months=9):
    body = json.dumps({"limit": 40,
                       "compareFilters": [{"compareType": "EQUAL", "fieldName": "symbolCode", "fieldValue": symbol}],
                       "dateRangeFilters": [{"fieldName": "settlementDate",
                                             "startDate": (date.today() - timedelta(days=30 * months)).isoformat(),
                                             "endDate": date.today().isoformat()}]}).encode()
    rows = json.loads(_get(FINRA_SI, {**_ua(), "Accept": "application/json", "Content-Type": "application/json"}, body))
    rows = sorted(rows, key=lambda x: x["settlementDate"])
    if not rows:
        return None
    last = rows[-1]
    prev = rows[-2] if len(rows) > 1 else None
    return {"date": [r["settlementDate"] for r in rows], "shares": [r["currentShortPositionQuantity"] for r in rows],
            "dtc": [r.get("daysToCoverQuantity") for r in rows],
            "last": last["currentShortPositionQuantity"], "last_date": last["settlementDate"],
            "days_to_cover": last.get("daysToCoverQuantity"), "adv": last.get("averageDailyVolumeQuantity"),
            "chg": (last["currentShortPositionQuantity"] / prev["currentShortPositionQuantity"] - 1)
                   if prev and prev["currentShortPositionQuantity"] else None,
            "explain": "FINRA 每月兩次（月中、月底）彙整的放空未回補股數，約 7 個工作天後公布，作用類似台股的融券餘額。"
                       "回補天數 = 放空股數 ÷ 平均日成交量；天數越高，代表空方要花越多天才能回補，軋空風險也越高。"}


# ---------- SEC Form 4 內部人交易 ----------

CODES = {"P": "公開市場買進", "S": "公開市場賣出", "A": "獲配／獎酬", "M": "選擇權履約", "F": "扣稅抵繳",
         "G": "贈與", "D": "處分給發行公司", "C": "轉換", "X": "認股權證履約"}


def cik_of(symbol):
    fresh = SEC_TICKERS.exists() and date.fromtimestamp(SEC_TICKERS.stat().st_mtime) >= date.today() - timedelta(days=7)
    if not fresh:
        SEC_TICKERS.parent.mkdir(parents=True, exist_ok=True)
        SEC_TICKERS.write_bytes(_get("https://www.sec.gov/files/company_tickers.json", _ua()))
    for v in json.loads(SEC_TICKERS.read_text(encoding="utf-8")).values():
        if v["ticker"] == symbol:
            return v["cik_str"], v["title"]
    return None, None


def _text(el, path):
    x = el.find(path)
    return x.text.strip() if x is not None and x.text else None


def insider(symbol, days=180, max_filings=25):
    cik, title = cik_of(symbol)
    if not cik:
        return None     # ETF 或非 SEC 申報公司
    sub = json.loads(_get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json", _ua()))
    rec = sub["filings"]["recent"]
    since = (date.today() - timedelta(days=days)).isoformat()
    filings = [(rec["accessionNumber"][i], rec["filingDate"][i], rec["primaryDocument"][i])
               for i in range(len(rec["form"])) if rec["form"][i] == "4" and rec["filingDate"][i] >= since][:max_filings]

    def parse(f):
        acc, fdate, doc = f
        raw = doc.split("/")[-1]          # primaryDocument 可能是 xslF345X05/xxx.xml（轉成 HTML 的版本），取原始 XML
        url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/{raw}"
        try:
            root = ET.fromstring(_get(url, _ua()))
        except Exception:
            return []
        owner = _text(root, "reportingOwner/reportingOwnerId/rptOwnerName") or "—"
        rel = root.find("reportingOwner/reportingOwnerRelationship")
        role = []
        if rel is not None:
            if _text(rel, "isDirector") in ("1", "true"):
                role.append("董事")
            if _text(rel, "isOfficer") in ("1", "true"):
                role.append(_text(rel, "officerTitle") or "高階主管")
            if _text(rel, "isTenPercentOwner") in ("1", "true"):
                role.append("10% 大股東")
        out = []
        for t in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
            code = _text(t, "transactionCoding/transactionCode")
            sh = float(_text(t, "transactionAmounts/transactionShares/value") or 0)
            px = float(_text(t, "transactionAmounts/transactionPricePerShare/value") or 0)
            ad = _text(t, "transactionAmounts/transactionAcquiredDisposedCode/value")
            out.append({"date": _text(t, "transactionDate/value") or fdate, "filed": fdate, "owner": owner,
                        "role": "、".join(role) or "—", "code": code, "code_name": CODES.get(code, code),
                        "shares": sh, "price": px, "value": sh * px, "side": "買" if ad == "A" else "賣"})
        return out

    # SEC 限制每秒 10 次，4 執行緒足夠安全
    txns = [x for rows in data.pmap(parse, filings, workers=4) for x in rows]
    txns.sort(key=lambda x: x["date"], reverse=True)
    buys = [x for x in txns if x["code"] == "P"]
    sells = [x for x in txns if x["code"] == "S"]
    return {"cik": cik, "company": title, "filings": len(filings), "days": days, "txns": txns[:40],
            "buy_value": sum(x["value"] for x in buys), "sell_value": sum(x["value"] for x in sells),
            "buy_n": len(buys), "sell_n": len(sells),
            "explain": f"SEC Form 4：董事、高階主管、10% 大股東在交易後 2 個工作天內必須申報。近 {days} 天共 {len(filings)} 份申報。"
                       "只有代碼 P（公開市場自掏腰包買進）和 S（公開市場賣出）比較有參考價值；"
                       "A（獎酬）、M（選擇權履約）、F（扣稅抵繳）屬於薪酬或稅務性質，不代表看法。"
                       "內部人賣股的原因很多（繳稅、分散資產、預先排定的 10b5-1 計畫），買進的訊號通常比賣出強。"}


# ---------- 匯總 ----------

def analyze(symbol, dates):
    today = date.today().isoformat()
    cache = CACHE / f"_uschips_{symbol}_{today}_v2.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    out, errors = {}, {}
    days = 20 if data.WEB else 60
    sources = [("short_volume", lambda: short_volume(symbol, dates[-days:]))]
    if data.WEB:     # 瀏覽器跨網域限制：FINRA 放空餘額（POST 沒有 CORS）、SEC（代號對照表回 403）
        out["short_interest"] = out["insider"] = None
        errors["short_interest"] = errors["insider"] = "網頁版不支援（瀏覽器跨網域限制），請用本機版"
    else:
        sources += [("short_interest", lambda: short_interest(symbol)), ("insider", lambda: insider(symbol))]
    for key, fn in sources:
        try:
            out[key] = fn()
        except Exception as e:      # 單一來源失敗不影響其他
            out[key], errors[key] = None, f"{type(e).__name__}: {e}"
    out["errors"] = errors
    CACHE.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out
