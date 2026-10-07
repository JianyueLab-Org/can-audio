"""协议和数据换算的单元测试。

    python -m unittest test_xpc -v

不连服务器、不碰音频、不需要 X-Plane。重点是两头对得上的地方：PBH 的编码
必须能被 can-fsd 原样解回来，RREF 回包必须按 X-Plane 的格式解析。
"""

import array
import inspect
import json
import math
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

import numpy as np

# pymumble 要本机的 opus 原生库，这些测试碰不到音频，缺库时放个替身。
try:
    import opuslib  # noqa: F401
except Exception:
    for _name in ("opuslib", "opuslib.api", "opuslib.api.decoder",
                  "opuslib.api.encoder", "opuslib.api.info", "opuslib.exceptions"):
        sys.modules.setdefault(_name, mock.MagicMock())

import altitude
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

    def test_clearing_the_simulator_sample_stops_position_packets(self):
        self.pilot._send_position()
        self.pilot.update_position(None)
        self.pilot._send_position()
        self.assertEqual(len(self.sent), 1)


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

    def test_capabilities_advertise_aircraft_config(self):
        """xPilot/vPilot 只向 CAPS 里报了 ACCONFIG=1 的客户端要 ACC。"""
        sent = []
        self.pilot._send = sent.append
        self.pilot._handle_packet("$CQCES2345:CCA1501:CAPS")
        self.assertIn(":ACCONFIG=1", sent[0])

    def test_config_request_carries_the_standard_body(self):
        sent = []
        self.pilot._send = sent.append
        self.pilot.request_config("CES2345")
        self.assertEqual(sent, ['$CQCCA1501:CES2345:ACC:{"request":"full"}'])

    def test_standard_config_request_is_answered_in_kind(self):
        """{"request":"full"} 的回答走 $CQ，配置包在 "config" 里（protocol.md 的 ACC）。"""
        sent = []
        self.pilot.update_position({
            "gear_down": False, "flaps": 0.25, "spoilers": False,
            "lights": {"landing_on": True, "taxi_on": False},
            "engines_on": True, "on_ground": False,
        })
        self.pilot._send = sent.append
        self.pilot._handle_packet('$CQCES2345:CCA1501:ACC:{"request":"full"}')
        self.assertTrue(sent[0].startswith("$CQCCA1501:CES2345:ACC:"))
        config = json.loads(sent[0].split(":ACC:", 1)[1])["config"]
        self.assertTrue(config["is_full_data"])
        self.assertTrue(config["lights"]["landing_on"])
        self.assertEqual(config["flaps_pct"], 25)
        self.assertFalse(config["gear_down"])

    def test_standard_config_reply_reaches_the_traffic_table(self):
        """xPilot 的回答是 $CQ…:ACC:{"config":{...}}，以前被当成请求丢掉了。"""
        table = traffic_module.TrafficTable()
        table.update_position("CES2345", latitude=31, longitude=121,
                              altitude=1000, pitch=0, bank=0, heading=0)
        self.pilot.traffic = table
        payload = json.dumps({"config": {"is_full_data": True, "gear_down": True,
                                         "flaps_pct": 40,
                                         "lights": {"landing_on": True}}})
        self.pilot._handle_packet(f"$CQCES2345:CCA1501:ACC:{payload}")
        aircraft = table.get("CES2345")
        self.assertTrue(aircraft.lights["landing_on"])
        self.assertAlmostEqual(aircraft.flaps, 0.4)

    def test_broadcast_config_update_reaches_the_traffic_table(self):
        """开灯是广播给 @94836 的增量，只带变了的键。"""
        table = traffic_module.TrafficTable()
        table.update_position("CES2345", latitude=31, longitude=121,
                              altitude=1000, pitch=0, bank=0, heading=0)
        self.pilot.traffic = table
        table.set_config("CES2345", {"lights": {"taxi_on": True}})
        self.pilot._handle_packet(
            '$CQCES2345:@94836:ACC:{"config":{"lights":{"landing_on":true}}}')
        aircraft = table.get("CES2345")
        self.assertTrue(aircraft.lights["landing_on"])
        self.assertTrue(aircraft.lights["taxi_on"])

    def test_broadcast_request_is_not_answered(self):
        sent = []
        self.pilot.update_position({"lights": {}})
        self.pilot._send = sent.append
        self.pilot._handle_packet('$CQCES2345:@94836:ACC:{"request":"full"}')
        self.assertEqual(sent, [])

    def test_own_config_is_broadcast_on_change_only(self):
        """第一次全量，之后只发变了的键，没变不发。"""
        sent = []
        self.pilot._send = sent.append
        state = {"gear_down": True, "flaps": 0.0, "on_ground": True,
                 "lights": {"landing_on": False, "taxi_on": True}}
        self.pilot.update_position(dict(state))
        self.pilot._broadcast_config()
        first = json.loads(sent[-1].split(":ACC:", 1)[1])["config"]
        self.assertTrue(sent[-1].startswith("$CQCCA1501:@94836:ACC:"))
        self.assertTrue(first["is_full_data"])

        self.pilot._broadcast_config()
        self.assertEqual(len(sent), 1)

        state["lights"] = {"landing_on": True, "taxi_on": True}
        self.pilot.update_position(dict(state))
        self.pilot._broadcast_config()
        update = json.loads(sent[-1].split(":ACC:", 1)[1])["config"]
        self.assertFalse(update["is_full_data"])
        self.assertTrue(update["lights"]["landing_on"])
        self.assertNotIn("gear_down", update)

    def test_no_broadcast_before_the_simulator_reports(self):
        sent = []
        self.pilot._send = sent.append
        self.pilot._broadcast_config()
        self.assertEqual(sent, [])

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


