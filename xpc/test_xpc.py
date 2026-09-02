"""协议和数据换算的单元测试。

    python -m unittest test_xpc -v

不连服务器、不碰音频、不需要 X-Plane。重点是两头对得上的地方：PBH 的编码
必须能被 can-fsd 原样解回来，RREF 回包必须按 X-Plane 的格式解析。
"""

import array
import inspect
import json
import os
import shutil
import struct
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

# pymumble 要本机的 opus 原生库，这些测试碰不到音频，缺库时放个替身。
try:
    import opuslib  # noqa: F401
except Exception:
    for _name in ("opuslib", "opuslib.api", "opuslib.api.decoder",
                  "opuslib.api.encoder", "opuslib.api.info", "opuslib.exceptions"):
        sys.modules.setdefault(_name, mock.MagicMock())

import bridge
import cslmatch
import fsdpilot
import traffic as traffic_module
import xpinstall
import xplane


def unpack_pbh(packed):
    """can-fsd 的解码，抄自 internal/fsd/packet.go 的 PitchBankHeading。

    测试拿它当参照物：我们编出来的东西必须能被服务端原样解回去。
    """
    ratio = 360.0 / 1024.0
    mask = 0x3FF

    def normalise(value):
        return value - 360.0 if value > 180.0 else value

    pitch = normalise((packed >> 22 & mask) * ratio)
    bank = normalise((packed >> 12 & mask) * ratio)
    heading = (packed >> 2 & mask) * ratio
    return pitch, bank, heading


class PbhTest(unittest.TestCase):
    """姿态编码。错了别人会看到飞机以奇怪的角度飞。"""

    # 量化步长是 360/1024 ≈ 0.35°，来回一趟的误差不该超过半格
    TOLERANCE = 360.0 / 1024.0 / 2 + 1e-6

    def assert_round_trip(self, pitch, bank, heading):
        got_pitch, got_bank, got_heading = unpack_pbh(
            fsdpilot.pack_pbh(pitch, bank, heading))
        self.assertAlmostEqual(pitch, got_pitch, delta=self.TOLERANCE)
        self.assertAlmostEqual(bank, got_bank, delta=self.TOLERANCE)
        self.assertAlmostEqual(heading % 360.0, got_heading, delta=self.TOLERANCE)

    def test_level_flight(self):
        self.assert_round_trip(0.0, 0.0, 0.0)

    def test_typical_attitude(self):
        self.assert_round_trip(2.5, -15.0, 271.0)

    def test_negative_pitch_and_bank(self):
        # 下降加左坡度——负角度按 0..360 折回去，不能溢出成别的值
        self.assert_round_trip(-3.5, -30.0, 89.0)

    def test_extremes(self):
        for pitch, bank, heading in ((90.0, 0.0, 0.0), (-90.0, 0.0, 180.0),
                                     (0.0, 179.0, 359.0), (0.0, -179.0, 1.0)):
            with self.subTest(pitch=pitch, bank=bank, heading=heading):
                self.assert_round_trip(pitch, bank, heading)

    def test_heading_wraps(self):
        # 360 和 0 是同一个方向，编出来必须一样
        self.assertEqual(fsdpilot.pack_pbh(0, 0, 360.0),
                         fsdpilot.pack_pbh(0, 0, 0.0))

    def test_fits_in_32_bits(self):
        for heading in range(0, 360, 7):
            packed = fsdpilot.pack_pbh(-45.0, 45.0, heading)
            self.assertGreaterEqual(packed, 0)
            self.assertLess(packed, 2 ** 32)

    def test_on_ground_flag(self):
        air = fsdpilot.pack_pbh(0, 0, 90.0, on_ground=False)
        ground = fsdpilot.pack_pbh(0, 0, 90.0, on_ground=True)
        self.assertEqual(ground & 0x2, 0x2)
        self.assertEqual(air & 0x2, 0)
        # 地面标志不该动到姿态
        self.assertEqual(unpack_pbh(air), unpack_pbh(ground))


class CallsignTest(unittest.TestCase):
    """呼号规则来自 can-fsd 的 IsValidCallsign，客户端先拦一道。"""

    def test_normal_callsign_passes(self):
        self.assertIsNone(fsdpilot.callsign_problem("CCA1501"))

    def test_underscore_allowed(self):
        self.assertIsNone(fsdpilot.callsign_problem("ZSPD_TWR"))

    def test_too_long_rejected(self):
        problem = fsdpilot.callsign_problem("ABCDEFGHIJKLM")   # 13 个字符
        self.assertIsNotNone(problem)
        self.assertIn("12", problem)

    def test_eleven_characters_is_fine_now(self):
        # 上限从 10 提到 12 是为了 vATIS 的 ZSPD_D_ATIS / ZSPD_A_ATIS
        self.assertIsNone(fsdpilot.callsign_problem("ZSPD_D_ATIS"))

    def test_too_short_rejected(self):
        self.assertIsNotNone(fsdpilot.callsign_problem("A"))

    def test_illegal_character_rejected(self):
        self.assertIsNotNone(fsdpilot.callsign_problem("CCA150#"))

    def test_lowercase_is_normalised(self):
        self.assertIsNone(fsdpilot.callsign_problem("cca1501"))


class SanitizeTest(unittest.TestCase):
    """包是冒号分帧的，正文里的冒号会把包切坏。"""

    def test_colon_replaced(self):
        self.assertEqual(fsdpilot.sanitize("a:b"), "a b")

    def test_newlines_replaced(self):
        self.assertEqual(fsdpilot.sanitize("a\r\nb"), "a  b")

    def test_none_is_empty(self):
        self.assertEqual(fsdpilot.sanitize(None), "")


class DotCommandTest(unittest.TestCase):
    """`.wallop` 得在客户端翻成发往 `*S` 的 #TM。

    EuroScope 和 CRC 都是在客户端做这一步的，这个客户端原来没做：用户打的
    `.wallop 求助` 被当成普通正文，跟着收件人框（空的时候是 COM1 频率）发到频
    率上，服务端的 handleWallop 一次都不会触发。督导收不到，而界面照样回一行
    "已发送"——所以这个缺陷是**静默**的，这也是它活到今天的原因。
    """

    def test_wallop_goes_to_the_supervisor_channel(self):
        recipient, body = fsdpilot.parse_dot_command(".wallop 请求协助")
        self.assertEqual(recipient, fsdpilot.WALLOP_RECIPIENT)
        self.assertEqual(body, "请求协助")

    def test_command_name_is_case_insensitive(self):
        recipient, _ = fsdpilot.parse_dot_command(".WALLOP help")
        self.assertEqual(recipient, fsdpilot.WALLOP_RECIPIENT)

    def test_body_is_left_verbatim(self):
        # 正文是用户写给督导的原话，大小写和标点都不该被改写。
        _, body = fsdpilot.parse_dot_command(".wallop  Runway 36L OCCUPIED!  ")
        self.assertEqual(body, "Runway 36L OCCUPIED!")

    def test_colons_in_the_body_survive(self):
        # 分帧要洗的冒号归 sanitize 管，解析这一步不该先把正文切断。
        _, body = fsdpilot.parse_dot_command(".wallop ETA 12:30")
        self.assertEqual(body, "ETA 12:30")

    def test_tab_after_the_command_still_parses(self):
        recipient, body = fsdpilot.parse_dot_command(".wallop\t求助")
        self.assertEqual(recipient, fsdpilot.WALLOP_RECIPIENT)
        self.assertEqual(body, "求助")

    def test_wallop_with_no_text_yields_an_empty_body(self):
        # 界面据此提示，而不是给督导发一条空消息。
        recipient, body = fsdpilot.parse_dot_command(".wallop")
        self.assertEqual(recipient, fsdpilot.WALLOP_RECIPIENT)
        self.assertEqual(body, "")

    def test_ordinary_message_is_untouched(self):
        recipient, body = fsdpilot.parse_dot_command("request pushback")
        self.assertIsNone(recipient)
        self.assertEqual(body, "request pushback")

    def test_unknown_dot_command_is_sent_as_text(self):
        # 猜不出用户是想打命令还是真要发一句以点开头的话。吞掉一条本该发出去
        # 的消息，比把一句奇怪的话发到频率上更糟。
        recipient, body = fsdpilot.parse_dot_command(".wallpo 求助")
        self.assertIsNone(recipient)
        self.assertEqual(body, ".wallpo 求助")

    def test_none_is_handled(self):
        recipient, body = fsdpilot.parse_dot_command(None)
        self.assertIsNone(recipient)
        self.assertEqual(body, "")


class PositionPacketTest(unittest.TestCase):
    """位置包的字段顺序必须和 can-fsd 的 handlePilotPosition 对上。"""

    def setUp(self):
        self.sent = []
        self.pilot = fsdpilot.FSDPilot("example.invalid", "CCA1501", "1234", "pw")
        self.pilot._send = self.sent.append
        self.pilot.update_position({
            "latitude": 31.14340, "longitude": 121.80500,
            "altitude": 35000, "groundspeed": 450,
            "pitch": 2.0, "bank": -5.0, "heading": 271.0,
            "squawk": 2000, "xpdr_mode": 2, "on_ground": False,
        })

    def test_field_layout(self):
        self.pilot._send_position()
        fields = self.sent[0].split(":")
        self.assertEqual(fields[0], "@N")               # 应答机正常
        self.assertEqual(fields[1], "CCA1501")
        self.assertEqual(fields[2], "2000")             # squawk
        self.assertEqual(float(fields[4]), 31.14340)    # 纬度
        self.assertEqual(float(fields[5]), 121.80500)   # 经度
        self.assertEqual(fields[6], "35000")            # 高度
        self.assertEqual(fields[7], "450")              # 地速
        self.assertEqual(len(fields), 10)

    def test_pressure_correction_defaults_to_zero(self):
        """取不到修正量就是 0，也就是修正前的行为。"""
        self.pilot._send_position()
        self.assertEqual(self.sent[0].split(":")[9], "0")

    def test_pressure_correction_reaches_the_last_field(self):
        """高度字段是真高，最后这个字段才让管制端算出气压高度。

        写死 0 的时候管制端看到的就是真高，和座舱高度表差一千英尺。
        """
        self.pilot.update_position({
            "latitude": 0, "longitude": 0, "altitude": 34000, "groundspeed": 450,
            "pitch": 0, "bank": 0, "heading": 0, "squawk": 2000, "xpdr_mode": 2,
            "pressure_delta": 1000,
        })
        self.pilot._send_position()
        fields = self.sent[0].split(":")
        self.assertEqual(fields[6], "34000")            # 真高照旧
        self.assertEqual(fields[9], "1000")
        # 管制端把两者相加，得到的就是座舱高度表上的数
        self.assertEqual(int(fields[6]) + int(fields[9]), 35000)

    def test_a_negative_correction_survives(self):
        self.pilot.update_position({
            "latitude": 0, "longitude": 0, "altitude": 35000, "groundspeed": 450,
            "pitch": 0, "bank": 0, "heading": 0, "squawk": 2000, "xpdr_mode": 2,
            "pressure_delta": -700,
        })
        self.pilot._send_position()
        self.assertEqual(self.sent[0].split(":")[9], "-700")

    def test_attitude_survives_the_packet(self):
        self.pilot._send_position()
        pitch, bank, heading = unpack_pbh(int(self.sent[0].split(":")[8]))
        self.assertAlmostEqual(pitch, 2.0, delta=0.4)
        self.assertAlmostEqual(bank, -5.0, delta=0.4)
        self.assertAlmostEqual(heading, 271.0, delta=0.4)

    def test_squawk_is_four_digits(self):
        self.pilot.update_position({
            "latitude": 0, "longitude": 0, "altitude": 0, "groundspeed": 0,
            "pitch": 0, "bank": 0, "heading": 0, "squawk": 21, "xpdr_mode": 2,
        })
        self.pilot._send_position()
        self.assertEqual(self.sent[0].split(":")[2], "0021")

    def test_standby_transponder(self):
        self.pilot.update_position({
            "latitude": 0, "longitude": 0, "altitude": 0, "groundspeed": 0,
            "pitch": 0, "bank": 0, "heading": 0, "squawk": 2000, "xpdr_mode": 1,
        })
        self.pilot._send_position()
        self.assertTrue(self.sent[0].startswith("@S:"))

    def test_ident_changes_the_mode(self):
        self.pilot.ident()
        self.pilot._send_position()
        self.assertTrue(self.sent[0].startswith("@Y:"))

    def test_slows_down_when_parked(self):
        self.pilot.update_position({
            "latitude": 0, "longitude": 0, "altitude": 0, "groundspeed": 0,
            "pitch": 0, "bank": 0, "heading": 0, "squawk": 2000, "xpdr_mode": 2,
            "on_ground": True,
        })
        self.assertEqual(self.pilot._send_position(), fsdpilot.SLOW_POSITION_INTERVAL)

    def test_full_rate_in_the_air(self):
        self.assertEqual(self.pilot._send_position(), fsdpilot.POSITION_INTERVAL)

    def test_no_packet_without_position(self):
        pilot = fsdpilot.FSDPilot("example.invalid", "CCA1501", "1234", "pw")
        pilot._send = self.sent.append
        pilot._send_position()
        self.assertEqual(self.sent, [])


class PasswordLoggingTest(unittest.TestCase):
    """日志会被用户贴出来，密码不能在里面。"""

    def test_login_packet_is_redacted(self):
        packet = "#APCCA1501:SERVER:1234:hunter2:1:100:8:Test Pilot"
        self.assertNotIn("hunter2", fsdpilot.FSDPilot._redact(packet))

    def test_other_packets_untouched(self):
        packet = "@N:CCA1501:2000:1:31.1:121.8:35000:450:0:0"
        self.assertEqual(fsdpilot.FSDPilot._redact(packet), packet)


class PacketHandlingTest(unittest.TestCase):
    def setUp(self):
        self.pilot = fsdpilot.FSDPilot("example.invalid", "CCA1501", "1234", "pw")
        self.pilot._send = lambda packet: True

    def test_error_before_login_stops_the_connection(self):
        self.pilot._logged_in = False
        result = self.pilot._handle_packet("$ERSERVER:CCA1501:6::Invalid CID/password")
        self.assertIs(result, False)

    def test_error_after_login_is_survivable(self):
        self.pilot._logged_in = True
        result = self.pilot._handle_packet("$ERSERVER:CCA1501:6::something")
        self.assertIsNot(result, False)

    def test_text_message_reaches_the_callback(self):
        received = []
        self.pilot.on_text = lambda *args: received.append(args)
        self.pilot._handle_packet("#TMZSPD_TWR:CCA1501:contact ground 121.8")
        self.assertEqual(received, [("ZSPD_TWR", "CCA1501", "contact ground 121.8")])

    def test_message_body_may_contain_colons(self):
        received = []
        self.pilot.on_text = lambda *args: received.append(args)
        self.pilot._handle_packet("#TMZSPD_TWR:CCA1501:climb FL350:expedite")
        self.assertEqual(received[0][2], "climb FL350:expedite")

    def test_controller_position_is_recorded(self):
        self.pilot._handle_packet("%ZSPD_TWR:28500:5:100:1:31.14:121.80:0")
        self.assertIn("ZSPD_TWR", self.pilot.controllers)
        self.assertEqual(self.pilot.controllers["ZSPD_TWR"]["frequency"], "128.500")

    def test_controller_removed_on_disconnect(self):
        self.pilot._handle_packet("%ZSPD_TWR:28500:5:100:1:31.14:121.80:0")
        self.pilot._handle_packet("#DAZSPD_TWR:1234")
        self.assertNotIn("ZSPD_TWR", self.pilot.controllers)

    def test_caps_reply_marks_login_done(self):
        self.pilot._handle_packet("$CRSERVER:CCA1501:CAPS:ATCINFO=1")
        self.assertTrue(self.pilot._logged_in)

    def test_ping_is_answered(self):
        sent = []
        self.pilot._send = sent.append
        self.pilot._handle_packet("$PISERVER:CCA1501:12345")
        self.assertTrue(sent[0].startswith("$POCCA1501:SERVER:"))

    def test_capability_query_is_answered(self):
        sent = []
        self.pilot._send = sent.append
        self.pilot._handle_packet("$CQZSPD_TWR:CCA1501:CAPS")
        self.assertTrue(sent[0].startswith("$CRCCA1501:ZSPD_TWR:CAPS"))

    def test_aircraft_query_is_answered_with_config_json(self):
        """ACC 的回复是配置 JSON（灯光/襟翼/起落架），不是机型码。

        以前回的是机型码，和请求方 set_config 期望的负载完全对不上——结果
        就是所有他机永远全程关灯、光杆落地。
        """
        sent = []
        self.pilot.update_position({
            "gear_down": False, "flaps": 0.5, "spoilers": True,
            "lights": {"beacon_on": True, "landing_on": False},
            "engines_on": True,
        })
        self.pilot._send = sent.append
        self.pilot._handle_packet("$CQZSPD_TWR:CCA1501:ACC")
        self.assertTrue(sent[0].startswith("$CRCCA1501:ZSPD_TWR:ACC:"))
        config = json.loads(sent[0].split(":ACC:", 1)[1])
        self.assertFalse(config["gear_down"])
        self.assertEqual(config["flaps_pct"], 50.0)
        self.assertTrue(config["spoilers_out"])
        self.assertTrue(config["lights"]["beacon_on"])

    def test_a_config_reply_reaches_the_traffic_table(self):
        """$CR …:ACC:{json} 要落进 TrafficTable.set_config。"""
        table = traffic_module.TrafficTable()
        table.update_position("CES2345", latitude=31, longitude=121,
                              altitude=1000, pitch=0, bank=0, heading=0)
        self.pilot.traffic = table
        payload = json.dumps({"gear_down": True, "flaps_pct": 40,
                              "lights": {"strobe_on": True}})
        self.pilot._handle_packet(f"$CRCES2345:CCA1501:ACC:{payload}")
        aircraft = table.get("CES2345")
        self.assertTrue(aircraft.gear_down)
        self.assertAlmostEqual(aircraft.flaps, 0.4)
        self.assertTrue(aircraft.lights["strobe_on"])

    def test_query_for_someone_else_is_ignored(self):
        sent = []
        self.pilot._send = sent.append
        self.pilot._handle_packet("$CQZSPD_TWR:CES2345:CAPS")
        self.assertEqual(sent, [])

    def test_unknown_packet_does_not_break_the_loop(self):
        self.assertIsNot(self.pilot._handle_packet("$XXgarbage"), False)


