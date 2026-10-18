"""RNNoise 麦克风降噪。

ctypes 直接调 xiph RNNoise v0.2 的原生库（模型编在库里）。库由 CI 从
native/rnnoise/ 构建，随包放在 _internal/；PyInstaller 看不见 ctypes 的
依赖，所以 gui.spec 显式打包、发布流程显式验证。

库不在、或者设备不是 48 kHz（模型只训练了 48 kHz）时直通不处理，
写一条 WARNING——降噪没了可以接受，发不出话不行。
"""

import ctypes
import ctypes.util
import logging
import os
import sys

import numpy as np

log = logging.getLogger("denoise")

FRAME = 480           # RNNoise 一次处理 10 ms @ 48 kHz
RATE = 48000

_NAMES = {
    "win32": ("rnnoise.dll",),
    "darwin": ("librnnoise.dylib", "librnnoise.0.dylib"),
}.get(sys.platform, ("librnnoise.so", "librnnoise.so.0"))

_lib = None
_tried = False


def _candidates():
    dirs = (getattr(sys, "_MEIPASS", None),
            os.path.dirname(os.path.abspath(__file__)),
            "/opt/homebrew/lib", "/usr/local/lib")
    for directory in dirs:
        if not directory:
            continue
        for name in _NAMES:
            path = os.path.join(directory, name)
            if os.path.exists(path):
                yield path
    found = ctypes.util.find_library("rnnoise")
    if found:
        yield found


def _bind(lib):
    lib.rnnoise_create.restype = ctypes.c_void_p
    lib.rnnoise_create.argtypes = [ctypes.c_void_p]
    lib.rnnoise_process_frame.restype = ctypes.c_float
    lib.rnnoise_process_frame.argtypes = [ctypes.c_void_p,
                                          ctypes.POINTER(ctypes.c_float),
                                          ctypes.POINTER(ctypes.c_float)]
    lib.rnnoise_destroy.restype = None
    lib.rnnoise_destroy.argtypes = [ctypes.c_void_p]
    return lib


def load():
    """找到并加载 RNNoise。只试一次，找不到就一直是 None。"""
    global _lib, _tried
    if _tried:
        return _lib
    _tried = True
    for path in _candidates():
        try:
            _lib = _bind(ctypes.CDLL(path))
        except (OSError, AttributeError) as e:
            log.warning("could not load RNNoise from %s: %s", path, e)
            continue
        log.info("loaded RNNoise from %s", path)
        return _lib
    log.warning("RNNoise library not found; microphone noise suppression is unavailable")
    return None


def available():
    return load() is not None


def effective(setting):
    """设置开着且库可用，才算降噪在工作。校准记录按这个值区分。"""
    return bool(setting) and available()


class Denoiser:
    """一条发送链路一个。状态跨帧、跨 PTT 保留。"""

    def __init__(self, rate, lib=None):
        self.rate = rate
        # lib=False 表示"明确没有库"，测试用；None 表示去找
        self._lib = load() if lib is None else (lib or None)
        self._state = None
        self._buffer = np.zeros(FRAME, dtype=np.float32)
        self.active = False
        if self._lib is None:
            return
        if rate != RATE:
            log.warning("noise suppression needs 48 kHz; the input runs at %d Hz, "
                        "passing audio through", rate)
            return
        self._state = self._lib.rnnoise_create(None)
        self.active = bool(self._state)

    def process(self, samples):
        x = np.asarray(samples, dtype=np.int16)
        if not self.active or x.size == 0 or x.size % FRAME:
            return x.copy()
        out = np.empty(x.size, dtype=np.float32)
        pointer = self._buffer.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
        for i in range(0, x.size, FRAME):
            self._buffer[:] = x[i:i + FRAME]
            self._lib.rnnoise_process_frame(self._state, pointer, pointer)
            out[i:i + FRAME] = self._buffer
        return np.clip(np.round(out), -32768, 32767).astype(np.int16)

    def close(self):
        state, self._state = self._state, None
        self.active = False
        if state and self._lib is not None:
            self._lib.rnnoise_destroy(state)

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
