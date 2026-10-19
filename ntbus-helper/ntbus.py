"""南投假期（ntbus.welcometw.com）訂票小幫手：盯「最早前 5 天」的新日期何時開放，開放就通知。

只查詢、不下單：開放後推播＋附上訂票頁連結，「立即購買」由使用者自己按。

資料來源：網站日曆用的 /ajax/tour/queryBooking/summary（不用登入）。
回傳 lastBookingDate（目前可預約的最後一天）與每天每班次的 status / remainQuantity。
新日期大約在 00:00～00:10 之間出現（伺服器刷新時間不固定），所以從 23:58 開始每 10 秒查一次。
要搭哪天由使用者指定（data/targets.json）；只有指定的日期開放那晚才會查網站。

兩種執行環境：
- Mac（launchd）：通知中心＋語音＋Telegram，日期在本機網頁設定。
- GitHub Actions（PokeBO 的 .github/workflows/ntbus.yml）：Mac 關機也能盯。repo 是公開的，所以
  日期放在 Actions 變數 NTBUS_TARGETS（每行「2026-10-27 去 08:00」），log 不印日期與座位，結果只送 Telegram。

指令：
  python3 ntbus.py status              # 目前各路線最後可預約日與目標班次座位
  python3 ntbus.py add 2026-10-27 去 08:00   # 指定要搭的日期（去／回，也可以在網頁上設定）
  python3 ntbus.py remove 2026-10-27 [9Oqr]
  python3 ntbus.py targets             # 列出指定的日期與開放時間
  python3 ntbus.py sync                # 把日期同步到 GitHub 變數（add／remove／網頁儲存時會自動同步）
  python3 ntbus.py watch               # 盯新日期開放（launchd 每晚 23:57 啟動，沒指定的日子不查）
  python3 ntbus.py watch --now         # 立刻開始盯（測試用，不等 23:58）
  python3 ntbus.py serve               # 狀態網頁 http://127.0.0.1:8766/
  python3 ntbus.py notify-test         # 測試通知
  python3 ntbus.py ping [--notify]     # 測試能否連到網站（GitHub 用，不印日期）
  python3 ntbus.py launchd --install   # 產生並安裝排程
"""
import itertools
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
LOG = ROOT / "logs" / "ntbus.log"
OPEN_LOG = DATA / "open_log.jsonl"      # 每次新日期開放的時間與座位（用來看伺服器實際刷新時間）
BASE = "https://ntbus.welcometw.com"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) ntbus-helper"
# python.org 版 Python 在 macOS 沒有根憑證，改用系統內建的
_CA = "/etc/ssl/cert.pem"
SSL_CTX = ssl.create_default_context(cafile=_CA if os.path.exists(_CA) else None)
# 公開 repo 的 Actions log 任何人都看得到：GitHub 上執行時只印不含日期的訊息
GH = os.environ.get("GITHUB_ACTIONS") == "true"


def load_config():
    return json.loads((ROOT / "config.json").read_text(encoding="utf-8"))


def log(msg, public=False):
    if GH:
        if public:
            print(msg, flush=True)
        return
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")
    print(msg, flush=True)


def _env(cfg, key):
    if os.environ.get(key):
        return os.environ[key]
    for p in cfg.get("env_files", [".env.local"]):
        env = (ROOT / p).resolve()
        for line in env.read_text().splitlines() if env.exists() else []:
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip()
    return None


# ── 網站 API ──────────────────────────────────────

def query(route, month=None):
    """查一條路線的日曆；month 為 YYYYMM01，預設本月。回傳 summary 的 result。"""
    q = urllib.parse.urlencode({"productId": route["product_id"], "saleItemId": route["sale_item_id"],
                                "queryDate": month or date.today().strftime("%Y%m01")})
    req = urllib.request.Request(f"{BASE}/ajax/tour/queryBooking/summary?{q}",
                                 headers={"User-Agent": UA, "Accept": "application/json",
                                          "X-Requested-With": "XMLHttpRequest"})
    with urllib.request.urlopen(req, timeout=15, context=SSL_CTX) as r:
        j = json.loads(r.read())
    if not j.get("success"):
        raise RuntimeError(f"API 回傳失敗：{j.get('message')} ({j.get('result')})")
    return j["result"]


