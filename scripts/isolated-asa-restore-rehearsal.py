#!/usr/bin/env python3
"""Rehearse an owner-decrypted ASA backup in fresh network-none containers.

Uses the reviewed ASA repository's restore_check through an isolated exec-only
adapter. Never addresses the running installation's Docker project or database.
Plan by default; source snapshot and input receipts must be root-private/trusted.
"""
import argparse
import datetime as dt
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid

STATE = Path('/var/lib/asa-restore-rehearsal')
SOURCE_FILES = {'__init__.py', 'backup.py', 'contracts.py', 'system.py'}
LABEL = 'org.asa-lab.isolated-rehearsal'


def require(value, code):
    if not value:
        raise RuntimeError(code)


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(data)
    return h.hexdigest()


def private_file(path):
    path = Path(path)
    require(path.resolve() == path and path.is_file(), 'PRIVATE_FILE_PATH')
    st = path.stat()
    require(st.st_uid == 0 and st.st_mode & 0o077 == 0, 'PRIVATE_FILE_MODE')
    return path


def trusted_tree(root):
    root = Path(root)
    require(root.resolve() == root and root.is_dir(), 'SOURCE_PATH')
    for parent in [root, *root.parents]:
        require(parent.stat().st_uid == 0 and parent.stat().st_mode & 0o022 == 0, 'SOURCE_PARENT_TRUST')
    receipt = json.loads(private_file(root / 'source-receipt.json').read_text())
    require(receipt.get('repository') == 'spikeal8-maker/asa-lab'
            and re.fullmatch('[a-f0-9]{40}', receipt.get('commit', '')), 'SOURCE_IDENTITY')
    require(set(receipt.get('files', {})) == SOURCE_FILES, 'SOURCE_MODULE_SET')
    require({p.name for p in root.iterdir()} == SOURCE_FILES | {'source-receipt.json'}, 'SOURCE_EXTRA_MODULE')
    for name, digest in receipt['files'].items():
        require(sha(private_file(root / name)) == digest, 'SOURCE_HASH')
    return receipt


