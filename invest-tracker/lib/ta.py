"""個股技術分析：抓 FinMind 真實資料，計算指標、訊號與規則式檢核。

所有數值都由資料直接計算，不產生模擬或推估的「AI 分數」。
台股：日 K + 三大法人 + 融資融券；美股：只有日 K。
"""
import csv
import json
import math
from datetime import date, timedelta

from . import data, ledger, ta_plus, tdcc

CACHE = data.DATA / "ta_cache"
INFO = data.DATA / "stock_info.csv"
LOOKBACK_DAYS = 1100     # 約 3 年：MA60 / MACD 暖機，並提供訊號回測樣本
SHOW = 260               # 回傳最近 260 個交易日給前端切換 60/120/250 日
CACHE_VERSION = 14       # 分析結果的欄位有變動時加 1，讓舊快取自動失效


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


def _sbl(symbol, start):
    """借券賣出餘額（張）。借券賣出多為法人放空或避險；和融券是兩套制度。"""
    rows = data.fetch("TaiwanDailyShortSaleBalances", symbol, start)
    return {"date": [r["date"] for r in rows],
            "sbl": [round(r["SBLShortSalesCurrentDayBalance"] / 1000) for r in rows]}


def _optional(fn, *args):
    """補充資料抓不到時不影響整體分析（額度用完仍要往上丟）。"""
    try:
        return fn(*args)
    except data.QuotaError:
        raise
    except Exception:
        return None


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


# 量縮後帶量上漲：連續 QUIET_DAYS 日成交量 < QUIET_RATIO 倍 20 日均量，當天量 > SURGE_RATIO 倍且收盤上漲
QUIET_DAYS, QUIET_RATIO, SURGE_RATIO = 3, 0.7, 1.5

# 每個訊號／警示的白話說明（key 是標題去掉括號補充後的文字）
HINTS = {
    "KD 黃金交叉": "短線動能轉強：收盤在近 9 日高低區間的位置往上走。低檔出現較常被視為反彈跡象，盤整時容易反覆出現。",
    "KD 死亡交叉": "短線動能轉弱：收盤在近 9 日高低區間的位置往下走。高檔出現代表漲勢可能放緩。",
    "MACD 黃金交叉": "中期動能轉強：短期均線的上升速度超過長期均線。反應比 KD 慢，但比較少假訊號。",
    "MACD 死亡交叉": "中期動能轉弱：短期均線的上升速度落後長期均線。",
    "柱狀體翻正": "多空力道由弱轉強的早期跡象，通常比 MACD 交叉早一點出現。",
    "柱狀體翻負": "多空力道由強轉弱的早期跡象，通常比 MACD 交叉早一點出現。",
    "站上 MA20": "股價回到近一個月的平均價格之上，近一個月買進的人多數處於獲利。",
    "跌破 MA20": "股價跌到近一個月的平均價格之下，近一個月買進的人多數處於虧損。",
    "站上 MA60": "股價回到近一季的平均價格之上，中期走勢轉好。",
    "跌破 MA60": "股價跌到近一季的平均價格之下，中期走勢轉弱。",
    "爆量": "成交量放大到平常的 2 倍以上，代表有大量資金進出；收紅代表買方較積極，收黑代表賣方較積極。",
    "量縮後帶量上漲": "連續幾天成交清淡（市場在觀望），接著帶量上漲，代表有資金開始進場。之後量能能不能延續才是關鍵。",
    "RSI 進入超買": "近 14 日漲多跌少，短線過熱，容易整理；但強勢股可以維持超買很久。",
    "RSI 進入超賣": "近 14 日跌多漲少，短線超跌，可能反彈；但弱勢股可以維持超賣很久。",
    "KD 高檔鈍化區": "K 值在 80 以上代表漲勢強，但這時追價，買在短線高點的機率也比較高。",
    "KD 低檔區": "短線跌多，可能出現反彈；但弱勢股可以在低檔停留很久。",
    "月線乖離過大": "股價離近一個月的平均價格太遠，歷史上常會拉回或反彈，往月線靠近。",
    "突破布林上軌": "股價超出近 20 日正常波動範圍的上緣：代表很強，也代表短線偏熱。",
    "跌破布林下軌": "股價跌出近 20 日正常波動範圍的下緣：代表很弱，也代表短線超跌。",
    "波動放大": "最近每天的漲跌幅度比平常大很多，價格可能劇烈變動。",
    "外資連 5 日賣超": "外資連續一週站在賣方，常會壓抑股價表現。",
    "融資增、股價跌": "借錢買股的人變多，股價卻在跌；如果繼續跌，可能引發融資斷頭的賣壓。",
    "借券賣出餘額 5 日增加": "借券賣出多半是法人放空或避險；餘額快速增加，代表看壞或避險的部位變多。",
    "千張大戶持股連 3 週減少": "持有 1,000 張以上的大股東持股比例連續下降，代表大戶在調節；也可能是 ETF 贖回或股權移轉，要搭配新聞看。",
}
HINTS["RSI 超買"], HINTS["RSI 超賣"] = HINTS["RSI 進入超買"], HINTS["RSI 進入超賣"]
HINTS["收盤低於季線"], HINTS["今日爆量"] = HINTS["跌破 MA60"], HINTS["爆量"]
HINTS["今日量縮後帶量上漲"] = HINTS["量縮後帶量上漲"]


def hint(title):
    return HINTS.get(title.split("（")[0])


