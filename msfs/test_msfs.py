"""MSFS 版特有部分的单元测试。

    python -m unittest test_msfs -v

不连模拟器、不连服务器、不碰音频。FSD 协议、他机插值那些和 xpc 共用的部分由
xpc/test_xpc.py 覆盖，这里只测换掉的那一层：SimConnect 的单位换算、aircraft.cfg
解析、机型匹配，以及他机注入里那段自己接管的 objectID 关联。
"""

import hashlib
import json
import math
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

# pymumble 要本机的 opus 原生库，这些测试碰不到音频，缺库时放个替身。
try:
    import opuslib  # noqa: F401
except Exception:
    for _name in ("opuslib", "opuslib.api", "opuslib.api.decoder",
                  "opuslib.api.encoder", "opuslib.api.info", "opuslib.exceptions"):
        sys.modules.setdefault(_name, mock.MagicMock())

import aimatch
import altitude as altitude_module
import simlink


class SquawkTest(unittest.TestCase):
    """应答机码在 SimVar 里是 BCD。当十进制读会得到乱码。"""

    def test_common_codes(self):
        self.assertEqual(simlink.bcd_to_squawk(0x1200), 1200)
        self.assertEqual(simlink.bcd_to_squawk(0x2000), 2000)
        self.assertEqual(simlink.bcd_to_squawk(0x7700), 7700)

    def test_leading_zero_is_kept(self):
        self.assertEqual(simlink.bcd_to_squawk(0x0021), 21)

    def test_all_sevens(self):
        self.assertEqual(simlink.bcd_to_squawk(0x7777), 7777)

    def test_zero(self):
        self.assertEqual(simlink.bcd_to_squawk(0x0000), 0)

    def test_non_octal_nibble_is_not_treated_as_bcd(self):
        # 出现 8 或 9 说明这不是 BCD。0x1290 = 4752，本身是个合法八进制码，
        # 那就按十进制照用，别硬套 BCD 解出个乱码。
        self.assertEqual(simlink.bcd_to_squawk(0x1290), 4752)

    def test_garbage_falls_back(self):
        self.assertEqual(simlink.bcd_to_squawk(None), 2000)
        self.assertEqual(simlink.bcd_to_squawk("x"), 2000)

    def test_plain_decimal_in_range_is_accepted(self):
        # 1200 十进制 = 0x4B0，第三个半字节是 11，不是 BCD；但 1200 本身就是
        # 常见的合法应答机码，照用。
        self.assertEqual(simlink.bcd_to_squawk(1200), 1200)

    def test_out_of_range_falls_back(self):
        # 既不是 BCD，十进制也超出 0000-7777，只能给默认值
        self.assertEqual(simlink.bcd_to_squawk(88888), 2000)


class SnapshotTest(unittest.TestCase):
    """SimVar 的角度是弧度，字段名骗人（PLANE_PITCH_DEGREES 也是弧度）。

    snapshot() 的输出必须和 xpc/xplane.py 逐字段一致，否则 fsdpilot 和 voice
    没法原样复用。
    """

    def setUp(self):
        import math
        self.link = simlink.SimLink()
        self.link.values = {
            # Python-SimConnect 按 Degrees 请求经纬度，拿到的已经是度。
            # 这个测试原来喂弧度、断言出度，把错误假设一起钉住了，所以
            # math.degrees 那个 bug 一路绿灯到实飞才暴露。
            "latitude": 31.1434,
            "longitude": 121.805,
            "altitude": 35000.0,
            "agl": 34000.0,
            "groundspeed": 450.0,
            "pitch": math.radians(-2.0),      # SimVar 抬头为负
            "bank": math.radians(5.0),        # SimVar 右坡为负
            "heading": math.radians(271.0),
            "squawk": 0x2000,
            "com1": 121.5, "com2": 118.0,
            "on_ground": 0,
            "gear": 1, "flaps": 40.0, "spoilers": 0,
            "engine_on": 1,
            "light_strobe": 1, "light_nav": 1,
        }

    def test_latitude_passes_through_unconverted(self):
        self.assertAlmostEqual(self.link.snapshot()["latitude"], 31.1434, places=4)

    def test_longitude_passes_through_unconverted(self):
        self.assertAlmostEqual(self.link.snapshot()["longitude"], 121.805, places=4)

    def test_position_stays_inside_the_valid_range(self):
        """经纬度必须落在合法范围内。

        实飞时每个位置包都被回 "Invalid latitude/longitude"：经纬度已经是度，
        又 math.degrees 了一次，31.14 变成 1784.2。这条断言是那次的回归。
        """
        for latitude, longitude in ((31.1434, 121.805), (-33.94, 151.18),
                                    (0.0, 0.0), (89.9, -179.9)):
            self.link.values["latitude"] = latitude
            self.link.values["longitude"] = longitude
            snapshot = self.link.snapshot()
            self.assertTrue(-90 <= snapshot["latitude"] <= 90,
                            f"纬度 {snapshot['latitude']} 越界")
            self.assertTrue(-180 <= snapshot["longitude"] <= 180,
                            f"经度 {snapshot['longitude']} 越界")

    def test_attitude_is_still_converted_from_radians(self):
        # 名字里带 DEGREES 的那几个反而是弧度，这些转换是对的，别一起改掉
        import math
        self.link.values["pitch"] = math.radians(-2.0)
        self.link.values["heading"] = math.radians(271.0)
        snapshot = self.link.snapshot()
        self.assertAlmostEqual(snapshot["pitch"], 2.0, places=3)
        self.assertAlmostEqual(snapshot["heading"], 271.0, places=3)

    def test_pitch_sign_is_flipped(self):
        # SimVar 里抬头是负的，FSD 那边抬头是正的
        self.assertAlmostEqual(self.link.snapshot()["pitch"], 2.0, places=3)

    def test_bank_sign_is_flipped(self):
        self.assertAlmostEqual(self.link.snapshot()["bank"], -5.0, places=3)

    def test_heading_in_degrees(self):
        self.assertAlmostEqual(self.link.snapshot()["heading"], 271.0, places=3)

    def test_heading_wraps(self):
        import math
        self.link.values["heading"] = math.radians(370.0)
        self.assertAlmostEqual(self.link.snapshot()["heading"], 10.0, places=3)

    def test_altitude_already_in_feet(self):
        self.assertEqual(self.link.snapshot()["altitude"], 35000)

    def test_groundspeed_already_in_knots(self):
        self.assertEqual(self.link.snapshot()["groundspeed"], 450)

    def test_squawk_is_decoded(self):
        self.assertEqual(self.link.snapshot()["squawk"], 2000)

    def test_frequency_passes_through(self):
        self.assertEqual(self.link.snapshot()["com1"], 121.5)

    def test_out_of_band_frequency_is_none(self):
        self.link.values["com1"] = 0.0
        self.assertIsNone(self.link.snapshot()["com1"])
        self.link.values["com1"] = 999.0
        self.assertIsNone(self.link.snapshot()["com1"])

    def test_flaps_scaled_to_ratio(self):
        self.assertAlmostEqual(self.link.snapshot()["flaps"], 0.4)

    def test_lights_reported(self):
        lights = self.link.snapshot()["lights"]
        self.assertTrue(lights["strobe_on"])
        self.assertFalse(lights["beacon_on"])

    def test_no_values_means_no_snapshot(self):
        self.assertIsNone(simlink.SimLink().snapshot())

    def test_field_names_match_the_xplane_client(self):
        """和 xpc 共用 fsdpilot/voice，字段名对不上就会静默出错。"""
        required = {"latitude", "longitude", "altitude", "groundspeed",
                    "pitch", "bank", "heading", "squawk", "xpdr_mode",
                    "com1", "com2", "com1_power", "on_ground", "pressure_delta",
                    "network_altitude", "pressure_altitude", "temperature_error",
                    "agl", "velocity_east", "velocity_up", "velocity_north",
                    "pitch_rate", "heading_rate", "bank_rate", "nose_wheel"}
        self.assertTrue(required.issubset(self.link.snapshot()))

    def test_world_velocity_is_converted_to_metres_per_second(self):
        # VELOCITY WORLD X/Y/Z：东/上/北，RequestList.py 按 Feet per second 要
        self.link.values.update({"velocity_east": 100.0, "velocity_up": -10.0,
                                 "velocity_north": 50.0})
        snapshot = self.link.snapshot()
        self.assertAlmostEqual(snapshot["velocity_east"], 30.48)
        self.assertAlmostEqual(snapshot["velocity_up"], -3.048)
        self.assertAlmostEqual(snapshot["velocity_north"], 15.24)

    def test_rotation_rates_follow_the_attitude_signs(self):
        """ROTATION VELOCITY BODY X/Z 和 PITCH/BANK 一样是低头、左坡为正，翻过来。"""
        import math
        self.link.values.update({"pitch_rate": math.radians(-1.0),
                                 "heading_rate": math.radians(3.0),
                                 "bank_rate": math.radians(2.0)})
        snapshot = self.link.snapshot()
        self.assertAlmostEqual(snapshot["pitch_rate"], 1.0)
        self.assertAlmostEqual(snapshot["heading_rate"], 3.0)
        self.assertAlmostEqual(snapshot["bank_rate"], -2.0)

    def test_the_xplane_client_reports_the_same_velocity_fields(self):
        """fsdpilot 是分叉的一对，快速位置包从两边的快照里取同样的键。"""
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "xpc", "xplane.py")
        if not os.path.exists(path):
            self.skipTest("边上没有 xpc 目录")
        with open(path, encoding="utf-8") as f:
            source = f.read()
        for name in ("velocity_east", "velocity_up", "velocity_north",
                     "pitch_rate", "heading_rate", "bank_rate", "nose_wheel"):
            self.assertIn(f'"{name}":', source, name)


class AltitudeFieldParityTest(unittest.TestCase):
    """两边的 snapshot() 都带同样的高度字段，fsdpilot 和状态栏从它们取值。"""

    FIELDS = ("altitude", "network_altitude", "pressure_altitude",
              "temperature_error", "pressure_delta")

    def test_both_clients_report_the_altitude_fields(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "xpc", "xplane.py")
        if not os.path.exists(path):
            self.skipTest("边上没有 xpc 目录")
        with open(path, encoding="utf-8") as f:
            source = f.read()
        link = simlink.SimLink()
        link.values = {"latitude": 31.0, "longitude": 121.0}
        snapshot = link.snapshot()
        for name in self.FIELDS:
            self.assertIn(f'"{name}":', source, name)
            self.assertIn(name, snapshot, name)


class UnitOverrideTest(unittest.TestCase):
    """RequestList.py 给 ROTATION VELOCITY BODY 写的单位是 Feet per second。"""

    def test_the_rotation_rates_are_requested_in_radians_per_second(self):
        class FakeRequest:
            def __init__(self, datum):
                self.definitions = [(datum, b"Feet per second")]

        requests = {name: FakeRequest(name.replace("_", " ").encode())
                    for name in simlink.UNIT_OVERRIDES}
        fake = type("R", (), {"find": lambda _, name: requests.get(name)})()
        simlink.override_units(fake)
        for name in ("ROTATION_VELOCITY_BODY_X", "ROTATION_VELOCITY_BODY_Y",
                     "ROTATION_VELOCITY_BODY_Z"):
            datum, unit = requests[name].definitions[0]
            self.assertEqual(unit, b"Radians per second")
            self.assertEqual(datum, name.replace("_", " ").encode())

    def test_pressure_altitude_is_requested_in_feet(self):
        """RequestList.py 给 PRESSURE_ALTITUDE 写的单位是 Meters。"""
        class FakeRequest:
            def __init__(self):
                self.definitions = [(b"PRESSURE ALTITUDE", b"Meters")]

        request = FakeRequest()
        fake = type("R", (), {"find": lambda _, name: request
                              if name == "PRESSURE_ALTITUDE" else None})()
        with self.assertLogs("sim", "WARNING"):      # 其余几个在假对象里找不到
            simlink.override_units(fake)
        self.assertEqual(request.definitions[0], (b"PRESSURE ALTITUDE", b"Feet"))

    def test_every_override_is_a_simvar_we_read(self):
        self.assertTrue(set(simlink.UNIT_OVERRIDES) <= set(simlink.SIMVARS.values()))

    def test_a_missing_request_is_logged_not_raised(self):
        fake = type("R", (), {"find": lambda _, name: None})()
        with self.assertLogs("sim", "WARNING"):
            simlink.override_units(fake)


class PressureAltitudeTest(unittest.TestCase):
    """网络高度、气压高度和修正量（altitude.msfs_altitudes，照 xPilot）。

    网络高度 = isa_altitude(PRESSURE ALTITUDE, SEA LEVEL PRESSURE)，
    温度误差 = 网络高度 − PLANE ALTITUDE，修正量 = 气压高度 − 网络高度。
    """

    def _link(self, **values):
        link = simlink.SimLink()
        link.values = {"latitude": 31.0, "longitude": 121.0, **values}
        return link.snapshot()

    def test_standard_atmosphere(self):
        snapshot = self._link(altitude=38000.0, pressure_altitude=38000.0,
                              sea_level_pressure=1013.25)
        self.assertEqual(snapshot["altitude"], 38000)
        self.assertEqual(snapshot["network_altitude"], 38000)
        self.assertEqual(snapshot["pressure_altitude"], 38000)
        self.assertEqual(snapshot["temperature_error"], 0)
        self.assertEqual(snapshot["pressure_delta"], 0)

    def test_warm_atmosphere_sends_the_altimeter_altitude(self):
        """FL380、标准海压、比 ISA 暖：真高 39700，位置包报 38000。"""
        snapshot = self._link(altitude=39700.0, pressure_altitude=38000.0,
                              sea_level_pressure=1013.25)
        self.assertEqual(snapshot["altitude"], 39700)
        self.assertEqual(snapshot["network_altitude"], 38000)
        self.assertEqual(snapshot["temperature_error"], -1700)
        self.assertEqual(snapshot["pressure_delta"], 0)

    def test_high_pressure_day(self):
        """海压 1030：FL380 在 ISA 温度下真高约 38334，修正量是负的。"""
        true_altitude = altitude_module.isa_altitude(38000.0, 1030.0)
        snapshot = self._link(altitude=true_altitude, pressure_altitude=38000.0,
                              sea_level_pressure=1030.0)
        self.assertEqual(snapshot["network_altitude"], round(true_altitude))
        self.assertEqual(snapshot["temperature_error"], 0)
        self.assertEqual(snapshot["pressure_altitude"], 38000)
        self.assertAlmostEqual(snapshot["pressure_delta"], -334, delta=1)
        self.assertEqual(snapshot["network_altitude"] + snapshot["pressure_delta"],
                         38000)

    def test_is_the_same_quantity_as_xplane_12(self):
        """X-Plane 12 的 elevation + altimeter_temperature_error 也是拨海压时的读数。"""
        true_altitude, pressure, sea_level = 36500.0, 35000.0, 1003.0
        msfs = altitude_module.msfs_altitudes(true_altitude, sea_level, pressure)
        error = altitude_module.isa_altitude(pressure, sea_level) - true_altitude
        xp12 = altitude_module.xplane_altitudes(true_altitude, sea_level,
                                                pressure, error)
        for a, b in zip(msfs, xp12):
            self.assertAlmostEqual(a, b, places=6)

    def test_without_pressure_altitude_nothing_is_corrected(self):
        snapshot = self._link(altitude=34000.0, sea_level_pressure=1013.25)
        self.assertEqual(snapshot["network_altitude"], 34000)
        self.assertEqual(snapshot["pressure_altitude"], 34000)
        self.assertEqual(snapshot["pressure_delta"], 0)

    def test_without_sea_level_pressure_the_sim_pressure_altitude_is_kept(self):
        snapshot = self._link(altitude=34000.0, pressure_altitude=35000.0)
        self.assertEqual(snapshot["network_altitude"], 34000)
        self.assertEqual(snapshot["pressure_altitude"], 35000)
        self.assertEqual(snapshot["temperature_error"], 0)
        self.assertEqual(snapshot["pressure_delta"], 1000)

    def test_pressure_altitude_read_in_metres_is_ignored(self):
        """RequestList.py 的默认单位是 Meters：38000 ft 读成 11582。"""
        snapshot = self._link(altitude=38000.0, pressure_altitude=11582.4,
                              sea_level_pressure=1013.25)
        self.assertEqual(snapshot["network_altitude"], 38000)
        self.assertEqual(snapshot["pressure_altitude"], 38000)
        self.assertEqual(snapshot["pressure_delta"], 0)

    def test_nothing_reads_the_default_altimeter(self):
        """INDICATED ALTITUDE / KOHLSMAN 是默认 1 号高度表，Fenix、PMDG 不驱动它。"""
        for simvar in simlink.SIMVARS.values():
            self.assertNotIn("INDICATED_ALTITUDE", simvar)
            self.assertNotIn("KOHLSMAN", simvar)

    def test_the_old_kollsman_formula_was_off_by_a_hundred_and_sixty_feet(self):
        """实报：修正生效的地方差 100–200 ft。

        Fenix 拨 STD 飞 FL380，海压 1030 hPa（30.42 inHg），ISA 温度。默认 1 号
        高度表没人拨，停在 QNH 30.42，读数是拨海压时的高度。旧公式
        indicated + (29.92 − Kollsman) × 1000 按 1000 ft/inHg 线性换算，
        在 FL380 少算 165 ft。新路径直接读 PRESSURE ALTITUDE。
        """
        sea_level = 1030.0
        kollsman = sea_level / altitude_module.INHG_TO_HPA
        true_altitude = altitude_module.isa_altitude(38000.0, sea_level)
        indicated = true_altitude          # 默认高度表拨在海压上
        old = indicated + (29.92 - kollsman) * 1000.0
        self.assertTrue(100 <= 38000 - old <= 200, 38000 - old)
        snapshot = self._link(altitude=true_altitude, pressure_altitude=38000.0,
                              sea_level_pressure=sea_level)
        self.assertEqual(snapshot["pressure_altitude"], 38000)
        self.assertEqual(snapshot["network_altitude"] + snapshot["pressure_delta"],
                         38000)


class TransponderModeTest(unittest.TestCase):
    """待机会在管制端把高度和地速一起抹掉，所以只在飞机确实停着时才当真。

    位置包的包头带应答机模式，待机是 `@S`。EuroScope 收到 `@S` 就当这是个没有
    C 模式的目标，标牌上的高度和地速一起空掉——管制员看到的现象是"有的飞机读
    不到速度"。而很多默认机和非精细机根本没把应答机旋钮接到 TRANSPONDER STATE
    上，那个 SimVar 从头到尾停在 1（待机），于是这些飞机全程没有高度和地速。
    """

    def test_online_states_report_mode_c(self):
        """3 开 / 4 高度(C) / 5 地面都是真的在线。"""
        for state in (3, 4, 5):
            self.assertEqual(simlink.xpdr_mode(state, False, 450),
                             simlink.XPDR_ONLINE, f"state={state}")

    def test_a_parked_cold_aircraft_stays_on_standby(self):
        """冷舱停机坪的飞机不该在雷达上是个亮着的 C 模式目标。"""
        for state in (0, 1, 2):
            self.assertEqual(simlink.xpdr_mode(state, True, 0),
                             simlink.XPDR_STANDBY, f"state={state}")

    def test_an_airborne_aircraft_is_never_believed_on_standby(self):
        """这就是回归本身：在飞的飞机报待机，几乎都是机模没接线。"""
        self.assertEqual(simlink.xpdr_mode(1, False, 450), simlink.XPDR_ONLINE)

    def test_a_taxiing_aircraft_is_not_believed_either(self):
        """已经在动了就不算"停着"，地面管制同样要看地速。"""
        self.assertEqual(simlink.xpdr_mode(1, True, 15), simlink.XPDR_ONLINE)

    def test_an_unreadable_simvar_reports_online(self):
        """老机模没有这个 SimVar，沿用旧行为当在线。"""
        self.assertEqual(simlink.xpdr_mode(None, True, 0), simlink.XPDR_ONLINE)
        self.assertEqual(simlink.xpdr_mode("", False, 450), simlink.XPDR_ONLINE)

    def test_snapshot_reports_online_for_an_airborne_standby(self):
        link = simlink.SimLink()
        link.values = {"latitude": 31.0, "longitude": 121.0, "altitude": 34000.0,
                       "groundspeed": 450.0, "on_ground": 0, "xpdr_state": 1}
        self.assertEqual(link.snapshot()["xpdr_mode"], simlink.XPDR_ONLINE)

    def test_snapshot_still_reports_standby_on_the_stand(self):
        link = simlink.SimLink()
        link.values = {"latitude": 31.0, "longitude": 121.0, "altitude": 20.0,
                       "groundspeed": 0.0, "on_ground": 1, "xpdr_state": 1}
        self.assertEqual(link.snapshot()["xpdr_mode"], simlink.XPDR_STANDBY)


