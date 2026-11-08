#!/usr/bin/python3
"""ASA host collector lifecycle through the official local 1Panel v2.3.2 API.

This bridge never creates systemd services. The native Supervisor process is
staged with autostart=false. Adoption preserves the original runner identity,
private index, event history and cursor files. A failed API request never
starts a fallback writer. Configuration is installed by the companion script.
"""
from __future__ import annotations

import argparse
import configparser
from contextlib import redirect_stdout
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

CONFIG = Path('/var/lib/server-settings/asa-1panel-collector.json')
BRIDGE = Path('/usr/local/sbin/asa-1panel-collector')
MANAGER = '1panel-supervisor'
PANEL_VERSION = 'v2.3.2'
PROCESS_ROUTE = '/hosts/tool/supervisor/process'
RESULT_FORMAT = 'asa-1panel-collector-result-v1'


class PanelCollectorError(RuntimeError):
    pass


def need(condition, code):
    if not condition:
        raise PanelCollectorError(code)


def controlled(path, limit=256 * 1024, secret=False):
    path = Path(path)
    need(path.is_absolute() and '..' not in path.parts, 'CONTROLLED_PATH_REQUIRED')
    for item in (path, *path.parents):
        info = item.lstat()
        need(info.st_uid == 0 and not stat.S_ISLNK(info.st_mode) and not info.st_mode & 0o022,
             'ROOT_CONTROLLED_PATH_REQUIRED')
    info = path.lstat()
    need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= limit,
         'BOUNDED_REGULAR_FILE_REQUIRED')
    if secret:
        need(stat.S_IMODE(info.st_mode) == 0o600, 'PRIVATE_FILE_MODE_REQUIRED')
    with path.open('rb') as stream:
        raw = stream.read(limit + 1)
        final = os.fstat(stream.fileno())
    need(len(raw) <= limit and (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
         == (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns, final.st_ctime_ns), 'FILE_CHANGED')
    return raw


def atomic(path, data):
    """Replace an already owned metadata file; never a log/cursor/store file."""
    path = Path(path)
    controlled(path)
    previous = path.lstat()
    temporary = path.with_name(path.name + '.panel-new')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchown(stream.fileno(), previous.st_uid, previous.st_gid)
            os.fchmod(stream.fileno(), stat.S_IMODE(previous.st_mode))
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_DIRECTORY | os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode()


def run(arguments, timeout=20):
    result = subprocess.run(arguments, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                            timeout=timeout, check=False,
                            env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C.UTF-8',
                                 'PYTHONDONTWRITEBYTECODE': '1'})
    need(result.returncode == 0 and len(result.stdout) <= 65536, 'LOCAL_COMMAND_FAILED')
    return result.stdout


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


class PanelAPI:
    def __init__(self, host, key):
        parsed = urllib.parse.urlsplit(host)
        need(parsed.scheme == 'http' and parsed.hostname == '127.0.0.1' and parsed.port
             and not parsed.username and not parsed.password and parsed.path in ('', '/')
             and not parsed.query and not parsed.fragment, 'LOCAL_PANEL_REQUIRED')
        need(re.fullmatch(r'[a-fA-F0-9]{32}', key) is not None, 'PANEL_KEY_FORMAT')
        self.host = host.rstrip('/')
        self.key = key
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(self, method, path, data=None):
        allowed = {('POST', '/hosts/tool/status'), ('POST', '/hosts/tool/init'),
                   ('GET', PROCESS_ROUTE), ('POST', PROCESS_ROUTE),
                   ('POST', '/hosts/tool/supervisor/process/file/get')}
        need((method, path) in allowed, 'UNSUPPORTED_PANEL_OPERATION')
        stamp = str(int(time.time()))
        token = hashlib.md5(('1panel' + self.key + stamp).encode()).hexdigest()
        request = urllib.request.Request(self.host + '/api/v2' + path, method=method,
                    data=None if data is None else json.dumps(data).encode(),
                    headers={'Content-Type': 'application/json', '1Panel-Timestamp': stamp,
                             '1Panel-Token': token})
        try:
            with self.opener.open(request, timeout=15) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
            need(len(raw) <= 2 * 1024 * 1024, 'PANEL_RESPONSE_TOO_LARGE')
            value = json.loads(raw)
            need(isinstance(value, dict) and value.get('code') == 200, 'PANEL_REQUEST_REJECTED')
            return value.get('data')
        except (urllib.error.URLError, ValueError, OSError) as exc:
            raise PanelCollectorError('PANEL_REQUEST_FAILED') from exc


