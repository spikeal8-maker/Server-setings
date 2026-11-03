"""Offline fixtures. Native FD mutations use only a fresh temporary directory.

Positive root metadata is simulated; actual root staging/systemd remain separate.
The focused CI supplies an immutable public 2a checkout for real transport reuse.
"""
from contextlib import contextmanager, redirect_stdout
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import ast
import hashlib
import importlib.util
import io
import json
import os
import re
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch, Mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('provider_exporter_tested', ROOT/'scripts/provider_health_exporter.py')
exporter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(exporter)


def receipt(at=None):
    at = at or exporter.stamp()
    return dict(format=exporter.FORMAT, sourceRevision=exporter.SOURCE_REVISION,
                configSha256='a'*64, sampleId='b'*32, startedAt=at, checkedAt=at,
                time=at, kind='provider_health', level='info',
                providers=[dict(id=name, checkedAt=at, availability='reachable', failure='none', durationMs=1,
                                stats=({'ramTotalBytes': 100, 'ramFreeBytes': 50, 'deviceCount': 1,
                                        'cudaDeviceCount': 1, 'vramTotalBytes': 10, 'vramFreeBytes': 5}
                                       if name == 'comfyui' else {'loadedModels': 0, 'availableModels': 1}))
                           for name in exporter.PROVIDERS])


def immutable_blobs(test):
    source = os.environ.get('ASA_PROVIDER_TEST_SOURCE')
    if not source:
        test.skipTest('Exact public 2a source supplied by focused workflow or ASA_PROVIDER_TEST_SOURCE')
    blobs = {p: (Path(source)/'tools'/p).read_bytes() for p in exporter.PINS}
    test.assertEqual({p: (len(b), hashlib.sha256(b).hexdigest()) for p, b in blobs.items()}, exporter.PINS)
    return blobs


def config():
    return {'providers': {'ollama': {'origin': 'http://127.0.0.1:11434'},
                          'comfyui': {'origin': 'http://127.0.0.1:8188'}}}


def collector_functions(test):
    """Test-only exact 2a consumer: execute selected pure AST, no collector imports/I/O."""
    source = os.environ.get('ASA_PROVIDER_TEST_SOURCE')
    if not source:
        test.skipTest('Exact public 2a collect_logs.py supplied by ASA_PROVIDER_TEST_SOURCE')
    raw = (Path(source)/'tools/collect_logs.py').read_bytes()
    test.assertEqual((len(raw), hashlib.sha256(raw).hexdigest()),
                     (138984, '8e728afecab47a89ac73261fc588faf7be172f6ff32b77db16138d7d079a00f2'))
    functions = {'clean', 'timestamp', 'file_timestamp', 'log_level', 'host_source', 'editor_module',
                 'subsystem', 'classification_payload', 'event_subsystem', 'classify_event', 'normalize'}
    constants = {'UTC', 'SENSITIVE', 'EDITOR_MODULES', 'LOG_MODULES'}
    nodes = [node for node in ast.parse(raw).body
             if (isinstance(node, ast.FunctionDef) and node.name in functions)
             or (isinstance(node, ast.Assign) and len(node.targets) == 1
                 and isinstance(node.targets[0], ast.Name) and node.targets[0].id in constants)]
    test.assertEqual({n.name for n in nodes if isinstance(n, ast.FunctionDef)}, functions)
    test.assertEqual({n.targets[0].id for n in nodes if isinstance(n, ast.Assign)}, constants)
    scope = dict(json=json, re=re, hashlib=hashlib, datetime=datetime, timedelta=timedelta, timezone=timezone)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), '<immutable-2a-collector-pure-functions>', 'exec'), scope)
    return types.SimpleNamespace(**{name: scope[name] for name in functions})


