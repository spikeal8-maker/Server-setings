#!/usr/bin/env python3
"""Private encrypted backup runner, submitted through the existing MCP job gate.

No live restore, archive deletion or automatic migration is implemented here.
The installer must not enable the timer before an isolated restore is accepted.
"""
import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import socket
import subprocess
import tarfile
import tempfile
import time
import urllib.request
import uuid
from zoneinfo import ZoneInfo

CONFIG = Path('/etc/server-settings/asa-nightly-backup.json')
STATE = Path('/var/lib/asa-nightly-backup')
RECIPIENTS = Path('/etc/server-settings/age-recipients.txt')
CLOUD_HELPER = Path('/opt/server-settings/asa-operations/cloud_storage.py')
API_ENV = Path('/etc/server-settings/asa-operations.env')


def require(value, code):
    if not value:
        raise RuntimeError(code)


def private_file(path):
    path = Path(path)
    require(not path.is_symlink() and path.is_file(), 'PRIVATE_FILE')
    st = path.stat()
    require(st.st_uid == 0 and st.st_mode & 0o077 == 0, 'PRIVATE_FILE_MODE')
    return path


def config():
    c = json.loads(private_file(CONFIG).read_text())
    require(c['hostname'] == socket.gethostname(), 'HOST_IDENTITY')
    root = Path(c['root'])
    require(root.resolve() == root and root.is_dir(), 'CANONICAL_ROOT')
    require(c['installation_id'] == 'asa-test' and c['project'] == 'asa-lab-dev', 'INSTALLATION')
    require(c['profile'] == 'production' and c['owner_uid'] > 0, 'PROFILE_OWNER')
    require(re.fullmatch('[a-f0-9]{32}', c['installation_instance_id']), 'INSTALLATION_GENERATION')
    require(re.fullmatch('[a-z0-9][a-z0-9_-]{0,63}', c['cloud_project']), 'CLOUD_NAMESPACE')
    return c


def private_state():
    require(STATE.resolve() == STATE, 'STATE_SYMLINK')
    STATE.mkdir(mode=0o700, exist_ok=True)
    require(STATE.stat().st_uid == 0 and STATE.stat().st_mode & 0o077 == 0, 'STATE_MODE')


def save(name, value):
    private_state()
    target = STATE / name
    require(not target.is_symlink(), 'STATE_FILE_SYMLINK')
    temporary = STATE / ('.' + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(target)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def rpc(name, arguments):
    values = {}
    for line in private_file(API_ENV).read_text().splitlines():
        if '=' in line and not line.startswith('#'):
            key, value = line.split('=', 1)
            values[key] = value.strip().strip('"\'')
    request = urllib.request.Request('http://127.0.0.1:39446/mcp',
        data=json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                         'params': {'name': name, 'arguments': arguments}}).encode(),
        headers={'Authorization': 'Bearer ' + values['ASA_OPS_TOKEN'], 'Content-Type': 'application/json'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=30) as reply:
            result = json.load(reply)
        content = result['result']['structuredContent']
        require(content['ok'], 'MCP_' + (content.get('error') or {}).get('code', 'FAILED'))
        return content['data']
    except Exception:
        # Raw responses and headers can contain authentication material.
        raise RuntimeError('MCP_REQUEST_FAILED_INSPECT_OPERATION') from None


def schedule(c):
    day = dt.datetime.now(ZoneInfo('Europe/Moscow')).date().isoformat()
    args = {'installation_id': c['installation_id'],
            'command': shlex.join(['/usr/bin/python3', str(Path(__file__).resolve()), 'run', '--apply']),
            'cwd': c['root'], 'timeout_seconds': 7000,
            'idempotency_key': 'nightly-asa-backup-' + day + '-' + c['installation_instance_id']}
    # MCP Store and the manager's own operation.lock prevent ordinary update overlap.
    result = rpc('asa_admin_execute', args)
    opid = result['operation']['operation_id']
    save('scheduled.json', {'operation_id': opid, 'day': day, 'reused': result['reused']})
    while True:
        operation = rpc('asa_operation_get', {'operation_id': opid})
        if operation['status'] not in ('queued', 'running'):
            require(operation['status'] == 'success', 'BACKUP_OPERATION_FAILED')
            return {'operation_id': opid, 'status': 'success'}
        time.sleep(10)


