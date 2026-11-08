"""Exercise the real CLI main in an isolated child, with owned local fixtures.

STAGED uses the real worker function. ACTIVE uses a streaming worker boundary
fixture to verify CLI forwarding without running an exporter or host command.
No Linux ownership, 1Panel, network, or real backup is required.
"""
import json
from pathlib import Path
import subprocess
import sys
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'scripts/school-1panel-backup-schedules.py'
HARNESS = r'''
import copy
from contextlib import ExitStack, nullcontext
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from unittest import mock

source = Path(sys.argv[1]).resolve()
kind, mode = sys.argv[2:4]
spec = importlib.util.spec_from_file_location('owned_school_cli_fixture', source)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
generation = 'a' * 32
dispatch_id = 'b' * 32
checksum = hashlib.sha256(source.read_bytes()).hexdigest()
state = {'generation': generation, 'programPath': str(source), 'programSha256': checksum,
         'phase': 'STAGED' if mode == 'staged' else 'ACTIVE',
         'owner': 'legacy' if mode == 'staged' else '1panel'}
dispatch = {'dispatchId': dispatch_id, 'generation': generation, 'kind': kind,
            'phase': state['phase'], 'programSha256': checksum, 'createdAt': module.stamp()}
writes = []

def file_json(path):
    if path == module.STATE / 'dispatch' / (kind + '.json'):
        return copy.deepcopy(dispatch)
    if path == module.STATE / 'runs' / (kind + '.json'):
        return None
    raise AssertionError('CLI fixture attempted an unrelated read')

def atomic(path, value):
    assert path == module.STATE / 'runs' / (kind + '.json')
    writes.append(copy.deepcopy(value))

def active_stream(owned_state, owned_kind, owned_generation):
    assert owned_state == state and owned_kind == kind and owned_generation == generation
    progress = {'status': 'BACKUP_RUNNING', 'dispatchId': dispatch_id, 'kind': kind}
    result = {'status': 'BACKUP_CLOUD_SHA256_PASS', 'dispatchId': dispatch_id, 'kind': kind}
    print(json.dumps(progress, sort_keys=True), flush=True)
    print(json.dumps(result, sort_keys=True), flush=True)
    return result

with ExitStack() as stack:
    stack.enter_context(mock.patch.object(module.sys, 'argv', [str(source), 'worker', '--kind', kind,
                        '--generation', generation, '--apply']))
    stack.enter_context(mock.patch.object(module, 'check_host'))
    stack.enter_context(mock.patch.object(module, 'lock', side_effect=lambda *args, **kwargs: nullcontext()))
    stack.enter_context(mock.patch.object(module, 'load_state', return_value=copy.deepcopy(state)))
    stack.enter_context(mock.patch.object(module, 'file_json', side_effect=file_json))
    stack.enter_context(mock.patch.object(module, 'atomic', side_effect=atomic))
    runner_check = stack.enter_context(mock.patch.object(module, 'verify_runners'))
    stack.enter_context(mock.patch.object(module, 'Panel', side_effect=AssertionError('No native API in worker CLI fixture')))
    stack.enter_context(mock.patch.object(module, 'Legacy', side_effect=AssertionError('No legacy lifecycle in fixture')))
    stack.enter_context(mock.patch.object(module.subprocess, 'Popen', side_effect=AssertionError('No exporter in fixture')))
    stack.enter_context(mock.patch.object(module.subprocess, 'run', side_effect=AssertionError('No host subprocess in fixture')))
    if mode == 'active':
        stack.enter_context(mock.patch.object(module, 'worker', side_effect=active_stream))
    code = module.main()
    assert code == 0
    if mode == 'staged':
        runner_check.assert_called_once_with()
        assert len(writes) == 2
        assert writes[0]['status'] == 'STARTING'
        assert writes[1]['status'] == 'STAGED_GATE_VERIFIED_NO_BACKUP'
        assert writes[1]['backupExecuted'] is False
    else:
        assert not writes
        runner_check.assert_not_called()
raise SystemExit(code)
'''


class WorkerCliTests(unittest.TestCase):
    def run_cli(self, kind, mode):
        result = subprocess.run([sys.executable, '-I', '-B', '-c', HARNESS, str(SOURCE), kind, mode],
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, '')
        return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]

    def test_real_staged_worker_cli_emits_one_terminal_marker_for_each_kind(self):
        for kind in ('database', 'settings'):
            with self.subTest(kind=kind):
                records = self.run_cli(kind, 'staged')
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]['status'], 'STAGED_GATE_VERIFIED_NO_BACKUP')
                self.assertEqual(records[0]['kind'], kind)
                self.assertFalse(records[0]['backupExecuted'])
                self.assertEqual(records[0]['level'], 'INFO')
                self.assertIn('timestamp', records[0])

    def test_active_worker_stream_keeps_progress_and_one_terminal_result(self):
        for kind in ('database', 'settings'):
            with self.subTest(kind=kind):
                records = self.run_cli(kind, 'active')
                self.assertEqual([row['status'] for row in records],
                                 ['BACKUP_RUNNING', 'BACKUP_CLOUD_SHA256_PASS'])
                self.assertTrue(all(row['kind'] == kind for row in records))
                self.assertEqual(len({row['dispatchId'] for row in records}), 1)


if __name__ == '__main__':
    unittest.main()
