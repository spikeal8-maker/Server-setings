#!/usr/bin/env python3
"""Apply the vendor MPTCP mitigation without restarting networking. Plan by default."""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess

SETTING = Path('/proc/sys/net/mptcp/enabled')
CONFIG = Path('/etc/sysctl.d/90-asa-no-mptcp.conf')
CONTENT = '# ASA mitigation: https://github.com/1Panel-dev/1Panel/releases/tag/v2.4.2\nnet.mptcp.enabled = 0\n'


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--apply', action='store_true')
    p.add_argument('--expected-host')
    a = p.parse_args()
    before = SETTING.read_text().strip() if SETTING.exists() else None
    if not a.apply or before is None:
        print(json.dumps({'plan': not a.apply, 'supported': before is not None,
                          'current': before, 'target': '0', 'network_restart': False}))
        return
    assert os.geteuid() == 0 and a.expected_host == socket.gethostname()
    assert CONFIG.parent.resolve() == CONFIG.parent and not CONFIG.is_symlink()
    assert CONFIG.parent.stat().st_uid == 0 and not CONFIG.parent.stat().st_mode & 0o022
    if CONFIG.exists():
        assert CONFIG.stat().st_uid == 0 and not CONFIG.stat().st_mode & 0o022
        assert CONFIG.read_text() == CONTENT, 'Existing configuration needs review'
    backup = Path('/var/backups/asa-mptcp-mitigation')
    assert not backup.is_symlink()
    backup.mkdir(mode=0o700, exist_ok=True)
    assert backup.stat().st_uid == 0 and backup.stat().st_mode & 0o077 == 0
    receipt = backup / 'before.json'
    try:
        fd = os.open(receipt, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        assert not receipt.is_symlink() and receipt.stat().st_uid == 0
    else:
        with os.fdopen(fd, 'w') as f:
            json.dump({'enabled': before, 'config_existed': CONFIG.exists()}, f)
    CONFIG.write_text(CONTENT)
    CONFIG.chmod(0o644)
    subprocess.run(['sysctl', '-w', 'net.mptcp.enabled=0'], check=True)
    assert SETTING.read_text().strip() == '0'
    print(json.dumps({'mitigated': True, 'enabled': '0', 'network_restart': False}))


if __name__ == '__main__':
    main()
