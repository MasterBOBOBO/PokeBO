"""Telegram 記帳：在手機回 bot「/buy 0050 成交 281 股 均價 106.45」，Mac 每分鐘檢查一次並記帳回覆。

- 只接受本人（.env.local 的 TELEGRAM_CHAT_ID）的訊息；家人傳的指令不處理。
- 不向 Telegram 確認（getUpdates 不帶 offset），改在 data/state/telegram_offset 記住處理到哪一則：
  ntbus-helper 的 `family who` 也要讀最近 24 小時的訊息，確認掉就看不到了。
- Telegram 只保留 24 小時內的訊息：Mac 關機超過一天，期間傳的指令會遺失。
- 回覆和其他通知一樣不放金額，只有股數、價格、百分比。
"""
import json
import re
import urllib.parse
from datetime import date

from . import backup, data, ledger, report

STATE = data.DATA / "state" / "telegram_offset"
TAG = "Telegram 記帳"
HELP = ("記帳指令（只處理本人傳的訊息）：\n"
        "/buy 0050 成交 281 股 均價 106.45\n"
        "/sell 1216 成交 1000 股 均價 81.1\n"
        "可加「手續費 20」「日期 10/27」；沒寫日期時，定額標的用本月扣款成交日，其他用今天。\n"
        "/undo 刪除最後一筆用 Telegram 記的交易")
_NUM = r"(\d+(?:,\d{3})*(?:\.\d+)?)"


def _num(s):
    return float(s.replace(",", ""))


def parse(text, today=None):
    """解析 /buy、/sell 指令，回傳 dict 或錯誤訊息字串。"""
    today = today or date.today()
    m = re.match(r"^/(buy|sell)(?:@\w+)?\s+([A-Za-z0-9]+)\s*(.*)$", text.strip(), re.S)
    if not m:
        return "看不懂這個指令。\n" + HELP
    cmd, symbol, rest = m.group(1), m.group(2).upper(), m.group(3)
    fee = re.search(r"手續費\s*" + _NUM, rest)
    rest_wo_fee = rest.replace(fee.group(0), " ") if fee else rest
    d = re.search(r"(?:日期\s*)?(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2})", rest_wo_fee)
    if d:
        rest_wo_fee = rest_wo_fee.replace(d.group(0), " ")
    shares = re.search(_NUM + r"\s*股", rest_wo_fee)
    price = re.search(r"(?:均價|價格|成交價|@)\s*" + _NUM, rest_wo_fee)
    nums = re.findall(_NUM, rest_wo_fee)
    if not shares and not price and len(nums) == 2:      # 簡寫：/buy 0050 281 106.45
        sh, px = _num(nums[0]), _num(nums[1])
    elif shares and price:
        sh, px = _num(shares.group(1)), _num(price.group(1))
    else:
        return "缺少股數或均價，例：/buy 0050 成交 281 股 均價 106.45"
    if sh <= 0 or px <= 0:
        return "股數和均價要大於 0"
    when = None
    if d:
        s = d.group(1)
        if "-" in s:
            when = s
        else:
            mo, dy = (int(x) for x in s.split("/"))
            try:
                when = date(today.year, mo, dy)
                if when > today and (today - date(today.year - 1, mo, dy)).days <= 60:
                    when = date(today.year - 1, mo, dy)              # 1 月記去年 12 月的交易
                when = when.isoformat()
            except ValueError:
                return f"日期 {s} 不正確"
        if when > today.isoformat():
            return f"日期 {when} 還沒到"
    return {"symbol": symbol, "shares": sh if cmd == "buy" else -sh, "price": px,
            "fee": _num(fee.group(1)) if fee else 0.0, "date": when}


def _default_date(cfg, symbol, txns, today):
    """定額標的：本月還沒記的扣款 → 用扣款成交日；其他用今天。"""
    for x in ledger.missing_dca(cfg, txns, today):
        if x["symbol"] == symbol:
            return x["trade_date"]
    return today.isoformat()


def _summary(cfg):
    r = report.build(cfg)
    s = r["summary"]
    left = sorted({x["symbol"] for x in r["missing"]["dca"]})
    line = f"總損益 {s['pnl_pct']:+.1%}"
    return line + (f"；待記帳：{'、'.join(left)}" if left else "；本月扣款都記好了")


