#!/usr/bin/env python3
"""VM100 configuration snapshot and isolated restore validation; no live restore.

Only the reviewed managed paths below, three online SQLite snapshots and two
canonical normalized-log exports are included. The gateway's live bbolt DB,
raw logs, application databases, Docker images and offline age identity are
never archived. No services, networking, deployment or schedules are changed.
"""
from __future__ import annotations

import argparse
import configparser
import datetime as dt
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import resource
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import urllib.parse
import zipfile

HOST = 'asa-school-01'
INSTANCE = 'fc0ba60fe1d946bba1694d73557cc11f'
PROJECT = 'asa-lab-school'
APP_ROOT = Path('/srv/asa-lab')
REPOSITORY = 'spikeal8-maker/asa-lab'
SHA1 = re.compile(r'[a-f0-9]{40}')
IMAGE_ID = re.compile(r'sha256:[a-f0-9]{64}')
RELEASE_SERVICES = {
    'asa-deployment-v1': {'api', 'web', 'scratch', 'minio'},
    'asa-deployment-v2': {'api', 'web', 'scratch', 'minio', 'search'},
}
LEGACY_REVISION = '3e8713476cafedda3e24a39665458e833efd7c22'
LEGACY_API_IMAGE = 'sha256:8c75744e9ffa805b9f10fd08755b55de2148bf4b420fa008e78ffc28aec4e36f'
LEGACY_EXPORTER_BLOB = '47e562c63f0246648736461aabc31c70bac670ce'
BASE_REVISION = '68b20c23dfb07ebb533b2ca62dd04c4a6ce758fe'
PUBLIC_RECIPIENT_SHA256 = '32c01d34f5d6e30bdeb5d2e96c9d948251d2a33fdd88a3509cd3e79c8f590fcc'
TARGET = Path('/var/backups/server-settings')
RECOVERY = TARGET / 'school-recovery'
RECIPIENTS = Path('/etc/server-settings/age-recipients.txt')
GATEWAY = Path('/var/lib/private/asa-school-mcp-gateway')
NAME = re.compile(r'asa-school-config-01-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}[.]tar[.]age\Z')
MAX_CIPHER = 512 * 1024**2
MAX_CONFIG = 2 * 1024**2
MAX_SQLITE = 128 * 1024**2
MAX_ZIP = 256 * 1024**2
MAX_RECORD_BYTES = 2 * 1024**3
MAX_WINDOW_ZIP = 64 * 1024**2
MAX_WINDOW_RAW = 512 * 1024**2
MAX_WINDOW_SEGMENTS = 20000
LOG_SETTLE_SECONDS = 120
MAX_FILES = 160
DB_NAMES = ('core.db', 'agent.db', 'xpack.db')
LEGACY_FORMAT = 'asa-school-managed-config-v1'
RELEASE_FORMAT = 'asa-school-managed-config-v2'
FORMAT = 'asa-school-managed-config-v3'
LOG_TIME = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z')
FULL_LOG_WINDOW = {'from': '1970-01-01T00:00:00.000Z', 'to': '9999-12-31T23:59:59.999Z'}

UNITS = (
    '1panel-core.service', '1panel-agent.service', 'asa-frpc.service',
    'asa-admin-executor.socket', 'asa-admin-executor@.service',
    'asa-operations.service', 'asa-school-mcp-oauth-gateway.service',
    'server-settings-1panel-mcp.service', 'asa-log-collector-asa-lab-school.service',
    'asa-school-nightly-database-backup.service', 'asa-school-nightly-database-backup.timer',
)
STATIC = tuple(map(Path, (
    '/usr/local/bin/1pctl',
    '/opt/1panel/apps/openresty/openresty/docker-compose.yml',
    '/opt/1panel/apps/openresty/openresty/data.yml',
    '/opt/1panel/apps/openresty/openresty/.env',
    '/opt/1panel/www/conf.d/asa-lab-x.conf',
    '/opt/1panel/www/conf.d/asa-school-panel.conf',
    '/opt/1panel/www/sites/asa-lab-x/proxy/asa-mcp-identity.conf',
    '/opt/1panel/www/sites/asa-lab-x/proxy/asa-mcp-login.conf',
    '/opt/1panel/www/sites/asa-lab-x/proxy/asa-mcp-oauth-jwks.conf',
    '/opt/1panel/www/sites/asa-lab-x/proxy/asa-mcp-oauth-metadata.conf',
    '/opt/1panel/www/sites/asa-lab-x/proxy/asa-mcp.conf',
    '/opt/1panel/www/sites/asa-lab-x/proxy/root.conf',
    '/opt/1panel/www/sites/asa-school-panel/proxy/root.conf',
    '/opt/1panel/apps/openresty/openresty/conf/fastcgi-php.conf',
    '/opt/1panel/apps/openresty/openresty/conf/fastcgi_params',
    '/opt/1panel/apps/openresty/openresty/conf/mime.types',
    '/opt/1panel/apps/openresty/openresty/conf/nginx.conf',
    '/opt/1panel/apps/openresty/openresty/conf/default/00.default.conf',
    '/opt/1panel/apps/openresty/openresty/conf/default/default.conf',
    '/opt/1panel/apps/openresty/openresty/conf/ssl/fullchain.pem',
    '/opt/1panel/apps/openresty/openresty/conf/ssl/privkey.pem',
    '/opt/1panel/apps/openresty/openresty/conf/ssl/root_ssl.conf',
    '/opt/1panel/apps/openresty/openresty/1pwaf/data/conf/waf.conf',
    '/etc/asa-frpc/frpc.toml', '/etc/asa-frpc/frps-token',
    '/etc/server-settings/asa-operations.json', '/etc/server-settings/asa-operations.env',
    '/etc/server-settings/asa-admin-executor.json', '/etc/server-settings/mcp.env',
    '/etc/server-settings/asa-school-mcp-gateway.env', '/etc/server-settings/age-recipients.txt',
    '/var/lib/server-settings/1panel-admin.env', '/var/lib/server-settings/1panel-api.key',
    '/var/lib/server-settings/asa-school-mcp-owner-password',
    '/var/lib/server-settings/asa-school-host-admin-install.json',
    '/var/lib/private/asa-school-mcp-gateway/secret',
    '/var/lib/private/asa-school-mcp-gateway/private_key.pem',
    '/srv/asa-cloud-storage/compose.json', '/srv/asa-cloud-storage/config/rclone.conf',
    '/srv/asa-lab/.asa/installation.json', '/srv/asa-lab/.asa/log-collector.json',
    '/srv/asa-lab/.asa/collector-runner.json',
    '/etc/systemd/system/asa-school-mcp-oauth-gateway.service.d/20-host-admin.conf',
))) + tuple(Path('/etc/systemd/system') / name for name in UNITS)
CONFIG_ROOTS = tuple(map(Path, (
    '/opt/1panel/www/conf.d', '/opt/1panel/www/sites/asa-lab-x/proxy',
    '/opt/1panel/www/sites/asa-school-panel/proxy',
    '/opt/1panel/apps/openresty/openresty/conf',
    '/opt/1panel/apps/openresty/openresty/1pwaf/data/conf', '/opt/1panel/www/stream.d',
)))
STREAM = Path('/opt/1panel/www/stream.d')
SOURCE_BLOBS = {
    'school_yandex_app_folder.py': 'f6de5a1a338a8ce9d848b51bf32496d4dc37ad4c',
    'cloud_storage.py': '9264f512f2e5f4e974cb17f04973c231486f0aea',
}

# Optional versioned 1Panel integration. Old verified archives remain valid.
# Once a feature is present, its complete configuration set is mandatory.
SUPERVISOR_CONFIG = Path('/etc/supervisor/supervisord.conf')
SUPERVISOR_ROOT = Path('/opt/1panel/tools/supervisord')
PANEL_COLLECTOR = tuple(map(Path, (
    '/var/lib/server-settings/asa-1panel-collector.json',
    '/usr/local/sbin/asa-1panel-collector',
    '/opt/1panel/tools/supervisord/supervisor.d/asa-log-collector-asa-lab-school.ini',
)))
PANEL_SCHEDULES = tuple(map(Path, (
    '/var/lib/server-settings/1panel-backup-schedules/state.json',
    '/opt/1panel/tools/supervisord/supervisor.d/asa-school-nightly-database-backup.ini',
    '/opt/1panel/tools/supervisord/supervisor.d/asa-school-nightly-settings-backup.ini',
)))
PANEL_OPTIONAL = tuple(map(Path, (
    '/var/lib/server-settings/supervisord-before-1panel.conf',
    '/srv/asa-lab/.asa/diagnostics/private/1panel-handover.json',
)))
SCHEDULE_RELEASES = Path('/opt/asa-school-panel-schedules/releases')
SCHEDULE_SOURCE = re.compile(r'/opt/asa-school-panel-schedules/releases/[a-f0-9]{16}/school-1panel-backup-schedules[.]py\Z')
LEGACY_COLLECTOR = (Path('/etc/systemd/system/asa-log-collector-asa-lab-school.service'),)
LEGACY_DATABASE = tuple(Path('/etc/systemd/system/asa-school-nightly-database-backup.' + suffix)
                        for suffix in ('service', 'timer'))
LEGACY_GROUPS = (LEGACY_COLLECTOR, LEGACY_DATABASE)
LEGACY_PATHS = frozenset(LEGACY_COLLECTOR + LEGACY_DATABASE)
PANEL_BRIDGE_SHA256 = 'ad0ba649f75f75fef511d5005843b3aebcf2994b8751fbc4ae1d3ec274970277'


