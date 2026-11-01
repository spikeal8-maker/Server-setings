#!/usr/bin/env python3
"""ASA School VM100 PostgreSQL + MinIO encrypted backup (scheduled or manual).

Scheduled backups are restricted to 03:00-05:00 Europe/Moscow. Explicit
operator-initiated manual backups are allowed at any local time through
a separate command. Uses the installed ASA Lab exporter, briefly stopping
writers for a
consistent DB/object snapshot, then restarts them. Immediately encrypts all
private export files with an owner-held age PUBLIC recipient. Uploads only
ciphertext into the dedicated Yandex OAuth application folder and downloads
it for SHA256 verification. Never deletes an encrypted backup.
"""
from __future__ import annotations
import argparse
from datetime import datetime,time,timezone
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import uuid
from zoneinfo import ZoneInfo

import school_yandex_app_folder as yandex

HOST="asa-school-01"
APP_ROOT=Path("/srv/asa-lab")
MANAGER=APP_ROOT/"tools/asa_manager.py"
MAINTENANCE=APP_ROOT/".asa/maintenance.json"
ROOT=Path("/var/backups/asa-school-nightly")
STAGING=ROOT/"staging"
ARCHIVES=ROOT/"archives"
LOCK=Path("/run/lock/asa-school-nightly-db.lock")
RECIPIENT=Path("/etc/server-settings/age-recipients.txt")
MOSCOW=ZoneInfo("Europe/Moscow")


class NightlyError(Exception):
    pass


def check_host():
    if os.geteuid()!=0 or socket.gethostname()!=HOST:
        raise NightlyError("VM100_ROOT_REQUIRED")
    if APP_ROOT.is_symlink() or APP_ROOT.stat().st_uid!=0:
        raise NightlyError("ASA_INSTALLATION_ROOT_UNEXPECTED")
    for path in (MANAGER,RECIPIENT):
        if path.is_symlink() or not path.is_file():
            raise NightlyError("BACKUP_INPUT_NOT_REGULAR")
    if MANAGER.stat().st_uid!=0:
        raise NightlyError("ASA_MANAGER_OWNER_CHANGED")
    if RECIPIENT.stat().st_uid!=0 or stat.S_IMODE(RECIPIENT.stat().st_mode)!=0o600:
        raise NightlyError("AGE_RECIPIENT_OWNER_MODE")
    pub=RECIPIENT.read_text(encoding="ascii").strip()
    if len(pub.splitlines())!=1 or re.fullmatch(r"age1[a-z0-9]{55,80}",pub) is None:
        raise NightlyError("AGE_PUBLIC_RECIPIENT_INVALID")
    if MAINTENANCE.exists() or MAINTENANCE.is_symlink():
        raise NightlyError("APP_MAINTENANCE_IN_PROGRESS")


def is_night(value=None):
    current=value or datetime.now(MOSCOW)
    if current.tzinfo is None:raise NightlyError("CLOCK_HAS_NO_TIMEZONE")
    t=current.astimezone(MOSCOW).time().replace(tzinfo=None)
    return time(3,0)<=t<time(5,0)


def checked_directory(path):
    if path.is_symlink() or not path.is_dir():
        raise NightlyError("BACKUP_DIRECTORY_MISSING_OR_SYMLINK")
    info=path.stat()
    if info.st_uid!=0 or stat.S_IMODE(info.st_mode)!=0o700:
        raise NightlyError("BACKUP_DIRECTORY_OWNER_MODE")


def preflight():
    check_host()
    for path in (ROOT,STAGING,ARCHIVES):checked_directory(path)
    if shutil.disk_usage(ROOT).free<15*1024**3:
        raise NightlyError("DISK_LESS_THAN_15_GIB")
    provider=yandex.AppFolder(yandex.fresh_token())
    provider.folder_preflight()
    return provider


