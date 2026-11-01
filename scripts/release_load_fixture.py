#!/usr/bin/env python3
"""Opt-in host-only synthetic QA cohort. Default plan performs no I/O.

No live code is imported until --apply. Never print adapter exceptions or state.
"""
import argparse
import contextlib
from datetime import datetime, timedelta, timezone
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import urllib.parse
import urllib.request
import uuid

RESOURCE_KEYS = {'classroomId', 'classProjectId', 'publicationId', 'publicCourseId', 'courseRunId', 'lessonId'}
UUID = re.compile(r'[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\Z')
TOKEN = re.compile(r'[A-Za-z0-9_-]{32,256}\Z')
LABEL = re.compile(r'QA-[a-z0-9][a-z0-9-]{2,24}\Z')
NONCE = re.compile(r'[0-9a-f]{24}\Z')
CLASS_CODE = re.compile(r'[ABCDEFGHJKLMNPQRSTUVWXYZ23456789]{9}\Z')
MAX_BYTES = 512 * 1024


class FixtureError(Exception):
    pass


def need(condition, code):
    if not condition:
        raise FixtureError(code)


def ident(value):
    need(isinstance(value, str) and UUID.fullmatch(value), 'UUID_INVALID')
    return value


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def origin(value):
    parsed = urllib.parse.urlsplit(value)
    need(parsed.scheme == 'https' and parsed.hostname and parsed.netloc == parsed.netloc.lower()
         and not parsed.username and not parsed.password and not parsed.path and not parsed.query
         and not parsed.fragment and parsed.port in (None, 443), 'HTTPS_ORIGIN_INVALID')
    return value


def parameters(label, revision, schema, url):
    need(isinstance(label, str) and LABEL.fullmatch(label), 'NEW_QA_LABEL_REQUIRED')
    need(isinstance(revision, str) and re.fullmatch(r'[0-9a-f]{40}', revision), 'REVISION_INVALID')
    need(type(schema) is int and schema > 0, 'SCHEMA_INVALID')
    origin(url)


def fresh(label, revision, schema, url, source_ids, now=None):
    parameters(label, revision, schema, url)
    need(isinstance(source_ids, list) and len(source_ids) <= 128 and len(set(source_ids)) == len(source_ids)
         and all(isinstance(s, str) and re.fullmatch(r'[A-Za-z0-9:_./-]{1,160}', s) for s in source_ids), 'SOURCE_IDS_INVALID')
    now = now or datetime.now(timezone.utc)
    nonce = secrets.token_hex(12)
    actors = {}
    for role, suffix in (('teacher', 't'), ('admin', 'a')):
        actors[role] = {'email': f'asa-qa-{nonce}-{role}@example.invalid',
                        'username': f'qa_{nonce}_{suffix}', 'token': secrets.token_urlsafe(48)}
    return {'format': 'asa-synthetic-cohort-v1', 'authorizedSyntheticFixture': True,
            'nonce': nonce, 'label': label, 'revision': revision, 'schema': schema, 'origin': url,
            'createdAt': now.isoformat(), 'expiresAt': (now + timedelta(hours=2)).isoformat(),
            'state': 'intent', 'roles': actors, 'seats': [], 'resources': {},
            'expectedRequiredLogSourceIds': source_ids, 'pending': None,
            'notProven': ['registration_email_confirmation', 'account_password_login',
                          'browser_join', 'mixed_learning_publication_resources', 'release_acceptance']}


