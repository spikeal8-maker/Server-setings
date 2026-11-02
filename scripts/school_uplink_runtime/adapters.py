import hashlib
import os
from pathlib import Path
import signal
import time

from .network import check_device, interface_name, lease_from_environment
from .storage import atomic, load, private_directory, require, save, Stop


def profile_digest(profile):
    import json
    # Private proof only: never print/publish a digest of SSID/password.
    return hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()


def process_identity(pid):
    try:
        return Path(f'/proc/{int(pid)}/stat').read_text().rsplit(')', 1)[1].split()[19]
    except (OSError, ValueError):
        return None


def stop_process(record):
    pidfile = Path(record['pidfile'])
    if 'pid' not in record:
        if not pidfile.exists():
            return
        require(pidfile.is_file() and not pidfile.is_symlink() and pidfile.stat().st_uid == 0, 'OWNED_PROCESS_PIDFILE')
        pid = int(pidfile.read_text().strip())
        if process_identity(pid) is None:
            return
        argv = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
        require(record['marker'].encode() in argv and Path(f'/proc/{pid}/exe').resolve().name == record['executable'], 'PROCESS_INTENT_IDENTITY')
        record = dict(record, pid=pid, start=process_identity(pid))
    if process_identity(record['pid']) == record['start']:
        os.kill(record['pid'], signal.SIGTERM)
        for _ in range(30):
            if process_identity(record['pid']) != record['start']:
                return
            time.sleep(0.1)
        require(False, 'OWNED_PROCESS_DID_NOT_STOP')


def select_interface(profile, commands, sysfs=Path('/sys/class/net')):
    if profile['mode'] == 'router':
        require(profile.get('interface') in ('nic0', 'nic2', 'nic3'), 'ROUTER_PORT')
        result = profile['interface']
    else:
        candidates = []
        for item in sysfs.iterdir():
            if item.name in ('lo', 'vmbr0', 'nic1') or (item / 'master').exists():
                continue
            if profile['mode'] == 'wifi' and (item / 'wireless').is_dir():
                candidates.append(item.name)
            if profile['mode'] == 'modem' and (item / 'device/driver').exists():
                # HiLink/USB Ethernet tether only. Serial/PPP/MBIM needs an
                # individually validated bearer, not guessed APN/PIN commands.
                if (item / 'device/driver').resolve().name in ('rndis_host', 'cdc_ether', 'cdc_ncm'):
                    candidates.append(item.name)
        require(len(candidates) == 1, 'ONE_ELIGIBLE_DEVICE_REQUIRED: connect one free WiFi/USB Ethernet device; locked SIM or serial/PPP/MBIM requires individual setup')
        result = candidates[0]
    return check_device(commands, interface_name(result), sysfs)


def wifi_configuration(ssid, password):
    require(isinstance(ssid, str) and 1 <= len(ssid.encode()) <= 32, 'WIFI_SSID')
    require(isinstance(password, str) and 8 <= len(password) <= 63, 'WIFI_PASSWORD_8_63')
    psk = hashlib.pbkdf2_hmac('sha1', password.encode(), ssid.encode(), 4096, 32).hex()
    # SSID hex and derived PSK avoid config/line/shell injection and argv secrets.
    return f'update_config=0\nnetwork={{\n ssid={ssid.encode().hex()}\n psk={psk}\n key_mgmt=WPA-PSK\n}}\n'.encode()


def start_adapter(profile, device, commands, state, remember, hook):
    interface = device['interface']
    if not device['was_up']:
        remember(['ip', 'link', 'set', 'dev', interface, 'up'], ['ip', 'link', 'set', 'dev', interface, 'down'])
    if profile['mode'] == 'wifi':
        config = state / 'wifi.conf'
        atomic(config, wifi_configuration(profile['ssid'], profile['password']))
        pidfile = state / 'wifi.pid'
        require(not pidfile.exists(), 'STALE_WIFI_PID')
        remember(None, {'pidfile': str(pidfile), 'marker': str(config), 'executable': 'wpa_supplicant'})
        commands.call('wpa_supplicant', '-B', '-i', interface, '-c', str(config), '-P', str(pidfile))
    session = state / f'dhcp-{interface}.json'
    proposal = state / f'lease-{interface}.json'
    if proposal.exists():
        proposal.unlink()
    save(session, {'interface': interface, 'created': time.time()})
    pidfile = state / 'dhclient.pid'
    require(not pidfile.exists(), 'STALE_DHCP_PID')
    remember(None, {'pidfile': str(pidfile), 'marker': str(pidfile), 'executable': 'dhclient'})
    # -sf replaces dhclient-script entirely: no stock route/resolvconf hook runs.
    commands.call('dhclient', '-4', '-1', '-sf', str(hook), '-pf', str(pidfile), '-lf', str(state / 'dhclient.leases'), interface)
    require(proposal.exists(), 'NO_VALID_DHCP_LEASE')
    lease = load(proposal)
    require(lease['interface'] == interface and lease['expires'] > time.time() + 30, 'LEASE_EXPIRED')
    return lease


def remember_process(pidfile, remember):
    require(pidfile.is_file() and not pidfile.is_symlink(), 'PROCESS_PID_MISSING')
    pid = int(pidfile.read_text().strip())
    start = process_identity(pid)
    require(start is not None, 'PROCESS_IDENTITY')
    remember(None, {'pid': pid, 'start': start, 'pidfile': str(pidfile)})


def dhcp_hook(state, env):
    state = private_directory(state)
    interface = interface_name(env.get('interface'))
    session = load(state / f'dhcp-{interface}.json')
    require(session['interface'] == interface and time.time() - session['created'] < 604800, 'DHCP_SESSION')
    if env.get('reason') in ('PREINIT', 'TIMEOUT', 'FAIL', 'EXPIRE', 'RELEASE', 'STOP'):
        save(state / f'lease-{interface}.json', {'interface': interface, 'expires': 0})
        return
    lease = lease_from_environment(env, interface)
    save(state / f'lease-{interface}.json', lease)
