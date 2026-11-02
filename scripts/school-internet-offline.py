#!/usr/bin/env python3
"""Offline preparation only. Never connects interfaces or changes routes/DNS."""
import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys
import tempfile

HOST='pve'
ROOT=Path('/etc/asa-school-internet')
PROFILE=ROOT/'profiles.json'
NICS=('nic0','nic2','nic3')
INTERFACES_SHA='e676d55c7ff7e8eaa3cda0896f3caac4209ebe0fa4a56d3c6907a8bc268f0aba'
RESOLV_SHA='3f4c6ebc4d48dbfbd3312cbd06e1a102a0d6767b1e64a3a9949664f24a1af52b'
PACKAGES=('iw','wpasupplicant','modemmanager','usb-modeswitch','mobile-broadband-provider-info')
PACKAGE_VERSIONS={'iw': '6.9-1', 'libduktape207': '2.7.0-2+b2', 'libgudev-1.0-0': '238-6', 'libjim0.83': '0.83-2', 'libmbim-glib4': '1.32.0-1', 'libmbim-proxy': '1.32.0-1', 'libmm-glib0': '1.24.0-1+deb13u1', 'libnl-genl-3-200': '3.7.0-2', 'libpcsclite1': '2.3.3-1', 'libpolkit-agent-1-0': '126-2', 'libpolkit-gobject-1-0': '126-2', 'libqmi-glib5': '1.36.0-1', 'libqmi-proxy': '1.36.0-1', 'libqrtr-glib0': '1.2.2-1+b2', 'mobile-broadband-provider-info': '20250613-2', 'modemmanager': '1.24.0-1+deb13u1', 'polkitd': '126-2', 'sgml-base': '1.31+nmu1', 'usb-modeswitch': '2.6.1-4+b2', 'usb-modeswitch-data': '20191128-7', 'wpasupplicant': '2:2.10-24', 'xml-core': '0.19'}

class Stop(Exception):pass


def trust(path,missing=False):
    path=Path(path)
    if not path.is_absolute() or '..' in path.parts:raise Stop('Нужен абсолютный путь')
    current=Path(path.anchor)
    for part in path.parts[1:]:
        current=current/part
        try:s=current.lstat()
        except FileNotFoundError:
            if missing:return
            raise Stop('Нет обязательного файла') from None
        if stat.S_ISLNK(s.st_mode) or s.st_uid!=0 or s.st_mode&0o022:raise Stop('Небезопасный путь')
        if current!=path and not stat.S_ISDIR(s.st_mode):raise Stop('Небезопасный родитель')


def require_host():
    if os.geteuid()!=0 or socket.gethostname()!=HOST:raise Stop('Запускать только root на школьном PVE')


def private_root():
    trust(ROOT,True)
    ROOT.mkdir(mode=0o700,exist_ok=True)
    if stat.S_IMODE(ROOT.stat().st_mode)!=0o700:raise Stop('Каталог профилей должен иметь права0700')


def atomic_private(path,data):
    trust(path,True)
    fd,name=tempfile.mkstemp(prefix='.offline-',dir=path.parent)
    try:
        os.fchmod(fd,0o600)
        with os.fdopen(fd,'wb') as out:out.write(data);out.flush();os.fsync(out.fileno())
        os.replace(name,path)
        directory=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(directory)
        finally:os.close(directory)
    finally:
        if os.path.exists(name):os.unlink(name)


def read_profiles():
    trust(PROFILE,True)
    if not PROFILE.exists():return {'schema':1,'armed':False,'profiles':[]}
    if stat.S_IMODE(PROFILE.stat().st_mode)!=0o600:raise Stop('Профили должны иметь права0600')
    data=json.loads(PROFILE.read_text())
    if data.get('schema')!=1 or data.get('armed') is not False or not isinstance(data.get('profiles'),list):raise Stop('Неизвестное состояние профилей')
    for profile in data['profiles']:validate(profile)
    return data


def text(value,maxbytes=128):
    if not value or len(value.encode())>maxbytes or any(ord(c)<32 or ord(c)==127 for c in value):raise Stop('Недопустимое значение')
    return value


