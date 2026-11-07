"""Review gates for VM100's narrow enrollment and reversible gateway switch.

Real root-owned fixture files are used; systemctl and the private live backend
are replaced at the boundary. No test contacts or changes a live service.
"""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.parse

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO/'scripts'))


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, REPO/'scripts'/filename)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


install = load('school_host_admin_install', 'install-school-host-admin.py')
legacy = load('school_legacy_acceptance', 'school-mcp-oauth-e2e-acceptance.py')


class PlanAndProtocol(unittest.TestCase):
    def test_plan_on_other_host_has_no_side_effects_or_secret_reads(self):
        with patch.object(sys, 'argv', ['install-school-host-admin.py']), \
                patch.object(install.socket, 'gethostname', return_value='offline-review'), \
                patch.object(install, 'run', side_effect=AssertionError('no subprocess')), \
                patch.object(install, 'environment', side_effect=AssertionError('no secrets')), \
                patch.object(install, 'create', side_effect=AssertionError('no writes')), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(install.main(), 0)
        value = json.loads(out.getvalue())
        self.assertEqual(value['mode'], 'plan')
        self.assertEqual(value['live_preflight'], 'not_run_on_this_host')
        self.assertFalse(value['public_ports_added'])

    def test_apply_requires_exact_root_host_and_explicit_admin_authorization(self):
        for args, uid, host, code in [
                (['--apply'], 0, install.HOST, 'EXACT_ROOT_HOST_REQUIRED'),
                (['--apply', '--expected-host', install.HOST], 1000, install.HOST, 'EXACT_ROOT_HOST_REQUIRED'),
                (['--apply', '--expected-host', install.HOST], 0, 'another-host', 'EXACT_ROOT_HOST_REQUIRED'),
                (['--apply', '--expected-host', install.HOST], 0, install.HOST, 'EXPLICIT_FULL_ADMIN_REQUIRED')]:
            with self.subTest(code=code, uid=uid, host=host), \
                    patch.object(sys, 'argv', ['install-school-host-admin.py']+args), \
                    patch.object(install.os, 'geteuid', return_value=uid), \
                    patch.object(install.socket, 'gethostname', return_value=host), \
                    patch.object(install, 'enroll', side_effect=AssertionError('must not enroll')), \
                    contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(install.main(), 2)
            self.assertEqual(json.loads(err.getvalue())['code'], code)

    def test_guard_error_is_safe_json_without_traceback(self):
        with patch.object(sys, 'argv', ['install-school-host-admin.py', '--repair-gateway-route']), \
                patch.object(install.os, 'geteuid', return_value=0), \
                patch.object(install.socket, 'gethostname', return_value=install.HOST), \
                patch.object(install, 'trusted_metadata', side_effect=install.OpsError('HOST_PATH_TRUST', 'private-detail')), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(install.main(), 2)
        self.assertEqual(json.loads(err.getvalue()), {'status': 'blocked', 'code': 'HOST_PATH_TRUST'})

    def test_initial_gateway_installer_uses_base_target(self):
        source = (REPO/'scripts/install-school-mcp-oauth-gateway.py').read_text()
        targets = [line for line in source.splitlines() if line.startswith('ExecStart=')]
        self.assertEqual(len(targets), 1)
        self.assertTrue(targets[0].endswith('-- http://127.0.0.1:39443'))
        self.assertNotIn('39443/mcp', targets[0])

    def test_legacy_consent_accepts_302_303_with_exact_callback_state_and_issuer(self):
        target = legacy.CALLBACK+'?'+urllib.parse.urlencode({'code': 'one-code', 'state': 'bound-state', 'iss': legacy.ROOT+'/'})
        for status in (302, 303):
            self.assertEqual(legacy.callback_code(status, {'Location': target}, 'bound-state'), 'one-code')
        for status, url in [(307, target), (303, target.replace('61987', '61988')),
                            (303, target.replace('/asa-mcp-oauth-proof', '/asa-mcp-oauth-proof-extra')),
                            (303, target+'&code=second'), (303, target+'&state=second'),
                            (303, target.replace('bound-state', 'wrong-state')),
                            (303, target+'#fragment'), (303, target.replace('x.asa-lab.ru', 'evil.example'))]:
            with self.subTest(status=status, url=url), self.assertRaises(legacy.ProofError):
                legacy.callback_code(status, {'Location': url}, 'bound-state')


