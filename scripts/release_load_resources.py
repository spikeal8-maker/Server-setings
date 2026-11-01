#!/usr/bin/env python3
"""API-only own synthetic mixed resources. No --apply means no I/O.

Creates only QA material, never a publication or invitation. Cleanup precedes
cohort session expiry; it cannot extend a session or recover an expired login.
"""
import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid

from release_load_fixture import (FixtureError, HostAdapter, PrivateStore, ident,
                                  need, parameters, validate)

STAGES = {'project', 'course', 'section', 'lesson', 'publish', 'run', 'public', 'positives', 'cleanup'}
META_KEYS = {'version', 'state', 'binding', 'pending', 'publicationId', 'requestIds',
             'classProjectId', 'courseId', 'sectionId', 'sourceLessonId', 'versionId',
             'versionNumber', 'draftRevision', 'runId', 'runLessonId', 'visibility', 'cleanup'}


def title(f, kind):
    return f['label'] + ' ' + f['nonce'] + ' mixed ' + kind


def binding(f):
    data = {'nonce': f['nonce'], 'origin': f['origin'], 'revision': f['revision'], 'schema': f['schema'],
            'installation': f.get('installation'), 'expiresAt': f['expiresAt'],
            'roles': {k: a['accountId'] for k, a in f['roles'].items()},
            'classroom': f['resources']['classroomId'], 'seats': [s['seat'] for s in f['seats']],
            'requiredSources': f['expectedRequiredLogSourceIds']}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def validate_resources(f):
    validate(f)
    m = f.get('mixedResources')
    need(isinstance(m, dict) and set(m) <= META_KEYS and m.get('version') == 1
         and m.get('binding') == binding(f), 'RESOURCE_BINDING_INVALID')
    need(m.get('state') in ('intent', 'running', 'ready', 'partial', 'cleaned')
         and m.get('pending') in STAGES | {None}
         and m.get('visibility') in ('private', 'public', 'unknown', 'not_created'), 'RESOURCE_STATE_INVALID')
    ident(m.get('publicationId'))
    need(isinstance(m.get('requestIds'), dict) and set(m['requestIds']) == {'project', 'course', 'publish', 'run'}, 'RESOURCE_REQUEST_IDS_INVALID')
    for value in m['requestIds'].values():
        ident(value)
    for key in ('classProjectId', 'courseId', 'sectionId', 'sourceLessonId', 'versionId', 'runId', 'runLessonId'):
        if key in m:
            ident(m[key])
    for key in ('draftRevision', 'versionNumber'):
        if key in m:
            need(type(m[key]) is int and 0 < m[key] < 1000000, 'RESOURCE_REVISION_INVALID')
    if 'cleanup' in m:
        need(m['cleanup'] in ('PRIVATE_OBSERVED_INFLIGHT_UNKNOWN', 'PRIVATE_VERIFIED',
                              'UNKNOWN_OPERATOR_CLEANUP_REQUIRED', 'NOT_CREATED_VERIFIED'), 'RESOURCE_CLEANUP_INVALID')
    return m


class Calls:
    def __init__(self, f, adapter, *, cleanup=False):
        self.f, self.adapter = f, adapter
        self.left = 20 if cleanup else 140
        self.deadline = time.monotonic() + (120 if cleanup else 300)

    def __call__(self, path, body=None, *, user='teacher', **kwargs):
        need(self.left > 0 and time.monotonic() < self.deadline, 'RESOURCE_REQUEST_BUDGET_EXCEEDED')
        need(datetime.now(timezone.utc) < datetime.fromisoformat(self.f['expiresAt']), 'RESOURCE_SESSION_EXPIRED')
        self.left -= 1
        actor = self.f['seats'][user] if isinstance(user, int) else self.f['roles'].get(user)
        cookie = 'asa_student_session' if isinstance(user, int) else 'asa_session'
        data, _ = self.adapter.api(actor['token'] if actor else None, path, body, cookie=cookie, **kwargs)
        need(isinstance(data, dict) and 'error' not in data, 'RESOURCE_API_INVALID')
        return data


