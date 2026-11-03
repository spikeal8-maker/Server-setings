"""Offline source contracts; root-native fixtures opt in only on ephemeral Linux CI."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
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
                        bootstrap.download('deployment/paths.py', pin)
                else:
                    self.assertEqual(bootstrap.download('deployment/paths.py', pin), b'abc')
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
