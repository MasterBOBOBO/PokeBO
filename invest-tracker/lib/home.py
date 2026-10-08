"""長期持有者首頁（/api/home）：配置相對政策、累積投入與市值、和大盤比較、回撤、定額進度、股利行事曆。

只呈現事實和政策規則的比較結果，不產生買賣建議。
"""
import math
from datetime import date, timedelta

from . import data, divcal, events, ledger, market, report, risk

BENCHMARKS = ["0050", "VOO"]
# 政策條的項目名稱要和 report.health_checks 一致，狀態直接沿用健檢結果
HEALTH_ITEMS = {"us": "地區分散", "hd": "高股息 ETF 占比", "stock": "單一個股占比",
                "issuer": "單一公司穿透曝險", "estate": "美國遺產稅曝險", "sector": "美股定額產業集中"}


def _cash_flows(cfg, txns):
    """[(date, twd)]，負數為投入。期初部位以建檔日市值計（和 XIRR 一致）。"""
    out = []
    for t in txns:
        mk = t["market"]
        if t["source"] == "opening":
            hit = data.on_or_before(data.load_series(data.price_path(mk, t["symbol"]), "close"), t["date"])
            fx = ledger.fx_on(t["date"], "spot_buy") if mk == "US" else 1
            amt = t["shares"] * hit[1] * fx if hit and fx else t["shares"] * t["price"]
        else:
            amt = (t["shares"] * t["price"] + t["fee"]) * ((t.get("fx") or 1) if mk == "US" else 1)
        out.append((t["date"], -amt))
    return sorted(out)


def _carry(series, days):
    """把 [(date, v)] 依 days 往前補值，回傳 {date: v}；第一筆資料之前為 None。"""
    out, i, last = {}, 0, None
    for d in days:
        while i < len(series) and series[i][0] <= d:
            last = series[i][1]
            i += 1
        out[d] = last
    return out


def growth(cfg, txns, divs):
    """自第一筆交易起每日的累積投入、市值、市值＋已領股利（TWD）。"""
    if not txns:
        return {"date": [], "invested": [], "value": [], "with_div": []}
    start = min(t["date"] for t in txns)
    syms = sorted({(t["market"], t["symbol"]) for t in txns})
    closes = {s: data.load_series(data.price_path(*s), "close") for s in syms}
    days = sorted({d for ser in closes.values() for d, _ in ser if d >= start})
    px = {s: _carry(ser, days) for s, ser in closes.items()}
    fx = _carry(data.load_series(data.FX_PATH, "spot_buy"), days)
    flows = _cash_flows(cfg, txns)
    out = {"date": [], "invested": [], "value": [], "with_div": []}
    for d in days:
        value = 0.0
        for mk, sym in syms:
            n = sum(t["shares"] * ledger.split_factor(cfg, sym, t["date"], d)
                    for t in txns if t["symbol"] == sym and t["date"] <= d)
            p = px[(mk, sym)][d]
            if n and p:
                value += n * p * ((fx[d] or 0) if mk == "US" else 1)
        invested = -sum(a for fd, a in flows if fd <= d)
        got = sum(x["amount_twd"] for x in divs if x["date"] <= d)
        out["date"].append(d)
        out["invested"].append(round(invested))
        out["value"].append(round(value))
        out["with_div"].append(round(value + got))
    return out


def benchmark(cfg, flows, days, symbol):
    """同樣的投入時間和金額改買 symbol（含息再投入），每日市值（TWD）。沒有價格資料時回傳 None。"""
    mk = ledger.market_of(symbol, cfg)
    tr = risk._total_return(cfg, mk, symbol)
    if not tr or not days:
        return None
    idx = sorted(tr.items())
    fx_sell = data.load_series(data.FX_PATH, "spot_sell")
    fx_buy = _carry(data.load_series(data.FX_PATH, "spot_buy"), days)
    units_at, units = [], 0.0
    for d, amt in flows:
        hit = data.on_or_after(idx, d) or idx[-1]
        rate = (data.on_or_before(fx_sell, d) or (None, 0))[1] if mk == "US" else 1
        if not rate:
            return None
        units += -amt / rate / hit[1]
        units_at.append((d, units))
    held, px = _carry(units_at, days), _carry(idx, days)
    curve = []
    for d in days:
        if held[d] is None or px[d] is None:
            curve.append(None)
            continue
        rate = fx_buy[d] if mk == "US" else 1
        curve.append(round(held[d] * px[d] * rate) if rate else None)
    return curve