def load_configuration(root=None, project=None):
    need(os.geteuid() == 0, 'ROOT_REQUIRED')
    value = json.loads(controlled(CONFIG, secret=True))
    keys = {'format', 'root', 'project', 'instanceId', 'panelHost', 'panelKeyFile',
            'panelVersion', 'python', 'program'}
    need(isinstance(value, dict) and set(value) == keys and value['format'] == 'asa-1panel-collector-v1',
         'PANEL_CONFIGURATION_SCHEMA')
    need(value['panelVersion'] == PANEL_VERSION, 'PANEL_VERSION_UNSUPPORTED')
    need(re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', value['project']) is not None
         and value['program'] == 'asa-log-collector-' + value['project'], 'PANEL_PROGRAM_IDENTITY')
    need(re.fullmatch(r'[a-f0-9]{32}', value['instanceId']) is not None, 'INSTALLATION_INSTANCE_REQUIRED')
    need(root in (None, value['root']) and project in (None, value['project']), 'REQUEST_IDENTITY_MISMATCH')
    installation = Path(value['root']) / '.asa/installation.json'
    settings = json.loads(controlled(installation))
    need(settings.get('project') == value['project'] and settings.get('instanceId') == value['instanceId']
         and settings.get('collectorMode') == 'system-root', 'INSTALLATION_IDENTITY_MISMATCH')
    return value, settings


def api_client(config):
    version_program = shutil.which('1panel', path='/usr/local/bin:/usr/bin:/bin')
    need(version_program is not None, 'PANEL_VERSION_PROGRAM_MISSING')
    binary = Path(version_program).resolve(strict=True)
    for item in (binary, *binary.parents):
        info = item.lstat()
        need(info.st_uid == 0 and not stat.S_ISLNK(info.st_mode) and not info.st_mode & 0o022,
             'PANEL_VERSION_PROGRAM_UNTRUSTED')
    observed = set(re.findall(r'v[0-9]+\.[0-9]+\.[0-9]+(?:[-+][a-zA-Z0-9.-]+)?', run([str(binary), 'version'])))
    need(observed == {config['panelVersion']}, 'PANEL_VERSION_CHANGED')
    key = controlled(config['panelKeyFile'], secret=True).decode().strip()
    return PanelAPI(config['panelHost'], key)


def expected_process(config, autostart='false'):
    command = shlex.join([str(BRIDGE), 'run', '--root', config['root'], '--project', config['project']])
    return {'name': config['program'], 'command': command, 'dir': config['root'], 'user': 'root',
            'numprocs': '1', 'autoRestart': 'true', 'autoStart': autostart, 'environment': ''}


def supervisor_ready(api):
    value = api.request('POST', '/hosts/tool/status', {'type': 'supervisord'})
    need(isinstance(value, dict) and value.get('type') == 'supervisord', 'SUPERVISOR_STATUS_SCHEMA')
    status = value.get('config')
    need(isinstance(status, dict) and status.get('isExist') is True and status.get('ctlExist') is True
         and status.get('status') == 'running' and status.get('init') is False,
         'SUPERVISOR_PREREQUISITE_NOT_READY')
    return {key: status.get(key) for key in ('isExist', 'ctlExist', 'status', 'init', 'version')}


