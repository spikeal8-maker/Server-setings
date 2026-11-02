#!/usr/bin/python3 -I
"""Install reviewed versioned CLI/units dormant; exact phase1 wrapper CAS only."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess

OLD_SHA = 'c6e3aee91c6c81e5e26cdb4aaa7052c234ea85be'
OLD_SOURCE = Path('/opt/server-settings/school-internet-offline') / OLD_SHA / 'school-internet-offline.py'
OLD_HASH = '1dca342f1b5fc1ad041a3a7318297af3c26560cbe78154c130d1bb80c5768c67'
REGDB_VERSION = '2026.05.30-1~deb13u1'
ROOT = Path(__file__).absolute().parents[1]


def trust(path, missing=False):
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            value = current.lstat()
        except FileNotFoundError:
            if missing:
                return
            raise
        if stat.S_ISLNK(value.st_mode) or value.st_uid != 0 or value.st_mode & 0o022:
            raise RuntimeError('UNTRUSTED_INSTALL_PATH')


def wrapper(target):
    return ('#!/bin/sh\nexec /usr/bin/python3 -I ' + str(target) + ' "$@"\n').encode()


def regdb(offline):
    existing = subprocess.run(['dpkg-query', '-W', '-f=${db:Status-Status}|${Version}', 'wireless-regdb'], capture_output=True, text=True)
    if existing.returncode == 0 and existing.stdout.startswith('installed|'):
        if existing.stdout != 'installed|' + REGDB_VERSION:
            raise RuntimeError('REGDB_VERSION_REVIEW_REQUIRED')
        return
    args = ['apt-get', '-s', '--no-install-recommends', 'install', 'wireless-regdb=' + REGDB_VERSION]
    plan = subprocess.run(args, check=True, capture_output=True, text=True, env={**os.environ, 'LC_ALL': 'C'}).stdout
    installs = re.findall(r'(?m)^Inst (\S+)(.*)$', plan)
    if re.search(r'(?m)^Remv ', plan) or len(installs) != 1 or installs[0][0] != 'wireless-regdb' or not installs[0][1].lstrip().startswith('(' + REGDB_VERSION + ' '):
        raise RuntimeError('REGDB_ADDITIVE_PLAN_CHANGED')
    before = offline.network_snapshot()
    offline.baseline()
    subprocess.run(['apt-get', '-y', '--no-upgrade', '--no-remove', '--no-install-recommends', 'install', 'wireless-regdb=' + REGDB_VERSION],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   env={**os.environ, 'SYSTEMD_OFFLINE': '1', 'DEBIAN_FRONTEND': 'noninteractive', 'NEEDRESTART_MODE': 'l'}, timeout=180)
    offline.baseline()
    if offline.network_snapshot() != before:
        raise RuntimeError('REGDB_NETWORK_BASELINE_CHANGED')


def unit_preflight(name, allow_missing=True):
    for property_name in ('DropInPaths', 'FragmentPath'):
        value = subprocess.run(['systemctl', 'show', name, '--property=' + property_name, '--value'], check=True, capture_output=True, text=True).stdout.strip()
        if property_name == 'DropInPaths' and value:
            raise RuntimeError('FOREIGN_UNIT_DROPINS')
        allowed = ('', '/etc/systemd/system/' + name) if allow_missing else ('/etc/systemd/system/' + name,)
        if property_name == 'FragmentPath' and value not in allowed:
            raise RuntimeError('FOREIGN_UNIT_FRAGMENT')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-sha')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if not args.apply:
        print(json.dumps({'plan': True, 'armed': False, 'primary_network_changes': False, 'wrapper_guard': OLD_SHA, 'regdb_additive_pin': REGDB_VERSION}))
        return
    if os.geteuid() != 0 or socket.gethostname() != 'pve' or not re.fullmatch(r'[0-9a-f]{40}', args.source_sha or ''):
        raise RuntimeError('EXACT_REVIEWED_SOURCE_SHA_ROOT_PVE_REQUIRED')
    files = [Path(__file__).absolute(), ROOT / 'scripts/school-internet-offline.py', ROOT / 'scripts/school-uplink-runtime.py',
             ROOT / 'scripts/school-internet-dispatch.py', ROOT / 'scripts/asa-school-dhcp-hook',
             *sorted((ROOT / 'scripts/school_uplink_runtime').glob('*.py')),
             *sorted((ROOT / 'units').glob('asa-school-uplink*'))]
    for path in files:
        trust(path)
    trust(OLD_SOURCE)
    if hashlib.sha256(OLD_SOURCE.read_bytes()).hexdigest() != OLD_HASH:
        raise RuntimeError('DEPLOYED_PHASE1_SOURCE_DRIFT')
    spec = importlib.util.spec_from_file_location('reviewed_offline', ROOT / 'scripts/school-internet-offline.py')
    offline = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(offline)
    offline.baseline()
    config = Path('/etc/asa-school-internet/profiles.json')
    if config.exists():
        offline.read_profiles()  # exact offline schema, authoritative armed=false
    if Path('/var/lib/asa-school-internet/journal.json').exists():
        raise RuntimeError('RECOVER_ACTIVE_TRANSACTION_BEFORE_INSTALL')
    public = Path('/usr/local/sbin/asa-school-internet')
    trust(public)
    expected = ('#!/bin/sh\nexec /usr/bin/python3 ' + str(OLD_SOURCE) + ' "$@"\n').encode()
    destination = Path('/opt/server-settings/school-uplink-runtime') / args.source_sha
    planned = wrapper(destination / 'scripts/school-internet-dispatch.py')
    if public.read_bytes() not in (expected, planned):
        raise RuntimeError('FOREIGN_WRAPPER_CAS_REJECTED')
    # Preflight every shared destination before any package/unit installation.
    shared = {Path('/usr/local/sbin/asa-school-uplink-runtime'): wrapper(destination / 'scripts/school-uplink-runtime.py'),
              Path('/usr/local/libexec/asa-school-dhcp-hook'): wrapper(destination / 'scripts/asa-school-dhcp-hook')}
    for source in files:
        if source.parent == ROOT / 'units':
            unit_preflight(source.name)
            shared[Path('/etc/systemd/system') / source.name] = source.read_bytes()
    for path, content in shared.items():
        trust(path, missing=True)
        if path.exists() and path.read_bytes() != content:
            raise RuntimeError('FOREIGN_RUNTIME_FILE_NO_OVERWRITE')
    for unit in ('wpa_supplicant.service', 'ModemManager.service'):
        if subprocess.run(['systemctl', 'is-enabled', unit], capture_output=True, text=True).stdout.strip() != 'masked':
            raise RuntimeError('PHASE1_MASK_REQUIRED')
    if subprocess.run(['systemctl', 'is-active', 'asa-school-uplink.service'], capture_output=True, text=True).stdout.strip() not in ('inactive', 'unknown'):
        raise RuntimeError('MANAGER_MUST_REMAIN_DORMANT')
    state = Path('/var/lib/asa-school-internet')
    trust(state, missing=True)
    state.mkdir(mode=0o700, exist_ok=True)
    if stat.S_IMODE(state.stat().st_mode) != 0o700:
        raise RuntimeError('PRIVATE_STATE_MODE')
    receipt_path = state / ('install-' + args.source_sha + '.json')
    receipt = {'requested_reviewed_sha': args.source_sha, 'phase': 'preflight', 'armed': False,
               'previous_wrapper': expected.decode(), 'manager_started': False}
    offline.atomic_private(receipt_path, json.dumps(receipt).encode())
    regdb(offline)
    trust(destination, missing=True)
    destination.mkdir(parents=True, mode=0o755, exist_ok=True)
    hashes = {}
    for source in files:
        relative = source.relative_to(ROOT)
        target = destination / relative
        trust(target, missing=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        content = source.read_bytes()
        if target.exists() and target.read_bytes() != content:
            raise RuntimeError('VERSION_CONTENT_DRIFT')
        offline.atomic_private(target, content)
        target.chmod(0o755 if target.suffix == '.py' or target.name == 'asa-school-dhcp-hook' else 0o644)
        hashes[str(relative)] = hashlib.sha256(content).hexdigest()
    for path, content in shared.items():
        path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        offline.atomic_private(path, content)
        path.chmod(0o644 if path.parent == Path('/etc/systemd/system') else 0o755)
    receipt.update(phase='shared_files_ready', source_hashes=hashes)
    offline.atomic_private(receipt_path, json.dumps(receipt).encode())
    # Recheck CAS immediately before switching the only owner-facing wrapper.
    if public.read_bytes() not in (expected, planned):
        raise RuntimeError('WRAPPER_CHANGED_DURING_INSTALL')
    offline.atomic_private(public, planned)
    public.chmod(0o755)
    receipt['phase'] = 'public_wrapper_switched_pending_timer_acceptance'
    offline.atomic_private(receipt_path, json.dumps(receipt).encode())
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    for source in files:
        if source.parent == ROOT / 'units':
            unit_preflight(source.name, allow_missing=False)
    subprocess.run(['systemctl', 'enable', '--now', 'asa-school-uplink-watchdog.timer'], check=True, capture_output=True)
    offline.baseline()
    for unit in ('wpa_supplicant.service', 'ModemManager.service'):
        if subprocess.run(['systemctl', 'is-enabled', unit], capture_output=True, text=True).stdout.strip() != 'masked':
            raise RuntimeError('PHASE1_MASK_CHANGED')
    if subprocess.run(['systemctl', 'is-active', 'asa-school-uplink-watchdog.timer'], capture_output=True, text=True).stdout.strip() != 'active':
        raise RuntimeError('WATCHDOG_TIMER_NOT_ACTIVE')
    if subprocess.run(['systemctl', 'is-active', 'asa-school-uplink.service'], capture_output=True, text=True).stdout.strip() not in ('inactive', 'unknown'):
        raise RuntimeError('MANAGER_UNEXPECTEDLY_STARTED')
    receipt['phase'] = 'accepted_dormant'
    offline.atomic_private(receipt_path, json.dumps(receipt).encode())
    print(json.dumps({'installed_dormant': True, 'armed': False, 'manager_started': False, 'source_sha': args.source_sha, 'watchdog_timer': 'enabled'}))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        raise SystemExit('STOP: dormant runtime installation not accepted; inspect exact guards locally, no automatic primary repair') from None