@unittest.skipUnless(os.geteuid() == 0, 'Root-owned trusted-path fixtures require root')
class GatewayTransactions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='asa-gateway-unit-', dir='/var/lib')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.paths = {
            'GATEWAY_UNIT': self.root/'gateway.service',
            'GATEWAY_ENV': self.root/'gateway.env',
            'ENV': self.root/'operations.env',
            'DROPIN': self.root/'gateway.service.d/20-host-admin.conf',
            'ROLLBACK': self.root/'rollback',
            'ROUTE_BACKUP': self.root/'route.before.service',
        }
        self.patch_paths = patch.multiple(install, **self.paths)
        self.patch_paths.start()
        self.addCleanup(self.patch_paths.stop)
        self.before_env = ('# Keep issuer and persistent OAuth storage\n'
            'EXTERNAL_URL=https://x.asa-lab.ru\n'
            'DATA_PATH=/var/lib/asa-school-mcp-gateway\n'
            'PASSWORD=fixture-owner-password-never-output\n'
            'PROXY_BEARER_TOKEN=fixture-original-bearer-token\n'
            'EXTRA_FLAG=preserve-me\n')
        install.create(install.GATEWAY_ENV, self.before_env)
        install.create(install.ENV, 'ASA_OPS_TOKEN=fixture-new-backend-bearer\n')
        self.unit('http://127.0.0.1:39443')
        self.calls = []
        self.fail_restarts = 0
        self.run_patch = patch.object(install, 'run', side_effect=self.systemctl)
        self.run_patch.start()
        self.addCleanup(self.run_patch.stop)

    def unit(self, target):
        text = ('[Unit]\nAfter=network-online.target server-settings-1panel-mcp.service\n'
                'Requires=server-settings-1panel-mcp.service\n[Service]\n'
                'EnvironmentFile=/etc/server-settings/asa-school-mcp-gateway.env\n'
                'ExecStart=/opt/server-settings/bin/asa-mcp-auth-proxy --listen 127.0.0.1:39450 '
                '--no-auto-tls --external-url https://x.asa-lab.ru -- '+target+'\n')
        install.GATEWAY_UNIT.write_text(text)
        install.GATEWAY_UNIT.chmod(0o644)
        return text

    def systemctl(self, args, **kwargs):
        self.calls.append(args)
        self.assertEqual(args[0], 'systemctl')
        if args[1] == 'show':
            source = install.DROPIN.read_text() if install.DROPIN.exists() else install.GATEWAY_UNIT.read_text()
            return subprocess.CompletedProcess(args, 0, stdout=source, stderr='')
        if args[1] == 'restart' and self.fail_restarts:
            self.fail_restarts -= 1
            raise subprocess.CalledProcessError(1, args, output='private-unprinted-output')
        return subprocess.CompletedProcess(args, 0, stdout='', stderr='')

    def test_baseline_refuses_old_duplicated_path_before_any_mutation(self):
        self.unit('http://127.0.0.1:39443/mcp')
        with self.assertRaisesRegex(install.Blocked, 'CORRECTED_11_TOOL_GATEWAY_REQUIRED'):
            install.baseline_gateway()
        self.assertFalse(install.ROLLBACK.exists())
        self.assertFalse(install.DROPIN.exists())

    def test_route_repair_is_narrow_private_and_idempotent(self):
        original = self.unit('http://127.0.0.1:39443/mcp')
        result = install.repair_gateway_route()
        self.assertEqual(result['gateway_backend'], 'http://127.0.0.1:39443')
        self.assertFalse(result['oauth_environment_changed'])
        self.assertFalse(result['public_authenticated_acceptance'])
        self.assertEqual(install.GATEWAY_ENV.read_text(), self.before_env)
        self.assertEqual(install.ROUTE_BACKUP.read_text(), original)
        self.assertEqual(stat.S_IMODE(install.ROUTE_BACKUP.stat().st_mode), 0o600)
        self.assertEqual(install.GATEWAY_UNIT.read_text(), original.replace('39443/mcp\n', '39443\n'))
        self.assertEqual(install.repair_gateway_route()['gateway_route'], 'already_correct')
        self.assertEqual(len([c for c in self.calls if c[1] == 'restart']), 1)

    def test_route_repair_refuses_unknown_suffix_and_effective_dropin(self):
        self.unit('http://127.0.0.1:39443/mcp/unreviewed')
        with self.assertRaisesRegex(install.Blocked, 'GATEWAY_ROUTE_DRIFT'):
            install.repair_gateway_route()
        self.unit('http://127.0.0.1:39443/mcp')
        install.DROPIN.parent.mkdir()
        install.create(install.DROPIN, '[Service]\nExecStart=/bin/gateway -- http://127.0.0.1:39446\n', mode=0o644)
        with self.assertRaisesRegex(install.Blocked, 'GATEWAY_EFFECTIVE_ROUTE_DRIFT'):
            install.repair_gateway_route()
        self.assertFalse(install.ROUTE_BACKUP.exists())

    def test_failed_route_restart_restores_exact_unit_and_preserves_credentials(self):
        original = self.unit('http://127.0.0.1:39443/mcp')
        self.fail_restarts = 1
        with self.assertRaises(subprocess.CalledProcessError):
            install.repair_gateway_route()
        self.assertEqual(install.GATEWAY_UNIT.read_text(), original)
        self.assertEqual(install.GATEWAY_ENV.read_text(), self.before_env)
        self.assertEqual(len([c for c in self.calls if c[1] == 'restart']), 2)

    def switch(self):
        with patch.object(install, 'verify_private', return_value=({}, 'not-printed')) as verify:
            result = install.switch_gateway()
        verify.assert_called_once_with()
        return result

    def test_switch_changes_only_private_token_and_dependency_then_rolls_back_to_working_11(self):
        original_unit = install.GATEWAY_UNIT.read_text()
        result = self.switch()
        self.assertFalse(result['public_authenticated_acceptance'])
        changed = self.before_env.replace('fixture-original-bearer-token', 'fixture-new-backend-bearer')
        self.assertEqual(install.GATEWAY_ENV.read_text(), changed)
        self.assertEqual(install.GATEWAY_UNIT.read_text(), original_unit)
        self.assertIn('Requires=\nRequires=asa-operations.service\n', install.DROPIN.read_text())
        self.assertIn('After=\nAfter=network-online.target asa-operations.service\n', install.DROPIN.read_text())
        self.assertTrue(install.DROPIN.read_text().rstrip().endswith('-- http://127.0.0.1:39446'))
        self.assertEqual(stat.S_IMODE(install.ROLLBACK.stat().st_mode), 0o700)
        for entry in install.ROLLBACK.iterdir():
            self.assertEqual(stat.S_IMODE(entry.stat().st_mode), 0o600)
        result = install.rollback_gateway()
        self.assertEqual(result['gateway_backend'], 'http://127.0.0.1:39443')
        self.assertEqual(install.GATEWAY_ENV.read_text(), self.before_env)
        self.assertFalse(install.DROPIN.exists())
        self.assertTrue(install.ROLLBACK.exists())

    def test_failed_switch_restart_automatically_restores_working_11(self):
        self.fail_restarts = 1
        with self.assertRaises(subprocess.CalledProcessError):
            self.switch()
        self.assertEqual(install.GATEWAY_ENV.read_text(), self.before_env)
        self.assertFalse(install.DROPIN.exists())
        install.baseline_gateway()

    def test_rollback_refuses_concurrent_owner_credential_change(self):
        self.switch()
        modified = install.GATEWAY_ENV.read_text().replace('fixture-owner-password-never-output', 'new-owner-password')
        install.replace(install.GATEWAY_ENV, modified)
        with self.assertRaisesRegex(install.Blocked, 'GATEWAY_ENV_CHANGED'):
            install.rollback_gateway()
        self.assertEqual(install.GATEWAY_ENV.read_text(), modified)
        self.assertTrue(install.DROPIN.exists())

    def test_rollback_refuses_concurrent_owner_service_change(self):
        self.switch()
        original = install.DROPIN.read_text()
        install.replace(install.DROPIN, original+'Restart=always\n', mode=0o644)
        with self.assertRaisesRegex(install.Blocked, 'GATEWAY_DROPIN_CHANGED'):
            install.rollback_gateway()
        self.assertTrue(install.DROPIN.exists())

    def test_environment_does_not_accept_public_mode_or_duplicate_keys(self):
        install.GATEWAY_ENV.chmod(0o644)
        with self.assertRaisesRegex(install.Blocked, 'ENV_NOT_ROOT_ONLY'):
            install.environment(install.GATEWAY_ENV)
        install.GATEWAY_ENV.chmod(0o600)
        install.replace(install.GATEWAY_ENV, self.before_env+'PASSWORD=duplicate-private-value\n')
        with self.assertRaisesRegex(install.Blocked, 'ENV_FORMAT'):
            install.environment(install.GATEWAY_ENV)


