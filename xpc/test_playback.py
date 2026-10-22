"""飞行员端声卡回调混音测试。"""
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

import numpy as np
import voice
import rxmix


class PlaybackTest(unittest.TestCase):
    def client(self):
        client = voice.Voice.__new__(voice.Voice)
        client._rate, client._chunk = 48000, 960
        client._rx_mix_wakeup = threading.Condition()
        client._rx_mix_frames = {}
        client._received_frames = client._received_total = 0
        return client

    def test_variable_requests_mix_without_truncation(self):
        client = self.client()
        samples = np.arange(5760, dtype=np.int16)
        rxmix.enqueue(client._rx_mix_frames, 1, samples)
        rxmix.enqueue(client._rx_mix_frames, 2, np.full(5760, 100, np.int16))
        result = []
        for size in (480, 1920, 960, 2400):
            data, _ = client._playback_callback(None, size, {}, 0)
            self.assertEqual(len(data), size * 2)
            result.append(np.frombuffer(data, np.int16))
        np.testing.assert_array_equal(np.concatenate(result), samples + 100)
        self.assertEqual(client._received_frames, 4)

    def test_silence_does_not_count_as_received_audio(self):
        client = self.client()
        data, _ = client._playback_callback(None, 1920, {}, 0)
        self.assertEqual(data, bytes(3840))
        self.assertEqual(client._received_total, 0)

    def test_fallback_rate_preserves_device_frame_length(self):
        client = self.client()
        client._rate = 44100
        rxmix.enqueue(client._rx_mix_frames, 1, np.full(960, 123, np.int16))
        data, _ = client._playback_callback(None, 882, {}, 0)
        np.testing.assert_array_equal(np.frombuffer(data, np.int16), np.full(882, 123))

    def test_open_audio_registers_device_callback(self):
        client = self.client()
        client.settings = SimpleNamespace()
        client._best_rate = lambda: 48000
        client._device_name = lambda *args: 'test-device'
        audio = mock.Mock()
        with mock.patch.object(voice.pyaudio, 'PyAudio', return_value=audio):
            client._open_audio()
        output = [call for call in audio.open.call_args_list if call.kwargs.get('output')]
        self.assertEqual(output[0].kwargs['stream_callback'], client._playback_callback)
