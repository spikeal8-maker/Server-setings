"""Offline source-backed API protocol tests; no engine, database or live host."""
import contextlib
import copy
from datetime import datetime, timedelta, timezone
from email.message import Message
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import release_load_resources as resources
import release_load_fixture as fixture
from release_load_contracts import private_fixture, route_inventory


def uid():
    return str(uuid.uuid4())


def cohort():
    f = fixture.fresh('QA-mixed-offline', 'a' * 40, 294, 'https://school.example', ['docker', 'operations:updates'])
    for actor in f['roles'].values():
        actor.update({k: uid() for k in ('accountId', 'principalId', 'workspaceId', 'tenantId')})
    f.update(state='core_ready', resources={'classroomId': uid()},
             seats=[{'seat': uid(), 'studentCode': f'Qa{i:04}', 'token': 'T' + str(i).zfill(63)} for i in range(100)])
    return f


class Store:
    def __init__(self):
        self.values = []
        self.broken = False
        self.fail_public_receipt = False
        self.fail_ai_receipt = False

    def save(self, f):
        fixture.validate(f)
        if self.fail_public_receipt and f.get('mixedResources', {}).get('visibility') == 'public':
            self.broken = True
        if self.fail_ai_receipt and 'allowRevision' in f.get('mixedResources', {}).get('aiAccess', {}):
            self.broken = True
        if self.broken:
            raise OSError('PRIVATE_DISK_SECRET')
        self.values.append(copy.deepcopy(f))


