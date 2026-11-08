#!/usr/bin/env python3
"""Install only the reviewed VM100 daily settings timer, without running a backup."""
from __future__ import annotations

FILES = {'backup-settings.sh': '069a0955933bed86d0d12b8e3ea3ed657f83c90e23ce89df6477cc4831eedd36', 'school-settings-cloud-roundtrip.py': 'bb55a78d088861fcc599ea011f5ab2310921c0c6d31f1d28e7e1664662b8541f', 'school_settings_snapshot.py': '2a0b49e5816aa6d145c717b33de4061c7ea65e39c48d59d93db2b80cf93fbea2', 'school_yandex_app_folder.py': '64ad5d3c9add88d7f754280e94f2f66b484892a97a4da53e3eba3d65ae46dc20', 'cloud_storage.py': 'c6e30c3d1fa8aaa57e14c89b22c5677af5f5c5e132881925f2197949d7797d08', 'school-nightly-settings-backup.py': '8a4ba48ff6119bbf24305a134a7ba722672fffd8ccf7fef7835fa8928fd84c38'}
RELEASE_HASH = '52887551f6bf27e9f73ff2ba6b754f082b2bbec19dd4ef59813ce287ab7b04e0'
RUNNER = 'school-nightly-settings-backup.py'

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys
from zoneinfo import ZoneInfo

NAME = 'asa-school-nightly-settings-backup'
UNIT_ROOT = Path('/etc/systemd/system')
SERVICE = UNIT_ROOT / (NAME + '.service')
TIMER = UNIT_ROOT / (NAME + '.timer')
PREPARED = Path('/var/lib/server-settings/config-backup-prepared/74964b80cfbac789')
BASE = Path('/opt/asa-school-settings-backup')
RELEASE = BASE / 'releases' / RELEASE_HASH[:16]
STATE = Path('/var/backups/server-settings/school-nightly-settings')
SCHEDULE = '*-*-* 04:00:00 Europe/Moscow'
SERVICE_BODY = '''[Unit]
Description=ASA School managed settings and retained normalized logs backup
Wants=network-online.target
After=network-online.target docker.service
Requires=docker.service

[Service]
Type=oneshot
User=root
Group=root
WorkingDirectory=/srv/asa-lab
Environment=PYTHONDONTWRITEBYTECODE=1
UMask=0077
ExecStart=/usr/bin/python3 -B ''' + str(RELEASE / RUNNER) + ''' --apply
Nice=10
IOSchedulingClass=best-effort
IOSchedulingPriority=7
TimeoutStartSec=1900
TimeoutStopSec=45
KillMode=control-group
StandardOutput=journal
StandardError=journal
SyslogIdentifier=asa-school-nightly-settings-backup
SyslogLevel=info
SyslogLevelPrefix=yes
NoNewPrivileges=yes
PrivateTmp=yes
'''
TIMER_BODY = '''[Unit]
Description=ASA School settings backup daily at 04:00 Moscow

[Timer]
OnCalendar=*-*-* 04:00:00 Europe/Moscow
Persistent=false
RandomizedDelaySec=0
AccuracySec=1min
Unit=asa-school-nightly-settings-backup.service

[Install]
WantedBy=timers.target
'''


class InstallError(Exception):
    pass


def need(value, code):
    if not value:
        raise InstallError(code)


def read(path):
    path = Path(path)
    for parent in (path, *path.parents):
        info = parent.lstat()
        need(info.st_uid == 0 and not stat.S_ISLNK(info.st_mode)
             and not info.st_mode & 0o022, 'INSTALL_SOURCE_UNTRUSTED')
    info = path.lstat()
    need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= 128 * 1024,
         'INSTALL_SOURCE_BOUND')
    raw = path.read_bytes()
    final = path.lstat()
    need(len(raw) == info.st_size and (final.st_ino, final.st_mtime_ns, final.st_ctime_ns) ==
         (info.st_ino, info.st_mtime_ns, info.st_ctime_ns), 'INSTALL_SOURCE_CHANGED')
    return raw


