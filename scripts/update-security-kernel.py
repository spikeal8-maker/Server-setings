#!/usr/bin/env python3
"""Install only security-origin HWE kernel updates; no reboot/removal. Plan by default."""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess

PACKAGES = ['linux-generic-hwe-24.04', 'linux-headers-generic-hwe-24.04', 'linux-image-generic-hwe-24.04']


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--apply', action='store_true')
    p.add_argument('--expected-host')
    a = p.parse_args()
    import apt
    cache = apt.Cache()
    updates = []
    for name in PACKAGES:
        package = cache[name]
        assert package.is_installed
        if package.is_upgradable:
            assert any(o.archive.endswith('-security') for o in package.candidate.origins)
            updates.append({'name': name, 'version': package.candidate.version})
    command = ['apt-get', 'install', '--only-upgrade'] + [u['name'] + '=' + u['version'] for u in updates]
    if not updates:
        print(json.dumps({'updated': False, 'reason': 'No newer security kernel candidates'})); return
    plan = subprocess.run(command[:1] + ['--simulate'] + command[1:], capture_output=True, text=True, check=True)
    assert not any(line.startswith('Remv ') for line in plan.stdout.splitlines()), 'Package removals refused'
    assert all(line.split()[1].startswith('linux-') for line in plan.stdout.splitlines() if line.startswith('Inst ')), 'Non-kernel change refused'
    print(json.dumps({'plan': not a.apply, 'updates': updates, 'reboot': False}), flush=True)
    if not a.apply:
        return
    assert os.geteuid() == 0 and a.expected_host == socket.gethostname()
    running = subprocess.check_output(['uname', '-r'], text=True).strip()
    assert Path('/boot/vmlinuz-' + running).is_file(), 'Running recovery image missing'
    env = dict(os.environ, DEBIAN_FRONTEND='noninteractive', NEEDRESTART_MODE='l')
    subprocess.run(command + ['-y'], env=env, check=True)
    assert Path('/boot/vmlinuz-' + running).is_file(), 'Previous kernel was not preserved'
    print(json.dumps({'installed': True, 'running_kernel': running, 'reboot_required': True}))


if __name__ == '__main__':
    main()