def manager(c, arguments, log):
    import pwd
    owner = pwd.getpwuid(c['owner_uid'])
    command = ['/usr/bin/python3', str(Path(c['root']) / 'tools/asa_manager.py'),
               *arguments, '--root', c['root'], '--profile', c['profile']]
    env = dict(os.environ, HOME=owner.pw_dir)
    process = subprocess.Popen(command, stdout=log, stderr=log, cwd=c['root'], env=env,
        user=owner.pw_uid, group=owner.pw_gid, extra_groups=os.getgrouplist(owner.pw_name, owner.pw_gid),
        start_new_session=True)
    try:
        code = process.wait(timeout=1800)
    except subprocess.TimeoutExpired:
        # SIGINT permits the manager's backup finally-block to resume stopped writers.
        os.killpg(process.pid, signal.SIGINT)
        try:
            process.wait(timeout=360)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        raise RuntimeError('MANAGER_TIMEOUT_INSPECT_MAINTENANCE_NO_AUTOMATIC_RETRY') from None
    require(code == 0, 'MANAGER_FAILED_INSPECT_PRIVATE_LOG')


def cloud():
    # The separately reviewed transport stores tokens privately and supports no deletion.
    require(CLOUD_HELPER.resolve() == CLOUD_HELPER and CLOUD_HELPER.stat().st_uid == 0
            and not CLOUD_HELPER.stat().st_mode & 0o022, 'CLOUD_HELPER_TRUST')
    require(all(parent.stat().st_uid == 0 and not parent.stat().st_mode & 0o022
                for parent in CLOUD_HELPER.parents), 'CLOUD_HELPER_PARENT_TRUST')
    spec = importlib.util.spec_from_file_location('asa_private_cloud', CLOUD_HELPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    original = module.rclone
    module.rclone = lambda *args, **kw: original(*args, timeout_seconds=min(900, kw.get('timeout_seconds', 900)))
    return module


def run_backup(c):
    private_state()
    import fcntl
    fd = os.open(STATE / '.run.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    root = Path(c['root'])
    require(shutil.disk_usage(root).free >= 20 * 1024**3, 'BACKUP_FREE_SPACE_BELOW_20_GIB')
    recipients = private_file(RECIPIENTS).read_text().splitlines()
    require(any(re.fullmatch(r'age1[a-z0-9]+', line.strip()) for line in recipients), 'AGE_PUBLIC_RECIPIENT_REQUIRED')
    identity = json.loads((root / '.asa/installation.json').read_text())
    require(identity.get('instanceId') == c['installation_instance_id'], 'STALE_INSTALLATION')
    stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:12]
    staging = Path(tempfile.mkdtemp(prefix='.nightly-' + stamp + '-', dir=root / 'backups'))
    os.chown(staging, c['owner_uid'], -1)
    logpath = STATE / ('backup-' + stamp + '.log')
    require(not logpath.exists(), 'LOG_EXISTS')
    with logpath.open('x') as log:
        logpath.chmod(0o600)
        manager(c, ['backup', '--output', str(staging)], log)
        exports = [p for p in staging.iterdir() if p.is_dir() and (p / 'manifest.json').is_file()]
        require(len(exports) == 1, 'EXPECTED_ONE_VERIFIED_EXPORT')
        export = exports[0]
        manager(c, ['verify-backup', '--backup', str(export)], log)
        manifest = json.loads((export / 'manifest.json').read_text())
        require(manifest.get('complete') is True and manifest.get('project') == c['project'], 'BACKUP_MANIFEST')
        transport = cloud()
        spool = transport.ROOT / 'data' / c['cloud_project'] / 'outgoing'
        require(transport.status().get('connected') is True, 'CLOUD_NOT_CONNECTED')
        # transfer creates spool directories only on upload/download; create these narrow paths.
        for directory in [spool.parent, spool]:
            require(not directory.is_symlink(), 'SPOOL_SYMLINK')
            directory.mkdir(mode=0o700, exist_ok=True)
            os.chown(directory, 1000, 1000)
        archive = staging / 'verified-export.tar'
        with tarfile.open(archive, 'w', dereference=False) as tar:
            tar.add(export, arcname='asa-backup')
        name = 'asa-nightly-' + stamp + '.tar.age'
        # Encrypt under root-only state, not into a container-writable directory.
        encrypted = STATE / name
        fd = os.open(encrypted, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as output:
            subprocess.run(['/usr/bin/age', '-R', str(RECIPIENTS), str(archive)],
                           stdout=output, stderr=log, timeout=900, check=True)
        digest = transport.file_hash(encrypted)
        directory_fd = os.open(spool, os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            destination_fd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                                     0o600, dir_fd=directory_fd)
            with os.fdopen(destination_fd, 'wb') as output, encrypted.open('rb') as source:
                shutil.copyfileobj(source, output)
                output.flush()
                os.fsync(output.fileno())
                os.fchown(output.fileno(), 1000, 1000)
        finally:
            os.close(directory_fd)
        sent = transport.transfer('upload', c['cloud_project'], name, digest)
        received = transport.transfer('download', c['cloud_project'], name, digest)
        require(sent['sha256'] == received['sha256'] == digest, 'ROUNDTRIP_HASH')
    receipt = {'status': 'success', 'at': dt.datetime.now(dt.timezone.utc).isoformat(),
               'installation_instance_id': c['installation_instance_id'],
               'name': name, 'sha256': digest, 'bytes': sent['bytes'], 'project': c['cloud_project'],
               'runtime_revision': manifest['runtime'].get('revision'),
               'runtime_schema': manifest['runtime'].get('schema'),
               'backup_verified': True, 'cloud_roundtrip_verified': True,
               'decryption_verified': False, 'isolated_restore_verified': False, 'database_changed': False}
    save('last-success.json', receipt)
    # Delete only the private directory allocated by THIS run, never old backups/cloud files.
    require(staging.parent == root / 'backups' and staging.name.startswith('.nightly-' + stamp + '-'), 'CLEANUP_IDENTITY')
    require(not staging.is_symlink(), 'CLEANUP_SYMLINK')
    shutil.rmtree(staging)
    return receipt


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['run', 'schedule', 'status'])
    p.add_argument('--apply', action='store_true')
    a = p.parse_args()
    if a.action != 'status' and not a.apply:
        print(json.dumps({'plan': True, 'action': a.action, 'cloud_upload': 'age only',
                          'live_restore': False, 'delete_existing_archives': False})); return
    require(os.geteuid() == 0, 'ROOT_REQUIRED')
    c = config()
    if a.action == 'status':
        result = json.loads(private_file(STATE / 'last-success.json').read_text())
        require(result.get('installation_instance_id') == c['installation_instance_id'], 'BACKUP_DIFFERENT_INSTALLATION')
        age = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(result['at'])).total_seconds()
        require(0 <= age <= 26 * 3600, 'BACKUP_STALE')
        failure = STATE / 'last-failure.json'
        if failure.exists():
            failed_at = json.loads(private_file(failure).read_text())['at']
            require(dt.datetime.fromisoformat(failed_at) < dt.datetime.fromisoformat(result['at']),
                    'BACKUP_LAST_RUN_FAILED')
    else:
        try:
            result = schedule(c) if a.action == 'schedule' else run_backup(c)
        except Exception:
            save('last-failure.json', {'at': dt.datetime.now(dt.timezone.utc).isoformat(),
                                      'action': a.action, 'status': 'failed', 'inspect_private_logs': True})
            raise RuntimeError('BACKUP_FAILED_INSPECT_PRIVATE_STATE_NO_BLIND_RETRY') from None
    print(json.dumps(result))


if __name__ == '__main__':
    main()
