"""Controller endpoint persistence and transport forwarding."""

import os
import sys
from pathlib import Path
import tempfile
import unittest
from unittest import mock

sys.modules.setdefault("pyaudio", mock.MagicMock())
import test_voice  # supplies native audio stubs
import settings
import voice


class ServerEndpointTest(unittest.TestCase):
    def test_default_and_custom_endpoint_round_trip(self):
        scratch = Path(__file__).resolve().parents[1] / ".temp"
        scratch.mkdir(exist_ok=True)
        previous = os.getcwd()
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            try:
                os.chdir(directory)
                config = settings.Settings()
                self.assertEqual((getattr(config, "mumble_host", None),
                                  getattr(config, "mumble_port", None)),
                                 ("audio.ceruleanavi.net", 64738))
                config.mumble_host = "custom.example"
                config.mumble_port = 65000
                config.save_settings()
                loaded = settings.Settings()
                self.assertEqual((loaded.mumble_host, loaded.mumble_port),
                                 ("custom.example", 65000))
            finally:
                os.chdir(previous)

    def test_selected_port_reaches_mumble(self):
        client = voice.VoiceClient("custom.example", "123", "password", port=65000)
        with mock.patch.object(voice, "bounded_mumble") as factory, \
                mock.patch.object(voice.pyaudio, "PyAudio"), \
                mock.patch.object(client, "_find_best_sample_rate", return_value=48000), \
                mock.patch.object(client, "setup_audio"):
            mumble = factory.return_value.return_value
            mumble.start.side_effect = lambda: setattr(client, "connected", True)
            self.assertTrue(client.connect())
            self.assertEqual(factory.return_value.call_args.kwargs["port"], 65000)


if __name__ == "__main__":
    unittest.main()