class ReceiptContracts(unittest.TestCase):
    def test_exact_typed_success_and_current_config(self):
        value = receipt()
        self.assertIs(exporter.validate_receipt(value), value)
        self.assertLess(exporter.audit(value, 'a'*64), 2)

    def test_whitelist_rejects_extra_fields_model_names_and_type_confusion(self):
        for mutate in [lambda v: v.update(url='private'), lambda v: v['providers'][0].update(raw='private'),
                       lambda v: v['providers'][1]['stats'].update(model='private'),
                       lambda v: v['providers'][0]['stats'].update(deviceCount=True),
                       lambda v: v['providers'][0]['stats'].update(ramFreeBytes=101),
                       lambda v: v['providers'][1]['stats'].update(availableModels=257),
                       lambda v: v['providers'][1].update(durationMs=True),
                       lambda v: v['providers'][0].update(failure='raw exception'),
                       lambda v: v['providers'].reverse(), lambda v: v['providers'].pop(),
                       lambda v: v.update(sourceRevision='c'*40)]:
            value = receipt()
            mutate(value)
            with self.subTest(value=value), self.assertRaises(exporter.Blocked):
                exporter.validate_receipt(value)

    def test_latest_receipt_cannot_be_empty_stale_future_or_relabelled(self):
        now = datetime.now(timezone.utc)
        for seconds in (-121, 1):
            at = (now+timedelta(seconds=seconds)).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
            with self.assertRaises(exporter.Blocked):
                exporter.audit(receipt(at), 'a'*64, now)
        with self.assertRaises(exporter.Blocked):
            exporter.audit(receipt(), 'c'*64)
        with self.assertRaises(exporter.Blocked):
            exporter.audit({}, 'a'*64)

    def test_fresh_failure_never_becomes_availability(self):
        value = receipt()
        value['providers'][0].update(availability='unavailable', failure='timeout', stats={})
        value['level'] = 'error'
        exporter.validate_receipt(value)
        with self.assertRaisesRegex(exporter.Blocked, 'PROVIDER_UNAVAILABLE'):
            exporter.audit(value, 'a'*64)

    def test_event_envelope_is_exact_and_cannot_relabel_provider_outcome(self):
        for unavailable in (False, True):
            base = receipt()
            if unavailable:
                base['providers'][0].update(availability='unavailable', failure='timeout', stats={})
                base['level'] = 'error'
            exporter.validate_receipt(base)
            for key, values in {'time': ['2000-01-01T00:00:00.000Z', None, True, []],
                                'kind': ['http_response', None, True, {}],
                                'level': ['info' if unavailable else 'error', 'warn', None, False, []]}.items():
                for item in values:
                    value = deepcopy(base)
                    value[key] = item
                    with self.subTest(unavailable=unavailable, key=key, item=item), self.assertRaisesRegex(exporter.Blocked, 'RECEIPT_EVENT_ENVELOPE'):
                        exporter.validate_receipt(value)
                value = deepcopy(base)
                del value[key]
                with self.assertRaisesRegex(exporter.Blocked, 'RECEIPT_FORMAT'):
                    exporter.validate_receipt(value)

    def test_duplicate_json_keys_and_parent_paths_are_denied(self):
        with self.assertRaises(exporter.Blocked):
            exporter.decode(b'{"version":1,"version":2}')
        for path in ('relative', '/safe/../foreign', ''):
            with self.assertRaises(exporter.Blocked):
                exporter.absolute(path)

    def test_provider_projection_contains_only_closed_numeric_stats(self):
        value = {'ramTotalBytes': 100, 'ramFreeBytes': 50, 'model': 'PRIVATE MODEL',
                 'devices': [{'type': 'cuda', 'name': 'PRIVATE GPU', 'vram_total': 10, 'vram_free': 5}]}
        result = exporter.project('comfyui', value)
        self.assertTrue(all(type(v) is int for v in result.values()))
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_default_cli_refuses_nonisolated_entry_before_reading_paths(self):
        result = subprocess.run([sys.executable, '-B', str(ROOT/'scripts/provider_health_exporter.py'),
                                 '--config', 'SHOULD_NOT_BE_OPENED', '--apply'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)['code'], 'ISOLATED_PYTHON_REQUIRED')
        self.assertEqual(result.stderr, '')


