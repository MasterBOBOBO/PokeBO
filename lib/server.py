"""技術分析伺服器：預設只綁 127.0.0.1；FinMind token 留在伺服器端，不進瀏覽器。

區域網路模式（serve --lan）讓手機在同一個 Wi-Fi 下瀏覽，所有非本機的請求都必須帶通行碼
（.env.local 的 DASHBOARD_KEY，第一次用 ?key=... 進入後改存 HttpOnly cookie）。

路由：
  /                  我的組合（長期持有者首頁）；帶 ?id= 會轉到 /ta
  /ta                個股技術分析儀表板
  /api/home          首頁資料 JSON
  /api/news?id=      個股新聞與重大訊息
  /api/symbols       搜尋框用的股票清單（台股上市櫃＋美股）
  /api/stock?id=     個股分析 JSON（同日快取；refresh=1 強制重抓）
  /api/holdings      快速選擇用的持股清單
  /api/quota         FinMind 本小時用量（官方 + 本機紀錄）
  /report?id=        內嵌資料的離線報告 HTML
"""
import hmac
import json
import re
import secrets
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import data, ledger, ta

PAGE = data.ROOT / "web" / "dashboard.html"
HOME = data.ROOT / "web" / "home.html"
SYMBOL = re.compile(r"^[0-9A-Z]{1,10}$")
REPORTS = data.ROOT / "reports"
# 網站圖示（web/ 底下）：ta- 開頭是技術分析頁的放大鏡，其他頁面用金色箭頭；/favicon.ico 給沒讀 <link rel="icon"> 的瀏覽器
ICONS = {f"{p}{n}" for p in ("", "ta-") for n in ("favicon-16.png", "favicon-32.png", "icon-192.png", "apple-touch-icon.png")}


def dashboard_key():
    """區域網路模式的通行碼；第一次使用時隨機產生並寫入 .env.local。"""
    env = data.ROOT / ".env.local"
    for line in env.read_text().splitlines() if env.exists() else []:
        if line.startswith("DASHBOARD_KEY="):
            return line.split("=", 1)[1].strip()
    key = secrets.token_urlsafe(18)
    with env.open("a") as f:
        f.write(f"DASHBOARD_KEY={key}\n")
    env.chmod(0o600)
    return key


def embed(payload):
    """把分析結果嵌入頁面，產生不需伺服器的離線報告。"""
    blob = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    return PAGE.read_text(encoding="utf-8").replace(
        "window.__DATA__ = null; /*__EMBED__*/", f"window.__DATA__ = {blob};")


def holdings():
    cfg = data.load_config()
    dca = {s for p in cfg["plans"].values() for s in p["amounts"]}
    out = []
    for mk, sym in ledger.tracked_symbols(cfg):
        info = ta.stock_info(sym) if mk == "TW" else None
        name = info["stock_name"] if info else cfg.get("names", {}).get(sym, "")
        out.append({"symbol": sym, "name": name, "dca": sym in dca})
    return out