class PollResultTest(unittest.TestCase):
    """在主菜单里读不到位置是常态，不该把 SimConnect 连接推倒重来。

    实飞日志里每隔五六秒一条 "SIM OPEN"，就是把"没进飞行"当成"连接断了"。
    """

    def setUp(self):
        self.link = simlink.SimLink()

    def test_three_distinct_results(self):
        self.assertEqual(len({simlink.OK, simlink.NO_DATA, simlink.FAILED}), 3)

    def test_no_data_when_position_is_missing(self):
        self.link._requests = type("R", (), {"get": lambda s, v: None})()
        self.assertIs(self.link._poll(), simlink.NO_DATA)

    def test_failed_when_simconnect_raises(self):
        def boom(self, simvar):
            raise OSError("连接没了")
        self.link._requests = type("R", (), {"get": boom})()
        self.assertIs(self.link._poll(), simlink.FAILED)

    def test_ok_when_position_is_present(self):
        self.link._requests = type("R", (), {"get": lambda s, v: 1.0})()
        self.assertIs(self.link._poll(), simlink.OK)

    def test_no_data_does_not_reopen_the_connection(self):
        # 只有 FAILED 才该走 _close()
        import inspect
        source = inspect.getsource(simlink.SimLink._run)
        no_data_block = source.split("if result is NO_DATA:")[1].split("continue")[0]
        self.assertNotIn("_close()", no_data_block)


class NoDataGraceTest(unittest.TestCase):
    """一轮读不到位置不等于断了。

    实飞日志（msfs-for-can.log，2026-08-08）里刷出 21 次「MSFS 没有数据（是否已
    进入飞行？）」，每次 0～2 秒就恢复，而那段时间飞机一直挂在 FSD 上——读的是
    二十几个 SimVar，其中一次超时就足够把状态翻掉。
    """

    def setUp(self):
        self.link = simlink.SimLink()
        self.states = []
        self.link.on_state = lambda c, m: self.states.append(c)
        # 装成"刚刚读到过位置"的样子
        self.link._connected = True
        self.link.last_update = time.time()

    def test_a_single_missed_round_does_not_report_a_disconnect(self):
        self.link._report_no_data()
        self.assertEqual(self.states, [])
        self.assertTrue(self.link._connected)

    def test_a_gap_longer_than_stale_after_does_report(self):
        self.link.last_update = time.time() - simlink.STALE_AFTER - 0.1
        self.link._report_no_data()
        self.assertEqual(self.states, [False])

    def test_reading_a_position_again_clears_the_grace(self):
        # 连着几轮没读到，但每次都在宽限内；中间真读到一次就重新计时
        for _ in range(5):
            self.link._report_no_data()
        self.link._requests = type("R", (), {"get": lambda s, v: 1.0})()
        self.assertIs(self.link._poll(), simlink.OK)
        self.link._report_no_data()
        self.assertEqual(self.states, [])

    def test_never_having_had_a_position_reports_immediately(self):
        # 在主菜单里从没读到过位置，不该让人对着假的"已连接"等三秒
        self.link.last_update = 0.0
        self.link._report_no_data()
        self.assertEqual(self.states, [False])

    def test_the_grace_matches_the_connected_property(self):
        # 两处判据必须是同一条，否则 connected 说断了而状态还是绿的
        import inspect
        self.assertIn("STALE_AFTER",
                      inspect.getsource(simlink.SimLink._report_no_data))

    def test_position_packets_keep_flowing_during_the_grace(self):
        # 宽限期里 snapshot() 照常给出上一轮的值，网上看不出空档
        self.link.values = {"latitude": 31.1, "longitude": 121.8}
        self.link._report_no_data()
        self.assertEqual(self.link.snapshot()["latitude"], 31.1)