def program_directory_snapshot(api):
    """Resolve the existing native include directory, without assuming DataDir."""
    response = api.request('POST', '/hosts/tool/status', {'type': 'supervisord'})
    status = response.get('config') if isinstance(response, dict) else None
    need(isinstance(status, dict) and status.get('init') is False
         and isinstance(status.get('configPath'), str), 'SUPERVISOR_INCLUDE_UNPROVEN')
    text = controlled(status['configPath'], limit=128 * 1024).decode()
    parsed = configparser.ConfigParser(interpolation=None)
    parsed.read_string(text)
    include = parsed.get('include', 'files', fallback='').strip()
    path = Path(include)
    need(path.is_absolute() and path.name == '*.ini' and not any(c in str(path.parent) for c in '*?[ \t\r\n'),
         'SUPERVISOR_INCLUDE_UNPROVEN')
    directory = path.parent
    for item in (directory, *directory.parents):
        info = item.lstat()
        need(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022,
             'SUPERVISOR_INCLUDE_UNTRUSTED')
    info = directory.lstat()
    entries = tuple(sorted(item.name for item in directory.iterdir()))
    need(len(entries) <= 200, 'SUPERVISOR_INCLUDE_BOUND')
    return str(directory), info.st_dev, info.st_ino, info.st_mtime_ns, entries


def process(api, config, missing=False):
    before = program_directory_snapshot(api) if missing else None
    values = api.request('GET', PROCESS_ROUTE)
    if values is None and missing:
        after = program_directory_snapshot(api)
        need(before == after and before[-1] == (), 'SUPERVISOR_NIL_WITHOUT_EMPTY_DIRECTORY')
        values = []
    need(isinstance(values, list) and len(values) <= 200 and all(isinstance(item, dict) for item in values),
         'SUPERVISOR_PROCESS_SCHEMA')
    rows = [item for item in values if item.get('name') == config['program']]
    others = [item for item in values if item.get('name') != config['program']
              and item.get('dir') == config['root'] and 'collect_logs.py' in item.get('command', '')]
    need(not others, 'PARALLEL_COLLECTOR_REGISTRATION')
    if missing and not rows:
        return None
    need(len(rows) == 1, 'SUPERVISOR_PROCESS_NOT_UNIQUE')
    row = rows[0]
    expected = expected_process(config)
    need(all(row.get(key, '') == value for key, value in expected.items() if key != 'autoStart')
         and row.get('autoStart') in ('true', 'false'), 'SUPERVISOR_PROCESS_DIFFERENT')
    return row


def native_log_directory(api, config, staged=True):
    directory = Path(program_directory_snapshot(api)[0])
    need(directory.name == 'supervisor.d', 'NATIVE_SUPERVISOR_DIRECTORY_UNSUPPORTED')
    logs = directory.parent / 'log'
    for item in (logs, *logs.parents):
        info = item.lstat()
        need(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022,
             'NATIVE_LOG_DIRECTORY_UNTRUSTED')
    if staged:
        raw = api.request('POST', '/hosts/tool/supervisor/process/file/get', {'name': config['program'], 'file': 'config'})
        need(isinstance(raw, str) and len(raw) <= 65536, 'NATIVE_PROCESS_CONFIG_SCHEMA')
        parsed = configparser.ConfigParser(interpolation=None)
        parsed.read_string(raw)
        section = 'program:' + config['program']
        need(parsed.get(section, 'stdout_logfile', fallback='') == str(logs / (config['program'] + '.out.log'))
             and parsed.get(section, 'stderr_logfile', fallback='') == str(logs / (config['program'] + '.err.log')),
             'NATIVE_PROCESS_LOG_PATH_CHANGED')
    return logs


