#!/usr/bin/env python3
"""Install the separate manual backup shortcut on school VM100.

Does not edit the scheduled timer, cloud OAuth, databases, systemd units,
existing backup files or live Docker services. Will not overwrite any
nonmatching existing local command.
"""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import shutil
import socket
import stat
import subprocess
import sys

HOST = "asa-school-01"
SOURCE = Path("/srv/Server-setings/scripts/asa-school-backup-now.sh")
BACKUP = Path("/srv/Server-setings/scripts/school-nightly-database-backup.py")
DESTINATION = Path("/usr/local/sbin/asa-school-backup-now")
TIMER = "asa-school-nightly-database-backup.timer"


class InstallerError(Exception):
    pass


def verify():
    if os.geteuid() != 0 or socket.gethostname() != HOST:
        raise InstallerError("VM100_ROOT_REQUIRED")
    for path in (SOURCE, BACKUP):
        if path.is_symlink() or not path.is_file() or path.stat().st_uid != 0:
            raise InstallerError("SCHOOL_GITHUB_SOURCE_UNTRUSTED")
    subprocess.run(["/usr/bin/bash", "-n", str(SOURCE)], timeout=8,
                   check=True, capture_output=True)
    subprocess.run(["/usr/bin/python3", "-m", "py_compile", str(BACKUP)],
                   timeout=8, check=True, capture_output=True)
    for action in ("is-enabled", "is-active"):
        result = subprocess.run(["/usr/bin/systemctl", action, TIMER],
                                capture_output=True, text=True, timeout=8,
                                check=False)
        expected = "enabled" if action == "is-enabled" else "active"
        if result.returncode != 0 or result.stdout.strip() != expected:
            raise InstallerError("NIGHTLY_TIMER_MUST_REMAIN_ACTIVE")


def install():
    verify()
    if DESTINATION.exists() or DESTINATION.is_symlink():
        info = DESTINATION.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                or stat.S_IMODE(info.st_mode) != 0o755
                or DESTINATION.read_bytes() != SOURCE.read_bytes()):
            raise InstallerError("EXISTING_MANUAL_BACKUP_COMMAND_UNTRUSTED")
        print("MANUAL_BACKUP_COMMAND_ALREADY_INSTALLED=YES")
        return
    descriptor = os.open(DESTINATION, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                         os.O_NOFOLLOW | os.O_CLOEXEC, 0o755)
    try:
        with os.fdopen(descriptor, "wb") as stream, SOURCE.open("rb") as source:
            os.fchmod(stream.fileno(), 0o755)
            shutil.copyfileobj(source, stream)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        DESTINATION.unlink(missing_ok=True)
        raise
    print("MANUAL_BACKUP_COMMAND_INSTALLED=YES")
    print("COMMAND=sudo asa-school-backup-now")
    print("MANUAL_BACKUP_ALLOWED_ANY_TIME=YES")
    print("NIGHT_TIMER_UNMODIFIED=YES")
    print("LIVE_DATABASE_BACKUP_NOT_TRIGGERED_DURING_INSTALL=YES")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        verify()
        if args.apply:
            install()
        else:
            print("PLAN: install /usr/local/sbin/asa-school-backup-now, no backup initiated.")
        return 0
    except (InstallerError, OSError, ValueError, subprocess.SubprocessError) as exc:
        code = str(exc) if isinstance(exc, InstallerError) else type(exc).__name__
        print("MANUAL_BACKUP_INSTALL_BLOCKED="+code, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
