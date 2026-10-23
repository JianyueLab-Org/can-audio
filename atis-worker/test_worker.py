"""Tests for the CAN ATIS voice worker. No server, audio device or network needed."""
import argparse
import os
import subprocess
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import can_mumble_worker as worker  # noqa: E402


class TransportTests(unittest.TestCase):
    def test_udp_uses_udp_rtt_and_tcp_fallback_uses_tcp_rtt(self):
        connection = SimpleNamespace(udp_active=True,
                                     udp_ping_stats={"nb": 2, "avg": 12.5},
                                     ping_stats={"nb": 3, "avg": 42.0})
        self.assertEqual(worker.transport_status(connection), ("UDP", 12.5))
        connection.udp_active = False
        self.assertEqual(worker.transport_status(connection), ("TCP", 42.0))

    def test_no_reply_does_not_show_library_default_as_measured_latency(self):
        connection = SimpleNamespace(ping_stats={"nb": 0, "avg": 40.0})
        self.assertEqual(worker.transport_status(connection), ("TCP", None))

    def test_invalid_latency_is_not_reported(self):
        for value in (-1, float("nan"), float("inf")):
            connection = SimpleNamespace(ping_stats={"nb": 1, "avg": value})
            self.assertEqual(worker.transport_status(connection), ("TCP", None))


class FrequencyTests(unittest.TestCase):
    def test_hz_from_can_atis_maps_to_the_controllers_channel(self):
        """can-atis passes AtisStation.Frequency, which is Hz: 127800000 -> FREQ_127800."""
        self.assertEqual(worker.frequency_khz("127800000"), 127800)
        self.assertEqual(worker.frequency_khz("118025000"), 118025)

    def test_mhz_is_still_accepted(self):
        self.assertEqual(worker.frequency_khz("127.8"), 127800)
        self.assertEqual(worker.frequency_khz("118.025"), 118025)
        self.assertEqual(worker.frequency_khz("121.5"), 121500)

    def test_hz_and_mhz_give_the_same_channel(self):
        for mhz, hz in (("118.0", "118000000"), ("136.975", "136975000"), ("124.35", "124350000")):
            self.assertEqual(worker.frequency_khz(mhz), worker.frequency_khz(hz))

    def test_ambiguous_or_invalid_values_are_refused(self):
        for value in ("127800", "1000", "0", "-127.8", "nan", "inf", "", "abc", "1000000000"):
            with self.assertRaises(argparse.ArgumentTypeError, msg=value):
                worker.frequency_khz(value)

    def test_bad_frequency_exits_before_connecting(self):
        """A bad --frequency must fail in argparse, not open a Mumble connection."""
        result = subprocess.run(
            [sys.executable, worker.__file__, "--host", "127.0.0.1", "--port", "1",
             "--cid", "1", "--callsign", "TEST_ATIS", "--frequency", "127800"],
            input=b"", capture_output=True, timeout=60)
        self.assertEqual(result.returncode, 2)
        self.assertIn(b"Hz or MHz", result.stderr)
        self.assertNotIn(b"connecting", result.stdout)


class SelfTestTests(unittest.TestCase):
    def test_self_test_reports_ready_on_stdout(self):
        """The build script runs --self-test on the packaged exe; it must print ready and exit 0."""
        result = subprocess.run([sys.executable, worker.__file__, "--self-test"],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"state": "ready"', result.stdout)


class TlsPinTests(unittest.TestCase):
    def test_importing_the_worker_pins_the_voice_server_certificate(self):
        """The worker sends the member's password; the fingerprint pin must be live."""
        import mumblecompat
        self.assertTrue(mumblecompat.tls_pinned())


if __name__ == "__main__":
    unittest.main()
