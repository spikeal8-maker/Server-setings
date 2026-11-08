#!/usr/bin/python3
"""Prepare a visible 1Panel Supervisor collector from an exact Git checkout.

Run from 1Panel Terminal. Plan-only unless --apply is present. The optional
Supervisor prerequisite follows the official 1Panel Ubuntu/Debian recipe;
it installs only the reviewed new package set, never upgrades existing
packages, never runs apt update and never changes an existing application.
The collector is registered stopped. Its adoption is a separate operation.
"""
from __future__ import annotations

import argparse
import configparser
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys


SOURCE = Path(__file__).absolute().with_name('asa-1panel-collector.py')
spec = importlib.util.spec_from_file_location('asa_panel_collector', SOURCE)
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


def new_file(path, data, mode):
    path = Path(path)
    if path.exists() or path.is_symlink():
        bridge.need(bridge.controlled(path) == data and stat.S_IMODE(path.stat().st_mode) == mode,
                    'EXISTING_INSTALL_FILE_DIFFERENT')
        return
    parent = path.parent
    for item in (parent, *parent.parents):
        info = item.lstat()
        bridge.need(info.st_uid == 0 and stat.S_ISDIR(info.st_mode) and not info.st_mode & 0o022,
                    'INSTALL_PARENT_UNTRUSTED')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, 'wb') as stream:
        os.fchmod(stream.fileno(), mode)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def private_directory(path):
    path = Path(path)
    for item in (path.parent, *path.parent.parents):
        info = item.lstat()
        bridge.need(info.st_uid == 0 and stat.S_ISDIR(info.st_mode) and not info.st_mode & 0o022,
                    'INSTALL_PARENT_UNTRUSTED')
    if not path.exists() and not path.is_symlink():
        path.mkdir(mode=0o700)
    info = path.lstat()
    bridge.need(info.st_uid == 0 and stat.S_ISDIR(info.st_mode) and not info.st_mode & 0o022,
                'INSTALL_DIRECTORY_UNTRUSTED')


def apt_plan_digest(packages):
    return hashlib.sha256(bridge.json_bytes([{'name': name, 'version': version} for name, version in packages])).hexdigest()


def apt_plan():
    output = bridge.run(['/usr/bin/apt-get', '--simulate', '--no-install-recommends', '--no-upgrade',
                         'install', 'supervisor'], timeout=30)
    packages = []
    for line in output.splitlines():
        bridge.need(not line.startswith(('Remv ', 'Purg ')), 'APT_REMOVAL_FORBIDDEN')
        if not line.startswith('Inst '):
            continue
        match = re.fullmatch(r'Inst ([a-z0-9+.-]+)(?::[a-z0-9]+)? \(([^\s()]+) .+\)(?: \[[a-z0-9]+\])?', line)
        bridge.need(match is not None and '[' not in line.split('(', 1)[0], 'APT_UPGRADE_OR_UNKNOWN_PLAN')
        packages.append((match.group(1), match.group(2)))
    bridge.need(1 <= len(packages) <= 8 and len({name for name, _ in packages}) == len(packages)
                and any(name == 'supervisor' for name, _ in packages), 'APT_PACKAGE_PLAN_UNAVAILABLE')
    return packages


def supervisor_status(api):
    result = api.request('POST', '/hosts/tool/status', {'type': 'supervisord'})
    bridge.need(isinstance(result, dict) and isinstance(result.get('config'), dict), 'SUPERVISOR_STATUS_SCHEMA')
    return result['config']


def install_supervisor(api, packages):
    bridge.need(apt_plan() == packages, 'APT_PLAN_CHANGED')
    # Explicit versions and --no-upgrade keep this separate from OS maintenance.
    bridge.run(['/usr/bin/apt-get', '--yes', '--no-install-recommends', '--no-upgrade', '--no-remove',
                'install', *[name + '=' + version for name, version in packages]], timeout=180)
    observed = supervisor_status(api)
    bridge.need(observed.get('isExist') is True and observed.get('ctlExist') is True,
                'SUPERVISOR_PACKAGE_NOT_DETECTED')
    return observed