def with_native_logs(configuration, directory):
    need(isinstance(configuration, dict) and isinstance(configuration.get('fileSources', []), list), 'LOG_CONFIGURATION_SCHEMA')
    sources = list(configuration.get('fileSources', []))
    desired = {'source': 'linux:1panel-supervisor', 'path': str(directory)}
    existing = [item for item in sources if isinstance(item, dict) and item.get('source') == desired['source']]
    need(not existing or existing == [desired], 'PANEL_LOG_SOURCE_COLLISION')
    if not existing:
        sources.append(desired)
    return {**configuration, 'fileSources': sources}


def safe_process(row):
    states = row.get('status') or []
    need(isinstance(states, list) and len(states) <= 1, 'SUPERVISOR_PROCESS_COUNT')
    return {'name': row['name'], 'autoStart': row['autoStart'], 'autoRestart': row['autoRestart'],
            'status': [{key: item.get(key) for key in ('status', 'PID', 'uptime')} for item in states]}


def is_running(row):
    states = row.get('status') or []
    return (len(states) == 1 and states[0].get('status') == 'RUNNING'
            and str(states[0].get('PID', '')).isdigit() and int(states[0]['PID']) > 0)


def is_stopped(row):
    states = row.get('status') or []
    return len(states) == 1 and states[0].get('status') == 'STOPPED' and states[0].get('PID', '') in ('', '0')


def process_state(row):
    states = row.get('status')
    # v2.3.2 getProcessStatus ignores short supervisorctl rows. Supervisor
    # 4.2.5 STARTING/STOPPING descriptions can therefore appear as null/[];
    # neither is evidence of a stopped or running process.
    if states is None or states == []:
        return 'OBSERVATION_PENDING'
    need(isinstance(states, list) and len(states) == 1 and isinstance(states[0], dict),
         'SUPERVISOR_PROCESS_COUNT')
    state = states[0].get('status')
    need(state in ('STOPPED', 'STARTING', 'RUNNING', 'BACKOFF', 'STOPPING', 'EXITED', 'FATAL', 'UNKNOWN'),
         'SUPERVISOR_PROCESS_STATE_SCHEMA')
    return state


def registration(config, selected=False):
    path = Path(config['root']) / '.asa/collector-runner.json'
    value = json.loads(controlled(path))
    need(value.get('root') == config['root'] and value.get('project') == config['project']
         and value.get('mode') == 'system-root' and value.get('owner') == 'root'
         and value.get('name') in ('asa-log-collector-' + config['project'] + '.service', 'asa-log-collector.service'),
         'COLLECTOR_REGISTRATION_MISMATCH')
    if selected:
        need(value.get('manager') == MANAGER, 'PANEL_MANAGER_NOT_SELECTED')
    return value


def legacy_state(config):
    identity = registration(config)
    fields = ('LoadState', 'ActiveState', 'UnitFileState', 'MainPID')
    raw = run(['/usr/bin/systemctl', 'show', identity['name'], '--property=' + ','.join(fields)])
    result = dict(line.split('=', 1) for line in raw.splitlines() if '=' in line)
    need(set(result) == set(fields) or result.get('LoadState') == 'not-found', 'LEGACY_STATE_SCHEMA')
    return result


def require_legacy_disabled(config):
    state = legacy_state(config)
    need(state.get('LoadState') in ('loaded', 'not-found') and state.get('ActiveState') == 'inactive'
         and state.get('UnitFileState', '') in ('disabled', '', 'masked') and state.get('MainPID', '0') == '0',
         'LEGACY_COLLECTOR_STILL_ENABLED_OR_RUNNING')


def stage(api, config, apply=False):
    supervisor_ready(api)
    row = process(api, config, missing=True)
    if row is None and apply:
        api.request('POST', PROCESS_ROUTE, {'operate': 'create', **expected_process(config)})
        row = process(api, config)
    if row is not None:
        need(row['autoStart'] == 'false' and is_stopped(row), 'STAGED_PROCESS_NOT_STOPPED')
    return {'state': 'staged' if row is not None else 'planned',
            'process': safe_process(row) if row is not None else {'name': config['program'], 'autoStart': 'false'}}


