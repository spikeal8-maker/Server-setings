#!/usr/bin/env python3
"""Install the two reproducible VM100 encrypted school backup systemd timers.

No ASA runtime update, no old cron changes, no private credentials in units.
The source is an immutable release directory named by its GitHub commit SHA.
"""
from __future__ import annotations
import argparse
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys
import tempfile

HOST = "asa-school-01"
RELEASE_DIR = Path(__file__).resolve().parent
ROOT = Path("/var/backups/asa-school-gfs")
UNIT_DIR = Path("/etc/systemd/system")
OLD_TIMER = "asa-school-nightly-database-backup.timer"
JOBS = {
    "hourly": ("asa-school-gfs-hourly.timer", "asa-school-gfs-hourly.service",
               "*-*-* *:45:00 Europe/Moscow"),
    "full": ("asa-school-gfs-full.timer", "asa-school-gfs-full.service",
             "*-*-* 00:20:00 Europe/Moscow"),
}


class InstallError(RuntimeError):
    pass


def require(condition: bool, code: str) -> None:
    if not condition:
        raise InstallError(code)


def run(args: list[str], timeout: int = 45) -> str:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        raise InstallError("SYSTEM_COMMAND_MISSING_OR_TIMEOUT") from None
    require(p.returncode == 0, "SYSTEM_COMMAND_FAILED")
    return p.stdout.strip()


def source_preflight() -> None:
    require(os.geteuid() == 0 and socket.gethostname() == HOST, "VM100_ROOT_REQUIRED")
    require(RELEASE_DIR.parent.name == "releases" and
            re.fullmatch(r"[a-f0-9]{40}", RELEASE_DIR.name) is not None,
            "RELEASE_DIRECTORY_MUST_BE_GITHUB_SHA")
    for name in ("school-gfs-backup.py", "school-nightly-database-backup.py",
                 "school_yandex_app_folder.py", "cloud_storage.py"):
        p = RELEASE_DIR / name
        require(p.is_file() and not p.is_symlink() and p.stat().st_uid == 0,
                "VERSIONED_DEPENDENCY_MISSING")
    require(Path("/srv/asa-lab/.asa/installation.json").exists(), "ASA_NOT_INSTALLED")
    old = subprocess.run(["systemctl", "is-active", OLD_TIMER], capture_output=True, text=True)
    require(old.stdout.strip() != "active", "LEGACY_TIMER_ACTIVE_DISABLE_MANUALLY")
    for _, (_, _, calendar) in JOBS.items():
        run(["systemd-analyze", "calendar", calendar], timeout=12)


def make_directories() -> None:
    for directory in (ROOT, ROOT/"staging", ROOT/"archives",
                      ROOT/"archives"/"hourly", ROOT/"archives"/"full", ROOT/"receipts"):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        require(not directory.is_symlink(), "GFS_DIRECTORY_SYMLINK")
        meta = directory.stat()
        require(meta.st_uid == 0 and stat.S_IMODE(meta.st_mode) == 0o700,
                "GFS_DIRECTORY_WRONG_OWNER_MODE")


def unit_texts() -> dict[str, str]:
    units = {}
    for kind, (timer, service, calendar) in JOBS.items():
        runner = RELEASE_DIR/"school-gfs-backup.py"
        units[service] = (
            "[Unit]\n"
            "Description=ASA Lab VM100 age-encrypted " + kind + " backup to Yandex app folder\n"
            "Requires=docker.service\nAfter=docker.service network-online.target\n"
            "Wants=network-online.target\n"
            "ConditionPathExists=/etc/server-settings/age-recipients.txt\n\n"
            "[Service]\nType=oneshot\nUser=root\nUMask=0077\n"
            "WorkingDirectory=/srv/asa-lab\n"
            "Environment=PYTHONDONTWRITEBYTECODE=1\n"
            "ExecStart=/usr/bin/python3 -B " + str(runner) + " --kind " + kind + " --apply\n"
            "TimeoutStartSec=1800\n"
            "StandardOutput=journal\nStandardError=journal\n"
        )
        units[timer] = (
            "[Unit]\nDescription=Schedule ASA Lab VM100 " + kind + " encrypted backup\n\n"
            "[Timer]\nOnCalendar=" + calendar + "\n"
            "Persistent=false\nAccuracySec=1min\n"
            "Unit=" + service + "\n\n[Install]\nWantedBy=timers.target\n"
        )
    return units