class AircraftCfgTest(unittest.TestCase):
    """aircraft.cfg 是人手写的，格式相当随意。"""

    def setUp(self):
        self.directory = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.directory, ignore_errors=True)

    def _write(self, text, name="aircraft.cfg"):
        path = os.path.join(self.directory, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def test_reads_title_and_type(self):
        models = aimatch.parse_aircraft_cfg(self._write(
            '[GENERAL]\nicao_type_designator = "A20N"\n\n'
            '[FLTSIM.0]\ntitle = "Airbus A320neo Asobo"\nicao_airline = ""\n'))
        self.assertEqual(len(models), 1)
        self.assertEqual(models[0].title, "Airbus A320neo Asobo")
        self.assertEqual(models[0].icao, "A20N")
        self.assertEqual(models[0].airline, "")

    def test_several_liveries_in_one_file(self):
        models = aimatch.parse_aircraft_cfg(self._write(
            '[GENERAL]\nicao_type_designator = "A20N"\n\n'
            '[FLTSIM.0]\ntitle = "A320neo Asobo"\n\n'
            '[FLTSIM.1]\ntitle = "A320neo Air China"\nicao_airline = "CCA"\n'))
        self.assertEqual(len(models), 2)
        self.assertEqual({m.airline for m in models}, {"", "CCA"})
        # 机型码来自 [GENERAL]，每个涂装都该拿到
        self.assertEqual({m.icao for m in models}, {"A20N"})

    def test_entries_without_a_title_are_skipped(self):
        models = aimatch.parse_aircraft_cfg(self._write(
            '[GENERAL]\nicao_type_designator = "B738"\n\n'
            '[FLTSIM.0]\nicao_airline = "CCA"\n\n'
            '[FLTSIM.1]\ntitle = "737 Max"\n'))
        self.assertEqual([m.title for m in models], ["737 Max"])

    def test_duplicate_keys_do_not_break_it(self):
        # configparser 默认会抛，必须 strict=False
        models = aimatch.parse_aircraft_cfg(self._write(
            '[GENERAL]\nicao_type_designator = "B738"\n\n'
            '[FLTSIM.0]\ntitle = "A"\ntitle = "B"\n'))
        self.assertEqual(len(models), 1)

    def test_trailing_comments_and_quotes_stripped(self):
        models = aimatch.parse_aircraft_cfg(self._write(
            '[GENERAL]\nicao_type_designator = "B738" ; 注释\n\n'
            '[FLTSIM.0]\ntitle = "Boeing 738"\n'))
        self.assertEqual(models[0].icao, "B738")

    def test_missing_file_is_not_an_error(self):
        self.assertEqual(
            aimatch.parse_aircraft_cfg(os.path.join(self.directory, "nope.cfg")), [])

    def test_finds_cfgs_in_a_tree(self):
        inner = os.path.join(self.directory, "pkg", "SimObjects",
                             "Airplanes", "A320")
        os.makedirs(inner)
        with open(os.path.join(inner, "aircraft.cfg"), "w") as f:
            f.write('[FLTSIM.0]\ntitle = "x"\n')
        self.assertEqual(len(aimatch.find_aircraft_cfgs(self.directory)), 1)

    def test_texture_directories_are_skipped(self):
        # 贴图目录里没有飞机定义，跳过能省掉大量磁盘遍历
        inner = os.path.join(self.directory, "pkg", "texture.cca")
        os.makedirs(inner)
        with open(os.path.join(inner, "aircraft.cfg"), "w") as f:
            f.write('[FLTSIM.0]\ntitle = "x"\n')
        self.assertEqual(aimatch.find_aircraft_cfgs(self.directory), [])


class RealWorldLayoutTest(unittest.TestCase):
    """这几条都是拿开发机上真实的 MSFS 安装跑出来才发现的。

    合成的 aircraft.cfg 全过，真机上却只扫到 10 个涂装、3 种机型，而且所有飞机
    都被一个 Fenix 的部件配置顶替了。
    """

    def setUp(self):
        self.directory = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.directory, ignore_errors=True)

    def test_usercfg_gives_the_real_package_path(self):
        # 包目录默认在 AppData 下，但装的时候可以改到任何地方。开发机上
        # UserCfg.opt 写的是 D:\MSFS2022（257 个飞机），AppData 下一个都没有。
        packages = os.path.join(self.directory, "MSFS2022")
        os.makedirs(packages)
        cfg = os.path.join(self.directory, "UserCfg.opt")
        with open(cfg, "w", encoding="utf-8") as f:
            f.write('SomeOther "x"\n')
            f.write(f'InstalledPackagesPath "{packages}"\n')
        self.assertEqual(aimatch._packages_from_usercfg(cfg), packages)

    def test_a_junctioned_package_folder_is_still_scanned(self):
        # os.walk 默认不进符号链接，而 Windows 的目录联接在 Python 3.8 之后就是
        # 符号链接——商店版常把 Official 做成 junction，"搬到别的盘再留个
        # junction"也是社区里最普遍的做法。跳过它 = 整个官方机库都扫不到，
        # 只剩 Community 里几个附加件，正是实飞日志里那个 4 涂装 / 0 机型。
        real = os.path.join(self.directory, "elsewhere", "SimObjects",
                            "Airplanes", "A320")
        os.makedirs(real)
        with open(os.path.join(real, "aircraft.cfg"), "w") as f:
            f.write('[GENERAL]\nicao_type_designator = "A20N"\n\n'
                    '[FLTSIM.0]\ntitle = "Airbus A320neo"\n')

        packages = os.path.join(self.directory, "Packages")
        os.makedirs(packages)
        link = os.path.join(packages, "Official")
        try:
            os.symlink(os.path.join(self.directory, "elsewhere"), link,
                       target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("这个环境不让建符号链接")

        found = aimatch.find_aircraft_cfgs(packages)
        self.assertEqual(len(found), 1)
        self.assertEqual(aimatch.ModelSet.load(packages).types, {"A20N"})

    def test_a_symlink_loop_does_not_hang_the_scan(self):
        # 跟着链接走就得自己防环，否则扫盘永远回不来，界面上是"一直在加载"
        tree = os.path.join(self.directory, "pkg")
        os.makedirs(tree)
        try:
            os.symlink(self.directory, os.path.join(tree, "back"),
                       target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("这个环境不让建符号链接")
        self.assertEqual(aimatch.find_aircraft_cfgs(self.directory), [])

    def test_usercfg_separated_by_a_tab(self):
        # 只按单个空格切的话，制表符分隔的写法什么都切不出来，整个安装被漏掉
        packages = os.path.join(self.directory, "MSFS2024")
        os.makedirs(packages)
        cfg = os.path.join(self.directory, "UserCfg.opt")
        with open(cfg, "w", encoding="utf-8") as f:
            f.write(f'InstalledPackagesPath\t"{packages}"\n')
        self.assertEqual(aimatch._packages_from_usercfg(cfg), packages)

    def test_usercfg_path_containing_spaces(self):
        packages = os.path.join(self.directory, "Flight Sim Packages")
        os.makedirs(packages)
        cfg = os.path.join(self.directory, "UserCfg.opt")
        with open(cfg, "w", encoding="utf-8") as f:
            f.write(f'InstalledPackagesPath "{packages}"\n')
        self.assertEqual(aimatch._packages_from_usercfg(cfg), packages)

    def test_store_2024_package_family_is_looked_at(self):
        # 商店版 2024 的包名是 Limitless 不是 FlightSimulator
        import inspect
        source = inspect.getsource(aimatch.default_roots)
        self.assertIn("Microsoft.Limitless_8wekyb3d8bbwe", source)
        self.assertIn("Microsoft.FlightSimulator_8wekyb3d8bbwe", source)

    def test_usercfg_pointing_nowhere_is_ignored(self):
        cfg = os.path.join(self.directory, "UserCfg.opt")
        with open(cfg, "w", encoding="utf-8") as f:
            f.write('InstalledPackagesPath "Z:\\\\does\\\\not\\\\exist"\n')
        self.assertIsNone(aimatch._packages_from_usercfg(cfg))

    def test_missing_usercfg_is_not_an_error(self):
        self.assertIsNone(aimatch._packages_from_usercfg(
            os.path.join(self.directory, "nope.opt")))

    def test_attachments_are_not_aircraft(self):
        # Fenix 在 attachments/ 下放了几十个部件配置，每个都有 [GENERAL] 和
        # title 但没有机型码。当成飞机会污染匹配表。
        inner = os.path.join(self.directory, "pkg", "SimObjects", "Airplanes",
                             "FNX_32X", "attachments", "fnx", "x", "config")
        os.makedirs(inner)
        with open(os.path.join(inner, "aircraft.cfg"), "w") as f:
            f.write('[GENERAL]\nicao_model = "A-319 CFM SL"\n\n'
                    '[FLTSIM.0]\ntitle = "FenixA319 CFM SL"\n')
        self.assertEqual(aimatch.find_aircraft_cfgs(self.directory), [])

    def test_type_designator_with_a_suffix(self):
        # 真机上见过 icao_type_designator = "A359 ULR"
        self.assertEqual(aimatch._clean_icao('"A359 ULR"'), "A359")

    def test_type_designator_normalised(self):
        self.assertEqual(aimatch._clean_icao("a20n"), "A20N")
        self.assertEqual(aimatch._clean_icao(" B738 "), "B738")

    def test_nonsense_type_designator_is_dropped(self):
        # 假机型码进了索引，真正是这个机型的飞机就永远匹配不到了
        self.assertEqual(aimatch._clean_icao("A-319 CFM SL"), "")
        self.assertEqual(aimatch._clean_icao("X"), "")
        self.assertEqual(aimatch._clean_icao(""), "")

    def test_fallback_prefers_a_model_with_a_type(self):
        # 没有机型码的多半是装得不规范的附加件，拿它当所有飞机的替身最难看
        models = aimatch.ModelSet([
            aimatch.Model("某个部件配置"),
            aimatch.Model("Cessna 172", icao="C172"),
        ])
        model, _ = models.match(equipment="ZZZZ")
        self.assertEqual(model.title, "Cessna 172")


class ModelMatchingTest(unittest.TestCase):
    """退化链。最重要的一条：永远要有结果。"""

    def setUp(self):
        self.models = aimatch.ModelSet([
            aimatch.Model("738 Air China", icao="B738", airline="CCA"),
            aimatch.Model("738 China Eastern", icao="B738", airline="CES"),
            aimatch.Model("739 Air China", icao="B739", airline="CCA"),
            aimatch.Model("A320neo Asobo", icao="A20N"),
            aimatch.Model("Cessna 172", icao="C172"),
        ])

    def test_exact_type_and_airline(self):
        model, why = self.models.match(equipment="B738", airline="CES")
        self.assertEqual(model.title, "738 China Eastern")
        self.assertIn("都匹配", why)

    def test_type_only_when_airline_unknown(self):
        self.assertEqual(self.models.match(equipment="B738")[0].icao, "B738")

    def test_unknown_airline_still_matches_type(self):
        model, why = self.models.match(equipment="B738", airline="UAL")
        self.assertEqual(model.icao, "B738")
        self.assertIn("涂装不对", why)

    def test_family_fallback_prefers_right_airline(self):
        model, why = self.models.match(equipment="B737", airline="CCA")
        self.assertEqual(model.airline, "CCA")
        self.assertIn("同族", why)

    def test_neo_variants_are_one_family(self):
        # A320 和 A20N 是同一架飞机的两种代码，必须互相顶替
        model, why = self.models.match(equipment="A320")
        self.assertEqual(model.icao, "A20N")
        self.assertIn("同族", why)

    def test_generic_fallback(self):
        model, why = self.models.match(equipment="A359")
        self.assertIn("通用", why)

    def test_widebody_is_not_replaced_by_a_narrowbody(self):
        """拿 A319 去顶 B777 视觉上差得离谱。

        实测发现的：本机装了 787 和 A350，但没装 777，原来会一路掉到兜底挑中
        一架 A319。同族之后加一级"同类机身"就能救回来。
        """
        models = aimatch.ModelSet([
            aimatch.Model("A319", icao="A319"),        # 排在前面，容易被兜底选中
            aimatch.Model("787-10", icao="B78X"),
        ])
        model, why = models.match(equipment="B77W")
        self.assertEqual(model.icao, "B78X", why)
        self.assertIn("宽体", why)

    def test_narrowbody_substitutes_for_narrowbody(self):
        models = aimatch.ModelSet([
            aimatch.Model("747", icao="B748"),
            aimatch.Model("A319", icao="A319"),
        ])
        model, why = models.match(equipment="B738")
        self.assertEqual(model.icao, "A319", why)
        self.assertIn("窄体", why)

    def test_light_aircraft_not_replaced_by_an_airliner(self):
        models = aimatch.ModelSet([
            aimatch.Model("A319", icao="A319"),
            aimatch.Model("172", icao="C172"),
        ])
        model, why = models.match(equipment="SR22")
        self.assertEqual(model.icao, "C172", why)

    def test_category_beats_the_generic_guess(self):
        """同类机身必须排在「按前缀猜通用机型」前面。

        GENERIC_BY_PREFIX 是两位前缀，A3 / B7 同时盖住窄体和宽体：B77W 猜出
        B738、A359 猜出 A320。通用那级排在前面的话，只要装了 B738 或 A320
        （几乎人人都有），所有宽体都会退成窄体，同类机身那级永远轮不到。

        关键是**装了 B738**——上面那条只装了 A319 和 B78X，通用猜出的 B738
        找不到，自然轮到同类机身，顺序错了也照样通过。
        """
        models = aimatch.ModelSet([
            aimatch.Model("737-800", icao="B738"),
            aimatch.Model("A320neo", icao="A20N"),
            aimatch.Model("787-9", icao="B789"),
        ])
        for want in ("B77W", "B77L", "A359", "A388", "B744"):
            model, why = models.match(equipment=want)
            self.assertEqual(model.icao, "B789",
                             f"{want} 应当顶一架宽体，却拿到 {model.icao}（{why}）")
            self.assertIn("宽体", why)

    def test_category_lookup(self):
        self.assertEqual(aimatch.category_of("B77W"), "宽体")
        self.assertEqual(aimatch.category_of("B738"), "窄体")
        self.assertEqual(aimatch.category_of("CRJ9"), "支线")
        self.assertEqual(aimatch.category_of("C172"), "通航")
        self.assertEqual(aimatch.category_of("ZZZZ"), "")

    def test_categories_do_not_overlap(self):
        # 一个机型落进两类，替身就成了看字典顺序的抽奖
        seen = {}
        for name, types in aimatch.CATEGORIES.items():
            for icao in types:
                self.assertNotIn(icao, seen,
                                 f"{icao} 同时在 {seen.get(icao)} 和 {name}")
                seen[icao] = name

    def test_unknown_type_still_returns_something(self):
        model, why = self.models.match(equipment="ZZZZ")
        self.assertIsNotNone(model, why)

    def test_no_information_still_returns_something(self):
        self.assertIsNotNone(self.models.match()[0])

    def test_empty_set_reports_why(self):
        model, why = aimatch.ModelSet().match(equipment="B738")
        self.assertIsNone(model)
        self.assertIn("没有找到", why)

    def test_rejected_models_are_skipped(self):
        """模拟器拒绝生成过的模型要换一个，不能死磕。

        实飞日志里 CREATE_OBJECT_FAILED 反复出现：匹配挑中的模型建不出来，
        被拉黑之后匹配器还是挑同一个，注入端又因为在黑名单里而跳过——飞机
        永远出不来。
        """
        model, why = self.models.match(equipment="B738", airline="CCA",
                                       exclude={"738 Air China"})
        self.assertNotEqual(model.title, "738 Air China", why)
        self.assertEqual(model.icao, "B738", "还是该给个 738")

    def test_exclusion_falls_through_every_tier(self):
        # 整个机型都被拉黑时，要继续往同族/同类退，而不是直接放弃
        model, why = self.models.match(
            equipment="B738",
            exclude={"738 Air China", "738 China Eastern"})
        self.assertIsNotNone(model, why)
        self.assertNotIn(model.title, ("738 Air China", "738 China Eastern"))

    def test_everything_rejected_returns_nothing(self):
        # 全都建不出来时要明说，别硬塞一个已知会失败的
        titles = {m.title for m in self.models.models}
        model, why = self.models.match(equipment="B738", exclude=titles)
        self.assertIsNone(model)
        self.assertIn("拒绝", why)

    def test_exclusion_is_case_insensitive(self):
        model, _ = self.models.match(equipment="B738", airline="CCA",
                                     exclude={"738 AIR CHINA"})
        self.assertNotEqual(model.title, "738 Air China")

    def test_explicit_title_wins_when_installed(self):
        model, why = self.models.match(equipment="B738", csl="Cessna 172")
        self.assertEqual(model.title, "Cessna 172")

    def test_unknown_csl_name_is_ignored(self):
        # 对方报的多半是 X-Plane 的 CSL 名，这里装不着，应当继续按机型匹配
        model, _ = self.models.match(equipment="B738", airline="CCA",
                                     csl="BB_A320_CCA")
        self.assertEqual(model.title, "738 Air China")

    def test_lowercase_input(self):
        self.assertEqual(
            self.models.match(equipment="b738", airline="ces")[0].title,
            "738 China Eastern")

    def test_models_without_a_type_are_not_indexed(self):
        # 没有 icao_type_designator 的飞机进不了索引，但一架带机型码的都没有时
        # 仍然要拿它兜底——看不见的飞机比涂装错的飞机危险得多
        models = aimatch.ModelSet([aimatch.Model("怪飞机")])
        model, why = models.match(equipment="B738")
        self.assertEqual(model.title, "怪飞机")
        self.assertIn("没有带机型码", why)


class FlightPlanTest(unittest.TestCase):
    """$FP 的字段布局。协议层和 xpc 共用同一份 fsdpilot.py。"""

    def setUp(self):
        import fsdpilot
        self.fsdpilot = fsdpilot
        self.sent = []
        self.pilot = fsdpilot.FSDPilot("example.invalid", "CCA1501", "1", "pw")
        self.pilot._send = lambda packet: self.sent.append(packet) or True

    def test_field_count(self):
        # can-fsd 的 minimumFields 要求 17 段
        self.pilot.file_flight_plan({})
        self.assertEqual(len(self.sent[0].split(":")), 17)

    def test_identifies_as_msfs_not_xplane(self):
        # 这份是从 xpc 复制来的，连它报 X-Plane 的编号一起带了过来
        self.assertEqual(self.fsdpilot.SIMULATOR,
                         self.fsdpilot.SIMULATOR_MSFS_2020)
        self.assertEqual(self.fsdpilot.CLIENT_NAME, "MSFS for CAN")


class DotCommandTest(unittest.TestCase):
    """`.wallop` 在客户端翻成发往 `*S` 的 #TM。

    协议层这一份是从 xpc 复制来的，两边会各自漂移，所以这里直接测**本目录**
    的那一份，而不是指望 test_xpc.py 替它把关——上面
    `test_identifies_as_msfs_not_xplane` 记着的就是复制没跟上的那次。

    缺陷本身是静默的：原来 `.wallop 求助` 会被当成普通正文，跟着收件人框
    （空的时候是 COM1 频率）发到频率上，服务端的 handleWallop 一次都不会触
    发，而界面照样回一行"已发送"。
    """

    def setUp(self):
        import fsdpilot
        self.fsdpilot = fsdpilot

    def test_wallop_goes_to_the_supervisor_channel(self):
        recipient, body = self.fsdpilot.parse_dot_command(".wallop 请求协助")
        self.assertEqual(recipient, self.fsdpilot.WALLOP_RECIPIENT)
        self.assertEqual(body, "请求协助")

    def test_command_name_is_case_insensitive(self):
        recipient, _ = self.fsdpilot.parse_dot_command(".WALLOP help")
        self.assertEqual(recipient, self.fsdpilot.WALLOP_RECIPIENT)

    def test_colons_in_the_body_survive(self):
        # 分帧要洗的冒号归 sanitize 管，解析这一步不该先把正文切断。
        _, body = self.fsdpilot.parse_dot_command(".wallop ETA 12:30")
        self.assertEqual(body, "ETA 12:30")

    def test_wallop_with_no_text_yields_an_empty_body(self):
        recipient, body = self.fsdpilot.parse_dot_command(".wallop")
        self.assertEqual(recipient, self.fsdpilot.WALLOP_RECIPIENT)
        self.assertEqual(body, "")

    def test_ordinary_message_is_untouched(self):
        recipient, body = self.fsdpilot.parse_dot_command("request pushback")
        self.assertIsNone(recipient)
        self.assertEqual(body, "request pushback")

    def test_unknown_dot_command_is_sent_as_text(self):
        # 吞掉一条本该发出去的消息，比把一句奇怪的话发到频率上更糟。
        recipient, body = self.fsdpilot.parse_dot_command(".wallpo 求助")
        self.assertIsNone(recipient)
        self.assertEqual(body, ".wallpo 求助")


class InjectorTest(unittest.TestCase):
    """他机注入。真正跑要 SimConnect，这里只测不依赖模拟器的那部分。"""

    def setUp(self):
        import inject
        self.inject = inject

    def test_position_definition_field_count(self):
        # 写进去的结构体字段数必须和数据定义一致，错位飞机会跑到地球另一边
        self.assertEqual(len(self.inject._Definition.FIELDS), 7)

    def test_no_unsettable_simvar_in_the_definition(self):
        """SIM ON GROUND 不可写，混进定义会让整条 SetDataOnSimObject 失败。

        后果和跳板没换一样：飞机建在初始位置之后再也不动，日志一片干净。
        """
        names = [name for name, _ in self.inject._Definition.FIELDS]
        self.assertNotIn(b"SIM ON GROUND", names)

    def test_move_negates_pitch_and_bank(self):
        """写回模拟器时俯仰和滚转要取负。

        FSD 抬头为正，MSFS 的 PLANE PITCH DEGREES 低头为正（simlink 读的时候
        就取了负）。不翻回来的话，进近的飞机在别人模拟器里全程俯冲。
        """
        written = []

        class Dll:
            @staticmethod
            def SetDataOnSimObject(handle, definition, object_id, a, b, size, values):
                written.append(list(values))
                return 0

        injector = self.inject.TrafficInjector(sim=None)
        injector.sim = type("S", (), {"dll": Dll, "hSimConnect": None})()
        injector._move(1, {"latitude": 30.0, "longitude": 120.0,
                           "altitude": 5000, "pitch": 10.0, "bank": 25.0,
                           "heading": 90.0, "groundspeed": 140})
        self.assertEqual(written[0][3], -10.0, "俯仰没有取负")
        self.assertEqual(written[0][4], -25.0, "滚转没有取负")
        self.assertEqual(written[0][5], 90.0, "航向不该动")

    def test_definition_fields_are_bytes(self):
        # ctypes 的 c_char_p 只吃 bytes，写成 str 会在运行时才炸
        for name, unit in self.inject._Definition.FIELDS:
            self.assertIsInstance(name, bytes)
            self.assertIsInstance(unit, bytes)

    def test_unavailable_without_simconnect(self):
        # 模拟器没开时构造不该抛，只是标记不可用
        injector = self.inject.TrafficInjector(sim=None)
        self.assertFalse(injector.available)

    def test_sync_is_a_noop_when_unavailable(self):
        injector = self.inject.TrafficInjector(sim=None)
        injector.sync([{"callsign": "CES2345", "latitude": 0, "longitude": 0,
                        "altitude": 0, "model": "x"}])
        self.assertEqual(injector.aircraft, {})

    def test_bad_titles_are_not_retried(self):
        """建不出来的模型不该每轮都再试一次。

        实测日志里 CREATE_OBJECT_FAILED 反复出现，而且包自带的报错只有一句
        枚举名，不说是哪架飞机、哪个模型，完全没法查。
        """
        injector = self.inject.TrafficInjector(sim=None)
        injector.available = True
        injector.bad_titles.add("坏模型")
        calls = []
        injector.sim = type("S", (), {"dll": None, "hSimConnect": None})()
        injector._enums = None
        injector._create("CES1003", {"latitude": 0, "longitude": 0,
                                     "altitude": 0}, "坏模型")
        self.assertEqual(injector.aircraft, {}, "拉黑的模型不该再尝试")

    def test_exception_codes_come_from_the_enum(self):
        # CREATE_OBJECT_FAILED 是 22；按"排第 12 位"猜会得到 TOO_MANY_REQUESTS
        import inspect
        source = inspect.getsource(self.inject.TrafficInjector._note_exception)
        self.assertIn("SIMCONNECT_EXCEPTION_CREATE_OBJECT_FAILED", source)
        self.assertNotIn("= 12", source)

    def test_requested_titles_are_remembered_for_diagnostics(self):
        # 出错时要说得出是哪个模型，否则日志没法查
        injector = self.inject.TrafficInjector(sim=None)
        self.assertIsInstance(injector._requested_titles, dict)

    def _fake_simconnect(self):
        """照着 Python-SimConnect 的形状做一个替身。

        关键是复制它那个**构造时就把方法包成 ctypes 跳板**的做法
        （SimConnect.py:140 的 `my_dispatch_proc_rd = dll.DispatchProc(...)`），
        收消息的循环调的是跳板而不是属性（同文件 181 行）。不复制这一点，
        这条测试就测不出真问题。
        """
        test = self

        class Dll:
            @staticmethod
            def DispatchProc(func):
                # 真的 ctypes 会包一层，这里只要"包的是当时那个函数"这个语义
                return ("trampoline", func)

        class Sim:
            def __init__(self):
                self.dll = Dll()
                self.hSimConnect = object()
                self.calls = []
                self.my_dispatch_proc = self.original
                self.my_dispatch_proc_rd = self.dll.DispatchProc(
                    self.my_dispatch_proc)

            def original(self, pData, cbData, pContext):
                self.calls.append("original")

            def deliver(self, pData):
                """模拟那个循环：调跳板里存的那个函数。"""
                return self.my_dispatch_proc_rd[1](pData, 0, None)

        return Sim()

    def test_the_dispatch_hook_replaces_the_trampoline_not_just_the_attribute(self):
        """只改 `my_dispatch_proc` 属性的话，我们这一层永远不会被调用。

        Python-SimConnect 在 __init__ 里就把原方法包成了 ctypes 跳板，收消息的
        循环调的是跳板。实测（v2.0.3 的日志）的后果：10 次创建请求、**0 个对象
        号**、0 次移除、0 条警告——他机生成在初始位置之后就不动了，离线也删不
        掉，机型问到后重新匹配又再建一架。
        """
        injector = self.inject.TrafficInjector(sim=None)
        sim = self._fake_simconnect()
        injector.sim = sim
        injector.available = True
        injector._enums = type("E", (), {
            "SIMCONNECT_RECV_ID": type("R", (), {
                "SIMCONNECT_RECV_ID_ASSIGNED_OBJECT_ID": 12,
                "SIMCONNECT_RECV_ID_EXCEPTION": 9})(),
        })()

        injector._install_dispatch()

        self.assertIsNot(sim.my_dispatch_proc_rd[1], sim.original,
                         "跳板还指着原方法——循环调的就是它，我们这层等于没装")
        self.assertIs(sim.my_dispatch_proc_rd[1], sim.my_dispatch_proc,
                      "跳板和属性必须是同一个函数")

    def test_an_assigned_object_id_reaches_our_table(self):
        """走完整条路：循环调跳板 → 我们记下 requestID→objectID → 交回原处理。

        记不下来的话 `record["object_id"]` 永远是 None，`_sync_one` 每轮停在
        "还在等 objectID"，他机就再也不动了。
        """
        import ctypes

        injector = self.inject.TrafficInjector(sim=None)
        sim = self._fake_simconnect()
        injector.sim = sim
        injector.available = True

        class Body(ctypes.Structure):
            _fields_ = [("dwID", ctypes.c_uint32),
                        ("dwRequestID", ctypes.c_uint32),
                        ("dwObjectID", ctypes.c_uint32)]

        injector._enums = type("E", (), {
            "SIMCONNECT_RECV_ID": type("R", (), {
                "SIMCONNECT_RECV_ID_ASSIGNED_OBJECT_ID": 12,
                "SIMCONNECT_RECV_ID_EXCEPTION": 9})(),
            "SIMCONNECT_RECV_ASSIGNED_OBJECT_ID": Body,
        })()
        injector._install_dispatch()

        message = Body(dwID=12, dwRequestID=10001, dwObjectID=4242)
        sim.deliver(ctypes.pointer(message))

        self.assertEqual(injector._assigned.get(10001), 4242,
                         "对象号没有被记下来")
        self.assertEqual(sim.calls, ["original"],
                         "记完之后必须原样交回包自己的处理")

    def _fake_injector(self, removed):
        """一个不碰 SimConnect 的注入器，只记下 AIRemoveObject 调了哪些对象。"""
        injector = self.inject.TrafficInjector(sim=None)
        injector.available = True

        class Dll:
            @staticmethod
            def AIRemoveObject(handle, object_id, request_id):
                removed.append(object_id)
                return 0

        injector.sim = type("S", (), {"dll": Dll, "hSimConnect": None})()
        return injector

    def test_an_aircraft_removed_before_its_id_arrives_is_still_deleted(self):
        """号码回来晚了的飞机也必须删掉，否则会永远停在天上。

        AICreateNonATCAircraft 只是把请求发出去，objectID 是异步回来的。飞机
        刚建好就飞出范围时，remove() 那一刻 object_id 还是 None——可模拟器里
        它是真的存在的。不补这一刀的话，它会以最后的位置一直停在那儿，而且
        我们连它的号码都不再记得，只能重启模拟器。
        """
        removed = []
        injector = self._fake_injector(removed)
        injector.aircraft["CES2345"] = {"object_id": None, "title": "738",
                                        "request_id": 10001}
        injector._pending[10001] = "CES2345"

        injector.remove("CES2345")
        self.assertEqual(removed, [], "号码还没回来，这时候删不了")

        # 号码现在到了
        injector._assigned[10001] = 4242
        injector._collect_assigned()
        self.assertEqual(removed, [4242], "已经不要的飞机没有被补删，会变成幽灵")
        self.assertNotIn(10001, injector._assigned, "补删之后不该再留着")
        self.assertNotIn(10001, injector._orphaned)

    def test_a_late_id_for_a_live_aircraft_is_claimed_not_deleted(self):
        """正常情况不能误删：还要着的飞机，号码回来就该认领。"""
        removed = []
        injector = self._fake_injector(removed)
        injector.aircraft["CCA101"] = {"object_id": None, "title": "320",
                                       "request_id": 10002}
        injector._pending[10002] = "CCA101"
        injector._assigned[10002] = 77

        injector._collect_assigned()
        self.assertEqual(removed, [], "这架还要着，不该删")
        self.assertEqual(injector.aircraft["CCA101"]["object_id"], 77)

    def test_cap_leaves_headroom(self):
        # 每架都是完整的飞机模型，放太多会掉帧
        self.assertLessEqual(self.inject.MAX_AIRCRAFT, 64)
        self.assertGreater(self.inject.MAX_AIRCRAFT, 0)

    def _exception_enums(self):
        return type("E", (), {
            "SIMCONNECT_EXCEPTION": type("X", (), {
                "SIMCONNECT_EXCEPTION_CREATE_OBJECT_FAILED": 22,
                "SIMCONNECT_EXCEPTION_OBJECT_OUTSIDE_REALITY_BUBBLE": 30,
                "SIMCONNECT_EXCEPTION_OBJECT_CONTAINER": 31,
            })(),
        })()

    def _exception_body(self, code):
        return type("B", (), {"dwException": code})()

    def test_one_failure_does_not_blacklist_the_whole_fleet(self):
        """EXCEPTION 对不上是哪次请求，一次失败不能把同一轮的模型全拉黑。

        实测场景：15 架同时创建，其中一架的第三方涂装坏了——原来的写法把
        15 个模型全部永久拉黑，一分钟内整个机队被错杀，只能重启客户端。
        """
        injector = self.inject.TrafficInjector(sim=None)
        injector._enums = self._exception_enums()
        injector._requested_titles = {1: "好模型A", 2: "坏模型", 3: "好模型B"}
        injector._pending = {}
        injector._note_exception(self._exception_body(22))
        self.assertEqual(injector.bad_titles, set(),
                         "多个模型在场时一次失败不该拉黑任何一个")
        # 连着失败 BLACKLIST_AFTER 次才算数
        for _ in range(self.inject.BLACKLIST_AFTER - 1):
            injector._requested_titles = {9: "坏模型", 10: "好模型A"}
            injector._note_exception(self._exception_body(22))
        self.assertIn("坏模型", injector.bad_titles)

    def test_a_lone_failure_is_blacklisted_immediately(self):
        # 同一轮里只有一个模型时可以直接指认
        injector = self.inject.TrafficInjector(sim=None)
        injector._enums = self._exception_enums()
        injector._requested_titles = {1: "坏模型"}
        injector._pending = {}
        injector._note_exception(self._exception_body(22))
        self.assertIn("坏模型", injector.bad_titles)

    def test_a_success_clears_the_suspicion(self):
        # 失败计数只数连续的：建成过一次就洗清
        injector = self.inject.TrafficInjector(sim=None)
        injector._enums = self._exception_enums()
        injector._requested_titles = {1: "模型甲", 2: "模型乙"}
        injector._pending = {}
        injector._note_exception(self._exception_body(22))
        injector.aircraft["CCA101"] = {"object_id": None, "title": "模型甲",
                                       "request_id": 5}
        injector._pending[5] = "CCA101"
        injector._assigned[5] = 42
        injector.sim = type("S", (), {
            "dll": type("D", (), {"AIRemoveObject":
                                  staticmethod(lambda *a: 0)}),
            "hSimConnect": None})()
        injector._collect_assigned()
        self.assertNotIn("模型甲", injector._title_failures)

    def test_reality_bubble_does_not_tear_down_pending_creations(self):
        """位置太远只是暂时的，不能把同一轮里正在建的别的飞机全丢掉。

        原来的写法每次 OUTSIDE_REALITY_BUBBLE 都清空 _pending 并删掉所有还
        没拿到号的记录——traffic_range_nm 默认 60 nm 远超加载气泡，这条异常
        是常态，附近真正要看的飞机被拖着反复拆了又建。
        """
        injector = self.inject.TrafficInjector(sim=None)
        injector._enums = self._exception_enums()
        injector._requested_titles = {1: "模型甲"}
        injector._pending = {1: "CES2345"}
        injector.aircraft["CES2345"] = {"object_id": None, "title": "模型甲",
                                        "request_id": 1}
        injector._note_exception(self._exception_body(30))
        self.assertIn("CES2345", injector.aircraft)
        self.assertIn(1, injector._pending)
        self.assertEqual(injector.bad_titles, set())

    def test_a_creation_that_never_answers_times_out_and_retries(self):
        """objectID 等太久不回来的记录要放弃重来，不能永远停在"还在等"。"""
        import time as time_module
        removed = []
        injector = self._fake_injector(removed)
        injector.aircraft["CES2345"] = {
            "object_id": None, "title": "738", "request_id": 10001,
            "requested_at": time_module.time() - self.inject.PENDING_TIMEOUT - 1}
        injector._pending[10001] = "CES2345"
        injector._collect_assigned()
        self.assertNotIn("CES2345", injector.aircraft,
                         "超时的记录该丢掉，让下一轮重建")
        self.assertIn(10001, injector._orphaned,
                      "万一模拟器其实建出来了，号码回来时要补删")


class TakeControlTest(unittest.TestCase):
    """建好的 AI 飞机要冻结并释放 AI 控制，否则两次写入之间 MSFS 自己推它。

    实测 v2.2.12：他机"一卡卡，然后抽搐"——AI 的飞行模型在两次
    SetDataOnSimObject 之间把飞机往别处带，下一次写入再拽回来。
    """

    def setUp(self):
        import ctypes
        import inject
        self.inject = inject
        self.ctypes = ctypes

    def _fake(self, map_result=0, transmit_result=0):
        """照 Python-SimConnect 的形状做的替身，记下每一次 DLL 调用。

        dispatch 跳板在构造时就包好（SimConnect.py:140），收消息走跳板。
        """
        ctypes = self.ctypes
        calls = []
        packets = iter(range(500, 10000))

        class Dll:
            @staticmethod
            def DispatchProc(func):
                return ("trampoline", func)

            @staticmethod
            def AddToDataDefinition(*args):
                return 0

            @staticmethod
            def MapClientEventToSimEvent(handle, event_id, name):
                calls.append(("map", event_id, name))
                if isinstance(map_result, Exception):
                    raise map_result
                return map_result

            @staticmethod
            def TransmitClientEvent(handle, object_id, event_id, data, group, flags):
                calls.append(("transmit", object_id, event_id, data, group, flags))
                return transmit_result

            @staticmethod
            def AIReleaseControl(handle, object_id, request_id):
                calls.append(("release", object_id, request_id))
                return 0

            @staticmethod
            def AICreateNonATCAircraft(handle, title, tail, init, request_id):
                calls.append(("create", title, request_id))
                return 0

            @staticmethod
            def AIRemoveObject(handle, object_id, request_id):
                calls.append(("remove", object_id))
                return 0

            @staticmethod
            def SetDataOnSimObject(*args):
                calls.append(("move", args[2]))
                return 0

            @staticmethod
            def GetLastSentPacketID(handle, pointer):
                pointer._obj.value = next(packets)
                return 0

        class Sim:
            def __init__(self):
                self.dll = Dll()
                self.hSimConnect = "handle"
                self.my_dispatch_proc = self.original
                self.my_dispatch_proc_rd = self.dll.DispatchProc(self.my_dispatch_proc)

            def original(self, pData, cbData, pContext):
                pass

            def deliver(self, message):
                return self.my_dispatch_proc_rd[1](ctypes.pointer(message), 0, None)

        class Assigned(ctypes.Structure):
            _fields_ = [("dwID", ctypes.c_uint32),
                        ("dwRequestID", ctypes.c_uint32),
                        ("dwObjectID", ctypes.c_uint32)]

        class Init(ctypes.Structure):
            _fields_ = [(name, ctypes.c_double) for name in
                        ("Latitude", "Longitude", "Altitude", "Pitch", "Bank",
                         "Heading")] + [("OnGround", ctypes.c_uint32),
                                        ("Airspeed", ctypes.c_uint32)]

        class Enums:
            SIMCONNECT_RECV_ID = type("R", (), {
                "SIMCONNECT_RECV_ID_ASSIGNED_OBJECT_ID": 12,
                "SIMCONNECT_RECV_ID_EXCEPTION": 9})()
            SIMCONNECT_RECV_ASSIGNED_OBJECT_ID = Assigned
            SIMCONNECT_DATA_INITPOSITION = Init
            SIMCONNECT_DATATYPE = type("T", (), {"SIMCONNECT_DATATYPE_FLOAT64": 4})()
            SIMCONNECT_UNUSED = 0xFFFFFFFF

        sim = Sim()
        injector = self.inject.TrafficInjector(sim=None)
        injector.sim = sim
        injector._enums = Enums
        injector._install_dispatch()
        injector._define_position()
        injector._map_freeze_events()
        injector.available = True
        self.Assigned = Assigned
        return injector, sim, calls

    def _entry(self, model):
        return {"callsign": "CES2345", "latitude": 31.2, "longitude": 121.5,
                "altitude": 5000.0, "pitch": 2.0, "bank": 0.0, "heading": 90.0,
                "groundspeed": 200, "on_ground": False, "model": model,
                "equipment": "B738"}

    def _assign(self, injector, sim, calls, object_id):
        request_id = [c for c in calls if c[0] == "create"][-1][2]
        sim.deliver(self.Assigned(dwID=12, dwRequestID=request_id,
                                  dwObjectID=object_id))

    def _control(self, calls, object_id):
        return [c for c in calls
                if c[0] in ("transmit", "release") and c[1] == object_id]

    def test_lights_are_switched_on_change_only(self):
        """他机的灯走 *_SET 事件发给那一架；第一次全发，之后变了才发。"""
        injector, sim, calls = self._fake()
        injector._map_light_events()
        events = {key: event_id for key, event_id, _ in injector._light_events}
        self.assertEqual(set(events), {key for key, _ in self.inject.LIGHT_EVENTS})

        entry = self._entry("738")
        entry["lights"] = {"landing_on": True, "beacon_on": True}
        injector.sync([entry])
        self._assign(injector, sim, calls, 4242)
        del calls[:]
        injector.sync([entry])
        lights = {c[2]: c[3] for c in calls
                  if c[0] == "transmit" and c[2] in events.values()}
        self.assertEqual(len(lights), len(events), "第一次每盏灯都要发")
        self.assertEqual(lights[events["landing_on"]], 1)
        self.assertEqual(lights[events["taxi_on"]], 0)

        del calls[:]
        injector.sync([entry])
        self.assertEqual([c for c in calls if c[0] == "transmit"], [])

        entry["lights"] = {"landing_on": False, "beacon_on": True}
        injector.sync([entry])
        switched = [c for c in calls if c[0] == "transmit"]
        self.assertEqual([(c[1], c[2], c[3]) for c in switched],
                         [(4242, events["landing_on"], 0)])

    def test_the_freeze_events_are_mapped_once_at_setup(self):
        injector, sim, calls = self._fake()
        maps = [c for c in calls if c[0] == "map"]
        self.assertEqual([c[2] for c in maps], list(self.inject.FREEZE_EVENTS))
        self.assertEqual(len({c[1] for c in maps}), 3, "三个事件要三个不同的号")
        injector.sync([self._entry("738")])
        self._assign(injector, sim, calls, 4242)
        injector.sync([self._entry("738")])
        self.assertEqual(len([c for c in calls if c[0] == "map"]), 3,
                         "映射只在建注入器时做一次")

    def test_an_assigned_object_is_frozen_and_released_once(self):
        injector, sim, calls = self._fake()
        injector.sync([self._entry("738")])
        self._assign(injector, sim, calls, 4242)
        injector.sync([self._entry("738")])
        injector.sync([self._entry("738")])

        control = self._control(calls, 4242)
        releases = [c for c in control if c[0] == "release"]
        transmits = [c for c in control if c[0] == "transmit"]
        self.assertEqual(len(releases), 1, "AIReleaseControl 每个对象只发一次")
        event_ids = {c[1] for c in calls if c[0] == "map"}
        self.assertEqual({c[2] for c in transmits}, event_ids)
        self.assertEqual(len(transmits), 3, "三个冻结事件每个对象各一次")
        for _, object_id, _, data, group, flags in transmits:
            self.assertEqual(object_id, 4242)
            self.assertEqual(data, 1, "dwData=1 是冻上")
            self.assertEqual(group, self.inject.GROUP_PRIORITY_HIGHEST)
            self.assertEqual(flags, self.inject.EVENT_FLAG_GROUPID_IS_PRIORITY)
        self.assertTrue(injector.aircraft["CES2345"]["frozen"])
        # 冻结要在第一次写位置之前
        first_move = next(i for i, c in enumerate(calls) if c[0] == "move")
        last_control = max(i for i, c in enumerate(calls)
                           if c in control)
        self.assertLess(last_control, first_move)

    def test_a_model_change_freezes_the_new_object_too(self):
        injector, sim, calls = self._fake()
        injector.sync([self._entry("通用模型")])
        self._assign(injector, sim, calls, 4242)
        injector.sync([self._entry("通用模型")])
        # #SB 回来了，换模型 = 删了重建
        injector.sync([self._entry("738 Air China")])
        self.assertIn(("remove", 4242), calls)
        self._assign(injector, sim, calls, 5151)
        injector.sync([self._entry("738 Air China")])
        self.assertEqual(len(self._control(calls, 4242)), 4)
        self.assertEqual(len(self._control(calls, 5151)), 4,
                         "重建出来的新对象没有被冻结")

    def test_a_refused_call_is_logged_once_per_object(self):
        injector, sim, calls = self._fake(transmit_result=-2147467259)
        with self.assertLogs("inject", level="WARNING") as logs:
            injector.sync([self._entry("738")])
            self._assign(injector, sim, calls, 4242)
            injector.sync([self._entry("738")])
            injector.sync([self._entry("738")])
        lines = [line for line in logs.output if "off AI control" in line]
        self.assertEqual(len(lines), 1, logs.output)
        self.assertIn("0x80004005", lines[0])
        self.assertFalse(injector.aircraft["CES2345"]["frozen"])

    def test_a_raised_hresult_is_caught(self):
        # 包里 restype 是 ctypes.HRESULT：失败在 Windows 上是抛 OSError
        injector, sim, calls = self._fake(map_result=OSError("-2147467259"))
        self.assertEqual(injector._freeze_events, ())
        injector.sync([self._entry("738")])
        with self.assertLogs("inject", level="WARNING") as logs:
            self._assign(injector, sim, calls, 4242)
            injector.sync([self._entry("738")])
        self.assertTrue(any("not frozen" in line for line in logs.output),
                        logs.output)
        # 冻结不了也照样释放 AI、照样写位置
        self.assertIn("release", [c[0] for c in self._control(calls, 4242)])
        self.assertIn(("move", 4242), calls)

    def test_a_refused_freeze_names_the_aircraft(self):
        injector, sim, calls = self._fake()
        injector.sync([self._entry("738")])
        self._assign(injector, sim, calls, 4242)
        injector.sync([self._entry("738")])
        packet = next(send_id for send_id, (_, _, what)
                      in injector._control_packets.items()
                      if what == "FREEZE_ALTITUDE_SET")
        exceptions = type("X", (), {
            "SIMCONNECT_EXCEPTION_CREATE_OBJECT_FAILED": 22,
            "SIMCONNECT_EXCEPTION_OBJECT_OUTSIDE_REALITY_BUBBLE": 30,
            "SIMCONNECT_EXCEPTION_OBJECT_CONTAINER": 31,
        })
        injector._enums.SIMCONNECT_EXCEPTION = exceptions
        body = type("B", (), {"dwException": 3, "UNKNOWN_SENDID": packet})()
        with self.assertLogs("inject", level="WARNING") as logs:
            injector._note_exception(body)
        self.assertIn("FREEZE_ALTITUDE_SET", logs.output[0])
        self.assertIn("CES2345", logs.output[0])

    def test_constants_match_the_simconnect_package(self):
        try:
            from SimConnect import Enum as sc_enum
        except Exception:
            self.skipTest("SimConnect package not installed")
        self.assertEqual(self.inject.GROUP_PRIORITY_HIGHEST,
                         sc_enum.SIMCONNECT_GROUP_PRIORITY_HIGHEST.value)
        self.assertEqual(
            self.inject.EVENT_FLAG_GROUPID_IS_PRIORITY,
            int(sc_enum.SIMCONNECT_EVENT_FLAG.SIMCONNECT_EVENT_FLAG_GROUPID_IS_PRIORITY))
        # 包自己的 EXCEPTION 结构体里，真正的 dwSendID 落在 UNKNOWN_SENDID 上
        fields = [name for name, _ in sc_enum.SIMCONNECT_RECV_EXCEPTION._fields_]
        self.assertEqual(fields[fields.index("dwException") + 1], "UNKNOWN_SENDID")


class InjectionLoopTest(unittest.TestCase):
    """写入频率：原来 Qt 定时器 5 Hz，MSFS 画面上一卡一卡。"""

    def setUp(self):
        import inject
        self.inject = inject

    def test_runs_off_the_calling_thread_at_the_target_rate(self):
        import threading
        seen = []
        loop = self.inject.InjectionLoop(
            lambda: seen.append((threading.current_thread(), time.perf_counter())))
        loop.start()
        time.sleep(1.0)
        loop.stop()
        threads = {thread for thread, _ in seen}
        self.assertNotIn(threading.current_thread(), threads)
        self.assertEqual(len(threads), 1, "应当是同一条常驻线程")
        rate = (len(seen) - 1) / (seen[-1][1] - seen[0][1])
        self.assertGreater(rate, self.inject.FRAME_RATE * 0.75, f"{rate:.1f} Hz")
        self.assertLess(rate, self.inject.FRAME_RATE * 1.25, f"{rate:.1f} Hz")

    def test_stop_is_final_and_restart_works(self):
        calls = []
        loop = self.inject.InjectionLoop(lambda: calls.append(1))
        loop.start()
        time.sleep(0.1)
        loop.stop()
        self.assertFalse(loop.running)
        count = len(calls)
        time.sleep(0.15)
        self.assertEqual(len(calls), count, "stop() 之后还在跑")
        loop.start()
        time.sleep(0.1)
        loop.stop()
        self.assertGreater(len(calls), count, "停了之后再也起不来")

    def test_a_failing_step_does_not_kill_the_loop(self):
        calls = []

        def step():
            calls.append(1)
            raise RuntimeError("boom")

        loop = self.inject.InjectionLoop(step)
        with self.assertLogs("inject", level="WARNING"):
            loop.start()
            time.sleep(0.15)
            loop.stop()
        self.assertGreater(len(calls), 1)


def _frame_fake(inject, on_frame=None, fail=None):
    """带 Frame / 地面请求 / 装饰字段的 Python-SimConnect 替身。

    和 TakeControlTest 的一样，dispatch 跳板在构造时就包好（SimConnect.py:140），
    收消息走跳板。fail 是 {DLL 函数名: 返回值或异常}。
    """
    import ctypes
    import threading as threading_module

    fail = fail or {}
    calls = []
    packets = iter(range(500, 100000))
    lock = threading_module.Lock()

    def record(entry):
        with lock:
            calls.append(entry)

    def result(name):
        value = fail.get(name, 0)
        if isinstance(value, Exception):
            raise value
        return value

    class Dll:
        @staticmethod
        def DispatchProc(func):
            return ("trampoline", func)

        @staticmethod
        def AddToDataDefinition(handle, definition, name, unit, kind, epsilon, datum):
            record(("define", definition, name, unit))
            if name in fail:
                return fail[name]
            return 0

        @staticmethod
        def MapClientEventToSimEvent(handle, event_id, name):
            return 0

        @staticmethod
        def SubscribeToSystemEvent(handle, event_id, name):
            record(("subscribe", event_id, name))
            return result("SubscribeToSystemEvent")

        @staticmethod
        def TransmitClientEvent(*args):
            return 0

        @staticmethod
        def AIReleaseControl(*args):
            return 0

        @staticmethod
        def AICreateNonATCAircraft(handle, title, tail, init, request_id):
            record(("create", title, request_id, init.Altitude))
            return 0

        @staticmethod
        def AIRemoveObject(handle, object_id, request_id):
            record(("remove", object_id))
            return 0

        @staticmethod
        def RequestDataOnSimObject(handle, request_id, definition, object_id,
                                   period, flags, origin, interval, limit):
            record(("request", request_id, definition, object_id, period,
                    interval))
            return result("RequestDataOnSimObject")

        @staticmethod
        def SetDataOnSimObject(handle, definition, object_id, flags, count,
                               size, values):
            record(("set", definition, object_id, list(values),
                    threading_module.current_thread()))
            if definition in fail:
                value = fail[definition]
                if isinstance(value, Exception):
                    raise value
                return value
            return 0

        @staticmethod
        def GetLastSentPacketID(handle, pointer):
            pointer._obj.value = next(packets)
            return 0

    class Sim:
        def __init__(self):
            self.dll = Dll()
            self.hSimConnect = "handle"
            self.forwarded = []
            self.my_dispatch_proc = self.original
            self.my_dispatch_proc_rd = self.dll.DispatchProc(self.my_dispatch_proc)

        def original(self, pData, cbData, pContext):
            self.forwarded.append(pData.contents.dwID)

        def deliver(self, message):
            return self.my_dispatch_proc_rd[1](
                ctypes.cast(ctypes.pointer(message),
                            ctypes.POINTER(Header)), 0, None)

    class Header(ctypes.Structure):
        _fields_ = [("dwID", ctypes.c_uint32)]

    class Assigned(ctypes.Structure):
        _fields_ = [("dwID", ctypes.c_uint32),
                    ("dwRequestID", ctypes.c_uint32),
                    ("dwObjectID", ctypes.c_uint32)]

    class Event(ctypes.Structure):
        _fields_ = [("dwID", ctypes.c_uint32), ("uGroupID", ctypes.c_uint32),
                    ("uEventID", ctypes.c_uint32), ("dwData", ctypes.c_uint32)]

    class ObjectData(ctypes.Structure):
        # 真结构体的 dwData 是 DWORD * 8192，数据区从 40 字节处开始；这里只要
        # "从 dwData 的偏移读 double" 这个语义
        _fields_ = [("dwID", ctypes.c_uint32), ("dwRequestID", ctypes.c_uint32),
                    ("dwObjectID", ctypes.c_uint32), ("dwDefineID", ctypes.c_uint32),
                    ("dwFlags", ctypes.c_uint32), ("dwentrynumber", ctypes.c_uint32),
                    ("dwoutof", ctypes.c_uint32), ("dwDefineCount", ctypes.c_uint32),
                    ("dwData", ctypes.c_double * 2)]

    class Init(ctypes.Structure):
        _fields_ = [(name, ctypes.c_double) for name in
                    ("Latitude", "Longitude", "Altitude", "Pitch", "Bank",
                     "Heading")] + [("OnGround", ctypes.c_uint32),
                                    ("Airspeed", ctypes.c_uint32)]

    class Enums:
        SIMCONNECT_RECV_ID = type("R", (), {
            "SIMCONNECT_RECV_ID_EXCEPTION": 1,
            "SIMCONNECT_RECV_ID_EVENT_FRAME": 7,
            "SIMCONNECT_RECV_ID_SIMOBJECT_DATA": 8,
            "SIMCONNECT_RECV_ID_ASSIGNED_OBJECT_ID": 12})()
        SIMCONNECT_RECV_ASSIGNED_OBJECT_ID = Assigned
        SIMCONNECT_RECV_EVENT = Event
        SIMCONNECT_RECV_SIMOBJECT_DATA = ObjectData
        SIMCONNECT_DATA_INITPOSITION = Init
        SIMCONNECT_DATATYPE = type("T", (), {"SIMCONNECT_DATATYPE_FLOAT64": 4})()
        SIMCONNECT_UNUSED = 0xFFFFFFFF
        SIMCONNECT_EXCEPTION = type("X", (), {
            "SIMCONNECT_EXCEPTION_CREATE_OBJECT_FAILED": 22,
            "SIMCONNECT_EXCEPTION_OBJECT_OUTSIDE_REALITY_BUBBLE": 30,
            "SIMCONNECT_EXCEPTION_OBJECT_CONTAINER": 31,
        })

    sim = Sim()
    injector = inject.TrafficInjector(sim=None)
    injector.sim = sim
    injector.on_frame = on_frame
    injector._enums = Enums
    # _setup() 的顺序，只是不去 import 真的 SimConnect
    injector._install_dispatch()
    injector._define_position()
    injector._map_freeze_events()
    injector._define_ground()
    injector._define_surfaces()
    injector._subscribe_frames()
    injector.available = True
    types = type("Types", (), {"Assigned": Assigned, "Event": Event,
                               "ObjectData": ObjectData})
    return injector, sim, calls, types


def _traffic_entry(**overrides):
    entry = {"callsign": "CES2345", "latitude": 31.2, "longitude": 121.5,
             "altitude": 5000.0, "pitch": 2.0, "bank": 0.0, "heading": 90.0,
             "groundspeed": 200, "on_ground": False, "model": "738",
             "equipment": "B738", "agl": None, "nose_wheel": 0.0,
             "gear_down": None, "flaps": 0.0}
    entry.update(overrides)
    return entry


def _assign(sim, calls, types, object_id):
    request_id = [c for c in calls if c[0] == "create"][-1][2]
    sim.deliver(types.Assigned(dwID=12, dwRequestID=request_id,
                               dwObjectID=object_id))


class FrameEventTest(unittest.TestCase):
    """注入跟着模拟器的 Frame 事件走，Frame 不来时退回 30 Hz 定时器。"""

    def setUp(self):
        import inject
        self.inject = inject

    def test_setup_subscribes_to_the_frame_event(self):
        injector, sim, calls, _ = _frame_fake(self.inject)
        self.assertIn(("subscribe", self.inject.FRAME_EVENT_ID, b"Frame"), calls)
        self.assertTrue(injector.frames_subscribed)

    def test_a_failed_subscription_is_logged_and_injection_still_works(self):
        with self.assertLogs("inject", level="WARNING") as logs:
            injector, sim, calls, types = _frame_fake(
                self.inject, fail={"SubscribeToSystemEvent": OSError("-2147467259")})
        self.assertFalse(injector.frames_subscribed)
        self.assertTrue(any("Frame event" in line for line in logs.output))
        injector.sync([_traffic_entry()])
        _assign(sim, calls, types, 4242)
        injector.sync([_traffic_entry()])
        self.assertTrue([c for c in calls if c[0] == "set" and c[2] == 4242])

    def test_a_frame_event_reaches_on_frame_through_the_trampoline(self):
        frames = []
        injector, sim, calls, types = _frame_fake(
            self.inject, on_frame=lambda: frames.append(1))
        sim.deliver(types.Event(dwID=7, uEventID=self.inject.FRAME_EVENT_ID))
        self.assertEqual(frames, [1])
        self.assertEqual(sim.forwarded, [],
                         "自己订的 Frame 不该交给包的处理（它只会打一条坏掉的 DEBUG）")
        # 别人的帧事件照样交回去
        sim.deliver(types.Event(dwID=7, uEventID=3))
        self.assertEqual(frames, [1])
        self.assertEqual(sim.forwarded, [7])

    def test_frames_drive_the_writes_on_the_loop_thread(self):
        import threading
        loop = None
        injector, sim, calls, types = _frame_fake(
            self.inject, on_frame=lambda: loop.frame())
        injector.sync([_traffic_entry()])
        _assign(sim, calls, types, 4242)
        injector.sync([_traffic_entry()])
        before = len(calls)
        loop = self.inject.InjectionLoop(lambda: injector.sync([_traffic_entry()]))
        with self.assertLogs("inject", level="INFO") as logs:
            loop.start()
            dispatch_frames = 0
            for _ in range(30):             # 50 fps，0.6 s
                sim.deliver(types.Event(dwID=7, uEventID=self.inject.FRAME_EVENT_ID))
                dispatch_frames += 1
                time.sleep(0.02)
            mode = loop.mode
            loop.stop()
        self.assertEqual(mode, "frame")
        self.assertTrue(any("follows the simulator's frames" in line
                            for line in logs.output), logs.output)
        writes = [c for c in calls[before:] if c[0] == "set" and c[2] == 4242
                  and c[1] == injector.definition_id]
        self.assertGreater(len(writes), dispatch_frames * 0.6,
                           f"{len(writes)} writes for {dispatch_frames} frames")
        self.assertLessEqual(len(writes), dispatch_frames + 3)
        threads = {c[4] for c in writes}
        self.assertNotIn(threading.current_thread(), threads,
                         "写入不该在 dispatch 线程（这里是测试线程）上做")

    def test_the_timer_takes_over_without_frames_and_hands_back(self):
        steps = []
        loop = self.inject.InjectionLoop(lambda: steps.append(time.perf_counter()),
                                         frame_timeout=0.1)
        with self.assertLogs("inject", level="INFO") as logs:
            loop.start()
            time.sleep(0.3)
            self.assertEqual(loop.mode, "timer")
            timer_steps = len(steps)
            for _ in range(15):
                loop.frame()
                time.sleep(0.02)
            self.assertEqual(loop.mode, "frame")
            time.sleep(0.4)                 # Frame 停了（暂停）
            self.assertEqual(loop.mode, "timer")
            loop.stop()
        self.assertGreater(timer_steps, 5, "没有 Frame 时定时器没有接手")
        modes = [line for line in logs.output if "traffic injection" in line]
        self.assertEqual(len(modes), 3, modes)
        self.assertIn("timer", modes[0])
        self.assertIn("frames", modes[1])
        self.assertIn("timer", modes[2])

    def test_frames_above_the_cap_are_thinned(self):
        steps = []
        loop = self.inject.InjectionLoop(lambda: steps.append(1),
                                         max_frame_rate=60.0)
        loop.start()
        start = time.perf_counter()
        frames = 0
        while time.perf_counter() - start < 0.5:
            loop.frame()
            frames += 1
            time.sleep(0.004)               # ~200 fps
        loop.stop()
        self.assertGreater(frames, 60)
        self.assertLess(len(steps), 60 * 0.5 * 1.25 + 3,
                        f"{len(steps)} steps for {frames} frames")

    def test_stop_wakes_a_loop_waiting_for_a_frame_and_is_final(self):
        steps = []
        loop = self.inject.InjectionLoop(lambda: steps.append(1),
                                         frame_timeout=5.0)
        loop.frame()
        loop.start()
        time.sleep(0.05)
        self.assertEqual(loop.mode, "frame")
        began = time.perf_counter()
        loop.stop()
        self.assertLess(time.perf_counter() - began, 0.5,
                        "stop() 等满了 Frame 超时")
        self.assertFalse(loop.running)
        count = len(steps)
        for _ in range(5):
            loop.frame()                    # 断开之后模拟器照样发 Frame
            time.sleep(0.01)
        self.assertEqual(len(steps), count, "停了之后 Frame 又把循环叫醒了")


class GroundRequestTest(unittest.TestCase):
    """每个注入对象向模拟器要地面标高和模型离地高度。"""

    def setUp(self):
        import inject
        self.inject = inject

    def _claimed(self, fail=None):
        injector, sim, calls, types = _frame_fake(self.inject, fail=fail)
        injector.sync([_traffic_entry()])
        _assign(sim, calls, types, 4242)
        injector.sync([_traffic_entry()])
        return injector, sim, calls, types

    def _requests(self, calls, object_id):
        return [c for c in calls if c[0] == "request" and c[3] == object_id]

    def test_definitions_are_separate_from_the_position(self):
        injector, sim, calls, _ = _frame_fake(self.inject)
        defined = {}
        for _, definition, name, _unit in [c for c in calls if c[0] == "define"]:
            defined.setdefault(definition, []).append(name)
        position = defined.pop(injector.definition_id)
        self.assertEqual(len(position), len(self.inject._Definition.FIELDS))
        for definition, names in defined.items():
            self.assertEqual(len(names), 1,
                             f"定义 {definition} 里不止一个字段：{names}")
        names = {n[0] for n in defined.values()}
        self.assertIn(b"GROUND ALTITUDE", names)
        self.assertIn(b"STATIC CG TO GROUND", names)
        for _, name, _ in self.inject.SURFACE_FIELDS:
            self.assertIn(name, names)
            self.assertNotIn(name, position)

    def test_a_claimed_object_asks_for_its_ground(self):
        injector, sim, calls, _ = self._claimed()
        requests = self._requests(calls, 4242)
        ground = [c for c in requests if c[2] == injector.definition_id
                  + self.inject.GROUND_DEFINITION_OFFSET]
        height = [c for c in requests if c[2] == injector.definition_id
                  + self.inject.HEIGHT_DEFINITION_OFFSET]
        self.assertEqual(len(ground), 1)
        self.assertEqual(ground[0][4], self.inject.PERIOD_SIM_FRAME)
        self.assertEqual(ground[0][5], self.inject.GROUND_INTERVAL)
        self.assertEqual(len(height), 1)
        self.assertEqual(height[0][4], self.inject.PERIOD_ONCE)
        injector.sync([_traffic_entry()])
        self.assertEqual(len(self._requests(calls, 4242)), 2, "每个对象只要一次")

    def _reply(self, sim, types, calls, kind_offset, injector, value, object_id=4242):
        definition = injector.definition_id + kind_offset
        request_id = next(c[1] for c in calls if c[0] == "request"
                          and c[2] == definition and c[3] == object_id)
        message = types.ObjectData(dwID=8, dwRequestID=request_id,
                                   dwObjectID=object_id)
        message.dwData[0] = value
        sim.deliver(message)

    def test_replies_are_consumed_and_clamp_an_aircraft_on_the_ground(self):
        injector, sim, calls, types = self._claimed()
        sim.forwarded.clear()               # ASSIGNED_OBJECT_ID 是要交回去的
        with self.assertLogs("inject", level="INFO") as logs:
            self._reply(sim, types, calls, self.inject.GROUND_DEFINITION_OFFSET,
                        injector, 120.0)
        self.assertTrue(any("reports the ground" in line for line in logs.output))
        self._reply(sim, types, calls, self.inject.HEIGHT_DEFINITION_OFFSET,
                    injector, 8.0)
        self.assertEqual(sim.forwarded, [], "自己要的数据不该交给包的处理")
        # 别人的 SIMOBJECT_DATA 照样交回去
        sim.deliver(types.ObjectData(dwID=8, dwRequestID=3, dwObjectID=1))
        self.assertEqual(sim.forwarded, [8])

        mark = len(calls)
        injector.sync([_traffic_entry(altitude=150.0, agl=7.0, on_ground=True)])
        move = [c for c in calls[mark:] if c[0] == "set"
                and c[1] == injector.definition_id][0]
        self.assertAlmostEqual(move[3][2], 128.0, places=3,
                               msg="在地面的飞机要落在本地地面 + 模型离地高度")

    def test_no_ground_reply_writes_the_reported_altitude(self):
        injector, sim, calls, types = self._claimed()
        mark = len(calls)
        injector.sync([_traffic_entry(altitude=150.0, agl=7.0, on_ground=True)])
        move = [c for c in calls[mark:] if c[0] == "set"
                and c[1] == injector.definition_id][0]
        self.assertEqual(move[3][2], 150.0)

    def test_an_implausible_ground_is_ignored_and_logged_once(self):
        injector, sim, calls, types = self._claimed()
        # 冻住的对象万一报 0：高原机场上会把飞机往下拽几千英尺
        self._reply(sim, types, calls, self.inject.GROUND_DEFINITION_OFFSET,
                    injector, 0.0)
        with self.assertLogs("inject", level="WARNING") as logs:
            for _ in range(3):
                mark = len(calls)
                injector.sync([_traffic_entry(altitude=6900.0, agl=8.0,
                                              on_ground=True)])
        move = [c for c in calls[mark:] if c[0] == "set"
                and c[1] == injector.definition_id][0]
        self.assertEqual(move[3][2], 6900.0)
        self.assertEqual(len([l for l in logs.output if "not using it" in l]), 1)

    def test_removal_stops_the_request_and_forgets_the_ground(self):
        injector, sim, calls, types = self._claimed()
        self._reply(sim, types, calls, self.inject.GROUND_DEFINITION_OFFSET,
                    injector, 120.0)
        injector.sync([])
        never = [c for c in self._requests(calls, 4242)
                 if c[4] == self.inject.PERIOD_NEVER]
        self.assertEqual(len(never), 1)
        self.assertLess(calls.index(never[0]), calls.index(("remove", 4242)),
                        "要先停请求再删对象")
        self.assertNotIn(4242, injector._ground)
        self.assertEqual(injector._data_requests, {})

    def test_a_refused_ground_request_names_the_aircraft(self):
        injector, sim, calls, types = self._claimed()
        packet = next(send_id for send_id, (_, _, what)
                      in injector._control_packets.items()
                      if what == "the ground elevation request")
        body = type("B", (), {"dwException": 15, "UNKNOWN_SENDID": packet})()
        with self.assertLogs("inject", level="WARNING") as logs:
            injector._note_exception(body)
        self.assertIn("ground elevation request", logs.output[0])
        self.assertIn("CES2345", logs.output[0])

    def test_a_request_that_raises_does_not_stop_the_aircraft(self):
        with self.assertLogs("inject", level="WARNING") as logs:
            injector, sim, calls, types = self._claimed(
                fail={"RequestDataOnSimObject": OSError("-2147467259")})
        self.assertTrue(any("could not request" in line for line in logs.output))
        self.assertEqual(injector._data_requests, {})
        mark = len(calls)
        injector.sync([_traffic_entry()])
        self.assertTrue([c for c in calls[mark:] if c[0] == "set"
                         and c[1] == injector.definition_id])

    def test_an_undefinable_ground_disables_the_requests(self):
        with self.assertLogs("inject", level="WARNING"):
            injector, sim, calls, types = self._claimed(
                fail={b"GROUND ALTITUDE": -2147467259})
        self.assertFalse(injector._ground_ready)
        self.assertEqual(self._requests(calls, 4242), [])


class SurfaceWriteTest(unittest.TestCase):
    """起落架/襟翼/前轮：各自一个定义，被拒只丢那一个字段。"""

    def setUp(self):
        import inject
        self.inject = inject

    def _claimed(self, fail=None):
        injector, sim, calls, types = _frame_fake(self.inject, fail=fail)
        injector.sync([_traffic_entry()])
        _assign(sim, calls, types, 4242)
        return injector, sim, calls, types

    def _definition(self, injector, key):
        index = [k for k, _, _ in self.inject.SURFACE_FIELDS].index(key)
        return injector.definition_id + self.inject.SURFACE_DEFINITION_OFFSET + index

    def _writes(self, calls, definition, since=0):
        return [c[3][0] for c in calls[since:] if c[0] == "set" and c[1] == definition]

    def test_values_are_written_only_when_they_change(self):
        injector, sim, calls, types = self._claimed()
        gear = self._definition(injector, "gear")
        flaps = self._definition(injector, "flaps_left")
        for _ in range(3):
            injector.sync([_traffic_entry(gear_down=True, flaps=0.25)])
        self.assertEqual(self._writes(calls, gear), [1.0])
        self.assertEqual(self._writes(calls, flaps), [0.25])
        injector.sync([_traffic_entry(gear_down=False, flaps=0.25)])
        self.assertEqual(self._writes(calls, gear), [1.0, 0.0])

    def test_unknown_gear_on_the_ground_is_down(self):
        injector, sim, calls, types = self._claimed()
        injector.sync([_traffic_entry(on_ground=True, gear_down=None)])
        self.assertEqual(self._writes(calls, self._definition(injector, "gear")),
                         [1.0])

    def test_a_refused_field_is_dropped_and_the_position_keeps_moving(self):
        injector, sim, calls, types = self._claimed()
        injector.sync([_traffic_entry(on_ground=True, nose_wheel=30.0)])
        wheel = self._definition(injector, "nose_wheel")
        self.assertEqual(self._writes(calls, wheel), [0.5])
        packet = next(p for p, key in injector._surface_packets.items()
                      if key == "nose_wheel")
        body = type("B", (), {"dwException": 20, "UNKNOWN_SENDID": packet})()
        with self.assertLogs("inject", level="WARNING") as logs:
            injector._note_exception(body)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("GEAR CENTER STEER ANGLE", logs.output[0])
        mark = len(calls)
        injector.sync([_traffic_entry(on_ground=True, nose_wheel=-30.0)])
        self.assertEqual(self._writes(calls, wheel, mark), [], "被拒过的字段又写了")
        self.assertTrue([c for c in calls[mark:] if c[0] == "set"
                         and c[1] == injector.definition_id],
                        "装饰字段被拒不该拖累位置写入")

    def test_a_raising_write_is_logged_once(self):
        flaps = self._definition(self.inject.TrafficInjector(sim=None),
                                 "flaps_right")
        injector2, sim2, calls2, types2 = _frame_fake(
            self.inject, fail={flaps: OSError("-2147467259")})
        injector2.sync([_traffic_entry()])
        _assign(sim2, calls2, types2, 4242)
        with self.assertLogs("inject", level="WARNING") as logs:
            for flap in (0.1, 0.2, 0.3):
                injector2.sync([_traffic_entry(flaps=flap)])
        self.assertEqual(len([l for l in logs.output
                              if "TRAILING EDGE FLAPS RIGHT" in l]), 1)
        self.assertEqual(len(self._writes(calls2, flaps)), 1)
        self.assertEqual(len(self._writes(
            calls2, self._definition(injector2, "flaps_left"))), 3)


class GroundClampTest(unittest.TestCase):
    """地面贴合的纯函数（xPilot 的 PerformGroundClamping）。"""

    def setUp(self):
        import inject
        self.inject = inject

    def _run(self, clamp, seconds, dt=1 / 60, start=0.0, **sample):
        now = start
        altitude = None
        while now < start + seconds - 1e-9:
            now += dt
            altitude = clamp.update(now, dt, **sample)
        return now, altitude

    def test_on_the_ground_sits_on_local_ground_plus_model_height(self):
        self.assertEqual(self.inject.ground_target_offset(
            150.0, 7.0, 120.0, True, False, 8.0), -22.0)
        clamp = self.inject.GroundClamp()
        # 第一次拿到地面直接到位
        self.assertAlmostEqual(clamp.update(0.0, 0.0, 150.0, 7.0, True, 120.0, 8.0),
                               128.0)

    def test_never_below_local_ground(self):
        clamp = self.inject.GroundClamp()
        self.assertEqual(clamp.update(0.0, 0.0, 90.0, None, False, 120.0, 8.0),
                         128.0)

    def test_airborne_offset_needs_two_seconds_near_the_ground(self):
        f = self.inject.ground_target_offset
        self.assertEqual(f(600.0, 80.0, 540.0, False, True), 20.0)
        self.assertEqual(f(600.0, 80.0, 540.0, False, False), 0.0)
        clamp = self.inject.GroundClamp()
        sample = dict(altitude=600.0, agl=80.0, on_ground=False, local_ground=540.0)
        _, altitude = self._run(clamp, 1.9, **sample)
        self.assertEqual(altitude, 600.0, "不到两秒就用了偏移")
        now, _ = self._run(clamp, 0.2, start=1.9, **sample)
        self.assertEqual(clamp.target, 20.0)
        _, altitude = self._run(clamp, 2.1, start=now, **sample)
        self.assertAlmostEqual(altitude, 620.0, places=6, msg="着陆段 2 秒走完")

    def test_landing_blends_in_two_seconds(self):
        clamp = self.inject.GroundClamp()
        airborne = dict(altitude=100.0, agl=100.0, on_ground=False, local_ground=30.0)
        now, _ = self._run(clamp, 0.5, **airborne)
        self.assertEqual(clamp.offset, 0.0)
        # 接地：对方地面 0，本地地面 30
        ground = dict(altitude=6.0, agl=6.0, on_ground=True, local_ground=30.0,
                      model_height=6.0)
        now, altitude = self._run(clamp, 1.0, start=now, **ground)
        self.assertGreater(clamp.offset, 0.0)
        self.assertLess(clamp.offset, 30.0, "一帧就跳到位了")
        self.assertEqual(altitude, 36.0, "不低于地面 + 模型高度")
        _, altitude = self._run(clamp, 1.1, start=now, **ground)
        self.assertAlmostEqual(clamp.offset, 30.0, places=6)

    def test_climb_out_takes_ten_seconds(self):
        clamp = self.inject.GroundClamp()
        ground = dict(altitude=6.0, agl=6.0, on_ground=True, local_ground=30.0,
                      model_height=6.0)
        now, _ = self._run(clamp, 0.1, **ground)
        self.assertAlmostEqual(clamp.offset, 30.0)
        climb = dict(altitude=500.0, agl=500.0, on_ground=False, local_ground=30.0)
        now, _ = self._run(clamp, 5.0, start=now, **climb)
        self.assertAlmostEqual(clamp.offset, 15.0, delta=0.2,
                               msg="爬升段应当 10 秒走完，5 秒走一半")
        self._run(clamp, 5.1, start=now, **climb)
        self.assertEqual(clamp.offset, 0.0)

    def test_above_the_ceiling_the_ground_is_ignored(self):
        clamp = self.inject.GroundClamp()
        self.assertEqual(clamp.update(0.0, 0.0, 18000.0, 17000.0, False, 40000.0),
                         18000.0)

    def test_the_result_does_not_depend_on_the_frame_rate(self):
        results = []
        for dt in (1 / 30, 1 / 60, 1 / 144):
            clamp = self.inject.GroundClamp()
            self._run(clamp, 0.1, dt=dt, altitude=6.0, agl=6.0, on_ground=True,
                      local_ground=30.0, model_height=6.0)
            _, altitude = self._run(clamp, 3.0, dt=dt, start=0.1, altitude=500.0,
                                    agl=500.0, on_ground=False, local_ground=30.0)
            results.append(clamp.offset)
        self.assertAlmostEqual(min(results), max(results), delta=0.1, msg=results)

    def test_an_implausible_local_ground_is_rejected(self):
        clamp = self.inject.GroundClamp()
        self.assertEqual(clamp.update(0.0, 0.0, 6900.0, 8.0, True, 0.0), 6900.0)
        self.assertTrue(clamp.rejected)


class ModelWaitTest(unittest.TestCase):
    """机型还没问到时先等一会再建，免得通用模型建出来又换掉。"""

    def setUp(self):
        import inject
        self.inject = inject

    def _creates(self, calls):
        return [c for c in calls if c[0] == "create"]

    def test_a_known_type_is_created_at_once(self):
        injector, sim, calls, _ = _frame_fake(self.inject)
        injector.sync([_traffic_entry()], now=100.0)
        self.assertEqual(len(self._creates(calls)), 1)

    def test_an_unknown_type_waits_then_is_created_anyway(self):
        injector, sim, calls, _ = _frame_fake(self.inject)
        entry = _traffic_entry(equipment="", model="通用模型")
        injector.sync([entry], now=100.0)
        injector.sync([entry], now=100.0 + self.inject.MODEL_WAIT - 0.1)
        self.assertEqual(self._creates(calls), [], "没等机型就建了")
        injector.sync([entry], now=100.0 + self.inject.MODEL_WAIT)
        self.assertEqual(len(self._creates(calls)), 1,
                         "只发 @ 的老客户端也得看得见")

    def test_the_type_arriving_during_the_wait_creates_the_right_model(self):
        injector, sim, calls, _ = _frame_fake(self.inject)
        injector.sync([_traffic_entry(equipment="", model="通用模型")], now=100.0)
        injector.sync([_traffic_entry(model="738 Air China")], now=100.5)
        creates = self._creates(calls)
        self.assertEqual(len(creates), 1)
        self.assertEqual(creates[0][1], b"738 Air China")
        self.assertEqual(injector._waiting_for_type, {})

    def test_an_aircraft_that_leaves_while_waiting_is_forgotten(self):
        injector, sim, calls, _ = _frame_fake(self.inject)
        injector.sync([_traffic_entry(equipment="", model="通用模型")], now=100.0)
        injector.sync([], now=100.5)
        self.assertEqual(injector._waiting_for_type, {})


class VoiceHostTest(unittest.TestCase):
    """语音服务器换域名之后，老配置里存的那个旧域名必须换掉。

    settings.py 是和 xpc 有意分开的一份（配置文件名不一样），所以这一条要在
    两边各测一次。mumble_host 存进 msfs_settings.json，只改 DEFAULTS 只对全新
    安装有效；旧域名停掉那天老用户看到的是"连不上语音服务器"，而设置界面上
    那一行看着完全正常。
    """

    def setUp(self):
        import settings as settings_module
        self.module = settings_module
        self.temp = tempfile.mkdtemp(prefix="msfs_settings_")
        self.addCleanup(shutil.rmtree, self.temp, True)
        self.path = os.path.join(self.temp, "msfs_settings.json")

    def write(self, data):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f)

    def test_default_voice_host(self):
        self.assertEqual(self.module.MUMBLE_HOST, "audio.ceruleanavi.net")
        self.assertEqual(self.module.Settings(self.path).mumble_host,
                         "audio.ceruleanavi.net")

    def test_migrates_the_old_domain(self):
        # 两个旧域名都要认：hjdczy.top 是更早的那次改名，audio.airwaysn.org
        # 是这一次的，而 airwaysn.org 整个域已经不解析了。
        for host in ("hjdczy.top", "audio.airwaysn.org"):
            with self.subTest(host=host):
                self.write({"mumble_host": host})
                self.assertEqual(self.module.Settings(self.path).mumble_host,
                                 "audio.ceruleanavi.net")

    def test_migrates_the_old_fsd_domain(self):
        # fsd_host 同样是存进配置文件的，只改默认值对老用户没用：他们连不上，
        # 而报出来的是超时，看不出是域名的事。
        self.write({"fsd_host": "fsd.airwaysn.org"})
        self.assertEqual(self.module.Settings(self.path).fsd_host,
                         "fsd.ceruleanavi.net")

    def test_keeps_a_deliberate_fsd_override(self):
        self.write({"fsd_host": "127.0.0.1"})
        self.assertEqual(self.module.Settings(self.path).fsd_host, "127.0.0.1")

    def test_keeps_a_deliberate_override(self):
        # 自己指了别的服务器（测试服、局域网）是有意为之，不能替他改掉
        self.write({"mumble_host": "127.0.0.1"})
        self.assertEqual(self.module.Settings(self.path).mumble_host, "127.0.0.1")


class SharedCopyTest(unittest.TestCase):
    """和 xpc 逐字节共享的那几个文件不能各改各的。

    这个仓库靠复制共享代码，不靠 import。`voice.py` 的运行时测试只写在
    xpc/test_xpc.py 里——只有这两份完全一样，那些测试才代表这一份也是对的。
    掉线重连之后不回频率频道的 bug 就同时存在于两边。

    `fsdpilot.py` 和 `applog.py` 的分叉是有意的，不在这里管——前者要报不同的
    模拟器编号，后者写不同的日志文件名。`i18n.py` 也是有意分开的：键名一样，
    文案里提到模拟器的那几条不一样。

    `ptt.py` 和 `theme.py` 是后加的共享件，同样一处都不能自己改。`chime.py`
    也一样：提示音的判定和播放只在 xpc/test_xpc.py 里测，两份不一致的话
    这边就成了没人测过的代码。

    `micgain.py`、`denoise.py` 和它们的测试也是共享件：降噪、增益和限幅两边
    必须一致，否则两个客户端发出去的响度和底噪不一样，校准就白做了。
    """

    SHARED = ("voice.py", "traffic.py", "altitude.py", "mumblecompat.py", "ptt.py",
              "theme.py", "update.py", "chime.py", "observer.py",
              "micgain.py", "test_micgain.py", "denoise.py", "test_denoise.py",
              "calibration.py")

    def test_shared_files_are_byte_identical_to_xpc(self):
        here = os.path.dirname(os.path.abspath(__file__))
        there = os.path.join(os.path.dirname(here), "xpc")
        if not os.path.isdir(there):
            self.skipTest("边上没有 xpc 目录")
        for name in self.SHARED:
            mine = os.path.join(here, name)
            theirs = os.path.join(there, name)
            if not os.path.exists(theirs):
                continue
            with open(mine, "rb") as f:
                a = f.read()
            with open(theirs, "rb") as f:
                b = f.read()
            self.assertEqual(
                hashlib.md5(a).hexdigest(), hashlib.md5(b).hexdigest(),
                f"{name} 和 xpc 的那份不一样了——改了一边就要把另一边同步过去")


class StoredPasswordTest(unittest.TestCase):
    """密码不再默认落盘。

    这一格里的 password 不是"这个客户端的密码"——它就是成员的**网站密码**：
    can-api 的 `VerifyNetworkCredential` 对两个列都认，注册和改密写进去的是
    同一个秘密。所以配置文件泄露一次，泄露的是整个账号。

    而它躺的地方偏偏最容易被端走：写在**当前工作目录**，也就是用户双击 exe
    的地方——MSFS 的 Community 文件夹、会被云同步的游戏目录、报障时打包发过来的那个 zip。

    迁移的形状照着 OLD_MUMBLE_HOSTS 那个来：认得出老样子，就地改掉，并且
    说出来。这里最要紧的一条是**不能把人悄悄锁在外面**，所以老密码这一次
    还在内存里，连接照常。
    """

    def setUp(self):
        import settings as settings_module
        self.module = settings_module
        self.temp = tempfile.mkdtemp(prefix="msfs_settings_")
        self.addCleanup(shutil.rmtree, self.temp, True)
        self.path = os.path.join(self.temp, "msfs_settings.json")

    def write(self, data):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f)

    def read(self):
        with open(self.path, "r", encoding="utf-8") as f:
            return json.load(f)

    # ---------- 默认 ----------
    def test_remembering_is_off_by_default(self):
        self.assertIs(self.module.DEFAULTS["remember_password"], False,
                      "默认存网站密码是不行的")

    def test_a_password_is_not_written_unless_asked(self):
        s = self.module.Settings(self.path)
        s.cid = "1234"
        s.password = "hunter2"
        s.save()
        self.assertEqual(self.read()["password"], "",
                         "没勾记住密码，密码不该出现在文件里")
        self.assertEqual(self.read()["cid"], "1234", "别的字段照旧存")

    def test_ticking_the_box_stores_it(self):
        """勾了就是勾了——这是用户自己的决定，不能替他做主。"""
        s = self.module.Settings(self.path)
        s.password = "hunter2"
        s.remember_password = True
        s.save()
        self.assertEqual(self.read()["password"], "hunter2")
        self.assertIs(self.read()["remember_password"], True)
        again = self.module.Settings(self.path)
        self.assertEqual(again.password, "hunter2")
        self.assertIs(again.remember_password, True)
        self.assertFalse(again.password_migrated, "这不是待迁移的老配置")

    # ---------- 老配置 ----------
    def test_an_old_file_still_connects_this_session(self):
        """**不能悄悄把人锁在外面。**

        老版本存下来的密码这一次还要能用：读进内存，连接照常。下次启动那一格
        才是空的，而界面会说明原因（msg.password_dropped）。
        """
        self.write({"cid": "1234", "password": "hunter2"})
        s = self.module.Settings(self.path)
        self.assertEqual(s.password, "hunter2", "这一次运行还得连得上")
        self.assertTrue(s.password_migrated, "界面要靠它提示一句")

    def test_an_old_file_is_rewritten_at_once(self):
        """当场重写，不等下一次 save()。

        等的话，一个只是打开看看就关掉的用户，明文密码原封不动留在那儿。
        """
        self.write({"cid": "1234", "password": "hunter2"})
        self.module.Settings(self.path)
        self.assertEqual(self.read()["password"], "")
        self.assertIs(self.read()["remember_password"], False)

    def test_the_migration_only_happens_once(self):
        self.write({"cid": "1234", "password": "hunter2"})
        self.module.Settings(self.path)
        second = self.module.Settings(self.path)
        self.assertFalse(second.password_migrated,
                         "已经迁过的文件不该再被当成老配置")
        self.assertEqual(second.password, "")

    def test_turning_it_back_off_clears_what_was_stored(self):
        """取消勾选要真的把文件里那份删掉，不能只是不再更新它。"""
        self.write({"cid": "1234", "password": "hunter2",
                    "remember_password": True})
        s = self.module.Settings(self.path)
        self.assertEqual(s.password, "hunter2")
        self.assertFalse(s.password_migrated)
        s.remember_password = False
        s.save()
        self.assertEqual(self.read()["password"], "")

    def test_an_empty_old_password_is_not_a_migration(self):
        """没存过密码的老配置不该弹那句提示。"""
        for value in ("", "   "):
            self.write({"cid": "1234", "password": value})
            s = self.module.Settings(self.path)
            self.assertFalse(s.password_migrated)

    def test_a_user_who_cleared_their_password_is_not_re_migrated(self):
        """自己关掉记住密码之后，password 是空串但键是在的。

        判据认的是 `remember_password` 这个键在不在，不是密码空不空——否则
        每次启动都会重跑一遍迁移，提示也会每次都弹。
        """
        self.write({"cid": "1234", "password": "", "remember_password": False})
        s = self.module.Settings(self.path)
        self.assertFalse(s.password_migrated)


