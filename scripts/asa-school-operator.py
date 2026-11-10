#!/usr/bin/python3 -IB
"""Small, host-locked entry points for the school 1Panel terminal."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys

HOST = 'asa-school-01'
ROOT = Path('/srv/asa-lab')
TOOLS = Path('/opt/asa-school-release-tools')
APPROVAL = Path('/etc/asa-school/approved-release.json')
PYTHON = '/usr/bin/python3'
GFS = '/usr/local/bin/asa-school-gfs'
SHA = r'[0-9a-f]{40}'
PROBE = '''
import contextlib, json, sys
from pathlib import Path
from deployment.system import Installation
from deployment.releases import discover_release
with contextlib.redirect_stdout(sys.stderr):
    if sys.argv[1] == 'installed':
        release = Installation(Path('/srv/asa-lab'), 'production').readiness()
    else:
        release = discover_release(*sys.argv[2:])
print('ASA_OPERATOR_RESULT=' + json.dumps({k: release[k] for k in ('revision', 'schema')}))
'''


class Blocked(Exception):
    pass


def trusted(path):
    for item in (path, *path.parents):
        info = item.lstat()
        if item.is_symlink() or info.st_uid != 0 or info.st_mode & 0o022:
            raise Blocked('Администратору нужно проверить права на служебные файлы ASA Lab.')


def approval():
    if not APPROVAL.exists():
        return None
    trusted(APPROVAL)
    if not APPROVAL.is_file() or APPROVAL.stat().st_size > 4096:
        raise Blocked('Допуск выпуска для школы повреждён. Обратитесь к администратору.')
    try:
        value = json.loads(APPROVAL.read_text(encoding='utf-8'))
    except (UnicodeError, ValueError):
        raise Blocked('Допуск выпуска для школы повреждён. Обратитесь к администратору.') from None
    fields = {'fromRevision': SHA, 'toRevision': SHA, 'toolsRevision': SHA,
              'deploymentDigest': r'sha256:[0-9a-f]{64}'}
    if (not isinstance(value, dict) or set(value) != set(fields)
            or any(not isinstance(value[key], str) or not re.fullmatch(pattern, value[key])
                   for key, pattern in fields.items())
            or value['fromRevision'] == value['toRevision']):
        raise Blocked('Допуск выпуска для школы повреждён. Обратитесь к администратору.')
    return value


def json_command(args, message, timeout, prefix=''):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode:
        raise Blocked(message)
    try:
        raw = result.stdout
        if prefix:
            raw = next((line[len(prefix):] for line in reversed(raw.splitlines()) if line.startswith(prefix)), '')
        value = json.loads(raw)
    except ValueError:
        raise Blocked(message) from None
    if not isinstance(value, dict):
        raise Blocked(message)
    return value


def probe(action, checkout=ROOT, *arguments):
    tool_path = checkout / 'tools'
    trusted(tool_path)
    return json_command([PYTHON, '-I', '-B', '-c',
                         'import sys; sys.path.insert(0, ' + repr(str(tool_path)) + ')\n' + PROBE,
                         action, *arguments],
                        'Не удалось узнать версию ASA Lab. Повторите проверку или откройте журналы.',
                        90, 'ASA_OPERATOR_RESULT=')


def check_update():
    current = probe('installed')
    print(f"Установлена версия {current['revision'][:8]} (схема базы {current['schema']}).", flush=True)
    latest = probe('published')
    if latest['revision'] == current['revision']:
        print('Установлен последний опубликованный выпуск.')
    else:
        print(f"Опубликован выпуск {latest['revision'][:8]} (схема базы {latest['schema']}).")
    permit = approval()
    if permit and permit['fromRevision'] == current['revision']:
        print(f"Для школы допущен выпуск {permit['toRevision'][:8]}. Выберите «Установить обновление».")
    elif latest['revision'] != current['revision']:
        print('Для школы пока нет допущенного обновления. Сначала проверяется возврат на тестовом сервере.')
    return 0


def manager(permit):
    script = TOOLS / 'tools/asa_manager.py'
    if not script.is_file():
        raise Blocked('Администратору нужно подготовить инструменты обновления и возврата.')
    trusted(script)
    git = ['/usr/bin/git', '-C', str(TOOLS)]
    if (subprocess.check_output([*git, 'rev-parse', 'HEAD'], text=True).strip() != permit['toolsRevision']
            or subprocess.check_output([*git, 'status', '--porcelain'], text=True).strip()):
        raise Blocked('Инструменты обновления отличаются от проверенной версии. Обратитесь к администратору.')
    for folder in (TOOLS / 'tools', TOOLS / 'apps/api/src'):
        trusted(folder)
        result = subprocess.run(['/usr/bin/find', '-P', str(folder), '(', '-type', 'l', '-o',
                                 '!', '-user', 'root', '-o', '-perm', '/022', ')', '-print', '-quit'],
                                capture_output=True, text=True, check=True)
        if result.stdout:
            raise Blocked('Администратору нужно проверить права на инструменты обновления.')
    return [PYTHON, '-E', '-s', '-B', str(script)]


def run_manager(command, action, *arguments):
    return subprocess.run([*command, action, '--root', str(ROOT), '--profile', 'production',
                           *arguments], check=False).returncode


def confirm(message):
    try:
        if input(f'{message}\nДля продолжения введите ДА: ').strip().upper() == 'ДА':
            return True
    except EOFError:
        pass
    print('Операция отменена.')
    return False


def fresh_backup():
    message = 'Нет подтверждённой полной копии за последние 36 часов. Сначала создайте полную копию.'
    status = json_command([GFS, 'status', '--json'], message, 45)
    try:
        at = datetime.fromisoformat(status['full']['lastVerified'])
        age = datetime.now(timezone.utc) - at
    except (KeyError, ValueError, TypeError):
        raise Blocked(message) from None
    if not timedelta() <= age <= timedelta(hours=36):
        raise Blocked(message)


def install_update():
    permit = approval()
    if permit is None:
        raise Blocked('Для школы пока нет допущенного обновления. Сначала проверяется возврат на тестовом сервере.')
    current = probe('installed')
    if current['revision'] == permit['toRevision']:
        print('Допущенный выпуск уже установлен.')
        return 0
    if current['revision'] != permit['fromRevision']:
        raise Blocked('Допуск относится к другой установленной версии. Обратитесь к администратору.')
    command = manager(permit)
    digest = permit['deploymentDigest']
    if probe('exact', TOOLS, digest)['revision'] != permit['toRevision']:
        raise Blocked('Опубликованный выпуск не совпадает с допуском для школы.')
    arguments = ('--require-application-rollback', '--deployment-digest', digest)
    if run_manager(command, 'check', *arguments):
        raise Blocked('Предварительная проверка обновления не пройдена. Причина указана выше.')
    fresh_backup()
    if not confirm('Обновление временно прервёт работу ASA Lab. Запускайте вне уроков.'):
        return 0
    if approval() != permit or probe('installed')['revision'] != permit['fromRevision']:
        raise Blocked('Пока вы подтверждали, установленная версия или допуск изменились. Повторите проверку.')
    fresh_backup()
    return run_manager(command, 'update', *arguments)


def rollback_plan():
    permit = approval()
    if permit is None:
        raise Blocked('Плана возврата пока нет. Он создаётся при установке допущенного обновления.')
    return run_manager(manager(permit), 'rollback-plan')


def backup_full():
    if not confirm('Полная копия базы и файлов временно прервёт работу ASA Lab. Запускайте вне уроков.'):
        return 0
    return subprocess.run([GFS, 'full'], check=False).returncode


def main():
    actions = {'check-update': check_update, 'install-update': install_update,
               'rollback-plan': rollback_plan, 'backup-full': backup_full}
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=actions)
    args = parser.parse_args()
    if os.geteuid() != 0 or socket.gethostname() != HOST:
        print('Откройте терминал школьного сервера и запустите команду через sudo.', file=sys.stderr)
        return 2
    try:
        return actions[args.action]()
    except Blocked as error:
        print(error, file=sys.stderr)
        return 2
    except (OSError, subprocess.SubprocessError):
        print('Не удалось завершить проверку ASA Lab. Откройте журналы или обратитесь к администратору.',
              file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
