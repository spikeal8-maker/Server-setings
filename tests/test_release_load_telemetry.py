"""Offline protected runtime sampling; no credentials, school I/O or application edits."""
import copy
from datetime import datetime, timedelta, timezone
import io
import json
import types
import unittest
from unittest import mock
import urllib.error

from test_release_load_100 import gate, contracts, fixture, completed_receipt


def admin_fixture():
    f = fixture(20)
    f['roles'] = {'admin': {'accountId': '00000000-0000-0000-0000-000000000999', 'token': 'a'*64}}
    return f


def operations():
    return {'checkedAt': datetime.now(timezone.utc).isoformat(),
            'build': {'revision': 'a'*40, 'synchronized': True}, 'migration': {'version': '286'},
            'services': {'api': 'responding', 'database': 'responding'},
            'runtime': {'memory': {'rssMb': 100}, 'database': {'waiting': 0}, 'requests': {'total': 2}},
            'counts': {'private': 'private-body-canary'}, 'accounts': ['private-body-canary']}


class PrivateTelemetry(unittest.TestCase):
    def runner(self):
        runner = gate.Runner(admin_fixture(), 'a'*40, 20, schema=286)
        runner.admin_identity_verified = True
        return runner

    def test_private_source_works_when_public_404_and_uses_only_admin_cookie(self):
        runner = self.runner()
        calls = []
        def respond(req, timeout):
            calls.append(req)
            if req.full_url.endswith('/health/metrics'):
                raise urllib.error.HTTPError(req.full_url, 404, 'no public metrics', {}, io.BytesIO())
            return mock.MagicMock(__enter__=lambda self: types.SimpleNamespace(
                status=200, read=lambda bound: json.dumps(operations()).encode()), __exit__=lambda *args: None)
        with mock.patch.object(runner.opener, 'open', side_effect=respond):
            result = runner.runtime_telemetry()
        self.assertEqual([req.full_url for req in calls], ['https://school.example/api/admin/v1/operations/status'])
        self.assertEqual(calls[0].get_header('Cookie'), 'asa_session='+'a'*64)
        self.assertEqual(result['memory.rssMb'], 100)
        self.assertNotIn('private-body-canary', json.dumps([result, runner.samples, runner.report()]))
        self.assertNotIn('a'*64, json.dumps([result, runner.samples, runner.report()]))

    def test_privileged_sources_never_fallback_on_401_403_404(self):
        for status in (401, 403, 404):
            runner = self.runner()
            def denied(req, timeout):
                raise urllib.error.HTTPError(req.full_url, status, 'private-body-canary', {},
                                             io.BytesIO(b'{"private":"private-body-canary"}'))
            with self.subTest(status=status), mock.patch.object(runner.opener, 'open', side_effect=denied) as opened:
                with self.assertRaises(gate.CheckFailed): runner.runtime_telemetry()
                self.assertEqual(opened.call_count, 1)
                self.assertTrue(runner.halt_new.is_set())
                self.assertNotIn('private-body-canary', json.dumps(runner.report()))

    def test_missing_identity_stops_before_any_network(self):
        runner = self.runner(); runner.admin_identity_verified = False
        with mock.patch.object(runner, 'call', side_effect=AssertionError('network')):
            with self.assertRaises(contracts.ContractError): runner.runtime_telemetry()

    def test_wrong_account_or_nonplatform_admin_never_starts_work(self):
        for change in ('account', 'permission'):
            runner = self.runner(); runner.admin_identity_verified = False
            calls = []
            def call(user, path, **kwargs):
                calls.append(path)
                if path == '/health/ready':
                    return {'status': 'ready', 'deployment': {'revision': 'a'*40, 'schemaVersion': 286, 'synchronized': True}}
                if path == '/api/class-join/me':
                    return {'authenticated': True, 'student': {'seatId': runner.f['seats'][user]['seat']}, 'classroom': {'id': 'qa'}}
                if path == '/api/auth/me':
                    return {'authenticated': True, 'account': {'id': runner.f['roles']['admin']['accountId'] if change == 'permission' else 'foreign'}, 'capabilities': []}
                return {'administrator': True, 'scopes': [{'kind': 'organization', 'role': 'organization_admin'}]}
            with self.subTest(change=change), mock.patch.object(runner, 'call', side_effect=call):
                with self.assertRaises(contracts.ContractError): runner.run()
                self.assertFalse(runner.admin_identity_verified)
                self.assertNotIn('/api/projects', calls)
                self.assertNotIn('/api/admin/v1/operations/status', calls)

    def test_verified_core_admin_and_mixed_identity_checks_are_preserved(self):
        runner = self.runner(); runner.admin_identity_verified = False
        def call(user, path, **kwargs):
            if path == '/health/ready':
                return {'status': 'ready', 'deployment': {'revision': 'a'*40, 'schemaVersion': 286, 'synchronized': True}}
            if path == '/api/class-join/me':
                return {'authenticated': True, 'student': {'seatId': runner.f['seats'][user]['seat']}, 'classroom': {'id': 'qa'}}
            if path == '/api/auth/me':
                return {'authenticated': True, 'account': {'id': runner.f['roles'][user]['accountId']}, 'capabilities': [{'capability': 'educator', 'state': 'verified'}]}
            return {'administrator': True, 'scopes': [{'kind': 'platform', 'role': 'platform_admin'}]}
        for profile in ('core', 'mixed'):
            runner.profile = profile
            runner.f['roles']['teacher'] = {'accountId': 'separate-teacher', 'token': 'b'*64}
            runner.f['resources'] = {'classroomId': 'qa'}
            runner.actual_seats = []
            with self.subTest(profile=profile), mock.patch.object(runner, 'call', side_effect=call) as called:
                runner.identities()
                self.assertTrue(runner.admin_identity_verified)
                called.assert_any_call('admin', '/api/auth/me', label='identity-admin')
                called.assert_any_call('admin', '/api/admin/v1/me', label='identity-admin-scope')
                if profile == 'mixed': called.assert_any_call('teacher', '/api/auth/me', label='identity-teacher')

    def test_null_wrong_shape_stale_and_wrong_deployment_fail_closed(self):
        variants = [None, {}, {'runtime': None}]
        for path, value in [('runtime', None), ('runtime', {'memory': {'rssMb': True}}),
                            ('checkedAt', 'not-a-time'), ('checkedAt', datetime.now().isoformat()),
                            ('checkedAt', (datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat()),
                            ('checkedAt', (datetime.now(timezone.utc)+timedelta(minutes=1)).isoformat()),
                            ('build', {'revision': 'b'*40, 'synchronized': True}),
                            ('build', {'revision': 'a'*40, 'synchronized': False}),
                            ('migration', {'version': '287'})]:
            row = operations(); row[path] = value; variants.append(row)
        for row in variants:
            runner = self.runner()
            with self.subTest(row=row), mock.patch.object(runner, 'call', return_value=row) as call:
                with self.assertRaises(contracts.ContractError): runner.runtime_telemetry()
                call.assert_called_once_with('admin', '/api/admin/v1/operations/status', label='runtime-metrics')

    def test_contract_failure_in_health_halts_and_never_records_body(self):
        runner = self.runner()
        def call(user, path, **kwargs):
            if path == '/health/ready': return {}
            return dict(operations(), runtime=None)
        with mock.patch.object(runner, 'call', side_effect=call), mock.patch.object(runner.stop, 'wait', side_effect=lambda _: runner.stop.set()):
            runner.health()
        self.assertTrue(runner.halt_new.is_set())
        self.assertEqual(runner.telemetry, [])
        self.assertEqual(runner.samples[-1]['error'], 'telemetry_incomplete')
        self.assertNotIn('private-body-canary', json.dumps(runner.report()))

    def test_legacy_public_is_core_without_admin_only(self):
        runner = gate.Runner(fixture(20), 'a'*40, 20, schema=286)
        with mock.patch.object(runner, 'call', return_value=operations()['runtime']) as call:
            self.assertEqual(runner.runtime_telemetry()['requests.total'], 2)
            call.assert_called_once_with(None, '/health/metrics', label='runtime-metrics')
        self.assertEqual(contracts.telemetry_source(fixture(), 'mixed')['kind'], 'admin-operations-runtime')
        self.assertEqual(contracts.telemetry_source(admin_fixture(), 'core')['kind'], 'admin-operations-runtime')

    def test_progression_rejects_changed_or_missing_source_and_stale_sample(self):
        report, binding, workload = completed_receipt()
        for change in ('source', 'missing', 'old', 'future', 'binding', 'actor'):
            bad = copy.deepcopy(report); bound = copy.deepcopy(binding)
            if change == 'source': bad['telemetrySource'] = contracts.telemetry_source({}, 'mixed')
            if change == 'missing': bad.pop('telemetrySource')
            if change == 'old': bad['telemetry'][0]['sampledAt'] = bad['runStartedAt']-1
            if change == 'future': bad['telemetry'][0]['sampledAt'] = bad['runFinishedAt']+1
            if change == 'binding': bound['telemetrySource'] = contracts.telemetry_source({}, 'mixed')
            if change == 'actor': next(r for r in bad['requests'] if r['label'] == 'runtime-metrics')['actor'] = 'admin'
            with self.subTest(change=change), self.assertRaises(contracts.ContractError):
                contracts.previous_step(bad, bound, workload, 50, gate.time.time())


if __name__ == '__main__':
    unittest.main()
