#!/usr/bin/env python3
"""Install a root-owned scheduler; timer activation requires a restore receipt."""
import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess

SOURCE = Path(__file__).resolve().parent
CONFIG = Path('/etc/server-settings/asa-nightly-backup.json')
STATE = Path('/var/lib/asa-nightly-backup')


def require(value, code):
    if not value:
        raise RuntimeError(code)


def read_private(path):
    require(not path.is_symlink() and path.is_file(), 'PRIVATE_FILE')
    st = path.stat()
    require(st.st_uid == 0 and st.st_mode & 0o077 == 0, 'PRIVATE_FILE_MODE')
    return json.loads(path.read_text())


def validate_rehearsal(c, accepted, backup, now):
    require(accepted.get('hostname') == c['hostname'] and
            accepted.get('installation_instance_id') == c['installation_instance_id'], 'RESTORE_RECEIPT_IDENTITY')
    require(accepted.get('archive_sha256') == backup.get('sha256') and
            re.fullmatch('[a-f0-9]{64}', accepted.get('archive_sha256', '')), 'RESTORE_RECEIPT_ARCHIVE')
    require(all(accepted.get(key) is True for key in [
        'decryption_verified', 'postgres_restored', 'objects_verified', 'restored_account_project_checks']),
        'RESTORE_NOT_ACCEPTED')
    age = now - dt.datetime.fromisoformat(accepted['accepted_at'])
    require(dt.timedelta(0) <= age <= dt.timedelta(days=7), 'RESTORE_RECEIPT_STALE')


def service(runner):
    return f'''[Unit]
Description=Encrypted ASA backup through owner-authorized MCP gate
After=network-online.target asa-operations.service docker.service
Wants=network-online.target

[Service]
Type=oneshot
UMask=0077
ExecStart=/usr/bin/python3 {runner} schedule --apply
TimeoutStartSec=7100
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths=/var/lib/asa-nightly-backup
'''


TIMER = '''[Unit]
Description=Daily ASA encrypted backup at02:30 Moscow

[Timer]
OnCalendar=*-*-* 02:30:00 Europe/Moscow
Persistent=true
AccuracySec=1min
Unit=asa-nightly-backup.service

[Install]
WantedBy=timers.target
'''


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--apply', action='store_true')
    p.add_argument('--expected-host')
    p.add_argument('--source-id')
    p.add_argument('--enable-timer', action='store_true')
    p.add_argument('--restore-receipt', type=Path)
    a = p.parse_args()
    if not a.apply:
        print(json.dumps({'plan': True, 'schedule': '02:30 Europe/Moscow', 'enable_requested': a.enable_timer,
                          'requires': ['private config', 'public age recipient', 'cloud enrollment', 'accepted isolated restore before enable'],
                          'live_restore': False, 'delete_existing_archives': False})); return
    require(os.geteuid() == 0 and a.expected_host == socket.gethostname(), 'HOST_OR_ROOT')
    require(a.source_id and re.fullmatch('[a-f0-9]{40}', a.source_id), 'REVIEWED_SOURCE_ID_REQUIRED')
    c = read_private(CONFIG)
    require(c['hostname'] == a.expected_host, 'CONFIG_HOST')
    enabled = subprocess.run(['systemctl', 'is-enabled', 'asa-nightly-backup.timer'],
                             capture_output=True, text=True).returncode == 0
    require(not enabled or a.enable_timer, 'EXISTING_TIMER_REQUIRES_ACCEPTED_REHEARSAL')
    # Validate activation prerequisites BEFORE touching any existing unit/source.
    if a.enable_timer:
        require(a.restore_receipt is not None, 'RESTORE_RECEIPT_REQUIRED')
        validate_rehearsal(c, read_private(a.restore_receipt), read_private(STATE / 'last-success.json'),
                           dt.datetime.now(dt.timezone.utc))
    root = Path('/opt/server-settings/asa-nightly-backup')
    require(not root.is_symlink(), 'SOURCE_SYMLINK')
    root.mkdir(mode=0o700, exist_ok=True)
    for parent in [root, *root.parents]:
        require(parent.stat().st_uid == 0 and not parent.stat().st_mode & 0o022, 'SOURCE_PARENT_TRUST')
    target = root / a.source_id
    runner = target / 'nightly-asa-backup.py'
    if target.exists():
        require(not target.is_symlink() and target.stat().st_uid == 0
                and target.stat().st_mode & 0o077 == 0, 'EXISTING_VERSION_TRUST')
        require(not runner.is_symlink() and runner.stat().st_uid == 0
                and runner.stat().st_mode & 0o077 == 0, 'EXISTING_RUNNER_TRUST')
        require(runner.read_bytes() == (SOURCE / runner.name).read_bytes(), 'EXISTING_VERSION_CONTENT')
    else:
        target.mkdir(mode=0o700)
        shutil.copyfile(SOURCE / runner.name, runner)
        runner.chmod(0o600)
    require(not STATE.is_symlink(), 'STATE_SYMLINK')
    STATE.mkdir(mode=0o700, exist_ok=True)
    require(STATE.stat().st_uid == 0 and STATE.stat().st_mode & 0o077 == 0, 'STATE_MODE')
    directory = Path('/etc/systemd/system')
    for name, content in [('asa-nightly-backup.service', service(runner)), ('asa-nightly-backup.timer', TIMER)]:
        path = directory / name
        require(not path.is_symlink(), 'UNIT_SYMLINK')
        path.write_text(content)
        path.chmod(0o644)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    if a.enable_timer:
        subprocess.run(['systemctl', 'enable', '--now', 'asa-nightly-backup.timer'], check=True)
    print(json.dumps({'installed': True, 'timer_enable_requested': a.enable_timer,
                      'existing_timer_disabled': False, 'source_id': a.source_id}))


if __name__ == '__main__':
    main()
