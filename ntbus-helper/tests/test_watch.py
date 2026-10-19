"""模擬午夜刷新：指定日期開放那晚才查；開放後依指定班次篩座位，額滿時列出其他有位的班次。"""
import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ntbus  # noqa: E402

TODAY = date.today()


def d(n):
    return (TODAY + timedelta(days=n)).strftime("%Y%m%d")


def fake_result(last_n, sessions):
    days = [d(i) for i in range(1, last_n + 1)]
    last = days[-1]
    return {"lastBookingDate": f"{last[:4]}-{last[4:6]}-{last[6:]}", "nearestBookingDate": days[0],
            "dateList": [{"date": x, "settingList": [{"bookingSessionStartTime": t, "status": st, "remainQuantity": n,
                                                       "bookingSessionName": t} for t, (st, n) in sessions.items()]}
                         for x in days]}


SESSIONS = {"07:50": ("FULL", 0), "08:00": ("FULL", 0), "08:45": ("AVAILABLE", 2), "09:00": ("AVAILABLE", 6),
            "13:35": ("AVAILABLE", 12)}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.patches = [mock.patch.object(ntbus, k, v) for k, v in
                        {"DATA": tmp, "OPEN_LOG": tmp / "open_log.jsonl", "LOG": tmp / "ntbus.log",
                         "TARGETS": tmp / "targets.json"}.items()]
        for p in self.patches:
            p.start()
        self.cfg = json.loads((ntbus.ROOT / "config.json").read_text(encoding="utf-8"))
        self.cfg["watch"].update(interval_sec=0, stop_after_min=1, follow_min=0)

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()


class TargetsTest(Base):
    def test_save_validates_and_dedups(self):
        items = ntbus.save_targets(self.cfg, [{"date": "2099-01-02", "route": "9Oqr", "time": "08:00"},
                                              {"date": "20990102", "route": "9Oqr", "time": "08:00"}])
        self.assertEqual(items, [{"date": "20990102", "route": "9Oqr", "time": "08:00"}])
        with self.assertRaises(ValueError):
            ntbus.save_targets(self.cfg, [{"date": "2099-01-02", "route": "XXXX", "time": "08:00"}])
        with self.assertRaises(ValueError):
            ntbus.save_targets(self.cfg, [{"date": "2099-13-02", "route": "9Oqr", "time": "08:00"}])

    def test_past_dates_dropped(self):
        ntbus.TARGETS.write_text(json.dumps([{"date": "20200101", "route": "9Oqr", "time": "08:00"},
                                             {"date": "20990101", "route": "9Oqr", "time": "08:00"}]))
        self.assertEqual([t["date"] for t in ntbus.load_targets()], ["20990101"])

    def test_opens_on(self):
        self.assertEqual(ntbus.opens_on(self.cfg, "20261027"), date(2026, 10, 22))


class GitHubModeTest(Base):
    def test_parse_text(self):
        items = ntbus.parse_targets_text(self.cfg, "2099-10-27 去 08:00\n2099/10/27 回 # 回程\n\n20991028 9Oqr 08:45")
        self.assertEqual(items, [{"date": "20991027", "route": "9Oqr", "time": "08:00"},
                                 {"date": "20991027", "route": "yj9k", "time": "13:35"},
                                 {"date": "20991028", "route": "9Oqr", "time": "08:45"}])
        self.assertEqual(ntbus.parse_targets_text(self.cfg, ntbus.targets_text(self.cfg, items)), items)
        with self.assertRaises(ValueError):
            ntbus.parse_targets_text(self.cfg, "2099-10-27 北")

    def test_short_date_rolls_to_next_year(self):
        past = (TODAY - timedelta(days=1)).strftime("%m%d")
        if past[:2] == "12" and TODAY.month == 1:
            return
        t = ntbus.parse_targets_text(self.cfg, f"{past} 去")[0]
        self.assertEqual(t["date"][:4], str(TODAY.year + 1))

    def test_env_targets_and_quiet_log(self):
        offset = 6 if datetime.now().hour >= 12 else 5
        env = {"GITHUB_ACTIONS": "true", "NTBUS_TARGETS": f"{d(offset)} 去 08:00"}
        with mock.patch.dict("os.environ", env), mock.patch.object(ntbus, "GH", True), \
             mock.patch("builtins.print") as pr, \
             mock.patch.object(ntbus, "query", return_value=fake_result(offset, SESSIONS)), \
             mock.patch.object(ntbus, "notify") as notify, mock.patch("time.sleep"):
            ntbus.watch(self.cfg, now_mode=True)
        self.assertEqual(len(notify.call_args_list), 1)
        printed = " ".join(str(c.args) for c in pr.call_args_list)
        self.assertNotIn(d(offset)[4:6] + "/" + d(offset)[6:], printed)   # log 不出現日期
        self.assertNotIn("08:00", printed)
        self.assertFalse(ntbus.LOG.exists())


class SummaryTest(Base):
    def test_summary(self):
        items = [{"date": d(9), "route": "yj9k", "time": "13:35"}, {"date": d(3), "route": "9Oqr", "time": "08:00"}]
        text = ntbus.targets_summary(self.cfg, items, {"9Oqr": fake_result(5, SESSIONS), "yj9k": fake_result(5, SESSIONS)})
        first, second = text.split("📅")[1:]
        self.assertIn("❌ 08:00 額滿；其他有位：08:45 剩 2", first)          # 已開放：篩座位
        self.assertIn("00:00 開放，到時會自動盯", second)                    # 未開放：開放時間
        self.assertIn("目前沒有指定日期", ntbus.targets_summary(self.cfg, [], {}))


