#!/usr/bin/env python3
"""Tests only the newly introduced scheduling/interlock/receipt risks."""
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('nightly_settings', ROOT / 'scripts/school-nightly-settings-backup.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


@unittest.skipUnless(os.geteuid() == 0, 'Production path/mode guards require a root-owned test tree')
class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='scheduler-fixture-', dir='/root')
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_real_flock_excludes_database_and_settings_in_both_directions(self):
        lock = self.root / 'shared-db.lock'
        hold = ('import fcntl,os,sys;f=os.open(sys.argv[1],os.O_RDWR|os.O_CREAT,0o600);'
                'fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB);print("LOCKED",flush=True);sys.stdin.read()')
        child = subprocess.Popen([sys.executable, '-c', hold, str(lock)], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), 'LOCKED')
            started = time.monotonic()
            with self.assertRaisesRegex(runner.ScheduleError, 'DATABASE_OR_CONFIG_LOCK_BUSY'):
                with runner.lock(lock, 0.05):
                    self.fail('acquired a database-held lock')
            elapsed = time.monotonic() - started
            self.assertGreaterEqual(elapsed, 0.04)
            self.assertLess(elapsed, 1)
        finally:
            child.communicate('', timeout=5)
        self.assertEqual(child.returncode, 0)
        probe = ('import fcntl,os,sys;f=os.open(sys.argv[1],os.O_RDWR);'
                 '\ntry: fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)'
                 '\nexcept BlockingIOError: sys.exit(73)'
                 '\nsys.exit(0)')
        with runner.lock(lock, 0):
            other = subprocess.run([sys.executable, '-c', probe, str(lock)], capture_output=True, timeout=5)
            self.assertEqual(other.returncode, 73)
        with runner.lock(lock, 0):
            pass

    def test_immutable_release_rejects_modified_code_before_import(self):
        release_parent = self.root / 'releases'
        release_parent.mkdir(mode=0o700)
        sources = ROOT / 'scripts'
        files = {name: (sources / name).read_bytes() for name in runner.PINS}
        files[runner.RUNNER] = (ROOT / 'scripts' / runner.RUNNER).read_bytes()
        hashes = {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()}
        digest = hashlib.sha256(json.dumps(hashes, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        release = release_parent / digest[:16]
        release.mkdir(mode=0o700)
        for name, raw in files.items():
            (release / name).write_bytes(raw)
            (release / name).chmod(0o600)
        manifest = release / 'release.json'
        manifest.write_text(json.dumps({'format': 'asa-school-settings-release-v1', 'files': hashes}))
        manifest.chmod(0o600)
        database = self.root / 'database.py'
        shutil.copyfile(ROOT / 'scripts/school-nightly-database-backup.py', database)
        database.chmod(0o600)
        with patch.object(runner, '__file__', str(release / runner.RUNNER)), \
             patch.object(runner, 'RELEASES', release_parent), patch.object(runner, 'DB_SCRIPT', database):
            snapshot, cloud, actual = runner.load_code()
            self.assertEqual(actual, digest)
            changed = release / 'school-settings-cloud-roundtrip.py'
            changed.write_bytes(changed.read_bytes() + b'\n')
            with self.assertRaisesRegex(runner.ScheduleError, 'RELEASE_CODE_CHANGED'):
                runner.load_code()

    def test_failure_keeps_previous_success_and_hides_exception_contents(self):
        previous = {'status': 'PRIOR_TRANSFER_PASS', 'archive': 'previous-immutable-copy'}
        success = self.root / 'latest-success.json'
        success.write_text(json.dumps(previous))
        success.chmod(0o600)
        output, error = io.StringIO(), io.StringIO()
        with patch.object(runner, 'STATE', self.root), patch.object(runner, 'check_host'), \
             patch.object(runner, 'in_window', return_value=True), \
             patch.object(runner, 'load_code', side_effect=ValueError('private-fixture-token-must-not-print')), \
             redirect_stdout(output), redirect_stderr(error):
            self.assertEqual(runner.execute(), 2)
        failure = json.loads((self.root / 'latest-failure.json').read_text())
        attempt = json.loads((self.root / 'latest-attempt.json').read_text())
        self.assertEqual(failure['status'], 'CONFIG_NIGHTLY_STOP')
        self.assertEqual(failure['code'], 'ValueError')
        self.assertEqual(attempt, failure)
        self.assertEqual(json.loads(success.read_text()), previous)
        self.assertNotIn('private-fixture-token', output.getvalue() + error.getvalue())
        self.assertTrue(error.getvalue().startswith('<3>'))
        self.assertTrue(json.loads(error.getvalue().removeprefix('<3>'))['failureReceiptPersisted'])
        for file in self.root.iterdir():
            self.assertEqual(file.stat().st_mode & 0o777, 0o600)

    def test_new_transfer_success_cannot_be_reported_as_new_decrypt(self):
        cipher_sha = 'b' * 64
        window = {'from':'2026-11-13T00:00:00.000Z', 'to':'2026-11-14T00:00:00.000Z'}

        def create(name):
            return {'status': 'CONFIG_ARCHIVE_CREATED_NOT_CLOUD_VERIFIED', 'archive': name,
                    'coverageComplete': False, 'sqliteDatabases': 3, 'configurationFiles': 54,
                    'normalizedLogWindow': window,
                    'normalizedLogs': {'host': {'coverageComplete': False}, 'application': {'coverageComplete': False}},
                    'bytes': 4096, 'ciphertextSha256': cipher_sha}

        def transfer(name):
            receipt = {'status': 'CONFIG_UPLOAD_FULL_DOWNLOAD_PASS_DECRYPT_PENDING', 'archive': name,
                       'remoteDirectory': runner.REMOTE, 'bytes': 4096, 'ciphertextSha256': cipher_sha,
                       'fullDownloadVerified': True, 'restoreValidated': False}
            file = self.root / (name + '.cloud.json')
            file.write_text(json.dumps(receipt))
            file.chmod(0o600)

        @contextmanager
        def no_lock(path, wait):
            yield

        snapshot = types.SimpleNamespace(create=create, RECOVERY=self.root, validate_window=lambda value: value,
                                         verify_cipher=lambda path: (4096, cipher_sha))
        cloud = types.SimpleNamespace(school_configuration_transfer=transfer)
        with patch.object(runner, 'STATE', self.root), patch.object(runner, 'TARGET', self.root), \
             patch.object(runner, 'check_host'), patch.object(runner, 'in_window', return_value=True), \
             patch.object(runner, 'load_code', return_value=(snapshot, cloud, 'c' * 64)), \
             patch.object(runner, 'lock', side_effect=no_lock), redirect_stdout(io.StringIO()):
            self.assertEqual(runner.execute(), 0)
        success = json.loads((self.root / 'latest-success.json').read_text())
        self.assertEqual(success['status'], 'CONFIG_NIGHTLY_FULL_DOWNLOAD_SHA256_PASS')
        self.assertTrue(success['fullDownloadVerified'])
        self.assertFalse(success['nightlyRestoreValidated'])
        self.assertEqual(success['baselineIsolatedRestore'], runner.BASELINE)
        self.assertNotEqual(success['archive'], runner.BASELINE['archive'])
        self.assertFalse(success['coverageComplete'])
        self.assertEqual(success['normalizedLogWindow'],window)
        self.assertEqual(success, json.loads((self.root / 'latest-attempt.json').read_text()))


if __name__ == '__main__':
    unittest.main(verbosity=2)