class ActualPinnedTransport(unittest.TestCase):
    def test_probed_success_and_failure_use_actual_collector_event_time_and_level(self):
        consumer = collector_functions(self)
        replies = {'/api/ps': {'models': []}, '/api/tags': {'models': []},
                   '/system_stats': {'system': {'ram_total': 100, 'ram_free': 40}, 'devices': []}}
        with exporter.approved_modules(immutable_blobs(self)) as operational:
            for failed_provider in (None, 'comfyui', 'ollama'):
                def reply(target, path):
                    if failed_provider and path in exporter.GETS[failed_provider]:
                        raise TimeoutError('PRIVATE ERROR')
                    return replies[path]
                with patch.object(operational, 'fetch_json', side_effect=reply):
                    value = exporter.probe(config(), 'a'*64, operational)
                raw = exporter.encode(value).decode()
                level = 'error' if failed_provider else 'info'
                self.assertEqual(value['time'], value['checkedAt'])
                self.assertEqual(value['kind'], 'provider_health')
                self.assertEqual(value['level'], level)
                at, authority = consumer.file_timestamp(raw, '2001-01-01T00:00:00.000Z')
                self.assertEqual((at, authority), (value['checkedAt'], 'event'))
                event = consumer.normalize('provider-monitor', raw, at, value['sampleId'])
                self.assertEqual((event['time'], event['level'], event['source']), (value['checkedAt'], level, 'provider-monitor'))
                self.assertEqual(json.loads(event['message'])['kind'], 'provider_health')
                if failed_provider:
                    with self.assertRaisesRegex(exporter.Blocked, '^PROVIDER_UNAVAILABLE$'):
                        exporter.audit(value, 'a'*64)
                else:
                    exporter.audit(value, 'a'*64)
                legacy = {k: v for k, v in value.items() if k not in ('time', 'kind', 'level')}
                legacy_raw = exporter.encode(legacy).decode()
                fallback, authority = consumer.file_timestamp(legacy_raw, '2001-01-01T00:00:00.000Z')
                self.assertEqual((fallback, authority), ('2001-01-01T00:00:00.000Z', 'file_mtime'))
                self.assertEqual(consumer.normalize('provider-monitor', legacy_raw, fallback, 'legacy')['level'], 'info')

    def test_exact_closure_dependencies_and_no_user_module_path(self):
        blobs = immutable_blobs(self)
        before = list(sys.path)
        with exporter.approved_modules(blobs) as operational:
            self.assertEqual(operational.PROVIDERS, exporter.GETS)
            self.assertEqual(sys.modules['deployment'].__path__, [])
            self.assertIs(sys.modules['deployment.paths'].plain_paths, operational.plain_paths)
        self.assertEqual(sys.path, before)
        self.assertNotIn('deployment', sys.modules)

    def test_every_wrong_pin_is_rejected_before_any_module_execution(self):
        blobs = immutable_blobs(self)
        for path in blobs:
            changed = {**blobs, path: blobs[path]+b'\nraise RuntimeError("MUST NEVER EXECUTE")\n'}
            with patch.object(exporter.types, 'ModuleType', side_effect=AssertionError('import')), self.assertRaisesRegex(exporter.Blocked, 'SOURCE_PIN_MISMATCH'):
                with exporter.approved_modules(changed):
                    pass

    def test_existing_module_collision_is_not_replaced(self):
        blobs = immutable_blobs(self)
        marker = types.ModuleType('deployment')
        with patch.dict(sys.modules, {'deployment': marker}), self.assertRaisesRegex(exporter.Blocked, 'IMPORT_COLLISION'):
            with exporter.approved_modules(blobs):
                pass
        self.assertNotIn('deployment', sys.modules)

    def test_real_transport_three_gets_project_actual_provider_schema(self):
        with exporter.approved_modules(immutable_blobs(self)) as operational:
            replies = {'/api/ps': {'models': [{'name': 'PRIVATE MODEL', 'size': 10, 'size_vram': 5}]},
                       '/api/tags': {'models': [{'name': 'PRIVATE MODEL'}]},
                       '/system_stats': {'system': {'ram_total': 100, 'ram_free': 40},
                                         'devices': [{'type': 'cuda', 'index': 0, 'name': 'PRIVATE GPU',
                                                      'vram_total': 20, 'vram_free': 10}]}}
            with patch.object(operational, 'fetch_json', side_effect=lambda target, path: replies[path]) as fetch:
                value = exporter.probe(config(), 'a'*64, operational)
            self.assertEqual(fetch.call_args_list, [unittest.mock.call('http://127.0.0.1:8188', '/system_stats'),
                                                   unittest.mock.call('http://127.0.0.1:11434', '/api/ps'),
                                                   unittest.mock.call('http://127.0.0.1:11434', '/api/tags')])
            self.assertNotIn('PRIVATE', json.dumps(value))
            self.assertNotIn('127.0.0.1', json.dumps(value))
            exporter.audit(value, 'a'*64)

    def test_timeout_and_deadline_emit_failed_receipts_without_exception_payload(self):
        with exporter.approved_modules(immutable_blobs(self)) as operational:
            with patch.object(operational, 'fetch_json', side_effect=TimeoutError('PRIVATE ERROR')):
                value = exporter.probe(config(), 'a'*64, operational)
            self.assertTrue(all(r['failure'] == 'timeout' for r in value['providers']))
            self.assertNotIn('PRIVATE', json.dumps(value))
            ticks = iter([0] + [21]*30)
            with patch.object(exporter.time, 'monotonic', side_effect=lambda: next(ticks)), patch.object(operational, 'fetch_json') as fetch:
                value = exporter.probe(config(), 'a'*64, operational)
            fetch.assert_not_called()
            self.assertTrue(all(r['failure'] == 'deadline' for r in value['providers']))

    def test_real_fetch_rejects_oversized_body_and_deadline_without_network(self):
        with exporter.approved_modules(immutable_blobs(self)) as operational:
            for oversized in (True, False):
                connection = Mock()
                response = connection.getresponse.return_value
                response.status = 200
                response.getheader.return_value = str(operational.HTTP_BYTES+1) if oversized else None
                with patch.object(operational.http.client, 'HTTPConnection', return_value=connection) as http, patch.object(operational.threading, 'Timer'):
                    if oversized:
                        with self.assertRaises(ValueError):
                            operational.fetch_json('http://127.0.0.1:11434', '/api/ps')
                    else:
                        ticks = iter([10, 14] + [14]*20)
                        with patch.object(operational.time, 'monotonic', side_effect=lambda: next(ticks)), self.assertRaises(TimeoutError):
                            operational.fetch_json('http://127.0.0.1:11434', '/api/ps')
                http.assert_called_once_with('127.0.0.1', 11434, timeout=3)
                connection.request.assert_called_once_with('GET', '/api/ps', headers={'Accept': 'application/json'})
                connection.close.assert_called_once()

    def test_origins_never_admit_dns_public_auth_paths_or_queries(self):
        with exporter.approved_modules(immutable_blobs(self)) as operational:
            for value in ('https://example.com', 'http://8.8.8.8', 'http://u:p@127.0.0.1',
                          'http://127.0.0.1/api/generate', 'http://127.0.0.1?secret=x', 'http://169.254.1.1'):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    operational.origin(value)


