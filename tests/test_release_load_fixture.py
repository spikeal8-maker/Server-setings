"""Offline adapter proofs only. No local Docker, HTTP, database or real users."""
import contextlib
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import release_load_fixture as fixture
from release_load_contracts import private_fixture

ORIGIN = 'https://school.example'
REVISION = 'a' * 40


class MemoryStore:
    def __init__(self):
        self.values = []

    def save(self, f):
        fixture.validate(f)
        self.values.append(json.loads(json.dumps(f)))


class FakeAdapter:
    def __init__(self, f, fail_join=None):
        self.f = f
        self.calls = []
        self.bounded = []
        self.fail_join = fail_join
        self.number = 0
        self.classroom = str(uuid.uuid4())
        self.last_seat = None

    def account(self, f, role):
        self.calls.append(('account', role))
        return {k: str(uuid.uuid4()) for k in ('accountId', 'principalId', 'workspaceId', 'tenantId')}

    def api(self, token, path, body=None, **kwargs):
        self.calls.append((token is not None, path, body, kwargs))
        if path == '/api/classrooms':
            return {'classroom': {'id': self.classroom, 'title': body['title'], 'teacherRole': 'owner',
                                  'workspaceKind': 'personal', 'joinCode': 'ABC DEF 234'}}, None
        if path.endswith('/seats'):
            self.number += 1
            self.last_seat = str(uuid.uuid4())
            return {'student': {'id': self.last_seat, 'studentCode': f'Qa{self.number:04}'}}, None
        if path == '/api/class-join/studentseat':
            if self.number == self.fail_join:
                raise RuntimeError('SECRET_SQL_ERROR_SESSION_PRIVATE')
            return {'authenticated': True, 'student': {'seatId': self.last_seat},
                    'classroom': {'id': self.classroom}}, 'T' + str(self.number).zfill(63)
        raise AssertionError('unexpected fake API route')

    def bound(self, f, expire=False):
        self.bounded.append(expire)


class TrackingAdapter(FakeAdapter):
    """Track server-created sessions even when their receipt cannot be saved."""
    def __init__(self, f):
        super().__init__(f)
        self.sessions = {}
        self.fences = []

    def api(self, *args, **kwargs):
        response, token = super().api(*args, **kwargs)
        if token:
            self.sessions[token] = {'hours': 24, 'revoked': False}
        return response, token

    def bound(self, f, expire=False):
        fixture.validate(f)
        self.fences.append(json.loads(json.dumps(f)))
        super().bound(f, expire)
        if expire:
            for session in self.sessions.values():
                session.update(hours=0, revoked=True)
        else:
            for row in f['seats']:
                if row.get('token') in self.sessions:
                    self.sessions[row['token']]['hours'] = 2


