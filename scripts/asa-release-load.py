#!/usr/bin/env python3
"""Opt-in, revision-pinned acceptance of 50 isolated synthetic project users.

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

MODULES = ('three-d', 'electronics', 'blocks')


class CheckFailed(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Even a same-host redirect fails acceptance; never forward credentials.
        return None


def fixture_input(path, origin, users):
    path = Path(path)
    if os.name != 'nt' and path.stat().st_mode & 0o077:
        raise CheckFailed('fixture must have mode 0600')
    f = json.loads(path.read_text(encoding='utf-8'))
    seats = f.get('seats', [])
    if f.get('authorizedSyntheticFixture') is not True or f.get('origin') != origin:
        raise CheckFailed('explicit synthetic fixture and exact origin required')
    if len(seats) < users or len({s.get('seat') for s in seats[:users]}) != users:
        raise CheckFailed('distinct synthetic seats required')
    if any(not isinstance(s.get('token'), str) or len(s['token']) < 32 for s in seats[:users]):
        raise CheckFailed('fixture session missing')
    if len({s['token'] for s in seats[:users]}) != users:
        raise CheckFailed('distinct synthetic sessions required')
    return f


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


class Runner:
    def __init__(self, fixture, revision, users, max_p95_ms=5000, pace_ms=0):
        self.f = fixture
        self.origin = fixture['origin']
        self.revision = revision
        self.users = users
        self.max_p95_ms = max_p95_ms
        self.pace_ms = pace_ms
        self.samples = []
        self.results = []
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.ssl = ssl.create_default_context()
        self.opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=self.ssl), NoRedirect())

    def call(self, user, path, body=None, method=None, bearer=None, binary=None, expected=(200,), label='request'):
        if not path.startswith('/') or path.startswith('//'):
            raise CheckFailed('relative application path required')
        headers = {'Origin': self.origin, 'User-Agent': 'ASA-Release-50-Acceptance/1.0', 'Idempotency-Key': str(uuid.uuid4())}
        if user is not None:
            headers['Cookie'] = 'asa_student_session=' + self.f['seats'][user]['token']
        if bearer:
            headers['Authorization'] = 'Bearer ' + bearer
        data = binary
        if body is not None:
            headers['Content-Type'] = 'application/json'
            data = json.dumps(body, ensure_ascii=False).encode()
        elif binary is not None:
            headers['Content-Type'] = 'image/svg+xml'
        req = urllib.request.Request(self.origin + path, data=data, method=method, headers=headers)
        if user is not None and self.pace_ms and label != 'identity':
            time.sleep(self.pace_ms / 1000)
        started = time.monotonic()
        status, raw, problem = None, b'', None
        try:
            with self.opener.open(req, timeout=30) as response:
                status, raw = response.status, response.read(8 * 1024 * 1024 + 1)
                if len(raw) > 8 * 1024 * 1024:
                    raise CheckFailed('response exceeds test bound')
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read(65536)
        except Exception as e:
            problem = type(e).__name__
        elapsed = round((time.monotonic() - started) * 1000, 2)
        try:
            parsed = json.loads(raw)
        except (ValueError, UnicodeError):
            parsed = None
        code = parsed.get('error', {}).get('code') if isinstance(parsed, dict) else None
        with self.lock:
            self.samples.append({'label': label, 'user': None if user is None else user + 1,
                                 'status': status, 'ms': elapsed, 'ok': status in expected and not problem,
                                 'error': problem or code, 'bytes': len(raw)})
        if problem or status not in expected:
            raise CheckFailed(f'{label}: HTTP {status}, {problem or code or "non_json_error"}')
        return parsed if parsed is not None else raw

    def assert_revision(self):
        ready = self.call(None, '/health/ready', label='revision')
        if ready.get('deployment', {}).get('revision') != self.revision:
            raise CheckFailed('running revision differs from explicitly pinned revision')

    def health(self):
        while not self.stop.is_set():
            try:
                self.call(None, '/health/ready', label='health')
            except CheckFailed:
                pass
            self.stop.wait(1)

    def journey(self, user, module, barrier):
        result = {'user': user + 1, 'module': module, 'ok': False}
        barrier.wait(timeout=60)
        started = time.monotonic()
        try:
            self.call(user, '/api/projects', label='list')
            created = self.call(user, '/api/projects', {'scope': 'personal', 'classroomId': None,
                                     'module': module, 'title': f'RELEASE 50 TEST {module} {user+1}'},
                                expected=(201,), label='create')
            pid = created['project']['id']
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
            result.update(ok=True, projectId=pid, revision=reopened['draft']['revision'])
        except Exception as e:
            # Never include transport objects, fixture tokens or full response bodies.
            result['error'] = str(e)[:180] if isinstance(e, CheckFailed) else type(e).__name__
        result['ms'] = round((time.monotonic() - started) * 1000, 2)
        with self.lock:
            self.results.append(result)
        return result

    def run(self):
        self.assert_revision()
        actual_seats = []
        for user in range(self.users):
            who = self.call(user, '/api/class-join/me', label='identity')
            actual = who.get('student', {}).get('seatId')
            if who.get('authenticated') is not True or actual != self.f['seats'][user]['seat']:
                raise CheckFailed('actual student session differs from synthetic fixture')
            actual_seats.append(actual)
        if len(set(actual_seats)) != self.users:
            raise CheckFailed('actual student sessions are not distinct')
        monitor = threading.Thread(target=self.health, daemon=True)
        monitor.start()
        try:
            # Every synthetic pupil completes all three modules; each wave has 50 concurrent journeys.
            for module in MODULES:
                barrier = threading.Barrier(self.users)
                with concurrent.futures.ThreadPoolExecutor(max_workers=self.users) as pool:
                    jobs = [pool.submit(self.journey, user, module, barrier) for user in range(self.users)]
                    for job in concurrent.futures.as_completed(jobs):
                        job.result()
            self.assert_revision()
        finally:
            self.stop.set()
            monitor.join(35)
        successful = [s['ms'] for s in self.samples if s['ok'] and s['label'] not in ('isolation-denial', 'health', 'revision')]
        latency = metrics(successful)
        by_operation = {label: metrics([s['ms'] for s in self.samples if s['label'] == label and s['ok']])
                        for label in sorted({s['label'] for s in self.samples})}
        passed = (len(self.results) == self.users * len(MODULES) and all(x['ok'] for x in self.results)
                  and all(x['ok'] for x in self.samples) and latency is not None
                  and all(m['p95Ms'] <= self.max_p95_ms for m in by_operation.values() if m))
        return {'format': 'asa-release-project-load-v1', 'revision': self.revision, 'users': self.users,
                'scope': 'HTTP project/media persistence; browser rendering and AI require separate acceptance',
                'passed': passed, 'requestLatency': latency, 'operationLatency': by_operation,
                'maxP95Ms': self.max_p95_ms, 'paceMs': self.pace_ms, 'actualDistinctStudents': len(set(actual_seats)),
                'modules': {m: {'journeys': sum(x['module'] == m for x in self.results),
                               'passed': sum(x['module'] == m and x['ok'] for x in self.results)} for m in MODULES},
                'journeys': self.results, 'requests': self.samples}


def worker(send, fixture, revision, threshold, pace_ms):
    runner = Runner(fixture, revision, 50, threshold, pace_ms)
    try:
        report = runner.run()
    except Exception as e:
        report = {'format': 'asa-release-project-load-v1', 'revision': revision, 'users': 50,
                  'passed': False, 'error': str(e)[:180] if isinstance(e, CheckFailed) else type(e).__name__,
                  'journeys': runner.results, 'requests': runner.samples}
    try:
        send.send(report)
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


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--origin', required=True)
    p.add_argument('--revision', required=True)
    p.add_argument('--fixture', type=Path)
    p.add_argument('--output', type=Path)
    p.add_argument('--users', type=int, default=50, choices=[50])
    p.add_argument('--max-p95-ms', type=int, default=5000)
    p.add_argument('--pace-ms', type=int, default=0, help='Optional per-user think time before each authenticated request; reported explicitly')
    p.add_argument('--deadline-seconds', type=int, default=300, help='Hard wall-time bound for this test process, including identity checks')
    p.add_argument('--apply', action='store_true')
    args = p.parse_args(argv)
    u = urllib.parse.urlsplit(args.origin)
    if u.scheme != 'https' or not u.hostname or u.username or u.password or u.query or u.fragment or u.path not in ('', '/'):
        p.error('an exact HTTPS application origin without credentials is required')
    if len(args.revision) != 40 or any(c not in '0123456789abcdef' for c in args.revision):
        p.error('pin a full lowercase Git revision')
    if args.max_p95_ms <= 0:
        p.error('latency threshold must be positive')
    if not 0 <= args.pace_ms <= 5000 or not 30 <= args.deadline_seconds <= 900:
        p.error('pace must be 0..5000 ms; wall-time deadline must be 30..900 seconds')
    if not args.apply:
        print(json.dumps({'mode': 'plan', 'networkRequests': 0, 'users': 50, 'modules': MODULES,
                          'syntheticProjectsCreatedOnApply': 150, 'revision': args.revision,
                          'requires': 'authorized synthetic fixture on the deployment host; no real learner credentials'}))
        return 0
    if not args.fixture or not args.output:
        p.error('--apply requires --fixture and --output')
    if args.fixture.resolve() == args.output.resolve() or args.output.exists():
        p.error('output must be new and must not overwrite a fixture or earlier receipt')
    fixture = fixture_input(args.fixture, args.origin, 50)
    report = {'format': 'asa-release-project-load-v1', 'revision': args.revision, 'users': 50,
              'passed': False, 'state': 'running', 'deadlineSeconds': args.deadline_seconds}
    write_receipt(args.output, report, exclusive=True)
    context = multiprocessing.get_context('spawn')
    receive, send = context.Pipe(duplex=False)
    child = context.Process(target=worker, args=(send, fixture, args.revision, args.max_p95_ms, args.pace_ms))
    try:
        child.start()
        send.close()
        if receive.poll(args.deadline_seconds):
            try:
                report = receive.recv()
            except EOFError:
                report.update(state='failed', error='worker_exited_without_receipt')
        else:
            report.update(state='failed', error='wall_time_deadline_exceeded')
    except BaseException:
        report.update(state='failed', error='worker_interrupted')
        raise
    finally:
        if child.pid is not None:
            if child.is_alive():
                child.terminate()
            child.join(5)
            if child.is_alive():
                child.kill()
                child.join(5)
        receive.close()
        write_receipt(args.output, report)
    print(json.dumps({k: v for k, v in report.items() if k not in ('journeys', 'requests')}))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except CheckFailed as e:
        print('BLOCKED ' + str(e))
        raise SystemExit(2)
