"""mic_calibration、mic_denoise 落盘；mic_volume 是本次会话的乘数，不落盘。"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.modules.setdefault("pyaudio", mock.MagicMock())

import denoise
import settings as settings_module


class MicCalibrationSettingsTest(unittest.TestCase):

    def setUp(self):
        self._cwd = os.getcwd()
        os.chdir(tempfile.mkdtemp(prefix="can-settings-"))
        self._available = denoise.available
        denoise.available = lambda: True

    def tearDown(self):
        denoise.available = self._available
        os.chdir(self._cwd)

    def test_calibration_and_denoise_round_trip(self):
        s = settings_module.Settings()
        s.mic_calibration["Mic A"] = {"gain_db": 8.5, "denoise": True}
        s.mic_denoise = True
        s.save_settings()
        again = settings_module.Settings()
        self.assertEqual(again.mic_calibration, s.mic_calibration)
        self.assertTrue(again.mic_denoise)
        self.assertEqual(again.baseline_for("Mic A"), 8.5)
        self.assertIsNone(again.baseline_for("Mic B"))

    def test_denoise_defaults_on_and_can_be_turned_off(self):
        self.assertTrue(settings_module.Settings().mic_denoise)
        s = settings_module.Settings()
        s.mic_denoise = False
        s.save_settings()
        self.assertFalse(settings_module.Settings().mic_denoise)

    def test_toggling_denoise_invalidates_the_baseline(self):
        s = settings_module.Settings()
        s.mic_calibration["Mic A"] = {"gain_db": 8.5, "denoise": True}
        s.mic_denoise = False
        self.assertIsNone(s.baseline_for("Mic A"))

    def test_missing_library_means_denoise_inactive(self):
        denoise.available = lambda: False
        s = settings_module.Settings()
        s.mic_calibration["Mic A"] = {"gain_db": 4.0, "denoise": False}
        self.assertFalse(s.denoise_active())
        self.assertEqual(s.baseline_for("Mic A"), 4.0)

    def test_mic_volume_is_not_persisted(self):
        s = settings_module.Settings()
        s.mic_volume = 150
        s.save_settings()
        with open(s.config_file, encoding="utf-8") as f:
            self.assertNotIn("mic_volume", json.load(f))
        self.assertEqual(settings_module.Settings().mic_volume, 100)

    def test_old_mic_volume_is_ignored(self):
        with open("radio_settings.json", "w", encoding="utf-8") as f:
            json.dump({"mic_volume": 170}, f)
        self.assertEqual(settings_module.Settings().mic_volume, 100)

    def test_corrupt_calibration_becomes_empty(self):
        with open("radio_settings.json", "w", encoding="utf-8") as f:
            json.dump({"mic_calibration": [1, 2]}, f)
        self.assertEqual(settings_module.Settings().mic_calibration, {})


if __name__ == "__main__":
    unittest.main()
