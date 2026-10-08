"""台股個股基本面：月營收（年增、月增、累計年增）與估值位置（本益比、股價淨值比、殖利率的 5 年百分位）。

只描述「現在落在自己歷史的哪個位置」，不判斷貴或便宜、不給買賣建議。
ETF 沒有營收與本益比，美股基本面需要付費 API，兩者都不提供。
每檔分析多 2 次 FinMind 呼叫（TaiwanStockMonthRevenue、TaiwanStockPER），結果跟著技術分析一起快取。
"""
from datetime import date, timedelta

from . import data

YEARS = 5


def applicable(symbol):
    """台股個股才有基本面（ETF 代號 00 開頭）。"""
    return symbol[:1].isdigit() and not symbol.startswith("00")


def percentile(values, x):
    """x 在 values 中的百分位（0–1）：小於等於 x 的比例。"""
    vals = [v for v in values if v is not None]
    if not vals or x is None:
        return None
    return sum(v <= x for v in vals) / len(vals)


def quantile(values, q):
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    i = (len(vals) - 1) * q
    lo = int(i)
    hi = min(lo + 1, len(vals) - 1)
    return vals[lo] + (vals[hi] - vals[lo]) * (i - lo)


def zone(p):
    if p is None:
        return "—"
    return "歷史偏低區" if p < 0.2 else "歷史偏高區" if p > 0.8 else "歷史中間區"


def _next_month(y, m):
    return (y + 1, 1) if m == 12 else (y, m + 1)


def revenue(rows, months=24):
    """rows：FinMind TaiwanStockMonthRevenue。回傳最近 months 個月的營收、年增、月增，以及摘要。"""
    by = {(int(r["revenue_year"]), int(r["revenue_month"])): r["revenue"] for r in rows if r.get("revenue")}
    if not by:
        return None
    keys = sorted(by)
    series = []
    for y, m in keys[-months:]:
        prev = by.get((y - 1, m))
        py, pm = (y - 1, 12) if m == 1 else (y, m - 1)
        series.append({"ym": f"{y:04d}-{m:02d}", "revenue": by[(y, m)],
                       "yoy": by[(y, m)] / prev - 1 if prev else None,
                       "mom": by[(y, m)] / by[(py, pm)] - 1 if by.get((py, pm)) else None})
    y, m = keys[-1]
    ytd = sum(by.get((y, k), 0) for k in range(1, m + 1))
    ytd_prev = sum(by.get((y - 1, k), 0) for k in range(1, m + 1))
    yoys = [s["yoy"] for s in series[-3:] if s["yoy"] is not None]
    ny, nm = _next_month(y, m)          # 下一次公布的是 (ny, nm) 的營收，期限是再下個月 10 日
    dy, dm = _next_month(ny, nm)
    return {"series": series, "latest": series[-1],
            "ytd_yoy": ytd / ytd_prev - 1 if ytd_prev and all((y - 1, k) in by for k in range(1, m + 1)) else None,
            "yoy_3m": sum(yoys) / len(yoys) if yoys else None,
            "next": {"ym": f"{ny:04d}-{nm:02d}", "deadline": f"{dy:04d}-{dm:02d}-10"}}


def valuation(rows):
    """rows：FinMind TaiwanStockPER（每日 PER / PBR / dividend_yield）。虧損時 PER 為 0，不列入。"""
    rows = sorted(rows, key=lambda r: r["date"])
    if not rows:
        return None
    last = rows[-1]
    out = {"date": last["date"], "start": rows[0]["date"]}
    for key, name in (("PER", "本益比"), ("PBR", "股價淨值比"), ("dividend_yield", "殖利率")):
        hist = [r[key] for r in rows if r.get(key) and r[key] > 0]
        cur = last.get(key) if last.get(key) and last[key] > 0 else None
        p = percentile(hist, cur)
        out[key] = {"name": name, "value": cur, "pct": p, "zone": zone(p),
                    "p20": quantile(hist, 0.2), "p50": quantile(hist, 0.5), "p80": quantile(hist, 0.8)}
    # 給圖表用：每週取一點，約 260 點
    pick = rows[::5] + ([last] if (len(rows) - 1) % 5 else [])
    out["chart"] = {"date": [r["date"] for r in pick],
                    "PER": [r["PER"] if r.get("PER") and r["PER"] > 0 else None for r in pick],
                    "dividend_yield": [r.get("dividend_yield") or None for r in pick]}
    return out


def analyze(symbol, today=None):
    if not applicable(symbol):
        return None
    today = today or date.today()
    rev_start = date(today.year - 3, 1, 1).isoformat()
    per_start = (today - timedelta(days=365 * YEARS)).isoformat()
    rev = revenue(data.fetch("TaiwanStockMonthRevenue", symbol, rev_start))
    val = valuation(data.fetch("TaiwanStockPER", symbol, per_start))
    if not rev and not val:
        return None
    return {"revenue": rev, "valuation": val,
            "explain": "月營收：FinMind TaiwanStockMonthRevenue；年增＝本月 ÷ 去年同月 − 1，月增＝本月 ÷ 上月 − 1，"
                       "累計年增＝今年 1 月到本月合計 ÷ 去年同期 − 1。上市櫃公司須在每月 10 日前公布上月營收。<br>"
                       f"估值：FinMind TaiwanStockPER（證交所每日公布），百分位＝近 {YEARS} 年中有多少比例的交易日小於等於今天的數值；"
                       "虧損期間沒有本益比，不列入。百分位只描述現在相對自己歷史的位置，不代表會漲或跌。"}