def record(cfg, p, today=None):
    """記一筆交易，回傳回覆文字。"""
    today = today or date.today()
    sym = p["symbol"]
    market = ledger.market_of(sym, cfg)
    if not data.price_path(market, sym).exists():
        return f"找不到 {sym} 的股價資料，請確認代號（新標的請在 Mac 上用 invest.py buy 記帳）"
    txns = ledger.load()
    held = sum(t["shares"] for t in txns if t["symbol"] == sym)
    if p["shares"] < 0 and -p["shares"] > held + 1e-6:
        return f"{sym} 帳上只有 {held:,.4g} 股，不能賣出 {-p['shares']:,.4g} 股"
    when = p["date"] or _default_date(cfg, sym, txns, today)
    txn = ledger.make_txn(cfg, sym, when, p["shares"], p["price"], p["fee"], note=TAG)
    replaced = [t for t in txns if t["symbol"] == sym and t["source"] == "estimated"
                and t["plan_month"] == txn["plan_month"]]
    ledger.save([t for t in txns if t not in replaced] + [txn])
    backup.snapshot(f"telegram {sym} {txn['shares']} @ {txn['price']}")
    side = "買進" if txn["shares"] > 0 else "賣出"
    return (f"✅ 已記帳：{sym} {side} {abs(txn['shares']):,.4g} 股 @ {txn['price']:g}"
            f"（{when}，手續費 {txn['fee']:g}）\n{_summary(cfg)}\n記錯了回 /undo")


def undo(cfg):
    txns = ledger.load()
    mine = [t for t in txns if t.get("note") == TAG]
    if not mine:
        return "沒有可以刪除的 Telegram 記帳紀錄"
    last = mine[-1]
    txns.remove(last)
    ledger.save(txns)
    backup.snapshot(f"telegram undo {last['symbol']}")
    return (f"↩️ 已刪除：{last['symbol']} {last['shares']:+,.4g} 股 @ {last['price']:g}（{last['date']}）\n"
            f"{_summary(cfg)}")


def handle(cfg, text, today=None):
    t = text.strip()
    if re.match(r"^/undo(@\w+)?$", t):
        return undo(cfg)
    if re.match(r"^/(help|start)(@\w+)?$", t):
        return HELP
    if re.match(r"^/(buy|sell)\b", t):
        p = parse(t, today)
        return p if isinstance(p, str) else record(cfg, p, today)
    return None                     # 一般聊天訊息不回


def _api(token, method, params=None):
    url = f"https://api.telegram.org/bot{token}/{method}"
    if params is None:
        return json.loads(data.http_get(url, timeout=20))
    return json.loads(data.http_post(url, urllib.parse.urlencode(params).encode(), timeout=20))


def poll(cfg, env):
    """檢查新訊息並處理；回傳處理的指令數。env(key) 讀 .env.local。"""
    token, owner = env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID")
    if not (token and owner):
        return 0
    last = int(STATE.read_text()) if STATE.exists() else None
    ups = _api(token, "getUpdates").get("result", [])
    if last is None:                # 第一次執行：舊訊息不補處理，避免重複記帳
        last = max((u["update_id"] for u in ups), default=0)
    done = 0
    for u in sorted(ups, key=lambda u: u["update_id"]):
        if u["update_id"] <= last:
            continue
        last = u["update_id"]
        msg = u.get("message") or {}
        if str((msg.get("chat") or {}).get("id")) != str(owner) or not msg.get("text"):
            continue
        try:
            reply = handle(cfg, msg["text"])
        except Exception as e:      # 記帳失敗也要回覆，不要讓使用者以為記好了
            reply = f"❌ 記帳失敗：{type(e).__name__}: {e}"[:300]
        if reply:
            _api(token, "sendMessage", {"chat_id": owner, "text": reply,
                                        "reply_to_message_id": msg["message_id"]})
            done += 1
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(str(last))     # 每則處理完就存，當掉重跑也不會重複記帳
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(str(last))
    return done
