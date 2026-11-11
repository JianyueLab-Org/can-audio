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
        calls = [c for c in client.audio.open.call_args_list
                 if c.kwargs.get('output') and c.kwargs.get('start', True)]
        self.assertEqual(calls[0].kwargs['stream_callback'], client._playback_callback)

    def test_fallback_rate_consumes_correct_duration(self):
        client = make_client()
        client.OUTPUT_RATE = 44100
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


class FakeAudio:
    """只接受各自支持的采样率的 PyAudio 替身，拒绝时和 PortAudio 一样报 -9997。"""

    def __init__(self, input_rates, output_rates, native_input=44100.0, native_output=48000.0):
        self.input_rates = set(input_rates)
        self.output_rates = set(output_rates)
        self.native = {True: native_input, False: native_output}
        self.opened = []

    def open(self, **kwargs):
        allowed = self.input_rates if kwargs.get('input') else self.output_rates
        if kwargs['rate'] not in allowed:
            raise OSError(-9997, 'Invalid sample rate')
        self.opened.append(kwargs)
        return mock.Mock()

    def get_default_input_device_info(self):
        return {'defaultSampleRate': self.native[True]}

    def get_default_output_device_info(self):
        return {'defaultSampleRate': self.native[False]}

    def get_device_info_by_index(self, index):
        return {'defaultSampleRate': self.native[index == 'in']}


class SampleRateProbeTest(unittest.TestCase):
    """issue #1：麦克风能开 48 kHz、扬声器不能时，连接报 Invalid sample rate。"""

    def streams(self, audio):
        started = [c for c in audio.opened if c.get('start', True)]
        return ([c for c in started if c.get('input')][-1],
                [c for c in started if c.get('output')][-1])

    def test_output_rate_is_probed_on_its_own(self):
        client = make_client()
        client.audio = FakeAudio(input_rates={48000}, output_rates={44100})
        client.setup_audio()
        mic, speaker = self.streams(client.audio)
        self.assertEqual(mic['rate'], 48000)
        self.assertEqual(speaker['rate'], 44100)
        self.assertEqual(client.OUTPUT_RATE, 44100)

    def test_device_native_rate_is_tried_last(self):
        client = make_client()
        client.audio = FakeAudio(input_rates={48000}, output_rates={96000},
                                 native_output=96000.0)
        client.setup_audio()
        _, speaker = self.streams(client.audio)
        self.assertEqual(speaker['rate'], 96000)
        rxmix.enqueue(client._rx_mix_frames, 1, np.full(960, 50, np.int16))
        data, _ = client._playback_callback(None, 1920, {}, 0)
        np.testing.assert_array_equal(np.frombuffer(data, np.int16), np.full(1920, 50))

    def test_changing_devices_probes_again(self):
        client = make_client()
        client.audio = FakeAudio(input_rates={48000}, output_rates={48000})
        client.setup_audio()
        client.audio.output_rates = {32000}
        client.setup_audio(output_device='other')
        _, speaker = self.streams(client.audio)
        self.assertEqual(speaker['rate'], 32000)

    def test_mic_at_a_fallback_rate_is_sent_at_48k(self):
        client = make_client()
        client.audio = FakeAudio(input_rates={44100}, output_rates={48000})
        client.setup_audio()
        self.assertEqual(client.RATE, 44100)
        self.assertEqual(client.CHUNK, 882)
        out = client._process_mic(np.full(882, 1000, np.int16).tobytes())
        self.assertEqual(out.size, 960, "pymumble 只认 48 kHz，不重采样会变调")


if __name__ == '__main__':
    unittest.main()
