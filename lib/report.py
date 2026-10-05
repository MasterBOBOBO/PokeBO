"""績效計算：持倉、損益、配息、XIRR、配置偏離。全部換算 TWD。"""
from datetime import date

from . import data, ledger, risk


def xirr(flows):
    """flows: [(date_str, amount)]，負數為投入。二分法求年化報酬率。"""
    flows = sorted(flows)
    if not flows or all(a >= 0 for _, a in flows) or all(a <= 0 for _, a in flows):
        return None
    t0 = date.fromisoformat(flows[0][0])
    # 期間太短時年化數字會失真，不顯示
    if (date.fromisoformat(flows[-1][0]) - t0).days < 90:
        return None
    ts = [((date.fromisoformat(d) - t0).days / 365.0, a) for d, a in flows]

    def npv(r):
        return sum(a / (1 + r) ** t for t, a in ts)

    lo, hi = -0.99, 10.0
    if npv(lo) * npv(hi) > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        if npv(lo) * npv(mid) <= 0:
            hi = mid
        else:
            lo = mid
    return (lo + hi) / 2


def _net(cfg, market, gross_twd):
    """台股單筆股利達門檻扣二代健保補充保費；美股扣 30% 預扣稅。"""
    tax = cfg["tax"]
    if market == "US":
        return gross_twd * (1 - tax["us_withholding"])
    if gross_twd >= tax["tw_nhi_threshold"]:
        return gross_twd * (1 - tax["tw_nhi_rate"])
    return gross_twd


def _dividend_series(market, symbol):
    if market == "US":
        return data.us_dividends(symbol)
    return data.load_series(data.dividend_path(symbol), "cash")


def dividends(cfg, txns, market, symbol, as_of):
    """依除息日前的持股計算已領股利（淨額，TWD）。"""
    out = []
    for ex, cash in _dividend_series(market, symbol):
        if ex > as_of:
            continue
        held = sum(t["shares"] * ledger.split_factor(cfg, symbol, t["date"], ex)
                   for t in txns if t["symbol"] == symbol and t["date"] < ex)
        if held > 0:
            fx = ledger.fx_on(ex, "spot_buy") if market == "US" else 1
            gross = held * cash * fx
            out.append({"date": ex, "symbol": symbol, "shares": held, "per_share": cash,
                        "gross_twd": round(gross), "amount_twd": round(_net(cfg, market, gross))})
    return out


def dividend_outlook(cfg, holdings, fx_now):
    """以近 12 個月每次配息 × 目前股數，估算未來一年股利現金流。"""
    out = []
    for h in holdings:
        series = _dividend_series(h["market"], h["symbol"])
        last = h["price_date"]
        cutoff = date.fromisoformat(last).replace(year=date.fromisoformat(last).year - 1)
        events = [(d, c) for d, c in series if cutoff.isoformat() < d <= last]
        if not events:
            continue
        rate = fx_now if h["market"] == "US" else 1
        payments = [h["shares"] * c * rate for _, c in events]
        net = sum(_net(cfg, h["market"], p) for p in payments)
        nhi = sum(p * cfg["tax"]["tw_nhi_rate"] for p in payments
                  if h["market"] == "TW" and p >= cfg["tax"]["tw_nhi_threshold"])
        out.append({"symbol": h["symbol"], "times": len(events),
                    "gross_twd": round(sum(payments)), "net_twd": round(net),
                    "nhi_twd": round(nhi), "yield_on_value": sum(payments) / h["value_twd"],
                    "per_share_12m": sum(c for _, c in events)})
    return sorted(out, key=lambda x: -x["net_twd"])


