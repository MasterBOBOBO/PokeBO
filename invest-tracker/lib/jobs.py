"""排程工作（由 launchd 呼叫）：每日更新 + 健檢變化通知；每晚新聞；每週摘要；每月產出月報；開機補跑。

排程時間集中在 SCHEDULE（invest.py launchd 產生設定檔、catchup 判斷漏跑都讀這裡）。
Mac 睡眠時錯過的排程，launchd 會在醒來後自動補跑；關機時錯過的，由登入時執行的 catchup 補跑。

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
import socket
import time
from datetime import date, datetime, timedelta

from . import backup, data, html_report, report

STATE = data.DATA / "state" / "last_health.json"
RUNS = data.DATA / "state" / "job_runs.json"
# launchd StartCalendarInterval 格式（Weekday：0/7 = 週日、1 = 週一 … 6 = 週六）。
# 全部排在晚上：法人、融資、重大訊息當天都已公布；錯開幾分鐘，避免同時更新同一批資料檔。
# 週報排在週六早上，台股、美股週五的收盤都已經有資料。
SCHEDULE = {
    "daily": [{"Weekday": w, "Hour": 22, "Minute": 0} for w in range(1, 6)],
    "news": [{"Hour": 22, "Minute": 5}],
    "monthly": [{"Day": 28, "Hour": 22, "Minute": 15}],
    "weekly": [{"Weekday": 6, "Hour": 9, "Minute": 0}],
}
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
    """每天晚上：更新行情 → 比對 Health Check 狀態，有變化才通知 → 備份帳本。"""
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
    """每週六早上：更新 → 產生每週摘要 → 本機存完整版，通知只送不含金額的版本（除非 include_amounts）。"""
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


def last_due(specs, now):
    """最近一次「應該執行」的時間（往回找 40 天）。"""
    best = None
    for back in range(40):
        d = (now - timedelta(days=back)).date()
        for sp in specs:
            if "Weekday" in sp and (d.weekday() + 1) % 7 != sp["Weekday"] % 7:
                continue
            if "Day" in sp and d.day != sp["Day"]:
                continue
            t = datetime(d.year, d.month, d.day, sp.get("Hour", 0), sp.get("Minute", 0))
            if t <= now and (best is None or t > best):
                best = t
        if best:
            return best
    return None


def _runs():
    return json.loads(RUNS.read_text(encoding="utf-8")) if RUNS.exists() else {}


def _mark(name, when=None):
    runs = _runs()
    runs[name] = (when or datetime.now()).isoformat(timespec="seconds")
    RUNS.parent.mkdir(parents=True, exist_ok=True)
    RUNS.write_text(json.dumps(runs, indent=2), encoding="utf-8")


def missed(now=None, runs=None):
    """回傳漏跑的排程名稱（依 SCHEDULE 順序）。從沒執行過的不算漏跑，避免第一次安裝就把所有通知送一遍。"""
    now = now or datetime.now()
    runs = _runs() if runs is None else runs
    out = []
    for name, specs in SCHEDULE.items():
        due = last_due(specs, now)
        last = runs.get(name)
        if due and last and datetime.fromisoformat(last) < due:
            out.append(name)
    return out


def _wait_network(host="api.finmindtrade.com", tries=20, gap=15):
    """開機登入時網路可能還沒好，最多等 5 分鐘。"""
    for _ in range(tries):
        try:
            socket.create_connection((host, 443), timeout=5).close()
            return True
        except OSError:
            time.sleep(gap)
    return False


def catchup(cfg):
    """登入（開機）時執行：補跑關機期間錯過的排程。重大訊息 API 只給最近一天，隔天早上補跑還能撈回前一天的公告。"""
    runs = _runs()
    for name in SCHEDULE:            # 第一次使用：把現在當作已執行，之後才開始判斷漏跑
        runs.setdefault(name, datetime.now().isoformat(timespec="seconds"))
    RUNS.parent.mkdir(parents=True, exist_ok=True)
    RUNS.write_text(json.dumps(runs, indent=2), encoding="utf-8")
    todo = missed(runs=runs)
    if not todo:
        log("catchup: 沒有漏跑的排程")
        return []
    if not _wait_network():
        log("catchup: 網路不通，下次登入再補")
        return []
    log(f"catchup: 補跑 {todo}")
    for name in todo:
        try:
            run(name)
        except Exception:
            pass                      # run() 已記錄並通知，繼續補下一個
    return todo


def run(name):
    cfg = data.load_config()
    jobs = {"daily": daily, "monthly": monthly, "weekly": weekly, "news": news_job, "catchup": catchup}
    try:
        result = jobs[name](cfg)
        if name != "catchup":
            _mark(name)
        return result
    except Exception as e:
        log(f"{name} FAILED: {e}\n{traceback.format_exc()}")
        notify(cfg, f"投資追蹤排程失敗（{name}）", f"{type(e).__name__}: {e}"[:200])
        raise
