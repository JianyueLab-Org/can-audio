"""Persisted pilot endpoints and the voice transport destination."""

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import settings


def _prepare_audio_imports():
    try:
        import opuslib
    except Exception:
        for name in ("opuslib", "opuslib.api", "opuslib.api.decoder",
                     "opuslib.api.encoder", "opuslib.api.info", "opuslib.exceptions"):
            sys.modules.setdefault(name, mock.MagicMock())
    try:
        import pyaudio
    except ImportError:
        sys.modules.setdefault("pyaudio", mock.MagicMock())


class EndpointSettingsTest(unittest.TestCase):
    def setUp(self):
        scratch = Path(__file__).resolve().parents[1] / ".temp"
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "settings.json"

    def test_old_settings_get_default_voice_port(self):
        self.path.write_text(json.dumps({"mumble_host": "intranet.example"}))
        loaded = settings.Settings(str(self.path))
        self.assertEqual(loaded.mumble_host, "intranet.example")
        self.assertEqual(getattr(loaded, "mumble_port", None), 64738)

    def test_custom_endpoint_survives_save_and_reload(self):
        saved = settings.Settings(str(self.path))
        saved.mumble_host = "voice.intranet.example"
        saved.mumble_port = 65000
        saved.fsd_host = "fsd.intranet.example"
        saved.fsd_port = 6900
        saved.save()
        loaded = settings.Settings(str(self.path))
        self.assertEqual((loaded.mumble_host, loaded.mumble_port),
                         ("voice.intranet.example", 65000))
        self.assertEqual((loaded.fsd_host, loaded.fsd_port),
                         ("fsd.intranet.example", 6900))

    def test_selected_voice_port_reaches_mumble_transport(self):
        _prepare_audio_imports()
        import voice
        destinations = []

        def connect(host, username, **kwargs):
            destinations.append((host, kwargs.get("port")))
            raise RuntimeError("test transport stops before opening a socket")

        radio = voice.Voice("voice.intranet.example", "1000", port=65000)
        with mock.patch.object(radio, "_open_audio"), \
                mock.patch.object(radio, "_release"), \
                mock.patch.object(voice, "bounded_mumble", return_value=connect):
            radio.start()
        self.assertEqual(destinations, [("voice.intranet.example", 65000)])


class NetworkPickerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        _prepare_audio_imports()
        from PyQt6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.fetch = mock.patch("serverdirectory.fetch", return_value={})
        self.fetch.start()
        self.addCleanup(self.fetch.stop)

    def test_network_selectors_lock_for_either_connection_object(self):
        import gui
        from PyQt6.QtWidgets import QDialog, QWidget
        for voice, fsd in ((None, None), (object(), None), (None, object())):
            with self.subTest(voice=voice is not None, fsd=fsd is not None):
                parent = QWidget()
                parent.voice, parent.fsd = voice, fsd
                dialog = QDialog(parent)
                dialog.settings = settings.Settings("missing-test-settings.json")
                page = gui.SettingsDialog._network_tab(dialog)
                self.assertTrue(hasattr(dialog, "mumble_picker"))
                self.assertTrue(hasattr(dialog, "fsd_picker"))
                unlocked = voice is None and fsd is None
                self.assertEqual(dialog.mumble_picker.host_edit.isEnabled(), unlocked)
                self.assertEqual(dialog.mumble_picker.port_edit.isEnabled(), unlocked)
                self.assertEqual(dialog.fsd_picker.host_edit.isEnabled(), unlocked)
                self.assertEqual(dialog.fsd_picker.port_edit.isEnabled(), unlocked)
                page.deleteLater()
                parent.deleteLater()
