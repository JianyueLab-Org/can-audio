"""启动前认证边界；真实网络准入见 smoke_admission.py。"""
from contextlib import closing
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import admission


class AdmissionConfigTest(unittest.TestCase):
    def setUp(self):
        scratch = Path(os.environ.get(
            "CAN_AUDIO_TEST_WORKDIR", Path(__file__).resolve().parents[1] / ".temp" / "voice-admission"))
        scratch.mkdir(parents=True, exist_ok=True)
        self.work = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.work.cleanup)
        self.root = Path(self.work.name)
        self.ini = self.root / "mumble.ini"
        self.db = self.root / "mumble.sqlite"

    def configure(self, extra="", database=None):
        self.ini.write_text(f"database={database or self.db}\n{extra}", encoding="utf-8")

    def stored_override(self, value, server=1):
        with closing(sqlite3.connect(self.db)) as connection, connection:
            connection.execute('CREATE TABLE IF NOT EXISTS config (server_id INTEGER, key TEXT, value TEXT)')
            connection.execute('INSERT INTO config VALUES (?, ?, ?)',
                               (server, "forceExternalAuth", value))

    def test_fresh_database_is_guarded_before_any_server_start(self):
        self.configure("forceExternalAuth=false\n[Ice]\nIce.Warn.UnknownProperties=1\n")
        admission.prepare(self.ini)
        root = admission.root_settings(self.ini.read_text())
        self.assertEqual(root["forceExternalAuth"], "true")
        self.assertFalse(self.db.exists(), "preflight must not create a differently owned database")
        self.assertIn("[Ice]\nIce.Warn.UnknownProperties=1", self.ini.read_text())

    def test_unsafe_persisted_override_prevents_launch_without_rewriting_member_data(self):
        self.configure()
        self.stored_override("false")
        before = self.db.read_bytes()
        with self.assertRaises(admission.UnsafeAdmission):
            admission.prepare(self.ini)
        self.assertEqual(self.db.read_bytes(), before)

    def test_other_virtual_servers_cannot_reopen_the_public_boundary(self):
        self.configure()
        self.stored_override("0", server=2)
        with self.assertRaises(admission.UnsafeAdmission):
            admission.prepare(self.ini)

    def test_safe_persisted_override_and_relative_database_are_supported(self):
        self.configure(database="mumble.sqlite")
        self.stored_override("true")
        admission.prepare(self.ini)
        self.assertEqual(admission.root_settings(self.ini.read_text())["forceExternalAuth"], "true")

    def test_symlinked_ini_checks_the_database_in_the_supplied_directory(self):
        physical = self.root / "physical"
        physical.mkdir()
        logical = self.root / "logical"
        logical.mkdir()
        self.ini = physical / "mumble.ini"
        self.db = logical / "mumble.sqlite"
        self.configure(database="mumble.sqlite")
        self.stored_override("false")
        selected = logical / "mumble.ini"
        selected.symlink_to(self.ini)
        before = self.ini.read_bytes()
        with self.assertRaises(admission.UnsafeAdmission):
            admission.prepare(selected)
        self.assertEqual(self.ini.read_bytes(), before)

    def test_symlinked_ini_retains_a_safe_relative_database(self):
        physical = self.root / "physical"
        physical.mkdir()
        logical = self.root / "logical"
        logical.mkdir()
        self.ini = physical / "mumble.ini"
        self.db = logical / "mumble.sqlite"
        self.configure(database="mumble.sqlite")
        self.stored_override("true")
        selected = logical / "mumble.ini"
        selected.symlink_to(self.ini)
        before = self.db.read_bytes()
        admission.prepare(selected)
        self.assertEqual(self.db.read_bytes(), before)
        self.assertEqual(admission.root_settings(self.ini.read_text())["forceExternalAuth"], "true")

    def test_decorated_general_aliases_cannot_disable_native_authentication(self):
        for header in ("[General] ; comment", "[General] # comment",
                       "[General]trailing-text", "[ General ]"):
            with self.subTest(header=header):
                self.configure(header + "\nforceExternalAuth=false\n")
                before = self.ini.read_bytes()
                with self.assertRaises(admission.UnsafeAdmission):
                    admission.prepare(self.ini)
                self.assertEqual(self.ini.read_bytes(), before)

    def test_unknown_or_old_murmur_cannot_start_with_an_unverified_control(self):
        for version in ("", "1.4.287", "1.5.0", "unknown"):
            with self.subTest(version=version):
                with self.assertRaises(admission.UnsafeAdmission):
                    admission.require_supported_version(version)
        admission.require_supported_version("1:1.5.735-1build1")

    def test_uninspectable_database_never_falls_back_to_a_default(self):
        self.configure()
        self.db.write_bytes(b"not a sqlite database")
        with self.assertRaises(admission.UnsafeAdmission):
            admission.prepare(self.ini)

    def test_database_metadata_failure_cannot_skip_an_unsafe_persisted_override(self):
        self.configure()
        self.stored_override("false")
        before = self.ini.read_bytes()
        original_stat = os.stat

        def metadata(path, *args, **kwargs):
            if path == self.db or path == str(self.db):
                raise PermissionError("database metadata is inaccessible")
            return original_stat(path, *args, **kwargs)

        with patch("os.stat", metadata):
            with self.assertRaises(PermissionError):
                admission.prepare(self.ini)
        self.assertEqual(self.ini.read_bytes(), before)

    def test_ambiguous_or_external_database_configuration_prevents_launch(self):
        for extra in ("dbDriver=QMYSQL\n", "database=\n", "dbPrefix=bad-prefix\n",
                      "[General]\nforceExternalAuth=false\n", "%66orceExternalAuth=false\n"):
            with self.subTest(extra=extra):
                self.configure(extra)
                with self.assertRaises(admission.UnsafeAdmission):
                    admission.prepare(self.ini)


if __name__ == "__main__":
    unittest.main()
