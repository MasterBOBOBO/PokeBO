"""持倉帳本：private/positions.json，每筆是一次買進（或賣出）交易。"""
import json
import math
from datetime import date

from . import data

POSITIONS = data.PRIVATE / "positions.json"


def load():
    if not POSITIONS.exists():
        return []
    return json.loads(POSITIONS.read_text(encoding="utf-8"))["transactions"]


def save(txns):
    POSITIONS.parent.mkdir(parents=True, exist_ok=True)
    txns.sort(key=lambda t: (t["date"], t["market"], t["symbol"]))
    POSITIONS.write_text(json.dumps({"transactions": txns}, ensure_ascii=False, indent=2),
                         encoding="utf-8")


def market_of(symbol, cfg):
    for m, plan in cfg["plans"].items():
        if symbol in plan["amounts"]:
            return m
    return "TW" if symbol[:1].isdigit() else "US"


def fx_on(d, side="spot_sell"):
    hit = data.on_or_before(data.load_series(data.FX_PATH, side), d)
    return hit[1] if hit else None


def plan_month(cfg, market, d):
    """扣款所屬月份：成交日早於扣款日代表是上個月扣款遇假日順延。"""
    dt = date.fromisoformat(d)
    if market in cfg["plans"] and dt.day < cfg["plans"][market]["day"]:
        y, m = (dt.year - 1, 12) if dt.month == 1 else (dt.year, dt.month - 1)
        return f"{y:04d}-{m:02d}"
    return d[:7]


def make_txn(cfg, symbol, d, shares, price, fee=0.0, source="actual", note="", month=None):
    market = market_of(symbol, cfg)
    txn = {
        "date": d, "plan_month": month or plan_month(cfg, market, d),
        "market": market, "symbol": symbol,
        "shares": shares, "price": price, "fee": fee,
        "currency": cfg["plans"][market]["currency"],
        "source": source, "note": note,
    }
    if market == "US":
        txn["fx"] = fx_on(d)
    return txn


def _months(start_ym, end_d):
    y, m = map(int, start_ym.split("-"))
    while (y, m) <= (end_d.year, end_d.month):
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def backfill(cfg, start_ym, market=None):
    """依定期定額規則估算歷史扣款；已有交易的月份不重複建立。"""
    txns = load()
    have = {(t["market"], t["symbol"], t["plan_month"]) for t in txns}
    added = []
    today = date.today()
    for mk, plan in cfg["plans"].items():
        if market and mk != market:
            continue
        for sym, budget in plan["amounts"].items():
            if not budget:
                continue
            series = data.load_series(data.price_path(mk, sym), "close")
            for y, m in _months(start_ym, today):
                if (mk, sym, f"{y:04d}-{m:02d}") in have:
                    continue
                target = date(y, m, min(plan["day"], 28))
                hit = data.on_or_after(series, target.isoformat())
                # 遇假日順延到下一個交易日；7 天內都沒有資料代表還沒發生
                if not hit or (date.fromisoformat(hit[0]) - target).days > 7:
                    continue
                d, price = hit
                if mk == "TW":
                    rate = plan["fee_rate"] * plan["fee_discount"]
                    shares = math.floor(budget / (price * (1 + rate)))
                    fee = max(plan["fee_min"], math.floor(shares * price * rate))
                else:
                    fee = plan.get("fee_per_order", 0)
                    shares = round((budget - fee) / price, 5)
                txn = make_txn(cfg, sym, d, shares, price, fee, source="estimated",
                               note="定期定額估算", month=f"{y:04d}-{m:02d}")
                txns.append(txn)
                added.append(txn)
    save(txns)
    return added


def tracked_symbols(cfg):
    """計畫標的 + 分類表 + 帳本內所有標的，回傳 [(market, symbol)]。"""
    syms = {s for p in cfg["plans"].values() for s in p["amounts"]}
    syms |= set(cfg.get("categories", {})) | {t["symbol"] for t in load()}
    return sorted((market_of(s, cfg), s) for s in syms)


def split_factor(cfg, symbol, txn_date, as_of):
    f = 1
    for s in cfg.get("splits", {}).get(symbol, []):
        if txn_date < s["date"] <= as_of:
            f *= s["ratio"]
    return f


# ---------- 漏記偵測 ----------

def missing_dca(cfg, txns=None, today=None):
    """建檔日之後，已經實際扣款（扣款日之後有交易資料）但帳本沒有當月成交的定額標的。"""
    txns = load() if txns is None else txns
    today = today or date.today()
    start = date.fromisoformat(cfg.get("tracking_start", today.isoformat()))
    have = {(t["symbol"], t["plan_month"]) for t in txns if t["source"] in ("actual", "estimated")}
    out = []
    for mk, plan in cfg["plans"].items():
        for sym, amt in plan["amounts"].items():
            if not amt:
                continue
            series = data.load_series(data.price_path(mk, sym), "close")
            for y, m in _months(f"{start.year:04d}-{start.month:02d}", today):
                target = date(y, m, min(plan["day"], 28))
                if target < start:
                    continue
                hit = data.on_or_after(series, target.isoformat())
                # 扣款日後 7 天內有交易資料 = 已扣款且資料已到
                if not hit or (date.fromisoformat(hit[0]) - target).days > 7:
                    continue
                ym = f"{y:04d}-{m:02d}"
                if (sym, ym) not in have:
                    out.append({"market": mk, "symbol": sym, "plan_month": ym, "trade_date": hit[0],
                                "amount": amt, "currency": plan["currency"]})
    return out


def missing_drip(cfg, txns=None, today=None, wait_days=35):
    """有開股息再投資的標的：除息後超過 wait_days 天，仍沒有 source=drip 的買進紀錄。"""
    txns = load() if txns is None else txns
    today = today or date.today()
    start = cfg.get("tracking_start", today.isoformat())
    out = []
    for mk, plan in cfg["plans"].items():
        for sym in plan.get("drip", []):
            exs = [d for d, _ in data.load_series(data.dividend_path(sym), "cash") if d >= start]
            for i, ex in enumerate(exs):
                if (today - date.fromisoformat(ex)).days < wait_days:
                    continue
                nxt = exs[i + 1] if i + 1 < len(exs) else "9999-12-31"
                if not any(t["symbol"] == sym and t["source"] == "drip" and ex <= t["date"] < nxt for t in txns):
                    out.append({"market": mk, "symbol": sym, "ex_date": ex})
    return out
