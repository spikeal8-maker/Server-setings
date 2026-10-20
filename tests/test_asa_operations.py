import concurrent.futures
import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from asa_ops.store import Store, OpsError, digest, now
from asa_ops.schemas import INPUTS, tools, validate
from asa_ops.server import Controller, Official
from asa_ops.worker import work


class OperationStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.plan = self.store.plan('asa-test', {'ready': True, 'release': {'revision': 'a'*40}})

    def start(self, key='test-request-key-0001', request=None):
        return self.store.start('asa-test', 'update', self.plan['plan_id'], key, request or {'sha': 'a'*40})

    def test_durable_idempotency_after_reopen(self):
        first, created = self.start()
        self.assertTrue(created)
        self.store = Store(self.temp.name)
        second, created = self.start()
        self.assertFalse(created)
        self.assertEqual(first['operation_id'], second['operation_id'])

    def test_same_key_different_payload_rejected(self):
        self.start()
        with self.assertRaisesRegex(OpsError, 'different request'):
            self.start(request={'sha': 'b'*40})

    def test_parallel_start_only_one_wins(self):
        def attempt(n):
            try:
                return self.start('parallel-request-'+str(n))[1]
            except OpsError as e:
                return e.code
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            values = list(pool.map(attempt, range(8)))
        self.assertEqual(values.count(True), 1)
        self.assertEqual(values.count('BUSY'), 7)

    def test_dead_worker_requires_recovery_and_blocks_new_update(self):
        record, _ = self.start()
        with self.store.db() as db:
            db.execute("UPDATE operations SET updated=?,pid=?,process_identity=? WHERE id=?",
                       ('2020-01-01T00:00:00+00:00', 99999999, 'foreign-boot:start', record['operation_id']))
        self.store.reconcile()
        self.assertEqual(self.store.get(record['operation_id'])['status'], 'recovery_required')
        with self.assertRaises(OpsError):
            self.start('another-request-key-001')

    def test_completed_operation_releases_installation(self):
        record, _ = self.start()
        self.store.update(record['operation_id'], status='success', phase='complete', result={'accepted': True})
        second, created = self.start('second-request-key-0002')
        self.assertTrue(created)
        self.assertNotEqual(record['operation_id'], second['operation_id'])

    def test_queued_before_launch_is_abandoned_without_replay(self):
        record, _ = self.start()
        with self.store.db() as db:
            db.execute('UPDATE operations SET updated=? WHERE id=?', ('2020-01-01T00:00:00+00:00', record['operation_id']))
        self.store.reconcile()
        recovered = self.store.get(record['operation_id'])
        self.assertEqual(recovered['status'], 'failed')
        self.assertEqual(recovered['phase'], 'never_started')
        self.assertTrue(self.start('new-explicit-start-0001')[1])

    def test_immutable_plan_detects_tampering(self):
        with self.store.db() as db:
            db.execute('UPDATE plans SET value=?', (json.dumps({'ready': True, 'release': 'foreign'}),))
        with self.assertRaisesRegex(OpsError, 'integrity'):
            self.store.get_plan(self.plan['plan_id'])

    def test_logs_survive_reopen_and_incremental_cursor(self):
        record, _ = self.start()
        opid = record['operation_id']
        self.store.event(opid, 'backup', 'stage entered')
        part = self.store.log(opid)
        self.store.event(opid, 'verify', 'technical checks')
        second = Store(self.temp.name).log(opid, part['cursor'])
        self.assertEqual(len(second['lines']), 1)
        self.assertEqual(second['lines'][0]['phase'], 'verify')

    def test_path_traversal_rejected(self):
        for value in ['../../etc/passwd', 'a'*33, 12, None]:
            with self.assertRaises(OpsError):
                self.store.get(value)


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name)/'config.json'
        self.config.write_text(json.dumps({'installation': {'id': 'asa-test', 'root': self.temp.name,
                                    'state_root': str(Path(self.temp.name)/'state')}, 'official_command': []}))
        self.c = Controller(self.config, official=object())

    def plan(self, ready=True):
        return self.c.store.plan('asa-test', {'ready': ready, 'release': {'revision': 'a'*40}})

    def test_shell_sql_url_and_foreign_installation_rejected(self):
        for payload in [
            {'installation_id': 'asa-test', 'data_mode': 'restore'},
            {'installation_id': 'asa-test', 'data_mode': 'preserve', 'command': 'rm -rf /'},
            {'installation_id': 'foreign', 'data_mode': 'preserve'},
            {'installation_id': 'asa-test', 'data_mode': 'preserve', 'url': 'https://evil.example'},
        ]:
            with self.assertRaises(OpsError):
                self.c.dispatch('asa_update_plan', payload)

    def test_expired_plan_retry_returns_original_operation(self):
        plan = self.plan()
        args = {'installation_id': 'asa-test', 'plan_id': plan['plan_id'], 'idempotency_key': 'repeated-start-key-001'}
        with patch.object(self.c, 'launch', side_effect=lambda r, a: r) as launch:
            first = self.c.dispatch('asa_update_start', args)
            self.c.config['plan_ttl_seconds'] = -1
            again = self.c.dispatch('asa_update_start', args)
            self.assertEqual(first['operation']['operation_id'], again['operation']['operation_id'])
            self.assertTrue(again['reused'])
            self.assertEqual(launch.call_count, 1)

    def test_blocked_plan_never_launches(self):
        plan = self.plan(False)
        with patch.object(self.c, 'launch') as launch:
            with self.assertRaises(OpsError):
                self.c.dispatch('asa_update_start', {'installation_id': 'asa-test', 'plan_id': plan['plan_id'], 'idempotency_key': 'blocked-start-key-001'})
            launch.assert_not_called()

    def test_finalize_replay_never_relaunches_failed_attempt(self):
        plan = self.plan()
        record, _ = self.c.store.start('asa-test', 'update', plan['plan_id'], 'test-update-key-0001', {})
        opid = record['operation_id']
        self.c.store.update(opid, status='recovery_required')
        args = {'operation_id': opid, 'idempotency_key': 'test-finalize-key-0001'}
        with patch.object(self.c, 'launch', side_effect=lambda r, a: r) as launch:
            self.c.dispatch('asa_operation_finalize', args)
            self.c.store.update(opid, status='failed')
            repeated = self.c.dispatch('asa_operation_finalize', args)
            self.assertTrue(repeated['reused'])
            self.assertEqual(launch.call_count, 1)

    def test_schemas_closed_and_structured_error(self):
        for tool in tools():
            self.assertFalse(tool['inputSchema']['additionalProperties'])
            self.assertIn('outputSchema', tool)
        result = self.c.rpc({'method': 'tools/call', 'params': {'name': 'asa_operation_get', 'arguments': {'operation_id': '../secret'}}})
        self.assertTrue(result['isError'])
        self.assertFalse(result['structuredContent']['ok'])
        self.assertIn('code', result['structuredContent']['error'])

    def test_worker_pinned_manifest_and_one_continuation(self):
        plan = self.plan()
        record, _ = self.c.store.start('asa-test', 'update', plan['plan_id'], 'worker-update-key-001', {})
        calls = []
        def fake(config, action, payload, *unused):
            calls.append((action, payload))
            if action == 'update':
                return {'retry_new_manager': True, 'transition_fingerprint': {}, 'manager_exit_code': 2}
            return plan
        with patch('asa_ops.worker.adapter', side_effect=fake):
            work(str(self.config), record['operation_id'], 'update')
        self.assertEqual([c[0] for c in calls], ['update', 'continuation', 'update'])
        self.assertEqual(calls[0][1]['plan']['release'], calls[2][1]['plan']['release'])
        self.assertEqual(self.c.store.get(record['operation_id'])['status'], 'failed')


