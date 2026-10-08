"""基本面、市場溫度計、事件行事曆、每週摘要的回歸測試：python3 -m unittest discover tests"""
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import events, fundamentals, market, weekly  # noqa: E402


class Fundamentals(unittest.TestCase):
    def test_applicable(self):
        self.assertTrue(fundamentals.applicable("2330"))
        self.assertFalse(fundamentals.applicable("0050"))
        self.assertFalse(fundamentals.applicable("VOO"))

    def test_percentile_and_quantile(self):
        self.assertEqual(fundamentals.percentile([1, 2, 3, 4], 3), 0.75)
        self.assertEqual(fundamentals.quantile([1, 2, 3, 5], 0.5), 2.5)
        self.assertEqual(fundamentals.zone(0.9), "歷史偏高區")

    def test_revenue_yoy_mom_ytd(self):
        rows = [{"revenue_year": y, "revenue_month": m, "revenue": v}
                for y, m, v in [(2025, 1, 100), (2025, 2, 100), (2026, 1, 120), (2026, 2, 150)]]
        r = fundamentals.revenue(rows)
        self.assertAlmostEqual(r["latest"]["yoy"], 0.5)
        self.assertAlmostEqual(r["latest"]["mom"], 0.25)
        self.assertAlmostEqual(r["ytd_yoy"], 0.35)
        self.assertEqual(r["next"], {"ym": "2026-03", "deadline": "2026-04-10"})

    def test_revenue_december_rolls_year(self):
        r = fundamentals.revenue([{"revenue_year": 2026, "revenue_month": 12, "revenue": 1}])
        self.assertEqual(r["next"], {"ym": "2027-01", "deadline": "2027-02-10"})

    def test_valuation_skips_loss_periods(self):
        rows = [{"date": f"2026-01-{d:02d}", "PER": p, "PBR": 1.0, "dividend_yield": 2.0}
                for d, p in [(1, 10), (2, 0), (3, 20), (4, 15)]]
        v = fundamentals.valuation(rows)
        self.assertEqual(v["PER"]["value"], 15)
        self.assertAlmostEqual(v["PER"]["pct"], 2 / 3)        # 0 不列入歷史
        self.assertEqual(v["chart"]["date"], ["2026-01-01", "2026-01-04"])   # 每 5 筆取一點，並補上最後一天


class Market(unittest.TestCase):
    HTML = ('<h4>2026 FOMC Meetings</h4>'
            '<div class="fomc-meeting__month"><strong>January</strong></div><div class="fomc-meeting__date">27-28</div>'
            '<div class="fomc-meeting__month"><strong>Apr/May</strong></div><div class="fomc-meeting__date">30-1*</div>'
            '<div class="fomc-meeting__month"><strong>August</strong></div><div class="fomc-meeting__date">22 (notation vote)</div>'
            '<div class="fomc-meeting__month"><strong>Dec/Jan</strong></div><div class="fomc-meeting__date">31-1</div>')

    def test_parse_fomc(self):
        got = market.parse_fomc(self.HTML)
        self.assertEqual(got, [{"date": "2026-01-28", "sep": False}, {"date": "2026-05-01", "sep": True},
                               {"date": "2027-01-01", "sep": False}])

    def test_light_ranges(self):
        self.assertEqual(market.light_of(41)[0], "紅燈")
        self.assertEqual(market.light_of(32)[0], "黃紅燈")
        self.assertEqual(market.light_of(23)[0], "綠燈")
        self.assertEqual(market.light_of(22)[0], "黃藍燈")
        self.assertEqual(market.light_of(16)[0], "藍燈")

    def test_heat(self):
        self.assertEqual(market._heat(0.9), "偏熱")
        self.assertEqual(market._heat(0.9, invert=True), "偏冷")    # VIX 高 = 恐慌


class Events(unittest.TestCase):
    CFG = {"plans": {"TW": {"day": 27, "amounts": {"0050": 1}}, "US": {"day": 25, "amounts": {"VOO": None}}}}

    def test_calendar(self):
        holdings = [{"market": "TW", "symbol": "2330", "category": "台股個股"},
                    {"market": "TW", "symbol": "2882", "category": "台股金融股"}]
        ev = events.build(self.CFG, holdings, [], today=date(2026, 10, 8), days=60,
                          fomc=[{"date": "2026-10-28", "sep": False}])
        titles = [(e["date"], e["title"]) for e in ev]
        self.assertIn(("2026-10-10", "9 月營收公布期限"), titles)
        self.assertIn(("2026-10-27", "台股定期定額扣款"), titles)
        self.assertNotIn("美股定期定額扣款", [t for _, t in titles])          # 金額未設定
        self.assertIn(("2026-11-14", "2026 年第三季財報公布期限"), titles)   # 一般公司
        self.assertIn(("2026-11-29", "2026 年第三季財報公布期限"), titles)   # 金融業
        self.assertIn(("2026-10-28", "FOMC 利率決議"), titles)
        self.assertEqual(ev, sorted(ev, key=lambda e: e["date"]))

    def test_dividend_events(self):
        div = [{"symbol": "0056", "ex_date": "2026-10-22", "pay_date": "2026-11-11", "per_share": 1.0, "currency": "TWD",
                "status": "已公告", "net_twd": 1000, "drip": True}]
        ev = events.build({"plans": {}}, [], div, today=date(2026, 10, 8), fomc=[])
        self.assertEqual([e["kind"] for e in ev], ["除息", "入帳"])
        self.assertIn("股息再投資", ev[1]["detail"])


class Weekly(unittest.TestCase):
    def test_return_over_window(self):
        self.assertAlmostEqual(weekly._ret([100, 101, 102, 103, 104, 105, 110], n=5), 110 / 101 - 1)
        self.assertAlmostEqual(weekly._ret([100, 110]), 0.1)
        self.assertIsNone(weekly._ret([100]))


if __name__ == "__main__":
    unittest.main()