class MicCalibrationSettingsTest(unittest.TestCase):
    """mic_calibration、mic_denoise 落盘；mic_volume 是本次会话的乘数，不落盘。"""

    def setUp(self):
        import denoise
        import settings as settings_module
        self.denoise = denoise
        self.settings_module = settings_module
        self._available = denoise.available
        denoise.available = lambda: True
        self.path = os.path.join(tempfile.mkdtemp(prefix="can-settings-"),
                                 "settings.json")

    def tearDown(self):
        self.denoise.available = self._available

    def test_calibration_and_denoise_round_trip(self):
        s = self.settings_module.Settings(self.path)
        s.mic_calibration["Mic A"] = {"gain_db": 8.5, "denoise": True}
        s.save()
        again = self.settings_module.Settings(self.path)
        self.assertEqual(again.mic_calibration, {"Mic A": {"gain_db": 8.5, "denoise": True}})
        self.assertTrue(again.mic_denoise)
        self.assertEqual(again.baseline_for("Mic A"), 8.5)

    def test_toggling_denoise_invalidates_the_baseline(self):
        s = self.settings_module.Settings(self.path)
        s.mic_calibration["Mic A"] = {"gain_db": 8.5, "denoise": True}
        s.mic_denoise = False
        self.assertIsNone(s.baseline_for("Mic A"))

    def test_instances_do_not_share_the_dict(self):
        a = self.settings_module.Settings(self.path + ".a")
        b = self.settings_module.Settings(self.path + ".b")
        a.mic_calibration["Mic A"] = {"gain_db": 1.0}
        self.assertEqual(b.mic_calibration, {})

    def test_mic_volume_is_not_persisted(self):
        s = self.settings_module.Settings(self.path)
        s.mic_volume = 150
        s.save()
        with open(self.path, encoding="utf-8") as f:
            self.assertNotIn("mic_volume", json.load(f))
        self.assertEqual(self.settings_module.Settings(self.path).mic_volume, 100)

    def test_old_mic_volume_is_ignored(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"mic_volume": 170}, f)
        self.assertEqual(self.settings_module.Settings(self.path).mic_volume, 100)

    def test_corrupt_calibration_becomes_empty(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"mic_calibration": "x"}, f)
        self.assertEqual(self.settings_module.Settings(self.path).mic_calibration, {})