class ProtocolBridgeTest(unittest.TestCase):
    def test_official_stdio_tools_forwarding_and_new_operation_schemas(self):
        with tempfile.TemporaryDirectory() as td:
            child = Path(td)/'official.py'
            child.write_text('''import sys,json
for line in sys.stdin:
 r=json.loads(line)
 if "id" not in r: continue
 if r["method"]=="tools/list": result={"tools":[{"name":"existing_official_tool","inputSchema":{"type":"object"}}]}
 elif r["method"]=="tools/call": result={"structuredContent":{"ok":True}}
 else: result={"protocolVersion":"2025-03-26","capabilities":{},"serverInfo":{"name":"fixture","version":"1"}}
 print(json.dumps({"jsonrpc":"2.0","id":r["id"],"result":result}),flush=True)
''')
            official = Official([sys.executable, '-u', str(child)])
            try:
                self.assertEqual(official.request('tools/list', {})['tools'][0]['name'], 'existing_official_tool')
                self.assertTrue(official.request('tools/call', {'name': 'existing_official_tool'})['structuredContent']['ok'])
            finally:
                if official.process:
                    official.process.terminate();official.process.wait(timeout=5)
                    official.process.stdin.close();official.process.stdout.close()


if __name__ == '__main__':
    unittest.main()