def validate(f):
    need(isinstance(f, dict) and f.get('format') == 'asa-synthetic-cohort-v1'
         and f.get('authorizedSyntheticFixture') is True, 'FIXTURE_FORMAT')
    parameters(f.get('label'), f.get('revision'), f.get('schema'), f.get('origin'))
    nonce = f.get('nonce')
    need(isinstance(nonce, str) and NONCE.fullmatch(nonce), 'NONCE_INVALID')
    try:
        created, expiry = [datetime.fromisoformat(f[k]) for k in ('createdAt', 'expiresAt')]
        need(created.utcoffset() is not None and expiry.utcoffset() is not None
             and 0 < (expiry - created).total_seconds() <= 7200, 'TTL_INVALID')
    except (KeyError, TypeError, ValueError):
        raise FixtureError('TTL_INVALID') from None
    need(set(f.get('roles', {})) == {'teacher', 'admin'}, 'ROLES_INVALID')
    for role, suffix in (('teacher', 't'), ('admin', 'a')):
        actor = f['roles'][role]
        need(actor.get('email') == f'asa-qa-{nonce}-{role}@example.invalid'
             and actor.get('username') == f'qa_{nonce}_{suffix}'
             and TOKEN.fullmatch(str(actor.get('token', ''))), 'ACTOR_FOREIGN')
        for key in ('accountId', 'principalId', 'workspaceId', 'tenantId'):
            if key in actor:
                ident(actor[key])
    seats = f.get('seats')
    need(isinstance(seats, list) and len(seats) <= 100, 'SEATS_INVALID')
    for row in seats:
        ident(row.get('seat'))
        need(re.fullmatch(r'[A-Za-z0-9]{4,10}', str(row.get('studentCode', ''))), 'STUDENT_CODE_INVALID')
        if 'token' in row:
            need(TOKEN.fullmatch(str(row['token'])), 'TOKEN_INVALID')
    need(len({s['seat'] for s in seats}) == len(seats), 'SEAT_DUPLICATE')
    need(len({s['token'] for s in seats if 'token' in s}) == sum('token' in s for s in seats), 'TOKEN_DUPLICATE')
    need(f['roles']['teacher']['token'] != f['roles']['admin']['token'], 'TOKEN_DUPLICATE')
    source_ids = f.get('expectedRequiredLogSourceIds')
    need(isinstance(source_ids, list) and len(source_ids) <= 128 and len(set(source_ids)) == len(source_ids)
         and all(isinstance(s, str) and re.fullmatch(r'[A-Za-z0-9:_./-]{1,160}', s) for s in source_ids), 'SOURCE_IDS_INVALID')
    need(isinstance(f.get('resources'), dict) and set(f['resources']) <= RESOURCE_KEYS, 'RESOURCES_INVALID')
    for value in f['resources'].values():
        ident(value)
    if 'joinCode' in f:
        need(CLASS_CODE.fullmatch(f['joinCode'].replace(' ', '')), 'CLASS_CODE_INVALID')
    return f


def private_path(path, directory=False):
    path = Path(path).absolute()
    need('..' not in path.parts and path.resolve() == path, 'PRIVATE_PATH_ALIAS')
    for parent in (path, *path.parents):
        need(not (parent / '.git').exists(), 'PRIVATE_PATH_INSIDE_GIT')
        if parent.exists():
            info = parent.lstat()
            need(not stat.S_ISLNK(info.st_mode) and not getattr(info, 'st_file_attributes', 0) & 0x400,
                 'PRIVATE_PATH_ALIAS')
    parent = path if directory else path.parent
    if parent.exists():
        info = parent.stat()
        need(stat.S_ISDIR(info.st_mode), 'PRIVATE_DIRECTORY_INVALID')
        if os.name != 'nt':
            need(info.st_uid == os.geteuid() and not info.st_mode & 0o077, 'PRIVATE_DIRECTORY_PERMISSIONS')
    return path


def sync_directory(path):
    if os.name != 'nt':
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


