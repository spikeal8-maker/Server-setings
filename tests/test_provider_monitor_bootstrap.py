"""Offline source contracts; root-native fixtures opt in only on ephemeral Linux CI."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('provider_bootstrap', ROOT/'scripts/bootstrap-provider-monitor.py')
bootstrap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bootstrap)


class BootstrapContracts(unittest.TestCase):
    def test_manifest_is_independently_pinned_and_all_runtime_blobs_unchanged(self):
        raw = (ROOT/'templates/provider-monitor/bootstrap-manifest.json').read_bytes()
        self.assertEqual((len(raw), hashlib.sha256(raw).hexdigest()), bootstrap.MANIFEST_PIN)
        manifest = json.loads(raw)
        self.assertEqual(manifest['applicationRevision'], bootstrap.APP_REVISION)
        self.assertEqual(set(manifest['applicationFiles']), bootstrap.APPLICATION_FILES)
        self.assertEqual(set(manifest['sourceFiles']), bootstrap.SOURCE_FILES)
        # Git raw LF bytes, not Windows checkout materialization or text normalization.
        for name, pin in manifest['sourceFiles'].items():
            raw = (subprocess.check_output(['git', 'show', 'HEAD:'+name], cwd=ROOT) if (ROOT/'.git').exists()
                   else (ROOT/name).read_bytes())
            self.assertEqual((len(raw), hashlib.sha256(raw).hexdigest()), (pin['bytes'], pin['sha256']))
            if name.endswith('.service'):
                self.assertEqual(raw.count(b'/opt/asa-provider-monitor'), 2)
                projected = raw.replace(b'/opt/asa-provider-monitor', b'/var/lib/asa-provider-monitor-runtime')
                self.assertEqual((len(projected), hashlib.sha256(projected).hexdigest()),
                                 (manifest['serviceProjection']['bytes'], manifest['serviceProjection']['sha256']))

    def test_private_numeric_origins_no_auth_dns_redirect_path_or_injection(self):
        for value in ('http://127.0.0.1:11434', 'http://192.168.1.20:8188', 'https://[fd00::1]:443'):
            self.assertEqual(bootstrap.origin(value), value)
        for value in ('http://localhost:1', 'http://8.8.8.8:1', 'http://127.0.0.1', 'http://127.0.0.1:1/',
                      'http://u:p@127.0.0.1:1', 'http://127.0.0.1:1?key=x', 'http://127.0.0.1:1#x',
                      'http://0.0.0.0:1', 'http://169.254.1.1:1', 'http://127.0.0.1:01'):
            with self.subTest(value=value), self.assertRaises((bootstrap.Blocked, ValueError)):
                bootstrap.origin(value)

    def test_plan_never_downloads_writes_activates_or_probes(self):
        args = types.SimpleNamespace(reader_gid=1000, offline_bundle=None, apply=False, activate=False)
        with patch.object(bootstrap.os, 'geteuid', return_value=0, create=True), patch.object(bootstrap, 'read'), \
             patch.object(bootstrap, 'sources', return_value=({}, {})), patch.object(bootstrap, 'configuration', return_value=b'config'), \
             patch.object(bootstrap, 'inspect', return_value=False), patch.object(bootstrap, 'download') as fetch, \
             patch.object(bootstrap, 'write_new') as write, patch.object(bootstrap, 'activate') as activate, \
             patch.object(bootstrap, 'command', side_effect=AssertionError('systemd')), \
             patch.object(bootstrap.subprocess, 'run', side_effect=AssertionError('process')):
            value = bootstrap.install(args)
        self.assertEqual(value['status'], 'PLAN')
        self.assertFalse(value['writes'])
        fetch.assert_not_called()
        write.assert_not_called()
        activate.assert_not_called()

    def test_plan_static_inspection_never_queries_systemd(self):
        with patch.object(bootstrap, 'present', return_value=None), patch.object(bootstrap, 'unit_guard', side_effect=AssertionError('systemd')):
            self.assertFalse(bootstrap.inspect(b'config', {}, {}, 1000))

    def test_existing_manual_layout_is_rejected_before_any_write_or_download(self):
        args = types.SimpleNamespace(reader_gid=1000, offline_bundle=None, apply=True, activate=True)
        def present(path):
            return (1,) if path in (bootstrap.CONFIG, bootstrap.OUTPUT) else None
        with patch.object(bootstrap.os, 'geteuid', return_value=0, create=True), patch.object(bootstrap, 'read'), \
             patch.object(bootstrap, 'sources', return_value=({}, {})), patch.object(bootstrap, 'configuration', return_value=b'config'), \
             patch.object(bootstrap, 'present', side_effect=present), patch.object(bootstrap, 'download', side_effect=AssertionError('network')), \
             patch.object(bootstrap, 'write_new', side_effect=AssertionError('write')), patch.object(bootstrap, 'make_dir', side_effect=AssertionError('mkdir')), \
             patch.object(bootstrap, 'unit_guard', side_effect=AssertionError('systemd')):
            with self.assertRaisesRegex(bootstrap.Blocked, '^PARTIAL_INSTALL_PRESERVED$'):
                bootstrap.install(args)

    def test_source_download_is_fixed_pinned_bounded_and_rejects_redirect(self):
        pin = {'bytes': 3, 'sha256': hashlib.sha256(b'abc').hexdigest()}
        for status, chunks, expected in [(200, [b'abc', b''], None), (302, [], 'SOURCE_HTTP'),
                                         (200, [b'abcd'], 'SOURCE_HTTP_BOUND'), (200, [b'ab', b''], 'SOURCE_PIN_MISMATCH')]:
            connection = Mock()
            response = connection.getresponse.return_value
            response.status = status
            response.getheader.return_value = None
            response.read1.side_effect = chunks
            with patch.object(bootstrap.http.client, 'HTTPSConnection', return_value=connection) as factory, patch.object(bootstrap.threading, 'Timer'):
                if expected:
                    with self.assertRaisesRegex(bootstrap.Blocked, expected):
                        bootstrap.fetch_http('deployment/paths.py', pin)
                else:
                    self.assertEqual(bootstrap.fetch_http('deployment/paths.py', pin), b'abc')
            factory.assert_called_once_with('raw.githubusercontent.com', timeout=10)
            connection.request.assert_called_once_with('GET', '/spikeal8-maker/asa-lab/'+bootstrap.APP_REVISION+'/tools/deployment/paths.py', headers={'Accept': 'application/octet-stream'})
            connection.close.assert_called_once()

    def test_unit_collision_and_dropin_fail_closed(self):
        for props in ('LoadState=loaded\nFragmentPath=/usr/lib/systemd/system/foreign.service\nDropInPaths=\n',
                      'LoadState=loaded\nFragmentPath=\nDropInPaths=/etc/foreign.conf\n', ''):
            with patch.object(bootstrap.subprocess, 'run', return_value=types.SimpleNamespace(returncode=0, stdout=props)), self.assertRaises(bootstrap.Blocked):
                bootstrap.unit_guard(False)

    def test_duplicate_json_never_silently_overwrites_config(self):
        with self.assertRaisesRegex(bootstrap.Blocked, 'DUPLICATE_JSON_KEY'):
            bootstrap.decoded(b'{"fileSources":[],"fileSources":[]}')

    def test_root_reader_group_is_explicit_existing_and_not_docker_or_primary_zero(self):
        groups = types.SimpleNamespace(
            getgrgid=lambda gid: types.SimpleNamespace(gr_gid=gid) if gid in (1000, 998, 999) else (_ for _ in ()).throw(KeyError(gid)),
            getgrnam=lambda name: types.SimpleNamespace(gr_gid=998))
        with patch.dict(sys.modules, {'grp': groups}), \
             patch.object(bootstrap.os, 'stat', return_value=types.SimpleNamespace(st_gid=999)):
            self.assertEqual(bootstrap.root_reader_gid({'collectorMode': 'system-root', 'collectorReaderGid': 1000}), 1000)
            for value in (None, False, 0, -1, 998, 999, 2**31, '1000'):
                with self.subTest(value=value), self.assertRaises(bootstrap.Blocked):
                    bootstrap.root_reader_gid({'collectorMode': 'system-root', 'collectorReaderGid': value})
            with self.assertRaises(KeyError):
                bootstrap.root_reader_gid({'collectorMode': 'system-root', 'collectorReaderGid': 1001})
            with self.assertRaisesRegex(bootstrap.Blocked, 'ROOT_READER_GROUP'):
                bootstrap.root_reader_gid({'collectorMode': 'user', 'collectorReaderGid': 1000})

    def test_system_root_unit_requires_exact_selected_numeric_group_and_user_contract_stays_closed(self):
        root = ROOT.absolute()
        text = ('[Unit]\nDescription=ASA Lab diagnostic collector\nAfter=network.target\n[Service]\n'
                f'WorkingDirectory={root}\nExecStart={bootstrap.shlex.quote(sys.executable)} '
                f'{bootstrap.shlex.quote(str(root/"tools/collect_logs.py"))} --root {bootstrap.shlex.quote(str(root))}\n'
                'Restart=always\nRestartSec=20\nNoNewPrivileges=true\nUMask=0077\n'
                'User=root\nGroup=1000\n[Install]\nWantedBy=multi-user.target\n')
        bootstrap.collector_unit(text, root, 'fixture', {}, 0, 1000)
        for candidate in (text.replace('Group=1000\n', ''), text.replace('Group=1000', 'Group=0'),
                          text.replace('Group=1000', 'Group=1001'), text.replace('Group=1000', 'Group=readers'),
                          text.replace('User=root', 'User=nobody'), text.replace('Group=1000', 'Group=1000\nExecStartPre=/bin/true')):
            with self.subTest(candidate=candidate), self.assertRaises(bootstrap.Blocked):
                bootstrap.collector_unit(candidate, root, 'fixture', {}, 0, 1000)
        user = text.replace('User=root\nGroup=1000\n', '').replace('multi-user.target', 'default.target')
        bootstrap.collector_unit(user, root, 'fixture', {}, 1000)
        with self.assertRaises(bootstrap.Blocked):
            bootstrap.collector_unit(user.replace('[Install]', 'Group=1000\n[Install]'), root, 'fixture', {}, 1000)

    def test_cli_requires_isolated_entry_before_paths_and_redacts_arguments(self):
        result = subprocess.run([sys.executable, '-B', str(ROOT/'scripts/bootstrap-provider-monitor.py'), 'PRIVATE_ARGUMENT'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)['code'], 'ISOLATED_LINUX_PYTHON_REQUIRED')
        self.assertNotIn('PRIVATE_ARGUMENT', result.stdout+result.stderr)


@unittest.skipUnless(sys.platform == 'linux' and os.environ.get('ASA_PROVIDER_BOOTSTRAP_ROOT_TESTS') == '1', 'Explicit ephemeral root Linux fixture only')
class RootNativeContracts(unittest.TestCase):
    def setUp(self):
        self.assertEqual(os.geteuid(), 0)
        self.tmp = tempfile.TemporaryDirectory(prefix='provider-bootstrap-contract-', dir='/var/lib')
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_full_transfer_deadline_kills_and_reaps_a_stalled_dns_worker(self):
        worker = self.root/'stalled.py'
        pidfile = self.root/'pid'
        worker.write_text('import os,socket,time\nfrom pathlib import Path\n'
                          'Path('+repr(str(pidfile))+').write_text(str(os.getpid()))\n'
                          'socket.getaddrinfo=lambda *args: time.sleep(60)\n'
                          'socket.getaddrinfo("raw.githubusercontent.com",443)\n')
        start = time.monotonic()
        with patch.object(bootstrap, '__file__', str(worker)), patch.object(bootstrap, 'DOWNLOAD_SECONDS', .5):
            with self.assertRaisesRegex(bootstrap.Blocked, '^SOURCE_TRANSFER_TIMEOUT$'):
                bootstrap.download('deployment/paths.py', {'bytes': 1, 'sha256': '0'*64})
        self.assertLess(time.monotonic()-start, 3)
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pidfile.read_text()), 0)

    def test_actual_symlink_hardlink_groupwrite_and_wrong_owner_denied(self):
        target = self.root/'target'
        target.write_bytes(b'x')
        target.chmod(0o600)
        (self.root/'link').symlink_to(target)
        os.link(target, self.root/'hard')
        for path in (self.root/'link', self.root/'hard'):
            with self.assertRaises((OSError, bootstrap.Blocked)):
                bootstrap.read(path, 10)
        (self.root/'hard').unlink()
        target.chmod(0o620)
        with self.assertRaisesRegex(bootstrap.Blocked, 'FILE_TRUST'):
            bootstrap.read(target, 10)
        target.chmod(0o600)
        os.chown(target, 65534, 65534)
        with self.assertRaisesRegex(bootstrap.Blocked, 'FILE_TRUST'):
            bootstrap.read(target, 10)

    def test_create_only_write_never_overwrites_foreign_file_or_symlink(self):
        target = self.root/'business'
        target.write_bytes(b'preserve')
        target.chmod(0o600)
        for path in (target, self.root/'link'):
            if path.name == 'link':
                path.symlink_to(target)
            with self.assertRaises(FileExistsError):
                bootstrap.write_new(path, b'replace', 0o600)
        self.assertEqual(target.read_bytes(), b'preserve')

    def test_actual_parent_write_delegation_rejected(self):
        child = self.root/'delegated'
        child.mkdir(mode=0o775)
        child.chmod(0o775)
        with self.assertRaisesRegex(bootstrap.Blocked, 'DIRECTORY_TRUST'):
            with bootstrap.directory(child):
                pass


if __name__ == '__main__':
    unittest.main()
