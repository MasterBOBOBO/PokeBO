"""綜合儀表板的進階分析。每個分數都附上 explain（公式 + 原始數值），前端 ⓘ 會顯示。

原則：只用真實資料計算；做不到的（券商分點、大戶散戶）不估算。
區間統計是「歷史分布」，不是預測。
"""
import json
import math
from datetime import date, timedelta

from . import data

MARKET_CACHE = data.DATA / "ta_cache"


def clip(x, lo=0.0, hi=100.0):
    return max(lo, min(hi, x))


def _pct(x):
    return f"{x * 100:+.1f}%"


# ---------- 額外資料 ----------

def fetch_extra(symbol, start):
    """當沖與股本／外資持股（台股）。"""
    dt = {r["date"]: r["Volume"] for r in data.fetch("TaiwanStockDayTrading", symbol, start)}
    sh = data.fetch("TaiwanStockShareholding", symbol, start)
    return {"daytrade": dt,
            "shares_issued": sh[-1]["NumberOfSharesIssued"] if sh else None,
            "foreign_ratio": [(r["date"], r["ForeignInvestmentSharesRatio"]) for r in sh]}


def market(today=None):
    """大盤：加權指數、全市場融資、法人總買賣超。同一天快取一次。"""
    today = today or date.today().isoformat()
    cache = MARKET_CACHE / f"_market_{today}.json"
    if cache.exists():
        out = json.loads(cache.read_text(encoding="utf-8"))
        out.setdefault("kind", "TW")      # 舊版快取沒有 kind 欄位
        return out
    start = (date.fromisoformat(today) - timedelta(days=120)).isoformat()
    idx = [(r["date"], r["close"]) for r in data.fetch("TaiwanStockPrice", "TAIEX", start) if r["close"] > 0]
    margin = [(r["date"], r["TodayBalance"]) for r in data.fetch("TaiwanStockTotalMarginPurchaseShortSale", "", start)
              if r["name"] == "MarginPurchaseMoney"]
    inst = {}
    for r in data.fetch("TaiwanStockTotalInstitutionalInvestors", "", start):
        if r["name"] == "total":
            inst[r["date"]] = (r["buy"] - r["sell"]) / 1e8        # 億元
    closes = [c for _, c in idx]
    ma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else None
    ma60 = sum(closes[-60:]) / 60 if len(closes) >= 60 else None
    days = sorted(inst)
    out = {
        "date": idx[-1][0] if idx else None,
        "taiex": closes[-1] if closes else None,
        "taiex_chg": closes[-1] / closes[-2] - 1 if len(closes) > 1 else None,
        "taiex_ma20": ma20, "taiex_ma60": ma60,
        "series": {"date": [d for d, _ in idx[-60:]], "close": closes[-60:]},
        "margin_money": margin[-1][1] / 1e8 if margin else None,                    # 億元
        "margin_chg5": (margin[-1][1] / margin[-6][1] - 1) if len(margin) > 5 else None,
        "inst_today": inst[days[-1]] if days else None,
        "inst_5d": sum(inst[d] for d in days[-5:]) if days else None,
        "inst_20d": sum(inst[d] for d in days[-20:]) if days else None,
    }
    score, why = 0, []
    if ma20 and closes[-1] > ma20:
        score += 1
        why.append("指數在月線上")
    if ma20 and ma60 and ma20 > ma60:
        score += 1
        why.append("月線在季線上")
    if out["inst_5d"] and out["inst_5d"] > 0:
        score += 1
        why.append("法人 5 日買超")
    if out["margin_chg5"] is not None and out["margin_chg5"] <= 0.02:
        score += 1
        why.append("融資未明顯擴張")
    out["state"] = ["偏弱", "偏弱", "中性", "偏強", "偏強"][score]
    out["score"] = score
    out["kind"] = "TW"
    out["explain"] = "大盤狀態 = 下列 4 項符合幾項（0–1 偏弱、2 中性、3–4 偏強）：指數 > MA20、MA20 > MA60、" \
                     "法人 5 日合計買超、全市場融資 5 日增幅 ≤ 2%。目前符合：" + ("、".join(why) or "無")
    MARKET_CACHE.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out


# ---------- 計算 ----------

