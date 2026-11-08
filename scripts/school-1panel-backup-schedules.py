#!/usr/bin/env python3
"""Native 1Panel Cron -> Supervisor backups for the reviewed school VM.

No backup is run by stage/probe/activate. A staged probe exercises both native
dispatchers with the backup gate closed. A real run is a separate, durable
Supervisor process, so cancelling/timing out its Cron observer cannot kill the
database exporter while writers are paused. Existing runners and archives are
not rewritten. No command in this program stops a running backup process.
"""
from __future__ import annotations

import argparse
import configparser
from contextlib import contextmanager
import ctypes
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from zoneinfo import ZoneInfo

HOST = 'asa-school-01'
INSTANCE = 'fc0ba60fe1d946bba1694d73557cc11f'
PROJECT = 'asa-lab-school'
APP = Path('/srv/asa-lab')
NAME = 'school-1panel-backup-schedules.py'
FORMAT = 'asa-school-native-backup-schedules-v1'
BASE = Path('/opt/asa-school-panel-schedules')
STATE = Path('/var/lib/server-settings/1panel-backup-schedules')
LOCK = Path('/run/lock/asa-school-panel-schedules.lock')
DB_LOCK = Path('/run/lock/asa-school-nightly-db.lock')
PANEL = 'http://127.0.0.1:19192'
PANEL_VERSION = 'v2.3.2'
PANEL_BINARY_ALIAS = Path('/usr/bin/1panel')
PANEL_BINARY_TARGET = Path('/usr/local/bin/1panel-core')
MOSCOW = ZoneInfo('Europe/Moscow')
KINDS = ('database', 'settings')
COLLECTOR = 'asa-log-collector-asa-lab-school'
SETTINGS_BASE = Path('/opt/asa-school-settings-backup/releases')
SETTINGS_FILES = {
    'backup-settings.sh': '069a0955933bed86d0d12b8e3ea3ed657f83c90e23ce89df6477cc4831eedd36',
    'school-settings-cloud-roundtrip.py': 'bb55a78d088861fcc599ea011f5ab2310921c0c6d31f1d28e7e1664662b8541f',
    'school_settings_snapshot.py': '9a7719c5705fb6883857b0c25be1e19f9ee7f3009c8594a43ca15d04d0bffbe2',
    'school_yandex_app_folder.py': '64ad5d3c9add88d7f754280e94f2f66b484892a97a4da53e3eba3d65ae46dc20',
    'cloud_storage.py': 'c6e30c3d1fa8aaa57e14c89b22c5677af5f5c5e132881925f2197949d7797d08',
    'school-nightly-settings-backup.py': 'fd27a26dba0850663010501ad4456a494bf0203e4974c288a59c3e0474b0352b',
}
LEGACY_PINS = {
    'database': {
        'service': 'ea635ce9d89ec4387f4479372101304e1fd93e3a4cd36c6606e02a8ecdea6ae2',
        'timer': '581928a86a62cbbc2c9f345f729400fe2cecb0d26df53b7cf70a9ee93812f439'},
    'settings': {
        'service': ('f213e01a07157b2cf4d46a1e7bd776367539aa4f046a67626eddb556cc128c2c',
                    'aeb07a2d2d99cced1ce48db5801eb615643324a896626a224dafef7840410105',
                    '6ad097408388a6b5dbb551cce6484438c41b46680fad6fe681b40242ef7ac997'),
        'timer': '170aeb53744297ef04abe45408993b464008a38569e46ddb78889efabec2bb92'},
}
DATABASE_SCRIPT = Path('/srv/Server-setings/scripts/school-nightly-database-backup.py')
DATABASE_BLOB = 'db883469c107dc9d5459fec33b5a76e91bfe9fa5'
RUNNER_RESULT = {
    'database': Path('/var/backups/asa-school-nightly/latest-success.json'),
    'settings': Path('/var/backups/server-settings/school-nightly-settings/latest-success.json'),
}
INACTIVE = ('STOPPED', 'EXITED')
ACTIVE = ('STARTING', 'RUNNING', 'STOPPING', 'BACKOFF')


class ScheduleError(Exception):
    pass


def need(value, code):
    if not value:
        raise ScheduleError(code)


def stamp():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def safe_code(error):
    value = str(error) if isinstance(error, ScheduleError) else type(error).__name__
    return value if re.fullmatch(r'[A-Z][A-Z0-9_]{0,95}', value) else type(error).__name__


def unit(kind, extension='timer'):
    need(kind in KINDS and extension in ('timer', 'service'), 'UNIT_SCOPE')
    return 'asa-school-nightly-' + kind + '-backup.' + extension


def program(kind):
    return unit(kind, 'service').removesuffix('.service')


def cron_name(kind):
    need(kind in KINDS, 'KIND_SCOPE')
    return 'ASA-School-' + kind + '-backup'


def spec(kind):
    return 'CRON_TZ=Europe/Moscow ' + ('20 3' if kind == 'database' else '0 4') + ' * * *'


def intended_next(kind, now=None):
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(MOSCOW)
    result = local.replace(hour=3 if kind == 'database' else 4,
                           minute=20 if kind == 'database' else 0, second=0, microsecond=0)
    if result <= local:
        result += timedelta(days=1)
    return result.astimezone(timezone.utc)


def handover_window(now=None):
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(MOSCOW)
    minute = local.hour * 60 + local.minute
    need(not 165 <= minute < 315, 'HANDOVER_BLOCKED_0245_TO_0515_MOSCOW')
    need(all((intended_next(kind, now) - now).total_seconds() >= 600 for kind in KINDS),
         'HANDOVER_TOO_CLOSE_TO_SCHEDULE')


def trusted(path, *, directory=False, private=False):
    path = Path(path)
    for part in (path, *path.parents):
        info = part.lstat()
        need(info.st_uid == 0 and not stat.S_ISLNK(info.st_mode)
             and not info.st_mode & 0o022, 'PATH_UNTRUSTED')
    info = path.lstat()
    need(stat.S_ISDIR(info.st_mode) if directory else
         stat.S_ISREG(info.st_mode) and info.st_nlink == 1, 'PATH_TYPE')
    if private:
        need(stat.S_IMODE(info.st_mode) == (0o700 if directory else 0o600), 'PRIVATE_MODE')
    return info


def read(path, limit=256 * 1024, *, private=False):
    info = trusted(path, private=private)
    need(info.st_size <= limit, 'READ_BOUND')
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as source:
        initial = os.fstat(source.fileno())
        need((initial.st_dev, initial.st_ino, initial.st_mtime_ns, initial.st_size) ==
             (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size), 'READ_CHANGED')
        value = source.read(limit + 1)
        final = os.fstat(source.fileno())
        current = Path(path).lstat()
        need(len(value) == info.st_size and
             (initial.st_dev, initial.st_ino, initial.st_mtime_ns, initial.st_ctime_ns, initial.st_size) ==
             (final.st_dev, final.st_ino, final.st_mtime_ns, final.st_ctime_ns, final.st_size) ==
             (current.st_dev, current.st_ino, current.st_mtime_ns, current.st_ctime_ns, current.st_size),
             'READ_CHANGED')
    return value


