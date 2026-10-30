"""麦克风音量自动校准（无界面版，给 bridge.py 的 calibrate 命令用）。

流程同旧 GUI 版 calibration.py：先录 1 秒静音测底噪，再录 8 秒朗读，交给 micgain 算基准增益。
自己开一个输入流（采样率按发送链路的顺序挑第一个能开的，20 ms 一帧），在后台线程里阻塞读，
按读到的帧数切换阶段，不看墙钟——设备卡一下也不会少录。

降噪开着时每帧先过 RNNoise，测的是实际发出去的信号；削波仍按原始信号判。
进度和结果通过 on_event 回调发出，字段见 README 的 calibration 事件。
"""

import logging
import threading

import numpy as np

import denoise
import micgain

log = logging.getLogger("calibration")

RATES = (48000, 44100, 32000, 24000, 16000)
QUIET_SECONDS = 1.0
READ_SECONDS = 8.0
LEVEL_EVERY = 3          # 每 3 帧（60 ms）报一次电平


class PyAudioSource:
    """真实麦克风。device_index 为 None 时用系统默认输入设备。"""

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

    def read(self):
        data = self._stream.read(self.chunk, exception_on_overflow=False)
        return np.frombuffer(data, dtype=np.int16)

    def close(self):
        try:
            self._stream.stop_stream()
            self._stream.close()
        except Exception as e:
            log.warning(f"closing the calibration stream raised: {e}")
        self._audio.terminate()


class Calibration:
    """一次校准。start() 立即返回，录音和计算都在后台线程；cancel() 可随时中止。"""

    def __init__(self, device_index, denoise_on, on_event,
                 source_factory=None, denoiser_factory=None):
        self.device_index = device_index
        self.denoise_on = denoise.effective(denoise_on)
        self._on_event = on_event
        self._source_factory = source_factory or PyAudioSource
        self._denoiser_factory = denoiser_factory or denoise.Denoiser
        self._cancel = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, name="calibration", daemon=True)
        self._thread.start()

    def cancel(self, wait=True):
        self._cancel.set()
        if wait and self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def _emit(self, **data):
        try:
            self._on_event(data)
        except Exception:
            log.exception("calibration event handler failed")

    def _run(self):
        try:
            source = self._source_factory(self.device_index)
        except Exception as e:
            log.warning(f"calibration could not open input {self.device_index}: {e}")
            self._emit(stage="error", message=str(e))
            return
        denoiser = self._denoiser_factory(source.rate) if self.denoise_on else None
        try:
            self._record(source, denoiser)
        except Exception as e:
            log.exception("calibration failed")
            self._emit(stage="error", message=str(e))
        finally:
            source.close()
            if denoiser is not None:
                denoiser.close()

    def _record(self, source, denoiser):
        per_second = source.rate / source.chunk
        quiet_frames = round(QUIET_SECONDS * per_second)
        total_frames = quiet_frames + round(READ_SECONDS * per_second)
        noise, reading, raw_frames = [], [], []
        self._emit(stage="quiet", progress=0.0, level=micgain.SILENCE_DBFS)
        for n in range(total_frames):
            if self._cancel.is_set():
                self._emit(stage="cancelled")
                return
            raw = source.read()
            frame = denoiser.process(raw) if denoiser else raw
            if n < quiet_frames:
                noise.append(frame)
            else:
                reading.append(frame)
                raw_frames.append(raw)
            if n + 1 == quiet_frames:
                self._emit(stage="read", progress=(n + 1) / total_frames,
                           level=micgain.frame_rms_dbfs(frame))
            elif n % LEVEL_EVERY == 0:
                self._emit(stage="quiet" if n < quiet_frames else "read",
                           progress=(n + 1) / total_frames, level=micgain.frame_rms_dbfs(frame))

        m = micgain.measure(reading, noise, source.rate, clip_frames=raw_frames)
        result = micgain.compute_gain(m)
        state = "on" if self.denoise_on else "off"
        summary = (f"speech {m.speech_dbfs:.1f} dBFS, noise {m.noise_dbfs:.1f} dBFS, "
                   f"{m.speech_seconds:.1f} s of speech, {m.clip_ratio:.1%} clipped")
        if result.reason:
            log.info(f"calibration rejected (denoise {state}): {result.reason} ({summary})")
        else:
            log.info(f"calibration accepted (denoise {state}): gain {result.gain_db:+.1f} dB ({summary})")
        self._emit(stage="done", gain_db=result.gain_db, reason=result.reason,
                   speech_dbfs=round(m.speech_dbfs, 1), noise_dbfs=round(m.noise_dbfs, 1),
                   speech_seconds=round(m.speech_seconds, 2), clip_ratio=round(m.clip_ratio, 4),
                   denoise=self.denoise_on)
