"""Tests for the CAN ATIS voice worker. No server, audio device or network needed."""
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
