"""声卡回调不依赖 Python 定时器，兼容可变帧长和多人混音。"""
import unittest
import numpy as np
from test_voice import make_client
import rxmix
from unittest import mock


class PlaybackTest(unittest.TestCase):
    def test_output_open_registers_callback(self):
        client = make_client()
        client.audio = mock.Mock()
        client.setup_audio()
        calls = [c for c in client.audio.open.call_args_list if c.kwargs.get('output')]
        self.assertEqual(calls[0].kwargs['stream_callback'], client._playback_callback)

    def test_fallback_rate_consumes_correct_duration(self):
        client = make_client()
        client.RATE = 44100
        rxmix.enqueue(client._rx_mix_frames, 1, np.full(960, 123, np.int16))
        data, _ = client._playback_callback(None, 882, {}, 0)
        np.testing.assert_array_equal(np.frombuffer(data, np.int16), np.full(882, 123))
        self.assertEqual(client._rx_mix_frames, {})

    def test_variable_device_requests_preserve_all_samples(self):
        client = make_client()
        samples = np.arange(5760, dtype=np.int16)
        rxmix.enqueue(client._rx_mix_frames, 1, samples)
        rxmix.enqueue(client._rx_mix_frames, 2, np.full(5760, 100, np.int16))
        result = []
        for size in (480, 1920, 960, 2400):
            data, _ = client._playback_callback(None, size, {}, 0)
            self.assertEqual(len(data), size * 2)
            result.append(np.frombuffer(data, np.int16))
        np.testing.assert_array_equal(np.concatenate(result), samples + 100)

    def test_idle_returns_exact_silence_without_stream_write(self):
        client = make_client()
        for size in (480, 960, 1920):
            data, _ = client._playback_callback(None, size, {}, 0)
            self.assertEqual(data, bytes(size * 2))

    def test_ten_minutes_never_grows_queue(self):
        client = make_client()
        a = np.full(2880, 100, np.int16)
        b = np.full(480, 300, np.int16)
        for tick in range(30000):
            if tick % 3 == 0:
                rxmix.enqueue(client._rx_mix_frames, 1, a)
            rxmix.enqueue(client._rx_mix_frames, 2, b)
            rxmix.enqueue(client._rx_mix_frames, 2, b)
            data, _ = client._playback_callback(None, 960, {}, 0)
            self.assertEqual(data, np.full(960, 400, np.int16).tobytes())
        self.assertEqual(client._rx_mix_frames, {})