def preflight() -> None:
    source_preflight()
    make_directories()
    for kind in JOBS:
        run(["/usr/bin/python3", "-B", str(RELEASE_DIR/"school-gfs-backup.py"),
             "--kind", kind, "--preflight"], timeout=70)
    for name, contents in unit_texts().items():
        path = UNIT_DIR/name
        require(not path.is_symlink(), "EXISTING_UNIT_IS_SYMLINK")
        require(not path.exists() or path.read_text(encoding="utf-8") == contents,
                "EXISTING_UNIT_DRIFT_REFUSE_OVERWRITE")
    print("GFS_INSTALL_PREFLIGHT_PASS source_sha=" + RELEASE_DIR.name, flush=True)


def apply() -> None:
    preflight()
    units = unit_texts()
    newly_created = []
    enabled = []
    try:
        for name, content in units.items():
            path = UNIT_DIR/name
            if path.exists():
                continue
            fd,tmp = tempfile.mkstemp(prefix=".asa-gfs-",dir=UNIT_DIR)
            try:
                with os.fdopen(fd,"w",encoding="utf-8") as out:
                    os.fchmod(out.fileno(),0o644)
                    out.write(content);out.flush();os.fsync(out.fileno())
                require(not path.exists() and not path.is_symlink(), "UNIT_CREATED_CONCURRENTLY")
                os.replace(tmp,path)
                newly_created.append(path)
            finally:
                if os.path.exists(tmp):os.unlink(tmp)
        run(["systemctl","daemon-reload"])
        for kind,(timer,_,_) in JOBS.items():
            run(["systemctl","enable","--now",timer],timeout=25)
            enabled.append(timer)
        for _,(timer,_,_) in JOBS.items():
            require(run(["systemctl","is-active",timer])=="active", "TIMER_NOT_ACTIVE")
            require(run(["systemctl","is-enabled",timer])=="enabled", "TIMER_NOT_ENABLED")
        print("GFS_TIMERS_ENABLED",flush=True)
        for kind,(timer,_,calendar) in JOBS.items():
            print("TIMER",kind,calendar,"name",timer,flush=True)
        print("GFS_FULL_AT=00:20_EUROPE_MOSCOW NO_CATCHUP",flush=True)
        print("GFS_HOURLY_DB_AT=XX:45_EUROPE_MOSCOW",flush=True)
        print("GFS_BOOTSTRAP_BACKUP_RAN_NOW=NO",flush=True)
    except Exception:
        for name in enabled:
            subprocess.run(["systemctl","disable","--now",name],
                           capture_output=True,check=False,timeout=30)
        for file in newly_created:
            file.unlink(missing_ok=True)
        subprocess.run(["systemctl","daemon-reload"],capture_output=True,check=False,timeout=20)
        raise


def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply",action="store_true")
    parser.add_argument("--preflight",action="store_true")
    args=parser.parse_args()
    try:
        if args.apply:
            apply()
        elif args.preflight:
            preflight()
        else:
            for kind,(_,_,calendar) in JOBS.items():
                print("PLAN",kind,calendar,"source",RELEASE_DIR)
            print("PLAN no changes without --apply")
        return 0
    except (InstallError,OSError,ValueError,subprocess.SubprocessError) as error:
        safe=str(error) if isinstance(error,InstallError) else type(error).__name__
        print("GFS_INSTALL_BLOCKED="+safe,file=sys.stderr)
        return 2


if __name__=="__main__":
    raise SystemExit(main())