def export_asa(stage):
    command=["/usr/bin/python3",str(MANAGER),"--root",str(APP_ROOT),
             "--profile","production","backup","--output",str(stage)]
    complete=subprocess.run(command,cwd=APP_ROOT,capture_output=True,check=False)
    if complete.returncode!=0:
        raise NightlyError("ASA_MANAGER_BACKUP_FAILED")
    produced=list(stage.iterdir())
    if len(produced)!=1 or produced[0].is_symlink() or not produced[0].is_dir():
        raise NightlyError("ASA_EXPORT_STRUCTURE_CHANGED")
    saved=produced[0]
    manifest=saved/"manifest.json"
    if manifest.is_symlink() or not manifest.is_file():
        raise NightlyError("ASA_EXPORT_MANIFEST_MISSING")
    info=json.loads(manifest.read_text(encoding="utf-8"))
    if info.get("format")!="asa-backup-v1" or info.get("complete") is not True:
        raise NightlyError("ASA_EXPORT_NOT_COMPLETE")
    if info.get("project")!="asa-lab-school" or info.get("profile")!="production":
        raise NightlyError("ASA_EXPORT_WRONG_PROJECT")
    for rel in ("database.dump","globals.sql","private.env","objects","configuration"):
        if not (saved/rel).exists():
            raise NightlyError("ASA_EXPORT_MISSING_"+rel.replace(".","_").upper())
    if (saved/"database.dump").stat().st_size<1024:
        raise NightlyError("POSTGRES_DUMP_EMPTY")
    return saved


