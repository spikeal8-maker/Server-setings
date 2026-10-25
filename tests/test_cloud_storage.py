import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('cloud_storage',Path(__file__).resolve().parents[1]/'scripts/cloud_storage.py')
cloud=importlib.util.module_from_spec(spec);spec.loader.exec_module(cloud)


class CloudStorageTests(unittest.TestCase):
    def test_namespace_and_archive_traversal_rejected(self):
        self.assertEqual(cloud.remote_path('asa-lab'),'yandex:ASA-Backups')
        self.assertEqual(cloud.remote_path('izo-asa'),'yandex:Server-Backups/izo-asa')
        for name in ['../project','x:y','', '/etc']:
            with self.assertRaises(ValueError):cloud.remote_path(name)
        for name in ['../secret.age','dump.sql','a/b.age','x.age;echo']:
            with self.assertRaises(ValueError):cloud.archive_name(name)

    def test_container_has_no_host_port_or_live_database_mount(self):
        c=cloud.compose()['services']['rclone']
        self.assertNotIn('ports',c);self.assertTrue(c['read_only']);self.assertEqual(c['user'],'1000:1000')
        self.assertEqual([x['target'] for x in c['volumes']],['/config/rclone','/data'])
        self.assertIn('127.0.0.1:5572',c['command'])
        self.assertIn('@sha256:',c['image'])

    def test_missing_account_does_not_start_transfer(self):
        with patch.object(cloud,'connected',return_value=False),patch.object(cloud,'rclone') as run:
            with self.assertRaisesRegex(ValueError,'CLOUD_NOT_CONNECTED'):cloud.transfer('list','asa-lab')
            run.assert_not_called()

    def test_download_refuses_existing_file_and_plaintext_upload_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);base=root/'data/asa-lab'
            for name in ['incoming','outgoing']:(base/name).mkdir(parents=True)
            (base/'incoming/backup.age').write_bytes(b'existing')
            (base/'outgoing/backup.age').write_bytes(b'plaintext SQL dump')
            with patch.object(cloud,'ROOT',root),patch.object(cloud,'connected',return_value=True),patch.object(cloud,'rclone') as run:
                with self.assertRaisesRegex(ValueError,'CLOUD_DOWNLOAD_EXISTS'):cloud.transfer('download','asa-lab','backup.age')
                with self.assertRaisesRegex(ValueError,'CLOUD_AGE_ENVELOPE'):cloud.transfer('upload','asa-lab','backup.age')
                run.assert_not_called()