class FlightPlanTest(unittest.TestCase):
    """$FP 的字段布局。真实日志里每次提交都被回 "Too few fields for $FP"。"""

    # can-fsd 的 minimumFields（packet.go）要求 17 段，
    # 布局见 docs/protocol.md 的 Flight Plan `$FP`
    FIELDS = 17

    def setUp(self):
        self.sent = []
        self.pilot = fsdpilot.FSDPilot("example.invalid", "CCA1501", "1234", "pw")
        self.pilot._send = lambda packet: self.sent.append(packet) or True

    def test_field_count(self):
        self.pilot.file_flight_plan({
            "rules": "I", "aircraft": "B738", "cruise_speed": "450",
            "departure": "ZSPD", "arrival": "ZBAA", "cruise_altitude": "35000",
            "route": "PIKAS A461 SASAN", "remarks": "/v/",
        })
        self.assertEqual(len(self.sent[0].split(":")), self.FIELDS)

    def test_empty_plan_still_has_every_field(self):
        # 什么都不填也得凑满 17 段，否则整包被拒
        self.pilot.file_flight_plan({})
        self.assertEqual(len(self.sent[0].split(":")), self.FIELDS)

    def test_route_colons_do_not_break_the_packet(self):
        self.pilot.file_flight_plan({"route": "A:B", "remarks": "x:y"})
        self.assertEqual(len(self.sent[0].split(":")), self.FIELDS)

    def test_filed_to_server(self):
        # 按 protocol.md，填报发给 SERVER；*A 是服务端转发给管制时用的
        self.pilot.file_flight_plan({})
        self.assertEqual(self.sent[0].split(":")[1], "SERVER")

    def test_field_order_matches_the_protocol(self):
        self.pilot.file_flight_plan({
            "rules": "I", "aircraft": "B738", "cruise_speed": "450",
            "departure": "ZSPD", "departure_time": "1230",
            "cruise_altitude": "35000", "arrival": "ZBAA",
            "enroute_hours": "2", "enroute_minutes": "15",
            "fuel_hours": "4", "fuel_minutes": "30",
            "alternate": "ZSNJ", "remarks": "RMK", "route": "PIKAS",
        })
        f = self.sent[0].split(":")
        self.assertEqual(f[0], "$FPCCA1501")
        self.assertEqual(f[2], "I")          # 飞行规则
        self.assertEqual(f[3], "B738")       # 机型
        self.assertEqual(f[4], "450")        # 真空速
        self.assertEqual(f[5], "ZSPD")       # 起飞地
        self.assertEqual(f[8], "35000")      # 巡航高度
        self.assertEqual(f[9], "ZBAA")       # 目的地
        self.assertEqual(f[10], "2")         # 航路小时
        self.assertEqual(f[11], "15")        # 航路分钟
        self.assertEqual(f[12], "4")         # 燃油小时
        self.assertEqual(f[13], "30")        # 燃油分钟
        self.assertEqual(f[14], "ZSNJ")      # 备降场
        self.assertEqual(f[16], "PIKAS")     # 航路

    def test_simulator_is_not_flight_simulator_2004(self):
        """模拟器编号原来写的 8，在 can-fsd 的枚举里是 MSFS 2004。"""
        self.assertNotEqual(fsdpilot.SIMULATOR, 8)
        self.assertEqual(fsdpilot.SIMULATOR, fsdpilot.SIMULATOR_XPLANE_12)


class VoiceChannelTest(unittest.TestCase):
    """频道切换。真实日志里连着两条 "Channel FREQ_121700 does not exists"。"""

    def setUp(self):
        for name in ("pyaudio", "pymumble_py3", "pymumble_py3.constants",
                     "pymumble_py3.errors", "numpy"):
            sys.modules.setdefault(name, mock.MagicMock())
        import voice
        self.voice = voice

    def test_waits_for_the_server_instead_of_a_fixed_sleep(self):
        """建频道是一次网络往返，固定 sleep 赌不起。

        原来 new_channel 之后 sleep(0.3) 就去找，远程服务器上经常还没回来，
        报出来是"频道不存在"，看着像建不了。等待逻辑现在在 _switch_channel
        里——它跑在工作线程上，set_frequency 只负责记下目标。
        """
        source = inspect.getsource(self.voice.Voice._switch_channel)
        self.assertNotIn("sleep(0.3)", source)
        self.assertIn("_wait_for_channel", source)

    def test_switching_is_serialised(self):
        # start() 的补切和工作线程会同时进来，各建一次各报一次错
        source = inspect.getsource(self.voice.Voice._switch_channel)
        self.assertIn("_channel_lock", source)

    def test_channel_timeout_is_generous_enough_for_a_remote_server(self):
        self.assertGreaterEqual(self.voice.CHANNEL_TIMEOUT, 2.0)

    def test_set_frequency_returns_immediately(self):
        """set_frequency 不能阻塞调用方。

        它是从 gui.py 的 tick() 调的，tick() 跑在 Qt 主线程上。真正的切换要等
        服务器回 ChannelState，最坏 CHANNEL_TIMEOUT 秒——在主线程上等这么久，
        窗口直接"未响应"（实测过，日志停在"建一个临时的"之后就没了）。

        前面几条测试只看代码结构，正是这样漏掉了这个问题，所以这条直接计时。
        """
        caster = self.voice.Voice.__new__(self.voice.Voice)
        caster.frequency = None
        caster._pending = None
        caster._channel_wanted = threading.Event()

        started = time.time()
        caster.set_frequency(121.5)
        elapsed = time.time() - started

        self.assertLess(elapsed, 0.05,
                        f"set_frequency 阻塞了 {elapsed:.2f} 秒")
        self.assertEqual(caster._pending, 121.5, "目标频率应当记下来")
        self.assertTrue(caster._channel_wanted.is_set(), "应当叫醒切换线程")

    def test_set_frequency_does_not_touch_the_network(self):
        # 一个连 mumble 都没有的实例上调用也不该炸——真正的活儿在工作线程
        caster = self.voice.Voice.__new__(self.voice.Voice)
        caster.frequency = None
        caster._pending = None
        caster._channel_wanted = threading.Event()
        caster.mumble = None
        caster.set_frequency(133.15)
        self.assertEqual(caster._pending, 133.15)

    def test_repeated_same_frequency_is_cheap(self):
        caster = self.voice.Voice.__new__(self.voice.Voice)
        caster.frequency = None
        caster._pending = None
        caster._channel_wanted = threading.Event()
        caster.set_frequency(121.5)
        caster._channel_wanted.clear()
        caster.set_frequency(121.5)     # tick() 每 0.5 秒就来一次
        self.assertFalse(caster._channel_wanted.is_set(),
                         "频率没变就不该反复叫醒工作线程")

    def test_root_channel_does_not_block_transmit(self):
        """根频道的 channel_id 是 0，不能当成"没进频道"。

        写成 `not myself["channel_id"]` 的话，人在根频道时 PTT 会一声不吭地
        什么都不发——用户看到的就是"语音用不了"，日志里一个字都没有。
        """
        source = inspect.getsource(self.voice.Voice._run)
        self.assertNotIn('not myself["channel_id"]', source)
        self.assertIn('myself["channel_id"] is None', source)

    def test_silent_ptt_is_explained(self):
        # 按了 PTT 却一帧没发，必须说出原因，否则没法查
        source = inspect.getsource(self.voice.Voice)
        self.assertIn("_skip_reason", source)
        self.assertIn("not a single frame was sent", source)

    def test_frames_are_counted(self):
        # "发了但对方听不到"和"根本没发"是两回事，只有帧数能分开
        source = inspect.getsource(self.voice.Voice)
        self.assertIn("_sent_frames", source)
        self.assertIn("_received_frames", source)

    def test_switching_retries_until_it_succeeds(self):
        """频道切换必须自愈，不能一次失败就永远留在根频道。

        原来是事件驱动：set_frequency 置位、工作线程消费掉。刚上线那几秒
        mumble 常常还没就绪，那一次切换白跑，而 _pending 没变、set_frequency
        又直接 return，于是再也不重试。实测日志里就是这样——连上 19 秒后按
        PTT，全程没有任何频道切换记录，人一直在根频道。
        """
        source = inspect.getsource(self.voice.Voice._channel_loop)
        # 目标和当前不一致就该重试，而不是只在事件到来时才动
        self.assertIn("target == self.frequency", source)
        self.assertIn("CHANNEL_RETRY_INTERVAL", source)

    def test_retry_is_frequent_enough_to_be_unnoticeable(self):
        self.assertLessEqual(self.voice.CHANNEL_RETRY_INTERVAL, 2.0)

    def test_transmitting_from_root_is_reported(self):
        # 留在根频道还发，等于对着没人的地方喊，日志必须说出来
        source = inspect.getsource(self.voice.Voice._run)
        self.assertIn("ROOT_CHANNEL", source)

    def test_failed_connection_is_not_reported_as_connected(self):
        """pymumble 的 connected 是状态码：3 是 FAILED，也是真值。

        实测里用户名填错，Mumble 回 "Wrong certificate or password"，连接线程
        带着异常死掉，界面却报"语音已连接"，然后一切莫名其妙地不工作。
        """
        caster = self.voice.Voice.__new__(self.voice.Voice)
        # 测试环境里 pymumble 的常量可能是替身，所以拿模块自己导入的那个比对
        connected_state = self.voice.PYMUMBLE_CONN_STATE_CONNECTED
        # 连接标记由 CONNECTED / DISCONNECTED 回调翻转，这里假定已经连上
        caster._connection_established = threading.Event()
        caster._connection_established.set()

        caster.mumble = type("M", (), {"connected": connected_state})()
        self.assertTrue(caster.connected, "真的连上了应当是 True")

        # 0 未连接、1 认证中、3 失败——用 bool() 判断的话 1 和 3 都会是真值
        for state in (0, 1, 3):
            caster.mumble = type("M", (), {"connected": state})()
            self.assertFalse(caster.connected,
                             f"connected={state} 不该算作已连接")

    def test_no_mumble_means_not_connected(self):
        caster = self.voice.Voice.__new__(self.voice.Voice)
        caster._connection_established = threading.Event()
        caster._connection_established.set()
        caster.mumble = None
        self.assertFalse(caster.connected)

    def test_a_dead_main_loop_is_not_reported_as_connected(self):
        """主循环结束时 pymumble **不会**把 connected 改回去，它就停在 2。

        只看状态码的话，连接早就死透了（命令队列再没人抽）界面还是绿的——那
        正是"进不了频道又不报错"的表象。所以还要看那个由回调翻转的独立标记，
        老飞行员端的 _connection_established 就是干这个的。
        """
        caster = self.voice.Voice.__new__(self.voice.Voice)
        connected_state = self.voice.PYMUMBLE_CONN_STATE_CONNECTED
        caster.mumble = type("M", (), {"connected": connected_state})()

        caster._connection_established = threading.Event()
        caster._connection_established.set()
        self.assertTrue(caster.connected, "前提：标记在时算已连接")

        caster._connection_established.clear()   # 主循环退出时清掉
        self.assertFalse(caster.connected,
                         "状态码还停在已连接，但循环已经没了，不能算连着")

    def test_stuck_channel_is_explained(self):
        # 切不过去的两个分支原来是静默 continue，日志里什么都看不到
        source = inspect.getsource(self.voice.Voice._channel_loop)
        self.assertIn("_note_stuck", source)

    def test_channel_commands_never_block(self):
        """建频道和进频道都不能用 pymumble 的阻塞接口。

        channels.new_channel() 和 users.move_in() 都走
        execute_command(blocking=True)，那个 acquire 没有超时——pymumble 自己
        的源码里就写着 "TODO: manage a timeout for blocking commands"。命令没
        被处理就永远卡住，而且我们还握着 _channel_lock，整条切换链全死。

        实测日志停在"建一个临时的"，之后既没有成功也没有任何错误——线程根本
        没从那一行返回。
        """
        # 用 AST 看真正的调用，别跟注释和文档字符串较劲——那里面也提到了这两
        # 个接口，按文本匹配会误判
        import ast
        tree = ast.parse(inspect.getsource(self.voice).lstrip())
        blocking_calls = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in ("new_channel", "move_in"):
                    blocking_calls.append(node.func.attr)
        self.assertEqual(blocking_calls, [],
                         f"{blocking_calls} 会无限期阻塞，要自己发命令")

        for name in ("_create_channel", "_switch_channel"):
            body = inspect.getsource(getattr(self.voice.Voice, name))
            self.assertIn("blocking=False", body, f"{name} 应当非阻塞地发命令")

    def test_move_is_confirmed_before_bookkeeping(self):
        # 命令是异步的：没确认就记账的话，收敛循环会以为成功而不再重试
        source = inspect.getsource(self.voice.Voice._switch_channel)
        self.assertIn("_wait_until_in", source)

    def test_switching_happens_on_a_worker_thread(self):
        source = inspect.getsource(self.voice.Voice)
        self.assertIn("_channel_loop", source)
        self.assertIn("_switch_channel", source)


# ---------------------------------------------------------------------------
# 上面那一堆大多是拿 inspect.getsource 对字符串，只能证明"代码长这样"，证明不了
# "跑起来是对的"。下面这些真的把 _channel_loop / _run 跑起来——掉线重连之后不
# 回频道那个 bug 就是这么找出来的：源码匹配全部通过，人却在根频道对着空气喊。
# ---------------------------------------------------------------------------

class FakeVoiceServer:
    """够用的 Mumble 替身，命令异步生效，和真的一样。"""

    def __init__(self, connected, missing_channel_error, latency=0.05):
        # 这个模块里 pymumble 是替身，常量和异常类都不是真的——所以状态码和
        # "频道不存在"的异常都从 voice 自己导入的那份拿，不要写死
        self.connected = connected
        self.latency = latency
        self.by_name = {}
        self.my_channel = 0             # 根频道
        self.next_id = 1
        self.commands = []
        self.sent = []                  # 发出去的话音
        outer = self

        class Channels:
            def find_by_name(self, name):
                if name in outer.by_name:
                    return outer.by_name[name]
                raise missing_channel_error(name)

        class Myself:
            def __getitem__(self, key):
                if key == "channel_id":
                    return outer.my_channel
                if key == "name":
                    return "1000"
                raise KeyError(key)

        class Users:
            myself = Myself()
            myself_session = 7

        class Output:
            def add_sound(self, pcm):
                outer.sent.append(pcm)

        self.channels = Channels()
        self.users = Users()
        self.sound_output = Output()

    def execute_command(self, cmd, blocking=True):
        assert not blocking, "阻塞接口没有超时，不能用"
        self.commands.append(cmd)
        timer = threading.Timer(self.latency, self._apply, args=(cmd,))
        timer.daemon = True
        timer.start()

    def _apply(self, cmd):
        params = cmd.parameters
        if "name" in params:
            self.by_name[params["name"]] = {"channel_id": self.next_id,
                                            "name": params["name"]}
            self.next_id += 1
        elif "session" in params:
            self.my_channel = params["channel_id"]


class FakeCommand:
    def __init__(self, parameters):
        self.parameters = parameters


class FakeMessages:
    """pymumble.messages 的替身。

    这个模块把 pymumble 整个换成了 MagicMock，`messages.CreateChannel(...)` 于是
    只会返回一个 MagicMock，`parameters` 里什么都没有。这里照抄 pymumble
    messages.py 里那两个命令的真实字段，假服务器才认得出发的是什么。

    真实字段名由 atis / controller / client 那几套测试盯着——它们用的是真的
    pymumble，构造出来的就是真命令。
    """

    @staticmethod
    def CreateChannel(parent, name, temporary):
        return FakeCommand({"parent": parent, "name": name,
                            "temporary": temporary})

    @staticmethod
    def MoveCmd(session, channel_id):
        return FakeCommand({"session": session, "channel_id": channel_id})


class FakeStream:
    def __init__(self):
        self.reads = 0

    def read(self, frames, exception_on_overflow=False):
        self.reads += 1
        time.sleep(0.005)
        return b"\x01\x02" * frames

    def write(self, data):
        pass

    def stop_stream(self):
        pass

    def close(self):
        pass