class ProvisionerTests(unittest.TestCase):
    def fresh(self, **kwargs):
        return fixture.fresh('QA-offline-proof', REVISION, 289, ORIGIN, ['docker', 'operations:updates'], **kwargs)

    def test_default_plan_never_opens_paths_creates_adapter_or_uses_randomness(self):
        output = io.StringIO()
        with patch.object(fixture, 'PrivateStore', side_effect=AssertionError('private path read')), \
             patch.object(fixture.secrets, 'token_hex', side_effect=AssertionError('random')), \
             patch.object(fixture.subprocess, 'run', side_effect=AssertionError('process')), \
             contextlib.redirect_stdout(output):
            result = fixture.main(['--origin', ORIGIN, '--revision', REVISION, '--schema', '289',
                                   '--label', 'QA-offline-proof', '--fixture', '/not/read.json'],
                                  adapter_factory=lambda *a: self.fail('adapter'))
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue())['state'], 'PLAN_ONLY')

    def test_one_hundred_proper_api_codes_and_logins_make_runner_core_fixture(self):
        f, store = self.fresh(), MemoryStore()
        adapter = FakeAdapter(f)
        fixture.provision(f, store, adapter)
        self.assertEqual(f['state'], 'core_ready')
        self.assertEqual(len(f['seats']), 100)
        self.assertEqual(len({s['seat'] for s in f['seats']}), 100)
        self.assertEqual(len({s['token'] for s in f['seats']}), 100)
        self.assertEqual(sum(c[1] == '/api/class-join/studentseat' for c in adapter.calls if len(c) == 4), 100)
        self.assertEqual(len(adapter.bounded), 101)
        self.assertTrue(all(not b for b in adapter.bounded))
        self.assertLessEqual((datetime.fromisoformat(f['expiresAt']) - datetime.fromisoformat(f['createdAt'])).total_seconds(), 7200)
        with tempfile.TemporaryDirectory() as directory:
            with fixture.PrivateStore(Path(directory) / 'private' / 'fixture.json', create=True) as disk:
                disk.save(f)
                accepted = private_fixture(disk.path, ORIGIN, 100, 'core')
                self.assertEqual(accepted['resources']['classroomId'], adapter.classroom)
                with self.assertRaises(Exception):
                    private_fixture(disk.path, ORIGIN, 100, 'mixed')

    def test_partial_failure_persists_intent_and_expires_even_lost_join_cookie(self):
        f, store = self.fresh(), MemoryStore()
        adapter = FakeAdapter(f, fail_join=3)
        with self.assertRaisesRegex(fixture.FixtureError, '^PROVISION_PARTIAL_RETAINED$'):
            fixture.provision(f, store, adapter)
        self.assertEqual(f['state'], 'partial_requires_expiry')
        self.assertEqual(f['pending'], 'join_seat_3')
        self.assertEqual(f['failureExpiry'], 'OBSERVED_SESSIONS_EXPIRED_INFLIGHT_REQUEST_UNKNOWN')
        self.assertEqual(len(f['seats']), 3)
        self.assertNotIn('token', f['seats'][2])
        self.assertTrue(adapter.bounded[-1])
        self.assertTrue(any(s['pending'] == 'bootstrap_teacher' for s in store.values))
        with self.assertRaisesRegex(fixture.FixtureError, 'NO_AUTOMATIC_REPLAY'):
            fixture.provision(f, store, adapter)

    def test_partial_unknown_expiry_remains_explicit_not_fake_success(self):
        f, store = self.fresh(), MemoryStore()
        adapter = FakeAdapter(f, fail_join=1)
        adapter.bound = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('private'))
        with self.assertRaises(fixture.FixtureError):
            fixture.provision(f, store, adapter)
        self.assertEqual(f['failureExpiry'], 'UNKNOWN_OPERATOR_MUST_EXPIRE')

    def test_malformed_seat_code_never_poisoned_manifest_or_skips_own_expiry(self):
        for invalid_code in ('Z' * 32, None, 123456):
            with self.subTest(code_type=type(invalid_code).__name__):
                f, store = self.fresh(), MemoryStore()
                adapter = TrackingAdapter(f)
                original = adapter.api

                def bad_code(*args, **kwargs):
                    response, token = original(*args, **kwargs)
                    if args[1].endswith('/seats'):
                        response['student']['studentCode'] = invalid_code
                    return response, token

                adapter.api = bad_code
                output = io.StringIO()
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                    with self.assertRaisesRegex(fixture.FixtureError, '^PROVISION_PARTIAL_RETAINED$'):
                        fixture.provision(f, store, adapter)
                self.assertEqual(adapter.bounded, [True])
                self.assertEqual(adapter.number, 1)
                self.assertEqual(f['seats'], [])
                self.assertEqual(f['pending'], 'create_seat_1')
                self.assertEqual(f['failureExpiry'], 'OBSERVED_SESSIONS_EXPIRED_INFLIGHT_REQUEST_UNKNOWN')
                self.assertEqual(fixture.validate(adapter.fences[0]), adapter.fences[0])
                self.assertTrue(all(value['seats'] == [] for value in store.values))
                self.assertEqual(output.getvalue(), '')

    def test_permanent_save_failure_after_third_join_still_expires_native_session(self):
        class FailingStore(MemoryStore):
            broken = False

            def save(self, f):
                if len(f['seats']) == 3 and 'token' in f['seats'][2]:
                    self.broken = True
                if self.broken:
                    raise OSError('PRIVATE_DISK_ERROR_TOKEN_SENTINEL')
                super().save(f)

        f, store = self.fresh(), FailingStore()
        adapter = TrackingAdapter(f)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            with self.assertRaisesRegex(fixture.FixtureError, '^PROVISION_PARTIAL_RECEIPT_FAILED$'):
                fixture.provision(f, store, adapter)
        self.assertEqual(adapter.number, 3)
        self.assertEqual(adapter.bounded, [False, False, True])
        self.assertEqual(len(adapter.sessions), 3)
        self.assertTrue(all(s == {'hours': 0, 'revoked': True} for s in adapter.sessions.values()))
        self.assertEqual(len(adapter.fences[-1]['seats']), 3)
        self.assertIn('token', adapter.fences[-1]['seats'][2])
        last_durable = store.values[-1]
        self.assertEqual(last_durable['pending'], 'join_seat_3')
        self.assertNotIn('token', last_durable['seats'][2])
        fixture.validate(last_durable)
        self.assertEqual(f['failureReceipt'], 'WRITE_FAILED_LAST_VALID_INTENT_RETAINED')
        self.assertEqual(f['failureExpiry'], 'OBSERVED_SESSIONS_EXPIRED_INFLIGHT_REQUEST_UNKNOWN')
        self.assertEqual(output.getvalue(), '')
        with self.assertRaisesRegex(fixture.FixtureError, 'NO_AUTOMATIC_REPLAY'):
            fixture.provision(f, store, adapter)

    def test_malformed_sql_bootstrap_receipt_still_executes_actual_nonce_fence(self):
        for malformed in ('{"accountId":PRIVATE_SQL_ROW_SECRET', '{"accountId":"not-a-uuid"}'):
            with self.subTest(receipt=malformed[:12]):
                f, store = self.fresh(), MemoryStore()
                adapter = fixture.HostAdapter.__new__(fixture.HostAdapter)
                adapter.container, adapter.user, adapter.database = 'fake-id', 'qa_admin', 'qa_database'
                output = io.StringIO()
                with patch.object(fixture.subprocess, 'run', side_effect=[
                    SimpleNamespace(returncode=0, stdout=malformed, stderr='PRIVATE_SQL_ROW_SECRET'),
                    SimpleNamespace(returncode=0, stdout='{"bounded":true}', stderr='')
                ]) as run, contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                    with self.assertRaisesRegex(fixture.FixtureError, '^PROVISION_PARTIAL_RETAINED$'):
                        fixture.provision(f, store, adapter)
                self.assertEqual(run.call_count, 2)
                bootstrap, cleanup = [call.kwargs['input'] for call in run.call_args_list]
                self.assertIn('public.auth_register_account(', bootstrap)
                self.assertIn("SET state='revoked'", cleanup)
                self.assertIn("member_role='owner'", cleanup)
                self.assertIn(f['roles']['teacher']['email'], cleanup)
                self.assertNotIn('accountId', f['roles']['teacher'])
                self.assertEqual(f['pending'], 'bootstrap_teacher')
                fixture.validate(f)
                self.assertNotIn('PRIVATE_SQL_ROW_SECRET', json.dumps(store.values))
                self.assertEqual(output.getvalue(), '')

    def test_expiry_requires_exact_nonce_and_settled_unknown_request(self):
        f, store = self.fresh(), MemoryStore()
        adapter = FakeAdapter(f)
        f.update(state='partial_requires_expiry', pending='bootstrap_admin')
        with self.assertRaises(fixture.FixtureError):
            fixture.expire(f, store, adapter, '0' * 24, True)
        with self.assertRaisesRegex(fixture.FixtureError, 'INFLIGHT_REQUEST_NOT_SETTLED'):
            fixture.expire(f, store, adapter, f['nonce'])
        self.assertEqual(adapter.bounded, [])
        fixture.expire(f, store, adapter, f['nonce'], True)
        self.assertEqual(adapter.bounded, [True])
        self.assertEqual(f['state'], 'expired')

    def test_invalid_real_looking_account_label_or_long_student_code_rejected(self):
        for label in ('teacher', 'QA-', 'QA-real@school', '../QA-data'):
            with self.assertRaises(fixture.FixtureError):
                fixture.fresh(label, REVISION, 289, ORIGIN, [])
        f = self.fresh()
        f['roles']['teacher']['email'] = 'owner@school.example'
        with self.assertRaises(fixture.FixtureError):
            fixture.validate(f)
        f = self.fresh()
        f['seats'] = [{'seat': str(uuid.uuid4()), 'studentCode': 'x' * 32}]
        with self.assertRaises(fixture.FixtureError):
            fixture.validate(f)

    def test_no_password_or_secret_in_plan_error_output(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), patch.object(fixture, 'parameters', side_effect=RuntimeError('PRIVATE_TOKEN_PASSWORD_SQL')):
            self.assertEqual(fixture.main(['--origin', ORIGIN, '--revision', REVISION, '--schema', '289', '--label', 'QA-proof']), 2)
        self.assertNotIn('PRIVATE', output.getvalue())
        self.assertNotIn('PASSWORD', output.getvalue())
        self.assertIn('FIXTURE_STAGE_FAILED', output.getvalue())

    def test_closed_resource_attach_preserves_cohort_and_rejects_foreign_class(self):
        f, store = self.fresh(), MemoryStore()
        fixture.provision(f, store, FakeAdapter(f))
        ids = {key: str(uuid.uuid4()) for key in fixture.RESOURCE_KEYS}
        ids['classroomId'] = f['resources']['classroomId']
        with self.assertRaises(fixture.FixtureError):
            fixture.attach_resources(f, f['nonce'], ids)
        with self.assertRaises(fixture.FixtureError):
            fixture.attach_resources(f, '0' * 24, ids, True)
        bad = {**ids, 'classroomId': str(uuid.uuid4())}
        with self.assertRaises(fixture.FixtureError):
            fixture.attach_resources(f, f['nonce'], bad, True)
        before = [s['seat'] for s in f['seats']]
        fixture.attach_resources(f, f['nonce'], ids, True)
        self.assertEqual(before, [s['seat'] for s in f['seats']])
        self.assertEqual(f['resourceEvidence'], 'OPERATOR_ATTESTATION_ONLY_API_ACCEPTANCE_NOT_RUN')

    def test_sql_bootstrap_and_expiry_are_narrow_no_destructive_statement(self):
        f = self.fresh()
        f['resources']['classroomId'] = str(uuid.uuid4())
        for role in ('teacher', 'admin'):
            query = fixture.account_sql(f, role)
            self.assertIn('public.auth_register_account(', query)
            self.assertIn('QA_IDENTITY_EXISTS', query)
            self.assertIn('scrypt-v1$16384$8$1$', query)
            self.assertNotIn(f['roles'][role]['token'], query)
            self.assertIn(fixture.digest(f['roles'][role]['token']), query)
        query = fixture.fence_sql(f, expire=True)
        for expected in ('QA_FOREIGN_ACCOUNT', 'QA_FOREIGN_CLASS', "member_role='owner'", "g.capability='creator'",
                         "g.capability='platform_admin'", "state='revoked'", 'policy_version=', "@example.invalid"):
            self.assertIn(expected, query)
        for forbidden in ('DELETE ', 'DROP ', 'TRUNCATE ', 'password_hash=', 'UPDATE public.classrooms', 'UPDATE public.accounts'):
            self.assertNotIn(forbidden, query)

    def test_database_adapter_uses_stdin_and_withholds_error_rows(self):
        adapter = fixture.HostAdapter.__new__(fixture.HostAdapter)
        adapter.container, adapter.user, adapter.database = 'fake-id', 'qa_admin', 'qa_database'
        secret = 'PRIVATE_SESSION_TOKEN_SQL_ROW'
        with patch.object(fixture.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='\n{"bounded":true}\n')) as run:
            self.assertEqual(adapter.sql('SELECT ' + secret), {'bounded': True})
            self.assertNotIn(secret, ' '.join(run.call_args.args[0]))
            self.assertEqual(run.call_args.kwargs['input'], 'SELECT ' + secret)
            self.assertTrue(run.call_args.kwargs['capture_output'])
            self.assertEqual(run.call_args.kwargs['timeout'], 20)
        output = io.StringIO()
        with patch.object(fixture.subprocess, 'run', return_value=SimpleNamespace(returncode=1, stdout=secret, stderr=secret)), contextlib.redirect_stdout(output):
            with self.assertRaisesRegex(fixture.FixtureError, '^DATABASE_STAGE_FAILED$'):
                adapter.sql('SELECT ' + secret)
        self.assertEqual(output.getvalue(), '')

    def test_expired_intent_never_bootstraps_and_requests_only_own_expiry(self):
        f = self.fresh(now=datetime.now(timezone.utc) - timedelta(hours=3))
        adapter, store = FakeAdapter(f), MemoryStore()
        with self.assertRaises(fixture.FixtureError):
            fixture.provision(f, store, adapter)
        self.assertEqual(adapter.calls, [])
        self.assertEqual(adapter.bounded, [True])

    def test_success_requires_database_readback_of_all_exact_session_hashes(self):
        f = self.fresh()
        f['resources']['classroomId'] = str(uuid.uuid4())
        f['seats'] = [{'seat': str(uuid.uuid4()), 'studentCode': 'Ab2345', 'token': 'X' * 64}]
        query = fixture.fence_sql(f)
        self.assertIn(fixture.digest('X' * 64), query)
        self.assertNotIn('X' * 64, query)
        self.assertIn('s.expires_at>now()', query)
        self.assertIn('(s.seat_id,s.token_hash) IN', query)
        adapter = fixture.HostAdapter.__new__(fixture.HostAdapter)
        adapter.sql = lambda query: {'bounded': False}
        with self.assertRaisesRegex(fixture.FixtureError, '^SESSION_BOUND_NOT_VERIFIED$'):
            adapter.bound(f)

    def test_pinned_source_contract_is_finite_hash_only_and_covers_public_boundary(self):
        path = Path(__file__).resolve().parents[1] / 'profiles/school-fixture-source-contract.json'
        contract = json.loads(path.read_text())
        self.assertEqual(contract['version'], 1)
        self.assertEqual(len(contract['sourceFiles']), 11)
        self.assertIn('apps/api/src/gallery.controller.ts', contract['sourceFiles'])
        self.assertIn('migrations/0241_max_external_only_registration.sql', contract['sourceFiles'])
        for path, value in contract['sourceFiles'].items():
            self.assertNotIn('..', Path(path).parts)
            self.assertRegex(value, r'^[a-f0-9]{64}$')

    def test_origin_rejects_plaintext_credentials_queries_and_other_ports(self):
        for url in ('http://school.example', 'https://u:p@school.example', 'https://school.example/path',
                    'https://school.example?token=x', 'https://school.example:444'):
            with self.assertRaises(fixture.FixtureError):
                fixture.origin(url)

    def test_private_storage_no_overwrite_nonce_no_git_no_links(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            private = root / 'private' / 'f.json'
            f = self.fresh()
            with fixture.PrivateStore(private, create=True) as store:
                store.save(f)
                self.assertEqual(store.read(), f)
                if os.name != 'nt':
                    self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o600)
                    self.assertEqual(stat.S_IMODE(private.parent.stat().st_mode), 0o700)
                with self.assertRaises(fixture.FixtureError):
                    store.save(self.fresh())
            with self.assertRaises(fixture.FixtureError):
                fixture.PrivateStore(private, create=True)
            (root / '.git').mkdir()
            with self.assertRaises(fixture.FixtureError):
                fixture.private_path(root / 'another.json')

    @unittest.skipIf(os.name == 'nt', 'POSIX private-file invariants; live apply is Linux root only')
    def test_private_storage_rejects_modes_symlink_hardlink_and_concurrent_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'private' / 'f.json'
            with fixture.PrivateStore(path, create=True) as store:
                store.save(self.fresh())
                with self.assertRaisesRegex(fixture.FixtureError, 'FIXTURE_BUSY'):
                    with fixture.PrivateStore(path):
                        pass
                path.chmod(0o644)
                with self.assertRaises(fixture.FixtureError):
                    store.read()
                path.chmod(0o600)
                os.link(path, path.with_suffix('.other'))
                with self.assertRaises(fixture.FixtureError):
                    store.read()
            link = Path(directory) / 'link'
            link.symlink_to(path.parent, target_is_directory=True)
            with self.assertRaises(fixture.FixtureError):
                fixture.private_path(link / 'f.json')


if __name__ == '__main__':
    unittest.main()
