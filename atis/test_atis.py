"""通播的测试：METAR 解析、模板渲染、席位模型。

    python -m unittest test_atis -v      （在 atis 目录下运行）

重点在两处容易出错的地方：
- 每个气象要素的 text / voice 两种形态（对应 vATIS 的 :VOX）
- 情报字母的推进和范围限制
"""

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import metar as metar_module
import profile as profile_module
import template as template_module
import voicefix
import weather
from metar import Metar
from profile import Preset, Profile, Station

ZSPD = "ZSPD 251300Z 09004MPS 9999 FEW030 SCT100 25/18 Q1013 NOSIG"


class MetarTest(unittest.TestCase):

    def setUp(self):
        self.metar = Metar(ZSPD)

    def test_station_and_time(self):
        self.assertEqual(self.metar.station, "ZSPD")
        self.assertEqual(self.metar.observation_time.text, "251300Z")
        self.assertEqual(self.metar.observation_time.voice, "time one three zero zero")

    def test_wind_has_both_forms(self):
        # 文字通播照抄电码，语音要念得出来——这就是 :VOX 的意义
        self.assertEqual(self.metar.wind.text, "09004MPS")
        self.assertEqual(self.metar.wind.voice,
                         "wind zero niner zero degrees four meters per second")

    def test_visibility_and_clouds(self):
        self.assertEqual(self.metar.visibility.voice,
                         "visibility one zero kilometers or more")
        self.assertEqual(self.metar.clouds.text, "FEW030 SCT100")
        self.assertEqual(self.metar.clouds.voice, "few three thousand, scattered one zero thousand")

    def test_temperature_and_pressure(self):
        self.assertEqual(self.metar.temperature.voice, "temperature two five")
        self.assertEqual(self.metar.dew_point.voice, "dewpoint one eight")
        self.assertEqual(self.metar.pressure.voice, "QNH one zero one three hectopascals")

    def test_calm_and_variable_wind(self):
        self.assertEqual(Metar("ZBAA 251300Z 00000MPS 9999 25/18 Q1013").wind.voice,
                         "wind calm")
        self.assertEqual(Metar("ZBAA 251300Z VRB02MPS 9999 25/18 Q1013").wind.voice,
                         "wind variable two meters per second")

    def test_gust_and_variation(self):
        wind = Metar("KLAX 251300Z 26015G25KT 220V300 10SM 25/18 A2992").wind
        self.assertIn("gusting two five knots", wind.voice)
        self.assertIn("variable between two two zero and three zero zero", wind.voice)

    def test_negative_temperature(self):
        parsed = Metar("ZYTX 251300Z 09004MPS 9999 M03/M07 A2992")
        self.assertEqual(parsed.temperature.voice, "temperature minus three")
        self.assertIn("altimeter two niner point niner two", parsed.pressure.voice)

    def test_weather_and_rvr(self):
        parsed = Metar("ZSPD 251300Z 09004MPS 3000 -SHRA R35L/1200 BKN010 25/18 Q1013")
        self.assertEqual(parsed.present_weather.voice, "light showers of rain")
        self.assertIn("runway three five", parsed.rvr.voice)
        self.assertEqual(parsed.visibility.voice, "visibility three kilometers")

    def test_cavok_and_cb(self):
        self.assertTrue(Metar("ZSPD 251300Z 09004MPS CAVOK 25/18 Q1013").cavok)
        parsed = Metar("ZSPD 251300Z 09004MPS 9999 BKN020CB 25/18 Q1013")
        self.assertEqual(parsed.clouds.voice, "broken two thousand cumulonimbus")

    def test_trend_is_separated(self):
        # NOSIG 之后的内容不该混进气象要素里
        self.assertEqual(self.metar.trend.text, "NOSIG")
        self.assertNotIn("NOSIG", self.metar.full_wx().text)

    def test_full_wx_order(self):
        full = self.metar.full_wx()
        self.assertEqual(full.text, "09004MPS 9999 FEW030 SCT100 25/18 Q1013")
        self.assertLess(full.voice.index("wind"), full.voice.index("visibility"))
        self.assertLess(full.voice.index("visibility"), full.voice.index("QNH"))

    def test_garbage_does_not_explode(self):
        parsed = Metar("ZSPD 251300Z 09004MPS 9999 XYZZY123 25/18 Q1013")
        self.assertTrue(parsed.is_valid())
        self.assertEqual(parsed.temperature.voice, "temperature two five")

    def test_metar_prefix_and_empty(self):
        self.assertEqual(Metar("METAR " + ZSPD).station, "ZSPD")
        self.assertFalse(Metar("").is_valid())


class PresetClosingAndChineseExtraTest(unittest.TestCase):
    """收尾语和中文附加文本都跟着预设走。

    同一个机场不同构型要交代的事不一样：「并确认能否执行 RNAV 程序」只该出现在
    RNAV 离场可用的那份稿子里。中文那段更是没法从英文模板生成——chinese.py 是
    从 METAR 独立渲染的，语序和英文完全不同。
    """

    def test_preset_closing_replaces_the_default(self):
        pr = Preset(closing="advise you have information [ATIS_LETTER] and RNAV")
        ctx = template_module.build_context(
            Metar("ZBAA 291000Z 30007MPS CAVOK 24/M08 Q1003"),
            "ZBAA", "J", closing=pr.closing or None)
        _, voice = template_module.render("[CLOSING]", ctx)
        # RNAV 在语音稿里会被展开成 R NAV，否则 TTS 当成单词念
        self.assertIn("information Juliett and R NAV", voice)

    def test_empty_closing_falls_back_to_the_builtin(self):
        ctx = template_module.build_context(
            Metar("ZBAA 291000Z 30007MPS CAVOK 24/M08 Q1003"),
            "ZBAA", "J", closing=Preset().closing or None)
        _, voice = template_module.render("[CLOSING]", ctx)
        self.assertIn("advise on initial contact", voice)

    def test_both_new_fields_survive_a_round_trip(self):
        pr = Preset(closing="收尾", chinese_extra="中文附言")
        back = Preset.from_dict(pr.to_dict())
        self.assertEqual(back.closing, "收尾")
        self.assertEqual(back.chinese_extra, "中文附言")

    def test_old_presets_without_them_still_load(self):
        pr = Preset.from_dict({"name": "默认"})
        self.assertEqual(pr.closing, "")
        self.assertEqual(pr.chinese_extra, "")


class SpokenFacilityAndClosingTest(unittest.TestCase):

    METAR = "ZSPD 291200Z 14005MPS CAVOK 30/26 Q1010"

    def context(self, **kw):
        return template_module.build_context(Metar(self.METAR), "ZSPD", "F", **kw)

    def test_closing_letter_is_spoken_too(self):
        """收尾语里的字母也要念通话字母。

        原来收尾语只渲染了文字那一遍，于是同一句通播开头念
        "INFORMATION Foxtrot"、结尾念 "information F"——听上去像两份稿子。
        """
        _, voice = template_module.render("[CLOSING]", self.context())
        self.assertIn("information Foxtrot", voice)
        self.assertNotIn("information F ", voice + " ")

    def test_facility_is_spoken_as_the_airport_name(self):
        """语音念机场全名，文字稿留 ICAO。

        念 "Z S P D" 听着像在拼写，真实通播念的是机场名。
        """
        ctx = self.context(facility_voice="Shanghai Pudong International Airport")
        text, voice = template_module.render("[FACILITY]", ctx)
        self.assertEqual(text.strip(), "ZSPD")
        self.assertEqual(voice.strip(), "Shanghai Pudong International Airport")

    def test_without_a_name_it_falls_back_to_the_code(self):
        text, voice = template_module.render("[FACILITY]", self.context())
        self.assertEqual(text.strip(), "ZSPD")
        self.assertEqual(voice.strip(), "ZSPD")


class PresetChineseRunwayTest(unittest.TestCase):
    """中文稿的跑道跟着预设走。

    切到"北向"时英文稿的 ARR RWY 会变，中文稿要是还念着南向的跑道，同一份
    通播里两种语言互相矛盾——而大陆机场是双语播的，两边都有人听。
    """

    def test_preset_carries_its_own_chinese_runway(self):
        pr = Preset(name="北向", chinese_runway="三六右")
        self.assertEqual(Preset.from_dict(pr.to_dict()).chinese_runway, "三六右")

    def test_old_profiles_without_the_field_still_load(self):
        """老配置里没有这一项，读出来该是空串而不是炸掉。"""
        pr = Preset.from_dict({"name": "默认"})
        self.assertEqual(pr.chinese_runway, "")

    def test_empty_preset_runway_falls_back_to_the_station(self):
        """预设没填就用席位上的那个，不能变成不念跑道。"""
        station = Station("ZSPD", frequency="127.850", chinese_runway="三五左",
                          presets=[Preset(name="默认")])
        pr = station.presets[0]
        self.assertEqual(pr.chinese_runway or station.chinese_runway, "三五左")


class AtisWordingTest(unittest.TestCase):
    """按本网通播稿子定下来的几处念法。"""

    def test_information_letter_is_spoken_as_a_callsign_word(self):
        """通播念的是 INFORMATION ALPHA，不是 INFORMATION A。

        直接把孤零零一个 "A" 交给 SAPI，念出来是"诶"——听着不像通播，而且和
        飞行员回报的 "information alpha" 对不上。文字稿仍然留字母本身。
        """
        m = Metar("ZSPD 291130Z 14005MPS CAVOK 30/25 Q1010")
        ctx = template_module.build_context(m, facility="ZSPD", letter="A")
        text, voice = template_module.render("[FACILITY] INFORMATION [ATIS_LETTER]", ctx)
        self.assertEqual(text.strip(), "ZSPD INFORMATION A")
        self.assertEqual(voice.strip(), "ZSPD INFORMATION Alpha")

    def test_every_letter_has_a_word(self):
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            self.assertNotEqual(metar_module.spell_letter(letter), letter, letter)

    def test_runways_in_a_list_are_all_expanded(self):
        """ARR RWY 16L, 17R —— 列表里第二个之后也要展开。

        原来只有紧跟 RWY 的那一个会展开，17R 原样留给 TTS，念成"十七阿"。
        真实通播的进离场跑道全是这种列表写法，所以这条几乎必然被撞上。
        """
        out = voicefix.expand_free_text("ARR RWY 16L, 17R, DEP RWY 16R, 17L")
        self.assertEqual(
            out,
            "arrival runway one six left, one seven right, "
            "departure runway one six right, one seven left")

    def test_runway_sides_are_words_not_letters(self):
        self.assertIn("left", voicefix.expand_free_text("RWY 16L"))
        self.assertIn("right", voicefix.expand_free_text("RWY 16R"))
        self.assertIn("center", voicefix.expand_free_text("RWY 16C"))

    def test_atc_and_rnav_are_spelled_out(self):
        """不展开的话 TTS 会把它们当成单词念（"atk"、"arnav"）。"""
        self.assertEqual(
            voicefix.expand_free_text("advise ATC when requesting clearance"),
            "advise A T C when requesting clearance")
        self.assertEqual(voicefix.expand_free_text("RNAV departures available"),
                         "R NAV departures available")

    def test_a_runway_written_as_36Left_is_not_expanded(self):
        """跑道必须写成 36L，不能写 36Left。

        展开靠的是"两位数字紧跟 L/R/C 再收尾"这个形状。写成 36Left 的话，L 后面
        还是字母、收不了尾，连"两位整数"那条兜底规则也匹配不上（6 后面是 L，没有
        词边界），于是整串原样交给 TTS。真实通播里写错这一处，念出来就不是
        "three six left"。这条钉着这个坑，免得下次有人照着英文单词去写。
        """
        self.assertEqual(voicefix.expand_free_text("RWY 36Left"),
                         "runway 36Left")
        self.assertEqual(voicefix.expand_free_text("RWY 36L"),
                         "runway three six left")

    def test_wind_says_degrees(self):
        m = Metar("ZSPD 291130Z 14005MPS CAVOK 30/25 Q1010")
        self.assertEqual(m.wind.voice,
                         "wind one four zero degrees five meters per second")

    def test_qnh_says_the_unit(self):
        m = Metar("ZSPD 291130Z 14005MPS CAVOK 30/25 Q1010")
        self.assertEqual(m.pressure.voice, "QNH one zero one zero hectopascals")