def validate(profile):
    mode=profile.get('mode')
    if profile.get('qualified') is not False:raise Stop('Офлайн-профиль не является проверенным подключением')
    if mode=='wifi':
        if set(profile)!={'mode','ssid','password','qualified'}:raise Stop('Неизвестные поля Wi-Fi')
        text(profile['ssid'],32);password=text(profile['password'],63)
        if len(password)<8:raise Stop('Нужен WPA2/WPA3 пароль от8до63символов')
    elif mode=='router':
        if set(profile)!={'mode','interface','qualified'} or profile['interface'] not in NICS:raise Stop('Выберите свободный nic0/nic2/nic3')
    elif mode=='modem':
        if set(profile)!={'mode','apn','qualified'} or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]{0,99}',profile['apn']):raise Stop('Недопустимый APN')
    else:raise Stop('Неизвестный тип профиля')


def save(profile):
    import fcntl
    validate(profile);private_root()
    lock=ROOT/'profile.lock';trust(lock,True)
    fd=os.open(lock,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX)
        data=read_profiles()
        data['profiles']=[p for p in data['profiles'] if p['mode']!=profile['mode']]+[profile]
        atomic_private(PROFILE,(json.dumps(data,ensure_ascii=False,indent=2)+'\n').encode())
    finally:os.close(fd)


def baseline():
    for filename,expected in (('/etc/network/interfaces',INTERFACES_SHA),('/etc/resolv.conf',RESOLV_SHA)):
        path=Path(filename);trust(path)
        if not stat.S_ISREG(path.stat().st_mode) or hashlib.sha256(path.read_bytes()).hexdigest()!=expected:raise Stop('Основная конфигурация изменилась: нужна новая диагностика')


def run(command):
    return subprocess.run(command,check=True,capture_output=True,text=True,timeout=120).stdout


def package_plan():
    baseline()
    output=subprocess.run(['apt-get','-s','--no-install-recommends','install',*PACKAGES],check=True,capture_output=True,text=True,timeout=120,env={**os.environ,'LC_ALL':'C'}).stdout
    if re.search(r'(?m)^Remv ',output):raise Stop('APT предлагает удаление пакетов')
    # Inst name [oldversion] denotes upgrade. This preparation may add packages only.
    installs=re.findall(r'(?m)^Inst (\S+)(.*)$',output)
    if any(re.match(r'\s*\[',rest) for _,rest in installs):raise Stop('APT предлагает обновление: нужен отдельный review')
    observed={name:re.match(r'\s*\((\S+)',rest).group(1) if re.match(r'\s*\((\S+)',rest) else None for name,rest in installs}
    if any(PACKAGE_VERSIONS.get(name)!=version for name,version in observed.items()):raise Stop('Новые зависимости/версии: требуется review')
    names=set(observed)
    return {'plan':True,'packages_to_add':sorted(names),'package_apply_supported':True,
            'interfaces_unchanged':True,'dns_unchanged':True,'automatic_failover_implemented':False}


def network_snapshot():
    return {kind:json.loads(run(['/usr/sbin/ip','-j',*args])) for kind,args in
            {'routes':['-4','route','show','table','all'],'rules':['-4','rule','show']}.items()}