class FakeAPI:
    def __init__(self, f, publication):
        self.f, self.publication = f, publication
        self.calls, self.courses = [], []
        self.extra_seat, self.extra_teacher, self.bad_publication = False, False, False
        self.foreign_teacher = False
        self.foreign_class = False
        self.fail_public, self.bad_learner, self.bad_receipt = False, False, False
        self.project, self.lesson, self.run = None, None, None
        self.run_lesson, self.revision = uid(), uid()
        self.policy_mode, self.policy_revision, self.minimum_rating = 'deny', 0, 67
        self.ai_override, self.fail_allow, self.bad_allow_receipt = 'inherit', False, False
        self.bad_ai_seat, self.intervene, self.restore_conflict = False, False, False
        self.allow_conflict, self.before_allow = False, None

    def sql(self, *args):
        raise AssertionError('Resource builder must not use SQL')

    def api(self, token, path, body=None, **kwargs):
        self.calls.append((token, path, copy.deepcopy(body), kwargs))
        if path == '/api/ai/classrooms/' + self.f['resources']['classroomId'] + '/access':
            assert token == self.f['roles']['teacher']['token']
            if body is not None:
                assert kwargs['method'] == 'PUT' and body['kind'] == 'classroom'
                assert body['minimumRating'] == self.minimum_rating
                assert self.policy_revision == body['expectedRevision']
                if body['mode'] == 'allow' and self.before_allow:
                    self.before_allow(body)
                if body['mode'] == 'allow' and self.allow_conflict:
                    self.policy_mode, self.policy_revision = 'allow', 1
                    raise OSError('PRIVATE_CONCURRENT_POLICY_CONFLICT')
                if body['mode'] == 'deny' and self.restore_conflict:
                    self.policy_revision += 1
                    raise OSError('PRIVATE_POLICY_CONFLICT')
                self.policy_mode = body['mode']
                self.policy_revision += 1
                if body['mode'] == 'allow' and self.fail_allow:
                    raise OSError('PRIVATE_ALLOW_LOST_RESPONSE')
                return {'saved': True, 'revision': 'PRIVATE_AI_SECRET' if self.bad_allow_receipt and body['mode'] == 'allow' else self.policy_revision}, None
            return {'classroomId': self.f['resources']['classroomId'], 'mode': self.policy_mode,
                    'minimumRating': self.minimum_rating, 'revision': self.policy_revision, 'readOnly': False,
                    'students': [{'seatId': s['seat'], 'displayName': 'PRIVATE_NAME', 'status': 'active',
                                  'override': self.ai_override, 'rating': 99,
                                  'canGenerate': self.policy_mode == 'allow'} for s in self.f['seats']]}, None
        if path == '/api/ai/scopes':
            seat = next(s for s in self.f['seats'] if s['token'] == token)
            if self.intervene and token == self.f['seats'][2]['token']:
                self.policy_revision += 1
                self.intervene = False
            return {'personal': False, 'items': [{'seatId': seat['seat'], 'classroomId': self.f['resources']['classroomId'],
                                                 'canGenerate': self.policy_mode == 'allow' and not self.bad_ai_seat,
                                                 'reason': None, 'minimumRating': self.minimum_rating}]}, None
        if path == '/api/auth/me':
            a = self.f['roles']['teacher']
            return {'authenticated': True, 'account': {'id': a['accountId'], 'email': 'real@school.example' if self.foreign_teacher else a['email'],
                                                       'displayName': self.f['label'] + ' teacher'},
                    'activeWorkspace': {'workspaceId': a['workspaceId'], 'kind': 'personal'},
                    'capabilities': [{'capability': k, 'state': 'verified'} for k in ('educator', 'content_author')]}, None
        if path == '/api/classrooms/' + self.f['resources']['classroomId']:
            return {'classroom': {'id': self.f['resources']['classroomId'], 'title': 'real class' if self.foreign_class else self.f['label'] + ' ' + self.f['nonce'],
                                  'teacherRole': 'owner', 'workspaceKind': 'personal'}}, None
        if path.endswith('/roster'):
            rows = [{'id': s['seat']} for s in self.f['seats']]
            return {'items': rows + ([{'id': uid()}] if self.extra_seat else [])}, None
        if path.endswith('/teachers'):
            rows = [{'accountId': self.f['roles']['teacher']['accountId'], 'role': 'owner'}]
            return {'items': rows + ([{'accountId': uid(), 'role': 'co_teacher'}] if self.extra_teacher else []), 'invitations': []}, None
        if path == '/api/public-projects/' + self.publication:
            return {'publication': {'publicationId': uid() if self.bad_publication else self.publication,
                                    'revisionId': self.revision, 'revisionNo': 1, 'authorAvatarUrl': None}}, None
        if path == '/api/public-projects/' + self.publication + '/artifact':
            assert token is None and kwargs['revision_id'] == self.revision
            return {'artifact': {'schemaVersion': 1, 'provenance': {'publicationId': self.publication, 'revisionNumber': 1},
                                  'capabilities': {'readOnly': True}, 'status': 'display'}}, None
        if path == '/api/courses' and body is None:
            return {'items': copy.deepcopy(self.courses)}, None
        if path == '/api/projects' and body:
            self.project = {'id': uid(), 'title': body['title'], 'moduleKey': body['module'],
                            'classroomId': body['classroomId'], 'scope': body['scope']}
            return {'project': copy.deepcopy(self.project), 'created': True}, None
        if path == '/api/courses' and body:
            row = {'id': uid(), 'title': body['title'], 'visibility': 'private', 'archivedAt': None}
            self.courses.append(row)
            return {'id': row['id'], 'draftRevision': 'PRIVATE_API_SECRET' if self.bad_receipt else 1}, None
        if path.endswith('/sections') and body:
            return {'id': uid(), 'draftRevision': 2}, None
        if path.endswith('/lessons') and body:
            assert body['kind'] == 'material' and all(b['type'] == 'paragraph' for b in body['blocks'])
            self.lesson = uid()
            return {'id': self.lesson, 'draftRevision': 3}, None
        if path.endswith('/publish') and body:
            return {'versionId': uid(), 'versionNumber': 1}, None
        if path.endswith('/course-runs') and body:
            assert body['dueAt'] is None and body['audienceType'] == 'whole_class' and body['seatIds'] == []
            self.run = uid()
            return {'runId': self.run, 'versionNumber': 1}, None
        if path.startswith('/api/sharing/course/'):
            assert kwargs.get('method') == 'PUT' and path.split('/')[-2] == self.courses[0]['id']
            self.courses[0]['visibility'] = body['visibility']
            if body['visibility'] == 'public' and self.fail_public:
                raise OSError('PRIVATE_LOST_PUBLIC_RESPONSE')
            return {'ok': True}, None
        if self.project and path == '/api/projects/' + self.project['id']:
            return {'project': copy.deepcopy(self.project), 'draft': {'document': {}}}, None
        if path.startswith('/api/gallery/knowledge/'):
            assert self.courses[0]['visibility'] == 'public'
            return {'title': self.courses[0]['title'], 'versionNumber': 1, 'sections': [{}]}, None
        if path == '/api/class-join/me/course-runs':
            if self.bad_learner and token == self.f['seats'][2]['token']:
                return {'items': []}, None
            return {'items': [{'id': self.run, 'courseId': self.courses[0]['id'],
                               'sections': [{'lessons': [{'id': self.run_lesson, 'sourceLessonId': self.lesson, 'kind': 'material'}]}]}]}, None
        raise AssertionError('Unexpected offline API route')