@unittest.skipUnless(sys.platform == 'linux', 'Native nofollow/dirfd/flock filesystem fixtures require Linux')
class NativeFilesystemContracts(unittest.TestCase):
    @contextmanager
    def root_metadata(self, bad_parent=None):
        """Synthetic ownership only; preserve real links/inodes/modes/CAS/bytes."""
        fstat, os_stat = os.fstat, os.stat
        def project(info, name):
            fields = {k: getattr(info, k) for k in ('st_dev', 'st_ino', 'st_mode', 'st_size', 'st_nlink', 'st_mtime_ns', 'st_ctime_ns')}
            fields.update(st_uid=1000 if name == bad_parent else 0,
                          st_gid=1000 if stat.S_ISDIR(info.st_mode) or name in exporter.LOGS else 0)
            if stat.S_ISDIR(info.st_mode):
                fields['st_mode'] &= ~0o022
            return types.SimpleNamespace(**fields)
        def fstat_view(fd):
            return project(fstat(fd), Path(os.readlink('/proc/self/fd/'+str(fd))).name)
        def stat_view(path, *args, **kwargs):
            return project(os_stat(path, *args, **kwargs), Path(path).name)
        with patch.object(exporter.os, 'fstat', side_effect=fstat_view), patch.object(exporter.os, 'stat', side_effect=stat_view), patch.object(exporter.os, 'fchown'):
            yield

    def test_real_untrusted_temp_ancestor_denied(self):
        with tempfile.TemporaryDirectory(prefix='provider-negative-') as tmp:
            with self.assertRaises(exporter.Blocked):
                with exporter.directory(tmp):
                    pass

    def test_all_ancestor_owners_and_directory_symlinks_are_checked(self):
        with tempfile.TemporaryDirectory(prefix='provider-ancestor-') as tmp:
            parent = Path(tmp)/'foreign-parent'
            child = parent/'child'
            child.mkdir(parents=True)
            with self.root_metadata('foreign-parent'), self.assertRaisesRegex(exporter.Blocked, 'UNTRUSTED_DIRECTORY'):
                with exporter.directory(child):
                    pass
            link = Path(tmp)/'redirect'
            link.symlink_to(child, target_is_directory=True)
            with self.root_metadata(), self.assertRaises(OSError):
                with exporter.directory(link):
                    pass

    def test_real_file_symlink_hardlink_fifo_and_permissive_mode_denied(self):
        with tempfile.TemporaryDirectory(prefix='provider-input-') as tmp:
            root = Path(tmp)
            target = root/'target'
            target.write_bytes(b'public bytes')
            target.chmod(0o600)
            (root/'link').symlink_to(target)
            os.link(target, root/'hardlink')
            os.mkfifo(root/'fifo', 0o600)
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with self.root_metadata():
                    for name in ('link', 'hardlink', 'fifo'):
                        with self.subTest(name=name), self.assertRaises((OSError, exporter.Blocked)):
                            exporter.read_file(fd, name, 100)
                    (root/'hardlink').unlink()
                    target.chmod(0o664)
                    with self.assertRaises(exporter.Blocked):
                        exporter.read_file(fd, 'target', 100)
            finally:
                os.close(fd)

    def test_rotation_latest_and_check_use_real_temp_files_under_lock(self):
        with tempfile.TemporaryDirectory(prefix='provider-rotation-') as tmp:
            root = Path(tmp)
            root.chmod(0o750)
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with self.root_metadata(), patch.object(exporter, 'MAX_LOG', len(exporter.encode(receipt()))+16):
                    exporter.output_directory(fd, 1000)
                    with exporter.locked(fd, create=True):
                        for index in range(7):
                            value = receipt()
                            value['sampleId'] = format(index, '032x')
                            exporter.publish(fd, 1000, value, exporter.output_files(fd, 1000))
                    with exporter.locked(fd, create=False):
                        files = exporter.output_files(fd, 1000)
                        self.assertEqual(set(files), {*exporter.LOGS, 'latest.json', 'probe.lock'})
                        self.assertEqual(files['health.log'][0].splitlines(keepends=True)[-1], files['latest.json'][0])
                        self.assertLess(exporter.audit(exporter.decode(files['latest.json'][0]), 'a'*64), 2)
                        self.assertEqual(sum(len(files[n][0].splitlines()) for n in exporter.LOGS), 5)
            finally:
                os.close(fd)

    def test_foreign_or_interrupted_output_is_never_overwritten(self):
        with tempfile.TemporaryDirectory(prefix='provider-foreign-') as tmp:
            root = Path(tmp)
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                for name, body in [('business.txt', b'foreign'), ('health.log', b'{"foreign":1}\n'),
                                   ('health.log', b'partial'), ('latest.json', b'{}')]:
                    p = root/name
                    p.write_bytes(body)
                    p.chmod(0o640 if name in exporter.LOGS else 0o600)
                    with self.root_metadata(), self.assertRaises(exporter.Blocked):
                        exporter.output_files(fd, 1000)
                    self.assertEqual(p.read_bytes(), body)
                    p.unlink()
            finally:
                os.close(fd)

    def test_output_compare_and_swap_rejects_replaced_receipt(self):
        with tempfile.TemporaryDirectory(prefix='provider-cas-') as tmp:
            root = Path(tmp)
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with self.root_metadata(), exporter.locked(fd, create=True):
                    exporter.publish(fd, 1000, receipt(), exporter.output_files(fd, 1000))
                    previous = exporter.output_files(fd, 1000)
                    replacement = root/'replacement'
                    replacement.write_bytes(previous['latest.json'][0])
                    replacement.chmod(0o600)
                    os.replace(replacement, root/'latest.json')
                    with self.assertRaisesRegex(exporter.Blocked, 'OUTPUT_CHANGED'):
                        exporter.publish(fd, 1000, receipt(), previous)
            finally:
                os.close(fd)

    def test_plan_executes_no_probe_and_creates_no_output(self):
        blobs = immutable_blobs(self)
        with tempfile.TemporaryDirectory(prefix='provider-plan-') as tmp:
            root = Path(tmp)
            bundle = root/'bundle/deployment'
            bundle.mkdir(parents=True)
            for p, raw in blobs.items():
                target = bundle/Path(p).name
                target.write_bytes(raw)
                target.chmod(0o600)
            output = root/'output'
            output.mkdir(mode=0o750)
            output.chmod(0o750)
            value = {**config(), 'version': 1, 'readerGid': 1000, 'bundleRoot': str(bundle.parent), 'outputDirectory': str(output)}
            cfg = root/'config.json'
            cfg.write_text(json.dumps(value))
            cfg.chmod(0o600)
            flags = types.SimpleNamespace(**{k: getattr(sys.flags, k) for k in dir(sys.flags) if not k.startswith('_') and not callable(getattr(sys.flags, k))})
            flags.isolated = 1
            with self.root_metadata(), patch.object(exporter.sys, 'flags', flags), patch.object(exporter.sys, 'dont_write_bytecode', True), patch.object(exporter.os, 'geteuid', return_value=0), patch.object(exporter, 'probe', side_effect=AssertionError('network')), patch.object(exporter, 'locked', side_effect=AssertionError('write')), redirect_stdout(io.StringIO()) as stdout:
                self.assertEqual(exporter.main(['--config', str(cfg)]), 0)
            self.assertEqual(json.loads(stdout.getvalue())['status'], 'PLAN')
            self.assertEqual(list(output.iterdir()), [])