def legacy_omissions(paths):
    """A legacy group is complete or replaced by the full native proof set."""
    paths = set(paths)
    missing = set()
    for legacy, native in ((LEGACY_COLLECTOR, (*PANEL_COLLECTOR, PANEL_OPTIONAL[1])),
                           (LEGACY_DATABASE, PANEL_SCHEDULES)):
        present = paths.intersection(legacy)
        need(not present or len(present) == len(legacy), 'PARTIAL_LEGACY_CONFIGURATION')
        if not present:
            need(set((*native, SUPERVISOR_CONFIG)).issubset(paths), 'NATIVE_OWNERSHIP_SCOPE_REQUIRED')
            if legacy == LEGACY_DATABASE:
                need(any(SCHEDULE_SOURCE.fullmatch(str(p)) for p in paths), 'MANIFEST_PANEL_RELEASE_MISSING')
            missing.update(legacy)
    return missing


def native_ownership_for_omissions(paths, read_bytes):
    """Validate captured bytes only. Never import archived code or call 1Panel."""
    missing = legacy_omissions(paths)
    if not missing:
        return

    def document(path):
        value = json.loads(read_bytes(path))
        need(isinstance(value, dict), 'NATIVE_OWNERSHIP_DOCUMENT')
        return value

    def ini(path):
        value = configparser.ConfigParser(interpolation=None)
        value.read_string(read_bytes(path).decode('utf-8'))
        need(not value.defaults(), 'NATIVE_CONFIGURATION_DEFAULTS')
        return value

    supervisor = ini(SUPERVISOR_CONFIG)
    need(supervisor.get('include', 'files', fallback='') == str(SUPERVISOR_ROOT / 'supervisor.d/*.ini')
         and not any(section.startswith(('program:', 'group:')) for section in supervisor.sections()),
         'NATIVE_SUPERVISOR_SCOPE')

    if set(LEGACY_COLLECTOR).issubset(missing):
        config = document(PANEL_COLLECTOR[0])
        expected = {'format': 'asa-1panel-collector-v1', 'root': '/srv/asa-lab', 'project': PROJECT,
                    'instanceId': INSTANCE, 'panelHost': 'http://127.0.0.1:19192',
                    'panelKeyFile': '/var/lib/server-settings/1panel-api.key', 'panelVersion': 'v2.3.2',
                    'python': '/usr/bin/python3', 'program': 'asa-log-collector-' + PROJECT}
        need(config == expected and hashlib.sha256(read_bytes(PANEL_COLLECTOR[1])).hexdigest() == PANEL_BRIDGE_SHA256,
             'NATIVE_COLLECTOR_BRIDGE_CHANGED')
        settings = document(Path('/srv/asa-lab/.asa/installation.json'))
        runner = document(Path('/srv/asa-lab/.asa/collector-runner.json'))
        logs = document(Path('/srv/asa-lab/.asa/log-collector.json'))
        handover = document(PANEL_OPTIONAL[1])
        need(settings.get('instanceId') == INSTANCE and settings.get('project') == PROJECT
             and settings.get('collectorMode') == 'system-root' and settings.get('collectorManager') == '1panel-supervisor'
             and runner.get('root') == '/srv/asa-lab' and runner.get('project') == PROJECT
             and runner.get('mode') == 'system-root' and runner.get('owner') == 'root'
             and runner.get('name') == LEGACY_COLLECTOR[0].name and runner.get('manager') == '1panel-supervisor'
             and re.fullmatch(r'[a-f0-9]{40}', str(runner.get('revision')))
             and re.fullmatch(r'[a-f0-9]{64}', str(runner.get('scriptDigest'))), 'NATIVE_COLLECTOR_IDENTITY')
        need(handover.get('format') == 'asa-1panel-handover-v1' and handover.get('state') == 'accepted'
             and handover.get('instanceId') == INSTANCE and handover.get('project') == PROJECT
             and handover.get('legacyState', {}).get('LoadState') == 'not-found'
             and handover.get('targetRunner') == runner and handover.get('targetSettings') == settings
             and handover.get('targetCollectorConfiguration') == logs, 'NATIVE_COLLECTOR_OWNERSHIP')
        need(isinstance(logs.get('fileSources'), list) and
             [v for v in logs['fileSources'] if isinstance(v, dict) and v.get('source') == 'linux:1panel-supervisor'] ==
             [{'source': 'linux:1panel-supervisor', 'path': str(SUPERVISOR_ROOT / 'log')}], 'NATIVE_COLLECTOR_LOG_SOURCE')
        section = 'program:' + config['program']
        process = ini(PANEL_COLLECTOR[2])
        values = {'command': '/usr/local/sbin/asa-1panel-collector run --root /srv/asa-lab --project ' + PROJECT,
                  'directory': '/srv/asa-lab', 'autorestart': 'true', 'autostart': 'true', 'startsecs': '3',
                  'user': 'root', 'priority': '999', 'numprocs': '1',
                  'process_name': '%(program_name)s_%(process_num)02d', 'environment': ''}
        for stream, suffix in (('stdout', 'out'), ('stderr', 'err')):
            values[stream + '_logfile'] = str(SUPERVISOR_ROOT / 'log' / (config['program'] + '.' + suffix + '.log'))
            values[stream + '_logfile_maxbytes'] = '2MB'
        need(process.sections() == [section] and dict(process[section]) == values, 'NATIVE_COLLECTOR_PROCESS_CHANGED')

    if set(LEGACY_DATABASE).issubset(missing):
        state = document(PANEL_SCHEDULES[0])
        kinds = ('database', 'settings')
        generation = str(state.get('generation'))
        checksum = str(state.get('programSha256'))
        source = Path(str(state.get('programPath')))
        need(state.get('format') == 'asa-school-native-backup-schedules-v1'
             and state.get('instance') == INSTANCE and state.get('project') == PROJECT
             and state.get('phase') == 'ACTIVE' and state.get('owner') == '1panel'
             and state.get('originalOwner') == 'none' and state.get('clockTimeZone') == 'Europe/Moscow'
             and state.get('legacy') == {kind: {'timer': None, 'service': None} for kind in kinds}
             and re.fullmatch(r'[a-f0-9]{32}', generation) and re.fullmatch(r'[a-f0-9]{64}', checksum)
             and source == SCHEDULE_RELEASES / checksum[:16] / 'school-1panel-backup-schedules.py'
             and source in paths and hashlib.sha256(read_bytes(source)).hexdigest() == checksum,
             'NATIVE_SCHEDULE_OWNERSHIP')
        jobs = state.get('jobs')
        need(isinstance(jobs, dict) and set(jobs) == set(kinds), 'NATIVE_SCHEDULE_JOBS')
        dispatches, job_ids = set(), set()
        for kind, path in zip(kinds, PANEL_SCHEDULES[1:]):
            job = jobs[kind]
            probe = job.get('probe', {}) if isinstance(job, dict) else {}
            need(isinstance(job, dict) and type(job.get('id')) is int and job['id'] > 0
                 and job.get('processConfigured') is True and probe.get('status') == 'NATIVE_GATED_PROBE_PASS'
                 and probe.get('backupExecuted') is False and probe.get('programSha256') == checksum
                 and re.fullmatch(r'[a-f0-9]{32}', str(probe.get('dispatchId'))), 'NATIVE_SCHEDULE_PROBE')
            dispatches.add(probe['dispatchId'])
            job_ids.add(job['id'])
            program = 'asa-school-nightly-' + kind + '-backup'
            values = {'command': '/usr/bin/python3 -B ' + str(source) + ' worker --kind ' + kind +
                      ' --generation ' + generation + ' --apply', 'directory': '/srv/asa-lab', 'user': 'root',
                      'numprocs': '1', 'process_name': '%(program_name)s_%(process_num)02d',
                      'autostart': 'false', 'autorestart': 'false', 'startsecs': '0', 'startretries': '0',
                      'stopsignal': 'TERM', 'stopwaitsecs': '2147483647', 'stopasgroup': 'false',
                      'killasgroup': 'false', 'exitcodes': '0', 'priority': '999', 'umask': '0077',
                      'environment': 'PYTHONDONTWRITEBYTECODE="1",PYTHONUNBUFFERED="1",TZ="Europe/Moscow",LC_ALL="C"'}
            for stream, suffix in (('stdout', 'out'), ('stderr', 'err')):
                values[stream + '_logfile'] = str(SUPERVISOR_ROOT / 'log' / (program + '.' + suffix + '.log'))
                values[stream + '_logfile_maxbytes'] = '2MB'
                values[stream + '_logfile_backups'] = '10'
            process = ini(path)
            need(process.sections() == ['program:' + program] and dict(process['program:' + program]) == values,
                 'NATIVE_SCHEDULE_PROCESS_CHANGED')
        need(len(dispatches) == 2 and len(job_ids) == 2, 'NATIVE_SCHEDULE_DUPLICATE_IDENTITY')


def panel_configuration_paths():
    """Only known process/config/code files; no raw log trees or cursor DBs."""
    selected = []
    for group in (PANEL_COLLECTOR, PANEL_SCHEDULES):
        present = [p for p in group if p.exists() or p.is_symlink()]
        if present:
            need(len(present) == len(group), 'PANEL_CONFIGURATION_INCOMPLETE')
            selected += present
    if not selected:
        return []
    need(SUPERVISOR_CONFIG.is_file() and not SUPERVISOR_CONFIG.is_symlink(), 'PANEL_SUPERVISOR_CONFIG_MISSING')
    selected.append(SUPERVISOR_CONFIG)
    selected += [p for p in PANEL_OPTIONAL if p.exists() or p.is_symlink()]
    directory = SUPERVISOR_ROOT / 'supervisor.d'
    trusted(directory, directory=True)
    actual = list(directory.iterdir())
    allowed = set(PANEL_COLLECTOR + PANEL_SCHEDULES)
    need(len(actual) <= 3 and all(p in allowed for p in actual), 'PANEL_FOREIGN_PROGRAM_REQUIRES_REVIEW')
    if PANEL_SCHEDULES[0] in selected:
        trusted(SCHEDULE_RELEASES, directory=True)
        releases = list(SCHEDULE_RELEASES.iterdir())
        need(1 <= len(releases) <= 4 and all(re.fullmatch(r'[a-f0-9]{16}', p.name) for p in releases),
             'PANEL_RELEASE_SET_BOUND')
        for release in releases:
            trusted(release, directory=True)
            source = release / 'school-1panel-backup-schedules.py'
            need(source.is_file() and not source.is_symlink(), 'PANEL_RELEASE_SOURCE_MISSING')
            selected.append(source)
    return selected