class PrivateStore:
    def __init__(self, path, create=False):
        self.path = private_path(path)
        self.create = create
        if create:
            need(not self.path.exists(), 'FIXTURE_EXISTS')
        self.lock = None

    def __enter__(self):
        parent = self.path.parent
        need(parent.parent.is_dir(), 'PRIVATE_PARENT_MISSING')
        if not parent.exists():
            parent.mkdir(mode=0o700)
            sync_directory(parent.parent)
        private_path(self.path)
        # POSIX live only; fcntl deliberately imported after --apply/test entry.
        if os.name != 'nt':
            import fcntl
            fd = os.open(str(self.path) + '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            info = os.fstat(fd)
            if not (stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.geteuid()
                    and not info.st_mode & 0o077):
                os.close(fd)
                raise FixtureError('LOCK_UNSAFE')
            self.lock = fd
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                self.lock = None
                raise FixtureError('FIXTURE_BUSY') from None
        if self.create and self.path.exists():
            self.__exit__()
            self.lock = None
            raise FixtureError('FIXTURE_EXISTS')
        return self

    def __exit__(self, *args):
        if self.lock is not None:
            os.close(self.lock)

    def read(self):
        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
        fd = os.open(self.path, flags)
        try:
            info = os.fstat(fd)
            need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= MAX_BYTES, 'FIXTURE_FILE_UNSAFE')
            if os.name != 'nt':
                need(info.st_uid == os.geteuid() and not info.st_mode & 0o077, 'FIXTURE_FILE_PERMISSIONS')
            raw = os.read(fd, MAX_BYTES + 1)
            need(len(raw) == info.st_size, 'FIXTURE_FILE_CHANGED')
            return validate(json.loads(raw))
        finally:
            os.close(fd)

    def save(self, value):
        validate(value)
        if self.path.exists():
            current = self.read()  # Refuse replacing links/foreign or non-private data.
            need(all(current[k] == value[k] for k in ('nonce', 'label', 'origin', 'revision', 'schema', 'createdAt', 'expiresAt')),
                 'FIXTURE_REPLACEMENT_FOREIGN')
        raw = (json.dumps(value, separators=(',', ':')) + '\n').encode()
        need(len(raw) <= MAX_BYTES, 'FIXTURE_TOO_LARGE')
        temporary = self.path.with_name('.' + self.path.name + '.' + secrets.token_hex(8))
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as out:
            out.write(raw)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, self.path)
        sync_directory(self.path.parent)


def literal(value):
    need(isinstance(value, str) and '\x00' not in value and len(value) <= 4096, 'SQL_VALUE_INVALID')
    return "'" + value.replace("'", "''") + "'"


def account_sql(f, role):
    a = f['roles'][role]
    # Structurally valid scrypt-v1 digest with no known password/preimage.
    unusable = 'scrypt-v1$16384$8$1$' + secrets.token_hex(16) + '$' + secrets.token_hex(64)
    capabilities = ('educator', 'content_author') if role == 'teacher' else ('platform_admin',)
    email, username, marker = literal(a['email']), literal(a['username']), literal('qa-' + f['nonce'])
    caps = ','.join(f"(v.account_id,{literal(c)},'verified',{marker},'admin')" for c in capabilities)
    return f"""BEGIN;
SET LOCAL statement_timeout='10s'; SET LOCAL lock_timeout='3s';
SELECT pg_advisory_xact_lock(hashtextextended({literal('asa-qa:' + f['nonce'])},0));
DO $qa$ DECLARE v record; BEGIN
 IF EXISTS(SELECT 1 FROM public.accounts WHERE email={email}) OR EXISTS(SELECT 1 FROM public.profiles WHERE username={username} OR display_name={literal(f['label'] + ' ' + role)}) THEN RAISE EXCEPTION 'QA_IDENTITY_EXISTS'; END IF;
 SELECT * INTO STRICT v FROM public.auth_register_account({email},{literal(unusable)},{literal(f['label'] + ' ' + role)},{username},DATE '1990-01-01','RU',{marker},{literal(digest(a['token']))},2);
 UPDATE public.accounts SET email_verification_state='verified' WHERE id=v.account_id AND email={email};
 INSERT INTO public.capability_grants(account_id,capability,state,policy_version,granted_by) VALUES {caps};
 UPDATE public.sessions_v2 SET expires_at=LEAST(expires_at,{literal(f['expiresAt'])}::timestamptz) WHERE principal_id=v.principal_id;
END $qa$;
SELECT json_build_object('accountId',a.id,'principalId',p.id,'workspaceId',w.id,'tenantId',w.tenant_id)
 FROM public.accounts a JOIN public.profiles pr ON pr.account_id=a.id JOIN public.principals p ON p.account_id=a.id
 JOIN public.workspace_memberships m ON m.account_id=a.id AND m.role='owner' JOIN public.workspaces w ON w.id=m.workspace_id AND w.kind='personal'
 WHERE a.email={email} AND pr.username={username};
COMMIT;"""


