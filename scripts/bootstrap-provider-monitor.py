"""Create-only provider monitor bootstrap, or owner-only ASA file-source integration.

PLAN is the default. No Docker, app imports, automatic privilege elevation or app restart.
"""
from contextlib import contextmanager
import argparse
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import socket
import stat
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit
import uuid

MANIFEST_PIN = (1235, '64980bb48025c1fec82bbad7762ab1a212ae710d61828018352cb7add066d35a')
SOURCE = Path(__file__).absolute().parents[1]
CODE = Path('/var/lib/asa-provider-monitor-runtime')
CONFIG = Path('/etc/asa-provider-monitor')
OUTPUT = Path('/var/lib/asa-provider-monitor')
UNITS = Path('/etc/systemd/system')
NAMES = ('asa-provider-monitor.service', 'asa-provider-monitor.timer')
APP_REVISION = '2a28cffe7a20302f9b7231ef823bdc05f3fe43d0'
APPLICATION_FILES = {'deployment/__init__.py', 'deployment/paths.py', 'deployment/operational.py'}
SOURCE_FILES = {'scripts/provider_health_exporter.py', *('templates/provider-monitor/'+n for n in NAMES)}
DOWNLOAD_SECONDS = 12


class Blocked(RuntimeError):
    pass


def need(ok, code):
    if not ok:
        raise Blocked(code)


def encoded(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)+'\n').encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def unique(pairs):
    value = {}
    for key, item in pairs:
        need(key not in value, 'DUPLICATE_JSON_KEY')
        value[key] = item
    return value


def decoded(raw):
    return json.loads(raw, object_pairs_hook=unique)


def absolute(value):
    p = Path(value)
    need(p.is_absolute() and '..' not in p.parts and '\x00' not in str(p), 'ABSOLUTE_PATH')
    need(str(p) == str(value), 'CANONICAL_PATH')
    return p


def identity(info):
    return tuple(getattr(info, n) for n in ('st_dev', 'st_ino', 'st_mode', 'st_uid', 'st_gid', 'st_nlink', 'st_size', 'st_mtime_ns', 'st_ctime_ns'))


@contextmanager
def directory(path, owners=(0,)):
    path = absolute(str(path))
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in ('', *path.parts[1:]):
            if part:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            info = os.fstat(fd)
            need(info.st_uid in owners and not info.st_mode & 0o022, 'DIRECTORY_TRUST')
        yield fd
    finally:
        os.close(fd)


def read_at(fd, name, limit, *, owner=0, mode=None, gid=None):
    stream = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    try:
        info = os.fstat(stream)
        need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == owner
             and not info.st_mode & 0o022 and info.st_size <= limit, 'FILE_TRUST')
        need(mode is None or stat.S_IMODE(info.st_mode) == mode, 'FILE_MODE')
        need(gid is None or info.st_gid == gid, 'FILE_GROUP')
        raw = bytearray()
        while chunk := os.read(stream, min(65536, limit+1-len(raw))):
            raw.extend(chunk)
            need(len(raw) <= limit, 'FILE_BOUND')
        need(identity(info) == identity(os.fstat(stream)), 'FILE_CHANGED')
        return bytes(raw), identity(info)
    finally:
        os.close(stream)


def read(path, limit, **kwargs):
    owners = kwargs.pop('owners', (0,))
    with directory(path.parent, owners) as fd:
        return read_at(fd, path.name, limit, **kwargs)


def present(path):
    with directory(path.parent) as fd:
        try:
            return identity(os.stat(path.name, dir_fd=fd, follow_symlinks=False))
        except FileNotFoundError:
            return None


def pinned(raw, pin):
    need(len(raw) == pin['bytes'] and digest(raw) == pin['sha256'], 'SOURCE_PIN_MISMATCH')
    return raw


