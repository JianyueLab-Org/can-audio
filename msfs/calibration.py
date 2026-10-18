"""麦克风音量自动校准对话框。

先录 1 秒静音测底噪，再录 8 秒朗读，交给 micgain 算基准增益。对话框自己开
一个输入流，采样率和帧长与发送链路一致（20 ms 一帧）；开之前调用方要把
PTT 监听停掉，校准期间不能发话。

降噪开着时每帧先过 RNNoise，测的是实际发出去的信号；削波仍按原始信号判。

用 QTimer 每 20 ms 取一次已到的数据，全在界面线程里，不起线程。
"""

import logging
import time

import numpy as np
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QDialog, QHBoxLayout, QVBoxLayout
from qfluentwidgets import (BodyLabel, CaptionLabel, PrimaryPushButton, ProgressBar,
                            PushButton, StrongBodyLabel)

import denoise
import micgain
import theme
from i18n import t

log = logging.getLogger("calibration")

RATES = (48000, 44100, 32000, 24000, 16000)
QUIET_SECONDS = 1.0
READ_SECONDS = 8.0
METER_FLOOR_DBFS = -60.0

FAIL_KEYS = {
    "clipping": "calib.fail_clipping",
    "too_short": "calib.fail_too_short",
    "too_noisy": "calib.fail_too_noisy",
}


def input_device_name(index):
    """输入设备的真实名字；index 为 None 时取系统默认输入设备。取不到返回 None。"""
    try:
        import pyaudio
        audio = pyaudio.PyAudio()
    except Exception as e:
        log.warning(f"could not start PyAudio to resolve the input device: {e}")
        return None
    try:
        info = (audio.get_default_input_device_info() if index is None
                else audio.get_device_info_by_index(index))
        if not info.get("maxInputChannels"):
            return None
        name = info.get("name")
        return name if isinstance(name, str) and name else None
    except Exception as e:
        log.warning(f"could not resolve input device {index}: {e}")
        return None
    finally:
        audio.terminate()


class PyAudioSource:
    """真实麦克风。采样率按发送链路的顺序挑第一个能开的。"""

    def __init__(self, device_index):
        import pyaudio
        self._audio = pyaudio.PyAudio()
        self._stream = None
        last_error = None
        for rate in RATES:
            chunk = int(rate * 0.02)
            try:
                self._stream = self._audio.open(
                    format=pyaudio.paInt16, channels=1, rate=rate, input=True,
                    frames_per_buffer=chunk, input_device_index=device_index)
            except Exception as e:
                last_error = e
                continue
            self.rate = rate
            self.chunk = chunk
            return
        self._audio.terminate()
        raise last_error or RuntimeError("no usable sample rate")

    def read_available(self):
        available = self._stream.get_read_available()
        available -= available % self.chunk
        if available <= 0:
            return np.zeros(0, dtype=np.int16)
        data = self._stream.read(available, exception_on_overflow=False)
        return np.frombuffer(data, dtype=np.int16)

    def close(self):
        try:
            self._stream.stop_stream()
            self._stream.close()
        except Exception as e:
            log.warning(f"closing the calibration stream raised: {e}")
        self._audio.terminate()


