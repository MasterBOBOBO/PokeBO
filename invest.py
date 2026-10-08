#!/usr/bin/env python3
"""長期資產配置追蹤器。用法見 CLAUDE.md。"""
import argparse
import json
import sys

from lib import data, html_report, ledger, report, scenario


def cmd_update(cfg, args):
    start = cfg["history_start"]
    for mk, sym in ledger.tracked_symbols(cfg):
        n = data.update_prices(mk, sym, start)
        print(f"{mk} {sym}: +{n} 筆")
        if mk == "TW":
            data.update_tw_dividends(sym, start)
            data.update_tw_dividend_announce(sym)
    print(f"USD/TWD: +{data.update_fx(start)} 筆")
    from lib import market
    errors = market.update(cfg)
    print("市場溫度計：" + ("；".join(errors) if errors else "已更新"))


def cmd_buy(cfg, args):
    txns = ledger.load()
    txn = ledger.make_txn(cfg, args.symbol, args.date, args.shares, args.price,
                          args.fee, source=args.source, note=args.note or "",
                          month=args.month)
    # 同月份同標的的估算交易，以實際成交取代
    replaced = [t for t in txns if t["symbol"] == txn["symbol"] and t["source"] == "estimated"
                and t["plan_month"] == txn["plan_month"]]
    txns = [t for t in txns if t not in replaced] + [txn]
    ledger.save(txns)
    print(json.dumps(txn, ensure_ascii=False))
    if replaced:
        print(f"已取代 {len(replaced)} 筆同月估算交易")
    from lib import backup
    backup.snapshot(f"buy {txn['symbol']} {txn['shares']} @ {txn['price']}")


def cmd_backfill(cfg, args):
    added = ledger.backfill(cfg, args.start, args.market)
    for t in added:
        print(f"{t['date']} {t['symbol']:<5} {t['shares']:>10} @ {t['price']}")
    print(f"新增 {len(added)} 筆估算交易")


def cmd_report(cfg, args):
    r = report.build(cfg)
    if not r["holdings"]:
        sys.exit("尚無持倉，請先 buy 或 backfill")
    print(json.dumps(r, ensure_ascii=False, indent=2) if args.json else report.render(r))


def cmd_html(cfg, args):
    r = report.build(cfg)
    if not r["holdings"]:
        sys.exit("尚無持倉，請先 buy 或 backfill")
    print(html_report.write(r, cfg))


def cmd_simulate(cfg, args):
    sc = scenario.load(args.file)
    before, after, cash = scenario.compare(cfg, sc)
    print(scenario.render(before, after, cash, sc))


def cmd_serve(cfg, args):
    from lib import server
    server.serve(args.port, lan=args.lan or data.load_config().get("dashboard_lan", False))


def cmd_ta(cfg, args):
    from lib import server, ta
    r = ta.analyze(args.symbol, refresh=args.refresh)
    out = data.ROOT / "reports" / "ta" / f"{r['symbol']}_{r['quote']['date']}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(server.embed(r), encoding="utf-8")
    pass_n = sum(c["pass"] for c in r["checklist"])
    print(f"{r['symbol']} {r['name']}  收盤 {r['quote']['close']}（{r['quote']['change_pct']:+.2%}）  "
          f"{r['trend']}  檢核 {pass_n}/{len(r['checklist'])}")
    for w in r["warnings"]:
        print(f"  [{w['level']}] {w['title']}：{w['detail']}")
    print(out)


def cmd_backup(cfg, args):
    from lib import backup
    print(backup.snapshot(args.reason) or "沒有變動，不需備份")


def cmd_job(cfg, args):
    from lib import jobs
    jobs.run(args.name)


def cmd_reconcile(cfg, args):
    from lib import backup, reconcile
    broker = reconcile.read_broker(args.file)
    items = reconcile.compare(cfg, broker)
    if args.apply_cost:
        fixed = reconcile.apply_cost(cfg, items)
        print(f"已校正成本：{'、'.join(fixed) or '無'}\n")
    print(reconcile.render(items))
    reconcile.save_state(items, args.file)
    backup.snapshot(f"reconcile {args.file}")


def cmd_quota(cfg, args):
    q, loc = data.quota(max_age=0), data.local_usage()
    if q:
        bar = "█" * round(q["pct"] * 30) + "░" * (30 - round(q["pct"] * 30))
        print(f"FinMind 本小時已用：{q['used']} / {q['limit']} 次（{q['pct']:.0%}）{bar}  方案：{q['level']}")
        print(f"剩餘：{q['limit'] - q['used']} 次（官方數字可能延遲數分鐘）")
    else:
        print("無法取得官方用量")
    print(f"本機近 60 分鐘呼叫：{loc['total']} 次")
    for ds, n in loc["by_dataset"].items():
        print(f"  {ds:<45}{n:>5}")