def query_through(route, last, res=None):
    """查到 last 那天所在的月份（跨月時本月的 dateList 不含下個月）。"""
    res = res or query(route)
    if last and last[:6] != date.today().strftime("%Y%m"):
        nxt = query(route, last[:6] + "01")
        res = {**nxt, "dateList": res.get("dateList", []) + nxt.get("dateList", [])}
    return res


def session_of(res, day, time_):
    """找某天（YYYYMMDD）某班（HH:MM）的班次；沒有就回傳 None。"""
    for d in res.get("dateList", []):
        if d["date"] == day:
            for s in d.get("settingList", []):
                if s.get("bookingSessionStartTime") == time_:
                    return {"status": s.get("status"), "remain": s.get("remainQuantity"),
                            "name": s.get("bookingSessionName")}
            return {"status": "NO_SESSION", "remain": 0, "name": None}
    return None


def ymd(s):
    """'2026-10-24' 或 '20261024' → '20261024'"""
    return (s or "").replace("-", "")


def purchase_url(route, day=None):
    q = {"quantity": route.get("quantity", 1)}
    if day:
        q["bookingDate"] = day
    return f"{BASE}/tour/{route['sale_item_id']}/purchase/?{urllib.parse.urlencode(q)}"


STATUS_TEXT = {"AVAILABLE": "可預約", "FULL": "額滿", "WAITING": "候補", "INSUFFICIENT": "不足",
               "COMING_SOON": "尚未開放", "NO_SESSION": "當天沒有這班", None: "—"}


def fmt_day(day):
    d = datetime.strptime(day, "%Y%m%d")
    return f"{d:%m/%d}（{'一二三四五六日'[d.weekday()]}）"


def snapshot(cfg):
    """目前各路線狀態（status 指令與網頁共用）。"""
    out = []
    for r in cfg["routes"]:
        try:
            first = query(r)
            last = ymd(first.get("lastBookingDate"))
            res = query_through(r, last, first)
            days, times = [], set()
            for d in res.get("dateList", []):
                sessions = {x["bookingSessionStartTime"]: {"status": x.get("status"), "remain": x.get("remainQuantity")}
                            for x in d.get("settingList", []) if x.get("bookingSessionStartTime")}
                times.update(sessions)
                days.append({"date": d["date"], "label": fmt_day(d["date"]), "sessions": sessions,
                             **(session_of(res, d["date"], r["time"]) or {})})
            out.append({"id": r["sale_item_id"], "name": r["name"], "time": r["time"], "last": last,
                        "nearest": ymd(res.get("nearestBookingDate")), "days": days,
                        "times": sorted(times), "url": purchase_url(r)})
        except Exception as e:  # 網站暫時連不上時，頁面照樣顯示其他路線
            out.append({"id": r["sale_item_id"], "name": r["name"], "time": r["time"], "error": str(e),
                        "times": [r["time"]], "url": purchase_url(r)})
    return out


# ── 通知 ──────────────────────────────────────────

def notify(cfg, title, body, url=None):
    n = cfg.get("notify", {})
    if n.get("macos", True) and not GH:
        sound = n.get("macos_sound", "Glass")
        script = f"display notification {json.dumps(body)} with title {json.dumps(title)} sound name {json.dumps(sound)}"
        subprocess.run(["osascript", "-e", script], capture_output=True)
    if n.get("say") and not GH:
        subprocess.Popen(["say", "-v", "Meijia", title], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if n.get("telegram"):
        token, chat = _env(cfg, "TELEGRAM_BOT_TOKEN"), _env(cfg, "TELEGRAM_CHAT_ID")
        if token and chat:
            text = f"{title}\n{body}" + (f"\n{url}" if url else "")
            payload = urllib.parse.urlencode({"chat_id": chat, "text": text, "disable_web_page_preview": "true"}).encode()
            try:
                urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", payload,
                                       timeout=20, context=SSL_CTX)
            except Exception as e:
                log(f"Telegram 送出失敗：{type(e).__name__}", public=True)
        else:
            log("Telegram 未設定 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID", public=True)
    log(f"notify: {title} | {body}")


