"""市場溫度計：景氣燈號、加權指數年線乖離、融資餘額變化、VIX，以及 FOMC 會議日期。

每一項都只描述「現在相對自己歷史的位置」，不是進出場訊號；定期定額照原設定扣款。
資料在每日排程（或 invest.py update）時更新，首頁只讀快取，不會臨時呼叫 API。
只在本機版使用（國發會、聯準會網站沒有 CORS）。
"""
import csv
import io
import json
import re
import zipfile
from datetime import date, timedelta

from . import data

DIR = data.DATA / "market"
NDC_PATH = DIR / "ndc.csv"
MARGIN_PATH = DIR / "margin.csv"
FOMC_PATH = DIR / "fomc.json"
VIX = "^VIX"
TAIEX = "TAIEX"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh) invest-tracker"}
NDC_DATASET = "https://data.gov.tw/api/v2/rest/dataset/6099"
FOMC_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
# 國發會景氣對策信號：分數區間 → 燈號（官方定義）
LIGHTS = [(38, "紅燈", "景氣熱絡"), (32, "黃紅燈", "景氣活絡，留意轉向"), (23, "綠燈", "景氣穩定"),
          (17, "黃藍燈", "景氣欠佳，留意轉向"), (9, "藍燈", "景氣低迷")]


def _stale(path, days):
    return not path.exists() or date.fromtimestamp(path.stat().st_mtime) <= date.today() - timedelta(days=days)


# ---------- 更新 ----------

def update_ndc(force=False):
    """國發會「景氣指標及燈號」開放資料（每月底公布上個月）。每 7 天最多下載一次。"""
    if not force and not _stale(NDC_PATH, 7):
        return 0
    meta = json.loads(data.http_get(NDC_DATASET, UA))
    url = next(d["resourceDownloadUrl"] for d in meta["result"]["distribution"] if d.get("resourceDownloadUrl"))
    z = zipfile.ZipFile(io.BytesIO(data.http_get(url, UA)))
    # 壓縮檔裡的檔名編碼不固定，改用表頭找出「景氣指標與燈號」那個檔
    text = next(t for t in (z.read(n).decode("utf-8-sig", "ignore") for n in z.namelist() if n.endswith(".csv"))
                if "景氣對策信號綜合分數" in t.split("\n", 1)[0] and "領先指標綜合指數" in t.split("\n", 1)[0])
    rows = []
    for r in csv.DictReader(io.StringIO(text)):
        score = r.get("景氣對策信號綜合分數", "-")
        if not score or score == "-":
            continue
        ym = r["Date"]
        rows.append({"ym": f"{ym[:4]}-{ym[4:6]}", "score": int(float(score)), "light": r.get("景氣對策信號", ""),
                     "leading": r.get("領先指標不含趨勢指數", "")})
    DIR.mkdir(parents=True, exist_ok=True)
    data._write(NDC_PATH, rows, ["ym", "score", "light", "leading"])
    return len(rows)


def update_margin(history_start):
    """整體市場融資餘額（元）。"""
    start = data._next_start(MARGIN_PATH, history_start)
    if start > date.today().isoformat():
        return 0
    raw = data.fetch("TaiwanStockTotalMarginPurchaseShortSale", "", start)
    rows = [{"date": r["date"], "balance": r["TodayBalance"]} for r in raw if r.get("name") == "MarginPurchaseMoney"]
    return data._merge(MARGIN_PATH, rows, ["date", "balance"])


def parse_fomc(html):
    """聯準會 FOMC 行事曆頁面 → [{date: 決議日, sep: 是否公布經濟預測}]。"""
    out = []
    parts = re.split(r"(\d{4}) FOMC Meetings", html)
    for i in range(1, len(parts) - 1, 2):
        year, body = int(parts[i]), parts[i + 1]
        for m in re.finditer(r'fomc-meeting__month[^>]*>\s*<strong>([^<]+)</strong>.*?fomc-meeting__date[^>]*>([^<]+)<', body, re.S):
            month, days = m.group(1).strip(), m.group(2).strip()
            if "notation" in days or "unscheduled" in days.lower():
                continue
            last_month = month.split("/")[-1][:3]
            day = re.findall(r"\d+", days)
            if not day:
                continue
            try:
                mon = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"].index(last_month) + 1
            except ValueError:
                continue
            y = year + 1 if month.startswith("Dec/") else year
            out.append({"date": date(y, mon, int(day[-1])).isoformat(), "sep": "*" in days})
    return sorted({e["date"]: e for e in out}.values(), key=lambda e: e["date"])


def update_fomc(force=False):
    if not force and not _stale(FOMC_PATH, 7):
        return 0
    meetings = parse_fomc(data.http_get(FOMC_URL, UA).decode("utf-8", "ignore"))
    if meetings:
        DIR.mkdir(parents=True, exist_ok=True)
        FOMC_PATH.write_text(json.dumps(meetings), encoding="utf-8")
    return len(meetings)


def update(cfg):
    """每日排程呼叫。各項獨立，一項失敗不影響其他項；回傳錯誤訊息清單。"""
    errors = []
    start = cfg["history_start"]
    for name, fn in (("TAIEX", lambda: data.update_prices("TW", TAIEX, start)),
                     ("VIX", lambda: data.update_prices("US", VIX, "2016-01-01")),
                     ("融資餘額", lambda: update_margin(start)),
                     ("景氣燈號", update_ndc), ("FOMC", update_fomc)):
        try:
            fn()
        except data.QuotaError:
            raise
        except Exception as e:
            errors.append(f"{name}：{type(e).__name__}: {e}")
    return errors


