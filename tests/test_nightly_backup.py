import contextlib
import datetime as dt
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parents[1] / 'scripts' / filename)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


runner = load('nightly_backup', 'nightly-asa-backup.py')
installer = load('nightly_installer', 'install-nightly-asa-backup.py')


class BackupTests(unittest.TestCase):
    def setUp(self):
        # Windows runner may not have tzdata; Moscow date semantics remain fixed.
        self.enterContext(patch.object(runner, 'ZoneInfo', return_value=dt.timezone(dt.timedelta(hours=3))))

    def test_run_plan_does_not_read_config_or_contact_mcp(self):
        with patch.object(runner, 'config', side_effect=AssertionError('must not read')), patch('sys.argv', ['backup', 'run']), contextlib.redirect_stdout(io.StringIO()) as output:
            runner.main()
        self.assertTrue(json.loads(output.getvalue())['plan'])

    def test_installer_plan_never_touches_services(self):
        with patch.object(installer.subprocess, 'run', side_effect=AssertionError('must not invoke')), patch('sys.argv', ['install']), contextlib.redirect_stdout(io.StringIO()) as output:
            installer.main()
        self.assertIn('accepted isolated restore before enable', json.loads(output.getvalue())['requires'])

    def test_scheduler_uses_mcp_gate_and_stable_daily_idempotency(self):
        calls = []

        def rpc(name, args):
            calls.append((name, args))
            return {'operation': {'operation_id': 'a' * 32}, 'reused': True} if name == 'asa_admin_execute' else {'status': 'success'}

        c = {'installation_id': 'asa-test', 'root': '/srv/asa-lab', 'installation_instance_id': 'b' * 32}
        with patch.object(runner, 'rpc', side_effect=rpc), patch.object(runner, 'save'):
            first = runner.schedule(c)
            runner.schedule(c)
        self.assertEqual(first['operation_id'], 'a' * 32)
        self.assertEqual(calls[0][1]['idempotency_key'], calls[2][1]['idempotency_key'])
        self.assertEqual(calls[0][1]['timeout_seconds'], 7000)
        self.assertIn('run --apply', calls[0][1]['command'])

    def test_busy_never_falls_back_to_direct_backup(self):
        with patch.object(runner, 'rpc', side_effect=RuntimeError('MCP_BUSY')), patch.object(runner, 'run_backup') as run:
            with self.assertRaises(RuntimeError):
                runner.schedule({'installation_id': 'asa-test', 'root': '/srv/asa-lab', 'installation_instance_id': 'b' * 32})
            run.assert_not_called()

    def test_failed_job_does_not_replay(self):
        with patch.object(runner, 'rpc', side_effect=[{'operation': {'operation_id': 'a' * 32}, 'reused': False}, {'status': 'recovery_required'}]) as rpc, patch.object(runner, 'save'):
            with self.assertRaises(RuntimeError):
                runner.schedule({'installation_id': 'asa-test', 'root': '/srv/asa-lab', 'installation_instance_id': 'b' * 32})
            self.assertEqual(rpc.call_count, 2)

    def test_manager_interrupt_allows_finally_before_force_kill(self):
        if sys.platform == 'win32':
            self.skipTest('POSIX owner/signal behavior')
        owner = SimpleNamespace(pw_name='admins', pw_uid=1000, pw_gid=1000, pw_dir='/home/admins')
        process = SimpleNamespace(pid=123, wait=unittest.mock.Mock(side_effect=[subprocess.TimeoutExpired('manager', 1800), 1]))
        with patch('pwd.getpwuid', return_value=owner), patch.object(runner.os, 'getgrouplist', return_value=[1000]), patch.object(runner.subprocess, 'Popen', return_value=process), patch.object(runner.os, 'killpg') as kill:
            with self.assertRaisesRegex(RuntimeError, 'NO_AUTOMATIC_RETRY'):
                runner.manager({'owner_uid': 1000, 'root': '/srv/asa-lab', 'profile': 'production'}, ['backup'], io.StringIO())
            kill.assert_called_once_with(123, runner.signal.SIGINT)

    def test_no_redirect_for_private_mcp_auth(self):
        self.assertIsNone(runner.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://other/'))


class RehearsalGateTests(unittest.TestCase):
    def setUp(self):
        self.now = dt.datetime.now(dt.timezone.utc)
        self.c = {'hostname': 'host', 'installation_instance_id': 'i'}
        self.backup = {'sha256': 'a' * 64}
        self.accepted = {'hostname': 'host', 'installation_instance_id': 'i', 'archive_sha256': 'a' * 64,
                         'decryption_verified': True, 'postgres_restored': True, 'objects_verified': True,
                         'restored_account_project_checks': True, 'accepted_at': self.now.isoformat()}

    def test_complete_current_matching_rehearsal_accepted(self):
        installer.validate_rehearsal(self.c, self.accepted, self.backup, self.now)

    def test_hash_or_installation_mismatch_rejected(self):
        for delta in [{'archive_sha256': 'b' * 64}, {'installation_instance_id': 'other'}]:
            with self.assertRaises(RuntimeError):
                installer.validate_rehearsal(self.c, {**self.accepted, **delta}, self.backup, self.now)

    def test_pg_restore_list_alone_is_not_a_rehearsal(self):
        with self.assertRaisesRegex(RuntimeError, 'RESTORE_NOT_ACCEPTED'):
            installer.validate_rehearsal(self.c, {**self.accepted, 'postgres_restored': False}, self.backup, self.now)

    def test_stale_or_future_acceptance_rejected(self):
        for delta in [dt.timedelta(days=8), dt.timedelta(minutes=-1)]:
            with self.assertRaises(RuntimeError):
                installer.validate_rehearsal(self.c, {**self.accepted, 'accepted_at': (self.now - delta).isoformat()}, self.backup, self.now)


if __name__ == '__main__':
    unittest.main()