def volume_profile(s, n=60, bins=24):
    """價量分布：每天的成交量平均分配到當日最低–最高價之間，統計各價格區間累積量。"""
    hi, lo = max(s["high"][-n:]), min(s["low"][-n:])
    if hi <= lo:
        return None
    step = (hi - lo) / bins
    vol = [0.0] * bins
    for h, l, v in zip(s["high"][-n:], s["low"][-n:], s["volume"][-n:]):
        a, b = int((l - lo) / step), min(bins - 1, int((h - lo) / step))
        for k in range(a, b + 1):
            vol[k] += v / (b - a + 1)
    total = sum(vol)
    edges = [lo + step * k for k in range(bins + 1)]
    poc = max(range(bins), key=lambda k: vol[k])
    # 價值區（成交量 70% 集中區）：從 POC 往兩側擴張
    a = b = poc
    acc = vol[poc]
    while acc < total * 0.7 and (a > 0 or b < bins - 1):
        left = vol[a - 1] if a > 0 else -1
        right = vol[b + 1] if b < bins - 1 else -1
        if right >= left:
            b += 1
            acc += vol[b]
        else:
            a -= 1
            acc += vol[a]
    close = s["close"][-1]
    above = sum(v for k, v in enumerate(vol) if edges[k] > close) / total
    below = sum(v for k, v in enumerate(vol) if edges[k + 1] < close) / total
    return {"edges": [round(e, 2) for e in edges], "vol": [round(v) for v in vol], "poc": round((edges[poc] + edges[poc + 1]) / 2, 2),
            "va_low": round(edges[a], 2), "va_high": round(edges[b + 1], 2),
            "above": above, "below": below, "close": close,
            "explain": f"近 {n} 日每天的成交量平均分配到當日最低到最高價之間，分成 {bins} 個價格區間累計。"
                       f"最大量區（POC）{(edges[poc] + edges[poc + 1]) / 2:.2f}；70% 成交量集中在 {edges[a]:.2f}–{edges[b + 1]:.2f}。"
                       f"現價上方累積 {above:.0%} 的量（潛在賣壓／套牢區），下方 {below:.0%}（潛在支撐）。"}


def inst_cost(chips, s, n):
    """法人估算成本：近 n 日「有買超的日子」以買超張數加權的收盤價。"""
    close = dict(zip(s["date"], s["close"]))
    out = {}
    for g, name in (("foreign", "外資"), ("trust", "投信")):
        pairs = [(v, close[d]) for d, v in zip(chips["date"][-n:], chips[g][-n:]) if v > 0 and d in close]
        vol = sum(v for v, _ in pairs)
        out[g] = {"name": name, "cost": sum(v * c for v, c in pairs) / vol if vol else None, "buy_lots": vol}
    return out


def streak(xs):
    """連續買超（正）或賣超（負）天數。"""
    if not xs or xs[-1] == 0:
        return 0
    sign, n = (1 if xs[-1] > 0 else -1), 0
    for x in reversed(xs):
        if (x > 0) - (x < 0) != sign:
            break
        n += 1
    return n * sign


def forward_returns(close, h=10):
    return [close[i + h] / close[i] - 1 for i in range(len(close) - h)]


def percentile(xs, p):
    xs = sorted(xs)
    if not xs:
        return None
    k = (len(xs) - 1) * p
    f, c = math.floor(k), math.ceil(k)
    return xs[f] + (xs[c] - xs[f]) * (k - f)


def range_fan(s, h=10):
    """歷史 h 日報酬分布（本檔過去資料）→ 未來 h 日價格區間。不是預測。"""
    rets = forward_returns(s["close"], h)
    if len(rets) < 60:
        return None
    c = s["close"][-1]
    qs = {p: percentile(rets, p) for p in (0.05, 0.25, 0.5, 0.75, 0.95)}
    path = {p: [round(c * (1 + q * math.sqrt(t / h) if q is not None else c), 2) for t in range(h + 1)]
            for p, q in qs.items()}
    up = sum(r > 0.02 for r in rets) / len(rets)
    down = sum(r < -0.02 for r in rets) / len(rets)
    return {"h": h, "n": len(rets), "q": qs, "path": {str(k): v for k, v in path.items()},
            "up": up, "flat": 1 - up - down, "down": down,
            "explain": f"用本檔過去 {len(rets)} 個交易日的「{h} 日後報酬」統計分布（不是預測）："
                       f"5%–95% 區間 {_pct(qs[0.05])} ~ {_pct(qs[0.95])}，中位數 {_pct(qs[0.5])}。"
                       f"歷史上 {h} 日後上漲 >2% 的比例 {up:.0%}、跌 >2% 的比例 {down:.0%}。區間以 √t 從今天展開。"}