# ---------------------------------------------------------------------------
# FSD 连接和他机：掉线重连、发送失败、快速位置包、重复样本、时间戳。
# fsdpilot.py 是 msfs 自己的分叉，这些只在这里测。
# ---------------------------------------------------------------------------

import logging
import re
import socket
import threading

import fsdpilot
import traffic as traffic_module

CAN_FSD = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "can-fsd", "internal", "fsd")


class _FakeClock:
    """假的单调钟和假的 stop_event：wait(delay) 直接把钟拨过去。"""

    def __init__(self):
        self.now = 1000.0
        self.waits = []

    def monotonic(self):
        return self.now

    def wait(self, delay):
        self.waits.append(delay)
        self.now += delay
        return False

    def is_set(self):
        return False

    def set(self):
        pass


class FsdRetryPolicyTest(unittest.TestCase):
    """重连要熬过服务端的"幽灵连接"，认证失败则立刻停。

    can-fsd 在旧连接死透之前拒绝同一个呼号（postoffice.go 的 register），
    读超时 90 秒、空闲清理最长约 90 秒。老策略三次 × 3 秒就放弃，还连语音
    一起下线。
    """

    def make_client(self, outcomes):
        """outcomes 里每一项是一次 _connect 的结果：True，或一个 FAILURE_*。"""
        client = fsdpilot.FSDPilot.__new__(fsdpilot.FSDPilot)
        client.callsign = "CCA1501"
        client.running = True
        client.clock = _FakeClock()
        client.stop_event = client.clock
        client.reconnect_window = fsdpilot.RECONNECT_WINDOW
        client.gave_up = False
        client._retryable = False
        client._failure = fsdpilot.FAILURE_TRANSIENT
        client.states = []
        client._status = lambda state, message: client.states.append(
            ('reconnecting' if client._retryable and state in ('error', 'stopped')
             else state, message))
        client.connect_calls = 0

        def fake_connect():
            index = client.connect_calls
            client.connect_calls += 1
            outcome = outcomes[index] if index < len(outcomes) else outcomes[-1]
            if outcome is True:
                return True
            client._failure = outcome
            if outcome == fsdpilot.FAILURE_FATAL:
                client._retryable = False
            return False

        client._connect = fake_connect
        client._loop = lambda: None
        client._close = lambda: None
        return client

    def run_client(self, client):
        with mock.patch.object(fsdpilot.time, "monotonic", client.clock.monotonic):
            client._run()

    def test_the_window_outlasts_the_server_holding_the_callsign(self):
        # can-fsd：readTimeout 90 s；空闲 60 s、每 30 s 扫一次
        self.assertGreaterEqual(fsdpilot.RECONNECT_WINDOW, 120)

    def test_a_callsign_held_by_the_ghost_is_waited_out(self):
        in_use = fsdpilot.FAILURE_IN_USE
        # 掉线后一分半左右都被旧连接占着（3+5+10+15+20+30 秒），然后放出来了
        client = self.make_client([True] + [in_use] * 6 + [True, fsdpilot.FAILURE_FATAL])
        self.run_client(client)
        self.assertEqual(client.connect_calls, 9)
        self.assertGreaterEqual(sum(client.clock.waits), 90)
        self.assertFalse(client.gave_up)
        self.assertGreater(client.connect_calls, 4, "老预算只有三次重试")
        self.assertNotIn('offline', [state for state, _ in client.states])

    def test_gives_up_only_after_the_whole_window(self):
        client = self.make_client([True] + [fsdpilot.FAILURE_IN_USE] * 100)
        self.run_client(client)
        self.assertTrue(client.gave_up)
        self.assertEqual(client.states[-1][0], 'offline')
        self.assertGreaterEqual(sum(client.clock.waits), fsdpilot.RECONNECT_WINDOW)
        self.assertLessEqual(max(client.clock.waits), max(fsdpilot.RECONNECT_DELAYS))

    def test_a_dropped_link_that_cannot_reconnect_also_keeps_trying(self):
        client = self.make_client([True] + [fsdpilot.FAILURE_TRANSIENT] * 100)
        self.run_client(client)
        self.assertTrue(client.gave_up)
        self.assertGreater(client.connect_calls, 4)

    def test_the_backoff_grows(self):
        client = self.make_client([True] + [fsdpilot.FAILURE_TRANSIENT] * 100)
        self.run_client(client)
        waits = client.clock.waits
        self.assertEqual(waits[0], fsdpilot.RECONNECT_DELAYS[0])
        self.assertGreater(waits[3], waits[0])

    def test_an_auth_failure_while_reconnecting_stops_at_once(self):
        client = self.make_client([True, fsdpilot.FAILURE_IN_USE, fsdpilot.FAILURE_FATAL])
        self.run_client(client)
        self.assertEqual(client.connect_calls, 3)
        self.assertFalse(client.gave_up)
        # 掉线后一次、呼号被占后一次；认证失败之后不再等
        self.assertEqual(len(client.clock.waits), 2)

    def test_first_connection_in_use_is_waited_out(self):
        """客户端崩了或断网后重开：头一次登录撞上的就是自己的旧连接。"""
        client = self.make_client([fsdpilot.FAILURE_IN_USE] * 3 + [True, fsdpilot.FAILURE_FATAL])
        self.run_client(client)
        self.assertEqual(client.connect_calls, 5)

    def test_first_connection_that_cannot_reach_the_server_is_not_retried(self):
        client = self.make_client([fsdpilot.FAILURE_TRANSIENT])
        self.run_client(client)
        self.assertEqual(client.connect_calls, 1)
        self.assertEqual(client.states, [])

    def test_first_connection_with_a_bad_password_is_not_retried(self):
        client = self.make_client([fsdpilot.FAILURE_FATAL])
        self.run_client(client)
        self.assertEqual(client.connect_calls, 1)


