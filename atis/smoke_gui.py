"""GUI 冒烟测试：离屏把窗口和对话框都建起来，不连服务器、不取天气。

    python smoke_gui.py        （在 atis 目录下运行）

天气获取被换成固定报文，避免测试依赖外网。
"""

import os
import sys
import tempfile
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# 界面语言钉死，断言才有确定的结果。不钉的话，第一次启动是跟系统走的，在英文
# 系统上跑这个脚本，所有比中文字面量的断言都会失败。
os.environ.setdefault("CAN_LANG", "zh")

# 挪到临时目录再跑。Profile / Settings 的路径都是相对当前目录的裸文件名，直接在
# atis 目录下跑的话，这个测试会**读到并写坏使用者真实的 atis_profile.json**——
# "添加两个席位" 在你已经配过席位时必然失败（呼号重复），跑完还会把它覆盖掉。
# airports.py 和图标走的都是 __file__ / sys._MEIPASS，所以换目录是安全的。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(tempfile.mkdtemp(prefix="can-atis-smoke-"))

# pymumble 需要本机的 opus 原生库。这里不碰音频，缺库时放个替身让导入过去。
try:
    import opuslib  # noqa: F401
except Exception:
    for _name in ("opuslib", "opuslib.api", "opuslib.api.decoder",
                  "opuslib.api.encoder", "opuslib.api.info", "opuslib.exceptions"):
        sys.modules.setdefault(_name, mock.MagicMock())
    print("提示: 未找到 opus 原生库，已用替身放行（不影响本测试）\n")

from PyQt6.QtWidgets import QApplication, QMessageBox

# 模态对话框在离屏模式下同样会一直等人点，测试里换成记录调用。
# question 也必须换掉——它会等一个按钮，漏了就整个测试挂死。
_dialogs = []
for _name in ("warning", "critical", "information"):
    setattr(QMessageBox, _name,
            staticmethod(lambda *args, _n=_name: _dialogs.append(
                (_n, args[2] if len(args) > 2 else ""))))


def _question(*args, **kwargs):
    _dialogs.append(("question", args[2] if len(args) > 2 else ""))
    return QMessageBox.StandardButton.No      # 测试里一律选"否"


QMessageBox.question = staticmethod(_question)

# 查更新会真的发网络请求，而弹出来的那个 QMessageBox 用的是 exec()——离屏模式下
# 它照样会一直等人点，整个冒烟测试就挂死在那里。这里一律换成"没有新版"；
# 真正的对话框逻辑另有一条用例单独测。
import update
update.check = lambda *args, **kwargs: None

import gui
import weather
from profile import Station

ZSPD = "ZSPD 251300Z 09004MPS 9999 FEW030 SCT100 25/18 Q1013 NOSIG"