def teacher_identity(f, call):
    who = call('/api/auth/me')
    actor = f['roles']['teacher']
    need(who.get('authenticated') is True and who.get('account', {}).get('id') == actor['accountId']
         and who['account'].get('email') == actor['email']
         and who['account'].get('displayName') == f['label'] + ' teacher'
         and who.get('activeWorkspace', {}).get('workspaceId') == actor['workspaceId']
         and who['activeWorkspace'].get('kind') == 'personal', 'RESOURCE_FOREIGN_TEACHER')
    capabilities = who.get('capabilities', [])
    need(all(any(c.get('capability') == key and c.get('state') == 'verified' for c in capabilities)
             for key in ('educator', 'content_author')), 'RESOURCE_QA_CAPABILITY_MISSING')


def own_class(f, call):
    teacher_identity(f, call)
    cid = ident(f['resources']['classroomId'])
    classroom = call('/api/classrooms/' + cid).get('classroom', {})
    need(classroom.get('id') == cid and classroom.get('teacherRole') == 'owner'
         and classroom.get('workspaceKind') == 'personal'
         and classroom.get('title') == f['label'] + ' ' + f['nonce'], 'RESOURCE_FOREIGN_CLASS')
    roster = call('/api/classrooms/' + cid + '/roster').get('items')
    need(isinstance(roster, list) and len(roster) == 100
         and {r.get('id') for r in roster} == {s['seat'] for s in f['seats']}, 'RESOURCE_REAL_OR_MISSING_PUPILS')
    team = call('/api/classrooms/' + cid + '/teachers')
    people = team.get('items')
    need(isinstance(people, list) and len(people) == 1
         and people[0].get('accountId') == f['roles']['teacher']['accountId']
         and people[0].get('role') == 'owner' and team.get('invitations') == [], 'RESOURCE_REAL_OR_INVITED_TEACHERS')


def owned_course(f, call, *, allow_missing=False):
    # This API lists only courses owned by the authenticated author. Exact
    # nonce title also recovers a lost create response without adopting data.
    rows = call('/api/courses').get('items')
    need(isinstance(rows, list), 'RESOURCE_COURSE_LIST_INVALID')
    found = [r for r in rows if r.get('title') == title(f, 'course')]
    need(len(found) <= 1, 'RESOURCE_COURSE_AMBIGUOUS')
    if not found:
        need(f['mixedResources'].get('courseId') is None, 'RESOURCE_KNOWN_COURSE_NOT_OWNED')
        need(allow_missing, 'RESOURCE_OWN_COURSE_MISSING')
        return None
    row = found[0]
    ident(row.get('id'))
    known = f['mixedResources'].get('courseId')
    need(known is None or row['id'] == known, 'RESOURCE_FOREIGN_COURSE')
    need(row.get('visibility') in ('private', 'public') and row.get('archivedAt') is None, 'RESOURCE_COURSE_STATE_CHANGED')
    return row


def cleanup_visibility(f, adapter, *, settled=False):
    validate_resources(f)
    call = Calls(f, adapter, cleanup=True)
    teacher_identity(f, call)
    row = owned_course(f, call, allow_missing=True)
    if row is None:
        need(settled, 'RESOURCE_CREATE_INFLIGHT_UNKNOWN')
        return 'NOT_CREATED_VERIFIED'
    if row['visibility'] != 'private':
        result = call('/api/sharing/course/' + row['id'] + '/visibility', {'visibility': 'private'}, method='PUT')
        need(result.get('ok') is True, 'RESOURCE_PRIVATE_WRITE_UNVERIFIED')
    after = owned_course(f, call)
    need(after['visibility'] == 'private', 'RESOURCE_PRIVATE_READBACK_FAILED')
    return 'PRIVATE_VERIFIED' if settled else 'PRIVATE_OBSERVED_INFLIGHT_UNKNOWN'


def verify_publication(call, publication_id):
    p = call('/api/public-projects/' + ident(publication_id), user=None).get('publication', {})
    need(p.get('publicationId') == publication_id, 'RESOURCE_PUBLICATION_NOT_PUBLIC')
    rid = ident(p.get('revisionId'))
    need(type(p.get('revisionNo')) is int and p['revisionNo'] > 0, 'RESOURCE_PUBLICATION_INVALID')
    artifact = call('/api/public-projects/' + publication_id + '/artifact', user=None, revision_id=rid).get('artifact', {})
    need(artifact.get('provenance') == {'publicationId': publication_id, 'revisionNumber': p['revisionNo']}
         and type(artifact.get('schemaVersion')) is int and artifact['schemaVersion'] > 0
         and artifact.get('capabilities', {}).get('readOnly') is True
         and artifact.get('status') in ('display', 'simulation', 'static-only'), 'RESOURCE_PUBLIC_ARTIFACT_INVALID')