class FsdLoginRefusalTest(unittest.TestCase):
    """登录被拒时按 can-fsd 的错误码分类（internal/fsd/errors.go）。"""

    def make_pilot(self):
        states = []
        pilot = fsdpilot.FSDPilot("fsd.example", "CCA1501", "1000", "pw",
                                  on_status=lambda state, message: states.append(state))
        return pilot, states

    def test_callsign_in_use_is_retryable(self):
        pilot, states = self.make_pilot()
        self.assertFalse(pilot._handle_packet(
            "$ERserver:CCA1501:1::Callsign already in use"))
        self.assertEqual(pilot._failure, fsdpilot.FAILURE_IN_USE)
        self.assertEqual(states, ['reconnecting'], "首连撞上呼号被占也不是终态")

    def test_bad_password_is_terminal_even_mid_reconnect(self):
        pilot, states = self.make_pilot()
        pilot._retryable = True       # 已经登录过、正在重连
        pilot._handle_packet("$ERserver:CCA1501:6::Invalid CID/password")
        self.assertEqual(pilot._failure, fsdpilot.FAILURE_FATAL)
        self.assertEqual(states, ['error'], "终态不能被翻成 reconnecting")

    def test_rate_limited_and_kicked_are_terminal(self):
        for packet in ("$ERserver:CCA1501:6::Too many failed attempts; try again later",
                       "$ERserver:CCA1501:13::Recently disconnected by a supervisor"):
            pilot, _ = self.make_pilot()
            pilot._handle_packet(packet)
            self.assertEqual(pilot._failure, fsdpilot.FAILURE_FATAL, packet)

    def test_server_full_is_transient(self):
        pilot, _ = self.make_pilot()
        pilot._handle_packet("$ERserver:CCA1501:12::Server full")
        self.assertEqual(pilot._failure, fsdpilot.FAILURE_TRANSIENT)

    def test_codes_match_can_fsd(self):
        path = os.path.join(CAN_FSD, "errors.go")
        if not os.path.exists(path):
            self.skipTest("边上没有 can-fsd")
        with open(path, encoding="utf-8") as f:
            source = f.read()
        codes = dict(re.findall(r"(ErrCode\w+)\s*=\s*(\d+)", source))
        self.assertEqual(codes["ErrCodeCallsignInUse"], fsdpilot.ERR_CALLSIGN_IN_USE)
        self.assertEqual(codes["ErrCodeServerFull"], fsdpilot.ERR_SERVER_FULL)


class _BrokenSocket:
    """sendall 一律失败；shutdown 之后 recv 读到 EOF，之前一直超时。"""

    def __init__(self):
        self.shut = threading.Event()

    def settimeout(self, timeout):
        self.timeout = timeout

    def sendall(self, data):
        raise ConnectionResetError(10054, "connection reset by peer")

    def recv(self, size):
        if self.shut.wait(self.timeout or 0.01):
            return b""
        raise socket.timeout()

    def shutdown(self, how):
        self.shut.set()

    def close(self):
        self.shut.set()