def handover_receipt(config, required=True):
    path = Path(config['root']) / '.asa/diagnostics/private/1panel-handover.json'
    if not path.exists() and not path.is_symlink():
        need(not required, 'HANDOVER_RECEIPT_MISSING')
        return None
    value = json.loads(controlled(path))
    need(value.get('format') == 'asa-1panel-handover-v1' and value.get('project') == config['project']
         and value.get('instanceId') == config['instanceId'], 'HANDOVER_RECEIPT_IDENTITY')
    return value


def reconcile(api, config, settings, restart=False, apply=False, preparing=False):
    canonical(config)
    need(settings.get('collectorManager') == MANAGER, 'PANEL_MANAGER_NOT_SELECTED')
    registration(config, selected=True)
    receipt = handover_receipt(config)
    need(receipt.get('state') == ('prepared' if preparing else 'accepted'), 'HANDOVER_REQUIRES_EXPLICIT_RECOVERY')
    require_legacy_disabled(config)
    supervisor_ready(api)
    row = process(api, config)
    need(row['autoStart'] == 'true', 'PANEL_PROCESS_DISABLED_BY_OPERATOR')
    if not apply:
        return {'state': 'planned', 'process': safe_process(row)}
    # A native config update can already have autostarted the program. Sending
    # start during its startsecs window would fail with ALREADY_STARTED and
    # incorrectly roll back an otherwise healthy handover.
    if process_state(row) in ('STARTING', 'OBSERVATION_PENDING'):
        settling = time.monotonic() + 20
        while process_state(row) in ('STARTING', 'OBSERVATION_PENDING'):
            need(time.monotonic() < settling, 'SUPERVISOR_START_NOT_SETTLED')
            time.sleep(0.25)
            row = process(api, config)
    need(is_running(row) or is_stopped(row), 'SUPERVISOR_PROCESS_UNSETTLED')
    if restart or not is_running(row):
        api.request('POST', PROCESS_ROUTE, {'name': config['program'], 'operate': 'restart' if is_running(row) else 'start'})
        row = process(api, config)
    deadline = time.monotonic() + 20
    while not is_running(row):
        need(time.monotonic() < deadline, 'SUPERVISOR_PROCESS_NOT_RUNNING')
        time.sleep(0.25)
        row = process(api, config)
    require_legacy_disabled(config)
    return {'format': RESULT_FORMAT, 'manager': MANAGER, 'root': config['root'], 'project': config['project'],
            'state': 'running', 'processCount': 1, 'restartRequested': restart}


def canonical(config, require_adapter=True):
    root = Path(config['root'])
    for name in ('tools/deployment/__init__.py', 'tools/deployment/collector.py', 'tools/deployment/system.py',
                 'tools/deployment/contracts.py', 'tools/deployment/resources.py', 'tools/deployment/paths.py',
                 'tools/collect_logs.py'):
        controlled(root / name)
    sys.path.insert(0, str(root / 'tools'))
    lifecycle = importlib.import_module('deployment.collector')
    install = lifecycle.Installation(root)
    need(getattr(lifecycle, 'PANEL_MANAGER', None) == MANAGER
         or not require_adapter and install.settings.get('collectorManager') is None,
         'CANONICAL_PANEL_ADAPTER_MISSING')
    lifecycle.root_runner_paths(install)
    lifecycle.validate_config(install)
    return lifecycle, install


def collector_run(config, settings):
    need(settings.get('collectorManager') == MANAGER, 'PANEL_MANAGER_NOT_SELECTED')
    registration(config, selected=True)
    require_legacy_disabled(config)
    canonical(config)
    python = Path(config['python']).resolve(strict=True)
    controlled(python, limit=32 * 1024 * 1024)
    controlled(Path('/usr/bin/setpriv').resolve(strict=True), limit=2 * 1024 * 1024)
    os.umask(0o077)
    os.chdir(config['root'])
    os.execv('/usr/bin/setpriv', ['setpriv', '--no-new-privs', str(python), '-B',
             str(Path(config['root']) / 'tools/collect_logs.py'), '--root', config['root']])


