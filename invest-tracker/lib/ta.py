"""個股技術分析：抓 FinMind 真實資料，計算指標、訊號與規則式檢核。

所有數值都由資料直接計算，不產生模擬或推估的「AI 分數」。
台股：日 K + 三大法人 + 融資融券；美股：只有日 K。
"""
import csv
import json
import math
from datetime import date, timedelta

from . import data, ledger, ta_plus

CACHE = data.DATA / "ta_cache"
INFO = data.DATA / "stock_info.csv"
LOOKBACK_DAYS = 1100     # 約 3 年：MA60 / MACD 暖機，並提供訊號回測樣本
SHOW = 260               # 回傳最近 260 個交易日給前端切換 60/120/250 日
CACHE_VERSION = 9        # 分析結果的欄位有變動時加 1，讓舊快取自動失效


# ---------- 基本資料 ----------

def stock_info(symbol):
    if not INFO.exists() or date.fromtimestamp(INFO.stat().st_mtime) < date.today() - timedelta(days=7):
        rows = _fetch_info()
        with INFO.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["stock_id", "stock_name", "industry_category", "type"])
            w.writeheader()
            seen = set()
            for r in rows:
                if r["stock_id"] in seen:
                    continue
                seen.add(r["stock_id"])
                w.writerow({k: r[k] for k in w.fieldnames})
    with INFO.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["stock_id"] == symbol:
                return r
    return None


def _fetch_info():
    return json.loads(data.http_get(*data.auth(f"{data.API}?dataset=TaiwanStockInfo"), timeout=30))["data"]


# ---------- 指標 ----------

def sma(xs, n):
    out, s = [], 0.0
    for i, x in enumerate(xs):
        s += x
        if i >= n:
            s -= xs[i - n]
        out.append(s / n if i >= n - 1 else None)
    return out


def ema(xs, n):
    k, out, e = 2 / (n + 1), [], None
    for x in xs:
        e = x if e is None else x * k + e * (1 - k)
        out.append(e)
    return out


def kd(high, low, close, n=9):
    """台灣慣用 KD(9,3,3)：K = 2/3 K昨 + 1/3 RSV，初始值 50。"""
    ks, ds, k, d = [], [], 50.0, 50.0
    for i in range(len(close)):
        hh, ll = max(high[max(0, i - n + 1):i + 1]), min(low[max(0, i - n + 1):i + 1])
        rsv = 50.0 if hh == ll else (close[i] - ll) / (hh - ll) * 100
        k = k * 2 / 3 + rsv / 3
        d = d * 2 / 3 + k / 3
        ks.append(k)
        ds.append(d)
    return ks, ds


def macd(close, fast=12, slow=26, sig=9):
    dif = [a - b for a, b in zip(ema(close, fast), ema(close, slow))]
    m = ema(dif, sig)
    return dif, m, [a - b for a, b in zip(dif, m)]


def rsi(close, n=14):
    out, gain, loss = [None], 0.0, 0.0
    for i in range(1, len(close)):
        ch = close[i] - close[i - 1]
        g, l_ = max(ch, 0), max(-ch, 0)
        if i <= n:
            gain += g / n
            loss += l_ / n
            out.append(None if i < n else (100 - 100 / (1 + gain / loss) if loss else 100))
        else:
            gain = (gain * (n - 1) + g) / n
            loss = (loss * (n - 1) + l_) / n
            out.append(100 - 100 / (1 + gain / loss) if loss else 100)
    return out


def bollinger(close, n=20, k=2):
    mid, up, low = sma(close, n), [], []
    for i, m in enumerate(mid):
        if m is None:
            up.append(None)
            low.append(None)
            continue
        win = close[i - n + 1:i + 1]
        sd = math.sqrt(sum((x - m) ** 2 for x in win) / n)
        up.append(m + k * sd)
        low.append(m - k * sd)
    return up, mid, low


def atr(high, low, close, n=14):
    tr = [high[0] - low[0]] + [max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1]))
                               for i in range(1, len(close))]
    return sma(tr, n)


# ---------- 抓資料 ----------

