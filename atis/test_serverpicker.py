"""目录刷新、自定义地址和连接锁定的 GUI 行为。"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import time
import threading
import unittest
from unittest.mock import patch

from PyQt6.QtWidgets import QApplication
import serverdirectory
import serverpicker


class PickerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.picker = serverpicker.ServerPicker("audio", "custom.example.org", 64800,
                                                auto_refresh=False)
        self.addCleanup(self.picker.close)

    def loaded(self):
        self.picker._loaded({"audio": [serverdirectory.Server("Tokyo", "audio.example.org", 64739)]})

    def test_refresh_preserves_endpoint_and_selection_uses_api_port(self):
        self.loaded()
        self.assertEqual((self.picker.host(), self.picker.port()), ("custom.example.org", 64800))
        self.picker.server_combo.setCurrentIndex(1)
        self.assertEqual((self.picker.host(), self.picker.port()), ("audio.example.org", 64739))
        self.picker.server_combo.setCurrentIndex(0)
        self.assertEqual((self.picker.host(), self.picker.port()), ("custom.example.org", 64800))

    def test_failure_keeps_previous_list_and_custom_endpoint(self):
        self.loaded()
        self.picker._loaded(None)
        self.assertEqual(self.picker.server_combo.count(), 2)
        self.assertEqual(self.picker.host(), "custom.example.org")

    def test_locked_selection_cannot_change_endpoint_after_async_result(self):
        self.picker.set_locked(True)
        self.loaded()
        self.assertFalse(self.picker.server_combo.isEnabled())
        self.assertFalse(self.picker.refresh_button.isEnabled())
        self.assertFalse(self.picker.host_edit.isEnabled())
        self.picker.server_combo.setCurrentIndex(1)
        self.assertEqual(self.picker.host(), "custom.example.org")
        self.picker.set_locked(False)
        self.assertTrue(self.picker.host_edit.isEnabled())

    def test_background_fetch_does_not_block_and_delivers_on_gui_thread(self):
        release = threading.Event()
        started = threading.Event()
        finished = threading.Event()
        def fetch():
            started.set()
            release.wait(2)
            finished.set()
            return {"audio": [serverdirectory.Server("Tokyo", "audio.example.org", 64739)]}
        with patch("serverdirectory.fetch", side_effect=fetch):
            self.picker.refresh()
            self.assertTrue(started.wait(1))
            self.assertFalse(finished.is_set())
            release.set()
            deadline = time.monotonic() + 2
            while self.picker.server_combo.count() == 1 and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(.005)
            self.assertEqual(self.picker.server_combo.count(), 2)
            self.assertEqual(self.picker.host(), "custom.example.org")


if __name__ == "__main__":
    unittest.main()
