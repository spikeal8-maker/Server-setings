import hashlib
import os
from pathlib import Path
import signal
import ctypes
import shutil
import select
import time

from .network import check_device, interface_name, lease_from_environment
from .storage import atomic, load, private_directory, require, save, Stop


def profile_digest(profile):
    import json
    # Private proof only: never print/publish a digest of SSID/password.
    return hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()


def process_identity(pid):
    try:
        fields = Path(f'/proc/{int(pid)}/stat').read_text().rsplit(')', 1)[1].split()
        return None if fields[0] == 'Z' else fields[19]
    except (OSError, ValueError):
        return None


def stop_process(record):
    require('boot_id' in record and 'executable' in record, 'PROCESS_PROOF_INCOMPLETE')
    if record['boot_id'] != boot_id() or process_identity(record['pid']) != record['start']:
        return  # Old process is gone; do not signal a reused PID/other boot.
    require(hasattr(os, 'pidfd_open') and hasattr(signal, 'pidfd_send_signal'), 'PIDFD_REQUIRED')
    try:
        fd = os.pidfd_open(record['pid'])
    except ProcessLookupError:
        return
    try:
        if record['boot_id'] != boot_id() or process_identity(record['pid']) != record['start']:
            return
        executable = os.readlink(f"/proc/{record['pid']}/exe")
        state = Path(f"/proc/{record['pid']}/stat").read_text().rsplit(')', 1)[1].split()[0]
        barrier = executable == record.get('barrier_executable') and hashlib.sha256(Path(f"/proc/{record['pid']}/cmdline").read_bytes()).hexdigest() == record.get('barrier_argv')
        require(executable == record['executable'] or barrier, 'FOREIGN_PROCESS_EXECUTABLE')
        signal.pidfd_send_signal(fd, signal.SIGTERM)
        if state == 'T':
            signal.pidfd_send_signal(fd, signal.SIGCONT)
        require(bool(select.select([fd], [], [], 3)[0]), 'OWNED_PROCESS_DID_NOT_STOP')
    finally:
        os.close(fd)


def boot_id():
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def spawn_owned(argv, remember, pidfile=None):
    """Child cannot exec/link-associate before durable PID/start-time ownership."""
    executable = shutil.which(argv[0])
    from .storage import trust
    require(executable is not None, 'REQUIRED_EXECUTABLE_MISSING')
    # /usr/sbin may be a distro symlink; resolve it then validate real ancestors.
    executable = str(Path(executable).resolve())
    trust(executable)
    parent = os.getpid()
    pid = os.fork()
    if pid == 0:
        try:
            libc = ctypes.CDLL(None)
            if libc.prctl(1, signal.SIGKILL) != 0 or os.getppid() != parent:
                os._exit(125)
            os.kill(os.getpid(), signal.SIGSTOP)
            # Journal now owns the stable PID. It may outlive this CLI process.
            if libc.prctl(1, 0) != 0:
                os._exit(125)
            fd = os.open('/dev/null', os.O_RDWR)
            os.umask(0o077)
            for descriptor in (0, 1, 2):
                os.dup2(fd, descriptor)
            os.execve(executable, [executable, *argv[1:]], {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C', 'LC_ALL': 'C'})
        except BaseException:
            os._exit(125)
    try:
        _, status = os.waitpid(pid, os.WUNTRACED)
        require(os.WIFSTOPPED(status), 'CHILD_START_BARRIER')
        remember(None, {'pid': pid, 'start': process_identity(pid), 'pidfile': str(pidfile) if pidfile else '', 'boot_id': boot_id(),
                        'executable': executable, 'barrier_executable': os.readlink('/proc/self/exe'),
                        'barrier_argv': hashlib.sha256(Path('/proc/self/cmdline').read_bytes()).hexdigest()})
        os.kill(pid, signal.SIGCONT)
    except BaseException:
        try:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        except ProcessLookupError:
            pass
        raise


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
    if profile['mode'] == 'wifi':
        config = state / 'wifi.conf'
        atomic(config, wifi_configuration(profile['ssid'], profile['password']))
        pidfile = state / 'wifi.pid'
        require(not pidfile.exists(), 'STALE_WIFI_PID')
        spawn_owned(['wpa_supplicant', '-i', interface, '-c', str(config)], remember)
    session = state / f'dhcp-{interface}.json'
    proposal = state / f'lease-{interface}.json'
    if proposal.exists():
        proposal.unlink()
    save(session, {'interface': interface, 'created': time.time()})
    pidfile = state / 'dhclient.pid'
    require(not pidfile.exists(), 'STALE_DHCP_PID')
    # -sf replaces dhclient-script entirely: no stock route/resolvconf hook runs.
    from .storage import trust
    trust(hook)
    spawn_owned(['dhclient', '-d', '-4', '-1', '-sf', str(hook), '-pf', str(pidfile), '-lf', str(state / 'dhclient.leases'), interface], remember, pidfile)
    for _ in range(120):
        if proposal.exists():
            lease = load(proposal)
            if lease.get('interface') == interface and lease.get('expires_mono', 0) > time.monotonic() + 30:
                return lease
        time.sleep(0.25)
    require(False, 'NO_VALID_DHCP_LEASE')


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