def signal_backtest(s, h=10):
    """本檔歷史上各訊號出現後 h 日的表現，對照所有交易日的基準。"""
    from .ta import _cross
    close, n = s["close"], len(s["close"])
    base = forward_returns(close, h)
    vol20 = [None] * n
    for i in range(20, n):
        vol20[i] = sum(s["volume"][i - 20:i]) / 20
    tests = {
        "KD 黃金交叉": lambda i: _cross(s["k"], s["d"], i) == 1,
        "MACD 黃金交叉": lambda i: _cross(s["dif"], s["macd"], i) == 1,
        "站上 MA20": lambda i: _cross(close, s["ma20"], i) == 1,
        "爆量上漲": lambda i: vol20[i] and s["volume"][i] > 2 * vol20[i] and close[i] > close[i - 1],
        "KD 死亡交叉": lambda i: _cross(s["k"], s["d"], i) == -1,
        "跌破 MA20": lambda i: _cross(close, s["ma20"], i) == -1,
    }
    rows = []
    for name, f in tests.items():
        rs = [close[i + h] / close[i] - 1 for i in range(61, n - h) if f(i)]
        rows.append({"signal": name, "n": len(rs), "win": sum(r > 0 for r in rs) / len(rs) if rs else None,
                     "avg": sum(rs) / len(rs) if rs else None, "enough": len(rs) >= 10})
    bw = sum(r > 0 for r in base) / len(base) if base else None
    return {"h": h, "base_win": bw, "base_n": len(base), "rows": rows,
            "explain": f"在本檔過去的價格資料中，找出每次出現該訊號的日子，統計 {h} 個交易日後的漲跌。"
                       f"基準 = 任意一天買進持有 {h} 日的上漲比例 {bw:.0%}。勝率要明顯高於基準才有參考價值；"
                       f"樣本少於 10 次會標示「樣本不足」。過去表現不代表未來。"}


def energy(s, n=20):
    up = sum(v for v, c, o in zip(s["volume"][-n:], s["close"][-n:], s["open"][-n:]) if c >= o)
    dn = sum(v for v, c, o in zip(s["volume"][-n:], s["close"][-n:], s["open"][-n:]) if c < o)
    tot = up + dn or 1
    return {"bull": up / tot, "bear": dn / tot, "ratio": up / dn if dn else None,
            "explain": f"近 {n} 日紅 K（收 ≥ 開）的成交量 {up:,.0f}，黑 K 的成交量 {dn:,.0f}；"
                       f"多方能量 = 紅 K 量 ÷ 總量 = {up / tot:.0%}。"}


def radar(s, chips, extra, levels, market_kind):
    """多維度評分（0–100，越高越好）。"""
    i = len(s["close"]) - 1
    c = s["close"][i]
    dims = []

    def add(name, score, explain):
        dims.append({"name": name, "score": round(clip(score)), "explain": explain})

    ma20_up = s["ma20"][i] > s["ma20"][i - 5] if s["ma20"][i - 5] else False
    parts = [c > s["ma20"][i], s["ma20"][i] > s["ma60"][i], ma20_up, c > s["ma60"][i]]
    add("趨勢", 25 * sum(parts), "4 項各 25 分：收盤 > MA20、MA20 > MA60、MA20 較 5 日前上升、收盤 > MA60。"
        f"符合 {sum(parts)} 項。")
    r = s["rsi"][i] or 50
    m = 100 if s["dif"][i] > s["macd"][i] else 0
    k = 100 if s["k"][i] > s["d"][i] else 0
    add("動能", (r + m + k) / 3, f"(RSI {r:.0f} + MACD 在訊號線上 {m} + K > D {k}) ÷ 3。")
    vs = levels["vol20_ann"]
    add("穩定度", 100 - vs * 200, f"100 − 20 日年化波動度 {vs:.0%} × 200（波動 0% = 100 分，50% 以上 = 0 分）。")
    if chips:
        tot = chips["institutional"]["total"][-20:]
        pos = sum(x > 0 for x in tot)
        add("法人", pos / len(tot) * 100 if tot else 50, f"近 20 日中三大法人合計買超的天數 {pos}/{len(tot)}。")
        mg = chips["margin"]["margin"]
        chg = mg[-1] / mg[-21] - 1 if len(mg) > 20 and mg[-21] else 0
        dtr = extra.get("daytrade_ratio20") or 0
        add("籌碼", 70 - chg * 300 - max(0, dtr - 0.2) * 150,
            f"70 − 融資 20 日變化 {_pct(chg)} × 300 − 當沖比例超過 20% 的部分 × 150（當沖比例 {dtr:.0%}）。")
        money = sum(v * p for v, p in zip(s["volume"][-20:], s["close"][-20:])) / 20 * 1000
        add("流動性", (math.log10(max(money, 1)) - 7) / 3 * 100,
            f"20 日平均成交金額 NT${money / 1e8:,.1f} 億，以對數換算（1 千萬 = 0 分，100 億 = 100 分）。")
    if not chips:
        money = sum(v * p for v, p in zip(s["volume"][-20:], s["close"][-20:])) / 20
        add("流動性", (math.log10(max(money, 1)) - 7) / 3 * 100,
            f"20 日平均成交金額 US${money / 1e6:,.0f} 百萬，以對數換算（1 千萬美元 = 0 分，100 億美元 = 100 分）。")
    total = sum(d["score"] for d in dims) / len(dims)
    grade = "A" if total >= 80 else "B" if total >= 60 else "C" if total >= 40 else "D"
    return {"dims": dims, "total": round(total), "grade": grade,
            "explain": "總分 = 各維度平均；A ≥ 80、B ≥ 60、C ≥ 40、D < 40。"
                       + ("" if chips else "美股沒有三大法人與融資融券制度的資料，所以不算法人，共 4 個維度。")}


