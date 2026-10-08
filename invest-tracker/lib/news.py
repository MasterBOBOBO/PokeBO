"""個股新聞與重大訊息：只列標題、來源、時間、連結，不判斷利多利空。

- 新聞：FinMind TaiwanStockNews（各大媒體標題，ETF 也有）。每次呼叫只回傳單一天，所以按日期分檔快取
  （data/news/{代號}/{日期}.json）。同一則常以 UDN／udn／udn.com 重複出現，依標題合併。
- 重大訊息：證交所（上市）、櫃買中心（上櫃）OpenAPI 的「每日重大訊息」，是公司依法申報的正式公告。
  OpenAPI 只提供最近一天，所以每天抓下來累積在 data/news/material.csv；沒有 CORS，只在本機版使用。
- 美股個股：SEC 8-K 申報（重大事件）；ETF 沒有。
"""
import csv
import json
import re
from datetime import date, datetime, timedelta

from . import data

DIR = data.DATA / "news"
MATERIAL = DIR / "material.csv"
MATERIAL_FIELDS = ["time", "symbol", "name", "subject", "clause", "market"]
UA = {"User-Agent": "Mozilla/5.0 (Macintosh) invest-tracker"}
TWSE = "https://openapi.twse.com.tw/v1/opendata/t187ap04_L"
TPEX = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap04_O"
NEWS_TTL_HOURS = 3
# SEC 8-K 項目代碼（常見的）
ITEMS_8K = {"1.01": "簽訂重大合約", "1.02": "終止重大合約", "1.05": "資安事件", "2.01": "完成收購或處分資產",
            "2.02": "營運與財務結果", "2.03": "新增重大債務", "2.05": "重組或退出成本", "2.06": "資產減損",
            "3.01": "下市或不符上市規定", "3.02": "未註冊股票發行", "4.01": "更換會計師", "5.02": "董事或高階主管異動",
            "5.03": "修改章程", "5.07": "股東會表決結果", "7.01": "Reg FD 資訊揭露", "8.01": "其他重大事件",
            "9.01": "財務報表與附件"}


# ---------- 新聞 ----------

def _clean_title(t):
    """去掉標題尾端的「 - 來源」與「| 分類」，作為合併重複新聞的鍵。"""
    t = re.sub(r"\s*[-|｜]\s*[^-|｜]{1,20}$", "", t.strip())
    return re.sub(r"\s+", " ", t)


def _tw_time(s):
    """FinMind 新聞時間是 UTC，換成台灣時間（+8）。"""
    try:
        return (datetime.fromisoformat(s[:19]) + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return s[:16]


def dedupe(rows):
    """同標題合併，保留最早一則並合併來源；依時間新到舊排序。"""
    out = {}
    for r in sorted(rows, key=lambda r: r["date"]):
        title = _clean_title(r.get("title") or "")
        if not title:
            continue
        key = title.lower()
        src = (r.get("source") or "").replace(".com", "").replace(".tw", "").strip()
        if key in out:
            if src and src.lower() not in {s.lower() for s in out[key]["sources"]}:
                out[key]["sources"].append(src)
            continue
        out[key] = {"time": _tw_time(r["date"]), "title": title, "link": r.get("link") or "", "sources": [src] if src else []}
    return sorted(out.values(), key=lambda x: x["time"], reverse=True)


def _day_news(symbol, d, cached_only=False):
    """單日新聞（FinMind 每次只回傳 start_date 當天）。過去的日子抓過一次就不再抓，今天快取 3 小時。"""
    path = DIR / symbol / f"{d}.json"
    if not data.WEB and path.exists():
        cached = json.loads(path.read_text(encoding="utf-8"))
        complete = d < date.today().isoformat() and cached["fetched"][:10] > d      # 那天結束後才抓的
        fresh = datetime.now() - datetime.fromisoformat(cached["fetched"]) < timedelta(hours=NEWS_TTL_HOURS)
        if complete or fresh or cached_only:
            return cached["rows"]
    if cached_only:
        return []
    rows = [{k: r.get(k) for k in ("date", "title", "link", "source")} for r in data.fetch("TaiwanStockNews", symbol, d)]
    if not data.WEB:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"fetched": datetime.now().isoformat(timespec="seconds"), "rows": rows},
                                   ensure_ascii=False), encoding="utf-8")
    return rows