def initialize_supervisor(api, status):
    if status.get('init') is False:
        bridge.supervisor_ready(api)
        return
    # 1Panel init replaces include.files and restarts this one service. Only a
    # fresh, empty distro installation is eligible; never displace programs.
    bridge.need(status.get('configPath') == '/etc/supervisor/supervisord.conf'
                and status.get('serviceName') == 'supervisor', 'SUPERVISOR_INIT_PATH_UNREVIEWED')
    config_path = Path(status['configPath'])
    raw = bridge.controlled(config_path)
    config = configparser.ConfigParser(interpolation=None)
    config.read_string(raw.decode())
    bridge.need(config.get('include', 'files', fallback='').strip() == '/etc/supervisor/conf.d/*.conf'
                and not any(name.startswith(('program:', 'group:')) for name in config.sections()),
                'SUPERVISOR_EXISTING_CONFIGURATION')
    includes = Path('/etc/supervisor/conf.d')
    bridge.need(includes.is_dir() and not includes.is_symlink() and not list(includes.iterdir()),
                'SUPERVISOR_FOREIGN_PROGRAMS_PRESENT')
    bridge.need(bridge.run(['/usr/bin/supervisorctl', 'status']).strip() == '', 'SUPERVISOR_RUNNING_PROGRAMS_PRESENT')
    new_file('/var/lib/server-settings/supervisord-before-1panel.conf', raw, 0o600)
    api.request('POST', '/hosts/tool/init', {'type': 'supervisord',
                'configPath': str(config_path), 'serviceName': 'supervisor'})
    bridge.supervisor_ready(api)


def configuration(root, panel_host, key_file):
    root = Path(root).absolute()
    settings = json.loads(bridge.controlled(root / '.asa/installation.json'))
    project = settings.get('project', '')
    instance = settings.get('instanceId', '')
    bridge.need(settings.get('collectorMode') == 'system-root'
                and re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', project)
                and re.fullmatch(r'[a-f0-9]{32}', instance), 'PREPARED_ROOT_INSTALLATION_REQUIRED')
    bridge.need(settings.get('collectorManager') in (None, bridge.MANAGER), 'FOREIGN_COLLECTOR_MANAGER')
    return {'format': 'asa-1panel-collector-v1', 'root': str(root), 'project': project, 'instanceId': instance,
            'panelHost': panel_host, 'panelKeyFile': str(Path(key_file).absolute()),
            'panelVersion': bridge.PANEL_VERSION, 'python': sys.executable,
            'program': 'asa-log-collector-' + project}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True)
    parser.add_argument('--panel-host', required=True)
    parser.add_argument('--key-file', default='/var/lib/server-settings/1panel-api.key')
    parser.add_argument('--install-supervisor', action='store_true')
    parser.add_argument('--apt-plan-sha256')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    try:
        bridge.need(os.geteuid() == 0, 'ROOT_REQUIRED')
        payload = bridge.controlled(SOURCE)
        config = configuration(args.root, args.panel_host, args.key_file)
        api = bridge.api_client(config)
        # A legacy canonical adapter is sufficient only to prepare a stopped
        # native program. Selecting/running it remains the new adapter's job.
        lifecycle, install = bridge.canonical(config, require_adapter=False)
        status = supervisor_status(api)
        packages = apt_plan() if status.get('isExist') is False else []
        result = {'state': 'planned', 'root': config['root'], 'project': config['project'],
                  'panelVersion': config['panelVersion'], 'bridgeSha256': hashlib.sha256(payload).hexdigest(),
                  'newPackages': [{'name': name, 'version': version} for name, version in packages],
                  'aptPlanSha256': apt_plan_digest(packages),
                  'supervisorInitializationRequired': bool(packages) or status.get('init') is True,
                  'program': config['program'], 'collectorAutostart': False,
                  'collectorAdoption': 'SEPARATE_STEP', 'operatingSystemUpgrade': False}
        if not args.apply:
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return 0
        if packages:
            bridge.need(args.install_supervisor, 'SUPERVISOR_INSTALL_FLAG_REQUIRED')
            bridge.need(args.apt_plan_sha256 == result['aptPlanSha256'], 'APT_REVIEWED_PLAN_REQUIRED')
        private_directory('/var/lib/server-settings')
        if packages:
            status = install_supervisor(api, packages)
        initialize_supervisor(api, status)
        new_file(bridge.BRIDGE, payload, 0o755)
        new_file(bridge.CONFIG, bridge.json_bytes(config), 0o600)
        runner_path = install.state / 'collector-runner.json'
        if not runner_path.exists():
            # A clean installation records its identity without creating or
            # starting a temporary systemd collector.
            identity = lifecycle.runner_identity(install)
            bridge.need(identity.get('mode') == 'system-root' and identity.get('manager') is None,
                        'FRESH_RUNNER_IDENTITY_UNSUPPORTED')
            with bridge.redirect_stdout(bridge.io.StringIO()):
                lifecycle.verify_systemd_state(identity, Path('/etc/systemd/system') / identity['name'], config['python'])
            new_file(runner_path, bridge.json_bytes(identity), 0o600)
        result.update(bridge.stage(api, config, apply=True))
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
        code = str(error) if isinstance(error, bridge.PanelCollectorError) else type(error).__name__
        print(json.dumps({'state': 'blocked', 'code': code}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