# ── 指定日期 ──────────────────────────────────────
# data/targets.json：[{"date": "20261027", "route": "9Oqr", "time": "08:00"}, ...]
# 搭乘日 D 會在 D - advance_days 那天的 00:00 開放（伺服器刷新可能晚幾分鐘）。

TARGETS = DATA / "targets.json"


def load_targets(cfg=None):
    today = date.today().strftime("%Y%m%d")
    if os.environ.get("NTBUS_TARGETS") is not None:
        items = parse_targets_text(cfg or load_config(), os.environ["NTBUS_TARGETS"])
    elif TARGETS.exists():
        items = json.loads(TARGETS.read_text(encoding="utf-8"))
    else:
        items = []
    return [t for t in items if t["date"] >= today]


def parse_targets_text(cfg, text):
    """NTBUS_TARGETS 的格式：每行（或用逗號分隔）「日期 路線 [班次]」，路線可寫 去／回 或 tour 代號。
    例：2026-10-27 去 08:00、1027 回（沒寫年份用今年，已過的日期算明年；沒寫班次用預設班次）"""
    out = []
    for raw in text.replace("，", ",").replace(",", "\n").splitlines():
        parts = raw.split("#")[0].split()
        if not parts:
            continue
        if len(parts) < 2:
            raise ValueError(f"格式錯誤：{raw.strip()}（要寫 日期 路線 [班次]）")
        d = parts[0].replace("-", "").replace("/", "")
        if len(d) == 4:
            d = f"{date.today().year}{d}"
            if d < date.today().strftime("%Y%m%d"):
                d = f"{date.today().year + 1}{d[4:]}"
        r = next((x for x in cfg["routes"] if parts[1] in (x["sale_item_id"], x.get("alias"))), None)
        if not r:
            raise ValueError(f"未知路線：{parts[1]}")
        out.append({"date": d, "route": r["sale_item_id"], "time": parts[2] if len(parts) > 2 else r["time"]})
    for t in out:
        datetime.strptime(t["date"], "%Y%m%d")
        datetime.strptime(t["time"], "%H:%M")
    return out


def targets_text(cfg, items):
    """反過來產生 NTBUS_TARGETS 的內容（網頁上給使用者複製到 GitHub）。"""
    alias = {r["sale_item_id"]: r.get("alias", r["sale_item_id"]) for r in cfg["routes"]}
    return "\n".join(f"{t['date'][:4]}-{t['date'][4:6]}-{t['date'][6:]} {alias[t['route']]} {t['time']}" for t in items)


def save_targets(cfg, items):
    routes = {r["sale_item_id"] for r in cfg["routes"]}
    clean, seen = [], set()
    for t in items:
        d, rid, tm = ymd(str(t.get("date", ""))), t.get("route"), str(t.get("time", ""))
        datetime.strptime(d, "%Y%m%d")      # 格式錯誤會丟 ValueError
        datetime.strptime(tm, "%H:%M")
        if rid not in routes:
            raise ValueError(f"未知路線 {rid}")
        if (d, rid, tm) not in seen:
            seen.add((d, rid, tm))
            clean.append({"date": d, "route": rid, "time": tm})
    clean.sort(key=lambda t: (t["date"], t["route"], t["time"]))
    DATA.mkdir(parents=True, exist_ok=True)
    TARGETS.write_text(json.dumps(clean, ensure_ascii=False, indent=1), encoding="utf-8")
    return clean