def fence_sql(f, expire=False):
    """Exact synthetic marker + email + username + class owner, before any UPDATE.

    Recovery discovers ONLY nonce-owned accounts/classes, including a lost API
    response. It never trusts raw IDs supplied by the private manifest alone.
    """
    validate(f)
    marker = literal('qa-' + f['nonce'])
    emails = ','.join(literal(a['email']) for a in f['roles'].values())
    title = literal(f['label'] + ' ' + f['nonce'])
    teacher = literal(f['roles']['teacher']['email'])
    actor_checks = []
    for role in ('teacher', 'admin'):
        a = f['roles'][role]
        checks = f"p.username<>{literal(a['username'])} OR NOT EXISTS(SELECT 1 FROM public.capability_grants g WHERE g.account_id=a.id AND g.capability='creator' AND g.policy_version={marker})"
        if 'accountId' in a:
            checks += f" OR a.id<>{literal(a['accountId'])}::uuid"
        actor_checks.append(f"IF EXISTS(SELECT 1 FROM public.accounts a LEFT JOIN public.profiles p ON p.account_id=a.id WHERE a.email={literal(a['email'])} AND (p.account_id IS NULL OR {checks})) THEN RAISE EXCEPTION 'QA_FOREIGN_ACCOUNT'; END IF;")
    cls = f"SELECT c.id FROM public.classrooms c JOIN public.classroom_memberships m ON m.classroom_id=c.id AND m.member_role='owner' JOIN public.accounts a ON a.id=m.account_id WHERE a.email={teacher} AND c.title={title}"
    if f['resources'].get('classroomId'):
        actor_checks.append(f"IF NOT EXISTS({cls} AND c.id={literal(f['resources']['classroomId'])}::uuid) THEN RAISE EXCEPTION 'QA_FOREIGN_CLASS'; END IF;")
    end = 'now()' if expire else literal(f['expiresAt']) + '::timestamptz'
    grant = f"UPDATE public.capability_grants g SET state='revoked' FROM public.accounts a WHERE a.id=g.account_id AND a.email={literal(f['roles']['admin']['email'])} AND g.capability='platform_admin' AND g.policy_version={marker};" if expire else ''
    revoked = ',revoked_at=COALESCE(revoked_at,now())' if expire else ''
    if expire:
        proof = f"""NOT EXISTS(SELECT 1 FROM public.sessions_v2 s JOIN public.principals p ON p.id=s.principal_id JOIN public.accounts a ON a.id=p.account_id WHERE a.email IN ({emails}) AND (s.revoked_at IS NULL OR s.expires_at>now()))
 AND NOT EXISTS(SELECT 1 FROM public.classroom_student_sessions s JOIN public.classroom_student_seats seat ON seat.id=s.seat_id WHERE seat.classroom_id IN ({cls}) AND (s.revoked_at IS NULL OR s.expires_at>now()))
 AND NOT EXISTS(SELECT 1 FROM public.capability_grants g JOIN public.accounts a ON a.id=g.account_id WHERE a.email={literal(f['roles']['admin']['email'])} AND g.capability='platform_admin' AND g.policy_version={marker} AND g.state<>'revoked')"""
    else:
        accounts = ','.join(f"({literal(a['email'])},{literal(digest(a['token']))})" for a in f['roles'].values())
        known_seats = [s for s in f['seats'] if 'token' in s]
        seats = ','.join(f"({literal(s['seat'])}::uuid,{literal(digest(s['token']))})" for s in known_seats)
        seat_proof = (f"(SELECT count(*) FROM public.classroom_student_sessions s JOIN public.classroom_student_seats seat ON seat.id=s.seat_id WHERE (s.seat_id,s.token_hash) IN ({seats}) AND seat.classroom_id IN ({cls}) AND s.revoked_at IS NULL AND s.expires_at<={end} AND s.expires_at>now())={len(known_seats)}"
                      if seats else 'true')
        proof = f"""(SELECT count(*) FROM public.sessions_v2 s JOIN public.principals p ON p.id=s.principal_id JOIN public.accounts a ON a.id=p.account_id WHERE (a.email,s.token_hash) IN ({accounts}) AND s.revoked_at IS NULL AND s.expires_at<={end} AND s.expires_at>now())=2 AND {seat_proof}"""
    return f"""BEGIN; SET LOCAL statement_timeout='10s'; SET LOCAL lock_timeout='3s';
SELECT pg_advisory_xact_lock(hashtextextended({literal('asa-qa:' + f['nonce'])},0));
DO $qa$ BEGIN {' '.join(actor_checks)} END $qa$;
UPDATE public.sessions_v2 s SET expires_at=LEAST(s.expires_at,{end}){revoked} FROM public.principals p JOIN public.accounts a ON a.id=p.account_id WHERE s.principal_id=p.id AND a.email IN ({emails});
UPDATE public.classroom_student_sessions s SET expires_at=LEAST(s.expires_at,{end}){revoked} FROM public.classroom_student_seats seat WHERE seat.id=s.seat_id AND seat.classroom_id IN ({cls});
{grant}
SELECT json_build_object('bounded',({proof})); COMMIT;"""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class HostAdapter:
    def __init__(self, root, f):
        need(os.name == 'posix' and os.geteuid() == 0, 'APPLY_REQUIRES_SERVER_ROOT')
        self.root = Path(root).absolute()
        need(self.root.resolve() == self.root and self.root.is_dir(), 'CANONICAL_ROOT_REQUIRED')
        # Exact checkout first, before executing its trusted manager code.
        def git(*args):
            result = subprocess.run(['git', '-C', str(self.root), *args], capture_output=True, timeout=10)
            need(result.returncode == 0, 'SOURCE_UNAVAILABLE')
            return result.stdout.decode().strip()
        need(git('rev-parse', 'HEAD') == f['revision'] and not git('status', '--porcelain'), 'SOURCE_REVISION_DIRTY')
        contract = json.loads((Path(__file__).resolve().parents[1] / 'profiles/school-fixture-source-contract.json').read_text())
        need(contract.get('version') == 1 and len(contract.get('sourceFiles', {})) == 11, 'SOURCE_CONTRACT_INVALID')
        for path, expected in contract['sourceFiles'].items():
            need(not Path(path).is_absolute() and '..' not in Path(path).parts
                 and re.fullmatch(r'[a-f0-9]{64}', expected), 'SOURCE_CONTRACT_INVALID')
            blob = subprocess.run(['git', '-C', str(self.root), 'show', f['revision'] + ':' + path], capture_output=True, timeout=10)
            need(blob.returncode == 0 and hashlib.sha256(blob.stdout).hexdigest() == expected, 'FIXTURE_API_SCHEMA_DRIFT')
        sys.path.insert(0, str(self.root / 'tools'))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            system = importlib.import_module('deployment.system')
            need(Path(system.__file__).resolve() == self.root / 'tools/deployment/system.py', 'MANAGER_IMPORT_COLLISION')
            self.install = system.Installation(self.root)
            need(self.install.env.get('ASA_UPDATE_ENTRY_ORIGIN') == f['origin'], 'CANONICAL_ORIGIN_MISMATCH')
            records = self.install.identity()
            self.install.readiness(f['revision'], f['schema'])
            env = self.install.container_environment(records['postgres'])
        self.binding = {'root': str(self.root), 'project': self.install.project,
                        'volumes': {name: sorted(m['Name'] for m in records[name]['mounts'] if m.get('Type') == 'volume')
                                    for name in ('postgres', 'minio')}}
        if 'installation' in f:
            need(f['installation'] == self.binding, 'FOREIGN_INSTALLATION')
        self.database = env.get('POSTGRES_DB')
        self.user = env.get('POSTGRES_USER')
        need(all(isinstance(v, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,63}', v) for v in (self.database, self.user)), 'DATABASE_IDENTITY_INVALID')
        self.container = records['postgres']['id']
        self.url = f['origin']
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        health, _ = self.api(None, '/health/ready')
        deployment = health.get('deployment', {})
        need(health.get('status') == 'ready' and deployment.get('revision') == f['revision']
             and deployment.get('schemaVersion') == deployment.get('expectedSchemaVersion') == f['schema']
             and deployment.get('synchronized') is True, 'ORIGIN_RUNTIME_MISMATCH')

    def sql(self, query):
        # SQL and secrets use stdin only. No psql error rows/commands are exposed.
        result = subprocess.run(['docker', 'exec', '-i', self.container, 'psql', '-X', '-qAt',
                                 '-v', 'ON_ERROR_STOP=1', '-U', self.user, '-d', self.database],
                                input=query, text=True, capture_output=True, timeout=20)
        need(result.returncode == 0 and len(result.stdout) <= 8192, 'DATABASE_STAGE_FAILED')
        rows = [line for line in result.stdout.splitlines() if line.startswith('{')]
        need(len(rows) == 1, 'DATABASE_RECEIPT_INVALID')
        return json.loads(rows[0])

    def account(self, f, role):
        result = self.sql(account_sql(f, role))
        need(set(result) == {'accountId', 'principalId', 'workspaceId', 'tenantId'}, 'ACCOUNT_RECEIPT_INVALID')
        return {key: ident(value) for key, value in result.items()}

    def bound(self, f, expire=False):
        need(self.sql(fence_sql(f, expire)) == {'bounded': True}, 'SESSION_BOUND_NOT_VERIFIED')

    def api(self, token, path, body=None, cookie='asa_session', key=None):
        need(path.startswith('/') and not path.startswith('//') and '?' not in path, 'API_PATH_INVALID')
        headers = {'Origin': self.url, 'User-Agent': 'ASA-Synthetic-Fixture/1'}
        if token:
            need(TOKEN.fullmatch(token), 'TOKEN_INVALID')
            headers['Cookie'] = cookie + '=' + token
        if key:
            headers['Idempotency-Key'] = key
        data = None
        if body is not None:
            headers['Content-Type'] = 'application/json'
            data = json.dumps(body).encode()
        request = urllib.request.Request(self.url + path, data=data, headers=headers)
        with self.opener.open(request, timeout=10) as response:
            need(response.status in (200, 201) and response.geturl() == self.url + path, 'API_STATUS_INVALID')
            raw = response.read(256 * 1024 + 1)
            need(len(raw) <= 256 * 1024, 'API_BODY_TOO_LARGE')
            session = None
            for header in response.headers.get_all('Set-Cookie', []):
                match = re.match(r'asa_student_session=([A-Za-z0-9_-]{32,256})(?:;|$)', header)
                if match:
                    need(session is None, 'SESSION_COOKIE_DUPLICATE')
                    session = match[1]
            return json.loads(raw), session


def provision(f, store, adapter):
    need(f['state'] == 'intent', 'NO_AUTOMATIC_REPLAY')
    try:
        for role in ('teacher', 'admin'):
            need(datetime.now(timezone.utc) < datetime.fromisoformat(f['expiresAt']), 'FIXTURE_DEADLINE_EXPIRED')
            f['pending'] = 'bootstrap_' + role
            store.save(f)  # Persist recovery intent BEFORE each external mutation.
            f['roles'][role].update(adapter.account(f, role))
            store.save(f)
        teacher = f['roles']['teacher']['token']
        f['pending'] = 'create_class'
        store.save(f)
        response, _ = adapter.api(teacher, '/api/classrooms', {
            'title': f['label'] + ' ' + f['nonce'], 'ageBand': 'mixed',
            'topicKeys': [], 'safeModeDefault': True}, key=str(uuid.uuid4()))
        c = response['classroom']
        need(c.get('teacherRole') == 'owner' and c.get('workspaceKind') == 'personal'
             and c.get('title') == f['label'] + ' ' + f['nonce'], 'NEW_CLASS_OWNERSHIP_INVALID')
        f['resources']['classroomId'] = ident(c['id'])
        f['joinCode'] = c['joinCode']
        store.save(f)
        for index in range(100):
            need(datetime.now(timezone.utc) < datetime.fromisoformat(f['expiresAt']), 'FIXTURE_DEADLINE_EXPIRED')
            f['pending'] = 'create_seat_' + str(index + 1)
            store.save(f)
            data, _ = adapter.api(teacher, '/api/classrooms/' + c['id'] + '/seats', {
                'displayLabel': f['label'] + ' pupil ' + str(index + 1), 'safeMode': True})
            student = data['student']
            row = {'seat': ident(student['id']), 'studentCode': student['studentCode']}
            f['seats'].append(row)
            f['pending'] = 'join_seat_' + str(index + 1)
            store.save(f)
            joined, token = adapter.api(None, '/api/class-join/studentseat', {
                'code': f['joinCode'], 'studentCode': row['studentCode']})
            need(joined.get('authenticated') is True and joined.get('student', {}).get('seatId') == row['seat']
                 and joined.get('classroom', {}).get('id') == c['id']
                 and isinstance(token, str) and TOKEN.fullmatch(token), 'ACTUAL_SEAT_JOIN_FAILED')
            row['token'] = token
            store.save(f)
            adapter.bound(f)  # Native login TTL is shortened to this fixed cohort deadline.
        adapter.bound(f)
        need(datetime.now(timezone.utc) < datetime.fromisoformat(f['expiresAt']), 'FIXTURE_DEADLINE_EXPIRED')
        f.update(state='core_ready', pending=None)
        store.save(f)
    except BaseException:
        f['state'] = 'partial_requires_expiry'
        store.save(f)
        # A lost API response may have committed. Fence all sessions of the
        # nonce-owned class, not only cookies successfully returned to Python.
        try:
            adapter.bound(f, expire=True)
            f['failureExpiry'] = ('OBSERVED_SESSIONS_EXPIRED_INFLIGHT_REQUEST_UNKNOWN'
                                  if f['pending'] is not None else 'confirmed')
        except BaseException:
            f['failureExpiry'] = 'UNKNOWN_OPERATOR_MUST_EXPIRE'
        store.save(f)
        raise FixtureError('PROVISION_PARTIAL_RETAINED') from None


def attach_resources(f, nonce, resources, operator_attests_own_qa=False, existing_publication=False):
    validate(f)
    need(f['nonce'] == nonce and operator_attests_own_qa is True, 'OWN_QA_RESOURCE_ATTESTATION_REQUIRED')
    need(f['state'] == 'core_ready' and datetime.now(timezone.utc) < datetime.fromisoformat(f['expiresAt']), 'CORE_FIXTURE_NOT_ACTIVE')
    need(bool(f['expectedRequiredLogSourceIds']), 'PINNED_REQUIRED_SOURCES_MISSING')
    need(isinstance(resources, dict) and set(resources) == RESOURCE_KEYS, 'ALL_MIXED_RESOURCE_IDS_REQUIRED')
    need(resources['classroomId'] == f['resources'].get('classroomId'), 'FOREIGN_CLASSROOM')
    f['resources'] = {key: ident(value) for key, value in resources.items()}
    f['resourceEvidence'] = 'OPERATOR_ATTESTATION_ONLY_API_ACCEPTANCE_NOT_RUN'
    f['publicationEvidence'] = 'OPERATOR_EXISTING_PUBLIC_READ_ONLY' if existing_publication else 'OPERATOR_OWN_QA_PUBLICATION'
    return f


def expire(f, store, adapter, nonce, attest_no_inflight=False):
    validate(f)
    need(f['nonce'] == nonce, 'EXACT_FIXTURE_BINDING_REQUIRED')
    need(f.get('pending') is None or attest_no_inflight is True, 'INFLIGHT_REQUEST_NOT_SETTLED')
    adapter.bound(f, expire=True)
    f.update(state='expired', pending=None)
    store.save(f)


def main(argv=None, adapter_factory=HostAdapter):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('provision', 'expire', 'attach-resources'), nargs='?', default='provision')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--origin', required=True)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--schema', required=True, type=int)
    parser.add_argument('--label', required=True)
    parser.add_argument('--canonical-root')
    parser.add_argument('--fixture')
    parser.add_argument('--nonce')
    parser.add_argument('--required-source-id', action='append', default=[])
    parser.add_argument('--resource', action='append', default=[], help='KEY=UUID; IDs only, never tokens')
    parser.add_argument('--attest-own-qa-resources', action='store_true')
    parser.add_argument('--publication-existing-public', action='store_true', help='Attach an explicitly selected existing PUBLIC read-only publication; never create/publicize content')
    parser.add_argument('--attest-no-inflight', action='store_true', help='For partial intent expiry only: operator verified earlier own requests are no longer running')
    args = parser.parse_args(argv)
    try:
        parameters(args.label, args.revision, args.schema, args.origin)
        if not args.apply:
            # No path resolution/read/import/adapter/network/randomness in plan.
            print(json.dumps({'state': 'PLAN_ONLY', 'action': args.action, 'syntheticSeats': 100,
                              'newAccounts': 2, 'sessionMaxSeconds': 7200,
                              'publicContentCreated': False, 'mixedResources': 'SEPARATE_REVIEWED_API_STEP'}))
            return 0
        need(args.canonical_root and args.fixture, 'ROOT_AND_PRIVATE_FIXTURE_REQUIRED')
        need(os.name == 'posix' and os.geteuid() == 0, 'APPLY_REQUIRES_SERVER_ROOT')
        with PrivateStore(args.fixture, create=args.action == 'provision') as store:
            if args.action == 'provision':
                f = fresh(args.label, args.revision, args.schema, args.origin, args.required_source_id)
                store.save(f)
            else:
                f = store.read()
                need(f['nonce'] == args.nonce and (f['label'], f['revision'], f['schema'], f['origin']) ==
                     (args.label, args.revision, args.schema, args.origin), 'EXACT_FIXTURE_BINDING_REQUIRED')
            adapter = adapter_factory(args.canonical_root, f)
            if args.action == 'provision':
                f['installation'] = adapter.binding
                store.save(f)
                provision(f, store, adapter)
            elif args.action == 'expire':
                expire(f, store, adapter, args.nonce, args.attest_no_inflight)
            else:
                pairs = [item.split('=', 1) for item in args.resource]
                need(all(len(pair) == 2 for pair in pairs) and len({p[0] for p in pairs}) == len(pairs), 'RESOURCE_ARGUMENTS_INVALID')
                store.save(attach_resources(f, args.nonce, dict(pairs), args.attest_own_qa_resources, args.publication_existing_public))
        print(json.dumps({'state': f['state'], 'seats': len(f['seats']), 'fixture': str(store.path)}))
        return 0
    except (Exception, KeyboardInterrupt) as error:
        # No chained SQL/HTTP exceptions, fixture values, URLs or private tokens.
        code = str(error) if isinstance(error, FixtureError) and re.fullmatch(r'[A-Z_]{1,100}', str(error)) else 'FIXTURE_STAGE_FAILED'
        print(json.dumps({'state': 'STOP', 'code': code}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