# ---------- 計算 ----------

def _pct(values, x):
    vals = [v for v in values if v is not None]
    return sum(v <= x for v in vals) / len(vals) if vals and x is not None else None


def _heat(p, invert=False):
    if p is None:
        return "—"
    if invert:
        p = 1 - p
    return "偏熱" if p > 0.8 else "偏冷" if p < 0.2 else "中性"


def light_of(score):
    for lo, name, desc in LIGHTS:
        if score >= lo:
            return name, desc
    return "藍燈", "景氣低迷"


def ndc_item():
    rows = data._read(NDC_PATH)
    if not rows:
        return None
    last = rows[-1]
    score = int(last["score"])
    name, desc = light_of(score)
    p = _pct([int(r["score"]) for r in rows], score)
    heat = {"紅燈": "偏熱", "黃紅燈": "偏熱", "綠燈": "中性", "黃藍燈": "偏冷", "藍燈": "偏冷"}[name]
    return {"key": "ndc", "name": "景氣對策信號", "value": f"{name} {score} 分", "pct": p, "heat": heat,
            "asof": last["ym"], "detail": f"{desc}；{rows[0]['ym'][:4]} 年以來第 {round(p * 100)} 百分位。國發會每月底公布上個月。",
            "history": [{"ym": r["ym"], "score": int(r["score"])} for r in rows[-24:]]}


def taiex_item(years=10):
    s = data.load_series(data.price_path("TW", TAIEX), "close")
    if len(s) < 260:
        return None
    closes = [v for _, v in s]
    bias = [None] * 239 + [closes[i] / (sum(closes[i - 239:i + 1]) / 240) - 1 for i in range(239, len(closes))]
    cutoff = (date.fromisoformat(s[-1][0]) - timedelta(days=365 * years)).isoformat()
    hist = [b for (d, _), b in zip(s, bias) if d >= cutoff and b is not None]
    cur = bias[-1]
    p = _pct(hist, cur)
    w = [v for d, v in s if d >= (date.fromisoformat(s[-1][0]) - timedelta(days=365)).isoformat()]
    gap = closes[-1] / max(w) - 1
    high = "在 52 週高點附近" if gap > -0.005 else f"距 52 週高點 {gap:+.1%}"
    return {"key": "taiex", "name": "加權指數年線乖離", "value": f"{cur * 100:+.1f}%", "pct": p, "heat": _heat(p),
            "asof": s[-1][0],
            "detail": f"指數 {closes[-1]:,.0f}，{high}；乖離＝指數 ÷ 240 日均線 − 1，"
                      f"位於近 {min(years, round(len(hist) / 245))} 年第 {round(p * 100)} 百分位。"}


def margin_item(n=20):
    s = data.load_series(MARGIN_PATH, "balance")
    if len(s) < n + 60:
        return None
    vals = [v for _, v in s]
    chg = [vals[i] / vals[i - n] - 1 for i in range(n, len(vals))]
    p = _pct(chg, chg[-1])
    return {"key": "margin", "name": f"融資餘額 {n} 日變化", "value": f"{chg[-1] * 100:+.1f}%", "pct": p, "heat": _heat(p),
            "asof": s[-1][0],
            "detail": f"整體融資餘額 {vals[-1] / 1e8:,.0f} 億元；{n} 日變化位於 {s[0][0][:4]} 年以來第 {round(p * 100)} 百分位。"
                      "融資快速增加代表散戶加槓桿。"}


def vix_item(years=10):
    s = data.load_series(data.price_path("US", VIX), "close")
    if not s:
        return None
    cur = s[-1][1]
    cutoff = (date.fromisoformat(s[-1][0]) - timedelta(days=365 * years)).isoformat()
    p = _pct([v for d, v in s if d >= cutoff], cur)
    level = "平靜" if cur < 15 else "一般" if cur < 20 else "偏緊張" if cur < 30 else "恐慌"
    return {"key": "vix", "name": "VIX 波動率指數", "value": f"{cur:.1f}（{level}）", "pct": p,
            "heat": _heat(p, invert=True), "asof": s[-1][0],
            "detail": f"美股選擇權隱含的 30 天波動度；位於 {s[0][0][:4]} 年以來第 {round(p * 100)} 百分位。"
                      "VIX 低＝市場情緒樂觀（偏熱），VIX 高＝恐慌（偏冷）。"}


def fomc_meetings():
    return json.loads(FOMC_PATH.read_text(encoding="utf-8")) if FOMC_PATH.exists() else []


def snapshot():
    items = []
    for fn in (ndc_item, taiex_item, margin_item, vix_item):
        try:
            x = fn()
        except Exception as e:  # 單一項目資料壞掉時，其他項照常顯示
            x = {"key": fn.__name__, "name": fn.__name__, "error": str(e)}
        if x:
            items.append(x)
    heat = [x.get("heat") for x in items]
    return {"items": items, "hot": heat.count("偏熱"), "cold": heat.count("偏冷"), "neutral": heat.count("中性"),
            "note": "溫度只描述市場現在相對自己歷史的位置，不是進出場訊號；定期定額照原設定扣款，加減碼依投資政策。"}