class SnapshotError(Exception):
    pass


def need(value, code):
    if not value:
        raise SnapshotError(code)


def stamp():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def archive_name(name):
    need(isinstance(name, str) and NAME.fullmatch(name), 'CONFIG_ARCHIVE_NAME_REJECTED')
    return name


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns,
            info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode), info.st_nlink)


def metadata(info):
    return {'uid': info.st_uid, 'gid': info.st_gid, 'mode': stat.S_IMODE(info.st_mode),
            'bytes': info.st_size, 'mtimeNs': info.st_mtime_ns}


def trusted(path, *, owner=None, directory=False):
    path = Path(path)
    for current in (path, *path.parents):
        info = current.lstat()
        allowed = {0}
        if current == Path('/srv/asa-cloud-storage/config'):
            allowed.add(1000)
        if current == GATEWAY:
            allowed.add(GATEWAY.lstat().st_uid)
        if current == path and owner is not None:
            allowed.add(owner)
        need(not stat.S_ISLNK(info.st_mode) and info.st_uid in allowed
             and not info.st_mode & 0o022, 'SOURCE_PATH_UNTRUSTED')
    info = path.lstat()
    need(stat.S_ISDIR(info.st_mode) if directory else
         stat.S_ISREG(info.st_mode) and info.st_nlink == 1, 'SOURCE_TYPE_UNTRUSTED')
    return info


def source_owner(path):
    if path == Path('/srv/asa-cloud-storage/config/rclone.conf'):
        return 1000
    return GATEWAY.lstat().st_uid if path.parent == GATEWAY else 0


def stable_read(path, limit=MAX_CONFIG):
    path = Path(path)
    info = trusted(path, owner=source_owner(path))
    need(info.st_size <= limit, 'CONFIG_FILE_TOO_LARGE')
    need(not os.listxattr(path, follow_symlinks=False), 'EXTENDED_ATTRIBUTES_REQUIRE_REVIEW')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        need(signature(os.fstat(stream.fileno())) == signature(info), 'CONFIG_CHANGED_BEFORE_READ')
        data = stream.read(limit + 1)
        need(len(data) == info.st_size and signature(os.fstat(stream.fileno())) == signature(info)
             and signature(path.lstat()) == signature(info), 'CONFIG_CHANGED_DURING_READ')
    return data, info


def private_directory(path, *, create=False):
    path = Path(path)
    if create and not path.exists():
        trusted(path.parent, directory=True)
        path.mkdir(mode=0o700)
    info = trusted(path, directory=True)
    need(info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o700, 'PRIVATE_DIRECTORY_MODE')


def private_write(path, data):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def write_json(path, data):
    private_write(path, (json.dumps(data, sort_keys=True, separators=(',', ':')) + '\n').encode())


def run(args, *, timeout=30, output=None, pass_fds=(), limit=None):
    def bound():
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        if limit is not None:
            resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))
    done = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=output or subprocess.PIPE,
        stderr=subprocess.PIPE, timeout=timeout, check=False, pass_fds=pass_fds, preexec_fn=bound)
    need(done.returncode == 0, 'SUBPROCESS_FAILED_' + Path(args[0]).name.upper().replace('-', '_'))
    return done.stdout or b''


def check_host():
    need(os.geteuid() == 0 and socket.gethostname() == HOST, 'VM100_ROOT_REQUIRED')
    raw, _ = stable_read(APP_ROOT / '.asa' / 'installation.json')
    identity = json.loads(raw)
    need(identity.get('instanceId') == INSTANCE and identity.get('project') == PROJECT,
         'SCHOOL_INSTANCE_CHANGED')