def run(arguments, timeout=15):
    environment = dict(os.environ, LC_ALL='C', TZ='UTC', PYTHONDONTWRITEBYTECODE='1')
    result = subprocess.run(arguments, stdin=subprocess.DEVNULL, capture_output=True,
                            text=True, timeout=timeout, check=False, env=environment)
    need(result.returncode == 0 and len(result.stdout) <= 65536, 'INSTALL_COMMAND_' + Path(arguments[0]).name.upper().replace('-', '_'))
    return result.stdout


def properties(name, fields):
    raw = run(['/usr/bin/systemctl', 'show', name, '--property=' + ','.join(fields)])
    return dict(line.split('=', 1) for line in raw.splitlines() if '=' in line)


def directory(path):
    if not path.exists():
        parent = path.parent.lstat()
        need(stat.S_ISDIR(parent.st_mode) and parent.st_uid == 0 and not parent.st_mode & 0o022,
             'INSTALL_PARENT_UNTRUSTED')
        path.mkdir(mode=0o700)
    info = path.lstat()
    need(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o700,
         'INSTALL_DIRECTORY_MODE')


def write_once(path, data, mode=0o600):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(descriptor, 'wb') as target:
        os.fchmod(target.fileno(), mode)
        target.write(data)
        target.flush()
        os.fsync(target.fileno())


def next_fire():
    now = datetime.now(timezone.utc)
    local = now.astimezone(ZoneInfo('Europe/Moscow'))
    following = local.replace(hour=4, minute=0, second=0, microsecond=0)
    if following <= local:
        following += timedelta(days=1)
    following = following.astimezone(timezone.utc)
    need((following - now).total_seconds() >= 600, 'INSTALL_TOO_CLOSE_TO_SETTINGS_SCHEDULE')
    return following


