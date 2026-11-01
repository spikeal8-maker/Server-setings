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

    def test_manual_daytime_run_never_stops_database(self):
        with patch.object(nightly, "is_night", return_value=False), \
             patch.object(nightly, "preflight") as cloud, \
             patch.object(nightly, "export_asa") as exporter:
            with self.assertRaises(nightly.NightlyError):
                nightly.execute()
        cloud.assert_not_called()
        exporter.assert_not_called()

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
        with tempfile.TemporaryDirectory() as tmp:
            file = Path(tmp) / "cipher.age"
            file.write_bytes(b"age-encryption.org/v1\n" + b"c" * 2048)
            file.chmod(0o600)
            with patch.object(transport.os, "geteuid", return_value=0):
                size, hash_value = transport.private_cipher(file)
            self.assertEqual(size, file.stat().st_size)
            self.assertEqual(hash_value, hashlib.sha256(file.read_bytes()).hexdigest())
            file.write_bytes(b"NOT_AGE_HEADER" + b"d" * 2048)
            with self.assertRaises(transport.BackupTransportError):
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