def sync_github(cfg, items):
    """把指定日期寫到 GitHub Actions 變數，Mac 關機時 GitHub 版也盯同樣的日期。
    需要 .env.local 的 GITHUB_VARIABLES_TOKEN：fine-grained token，只授權 PokeBO 的 Variables（Read and write）。
    回傳 (成功與否, 訊息)。"""
    g = cfg.get("github") or {}
    token = _env(cfg, "GITHUB_VARIABLES_TOKEN")
    if not g.get("repo") or not token:
        return False, "未設定 GITHUB_VARIABLES_TOKEN，請手動複製到 GitHub"
    name = g.get("variable", "NTBUS_TARGETS")
    # GitHub 變數不能是空字串，沒有日期時放一行註解（parse_targets_text 會忽略）
    value = targets_text(cfg, items) or "# 沒有指定日期"
    base = f"https://api.github.com/repos/{g['repo']}/actions/variables"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28", "User-Agent": UA, "Content-Type": "application/json"}

    def call(method, url):
        body = None if method == "GET" else json.dumps({"name": name, "value": value}).encode()
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=15, context=SSL_CTX) as r:
            return json.loads(r.read() or b"{}")

    try:
        try:
            call("PATCH", f"{base}/{name}")
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
            call("POST", base)          # 第一次：變數還不存在
        # 讀回 GitHub 上的值，用 GitHub 版同一套解析算筆數，確認它讀得到
        saved = parse_targets_text(cfg, call("GET", f"{base}/{name}").get("value", ""))
    except urllib.error.HTTPError as e:
        msg = {401: "token 無效或過期", 403: "token 沒有 Variables 寫入權限"}.get(e.code, f"HTTP {e.code}")
        log(f"同步 GitHub 失敗：{msg}")
        return False, f"同步 GitHub 失敗：{msg}"
    except Exception as e:
        log(f"同步 GitHub 失敗：{e}")
        return False, f"同步 GitHub 失敗：{type(e).__name__}"
    except ValueError as e:
        return False, f"GitHub 上的內容無法解析：{e}"
    if sorted(map(str, saved)) != sorted(map(str, items)):
        log(f"同步 GitHub 後讀回不一致：送出 {len(items)} 筆、讀回 {len(saved)} 筆")
        return False, f"GitHub 讀回 {len(saved)} 筆，和本機 {len(items)} 筆不一致"
    log(f"已同步 GitHub 變數 {name}（{len(items)} 筆）")
    return True, f"已同步到 GitHub，GitHub 確認讀到 {len(saved)} 筆"


def opens_on(cfg, day):
    """搭乘日 day（YYYYMMDD）在哪一天的 00:00 開放。"""
    return datetime.strptime(day, "%Y%m%d").date() - timedelta(days=cfg["watch"]["advance_days"])


def route_by_id(cfg, rid):
    return next(r for r in cfg["routes"] if rid in (r["sale_item_id"], r.get("alias")))


def alternatives(res, day, time_, n=3):
    """同一天還有位子的其他班次，依和指定時間的差距排序。"""
    def minutes(t):
        h, m = t.split(":")
        return int(h) * 60 + int(m)
    out = []
    for d in res.get("dateList", []):
        if d["date"] == day:
            for s in d.get("settingList", []):
                t = s.get("bookingSessionStartTime")
                if t and t != time_ and s.get("status") == "AVAILABLE":
                    out.append((abs(minutes(t) - minutes(time_)), t, s.get("remainQuantity")))
    return [f"{t} 剩 {q}" for _, t, q in sorted(out)[:n]]


def seat_text(res, day, times):
    """指定班次的座位；額滿時附上最接近的其他班次。"""
    lines, best = [], None
    for t in times:
        s = session_of(res, day, t) or {"status": None, "remain": None}
        if s["status"] == "AVAILABLE":
            lines.append(f"✅ {t} 可預約，剩 {s['remain']} 位")
            best = best or s
        else:
            alt = alternatives(res, day, t)
            lines.append(f"❌ {t} {STATUS_TEXT.get(s['status'], s['status'])}"
                         + (f"；其他有位：{'、'.join(alt)}" if alt else "；當天其他班也沒位子"))
    return "\n".join(lines), best


# ── 盯開放 ────────────────────────────────────────

def _next_midnight(now):
    return datetime.combine(now.date() + timedelta(days=1), datetime.min.time())


