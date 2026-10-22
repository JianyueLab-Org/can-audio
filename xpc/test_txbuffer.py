"""发射队列过载后保留新音频，不积累无限延迟。"""
import pathlib
import threading
import types
import unittest

import txbuffer


class TransmitBufferTest(unittest.TestCase):
    def output(self):
        return types.SimpleNamespace(encoder_framesize=0.02, channels=1,
                                     lock=threading.Lock(), pcm=[])

    def test_long_stall_keeps_only_latest_twelve_frames(self):
        output = self.output()
        dropped = 0
        for i in range(1000):
            dropped += txbuffer.enqueue(output, bytes([i % 256]) * 1920)
        self.assertEqual(len(output.pcm), 12)
        self.assertEqual(output.pcm[0], bytes([988 % 256]) * 1920)
        self.assertEqual(output.pcm[-1], bytes([999 % 256]) * 1920)
        self.assertAlmostEqual(dropped, 988 * 0.02)

    def test_partial_frames_join_without_dropping_samples(self):
        output = self.output()
        txbuffer.enqueue(output, b'\x01\x00' * 240)
        txbuffer.enqueue(output, b'\x02\x00' * 960)
        self.assertEqual(list(map(len, output.pcm)), [1920, 480])
        self.assertEqual(b''.join(output.pcm), b'\x01\x00' * 240 + b'\x02\x00' * 960)

    def test_all_three_copies_agree(self):
        root = pathlib.Path(__file__).resolve().parent.parent
        for component in ('controller', 'msfs'):
            self.assertEqual((root / component / 'txbuffer.py').read_bytes(),
                             pathlib.Path(txbuffer.__file__).read_bytes())


if __name__ == '__main__':
    unittest.main()