def build(cfg, txns=None):
    """txns 可傳入假想交易（情境模擬用），預設讀帳本。"""
    txns = ledger.load() if txns is None else txns
    fx_series = data.load_series(data.FX_PATH, "spot_buy")
    fx_now = fx_series[-1] if fx_series else None
    holdings, flows, flows_by_mkt = [], [], {"TW": [], "US": []}

    symbols = sorted({(t["market"], t["symbol"]) for t in txns})
    for mk, sym in symbols:
        series = data.load_series(data.price_path(mk, sym), "close")
        if not series:
            continue
        px_date, px = series[-1]
        mine = [t for t in txns if t["symbol"] == sym]
        shares = cost = cost_twd = 0.0
        for t in mine:
            f = ledger.split_factor(cfg, sym, t["date"], px_date)
            shares += t["shares"] * f
            c = t["shares"] * t["price"] + t["fee"]
            c_twd = c * (t.get("fx") or 1) if mk == "US" else c
            cost += c
            cost_twd += c_twd
            # 期初部位沒有歷史日期，XIRR 以建檔當日市值當作投入（= 自追蹤起報酬）
            if t["source"] == "opening":
                hit = data.on_or_before(series, t["date"])
                fx = ledger.fx_on(t["date"], "spot_buy") if mk == "US" else 1
                c_twd = t["shares"] * hit[1] * fx if hit else c_twd
            flows.append((t["date"], -c_twd))
            flows_by_mkt[mk].append((t["date"], -c_twd))
        rate = fx_now[1] if mk == "US" else 1
        value = shares * px
        value_twd = value * rate
        h = {"market": mk, "symbol": sym, "shares": round(shares, 5), "price": px,
             "price_date": px_date, "currency": mine[0]["currency"],
             "cost": round(cost, 2), "value": round(value, 2),
             "cost_twd": round(cost_twd), "value_twd": round(value_twd),
             "pnl_twd": round(value_twd - cost_twd),
             "pnl_pct": (value_twd / cost_twd - 1) if cost_twd else 0,
             "category": cfg.get("categories", {}).get(sym, "其他"),
             "estimated_txns": sum(t["source"] == "estimated" for t in mine),
             "txns": len(mine), "first": mine[0]["date"], "last": mine[-1]["date"]}
        holdings.append(h)
        today = date.today().isoformat()
        flows.append((today, value_twd))
        flows_by_mkt[mk].append((today, value_twd))

    divs = []
    for mk, sym in symbols:
        divs += dividends(cfg, txns, mk, sym, date.today().isoformat())
    for d in divs:
        mk = ledger.market_of(d["symbol"], cfg)
        flows.append((d["date"], d["amount_twd"]))
        flows_by_mkt[mk].append((d["date"], d["amount_twd"]))

    # 配置偏離：計畫內各標的「持倉市值比例」對比「每月扣款金額比例」
    drift = []
    for mk, plan in cfg["plans"].items():
        amounts = plan["amounts"]
        if not plan.get("drift_check", True) or len(amounts) < 2 or not all(amounts.values()):
            continue
        targets = {s: a / sum(amounts.values()) for s, a in amounts.items()}
        hs = [h for h in holdings if h["symbol"] in targets]
        total = sum(h["value"] for h in hs)
        if not total:
            continue
        for sym, w in targets.items():
            v = next((h["value"] for h in hs if h["symbol"] == sym), 0)
            actual = v / total
            drift.append({"market": mk, "symbol": sym, "target": w, "actual": actual,
                          "diff": actual - w,
                          "alert": abs(actual - w) >= cfg["policy"]["max_drift"]})

    cost = sum(h["cost_twd"] for h in holdings)
    value = sum(h["value_twd"] for h in holdings)
    alloc = {}
    for h in holdings:
        for key in (("market", h["market"]), ("category", h["category"])):
            alloc.setdefault(key[0], {}).setdefault(key[1], 0)
            alloc[key[0]][key[1]] += h["value_twd"]
    alloc = {k: {n: v / value for n, v in sorted(g.items(), key=lambda x: -x[1])}
             for k, g in alloc.items()} if value else {}
    alloc.setdefault("market", {})
    alloc.setdefault("category", {})
    div_total = sum(d["amount_twd"] for d in divs)
    r = {
        "as_of": max((h["price_date"] for h in holdings), default=None),
        "fx_usd_twd": fx_now,
        "summary": {"cost_twd": cost, "value_twd": value, "dividends_twd": div_total,
                    "pnl_twd": value + div_total - cost,
                    "pnl_pct": (value + div_total) / cost - 1 if cost else 0,
                    "xirr": xirr(flows),
                    "xirr_tw": xirr(flows_by_mkt["TW"]), "xirr_us": xirr(flows_by_mkt["US"])},
        "holdings": holdings, "dividends": divs, "drift": drift, "allocation": alloc,
        "has_opening": any(t["source"] == "opening" for t in txns),
        "dividend_outlook": dividend_outlook(cfg, holdings, fx_now[1] if fx_now else 0),
        "risk": risk.analyze(cfg, holdings),
        "plans": cfg["plans"],
    }
    r["look_through"] = look_through(cfg, holdings, value)
    r["missing"] = {"dca": ledger.missing_dca(cfg, txns), "drip": ledger.missing_drip(cfg, txns)}
    from . import reconcile
    r["reconcile"] = reconcile.last_state()
    r["health"] = health_checks(cfg, r)
    return r


