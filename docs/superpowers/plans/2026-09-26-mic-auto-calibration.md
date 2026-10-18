# 麦克风音量自动校准与降噪 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** controller / xpc / msfs 三个麦克风客户端：发送链路最前端加 RNNoise 降噪；按输入设备做一次朗读校准，把语音电平拉到 −20 dBFS；音量滑块变成本次启动有效的乘数；末端加 −1 dBFS 限幅器。

**Architecture:** 纯 numpy 的 `micgain.py` 负责测量、增益和限幅；numpy + ctypes 的 `denoise.py` 包一层 RNNoise 原生库；Qt 的 `calibration.py` 负责朗读对话框。`voice.py` 发送循环改为 `Denoiser.process()` → `micgain.apply_gain()`。`rnnoise.dll` 由 CI 从 xiph v0.2 发布包构建，随包分发。设置新增 `mic_calibration`（按设备名）与 `mic_denoise`；`mic_volume` 不再落盘。三个组件各持一份副本。

**Tech Stack:** Python 3.12、numpy、ctypes、PyAudio、PyQt6 + qfluentwidgets、unittest、CMake + MSVC（CI）、PyInstaller 6。

**Spec:** `docs/superpowers/specs/2026-09-26-mic-auto-calibration-design.md`

## Global Constraints

- 目标电平 `TARGET_DBFS = -20.0`；增益范围 `[-12.0, +20.0]` dB；限幅上限 `-1.0` dBFS；释放 `50` ms。
- 拒绝条件按此顺序：削波帧比例（原始信号）`> 0.01` → `clipping`；语音时长 `< 2.0` 秒 → `too_short`；语音电平 − 底噪 `< 15` dB → `too_noisy`。
- 语音帧：帧 RMS 比底噪高 `> 10` dB。帧长 20 ms。
- RNNoise：xiph `rnnoise-0.2.tar.gz`，SHA-256 `90fce4b00b9ff24c08dbfe31b82ffd43bae383d85c5535676d28b0a2b11c0d37`，URL `https://github.com/xiph/rnnoise/releases/download/v0.2/rnnoise-0.2.tar.gz`。帧 480 采样，仅 48 kHz。
- 降噪实际状态 = `mic_denoise` 且库可用（`denoise.effective(setting)`）。校准记录的 `denoise` 与之不一致时视为未校准。
- 日志文本英文；界面文本全部走 `i18n.t()`，zh 与 en 两份齐全，不硬编码中文（`test_i18n` 会查）。
- 组件间不共享代码。`xpc/` 与 `msfs/` 的 `micgain.py`、`denoise.py`、`calibration.py`、`test_micgain.py`、`test_denoise.py` 字节一致并进 `SharedCopyTest.SHARED`；`controller/` 的同名文件内容与 xpc 相同，但不受该测试约束。
- `rnnoise.dll` 不进仓库（`.gitignore`），只由 CI 或本地构建产生。
- `atis/` 不改。can-voice 不改。
- 测试在组件目录内运行：`python -m unittest discover -p "test_*.py"`，再跑 `python smoke_gui.py`。
- 本机没有 PyQt6 和 Python 3.12。只依赖 numpy 的测试本机可跑（`python3 -m unittest test_micgain test_denoise -v`）；依赖 PyQt6 / qfluentwidgets 的测试和 `smoke_gui.py` 需要 3.12 环境：在 can-audio 根目录 `uv venv --python 3.12 .temp/venv && uv pip install --python .temp/venv -r requirements-build.txt`（`.temp/` 已在 `.gitignore`）。PyAudio 装不上时，GUI 部分以 CI 结果为准，并在交付时说明。任务结束删除 `.temp/`。
- 提交用 YubiKey 签名，可能卡住：`perl -e 'alarm 40; exec @ARGV' git commit …`，超时则 `git -c commit.gpgsign=false commit …` 并报告哪些提交未签名。
- 提交信息结尾：`Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`

---

### Task 1: `micgain.py`（controller）及其测试

**Files:**
- Create: `controller/micgain.py`
- Create: `controller/test_micgain.py`

**Interfaces:**
- Produces（后续任务依赖，签名不得改动）：
  - 常量 `TARGET_DBFS, MIN_GAIN_DB, MAX_GAIN_DB, CEILING_DBFS, SILENCE_DBFS, FULL_SCALE`
  - `db_to_linear(db: float) -> float`
  - `frame_rms_dbfs(frame) -> float`
  - `Measurement(speech_dbfs, noise_dbfs, speech_seconds, clip_ratio)`（frozen dataclass）
  - `measure(frames: list, noise_frames: list, rate: int, clip_frames: list | None = None) -> Measurement`（`clip_frames` 为 None 时用 `frames` 判削波）
  - `GainResult(gain_db: float | None, reason: str | None)`；`reason ∈ {"clipping", "too_short", "too_noisy"}`
  - `compute_gain(m: Measurement) -> GainResult`
  - `calibration_entry(m: Measurement, gain_db: float, denoise: bool, now: datetime | None = None) -> dict`
  - `baseline_db(calibrations, device_name, denoise: bool) -> float | None`
  - `effective_db(baseline: float, percent: int) -> float | None`
  - `describe_multiplier(old: int, new: int, baseline: float) -> str`
  - `class Limiter(rate, ceiling_dbfs=CEILING_DBFS, release_ms=RELEASE_MS)`：属性 `rate`、`ceiling`、`gain`；`process(samples) -> np.ndarray[int16]`
  - `apply_gain(samples, baseline: float, percent: int, limiter: Limiter) -> np.ndarray[int16]`

- [ ] **Step 1: 写失败的测试**

`controller/test_micgain.py`：