def risk_radar(s, chips, extra, levels):
    """風險面（0–100，越高風險越高）。"""
    i = len(s["close"]) - 1
    dims = []

    def add(name, score, explain):
        dims.append({"name": name, "score": round(clip(score)), "explain": explain})

    add("波動風險", levels["vol20_ann"] * 200, f"20 日年化波動度 {levels['vol20_ann']:.0%} × 200。")
    b = abs(levels["bias20"] or 0)
    add("乖離風險", b * 500, f"|月線乖離率| {b:.1%} × 500（乖離 20% = 100 分）。")
    k = s["k"][i]
    add("過熱風險", max(0, k - 50) * 2, f"(K 值 {k:.0f} − 50) × 2，K 值越高越接近過熱。")
    if chips:
        f = chips["institutional"]["foreign"]
        st = streak(f)
        add("法人賣壓", max(0, -st) * 20 + (30 if sum(f[-5:]) < 0 else 0),
            f"外資連續賣超 {max(0, -st)} 天 × 20，加上 5 日合計賣超時 +30（5 日合計 {sum(f[-5:]):+,} 張）。")
        dtr = extra.get("daytrade_ratio20") or 0
        add("當沖過熱", dtr * 250, f"20 日平均當沖比例 {dtr:.0%} × 250（40% = 100 分）。")
        mg = chips["margin"]["margin"]
        chg5 = mg[-1] / mg[-6] - 1 if len(mg) > 5 and mg[-6] else 0
        p5 = s["close"][i] / s["close"][i - 5] - 1
        add("融資風險", max(0, chg5) * 600 + (40 if chg5 > 0 and p5 < 0 else 0),
            f"融資 5 日增幅 {_pct(chg5)} × 600；若融資增、股價跌（5 日 {_pct(p5)}）再 +40。")
    total = sum(d["score"] for d in dims) / len(dims)
    level = "高" if total >= 60 else "中" if total >= 35 else "低"
    return {"dims": dims, "total": round(total), "level": level,
            "explain": "風險指數 = 各風險維度平均；≥ 60 高、≥ 35 中、< 35 低。"}


def lights(s, chips, warnings, radar_r):
    """信號燈：紅 = 偏強、黃 = 中性、綠 = 偏弱（台股慣例紅漲綠跌）；風險燈另計。"""
    d = {x["name"]: x["score"] for x in radar_r["dims"]}

    def tone(v):
        return "bull" if v >= 60 else "neutral" if v >= 40 else "bear"

    out = [{"name": "趨勢", "tone": tone(d["趨勢"]), "value": d["趨勢"]},
           {"name": "動能", "tone": tone(d["動能"]), "value": d["動能"]}]
    if "法人" in d:
        out.append({"name": "籌碼", "tone": tone((d["法人"] + d["籌碼"]) / 2), "value": round((d["法人"] + d["籌碼"]) / 2)})
    alerts = sum(w["level"] == "alert" for w in warnings)
    watch = sum(w["level"] == "watch" for w in warnings)
    out.append({"name": "風險", "tone": "risk-high" if alerts else "risk-mid" if watch else "risk-low",
                "value": f"{alerts} 警示 / {watch} 留意"})
    return {"items": out, "explain": "趨勢、動能、籌碼燈號取自多維度分數：≥ 60 紅燈（偏強）、40–59 黃燈、< 40 綠燈（偏弱）。"
                                     "籌碼 = (法人 + 籌碼) ÷ 2。風險燈依技術警示數量：有 Alert 為高、只有 Watch 為中。"}