def _prices(symbol, market, start):
    if market == "US":
        raw = data.fetch("USStockPrice", symbol, start)
        return [{"date": r["date"], "open": r["Open"], "high": r["High"], "low": r["Low"],
                 "close": r["Close"], "volume": r["Volume"], "money": None, "trades": None, "adj": r.get("Adj_Close")}
                for r in raw if r["Close"] > 0]
    raw = data.fetch("TaiwanStockPrice", symbol, start)
    return [{"date": r["date"], "open": r["open"], "high": r["max"], "low": r["min"], "close": r["close"],
             "volume": r["Trading_Volume"], "money": r["Trading_money"], "trades": r["Trading_turnover"]}
            for r in raw if r["close"] > 0]


def _institutional(symbol, start):
    """三大法人買賣超（張）。外資含外資自營商，自營商含避險。"""
    groups = {"Foreign_Investor": "foreign", "Foreign_Dealer_Self": "foreign",
              "Investment_Trust": "trust", "Dealer_self": "dealer", "Dealer_Hedging": "dealer"}
    by_day = {}
    for r in data.fetch("TaiwanStockInstitutionalInvestorsBuySell", symbol, start):
        g = groups.get(r["name"])
        if g:
            day = by_day.setdefault(r["date"], {"foreign": 0, "trust": 0, "dealer": 0})
            day[g] += (r["buy"] - r["sell"]) / 1000
    days = sorted(by_day)
    out = {"date": days}
    for g in ("foreign", "trust", "dealer"):
        out[g] = [round(by_day[d][g]) for d in days]
    out["total"] = [a + b + c for a, b, c in zip(out["foreign"], out["trust"], out["dealer"])]
    return out


def _margin(symbol, start):
    rows = data.fetch("TaiwanStockMarginPurchaseShortSale", symbol, start)
    return {"date": [r["date"] for r in rows],
            "margin": [r["MarginPurchaseTodayBalance"] for r in rows],
            "short": [r["ShortSaleTodayBalance"] for r in rows]}


# ---------- 訊號與檢核 ----------

def _cross(a, b, i):
    """回傳 1 = a 由下往上穿越 b，-1 = 由上往下，0 = 無。"""
    if i == 0 or None in (a[i], b[i], a[i - 1], b[i - 1]):
        return 0
    if a[i - 1] <= b[i - 1] and a[i] > b[i]:
        return 1
    if a[i - 1] >= b[i - 1] and a[i] < b[i]:
        return -1
    return 0


def signals(s, window=60):
    out = []
    n = len(s["close"])
    vol20 = sma(s["volume"], 20)

    def add(i, kind, label, detail, tone):
        out.append({"date": s["date"][i], "kind": kind, "label": label, "detail": detail, "tone": tone})

    for i in range(max(1, n - window), n):
        c = _cross(s["k"], s["d"], i)
        if c:
            zone = "（低檔）" if c > 0 and s["k"][i] < 20 else "（高檔）" if c < 0 and s["k"][i] > 80 else ""
            add(i, "kd", f"KD {'黃金' if c > 0 else '死亡'}交叉{zone}", f"K {s['k'][i]:.1f} / D {s['d'][i]:.1f}",
                "bull" if c > 0 else "bear")
        c = _cross(s["dif"], s["macd"], i)
        if c:
            add(i, "macd", f"MACD {'黃金' if c > 0 else '死亡'}交叉", f"DIF {s['dif'][i]:.2f} / MACD {s['macd'][i]:.2f}",
                "bull" if c > 0 else "bear")
        if s["osc"][i - 1] <= 0 < s["osc"][i]:
            add(i, "osc", "柱狀體翻正", f"OSC {s['osc'][i]:.2f}", "bull")
        elif s["osc"][i - 1] >= 0 > s["osc"][i]:
            add(i, "osc", "柱狀體翻負", f"OSC {s['osc'][i]:.2f}", "bear")
        for ma in ("ma20", "ma60"):
            c = _cross(s["close"], s[ma], i)
            if c:
                add(i, "ma", f"{'站上' if c > 0 else '跌破'} {ma.upper()}", f"收盤 {s['close'][i]:.2f} / {ma.upper()} {s[ma][i]:.2f}",
                    "bull" if c > 0 else "bear")
        if vol20[i - 1] and s["volume"][i] > 2 * vol20[i - 1]:
            add(i, "vol", "爆量", f"成交量為 20 日均量 {s['volume'][i] / vol20[i - 1]:.1f} 倍",
                "bull" if s["close"][i] >= s["close"][i - 1] else "bear")
        if s["rsi"][i] and s["rsi"][i - 1]:
            if s["rsi"][i - 1] <= 70 < s["rsi"][i]:
                add(i, "rsi", "RSI 進入超買", f"RSI {s['rsi'][i]:.1f}", "neutral")
            elif s["rsi"][i - 1] >= 30 > s["rsi"][i]:
                add(i, "rsi", "RSI 進入超賣", f"RSI {s['rsi'][i]:.1f}", "neutral")
    return out