class TemplateTest(unittest.TestCase):

    def setUp(self):
        self.metar = Metar(ZSPD)
        self.context = template_module.build_context(
            self.metar, facility="ZSPD", letter="B",
            airport_conditions="跑道 35L 使用中", notams="无")

    def render(self, template):
        return template_module.render(template, self.context)

    def test_text_uses_raw_and_voice_uses_spoken(self):
        text, voice = self.render("[WIND]")
        self.assertEqual(text, "09004MPS")
        self.assertEqual(voice, "wind zero niner zero degrees four meters per second")

    def test_vox_suffix_forces_spoken_form_in_text(self):
        text, _ = self.render("[WIND:VOX]")
        self.assertEqual(text, "wind zero niner zero degrees four meters per second")

    def test_aliases_point_at_the_same_value(self):
        for name in ("[ATIS_LETTER]", "[ATIS_CODE]", "[LETTER]", "[ID]"):
            self.assertEqual(self.render(name)[0], "B", name)
        self.assertEqual(self.render("[VIS]")[0], self.render("[PREVAILING_VISIBILITY]")[0])

    def test_free_text_variables(self):
        self.assertEqual(self.render("[ARPT_COND]")[0], "跑道 35L 使用中")
        self.assertEqual(self.render("[NOTAMS]")[0], "无")

    def test_closing_can_reference_the_letter(self):
        _, voice = self.render("[CLOSING]")
        self.assertIn("information B", voice)

    def test_unknown_variable_is_left_alone(self):
        text, _ = self.render("[NOPE]")
        self.assertEqual(text, "[NOPE]", "认不出的变量要留着，方便发现拼错")
        self.assertEqual(template_module.unknown_variables("[NOPE] [WIND]"), ["NOPE"])

    def test_empty_variables_do_not_leave_debris(self):
        # 没有 RVR 时不该留下多余的空格和标点
        text, _ = self.render("[WIND]. [RVR]. [PRESSURE]")
        self.assertNotIn("  ", text)
        self.assertNotIn("..", text)

    def test_default_template_renders(self):
        text, voice = self.render(template_module.DEFAULT_TEMPLATE)
        self.assertTrue(text.startswith("ZSPD ATIS B"))
        self.assertIn("跑道 35L 使用中", text)
        self.assertIn("wind zero niner zero", voice)
        self.assertIn("information B", voice)


class StationTest(unittest.TestCase):

    def test_callsign_by_type(self):
        self.assertEqual(Station("zspd").callsign, "ZSPD_ATIS")
        self.assertEqual(Station("ZSPD", atis_type=profile_module.TYPE_DEPARTURE).callsign,
                         "ZSPD_D_ATIS")
        self.assertEqual(Station("ZSPD", atis_type=profile_module.TYPE_ARRIVAL).callsign,
                         "ZSPD_A_ATIS")

    def test_channel_matches_the_network_convention(self):
        station = Station("ZSPD", frequency="127.850")
        self.assertEqual(station.frequency_khz, 127850)
        self.assertEqual(station.channel, "FREQ_127850")

    def test_letter_advances_and_wraps(self):
        station = Station("ZSPD")
        self.assertEqual(station.letter, "A")
        self.assertEqual(station.advance_letter(), "B")
        station.set_letter("Z")
        self.assertEqual(station.advance_letter(), "A")

    def test_code_range_limits_the_letters(self):
        station = Station("ZSPD", code_range=("A", "C"))
        self.assertEqual(station.letters_in_range(), ["A", "B", "C"])
        station.letter = "C"
        self.assertEqual(station.advance_letter(), "A")
        self.assertFalse(station.set_letter("M"), "范围外的字母不该被接受")

    def test_code_range_can_wrap_past_z(self):
        station = Station("ZSPD", code_range=("Y", "B"))
        self.assertEqual(station.letters_in_range(), ["Y", "Z", "A", "B"])
        station.letter = "Z"
        self.assertEqual(station.advance_letter(), "A")


class ProfileTest(unittest.TestCase):

    def setUp(self):
        handle, self.path = tempfile.mkstemp(suffix=".json")
        os.close(handle)
        os.remove(self.path)
        self.profile = Profile(self.path)

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_add_and_reject_duplicates(self):
        self.profile.add(Station("ZSPD"))
        with self.assertRaises(ValueError):
            self.profile.add(Station("ZSPD"))
        # 类型不同就是不同席位，可以共存
        self.profile.add(Station("ZSPD", atis_type=profile_module.TYPE_DEPARTURE))
        self.assertEqual(len(self.profile), 2)

    def test_round_trip(self):
        station = Station("ZSPD", "浦东", "127.850", code_range=("A", "M"))
        station.presets = [Preset("白天", "[FACILITY] [ATIS_LETTER]", "跑道 35L")]
        station.advance_letter()
        self.profile.add(station)
        self.profile.save()

        restored = Profile(self.path)
        self.assertEqual(len(restored), 1)
        loaded = restored.get("ZSPD_ATIS")
        self.assertEqual(loaded.name, "浦东")
        self.assertEqual(loaded.frequency, "127.850")
        self.assertEqual(loaded.letter, "B")
        self.assertEqual(loaded.code_range, ("A", "M"))
        self.assertEqual(loaded.presets[0].airport_conditions, "跑道 35L")

    def test_bad_entries_are_skipped(self):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"stations": [{"identifier": "ZSPD"}, {"nope": 1}]}, f)
        restored = Profile(self.path)
        self.assertEqual(len(restored), 1)


VATIS_PROFILE = {
    "name": "測試配置",
    "id": "9d1f5c3a-0000-4000-8000-000000000001",
    "stations": [
        {
            "id": "s1",
            "identifier": "KLAX",
            "name": "Los Angeles Intl",
            "atisType": "Combined",
            "codeRange": {"low": "A", "high": "M"},
            "frequency": 133800000,
            "idsEndpoint": "https://ids.example/api",
            "atisFormat": {"surfaceWind": {"speakLeadingZero": True}},
            "presets": [
                {"id": "p2", "ordinal": 1, "name": "夜间",
                 "template": "[FACILITY] ATIS [ATIS_LETTER]. [WX]. @RWY_CLOSED",
                 "airportConditions": "", "notams": "TWY B CLOSED"},
                {"id": "p1", "ordinal": 0, "name": "白天",
                 "template": "[FACILITY] ATIS [ATIS_LETTER]. [WX]. [ARPT_COND]",
                 "airportConditions": "RWY 25L IN USE", "notams": ""},
            ],
            "contractions": [
                {"variableName": "RWY_CLOSED", "text": "RWY 07L CLSD",
                 "voice": "runway zero seven left closed"},
            ],
        },
        {
            "id": "s2",
            "identifier": "KLAX",
            "name": "Los Angeles Departure",
            "atisType": "Departure",
            "codeRange": {"low": "N", "high": "Z"},
            "frequency": 135650000,
            "presets": [{"name": "默认", "template": "[FACILITY] [ATIS_LETTER]"}],
        },
    ],
}