def verdict(trend, radar_r, risk_r, energy_r, chips, position):
    """總評：依分數組合產生的規則式結論。"""
    t = radar_r["total"]
    stance = "偏強" if t >= 60 and risk_r["level"] != "高" else "偏弱" if t < 40 else "中性"
    reasons = [f"{trend}，綜合分數 {t}（{radar_r['grade']} 級）", f"多方能量 {energy_r['bull']:.0%}",
               f"風險指數 {risk_r['total']}（{risk_r['level']}）"]
    if chips:
        f = streak(chips["institutional"]["foreign"])
        if f:
            reasons.append(f"外資連{'買' if f > 0 else '賣'} {abs(f)} 日")
    top_risk = max(risk_r["dims"], key=lambda x: x["score"])
    note = f"最大風險來源：{top_risk['name']}（{top_risk['score']}）。"
    if position and position.get("dca"):
        note += "這是你的定期定額標的，結論只供參考，不影響扣款紀律。"
    return {"stance": stance, "reasons": reasons, "note": note,
            "explain": "判讀規則：綜合分數 ≥ 60 且風險不是「高」→ 技術面偏強；< 40 → 偏弱；其餘中性。"
                       "這描述的是近期價格走勢的強弱，<b>不是加碼或減碼的建議</b>：技術訊號對未來報酬的預測力很有限"
                       "（見「訊號歷史勝率」與基準的比較）。加減碼請依月報健檢的配置政策決定，定期定額照原設定扣款。"}


def build(s, chips, extra, levels, warnings, trend, position, market_kind):
    out = {"energy": energy(s), "profile": volume_profile(s), "fan": range_fan(s), "backtest": signal_backtest(s)}
    if chips:
        inst = chips["institutional"]
        out["inst_cost"] = {"20": inst_cost(inst, s, 20), "60": inst_cost(inst, s, 60)}
        out["streak"] = {g: streak(inst[g]) for g in ("foreign", "trust", "dealer")}
        out["daytrade"] = extra.get("daytrade_series")
        out["turnover20"] = extra.get("turnover20")
        out["daytrade_ratio20"] = extra.get("daytrade_ratio20")
        out["foreign_ratio"] = extra.get("foreign_ratio_last")
    vw = {}
    for n in (5, 20, 60):
        v = s["volume"][-n:]
        vw[str(n)] = sum(a * b for a, b in zip(v, s["close"][-n:])) / sum(v) if sum(v) else None
    out["vwap"] = vw
    out["intraday_range20"] = sum((h - l) / c for h, l, c in zip(s["high"][-20:], s["low"][-20:], s["close"][-20:])) / 20
    out["radar"] = radar(s, chips, extra, levels, market_kind)
    out["risk"] = risk_radar(s, chips, extra, levels)
    out["lights"] = lights(s, chips, warnings, out["radar"])
    out["verdict"] = verdict(trend, out["radar"], out["risk"], out["energy"], chips, position)
    return out