def pinned_transport():
    for name, expected in SOURCE_BLOBS.items():
        path = Path(__file__).with_name(name)
        raw, _ = stable_read(path, 1024 * 1024)
        actual = hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()
        need(actual == expected, 'TRANSPORT_SOURCE_CHANGED')
    spec = importlib.util.spec_from_file_location('school_config_app_folder',
                                                Path(__file__).with_name('school_yandex_app_folder.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def select_container(label):
    raw = run(['/usr/bin/docker', 'ps', '--all', '--filter', 'label=' + label, '--format', '{{.ID}}'])
    need(len(raw) <= 256, 'CONTAINER_SELECTION_BOUND')
    ids = raw.decode('ascii').split()
    need(len(ids) == 1 and re.fullmatch(r'[a-f0-9]{12,64}', ids[0]), 'EXACT_CONTAINER_REQUIRED')
    return ids[0]


def docker_view(container, expression):
    raw = run(['/usr/bin/docker', 'inspect', '--format', expression, container])
    need(len(raw) <= 32768, 'DOCKER_METADATA_BOUND')
    return json.loads(raw)


def validate_application_release(release, code):
    # Mirror the supported deployment manifests from the ASA updater. The
    # archived declaration contains only these public release identity fields.
    need(isinstance(release, dict), code)
    release_format = release.get('format')
    need(isinstance(release_format, str) and release_format in RELEASE_SERVICES
         and release.get('repository') == REPOSITORY and release.get('platform') == 'linux/amd64'
         and isinstance(release.get('revision'), str) and SHA1.fullmatch(release['revision'])
         and type(release.get('schema')) is int and release['schema'] > 0
         and type(release.get('publicationRunId')) is int and release['publicationRunId'] > 0, code)
    images = release.get('images')
    need(isinstance(images, dict) and images.keys() == RELEASE_SERVICES[release_format], code)
    for service, reference in images.items():
        prefix = 'ghcr.io/' + REPOSITORY + '/' + service + '@'
        need(isinstance(reference, str) and reference.startswith(prefix)
             and IMAGE_ID.fullmatch(reference.removeprefix(prefix)), code)
    fields = ('format', 'repository', 'revision', 'schema', 'platform', 'publicationRunId', 'images')
    return {key: release[key] for key in fields}


def attested_api_runtime():
    raw, _ = stable_read(APP_ROOT / '.asa' / 'installed-release.json', 65536)
    try:
        installed = json.loads(raw)
    except ValueError:
        raise SnapshotError('API_RELEASE_MANIFEST_INVALID') from None
    release = validate_application_release(installed, 'API_RELEASE_MANIFEST_INVALID')
    reference = release['images']['api']
    raw = run(['/usr/bin/docker', 'ps', '--all', '--filter',
        'label=com.docker.compose.project=' + PROJECT, '--filter',
        'label=com.docker.compose.service=api', '--format', '{{.ID}}'])
    need(len(raw) <= 256, 'CONTAINER_SELECTION_BOUND')
    ids = raw.decode('ascii').split()
    need(len(ids) == 1 and re.fullmatch(r'[a-f0-9]{12,64}', ids[0]), 'EXACT_API_REQUIRED')
    cid = ids[0]
    value = docker_view(cid,
        '{"id":{{json .Id}},"image":{{json .Image}},"imageReference":{{json .Config.Image}},'
        '"labels":{{json .Config.Labels}},"user":{{json .Config.User}},'
        '"state":{{json .State.Status}},"health":{{json .State.Health.Status}},'
        '"startedAt":{{json .State.StartedAt}},"mounts":{{json .Mounts}}}')
    labels = value['labels'] or {}
    need(isinstance(value['id'], str) and re.fullmatch(r'[a-f0-9]{64}', value['id'])
         and value['id'].startswith(cid) and value['imageReference'] == reference
         and value['user'] == 'node' and value['state'] == 'running' and value['health'] == 'healthy'
         and labels.get('com.docker.compose.project') == PROJECT
         and labels.get('com.docker.compose.service') == 'api'
         and labels.get('com.docker.compose.project.working_dir') == str(APP_ROOT)
         and labels.get('org.opencontainers.image.revision') == release['revision'],
         'API_RUNTIME_CHANGED')
    raw = run(['/usr/bin/docker', 'image', 'inspect', '--format',
        '{"id":{{json .Id}},"digests":{{json .RepoDigests}},"os":{{json .Os}},'
        '"architecture":{{json .Architecture}},"revision":{{json (index .Config.Labels "org.opencontainers.image.revision")}}}',
        reference])
    need(len(raw) <= 32768, 'DOCKER_METADATA_BOUND')
    image = json.loads(raw)
    need(isinstance(image.get('id'), str) and IMAGE_ID.fullmatch(image['id'])
         and image['id'] == value['image'] and isinstance(image.get('digests'), list)
         and reference in image['digests'] and image.get('revision') == release['revision']
         and image.get('os') == 'linux' and image.get('architecture') == 'amd64', 'API_IMAGE_DRIFT')
    mounts = {m['Destination']: m for m in value['mounts']}
    host = mounts.get('/var/lib/asa-logs', {})
    app = mounts.get('/var/lib/asa-application-logs', {})
    need(len(mounts) == len(value['mounts'])
         and not any(path == '/' or path == '/app' or path.startswith('/app/') for path in mounts)
         and host.get('Type') == 'bind' and host.get('Source') == str(APP_ROOT / '.asa/diagnostics/store')
         and host.get('RW') is False and app.get('Type') == 'volume'
         and app.get('Name') == PROJECT + '_application-logs', 'LOG_MOUNTS_CHANGED')
    return {'container': value['id'], 'startedAt': value['startedAt'], 'release': release, 'image': image['id']}


def api_runtime():
    return attested_api_runtime()['container']


def allowed_config(path):
    return path in STATIC or path in (SUPERVISOR_CONFIG, *PANEL_COLLECTOR, *PANEL_SCHEDULES, *PANEL_OPTIONAL) \
        or bool(SCHEDULE_SOURCE.fullmatch(str(path))) \
        or bool(re.fullmatch(r'/[A-Za-z0-9_./-]{1,512}', str(path))) and any(path.is_relative_to(root) and path.suffix in
        ('.conf', '.pem', '.crt', '.key', '.types', '.htpasswd') for root in CONFIG_ROOTS)


def config_inputs():
    cid = select_container('com.docker.compose.project.config_files='
                          '/opt/1panel/apps/openresty/openresty/docker-compose.yml')
    mounts = docker_view(cid, '{{json .Mounts}}')
    need(isinstance(mounts, list) and len(mounts) <= 16, 'OPENRESTY_MOUNTS_BOUND')
    bindings = [(m['Destination'], m['Source']) for m in mounts
                if m.get('Type') == 'bind' and str(m.get('Source', '')).startswith('/opt/1panel/')]
    pending = [path for path in STATIC if path not in LEGACY_PATHS or path.exists() or path.is_symlink()] \
        + panel_configuration_paths()
    missing_legacy = legacy_omissions(pending)
    observations = {}
    if STREAM.exists():
        trusted(STREAM, directory=True)
        matches = sorted(STREAM.glob('*.conf'))
        need(len(matches) <= 64, 'STREAM_CONFIG_BOUND')
        pending += matches
        observations[str(STREAM)] = signature(STREAM.lstat())
    else:
        need(not STREAM.is_symlink(), 'STREAM_DIRECTORY_SYMLINK')
        observations[str(STREAM)] = None
    files, snapshots, dependencies = {}, {str(path): None for path in missing_legacy}, []
    while pending:
        path = pending.pop(0)
        if path in files:
            continue
        need(allowed_config(path) and len(files) < MAX_FILES, 'UNREVIEWED_CONFIG_PATH')
        data, info = stable_read(path)
        files[path] = (data, info)
        snapshots[str(path)] = signature(info)
        need(sum(len(v[0]) for v in files.values()) <= 8 * 1024**2, 'CONFIG_TOTAL_BOUND')
        if path.suffix != '.conf':
            continue
        text = data.decode('utf-8')
        for match in re.finditer(r'(?m)^\s*(include|ssl_certificate|ssl_certificate_key|auth_basic_user_file)\s+([^;]+);', text):
            directive, raw = match.groups()
            value = raw.strip().strip('\"\'')
            need(re.fullmatch(r'[A-Za-z0-9_./*?-]{1,512}', value) and '..' not in PurePosixPath(value).parts,
                 'DYNAMIC_CONFIG_DEPENDENCY_REQUIRES_REVIEW')
            if not value.startswith('/'):
                value = '/usr/local/openresty/nginx/conf/' + value
            options = [(dst, src) for dst, src in bindings
                       if value == dst or value.startswith(dst.rstrip('/') + '/')]
            need(options, 'UNMAPPED_CONFIG_DEPENDENCY_REQUIRES_REVIEW')
            dst, src = max(options, key=lambda pair: len(pair[0]))
            target = Path(src.rstrip('/') + value[len(dst):])
            need(any(target.is_relative_to(root) for root in CONFIG_ROOTS),
                 'OUTSIDE_CONFIG_DEPENDENCY_REQUIRES_REVIEW')
            need(not any(c in str(target.parent) for c in '*?['), 'DEPENDENCY_GLOB_SCOPE')
            if '*' in target.name or '?' in target.name:
                need(directive == 'include' and target.suffix == '.conf', 'DEPENDENCY_GLOB_TYPE')
                if target.parent.exists():
                    trusted(target.parent, directory=True)
                    found = sorted(target.parent.glob(target.name))
                    observations[str(target.parent)] = signature(target.parent.lstat())
                else:
                    need(not target.parent.is_symlink(), 'DEPENDENCY_DIRECTORY_SYMLINK')
                    found = []
                    observations[str(target.parent)] = None
                need(len(found) <= 64, 'DEPENDENCY_GLOB_BOUND')
            else:
                found = [target]
            dependencies.append({'config': str(path), 'directive': directive,
                                 'target': str(target), 'matchedFiles': len(found)})
            pending += found
    native_ownership_for_omissions(files, lambda path: files[path][0])
    for path in files:
        for parent in path.parents:
            if parent == Path('/'):
                break
            observations.setdefault(str(parent), signature(parent.lstat()))
    frp = tomllib.loads(files[Path('/etc/asa-frpc/frpc.toml')][0].decode('utf-8'))
    auth = frp.get('auth', {})
    tls = frp.get('transport', {}).get('tls', {})
    need(auth.get('tokenSource', {}).get('file', {}).get('path') == '/etc/asa-frpc/frps-token'
         and auth.get('token') is None and frp.get('includes', []) == []
         and all(tls.get(key) is None for key in ('trustedCaFile','certFile','keyFile')),
         'FRP_DEPENDENCIES_CHANGED')
    return files, snapshots, observations, dependencies


def unchanged(snapshots, directories):
    for value, expected in snapshots.items():
        path = Path(value)
        actual = signature(path.lstat()) if path.exists() or path.is_symlink() else None
        need(actual == expected, 'CONFIG_SET_CHANGED_DURING_SNAPSHOT')
    for value, expected in directories.items():
        path = Path(value)
        actual = signature(path.lstat()) if path.exists() or path.is_symlink() else None
        need((actual is None) == (expected is None), 'CONFIG_DIRECTORY_SET_CHANGED')
        if actual is not None:
            need(tuple(actual[i] for i in (0,1,5,6,7)) == tuple(expected[i] for i in (0,1,5,6,7)),
                 'CONFIG_DIRECTORY_PERMISSIONS_CHANGED')


def sqlite_integrity(path):
    deadline = time.monotonic() + 120
    connection = sqlite3.connect('file:' + urllib.parse.quote(str(path), safe='/') + '?mode=ro&immutable=1', uri=True)
    try:
        connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
        need(connection.execute('PRAGMA integrity_check').fetchall() == [('ok',)], 'SQLITE_INTEGRITY_FAILED')
    finally:
        connection.close()


def sqlite_snapshot(source, target):
    info = trusted(source)
    need(info.st_size <= MAX_SQLITE, 'SQLITE_SOURCE_BOUND')
    deadline = time.monotonic() + 120
    read = sqlite3.connect('file:' + urllib.parse.quote(str(source), safe='/') + '?mode=ro', uri=True, timeout=10)
    write = sqlite3.connect(target)
    os.chmod(target, 0o600)
    try:
        page_size = read.execute('PRAGMA page_size').fetchone()[0]
        need(512 <= page_size <= 65536 and page_size & (page_size - 1) == 0, 'SQLITE_PAGE_SIZE')
        def progress(status, remaining, total):
            need(time.monotonic() < deadline and total * page_size <= MAX_SQLITE,
                 'SQLITE_BACKUP_BOUND')
        read.backup(write, pages=256, progress=progress, sleep=0.05)
    finally:
        write.close()
        read.close()
    need(target.stat().st_size <= MAX_SQLITE, 'SQLITE_SNAPSHOT_BOUND')
    sqlite_integrity(target)
    return info


LOG_SCOPE_JS = r"""
import {createHash} from 'node:crypto';
import {lstat, mkdir, rm, stat, symlink, writeFile} from 'node:fs/promises';
import {join} from 'node:path';
const stores = [['host','/var/lib/asa-logs'],['application','/var/lib/asa-application-logs']];
const HOUR = 60*60*1000;
const MAX_CATALOG = 64*1024*1024;
const MAX_SEGMENT = 4*1024*1024;
const LOG_TIME = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/;
function timestamp(value) {
  return typeof value === 'string' && LOG_TIME.test(value) &&
    Number.isFinite(Date.parse(value)) && new Date(value).toISOString() === value;
}
function validWindow(window) {
  const hours = (Date.parse(window?.to) - Date.parse(window?.from)) / HOUR;
  return timestamp(window?.from) && timestamp(window?.to) && [1,3,6,12,24].includes(hours);
}
async function readStores(readLogCatalog) {
  const output = [];
  for (const [kind, root] of stores) {
    const size = (await stat(join(root,'catalog.json'))).size;
    if (size > MAX_CATALOG) throw Error('CATALOG_LIMIT');
    const catalog = await readLogCatalog(root);
    if (!catalog || (catalog.applicationOwned === true) !== (kind === 'application')) {
      throw Error('CATALOG_SCOPE');
    }
    output.push({kind,root,catalog});
  }
  return output;
}
function selectScope(catalog, window) {
  const selected = [];
  const files = new Set();
  let storedBytes = 0, rawBytes = 0;
  for (const s of catalog.segments) {
    if (!timestamp(s?.first) || !timestamp(s?.last) || s.first > s.last) throw Error('SEGMENT_TIME');
    if (s.last < window.from || s.first > window.to) continue;
    const range = /^seg-(\d{16})-(\d{16})\.jsonl(?:\.gz)?$/.exec(s.file);
    if (!range || range[1] > range[2] || files.has(s.file) ||
        !Number.isSafeInteger(s.bytes) || s.bytes < 1 || s.bytes > MAX_SEGMENT ||
        (s.schema === 2 && !/^[a-f0-9]{64}$/.test(s.sha256))) throw Error('SEGMENT_METADATA');
    const raw = s.rawBytes ?? (s.file.endsWith('.gz') ? MAX_SEGMENT : s.bytes);
    if (!Number.isSafeInteger(raw) || raw < 1 || raw > MAX_SEGMENT) throw Error('SEGMENT_RAW_BYTES');
    files.add(s.file);
    storedBytes += s.bytes;
    rawBytes += raw;
    selected.push(s);
  }
  selected.sort((a,b) => a.file.localeCompare(b.file));
  const digest = createHash('sha256');
  for (const s of selected) digest.update(JSON.stringify(s) + '\n');
  return {selected, proof:{segments:selected.length,storedBytes,rawBytes,sha256:digest.digest('hex')}};
}
function logPlan(input, window) {
  const selected = input.map(store => ({...store,...selectScope(store.catalog,window)}));
  const totals = {segments:0,storedBytes:0,rawBytes:0};
  for (const {proof} of selected) {
    for (const field of Object.keys(totals)) totals[field] += proof[field];
  }
  return {selected, catalogs:Object.fromEntries(selected.map(store => [store.kind,store.proof])),
    withinBounds:totals.segments <= SOURCE_LIMIT && totals.storedBytes <= ZIP_LIMIT &&
      totals.rawBytes <= RAW_LIMIT};
}
function boundedPlan(input, end) {
  for (const hours of [24,12,6,3,1]) {
    const window = {from:new Date(Date.parse(end)-hours*HOUR).toISOString(),to:end};
    const plan = logPlan(input,window);
    if (plan.withinBounds) return {window,...plan};
  }
  throw Error('WINDOW_LIMIT');
}
async function scopedCatalog(store, dir) {
  await mkdir(dir,{mode:0o700});
  for (let i=0; i<store.selected.length; i+=32) {
    await Promise.all(store.selected.slice(i,i+32).map(async segment => {
      const source = join(store.root,segment.file);
      const info = await lstat(source);
      if (!info.isFile() || info.size !== segment.bytes) throw Error('SEGMENT_CHANGED');
      await symlink(source,join(dir,segment.file));
    }));
  }
  const {version,applicationOwned,retentionDays,maxBytes,trimmed} = store.catalog;
  await writeFile(join(dir,'catalog.json'),
    JSON.stringify({version,applicationOwned,retentionDays,maxBytes,trimmed,sources:[],segments:store.selected}),
    {flag:'wx',mode:0o600});
}
"""


EXPORT_JS = LOG_SCOPE_JS + r"""
const work = process.argv[1];
const owner = process.argv[2];
let created = false;
const watchdog = setTimeout(()=>process.exit(124),210000);
watchdog.unref();
try {
  if (process.getuid() !== 1000 || process.getgid() !== 1000 ||
      !/^\/tmp\/asa-config-export-asa-school-config-01-\d{8}T\d{6}Z-[a-f0-9]{8}$/.test(work) ||
      !/^[a-f0-9]{32}$/.test(owner)) throw Error();
  const {window,catalogs} = JSON.parse(process.argv[3]);
  if (!validWindow(window)) throw Error('WINDOW_INVALID');
  const {readLogCatalog, exportLogs} = await import('/app/dist/admin-logs.worker.js');
  const filter = {scope:'all',...window,source:'',module:'',level:'',search:'',before:null};
  const plan = logPlan(await readStores(readLogCatalog),window);
  if (!plan.withinBounds) throw Error('WINDOW_LIMIT');
  if (JSON.stringify(plan.catalogs) !== JSON.stringify(catalogs)) throw Error('WINDOW_CHANGED');
  await mkdir(work,{mode:0o700}); created = true;
  await writeFile(work+'/.owner',owner,{flag:'wx',mode:0o600});
  const results = {};
  let outputBytes = 0;
  for (const store of plan.selected) {
    const dir = join(work, store.kind);
    const output = join(work, store.kind + '.zip');
    await scopedCatalog(store,dir);
    const result = await exportLogs(dir,filter,output);
    await rm(dir,{recursive:true});
    outputBytes += result.bytes;
    if (!Number.isSafeInteger(result.count) || result.count < 0 || outputBytes > ZIP_LIMIT ||
        (await stat(output)).size !== result.bytes) throw Error('ZIP_LIMIT');
    results[store.kind] = result;
  }
  const after = logPlan(await readStores(readLogCatalog),window);
  if (JSON.stringify(after.catalogs) !== JSON.stringify(catalogs)) throw Error('WINDOW_CHANGED');
  process.stdout.write(JSON.stringify({ok:true,results}));
} catch (error) {
  if (created) await rm(work,{recursive:true,force:true});
  const code = ['WINDOW_CHANGED','SEGMENT_CHANGED','LOG_SNAPSHOT_CHANGED'].includes(error.message) ||
    error.code === 'ENOENT' ? 'WINDOW_CHANGED' : 'CANONICAL_NORMALIZED_EXPORT_FAILED';
  process.stdout.write(JSON.stringify({ok:false,code}));
}
"""
PREFLIGHT_JS = LOG_SCOPE_JS + r"""
const out={uid:process.getuid(),gid:process.getgid(),groups:process.getgroups().sort((a,b)=>a-b),ready:false};
try {
  if (out.uid!==1000 || out.gid!==1000 || out.groups.length>64) throw Error('RUNTIME_GROUPS');
  const {readLogCatalog}=await import('/app/dist/admin-logs.worker.js');
  const input = await readStores(readLogCatalog);
  const frozen = process.argv[1] ? JSON.parse(process.argv[1]) : null;
  if (frozen && !validWindow(frozen)) throw Error('WINDOW_INVALID');
  const {window,catalogs,withinBounds} = frozen
    ? {window:frozen,...logPlan(input,frozen)} : boundedPlan(input,new Date(Date.now()-SETTLE_MS).toISOString());
  Object.assign(out,{window,catalogs,ready:withinBounds});
} catch (error) {
  out.code = ['WINDOW_LIMIT','SEGMENT_TIME','SEGMENT_METADATA','SEGMENT_RAW_BYTES','CATALOG_LIMIT',
    'CATALOG_SCOPE','RUNTIME_GROUPS'].includes(error.message) ? error.message : 'NORMALIZED_PREFLIGHT_FAILED';
}
process.stdout.write(JSON.stringify(out));
"""


def log_worker(source):
    return (f'const ZIP_LIMIT={MAX_WINDOW_ZIP}, RAW_LIMIT={MAX_WINDOW_RAW}, '
            f'SOURCE_LIMIT={MAX_WINDOW_SEGMENTS}, SETTLE_MS={LOG_SETTLE_SECONDS*1000};\n' + source)


def validate_window(window):
    need(isinstance(window, dict) and set(window) == {'from','to'}
         and all(isinstance(value, str) and LOG_TIME.fullmatch(value) for value in window.values()),
         'NORMALIZED_LOG_WINDOW')
    try:
        span = dt.datetime.fromisoformat(window['to']) - dt.datetime.fromisoformat(window['from'])
    except ValueError:
        raise SnapshotError('NORMALIZED_LOG_WINDOW') from None
    need(span in (dt.timedelta(hours=hours) for hours in (1,3,6,12,24)), 'NORMALIZED_LOG_WINDOW')
    return window


def validate_source_proofs(proofs):
    need(isinstance(proofs, dict) and set(proofs) == {'host','application'}, 'NORMALIZED_LOG_SOURCES')
    limits = {'segments':MAX_WINDOW_SEGMENTS, 'storedBytes':MAX_WINDOW_ZIP, 'rawBytes':MAX_WINDOW_RAW}
    for proof in proofs.values():
        need(isinstance(proof, dict) and set(proof) == set(limits) | {'sha256'}
             and isinstance(proof['sha256'], str) and re.fullmatch(r'[a-f0-9]{64}', proof['sha256'])
             and all(type(proof[key]) is int and proof[key] >= 0 for key in limits), 'NORMALIZED_LOG_SOURCES')
    need(all(sum(value[key] for value in proofs.values()) <= maximum for key, maximum in limits.items()),
         'NORMALIZED_LOG_SOURCES_BOUND')


def log_preflight(cid, window=None):
    # Keep the container's configured user and groups; do not override --user.
    arguments = [json.dumps(validate_window(window))] if window is not None else []
    raw = run(['/usr/bin/docker', 'exec', cid, 'node', '--max-old-space-size=256',
               '--input-type=module', '-e', log_worker(PREFLIGHT_JS), *arguments], timeout=45)
    need(len(raw) <= 4096, 'LOG_PREFLIGHT_BOUND')
    result = json.loads(raw)
    need(isinstance(result, dict) and set(result) <= {'uid','gid','groups','catalogs','window','ready','code'},
         'LOG_PREFLIGHT_RECEIPT')
    if result.get('ready') is True:
        selected = validate_window(result.get('window'))
        need(window is None or window == selected, 'LOG_PREFLIGHT_RECEIPT')
        validate_source_proofs(result.get('catalogs'))
    return result


CLEAN_JS = r"""
import {lstat,rm,readFile} from 'node:fs/promises';
const path=process.argv[1];
const owner=process.argv[2];
if(!/^\/tmp\/asa-config-export-asa-school-config-01-\d{8}T\d{6}Z-[a-f0-9]{8}$/.test(path))process.exit(2);
try {
  const s=await lstat(path);
  if(!s.isDirectory()||s.isSymbolicLink()||s.uid!==1000||(s.mode&0o777)!==0o700)process.exit(2);
  const marker=await lstat(path+'/.owner');
  if(!marker.isFile()||marker.isSymbolicLink()||marker.uid!==1000||marker.size!==32||
     (await readFile(path+'/.owner','utf8'))!==owner)process.exit(2);
  await rm(path,{recursive:true});
} catch(error) { if(error.code!=='ENOENT')process.exit(2); }
"""


def verify_zip(path, kind, window=None):
    maximum = MAX_ZIP if window is None else MAX_WINDOW_ZIP
    raw_maximum = MAX_RECORD_BYTES if window is None else MAX_WINDOW_RAW
    need(Path(path).stat().st_size <= maximum, 'ZIP_SIZE_BOUND')
    with zipfile.ZipFile(path) as archive:
        items = archive.infolist()
        names = [item.filename for item in items]
        need(2 <= len(items) <= 20002 and len(set(names)) == len(names), 'ZIP_MEMBER_BOUND')
        for item in items:
            need(item.compress_type == zipfile.ZIP_DEFLATED and not item.flag_bits & 1
                 and not stat.S_ISLNK(item.external_attr >> 16), 'ZIP_MEMBER_TYPE')
            limit = 8 * 1024**2 if item.filename == 'manifest.json' else 16384 if item.filename == 'README.txt' else 4 * 1024**2
            need(item.file_size <= limit, 'ZIP_MEMBER_SIZE')
        manifest = json.loads(archive.read('manifest.json'))
        archive.read('README.txt')
        need(manifest.get('normalized') is True and manifest.get('sourceStatusIncluded') is False
             and manifest.get('coverage', {}).get('complete') is False
             and manifest.get('searchApplied') is False, 'ZIP_NORMALIZATION_CONTRACT')
        need(manifest['coverage'].get('application') == ('embedded' if kind == 'application' else 'host-collector'),
             'ZIP_SOURCE_SCOPE')
        need(manifest.get('filter') == {'scope':'all',**(window or FULL_LOG_WINDOW),
             'source':'','module':'','level':'','before':None},
             'ZIP_FILTER_CHANGED')
        catalog = manifest.get('catalog', {})
        segments = catalog.get('segments', [])
        need(catalog.get('version') == 1 and catalog.get('sources') == []
             and isinstance(segments, list) and len(segments) <= 20000, 'ZIP_CATALOG_CONTRACT')
        expected = ['manifest.json', 'README.txt'] + [s['file'] for s in segments]
        need(set(names) == set(expected) and len(names) == len(expected), 'ZIP_MANIFEST_MEMBERS')
        count = total = 0
        for index, segment in enumerate(segments, 1):
            name = 'records/part-' + str(index).zfill(5) + '.jsonl'
            need(segment.get('file') == name and segment.get('schema') == 2
                 and re.fullmatch(r'[a-f0-9]{64}', str(segment.get('sha256'))), 'ZIP_SEGMENT_CONTRACT')
            h = hashlib.sha256()
            nbytes = nlines = 0
            with archive.open(name) as stream:
                while True:
                    line = stream.readline(131073)
                    if not line:
                        break
                    need(len(line) <= 131072 and line.endswith(b'\n'), 'ZIP_RECORD_BOUND')
                    row = json.loads(line)
                    need(isinstance(row, dict) and re.fullmatch(r'[a-f0-9]{64}', str(row.get('id')))
                         and all(isinstance(row.get(k), str) for k in ('time','source','module','level','message')),
                         'ZIP_RECORD_CONTRACT')
                    if window is not None:
                        need(LOG_TIME.fullmatch(row['time']) and window['from'] <= row['time'] <= window['to'],
                             'ZIP_RECORD_OUTSIDE_WINDOW')
                        try:
                            dt.datetime.fromisoformat(row['time'])
                        except ValueError:
                            raise SnapshotError('ZIP_RECORD_OUTSIDE_WINDOW') from None
                    nlines += 1
                    nbytes += len(line)
                    need(total + nbytes <= raw_maximum, 'ZIP_RAW_BYTES_BOUND')
                    h.update(line)
            need(nbytes == segment.get('bytes') == segment.get('rawBytes')
                 and nlines == segment.get('count') and h.hexdigest() == segment['sha256'], 'ZIP_SEGMENT_INTEGRITY')
            count += nlines
            total += nbytes
        need((count > 0 or window is not None and not segments)
             and count == manifest.get('count') and total == catalog.get('bytes'), 'ZIP_COUNT_OR_BYTES')
        return {'records': count, 'parts': len(segments), 'recordBytes': total,
                'coverageComplete': False, 'normalized': True, 'crcAndPartSha256Verified': True}


def verify_normalized_logs(stage, window):
    paths = {kind: stage / 'normalized' / (kind + '.zip') for kind in ('host', 'application')}
    if window is not None:
        need(sum(path.stat().st_size for path in paths.values()) <= MAX_WINDOW_ZIP, 'NORMALIZED_ZIP_TOTAL_BOUND')
    result = {kind: verify_zip(path, kind, window) for kind, path in paths.items()}
    if window is not None:
        need(sum(stats['recordBytes'] for stats in result.values()) <= MAX_WINDOW_RAW, 'NORMALIZED_RAW_TOTAL_BOUND')
    return result


def export_logs(cid, name, stage, access):
    work = '/tmp/asa-config-export-' + name.removesuffix('.tar.age')
    window = access['window']
    for attempt in range(2):
        owner = os.urandom(16).hex()
        expected = {'window':window, 'catalogs':access['catalogs']}
        try:
            # Limit actual writes in the container as well as transferred and verified data.
            raw = run(['/usr/bin/docker', 'exec', cid, '/bin/sh', '-c', 'ulimit -f "$1" && shift && exec "$@"',
                'asa-config-export', str(MAX_WINDOW_ZIP // 512), 'node', '--max-old-space-size=256',
                '--input-type=module', '-e', log_worker(EXPORT_JS), work, owner, json.dumps(expected)], timeout=240)
            need(len(raw) <= 4096, 'EXPORT_RECEIPT_BOUND')
            receipt = json.loads(raw)
            if receipt.get('code') == 'WINDOW_CHANGED' and attempt == 0:
                access = log_preflight(cid, window)
                need(access.get('ready') is True, 'NORMALIZED_WINDOW_CHANGED_OR_OVER_LIMIT')
                continue
            need(receipt.get('ok') is True, 'NORMALIZED_WINDOW_CHANGED' if receipt.get('code') == 'WINDOW_CHANGED'
                 else 'NORMALIZED_EXPORT_FAILED')
            for kind in ('host', 'application'):
                target = stage / 'normalized' / (kind + '.zip')
                with target.open('xb') as output:
                    os.chmod(target, 0o600)
                    run(['/usr/bin/docker', 'exec', cid, '/bin/cat',
                        work + '/' + kind + '.zip'], timeout=120, output=output, limit=MAX_WINDOW_ZIP)
                    output.flush()
                    os.fsync(output.fileno())
                need(target.stat().st_size == receipt['results'][kind]['bytes'], 'EXPORTED_ZIP_COPY_SIZE')
            result = verify_normalized_logs(stage, window)
            need(all(stats['records'] == receipt['results'][kind]['count']
                     for kind, stats in result.items()), 'EXPORT_RECORD_COUNT')
            return result, access['catalogs']
        finally:
            run(['/usr/bin/docker', 'exec', cid, 'node',
                 '--input-type=module', '-e', CLEAN_JS, work, owner], timeout=30)


def entry(path, member, info, kind):
    return {'member': member, 'kind': kind, 'bytes': path.stat().st_size,
            'sha256': digest(path), 'sourceMetadata': metadata(info) if info else None}


def validate_manifest(manifest, name):
    need(manifest.get('archive') == name and manifest.get('hostname') == HOST
         and manifest.get('installationInstance') == INSTANCE
         and manifest.get('settingsBaseRevision') == BASE_REVISION, 'MANIFEST_IDENTITY')
    if manifest.get('format') == LEGACY_FORMAT:
        need(manifest.get('applicationRevision') == LEGACY_REVISION
             and manifest.get('apiImage') == LEGACY_API_IMAGE
             and manifest.get('canonicalExporterBlob') == LEGACY_EXPORTER_BLOB, 'MANIFEST_IDENTITY')
    else:
        need(manifest.get('format') in (RELEASE_FORMAT, FORMAT), 'MANIFEST_IDENTITY')
        release = validate_application_release(manifest.get('applicationRelease'), 'MANIFEST_IDENTITY')
        need(release == manifest['applicationRelease']
             and manifest.get('applicationRevision') == release['revision']
             and manifest.get('apiImageReference') == release['images']['api']
             and isinstance(manifest.get('apiImage'), str) and IMAGE_ID.fullmatch(manifest['apiImage']),
             'MANIFEST_IDENTITY')
    if manifest.get('format') == FORMAT:
        validate_window(manifest.get('normalizedLogWindow'))
        validate_source_proofs(manifest.get('normalizedLogSources'))
    else:
        need('normalizedLogWindow' not in manifest and 'normalizedLogSources' not in manifest,
             'NORMALIZED_LOG_WINDOW')
    need(manifest.get('coverageComplete') is False
         and manifest.get('gatewayOAuthContinuity') is False
         and manifest.get('gatewayLiveDatabaseIncluded') is False, 'MANIFEST_SCOPE')
    rows = manifest.get('entries')
    need(isinstance(rows, list) and len(STATIC) - len(LEGACY_PATHS) + 5 <= len(rows) <= MAX_FILES + 5,
         'MANIFEST_ENTRY_BOUND')
    result = {}
    for row in rows:
        name = row.get('member')
        need(isinstance(name, str) and name not in result and '\\' not in name
             and not name.startswith('/') and '..' not in PurePosixPath(name).parts
             and str(PurePosixPath(name)) == name, 'MANIFEST_PATH')
        if row.get('kind') == 'config':
            need(name.startswith('files/') and allowed_config(Path('/' + name[6:])), 'MANIFEST_CONFIG_SCOPE')
            limit = MAX_CONFIG
        elif row.get('kind') == 'sqlite':
            need(name in ['sqlite/' + v for v in DB_NAMES], 'MANIFEST_SQLITE_SCOPE')
            limit = MAX_SQLITE
        else:
            need(row.get('kind') == 'normalized' and name in
                 ('normalized/host.zip','normalized/application.zip'), 'MANIFEST_LOG_SCOPE')
            limit = MAX_WINDOW_ZIP if manifest.get('format') == FORMAT else MAX_ZIP
        need(type(row.get('bytes')) is int and 0 <= row['bytes'] <= limit
             and re.fullmatch(r'[a-f0-9]{64}', str(row.get('sha256'))), 'MANIFEST_INTEGRITY_FIELDS')
        if row['kind'] != 'normalized':
            meta = row.get('sourceMetadata')
            need(isinstance(meta, dict) and all(type(meta.get(k)) is int and meta[k] >= 0
                 for k in ('uid','gid','mode','bytes','mtimeNs')) and meta['mode'] <= 0o7777,
                 'MANIFEST_PERMISSIONS')
        result[name] = row
    directories = manifest.get('directories')
    parents = {str(p) for row in rows if row['kind'] == 'config'
               for p in Path('/' + row['member'][6:]).parents if p != Path('/')}
    need(isinstance(directories, list) and len(directories) <= 200, 'MANIFEST_DIRECTORIES_BOUND')
    seen_dirs = set()
    for row in directories:
        directory, meta = row.get('path'), row.get('metadata')
        bounded = isinstance(directory, str) and bool(re.fullmatch(r'/[A-Za-z0-9_./-]+', directory)) \
            and '..' not in PurePosixPath(directory).parts \
            and any(Path(directory).is_relative_to(root) for root in CONFIG_ROOTS)
        need(directory not in seen_dirs and (directory in parents or bounded),
             'MANIFEST_DIRECTORY_SCOPE')
        seen_dirs.add(directory)
        need(meta is None and bounded or isinstance(meta, dict)
             and all(type(meta.get(k)) is int and meta[k] >= 0 for k in ('uid','gid','mode','bytes','mtimeNs'))
             and meta['mode'] <= 0o7777, 'MANIFEST_DIRECTORY_PERMISSIONS')
    need(parents.issubset(seen_dirs), 'MANIFEST_DIRECTORY_MISSING')
    available = {Path('/' + member[6:]) for member, row in result.items() if row['kind'] == 'config'}
    missing_legacy = legacy_omissions(available)
    required = {'files/' + str(p).lstrip('/') for p in STATIC if p not in missing_legacy}
    panel_members = {p: 'files/' + str(p).lstrip('/') for p in (SUPERVISOR_CONFIG, *PANEL_COLLECTOR, *PANEL_SCHEDULES)}
    for group in (PANEL_COLLECTOR, PANEL_SCHEDULES):
        if any(panel_members[p] in result for p in group):
            required |= {panel_members[p] for p in (*group, SUPERVISOR_CONFIG)}
    if panel_members[PANEL_SCHEDULES[0]] in result:
        need(any(name.startswith('files/') and SCHEDULE_SOURCE.fullmatch('/' + name[6:]) for name in result),
             'MANIFEST_PANEL_RELEASE_MISSING')
    required |= {'sqlite/' + n for n in DB_NAMES} | {'normalized/host.zip','normalized/application.zip'}
    need(required.issubset(result) and sum(r['bytes'] for r in rows) <= MAX_CIPHER - 4 * 1024**2,
         'MANIFEST_REQUIRED_SCOPE')
    return result


def verify_tar(tar_path, destination, name):
    info = destination.lstat()
    need(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()
         and stat.S_IMODE(info.st_mode) == 0o700 and not any(destination.iterdir()),
         'RESTORE_DIRECTORY_NOT_EMPTY_OR_PRIVATE')
    need(Path(tar_path).stat().st_size <= MAX_CIPHER and Path(tar_path).stat().st_size % 512 == 0,
         'TAR_SIZE_BOUND')
    total = 0
    names = set()
    expected_offset = 0
    with tarfile.open(tar_path, 'r:') as archive:
        first = archive.next()
        need(first is not None and first.name == 'manifest.json' and first.isreg()
             and first.size <= 2 * 1024**2, 'TAR_MANIFEST_FIRST')
        manifest = json.loads(archive.extractfile(first).read())
        expected = validate_manifest(manifest, name)
        current = first
        while current is not None:
            need(current.isreg() and current.uid == 0 and current.gid == 0 and current.mode == 0o600
                 and not current.pax_headers and current.offset == expected_offset
                 and current.offset_data == expected_offset + 512, 'TAR_MEMBER_METADATA')
            member = current.name
            need(member not in names and (member == 'manifest.json' or member in expected), 'TAR_EXTRA_OR_DUPLICATE')
            names.add(member)
            if member != 'manifest.json':
                need(current.size == expected[member]['bytes'], 'TAR_MEMBER_SIZE')
            target = destination / member
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            source = archive.extractfile(current)
            h = hashlib.sha256()
            with target.open('xb') as output:
                os.chmod(target, 0o600)
                for block in iter(lambda: source.read(1024 * 1024), b''):
                    total += len(block)
                    need(total <= MAX_CIPHER, 'TAR_TOTAL_BYTES_BOUND')
                    output.write(block)
                    h.update(block)
            if member != 'manifest.json':
                need(h.hexdigest() == expected[member]['sha256'], 'TAR_FILE_SHA256')
            expected_offset = current.offset_data + ((current.size + 511) // 512) * 512
            current = archive.next()
        need(names == set(expected) | {'manifest.json'}, 'TAR_MISSING_MEMBER')
    with Path(tar_path).open('rb') as stream:
        stream.seek(expected_offset)
        # tarfile writes two end blocks, then pads to the next record boundary.
        max_end_padding = tarfile.RECORDSIZE + tarfile.BLOCKSIZE
        tail = stream.read(max_end_padding + 1)
        need(2 * tarfile.BLOCKSIZE <= len(tail) <= max_end_padding and not any(tail), 'TAR_END_MARKER')
    configurations = {Path('/' + member[6:]) for member, row in expected.items() if row['kind'] == 'config'}
    native_ownership_for_omissions(configurations,
        lambda path: (destination / 'files' / str(path).lstrip('/')).read_bytes())
    for db in DB_NAMES:
        sqlite_integrity(destination / 'sqlite' / db)
    window = manifest.get('normalizedLogWindow')
    logs = verify_normalized_logs(destination, window)
    need(logs == manifest.get('normalizedLogs'), 'RESTORED_LOG_SUMMARY_CHANGED')
    return {'manifestVerified': True, 'fileCount': len(expected), 'sqliteIntegrity': dict.fromkeys(DB_NAMES, 'ok'),
            'normalizedLogs': logs, 'coverageComplete': False, 'permissionsManifestValidated': True,
            'livePathsWritten': False, 'gatewayOAuthContinuity': False,
            **({'normalizedLogWindow': window} if window is not None else {})}


def create(name):
    check_host()
    name = archive_name(name)
    runtime = attested_api_runtime()
    cid = runtime['container']
    access = log_preflight(cid)
    print(json.dumps({'stage': 'NORMALIZED_EXPORT_READABILITY', **access}, separators=(',', ':')), flush=True)
    need(access.get('ready') is True, 'NORMALIZED_CATALOGS_NOT_READABLE_WITH_RUNTIME_GROUPS')
    private_directory(TARGET, create=True)
    need(shutil.disk_usage(TARGET).free >= 4 * 1024**3, 'SNAPSHOT_DISK_HEADROOM')
    output = TARGET / name
    need(not output.exists() and not output.is_symlink(), 'ARCHIVE_ALREADY_EXISTS')
    recipient, _ = stable_read(RECIPIENTS, 4096)
    public = [v.strip() for v in recipient.splitlines() if v.strip() and not v.lstrip().startswith(b'#')]
    need(len(public) == 1 and re.fullmatch(rb'age1[a-z0-9]{55,80}', public[0])
         and hashlib.sha256(public[0]).hexdigest() == PUBLIC_RECIPIENT_SHA256, 'PUBLIC_RECIPIENT_CHANGED')
    files, snapshots, directories, dependencies = config_inputs()
    need(files[RECIPIENTS][0] == recipient, 'RECIPIENT_FILE_CHANGED_BEFORE_SNAPSHOT')
    with tempfile.TemporaryDirectory(prefix='school-config-private-', dir=TARGET) as temporary:
        stage = Path(temporary)
        os.chmod(stage, 0o700)
        for sub in ('sqlite','normalized'):
            (stage / sub).mkdir(mode=0o700)
        rows = []
        for source, (data, info) in sorted(files.items()):
            member = 'files/' + str(source).lstrip('/')
            target = stage / member
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            private_write(target, data)
            rows.append(entry(target, member, info, 'config'))
        for db in DB_NAMES:
            target = stage / 'sqlite' / db
            info = sqlite_snapshot(Path('/opt/1panel/db') / db, target)
            rows.append(entry(target, 'sqlite/' + db, info, 'sqlite'))
        logs, sources = export_logs(cid, name, stage, access)
        for kind in ('host','application'):
            member = 'normalized/' + kind + '.zip'
            rows.append(entry(stage / member, member, None, 'normalized'))
        unchanged(snapshots, directories)
        _, final_snapshots, _, final_dependencies = config_inputs()
        need(final_snapshots == snapshots and final_dependencies == dependencies,
             'CONFIG_DEPENDENCY_SET_CHANGED_DURING_SNAPSHOT')
        need(attested_api_runtime() == runtime, 'API_REPLACED_DURING_SNAPSHOT')
        release = runtime['release']
        manifest = {'format': FORMAT, 'archive': name, 'createdAt': stamp(), 'hostname': HOST,
            'installationInstance': INSTANCE, 'settingsBaseRevision': BASE_REVISION,
            'applicationRelease': release, 'applicationRevision': release['revision'],
            'apiImage': runtime['image'], 'apiImageReference': release['images']['api'],
            'entries': rows, 'directories': [{'path': p, 'metadata': metadata(Path(p).lstat()) if sig else None}
                for p, sig in sorted(directories.items())], 'nginxDependencies': dependencies,
            'normalizedLogs': logs, 'coverageComplete': False,
            'normalizedLogWindow': access['window'], 'normalizedLogSources': sources,
            'coverageStatement': 'Retained normalized evidence within the stated UTC window; late arrivals and prior gaps are possible.',
            'sqliteConsistency': 'Online SQLite backup API; each database individually consistent, not one cross-database transaction.',
            'gatewayOAuthContinuity': False, 'gatewayLiveDatabaseIncluded': False,
            'restoreRequirement': 'Reinstall pinned services and resolve service accounts; restore settings only in maintenance. Register ChatGPT again. No live restore is performed by this script.',
            'excluded': ['PostgreSQL','MinIO','raw logs','gateway bbolt db','operations history SQLite',
                         'collector private index','Docker images','WAF runtime data outside referenced configuration',
                         'SSH/Tailscale/network configuration','offline age identity']}
        validate_manifest(manifest, name)
        write_json(stage / 'manifest.json', manifest)
        plain = stage / 'snapshot.tar'
        with tarfile.open(plain, 'w', format=tarfile.USTAR_FORMAT) as archive:
            for member in ['manifest.json'] + [row['member'] for row in rows]:
                file = stage / member
                info = tarfile.TarInfo(member)
                info.uid = info.gid = 0
                info.mode = 0o600
                info.size = file.stat().st_size
                info.mtime = int(time.time())
                with file.open('rb') as stream:
                    archive.addfile(info, stream)
        os.chmod(plain, 0o600)
        need(plain.stat().st_size <= MAX_CIPHER - 1024 * 1024, 'PLAINTEXT_ARCHIVE_BOUND')
        with output.open('xb') as stream:
            os.chmod(output, 0o600)
            try:
                run(['/usr/bin/age', '-r', public[0].decode('ascii'), str(plain)], timeout=180, output=stream, limit=MAX_CIPHER)
                stream.flush()
                os.fsync(stream.fileno())
            except BaseException:
                output.unlink(missing_ok=True)
                raise
    size, checksum = verify_cipher(output)
    private_write(TARGET / (name + '.sha256'), (checksum + '  ' + str(output) + '\n').encode('ascii'))
    receipt = {'status': 'CONFIG_ARCHIVE_CREATED_NOT_CLOUD_VERIFIED', 'archive': name, 'bytes': size,
        'ciphertextSha256': checksum, 'configurationFiles': len(files), 'sqliteDatabases': 3,
        'normalizedLogs': logs, 'normalizedLogWindow': access['window'],
        'coverageComplete': False, 'gatewayOAuthContinuity': False,
        'schedulesChanged': False, 'servicesChanged': False, 'restoreValidated': False}
    write_json(TARGET / (name + '.created.json'), receipt)
    return receipt


def verify_cipher(path):
    info = trusted(path)
    need(info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o600
         and 128 <= info.st_size <= MAX_CIPHER, 'PRIVATE_CIPHERTEXT_REQUIRED')
    with path.open('rb') as stream:
        need(stream.read(30).startswith(b'age-encryption.org/v1'), 'AGE_HEADER_REQUIRED')
    return info.st_size, digest(path)


def identity_memfd():
    data = bytearray(sys.stdin.buffer.read(4097))
    descriptor = None
    lines = []
    try:
        need(32 <= len(data) <= 4096, 'IDENTITY_STDIN_SIZE')
        lines = [v.strip() for v in bytes(data).splitlines() if v.strip() and not v.lstrip().startswith(b'#')]
        need(len(lines) == 1 and re.fullmatch(rb'AGE-SECRET-KEY-1[A-Z0-9]{50,110}', lines[0]), 'IDENTITY_STDIN_FORMAT')
        descriptor = os.memfd_create('asa-config-one-shot-identity', os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
        os.fchmod(descriptor, 0o600)
        os.write(descriptor, lines[0] + b'\n')
        os.lseek(descriptor, 0, os.SEEK_SET)
        fcntl.fcntl(descriptor, fcntl.F_ADD_SEALS,
            fcntl.F_SEAL_SEAL | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_GROW | fcntl.F_SEAL_WRITE)
        public = run(['/usr/bin/age-keygen', '-y', '/proc/self/fd/' + str(descriptor)], pass_fds=(descriptor,)).strip()
        need(hashlib.sha256(public).hexdigest() == PUBLIC_RECIPIENT_SHA256, 'IDENTITY_RECIPIENT_MISMATCH')
        os.lseek(descriptor, 0, os.SEEK_SET)
        return descriptor
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        raise
    finally:
        data[:] = b'\0' * len(data)
        lines.clear()


def restore_verify(name, expected_sha):
    check_host()
    name = archive_name(name)
    need(re.fullmatch(r'[a-f0-9]{64}', expected_sha or ''), 'EXPECTED_CIPHER_SHA_REQUIRED')
    cipher = RECOVERY / 'incoming' / name
    size, actual = verify_cipher(cipher)
    need(actual == expected_sha, 'DOWNLOADED_CIPHER_PIN_CHANGED')
    cloud_raw, _ = stable_read(TARGET / (name + '.cloud.json'))
    cloud = json.loads(cloud_raw)
    need(cloud.get('archive') == name and cloud.get('bytes') == size
         and cloud.get('ciphertextSha256') == actual and cloud.get('fullDownloadVerified') is True
         and cloud.get('remoteDirectory') == 'app:/School-VM100/Configuration-Backups', 'CLOUD_RECEIPT_MISMATCH')
    private_directory(RECOVERY)
    verified = RECOVERY / 'verified'
    private_directory(verified, create=True)
    destination = verified / name.removesuffix('.tar.age')
    need(not destination.exists() and not destination.is_symlink(), 'RESTORE_DESTINATION_EXISTS')
    need(shutil.disk_usage(RECOVERY).free >= 2 * MAX_CIPHER, 'RESTORE_DISK_HEADROOM')
    descriptor = identity_memfd()
    try:
        with tempfile.TemporaryDirectory(prefix='config-decrypt-private-', dir=RECOVERY) as temporary:
            stage = Path(temporary)
            plain = stage / 'snapshot.tar'
            extracted = stage / 'extracted'
            extracted.mkdir(mode=0o700)
            with plain.open('xb') as stream:
                os.chmod(plain, 0o600)
                run(['/usr/bin/age', '-d', '-i', '/proc/self/fd/' + str(descriptor), str(cipher)],
                    timeout=180, output=stream, pass_fds=(descriptor,), limit=MAX_CIPHER)
            result = verify_tar(plain, extracted, name)
            os.rename(extracted, destination)
    finally:
        os.close(descriptor)
    result.update(status='CONFIG_CLOUD_DOWNLOAD_DECRYPT_ISOLATED_VERIFY_PASS', archive=name,
                  ciphertextSha256=actual, bytes=size, verifiedDirectory=str(destination))
    write_json(TARGET / (name + '.restore.json'), result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--verify-stdin-identity', action='store_true')
    parser.add_argument('--expected-cipher-sha256')
    args = parser.parse_args()
    archive_name(args.archive)
    if not args.apply:
        print(json.dumps({'status': 'PLAN_ONLY', 'archive': args.archive,
            'action': 'isolated_decrypt_and_verify' if args.verify_stdin_identity else 'managed_configuration_snapshot',
            'configurationRequiredFiles': len(STATIC) - len(LEGACY_PATHS),
            'conditionalLegacyFiles': len(LEGACY_PATHS), 'legacyOmissionRequires': 'complete fresh native ownership',
            'sqliteDatabases': list(DB_NAMES),
            'normalizedLogExports': ['host','application'], 'coverageComplete': False,
            'gatewayOAuthContinuity': False, 'changes': False}))
        return 0
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    check_host()
    lock_path = Path('/run/lock/asa-school-host-config-backup.lock')
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(descriptor)
        need(info.st_uid == 0 and stat.S_ISREG(info.st_mode) and info.st_nlink == 1
             and stat.S_IMODE(info.st_mode) == 0o600, 'CONFIG_LOCK_UNTRUSTED')
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = restore_verify(args.archive, args.expected_cipher_sha256) if args.verify_stdin_identity else create(args.archive)
        print(json.dumps(result, sort_keys=True, separators=(',', ':')))
    finally:
        os.close(descriptor)
    return 0


if __name__ == '__main__':
    sys.dont_write_bytecode = True
    try:
        raise SystemExit(main())
    except Exception as exc:
        code = str(exc) if isinstance(exc, SnapshotError) else type(exc).__name__
        print(json.dumps({'status': 'CONFIG_BACKUP_STOP', 'code': code, 'secretValuesPrinted': False}), file=sys.stderr)
        raise SystemExit(2)

