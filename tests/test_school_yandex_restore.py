import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec=importlib.util.spec_from_file_location("school_restore",SCRIPTS/"school-yandex-restore.py")
subject=importlib.util.module_from_spec(spec)
spec.loader.exec_module(subject)


class SchoolYandexRestoreTests(unittest.TestCase):
    def test_explicit_sources_and_archive_shapes(self):
        self.assertEqual(subject.SOURCES["school"],"app:/School-VM100/Database-Backups")
        self.assertEqual(subject.SOURCES["legacy"],"disk:/ASA-Backups")
        self.assertEqual(
            subject.name_ok("legacy","asa-lab-20261029T231505Z-36ab7eb5.tar.age"),
            "asa-lab-20261029T231505Z-36ab7eb5.tar.age"
        )
        self.assertEqual(
            subject.name_ok("school","asa-school-01-20261102T002000Z-1234abcd.tar.age"),
            "asa-school-01-20261102T002000Z-1234abcd.tar.age"
        )
        for source,name in (
            ("school","../backup.tar.age"),
            ("legacy","ASA-Lab-school-20260927T191207Z.dump"),
            ("legacy","asa-school-01-20261102T002000Z-1234abcd.tar.age"),
            ("school","asa-lab-20261029T231505Z-36ab7eb5.tar.age"),
            ("legacy","something; rm -rf.tar.age"),
        ):
            with self.subTest(name=name):
                with self.assertRaises(subject.RestoreError):
                    subject.name_ok(source,name)

    def test_readonly_catalog_filters_plaintext_and_invalid_legacy(self):
        def fake_api(suffix,*,params=None,method="GET"):
            return 200,{
                "type":"dir",
                "_embedded":{
                    "total":3,
                    "items":[
                        {"name":"asa-lab-20261029T231505Z-36ab7eb5.tar.age","size":220943720,"type":"file"},
                        {"name":"ASA-Lab-school-20260927T191207Z.dump","size":2100,"type":"file"},
                        {"name":"../../attack.tar.age","size":2200,"type":"file"},
                    ],
                },
            }
        client=type("Client",(),{"api":staticmethod(fake_api)})()
        result=subject.list_entries(client,"legacy")
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]["size"],220943720)

    def test_download_never_targets_live_database(self):
        self.assertEqual(str(subject.INCOMING),"/var/backups/asa-school-recovery/incoming")
        self.assertEqual(str(subject.UNPACKED),"/var/backups/asa-school-recovery/unpacked")
        self.assertNotEqual(subject.INCOMING,subject.ASA)
        self.assertNotEqual(subject.ROOT,subject.ASA)
        self.assertNotIn("--replace-live",(SCRIPTS/"school-yandex-restore.py").read_text())

    def test_age_identity_only_from_private_offline_mount(self):
        with tempfile.TemporaryDirectory() as path:
            test=Path(path)/"key.txt"
            test.write_text("AGE-SECRET-KEY-TEST\n")
            test.chmod(0o600)
            with self.assertRaises(subject.RestoreError):
                subject.trusted_age_identity(str(test))

    def test_hidden_one_shot_identity_is_not_written_to_disk(self):
        import os
        private="AGE-SECRET-KEY-1"+"A"*58
        with patch("builtins.input",return_value=""), \
             patch.object(subject.getpass,"getpass",return_value=private):
            with subject.offline_identity(None) as (name,fds):
                self.assertEqual(len(fds),1)
                self.assertEqual(name,"/proc/self/fd/"+str(fds[0]))
                os.lseek(fds[0],0,os.SEEK_SET)
                self.assertEqual(os.read(fds[0],200),private.encode()+b"\n")
                self.assertFalse(Path(name).is_file() and "/var/backups/" in name)
            with self.assertRaises(OSError):
                os.fstat(fds[0])

    def test_invalid_hidden_key_never_opens_usable_identity(self):
        with patch("builtins.input",return_value=""), \
             patch.object(subject.getpass,"getpass",return_value="not-a-key"):
            with self.assertRaisesRegex(subject.RestoreError,"AGE_PRIVATE_KEY_FORMAT_INVALID"):
                with subject.offline_identity(None):
                    pass

    def test_rehearsal_command_is_only_nonlive_restore_check(self):
        import inspect
        script=inspect.getsource(subject.rehearsed_restore)
        self.assertIn('"restore-check"',script)
        self.assertIn('"_test"',script)
        self.assertNotIn('dropdb',script)
        self.assertNotIn('"restore"',script)
        self.assertIn("RESTORE-TEST",script)
        self.assertIn("PRODUCTION_DATABASE_UNTOUCHED=YES",script)


if __name__=="__main__":
    unittest.main()
