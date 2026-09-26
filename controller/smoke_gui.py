"""GUI 冒烟测试：离屏把窗口和电台栈都建起来，不连任何服务器。

    python smoke_gui.py        （在 controller 目录下运行）

只验证"控件能不能建出来、信号能不能连上、槽函数会不会炸"，
连接、音频、发话都不在这里测。
"""

import os
import sys
import tempfile
import time
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# 挪到临时目录再跑。Settings.config_file 是 "radio_settings.json" 这样的裸文件名，
# 相对当前目录解析——直接在 controller 目录下跑的话，这个测试会**读到开发者真实
# 的电台栈**（于是 "应该有两部电台" 在你存过频率时必然失败），跑完还会把它清空。
# gui.resource_path 走的是 __file__，applog 这里也没启动，所以换目录是安全的。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(tempfile.mkdtemp(prefix="can-smoke-"))

# pymumble 需要本机的 opus 原生库来编解码音频。这里不碰音频，缺库时放个替身
# 让导入过去；pymumble 本身仍然是真的。
try:
    import opuslib  # noqa: F401
except Exception:
    for _name in ("opuslib", "opuslib.api", "opuslib.api.decoder",
                  "opuslib.api.encoder", "opuslib.api.info", "opuslib.exceptions"):
        sys.modules.setdefault(_name, mock.MagicMock())
    print("提示: 未找到 opus 原生库，已用替身放行（不影响本测试）\n")

from PyQt6.QtWidgets import QApplication, QMessageBox

# 查更新会真的发网络请求，而弹出来的那个 QMessageBox 用的是 exec()——离屏模式下
# 它照样会一直等人点，整个冒烟测试就挂死在那里。这里一律换成"没有新版"；
# 真正的对话框逻辑另有一条用例单独测。
import update
update.check = lambda *args, **kwargs: None

import gui

# 弹出的提示框是模态的，离屏也一样会一直等人点。测试里把它们换成记录调用。
_dialogs = []
for _name in ("warning", "critical", "information"):
    setattr(QMessageBox, _name,
            staticmethod(lambda *args, _n=_name: _dialogs.append((_n, args[2] if len(args) > 2 else ""))))

# 校验类提示改用了 InfoBar（不模态，从右上角滑出来自己消失）。它不会挂住测试，
# 但要能断言"确实提示了"，所以同样记一笔。
gui.ControllerWindow.warn = lambda self, title, content: _dialogs.append(
    ("infobar", content))