class FsdSendFailureTest(unittest.TestCase):
    """发不出去必须真的触发重连，不能停在"重连中"什么也不做。"""

    def test_a_failed_send_closes_the_socket(self):
        pilot = fsdpilot.FSDPilot("fsd.example", "CCA1501", "1000", "pw")
        sock = _BrokenSocket()
        pilot._sock = sock
        with self.assertLogs("fsd", logging.WARNING) as logs:
            self.assertFalse(pilot._send("#TMCCA1501:@21800:hello"))
        self.assertTrue(sock.shut.is_set(), "socket 该被 shutdown")
        self.assertIsInstance(pilot._broken, ConnectionResetError)
        self.assertIn("ConnectionResetError", "\n".join(logs.output))

    def test_a_failed_position_send_leads_to_a_reconnect(self):
        states = []
        pilot = fsdpilot.FSDPilot("fsd.example", "CCA1501", "1000", "pw",
                                  on_status=lambda state, message: states.append((state, message)))
        pilot.update_position({"latitude": 31.0, "longitude": 121.0, "altitude": 3000,
                               "groundspeed": 200, "pitch": 0.0, "bank": 0.0,
                               "heading": 90.0})
        calls = []

        def fake_connect():
            calls.append(1)
            pilot._broken = None
            if len(calls) > 1:
                return False
            pilot._sock = _BrokenSocket()
            pilot._logged_in = True
            return True

        pilot._connect = fake_connect
        waits = []
        pilot.stop_event = mock.MagicMock()
        pilot.stop_event.is_set.return_value = False
        # 第一次等待之前就说明已经走到"安排重连"了；返回 True 让 _run 结束
        pilot.stop_event.wait.side_effect = lambda delay: waits.append(delay) or True
        pilot.running = True
        worker = threading.Thread(target=pilot._run, daemon=True)
        worker.start()
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive(), "_loop 卡在一条坏掉的 socket 上了")
        self.assertEqual(waits, [fsdpilot.RECONNECT_DELAYS[0]], "没有安排重连")
        self.assertIn('reconnecting', [state for state, _ in states])

    def test_a_receive_error_is_logged_with_its_type(self):
        pilot = fsdpilot.FSDPilot("fsd.example", "CCA1501", "1000", "pw")

        class Resetting:
            def settimeout(self, timeout):
                pass

            def recv(self, size):
                raise ConnectionResetError(10054, "reset")

        pilot._sock = Resetting()
        with self.assertLogs("fsd", logging.INFO) as logs:
            self.assertEqual(pilot._read_packet(timeout=0.1), "")
        self.assertIn("ConnectionResetError", "\n".join(logs.output))
        self.assertIn("10054", "\n".join(logs.output))

    def test_eof_is_logged_as_eof(self):
        pilot = fsdpilot.FSDPilot("fsd.example", "CCA1501", "1000", "pw")

        class Closed:
            def settimeout(self, timeout):
                pass

            def recv(self, size):
                return b""

        pilot._sock = Closed()
        with self.assertLogs("fsd", logging.INFO) as logs:
            self.assertEqual(pilot._read_packet(timeout=0.1), "")
        self.assertIn("EOF", "\n".join(logs.output))


class FastPositionTest(unittest.TestCase):
    """协议版本 101：收 ^ / #SL / #ST，别人的 Velocity 客户端才是 5 Hz。

    报文取自 can-fsd docs/protocol.md 的示例，原样。
    """

    FAST = ("^DAL1151:40.6354992:-73.7795597:16.81:8.10:12582828:"
            "0.0015:0.0001:0.0005:0.0001:0.0000:-0.0029:-0.40")
    SLOW = ("#SLPRM4211:41.0844150:-73.1060790:26684.57:26961.66:4269806144:"
            "196.8918:-1.4936:174.1947:-0.0000:-0.0000:-0.0001:-2.11")
    STOPPED = "#STDAL2119:40.6453400:-73.7743400:13.56:-0.03:29360076:0.00"

    def setUp(self):
        self.table = traffic_module.TrafficTable()
        self.pilot = fsdpilot.FSDPilot("fsd.example", "CCA1501", "1000", "pw",
                                       traffic=self.table)
        self.pilot._send = lambda packet: True

    def test_logs_in_at_the_velocity_revision(self):
        self.assertEqual(fsdpilot.PROTO_REVISION, 101)

    def test_the_revision_is_the_one_can_fsd_forwards_fast_positions_to(self):
        path = os.path.join(CAN_FSD, "client.go")
        if not os.path.exists(path):
            self.skipTest("边上没有 can-fsd")
        with open(path, encoding="utf-8") as f:
            source = f.read()
        match = re.search(r"ProtoRevisionVelocity\s*=\s*(\d+)", source)
        self.assertEqual(int(match.group(1)), fsdpilot.PROTO_REVISION)

    def test_field_counts_match_can_fsd(self):
        """我们的解析下限和 can-fsd 的 minFields 一致，示例报文也够数。"""
        fast, stopped = can_fsd_fast_min_fields(self)
        self.assertEqual(fast, len(self.FAST.split(":")))
        self.assertEqual(fast, len(self.SLOW.split(":")))
        self.assertEqual(stopped, len(self.STOPPED.split(":")))

    def test_fast_position(self):
        self.assertTrue(self.pilot._handle_packet(self.FAST))
        aircraft = self.table.get("DAL1151")
        self.assertIsNotNone(aircraft)
        sample = aircraft.latest
        self.assertAlmostEqual(sample.latitude, 40.6354992)
        self.assertAlmostEqual(sample.longitude, -73.7795597)
        self.assertAlmostEqual(sample.altitude, 16.81)
        # X 向东、Y 向上、Z 向北；存的也是 (东, 上, 北)
        self.assertEqual(sample.velocity, (0.0015, 0.0001, 0.0005))
        attitude = fsdpilot.unpack_pbh(12582828)
        self.assertAlmostEqual(sample.heading, attitude["heading"])

    def test_rotation_agl_and_nose_wheel_are_kept(self):
        """角速度是弧度每秒，X/Z 按 xPilot 的方向（低头、左坡为正）取反。"""
        self.pilot._handle_packet(self.FAST)
        sample = self.table.get("DAL1151").latest
        pitch_rate, heading_rate, bank_rate = sample.rotation
        self.assertAlmostEqual(pitch_rate, -math.degrees(0.0001))
        self.assertAlmostEqual(heading_rate, 0.0)
        self.assertAlmostEqual(bank_rate, math.degrees(0.0029))
        self.assertAlmostEqual(sample.agl, 8.10)
        self.assertAlmostEqual(sample.nose_wheel, -0.40)

    def test_slow_variant_and_groundspeed(self):
        self.pilot._handle_packet(self.SLOW)
        sample = self.table.get("PRM4211").latest
        self.assertEqual(sample.velocity, (196.8918, -1.4936, 174.1947))
        expected = round(((174.1947 ** 2 + 196.8918 ** 2) ** 0.5) * 1.943844492)
        self.assertEqual(sample.groundspeed, expected)
        self.assertAlmostEqual(sample.nose_wheel, -2.11)

    def test_stopped_variant_has_zero_velocity(self):
        self.pilot._handle_packet(self.STOPPED)
        sample = self.table.get("DAL2119").latest
        self.assertEqual(sample.velocity, (0.0, 0.0, 0.0))
        self.assertEqual(sample.rotation, (0.0, 0.0, 0.0))
        self.assertEqual(sample.groundspeed, 0)
        self.assertAlmostEqual(sample.agl, -0.03)
        self.assertAlmostEqual(sample.nose_wheel, 0.0)

    def test_our_own_fast_position_is_ignored(self):
        self.pilot._handle_packet(self.FAST.replace("DAL1151", "CCA1501"))
        self.assertNotIn("CCA1501", self.table)

    def test_a_short_packet_is_ignored(self):
        self.assertTrue(self.pilot._handle_packet("^DAL1151:40.0:-73.0"))
        self.assertNotIn("DAL1151", self.table)

    def test_fast_position_first_sight_asks_for_the_type(self):
        sent = []
        self.pilot._send = lambda packet: sent.append(packet) or True
        self.pilot._handle_packet(self.FAST)
        self.assertIn("#SBCCA1501:DAL1151:PIR", sent)


def can_fsd_fast_min_fields(test):
    """can-fsd 的 minFields：(^ / #SL 的段数, #ST 的段数)。"""
    path = os.path.join(CAN_FSD, "packet.go")
    if not os.path.exists(path):
        test.skipTest("边上没有 can-fsd")
    with open(path, encoding="utf-8") as f:
        source = f.read()
    fast = re.search(r"case PacketFastPilotPosition, PacketFastPilotPositionSlow:"
                     r"\s*return (\d+)", source)
    stopped = re.search(r"case PacketFastPilotPositionStopped:\s*return (\d+)", source)
    return int(fast.group(1)), int(stopped.group(1))


def moving_snapshot(**changes):
    snapshot = {
        "latitude": 31.143400, "longitude": 121.805000,
        "altitude": 35000, "agl": 34000, "groundspeed": 450,
        "pitch": 2.0, "bank": -5.0, "heading": 271.0,
        "squawk": 2000, "xpdr_mode": 2, "on_ground": False,
        "velocity_east": -231.2, "velocity_up": 2.5, "velocity_north": 4.1,
        "pitch_rate": 0.5, "heading_rate": -3.0, "bank_rate": 1.5,
        "nose_wheel": 0.0,
    }
    snapshot.update(changes)
    return snapshot


def parked_snapshot():
    return moving_snapshot(
        altitude=13, agl=0, groundspeed=0, on_ground=True,
        velocity_east=0.0, velocity_up=0.0, velocity_north=0.0,
        pitch_rate=0.0, heading_rate=0.0, bank_rate=0.0, nose_wheel=12.5)


class FastPositionSendTest(unittest.TestCase):
    """发 ^ / #SL / #ST。节奏和内容照 xPilot 的 networkmanager.cpp。"""

    def setUp(self):
        self.sent = []
        self.pilot = fsdpilot.FSDPilot("fsd.example", "CCA1501", "1000", "pw")
        self.pilot._send = lambda packet: self.sent.append(packet) or True
        self.pilot.update_position(moving_snapshot())

    def test_send_fast_is_off_until_the_server_says_so(self):
        self.assertFalse(self.pilot.send_fast)

    def test_send_fast_on_and_off(self):
        self.assertTrue(self.pilot._handle_packet("$SFSERVER:CCA1501:1"))
        self.assertTrue(self.pilot.send_fast)
        self.assertTrue(self.pilot._handle_packet("$SFSERVER:CCA1501:0"))
        self.assertFalse(self.pilot.send_fast)

    def test_send_fast_for_someone_else_is_ignored(self):
        self.pilot._handle_packet("$SFSERVER:CES2345:1")
        self.assertFalse(self.pilot.send_fast)

    def test_the_fast_tick_sends_a_fast_position(self):
        self.assertEqual(self.pilot._send_fast_position(slow=False), "^")
        self.assertTrue(self.sent[0].startswith("^CCA1501:"))

    def test_fast_field_layout_matches_can_fsd(self):
        fast, _ = can_fsd_fast_min_fields(self)
        self.pilot._send_fast_position(slow=False)
        fields = self.sent[0].split(":")
        self.assertEqual(len(fields), fast)
        self.assertEqual(float(fields[1]), 31.1434)          # 纬度
        self.assertEqual(float(fields[2]), 121.805)          # 经度
        self.assertEqual(fields[3], "35000.00")              # 真高，英尺
        self.assertEqual(fields[4], "34000.00")              # 离地高，英尺
        attitude = fsdpilot.unpack_pbh(int(fields[5]))       # 和 `@` 同一个 PBH
        self.assertAlmostEqual(attitude["pitch"], 2.0, delta=0.4)
        self.assertAlmostEqual(attitude["bank"], -5.0, delta=0.4)
        self.assertAlmostEqual(attitude["heading"], 271.0, delta=0.4)
        # 速度：东、上、北，米每秒
        self.assertEqual([float(v) for v in fields[6:9]], [-231.2, 2.5, 4.1])
        # 角速度：弧度每秒，低头/右转/左坡为正（xPilot 发 -Q、R、-P）
        self.assertAlmostEqual(float(fields[9]), -math.radians(0.5), places=4)
        self.assertAlmostEqual(float(fields[10]), math.radians(-3.0), places=4)
        self.assertAlmostEqual(float(fields[11]), -math.radians(1.5), places=4)
        self.assertEqual(fields[12], "0.00")                 # 前轮角，度

    def test_a_parked_aircraft_sends_stopped(self):
        _, stopped = can_fsd_fast_min_fields(self)
        self.pilot.update_position(parked_snapshot())
        self.assertEqual(self.pilot._send_fast_position(slow=False), "#ST")
        fields = self.sent[0].split(":")
        self.assertTrue(fields[0].startswith("#STCCA1501"))
        self.assertEqual(len(fields), stopped)
        self.assertEqual(fields[6], "12.50")                 # 前轮角

    def test_the_slow_tick_sends_slow_only_when_moving(self):
        _ = self.pilot._send_fast_position(slow=True)
        self.assertTrue(self.sent[0].startswith("#SLCCA1501:"))
        self.assertEqual(len(self.sent[0].split(":")), 13)
        self.pilot.update_position(parked_snapshot())
        self.assertIsNone(self.pilot._send_fast_position(slow=True))
        self.assertEqual(len(self.sent), 1)

    def test_turning_fast_off_hands_over_to_slow(self):
        """关掉时补一个包：在动补 #SL（别人手里的速度不能被清零）。"""
        self.pilot._handle_packet("$SFSERVER:CCA1501:1")
        self.pilot._handle_packet("$SFSERVER:CCA1501:0")
        self.assertTrue(self.sent[-1].startswith("#SLCCA1501:"))

    def test_turning_fast_off_while_parked_sends_stopped(self):
        self.pilot.update_position(parked_snapshot())
        self.pilot._handle_packet("$SFSERVER:CCA1501:1")
        self.pilot._handle_packet("$SFSERVER:CCA1501:0")
        self.assertTrue(self.sent[-1].startswith("#STCCA1501:"))

    def test_a_repeated_flag_sends_nothing(self):
        self.pilot._handle_packet("$SFSERVER:CCA1501:0")
        self.assertEqual(self.sent, [])

    def test_nothing_without_a_snapshot(self):
        pilot = fsdpilot.FSDPilot("fsd.example", "CCA1501", "1000", "pw")
        pilot._send = self.sent.append
        self.assertIsNone(pilot._send_fast_position(slow=False))
        self.assertEqual(self.sent, [])

    def test_small_rates_count_as_stopped(self):
        self.assertTrue(fsdpilot.is_stopped(parked_snapshot()))
        self.assertTrue(fsdpilot.is_stopped(moving_snapshot(
            velocity_east=0.001, velocity_up=0.0, velocity_north=0.0,
            pitch_rate=0.0, heading_rate=0.1, bank_rate=0.0)))
        self.assertFalse(fsdpilot.is_stopped(moving_snapshot()))

    def test_what_we_send_is_what_we_receive(self):
        """我们发出去的 ^，另一个我们收进来，速度和角速度原样还原。"""
        self.pilot._send_fast_position(slow=False)
        table = traffic_module.TrafficTable()
        other = fsdpilot.FSDPilot("fsd.example", "CES2345", "1001", "pw",
                                  traffic=table)
        other._send = lambda packet: True
        other._handle_packet(self.sent[0])
        sample = table.get("CCA1501").latest
        self.assertEqual(sample.velocity, (-231.2, 2.5, 4.1))
        for got, want in zip(sample.rotation, (0.5, -3.0, 1.5)):
            self.assertAlmostEqual(got, want, delta=0.01)
        self.assertAlmostEqual(sample.agl, 34000.0)

    def test_the_login_sequence_sends_at_then_a_fast_position(self):
        """_loop 一开头：先 `@`，再一个快速位置（停着就是 #ST）。"""
        self.pilot.update_position(parked_snapshot())
        self.pilot.running = False          # 循环体一次都不跑
        self.pilot._loop()
        self.assertTrue(self.sent[0].startswith("@"))
        self.assertTrue(self.sent[1].startswith("#STCCA1501:"))


class VelocityTrafficTest(unittest.TestCase):
    """发过快速位置的飞机按上报的速度走；`@` 从此只是心跳。"""

    def setUp(self):
        self.table = traffic_module.TrafficTable()

    def fast(self, at, lat=30.0, lon=120.0, velocity=(0.0, 5.0, 100.0)):
        self.table.update_position("DAL1", latitude=lat, longitude=lon,
                                   altitude=10000.0, pitch=0.0, bank=0.0,
                                   heading=0.0, velocity=velocity, now=at)

    def slow(self, at, lat, lon=120.0, squawk=1234, groundspeed=250):
        self.table.update_position("DAL1", latitude=lat, longitude=lon,
                                   altitude=10000, pitch=0.0, bank=0.0,
                                   heading=0.0, squawk=squawk,
                                   groundspeed=groundspeed, now=at)

    def test_moves_along_the_velocity(self):
        self.fast(100.0)
        position = self.table.get("DAL1").state_at(101.0)
        self.assertAlmostEqual(position["latitude"],
                               30.0 + 100.0 / traffic_module.METRES_PER_DEGREE)
        self.assertAlmostEqual(position["altitude"],
                               10000.0 + 5.0 * traffic_module.FEET_PER_METRE)

    def test_slow_position_does_not_move_it(self):
        self.fast(100.0, lat=30.0)
        self.slow(100.1, lat=30.5, squawk=4321, groundspeed=260)
        aircraft = self.table.get("DAL1")
        self.assertEqual(aircraft.latest.latitude, 30.0, "`@` 不该替换带速度的样本")
        self.assertEqual(aircraft.motion.target[0], 30.0)
        self.assertEqual(aircraft.squawk, 4321, "应答机还是要跟 `@` 走")
        self.assertEqual(aircraft.latest.groundspeed, 260)

    def test_slow_positions_never_count_again(self):
        """xPilot 的 HaveVelocities：发过一次快速位置，`@` 就永远只是心跳。"""
        self.fast(100.0, lat=30.0, velocity=(0.0, 0.0, 0.0))
        self.slow(130.0, lat=30.5)
        aircraft = self.table.get("DAL1")
        self.assertEqual(aircraft.latest.latitude, 30.0)
        self.assertAlmostEqual(aircraft.state_at(131.0)["latitude"], 30.0)

    def test_slow_positions_keep_it_alive(self):
        self.fast(100.0)
        self.slow(110.0, lat=30.0)
        self.assertEqual(self.table.prune(now=100.0 + traffic_module.STALE_AFTER + 1), [])

    def test_vertical_speed_comes_from_the_velocity(self):
        self.fast(100.0, velocity=(0.0, 5.08, 0.0))
        self.assertAlmostEqual(self.table.get("DAL1").vertical_speed,
                               5.08 * traffic_module.FEET_PER_METRE * 60.0)

    def test_a_repeated_fast_snapshot_does_not_pull_it_back(self):
        """发送方快照没刷新、同一个位置又发了一遍：只换速度，不往回拽。"""
        self.fast(100.0)
        before = self.table.get("DAL1").state_at(100.2)["latitude"]
        self.fast(100.2)                      # 同一份快照
        aircraft = self.table.get("DAL1")
        self.assertEqual(aircraft.motion.error_remaining, 0.0)
        later = aircraft.state_at(100.4)["latitude"]
        self.assertAlmostEqual(later - before,
                               0.2 * 100.0 / traffic_module.METRES_PER_DEGREE)


