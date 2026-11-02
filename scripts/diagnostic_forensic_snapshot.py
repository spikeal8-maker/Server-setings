#!/usr/bin/env python3
"""Linux operator forensic copy. PLAN by default; never repairs or starts/stops anything."""
import argparse
import contextlib
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import time
import uuid

OWNER_UID = 0
LIMIT = 64 * 1024**3
RESERVE = 64 * 1024**2


class Blocked(RuntimeError):
    pass


def require(condition, code):
    if not condition:
        raise Blocked(code)


def signature(s):
    return (s.st_dev, s.st_ino, s.st_mode, s.st_size, s.st_mtime_ns, s.st_ctime_ns)


def directory(path, private=False):
    """Pin every ancestor without following symlinks. Caller owns returned fd."""
    path = Path(path)
    require(path.is_absolute() and '..' not in path.parts, 'ABSOLUTE_SAFE_PATH_REQUIRED')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
            if private:
                s = os.fstat(fd)
                require(s.st_uid in (0, OWNER_UID) and not s.st_mode & 0o022, 'UNTRUSTED_DESTINATION_PARENT')
        if private:
            require(stat.S_IMODE(os.fstat(fd).st_mode) == 0o700, 'PRIVATE_PARENT_0700_REQUIRED')
        return fd
    except Exception:
        os.close(fd)
        raise


@contextlib.contextmanager
def opened(root, relative):
    parts = Path(relative).parts
    require(parts and all(p not in ('..', '.', '/') for p in parts), 'UNSAFE_ENTRY')
    parent = os.dup(root)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            os.close(parent)
            parent = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            require(stat.S_ISREG(os.fstat(fd).st_mode), 'NON_REGULAR_SOURCE')
            yield fd
        finally:
            os.close(fd)
    finally:
        os.close(parent)