def main():
    app = QApplication(sys.argv)
    gui.apply_theme()          # 和 __main__ 里一样，主题要在建窗口之前设
    failures = []

    def check(name, fn):
        try:
            fn()
            print(f"  ok   {name}")
        except Exception as e:
            failures.append((name, e))
            print(f"  FAIL {name}: {type(e).__name__}: {e}")

    # 冒烟测试不能弹校准框（dialog.open() 在离屏环境里没人点）；
    # 对话框本身用假输入源单独建一次。
    import calibration
    import numpy as np
    calibration.input_device_name = lambda index: None

    class FakeSource:
        rate = 48000
        chunk = 960

        def read_available(self):
            return np.zeros(0, dtype=np.int16)

        def close(self):
            pass

    def calibration_dialog():
        for denoise_on in (False, True):
            dialog = calibration.CalibrationDialog(
                None, "Fake Mic", denoise_on,
                source_factory=lambda index: FakeSource())
            dialog.start()
            dialog._tick()
            dialog._finish()                   # 全静音 → too_short，不应抛异常
            assert dialog.entry() is None
            dialog.reject()

    check("校准对话框", calibration_dialog)

    print("主窗口：")
    window = gui.ControllerWindow()
    # 语言必须钉在**建完窗口之后**，否则断言会跟着开发机的系统语言飘：
    # ControllerWindow.__init__ 自己会按 设置/系统语言 调一次 set_language，
    # 在它之前设的会被它盖掉（英文系统上卡片就是拿英文建出来的）。
    gui.i18n.set_language("zh")
    window.retranslate()
    check("建立主窗口", lambda: window)
    check("切到电台栈页", lambda: window.pages.setCurrentIndex(1))

    print("电台栈：")

    def add_two():
        window.freq_input.setText("118.000")
        window.callsign_input.setText("zspd_twr")
        window.add_radio()
        window.freq_input.setText("121.700")
        window.add_radio()
        assert len(window.stack) == 2, "应该有两部电台"
        assert len(window.rows) == 2, "应该画出两行"
    check("添加两个频率", add_two)

    def callsign_is_upper_and_shown():
        # 照 TrackAudio 的排法，频率在上（等宽大字）、呼号在下（小字、暗色）
        row = window.rows[118000]
        assert "118.000" in row.freq_label.text(), row.freq_label.text()
        assert row.callsign_label.text() == "ZSPD_TWR", row.callsign_label.text()
    check("呼号大写并显示", callsign_is_upper_and_shown)

    def reject_duplicate():
        _dialogs.clear()
        window.freq_input.setText("118.000")
        window.add_radio()
        assert len(window.stack) == 2, "重复频率不该被加进来"
        assert _dialogs, "应该给出提示"
    check("重复频率被拒并提示", reject_duplicate)

    def reject_bad_frequency():
        _dialogs.clear()
        window.freq_input.setText("八八八")
        window.add_radio()
        assert len(window.stack) == 2
        assert _dialogs, "应该给出提示"
    check("非法频率被拒并提示", reject_bad_frequency)

    check("点 RX", lambda: window.toggle_rx(118000))
    check("点 TX", lambda: window.toggle_tx(118000))
    check("点 XC", lambda: window.toggle_xc(118000))
    check("TX 连带打开 RX", lambda: (_ for _ in ()).throw(AssertionError("rx 没打开"))
          if not window.stack.get(118000).rx else None)

    check("选主频率", lambda: window.select_radio(121700))
    check("音量", lambda: window.set_volume(118000, 40))
    check("静音", lambda: window.set_muted(118000, True))

    print("状态配色（采真实渲染出来的像素）：")

    def rx_tx_colours_are_actually_painted():
        """RX/TX 开没开、正不正在响，必须真的画出不同的颜色。

        这条不是多余的：改用 qfluentwidgets 时 RX/TX/XC 的高亮整个失效过一次
        ——setStyleSheet 设上了，却被库自己的 FluentStyleSheet 重新 apply 盖掉，
        三个开关一律灰色。而当时上面那二十多项冒烟**全绿**，因为它们只验证
        控件建得出来、信号连得上。管制员一眼看不出哪个频率在响，是这个界面
        最核心的信息丢了，所以这里直接采像素。
        """
        from PyQt6.QtCore import QPoint
        window.resize(760, 560)
        window.pages.setCurrentIndex(1)
        window.show()
        app.processEvents()

        khz = sorted(r.frequency_khz for r in window.stack)[0]
        row = window.rows[khz]
        radio = window.stack.get(khz)

        def fill(button):
            image = window.grab().toImage()
            centre = button.rect().center()
            # 采左内侧，正中间是文字，会把白色笔画混进来
            point = button.mapTo(window, QPoint(centre.x() - 18, centre.y()))
            return image.pixelColor(point).name()

        radio.muted = False       # 静音会把 RX 整个染红，会盖掉要测的三态
        radio.rx = False
        row.refresh(radio)
        app.processEvents()
        off = fill(row.rx_button)

        radio.rx = True
        row.refresh(radio)
        app.processEvents()
        on = fill(row.rx_button)

        radio.currently_rx = True
        row.refresh(radio)
        app.processEvents()
        active = fill(row.rx_button)

        assert on != off, f"RX 开和关画出来是同一个颜色（{on}）"
        assert on == gui.ON_COLOR, f"RX 开的颜色是 {on}，应当是 {gui.ON_COLOR}"
        assert active != on, f"正在收听时没有变亮（都是 {on}）"
        radio.currently_rx = False
        row.refresh(radio)
    check("RX 三态画出来确实不同色", rx_tx_colours_are_actually_painted)

    print("运行时状态：")
    check("收到话音", lambda: window.on_voice_rx(118000, True, "CES2345"))
    check("话音结束", lambda: window.on_voice_rx(118000, False, ""))
    check("最后通话已记录", lambda: (_ for _ in ()).throw(AssertionError(
        window.rows[118000].last_rx.text()))
        if "CES2345" not in window.rows[118000].last_rx.text() else None)
    check("PTT 亮起", lambda: window.on_voice_tx(True))
    check("PTT 熄灭", lambda: window.on_voice_tx(False))
    check("连接状态回报", lambda: window.on_voice_state("online", "测试"))

    def disconnect_indicator():
        window.on_voice_rx(118000, True, "CES2345")     # 先弄成"正在通话"
        window.on_connection_change(False)
        assert window.conn_label.text() == gui.t("main.disconnected"),             window.conn_label.text()
        assert not window.stack.get(118000).currently_rx, "掉线后不该还停在正在通话"
    check("掉线指示并清掉收发状态", disconnect_indicator)
    check("恢复连接", lambda: window.on_connection_change(True))

    print("数据源：席位频率与呼号")

    def feed(controllers=(), pilots=()):
        return {"general": {}, "pilots": list(pilots),
                "controllers": list(controllers), "atis": []}

    def adopts_my_position_frequency():
        """在网上上了席位，语音这边不该还要手动敲一遍频率。"""
        window.cid = "1000"
        before = len(window.stack)
        window.on_datafeed(feed(controllers=[{
            "cid": "1000", "callsign": "ZSPD_APP",
            "frequency": "125.900", "facility": 5}]))
        assert len(window.stack) == before + 1, "没有把席位频率加进来"
        radio = window.stack.get(125900)
        assert radio is not None and radio.callsign == "ZSPD_APP", radio
    check("上了席位自动加频率", adopts_my_position_frequency)

    def my_own_frequency_defaults_to_rx_and_tx():
        """上了席位本来就是要在这个频率上收发的，不该还要手动点两下。

        漏点了不会报错，只是喊了半天没人应。
        """
        radio = window.stack.get(125900)
        assert radio.rx, "自己的席位频率没有默认开 RX"
        assert radio.tx, "自己的席位频率没有默认开 TX"
        assert window.stack.selected_khz == 125900, "自己的席位频率不是主频率"
    check("自己的频率默认 RX+TX", my_own_frequency_defaults_to_rx_and_tx)

    def a_manual_tx_off_is_not_undone():
        """管制员自己把 TX 关掉之后，下一轮刷新不该又给他打开。"""
        window.stack.set_tx(125900, False)
        window.on_datafeed(feed(controllers=[{
            "cid": "1000", "callsign": "ZSPD_APP",
            "frequency": "125.900", "facility": 5}]))
        assert not window.stack.get(125900).tx, "刷新之后 TX 又被打开了"
        window.stack.set_tx(125900, True)      # 复原，后面的用例还要用
    check("手动关掉的 TX 不会被刷回来", a_manual_tx_off_is_not_undone)

    def does_not_add_it_twice():
        before = len(window.stack)
        window.on_datafeed(feed(controllers=[{
            "cid": "1000", "callsign": "ZSPD_APP",
            "frequency": "125.900", "facility": 5}]))
        assert len(window.stack) == before, "同一个席位被重复加了"
    check("不会重复加", does_not_add_it_twice)

    def the_staffed_frequency_cannot_be_removed():
        """正在管的席位频率不许删——手滑删掉等于把自己从工作频率上摘下去。"""
        window.remove_radio(125900)
        assert window.stack.get(125900) is not None, "自己的席位频率被删掉了"
    check("自己的席位频率删不掉", the_staffed_frequency_cannot_be_removed)

    def respects_a_manual_removal():
        """不在管的频率，用户手动删掉之后别每 60 秒跟他抢一次。"""
        window.on_datafeed(feed(controllers=[]))        # 先下席位，解锁
        window.remove_radio(125900)
        window.on_datafeed(feed(controllers=[{
            "cid": "1000", "callsign": "ZSPD_APP",
            "frequency": "125.900", "facility": 5}]))
        assert window.stack.get(125900) is None, "用户删掉的频率又被加回来了"
    check("用户删掉就不再自动加", respects_a_manual_removal)

    def off_duty_means_receive_only():
        """数据源上没有自己的席位时，只能收不能发。"""
        window.on_datafeed(feed(controllers=[]))
        assert not window.stack.transmit_allowed, "下了席位还允许发信"
        window.stack.set_tx(118000, True)
        assert not window.stack.get(118000).tx, "下了席位还能打开 TX"
    check("没上席位只能收", off_duty_means_receive_only)

    def going_off_duty_drops_tx():
        """已经开着的 TX 要落下来，光把按钮画灰拦不住 VoiceTarget。"""
        window.on_datafeed(feed(controllers=[{
            "cid": "1000", "callsign": "ZSPD_APP",
            "frequency": "125.900", "facility": 5}]))
        window.stack.set_tx(118000, True)
        assert window.stack.get(118000).tx, "前提：上着席位时能开 TX"
        window.on_datafeed(feed(controllers=[]))
        assert window.stack.tx_frequencies() == [], "下席位之后 TX 没有落下"
    check("下席位把 TX 落下", going_off_duty_drops_tx)

    def online_list_shows_every_position():
        window.on_datafeed(feed(controllers=[
            {"cid": "2001", "callsign": "ZBAA_TWR",
             "frequency": "118.500", "facility": 4},
            {"cid": "2002", "callsign": "ZSSS_GND",
             "frequency": "121.900", "facility": 4}]))
        shown = [c for c, _, _ in window.online]
        assert shown == ["ZBAA_TWR", "ZSSS_GND"], shown
        assert window._online_buttons, "在线频率一栏是空的"
    check("显示所有在线频率", online_list_shows_every_position)

    def refreshing_does_not_pile_up():
        """每分钟刷一次，控件和布局项都不能越堆越多。"""
        data = feed(controllers=[
            {"cid": "2001", "callsign": "ZBAA_TWR",
             "frequency": "118.500", "facility": 4},
            {"cid": "2002", "callsign": "ZSSS_GND",
             "frequency": "121.900", "facility": 4}])
        window.on_datafeed(data)
        first = window.online_box.count()
        for _ in range(3):
            window.on_datafeed(data)
        assert window.online_box.count() == first, (
            f"刷了几轮之后布局项从 {first} 变成 {window.online_box.count()}")
    check("反复刷新不堆积", refreshing_does_not_pile_up)

    def only_the_callsign_is_shown():
        """按钮上写不下"呼号 + 频率"，实测被压成 "ZBAA ...8.500"。"""
        window.on_datafeed(feed(controllers=[
            {"cid": "2001", "callsign": "ZBAA_TWR",
             "frequency": "118.500", "facility": 4}]))
        texts = [b.text() for b in window._online_buttons if hasattr(b, "text")]
        assert "ZBAA_TWR" in texts, texts
        assert not any("118.500" in x for x in texts), texts
    check("按钮只写呼号", only_the_callsign_is_shown)

    def clicking_an_online_frequency_adds_it():
        before = len(window.stack)
        window.add_online_frequency(118500, "ZBAA_TWR")
        assert len(window.stack) == before + 1, "点了没加进来"
        assert window.stack.get(118500).callsign == "ZBAA_TWR"
        window.stack.remove(118500)
    check("点在线频率就加进栈", clicking_an_online_frequency_adds_it)

    def ignores_the_no_frequency_placeholder():
        window.on_datafeed(feed(controllers=[{
            "cid": "1000", "callsign": "ZSPD_DEL",
            "frequency": "199.998", "facility": 5}]))
        assert window.stack.get(199998) is None, "199.998 是占位，不能当频率用"
    check("199.998 不当频率", ignores_the_no_frequency_placeholder)

    def resolves_cid_to_callsign():
        """语音层报上来的是纯数字 CID，界面上要显示成呼号。"""
        khz = sorted(r.frequency_khz for r in window.stack)[0]
        window.on_voice_rx(khz, True, "2001")          # Mumble 用户名 = CAN 号
        window.on_datafeed(feed(pilots=[
            {"cid": "2001", "callsign": "CES2345", "name": "某人"}]))
        text = window.rows[khz].last_rx.text()
        assert "CES2345" in text, text
        assert "2001" not in text, f"还在显示 CID：{text}"
    check("CID 显示成呼号", resolves_cid_to_callsign)

    def unknown_cid_still_shows_something():
        khz = sorted(r.frequency_khz for r in window.stack)[0]
        window.on_voice_rx(khz, True, "9999")          # 不在数据源里
        text = window.rows[khz].last_rx.text()
        assert "9999" in text, f"查不到呼号时至少要显示 CID：{text}"
    check("查不到呼号就显示 CID", unknown_cid_still_shows_something)

    def a_dead_datafeed_changes_nothing():
        before = len(window.stack)
        window.on_datafeed(None)          # 取不到就是 None
        assert len(window.stack) == before
    check("数据源挂了不影响别的", a_dead_datafeed_changes_nothing)

    print("多语言：")

    def switching_language_changes_the_visible_text():
        """换语言之后界面上的字要当场变。

        只改设置、要重启才生效的话，用户第一反应是"是不是没保存"。
        """
        gui.i18n.set_language("zh")
        window.retranslate()
        chinese = window.add_button.text()
        gui.i18n.set_language("en")
        window.retranslate()
        english = window.add_button.text()
        assert chinese != english, f"换了语言但界面没变（都是 {chinese}）"
        assert english == "Add", english
        # 占位符类的也要跟着变
        assert window.freq_input.placeholderText() != "频率，例如 118.000"
        gui.i18n.set_language("zh")
        window.retranslate()
        assert window.add_button.text() == "添加频率"
    check("换语言当场生效", switching_language_changes_the_visible_text)

    def radio_cards_are_retranslated_too():
        """电台卡片上的字也得跟着换。

        卡片是**复用**的（rebuild_rows 对已有的行只调 refresh），所以按钮上的
        文字和悬停提示这类"建的时候设一次"的字，不主动重刷就会一直停在旧语言上
        ——切到英文之后卡片上还写着"静音"。
        """
        khz = sorted(r.frequency_khz for r in window.stack)[0]
        row = window.rows[khz]
        gui.i18n.set_language("en")
        window.retranslate()
        assert "最后通话" not in row.last_rx.text(), f"卡片没重译: {row.last_rx.text()}"
        assert row.mute_button.text() == "Mute", \
            f"静音按钮没跟着换语言: {row.mute_button.text()!r}"
        assert "Receive" in row.rx_button.toolTip(), \
            f"RX 悬停提示没跟着换语言: {row.rx_button.toolTip()!r}"
        assert "Remove" in row.remove_button.toolTip(), \
            f"移除的悬停提示没跟着换语言: {row.remove_button.toolTip()!r}"
        gui.i18n.set_language("zh")
        window.retranslate()
        assert row.mute_button.text() == "静音", row.mute_button.text()
    check("电台卡片也重译", radio_cards_are_retranslated_too)

    def status_bar_follows_the_language():
        """状态栏那条也要跟着换语言。

        原来这里测的是"换语言不能把频道监听的警告抹成就绪"，那条警告已经去掉了，
        剩下的只有"就绪"，那就退回来盯它本身有没有跟着翻。
        """
        window.voice = mock.MagicMock()
        gui.i18n.set_language("en")
        window.retranslate()
        assert window.status_label.text() == "Ready", window.status_label.text()
        gui.i18n.set_language("zh")
        window.retranslate()
        assert window.status_label.text() == "就绪", window.status_label.text()
        window.voice = None
    check("状态栏跟着换语言", status_bar_follows_the_language)

    def cards_are_shown_in_frequency_order():
        """卡片的顺序要和电台栈一致。

        栈是按频率排过序的（radiostack.add 每次都 sort），而卡片一律往流式布局
        末尾追加的话，后加的低频率会排到高频率后面——屏幕上的频率就不是升序了，
        而这个界面就是拿来扫视的。
        """
        window.freq_input.setText("119.000")     # 排序上落在已有两个频率中间
        window.add_radio()
        layout = window.stack_layout
        on_screen = [layout.itemAt(i).widget().khz for i in range(layout.count())]
        assert on_screen == sorted(on_screen), \
            f"屏幕上的频率不是升序: {on_screen}"
        assert on_screen == [r.frequency_khz for r in window.stack], \
            f"卡片顺序和电台栈对不上: {on_screen}"
        window.remove_radio(119000)
    check("卡片按频率升序排列", cards_are_shown_in_frequency_order)

    def every_key_used_by_the_window_exists():
        """漏定义的键会在界面上直接显示成 key，这里提前抓出来。"""
        missing = [key for key in gui.i18n.TEXT
                   if not gui.i18n.TEXT[key].get("zh")]
        assert not missing, missing
        # 界面上任何一处都不该出现裸的 key（形如 a.b）
        for widget_text in (window.add_button.text(), window.top_button.text(),
                            window.settings_button.text(),
                            window.empty_hint.text()):
            assert "." not in widget_text or " " in widget_text, \
                f"这看起来是没翻出来的键: {widget_text}"
    check("界面上没有漏翻的键", every_key_used_by_the_window_exists)

    print("窗口置顶：")

    def toggle_puts_the_flag_on_and_off():
        from PyQt6.QtCore import Qt as _Qt
        flag = _Qt.WindowType.WindowStaysOnTopHint
        window.toggle_always_on_top(True)
        assert bool(window.windowFlags() & flag), "置顶标志没设上"
        assert window.settings.always_on_top is True
        window.toggle_always_on_top(False)
        assert not (window.windowFlags() & flag), "取消置顶没生效"
        assert window.settings.always_on_top is False
    check("开关都生效", toggle_puts_the_flag_on_and_off)

    def the_window_is_still_visible_afterwards():
        """Windows 上改 window flag 会把窗口隐藏掉。

        忘了补 show() 的话，用户点一下置顶整个窗口就消失了——而且不会有任何
        报错，看起来就像程序崩了。
        """
        window.show()
        app.processEvents()
        window.toggle_always_on_top(True)
        app.processEvents()
        assert not window.isHidden(), "点了置顶之后窗口不见了"
        window.toggle_always_on_top(False)
        app.processEvents()
        assert not window.isHidden(), "取消置顶之后窗口不见了"
    check("切换之后窗口还在", the_window_is_still_visible_afterwards)

    def maximised_state_survives():
        """show() 会把最大化状态丢掉，最大化的人一点置顶窗口就缩回去了。"""
        window.showMaximized()
        app.processEvents()
        if not window.isMaximized():
            return                    # 离屏平台不一定支持最大化，跳过
        window.toggle_always_on_top(True)
        app.processEvents()
        assert window.isMaximized(), "置顶之后窗口从最大化缩回去了"
        window.toggle_always_on_top(False)
        window.showNormal()
    check("最大化状态不丢", maximised_state_survives)

    def the_setting_is_remembered():
        window.toggle_always_on_top(True)
        from settings import Settings
        assert Settings().always_on_top is True, "置顶状态没有存下来"
        window.toggle_always_on_top(False)
        assert Settings().always_on_top is False
    check("重启后还记得", the_setting_is_remembered)

    print("精简模式：")

    def compact_hides_everything_but_the_cards():
        window.toggle_compact(True)
        for name in ("add_bar", "online_bar", "bottom_bar", "conn_label",
                     "session_label", "settings_button", "disconnect_button"):
            assert not getattr(window, name).isVisible(), f"{name} 没有收起来"
        assert window.stack_area.isVisible(), "电台卡片被一起藏掉了"
    check("精简只留卡片", compact_hides_everything_but_the_cards)

    def compact_actually_tightens_the_layout():
        """光藏控件不够——留着正常模式的边距和带文字的大按钮，一张卡的窗口
        里有一半是空的，那就失去精简的意义了。"""
        import gui as gui_module
        margins = window.main_layout.contentsMargins()
        assert margins.left() == gui_module.COMPACT_MARGINS[0], margins.left()
        assert window.main_layout.spacing() == gui_module.COMPACT_SPACING
        assert window.top_button.text() == "", "精简时按钮上还留着文字"
        assert window.top_button.width() <= 40, window.top_button.width()
    check("精简把留白也收紧", compact_actually_tightens_the_layout)

    def there_is_always_a_way_back():
        """顶栏不能整条藏——藏完就没有任何路径切回去了。"""
        assert window.compact_button.isVisible(), "切回去的开关自己也被藏了"
        assert window.top_button.isVisible(), "置顶开关被藏了"
    check("精简后仍能切回", there_is_always_a_way_back)

    def expanding_restores_everything():
        window.toggle_compact(False)
        for name in ("add_bar", "online_bar", "bottom_bar", "conn_label",
                     "session_label", "settings_button", "disconnect_button"):
            assert getattr(window, name).isVisible(), f"{name} 没有恢复"
    check("展开全部恢复", expanding_restores_everything)

    def the_state_is_remembered():
        from settings import Settings
        window.toggle_compact(True)
        assert Settings().compact is True, "精简状态没有存下来"
        window.toggle_compact(False)
        assert Settings().compact is False
    check("精简状态记得住", the_state_is_remembered)

    print("持久化：")

    def the_stack_is_not_persisted():
        """频率不跨会话保留：上一场的临时频道多半早就没人了。"""
        from settings import Settings
        window.settings.save_settings()
        assert not hasattr(Settings(), "radios") or not Settings().radios, \
            "电台栈被写进设置了"
    check("电台栈不写进设置", the_stack_is_not_persisted)

    def a_fresh_window_starts_empty():
        import json
        import os
        path = "radio_settings.json"
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                assert "radios" not in json.load(f), "设置文件里还有 radios"
    check("设置文件里没有频率", a_fresh_window_starts_empty)

    print("移除与设置：")
    check("移除一个频率", lambda: window.remove_radio(118000))
    check("行也跟着消失", lambda: (_ for _ in ()).throw(AssertionError(len(window.rows)))
          if len(window.rows) != 1 else None)

    from settings import SettingsDialog
    import ptt
    dialog = SettingsDialog(window.settings, window)
    check("建立设置对话框", lambda: dialog)
    check("PTT 绑定列表", lambda: dialog.ptt_list.bindings)

    def a_binding_can_be_added_and_removed():
        """录制那条路要真的走一遍：它是唯一能加绑定的入口。"""
        added = ptt.Binding(ptt.MOUSE, button="x2")
        before = len(dialog.ptt_list.bindings)
        dialog.ptt_list.on_captured(added)      # 模拟录到了鼠标侧键
        assert dialog.ptt_list.bindings[-1] == added, "绑定没加进去"
        dialog.ptt_list.on_captured(added)      # 重复的一条不该再加一遍
        assert len(dialog.ptt_list.bindings) == before + 1, "重复绑定被加了两次"
        dialog.ptt_list.remove(added)
        assert added not in dialog.ptt_list.bindings, "绑定没移除掉"
    check("加/删一条 PTT 绑定", a_binding_can_be_added_and_removed)

    def an_empty_list_still_builds():
        """一条绑定都没有时也得能画出来——这时界面上是一句"PTT 用不了"。"""
        dialog.ptt_list.bindings = []
        dialog.ptt_list.rebuild()
    check("绑定清空后仍能重画", an_empty_list_still_builds)

    def settings_without_rnnoise():
        import denoise
        original = denoise.available
        denoise.available = lambda: False
        try:
            dialog = gui.SettingsDialog(window.settings, window)
            assert not dialog.denoise_checkbox.isEnabled()
            dialog.reject()
        finally:
            denoise.available = original

    check("设置对话框（降噪不可用）", settings_without_rnnoise)

    def calibration_prompt_only_when_device_or_denoise_changed():
        """保存设置之后，只有真的换了输入设备或切了降噪才该再问一次校准——
        单纯换个语言之类的保存不该每次都弹一次校准框。"""
        cases = [
            # (old_device, old_denoise, new_device, new_denoise, expected)
            (None, True, None, True, False),      # 什么都没变
            (None, True, 3, True, True),           # 换了设备
            (None, True, None, False, True),       # 切了降噪
            (None, False, 3, True, True),          # 两个都变了
        ]
        for old_device, old_denoise, new_device, new_denoise, expected in cases:
            got = gui.calibration_state_changed(old_device, old_denoise,
                                                new_device, new_denoise)
            assert got == expected, (
                f"calibration_state_changed({old_device!r}, {old_denoise!r}, "
                f"{new_device!r}, {new_denoise!r}) = {got}, expected {expected}")
    check("保存设置后只在设备或降噪变了才提示校准",
          calibration_prompt_only_when_device_or_denoise_changed)

    window.remove_radio(121700)
    window.ptt_watcher.stop()

    print()
    if failures:
        print(f"{len(failures)} 项失败")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