def load_canonical(root, nonce):
    receipt = trusted_tree(root)
    package = 'asa_reviewed_rehearsal_' + nonce
    spec = importlib.util.spec_from_file_location(package, Path(root) / '__init__.py',
                                                submodule_search_locations=[str(root)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[package] = module
    old = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
        backup = __import__(package + '.backup', fromlist=['restore_check'])
    finally:
        sys.dont_write_bytecode = old
    return backup, receipt


def validate_members(members, limit=30 * 1024**3):
    seen = set()
    total = 0
    for member in members:
        name = member.name.rstrip('/')
        path = PurePosixPath(name)
        require(name and not path.is_absolute() and '..' not in path.parts
                and '\\' not in name and ':' not in name and all(ord(c) >= 32 for c in name) and str(path) == name
                and path.parts[0] == 'asa-backup', 'TAR_PATH')
        require(name not in seen and (member.isfile() or member.isdir()), 'TAR_TYPE_OR_DUPLICATE')
        require(member.size >= 0 and not getattr(member, 'sparse', None), 'TAR_SPARSE')
        seen.add(name)
        total += member.size
        require(total <= limit, 'TAR_SIZE_LIMIT')
    require('asa-backup/manifest.json' in seen, 'TAR_MANIFEST')


def unpack(bundle, destination):
    with tarfile.open(bundle, 'r:') as tar:
        members = tar.getmembers()
        validate_members(members)
        for member in members:
            target = destination.joinpath(*PurePosixPath(member.name.rstrip('/')).parts)
            if member.isdir():
                target.mkdir(mode=0o700, parents=True, exist_ok=True)
            else:
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with target.open('xb') as output, tar.extractfile(member) as source:
                    shutil.copyfileobj(source, output)
                target.chmod(0o600)
    return destination / 'asa-backup'


def validate_inputs(c, proof, last, bundle, archive):
    require(c['hostname'] == socket.gethostname(), 'HOST_IDENTITY')
    identity = json.loads((Path(c['root']) / '.asa/installation.json').read_text())
    require(identity.get('instanceId') == c['installation_instance_id']
            == last.get('installation_instance_id') == proof.get('installation_instance_id'), 'GENERATION')
    require(last.get('status') == 'success' and last.get('backup_verified') is True
            and last.get('cloud_roundtrip_verified') is True, 'BACKUP_NOT_ACCEPTED')
    require(proof.get('decryption_verified') is True, 'DECRYPTION_NOT_VERIFIED')
    require(re.fullmatch('[a-f0-9]{64}', last.get('sha256', ''))
            and last['sha256'] == proof.get('archive_sha256') == sha(archive), 'ARCHIVE_HASH')
    require(proof.get('plaintext_sha256') == sha(bundle), 'PLAINTEXT_HASH')


class Docker:
    def __init__(self, nonce, log):
        self.nonce, self.log = nonce, log
        self.containers, self.volumes = [], []

    def run(self, args, *, data=None, input_file=None, capture=True, timeout=120):
        result = subprocess.run(['docker', *args], input=data, stdin=input_file,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                timeout=timeout)
        if result.returncode:
            self.log.write(result.stderr)
            self.log.flush()
            raise RuntimeError('ISOLATED_DOCKER_STEP_FAILED_INSPECT_PRIVATE_LOG')
        return result.stdout.decode('utf-8', errors='replace').strip() if capture else result.stdout

    def inspect(self, name, kind='container'):
        return json.loads(self.run([kind, 'inspect', name]))[0]

    def volume(self, name):
        # Docker create is idempotent; matching label is checked before use/removal.
        self.run(['volume', 'create', '--label', LABEL + '=' + self.nonce, name])
        require(self.inspect(name, 'volume').get('Labels', {}).get(LABEL) == self.nonce, 'VOLUME_IDENTITY')
        self.volumes.append(name)

    def create(self, name, image, volume, target, envfile, user, command, *, entrypoint=None, readonly_data=False):
        require(re.fullmatch('sha256:[a-f0-9]{64}', image), 'PINNED_IMAGE_REQUIRED')
        self.run(['image', 'inspect', image])  # No pull; only the backed-up local image.
        args = ['create', '--name', name, '--label', LABEL + '=' + self.nonce,
            '--network', 'none', '--read-only', '--cap-drop', 'ALL',
            '--security-opt', 'no-new-privileges:true', '--memory', '2g', '--cpus', '2',
            '--pids-limit', '256', '--user', user, '--env-file', str(envfile),
            '--mount', 'type=volume,source=' + volume + ',target=' + target + (',readonly' if readonly_data else ''),
            '--tmpfs', '/tmp:rw,nosuid,size=256m,mode=1777',
            '--tmpfs', '/var/run/postgresql:rw,nosuid,size=16m,mode=1777']
        if entrypoint:
            args += ['--interactive', '--entrypoint', entrypoint]
        cid = self.run([*args, image, *command])
        require(re.fullmatch('[a-f0-9]{64}', cid), 'CONTAINER_ID_FORMAT')
        self.containers.append(cid)
        row = self.inspect(cid)
        require(row['Config'].get('Labels', {}).get(LABEL) == self.nonce
                and row['Image'] == image, 'CONTAINER_IDENTITY')
        h = row['HostConfig']
        require(h['NetworkMode'] == 'none' and not h.get('PortBindings')
                and not h.get('PublishAllPorts') and not h.get('Privileged')
                and h.get('ReadonlyRootfs') is True and not h.get('Binds')
                and 'ALL' in h.get('CapDrop', []) and not h.get('CapAdd'), 'CONTAINER_ISOLATION')
        data_mounts = [m for m in row['Mounts'] if m['Type'] == 'volume']
        require(len(data_mounts) == 1 and data_mounts[0]['Name'] == volume
                and data_mounts[0]['Destination'] == target
                and (not readonly_data or data_mounts[0]['RW'] is False)
                and all(m['Type'] == 'volume' or (m['Type'] == 'tmpfs'
                    and m['Destination'] in ('/tmp', '/var/run/postgresql'))
                    for m in row['Mounts']), 'CONTAINER_MOUNT')
        return cid

    def cleanup(self):
        for cid in reversed(self.containers):
            row = self.inspect(cid)
            require(row['Id'] == cid and row['Config'].get('Labels', {}).get(LABEL) == self.nonce,
                    'CLEANUP_CONTAINER_IDENTITY')
            self.run(['rm', '-f', cid])
        for name in reversed(self.volumes):
            require(self.inspect(name, 'volume').get('Labels', {}).get(LABEL) == self.nonce,
                    'CLEANUP_VOLUME_IDENTITY')
            self.run(['volume', 'rm', name])


class IsolatedInstallation:
    """Only canonical restore_check's exec-to-postgres operations are supported."""
    def __init__(self, docker, cid, admin):
        self.docker, self.cid = docker, cid
        self.env = {'POSTGRES_USER': admin, 'POSTGRES_DB': 'bootstrap'}

    def identity(self):
        row = self.docker.inspect(self.cid)
        require(row['Config'].get('Labels', {}).get(LABEL) == self.docker.nonce, 'PG_IDENTITY')
        return {'postgres': {'id': self.cid}}

    def compose(self, *args, **kwargs):
        values = list(args)
        require(values[:2] == ['exec', '-T'], 'COMPOSE_VERB_FORBIDDEN')
        values = values[2:]
        flags = []
        while values and values[0] == '-e':
            require(len(values) >= 2 and values[1].split('=', 1)[0]
                    in ('RESTORE_DATABASE', 'CHECK_SQL'), 'COMPOSE_ENV_FORBIDDEN')
            flags += values[:2]
            values = values[2:]
        require(values and values.pop(0) == 'postgres' and values
                and values[0] in ('sh', 'psql', 'pg_restore', 'rm'), 'COMPOSE_SERVICE_FORBIDDEN')
        self.identity()
        require(set(kwargs) <= {'input_data', 'input_file', 'capture'}, 'COMPOSE_KWARGS_FORBIDDEN')
        data = kwargs.get('input_data')
        return self.docker.run(['exec', '-i', *flags, self.cid, *values],
            data=data.encode() if isinstance(data, str) else data,
            input_file=kwargs.get('input_file'), timeout=900)


def write_env(path, values):
    require(all(re.fullmatch('[A-Za-z_][A-Za-z0-9_]*', key) and '\n' not in value for key, value in values.items()), 'ENV_FORMAT')
    with path.open('x') as stream:
        stream.write(''.join(key + '=' + value + '\n' for key, value in values.items()))
    path.chmod(0o600)


def wait_ready(docker, cid, command):
    deadline = time.monotonic() + 90
    while True:
        try:
            docker.run(['exec', cid, *command], timeout=10)
            return
        except RuntimeError:
            require(time.monotonic() < deadline, 'ISOLATED_STARTUP_TIMEOUT')
            time.sleep(1)


def rehearse(c, bundle, source, stage, log):
    nonce = uuid.uuid4().hex
    canonical, source_receipt = load_canonical(source, nonce)
    export = unpack(bundle, stage)
    manifest = canonical.verify_backup(export)
    require(manifest.get('project') == c['project'] and manifest.get('profile') == c['profile'], 'BACKUP_TARGET')
    docker = Docker(nonce, log)
    admin = 'audit_' + nonce[:12]
    s3user, s3secret = 'audit' + nonce[:12], secrets.token_hex(24)
    pg_env, minio_env = stage / 'pg.env', stage / 'minio.env'
    write_env(pg_env, {'POSTGRES_USER': admin, 'POSTGRES_PASSWORD': secrets.token_hex(24), 'POSTGRES_DB': 'bootstrap'})
    write_env(minio_env, {'MINIO_ROOT_USER': s3user, 'MINIO_ROOT_PASSWORD': s3secret,
        'MINIO_BROWSER': 'off', 'MC_CONFIG_DIR': '/tmp/.mc',
        'MC_HOST_audit': 'http://' + s3user + ':' + s3secret + '@127.0.0.1:9000'})
    result = None
    try:
        pgvolume, objectvolume = 'asa-rehearsal-pg-' + nonce, 'asa-rehearsal-objects-' + nonce
        docker.volume(pgvolume)
        docker.volume(objectvolume)
        pg = docker.create('asa-rehearsal-pg-' + nonce, manifest['images']['postgres']['imageId'],
            pgvolume, '/var/lib/postgresql/data', pg_env, 'postgres', ['postgres'])
        docker.run(['start', pg])
        wait_ready(docker, pg, ['pg_isready', '-U', admin, '-d', 'bootstrap'])
        with (export / 'globals.sql').open('rb') as stream:
            docker.run(['exec', '-i', pg, 'psql', '-X', '-v', 'ON_ERROR_STOP=1', '-U', admin,
                        '-d', 'bootstrap'], input_file=stream)
        install = IsolatedInstallation(docker, pg, admin)
        database = 'rehearsal_' + nonce[:12] + '_test'
        canonical.restore_check(install, export, database)
        def query(sql):
            return docker.run(['exec', '-i', pg, 'psql', '-XAt', '-v', 'ON_ERROR_STOP=1',
                '-U', admin, '-d', database], data=sql.encode())
        counts = json.loads(query("SELECT json_build_object('accounts',(SELECT count(*) FROM public.accounts),"
                                 "'projects',(SELECT count(*) FROM public.projects),"
                                 "'schema',(SELECT max(version::integer) FROM public.schema_migrations));"))
        require(counts['schema'] == manifest['runtime']['schema'] and counts['accounts'] > 0,
                'RESTORED_SCHEMA_OR_ACCOUNTS')
        # The private parent remains root0700. These children are accessible only
        # after Docker copies them into the new exclusively owned data volume.
        objects = export / 'objects'
        require(objects.is_dir() and not objects.is_symlink(), 'OBJECTS_DIRECTORY')
        for path in [objects, *objects.rglob('*')]:
            require(not path.is_symlink(), 'OBJECT_SYMLINK')
            os.chown(path, 1000, 1000)
        minio = docker.create('asa-rehearsal-minio-' + nonce, manifest['images']['minio']['imageId'],
            objectvolume, '/data', minio_env, '1000:1000', ['server', '/data'])
        docker.run(['cp', '-a', str(objects) + '/.', minio + ':/data'])
        # Verify copied objects before the isolated server writes its own metadata.
        checks = []
        for relative, expected in manifest['files'].items():
            if relative.startswith('objects/'):
                checks.append(expected['sha256'] + '  ' + relative[len('objects/'):])
        require(checks, 'NO_OBJECTS_VERIFIED')
        helper = docker.create('asa-rehearsal-hashes-' + nonce, manifest['images']['minio']['imageId'],
            objectvolume, '/data', minio_env, '1000:1000', ['-eu', '-c', 'cd /data; sha256sum -c -'],
            entrypoint='/bin/sh', readonly_data=True)
        docker.run(['start', '-a', '-i', helper], data=('\n'.join(checks) + '\n').encode(), timeout=300)
        require(docker.inspect(helper)['State']['ExitCode'] == 0, 'RESTORED_OBJECT_HASH')
        docker.run(['start', minio])
        wait_ready(docker, minio, ['curl', '-fsS', 'http://127.0.0.1:9000/minio/health/live'])
        listing = docker.run(['exec', minio, 'mc', 'ls', '--json', 'audit'])
        buckets = [json.loads(line) for line in listing.splitlines() if line]
        require(any(row.get('status') == 'success' and row.get('type') == 'folder' for row in buckets), 'S3_BUCKET_LIST')
        # List and read one actual object without printing its private key/content.
        samples = 0
        for bucket in buckets:
            if bucket.get('type') != 'folder':
                continue
            rows = docker.run(['exec', minio, 'mc', 'ls', '--recursive', '--json', 'audit/' + bucket['key']])
            for line in rows.splitlines():
                row = json.loads(line)
                if row.get('type') == 'file' and 0 < row.get('size', 0) <= 5 * 1024**2:
                    content = docker.run(['exec', minio, 'mc', 'cat', 'audit/' + bucket['key'].rstrip('/') + '/' + row['key']], capture=False)
                    require(len(content) == row['size'], 'S3_OBJECT_READ')
                    samples += 1
                    break
            if samples:
                break
        require(samples > 0, 'S3_NO_SAMPLE_OBJECT')
        result = {'postgres_restored': True, 'objects_verified': True, 'restored_account_project_checks': True,
                  'counts': counts, 'object_files_verified': len(checks), 's3_samples_read': samples,
                  'canonical_source_commit': source_receipt['commit'],
                  'canonical_manifest_sha256': sha(export / 'manifest.json'),
                  'runtime_revision': manifest['runtime']['revision'], 'source_row_fingerprint_equality': False}
    finally:
        docker.cleanup()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--expected-host')
    parser.add_argument('--bundle', type=Path)
    parser.add_argument('--archive', type=Path)
    parser.add_argument('--decryption-receipt', type=Path)
    parser.add_argument('--source', type=Path)
    parser.add_argument('--receipt', type=Path, default=STATE / 'accepted.json')
    a = parser.parse_args()
    if not a.apply:
        print(json.dumps({'plan': True, 'containers': 'fresh network-none, no ports, only owned volumes',
                          'restore': 'reviewed canonical ASA restore_check via isolated exec adapter',
                          'live_database_access': False, 'timer_enabled': False})); return
    require(os.geteuid() == 0 and a.expected_host == socket.gethostname(), 'ROOT_HOST')
    require(STATE.resolve() == STATE, 'STATE_PATH')
    STATE.mkdir(mode=0o700, exist_ok=True)
    require(STATE.stat().st_uid == 0 and STATE.stat().st_mode & 0o077 == 0, 'STATE_MODE')
    import fcntl
    lock = os.open(STATE / '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    config = json.loads(private_file('/etc/server-settings/asa-nightly-backup.json').read_text())
    last = json.loads(private_file('/var/lib/asa-nightly-backup/last-success.json').read_text())
    proof = json.loads(private_file(a.decryption_receipt).read_text())
    bundle, archive = private_file(a.bundle), private_file(a.archive)
    validate_inputs(config, proof, last, bundle, archive)
    trusted_tree(a.source)
    require(a.receipt.parent == STATE and not a.receipt.exists(), 'RECEIPT_PATH_OR_EXISTS')
    stage = Path(tempfile.mkdtemp(prefix='run-', dir=STATE))
    logpath = STATE / (stage.name + '.log')
    try:
        with logpath.open('xb') as log:
            logpath.chmod(0o600)
            result = rehearse(config, bundle, a.source, stage, log)
        result.update({'hostname': socket.gethostname(), 'installation_instance_id': config['installation_instance_id'],
            'archive_sha256': last['sha256'], 'decryption_verified': True,
            'accepted_at': dt.datetime.now(dt.timezone.utc).isoformat(), 'isolated_resources_removed': True,
            'live_database_changed': False})
        fd = os.open(a.receipt, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(result, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        print(json.dumps({'accepted': True, 'receipt': str(a.receipt), 'timer_enabled': False,
                          'live_database_changed': False}))
    finally:
        require(stage.parent == STATE and stage.name.startswith('run-') and not stage.is_symlink(), 'CLEANUP_STAGE')
        shutil.rmtree(stage)


if __name__ == '__main__':
    main()