def guidance(symbol, market_kind, position, snap, cfg, plus, levels, s):
    """依「使用者自己的投資政策」產生的規則式參考，每條附判斷依據。不是持牌投顧的個人化建議。"""
    pol = cfg["policy"]
    cat = cfg.get("categories", {}).get(symbol)
    w = snap["holdings"].get(symbol, {}).get("weight") if snap else None
    dca_plan = next((p for p in cfg["plans"].values() if symbol in p["amounts"]), None)
    items = []

    def add(kind, title, detail):
        items.append({"kind": kind, "title": title, "detail": detail})

    # 1. 角色
    if dca_plan:
        unit = "NT$" if dca_plan["currency"] == "TWD" else "US$"
        add("keep", "定期定額：照原設定持續扣款",
            f"這是你的定額標的（每月 {unit}{dca_plan['amounts'][symbol]:,}）。技術面強弱不影響扣款；"
            "定額的優點就是不用判斷高低點。")
    elif position:
        add("info", "非定額持股：依配置政策檢視，不依技術訊號買賣",
            f"占你的組合 {w:.1%}。" if w is not None else "")
    else:
        add("info", "未持有：新增前先確認是否符合配置政策",
            "先確認它屬於哪一類資產、加入後會不會讓該類別或單一公司超過你的政策上限，再決定是否買進。")

    # 2. 配置政策
    if snap:
        cats = snap["category"]
        if cat == "台股高股息":
            hd = cats.get("台股高股息", 0)
            if hd > pol["max_high_dividend"]:
                add("caution", "高股息類別已超過政策上限，新資金不宜再投入此類",
                    f"高股息 ETF 占組合 {hd:.1%}，政策上限 {pol['max_high_dividend']:.0%}。"
                    "若要調整，可參考 scenarios/rebalance-2026-10.json 的模擬。")
            else:
                add("ok", "高股息類別在政策範圍內", f"占組合 {hd:.1%}（上限 {pol['max_high_dividend']:.0%}）。")
        if market_kind == "US" or (cat or "").startswith("美股"):
            us = snap["market"].get("US", 0)
            if us < pol["min_us_share"]:
                add("candidate", "美股占比低於政策下限，此類是補足配置的候選",
                    f"美股占組合 {us:.1%}，政策下限 {pol['min_us_share']:.0%}。"
                    f"注意美國資產超過 US${pol['us_estate_limit_usd']:,} 會有遺產稅曝險。")
            else:
                add("ok", "美股占比在政策範圍內", f"美股占組合 {us:.1%}（下限 {pol['min_us_share']:.0%}）。")
        if cat in ("台股個股", "台股金融股", "美股個股") or (not cat and market_kind == "TW" and not symbol.startswith("00")):
            lim = pol["max_single_stock"]
            cur = w or 0
            add("caution" if cur > lim else "ok",
                "單一個股超過政策上限" if cur > lim else "單一個股在政策範圍內",
                f"目前占組合 {cur:.1%}，政策上限 {lim:.0%}"
                + (f"；距政策上限尚有 {lim - cur:.1%}（約 NT${(lim - cur) * snap['value_twd']:,.0f}）。" if cur <= lim else "。"))
        lt = snap.get("look_through", {})
        if symbol in ("2330", "0050") and "2330" in lt:
            lim_i = pol.get("max_single_issuer")
            add("info" if lim_i is None else ("caution" if lt["2330"] > lim_i else "ok"),
                "注意台積電的穿透曝險",
                f"直接持有加上經由 0050 持有，台積電占你的組合 {lt['2330']:.1%}"
                + ("（尚未設定上限）。" if lim_i is None else f"（上限 {lim_i:.0%}）。")
                + "加碼 0050 或 2330 都會提高這個比例。")

    # 3. 執行面（只適用一次性買賣）
    risk, bias = plus["risk"], levels.get("bias20") or 0
    k = s["k"][-1]
    if risk["level"] == "高" or abs(bias) > 0.10 or k > 80:
        why = "、".join(x for x in [f"風險指數{risk['level']}" if risk["level"] == "高" else "",
                                    f"月線乖離 {bias:+.1%}" if abs(bias) > 0.10 else "",
                                    f"K 值 {k:.0f}" if k > 80 else ""] if x)
        add("timing", "若有一次性買入計畫，建議分 2–3 批執行",
            f"目前 {why}，短線波動可能較大；分批可降低買在相對高點的影響。定期定額不受影響。")
    if plus["verdict"]["stance"] == "偏弱" and position and not dca_plan:
        add("timing", "技術面偏弱本身不構成賣出理由",
            "賣出應該依配置政策（例如類別超標、單一個股超過上限），或是持有的理由已經改變，而不是依短期走勢。")

    return {"items": items, "plain": plain_summary(items, dca_plan, position, cat),
            "disclaimer": "以上依你在 config.json 設定的投資政策與目前持倉，由規則自動產生，"
                          "不是持牌投資顧問的個人化建議；實際決策請自行判斷，必要時諮詢專業顧問。"}


def plain_summary(items, dca_plan, position, cat):
    """把投資參考翻成一句白話。每個結論都直接對應一條政策規則，不依股價走勢判斷買賣時機。"""
    kinds = {x["kind"] for x in items}
    titles = " ".join(x["title"] for x in items)
    parts = []
    if dca_plan:
        parts.append("這檔照原本的定期定額繼續扣就好，不用額外買或賣")
    elif position:
        if "超過政策上限" in titles:
            parts.append("這檔（或它所屬的類別）已經超過你的政策上限，如果要調整配置，可以優先從這裡減少")
        else:
            parts.append("這檔的比重還在你的政策範圍內，沒有需要調整的理由，不必因為短期漲跌買賣")
    else:
        parts.append("你目前沒有這檔，除非它能補足你缺少的配置，否則不必因為技術面強弱買進")
    hd_over = "高股息類別已超過政策上限" in titles
    if hd_over and not position:
        parts.append("而且這一類你已經超標，先不要新增")
    elif hd_over and dca_plan:
        parts.append("另外這一類你已經超標，定額以外先不要再額外加碼")
    # 非定額持股的超標已在第一句說明，不重複
    if "candidate" in kinds:
        parts.append("美股比重偏低，有閒錢想加碼時，這一類是優先考慮的方向")
    if "注意台積電的穿透曝險" in titles:
        parts.append("加碼前記得台積電在你的組合裡已經占約四分之一")
    if "timing" in kinds and "若有一次性買入計畫" in titles:
        parts.append("如果真的要一次買，分 2–3 次買比較穩")
    return "依你的投資政策，簡單說：" + "；".join(parts) + "。"


# ---------- 美股 ----------

