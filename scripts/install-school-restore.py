#!/usr/bin/env python3
"""Guarded installer for the native ASA School Yandex restore CLI.

Only installs /usr/local/sbin/asa-school-restore; never restores data,
changes Postgres/MinIO, or modifies a 1Panel configuration/database.
"""
import argparse
import os
from pathlib import Path
import shutil
import socket
import stat
import subprocess
import sys

SOURCE=Path("/srv/Server-setings/scripts/asa-school-restore.sh")
HANDLER=Path("/srv/Server-setings/scripts/school-yandex-restore.py")
DEST=Path("/usr/local/sbin/asa-school-restore")


def main():
    args=argparse.ArgumentParser(description=__doc__)
    args.add_argument("--apply",action="store_true")
    arg=args.parse_args()
    try:
        if os.geteuid()!=0 or socket.gethostname()!="asa-school-01":
            raise ValueError("SCHOOL_VM100_ROOT_REQUIRED")
        for p in (SOURCE,HANDLER):
            if p.is_symlink() or not p.is_file() or p.stat().st_uid!=0:
                raise ValueError("TRUSTED_SOURCE_MISSING")
        subprocess.run(["/usr/bin/bash","-n",str(SOURCE)],check=True,timeout=10,capture_output=True)
        subprocess.run(["/usr/bin/python3","-m","py_compile",str(HANDLER)],check=True,timeout=10,capture_output=True)
        if not arg.apply:
            print("PLAN: install only /usr/local/sbin/asa-school-restore.")
            print("No database import, no cloud upload, no 1Panel quick commands yet.")
            return 0
        if DEST.exists() or DEST.is_symlink():
            meta=DEST.lstat()
            if (not stat.S_ISREG(meta.st_mode) or meta.st_uid!=0
                or stat.S_IMODE(meta.st_mode)!=0o755
                or DEST.read_bytes()!=SOURCE.read_bytes()):
                raise ValueError("EXISTING_RESTORE_CLI_CHANGED")
            print("SCHOOL_RESTORE_CLI_ALREADY_PRESENT=YES")
        else:
            fd=os.open(DEST,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o755)
            try:
                with os.fdopen(fd,"wb") as d,SOURCE.open("rb") as inp:
                    os.fchmod(d.fileno(),0o755)
                    shutil.copyfileobj(inp,d)
                    d.flush();os.fsync(d.fileno())
            except BaseException:
                DEST.unlink(missing_ok=True)
                raise
            print("SCHOOL_RESTORE_CLI_INSTALLED=YES")
        print("SCHOOL_RESTORE_LIVE_DB_NOT_MODIFIED=YES")
        print("COMMAND=sudo asa-school-restore list")
        return 0
    except (OSError,ValueError,subprocess.SubprocessError) as e:
        print("SCHOOL_RESTORE_INSTALL_BLOCKED="+(str(e) if isinstance(e,ValueError) else type(e).__name__),file=sys.stderr)
        return 2

if __name__=="__main__":
    raise SystemExit(main())