class SyncTest(Base):
    def test_no_token_skips(self):
        with mock.patch.object(ntbus, "_env", return_value=None), mock.patch("urllib.request.urlopen") as op:
            ok, msg = ntbus.sync_github(self.cfg, [])
        self.assertFalse(ok)
        op.assert_not_called()

    def test_patch_then_create(self):
        import urllib.error
        sent = []

        class Resp:
            status = 201
            def __enter__(self): return self
            def __exit__(self, *a): return False

        store = {}

        def op(req, timeout=None, context=None):
            if req.get_method() == "GET":
                r = Resp()
                r.read = lambda: json.dumps(store).encode()
                return r
            sent.append((req.get_method(), req.full_url, json.loads(req.data)))
            if req.get_method() == "PATCH":
                raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, None)
            store.update(sent[-1][2])
            r = Resp()
            r.read = lambda: b"{}"
            return r

        items = [{"date": "20991027", "route": "9Oqr", "time": "08:00"}]
        with mock.patch.object(ntbus, "_env", return_value="tok"), mock.patch("urllib.request.urlopen", side_effect=op):
            ok, msg = ntbus.sync_github(self.cfg, items)
        self.assertTrue(ok, msg)
        self.assertIn("確認讀到 1 筆", msg)
        self.assertEqual([m for m, _, _ in sent], ["PATCH", "POST"])
        self.assertEqual(sent[1][2], {"name": "NTBUS_TARGETS", "value": "2099-10-27 去 08:00"})
        self.assertTrue(sent[1][1].endswith("/repos/MasterBOBOBO/PokeBO/actions/variables"))

    def test_empty_list_uses_comment(self):
        with mock.patch.object(ntbus, "_env", return_value="tok"), mock.patch("urllib.request.urlopen") as op:
            op.return_value.__enter__.return_value.read.return_value = json.dumps({"value": "# 沒有指定日期"}).encode()
            ok, _ = ntbus.sync_github(self.cfg, [])
        self.assertTrue(ok)
        value = json.loads(op.call_args_list[0].args[0].data)["value"]
        self.assertEqual(ntbus.parse_targets_text(self.cfg, value), [])


class WatchTest(Base):
    def run_watch(self, side_effect):
        with mock.patch.object(ntbus, "query", side_effect=side_effect) as q, \
             mock.patch.object(ntbus, "notify") as notify, mock.patch("time.sleep"):
            ntbus.watch(self.cfg, now_mode=True)
        return q, notify

    def test_no_target_tonight_skips_site(self):
        ntbus.save_targets(self.cfg, [{"date": d(9), "route": "9Oqr", "time": "08:00"}])
        q, notify = self.run_watch(lambda *a, **k: fake_result(5, SESSIONS))
        self.assertEqual(q.call_count, 0)
        notify.assert_not_called()

    def test_opens_and_filters_seats(self):
        # now_mode 在中午以後執行時，今晚 00:00 開放的是 明天 + 5 天 = d(6)
        offset = 6 if datetime.now().hour >= 12 else 5
        ntbus.save_targets(self.cfg, [{"date": d(offset), "route": "9Oqr", "time": "08:00"},
                                      {"date": d(offset), "route": "yj9k", "time": "13:35"}])
        calls = {"n": 0}

        def q(route, month=None):
            calls["n"] += 1
            return fake_result(offset - 1 if calls["n"] <= 4 else offset, SESSIONS)

        _, notify = self.run_watch(q)
        self.assertEqual(len(notify.call_args_list), 2)
        bodies = {c.args[1]: c.args[2] for c in notify.call_args_list}
        go = next(b for t, b in bodies.items() if "高鐵台中站→溪頭" in t)
        back = next(b for t, b in bodies.items() if "溪頭→高鐵台中站" in t)
        self.assertIn("❌ 08:00 額滿；其他有位：08:45 剩 2、09:00 剩 6", go)
        self.assertIn("✅ 13:35 可預約，剩 12 位", back)
        self.assertTrue(any(t.startswith("⚠️") and "高鐵台中站→溪頭" in t for t in bodies))
        self.assertIn(f"bookingDate={d(offset)}", notify.call_args_list[0].kwargs["url"])
        rows = [json.loads(l) for l in ntbus.OPEN_LOG.read_text().splitlines()]
        self.assertEqual(len(rows), 2)

    def test_gives_up_with_notice(self):
        offset = 6 if datetime.now().hour >= 12 else 5
        ntbus.save_targets(self.cfg, [{"date": d(offset), "route": "9Oqr", "time": "08:00"}])
        t0 = datetime.now()
        ticks = iter([t0 + timedelta(seconds=20 * i) for i in range(100)])
        with mock.patch.object(ntbus, "datetime", wraps=datetime) as dt:
            dt.now.side_effect = lambda: next(ticks)
            dt.strptime = datetime.strptime
            dt.combine = datetime.combine
            _, notify = self.run_watch(lambda *a, **k: fake_result(offset - 1, SESSIONS))
        self.assertEqual(len(notify.call_args_list), 1)
        self.assertIn("還沒看到新日期", notify.call_args_list[0].args[1])


if __name__ == "__main__":
    unittest.main()
