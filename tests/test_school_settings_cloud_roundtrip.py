import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
SOURCE=ROOT/"scripts/school-settings-cloud-roundtrip.py"
spec=importlib.util.spec_from_file_location("school_config_cloud_roundtrip",SOURCE)
mod=importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class SchoolSettingsCloudTests(unittest.TestCase):
    def test_exact_archive_naming_no_path_traversal(self):
        self.assertEqual(
            mod.valid_name("server-settings-20261101T121900Z.tar.age"),
            "server-settings-20261101T121900Z.tar.age")
        for candidate in ("../server-settings-20261101T121900Z.tar.age",
                          "server-settings-20261101.tar.age",
                          "server-settings-20261101T121900Z.tar",
                          "server-settings-20261101T121900Z.tar.age/anything",
                          "school-secret.tar.age",
                          "server-settings-20261101T121900Z.tar.age; rm"):
            with self.subTest(candidate=candidate):
                with self.assertRaises(mod.BackupError):
                    mod.valid_name(candidate)

    def test_sha256_works_without_plaintext(self):
        cipher=b"age-encryption.org/v1\\n" + b"test-encrypted-bytes"
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"cipher.age"
            path.write_bytes(cipher)
            self.assertEqual(mod.hash_bytes(path),hashlib.sha256(cipher).hexdigest())

    def test_project_never_targets_primary_asa_backup_namespace(self):
        self.assertEqual(mod.PROJECT,"server-settings")
        self.assertEqual(mod.HOST,"asa-school-01")
        self.assertEqual(mod.SPOOL,Path("/srv/asa-cloud-storage/data"))


if __name__=="__main__":
    unittest.main()
