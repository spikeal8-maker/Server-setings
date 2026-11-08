#!/usr/bin/env python3
"""VM100 daily managed-settings copy; ciphertext verification, no unattended decrypt.

The six public files in this immutable release are pinned. The previously
accepted decrypt is a dated baseline, never a claim about a new nightly copy.
The PostgreSQL/MinIO lock is shared; no application services are paused here.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
from datetime import datetime, time as daytime, timezone
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import resource
import shutil
import signal
import socket
import stat
import sys
import tempfile
import time
import uuid
from zoneinfo import ZoneInfo

HOST = 'asa-school-01'
INSTANCE = 'fc0ba60fe1d946bba1694d73557cc11f'
PROJECT = 'asa-lab-school'
RELEASES = Path('/opt/asa-school-settings-backup/releases')
TARGET = Path('/var/backups/server-settings')
STATE = TARGET / 'school-nightly-settings'
DB_LOCK = Path('/run/lock/asa-school-nightly-db.lock')
CONFIG_LOCK = Path('/run/lock/asa-school-host-config-backup.lock')
DB_SCRIPT = Path('/srv/Server-setings/scripts/school-nightly-database-backup.py')
DB_SCRIPT_BLOB = 'db883469c107dc9d5459fec33b5a76e91bfe9fa5'
REMOTE = 'app:/School-VM100/Configuration-Backups'
MOSCOW = ZoneInfo('Europe/Moscow')
LOCK_WAIT_SECONDS = 600
TOTAL_SECONDS = 1800
RUNNER = 'school-nightly-settings-backup.py'
PINS = {
    'backup-settings.sh': '069a0955933bed86d0d12b8e3ea3ed657f83c90e23ce89df6477cc4831eedd36',
    'school-settings-cloud-roundtrip.py': 'bb55a78d088861fcc599ea011f5ab2310921c0c6d31f1d28e7e1664662b8541f',
    'school_settings_snapshot.py': '2a0b49e5816aa6d145c717b33de4061c7ea65e39c48d59d93db2b80cf93fbea2',
    'school_yandex_app_folder.py': '64ad5d3c9add88d7f754280e94f2f66b484892a97a4da53e3eba3d65ae46dc20',
    'cloud_storage.py': 'c6e30c3d1fa8aaa57e14c89b22c5677af5f5c5e132881925f2197949d7797d08',
}
BASELINE = {
    'acceptedAt': '2026-11-08T09:53:41Z',
    'archive': 'asa-school-config-01-20261108T092203Z-fc8b50ef.tar.age',
    'bytes': 25186504,
    'ciphertextSha256': 'e772ecf0e98b9624f8231e9e7887310fd7080fbd10ecc28e8502e5f992afdbd0',
    'status': 'CONFIG_CLOUD_DOWNLOAD_DECRYPT_ISOLATED_VERIFY_PASS',
    'automaticallyRevalidated': False,
}


class ScheduleError(Exception):
    pass


def need(value, code):
    if not value:
        raise ScheduleError(code)


def stamp():
    return datetime.now(timezone.utc).isoformat()


def trusted(path, *, directory=False, private=False):
    path = Path(path)
    for parent in (path, *path.parents):
        info = parent.lstat()
        need(info.st_uid == 0 and not stat.S_ISLNK(info.st_mode)
             and not info.st_mode & 0o022, 'SCHEDULE_PATH_UNTRUSTED')
    info = path.lstat()
    need(stat.S_ISDIR(info.st_mode) if directory else
         stat.S_ISREG(info.st_mode) and info.st_nlink == 1, 'SCHEDULE_PATH_TYPE')
    if private:
        need(stat.S_IMODE(info.st_mode) == (0o700 if directory else 0o600), 'SCHEDULE_PRIVATE_MODE')
    return info


def read(path, limit=128 * 1024):
    def signature(info):
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
                info.st_uid, info.st_gid, info.st_mode, info.st_nlink)
    info = trusted(path)
    need(info.st_size <= limit, 'SCHEDULE_READ_BOUND')
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as source:
        current = os.fstat(source.fileno())
        need(signature(current) == signature(info), 'SCHEDULE_READ_CHANGED')
        raw = source.read(limit + 1)
        final = os.fstat(source.fileno())
        need(len(raw) == info.st_size and signature(current) == signature(final)
             and signature(Path(path).lstat()) == signature(info), 'SCHEDULE_READ_CHANGED')
    return raw


def check_host():
    need(os.geteuid() == 0 and socket.gethostname() == HOST, 'VM100_ROOT_REQUIRED')
    identity = json.loads(read('/srv/asa-lab/.asa/installation.json'))
    need(identity.get('instanceId') == INSTANCE and identity.get('project') == PROJECT,
         'SCHOOL_INSTANCE_CHANGED')


def load_code():
    database_code = read(DB_SCRIPT)
    need(hashlib.sha1(b'blob ' + str(len(database_code)).encode() + b'\0' + database_code).hexdigest()
         == DB_SCRIPT_BLOB, 'DATABASE_INTERLOCK_CODE_CHANGED')
    release = Path(__file__).absolute().parent
    trusted(release, directory=True, private=True)
    need(release.parent == RELEASES and re.fullmatch(r'[a-f0-9]{16}', release.name), 'RELEASE_LOCATION')
    manifest = json.loads(read(release / 'release.json', 16384))
    need(set(manifest) == {'format', 'files'} and manifest['format'] == 'asa-school-settings-release-v1',
         'RELEASE_MANIFEST_FORMAT')
    files = manifest['files']
    need(isinstance(files, dict) and set(files) == set(PINS) | {RUNNER}, 'RELEASE_FILE_SET')
    need(all(files.get(name) == expected for name, expected in PINS.items()), 'RELEASE_DEPENDENCY_PINS')
    for name, expected in files.items():
        trusted(release / name, private=True)
        need(hashlib.sha256(read(release / name)).hexdigest() == expected, 'RELEASE_CODE_CHANGED')
    release_hash = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    need(release.name == release_hash[:16], 'RELEASE_ID_CHANGED')
    sys.path.insert(0, str(release))
    import school_settings_snapshot as snapshot
    spec = importlib.util.spec_from_file_location('school_settings_cloud', release / 'school-settings-cloud-roundtrip.py')
    cloud = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cloud)
    return snapshot, cloud, release_hash


def atomic_receipt(name, value):
    need(name in ('latest-attempt.json', 'latest-success.json', 'latest-failure.json'), 'RECEIPT_NAME')
    trusted(STATE, directory=True, private=True)
    target = STATE / name
    if target.exists() or target.is_symlink():
        trusted(target, private=True)
    raw = (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode()
    need(len(raw) <= 32768, 'RECEIPT_SIZE')
    descriptor, temporary = tempfile.mkstemp(prefix='.receipt-', dir=STATE)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        directory = os.open(STATE, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def in_window(value=None):
    value = value or datetime.now(MOSCOW)
    need(value.tzinfo is not None, 'CLOCK_TIMEZONE_REQUIRED')
    current = value.astimezone(MOSCOW).time().replace(tzinfo=None)
    return daytime(4, 0) <= current < daytime(5, 0)


@contextmanager
def lock(path, wait_seconds):
    # /run/lock may be root-owned sticky 1777; verify the exact opened inode.
    parent = path.parent.lstat()
    need(stat.S_ISDIR(parent.st_mode) and parent.st_uid == 0 and
         (not parent.st_mode & 0o022 or parent.st_mode & stat.S_ISVTX), 'LOCK_PARENT_UNTRUSTED')
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        need(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1
             and stat.S_IMODE(info.st_mode) == 0o600, 'LOCK_INODE_UNTRUSTED')
        deadline = time.monotonic() + wait_seconds
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                need(time.monotonic() < deadline, 'DATABASE_OR_CONFIG_LOCK_BUSY')
                time.sleep(min(1.0, max(0, deadline - time.monotonic())))
        current = path.lstat()
        need((current.st_dev, current.st_ino) == (info.st_dev, info.st_ino), 'LOCK_PATH_CHANGED')
        yield
    finally:
        os.close(descriptor)


def safe_code(exc):
    value = str(exc) if type(exc).__name__ in ('ScheduleError', 'SnapshotError', 'BackupTransportError') else type(exc).__name__
    return value if re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,96}', value) else type(exc).__name__


def stop_signal(number, frame):
    raise ScheduleError('RUN_TOTAL_TIMEOUT' if number == signal.SIGALRM else 'SERVICE_TERMINATED')


def execute():
    started = time.monotonic()
    check_host()
    trusted(STATE, directory=True, private=True)
    result = {'status': 'IN_PROGRESS', 'startedAt': stamp(), 'trigger': 'scheduled',
              'project': PROJECT, 'remoteDirectory': REMOTE, 'archive': None,
              'baselineIsolatedRestore': BASELINE, 'nightlyRestoreValidated': False,
              'coverageComplete': False, 'gatewayOAuthContinuity': False}
    try:
        atomic_receipt('latest-attempt.json', result)
        print(json.dumps(result, sort_keys=True), flush=True)
        need(in_window(), 'ONLY_SCHEDULED_0400_TO_0500_MOSCOW')
        snapshot, cloud, release_hash = load_code()
        result['releaseSha256'] = release_hash
        with ExitStack() as locks:
            locks.enter_context(lock(DB_LOCK, LOCK_WAIT_SECONDS))
            locks.enter_context(lock(CONFIG_LOCK, 0))
            need(in_window(), 'SETTINGS_NIGHT_WINDOW_EXPIRED')
            name = 'asa-school-config-01-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8] + '.tar.age'
            result['archive'] = name
            atomic_receipt('latest-attempt.json', result)
            created = snapshot.create(name)
            need(created.get('status') == 'CONFIG_ARCHIVE_CREATED_NOT_CLOUD_VERIFIED'
                 and created.get('archive') == name and created.get('coverageComplete') is False
                 and created.get('sqliteDatabases') == 3, 'CREATED_RECEIPT_INVALID')
            cloud.school_configuration_transfer(name)
            transferred = json.loads(read(TARGET / (name + '.cloud.json')))
            need(transferred.get('status') == 'CONFIG_UPLOAD_FULL_DOWNLOAD_PASS_DECRYPT_PENDING'
                 and transferred.get('archive') == name and transferred.get('remoteDirectory') == REMOTE
                 and transferred.get('bytes') == created.get('bytes')
                 and transferred.get('ciphertextSha256') == created.get('ciphertextSha256')
                 and transferred.get('fullDownloadVerified') is True
                 and transferred.get('restoreValidated') is False, 'TRANSFER_RECEIPT_INVALID')
            size, checksum = snapshot.verify_cipher(snapshot.RECOVERY / 'incoming' / name)
            need((size, checksum) == (created['bytes'], created['ciphertextSha256']), 'FINAL_DOWNLOADED_CIPHER_CHANGED')
            result.update(status='CONFIG_NIGHTLY_FULL_DOWNLOAD_SHA256_PASS', bytes=size,
                          ciphertextSha256=checksum, fullDownloadVerified=True,
                          configurationFiles=created['configurationFiles'], sqliteDatabases=3,
                          normalizedLogs=created['normalizedLogs'], finishedAt=stamp(),
                          elapsedSeconds=round(time.monotonic() - started, 3),
                          freeBytes=shutil.disk_usage(TARGET).free)
            atomic_receipt('latest-success.json', result)
            atomic_receipt('latest-attempt.json', result)
        print(json.dumps(result, sort_keys=True), flush=True)
        return 0
    except Exception as exc:
        result.update(status='CONFIG_NIGHTLY_STOP', code=safe_code(exc), finishedAt=stamp(),
                      elapsedSeconds=round(time.monotonic() - started, 3), fullDownloadVerified=False)
        try:
            atomic_receipt('latest-failure.json', result)
            atomic_receipt('latest-attempt.json', result)
            result['failureReceiptPersisted'] = True
        except Exception:
            result['failureReceiptPersisted'] = False
        # systemd strips the <3> prefix and records PRIORITY=3; the existing
        # linux:journal collector includes warning/error records from all units.
        print('<3>' + json.dumps(result, sort_keys=True), file=sys.stderr, flush=True)
        return 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--apply', action='store_true')
    action.add_argument('--check', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    if args.apply:
        signal.signal(signal.SIGALRM, stop_signal)
        signal.signal(signal.SIGTERM, stop_signal)
        signal.alarm(TOTAL_SECONDS)
        try:
            return execute()
        finally:
            signal.alarm(0)
    if args.check:
        check_host()
        trusted(STATE, directory=True, private=True)
        snapshot, cloud, release_hash = load_code()
        snapshot.check_host()
        readiness = snapshot.log_preflight(snapshot.api_runtime())
        need(readiness.get('ready') is True, 'CATALOGS_NOT_READABLE')
        print(json.dumps({'status': 'CONFIG_SCHEDULE_READONLY_CHECK_PASS', 'releaseSha256': release_hash,
                          'catalogsReady': True, 'changes': False, 'baselineIsolatedRestore': BASELINE}, sort_keys=True))
        return 0
    print(json.dumps({'status': 'PLAN_ONLY', 'schedule': '04:00 Europe/Moscow',
                      'sharedDatabaseLock': str(DB_LOCK), 'lockWaitSeconds': LOCK_WAIT_SECONDS,
                      'maximumRunSeconds': TOTAL_SECONDS, 'remoteDirectory': REMOTE,
                      'nightlyRestoreValidated': False, 'baselineIsolatedRestore': BASELINE,
                      'deletions': False, 'changes': False}, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.dont_write_bytecode = True
    try:
        raise SystemExit(main())
    except Exception as exc:
        print('<3>' + json.dumps({'status': 'CONFIG_SCHEDULE_STOP', 'code': safe_code(exc),
                                 'failureReceiptPersisted': False}), file=sys.stderr)
        raise SystemExit(2)