def look_through(cfg, holdings, total):
    """穿透計算單一公司曝險：直接持股 + ETF 市值 × 成分權重（只涵蓋設定中有權重的 ETF）。"""
    lt = cfg.get("look_through", {})
    names = cfg.get("issuer_names", {})
    exp = {}
    for h in holdings:
        if h["symbol"] in lt:
            for sym, w in lt[h["symbol"]]["weights"].items():
                exp.setdefault(sym, {"direct": 0, "via_etf": 0})["via_etf"] += h["value_twd"] * w
        elif h["category"] in ("台股個股", "台股金融股"):
            exp.setdefault(h["symbol"], {"direct": 0, "via_etf": 0})["direct"] += h["value_twd"]
    from . import ta

    def name_of(sym):
        if sym in names:
            return names[sym]
        info = ta.stock_info(sym) if sym[:1].isdigit() else None
        return info["stock_name"] if info else sym

    out = [{"symbol": s, "name": name_of(s), "direct_twd": round(v["direct"]),
            "via_etf_twd": round(v["via_etf"]), "share": (v["direct"] + v["via_etf"]) / total if total else 0}
           for s, v in exp.items()]
    return {"issuers": sorted(out, key=lambda x: -x["share"])[:5],
            "as_of": {k: v["as_of"] for k, v in lt.items()}}


PASS, RISK, ISSUE, INFO = "✅ 通過", "⚠️ 風險", "❌ 問題", "ℹ️ 資訊"