def tw_news(symbol, days=3, limit=30, cached_only=False):
    """近 days 天（含今天）的新聞，合併重複後新到舊。每天 1 次 FinMind 呼叫，有快取。
    cached_only=True 只讀快取（首頁用，資料由排程預先抓好）。"""
    rows = []
    for i in range(days):
        rows += _day_news(symbol, (date.today() - timedelta(days=i)).isoformat(), cached_only)
    return dedupe(rows)[:limit]


# ---------- 重大訊息 ----------

def _roc(d, t):
    """民國日期 1151007 + 時間 70004（前面的 0 被省略）→ 2026-10-07 07:00。"""
    d = str(d).strip()
    t = str(t or "0").strip().zfill(6)
    return f"{int(d[:-4]) + 1911:04d}-{d[-4:-2]}-{d[-2:]} {t[:2]}:{t[2:4]}"


def parse_material(rows, market):
    out = []
    for r in rows:
        r = {k.strip(): v for k, v in r.items()}        # 證交所欄位名稱有多餘空白（"主旨 "）
        sym = r.get("公司代號") or r.get("SecuritiesCompanyCode")
        when = r.get("發言日期")
        if not sym or not when:
            continue
        out.append({"time": _roc(when, r.get("發言時間")), "symbol": str(sym).strip(),
                    "name": (r.get("公司名稱") or r.get("CompanyName") or "").strip(),
                    "subject": re.sub(r"\s+", " ", r.get("主旨") or "").strip(),
                    "clause": (r.get("符合條款") or "").strip(), "market": market})
    return out


def _read_material():
    if not MATERIAL.exists():
        return []
    with MATERIAL.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def update_material(keep_days=90):
    """抓今天的上市、上櫃重大訊息，併入累積檔（同時間＋代號＋主旨視為同一則）。回傳新增的筆數。"""
    rows = []
    for url, mk in ((TWSE, "上市"), (TPEX, "上櫃")):
        rows += parse_material(json.loads(data.http_get(url, UA)), mk)
    old = _read_material()
    seen = {(r["time"], r["symbol"], r["subject"]) for r in old}
    new = [r for r in rows if (r["time"], r["symbol"], r["subject"]) not in seen]
    cutoff = (date.today() - timedelta(days=keep_days)).isoformat()
    keep = sorted([r for r in old + new if r["time"] >= cutoff], key=lambda r: r["time"])
    DIR.mkdir(parents=True, exist_ok=True)
    with MATERIAL.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=MATERIAL_FIELDS)
        w.writeheader()
        w.writerows(keep)
    return len(new)


def material(symbols, days=7):
    since = (date.today() - timedelta(days=days)).isoformat()
    syms = set(symbols)
    return sorted((r for r in _read_material() if r["symbol"] in syms and r["time"] >= since),
                  key=lambda r: r["time"], reverse=True)


# ---------- 美股 8-K ----------

