#!/usr/bin/env python3
"""Key-only SSH with a timed rollback; explicit confirmation after remote tests."""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess

TARGET = Path('/etc/ssh/sshd_config.d/00-asa-security.conf')
STATE = Path('/var/backups/asa-ssh-hardening')
UNIT = 'asa-ssh-security-rollback'
CONTENT = '''# Managed ASA server security; forwarding remains available to its owner.
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
PermitRootLogin no
X11Forwarding no
MaxAuthTries 3
LoginGraceTime 30
'''


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def rollback():
    record = json.loads((STATE / 'before.json').read_text())
    if record['existed']:
        TARGET.write_text(record['content'])
        TARGET.chmod(record['mode'])
    else:
        TARGET.unlink(missing_ok=True)
    run('/usr/sbin/sshd', '-t')
    run('systemctl', 'reload', 'ssh.service')
    (STATE / 'pending').unlink(missing_ok=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--action', choices=['plan', 'apply', 'confirm', 'rollback'], default='plan')
    p.add_argument('--expected-host')
    p.add_argument('--verified-owner-key', action='store_true')
    a = p.parse_args()
    if a.action == 'plan':
        print(json.dumps({'plan': True, 'password_ssh': 'disable', 'root_login': 'disable',
                          'key_ssh': 'preserve', 'forwarding': 'unchanged', 'rollback_seconds': 180}))
        return
    assert os.geteuid() == 0 and a.expected_host == socket.gethostname()
    assert TARGET.parent.resolve() == TARGET.parent and not TARGET.is_symlink()
    assert not STATE.is_symlink()
    STATE.mkdir(mode=0o700, exist_ok=True)
    assert STATE.stat().st_uid == 0 and STATE.stat().st_mode & 0o077 == 0
    # Keep this descriptor locked for the lifetime of this standalone CLI process.
    import fcntl
    lock_fd = os.open(STATE / '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    for name in ['pending', 'before.json']:
        assert not (STATE / name).is_symlink()
    if a.action == 'confirm':
        assert (STATE / 'pending').exists(), 'No pending change'
        # Caller must first test a NEW key session and independent full-root MCP.
        run('systemctl', 'stop', UNIT + '.timer')
        (STATE / 'pending').unlink()
        print('SSH hardening confirmed; timed rollback disarmed')
        return
    if a.action == 'rollback':
        if (STATE / 'pending').exists():
            rollback()
        return
    assert a.verified_owner_key, 'Verify a new owner key session before apply'
    assert not (STATE / 'pending').exists(), 'Change already pending; inspect first'
    script = Path(__file__).resolve()
    assert script.stat().st_uid == 0 and not script.stat().st_mode & 0o022
    for parent in script.parents:
        assert parent.stat().st_uid == 0 and not parent.stat().st_mode & 0o022, 'Untrusted timer script parent'
    run('/usr/sbin/sshd', '-t')
    record = {'existed': TARGET.exists(), 'content': TARGET.read_text() if TARGET.exists() else '',
              'mode': TARGET.stat().st_mode & 0o777 if TARGET.exists() else 0o644}
    (STATE / 'before.json').write_text(json.dumps(record))
    (STATE / 'before.json').chmod(0o600)
    (STATE / 'pending').touch(mode=0o600)
    run('systemd-run', '--unit=' + UNIT, '--on-active=180s', '/usr/bin/python3', str(script),
        '--action', 'rollback', '--expected-host', a.expected_host)
    try:
        TARGET.write_text(CONTENT)
        TARGET.chmod(0o644)
        run('/usr/sbin/sshd', '-t')
        effective = dict(line.split(' ', 1) for line in run('/usr/sbin/sshd', '-T').splitlines())
        assert all(effective[k] == v for k, v in {
            'passwordauthentication': 'no', 'kbdinteractiveauthentication': 'no',
            'permitrootlogin': 'no', 'pubkeyauthentication': 'yes', 'x11forwarding': 'no'}.items())
        run('systemctl', 'reload', 'ssh.service')
    except BaseException:
        rollback()
        raise
    print('Applied; verify NEW key-only SSH and full MCP, then confirm within180s')


if __name__ == '__main__':
    main()