@unittest.skipUnless(os.geteuid() == 0, 'Root-owned trusted-path fixtures require root')
class SourceBinding(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='asa-source-unit-', dir='/var/lib')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source_patch = patch.object(install, 'SOURCE', self.root)
        self.source_patch.start()
        self.addCleanup(self.source_patch.stop)
        self.sha = '7'*40
        files = ['scripts/asa_ops/host_profile.py', 'scripts/install-school-host-admin.py',
                 'scripts/install-asa-operations.sh', 'scripts/school-mcp-host-admin-acceptance.py',
                 'templates/asa-operations.service', 'templates/asa-admin-executor.socket',
                 'templates/asa-admin-executor@.service']
        self.manifest = {'format': 'asa-school-host-admin-source-v1', 'source_sha': self.sha, 'files': {}}
        for name in files:
            path = self.root/name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('reviewed fixture source\n')
            self.manifest['files'][name] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.write_manifest()

    def write_manifest(self):
        path = self.root/'source-manifest.json'
        path.write_text(json.dumps(self.manifest))
        path.chmod(0o644)

    def test_trusted_bundle_hashes_and_declared_sha_are_bound(self):
        self.assertEqual(install.verify_source(self.sha), 'trusted_bundle_manifest_and_file_hashes')
        with self.assertRaisesRegex(install.Blocked, 'SOURCE_MANIFEST'):
            install.verify_source('8'*40)
        (self.root/'scripts/asa_ops/host_profile.py').write_text('modified source\n')
        with self.assertRaisesRegex(install.Blocked, 'SOURCE_FILE_MISMATCH'):
            install.verify_source(self.sha)

    def test_manifest_rejects_missing_required_entry_and_traversal(self):
        removed = self.manifest['files'].pop('templates/asa-operations.service')
        self.write_manifest()
        with self.assertRaisesRegex(install.Blocked, 'SOURCE_MANIFEST_INCOMPLETE'):
            install.verify_source(self.sha)
        self.manifest['files']['templates/asa-operations.service'] = removed
        self.manifest['files']['../untrusted-file'] = 'f'*64
        self.write_manifest()
        with self.assertRaisesRegex(install.Blocked, 'SOURCE_MANIFEST_ENTRY'):
            install.verify_source(self.sha)

    def test_git_checkout_requires_exact_head_and_clean_source(self):
        (self.root/'.git').mkdir()
        def invoke(args, **kwargs):
            return subprocess.CompletedProcess(args, 0, stdout=self.sha+'\n' if 'rev-parse' in args else '', stderr='')
        with patch.object(install, 'run', side_effect=invoke):
            self.assertEqual(install.verify_source(self.sha), 'git_head_and_clean_source')
            with self.assertRaisesRegex(install.Blocked, 'SOURCE_HEAD_MISMATCH'):
                install.verify_source('8'*40)
        with patch.object(install, 'run', side_effect=[subprocess.CompletedProcess([], 0, stdout=self.sha+'\n'),
                subprocess.CompletedProcess([], 0, stdout=' M scripts/asa_ops/server.py\n')]):
            with self.assertRaisesRegex(install.Blocked, 'SOURCE_DIRTY'):
                install.verify_source(self.sha)


if __name__ == '__main__':
    unittest.main()