def cmd_launchd(cfg, args):
    """依本機路徑產生 macOS launchd 排程（每日更新、每月月報、常駐儀表板），--install 直接安裝。"""
    import os
    import plistlib
    import subprocess
    py, root = sys.executable, str(data.ROOT)
    prefix = cfg.get("launchd_prefix", "com.invest-tracker")
    jobs = {
        "daily": {"ProgramArguments": [py, f"{root}/invest.py", "job", "daily"],
                  "StartCalendarInterval": [{"Weekday": w, "Hour": 15, "Minute": 10} for w in range(1, 6)]},
        "weekly": {"ProgramArguments": [py, f"{root}/invest.py", "job", "weekly"],
                   "StartCalendarInterval": [{"Weekday": 5, "Hour": 15, "Minute": 40}]},
        "monthly": {"ProgramArguments": [py, f"{root}/invest.py", "job", "monthly"],
                    "StartCalendarInterval": [{"Day": 28, "Hour": 15, "Minute": 30}]},
        "dashboard": {"ProgramArguments": [py, f"{root}/invest.py", "serve"], "RunAtLoad": True, "KeepAlive": True},
    }
    out = data.ROOT / "launchd"
    out.mkdir(exist_ok=True)
    (data.ROOT / "logs").mkdir(exist_ok=True)
    uid = os.getuid()
    for name, j in jobs.items():
        label = f"{prefix}.{name}"
        j.update({"Label": label, "WorkingDirectory": root,
                  "StandardOutPath": f"{root}/logs/{name}.out.log", "StandardErrorPath": f"{root}/logs/{name}.err.log",
                  "EnvironmentVariables": {"PYTHONUNBUFFERED": "1", "LANG": "zh_TW.UTF-8"}})
        path = out / f"{label}.plist"
        with path.open("wb") as f:
            plistlib.dump(j, f)
        print(f"產生 {path}")
        if args.install:
            dest = os.path.expanduser(f"~/Library/LaunchAgents/{label}.plist")
            subprocess.run(["cp", str(path), dest], check=True)
            subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"], capture_output=True)
            subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", dest], check=True)
            print(f"  已安裝並啟用 {label}")
    if not args.install:
        print("加上 --install 會複製到 ~/Library/LaunchAgents 並啟用")


def cmd_weekly(cfg, args):
    from lib import weekly
    title, text, _ = weekly.build(cfg, include_amounts=args.amounts)
    print(title)
    print(text)


def cmd_telegram_test(cfg, args):
    """.env.local 有 TELEGRAM_BOT_TOKEN 但沒有 CHAT_ID 時，從 getUpdates 找出你傳給 bot 的對話 id 並寫入。"""
    import urllib.parse
    from lib import jobs
    token = jobs._env("TELEGRAM_BOT_TOKEN")
    if not token:
        sys.exit("請先在 .env.local 加上 TELEGRAM_BOT_TOKEN=...（向 @BotFather 建立 bot 取得）")
    api = f"https://api.telegram.org/bot{token}"
    chat = jobs._env("TELEGRAM_CHAT_ID")
    if not chat:
        ups = json.loads(data.http_get(f"{api}/getUpdates"))["result"]
        chats = [u["message"]["chat"]["id"] for u in ups if "message" in u]
        if not chats:
            sys.exit("找不到對話：請先在 Telegram 打開你的 bot，傳一則任意訊息（例如 /start），再執行一次")
        chat = str(chats[-1])
        env = data.ROOT / ".env.local"
        with env.open("a") as f:
            f.write(f"TELEGRAM_CHAT_ID={chat}\n")
        env.chmod(0o600)
        print("已寫入 TELEGRAM_CHAT_ID 到 .env.local")
    body = urllib.parse.urlencode({"chat_id": chat, "text": "invest-tracker 測試訊息：Telegram 設定完成"}).encode()
    data.http_post(f"{api}/sendMessage", body)
    print("已送出測試訊息。要開始收每週摘要，請在 private/config.json 設定 notify.telegram = true")