def health_checks(cfg, r):
    """規則式健檢（✅ 通過 / ⚠️ 風險 / ❌ 問題 / ℹ️ 資訊）。門檻來自 config.json > policy。"""
    out = []

    def add(status, item, detail):
        out.append({"status": status, "item": item, "detail": detail})

    if not r["holdings"]:
        add(INFO, "尚無持倉", "先用 `invest.py buy ... --source opening` 建立期初部位，健檢才會開始計算")
        return out

    lag = (date.today() - date.fromisoformat(r["as_of"])).days if r["as_of"] else 99
    add(PASS if lag <= 4 else ISSUE, "資料是否最新", f"價格資料日 {r['as_of']}（{lag} 天前）")

    pol = cfg["policy"]
    alloc = r["allocation"]
    us = alloc["market"].get("US", 0)
    add(RISK if us < pol["min_us_share"] else PASS, "地區分散",
        f"美股 {us:.1%} / 台股 {alloc['market'].get('TW', 0):.1%}（政策：美股 ≥ {pol['min_us_share']:.0%}）")

    hd = alloc["category"].get("台股高股息", 0)
    add(RISK if hd > pol["max_high_dividend"] else PASS, "高股息 ETF 占比",
        f"占組合 {hd:.1%}（政策：≤ {pol['max_high_dividend']:.0%}）")

    top = max((h for h in r["holdings"] if h["category"] in ("台股個股", "台股金融股", "美股個股")),
              key=lambda h: h["value_twd"], default=None)
    if top:
        w = top["value_twd"] / r["summary"]["value_twd"]
        add(RISK if w > pol["max_single_stock"] else PASS, "單一個股占比",
            f"最大直接持股 {top['symbol']} 占 {w:.1%}（政策：≤ {pol['max_single_stock']:.0%}）")

    lt = r.get("look_through", {})
    if lt.get("issuers"):
        top_i = lt["issuers"][0]
        limit_i = pol.get("max_single_issuer")
        stale = [k for k, d in lt["as_of"].items() if (date.today() - date.fromisoformat(d)).days > 120]
        detail = (f"{top_i['name']} 穿透後占組合 {top_i['share']:.1%}"
                  f"（直接 NT${top_i['direct_twd']:,} + 經由 ETF NT${top_i['via_etf_twd']:,}）")
        if stale:
            status = RISK
            detail += f"；ETF 成分權重已過期：{'、'.join(stale)}"
        elif limit_i is None:
            status = INFO
            detail += "（政策：尚未設定）"
        else:
            status = RISK if top_i["share"] > limit_i else PASS
            detail += f"（政策：≤ {limit_i:.0%}）"
        add(status, "單一公司穿透曝險", detail)

    us_plan = cfg["plans"].get("US", {}).get("amounts", {})
    if us_plan.get("SMH"):
        semi = us_plan["SMH"] / sum(us_plan.values())
        add(RISK if semi > pol["max_plan_sector"] else PASS, "美股定額產業集中",
            f"SMH（半導體）占每月美股扣款 {semi:.0%}（政策：≤ {pol['max_plan_sector']:.0%}）")

    alerts = [d for d in r["drift"] if d["alert"]]
    add(RISK if alerts else PASS, "定額配置偏離",
        "、".join(f"{d['symbol']} {d['diff'] * 100:+.1f}pt" for d in alerts)
        or f"持倉比例都在扣款比例 ±{pol['max_drift'] * 100:.0f}pt 內")

    st = r["risk"]["stats"]
    if st:
        add(RISK if st["max_drawdown"] < pol["max_drawdown"] else PASS, "歷史最大回撤",
            f"{st['max_drawdown']:.1%}（{st['peak_date']} → {st['trough_date']}），以目前持股回測 "
            f"{st['start'][:4]}–{st['end'][:4]}（政策：≥ {pol['max_drawdown']:.0%}）")

    # 非美國稅務居民持有美國資產超過 6 萬美元的部分，身故時須課美國遺產稅
    situs = sum(h["value"] for h in r["holdings"] if h["market"] == "US")
    limit = pol["us_estate_limit_usd"]
    add(ISSUE if situs > limit else RISK if situs > limit * pol["us_estate_warn"] else PASS,
        "美國遺產稅曝險", f"美國資產 US${situs:,.0f}，非居民免稅額 US${limit:,.0f}")

    m = r.get("missing", {})
    gaps = [f"{x['symbol']} {x['plan_month']} 扣款（{x['trade_date']}）" for x in m.get("dca", [])]
    gaps += [f"{x['symbol']} {x['ex_date']} 除息的股息再投資" for x in m.get("drip", [])]
    est = sum(h["estimated_txns"] for h in r["holdings"])
    if est:
        gaps.append(f"{est} 筆估算交易待換成實際成交")
    add(RISK if gaps else PASS, "扣款記帳", "、".join(gaps) + " 尚未記帳" if gaps
        else f"自 {cfg.get('tracking_start', '建檔')} 起的定額扣款都已記錄")

    rc = r.get("reconcile")
    if not rc:
        add(RISK, "券商對帳", "尚未和券商庫存對帳")
    else:
        days = (date.today() - date.fromisoformat(rc["date"])).days
        due = pol.get("reconcile_every_days", 95)
        if rc["diffs"]:
            add(RISK, "券商對帳", f"{rc['date']} 對帳有 {rc['diffs']} 檔差異未處理：{'、'.join(rc['diff_symbols'])}")
        elif days > due:
            add(RISK, "券商對帳", f"上次對帳 {rc['date']}（{days} 天前），已超過 {due} 天")
        else:
            add(PASS, "券商對帳", f"{rc['date']} 對帳一致（{rc['matched']} 檔），下次請在 {due - days} 天內對帳")
    return out


