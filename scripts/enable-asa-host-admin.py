#!/usr/bin/env python3
"""Explicit full-host administration enrollment. Plan by default; never changes ASA data."""
import argparse
import json
import os
from pathlib import Path
import pwd
import shutil
import socket
import sqlite3
import subprocess

CONFIG = Path('/etc/server-settings/asa-operations.json')
EXECUTOR = Path('/etc/server-settings/asa-admin-executor.json')
SOURCE = Path(__file__).resolve().parents[1]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--apply', action='store_true')
    p.add_argument('--enable-full-admin', action='store_true')
    p.add_argument('--expected-host')
    a = p.parse_args()
    if not a.apply:
        print('PLAN: enable owner-authorized root shell on this host via existing authenticated MCP. This authority can bypass ordinary update gates. No data/service changes.'); return
    assert os.geteuid() == 0 and a.enable_full_admin, 'Explicit root/--enable-full-admin required'
    assert a.expected_host == socket.gethostname(), 'Exact expected hostname required'
    c = json.loads(CONFIG.read_text()); i = c['installation']
    assert CONFIG.stat().st_uid == 0 and not CONFIG.is_symlink()
    assert not CONFIG.stat().st_mode & 0o022
    state = Path(i['state_root']) / 'operations.sqlite3'
    with sqlite3.connect(state.as_uri()+'?mode=ro', uri=True) as db:
        assert not db.execute("SELECT 1 FROM operations WHERE status IN ('queued','running','recovery_required')").fetchone(), 'Active/interrupted operation: defer enrollment'
    assert Path(i['root']).resolve() == Path(i['root'])
    identity = json.loads((Path(i['root'])/'.asa/installation.json').read_text())['instanceId']
    import re
    assert re.fullmatch('[a-f0-9]{32}', identity)
    owner = pwd.getpwuid(i['owner_uid']); assert owner.pw_uid != 0
    private = Path('/var/lib/server-settings/asa-admin-executor')
    if private.exists():
        st = private.lstat()
        import stat
        assert stat.S_ISDIR(st.st_mode) and st.st_uid == 0 and stat.S_IMODE(st.st_mode) == 0o700
    else:
        private.mkdir(mode=0o700)
    value = {'enabled': True, 'hostname': a.expected_host, 'owner_uid': owner.pw_uid,
             'installation_id': i['id'], 'root': i['root'], 'installation_instance_id': identity,
             'secret_files': [str(Path(i['root'])/'.env'), '/etc/server-settings/asa-operations.env',
                              '/etc/server-settings/1panel-gateway.env', '/etc/server-settings/gateway.env', '/etc/server-settings/mcp.env',
                              '/var/lib/server-settings/1panel-api.key']}
    from asa_ops.admin_executor import save
    assert not EXECUTOR.is_symlink()
    if EXECUTOR.exists():
        assert EXECUTOR.stat().st_uid == 0 and not EXECUTOR.stat().st_mode & 0o077
    save(EXECUTOR, value)
    # Reviewed backend installation refuses active workers and preserves app/data.
    subprocess.run(['bash', str(SOURCE/'scripts/install-asa-operations.sh'), '--apply'], check=True)
    for name in ['asa-admin-executor.socket', 'asa-admin-executor@.service']:
        content = (SOURCE/'templates'/name).read_text().replace('SocketUser=admins', 'SocketUser='+owner.pw_name)
        import grp
        content = content.replace('SocketGroup=admins', 'SocketGroup='+grp.getgrgid(owner.pw_gid).gr_name)
        target = Path('/etc/systemd/system') / name
        assert not target.is_symlink()
        target.write_text(content); target.chmod(0o644)
    c['host_admin_enabled'] = True
    backup = CONFIG.with_name('asa-operations.before-host-admin.json')
    if not backup.exists():
        shutil.copyfile(CONFIG, backup); backup.chmod(0o600)
    save(CONFIG, c)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', 'enable', '--now', 'asa-admin-executor.socket'], check=True)
    subprocess.run(['systemctl', 'restart', 'asa-operations.service'], check=True)
    print(json.dumps({'host': a.expected_host, 'installation_id': i['id'], 'root_administration': 'enabled',
                      'application_containers_changed': False, 'new_public_listener': False}))


if __name__ == '__main__':
    main()