def sources():
    raw, _ = read(SOURCE/'templates/provider-monitor/bootstrap-manifest.json', MANIFEST_PIN[0])
    need((len(raw), digest(raw)) == MANIFEST_PIN, 'MANIFEST_PIN_MISMATCH')
    manifest = decoded(raw)
    need(set(manifest) == {'format', 'applicationRevision', 'applicationFiles', 'sourceFiles', 'serviceProjection'}
         and manifest['format'] == 'asa-provider-bootstrap-manifest-v1'
         and manifest['applicationRevision'] == APP_REVISION
         and set(manifest['applicationFiles']) == APPLICATION_FILES
         and set(manifest['sourceFiles']) == SOURCE_FILES, 'MANIFEST_CONTRACT')
    files = {p: pinned(read(SOURCE/p, pin['bytes'])[0], pin) for p, pin in manifest['sourceFiles'].items()}
    # One closed portability projection; the original PR66 template stays immutable.
    service = 'templates/provider-monitor/asa-provider-monitor.service'
    need(files[service].count(b'/opt/asa-provider-monitor') == 2, 'SERVICE_PROJECTION')
    files[service] = pinned(files[service].replace(b'/opt/asa-provider-monitor', b'/var/lib/asa-provider-monitor-runtime'),
                            manifest['serviceProjection'])
    return manifest, files


def origin(value):
    url = urlsplit(value)
    need(url.scheme in ('http', 'https') and url.username is None and url.password is None
         and url.path == '' and not url.query and not url.fragment and url.hostname is not None, 'NUMERIC_PRIVATE_ORIGIN')
    host = ipaddress.ip_address(url.hostname)
    need((host.is_private or host.is_loopback) and not host.is_link_local and not host.is_multicast
         and not host.is_unspecified and not host.is_reserved, 'NUMERIC_PRIVATE_ORIGIN')
    need(url.port is not None and 0 < url.port < 65536
         and value == url.scheme+'://'+('['+str(host)+']' if host.version == 6 else str(host))+':'+str(url.port),
         'CANONICAL_ORIGIN')
    return value


def configuration(args):
    need(type(args.reader_gid) is int and 0 < args.reader_gid < 2**31, 'READER_GROUP')
    import grp
    grp.getgrgid(args.reader_gid)
    return encoded(dict(version=1, bundleRoot=str(CODE/'app-source/tools'), outputDirectory=str(OUTPUT),
                        readerGid=args.reader_gid, providers={name: {'origin': origin(getattr(args, name+'_origin'))}
                                                             for name in ('comfyui', 'ollama')}))


