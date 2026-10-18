"""管制端的配置和设置对话框。

控件用 qfluentwidgets，和 gui.py 同一套——对话框原来是纯 QDialog + QPushButton，
在深色 Fluent 主界面上弹出来是一块浅色的方框，边框和字号也和主界面对不上。
QDialog 本身留着（qfluentwidgets 那套 MessageBoxBase 要一个遮罩父窗口，不适合
这种带一堆表单项的设置框），底色由 theme.dialog_qss() 铺。
"""

import json
import logging
import os

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QWidget
from qfluentwidgets import (BodyLabel, CaptionLabel, CheckBox, ComboBox, FluentIcon,
                            LineEdit, PrimaryPushButton, PushButton, Slider, StrongBodyLabel,
                            TransparentToolButton)

import applog
import calibration
import denoise
import i18n
import micgain
import ptt
import theme
import version
from i18n import t

log = logging.getLogger("settings")

# 新装的默认 PTT 键。老配置里的 ptt_key 会被升级成一条键盘绑定，见 ptt.load()。
DEFAULT_PTT_KEY = "v"
DEFAULT_MUMBLE_HOST = "audio.ceruleanavi.net"


class Settings:
    def __init__(self):
        self.config_file = "radio_settings.json"
        # PTT 现在是一串绑定（键盘 / 鼠标侧键 / 摇杆），任意一个按住即发话。
        # 原来的 ptt_key 单字段读得进来，见 load_settings()。
        self.ptt_bindings = [ptt.keyboard_binding(DEFAULT_PTT_KEY)]
        # 麦克风音量是乘在校准基准上的本次会话乘数：每次启动都是 100%，不落盘。
        self.mic_volume = 100
        # 设备名 → 校准记录，见 micgain.calibration_entry()。按名字存是因为
        # PyAudio 的设备序号跨启动不稳定。
        self.mic_calibration = {}
        self.mic_denoise = True
        self.speaker_volume = 100
        self.input_device_index = None
        self.output_device_index = None
        self.last_username = ""
        self.mumble_host = DEFAULT_MUMBLE_HOST
        # 电台栈**不存**。频率该从数据源来：上了席位的自动加，别人的席位在
        # "在线频率"里点。留着上一场的频率反而危险——那些临时频道多半早就没
        # 人了，屏幕上却看起来一切正常。老配置里的 radios 键读到也直接忽略。
        # 窗口置顶。管制员多半把语音压在雷达/模拟器上面用，这个开关要能记住
        self.always_on_top = False
        # 精简模式：只留电台卡片。和置顶是一对，同样要记住
        self.compact = False
        # 界面语言。空字符串表示"还没选过"，第一次启动跟系统走
        self.language = ""
        # 更新检查：启动时问一次 can 有没有新版，装不装由用户决定。
        # skipped_version 记住"这一版我不要"，免得每次启动再问一遍。
        self.update_check = True
        self.skipped_version = ""
        self.update_url = ""
        self.debug = False
        self.load_settings()

    def load_settings(self):
        try:
            if os.path.exists(self.config_file):
                with open(self.config_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    # 老配置里只有 ptt_key。升不上来的话，用户原来设的 PTT 键会在
                    # 升级之后悄悄失效——界面一切正常，只是没人听得见。
                    self.ptt_bindings = ptt.load(
                        data.get("ptt_bindings"),
                        legacy_key=data.get("ptt_key", DEFAULT_PTT_KEY))
                    stored_calibration = data.get("mic_calibration")
                    self.mic_calibration = stored_calibration if isinstance(stored_calibration, dict) else {}
                    self.mic_denoise = bool(data.get("mic_denoise", True))
                    self.speaker_volume = data.get("speaker_volume", 100)
                    self.input_device_index = data.get("input_device_index", None)
                    self.output_device_index = data.get("output_device_index", None)
                    self.last_username = data.get("last_username", "")
                    self.mumble_host = data.get("mumble_host") or DEFAULT_MUMBLE_HOST
                    self.always_on_top = bool(data.get("always_on_top", False))
                    self.compact = bool(data.get("compact", False))
                    self.language = data.get("language", "") or ""
                    self.update_check = bool(data.get("update_check", True))
                    self.skipped_version = str(data.get("skipped_version") or "")
                    self.update_url = str(data.get("update_url") or "")
                    self.debug = bool(data.get("debug", False))
        except Exception as e:
            log.warning(f"could not load the settings: {e}")

    def save_settings(self):
        try:
            data = {
                # ptt_key 不再写回去。留着它就有两个说了算的地方，而且它只能表达
                # 三种绑定里的一种，回头必然和实际用的那条对不上。
                "ptt_bindings": ptt.dump(self.ptt_bindings),
                "mic_calibration": self.mic_calibration,
                "mic_denoise": self.mic_denoise,
                "speaker_volume": self.speaker_volume,
                "input_device_index": self.input_device_index,
                "output_device_index": self.output_device_index,
                "last_username": self.last_username,
                "mumble_host": self.mumble_host,
                "always_on_top": self.always_on_top,
                "compact": self.compact,
                "language": self.language,
                "debug": self.debug,
            }
            with open(self.config_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception as e:
            log.warning(f"could not save the settings: {e}")

    def denoise_active(self):
        return denoise.effective(self.mic_denoise)

    def baseline_for(self, device_name):
        return micgain.baseline_db(self.mic_calibration, device_name,
                                   self.denoise_active())


class PttBindingList(QWidget):
    """PTT 绑定的编辑器：一行一条，底下一个"添加绑定"。

    添加走 ptt.PttCapture——按什么就是什么，不用让用户去猜自己的摇杆上那个扳机
    是几号按钮（原来 xpc 的设置里就是一个 0-63 的数字框，实际没人填得对）。

    **调用方必须在打开这个对话框之前把 PttWatcher 停掉**：录制要独占 SDL 的事件
    队列，而且录制时按下的那一下不该真的发出去一段语音。
    """

    captured = pyqtSignal(object)      # 录到的绑定，从监听线程转回界面线程

    def __init__(self, bindings, parent=None):
        super().__init__(parent)
        self.bindings = list(bindings)
        self._capture = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.title = StrongBodyLabel(t("settings.ptt_title"))
        self.hint = CaptionLabel(t("settings.ptt_hint"))
        self.hint.setStyleSheet(f"color: {theme.IDLE_COLOR};")
        layout.addWidget(self.title)
        layout.addWidget(self.hint)

        self.rows = QVBoxLayout()
        self.rows.setSpacing(4)
        layout.addLayout(self.rows)

        self.add_button = PushButton(FluentIcon.ADD, t("settings.ptt_add"))
        self.add_button.clicked.connect(self.toggle_capture)
        layout.addWidget(self.add_button)

        self.captured.connect(self.on_captured)
        self.rebuild()

    # ---------- 列表 ----------
    def rebuild(self):
        while self.rows.count():
            item = self.rows.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
            elif item.layout() is not None:
                self._clear_layout(item.layout())
        if not self.bindings:
            empty = BodyLabel(t("settings.ptt_none"))
            empty.setStyleSheet(f"color: {theme.MUTED_COLOR};")
            self.rows.addWidget(empty)
            return
        for binding in list(self.bindings):
            self.rows.addLayout(self._row(binding))

    def _row(self, binding):
        row = QHBoxLayout()
        row.setSpacing(6)
        label = BodyLabel(i18n.binding_label(binding))
        remove = TransparentToolButton(FluentIcon.DELETE)
        remove.setToolTip(t("settings.ptt_remove"))
        remove.clicked.connect(lambda _=False, b=binding: self.remove(b))
        row.addWidget(label)
        row.addStretch()
        row.addWidget(remove)
        return row

    @staticmethod
    def _clear_layout(layout):
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def remove(self, binding):
        self.bindings = [b for b in self.bindings if b != binding]
        self.rebuild()

    # ---------- 录制 ----------
    def toggle_capture(self):
        if self._capture is not None:
            self.stop_capture()
            return
        self.add_button.setText(t("settings.ptt_capturing"))
        # 录制期间必须能取消：用户可能只想看看，或者手边根本没有摇杆。按钮
        # 自己就是取消键，所以不能禁用它。
        self._capture = ptt.PttCapture(self.captured.emit)
        self._capture.start()

    def stop_capture(self):
        capture, self._capture = self._capture, None
        if capture is not None:
            capture.stop()
        self.add_button.setText(t("settings.ptt_add"))

    def on_captured(self, binding):
        """在界面线程上跑（captured 信号转过来的）。"""
        self.stop_capture()
        if binding in self.bindings:
            self.hint.setText(t("settings.ptt_duplicate"))
            self.hint.setStyleSheet(f"color: {theme.ACTIVE_COLOR};")
            return
        self.hint.setText(t("settings.ptt_hint"))
        self.hint.setStyleSheet(f"color: {theme.IDLE_COLOR};")
        self.bindings.append(binding)
        self.rebuild()


class SettingsDialog(QDialog):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle(t("settings.title"))
        self.setStyleSheet(theme.dialog_qss())
        self.setMinimumWidth(420)
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout()
        layout.setSpacing(10)

        host_layout = QHBoxLayout()
        self.mumble_host_input = LineEdit()
        self.mumble_host_input.setText(self.settings.mumble_host)
        host_layout.addWidget(BodyLabel(t("settings.mumble_host")))
        host_layout.addWidget(self.mumble_host_input, 1)
        layout.addLayout(host_layout)

        layout.addWidget(StrongBodyLabel(t("settings.volume")))

        mic_layout = QHBoxLayout()
        self.mic_slider = Slider(Qt.Orientation.Horizontal)
        self.mic_slider.setRange(0, 200)
        self.mic_slider.setValue(self.settings.mic_volume)
        self.mic_value = BodyLabel(f"{self.settings.mic_volume}%")
        self.mic_slider.valueChanged.connect(lambda v: self.mic_value.setText(f"{v}%"))
        mic_layout.addWidget(BodyLabel(t("settings.mic")))
        mic_layout.addWidget(self.mic_slider)
        mic_layout.addWidget(self.mic_value)
        layout.addLayout(mic_layout)

        calib_layout = QHBoxLayout()
        self.baseline_label = CaptionLabel("")
        self.baseline_label.setStyleSheet(f"color: {theme.IDLE_COLOR};")
        calib_button = PushButton(t("calib.button"))
        calib_button.clicked.connect(self.calibrate)
        calib_layout.addWidget(self.baseline_label)
        calib_layout.addStretch()
        calib_layout.addWidget(calib_button)
        layout.addLayout(calib_layout)

        self.denoise_checkbox = CheckBox(t("calib.denoise"))
        if denoise.available():
            self.denoise_checkbox.setChecked(self.settings.mic_denoise)
        else:
            self.denoise_checkbox.setChecked(False)
            self.denoise_checkbox.setEnabled(False)
            self.denoise_checkbox.setText(t("calib.denoise_unavailable"))
        layout.addWidget(self.denoise_checkbox)

        speaker_layout = QHBoxLayout()
        self.speaker_slider = Slider(Qt.Orientation.Horizontal)
        self.speaker_slider.setRange(0, 200)
        self.speaker_slider.setValue(self.settings.speaker_volume)
        self.speaker_value = BodyLabel(f"{self.settings.speaker_volume}%")
        self.speaker_slider.valueChanged.connect(
            lambda v: self.speaker_value.setText(f"{v}%"))
        speaker_layout.addWidget(BodyLabel(t("settings.speaker")))
        speaker_layout.addWidget(self.speaker_slider)
        speaker_layout.addWidget(self.speaker_value)
        layout.addLayout(speaker_layout)

        self.ptt_list = PttBindingList(self.settings.ptt_bindings)
        layout.addWidget(self.ptt_list)

        language_layout = QHBoxLayout()
        self.language_combo = ComboBox()
        for code, name in i18n.available().items():
            self.language_combo.addItem(name, userData=code)
        index = self._find_data(self.language_combo, i18n.current())
        if index >= 0:
            self.language_combo.setCurrentIndex(index)
        language_layout.addWidget(BodyLabel(t("settings.language")))
        language_layout.addWidget(self.language_combo)
        layout.addLayout(language_layout)

        input_layout = QHBoxLayout()
        self.input_combo = ComboBox()
        self.populate_audio_devices(self.input_combo, True)
        input_layout.addWidget(BodyLabel(t("settings.input")))
        input_layout.addWidget(self.input_combo)
        layout.addLayout(input_layout)
        self.input_combo.currentIndexChanged.connect(lambda _: self._refresh_baseline())
        self._refresh_baseline()

        output_layout = QHBoxLayout()
        self.output_combo = ComboBox()
        self.populate_audio_devices(self.output_combo, False)
        output_layout.addWidget(BodyLabel(t("settings.output")))
        output_layout.addWidget(self.output_combo)
        layout.addLayout(output_layout)

        # 日志：出问题时让用户能一键找到文件，而不是去解释路径
        log_layout = QHBoxLayout()
        self.debug_checkbox = CheckBox(t("settings.debug"))
        self.debug_checkbox.setChecked(self.settings.debug)
        self.debug_checkbox.setToolTip(t("settings.debug_tip"))
        open_log = PushButton(FluentIcon.FOLDER, t("settings.open_log"))
        open_log.clicked.connect(lambda: applog.open_log_folder())
        log_layout.addWidget(self.debug_checkbox)
        log_layout.addStretch()
        log_layout.addWidget(open_log)
        layout.addLayout(log_layout)

        # 连上之后登录页就看不见了，版本号在这里再露一次
        version_label = CaptionLabel(version.full())
        version_label.setStyleSheet(f"color: {theme.IDLE_COLOR};")
        layout.addWidget(version_label)

        button_layout = QHBoxLayout()
        save_button = PrimaryPushButton(t("settings.save"))
        save_button.clicked.connect(self.save_and_close)
        cancel_button = PushButton(t("settings.cancel"))
        cancel_button.clicked.connect(self.reject)
        button_layout.addStretch()
        button_layout.addWidget(cancel_button)
        button_layout.addWidget(save_button)
        layout.addLayout(button_layout)

        layout.addStretch()
        self.setLayout(layout)

    @staticmethod
    def _find_data(combo, value):
        """qfluentwidgets 的 ComboBox 没有 findData()。"""
        for i in range(combo.count()):
            if combo.itemData(i) == value:
                return i
        return -1

    def populate_audio_devices(self, combo_box, is_input):
        import pyaudio
        p = pyaudio.PyAudio()
        combo_box.addItem(t("settings.system_default"), userData=None)
        for i in range(p.get_device_count()):
            info = p.get_device_info_by_index(i)
            channels = 'maxInputChannels' if is_input else 'maxOutputChannels'
            if info.get(channels):
                combo_box.addItem(info.get('name'), userData=i)
        current = self.settings.input_device_index if is_input else self.settings.output_device_index
        if current is not None:
            index = self._find_data(combo_box, current)
            if index >= 0:
                combo_box.setCurrentIndex(index)
        p.terminate()

    def cleanup(self):
        """把录制线程停掉。

        对话框关了而录制还开着的话，pynput 的监听器会一直挂在那儿，用户之后
        随便按个键就会被录进一个已经不存在的对话框里。
        """
        self.ptt_list.stop_capture()

    def reject(self):
        self.cleanup()
        super().reject()

    def accept(self):
        self.cleanup()
        super().accept()

    def _refresh_baseline(self):
        name = calibration.input_device_name(self.input_combo.currentData())
        baseline = self.settings.baseline_for(name)
        self.baseline_label.setText(
            t("calib.uncalibrated") if baseline is None
            else t("calib.baseline", db=f"{baseline:+.1f}"))

    def calibrate(self):
        """校准下拉框里当前选中的输入设备，按设置里已保存的降噪状态测，结果立即落盘。"""
        index = self.input_combo.currentData()
        name = calibration.input_device_name(index)
        if name is None:
            self.baseline_label.setText(t("calib.no_device"))
            return
        dialog = calibration.CalibrationDialog(index, name,
                                               self.settings.denoise_active(), self)
        if dialog.exec() and dialog.entry():
            self.settings.mic_calibration[name] = dialog.entry()
            self.settings.save_settings()
        self._refresh_baseline()

    def save_and_close(self):
        self.settings.mumble_host = (self.mumble_host_input.text().strip()
                                    or DEFAULT_MUMBLE_HOST)
        self.settings.ptt_bindings = list(self.ptt_list.bindings)
        self.settings.mic_volume = self.mic_slider.value()
        self.settings.speaker_volume = self.speaker_slider.value()
        self.settings.input_device_index = self.input_combo.currentData()
        self.settings.output_device_index = self.output_combo.currentData()
        self.settings.debug = self.debug_checkbox.isChecked()
        if denoise.available():
            self.settings.mic_denoise = self.denoise_checkbox.isChecked()
        self.settings.language = self.language_combo.currentData()
        i18n.set_language(self.settings.language)
        self.settings.save_settings()
        self.accept()
