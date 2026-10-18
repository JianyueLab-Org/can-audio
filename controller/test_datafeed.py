"""数据源相关的测试：席位频率自动加入、CID→呼号。

    python -m unittest test_datafeed -v      （在 controller 目录下运行）

不联网：数据源用固定的样例，字段名照 can-fsd 的 internal/api/datafeed.go
（`pilots[]`/`controllers[]` 都有 cid、callsign，controllers 另有 frequency、
facility）。
"""

import unittest
from unittest import mock

import datafeed


def feed(controllers=(), pilots=(), atis=()):
    return {"general": {}, "pilots": list(pilots),
            "controllers": list(controllers), "atis": list(atis)}


def controller(cid, callsign, frequency, facility=4):
    return {"cid": str(cid), "callsign": callsign, "frequency": frequency,
            "facility": facility, "rating": 2}


def pilot(cid, callsign):
    return {"cid": str(cid), "callsign": callsign, "name": "某人"}


class OnlinePositionsTest(unittest.TestCase):
    """在线席位一览。列出来是给人点的，所以点不进去的一律不列。"""

    def test_lists_callsign_and_frequency_sorted(self):
        data = feed(controllers=[
            controller(1001, "ZSPD_TWR", "118.000"),
            controller(1002, "ZBAA_APP", "126.750"),
            controller(1003, "ZSSS_GND", "121.900"),
        ])
        self.assertEqual(
            datafeed.online_positions(data),
            [("ZSPD_TWR", 118000, 4),
             ("ZSSS_GND", 121900, 4),
             ("ZBAA_APP", 126750, 4)])

    def test_observers_are_not_listed(self):
        """挂观察员不算在管制，和 controller_for 的判定保持一致。"""
        data = feed(controllers=[
            controller(1001, "ZSPD_OBS", "118.000",
                       facility=datafeed.FACILITY_OBSERVER),
            controller(1002, "ZSPD_TWR", "118.100"),
        ])
        self.assertEqual([c for c, _, _ in datafeed.online_positions(data)],
                         ["ZSPD_TWR"])

    def test_the_no_frequency_placeholder_is_dropped(self):
        """199.998 是 can-fsd 的"没设频率"，拿它拼出来的频道谁也不在。"""
        data = feed(controllers=[controller(1001, "ZSPD_TWR", "199.998")])
        self.assertEqual(datafeed.online_positions(data), [])

    def test_unparsable_frequency_is_dropped(self):
        data = feed(controllers=[controller(1001, "ZSPD_TWR", "不是频率"),
                                 controller(1002, "ZSPD_GND", "121.900")])
        self.assertEqual([c for c, _, _ in datafeed.online_positions(data)],
                         ["ZSPD_GND"])

    def test_two_positions_on_one_frequency_both_stay(self):
        data = feed(controllers=[controller(1001, "ZSPD_TWR", "118.000"),
                                 controller(1002, "ZSPD_TWR_2", "118.000")])
        self.assertEqual(len(datafeed.online_positions(data)), 2)

    def test_empty_and_missing_data(self):
        self.assertEqual(datafeed.online_positions(None), [])
        self.assertEqual(datafeed.online_positions({}), [])
        self.assertEqual(datafeed.online_positions(feed()), [])


