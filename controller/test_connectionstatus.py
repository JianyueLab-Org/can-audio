from types import SimpleNamespace
import unittest

from connectionstatus import snapshot


class ConnectionStatusTest(unittest.TestCase):
    def client(self, udp=False, count=2):
        return SimpleNamespace(connected=True, running=True,
            mumble=SimpleNamespace(ping_stats={'nb': count}, udp_ping_stats={'nb': count}),
            transport_status=lambda: ('UDP', 23.5) if udp else ('TCP', 41.2))

    def test_transport_uses_active_channel_rtt(self):
        self.assertEqual(snapshot(self.client())['latency_ms'], 41.2)
        self.assertEqual(snapshot(self.client(True))['transport'], 'UDP')
        self.assertEqual(snapshot(self.client(True))['latency_ms'], 23.5)

    def test_default_ping_average_is_not_a_measured_latency(self):
        self.assertIsNone(snapshot(self.client(count=0))['latency_ms'])

    def test_disconnected_has_no_stale_transport(self):
        client = self.client(True)
        client.connected = False
        self.assertEqual(snapshot(client), dict(connected=False, running=True, transport=None, latency_ms=None))
        self.assertEqual(snapshot(None), dict(connected=False, running=False, transport=None, latency_ms=None))
