"""集保戶股權分散（臺灣集中保管結算所開放資料 1-5，每週更新，免費）。

官方只提供「最新一週」的全市場檔案，沒有歷史查詢，所以每次下載後把各股摘要存成
data/tdcc/{資料日期}.csv，歷史從開始累積的那週算起。網站沒有 CORS，只在本機版使用。
除了固定的千張／400 張／50 張摘要，也存每一級的持股比例（p1–p15）和人數（n1–n15），
讓前端自選大戶、散戶門檻；2026-10-08 以前存的檔案沒有分級欄位。

持股分級（股）：1 = 1–999、2 = 1,000–5,000 … 8 = 40,001–50,000、9 = 50,001–100,000、
10 = 100,001–200,000、11 = 200,001–400,000、12 = 400,001–600,000、13 = 600,001–800,000、
14 = 800,001–1,000,000、15 = 1,000,001 以上、16 = 差異數調整、17 = 合計。
"""
import csv
import io
from datetime import date, timedelta

from . import data

URL = "https://opendata.tdcc.com.tw/getOD.ashx?id=1-5"
DIR = data.DATA / "tdcc"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh) invest-tracker"}
LEVELS = range(1, 16)
FIELDS = (["symbol", "holders", "big1000", "big400", "retail50", "big1000_holders"]
          + [f"p{lv}" for lv in LEVELS] + [f"n{lv}" for lv in LEVELS])


def summarize(text):
    """全市場 CSV → (資料日期, {代號: 摘要})。比例是占集保庫存的 %。"""
    by = {}
    day = None
    for r in csv.reader(io.StringIO(text.lstrip("﻿"))):
        if len(r) < 6 or not r[2].strip().isdigit():
            continue
        day = r[0].strip()
        sym, lv = r[1].strip(), int(r[2])
        s = by.setdefault(sym, {"symbol": sym, "holders": 0, "big1000": 0.0, "big400": 0.0, "retail50": 0.0,
                                "big1000_holders": 0})
        people, pct = int(r[3]), float(r[5])
        if lv in LEVELS:
            s[f"p{lv}"], s[f"n{lv}"] = pct, people
        if lv == 17:
            s["holders"] = people
        elif lv == 15:
            s["big1000"] += pct
            s["big1000_holders"] = people
        if 12 <= lv <= 15:
            s["big400"] += pct
        elif 1 <= lv <= 8:
            s["retail50"] += pct
    for s in by.values():
        for k in ("big1000", "big400", "retail50"):
            s[k] = round(s[k], 2)
    return day, by


def update(force=False):
    """下載最新一週並存檔；同一天最多下載一次，已經有的資料日期不重複寫入。回傳新存的資料日期或 None。"""
    if data.WEB:
        return None
    mark = DIR / ".last_fetch"
    if not force and mark.exists() and mark.read_text().strip() == date.today().isoformat():
        return None
    day, by = summarize(data.http_get(URL, UA, timeout=90).decode("utf-8-sig", "ignore"))
    DIR.mkdir(parents=True, exist_ok=True)
    mark.write_text(date.today().isoformat())
    f = DIR / f"{day}.csv" if day else None
    if not by or not f or (f.exists() and "p15" in f.read_text(encoding="utf-8").split("\n", 1)[0]):
        return None                    # 已經有完整分級的同一週就不重寫（舊格式會補寫分級欄位）
    data._write(DIR / f"{day}.csv", sorted(by.values(), key=lambda s: s["symbol"]), FIELDS)
    return day


def history(symbol, weeks=26):
    """本檔每週摘要（舊 → 新）。沒有任何資料時回傳 None。"""
    if data.WEB:
        return None
    files = sorted(DIR.glob("*.csv"))[-weeks:] if DIR.exists() else []
    if not files:
        try:
            update()                       # 第一次使用：先抓一次最新一週
        except Exception:
            return None
        files = sorted(DIR.glob("*.csv"))[-weeks:] if DIR.exists() else []
    out = {k: [] for k in ["date"] + FIELDS[1:6]}
    out["levels"], out["level_people"] = [], []    # 每週 15 級的持股 % 與人數；舊格式為 None
    for f in files:
        with f.open(encoding="utf-8") as fh:
            row = next((r for r in csv.DictReader(fh) if r["symbol"] == symbol), None)
        if not row:
            continue
        d = f.stem
        out["date"].append(f"{d[:4]}-{d[4:6]}-{d[6:]}")
        for k in ("holders", "big1000_holders"):
            out[k].append(int(row[k]))
        for k in ("big1000", "big400", "retail50"):
            out[k].append(float(row[k]))
        has = bool(row.get("p15"))
        out["levels"].append([float(row[f"p{lv}"] or 0) for lv in LEVELS] if has else None)
        out["level_people"].append([int(row[f"n{lv}"] or 0) for lv in LEVELS] if has else None)
    return out if out["date"] else None