class MixedResourcesTests(unittest.TestCase):
    def test_plan_has_no_source_fixture_network_or_subprocess_access(self):
        output = io.StringIO()
        with patch.object(resources, 'PrivateStore', side_effect=AssertionError('fixture read')), \
             patch.object(resources.subprocess, 'run', side_effect=AssertionError('process')), \
             patch.object(resources, 'HostAdapter', side_effect=AssertionError('host')), contextlib.redirect_stdout(output):
            self.assertEqual(resources.main(['--origin', 'https://school.example', '--revision', 'a' * 40,
                                              '--schema', '294', '--label', 'QA-mixed-plan']), 0)
        self.assertEqual(json.loads(output.getvalue())['state'], 'PLAN_ONLY')

    def test_full_mixed_ids_are_verified_and_preserve_original_sessions(self):
        f, store, publication = cohort(), Store(), uid()
        before = copy.deepcopy(f)
        api = FakeAPI(f, publication)
        resources.build(f, store, api, publication)
        self.assertEqual(f['mixedResources']['state'], 'ready')
        self.assertEqual(f['resources']['lessonId'], api.run_lesson)
        self.assertNotEqual(f['resources']['lessonId'], api.lesson)
        self.assertEqual(f['resources']['publicationId'], publication)
        self.assertEqual(f['seats'], before['seats'])
        self.assertEqual(f['roles'], before['roles'])
        self.assertEqual(f['expiresAt'], before['expiresAt'])
        self.assertEqual(len([c for c in api.calls if c[1] == '/api/class-join/me/course-runs']), 100)
        writes = [c for c in api.calls if c[2] is not None]
        self.assertEqual(len(writes), 8)
        self.assertEqual(len([c for c in api.calls if c[1] == '/api/ai/scopes']), 100)
        self.assertEqual(f['mixedResources']['aiAccess']['state'], 'enabled')
        self.assertEqual(f['mixedResources']['aiAccess']['minimumRating'], 67)
        self.assertTrue(all('/publication' not in c[1] and '/invite' not in c[1] for c in writes))
        self.assertTrue(all(c[0] is None for c in api.calls if c[1].startswith('/api/public-projects/')))
        with tempfile.TemporaryDirectory() as directory:
            with fixture.PrivateStore(Path(directory) / 'private' / 'f.json', create=True) as disk:
                disk.save(f)
                self.assertEqual(private_fixture(disk.path, f['origin'], 100, 'mixed')['resources'], f['resources'])
        with self.assertRaisesRegex(fixture.FixtureError, 'RESOURCE_NO_AUTOMATIC_REPLAY'):
            resources.build(f, store, api, publication)

    def test_real_extra_seat_teacher_or_nonpublic_publication_blocks_all_writes(self):
        for field in ('foreign_teacher', 'extra_seat', 'extra_teacher', 'bad_publication'):
            with self.subTest(field=field):
                f, store, publication = cohort(), Store(), uid()
                api = FakeAPI(f, publication)
                setattr(api, field, True)
                with self.assertRaises(fixture.FixtureError):
                    resources.build(f, store, api, publication)
                self.assertFalse(any(c[2] is not None for c in api.calls))
                self.assertNotIn('mixedResources', f)

    def test_lost_public_response_cleanup_and_unknown_are_preserved_without_replay(self):
        f, store, publication = cohort(), Store(), uid()
        api = FakeAPI(f, publication)
        api.fail_public = True
        with self.assertRaisesRegex(fixture.FixtureError, '^RESOURCE_PARTIAL_RETAINED$'):
            resources.build(f, store, api, publication)
        self.assertEqual(api.courses[0]['visibility'], 'private')
        self.assertEqual(f['mixedResources']['cleanup'], 'PRIVATE_OBSERVED_INFLIGHT_UNKNOWN')
        with self.assertRaisesRegex(fixture.FixtureError, 'RESOURCE_INFLIGHT_NOT_SETTLED'):
            resources.cleanup(f, store, api)
        resources.cleanup(f, store, api, attest_no_inflight=True)
        self.assertEqual(f['mixedResources']['state'], 'cleaned')
        self.assertEqual(f['mixedResources']['cleanup'], 'PRIVATE_VERIFIED')
        self.assertEqual(set(f['resources']), {'classroomId'})

    def test_public_receipt_disk_failure_does_not_prevent_private_cleanup(self):
        f, store, publication = cohort(), Store(), uid()
        store.fail_public_receipt = True
        api = FakeAPI(f, publication)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            with self.assertRaisesRegex(fixture.FixtureError, '^RESOURCE_PARTIAL_RECEIPT_FAILED$'):
                resources.build(f, store, api, publication)
        self.assertEqual(api.courses[0]['visibility'], 'private')
        self.assertEqual(store.values[-1]['mixedResources']['pending'], 'public')
        self.assertEqual(store.values[-1]['mixedResources']['visibility'], 'unknown')
        self.assertEqual(output.getvalue(), '')

    def test_malformed_course_receipt_cannot_poison_recovery_ownership(self):
        f, store, publication = cohort(), Store(), uid()
        api = FakeAPI(f, publication)
        api.bad_receipt = True
        with self.assertRaisesRegex(fixture.FixtureError, '^RESOURCE_PARTIAL_RETAINED$'):
            resources.build(f, store, api, publication)
        self.assertNotIn('courseId', f['mixedResources'])
        self.assertNotIn('PRIVATE_API_SECRET', json.dumps(store.values))
        self.assertEqual(api.courses[0]['visibility'], 'private')

    def test_missing_pupil_positive_fails_and_withdraws_only_own_course(self):
        f, store, publication = cohort(), Store(), uid()
        api = FakeAPI(f, publication)
        api.bad_learner = True
        with self.assertRaises(fixture.FixtureError):
            resources.build(f, store, api, publication)
        self.assertEqual(api.courses[0]['visibility'], 'private')
        self.assertEqual(set(f['resources']), {'classroomId'})

    def test_cleanup_refuses_foreign_renamed_course_and_never_extends_expired_session(self):
        f, store, publication = cohort(), Store(), uid()
        api = FakeAPI(f, publication)
        resources.build(f, store, api, publication)
        api.courses[0]['title'] = 'foreign course'
        writes = len([c for c in api.calls if c[2]])
        with self.assertRaises(fixture.FixtureError):
            resources.cleanup(f, store, api)
        new_writes = [c for c in api.calls if c[2]][writes:]
        self.assertTrue(all(c[1].startswith('/api/ai/classrooms/') and c[2]['mode'] == 'deny' for c in new_writes))
        self.assertEqual(api.policy_mode, 'deny')
        api.courses[0]['title'] = resources.title(f, 'course')
        calls = len(api.calls)
        with patch.object(resources, 'datetime', wraps=datetime) as clock:
            clock.now.return_value = datetime.now(timezone.utc) + timedelta(hours=3)
            with self.assertRaisesRegex(fixture.FixtureError, 'RESOURCE_SESSION_EXPIRED'):
                resources.cleanup(f, store, api)
        self.assertEqual(len(api.calls), calls)

    def test_additional_side_effect_source_drift_blocks_before_api_writes(self):
        with patch.object(resources.subprocess, 'run', return_value=type('Result', (), {'returncode': 0, 'stdout': b'changed-source'})()):
            with self.assertRaisesRegex(fixture.FixtureError, 'RESOURCE_SOURCE_DRIFT'):
                resources.verify_source('/unused/canonical/root', 'a' * 40)

    def test_pinned_routes_keep_sse_and_exact_count_while_alignment_is_finite(self):
        catalog = route_inventory()
        self.assertEqual(len(catalog['routes']), 65)
        self.assertEqual(len(catalog['sourceFiles']), 44)
        self.assertEqual(catalog['sourceFiles']['apps/api/src/app.module.ts'], '8e84ef7b640538d0ae3637437cd834dfcc4dbc8df21c0c8bf1369a3cfebef476')
        self.assertEqual(catalog['sourceFiles']['apps/api/src/ai-chat.controller.ts'], '4fe4b7d75aeb6e11ca3fb95d76979bd6bd30d34330063957f5db3bb40d380416')
        self.assertEqual(catalog['sourceFiles']['apps/api/src/public-projects.controller.ts'], 'b51de767636a771d89fc145c12b65805b4a0f23123bf902afa3c031e9b552d78')
        for path in ('apps/api/src/chess-network.controller.ts', 'apps/api/src/ai-working-files.ts',
                     'migrations/0294_games_chess_network.sql', 'migrations/0296_ai_working_files.sql',
                     'migrations/0298_ai_image_file_binding.sql', 'migrations/0300_ai_image_durable_queue.sql'):
            self.assertIn(path, catalog['sourceFiles'])

    def test_host_transport_only_adds_explicit_course_visibility_and_public_revision_query(self):
        class Response:
            status = 200
            headers = Message()

            def __init__(self, url):
                self.url = url

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def geturl(self):
                return self.url

            def read(self, limit):
                return b'{"ok":true}'

        class Opener:
            calls = []

            def open(self, request, timeout):
                self.calls.append(request)
                return Response(request.full_url)

        adapter = fixture.HostAdapter.__new__(fixture.HostAdapter)
        adapter.url, adapter.opener = 'https://school.example', Opener()
        course, pub, revision = uid(), uid(), uid()
        adapter.api('A' * 64, '/api/sharing/course/' + course + '/visibility', {'visibility': 'private'}, method='PUT')
        adapter.api(None, '/api/public-projects/' + pub + '/artifact', revision_id=revision)
        self.assertEqual(adapter.opener.calls[0].get_method(), 'PUT')
        self.assertEqual(adapter.opener.calls[1].full_url, adapter.url + '/api/public-projects/' + pub + '/artifact?revisionId=' + revision)
        for kwargs in ({'method': 'DELETE'}, {'method': 'PUT'}, {'revision_id': revision}):
            with self.assertRaises(fixture.FixtureError):
                adapter.api(None, '/api/elsewhere', **kwargs)
        policy = {'kind': 'classroom', 'mode': 'allow', 'minimumRating': 67,
                  'expectedRevision': 0, 'requestId': uid()}
        route = '/api/ai/classrooms/' + uid() + '/access'
        adapter.api('A' * 64, route, policy, method='PUT')
        self.assertEqual(adapter.opener.calls[-1].get_method(), 'PUT')
        for change in ({'mode': 'rating'}, {'kind': 'seat'}, {'expectedRevision': True},
                       {'expectedRevision': 1}, {'minimumRating': -1}, {'minimumRating': True},
                       {'requestId': 'PRIVATE_SECRET'}, {'extra': 'PRIVATE_SECRET'}):
            with self.assertRaises(fixture.FixtureError):
                adapter.api('A' * 64, route, policy | change, method='PUT')
        for route in ('/api/admin/ai/settings', '/api/ai/classrooms/not-a-uuid/access'):
            with self.assertRaises(fixture.FixtureError):
                adapter.api('A' * 64, route, policy, method='PUT')


