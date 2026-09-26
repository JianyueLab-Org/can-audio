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