class MotionTest(unittest.TestCase):
    """xPilot 的运动模型（network_aircraft.cpp），纯的积分器。"""

    def motion(self, heading=90.0, velocity=(0.0, 0.0, 0.0),
               rotation=(0.0, 0.0, 0.0), lat=30.0, lon=120.0, altitude=10000.0):
        motion = traffic_module.Motion()
        motion.receive(lat, lon, altitude, 0.0, 0.0, heading,
                       velocity=velocity, rotation=rotation)
        return motion

    def test_nothing_to_draw_before_the_first_sample(self):
        motion = traffic_module.Motion()
        motion.advance(1.0)
        self.assertIsNone(motion.state())

    def test_the_first_sample_is_drawn_where_it_is(self):
        state = self.motion(heading=271.0).state()
        self.assertEqual((state["latitude"], state["longitude"], state["altitude"]),
                         (30.0, 120.0, 10000.0))
        self.assertAlmostEqual(state["heading"], 271.0)

    def test_constant_velocity_is_a_straight_line(self):
        motion = self.motion(velocity=(0.0, 5.0, 100.0))
        for _ in range(60):
            motion.advance(1.0 / 30.0)
        state = motion.state()
        self.assertAlmostEqual(state["latitude"],
                               30.0 + 200.0 / traffic_module.METRES_PER_DEGREE, places=12)
        self.assertEqual(state["longitude"], 120.0)
        self.assertAlmostEqual(state["altitude"],
                               10000.0 + 10.0 * traffic_module.FEET_PER_METRE, places=9)

    def test_the_result_does_not_depend_on_the_frame_rate(self):
        """30 Hz 注入、10 Hz 推送、插件每帧：同样的时长，同样的结果。"""
        def run(frames):
            motion = self.motion(velocity=(80.0, 3.0, 60.0), rotation=(1.0, 3.0, -2.0))
            motion.advance(0.3)
            motion.receive(30.0002, 120.001, 10020.0, 1.0, 5.0, 92.0,
                           velocity=(82.0, 3.0, 58.0), rotation=(1.0, 3.0, -2.0))
            for _ in range(frames):
                motion.advance(3.0 / frames)
            return motion.state()
        coarse, fine = run(3), run(300)
        for key in ("latitude", "longitude", "altitude", "pitch", "bank", "heading"):
            self.assertAlmostEqual(coarse[key], fine[key], places=6, msg=key)

    def test_a_new_sample_does_not_move_the_drawn_aircraft(self):
        motion = self.motion(velocity=(100.0, 0.0, 0.0))
        motion.advance(0.2)
        before = motion.state()
        motion.receive(30.001, 120.003, 10050.0, 3.0, 10.0, 95.0,
                       velocity=(100.0, 0.0, 0.0))
        after = motion.state()
        for key in before:
            self.assertEqual(before[key], after[key], key)

    def test_an_error_converges_over_two_seconds(self):
        """误差 =（上报 − 画出）/ 2 秒，2 秒后正好落在上报位置的延长线上。"""
        velocity = (100.0, 2.0, 50.0)
        motion = self.motion(velocity=velocity)
        motion.advance(0.2)
        target = (30.0003, 120.0021, 10010.0)
        motion.receive(*target, 0.0, 0.0, 90.0, velocity=velocity)
        for _ in range(10):
            motion.advance(traffic_module.ERROR_TIME / 10)
        state = motion.state()
        cos_lat = math.cos(math.radians(30.0003))
        expected_lat = target[0] + 2.0 * 50.0 / traffic_module.METRES_PER_DEGREE
        expected_lon = target[1] + 2.0 * 100.0 / (traffic_module.METRES_PER_DEGREE * cos_lat)
        self.assertAlmostEqual(state["latitude"], expected_lat, places=8)
        self.assertAlmostEqual(state["longitude"], expected_lon, places=7)
        self.assertAlmostEqual(state["altitude"],
                               target[2] + 2.0 * 2.0 * traffic_module.FEET_PER_METRE,
                               places=6)

    def test_the_correction_is_continuous(self):
        """修正是速度，不是跳变：每一帧走的距离都差不多。"""
        motion = self.motion(velocity=(0.0, 0.0, 100.0))
        motion.advance(0.2)
        motion.receive(30.0005, 120.0, 10000.0, 0.0, 0.0, 90.0,
                       velocity=(0.0, 0.0, 100.0))
        previous = motion.state()["latitude"]
        steps = []
        for _ in range(90):
            motion.advance(1.0 / 30.0)
            latitude = motion.state()["latitude"]
            steps.append(latitude - previous)
            previous = latitude
        largest = max(steps) * traffic_module.METRES_PER_DEGREE
        self.assertLess(largest, (100.0 + 30.0) / 30.0,
                        "一帧走得比速度加误差速度还多，是跳过去的")

    def test_the_error_stops_after_two_seconds(self):
        motion = self.motion(velocity=(0.0, 0.0, 0.0))
        motion.receive(30.001, 120.0, 10000.0, 0.0, 0.0, 90.0)
        motion.advance(traffic_module.ERROR_TIME + 5.0)
        self.assertAlmostEqual(motion.state()["latitude"], 30.001, places=9)

    def test_rotation_rates_integrate(self):
        motion = self.motion(heading=90.0, rotation=(0.0, 3.0, 0.0))
        motion.advance(0.4)
        self.assertAlmostEqual(motion.state()["heading"], 91.2, places=6)

    def test_rotation_rates_are_cleared_after_half_a_second(self):
        """0.5 秒没有新样本：角速度清零，姿态落到最后上报的那个。"""
        motion = self.motion(heading=90.0, rotation=(0.0, 3.0, 0.0))
        motion.advance(0.6)
        self.assertEqual(motion.rotation, (0.0, 0.0, 0.0))
        self.assertAlmostEqual(motion.state()["heading"], 90.0, places=6)
        motion.advance(5.0)
        self.assertAlmostEqual(motion.state()["heading"], 90.0, places=6)

    def test_positional_velocity_survives_the_rotation_timeout(self):
        motion = self.motion(velocity=(0.0, 0.0, 100.0), rotation=(0.0, 3.0, 0.0))
        motion.advance(1.0)
        self.assertAlmostEqual(motion.state()["latitude"],
                               30.0 + 100.0 / traffic_module.METRES_PER_DEGREE)

    def test_a_late_sample_brings_no_rotation(self):
        """xPilot 在 UpdateVelocityVectors 里先清角速度：隔太久的样本的角速度不算。"""
        motion = self.motion(heading=90.0)
        motion.advance(1.0)
        motion.receive(30.0, 120.0, 10000.0, 0.0, 0.0, 100.0, rotation=(0.0, 3.0, 0.0))
        self.assertEqual(motion.rotation, (0.0, 0.0, 0.0))
        self.assertAlmostEqual(motion.state()["heading"], 100.0, places=6)

    def test_an_attitude_error_is_corrected_the_short_way_across_north(self):
        motion = self.motion(heading=359.0)
        motion.advance(0.2)
        motion.receive(30.0, 120.0, 10000.0, 0.0, 0.0, 1.0)
        motion.advance(0.2)
        heading = motion.state()["heading"]
        self.assertTrue(heading > 359.0 or heading < 1.0, heading)
        # 走一半（误差速度 2°/2 s，0.2 s 走 0.2°）
        self.assertAlmostEqual(heading, 359.2, places=6)

    def test_heading_rate_crosses_north(self):
        motion = self.motion(heading=359.0, rotation=(0.0, 5.0, 0.0))
        motion.advance(0.4)
        self.assertAlmostEqual(motion.state()["heading"], 1.0, places=6)

    def test_longitude_wraps_across_the_antimeridian(self):
        motion = self.motion(lon=179.9999, velocity=(100.0, 0.0, 0.0))
        motion.advance(1.0)
        longitude = motion.state()["longitude"]
        self.assertTrue(-180.0 <= longitude < -179.99, longitude)

    def test_bank_and_pitch_come_back_out(self):
        motion = traffic_module.Motion()
        motion.receive(30.0, 120.0, 0.0, 4.0, -20.0, 200.0)
        state = motion.state()
        self.assertAlmostEqual(state["pitch"], 4.0, places=9)
        self.assertAlmostEqual(state["bank"], -20.0, places=9)
        self.assertAlmostEqual(state["heading"], 200.0, places=9)


class SlowSenderTest(unittest.TestCase):
    """只发 `@` 的客户端：用相邻两个样本算速度，走同一条误差速度的路。"""

    def setUp(self):
        self.table = traffic_module.TrafficTable()

    def add(self, at, lat, lon=120.0, altitude=10000, heading=90.0):
        self.table.update_position("CES2345", latitude=lat, longitude=lon,
                                   altitude=altitude, pitch=0.0, bank=0.0,
                                   heading=heading, groundspeed=250, now=at)

    def at(self, now):
        return self.table.get("CES2345").state_at(now)

    def test_a_single_sample_is_held(self):
        self.add(100.0, 30.0)
        self.assertAlmostEqual(self.at(105.0)["latitude"], 30.0)

    def test_velocity_is_derived_from_two_samples(self):
        self.add(100.0, 30.0, altitude=10000)
        self.add(101.0, 30.01, altitude=10010)
        east, up, north = self.table.get("CES2345").motion.velocity
        self.assertAlmostEqual(north, 0.01 * traffic_module.METRES_PER_DEGREE, places=6)
        self.assertAlmostEqual(up, 10 * traffic_module.METRES_PER_FOOT, places=9)
        self.assertAlmostEqual(east, 0.0, places=9)

    def test_it_moves_smoothly_and_catches_up(self):
        """5 Hz 的 `@`：每个样本到达时画面不跳，最后跟上真实位置。"""
        speed = 0.001                        # 度每秒，匀速向北
        now = 100.0
        self.add(now, 30.0)
        previous = self.at(now)["latitude"]
        for tick in range(1, 51):
            now = 100.0 + tick * 0.2
            before = self.at(now)["latitude"]
            self.assertLess(before - previous, 0.2 * speed * 1.8,
                            f"{now} 之前走得太快")
            self.add(now, 30.0 + speed * tick * 0.2)
            self.assertEqual(before, self.at(now)["latitude"], f"{now} 跳了")
            previous = before
        # 起步那一下的误差按 2 秒的时间常数收敛，10 秒后剩不到 1%
        self.assertAlmostEqual(self.at(now)["latitude"], 30.0 + speed * 10.0, places=5)

    def test_heading_rate_takes_the_short_way(self):
        # 359° 到 1° 应当往前走 2°，不是倒着走 358°
        # 相隔要在 ROTATION_HOLD 以内，否则到达时角速度就被清掉了
        self.add(100.0, 30.0, heading=359.0)
        self.add(100.2, 30.0, heading=1.0)
        _, heading_rate, _ = self.table.get("CES2345").motion.rotation
        self.assertAlmostEqual(heading_rate, 10.0, places=6)
        heading = self.at(100.3)["heading"]
        self.assertTrue(heading > 359.0 or heading < 1.0, heading)

    def test_heading_rate_short_way_downwards(self):
        self.add(100.0, 30.0, heading=10.0)
        self.add(100.2, 30.0, heading=350.0)
        _, heading_rate, _ = self.table.get("CES2345").motion.rotation
        self.assertAlmostEqual(heading_rate, -100.0, places=6)

    def test_longitude_takes_the_short_way_across_the_antimeridian(self):
        # 179.98°E 到 -179.98° 是往前 0.04°，线性差值会横穿整个地球
        self.add(100.0, 30.0, lon=179.98)
        self.add(101.0, 30.0, lon=-179.98)
        east, _, _ = self.table.get("CES2345").motion.velocity
        self.assertGreater(east, 0.0)
        longitude = self.at(101.5)["longitude"]
        self.assertTrue(abs(longitude) > 179.9,
                        f"应当贴着 180° 经线，算出来是 {longitude}")

    def test_duplicate_timestamp_replaces_latest(self):
        # 同一时刻的两个包（同一次 recv 读出来的）：新的替换 latest，
        # 不当新的一段——两点间隔是零，速度会除零
        self.add(100.0, 30.0)
        self.add(100.0, 40.0)
        aircraft = self.table.get("CES2345")
        self.assertIsNone(aircraft.previous)
        self.assertEqual(aircraft.latest.latitude, 40.0)
        self.assertEqual(aircraft.motion.velocity, (0.0, 0.0, 0.0))

    def test_vertical_speed(self):
        self.add(100.0, 30.0, altitude=10000)
        self.add(101.0, 30.0, altitude=10010)
        self.assertAlmostEqual(self.table.get("CES2345").vertical_speed, 600.0, places=3)

    def test_state_at_an_earlier_time_does_not_rewind(self):
        self.add(100.0, 30.0)
        self.add(101.0, 30.01)
        later = self.at(102.0)["latitude"]
        self.assertEqual(self.at(50.0)["latitude"], later)


class DuplicateSampleTest(unittest.TestCase):
    """发送方把同一份快照重复发几次，不能被当成"停下来了"。"""

    def setUp(self):
        self.table = traffic_module.TrafficTable()

    def add(self, at, lat, altitude=10000):
        self.table.update_position("CES1", latitude=lat, longitude=120.0,
                                   altitude=altitude, pitch=0.0, bank=0.0,
                                   heading=0.0, groundspeed=250, now=at)

    def test_a_repeated_snapshot_is_not_a_new_sample(self):
        self.add(100.0, 30.0)
        self.add(101.0, 30.01)
        self.add(101.2, 30.01)          # 同一份快照又来了
        self.add(101.4, 30.01)
        aircraft = self.table.get("CES1")
        self.assertEqual(aircraft.previous.time, 100.0)
        self.assertEqual(aircraft.latest.time, 101.0)
        # 两点之间的速度还在，飞机继续往前走，而不是停在 30.01
        self.assertGreater(aircraft.motion.velocity[2], 0.0)

    def test_the_next_real_update_follows_on(self):
        self.add(100.0, 30.0)
        self.add(101.0, 30.01)
        self.add(101.2, 30.01)
        self.add(102.0, 30.02)
        aircraft = self.table.get("CES1")
        self.assertEqual(aircraft.previous.time, 101.0)
        self.assertEqual(aircraft.latest.time, 102.0)

    def test_an_aircraft_that_really_stopped_does_stop(self):
        self.add(100.0, 30.0)
        self.add(101.0, 30.01)
        later = 101.0 + traffic_module.DUPLICATE_WINDOW + 0.1
        self.add(later, 30.01)
        aircraft = self.table.get("CES1")
        self.assertEqual(aircraft.latest.time, later)
        self.assertEqual(aircraft.motion.velocity[2], 0.0)
        settled = aircraft.state_at(later + traffic_module.ERROR_TIME + 1)
        self.assertAlmostEqual(settled["latitude"], 30.01, places=9)

    def test_an_altitude_change_is_not_a_duplicate(self):
        self.add(100.0, 30.0, altitude=10000)
        self.add(100.2, 30.0, altitude=10010)
        self.assertEqual(self.table.get("CES1").latest.altitude, 10010)

    def test_repeats_keep_the_aircraft_alive(self):
        """重复样本不进 latest，但对方还在线，prune 不能把它清掉。"""
        self.add(100.0, 30.0)
        for i in range(1, 100):
            self.add(100.0 + i * 0.2, 30.0)
        now = 100.0 + 99 * 0.2 + 1.0
        self.assertEqual(self.table.prune(now=now), [])


class TrafficClockTest(unittest.TestCase):
    """时间戳用高分辨率单调钟，打平的时候新的样本胜出。"""

    def setUp(self):
        self.table = traffic_module.TrafficTable()

    def add(self, at, lat):
        self.table.update_position("CES1", latitude=lat, longitude=120.0,
                                   altitude=10000, pitch=0.0, bank=0.0,
                                   heading=0.0, now=at)

    def test_the_default_clock_is_perf_counter(self):
        before = time.perf_counter()
        self.table.update_position("CES1", latitude=30.0, longitude=120.0,
                                   altitude=10000, pitch=0.0, bank=0.0, heading=0.0)
        after = time.perf_counter()
        self.assertTrue(before <= self.table.get("CES1").latest.time <= after)

    def test_a_tied_timestamp_keeps_the_newer_sample(self):
        self.add(100.0, 30.0)
        self.add(100.2, 30.1)
        self.add(100.2, 30.2)           # 同一次 recv 读出来的下一个包
        aircraft = self.table.get("CES1")
        self.assertEqual(aircraft.latest.latitude, 30.2)
        self.assertEqual(aircraft.previous.latitude, 30.0)
        self.assertGreater(aircraft.latest.time, aircraft.previous.time)
        # 速度按 previous → 新的 latest 算
        self.assertAlmostEqual(aircraft.motion.velocity[2],
                               1.0 * traffic_module.METRES_PER_DEGREE, places=6)

    def test_the_first_type_request_is_not_skipped_early_in_uptime(self):
        """单调钟的零点不固定，info_requested 不能从 0 起算。"""
        asked = []
        table = traffic_module.TrafficTable(on_request_info=asked.append)
        table.update_position("CES1", latitude=30.0, longitude=120.0, altitude=0,
                              pitch=0.0, bank=0.0, heading=0.0, now=5.0)
        self.assertEqual(asked, ["CES1"])


class SimVarPollingTest(unittest.TestCase):
    """每轮只串着读会动的那几个 SimVar，其余轮流读。"""

    def setUp(self):
        self.link = simlink.SimLink()
        self.read = []

        def get(_, simvar):
            self.read.append(simvar)
            return 1.0
        self.link._requests = type("R", (), {"get": get})()

    def test_the_first_round_reads_everything(self):
        self.link._poll()
        self.assertEqual(sorted(self.read), sorted(simlink.SIMVARS.values()))

    def test_later_rounds_read_the_fast_set_plus_a_slice(self):
        self.link._poll()
        self.read.clear()
        self.link._poll()
        fast = {simlink.SIMVARS[name] for name in simlink.FAST_SIMVARS}
        self.assertTrue(fast <= set(self.read))
        self.assertEqual(len(self.read), len(fast) + simlink.SLOW_PER_POLL)

    def test_every_slow_simvar_comes_round(self):
        self.link._poll()
        self.read.clear()
        rounds = -(-len(simlink.SLOW_SIMVARS) // simlink.SLOW_PER_POLL)
        for _ in range(rounds):
            self.link._poll()
        slow = {simlink.SIMVARS[name] for name in simlink.SLOW_SIMVARS}
        self.assertTrue(slow <= set(self.read))

    def test_the_position_fields_are_all_fast(self):
        for name in ("latitude", "longitude", "altitude", "pitch", "bank",
                     "heading", "groundspeed", "on_ground", "pressure_altitude",
                     "velocity_east", "velocity_up", "velocity_north",
                     "pitch_rate", "heading_rate", "bank_rate"):
            self.assertIn(name, simlink.FAST_SIMVARS)

    def test_all_failing_in_a_partial_round_is_still_failed(self):
        self.link._poll()

        def boom(_, simvar):
            raise OSError("连接没了")
        self.link._requests = type("R", (), {"get": boom})()
        self.assertIs(self.link._poll(), simlink.FAILED)


class NetworkAltitudeTest(unittest.TestCase):
    """位置包报网络高度，他机高度按本机温度误差修正（xPilot AdjustIncomingAltitude）。"""

    OWN = {
        "latitude": 31.2, "longitude": 121.5,
        "altitude": 39700, "network_altitude": 38000,
        "pressure_altitude": 38000, "temperature_error": -1700,
        "pressure_delta": 0, "agl": 39000.0, "groundspeed": 450,
        "pitch": 0.0, "bank": 0.0, "heading": 90.0, "squawk": 2000,
        "xpdr_mode": 2, "on_ground": False,
        "velocity_east": 230.0, "velocity_up": 0.0, "velocity_north": 0.0,
        "pitch_rate": 0.0, "heading_rate": 0.0, "bank_rate": 0.0,
        "nose_wheel": 0.0,
    }
    FAST = ("^CES2345:31.3:121.6:{alt}:{alt}:12582828:"
            "230.0:0.0:0.0:0.0:0.0:0.0:0.0")

    def setUp(self):
        self.table = traffic_module.TrafficTable()
        self.sent = []
        self.pilot = fsdpilot.FSDPilot("fsd.example", "CCA1501", "1000", "pw",
                                       traffic=self.table)
        self.pilot._send = lambda packet: self.sent.append(packet) or True

    def _at(self, alt):
        pbh = fsdpilot.pack_pbh(0.0, 0.0, 90.0)
        return f"@N:CES2345:2000:1:31.3:121.6:{alt}:450:{pbh}:0"

    def _received(self):
        return self.table.get("CES2345").latest.altitude

    def test_position_packet_carries_the_network_altitude_and_the_delta(self):
        own = dict(self.OWN, altitude=38334, network_altitude=38334,
                   temperature_error=0, pressure_delta=-334)
        self.pilot.update_position(own)
        self.pilot._send_position()
        fields = self.sent[0].split(":")
        self.assertEqual(fields[6], "38334")
        self.assertEqual(fields[9], "-334")
        self.assertEqual(int(fields[6]) + int(fields[9]), 38000)

    def test_warm_atmosphere_sends_the_flight_level(self):
        self.pilot.update_position(dict(self.OWN))
        self.pilot._send_position()
        fields = self.sent[0].split(":")
        self.assertEqual(fields[6], "38000")
        self.assertEqual(fields[9], "0")

    def test_fast_packets_carry_the_network_altitude(self):
        self.pilot.update_position(dict(self.OWN))
        for kind in ("^", "#SL", "#ST"):
            packet = fsdpilot.fast_position_packet(kind, "CCA1501", self.OWN)
            self.assertEqual(packet.split(":")[3], "38000.00", kind)

    def test_incoming_position_is_adjusted_by_our_temperature_error(self):
        self.pilot.update_position(dict(self.OWN))
        self.pilot._handle_packet(self._at(38000))
        self.assertEqual(self._received(), 39700)

    def test_incoming_fast_position_is_adjusted_too(self):
        self.pilot.update_position(dict(self.OWN))
        self.pilot._handle_packet(self.FAST.format(alt="40000.00"))
        self.assertAlmostEqual(self._received(), 41700.0)

    def test_distant_traffic_is_left_alone(self):
        self.pilot.update_position(dict(self.OWN))
        self.pilot._handle_packet(self._at(31000))
        self.assertEqual(self._received(), 31000)

    def test_without_our_own_snapshot_nothing_is_adjusted(self):
        self.pilot._handle_packet(self._at(38000))
        self.assertEqual(self._received(), 38000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
