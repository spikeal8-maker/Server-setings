#!/usr/bin/env python3
"""Opt-in, revision/schema-pinned HTTP acceptance of 20/50/100 synthetic pupils.

No credential provisioning, live update, deletion, TLS bypass or automatic retry.
Fixtures stay on the deployment host; reports contain no session/capability tokens.
"""
import argparse
import base64
import concurrent.futures
import copy
import hashlib
import json
import math
import multiprocessing
import os
from pathlib import Path
import ssl
import statistics
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zlib
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from release_load_contracts import (ContractError, PROFILES, READS, contract, field, require,
                                    private_fixture, quantiles, route_inventory, coverage,
                                    numeric_telemetry, safe_code, local_ai, required_log_coverage)
from release_load_ai import LocalAiWorkload

MODULES = ('three-d', 'electronics', 'blocks')


class CheckFailed(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Even a same-host redirect fails acceptance; never forward credentials.
        return None


def fixture_input(path, origin, users):
    try:
        return private_fixture(path, origin, users, 'core')
    except ContractError as e:
        raise CheckFailed(str(e)) from None


def metrics(values):
    values = sorted(values)
    return None if not values else {
        'count': len(values), 'minMs': values[0], 'medianMs': statistics.median(values),
        'p95Ms': values[math.ceil(len(values) * .95) - 1], 'maxMs': values[-1],
    }


def png_bytes(index):
    def chunk(kind, data):
        return struct.pack('!I', len(data)) + kind + data + struct.pack('!I', zlib.crc32(kind + data))
    raw = (b'\0' + bytes((index % 256, 80, 180, 255)) * 16) * 16
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!2I5B', 16, 16, 8, 6, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b'')


class Runner(LocalAiWorkload):
    def __init__(self, fixture, revision, users, max_p95_ms=5000, pace_ms=0, *, schema=None,
                 profile='core', catalog=None, partial=None, p99_ms=10000, ai_texts=0, ai_images=0, cancel_event=None,
                 ai_first_token_ms=120000, ai_total_ms=240000, inventory_verified=False):
        self.f = fixture
        self.origin = fixture['origin']
        self.revision = revision
        self.users = users
        self.max_p95_ms = max_p95_ms
        self.pace_ms = pace_ms
        self.schema = schema
        self.profile = profile
        self.catalog = catalog or route_inventory()
        self.partial = partial
        self.p99_ms = p99_ms
        self.ai_texts, self.ai_images = ai_texts, ai_images
        self.ai_first_token_ms, self.ai_total_ms = ai_first_token_ms, ai_total_ms
        self.inventory_verified = inventory_verified
        self.cancel_event = cancel_event or threading.Event()
        self.samples = []
        self.results = []
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.halt_new = threading.Event()
        self.checks = set()
        self.telemetry = []
        self.ai_results = []
        self.actual_seats = []
        self.provenance = []
        self.inflight = {}
        self.last_checkpoint = 0.0
        self.ssl = ssl.create_default_context()
        self.opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=self.ssl), NoRedirect())

    def headers(self, user, bearer=None):
        headers = {'Origin': self.origin, 'User-Agent': 'ASA-Release-Load/2', 'Idempotency-Key': str(uuid.uuid4())}
        if isinstance(user, int):
            headers['Cookie'] = 'asa_student_session=' + self.f['seats'][user]['token']
        elif user in ('teacher', 'admin'):
            headers['Cookie'] = 'asa_session=' + self.f['roles'][user]['token']
        if bearer:
            headers['Authorization'] = 'Bearer ' + bearer
        return headers

    def sample(self, row):
        with self.lock:
            self.samples.append(row)
        self.checkpoint()

    def checkpoint(self, force=False):
        if self.partial is None:
            return
        with self.lock:
            now = time.monotonic()
            if not force and now - self.last_checkpoint < 1:
                return
            self.last_checkpoint = now
            report = {'format': 'asa-release-http-load-v2', 'passed': False, 'state': 'running',
                      'revision': self.revision, 'schema': self.schema, 'users': self.users,
                      'requests': list(self.samples), 'journeys': list(self.results), 'ai': list(self.ai_results),
                      'telemetry': list(self.telemetry), 'inFlight': list(self.inflight.values()), 'releaseAccepted': False}
            write_receipt(self.partial, report)

    def call(self, user, path, body=None, method=None, bearer=None, binary=None, expected=(200,), label='request', cleanup=False):
        if not path.startswith('/') or path.startswith('//'):
            raise CheckFailed('relative application path required')
        if (self.halt_new.is_set() or self.cancel_event.is_set()) and user is not None and not cleanup:
            raise CheckFailed('new_work_stopped_after_failure')
        headers = self.headers(user, bearer)
        data = binary
        if body is not None:
            headers['Content-Type'] = 'application/json'
            data = json.dumps(body, ensure_ascii=False).encode()
        elif binary is not None:
            headers['Content-Type'] = 'image/svg+xml'
        req = urllib.request.Request(self.origin + path, data=data, method=method, headers=headers)
        if user is not None and self.pace_ms and label != 'identity' and not cleanup:
            if self.halt_new.wait(self.pace_ms / 1000):
                raise CheckFailed('new_work_stopped_after_failure')
        started = time.monotonic()
        status, raw, problem = None, b'', None
        try:
            with self.opener.open(req, timeout=30) as response:
                status, raw = response.status, response.read(8 * 1024 * 1024 + 1)
                if len(raw) > 8 * 1024 * 1024:
                    raise CheckFailed('response exceeds test bound')
        except urllib.error.HTTPError as e:
            status = e.code
            try:
                raw = e.read(65536)
            except Exception as read_error:
                problem = type(read_error).__name__
        except Exception as e:
            problem = type(e).__name__
        elapsed = round((time.monotonic() - started) * 1000, 2)
        try:
            parsed = json.loads(raw)
        except (ValueError, UnicodeError):
            parsed = None
        code = safe_code(parsed.get('error', {}).get('code')) if isinstance(parsed, dict) and isinstance(parsed.get('error'), dict) else None
        ok = status in expected and not problem
        self.sample({'label': label, 'actor': user + 1 if isinstance(user, int) else user or 'guest',
                     'status': status, 'ms': elapsed, 'ok': ok, 'expectedNegative': all(s >= 400 for s in expected),
                     'error': problem or code, 'bytes': len(raw)})
        if problem or status not in expected:
            if not cleanup:
                self.halt_new.set()
            raise CheckFailed(f'{label}: HTTP {status}, {problem or code or "non_json_error"}')
        return parsed if parsed is not None else raw

    def assert_revision(self):
        ready = self.call(None, '/health/ready', label='revision')
        contract(ready, {'status': 'string', 'deployment.revision': 'string', 'deployment.schemaVersion': 'int',
                         'deployment.synchronized': 'bool'})
        if (ready['status'] != 'ready' or ready['deployment']['revision'] != self.revision
                or ready['deployment']['schemaVersion'] != self.schema or ready['deployment']['synchronized'] is not True):
            raise CheckFailed('running revision differs from explicitly pinned revision')
        self.provenance.append({'revision': ready['deployment']['revision'], 'schema': ready['deployment']['schemaVersion']})

    def health(self):
        while not self.stop.is_set():
            try:
                self.call(None, '/health/ready', label='health')
                counters = numeric_telemetry(self.call(None, '/health/metrics', label='runtime-metrics'))
                with self.lock:
                    self.telemetry.append(counters)
            except CheckFailed:
                pass
            except ContractError:
                self.halt_new.set()
                self.sample({'label': 'runtime-metrics-contract', 'ok': False, 'expectedNegative': False,
                             'error': 'telemetry_incomplete', 'ms': 0, 'status': None})
            except Exception:
                self.halt_new.set()
                self.sample({'label': 'runtime-metrics-contract', 'ok': False, 'expectedNegative': False,
                             'error': 'telemetry_sampling_failed', 'ms': 0, 'status': None})
            self.stop.wait(1)

    def journey(self, user, module, barrier):
        result = {'user': user + 1, 'module': module, 'ok': False}
        barrier.wait(timeout=60)
        started = time.monotonic()
        try:
            self.call(user, '/api/projects', label='list')
            created = self.call(user, '/api/projects', {'scope': 'personal', 'classroomId': None,
                                     'module': module, 'title': f'RELEASE {self.users} TEST {module} {user+1}'},
                                expected=(201,), label='create')
            pid = created['project']['id']
            result['projectId'] = pid
            with self.lock:
                self.inflight[f'project:{module}:{user}'] = {'user': user + 1, 'module': module, 'projectId': pid}
            opened = self.call(user, '/api/projects/' + pid, label='open')
            draft = opened['draft']
            doc = copy.deepcopy(self.f.get('documents', {}).get(module, draft['document']))
            bearer = None
            asset_roundtrip = None
            if module == 'three-d':
                doc['grid']['width'] = 220 + user
            elif module == 'electronics':
                doc['viewport']['x'] = user + 1
            else:
                grant = self.call(user, f'/api/projects/{pid}/blocks/runtime-session', {}, label='scratch-session')
                if grant['runtimeOrigin'] != self.origin:
                    raise CheckFailed('Scratch runtime origin differs from application origin')
                bearer = grant['runtimeToken']
                svg = f'<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40"><rect width="{20+user%20}" height="30" fill="#2277bb"/></svg>'.encode()
                asset_id = hashlib.md5(svg).hexdigest()
                path = f'/api/blocks/runtime/projects/{pid}/assets/{asset_id}.svg'
                upload = self.call(user, path, method='PUT', bearer=bearer, binary=svg, label='scratch-upload')
                asset_roundtrip = (path, svg)
                doc = {'schemaVersion': 1, 'format': 'scratch-3', 'assets': [upload['asset']],
                       'projectJson': {'targets': [{'isStage': True, 'name': 'Stage', 'variables': {'qa': ['QA', user+1]},
                         'lists': {}, 'broadcasts': {}, 'blocks': {}, 'comments': {}, 'currentCostume': 0,
                         'costumes': [{'assetId': asset_id, 'dataFormat': 'svg', 'name': 'QA', 'md5ext': asset_id+'.svg',
                                       'bitmapResolution': 1, 'rotationCenterX': 20, 'rotationCenterY': 20}],
                         'sounds': [], 'volume': 100, 'layerOrder': 0, 'tempo': 60, 'videoTransparency': 50,
                         'videoState': 'on', 'textToSpeechLanguage': None}], 'monitors': [], 'extensions': [],
                         'meta': {'semver': '3.0.0', 'vm': '15.1.1', 'agent': 'ASA release acceptance'}}}
            route = f'/api/blocks/runtime/projects/{pid}/draft' if module == 'blocks' else f'/api/projects/{pid}/draft'
            saved = self.call(user, route, {'document': doc, 'baseRevision': draft['revision'], 'mutationId': str(uuid.uuid4())},
                              method='PUT', bearer=bearer, label='save')
            # Newly uploaded Scratch assets become readable only after a durable draft references them.
            if asset_roundtrip:
                path, svg = asset_roundtrip
                if self.call(user, path, bearer=bearer, label='scratch-read') != svg:
                    raise CheckFailed('Scratch media bytes differ after committed storage round trip')
            reopened = self.call(user, '/api/projects/' + pid, label='reopen')
            if reopened['draft']['document'] != doc or reopened['draft']['revision'] <= draft['revision']:
                raise CheckFailed('saved document or revision missing after reopen')
            image = png_bytes(user)
            self.call(user, f'/api/projects/{pid}/snapshot', {'imageDataUrl': 'data:image/png;base64,' + base64.b64encode(image).decode(),
                      'sourceRevision': reopened['draft']['revision']}, method='PUT', label='snapshot-save')
            if self.call(user, f'/api/projects/{pid}/snapshot', label='snapshot-read') != image:
                raise CheckFailed('snapshot bytes differ')
            self.call((user+1) % self.users, '/api/projects/' + pid, expected=(403, 404), label='isolation-denial')
            if user == 0:
                duplicate = self.call(user, f'/api/projects/{pid}/duplicate', {'title': 'RELEASE TEST COPY'}, expected=(201,), label='duplicate')
                copy_id = duplicate['project']['id']
                self.call(user, f'/api/projects/{copy_id}', {'title': 'RELEASE TEST RENAMED'}, method='PATCH', label='rename')
                for status in ('archived', 'active', 'trashed', 'active'):
                    changed = self.call(user, f'/api/projects/{copy_id}/status', {'status': status}, label='lifecycle-' + status)
                    require(changed['project']['status'] == status, 'project lifecycle status not preserved')
                copy_open = self.call(user, '/api/projects/' + copy_id, label='duplicate-reopen')
                require(copy_open['draft']['document'] == doc, 'duplicate/lifecycle lost document')
            result.update(ok=True, projectId=pid, revision=reopened['draft']['revision'])
        except Exception as e:
            # Never include transport objects, fixture tokens or full response bodies.
            result['error'] = str(e)[:180] if isinstance(e, CheckFailed) else type(e).__name__
            self.halt_new.set()
        result['ms'] = round((time.monotonic() - started) * 1000, 2)
        with self.lock:
            self.results.append(result)
            self.inflight.pop(f'project:{module}:{user}', None)
        self.checkpoint()
        return result

    def identities(self):
        self.assert_revision()
        for user in range(self.users):
            who = self.call(user, '/api/class-join/me', label='identity')
            contract(who, {'authenticated': 'bool', 'student.seatId': 'string', 'classroom.id': 'string'})
            require(who['authenticated'] is True and who['student']['seatId'] == self.f['seats'][user]['seat'],
                    'actual student session differs from synthetic fixture')
            if self.profile == 'mixed':
                require(who['classroom']['id'] == self.f['resources']['classroomId'], 'pupil is outside the test class')
            self.actual_seats.append(who['student']['seatId'])
        require(len(set(self.actual_seats)) == self.users, 'actual pupils are not distinct')
        self.checks.add('seat-identity')
        if self.profile == 'mixed':
            for role in ('teacher', 'admin'):
                who = self.call(role, '/api/auth/me', label='identity-' + role)
                contract(who, {'authenticated': 'bool', 'account.id': 'string', 'capabilities': 'list'})
                require(who['authenticated'] and who['account']['id'] == self.f['roles'][role]['accountId'], 'wrong synthetic Account role')
                if role == 'teacher':
                    require(any(c.get('capability') == 'educator' and c.get('state') in ('verified', 'provisional') for c in who['capabilities']), 'teacher educator capability missing')
            require(self.f['roles']['teacher']['accountId'] != self.f['roles']['admin']['accountId'], 'separate teacher/admin required')
            profile = self.call('admin', '/api/admin/v1/me', label='identity-admin-scope')
            contract(profile, {'administrator': 'bool', 'scopes': 'list'})
            require(profile['administrator'] and any(v.get('kind') == 'platform' and v.get('role') == 'platform_admin' for v in profile['scopes']), 'platform-admin fixture required')
        self.checkpoint(True)

    def probe(self, key, user=None):
        role, template, fields = READS[key]
        actor = user if isinstance(role, int) and user is not None else role
        path = template.format(**self.f.get('resources', {}))
        data = self.call(actor, path, label='portal-' + key)
        contract(data, fields)
        resources = self.f.get('resources', {})
        if key == 'publication':
            require(data['publication']['publicationId'] == resources['publicationId'], 'wrong publication projection')
            publication = data['publication']
            artifact = self.call(actor, f"/api/public-projects/{publication['publicationId']}/artifact?revisionId={publication['revisionId']}", label='publication-artifact')
            contract(artifact, {'artifact.schemaVersion': 'int', 'artifact.status': 'string', 'artifact.provenance.publicationId': 'string',
                                'artifact.provenance.revisionNumber': 'int', 'artifact.capabilities.readOnly': 'bool'})
            require(artifact['artifact']['provenance'] == {'publicationId': resources['publicationId'], 'revisionNumber': publication['revisionNo']}
                    and artifact['artifact']['capabilities']['readOnly'] is True
                    and artifact['artifact']['status'] in ('display', 'simulation', 'static-only'), 'public artifact identity/visibility unsupported')
        elif key == 'classroom':
            require(data['classroom']['id'] == resources['classroomId'], 'wrong test classroom')
        elif key == 'roster':
            require(set(self.actual_seats) <= {v.get('id') for v in data['items']}, 'teacher roster does not contain test pupils')
        elif key == 'class-project-list':
            require(any(v.get('id') == resources['classProjectId'] for v in data['items']), 'synthetic classroom project missing')
        elif key == 'class-editor-specific':
            require(data['project']['id'] == resources['classProjectId'] and data['project']['classroomId'] == resources['classroomId']
                    and data['project']['scope'] == 'classroom', 'classroom project projection is not the supplied classroom work')
        elif key == 'learning':
            course = next((v for v in data['items'] if v.get('id') == resources['courseRunId']), None)
            require(course is not None, 'assigned synthetic course run missing')
            lessons = [v for section in course.get('sections', []) for v in section.get('lessons', [])]
            require(any(v.get('id') == resources['lessonId'] for v in lessons), 'synthetic lesson missing from learner course projection')
        elif key == 'knowledge-course':
            require(bool(data['sections']) and data['versionNumber'] > 0, 'published course outline is empty')
        elif key == 'games':
            require(data['granted'] is True, 'synthetic pupil games admission is disabled')
        elif key == 'admin-operations':
            require(data['build']['revision'] == self.revision and int(data['migration']['version']) == self.schema
                    and data['services'] == {'api': 'responding', 'database': 'responding'}, 'admin operations provenance mismatch')
        elif key == 'admin-logs':
            required_log_coverage(data, self.f['expectedRequiredLogSourceIds'])
        with self.lock:
            self.checks.add(key)
        return data

    def mixed_pupil(self, user, barrier):
        barrier.wait(timeout=60)
        started = time.monotonic()
        groups = [('modules', 'games'), ('learning', 'assignments', 'results'), ('knowledge', 'knowledge-course'),
                  ('collections', 'public-list', 'publication'), ('ai-status', 'modules')]
        keys = groups[user % len(groups)]
        result = {'user': user + 1, 'module': 'mixed-portal', 'checks': keys, 'ok': False}
        try:
            for key in keys:
                self.probe(key, user)
            result['ok'] = True
        except Exception as e:
            result['error'] = str(e)[:180] if isinstance(e, (CheckFailed, ContractError)) else type(e).__name__
            self.halt_new.set()
        result['ms'] = round((time.monotonic() - started) * 1000, 2)
        with self.lock:
            self.results.append(result)
        self.checkpoint()
        return result

    def wave(self, tasks):
        if self.halt_new.is_set():
            return
        barrier = threading.Barrier(len(tasks))
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(tasks)) as pool:
            jobs = [pool.submit(fn, *args, barrier) for fn, args in tasks]
            for job in concurrent.futures.as_completed(jobs):
                job.result()
        self.checkpoint(True)

    def report(self, fatal=None):
        expected = self.users * (4 if self.profile == 'mixed' else 3)
        by_operation = {label: quantiles([s['ms'] for s in self.samples if s['label'] == label and s['ok'] and not s['expectedNegative']])
                        for label in sorted({s['label'] for s in self.samples})}
        positives = [s for s in self.samples if not s['expectedNegative']]
        unexpected = [s for s in self.samples if not s['ok']]
        ai_latency = {f'{kind}.{name}': quantiles([r[name] for r in self.ai_results if r['kind'] == kind and r.get(name) is not None])
                      for kind, names in [('text', ('ms', 'firstByteMs', 'firstTokenMs')), ('image', ('ms', 'admitMs'))] for name in names}
        ai_budget_ok = all(not stats or stats['p99Ms'] <= (self.max_p95_ms if metric.endswith('firstByteMs') else
                              self.ai_first_token_ms if metric.endswith('firstTokenMs') else self.ai_total_ms)
                           for metric, stats in ai_latency.items())
        passed = (fatal is None and len(self.results) == expected and all(x['ok'] for x in self.results)
                  and not unexpected and bool(self.telemetry) and len(self.provenance) >= 2
                  and all(m['p95Ms'] <= self.max_p95_ms and m['p99Ms'] <= self.p99_ms for m in by_operation.values() if m)
                  and len(self.ai_results) == self.ai_texts + self.ai_images and all(x.get('ok') for x in self.ai_results) and ai_budget_ok)
        return {'format': 'asa-release-http-load-v2', 'passed': passed, 'httpProfilePassed': passed, 'releaseAccepted': False,
                'revision': self.revision, 'schema': self.schema, 'provenance': self.provenance,
                'users': self.users, 'profile': self.profile, 'paceMs': self.pace_ms, 'state': 'failed' if fatal else 'complete',
                'error': fatal, 'scope': 'HTTP data contracts and synthetic project/media persistence; no browser execution',
                'actualDistinctStudents': len(set(self.actual_seats)), 'operationLatency': by_operation,
                'requestLatency': quantiles([s['ms'] for s in positives if s['ok']]),
                'unexpectedErrors': len(unexpected), 'expectedNegativeChecks': sum(s['expectedNegative'] for s in self.samples),
                'maxP95Ms': self.max_p95_ms, 'maxP99Ms': self.p99_ms,
                'modules': {m: {'journeys': sum(x['module'] == m for x in self.results),
                               'passed': sum(x['module'] == m and x['ok'] for x in self.results)} for m in (*MODULES, 'mixed-portal')},
                'sourceInventoryRevision': self.catalog['sourceRevision'], 'sourceInventoryVerified': self.inventory_verified,
                'routeCoverage': coverage(self.catalog, self.checks), 'aiLatency': ai_latency,
                'aiWorkloadState': 'NOT_RUN' if not (self.ai_texts or self.ai_images) else 'PASS' if len(self.ai_results) == self.ai_texts + self.ai_images and all(x.get('ok') for x in self.ai_results) and ai_budget_ok else 'FAIL',
                'aiBudgets': {'firstTokenMs': self.ai_first_token_ms, 'totalMs': self.ai_total_ms, 'firstByteMs': self.max_p95_ms},
                'aiConfiguration': {'textUsers': self.ai_texts, 'images': self.ai_images},
                'errorCountsByOperation': {label: sum(s['label'] == label and not s['ok'] for s in self.samples) for label in by_operation},
                'separateEvidence': {layer: {'state': 'NOT_RUN', 'reference': self.f.get('evidenceReferences', {}).get(layer)}
                                     for layer in self.catalog['notRunLayers']},
                'ai': self.ai_results, 'telemetry': self.telemetry, 'inFlight': list(self.inflight.values()),
                'journeys': self.results, 'requests': self.samples}

    def run(self):
        self.identities()
        monitor = threading.Thread(target=self.health, daemon=True)
        monitor.start()
        try:
            for module in MODULES:
                self.wave([(self.journey, (user, module)) for user in range(self.users)])
                rows = [x for x in self.results if x['module'] == module]
                if len(rows) == self.users and all(x['ok'] for x in rows):
                    self.checks.add('project-' + module)
                if self.halt_new.is_set():
                    break
            if self.profile == 'mixed' and not self.halt_new.is_set():
                for key in READS:
                    self.probe(key)
                for actor in (None, 0, 'teacher'):
                    self.call(actor, '/api/admin/v1/me', expected=(401, 403), label='admin-negative')
                if self.ai_texts or self.ai_images:
                    self.prepare_local_ai()
                tasks = [(self.mixed_pupil, (user,)) for user in range(self.users)]
                tasks += [(self.ai_text, (i,)) for i in range(self.ai_texts)]
                tasks += [(self.ai_image, (i,)) for i in range(self.ai_images)]
                self.wave(tasks)
            self.assert_revision()
        finally:
            self.stop.set()
            monitor.join(35)
        return self.report()