def fetch_http(path, pin):
    """Worker only: direct fixed GitHub origin, no proxy/redirect and exact bound."""
    connection = http.client.HTTPSConnection('raw.githubusercontent.com', timeout=10)
    deadline, timer = time.monotonic()+10, None
    try:
        connection.connect()
        sock = connection.sock
        def abort():
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        timer = threading.Timer(max(.001, deadline-time.monotonic()), abort)
        timer.daemon = True
        timer.start()
        connection.request('GET', '/spikeal8-maker/asa-lab/'+APP_REVISION+'/tools/'+path,
                           headers={'Accept': 'application/octet-stream'})
        response = connection.getresponse()
        need(response.status == 200, 'SOURCE_HTTP')
        declared = response.getheader('Content-Length')
        need(declared is None or declared.isdigit() and int(declared) == pin['bytes'], 'SOURCE_HTTP_LENGTH')
        raw = bytearray()
        while True:
            need(time.monotonic() < deadline, 'SOURCE_DEADLINE')
            chunk = response.read1(min(65536, pin['bytes']+1-len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
            need(len(raw) <= pin['bytes'], 'SOURCE_HTTP_BOUND')
        need(time.monotonic() < deadline, 'SOURCE_DEADLINE')
        return pinned(bytes(raw), pin)
    finally:
        if timer:
            timer.cancel()
        connection.close()


def download(path, pin):
    # The killable process boundary starts before the worker can resolve DNS or
    # connect. A socket timeout alone cannot bound getaddrinfo/TLS/connect stalls.
    need(path in APPLICATION_FILES, 'SOURCE_PATH')
    try:
        result = subprocess.run([sys.executable, '-I', '-B', str(Path(__file__).absolute()),
                                 '_fetch', '--path', path, '--apply'], capture_output=True, timeout=DOWNLOAD_SECONDS)
    except subprocess.TimeoutExpired:
        # subprocess.run kills and reaps its child on timeout; no background DNS worker remains.
        raise Blocked('SOURCE_TRANSFER_TIMEOUT') from None
    need(result.returncode == 0, 'SOURCE_TRANSFER_FAILED')
    return pinned(result.stdout, pin)


def command(argv):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=35)
    need(result.returncode == 0, 'COMMAND_FAILED')
    return result.stdout.strip()


def unit_guard(installed):
    for name in NAMES:
        result = subprocess.run(['systemctl', 'show', name, '--property=FragmentPath,DropInPaths,LoadState'], capture_output=True, text=True, timeout=10)
        need(result.returncode in (0, 1), 'UNIT_QUERY_FAILED')
        values = result.stdout
        data = dict(line.split('=', 1) for line in values.splitlines() if '=' in line)
        need(set(data) == {'FragmentPath', 'DropInPaths', 'LoadState'}, 'UNIT_QUERY_FAILED')
        need(not data.get('DropInPaths') and data.get('LoadState') in ('not-found', 'loaded'), 'UNIT_COLLISION')
        need(data.get('FragmentPath', '') in (('', str(UNITS/name)) if installed else ('',)), 'UNIT_COLLISION')
        need(installed or data['LoadState'] == 'not-found', 'UNIT_COLLISION')


def receipt(config):
    return encoded(dict(format='asa-provider-bootstrap-install-v1', manifestSha256=MANIFEST_PIN[1], configSha256=digest(config)))


def layout(config, source, app):
    return {CODE/'provider_health_exporter.py': (source['scripts/provider_health_exporter.py'], 0o600),
            **{CODE/'app-source/tools'/p: (raw, 0o600) for p, raw in app.items()},
            CONFIG/'config.json': (config, 0o600), CONFIG/'installation.json': (receipt(config), 0o600),
            **{UNITS/name: (source['templates/provider-monitor/'+name], 0o644) for name in NAMES}}


def dirs(gid):
    return {CODE: (0o755, 0), CODE/'app-source': (0o755, 0), CODE/'app-source/tools': (0o755, 0),
            CODE/'app-source/tools/deployment': (0o755, 0), CONFIG: (0o700, 0), OUTPUT: (0o750, gid)}


def inspect(config, source, manifest, gid, *, query_units=False):
    for name in NAMES:
        need(present(UNITS/(name+'.d')) is None, 'UNIT_DROPINS')
    roots = [present(p) for p in (CODE, CONFIG, OUTPUT)]
    if not any(roots):
        need(not any(present(UNITS/name) for name in NAMES), 'FOREIGN_UNIT')
        if query_units:
            unit_guard(False)
        return False
    need(all(roots), 'PARTIAL_INSTALL_PRESERVED')
    for path, (mode, group) in dirs(gid).items():
        with directory(path) as fd:
            info = os.fstat(fd)
            need(stat.S_IMODE(info.st_mode) == mode and info.st_gid == group, 'INSTALL_DIRECTORY_MODE')
    allowed = {CODE: {'provider_health_exporter.py', 'app-source'}, CODE/'app-source': {'tools'},
               CODE/'app-source/tools': {'deployment'}, CODE/'app-source/tools/deployment': {'__init__.py', 'paths.py', 'operational.py'},
               CONFIG: {'config.json', 'installation.json', 'bootstrap.lock'}}
    for path, names in allowed.items():
        with directory(path) as fd:
            need(set(os.listdir(fd)) == names, 'FOREIGN_OR_PARTIAL_INSTALL')
    app = {p: pinned(read(CODE/'app-source/tools'/p, pin['bytes'], mode=0o600, gid=0)[0], pin)
           for p, pin in manifest['applicationFiles'].items()}
    for path, (expected, mode) in layout(config, source, app).items():
        raw, _ = read(path, len(expected), mode=mode, gid=0)
        need(raw == expected, 'EXISTING_CONFIG_OR_SOURCE_DIFFERS')
    read(CONFIG/'bootstrap.lock', 0, mode=0o600, gid=0)
    if query_units:
        unit_guard(True)
    return True


def make_dir(path, mode, gid):
    with directory(path.parent) as fd:
        os.mkdir(path.name, mode=0o700, dir_fd=fd)
        child = os.open(path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        try:
            os.fchown(child, 0, gid)
            os.fchmod(child, mode)
            os.fsync(child)
        finally:
            os.close(child)
        os.fsync(fd)


def write_new(path, raw, mode):
    with directory(path.parent) as parent:
        fd = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            os.fchown(fd, 0, 0)
            os.fchmod(fd, mode)
            with os.fdopen(fd, 'wb', closefd=False) as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(parent)


def activate():
    enabled = subprocess.run(['systemctl', 'is-enabled', NAMES[1]], capture_output=True, text=True, timeout=10).stdout.strip()
    active = subprocess.run(['systemctl', 'is-active', NAMES[1]], capture_output=True, text=True, timeout=10).stdout.strip()
    if enabled == 'enabled' and active == 'active':
        return False
    need(enabled in ('disabled', 'enabled', 'not-found', '') and active in ('inactive', 'failed', 'unknown'), 'TIMER_STATE')
    command(['systemctl', 'daemon-reload'])
    unit_guard(True)
    command(['systemctl', 'start', NAMES[0]])
    command(['systemctl', 'enable', '--now', NAMES[1]])
    need(command(['systemctl', 'is-active', NAMES[1]]) == 'active', 'TIMER_NOT_ACTIVE')
    return True


def install(args):
    need(os.geteuid() == 0, 'ROOT_REQUIRED')
    # The bootstrap itself is staged from the externally selected reviewed Git commit.
    read(Path(__file__).absolute(), 64*1024)
    manifest, source = sources()
    config = configuration(args)
    existing = inspect(config, source, manifest, args.reader_gid, query_units=args.apply)
    app = None
    if args.offline_bundle:
        base = absolute(args.offline_bundle)
        app = {p: pinned(read(base/p, pin['bytes'])[0], pin) for p, pin in manifest['applicationFiles'].items()}
    if not args.apply:
        need(not args.activate, 'ACTIVATE_REQUIRES_APPLY')
        return dict(status='NO_CHANGE' if existing else 'PLAN', writes=False,
                    applicationDownloads=0 if existing or app else 3, providerRequests=False, restartRequired=False)
    import fcntl
    if not existing:
        if app is None:
            app = {p: download(p, pin) for p, pin in manifest['applicationFiles'].items()}
        # Recheck every absent target and source/config condition before the first write.
        need(not inspect(config, source, manifest, args.reader_gid, query_units=True), 'INSTALL_CHANGED')
        make_dir(CONFIG, 0o700, 0)
        write_new(CONFIG/'bootstrap.lock', b'', 0o600)
    with directory(CONFIG) as parent:
        lock = os.open('bootstrap.lock', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            need(identity(os.fstat(lock)) == read_at(parent, 'bootstrap.lock', 0, mode=0o600, gid=0)[1], 'LOCK_CHANGED')
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if not existing:
                for path, (mode, gid) in dirs(args.reader_gid).items():
                    if path != CONFIG:
                        make_dir(path, mode, gid)
                values = layout(config, source, app)
                for path, (raw, mode) in values.items():
                    if path != CONFIG/'installation.json':
                        write_new(path, raw, mode)
                # Receipt commits only the complete create-only layout. Failure preserves evidence.
                write_new(CONFIG/'installation.json', *values[CONFIG/'installation.json'])
            need(inspect(config, source, manifest, args.reader_gid, query_units=True), 'INSTALL_VERIFY')
            command(['/usr/bin/python3', '-I', '-B', str(CODE/'provider_health_exporter.py'), '--config', str(CONFIG/'config.json')])
            command(['systemd-analyze', 'verify', *(str(UNITS/name) for name in NAMES)])
            changed = activate() if args.activate else False
        finally:
            os.close(lock)
    return dict(status='NO_CHANGE' if existing and not changed else 'INSTALLED', configSha256=digest(config),
                providerRequests=bool(changed), timerActivationChanged=changed, appOrGpuRestarted=False)


def root_reader_gid(settings):
    """The explicit system-root reader group is independent of root's primary groups."""
    import grp
    gid = settings.get('collectorReaderGid')
    need(settings.get('collectorMode') == 'system-root'
         and type(gid) is int and 0 < gid < 2**31, 'ROOT_READER_GROUP')
    grp.getgrgid(gid)
    forbidden = {0}
    try:
        forbidden.add(grp.getgrnam('docker').gr_gid)
    except KeyError:
        pass
    try:
        forbidden.add(os.stat('/var/run/docker.sock').st_gid)
    except FileNotFoundError:
        pass
    need(gid not in forbidden, 'ROOT_READER_GROUP')
    return gid


def collector_unit(text, root, project, runner, uid, reader_gid=None):
    allowed = {'Unit': {'Description', 'After'}, 'Service': {'User', 'WorkingDirectory', 'ExecStart', 'Restart', 'RestartSec', 'NoNewPrivileges', 'UMask'},
               'Install': {'WantedBy'}}
    if uid == 0:
        allowed['Service'].add('Group')
    values, section = {}, None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(('#', ';')):
            continue
        if line.startswith('[') and line.endswith(']'):
            section = line[1:-1]
            need(section in allowed, 'COLLECTOR_UNIT')
            continue
        key, sep, value = line.partition('=')
        need(sep and section in allowed and key in allowed[section] and (section, key) not in values, 'COLLECTOR_UNIT')
        values[section, key] = value
    fields = {('Unit', 'Description'), ('Unit', 'After'), ('Service', 'WorkingDirectory'), ('Service', 'ExecStart'),
              ('Service', 'Restart'), ('Service', 'RestartSec'), ('Service', 'NoNewPrivileges'), ('Service', 'UMask'), ('Install', 'WantedBy')}
    need(set(values) == fields | ({('Service', 'User'), ('Service', 'Group')} if uid == 0 else set()), 'COLLECTOR_UNIT')
    argv = shlex.split(values['Service', 'ExecStart'])
    need(len(argv) == 4 and Path(argv[0]).is_absolute() and Path(argv[0]).resolve() == Path(sys.executable).resolve()
         and argv[1:] == [str(root/'tools/collect_logs.py'), '--root', str(root)]
         and values['Service', 'WorkingDirectory'] == str(root)
         and values['Service', 'Restart'] == 'always' and values['Service', 'RestartSec'] == '20'
         and values['Service', 'NoNewPrivileges'] == 'true' and values['Service', 'UMask'] == '0077'
         and values['Install', 'WantedBy'] == ('multi-user.target' if uid == 0 else 'default.target')
         and (uid != 0 or (values['Service', 'User'] == 'root' and type(reader_gid) is int
                          and reader_gid > 0 and values['Service', 'Group'] == str(reader_gid))), 'COLLECTOR_UNIT')


def integrate(args):
    """Only the registered existing owner writes its own config; no installed-code execution."""
    import pwd
    uid = os.geteuid()
    owner = pwd.getpwuid(uid)
    root = absolute(args.root)
    need(re.fullmatch('[a-z0-9][a-z0-9_-]{0,62}', args.project), 'COLLECTOR_PROJECT')
    owners = (0, uid)
    if uid == 0:
        read(Path(__file__).absolute(), 64*1024)
    with directory(root/'.asa', owners) as fd:
        if uid == 0:
            first = read_at(fd, 'installation.json', 65536, owner=0, mode=0o600)
            initial = decoded(first[0])
            need(isinstance(initial, dict), 'COLLECTOR_METADATA')
            metadata_gid = root_reader_gid(initial)
            need(first[1][4] == metadata_gid, 'FILE_GROUP')
            files = {'installation.json': first,
                     **{name: read_at(fd, name, 65536, owner=0, mode=0o600, gid=metadata_gid)
                        for name in ('collector-runner.json', 'log-collector.json')}}
        else:
            metadata_gid = owner.pw_gid
            files = {name: read_at(fd, name, 65536, owner=uid, mode=0o600, gid=owner.pw_gid)
                     for name in ('installation.json', 'collector-runner.json', 'log-collector.json')}
        settings, runner, config = [decoded(files[n][0]) for n in files]
        need(all(isinstance(v, dict) for v in (settings, runner, config)), 'COLLECTOR_METADATA')
        need(settings.get('format') == 'asa-installation-v1' and settings.get('project') == args.project
             and settings.get('profile') in ('dev', 'production'), 'INSTALLATION_IDENTITY')
        mode = 'system-root' if uid == 0 else 'user'
        need(settings.get('collectorMode') == 'system-root' if uid == 0 else settings.get('collectorMode') is None, 'COLLECTOR_MODE')
        name = runner.get('name')
        need(type(runner.get('version')) is int and runner['version'] == 1 and runner.get('root') == str(root) and runner.get('project') == args.project
             and runner.get('mode') == mode and runner.get('owner') == owner.pw_name
             and name in ('asa-log-collector-'+args.project+'.service', 'asa-log-collector.service')
             and isinstance(runner.get('revision'), str) and re.fullmatch('[a-f0-9]{40}', runner['revision']), 'RUNNER_IDENTITY')
        actual = hashlib.sha256()
        for path in ('collect_logs.py', 'collect-windows-logs.ps1', 'resource_sampling.py', 'memory_sampling.py', 'deployment/operational.py'):
            raw, _ = read(root/'tools'/path, 1024*1024, owners=owners, owner=uid)
            actual.update(path.encode())
            actual.update(raw)
        need(runner.get('scriptDigest') == actual.hexdigest(), 'RUNNER_SOURCE_DIGEST')
        unit = (Path('/etc/systemd/system') if uid == 0 else Path(owner.pw_dir)/'.config/systemd/user')/name
        text, _ = read(unit, 16384, owners=owners, owner=uid)
        # Local unit file ownership/shape is checked; loaded process identity is a separate operator check.
        collector_unit(text.decode(), root, args.project, runner, uid, metadata_gid if uid == 0 else None)
        dropin = unit.with_name(name+'.d')
        with directory(unit.parent, owners) as parent:
            try:
                os.stat(dropin.name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise Blocked('COLLECTOR_DROPINS')
        need(type(config.get('version')) is int and config['version'] == 1 and config.get('project') == args.project,
             'COLLECTOR_CONFIG')
        for key, low, high in (('retentionDays', 1, 365), ('maxBytes', 16*1024**2, 10*1024**3), ('intervalSeconds', 10, 300)):
            need(key not in config or type(config[key]) is int and low <= config[key] <= high, 'COLLECTOR_CONFIG')
        rows = config.get('fileSources', [])
        need(isinstance(rows, list) and all(isinstance(row, dict) and isinstance(row.get('source'), str)
             and re.fullmatch('[a-z][a-z0-9_-]{0,63}', row['source']) and isinstance(row.get('path'), str)
             and Path(row['path']).is_absolute() for row in rows), 'FILE_SOURCES')
        wanted = {'source': 'provider-monitor', 'path': str(OUTPUT)}
        matches = [row for row in rows if row['source'] == 'provider-monitor']
        need(not matches or matches == [wanted], 'FILE_SOURCE_COLLISION')
        with directory(OUTPUT) as output:
            info = os.fstat(output)
            allowed_groups = {metadata_gid} if uid == 0 else {owner.pw_gid, *os.getgroups()}
            need(stat.S_IMODE(info.st_mode) == 0o750 and info.st_gid in allowed_groups, 'MONITOR_READER_GROUP')
            read_at(output, 'health.log', 1024*1024, mode=0o640, gid=info.st_gid)
        if matches:
            return dict(status='NO_CHANGE', restartRequired=None, unit=name, mode=mode, loadedUnitVerified=False)
        changed = {**config, 'fileSources': [*rows, wanted]}
        if args.apply:
            import fcntl
            lock = os.open('log-collector.json', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
            temporary = '.provider-monitor-'+uuid.uuid4().hex+'.tmp'
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                need(identity(os.fstat(lock)) == files['log-collector.json'][1], 'CONFIG_CAS')
                out = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
                try:
                    if uid == 0:
                        need(os.fstat(out).st_uid == 0, 'CONFIG_NEW_OWNER')
                        if os.fstat(out).st_gid != metadata_gid:
                            os.fchown(out, 0, metadata_gid)
                    os.fchmod(out, 0o600)
                    with os.fdopen(out, 'wb', closefd=False) as stream:
                        stream.write(encoded(changed))
                        stream.flush()
                        os.fsync(out)
                    need(os.fstat(out).st_uid == uid and os.fstat(out).st_gid == metadata_gid, 'CONFIG_NEW_OWNER')
                finally:
                    os.close(out)
                for file, (_, expected) in files.items():
                    need(identity(os.stat(file, dir_fd=fd, follow_symlinks=False)) == expected, 'CONFIG_CAS')
                os.replace(temporary, 'log-collector.json', src_dir_fd=fd, dst_dir_fd=fd)
                os.fsync(fd)
            finally:
                os.close(lock)
                try:
                    os.unlink(temporary, dir_fd=fd)
                except FileNotFoundError:
                    pass
        return dict(status='INTEGRATED' if args.apply else 'PLAN', restartRequired=True, unit=name, mode=mode,
                    configSha256=digest(encoded(changed)), loadedUnitVerified=False, appOrHistoryChanged=False)


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Blocked('ARGUMENTS')


def main(argv=None):
    try:
        need(sys.platform == 'linux' and sys.flags.isolated and sys.dont_write_bytecode, 'ISOLATED_LINUX_PYTHON_REQUIRED')
        parser = Parser(description=__doc__)
        actions = parser.add_subparsers(dest='action', required=True, parser_class=Parser)
        setup = actions.add_parser('install')
        setup.add_argument('--reader-gid', type=int, required=True)
        setup.add_argument('--ollama-origin', required=True)
        setup.add_argument('--comfyui-origin', required=True)
        setup.add_argument('--offline-bundle')
        setup.add_argument('--activate', action='store_true')
        setup.add_argument('--apply', action='store_true')
        owner = actions.add_parser('integrate')
        owner.add_argument('--root', required=True)
        owner.add_argument('--project', required=True)
        owner.add_argument('--apply', action='store_true')
        worker = actions.add_parser('_fetch', help=argparse.SUPPRESS)
        worker.add_argument('--path', choices=sorted(APPLICATION_FILES), required=True)
        worker.add_argument('--apply', action='store_true', required=True)
        args = parser.parse_args(argv)
        if args.action == '_fetch':
            need(os.geteuid() == 0, 'ROOT_REQUIRED')
            read(Path(__file__).absolute(), 64*1024)
            manifest, _ = sources()
            raw = fetch_http(args.path, manifest['applicationFiles'][args.path])
            sys.stdout.buffer.write(raw)
            return 0
        result = install(args) if args.action == 'install' else integrate(args)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (Blocked, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, http.client.HTTPException):
        error = sys.exception()
        print(json.dumps(dict(status='BLOCKED', code=str(error) if isinstance(error, Blocked) else type(error).__name__,
                              partialArtifactsPreserved=True, applicationProviderPolicyQualified=False)))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