class ControllerLookupTest(unittest.TestCase):

    def test_finds_all_my_positions(self):
        data = feed(controllers=[
            controller(1000, "ZSPD_TWR", "118.000"),
            controller(1000, "ZSPD_APP", "121.700"),
            controller(1001, "ZBAA_APP", "126.750"),
        ])
        positions = datafeed.controllers_for("1000", data)
        self.assertEqual([entry["callsign"] for entry in positions],
                         ["ZSPD_TWR", "ZSPD_APP"])

    def test_finds_my_position(self):
        data = feed(controllers=[controller(1000, "ZSPD_TWR", "118.000")])
        entry = datafeed.controller_for("1000", data)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["callsign"], "ZSPD_TWR")

    def test_cid_may_be_int_or_str(self):
        data = feed(controllers=[controller(1000, "ZSPD_TWR", "118.000")])
        self.assertIsNotNone(datafeed.controller_for(1000, data))

    def test_someone_else_is_not_me(self):
        data = feed(controllers=[controller(1001, "ZSPD_TWR", "118.000")])
        self.assertIsNone(datafeed.controller_for("1000", data))

    def test_an_observer_is_not_controlling(self):
        """挂观察员不算上席位，不该给他自动加频率。

        判定和 can-fsd 的 handleQueryATC 一致：facility 要高于观察员。
        """
        data = feed(controllers=[controller(1000, "ZSPD_OBS", "118.000",
                                            facility=datafeed.FACILITY_OBSERVER)])
        self.assertIsNone(datafeed.controller_for("1000", data))

    def test_empty_feed_and_empty_cid(self):
        self.assertIsNone(datafeed.controller_for("1000", feed()))
        self.assertIsNone(datafeed.controller_for("", feed(
            controllers=[controller(1000, "X", "118.000")])))
        self.assertIsNone(datafeed.controller_for("1000", None))


class FrequencyTest(unittest.TestCase):

    def test_normal_frequency(self):
        self.assertEqual(
            datafeed.frequency_khz(controller(1, "X", "128.500")), 128500)

    def test_the_no_frequency_placeholder_is_rejected(self):
        """199.998 是 can-fsd 的"没设频率"。

        拿它拼出来的 FREQ_199998 是个谁也不在的频道，管制员会以为自己在守听。
        """
        self.assertIsNone(
            datafeed.frequency_khz(controller(1, "X", "199.998")))

    def test_junk_is_rejected_rather_than_guessed(self):
        for bad in ("", None, "abc", "0", "999.999", "12.000"):
            self.assertIsNone(
                datafeed.frequency_khz(controller(1, "X", bad)), bad)

    def test_no_entry(self):
        self.assertIsNone(datafeed.frequency_khz(None))


class RosterTest(unittest.TestCase):

    def test_pilots_and_controllers_are_both_in(self):
        data = feed(controllers=[controller(1000, "ZSPD_TWR", "118.000")],
                    pilots=[pilot(2001, "CES2345"), pilot(2002, "CCA101")])
        table = datafeed.roster(data)
        self.assertEqual(table["2001"], "CES2345")
        self.assertEqual(table["1000"], "ZSPD_TWR")

    def test_keys_are_strings(self):
        """Mumble 报上来的用户名是字符串，键不统一就永远查不到。"""
        table = datafeed.roster(feed(pilots=[pilot(2001, "CES2345")]))
        self.assertIn("2001", table)
        self.assertNotIn(2001, table)

    def test_entries_without_a_callsign_are_skipped(self):
        data = feed(pilots=[{"cid": "2001"}, {"callsign": "无 CID"}])
        self.assertEqual(datafeed.roster(data), {})

    def test_empty_feed(self):
        self.assertEqual(datafeed.roster(None), {})
        self.assertEqual(datafeed.roster(feed()), {})


class FetchTest(unittest.TestCase):

    def test_a_browser_user_agent_is_sent(self):
        """数据源前面挡着 Cloudflare，非浏览器 UA 一律 403。

        不带这个头的话线上什么都取不到，而本地是没法用假数据发现这件事的
        ——所以这里直接盯请求头。
        """
        captured = {}

        class Response:
            def read(self):
                return b'{"pilots":[]}'

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(request, timeout=None):
            captured["ua"] = request.get_header("User-agent")
            return Response()

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            datafeed.fetch()
        self.assertTrue(captured["ua"].startswith("Mozilla/"), captured["ua"])

    def test_network_failure_returns_none_instead_of_raising(self):
        """查不到呼号不该影响通话。"""
        def boom(request, timeout=None):
            raise OSError("连不上")

        with mock.patch("urllib.request.urlopen", boom):
            self.assertIsNone(datafeed.fetch())

    def test_garbage_json_returns_none(self):
        class Response:
            def read(self):
                return b"<html>Cloudflare</html>"

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        with mock.patch("urllib.request.urlopen", lambda r, timeout=None: Response()):
            self.assertIsNone(datafeed.fetch())


if __name__ == "__main__":
    unittest.main(verbosity=2)