def build(f, store, adapter, publication_id):
    validate(f)
    need(f.get('state') == 'core_ready' and f.get('pending') is None and 'mixedResources' not in f,
         'RESOURCE_NO_AUTOMATIC_REPLAY')
    need(len(f['seats']) == 100 and all('token' in s for s in f['seats'])
         and bool(f['expectedRequiredLogSourceIds']) and set(f['resources']) == {'classroomId'}, 'RESOURCE_CORE_INCOMPLETE')
    call = Calls(f, adapter)
    own_class(f, call)  # No mutations before exact synthetic audience checks.
    verify_publication(call, publication_id)  # Explicit existing PUBLIC read-only.
    recovery = copy.deepcopy(f)

    def persist(candidate):
        nonlocal recovery
        validate_resources(candidate)
        recovery = copy.deepcopy(candidate)
        f.clear()
        f.update(candidate)
        store.save(copy.deepcopy(candidate))

    def stage(name):
        candidate = copy.deepcopy(f)
        candidate['mixedResources'].update(state='running', pending=name)
        persist(candidate)  # Intent BEFORE every POST/PUT; never replayed.

    def receipt(**values):
        candidate = copy.deepcopy(f)
        candidate['mixedResources'].update(values)
        persist(candidate)

    initial = copy.deepcopy(f)
    initial['mixedResources'] = {'version': 1, 'state': 'intent', 'binding': binding(f),
                                 'pending': None, 'visibility': 'private',
                                 'publicationId': ident(publication_id),
                                 'requestIds': {k: str(uuid.uuid4()) for k in ('project', 'course', 'publish', 'run')}}
    # Refuse same-title existing resources before writing anything.
    rows = call('/api/courses').get('items')
    need(isinstance(rows, list) and not any(r.get('title') == title(f, 'course') for r in rows), 'RESOURCE_LABEL_ALREADY_EXISTS')
    try:
        persist(initial)
        cid = f['resources']['classroomId']
        stage('project')
        project_receipt = call('/api/projects', {'scope': 'classroom', 'classroomId': cid, 'module': 'three-d',
                                                'title': title(f, 'project')}, key=f['mixedResources']['requestIds']['project'])
        p = project_receipt.get('project', {})
        need(project_receipt.get('created') is True and p.get('scope') == 'classroom' and p.get('classroomId') == cid
             and p.get('moduleKey') == 'three-d' and p.get('title') == title(f, 'project'), 'RESOURCE_PROJECT_INVALID')
        receipt(classProjectId=ident(p.get('id')))
        stage('course')
        c = call('/api/courses', {'title': title(f, 'course'), 'summary': 'Synthetic QA acceptance material only',
                                 'visibility': 'private', 'requestId': f['mixedResources']['requestIds']['course']})
        receipt(courseId=ident(c.get('id')), draftRevision=c.get('draftRevision'))
        need(owned_course(f, call)['visibility'] == 'private', 'RESOURCE_COURSE_NOT_PRIVATE')
        course = f['mixedResources']['courseId']
        stage('section')
        section = call('/api/courses/' + course + '/sections', {'title': title(f, 'section'), 'expectedRevision': f['mixedResources']['draftRevision']})
        receipt(sectionId=ident(section.get('id')), draftRevision=section.get('draftRevision'))
        stage('lesson')
        lesson = call('/api/courses/' + course + '/lessons', {
            'sectionId': f['mixedResources']['sectionId'], 'title': title(f, 'material'), 'kind': 'material',
            'blocks': [{'id': 'qa-material', 'type': 'paragraph', 'text': 'Synthetic QA text. No student content or external links.'}],
            'expectedRevision': f['mixedResources']['draftRevision']})
        receipt(sourceLessonId=ident(lesson.get('id')), draftRevision=lesson.get('draftRevision'))
        stage('publish')
        version = call('/api/courses/' + course + '/publish', {'expectedRevision': f['mixedResources']['draftRevision'],
                                                              'requestId': f['mixedResources']['requestIds']['publish']})
        receipt(versionId=ident(version.get('versionId')), versionNumber=version.get('versionNumber'))
        # Whole-class audience is dynamic: verify the exact QA-only membership
        # again immediately before assignment; no invitations/sharing endpoints.
        own_class(f, call)
        stage('run')
        run = call('/api/classrooms/' + cid + '/course-runs', {
            'courseId': course, 'versionNumber': f['mixedResources']['versionNumber'], 'audienceType': 'whole_class',
            'seatIds': [], 'dueAt': None, 'requestId': f['mixedResources']['requestIds']['run'], 'timeZone': 'Europe/Moscow'})
        need(run.get('versionNumber') == f['mixedResources']['versionNumber'], 'RESOURCE_RUN_VERSION_INVALID')
        receipt(runId=ident(run.get('runId')))
        stage('public')
        owned_course(f, call)
        # Owner authorized this clearly labeled synthetic public course only.
        # A lost response stays UNKNOWN and requires settled manual cleanup.
        receipt(visibility='unknown')
        public = call('/api/sharing/course/' + course + '/visibility', {'visibility': 'public'}, method='PUT')
        need(public.get('ok') is True and owned_course(f, call)['visibility'] == 'public', 'RESOURCE_PUBLIC_WRITE_UNVERIFIED')
        receipt(visibility='public')
        stage('positives')
        opened = call('/api/projects/' + f['mixedResources']['classProjectId'])
        project = opened.get('project', {})
        need(project.get('id') == f['mixedResources']['classProjectId'] and project.get('classroomId') == cid
             and project.get('scope') == 'classroom' and isinstance(opened.get('draft', {}).get('document'), dict), 'RESOURCE_PROJECT_READBACK_FAILED')
        knowledge = call('/api/gallery/knowledge/' + course, user=0)
        need(knowledge.get('title') == title(f, 'course') and knowledge.get('versionNumber') == f['mixedResources']['versionNumber']
             and isinstance(knowledge.get('sections'), list) and bool(knowledge['sections']), 'RESOURCE_KNOWLEDGE_READBACK_FAILED')
        run_lesson = None
        for index in range(100):
            runs = call('/api/class-join/me/course-runs', user=index).get('items', [])
            matched = [r for r in runs if r.get('id') == f['mixedResources']['runId'] and r.get('courseId') == course]
            need(len(matched) == 1, 'RESOURCE_LEARNER_RUN_MISSING')
            lessons = [l for s in matched[0].get('sections', []) for l in s.get('lessons', [])
                       if l.get('sourceLessonId') == f['mixedResources']['sourceLessonId'] and l.get('kind') == 'material']
            need(len(lessons) == 1, 'RESOURCE_LEARNER_LESSON_MISSING')
            current = ident(lessons[0].get('id'))
            need(run_lesson is None or current == run_lesson, 'RESOURCE_LEARNER_LESSON_MISMATCH')
            run_lesson = current
        receipt(runLessonId=run_lesson)
        candidate = copy.deepcopy(f)
        m = candidate['mixedResources']
        candidate['resources'] = {'classroomId': cid, 'classProjectId': m['classProjectId'],
                                  'publicationId': m['publicationId'], 'publicCourseId': m['courseId'],
                                  'courseRunId': m['runId'], 'lessonId': m['runLessonId']}
        candidate['resourceEvidence'] = 'API_POSITIVE_PROJECTIONS_VERIFIED_NOT_LOAD_ACCEPTANCE'
        candidate['publicationEvidence'] = 'EXISTING_PUBLIC_READ_ONLY_VERIFIED'
        m.update(state='ready', pending=None)
        persist(candidate)
    except BaseException:
        # Reuse the reviewed core repair rule: cleanup first, independently of
        # receipt disk failure. No retries of unknown POSTs and no TTL extension.
        try:
            outcome = cleanup_visibility(recovery, adapter, settled=False)
        except BaseException:
            outcome = 'UNKNOWN_OPERATOR_CLEANUP_REQUIRED'
        failed = copy.deepcopy(recovery)
        if 'mixedResources' in failed:
            failed['mixedResources'].update(state='partial', visibility='unknown', cleanup=outcome)
        f.clear()
        f.update(failed)
        try:
            store.save(copy.deepcopy(failed))
        except BaseException:
            raise FixtureError('RESOURCE_PARTIAL_RECEIPT_FAILED') from None
        raise FixtureError('RESOURCE_PARTIAL_RETAINED') from None


