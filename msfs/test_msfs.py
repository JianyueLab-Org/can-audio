"""MSFS 版特有部分的单元测试。

    python -m unittest test_msfs -v

不连模拟器、不连服务器、不碰音频。FSD 协议、他机插值那些和 xpc 共用的部分由
xpc/test_xpc.py 覆盖，这里只测换掉的那一层：SimConnect 的单位换算、aircraft.cfg
解析、机型匹配，以及他机注入里那段自己接管的 objectID 关联。
"""

import hashlib
import json
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
                    "com1", "com2", "com1_power", "on_ground", "pressure_delta"}
        self.assertTrue(required.issubset(self.link.snapshot()))


class PressureAltitudeTest(unittest.TestCase):
    """位置包最后一个字段：气压高度减真高。

    实报的现象是"座舱高度表 35000，服务器上 34000"，差了一千英尺。原因不是
    单位错了，是两个高度本来就不是一回事：PLANE_ALTITUDE 是真高，高度表读的
    是按窗口里那个气压算出来的指示高度。以前这个字段写死 0，管制端于是直接
    拿真高当高度显示。
    """

    def test_standard_setting_means_indicated_is_pressure_altitude(self):
        # 拨 29.92 时指示高度就是气压高度，差值只剩指示高度和真高之差
        self.assertEqual(
            simlink.pressure_delta(35000.0, 29.92, 34000), 1000)

    def test_no_difference_reports_zero(self):
        self.assertEqual(simlink.pressure_delta(3000.0, 29.92, 3000), 0)

    def test_low_pressure_day(self):
        # 拨 28.92（比标准低一寸），气压高度比指示高度高一千英尺
        self.assertEqual(
            simlink.pressure_delta(35000.0, 28.92, 35000), 1000)

    def test_high_pressure_day(self):
        self.assertEqual(
            simlink.pressure_delta(0.0, 30.92, 0), -1000)

    def test_missing_readings_do_not_correct(self):
        """读不到就退回修正前的行为，不能瞎猜。"""
        self.assertEqual(simlink.pressure_delta(None, 29.92, 35000), 0)
        self.assertEqual(simlink.pressure_delta(35000.0, None, 35000), 0)

    def test_a_nonsense_barometer_does_not_correct(self):
        """SimVar 读不到时会是 0，(29.92-0)*1000 会把飞机在雷达上挪三万英尺。"""
        self.assertEqual(simlink.pressure_delta(35000.0, 0.0, 35000), 0)
        self.assertEqual(simlink.pressure_delta(35000.0, 99.0, 35000), 0)

    def test_snapshot_carries_it(self):
        link = simlink.SimLink()
        link.values = {
            "latitude": 31.0, "longitude": 121.0,
            "altitude": 34000.0,
            "indicated_altitude": 35000.0, "baro_setting": 29.92,
        }
        self.assertEqual(link.snapshot()["pressure_delta"], 1000)

    def test_snapshot_without_the_new_simvars_still_works(self):
        """老的 Python-SimConnect 取不到这两个值时不能连整份数据一起丢。"""
        link = simlink.SimLink()
        link.values = {"latitude": 31.0, "longitude": 121.0, "altitude": 34000.0}
        snapshot = link.snapshot()
        self.assertEqual(snapshot["altitude"], 34000)
        self.assertEqual(snapshot["pressure_delta"], 0)


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
    """

    SHARED = ("voice.py", "traffic.py", "mumblecompat.py", "ptt.py",
              "theme.py", "update.py", "chime.py", "observer.py")

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
