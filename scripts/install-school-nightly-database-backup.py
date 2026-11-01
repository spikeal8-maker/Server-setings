#!/usr/bin/env python3
"""Install a fail-closed Moscow-night-only ASA School full backup timer.

This changes no application containers, database data, network, OAuth,
Tailscale, 1Panel or existing schedules. A backup may start at 03:20 Europe/
Moscow only after a read-only Yandex app-folder upload-slot preflight succeeds.
The timer never catches up after boot at a daytime hour.
"""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys

HOST="asa-school-01"
NAME="asa-school-nightly-database-backup"
ROOT=Path("/var/backups/asa-school-nightly")
RUN=ROOT/"staging"
ARCHIVES=ROOT/"archives"
SCRIPT=Path("/srv/Server-setings/scripts/school-nightly-database-backup.py")
SERVICE=Path("/etc/systemd/system")/(NAME+".service")
TIMER=Path("/etc/systemd/system")/(NAME+".timer")
SCHEDULE="*-*-* 03:20:00 Europe/Moscow"

SERVICE_BODY="""[Unit]
Description=ASA Lab School nightly consistent PostgreSQL+MinIO encrypted backup
Wants=network-online.target
After=network-online.target docker.service
Requires=docker.service
ConditionPathExists=/etc/server-settings/age-recipients.txt

[Service]
Type=oneshot
User=root
Group=root
WorkingDirectory=/srv/asa-lab
Environment=PYTHONDONTWRITEBYTECODE=1
UMask=0077
ExecStart=/usr/bin/python3 /srv/Server-setings/scripts/school-nightly-database-backup.py --apply
Nice=10
IOSchedulingClass=best-effort
IOSchedulingPriority=7
TimeoutStartSec=infinity
TimeoutStopSec=infinity
NoNewPrivileges=yes
PrivateTmp=yes
"""

TIMER_BODY="""[Unit]
Description=ASA Lab School encrypted backups - daily at 03:20 Moscow only

[Timer]
OnCalendar=*-*-* 03:20:00 Europe/Moscow
Persistent=false
RandomizedDelaySec=0
AccuracySec=1min
Unit=asa-school-nightly-database-backup.service

[Install]
WantedBy=timers.target
"""


class InstallError(Exception):
    pass


def checked_host():
    if os.geteuid()!=0 or socket.gethostname()!=HOST:
        raise InstallError("VM100_ROOT_REQUIRED")
    if SCRIPT.is_symlink() or not SCRIPT.is_file() or SCRIPT.stat().st_uid!=0:
        raise InstallError("TRUSTED_REPOSITORY_SCRIPT_MISSING")


def make_directory(path):
    if path.is_symlink():raise InstallError("DIRECTORY_SYMLINK")
    if not path.exists():
        path.mkdir(mode=0o700)
        os.chown(path,0,0)
        os.chmod(path,0o700)
    if not path.is_dir() or path.stat().st_uid!=0 or stat.S_IMODE(path.stat().st_mode)!=0o700:
        raise InstallError("DIRECTORY_OWNER_MODE_INVALID")


def run(args, timeout=35):
    result=subprocess.run(args,capture_output=True,text=True,timeout=timeout,check=False)
    if result.returncode:
        raise InstallError("COMMAND_"+Path(str(args[0])).name+"_FAILED")
    return result.stdout