def cleanup(f, store, adapter, *, attest_no_inflight=False):
    m = validate_resources(f)
    need(m['pending'] is None or attest_no_inflight, 'RESOURCE_INFLIGHT_NOT_SETTLED')
    # Cleanup write intent is independent of the ability to persist a receipt.
    outcome = cleanup_visibility(f, adapter, settled=True)
    candidate = copy.deepcopy(f)
    candidate['mixedResources'].update(state='cleaned', pending=None, cleanup=outcome,
                                        visibility='not_created' if outcome == 'NOT_CREATED_VERIFIED' else 'private')
    candidate['resourceEvidence'] = 'WITHDRAWN_FROM_MIXED_TEST_COURSE_PRIVATE'
    # Keep every created ID in the private intent, but make accidental mixed
    # reuse fail its fixture preflight after the public prerequisite is hidden.
    candidate['resources'] = {'classroomId': candidate['resources']['classroomId']}
    validate_resources(candidate)
    f.clear()
    f.update(candidate)
    store.save(copy.deepcopy(candidate))


def verify_source(root, revision):
    contract = json.loads((Path(__file__).resolve().parents[1] / 'profiles/school-mixed-resource-contract.json').read_text())
    need(contract.get('version') == 1 and contract.get('lessonKind') == 'material' and contract.get('dueAt') is None
         and contract.get('writes') == 'owned-synthetic-api-only'
         and isinstance(contract.get('sourceFiles'), dict) and len(contract['sourceFiles']) == 20, 'RESOURCE_SOURCE_CONTRACT_INVALID')
    for path, expected in contract['sourceFiles'].items():
        need(not Path(path).is_absolute() and '..' not in Path(path).parts and re.fullmatch('[a-f0-9]{64}', expected), 'RESOURCE_SOURCE_CONTRACT_INVALID')
        r = subprocess.run(['git', '-C', str(root), 'show', revision + ':' + path], capture_output=True, timeout=10)
        need(r.returncode == 0 and hashlib.sha256(r.stdout).hexdigest() == expected, 'RESOURCE_SOURCE_DRIFT')


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=('build', 'cleanup'), nargs='?', default='build')
    p.add_argument('--apply', action='store_true')
    for key in ('origin', 'revision', 'label'):
        p.add_argument('--' + key, required=True)
    p.add_argument('--schema', type=int, required=True)
    p.add_argument('--canonical-root')
    p.add_argument('--fixture')
    p.add_argument('--nonce')
    p.add_argument('--publication', help='Explicit existing PUBLIC UUID, read-only')
    p.add_argument('--attest-no-inflight', action='store_true')
    a = p.parse_args(argv)
    try:
        parameters(a.label, a.revision, a.schema, a.origin)
        if not a.apply:
            print(json.dumps({'state': 'PLAN_ONLY', 'action': a.action, 'createsPublicSyntheticCourse': a.action == 'build',
                              'publication': 'EXPLICIT_EXISTING_PUBLIC_READ_ONLY', 'maximumCalls': 160 if a.action == 'build' else 20,
                              'maximumWrites': 8 if a.action == 'build' else 1,
                              'fixtureReads': 0, 'networkRequests': 0, 'releaseAccepted': False}))
            return 0
        need(os.name == 'posix' and os.geteuid() == 0 and a.canonical_root and a.fixture, 'RESOURCE_SERVER_ROOT_REQUIRED')
        with PrivateStore(a.fixture) as store:
            f = store.read()
            need(f['nonce'] == a.nonce and (f['origin'], f['revision'], f['schema'], f['label']) ==
                 (a.origin, a.revision, a.schema, a.label), 'RESOURCE_EXACT_FIXTURE_REQUIRED')
            verify_source(a.canonical_root, a.revision)
            adapter = HostAdapter(a.canonical_root, f)
            if a.action == 'build':
                build(f, store, adapter, ident(a.publication))
            else:
                cleanup(f, store, adapter, attest_no_inflight=a.attest_no_inflight)
        print(json.dumps({'state': f['mixedResources']['state'], 'fixture': str(store.path), 'releaseAccepted': False}))
        return 0
    except (Exception, KeyboardInterrupt) as error:
        code = str(error) if isinstance(error, FixtureError) and re.fullmatch('[A-Z_]{1,100}', str(error)) else 'RESOURCE_STAGE_FAILED'
        print(json.dumps({'state': 'STOP', 'code': code, 'releaseAccepted': False}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
