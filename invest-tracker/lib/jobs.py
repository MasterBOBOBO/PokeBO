"""排程工作（由 launchd 呼叫）：每日更新 + 健檢變化通知；每週摘要；每月產出月報。

通知管道：
- macOS 通知中心（預設，免設定）
- Telegram（選用）：.env.local 加上 TELEGRAM_BOT_TOKEN、TELEGRAM_CHAT_ID，config.json > notify.telegram = true
預設不在通知裡放金額（notify.include_amounts = false），只放百分比與狀態。
"""
import json
import subprocess
import traceback
import urllib.parse
import urllib.request
from datetime import date, datetime

from . import backup, data, html_report, report

STATE = data.DATA / "state" / "last_health.json"
LOG = data.ROOT / "logs" / "jobs.log"


def log(msg):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")
    print(msg)


def _env(key):
    env = data.ROOT / ".env.local"
    for line in env.read_text().splitlines() if env.exists() else []:
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip()
    return None


def notify(cfg, title, body, telegram_text=None):
    """telegram_text：要送到 Telegram 的完整內文（預設和通知中心相同）。"""
    n = cfg.get("notify", {})
    if n.get("macos", True):
        script = f"display notification {json.dumps(body)} with title {json.dumps(title)}"
        subprocess.run(["osascript", "-e", script], capture_output=True)
    if n.get("telegram"):
        token, chat = _env("TELEGRAM_BOT_TOKEN"), _env("TELEGRAM_CHAT_ID")
        if token and chat:
            payload = urllib.parse.urlencode({"chat_id": chat, "text": f"{title}\n{telegram_text or body}"}).encode()
            urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", payload,
                                   timeout=20, context=data.SSL_CTX)
    log(f"notify: {title} | {body}")


def _update(cfg):
    start = cfg["history_start"]
    from . import ledger
    for mk, sym in ledger.tracked_symbols(cfg):
        data.update_prices(mk, sym, start)
        if mk == "TW":
            data.update_tw_dividends(sym, start)
            data.update_tw_dividend_announce(sym)
    data.update_fx(start)
    from . import market
    for err in market.update(cfg):
        log(f"market update failed: {err}")
    # 預先抓取持有美股的 FINRA / SEC 資料，打開儀表板時不用等
    from . import us_chips
    for mk, sym in ledger.tracked_symbols(cfg):
        if mk == "US":
            try:
                us_chips.analyze(sym, [d for d, _ in data.load_series(data.price_path(mk, sym), "close")])
            except Exception as e:
                log(f"us_chips {sym} failed: {e}")


def _headline(cfg, r):
    s = r["summary"]
    pnl = f"總損益 {s['pnl_pct']:+.1%}"
    if cfg.get("notify", {}).get("include_amounts"):
        pnl = f"市值 NT${s['value_twd']:,.0f}，{pnl}"
    risks = [h["item"] for h in r["health"] if h["status"][:1] in ("⚠", "❌")]
    return pnl, risks


def daily(cfg):
    """收盤後：更新行情 → 比對 Health Check 狀態，有變化才通知 → 備份帳本。"""
    _update(cfg)
    r = report.build(cfg)
    now = {h["item"]: h["status"] for h in r["health"]}
    prev = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    # 只比較前後都有的項目（項目更名或新增時不誤報）
    changed = [(k, prev[k], v) for k, v in now.items() if k in prev and prev[k] != v]
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(now, ensure_ascii=False, indent=2), encoding="utf-8")
    pnl, risks = _headline(cfg, r)
    log(f"daily ok as_of={r['as_of']} {pnl} risks={risks}")
    if changed:
        body = "；".join(f"{k}：{a} → {b}" for k, a, b in changed)
        notify(cfg, "投資組合健檢有變化", body)
    remind_missing(cfg, r)
    q = data.quota(max_age=0)
    if q and q["pct"] and q["pct"] >= 0.8:
        notify(cfg, "FinMind API 用量偏高", f"本小時已用 {q['used']}/{q['limit']} 次（{q['pct']:.0%}）")
    log(f"quota {q['used']}/{q['limit']}" if q else "quota n/a")
    backup.snapshot("daily")