def watch(cfg, now_mode=False):
    """每晚 23:57 由 launchd 啟動：今晚 00:00 會開放的日期裡有使用者指定的，才去盯。"""
    w = cfg["watch"]
    start = datetime.now()
    midnight = _next_midnight(start) if start.hour >= 12 else datetime.combine(start.date(), datetime.min.time())
    opening = (midnight.date() + timedelta(days=w["advance_days"])).strftime("%Y%m%d")
    wanted = [t for t in load_targets(cfg) if t["date"] == opening]
    if not wanted:
        log(f"{midnight:%m/%d} 00:00 開放的是 {fmt_day(opening)}，沒有指定這天，不查詢")
        log("今晚沒有要盯的日期，結束", public=True)
        return
    log(f"今晚有 {len(wanted)} 個班次要盯", public=True)
    begin = start if now_mode else midnight - timedelta(seconds=w["start_before_sec"])
    stop = (start if now_mode else midnight) + timedelta(minutes=w["stop_after_min"])
    if not now_mode and datetime.now() < begin:
        log(f"等到 {begin:%H:%M:%S} 開始（等待 {fmt_day(opening)} 開放）")
        time.sleep((begin - datetime.now()).total_seconds())

    pending = {}
    for t in wanted:
        r = route_by_id(cfg, t["route"])
        pending.setdefault(r["name"], {"route": r, "target": opening, "times": []})["times"].append(t["time"])
    for name, p in pending.items():
        log(f"{name} {'、'.join(p['times'])}：等待 {fmt_day(opening)} 開放")

    errors = 0
    while pending and datetime.now() < stop:
        for name, p in list(pending.items()):
            r = p["route"]
            try:
                res = query_through(r, p["target"])
                errors = 0
            except Exception as e:
                errors += 1
                log(f"查詢失敗：{type(e).__name__} {e}"[:200], public=True)
                if errors >= 5:  # 連續失敗多半是被擋或網站忙，放慢避免加重負擔
                    time.sleep(30)
                continue
            if ymd(res.get("lastBookingDate")) >= p["target"]:
                opened = datetime.now()
                text, best = seat_text(res, p["target"], p["times"])
                head = "🚌" if best else "⚠️"
                notify(cfg, f"{head} {fmt_day(p['target'])} 開放了：{name}",
                       f"{text}\n（{opened:%H:%M:%S} 偵測）", url=purchase_url(r, p["target"]))
                for tm in p["times"]:
                    _record(name, tm, p["target"], opened, midnight, session_of(res, p["target"], tm) or {})
                del pending[name]
                if w.get("follow_min"):
                    _follow.setdefault(name, p)
        if pending:
            time.sleep(w["interval_sec"])

    for name, p in pending.items():
        notify(cfg, "南投假期小幫手：還沒看到新日期",
               f"{name} 到 {stop:%H:%M} 仍未開放 {fmt_day(p['target'])}，請手動確認", url=purchase_url(p["route"]))
    log("盯開放結束", public=True)
    _follow_seats(cfg)


_follow = {}


def _follow_seats(cfg):
    """開放後再追蹤幾分鐘目標班次的剩餘座位，了解多快被訂完（只記錄，不通知）。"""
    w = cfg["watch"]
    end = datetime.now() + timedelta(minutes=w.get("follow_min", 0))
    while _follow and datetime.now() < end:
        time.sleep(w.get("follow_interval_sec", 60))
        for name, p in _follow.items():
            try:
                res = query_through(p["route"], p["target"])
                for tm in p["times"]:
                    s = session_of(res, p["target"], tm) or {}
                    _append({"type": "follow", "route": name, "time": tm, "target": p["target"],
                             "at": f"{datetime.now():%Y-%m-%d %H:%M:%S}", "status": s.get("status"), "remain": s.get("remain")})
            except Exception as e:
                log(f"{name} 追蹤座位失敗：{e}")
    _follow.clear()


def _append(rec):
    DATA.mkdir(parents=True, exist_ok=True)
    with OPEN_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _record(name, time_, target, opened, midnight, s):
    _append({"type": "open", "route": name, "time": time_, "target": target,
             "at": f"{opened:%Y-%m-%d %H:%M:%S}", "delay_sec": round((opened - midnight).total_seconds()),
             "status": s.get("status"), "remain": s.get("remain")})


def open_history(limit=30):
    if not OPEN_LOG.exists():
        return []
    rows = [json.loads(l) for l in OPEN_LOG.read_text(encoding="utf-8").splitlines() if l.strip()]
    return rows[-limit:]


# ── 狀態網頁 ──────────────────────────────────────

