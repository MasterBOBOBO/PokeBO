"""券商對帳：比對帳本與券商庫存的股數與成本。

券商庫存檔（private/reconcile/YYYY-MM-DD.csv）欄位：
  symbol,shares,cost,avg_cost,note
  - cost 為總成本（原幣，含手續費），沒有時可只填 avg_cost
  - 截圖對帳時，由 Claude 讀圖後產生此檔
股數不一致不會自動修正（通常是漏記扣款或股息再投資），只列出差異與可能原因；
成本差異可用 --apply-cost 把期初部位的均價校正成券商數字。
"""
import csv
import json
from datetime import date

from . import data, ledger

DIR = data.PRIVATE / "reconcile"
STATE = data.DATA / "state" / "last_reconcile.json"
SHARE_TOL = {"TW": 0.5, "US": 0.00002}
COST_TOL = 0.001          # 成本差 0.1% 以內視為一致（券商四捨五入）


def last_state():
    return json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else None


def read_broker(path):
    rows = {}
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            sym = r["symbol"].strip().upper()
            shares = float(r["shares"])
            cost = float(r["cost"]) if r.get("cost") else (
                float(r["avg_cost"]) * shares if r.get("avg_cost") else None)
            rows[sym] = {"shares": shares, "cost": cost, "note": r.get("note", "")}
    return rows


def ledger_positions(cfg, txns):
    today = date.today().isoformat()
    pos = {}
    for t in txns:
        p = pos.setdefault(t["symbol"], {"market": t["market"], "shares": 0.0, "cost": 0.0})
        p["shares"] += t["shares"] * ledger.split_factor(cfg, t["symbol"], t["date"], today)
        p["cost"] += t["shares"] * t["price"] + t["fee"]
    return {s: p for s, p in pos.items() if abs(p["shares"]) > 1e-9}


def _hint(diff, sym, cfg):
    dca = any(sym in p["amounts"] for p in cfg["plans"].values())
    drip = any(sym in p.get("drip", []) for p in cfg["plans"].values())
    if diff > 0:
        reasons = (["漏記定額扣款"] if dca else []) + (["漏記股息再投資"] if drip else []) + ["漏記買進"]
        return "券商較多：可能" + "、".join(reasons)
    return "帳本較多：可能漏記賣出，或重複記帳"


def compare(cfg, broker, txns=None):
    txns = ledger.load() if txns is None else txns
    mine = ledger_positions(cfg, txns)
    items = []
    for sym in sorted(set(mine) | set(broker)):
        m, b = mine.get(sym), broker.get(sym)
        mk = (m or {}).get("market") or ledger.market_of(sym, cfg)
        it = {"symbol": sym, "market": mk,
              "ledger_shares": m["shares"] if m else 0, "broker_shares": b["shares"] if b else 0,
              "ledger_cost": m["cost"] if m else 0, "broker_cost": b["cost"] if b else None}
        if not m:
            it.update(status="share", hint="帳本沒有此標的：請用 buy 補記")
        elif not b:
            it.update(status="share", hint="券商沒有此持股：可能已賣出但未記帳")
        else:
            d = b["shares"] - m["shares"]
            it["share_diff"] = d
            if abs(d) > SHARE_TOL[mk]:
                it.update(status="share", hint=_hint(d, sym, cfg))
            elif b["cost"] is not None and abs(b["cost"] - m["cost"]) > max(1, m["cost"] * COST_TOL):
                it.update(status="cost", cost_diff=b["cost"] - m["cost"],
                          hint="股數一致、成本不同：可用 --apply-cost 以券商成本校正")
            else:
                it.update(status="ok", hint="")
        items.append(it)
    return items


def apply_cost(cfg, items):
    """只校正「股數一致、成本不同」的標的：調整其期初部位均價，使總成本 = 券商成本。"""
    txns = ledger.load()
    fixed = []
    for it in items:
        if it["status"] != "cost":
            continue
        opening = [t for t in txns if t["symbol"] == it["symbol"] and t["source"] == "opening"]
        if len(opening) != 1:
            continue
        o = opening[0]
        others = sum(t["shares"] * t["price"] + t["fee"] for t in txns if t["symbol"] == it["symbol"] and t is not o)
        o["price"] = round((it["broker_cost"] - others - o["fee"]) / o["shares"], 6)
        o["note"] = (o.get("note", "") + "；成本已依券商對帳校正").lstrip("；")
        it["status"], it["hint"] = "ok", "已以券商成本校正"
        fixed.append(it["symbol"])
    if fixed:
        ledger.save(txns)
    return fixed


def save_state(items, source):
    diffs = [i for i in items if i["status"] != "ok"]
    state = {"date": date.today().isoformat(), "source": str(source), "matched": len(items) - len(diffs),
             "diffs": len(diffs), "diff_symbols": [i["symbol"] for i in diffs]}
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    return state


def render(items):
    label = {"ok": "✅ 一致", "cost": "⚠️ 成本差異", "share": "❌ 股數差異"}
    lines = [f"{'標的':<7}{'帳本股數':>14}{'券商股數':>14}{'帳本成本':>14}{'券商成本':>14}  狀態", "-" * 78]
    for i in items:
        dec = 0 if i["market"] == "TW" else 5
        bc = "—" if i["broker_cost"] is None else f"{i['broker_cost']:,.2f}"
        lines.append(f"{i['symbol']:<7}{i['ledger_shares']:>14,.{dec}f}{i['broker_shares']:>14,.{dec}f}"
                     f"{i['ledger_cost']:>14,.2f}{bc:>14}  {label[i['status']]} {i['hint']}")
    ok = sum(i["status"] == "ok" for i in items)
    lines.append(f"\n共 {len(items)} 檔：一致 {ok}、成本差異 {sum(i['status'] == 'cost' for i in items)}、"
                 f"股數差異 {sum(i['status'] == 'share' for i in items)}")
    return "\n".join(lines)