def sec_8k(symbol, days=30):
    from . import us_chips
    cik, _ = us_chips.cik_of(symbol)
    if not cik:
        return None
    rec = json.loads(us_chips._get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json", us_chips._ua()))["filings"]["recent"]
    since = (date.today() - timedelta(days=days)).isoformat()
    out = []
    for i, form in enumerate(rec["form"]):
        if form not in ("8-K", "8-K/A") or rec["filingDate"][i] < since:
            continue
        items = [x.strip() for x in (rec.get("items", [""] * len(rec["form"]))[i] or "").split(",") if x.strip()]
        acc = rec["accessionNumber"][i].replace("-", "")
        out.append({"time": rec["filingDate"][i], "form": form,
                    "subject": "、".join(ITEMS_8K.get(x, f"Item {x}") for x in items if x != "9.01") or "8-K 申報",
                    "link": f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{rec['primaryDocument'][i]}"})
    return out


# ---------- 給儀表板 / 首頁 ----------

def for_stock(symbol, market):
    """技術分析頁「新聞與公告」卡片。重大訊息只在本機版。"""
    out = {"news": [], "material": [], "filings": None}
    if market == "TW":
        out["news"] = tw_news(symbol)
        if not data.WEB:
            out["material"] = material([symbol], days=30)
    elif not data.WEB:
        out["filings"] = sec_8k(symbol)
    return out


def for_holdings(holdings, days=3, per_symbol=3):
    """首頁「持股新聞」：重大訊息（近 7 天）＋每檔最新幾則新聞（近 days 天）。只讀快取（由排程預先抓好）。"""
    tw = [h["symbol"] for h in holdings if h["market"] == "TW"]
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M")
    news = []
    for sym in tw:
        try:
            items = [x for x in tw_news(sym, days=days, cached_only=True) if x["time"] >= since][:per_symbol]
        except Exception:
            items = []
        news += [{**x, "symbol": sym} for x in items]
    return {"material": material(tw, days=7), "news": sorted(news, key=lambda x: x["time"], reverse=True)}


# ---------- 每日推播 ----------

SENT = data.DATA / "state" / "news_sent.json"


def daily_update(cfg, holdings):
    """抓重大訊息與持股新聞，回傳「還沒推播過」的持股重大訊息與 8-K（推播後記錄，避免重複）。"""
    errors = []
    try:
        update_material()
    except Exception as e:
        errors.append(f"重大訊息：{type(e).__name__}: {e}")
    tw = [h["symbol"] for h in holdings if h["market"] == "TW"]
    for sym in tw:
        try:
            tw_news(sym)
        except data.QuotaError:
            raise
        except Exception as e:
            errors.append(f"{sym} 新聞：{e}")
    items = [{"key": f"{m['time']}|{m['symbol']}|{m['subject']}", "time": m["time"], "symbol": m["symbol"],
              "name": m["name"], "subject": m["subject"], "kind": "重大訊息"} for m in material(tw, days=3)]
    for h in holdings:
        if h["market"] == "US" and h.get("category", "").endswith("個股"):
            try:
                for f in sec_8k(h["symbol"], days=7) or []:
                    items.append({"key": f"{f['link']}", "time": f["time"], "symbol": h["symbol"], "name": "",
                                  "subject": f["subject"], "kind": f"SEC {f['form']}", "link": f["link"]})
            except Exception as e:
                errors.append(f"{h['symbol']} 8-K：{e}")
    sent = set(json.loads(SENT.read_text(encoding="utf-8"))) if SENT.exists() else set()
    new = [x for x in items if x["key"] not in sent]
    return new, errors


def mark_sent(items):
    sent = json.loads(SENT.read_text(encoding="utf-8")) if SENT.exists() else []
    sent = (sent + [x["key"] for x in items])[-500:]
    SENT.parent.mkdir(parents=True, exist_ok=True)
    SENT.write_text(json.dumps(sent, ensure_ascii=False), encoding="utf-8")


def push_text(items):
    lines = []
    for x in sorted(items, key=lambda x: x["time"]):
        who = f"{x['symbol']} {x['name']}".strip()
        lines.append(f"{x['time'][5:].replace('-', '/')} {who}【{x['kind']}】{x['subject']}" + (f"\n{x['link']}" if x.get("link") else ""))
    return "\n".join(lines) + "\n\n只轉述公司公告主旨，不是買賣建議；詳情見首頁或公開資訊觀測站。"
