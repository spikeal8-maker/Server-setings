"""Isolated root provider observer. No application imports, generation or service control."""
from contextlib import contextmanager
from datetime import datetime, timezone
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import time
import types
import uuid

SOURCE_REVISION = '2a28cffe7a20302f9b7231ef823bdc05f3fe43d0'
# Raw immutable Git blobs, not newline-normalized working copies.
PINS = {
    'deployment/__init__.py': (66, 'b894d56afdcda00c7f34bab54c979d957581cadb120a414f207397850fd70a1b'),
    'deployment/paths.py': (2477, '517981b0847c1fb271eb407d2084ef4db08cc87f0a03bd0a8c84d8a73f6a434b'),
    'deployment/operational.py': (26444, 'c538817ec94cc16df51cec1cc4054789a4f0c873595eaf6263987a2ca2909ffa'),
}
PROVIDERS = ('comfyui', 'ollama')
GETS = {'ollama': ('/api/ps', '/api/tags'), 'comfyui': ('/system_stats',)}
FORMAT = 'asa-isolated-provider-health-v1'
MAX_LOG = 1024 * 1024
MAX_LINE = 8192
MAX_AGE = 120
MAX_PROBE_SECONDS = 20
LOGS = ('health.log', 'health.log.1', 'health.log.2', 'health.log.3', 'health.log.4')
FAILURES = {'none', 'timeout', 'connection_failed', 'invalid_response', 'deadline'}
STATS = {
    'ollama': {'loadedModels', 'availableModels', 'modelBytes', 'vramBytes'},
    'comfyui': {'ramTotalBytes', 'ramFreeBytes', 'deviceCount', 'cudaDeviceCount', 'vramTotalBytes', 'vramFreeBytes'},
}


class Blocked(RuntimeError):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        # Do not echo unknown argv (which could contain an operator secret).
        raise Blocked('ARGUMENTS')


