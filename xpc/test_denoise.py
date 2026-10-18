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
