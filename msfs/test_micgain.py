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
