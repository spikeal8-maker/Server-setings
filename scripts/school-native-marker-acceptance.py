"""One source-bound pre-activation proof; native reads and owned temporary export only.

The frozen binding must come from accepted native probe receipts. This script
does not run a probe, enable a schedule, create a principal, import Collector,
or modify the private SQLite index. The accepted snapshot helper exports both
stores unchanged; marker delivery is asserted only for the HOST archive.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path('/srv/asa-lab')
APP_REVISION = '3e8713476cafedda3e24a39665458e833efd7c22'
HOST = 'asa-school-01'
INSTANCE = 'fc0ba60fe1d946bba1694d73557cc11f'
PROJECT = 'asa-lab-school'
SOURCE = 'linux:1panel-supervisor'
KINDS = ('database', 'settings')
INDEX = ROOT / '.asa/diagnostics/private/index.sqlite'
STATE = Path('/var/lib/server-settings/1panel-backup-schedules')
EVIDENCE = Path('/var/lib/server-settings/xlog-native-marker-evidence')
SNAPSHOT = Path('/opt/asa-school-settings-backup/releases/fdd7ebb8daaebcfc/school_settings_snapshot.py')
SNAPSHOT_SHA = '9a7719c5705fb6883857b0c25be1e19f9ee7f3009c8594a43ca15d04d0bffbe2'
CRON = Path('/opt/asa-school-panel-schedules/releases/4b391bbd4b458921/school-1panel-backup-schedules.py')
CRON_SHA = '4b391bbd4b4589212acffc25a4239b79d15e5830176300610dfd1f7fc3ef596b'
SOURCE_PINS = {
    'docs/execution/current.yaml': '16a75017b3a6ddee89164b29e5832ecc00fc36cf26855b2ea5aea8dbc7cd7f91',
    'tools/collect_logs.py': 'cfd366f7bb0992db86fa2bb2683e66a9b828b9e29f6874c2ba90a9a9af32882d',
    'tools/test_database_progress.py': 'ed1d8a8ecdfd935f75839b1adb497efc57e73d757a7f71ecd5962f130f3872ed',
    'tools/deployment/collector.py': 'fb29c483863590e512ea32605fd558f1f1a9f8338eb26dac504cdd4518c845ed',
    'tools/deployment/test_panel_collector.py': '267f891986d854f78cab521804a83ec93491a82b7daddbd3271d3c1be4e87bc5',
    'docs/deployment/ONEPANEL_COLLECTOR.md': '14d6c3da599d5253556f000033f7a4168333014c47612ee9ca7e9e7b615298d7',
}
HEX32 = re.compile(r'[a-f0-9]{32}\Z')
HEX40 = re.compile(r'[a-f0-9]{40}\Z')
RUN = re.compile(r'xlog-native-marker-([0-9]{8})t([0-9]{6})z-([a-f0-9]{16})\Z')


class MarkerStop(RuntimeError):
    pass


def need(value, code):
    if not value:
        raise MarkerStop(code)


def now():
    return datetime.now(timezone.utc).isoformat()


def instant(value):
    need(isinstance(value, str) and len(value) <= 40, 'TIME_SHAPE')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    need(parsed.tzinfo is not None and 2026 <= parsed.year <= 2100, 'TIME_VALUE')
    return parsed.astimezone(timezone.utc)


def trusted(path, directory=False):
    path = Path(path)
    need(path.is_absolute() and '..' not in path.parts, 'ABSOLUTE_PATH_REQUIRED')
    for item in (path, *path.parents):
        info = item.lstat()
        need(info.st_uid == 0 and not stat.S_ISLNK(info.st_mode) and not info.st_mode & 0o022,
             'UNTRUSTED_INPUT_PATH')
        need(stat.S_ISDIR(info.st_mode) if item != path or directory else
             stat.S_ISREG(info.st_mode) and info.st_nlink == 1, 'UNTRUSTED_INPUT_TYPE')
    return path.lstat()


def read(path, limit=2 * 1024 * 1024):
    info = trusted(path)
    need(info.st_size <= limit, 'INPUT_SIZE_BOUND')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, 'rb') as stream:
        raw = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    sig = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
    need(len(raw) == info.st_size and sig(info) == sig(after) == sig(Path(path).lstat()), 'INPUT_CHANGED')
    return raw


def module(path, expected, name):
    need(hashlib.sha256(read(path)).hexdigest() == expected, 'HELPER_SOURCE_CHANGED')
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def git(*arguments):
    environment = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C.UTF-8',
                   'GIT_TERMINAL_PROMPT': '0', 'GIT_OPTIONAL_LOCKS': '0'}
    result = subprocess.run(['/usr/bin/git', '-C', str(ROOT), *arguments], stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, env=environment)
    need(result.returncode == 0 and max(len(result.stdout), len(result.stderr)) <= 2 * 1024 * 1024,
         'GIT_SOURCE_READ_FAILED')
    return result.stdout


def source_scope(revision):
    need(HEX40.fullmatch(revision) and revision != APP_REVISION, 'HOST_SOURCE_REVISION')
    need(not any(key in os.environ for key in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR')),
         'GIT_OVERRIDE')
    trusted(ROOT, directory=True)
    need(git('rev-parse', 'HEAD').decode().strip() == revision and
         not git('status', '--porcelain=v1', '--untracked-files=all', '-z'), 'CLEAN_CANONICAL_SOURCE_REQUIRED')
    need(git('remote', 'get-url', 'origin').decode().strip().removesuffix('.git') ==
         'https://github.com/spikeal8-maker/asa-lab', 'CANONICAL_ORIGIN')
    git('merge-base', '--is-ancestor', APP_REVISION, revision)
    changed = set(filter(None, git('diff', '--name-only', '--no-renames', '-z', APP_REVISION, revision,
                                  '--').decode().split('\0')))
    need(changed == set(SOURCE_PINS), 'HOST_SOURCE_ONLY_DIFF')
    for relative, expected in SOURCE_PINS.items():
        need(hashlib.sha256(read(ROOT / relative)).hexdigest() == expected, 'HOST_SOURCE_BYTES')
    need(not any((ROOT / '.asa' / name).exists() or (ROOT / '.asa' / name).is_symlink()
                 for name in ('maintenance.json', 'failed-switch.json', 'release-selection.json')),
         'DEPLOYMENT_BUSY')
    installation = json.loads(read(ROOT / '.asa/installation.json'))
    need(installation.get('collectorManager') == '1panel-supervisor'
         and installation.get('collectorMode') == 'system-root'
         and installation.get('automaticUpdates') is False, 'COLLECTOR_MANAGER_CHANGED')
    config = json.loads(read(ROOT / '.asa/log-collector.json'))
    selected = [value for value in config.get('fileSources', []) if value.get('source') == SOURCE]
    need(selected == [{'source': SOURCE, 'path': '/opt/1panel/tools/supervisord/log'}], 'NATIVE_FILE_SOURCE_CHANGED')


def validate_binding(value):
    need(isinstance(value, dict) and set(value) == {'runId', 'hostSourceRevision', 'generation', 'jobs'},
         'ACCEPTED_BINDING_SCHEMA')
    need(RUN.fullmatch(value['runId']) and HEX40.fullmatch(value['hostSourceRevision'])
         and HEX32.fullmatch(value['generation']) and set(value['jobs']) == set(KINDS), 'ACCEPTED_BINDING_IDENTITY')
    for kind in KINDS:
        row = value['jobs'][kind]
        need(set(row) == {'id', 'recordId', 'dispatchId', 'acceptedAt'}
             and all(type(row[key]) is int and row[key] > 0 for key in ('id', 'recordId'))
             and HEX32.fullmatch(row['dispatchId']), 'ACCEPTED_PROBE_BINDING')
        instant(row['acceptedAt'])
    need(value['jobs']['database']['dispatchId'] != value['jobs']['settings']['dispatchId']
         and value['jobs']['database']['id'] != value['jobs']['settings']['id'], 'DISTINCT_PROBES_REQUIRED')


def native_markers(cron, panel, binding):
    state = cron.load_state()
    need(state.get('phase') == 'STAGED' and state.get('owner') == state.get('originalOwner') == 'legacy'
         and state.get('clockTimeZone') == 'Europe/Moscow' and state.get('backupExecutedDuringInstall') is False
         and state.get('generation') == binding['generation'] and state.get('programSha256') == CRON_SHA,
         'STAGED_CLOSED_GATE_REQUIRED')
    cron.Controller(panel, cron.Legacy()).objects(state, status='Disable', idle=True)
    found = {}
    for kind in KINDS:
        expected = binding['jobs'][kind]
        job = state['jobs'][kind]
        accepted = job.get('probe', {})
        need(job.get('id') == expected['id'] and accepted.get('status') == 'NATIVE_GATED_PROBE_PASS'
             and accepted.get('programSha256') == CRON_SHA and accepted.get('backupExecuted') is False
             and accepted.get('nativeProcessLogObserved') is True
             and all(accepted.get(key) == expected[key] for key in ('dispatchId', 'recordId', 'acceptedAt')),
             'ACCEPTED_PROBE_CHANGED')
        records = [row for row in panel.records(job['id'], state['createdAt']) if row['id'] == expected['recordId']]
        need(len(records) == 1 and records[0].get('cronjobID') == job['id']
             and records[0].get('status') == 'Success', 'NATIVE_SUCCESS_RECORD_REQUIRED')
        run = cron.runtime_state(kind)
        need(isinstance(run, dict) and run.get('dispatchId') == expected['dispatchId']
             and run.get('generation') == binding['generation'] and run.get('kind') == kind
             and run.get('programSha256') == CRON_SHA and run.get('status') == 'STAGED_GATE_VERIFIED_NO_BACKUP'
             and run.get('backupExecuted') is False and run.get('level') == 'INFO'
             and run.get('restoreValidated') is False and run.get('supervision') == '1Panel Supervisor'
             and not {'runnerPid', 'backupAttempted', 'privateRunnerOutput'} & run.keys(), 'PROBE_RUNTIME_CHANGED')
        need(instant(run['startedAt']) <= instant(run['finishedAt']) <= instant(expected['acceptedAt'])
             <= datetime.now(timezone.utc), 'PROBE_TIME_BINDING')
        instant(run['timestamp'])
        raw = panel.request('/hosts/tool/supervisor/process/file/get', {'name': cron.program(kind), 'file': 'out.log'})
        need(isinstance(raw, str) and len(raw.encode()) <= 2_000_000, 'NATIVE_LOG_BOUND')
        lines = [line for line in raw.splitlines() if expected['dispatchId'] in line]
        need(len(lines) == 1 and json.loads(lines[0]) == run, 'NATIVE_JSON_MARKER_NOT_UNIQUE_OR_CHANGED')
        found[kind] = run
    return found


def host_markers(path, native, cron):
    matches = {kind: [] for kind in KINDS}
    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read('manifest.json'))
        for segment in manifest['catalog']['segments']:
            with archive.open(segment['file']) as stream:
                while True:
                    raw = stream.readline(131073)
                    if not raw:
                        break
                    need(len(raw) <= 131072 and raw.endswith(b'\n'), 'NORMALIZED_RECORD_BOUND')
                    row = json.loads(raw)
                    if row.get('source') != SOURCE:
                        continue
                    for kind in KINDS:
                        if native[kind]['dispatchId'] not in row['message']:
                            continue
                        expected_time = instant(native[kind]['timestamp']).isoformat(timespec='milliseconds').replace('+00:00', 'Z')
                        need(row.get('module') == 'system' and row.get('level') == 'info'
                             and row.get('origin') == cron.program(kind) + '.out.log'
                             and row.get('time') == expected_time and json.loads(row['message']) == native[kind]
                             and re.fullmatch(r'[a-f0-9]{64}', str(row.get('id'))), 'NORMALIZED_MARKER_CHANGED')
                        matches[kind].append(row['id'])
                        need(len(matches[kind]) <= 1, 'NORMALIZED_MARKER_DUPLICATED')
    need(all(len(matches[kind]) == 1 for kind in KINDS), 'NORMALIZED_MARKER_MISSING')
    return {kind: matches[kind][0] for kind in KINDS}


def published(ids):
    before = trusted(INDEX)
    for suffix in ('-wal', '-shm'):
        trusted(Path(str(INDEX) + suffix))
    with closing(sqlite3.connect(INDEX.as_uri() + '?mode=ro', uri=True, timeout=1, isolation_level=None)) as database:
        database.execute('PRAGMA query_only=ON')
        database.execute('BEGIN')
        try:
            for kind in KINDS:
                rows = database.execute('SELECT published FROM events WHERE id=?', (bytes.fromhex(ids[kind]),)).fetchall()
                need(rows == [(1,)], 'NORMALIZED_EVENT_NOT_PUBLISHED')
        finally:
            database.execute('ROLLBACK')
    current = trusted(INDEX)
    need((before.st_dev, before.st_ino) == (current.st_dev, current.st_ino), 'PRIVATE_INDEX_REPLACED')


def main(binding):
    need(os.geteuid() == 0 and socket.gethostname() == HOST and sys.flags.isolated and sys.dont_write_bytecode,
         'VM100_ISOLATED_ROOT_REQUIRED')
    validate_binding(binding)
    source_scope(binding['hostSourceRevision'])
    snapshot = module(SNAPSHOT, SNAPSHOT_SHA, 'accepted_native_marker_snapshot')
    cron = module(CRON, CRON_SHA, 'accepted_native_marker_cron')
    snapshot.check_host()
    cid = snapshot.api_runtime()
    access = snapshot.log_preflight(cid)
    need(access.get('ready') is True, 'CANONICAL_EXPORT_ACCESS_REQUIRED')
    protected = [STATE / 'state.json', *(STATE / 'runs' / (kind + '.json') for kind in KINDS),
                 ROOT / '.asa/installation.json', ROOT / '.asa/log-collector.json']
    before = {str(path): hashlib.sha256(read(path)).hexdigest() for path in protected}
    panel = cron.Panel()
    native = native_markers(cron, panel, binding)
    snapshot.private_directory(EVIDENCE, create=True)
    receipt_dir = EVIDENCE / binding['runId']
    need(not receipt_dir.exists() and not receipt_dir.is_symlink(), 'OWN_EVIDENCE_ALREADY_EXISTS')
    receipt_dir.mkdir(mode=0o700)
    result = {'format': 'asa-school-native-marker-acceptance-v1', 'status': 'NATIVE_MARKER_STOP',
              'createdAt': now(), 'runId': binding['runId'], 'hostname': HOST,
              'instance': INSTANCE, 'project': PROJECT, 'markerSource': SOURCE,
              'generation': binding['generation'], 'acceptedProbeBindings': binding['jobs'],
              'hostSourceRevision': binding['hostSourceRevision'], 'applicationRevision': APP_REVISION,
              'snapshotHelperSha256': SNAPSHOT_SHA, 'cronProgramSha256': CRON_SHA,
              'receiptPath': str(receipt_dir / 'receipt.json'),
              'scope': 'native-marker-normalized-host-export', 'authenticatedCoverageReevaluated': False,
              'globalCoverageComplete': False, 'temporaryExports': ['host', 'application'],
              'applicationOrScheduleMutation': False, 'temporaryCleanupVerified': False}
    try:
        need(shutil.disk_usage(EVIDENCE).free >= 4 * 1024**3, 'EXPORT_DISK_HEADROOM')
        match = RUN.fullmatch(binding['runId'])
        export_name = 'asa-school-config-01-' + match[1] + 'T' + match[2] + 'Z-' + match[3][:8] + '.tar.age'
        with tempfile.TemporaryDirectory(prefix='normalized-', dir=receipt_dir) as temporary:
            stage = Path(temporary)
            os.chmod(stage, 0o700)
            (stage / 'normalized').mkdir(mode=0o700)
            logs = snapshot.export_logs(cid, export_name, stage)
            ids = host_markers(stage / 'normalized/host.zip', native, cron)
            published(ids)
            host_zip = stage / 'normalized/host.zip'
            with host_zip.open('rb') as stream:
                zip_sha = hashlib.file_digest(stream, 'sha256').hexdigest()
            result['hostZip'] = {**logs['host'], 'sha256': zip_sha, 'bytes': host_zip.stat().st_size,
                                 'allTimeAllSources': True, 'exporterBlob': snapshot.EXPORTER_BLOB}
            result['applicationZipVerifiedByUnmodifiedHelper'] = logs['application']['crcAndPartSha256Verified']
        result['temporaryCleanupVerified'] = True
        need(snapshot.api_runtime() == cid, 'API_RUNTIME_REPLACED')
        source_scope(binding['hostSourceRevision'])
        need(all(hashlib.sha256(read(path)).hexdigest() == before[str(path)] for path in protected)
             and hashlib.sha256(read(SNAPSHOT)).hexdigest() == SNAPSHOT_SHA
             and hashlib.sha256(read(CRON)).hexdigest() == CRON_SHA, 'INPUT_CHANGED_DURING_PROOF')
        cron.Controller(panel, cron.Legacy()).objects(cron.load_state(), status='Disable', idle=True)
        result['jobs'] = {kind: {'acceptedProbeBound': True, 'nativeSuccessRecord': True,
                                'nativeMarkerCount': 1, 'hostZipMarkerCount': 1, 'publishedIndexCount': 1,
                                'sameMarkerObject': True, 'backupExecuted': False} for kind in KINDS}
        result['status'] = 'NATIVE_MARKERS_NORMALIZED_HOST_EXPORT_PASS'
        result['preservedClosedGateAndInputs'] = True
    except Exception as error:
        value = str(error)
        result['code'] = value if re.fullmatch(r'[A-Z0-9_]{1,100}', value) else type(error).__name__
    result['observedAt'] = now()
    encoded = (json.dumps(result, sort_keys=True, separators=(',', ':')) + '\n').encode()
    need(len(encoded) <= 8192, 'PUBLIC_RECEIPT_BOUND')
    snapshot.private_write(receipt_dir / 'receipt.json', encoded)
    print(encoded.decode(), end='', flush=True)
    return 0 if result['status'] == 'NATIVE_MARKERS_NORMALIZED_HOST_EXPORT_PASS' else 2


if __name__ == '__main__':
    try:
        need(len(sys.argv) == 2 and len(sys.argv[1]) <= 4096, 'ONE_FROZEN_BINDING_REQUIRED')
        raise SystemExit(main(json.loads(sys.argv[1])))
    except Exception as error:
        value = str(error)
        print(json.dumps({'status': 'NATIVE_MARKER_STOP',
                          'code': value if re.fullmatch(r'[A-Z0-9_]{1,100}', value) else type(error).__name__,
                          'observedAt': now(), 'globalCoverageComplete': False,
                          'authenticatedCoverageReevaluated': False}), flush=True)
        raise SystemExit(2)