def quiet_then_surge(s, vol20, i):
    """第 i 日是否「量縮後帶量上漲」。vol20 = sma(volume, 20)；每天都和前一日的 20 日均量比（不含當天）。"""
    if i - QUIET_DAYS - 1 < 0 or not vol20[i - QUIET_DAYS - 1]:
        return False
    v = s["volume"]
    return (all(v[j] < QUIET_RATIO * vol20[j - 1] for j in range(i - QUIET_DAYS, i))
            and v[i] > SURGE_RATIO * vol20[i - 1] and s["close"][i] > s["close"][i - 1])


def volume_state(s, vol20, i):
    """第 i 日的量比（÷ 前一日的 20 日均量）、狀態，以及到今天為止連續量縮的天數。"""
    v = s["volume"]
    if i < 1 or not vol20[i - 1]:
        return None
    ratio = v[i] / vol20[i - 1]
    streak, j = 0, i
    while j >= 1 and vol20[j - 1] and v[j] < QUIET_RATIO * vol20[j - 1]:
        streak, j = streak + 1, j - 1
    label = ("爆量" if ratio > 2 else "量增" if ratio > SURGE_RATIO else "量縮" if ratio < QUIET_RATIO else "正常")
    return {"ratio": round(ratio, 2), "label": label, "quiet_streak": streak}


def signals(s, window=60):
    out = []
    n = len(s["close"])
    vol20 = sma(s["volume"], 20)

    def add(i, kind, label, detail, tone):
        out.append({"date": s["date"][i], "kind": kind, "label": label, "detail": detail, "tone": tone,
                    "hint": hint(label)})

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
        if quiet_then_surge(s, vol20, i):
            add(i, "vol", "量縮後帶量上漲", f"前 {QUIET_DAYS} 日量都低於均量 {QUIET_RATIO} 倍，"
                f"今日量為 20 日均量 {s['volume'][i] / vol20[i - 1]:.1f} 倍", "bull")
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
        out.append({"level": level, "title": title, "detail": detail, "hint": hint(title)})

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
    if quiet_then_surge(s, vol20, i):
        add("watch", "今日量縮後帶量上漲", f"前 {QUIET_DAYS} 日量都低於均量 {QUIET_RATIO} 倍，"
            f"今日量為 20 日均量 {s['volume'][i] / vol20[i - 1]:.1f} 倍、收盤上漲")
    if levels.get("atr_ratio") and levels["atr_ratio"] > 1.5:
        add("watch", "波動放大", f"ATR(14) 為 60 日平均的 {levels['atr_ratio']:.1f} 倍")
    if chips:
        f = chips["institutional"]["foreign"][-5:]
        if len(f) == 5 and all(x < 0 for x in f):
            add("alert", "外資連 5 日賣超", f"合計 {sum(f):,} 張")
        m = chips["margin"]["margin"]
        if len(m) >= 6 and m[-1] > m[-6] * 1.05 and c < s["close"][-6]:
            add("alert", "融資增、股價跌", f"融資 5 日增加 {m[-1] / m[-6] - 1:+.1%}，股價 5 日 {c / s['close'][-6] - 1:+.1%}，籌碼轉弱")
        b = (chips.get("sbl") or {}).get("sbl") or []
        # 增幅 > 10%，且增加的張數超過半天的平均成交量，避免餘額很小時的雜訊
        if len(b) >= 6 and b[-6] and b[-1] > b[-6] * 1.10 and vol20[i] and b[-1] - b[-6] > 0.5 * vol20[i]:
            add("watch", "借券賣出餘額 5 日增加", f"{b[-1] / b[-6] - 1:+.1%}（{b[-1] - b[-6]:+,} 張），目前 {b[-1]:,} 張")
        t = (chips.get("tdcc") or {}).get("big1000") or []
        if len(t) >= 4 and t[-4] > t[-3] > t[-2] > t[-1]:
            add("watch", "千張大戶持股連 3 週減少", f"{t[-4]:.2f}% → {t[-1]:.2f}%（集保每週資料）")
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
    s["vol_ma20"] = sma(s["volume"], 20)

    chips = None
    if market == "TW":
        cstart = s["date"][-SHOW] if len(s["date"]) >= SHOW else s["date"][0]
        chips = {"institutional": _institutional(symbol, cstart), "margin": _margin(symbol, cstart),
                 "sbl": _optional(_sbl, symbol, cstart), "tdcc": _optional(tdcc.history, symbol),
                 "tdcc_local_only": data.WEB}

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
             "trades": rows[i]["trades"], "rows": len(rows),
             "vol_state": volume_state(s, sma(s["volume"], 20), i)}

    sig = signals(s)
    checks = checklist(s, chips)
    pos = _position(symbol)
    if pos and pos["avg_cost"]:
        pos["pnl_pct"] = cl[i] / pos["avg_cost"] - 1

    extra = {}
    if market == "TW":
        x = ta_plus.fetch_extra(symbol, s["date"][-60])
        chips["shares_issued"] = x["shares_issued"]
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

    from . import fundamentals
    try:
        fund = fundamentals.analyze(symbol)
    except data.QuotaError:
        raise
    except Exception as e:  # 基本面資料失敗不影響技術分析
        fund = {"error": str(e)}

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
        "fundamentals": fund,
        "source": "FinMind（TWSE / TPEx 公開資料，T+1）" if market == "TW" else "FinMind USStockPrice",
        "web": data.WEB,
    }
    CACHE.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out
