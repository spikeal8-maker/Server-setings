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
BASE_DIGEST_FILES = ('collect_logs.py', 'collect-windows-logs.ps1', 'resource_sampling.py', 'memory_sampling.py')
# Closed reviewed source contracts, never probe/fallback on a missing fifth file.
DIGEST_CONTRACTS = {
    'dfee5badd299d318a8e51ff0cbf88e9656333a5d': BASE_DIGEST_FILES,
    '3e32de6987665c4d7c1d33ba7ba82019afdf9cdb': (*BASE_DIGEST_FILES, 'deployment/operational.py'),
}


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


def owner_account(name):
    import pwd
    return pwd.getpwnam(name)


def validate_registration(root, settings, runner, unit):
    project = settings.get('project')
    require(isinstance(project, str) and re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,62}', project), 'RUNNER_PROJECT_INVALID')
    require(type(runner.get('version')) is int and runner.get('version') == 1 and runner.get('root') == str(root) and runner.get('project') == project,
            'RUNNER_INSTALLATION_MISMATCH')
    mode, owner = runner.get('mode'), runner.get('owner')
    require(mode in ('user', 'system', 'system-root'), 'RUNNER_MANAGER_UNKNOWN')
    require(isinstance(owner, str) and re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', owner), 'RUNNER_OWNER_INVALID')
    try:
        account = owner_account(owner)
    except KeyError:
        raise Blocked('RUNNER_OWNER_MISSING') from None
    require(type(account.pw_uid) is int and type(account.pw_gid) is int and account.pw_uid >= 0 and account.pw_gid >= 0,
            'RUNNER_UID_MISSING')
    require(mode == 'system-root' and owner == 'root' and account.pw_uid == 0 and settings.get('collectorMode') == 'system-root'
            or mode in ('system', 'user') and account.pw_uid > 0 and account.pw_gid > 0 and settings.get('collectorMode') != 'system-root',
            'RUNNER_OWNER_MODE_MISMATCH')
    require(type(runner.get('ownerUid', account.pw_uid)) is int and runner.get('ownerUid', account.pw_uid) == account.pw_uid, 'RUNNER_UID_MISMATCH')
    unit = runner.get('name') if unit is None else unit
    require(unit == runner.get('name') and unit in ('asa-log-collector.service', 'asa-log-collector-'+project+'.service'),
            'RUNNER_UNIT_MISMATCH')
    require(isinstance(runner.get('scriptDigest'), str) and re.fullmatch(r'[a-f0-9]{64}', runner['scriptDigest']),
            'RUNNER_DIGEST_INVALID')
    require(runner.get('revision') in DIGEST_CONTRACTS, 'RUNNER_REVISION_CONTRACT_UNSUPPORTED')
    return dict(revision=runner['revision'], mode=mode, owner=owner, uid=account.pw_uid, gid=account.pw_gid, name=unit, root=str(root),
                scriptDigest=runner['scriptDigest'])


def registered_runner(installation, unit):
    root = Path(installation)
    fd = directory(root/'.asa')
    try:
        values, metadata = [], []
        for name in ('installation.json', 'collector-runner.json'):
            with opened(fd, name) as source:
                info = os.fstat(source)
                require(info.st_size <= 1024**2 and info.st_nlink == 1, 'RUNNER_REGISTRATION_UNSAFE')
                data = os.read(source, 1024**2+1)
                require(signature(os.fstat(source)) == signature(info), 'RUNNER_REGISTRATION_RACE')
                values.append(json.loads(data)); metadata.append(info)
        identity = validate_registration(root, values[0], values[1], unit)
        require(all(s.st_uid in (0, identity['uid']) and not s.st_mode & 0o022 for s in metadata), 'RUNNER_REGISTRATION_OWNER')
    finally:
        os.close(fd)
    tools = directory(root/'tools')
    try:
        digest = hashlib.sha256()
        for name in DIGEST_CONTRACTS[identity['revision']]:
            digest.update(name.encode())
            with opened(tools, name) as stream:
                info = os.fstat(stream)
                require(info.st_size <= 8*1024**2, 'RUNNER_SOURCE_LIMIT')
                while data := os.read(stream, 1024**2): digest.update(data)
                require(signature(os.fstat(stream)) == signature(info), 'RUNNER_SOURCE_RACE')
        require(digest.hexdigest() == identity['scriptDigest'], 'RUNNER_REGISTRATION_STALE')
    finally:
        os.close(tools)
    return identity


def user_manager_prefix(identity):
    uid, gid = identity['uid'], identity['gid']
    runtime = Path('/run/user')/str(uid)
    fd = directory(runtime)
    try:
        info = os.fstat(fd)
        bus = os.stat('bus', dir_fd=fd, follow_symlinks=False)
        require(info.st_uid == uid and stat.S_IMODE(info.st_mode) == 0o700 and
                stat.S_ISSOCK(bus.st_mode) and bus.st_uid == uid, 'USER_BUS_UNTRUSTED')
    finally:
        os.close(fd)
    query = subprocess.run(['/usr/bin/loginctl', 'show-user', str(uid),
                            '--property=Linger', '--property=RuntimePath', '--property=State'],
                           capture_output=True, text=True, timeout=10)
    values = dict(line.split('=', 1) for line in query.stdout.splitlines() if '=' in line)
    require(query.returncode == 0 and values.get('Linger') == 'yes' and values.get('RuntimePath') == str(runtime)
            and values.get('State') in ('active', 'lingering'), 'USER_MANAGER_UNAVAILABLE')
    # Drop UID without a shell/PAM session or implicit user-manager activation.
    return ['/usr/bin/setpriv', '--reuid', str(uid), '--regid', str(gid), '--clear-groups', '--no-new-privs',
            '/usr/bin/env', '-i', 'PATH=/usr/bin:/bin', 'XDG_RUNTIME_DIR='+str(runtime),
            'DBUS_SESSION_BUS_ADDRESS=unix:path='+str(runtime/'bus'), '/usr/bin/systemctl', '--user']


def quiescent(installation, unit):
    identity = registered_runner(installation, unit)
    unit = identity['name']
    prefix = user_manager_prefix(identity) if identity['mode'] == 'user' else ['/usr/bin/systemctl', '--system']
    for name in (unit, unit[:-8] + '.timer'):
        p = subprocess.run(prefix+['show', name, '--property=LoadState,ActiveState,MainPID,WorkingDirectory,User'],
                           capture_output=True, text=True, timeout=10)
        values = dict(line.split('=', 1) for line in p.stdout.splitlines() if '=' in line)
        absent_timer = name.endswith('.timer') and values.get('LoadState') == 'not-found'
        require(absent_timer or (p.returncode == 0 and values.get('LoadState') == 'loaded'
                and values.get('ActiveState') in ('inactive', 'failed')
                and (values.get('MainPID', '0') if name.endswith('.timer') else values.get('MainPID')) == '0'),
                'COLLECTOR_NOT_QUIESCENT')
        if name == unit:
            require(values.get('WorkingDirectory') == identity['root'] and
                    values.get('User') == (identity['owner'] if identity['mode'] != 'user' else ''),
                    'LOADED_RUNNER_IDENTITY_MISMATCH')


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


def snapshot(installation, destination, unit=None, apply=False):
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
                    'diagnostics/private/segment-verification.key', 'diagnostics/store/catalog.json', 'log-collector.json',
                    'collector-runner.json', 'installation.json')
        require(all(p in before for p in required), 'REQUIRED_FORENSIC_COMPONENT_MISSING')
        identity = registered_runner(installation, unit)
        total = sum(v.get('bytes', 0) for v in before.values())
        require(os.fstatvfs(parent).f_favail >= len(before)*3+32, 'INSUFFICIENT_INODES')
        require(os.fstatvfs(parent).f_bavail * os.fstatvfs(parent).f_frsize >= total * 3 + RESERVE, 'INSUFFICIENT_SPACE')
        receipt = dict(format='asa-diagnostic-forensic-v1', status='PLAN',
                       toolSha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), files=sum('bytes' in v for v in before.values()),
                       scope='HOST_COLLECTOR_ONLY', excluded=['diagnostics/application'], sourceBytes=total, recoverySnapshotVerified=False, qualification='NOT_A_VERIFIED_RECOVERY_SNAPSHOT')
        receipt['runnerMode'] = identity['mode']
        receipt['runnerSourceRevision'] = identity['revision']
        receipt['requiresStoppedCollector'] = True
        if not apply:
            return receipt
        require(os.geteuid() == OWNER_UID, 'ROOT_REQUIRED')
        previous_umask = os.umask(0o077)
        quiescent(installation, unit)
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
            quiescent(installation, unit)
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
    parser.add_argument('--collector-unit', help='Optional exact assertion; default is the registered canonical unit')
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
