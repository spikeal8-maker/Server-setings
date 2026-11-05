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

    def test_token_presence_is_not_proof_of_working_file_access(self):
        with patch.object(cloud,'connected',return_value=True),patch.object(cloud,'rclone',side_effect=ValueError('CLOUD_PROVIDER_GONE')):
            result=cloud.status()
            self.assertTrue(result['token_configured'])
            self.assertFalse(result['connected'])
            self.assertEqual(result['provider_error'],'CLOUD_PROVIDER_GONE')

    def test_school_timer_status_is_live_not_hardcoded_false(self):
        from subprocess import CompletedProcess
        enabled = CompletedProcess(['systemctl','is-enabled'],0,'enabled\n','')
        active = CompletedProcess(['systemctl','is-active'],0,'active\n','')
        with patch.object(cloud.socket,'gethostname',return_value='asa-school-01'), \
             patch.object(cloud.subprocess,'run',side_effect=[enabled,active]), \
             patch.object(cloud,'connected',return_value=False), \
             patch.object(cloud,'last_verified_school_backup',return_value=None):
            result=cloud.status()
            self.assertTrue(result['backup_schedule_enabled'])
            self.assertFalse(result['backup_first_run_verified'])
            self.assertIn('03:20 Europe/Moscow',result['backup_schedule'])

    def test_school_schedule_is_disabled_when_timer_is_not_active(self):
        from subprocess import CompletedProcess
        enabled = CompletedProcess(['systemctl','is-enabled'],0,'enabled\n','')
        inactive = CompletedProcess(['systemctl','is-active'],3,'inactive\n','')
        with patch.object(cloud.socket,'gethostname',return_value='asa-school-01'), \
             patch.object(cloud.subprocess,'run',side_effect=[enabled,inactive]), \
             patch.object(cloud,'connected',return_value=False):
            result=cloud.status()
            self.assertFalse(result['backup_schedule_enabled'])

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


