#!/usr/bin/env python3
"""Safe, reversible activation of a newer immutable VM100 GFS backup release.

Switches ONLY the two managed GFS systemd service ExecStart references.
Does not touch ASA Lab, database, old backups, timers or any user configuration.
"""
from __future__ import annotations
import argparse
import datetime as dt
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys
import tempfile

HOST = "asa-school-01"
BASE = Path("/opt/asa-school-gfs/releases")
UNIT_DIR = Path("/etc/systemd/system")
RECEIPT = Path("/var/backups/asa-school-gfs/receipts/latest-source-switch.json")
LOCK = Path("/run/lock/asa-school-gfs-switch.lock")
NAMES = ("hourly", "full")


class SwitchError(RuntimeError):
    pass


def require(condition: bool, code: str) -> None:
    if not condition:
        raise SwitchError(code)


def run(args, timeout=35) -> str:
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    require(p.returncode == 0, "COMMAND_FAILED")
    return p.stdout.strip()


def load_units(sha: str) -> dict:
    require(re.fullmatch(r"[a-f0-9]{40}", sha) is not None, "INVALID_RELEASE_SHA")
    source = BASE/sha
    require(source.is_dir() and not source.is_symlink(), "RELEASE_DIR_MISSING")
    for name in ("school-gfs-backup.py", "install-school-gfs-backup.py",
                 "school-nightly-database-backup.py", "school_yandex_app_folder.py",
                 "cloud_storage.py"):
        p = source/name
        require(p.is_file() and not p.is_symlink() and p.stat().st_uid == 0,
                "RELEASE_SOURCE_UNTRUSTED")
    spec = importlib.util.spec_from_file_location("gfs_installer_" + sha[:10],
                                                   source/"install-school-gfs-backup.py")
    require(spec is not None and spec.loader is not None, "INSTALLER_IMPORT_UNAVAILABLE")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.unit_texts()


def preflight(old_sha: str, new_sha: str) -> dict[str, tuple[Path,str,str]]:
    require(os.geteuid() == 0 and socket.gethostname() == HOST,
            "SCHOOL_VM100_ROOT_REQUIRED")
    require(old_sha != new_sha, "SAME_RELEASE")
    old_units, new_units = load_units(old_sha), load_units(new_sha)
    change = {}
    for kind in NAMES:
        service = f"asa-school-gfs-{kind}.service"
        timer = f"asa-school-gfs-{kind}.timer"
        path = UNIT_DIR/service
        require(path.is_file() and not path.is_symlink(), "GFS_SERVICE_UNIT_MISSING")
        previous = path.read_text(encoding="utf-8")
        expected_old, expected_new = old_units[service], new_units[service]
        require(previous == expected_old, "SERVICE_DRIFT_REFUSE_OVERWRITE")
        require(expected_new == expected_old.replace("/releases/"+old_sha+"/",
                                                      "/releases/"+new_sha+"/"),
                "UNEXPECTED_SERVICE_CHANGE")
        require((UNIT_DIR/timer).read_text(encoding="utf-8") == old_units[timer],
                "TIMER_DRIFT_REFUSE_OVERWRITE")
        require(run(["systemctl","is-enabled",timer]) == "enabled", "GFS_TIMER_DISABLED")
        require(run(["systemctl","is-active",timer]) == "active", "GFS_TIMER_INACTIVE")
        active=subprocess.run(["systemctl","is-active",service],capture_output=True,text=True)
        require(active.stdout.strip() == "inactive", "GFS_BACKUP_JOB_RUNNING")
        change[kind] = (path, previous, expected_new)
    # No writes to secrets, runtime, application database or backups.
    for kind in NAMES:
        new_runner = BASE/new_sha/"school-gfs-backup.py"
        run(["python3","-B",str(new_runner),"--kind",kind,"--preflight"],timeout=90)
    return change


def atomic_unit(path: Path, content: str) -> None:
    fd,temp=tempfile.mkstemp(prefix=".gfs-switch-",dir=UNIT_DIR)
    try:
        with os.fdopen(fd,"w",encoding="utf-8") as output:
            os.fchmod(output.fileno(),0o644)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        require(path.is_file() and not path.is_symlink(), "SERVICE_UNIT_DISAPPEARED")
        os.replace(temp,path)
    finally:
        if os.path.exists(temp):os.unlink(temp)


def apply(old_sha: str, new_sha: str) -> None:
    fd=os.open(LOCK,os.O_CREAT|os.O_RDWR|os.O_CLOEXEC|os.O_NOFOLLOW,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        expected=preflight(old_sha,new_sha)
        changed=[]
        try:
            for kind in NAMES:
                path,old,new=expected[kind]
                atomic_unit(path,new)
                changed.append(kind)
            run(["systemctl","daemon-reload"])
            for kind in NAMES:
                service=f"asa-school-gfs-{kind}.service"
                props=run(["systemctl","show",service,"-p","ExecStart"])
                require(new_sha in props and old_sha not in props,
                        "SERVICE_RELEASE_READBACK_FAILED")
                timer=f"asa-school-gfs-{kind}.timer"
                require(run(["systemctl","is-active",timer])=="active",
                        "TIMER_WAS_STOPPED")
                require(run(["systemctl","is-enabled",timer])=="enabled",
                        "TIMER_WAS_DISABLED")
        except Exception:
            for kind in reversed(changed):
                path,old,_=expected[kind]
                atomic_unit(path,old)
            subprocess.run(["systemctl","daemon-reload"],capture_output=True,check=False,timeout=35)
            raise
        receipt={"format":"asa-school-gfs-switch-v1",
                 "changedAt":dt.datetime.now(dt.timezone.utc).isoformat(),
                 "previousSha":old_sha,"newSha":new_sha,
                 "services":[f"asa-school-gfs-{kind}.service" for kind in NAMES],
                 "runtimeUnchanged":True}
        require(RECEIPT.parent.is_dir() and not RECEIPT.parent.is_symlink(),
                "RECEIPT_DIRECTORY_MISSING")
        fd,tmp=tempfile.mkstemp(prefix=".switch-receipt-",dir=RECEIPT.parent)
        try:
            with os.fdopen(fd,"w",encoding="utf-8") as output:
                os.fchmod(output.fileno(),0o600)
                json.dump(receipt,output,sort_keys=True);output.write("\n")
                output.flush();os.fsync(output.fileno())
            os.replace(tmp,RECEIPT)
        finally:
            if os.path.exists(tmp):os.unlink(tmp)
        print("GFS_RELEASE_SWITCH_VERIFIED",old_sha,"->",new_sha,flush=True)
    finally:
        os.close(fd)


def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-sha",required=True)
    parser.add_argument("--new-sha",required=True)
    parser.add_argument("--apply",action="store_true")
    args=parser.parse_args()
    try:
        if args.apply:
            apply(args.old_sha,args.new_sha)
        else:
            preflight(args.old_sha,args.new_sha)
            print("GFS_SWITCH_PREFLIGHT_PASS",args.old_sha,"->",args.new_sha)
        return 0
    except (SwitchError,OSError,ValueError,KeyError,subprocess.SubprocessError,
            BlockingIOError) as error:
        safe=str(error) if isinstance(error,SwitchError) else type(error).__name__
        print("GFS_SWITCH_BLOCKED="+safe,file=sys.stderr)
        return 2


if __name__=="__main__":
    raise SystemExit(main())