class CalibrationDialog(QDialog):

    def __init__(self, device_index, device_name, denoise_on, parent=None,
                 source_factory=None, denoiser_factory=None):
        super().__init__(parent)
        self.device_index = device_index
        self.device_name = device_name
        self.denoise_on = bool(denoise_on)
        self._source_factory = source_factory or PyAudioSource
        self._denoiser_factory = denoiser_factory or denoise.Denoiser
        self._source = None
        self._denoiser = None
        self._stage = None
        self._started = 0.0
        self._noise = []
        self._reading = []
        self._raw = []
        self._entry = None
        self.setWindowTitle(t("calib.title"))
        self.setStyleSheet(theme.dialog_qss())
        self.setMinimumWidth(460)

        self.timer = QTimer(self)
        self.timer.setInterval(20)
        self.timer.timeout.connect(self._tick)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.addWidget(StrongBodyLabel(t("calib.title")))
        device = CaptionLabel(t("calib.device", name=self.device_name))
        device.setStyleSheet(f"color: {theme.IDLE_COLOR};")
        layout.addWidget(device)
        intro = BodyLabel(t("calib.intro"))
        intro.setWordWrap(True)
        layout.addWidget(intro)
        phrase = StrongBodyLabel(t("calib.phrase"))
        phrase.setWordWrap(True)
        layout.addWidget(phrase)
        self.meter = ProgressBar()
        self.meter.setRange(0, 100)
        self.meter.setValue(0)
        layout.addWidget(self.meter)
        self.status = BodyLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        buttons = QHBoxLayout()
        self.start_button = PrimaryPushButton(t("calib.start"))
        self.start_button.clicked.connect(self.start)
        self.save_button = PrimaryPushButton(t("calib.save"))
        self.save_button.setEnabled(False)
        self.save_button.clicked.connect(self.accept)
        cancel = PushButton(t("calib.cancel"))
        cancel.clicked.connect(self.reject)
        buttons.addStretch()
        buttons.addWidget(cancel)
        buttons.addWidget(self.start_button)
        buttons.addWidget(self.save_button)
        layout.addLayout(buttons)

    def entry(self):
        return self._entry

    # ---------- 录音 ----------
    def start(self):
        self._stop()
        self._entry = None
        self.save_button.setEnabled(False)
        try:
            self._source = self._source_factory(self.device_index)
        except Exception as e:
            log.warning(f"calibration could not open {self.device_name}: {e}")
            self._show(t("calib.open_failed", error=e), theme.MUTED_COLOR)
            return
        self._denoiser = (self._denoiser_factory(self._source.rate)
                          if self.denoise_on else None)
        self._noise, self._reading, self._raw = [], [], []
        self._stage = "quiet"
        self._started = time.monotonic()
        self.start_button.setEnabled(False)
        self._show(t("calib.stage_quiet"), theme.IDLE_COLOR)
        self.timer.start()

    def _tick(self):
        if self._source is None:
            return
        try:
            samples = self._source.read_available()
        except Exception as e:
            log.warning(f"calibration read failed: {e}")
            samples = np.zeros(0, dtype=np.int16)
        chunk = self._source.chunk
        for i in range(0, len(samples) - chunk + 1, chunk):
            raw = samples[i:i + chunk]
            frame = self._denoiser.process(raw) if self._denoiser else raw
            if self._stage == "quiet":
                self._noise.append(frame)
            else:
                self._reading.append(frame)
                self._raw.append(raw)
            level = micgain.frame_rms_dbfs(frame)
            self.meter.setValue(int(max(0.0, min(100.0,
                (level - METER_FLOOR_DBFS) / -METER_FLOOR_DBFS * 100.0))))
        elapsed = time.monotonic() - self._started
        if self._stage == "quiet" and elapsed >= QUIET_SECONDS:
            self._stage = "read"
            self._show(t("calib.stage_read"), theme.ON_COLOR)
        elif self._stage == "read" and elapsed >= QUIET_SECONDS + READ_SECONDS:
            self._finish()

    def _finish(self):
        rate = self._source.rate if self._source else 48000
        self._stop()
        m = micgain.measure(self._reading, self._noise, rate, clip_frames=self._raw)
        result = micgain.compute_gain(m)
        self.start_button.setText(t("calib.retry"))
        self.start_button.setEnabled(True)
        self.meter.setValue(0)
        state = "on" if self.denoise_on else "off"
        if result.reason:
            log.info(f"calibration rejected on {self.device_name} (denoise {state}): "
                     f"{result.reason} (speech {m.speech_dbfs:.1f} dBFS, noise "
                     f"{m.noise_dbfs:.1f} dBFS, {m.speech_seconds:.1f} s of speech, "
                     f"{m.clip_ratio:.1%} clipped)")
            self._show(t(FAIL_KEYS[result.reason]), theme.MUTED_COLOR)
            return
        log.info(f"calibration accepted on {self.device_name} (denoise {state}): gain "
                 f"{result.gain_db:+.1f} dB (speech {m.speech_dbfs:.1f} dBFS, noise "
                 f"{m.noise_dbfs:.1f} dBFS)")
        self._entry = micgain.calibration_entry(m, result.gain_db, self.denoise_on)
        self._show(t("calib.result", db=f"{result.gain_db:+.1f}"), theme.ON_COLOR)
        self.save_button.setEnabled(True)

    def _show(self, text, color):
        self.status.setText(text)
        self.status.setStyleSheet(f"color: {color};")

    def _stop(self):
        self.timer.stop()
        self._stage = None
        source, self._source = self._source, None
        if source is not None:
            source.close()
        denoiser, self._denoiser = self._denoiser, None
        if denoiser is not None:
            denoiser.close()

    def reject(self):
        self._stop()
        super().reject()

    def accept(self):
        self._stop()
        super().accept()

    def closeEvent(self, event):
        self._stop()
        super().closeEvent(event)