class HalvingDenoiser:
    def __init__(self, rate):
        self.rate = rate
        self.active = True

    def process(self, samples):
        return (np.asarray(samples, dtype=np.int16) // 2).astype(np.int16)

    def close(self):
        self.active = False


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

    def _mic(self, value):
        self.voice._denoiser_factory = HalvingDenoiser
        return self.voice._process_mic(np.full(960, value, np.int16).tobytes())

    def test_mic_baseline_and_multiplier_multiply(self):
        self.voice.settings.mic_denoise = False
        self.voice.settings.mic_baseline_db = 6.0206     # ×2
        self.voice.settings.mic_volume = 150             # ×1.5
        out = self._mic(1000)
        self.assertTrue(np.all(np.abs(out.astype(np.int32) - 3000) <= 1))

    def test_mic_denoise_runs_before_the_gain(self):
        self.voice.settings.mic_denoise = True
        self.voice.settings.mic_baseline_db = 6.0206
        out = self._mic(1000)
        self.assertTrue(np.all(np.abs(out.astype(np.int32) - 1000) <= 1))

    def test_loud_mic_does_not_wrap_around(self):
        """原来这里没有 clip：滑块过 100% 时大声说话会回绕成负数，听起来是爆音。"""
        self.voice.settings.mic_denoise = False
        self.voice.settings.mic_volume = 200
        out = self._mic(30000)
        self.assertTrue(np.all(out > 0))

    def test_missing_settings_mean_unity_and_denoise_on(self):
        out = self._mic(1000)
        self.assertTrue(np.all(out == 500), "缺省应开降噪（替身减半）且增益为 1")

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


class VoiceStartCancellationTest(unittest.TestCase):
    """A stop racing with the initial Mumble handshake must cancel startup."""

    def setUp(self):
        for name in ("pyaudio", "pymumble_py3", "pymumble_py3.constants",
                     "pymumble_py3.errors"):
            sys.modules.setdefault(name, mock.MagicMock())
        import voice
        self.voice = voice
        self.connected = voice.PYMUMBLE_CONN_STATE_CONNECTED
        voice.pymumble.errors.ConnectionRejectedError = type(
            "ConnectionRejectedError", (Exception,), {})
        self.ready_entered = threading.Event()
        self.allow_ready = threading.Event()
        self.stopped = threading.Event()
        self.current = None
        outer = self

        class FakeMumble:
            connected = 0

            def __init__(self, *args, **kwargs):
                self.callbacks = types.SimpleNamespace(
                    set_callback=lambda *a: None)
                self.parent_thread = threading.current_thread()

            def set_receive_sound(self, value):
                pass

            def run(self):
                self.connected = outer.connected
                outer.current._on_connected()
                outer.allow_ready.wait()

            def is_ready(self):
                outer.ready_entered.set()
                outer.allow_ready.wait()

            def stop(self):
                outer.stopped.set()
                outer.allow_ready.set()

        self._real_mumble = voice.pymumble.Mumble
        voice.pymumble.Mumble = FakeMumble

    def tearDown(self):
        self.allow_ready.set()
        self.voice.pymumble.Mumble = self._real_mumble

    def test_stop_during_handshake_does_not_start_worker_threads(self):
        v = self.voice.Voice(
            "host", "1000", "pw",
            settings=types.SimpleNamespace(mic_volume=100, speaker_volume=100))
        v._open_audio = lambda: (
            setattr(v, "_audio", types.SimpleNamespace(terminate=lambda: None)),
            setattr(v, "_input", FakeStream()),
            setattr(v, "_output", FakeStream()))
        self.current = v
        v._run = mock.Mock()
        v._channel_loop = mock.Mock()

        starter = threading.Thread(target=v.start, daemon=True)
        starter.start()
        self.assertTrue(self.ready_entered.wait(3), "start() never reached handshake")
        v.stop()
        self.allow_ready.set()
        starter.join(timeout=3)

        self.assertFalse(starter.is_alive())
        self.assertTrue(self.stopped.is_set())
        v._run.assert_not_called()
        v._channel_loop.assert_not_called()


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
    """重连要熬过服务端的"幽灵连接"，认证失败则立刻停。和 msfs 同一条策略。

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

    def test_a_drop_while_retrying_is_not_reported_as_terminal(self):
        """重连期间 _loop / _connect 报的 error 必须翻成 reconnecting。

        界面收到 error 会把整条连接当没了——而我们其实马上就要再试。
        """
        client = self.make_client([True, fsdpilot.FAILURE_TRANSIENT, True]
                                  + [fsdpilot.FAILURE_TRANSIENT] * 100)

        def loop_that_drops():
            client._status('error', "与 FSD 服务器的连接已断开")
        client._loop = loop_that_drops
        self.run_client(client)
        kinds = [state for state, _ in client.states]
        self.assertNotIn('error', kinds, kinds)
        self.assertIn('reconnecting', kinds)
        self.assertEqual(kinds[-1], 'offline')


class UpdateCheckTest(unittest.TestCase):
    """查有没有新版。查到了也只是告诉用户，更不更新是他的事。

    走的是 can 而不是 GitHub：大陆连 github.com 很不稳，60 MB 的包经常
    下到一半就断，而 ceruleanavi.net 是成员本来就连得上的。

    **这里的回包照抄 can-api 真发的那个**（`internal/api/clients.go`），不是
    一个方便测试的形状。以前这里造的是 can-web 时代的 `update_available` +
    `client` 对象，两条都和服务端对不上：`update_available` 服务端从来没发
    过（真的字段是嵌套的 `update.available`），而顶层 `client` 是**包名
    字符串**，那一包的信息在 `clients[包名]` 里。造错了的后果是这一整类测试
    全绿而四个客户端的查更新全是死的，所以 `payload()` 是这一类里最要紧的
    几行。
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

    def payload(self, version="2.0.2", available=True, current="2.0.1"):
        """can-api 对 `?client=xpc-for-can&version=…` 的真实回包。"""
        return {
            "version": version,
            "publishedAt": "2026-08-07T12:00:00Z",
            "notes": "https://example/releases/tag/v" + version,
            "clients": {
                name: {
                    "name": name,
                    "version": version,
                    "size": 59057038,
                    "download": "https://ceruleanavi.net/api/v1/clients/download/"
                                + name + "?v=" + version,
                    "origin": "https://github.com/example/releases/download/v"
                              + version + "/" + name + ".zip",
                }
                for name in ("audio-for-can", "atis-for-can",
                             "msfs-for-can", "xpc-for-can")
            },
            # 带了 ?client= 才有这两项，而且 client 是名字，不是那一包
            "client": "xpc-for-can",
            "update": {
                "available": available,
                "current": current,
                "latest": version,
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

    def test_update_links_must_be_absolute_https_urls(self):
        for value in (
            "https://ceruleanavi.net/api/v1/clients/download/xpc-for-can",
            "HTTPS://github.com/JianyueLab-Org/can-audio/releases",
        ):
            self.assertTrue(self.update.is_safe_url(value), value)
        for value in (
            "",
            "http://ceruleanavi.net/update.zip",
            "javascript:alert(1)",
            "file:///Users/me/update.zip",
            "//ceruleanavi.net/update.zip",
            "https://user:password@ceruleanavi.net/update.zip",
        ):
            self.assertFalse(self.update.is_safe_url(value), value)

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
        """服务端只回了总版本、没回单个包，也不该崩。

        `clients` 里那一包会缺，是因为那次构建挂了——服务端宁可不提它，也
        不肯广播一个 404 的下载地址。这时还是要报"有新版"，只是没有下载
        地址，界面会去开说明页。
        """
        payload = {"version": "2.0.2", "notes": "n"}
        with self.answer(payload):
            found = self.update.check("xpc-for-can", "2.0.1")
        self.assertIsNotNone(found)
        self.assertEqual(found.version, "2.0.2")
        self.assertEqual(found.download, "")     # 没有下载地址，界面会去开说明页

    def test_the_verdict_is_nested_under_update_not_a_top_level_flag(self):
        """**有没有新版在 `update.available`。**

        以前这里读的是顶层 `update_available`，而 can-api 从来没发过那个
        键——`data.get()` 拿到 None，于是每次都当作"已经是最新"，四个客户端
        的查更新一起是死的，而且安静得像本来就没有新版。造一个只有嵌套
        `update` 的回包（也就是服务端真发的那个）就能钉住。
        """
        payload = self.payload("2.0.2")
        self.assertNotIn("update_available", payload,
                         "can-api 没有这个字段，测试里也不许有")
        self.assertTrue(payload["update"]["available"])
        with self.answer(payload):
            found = self.update.check("xpc-for-can", "2.0.1")
        self.assertIsNotNone(found, "嵌套的 update.available 没被读到")
        self.assertEqual(found.version, "2.0.2")

    def test_the_top_level_client_field_is_a_name_not_an_object(self):
        """**顶层 `client` 是包名字符串**，那一包在 `clients[包名]` 里。

        当成 dict 去 `.get("version")` 抛的是 `AttributeError`，而调用方是
        `gui.py` 里一个裸的 worker 线程，异常在那儿就没了：界面上和"没有
        新版"分辨不出来。
        """
        payload = self.payload("2.0.2")
        self.assertIsInstance(payload["client"], str)
        with self.answer(payload):
            found = self.update.check("xpc-for-can", "2.0.1")
        self.assertIsNotNone(found)
        self.assertEqual(found.size, 59057038)
        self.assertIn("clients/download/xpc-for-can", found.download)

    def test_each_client_gets_its_own_build(self):
        """四个包在同一个回包里，别拿错别人的。"""
        payload = self.payload("2.0.2")
        for name in ("audio-for-can", "atis-for-can", "msfs-for-can",
                     "xpc-for-can"):
            with self.answer(payload):
                found = self.update.check(name, "2.0.1")
            self.assertIsNotNone(found, name)
            self.assertIn("clients/download/" + name + "?", found.download)

    def test_a_broken_payload_shape_is_not_an_exception(self):
        """解析也得包在 try 里：worker 线程没人接异常。"""
        for payload in ({"update": "yes", "version": "2.0.2"},
                        {"clients": "nope", "version": "2.0.2",
                         "update": {"available": True, "latest": "2.0.2"}},
                        {"clients": {"xpc-for-can": "nope"}, "version": "2.0.2"},
                        {"update": {"available": True, "latest": None}},
                        []):
            with self.answer(payload):
                try:
                    self.update.check("xpc-for-can", "2.0.1")
                except Exception as e:        # noqa: BLE001 —— 就是要证明它不抛
                    self.fail(f"{payload!r} 让查更新抛了 {e!r}")


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
        """一个高度 dataref 都没推时不修正。"""
        snapshot = self.link.snapshot()
        self.assertEqual(snapshot["network_altitude"], 35000)
        self.assertEqual(snapshot["pressure_altitude"], 35000)
        self.assertEqual(snapshot["pressure_delta"], 0)

    def test_xplane_12_uses_its_own_datarefs(self):
        """FL380、标准海压、比 ISA 暖：真高 39700，位置包报 38000。"""
        self.link.values.update({
            "elevation": 39700 * xplane.METRES_PER_FOOT,
            "pressure_altitude": 38000.0, "temperature_error": -1700.0,
            "sea_level_baro": 29.92,
        })
        snapshot = self.link.snapshot()
        self.assertEqual(snapshot["altitude"], 39700)     # 真高照旧
        self.assertEqual(snapshot["network_altitude"], 38000)
        self.assertEqual(snapshot["pressure_altitude"], 38000)
        self.assertEqual(snapshot["temperature_error"], -1700)
        self.assertEqual(snapshot["pressure_delta"], 0)

    def test_xplane_12_on_a_high_pressure_day(self):
        self.link.values.update({
            "elevation": 38334 * xplane.METRES_PER_FOOT,
            "pressure_altitude": 38000.0, "temperature_error": 0.0,
            "sea_level_baro": 1030.0 / 33.8639,
        })
        snapshot = self.link.snapshot()
        self.assertEqual(snapshot["network_altitude"], 38334)
        self.assertEqual(snapshot["pressure_delta"], -334)

    def test_xplane_11_derives_pressure_altitude_from_sea_level_pressure(self):
        """X-Plane 11 没有 pressure_altitude 和 temperature_error。"""
        true_altitude = 38334.4
        self.link.values.update({
            "elevation": true_altitude * xplane.METRES_PER_FOOT,
            "sea_level_baro": 1030.0 / 33.8639,
        })
        snapshot = self.link.snapshot()
        self.assertEqual(snapshot["network_altitude"], 38334)
        self.assertEqual(snapshot["temperature_error"], 0)
        self.assertEqual(snapshot["pressure_altitude"], 38000)
        self.assertEqual(snapshot["pressure_delta"], -334)

    def test_nothing_reads_the_cockpit_altimeter(self):
        """机模自己的气压旋钮不驱动默认高度表，读它就会带进拨错的气压。"""
        for dataref in xplane.DATAREFS.values():
            self.assertNotIn("altitude_ft_pilot", dataref)
            self.assertNotIn("barometer_setting", dataref)

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

    def test_snapshot_expires_after_simulator_stops(self):
        self.link.last_update = time.time() - xplane.STALE_AFTER - 0.1
        self.assertIsNone(self.link.snapshot())

    def test_lights_and_surfaces_are_reported(self):
        """以前一个灯都没订，ACC 回的 lights 永远是空的，别人看我们全程关灯。"""
        self.link.values.update({
            "light_landing": 1.0, "light_taxi": 0.0, "light_beacon": 1.0,
            "light_strobe": 1.0, "light_nav": 1.0,
            "gear": 0.0, "flaps": 0.5, "speedbrake": 0.6, "engine_on": 1.0,
        })
        snapshot = self.link.snapshot()
        self.assertTrue(snapshot["lights"]["landing_on"])
        self.assertFalse(snapshot["lights"]["taxi_on"])
        self.assertFalse(snapshot["gear_down"])
        self.assertAlmostEqual(snapshot["flaps"], 0.5)
        self.assertTrue(snapshot["spoilers"])
        self.assertTrue(snapshot["engines_on"])

    def test_stale_data_is_not_connected(self):
        self.link._connected = True
        self.link.last_update = 0        # 很久以前
        self.assertFalse(self.link.connected)

    def test_velocity_is_east_up_north(self):
        """OpenGL 局部坐标的 +z 朝南，向北是 -local_vz（xPilot 也这么发）。"""
        self.link.values.update({"local_vx": 10.0, "local_vy": 2.0, "local_vz": -50.0})
        snapshot = self.link.snapshot()
        self.assertEqual((snapshot["velocity_east"], snapshot["velocity_up"],
                          snapshot["velocity_north"]), (10.0, 2.0, 50.0))

    def test_rotation_rates_are_q_r_p(self):
        self.link.values.update({"pitch_rate": 1.5, "heading_rate": -3.0,
                                 "bank_rate": 4.0, "nose_wheel": 12.0})
        snapshot = self.link.snapshot()
        self.assertEqual((snapshot["pitch_rate"], snapshot["heading_rate"],
                          snapshot["bank_rate"]), (1.5, -3.0, 4.0))
        self.assertEqual(snapshot["nose_wheel"], 12.0)

    def test_velocity_defaults_to_zero(self):
        snapshot = self.link.snapshot()
        for name in ("velocity_east", "velocity_up", "velocity_north",
                     "pitch_rate", "heading_rate", "bank_rate", "nose_wheel"):
            self.assertEqual(snapshot[name], 0.0, name)

    def test_the_rate_datarefs_are_degrees_per_second(self):
        """P/Q/R 是度每秒；Prad/Qrad/Rrad 才是弧度。快照要度。"""
        self.assertEqual(xplane.DATAREFS["pitch_rate"], "sim/flightmodel/position/Q")
        self.assertEqual(xplane.DATAREFS["heading_rate"], "sim/flightmodel/position/R")
        self.assertEqual(xplane.DATAREFS["bank_rate"], "sim/flightmodel/position/P")


class AltitudeModelTest(unittest.TestCase):
    """altitude.py：ISA 换算、X-Plane 11/12、他机修正。msfs 有一份逐字节相同的拷贝。"""

    def test_standard_surface_height(self):
        self.assertAlmostEqual(altitude.standard_surface_height(1013.25), 0.0)
        self.assertAlmostEqual(altitude.standard_surface_height(1030.0), 453, delta=1)
        self.assertAlmostEqual(altitude.standard_surface_height(990.0), -644, delta=1)

    def test_isa_and_pressure_altitude_are_inverse(self):
        for sea_level in (960.0, 1013.25, 1040.0):
            for pressure in (-1000.0, 0.0, 10000.0, 38000.0, 45000.0):
                true_altitude = altitude.isa_altitude(pressure, sea_level)
                self.assertAlmostEqual(
                    altitude.pressure_altitude(true_altitude, sea_level), pressure,
                    places=6)

    def test_standard_pressure_changes_nothing(self):
        self.assertAlmostEqual(altitude.pressure_altitude(38000.0, 1013.25), 38000.0)
        self.assertAlmostEqual(altitude.isa_altitude(38000.0, 1013.25), 38000.0)

    def test_the_surface_height_is_the_altitude_of_pressure_altitude_zero(self):
        self.assertAlmostEqual(altitude.isa_altitude(0.0, 1030.0),
                               altitude.standard_surface_height(1030.0), places=6)

    def test_xplane_12(self):
        network, pressure, error = altitude.xplane_altitudes(
            39700.0, 1013.25, 38000.0, -1700.0)
        self.assertEqual((network, pressure, error), (38000.0, 38000.0, -1700.0))

    def test_xplane_12_without_the_temperature_error(self):
        network, pressure, error = altitude.xplane_altitudes(38334.0, 1030.0, 38000.0)
        self.assertEqual((network, pressure, error), (38334.0, 38000.0, 0.0))

    def test_xplane_11(self):
        true_altitude = altitude.isa_altitude(38000.0, 1030.0)
        network, pressure, error = altitude.xplane_altitudes(true_altitude, 1030.0)
        self.assertAlmostEqual(network, true_altitude)
        self.assertAlmostEqual(pressure, 38000.0, places=6)
        self.assertEqual(error, 0.0)

    def test_zeros_for_missing_xplane_12_datarefs_fall_back_to_xplane_11(self):
        network, pressure, error = altitude.xplane_altitudes(38000.0, 1013.25, 0.0, 0.0)
        self.assertEqual((network, error), (38000.0, 0.0))
        self.assertAlmostEqual(pressure, 38000.0)

    def test_xplane_11_without_sea_level_pressure(self):
        self.assertEqual(altitude.xplane_altitudes(35000.0, None),
                         (35000.0, 35000.0, 0.0))
        self.assertEqual(altitude.xplane_altitudes(35000.0, 0.0),
                         (35000.0, 35000.0, 0.0))

    def test_xpilot_linear_fallback_is_off_by_a_hundred_and_sixty_feet(self):
        """xPilot 的 X-Plane 11 路径按 30 ft/hPa 线性换算，FL380、1030 hPa 差 168 ft。"""
        true_altitude = altitude.isa_altitude(38000.0, 1030.0)
        linear = true_altitude + (1013.25 - 1030.0) * 30.0
        self.assertTrue(100 <= 38000 - linear <= 200, 38000 - linear)
        _, pressure, _ = altitude.xplane_altitudes(true_altitude, 1030.0)
        self.assertAlmostEqual(pressure, 38000.0, places=6)

    def test_msfs(self):
        network, pressure, error = altitude.msfs_altitudes(39700.0, 1013.25, 38000.0)
        self.assertAlmostEqual(network, 38000.0)
        self.assertEqual(pressure, 38000.0)
        self.assertAlmostEqual(error, -1700.0)

    def test_msfs_rejects_a_pressure_altitude_in_metres(self):
        self.assertEqual(altitude.msfs_altitudes(38000.0, 1013.25, 11582.4),
                         (38000.0, 38000.0, 0.0))

    def test_adjust_weights(self):
        own, error = 38000.0, -1700.0
        cases = {0: 1.0, 3000: 1.0, 4500: 0.5, 6000: 0.0, 7000: 0.0}
        for distance, weight in cases.items():
            for sign in (1, -1):
                remote = own + sign * distance
                self.assertAlmostEqual(
                    altitude.adjust_incoming_altitude(remote, own, error),
                    remote - error * weight, msg=(distance, sign))

    def test_no_own_snapshot_means_no_adjustment(self):
        self.assertEqual(altitude.adjust_incoming_altitude(38000.0, None, -1700.0),
                         38000.0)

    def test_no_temperature_error_means_no_adjustment(self):
        self.assertEqual(altitude.adjust_incoming_altitude(38000.0, 38000.0, 0),
                         38000.0)

    def test_the_plugin_does_not_need_its_own_copy(self):
        """插件拿到的 target 已经是修正过的高度：修正在 fsdpilot 里，进 TrafficTable 之前。"""
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "plugin", "PI_XpcTraffic.py")
        with open(path, encoding="utf-8") as f:
            self.assertNotIn("temperature_error", f.read())


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
        position = self.table.get("CES2345").state_at(time.perf_counter())
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


class DistanceTest(unittest.TestCase):
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


class ObjDataRefRewriteTest(unittest.TestCase):
    """Bluebell 的灯和舵面用 cjs/world_traffic/*，要换成插件驱动的 libxplanemp/*。"""

    BLUEBELL = ("I\n800\nOBJ\n\nTEXTURE b738.png\n"
                "ANIM_show 1 1 cjs/world_traffic/taxi_lights_on\n"
                "LIGHT_NAMED airplane_taxi 0 0 0\n"
                "ANIM_show 1 1 cjs/wolrd_traffic/landing_lights_on\n"
                "ANIM_rotate 1 0 0 0 90 0 1 cjs/world_traffic/main_gear_retraction_ratio\n")

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        cslmatch._drawn_paths.clear()
        self.addCleanup(cslmatch._drawn_paths.clear)

    def _obj(self, name, text):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        return path

    def test_world_traffic_datarefs_are_rewritten_into_a_copy(self):
        original = self._obj("b738.obj", self.BLUEBELL)
        drawn = cslmatch.obj_for_drawing(original)
        self.assertEqual(drawn, os.path.join(self.tmp, "b738" + cslmatch.REWRITTEN_SUFFIX))
        with open(drawn, encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn("world_traffic", text)
        self.assertNotIn("wolrd_traffic", text)
        self.assertIn("libxplanemp/controls/taxi_lites_on", text)
        self.assertIn("libxplanemp/controls/landing_lites_on", text)
        self.assertIn("libxplanemp/controls/gear_ratio", text)
        self.assertEqual(text.splitlines()[3], cslmatch.REWRITE_MARK)
        with open(original, encoding="utf-8") as f:
            self.assertEqual(f.read(), self.BLUEBELL, "原件不能动")

    def test_a_model_already_on_libxplanemp_is_used_as_is(self):
        original = self._obj("a320.obj", "I\n800\nOBJ\n\n"
                             "ANIM_show 1 1 libxplanemp/controls/taxi_lites_on\n")
        self.assertEqual(cslmatch.obj_for_drawing(original), original)
        self.assertEqual(os.listdir(self.tmp), ["a320.obj"])

    def test_every_target_is_registered_by_the_plugin(self):
        """换成插件没注册的 dataref 和不换一样，灯照样不亮。"""
        import importlib.util
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "plugin", "PI_XpcTraffic.py")
        spec = importlib.util.spec_from_file_location("pi_xpc_rewrite", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        registered = set(module.ANIMATION_DATAREFS)
        for _, new in cslmatch.OBJ_DATAREF_REPLACEMENTS:
            self.assertIn(new, registered)

    def test_an_unwritable_directory_falls_back_to_the_original(self):
        original = self._obj("b738.obj", self.BLUEBELL)
        with mock.patch("builtins.open", side_effect=[open(original, encoding="utf-8",
                                                           newline=""),
                                                      PermissionError("read-only")]):
            self.assertEqual(cslmatch.obj_for_drawing(original), original)


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


class StoredPasswordTest(unittest.TestCase):
    """密码不再默认落盘。

    这一格里的 password 不是"这个客户端的密码"——它就是成员的**网站密码**：
    can-api 的 `VerifyNetworkCredential` 对两个列都认，注册和改密写进去的是
    同一个秘密。所以配置文件泄露一次，泄露的是整个账号。

    而它躺的地方偏偏最容易被端走：写在**当前工作目录**，也就是用户双击 exe
    的地方——X-Plane 的 Community 文件夹、会被云同步的游戏目录、报障时打包发过来的那个 zip。

    迁移的形状照着 OLD_MUMBLE_HOSTS 那个来：认得出老样子，就地改掉，并且
    说出来。这里最要紧的一条是**不能把人悄悄锁在外面**，所以老密码这一次
    还在内存里，连接照常。
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
# FSD 连接和他机：发送失败、登录被拒、快速位置包、重复样本、时间戳。
# 和 msfs/test_msfs.py 里同名的那几组一样；fsdpilot.py 两边各一份，traffic.py
# 是共享文件。
# ---------------------------------------------------------------------------

import logging
import re
import socket


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

    def test_clearing_the_simulator_sample_stops_fast_packets(self):
        self.pilot._send_fast_position(slow=False)
        self.pilot.update_position(None)
        self.assertIsNone(self.pilot._send_fast_position(slow=False))
        self.assertEqual(len(self.sent), 1)

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


def _load_plugin(name="pi_xpc_motion"):
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "plugin", "PI_XpcTraffic.py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class MotionAgreementTest(unittest.TestCase):
    """插件里的 Motion 是 traffic.Motion 抄过去的一份，同样的输入必须同样的结果。"""

    FIELDS = ("latitude", "longitude", "altitude", "orientation", "target",
              "velocity", "rotation", "error_velocity", "error_rotation",
              "error_remaining", "since_update")

    def setUp(self):
        self.plugin = _load_plugin()

    def run_both(self, script):
        """script 是 [("receive", args, kwargs) | ("refresh", ...) | ("advance", dt)]。"""
        ours, theirs = traffic_module.Motion(), self.plugin.Motion()
        for step in script:
            for motion in (ours, theirs):
                if step[0] == "advance":
                    motion.advance(step[1])
                else:
                    getattr(motion, step[0])(*step[1], **step[2])
            for name in self.FIELDS:
                self.assertEqual(getattr(ours, name), getattr(theirs, name),
                                 f"{name} 在 {step} 之后不一致")
        return ours.state(), theirs.state()

    def test_error_blend(self):
        client, plugin = self.run_both([
            ("receive", (30.0, 120.0, 10000.0, 0.0, 0.0, 90.0),
             {"velocity": (100.0, 2.0, 50.0), "rotation": (0.0, 1.0, 0.0)}),
            ("advance", 0.2),
            ("receive", (30.0003, 120.0021, 10010.0, 2.0, 5.0, 91.0),
             {"velocity": (101.0, 2.0, 49.0), "rotation": (0.5, 1.0, 2.0)}),
            *[("advance", 1 / 60) for _ in range(150)],
        ])
        self.assertEqual(client, plugin)

    def test_rotation_hold_and_a_late_sample(self):
        client, plugin = self.run_both([
            ("receive", (30.0, 120.0, 10000.0, 0.0, 0.0, 90.0),
             {"rotation": (0.0, 3.0, 0.0)}),
            ("advance", 0.7),
            ("receive", (30.0, 120.0, 10000.0, 0.0, 10.0, 100.0),
             {"rotation": (0.0, 3.0, 0.0)}),
            ("advance", 0.1),
            ("refresh", ((0.0, 0.0, 10.0), (0.0, 2.0, 0.0)), {}),
            ("advance", 1.0),
        ])
        self.assertEqual(client, plugin)

    def test_heading_wrap(self):
        client, plugin = self.run_both([
            ("receive", (30.0, 120.0, 10000.0, 0.0, 0.0, 359.0),
             {"rotation": (0.0, 5.0, 0.0)}),
            ("advance", 0.2),
            ("receive", (30.0, 120.0, 10000.0, 0.0, 0.0, 1.0),
             {"rotation": (0.0, 5.0, 0.0)}),
            ("advance", 0.3),
        ])
        self.assertEqual(client, plugin)
        self.assertLess(plugin["heading"], 5.0)

    def test_frame_rate_independence(self):
        """插件每帧 advance，客户端按推送节奏 advance：结果一样。"""
        def run(module, frames):
            motion = module.Motion()
            motion.receive(30.0, 120.0, 10000.0, 0.0, 0.0, 90.0,
                           velocity=(80.0, 3.0, 60.0), rotation=(1.0, 3.0, -2.0))
            motion.advance(0.3)
            motion.receive(30.0002, 120.001, 10020.0, 1.0, 5.0, 92.0,
                           velocity=(82.0, 3.0, 58.0), rotation=(1.0, 3.0, -2.0))
            for _ in range(frames):
                motion.advance(3.0 / frames)
            return motion.state()
        client, plugin = run(traffic_module, 30), run(self.plugin, 180)
        for key in client:
            self.assertAlmostEqual(client[key], plugin[key], places=6, msg=key)

    def test_the_constants_agree(self):
        for name in ("ERROR_TIME", "ROTATION_HOLD", "METRES_PER_DEGREE",
                     "FEET_PER_METRE", "METRES_PER_FOOT"):
            self.assertEqual(getattr(traffic_module, name),
                             getattr(self.plugin, name), name)


class SampleFeedTest(unittest.TestCase):
    """客户端给插件的是样本和序号；插件按序号 receive / refresh。"""

    def setUp(self):
        self.table = bridge.SampleTable()
        self.plugin = _load_plugin("pi_xpc_feed")

    def fast(self, at, lat=30.0, velocity=(0.0, 0.0, 100.0), rotation=(0.0, 2.0, 0.0),
             callsign="DAL1"):
        self.table.update_position(callsign, latitude=lat, longitude=120.0,
                                   altitude=10000.0, pitch=0.0, bank=0.0,
                                   heading=90.0, velocity=velocity,
                                   rotation=rotation, agl=500.0, now=at)

    def entry(self, at):
        return self.table.snapshot(now=at)[0]

    def test_entries_carry_the_sample(self):
        self.fast(100.0)
        entry = self.entry(100.1)
        self.assertEqual(entry["target"], [30.0, 120.0, 10000.0, 0.0, 0.0, 90.0])
        self.assertEqual(entry["velocity"], [0.0, 0.0, 100.0])
        self.assertEqual(entry["rotation"], [0.0, 2.0, 0.0])
        self.assertEqual((entry["seq"], entry["received"]), (1, 1))
        sent = bridge.plugin_entry(entry)
        for key in bridge.DRAWN_KEYS:
            self.assertNotIn(key, sent)

    def test_the_sent_rotation_is_the_received_one_after_the_hold(self):
        """客户端自己的 Motion 0.5 s 后清掉角速度；发出去的不能跟着变成零。"""
        self.fast(100.0)
        entry = self.entry(101.0)
        self.assertEqual(self.table.get("DAL1").motion.rotation, (0.0, 0.0, 0.0))
        self.assertEqual(entry["rotation"], [0.0, 2.0, 0.0])
        self.assertEqual(entry["seq"], 1)

    def test_a_repeated_snapshot_is_a_refresh(self):
        self.fast(100.0)
        self.fast(100.2, velocity=(0.0, 0.0, 110.0))
        entry = self.entry(100.3)
        self.assertEqual((entry["seq"], entry["received"]), (2, 1))
        self.assertEqual(entry["velocity"], [0.0, 0.0, 110.0])

    def test_slow_senders_forward_the_derived_velocity(self):
        for at, lat in ((100.0, 30.0), (100.2, 30.001)):
            self.table.update_position("OLD1", latitude=lat, longitude=120.0,
                                       altitude=10000, pitch=0.0, bank=0.0,
                                       heading=0.0, now=at)
        entry = self.table.snapshot(now=100.3)[0]
        self.assertEqual(entry["received"], 2)
        self.assertAlmostEqual(entry["velocity"][2],
                               0.001 * traffic_module.METRES_PER_DEGREE / 0.2, places=6)

    def test_plane_info_first_still_gets_a_recording_motion(self):
        self.table.set_plane_info("DAL1", equipment="B738")
        self.fast(100.0)
        self.assertIsInstance(self.table.get("DAL1").motion, bridge.RecordingMotion)
        self.assertEqual(self.entry(100.0)["received"], 1)

    def test_the_plugin_reproduces_the_client_motion(self):
        """插件拿到样本的时刻和客户端一样时，画出来的和客户端算的一样。"""
        aircraft = self.plugin.NetworkAircraft("DAL1")
        times = [100.0 + 0.2 * i for i in range(10)]
        now = times[0]
        for index, at in enumerate(times):
            aircraft.advance(at - now)
            now = at
            lat = 30.0 + index * 0.00018
            if index == 5:
                lat = 30.0 + 4 * 0.00018           # 重复快照 → refresh
            self.fast(at, lat=lat, rotation=(0.0, 1.0 + index * 0.1, 0.0))
            aircraft.accept(bridge.plugin_entry(self.entry(at)), at)
        for _ in range(60):
            aircraft.advance(1 / 60)
        now += 1.0
        client = self.table.get("DAL1").state_at(now)
        plugin = aircraft.motion.state()
        for key in plugin:
            self.assertAlmostEqual(client[key], plugin[key], places=9, msg=key)

    def test_a_missed_frame_still_receives_the_newest_target(self):
        aircraft = self.plugin.NetworkAircraft("DAL1")
        self.fast(100.0)
        aircraft.accept(self.entry(100.0), 100.0)
        self.fast(100.2, lat=30.0002)
        self.fast(100.4, lat=30.0004)              # 中间那帧插件没收到
        aircraft.accept(self.entry(100.4), 100.4)
        self.assertEqual(aircraft.motion.target[0], 30.0004)
        self.assertEqual(aircraft.motion.error_remaining,
                         self.plugin.ERROR_TIME)

    def test_the_same_entry_twice_changes_nothing(self):
        aircraft = self.plugin.NetworkAircraft("DAL1")
        self.fast(100.0)
        entry = self.entry(100.0)
        aircraft.accept(entry, 100.0)
        aircraft.advance(0.3)
        since = aircraft.motion.since_update
        aircraft.accept(entry, 100.3)
        self.assertEqual(aircraft.motion.since_update, since,
                         "心跳帧不能被当成新样本")


class TerrainClampTest(unittest.TestCase):
    """xPilot 的贴地：本地地形和对方地形之差，慢慢加上、慢慢撤掉。"""

    def setUp(self):
        self.plugin = _load_plugin("pi_xpc_terrain")
        self.clamp = self.plugin.TerrainClamp()

    def test_no_terrain_means_no_change(self):
        self.assertEqual(self.clamp.step(0.1, 5000.0, None, 5000.0, 4000.0,
                                         False, True), 5000.0)

    def test_airborne_without_usable_data_is_only_kept_above_ground(self):
        self.assertEqual(self.clamp.step(0.1, 3000.0, 100.0, 3000.0, 2900.0,
                                         False, False), 3000.0)
        self.assertEqual(self.clamp.step(0.1, 50.0, 100.0, 50.0, 400.0,
                                         False, False), 100.0)

    def test_on_the_ground_the_first_frame_snaps(self):
        # 对方在 50 ft 的跑道上，我们这边地面 80 ft
        altitude = self.clamp.step(0.02, 50.0, 80.0, 50.0, 0.0, True, True)
        self.assertAlmostEqual(altitude, 80.0)

    def test_on_the_ground_the_offset_blends_in_over_two_seconds(self):
        self.clamp.step(0.02, 50.0, 50.0, 50.0, 0.0, False, True)
        # 接地，但本地地形比对方高 30 ft：两秒里走完，期间不低于地面
        heights = [self.clamp.step(0.1, 50.0, 80.0, 50.0, 0.0, True, False)
                   for _ in range(20)]
        self.assertAlmostEqual(self.clamp.offset, 30.0)
        self.assertTrue(all(h >= 80.0 for h in heights))

    def test_offset_is_removed_over_ten_seconds_after_climb_out(self):
        self.clamp.offset = self.clamp.target = 30.0
        self.clamp.magnitude = 30.0
        for _ in range(50):          # 5 s，离地 500 ft
            self.clamp.step(0.1, 1000.0, 80.0, 1000.0, 500.0, False, False)
        self.assertAlmostEqual(self.clamp.offset, 15.0, places=6)
        for _ in range(60):
            self.clamp.step(0.1, 1000.0, 80.0, 1000.0, 500.0, False, False)
        self.assertEqual(self.clamp.offset, 0.0)

    def test_usable_data_needs_two_seconds_below_100_ft_on_flat_ground(self):
        self.clamp.local = 80.0
        for index in range(12):
            self.clamp.record(100.0 + 0.2 * index, 30.0 + index * 1e-4, 120.0,
                              100.0 - index, 50.0 - index)
        self.assertTrue(self.clamp.usable)
        # 这样就在接地之前开始对齐：对方地形 50，本地 80
        self.clamp.step(0.1, 60.0, 80.0, 60.0, 10.0, False, False)
        self.assertAlmostEqual(self.clamp.target, 30.0)

    def test_steep_terrain_is_not_trusted(self):
        for index in range(12):
            self.clamp.local = 80.0 + index * 20.0
            self.clamp.record(100.0 + 0.2 * index, 30.0 + index * 1e-5, 120.0,
                              100.0, 50.0)
        self.assertFalse(self.clamp.usable)

    def test_draw_does_not_probe_above_the_ceiling(self):
        probed = []
        aircraft = self.plugin.NetworkAircraft("DAL1")
        aircraft.accept({"target": [30.0, 120.0, 30000.0, 0.0, 0.0, 90.0],
                         "seq": 1, "received": 1}, 0.0)
        aircraft.draw(0.02, lambda lat, lon: probed.append(1) or 0.0)
        self.assertEqual(probed, [])


class SurfaceMotionTest(unittest.TestCase):
    """起落架和襟翼按 xPilot 的时长慢慢动，不再一步到位。"""

    def setUp(self):
        self.plugin = _load_plugin("pi_xpc_surfaces")

    def aircraft(self, **entry):
        aircraft = self.plugin.NetworkAircraft("DAL1")
        base = {"target": [30.0, 120.0, 3000.0, 0.0, 0.0, 90.0], "seq": 1,
                "received": 1, "groundspeed": 250}
        base.update(entry)
        aircraft.accept(base, 0.0)
        aircraft.draw(0.02)
        return aircraft, base

    def test_the_first_frame_takes_the_target(self):
        aircraft, _ = self.aircraft(gear_down=True, flaps=0.5)
        self.assertEqual((aircraft.gear, aircraft.flaps), (1.0, 0.5))

    def test_gear_takes_ten_seconds(self):
        aircraft, entry = self.aircraft(gear_down=False)
        entry = dict(entry, gear_down=True)
        aircraft.accept(entry, 0.1)
        for _ in range(50):
            aircraft.draw(0.1)
        self.assertAlmostEqual(aircraft.gear, 0.5, places=6)
        for _ in range(60):
            aircraft.draw(0.1)
        self.assertEqual(aircraft.gear, 1.0)

    def test_flaps_take_five_seconds_and_do_not_overshoot(self):
        aircraft, entry = self.aircraft(flaps=0.0)
        aircraft.accept(dict(entry, flaps=0.3), 0.1)
        for _ in range(20):
            aircraft.draw(0.1)
        self.assertAlmostEqual(aircraft.flaps, 0.3)

    def test_nose_wheel_and_moving_values_reach_the_animation(self):
        aircraft, entry = self.aircraft(nose_wheel=12.5, gear_down=True)
        values = self.plugin.PythonInterface._animation_values(
            entry, aircraft.surfaces())
        names = self.plugin.ANIMATION_DATAREFS
        self.assertEqual(values[names.index("libxplanemp/controls/nws_ratio")], 12.5)
        self.assertEqual(values[names.index("libxplanemp/controls/gear_ratio")], 1.0)


class PluginFrameLoopTest(unittest.TestCase):
    """插件的主循环：每帧积分，消息只带样本，心跳 30 s。用假的 xp 跑。"""

    def setUp(self):
        self.plugin = _load_plugin("pi_xpc_loop")
        self.clock = [0.0]
        self.positions = []
        fake = types.SimpleNamespace(
            getElapsedTime=lambda: self.clock[0],
            worldToLocal=lambda lat, lon, alt: (lon * 1000.0, alt, -lat * 1000.0),
            instanceSetPosition=lambda inst, pos, data: self.positions.append(pos),
            loadObjectAsync=lambda path, cb, refcon: None,
            destroyInstance=lambda inst: None,
            unloadObject=lambda ref: None,
            getSystemPath=lambda: "",
            log=lambda text: None)
        self.plugin.xp = fake
        interface = self.plugin.PythonInterface.__new__(self.plugin.PythonInterface)
        interface.reassembler = self.plugin.Reassembler()
        interface.aircraft = {}
        interface.order = []
        interface.last_message = 0.0
        interface.last_frame = None
        interface.tcas_written = 0
        interface.tcas_available = False
        interface.have_planes = False
        interface.probe = None
        interface.probe_failed = False
        self.inbox = []
        interface._receive_latest = lambda: self.inbox.pop(0) if self.inbox else None
        self.interface = interface

    def message(self, *entries):
        self.inbox.append({"type": "traffic", "aircraft": list(entries)})

    def entry(self, seq=1, received=1, lat=30.0):
        return {"callsign": "DAL1", "object": "a.obj", "seq": seq,
                "received": received,
                "target": [lat, 120.0, 10000.0, 0.0, 0.0, 90.0],
                "velocity": [0.0, 0.0, 100.0], "rotation": [0.0, 0.0, 0.0]}

    def frame(self, dt=1 / 60):
        self.clock[0] += dt
        self.interface._pump()

    def test_aircraft_move_every_frame_between_messages(self):
        self.message(self.entry())
        self.frame()
        self.interface.aircraft["DAL1"].instance = object()
        for _ in range(6):
            self.frame()
        zs = [pos[2] for pos in self.positions]
        self.assertEqual(len(zs), 6)
        self.assertEqual(len(set(zs)), 6, "两次消息之间飞机也要每帧动")

    def test_a_heartbeat_keeps_them_and_silence_clears_them(self):
        self.message(self.entry())
        self.frame()
        for _ in range(25):
            self.message(self.entry())
            self.frame(1.0)
        self.assertIn("DAL1", self.interface.aircraft)
        self.frame(self.plugin.HEARTBEAT_TIMEOUT - 1.0)
        self.assertIn("DAL1", self.interface.aircraft, "30 s 之内不该清场")
        self.frame(2.0)
        self.assertEqual(self.interface.aircraft, {})

    def test_an_aircraft_missing_from_the_message_is_removed(self):
        self.message(self.entry())
        self.frame()
        self.message()
        self.frame()
        self.assertEqual(self.interface.aircraft, {})

    def test_the_sample_arrives_after_the_frame_advance(self):
        """先积分到此刻再收样本，新样本的误差不是拿上一帧的位置算的。"""
        self.message(self.entry())
        self.frame()
        self.frame(0.2)
        self.message(self.entry(seq=2, received=2, lat=30.0))
        self.frame(0.2)
        motion = self.interface.aircraft["DAL1"].motion
        # 画出来的已经往北走了 0.4 s，目标还在 30.0：误差速度向南
        self.assertLess(motion.error_velocity[2], 0.0)
        self.assertAlmostEqual(motion.error_velocity[2], -100.0 * 0.4 / 2.0, places=6)


class VertOffsetTest(unittest.TestCase):
    """CSL 模型的垂直偏移：xsb_aircraft.txt 写了就用，没写从 .obj 算。"""

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory)

    def write(self, name, text):
        path = os.path.join(self.directory, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def test_vert_offset_line(self):
        self.write("xsb_aircraft.txt",
                   "OBJ8_AIRCRAFT A\nOBJ8 SOLID YES a.obj\nICAO A320\nVERT_OFFSET 2.5\n"
                   "OBJ8_AIRCRAFT B\nOBJ8 SOLID YES b.obj\nICAO B738\n"
                   "OFFSET 0 0 3.1\n")
        models = {m.name: m for m in cslmatch.parse_package(self.directory)}
        self.assertEqual(models["A"].vert_offset, 2.5)
        self.assertEqual(models["B"].vert_offset, 3.1)
        self.assertEqual(cslmatch.vert_offset(models["A"]), 2.5)

    def test_computed_from_the_lowest_vertex(self):
        path = self.write("c.obj", "I\n800\nOBJ\n"
                          "VT   1.000000   -2.250000   3.0   0 1 0   0 0\n"
                          "VT   1.000000   4.500000   3.0   0 1 0   0 0\n")
        model = cslmatch.Model("C", path, icao="A320")
        self.assertAlmostEqual(cslmatch.vert_offset(model), 2.25)

    def test_an_unreadable_obj_is_zero(self):
        model = cslmatch.Model("D", os.path.join(self.directory, "none.obj"),
                               icao="A320")
        self.assertEqual(cslmatch.vert_offset(model), 0.0)


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