def encrypt_age(stage,saved,final):
    pending=ARCHIVES/("."+final.name+".partial")
    if final.exists() or final.is_symlink() or pending.exists() or pending.is_symlink():
        raise NightlyError("IMMUTABLE_ARCHIVE_COLLISION")
    tar=subprocess.Popen(
        ["/usr/bin/tar","-C",str(stage),"-cf","-","--",saved.name],
        stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    enc=None
    try:
        enc=subprocess.Popen(
            ["/usr/bin/age","-R",str(RECIPIENT),"-o",str(pending)],
            stdin=tar.stdout,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        tar.stdout.close()
        enc.communicate()
        tar_rc=tar.wait()
        if enc.returncode!=0 or tar_rc!=0:
            raise NightlyError("AGE_OR_TAR_EXPORT_FAILED")
        if pending.is_symlink() or not pending.is_file():
            raise NightlyError("ENCRYPTED_FILE_NOT_REGULAR")
        os.chmod(pending,0o600)
        with pending.open("rb") as stream:
            if not stream.read(30).startswith(b"age-encryption.org/v1"):
                raise NightlyError("ENCRYPTION_HEADER_INVALID")
        if pending.stat().st_size<128:
            raise NightlyError("ENCRYPTED_ARCHIVE_EMPTY")
        os.replace(pending,final)
        os.chmod(final,0o600)
    finally:
        if enc and enc.poll() is None:
            enc.kill();enc.wait()
        if tar.poll() is None:
            tar.kill();tar.wait()
        if pending.exists():pending.unlink()


def write_verified_receipt(result, *, trigger="scheduled"):
    if trigger not in ("scheduled", "manual"):
        raise NightlyError("BACKUP_TRIGGER_INVALID")
    """Record only an actual cloud upload/download SHA256 success."""
    receipt = ROOT / "latest-success.json"
    if receipt.is_symlink():
        raise NightlyError("LAST_SUCCESS_RECEIPT_IS_SYMLINK")
    if receipt.exists():
        meta = receipt.stat()
        if meta.st_uid != 0 or stat.S_IMODE(meta.st_mode) != 0o600:
            raise NightlyError("LAST_SUCCESS_RECEIPT_UNTRUSTED")
    value = {
        "status": "verified_cloud_sha256",
        "trigger": trigger,
        "project": "asa-lab-school",
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "archive": result["name"],
        "size": result["size"],
        "sha256": result["sha256"],
        "remote": "app:/School-VM100/Database-Backups",
        "restore_rehearsal": False,
    }
    fd, tmp = tempfile.mkstemp(prefix=".last-success-", dir=ROOT)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            os.fchmod(output.fileno(), 0o600)
            json.dump(value, output, ensure_ascii=False, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(tmp, receipt)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def execute(*, manual=False):
    if not manual and not is_night():
        raise NightlyError("ONLY_SCHEDULED_0300_TO_0500_MOSCOW")
    provider=preflight()
    fd=os.open(LOCK,os.O_WRONLY|os.O_CREAT|os.O_CLOEXEC|os.O_NOFOLLOW,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if not manual and not is_night():
            raise NightlyError("NIGHT_WINDOW_EXPIRED")
        trigger="manual" if manual else "scheduled"
        print("BACKUP_TRIGGER="+trigger,flush=True)
        print("ASA_EXPORT_BRIEFLY_STOPS_WEB_API_SCRATCH_MINIO_WRITERS=YES",flush=True)
        stage=Path(tempfile.mkdtemp(prefix=".export-",dir=STAGING))
        try:
            exported=export_asa(stage)
            name="asa-school-01-"+datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")+"-"+uuid.uuid4().hex[:8]+".tar.age"
            encrypted=ARCHIVES/name
            encrypt_age(stage,exported,encrypted)
        finally:
            # Preserve evidence if ASA's own recovery marker remains after
            # failed writer restart; never delete files during maintenance.
            if not MAINTENANCE.exists():
                shutil.rmtree(stage)
        print("NIGHTLY_DATABASE_LOCAL_ENCRYPTED="+encrypted.name,flush=True)
        try:
            result=provider.upload_and_verify(encrypted)
        except yandex.BackupTransportError as error:
            raise NightlyError("LOCAL_AGE_BACKUP_SAVED_BUT_CLOUD_"+str(error)) from None
        write_verified_receipt(result, trigger=trigger)
        print("NIGHTLY_DATABASE_YANDEX_CIPHERTEXT_DOWNLOAD_SHA256=PASS",flush=True)
        print("NIGHTLY_DATABASE_REMOTE=app:/School-VM100/Database-Backups",flush=True)
        print("NIGHTLY_DATABASE_BYTES="+str(result["size"]),flush=True)
        print("NIGHTLY_DATABASE_CONTAINS=PostgreSQL+MinIO+release+private-config",flush=True)
        print("RESTORE_REHEARSAL=NOT_PERFORMED",flush=True)
    finally:
        os.close(fd)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    group=parser.add_mutually_exclusive_group()
    group.add_argument("--preflight",action="store_true")
    group.add_argument("--apply",action="store_true")
    parser.add_argument("--manual",action="store_true",
                        help="explicit operator-triggered backup at any hour; requires --apply")
    args=parser.parse_args()
    try:
        if args.manual and not args.apply:
            raise NightlyError("MANUAL_REQUIRES_EXPLICIT_APPLY")
        if args.preflight:
            preflight()
            print("SCHOOL_NIGHTLY_YANDEX_APP_FOLDER_WRITE_PREFLIGHT=PASS")
            print("DATABASE_WRITERS_STOPPED=NO")
        elif args.apply:
            execute(manual=args.manual)
        else:
            check_host()
            print("PLAN: scheduled at 03:20 Europe/Moscow; manual any time via sudo asa-school-backup-now.")
            print("Both modes: consistent PostgreSQL+MinIO, age encrypt, immutable Yandex upload+SHA256 download.")
            print("Backup services may pause briefly only after a verified Yandex write preflight.")
        return 0
    except (NightlyError,yandex.BackupTransportError,OSError,ValueError,
            subprocess.SubprocessError,BlockingIOError) as err:
        code=str(err) if isinstance(err,(NightlyError,yandex.BackupTransportError)) else type(err).__name__
        print("SCHOOL_NIGHTLY_BACKUP_BLOCKED="+code,file=sys.stderr)
        return 2


if __name__=="__main__":
    raise SystemExit(main())