class Templates(unittest.TestCase):
    def test_example_and_unit_share_dedicated_persistent_root_controlled_output(self):
        value = json.loads((ROOT/'templates/provider-monitor/config.example.json').read_text())
        service = (ROOT/'templates/provider-monitor/asa-provider-monitor.service').read_text()
        doc = (ROOT/'docs/48-ISOLATED-PROVIDER-MONITOR.md').read_text(encoding='utf-8')
        self.assertEqual(value['outputDirectory'], '/var/lib/asa-provider-monitor')
        self.assertEqual(value['bundleRoot'], '/opt/asa-provider-monitor/app-source/tools')
        self.assertNotIn('asa-stabilization-cli-909', json.dumps(value))
        self.assertEqual([line for line in service.splitlines() if line.startswith('ReadWritePaths=')],
                         ['ReadWritePaths='+value['outputDirectory']])
        self.assertIn('`'+value['outputDirectory']+'`', doc)
        for setting in ('User=root', 'Group=root', 'ProtectSystem=strict', 'UMask=0077'):
            self.assertIn(setting, service.splitlines())

    def test_manual_unit_has_no_installer_generation_ports_or_user_import_path(self):
        service = (ROOT/'templates/provider-monitor/asa-provider-monitor.service').read_text()
        timer = (ROOT/'templates/provider-monitor/asa-provider-monitor.timer').read_text()
        self.assertIn('/usr/bin/python3 -I -B ', service)
        self.assertIn('--apply', service)
        self.assertIn('NoNewPrivileges=true', service)
        self.assertIn('ProtectSystem=strict', service)
        self.assertIn('OnUnitActiveSec=60s', timer)
        for forbidden in ('PYTHONPATH', '192.168.', 'ExecStartPre=', 'ExecStartPost=', 'Restart=', 'ListenStream='):
            self.assertNotIn(forbidden, service+timer)


if __name__ == '__main__':
    unittest.main()