_cache = {"at": 0, "data": None}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/":
            return self._send(200, (ROOT / "web" / "index.html").read_bytes(), "text/html; charset=utf-8")
        if path == "/api/status":
            cfg = load_config()
            # 至少間隔 cache_sec 才重新查網站，多開幾個分頁也不會增加對網站的請求
            if time.time() - _cache["at"] > cfg.get("serve", {}).get("cache_sec", 30):
                _cache.update(at=time.time(), data=snapshot(cfg))
            body = {"routes": _cache["data"], "history": open_history(), "targets": targets_view(cfg),
                    "github_text": targets_text(cfg, load_targets(cfg)),
                    "checked_at": datetime.fromtimestamp(_cache["at"]).strftime("%H:%M:%S"),
                    "advance_days": cfg["watch"]["advance_days"]}
            return self._json(200, body)
        self._send(404, b"not found", "text/plain")

    def _json(self, code, body):
        self._send(code, json.dumps(body, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def do_POST(self):
        # 只接受本機頁面送來的請求，避免其他網站透過瀏覽器改設定
        host = self.headers.get("Host", "")
        origin = self.headers.get("Origin")
        if not host.startswith(("127.0.0.1:", "localhost:")) or (origin and origin != f"http://{host}"):
            return self._json(403, {"error": "forbidden"})
        if urllib.parse.urlparse(self.path).path != "/api/targets":
            return self._send(404, b"not found", "text/plain")
        cfg = load_config()
        try:
            items = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"[]")
            items = save_targets(cfg, items)
        except (ValueError, KeyError, TypeError) as e:
            return self._json(400, {"error": str(e)})
        ok, msg = sync_github(cfg, items)
        self._json(200, {"targets": targets_view(cfg), "github_text": targets_text(cfg, load_targets(cfg)),
                         "github_sync": {"ok": ok, "message": msg}})


def targets_view(cfg):
    """網頁用：每個指定日期的開放時間、狀態與目前座位（座位用快取，不另外查網站）。"""
    routes = {r["id"]: r for r in (_cache["data"] or []) if "id" in r}
    out = []
    for t in load_targets():
        r = routes.get(t["route"], {})
        day = next((d for d in r.get("days", []) if d["date"] == t["date"]), None)
        o = opens_on(cfg, t["date"])
        out.append({**t, "label": fmt_day(t["date"]), "route_name": route_by_id(cfg, t["route"])["name"],
                    "opens": f"{o:%m/%d}（{'一二三四五六日'[o.weekday()]}）00:00",
                    "open": bool(r.get("last") and r["last"] >= t["date"]),
                    "seat": (day or {}).get("sessions", {}).get(t["time"])})
    return out


def serve(cfg):
    port = cfg.get("serve", {}).get("port", 8766)
    log(f"狀態網頁 http://127.0.0.1:{port}/")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


# ── launchd ───────────────────────────────────────

def _plist(label, args, extra):
    py = sys.executable
    import plistlib
    d = {"Label": label, "ProgramArguments": [py, str(ROOT / "ntbus.py"), *args],
         "WorkingDirectory": str(ROOT), "EnvironmentVariables": {"LANG": "zh_TW.UTF-8", "PYTHONUNBUFFERED": "1"},
         "StandardOutPath": str(ROOT / "logs" / f"{args[0]}.out.log"),
         "StandardErrorPath": str(ROOT / "logs" / f"{args[0]}.err.log"), **extra}
    return plistlib.dumps(d)


def launchd(cfg, install=False):
    LABEL = cfg.get("launchd_label", "local.ntbus-helper")
    # 每晚 23:57 啟動：今晚開放的日期沒有被指定就直接結束；有的話睡到 23:58 再開始查
    # （launchd 在 Mac 睡眠時錯過的排程，醒來會補跑）
    jobs = {
        f"{LABEL}.watch": _plist(f"{LABEL}.watch", ["watch"],
                                 {"StartCalendarInterval": [{"Hour": 23, "Minute": 57}]}),
        f"{LABEL}.serve": _plist(f"{LABEL}.serve", ["serve"], {"RunAtLoad": True, "KeepAlive": True}),
    }
    out = ROOT / "launchd"
    out.mkdir(exist_ok=True)
    agents = Path.home() / "Library" / "LaunchAgents"
    uid = os.getuid()
    for label, body in jobs.items():
        (out / f"{label}.plist").write_bytes(body)
        print(f"產生 launchd/{label}.plist")
        if install:
            dst = agents / f"{label}.plist"
            subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"], capture_output=True)
            dst.write_bytes(body)
            r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(dst)], capture_output=True, text=True)
            print(f"  安裝 {label}：{'成功' if r.returncode == 0 else r.stderr.strip()}")