class QaAiPermissionTests(unittest.TestCase):
    def ready_legacy(self):
        f, store, publication = cohort(), Store(), uid()
        api = FakeAPI(f, publication)
        resources.build(f, store, api, publication)
        del f['mixedResources']['aiAccess']
        api.policy_mode, api.policy_revision = 'deny', 0
        api.calls.clear()
        store.values.clear()
        return f, store, api

    def test_existing_ready_fixture_gets_explicit_100_seat_permission_proof(self):
        f, store, api = self.ready_legacy()
        original = copy.deepcopy(f)
        def assert_intent_before_write(body):
            a = store.values[-1]['mixedResources']['aiAccess']
            self.assertEqual(body['requestId'], a['requestIds']['allow'])
            self.assertNotEqual(body['requestId'], a['requestIds']['restore'])
            self.assertNotIn('allowRevision', a)
            self.assertEqual(store.values[-1]['mixedResources']['pending'], 'ai-access')
        api.before_allow = assert_intent_before_write
        resources.enable_existing_ai(f, store, api)
        self.assertEqual((api.policy_mode, api.policy_revision), ('allow', 1))
        self.assertEqual(f['mixedResources']['aiAccess']['state'], 'enabled')
        self.assertIsNone(f['mixedResources']['pending'])
        self.assertEqual([c[0] for c in api.calls if c[1] == '/api/ai/scopes'], [s['token'] for s in f['seats']])
        for key in ('roles', 'seats', 'expiresAt', 'resources'):
            self.assertEqual(f[key], original[key])
        intents = [s['mixedResources'] for s in store.values if s['mixedResources']['pending'] == 'ai-access']
        self.assertEqual(len(intents), 1)
        self.assertNotIn('allowRevision', intents[0]['aiAccess'])
        self.assertEqual(set(intents[0]['aiAccess']['requestIds']), {'allow', 'restore'})
        self.assertNotIn('PRIVATE_NAME', json.dumps(store.values))
        calls = len(api.calls)
        with self.assertRaisesRegex(fixture.FixtureError, 'RESOURCE_AI_NO_AUTOMATIC_REPLAY'):
            resources.enable_existing_ai(f, store, api)
        self.assertEqual(len(api.calls), calls)

    def test_cleanup_restores_prior_minimum_and_deny_cas_and_hides_course_idempotently(self):
        f, store, api = self.ready_legacy()
        resources.enable_existing_ai(f, store, api)
        resources.cleanup(f, store, api)
        self.assertEqual((api.policy_mode, api.policy_revision, api.minimum_rating), ('deny', 2, 67))
        self.assertEqual(api.courses[0]['visibility'], 'private')
        self.assertEqual(f['mixedResources']['aiAccess']['cleanup'], 'DENY_VERIFIED')
        self.assertEqual(f['mixedResources']['state'], 'cleaned')
        writes = [c for c in api.calls if c[2] is not None]
        self.assertEqual(writes[-2][2], {'kind': 'classroom', 'mode': 'deny', 'minimumRating': 67,
                                       'expectedRevision': 1, 'requestId': f['mixedResources']['aiAccess']['requestIds']['restore']})
        resources.cleanup(f, store, api)
        self.assertEqual(len([c for c in api.calls if c[2] is not None]), len(writes))

    def test_foreign_class_membership_or_nonpristine_policy_prevents_every_put(self):
        for field, value in (('foreign_class', True), ('extra_seat', True), ('extra_teacher', True),
                             ('foreign_teacher', True), ('policy_mode', 'allow'), ('policy_revision', 1), ('ai_override', 'allow')):
            with self.subTest(field=field):
                f, store, api = self.ready_legacy()
                setattr(api, field, value)
                with self.assertRaises(fixture.FixtureError):
                    resources.enable_existing_ai(f, store, api)
                self.assertFalse(any(c[2] is not None for c in api.calls))
                self.assertNotIn('aiAccess', f['mixedResources'])

    def test_allow_receipt_disk_failure_still_restores_deny_and_hides_course(self):
        f, store, api = self.ready_legacy()
        store.fail_ai_receipt = True
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            with self.assertRaisesRegex(fixture.FixtureError, '^RESOURCE_PARTIAL_RECEIPT_FAILED$'):
                resources.enable_existing_ai(f, store, api)
        self.assertEqual((api.policy_mode, api.policy_revision), ('deny', 2))
        self.assertEqual(api.courses[0]['visibility'], 'private')
        self.assertEqual(f['mixedResources']['aiAccess']['cleanup'], 'DENY_VERIFIED')
        self.assertEqual(store.values[-1]['mixedResources']['pending'], 'ai-access')
        self.assertNotIn('allowRevision', store.values[-1]['mixedResources']['aiAccess'])
        self.assertEqual(set(f['resources']), {'classroomId'})
        self.assertEqual(output.getvalue(), '')

    def test_failed_intent_save_never_allows_and_still_hides_course(self):
        f, store, api = self.ready_legacy()
        store.broken = True
        with self.assertRaisesRegex(fixture.FixtureError, '^RESOURCE_PARTIAL_RECEIPT_FAILED$'):
            resources.enable_existing_ai(f, store, api)
        self.assertFalse(any(c[1].startswith('/api/ai/classrooms/') and c[2] is not None for c in api.calls))
        self.assertEqual((api.policy_mode, api.policy_revision), ('deny', 0))
        self.assertEqual(api.courses[0]['visibility'], 'private')

    def test_manual_cleanup_disk_failure_cannot_skip_either_owned_compensation(self):
        f, store, api = self.ready_legacy()
        resources.enable_existing_ai(f, store, api)
        store.broken = True
        with self.assertRaises(OSError):
            resources.cleanup(f, store, api)
        self.assertEqual((api.policy_mode, api.policy_revision), ('deny', 2))
        self.assertEqual(api.courses[0]['visibility'], 'private')
        self.assertEqual(f['mixedResources']['aiAccess']['cleanup'], 'DENY_VERIFIED')

    def test_malformed_initial_policy_refuses_before_grant_without_persisting_payload(self):
        for change in ({'readOnly': True}, {'classroomId': uid()}, {'revision': False},
                       {'minimumRating': True}, {'students': [{'seatId': 'PRIVATE_API_PAYLOAD'}]}):
            with self.subTest(change=change):
                f, store, api = self.ready_legacy()
                original_api = api.api
                def malformed(token, path, body=None, **kwargs):
                    data, cookie = original_api(token, path, body, **kwargs)
                    if path.endswith('/access') and body is None:
                        data.update(change)
                    return data, cookie
                with patch.object(api, 'api', side_effect=malformed):
                    with self.assertRaisesRegex(fixture.FixtureError, 'RESOURCE_AI_PREFLIGHT_FAILED'):
                        resources.enable_existing_ai(f, store, api)
                self.assertFalse(any(c[2] is not None for c in api.calls))
                self.assertNotIn('aiAccess', f['mixedResources'])
                self.assertEqual(store.values, [])

    def test_malformed_readback_after_valid_ack_restores_known_owned_revision(self):
        f, store, api = self.ready_legacy()
        original_api, corrupted = api.api, False
        def malformed(token, path, body=None, **kwargs):
            nonlocal corrupted
            data, cookie = original_api(token, path, body, **kwargs)
            if path.endswith('/access') and body is None and api.policy_revision == 1 and not corrupted:
                corrupted = True
                data['students'][0]['canGenerate'] = 'PRIVATE_API_SECRET'
            return data, cookie
        with patch.object(api, 'api', side_effect=malformed):
            with self.assertRaisesRegex(fixture.FixtureError, '^RESOURCE_PARTIAL_RETAINED$'):
                resources.enable_existing_ai(f, store, api)
        self.assertEqual((api.policy_mode, api.policy_revision), ('deny', 2))
        self.assertEqual(api.courses[0]['visibility'], 'private')
        self.assertNotIn('PRIVATE_API_SECRET', json.dumps(store.values))

    def test_lost_or_malformed_allow_does_not_adopt_unproven_revision_or_replay(self):
        for field in ('fail_allow', 'bad_allow_receipt'):
            with self.subTest(field=field):
                f, store, api = self.ready_legacy()
                setattr(api, field, True)
                with self.assertRaisesRegex(fixture.FixtureError, '^RESOURCE_PARTIAL_RETAINED$'):
                    resources.enable_existing_ai(f, store, api)
                self.assertEqual((api.policy_mode, api.policy_revision), ('allow', 1))
                self.assertEqual(f['mixedResources']['aiAccess']['cleanup'], 'UNKNOWN_OPERATOR_AI_CLEANUP_REQUIRED')
                self.assertNotIn('allowRevision', f['mixedResources']['aiAccess'])
                self.assertNotIn('PRIVATE_AI_SECRET', json.dumps(store.values))
                self.assertEqual(api.courses[0]['visibility'], 'private')
                with self.assertRaisesRegex(fixture.FixtureError, 'RESOURCE_INFLIGHT_NOT_SETTLED'):
                    resources.cleanup(f, store, api)
                with self.assertRaisesRegex(fixture.FixtureError, 'RESOURCE_CLEANUP_OPERATOR_REQUIRED'):
                    resources.cleanup(f, store, api, attest_no_inflight=True)
                policy_writes = [c for c in api.calls if c[1].startswith('/api/ai/classrooms/') and c[2] is not None]
                self.assertEqual(len(policy_writes), 1)
                self.assertEqual(f['mixedResources']['state'], 'partial')

    def test_intervening_policy_revision_is_not_overwritten_and_course_is_hidden(self):
        f, store, api = self.ready_legacy()
        api.intervene = True
        with self.assertRaisesRegex(fixture.FixtureError, '^RESOURCE_PARTIAL_RETAINED$'):
            resources.enable_existing_ai(f, store, api)
        self.assertEqual((api.policy_mode, api.policy_revision), ('allow', 2))
        self.assertEqual(f['mixedResources']['aiAccess']['cleanup'], 'REVISION_CHANGED_OPERATOR_REQUIRED')
        self.assertEqual(api.courses[0]['visibility'], 'private')
        self.assertFalse(any(c[2] and c[2].get('mode') == 'deny' for c in api.calls))

    def test_allow_cas_conflict_never_adopts_other_actors_revision_one(self):
        f, store, api = self.ready_legacy()
        api.allow_conflict = True
        with self.assertRaisesRegex(fixture.FixtureError, '^RESOURCE_PARTIAL_RETAINED$'):
            resources.enable_existing_ai(f, store, api)
        self.assertEqual((api.policy_mode, api.policy_revision), ('allow', 1))
        self.assertNotIn('allowRevision', f['mixedResources']['aiAccess'])
        self.assertFalse(any(c[2] and c[2].get('mode') == 'deny' for c in api.calls))
        self.assertEqual(api.courses[0]['visibility'], 'private')

    def test_cleanup_cas_race_or_ai_projection_failure_does_not_skip_course_cleanup(self):
        for conflict in (True, False):
            with self.subTest(conflict=conflict):
                f, store, api = self.ready_legacy()
                resources.enable_existing_ai(f, store, api)
                if conflict:
                    api.restore_conflict = True
                else:
                    api.ai_override = 'deny'
                with self.assertRaisesRegex(fixture.FixtureError, 'RESOURCE_CLEANUP_OPERATOR_REQUIRED'):
                    resources.cleanup(f, store, api)
                self.assertEqual(api.policy_mode, 'allow')
                self.assertEqual(api.courses[0]['visibility'], 'private')
                self.assertEqual(f['mixedResources']['state'], 'partial')
                self.assertEqual(f['mixedResources']['pending'], 'cleanup')
                calls = len(api.calls)
                with self.assertRaisesRegex(fixture.FixtureError, 'RESOURCE_INFLIGHT_NOT_SETTLED'):
                    resources.cleanup(f, store, api)
                self.assertEqual(len(api.calls), calls)

    def test_one_denied_learner_positive_withdraws_known_owned_allow(self):
        f, store, api = self.ready_legacy()
        api.bad_ai_seat = True
        with self.assertRaisesRegex(fixture.FixtureError, '^RESOURCE_PARTIAL_RETAINED$'):
            resources.enable_existing_ai(f, store, api)
        self.assertEqual((api.policy_mode, api.policy_revision), ('deny', 2))
        self.assertEqual(api.courses[0]['visibility'], 'private')
        self.assertEqual(f['mixedResources']['aiAccess']['cleanup'], 'DENY_VERIFIED')

    def test_ai_enable_plan_and_full_chain_pin_count_fail_closed_without_io(self):
        output = io.StringIO()
        with patch.object(resources, 'PrivateStore', side_effect=AssertionError('fixture')), \
             patch.object(resources, 'HostAdapter', side_effect=AssertionError('host')), \
             patch.object(resources.subprocess, 'run', side_effect=AssertionError('process')), contextlib.redirect_stdout(output):
            self.assertEqual(resources.main(['ai-enable', '--origin', 'https://school.example', '--revision', 'a' * 40,
                                             '--schema', '294', '--label', 'QA-ai-plan']), 0)
        plan = json.loads(output.getvalue())
        self.assertTrue(plan['allowsOwnSyntheticClassAi'])
        self.assertEqual((plan['fixtureReads'], plan['networkRequests']), (0, 0))
        contract_path = Path(resources.__file__).resolve().parents[1] / 'profiles/school-mixed-resource-contract.json'
        contract = json.loads(contract_path.read_text())
        self.assertEqual(len(contract['sourceFiles']), 23)
        for path in ('apps/api/src/ai-access.controller.ts', 'apps/api/src/ai-access.service.ts', 'migrations/0226_ai_student_admission.sql'):
            self.assertIn(path, contract['sourceFiles'])
        del contract['sourceFiles']['apps/api/src/ai-access.service.ts']
        with patch.object(Path, 'read_text', return_value=json.dumps(contract)), \
             patch.object(resources.subprocess, 'run', side_effect=AssertionError('must validate before subprocess')):
            with self.assertRaisesRegex(fixture.FixtureError, 'RESOURCE_SOURCE_CONTRACT_INVALID'):
                resources.verify_source('/unused', 'a' * 40)


if __name__ == '__main__':
    unittest.main()