```python
"""micgain 的测量、增益和限幅。只依赖 numpy，不需要音频设备。"""

import math
import unittest
from datetime import datetime, timezone

import numpy as np

import micgain

RATE = 48000
CHUNK = 960


def tone(dbfs, seconds, freq=440.0):
    """RMS 为 dbfs 的正弦，超出 int16 的部分削平（削波用例要的就是这个）。"""
    amplitude = micgain.FULL_SCALE * 10 ** (dbfs / 20.0) * math.sqrt(2.0)
    t = np.arange(int(RATE * seconds)) / RATE
    x = amplitude * np.sin(2 * math.pi * freq * t)
    return np.clip(np.round(x), -32768, 32767).astype(np.int16)


def noise(dbfs, seconds, seed=1):
    sigma = micgain.FULL_SCALE * 10 ** (dbfs / 20.0)
    rng = np.random.default_rng(seed)
    x = rng.normal(0.0, sigma, int(RATE * seconds))
    return np.clip(np.round(x), -32768, 32767).astype(np.int16)


def frames(x):
    return [x[i:i + CHUNK] for i in range(0, len(x) - CHUNK + 1, CHUNK)]


def session(speech_dbfs, speech_seconds, noise_dbfs=-65.0):
    """1 秒底噪 + (静 0.5 秒, 说 speech_seconds, 静 0.5 秒) 的朗读。"""
    quiet = frames(noise(noise_dbfs, 1.0, seed=2))
    talk = np.concatenate([
        noise(noise_dbfs, 0.5, seed=3),
        tone(speech_dbfs, speech_seconds).astype(np.int32)
        + noise(noise_dbfs, speech_seconds, seed=4),
        noise(noise_dbfs, 0.5, seed=5),
    ])
    talk = np.clip(talk, -32768, 32767).astype(np.int16)
    return frames(talk), quiet


class FrameLevelTest(unittest.TestCase):

    def test_sine_rms_is_measured_in_dbfs(self):
        level = micgain.frame_rms_dbfs(tone(-30.0, 0.02))
        self.assertAlmostEqual(level, -30.0, delta=0.1)

    def test_silence_is_the_floor_not_minus_infinity(self):
        self.assertEqual(micgain.frame_rms_dbfs(np.zeros(CHUNK, np.int16)),
                         micgain.SILENCE_DBFS)
        self.assertEqual(micgain.frame_rms_dbfs(np.zeros(0, np.int16)),
                         micgain.SILENCE_DBFS)


class ComputeGainTest(unittest.TestCase):

    def gain_for(self, speech_dbfs, seconds=3.0, noise_dbfs=-65.0):
        reading, quiet = session(speech_dbfs, seconds, noise_dbfs)
        m = micgain.measure(reading, quiet, RATE)
        return m, micgain.compute_gain(m)

    def test_quiet_speaker_is_raised_to_target(self):
        m, result = self.gain_for(-30.0)
        self.assertIsNone(result.reason)
        self.assertAlmostEqual(result.gain_db, 10.0, delta=0.5)
        self.assertAlmostEqual(m.speech_seconds, 3.0, delta=0.1)

    def test_loud_speaker_is_lowered_to_target(self):
        _, result = self.gain_for(-12.0)
        self.assertAlmostEqual(result.gain_db, -8.0, delta=0.5)

    def test_gain_is_clamped_high(self):
        _, result = self.gain_for(-45.0, noise_dbfs=-80.0)
        self.assertEqual(result.gain_db, micgain.MAX_GAIN_DB)

    def test_gain_is_clamped_low(self):
        _, result = self.gain_for(-5.0)
        self.assertEqual(result.gain_db, micgain.MIN_GAIN_DB)

    def test_clipping_is_rejected(self):
        _, result = self.gain_for(0.0)
        self.assertEqual(result, micgain.GainResult(None, "clipping"))

    def test_clipping_is_judged_on_the_raw_frames(self):
        """降噪后的信号可能不再削波，但削波发生在降噪之前，要看原始信号。"""
        reading, quiet = session(-30.0, 3.0)
        clipped, _ = session(0.0, 3.0)
        m = micgain.measure(reading, quiet, RATE, clip_frames=clipped)
        self.assertEqual(micgain.compute_gain(m).reason, "clipping")

    def test_too_little_speech_is_rejected(self):
        _, result = self.gain_for(-30.0, seconds=1.0)
        self.assertEqual(result, micgain.GainResult(None, "too_short"))

    def test_noisy_room_is_rejected(self):
        _, result = self.gain_for(-25.0, noise_dbfs=-38.0)
        self.assertEqual(result, micgain.GainResult(None, "too_noisy"))

    def test_no_frames_at_all_is_too_short(self):
        m = micgain.measure([], [], RATE)
        self.assertEqual(micgain.compute_gain(m).reason, "too_short")


class StoredCalibrationTest(unittest.TestCase):

    def test_entry_shape(self):
        m = micgain.Measurement(-28.54, -62.01, 3.0, 0.0)
        when = datetime(2026, 9, 26, 10, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(micgain.calibration_entry(m, 8.5, True, now=when), {
            "gain_db": 8.5,
            "speech_dbfs": -28.5,
            "noise_dbfs": -62.0,
            "denoise": True,
            "calibrated_at": "2026-09-26T10:00:00Z",
        })

    def test_baseline_lookup(self):
        stored = {"Mic A": {"gain_db": 8.5, "denoise": True},
                  "Bad": {"gain_db": "x", "denoise": True},
                  "Huge": {"gain_db": 99, "denoise": True},
                  "NotDict": 3}
        self.assertEqual(micgain.baseline_db(stored, "Mic A", True), 8.5)
        self.assertIsNone(micgain.baseline_db(stored, "Mic B", True))
        self.assertIsNone(micgain.baseline_db(stored, "Bad", True))
        self.assertIsNone(micgain.baseline_db(stored, "NotDict", True))
        self.assertIsNone(micgain.baseline_db(stored, None, True))
        self.assertIsNone(micgain.baseline_db(None, "Mic A", True))
        self.assertEqual(micgain.baseline_db(stored, "Huge", True), micgain.MAX_GAIN_DB)

    def test_a_calibration_made_with_other_denoise_state_does_not_count(self):
        stored = {"Mic A": {"gain_db": 8.5, "denoise": True},
                  "Old": {"gain_db": 3.0}}
        self.assertIsNone(micgain.baseline_db(stored, "Mic A", False))
        self.assertEqual(micgain.baseline_db(stored, "Old", False), 3.0)
        self.assertIsNone(micgain.baseline_db(stored, "Old", True))

    def test_multiplier_log_line(self):
        self.assertEqual(
            micgain.describe_multiplier(100, 130, 8.5),
            "mic multiplier 100% -> 130% (baseline +8.5 dB, effective +10.8 dB)")
        self.assertEqual(
            micgain.describe_multiplier(100, 0, 0.0),
            "mic multiplier 100% -> 0% (baseline +0.0 dB, effective muted)")


class LimiterTest(unittest.TestCase):

    def test_output_never_exceeds_the_ceiling(self):
        limiter = micgain.Limiter(RATE)
        hot = tone(-3.0, 1.0).astype(np.float64) * 10.0     # 约 +20 dB 过满量程
        for frame in frames(hot):
            out = limiter.process(frame)
            self.assertEqual(out.dtype, np.int16)
            self.assertLessEqual(int(np.max(np.abs(out.astype(np.int32)))),
                                 math.ceil(limiter.ceiling))

    def test_quiet_signal_passes_unchanged(self):
        limiter = micgain.Limiter(RATE)
        quiet = tone(-30.0, 0.02)
        out = limiter.process(quiet)
        self.assertTrue(np.all(np.abs(out.astype(np.int32) - quiet) <= 1))

    def test_gain_recovers_after_a_peak(self):
        limiter = micgain.Limiter(RATE)
        limiter.process(np.full(CHUNK, 32767.0 * 4))
        self.assertLess(limiter.gain, 0.5)
        for _ in range(10):                                  # 200 ms
            limiter.process(tone(-30.0, 0.02))
        self.assertGreater(limiter.gain, 0.95)


class ApplyGainTest(unittest.TestCase):

    def test_baseline_and_multiplier_multiply(self):
        limiter = micgain.Limiter(RATE)
        x = np.full(CHUNK, 1000, np.int16)
        out = micgain.apply_gain(x, 6.0206, 150, limiter)    # ×2 ×1.5
        self.assertTrue(np.all(np.abs(out.astype(np.int32) - 3000) <= 1))

    def test_zero_percent_is_silence(self):
        limiter = micgain.Limiter(RATE)
        out = micgain.apply_gain(np.full(CHUNK, 1000, np.int16), 10.0, 0, limiter)
        self.assertEqual(out.dtype, np.int16)
        self.assertFalse(out.any())

    def test_loud_input_does_not_wrap(self):
        limiter = micgain.Limiter(RATE)
        out = micgain.apply_gain(np.full(CHUNK, 30000, np.int16), 0.0, 200, limiter)
        self.assertTrue(np.all(out > 0), "int16 回绕会变成负数")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行，确认失败**

Run（在 `controller/` 内）：`python3 -m unittest test_micgain -v`
Expected: `ModuleNotFoundError: No module named 'micgain'`

- [ ] **Step 3: 实现**

`controller/micgain.py`：

```python
"""麦克风电平测量、校准增益和发送端限幅器。

只依赖 numpy：不碰 Qt，不碰音频设备，测试里用合成信号就能跑。
所有电平单位是 dBFS（相对 int16 满量程）。
"""

import math
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np

TARGET_DBFS = -20.0          # 有效语音帧的 RMS 目标
MIN_GAIN_DB = -12.0
MAX_GAIN_DB = 20.0
SPEECH_ABOVE_NOISE_DB = 10.0  # 比底噪高这么多才算语音帧
MIN_SPEECH_SECONDS = 2.0
MAX_CLIP_RATIO = 0.01
MIN_SNR_DB = 15.0
CEILING_DBFS = -1.0
RELEASE_MS = 50.0
FULL_SCALE = 32768.0
SILENCE_DBFS = -120.0         # 全零帧的电平，代替负无穷

REASONS = ("clipping", "too_short", "too_noisy")


def db_to_linear(db):
    return 10.0 ** (db / 20.0)


def frame_rms_dbfs(frame):
    x = np.asarray(frame, dtype=np.float64)
    if x.size == 0:
        return SILENCE_DBFS
    rms = math.sqrt(float(np.mean(x * x)))
    if rms <= 0.0:
        return SILENCE_DBFS
    return max(SILENCE_DBFS, 20.0 * math.log10(rms / FULL_SCALE))


def _power_mean_dbfs(levels):
    if not levels:
        return SILENCE_DBFS
    mean = sum(10.0 ** (level / 10.0) for level in levels) / len(levels)
    return max(SILENCE_DBFS, 10.0 * math.log10(mean))


def _clipped(frame):
    x = np.asarray(frame)
    return x.size > 0 and (int(x.max()) >= 32767 or int(x.min()) <= -32768)


@dataclass(frozen=True)
class Measurement:
    speech_dbfs: float
    noise_dbfs: float
    speech_seconds: float
    clip_ratio: float


def measure(frames, noise_frames, rate, clip_frames=None):
    """frames 是朗读期间的 20 ms 帧，noise_frames 是开头静音那一秒的帧。

    降噪开着的时候，frames / noise_frames 是降噪之后的帧（和实际发出去的一致），
    clip_frames 是降噪之前的原始帧——削波发生在麦克风那一侧，降噪看不出来。
    """
    clip_source = frames if clip_frames is None else clip_frames
    noise = _power_mean_dbfs([frame_rms_dbfs(f) for f in noise_frames])
    threshold = noise + SPEECH_ABOVE_NOISE_DB
    speech = [(level, f) for level, f in ((frame_rms_dbfs(f), f) for f in frames)
              if level > threshold]
    speech_dbfs = _power_mean_dbfs([level for level, _ in speech])
    seconds = sum(len(f) for _, f in speech) / float(rate)
    clip_ratio = ((sum(1 for f in clip_source if _clipped(f)) / len(clip_source))
                  if clip_source else 0.0)
    return Measurement(speech_dbfs, noise, seconds, clip_ratio)


@dataclass(frozen=True)
class GainResult:
    gain_db: float | None
    reason: str | None


def compute_gain(m):
    if m.clip_ratio > MAX_CLIP_RATIO:
        return GainResult(None, "clipping")
    if m.speech_seconds < MIN_SPEECH_SECONDS:
        return GainResult(None, "too_short")
    if m.speech_dbfs - m.noise_dbfs < MIN_SNR_DB:
        return GainResult(None, "too_noisy")
    gain = TARGET_DBFS - m.speech_dbfs
    return GainResult(round(min(MAX_GAIN_DB, max(MIN_GAIN_DB, gain)), 1), None)