class VatisImportTest(unittest.TestCase):

    def setUp(self):
        import vatis_import
        self.module = vatis_import
        handle, self.path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(handle, "w", encoding="utf-8") as f:
            json.dump(VATIS_PROFILE, f)

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def write(self, data):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f)

    def test_frequency_is_hertz(self):
        # vATIS 存的是赫兹，133800000 就是 133.800
        self.assertEqual(self.module.parse_frequency(133800000), "133.800")
        self.assertEqual(self.module.parse_frequency(118000), "118.000")   # 千赫
        self.assertEqual(self.module.parse_frequency(127.85), "127.850")   # 兆赫

    def test_frequency_out_of_range_is_rejected(self):
        for bad in (99000000, 250000000, "abc"):
            with self.assertRaises(self.module.ImportError_, msg=str(bad)):
                self.module.parse_frequency(bad)

    def test_imports_stations(self):
        name, stations, _ = self.module.load_profile(self.path)
        self.assertEqual(name, "測試配置")
        self.assertEqual([s.callsign for s in stations],
                         ["KLAX_ATIS", "KLAX_D_ATIS"])
        self.assertEqual(stations[0].frequency, "133.800")
        self.assertEqual(stations[0].code_range, ("A", "M"))
        self.assertEqual(stations[1].code_range, ("N", "Z"))

    def test_presets_keep_order_and_content(self):
        _, stations, _ = self.module.load_profile(self.path)
        presets = stations[0].presets
        self.assertEqual([p.name for p in presets], ["白天", "夜间"],
                         "应当按 ordinal 排序")
        self.assertEqual(presets[0].airport_conditions, "RWY 25L IN USE")
        self.assertEqual(presets[1].notams, "TWY B CLOSED")

    def test_contractions_are_imported_and_expand(self):
        _, stations, _ = self.module.load_profile(self.path)
        station = stations[0]
        self.assertEqual(station.contractions["RWY_CLOSED"][1],
                         "runway zero seven left closed")

        context = template_module.build_context(Metar(ZSPD), "KLAX", "A")
        text, voice = template_module.render(
            station.presets[1].template, context, station.contractions)
        self.assertIn("RWY 07L CLSD", text, "文字形态")
        self.assertIn("runway zero seven left closed", voice, "语音形态")

    def test_unsupported_settings_are_reported(self):
        _, _, notes = self.module.load_profile(self.path)
        joined = " ".join(notes)
        self.assertIn("IDS", joined, "没有对应功能的设置要说出来，别让人以为全导进来了")

    def test_old_profiles_use_composites(self):
        self.write({"name": "旧版", "composites": VATIS_PROFILE["stations"][:1]})
        _, stations, _ = self.module.load_profile(self.path)
        self.assertEqual(stations[0].callsign, "KLAX_ATIS")

    def test_numeric_atis_type(self):
        entry = dict(VATIS_PROFILE["stations"][0], atisType=1)
        self.write({"stations": [entry]})
        _, stations, _ = self.module.load_profile(self.path)
        self.assertEqual(stations[0].atis_type, profile_module.TYPE_DEPARTURE)

    def test_bad_station_is_skipped_not_fatal(self):
        entry = dict(VATIS_PROFILE["stations"][0])
        self.write({"stations": [{"identifier": ""}, entry]})
        _, stations, notes = self.module.load_profile(self.path)
        self.assertEqual(len(stations), 1)
        self.assertTrue(any("无法导入" in n for n in notes))

    def test_helpful_errors(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{ not json")
        with self.assertRaises(self.module.ImportError_):
            self.module.load_profile(self.path)

        self.write({"name": "空的"})
        with self.assertRaises(self.module.ImportError_):
            self.module.load_profile(self.path)


class FsdCallsignTest(unittest.TestCase):
    """呼号规则来自 can-fsd 的 IsValidCallsign / IsATISCallsign。"""

    def setUp(self):
        import fsdclient
        self.module = fsdclient

    def test_combined_callsign_is_accepted(self):
        self.assertIsNone(self.module.callsign_problem("ZSPD_ATIS"))

    def test_split_atis_callsigns_are_accepted(self):
        """ZSPD_D_ATIS / ZSPD_A_ATIS 有 11 个字符。

        can-fsd 的上限本来是 10，正好把这两个卡死，一个机场没法同时开离场和
        进场通播。服务端已经放宽到 12（packet.go 的 MaxCallsignLength），这里
        跟着放开。
        """
        self.assertIsNone(self.module.callsign_problem("ZSPD_D_ATIS"))
        self.assertIsNone(self.module.callsign_problem("ZSPD_A_ATIS"))

    def test_still_rejects_what_the_server_would(self):
        # 13 个字符，比服务端上限多一个
        problem = self.module.callsign_problem("ABCDEFGHIJKLM_ATIS")
        self.assertIsNotNone(problem)

    def test_must_end_with_atis(self):
        self.assertIn("_ATIS", self.module.callsign_problem("ZSPD_TWR"))

    def test_bad_characters(self):
        self.assertIsNotNone(self.module.callsign_problem("ZS PD_ATIS"))


class FsdProtocolTest(unittest.TestCase):
    """对着按 can-fsd 包格式应答的假服务端跑一遍，验的是"能不能和真服务端对上"。"""

    def setUp(self):
        import fsdclient
        self.module = fsdclient

    def make_server(self, reject=False):
        import socket
        import threading

        class FakeServer(threading.Thread):
            def __init__(self):
                super().__init__(daemon=True)
                self.listener = socket.socket()
                self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self.listener.bind(("127.0.0.1", 0))
                self.listener.listen(1)
                self.port = self.listener.getsockname()[1]
                self.received = []
                self.position_seen = threading.Event()
                self.running = True
                self._conn = None

            def send(self, packet):
                if self._conn:
                    self._conn.sendall((packet + "\r\n").encode())

            def run(self):
                try:
                    conn, _ = self.listener.accept()
                except OSError:
                    return
                self._conn = conn
                conn.settimeout(0.5)
                self.send("$DISERVER:CLIENT:VATSIM FSD V3.41b:abc123")
                buffer = b""
                while self.running:
                    try:
                        chunk = conn.recv(4096)
                    except socket.timeout:
                        continue
                    except OSError:
                        break
                    if not chunk:
                        break
                    buffer += chunk
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        packet = line.decode().strip()
                        if packet:
                            self.handle(packet)
                try:
                    conn.close()
                except OSError:
                    pass

            def handle(self, packet):
                self.received.append(packet)
                fields = packet.split(":")
                if packet.startswith("#AA") and reject:
                    self.send("$ERSERVER:unknown:006::Invalid CID/password.")
                    return
                if packet.startswith("$CQ") and len(fields) >= 3 and fields[2] == "CAPS":
                    self.send(f"$CRSERVER:{packet[3:].split(':')[0]}:CAPS:ATCINFO=1")
                    return
                if packet.startswith("$AX") and len(fields) >= 4 and fields[2] == "METAR":
                    callsign = packet[3:].split(":")[0]
                    self.send(f"$ARserver:{callsign}:METAR:{fields[3]} 251300Z "
                              f"09004MPS 9999 25/18 Q1013")
                    return
                if packet.startswith("%"):
                    self.position_seen.set()

            def stop(self):
                self.running = False
                try:
                    self.listener.close()
                except OSError:
                    pass

            def packets(self, prefix):
                return [p for p in self.received if p.startswith(prefix)]

        server = FakeServer()
        server.start()
        self.addCleanup(server.stop)
        return server

    def make_client(self, server, **kwargs):
        client = self.module.FSDClient(
            "127.0.0.1", "ZSPD_ATIS", "1005", "secret", "127.850",
            real_name="Test", port=server.port,
            latitude=31.1434, longitude=121.805,
            atis_lines=["ZSPD ATIS A", "WIND CALM"], **kwargs)
        self.addCleanup(client.stop)
        client.start()
        return client

    def wait(self, predicate, timeout=10):
        import time
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.05)
        return False

    def test_frequency_wire_encoding(self):
        # can-fsd 的 formatFrequency 反过来：包里 "27850" → 显示 127.850
        self.assertEqual(self.module.encode_frequency("127.850"), "27850")
        self.assertEqual(self.module.encode_frequency("118.000"), "18000")

    def test_login_packets(self):
        server = self.make_server()
        client = self.make_client(server)
        self.assertTrue(server.position_seen.wait(10), "没有等到位置包")

        ident = server.packets("$ID")[0].split(":")
        self.assertEqual(ident[0], "$IDZSPD_ATIS")
        self.assertEqual(ident[6], "1005")
        self.assertEqual(len(ident), 8, "第 9 个字段（质询）必须留空，否则服务端会发 $ZC")

        add = server.packets("#AA")[0].split(":")
        self.assertEqual(add[3], "1005")
        self.assertEqual(add[4], "secret")
        self.assertEqual(add[5], str(self.module.RATING_OBSERVER))

    def test_position_layout(self):
        server = self.make_server()
        self.make_client(server)
        self.assertTrue(server.position_seen.wait(10))

        # %callsign:frequency:facility:visRange:rating:lat:lon:0
        position = server.packets("%")[0].split(":")
        self.assertEqual(position[1], "27850")
        self.assertEqual(position[2], str(self.module.FACILITY_ATIS))
        self.assertAlmostEqual(float(position[5]), 31.1434, places=4)
        self.assertEqual(position[7], "0")

    def test_answers_pilot_atis_query(self):
        server = self.make_server()
        self.make_client(server)
        self.assertTrue(server.position_seen.wait(10))

        server.send("$CQCES2345:ZSPD_ATIS:ATIS")
        self.assertTrue(self.wait(lambda: server.packets("$CRZSPD_ATIS:CES2345")))
        replies = server.packets("$CRZSPD_ATIS:CES2345")
        self.assertEqual(replies[0], "$CRZSPD_ATIS:CES2345:ATIS:T:ZSPD ATIS A")
        self.assertEqual(replies[-1], "$CRZSPD_ATIS:CES2345:ATIS:E:2")

    def test_metar_round_trip(self):
        server = self.make_server()
        client = self.make_client(server)
        self.assertTrue(self.wait(lambda: client.connected))

        report = client.request_metar("ZSPD", timeout=10)
        self.assertIsNotNone(report, "没有拿到 $AR 回来的报文")
        self.assertTrue(report.startswith("ZSPD 251300Z"))

    def test_error_after_login_does_not_drop_connection(self):
        server = self.make_server()
        client = self.make_client(server)
        self.assertTrue(server.position_seen.wait(10))

        server.send("$ERserver:ZSPD_ATIS:012::No METAR available for ZZZZ")
        import time
        time.sleep(1)
        self.assertTrue(client.thread.is_alive(), "登录后的 $ER 不该把连接拆掉")

    def test_rejected_login_reports_reason(self):
        server = self.make_server(reject=True)
        states = []
        self.make_client(server, on_status=lambda s, m: states.append((s, m)))
        self.assertTrue(self.wait(lambda: any(s == 'error' for s, _ in states)))
        self.assertIn("Invalid CID/password", [m for s, m in states if s == 'error'][0])

    def test_logoff_packet(self):
        server = self.make_server()
        client = self.make_client(server)
        self.assertTrue(server.position_seen.wait(10))
        client.stop()
        self.assertTrue(self.wait(lambda: server.packets("#DA")))
        self.assertEqual(server.packets("#DA")[0], "#DAZSPD_ATIS:1005")


DATAFEED = {
    "controllers": [
        {"cid": "1000", "callsign": "RJTT_TWR", "rating": 5},
        {"cid": "1007", "callsign": "ZSPD_APP", "rating": 4},
    ],
    "atis": [
        {"cid": "1009", "callsign": "ZBAA_ATIS", "rating": 2},
    ],
}


class RatingLookupTest(unittest.TestCase):
    """通播的等级要跟本人此刻的管制席位一致，写死 OBS 会显示成观察员。"""

    def setUp(self):
        import datafeed
        self.module = datafeed

    def test_finds_the_rating_of_a_controller(self):
        self.assertEqual(self.module.rating_for("1000", data=DATAFEED), 5)

    def test_also_looks_in_atis_stations(self):
        self.assertEqual(self.module.rating_for("1009", data=DATAFEED), 2)

    def test_absent_cid_returns_none(self):
        # 人不在线时查不到，调用方退回配置里的值
        self.assertIsNone(self.module.rating_for("9999", data=DATAFEED))

    def test_blank_cid(self):
        self.assertIsNone(self.module.rating_for("", data=DATAFEED))

    def test_bad_rating_is_ignored(self):
        data = {"controllers": [{"cid": "1000", "rating": "abc"},
                                {"cid": "1000", "callsign": "X_TWR", "rating": 3}]}
        self.assertEqual(self.module.rating_for("1000", data=data), 3)

    def test_empty_datafeed(self):
        self.assertIsNone(self.module.rating_for("1000", data={}))
        self.assertIsNone(self.module.rating_for("1000", data=None, url="http://0.0.0.0:1"))


class ControllerGateTest(unittest.TestCase):
    """没在管制就不该挂通播，否则会留下无人值守的通播席位。"""

    def setUp(self):
        import datafeed
        self.module = datafeed

    def feed(self, **kwargs):
        data = {"controllers": [], "atis": []}
        data.update(kwargs)
        return data

    def test_active_controller_passes(self):
        data = self.feed(controllers=[
            {"cid": "1000", "callsign": "RJTT_TWR", "facility": 4, "rating": 5}])
        entry = self.module.controller_for("1000", data=data)
        self.assertIsNotNone(entry)
        self.assertEqual(entry["callsign"], "RJTT_TWR")

    def test_observer_does_not_count(self):
        # 判定和 can-fsd 的 handleQueryATC 一致：facility 要高于观察员
        data = self.feed(controllers=[
            {"cid": "1000", "callsign": "RJTT_OBS", "facility": 0, "rating": 1}])
        self.assertIsNone(self.module.controller_for("1000", data=data))

    def test_an_existing_atis_does_not_count(self):
        data = self.feed(atis=[
            {"cid": "1000", "callsign": "RJAA_ATIS", "facility": 7, "rating": 5}])
        self.assertIsNone(self.module.controller_for("1000", data=data),
                          "已经开着的通播不能当作在管制")

    def test_someone_else_controlling_does_not_count(self):
        data = self.feed(controllers=[
            {"cid": "1007", "callsign": "RJTT_TWR", "facility": 4, "rating": 5}])
        self.assertIsNone(self.module.controller_for("1000", data=data))

    def test_missing_facility_is_treated_as_observer(self):
        data = self.feed(controllers=[{"cid": "1000", "callsign": "X"}])
        self.assertIsNone(self.module.controller_for("1000", data=data))

    def test_no_data_returns_none(self):
        self.assertIsNone(self.module.controller_for("1000", data={}))


class AutoRatingTest(unittest.TestCase):
    """FSDClient 的 rating=None 表示自动，登录前查一次。"""

    def setUp(self):
        import fsdclient
        self.module = fsdclient

    def test_explicit_rating_is_kept(self):
        client = self.module.FSDClient("h", "ZSPD_ATIS", "1", "p", "118.000", rating=5)
        self.assertEqual(client.rating, 5)

    def test_auto_defers_until_connect(self):
        client = self.module.FSDClient("h", "ZSPD_ATIS", "1", "p", "118.000",
                                       rating=0, rating_lookup=lambda: 5)
        self.assertIsNone(client.rating, "连接之前不该定下来")

    def test_lookup_failure_falls_back_to_observer(self):
        def explode():
            raise RuntimeError("数据源挂了")
        client = self.module.FSDClient("h", "ZSPD_ATIS", "1", "p", "118.000",
                                       rating=None, rating_lookup=explode)
        client.running = True
        client._connect()          # 连不上，但等级应当已经定好
        self.assertEqual(client.rating, self.module.RATING_OBSERVER,
                         "查不到就退回 OBS，不能让播出起不来")


class AirportPositionTest(unittest.TestCase):
    """席位不填坐标就会落在 0/0——几内亚湾外海。"""

    def setUp(self):
        import airports
        self.airports = airports

    def test_known_airports(self):
        self.assertEqual(self.airports.coordinates("RJAA"), (35.76694, 140.38778))
        self.assertEqual(self.airports.coordinates("zspd"), (31.14233, 121.79084))

    def test_unknown_airport(self):
        self.assertIsNone(self.airports.coordinates("XXXX"))
        self.assertIsNone(self.airports.coordinates(""))

    def test_station_fills_its_position_from_the_icao(self):
        station = Station("RJAA", frequency="128.250")
        self.assertAlmostEqual(station.latitude, 35.76694, places=4)
        self.assertAlmostEqual(station.longitude, 140.38778, places=4)

    def test_manual_position_wins(self):
        station = Station("RJAA", frequency="128.250",
                          latitude=35.5, longitude=140.1)
        self.assertEqual(station.latitude, 35.5)

    def test_unknown_airport_leaves_zero(self):
        station = Station("XXXX", frequency="128.250")
        self.assertEqual((station.latitude, station.longitude), (0.0, 0.0))

    def test_position_survives_a_round_trip(self):
        station = Station("RJAA", frequency="128.250")
        restored = Station.from_dict(station.to_dict())
        self.assertAlmostEqual(restored.latitude, 35.76694, places=4)


class SettingsTest(unittest.TestCase):
    """FSD 和语音是两台不同的服务器，配错了会一直连不上。"""

    def setUp(self):
        import settings as settings_module
        self.module = settings_module
        self.previous_cwd = os.getcwd()
        self.temp = tempfile.mkdtemp(prefix="atis_settings_")
        os.chdir(self.temp)
        self.addCleanup(lambda: os.chdir(self.previous_cwd))

    def write(self, data):
        with open("atis_settings.json", "w", encoding="utf-8") as f:
            json.dump(data, f)

    def test_default_fsd_host(self):
        self.assertEqual(self.module.Settings().fsd_host, "fsd.ceruleanavi.net")
        self.assertEqual(self.module.Settings().fsd_port, 6809)

    def test_migrates_the_voice_host_away(self):
        # 早期版本把语音服务器的地址填成了 FSD 地址，那台机器上没有 FSD。
        # 语音服务器换过两次域名，三个都得认：前两个还留在老配置里，最后
        # 一个是同一个人下次还会填错的那个。
        for host in ("hjdczy.top", "audio.airwaysn.org", "audio.ceruleanavi.net"):
            with self.subTest(host=host):
                self.write({"fsd_host": host})
                self.assertEqual(self.module.Settings().fsd_host,
                                 "fsd.ceruleanavi.net")

    def test_migrates_the_dead_domain(self):
        """airwaysn.org 停用后，老配置里存下来的地址要换到新域名。

        这三个字段都是**写回配置文件**的，所以只改模块里的默认值对老用户
        没有任何作用——他们的 json 里还是 airwaysn.org，而那个域已经不解析，
        通播会一边"设置看着正常"一边什么都取不到。
        """
        self.write({
            "fsd_host": "fsd.airwaysn.org",
            "datafeed_url": "https://data.airwaysn.org/v1/data.json",
            "config_url": "https://airwaysn.org/api/v1/atis/config",
        })
        settings = self.module.Settings()
        self.assertEqual(settings.fsd_host, "fsd.ceruleanavi.net")
        self.assertEqual(settings.datafeed_url,
                         "https://data.ceruleanavi.net/v1/data.json")
        self.assertEqual(settings.config_url,
                         "https://ceruleanavi.net/api/v1/atis/config")

    def test_keeps_a_deliberate_url_override(self):
        # 自己指到内网镜像或测试服是有意为之，不能替他改掉
        self.write({"datafeed_url": "http://127.0.0.1:20350/v1/data.json",
                    "config_url": "http://127.0.0.1:4321/api/v1/atis/config"})
        settings = self.module.Settings()
        self.assertEqual(settings.datafeed_url,
                         "http://127.0.0.1:20350/v1/data.json")
        self.assertEqual(settings.config_url,
                         "http://127.0.0.1:4321/api/v1/atis/config")

    def test_keeps_a_deliberate_override(self):
        self.write({"fsd_host": "127.0.0.1", "fsd_port": 16809})
        settings = self.module.Settings()
        self.assertEqual(settings.fsd_host, "127.0.0.1")
        self.assertEqual(settings.fsd_port, 16809)


class LanguageSegmentTest(unittest.TestCase):
    """中英双语稿要按语言分段合成。

    原来是整篇挑一个音色，判据是"文中有没有汉字"——双语稿里那一大段英文于是
    也被中文音色念了，听着像外国人念中文。分段之后各用各的音色、各用各的语速，
    切换处还能插一段静音。
    """

    def setUp(self):
        import sys
        from unittest import mock
        try:
            import opuslib  # noqa: F401
        except Exception:
            for name in ("opuslib", "opuslib.api", "opuslib.api.decoder",
                         "opuslib.api.encoder", "opuslib.api.info",
                         "opuslib.exceptions"):
                sys.modules.setdefault(name, mock.MagicMock())
        global broadcast
        import broadcast

    ENGLISH = ("Beijing Capital International Airport information Juliett, "
               "arrival runway zero one. R NAV departures available.")
    CHINESE = ("北京首都国际机场情报通播 朱丽叶 跑道 洞幺 进港 "
               "本场 RNAV 离场可用 应答机置于 S 模式")

    def test_bilingual_splits_into_english_then_chinese(self):
        segments = broadcast._segments(f"{self.ENGLISH} {self.CHINESE}")
        self.assertEqual([chinese for chinese, _ in segments], [False, True],
                         f"应当正好切成英文、中文两段：{segments}")

    def test_short_english_inside_chinese_does_not_split_it(self):
        """`本场 RNAV 离场可用` 不该在 RNAV 处换两次音色。"""
        segments = broadcast._segments(self.CHINESE)
        self.assertEqual(len(segments), 1, f"中文被切碎了：{segments}")
        self.assertTrue(segments[0][0])
        for word in ("RNAV", "S"):
            self.assertIn(word, segments[0][1])

    def test_single_language_is_one_segment(self):
        self.assertEqual(len(broadcast._segments(self.ENGLISH)), 1)
        self.assertEqual(len(broadcast._segments("")), 0)

    def test_chinese_is_slower_than_english(self):
        self.assertLess(broadcast.RATE_CHINESE, broadcast.RATE_ENGLISH,
                        "中文语速要低一档，通播是要人一遍听懂的")

    def test_there_is_a_gap_at_the_language_switch(self):
        self.assertGreater(broadcast.LANGUAGE_GAP, 0,
                           "不留间隔的话两种语言会连在一起，像同一句说串了")

    # ---------- 一次 runAndWait ----------
    def _fake_engine(self):
        """记下每一次调用，并且真的写出 wav，好让 _read_wav 读得回来。"""
        import wave as wave_module

        class FakeEngine:
            def __init__(self):
                self.calls = []          # ('rate'|'voice'|'save'|'run', 值)

            def setProperty(self, name, value):
                self.calls.append((name, value))

            def getProperty(self, name):
                return []                # 没有音色表，_pick_voice 返回 None

            def save_to_file(self, text, path):
                self.calls.append(("save", text))
                with wave_module.open(path, "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(22050)
                    w.writeframes(b"\x00\x01" * 2205)     # 0.1 秒

            def runAndWait(self):
                self.calls.append(("run", None))

        return FakeEngine()

    def test_the_whole_script_uses_exactly_one_run_and_wait(self):
        """同一个引擎连着进第二次 runAndWait()，pyttsx3 的 SAPI 驱动会永久卡住。

        实测：双语稿（两段）当场挂死，日志停在合成之前——"合成完成"和
        "语音合成失败"两条都不出现，通播一声不响。所以无论几段，整篇只能进
        一次；每段的音色和语速靠 setProperty 排进同一个队列来区分。
        """
        synth = broadcast.Synthesizer()
        engine = self._fake_engine()
        synth._ready = lambda: engine

        pcm = synth._render(f"{self.ENGLISH} {self.CHINESE}")
        self.assertIsNotNone(pcm, "双语稿没合成出来")

        runs = [c for c in engine.calls if c[0] == "run"]
        saves = [c for c in engine.calls if c[0] == "save"]
        self.assertEqual(len(runs), 1,
                         f"runAndWait 调了 {len(runs)} 次，第二次会卡死")
        self.assertEqual(len(saves), 2, "两段应当各写一个 wav")

        # 每段的语速要在它自己的 save_to_file 之前排进队列
        rates = [value for name, value in engine.calls if name == "rate"]
        self.assertEqual(rates, [broadcast.RATE_ENGLISH, broadcast.RATE_CHINESE])

    def test_the_gap_really_lands_in_the_audio(self):
        synth = broadcast.Synthesizer()
        synth._ready = lambda: self._fake_engine()
        pcm = synth._render(f"{self.ENGLISH} {self.CHINESE}")
        seconds = len(pcm) / (2.0 * broadcast.TARGET_RATE)
        # 两段各 0.1 秒 + 间隔 + 0.2 秒收尾
        self.assertAlmostEqual(seconds, 0.1 * 2 + broadcast.LANGUAGE_GAP + 0.2,
                               delta=0.05)


class ResampleTest(unittest.TestCase):
    """语音合成出来的采样率不一定是 48kHz，送进 Mumble 前要转换。"""

    def setUp(self):
        # broadcast 会拉起 pymumble，缺 opus 原生库时用替身放行
        import sys
        from unittest import mock
        try:
            import opuslib  # noqa: F401
        except Exception:
            for name in ("opuslib", "opuslib.api", "opuslib.api.decoder",
                         "opuslib.api.encoder", "opuslib.api.info",
                         "opuslib.exceptions"):
                sys.modules.setdefault(name, mock.MagicMock())
        global broadcast
        import broadcast

    def test_length_scales_with_rate(self):
        import numpy as np
        audio = np.zeros(22050, dtype=np.int16)
        out = broadcast.resample(audio, 22050, 48000)
        self.assertEqual(len(out), 48000, "1 秒的音频重采样后还该是 1 秒")

    def test_same_rate_is_untouched(self):
        import numpy as np
        audio = np.arange(10, dtype=np.int16)
        self.assertIs(broadcast.resample(audio, 48000, 48000), audio)

    def test_signal_shape_is_preserved(self):
        import numpy as np
        source = np.linspace(0, 1000, 1000)
        out = broadcast.resample(source, 8000, 16000)
        self.assertAlmostEqual(out[0], source[0], places=3)
        self.assertAlmostEqual(out[-1], source[-1], places=3)
        self.assertTrue(np.all(np.diff(out) >= 0), "单调上升的信号不该出现回折")

    def test_empty_input(self):
        import numpy as np
        self.assertEqual(len(broadcast.resample(np.zeros(0), 22050, 48000)), 0)


class VoiceFixTest(unittest.TestCase):
    """语音稿收尾。用例全部来自 RJTT 的真实播出输出。"""

    def setUp(self):
        global voicefix
        import voicefix

    # 真实播出时管制员填的自由文本，原样照抄自 vATIS 的 RJTT profile
    FREE = ("ILS ZULU RWY34L APCH AND ILS ZULU RWY34R APCH. LDG RWY 34 LEFT "
            "AND 34 RIGHT. DEP RWY 05 AND 34 RIGHT. DEP FREQ 126.0, "
            "SIMUL PARL ILS APCHS TO RWY34L_R ARE INPR")

    def test_abbreviations_are_expanded(self):
        out = voicefix.expand_free_text(self.FREE)
        for short in ("APCH", "LDG", "RWY", "DEP", "SIMUL", "PARL", "INPR"):
            self.assertNotIn(short, out.upper().split(),
                             f"{short} 没展开，TTS 会逐字母念")

    def test_runway_number_is_spelled(self):
        # RWY34L 念成"三十四"是错的，通播里逐位念
        out = voicefix.expand_free_text("RWY34L")
        self.assertIn("three four left", out)
        self.assertNotIn("34", out)

    def test_underscore_means_and(self):
        # 日本通播里 RWY34L_R 是 34 左和 34 右
        out = voicefix.expand_free_text("RWY34L_R")
        self.assertIn("left and right", out)
        self.assertNotIn("_", out)

    def test_frequency_decimal(self):
        out = voicefix.expand_free_text("DEP FREQ 126.0")
        self.assertIn("one two six decimal zero", out)

    def test_no_stray_symbols_left(self):
        out = voicefix.expand_free_text(self.FREE)
        for symbol in ("_", "/"):
            self.assertNotIn(symbol, out)

    # 真实播出里粘在一起的那段
    GLUED = ("broken niner thousandtemperature two five/dew point two two "
             "QNH one zero one four hectopascals2992")

    def test_glued_words_are_separated(self):
        # 全小写的粘连没有模式能识别，靠已知的段起始词切
        out = voicefix.polish(self.GLUED)
        self.assertNotIn("thousandtemperature", out)
        self.assertIn("thousand, temperature", out)

    def test_glued_number_is_separated_and_spelled(self):
        out = voicefix.polish(self.GLUED)
        self.assertNotIn("hectopascals2992", out)
        self.assertIn("two niner niner two", out)

    def test_slash_is_removed(self):
        self.assertNotIn("/", voicefix.polish(self.GLUED))

    def test_polish_leaves_good_text_alone(self):
        good = "wind zero eight zero at one zero knots, visibility one zero kilometers"
        self.assertEqual(voicefix.polish(good), good)

    def test_join_elements_separates(self):
        self.assertEqual(
            voicefix.join_elements(["broken niner thousand", "temperature two five"]),
            "broken niner thousand, temperature two five")

    def test_join_skips_empties(self):
        self.assertEqual(voicefix.join_elements(["a", "", None, "b"]), "a, b")

    def test_empty_input(self):
        self.assertEqual(voicefix.polish(""), "")
        self.assertEqual(voicefix.expand_free_text(""), "")

    def test_punctuation_spacing(self):
        self.assertEqual(voicefix.polish("a ,b .c"), "a, b. c")


class ProfileSetTest(unittest.TestCase):
    """多份 profile。vATIS 的模型：一份 profile 装一组席位。

    同一个人可能同时管华东和华北，两边的席位、模板、跑道构型完全不同；混在
    一张列表里，值班时要在十几个不相关的席位里找自己那两个。
    """

    def setUp(self):
        self.path = os.path.join(tempfile.mkdtemp(), "atis_profile.json")

    def write(self, data):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

    def read(self):
        with open(self.path, encoding="utf-8") as f:
            return json.load(f)

    # ---------- 向后兼容：这一条最重要 ----------
    def test_an_old_single_profile_file_still_loads(self):
        """现有的 atis_profile.json 是 {"stations": [...]}，没有 profile 这一层。

        读不进来的后果不是"少个功能"，是所有人打开就是空配置。
        """
        self.write({"stations": [Station("ZSPD", frequency="127.850").to_dict()]})
        store = profile_module.ProfileSet(self.path)
        self.assertEqual(len(store), 1)
        self.assertEqual(store.active().name, profile_module.DEFAULT_PROFILE_NAME)
        self.assertIsNotNone(store.active().get("ZSPD_ATIS"))

    def test_saving_upgrades_the_file_shape(self):
        self.write({"stations": []})
        store = profile_module.ProfileSet(self.path)
        store.save()
        data = self.read()
        self.assertIn("profiles", data)
        self.assertEqual(data["active"], profile_module.DEFAULT_PROFILE_NAME)

    def test_a_missing_file_still_gives_one_profile(self):
        """界面不该到处判 None。"""
        store = profile_module.ProfileSet(self.path)
        self.assertEqual(len(store), 1)
        self.assertIsNotNone(store.active())

    def test_single_profile_save_keeps_the_sibling_profiles(self):
        """gui 用单份 Profile 直读直写；多 profile 的文件不能被它压扁。

        load() 认得 {"profiles": [...]} 并只取当前那份，save() 原来却把整个
        文件重写成 {"stations": [...]}——改一次跑道构型，其余 profile 全部
        无声消失。
        """
        self.write({
            "active": "华东",
            "profiles": [
                {"name": "华东",
                 "stations": [Station("ZSPD", frequency="127.850").to_dict()]},
                {"name": "华北",
                 "stations": [Station("ZBAA", frequency="127.600").to_dict()]},
            ]})
        single = profile_module.Profile(path=self.path)
        single.load()
        self.assertEqual(single.name, "华东")
        single.save()
        data = self.read()
        self.assertIn("profiles", data, "文件被压扁回单份形状了")
        names = [p.get("name") for p in data["profiles"]]
        self.assertIn("华北", names, "另一份 profile 被吞了")
        self.assertEqual(data.get("active"), "华东")

    # ---------- 增删改 ----------
    def test_add_select_and_round_trip(self):
        store = profile_module.ProfileSet(self.path)
        store.add("华北")
        store.get("华北").add(Station("ZBAA", frequency="127.000"))
        self.assertTrue(store.select("华北"))
        store.save()

        again = profile_module.ProfileSet(self.path)
        self.assertEqual(again.active_name, "华北")
        self.assertIsNotNone(again.active().get("ZBAA_ATIS"))
        self.assertEqual(sorted(again.names),
                         sorted([profile_module.DEFAULT_PROFILE_NAME, "华北"]))

    def test_duplicate_and_empty_names_are_refused(self):
        store = profile_module.ProfileSet(self.path)
        store.add("华北")
        with self.assertRaises(ValueError):
            store.add("华北")
        with self.assertRaises(ValueError):
            store.add("   ")

    def test_renaming_follows_the_selection(self):
        store = profile_module.ProfileSet(self.path)
        store.add("华北")
        store.select("华北")
        store.rename("华北", "华北区域")
        self.assertEqual(store.active_name, "华北区域")
        self.assertIsNone(store.get("华北"))

    def test_renaming_onto_an_existing_name_is_refused(self):
        store = profile_module.ProfileSet(self.path)
        store.add("华北")
        with self.assertRaises(ValueError):
            store.rename("华北", profile_module.DEFAULT_PROFILE_NAME)

    def test_the_last_profile_cannot_be_removed(self):
        """删光了界面就没有可操作的对象了。"""
        store = profile_module.ProfileSet(self.path)
        self.assertEqual(len(store), 1)
        with self.assertRaises(ValueError):
            store.remove(store.active_name)

    def test_removing_the_active_one_falls_back(self):
        store = profile_module.ProfileSet(self.path)
        store.add("华北")
        store.select("华北")
        self.assertTrue(store.remove("华北"))
        self.assertEqual(store.active_name, profile_module.DEFAULT_PROFILE_NAME)

    def test_an_unknown_active_name_falls_back(self):
        """文件被手改过、active 指向一个不存在的名字。"""
        self.write({"active": "没有这份", "profiles": [{"name": "甲", "stations": []}]})
        store = profile_module.ProfileSet(self.path)
        self.assertEqual(store.active().name, "甲")
        self.assertEqual(store.active_name, "甲")


class NetworkConfigTest(unittest.TestCase):
    """从 can-web 取全网配置（/api/v1/atis/config）并并进本地。

    和下面那个 AtisStationsFromFeedTest 取的东西**不是一回事**：数据源的
    atis[] 是此刻谁在播（只有机场和频率，是运行状态），这个接口给的是配置
    本身——席位、频率、跑道构型预设、模板、中文用词。
    """

    def setUp(self):
        global netconfig
        import netconfig
        self.path = os.path.join(tempfile.mkdtemp(), "atis_profile.json")

    def entry(self, identifier="ZSPD", frequency="127.850", **extra):
        station = Station(identifier, frequency=frequency,
                          voice_language=profile_module.LANGUAGE_BOTH,
                          chinese_name="上海浦东")
        station.presets = [profile_module.Preset(
            "南向", "[FACILITY] information [ATIS_LETTER]",
            chinese_runway="跑道 幺六左 进港。")]
        data = station.to_dict()
        data.pop("letter")              # 服务端不发情报字母，它是运行状态
        data.update(extra)
        return data

    def document(self, *entries, **head):
        doc = {"version": "abc123", "updated": "2026-07-30",
               "notes": "华东五场", "stations": list(entries) or [self.entry()]}
        doc.update(head)
        return doc

    def profile(self, *stations):
        store = Profile(path=self.path)
        for station in stations:
            store.add(station)
        return store

    # ---------- 解析 ----------
    def test_reads_a_whole_station_not_just_the_frequency(self):
        """要点就在这里：网络上以前只能拿到机场和频率，模板得自己打。"""
        config = netconfig.parse(self.document())
        self.assertEqual(config.version, "abc123")
        self.assertEqual(config.updated, "2026-07-30")
        station = config.stations[0]
        self.assertEqual(station.callsign, "ZSPD_ATIS")
        self.assertEqual(station.frequency, "127.850")
        self.assertEqual(station.voice_language, profile_module.LANGUAGE_BOTH)
        self.assertEqual(station.chinese_name, "上海浦东")
        self.assertEqual(station.presets[0].name, "南向")
        self.assertEqual(station.presets[0].chinese_runway, "跑道 幺六左 进港。")

    def test_unknown_fields_do_not_sink_the_document(self):
        """服务端加了新字段，旧客户端只应当忽略它，不是整份读不进来。"""
        config = netconfig.parse(self.document(
            self.entry(**{"some_future_field": {"a": 1}})))
        self.assertEqual(len(config), 1)

    def test_a_bad_station_is_skipped_and_reported(self):
        config = netconfig.parse(self.document(
            {"name": "缺 identifier"},
            self.entry("ZBAA", "127.000"),
            self.entry("ZSHC", "abc")))
        self.assertEqual([s.identifier for s in config.stations], ["ZBAA"])
        self.assertEqual(len(config.problems), 2)

    def test_a_frequency_outside_the_vhf_band_is_refused(self):
        """开播时才炸的话，用户看到的是"频道里没人"，不是"配置有问题"。"""
        with self.assertRaises(netconfig.NetConfigError):
            netconfig.parse(self.document(self.entry("ZSPD", "88.500")))

    def test_an_empty_document_is_an_error_not_an_empty_merge(self):
        for bad in ({}, {"stations": []}, {"stations": "nope"}):
            with self.assertRaises(netconfig.NetConfigError):
                netconfig.parse(bad)

    def test_a_saved_profile_file_can_be_used_as_the_source(self):
        """也认本客户端自己的存盘形状，这样 config_url 可以指向自建的一份。"""
        config = netconfig.parse(
            {"profiles": [{"name": "华东", "stations": [self.entry()]}]})
        self.assertEqual(config.stations[0].callsign, "ZSPD_ATIS")

    # ---------- 取 ----------
    def test_fetch_failures_say_why(self):
        """用户明确按了更新，失败必须给出原因，不能只是"什么都没发生"。"""
        import urllib.error

        cases = [
            (urllib.error.HTTPError("u", 429, "Too Many", None, None), "限流"),
            (urllib.error.HTTPError("u", 500, "Boom", None, None), "500"),
            (urllib.error.URLError("名字解析失败"), "连不上"),
        ]
        for error, fragment in cases:
            with mock.patch("urllib.request.urlopen", side_effect=error):
                with self.assertRaises(netconfig.NetConfigError) as caught:
                    netconfig.fetch("https://example.invalid/config")
            self.assertIn(fragment, str(caught.exception))

    def test_garbage_body_is_reported_as_such(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b"<html>404</html>"
        with mock.patch("urllib.request.urlopen", return_value=response):
            with self.assertRaises(netconfig.NetConfigError) as caught:
                netconfig.fetch("https://example.invalid/config")
        self.assertIn("JSON", str(caught.exception))

    # ---------- 比对 ----------
    def test_compare_splits_missing_differing_and_same(self):
        local_same = Station.from_dict(self.entry("ZSPD"))
        local_differs = Station.from_dict(self.entry("ZBAA", "127.000"))
        local_differs.presets[0].notams = "本地自己加的"
        store = self.profile(local_same, local_differs)

        config = netconfig.parse(self.document(
            self.entry("ZSPD"), self.entry("ZBAA", "127.000"),
            self.entry("ZSHC", "127.250")))
        missing, differing, same = netconfig.compare(store, config.stations)
        self.assertEqual([s.identifier for s in missing], ["ZSHC"])
        self.assertEqual([s.identifier for s in differing], ["ZBAA"])
        self.assertEqual([s.identifier for s in same], ["ZSPD"])

    def test_a_different_information_letter_is_not_a_difference(self):
        """字母每几分钟推进一格。带着它比，每个席位永远都"和网络版不一样"。"""
        local = Station.from_dict(self.entry("ZSPD"))
        local.advance_letter()
        store = self.profile(local)
        config = netconfig.parse(self.document(self.entry("ZSPD")))
        _, differing, same = netconfig.compare(store, config.stations)
        self.assertEqual(differing, [])
        self.assertEqual(len(same), 1)

    # ---------- 合并 ----------
    def test_merge_adds_what_is_missing(self):
        store = self.profile()
        config = netconfig.parse(self.document(self.entry("ZSPD"),
                                               self.entry("ZBAA", "127.000")))
        added, replaced, kept, skipped = netconfig.merge(store, config.stations)
        self.assertEqual(len(added), 2)
        self.assertEqual((replaced, kept, skipped), ([], [], []))
        self.assertIsNotNone(store.get("ZBAA_ATIS"))

    def test_local_edits_survive_by_default(self):
        """值班时改过的构型和 NOTAM 都在预设里，默认一律不能盖。"""
        local = Station.from_dict(self.entry("ZSPD"))
        local.presets[0].notams = "临时：跑道 17L 关闭"
        store = self.profile(local)
        config = netconfig.parse(self.document(self.entry("ZSPD")))

        added, replaced, kept, _ = netconfig.merge(store, config.stations)
        self.assertEqual((added, replaced), ([], []))
        self.assertEqual(len(kept), 1)
        self.assertEqual(store.get("ZSPD_ATIS").presets[0].notams,
                         "临时：跑道 17L 关闭")

    def test_overwrite_replaces_but_keeps_the_letter(self):
        """播了一半把字母退回 A，飞行员报的和听到的就对不上了。"""
        local = Station.from_dict(self.entry("ZSPD"))
        local.presets[0].notams = "本地的"
        local.set_letter("F")
        store = self.profile(local)
        config = netconfig.parse(self.document(self.entry("ZSPD")))

        _, replaced, _, _ = netconfig.merge(store, config.stations,
                                            overwrite=True)
        self.assertEqual(len(replaced), 1)
        again = store.get("ZSPD_ATIS")
        self.assertEqual(again.presets[0].notams, "")
        self.assertEqual(again.letter, "F")

    def test_a_broadcasting_station_is_never_touched(self):
        """播出中的 Station 被 Broadcaster 和 FSDClient 拿着。

        换掉它只会让在播的内容和界面上显示的稿子对不上，而界面看起来一切正常。
        """
        local = Station.from_dict(self.entry("ZSPD"))
        local.presets[0].notams = "正在播的这份"
        store = self.profile(local)
        config = netconfig.parse(self.document(self.entry("ZSPD")))

        added, replaced, _, skipped = netconfig.merge(
            store, config.stations, overwrite=True, protected={"ZSPD_ATIS"})
        self.assertEqual((added, replaced), ([], []))
        self.assertEqual([s.callsign for s in skipped], ["ZSPD_ATIS"])
        self.assertEqual(store.get("ZSPD_ATIS").presets[0].notams, "正在播的这份")

    def test_merge_does_not_save_by_itself(self):
        """存盘归调用方——它那边可能是 ProfileSet，路径也在它手上。"""
        store = self.profile()
        config = netconfig.parse(self.document())
        netconfig.merge(store, config.stations)
        self.assertFalse(os.path.exists(self.path))


class AtisStationsFromFeedTest(unittest.TestCase):
    """从 can-fsd 数据源的 atis[] 里抽席位。

    **这不是取配置。** 配置在 can-web 的 /api/v1/atis/config，由 netconfig.py
    取（见 NetworkConfigTest）；atis[] 是**此刻在线的运行状态**，只能拿到机场
    和频率。/api/v1/atis 是第三样东西：给 EuroScope 用的文本生成器。
    """

    def setUp(self):
        global datafeed
        import datafeed

    def feed(self, *entries):
        return {"general": {}, "pilots": [], "controllers": [],
                "atis": list(entries)}

    def entry(self, callsign, frequency="128.500"):
        return {"cid": "900", "callsign": callsign, "frequency": frequency,
                "rating": 2, "text_atis": ["ATIS A"]}

    def test_reads_icao_frequency_and_type(self):
        data = self.feed(self.entry("ZSPD_ATIS", "127.850"),
                         self.entry("ZBAA_D_ATIS", "126.250"),
                         self.entry("ZGGG_A_ATIS", "121.900"))
        self.assertEqual(
            datafeed.atis_stations(data=data),
            [("ZBAA", "ZBAA_D_ATIS", "126.250", "departure"),
             ("ZGGG", "ZGGG_A_ATIS", "121.900", "arrival"),
             ("ZSPD", "ZSPD_ATIS", "127.850", "combined")])

    def test_the_no_frequency_placeholder_is_dropped(self):
        """199.998 是 can-fsd 的"没设频率"，拿它建席位会得到一个空频道。"""
        data = self.feed(self.entry("ZSPD_ATIS", datafeed.NO_FREQUENCY))
        self.assertEqual(datafeed.atis_stations(data=data), [])

    def test_a_departure_suffix_is_not_mistaken_for_combined(self):
        """_D_ATIS 也以 _ATIS 结尾，长后缀必须先匹配。"""
        data = self.feed(self.entry("ZSPD_D_ATIS"))
        icao, _, _, kind = datafeed.atis_stations(data=data)[0]
        self.assertEqual((icao, kind), ("ZSPD", "departure"))

    def test_non_atis_callsigns_are_ignored(self):
        data = self.feed(self.entry("ZSPD_TWR"), self.entry("ZSPD_ATIS"))
        self.assertEqual([c for _, c, _, _ in datafeed.atis_stations(data=data)],
                         ["ZSPD_ATIS"])

    def test_a_bad_icao_is_ignored(self):
        data = self.feed(self.entry("X_ATIS"), self.entry("ZSPD_ATIS"))
        self.assertEqual([i for i, _, _, _ in datafeed.atis_stations(data=data)],
                         ["ZSPD"])

    def test_empty_and_missing_data(self):
        """拿不到数据时给空列表。

        **data=None 不是"没有数据"，是"你自己去取"** —— atis_stations 会调
        fetch()，也就是真的去连 data.ceruleanavi.net。这条断言原来直接写
        data=None，于是网络上只要有人在播通播，它就拿回真实席位而不是空列表，
        CI 随机红一次（实测抓到的是 ZBSJ_ATIS 127.650）。测的本来就是"取不到
        数据"这条路，把 fetch 换成替身才是它的本意。
        """
        original = datafeed.fetch
        datafeed.fetch = lambda *a, **k: None
        self.addCleanup(setattr, datafeed, "fetch", original)

        self.assertEqual(datafeed.atis_stations(data=None), [])
        self.assertEqual(datafeed.atis_stations(data={}), [])
        self.assertEqual(datafeed.atis_stations(data=self.feed()), [])

    def test_it_never_reaches_the_network_on_its_own(self):
        """给了 data 就不该再去取 —— 否则每个用例都在打真实数据源。

        上面那条踩过一次了，钉住它：给了 data 的调用一次网络都不许发。
        """
        calls = []
        original = datafeed.fetch

        def counting_fetch(*args, **kwargs):
            calls.append(args)
            return None

        datafeed.fetch = counting_fetch
        self.addCleanup(setattr, datafeed, "fetch", original)

        datafeed.atis_stations(data=self.feed(self.entry("ZSPD_ATIS")))
        datafeed.atis_stations(data={})
        self.assertEqual(calls, [])


class BroadcastRulesTest(unittest.TestCase):
    """开播前的拦截规则。

    这几条原来在 gui.py 里各写了两遍（点按钮时一遍、数据源核对回来再一遍），
    四段文案各自独立，只能靠点按钮触发，测不到。

    两遍检查本身是必要的：核对要走网络，中间隔着几秒，这期间完全可能又开了
    一个同频率的席位。所以规则要能共用，但不能因此省掉第二次检查。
    """

    def setUp(self):
        global rules
        import rules
        self.profile = Profile(path=os.path.join(
            tempfile.mkdtemp(), "atis_profile.json"))
        self.pudong = Station("ZSPD", "浦东", "127.850")
        self.profile.add(self.pudong)
        self.rendered = ("ZSPD ATIS A", "Shanghai ATIS Alpha")

    def refuse(self, station=None, broadcasting=(), cid="1000",
               password="pw", rendered=True):
        return rules.blocking_reason(
            station or self.pudong, self.profile, broadcasting, cid, password,
            self.rendered if rendered else None)

    def test_nothing_blocks_a_normal_start(self):
        self.assertIsNone(self.refuse())

    def test_credentials_are_required(self):
        self.assertEqual(self.refuse(cid="")[0], "错误")
        self.assertEqual(self.refuse(password="")[0], "错误")
        self.assertIn("用户名", self.refuse(cid="")[1])

    def test_no_script_is_blocked(self):
        title, message = self.refuse(rendered=False)
        self.assertEqual(title, "错误")
        self.assertIn("刷新天气", message)

    def test_a_blank_voice_script_counts_as_no_script(self):
        """渲染出来但语音是空白的，播出去就是一段静音。"""
        self.rendered = ("有文字", "   ")
        self.assertIsNotNone(self.refuse())

    # ---------- 频率冲突 ----------
    def test_same_frequency_distinct_airports_can_broadcast(self):
        twin = Station("ZSSS", "虹桥", "127.850")     # 同频率，不同机场
        self.profile.add(twin)
        self.assertIsNone(self.refuse(broadcasting={twin.callsign}))

    def test_a_different_frequency_is_fine(self):
        other = Station("ZSSS", "虹桥", "132.250")
        self.profile.add(other)
        self.assertIsNone(self.refuse(broadcasting={other.callsign}))

    def test_duplicate_callsigns_are_still_refused(self):
        with self.assertRaises(ValueError):
            self.profile.add(Station("ZSPD", "Duplicate", "128.800"))

    def test_same_frequency_broadcasters_use_distinct_usernames(self):
        broadcast = _load_broadcast()
        with mock.patch.object(broadcast, "Synthesizer"):
            tokyo = broadcast.Broadcaster("host", "1000", "pw", Station("RJTT", "Tokyo", "128.800"))
            osaka = broadcast.Broadcaster("host", "1000", "pw", Station("RJOO", "Osaka", "128.800"))
        self.assertEqual(tokyo.user, "1000_atis128800_RJTT_ATIS")
        self.assertEqual(osaka.user, "1000_atis128800_RJOO_ATIS")
        self.assertNotEqual(tokyo.user, osaka.user)

    def test_credentials_are_checked_before_the_conflict(self):
        """先报最好改的那一条：没填账号时不该先弹频率冲突。"""
        twin = Station("ZSSS", "虹桥", "127.850")
        self.profile.add(twin)
        self.assertEqual(self.refuse(cid="", broadcasting={twin.callsign})[0],
                         "错误")


class ScriptTest(unittest.TestCase):
    """渲染层。以前这段逻辑埋在 gui.py 里、还抄了两遍（预览一遍、推送一遍），
    只能靠 smoke 间接覆盖；拆成 script.py 之后可以直接测。

    两遍抄写的真正代价不是重复，是**改岔之后界面上看不出来**——预览显示的和
    播出去的成了两份稿子，只有听的人知道。
    """

    def setUp(self):
        global script
        import script
        self.metar = Metar(ZSPD)
        self.preset = Preset(
            airport_conditions="ARR RWY 16L, DEP RWY 16R.",
            chinese_runway="跑道 幺六左 进港，跑道 幺六右 出港。",
            chinese_extra="放行频率 幺两幺点六。")

    def station(self, language=profile_module.LANGUAGE_ENGLISH, **kw):
        return Station("ZSPD", "Shanghai Pudong International Airport",
                       "127.850", voice_language=language,
                       chinese_name="上海浦东国际机场", **kw)

    # ---------- render ----------
    def test_missing_pieces_give_none(self):
        """缺天气就不该推一份缺了半截的稿子出去。"""
        station = self.station()
        self.assertIsNone(script.render(station, self.preset, None))
        self.assertIsNone(script.render(station, None, self.metar))
        self.assertIsNone(script.render(None, self.preset, self.metar))

    def test_text_keeps_the_raw_groups(self):
        text, _ = script.render(self.station(), self.preset, self.metar)
        self.assertIn("09004MPS", text, "文字通播要照抄电码")
        self.assertIn("Q1013", text)

    def test_english_station_speaks_english_only(self):
        _, voice = script.render(self.station(), self.preset, self.metar)
        self.assertIn("wind zero niner zero", voice)
        self.assertNotIn("风", voice, "选了英文就不该混进中文")

    def test_chinese_station_speaks_chinese_only(self):
        station = self.station(profile_module.LANGUAGE_CHINESE)
        _, voice = script.render(station, self.preset, self.metar)
        self.assertIn("上海浦东国际机场", voice)
        self.assertIn("风向", voice)
        self.assertNotIn("wind", voice, "选了中文就不该混进英文")

    def test_bilingual_puts_chinese_after_english(self):
        """中文飞行员听得懂英文的居多，反过来不一定。"""
        station = self.station(profile_module.LANGUAGE_BOTH)
        _, voice = script.render(station, self.preset, self.metar)
        self.assertLess(voice.index("wind zero niner zero"),
                        voice.index("风向"), "中文应当在英文之后")

    def test_preset_runway_wins_over_the_station_one(self):
        """切到别的构型时英文的 ARR RWY 会变，中文不跟着就自相矛盾了。"""
        station = self.station(profile_module.LANGUAGE_CHINESE,
                               chinese_runway="三四右")
        _, voice = script.render(station, self.preset, self.metar)
        self.assertIn("幺六左", voice)
        self.assertNotIn("三四右", voice, "预设里的跑道没有盖过席位上那个")

    def test_station_runway_is_the_fallback(self):
        station = self.station(profile_module.LANGUAGE_CHINESE,
                               chinese_runway="三四右")
        bare = Preset()
        _, voice = script.render(station, bare, self.metar)
        self.assertIn("三四右", voice)

    # ---------- summary ----------
    def test_summary_has_the_four_things(self):
        station = self.station()
        line = script.summary(station, self.metar)
        for piece in ("ZSPD", station.letter, "09004MPS", "Q1013"):
            self.assertIn(piece, line, line)

    def test_summary_without_weather(self):
        line = script.summary(self.station(), None)
        self.assertIn("ZSPD", line)
        self.assertNotIn("Q", line.replace("ZSPD", ""), line)

    def test_summary_marks_the_broadcasting_one(self):
        self.assertTrue(script.summary(self.station(), self.metar,
                                       broadcasting=True).startswith("●"))
        self.assertFalse(script.summary(self.station(), self.metar,
                                        broadcasting=False).startswith("●"))

    def test_summary_distinguishes_departure_and_arrival(self):
        """同一个机场的综合/离场/进场不能长得一模一样。"""
        lines = {
            script.summary(self.station(atis_type=kind), self.metar)
            for kind in (profile_module.TYPE_COMBINED,
                         profile_module.TYPE_DEPARTURE,
                         profile_module.TYPE_ARRIVAL)
        }
        self.assertEqual(len(lines), 3, lines)


class ChineseVoiceTest(unittest.TestCase):
    """中文通播稿。不是英文的逐词翻译，语序和读法都是民航自己的一套。"""

    def setUp(self):
        global chinese
        import chinese

    def test_radio_digits(self):
        # 幺两拐洞是无线电通话规范，不是方言；和 server/ATIS/process.py 一致
        self.assertEqual(chinese.spell("09004"), "洞 九 洞 洞 四")
        self.assertEqual(chinese.spell("7"), "拐")
        self.assertEqual(chinese.spell("21"), "两 幺")

    def test_counting_numbers(self):
        # 温度、米数念整数，不逐位
        self.assertEqual(chinese.spell_count(25), "二十五")
        self.assertEqual(chinese.spell_count(900), "九百")
        self.assertEqual(chinese.spell_count(3000), "三千")
        self.assertEqual(chinese.spell_count(15), "十五")
        self.assertEqual(chinese.spell_count(-3), "零下 三")
        self.assertEqual(chinese.spell_count(0), "零")

    def test_wind(self):
        # 风向和风速各带名头，真实通播就是这么播的——只说"风 洞九洞 度 四
        # 米每秒"听不出哪个数是什么
        self.assertEqual(chinese._wind("09004MPS"),
                         "风向 洞 九 洞 度 风速 四 米每秒")

    def test_wind_variable(self):
        self.assertIn("风向不定", chinese._wind("VRB02MPS"))

    def test_wind_calm(self):
        self.assertEqual(chinese._wind("00000MPS"), "静风")

    def test_wind_gusts(self):
        self.assertIn("阵风", chinese._wind("27010G18MPS"))

    def test_wind_with_a_variation_group_still_reports_the_wind(self):
        """metar._wind() 会把变化组拼在文本形式后面（"09004MPS 350V050"）。

        原来整串去匹配单段的正则，匹配不上就整组静默丢掉——带变化组的 METAR
        播出来的中文通播**完全没有风**，能见度紧跟在时间后面。
        """
        spoken = chinese._wind("09004MPS 350V050")
        self.assertIn("风向 洞 九 洞 度", spoken)
        self.assertIn("风速 四 米每秒", spoken)
        self.assertIn("之间变化", spoken)

    def test_visibility(self):
        self.assertEqual(chinese._visibility("9999"), "能见度 幺洞 公里 以上")
        self.assertEqual(chinese._visibility("3000"), "能见度 三 公里")
        self.assertEqual(chinese._visibility("0800"), "能见度 八百 米")

    def test_cavok(self):
        self.assertIn("云高", chinese._visibility("CAVOK"))

    def test_cloud_height_uses_the_domestic_convention(self):
        """按 100 英尺 = 30 米折算。

        用精确的 30.48 会念出"九百一十米"，真实通播念的是"九百米"。
        """
        self.assertEqual(chinese._clouds("FEW030"), "少云 九百 米")
        self.assertEqual(chinese._clouds("OVC050"), "阴天 一千五百 米")
        self.assertEqual(chinese._clouds("SCT100"), "疏云 三千 米")

    def test_cloud_with_type(self):
        self.assertIn("积雨云", chinese._clouds("BKN020CB"))

    def test_cloud_without_height(self):
        self.assertEqual(chinese._clouds("NSC"), "无重要云")

    def test_weather_intensity(self):
        self.assertEqual(chinese._weather("-RA"), "小雨")
        self.assertEqual(chinese._weather("+TSRA"), "大雷暴雨")
        self.assertIn("附近有", chinese._weather("VCSH"))

    def test_temperature(self):
        # 负号跟在名头后面、单位念出来，都是照真实通播的说法
        self.assertEqual(chinese._temperature("气温", "25"), "气温 二十五 摄氏度")
        self.assertEqual(chinese._temperature("露点", "M03"), "露点负 三 摄氏度")

    def test_pressure_hectopascals(self):
        self.assertEqual(chinese._pressure("Q1013"), "修正海压 幺 洞 幺 三 百帕")

    def test_pressure_inches(self):
        self.assertIn("英寸汞柱", chinese._pressure("A2992"))

    def test_full_script(self):
        parsed = metar_module.Metar(ZSPD)
        script = chinese.render(parsed, facility="上海浦东", letter="D",
                                runway="三六左")
        for fragment in ("上海浦东情报通播", "德尔塔", "风向", "风速", "能见度",
                         "气温", "露点", "修正海压",
                         "首次与管制员联络时报告你已收到通播 德尔塔"):
            self.assertIn(fragment, script)

    def test_the_letter_is_spoken_as_a_chinese_word(self):
        """中文稿里不能出现拉丁字母 J——TTS 会在一串汉字里蹦一个英文字母。"""
        self.assertEqual(chinese.spell_letter("J"), "朱丽叶")
        script = chinese.render(metar_module.Metar(ZSPD),
                                facility="北京首都国际机场", letter="J")
        self.assertIn("朱丽叶", script)
        self.assertNotIn("J", script)

    def test_a_whole_runway_paragraph_is_not_double_prefixed(self):
        """跑道那一格既可以只填跑道号，也可以填一整段构型说明。

        真实通播里跑道构型是气象**之前**的一整段（"跑道独立平行离场，跑道
        三六左 起始高度 六百米……"）。它自带"跑道"二字，再套一层"使用跑道"
        就成了"使用跑道 跑道独立平行离场"。
        """
        paragraph = "跑道独立平行离场 跑道 三六左 起始高度 六百米"
        script = chinese.render(metar_module.Metar(ZSPD), facility="北京首都",
                                letter="J", runway=paragraph)
        self.assertIn(paragraph, script)
        self.assertNotIn("使用跑道 跑道", script)
        # 只填跑道号时还是要有名头
        short = chinese.render(metar_module.Metar(ZSPD), facility="北京首都",
                               letter="J", runway="三六左")
        self.assertIn("使用跑道 三六左", short)

    def test_script_has_no_latin_weather_codes(self):
        # 漏翻的电码会被 TTS 逐字母念出来，非常难听
        script = chinese.render(metar_module.Metar(
            "ZBAA 270830Z 27010G18MPS 3000 -RA BKN020CB OVC050 M03/M07 Q1025"))
        for code in ("MPS", "BKN", "OVC", "CB", "RA", "Q10"):
            self.assertNotIn(code, script)

    def test_missing_metar_does_not_raise(self):
        self.assertIn("通播", chinese.render(None, facility="上海浦东", letter="A"))

    def test_garbage_metar_does_not_raise(self):
        chinese.render(metar_module.Metar("完全不是报文"))


class FakeChannels:
    def __init__(self, server):
        self.server = server

    def find_by_name(self, name):
        import pymumble_py3 as pymumble
        with self.server.lock:
            if name in self.server.by_name:
                return self.server.by_name[name]
        raise pymumble.errors.UnknownChannelError(name)

    def new_channel(self, parent_id, name, temporary=False):
        self.server.hang("channels.new_channel")


class FakeMyself:
    def __init__(self, server):
        self.server = server

    def __getitem__(self, key):
        if key == "channel_id":
            return self.server.my_channel
        raise KeyError(key)

    def get(self, key, default=None):
        if key == "channel_id":
            return self.server.my_channel
        return default

    def move_in(self, channel_id, token=None):
        self.server.hang("users.myself.move_in")


class FakeUsers:
    def __init__(self, server, session):
        self.myself = FakeMyself(server)
        self.myself_session = session


class FakeMumble:
    """够用的 Mumble 替身，重点是把 pymumble 的两种接口区别开。

    - ``execute_command(cmd, blocking=False)``：命令排队，假服务器在
      ``latency`` 之后才让它生效——真实的 pymumble 就是这样，命令是异步的，
      发出去不等于已经生效。
    - ``channels.new_channel()`` / ``users.myself.move_in()``：**永远不返回**。
      这两个入口走 ``execute_command(blocking=True)``，那个 ``lock.acquire()``
      没有任何超时（pymumble 源码里就写着 "TODO: manage a timeout for blocking
      commands"），服务器不处理命令时调用线程就死在那一行。

    照搬这个行为，任何还在走阻塞接口的代码都会在测试里挂住，被
    ``join(timeout=…)`` 抓出来——这比断言"有没有调用某个函数"结实得多。
    """

    def __init__(self, latency=0.0, answers=True, my_channel=0, session=42):
        self.lock = threading.Lock()
        self.latency = latency
        self.answers = answers          # False = 服务器收下命令但什么都不做
        self.by_name = {}
        self.my_channel = my_channel
        self.next_id = 1
        self.commands = []              # 走异步接口发出去的命令
        self.blocking_calls = []        # 有人走了阻塞接口（会挂死）
        self.channels = FakeChannels(self)
        self.users = FakeUsers(self, session)

    def hang(self, what):
        self.blocking_calls.append(what)
        threading.Event().wait()        # 永远不返回，和真的 pymumble 一样

    def execute_command(self, cmd, blocking=True):
        if blocking:
            self.hang("execute_command(blocking=True)")
        self.commands.append(cmd)
        if self.answers:
            timer = threading.Timer(self.latency, self._apply, args=(cmd,))
            timer.daemon = True
            timer.start()

    def _apply(self, cmd):
        params = cmd.parameters
        with self.lock:
            if "name" in params:                    # CreateChannel
                self.by_name[params["name"]] = {
                    "channel_id": self.next_id,
                    "name": params["name"],
                    "parent": params["parent"],
                    "temporary": params["temporary"],
                }
                self.next_id += 1
            elif "session" in params:               # MoveCmd
                self.my_channel = params["channel_id"]

    # 便于断言
    def created_names(self):
        return [(c.parameters["parent"], c.parameters["name"],
                 c.parameters["temporary"])
                for c in self.commands if "name" in c.parameters]

    def moves(self):
        return [c.parameters["channel_id"] for c in self.commands
                if "session" in c.parameters]


class ReconnectLimitTest(unittest.TestCase):
    """掉线之后最多重连三次，都失败就**这个席位**停播。

    以前是 `reconnect=True` 一路无限重试。通播这边尤其糟：所有席位都用同一个
    保留账号（默认 900），而服务端 `login.py` 对认证失败按 CAN ID 限流——一个
    席位的僵尸连接足以把这台机器上其它席位的语音一起锁死。

    停播只停这一个席位：同一个客户端上的其它席位各有自己的连接。
    """

    def setUp(self):
        try:
            import opuslib  # noqa: F401
        except Exception:
            for name in ("opuslib", "opuslib.api", "opuslib.api.decoder",
                         "opuslib.api.encoder", "opuslib.api.info",
                         "opuslib.exceptions"):
                sys.modules.setdefault(name, mock.MagicMock())
        global broadcast
        import broadcast
        self.broadcast = broadcast
        self.FAILED = broadcast.PYMUMBLE_CONN_STATE_FAILED

    def make_mumble(self, results, limit=3):
        """假基类：connect() 按 results 依次返回状态码，并记下调用次数。"""
        test = self

        class FakeBase:
            def __init__(self, *args, **kwargs):
                self.reconnect = kwargs.get("reconnect", False)
                self.connected = 0
                self.calls = 0
                test.base = self

            def connect(self):
                index = self.calls
                self.calls += 1
                value = results[index] if index < len(results) else test.FAILED
                self.connected = value
                return value

        mumble = self.broadcast.bounded_mumble(FakeBase)(
            "host", "900_atis127800", reconnect=True)
        mumble.reconnect_limit = limit
        return mumble

    def test_the_limit_stops_the_retrying(self):
        mumble = self.make_mumble([self.FAILED] * 10)
        mumble._session_established()          # 先有过一次真会话
        for expected in (1, 2, 3):
            mumble.connect()
            self.assertEqual(mumble.reconnect_attempts, expected)
            self.assertFalse(mumble.gave_up)

        mumble.connect()
        self.assertTrue(mumble.gave_up)
        self.assertFalse(mumble.reconnect,
                         "reconnect 必须置假，否则 pymumble 的 run() 还会继续转")
        self.assertEqual(self.base.calls, 3, "放弃那一次不该真的再连一遍")

    def test_authenticating_is_not_a_successful_connect(self):
        """`connect()` 成功返回的是 AUTHENTICATING，不是 CONNECTED。

        密码错的连接同样返回它，随后才在 loop() 里因为 Reject 结束。按返回值判
        "连上了"就会每次清零计数，于是无限重连——正好撞上按账号的限流。
        """
        mumble = self.make_mumble([1] * 10)        # 1 = AUTHENTICATING
        mumble._session_established()
        for _ in range(4):
            mumble.connect()
        self.assertTrue(mumble.gave_up, "被拒的重连被当成了成功")

    def test_a_real_session_resets_the_count(self):
        mumble = self.make_mumble([self.FAILED] * 10)
        mumble._session_established()
        mumble.connect()
        mumble.connect()
        mumble._session_established()          # 连回来了
        self.assertEqual(mumble.reconnect_attempts, 0)
        for _ in range(3):
            mumble.connect()
        self.assertFalse(mumble.gave_up, "重连成功后没有重新给满三次")

    def test_the_first_connection_is_not_a_reconnect(self):
        mumble = self.make_mumble([self.FAILED] * 10)
        for _ in range(5):
            mumble.connect()
        self.assertFalse(mumble.gave_up)
        self.assertTrue(mumble.reconnect)

    def test_giving_up_shouts_because_nobody_is_watching_that_thread(self):
        """连接是 mumble.start() 起的，pymumble 那条线程放弃后自己就没了。

        外面收不到任何信号：播报循环最多要等一整轮（_wait_for_quiet 能等 60 秒）
        才可能注意到。所以放弃的那一刻必须主动喊，并且把 stop_event 置上让所有
        wait 立刻返回。
        """
        caster = self.broadcast.Broadcaster.__new__(self.broadcast.Broadcaster)
        caster.running = True
        caster.gave_up = False
        caster.reconnect_limit = 3
        caster.stop_event = threading.Event()
        caster.station = type("S", (), {"callsign": "ZSPD_ATIS"})()
        states = []
        caster._state = lambda state, message: states.append((state, message))

        mumble = self.make_mumble([self.FAILED] * 10)
        mumble.on_give_up = caster._on_give_up
        mumble._session_established()
        for _ in range(4):
            mumble.connect()

        self.assertTrue(caster.gave_up)
        self.assertEqual(states[-1][0], 'offline', states)
        self.assertIn("ZSPD_ATIS", states[-1][1])
        self.assertTrue(caster.stop_event.is_set(),
                        "没有把 stop_event 置上，播报循环还会干等一整轮")

    def test_the_stop_message_does_not_bury_the_reason(self):
        """播报循环结束时那句"通播已停止"不能盖掉下线的原因。

        盖掉的话界面上只剩一个没有解释的停止，用户不知道是掉线还是自己按的。
        """
        caster = self.broadcast.Broadcaster.__new__(self.broadcast.Broadcaster)
        caster.running = False
        caster.gave_up = True
        caster.stop_event = threading.Event()
        caster.stop_event.set()
        caster._text_lock = threading.Lock()
        caster._voice_text = "x"
        caster._pending_text = None
        states = []
        caster._state = lambda state, message: states.append((state, message))

        caster._loop()
        self.assertEqual(states, [], f"不该再报一句停止：{states}")


class AtisFsdReconnectTest(unittest.TestCase):
    """席位的 FSD 链路同一条策略：掉线重连三次，用尽就整个下线。

    两条链路必须一致，否则"整个下线"没有统一含义：语音三次、FSD 一次就放弃的
    话，一次服务器重启会让席位从网络上消失而频率上还在播。
    """

    def setUp(self):
        global fsdclient
        import fsdclient
        self.fsdclient = fsdclient

    def make_client(self, connect_results, limit=3):
        """把 _connect / _loop / _close 换成替身，只测重连那一层的控制流。"""
        client = fsdclient.FSDClient.__new__(fsdclient.FSDClient)
        client.callsign = "ZSPD_ATIS"
        client.running = True
        client.stop_event = threading.Event()
        client.reconnect_limit = limit
        client.gave_up = False
        client._retryable = False
        client.on_status = None
        client.states = []
        # 照抄 _status 里那次翻译：重连期间的 error/stopped 不是终态
        client._status = lambda state, message: client.states.append(
            ('reconnecting' if client._retryable and state in ('error', 'stopped')
             else state, message))
        client.connect_calls = 0

        def fake_connect():
            index = client.connect_calls
            client.connect_calls += 1
            ok = connect_results[index] if index < len(connect_results) else False
            return ok

        client._connect = fake_connect
        client._loop = lambda: None
        client._close = lambda: None
        return client

    def test_three_attempts_then_the_station_goes_offline(self):
        client = self.make_client([True] + [False] * 10)
        # 不真的等 3 秒 × 3
        self.fsdclient.RECONNECT_DELAY = 0
        client._run()
        self.assertTrue(client.gave_up)
        self.assertEqual(client.connect_calls, 4, "一次首连 + 三次重连")
        self.assertEqual(client.states[-1][0], 'offline', client.states)
        self.assertIn("ZSPD_ATIS", client.states[-1][1])

    def test_a_reconnect_that_works_resets_the_count(self):
        client = self.make_client([True, False, True] + [False] * 10)
        self.fsdclient.RECONNECT_DELAY = 0
        client._run()
        self.assertTrue(client.gave_up)
        self.assertEqual(client.connect_calls, 6,
                         "首连 + 失败一次 + 重连成功，之后第二轮再给满三次")

    def test_the_first_connection_is_not_retried(self):
        """首连失败多半是密码不对或者呼号被占，重试只会把同一条错误刷三遍。"""
        client = self.make_client([False] * 10)
        client._run()
        self.assertFalse(client.gave_up)
        self.assertEqual(client.connect_calls, 1)

    def test_stopping_does_not_look_like_reconnecting(self):
        """有人按了停止播出，界面上不能写"重连中"。"""
        client = self.make_client([True] + [False] * 10)
        client._retryable = True
        client.running = False
        client.stop_event.set()
        client.thread = None
        fsdclient.FSDClient.stop(client)
        self.assertFalse(client._retryable)


class JoinChannelTest(unittest.TestCase):
    """频率频道不存在时要新建，并且要等服务器把它回报回来。

    钉两件事：
    - 建频道 / 进频道都不能走 pymumble 的阻塞接口。那个锁没有超时，命令没被
      处理就永久卡住，通播线程整个死掉——日志停在建频道那一行，既没有成功也
      没有任何错误。
    - 建完 sleep(0.2) 就去找是不够的，远程服务器上经常还没回来，报出来是
      "Channel FREQ_127800 does not exists"——听着像频道建不了，其实只是没等到。
    """

    def setUp(self):
        import sys
        from unittest import mock
        try:
            import opuslib  # noqa: F401
        except Exception:
            for name in ("opuslib", "opuslib.api", "opuslib.api.decoder",
                         "opuslib.api.encoder", "opuslib.api.info",
                         "opuslib.exceptions"):
                sys.modules.setdefault(name, mock.MagicMock())
        global broadcast
        import broadcast
        self.broadcast = broadcast
        # 有的用例要改这个模块级常量，跑完必须还原，否则会影响别的用例
        self._timeout = broadcast.CHANNEL_TIMEOUT

        # 不跑真的构造函数——它会拉起合成器和线程
        self.caster = broadcast.Broadcaster.__new__(broadcast.Broadcaster)
        self.caster.running = True
        self.caster.stop_event = threading.Event()
        self.caster._denial = None
        self.caster.states = []
        self.caster._state = lambda kind, message: self.caster.states.append(
            (kind, message))
        self.caster.station = type("S", (), {
            "channel": "FREQ_127800", "frequency": "127.800",
            "callsign": "ROAH_ATIS"})()

        self.server = FakeMumble(latency=0.3)
        self.caster.mumble = self.server

    def tearDown(self):
        self.broadcast.CHANNEL_TIMEOUT = self._timeout

    def join(self, budget=None):
        """在独立线程里调 _join_channel，返回 (结果, 耗时)。

        卡住的话不会拖死整个测试——线程是 daemon，join 超时就当场失败，并把
        走过的阻塞接口打出来。
        """
        if budget is None:
            budget = self.broadcast.CHANNEL_TIMEOUT * 2 + 3
        box = {}

        def work():
            box["value"] = self.caster._join_channel()

        thread = threading.Thread(target=work, daemon=True)
        started = time.time()
        thread.start()
        thread.join(budget)
        elapsed = time.time() - started
        self.assertFalse(
            thread.is_alive(),
            f"_join_channel 在 {budget:.1f} 秒内没有返回；"
            f"走过的阻塞接口={self.server.blocking_calls}")
        return box["value"], elapsed

    def test_existing_channel_is_used_directly(self):
        self.server.by_name["FREQ_127800"] = {"channel_id": 7}
        result, _ = self.join()
        self.assertTrue(result)
        self.assertEqual(self.server.created_names(), [], "已存在就不该再建")
        self.assertEqual(self.server.moves(), [7])
        self.assertEqual(self.server.my_channel, 7, "要真的进去，不是发完就算")

    def test_already_in_the_channel_needs_no_command_at_all(self):
        self.server.by_name["FREQ_127800"] = {"channel_id": 7}
        self.server.my_channel = 7
        result, _ = self.join()
        self.assertTrue(result)
        self.assertEqual(self.server.commands, [])

    def test_missing_channel_is_created_as_temporary(self):
        result, _ = self.join()
        self.assertTrue(result)
        self.assertEqual(self.server.created_names(), [(0, "FREQ_127800", True)])
        self.assertEqual(self.server.my_channel, self.server.moves()[0])

    def test_waits_rather_than_giving_up_immediately(self):
        # 服务器 0.3 秒后才回报频道——固定 sleep(0.2) 的老写法会在这里失败
        self.server.latency = 0.3
        result, elapsed = self.join()
        self.assertTrue(result)
        self.assertGreaterEqual(elapsed, 0.3, "至少要等到服务器回话")

    def test_a_server_that_never_answers_does_not_hang_the_thread(self):
        """这才是那个 bug：阻塞接口下服务器不回话，线程永远回不来。

        换成轮询之后，最坏也只是等满 CHANNEL_TIMEOUT 然后报错返回。
        """
        self.broadcast.CHANNEL_TIMEOUT = 0.5
        self.server.answers = False
        result, elapsed = self.join()
        self.assertFalse(result)
        self.assertGreaterEqual(elapsed, 0.5, "该等的还是要等满")
        self.assertLess(elapsed, 3.0, "但必须有上界")
        self.assertEqual(self.server.blocking_calls, [],
                         "不能再走 pymumble 那两个没有超时的阻塞接口")

    def test_move_that_never_takes_effect_is_not_reported_as_online(self):
        """进频道也是异步的，没确认就报"已在播出"，通播会对着根频道播。"""
        self.broadcast.CHANNEL_TIMEOUT = 0.5
        self.server.by_name["FREQ_127800"] = {"channel_id": 7}
        self.server.answers = False          # 命令收下了，但人不会被移过去
        result, elapsed = self.join()
        self.assertFalse(result)
        self.assertEqual(self.server.moves(), [7], "命令还是要发出去的")
        self.assertGreaterEqual(elapsed, 0.5)
        self.assertNotIn("online", [kind for kind, _ in self.caster.states])
        self.assertIn("没有生效", self.caster.states[-1][1])

    def test_timeout_says_so_without_blaming_the_channel(self):
        self.broadcast.CHANNEL_TIMEOUT = 0.3
        self.server.answers = False
        result, _ = self.join()
        self.assertFalse(result)
        kind, message = self.caster.states[-1]
        self.assertEqual(kind, "error")
        self.assertIn("没有出现", message)

    def test_permission_denial_is_reported_as_such(self):
        # 缺 MakeTempChannel 时再等也不会有，要说清楚是权限问题
        self.broadcast.CHANNEL_TIMEOUT = 5.0
        self.server.answers = False

        def deny():
            time.sleep(0.1)
            self.caster._denial = "没有权限（建立频率频道需要根频道的 MakeTempChannel 权限）"

        threading.Thread(target=deny, daemon=True).start()
        result, elapsed = self.join()
        self.assertFalse(result)
        self.assertLess(elapsed, 2.0, "被拒绝之后不该继续等满超时")
        self.assertIn("MakeTempChannel", self.caster.states[-1][1])

    def test_denial_callback_translates_the_type(self):
        self.caster.mumble.denial_type = lambda t: "Permission"
        self.caster._on_permission_denied(type("E", (), {"type": 1, "reason": ""})())
        self.assertIn("MakeTempChannel", self.caster._denial)

    def test_unknown_denial_type_still_reports_something(self):
        self.caster.mumble.denial_type = lambda t: "SomethingNew"
        self.caster._on_permission_denied(type("E", (), {"type": 9, "reason": ""})())
        self.assertIn("SomethingNew", self.caster._denial)

    def test_stopping_aborts_the_wait(self):
        self.broadcast.CHANNEL_TIMEOUT = 30.0
        self.server.answers = False
        self.caster.stop_event.set()
        result, elapsed = self.join(budget=5.0)
        self.assertFalse(result)
        self.assertLess(elapsed, 2.0, "停止时不该继续等")

    def test_stopping_aborts_the_wait_for_the_move_too(self):
        self.broadcast.CHANNEL_TIMEOUT = 30.0
        self.server.by_name["FREQ_127800"] = {"channel_id": 7}
        self.server.answers = False

        def stop():
            time.sleep(0.2)
            self.caster.stop_event.set()

        threading.Thread(target=stop, daemon=True).start()
        result, elapsed = self.join(budget=5.0)
        self.assertFalse(result)
        self.assertLess(elapsed, 2.0, "停止时不该继续等")


class WeatherFetchTest(unittest.TestCase):

    def test_normalize_strips_prefix_and_checks_station(self):
        self.assertEqual(weather.normalize("METAR " + ZSPD, "ZSPD"), ZSPD)
        self.assertIsNone(weather.normalize("ZBAA 251300Z", "ZSPD"))

    def test_bad_icao_is_rejected_before_any_request(self):
        with self.assertRaises(weather.WeatherError):
            weather.fetch_metar("ZS")


class WeatherRetryTest(unittest.TestCase):
    """气象源在 CDN 后面，偶发一次连接/TLS 抖动不该让席位整整 5 分钟没天气。

    实测遇到过一次 `CERTIFICATE_VERIFY_FAILED: certificate has expired`，而
    服务器证书本身是好的（有效期还有两个月），几分钟后自己就恢复了。
    """

    def setUp(self):
        self._delay = weather.RETRY_DELAY
        weather.RETRY_DELAY = 0.0        # 测试里不真的等
        self.calls = []

    def tearDown(self):
        weather.RETRY_DELAY = self._delay

    def urlopen(self, *outcomes):
        """按 outcomes 依次应答：异常就抛，字符串就当成报文返回。"""

        class Response:
            def __init__(self, body):
                self.body = body

            def read(self):
                return self.body.encode()

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake(request, timeout=None):
            self.calls.append(request.full_url)
            outcome = outcomes[min(len(self.calls) - 1, len(outcomes) - 1)]
            if isinstance(outcome, Exception):
                raise outcome
            return Response(outcome)
        return fake

    def test_a_transient_failure_is_retried_and_succeeds(self):
        from unittest import mock
        blip = OSError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate has expired")
        with mock.patch("urllib.request.urlopen", self.urlopen(blip, ZSPD)):
            self.assertEqual(weather.fetch_metar("ZSPD"), ZSPD)
        self.assertEqual(len(self.calls), 2, "第一次失败之后应当再试一次")

    def test_it_gives_up_after_the_retries(self):
        from unittest import mock
        blip = OSError("boom")
        with mock.patch("urllib.request.urlopen", self.urlopen(blip)):
            with self.assertRaises(weather.WeatherError):
                weather.fetch_metar("ZSPD")
        self.assertEqual(len(self.calls), weather.RETRIES + 1,
                         "不能无限重试，那会把气象源打死")

    def test_a_bad_icao_is_not_retried(self):
        from unittest import mock
        with mock.patch("urllib.request.urlopen", self.urlopen(ZSPD)):
            with self.assertRaises(weather.WeatherError):
                weather.fetch_metar("ZS")
        self.assertEqual(self.calls, [], "ICAO 写错了，重试多少遍都一样")

    def test_certificate_failures_say_what_to_check(self):
        """原文只会让人以为服务器坏了，实际能动的是本机时间和根证书。"""
        from unittest import mock
        blip = OSError("<urlopen error [SSL: CERTIFICATE_VERIFY_FAILED] "
                       "certificate verify failed: certificate has expired>")
        with mock.patch("urllib.request.urlopen", self.urlopen(blip)):
            with self.assertRaises(weather.WeatherError) as caught:
                weather.fetch_metar("ZSPD")
        message = str(caught.exception)
        self.assertIn("ZSPD", message)
        self.assertIn("本机时间", message)
        self.assertIn("CERTIFICATE_VERIFY_FAILED", message, "原文也要留着")

    def test_the_url_is_the_configured_one(self):
        from unittest import mock
        with mock.patch("urllib.request.urlopen", self.urlopen(ZSPD)):
            weather.fetch_metar("ZSPD", "https://例子/q?id=")
        self.assertEqual(self.calls, ["https://例子/q?id=ZSPD"])


def _load_broadcast():
    """延迟导入 broadcast：它会拉起 pymumble→opuslib，本机没有 opus 原生库时
    要先放替身，所以不能放在模块顶层导。"""
    from unittest import mock
    for name in ("opuslib", "opuslib.api", "opuslib.api.decoder",
                 "opuslib.api.encoder", "opuslib.api.info", "opuslib.exceptions"):
        sys.modules.setdefault(name, mock.MagicMock())
    import broadcast
    return broadcast


class RejectIsNotACrashTest(unittest.TestCase):
    """服务器拒绝连接是预期内的，不该记成 CRITICAL 未捕获异常。

    pymumble 的 run() 只接 socket.error，ConnectionRejectedError 会一路穿出连接
    线程，被 applog 的 threading.excepthook 记成"未捕获异常"外加一段 traceback
    ——实测日志里，一次普通的密码错误看上去就像程序崩了，排查时非常容易被带偏。

    在 run() 外面接住是安全的：pymumble 抛之前已经把 connected 置成 FAILED、也
    释放了 ready_lock，异常剩下的唯一作用就是终止线程，而 run() 正常返回同样
    终止线程。
    """

    def test_rejection_does_not_reach_the_thread_excepthook(self):
        from unittest import mock
        broadcast = _load_broadcast()
        import pymumble_py3 as pymumble
        client = broadcast.RejectAwareMumble.__new__(broadcast.RejectAwareMumble)
        escaped = []
        saved = threading.excepthook
        threading.excepthook = lambda args: escaped.append(args.exc_type.__name__)
        try:
            with mock.patch.object(
                    pymumble.Mumble, "run",
                    side_effect=pymumble.errors.ConnectionRejectedError("bad pw")):
                thread = threading.Thread(target=client.run)
                thread.start()
                thread.join(5)
        finally:
            threading.excepthook = saved
        self.assertFalse(thread.is_alive(), "连接线程要正常收尾")
        self.assertEqual(escaped, [], "拒绝连接不该冒成未捕获异常")

    def test_other_errors_still_propagate(self):
        """只吞"被拒绝"这一种，真出别的错还是要炸出来。"""
        from unittest import mock
        broadcast = _load_broadcast()
        import pymumble_py3 as pymumble
        client = broadcast.RejectAwareMumble.__new__(broadcast.RejectAwareMumble)
        with mock.patch.object(pymumble.Mumble, "run",
                               side_effect=RuntimeError("别的错")):
            with self.assertRaises(RuntimeError):
                client.run()


if __name__ == "__main__":
    unittest.main(verbosity=2)