def inventory(root, limit=LIMIT, host_scope=False):
    result = {}
    total = 0

    def visit(fd, prefix=''):
        nonlocal total
        start = signature(os.fstat(fd))
        for name in sorted(os.listdir(fd)):
            relative = prefix + name
            if host_scope:
                if prefix == '' and name not in ('diagnostics', 'log-collector.json', 'collector-runner.json', 'collector-operation.json', 'installation.json'):
                    continue
                if prefix == 'diagnostics/' and name not in ('private', 'store'):
                    require(name == 'application', 'UNKNOWN_DIAGNOSTIC_BOUNDARY')
                    continue
            require(len(result) < 100000, 'TOO_MANY_ENTRIES')
            s = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(s.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    require(signature(os.fstat(child)) == signature(s), 'SOURCE_RACE')
                    visit(child, relative + '/')
                finally:
                    os.close(child)
                result[relative + '/'] = {'identity': signature(s)}
            else:
                require(stat.S_ISREG(s.st_mode) and s.st_nlink == 1, 'LINK_OR_SPECIAL_SOURCE')
                total += s.st_size
                require(total <= limit, 'SOURCE_SIZE_LIMIT')
                digest = hashlib.sha256()
                with opened(root, relative) as source:
                    require(signature(os.fstat(source)) == signature(s), 'SOURCE_RACE')
                    while data := os.read(source, 1024**2):
                        digest.update(data)
                    require(signature(os.fstat(source)) == signature(s), 'SOURCE_RACE')
                result[relative] = {'identity': signature(s), 'bytes': s.st_size, 'sha256': digest.hexdigest()}
        require(signature(os.fstat(fd)) == start, 'SOURCE_RACE')
    visit(root)
    return result


def quiescent(unit):
    require(re.fullmatch(r'asa-log-collector(?:-[a-z0-9_-]+)?\.service', unit), 'UNSUPPORTED_COLLECTOR_UNIT')
    for name in (unit, unit[:-8] + '.timer'):
        p = subprocess.run(['systemctl', 'show', name, '--property=LoadState,ActiveState,MainPID'],
                           capture_output=True, text=True, timeout=10)
        values = dict(line.split('=', 1) for line in p.stdout.splitlines() if '=' in line)
        absent_timer = name.endswith('.timer') and values.get('LoadState') == 'not-found'
        require(absent_timer or (p.returncode == 0 and values.get('LoadState') == 'loaded'
                and values.get('ActiveState') in ('inactive', 'failed') and values.get('MainPID', '0') == '0'),
                'COLLECTOR_NOT_QUIESCENT')


def private_write(path, data):
    with path.open('xb') as stream:
        os.chmod(path, 0o600)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def copy_entry(root, relative, target, expected):
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    digest = hashlib.sha256()
    with opened(root, relative) as source, target.open('xb') as out:
        os.chmod(target, 0o600)
        require(signature(os.fstat(source)) == tuple(expected['identity']), 'SOURCE_RACE')
        while data := os.read(source, 1024**2):
            out.write(data)
            digest.update(data)
        out.flush()
        os.fsync(out.fileno())
        require(signature(os.fstat(source)) == tuple(expected['identity']) and
                digest.hexdigest() == expected['sha256'], 'SOURCE_RACE')


def sqlite_backup(stage, names):
    """Use SQLite backup API on a frozen working mirror, never opening live SQLite."""
    work = stage / 'sqlite-working'
    output = stage / 'sqlite-backup'
    work.mkdir(mode=0o700)
    output.mkdir(mode=0o700)
    require(len(names) <= 16, 'SQLITE_FILE_LIMIT')
    for index, relative in enumerate(names):
        source = stage / 'raw' / relative
        mirror = work / str(index)
        mirror.mkdir(mode=0o700)
        for suffix in ('', '-wal', '-shm', '-journal'):
            p = Path(str(source) + suffix)
            if p.exists():
                with p.open('rb') as source_stream, (mirror / ('db' + suffix)).open('xb') as mirror_stream:
                    os.chmod(mirror / ('db' + suffix), 0o600)
                    shutil.copyfileobj(source_stream, mirror_stream, 1024**2)
                    mirror_stream.flush()
                    os.fsync(mirror_stream.fileno())
        deadline = time.monotonic() + 60
        def progress(*_):
            require(time.monotonic() < deadline, 'SQLITE_BACKUP_DEADLINE')
        with contextlib.closing(sqlite3.connect('file:' + (mirror / 'db').as_posix() + '?mode=ro', uri=True)) as db:
            db.execute('PRAGMA query_only=ON')
            target = output / (hashlib.sha256(relative.encode()).hexdigest() + '.sqlite')
            with contextlib.closing(sqlite3.connect(target)) as destination:
                os.chmod(target, 0o600)
                db.backup(destination, pages=128, progress=progress)
                require(destination.execute('PRAGMA quick_check').fetchone() == ('ok',), 'SQLITE_COPY_INVALID')
            with target.open('rb') as synced:
                os.fsync(synced.fileno())
        # Only this tool's newly-created working mirror is removed. Raw evidence stays untouched.
        require(work.parent == stage and work.name == 'sqlite-working', 'WORKING_PATH_INVALID')
    shutil.rmtree(work)


def rename_new(parent, before, after):
    libc = ctypes.CDLL(None, use_errno=True)
    require(hasattr(libc, 'renameat2'), 'ATOMIC_NOREPLACE_UNAVAILABLE')
    result = libc.renameat2(parent, os.fsencode(before), parent, os.fsencode(after), 1)
    require(result == 0, 'DESTINATION_EXISTS_OR_RENAME_FAILED')


def snapshot(installation, destination, unit='asa-log-collector.service', apply=False):
    require(os.name == 'posix' and hasattr(os, 'O_NOFOLLOW'), 'LINUX_ONLY')
    installation, destination = Path(installation), Path(destination)
    require(installation.is_absolute() and destination.is_absolute(), 'ABSOLUTE_SAFE_PATH_REQUIRED')
    source_path = installation / '.asa'
    # HOST only; independent API application journal and other .asa state are excluded.
    require(not destination.is_relative_to(installation) and not installation.is_relative_to(destination), 'OVERLAPPING_PATHS')
    require(not destination.exists() and not destination.is_symlink(), 'DESTINATION_EXISTS')
    source = directory(source_path)
    try:
        parent = directory(destination.parent, private=True)
    except Exception:
        os.close(source)
        raise
    stage = None
    previous_umask = None
    try:
        before = inventory(source, host_scope=True)
        required = ('diagnostics/private/index.sqlite', 'diagnostics/private/collector.lock',
                    'diagnostics/private/segment-verification.key', 'diagnostics/store/catalog.json', 'log-collector.json')
        require(all(p in before for p in required), 'REQUIRED_FORENSIC_COMPONENT_MISSING')
        total = sum(v.get('bytes', 0) for v in before.values())
        require(os.fstatvfs(parent).f_favail >= len(before)*3+32, 'INSUFFICIENT_INODES')
        require(os.fstatvfs(parent).f_bavail * os.fstatvfs(parent).f_frsize >= total * 3 + RESERVE, 'INSUFFICIENT_SPACE')
        receipt = dict(format='asa-diagnostic-forensic-v1', status='PLAN',
                       toolSha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), files=sum('bytes' in v for v in before.values()),
                       scope='HOST_COLLECTOR_ONLY', excluded=['diagnostics/application'], sourceBytes=total, recoverySnapshotVerified=False, qualification='NOT_A_VERIFIED_RECOVERY_SNAPSHOT')
        receipt['requiresStoppedCollector'] = True
        if not apply:
            return receipt
        require(os.geteuid() == OWNER_UID, 'ROOT_REQUIRED')
        previous_umask = os.umask(0o077)
        quiescent(unit)
        import fcntl
        with opened(source, 'diagnostics/private/collector.lock') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Blocked('COLLECTOR_LOCK_BUSY') from None
            # Establish the copy baseline only after holding the same lock as the collector.
            require(inventory(source, host_scope=True) == before, 'SOURCE_RACE')
            stage_name = destination.name + '.partial-' + uuid.uuid4().hex
            os.mkdir(stage_name, mode=0o700, dir_fd=parent)
            stage = destination.parent / stage_name
            os.chmod(stage, 0o700)
            for relative, expected in before.items():
                if relative.endswith('/'):
                    (stage/'raw'/relative).mkdir(mode=0o700, parents=True, exist_ok=True)
                else:
                    copy_entry(source, relative, stage/'raw'/relative, expected)
            databases = [p for p in before if p.endswith(('.sqlite', '.sqlite3', '.db'))]
            require('diagnostics/private/index.sqlite' in databases, 'INDEX_REQUIRED')
            sqlite_backup(stage, databases)
            require(inventory(source, host_scope=True) == before, 'SOURCE_RACE')
            check = directory(source_path)
            try:
                require(os.fstat(check).st_ino == os.fstat(source).st_ino and
                        os.fstat(check).st_dev == os.fstat(source).st_dev, 'SOURCE_ROOT_CHANGED')
            finally:
                os.close(check)
            quiescent(unit)
            receipt.update(status='FORENSIC_COPY_ONLY', legacyDebt=debt_for_installation(stage))
            staged_fd = directory(stage)
            try:
                copies = inventory(staged_fd)
            finally:
                os.close(staged_fd)
            manifest = json.dumps({'source': before, 'copies': copies, 'receipt': receipt}, sort_keys=True).encode()
            private_write(stage/'manifest.private.json', manifest)
            receipt['manifestSha256'] = hashlib.sha256(manifest).hexdigest()
            private_write(stage/'receipt.json', json.dumps(receipt, sort_keys=True).encode())
            # Flush all directory entries before committing a no-overwrite final name.
            for path, dirs, _ in os.walk(stage, topdown=False):
                fd = directory(path)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            rename_new(parent, stage_name, destination.name)
            os.fsync(parent)
            return receipt
    finally:
        os.close(source)
        os.close(parent)
        if previous_umask is not None:
            os.umask(previous_umask)
        # Failed partial copies are retained privately, never promoted or erased.