def preflight(repository_source=False):
    need(os.geteuid() == 0 and socket.gethostname() == 'asa-school-01', 'VM100_ROOT_REQUIRED')
    identity = json.loads(read('/srv/asa-lab/.asa/installation.json'))
    need(identity.get('instanceId') == 'fc0ba60fe1d946bba1694d73557cc11f'
         and identity.get('project') == 'asa-lab-school', 'SCHOOL_INSTANCE_CHANGED')
    source = Path(__file__).absolute().parent
    source_files = {name: read((source if repository_source or name == RUNNER else PREPARED) / name)
                    for name in FILES}
    need(all(hashlib.sha256(source_files[name]).hexdigest() == expected for name, expected in FILES.items()),
         'PINNED_INSTALL_CODE_CHANGED')
    actual_hash = hashlib.sha256(json.dumps(FILES, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    need(actual_hash == RELEASE_HASH, 'RELEASE_MANIFEST_PIN_CHANGED')
    database = read('/srv/Server-setings/scripts/school-nightly-database-backup.py')
    need(hashlib.sha1(b'blob ' + str(len(database)).encode() + b'\0' + database).hexdigest()
         == 'db883469c107dc9d5459fec33b5a76e91bfe9fa5', 'DATABASE_INTERLOCK_CODE_CHANGED')
    db_service = read(UNIT_ROOT / 'asa-school-nightly-database-backup.service').decode('utf-8')
    db_timer = read(UNIT_ROOT / 'asa-school-nightly-database-backup.timer').decode('utf-8')
    need(db_service.count('ExecStart=') == 1 and
         'ExecStart=/usr/bin/python3 /srv/Server-setings/scripts/school-nightly-database-backup.py --apply\n' in db_service,
         'DATABASE_SERVICE_CHANGED')
    need(db_timer.count('OnCalendar=') == 1 and 'OnCalendar=*-*-* 03:20:00 Europe/Moscow\n' in db_timer,
         'DATABASE_TIMER_CHANGED')
    db = properties('asa-school-nightly-database-backup.timer', ('LoadState', 'UnitFileState', 'ActiveState'))
    need(db.get('LoadState') == 'loaded' and db.get('UnitFileState') == 'enabled'
         and db.get('ActiveState') == 'active', 'DATABASE_TIMER_NOT_ENABLED_ACTIVE')
    for unit in (SERVICE, TIMER):
        dropin = Path(str(unit) + '.d')
        need(not unit.exists() and not unit.is_symlink() and not dropin.exists()
             and not dropin.is_symlink(), 'NEW_UNIT_PATH_EXISTS')
        existing = properties(unit.name, ('LoadState', 'ActiveState', 'FragmentPath'))
        need(existing.get('LoadState') == 'not-found' and existing.get('ActiveState') == 'inactive'
             and not existing.get('FragmentPath'), 'SETTINGS_UNIT_ALREADY_REGISTERED')
    wants = UNIT_ROOT / 'timers.target.wants' / TIMER.name
    need(not wants.exists() and not wants.is_symlink(), 'NEW_TIMER_WANTS_EXISTS')
    if wants.parent.exists() or wants.parent.is_symlink():
        parent = wants.parent.lstat()
        need(stat.S_ISDIR(parent.st_mode) and parent.st_uid == 0 and not parent.st_mode & 0o022,
             'TIMER_WANTS_PARENT_UNTRUSTED')
    need(not RELEASE.exists() and not RELEASE.is_symlink(), 'IMMUTABLE_RELEASE_ALREADY_EXISTS')
    need(not (STATE / 'install.json').exists() and not (STATE / 'install.json').is_symlink(), 'INSTALL_RECEIPT_EXISTS')
    proof = json.loads(read('/var/backups/server-settings/asa-school-config-01-20261108T092203Z-fc8b50ef.tar.age.restore.json'))
    need(proof.get('status') == 'CONFIG_CLOUD_DOWNLOAD_DECRYPT_ISOLATED_VERIFY_PASS'
         and proof.get('archive') == 'asa-school-config-01-20261108T092203Z-fc8b50ef.tar.age'
         and proof.get('bytes') == 25186504
         and proof.get('ciphertextSha256') == 'e772ecf0e98b9624f8231e9e7887310fd7080fbd10ecc28e8502e5f992afdbd0'
         and proof.get('fileCount') == 59 and proof.get('manifestVerified') is True
         and proof.get('sqliteIntegrity') == {'core.db': 'ok', 'agent.db': 'ok', 'xpack.db': 'ok'}
         and proof.get('coverageComplete') is False and proof.get('livePathsWritten') is False
         and proof.get('permissionsManifestValidated') is True and proof.get('gatewayOAuthContinuity') is False,
         'ACCEPTED_ISOLATED_RESTORE_PROOF_MISSING')
    for kind in ('host', 'application'):
        need(proof.get('normalizedLogs', {}).get(kind, {}).get('crcAndPartSha256Verified') is True,
             'ACCEPTED_NORMALIZED_ZIP_PROOF_MISSING')
    calendar = run(['/usr/bin/systemd-analyze', 'calendar', SCHEDULE], timeout=10)
    need(SCHEDULE in calendar, 'SETTINGS_CALENDAR_NOT_RECOGNIZED')
    next_fire()
    return source_files


def install(repository_source=False):
    files = preflight(repository_source)
    activated = False
    try:
        for path in (BASE, BASE / 'releases', RELEASE, STATE):
            directory(path)
        for name, content in files.items():
            write_once(RELEASE / name, content)
        write_once(RELEASE / 'release.json', (json.dumps({'format': 'asa-school-settings-release-v1', 'files': FILES}, sort_keys=True) + '\n').encode())
        check = json.loads(run(['/usr/bin/python3', '-B', str(RELEASE / RUNNER), '--check'], timeout=75))
        need(check.get('status') == 'CONFIG_SCHEDULE_READONLY_CHECK_PASS'
             and check.get('releaseSha256') == RELEASE_HASH and check.get('changes') is False,
             'INSTALLED_READONLY_CHECK_FAILED')
        write_once(SERVICE, SERVICE_BODY.encode(), 0o644)
        write_once(TIMER, TIMER_BODY.encode(), 0o644)
        run(['/usr/bin/systemd-analyze', 'verify', str(SERVICE), str(TIMER)], timeout=20)
        run(['/usr/bin/systemctl', 'daemon-reload'])
        expected_next = next_fire()
        activated = True
        run(['/usr/bin/systemctl', 'enable', '--now', TIMER.name], timeout=20)
        timer = properties(TIMER.name, ('LoadState', 'UnitFileState', 'ActiveState', 'NextElapseUSecRealtime'))
        need(timer.get('LoadState') == 'loaded' and timer.get('UnitFileState') == 'enabled'
             and timer.get('ActiveState') == 'active', 'SETTINGS_TIMER_NOT_ENABLED_ACTIVE')
        actual_next = datetime.strptime(timer.get('NextElapseUSecRealtime', ''), '%a %Y-%m-%d %H:%M:%S UTC').replace(tzinfo=timezone.utc)
        need(actual_next == expected_next, 'SETTINGS_TIMER_NEXT_FIRE_MISMATCH')
        service = properties(SERVICE.name, ('ActiveState', 'SubState', 'ExecMainStartTimestampMonotonic'))
        need(service.get('ActiveState') == 'inactive' and service.get('SubState') == 'dead'
             and service.get('ExecMainStartTimestampMonotonic') == '0', 'SETTINGS_SERVICE_UNEXPECTEDLY_STARTED')
        need(read(SERVICE) == SERVICE_BODY.encode() and read(TIMER) == TIMER_BODY.encode(), 'INSTALLED_UNIT_CHANGED')
        result = {'status': 'CONFIG_DAILY_TIMER_INSTALLED_NOT_RUN', 'installedAt': datetime.now(timezone.utc).isoformat(),
                  'unit': NAME, 'release': str(RELEASE), 'releaseSha256': RELEASE_HASH,
                  'schedule': SCHEDULE, 'nextFireUtc': actual_next.isoformat(), 'enabled': True, 'active': True,
                  'persistentCatchup': False, 'databaseTimerChanged': False, 'backupRanDuringInstall': False,
                  'latestAttempt': str(STATE / 'latest-attempt.json'), 'latestSuccess': str(STATE / 'latest-success.json'),
                  'latestFailure': str(STATE / 'latest-failure.json'), 'journalUnit': SERVICE.name,
                  'collectorSourceAdded': False, 'failureJournalPriority': 3,
                  'existingCollectorSource': 'linux:journal', 'retentionDeletes': False, 'offlineKeyInstalled': False,
                  'nightlyRestoreValidated': False, 'baselineIsolatedRestoreAcceptedAt': '2026-11-08T09:53:41Z',
                  'firstArchiveContainsSchedulerCodeOrUnits': False, 'schedulerRecoverySource': 'pinned Server-setings scripts'}
        write_once(STATE / 'install.json', (json.dumps(result, sort_keys=True) + '\n').encode())
        print(json.dumps(result, sort_keys=True), flush=True)
        return 0
    except Exception as exc:
        stopped = None
        if activated:
            try:
                run(['/usr/bin/systemctl', 'disable', '--now', TIMER.name], timeout=20)
                after = properties(TIMER.name, ('ActiveState', 'UnitFileState'))
                stopped = after.get('ActiveState') == 'inactive' and after.get('UnitFileState') == 'disabled'
            except Exception:
                stopped = False
        code = str(exc) if isinstance(exc, InstallError) else type(exc).__name__
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,96}', code):
            code = type(exc).__name__
        print(json.dumps({'status': 'CONFIG_TIMER_INSTALL_STOP', 'code': code,
                          'timerDeactivatedAfterFailure': stopped, 'createdFilesRetainedForReview': True,
                          'archivesDeleted': False}), file=sys.stderr, flush=True)
        return 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--repository-source', action='store_true',
                        help='use these exact pinned sibling source files after a reviewed recovery checkout')
    args = parser.parse_args()
    os.umask(0o077)
    if args.apply:
        return install(args.repository_source)
    preflight(args.repository_source)
    print(json.dumps({'status': 'CONFIG_TIMER_READONLY_PREFLIGHT_PASS', 'releaseSha256': RELEASE_HASH,
                      'changes': False, 'schedule': SCHEDULE, 'source': 'repository' if args.repository_source else 'prepared-v2'}))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:
        code = str(exc) if isinstance(exc, InstallError) else type(exc).__name__
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,96}', code):
            code = type(exc).__name__
        print(json.dumps({'status': 'CONFIG_TIMER_PREFLIGHT_STOP', 'code': code, 'changes': False}), file=sys.stderr)
        raise SystemExit(2)