def worker(send, fixture, revision, users, threshold, pace_ms, options):
    runner = Runner(fixture, revision, users, threshold, pace_ms, **options)
    try:
        report = runner.run()
    except Exception as e:
        fatal = str(e)[:180] if isinstance(e, (CheckFailed, ContractError)) else type(e).__name__
        report = runner.report(fatal)
    try:
        # Publish the complete bounded evidence atomically; IPC carries only a
        # tiny acknowledgement, so recv cannot wait for a multi-MB report body.
        write_receipt(runner.partial, report)
        send.send(True)
    finally:
        send.close()


def write_receipt(path, report, exclusive=False):
    temporary = path if exclusive else path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as out:
        json.dump(report, out, ensure_ascii=False, indent=2)
        out.flush()
        os.fsync(out.fileno())
    if not exclusive:
        os.replace(temporary, path)


def supervise(child, receive, cancel, deadline, report, partial):
    """Bound wall time; keep partial failure evidence if a worker cannot finish."""
    try:
        if receive.poll(deadline):
            try:
                acknowledged = receive.recv()
                require(acknowledged is True, 'invalid worker acknowledgement')
                completed = json.loads(partial.read_text(encoding='utf-8'))
                require(completed.get('format') == 'asa-release-http-load-v2'
                        and completed.get('revision') == report['revision']
                        and completed.get('users') == report['users'], 'invalid worker receipt identity')
                return completed
            except EOFError:
                report.update(state='failed', error='worker_exited_without_receipt')
        else:
            report.update(state='failed', error='wall_time_deadline_exceeded')
    finally:
        cancel.set()
        child.join(2)
        if child.is_alive():
            child.terminate()
            child.join(3)
        if child.is_alive():
            child.kill()
            child.join(3)
        receive.close()
    if partial.exists():
        try:
            observed = json.loads(partial.read_text(encoding='utf-8'))
            if observed.get('revision') == report['revision'] and observed.get('users') == report['users']:
                observed.update(passed=False, httpProfilePassed=False, releaseAccepted=False, state=report['state'], error=report['error'],
                                inFlightCancellation='BEST_EFFORT_NOT_VERIFIED; inspect own synthetic requests before repeating')
                return observed
        except (OSError, ValueError):
            pass
    return report


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--origin', required=True)
    p.add_argument('--revision', required=True)
    p.add_argument('--schema', type=int)
    p.add_argument('--fixture', type=Path)
    p.add_argument('--output', type=Path)
    p.add_argument('--users', type=int, default=50, choices=PROFILES)
    p.add_argument('--profile', choices=('core', 'mixed'), default='core')
    p.add_argument('--source-root', type=Path, help='Clean ASA checkout at runtime revision, required for mixed apply')
    p.add_argument('--previous-receipt', type=Path, help='Successful 20/50 step of this same revision/schema/profile/pace')
    p.add_argument('--max-p95-ms', type=int, default=5000)
    p.add_argument('--max-p99-ms', type=int, default=10000)
    p.add_argument('--pace-ms', type=int, default=0)
    p.add_argument('--deadline-seconds', type=int, default=600)
    p.add_argument('--ai-text-users', type=int, default=0, help='Explicit local-only subset, 0..users; paid default is rejected')
    p.add_argument('--ai-images', type=int, default=0, help='Explicit ComfyUI subset, 0..10; last job tests cancel then one new retry')
    p.add_argument('--ai-max-first-token-ms', type=int, default=120000)
    p.add_argument('--ai-max-total-ms', type=int, default=240000)
    p.add_argument('--apply', action='store_true')
    args = p.parse_args(argv)
    u = urllib.parse.urlsplit(args.origin)
    if u.scheme != 'https' or not u.hostname or u.username or u.password or u.query or u.fragment or u.path:
        p.error('an exact HTTPS application origin without path/credentials is required')
    if len(args.revision) != 40 or any(c not in '0123456789abcdef' for c in args.revision):
        p.error('pin a full lowercase Git revision')
    if not 0 < args.max_p95_ms <= args.max_p99_ms or not 0 <= args.pace_ms <= 5000 or not 30 <= args.deadline_seconds <= 900:
        p.error('invalid latency/pace/deadline bounds')
    if not 0 <= args.ai_text_users <= args.users or not 0 <= args.ai_images <= min(10, args.users):
        p.error('AI workload exceeds explicit local bounds')
    if (args.ai_text_users or args.ai_images) and args.profile != 'mixed':
        p.error('AI requires mixed profile and separate synthetic role fixture')
    if not 0 < args.ai_max_first_token_ms <= args.ai_max_total_ms <= 240000:
        p.error('AI time budgets must be positive and bounded by 240 seconds')
    if not args.apply:
        print(json.dumps({'mode': 'plan', 'networkRequests': 0, 'fixtureReads': 0, 'users': args.users,
                          'profile': args.profile, 'paceMs': args.pace_ms, 'modules': MODULES,
                          'minimumSyntheticProjects': args.users * 3, 'maximumSyntheticProjects': args.users * 3 + 3,
                          'revision': args.revision, 'schema': args.schema,
                          'aiTexts': args.ai_text_users, 'maxNewImageJobs': args.ai_images + int(args.ai_images > 0),
                          'progression': [20, 50, 100], 'releaseAccepted': False,
                          'requires': 'private synthetic fixture; mixed also needs exact source inventory and teacher/admin/resources'}))
        return 0
    if not args.fixture or not args.output or not args.schema or args.schema < 1:
        p.error('--apply requires --fixture, --output and positive --schema')
    partial = args.output.with_name(args.output.name + '.partial.json')
    if args.fixture.resolve() in (args.output.resolve(), partial.resolve()) or args.output.exists() or partial.exists():
        p.error('output must be new and must not overwrite a fixture or earlier receipt')
    if args.profile == 'mixed' and args.source_root is None:
        p.error('mixed apply requires --source-root for route/API drift detection')
    fixture = private_fixture(args.fixture, args.origin, args.users, args.profile)
    catalog = route_inventory(args.source_root, args.revision) if args.source_root else route_inventory()
    previous_hash = None
    if args.users != 20:
        if not args.previous_receipt:
            p.error('50/100 apply requires prior successful 20/50 receipt; no blind escalation')
        previous_raw = args.previous_receipt.read_bytes()
        previous = json.loads(previous_raw)
        expected_users = 20 if args.users == 50 else 50
        require(previous.get('passed') is True and previous.get('users') == expected_users
                and previous.get('format') == 'asa-release-http-load-v2'
                and previous.get('actualDistinctStudents') == expected_users
                and previous.get('revision') == args.revision and previous.get('schema') == args.schema
                and previous.get('profile') == args.profile and previous.get('paceMs') == args.pace_ms
                and previous.get('maxP95Ms') == args.max_p95_ms and previous.get('maxP99Ms') == args.max_p99_ms
                and previous.get('aiWorkloadState') == ('PASS' if args.ai_text_users or args.ai_images else 'NOT_RUN'),
                'previous step differs or failed; no escalation')
        previous_hash = hashlib.sha256(previous_raw).hexdigest()
    report = {'format': 'asa-release-http-load-v2', 'revision': args.revision, 'schema': args.schema, 'users': args.users,
              'profile': args.profile, 'passed': False, 'state': 'running', 'deadlineSeconds': args.deadline_seconds,
              'releaseAccepted': False, 'previousReceiptSha256': previous_hash}
    write_receipt(args.output, report, exclusive=True)
    write_receipt(partial, report, exclusive=True)
    context = multiprocessing.get_context('spawn')
    receive, send = context.Pipe(duplex=False)
    cancel = context.Event()
    options = {'schema': args.schema, 'profile': args.profile, 'catalog': catalog, 'partial': partial,
               'p99_ms': args.max_p99_ms, 'ai_texts': args.ai_text_users, 'ai_images': args.ai_images, 'cancel_event': cancel,
               'ai_first_token_ms': args.ai_max_first_token_ms, 'ai_total_ms': args.ai_max_total_ms,
               'inventory_verified': args.source_root is not None}
    child = context.Process(target=worker, args=(send, fixture, args.revision, args.users, args.max_p95_ms, args.pace_ms, options))
    try:
        child.start()
        send.close()
        report = supervise(child, receive, cancel, args.deadline_seconds, report, partial)
    except BaseException:
        cancel.set()
        if child.pid is not None and child.is_alive():
            child.terminate()
            child.join(5)
        report.update(passed=False, state='failed', error='worker_interrupted')
        raise
    finally:
        report.update(previousReceiptSha256=previous_hash, deadlineSeconds=args.deadline_seconds)
        write_receipt(args.output, report)
    print(json.dumps({k: v for k, v in report.items() if k not in ('journeys', 'requests', 'routeCoverage', 'telemetry', 'ai')}))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (CheckFailed, ContractError, OSError, ValueError) as e:
        print('BLOCKED ' + (str(e) if isinstance(e, (CheckFailed, ContractError)) else type(e).__name__))
        raise SystemExit(2)