def make_unit(path, text):
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_file() or path.read_text(encoding="utf-8")!=text:
            raise InstallError("EXISTING_SYSTEMD_UNIT_DIFFERENT")
        return False
    descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o644)
    try:
        with os.fdopen(descriptor,"w",encoding="utf-8") as target:
            target.write(text);target.flush();os.fsync(target.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return True


def preflight():
    checked_host()
    # Avoid running unintended jobs from incorrect local time or invalid TZ.
    output=run(["/usr/bin/systemd-analyze","calendar",SCHEDULE],timeout=8)
    if "03:20:00" not in output or "Europe/Moscow" not in output:
        raise InstallError("SYSTEMD_CALENDAR_NOT_VERIFIED")
    run(["/usr/bin/python3",str(SCRIPT),"--preflight"],timeout=55)
    return output


def timer_state():
    output=run(["systemctl","show",TIMER.name,
                "--property=LoadState,UnitFileState,ActiveState"],timeout=10)
    values=dict(line.split("=",1) for line in output.splitlines() if "=" in line)
    if (values.get("LoadState")=="not-found" and
        values.get("ActiveState")=="inactive" and not values.get("UnitFileState")):
        return None
    if (values.get("LoadState")!="loaded" or
        values.get("UnitFileState") not in ("enabled","enabled-runtime","disabled") or
        values.get("ActiveState") not in ("active","inactive")):
        raise InstallError("EXISTING_NIGHT_TIMER_STATE_UNSUPPORTED")
    if not TIMER.is_file() or TIMER.is_symlink():
        raise InstallError("EXISTING_NIGHT_TIMER_OUTSIDE_MANAGED_PATH")
    return values["UnitFileState"],values["ActiveState"]


def restore_timer(previous):
    commands=[]
    if previous is None:
        commands.append(["systemctl","disable","--now",TIMER.name])
    else:
        enabled,active=previous
        if enabled=="enabled-runtime":
            commands.extend([["systemctl","disable",TIMER.name],
                             ["systemctl","enable","--runtime",TIMER.name]])
        else:
            commands.append(["systemctl","enable" if enabled=="enabled" else "disable",TIMER.name])
        commands.append(["systemctl","start" if active=="active" else "stop",TIMER.name])
    failed=False
    for command in commands:
        try:run(command,timeout=15)
        except (InstallError,OSError,subprocess.SubprocessError):failed=True
    if previous is not None:
        try:failed=timer_state()!=previous or failed
        except (InstallError,OSError,subprocess.SubprocessError):failed=True
    if failed:raise InstallError("NIGHT_TIMER_STATE_RESTORE_FAILED")


def install():
    checked_host()
    for folder in (ROOT,RUN,ARCHIVES):make_directory(folder)
    preflight()
    previous=timer_state()
    changed=[]
    activation_attempted=False
    try:
        if make_unit(SERVICE,SERVICE_BODY):changed.append(SERVICE)
        if make_unit(TIMER,TIMER_BODY):changed.append(TIMER)
        run(["systemctl","daemon-reload"],timeout=12)
        if previous!=("enabled","active"):
            activation_attempted=True
            run(["systemctl","enable","--now",TIMER.name],timeout=18)
        enabled=run(["systemctl","is-enabled",TIMER.name]).strip()
        active=run(["systemctl","is-active",TIMER.name]).strip()
        if enabled!="enabled" or active!="active":
            raise InstallError("NIGHT_TIMER_NOT_ACTIVE")
        next_runs=run(["systemctl","list-timers","--all","--no-pager",
                       TIMER.name],timeout=12)
        if TIMER.name not in next_runs:
            raise InstallError("NIGHT_TIMER_NOT_LISTED")
    except BaseException:
        rollback_failed=False
        if activation_attempted:
            try:restore_timer(previous)
            except (InstallError,OSError,subprocess.SubprocessError):rollback_failed=True
        for path in changed:
            try:path.unlink(missing_ok=True)
            except OSError:rollback_failed=True
        if changed:
            try:run(["systemctl","daemon-reload"],timeout=10)
            except (InstallError,OSError,subprocess.SubprocessError):rollback_failed=True
        if rollback_failed:print("NIGHT_TIMER_ROLLBACK_INCOMPLETE=YES",file=sys.stderr)
        raise
    print("NIGHTLY_ASA_POSTGRES_MINIO_TIMER_ENABLED=YES")
    print("NIGHTLY_ASA_SCHEDULE=03:20_EUROPE_MOSCOW_DAILY")
    print("NIGHTLY_ASA_CATCHUP_DAYTIME=DISABLED")
    print("NIGHTLY_ASA_ENCRYPTION_AGE_PUBLIC_ONLY=YES")
    print("NIGHTLY_ASA_YANDEX_DESTINATION=app:/School-VM100/Database-Backups")
    print("NIGHTLY_ASA_DATABASE_BACKUP_RAN_NOW=NO")
    print("NIGHTLY_ASA_ENCRYPTED_RESTORE_TEST=STILL_REQUIRED")


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--apply",action="store_true")
    args=p.parse_args()
    try:
        checked_host()
        if args.apply:
            install()
        else:
            print("PLAN: install age-encrypted daily PostgreSQL+MinIO backup at 03:20 Moscow; no daytime jobs.")
            print("Existing applications, Docker and OAuth not modified by the installer.")
        return 0
    except (InstallError,OSError,ValueError,subprocess.SubprocessError) as e:
        reason=str(e) if isinstance(e,InstallError) else type(e).__name__
        print("NIGHTLY_TIMER_BLOCKED="+reason,file=sys.stderr)
        return 2


if __name__=="__main__":
    raise SystemExit(main())