def adopt(api, config, settings, apply=False):
    """Single reversible handover. An interrupted receipt blocks blind retries."""
    lifecycle, install = canonical(config)
    runner = registration(config)
    selected = settings.get('collectorManager') == MANAGER and runner.get('manager') == MANAGER
    if selected:
        need(handover_receipt(config).get('state') == 'accepted', 'HANDOVER_REQUIRES_EXPLICIT_RECOVERY')
        return reconcile(api, config, settings, apply=apply)
    need(settings.get('collectorManager') is None and runner.get('manager') is None, 'MANAGER_SELECTION_INCOMPLETE')
    stage(api, config, apply=apply)
    row = process(api, config, missing=not apply)
    if row is not None:
        need(row['autoStart'] == 'false' and is_stopped(row), 'STAGED_PROCESS_NOT_STOPPED')
    identity = lifecycle.runner_identity(install)
    target_settings = {**settings, 'collectorManager': MANAGER}
    target_runner = {**identity, 'manager': MANAGER}
    log_path = install.state / 'log-collector.json'
    original_logs = json.loads(controlled(log_path))
    target_logs = with_native_logs(original_logs, native_log_directory(api, config, staged=row is not None))
    with redirect_stdout(io.StringIO()):
        lifecycle.verify_systemd_state(identity, Path('/etc/systemd/system') / identity['name'], config['python'])
    previous = legacy_state(config)
    need(previous.get('UnitFileState', '') in ('enabled', 'disabled', '')
         and previous.get('ActiveState') in ('active', 'inactive'), 'LEGACY_STATE_UNSUPPORTED')
    if not apply:
        return {'state': 'planned', 'program': config['program'], 'legacy': previous,
                'preserve': ['collector identity', 'private index', 'event history', 'cursors']}
    paths = [Path(config['root']) / '.asa' / name for name in ('installation.json', 'collector-runner.json', 'log-collector.json')]
    original = [controlled(path) for path in paths]
    before_health = lifecycle.status(install)
    old_nonce = (before_health.get('process') or {}).get('nonce')
    marker = install.state / 'diagnostics/private/1panel-handover.json'
    need(not marker.exists() and not marker.is_symlink(), 'PRIOR_HANDOVER_RECEIPT_REQUIRES_REVIEW')
    with lifecycle.runner_lock(install):
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(json_bytes({'format': 'asa-1panel-handover-v1', 'state': 'prepared',
                         'project': config['project'], 'instanceId': config['instanceId'],
                         'legacyState': previous, 'originalSettings': settings, 'originalRunner': runner,
                         'targetSettings': target_settings, 'targetRunner': target_runner,
                         'originalCollectorConfiguration': original_logs, 'targetCollectorConfiguration': target_logs}))
            stream.flush()
            os.fsync(stream.fileno())
        receipt_directory = os.open(marker.parent, os.O_DIRECTORY | os.O_RDONLY)
        try:
            os.fsync(receipt_directory)
        finally:
            os.close(receipt_directory)
        native_may_run = False
        try:
            if previous.get('LoadState') == 'loaded':
                run(['/usr/bin/systemctl', 'disable', '--now', identity['name']], timeout=40)
            require_legacy_disabled(config)
            atomic(paths[0], json_bytes(target_settings))
            atomic(paths[1], json_bytes(target_runner))
            atomic(paths[2], json_bytes(target_logs))
            native_may_run = True
            api.request('POST', PROCESS_ROUTE, {'operate': 'update', **expected_process(config, 'true')})
            result = reconcile(api, config, target_settings, apply=True, preparing=True)
            observed = lifecycle.status(lifecycle.Installation(Path(config['root'])))
            deadline = time.monotonic() + 30
            while observed.get('state') != 'running' or (observed.get('process') or {}).get('nonce') == old_nonce:
                need(time.monotonic() < deadline, 'COLLECTOR_HEARTBEAT_NOT_ACCEPTED')
                time.sleep(0.25)
                observed = lifecycle.status(lifecycle.Installation(Path(config['root'])))
            receipt = json.loads(controlled(marker))
            atomic(marker, json_bytes({**receipt, 'state': 'accepted'}))
            return {**result, 'handover': 'accepted', 'historyReset': False}
        except BaseException:
            safe_to_restore = not native_may_run
            if native_may_run:
                try:
                    api.request('POST', PROCESS_ROUTE, {'operate': 'update', **expected_process(config, 'false')})
                    current = process(api, config)
                    if not is_stopped(current):
                        api.request('POST', PROCESS_ROUTE, {'name': config['program'], 'operate': 'stop'})
                    safe_to_restore = is_stopped(process(api, config))
                except Exception:
                    safe_to_restore = False
            if safe_to_restore:
                for path, raw in zip(paths, original):
                    atomic(path, raw)
                if previous.get('LoadState') == 'loaded':
                    run(['/usr/bin/systemctl', 'enable' if previous.get('UnitFileState') == 'enabled' else 'disable', identity['name']])
                    if previous.get('ActiveState') == 'active':
                        run(['/usr/bin/systemctl', 'start', identity['name']])
                receipt = json.loads(controlled(marker))
                atomic(marker, json_bytes({**receipt, 'state': 'rolled_back'}))
            raise PanelCollectorError('HANDOVER_FAILED_ROLLED_BACK' if safe_to_restore else 'HANDOVER_RECOVERY_REQUIRED') from None


