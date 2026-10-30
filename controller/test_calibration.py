"""无界面校准：用替身麦克风喂合成信号，不需要音频设备。"""

import math
import threading
import unittest

import numpy as np

import calibration
import micgain

RATE = 48000
CHUNK = 960


def tone(dbfs, length=CHUNK):
    amplitude = micgain.FULL_SCALE * 10 ** (dbfs / 20.0) * math.sqrt(2.0)
    x = amplitude * np.sin(2 * math.pi * 440.0 * np.arange(length) / RATE)
    return np.clip(np.round(x), -32768, 32767).astype(np.int16)


class FakeSource:
    """前 50 帧底噪，之后按 levels 依次给帧；读完了一直给底噪。"""

    def __init__(self, levels, gate=None):
        self.rate, self.chunk = RATE, CHUNK
        self.levels = [-70.0] * 50 + list(levels)
        self.gate = gate
        self.closed = False
        self.reads = 0

    def read(self):
        if self.gate is not None and self.reads == 10:
            self.gate.wait(5)
        level = self.levels[self.reads] if self.reads < len(self.levels) else -70.0
        self.reads += 1
        return tone(level)

    def close(self):
        self.closed = True


def run(source, denoise_on=False, denoiser_factory=None):
    events = []
    done = threading.Event()

    def on_event(event):
        events.append(event)
        if event['stage'] in ('done', 'error', 'cancelled'):
            done.set()

    c = calibration.Calibration(None, denoise_on, on_event, source_factory=lambda _: source,
                                denoiser_factory=denoiser_factory)
    c.start()
    return c, events, done


class CalibrationTest(unittest.TestCase):
    def test_quiet_speech_is_boosted_to_target(self):
        source = FakeSource([-35.0] * 300)
        _, events, done = run(source)
        self.assertTrue(done.wait(10))
        final = events[-1]
        self.assertEqual(final['stage'], 'done')
        self.assertIsNone(final['reason'])
        self.assertAlmostEqual(final['gain_db'], 15.0, delta=0.2)
        self.assertAlmostEqual(final['speech_seconds'], 6.0, places=2)
        self.assertEqual(source.reads, 450)    # 1 s + 8 s，按帧数不按墙钟
        self.assertTrue(source.closed)
        stages = [e['stage'] for e in events]
        self.assertEqual(stages[0], 'quiet')
        self.assertIn('read', stages)
        self.assertLess(stages.index('read'), len(stages) - 1)

    def test_too_short_is_rejected(self):
        _, events, done = run(FakeSource([-30.0] * 50))
        self.assertTrue(done.wait(10))
        self.assertEqual(events[-1]['reason'], 'too_short')
        self.assertIsNone(events[-1]['gain_db'])

    def test_clipping_is_judged_on_raw_frames(self):
        class Flatten:
            """把一切压到 -30 dBFS 的“降噪”：处理后的帧不再削波。"""
            def __init__(self, rate):
                self.closed = False

            def process(self, samples):
                return tone(-30.0, len(samples))

            def close(self):
                self.closed = True

        original = calibration.denoise.effective
        calibration.denoise.effective = lambda setting: bool(setting)
        try:
            _, events, done = run(FakeSource([3.0] * 400), denoise_on=True, denoiser_factory=Flatten)
        finally:
            calibration.denoise.effective = original
        self.assertTrue(done.wait(10))
        self.assertEqual(events[-1]['reason'], 'clipping')
        self.assertTrue(events[-1]['denoise'])

    def test_cancel_stops_and_closes(self):
        gate = threading.Event()
        source = FakeSource([-30.0] * 400, gate=gate)
        c, events, done = run(source)
        c.cancel(wait=False)
        gate.set()
        self.assertTrue(done.wait(10))
        self.assertEqual(events[-1]['stage'], 'cancelled')
        c.cancel()
        self.assertTrue(source.closed)
        self.assertFalse(c.running)

    def test_open_failure_is_reported(self):
        def broken(_):
            raise OSError('no microphone')
        events = []
        c = calibration.Calibration(None, False, events.append, source_factory=broken)
        c.start()
        c._thread.join(5)
        self.assertEqual(events, [{'stage': 'error', 'message': 'no microphone'}])


if __name__ == '__main__':
    unittest.main()
