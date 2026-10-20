"""服务器目录的协议和错误回退。"""
import json
import unittest
from unittest.mock import patch

import serverdirectory


class DirectoryTest(unittest.TestCase):
    def payload(self):
        return {"status": 200, "data": {
            "audio": [{"id": 1, "kind": "audio", "name": "Tokyo",
                       "host": "audio.example.org", "port": 64739,
                       "location": "JP", "capabilities": []}],
            "fsd": [{"id": 2, "kind": "fsd", "name": "FSD",
                     "host": "fsd.example.org", "port": 6809,
                     "tlsPort": 6808, "capabilities": []}],
            "voice": [{"kind": "voice", "name": "QUIC",
                       "host": "quic.example.org", "port": 64738}],
            "datafeed": [], "automation": []}, "timestamp": "2026-10-19T00:00:00Z"}

    def test_audio_and_fsd_are_separate_and_use_actual_ports(self):
        result = serverdirectory.parse(self.payload())
        self.assertEqual([(s.name, s.host, s.port) for s in result["audio"]],
                         [("Tokyo", "audio.example.org", 64739)])
        self.assertEqual(result["fsd"][0].port, 6809)
        self.assertNotIn("voice", result)

    def test_bad_entries_do_not_hide_good_entries(self):
        payload = self.payload()
        payload["data"]["audio"] += [None, {}, {"kind": "voice", "name": "bad",
            "host": "wrong.example.org", "port": 64738}]
        for bad_port in (None, 0, 65536, True, "64738"):
            payload["data"]["audio"].append({"kind": "audio", "name": "bad",
                "host": "bad.example.org", "port": bad_port})
        result = serverdirectory.parse(payload)
        self.assertEqual(len(result["audio"]), 1)

    def test_error_envelope_and_malformed_groups_fail_safely(self):
        for payload in (None, [], {"status": 500, "data": {}},
                        {"status": 200, "data": []}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                serverdirectory.parse(payload)
        self.assertEqual(serverdirectory.parse({"status": 200, "data": {}}),
                         {"audio": [], "fsd": []})

    def test_fetch_handles_network_and_invalid_json_failures(self):
        with patch("serverdirectory.urllib.request.urlopen", side_effect=OSError("offline")):
            self.assertIsNone(serverdirectory.fetch())
        with patch("serverdirectory.urllib.request.urlopen") as opener:
            opener.return_value.__enter__.return_value.read.return_value = b"<html>error</html>"
            self.assertIsNone(serverdirectory.fetch())

    def test_fetch_parses_real_api_envelope(self):
        with patch("serverdirectory.urllib.request.urlopen") as opener:
            opener.return_value.__enter__.return_value.read.return_value = json.dumps(self.payload()).encode()
            self.assertEqual(serverdirectory.fetch()["audio"][0].port, 64739)


if __name__ == "__main__":
    unittest.main()