def need(value, code):
    if not value:
        raise Blocked(code)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def encode(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode()


def unique(pairs):
    result = {}
    for key, value in pairs:
        need(key not in result, 'DUPLICATE_JSON_KEY')
        result[key] = value
    return result


def decode(data):
    return json.loads(data, object_pairs_hook=unique)


def signature(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def absolute(value):
    need(isinstance(value, str) and value and '\x00' not in value, 'ABSOLUTE_PATH_REQUIRED')
    path = Path(value)
    need(path.is_absolute() and '..' not in path.parts, 'ABSOLUTE_PATH_REQUIRED')
    return path


@contextmanager
def directory(value):
    """Pin every ancestor from /; root ownership without writable delegation."""
    path = absolute(str(value))
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        need(os.fstat(fd).st_uid == 0 and not os.fstat(fd).st_mode & 0o022, 'UNTRUSTED_DIRECTORY')
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            need(info.st_uid == 0 and not info.st_mode & 0o022, 'UNTRUSTED_DIRECTORY')
        yield fd
    finally:
        os.close(fd)


def read_file(parent, name, limit, *, mode=None, gid=None):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    try:
        info = os.fstat(fd)
        need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == 0
             and not info.st_mode & 0o022 and info.st_size <= limit, 'UNTRUSTED_FILE')
        need(mode is None or stat.S_IMODE(info.st_mode) == mode, 'UNTRUSTED_FILE_MODE')
        need(gid is None or info.st_gid == gid, 'UNTRUSTED_FILE_GROUP')
        data = bytearray()
        while chunk := os.read(fd, min(65536, limit + 1 - len(data))):
            data.extend(chunk)
            need(len(data) <= limit, 'FILE_LIMIT')
        need(signature(info) == signature(os.fstat(fd)), 'FILE_CHANGED')
        return bytes(data), info
    finally:
        os.close(fd)


def configuration(path):
    path = absolute(str(path))
    with directory(path.parent) as parent:
        raw, _ = read_file(parent, path.name, 8192, mode=0o600)
    value = decode(raw)
    need(isinstance(value, dict) and set(value) == {'version', 'bundleRoot', 'outputDirectory', 'readerGid', 'providers'}
         and type(value['version']) is int and value['version'] == 1, 'CONFIG_FORMAT')
    need(type(value['readerGid']) is int and 0 < value['readerGid'] < 2**31, 'CONFIG_GROUP')
    for key in ('bundleRoot', 'outputDirectory'):
        absolute(value[key])
    providers = value['providers']
    need(isinstance(providers, dict) and set(providers) == set(PROVIDERS), 'CONFIG_PROVIDER_SET')
    for item in providers.values():
        need(isinstance(item, dict) and set(item) == {'origin'} and isinstance(item['origin'], str), 'CONFIG_PROVIDER')
    return value, sha(raw)


def bundle_bytes(root):
    result = {}
    with directory(absolute(root) / 'deployment') as parent:
        for path, (length, digest) in PINS.items():
            raw, _ = read_file(parent, path.split('/')[-1], length)
            need(len(raw) == length and sha(raw) == digest, 'SOURCE_PIN_MISMATCH')
            result[path] = raw
    return result


@contextmanager
def approved_modules(blobs):
    """No sys.path transplant: the sole absolute deployment import is prebound."""
    need(set(blobs) == set(PINS) and all((len(blobs[p]), sha(blobs[p])) == PINS[p] for p in PINS), 'SOURCE_PIN_MISMATCH')
    need(not any(k == 'deployment' or k.startswith('deployment.') for k in sys.modules), 'IMPORT_COLLISION')
    loaded = {}
    try:
        for name, path in [('deployment', 'deployment/__init__.py'), ('deployment.paths', 'deployment/paths.py'),
                           ('deployment.operational', 'deployment/operational.py')]:
            module = types.ModuleType(name)
            module.__file__ = '<immutable-2a>/' + path
            module.__package__ = 'deployment'
            if name == 'deployment':
                module.__path__ = []
            sys.modules[name] = loaded[name] = module
            exec(compile(blobs[path], module.__file__, 'exec'), module.__dict__)
        operational = loaded['deployment.operational']
        need(operational.PROVIDERS == GETS and operational.HTTP_SECONDS == 3 and operational.HTTP_BYTES == 256 * 1024,
             'SOURCE_CONTRACT')
        yield operational
    finally:
        for name, module in loaded.items():
            if sys.modules.get(name) is module:
                del sys.modules[name]


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def timestamp(value):
    need(isinstance(value, str) and re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z', value), 'RECEIPT_TIMESTAMP')
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    need(1970 <= result.year <= 2100, 'RECEIPT_TIMESTAMP')
    return result


def project(name, value):
    # Discard model names, device names, URLs and arbitrary provider keys.
    if name == 'ollama':
        result = {k: v for k, v in value.items() if k in STATS[name]}
    else:
        devices = value['devices']
        result = {k: value[k] for k in ('ramTotalBytes', 'ramFreeBytes')}
        result.update(deviceCount=len(devices), cudaDeviceCount=sum(d['type'] == 'cuda' for d in devices))
        cuda = [d for d in devices if d['type'] == 'cuda']
        result.update(vramTotalBytes=sum(d['vram_total'] for d in cuda), vramFreeBytes=sum(d['vram_free'] for d in cuda))
    if not all(type(v) is int and 0 <= v <= 2**60 for v in result.values()):
        raise ValueError('Invalid bounded numeric projection')
    return result


def validate_receipt(value):
    need(isinstance(value, dict) and set(value) == {'format', 'sourceRevision', 'configSha256', 'sampleId', 'startedAt', 'checkedAt', 'providers', 'time', 'kind', 'level'}, 'RECEIPT_FORMAT')
    need(value['format'] == FORMAT and value['sourceRevision'] == SOURCE_REVISION
         and isinstance(value['configSha256'], str) and re.fullmatch('[a-f0-9]{64}', value['configSha256'])
         and isinstance(value['sampleId'], str) and re.fullmatch('[a-f0-9]{32}', value['sampleId']), 'RECEIPT_IDENTITY')
    start, end = timestamp(value['startedAt']), timestamp(value['checkedAt'])
    need(0 <= (end-start).total_seconds() <= MAX_PROBE_SECONDS + 1, 'RECEIPT_DURATION')
    need(isinstance(value['providers'], list) and len(value['providers']) == 2
         and [v.get('id') for v in value['providers'] if isinstance(v, dict)] == list(PROVIDERS), 'RECEIPT_PROVIDER_SET')
    for row in value['providers']:
        need(set(row) == {'id', 'checkedAt', 'availability', 'failure', 'durationMs', 'stats'}
             and row['availability'] in ('reachable', 'unavailable') and row['failure'] in FAILURES
             and type(row['durationMs']) is int and 0 <= row['durationMs'] <= MAX_PROBE_SECONDS * 1000
             and start <= timestamp(row['checkedAt']) <= end, 'RECEIPT_PROVIDER')
        stats = row['stats']
        need(isinstance(stats, dict) and set(stats) <= STATS[row['id']]
             and all(type(v) is int and 0 <= v <= 2**60 for v in stats.values()), 'RECEIPT_STATS')
        if row['availability'] == 'unavailable':
            need(row['failure'] != 'none' and stats == {}, 'RECEIPT_FAILURE')
        else:
            required = {'loadedModels', 'availableModels'} if row['id'] == 'ollama' else {'ramTotalBytes', 'ramFreeBytes', 'deviceCount', 'cudaDeviceCount', 'vramTotalBytes', 'vramFreeBytes'}
            need(row['failure'] == 'none' and required <= stats.keys(), 'RECEIPT_SUCCESS')
            if row['id'] == 'ollama':
                need(stats['loadedModels'] <= 256 and stats['availableModels'] <= 256, 'RECEIPT_STATS')
            else:
                need(stats['ramFreeBytes'] <= stats['ramTotalBytes'] and stats['vramFreeBytes'] <= stats['vramTotalBytes']
                     and stats['cudaDeviceCount'] <= stats['deviceCount'] <= 8, 'RECEIPT_STATS')
    need(value['time'] == value['checkedAt'] and value['kind'] == 'provider_health'
         and value['level'] == ('error' if any(row['availability'] == 'unavailable' for row in value['providers']) else 'info'),
         'RECEIPT_EVENT_ENVELOPE')
    need(len(encode(value)) <= MAX_LINE, 'RECEIPT_LIMIT')
    return value


def probe(config, config_hash, operational):
    started, clock = stamp(), time.monotonic()
    rows = []
    for name in PROVIDERS:
        begin = time.monotonic()
        try:
            need(begin-clock < MAX_PROBE_SECONDS, 'PROBE_DEADLINE')
            values = [operational.fetch_json(config['providers'][name]['origin'], path) for path in GETS[name]]
            need(time.monotonic()-clock < MAX_PROBE_SECONDS, 'PROBE_DEADLINE')
            stats = project(name, operational.provider_sample(name, values))
            availability, failure = 'reachable', 'none'
        except Exception as error:
            availability, stats = 'unavailable', {}
            failure = ('deadline' if isinstance(error, Blocked) and str(error) == 'PROBE_DEADLINE' else 'timeout' if isinstance(error, TimeoutError)
                       else 'connection_failed' if isinstance(error, OSError) else 'invalid_response')
        rows.append(dict(id=name, checkedAt=stamp(), availability=availability, failure=failure,
                         durationMs=min(MAX_PROBE_SECONDS * 1000, max(0, round((time.monotonic()-begin)*1000))), stats=stats))
    checked = stamp()
    return validate_receipt(dict(format=FORMAT, sourceRevision=SOURCE_REVISION, configSha256=config_hash,
                                 sampleId=uuid.uuid4().hex, startedAt=started, checkedAt=checked, providers=rows,
                                 time=checked, kind='provider_health',
                                 level='error' if any(row['availability'] == 'unavailable' for row in rows) else 'info'))


def output_files(fd, gid):
    allowed = {*LOGS, 'latest.json', 'probe.lock'}
    need(set(os.listdir(fd)) <= allowed, 'FOREIGN_OUTPUT_ENTRY')
    result = {}
    for name in sorted(os.listdir(fd)):
        mode = 0o640 if name in LOGS else 0o600
        raw, info = read_file(fd, name, MAX_LOG if name in LOGS else MAX_LINE if name == 'latest.json' else 1,
                              mode=mode, gid=gid if name in LOGS else 0)
        if name in LOGS and raw:
            need(raw.endswith(b'\n'), 'INCOMPLETE_LOG')
            for line in raw.splitlines():
                need(len(line) <= MAX_LINE, 'RECEIPT_LIMIT')
                validate_receipt(decode(line))
        if name == 'latest.json':
            validate_receipt(decode(raw))
        result[name] = (raw, signature(info))
    if 'latest.json' in result:
        need('health.log' in result and result['health.log'][0].splitlines()[-1:] == result['latest.json'][0].splitlines(),
             'RECEIPT_LOG_DIVERGENCE')
    else:
        need(not any(result.get(name, (b'',))[0] for name in LOGS), 'MISSING_COMMITTED_RECEIPT')
    return result


def output_directory(fd, gid):
    info = os.fstat(fd)
    need(info.st_uid == 0 and info.st_gid == gid and stat.S_IMODE(info.st_mode) == 0o750, 'OUTPUT_DIRECTORY_MODE')


@contextmanager
def locked(fd, *, create):
    import fcntl
    created = False
    try:
        lock = os.open('probe.lock', os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=fd) if create else os.open('probe.lock', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        created = create
    except FileExistsError:
        lock = os.open('probe.lock', os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    try:
        if created:
            os.fchown(lock, 0, 0)
            os.fchmod(lock, 0o600)
        info = os.fstat(lock)
        need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == info.st_gid == 0
             and stat.S_IMODE(info.st_mode) == 0o600 and info.st_size <= 1, 'UNTRUSTED_LOCK')
        fcntl.flock(lock, (fcntl.LOCK_EX if create else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        yield
    finally:
        os.close(lock)


def cas(fd, name, expected):
    try:
        actual = signature(os.stat(name, dir_fd=fd, follow_symlinks=False))
    except FileNotFoundError:
        actual = None
    need(actual == expected, 'OUTPUT_CHANGED')


def write_all(fd, data):
    while data:
        length = os.write(fd, data)
        need(length > 0, 'WRITE_FAILED')
        data = data[length:]


def publish(fd, gid, value, previous):
    raw = encode(validate_receipt(value))
    for name, (_, identity) in previous.items():
        cas(fd, name, identity)
    current = previous.get('health.log', (b'', None))
    if len(current[0]) + len(raw) > MAX_LOG:
        # Only already-validated artifacts in this dedicated root-owned directory.
        if LOGS[-1] in previous:
            os.unlink(LOGS[-1], dir_fd=fd)
        for index in range(len(LOGS)-2, -1, -1):
            if LOGS[index] in previous:
                os.rename(LOGS[index], LOGS[index+1], src_dir_fd=fd, dst_dir_fd=fd)
        current = (b'', None)
    flags = os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK
    if current[1] is None:
        flags |= os.O_CREAT | os.O_EXCL
    stream = os.open('health.log', flags, 0o600, dir_fd=fd)
    try:
        if current[1] is None:
            os.fchown(stream, 0, gid)
            os.fchmod(stream, 0o640)
        else:
            need(signature(os.fstat(stream)) == current[1], 'OUTPUT_CHANGED')
        write_all(stream, raw)
        os.fsync(stream)
    finally:
        os.close(stream)
    temporary = '.latest-' + uuid.uuid4().hex + '.tmp'
    stream = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
    try:
        os.fchown(stream, 0, 0)
        os.fchmod(stream, 0o600)
        write_all(stream, raw)
        os.fsync(stream)
    finally:
        os.close(stream)
    try:
        cas(fd, 'latest.json', previous.get('latest.json', (None, None))[1])
        os.replace(temporary, 'latest.json', src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=fd)
        except FileNotFoundError:
            pass


def audit(value, config_hash, now=None):
    validate_receipt(value)
    need(value['configSha256'] == config_hash, 'RECEIPT_CONFIG_MISMATCH')
    current = now or datetime.now(timezone.utc)
    ages = [(current-timestamp(v)).total_seconds() for v in [value['startedAt'], value['checkedAt'], *[r['checkedAt'] for r in value['providers']]]]
    need(all(0 <= age <= MAX_AGE for age in ages), 'RECEIPT_STALE_OR_FUTURE')
    need(all(row['availability'] == 'reachable' and row['failure'] == 'none' for row in value['providers']), 'PROVIDER_UNAVAILABLE')
    return round(max(ages), 3)


def main(argv=None):
    try:
        need(sys.flags.isolated and sys.dont_write_bytecode, 'ISOLATED_PYTHON_REQUIRED')
        need(sys.platform == 'linux' and os.geteuid() == 0, 'ROOT_LINUX_REQUIRED')
        parser = Parser(description=__doc__)
        parser.add_argument('action', choices=('sample', 'check'), nargs='?', default='sample')
        parser.add_argument('--config', required=True)
        parser.add_argument('--apply', action='store_true')
        args = parser.parse_args(argv)
        need(not args.apply or args.action == 'sample', 'APPLY_SAMPLE_ONLY')
        # Validate executable ownership too; its exact Git SHA is an operator staging check.
        with directory(Path(__file__).absolute().parent) as parent:
            read_file(parent, Path(__file__).name, 64*1024)
        config, digest = configuration(args.config)
        blobs = bundle_bytes(config['bundleRoot'])
        with approved_modules(blobs) as operational:
            for item in config['providers'].values():
                operational.origin(item['origin'])
            with directory(config['outputDirectory']) as fd:
                output_directory(fd, config['readerGid'])
                # Refuse foreign/unsafe artifacts before even creating a lock.
                output_files(fd, config['readerGid'])
                if args.action == 'sample' and not args.apply:
                    print(json.dumps({'status': 'PLAN', 'providerIds': list(PROVIDERS), 'getRequests': 3,
                                      'sourceRevision': SOURCE_REVISION, 'servicesChanged': False}))
                    return 0
                with locked(fd, create=args.action == 'sample'):
                    previous = output_files(fd, config['readerGid'])
                    if args.action == 'check':
                        need('latest.json' in previous, 'RECEIPT_MISSING')
                        age = audit(decode(previous['latest.json'][0]), digest)
                        print(json.dumps({'status': 'AVAILABLE', 'maximumAgeSeconds': age, 'providerIds': list(PROVIDERS),
                                          'applicationProviderPolicyQualified': False}))
                        return 0
                    value = probe(config, digest, operational)
                    publish(fd, config['readerGid'], value, previous)
                    good = all(row['availability'] == 'reachable' for row in value['providers'])
                    print(json.dumps({'status': 'SAMPLED' if good else 'UNAVAILABLE', 'providerIds': list(PROVIDERS),
                                      'applicationProviderPolicyQualified': False}))
                    return 0 if good else 1
    except Exception as error:
        code = str(error) if isinstance(error, Blocked) else 'IO_OR_VALIDATION_FAILURE'
        print(json.dumps({'status': 'BLOCKED', 'code': code, 'applicationProviderPolicyQualified': False}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