def cmd_publish(cfg, args):
    """把公開 repo 的最新提交同步到 GitHub 上的子資料夾。推送前會先做個人資料掃描，發現可疑內容就中止。"""
    import re
    import subprocess
    pub = cfg.get("publish", {})
    target, prefix = pub.get("repo_dir"), pub.get("prefix", "invest-tracker")
    if not target:
        sys.exit("請在 private/config.json 設定 publish.repo_dir（GitHub repo 的本機路徑）")
    git = lambda *a, cwd=data.ROOT: subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True)
    if git("status", "--porcelain").stdout.strip():
        sys.exit("公開 repo 還有未提交的變更，請先 commit")
    # 個人資料掃描：private/config.json 裡的 email、.env.local 裡的所有金鑰、以及自訂關鍵字
    patterns = list(pub.get("forbidden", []))
    patterns += [w for w in cfg.get("sec_user_agent", "").split() if "@" in w]
    env = data.ROOT / ".env.local"
    for line in env.read_text().splitlines() if env.exists() else []:
        if "=" in line and len(line.split("=", 1)[1].strip()) >= 12:
            patterns.append(line.split("=", 1)[1].strip())
    hits = []
    for f in git("ls-files").stdout.split():
        try:
            text = (data.ROOT / f).read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError):
            continue
        hits += [f"{f}：{p[:6]}…" for p in patterns if p and p in text]
    hits += [f"{f}：屬於個人資料路徑" for f in git("ls-files").stdout.split()
             if re.match(r"(private/|CLAUDE\.local\.md|\.env|data/|reports/)", f)]
    if hits:
        print("⚠️ 發現疑似個人資料，已中止推送：")
        print("\n".join("  " + h for h in hits))
        sys.exit(1)
    print(f"✅ 個人資料掃描通過（{len(patterns)} 個關鍵字）")
    r = git("subtree", "pull", "-q", "--prefix", prefix, str(data.ROOT), "main", "-m",
            f"更新 {prefix}", cwd=target)
    if r.returncode:
        sys.exit(r.stderr.strip())
    if args.dry_run:
        print("（dry-run：已合併到本機的 GitHub repo，尚未推送）")
        return
    r = git("push", "origin", "main", cwd=target)
    print(r.stderr.strip() or "已推送")
    if r.returncode:
        sys.exit(1)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("update", help="增量更新股價、配息、匯率")
    b = sub.add_parser("buy", help="記錄一筆實際成交")
    b.add_argument("symbol")
    b.add_argument("shares", type=float, help="股數（賣出用負數）")
    b.add_argument("price", type=float)
    b.add_argument("--date", required=True)
    b.add_argument("--fee", type=float, default=0.0)
    b.add_argument("--note")
    b.add_argument("--source", choices=["actual", "opening", "drip"], default="actual",
                   help="opening = 期初部位（price 填券商平均成本）；drip = 股息再投資買進")
    b.add_argument("--month", help="扣款所屬月份 YYYY-MM（預設依成交日推算）")
    f = sub.add_parser("backfill", help="依定期定額規則估算歷史交易")
    f.add_argument("start", help="起始月份 YYYY-MM")
    f.add_argument("--market", choices=["TW", "US"])
    r = sub.add_parser("report", help="輸出績效報告")
    r.add_argument("--json", action="store_true")
    sub.add_parser("html", help="產出月報 HTML 到 reports/YYYY-MM.html")
    m = sub.add_parser("simulate", help="模擬調整方案後的 Health Check（不寫入帳本）")
    m.add_argument("file", help="scenarios/*.json")
    v = sub.add_parser("serve", help="啟動本機技術分析儀表板")
    v.add_argument("--port", type=int, default=8765)
    v.add_argument("--lan", action="store_true", help="開放同一個 Wi-Fi 的手機瀏覽（需要通行碼）")
    t = sub.add_parser("ta", help="產出個股技術分析離線報告")
    t.add_argument("symbol")
    t.add_argument("--refresh", action="store_true")
    k = sub.add_parser("backup", help="把帳本與設定的變動 commit 到本機 git")
    k.add_argument("--reason", default="manual")
    j = sub.add_parser("job", help="排程工作：daily（更新 + 健檢通知）/ monthly（月報）")
    j.add_argument("name", choices=["daily", "weekly", "monthly"])
    w = sub.add_parser("weekly", help="預覽每週摘要（不送出）")
    w.add_argument("--amounts", action="store_true", help="預覽含金額的版本")
    sub.add_parser("telegram-test", help="設定 Telegram：自動找出 chat id 並送一則測試訊息")
    c = sub.add_parser("reconcile", help="和券商庫存對帳（data/reconcile/*.csv）")
    c.add_argument("file")
    c.add_argument("--apply-cost", action="store_true", help="股數一致時，以券商成本校正期初均價")
    sub.add_parser("quota", help="查詢 FinMind API 本小時已用次數")
    pb = sub.add_parser("publish", help="個人資料掃描後，把程式碼同步到 GitHub repo 的子資料夾")
    pb.add_argument("--dry-run", action="store_true")
    l = sub.add_parser("launchd", help="產生（並安裝）macOS 排程：每日更新、每月月報、常駐儀表板")
    l.add_argument("--install", action="store_true")
    args = p.parse_args()
    cfg = data.load_config()
    {"update": cmd_update, "buy": cmd_buy, "backfill": cmd_backfill,
     "report": cmd_report, "html": cmd_html, "simulate": cmd_simulate,
     "serve": cmd_serve, "ta": cmd_ta, "backup": cmd_backup, "job": cmd_job,
     "reconcile": cmd_reconcile, "quota": cmd_quota, "launchd": cmd_launchd,
     "publish": cmd_publish, "weekly": cmd_weekly, "telegram-test": cmd_telegram_test}[args.cmd](cfg, args)


if __name__ == "__main__":
    main()
