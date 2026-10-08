"""新聞與重大訊息的回歸測試：python3 -m unittest discover tests"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import news  # noqa: E402


class News(unittest.TestCase):
    def test_dedupe_merges_sources_and_converts_time(self):
        rows = [{"date": "2026-10-08 02:14:00", "title": "台積電創新高 - UDN", "source": "UDN", "link": "a"},
                {"date": "2026-10-08 02:15:00", "title": "台積電創新高 - udn.com", "source": "udn.com", "link": "b"},
                {"date": "2026-10-08 01:00:00", "title": "外資買超", "source": "Yahoo股市", "link": "c"}]
        got = news.dedupe(rows)
        self.assertEqual(len(got), 2)
        self.assertEqual(got[0], {"time": "2026-10-08 10:14", "title": "台積電創新高", "link": "a", "sources": ["UDN"]})
        self.assertEqual(got[1]["time"], "2026-10-08 09:00")        # UTC → 台灣時間

    def test_roc_time(self):
        self.assertEqual(news._roc("1151007", "70004"), "2026-10-07 07:00")
        self.assertEqual(news._roc("1151007", "658"), "2026-10-07 00:06")

    def test_parse_material_both_markets(self):
        twse = [{"出表日期": "1151008", "發言日期": "1151007", "發言時間": "173000", "公司代號": "2330",
                 "公司名稱": "台積電", "主旨 ": "公告董事會決議\r\n配息", "符合條款": "第14款"}]
        tpex = [{"Date": "1151007", "發言日期": "1151006", "發言時間": "70004", "SecuritiesCompanyCode": "4530",
                 "CompanyName": "天意能創", "主旨": "公告更名"}]
        got = news.parse_material(twse, "上市") + news.parse_material(tpex, "上櫃")
        self.assertEqual(got[0]["subject"], "公告董事會決議 配息")     # 欄位名稱有空白、內容有換行
        self.assertEqual((got[0]["symbol"], got[0]["time"]), ("2330", "2026-10-07 17:30"))
        self.assertEqual((got[1]["symbol"], got[1]["market"]), ("4530", "上櫃"))

    def test_sent_items_are_not_pushed_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            orig = news.SENT
            news.SENT = Path(tmp) / "sent.json"
            try:
                items = [{"key": "k1", "time": "2026-10-07 17:30", "symbol": "2330", "name": "台積電",
                          "subject": "配息", "kind": "重大訊息"}]
                news.mark_sent(items)
                self.assertEqual(news.SENT.read_text(encoding="utf-8"), '["k1"]')
                self.assertIn("2330 台積電【重大訊息】配息", news.push_text(items))
            finally:
                news.SENT = orig


if __name__ == "__main__":
    unittest.main()