def directory(path):
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        trusted(path.parent, directory=True)
        path.mkdir(mode=0o700)
    trusted(path, directory=True, private=True)


def atomic(path, value):
    path = Path(path)
    trusted(path.parent, directory=True, private=True)
    if path.exists() or path.is_symlink():
        trusted(path, private=True)
    raw = canonical(value) + b'\n'
    need(len(raw) <= 128 * 1024, 'STATE_BOUND')
    fd, temporary = tempfile.mkstemp(prefix='.pending-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def immutable(path, raw):
    path = Path(path)
    if path.exists() or path.is_symlink():
        need(read(path, private=True) == raw, 'IMMUTABLE_RELEASE_CONFLICT')
        return
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as output:
        os.fchmod(output.fileno(), 0o600)
        output.write(raw)
        output.flush()
        os.fsync(output.fileno())


def release_manifest(path, value):
    if path.exists() or path.is_symlink():
        need(json.loads(read(path, private=True)) == value, 'IMMUTABLE_MANIFEST_CONFLICT')
    else:
        immutable(path, canonical(value) + b'\n')


@contextmanager
def lock(path=LOCK, *, shared=False):
    parent = path.parent.lstat()
    need(stat.S_ISDIR(parent.st_mode) and parent.st_uid == 0 and
         (not parent.st_mode & 0o022 or parent.st_mode & stat.S_ISVTX), 'LOCK_PARENT')
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        need(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1
             and stat.S_IMODE(info.st_mode) == 0o600, 'LOCK_INODE')
        try:
            fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ScheduleError('OPERATION_OR_BACKUP_BUSY') from None
        current = path.lstat()
        need((info.st_dev, info.st_ino) == (current.st_dev, current.st_ino), 'LOCK_REPLACED')
        yield
    finally:
        os.close(fd)


def command(arguments, timeout=15):
    environment = dict(os.environ, LC_ALL='C', TZ='UTC', PYTHONDONTWRITEBYTECODE='1')
    done = subprocess.run(arguments, stdin=subprocess.DEVNULL, capture_output=True,
                          timeout=timeout, check=False, env=environment)
    need(done.returncode == 0 and len(done.stdout) <= 256 * 1024, 'LOCAL_COMMAND_FAILED')
    return done.stdout.decode('utf-8')


def check_host():
    need(os.geteuid() == 0 and socket.gethostname() == HOST, 'VM100_ROOT_REQUIRED')
    identity = json.loads(read(APP / '.asa/installation.json'))
    need(identity.get('instanceId') == INSTANCE and identity.get('project') == PROJECT,
         'INSTALLATION_CHANGED')
    need(not (APP / '.asa/maintenance.json').exists()
         and not (APP / '.asa/maintenance.json').is_symlink(), 'ASA_MAINTENANCE_PRESENT')


def source_release():
    source = Path(__file__).absolute()
    raw = read(source)
    checksum = digest(raw)
    return raw, checksum, BASE / 'releases' / checksum[:16] / NAME


def settings_release():
    return SETTINGS_BASE / digest(canonical(SETTINGS_FILES))[:16]


def runner(kind):
    return DATABASE_SCRIPT if kind == 'database' else settings_release() / 'school-nightly-settings-backup.py'


def verify_database_transport():
    raw = read(DATABASE_SCRIPT)
    need(hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()
         == DATABASE_BLOB, 'DATABASE_RUNNER_CHANGED')
    for name in ('school_yandex_app_folder.py', 'cloud_storage.py'):
        need(digest(read(DATABASE_SCRIPT.with_name(name))) == SETTINGS_FILES[name],
             'DATABASE_TRANSPORT_CHANGED')


def verify_runners():
    verify_database_transport()
    for name, expected in SETTINGS_FILES.items():
        need(digest(read(settings_release() / name, private=True)) == expected,
             'SETTINGS_RELEASE_CHANGED')
    manifest = json.loads(read(settings_release() / 'release.json', private=True))
    need(manifest == {'format': 'asa-school-settings-release-v1', 'files': SETTINGS_FILES},
         'SETTINGS_MANIFEST_CHANGED')
    for path in (Path('/var/backups/asa-school-nightly'), Path('/var/backups/asa-school-nightly/staging'),
                 Path('/var/backups/asa-school-nightly/archives'), Path('/var/backups/server-settings'),
                 Path('/var/backups/server-settings/school-nightly-settings')):
        trusted(path, directory=True, private=True)


def install_release():
    raw, checksum, target = source_release()
    for path in (BASE, BASE / 'releases', target.parent, STATE, STATE / 'dispatch', STATE / 'runs', STATE / 'output'):
        directory(path)
    immutable(target, raw)
    release_manifest(target.parent / 'release.json', {'format': FORMAT, 'programSha256': checksum})
    # Preserve old settings releases. This copies public, pinned files only.
    files = {}
    for name, expected in SETTINGS_FILES.items():
        existing = settings_release() / name
        source = existing if existing.exists() else Path(__file__).absolute().parent / name
        files[name] = read(source)
        need(digest(files[name]) == expected, 'SETTINGS_SOURCE_PIN_CHANGED')
    for path in (SETTINGS_BASE.parent, SETTINGS_BASE, settings_release()):
        directory(path)
    for name, data in files.items():
        immutable(settings_release() / name, data)
    release_manifest(settings_release() / 'release.json',
                     {'format': 'asa-school-settings-release-v1', 'files': SETTINGS_FILES})
    verify_runners()
    return checksum, str(target)


def preflight(panel, legacy, *, fresh=False):
    """Inspect the exact source and old clocks before the first mutation."""
    panel.prerequisite()
    handover_window()
    verify_database_transport()
    raw, checksum, target = source_release()
    for name, expected in SETTINGS_FILES.items():
        existing = settings_release() / name
        source = existing if existing.exists() else Path(__file__).absolute().parent / name
        need(digest(read(source)) == expected, 'SETTINGS_SOURCE_PIN_CHANGED')
    if (STATE / 'state.json').exists() or (STATE / 'state.json').is_symlink():
        state = load_state()
        need(state['phase'] in ('STAGING', 'STAGED'), 'PREFLIGHT_EXISTING_PHASE')
    else:
        state = new_state(panel, legacy, checksum, str(target), fresh=fresh)
    legacy.idle()
    return {'status': 'NATIVE_CLOCK_READONLY_PREFLIGHT_PASS', 'observedAt': stamp(),
            'programSha256': checksum, 'panelVersion': PANEL_VERSION,
            'legacy': {kind: legacy.inspect(kind) for kind in KINDS},
            'originalOwner': state['originalOwner'], 'settingsSourcePinsVerified': True,
            'nativeObjectsCreated': False, 'backupExecuted': False, 'changes': False}


def load_state():
    value = json.loads(read(STATE / 'state.json', private=True))
    need(value.get('format') == FORMAT and value.get('instance') == INSTANCE
         and value.get('project') == PROJECT and set(value.get('jobs', {})) == set(KINDS),
         'STATE_SCOPE')
    need(re.fullmatch(r'[a-f0-9]{32}', value.get('generation', '')), 'STATE_GENERATION')
    need(value.get('programSha256') == source_release()[1], 'STATE_PROGRAM_CHANGED')
    need(value.get('programPath') == str(source_release()[2]), 'STATE_PROGRAM_PATH_CHANGED')
    return value


def save_state(value):
    value['updatedAt'] = stamp()
    atomic(STATE / 'state.json', value)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


def panel_version_executable():
    selected = shutil.which('1panel')
    need(selected, 'PANEL_BINARY_MISSING')
    path = Path(selected)
    before = path.lstat()
    if stat.S_ISLNK(before.st_mode):
        need(path == PANEL_BINARY_ALIAS, 'PANEL_BINARY_ALIAS_UNSUPPORTED')
        need(before.st_uid == 0 and before.st_nlink == 1, 'PANEL_BINARY_ALIAS_UNTRUSTED')
        for parent in path.parents:
            trusted(parent, directory=True)
        need(os.readlink(path) == str(PANEL_BINARY_TARGET), 'PANEL_BINARY_ALIAS_TARGET')
        executable = path.resolve(strict=True)
        need(executable == PANEL_BINARY_TARGET, 'PANEL_BINARY_ALIAS_TARGET')
        info = trusted(executable)
        identity = lambda value: (value.st_dev, value.st_ino, value.st_uid, value.st_gid,
                                  value.st_mode, value.st_nlink, value.st_size,
                                  value.st_mtime_ns, value.st_ctime_ns)
        need(identity(before) == identity(path.lstat())
             and os.readlink(path) == str(executable), 'PANEL_BINARY_ALIAS_CHANGED')
    else:
        executable = path
        info = trusted(executable)
    need(info.st_mode & 0o111, 'PANEL_BINARY_NOT_EXECUTABLE')
    return str(executable)


class Panel:
    def __init__(self):
        text = read('/etc/server-settings/mcp.env', private=True).decode('utf-8')
        values = {}
        for line in text.splitlines():
            if '=' not in line or line.lstrip().startswith('#'):
                continue
            name, value = line.split('=', 1)
            if name.strip() in ('PANEL_HOST', 'PANEL_ACCESS_TOKEN'):
                parts = shlex.split(value, comments=False, posix=True)
                need(len(parts) == 1 and name.strip() not in values, 'PANEL_CONFIG_PARSE')
                values[name.strip()] = parts[0]
        need(values.get('PANEL_HOST') == PANEL and values.get('PANEL_ACCESS_TOKEN'), 'PANEL_SCOPE')
        self.key = values['PANEL_ACCESS_TOKEN']
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(self, endpoint, payload=None, *, method='POST'):
        allowed = ('/cronjobs', '/cronjobs/search', '/cronjobs/next', '/cronjobs/status',
                   '/cronjobs/handle', '/cronjobs/search/records', '/hosts/tool/status',
                   '/hosts/tool/supervisor/process', '/hosts/tool/supervisor/process/file',
                   '/hosts/tool/supervisor/process/file/get')
        need(endpoint in allowed and method in ('POST', 'GET'), 'PANEL_ENDPOINT_SCOPE')
        if endpoint == '/hosts/tool/supervisor/process' and method == 'POST':
            need(isinstance(payload, dict) and payload.get('name') in {program(kind) for kind in KINDS}
                 and payload.get('operate') in ('create', 'start'), 'PANEL_PROCESS_MUTATION_SCOPE')
        if endpoint in ('/hosts/tool/supervisor/process/file', '/hosts/tool/supervisor/process/file/get'):
            need(isinstance(payload, dict) and payload.get('name') in {program(kind) for kind in KINDS},
                 'PANEL_PROCESS_FILE_SCOPE')
            if endpoint.endswith('/file'):
                need(payload.get('operate') == 'update' and payload.get('file') == 'config',
                     'PANEL_PROCESS_FILE_MUTATION_SCOPE')
            else:
                need(payload.get('file') in ('config', 'out.log'), 'PANEL_PROCESS_FILE_SCOPE')
        timestamp = str(int(time.time()))
        token = hashlib.md5(('1panel' + self.key + timestamp).encode()).hexdigest()
        request = urllib.request.Request(PANEL + '/api/v2' + endpoint, method=method,
            data=None if payload is None else canonical(payload),
            headers={'Content-Type': 'application/json', '1Panel-Timestamp': timestamp, '1Panel-Token': token})
        try:
            with self.opener.open(request, timeout=15) as response:
                raw = response.read(2_000_001)
            need(len(raw) <= 2_000_000, 'PANEL_RESPONSE_BOUND')
            document = json.loads(raw)
            need(document.get('code') in (0, 200), 'PANEL_RESPONSE_REJECTED')
            return document.get('data')
        except ScheduleError:
            raise
        except Exception:
            raise ScheduleError('PANEL_REQUEST_OUTCOME_UNKNOWN') from None

    def jobs(self):
        data = self.request('/cronjobs/search', {'page': 1, 'pageSize': 100, 'info': '',
            'orderBy': 'createdAt', 'order': 'descending', 'groupIDs': []})
        need(isinstance(data, dict) and isinstance(data.get('total'), int), 'CRON_RESPONSE')
        rows = data.get('items') or []
        need(isinstance(rows, list) and data['total'] == len(rows) <= 100, 'CRON_LIST_INCOMPLETE')
        return rows

    def programs(self):
        rows = self.request('/hosts/tool/supervisor/process', method='GET')
        need(isinstance(rows, list) and len(rows) <= 20, 'SUPERVISOR_LIST_BOUND')
        return rows

    def prerequisite(self):
        executable = panel_version_executable()
        need(re.search(r'(?<![\w.])v2\.3\.2(?![\w.-])', command([executable, 'version'], 8)),
             'PANEL_VERSION_CHANGED')
        data = self.request('/hosts/tool/status', {'type': 'supervisord'})
        config = data.get('config', {}) if isinstance(data, dict) else {}
        need(config.get('isExist') is True and config.get('ctlExist') is True
             and config.get('status') == 'running' and config.get('init') is False
             and config.get('configPath') == '/etc/supervisor/supervisord.conf',
             'NATIVE_SUPERVISOR_NOT_READY')

    def records(self, job_id, since):
        data = self.request('/cronjobs/search/records', {'page': 1, 'pageSize': 100,
            'cronjobID': job_id, 'startTime': since, 'endTime': stamp(), 'status': ''})
        rows = data.get('items') or [] if isinstance(data, dict) else None
        need(isinstance(rows, list) and data.get('total') == len(rows) <= 100,
             'CRON_RECORD_LIST_INCOMPLETE')
        return [{key: row.get(key) for key in ('id', 'cronjobID', 'status', 'startTime', 'interval')}
                for row in rows]


def cron_payload(state, kind):
    script = '#!/usr/bin/env bash\nset -euo pipefail\numask 077\nexec /usr/bin/python3 -B ' + \
        shlex.quote(state['programPath']) + ' trigger --kind ' + kind + ' --generation ' + \
        state['generation'] + ' --apply\n'
    return {'name': cron_name(kind), 'type': 'shell', 'specCustom': True, 'spec': spec(kind),
            'executor': 'bash', 'scriptMode': 'input', 'script': script, 'user': 'root',
            'retainCopies': 365, 'retryTimes': 0, 'timeout': 7230 if kind == 'database' else 2010,
            'ignoreErr': False, 'groupID': 0, 'containerName': '', 'command': '', 'secret': '', 'args': ''}


def process_payload(state, kind):
    return {'name': program(kind), 'operate': 'create', 'user': 'root', 'dir': str(APP),
            'command': '/usr/bin/python3 -B ' + state['programPath'] + ' worker --kind ' + kind +
                       ' --generation ' + state['generation'] + ' --apply',
            'numprocs': '1', 'autoRestart': 'false', 'autoStart': 'false',
            'environment': 'PYTHONDONTWRITEBYTECODE="1",PYTHONUNBUFFERED="1",TZ="Europe/Moscow",LC_ALL="C"'}


def process_config(state, kind):
    payload = process_payload(state, kind)
    values = [('command', payload['command']), ('directory', str(APP)), ('user', 'root'),
              ('numprocs', '1'), ('process_name', '%(program_name)s_%(process_num)02d'),
              ('autostart', 'false'), ('autorestart', 'false'), ('startsecs', '0'), ('startretries', '0'),
              ('stopsignal', 'TERM'), ('stopwaitsecs', '2147483647'), ('stopasgroup', 'false'),
              ('killasgroup', 'false'), ('exitcodes', '0'), ('priority', '999'), ('umask', '0077'),
              ('environment', payload['environment'])]
    for stream in ('stdout', 'stderr'):
        short = 'out' if stream == 'stdout' else 'err'
        values.extend([(stream + '_logfile', '/opt/1panel/tools/supervisord/log/' + program(kind) + '.' + short + '.log'),
                       (stream + '_logfile_maxbytes', '2MB'), (stream + '_logfile_backups', '10')])
    return '[program:' + program(kind) + ']\n' + ''.join(key + '=' + value + '\n' for key, value in values)


def unique(rows, name):
    found = [row for row in rows if row.get('name') == name]
    need(len(found) <= 1, 'DUPLICATE_NATIVE_OBJECT')
    return found[0] if found else None


def verify_job(state, kind, row, status=None):
    need(row is not None, 'NATIVE_CRON_MISSING')
    need(all(row.get(key) == value for key, value in cron_payload(state, kind).items()),
         'NATIVE_CRON_PAYLOAD_CHANGED')
    need(type(row.get('id')) is int and row['id'] > 0, 'NATIVE_CRON_ID_INVALID')
    expected = state['jobs'][kind].get('id')
    need(expected is None or row['id'] == expected, 'NATIVE_CRON_ID_CHANGED')
    need(row.get('status') in ('Enable', 'Disable') and (status is None or row['status'] == status),
         'NATIVE_CRON_STATUS_CHANGED')


def process_status(row):
    need(row is not None and isinstance(row.get('status'), list) and len(row['status']) == 1,
         'SUPERVISOR_PROCESS_STATUS_MISSING')
    status = row['status'][0].get('status')
    need(status in INACTIVE + ACTIVE + ('FATAL', 'UNKNOWN'), 'SUPERVISOR_PROCESS_STATUS_INVALID')
    return status


def verify_process(state, kind, row):
    expected = process_payload(state, kind)
    need(row is not None and all(row.get(key) == value for key, value in expected.items() if key != 'operate'),
         'NATIVE_PROCESS_PAYLOAD_CHANGED')
    return process_status(row)


def foreign_snapshot(rows):
    own = {program(kind) for kind in KINDS}
    foreign = [row for row in rows if row.get('name') not in own]
    need(all(row.get('name') == COLLECTOR for row in foreign), 'UNREVIEWED_SUPERVISOR_PROGRAM')
    # Values can include private environment entries: compare in memory only.
    keys = ('name', 'command', 'user', 'dir', 'numprocs', 'autoRestart', 'autoStart', 'environment')
    return [{**{key: row.get(key) for key in keys},
             'PID': [(item.get('status'), item.get('PID')) for item in row.get('status', [])]}
            for row in foreign]


def verify_config(panel, state, kind):
    actual = panel.request('/hosts/tool/supervisor/process/file/get', {'name': program(kind), 'file': 'config'})
    need(actual == process_config(state, kind), 'NATIVE_PROCESS_CONFIG_CHANGED')


def initial_config_owned(state, kind, content):
    need(isinstance(content, str) and len(content) <= 16384, 'INITIAL_PROCESS_CONFIG_BOUND')
    parsed = configparser.ConfigParser(interpolation=None, strict=True)
    parsed.read_string(content)
    section = 'program:' + program(kind)
    need(parsed.sections() == [section] and not parsed.defaults(), 'INITIAL_PROCESS_CONFIG_SCOPE')
    payload = process_payload(state, kind)
    expected = {'command': payload['command'], 'directory': str(APP), 'user': 'root',
                'numprocs': '1', 'process_name': '%(program_name)s_%(process_num)02d',
                'autostart': 'false', 'autorestart': 'false', 'startsecs': '3', 'priority': '999',
                'environment': payload['environment']}
    for stream, short in (('stdout', 'out'), ('stderr', 'err')):
        expected[stream + '_logfile'] = '/opt/1panel/tools/supervisord/log/' + program(kind) + '.' + short + '.log'
        expected[stream + '_logfile_maxbytes'] = '2MB'
    need(dict(parsed[section]) == expected, 'INITIAL_PROCESS_CONFIG_DRIFT')


def properties(name):
    fields = ('LoadState', 'UnitFileState', 'ActiveState', 'SubState', 'FragmentPath', 'DropInPaths')
    output = command(['/usr/bin/systemctl', 'show', name, '--property=' + ','.join(fields)])
    return dict(line.split('=', 1) for line in output.splitlines() if '=' in line)


class Legacy:
    def inspect(self, kind):
        result = {}
        for extension in ('timer', 'service'):
            name = unit(kind, extension)
            path = Path('/etc/systemd/system') / name
            value = properties(name)
            if value.get('LoadState') == 'not-found' and not path.exists() and not path.is_symlink():
                result[extension] = None
                continue
            need(value.get('LoadState') == 'loaded' and value.get('FragmentPath') == str(path)
                 and not value.get('DropInPaths'), 'LEGACY_UNIT_SCOPE_CHANGED')
            actual_sha = digest(read(path))
            pins = LEGACY_PINS[kind][extension]
            need(actual_sha in ((pins,) if isinstance(pins, str) else pins), 'LEGACY_UNIT_BYTES_CHANGED')
            result[extension] = {key: value.get(key) for key in ('UnitFileState', 'ActiveState', 'SubState')}
            result[extension]['sha256'] = actual_sha
        need((result['timer'] is None) == (result['service'] is None), 'PARTIAL_LEGACY_INSTALLATION')
        return result

    def idle(self):
        for kind in KINDS:
            state = self.inspect(kind)
            need(state['service'] is None or state['service']['ActiveState'] == 'inactive',
                 'LEGACY_BACKUP_NOT_IDLE')

    def disabled(self, kind):
        state = self.inspect(kind)['timer']
        return state is None or (state['UnitFileState'] == 'disabled' and state['ActiveState'] == 'inactive')

    def disable(self, kind):
        if self.inspect(kind)['timer'] is not None:
            command(['/usr/bin/systemctl', 'disable', '--now', unit(kind)], 20)
        need(self.disabled(kind), 'LEGACY_TIMER_DISABLE_NOT_CONFIRMED')

    def restore(self, kind, previous):
        if previous['timer'] is None:
            need(self.inspect(kind)['timer'] is None, 'FRESH_LEGACY_UNIT_APPEARED')
            return
        need(previous['timer']['UnitFileState'] == 'enabled'
             and previous['timer']['ActiveState'] == 'active', 'LEGACY_BASELINE_UNSUPPORTED')
        command(['/usr/bin/systemctl', 'enable', '--now', unit(kind)], 20)
        need(self.inspect(kind) == previous, 'LEGACY_TIMER_RESTORE_NOT_CONFIRMED')


class Controller:
    """State changes are durable before uncertain native calls; never delete history."""
    def __init__(self, panel, legacy, save=save_state):
        self.panel, self.legacy, self.save = panel, legacy, save

    def set_job_status(self, state, kind, desired):
        row = unique(self.panel.jobs(), cron_name(kind))
        verify_job(state, kind, row)
        if row['status'] != desired:
            try:
                self.panel.request('/cronjobs/status', {'id': row['id'], 'status': desired})
            except ScheduleError:
                # An accepted mutation can lose its response. Read; never blind retry.
                pass
            verify_job(state, kind, unique(self.panel.jobs(), cron_name(kind)), desired)

    def objects(self, state, *, status=None, idle=False):
        jobs, processes = self.panel.jobs(), self.panel.programs()
        foreign_snapshot(processes)
        for kind in KINDS:
            verify_job(state, kind, unique(jobs, cron_name(kind)), status)
            actual = verify_process(state, kind, unique(processes, program(kind)))
            verify_config(self.panel, state, kind)
            if idle:
                need(actual in INACTIVE, 'NATIVE_BACKUP_NOT_IDLE')

    def stage(self, state):
        need(state['phase'] in ('STAGING', 'STAGED') and state['owner'] != '1panel', 'STAGE_PHASE')
        self.legacy.idle()
        for kind in KINDS:
            rows = self.panel.programs()
            foreign_before = foreign_snapshot(rows)
            row = unique(rows, program(kind))
            if row is None:
                self.save(state)
                try:
                    self.panel.request('/hosts/tool/supervisor/process', process_payload(state, kind))
                except ScheduleError:
                    pass
                row = unique(self.panel.programs(), program(kind))
            need(verify_process(state, kind, row) in INACTIVE, 'STAGED_PROCESS_NOT_IDLE')
            current = self.panel.request('/hosts/tool/supervisor/process/file/get',
                                         {'name': program(kind), 'file': 'config'})
            if current != process_config(state, kind):
                # Only the just-owned, disabled process may receive its one-shot config.
                need(state['jobs'][kind].get('processConfigured') is not True, 'OWNED_CONFIG_DRIFT')
                initial_config_owned(state, kind, current)
                try:
                    self.panel.request('/hosts/tool/supervisor/process/file', {'name': program(kind),
                        'file': 'config', 'operate': 'update', 'content': process_config(state, kind)})
                except ScheduleError:
                    pass
                verify_config(self.panel, state, kind)
            need(foreign_snapshot(self.panel.programs()) == foreign_before, 'FOREIGN_SUPERVISOR_CHANGED')
            state['jobs'][kind]['processConfigured'] = True
            self.save(state)
            row = unique(self.panel.jobs(), cron_name(kind))
            if row is None:
                self.save(state)
                try:
                    self.panel.request('/cronjobs', cron_payload(state, kind))
                except ScheduleError:
                    pass
                row = unique(self.panel.jobs(), cron_name(kind))
            verify_job(state, kind, row)
            state['jobs'][kind]['id'] = row['id']
            self.save(state)
            self.set_job_status(state, kind, 'Disable')
            next_values = self.panel.request('/cronjobs/next', {'spec': spec(kind)})
            need(isinstance(next_values, list) and len(next_values) == 5
                 and all(isinstance(value, str) and re.fullmatch(r'\d{4}-\d\d-\d\d \d\d:\d\d:\d\d', value)
                         for value in next_values), 'NATIVE_CRON_PREVIEW_INVALID')
            state['jobs'][kind]['nativeNextPreview'] = next_values
            self.save(state)
        self.objects(state, status='Disable', idle=True)
        state.update(phase='STAGED', owner=state['originalOwner'])
        self.save(state)
        return {'status': 'NATIVE_SCHEDULES_STAGED_GATE_CLOSED', 'backupExecuted': False,
                'nativeJobIds': {kind: state['jobs'][kind]['id'] for kind in KINDS}}

    def restore_original(self, state):
        state.update(phase='ROLLING_BACK', owner='none')
        self.save(state)
        failures = []
        for kind in KINDS:
            try:
                self.set_job_status(state, kind, 'Disable')
            except Exception:
                failures.append('PANEL_DISABLE_' + kind.upper())
        # A closed durable gate is necessary but does not make a live backup safe to stop.
        try:
            self.legacy.idle()
            processes = self.panel.programs()
            for kind in KINDS:
                need(verify_process(state, kind, unique(processes, program(kind))) in INACTIVE,
                     'NATIVE_BACKUP_NOT_IDLE')
        except Exception:
            failures.append('BACKUP_IDLE_NOT_CONFIRMED')
        if not failures:
            for kind in KINDS:
                try:
                    self.legacy.restore(kind, state['legacy'][kind])
                except Exception:
                    failures.append('LEGACY_RESTORE_' + kind.upper())
        state.update(phase='RECOVERY_REQUIRED' if failures else 'ROLLED_BACK',
                     owner='none' if failures else state['originalOwner'], recoveryFailures=failures)
        self.save(state)
        need(not failures, 'CLOCK_HANDOVER_RECOVERY_REQUIRED')
        return {'status': 'ORIGINAL_CLOCK_RESTORED', 'nativeHistoryPreserved': True, 'backupExecuted': False}

    def activate(self, state):
        if state['phase'] == 'ACTIVE' and state['owner'] == '1panel':
            self.objects(state, status='Enable')
            need(all(self.legacy.disabled(kind) for kind in KINDS), 'DUPLICATE_CLOCK_DETECTED')
            return {'status': 'NATIVE_CLOCK_ALREADY_ACTIVE', 'backupExecuted': False}
        need(state['phase'] == 'STAGED' and state['owner'] == state['originalOwner'], 'ACTIVATE_PHASE')
        need(all(state['jobs'][kind].get('probe', {}).get('status') == 'NATIVE_GATED_PROBE_PASS'
                 and state['jobs'][kind]['probe'].get('backupExecuted') is False
                 and state['jobs'][kind]['probe'].get('programSha256') == state['programSha256']
                 for kind in KINDS), 'NATIVE_PROBE_NOT_ACCEPTED')
        self.objects(state, status='Disable', idle=True)
        self.legacy.idle()
        need({kind: self.legacy.inspect(kind) for kind in KINDS} == state['legacy'], 'LEGACY_BASELINE_DRIFT')
        state.update(phase='ACTIVATING', owner='none')
        self.save(state)
        try:
            for kind in KINDS:
                self.set_job_status(state, kind, 'Enable')
            for kind in KINDS:
                self.legacy.disable(kind)
            self.legacy.idle()
            self.objects(state, status='Enable', idle=True)
            need(all(self.legacy.disabled(kind) for kind in KINDS), 'DUPLICATE_CLOCK_DETECTED')
            state.update(phase='ACTIVE', owner='1panel', activatedAt=stamp())
            self.save(state)
        except Exception as error:
            self.restore_original(state)
            raise ScheduleError('ACTIVATION_FAILED_ORIGINAL_CLOCK_RESTORED') from error
        return {'status': 'NATIVE_1PANEL_CLOCK_ACTIVE', 'backupExecuted': False,
                'supervision': '1Panel Supervisor', 'legacyTimers': 'disabled',
                'firstAutomaticBackups': 'NOT_YET_PROVEN'}


def new_state(panel, legacy, checksum, path, *, fresh=False):
    jobs, processes = panel.jobs(), panel.programs()
    foreign_snapshot(processes)
    need(not any(unique(jobs, cron_name(kind)) or unique(processes, program(kind)) for kind in KINDS),
         'PREEXISTING_NATIVE_OBJECT_WITHOUT_OWNERSHIP')
    previous = {kind: legacy.inspect(kind) for kind in KINDS}
    legacy.idle()
    if fresh:
        need(all(value['timer'] is None for value in previous.values()), 'FRESH_MODE_LEGACY_EXISTS')
    else:
        need(all(value['timer'] and value['timer']['UnitFileState'] == 'enabled'
                 and value['timer']['ActiveState'] == 'active' for value in previous.values()),
             'LEGACY_TIMERS_NOT_ENABLED_ACTIVE')
    owner = 'none' if fresh else 'legacy'
    return {'format': FORMAT, 'instance': INSTANCE, 'project': PROJECT, 'generation': uuid.uuid4().hex,
            'programSha256': checksum, 'programPath': path, 'createdAt': stamp(),
            'phase': 'STAGING', 'owner': owner, 'originalOwner': owner, 'legacy': previous,
            'jobs': {kind: {'id': None, 'processConfigured': False} for kind in KINDS},
            'clockTimeZone': 'Europe/Moscow', 'backupExecutedDuringInstall': False}


def file_json(path):
    return json.loads(read(path, private=True)) if path.exists() or path.is_symlink() else None


def runtime_state(kind):
    return file_json(STATE / 'runs' / (kind + '.json'))


def validate_generation(state, generation):
    need(generation == state['generation'], 'NATIVE_GENERATION_CHANGED')


def trigger(panel, state, kind, generation, *, budget=None):
    validate_generation(state, generation)
    need(state['phase'] in ('STAGED', 'ACTIVE'), 'GATE_CLOSED_TRANSITION')
    need(state['phase'] != 'ACTIVE' or state['owner'] == '1panel', 'GATE_CLOSED_OWNER')
    verify_runners()
    controller = Controller(panel, Legacy())
    controller.objects(state, status='Disable' if state['phase'] == 'STAGED' else None)
    path = STATE / 'dispatch' / (kind + '.json')
    before = file_json(path)
    run = runtime_state(kind)
    if before:
        if run and run.get('dispatchId') == before.get('dispatchId') and run.get('finishedAt'):
            same_day = before.get('dayMoscow') == datetime.now(MOSCOW).date().isoformat()
            if before.get('phase') == state['phase'] and same_day:
                need(run.get('status') in ('STAGED_GATE_VERIFIED_NO_BACKUP', 'BACKUP_CLOUD_SHA256_PASS'),
                     'PRIOR_BACKUP_FAILED_REVIEW_REQUIRED')
                return {'status': 'DISPATCH_ALREADY_COMPLETED', 'run': run}
        else:
            raise ScheduleError('PRIOR_DISPATCH_OUTCOME_UNRESOLVED')
    row = unique(panel.programs(), program(kind))
    need(verify_process(state, kind, row) in INACTIVE, 'NATIVE_BACKUP_NOT_IDLE')
    Legacy().idle()
    if state['phase'] == 'ACTIVE':
        need(all(Legacy().disabled(item) for item in KINDS), 'DUPLICATE_CLOCK_DETECTED')
    dispatch = {'dispatchId': uuid.uuid4().hex, 'generation': state['generation'], 'kind': kind,
                'phase': state['phase'], 'createdAt': stamp(), 'dayMoscow': datetime.now(MOSCOW).date().isoformat(),
                'programSha256': state['programSha256'], 'status': 'START_REQUEST_RECORDED'}
    atomic(path, dispatch)
    print(json.dumps({'status': 'NATIVE_START_REQUEST_RECORDED', 'dispatchId': dispatch['dispatchId'],
                      'kind': kind, 'backupCompletionProven': False}), flush=True)
    try:
        panel.request('/hosts/tool/supervisor/process', {'name': program(kind), 'operate': 'start'})
    except ScheduleError:
        # Never issue a second start after an ambiguous response.
        pass
    maximum = budget if budget is not None else (30 if state['phase'] == 'STAGED' else 7200 if kind == 'database' else 1980)
    deadline = time.monotonic() + maximum
    while time.monotonic() < deadline:
        run = runtime_state(kind)
        if run and run.get('dispatchId') == dispatch['dispatchId'] and run.get('finishedAt'):
            dispatch.update(status='OBSERVED_COMPLETE', finishedAt=stamp())
            atomic(path, dispatch)
            need(run.get('status') in ('STAGED_GATE_VERIFIED_NO_BACKUP', 'BACKUP_CLOUD_SHA256_PASS'),
                 'NATIVE_BACKUP_REPORTED_FAILURE')
            return {'status': 'NATIVE_RUN_RESULT_OBSERVED', 'run': run}
        time.sleep(0.25 if state['phase'] == 'STAGED' else 2)
    dispatch.update(status='OBSERVER_DEADLINE_BACKUP_NOT_STOPPED', observerEndedAt=stamp())
    atomic(path, dispatch)
    raise ScheduleError('OBSERVATION_TIMEOUT_BACKUP_NOT_STOPPED')


def archive_from_output(kind, raw):
    need(len(raw) <= 512 * 1024, 'RUNNER_OUTPUT_BOUND')
    names = []
    for line in raw.decode('utf-8', errors='strict').splitlines():
        if kind == 'database' and line.startswith('NIGHTLY_DATABASE_LOCAL_ENCRYPTED='):
            names.append(line.split('=', 1)[1])
        elif kind == 'settings' and line.startswith('{'):
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if value.get('status') == 'CONFIG_NIGHTLY_FULL_DOWNLOAD_SHA256_PASS':
                names.append(value.get('archive'))
    prefix = 'asa-school-01-' if kind == 'database' else 'asa-school-config-01-'
    need(len(names) == 1 and isinstance(names[0], str)
         and re.fullmatch(re.escape(prefix) + r'\d{8}T\d{6}Z-[a-f0-9]{8}[.]tar[.]age', names[0]),
         'RUNNER_ARCHIVE_IDENTITY_UNPROVEN')
    return names[0]


def runner_failure_codes(raw):
    need(len(raw) <= 512 * 1024, 'RUNNER_OUTPUT_BOUND')
    codes = []
    for line in raw.decode('utf-8', errors='replace').splitlines():
        value = None
        if line.startswith('SCHOOL_NIGHTLY_BACKUP_BLOCKED='):
            value = line.split('=', 1)[1]
        elif line.startswith(('{', '<3>{')):
            try:
                record = json.loads(line.removeprefix('<3>'))
            except ValueError:
                continue
            if record.get('status') in ('CONFIG_NIGHTLY_STOP', 'CONFIG_SCHEDULE_STOP'):
                value = record.get('code')
        if isinstance(value, str) and re.fullmatch(r'[A-Z][A-Z0-9_]{0,95}', value) and value not in codes:
            codes.append(value)
    return codes[:4]


def receipt_after(kind, previous, started, archive=None):
    current = file_json(RUNNER_RESULT[kind])
    need(current and current != previous, 'RUNNER_SUCCESS_RECEIPT_NOT_ADVANCED')
    success = 'verified_cloud_sha256' if kind == 'database' else 'CONFIG_NIGHTLY_FULL_DOWNLOAD_SHA256_PASS'
    need(current.get('status') == success, 'RUNNER_SUCCESS_RECEIPT_INVALID')
    need(archive is None or current.get('archive') == archive, 'RUNNER_RECEIPT_ARCHIVE_CHANGED')
    date = current.get('createdAt') if kind == 'database' else current.get('finishedAt')
    need(isinstance(date, str) and datetime.fromisoformat(date.replace('Z', '+00:00')) >= started,
         'RUNNER_RECEIPT_PREDATES_DISPATCH')
    return {key: current.get(key) for key in ('status', 'archive', 'bytes', 'size', 'sha256',
            'ciphertextSha256', 'fullDownloadVerified', 'remote', 'remoteDirectory', 'createdAt', 'finishedAt')
            if key in current}


def worker(state, kind, generation):
    validate_generation(state, generation)
    need(Path(__file__).absolute() == Path(state['programPath']), 'WORKER_NOT_IMMUTABLE_RELEASE')
    dispatch = file_json(STATE / 'dispatch' / (kind + '.json'))
    need(dispatch and dispatch.get('generation') == generation and dispatch.get('kind') == kind
         and dispatch.get('phase') == state['phase'] and dispatch.get('programSha256') == state['programSha256'],
         'WORKER_WITHOUT_OWNED_DISPATCH')
    created = datetime.fromisoformat(dispatch['createdAt'])
    need(0 <= (datetime.now(timezone.utc) - created).total_seconds() <= 60, 'DISPATCH_TOO_OLD')
    previous_run = runtime_state(kind)
    need(not previous_run or previous_run.get('dispatchId') != dispatch['dispatchId'], 'WORKER_DISPATCH_REPLAY')
    verify_runners()
    started = datetime.now(timezone.utc)
    result = {'status': 'STARTING', 'dispatchId': dispatch['dispatchId'], 'kind': kind,
              'generation': generation, 'programSha256': state['programSha256'], 'startedAt': started.isoformat(),
              'timestamp': started.isoformat(), 'level': 'INFO', 'backupExecuted': False,
              'supervision': '1Panel Supervisor', 'restoreValidated': False}
    path = STATE / 'runs' / (kind + '.json')
    atomic(path, result)
    try:
        if state['phase'] == 'STAGED' and state['owner'] != '1panel':
            result.update(status='STAGED_GATE_VERIFIED_NO_BACKUP', finishedAt=stamp(), timestamp=stamp())
            atomic(path, result)
            print(json.dumps(result, sort_keys=True), flush=True)
            return result
        need(state['phase'] == 'ACTIVE' and state['owner'] == '1panel', 'WORKER_GATE_CLOSED')
        legacy = Legacy()
        legacy.idle()
        need(all(legacy.disabled(item) for item in KINDS), 'DUPLICATE_CLOCK_DETECTED')
        prior_receipt = file_json(RUNNER_RESULT[kind])
        # Preserve no-new-privileges, priority, core and umask restrictions of the
        # accepted units. The runner retains its own protected staging directories.
        os.umask(0o077)
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        need(ctypes.CDLL(None, use_errno=True).prctl(38, 1, 0, 0, 0) == 0, 'NO_NEW_PRIVILEGES_FAILED')
        os.nice(10)
        def defer_stop(number, frame):
            print(json.dumps({'status': 'STOP_DEFERRED_UNTIL_RUNNER_RECOVERY', 'kind': kind,
                              'dispatchId': dispatch['dispatchId']}), file=sys.stderr, flush=True)
        signal.signal(signal.SIGTERM, defer_stop)
        signal.signal(signal.SIGINT, defer_stop)
        environment = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', PYTHONUNBUFFERED='1', LC_ALL='C')
        arguments = ['/usr/bin/ionice', '-c2', '-n7', '/usr/bin/python3', '-B', str(runner(kind)), '--apply']
        result.update(status='BACKUP_STARTING', backupAttempted=True)
        atomic(path, result)
        print(json.dumps(result, sort_keys=True), flush=True)
        output_path = STATE / 'output' / (kind + '-' + dispatch['dispatchId'] + '.log')
        trusted(output_path.parent, directory=True, private=True)
        fd = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as output:
            os.fchmod(output.fileno(), 0o600)
            child = subprocess.Popen(arguments, cwd=APP, stdin=subprocess.DEVNULL, env=environment,
                                     stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
            result.update(status='BACKUP_RUNNING', backupExecuted=True, privateRunnerOutput=str(output_path),
                          workerPid=os.getpid(), runnerPid=child.pid)
            atomic(path, result)
            try:
                returncode = child.wait(timeout=None if kind == 'database' else 1900)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=45)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait()
                raise ScheduleError('SETTINGS_EXISTING_TIMEOUT_CONTRACT') from None
            output.flush()
            os.fsync(output.fileno())
        output_bytes = read(output_path, 512 * 1024, private=True)
        result.update(runnerExitCode=returncode, runnerOutputBytes=len(output_bytes),
                      runnerFailureCodes=runner_failure_codes(output_bytes))
        need(returncode == 0, 'ACCEPTED_RUNNER_FAILED')
        archive = archive_from_output(kind, output_bytes)
        summary = receipt_after(kind, prior_receipt, started, archive)
        result.update(status='BACKUP_CLOUD_SHA256_PASS', finishedAt=stamp(), timestamp=stamp(), runnerReceipt=summary)
        atomic(path, result)
        print(json.dumps(result, sort_keys=True), flush=True)
        return result
    except Exception as error:
        result.update(status='BACKUP_STOP', finishedAt=stamp(), timestamp=stamp(), level='ERROR', code=safe_code(error))
        atomic(path, result)
        print(json.dumps(result, sort_keys=True), file=sys.stderr, flush=True)
        raise


def probe(panel, state, kind):
    need(state['phase'] == 'STAGED' and state['owner'] != '1panel', 'PROBE_REQUIRES_CLOSED_GATE')
    Controller(panel, Legacy()).objects(state, status='Disable', idle=True)
    job = state['jobs'][kind]
    if job.get('probe', {}).get('status') == 'NATIVE_GATED_PROBE_PASS':
        return job['probe']
    if 'probeIntent' not in job:
        before = panel.records(job['id'], state['createdAt'])
        job['probeIntent'] = {'at': stamp(), 'previousRecordIds': [row['id'] for row in before]}
        save_state(state)
        try:
            panel.request('/cronjobs/handle', {'id': job['id']})
        except ScheduleError:
            pass
    # Re-entry only observes the existing intent; it never handles the job twice.
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        records = panel.records(job['id'], state['createdAt'])
        new = [row for row in records if row['id'] not in job['probeIntent']['previousRecordIds']]
        need(len(new) <= 1, 'PROBE_RECORD_AMBIGUOUS')
        run = runtime_state(kind)
        if new and new[0]['status'] == 'Success' and run and run.get('status') == 'STAGED_GATE_VERIFIED_NO_BACKUP':
            need(run.get('generation') == state['generation'] and run.get('programSha256') == state['programSha256']
                 and run.get('backupExecuted') is False and run.get('finishedAt') >= job['probeIntent']['at'],
                 'PROBE_RUN_BINDING_INVALID')
            log = panel.request('/hosts/tool/supervisor/process/file/get', {'name': program(kind), 'file': 'out.log'})
            need(isinstance(log, str) and run['dispatchId'] in log and 'STAGED_GATE_VERIFIED_NO_BACKUP' in log,
                 'NATIVE_PROCESS_LOG_NOT_OBSERVED')
            job['probe'] = {'status': 'NATIVE_GATED_PROBE_PASS', 'recordId': new[0]['id'],
                'dispatchId': run['dispatchId'], 'programSha256': state['programSha256'],
                'backupExecuted': False, 'nativeProcessLogObserved': True, 'acceptedAt': stamp()}
            save_state(state)
            return job['probe']
        if new:
            need(new[0]['status'] not in ('Failed', 'Canceled'), 'NATIVE_PROBE_FAILED')
        time.sleep(0.5)
    raise ScheduleError('PROBE_OUTCOME_UNKNOWN_DO_NOT_REDISPATCH')


def inspect(panel, state):
    jobs, processes = panel.jobs(), panel.programs()
    result = {'status': 'NATIVE_SCHEDULE_METADATA', 'observedAt': stamp(), 'phase': state['phase'],
              'owner': state['owner'], 'clockTimeZone': 'Europe/Moscow', 'backupExecutionNotAssumed': True,
              'jobs': {}, 'changes': False}
    for kind in KINDS:
        row = unique(jobs, cron_name(kind))
        verify_job(state, kind, row)
        proc = unique(processes, program(kind))
        status = verify_process(state, kind, proc)
        verify_config(panel, state, kind)
        result['jobs'][kind] = {'id': row['id'], 'cronStatus': row['status'], 'spec': spec(kind),
            'supervisorStatus': status, 'intendedNextUtc': intended_next(kind).isoformat(),
            'legacy': Legacy().inspect(kind), 'run': runtime_state(kind),
            'dispatch': file_json(STATE / 'dispatch' / (kind + '.json')),
            'records': panel.records(row['id'], (datetime.now(timezone.utc) - timedelta(days=7)).isoformat())}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', nargs='?', default='plan',
                        choices=('plan', 'preflight', 'stage', 'probe', 'activate', 'rollback', 'check', 'trigger', 'worker'))
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--fresh', action='store_true', help='preflight/stage with no legacy backup units')
    parser.add_argument('--kind', choices=KINDS)
    parser.add_argument('--generation')
    args = parser.parse_args()
    os.umask(0o077)
    if args.action == 'plan' or (args.action not in ('check', 'preflight') and not args.apply):
        print(json.dumps({'status': 'PLAN_ONLY', 'changes': False, 'manager': '1Panel Cron + Supervisor',
            'host': HOST, 'panelVersion': PANEL_VERSION, 'schedules': {kind: spec(kind) for kind in KINDS},
            'stagedProbeRunsBackup': False, 'legacyUnitsDeleted': False,
            'observerTimeoutStopsBackup': False, 'firstAutomaticRuns': 'NOT_PROVEN',
            'cleanLinuxInstall': 'NOT_PROVEN'}, sort_keys=True))
        return 0
    check_host()
    if args.action == 'preflight':
        value = preflight(Panel(), Legacy(), fresh=args.fresh)
    elif args.action in ('trigger', 'worker'):
        need(args.kind and args.generation, 'NATIVE_ARGUMENTS_REQUIRED')
        lock_name = args.kind + ('.dispatch.lock' if args.action == 'trigger' else '.run.lock')
        with lock(shared=True), lock(STATE / lock_name):
            state = load_state()
            value = worker(state, args.kind, args.generation) if args.action == 'worker' else trigger(Panel(), state, args.kind, args.generation)
    elif args.action == 'probe':
        need(args.kind, 'PROBE_KIND_REQUIRED')
        # No exclusive ownership lock while native workers need its shared side.
        with lock(shared=True), lock(STATE / 'probe.lock'):
            value = probe(Panel(), load_state(), args.kind)
    elif args.action == 'check':
        with lock(shared=True):
            value = inspect(Panel(), load_state())
    else:
        panel, legacy = Panel(), Legacy()
        panel.prerequisite()
        with lock():
            if args.action == 'stage':
                handover_window()
                checksum, path = install_release()
                if (STATE / 'state.json').exists():
                    state = load_state()
                else:
                    state = new_state(panel, legacy, checksum, path, fresh=args.fresh)
                    save_state(state)
                value = Controller(panel, legacy).stage(state)
            elif args.action == 'activate':
                handover_window()
                verify_runners()
                with lock(DB_LOCK):
                    value = Controller(panel, legacy).activate(load_state())
            else:
                with lock(DB_LOCK):
                    value = Controller(panel, legacy).restore_original(load_state())
    print(json.dumps(value, sort_keys=True), flush=True)
    return 0


if __name__ == '__main__':
    sys.dont_write_bytecode = True
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({'status': 'NATIVE_SCHEDULE_STOP', 'code': safe_code(error),
                          'backupStoppedByController': False}), file=sys.stderr, flush=True)
        raise SystemExit(2)