def warnings(s, chips, levels):
    """目前狀態的警示（技術警示報告）。level: alert / watch / info。"""
    out = []
    i = len(s["close"]) - 1
    c = s["close"][i]

    def add(level, title, detail):
        out.append({"level": level, "title": title, "detail": detail})

    k = s["k"][i]
    if k > 80:
        add("watch", "KD 高檔鈍化區", f"K 值 {k:.1f} > 80，追價風險升高")
    elif k < 20:
        add("watch", "KD 低檔區", f"K 值 {k:.1f} < 20，短線超賣")
    r = s["rsi"][i]
    if r and r > 70:
        add("watch", "RSI 超買", f"RSI(14) {r:.1f} > 70")
    elif r and r < 30:
        add("watch", "RSI 超賣", f"RSI(14) {r:.1f} < 30")
    if s["ma20"][i]:
        bias = c / s["ma20"][i] - 1
        if abs(bias) > 0.10:
            add("alert", f"月線乖離過大（{bias:+.1%}）", "股價與 MA20 距離超過 ±10%，容易出現均值回歸")
    if s["ma60"][i] and c < s["ma60"][i]:
        add("alert", "收盤低於季線", f"收盤 {c:.2f} < MA60 {s['ma60'][i]:.2f}，中期趨勢轉弱")
    if s["bb_up"][i] and c > s["bb_up"][i]:
        add("watch", "突破布林上軌", f"收盤 {c:.2f} > 上軌 {s['bb_up'][i]:.2f}")
    elif s["bb_low"][i] and c < s["bb_low"][i]:
        add("watch", "跌破布林下軌", f"收盤 {c:.2f} < 下軌 {s['bb_low'][i]:.2f}")
    vol20 = sma(s["volume"], 20)
    if vol20[i - 1] and s["volume"][i] > 2 * vol20[i - 1]:
        add("watch", "今日爆量", f"成交量為 20 日均量 {s['volume'][i] / vol20[i - 1]:.1f} 倍")
    if levels.get("atr_ratio") and levels["atr_ratio"] > 1.5:
        add("watch", "波動放大", f"ATR(14) 為 60 日平均的 {levels['atr_ratio']:.1f} 倍")
    if chips:
        f = chips["institutional"]["foreign"][-5:]
        if len(f) == 5 and all(x < 0 for x in f):
            add("alert", "外資連 5 日賣超", f"合計 {sum(f):,} 張")
        m = chips["margin"]["margin"]
        if len(m) >= 6 and m[-1] > m[-6] * 1.05 and c < s["close"][-6]:
            add("alert", "融資增、股價跌", f"融資 5 日增加 {m[-1] / m[-6] - 1:+.1%}，股價 5 日 {c / s['close'][-6] - 1:+.1%}，籌碼轉弱")
    if not out:
        add("info", "目前無警示", "各項技術條件都在正常區間")
    return out


