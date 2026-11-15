#!/usr/bin/env python3
"""Plan or attach the existing H03 ASA Lab installation to the official updater."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import runpy
import socket
import stat
import subprocess
import tempfile

from panelctl import PanelAPI, read_api_key, require_local_write

HOST = 'admins-MS-7D22'
ROOT = Path('/srv/asa-lab')
TOOLS_BIN = Path('/usr/local/libexec/asa-h03')
BIN = Path('/usr/local/bin')
HISTORY = Path('/var/lib/server-settings/asa-h03-operator-history')
WRAPPER = Path(__file__).with_name('asa-h03-update-command.sh')
PREVIOUS = 'a0d82f19612e21ef10a987152f4817cc7cd176af70bb5c67ca02d50ce9861142'
PRIVATE_INPUTS = ('compose.yaml', '.asa/installed-release.json', '.asa/last-operation.json',
                  '.asa/runtime-configuration.json')


def checked_targets():
    owner = pwd.getpwnam('admins').pw_uid
    for path in (ROOT, ROOT / '.asa'):
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != owner or info.st_mode & 0o022:
            raise SystemExit('Не подтверждён каталог установки H03')
    state = json.loads((ROOT / '.asa/installation.json').read_text(encoding='utf-8'))
    if state.get('profile') != 'production' or state.get('project') != 'asa-lab-dev':
        raise SystemExit('Требуется действующая установка ASA Lab на H03')
    targets = {}
    for name in PRIVATE_INPUTS:
        path = ROOT / name
        try:
            info = path.lstat()
        except FileNotFoundError:
            if name == '.asa/runtime-configuration.json':
                continue
            raise
        if not stat.S_ISREG(info.st_mode) or info.st_uid != owner:
            raise SystemExit(f'Не подтверждён файл установки H03 {name}')
        if info.st_mode & 0o022:
            targets[path] = stat.S_IMODE(info.st_mode)
    return targets


def approved_command(path, new, allow_previous=False):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise SystemExit(f'Не подтверждён существующий исполнитель {path}')
    body = path.read_bytes()
    approved_previous = allow_previous and hashlib.sha256(body).hexdigest() == PREVIOUS
    if body != new and not approved_previous:
        raise SystemExit(f'Найдена другая команда {path}')
    return body


def atomic_command(path, content):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
        stage = Path(temporary.name)
        try:
            temporary.write(content)
            temporary.flush()
            os.fchmod(temporary.fileno(), 0o755)
            os.fsync(temporary.fileno())
            stage.replace(path)
        finally:
            stage.unlink(missing_ok=True)


def install_tools(source, targets, previous, wrapper):
    TOOLS_BIN.mkdir(mode=0o755, exist_ok=True)
    # The upstream installer validates the separate root-owned checkout, canonical
    # origin and Git revision before publishing the pinned tool launcher.
    subprocess.run([str(source / 'tools/install-asa-command.sh'), '--production'],
                   env={**os.environ, 'ASA_INSTALL_ROOT': str(ROOT), 'ASA_BIN_DIR': str(TOOLS_BIN)},
                   check=True, timeout=45)
    owner = ['/usr/bin/sudo', '-n', '-H', '-u', 'admins', '--']
    adjusted = []
    try:
        for path, mode in targets.items():
            subprocess.run(owner + ['/usr/bin/chmod', f'{mode & ~0o022:o}', str(path)], check=True, timeout=15)
            adjusted.append((path, mode))
        command = owner + [str(TOOLS_BIN / 'asa')]
        verification = subprocess.check_output(command + ['--launcher-verify', str(ROOT)], text=True, timeout=30)
        if verification.strip() != 'ASA_LAUNCHER_FORWARD_V1':
            raise SystemExit('Не подтверждён постоянный обновлятор H03')
        subprocess.run(command + ['check'], check=True, timeout=160)
    except (OSError, subprocess.SubprocessError, SystemExit):
        for path, mode in reversed(adjusted):
            subprocess.run(owner + ['/usr/bin/chmod', f'{mode:o}', str(path)], check=True, timeout=15)
        raise
    if previous is not None and previous != wrapper:
        HISTORY.mkdir(mode=0o700, exist_ok=True)
        info = HISTORY.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
            raise SystemExit('Небезопасный каталог истории H03')
        saved = HISTORY / ('asa-' + PREVIOUS[:12] + '.before')
        if not saved.exists():
            with saved.open('xb') as stream:
                stream.write(previous)
        elif saved.read_bytes() != previous:
            raise SystemExit('Архив предыдущей команды H03 не совпадает')
    atomic_command(BIN / 'asa', wrapper)
    atomic_command(BIN / 'asa-test', wrapper)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True, help='Trusted, separate ASA Lab tools checkout')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if os.geteuid() != 0 or socket.gethostname() != HOST:
        raise SystemExit('Требуется root на тестовом сервере H03')
    os.umask(0o022)
    source = args.source.resolve(strict=True)
    api = PanelAPI('http://127.0.0.1:19192', read_api_key('/etc/server-settings/panel-domain-publisher.key'))
    require_local_write(api)
    commands = runpy.run_path(str(Path(__file__).with_name('install-school-1panel-operator-shortcuts.py')))
    menu = {commands['UPDATE_GROUP']: tuple((name, 'sudo asa-test ' + action)
                                             for name, action in commands['UPDATE_ACTIONS'].items())}
    wrapper = WRAPPER.read_bytes()
    with open('/run/lock/asa-h03-short-commands.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        targets = checked_targets()
        previous = approved_command(BIN / 'asa', wrapper, allow_previous=True)
        approved_command(BIN / 'asa-test', wrapper)
        commands['reconcile'](api, {}, False, menu=menu)
        print(f'file_modes_to_tighten={len(targets)}', flush=True)
        if args.apply:
            install_tools(source, targets, previous, wrapper)
            commands['reconcile'](api, {}, True, menu=menu)


if __name__ == '__main__':
    main()