US_INFO = data.DATA / "us_info.json"


def us_info(symbol):
    """美股名稱、產業、市值（FinMind USStockInfo，同一標的取最新一筆）。"""
    cache = json.loads(US_INFO.read_text(encoding="utf-8")) if US_INFO.exists() else {}
    hit = cache.get(symbol)
    if hit and hit.get("_fetched") == date.today().isoformat()[:7]:      # 每月更新一次
        return hit
    rows = data.fetch("USStockInfo", symbol, "2000-01-01")
    if not rows:
        return None
    r = max(rows, key=lambda x: x["date"])
    hit = {"name": r["stock_name"], "sector": r.get("Subsector") or "", "market_cap": r.get("MarketCap") or 0,
           "country": r.get("Country"), "_fetched": date.today().isoformat()[:7]}
    hit.update(fmp_profile(symbol) or {})
    cache[symbol] = hit
    US_INFO.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    return hit


def fmp_profile(symbol):
    """FMP 免費方案的公司／ETF 基本資料（Beta、市值、52 週區間）。沒有金鑰或失敗時回傳 None。"""
    import urllib.request
    key = None
    env = data.ROOT / ".env.local"
    for line in env.read_text().splitlines() if env.exists() else []:
        if line.startswith("FMP_API_KEY="):
            key = line.split("=", 1)[1].strip()
    if not key:
        return None
    try:
        url = f"https://financialmodelingprep.com/stable/profile?symbol={symbol}&apikey={key}"
        with urllib.request.urlopen(url, timeout=20, context=data.SSL_CTX) as r:
            p = (json.load(r) or [None])[0]
    except Exception:
        return None
    if not p:
        return None
    # 新上市標的歷史太短，FMP 回傳 0，不代表與大盤無關
    return {"beta": p.get("beta") or None, "fmp_market_cap": p.get("marketCap"), "range52": p.get("range"),
            "is_etf": p.get("isEtf"), "exchange": p.get("exchange")}


def us_market(today=None):
    """美股大盤：S&P 500、Nasdaq、費城半導體、VIX，加上美元兌台幣匯率。同一天快取一次。"""
    today = today or date.today().isoformat()
    cache = MARKET_CACHE / f"_us_market_{today}.json"
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    start = (date.fromisoformat(today) - timedelta(days=120)).isoformat()
    idx = {}
    for sym, name in (("^GSPC", "S&P 500"), ("^IXIC", "Nasdaq"), ("^SOX", "費城半導體"), ("^VIX", "VIX")):
        rows = [(r["date"], r["Close"]) for r in data.fetch("USStockPrice", sym, start) if r["Close"] > 0]
        cl = [c for _, c in rows]
        idx[sym] = {"name": name, "close": cl[-1] if cl else None, "date": rows[-1][0] if rows else None,
                    "chg": cl[-1] / cl[-2] - 1 if len(cl) > 1 else None,
                    "ma20": sum(cl[-20:]) / 20 if len(cl) >= 20 else None,
                    "ma60": sum(cl[-60:]) / 60 if len(cl) >= 60 else None,
                    "series": {"date": [d for d, _ in rows[-60:]], "close": cl[-60:]}}
    fx = data.fetch("TaiwanExchangeRate", "USD", start)
    fx = [(r["date"], r["spot_buy"]) for r in fx if r["spot_buy"] and r["spot_buy"] > 0]
    spx, ndx, vix = idx["^GSPC"], idx["^IXIC"], idx["^VIX"]
    checks = [("S&P 500 在月線上", spx["ma20"] and spx["close"] > spx["ma20"]),
              ("S&P 500 月線在季線上", spx["ma20"] and spx["ma60"] and spx["ma20"] > spx["ma60"]),
              ("Nasdaq 在月線上", ndx["ma20"] and ndx["close"] > ndx["ma20"]),
              ("VIX 低於 20", vix["close"] is not None and vix["close"] < 20)]
    score = sum(bool(ok) for _, ok in checks)
    v = vix["close"] or 0
    out = {"kind": "US", "date": spx["date"], "indices": idx,
           "vix_level": "偏低（市場平靜）" if v < 15 else "正常" if v < 25 else "偏高（市場緊張）",
           "fx": {"now": fx[-1][1] if fx else None, "date": fx[-1][0] if fx else None,
                  "chg20": fx[-1][1] / fx[-21][1] - 1 if len(fx) > 20 else None},
           "score": score, "state": ["偏弱", "偏弱", "中性", "偏強", "偏強"][score],
           "explain": "美股大盤狀態 = 下列 4 項符合幾項（0–1 偏弱、2 中性、3–4 偏強）：" + "、".join(n for n, _ in checks)
                      + "。目前符合：" + ("、".join(n for n, ok in checks if ok) or "無")
                      + "。VIX 是 S&P 500 選擇權隱含波動率，俗稱恐慌指數；低於 15 偏平靜，高於 25 代表市場緊張。"}
    MARKET_CACHE.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out