def checklist(s, chips):
    """規則式趨勢檢核，每項都能直接驗證。"""
    i = len(s["close"]) - 1
    items = [
        ("收盤在月線之上", s["close"][i] > (s["ma20"][i] or 0), f"收盤 {s['close'][i]:.2f} vs MA20 {s['ma20'][i]:.2f}"),
        ("月線在季線之上", (s["ma20"][i] or 0) > (s["ma60"][i] or 0), f"MA20 {s['ma20'][i]:.2f} vs MA60 {s['ma60'][i]:.2f}"),
        ("MACD 位於零軸之上", s["dif"][i] > 0, f"DIF {s['dif'][i]:.2f}"),
        ("KD 多方排列", s["k"][i] > s["d"][i], f"K {s['k'][i]:.1f} vs D {s['d'][i]:.1f}"),
        ("RSI 在 50 之上", (s["rsi"][i] or 0) > 50, f"RSI {s['rsi'][i]:.1f}"),
    ]
    if chips:
        inst = chips["institutional"]
        f5, t5 = sum(inst["foreign"][-5:]), sum(inst["trust"][-5:])
        m = chips["margin"]["margin"]
        items += [
            ("外資近 5 日買超", f5 > 0, f"{f5:+,} 張"),
            ("投信近 5 日買超", t5 > 0, f"{t5:+,} 張"),
            ("融資近 5 日未增加", len(m) >= 6 and m[-1] <= m[-6], f"{m[-1] - m[-6]:+,} 張" if len(m) >= 6 else "資料不足"),
        ]
    return [{"name": a, "pass": bool(b), "detail": c} for a, b, c in items]


def _trend(s):
    i = len(s["close"]) - 1
    c, m20, m60 = s["close"][i], s["ma20"][i], s["ma60"][i]
    if m20 and m60 and c > m20 > m60:
        return "多頭排列"
    if m20 and m60 and c < m20 < m60:
        return "空頭排列"
    return "區間整理"


def _position(symbol):
    txns = [t for t in ledger.load() if t["symbol"] == symbol]
    if not txns:
        return None
    cfg = data.load_config()
    last = max(t["date"] for t in txns)
    shares = sum(t["shares"] * ledger.split_factor(cfg, symbol, t["date"], last) for t in txns)
    cost = sum(t["shares"] * t["price"] + t["fee"] for t in txns)
    plan = next((m for m, p in cfg["plans"].items() if symbol in p["amounts"]), None)
    return {"shares": shares, "avg_cost": cost / shares if shares else None, "cost": cost,
            "dca": plan is not None,
            "dca_amount": cfg["plans"][plan]["amounts"][symbol] if plan else None}


# ---------- 主流程 ----------

