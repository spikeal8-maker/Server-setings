"""Explicit owner-enabled host administration through a private root socket."""
import json
import os
from pathlib import Path
import socket
import subprocess

from .store import OpsError

SOCKET = '/run/asa-admin-executor/control.sock'


def enabled(config):
    if config.get('host_admin_enabled') is not True:
        raise OpsError('ADMIN_DISABLED', 'Host administration is not enabled by the server owner')


def request(config, payload):
    enabled(config)
    payload = dict(payload, installation_id=config['installation']['id'])
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
        stream.settimeout(payload.get('timeout_seconds', 30) + 30)
        stream.connect(SOCKET)
        stream.sendall(json.dumps(payload).encode() + b'\n')
        with stream.makefile('rb') as reader:
            raw = reader.readline(262145)
    if len(raw) > 262144 or not raw.endswith(b'\n'):
        raise OpsError('ADMIN_PROTOCOL', 'Root executor returned an invalid response; never replay automatically')
    value = json.loads(raw)
    if not value.get('ok'):
        raise OpsError(value.get('code', 'ADMIN_ERROR'), 'Root executor refused the request; inspect administration status')
    return value['data']


def inventory(config):
    """Metadata only: deliberately exclude environment, mounts and user records."""
    result = subprocess.run(['docker', 'ps', '-a', '--format', '{{json .}}'],
                            capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise OpsError('DOCKER', 'Docker inventory is unavailable')
    return {'hostname': socket.gethostname(), 'executor_uid': os.getuid() if hasattr(os, 'getuid') else None,
            'root_administration_enabled': config.get('host_admin_enabled') is True,
            'containers': [json.loads(line) for line in result.stdout.splitlines() if line]}
