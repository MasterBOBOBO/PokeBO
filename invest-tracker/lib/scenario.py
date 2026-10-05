"""情境模擬：把調整方案套進目前帳本，重算 Health Check，不寫入帳本。

情境檔（scenarios/*.json）格式：
{
  "name": "...",
  "trades": [{"symbol": "0056", "shares": -12000}, {"symbol": "VOO", "shares": 30.5}],
  "plan_amounts": {"US": {"VOO": 100, "QQQM": 50, "SMH": 50}, "TW": {"0056": 0}},
  "assumptions": {"tw_sell_cost": 0.002425, "us_buy_fee": 0.001}
}
成交價假設為最新收盤價；實際執行時價格會不同，所以目標要留緩衝。
"""
import copy
import json

from . import data, ledger, report


def load(path):
    return json.loads(open(path, encoding="utf-8").read())


def apply(cfg, sc):
    cfg2 = copy.deepcopy(cfg)
    for mk, amounts in sc.get("plan_amounts", {}).items():
        plan = cfg2["plans"][mk]["amounts"]
        for sym, amt in amounts.items():
            if amt:
                plan[sym] = amt
            else:
                plan.pop(sym, None)
    txns = ledger.load()
    fx = data.load_series(data.FX_PATH, "spot_sell")[-1][1]
    a = sc.get("assumptions", {})
    cash = {"tw_proceeds": 0.0, "tw_costs": 0.0, "us_spent_usd": 0.0, "us_fees_usd": 0.0}
    for t in sc["trades"]:
        mk = ledger.market_of(t["symbol"], cfg2)
        d, px = data.load_series(data.price_path(mk, t["symbol"]), "close")[-1]
        gross = abs(t["shares"]) * px
        if mk == "TW" and t["shares"] < 0:
            cash["tw_proceeds"] += gross
            cash["tw_costs"] += gross * a.get("tw_sell_cost", 0.002425)
        elif mk == "TW":
            cash["tw_proceeds"] -= gross
            cash["tw_costs"] += gross * a.get("tw_buy_cost", 0.001425)
        else:
            cash["us_spent_usd"] += gross * (1 if t["shares"] > 0 else -1)
            cash["us_fees_usd"] += gross * a.get("us_buy_fee", 0.001)
        txn = ledger.make_txn(cfg2, t["symbol"], d, t["shares"], px, source="scenario",
                              note=sc.get("name", ""))
        if mk == "US":
            txn["fx"] = fx
        txns.append(txn)
    cash["fx"] = fx
    cash["us_spent_twd"] = (cash["us_spent_usd"] + cash["us_fees_usd"]) * fx
    cash["net_twd"] = cash["tw_proceeds"] - cash["tw_costs"] - cash["us_spent_twd"]
    return cfg2, txns, cash


def compare(cfg, sc):
    before = report.build(cfg)
    cfg2, txns, cash = apply(cfg, sc)
    after = report.build(cfg2, txns)
    return before, after, cash


def render(before, after, cash, sc):
    def dv(r):
        return sum(d["net_twd"] for d in r["dividend_outlook"])

    def mdd(r):
        st = r["risk"]["stats"]
        return f"{st['max_drawdown']:.1%}" if st else "n/a"

    lines = [f"情境：{sc.get('name', '')}", "=" * 72, "", "交易（以最新收盤價估算）"]
    for t in sc["trades"]:
        lines.append(f"  {'賣出' if t['shares'] < 0 else '買進'} {t['symbol']:<6} {abs(t['shares']):>12,.4f} 股")
    lines += [
        "",
        f"台股賣出淨額   NT$ {cash['tw_proceeds'] - cash['tw_costs']:>12,.0f}（交易成本 {cash['tw_costs']:,.0f}）",
        f"美股買進       US$ {cash['us_spent_usd']:>12,.2f}（手續費 {cash['us_fees_usd']:,.2f}，"
        f"匯率 {cash['fx']}）≈ NT$ {cash['us_spent_twd']:,.0f}",
        f"剩餘現金       NT$ {cash['net_twd']:>12,.0f}",
        "",
        f"{'指標':<16}{'調整前':>14}{'調整後':>14}",
        "-" * 44,
        f"{'美股占比':<16}{before['allocation']['market'].get('US', 0):>14.1%}"
        f"{after['allocation']['market'].get('US', 0):>14.1%}",
        f"{'高股息占比':<15}{before['allocation']['category'].get('台股高股息', 0):>14.1%}"
        f"{after['allocation']['category'].get('台股高股息', 0):>14.1%}",
        f"{'預估年股利(淨)':<13}{dv(before):>14,.0f}{dv(after):>14,.0f}",
        f"{'回測最大回撤':<14}{mdd(before):>14}{mdd(after):>14}",
        "",
        "健檢（調整前 → 調整後）",
    ]
    prev = {h["item"]: h["status"] for h in before["health"]}
    for h in after["health"]:
        lines.append(f"  {prev.get(h['item'], '—'):<6} → {h['status']:<6} {h['item']}：{h['detail']}")
    return "\n".join(lines)