class Handler(BaseHTTPRequestHandler):
    lan_key = None          # 區域網路模式時設定；None = 只開放本機
    symbols_cache = ("", [])  # 搜尋框股票清單，同一天只整理一次

    def _authorized(self, q):
        if self.client_address[0] in ("127.0.0.1", "::1") or not self.lan_key:
            return True
        cookie = dict(c.strip().split("=", 1) for c in (self.headers.get("Cookie") or "").split(";") if "=" in c)
        given = q.get("key") or cookie.get("it_key", "")
        return hmac.compare_digest(given.encode(), self.lan_key.encode())

    def _send(self, code, body, ctype):
        raw = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def do_GET(self):
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        sym = q.get("id", "").strip().upper()
        if not self._authorized(q):
            return self._send(403, "<h1>需要通行碼</h1><p>請用電腦上顯示的完整網址開啟（含 ?key=...）。</p>",
                              "text/html; charset=utf-8")
        if "key" in q:      # 第一次帶通行碼進來：存成 cookie，再把網址上的 key 拿掉
            rest = "&".join(f"{k}={v}" for k, v in q.items() if k != "key")
            self.send_response(302)
            self.send_header("Set-Cookie", f"it_key={q['key']}; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000")
            self.send_header("Location", url.path + (f"?{rest}" if rest else ""))
            self.end_headers()
            return
        try:
            name = "favicon-32.png" if url.path == "/favicon.ico" else url.path.lstrip("/")
            if name in ICONS:
                return self._send(200, (data.ROOT / "web" / name).read_bytes(), "image/png")
            if url.path == "/reports/":
                files = sorted((p for p in REPORTS.glob("*.html")), reverse=True) if REPORTS.exists() else []
                links = "".join(f'<li><a href="/reports/{p.name}">{p.stem} 月報</a></li>' for p in files) or "<li>尚無月報</li>"
                wk = sorted((REPORTS / "weekly").glob("*.md"), reverse=True)[:12] if (REPORTS / "weekly").exists() else []
                links += "".join(f'<li><a href="/reports/weekly/{p.name}">{p.stem} 每週摘要</a></li>' for p in wk)
                return self._send(200, f"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<link rel="icon" type="image/png" href="/favicon-32.png"><title>報告</title><style>body{{margin:0;font:14px/1.6 system-ui,-apple-system,"PingFang TC",sans-serif;background:#0b0e16;color:#e8eaf0}}
main{{max-width:720px;margin:0 auto;padding:16px}}
.top,.card{{background:#141927;border:1px solid rgba(255,255,255,.08);border-radius:12px;padding:12px 16px;margin-bottom:12px}}
.top{{display:flex;flex-wrap:wrap;gap:8px;align-items:center}}.top b{{margin-right:auto;font-size:16px}}
.top a{{color:#e8eaf0;text-decoration:none;border:1px solid #2f3850;background:#1a2033;border-radius:8px;padding:6px 12px}}
ul{{list-style:none;margin:0;padding:0}}li{{border-bottom:1px solid #232b3d}}li:last-child{{border-bottom:none}}
li a{{display:block;padding:10px 4px;color:#5b8def;text-decoration:none}}</style>
<main><nav class="top"><b>月報與每週摘要</b><a href="/">我的組合</a><a href="/ta">技術分析</a></nav>
<div class="card"><ul>{links}</ul></div></main>""", "text/html; charset=utf-8")
            if url.path.startswith("/reports/weekly/") and url.path.endswith(".md"):
                f = REPORTS / "weekly" / url.path.rsplit("/", 1)[-1]
                if f.parent == REPORTS / "weekly" and f.exists():
                    import html
                    return self._send(200, f"""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<link rel="icon" type="image/png" href="/favicon-32.png"><title>每週摘要 {f.stem}</title><style>body{{margin:0;font:14px/1.7 system-ui,-apple-system,"PingFang TC",sans-serif;background:#0b0e16;color:#e8eaf0}}
main{{max-width:720px;margin:0 auto;padding:16px}}
.top,.card{{background:#141927;border:1px solid rgba(255,255,255,.08);border-radius:12px;padding:12px 16px;margin-bottom:12px}}
.top{{display:flex;flex-wrap:wrap;gap:8px;align-items:center}}.top b{{margin-right:auto;font-size:16px}}
.top a{{color:#e8eaf0;text-decoration:none;border:1px solid #2f3850;background:#1a2033;border-radius:8px;padding:6px 12px}}
pre{{white-space:pre-wrap;font:inherit;margin:0}}</style>
<main><nav class="top"><b>每週摘要 {f.stem}</b><a href="/">我的組合</a><a href="/reports/">所有報告</a></nav>
<div class="card"><pre>{html.escape(f.read_text(encoding="utf-8"))}</pre></div></main>""", "text/html; charset=utf-8")
                return self._json(404, {"error": "找不到每週摘要"})
            if url.path.startswith("/reports/") and url.path.endswith(".html"):
                f = REPORTS / url.path.rsplit("/", 1)[-1]
                if f.parent == REPORTS and f.exists():
                    return self._send(200, f.read_text(encoding="utf-8"), "text/html; charset=utf-8")
                return self._json(404, {"error": "找不到月報"})
            if url.path == "/" and "id" in q:      # 舊網址 /?id=2330 → 技術分析
                self.send_response(302)
                self.send_header("Location", "/ta?" + url.query)
                self.end_headers()
            elif url.path == "/":
                self._send(200, HOME.read_text(encoding="utf-8"), "text/html; charset=utf-8")
            elif url.path == "/ta":
                self._send(200, PAGE.read_text(encoding="utf-8"), "text/html; charset=utf-8")
            elif url.path == "/api/symbols":
                from . import symbols
                today = date.today().isoformat()
                if Handler.symbols_cache[0] != today:
                    Handler.symbols_cache = (today, symbols.all_symbols())
                self._json(200, Handler.symbols_cache[1])
            elif url.path == "/api/news":
                if not SYMBOL.match(sym):
                    return self._json(400, {"error": "代號格式不正確"})
                from . import news
                self._json(200, news.for_stock(sym, "TW" if sym[:1].isdigit() else "US"))
            elif url.path == "/api/home":
                from . import home
                self._json(200, home.build())
            elif url.path == "/api/fx":
                rows = data.load_series(data.FX_PATH, "spot_buy")[-120:]
                self._json(200, {"date": [d for d, _ in rows], "rate": [v for _, v in rows]})
            elif url.path == "/api/quota":
                self._json(200, {"official": data.quota(), "local": data.local_usage()})
            elif url.path == "/api/holdings":
                self._json(200, holdings())
            elif url.path in ("/api/stock", "/report"):
                if not SYMBOL.match(sym):
                    return self._json(400, {"error": "代號格式不正確"})
                result = ta.analyze(sym, refresh=q.get("refresh") == "1")
                if url.path == "/report":
                    self._send(200, embed(result), "text/html; charset=utf-8")
                else:
                    self._json(200, result)
            else:
                self._json(404, {"error": "not found"})
        except data.QuotaError as e:
            self._json(429, {"error": str(e)})
        except ValueError as e:
            self._json(404, {"error": str(e)})
        except Exception as e:  # FinMind 錯誤、額度用完等
            self._json(502, {"error": f"{type(e).__name__}: {e}"})

    def log_message(self, fmt, *args):
        print(f"[{self.log_date_time_string()}] {fmt % args}")


def _lan_ip():
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))      # 不會真的送出封包，只用來找出對外網卡的 IP
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def serve(port=8765, lan=False):
    host = "0.0.0.0" if lan else "127.0.0.1"
    if lan:
        Handler.lan_key = dashboard_key()
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"我的組合：http://127.0.0.1:{port}/   技術分析：http://127.0.0.1:{port}/ta   （Ctrl+C 結束）")
    if lan:
        ip = _lan_ip()
        print(f"手機（同一個 Wi-Fi）：http://{ip}:{port}/?key={Handler.lan_key}")
        print(f"月報列表：http://{ip}:{port}/reports/?key={Handler.lan_key}")
        print("⚠️ 只在家裡等可信任的網路開啟；咖啡廳、公司等公共網路請不要使用區域網路模式。")
    srv.serve_forever()
