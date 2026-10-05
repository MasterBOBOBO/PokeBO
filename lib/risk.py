"""風險分析：以「目前持股數不變」回測過去 N 年的組合市值，算最大回撤與波動度。

回答的問題是「這個組合過去最慘會跌多深」，不是實際歷史績效。
價格使用含息總報酬（股利再投入），避免高股息 ETF 每次除息被誤算成下跌。
"""
import math
from datetime import date

from . import data


def _split_adjusted(cfg, symbol, rows):
    splits = cfg.get("splits", {}).get(symbol, [])
    out = []
    for d, v in rows:
        for sp in splits:
            if d < sp["date"]:
                v /= sp["ratio"]
        out.append((d, v))
    return out


def _total_return(cfg, market, symbol):
    """含息總報酬序列，尾端縮放到最新收盤價，讓「目前股數 × 價格」= 目前市值。"""
    if market == "US":
        rows = data.load_series(data.price_path(market, symbol), "adj_close")
    else:
        closes = _split_adjusted(cfg, symbol, data.load_series(data.price_path(market, symbol), "close"))
        divs = dict(_split_adjusted(cfg, symbol, data.load_series(data.dividend_path(symbol), "cash")))
        rows, idx = [], 1.0
        for (pd_, pp), (d, p) in zip(closes, closes[1:]):
            idx *= (p + divs.get(d, 0)) / pp
            rows.append((d, idx))
        rows = closes[:1] and [(closes[0][0], 1.0)] + rows
    if not rows:
        return {}
    last_close = data.load_series(data.price_path(market, symbol), "close")[-1][1]
    k = last_close / rows[-1][1]
    return {d: v * k for d, v in rows}


def _with_proxy(cfg, market, symbol):
    """上市前用代理標的的報酬率往前延伸（例如 00919 用 0056）。"""
    px = _total_return(cfg, market, symbol)
    proxy = cfg.get("backtest_proxies", {}).get(symbol)
    if not px or not proxy:
        return px, None
    ppx = _total_return(cfg, market, proxy)
    first = min(px)
    if first not in ppx:
        return px, None
    k = px[first] / ppx[first]
    merged = {d: v * k for d, v in ppx.items() if d < first}
    merged.update(px)
    return merged, proxy


def backtest(cfg, holdings, years=5):
    start = date(date.today().year - years, date.today().month, 1).isoformat()
    fx = dict(data.load_series(data.FX_PATH, "spot_buy"))
    series, skipped, proxied = {}, [], {}
    for h in holdings:
        px, proxy = _with_proxy(cfg, h["market"], h["symbol"])
        if proxy:
            proxied[h["symbol"]] = proxy
        # 上市不到回測起點的標的（例如 SPCX）無法回測，排除並列出
        if not px or min(px) > start:
            skipped.append(h["symbol"])
            continue
        series[h["symbol"]] = (h, px)
    days = sorted({d for _, px in series.values() for d in px if d >= start})
    last = {s: None for s in series}
    last_fx = None
    curve = []
    for d in days:
        last_fx = fx.get(d, last_fx)
        total = 0.0
        for sym, (h, px) in series.items():
            if d in px:
                last[sym] = px[d]
            if last[sym] is None:
                break
            total += h["shares"] * last[sym] * ((last_fx or 0) if h["market"] == "US" else 1)
        else:
            if last_fx:
                curve.append((d, total))
    return curve, skipped, proxied


def drawdown_stats(curve):
    if len(curve) < 2:
        return None
    peak_v, peak_d = curve[0][1], curve[0][0]
    worst = (0.0, None, None)
    dd_curve = []
    for d, v in curve:
        if v > peak_v:
            peak_v, peak_d = v, d
        dd = v / peak_v - 1
        dd_curve.append((d, dd))
        if dd < worst[0]:
            worst = (dd, peak_d, d)
    # 回撤後回到前高的日期
    recovered = None
    if worst[1]:
        peak_val = dict(curve)[worst[1]]
        recovered = next((d for d, v in curve if d > worst[2] and v >= peak_val), None)
    rets = [b[1] / a[1] - 1 for a, b in zip(curve, curve[1:]) if a[1]]
    mean = sum(rets) / len(rets)
    vol = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)) * math.sqrt(250)
    return {
        "max_drawdown": worst[0], "peak_date": worst[1], "trough_date": worst[2],
        "recovered_date": recovered, "current_drawdown": dd_curve[-1][1],
        "volatility": vol, "start": curve[0][0], "end": curve[-1][0],
        "drawdown_curve": dd_curve,
    }


def holding_drawdowns(cfg, holdings):
    """各標的距 52 週高點的跌幅。"""
    out = []
    for h in holdings:
        px = _split_adjusted(cfg, h["symbol"],
                             data.load_series(data.price_path(h["market"], h["symbol"]), "close"))
        px = dict(px)
        if not px:
            continue
        last_d = max(px)
        cutoff = date.fromisoformat(last_d).replace(year=date.fromisoformat(last_d).year - 1)
        window = [v for d, v in px.items() if d >= cutoff.isoformat()]
        out.append({"symbol": h["symbol"], "high_52w": max(window),
                    "from_high": px[last_d] / max(window) - 1})
    return out


def analyze(cfg, holdings):
    curve, skipped, proxied = backtest(cfg, holdings)
    stats = drawdown_stats(curve)
    return {"backtest_curve": curve, "skipped": skipped, "proxied": proxied, "stats": stats,
            "holdings_from_high": holding_drawdowns(cfg, holdings)}
