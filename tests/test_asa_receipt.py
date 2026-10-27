"""P0: offline proof that one manager invocation cannot inherit an old receipt."""
import datetime as dt
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from asa_ops import adapter as bridge
from asa_ops.store import Store, digest
from asa_ops.worker import work


class ManagerReceiptAttributionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.state = self.root / '.asa'
        self.state.mkdir()
        self.receipt = self.state / 'last-operation.json'
        self.backup_root = self.root / 'backups'
        self.backup_root.mkdir()
        self.backup = self.backup_root / 'verified-synthetic'
        self.backup.mkdir()
        self.release = {'revision': 'b' * 40, 'schema': 266}
        self.current = {'revision': 'a' * 40, 'schema': 255}
        self.initial_fingerprint = {
            'checkout_revision': 'a' * 40,
            'files': {'.asa/last-operation.json': 'pre-invocation-sha'},
        }
        self.plan = {
            'ready': True, 'release': self.release,
            'pin': {'manifest_sha256': digest(self.release)},
            'current': self.current, 'fingerprint': self.initial_fingerprint,
        }
        self.install = types.SimpleNamespace(
            root=self.root, state=self.state,
            settings={'backupDirectory': str(self.backup_root)}, checkout='a' * 40)
        self.install.git = lambda *args: self.install.checkout if args == ('rev-parse', 'HEAD') else None
        self.manager = types.SimpleNamespace()
        for name in ('doctor', 'fetch_images', 'selected_source'):
            setattr(self.manager, name, lambda *args, **kw: None)
        self.manager.export_backup = lambda *args, **kw: self.backup
        self.manager.main = self.fake_main
        self.exit_code = 2
        self.stderr = 'BLOCKED IO_OR_CONFIGURATION: sanitized manager fixture'
        self.receipt_bytes = None
        self.stop_at_source = False
        self.stop_after_doctor = False
        self.do_backup = True
        self.verify_error = None

    def fixture_receipt(self, *, status='success', phase='verify_entry', selected=None,
                        backup=None, previous=None, time=None):
        return {
            'at': time or dt.datetime.now(dt.timezone.utc).isoformat(),
            'status': status, 'phase': phase, 'selected': selected or self.release,
            'previous': previous or self.current,
            'backup': str(self.backup if backup is None else backup),
        }

    def fake_main(self, argv):
        self.assertEqual(argv[0], 'update')
        self.manager.doctor(self.install)
        if self.stop_after_doctor:
            print(self.stderr, file=sys.stderr)
            return self.exit_code
        self.manager.fetch_images(self.release)
        self.manager.selected_source(self.install, self.release)
        if not self.stop_at_source:
            if self.do_backup:
                self.manager.export_backup(self.install)
            if self.receipt_bytes is not None:
                self.receipt.write_bytes(self.receipt_bytes)
        print(self.stderr, file=sys.stderr)
        return self.exit_code

    def verify_backup(self, path):
        if self.verify_error is not None:
            raise self.verify_error
        self.assertEqual(Path(path), self.backup)
        return {'runtime': self.current, 'files': {'database.dump': 'digest', 'objects/a': 'digest'}}

    def run_case(self):
        pkg = types.ModuleType('deployment')
        pkg.__path__ = []
        contracts = types.ModuleType('deployment.contracts')
        contracts.validate_release = lambda release: release
        contracts.verify_backup = self.verify_backup
        with patch.dict(sys.modules, {'deployment': pkg, 'deployment.contracts': contracts}), \
             patch.object(bridge, 'load', return_value=(self.manager, self.install)), \
             patch.object(bridge, 'fingerprint', return_value=self.initial_fingerprint), \
             patch.object(bridge, 'plan', return_value={'ready': True}), \
             patch.object(bridge, 'event'):
            return bridge.update({'root': str(self.root), 'profile': 'staging'}, {'plan': self.plan})

    def test_old_success_and_backup_are_not_misattributed_after_error(self):
        stale = self.fixture_receipt(
            phase='complete', backup=self.backup_root / 'yesterday',
            time=(dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).isoformat())
        self.receipt.write_text(json.dumps(stale))
        unchanged = self.receipt.read_bytes()
        result = self.run_case()
        self.assertEqual(result['manager_exit_code'], 2)
        self.assertEqual(result['manager_status'], 'failed')
        self.assertEqual(result['phase'], 'failed')
        self.assertEqual(result['receipt_evidence'], 'stale')
        self.assertFalse(result['receipt_attributed'])
        self.assertEqual(result['error'], {'code': 'IO_OR_CONFIGURATION', 'stage': 'post_backup'})
        self.assertEqual(result['backup_status'], 'verified')
        self.assertEqual(result['backup'], str(self.backup))
        self.assertEqual(result['backup_files'], 2)
        self.assertEqual(self.receipt.read_bytes(), unchanged)

    def test_missing_receipt_and_unknown_manager_error(self):
        self.do_backup = False
        self.stderr = 'TRACEBACK with PRIVATE_0123456789 but no trusted BLOCKED code'
        result = self.run_case()
        self.assertEqual(result['receipt_evidence'], 'missing')
        self.assertEqual(result['manager_status'], 'failed')
        self.assertEqual(result['backup_status'], 'not_observed')
        self.assertEqual(result['error']['code'], 'MANAGER_EXIT_NONZERO')
        self.assertNotIn('PRIVATE_0123456789', json.dumps(result))

    def test_corrupted_new_receipt_fails_closed(self):
        self.receipt_bytes = b'{not valid JSON'
        result = self.run_case()
        self.assertEqual(result['receipt_evidence'], 'invalid')
        self.assertEqual(result['manager_status'], 'failed')
        self.assertFalse(result['receipt_attributed'])

    def test_fresh_receipt_with_foreign_release_is_rejected(self):
        stale = self.fixture_receipt(selected={'revision': 'c' * 40, 'schema': 266})
        self.receipt_bytes = json.dumps(stale).encode()
        result = self.run_case()
        self.assertEqual(result['receipt_evidence'], 'mismatch')
        self.assertEqual(result['manager_status'], 'failed')

    def test_success_has_attributed_receipt_and_verified_export(self):
        self.exit_code = 0
        self.receipt_bytes = json.dumps(self.fixture_receipt()).encode()
        result = self.run_case()
        self.assertEqual(result['manager_status'], 'success')
        self.assertEqual(result['phase'], 'verify_entry')
        self.assertTrue(result['receipt_attributed'])
        self.assertEqual(result['backup_status'], 'verified')
        self.assertIsNone(result['error'])
        self.assertFalse(result['no_change'])

    def test_failed_switch_current_receipt_is_not_called_success(self):
        self.receipt_bytes = json.dumps(self.fixture_receipt(
            status='failed_switch', phase='switch')).encode()
        result = self.run_case()
        self.assertEqual(result['manager_status'], 'failed_switch')
        self.assertEqual(result['phase'], 'switch')
        self.assertTrue(result['receipt_attributed'])
        self.assertEqual(result['backup_status'], 'verified')

    def test_new_receipt_with_foreign_backup_pointer_is_rejected(self):
        receipt = self.fixture_receipt(backup=self.backup_root / 'unrelated-old-export')
        self.receipt_bytes = json.dumps(receipt).encode()
        result = self.run_case()
        self.assertEqual(result['receipt_evidence'], 'backup_mismatch')
        self.assertEqual(result['manager_status'], 'failed')
        self.assertEqual(result['backup_status'], 'verified')
        self.assertEqual(result['backup'], str(self.backup))

    def test_receipt_backup_pointer_with_wrong_type_is_safe(self):
        receipt = self.fixture_receipt()
        receipt['backup'] = {'secret': 'synthetic-never-expose'}
        self.receipt_bytes = json.dumps(receipt).encode()
        result = self.run_case()
        self.assertEqual(result['receipt_evidence'], 'backup_mismatch')
        self.assertEqual(result['manager_status'], 'failed')
        self.assertNotIn('synthetic-never-expose', json.dumps(result))

    def test_inconsistent_receipt_status_and_phase_are_rejected(self):
        for status, phase in (('success', 'switch'), ('failed_switch', 'complete')):
            with self.subTest(status=status, phase=phase):
                self.receipt_bytes = json.dumps(
                    self.fixture_receipt(status=status, phase=phase)).encode()
                result = self.run_case()
                self.assertEqual(result['receipt_evidence'], 'invalid')
                self.assertFalse(result['receipt_attributed'])
                self.assertEqual(result['manager_status'], 'failed')

    def test_oversized_receipt_fails_closed_without_exposing_contents(self):
        self.receipt.write_bytes(b'PRIVATE_REDACTION_' + b'x' * 65536)
        snapshot = bridge._receipt_snapshot(self.receipt)
        self.assertEqual(snapshot, {'invalid': True})
        result = self.run_case()
        self.assertEqual(result['receipt_evidence'], 'invalid')
        self.assertEqual(result['manager_status'], 'failed')
        self.assertNotIn('PRIVATE_REDACTION_', json.dumps(result))

    def test_symlinked_receipt_is_never_trusted(self):
        external = self.root / 'private-target.json'
        external.write_text(json.dumps(self.fixture_receipt()))
        self.receipt.symlink_to(external)
        self.assertEqual(bridge._receipt_snapshot(self.receipt), {'invalid': True})
        result = self.run_case()
        self.assertEqual(result['receipt_evidence'], 'invalid')
        self.assertFalse(result['receipt_attributed'])
        self.assertEqual(result['manager_status'], 'failed')

    def test_new_receipt_with_historical_timestamp_is_rejected(self):
        receipt = self.fixture_receipt(
            time=(dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).isoformat())
        self.receipt_bytes = json.dumps(receipt).encode()
        result = self.run_case()
        self.assertEqual(result['receipt_evidence'], 'mismatch')
        self.assertFalse(result['receipt_attributed'])

    def test_valid_success_receipt_with_nonzero_exit_is_conflict(self):
        self.receipt_bytes = json.dumps(self.fixture_receipt()).encode()
        result = self.run_case()
        self.assertEqual(result['receipt_evidence'], 'conflicting_exit')
        self.assertEqual(result['manager_status'], 'failed')

    def test_retry_new_manager_is_bounded_and_does_not_reuse_receipt(self):
        self.stop_at_source = True
        self.install.checkout = self.release['revision']
        self.stderr = 'BLOCKED RETRY_NEW_MANAGER: synthetic'
        result = self.run_case()
        self.assertTrue(result['retry_new_manager'])
        self.assertEqual(result['receipt_evidence'], 'missing')
        self.assertEqual(result['error'], {'code': 'RETRY_NEW_MANAGER', 'stage': 'select_source'})
        self.assertEqual(result['backup_status'], 'not_observed')

    def test_retry_new_manager_is_denied_after_backup(self):
        self.install.checkout = self.release['revision']
        self.stderr = 'BLOCKED RETRY_NEW_MANAGER: synthetic after-backup spoof'
        result = self.run_case()
        self.assertFalse(result['retry_new_manager'])
        self.assertEqual(result['error']['stage'], 'post_backup')
        self.assertEqual(result['backup_status'], 'verified')
        self.assertEqual(result['manager_status'], 'failed')

    def test_zero_exit_without_receipt_after_source_selection_is_not_no_change(self):
        self.exit_code = 0
        self.do_backup = False
        self.current = {'revision': self.release['revision'], 'schema': self.release['schema']}
        self.plan['current'] = self.current
        result = self.run_case()
        self.assertEqual(result['manager_exit_code'], 0)
        self.assertEqual(result['receipt_evidence'], 'missing')
        self.assertFalse(result['no_change'])
        self.assertFalse(result['receipt_attributed'])
        self.assertEqual(result['manager_status'], 'not_written')

    def test_zero_exit_without_receipt_after_backup_is_not_no_change(self):
        self.exit_code = 0
        self.current = {'revision': self.release['revision'], 'schema': self.release['schema']}
        self.plan['current'] = self.current
        result = self.run_case()
        self.assertEqual(result['manager_exit_code'], 0)
        self.assertEqual(result['backup_status'], 'verified')
        self.assertFalse(result['no_change'])
        self.assertEqual(result['manager_status'], 'not_written')

    def test_error_code_is_allowlisted_and_never_leaks_secret(self):
        self.stderr = 'BLOCKED SEARCH_SECRET_INVALID: synthetic-secret-DO-NOT-EXPOSE\nSAFE_ACTION: secret'
        result = self.run_case()
        self.assertEqual(result['error']['code'], 'SEARCH_SECRET_INVALID')
        self.assertEqual(result['error']['stage'], 'post_backup')
        self.assertNotIn('synthetic-secret-DO-NOT-EXPOSE', json.dumps(result))

    def test_unverified_backup_is_not_reported_as_usable(self):
        self.verify_error = ValueError('synthetic unreadable backup')
        result = self.run_case()
        self.assertEqual(result['manager_status'], 'failed')
        self.assertEqual(result['backup_status'], 'unverified')
        self.assertIsNone(result['backup'])

    def test_success_without_new_receipt_only_no_change_when_already_deployed(self):
        self.exit_code = 0
        self.do_backup = False
        self.stop_after_doctor = True
        self.current = {'revision': self.release['revision'], 'schema': self.release['schema']}
        self.plan['current'] = self.current
        result = self.run_case()
        self.assertTrue(result['no_change'])
        self.assertEqual(result['manager_status'], 'no_change')
        self.assertFalse(result['receipt_attributed'])

    def test_nonzero_failure_after_backup_keeps_backup_readonly(self):
        old = self.fixture_receipt(time=(dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)).isoformat())
        self.receipt.write_text(json.dumps(old))
        prior = list(self.backup_root.iterdir())
        result = self.run_case()
        self.assertEqual(result['backup_status'], 'verified')
        self.assertEqual(list(self.backup_root.iterdir()), prior)
        self.assertEqual(result['manager_status'], 'failed')


class WorkerFreshReceiptGuardTests(unittest.TestCase):
    def test_zero_exit_without_receipt_is_not_accepted_or_finalized(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / 'state'
            state.mkdir()
            config = root / 'config.json'
            config.write_text(json.dumps({'installation': {
                'id': 'asa-test', 'root': str(root), 'state_root': str(state)}}))
            store = Store(state)
            plan = store.plan('asa-test', {'ready': True, 'release': {'revision': 'b' * 40}})
            record, _ = store.start('asa-test', 'update', plan['plan_id'],
                                    'receipt-guard-key-001', {})
            with patch('asa_ops.maintenance.check_generation'), \
                 patch('asa_ops.worker.adapter', return_value={
                     'manager_exit_code': 0, 'receipt_attributed': False,
                     'no_change': False, 'manager_status': 'not_written'}):
                work(str(config), record['operation_id'], 'update')
            current = store.get(record['operation_id'])
            self.assertEqual(current['status'], 'failed')
            self.assertEqual(current['result']['error']['code'], 'RECEIPT_NOT_CURRENT')


if __name__ == '__main__':
    unittest.main()
