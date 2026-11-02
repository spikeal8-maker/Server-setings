#!/usr/bin/python3 -I
"""Dormant-by-default runtime CLI; mutations require root/PVE and --apply."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import sys
import time


def trusted_sources():
    base = Path(__file__).absolute().parent
    for path in [Path(__file__).absolute(), *sorted((base / 'school_uplink_runtime').glob('*.py'))]:
        current = Path(path.anchor)
        for part in path.parts[1:]:
            current /= part
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                raise SystemExit('UPLINK_STOP:UNTRUSTED_INSTALLED_SOURCE')
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(base))


trusted_sources()
from school_uplink_runtime.adapters import dhcp_hook, profile_digest, boot_id
from school_uplink_runtime.manager import Manager
from school_uplink_runtime.network import Commands
from school_uplink_runtime.storage import load, require, Stop


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['status', 'qualify', 'arm', 'disarm', 'recover', 'watchdog', 'run', 'dhcp-hook'])
    parser.add_argument('--mode', choices=['router', 'wifi', 'modem'])
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    require(os.geteuid() == 0, 'ROOT_REQUIRED')
    require(socket.gethostname() == 'pve' and Path('/etc/pve').is_dir(), 'EXACT_PVE_HOST_REQUIRED')
    if args.action == 'status':
        config = Path('/etc/asa-school-internet/profiles.json')
        state = Path('/var/lib/asa-school-internet/journal.json')
        data = load(config) if config.exists() else {'armed': False, 'profiles': []}
        journal = load(state) if state.exists() else {}
        proofs_file = Path('/var/lib/asa-school-internet/qualification.json')
        proofs = load(proofs_file) if proofs_file.exists() else {}
        profile = next((p for p in data['profiles'] if p['mode'] == journal.get('mode')), None)
        proof = proofs.get(journal.get('mode'), {})
        qualified = bool(profile and journal.get('phase') in ('standby', 'backup') and journal.get('boot_id') == boot_id()
                         and proof.get('profile') == journal.get('profile') == profile_digest(profile)
                         and proof.get('mac') == journal.get('device', {}).get('mac')
                         and journal.get('lease', {}).get('expires_mono', 0) > time.monotonic())
        # No credentials or profile digests in stdout/journalctl.
        print(json.dumps({'armed': data.get('armed', False), 'phase': journal.get('phase', 'dormant'),
                          'mode': journal.get('mode'), 'hardware_qualified': qualified,
                          'configured_modes': [p['mode'] for p in data['profiles']]}))
        return
    if not args.apply:
        print('PLAN_ONLY: no network, DNS, service or profile changes; actual hardware qualification required before arm')
        return
    if args.action == 'dhcp-hook':
        dhcp_hook(Path('/var/lib/asa-school-internet'), os.environ)
        return
    commands = Commands()
    if args.action in ('qualify', 'arm', 'run'):
        require(hashlib.sha256(Path('/etc/network/interfaces').read_bytes()).hexdigest() == 'e676d55c7ff7e8eaa3cda0896f3caac4209ebe0fa4a56d3c6907a8bc268f0aba', 'PRIMARY_INTERFACES_CHANGED_NEEDS_DIAGNOSIS')
    for name in ('all', 'default', 'vmbr0'):
        require(Path('/proc/sys/net/ipv4/conf', name, 'rp_filter').read_text().strip() == '2', 'LOOSE_RPF_REQUIRED_NO_IMPLICIT_SYSCTL_CHANGE')
    manager = Manager(commands)
    if args.action == 'qualify':
        require(args.mode is not None, 'MODE_REQUIRED')
        manager.qualify(args.mode)
    elif args.action == 'arm':
        manager.arm()
    elif args.action == 'disarm':
        manager.disarm()
    elif args.action == 'recover':
        with manager.transaction():
            manager.rollback()  # preserve explicitly armed intent across reboot
    elif args.action == 'watchdog':
        manager.watchdog()
    elif args.action == 'run':
        while True:
            try:
                manager.tick()
            except Exception:
                # Fail closed with exact-owned rollback; never repair main.
                manager.disarm()
                raise
            time.sleep(5)
    print('UPLINK_ACTION_COMPLETE')


if __name__ == '__main__':
    try:
        main()
    except (Stop, ValueError, OSError) as error:
        # Do not print arbitrary exception strings containing untrusted input.
        print('UPLINK_STOP:' + (str(error) if isinstance(error, Stop) else type(error).__name__))
        raise SystemExit(2)