def us_extra(rows, s, cfg):
    """美股補充：近 12 個月配息（由還原價反推）、殖利率（稅前／稅後）、以美元與台幣計的報酬差異。"""
    last = s["date"][-1]
    y_ago = (date.fromisoformat(last) - timedelta(days=365)).isoformat()
    divs = []
    for a, b in zip(rows, rows[1:]):
        if a.get("adj") and b.get("adj") and b["date"] > y_ago:
            f0, f1 = a["adj"] / a["close"], b["adj"] / b["close"]
            d = a["close"] * (1 - f0 / f1)
            if d > 0 and d / a["close"] > 0.0005:
                divs.append((b["date"], round(d, 4)))
    c = s["close"][-1]
    dps = sum(d for _, d in divs)
    wht = cfg["tax"]["us_withholding"]
    i0 = next((k for k, d in enumerate(s["date"]) if d >= y_ago), 0)
    r_usd = c / s["close"][i0] - 1
    fx = dict(data.load_series(data.FX_PATH, "spot_buy"))
    fx_now = data.on_or_before(sorted(fx.items()), last)
    fx_then = data.on_or_before(sorted(fx.items()), s["date"][i0])
    r_twd = (1 + r_usd) * (fx_now[1] / fx_then[1]) - 1 if fx_now and fx_then else None
    return {"dividends": divs, "dps12": dps, "yield": dps / c if c else None,
            "yield_net": dps / c * (1 - wht) if c else None, "withholding": wht,
            "ret_usd": r_usd, "ret_twd": r_twd, "fx_now": fx_now[1] if fx_now else None,
            "fx_then": fx_then[1] if fx_then else None, "since": s["date"][i0],
            "explain": f"配息由 FinMind 還原價（Adj_Close）的跳動反推，可能與實際入帳略有差異；稅後殖利率扣除 {wht:.0%} 美國預扣稅。"
                       f"台幣報酬 = (1 + 美元報酬) × (現在匯率 ÷ {s['date'][i0]} 匯率) − 1，"
                       "差距就是匯率變動帶來的影響（台幣升值時，換回台幣的報酬會變少）。"}


def add_us_chip_scores(plus, levels):
    """美股籌碼補進多維度評分、風險雷達和信號燈（用 FINRA 放空資料）。"""
    uc = plus.get("us_chips") or {}
    sv, si = uc.get("short_volume"), uc.get("short_interest")
    if not sv and not si:
        return
    z = (sv or {}).get("z") or 0
    chg = (si or {}).get("chg") or 0
    dtc = (si or {}).get("days_to_cover") or 0
    score = clip(70 - z * 15 - max(0, chg) * 100 - max(0, dtc - 3) * 5)
    plus["radar"]["dims"].append({"name": "籌碼", "score": round(score),
        "explain": f"70 − 放空比例 z 值 {z:+.2f} × 15 − 放空餘額增幅 {chg:+.1%}（只算增加）× 100 − 回補天數超過 3 天的部分 × 5"
                   f"（回補天數 {dtc:.1f}）。"})
    plus["radar"]["total"] = round(sum(d["score"] for d in plus["radar"]["dims"]) / len(plus["radar"]["dims"]))
    t = plus["radar"]["total"]
    plus["radar"]["grade"] = "A" if t >= 80 else "B" if t >= 60 else "C" if t >= 40 else "D"
    plus["radar"]["explain"] = plus["radar"]["explain"].replace("共 4 個維度", "籌碼改用 FINRA 放空資料，共 5 個維度")
    plus["risk"]["dims"].append({"name": "放空壓力", "score": round(clip(max(0, z) * 30 + dtc * 8 + max(0, chg) * 150)),
        "explain": f"放空比例 z 值 {z:+.2f}（只算增加）× 30 + 回補天數 {dtc:.1f} × 8 + 放空餘額增幅 {chg:+.1%}（只算增加）× 150。"})
    plus["risk"]["total"] = round(sum(d["score"] for d in plus["risk"]["dims"]) / len(plus["risk"]["dims"]))
    r = plus["risk"]["total"]
    plus["risk"]["level"] = "高" if r >= 60 else "中" if r >= 35 else "低"
    tone = "bull" if score >= 60 else "neutral" if score >= 40 else "bear"
    plus["lights"]["items"].insert(2, {"name": "籌碼", "tone": tone, "value": round(score)})