def main():
    app = QApplication(sys.argv)
    failures = []

    # 不要在冒烟测试里真的去取天气
    weather.fetch_metar = lambda icao, url=None, timeout=15: ZSPD

    def check(name, fn):
        try:
            fn()
            print(f"  ok   {name}")
        except Exception as e:
            failures.append((name, e))
            print(f"  FAIL {name}: {type(e).__name__}: {e}")

    # 用临时配置，别动开发机上真实的 atis_profile.json
    import profile as profile_module
    profile_module.Profile.save = lambda self: None

    print("主窗口：")
    window = gui.AtisWindow()
    window.profile.path = os.devnull
    check("建立主窗口", lambda: window)

    print("席位：")

    def add_stations():
        window.profile.add(Station("ZSPD", "浦东", "127.850"))
        window.profile.add(Station("ZSPD", "浦东离场", "127.900",
                                   atis_type=profile_module.TYPE_DEPARTURE))
        window.refresh_stations()
        assert window.station_list.count() == 2
    check("添加两个席位", add_stations)

    check("呼号按类型区分", lambda: (_ for _ in ()).throw(AssertionError(
        [s.callsign for s in window.profile]))
        if {s.callsign for s in window.profile} != {"ZSPD_ATIS", "ZSPD_D_ATIS"} else None)

    check("选中第一个", lambda: window.station_list.setCurrentRow(0))

    # 用 isHidden() 而不是 isVisible()：离屏跑的时候顶层窗口没有 show()，
    # isVisible() 对所有子控件一律返回 False，测不出任何东西
    def the_metar_line_follows_the_selection():
        """切席位时 METAR 那一行要跟着走。

        原来它只在 on_metar 里、且到货的正好是当前席位时才写一次——于是屏幕上
        会出现"选中 ZBAA、右边渲染的是 ZBAA、METAR 那行却是 ZSHC"，看的人会
        以为渲染用错了天气。
        """
        import metar as metar_module
        first, second = window.profile.stations[0], window.profile.stations[1]
        window.on_metar(first.callsign, metar_module.Metar(ZSPD), "")
        window.station_list.setCurrentRow(1)
        text = window.metar_label.text()
        assert "09004MPS" not in text, f"切到别的席位还显示上一个的报文：{text!r}"
        window.station_list.setCurrentRow(0)
        assert "09004MPS" in window.metar_label.text(), \
            f"切回来没有恢复：{window.metar_label.text()!r}"
    check("METAR 行跟着席位走", the_metar_line_follows_the_selection)

    def manual_text_can_be_edited_on_the_main_page():
        station = window.current_station()
        preset = station.presets[0]
        previous = (preset.manual_text, preset.manual_atis, preset.manual_voice)
        try:
            preset.manual_text = True
            preset.manual_atis = "手动文字稿"
            preset.manual_voice = "手动语音稿"
            window.regenerate()
            assert not window.text_preview.isReadOnly(), "手动模式下文字通播仍是只读"
            assert not window.voice_preview.isReadOnly(), "手动模式下语音稿仍是只读"
            window.text_preview.setPlainText("新的文字稿")
            window.voice_preview.setPlainText("新的语音稿")
            assert preset.manual_atis == "新的文字稿"
            assert preset.manual_voice == "新的语音稿"

            dialog = gui.PresetDialog(preset, parent=window)
            assert dialog.generated_fields.isHidden(), "手动模式下模板字段没有隐藏"
        finally:
            preset.manual_text, preset.manual_atis, preset.manual_voice = previous
            window.regenerate()
    check("手动模式可编辑主页面稿件", manual_text_can_be_edited_on_the_main_page)

    def the_top_bar_does_not_grow_with_the_window():
        """顶栏纵向要钉成 Fixed。

        它是个包了一层的 QWidget（精简模式要整条藏起来），而裸 QWidget 默认是
        Preferred，会跟着窗口一起长——多出来的高度全被这一条吃掉，账号输入框
        浮在窗口正中间，底下的分割器反被挤扁。用 addLayout 时没这问题，所以是
        包那一层引进来的。
        """
        window.show()
        window.resize(1000, 560)
        app.processEvents()
        short = window.top_bar.height()
        window.resize(1000, 960)
        app.processEvents()
        tall = window.top_bar.height()
        assert tall == short, f"窗口拉高之后顶栏从 {short} 变成 {tall}"
        assert tall < 120, f"顶栏 {tall}px，太高了"
    check("顶栏不跟着窗口长高", the_top_bar_does_not_grow_with_the_window)

    def pinning_is_remembered():
        from settings import Settings
        window.toggle_always_on_top(True)
        assert Settings().always_on_top is True, "置顶状态没有存下来"
        window.toggle_always_on_top(False)
        assert Settings().always_on_top is False
    check("置顶状态记得住", pinning_is_remembered)

    def compact_leaves_only_the_list():
        window.toggle_compact(True)
        for name in ("top_bar", "station_buttons", "right_panel"):
            assert getattr(window, name).isHidden(), f"{name} 没有收起来"
        assert not window.station_list.isHidden(), "席位列表被一起藏掉了"
        assert not window.compact_button.isHidden(), "切回去的开关自己也被藏了"
    check("精简只留席位列表", compact_leaves_only_the_list)

    def expanding_restores_everything():
        window.toggle_compact(False)
        for name in ("top_bar", "station_buttons", "right_panel"):
            assert not getattr(window, name).isHidden(), f"{name} 没有恢复"
    check("展开全部恢复", expanding_restores_everything)

    def compact_state_is_remembered():
        from settings import Settings
        window.toggle_compact(True)
        assert Settings().compact is True, "精简状态没有存下来"
        window.toggle_compact(False)
        assert Settings().compact is False
    check("精简状态记得住", compact_state_is_remembered)

    def importing_from_the_network_adds_stations():
        """从数据源取席位。不联网——把 datafeed.atis_stations 换成替身。"""
        import datafeed
        original = datafeed.atis_stations
        datafeed.atis_stations = lambda *a, **k: [
            ("ZGGG", "ZGGG_ATIS", "126.500", "combined"),
            ("ZSPD", "ZSPD_ATIS", "127.850", "combined"),   # 已存在，应当跳过
        ]
        try:
            before = len(window.profile)
            window.import_from_network()
            assert len(window.profile) == before + 1, "新席位没加进来"
            added = window.profile.get("ZGGG_ATIS")
            assert added is not None, "ZGGG 没加进来"
            assert added.frequency == "126.500", added.frequency
        finally:
            datafeed.atis_stations = original
            window.profile.remove("ZGGG_ATIS")
            window.refresh_stations()
    check("从网络取席位", importing_from_the_network_adds_stations)

    # 从 can-web 取**配置**（席位 + 预设 + 模板 + 中文用词），和上面取在线席位
    # 不是一回事。不联网：换掉 netconfig.fetch。
    def _network_document(notams=""):
        station = profile_module.Station("ZGGG", "白云", "126.500")
        station.presets = [profile_module.Preset(
            "东向", "[FACILITY] information [ATIS_LETTER]", notams=notams,
            chinese_runway="跑道 洞两左 进港。")]
        entry = station.to_dict()
        entry.pop("letter")          # 服务端不发情报字母，它是运行状态
        return {"version": "smoke1", "updated": "2026-07-30",
                "notes": "冒烟用的一份", "stations": [entry]}

    def _with_network(document, answer, action):
        """把 fetch 和"要不要"的回答都换成固定值，跑完恢复。"""
        import netconfig
        original_fetch, original_question = netconfig.fetch, QMessageBox.question
        netconfig.fetch = lambda url=None, timeout=15: document
        QMessageBox.question = staticmethod(lambda *a, **k: answer)
        try:
            action()
        finally:
            netconfig.fetch = original_fetch
            QMessageBox.question = staticmethod(original_question)

    def updating_from_the_network_brings_whole_stations():
        before = len(window.profile)
        _with_network(_network_document(), QMessageBox.StandardButton.Yes,
                      window.update_from_network)
        added = window.profile.get("ZGGG_ATIS")
        assert len(window.profile) == before + 1, "席位没加进来"
        assert added is not None, "ZGGG_ATIS 没加进来"
        assert added.frequency == "126.500", added.frequency
        # 这才是这个功能的意义：预设和模板一起来，不用自己再打一遍
        assert added.presets[0].name == "东向", [p.name for p in added.presets]
        assert added.presets[0].chinese_runway, "中文跑道词没跟过来"
        assert window.settings.config_version == "smoke1", "版本号没记下来"
    check("取到完整席位（含预设）", updating_from_the_network_brings_whole_stations)

    def a_second_update_reports_no_change():
        _dialogs.clear()
        before = len(window.profile)
        _with_network(_network_document(), QMessageBox.StandardButton.Yes,
                      window.update_from_network)
        assert len(window.profile) == before, "同一份配置又加了一遍"
        assert any("一致" in text for _, text in _dialogs), _dialogs
    check("已是最新时不重复加", a_second_update_reports_no_change)

    def declining_the_overwrite_keeps_local_edits():
        """值班时改过的构型和 NOTAM 都在预设里，选「否」就一个字都不能动。"""
        window.profile.get("ZGGG_ATIS").presets[0].notams = "临时：跑道 02R 关闭"
        _with_network(_network_document("网络版的 NOTAM"),
                      QMessageBox.StandardButton.No, window.update_from_network)
        assert window.profile.get("ZGGG_ATIS").presets[0].notams == \
            "临时：跑道 02R 关闭", "选了否还是被覆盖了"
    check("拒绝覆盖时保留本地修改", declining_the_overwrite_keeps_local_edits)

    def accepting_the_overwrite_keeps_the_letter():
        """覆盖可以，但播了一半把情报字母退回 A 是不行的。"""
        window.profile.get("ZGGG_ATIS").set_letter("F")
        _with_network(_network_document("网络版的 NOTAM"),
                      QMessageBox.StandardButton.Yes, window.update_from_network)
        again = window.profile.get("ZGGG_ATIS")
        assert again.presets[0].notams == "网络版的 NOTAM", again.presets[0].notams
        assert again.letter == "F", f"情报字母被重置成了 {again.letter}"
        window.profile.remove("ZGGG_ATIS")
        window.refresh_stations()
    check("覆盖时保留情报字母", accepting_the_overwrite_keeps_the_letter)

    def a_failed_fetch_says_why():
        import netconfig
        _dialogs.clear()
        original = netconfig.fetch

        def boom(url=None, timeout=15):
            raise netconfig.NetConfigError("连不上 can（测试）")
        netconfig.fetch = boom
        try:
            window.update_from_network()
        finally:
            netconfig.fetch = original
        assert any("连不上" in text for _, text in _dialogs), _dialogs
    check("取不到时给出原因", a_failed_fetch_says_why)

    def departure_and_arrival_are_distinguishable():
        """同一个机场的综合/离场/进场不能长得一模一样。"""
        texts = [window.station_list.item(i).text()
                 for i in range(window.station_list.count())]
        assert len(set(texts)) == len(texts), f"有两行完全相同：{texts}"
    check("同场不同类型能区分", departure_and_arrival_are_distinguishable)

    def advancing_the_letter_updates_the_list():
        station = window.current_station()
        before = station.letter
        window.advance_letter()
        after = window.current_station().letter
        assert after != before, "字母没有推进"
        assert after in window.station_list.currentItem().text(), \
            f"列表还停在上一份：{window.station_list.currentItem().text()!r}"
    check("推进字母列表跟着变", advancing_the_letter_updates_the_list)

    def updating_labels_keeps_the_preset_selection():
        """字母每 5 分钟就可能推进一次，不能顺手把用户选的构型重置掉。

        refresh_stations() 会清空列表再填，顺带触发 on_station_selected()，
        预设下拉框就回到第一项了——所以字母更新只能走 update_station_labels()。
        """
        if window.preset_combo.count() < 2:
            return                      # 只有一个预设时这条测不出来
        window.preset_combo.setCurrentIndex(1)
        chosen = window.preset_combo.currentIndex()
        window.update_station_labels()
        assert window.preset_combo.currentIndex() == chosen, "预设选择被重置了"
    check("更新字母不动预设选择", updating_labels_keeps_the_preset_selection)

    def feed_metar():
        import metar as metar_module
        station = window.current_station()
        window.on_metar(station.callsign, metar_module.Metar(ZSPD), "")
    check("喂一份天气进去", feed_metar)

    def the_list_shows_the_four_things_that_matter():
        """值班时要扫的就这四样：机场、当前代码、风、修压。

        注意第一份报文的 changed 是 False（没有旧值可比），所以列表刷新不能只
        挂在 changed 上——那正是从空白变成有数据的那一次。
        """
        station = window.current_station()
        text = window.station_list.currentItem().text()
        assert station.identifier in text, f"没有机场码：{text!r}"
        assert station.letter in text, f"没有情报字母：{text!r}"
        assert "09004MPS" in text, f"没有风：{text!r}"
        assert "Q1013" in text, f"没有修压：{text!r}"
    check("列表显示机场/代码/风/修压", the_list_shows_the_four_things_that_matter)

    def preview_generated():
        text = window.text_preview.toPlainText()
        voice = window.voice_preview.toPlainText()
        assert "ZSPD ATIS" in text, text
        assert "09004MPS" in text, "文字通播应当照抄电码"
        assert "wind zero niner zero" in voice, "语音稿应当念出来"
    check("生成文字通播和语音稿", preview_generated)

    def letter_advances_on_new_metar():
        import metar as metar_module
        station = window.current_station()
        before = station.letter
        window.on_metar(station.callsign,
                        metar_module.Metar(ZSPD.replace("25/18", "26/19")), "")
        assert station.letter != before, "报文变了就该换情报字母"
    check("天气变化推进情报字母", letter_advances_on_new_metar)

    check("手动推进字母", lambda: window.advance_letter())

    print("取天气失败：")

    def failure_shows_up():
        import metar as metar_module
        station = window.current_station()
        window.on_metar(station.callsign, None,
                        "取 ZSPD 的 METAR 失败: 证书校验没过")
        assert "失败" in window.status_label.text(), window.status_label.text()
    check("失败会显示出来", failure_shows_up)

    def recovery_clears_the_stale_error():
        """一次抖动留下的报错不能一直挂着。

        取天气成功是静默的（只有报文变了才写状态栏），所以没有谁会去改写它，
        天气早就正常了界面还在喊失败——用户只能截图状态栏来问。
        """
        import metar as metar_module
        station = window.current_station()
        # 喂一份和上次一样的报文：这一路是完全静默的，最容易漏
        window.on_metar(station.callsign,
                        metar_module.Metar(window.raw_metars[station.callsign]), "")
        assert "失败" not in window.status_label.text(), window.status_label.text()
    check("恢复之后撤掉旧报错", recovery_clears_the_stale_error)

    def other_stations_errors_survive():
        """两个席位各自失败，好了一个不该把另一个的错误也抹掉。"""
        import metar as metar_module
        first, second = list(window.profile)[0], list(window.profile)[1]
        window.on_metar(first.callsign, None, f"取 {first.callsign} 失败")
        window.on_metar(second.callsign, None, f"取 {second.callsign} 失败")
        window.on_metar(first.callsign, metar_module.Metar(ZSPD), "")
        assert second.callsign in window.status_label.text(), window.status_label.text()
    check("只撤掉自己的那条", other_stations_errors_survive)

    print("中文语音：")

    def chinese_script_is_generated():
        station = window.profile.get("ZSPD_ATIS")
        station.voice_language = profile_module.LANGUAGE_CHINESE
        station.chinese_name = "上海浦东"
        station.chinese_runway = "三六左"
        rendered = window.render_for(station)
        assert rendered, "没有渲染出通播"
        text, voice = rendered
        assert "09004MPS" in text, "文字通播仍应照抄电码"
        for fragment in ("上海浦东", "风向", "风速", "能见度", "气温", "露点",
                         "修正海压"):
            assert fragment in voice, f"中文稿缺少 {fragment}：{voice}"
        assert "wind" not in voice, "选了中文就不该混进英文稿"
    check("中文语音稿", chinese_script_is_generated)

    def bilingual_has_both():
        station = window.profile.get("ZSPD_ATIS")
        station.voice_language = profile_module.LANGUAGE_BOTH
        _, voice = window.render_for(station)
        assert "wind" in voice and "能见度" in voice, "双语应当两种都有"
    check("中英双语", bilingual_has_both)

    def english_is_unchanged():
        station = window.profile.get("ZSPD_ATIS")
        station.voice_language = profile_module.LANGUAGE_ENGLISH
        _, voice = window.render_for(station)
        assert "wind" in voice and "能见度" not in voice
    check("英文不受影响", english_is_unchanged)

    def language_survives_a_save_load():
        station = window.profile.get("ZSPD_ATIS")
        station.voice_language = profile_module.LANGUAGE_CHINESE
        station.chinese_name = "上海浦东"
        again = profile_module.Station.from_dict(station.to_dict())
        assert again.voice_language == profile_module.LANGUAGE_CHINESE
        assert again.chinese_name == "上海浦东"
    check("语言设置能存下来", language_survives_a_save_load)

    def old_profiles_default_to_english():
        # 升级不该改变已有席位的行为
        station = profile_module.Station.from_dict(
            {"identifier": "ZBAA", "frequency": "127.500"})
        assert station.voice_language == profile_module.LANGUAGE_ENGLISH
    check("老配置默认英文", old_profiles_default_to_english)

    print("自动更新：")

    def interval_is_configurable():
        import settings as settings_module
        window.settings.metar_refresh = 900
        assert window.apply_refresh_interval() == 900
        assert window.timer.interval() == 900 * 1000
    check("刷新间隔可配置", interval_is_configurable)

    def silly_intervals_are_clamped():
        import settings as settings_module
        window.settings.metar_refresh = 0
        assert window.apply_refresh_interval() == settings_module.MIN_METAR_REFRESH
        window.settings.metar_refresh = 999999
        assert window.apply_refresh_interval() == settings_module.MAX_METAR_REFRESH
        window.settings.metar_refresh = 300
        window.apply_refresh_interval()
    check("离谱的间隔被夹住", silly_intervals_are_clamped)

    print("对话框：")
    station_dialog = gui.StationDialog(window.current_station(), parent=window)
    check("建立席位对话框", lambda: station_dialog)

    def reject_bad_input():
        _dialogs.clear()
        station_dialog.identifier.setText("ZS")
        station_dialog.validate_and_accept()
        assert _dialogs, "非法机场代码应当被拦下"
    check("席位输入校验", reject_bad_input)

    preset_dialog = gui.PresetDialog(window.current_station().presets[0], parent=window)
    check("建立预设对话框", lambda: preset_dialog)
    check("预设可保存", lambda: preset_dialog.apply())

    settings_dialog = gui.SettingsDialog(window.settings, window)
    check("建立设置对话框", lambda: settings_dialog)

    def custom_voice_host_is_saved_and_used():
        import settings as settings_module
        original_host = window.settings.mumble_host
        station = window.current_station()
        try:
            assert settings_dialog.mumble_host_input.text() == original_host
            settings_dialog.mumble_host_input.setText("  voice.example.net  ")
            settings_dialog.save_and_close()
            assert window.settings.mumble_host == "voice.example.net"
            assert settings_module.Settings().mumble_host == "voice.example.net"
            with mock.patch.object(window.settings, "connect_fsd", False), \
                    mock.patch.object(gui, "Broadcaster") as broadcaster:
                window.start_broadcast(station, "1000", "test-password")
                assert broadcaster.call_args.args[0] == "voice.example.net"
                broadcaster.return_value.start.assert_called_once()
                window.stop_broadcast(station.callsign)
        finally:
            window.settings.mumble_host = original_host
            window.settings.save_settings()
    check("自定义语音地址保存并传给广播连接", custom_voice_host_is_saved_and_used)

    def cancelling_keeps_the_voice_host():
        original_host = window.settings.mumble_host
        dialog = gui.SettingsDialog(window.settings, window)
        dialog.mumble_host_input.setText("cancelled.example.net")
        dialog.reject()
        assert window.settings.mumble_host == original_host
    check("取消设置不改变语音地址", cancelling_keeps_the_voice_host)

    def blank_voice_host_restores_the_default():
        import settings as settings_module
        dialog = gui.SettingsDialog(window.settings, window)
        dialog.mumble_host_input.setText("   ")
        dialog.save_and_close()
        assert window.settings.mumble_host == settings_module.DEFAULT_MUMBLE_HOST
        assert settings_module.Settings().mumble_host == settings_module.DEFAULT_MUMBLE_HOST
    check("语音地址留空恢复默认", blank_voice_host_restores_the_default)

    print("导入 vATIS 配置：")

    def import_vatis_profile():
        import json
        import tempfile
        from PyQt6.QtWidgets import QFileDialog

        profile = {
            "name": "vATIS 测试",
            "stations": [{
                "identifier": "ZGGG", "name": "白云", "atisType": "Arrival",
                "codeRange": {"low": "A", "high": "F"}, "frequency": 127250000,
                "idsEndpoint": "https://ids.example",
                "presets": [{"name": "白天", "template": "[FACILITY] [ATIS_LETTER] @RWY"}],
                "contractions": [{"variableName": "RWY", "text": "RWY 01",
                                  "voice": "runway zero one"}],
            }],
        }
        handle, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(handle, "w", encoding="utf-8") as f:
            json.dump(profile, f)

        # 文件选择框是模态的，测试里直接给定路径
        QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: (path, ""))
        _dialogs.clear()
        try:
            window.import_vatis()
        finally:
            os.remove(path)

        station = window.profile.get("ZGGG_A_ATIS")
        assert station is not None, "应当导入 ZGGG_A_ATIS"
        assert station.frequency == "127.250", station.frequency
        assert station.code_range == ("A", "F"), station.code_range
        assert station.contractions["RWY"][1] == "runway zero one"
        assert _dialogs, "应当给出导入结果说明"
    check("导入并落到席位列表", import_vatis_profile)

    check("导入后能选中新席位", lambda: window.station_list.setCurrentRow(
        window.station_list.count() - 1))

    print("开播前的管制席位核对：")

    def refuse_without_controller():
        _dialogs.clear()
        window.on_precheck("ZSPD_ATIS", "1000", "pw", True, {}, 0)
        assert _dialogs, "没在管制就该拦下来"
        assert "ZSPD_ATIS" not in window.broadcasters, "不该建立任何连接"
    check("没在管制则拒绝开播", refuse_without_controller)

    def ask_when_datafeed_unreachable():
        _dialogs.clear()
        window.on_precheck("ZSPD_ATIS", "1000", "pw", False, {}, 0)
        assert _dialogs, "数据源连不上时应当询问而不是直接放行"
    check("数据源不可达时询问", ask_when_datafeed_unreachable)

    check("核对后按钮恢复可用", lambda: (_ for _ in ()).throw(
        AssertionError("按钮仍然禁用"))
        if not window.broadcast_button.isEnabled() else None)

    print("错误隔离：")

    def fsd_error_keeps_voice():
        # FSD 只是附加能力，登录失败不该把频率上的语音通播一起停掉
        window.broadcasters["ZSPD_ATIS"] = object()
        window.fsd_clients["ZSPD_ATIS"] = type("F", (), {"stop": lambda s: None})()
        _dialogs.clear()
        window.on_broadcast_state("ZSPD_ATIS", "fsd-error", "[FSD] 拒绝登录")
        assert "ZSPD_ATIS" in window.broadcasters, "语音不该被停掉"
        assert "ZSPD_ATIS" not in window.fsd_clients, "FSD 那条应当收掉"
        assert not _dialogs, "不该弹错误框打断播出"
        window.broadcasters.pop("ZSPD_ATIS")
    check("FSD 失败时语音继续", fsd_error_keeps_voice)

    print("播出状态回调：")
    check("状态回报", lambda: window.on_broadcast_state("ZSPD_ATIS", "online", "测试"))
    check("未播出时删除席位", lambda: window.remove_station())

    print("多语言：")

    def english_builds_every_window():
        """整套界面用英文再建一遍。

        漏翻的键会原样显示成 "settings.title" 这种，扫一遍就能抓住——单看中文
        界面是永远发现不了的。
        """
        import i18n
        from i18n import t
        i18n.set_language("en")
        try:
            english = gui.SettingsDialog(window.settings, window)
            assert english.windowTitle() == t("settings.title"), english.windowTitle()
            station_dialog = gui.StationDialog(parent=window)
            assert station_dialog.windowTitle() == t("station.title")
            # 通播稿的语言下拉不能跟着界面语言变内容，只有说法跟着变
            labels = [station_dialog.language.itemText(i)
                      for i in range(station_dialog.language.count())]
            assert len(labels) == 3, labels
            assert all("." not in text for text in labels), labels
        finally:
            i18n.set_language("zh")
    check("英文界面能建起来", english_builds_every_window)

    window.timer.stop()

    print()
    if failures:
        print(f"{len(failures)} 项失败")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
