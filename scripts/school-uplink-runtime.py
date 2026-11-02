#!/usr/bin/env python3
"""Dormant-by-default runtime CLI; mutations require root/PVE and --apply."""
import argparse
import json
import os
from pathlib import Path
import socket
import time

from school_uplink_runtime.adapters import dhcp_hook
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
        data = load(config)
        journal = load(state) if state.exists() else {}
        # No credentials or profile digests in stdout/journalctl.
        print(json.dumps({'armed': data.get('armed', False), 'phase': journal.get('phase', 'dormant'),
                          'mode': journal.get('mode'), 'hardware_qualified': bool(journal)}))
        return
    if not args.apply:
        print('PLAN_ONLY: no network, DNS, service or profile changes; actual hardware qualification required before arm')
        return
    if args.action == 'dhcp-hook':
        dhcp_hook(Path('/var/lib/asa-school-internet'), os.environ)
        return
    commands = Commands()
    for name in ('all', 'default', 'vmbr0'):
        require(Path('/proc/sys/net/ipv4/conf', name, 'rp_filter').read_text().strip() == '2', 'LOOSE_RPF_REQUIRED_NO_IMPLICIT_SYSCTL_CHANGE')
    manager = Manager(commands)
    if args.action == 'qualify':
        require(args.mode is not None, 'MODE_REQUIRED')
        manager.qualify(args.mode)
    elif args.action == 'arm':
        manager.arm()
    elif args.action in ('disarm', 'recover'):
        manager.disarm()
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
