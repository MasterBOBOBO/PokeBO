"""核心計算的回歸測試：python3 -m unittest discover tests"""
import csv
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import data, ledger, report, scenario, ta  # noqa: E402

CFG = {"plans": {"TW": {"currency": "TWD", "day": 27, "amounts": {"0050": 30000}},
                 "US": {"currency": "USD", "day": 25, "amounts": {"VOO": 20}}},
       "splits": {"0050": [{"date": "2025-06-18", "ratio": 4}]}}


class Indicators(unittest.TestCase):
    def test_sma(self):
        self.assertEqual(ta.sma([1, 2, 3, 4], 2), [None, 1.5, 2.5, 3.5])

    def test_kd_flat_price_stays_50(self):
        k, d = ta.kd([10] * 20, [10] * 20, [10] * 20)
        self.assertAlmostEqual(k[-1], 50)
        self.assertAlmostEqual(d[-1], 50)

    def test_kd_close_at_high_goes_up(self):
        high = [10 + i for i in range(20)]
        k, d = ta.kd(high, [x - 1 for x in high], high)
        self.assertGreater(k[-1], 95)

    def test_macd_flat_is_zero(self):
        dif, m, osc = ta.macd([100.0] * 60)
        self.assertAlmostEqual(dif[-1], 0)
        self.assertAlmostEqual(osc[-1], 0)

    def test_rsi_rising_is_100(self):
        self.assertEqual(ta.rsi([float(i) for i in range(30)])[-1], 100)

    def test_cross_detection(self):
        a, b = [1, 2, 3], [2, 2, 2]
        self.assertEqual(ta._cross(a, b, 2), 1)
        self.assertEqual(ta._cross(b, a, 2), -1)


class Portfolio(unittest.TestCase):
    def test_xirr_ten_percent(self):
        r = report.xirr([("2025-01-01", -100), ("2026-01-01", 110)])
        self.assertAlmostEqual(r, 0.10, places=4)

    def test_xirr_short_period_is_none(self):
        self.assertIsNone(report.xirr([("2026-01-01", -100), ("2026-02-01", 110)]))

    def test_plan_month_holiday_rollover(self):
        self.assertEqual(ledger.plan_month(CFG, "TW", "2025-02-03"), "2025-01")   # 春節順延
        self.assertEqual(ledger.plan_month(CFG, "TW", "2025-02-27"), "2025-02")
        self.assertEqual(ledger.plan_month(CFG, "TW", "2025-01-02"), "2024-12")   # 跨年

    def test_split_factor(self):
        self.assertEqual(ledger.split_factor(CFG, "0050", "2025-01-01", "2026-01-01"), 4)
        self.assertEqual(ledger.split_factor(CFG, "0050", "2025-07-01", "2026-01-01"), 1)
        self.assertEqual(ledger.split_factor(CFG, "0050", "2025-01-01", "2025-06-01"), 1)

    def test_nhi_threshold(self):
        cfg = {"tax": {"tw_nhi_rate": 0.0211, "tw_nhi_threshold": 20000, "us_withholding": 0.3}}
        self.assertEqual(report._net(cfg, "TW", 19999), 19999)
        self.assertAlmostEqual(report._net(cfg, "TW", 20000), 20000 * (1 - 0.0211))
        self.assertAlmostEqual(report._net(cfg, "US", 100), 70)


class UsDividendDerivation(unittest.TestCase):
    def test_dividend_from_adj_close_jump(self):
        """VOO 2025-09-29 實際配息約 1.74，應能由 Adj/Close 比值反推。"""
        rows = [("2025-09-25", 606.59, 597.97), ("2025-09-26", 610.16, 601.49),
                ("2025-09-29", 610.13, 603.18), ("2025-09-30", 612.38, 605.41)]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "US_TEST.csv"
            with path.open("w", newline="") as f:
                w = csv.writer(f)
                w.writerow(data.US_FIELDS)
                for d, c, a in rows:
                    w.writerow([d, c, c, c, c, 0, a])
            orig = data.price_path
            data.price_path = lambda m, s: path
            try:
                divs = data.us_dividends("TEST")
            finally:
                data.price_path = orig
        self.assertEqual(len(divs), 1)
        self.assertEqual(divs[0][0], "2025-09-29")
        self.assertAlmostEqual(divs[0][1], 1.74, delta=0.02)