def policy_bars(cfg, r):
    """政策條：數值、門檻、方向（min = 下限、max = 上限）、狀態沿用健檢。"""
    pol = cfg["policy"]
    status = {h["item"]: h["status"] for h in r["health"]}
    alloc, total = r["allocation"], r["summary"]["value_twd"] or 1
    bars = [
        {"key": "us", "label": "美股占比", "value": alloc["market"].get("US", 0), "limit": pol["min_us_share"], "kind": "min"},
        {"key": "hd", "label": "高股息 ETF", "value": alloc["category"].get("台股高股息", 0),
         "limit": pol["max_high_dividend"], "kind": "max"},
    ]
    top = max((h for h in r["holdings"] if h["category"] in ("台股個股", "台股金融股", "美股個股")),
              key=lambda h: h["value_twd"], default=None)
    if top:
        bars.append({"key": "stock", "label": f"最大單一個股（{top['symbol']}）", "value": top["value_twd"] / total,
                     "limit": pol["max_single_stock"], "kind": "max"})
    issuers = r["look_through"]["issuers"]
    if issuers:
        bars.append({"key": "issuer", "label": f"{issuers[0]['name']}穿透曝險", "value": issuers[0]["share"],
                     "limit": pol.get("max_single_issuer"), "kind": "max"})
    us_plan = cfg["plans"].get("US", {}).get("amounts", {})
    if us_plan.get("SMH") and all(us_plan.values()):
        bars.append({"key": "sector", "label": "SMH 占美股扣款", "value": us_plan["SMH"] / sum(us_plan.values()),
                     "limit": pol["max_plan_sector"], "kind": "max"})
    for b in bars:
        b["status"] = status.get(HEALTH_ITEMS[b["key"]], report.INFO)
    return bars


def estate(cfg, r, today):
    """美國遺產稅免稅額使用率，以及依目前美股定額速度（不計漲跌）推估多久會用完。"""
    pol = cfg["policy"]
    situs = sum(h["value"] for h in r["holdings"] if h["market"] == "US")
    limit = pol["us_estate_limit_usd"]
    monthly = sum(v or 0 for v in cfg["plans"].get("US", {}).get("amounts", {}).values())
    months = None if situs >= limit or not monthly else math.ceil((limit - situs) / monthly)
    warn_months = None if situs >= limit * pol["us_estate_warn"] or not monthly \
        else math.ceil((limit * pol["us_estate_warn"] - situs) / monthly)

    def after(n):
        if n is None:
            return None
        y, m = divmod(today.month - 1 + n, 12)
        return f"{today.year + y:04d}-{m + 1:02d}"

    return {"value_usd": round(situs, 2), "limit_usd": limit, "warn": pol["us_estate_warn"],
            "share": situs / limit, "monthly_usd": monthly, "reach": after(months), "warn_reach": after(warn_months),
            "status": {h["item"]: h["status"] for h in r["health"]}.get(HEALTH_ITEMS["estate"], report.INFO)}


