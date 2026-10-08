"""長期持有者首頁與股利行事曆的回歸測試：python3 -m unittest discover tests"""
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import data, divcal, home, ledger, risk  # noqa: E402

CFG = {"plans": {"TW": {"currency": "TWD", "day": 27, "amounts": {"0056": 4000}, "drip": ["0056"]},
                 "US": {"currency": "USD", "day": 25, "amounts": {"VOO": 20, "SMH": 80}}},
       "tax": {"tw_nhi_rate": 0.0211, "tw_nhi_threshold": 20000, "us_withholding": 0.3},
       "policy": {"us_estate_limit_usd": 60000, "us_estate_warn": 0.85},
       "splits": {}}
TODAY = date(2026, 10, 8)


class Patched(unittest.TestCase):
    """把資料層換成測試資料；每個測試設定 self.hist / self.announce。"""
    hist, announce = [], []

    def setUp(self):
        self._orig = (data.load_series, data.load_announce, data.us_dividends)
        data.load_series = lambda path, col: list(self.hist)
        data.load_announce = lambda sym: list(self.announce)
        data.us_dividends = lambda sym: list(self.hist)

    def tearDown(self):
        data.load_series, data.load_announce, data.us_dividends = self._orig

    def events(self, market="TW", shares=1000, txns=()):
        return divcal.symbol_events(CFG, market, "0056", shares, list(txns), TODAY)


class DividendCalendar(Patched):
    def test_announced_with_amount(self):
        self.announce = [{"ex_date": "2026-10-22", "pay_date": "2026-11-11", "cash": 1.2}]
        ev = self.events()
        self.assertEqual([(e["status"], e["per_share"], e["pay_date"]) for e in ev], [("已公告", 1.2, "2026-11-11")])

    def test_date_announced_amount_pending_uses_last_cash(self):
        self.hist = [("2026-07-21", 1.35)]
        self.announce = [{"ex_date": "2026-10-22", "pay_date": "2026-11-11", "cash": 0}]
        ev = [e for e in self.events() if e["ex_date"] == "2026-10-22"]
        self.assertEqual((ev[0]["status"], ev[0]["per_share"]), ("日期已公告", 1.35))

    def test_projection_skipped_when_announced_nearby(self):
        """去年 10/23 配息推估到今年 10/23，但今年 10/22 已公告 → 只留已公告那筆。"""
        self.hist = [("2025-10-23", 0.866)]
        self.announce = [{"ex_date": "2026-10-22", "pay_date": "2026-11-11", "cash": 1.0}]
        self.assertEqual([e["status"] for e in self.events()], ["已公告"])

    def test_projection_uses_last_year_and_pay_lag(self):
        self.hist = [("2026-01-22", 0.866)]
        self.announce = [{"ex_date": "2025-07-21", "pay_date": "2025-08-10", "cash": 1.0}]   # 間隔 20 天
        ev = [e for e in self.events() if e["status"] == "推估"]
        self.assertEqual((ev[0]["ex_date"], ev[0]["pay_date"]), ("2027-01-22", "2027-02-11"))

    def test_pending_payment_counts_opening_position(self):
        """建檔前除息、建檔後入帳：期初部位也要算進去。"""
        self.announce = [{"ex_date": "2026-09-16", "pay_date": "2026-10-15", "cash": 1.1}]
        opening = {"symbol": "0056", "date": "2026-10-05", "shares": 2000, "source": "opening"}
        later = {"symbol": "0056", "date": "2026-10-06", "shares": 500, "source": "actual"}
        ev = self.events(txns=[opening, later])
        self.assertEqual((ev[0]["status"], ev[0]["shares"]), ("待入帳", 2000))

    def test_already_paid_is_dropped(self):
        self.announce = [{"ex_date": "2026-07-21", "pay_date": "2026-08-10", "cash": 1.35}]
        self.assertEqual(self.events(), [])

    def test_nhi_and_withholding(self):
        self.announce = [{"ex_date": "2026-10-22", "pay_date": "2026-11-11", "cash": 1.0}]
        holdings = [{"market": "TW", "symbol": "0056", "shares": 30000, "currency": "TWD"},
                    {"market": "TW", "symbol": "00713", "shares": 10000, "currency": "TWD"}]
        cal = divcal.build(CFG, holdings, [], fx_now=32, today=TODAY)
        by = {e["symbol"]: e for e in cal["events"]}
        self.assertEqual((by["0056"]["deduct_label"], by["0056"]["deduct_twd"]), ("補充保費", round(30000 * 0.0211)))
        self.assertEqual(by["00713"]["deduct_twd"], 0)                 # 未達 2 萬
        self.assertTrue(by["0056"]["drip"])
        self.assertEqual(cal["months"][1]["month"], "2026-11")
        self.assertEqual(cal["months"][1]["net_twd"], by["0056"]["net_twd"] + by["00713"]["net_twd"])

    def test_us_withholding(self):
        self.hist = [("2025-12-22", 2.0)]
        cal = divcal.build(CFG, [{"market": "US", "symbol": "VOO", "shares": 1, "currency": "USD"}], [], 30, TODAY)
        e = cal["events"][0]
        self.assertEqual((e["gross_twd"], e["deduct_twd"], e["deduct_label"]), (60, 18, "預扣稅"))


class Benchmark(unittest.TestCase):
    def setUp(self):
        self._orig = (risk._total_return, data.load_series)

    def tearDown(self):
        risk._total_return, data.load_series = self._orig

    def test_same_flows_into_index(self):
        """第 1 天投入 100、第 2 天再投入 100，指數 10 → 20 → 20：市值 = 10 單位 × 20 + 5 單位 × 20。"""
        risk._total_return = lambda cfg, mk, sym: {"2026-01-01": 10.0, "2026-01-02": 20.0, "2026-01-03": 20.0}
        data.load_series = lambda path, col: []
        days = ["2026-01-01", "2026-01-02", "2026-01-03"]
        curve = home.benchmark(CFG, [("2026-01-01", -100), ("2026-01-02", -100)], days, "0050")
        self.assertEqual(curve, [100, 300, 300])

    def test_us_converts_twd(self):
        risk._total_return = lambda cfg, mk, sym: {"2026-01-01": 100.0, "2026-01-02": 110.0}
        data.load_series = lambda path, col: [("2026-01-01", 30.0), ("2026-01-02", 30.0)]
        curve = home.benchmark(CFG, [("2026-01-01", -3000)], ["2026-01-01", "2026-01-02"], "VOO")
        self.assertEqual(curve, [3000, 3300])

    def test_no_price_data(self):
        risk._total_return = lambda cfg, mk, sym: {}
        self.assertIsNone(home.benchmark(CFG, [("2026-01-01", -1)], ["2026-01-01"], "0050"))


class Estate(unittest.TestCase):
    def test_months_to_limit(self):
        r = {"holdings": [{"market": "US", "value": 59000}], "health": []}
        e = home.estate(CFG, r, TODAY)
        self.assertEqual(e["reach"], "2027-08")          # 差 US$1,000，每月 US$100 → 10 個月後
        self.assertIsNone(e["warn_reach"])               # 已超過 85% 警示線


class Growth(unittest.TestCase):
    def test_empty_ledger(self):
        self.assertEqual(home.growth(CFG, [], [])["date"], [])


if __name__ == "__main__":
    unittest.main()