def debt_for_installation(stage):
    # Paths in raw are relative to .asa; debt() expects a diagnostics-relative index.
    old = stage/'sqlite-backup'/(hashlib.sha256(b'diagnostics/private/index.sqlite').hexdigest()+'.sqlite')
    try:
        with contextlib.closing(sqlite3.connect('file:'+old.as_posix()+'?mode=ro', uri=True)) as db:
            a = db.execute("SELECT value FROM state WHERE key='retired:segments'").fetchone()
            b = db.execute("SELECT value FROM state WHERE key='retired:metadata'").fetchone()
            require(a and b and max(len(a[0]), len(b[0])) <= 16*1024**2, 'UNKNOWN_LEGACY_DEBT')
            return len(set(json.loads(a[0])) - (set(json.loads(b[0])) & {r[0] for r in db.execute('SELECT name FROM segment_verification')}))
    except (sqlite3.Error, ValueError, TypeError, Blocked):
        return 'UNKNOWN'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--installation', required=True)
    parser.add_argument('--destination', required=True)
    parser.add_argument('--collector-unit', default='asa-log-collector.service')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    try:
        result = snapshot(args.installation, args.destination, args.collector_unit, args.apply)
        print(json.dumps(result, sort_keys=True))
        return 0
    except Exception as exc:
        # Never print paths, raw payload, SQLite messages, config or key material.
        print(json.dumps({'status': 'BLOCKED', 'code': str(exc) if isinstance(exc, Blocked) else 'COPY_FAILED',
                          'recoverySnapshotVerified': False, 'partialMayExist': bool(args.apply)}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