def dca_status(cfg, r, txns, today):
    """每個定額標的：本月扣款是否已記帳、自建檔以來的定額累積。"""
    missing = {(x["symbol"], x["plan_month"]) for x in r["missing"]["dca"]}
    price = {h["symbol"]: h for h in r["holdings"]}
    ym = today.strftime("%Y-%m")
    out = []
    for mk, plan in cfg["plans"].items():
        for sym, amt in plan["amounts"].items():
            if not amt:
                continue
            due = date(today.year, today.month, min(plan["day"], 28))
            this = [t for t in txns if t["symbol"] == sym and t["plan_month"] == ym and t["source"] in ("actual", "estimated")]
            if any(t["source"] == "actual" for t in this):
                st = "已記帳"
            elif this:
                st = "估算中，待換成實際成交"
            elif (sym, ym) in missing:
                st = "漏記"
            elif today < due:
                st = f"{due.isoformat()} 扣款"
            else:
                st = "已過扣款日，等資料或待記帳"
            buys = [t for t in txns if t["symbol"] == sym and t["source"] in ("actual", "estimated", "drip")]
            shares = sum(t["shares"] * ledger.split_factor(cfg, sym, t["date"], today.isoformat()) for t in buys)
            cost = sum(t["shares"] * t["price"] + t["fee"] for t in buys)
            h = price.get(sym)
            out.append({"market": mk, "symbol": sym, "amount": amt, "currency": plan["currency"], "day": plan["day"],
                        "status": st, "count": len({t["plan_month"] for t in buys if t["source"] != "drip"}),
                        "drip_count": sum(t["source"] == "drip" for t in buys),
                        "dca_cost": round(cost, 2), "dca_shares": round(shares, 5),
                        "dca_avg": cost / shares if shares else None,
                        "price": h["price"] if h else None,
                        "holding_pnl_pct": h["pnl_pct"] if h else None})
    return out


def build(cfg=None, today=None):
    cfg = cfg or data.load_config()
    today = today or date.today()
    txns = ledger.load()
    r = report.build(cfg, txns)
    s = r["summary"]
    g = growth(cfg, txns, r["dividends"])
    flows = _cash_flows(cfg, txns)
    names = {"0050": "0050 元大台灣50", "VOO": "VOO 標普500"}
    bench = []
    for sym in cfg.get("benchmarks", BENCHMARKS):
        curve = benchmark(cfg, flows, g["date"], sym)
        if not curve or curve[-1] is None:
            continue
        end = curve[-1]
        invested = g["invested"][-1] or 1
        bench.append({"symbol": sym, "name": names.get(sym, sym), "curve": curve, "value_twd": end,
                      "return": end / invested - 1,
                      "xirr": report.xirr([(d, a) for d, a in flows] + [(today.isoformat(), end)])})
    st = r["risk"]["stats"]
    start = cfg.get("tracking_start") or (min(t["date"] for t in txns) if txns else today.isoformat())
    dd_curve = st["drawdown_curve"][-250:] if st else []
    fx_now = r["fx_usd_twd"][1] if r["fx_usd_twd"] else 0
    cal = divcal.build(cfg, r["holdings"], txns, fx_now, today)
    return {
        "as_of": r["as_of"], "today": today.isoformat(), "fx_usd_twd": r["fx_usd_twd"],
        "summary": {**s, "tracking_start": start,
                    "xirr_ready": (date.fromisoformat(start) + timedelta(days=90)).isoformat(),
                    "invested_twd": g["invested"][-1] if g["invested"] else 0,
                    "return_with_div": (g["with_div"][-1] / g["invested"][-1] - 1) if g["invested"] and g["invested"][-1] else None},
        "policy": policy_bars(cfg, r),
        "estate": estate(cfg, r, today),
        "growth": g, "benchmarks": bench,
        "drawdown": {"current": st["current_drawdown"] if st else None, "max": st["max_drawdown"] if st else None,
                     "peak_date": st["peak_date"] if st else None, "trough_date": st["trough_date"] if st else None,
                     "recovered_date": st["recovered_date"] if st else None,
                     "limit": cfg["policy"]["max_drawdown"], "start": st["start"] if st else None,
                     "curve": {"date": [d for d, _ in dd_curve], "dd": [round(v, 4) for _, v in dd_curve]},
                     "holdings": sorted(r["risk"]["holdings_from_high"], key=lambda x: x["from_high"])},
        "dca": dca_status(cfg, r, txns, today),
        "dividends": cal,
        "events": events.build(cfg, r["holdings"], cal["events"], today),
        "market": market.snapshot(),
        "allocation": [{"symbol": h["symbol"], "category": h["category"], "market": h["market"],
                        "value_twd": h["value_twd"], "weight": h["value_twd"] / (s["value_twd"] or 1),
                        "pnl_pct": h["pnl_pct"]} for h in sorted(r["holdings"], key=lambda h: -h["value_twd"])],
        "categories": r["allocation"]["category"],
        "health": r["health"],
    }