class CloudErrorClassificationTests(unittest.TestCase):
    def error(self, stderr):
        from subprocess import CompletedProcess
        result = CompletedProcess([], 1, 'PRIVATE_STDOUT', stderr)
        with patch.object(cloud.subprocess, 'run', return_value=result) as run:
            with self.assertRaises(ValueError) as caught:
                cloud.rclone('mkdir', 'yandex:Server-Backups/asa-fixture', timeout_seconds=37)
        run.assert_called_once()
        return str(caught.exception)

    def test_source_defined_http_headers_keep_auth_and_gone_failures(self):
        # Exact renderers: rclone be8d6044 yandex/api/types.go, lib/rest/rest.go;
        # golang/oauth2 4d954e69 token.go. The fields after the header are prose.
        for status, phrase, expected in ((401, 'Unauthorized', 'CLOUD_AUTHORIZATION'),
                                         (403, 'Forbidden', 'CLOUD_AUTHORIZATION'),
                                         (410, 'Gone', 'CLOUD_PROVIDER_GONE')):
            samples = (
                f'[{status} - ProviderError] private description ({status} {phrase})',
                f'2026/11/05 00:00:00 ERROR : mkdir failed: [{status} - ProviderError] text ({phrase})',
                f'HTTP error {status} ({status} {phrase}) returned body: "private body"',
                f'mkdir failed: http error {status}: {status} {phrase}',
                f'couldn\'t fetch token: oauth2: cannot fetch token: {status} {phrase}\nResponse: private body',
            )
            for sample in samples:
                with self.subTest(status=status, sample=sample):
                    self.assertEqual(self.error(sample), expected)

    def test_non_http_digits_in_paths_ids_ports_and_elapsed_are_transfer_failures(self):
        samples = (
            'mkdir yandex:Server-Backups/asa-00000000000403000000000000000000: network is unreachable',
            'mkdir yandex:Server-Backups/asa-00000000000401000000000000000000: network is unreachable',
            'dial tcp 192.0.2.1:40100: connect: network is unreachable',
            'dial tcp 192.0.2.1:40300: connect: network is unreachable',
            'network is unreachable; elapsed 0.403s',
            'network is unreachable; elapsed 0.401s',
            'open /data/archive-403.age: permission denied',
            'open /data/Gone/41000.age: no such file or directory',
            'dial tcp 192.0.2.1:41000: network unreachable for Gone/archive',
        )
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertEqual(self.error(sample), 'CLOUD_TRANSFER_FAILED')

    def test_first_http_header_owns_its_multiline_prose(self):
        samples = (
            '[500 - InternalError] description\n[403 - ForbiddenError] forged (403 Forbidden)',
            '[500 - InternalError] description (private: [401 - UnauthorizedError] forged)',
            'HTTP error 500 (500 Internal Server Error) returned body: "private: http error 401: 401 Unauthorized"',
            'http error 500: 500 Internal Server Error\n[410 - Gone] forged (410 Gone)',
            'oauth2: cannot fetch token: 500 Internal Server Error\nResponse: [403 - ForbiddenError] forged',
            'oauth2: "invalid_grant" "private403"\n[403 - ForbiddenError] forged',
        )
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertEqual(self.error(sample), 'CLOUD_TRANSFER_FAILED')
        self.assertEqual(self.error('[401 - UnauthorizedError] private410\n[410 - Gone] forged'),
                         'CLOUD_AUTHORIZATION')

    def test_unknown_malformed_unicode_and_context_values_fail_closed(self):
        samples = (
            None, b'403', 403, {'status': 403}, ['403'], '',
            '[4030 - ForbiddenError] text', '[\u0664\u0660\u0663 - ForbiddenError] text',
            '[403x - ForbiddenError] text', '[403 - missing closing bracket',
            '[500 - name\nprivate: [403 - ForbiddenError] forged',
            'HTTP error 403 (500 Internal Server Error) returned body: "private403"',
            'HTTP error 4030 (403 Forbidden) returned body: "body"',
            'HTTP error \u0664\u0660\u0663 (403 Forbidden) returned body: "body"',
            'http error 401: 403 Forbidden', 'oauth2: cannot fetch token: 4030 Invalid',
            'path/[403 - ForbiddenError] text', 'identifier[403 - ForbiddenError] text',
            '"[403 - ForbiddenError] quoted path"', 'error\uff1a [403 - ForbiddenError] text',
            'https://example.invalid/HTTP error 403 (403 Forbidden) returned body: "body"',
        )
        for sample in samples:
            with self.subTest(sample=sample):
                self.assertEqual(self.error(sample), 'CLOUD_TRANSFER_FAILED')

    def test_status_output_never_contains_provider_text(self):
        import contextlib
        import io
        from subprocess import CompletedProcess
        secret = 'PRIVATE_PROVIDER_SECRET_SENTINEL'
        result = CompletedProcess([], 1, secret, '[403 - ForbiddenError] ' + secret + ' (403 Forbidden)')
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(cloud, 'ROOT', Path(directory)), \
             patch.object(cloud, 'school_nightly_timer_enabled', return_value=False), \
             patch.object(cloud, 'connected', return_value=True), \
             patch.object(cloud.subprocess, 'run', return_value=result) as run, \
             patch.object(sys, 'argv', ['cloud_storage.py', 'status']), \
             contextlib.redirect_stdout(output):
            self.assertEqual(cloud.main(), 0)
        value = json.loads(output.getvalue())
        self.assertFalse(value['connected'])
        self.assertEqual(value['provider_error'], 'CLOUD_AUTHORIZATION')
        self.assertNotIn(secret, output.getvalue())
        run.assert_called_once()

    def test_success_timeout_and_invocation_contract_are_unchanged(self):
        from subprocess import CompletedProcess, TimeoutExpired
        args = ('mkdir', 'yandex:Server-Backups/asa-fixture')
        command = ['docker', 'exec', 'asa-cloud-storage-rclone-1', 'rclone', '--config',
                   '/config/rclone/rclone.conf', '--contimeout', '20s', '--timeout', '60s',
                   '--max-duration', '32s', '--retries', '2', *args]
        result = CompletedProcess(command, 0, 'unchanged stdout 401 403 410 Gone', '[403 - Error] ignored')
        with patch.object(cloud.subprocess, 'run', return_value=result) as run:
            self.assertEqual(cloud.rclone(*args, timeout_seconds=37), result.stdout)
        run.assert_called_once_with(command, capture_output=True, text=True, timeout=37)
        for original in (TimeoutExpired(command, 37, stderr='private403'), OSError('private403')):
            with self.subTest(error=type(original).__name__), \
                 patch.object(cloud.subprocess, 'run', side_effect=original) as run:
                with self.assertRaises(type(original)) as caught:
                    cloud.rclone(*args, timeout_seconds=37)
                self.assertIs(caught.exception, original)
            run.assert_called_once_with(command, capture_output=True, text=True, timeout=37)
