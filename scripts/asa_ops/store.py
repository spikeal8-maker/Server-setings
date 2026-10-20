"""Transactional plans, idempotency and crash recovery; never resumes mutations."""
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import uuid


class OpsError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
        raise OpsError("INVALID_ID", "Expected an opaque operation or plan identifier")
    return value


def process_identity(pid):
    """PID plus boot/start identity prevents PID reuse from looking like a live job."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
        fields = raw.rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        return Path('/proc/sys/kernel/random/boot_id').read_text().strip() + ":" + fields[19]
    except (OSError, IndexError):
        return None


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name != 'nt':
            os.chmod(self.root, 0o700)
        with self.db() as db:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS plans(id TEXT PRIMARY KEY, installation TEXT, hash TEXT, value TEXT);
              CREATE TABLE IF NOT EXISTS operations(
                id TEXT PRIMARY KEY, installation TEXT, kind TEXT, plan_id TEXT, idem TEXT UNIQUE,
                request_hash TEXT, status TEXT, phase TEXT, created TEXT, updated TEXT,
                pid INTEGER, process_identity TEXT, result TEXT);
              CREATE UNIQUE INDEX IF NOT EXISTS one_active_installation ON operations(installation)
                WHERE status IN ('queued','running','recovery_required');
            ''')

    @contextlib.contextmanager
    def db(self):
        db = sqlite3.connect(self.root / 'operations.sqlite3', timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA synchronous=FULL')
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def plan(self, installation, value):
        value = dict(value, installation_id=installation, created_at=now())
        pid = uuid.uuid4().hex
        with self.db() as db:
            db.execute('INSERT INTO plans VALUES(?,?,?,?)', (pid, installation, digest(value), json.dumps(value)))
        return dict(value, plan_id=pid, plan_hash=digest(value))

    def get_plan(self, pid):
        with self.db() as db:
            row = db.execute('SELECT * FROM plans WHERE id=?', (identifier(pid),)).fetchone()
        if row is None:
            raise OpsError('NOT_FOUND', 'Plan does not exist')
        value = json.loads(row['value'])
        if digest(value) != row['hash']:
            raise OpsError('INTEGRITY', 'Stored plan integrity check failed')
        return dict(value, plan_id=pid, plan_hash=row['hash'])

    def start(self, installation, kind, plan_id, key, request):
        if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{16,128}', key):
            raise OpsError('IDEMPOTENCY_KEY', 'Use a 16–128 character idempotency key')
        request_hash = digest(request)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM operations WHERE idem=?', (key,)).fetchone()
            if old:
                if old['request_hash'] != request_hash:
                    raise OpsError('IDEMPOTENCY_CONFLICT', 'Key already belongs to a different request')
                return self._public(old), False
            opid = uuid.uuid4().hex
            try:
                db.execute('INSERT INTO operations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (opid, installation, kind, plan_id, key, request_hash, 'queued', 'queued', now(), now(), None, None, None))
            except sqlite3.IntegrityError as exc:
                raise OpsError('BUSY', 'Another operation owns this installation; inspect it instead of retrying') from exc
            row = db.execute('SELECT * FROM operations WHERE id=?', (opid,)).fetchone()
        return self._public(row), True

    def reused(self, key, request):
        with self.db() as db:
            row = db.execute('SELECT * FROM operations WHERE idem=?', (key,)).fetchone()
        if row:
            if row['request_hash'] != digest(request):
                raise OpsError('IDEMPOTENCY_CONFLICT', 'Key belongs to a different request')
            return self._public(row)
        return None

    @staticmethod
    def _public(row):
        result = {k: row[k] for k in ('id', 'installation', 'kind', 'plan_id', 'status', 'phase', 'created', 'updated')}
        result['operation_id'] = result.pop('id')
        result['installation_id'] = result.pop('installation')
        result['result'] = json.loads(row['result']) if row['result'] else None
        return result

    def get(self, opid):
        with self.db() as db:
            row = db.execute('SELECT * FROM operations WHERE id=?', (identifier(opid),)).fetchone()
        if row is None:
            raise OpsError('NOT_FOUND', 'Operation does not exist')
        return self._public(row)

    def list(self, installation, limit=20):
        with self.db() as db:
            rows = db.execute('SELECT * FROM operations WHERE installation=? ORDER BY created DESC LIMIT ?',
                              (installation, limit)).fetchall()
        return [self._public(r) for r in rows]

    def update(self, opid, **fields):
        allowed = {'status', 'phase', 'pid', 'process_identity', 'result'}
        if not fields or not set(fields) <= allowed:
            raise ValueError('Unexpected operation field')
        fields['updated'] = now()
        if 'result' in fields:
            fields['result'] = json.dumps(fields['result'])
        with self.db() as db:
            db.execute('UPDATE operations SET '+','.join(k+'=?' for k in fields)+' WHERE id=?',
                       (*fields.values(), identifier(opid)))

    def reconcile(self):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            for row in db.execute("SELECT * FROM operations WHERE status IN ('queued','running')").fetchall():
                # A queued record is committed before Popen. Give its creator time to assign PID.
                age = (dt.datetime.now(dt.timezone.utc)-dt.datetime.fromisoformat(row['updated'])).total_seconds()
                if age < 30:
                    continue
                if not row['pid'] or process_identity(row['pid']) != row['process_identity']:
                    # A queued transaction with no live worker and no first
                    # event can be abandoned without executing or replaying it.
                    # Check /proc for the narrow Popen→PID-write interruption.
                    found = False
                    for proc in Path('/proc').glob('[0-9]*') if os.name != 'nt' else []:
                        try:
                            args = (proc/'cmdline').read_bytes().split(b'\0')
                            if b'asa_ops.worker' in args and row['id'].encode() in args:
                                found = True;break
                        except OSError:
                            continue
                    if found:
                        continue
                    never_started = row['status'] == 'queued' and not (self.root/(row['id']+'.jsonl')).exists()
                    state, phase = ('failed', 'never_started') if never_started else ('recovery_required', 'worker_lost')
                    db.execute('UPDATE operations SET status=?,phase=?,updated=? WHERE id=?', (state, phase, now(), row['id']))

    def log(self, opid, cursor=0, limit=200):
        path = self.root / (identifier(opid)+'.jsonl')
        if not path.exists():
            return {'cursor': cursor, 'lines': []}
        lines = []
        with path.open('rb') as stream:
            stream.seek(cursor)
            for _ in range(limit):
                raw = stream.readline()
                if not raw:
                    break
                if not raw.endswith(b'\n'):
                    stream.seek(-len(raw), 1)
                    break
                lines.append(json.loads(raw))
            return {'cursor': stream.tell(), 'lines': lines}

    def event(self, opid, phase, message):
        raw = json.dumps({'at': now(), 'phase': phase, 'message': message[:2000]}, ensure_ascii=False)+'\n'
        fd = os.open(self.root / (identifier(opid)+'.jsonl'), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, 'a', encoding='utf-8') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
