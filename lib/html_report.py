"""月報 HTML：單一檔案、無外部依賴，圖表為內嵌 SVG，支援深淺色。"""
import html
import json
import math
from datetime import date, datetime

from . import data

W, H, PAD_L, PAD_R, PAD_T, PAD_B = 720, 240, 64, 16, 16, 28


def _esc(x):
    return html.escape(str(x))


def _ntd(v, signed=False):
    s = f"{v:+,.0f}" if signed else f"{v:,.0f}"
    return f"NT$ {s}"


def _compact(v):
    a = abs(v)
    if a >= 1e8:
        return f"{v / 1e8:.2f} 億"
    if a >= 1e4:
        return f"{v / 1e4:,.0f} 萬"
    return f"{v:,.0f}"


def _pct(v, signed=True):
    return "—" if v is None else (f"{v * 100:+.1f}%" if signed else f"{v * 100:.1f}%")


def _nice_ticks(lo, hi, n=4):
    span = hi - lo or abs(hi) or 1
    raw = span / n
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        step = m * mag
        if span / step <= n:
            break
    start = (lo // step) * step
    ticks, t = [], start
    while t <= hi + step * 0.001:
        if t >= lo - step * 0.001:
            ticks.append(t)
        t += step
    if ticks[0] > lo:
        ticks.insert(0, ticks[0] - step)
    if ticks[-1] < hi:
        ticks.append(ticks[-1] + step)
    return ticks


def _line_chart(cid, points, fmt_tick, fmt_tip, series_var, baseline_zero=False, marks=()):
    """單一序列折線 + 面積。points: [(date, value)]；hover 由頁面 JS 處理。"""
    vals = [v for _, v in points]
    lo, hi = min(vals), max(vals)
    if baseline_zero:
        hi = max(hi, 0)
    ticks = _nice_ticks(lo, hi)
    if baseline_zero:
        ticks = [t for t in ticks if t <= 0]
    lo, hi = ticks[0], ticks[-1]
    n = len(points)
    iw, ih = W - PAD_L - PAD_R, H - PAD_T - PAD_B

    def x(i):
        return PAD_L + iw * i / (n - 1)

    def y(v):
        return PAD_T + ih * (1 - (v - lo) / (hi - lo))

    path = " ".join(f"{'M' if i == 0 else 'L'}{x(i):.1f},{y(v):.1f}" for i, (_, v) in enumerate(points))
    base = y(0) if baseline_zero else y(lo)
    area = f"{path} L{x(n - 1):.1f},{base:.1f} L{x(0):.1f},{base:.1f} Z"
    grid = "".join(
        f'<line x1="{PAD_L}" x2="{W - PAD_R}" y1="{y(t):.1f}" y2="{y(t):.1f}" class="grid"/>'
        f'<text x="{PAD_L - 8}" y="{y(t) + 4:.1f}" class="tick" text-anchor="end">{_esc(fmt_tick(t))}</text>'
        for t in ticks)
    # 只在每年第一個交易日標年份
    years, xlabels = set(), ""
    for i, (d, _) in enumerate(points):
        if d[:4] not in years:
            years.add(d[:4])
            if i > 0:
                xlabels += f'<text x="{x(i):.1f}" y="{H - 8}" class="tick" text-anchor="middle">{d[:4]}</text>'
    mk = ""
    idx = {d: i for i, (d, _) in enumerate(points)}
    for d, label, place in marks:
        if d in idx:
            i = idx[d]
            v = points[i][1]
            dy = -12 if place == "above" else 20
            mk += (f'<circle cx="{x(i):.1f}" cy="{y(v):.1f}" r="4" class="dot" style="fill:var({series_var})"/>'
                   f'<text x="{x(i):.1f}" y="{y(v) + dy:.1f}" class="annot" '
                   f'text-anchor="middle">{_esc(label)}</text>')
    payload = json.dumps({"d": [p[0] for p in points], "v": [fmt_tip(p[1]) for p in points],
                          "x0": PAD_L, "x1": W - PAD_R, "y": [round(y(p[1]), 1) for p in points]},
                         ensure_ascii=False)
    return f'''<div class="chart" data-chart='{_esc(payload)}' style="--c:var({series_var})">
<svg viewBox="0 0 {W} {H}" role="img" aria-labelledby="{cid}-t">
{grid}<line x1="{PAD_L}" x2="{W - PAD_R}" y1="{base:.1f}" y2="{base:.1f}" class="axis"/>
<path d="{area}" class="area"/><path d="{path}" class="line"/>{mk}{xlabels}
<line class="cross" y1="{PAD_T}" y2="{H - PAD_B}" x1="0" x2="0"/><circle class="hot" r="5" cx="-10" cy="-10"/>
</svg><div class="tip" hidden></div></div>'''


def _bar_rows(rows, fmt):
    """水平條：rows=[(label, share, tooltip)]，單一色。"""
    mx = max(s for _, s, _ in rows) or 1
    out = ""
    for label, share, tip in rows:
        out += (f'<div class="bar-row" title="{_esc(tip)}"><span class="bar-label">{_esc(label)}</span>'
                f'<span class="bar-track"><span class="bar" style="width:{share / mx * 100:.1f}%"></span></span>'
                f'<span class="bar-val">{_esc(fmt(share))}</span></div>')
    return out


def _drift_plot(drift):
    """點圖：每列一個標的，空心 = 扣款比例，實心 = 持倉比例。"""
    mx = max(max(d["target"], d["actual"]) for d in drift)
    mx = min(1.0, (int(mx * 10) + 1) / 10)
    w, row_h, l, r = 720, 44, 72, 72
    h = row_h * len(drift) + 28

    def x(v):
        return l + (w - l - r) * v / mx

    body = ""
    for t in [i / 10 for i in range(0, int(mx * 10) + 1)]:
        body += (f'<line x1="{x(t):.1f}" x2="{x(t):.1f}" y1="0" y2="{h - 24}" class="grid"/>'
                 f'<text x="{x(t):.1f}" y="{h - 6}" class="tick" text-anchor="middle">{t:.0%}</text>')
    for i, d in enumerate(drift):
        cy = row_h * i + row_h / 2
        a, b = sorted((x(d["target"]), x(d["actual"])))
        body += (f'<text x="{l - 12}" y="{cy + 4:.1f}" class="lbl" text-anchor="end">{_esc(d["symbol"])}</text>'
                 f'<line x1="{a:.1f}" x2="{b:.1f}" y1="{cy:.1f}" y2="{cy:.1f}" class="link"/>'
                 f'<circle cx="{x(d["target"]):.1f}" cy="{cy:.1f}" r="6" class="target">'
                 f'<title>{_esc(d["symbol"])} 扣款比例 {d["target"]:.0%}</title></circle>'
                 f'<circle cx="{x(d["actual"]):.1f}" cy="{cy:.1f}" r="6" class="actual">'
                 f'<title>{_esc(d["symbol"])} 持倉比例 {d["actual"]:.1%}</title></circle>'
                 f'<text x="{w - r + 12}" y="{cy + 4:.1f}" class="lbl">{d["diff"] * 100:+.1f}pt</text>')
    return f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="美股定額配置偏離">{body}</svg>'


def _summary_lines(r):
    s, st = r["summary"], r["risk"]["stats"]
    alloc = r["allocation"]
    div_net = sum(d["net_twd"] for d in r["dividend_outlook"])
    lines = [
        f"組合市值 {_ntd(s['value_twd'])}，總損益 {_ntd(s['pnl_twd'], True)}（{_pct(s['pnl_pct'])}）。",
        f"未來一年預估股利淨額約 {_ntd(div_net)}（每月約 {_ntd(div_net / 12)}），"
        f"高股息 ETF 占組合 {_pct(alloc['category'].get('台股高股息', 0), False)}。",
    ]
    if st:
        now = ("目前在歷史高點附近" if st["current_drawdown"] > -0.03
               else f"目前距高點 {_pct(st['current_drawdown'])}")
        lines.append(f"以目前持股回測 {st['start'][:4]}–{st['end'][:4]}，最大回撤 {_pct(st['max_drawdown'])}"
                     f"（以現在市值換算約少 {_ntd(-st['max_drawdown'] * s['value_twd'])}），{now}。")
    lines.append(f"台股占 {_pct(alloc['market'].get('TW', 0), False)}、美股占 "
                 f"{_pct(alloc['market'].get('US', 0), False)}，地理分散是目前最大的結構性風險。")
    return lines


def render(r, cfg):
    s = r["summary"]
    st = r["risk"]["stats"]
    month = r["as_of"][:7]
    div_net = sum(d["net_twd"] for d in r["dividend_outlook"])
    nhi = sum(d["nhi_twd"] for d in r["dividend_outlook"])

    tiles = [
        ("總損益（不含息）", _ntd(s["pnl_twd"], True), _pct(s["pnl_pct"]) + " vs 成本"),
        ("預估年股利（淨）", _ntd(div_net), f"二代健保約 {_ntd(nhi)}"),
        ("回測最大回撤", _pct(st["max_drawdown"]) if st else "—",
         f"{st['peak_date'][:7]} → {st['trough_date'][:7]}" if st else ""),
        ("年化報酬 XIRR", _pct(s["xirr"]) if s["xirr"] is not None else "累積中",
         "自建檔日起算，滿 90 天顯示"),
    ]
    tiles_html = "".join(f'<div class="tile"><div class="t-label">{_esc(a)}</div>'
                         f'<div class="t-value">{_esc(b)}</div><div class="t-sub">{_esc(c)}</div></div>'
                         for a, b, c in tiles)

    health = "".join(f'<tr><td class="st">{_esc(h["status"])}</td><td>{_esc(h["item"])}</td>'
                     f'<td class="muted">{_esc(h["detail"])}</td></tr>' for h in r["health"])

    curve = r["risk"]["backtest_curve"]
    charts = ""
    if curve and st:
        value_chart = _line_chart(
            "bt", curve, _compact, lambda v: _ntd(v), "--series-1",
            marks=[(st["peak_date"], f"高點 {st['peak_date'][:7]}", "above"),
                   (st["trough_date"], f"低點 {_pct(st['max_drawdown'])}", "below")])
        dd_chart = _line_chart("dd", st["drawdown_curve"], lambda v: f"{v * 100 + 0:.0f}%",
                               lambda v: f"{v * 100:.1f}%", "--series-2", baseline_zero=True)
        note = []
        if r["risk"]["proxied"]:
            note.append("、".join(f"{k} 上市前以 {v} 代替" for k, v in r["risk"]["proxied"].items()))
        if r["risk"]["skipped"]:
            note.append("未納入：" + "、".join(r["risk"]["skipped"]) + "（上市未滿回測期間）")
        charts = f'''
<section><h2>如果這組持股放在過去 5 年</h2>
<p class="lede">以目前持股數不變、含息再投入回測，回答「最壞會跌多深、多久回來」。
{st['peak_date']} 高點跌到 {st['trough_date']}，{'於 ' + st['recovered_date'] + ' 回到前高' if st['recovered_date'] else '尚未回到前高'}；年化波動度 {_pct(st['volatility'], False)}。</p>
<h3>組合市值（回測）</h3>{value_chart}
<h3>距前高跌幅</h3>{dd_chart}
<p class="foot">{_esc('；'.join(note))}</p></section>'''

    cat = r["allocation"]["category"]
    alloc_html = _bar_rows([(k, v, f"{k} {v:.1%}") for k, v in cat.items()], lambda v: f"{v:.1%}")

    drift_html = ""
    if r["drift"]:
        drift_html = f'''
<section><h2>美股定額：持倉比例 vs 每月扣款比例</h2>
<p class="lede">每月扣款 {"、".join(f"{k} ${v}" for k, v in cfg["plans"]["US"]["amounts"].items())}。
差距超過 ±{cfg["policy"]["max_drift"] * 100:.0f}pt 會標示，代表過去漲跌或扣款設定變更讓持倉偏離目前的扣款比例。</p>
<div class="legend"><span><i class="k target"></i>扣款比例</span><span><i class="k actual"></i>持倉比例</span></div>
{_drift_plot(r["drift"])}</section>'''

    div_rows = "".join(
        f'<tr><td>{_esc(d["symbol"])}</td><td class="num">{d["times"]}</td>'
        f'<td class="num">{d["gross_twd"]:,}</td><td class="num">{d["nhi_twd"]:,}</td>'
        f'<td class="num">{d["net_twd"]:,}</td><td class="num">{d["yield_on_value"]:.1%}</td></tr>'
        for d in r["dividend_outlook"])

    hold_rows = "".join(
        f'<tr><td>{_esc(h["symbol"])}<span class="sub">{_esc(h["category"])}</span></td>'
        f'<td class="num">{h["shares"]:,.{0 if h["market"] == "TW" else 4}f}</td>'
        f'<td class="num">{h["price"]:,.2f}</td><td class="num">{h["cost_twd"]:,}</td>'
        f'<td class="num">{h["value_twd"]:,}</td>'
        f'<td class="num {"up" if h["pnl_pct"] >= 0 else "down"}">{_pct(h["pnl_pct"])}</td>'
        f'<td class="num">{h["value_twd"] / s["value_twd"]:.1%}</td></tr>'
        for h in sorted(r["holdings"], key=lambda h: -h["value_twd"]))

    summary = "".join(f"<li>{_esc(x)}</li>" for x in _summary_lines(r))

    return f'''<!doctype html>
<html lang="zh-Hant"><head><meta charset="utf-8">
<script>try{{var t=localStorage.getItem("it-theme");if(t==="light"||t==="dark")document.documentElement.dataset.theme=t}}catch(e){{}}</script>
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" type="image/png" sizes="32x32" href="/favicon-32.png?v=2"><link rel="apple-touch-icon" href="/apple-touch-icon.png?v=2">
<title>投資組合月報 {month}</title>
<style>
:root{{color-scheme:dark;--page:#0b0e16;--surface:#141927;--surface-2:#1a2033;--ink:#e8eaf0;--ink-2:#a3abbd;--muted:#6f7890;
--grid:#232b3d;--axis:#2f3850;--ring:rgba(255,255,255,.08);--series-1:#3987e5;--series-2:#d95926;
--up:#e66767;--down:#0ca30c;--accent:#5b8def}}
:root[data-theme="light"]{{color-scheme:light;--page:#f3f4f7;--surface:#ffffff;--surface-2:#eef0f5;--ink:#151a28;--ink-2:#495165;--muted:#6b7387;--grid:#e4e7ee;--axis:#cdd3df;--ring:rgba(15,23,42,.10);--series-1:#2a78d6;--series-2:#d4571f;--up:#d03b3b;--down:#0a7f0a;--accent:#2f6fde}}
@media (prefers-color-scheme:light){{:root:not([data-theme="dark"]){{color-scheme:light;--page:#f3f4f7;--surface:#ffffff;--surface-2:#eef0f5;--ink:#151a28;--ink-2:#495165;--muted:#6b7387;--grid:#e4e7ee;--axis:#cdd3df;--ring:rgba(15,23,42,.10);--series-1:#2a78d6;--series-2:#d4571f;--up:#d03b3b;--down:#0a7f0a;--accent:#2f6fde}}}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--page);color:var(--ink);font:14px/1.6 system-ui,-apple-system,"PingFang TC","Microsoft JhengHei","Segoe UI",sans-serif}}
main{{max-width:880px;margin:0 auto;padding:16px 16px 64px}}
nav.top{{display:flex;flex-wrap:wrap;gap:8px;align-items:center;background:var(--surface);border:1px solid var(--ring);border-radius:12px;padding:12px 16px;margin-bottom:16px}}
nav.top b{{margin-right:auto;font-size:16px}}
nav.top a{{color:var(--ink);text-decoration:none;border:1px solid var(--axis);background:var(--surface-2);border-radius:8px;padding:6px 12px;font-size:14px}}
nav.top a:hover,nav.top button:hover{{border-color:var(--accent)}}
nav.top button{{font:inherit;cursor:pointer;color:var(--ink);border:1px solid var(--axis);background:var(--surface-2);border-radius:8px;padding:6px 12px;font-size:14px}}
@media print{{nav.top{{display:none}}}}
header .eyebrow{{color:var(--muted);font-size:13px}}
h1{{font-size:26px;margin:4px 0 2px}} h2{{font-size:19px;margin:0 0 6px}} h3{{font-size:14px;color:var(--ink-2);margin:20px 0 4px;font-weight:600}}
section{{background:var(--surface);border:1px solid var(--ring);border-radius:12px;padding:20px;margin-top:16px}}
.hero{{font-size:48px;font-weight:600;letter-spacing:-.5px;margin:12px 0 0}}
.hero-sub{{color:var(--ink-2)}}
.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-top:16px}}
.tile{{background:var(--surface);border:1px solid var(--ring);border-radius:12px;padding:14px 16px}}
.t-label{{color:var(--ink-2);font-size:13px}} .t-value{{font-size:22px;font-weight:600;margin:2px 0}} .t-sub{{color:var(--muted);font-size:12px}}
ul.summary{{margin:8px 0 0;padding-left:20px}} ul.summary li{{margin:4px 0}}
.lede{{color:var(--ink-2);margin:0 0 8px}} .foot{{color:var(--muted);font-size:12px;margin:8px 0 0}}
table{{width:100%;border-collapse:collapse;font-size:14px}}
th{{text-align:left;color:var(--muted);font-weight:500;font-size:12px;border-bottom:1px solid var(--axis);padding:6px 8px}}
td{{border-bottom:1px solid var(--grid);padding:8px;vertical-align:top}}
.num{{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}} th.num{{text-align:right}}
.muted{{color:var(--ink-2)}} .st{{white-space:nowrap}} .sub{{display:block;color:var(--muted);font-size:12px}}
.up{{color:var(--up)}} .down{{color:var(--down)}}
.table-wrap{{overflow-x:auto}}
svg{{width:100%;height:auto;display:block;overflow:visible}}
.grid{{stroke:var(--grid);stroke-width:1}} .axis{{stroke:var(--axis);stroke-width:1}}
.tick{{fill:var(--muted);font-size:11px;font-variant-numeric:tabular-nums}}
.annot{{fill:var(--ink-2);font-size:12px}} .lbl{{fill:var(--ink-2);font-size:13px}}
.line{{fill:none;stroke:var(--c);stroke-width:2;stroke-linejoin:round;stroke-linecap:round}}
.area{{fill:var(--c);opacity:.1}}
.dot{{stroke:var(--surface);stroke-width:2}}
.cross{{stroke:var(--axis);stroke-width:1;visibility:hidden}}
.hot{{fill:var(--c);stroke:var(--surface);stroke-width:2;visibility:hidden}}
.chart{{position:relative;touch-action:pan-y}}
.chart.on .cross,.chart.on .hot{{visibility:visible}}
.tip{{position:absolute;top:0;pointer-events:none;background:var(--surface-2);border:1px solid var(--ring);border-radius:8px;
padding:6px 10px;font-size:12px;box-shadow:0 2px 8px rgba(0,0,0,.12);white-space:nowrap}}
.tip b{{display:block;font-size:14px}}
.bar-row{{display:grid;grid-template-columns:96px 1fr 56px;align-items:center;gap:10px;margin:6px 0}}
.bar-label{{color:var(--ink-2);font-size:13px}} .bar-val{{text-align:right;font-variant-numeric:tabular-nums;font-size:13px}}
.bar-track{{height:16px}} .bar{{display:block;height:16px;min-width:2px;background:var(--series-1);border-radius:0 4px 4px 0}}
.legend{{display:flex;gap:16px;font-size:13px;color:var(--ink-2);margin:4px 0 8px}}
.k{{display:inline-block;width:12px;height:12px;border-radius:50%;margin-right:6px;vertical-align:-1px}}
.k.target,circle.target{{fill:var(--surface);stroke:var(--ink-2);stroke-width:2}} .k.target{{border:2px solid var(--ink-2)}}
.k.actual{{background:var(--series-1)}} circle.actual{{fill:var(--series-1);stroke:var(--surface);stroke-width:2}}
.link{{stroke:var(--axis);stroke-width:2}}
@media (max-width:560px){{.hero{{font-size:36px}} .bar-row{{grid-template-columns:80px 1fr 48px}}}}
</style></head><body><main>
<nav class="top"><b>投資組合月報</b><button id="theme-btn" type="button">☀ 淺色</button><a href="/">我的組合</a><a href="/ta">技術分析</a><a href="/reports/">所有報告</a></nav>
<header><div class="eyebrow">投資組合月報 · 資料日 {r["as_of"]} · USD/TWD {r["fx_usd_twd"][1]}</div>
<h1>{month[:4]} 年 {int(month[5:])} 月</h1>
<div class="hero">{_ntd(s["value_twd"])}</div>
<div class="hero-sub">總市值 · 成本 {_ntd(s["cost_twd"])}</div></header>
<div class="tiles">{tiles_html}</div>

<section><h2>重點摘要</h2><ul class="summary">{summary}</ul></section>

<section><h2>健檢</h2><div class="table-wrap"><table>
<thead><tr><th>狀態</th><th>項目</th><th>說明</th></tr></thead><tbody>{health}</tbody></table></div></section>
{charts}
<section><h2>資產配置</h2><p class="lede">占總市值比例</p>{alloc_html}</section>
{drift_html}
<section><h2>未來一年股利現金流（估）</h2>
<p class="lede">以近 12 個月每次配息 × 目前股數估算。台股單筆 ≥ NT$20,000 扣 2.11% 二代健保；美股扣 30% 預扣稅。</p>
<div class="table-wrap"><table><thead><tr><th>標的</th><th class="num">次數</th><th class="num">毛額</th>
<th class="num">補充保費</th><th class="num">淨額</th><th class="num">殖利率</th></tr></thead><tbody>{div_rows}
<tr><td><b>合計</b></td><td></td><td class="num"><b>{sum(d["gross_twd"] for d in r["dividend_outlook"]):,}</b></td>
<td class="num"><b>{nhi:,}</b></td><td class="num"><b>{div_net:,}</b></td><td></td></tr></tbody></table></div></section>

<section><h2>持倉明細</h2><div class="table-wrap"><table><thead><tr><th>標的</th><th class="num">股數</th>
<th class="num">現價</th><th class="num">成本 TWD</th><th class="num">市值 TWD</th><th class="num">報酬</th>
<th class="num">占比</th></tr></thead><tbody>{hold_rows}</tbody></table></div>
<p class="foot">市值以 FinMind T+1 收盤價計算，和券商 App 盤中市值會有差異。台股各檔成本由券商損益反推。</p></section>

<section><h2>風險與取捨</h2><ul class="summary">
<li>本報告只做配置追蹤，不構成買賣建議；調整前先確認稅負（股利所得、二代健保）與交易成本。</li>
<li>回測假設持股數不變、股利再投入，不代表未來報酬；新上市標的以代理標的或排除處理。</li>
<li>美股股利由還原價反推，可能與實際入帳金額略有差異。</li></ul>
<p class="foot">產生時間 {datetime.now():%Y-%m-%d %H:%M} · invest-tracker</p></section>
</main>
<script>
(function(){{
  const btn=document.getElementById('theme-btn'), root=document.documentElement;
  const cur=()=>root.dataset.theme||(matchMedia('(prefers-color-scheme: light)').matches?'light':'dark');
  const paint=()=>{{btn.textContent=cur()==='dark'?'☀ 淺色':'☾ 深色';}};
  btn.onclick=()=>{{const n=cur()==='dark'?'light':'dark';root.dataset.theme=n;try{{localStorage.setItem('it-theme',n)}}catch(e){{}}paint();}};
  matchMedia('(prefers-color-scheme: light)').addEventListener('change',paint); paint();
}})();
document.querySelectorAll('.chart').forEach(el=>{{
  const p=JSON.parse(el.dataset.chart), svg=el.querySelector('svg'), tip=el.querySelector('.tip');
  const cross=el.querySelector('.cross'), hot=el.querySelector('.hot'), n=p.d.length;
  function at(ev){{
    const b=svg.getBoundingClientRect(), vx=(ev.clientX-b.left)/b.width*{W};
    const i=Math.max(0,Math.min(n-1,Math.round((vx-p.x0)/(p.x1-p.x0)*(n-1))));
    const x=p.x0+(p.x1-p.x0)*i/(n-1);
    cross.setAttribute('x1',x);cross.setAttribute('x2',x);hot.setAttribute('cx',x);hot.setAttribute('cy',p.y[i]);
    tip.innerHTML='<span>'+p.d[i]+'</span><b>'+p.v[i]+'</b>';tip.hidden=false;el.classList.add('on');
    const px=x/{W}*b.width, tw=tip.offsetWidth;
    tip.style.left=Math.min(Math.max(px-tw/2,0),b.width-tw)+'px';
    tip.style.top=Math.max(p.y[i]/{H}*b.height-56,0)+'px';
  }}
  svg.addEventListener('pointermove',at);svg.addEventListener('pointerdown',at);
  svg.addEventListener('pointerleave',()=>{{tip.hidden=true;el.classList.remove('on')}});
}});
</script></body></html>'''


def write(r, cfg, out=None):
    path = out or data.ROOT / "reports" / f"{r['as_of'][:7]}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(r, cfg), encoding="utf-8")
    return path
