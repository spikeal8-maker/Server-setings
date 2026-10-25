"""Root-owned systemd socket worker. Standalone: never import writable app code."""
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import socket
import stat
import struct
import subprocess
import sys
import time

CONFIG = Path('/etc/server-settings/asa-admin-executor.json')
STATE = Path('/var/lib/server-settings/asa-admin-executor')
MAX_OUTPUT = 8 * 1024 * 1024


def require(value, code):
    if not value:
        raise ValueError(code)


def trusted(path, mode, directory=False):
    st = path.lstat()
    require((stat.S_ISDIR if directory else stat.S_ISREG)(st.st_mode)
            and st.st_uid == 0 and stat.S_IMODE(st.st_mode) == mode, 'ADMIN_STORAGE_IDENTITY')


def authorize(config, payload, peer_uid):
    require(config.get('enabled') is True, 'ADMIN_DISABLED')
    require(config['hostname'] == socket.gethostname(), 'ADMIN_HOST')
    require(peer_uid == config['owner_uid'], 'ADMIN_PEER')
    require(payload.get('installation_id') == config['installation_id'], 'ADMIN_INSTALLATION')
    current = json.loads((Path(config['root']) / '.asa/installation.json').read_text())
    require(current.get('instanceId') == config['installation_instance_id'], 'STALE_INSTALLATION')
    if payload.get('action') == 'execute':
        require(payload.get('installation_instance_id') == config['installation_instance_id'], 'STALE_INSTALLATION')


def redact(text, config):
    values = set()
    for filename in config.get('secret_files', []):
        try:
            for line in Path(filename).read_text().splitlines():
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                value = line.split('=', 1)[1] if '=' in line else line
                value = value.strip().strip("\"'")
                if len(value) >= 8:
                    values.add(value)
                try:
                    parsed = json.loads(value)
                    if isinstance(parsed, dict):
                        values.update(v for v in parsed.values() if isinstance(v, str) and len(v) >= 8)
                except ValueError:
                    pass
        except (OSError, UnicodeError):
            pass
    for value in sorted(values, key=len, reverse=True):
        text = text.replace(value, '[REDACTED]')
    text = re.sub(r'(?i)(bearer\s+)[A-Za-z0-9_.~+/=-]+', r'\1[REDACTED]', text)
    return text


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def save(path, value):
    temp = path.with_suffix('.tmp')
    with open(temp, 'w', encoding='utf-8') as out:
        os.chmod(temp, 0o600)
        json.dump(value, out)
        out.flush()
        os.fsync(out.fileno())
    os.replace(temp, path)
    sync_directory(path.parent)


def execute(config, payload, state=STATE):
    require(payload.get('action') in ('execute', 'status'), 'ADMIN_ACTION')
    opid = payload.get('operation_id')
    if payload['action'] == 'status' and not opid:
        return {'enabled': True, 'hostname': socket.gethostname(), 'uid': os.geteuid(),
                'installation_id': config['installation_id'], 'authority': 'root shell; owner-authorized'}
    require(isinstance(opid, str) and re.fullmatch('[a-f0-9]{32}', opid), 'ADMIN_OPERATION_ID')
    target = state / opid
    if payload['action'] == 'status':
        require(target.is_dir(), 'ADMIN_NOT_FOUND')
        result = json.loads((target / 'state.json').read_text())
        result.pop('request_hash', None)
        return result
    require(set(payload) == {'action', 'operation_id', 'installation_id', 'installation_instance_id', 'command', 'cwd', 'timeout_seconds'}, 'ADMIN_ARGUMENTS')
    command, cwd, timeout = payload['command'], payload['cwd'], payload['timeout_seconds']
    require(isinstance(command, str) and 0 < len(command) <= 32000 and '\0' not in command, 'ADMIN_COMMAND')
    require(isinstance(cwd, str) and Path(cwd).is_absolute() and Path(cwd).is_dir() and '\0' not in cwd, 'ADMIN_CWD')
    require(type(timeout) is int and 1 <= timeout <= 7200, 'ADMIN_TIMEOUT')
    request_hash = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    try:
        target.mkdir(mode=0o700)
        sync_directory(state)
    except FileExistsError:
        old = json.loads((target / 'state.json').read_text())
        require(old.get('request_hash') == request_hash, 'ADMIN_IDEMPOTENCY_CONFLICT')
        # Existence is never permission to execute again, including interrupted jobs.
        return {k: v for k, v in old.items() if k != 'request_hash'}
    current = {'status': 'running', 'operation_id': opid, 'request_hash': request_hash,
               'started_at': datetime.datetime.now(datetime.timezone.utc).isoformat()}
    save(target / 'request.json', payload)
    save(target / 'state.json', current)  # durable root audit before executing anything
    start = time.monotonic()
    output = bytearray()
    timed_out = False
    with open(target / 'output.private.log', 'xb') as raw:
        os.chmod(target / 'output.private.log', 0o600)
        child = subprocess.Popen(['/bin/bash', '--noprofile', '--norc', '-c', command], cwd=cwd,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 start_new_session=True, env={'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
                                                              'HOME': '/root', 'LANG': 'C.UTF-8'})
        current['command_pid'] = child.pid
        save(target / 'state.json', current)
        selector = selectors.DefaultSelector()
        selector.register(child.stdout, selectors.EVENT_READ)
        eof = False
        while not eof:
            if time.monotonic() - start >= timeout:
                timed_out = True
                break
            for key, _ in selector.select(min(0.2, max(0.01, timeout - (time.monotonic() - start)))):
                block = os.read(key.fd, 65536)
                if not block:
                    eof = True
                    break
                available = max(0, MAX_OUTPUT - raw.tell())
                raw.write(block[:available])
                if len(output) < 32768:
                    output.extend(block[:32768-len(output)])
        selector.close()
        # A shell that closes stdout can still be running. Its remaining deadline applies.
        try:
            child.wait(timeout=max(0.01, timeout - (time.monotonic() - start)))
        except subprocess.TimeoutExpired:
            timed_out = True
        # Root commands are trusted administration, not sandboxed programs. Kill remaining
        # ordinary process-group children on completion/timeout; systemd owns the cgroup too.
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
        child.stdout.close()
        raw.flush()
        os.fsync(raw.fileno())
    current.update(status='complete', exit_code=child.returncode, timed_out=timed_out,
                   output=redact(output.decode('utf-8', errors='replace'), config),
                   output_may_be_truncated=len(output) == 32768,
                   duration_seconds=round(time.monotonic()-start, 3))
    save(target / 'state.json', current)
    return {k: v for k, v in current.items() if k != 'request_hash'}


def main():
    os.umask(0o077)
    try:
        require(os.geteuid() == 0, 'ADMIN_ROOT')
        trusted(CONFIG, 0o600)
        trusted(STATE, 0o700, directory=True)
        config = json.loads(CONFIG.read_text())
        with socket.socket(fileno=os.dup(0)) as peer:
            _, uid, _ = struct.unpack('3i', peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        raw = sys.stdin.buffer.readline(65537)
        require(len(raw) <= 65536 and raw.endswith(b'\n'), 'ADMIN_REQUEST_SIZE')
        payload = json.loads(raw)
        authorize(config, payload, uid)
        result = {'ok': True, 'data': execute(config, payload)}
    except Exception as exc:
        code = str(exc) if isinstance(exc, ValueError) and re.fullmatch('[A-Z_]+', str(exc)) else 'ADMIN_EXECUTOR_ERROR'
        result = {'ok': False, 'code': code}
    sys.stdout.write(json.dumps(result) + '\n')
    sys.stdout.flush()


if __name__ == '__main__':
    main()