REMINDED = data.DATA / "state" / "reminded.json"


def remind_missing(cfg, r):
    """有新的漏記（定額扣款 / 股息再投資）就提醒一次；之後每 7 天再提醒，直到補記。"""
    sent = json.loads(REMINDED.read_text(encoding="utf-8")) if REMINDED.exists() else {}
    today = date.today()
    due = []
    for x in r["missing"]["dca"]:
        key = f"dca:{x['symbol']}:{x['plan_month']}"
        unit = "NT$" if x["currency"] == "TWD" else "US$"
        due.append((key, f"{x['symbol']} {x['plan_month']} 定額扣款（{x['trade_date']}，{unit}{x['amount']:,}）"))
    for x in r["missing"]["drip"]:
        due.append((f"drip:{x['symbol']}:{x['ex_date']}", f"{x['symbol']} {x['ex_date']} 除息的股息再投資"))
    todo = [(k, msg) for k, msg in due
            if k not in sent or (today - date.fromisoformat(sent[k])).days >= 7]
    if todo:
        notify(cfg, "請記錄成交", "、".join(m for _, m in todo) + "。用 /buy 補記，例：/buy 0050 成交 265 股 均價 113.2")
        for k, _ in todo:
            sent[k] = today.isoformat()
    # 已補記的項目從提醒紀錄移除
    open_keys = {k for k, _ in due}
    sent = {k: v for k, v in sent.items() if k in open_keys}
    REMINDED.parent.mkdir(parents=True, exist_ok=True)
    REMINDED.write_text(json.dumps(sent, ensure_ascii=False, indent=2), encoding="utf-8")


def monthly(cfg):
    """每月 28 日（台股 27 日扣款的 T+1）：更新 → 產出月報 → 通知摘要。"""
    _update(cfg)
    r = report.build(cfg)
    path = html_report.write(r, cfg)
    pnl, risks = _headline(cfg, r)
    div = sum(d["net_twd"] for d in r["dividend_outlook"])
    body = f"{pnl}；風險 {len(risks)} 項" + (f"（{'、'.join(risks)}）" if risks else "")
    if cfg.get("notify", {}).get("include_amounts"):
        body += f"；預估年股利 NT${div:,.0f}"
    notify(cfg, f"{r['as_of'][:7]} 月報已產出", body)
    log(f"monthly ok {path}")
    backup.snapshot("monthly report")
    return path


def weekly(cfg):
    """每週五收盤後：更新 → 產生每週摘要 → 本機存完整版，通知只送不含金額的版本（除非 include_amounts）。"""
    from . import home, weekly as wk
    _update(cfg)
    h = home.build(cfg)
    _, full, _ = wk.build(cfg, h, include_amounts=True)
    path = wk.save(full)
    title, text, short = wk.build(cfg, h)
    notify(cfg, title, short, telegram_text=text)
    log(f"weekly ok {path}")
    return path


def news_job(cfg):
    """每晚：抓重大訊息與持股新聞；有新的持股重大訊息才推播（同一則只推一次）。"""
    from . import news
    r = report.build(cfg)
    items, errors = news.daily_update(cfg, r["holdings"])
    for err in errors:
        log(f"news: {err}")
    if items:
        notify(cfg, f"持股重大訊息 {date.today().month}/{date.today().day}",
               f"{len(items)} 則：" + "、".join(sorted({x['symbol'] for x in items})), telegram_text=news.push_text(items))
        news.mark_sent(items)
    log(f"news ok new={len(items)}")
    return items


def run(name):
    cfg = data.load_config()
    try:
        return {"daily": daily, "monthly": monthly, "weekly": weekly, "news": news_job}[name](cfg)
    except Exception as e:
        log(f"{name} FAILED: {e}\n{traceback.format_exc()}")
        notify(cfg, f"投資追蹤排程失敗（{name}）", f"{type(e).__name__}: {e}"[:200])
        raise