def recover(api, config, settings, apply=False):
    """Restore only the exact pre-handover manager after a failed handover.

    A native STOPPED readback precedes re-enabling the old runner. Accepted
    handovers are not reverted by this recovery command. All evidence remains.
    """
    lifecycle, install = canonical(config)
    marker = install.state / 'diagnostics/private/1panel-handover.json'
    receipt = json.loads(controlled(marker))
    need(receipt.get('format') == 'asa-1panel-handover-v1'
         and receipt.get('state') in ('prepared', 'rolled_back')
         and receipt.get('project') == config['project'] and receipt.get('instanceId') == config['instanceId'],
         'HANDOVER_RECEIPT_NOT_RECOVERABLE')
    original_settings, original_runner = receipt.get('originalSettings'), receipt.get('originalRunner')
    need(isinstance(original_settings, dict) and isinstance(original_runner, dict)
         and original_settings.get('collectorManager') is None and original_runner.get('manager') is None
         and original_settings.get('instanceId') == config['instanceId']
         and original_settings.get('project') == config['project']
         and original_runner.get('root') == config['root'] and original_runner.get('project') == config['project']
         and original_runner.get('mode') == 'system-root' and original_runner.get('owner') == 'root',
         'HANDOVER_ORIGINAL_IDENTITY_MISMATCH')
    current_runner = registration(config)
    target_settings, target_runner = receipt.get('targetSettings'), receipt.get('targetRunner')
    need(target_settings == {**original_settings, 'collectorManager': MANAGER}
         and isinstance(target_runner, dict) and target_runner.get('manager') == MANAGER
         and all(target_runner.get(key) == original_runner.get(key) for key in ('root', 'project', 'mode', 'owner', 'name'))
         and re.fullmatch(r'[a-f0-9]{64}', str(target_runner.get('scriptDigest', '')))
         and re.fullmatch(r'[a-f0-9]{40}', str(target_runner.get('revision', ''))), 'HANDOVER_TARGET_IDENTITY_MISMATCH')
    need(settings in (original_settings, target_settings) and current_runner in (original_runner, target_runner),
         'METADATA_CHANGED_AFTER_HANDOVER')
    original_logs, target_logs = receipt.get('originalCollectorConfiguration'), receipt.get('targetCollectorConfiguration')
    current_logs = json.loads(controlled(install.state / 'log-collector.json'))
    need(isinstance(original_logs, dict) and isinstance(target_logs, dict)
         and current_logs in (original_logs, target_logs), 'LOG_CONFIGURATION_CHANGED_AFTER_HANDOVER')
    previous = receipt.get('legacyState', {})
    need(previous.get('LoadState') in ('loaded', 'not-found')
         and previous.get('UnitFileState', '') in ('enabled', 'disabled', '')
         and previous.get('ActiveState') in ('active', 'inactive'), 'HANDOVER_OLD_STATE_INVALID')
    with redirect_stdout(io.StringIO()):
        lifecycle.verify_systemd_state(original_runner, Path('/etc/systemd/system') / original_runner['name'], config['python'])
    supervisor_ready(api)
    row = process(api, config, missing=True)
    if not apply:
        return {'state': 'recovery_planned', 'legacy': previous,
                'native': safe_process(row) if row else None, 'historyReset': False}
    with lifecycle.runner_lock(install):
        if row is not None:
            if row['autoStart'] != 'false':
                api.request('POST', PROCESS_ROUTE, {'operate': 'update', **expected_process(config, 'false')})
            if not is_stopped(process(api, config)):
                api.request('POST', PROCESS_ROUTE, {'name': config['program'], 'operate': 'stop'})
            need(is_stopped(process(api, config)), 'NATIVE_STOP_NOT_CONFIRMED')
        paths = [install.state / name for name in ('installation.json', 'collector-runner.json', 'log-collector.json')]
        atomic(paths[0], json_bytes(original_settings))
        atomic(paths[1], json_bytes(original_runner))
        atomic(paths[2], json_bytes(original_logs))
        if previous['LoadState'] == 'loaded':
            run(['/usr/bin/systemctl', 'enable' if previous.get('UnitFileState') == 'enabled' else 'disable', original_runner['name']])
            run(['/usr/bin/systemctl', 'start' if previous['ActiveState'] == 'active' else 'stop', original_runner['name']])
        restored = legacy_state(config)
        need(all(restored.get(key, '') == previous.get(key, '') for key in ('LoadState', 'UnitFileState', 'ActiveState')),
             'LEGACY_RESTORE_NOT_CONFIRMED')
        atomic(marker, json_bytes({**receipt, 'state': 'rolled_back'}))
    return {'state': 'rolled_back', 'project': config['project'], 'historyReset': False, 'collectorAccepted': False,
            'retry': 'Review and archive the retained handover receipt before a new adoption.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('status', 'stage', 'adopt', 'reconcile', 'run', 'recover'))
    parser.add_argument('--root')
    parser.add_argument('--project')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--restart', action='store_true')
    args = parser.parse_args()
    try:
        config, settings = load_configuration(args.root, args.project)
        if args.action == 'run':
            collector_run(config, settings)
            raise PanelCollectorError('EXEC_DID_NOT_REPLACE_PROCESS')
        api = api_client(config)
        if args.action == 'status':
            ready = supervisor_ready(api)
            row = process(api, config, missing=True)
            result = {'state': 'observed', 'supervisor': ready, 'process': safe_process(row) if row else None,
                      'legacy': legacy_state(config), 'selectedManager': settings.get('collectorManager'),
                      'root': config['root'], 'project': config['project']}
        elif args.action == 'stage':
            result = stage(api, config, args.apply)
        elif args.action == 'adopt':
            result = adopt(api, config, settings, args.apply)
        elif args.action == 'recover':
            result = recover(api, config, settings, args.apply)
        else:
            result = reconcile(api, config, settings, args.restart, args.apply)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
        code = str(error) if isinstance(error, PanelCollectorError) else type(error).__name__
        print(json.dumps({'state': 'blocked', 'code': code}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