class ScenarioIsReadOnly(unittest.TestCase):
    def test_simulate_does_not_touch_ledger(self):
        sc_path = data.PRIVATE / "scenarios" / "rebalance-2026-10.json"
        if not ledger.POSITIONS.exists() or not sc_path.exists():
            self.skipTest("需要實際帳本與情境檔")
        before = hashlib.sha256(ledger.POSITIONS.read_bytes()).hexdigest()
        scenario.apply(data.load_config(), json.loads(sc_path.read_text(encoding="utf-8")))
        self.assertEqual(before, hashlib.sha256(ledger.POSITIONS.read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()


class MissingEntries(unittest.TestCase):
    CFG = {"tracking_start": "2026-10-05",
           "plans": {"TW": {"currency": "TWD", "day": 27, "amounts": {"0050": 30000}, "drip": ["0050"]}}}

    def _patch(self, prices, divs=()):
        self._orig = data.load_series
        data.load_series = lambda path, col: list(divs) if "dividends" in str(path) else list(prices)

    def tearDown(self):
        data.load_series = self._orig

    def test_dca_missing_after_trade_day(self):
        from datetime import date
        self._patch([("2026-10-27", 113.0), ("2026-10-28", 114.0)])
        miss = ledger.missing_dca(self.CFG, txns=[], today=date(2026, 10, 28))
        self.assertEqual([(m["symbol"], m["plan_month"]) for m in miss], [("0050", "2026-10")])

    def test_dca_not_missing_when_recorded(self):
        from datetime import date
        self._patch([("2026-10-27", 113.0)])
        t = {"symbol": "0050", "plan_month": "2026-10", "source": "actual", "date": "2026-10-27"}
        self.assertEqual(ledger.missing_dca(self.CFG, txns=[t], today=date(2026, 10, 28)), [])

    def test_dca_not_due_before_trade(self):
        from datetime import date
        self._patch([("2026-10-02", 112.8)])
        self.assertEqual(ledger.missing_dca(self.CFG, txns=[], today=date(2026, 10, 20)), [])

    def test_drip_missing_after_wait(self):
        from datetime import date
        self._patch([], divs=[("2027-01-21", 1.0)])
        self.assertEqual(len(ledger.missing_drip(self.CFG, txns=[], today=date(2027, 3, 1))), 1)
        t = {"symbol": "0050", "source": "drip", "date": "2027-02-20"}
        self.assertEqual(ledger.missing_drip(self.CFG, txns=[t], today=date(2027, 3, 1)), [])
        self.assertEqual(ledger.missing_drip(self.CFG, txns=[], today=date(2027, 2, 1)), [])   # 還沒到期


class Reconcile(unittest.TestCase):
    def test_share_and_cost_diffs(self):
        from lib import reconcile
        txns = [{"symbol": "0050", "market": "TW", "date": "2026-10-05", "shares": 1000, "price": 60, "fee": 0},
                {"symbol": "VOO", "market": "US", "date": "2026-10-05", "shares": 0.5, "price": 600, "fee": 0}]
        broker = {"0050": {"shares": 1265, "cost": None}, "VOO": {"shares": 0.5, "cost": 310}}
        items = {i["symbol"]: i for i in reconcile.compare(CFG, broker, txns)}
        self.assertEqual(items["0050"]["status"], "share")
        self.assertIn("漏記定額扣款", items["0050"]["hint"])
        self.assertEqual(items["VOO"]["status"], "cost")


class Analytics(unittest.TestCase):
    def _series(self, n=80):
        from lib import ta as t
        close = [100 + (i % 10) for i in range(n)]
        s = {"date": [f"2026-01-{i:02d}" for i in range(n)], "close": close, "open": [c - 0.5 for c in close],
             "high": [c + 1 for c in close], "low": [c - 1 for c in close], "volume": [1000.0] * n}
        return s

    def test_streak(self):
        from lib import ta_plus
        self.assertEqual(ta_plus.streak([1, -2, 3, 4]), 2)
        self.assertEqual(ta_plus.streak([5, -1, -1, -1]), -3)
        self.assertEqual(ta_plus.streak([1, 0]), 0)

    def test_volume_profile_conserves_volume(self):
        from lib import ta_plus
        s = self._series()
        vp = ta_plus.volume_profile(s, n=60)
        self.assertAlmostEqual(sum(vp["vol"]), 60 * 1000, delta=60)
        self.assertTrue(vp["va_low"] <= vp["poc"] <= vp["va_high"])

    def test_energy_all_up_days(self):
        from lib import ta_plus
        e = ta_plus.energy(self._series())          # 收 > 開 → 全部是紅 K
        self.assertEqual(e["bull"], 1.0)

    def test_percentile(self):
        from lib import ta_plus
        self.assertEqual(ta_plus.percentile([1, 2, 3, 4, 5], 0.5), 3)
        self.assertEqual(ta_plus.percentile([1, 2, 3, 4, 5], 0.0), 1)

    def test_quiet_then_surge(self):
        from lib import ta as t
        vol = [1000.0] * 30 + [500.0] * 3 + [2000.0]
        close = [100.0] * 33 + [101.0]
        s = {"volume": vol, "close": close}
        v20 = t.sma(vol, 20)
        self.assertTrue(t.quiet_then_surge(s, v20, 33))
        self.assertFalse(t.quiet_then_surge(s, v20, 32))
        s2 = {"volume": vol, "close": close[:-1] + [99.0]}          # 帶量但收跌
        self.assertFalse(t.quiet_then_surge(s2, v20, 33))
        s3 = {"volume": vol[:31] + [900.0, 500.0, 2000.0], "close": close}   # 中間有一天沒量縮
        self.assertFalse(t.quiet_then_surge(s3, t.sma(s3["volume"], 20), 33))
        self.assertFalse(t.quiet_then_surge(s, v20, 5))             # 均量還沒算出來

    def test_volume_state(self):
        from lib import ta as t
        vol = [1000.0] * 30 + [500.0] * 3
        st = t.volume_state({"volume": vol}, t.sma(vol, 20), 32)
        self.assertEqual((st["label"], st["quiet_streak"]), ("量縮", 3))
        vol2 = vol + [3000.0]
        st = t.volume_state({"volume": vol2}, t.sma(vol2, 20), 33)
        self.assertEqual((st["label"], st["quiet_streak"]), ("爆量", 0))
        self.assertIsNone(t.volume_state({"volume": vol}, t.sma(vol, 20), 10))

    def test_tdcc_summarize(self):
        from lib import tdcc
        rows = ["\ufeff資料日期,證券代號,持股分級,人數,股數,占集保庫存數比例%"]
        pcts = [1, 3, 1, 1, 0.5, 0.5, 0.5, 0.5, 1, 1, 1, 1, 1, 1, 85, 0, 100]
        for lv, pc in enumerate(pcts, 1):
            rows.append(f"20261002,2330  ,{lv},{1000 if lv == 17 else 10},0,{pc:.2f}")
        day, by = tdcc.summarize("\n".join(rows))
        s = by["2330"]
        self.assertEqual(day, "20261002")
        self.assertEqual((s["holders"], s["big1000"], s["big400"], s["retail50"]), (1000, 85, 88, 8))
        self.assertEqual((s["p1"], s["p15"], s["n15"]), (1, 85, 10))      # 每一級都存，前端自選門檻

    def test_hints_cover_signal_titles(self):
        from lib import ta as t
        for title in ["KD 黃金交叉（低檔）", "跌破 MA60", "爆量", "量縮後帶量上漲", "月線乖離過大（+12.0%）",
                      "今日量縮後帶量上漲", "收盤低於季線"]:
            self.assertTrue(t.hint(title), title)
        self.assertIsNone(t.hint("目前無警示"))

    def test_inst_cost_weights_buy_days_only(self):
        from lib import ta_plus
        s = {"date": ["d1", "d2", "d3"], "close": [10, 20, 30]}
        inst = {"date": ["d1", "d2", "d3"], "foreign": [100, -50, 300], "trust": [0, 0, 0]}
        c = ta_plus.inst_cost(inst, s, 3)
        self.assertAlmostEqual(c["foreign"]["cost"], (100 * 10 + 300 * 30) / 400)
        self.assertIsNone(c["trust"]["cost"])


class QuotaGuard(unittest.TestCase):
    def test_blocks_near_limit_without_calling_api(self):
        orig_quota, orig_cache = data.quota, dict(data._quota_cache)
        near = {"used": 595, "limit": 600, "pct": 595 / 600, "level": "Free"}
        data._quota_cache.update(t=9e18, v=dict(near))
        data.quota = lambda max_age=30: near
        try:
            with self.assertRaises(data.QuotaError):
                data.fetch("TaiwanStockPrice", "2330", "2026-10-01")
        finally:
            data.quota = orig_quota
            data._quota_cache.clear()
            data._quota_cache.update(orig_cache)


class Guidance(unittest.TestCase):
    CFG = {"policy": {"max_high_dividend": 0.40, "min_us_share": 0.10, "max_single_stock": 0.10,
                      "us_estate_limit_usd": 60000, "max_single_issuer": None},
           "plans": {"TW": {"currency": "TWD", "amounts": {"0056": 4000}}, "US": {"currency": "USD", "amounts": {"VOO": 20}}},
           "categories": {"0056": "台股高股息", "2882": "台股金融股", "VOO": "美股ETF"}}
    SNAP = {"value_twd": 10_000_000, "holdings": {"0056": {"weight": 0.23}, "2882": {"weight": 0.12}},
            "category": {"台股高股息": 0.52}, "market": {"US": 0.004, "TW": 0.996}, "look_through": {}}
    PLUS = {"risk": {"level": "中"}, "verdict": {"stance": "中性"}}
    S = {"k": [50]}

    def kinds(self, sym, mk="TW", pos=None):
        from lib import ta_plus
        g = ta_plus.guidance(sym, mk, pos, self.SNAP, self.CFG, self.PLUS, {"bias20": 0.01}, self.S)
        return [x["kind"] for x in g["items"]], g

    def test_dca_high_dividend_over_limit(self):
        k, _ = self.kinds("0056", pos={"dca": True})
        self.assertEqual(k, ["keep", "caution"])

    def test_us_below_minimum_is_candidate(self):
        k, _ = self.kinds("VOO", "US", pos={"dca": True})
        self.assertIn("candidate", k)

    def test_single_stock_over_limit(self):
        k, g = self.kinds("2882", pos={"dca": False})
        self.assertIn("caution", k)

    def test_never_says_buy_or_sell_amounts(self):
        for sym, mk in (("0056", "TW"), ("VOO", "US"), ("2882", "TW"), ("9999", "TW")):
            _, g = self.kinds(sym, mk, pos=None)
            text = "".join(x["title"] + x["detail"] for x in g["items"])
            for word in ("建議買進", "建議賣出", "加碼到", "應該買", "應該賣"):
                self.assertNotIn(word, text)


class PlainSummary(unittest.TestCase):
    def test_wording_by_role(self):
        from lib import ta_plus
        over = [{"kind": "caution", "title": "高股息類別已超過政策上限，新資金不宜再投入此類", "detail": ""}]
        dca = ta_plus.plain_summary(over, {"amounts": {}}, {"dca": True}, "台股高股息")
        held = ta_plus.plain_summary(over, None, {"dca": False}, "台股高股息")
        new = ta_plus.plain_summary(over, None, None, "台股高股息")
        self.assertIn("定期定額繼續扣", dca)
        self.assertIn("定額以外", dca)
        self.assertNotIn("定額以外", held)          # 非定額持股不提定額
        self.assertIn("先不要新增", new)
        for t in (dca, held, new):
            self.assertTrue(t.startswith("依你的投資政策"))


class USChips(unittest.TestCase):
    def test_short_volume_parse_and_zscore(self):
        from lib import us_chips
        days = [f"2026-09-{d:02d}" for d in range(1, 21)]
        files = {d.replace("-", ""): "Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market\n"
                 f"{d.replace('-', '')}|ABC|{50 if i < 15 else 80}|0|100|Q\n" for i, d in enumerate(days)}
        orig = us_chips._daily_file
        us_chips._daily_file = lambda d: files.get(d)
        orig_prune = us_chips.prune_daily
        us_chips.prune_daily = lambda keep_days=120: None
        try:
            r = us_chips.short_volume("ABC", days)
        finally:
            us_chips._daily_file, us_chips.prune_daily = orig, orig_prune
        self.assertEqual(len(r["ratio"]), 20)
        self.assertAlmostEqual(r["last"], 0.8)
        self.assertGreater(r["z"], 1)          # 最後 5 天放空比明顯高於平常

    def test_symbol_must_match_exactly(self):
        from lib import us_chips
        txt = "Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market\n20260901|SMHX|90|0|100|Q\n20260901|SMH|40|0|100|Q\n"
        orig, orig_prune = us_chips._daily_file, us_chips.prune_daily
        us_chips._daily_file = lambda d: txt
        us_chips.prune_daily = lambda keep_days=120: None
        try:
            r = us_chips.short_volume("SMH", ["2026-09-01"])
        finally:
            us_chips._daily_file, us_chips.prune_daily = orig, orig_prune
        self.assertAlmostEqual(r["ratio"][0], 0.4)   # 不能誤抓 SMHX


class EmptyPortfolio(unittest.TestCase):
    def test_build_without_holdings(self):
        cfg = json.loads((data.ROOT / "config.example.json").read_text(encoding="utf-8"))
        r = report.build(cfg, txns=[])
        self.assertEqual(r["holdings"], [])
        self.assertEqual(r["health"][0]["item"], "尚無持倉")