def calibration_entry(m, gain_db, denoise, now=None):
    """写进设置文件 mic_calibration[设备名] 的那一条。"""
    now = now or datetime.now(timezone.utc)
    return {
        "gain_db": gain_db,
        "speech_dbfs": round(m.speech_dbfs, 1),
        "noise_dbfs": round(m.noise_dbfs, 1),
        "denoise": bool(denoise),
        "calibrated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def baseline_db(calibrations, device_name, denoise):
    """该设备在当前降噪状态下的基准增益。

    None 表示没校准过——包括记录是在另一种降噪状态下做的（降噪会改变测得的
    电平）；坏记录也当没校准。缺 denoise 键的记录视为不降噪时做的。
    """
    if not device_name or not isinstance(calibrations, dict):
        return None
    entry = calibrations.get(device_name)
    if not isinstance(entry, dict):
        return None
    if bool(entry.get("denoise", False)) != bool(denoise):
        return None
    try:
        gain = float(entry.get("gain_db"))
    except (TypeError, ValueError):
        return None
    if math.isnan(gain):
        return None
    return min(MAX_GAIN_DB, max(MIN_GAIN_DB, gain))


def effective_db(baseline, percent):
    if percent <= 0:
        return None
    return baseline + 20.0 * math.log10(percent / 100.0)


def describe_multiplier(old, new, baseline):
    effective = effective_db(baseline, new)
    effective_text = "muted" if effective is None else f"{effective:+.1f} dB"
    return (f"mic multiplier {old}% -> {new}% "
            f"(baseline {baseline:+.1f} dB, effective {effective_text})")


class Limiter:
    """峰值限幅。每帧算一次所需增益：要压就立即压，放开按 release_ms 指数回升。

    帧内从上一帧的增益线性过渡到本帧的增益，避免帧边界上的台阶；过渡期间
    仍超上限的采样最后被硬削到上限，所以输出永远不超过 ceiling。
    """

    def __init__(self, rate, ceiling_dbfs=CEILING_DBFS, release_ms=RELEASE_MS):
        self.rate = rate
        self.ceiling = (FULL_SCALE - 1.0) * db_to_linear(ceiling_dbfs)
        self.release_ms = release_ms
        self.gain = 1.0

    def process(self, samples):
        x = np.asarray(samples, dtype=np.float64)
        if x.size == 0:
            return np.zeros(0, dtype=np.int16)
        peak = float(np.max(np.abs(x)))
        needed = 1.0 if peak <= self.ceiling else self.ceiling / peak
        if needed < self.gain:
            target = needed
        else:
            frame_ms = 1000.0 * x.size / self.rate
            recovered = 1.0 - (1.0 - self.gain) * math.exp(-frame_ms / self.release_ms)
            target = min(needed, recovered)
        ramp = np.linspace(self.gain, target, x.size)
        self.gain = target
        y = np.clip(x * ramp, -self.ceiling, self.ceiling)
        return np.round(y).astype(np.int16)


def apply_gain(samples, baseline, percent, limiter):
    """int16 → × 基准 × 乘数 → 限幅 → int16。降噪在这之前做。"""
    x = np.asarray(samples, dtype=np.float64)
    if percent <= 0:
        return np.zeros(x.size, dtype=np.int16)
    return limiter.process(x * db_to_linear(baseline) * (percent / 100.0))
```

- [ ] **Step 4: 运行，确认通过**

Run：`python3 -m unittest test_micgain -v`
Expected: 全部 PASS。若 `test_noisy_room_is_rejected` 或 `test_gain_is_clamped_high` 因合成信号边界不稳而失败，调整测试里的 `noise_dbfs`，不改 `micgain.py` 的常量（常量来自 spec）。

- [ ] **Step 5: 提交**

```bash
git add controller/micgain.py controller/test_micgain.py
git commit -m "controller: 新增 micgain（电平测量、校准增益、限幅器）"
```

---

### Task 2: `denoise.py`（controller）及其测试

**Files:**
- Create: `controller/denoise.py`
- Create: `controller/test_denoise.py`

**Interfaces:**
- Produces：
  - 常量 `FRAME = 480`、`RATE = 48000`
  - `load() -> ctypes.CDLL | None`（只尝试一次，结果缓存）
  - `available() -> bool`
  - `effective(setting) -> bool`：`bool(setting) and available()`
  - `class Denoiser(rate: int, lib=None)`：属性 `rate`、`active: bool`；`process(samples) -> np.ndarray[int16]`（不处理时返回输入的 int16 副本）；`close()`
  - 库对象协议（`lib` 参数，测试用假库）：`rnnoise_create(None) -> 句柄`、`rnnoise_process_frame(state, out_ptr, in_ptr) -> float`、`rnnoise_destroy(state)`

- [ ] **Step 1: 写失败的测试**

`controller/test_denoise.py`：

```python
"""RNNoise 包装：假库测帧切分和直通，真库（有的话）测确实降了噪。"""

import ctypes
import math
import unittest

import numpy as np

import denoise


class FakeRnnoise:
    """原地把每个采样减半，记下调用次数。"""

    def __init__(self):
        self.calls = 0
        self.destroyed = False

    def rnnoise_create(self, model):
        return 1

    def rnnoise_process_frame(self, state, out, inp):
        self.calls += 1
        frame = np.ctypeslib.as_array(out, shape=(denoise.FRAME,))
        frame *= 0.5
        return 0.0

    def rnnoise_destroy(self, state):
        self.destroyed = True


class FakeLibraryTest(unittest.TestCase):

    def test_a_20ms_frame_is_two_rnnoise_calls(self):
        lib = FakeRnnoise()
        d = denoise.Denoiser(48000, lib=lib)
        self.assertTrue(d.active)
        out = d.process(np.full(960, 1000, np.int16))
        self.assertEqual(lib.calls, 2)
        self.assertEqual(out.dtype, np.int16)
        self.assertTrue(np.all(out == 500))

    def test_other_sample_rates_pass_through(self):
        lib = FakeRnnoise()
        d = denoise.Denoiser(44100, lib=lib)
        self.assertFalse(d.active)
        x = np.full(882, 1000, np.int16)
        out = d.process(x)
        self.assertEqual(lib.calls, 0)
        self.assertTrue(np.array_equal(out, x))

    def test_a_frame_that_is_not_a_multiple_of_480_passes_through(self):
        lib = FakeRnnoise()
        d = denoise.Denoiser(48000, lib=lib)
        x = np.full(500, 1000, np.int16)
        self.assertTrue(np.array_equal(d.process(x), x))
        self.assertEqual(lib.calls, 0)

    def test_no_library_passes_through(self):
        d = denoise.Denoiser(48000, lib=False)
        self.assertFalse(d.active)
        x = np.full(960, 1000, np.int16)
        self.assertTrue(np.array_equal(d.process(x), x))

    def test_close_destroys_the_state_once(self):
        lib = FakeRnnoise()
        d = denoise.Denoiser(48000, lib=lib)
        d.close()
        d.close()
        self.assertTrue(lib.destroyed)
        self.assertFalse(d.active)

    def test_effective_needs_both_the_setting_and_the_library(self):
        original = denoise.available
        try:
            denoise.available = lambda: True
            self.assertTrue(denoise.effective(True))
            self.assertFalse(denoise.effective(False))
            denoise.available = lambda: False
            self.assertFalse(denoise.effective(True))
        finally:
            denoise.available = original


@unittest.skipUnless(denoise.available(), "RNNoise library not found")
class RealLibraryTest(unittest.TestCase):

    def test_white_noise_is_attenuated(self):
        rng = np.random.default_rng(7)
        sigma = 32768.0 * 10 ** (-40 / 20.0)
        x = np.clip(np.round(rng.normal(0.0, sigma, 48000 * 3)),
                    -32768, 32767).astype(np.int16)
        d = denoise.Denoiser(48000)
        out = np.concatenate([d.process(x[i:i + 960])
                              for i in range(0, len(x), 960)])
        d.close()
        tail_in = x[48000:].astype(np.float64)       # 跳过 1 秒预热
        tail_out = out[48000:].astype(np.float64)
        ratio_db = 10 * math.log10(np.mean(tail_out ** 2) / np.mean(tail_in ** 2))
        self.assertLessEqual(ratio_db, -10.0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行，确认失败**

Run（在 `controller/` 内）：`python3 -m unittest test_denoise -v`
Expected: `ModuleNotFoundError: No module named 'denoise'`

- [ ] **Step 3: 实现**

`controller/denoise.py`：

```python
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
```

- [ ] **Step 4: 运行，确认通过**

Run：`python3 -m unittest test_denoise -v`
Expected: `FakeLibraryTest` 全部 PASS；本机没有库时 `RealLibraryTest` 显示 skipped。若本机 `brew install rnnoise` 成功，`RealLibraryTest` 也应 PASS。

- [ ] **Step 5: 提交**

```bash
git add controller/denoise.py controller/test_denoise.py
git commit -m "controller: 新增 denoise（RNNoise ctypes 包装）"
```

---

### Task 3: RNNoise 原生库构建与打包

**Files:**
- Create: `native/rnnoise/CMakeLists.txt`
- Create: `native/rnnoise/build.ps1`
- Create: `native/rnnoise/README.md`
- Modify: `.gitignore`
- Modify: `controller/gui.spec`、`xpc/gui.spec`、`msfs/gui.spec`
- Modify: `.github/workflows/release.yml`（新增 `rnnoise` job；`test` 与 `build` 依赖它；"验原生库"步骤）

**Interfaces:**
- Produces：CI artifact `rnnoise-dll`（内含 `rnnoise.dll`）；测试与打包前被复制到 `controller/`、`xpc/`、`msfs/`；打包产物 `_internal/rnnoise.dll`。

- [ ] **Step 1: 构建配方**

`native/rnnoise/CMakeLists.txt`：

```cmake
# xiph RNNoise v0.2 → rnnoise.dll（MSVC）。源文件表与 pyrnnoise 的 CMakeLists 一致；
# 发布包里已带 src/rnnoise_data.c，不需要构建时下载模型。
cmake_minimum_required(VERSION 3.14)
project(rnnoise LANGUAGES C)

set(RNNOISE_SRC "" CACHE PATH "Path to the unpacked rnnoise-0.2 directory")
if(NOT RNNOISE_SRC)
  message(FATAL_ERROR "set -DRNNOISE_SRC=<unpacked rnnoise-0.2>")
endif()

add_library(rnnoise SHARED
  ${RNNOISE_SRC}/src/celt_lpc.c
  ${RNNOISE_SRC}/src/denoise.c
  ${RNNOISE_SRC}/src/kiss_fft.c
  ${RNNOISE_SRC}/src/parse_lpcnet_weights.c
  ${RNNOISE_SRC}/src/pitch.c
  ${RNNOISE_SRC}/src/rnn.c
  ${RNNOISE_SRC}/src/rnnoise_data.c
  ${RNNOISE_SRC}/src/rnnoise_tables.c
  ${RNNOISE_SRC}/src/nnet.c
  ${RNNOISE_SRC}/src/nnet_default.c
)
target_include_directories(rnnoise PRIVATE ${RNNOISE_SRC}/include ${RNNOISE_SRC}/src)
if(MSVC)
  target_compile_definitions(rnnoise PRIVATE DLL_EXPORT RNNOISE_BUILD)
endif()
```

`native/rnnoise/build.ps1`：

```powershell
# 下载 xiph RNNoise v0.2 发布包、校验、构建 rnnoise.dll，输出到 -Out 目录。
param([string]$Out = "$PSScriptRoot/out")
$ErrorActionPreference = "Stop"

$url = "https://github.com/xiph/rnnoise/releases/download/v0.2/rnnoise-0.2.tar.gz"
$sha = "90fce4b00b9ff24c08dbfe31b82ffd43bae383d85c5535676d28b0a2b11c0d37"
$work = Join-Path $PSScriptRoot "work"
New-Item -ItemType Directory -Force -Path $work, $Out | Out-Null

$tarball = Join-Path $work "rnnoise-0.2.tar.gz"
Invoke-WebRequest -Uri $url -OutFile $tarball
$actual = (Get-FileHash -Algorithm SHA256 $tarball).Hash.ToLower()
if ($actual -ne $sha) { throw "rnnoise-0.2.tar.gz SHA-256 mismatch: $actual" }

tar -xzf $tarball -C $work
cmake -S $PSScriptRoot -B (Join-Path $work "build") -A x64 `
      -DRNNOISE_SRC=(Join-Path $work "rnnoise-0.2")
cmake --build (Join-Path $work "build") --config Release
Copy-Item (Join-Path $work "build/Release/rnnoise.dll") $Out -Force
Copy-Item (Join-Path $work "rnnoise-0.2/COPYING") (Join-Path $Out "RNNOISE-COPYING") -Force
```

`native/rnnoise/README.md`：

```markdown
# rnnoise

`denoise.py` 通过 ctypes 加载的 RNNoise 原生库。

- 来源：xiph RNNoise v0.2 发布包，BSD-3，SHA-256 固定在 `build.ps1`。
- Windows：`pwsh native/rnnoise/build.ps1 -Out controller`（需要 CMake 和 MSVC）。CI 在 `release.yml` 的 `rnnoise` job 里执行同一脚本。
- macOS 开发：`brew install rnnoise`。
- 产物 `rnnoise.dll` 不进仓库。
```

`.gitignore` 追加：

```
rnnoise.dll
native/rnnoise/work/
native/rnnoise/out/
```

- [ ] **Step 2: gui.spec 打包**

三个 `gui.spec` 在 `opus_binaries = ...` 之后加：

```python
def find_rnnoise():
    """rnnoise.dll 是 denoise.py 运行时用 ctypes 加载的，PyInstaller 看不见。

    CI 用 native/rnnoise/build.ps1 构建后放进组件目录。缺了不会打包失败，
    只会让用户拿到一个没有降噪的包，所以这里要警告、发布流程要验。
    """
    path = os.path.join(os.path.dirname(os.path.abspath(SPEC)), 'rnnoise.dll')
    if os.path.exists(path):
        return [(path, '.')]
    print('警告: 没有找到 rnnoise.dll，打出来的程序没有麦克风降噪。'
          '先运行 native/rnnoise/build.ps1 -Out <组件目录>。')
    return []
```

- `controller/gui.spec`、`xpc/gui.spec`：`binaries=opus_binaries,` → `binaries=opus_binaries + find_rnnoise(),`
- `msfs/gui.spec`：`binaries = opus_binaries + find_simconnect()` → `binaries = opus_binaries + find_simconnect() + find_rnnoise()`

- [ ] **Step 3: release.yml**

在 `test:` job 之前新增：

```yaml
  rnnoise:
    # denoise.py 的原生库。从 xiph 的 v0.2 发布包构建（SHA-256 固定在脚本里），
    # 不进仓库；test 和 build 都从这里拿。
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/cache@v4
        id: cache
        with:
          path: native/rnnoise/out
          key: rnnoise-0.2-${{ hashFiles('native/rnnoise/CMakeLists.txt', 'native/rnnoise/build.ps1') }}
      - name: 构建 rnnoise.dll
        if: steps.cache.outputs.cache-hit != 'true'
        shell: pwsh
        run: ./native/rnnoise/build.ps1 -Out native/rnnoise/out
      - uses: actions/upload-artifact@v4
        with:
          name: rnnoise-dll
          path: native/rnnoise/out/
```

`test:` job：加 `needs: rnnoise`（若已有 `needs` 则并入列表），并在 `pip install` 步骤之后加：

```yaml
      - uses: actions/download-artifact@v4
        with:
          name: rnnoise-dll
          path: native/rnnoise/out
      - name: 放 rnnoise.dll 进三个麦克风客户端
        shell: pwsh
        run: |
          foreach ($d in @("controller", "xpc", "msfs")) {
            Copy-Item native/rnnoise/out/rnnoise.dll $d/
          }
```

`build:` job：`needs: version` → `needs: [version, rnnoise]`；在"打包"步骤之前加：

```yaml
      - uses: actions/download-artifact@v4
        if: matrix.component != 'atis'
        with:
          name: rnnoise-dll
          path: native/rnnoise/out
      - name: 放 rnnoise.dll
        if: matrix.component != 'atis'
        shell: pwsh
        run: Copy-Item native/rnnoise/out/rnnoise.dll ${{ matrix.component }}/
```

"验原生库和版本号"步骤中，`opus.dll` 那行之后加：

```powershell
          if ("${{ matrix.component }}" -ne "atis" -and
              -not (Test-Path "$root/rnnoise.dll")) {
            $missing += "rnnoise.dll"
          }
```

并把该步骤开头注释里的"opus.dll 和 SimConnect.dll"改为"opus.dll、rnnoise.dll 和 SimConnect.dll"。

- [ ] **Step 4: 验证**

本机无法跑 MSVC 构建。推送到分支后看 CI：`rnnoise` job 成功上传 artifact；`test` job 中 `RealLibraryTest` 为 PASS（不是 skipped）；`build` job 的"验原生库"步骤通过。若本机有 PowerShell + CMake + 编译器，可先本地跑 `pwsh native/rnnoise/build.ps1`。

- [ ] **Step 5: 提交**

```bash
git add native/rnnoise .gitignore controller/gui.spec xpc/gui.spec msfs/gui.spec .github/workflows/release.yml
git commit -m "构建并打包 RNNoise 原生库（xiph v0.2）"
```

---

### Task 4: `micgain.py`、`denoise.py` 复制到 xpc / msfs

**Files:**
- Create: `xpc/` 与 `msfs/` 下的 `micgain.py`、`denoise.py`、`test_micgain.py`、`test_denoise.py`（与 controller 字节一致）
- Modify: `msfs/test_msfs.py`（`SharedCopyTest.SHARED` 与其文档字符串）

**Interfaces:**
- Consumes：Task 1、Task 2 的全部接口。

- [ ] **Step 1: 先改检查**

`msfs/test_msfs.py` 中：

```python
    SHARED = ("voice.py", "traffic.py", "mumblecompat.py", "ptt.py",
              "theme.py", "update.py", "chime.py", "observer.py",
              "micgain.py", "test_micgain.py", "denoise.py", "test_denoise.py")
```

文档字符串末尾追加：

```
    `micgain.py`、`denoise.py` 和它们的测试也是共享件：降噪、增益和限幅两边
    必须一致，否则两个客户端发出去的响度和底噪不一样，校准就白做了。
```

- [ ] **Step 2: 运行，确认失败**

Run（`msfs/` 内，3.12 环境）：`python -m unittest test_msfs.SharedCopyTest -v`
Expected: 失败，提示 `micgain.py` 不存在。

- [ ] **Step 3: 复制**

```bash
for f in micgain.py denoise.py test_micgain.py test_denoise.py; do
  cp controller/$f xpc/$f
  cp controller/$f msfs/$f
done
```

- [ ] **Step 4: 运行，确认通过**

```bash
(cd xpc && python3 -m unittest test_micgain test_denoise -v)
(cd msfs && python3 -m unittest test_micgain test_denoise -v && python -m unittest test_msfs.SharedCopyTest -v)
```

Expected: 全部 PASS（真库测试可 skipped）。

- [ ] **Step 5: 提交**

```bash
git add xpc/micgain.py msfs/micgain.py xpc/denoise.py msfs/denoise.py \
        xpc/test_micgain.py msfs/test_micgain.py xpc/test_denoise.py msfs/test_denoise.py \
        msfs/test_msfs.py
git commit -m "xpc、msfs: 新增 micgain、denoise 共享副本"
```

---

### Task 5: controller 发送链路

**Files:**
- Modify: `controller/voice.py`（import 区；`__init__` 中 `self.mic_volume = 100` 附近；`set_mic_volume` :739 附近；`_transmit_loop` :1268-1300）
- Test: `controller/test_voice.py`

**Interfaces:**
- Consumes：`micgain.apply_gain`、`micgain.Limiter`、`micgain.MIN_GAIN_DB`、`micgain.MAX_GAIN_DB`；`denoise.Denoiser`。
- Produces：
  - `VoiceClient.mic_baseline_db: float`（默认 `0.0`）；`set_mic_baseline(db)`（夹到 `[-12, 20]`）
  - `VoiceClient.mic_denoise: bool`（默认 `True`）；`set_mic_denoise(enabled: bool)`
  - `VoiceClient._process_mic(data: bytes) -> np.ndarray[int16]`
  - 测试钩子：`VoiceClient._denoiser_factory`（默认 `denoise.Denoiser`，签名 `(rate) -> Denoiser`）

- [ ] **Step 1: 写失败的测试**

在 `controller/test_voice.py` 的 `TransmitThreadTest` 之后新增（文件顶部如无 `import numpy as np` 则补上）：

```python
class HalvingDenoiser:
    def __init__(self, rate):
        self.rate = rate
        self.active = True
        self.frames = 0

    def process(self, samples):
        self.frames += 1
        return (np.asarray(samples, dtype=np.int16) // 2).astype(np.int16)

    def close(self):
        self.active = False


class MicProcessingTest(unittest.TestCase):
    """发送前的麦克风处理：降噪 → 基准 × 乘数 → 限幅器，不回绕。"""

    def client(self, denoise=False):
        client = make_client()
        client._denoiser_factory = HalvingDenoiser
        client.set_mic_denoise(denoise)
        return client

    def test_baseline_and_multiplier_multiply(self):
        client = self.client()
        client.set_mic_baseline(6.0206)          # ×2
        client.set_mic_volume(150)               # ×1.5
        out = client._process_mic(np.full(960, 1000, np.int16).tobytes())
        self.assertTrue(np.all(np.abs(out.astype(np.int32) - 3000) <= 1))

    def test_denoise_runs_before_the_gain(self):
        client = self.client(denoise=True)
        client.set_mic_baseline(6.0206)          # ×2
        out = client._process_mic(np.full(960, 1000, np.int16).tobytes())
        self.assertTrue(np.all(np.abs(out.astype(np.int32) - 1000) <= 1))

    def test_denoise_off_skips_the_denoiser(self):
        client = self.client(denoise=False)
        out = client._process_mic(np.full(960, 1000, np.int16).tobytes())
        self.assertTrue(np.all(out == 1000))

    def test_loud_input_never_wraps(self):
        client = self.client()
        client.set_mic_volume(200)
        out = client._process_mic(np.full(960, 30000, np.int16).tobytes())
        self.assertTrue(np.all(out > 0), "int16 回绕会变成负数")

    def test_baseline_is_clamped(self):
        client = self.client()
        client.set_mic_baseline(40)
        self.assertEqual(client.mic_baseline_db, 20.0)
        client.set_mic_baseline(-40)
        self.assertEqual(client.mic_baseline_db, -12.0)
```

- [ ] **Step 2: 运行，确认失败**

Run（`controller/` 内）：`python3 -m unittest test_voice.MicProcessingTest -v`
Expected: `AttributeError: ... 'set_mic_denoise'`

- [ ] **Step 3: 实现**

import 区，在 `import mumblecompat` 旁：

```python
import denoise
import micgain
```

`__init__` 中 `self.mic_volume = 100` 之后：

```python
        # 发送链路：降噪 → × 校准基准（dB）× 会话乘数（mic_volume）→ 限幅。
        # 降噪器和限幅器按采样率懒建，跨 PTT 保留状态。
        self.mic_baseline_db = 0.0
        self.mic_denoise = True
        self._denoiser_factory = denoise.Denoiser
        self._denoiser = None
        self._mic_limiter = None
```

`set_mic_volume` 之后：

```python
    def set_mic_baseline(self, db):
        self.mic_baseline_db = max(micgain.MIN_GAIN_DB,
                                   min(micgain.MAX_GAIN_DB, float(db)))

    def set_mic_denoise(self, enabled):
        self.mic_denoise = bool(enabled)
```

`_transmit_loop` 之前新增：

```python
    def _process_mic(self, data):
        """一帧麦克风 PCM → 要发出去的 int16。"""
        samples = np.frombuffer(data, dtype=np.int16)
        if self.mic_denoise:
            if self._denoiser is None or self._denoiser.rate != self.RATE:
                if self._denoiser is not None:
                    self._denoiser.close()
                self._denoiser = self._denoiser_factory(self.RATE)
            samples = self._denoiser.process(samples)
        if self._mic_limiter is None or self._mic_limiter.rate != self.RATE:
            self._mic_limiter = micgain.Limiter(self.RATE)
        return micgain.apply_gain(samples, self.mic_baseline_db, self.mic_volume,
                                  self._mic_limiter)
```

`_transmit_loop` 中把

```python
                    audio = np.frombuffer(data, dtype=np.int16)
                    audio = np.clip(audio * (self.mic_volume / 100.0),
                                    np.iinfo(np.int16).min,
                                    np.iinfo(np.int16).max).astype(np.int16)
```

替换为

```python
                    audio = self._process_mic(data)
```

- [ ] **Step 4: 运行，确认通过**

Run：`python3 -m unittest test_voice -v`
Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add controller/voice.py controller/test_voice.py
git commit -m "controller: 发送链路改为 降噪 → 基准 × 乘数 → 限幅器"
```

---

### Task 6: xpc / msfs 发送链路（修复 int16 回绕）

**Files:**
- Modify: `xpc/voice.py`（import 区；`__init__` 中 `self._rate = 48000` :231 附近；发送循环 :1187-1188）
- Copy: `msfs/voice.py` ← `xpc/voice.py`
- Test: `xpc/test_xpc.py`（`VoiceRuntimeTest`）

**Interfaces:**
- Consumes：同 Task 5。
- Produces：
  - `Voice._process_mic(data: bytes) -> np.ndarray[int16]`
  - 运行时读取 `settings.mic_baseline_db`（缺省 `0.0`）、`settings.mic_volume`（缺省 100）、`settings.mic_denoise`（缺省 True）。`mic_baseline_db` 由 GUI 设置（Task 10），不落盘。
  - 测试钩子 `Voice._denoiser_factory`（默认 `denoise.Denoiser`）

- [ ] **Step 1: 写失败的测试**

`xpc/test_xpc.py` 模块级（`VoiceRuntimeTest` 之前）加：

```python
class HalvingDenoiser:
    def __init__(self, rate):
        self.rate = rate
        self.active = True

    def process(self, samples):
        return (np.asarray(samples, dtype=np.int16) // 2).astype(np.int16)

    def close(self):
        self.active = False
```

`VoiceRuntimeTest` 中新增方法（文件顶部如无 `import numpy as np` 则补上）：

```python
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
```

- [ ] **Step 2: 运行，确认失败**

Run（`xpc/` 内）：`python3 -m unittest test_xpc.VoiceRuntimeTest -v`
Expected: 新增四项 `AttributeError: ... '_process_mic'`。

- [ ] **Step 3: 实现**

import 区加：

```python
import denoise
import micgain
```

`__init__` 中 `self._rate = 48000` 之后：

```python
        self._denoiser_factory = denoise.Denoiser
        self._denoiser = None
        self._mic_limiter = None
```

`_best_rate` 之前新增：

```python
    def _process_mic(self, data):
        """一帧麦克风 PCM → 要发出去的 int16：降噪 → × 基准 × 乘数 → 限幅。

        基准来自校准（settings.mic_baseline_db，GUI 按当前输入设备设置），
        乘数是设置里的麦克风音量，只在本次启动有效。
        """
        rate = getattr(self, "_rate", None) or 48000
        samples = np.frombuffer(data, dtype=np.int16)
        if getattr(self.settings, "mic_denoise", True):
            if self._denoiser is None or self._denoiser.rate != rate:
                if self._denoiser is not None:
                    self._denoiser.close()
                self._denoiser = self._denoiser_factory(rate)
            samples = self._denoiser.process(samples)
        if self._mic_limiter is None or self._mic_limiter.rate != rate:
            self._mic_limiter = micgain.Limiter(rate)
        baseline = getattr(self.settings, "mic_baseline_db", 0.0) or 0.0
        percent = getattr(self.settings, "mic_volume", 100)
        return micgain.apply_gain(samples, baseline, percent, self._mic_limiter)
```

发送循环中把

```python
                volume = getattr(self.settings, "mic_volume", 100) / 100.0
                samples = (np.frombuffer(data, dtype=np.int16) * volume).astype(np.int16)
```

替换为

```python
                samples = self._process_mic(data)
```

然后 `cp xpc/voice.py msfs/voice.py`。

- [ ] **Step 4: 运行，确认通过**

```bash
(cd xpc && python3 -m unittest test_xpc -v)
(cd msfs && python -m unittest test_msfs -v)
```

Expected: 全部 PASS，含 `SharedCopyTest`。

- [ ] **Step 5: 提交**

```bash
git add xpc/voice.py msfs/voice.py xpc/test_xpc.py
git commit -m "xpc、msfs: 发送链路加降噪、基准增益和限幅器，修复音量超过 100% 时的 int16 回绕"
```

---

### Task 7: 设置存储（三个组件）

**Files:**
- Modify: `controller/settings.py`（`Settings.__init__`、`load_settings`、`save_settings`）
- Modify: `xpc/settings.py`、`msfs/settings.py`（两份不同，分别改）
- Test: 新建 `controller/test_settings_calibration.py`；`xpc/test_xpc.py`、`msfs/test_msfs.py` 各新增一个类

**Interfaces:**
- Consumes：`micgain.baseline_db`、`denoise.effective`。
- Produces（三个组件一致）：
  - `Settings.mic_calibration: dict`，落盘
  - `Settings.mic_denoise: bool`，默认 True，落盘
  - `Settings.mic_volume: int`，每次启动为 `100`，不落盘
  - `Settings.denoise_active() -> bool`：`denoise.effective(self.mic_denoise)`
  - `Settings.baseline_for(device_name) -> float | None`：`micgain.baseline_db(self.mic_calibration, device_name, self.denoise_active())`
  - 保存方法：controller `save_settings()`，xpc / msfs `save()`（沿用原名）

- [ ] **Step 1: 写失败的测试**

`controller/test_settings_calibration.py`：

```python
"""mic_calibration、mic_denoise 落盘；mic_volume 是本次会话的乘数，不落盘。"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.modules.setdefault("pyaudio", mock.MagicMock())

import denoise
import settings as settings_module


class MicCalibrationSettingsTest(unittest.TestCase):

    def setUp(self):
        self._cwd = os.getcwd()
        os.chdir(tempfile.mkdtemp(prefix="can-settings-"))
        self._available = denoise.available
        denoise.available = lambda: True

    def tearDown(self):
        denoise.available = self._available
        os.chdir(self._cwd)

    def test_calibration_and_denoise_round_trip(self):
        s = settings_module.Settings()
        s.mic_calibration["Mic A"] = {"gain_db": 8.5, "denoise": True}
        s.mic_denoise = True
        s.save_settings()
        again = settings_module.Settings()
        self.assertEqual(again.mic_calibration, s.mic_calibration)
        self.assertTrue(again.mic_denoise)
        self.assertEqual(again.baseline_for("Mic A"), 8.5)
        self.assertIsNone(again.baseline_for("Mic B"))

    def test_denoise_defaults_on_and_can_be_turned_off(self):
        self.assertTrue(settings_module.Settings().mic_denoise)
        s = settings_module.Settings()
        s.mic_denoise = False
        s.save_settings()
        self.assertFalse(settings_module.Settings().mic_denoise)

    def test_toggling_denoise_invalidates_the_baseline(self):
        s = settings_module.Settings()
        s.mic_calibration["Mic A"] = {"gain_db": 8.5, "denoise": True}
        s.mic_denoise = False
        self.assertIsNone(s.baseline_for("Mic A"))

    def test_missing_library_means_denoise_inactive(self):
        denoise.available = lambda: False
        s = settings_module.Settings()
        s.mic_calibration["Mic A"] = {"gain_db": 4.0, "denoise": False}
        self.assertFalse(s.denoise_active())
        self.assertEqual(s.baseline_for("Mic A"), 4.0)

    def test_mic_volume_is_not_persisted(self):
        s = settings_module.Settings()
        s.mic_volume = 150
        s.save_settings()
        with open(s.config_file, encoding="utf-8") as f:
            self.assertNotIn("mic_volume", json.load(f))
        self.assertEqual(settings_module.Settings().mic_volume, 100)

    def test_old_mic_volume_is_ignored(self):
        with open("radio_settings.json", "w", encoding="utf-8") as f:
            json.dump({"mic_volume": 170}, f)
        self.assertEqual(settings_module.Settings().mic_volume, 100)

    def test_corrupt_calibration_becomes_empty(self):
        with open("radio_settings.json", "w", encoding="utf-8") as f:
            json.dump({"mic_calibration": [1, 2]}, f)
        self.assertEqual(settings_module.Settings().mic_calibration, {})


if __name__ == "__main__":
    unittest.main()
```

`xpc/test_xpc.py` 与 `msfs/test_msfs.py` 末尾（`if __name__` 之前）各新增：

```python
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
```

- [ ] **Step 2: 运行，确认失败**

```bash
(cd controller && python -m unittest test_settings_calibration -v)
(cd xpc && python -m unittest test_xpc.MicCalibrationSettingsTest -v)
(cd msfs && python -m unittest test_msfs.MicCalibrationSettingsTest -v)
```

Expected: FAIL（`mic_calibration` / `baseline_for` 不存在，`mic_volume` 被写入）。需要 3.12 环境。

- [ ] **Step 3: 实现**

`controller/settings.py`：import 区加 `import denoise`、`import micgain`。`__init__` 中把 `self.mic_volume = 100` 改为：

```python
        # 麦克风音量是乘在校准基准上的本次会话乘数：每次启动都是 100%，不落盘。
        self.mic_volume = 100
        # 设备名 → 校准记录，见 micgain.calibration_entry()。按名字存是因为
        # PyAudio 的设备序号跨启动不稳定。
        self.mic_calibration = {}
        self.mic_denoise = True
```

`load_settings` 中删除 `self.mic_volume = data.get("mic_volume", 100)`，在同位置加：

```python
                    calibration = data.get("mic_calibration")
                    self.mic_calibration = calibration if isinstance(calibration, dict) else {}
                    self.mic_denoise = bool(data.get("mic_denoise", True))
```

`save_settings` 的 `data` 中删除 `"mic_volume": self.mic_volume,`，加：

```python
                "mic_calibration": self.mic_calibration,
                "mic_denoise": self.mic_denoise,
```

`save_settings` 之后新增：

```python
    def denoise_active(self):
        return denoise.effective(self.mic_denoise)

    def baseline_for(self, device_name):
        return micgain.baseline_db(self.mic_calibration, device_name,
                                   self.denoise_active())
```

`xpc/settings.py` 与 `msfs/settings.py`（分别改，内容相同）：import 区加 `import denoise`、`import micgain`。`DEFAULTS` 中 `"mic_volume": 100,` 之后加：

```python
    # 设备名 → 校准记录（micgain.calibration_entry）。mic_volume 是乘在基准上
    # 的本次会话乘数，load() 不读、save() 不写。
    "mic_calibration": {},
    "mic_denoise": True,
```

`__init__` 中 `for key, value in DEFAULTS.items(): setattr(self, key, value)` 之后、`self.load()` 之前加：

```python
        # DEFAULTS 里的 {} 是模块级的同一个对象，直接 setattr 会让所有实例共享它
        self.mic_calibration = {}
```

`load` 中把 `self.mic_volume = _clamp_volume(self.mic_volume)` 替换为：

```python
        self.mic_volume = 100
        if not isinstance(self.mic_calibration, dict):
            self.mic_calibration = {}
        self.mic_denoise = bool(self.mic_denoise)
```

`save` 中 `data.pop("joystick_ptt", None)` 之后加 `data.pop("mic_volume", None)`。

类末尾新增：

```python
    def denoise_active(self):
        return denoise.effective(self.mic_denoise)

    def baseline_for(self, device_name):
        return micgain.baseline_db(self.mic_calibration, device_name,
                                   self.denoise_active())
```

- [ ] **Step 4: 运行，确认通过**

三个目录内分别：`python -m unittest discover -p "test_*.py"`
Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add controller/settings.py controller/test_settings_calibration.py \
        xpc/settings.py msfs/settings.py xpc/test_xpc.py msfs/test_msfs.py
git commit -m "设置：新增 mic_calibration、mic_denoise，麦克风音量改为不落盘的会话乘数"
```

---

### Task 8: 校准对话框与界面文本

**Files:**
- Create: `controller/calibration.py`、`xpc/calibration.py`、`msfs/calibration.py`（三份内容相同）
- Modify: 三个 `i18n.py`（`TEXT` 字典末尾加同一组键）
- Modify: 三个 `test_i18n.py`（加 `calibration.py` 的硬编码检查）
- Modify: `msfs/test_msfs.py`（`SharedCopyTest.SHARED` 加 `"calibration.py"`）
- Modify: 三个 `smoke_gui.py`

**Interfaces:**
- Consumes：`micgain.measure`（含 `clip_frames`）、`compute_gain`、`calibration_entry`、`frame_rms_dbfs`；`denoise.Denoiser`；`theme.dialog_qss`、`theme.IDLE_COLOR`、`theme.ON_COLOR`、`theme.MUTED_COLOR`；`i18n.t`。
- Produces：
  - `calibration.input_device_name(index: int | None) -> str | None`
  - `calibration.CalibrationDialog(device_index, device_name, denoise_on: bool, parent=None, source_factory=None, denoiser_factory=None)`：`QDialog`；接受后 `entry() -> dict | None`
  - source 协议：属性 `rate`、`chunk`；`read_available() -> np.ndarray[int16]`、`close()`

- [ ] **Step 1: i18n 键与检查**

三个 `i18n.py` 的 `TEXT` 字典末尾（`}` 之前）加：

```python
    # ---- 麦克风音量自动校准 / 降噪 ----
    "calib.title":        {"zh": "麦克风音量自动校准", "en": "Automatic microphone calibration"},
    "calib.button":       {"zh": "麦克风音量自动校准", "en": "Calibrate microphone"},
    "calib.baseline":     {"zh": "当前基准：{db} dB", "en": "Current baseline: {db} dB"},
    "calib.uncalibrated": {"zh": "当前基准：未校准", "en": "Current baseline: not calibrated"},
    "calib.device":       {"zh": "设备：{name}", "en": "Device: {name}"},
    "calib.intro":        {"zh": "点击开始后先保持安静 1 秒，然后以平时通话的音量和麦克风距离读出下面这段话。",
                           "en": "After pressing Start, stay quiet for one second, then read the text below at your normal speaking volume and microphone distance."},
    "calib.phrase":       {"zh": "上海进近，东方五三五一，通过六千米下降到四千二百米，航向二七零，建立航向道报告。",
                           "en": "Shanghai Approach, China Eastern five three five one, passing six thousand metres descending four thousand two hundred metres, heading two seven zero, will report established."},
    "calib.start":        {"zh": "开始", "en": "Start"},
    "calib.retry":        {"zh": "重试", "en": "Retry"},
    "calib.save":         {"zh": "保存", "en": "Save"},
    "calib.cancel":       {"zh": "取消", "en": "Cancel"},
    "calib.stage_quiet":  {"zh": "请保持安静……", "en": "Stay quiet…"},
    "calib.stage_read":   {"zh": "请朗读……", "en": "Read now…"},
    "calib.result":       {"zh": "校准完成，基准 {db} dB", "en": "Calibrated: baseline {db} dB"},
    "calib.fail_clipping":  {"zh": "输入削波。请把麦克风拿远一点，或调低系统输入音量后重试。",
                             "en": "The input is clipping. Move the microphone further away or lower the system input level, then retry."},
    "calib.fail_too_short": {"zh": "没有检测到足够的语音。请完整读出上面的文字后重试。",
                             "en": "Not enough speech was detected. Read the whole text above, then retry."},
    "calib.fail_too_noisy": {"zh": "环境太吵或麦克风声音太小。请换到安静的环境，或调高系统输入音量后重试。",
                             "en": "The room is too noisy or the microphone too quiet. Move somewhere quieter or raise the system input level, then retry."},
    "calib.open_failed":  {"zh": "打不开麦克风：{error}", "en": "Could not open the microphone: {error}"},
    "calib.no_device":    {"zh": "没有可用的输入设备", "en": "No input device available"},
    "calib.denoise":      {"zh": "麦克风降噪", "en": "Microphone noise suppression"},
    "calib.denoise_unavailable": {"zh": "降噪不可用", "en": "Noise suppression unavailable"},
```

三个 `test_i18n.py` 中，在 `test_settings_dialog_has_no_hardcoded_chinese`（或该文件里同类测试）旁加：

```python
    def test_calibration_dialog_has_no_hardcoded_chinese(self):
        self.assertEqual(self.offenders("calibration.py"), [])
```

`msfs/test_msfs.py` 的 `SharedCopyTest.SHARED` 追加 `"calibration.py"`。

- [ ] **Step 2: 运行，确认失败**

Run（controller 内）：`python -m unittest test_i18n -v`
Expected: `test_calibration_dialog_has_no_hardcoded_chinese` 报 `FileNotFoundError`。

- [ ] **Step 3: 实现 `calibration.py`**

`controller/calibration.py`：

```python
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
```

注意：`denoise_on` 由调用方传 `settings.denoise_active()`，所以记录里的 `denoise` 与之后 `baseline_for()` 的查询条件一致。设备不是 48 kHz 时降噪器直通，但记录仍按 `denoise_active()` 写——这样不会因为采样率反复弹框。

复制：

```bash
cp controller/calibration.py xpc/calibration.py
cp controller/calibration.py msfs/calibration.py
```

- [ ] **Step 4: 冒烟测试加一项**

三个 `smoke_gui.py` 的 `main()` 中，`def check(...)` 定义之后、构造主窗口之前加：

```python
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
```

- [ ] **Step 5: 运行，确认通过**

三个目录内分别：

```bash
python -m unittest discover -p "test_*.py"
python smoke_gui.py
```

Expected: 全部 PASS；smoke 输出 `ok   校准对话框`。

- [ ] **Step 6: 提交**

```bash
git add controller/calibration.py xpc/calibration.py msfs/calibration.py \
        controller/i18n.py xpc/i18n.py msfs/i18n.py \
        controller/test_i18n.py xpc/test_i18n.py msfs/test_i18n.py \
        controller/smoke_gui.py xpc/smoke_gui.py msfs/smoke_gui.py msfs/test_msfs.py
git commit -m "新增麦克风音量自动校准对话框"
```

---

### Task 9: controller 界面接线

**Files:**
- Modify: `controller/settings.py`（`SettingsDialog.setup_ui` 麦克风行；`save_and_close`；新增 `calibrate`、`_refresh_baseline`）
- Modify: `controller/gui.py`（import；`ControllerWindow.__init__` 末尾 :449 附近；`connect_voice` :1048 附近；`open_settings` :1237-1260；新增三个方法）
- Modify: `controller/smoke_gui.py`（降噪不可用时设置对话框可构造）

**Interfaces:**
- Consumes：`calibration.CalibrationDialog`、`calibration.input_device_name`；`denoise.available`；`Settings.mic_calibration`、`mic_denoise`、`denoise_active()`、`baseline_for()`、`save_settings()`；`VoiceClient.set_mic_baseline`、`set_mic_denoise`；`micgain.describe_multiplier`。
- Produces：`ControllerWindow.apply_mic_baseline() -> tuple[str | None, float | None]`、`maybe_prompt_calibration()`、`run_calibration(device_name)`。

- [ ] **Step 1: 设置对话框**

`controller/settings.py` import 区加 `import calibration`（`denoise` 已在 Task 7 引入）。

`setup_ui` 中，`layout.addLayout(mic_layout)` 之后加：

```python
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
```

`input_combo` 建好之后（`layout.addLayout(input_layout)` 之后）加：

```python
        self.input_combo.currentIndexChanged.connect(lambda _: self._refresh_baseline())
        self._refresh_baseline()
```

`save_and_close` 中 `self.settings.debug = ...` 之后加：

```python
        if denoise.available():
            self.settings.mic_denoise = self.denoise_checkbox.isChecked()
```

`save_and_close` 之前新增：

```python
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
```

注意：`calibrate` 调 `save_settings()` 时，其它字段仍是打开对话框前的值（对话框只在 `save_and_close` 里回写），不会提前保存未确认的改动。降噪复选框改了但还没保存时，这里按已保存的状态校准；保存后若状态变了，主窗口会按新状态再提示一次。

- [ ] **Step 2: 主窗口**

`controller/gui.py` import 区加 `import calibration`、`import micgain`。

`ControllerWindow` 中新增方法（放在 `open_settings` 之前）：

```python
    # ---------- 麦克风校准 ----------
    def apply_mic_baseline(self):
        """按当前输入设备和降噪状态取基准增益，交给语音客户端，并记一行日志。"""
        name = calibration.input_device_name(self.settings.input_device_index)
        baseline = self.settings.baseline_for(name)
        log.info("mic baseline for %s: %s (denoise %s)", name or "unknown device",
                 "uncalibrated" if baseline is None else f"{baseline:+.1f} dB",
                 "on" if self.settings.denoise_active() else "off")
        if self.voice:
            self.voice.set_mic_baseline(baseline or 0.0)
            self.voice.set_mic_denoise(self.settings.mic_denoise)
        return name, baseline

    def maybe_prompt_calibration(self):
        name, baseline = self.apply_mic_baseline()
        if name is not None and baseline is None:
            self.run_calibration(name)

    def run_calibration(self, device_name):
        was_running = hasattr(self, 'ptt_watcher')
        if was_running:
            self.ptt_watcher.stop()
        dialog = calibration.CalibrationDialog(
            self.settings.input_device_index, device_name,
            self.settings.denoise_active(), self)

        def done(code):
            if code == QDialog.DialogCode.Accepted and dialog.entry():
                self.settings.mic_calibration[device_name] = dialog.entry()
                self.settings.save_settings()
            else:
                log.info("calibration skipped on %s", device_name)
            self.apply_mic_baseline()
            if was_running:
                self.ptt_watcher.set_bindings(self.settings.ptt_bindings)
                self.ptt_watcher.start()

        dialog.finished.connect(done)
        dialog.open()
```

（若 `gui.py` 尚未 import `QDialog`，在 `PyQt6.QtWidgets` 的 import 中补上。）

`__init__` 中 `self.check_for_update()` 之后加：

```python
        # 所选麦克风在当前降噪状态下没校准过就弹一次。open() 不阻塞，窗口先显示出来。
        QTimer.singleShot(0, self.maybe_prompt_calibration)
```

`connect_voice` 中 `self.voice.set_mic_volume(self.settings.mic_volume)` 之后加：

```python
        self.apply_mic_baseline()
```

`open_settings` 中：`dialog = SettingsDialog(...)` 之前加 `old_mic = self.settings.mic_volume`；`if accepted:` 分支内 `self.retranslate()` 之后加：

```python
            if self.settings.mic_volume != old_mic:
                _, baseline = self.apply_mic_baseline()
                log.info(micgain.describe_multiplier(
                    old_mic, self.settings.mic_volume, baseline or 0.0))
```

并在 `if accepted:` 分支末尾（`setup_audio` 的 try 之后）加：

```python
            # 换了输入设备或切了降噪，且新状态下没校准过，就再提示一次
            QTimer.singleShot(0, self.maybe_prompt_calibration)
```

- [ ] **Step 3: smoke 覆盖"降噪不可用"**

`controller/smoke_gui.py` 中现有构造 `SettingsDialog` 的检查之后加：

```python
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
```

（`SettingsDialog` 在 gui.py 里由 `from settings import Settings, SettingsDialog` 引入；变量名 `window` 以 smoke 文件实际为准。）

- [ ] **Step 4: 运行**

```bash
cd controller && python -m unittest discover -p "test_*.py" && python smoke_gui.py
```

Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add controller/settings.py controller/gui.py controller/smoke_gui.py
git commit -m "controller: 接入麦克风校准与降噪开关（首次启动、换设备、设置入口）"
```

---

### Task 10: xpc / msfs 界面接线

**Files:**
- Modify: `xpc/gui.py`、`msfs/gui.py`（两份不同，分别改；锚点如下）
  - 主窗口 `__init__` 中 `self.check_for_update()` 之后（xpc :182，msfs :190）
  - `open_settings`（xpc :856，msfs :938 附近）
  - `SettingsDialog`：麦克风音量滑块 `form.addRow(BodyLabel(t("settings.mic_volume")), self.mic_slider)` 之后（xpc :1142，msfs :1222）；`apply()`
- Modify: `xpc/smoke_gui.py`、`msfs/smoke_gui.py`

**Interfaces:**
- Consumes：同 Task 9；`Settings.save()`；`settings.mic_baseline_db`、`settings.mic_denoise`（Task 6 的 `Voice._process_mic` 读取）。
- Produces：主窗口方法 `apply_mic_baseline()`、`maybe_prompt_calibration()`、`run_calibration(device_name)`。

以下代码在 xpc 与 msfs 中相同；主窗口类名以文件为准。

- [ ] **Step 1: 设置对话框**

import 区加 `import calibration`、`import denoise`。

麦克风音量滑块那一行之后加：

```python
        calib_row = QHBoxLayout()
        self.baseline_label = CaptionLabel("")
        self.baseline_label.setStyleSheet(f"color: {theme.IDLE_COLOR};")
        calib_button = PushButton(t("calib.button"))
        calib_button.clicked.connect(self.calibrate)
        calib_row.addWidget(self.baseline_label)
        calib_row.addStretch()
        calib_row.addWidget(calib_button)
        form.addRow(calib_row)

        self.denoise_check = CheckBox(t("calib.denoise"))
        if denoise.available():
            self.denoise_check.setChecked(bool(self.settings.mic_denoise))
        else:
            self.denoise_check.setChecked(False)
            self.denoise_check.setEnabled(False)
            self.denoise_check.setText(t("calib.denoise_unavailable"))
        form.addRow(self.denoise_check)

        self.input_box.currentIndexChanged.connect(lambda _: self._refresh_baseline())
        self._refresh_baseline()
```

（`CaptionLabel`、`PushButton`、`CheckBox`、`QHBoxLayout` 若未 import 则补上。）

`apply()` 中 `self.settings.mic_volume = self.mic_slider.value()` 之后加：

```python
        if denoise.available():
            self.settings.mic_denoise = self.denoise_check.isChecked()
```

`apply` 之前新增：

```python
    def _refresh_baseline(self):
        name = calibration.input_device_name(self.input_box.currentData())
        baseline = self.settings.baseline_for(name)
        self.baseline_label.setText(
            t("calib.uncalibrated") if baseline is None
            else t("calib.baseline", db=f"{baseline:+.1f}"))

    def calibrate(self):
        """校准当前选中的输入设备，按已保存的降噪状态测，结果立即落盘。"""
        index = self.input_box.currentData()
        name = calibration.input_device_name(index)
        if name is None:
            self.baseline_label.setText(t("calib.no_device"))
            return
        dialog = calibration.CalibrationDialog(index, name,
                                               self.settings.denoise_active(), self)
        if dialog.exec() and dialog.entry():
            self.settings.mic_calibration[name] = dialog.entry()
            self.settings.save()
        self._refresh_baseline()
```

注意：这里的 `self.settings.save()` 写的是打开对话框时的设置（`apply()` 还没跑），不会提前保存未确认的改动。

- [ ] **Step 2: 主窗口**

import 区加 `import micgain`（`calibration` 已在 Step 1 引入）。新增方法（放在 `open_settings` 之前）：

```python
    # ---------- 麦克风校准 ----------
    def apply_mic_baseline(self):
        """按当前输入设备和降噪状态取基准增益写到 settings.mic_baseline_db
        （Voice 发送时读），记一行日志。"""
        name = calibration.input_device_name(self.settings.input_device_index)
        baseline = self.settings.baseline_for(name)
        log.info("mic baseline for %s: %s (denoise %s)", name or "unknown device",
                 "uncalibrated" if baseline is None else f"{baseline:+.1f} dB",
                 "on" if self.settings.denoise_active() else "off")
        self.settings.mic_baseline_db = baseline or 0.0
        return name, baseline

    def maybe_prompt_calibration(self):
        name, baseline = self.apply_mic_baseline()
        if name is not None and baseline is None:
            self.run_calibration(name)

    def run_calibration(self, device_name):
        was_running = self.ptt_watcher.is_running()
        self.ptt_watcher.stop()
        dialog = calibration.CalibrationDialog(
            self.settings.input_device_index, device_name,
            self.settings.denoise_active(), self)

        def done(code):
            if code == QDialog.DialogCode.Accepted and dialog.entry():
                self.settings.mic_calibration[device_name] = dialog.entry()
                self.settings.save()
            else:
                log.info("calibration skipped on %s", device_name)
            self.apply_mic_baseline()
            self.ptt_watcher.set_bindings(self.settings.ptt_bindings)
            if was_running:
                self.ptt_watcher.start()

        dialog.finished.connect(done)
        dialog.open()
```

`mic_baseline_db` 不在 `DEFAULTS` 里，所以 `save()` 不会写它。

`__init__` 中 `self.check_for_update()` 之后加：

```python
        # 所选麦克风在当前降噪状态下没校准过就弹一次。open() 不阻塞，窗口先显示出来。
        QTimer.singleShot(0, self.maybe_prompt_calibration)
```

`open_settings` 中：`dialog = SettingsDialog(...)` 之前加 `old_mic = self.settings.mic_volume`；`if accepted:` 分支里 `dialog.apply()` 之后加：

```python
            if self.settings.mic_volume != old_mic:
                _, baseline = self.apply_mic_baseline()
                log.info(micgain.describe_multiplier(
                    old_mic, self.settings.mic_volume, baseline or 0.0))
```

在 `open_settings` 末尾（PTT 监听恢复之后）加：

```python
        if accepted:
            # 换了输入设备或切了降噪，且新状态下没校准过，就再提示一次
            QTimer.singleShot(0, self.maybe_prompt_calibration)
```

- [ ] **Step 3: smoke 覆盖"降噪不可用"**

`xpc/smoke_gui.py`、`msfs/smoke_gui.py` 中现有构造 `SettingsDialog` 的检查之后加：

```python
    def settings_without_rnnoise():
        import denoise
        original = denoise.available
        denoise.available = lambda: False
        try:
            dialog = gui.SettingsDialog(window.settings, window)
            assert not dialog.denoise_check.isEnabled()
            dialog.reject()
        finally:
            denoise.available = original

    check("设置对话框（降噪不可用）", settings_without_rnnoise)
```

（`window` 与 settings 的变量名以该 smoke 文件实际为准。）

- [ ] **Step 4: 运行**

```bash
(cd xpc && python -m unittest discover -p "test_*.py" && python smoke_gui.py)
(cd msfs && python -m unittest discover -p "test_*.py" && python smoke_gui.py)
```

Expected: 全部 PASS。

- [ ] **Step 5: 提交**

```bash
git add xpc/gui.py msfs/gui.py xpc/smoke_gui.py msfs/smoke_gui.py
git commit -m "xpc、msfs: 接入麦克风校准与降噪开关（首次启动、换设备、设置入口）"
```

---

### Task 11: 文档与收尾

**Files:**
- Modify: `CLAUDE.md`（can-audio 根目录）
- Modify: `docs/superpowers/specs/2026-09-26-mic-auto-calibration-design.md`（状态改为 `已实施`）

- [ ] **Step 1: CLAUDE.md**

在 "**Audio path.**" 段落之后加一段：

```markdown
**Microphone chain.** `voice.py` sends `Denoiser.process()` (RNNoise, 48 kHz only, pass-through when the library is missing) → `micgain.apply_gain()` = × calibrated baseline × session multiplier → limiter (−1 dBFS). The baseline is stored per input-device name in the settings file's `mic_calibration`, tagged with the denoise state it was measured under; a mismatch counts as uncalibrated. `mic_volume` is the session multiplier: 100% at every launch, never persisted. `mic_denoise` is persisted.

**`rnnoise.dll` is built, not committed.** `native/rnnoise/build.ps1` builds it from xiph's v0.2 tarball (SHA-256 pinned); `release.yml`'s `rnnoise` job runs it for both the test and build jobs, the three mic clients' `gui.spec` bundle it, and the native-library check fails the release if `_internal/rnnoise.dll` is missing. It is loaded through ctypes, so PyInstaller cannot see it — same trap as `opus.dll`.

`micgain.py`, `denoise.py` and `calibration.py` are byte-identical between `xpc/` and `msfs/` (`SharedCopyTest`); `controller/` carries its own copies.
```

- [ ] **Step 2: 全量验证**

```bash
for d in controller xpc msfs; do (cd $d && python -m unittest discover -p "test_*.py" && python smoke_gui.py) || echo "FAIL $d"; done
```

Expected: 无 `FAIL`。推送后确认 CI 的 `rnnoise`、`test`、`build` 三个 job 全绿，且 `test` 中 `RealLibraryTest` 实际运行（非 skipped）。

- [ ] **Step 3: 手动验证（交给用户，需要 Windows 和真实麦克风）**

1. 删除 `radio_settings.json` 中的 `mic_calibration`，启动 audio-for-can，应自动弹出校准框。
2. 小声读 → 应提示 `too_short` 或 `too_noisy`；正常读 → 显示基准，保存。
3. 日志中有 `loaded RNNoise from …`、`calibration accepted on … (denoise on)`、`mic baseline for …: +x.x dB (denoise on)`。
4. 开着风扇或敲键盘时 PTT，对端听到的背景噪声明显减弱。
5. 设置里关掉「麦克风降噪」保存，应再次弹出校准框；校准后日志为 `(denoise off)`。
6. 设置里拉麦克风音量到 130% 保存，日志出现 `mic multiplier 100% -> 130% …`；重启后回到 100%。
7. 换一个没校准过的输入设备并保存，应再次弹出校准框。
8. xpc / msfs 重复 1–7（设置文件为 `xpc_settings.json`）。

- [ ] **Step 4: 提交并清理**

```bash
git add CLAUDE.md docs/superpowers/specs/2026-09-26-mic-auto-calibration-design.md
git commit -m "docs: 麦克风音量自动校准与降噪已实施"
rm -rf .temp
```
