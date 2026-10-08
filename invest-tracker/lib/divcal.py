"""股利行事曆：未來一年每筆股利的除息日、入帳日、金額（毛額 / 補充保費或預扣稅 / 淨額）。

三種來源，可信度由高到低：
- 已公告：台股 TaiwanStockDividend 有除息日、發放日與金額
- 日期已公告：除息日、發放日已公告，金額還沒定（ETF 常見），金額用最近一次配息推估
- 推估：去年同期配過息、今年還沒公告，日期和金額都照去年推估
另外，已經除息但還沒入帳的股利會列為「待入帳」，股數用除息前的持股。

股數一律用目前持股，不計入之後的定額扣款與股息再投資，所以實際金額通常會略高。
"""
from datetime import date, timedelta

from . import data, ledger, risk

US_PAY_LAG = 5          # 美股 ETF 除息後約 3–7 天入帳（推估）
TW_PAY_LAG = 25         # 台股沒有歷史發放日時的預設間隔
MATCH_DAYS = 45         # 推估的除息日和已公告的除息日相差這麼多天內，視為同一次配息


def _history(cfg, market, symbol):
    """歷史每股配息 [(除息日, 每股金額)]，分割前的金額換算成分割後。"""
    rows = data.us_dividends(symbol) if market == "US" else data.load_series(data.dividend_path(symbol), "cash")
    return risk._split_adjusted(cfg, symbol, rows)


def _pay_lag(announced):
    lags = sorted((date.fromisoformat(a["pay_date"]) - date.fromisoformat(a["ex_date"])).days
                  for a in announced if a["pay_date"])
    return lags[len(lags) // 2] if lags else TW_PAY_LAG


def _shift_year(d):
    dt = date.fromisoformat(d)
    try:
        return dt.replace(year=dt.year + 1)
    except ValueError:          # 2/29
        return dt.replace(year=dt.year + 1, day=28)


def symbol_events(cfg, market, symbol, shares, txns, today, horizon_days=365):
    """單一標的的股利事件（不含金額換算）：[{ex_date, pay_date, per_share, shares, status}]。"""
    hist = _history(cfg, market, symbol)
    announced = data.load_announce(symbol) if market == "TW" else []
    end = today + timedelta(days=horizon_days)
    last_cash = hist[-1][1] if hist else None
    events = []

    def held_before(ex):
        # 期初部位是建檔前就持有的，建檔前除息、建檔後才入帳的股利也算
        return sum(t["shares"] * ledger.split_factor(cfg, symbol, t["date"], ex)
                   for t in txns if t["symbol"] == symbol and (t["date"] < ex or t["source"] == "opening"))

    for a in announced:
        ex = date.fromisoformat(a["ex_date"])
        pay = date.fromisoformat(a["pay_date"]) if a["pay_date"] else None
        if ex < today:
            # 已除息、還沒入帳
            if pay and pay >= today:
                cash = a["cash"] or dict(hist).get(a["ex_date"]) or last_cash
                n = held_before(a["ex_date"])
                if cash and n > 0:
                    events.append({"ex_date": a["ex_date"], "pay_date": a["pay_date"], "per_share": cash,
                                   "shares": n, "status": "待入帳"})
            continue
        if ex > end or not (a["cash"] or last_cash):
            continue
        events.append({"ex_date": a["ex_date"], "pay_date": a["pay_date"] or "",
                       "per_share": a["cash"] or last_cash, "shares": shares,
                       "status": "已公告" if a["cash"] else "日期已公告"})

    lag = US_PAY_LAG if market == "US" else _pay_lag(announced)
    known = [date.fromisoformat(a["ex_date"]) for a in announced]
    cutoff = (today - timedelta(days=365)).isoformat()
    for d, cash in hist:
        if d <= cutoff:
            continue
        ex = _shift_year(d)
        if ex < today or ex > end or any(abs((ex - k).days) <= MATCH_DAYS for k in known):
            continue
        events.append({"ex_date": ex.isoformat(), "pay_date": (ex + timedelta(days=lag)).isoformat(),
                       "per_share": cash, "shares": shares, "status": "推估"})
    return events


def build(cfg, holdings, txns, fx_now, today=None, horizon_days=365):
    """全部持股的股利行事曆 + 按入帳月份加總。"""
    today = today or date.today()
    tax = cfg["tax"]
    drip = {s for p in cfg["plans"].values() for s in p.get("drip", [])}
    events = []
    for h in holdings:
        if h["shares"] <= 0:
            continue
        for e in symbol_events(cfg, h["market"], h["symbol"], h["shares"], txns, today, horizon_days):
            fx = fx_now if h["market"] == "US" else 1
            gross = e["shares"] * e["per_share"] * fx
            if h["market"] == "US":
                deduct, deduct_label = gross * tax["us_withholding"], "預扣稅"
            elif gross >= tax["tw_nhi_threshold"]:
                deduct, deduct_label = gross * tax["tw_nhi_rate"], "補充保費"
            else:
                deduct, deduct_label = 0, ""
            e["per_share"] = round(e["per_share"], 4)
            events.append({**e, "market": h["market"], "symbol": h["symbol"],
                           "currency": h["currency"], "gross_twd": round(gross),
                           "deduct_twd": round(deduct), "deduct_label": deduct_label,
                           "net_twd": round(gross - deduct), "drip": h["symbol"] in drip})
    events.sort(key=lambda e: (e["pay_date"] or e["ex_date"], e["symbol"]))

    months = []
    y, m = today.year, today.month
    for _ in range(12):
        key = f"{y:04d}-{m:02d}"
        rows = [e for e in events if (e["pay_date"] or e["ex_date"])[:7] == key]
        months.append({"month": key, "net_twd": sum(e["net_twd"] for e in rows),
                       "gross_twd": sum(e["gross_twd"] for e in rows),
                       "symbols": sorted({e["symbol"] for e in rows})})
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    # 除息日在一年內、但入帳落在第 13 個月的不列入
    in_window = [e for e in events if (e["pay_date"] or e["ex_date"])[:7] <= months[-1]["month"]]
    return {"events": in_window, "months": months,
            "total_net_twd": sum(e["net_twd"] for e in in_window),
            "total_gross_twd": sum(e["gross_twd"] for e in in_window),
            "total_deduct_twd": sum(e["deduct_twd"] for e in in_window),
            "nhi_count": sum(1 for e in in_window if e["deduct_label"] == "補充保費")}
