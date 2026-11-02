#!/usr/bin/env python3
"""Repair masked traversal ACL for the existing MCP owner; plan by default."""
import argparse
import json
import os
from pathlib import Path
import socket
import stat
import subprocess


def validate_acl(acl, owner):
    """Do not unmask unrelated users/groups when setfacl recalculates the mask."""
    allowed = {'user::rwx', 'group::---', 'other::---', 'mask::---',
               'mask::--x', 'user:' + owner + ':--x'}
    for line in acl.splitlines():
        value = line.split('#', 1)[0].strip()
        if value and value not in allowed:
            raise ValueError('Unexpected parent ACL; manual review required')


def main():
    import pwd
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--apply', action='store_true')
    p.add_argument('--expected-host')
    a = p.parse_args()
    config = Path('/etc/server-settings/asa-operations.json')
    assert os.geteuid() == 0, 'Root required to inspect private policy'
    assert not config.is_symlink() and config.stat().st_uid == 0
    assert not config.stat().st_mode & 0o022
    c = json.loads(config.read_text())['installation']
    parent = Path('/var/lib/server-settings')
    state = Path(c['state_root'])
    assert state.parent == parent and state.resolve() == state
    assert parent.resolve() == parent and stat.S_ISDIR(parent.stat().st_mode)
    assert parent.stat().st_uid == 0 and not parent.stat().st_mode & 0o022
    owner = pwd.getpwuid(c['owner_uid'])
    assert owner.pw_uid != 0 and state.stat().st_uid == owner.pw_uid
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    before = subprocess.check_output(['getfacl', '-cp', str(parent)], text=True)
    validate_acl(before, owner.pw_name)
    if not a.apply:
        print(json.dumps({'plan': True, 'parent': str(parent), 'owner': owner.pw_name,
                          'change': 'restore named-user traversal ACL only', 'restart': False}))
        return
    assert a.expected_host == socket.gethostname(), 'Exact hostname required'
    backup = Path('/var/backups/server-settings-acl')
    assert not backup.is_symlink()
    backup.mkdir(mode=0o700, exist_ok=True)
    assert backup.stat().st_uid == 0 and stat.S_IMODE(backup.stat().st_mode) == 0o700
    receipt = backup / 'state-parent-before-repair.acl'
    # Preserve the first pre-repair ACL; never follow an existing link.
    try:
        fd = os.open(receipt, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        assert not receipt.is_symlink() and receipt.stat().st_uid == 0
    else:
        with os.fdopen(fd, 'w') as f:
            f.write('# file: /var/lib/server-settings\n' + before)
    subprocess.run(['setfacl', '-m', 'u:' + owner.pw_name + ':--x', str(parent)], check=True)
    subprocess.run(['runuser', '-u', owner.pw_name, '--', 'test', '-x', str(parent)], check=True)
    assert subprocess.run(['runuser', '-u', owner.pw_name, '--', 'test', '-r', str(parent)]).returncode != 0
    assert subprocess.run(['runuser', '-u', 'nobody', '--', 'test', '-x', str(parent)]).returncode != 0
    print(json.dumps({'repaired': True, 'restart': False, 'parent_listing_granted': False,
                      'rollback': 'setfacl --restore=' + str(receipt)}))


if __name__ == '__main__':
    main()
