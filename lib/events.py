"""持股事件行事曆：未來 N 天和你的持股有關的日期。

- 除息、股利入帳（來自股利行事曆 divcal）
- 定期定額扣款日（遇假日順延，實際日期以券商為準）
- 台股個股：月營收公布期限（每月 10 日）、財報公布期限（法定期限，公司可能提前公布）
- 美國 FOMC 利率決議（聯準會行事曆，台灣時間隔天凌晨公布）
法說會、美股個股財報需要付費或無公開 API，目前不提供。
"""
from datetime import date, timedelta

from . import market

# 證交法第 36 條：一般公司年報 3/31、Q1 5/15、Q2 8/14、Q3 11/14；
# 金控、銀行、票券、保險：Q1 5/30、半年報 8/31、Q3 11/29
REPORT_DEADLINES = {"general": [("03-31", "年度財報"), ("05-15", "第一季財報"), ("08-14", "第二季財報"), ("11-14", "第三季財報")],
                    "financial": [("03-31", "年度財報"), ("05-30", "第一季財報"), ("08-31", "半年度財報"), ("11-29", "第三季財報")]}
STOCK_CATEGORIES = ("台股個股", "台股金融股")


def _months(today, end):
    y, m = today.year, today.month
    while date(y, m, 1) <= end:
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def build(cfg, holdings, dividend_events, today=None, days=60, fomc=None):
    today = today or date.today()
    end = today + timedelta(days=days)
    out = []

    def add(d, kind, title, detail="", symbols=()):
        if today.isoformat() <= d <= end.isoformat():
            out.append({"date": d, "kind": kind, "title": title, "detail": detail, "symbols": list(symbols)})

    for e in dividend_events:
        if e["status"] != "待入帳":
            add(e["ex_date"], "除息", f"{e['symbol']} 除息",
                f"每股 {'US$' if e['currency'] == 'USD' else ''}{e['per_share']}（{e['status']}）", [e["symbol"]])
        if e["pay_date"]:
            add(e["pay_date"], "入帳", f"{e['symbol']} 股利入帳",
                f"預估淨額 NT${e['net_twd']:,}" + ("，記得記錄股息再投資" if e["drip"] else ""), [e["symbol"]])

    for mk, plan in cfg["plans"].items():
        syms = [s for s, a in plan["amounts"].items() if a]
        if not syms:
            continue
        for y, m in _months(today, end):
            add(date(y, m, min(plan["day"], 28)).isoformat(), "扣款", f"{'台股' if mk == 'TW' else '美股'}定期定額扣款",
                f"{'、'.join(syms)}（遇假日順延）；成交後貼截圖或用 /buy 記帳", syms)

    stocks = [h for h in holdings if h["market"] == "TW" and h["category"] in STOCK_CATEGORIES]
    if stocks:
        syms = [h["symbol"] for h in stocks]
        for y, m in _months(today, end):
            py, pm = (y - 1, 12) if m == 1 else (y, m - 1)
            add(date(y, m, 10).isoformat(), "營收", f"{pm} 月營收公布期限", "、".join(syms), syms)
        for kind in ("general", "financial"):
            group = [h["symbol"] for h in stocks if (h["category"] == "台股金融股") == (kind == "financial")]
            if not group:
                continue
            for y in (today.year, today.year + 1):
                for md, name in REPORT_DEADLINES[kind]:
                    label = f"{y - 1} 年{name}" if name == "年度財報" else f"{y} 年{name}"
                    add(f"{y}-{md}", "財報", f"{label}公布期限", "、".join(group) + "（法定期限，公司可能提前公布）", group)

    for mtg in market.fomc_meetings() if fomc is None else fomc:
        add(mtg["date"], "總經", "FOMC 利率決議",
            "台灣時間隔天凌晨公布" + ("；同時公布經濟預測與點陣圖" if mtg["sep"] else ""))

    order = {"總經": 0, "扣款": 1, "除息": 2, "入帳": 3, "營收": 4, "財報": 5}
    return sorted(out, key=lambda e: (e["date"], order.get(e["kind"], 9)))