def targets_summary(cfg, items, results):
    """每個指定日期：還沒開放 → 何時開放；已開放 → 目前座位（額滿附其他有位班次）。"""
    if not items:
        return "目前沒有指定日期，不會去盯。"
    lines = []
    key = lambda t: (t["date"], t["route"])  # noqa: E731
    for (day, rid), group in itertools.groupby(sorted(items, key=key), key=key):
        r, times = route_by_id(cfg, rid), [t["time"] for t in group]
        res = results.get(rid)
        lines.append(f"📅 {fmt_day(day)} {r['name']}")
        if res is None:
            lines.append("　查不到網站，稍後再試")
        elif ymd(res.get("lastBookingDate")) >= day:
            text, _ = seat_text(res, day, times)
            lines += ["　" + x for x in text.splitlines()] + [f"　{purchase_url(r, day)}"]
        else:
            o = opens_on(cfg, day)
            lines.append(f"　{'、'.join(times)}：{o:%m/%d}（{'一二三四五六日'[o.weekday()]}）00:00 開放，到時會自動盯並通知")
    return "\n".join(lines)


def _print_targets(cfg, items):
    if not items:
        print("目前沒有指定日期")
    for t in items:
        o = opens_on(cfg, t["date"])
        print(f"{fmt_day(t['date'])} {route_by_id(cfg, t['route'])['name']} {t['time']}　→ {o:%m/%d} 00:00 開放")


def main(argv):
    cfg = load_config()
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd == "status":
        for r in snapshot(cfg):
            if "error" in r:
                print(f"{r['name']} {r['time']}：查詢失敗 {r['error']}")
                continue
            print(f"{r['name']} {r['time']}　最後可預約 {fmt_day(r['last'])}")
            for d in r["days"]:
                print(f"  {d['label']} {STATUS_TEXT.get(d.get('status'), d.get('status'))} 剩 {d.get('remain')}")
    elif cmd == "add":          # add 2026-10-27 去 [08:00]（路線可寫 去／回 或 tour 代號）
        r = route_by_id(cfg, argv[3])
        items = save_targets(cfg, load_targets() + [{"date": argv[2], "route": r["sale_item_id"], "time": argv[4] if len(argv) > 4 else r["time"]}])
        _print_targets(cfg, items)
        print(sync_github(cfg, items)[1])
    elif cmd == "remove":       # remove 2026-10-27 [9Oqr]
        d = ymd(argv[2])
        items = save_targets(cfg, [t for t in load_targets() if not (t["date"] == d and (len(argv) < 4 or t["route"] == argv[3]))])
        _print_targets(cfg, items)
        print(sync_github(cfg, items)[1])
    elif cmd == "sync":         # 手動把目前的日期同步到 GitHub
        print(sync_github(cfg, load_targets(cfg))[1])
    elif cmd == "targets":
        _print_targets(cfg, load_targets())
    elif cmd == "ping":         # GitHub 上測試連線；--notify 把指定日期的狀態送 Telegram（log 不印日期與座位）
        ok, results = 0, {}
        for r in cfg["routes"]:
            try:
                first = query(r)
                results[r["sale_item_id"]] = query_through(r, ymd(first.get("lastBookingDate")), first)
                ok += 1
            except Exception as e:
                print(f"連線失敗：{type(e).__name__} {e}"[:200])
        print(f"網站可連線：{ok}/{len(cfg['routes'])} 條路線；指定日期 {len(load_targets(cfg))} 筆")
        if "--notify" in argv:
            notify(cfg, "🚌 南投假期小幫手：已收到設定", targets_summary(cfg, load_targets(cfg), results))
        if ok < len(cfg["routes"]):
            sys.exit(1)
    elif cmd == "watch":
        watch(cfg, now_mode="--now" in argv)
    elif cmd == "serve":
        serve(cfg)
    elif cmd == "notify-test":
        r = cfg["routes"][0]
        notify(cfg, "🚌 南投假期小幫手測試", f"通知正常：{r['name']} {r['time']}", url=purchase_url(r))
    elif cmd == "launchd":
        launchd(cfg, install="--install" in argv)
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv)
