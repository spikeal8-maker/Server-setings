import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest

SPEC = importlib.util.spec_from_file_location('school_ingress_capacity', Path(__file__).resolve().parents[1] / 'scripts/school-ingress-capacity.py')
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


def fixture():
    return {'http': {'middlewares': {
        'shared-rate': {'rateLimit': {'average': 600, 'period': '1m', 'burst': 300}},
        'auth': {'forwardAuth': {'address': 'http://authentication.internal'}},
    }, 'routers': {
        'school': {'rule': 'Host(`school.example`)', 'tls': {}, 'entryPoints': ['https'], 'middlewares': ['auth', 'shared-rate'], 'service': 'school'},
        'test2': {'rule': 'Host(`test2.example`)', 'tls': {}, 'middlewares': ['shared-rate'], 'service': 'school'},
        'other': {'rule': 'Host(`other.example`)', 'tls': {}, 'middlewares': ['auth'], 'service': 'other'},
    }, 'services': {'school': {'loadBalancer': {'servers': [{'url': 'http://frps:8080'}]}}, 'other': {'loadBalancer': {'servers': [{'url': 'http://other:80'}]}}}}}


class IngressCapacityTests(unittest.TestCase):
    def render(self, data):
        return gate.render(json.dumps(data).encode(), 'school.example', 'school', 'shared-rate')

    def test_only_named_tls_host_changes_and_shared_predecessor_is_preserved(self):
        original = fixture()
        raw, plan = self.render(original)
        actual = gate.parse(raw)
        expected = copy.deepcopy(original)
        expected['http']['middlewares'][plan['middleware']] = plan['profile']
        expected['http']['routers']['school']['middlewares'][1] = plan['middleware']
        self.assertEqual(actual, expected)
        self.assertEqual(actual['http']['routers']['test2'], original['http']['routers']['test2'])
        self.assertEqual(actual['http']['routers']['school']['middlewares'][0], 'auth')

    def test_existing_source_criterion_is_preserved(self):
        data = fixture()
        data['http']['middlewares']['shared-rate']['rateLimit']['sourceCriterion'] = {'ipStrategy': {'depth': 1}}
        raw, plan = self.render(data)
        self.assertEqual(gate.parse(raw)['http']['middlewares'][plan['middleware']]['rateLimit']['sourceCriterion'], {'ipStrategy': {'depth': 1}})

    def test_repeat_is_noop(self):
        raw, _ = self.render(fixture())
        repeated, plan = gate.render(raw, 'school.example', 'school', 'shared-rate')
        self.assertFalse(plan['changed'])
        self.assertEqual(raw, repeated)

    def test_refuses_nonexact_host_nontls_or_auth_replacement(self):
        for change in ['wrong-host', 'non-tls', 'tls-false', 'auth']:
            data = fixture()
            if change == 'wrong-host': data['http']['routers']['school']['rule'] = 'HostRegexp(`.*`)'
            elif change == 'non-tls': del data['http']['routers']['school']['tls']
            elif change == 'tls-false': data['http']['routers']['school']['tls'] = False
            else: data['http']['middlewares']['shared-rate'] = {'forwardAuth': {'address': 'http://auth'}}
            with self.subTest(change=change), self.assertRaises(gate.Blocked): self.render(data)

    def test_duplicate_yaml_alias_and_invalid_yaml_fail_without_content_leak(self):
        for raw in [b'a: 1\na: 2\n', b'a: &a [1]\nb: *a', b'key: [private-value']:
            with self.assertRaises(gate.Blocked) as error: gate.parse(raw)
            self.assertNotIn('private-value', str(error.exception))

    def test_apply_requires_digest_and_roundtrip_rollback_preserves_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            provider = base / 'dynamic'
            provider.mkdir()
            source = provider / 'school.yaml'
            before = json.dumps(fixture()).encode()
            source.write_bytes(before)
            args = ['--route-file', str(source), '--host', 'school.example', '--router', 'school', '--old-middleware', 'shared-rate']
            with contextlib.redirect_stdout(io.StringIO()): gate.main(args)
            self.assertEqual(source.read_bytes(), before)
            with self.assertRaises(gate.Blocked): gate.main(args + ['--apply'])
            backups = base / 'backups'
            output = io.StringIO()
            with contextlib.redirect_stdout(output): gate.main(args + ['--apply', '--expected-sha256', gate.digest(before), '--backup-directory', str(backups)])
            applied = json.loads(output.getvalue())
            self.assertNotEqual(source.read_bytes(), before)
            receipt = applied['receipt']
            intended = source.read_bytes()
            source.write_bytes(intended + b' ')
            with self.assertRaises(gate.Blocked): gate.main(args + ['--restore', receipt])
            self.assertEqual(source.read_bytes(), intended + b' ')
            source.write_bytes(intended)
            with contextlib.redirect_stdout(io.StringIO()): gate.main(args + ['--restore', receipt, '--apply', '--expected-sha256', applied['afterSha256'], '--backup-directory', str(backups)])
            self.assertEqual(source.read_bytes(), before)

    def test_drift_and_backup_inside_watch_directory_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'school.yaml'
            before = json.dumps(fixture()).encode()
            source.write_bytes(before)
            args = ['--route-file', str(source), '--host', 'school.example', '--router', 'school', '--old-middleware', 'shared-rate', '--apply']
            with self.assertRaises(gate.Blocked): gate.main(args + ['--expected-sha256', '0'*64, '--backup-directory', str(Path(tmp)/'backup')])
            with self.assertRaises(gate.Blocked): gate.main(args + ['--expected-sha256', gate.digest(before), '--backup-directory', str(Path(tmp)/'backup')])
            self.assertEqual(source.read_bytes(), before)

    @unittest.skipIf(os.name == 'nt', 'POSIX owner/mode enforcement')
    def test_public_rollback_receipt_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            provider = base / 'dynamic'
            provider.mkdir()
            source = provider / 'school.yaml'
            before = json.dumps(fixture()).encode()
            source.write_bytes(before)
            args = ['--route-file', str(source), '--host', 'school.example', '--router', 'school', '--old-middleware', 'shared-rate']
            output = io.StringIO()
            with contextlib.redirect_stdout(output): gate.main(args + ['--apply', '--expected-sha256', gate.digest(before), '--backup-directory', str(base/'backups')])
            receipt = Path(json.loads(output.getvalue())['receipt'])
            applied = source.read_bytes()
            receipt.chmod(0o644)
            with self.assertRaises(gate.Blocked): gate.main(args + ['--restore', str(receipt)])
            self.assertEqual(source.read_bytes(), applied)


if __name__ == '__main__':
    unittest.main()
