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

    def save(self, f):
        fixture.validate(f)
        if self.fail_public_receipt and f.get('mixedResources', {}).get('visibility') == 'public':
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
        self.fail_public, self.bad_learner, self.bad_receipt = False, False, False
        self.project, self.lesson, self.run = None, None, None
        self.run_lesson, self.revision = uid(), uid()

    def sql(self, *args):
        raise AssertionError('Resource builder must not use SQL')

    def api(self, token, path, body=None, **kwargs):
        self.calls.append((token, path, copy.deepcopy(body), kwargs))
        if path == '/api/auth/me':
            a = self.f['roles']['teacher']
            return {'authenticated': True, 'account': {'id': a['accountId'], 'email': 'real@school.example' if self.foreign_teacher else a['email'],
                                                       'displayName': self.f['label'] + ' teacher'},
                    'activeWorkspace': {'workspaceId': a['workspaceId'], 'kind': 'personal'},
                    'capabilities': [{'capability': k, 'state': 'verified'} for k in ('educator', 'content_author')]}, None
        if path == '/api/classrooms/' + self.f['resources']['classroomId']:
            return {'classroom': {'id': self.f['resources']['classroomId'], 'title': self.f['label'] + ' ' + self.f['nonce'],
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
        self.assertEqual(len(writes), 7)
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
        self.assertEqual(len([c for c in api.calls if c[2]]), writes)
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
        self.assertEqual(len(catalog['routes']), 64)
        self.assertEqual(len(catalog['sourceFiles']), 31)
        self.assertEqual(catalog['sourceFiles']['apps/api/src/ai-chat.controller.ts'], 'e9823158195c6013817554fe70d9edfffa54cf4f44eeeab925031386bbd69630')
        self.assertEqual(catalog['sourceFiles']['apps/api/src/public-projects.controller.ts'], 'b51de767636a771d89fc145c12b65805b4a0f23123bf902afa3c031e9b552d78')

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


if __name__ == '__main__':
    unittest.main()