def packages_apply():
    import fcntl
    private_root()
    lock=ROOT/'package.lock';trust(lock,True)
    fd=os.open(lock,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        plan=package_plan()
        units=('ModemManager.service','wpa_supplicant.service')
        before={unit:subprocess.run(['systemctl','show',unit,'--property=LoadState','--value'],capture_output=True,text=True,check=True).stdout.strip() for unit in units}
        if any(value!='not-found' for value in before.values()):raise Stop('Служба уже существует: требуется отдельный review')
        snapshot=network_snapshot()
        atomic_private(ROOT/'packages-before.json',(json.dumps(snapshot)+'\n').encode())
        # Preverified absent units only; refuse foreign overrides before creating masks.
        for unit in units:
            for directory in ('/etc/systemd/system','/run/systemd/system','/usr/lib/systemd/system','/lib/systemd/system'):
                candidate=Path(directory)/unit
                if os.path.lexists(candidate):raise Stop('Unit file already exists: separate review required')
        environment=os.environ.copy();environment.update({'SYSTEMD_OFFLINE':'1','DEBIAN_FRONTEND':'noninteractive','LC_ALL':'C'})
        result=None;apt_error=None;cleanup_errors=[];unchanged=False;hashes_unchanged=False
        try:
            # Mask before apt to block D-Bus activation, without forcing any existing override.
            subprocess.run(['systemctl','mask',*units],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=30)
            result=subprocess.run(['apt-get','-y','--no-upgrade','--no-remove','--no-install-recommends','install',*[name+'='+version for name,version in PACKAGE_VERSIONS.items()]],env=environment,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=900)
        except Exception as error:
            apt_error=type(error).__name__
        finally:
            # Even failed/timed-out apt must leave only the preverified new services masked.
            for command in (['systemctl','daemon-reload'],['systemctl','mask','--now',*units]):
                try:subprocess.run(command,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=30)
                except Exception as error:cleanup_errors.append(type(error).__name__)
            for unit in units:
                try:
                    enabled=subprocess.run(['systemctl','is-enabled',unit],capture_output=True,text=True,timeout=15).stdout.strip()
                    active=subprocess.run(['systemctl','is-active',unit],capture_output=True,text=True,timeout=15).stdout.strip()
                    if enabled!='masked' or active not in ('inactive','unknown'):raise Stop('New unit not safely masked/inactive')
                except Exception as error:cleanup_errors.append('unit-proof:'+type(error).__name__)
            try:baseline();hashes_unchanged=True
            except Exception as error:cleanup_errors.append('baseline:'+type(error).__name__)
            try:unchanged=network_snapshot()==snapshot
            except Exception as error:cleanup_errors.append('network:'+type(error).__name__)
            receipt={'schema':1,'exit_code':result.returncode if result else None,'apt_error':apt_error,
                     'cleanup_errors':cleanup_errors,'network_unchanged':unchanged,'primary_hashes_unchanged':hashes_unchanged,
                     'optional_units_masked':not cleanup_errors,'automatic_failover_enabled':False}
            atomic_private(ROOT/'packages-result.json',(json.dumps(receipt)+'\n').encode())
        if result is None or result.returncode or apt_error or cleanup_errors or not unchanged or not hashes_unchanged:
            raise Stop('Установка не принята: проверьте приватную квитанцию')
        print('Пакеты подготовлены; новые службы замаскированы. Сеть не изменена. Автопереключение не включено.')
    finally:os.close(fd)


def status():
    data=read_profiles()
    print('Подготовка резервного интернета: НЕ АКТИВИРОВАНО')
    print('Сохранённые офлайн-профили: '+(', '.join(p['mode'] for p in data['profiles']) or 'нет'))
    print('Профили не подключаются автоматически. Основная сеть и DNS не изменяются.')
    print('Следующий этап: проверка оборудования, policy/DNS manager, изолированные тесты и приёмка.')


def setup():
    if not sys.stdin.isatty():raise Stop('Настройка требует локального интерактивного терминала')
    print('Офлайн-подготовка на PVE. Подключение и автопереключение пока не реализованы.')
    print('1 Wi-Fi; 2 SIM-роутер по Ethernet; 3 USB-модем (APN, без PIN)')
    choice=input('Выберите1/2/3: ').strip()
    if choice=='1':profile={'mode':'wifi','ssid':input('SSID: '),'password':getpass.getpass('Пароль Wi-Fi (скрыт): '),'qualified':False}
    elif choice=='2':profile={'mode':'router','interface':input('Свободный порт nic0/nic2/nic3: ').strip(),'qualified':False}
    elif choice=='3':
        print('Модем сейчас не обнаружен. PIN не запрашивается: заблокированная SIM потребует отдельного безопасного подключения.')
        profile={'mode':'modem','apn':input('APN оператора: ').strip(),'qualified':False}
    else:raise Stop('Выбор отменён')
    save(profile);print('Офлайн-профиль сохранён приватно. Сеть не изменена; автопереключение выключено.')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['status','setup','packages-plan','packages-apply','arm'])
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args();require_host()
    if args.action in ('setup','packages-apply') and not args.apply:
        print('ПЛАН: офлайн-подготовка; для сохранения профиля/установки добавьте --apply. Сеть не изменяется.');return
    if args.action=='arm':raise Stop('STOP: менеджер маршрутов/DNS и аппаратная приёмка ещё не реализованы')
    if args.action=='setup':setup()
    elif args.action=='packages-plan':print(json.dumps(package_plan()))
    elif args.action=='packages-apply':packages_apply()
    else:status()

if __name__=='__main__':
    try:main()
    except (Stop,OSError,ValueError,subprocess.SubprocessError):raise SystemExit('STOP: операция не выполнена; проверьте локальные предпосылки. Секреты не выводятся.') from None
