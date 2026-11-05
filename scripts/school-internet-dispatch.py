#!/usr/bin/python3 -I
"""Owner CLI dispatcher. Versioned beside the reviewed offline/runtime sources."""
import importlib.util
import os
from pathlib import Path
import stat
import subprocess
import sys

BASE = Path(__file__).absolute().parent


def trust(path):
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        value = current.lstat()
        if stat.S_ISLNK(value.st_mode) or value.st_uid != 0 or value.st_mode & 0o022:
            raise RuntimeError('UNTRUSTED_DISPATCH_SOURCE')


def runtime(arguments):
    target = BASE / 'school-uplink-runtime.py'
    trust(target)
    return subprocess.run(['/usr/bin/python3', '-I', str(target), *arguments], check=True)


def main():
    trust(Path(__file__).absolute())
    source = BASE / 'school-internet-offline.py'
    trust(source)
    spec = importlib.util.spec_from_file_location('school_offline', source)
    offline = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(offline)
    offline.require_host()
    arguments = sys.argv[1:]
    if not arguments:
        if not sys.stdin.isatty():
            arguments = ['status']
        else:
            print('1 Сохранить подключение; 2 Статус; 3 Проверить подключённое оборудование; 4 Включить автопереключение; 5 Выключить')
            choice = input('Выбор: ').strip()
            arguments = {'1': ['setup', '--apply'], '2': ['status'], '4': ['arm', '--apply'], '5': ['disarm', '--apply']}.get(choice)
            if choice == '3':
                mode = {'1': 'wifi', '2': 'router', '3': 'modem'}.get(input('1 Wi-Fi WPA2; 2 SIM-роутер; 3 USB Ethernet/раздача телефона: ').strip())
                if mode:
                    arguments = ['qualify', '--mode', mode, '--apply']
            if not arguments:
                return
    action = arguments[0]
    if action in ('status', 'qualify', 'arm', 'disarm', 'recover'):
        if action == 'disarm' and '--apply' in arguments:
            subprocess.run(['systemctl', 'disable', '--now', 'asa-school-uplink.service'], check=True)
        runtime(arguments)
        if action == 'arm' and '--apply' in arguments:
            try:
                subprocess.run(['systemctl', 'enable', '--now', 'asa-school-uplink.service'], check=True)
            except BaseException:
                runtime(['disarm', '--apply'])
                raise
        return
    if action == 'setup' and '--apply' in arguments:
        data = offline.read_profiles()  # Refuses armed state; disarm explicitly.
        if Path('/var/lib/asa-school-internet/journal.json').exists():
            runtime(['disarm', '--apply'])
        if not sys.stdin.isatty():
            raise RuntimeError('INTERACTIVE_TERMINAL_REQUIRED')
        print('1 Wi-Fi WPA2-Personal; 2 SIM-роутер Ethernet; 3 USB HiLink/раздача телефона')
        choice = input('Выбор: ').strip()
        if choice == '2':
            rows = []
            for interface in offline.NICS:
                result = subprocess.run(['/usr/sbin/ip', '-j', 'link', 'show', 'dev', interface], capture_output=True, text=True)
                if result.returncode:
                    continue
                import json
                link = json.loads(result.stdout)[0]
                if link.get('master') or 'UP' in link.get('flags', []):
                    continue
                try:
                    carrier = Path('/sys/class/net', interface, 'carrier').read_text().strip() == '1'
                except OSError:
                    carrier = None
                rows.append((interface, carrier))
            if not rows:
                raise RuntimeError('NO_FREE_DOWN_PORTS')
            print('Свободные порты: ' + ', '.join(name + (' (кабель обнаружен)' if carrier else ' (кабель не подтверждён)') for name, carrier in rows))
            connected = [name for name, carrier in rows if carrier]
            selected = connected[0] if len(connected) == 1 else input('Порт из списка: ').strip()
            if selected not in [name for name, _ in rows]:
                raise RuntimeError('FREE_PORT_REQUIRED')
            offline.save({'mode': 'router', 'interface': selected, 'qualified': False})
        elif choice == '1':
            import getpass
            offline.save({'mode': 'wifi', 'ssid': input('SSID: '), 'password': getpass.getpass('Пароль WPA2 (скрыт): '), 'qualified': False})
        elif choice == '3':
            print('Адрес автоматически по DHCP. APN — в модеме/телефоне; PPP/MBIM/locked SIM требуют отдельной настройки. PIN не запрашивается.')
            offline.save({'mode': 'modem', 'apn': input('APN (необязательно, Enter пропустить): ').strip(), 'qualified': False})
        else:
            return
        print('Сохранено приватно. НЕ ПОДКЛЮЧЕНО/НЕ ВКЛЮЧЕНО: сначала проверка оборудования, затем arm.')
        return
    sys.argv = [str(source), *arguments]
    offline.main()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        raise SystemExit('STOP: действие не завершено; сеть не чинится автоматически, секреты не выводятся') from None
