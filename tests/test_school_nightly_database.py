import importlib.util
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    obj = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(obj)
    return obj

transport = load("appfolder_yandex_school", "school_yandex_app_folder.py")
nightly = load("nightly_asa_school", "school-nightly-database-backup.py")


class SchoolNightlyDatabaseTests(unittest.TestCase):
    def test_window_only_between_3_and_5_moscow(self):
        for hour in (0, 1, 2, 5, 7, 15, 23):
            clock = datetime(2026, 11, 2, hour, 20,
                             tzinfo=nightly.MOSCOW)
            self.assertFalse(nightly.is_night(clock))
        for minute in (0, 19, 20, 59):
            self.assertTrue(nightly.is_night(
                datetime(2026, 11, 2, 3, minute, tzinfo=nightly.MOSCOW)
            ))
        self.assertTrue(nightly.is_night(datetime(
            2026, 11, 2, 4, 59, tzinfo=nightly.MOSCOW
        )))

    def test_scheduled_daytime_run_remains_blocked(self):
        with patch.object(nightly, "is_night", return_value=False), \
             patch.object(nightly, "preflight") as cloud, \
             patch.object(nightly, "export_asa") as exporter:
            with self.assertRaises(nightly.NightlyError):
                nightly.execute()
        cloud.assert_not_called()
        exporter.assert_not_called()

    def test_explicit_manual_daytime_run_reaches_preflight_without_export_until_ready(self):
        class PreflightStop(Exception):
            pass
        with patch.object(nightly, "is_night", return_value=False), \
             patch.object(nightly, "preflight", side_effect=PreflightStop) as cloud, \
             patch.object(nightly, "export_asa") as exporter:
            with self.assertRaises(PreflightStop):
                nightly.execute(manual=True)
        cloud.assert_called_once_with()
        exporter.assert_not_called()

    def test_manual_apply_runs_without_time_gate(self):
        with patch.object(nightly, "execute") as execute, \
             patch.object(sys, "argv", ["asa-school-backup-now", "--apply", "--manual"]):
            self.assertEqual(nightly.main(), 0)
        execute.assert_called_once_with(manual=True)

    def test_manual_without_apply_refused(self):
        with patch.object(nightly, "execute") as execute, \
             patch.object(sys, "argv", ["school-nightly-database-backup.py", "--manual"]):
            self.assertEqual(nightly.main(), 2)
        execute.assert_not_called()

    def test_scheduled_apply_does_not_escape_night(self):
        with patch.object(nightly, "execute") as execute, \
             patch.object(sys, "argv", ["school-nightly-database-backup.py", "--apply"]):
            self.assertEqual(nightly.main(), 0)
        execute.assert_called_once_with(manual=False)

    def test_cloud_path_is_separate_app_folder(self):
        self.assertEqual(
            transport.ROOT, "app:/School-VM100/Database-Backups"
        )
        self.assertEqual(nightly.HOST, "asa-school-01")
        self.assertEqual(nightly.ARCHIVES,
                         Path("/var/backups/asa-school-nightly/archives"))

    def test_immutable_archive_filename_rejects_path_traversal(self):
        good = "asa-school-01-20261102T002000Z-1a2b3c4d.tar.age"
        self.assertEqual(transport.validate_archive_name(good), good)
        for bad in ("../" + good, good + "/more", good + ".tmp",
                    "asa-school-02-20261102T002000Z-1a2b3c4d.tar.age",
                    "asa-school-01-20261102T002000Z-ABC12345.tar.age"):
            with self.subTest(bad=bad):
                with self.assertRaises(transport.BackupTransportError):
                    transport.validate_archive_name(bad)

    def test_signed_links_restricted_to_yandex_https_443(self):
        host, port, path = transport.validate_signed_link(
            "https://uploader44.disk.yandex.net:443/upload-target/test?key=abc"
        )
        self.assertEqual(host, "uploader44.disk.yandex.net")
        self.assertEqual(port, 443)
        self.assertEqual(path, "/upload-target/test?key=abc")
        for url in ("http://uploader.disk.yandex.net/upload",
                    "https://attacker.test/upload",
                    "https://evil-yandex.net/upload",
                    "https://uploader.disk.yandex.net:444/upload",
                    "https://user@uploader.disk.yandex.net/upload"):
            with self.subTest(url=url):
                with self.assertRaises(transport.BackupTransportError):
                    transport.validate_signed_link(url)

    def test_cipher_requires_root_mode_and_age_header(self):
        # The CI runner is intentionally non-root. Simulate root-owned file
        # metadata rather than weakening the real transport's owner check.
        import io
        import stat
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        contents=b"age-encryption.org/v1\n" + b"c" * 2048
        file=MagicMock(spec=Path)
        file.is_symlink.return_value=False
        file.is_file.return_value=True
        file.stat.return_value=SimpleNamespace(
            st_uid=0, st_mode=stat.S_IFREG | 0o600, st_size=len(contents))
        file.open.side_effect=lambda *args, **kwargs: io.BytesIO(contents)
        size, hash_value=transport.private_cipher(file)
        self.assertEqual(size,len(contents))
        self.assertEqual(hash_value,hashlib.sha256(contents).hexdigest())
        file.stat.return_value=SimpleNamespace(
            st_uid=1000, st_mode=stat.S_IFREG | 0o600, st_size=len(contents))
        with self.assertRaisesRegex(transport.BackupTransportError,
                                    "CIPHER_OWNER_MODE_UNTRUSTED"):
            transport.private_cipher(file)
        file.stat.return_value=SimpleNamespace(
            st_uid=0, st_mode=stat.S_IFREG | 0o644, st_size=len(contents))
        with self.assertRaisesRegex(transport.BackupTransportError,
                                    "CIPHER_OWNER_MODE_UNTRUSTED"):
            transport.private_cipher(file)
        file.stat.return_value=SimpleNamespace(
            st_uid=0, st_mode=stat.S_IFREG | 0o600, st_size=len(contents))
        file.open.side_effect=lambda *args, **kwargs: io.BytesIO(
            b"NOT_AGE_HEADER" + b"d" * 2048)
        with self.assertRaisesRegex(transport.BackupTransportError,
                                    "NOT_AGE_CIPHERTEXT"):
            transport.private_cipher(file)

    def test_upload_uses_only_immutable_not_overwrite(self):
        seen = []
        client = transport.AppFolder("x" * 40)
        def fake_api(suffix, *, params=None, method="GET"):
            seen.append((suffix, params, method))
            return 200, {"href":"https://uploader.disk.yandex.net/upload","method":"PUT"}
        with patch.object(client, "api", side_effect=fake_api):
            client.signed_url("upload", "asa-school-01-20261102T002000Z-1a2b3c4d.tar.age")
        self.assertEqual(seen[0][1]["overwrite"], "false")
        self.assertTrue(seen[0][1]["path"].startswith("app:/School-VM100/Database-Backups/"))


if __name__ == "__main__":
    unittest.main()
