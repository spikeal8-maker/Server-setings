#!/usr/bin/env python3
"""Plan/apply a host-scoped Traefik capacity profile; never restart a proxy."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
import uuid

try:
    import yaml
except ImportError:
    yaml = None


class Blocked(ValueError):
    pass


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(path):
    """Check lexical components before resolve can hide a link followed by '..'."""
    path = Path(path).absolute()
    for node in (path, *path.parents):
        try:
            metadata = node.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, 'st_file_attributes', 0) & 0x400:
            raise Blocked('symlink/reparse paths are not supported')
    return path.resolve(strict=False)


def regular(path):
    path = canonical(path)
    s = path.stat()
    if not stat.S_ISREG(s.st_mode) or s.st_nlink != 1 or s.st_size > 1024 * 1024:
        raise Blocked('require one bounded regular source file')
    return path, s, path.read_bytes()


def parse(raw):
    if yaml is None:
        raise Blocked('install the pinned requirements-ingress.txt; no file changed')
    class UniqueLoader(yaml.SafeLoader):
        def construct_mapping(self, node, deep=False):
            value = {}
            for key, item in node.value:
                name = self.construct_object(key, deep=deep)
                if not isinstance(name, str) or name in value:
                    raise Blocked('duplicate or non-string YAML key')
                value[name] = self.construct_object(item, deep=deep)
            return value
    # Traefik's Go YAML v3 uses YAML 1.2 booleans for untyped values. Copy
    # resolvers before overriding so the shared PyYAML loader is never mutated.
    UniqueLoader.yaml_implicit_resolvers = {
        key: [(tag, pattern) for tag, pattern in entries if tag != 'tag:yaml.org,2002:bool']
        for key, entries in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }
    UniqueLoader.add_implicit_resolver('tag:yaml.org,2002:bool', re.compile(r'^(?:true|True|TRUE|false|False|FALSE)$'), list('tTfF'))
    try:
        if any(isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken)) for token in yaml.scan(raw)):
            raise Blocked('YAML aliases/anchors require explicit migration, not rewriting')
        data = yaml.load(raw, Loader=UniqueLoader)
    except yaml.YAMLError as exc:
        mark = getattr(exc, 'problem_mark', None)
        location = f' at line {mark.line + 1}, column {mark.column + 1}' if mark else ''
        raise Blocked(f'invalid YAML ({type(exc).__name__}){location}; input omitted') from None
    if not isinstance(data, dict):
        raise Blocked('Traefik source must be a mapping')
    return data


def private_regular(path):
    path, metadata, raw = regular(path)
    parent = path.parent.stat()
    if os.name != 'nt' and (metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) & 0o077
                           or parent.st_uid != os.geteuid() or stat.S_IMODE(parent.st_mode) & 0o077):
        raise Blocked('rollback artifacts must belong to operator inside a private directory')
    return path, metadata, raw


def public_budget(definition):
    """Never report Redis credentials, source headers or arbitrary config values."""
    rate = definition['rateLimit']
    period = rate.get('period')
    return {'average': rate.get('average') if type(rate.get('average')) is int else None,
            'burst': rate.get('burst') if type(rate.get('burst')) is int else None,
            'period': period if isinstance(period, str) and len(period) <= 64
            and re.fullmatch(r'(?:[0-9]+(?:\.[0-9]+)?(?:ns|us|µs|ms|s|m|h))+', period) else None,
            'hasSourceCriterion': 'sourceCriterion' in rate, 'hasRedis': 'redis' in rate}


def render(raw, host, router, old_middleware, average=6000, burst=2000):
    if not re.fullmatch(r'(?=.{1,253}$)[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', host) or '..' in host:
        raise Blocked('provide one lowercase DNS host without scheme, port or wildcard')
    if type(average) is not int or type(burst) is not int or not 600 <= average <= 12000 or not 300 <= burst <= 5000:
        raise Blocked('capacity profile outside reviewed bounds')
    before = parse(raw)
    data = copy.deepcopy(before)
    http = data.get('http', {})
    routes = http.get('routers', {})
    route = routes.get(router)
    if not isinstance(route, dict) or route.get('rule') != f'Host(`{host}`)' or not isinstance(route.get('tls'), dict):
        raise Blocked('router must be an exact TLS Host rule for this installation')
    middlewares = route.get('middlewares')
    if not isinstance(middlewares, list) or not all(isinstance(x, str) for x in middlewares):
        raise Blocked('router middleware chain is invalid')
    definitions = http.get('middlewares', {})
    previous = definitions.get(old_middleware)
    if not isinstance(previous, dict) or set(previous) != {'rateLimit'} or not isinstance(previous['rateLimit'], dict):
        raise Blocked('predecessor must be only a rate limiter, never an auth/header chain')
    owned = 'asa-capacity-100-' + hashlib.sha256(host.encode()).hexdigest()[:12]
    if (average, burst) != (6000, 2000):
        owned = f'{owned}-{average}-{burst}'
    if old_middleware == owned:
        raise Blocked('predecessor and requested profile must differ')
    wanted = copy.deepcopy(previous)
    wanted['rateLimit'].update(average=average, period='1m', burst=burst)
    if owned in definitions and definitions[owned] != wanted:
        raise Blocked('owned middleware already differs; require a new reviewed profile')
    if middlewares.count(owned) == 1 and old_middleware not in middlewares:
        if definitions.get(owned) != wanted:
            raise Blocked('incomplete owned middleware state')
        return raw, {'changed': False, 'router': router, 'host': host, 'middleware': owned, 'profile': public_budget(wanted)}
    if middlewares.count(old_middleware) != 1 or owned in middlewares:
        raise Blocked('expected predecessor must occur exactly once')
    # Do not mutate a shared predecessor: test2/other routers retain their budgets.
    definitions[owned] = wanted
    route['middlewares'] = [owned if x == old_middleware else x for x in middlewares]
    encoded = (json.dumps(data, ensure_ascii=False, indent=2) + '\n').encode()
    if len(encoded) > 1024 * 1024:
        raise Blocked('rendered configuration exceeds bound')
    return encoded, {'changed': True, 'router': router, 'host': host, 'middleware': owned,
                     'profile': public_budget(wanted), 'previous': public_budget(previous), 'unrelatedRoutersPreserved': True}


def sync_directory(path):
    if os.name != 'nt':
        directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def durable_mkdir(path):
    missing = []
    node = path
    while not node.exists():
        missing.append(node)
        node = node.parent
    for node in reversed(missing):
        node.mkdir(mode=0o700)
        sync_directory(node)
        sync_directory(node.parent)


def atomic(path, raw, metadata):
    temp = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.pending')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != 'nt':
            os.chown(temp, metadata.st_uid, metadata.st_gid)
        os.chmod(temp, stat.S_IMODE(metadata.st_mode))
        os.replace(temp, path)
        sync_directory(path.parent)
    finally:
        temp.unlink(missing_ok=True)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--route-file', type=Path, required=True)
    p.add_argument('--host', required=True)
    p.add_argument('--router', required=True)
    p.add_argument('--old-middleware', default='asa-stage-rate')
    p.add_argument('--average', type=int, default=6000, help='Per source IP requests/minute within reviewed bounds')
    p.add_argument('--burst', type=int, default=2000)
    p.add_argument('--expected-sha256')
    p.add_argument('--backup-directory', type=Path)
    p.add_argument('--restore', type=Path, help='Receipt from this tool; verifies exact current SHA before restoring')
    p.add_argument('--apply', action='store_true')
    args = p.parse_args(argv)
    path, metadata, raw = regular(args.route_file)
    if args.restore:
        receipt_path, _, receipt_raw = private_regular(args.restore)
        receipt = json.loads(receipt_raw)
        if receipt.get('format') != 'asa-ingress-capacity-v1' or receipt.get('target') != str(path):
            raise Blocked('rollback receipt belongs to another target')
        backup, _, restored = private_regular(receipt_path.parent / 'before.bin')
        if digest(restored) != receipt['beforeSha256'] or digest(raw) != receipt['afterSha256']:
            raise Blocked('rollback snapshot or current configuration has changed')
        if os.name != 'nt' and (metadata.st_uid != receipt['uid'] or metadata.st_gid != receipt['gid']
                               or stat.S_IMODE(metadata.st_mode) != receipt['mode']):
            raise Blocked('target ownership or mode drifted; do not overwrite unrelated changes')
        encoded = restored
        plan = {'changed': True, 'rollback': True}
    else:
        encoded, plan = render(raw, args.host, args.router, args.old_middleware, args.average, args.burst)
    result = {'mode': 'apply' if args.apply else 'plan', 'beforeSha256': digest(raw),
              'afterSha256': digest(encoded), **plan, 'proxyRestarted': False, 'liveVerification': 'NOT_RUN'}
    if args.apply and encoded != raw:
        if not re.fullmatch(r'[a-f0-9]{64}', args.expected_sha256 or '') or digest(raw) != args.expected_sha256:
            raise Blocked('apply requires the exact freshly reviewed source SHA256')
        if not args.backup_directory:
            raise Blocked('apply requires a private backup directory outside provider watch directory')
        backup = canonical(args.backup_directory)
        if path.parent == backup or path.parent in backup.parents:
            raise Blocked('backup directory must be outside the dynamic provider directory')
        durable_mkdir(backup)
        if os.name != 'nt' and (backup.stat().st_uid != os.geteuid() or stat.S_IMODE(backup.stat().st_mode) & 0o077):
            raise Blocked('backup directory must belong to operator with mode0700')
        operation = backup / (str(time.time_ns()) + '-' + uuid.uuid4().hex)
        durable_mkdir(operation)
        for name, content in [('before.bin', raw), ('receipt.json', json.dumps({'format': 'asa-ingress-capacity-v1',
             'target': str(path), 'beforeSha256': digest(raw), 'afterSha256': digest(encoded), 'host': args.host,
             'router': args.router, 'uid': metadata.st_uid, 'gid': metadata.st_gid,
             'mode': stat.S_IMODE(metadata.st_mode)}).encode())]:
            fd = os.open(operation / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        # Both artifact file contents and all newly-created directory entries
        # must be durable before the watched configuration can be replaced.
        sync_directory(operation)
        sync_directory(backup)
        # Recheck immediately before replacing the watched file; no silent drift overwrite.
        if regular(path)[2] != raw:
            raise Blocked('source changed during planning; backup retained, no apply')
        atomic(path, encoded, metadata)
        result['receipt'] = str(operation / 'receipt.json')
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (Blocked, OSError, ValueError, TypeError, KeyError) as exc:
        reason = str(exc)[:240] if isinstance(exc, Blocked) else f'{type(exc).__name__}; details omitted'
        print(json.dumps({'state': 'BLOCKED', 'reason': reason}))
        raise SystemExit(2)