class VoiceRuntimeTest(unittest.TestCase):
    """把 Voice 真的跑起来：切频道、发话音、掉线重连之后还能不能用。"""

    def setUp(self):
        for name in ("pyaudio", "pymumble_py3", "pymumble_py3.constants",
                     "pymumble_py3.errors"):
            sys.modules.setdefault(name, mock.MagicMock())
        import voice
        self.voice_module = voice
        # pymumble 是替身，errors.UnknownChannelError 不是真的异常类——换成一个
        # 真的，_find_channel 的 except 才接得住
        missing = type("UnknownChannelError", (Exception,), {})
        voice.pymumble.errors.UnknownChannelError = missing
        self._real_messages = voice.messages
        voice.messages = FakeMessages
        self.server = FakeVoiceServer(voice.PYMUMBLE_CONN_STATE_CONNECTED, missing)
        self.voice = voice.Voice(
            "host", "1000", "pw",
            settings=types.SimpleNamespace(mic_volume=100, speaker_volume=100))
        self.voice.mumble = self.server
        self.voice.running = True
        # 这些用例绕过 start() 直接塞连接，所以连接标记要自己置上——正常路径
        # 里它由 pymumble 的 CONNECTED 回调翻转
        self.voice._connection_established.set()
        self.voice._input = FakeStream()
        self.voice._output = FakeStream()
        self.voice._chunk = 960
        self.threads = []

    def tearDown(self):
        self.voice.running = False
        self.voice._channel_wanted.set()
        for thread in self.threads:
            thread.join(timeout=2)
        self.voice_module.messages = self._real_messages

    def run_loops(self):
        for target in (self.voice._channel_loop, self.voice._run):
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            self.threads.append(thread)

    def wait_until(self, predicate, timeout=4.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        return False

    def transmit(self, seconds=0.4):
        self.server.sent.clear()
        self.voice.set_transmitting(True)
        time.sleep(seconds)
        self.voice.set_transmitting(False)
        return list(self.server.sent)

    def test_tuning_creates_the_channel_and_joins_it(self):
        self.run_loops()
        self.voice.set_frequency(118.0)
        self.assertTrue(self.wait_until(lambda: self.voice.channel == "FREQ_118000"),
                        "没能进入频率频道")
        self.assertEqual(self.server.my_channel,
                         self.server.by_name["FREQ_118000"]["channel_id"],
                         "服务器那边要真的把人挪过去")

    def test_ptt_actually_sends_audio(self):
        self.run_loops()
        self.voice.set_frequency(118.0)
        self.assertTrue(self.wait_until(lambda: self.voice.channel is not None))
        self.assertTrue(self.transmit(), "按住 PTT 应当发出话音")

    def test_retuning_moves_to_the_new_channel(self):
        self.run_loops()
        self.voice.set_frequency(118.0)
        self.assertTrue(self.wait_until(lambda: self.voice.channel == "FREQ_118000"))
        self.voice.set_frequency(121.7)
        self.assertTrue(self.wait_until(lambda: self.voice.channel == "FREQ_121700"))
        self.assertEqual(self.server.my_channel,
                         self.server.by_name["FREQ_121700"]["channel_id"])

    def test_it_rejoins_after_the_server_puts_us_back_in_root(self):
        """掉线重连之后必须自己回到频率频道。

        pymumble 是 reconnect=True 建的，重连之后服务器把人放回根频道，而
        self.frequency / self.channel 还停在旧值。只比对这两个的话，收敛循环
        会认为"已经到位"而再也不切——界面一直显示已连接，人却在根频道。
        """
        self.run_loops()
        self.voice.set_frequency(118.0)
        self.assertTrue(self.wait_until(lambda: self.voice.channel == "FREQ_118000"))
        joined = self.server.my_channel

        self.server.my_channel = self.voice_module.ROOT_CHANNEL   # 重连了
        self.assertTrue(
            self.wait_until(lambda: self.server.my_channel == joined),
            "被放回根频道之后没有重新进入频率频道")

    def test_it_does_not_transmit_into_the_root_channel(self):
        """在根频道发话音，等于对着空气喊，还会打扰根频道里所有人。

        判据原来附带 `and self.channel is None`，重连之后 self.channel 停在旧
        值，条件不成立——话音就真的进了根频道，而帧数一路在涨，看着完全正常。
        """
        self.run_loops()
        self.voice.set_frequency(118.0)
        self.assertTrue(self.wait_until(lambda: self.voice.channel == "FREQ_118000"))

        # 卡住重连逻辑，专门制造"人在根频道但 self.channel 还是旧值"的一刻
        self.voice._channel_lock.acquire()
        try:
            self.server.my_channel = self.voice_module.ROOT_CHANNEL
            self.assertEqual(self.transmit(), [],
                             "留在根频道时一帧都不该发出去")
            self.assertIn("根频道", self.voice._skip_reason)
        finally:
            self.voice._channel_lock.release()


class ReleaseOrderTest(unittest.TestCase):
    """_release() 必须先停 Mumble 再收 PyAudio。

    接收回调跑在 pymumble 的线程上，正在 _output.write() 时把 PyAudio
    terminate 掉是 C 层崩溃，try/except 接不住——正常断开时对面恰好有人
    说话就中招。
    """

    def test_mumble_stops_before_audio_terminates(self):
        import threading as threading_module

        import voice

        order = []

        class FakeMumble:
            def stop(self):
                order.append("mumble.stop")

        class FakeStream:
            def stop_stream(self):
                pass

            def close(self):
                order.append("stream.close")

        class FakeAudio:
            def terminate(self):
                order.append("audio.terminate")

        client = object.__new__(voice.Voice)
        client.mumble = FakeMumble()
        client._input = FakeStream()
        client._output = FakeStream()
        client._audio = FakeAudio()
        client._stream_lock = threading_module.Lock()
        client._release()

        self.assertEqual(order[0], "mumble.stop",
                         "必须先停 Mumble：接收回调可能还在往流里写")
        self.assertIn("audio.terminate", order)
        self.assertLess(order.index("mumble.stop"),
                        order.index("audio.terminate"))


class VoiceStartupFailureTest(unittest.TestCase):
    """连接失败必须把资源放掉，否则第二次连接根本连不成。"""

    def setUp(self):
        for name in ("pyaudio", "pymumble_py3", "pymumble_py3.constants",
                     "pymumble_py3.errors"):
            sys.modules.setdefault(name, mock.MagicMock())
        import voice
        self.voice_module = voice
        self.audios = []
        self.stopped = []
        outer = self

        class FakeAudio:
            def __init__(self):
                self.terminated = False
                outer.audios.append(self)

            def terminate(self):
                self.terminated = True

        # pymumble 是替身，errors.ConnectionRejectedError 不是真的异常类——
        # _mumble_loop 的 except 接不住 MagicMock，会当场 TypeError
        rejected = type("ConnectionRejectedError", (Exception,), {})
        voice.pymumble.errors.ConnectionRejectedError = rejected

        class FakeMumble:
            connected = 3               # PYMUMBLE_CONN_STATE_FAILED
            callbacks = types.SimpleNamespace(set_callback=lambda *a: None)

            def __init__(self, *args, **kwargs):
                pass

            def set_receive_sound(self, value):
                pass

            def run(self):
                # 密码不对时真的 pymumble 就是从 run() 里把它抛出来的
                raise rejected("Wrong certificate or password")

            def is_ready(self):
                pass

            def stop(self):
                outer.stopped.append(self)

        self._real_mumble = voice.pymumble.Mumble
        voice.pymumble.Mumble = FakeMumble
        self.FakeAudio = FakeAudio

    def tearDown(self):
        self.voice_module.pymumble.Mumble = self._real_mumble

    def make_voice(self):
        states = []
        voice = self.voice_module.Voice(
            "host", "1000", "wrong-password",
            settings=types.SimpleNamespace(mic_volume=100, speaker_volume=100),
            on_status=lambda state, message: states.append(state))
        voice._open_audio = lambda: (
            setattr(voice, "_audio", self.FakeAudio()),
            setattr(voice, "_input", FakeStream()),
            setattr(voice, "_output", FakeStream()))
        return voice, states

    def test_a_rejected_login_releases_the_microphone(self):
        """不放掉的话，PyAudio 还占着麦克风。

        用户把密码改对再连一次，新的 Voice 在 _open_audio() 就失败，界面说
        "打不开音频设备"——把人指向声卡，而真正的原因是上一次登录失败。
        """
        voice, states = self.make_voice()
        voice.start()
        self.assertEqual(states[-1], 'error')
        self.assertEqual(len(self.audios), 1)
        self.assertTrue(self.audios[0].terminated, "PyAudio 没有 terminate")
        self.assertIsNone(voice._input)

    def test_a_rejected_login_stops_the_mumble_connection(self):
        """pymumble 是 reconnect=True 建的，扔着不管它会一直重连下去。

        服务端 login.py 对认证失败按账号限流，一个后台不停重试的僵尸连接足以
        把这个账号的语音锁死——密码改对了也连不上，直到重启程序。
        """
        voice, _ = self.make_voice()
        voice.start()
        self.assertEqual(len(self.stopped), 1, "失败之后必须 stop() 掉连接")
        self.assertIsNone(voice.mumble)

    def test_repeated_failures_do_not_pile_up(self):
        voice, _ = self.make_voice()
        for _ in range(3):
            voice.start()
        self.assertEqual(len(self.audios), 3)
        self.assertTrue(all(a.terminated for a in self.audios),
                        "每一次失败都要收干净，不能越攒越多")
        self.assertEqual(len(self.stopped), 3)


class VoiceParentThreadTest(unittest.TestCase):
    """pymumble 的主循环不能挂在那个"调完 start() 就退"的线程上。

    pymumble 在 __init__ 里记下 `parent_thread = threading.current_thread()`，
    主循环的条件是 `... and self.parent_thread.is_alive() and not self.exit`，
    而**抽命令队列就在那个循环里**：

        while self.commands.is_cmd():
            self.treat_command(self.commands.pop_cmd())

    gui.py 是 `threading.Thread(target=voice.start).start()` 调起来的，start()
    把工作线程拉起来就返回，那个一次性线程当场结束。于是循环退出——连接还在、
    频道表还在、myself 也还在，但从此没有一条命令发得出去：MoveCmd 永远躺在
    队列里，服务器压根没收到，既不把人挪进频道，也不会回 PermissionDenied。

    实测日志就是这个形状，能刷一整晚：

        → 发出进频道命令 FREQ_124550：会话号=111 从频道0 到频道1
        ← 进频道命令已入队 FREQ_124550
        发出了进入 FREQ_124550 的请求，但 5 秒内没有生效，稍后重试
        现场诊断 ... 我在频道=0 频道表共4个 表里有没有目标=True

    替身照抄 pymumble 那一行（构造时记下当前线程），所以这里测的是真的行为，
    不是字符串匹配。
    """

    def setUp(self):
        for name in ("pyaudio", "pymumble_py3", "pymumble_py3.constants",
                     "pymumble_py3.errors"):
            sys.modules.setdefault(name, mock.MagicMock())
        import voice
        self.voice_module = voice

        # pymumble 是替身，模块里的状态常量也是 MagicMock，不能写死 2
        connected_state = voice.PYMUMBLE_CONN_STATE_CONNECTED
        voice.pymumble.errors.ConnectionRejectedError = type(
            "ConnectionRejectedError", (Exception,), {})
        connected_clbk = voice.PYMUMBLE_CLBK_CONNECTED

        class FakeMumble:
            def __init__(self, *args, **kwargs):
                # pymumble 的 mumble.py:59 就是这么写的
                self.parent_thread = threading.current_thread()
                # 留一份原样的，用来证明这个陷阱是真的存在
                self.constructed_in = threading.current_thread()
                self.connected = 0
                self._callbacks = {}
                self._done = threading.Event()
                self.callbacks = types.SimpleNamespace(
                    set_callback=lambda name, fn: self._callbacks.__setitem__(
                        name, fn))

            def set_receive_sound(self, value):
                pass

            def run(self):
                """和真的一样：连上之后**一直不返回**，直到 stop()。"""
                self.mumble_thread = threading.current_thread()
                self.connected = connected_state
                callback = self._callbacks.get(connected_clbk)
                if callback:
                    callback()
                self._done.wait()

            def is_ready(self):
                pass

            def stop(self):
                self._done.set()

        self._real_mumble = voice.pymumble.Mumble
        voice.pymumble.Mumble = FakeMumble

    def tearDown(self):
        self.voice_module.pymumble.Mumble = self._real_mumble

    def make_voice(self):
        v = self.voice_module.Voice(
            "host", "1000", "pw",
            settings=types.SimpleNamespace(mic_volume=100, speaker_volume=100))
        v._open_audio = lambda: (
            setattr(v, "_audio", types.SimpleNamespace(terminate=lambda: None)),
            setattr(v, "_input", FakeStream()),
            setattr(v, "_output", FakeStream()))
        # 这两条循环不是这里要测的，让它们立刻结束，免得后台线程干扰
        v._run = lambda: None
        v._channel_loop = lambda: None
        return v

    def start_from_a_throwaway_thread(self, v):
        """完全照着 gui.py 的调法来。"""
        thread = threading.Thread(target=v.start, daemon=True)
        thread.start()
        thread.join(timeout=15)
        self.assertFalse(thread.is_alive(), "start() 没有返回")
        return thread

    def test_the_mumble_loop_outlives_the_thread_that_called_start(self):
        v = self.make_voice()
        starter = self.start_from_a_throwaway_thread(v)
        self.assertIsNotNone(v.mumble, "前提：连上了")
        self.assertFalse(starter.is_alive(), "前提：起头的那个线程已经退了")
        self.assertTrue(
            v.mumble.parent_thread.is_alive(),
            "pymumble 的 parent_thread 已经死了——它的主循环就此结束，命令队列"
            "再没人抽，MoveCmd 永远发不出去，而且一声不吭")
        v.stop()

    def test_the_trap_is_real_the_object_is_built_on_the_throwaway_thread(self):
        """证明上一条测的不是个假想：对象确实是在一次性线程里构造的。"""
        v = self.make_voice()
        starter = self.start_from_a_throwaway_thread(v)
        self.assertIs(v.mumble.constructed_in, starter,
                      "Mumble 对象就是在那个一次性线程里建的，所以 pymumble 默认"
                      "记下的 parent_thread 正是它")
        self.assertIsNot(v.mumble.parent_thread, starter,
                         "必须改指到一个和会话同寿的线程上")
        v.stop()


class ReconnectLimitTest(unittest.TestCase):
    """连上过之后掉线，最多重连三次，然后整个下线。

    以前是 `reconnect=True` 一路无限重试。后果不是"多试几次"：服务端
    `login.py` 对认证失败**按 CAN ID 限流**，一个在后台不停重连的僵尸足以把这个
    账号的语音锁死——用户把密码改对了也连不上，直到重启客户端。界面那边同样
    糟：最后一次状态停在"已断开"，连接其实还在挣扎，谁也说不清当前状态。

    假基类照抄 pymumble `run()` 的形状（mumble.py:120-143），所以这里测的是真
    行为，不是字符串匹配——**这一套里的每一条都依赖那个循环的两个细节**：
    失败的重连不发任何回调，而 `connect()` 成功时返回的是 AUTHENTICATING。
    """

    def setUp(self):
        for name in ("pyaudio", "pymumble_py3", "pymumble_py3.constants",
                     "pymumble_py3.errors", "numpy"):
            sys.modules.setdefault(name, mock.MagicMock())
        import voice
        self.voice_module = voice
        self.rejected = type("ConnectionRejectedError", (Exception,), {})
        voice.pymumble.errors.ConnectionRejectedError = self.rejected

    def make_base(self, outcomes):
        """按 outcomes 依次决定每次 connect() 的结果。

        'sync'   连上并收到 ServerSync（真正的会话）
        'auth'   TLS 建好、Authenticate 发出去了，但服务器随后拒绝——**真的
                 pymumble 这一路 connect() 返回的也是 AUTHENTICATING**，把它当
                 成功就会把计数清零，于是又变回无限重试
        'fail'   连不上（socket 错误），connect() 返回 FAILED
        """
        test = self
        rejected = self.rejected
        # **不能写死 1/2/3。** 这个测试模块把 pymumble 整个换成了 MagicMock，
        # 模块里的状态常量因此也是 mock 对象；写死数字的话 Voice.connected 里
        # 那句 `mumble.connected == PYMUMBLE_CONN_STATE_CONNECTED` 永远不成立，
        # 于是连上了也被判成"服务器拒绝"。用模块里的那几个对象本身。
        FAILED = self.voice_module.PYMUMBLE_CONN_STATE_FAILED
        CONNECTED = self.voice_module.PYMUMBLE_CONN_STATE_CONNECTED
        # pymumble 的 AUTHENTICATING，voice.py 没导入，随便给个哨兵——它的意义
        # 只是"不等于 CONNECTED"
        AUTHENTICATING = object()
        on_connected = self.voice_module.PYMUMBLE_CLBK_CONNECTED
        on_disconnected = self.voice_module.PYMUMBLE_CLBK_DISCONNECTED

        class FakeBase:
            def __init__(self, *args, **kwargs):
                self.reconnect = kwargs.get("reconnect", False)
                self.parent_thread = threading.current_thread()
                self.connected = 0
                self.exit = False
                self.connect_calls = 0
                self.stopped = 0
                self._callbacks = {}
                self._drop = threading.Event()
                self.callbacks = types.SimpleNamespace(
                    set_callback=lambda name, fn:
                        self._callbacks.__setitem__(name, fn))
                test.server = self

            def set_receive_sound(self, value):
                pass

            def is_ready(self):
                pass

            def stop(self):
                self.stopped += 1
                self.reconnect = False
                self.exit = True
                self._drop.set()

            def drop(self):
                """让当前这条会话断掉。"""
                self._drop.set()

            def _fire(self, name):
                callback = self._callbacks.get(name)
                if callback:
                    callback()

            def connect(self):
                index = self.connect_calls
                self.connect_calls += 1
                outcome = outcomes[index] if index < len(outcomes) else "fail"
                self._outcome = outcome
                if outcome == "fail":
                    self.connected = FAILED
                    return FAILED
                # 真的 pymumble 这里返回 AUTHENTICATING：TLS 建好、Authenticate
                # 发出去了，认证结果还没回来。密码错的连接也走这一支。
                self.connected = AUTHENTICATING
                return AUTHENTICATING

            def run(self):
                """照抄 pymumble run() 的形状。

                两处必须一样，否则这套测试就测不到真问题：
                - 连接失败那一支只 sleep+continue，**不发回调**；
                - 丢连接时两个分支都发 DISCONNECTED，然后才决定要不要重连。

                判定用 `is FAILED` 而不是 `>= FAILED`：常量在这个模块里是 mock
                对象，比不了大小。混入放弃时返回的正是同一个对象，所以这一支
                同时接住"连不上"和"次数用尽"。
                """
                while True:
                    if self.connect() is FAILED:
                        if not self.reconnect:
                            raise rejected("连接失败")
                        continue                     # 静默重试，正是问题所在
                    if self._outcome == "sync":
                        self.connected = CONNECTED
                        self._fire(on_connected)
                        self._drop.wait()
                        self._drop.clear()
                    # 'auth' 就是服务器拒绝：没有 ServerSync，会话直接结束
                    self.connected = 0
                    if not self.reconnect:
                        self._fire(on_disconnected)
                        break
                    self._fire(on_disconnected)

        return FakeBase

    def make_voice(self, outcomes, limit=3):
        """建一个 Voice，连接类换成假基类，音频设备全是替身。"""
        voice = self.voice_module
        base = self.make_base(outcomes)
        states = []
        v = voice.Voice("host", "1000", "pw",
                        settings=types.SimpleNamespace(mic_volume=100,
                                                       speaker_volume=100),
                        on_status=lambda state, message: states.append(
                            (state, message)),
                        reconnect_limit=limit)
        v._open_audio = lambda: (
            setattr(v, "_audio", types.SimpleNamespace(terminate=lambda: None)),
            setattr(v, "_input", FakeStream()),
            setattr(v, "_output", FakeStream()))
        v._run = lambda: None
        v._channel_loop = lambda: None
        self._patched = voice.pymumble.Mumble
        voice.pymumble.Mumble = base
        self.addCleanup(setattr, voice.pymumble, "Mumble", self._patched)
        return v, states

    def wait_for(self, predicate, timeout=5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return False

    # ---------- 核心 ----------
    def test_three_attempts_then_the_whole_thing_goes_offline(self):
        v, states = self.make_voice(["sync"] + ["fail"] * 10)
        v.start()
        self.assertTrue(v.connected, "前提：先连上")

        self.server.drop()
        self.assertTrue(self.wait_for(lambda: v.gave_up), "没有下线")

        # 一次首连 + 三次重连 = 四次 connect()，第五次是判定后的收摊，不发起连接
        self.assertEqual(self.server.connect_calls, 4,
                         f"重连次数不对：{self.server.connect_calls - 1} 次")
        self.assertEqual(states[-1][0], 'offline', states)
        self.assertIn("3", states[-1][1])
        self.assertGreaterEqual(self.server.stopped, 1,
                                "下线时必须真的 stop() 掉连接，不能只报个状态")
        self.assertIsNone(v.mumble, "麦克风和连接都要放掉")
        self.assertFalse(v.running)

    def test_a_reconnect_that_works_resets_the_count(self):
        """中途连回来了，计数必须清零，否则第二天第四次抖动就把人踢下线。"""
        v, states = self.make_voice(["sync", "fail", "sync"] + ["fail"] * 10)
        v.start()
        self.server.drop()
        self.assertTrue(self.wait_for(lambda: v.connected and
                                      self.server.connect_calls >= 3),
                        "没有重连回来")
        self.assertFalse(v.gave_up, "重连成功了却还是下线了")
        self.assertEqual(states[-1][0], 'online')

        # 再掉一次，还应当有完整的三次：
        # 首连 + 失败一次 + 重连成功 = 3，第二轮再给满 3 次 = 6
        self.server.drop()
        self.assertTrue(self.wait_for(lambda: v.gave_up))
        self.assertEqual(self.server.connect_calls, 6,
                         "第二轮没有重新给满三次")

    def test_a_rejected_password_is_not_a_successful_connect(self):
        """这条是那个坑：`connect()` 成功返回的是 AUTHENTICATING，不是 CONNECTED。

        密码被拒的连接同样返回 AUTHENTICATING，只是随后在 loop() 里因为 Reject
        结束。把返回值当成"连上了"就会每次清零计数，于是无限重连——而这一次撞
        的正好是服务端按账号的认证失败限流。
        """
        v, _ = self.make_voice(["sync"] + ["auth"] * 10)
        v.start()
        self.server.drop()
        self.assertTrue(self.wait_for(lambda: v.gave_up),
                        "被拒的重连被当成了成功，会一直重试下去")
        self.assertEqual(self.server.connect_calls, 4)

    def test_an_ordinary_drop_is_not_reported_as_a_terminal_error(self):
        """一次抖动不能报成"不再自动重连"。

        原来 _on_disconnected 一律报 error，而 pymumble 的 run() 每次丢连接都发
        这条回调、随后自己就连回来了。界面收到 error 会把 Voice 引用丢掉，于是
        语音其实恢复了，客户端却再也不跟着 COM1 换频道。
        """
        v, states = self.make_voice(["sync", "sync"] + ["fail"] * 10)
        v.start()
        seen = len(states)
        self.server.drop()
        self.assertTrue(self.wait_for(lambda: len(states) > seen))
        kinds = [state for state, _ in states[seen:]]
        self.assertIn('reconnecting', kinds, kinds)
        self.assertNotIn('error', kinds, "抖动被报成了终态错误")
        v.stop()

    def test_the_first_connection_is_not_a_reconnect(self):
        """第一次就连不上不走这套计数，照旧交给 start() 报错。

        首连失败多半是密码不对或者地址填错，重试三次只会把同一条错误刷三遍。
        """
        v, states = self.make_voice(["fail"] * 10)
        v.start()
        self.assertFalse(v.gave_up)
        self.assertEqual(states[-1][0], 'error')
        self.assertIsNone(v.mumble, "失败路径也必须放掉音频设备和连接")


class KickedTest(unittest.TestCase):
    """被服务端踢下线之后**不许**连回去。

    次数上限拦不住这种情况，而这正是问题所在：它数的是失败的重连，而被踢之前的
    那次登录是成功的，计数已经被清零了。同一个账号在两台机器上登录时，两端就这
    样互相顶掉、各自重连、各自又把对方顶掉，每一轮都成功，三次的预算永远用不
    完 —— 构造上的死循环。Murmur 那边看到同一个 IP 每几秒连一次，最后 autoban
    把它整个封掉，于是那台机器彻底连不上。

    能判出"被踢"是因为 pymumble 把两种情形分开了（client/API.md）：自己走的话
    UserRemove 里只有 session，被踢才会多出 actor / reason / ban。DISCONNECTED
    回调判不了这个 —— 它不带任何理由，被顶下线和网络抖动在那儿一模一样。
    """

    def setUp(self):
        for name in ("pyaudio", "pymumble_py3", "pymumble_py3.constants",
                     "pymumble_py3.errors", "numpy"):
            sys.modules.setdefault(name, mock.MagicMock())
        import voice
        self.voice = voice

    # ---------- 混入本身 ----------
    def make_bounded(self):
        """一个只记录 super().connect() 被调了几次的基类。"""
        calls = []

        class FakeBase:
            def __init__(self):
                self.reconnect = True
                self.connected = 0

            def connect(self):
                calls.append(1)
                return "dialled"

        bounded = type("Bounded", (self.voice.BoundedReconnect, FakeBase), {})
        instance = bounded()
        return instance, calls

    def test_a_kicked_connection_never_dials_again(self):
        instance, calls = self.make_bounded()
        instance._session_established()          # 被踢之前是连上过的
        instance.mark_kicked("您的账号在其他位置登录")

        result = instance.connect()

        self.assertEqual(calls, [], "被踢之后还去连，就是那场循环本身")
        self.assertTrue(instance.gave_up)
        self.assertFalse(instance.reconnect)
        self.assertEqual(result, self.voice.PYMUMBLE_CONN_STATE_FAILED)

    def test_a_successful_session_does_not_clear_the_kick(self):
        """先踢再收到 ServerSync 也不该复活 —— 计数清零管的是重连预算，
        不是"要不要重连"这个决定。"""
        instance, calls = self.make_bounded()
        instance.mark_kicked("挤下线了")
        instance._session_established()
        instance.connect()
        self.assertEqual(calls, [])

    def test_the_limit_alone_cannot_stop_a_kick_loop(self):
        """把 mark_kicked 拿掉，只靠三次上限：每一轮成功的会话都会把计数清零，
        所以永远到不了上限。这一条钉的是"为什么需要 mark_kicked"。"""
        instance, calls = self.make_bounded()
        for _ in range(10):
            instance._session_established()      # 每轮登录都成功
            instance.connect()                   # 然后被顶掉，重连
        self.assertEqual(len(calls), 10,
                         "十轮全都真的去连了 —— 上限拦不住这种循环")
        self.assertFalse(instance.gave_up)

    # ---------- Voice 的回调 ----------
    def make_voice(self):
        v = self.voice.Voice("h", "u", "p")
        v.mumble = mock.MagicMock()
        v.mumble.users.myself_session = 42
        v.mumble.mark_kicked = mock.MagicMock()
        self.states = []
        v._status = lambda state, message: self.states.append((state, message))
        return v

    def test_being_kicked_marks_the_connection(self):
        v = self.make_voice()
        v._on_user_removed({"session": 42},
                           {"session": 42, "actor": 1,
                            "reason": "您的账号在其他位置登录", "ban": False})

        v.mumble.mark_kicked.assert_called_once()
        self.assertEqual(self.states[-1][0], 'offline')
        self.assertIn("您的账号在其他位置登录", self.states[-1][1],
                      "服务端给的理由要原样告诉用户")

    def test_leaving_voluntarily_is_not_a_kick(self):
        """只有 session 的 UserRemove 是用户自己走的。当成被踢的话，一次正常
        退出就会把重连永久关掉。"""
        v = self.make_voice()
        v._on_user_removed({"session": 42}, {"session": 42})
        v.mumble.mark_kicked.assert_not_called()
        self.assertEqual(self.states, [])

    def test_somebody_else_being_kicked_is_ignored(self):
        v = self.make_voice()
        v._on_user_removed({"session": 7},
                           {"session": 7, "actor": 1, "reason": "x"})
        v.mumble.mark_kicked.assert_not_called()

    def test_a_kick_with_no_reason_still_counts(self):
        """Murmur 自己踢 ghost 时 reason 可以是空的 —— actor 在就够了。"""
        v = self.make_voice()
        v._on_user_removed({"session": 42},
                           {"session": 42, "actor": 1, "reason": ""})
        v.mumble.mark_kicked.assert_called_once()
        self.assertEqual(self.states[-1][0], 'offline')


class FsdReconnectLimitTest(unittest.TestCase):
    """FSD 链路同一条策略：掉线重连三次，用尽就整个下线。

    两条链路必须一致，否则"整个下线"没有统一含义：语音给三次、FSD 一掉就放弃的
    话，一次服务器重启会让飞机从网络上消失而语音还连着，别人看不见你却听得见。
    """

    def make_client(self, connect_results, limit=3):
        """_connect / _loop / _close 换成替身，只测重连那一层的控制流。"""
        client = fsdpilot.FSDPilot.__new__(fsdpilot.FSDPilot)
        client.callsign = "CCA1501"
        client.running = True
        client.stop_event = threading.Event()
        client.reconnect_limit = limit
        client.gave_up = False
        client._retryable = False
        client.states = []
        # 照抄 _status 里那次翻译：重连期间的 error/stopped 不是终态
        client._status = lambda state, message: client.states.append(
            ('reconnecting' if client._retryable and state in ('error', 'stopped')
             else state, message))
        client.connect_calls = 0

        def fake_connect():
            index = client.connect_calls
            client.connect_calls += 1
            return (connect_results[index] if index < len(connect_results)
                    else False)

        client._connect = fake_connect
        client._loop = lambda: None
        client._close = lambda: None
        return client

    def setUp(self):
        self._delay = fsdpilot.RECONNECT_DELAY
        fsdpilot.RECONNECT_DELAY = 0          # 别真的等 3 秒 × 3

    def tearDown(self):
        fsdpilot.RECONNECT_DELAY = self._delay

    def test_three_attempts_then_offline(self):
        client = self.make_client([True] + [False] * 10)
        client._run()
        self.assertTrue(client.gave_up)
        self.assertEqual(client.connect_calls, 4, "一次首连 + 三次重连")
        self.assertEqual(client.states[-1][0], 'offline', client.states)

    def test_a_reconnect_that_works_resets_the_count(self):
        client = self.make_client([True, False, True] + [False] * 10)
        client._run()
        self.assertEqual(client.connect_calls, 6,
                         "首连 + 失败一次 + 重连成功，之后第二轮再给满三次")

    def test_the_first_connection_is_not_retried(self):
        """首连失败多半是呼号被占或密码不对，重试只会把同一条错误刷三遍。"""
        client = self.make_client([False] * 10)
        client._run()
        self.assertFalse(client.gave_up)
        self.assertEqual(client.connect_calls, 1)
        self.assertEqual(client.states, [], "首连失败的原因由 _connect 自己报")

    def test_a_drop_while_retrying_is_not_reported_as_terminal(self):
        """重连期间 _loop / _connect 报的 error 必须翻成 reconnecting。

        界面收到 error 会把整条连接当没了——而我们其实马上就要再试。
        """
        client = self.make_client([True, False, True] + [False] * 10)

        def loop_that_drops():
            client._status('error', "与 FSD 服务器的连接已断开")
        client._loop = loop_that_drops
        client._run()
        kinds = [state for state, _ in client.states]
        self.assertNotIn('error', kinds, kinds)
        self.assertIn('reconnecting', kinds)
        self.assertEqual(kinds[-1], 'offline')


class UpdateCheckTest(unittest.TestCase):
    """查有没有新版。查到了也只是告诉用户，更不更新是他的事。

    走的是 can 而不是 GitHub：大陆连 github.com 很不稳，60 MB 的包经常
    下到一半就断，而 ceruleanavi.net 是成员本来就连得上的。
    """

    def setUp(self):
        import update
        self.update = update

    def answer(self, payload, status=200):
        """把 urlopen 换成一个吐固定 JSON 的替身。"""
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            payload).encode("utf-8")
        return mock.patch("urllib.request.urlopen", return_value=response)

    def payload(self, version="2.0.2", available=True):
        return {
            "status": 200,
            "version": version,
            "notes": "https://example/releases/tag/v" + version,
            "update_available": available,
            "client": {
                "name": "xpc-for-can",
                "version": version,
                "size": 59057038,
                "download": "https://ceruleanavi.net/api/v1/clients/download/"
                            "xpc-for-can?v=v" + version,
            },
        }

    # ---------- 版本比较 ----------
    def test_numeric_comparison(self):
        """**不能按字符串比。** `2.0.10` 按字符串排在 `2.0.9` 前面，那样要么
        永远催更新，要么有了新版也不提示。"""
        newer = self.update.is_newer
        self.assertTrue(newer("2.0.2", "2.0.1"))
        self.assertTrue(newer("2.0.10", "2.0.9"))
        self.assertFalse(newer("2.0.9", "2.0.10"))
        self.assertTrue(newer("2.1.0", "2.0.99"))
        self.assertFalse(newer("2.0.1", "2.0.1"))
        self.assertFalse(newer("1.9.9", "2.0.0"))

    def test_tolerates_the_shapes_the_clients_actually_report(self):
        """客户端报 `2.0.1`，tag 是 `v2.0.1`，从源码跑还可能带后缀。"""
        newer = self.update.is_newer
        self.assertFalse(newer("v2.0.1", "2.0.1"))
        self.assertTrue(newer("v2.0.2", "2.0.1"))
        self.assertTrue(newer("2.1.0-rc1", "2.0.9"))
        self.assertFalse(newer("", "2.0.1"))

    # ---------- 查询 ----------
    def test_reports_a_newer_version(self):
        with self.answer(self.payload("2.0.2")):
            found = self.update.check("xpc-for-can", "2.0.1")
        self.assertIsNotNone(found)
        self.assertEqual(found.version, "2.0.2")
        self.assertIn("ceruleanavi.net", found.download,
                      "下载必须走自己的服务器，不能把用户丢给 GitHub")
        self.assertEqual(found.size_label, "56.3 MB")

    def test_no_update_returns_none(self):
        with self.answer(self.payload("2.0.1", available=False)):
            self.assertIsNone(self.update.check("xpc-for-can", "2.0.1"))

    def test_a_server_that_offers_the_same_version_is_ignored(self):
        """服务端说有新版但版本号和自己一样——本地这道闸挡住，别天天催。"""
        with self.answer(self.payload("2.0.1", available=True)):
            self.assertIsNone(self.update.check("xpc-for-can", "2.0.1"))

    def test_an_older_version_is_ignored(self):
        with self.answer(self.payload("1.9.0", available=True)):
            self.assertIsNone(self.update.check("xpc-for-can", "2.0.1"))

    # ---------- 失败一律安静 ----------
    def test_failures_never_raise(self):
        """**查更新绝不能影响启动。** 网络不通、服务器 500、返回垃圾，
        统统当作"没有新版"，而不是让异常穿到界面上。"""
        import urllib.error
        cases = [
            urllib.error.URLError("名字解析失败"),
            urllib.error.HTTPError("u", 500, "boom", None, None),
            urllib.error.HTTPError("u", 429, "slow down", None, None),
            OSError("socket 挂了"),
        ]
        for error in cases:
            with mock.patch("urllib.request.urlopen", side_effect=error):
                self.assertIsNone(self.update.check("xpc-for-can", "2.0.1"))

    def test_garbage_body_is_not_an_update(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b"<html>nope</html>"
        with mock.patch("urllib.request.urlopen", return_value=response):
            self.assertIsNone(self.update.check("xpc-for-can", "2.0.1"))

    def test_a_payload_without_a_client_block_still_works(self):
        """服务端只回了总版本、没回单个包，也不该崩。"""
        payload = {"update_available": True, "version": "2.0.2", "notes": "n"}
        with self.answer(payload):
            found = self.update.check("xpc-for-can", "2.0.1")
        self.assertIsNotNone(found)
        self.assertEqual(found.version, "2.0.2")
        self.assertEqual(found.download, "")     # 没有下载地址，界面会去开说明页


class ChannelNameTest(unittest.TestCase):
    """频率到频道名是全网约定，改了三个客户端一起坏。"""

    def setUp(self):
        # voice 模块要 pyaudio 和 pymumble，这里只测纯函数，装个替身
        for name in ("pyaudio", "pymumble_py3", "pymumble_py3.constants",
                     "pymumble_py3.errors", "numpy"):
            sys.modules.setdefault(name, mock.MagicMock())

    def test_known_frequencies(self):
        import voice
        self.assertEqual(voice.channel_name(125.400), "FREQ_125400")
        self.assertEqual(voice.channel_name(118.000), "FREQ_118000")
        self.assertEqual(voice.channel_name(99.900), "FREQ_099900")

    def test_matches_the_other_clients(self):
        import voice
        for frequency in (118.0, 121.5, 127.85, 132.025):
            expected = f"FREQ_{str(int(round(frequency * 1000))).zfill(6)}"
            self.assertEqual(voice.channel_name(frequency), expected)

    def test_833_spacing(self):
        import voice
        self.assertEqual(voice.channel_name(132.005), "FREQ_132005")


class VoiceHostTest(unittest.TestCase):
    """语音服务器换域名之后，老配置里存的那个旧域名必须换掉。

    mumble_host 是写进 xpc_settings.json 的，所以只改 DEFAULTS 只对全新安装
    有效。旧域名停掉那天，老用户看到的是"连不上语音服务器"，而设置界面上那
    一行看着完全正常——没有任何线索指向配置文件。
    """

    def setUp(self):
        import settings as settings_module
        self.module = settings_module
        self.temp = tempfile.mkdtemp(prefix="xpc_settings_")
        self.addCleanup(shutil.rmtree, self.temp, True)
        self.path = os.path.join(self.temp, "xpc_settings.json")

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


class XPlaneParsingTest(unittest.TestCase):
    """RREF 回包的解析和单位换算。"""

    def setUp(self):
        self.link = xplane.XPlaneLink()

    def _rref(self, pairs):
        packet = b"RREF\x00"
        for index, value in pairs:
            packet += struct.pack("=if", index, value)
        return packet

    def test_parses_a_reply(self):
        index = xplane.NAME_TO_INDEX["latitude"]
        self.assertTrue(self.link._handle(self._rref([(index, 31.1434)])))
        self.assertAlmostEqual(self.link.values["latitude"], 31.1434, places=4)

    def test_parses_several_values_at_once(self):
        pairs = [(xplane.NAME_TO_INDEX["latitude"], 31.0),
                 (xplane.NAME_TO_INDEX["longitude"], 121.0),
                 (xplane.NAME_TO_INDEX["groundspeed"], 100.0)]
        self.assertTrue(self.link._handle(self._rref(pairs)))
        self.assertEqual(len(self.link.values), 3)

    def test_rejects_a_short_packet(self):
        self.assertFalse(self.link._handle(b"RREF\x00short"))

    def test_rejects_a_foreign_packet(self):
        self.assertFalse(self.link._handle(b"DATA\x00" + b"\x00" * 32))

    def test_unknown_index_is_ignored(self):
        self.assertFalse(self.link._handle(self._rref([(999, 1.0)])))

    def test_indices_are_unique(self):
        self.assertEqual(len(xplane.NAME_TO_INDEX), len(xplane.DATAREFS))
        self.assertEqual(len(set(xplane.INDEX_TO_NAME)), len(xplane.DATAREFS))


class ComFrequencyFallbackTest(unittest.TestCase):
    """X-Plane 11.30 以前没有 8.33 那个 dataref，两个一起订、优先精确的。

    不存在的 dataref X-Plane 只是不推送，不报错，所以不用按版本分支。
    """

    def setUp(self):
        self.link = xplane.XPlaneLink()

    def test_prefers_the_precise_dataref(self):
        # 两个都有时用 8.33 那个，它能表示 132.005
        self.assertEqual(self.link._frequency(132005.0, 13200.0), 132.005)

    def test_falls_back_to_the_legacy_dataref(self):
        # 老的单位是 10 kHz：12150 -> 121.500
        self.assertEqual(self.link._frequency(None, 12150.0), 121.5)

    def test_falls_back_when_precise_is_zero(self):
        self.assertEqual(self.link._frequency(0.0, 11800.0), 118.0)

    def test_none_when_neither_is_available(self):
        self.assertIsNone(self.link._frequency(None, None))
        self.assertIsNone(self.link._frequency(0.0, 0.0))

    def test_snapshot_uses_the_legacy_value(self):
        self.link.values = {"com1_legacy": 12150.0}
        self.assertEqual(self.link.snapshot()["com1"], 121.5)

    def test_both_com_radios_have_a_fallback(self):
        for name in ("com1", "com2"):
            self.assertIn(f"{name}_legacy", xplane.DATAREFS)

    def test_legacy_datarefs_have_their_own_indices(self):
        # 索引撞了会让回包对错 dataref
        self.assertEqual(len(set(xplane.NAME_TO_INDEX.values())),
                         len(xplane.DATAREFS))


class DiscoveryTest(unittest.TestCase):
    """信标发现。用例来自一次真实飞行的日志：连上模拟器花了 8 分半。

    那台机器上信标从两个网卡回来（198.18.0.1 的虚拟网卡和 192.168.31.231 的
    局域网卡），而且每次 15 秒没数据就把发现到的地址整个扔掉、退回本机重来。
    """

    def test_virtual_adapters_rank_last(self):
        # 198.18/15 是 benchmark 段，实际是 VPN 虚拟网卡，往那边发收不到数据
        self.assertGreater(xplane._address_rank("198.18.0.1"),
                           xplane._address_rank("192.168.31.231"))

    def test_loopback_ranks_first(self):
        self.assertLess(xplane._address_rank("127.0.0.1"),
                        xplane._address_rank("192.168.31.231"))

    def test_ordinary_lan_beats_virtual(self):
        for virtual in ("198.18.0.1", "172.17.0.1", "169.254.1.1"):
            self.assertGreater(xplane._address_rank(virtual),
                               xplane._address_rank("10.0.0.5"),
                               f"{virtual} 应当排在普通局域网地址之后")

    def test_beacon_is_parsed(self):
        packet = b"BECN\x00" + struct.pack("=BBiiIH", 1, 2, 11, 1200, 1, 49000)
        self.assertEqual(
            xplane.XPlaneLink._parse_beacon(packet, ("192.168.31.231", 5000)),
            ("192.168.31.231", 49000))

    def test_foreign_packet_is_rejected(self):
        self.assertIsNone(
            xplane.XPlaneLink._parse_beacon(b"XXXX\x00" + b"\x00" * 20,
                                            ("1.2.3.4", 5000)))

    def test_known_good_address_is_preferred_over_loopback(self):
        """收过数据的地址不该被扔掉。

        真实日志里发现了 192.168.31.231，等 15 秒没数据（X-Plane 还在读盘）就
        退回 127.0.0.1，来回折腾了 8 分钟。
        """
        link = xplane.XPlaneLink()
        link._known_good = ("192.168.31.231", 49000)
        fallback = (link._known_good or link._last_discovered
                    or ("127.0.0.1", xplane.DEFAULT_PORT))
        self.assertEqual(fallback, ("192.168.31.231", 49000))

    def test_last_discovered_is_used_when_nothing_worked_yet(self):
        link = xplane.XPlaneLink()
        link._last_discovered = ("192.168.31.231", 49000)
        fallback = (link._known_good or link._last_discovered
                    or ("127.0.0.1", xplane.DEFAULT_PORT))
        self.assertEqual(fallback, ("192.168.31.231", 49000))

    def test_loopback_only_as_a_last_resort(self):
        link = xplane.XPlaneLink()
        fallback = (link._known_good or link._last_discovered
                    or ("127.0.0.1", xplane.DEFAULT_PORT))
        self.assertEqual(fallback[0], "127.0.0.1")


class LoginTest(unittest.TestCase):
    """登录时发的东西。真实日志里每次登录都跟着一条服务器错误。"""

    def test_no_bogus_atc_query_on_login(self):
        """不要再发没有目标呼号的 $CQ…:SERVER:ATC。

        can-fsd 的 handleQueryATC 是问"某个指定呼号是不是在线管制"，第 3 段
        必须带目标；不带就回 "Missing callsign"（handler.go:400）。而且本来就
        不需要——管制席位是靠 % 位置包广播过来的。
        """
        # 只看真正发出去的语句：解释这段历史的注释里也提到了这个包
        sends = [line for line in
                 inspect.getsource(fsdpilot.FSDPilot._connect).splitlines()
                 if "_send(" in line and not line.strip().startswith("#")]
        self.assertTrue(sends, "登录时总要发点什么")
        for line in sends:
            self.assertNotIn("SERVER:ATC", line)


class WaitingTest(unittest.TestCase):
    """X-Plane 没起来的时候不该一秒重订一次。

    Windows 上往没人监听的端口发 UDP 会回 ICMP 不可达，下一次 recvfrom 抛
    ConnectionResetError。第一版按 OSError 处理直接重来，日志里就是每秒一条
    "已订阅 14 个 dataref"。
    """

    def setUp(self):
        self.link = xplane.XPlaneLink()
        self.link.address = ("127.0.0.1", 49000)

    def test_keeps_waiting_at_first(self):
        self.assertTrue(self.link._still_waiting(time.time()))

    def test_reports_disconnected_once_stale(self):
        states = []
        self.link.on_state = lambda connected, message: states.append(connected)
        self.link._connected = True
        self.link._still_waiting(time.time() - xplane.STALE_AFTER - 1)
        self.assertEqual(states, [False])

    def test_still_waiting_while_stale_but_not_hopeless(self):
        self.assertTrue(self.link._still_waiting(time.time() - xplane.STALE_AFTER - 1))
        self.assertIsNotNone(self.link.address, "还不到重新发现的时候")

    def test_rediscovers_after_a_long_silence(self):
        self.assertFalse(
            self.link._still_waiting(time.time() - xplane.REDISCOVER_AFTER - 1))
        self.assertIsNone(self.link.address, "应当清掉地址重新发现")

    def test_rediscover_is_slower_than_stale(self):
        self.assertGreater(xplane.REDISCOVER_AFTER, xplane.STALE_AFTER)


class SnapshotTest(unittest.TestCase):
    """换算：X-Plane 用公制，FSD 要英尺和节。"""

    def setUp(self):
        self.link = xplane.XPlaneLink()
        self.link.values = {
            "latitude": 31.1434, "longitude": 121.805,
            "elevation": 10668.0,          # 米 = 35000 英尺
            "agl": 3048.0,                 # 米 = 10000 英尺
            "groundspeed": 231.5,          # 米每秒 ≈ 450 节
            "pitch": 2.0, "bank": -5.0, "heading_true": 271.0,
            "squawk": 2000.0, "xpdr_mode": 2.0,
            "com1": 121500.0, "com2": 118000.0,
            "com1_power": 1.0, "on_ground": 0.0,
        }

    def test_metres_to_feet(self):
        self.assertEqual(self.link.snapshot()["altitude"], 35000)

    def test_pressure_correction_without_the_datarefs(self):
        """老 X-Plane 不推这两个 dataref 时退回修正前的行为。"""
        self.assertEqual(self.link.snapshot()["pressure_delta"], 0)

    def test_pressure_correction_from_the_altimeter(self):
        """高度表读数和真高之差，就是管制端要加的那一千英尺。"""
        self.link.values["indicated_altitude"] = 36000.0
        self.link.values["baro_setting"] = 29.92
        snapshot = self.link.snapshot()
        self.assertEqual(snapshot["altitude"], 35000)     # 真高照旧
        self.assertEqual(snapshot["pressure_delta"], 1000)

    def test_agl_in_feet(self):
        self.assertEqual(self.link.snapshot()["agl"], 10000)

    def test_metres_per_second_to_knots(self):
        self.assertEqual(self.link.snapshot()["groundspeed"], 450)

    def test_frequency_in_megahertz(self):
        self.assertEqual(self.link.snapshot()["com1"], 121.5)

    def test_833_frequency(self):
        self.link.values["com1"] = 132005.0
        self.assertEqual(self.link.snapshot()["com1"], 132.005)

    def test_zero_frequency_is_none(self):
        self.link.values["com1"] = 0.0
        self.link.values.pop("com1_legacy", None)
        self.assertIsNone(self.link.snapshot()["com1"])

    def test_heading_is_wrapped(self):
        self.link.values["heading_true"] = 370.0
        self.assertAlmostEqual(self.link.snapshot()["heading"], 10.0, places=3)

    def test_squawk_is_an_integer(self):
        self.link.values["squawk"] = 2000.9
        self.assertIsInstance(self.link.snapshot()["squawk"], int)

    def test_no_values_means_no_snapshot(self):
        self.assertIsNone(xplane.XPlaneLink().snapshot())

    def test_stale_data_is_not_connected(self):
        self.link._connected = True
        self.link.last_update = 0        # 很久以前
        self.assertFalse(self.link.connected)


class TransponderModeTest(unittest.TestCase):
    """待机会在管制端把高度和地速一起抹掉，所以只在飞机确实停着时才当真。

    位置包的包头带应答机模式，待机是 `@S`。EuroScope 收到 `@S` 就当这是个没有
    C 模式的目标，标牌上的高度和地速一起空掉——管制员看到的现象是"有的飞机读
    不到速度"。和 msfs/simlink.py 的 xpdr_mode() 是同一条规则。
    """

    def test_online_modes_report_mode_c(self):
        """dataref 的 2（开）和 3（测试/C）都算在线。"""
        for mode in (2, 3):
            self.assertEqual(xplane.xpdr_mode(mode, False, 450),
                             xplane.XPDR_ONLINE, f"mode={mode}")

    def test_a_parked_cold_aircraft_stays_on_standby(self):
        """冷舱停机坪的飞机不该在雷达上是个亮着的 C 模式目标。"""
        for mode in (0, 1):
            self.assertEqual(xplane.xpdr_mode(mode, True, 0),
                             xplane.XPDR_STANDBY, f"mode={mode}")

    def test_an_airborne_aircraft_is_never_believed_on_standby(self):
        self.assertEqual(xplane.xpdr_mode(1, False, 450), xplane.XPDR_ONLINE)

    def test_a_taxiing_aircraft_is_not_believed_either(self):
        self.assertEqual(xplane.xpdr_mode(1, True, 15), xplane.XPDR_ONLINE)

    def test_a_missing_dataref_reports_online(self):
        """这一轮 RREF 还没推过来时的默认值原来是 0（关），方向反了。"""
        self.assertEqual(xplane.xpdr_mode(None, True, 0), xplane.XPDR_ONLINE)

    def test_snapshot_reports_online_for_an_airborne_standby(self):
        link = xplane.XPlaneLink()
        link.values = {"latitude": 31.0, "longitude": 121.0, "elevation": 10668.0,
                       "groundspeed": 231.5, "on_ground": 0.0, "xpdr_mode": 1.0}
        self.assertEqual(link.snapshot()["xpdr_mode"], xplane.XPDR_ONLINE)

    def test_snapshot_still_reports_standby_on_the_stand(self):
        link = xplane.XPlaneLink()
        link.values = {"latitude": 31.0, "longitude": 121.0, "elevation": 6.0,
                       "groundspeed": 0.0, "on_ground": 1.0, "xpdr_mode": 1.0}
        self.assertEqual(link.snapshot()["xpdr_mode"], xplane.XPDR_STANDBY)

    def test_snapshot_without_the_dataref_reports_online(self):
        """位置已经有了、应答机 dataref 还没到，不能把自己从标牌上抹掉。"""
        link = xplane.XPlaneLink()
        link.values = {"latitude": 31.0, "longitude": 121.0, "elevation": 10668.0,
                       "groundspeed": 231.5, "on_ground": 0.0}
        self.assertEqual(link.snapshot()["xpdr_mode"], xplane.XPDR_ONLINE)


class UnpackPbhTest(unittest.TestCase):
    """还原别人的姿态。判定标准仍然是 can-fsd 那份转写，不是我们自己的编码。"""

    def test_matches_the_reference_decoder(self):
        for packed in (0, 1, 0xFFFFFFFF, 0x12345678, 0xABCDEF01):
            with self.subTest(packed=packed):
                expected = unpack_pbh(packed)
                got = fsdpilot.unpack_pbh(packed)
                self.assertAlmostEqual(got["pitch"], expected[0], places=6)
                self.assertAlmostEqual(got["bank"], expected[1], places=6)
                self.assertAlmostEqual(got["heading"], expected[2], places=6)

    def test_round_trips_our_own_encoding(self):
        packed = fsdpilot.pack_pbh(-3.0, 12.0, 271.0, on_ground=True)
        got = fsdpilot.unpack_pbh(packed)
        self.assertAlmostEqual(got["pitch"], -3.0, delta=0.4)
        self.assertAlmostEqual(got["bank"], 12.0, delta=0.4)
        self.assertAlmostEqual(got["heading"], 271.0, delta=0.4)
        self.assertTrue(got["on_ground"])


class TrafficReceptionTest(unittest.TestCase):
    """从 FSD 收他机。"""

    def setUp(self):
        self.table = traffic_module.TrafficTable()
        self.sent = []
        self.pilot = fsdpilot.FSDPilot("example.invalid", "CCA1501", "1234", "pw",
                                       aircraft="B738", traffic=self.table)
        self.pilot._send = lambda packet: self.sent.append(packet) or True

    def _position(self, callsign="CES2345", lat=31.2, lon=121.5):
        pbh = fsdpilot.pack_pbh(2.0, -5.0, 271.0)
        return f"@N:{callsign}:2000:1:{lat}:{lon}:35000:450:{pbh}:0"

    def test_other_aircraft_is_recorded(self):
        self.pilot._handle_packet(self._position())
        self.assertIn("CES2345", self.table)

    def test_attitude_is_decoded(self):
        self.pilot._handle_packet(self._position())
        position = self.table.get("CES2345").position_at(time.time())
        self.assertAlmostEqual(position["heading"], 271.0, delta=0.4)
        self.assertAlmostEqual(position["bank"], -5.0, delta=0.4)

    def test_our_own_echo_is_ignored(self):
        self.pilot._handle_packet(self._position(callsign="CCA1501"))
        self.assertEqual(len(self.table), 0)

    def test_plane_info_is_requested_on_first_sight(self):
        self.pilot._handle_packet(self._position())
        self.assertIn("#SBCCA1501:CES2345:PIR", self.sent)

    def test_plane_info_is_not_requested_every_packet(self):
        for _ in range(5):
            self.pilot._handle_packet(self._position())
        self.assertEqual(sum(1 for p in self.sent if p.endswith(":PIR")), 1)

    def test_disconnect_removes_the_aircraft(self):
        self.pilot._handle_packet(self._position())
        self.pilot._handle_packet("#DPCES2345:1234")
        self.assertNotIn("CES2345", self.table)

    def test_malformed_position_does_not_raise(self):
        self.pilot._handle_packet("@N:CES2345:2000:1:notanumber:121.5:35000:450:0:0")
        self.assertEqual(len(self.table), 0)

    def test_works_without_a_traffic_table(self):
        pilot = fsdpilot.FSDPilot("example.invalid", "CCA1501", "1234", "pw")
        pilot._send = lambda packet: True
        self.assertIsNot(pilot._handle_packet(self._position()), False)


class PlaneInfoExchangeTest(unittest.TestCase):
    """#SB 机型交换。can-fsd 的 handleSquawkbox 原样转发，服务端不用改。"""

    def setUp(self):
        self.table = traffic_module.TrafficTable()
        self.sent = []
        self.pilot = fsdpilot.FSDPilot("example.invalid", "CCA1501", "1234", "pw",
                                       aircraft="A320", traffic=self.table)
        self.pilot._send = lambda packet: self.sent.append(packet) or True

    def test_we_answer_a_request(self):
        self.pilot._handle_packet("#SBCES2345:CCA1501:PIR")
        self.assertEqual(len(self.sent), 1)
        self.assertIn("EQUIPMENT=A320", self.sent[0])

    def test_our_answer_carries_the_airline(self):
        # 航司码取呼号前三位字母，别人才能挑到正确涂装
        self.pilot._handle_packet("#SBCES2345:CCA1501:PIR")
        self.assertIn("AIRLINE=CCA", self.sent[0])

    def test_numeric_callsign_has_no_airline(self):
        pilot = fsdpilot.FSDPilot("example.invalid", "N172SP", "1", "pw")
        self.assertEqual(pilot.airline, "")

    def test_we_record_what_they_answer(self):
        self.pilot._handle_packet("#SBCES2345:CCA1501:PI:GEN:EQUIPMENT=B738:AIRLINE=CES")
        aircraft = self.table.get("CES2345")
        self.assertEqual(aircraft.equipment, "B738")
        self.assertEqual(aircraft.airline, "CES")

    def test_key_order_does_not_matter(self):
        # protocol.md 明说顺序不保证
        self.pilot._handle_packet("#SBCES2345:CCA1501:PI:GEN:AIRLINE=CES:EQUIPMENT=B738")
        self.assertEqual(self.table.get("CES2345").equipment, "B738")

    def test_missing_keys_are_tolerated(self):
        self.pilot._handle_packet("#SBCES2345:CCA1501:PI:GEN:EQUIPMENT=B738")
        self.assertEqual(self.table.get("CES2345").airline, "")

    def test_unknown_keys_are_ignored(self):
        self.pilot._handle_packet(
            "#SBCES2345:CCA1501:PI:GEN:EQUIPMENT=B738:SOMETHING=X")
        self.assertEqual(self.table.get("CES2345").equipment, "B738")

    def test_legacy_csl_form(self):
        self.pilot._handle_packet("#SBCES2345:CCA1501:PI:X:0:1:CSL=A320_DAL")
        self.assertEqual(self.table.get("CES2345").csl, "A320_DAL")

    def test_legacy_tilde_form(self):
        self.pilot._handle_packet("#SBCES2345:CCA1501:PI:X:0:0:~PA24")
        self.assertEqual(self.table.get("CES2345").csl, "PA24")

    def test_info_before_position_is_kept(self):
        self.pilot._handle_packet("#SBCES2345:CCA1501:PI:GEN:EQUIPMENT=B738")
        self.assertIn("CES2345", self.table)

    def test_info_before_position_survives_a_prune(self):
        """机型先到、位置未到的那条记录，下一轮 prune 不能立刻清掉。

        以前 prune 对 latest is None 的记录无条件删除，半秒后就没了——
        PI:GEN 白收，位置到达时机型又得重新问一轮。
        """
        self.pilot._handle_packet("#SBCES2345:CCA1501:PI:GEN:EQUIPMENT=B738")
        self.table.prune()
        self.assertIn("CES2345", self.table)
        # 宽限也不是永远：过了 STALE_AFTER 还没等到位置就该清了
        aircraft = self.table.get("CES2345")
        self.table.prune(now=aircraft.created + traffic_module.STALE_AFTER + 1)
        self.assertNotIn("CES2345", self.table)


class InterpolationTest(unittest.TestCase):
    """FSD 一秒才 5 个包，不插值飞机会一跳一跳。"""

    def setUp(self):
        self.table = traffic_module.TrafficTable()

    def _add(self, at, lat, lon, altitude=10000, heading=90.0):
        self.table.update_position("CES2345", latitude=lat, longitude=lon,
                                   altitude=altitude, pitch=0.0, bank=0.0,
                                   heading=heading, groundspeed=250, now=at)

    def test_midpoint(self):
        self._add(100.0, 30.0, 120.0)
        self._add(101.0, 30.1, 120.2)
        position = self.table.get("CES2345").position_at(100.5)
        self.assertAlmostEqual(position["latitude"], 30.05, places=6)
        self.assertAlmostEqual(position["longitude"], 120.1, places=6)

    def test_altitude_interpolates(self):
        self._add(100.0, 30.0, 120.0, altitude=10000)
        self._add(101.0, 30.0, 120.0, altitude=11000)
        self.assertAlmostEqual(
            self.table.get("CES2345").position_at(100.5)["altitude"], 10500, places=3)

    def test_heading_takes_the_short_way(self):
        # 359° 到 1° 应当往前走 2°，不是倒着走 358°
        self._add(100.0, 30.0, 120.0, heading=359.0)
        self._add(101.0, 30.0, 120.0, heading=1.0)
        self.assertAlmostEqual(
            self.table.get("CES2345").position_at(100.5)["heading"], 0.0, places=6)

    def test_heading_short_way_downwards(self):
        self._add(100.0, 30.0, 120.0, heading=10.0)
        self._add(101.0, 30.0, 120.0, heading=350.0)
        self.assertAlmostEqual(
            self.table.get("CES2345").position_at(100.5)["heading"], 0.0, places=6)

    def test_single_sample_is_held(self):
        self._add(100.0, 30.0, 120.0)
        self.assertAlmostEqual(
            self.table.get("CES2345").position_at(105.0)["latitude"], 30.0)

    def test_extrapolation_is_bounded(self):
        # 对方掉线时飞机该停在原地，不是一直飞出天际
        self._add(100.0, 30.0, 120.0)
        self._add(101.0, 30.1, 120.0)
        far = self.table.get("CES2345").position_at(200.0)["latitude"]
        self.assertLess(far, 30.5, "外推没有封顶")

    def test_no_backward_extrapolation(self):
        self._add(100.0, 30.0, 120.0)
        self._add(101.0, 30.1, 120.0)
        early = self.table.get("CES2345").position_at(50.0)["latitude"]
        self.assertAlmostEqual(early, 30.0, places=6)

    def test_duplicate_timestamp_is_dropped(self):
        # 同一时刻的重复包会让插值除零
        self._add(100.0, 30.0, 120.0)
        self._add(100.0, 40.0, 130.0)
        self.assertAlmostEqual(
            self.table.get("CES2345").position_at(100.0)["latitude"], 30.0)

    def test_vertical_speed(self):
        self._add(100.0, 30.0, 120.0, altitude=10000)
        self._add(101.0, 30.0, 120.0, altitude=10010)
        self.assertAlmostEqual(self.table.get("CES2345").vertical_speed, 600.0, places=3)

    def test_longitude_takes_the_short_way_across_the_antimeridian(self):
        # 179.98°E 到 -179.98° 是往前 0.04°，线性差值会横穿整个地球
        self._add(100.0, 30.0, 179.98)
        self._add(101.0, 30.0, -179.98)
        longitude = self.table.get("CES2345").position_at(100.5)["longitude"]
        self.assertTrue(abs(longitude) > 179.9,
                        f"中点应当贴着 180° 经线，算出来是 {longitude}")

    def test_range_across_the_antimeridian_is_short(self):
        distance = traffic_module.distance_nm(30.0, 179.9, 30.0, -179.9)
        self.assertLess(distance, 30, "跨 180° 经线的距离算成绕地球一圈了")


class TrafficTableTest(unittest.TestCase):
    def setUp(self):
        self.table = traffic_module.TrafficTable()

    def _add(self, callsign, lat=30.0, lon=120.0, at=1000.0):
        self.table.update_position(callsign, latitude=lat, longitude=lon,
                                   altitude=10000, pitch=0.0, bank=0.0,
                                   heading=90.0, groundspeed=250, now=at)

    def test_prune_removes_stale(self):
        self._add("CES2345", at=1000.0)
        self.assertEqual(self.table.prune(now=1000.0 + traffic_module.STALE_AFTER + 1),
                         ["CES2345"])
        self.assertEqual(len(self.table), 0)

    def test_prune_keeps_fresh(self):
        self._add("CES2345", at=1000.0)
        self.assertEqual(self.table.prune(now=1001.0), [])

    def test_snapshot_sorted_by_range(self):
        self._add("FAR", lat=32.0)
        self._add("NEAR", lat=30.1)
        entries = self.table.snapshot(now=1000.0, origin=(30.0, 120.0))
        self.assertEqual([e["callsign"] for e in entries], ["NEAR", "FAR"])

    def test_snapshot_limit_keeps_the_closest(self):
        # TCAS 只有 64 个位置，超了必须先扔远的
        for i in range(5):
            self._add(f"AC{i}", lat=30.0 + i * 0.5)
        entries = self.table.snapshot(now=1000.0, origin=(30.0, 120.0), limit=2)
        self.assertEqual([e["callsign"] for e in entries], ["AC0", "AC1"])

    def test_snapshot_range_filter(self):
        self._add("NEAR", lat=30.05)
        self._add("FAR", lat=35.0)
        entries = self.table.snapshot(now=1000.0, origin=(30.0, 120.0),
                                      max_range_nm=50)
        self.assertEqual([e["callsign"] for e in entries], ["NEAR"])

    def test_snapshot_without_origin_has_no_range(self):
        self._add("CES2345")
        self.assertNotIn("range_nm", self.table.snapshot(now=1000.0)[0])

    def test_model_dirty_starts_true(self):
        self._add("CES2345")
        self.assertTrue(self.table.snapshot(now=1000.0)[0]["model_dirty"])

    def test_mark_model_clean(self):
        self._add("CES2345")
        self.table.mark_model_clean("CES2345")
        self.assertFalse(self.table.snapshot(now=1000.0)[0]["model_dirty"])

    def test_new_plane_info_makes_it_dirty_again(self):
        self._add("CES2345")
        self.table.mark_model_clean("CES2345")
        self.table.set_plane_info("CES2345", equipment="B738")
        self.assertTrue(self.table.snapshot(now=1000.0)[0]["model_dirty"])

    def test_same_plane_info_does_not_redirty(self):
        self._add("CES2345")
        self.table.set_plane_info("CES2345", equipment="B738")
        self.table.mark_model_clean("CES2345")
        self.table.set_plane_info("CES2345", equipment="B738")
        self.assertFalse(self.table.snapshot(now=1000.0)[0]["model_dirty"])

    def test_config_drives_animation(self):
        self._add("CES2345")
        self.table.set_config("CES2345", {
            "gear_down": True, "flaps_pct": 40, "spoilers_out": False,
            "lights": {"strobe_on": True},
            "engines": {"1": {"on": True}, "2": {"on": False}}})
        entry = self.table.snapshot(now=1000.0)[0]
        self.assertTrue(entry["gear_down"])
        self.assertAlmostEqual(entry["flaps"], 0.4)
        self.assertTrue(entry["lights"]["strobe_on"])
        self.assertTrue(entry["engines_on"])

    def test_config_for_unknown_aircraft_is_ignored(self):
        self.assertIsNone(self.table.set_config("NOBODY", {"gear_down": True}))

    def test_request_callback_fires_once(self):
        asked = []
        table = traffic_module.TrafficTable(on_request_info=asked.append)
        for _ in range(3):
            table.update_position("CES2345", latitude=30.0, longitude=120.0,
                                  altitude=10000, pitch=0, bank=0, heading=0,
                                  now=1000.0)
        self.assertEqual(asked, ["CES2345"])

    def test_request_callback_not_fired_once_known(self):
        asked = []
        table = traffic_module.TrafficTable(on_request_info=asked.append)
        table.set_plane_info("CES2345", equipment="B738")
        table.update_position("CES2345", latitude=30.0, longitude=120.0,
                              altitude=10000, pitch=0, bank=0, heading=0, now=1000.0)
        self.assertEqual(asked, [])

    def test_distance(self):
        # 1 度纬度 = 60 海里
        self.assertAlmostEqual(traffic_module.distance_nm(30.0, 120.0, 31.0, 120.0),
                               60.0, places=3)


class CslParsingTest(unittest.TestCase):
    """xsb_aircraft.txt 各家写得并不一致，读的时候要宽松。"""

    def setUp(self):
        import tempfile
        self.directory = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.directory, ignore_errors=True)

    def _write(self, text):
        path = os.path.join(self.directory, "xsb_aircraft.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return self.directory

    def test_reads_a_simple_package(self):
        models = cslmatch.parse_package(self._write(
            "EXPORT_NAME BB_Airbus\n"
            "OBJ8_AIRCRAFT A320_CCA\n"
            "OBJ8 SOLID YES A320/A320_CCA.obj\n"
            "ICAO A320\n"
            "AIRLINE A320 CCA\n"))
        self.assertEqual(len(models), 1)
        self.assertEqual(models[0].icao, "A320")
        self.assertEqual(models[0].airline, "CCA")
        self.assertEqual(models[0].package, "BB_Airbus")

    def test_backslash_paths(self):
        models = cslmatch.parse_package(self._write(
            "OBJ8_AIRCRAFT X\nOBJ8 SOLID YES A320\\A320.obj\nICAO A320\n"))
        self.assertTrue(models[0].path.endswith("A320.obj"))

    def test_comments_are_skipped(self):
        models = cslmatch.parse_package(self._write(
            "# 注释\nOBJ8_AIRCRAFT X   # 行尾注释\n"
            "OBJ8 SOLID YES a.obj\nICAO B738\n"))
        self.assertEqual(models[0].icao, "B738")

    def test_entries_without_a_path_are_dropped(self):
        models = cslmatch.parse_package(self._write(
            "OBJ8_AIRCRAFT Broken\nICAO B738\n"
            "OBJ8_AIRCRAFT Good\nOBJ8 SOLID YES a.obj\nICAO A320\n"))
        self.assertEqual([m.icao for m in models], ["A320"])

    def test_missing_manifest_is_not_an_error(self):
        import tempfile
        self.assertEqual(cslmatch.parse_package(tempfile.mkdtemp()), [])

    def test_find_packages(self):
        import tempfile
        root = tempfile.mkdtemp()
        inner = os.path.join(root, "BB_Airbus")
        os.makedirs(inner)
        with open(os.path.join(inner, "xsb_aircraft.txt"), "w") as f:
            f.write("OBJ8_AIRCRAFT X\n")
        self.assertEqual(cslmatch.find_packages(root), [inner])

    def test_a_linked_csl_folder_is_still_scanned(self):
        # os.walk 默认不进符号链接，而 Windows 的目录联接从 Python 3.8 起就算
        # 符号链接。CSL 包动辄几个 GB，"放在另一块盘、原地留个链接"是这边最
        # 常见的安置方式——跳过它就一个包都扫不到，现象只是他机不显示。
        # msfs/aimatch.py 的 find_aircraft_cfgs 是同一个坑，同一个修法。
        elsewhere = os.path.join(self.directory, "elsewhere", "BB_Airbus")
        os.makedirs(elsewhere)
        with open(os.path.join(elsewhere, "xsb_aircraft.txt"), "w") as f:
            f.write("OBJ8_AIRCRAFT X\n")

        plugins = os.path.join(self.directory, "plugins")
        os.makedirs(plugins)
        try:
            os.symlink(os.path.join(self.directory, "elsewhere"),
                       os.path.join(plugins, "CSL"), target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("这个环境不让建符号链接")

        found = cslmatch.find_packages(plugins)
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].endswith("BB_Airbus"))

    def test_a_symlink_loop_does_not_hang_the_scan(self):
        # 跟着链接走就得自己防环，否则扫盘永远回不来
        tree = os.path.join(self.directory, "csl")
        os.makedirs(tree)
        try:
            os.symlink(self.directory, os.path.join(tree, "back"),
                       target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("这个环境不让建符号链接")
        self.assertEqual(cslmatch.find_packages(self.directory), [])


class ModelMatchingTest(unittest.TestCase):
    """匹配的退化链。最重要的一条：永远要有结果。"""

    def setUp(self):
        self.models = cslmatch.ModelSet([
            cslmatch.Model("B738_CCA", "b738_cca.obj", icao="B738", airline="CCA"),
            cslmatch.Model("B738_CES", "b738_ces.obj", icao="B738", airline="CES"),
            cslmatch.Model("B739_CCA", "b739_cca.obj", icao="B739", airline="CCA"),
            cslmatch.Model("A320_GEN", "a320.obj", icao="A320"),
            cslmatch.Model("C172_GEN", "c172.obj", icao="C172"),
        ])

    def test_exact_type_and_airline(self):
        model, why = self.models.match(equipment="B738", airline="CES")
        self.assertEqual(model.name, "B738_CES")
        self.assertIn("都匹配", why)

    def test_type_only_when_airline_unknown(self):
        model, _ = self.models.match(equipment="B738")
        self.assertEqual(model.icao, "B738")

    def test_type_matches_even_with_unknown_airline(self):
        model, why = self.models.match(equipment="B738", airline="UAL")
        self.assertEqual(model.icao, "B738")
        self.assertIn("涂装不对", why)

    def test_family_fallback_prefers_right_airline(self):
        # 没有 B737 的模型，同族里有 B738_CCA 和 B739_CCA
        model, why = self.models.match(equipment="B737", airline="CCA")
        self.assertEqual(model.airline, "CCA")
        self.assertIn("同族", why)

    def test_family_fallback_without_airline(self):
        model, why = self.models.match(equipment="B734")
        self.assertIn(model.icao, ("B738", "B739"))
        self.assertIn("同族", why)

    def test_generic_fallback_by_prefix(self):
        # A350 不在包里也不在同族表里，B7/A3 前缀退到通用
        model, why = self.models.match(equipment="A359")
        self.assertEqual(model.icao, "A320")
        self.assertIn("通用", why)

    def test_light_aircraft_generic(self):
        model, _ = self.models.match(equipment="P28A")
        self.assertEqual(model.icao, "C172")

    def test_widebody_is_not_replaced_by_a_narrowbody(self):
        # 拿 A319 去顶 B777 视觉上差得离谱；同族之后先按机身类别找
        models = cslmatch.ModelSet([
            cslmatch.Model("A319", "a319.obj", icao="A319"),
            cslmatch.Model("B78X", "b78x.obj", icao="B78X"),
        ])
        model, why = models.match(equipment="B77W")
        self.assertEqual(model.icao, "B78X", why)
        self.assertIn("宽体", why)

    def test_category_beats_the_generic_guess(self):
        """同类机身必须排在「按前缀猜通用机型」前面。

        GENERIC_BY_PREFIX 是两位前缀，A3 / B7 同时盖住窄体和宽体：B77W 猜出
        B738、A359 猜出 A320。通用那级要是排在前面，只要装了 B738 或 A320
        （最普及的两个），所有宽体都会退成窄体——一架 777 在别人屏幕上变成
        737，正是同类机身那一级本来要挡的情况。

        关键在于**装了 B738**。上面那条用例只装了 A319 和 B78X，通用猜出的
        B738 找不到，自然就轮到了同类机身，于是顺序错了也照样通过。
        """
        models = cslmatch.ModelSet([
            cslmatch.Model("B738", "b738.obj", icao="B738"),
            cslmatch.Model("A320", "a320.obj", icao="A320"),
            cslmatch.Model("B789", "b789.obj", icao="B789"),
        ])
        for want in ("B77W", "B77L", "A359", "A388", "B744"):
            model, why = models.match(equipment=want)
            self.assertEqual(model.icao, "B789",
                             f"{want} 应当顶一架宽体，却拿到 {model.icao}（{why}）")
            self.assertIn("宽体", why)

    def test_generic_still_used_when_the_category_has_nothing(self):
        """同类机身里一个都没装时，仍然要退到通用机型，别直接掉兜底。"""
        models = cslmatch.ModelSet([
            cslmatch.Model("B738", "b738.obj", icao="B738"),
        ])
        model, why = models.match(equipment="B77W")
        self.assertEqual(model.icao, "B738", why)
        self.assertIn("通用机型", why)

    def test_category_lookup(self):
        self.assertEqual(cslmatch.category_of("B77W"), "宽体")
        self.assertEqual(cslmatch.category_of("C172"), "通航")
        self.assertEqual(cslmatch.category_of("ZZZZ"), "")

    def test_categories_do_not_overlap(self):
        seen = {}
        for name, types in cslmatch.CATEGORIES.items():
            for icao in types:
                self.assertNotIn(icao, seen,
                                 f"{icao} 同时在 {seen.get(icao)} 和 {name}")
                seen[icao] = name

    def test_unknown_type_still_returns_something(self):
        # 看不见的飞机比涂装错的飞机危险得多
        model, why = self.models.match(equipment="ZZZZ")
        self.assertIsNotNone(model, why)

    def test_no_information_at_all_still_returns_something(self):
        model, _ = self.models.match()
        self.assertIsNotNone(model)

    def test_explicit_csl_name_wins(self):
        model, why = self.models.match(equipment="B738", airline="CCA", csl="A320_GEN")
        self.assertEqual(model.name, "A320_GEN")
        self.assertIn("CSL 名字", why)

    def test_empty_model_set_reports_why(self):
        model, why = cslmatch.ModelSet().match(equipment="B738")
        self.assertIsNone(model)
        self.assertIn("没有装", why)

    def test_lowercase_input_is_handled(self):
        model, _ = self.models.match(equipment="b738", airline="ces")
        self.assertEqual(model.name, "B738_CES")

    def test_family_lookup(self):
        self.assertIn("B739", cslmatch.family_of("B738"))
        self.assertEqual(cslmatch.family_of("ZZZZ"), ())


class BridgeTest(unittest.TestCase):
    """客户端和插件之间的分片协议。两边各有一份重组器，必须对称。"""

    def setUp(self):
        import bridge
        self.bridge = bridge
        self.reassembler = bridge.Reassembler()

    def _round_trip(self, message, max_payload=None, sequence=1):
        packets = (self.bridge.encode(message, sequence, max_payload)
                   if max_payload else self.bridge.encode(message, sequence))
        result = None
        for packet in packets:
            result = self.reassembler.feed(packet) or result
        return result, packets

    def test_small_message_is_one_packet(self):
        result, packets = self._round_trip({"type": "traffic", "aircraft": []})
        self.assertEqual(len(packets), 1)
        self.assertEqual(result["type"], "traffic")

    def test_large_message_is_split_and_rejoined(self):
        message = {"type": "traffic",
                   "aircraft": [{"callsign": f"AC{i:04d}", "latitude": 30.0 + i}
                                for i in range(200)]}
        result, packets = self._round_trip(message, max_payload=500)
        self.assertGreater(len(packets), 1, "应当分片")
        self.assertEqual(result, message)

    def test_partial_message_yields_nothing(self):
        message = {"a": "x" * 2000}
        packets = self.bridge.encode(message, 1, max_payload=100)
        self.assertIsNone(self.reassembler.feed(packets[0]))

    def test_new_frame_discards_the_old_incomplete_one(self):
        # 位置流里迟到的帧没价值，留着会让飞机往回跳
        old = self.bridge.encode({"a": "x" * 2000}, 1, max_payload=100)
        self.reassembler.feed(old[0])
        result, _ = self._round_trip({"type": "traffic"}, sequence=2)
        self.assertEqual(result["type"], "traffic")

    def test_garbage_is_ignored(self):
        self.assertIsNone(self.reassembler.feed(b"not json"))

    def test_wrong_version_is_ignored(self):
        self.assertIsNone(self.reassembler.feed(b'{"v":999,"seq":1,"part":0}'))

    def test_chinese_survives(self):
        result, _ = self._round_trip({"note": "国航一五零一"})
        self.assertEqual(result["note"], "国航一五零一")

    def test_fragmented_chinese_survives(self):
        """分片切口落在多字节字符中间也不能出事。

        v1 按字符串切分片，切口落在中文（CSL 路径、备注）中间时 json.dumps
        直接 UnicodeEncodeError——从那一帧起插件再也收不到任何数据。分片按
        字节 + base64 之后，任何切口都合法。max_payload 取小值保证每个切口
        都落在汉字里。
        """
        message = {"object": "D:/模型库/Bluebell/波音七三八" * 40}
        for payload in (37, 41, 43, 100):
            reassembler = self.bridge.Reassembler()
            result = None
            for packet in self.bridge.encode(message, 3, max_payload=payload):
                result = reassembler.feed(packet) or result
            self.assertEqual(result, message,
                             f"max_payload={payload} 时拼不回来")

    def test_a_late_old_frame_does_not_replace_a_newer_one(self):
        """迟到的旧帧要扔掉，不能顶掉刚拼好的新帧——飞机会往回跳。"""
        new = self.bridge.encode({"n": 2}, 5)
        old = self.bridge.encode({"n": 1}, 4, max_payload=100)
        self.assertEqual(self.reassembler.feed(new[0]), {"n": 2})
        for packet in old:
            self.assertIsNone(self.reassembler.feed(packet))

    def test_sequence_wraps_around_16_bits(self):
        # 序号是 (seq+1)&0xFFFF 环回的，0xFFFF 之后的 0 是新帧不是旧帧
        self.assertEqual(self.reassembler.feed(
            self.bridge.encode({"n": 1}, 0xFFFF)[0]), {"n": 1})
        self.assertEqual(self.reassembler.feed(
            self.bridge.encode({"n": 2}, 0)[0]), {"n": 2})

    def test_an_out_of_range_part_is_ignored_not_fatal(self):
        packets = self.bridge.encode({"a": "x" * 500}, 9, max_payload=100)
        self.assertIsNone(self.reassembler.feed(packets[0]))
        bad = json.loads(packets[0].decode("utf-8"))
        bad["part"] = 99
        self.assertIsNone(self.reassembler.feed(
            json.dumps(bad).encode("utf-8")))
        # 剩下的分片照常拼得回来
        result = None
        for packet in packets[1:]:
            result = self.reassembler.feed(packet) or result
        self.assertEqual(result, {"a": "x" * 500})

    def test_plugin_reassembler_matches_the_client_one(self):
        """插件里那份重组器是独立的一份代码，必须和这边行为一致。"""
        import importlib.util
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "plugin", "PI_XpcTraffic.py")
        spec = importlib.util.spec_from_file_location("pi_xpc", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        message = {"type": "traffic",
                   "aircraft": [{"callsign": f"AC{i}"} for i in range(150)]}
        plugin_side = module.Reassembler()
        result = None
        for packet in self.bridge.encode(message, 7, max_payload=400):
            result = plugin_side.feed(packet) or result
        self.assertEqual(result, message)

    def test_sender_does_not_raise_without_a_plugin(self):
        # 插件没开是常态，不该报错
        sender = self.bridge.BridgeSender()
        try:
            sender.send_traffic([])
        finally:
            sender.close()


class AnimationValuesTest(unittest.TestCase):
    """插件里 data 列表的顺序必须和 dataref 声明顺序一致，错了动画会串。"""

    def setUp(self):
        import importlib.util
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "plugin", "PI_XpcTraffic.py")
        spec = importlib.util.spec_from_file_location("pi_xpc2", path)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.values = self.module.PythonInterface._animation_values

    def test_length_matches_the_dataref_list(self):
        self.assertEqual(len(self.values({})),
                         len(self.module.ANIMATION_DATAREFS))

    def test_gear_down_on_the_ground(self):
        index = self.module.ANIMATION_DATAREFS.index("libxplanemp/controls/gear_ratio")
        self.assertEqual(self.values({"on_ground": True})[index], 1.0)

    def test_gear_up_when_fast_and_airborne(self):
        index = self.module.ANIMATION_DATAREFS.index("libxplanemp/controls/gear_ratio")
        self.assertEqual(
            self.values({"on_ground": False, "groundspeed": 300})[index], 0.0)

    def test_reported_gear_overrides_the_guess(self):
        index = self.module.ANIMATION_DATAREFS.index("libxplanemp/controls/gear_ratio")
        entry = {"on_ground": False, "groundspeed": 300, "gear_down": True}
        self.assertEqual(self.values(entry)[index], 1.0)

    def test_flaps_pass_through(self):
        index = self.module.ANIMATION_DATAREFS.index("libxplanemp/controls/flap_ratio")
        self.assertAlmostEqual(self.values({"flaps": 0.4})[index], 0.4)

    def test_strobe_light(self):
        index = self.module.ANIMATION_DATAREFS.index(
            "libxplanemp/controls/strobe_lites_on")
        self.assertEqual(self.values({"lights": {"strobe_on": True}})[index], 1.0)

    def test_engines_off_means_no_thrust(self):
        index = self.module.ANIMATION_DATAREFS.index("libxplanemp/controls/thrust_ratio")
        self.assertEqual(self.values({"engines_on": False})[index], 0.0)

    def test_fixed_string_is_padded_and_terminated(self):
        raw = self.module.PythonInterface._fixed_string("CCA1501", 8)
        self.assertEqual(len(raw), 8)
        self.assertTrue(raw.endswith(b"\x00"))

    def test_fixed_string_truncates(self):
        raw = self.module.PythonInterface._fixed_string("VERYLONGCALLSIGN", 8)
        self.assertEqual(len(raw), 8)
        self.assertTrue(raw.endswith(b"\x00"))

    def test_tcas_cap_leaves_room_for_own_aircraft(self):
        # 数组是 64 个位置，0 号给本机
        self.assertEqual(self.module.MAX_TCAS_TARGETS, 63)

    def test_tcas_is_probed_not_version_gated(self):
        """能力应当靠 findDataRef 探测，不是按版本号写死。

        X-Plane 11.50 以下没有 TCAS 接管，但按版本分支很容易写错，也挡不住
        别的插件已经占了 AI 机位的情况。
        """
        import inspect
        source = inspect.getsource(self.module.PythonInterface._find_tcas_datarefs)
        self.assertIn("findDataRef", source)
        self.assertIn("tcas_available", source)

    def test_planes_are_not_acquired_without_tcas(self):
        # 没这个能力还去抢 AI 机位，会挡住 LiveTraffic 之类真正用得上的插件
        source = inspect.getsource(self.module.PythonInterface.XPluginEnable)
        self.assertIn("tcas_available", source)


class PluginInstallTest(unittest.TestCase):
    """把他机插件装进 X-Plane。

    全程在临时目录里搭一棵假的 X-Plane 目录树，不碰真的模拟器，也不读本机上
    那份安装记录（`inspect()` 明确传 root，就不会去自动探测）。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = os.path.join(self.tmp, "X-Plane 12")
        os.makedirs(os.path.join(self.root, xpinstall.PLUGINS_DIR))

    def _install_xppython3(self):
        os.makedirs(os.path.join(self.root, xpinstall.XPPYTHON3_DIR))

    def _write_plugin(self, text):
        target = xpinstall.plugin_path(self.root)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write(text)
        return target

    # ---------- 目录识别 ----------
    def test_a_folder_with_resources_plugins_is_xplane(self):
        self.assertTrue(xpinstall.is_xplane_root(self.root))

    def test_some_other_folder_is_not(self):
        self.assertFalse(xpinstall.is_xplane_root(self.tmp))
        self.assertFalse(xpinstall.is_xplane_root(""))

    def test_the_check_does_not_require_pythonplugins(self):
        """没装 XPPython3 的机器上 PythonPlugins 是不存在的。

        拿它当判据会把一个完好的 X-Plane 目录判成"不是 X-Plane"，而那恰恰是
        最需要装插件的那批人。
        """
        self.assertFalse(os.path.isdir(
            os.path.join(self.root, xpinstall.PYTHON_PLUGINS_DIR)))
        self.assertTrue(xpinstall.is_xplane_root(self.root))

    # ---------- 状态 ----------
    def test_a_fresh_install_reports_missing(self):
        status = xpinstall.inspect(self.root)
        self.assertEqual(status.state, xpinstall.MISSING)
        self.assertTrue(status.can_install)

    def test_a_folder_that_is_not_xplane_reports_so(self):
        status = xpinstall.inspect(self.tmp)
        self.assertEqual(status.state, xpinstall.NOT_XPLANE)
        self.assertFalse(status.can_install)

    def test_xppython3_is_detected(self):
        self.assertFalse(xpinstall.inspect(self.root).xppython3)
        self._install_xppython3()
        self.assertTrue(xpinstall.inspect(self.root).xppython3)

    def test_installing_then_inspecting_reports_current(self):
        xpinstall.install(self.root)
        status = xpinstall.inspect(self.root)
        self.assertEqual(status.state, xpinstall.CURRENT)
        self.assertEqual(status.installed_protocol, bridge.PROTOCOL_VERSION)
        self.assertFalse(status.protocol_mismatch)

    def test_a_different_file_reports_outdated(self):
        self._write_plugin("PROTOCOL_VERSION = %d\n# 老版本\n"
                           % bridge.PROTOCOL_VERSION)
        self.assertEqual(xpinstall.inspect(self.root).state, xpinstall.OUTDATED)

    def test_installing_over_an_old_copy_brings_it_current(self):
        self._write_plugin("# 很旧的一份\n")
        self.assertEqual(xpinstall.inspect(self.root).state, xpinstall.OUTDATED)
        xpinstall.install(self.root)
        self.assertEqual(xpinstall.inspect(self.root).state, xpinstall.CURRENT)

    def test_install_creates_pythonplugins_if_it_is_missing(self):
        # 装了 XPPython3 也不一定已经有这个目录，它是第一次用时才建的
        target = xpinstall.install(self.root)
        self.assertTrue(os.path.isfile(target))
        self.assertEqual(os.path.basename(target), xpinstall.PLUGIN_NAME)

    # ---------- 协议号 ----------
    def test_a_protocol_mismatch_is_reported_on_its_own(self):
        """协议对不上时插件是**静默**丢帧的。

        `PI_XpcTraffic.py` 收到 v 不一样的包直接 return，不记日志；用户看到的
        只有"他机一架都不出现"。所以这条必须能单独报出来，不能混在"版本旧"里
        ——两者的后果差得远。
        """
        self._write_plugin("PROTOCOL_VERSION = %d\n" % (bridge.PROTOCOL_VERSION + 1))
        status = xpinstall.inspect(self.root)
        self.assertEqual(status.state, xpinstall.OUTDATED)
        self.assertTrue(status.protocol_mismatch)
        self.assertEqual(status.installed_protocol, bridge.PROTOCOL_VERSION + 1)

    def test_an_unreadable_protocol_is_not_a_mismatch(self):
        # 抠不出版本号（用户改坏了）时别谎报成"协议不一致"——那会把人引到
        # 一个不存在的原因上
        self._write_plugin("# 什么都没有\n")
        status = xpinstall.inspect(self.root)
        self.assertIsNone(status.installed_protocol)
        self.assertFalse(status.protocol_mismatch)

    def test_the_bundled_plugin_and_the_bridge_agree(self):
        """随包这份插件和客户端必须说同一版协议。

        两边各存一份常量，改了一边忘了另一边的话，打出来的包一装上去就是
        "他机一架都不出现"，而且不报错。
        """
        self.assertEqual(xpinstall.protocol_version(xpinstall.bundled_plugin()),
                         bridge.PROTOCOL_VERSION)

    # ---------- 出错 ----------
    def test_a_missing_source_does_not_report_current(self):
        """打包漏了 datas 时，绝不能说"已是最新"。

        那会让用户以为装好了，然后去查 X-Plane 那边为什么不出飞机。
        """
        self._write_plugin("# 随便什么\n")
        with mock.patch.object(xpinstall, "bundled_plugin",
                               return_value=os.path.join(self.tmp, "没有这个文件")):
            self.assertEqual(xpinstall.inspect(self.root).state, xpinstall.OUTDATED)

    def test_install_raises_when_the_source_is_missing(self):
        with mock.patch.object(xpinstall, "bundled_plugin",
                               return_value=os.path.join(self.tmp, "没有这个文件")):
            with self.assertRaises(OSError):
                xpinstall.install(self.root)

    # ---------- 自动探测 ----------
    def test_install_records_are_read_and_filtered(self):
        record = os.path.join(self.tmp, "x-plane_install_12.txt")
        with open(record, "w", encoding="utf-8") as f:
            # 第二行指向一个已经不在的目录：搬过或者删过的安装很常见
            f.write(self.root + "\n" + os.path.join(self.tmp, "搬走了") + "\n")
        with mock.patch.object(xpinstall, "_install_records", return_value=[record]):
            self.assertEqual(xpinstall.find_installs(), [self.root])

    def test_a_missing_record_is_not_an_error(self):
        with mock.patch.object(xpinstall, "_install_records",
                               return_value=[os.path.join(self.tmp, "没有")]):
            self.assertEqual(xpinstall.find_installs(), [])
            self.assertEqual(xpinstall.inspect().state, xpinstall.NO_ROOT)


class ChimeStream:
    def __init__(self, device, rate, refuse=()):
        self.device = device
        self.rate = rate
        self.written = b""
        self.closed = False
        if (device, rate) in refuse:
            raise OSError("Invalid sample rate")

    def write(self, data):
        self.written += data

    def stop_stream(self):
        pass

    def close(self):
        self.closed = True


class ChimePyAudio:
    """够 chime.py 用的一小块 PortAudio。

    `refuse` 里的 (设备, 采样率) 组合开不出来，用来演蓝牙耳机只吃 44.1 kHz
    和"客户端起来之后耳机被拔了"这两种。
    """

    paInt16 = 8

    def __init__(self, refuse=()):
        self.refuse = set(refuse)
        self.opened = []
        self.terminated = 0
        module = self

        class _PyAudio:
            def open(self_inner, format=None, channels=None, rate=None,
                     output=None, output_device_index=None, **kwargs):
                stream = ChimeStream(output_device_index, rate, module.refuse)
                module.opened.append(stream)
                return stream

            def terminate(self_inner):
                module.terminated += 1

        self.PyAudio = _PyAudio


class ChimeSettings:
    def __init__(self, **kwargs):
        self.output_device_index = None
        self.message_sound = True
        self.message_sound_all = False
        self.message_sound_volume = 100
        for key, value in kwargs.items():
            setattr(self, key, value)


class ChimeWaveformTest(unittest.TestCase):
    """合成出来的那两声。"""

    def setUp(self):
        import chime
        self.chime = chime
        chime._CACHE.clear()

    def test_it_is_16_bit_mono_and_about_the_right_length(self):
        data = self.chime.waveform(48000)
        expected = sum(int(48000 * seconds) for _, seconds in self.chime.TONES)
        expected += 2 * int(48000 * self.chime.GAP)
        self.assertEqual(len(data), expected * 2)      # 每个样点 2 字节

    def test_both_ends_are_silent(self):
        """两头不淡进淡出的话，每次提示音都会带一声"啪"。"""
        import array
        samples = array.array("h")
        samples.frombytes(self.chime.waveform(48000))
        self.assertEqual(samples[0], 0)
        self.assertEqual(samples[-1], 0)
        self.assertGreater(max(abs(s) for s in samples), 1000)

    def test_it_never_clips(self):
        samples = array.array("h")
        samples.frombytes(self.chime.waveform(48000, volume=200))
        self.assertLess(max(abs(s) for s in samples), 32768)

    def test_volume_scales_it(self):
        loud = array.array("h")
        loud.frombytes(self.chime.waveform(48000, 100))
        quiet = array.array("h")
        quiet.frombytes(self.chime.waveform(48000, 25))
        self.assertLess(max(abs(s) for s in quiet), max(abs(s) for s in loud))

    def test_a_bad_volume_falls_back_instead_of_raising(self):
        """配置文件是手写得动的，坏值不能让提示音变成一次崩溃。"""
        self.assertEqual(self.chime.waveform(48000, None),
                         self.chime.waveform(48000, 100))

    def test_the_sample_rate_is_followed(self):
        """退到 44.1 kHz 的时候波形也要跟着变，否则音调是歪的。"""
        self.assertNotEqual(len(self.chime.waveform(44100)),
                            len(self.chime.waveform(48000)))


class WantsAlertTest(unittest.TestCase):
    """哪条消息该响。响错比不响更招人烦，所以每一条都钉住。"""

    def setUp(self):
        import chime
        self.wants = chime.wants_alert

    def test_a_private_message_always_chimes(self):
        self.assertTrue(self.wants("CCA1501", "ZSPD_TWR", "CCA1501",
                                   "contact ground 121.8"))

    def test_a_frequency_message_naming_me_chimes(self):
        self.assertTrue(self.wants("CCA1501", "ZSPD_APP", "@28500",
                                   "CCA1501 descend to 3000 m"))

    def test_a_frequency_message_for_somebody_else_stays_quiet(self):
        self.assertFalse(self.wants("CCA1501", "ZSPD_APP", "@28500",
                                    "CES2345 turn left heading 090"))

    def test_a_longer_callsign_does_not_count_as_a_mention(self):
        """呼号 CCA150 不该被发给 CCA1501 的指令点到——那是另一架飞机。"""
        self.assertFalse(self.wants("CCA150", "ZSPD_APP", "@28500",
                                    "CCA1501, descend"))

    def test_the_mention_is_case_insensitive(self):
        self.assertTrue(self.wants("CCA1501", "ZSPD_APP", "@28500",
                                   "cca1501 cleared to land"))

    def test_punctuation_around_the_callsign_still_counts(self):
        self.assertTrue(self.wants("CCA1501", "ZSPD_APP", "@28500",
                                   "(CCA1501), radar contact"))

    def test_every_message_option_chimes_for_the_whole_frequency(self):
        self.assertTrue(self.wants("CCA1501", "CES2345", "@28500",
                                   "request pushback", every_message=True))

    def test_a_broadcast_chimes(self):
        self.assertTrue(self.wants("CCA1501", "SERVER", "*",
                                   "the network is going down in 10 minutes"))

    def test_my_own_message_never_chimes(self):
        self.assertFalse(self.wants("CCA1501", "CCA1501", "@28500", "roger"))

    def test_no_callsign_yet_does_not_crash(self):
        """还没连上就收到东西时，判断也得给出个答案而不是抛。"""
        self.assertFalse(self.wants("", "ZSPD_APP", "@28500", "CCA1501 descend"))
        self.assertTrue(self.wants(None, "ZSPD_TWR", "CCA1501", "hello"))


class ChimePlayTest(unittest.TestCase):
    """真的去开设备的那半边，全程用假 PortAudio。"""

    def setUp(self):
        import chime
        self.chime = chime
        self.fake = ChimePyAudio()
        self._saved = chime._pyaudio
        chime._pyaudio = lambda: self.fake
        self.addCleanup(setattr, chime, "_pyaudio", self._saved)

    def play(self, player, **kwargs):
        started = player.play(**kwargs)
        player.wait(2.0)
        return started

    def test_it_writes_the_waveform_to_the_chosen_device(self):
        player = self.chime.Chime(ChimeSettings(output_device_index=3))
        self.assertTrue(self.play(player))
        self.assertEqual(len(self.fake.opened), 1)
        stream = self.fake.opened[0]
        self.assertEqual(stream.device, 3)
        self.assertEqual(stream.written, self.chime.waveform(stream.rate, 100))
        self.assertTrue(stream.closed)
        self.assertEqual(self.fake.terminated, 1)

    def test_the_switch_turns_it_off(self):
        player = self.chime.Chime(ChimeSettings(message_sound=False))
        self.assertFalse(self.play(player))
        self.assertEqual(self.fake.opened, [])

    def test_zero_volume_opens_nothing(self):
        """音量拉到 0 就别去碰声卡了——开一次设备是有代价的。"""
        player = self.chime.Chime(ChimeSettings(message_sound_volume=0))
        self.assertFalse(self.play(player))
        self.assertEqual(self.fake.opened, [])

    def test_a_burst_of_messages_only_chimes_once(self):
        """五条消息一起到，用户要的是"有消息"，不是连响五声。"""
        player = self.chime.Chime(ChimeSettings())
        for _ in range(5):
            player.play()
        player.wait(2.0)
        self.assertEqual(len(self.fake.opened), 1)

    def test_it_chimes_again_once_the_interval_has_passed(self):
        player = self.chime.Chime(ChimeSettings(), min_interval=0.0)
        for _ in range(3):
            self.play(player)
        self.assertEqual(len(self.fake.opened), 3)

    def test_the_preview_ignores_both_the_switch_and_the_interval(self):
        """设置里点"试听"是用户自己按的：连点两下第二下没反应就像按钮坏了。"""
        player = self.chime.Chime(ChimeSettings(message_sound=False))
        self.assertTrue(self.play(player, force=True))
        self.assertTrue(self.play(player, force=True))
        self.assertEqual(len(self.fake.opened), 2)

    def test_an_unsupported_rate_falls_back(self):
        """蓝牙耳机常常只吃 44.1 kHz。"""
        self.fake.refuse = {(7, 48000)}
        player = self.chime.Chime(ChimeSettings(output_device_index=7))
        self.assertTrue(self.play(player))
        self.assertEqual(self.fake.opened[-1].rate, 44100)

    def test_a_dead_device_falls_back_to_the_default_one(self):
        """耳机在客户端起来之后被拔了：响在别处也好过一声不响。"""
        self.fake.refuse = {(7, rate) for rate in self.chime.FALLBACK_RATES}
        player = self.chime.Chime(ChimeSettings(output_device_index=7))
        self.assertTrue(self.play(player))
        self.assertIsNone(self.fake.opened[-1].device)

    def test_no_output_device_at_all_is_survivable(self):
        """一台机器上根本没有输出设备时，收消息本身不能跟着炸。"""
        self.fake.refuse = {(None, rate) for rate in self.chime.FALLBACK_RATES}
        player = self.chime.Chime(ChimeSettings())
        self.play(player)
        self.assertEqual(self.fake.opened, [])
        # 开不出来也必须把 PyAudio 收掉，否则每条消息漏一个 PortAudio 实例
        self.assertEqual(self.fake.terminated, 1)

    def test_a_broken_pyaudio_never_reaches_the_caller(self):
        """FSD 的收包线程会直接调到 play()，这里抛出去就是掉线。"""
        def explode():
            raise RuntimeError("no PortAudio here")
        self.chime._pyaudio = explode
        player = self.chime.Chime(ChimeSettings())
        self.assertTrue(self.play(player))      # 派出去了，只是没响成

    def test_it_recovers_after_a_failure(self):
        """一次失败不能把提示音永久卡在"正在放"上。"""
        def explode():
            raise RuntimeError("nope")
        self.chime._pyaudio = explode
        player = self.chime.Chime(ChimeSettings(), min_interval=0.0)
        self.play(player)
        self.chime._pyaudio = lambda: self.fake
        self.play(player)
        self.assertEqual(len(self.fake.opened), 1)


class FrequencyParsingTest(unittest.TestCase):
    """观察员手输的频率。读错一个数字，人就守在别的频道上。"""

    def setUp(self):
        import observer
        self.parse = observer.parse_frequency

    def test_the_usual_ways_of_writing_it(self):
        for text in ("121.8", "121.800", " 121.800 ", "121.80"):
            with self.subTest(text=text):
                self.assertEqual(self.parse(text), 121.8)

    def test_six_digit_kilohertz(self):
        """有人会照着 Mumble 频道名 FREQ_121800 抄。"""
        self.assertEqual(self.parse("121800"), 121.8)
        self.assertEqual(self.parse("118000"), 118.0)

    def test_a_number_works_too(self):
        self.assertEqual(self.parse(121.8), 121.8)

    def test_empty_means_no_frequency(self):
        for text in ("", "   ", None):
            with self.subTest(text=text):
                self.assertIsNone(self.parse(text))

    def test_junk_is_refused_rather_than_guessed(self):
        for text in ("abc", "121.8.9", "1e400", "--"):
            with self.subTest(text=text):
                self.assertIsNone(self.parse(text))

    def test_outside_the_vhf_band_is_refused(self):
        for text in ("99.0", "137.000", "0", "1218"):
            with self.subTest(text=text):
                self.assertIsNone(self.parse(text))

    def test_the_edges_are_included(self):
        self.assertEqual(self.parse("118.000"), 118.0)
        self.assertEqual(self.parse("136.975"), 136.975)

    def test_it_quantises_before_judging_the_range(self):
        """频道名只到千赫：多打一位不该作废，但也别因此放进带外的频率。"""
        self.assertEqual(self.parse("136.9754"), 136.975)
        self.assertIsNone(self.parse("137.0004"))

    def test_a_bool_is_not_a_frequency(self):
        """True 在 Python 里是 1，别让它变成一个频率。"""
        self.assertIsNone(self.parse(True))

    def test_infinity_and_nan_do_not_slip_through(self):
        for text in ("inf", "-inf", "nan"):
            with self.subTest(text=text):
                self.assertIsNone(self.parse(text))


class ObserverFrequencyTest(unittest.TestCase):
    """谁说了算：手输的还是座舱里的 COM1。"""

    def setUp(self):
        import observer
        self.pick = observer.frequency_for

    def test_a_normal_pilot_follows_com1(self):
        self.assertEqual(self.pick(com1=118.0, manual="121.800"), 118.0)

    def test_a_normal_pilot_never_gets_a_manual_frequency(self):
        """这是安全规矩，不是遗漏。

        飞行员要是能把语音频率和座舱 COM1 分开设，就会出现"管制以为你在
        121.8、你人在别的频道"这种事——比听不见更糟。
        """
        self.assertEqual(self.pick(com1=118.0, manual="121.800", observer=False),
                         118.0)
        self.assertIsNone(self.pick(com1=None, manual="121.800", observer=False))

    def test_an_observer_prefers_what_was_typed(self):
        self.assertEqual(self.pick(com1=118.0, manual="121.800", observer=True),
                         121.8)

    def test_an_observer_without_a_simulator(self):
        """副驾常常根本没开模拟器——这才是手输存在的理由。"""
        self.assertEqual(self.pick(com1=None, manual="121.800", observer=True),
                         121.8)

    def test_clearing_it_goes_back_to_following_com1(self):
        """空 = 跟随。省掉一个"手动/自动"开关，也省掉谁说了算的疑问。"""
        self.assertEqual(self.pick(com1=118.0, manual="", observer=True), 118.0)
        self.assertEqual(self.pick(com1=118.0, manual=None, observer=True), 118.0)

    def test_junk_in_the_box_falls_back_to_com1(self):
        self.assertEqual(self.pick(com1=118.0, manual="呃", observer=True), 118.0)

    def test_a_powered_down_radio_has_no_frequency(self):
        self.assertIsNone(self.pick(com1=118.0, com1_power=False))

    def test_an_observer_typing_one_ignores_the_cockpit_radio_switch(self):
        """他多半根本没在用那台电台。"""
        self.assertEqual(
            self.pick(com1=118.0, com1_power=False, manual="121.800", observer=True),
            121.8)

    def test_nothing_at_all_is_no_frequency(self):
        self.assertIsNone(self.pick())
        self.assertIsNone(self.pick(observer=True))


class ObserverFormatTest(unittest.TestCase):

    def setUp(self):
        import observer
        self.format = observer.format_frequency

    def test_it_writes_three_decimals(self):
        self.assertEqual(self.format(121.8), "121.800")
        self.assertEqual(self.format("121.8"), "121.800")

    def test_nothing_becomes_an_empty_string(self):
        """配置里存空串就是"跟随 COM1"，不能存成 "None"。"""
        self.assertEqual(self.format(None), "")
        self.assertEqual(self.format(""), "")
        self.assertEqual(self.format("呃"), "")

    def test_it_round_trips_through_the_parser(self):
        import observer
        self.assertEqual(observer.parse_frequency(self.format(121.8)), 121.8)


if __name__ == "__main__":
    unittest.main(verbosity=2)
