"""每週摘要：本週組合與大盤表現、漲跌貢獻、健檢狀態、未來兩週事件、市場溫度計。

本機存一份完整版（reports/weekly/，含金額）；送到手機的版本預設不含金額與股數
（config.json > notify.include_amounts = true 才會附上），只放百分比、狀態與日期。
"""
import re
from datetime import date, timedelta

from . import data, home

WEEK_DAYS = 5            # 以最近 5 個交易日當作「本週」


def _ret(curve, n=WEEK_DAYS):
    pts = [v for v in curve if v]
    if len(pts) < 2:
        return None
    base = pts[-1 - min(n, len(pts) - 1)]
    return pts[-1] / base - 1 if base else None


def base_date(h, n=WEEK_DAYS):
    """本週的起算日：最近 n 個交易日前；建檔不滿 n 天時用建檔日，和組合報酬一致。"""
    d = h["growth"]["date"]
    return d[-1 - min(n, len(d) - 1)] if d else None


def contributions(h, start):
    """各持股自 start 起的漲跌 × 期初權重（百分點），只看價格，不含股利。"""
    out = []
    for a in h["allocation"]:
        s = data.load_series(data.price_path(a["market"], a["symbol"]), "close")
        hit = data.on_or_before(s, start) if s else None
        if not hit or len(s) < 2:
            continue
        base = hit[1]
        r = s[-1][1] / base - 1 if base else 0
        w0 = a["weight"] / (1 + r) if r > -1 else a["weight"]
        out.append({"symbol": a["symbol"], "ret": r, "pp": w0 * r})
    return sorted(out, key=lambda x: -x["pp"])


def _pct(v, d=1):
    return "—" if v is None else f"{v * 100:+.{d}f}%"


def build(cfg=None, h=None, include_amounts=None, today=None):
    """回傳 (標題, 內文, 短摘要)。"""
    cfg = cfg or data.load_config()
    h = h or home.build(cfg)
    today = today or date.today()
    if include_amounts is None:
        include_amounts = cfg.get("notify", {}).get("include_amounts", False)
    s = h["summary"]
    me = _ret(h["growth"]["with_div"])
    start = base_date(h)
    lines = [f"資料日 {h['as_of']}（自 {start} 起算）" if start else f"資料日 {h['as_of']}", ""]

    lines.append("【本週表現】")
    lines.append(f"我的組合 {_pct(me, 2)}" + (f"（市值 NT${s['value_twd']:,.0f}）" if include_amounts else ""))
    for b in h["benchmarks"]:
        lines.append(f"同樣投入買 {b['name']} {_pct(_ret(b['curve']), 2)}")
    con = contributions(h, start) if start else []
    if con:
        up = [c for c in con[:2] if c["pp"] >= 0.00005]          # 小於 0.01pt 的不列
        down = [c for c in con[::-1][:2] if c["pp"] <= -0.00005]
        if up:
            lines.append("拉抬：" + "、".join(f"{c['symbol']} {_pct(c['ret'])}（{c['pp'] * 100:+.2f}pt）" for c in up))
        if down:
            lines.append("拖累：" + "、".join(f"{c['symbol']} {_pct(c['ret'])}（{c['pp'] * 100:+.2f}pt）" for c in down))

    risks = [x for x in h["health"] if x["status"][:1] in ("⚠", "❌")]
    lines += ["", "【健檢】"]
    lines += [f"{x['status']} {x['item']}" for x in risks] or ["✅ 全部通過"]

    end = (today + timedelta(days=14)).isoformat()
    ev = [e for e in h["events"] if e["date"] <= end]
    lines += ["", "【未來兩週】"]
    for e in ev:
        detail = e["detail"] if include_amounts else re.sub(r"[，,]?\s*預估淨額 NT\$[\d,]+[，,]?", "", e["detail"]).strip("，")
        lines.append(f"{e['date'][5:].replace('-', '/')} {e['title']}" + (f"：{detail}" if detail else ""))
    if not ev:
        lines.append("沒有事件")
    if include_amounts:
        due = [x for x in h["dividends"]["events"] if today.isoformat() <= (x["pay_date"] or "") <= end]
        if due:
            lines.append(f"兩週內預估股利入帳淨額 NT${sum(x['net_twd'] for x in due):,}")

    m = h["market"]
    if m["items"]:
        lines += ["", f"【市場溫度計】偏熱 {m['hot']}・中性 {m['neutral']}・偏冷 {m['cold']}"]
        lines += [f"{x['name']}：{x['value']}（{x['heat']}）" for x in m["items"] if not x.get("error")]

    miss = [x for x in h["dca"] if x["status"] == "漏記"]
    if miss:
        lines += ["", "【待記帳】" + "、".join(x["symbol"] for x in miss) + " 本月扣款尚未記帳"]

    lines += ["", "以上只描述事實與投資政策比較，不是買賣建議；定期定額照原設定扣款。"]
    title = f"投資週報 {today.month}/{today.day}"
    short = f"本週 {_pct(me, 2)}；風險 {len(risks)} 項；未來兩週 {len(ev)} 件事"
    return title, "\n".join(lines), short


def save(text, today=None):
    today = today or date.today()
    path = data.ROOT / "reports" / "weekly" / f"{today.isoformat()}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n", encoding="utf-8")
    return path