def _pct(x):
    return "未滿90天" if x is None else f"{x * 100:+6.2f}%"


def render(r):
    s = r["summary"]
    lines = [
        f"投資組合報告（資料日 {r['as_of']}，USD/TWD {r['fx_usd_twd'][1] if r['fx_usd_twd'] else 'n/a'}）",
        "=" * 64,
        f"總投入   NT$ {s['cost_twd']:>12,.0f}",
        f"目前市值 NT$ {s['value_twd']:>12,.0f}",
        f"已領配息 NT$ {s['dividends_twd']:>12,.0f}   (淨額，自建檔起)",
        f"總損益   NT$ {s['pnl_twd']:>12,.0f}   {_pct(s['pnl_pct'])}",
        f"年化報酬 XIRR{'（自建檔起）' if r['has_opening'] else ''} 全部 {_pct(s['xirr'])}"
        f" ｜ 台股 {_pct(s['xirr_tw'])} ｜ 美股 {_pct(s['xirr_us'])}",
        "",
        f"{'標的':<6}{'股數':>12}{'現價':>10}{'成本(TWD)':>13}{'市值(TWD)':>13}{'報酬':>9}",
        "-" * 64,
    ]
    for h in r["holdings"]:
        flag = " *" if h["estimated_txns"] else ""
        lines.append(f"{h['symbol']:<6}{h['shares']:>12,.4f}{h['price']:>10,.2f}"
                     f"{h['cost_twd']:>13,.0f}{h['value_twd']:>13,.0f}{_pct(h['pnl_pct']):>9}{flag}")
    if any(h["estimated_txns"] for h in r["holdings"]):
        lines.append("  * 含定期定額估算交易，可用實際成交紀錄修正")
    if r["allocation"]:
        lines += ["", "資產配置（占總市值）"]
        lines.append("  " + " ｜ ".join(f"{k} {v:.1%}" for k, v in r["allocation"]["market"].items()))
        for k, v in r["allocation"]["category"].items():
            lines.append(f"  {k:<8}{v:>7.1%}  {'█' * round(v * 40)}")
    if r["drift"]:
        lines += ["", "美股定額配置偏離（目標 → 實際）"]
        for d in r["drift"]:
            mark = "⚠️ 建議再平衡" if d["alert"] else "✅"
            lines.append(f"  {d['symbol']:<6} {d['target']:>5.0%} → {d['actual']:>6.1%}"
                         f"  ({d['diff'] * 100:+.1f}pt) {mark}")
    return "\n".join(lines)


SNAPSHOT = data.DATA / "state" / "portfolio_snapshot.json"


def snapshot(cfg=None, refresh=False):
    """給儀表板用的輕量組合快照（各標的與類別占比、政策門檻），同一天只計算一次。"""
    import json
    today = date.today().isoformat()
    if SNAPSHOT.exists() and not refresh:
        snap = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
        if snap.get("date") == today:
            return snap
    cfg = cfg or data.load_config()
    r = build(cfg)
    total = r["summary"]["value_twd"] or 1
    snap = {"date": today, "value_twd": total,
            "holdings": {h["symbol"]: {"weight": h["value_twd"] / total, "category": h["category"]} for h in r["holdings"]},
            "category": r["allocation"]["category"], "market": r["allocation"]["market"],
            "look_through": {x["symbol"]: x["share"] for x in r["look_through"]["issuers"]},
            "health": [{"item": h["item"], "status": h["status"]} for h in r["health"]]}
    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
    return snap
