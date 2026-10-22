"""不同分包、多人同时说话时，按时间片保留完整音频。"""
import pathlib
import threading
import types
import unittest

import numpy as np

import rxmix


class ReceiveMixTest(unittest.TestCase):
    def test_ten_minutes_of_two_speakers_do_not_accumulate_latency(self):
        queues = {}
        a = np.full(2880, 100, np.int16)
        b = np.full(480, 300, np.int16)
        peak = 0
        for tick in range(30000):
            if tick % 3 == 0:
                rxmix.enqueue(queues, 1, a)
            rxmix.enqueue(queues, 2, b)
            rxmix.enqueue(queues, 2, b)
            peak = max(peak, sum(map(len, queues.get(1, ()))))
            np.testing.assert_array_equal(rxmix.take(queues), np.full(960, 400))
        self.assertEqual(peak, 2880)
        self.assertEqual(queues, {})

    def test_different_packet_sizes_preserve_both_speakers(self):
        queues = {}
        # 一人发 60 ms 包，另一人发六个 10 ms 包。
        first = np.arange(2880, dtype=np.int16)
        rxmix.enqueue(queues, 1, first)
        for _ in range(6):
            rxmix.enqueue(queues, 2, np.full(480, 100, np.int16))
        actual = np.concatenate([rxmix.take(queues) for _ in range(3)])
        np.testing.assert_array_equal(actual, first + 100)
        self.assertEqual(queues, {})

    def test_short_tail_does_not_truncate_other_speaker(self):
        queues = {}
        rxmix.enqueue(queues, 1, np.full(1920, 100, np.int16))
        rxmix.enqueue(queues, 2, np.full(240, 300, np.int16))
        frame = rxmix.take(queues)
        np.testing.assert_array_equal(frame[:240], np.full(240, 400))
        np.testing.assert_array_equal(frame[240:], np.full(720, 100))
        np.testing.assert_array_equal(rxmix.take(queues), np.full(960, 100))
        self.assertIsNone(rxmix.take(queues))

    def test_overflow_is_bounded_by_samples_not_packet_count(self):
        queues = {}
        rxmix.enqueue(queues, 1, np.arange(14000, dtype=np.int16))
        self.assertEqual(sum(map(len, queues[1])), 11520)
        self.assertEqual(rxmix.take(queues)[0], 2480)

    def test_clipping_never_wraps(self):
        np.testing.assert_array_equal(rxmix.mix([
            np.array([30000, -30000], np.int16),
            np.array([30000, -30000], np.int16)]), [32767, -32768])

    def test_fallback_output_keeps_frame_duration(self):
        frame = rxmix.output_frame(np.full(960, 1234, np.int16), 882)
        np.testing.assert_array_equal(frame, np.full(882, 1234))

    def test_all_clients_use_identical_mixer(self):
        root = pathlib.Path(__file__).resolve().parent.parent
        for component in ('controller', 'msfs'):
            self.assertEqual((root / component / 'rxmix.py').read_bytes(),
                             pathlib.Path(rxmix.__file__).read_bytes())

    def test_pilot_playback_splits_long_packets_before_device_write(self):
        import voice
        client = voice.Voice.__new__(voice.Voice)
        client.running = True
        client._chunk, client._rate = 960, 48000
        client._rx_mix_wakeup = threading.Condition()
        client._output_stream_lock = threading.Lock()
        client._rx_mix_frames = {}
        client._received_frames = client._received_total = 0
        rxmix.enqueue(client._rx_mix_frames, 1, np.full(2880, 100, np.int16))
        rxmix.enqueue(client._rx_mix_frames, 2, np.full(2880, 300, np.int16))
        written = []

        def write(pcm):
            written.append(np.frombuffer(pcm, np.int16))
            if len(written) == 3:
                client.running = False

        client._output = types.SimpleNamespace(write=write)
        thread = threading.Thread(target=client._rx_mix_loop, daemon=True)
        thread.start()
        thread.join(timeout=2)
        client.running = False
        with client._rx_mix_wakeup:
            client._rx_mix_wakeup.notify_all()
        thread.join(timeout=1)
        self.assertEqual(len(written), 3)
        for frame in written:
            np.testing.assert_array_equal(frame, np.full(960, 400))


if __name__ == '__main__':
    unittest.main()