def analyze(symbol, refresh=False):
    symbol = symbol.strip().upper()
    today = date.today().isoformat()
    cache = CACHE / f"{symbol}_{today}_v{CACHE_VERSION}.json"
    if cache.exists() and not refresh:
        return json.loads(cache.read_text(encoding="utf-8"))

    market = "TW" if symbol[:1].isdigit() else "US"
    start = (date.today() - timedelta(days=LOOKBACK_DAYS)).isoformat()
    rows = _prices(symbol, market, start)
    if len(rows) < 30:
        raise ValueError(f"{symbol} 查無足夠的價格資料（{len(rows)} 筆）")
    info = stock_info(symbol) if market == "TW" else None
    usinfo = None
    if market == "US":
        try:
            usinfo = ta_plus.us_info(symbol)
        except Exception:
            usinfo = None

    s = {k: [r[k] for r in rows] for k in ("date", "open", "high", "low", "close", "volume")}
    s["volume"] = [v / 1000 if market == "TW" else v for v in s["volume"]]   # 台股換算成張
    cl = s["close"]
    for n in (5, 10, 20, 60):
        s[f"ma{n}"] = sma(cl, n)
    s["k"], s["d"] = kd(s["high"], s["low"], cl)
    s["dif"], s["macd"], s["osc"] = macd(cl)
    s["rsi"] = rsi(cl)
    s["bb_up"], s["bb_mid"], s["bb_low"] = bollinger(cl)
    atr14 = atr(s["high"], s["low"], cl)

    chips = None
    if market == "TW":
        cstart = s["date"][-SHOW] if len(s["date"]) >= SHOW else s["date"][0]
        chips = {"institutional": _institutional(symbol, cstart), "margin": _margin(symbol, cstart)}

    i = len(cl) - 1
    y_ago = (date.fromisoformat(s["date"][i]) - timedelta(days=365)).isoformat()
    w52 = [j for j, d in enumerate(s["date"]) if d >= y_ago]
    money = [r["money"] for r in rows[-20:]]
    vol_sh = [r["volume"] for r in rows[-20:]]
    atr_hist = [x for x in atr14[-60:] if x]
    rets = [cl[j] / cl[j - 1] - 1 for j in range(len(cl) - 20, len(cl))]
    mean = sum(rets) / len(rets)
    levels = {
        "vwap20": sum(money) / sum(vol_sh) if market == "TW" and all(money) and sum(vol_sh) else None,
        "support20": min(s["low"][-20:]), "resistance20": max(s["high"][-20:]),
        "support60": min(s["low"][-60:]), "resistance60": max(s["high"][-60:]),
        "high52": max(s["high"][j] for j in w52), "low52": min(s["low"][j] for j in w52),
        "atr14": atr14[i], "atr_ratio": atr14[i] / (sum(atr_hist) / len(atr_hist)) if atr_hist else None,
        "vol20_ann": math.sqrt(sum((r - mean) ** 2 for r in rets) / 19) * math.sqrt(250),
        "bias20": cl[i] / s["ma20"][i] - 1 if s["ma20"][i] else None,
    }

    prev = cl[i - 1]
    quote = {"date": s["date"][i], "close": cl[i], "change": cl[i] - prev, "change_pct": cl[i] / prev - 1,
             "open": s["open"][i], "high": s["high"][i], "low": s["low"][i], "volume": s["volume"][i],
             "trades": rows[i]["trades"], "rows": len(rows)}

    sig = signals(s)
    checks = checklist(s, chips)
    pos = _position(symbol)
    if pos and pos["avg_cost"]:
        pos["pnl_pct"] = cl[i] / pos["avg_cost"] - 1

    extra = {}
    if market == "TW":
        x = ta_plus.fetch_extra(symbol, s["date"][-60])
        dts = [(d, x["daytrade"].get(d, 0) / (v * 1000) if v else 0) for d, v in zip(s["date"][-60:], s["volume"][-60:])]
        extra = {"daytrade_series": {"date": [d for d, _ in dts], "ratio": [round(r, 4) for _, r in dts]},
                 "daytrade_ratio20": sum(r for _, r in dts[-20:]) / 20,
                 "turnover20": (sum(s["volume"][-20:]) / 20 * 1000 / x["shares_issued"]) if x["shares_issued"] else None,
                 "foreign_ratio_last": x["foreign_ratio"][-1][1] / 100 if x["foreign_ratio"] else None}
    warns = warnings(s, chips, levels)
    trend = _trend(s)
    plus = ta_plus.build(s, chips, extra, levels, warns, trend, pos, market)
    if data.WEB:
        plus["guidance"] = None        # 網頁版沒有個人持倉與投資政策，不產生投資參考
    else:
        try:
            from . import report
            snap = report.snapshot()
        except Exception:
            snap = None
        plus["guidance"] = ta_plus.guidance(symbol, market, pos, snap, data.load_config(), plus, levels, s)
    if market == "US":
        plus["us"] = ta_plus.us_extra(rows, s, data.load_config())
        from . import us_chips
        plus["us_chips"] = us_chips.analyze(symbol, s["date"])
        ta_plus.add_us_chip_scores(plus, levels)
    try:
        mkt = ta_plus.us_market() if market == "US" else ta_plus.market()
    except Exception as e:  # 大盤資料失敗不影響個股分析
        mkt = {"error": str(e)}

    out = {
        "symbol": symbol, "market": market,
        "name": info["stock_name"] if info else (usinfo or {}).get("name") or data.load_config().get("names", {}).get(symbol, symbol),
        "industry": info["industry_category"] if info else "美股 · " + ((usinfo or {}).get("sector") or "—"),
        "us_info": usinfo,
        "board": (info or {}).get("type", "us"),
        "generated": today, "quote": quote, "levels": levels,
        "series": {k: [None if v is None else round(v, 4) for v in vals[-SHOW:]] if k != "date" else vals[-SHOW:]
                   for k, vals in s.items()},
        "chips": chips, "signals": sig, "warnings": warns,
        "checklist": checks, "trend": trend, "position": pos, "plus": plus, "market_overview": mkt,
        "source": "FinMind（TWSE / TPEx 公開資料，T+1）" if market == "TW" else "FinMind USStockPrice",
        "web": data.WEB,
    }
    CACHE.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out
