#!/usr/bin/env python3
"""One-shot encrypted school settings archive upload/download acceptance.

Requires an existing age-encrypted settings snapshot from backup-settings.sh,
an owner-held age identity (not on the VM), and the existing isolated
cloud_storage.py rclone container. This does not backup PostgreSQL or MinIO,
does not schedule future backups, and never removes or overwrites archives.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import socket
import stat
import sys

HOST="asa-school-01"
PREFIX="server-settings-"
PROJECT="server-settings"
SOURCE=Path("/var/backups/server-settings")
SPOOL=Path("/srv/asa-cloud-storage/data")
CLOUD_PY=Path(__file__).with_name("cloud_storage.py")


class BackupError(Exception):
    pass


def valid_name(name: str):
    if not re.fullmatch(r"server-settings-[0-9]{8}T[0-9]{6}Z[.]tar[.]age",name):
        raise BackupError("ARCHIVE_NAME_NOT_PINNED")
    return name


def hash_bytes(path: Path):
    hasher=hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def verify_source(name: str):
    if os.geteuid()!=0 or socket.gethostname()!=HOST:
        raise BackupError("VM100_ROOT_REQUIRED")
    name=valid_name(name)
    file=SOURCE/name
    check=SOURCE/(name+".sha256")
    for p in (file,check):
        meta=p.lstat()
        if not stat.S_ISREG(meta.st_mode) or meta.st_uid!=0 or stat.S_IMODE(meta.st_mode)!=0o600:
            raise BackupError("ARCHIVE_SOURCE_NOT_TRUSTED")
    if not (32<=file.stat().st_size<=536870912):
        raise BackupError("ARCHIVE_SIZE_OUT_OF_BOUNDS")
    with file.open("rb") as stream:
        if not stream.read(40).startswith(b"age-encryption.org/v1"):
            raise BackupError("NOT_AGE_ENCRYPTED")
    parts=check.read_text(encoding="ascii").strip().split()
    digest=hash_bytes(file)
    if len(parts)!=2 or parts[0]!=digest or parts[1]!=str(file):
        raise BackupError("SOURCE_SHA256_MISMATCH")
    return file,digest


def ensure_spool():
    root=Path("/srv/asa-cloud-storage")
    if (root.is_symlink() or not root.is_dir() or root.stat().st_uid!=0
            or stat.S_IMODE(root.stat().st_mode)!=0o700):
        raise BackupError("CLOUD_ROOT_UNTRUSTED")
    for path in (SPOOL,SPOOL/PROJECT,SPOOL/PROJECT/"outgoing",SPOOL/PROJECT/"incoming"):
        if path.is_symlink():
            raise BackupError("CLOUD_SPOOL_SYMLINK")
        if not path.exists():
            path.mkdir(mode=0o700)
            os.chown(path,1000,1000)
        meta=path.stat()
        if not stat.S_ISDIR(meta.st_mode) or meta.st_uid!=1000 or stat.S_IMODE(meta.st_mode)!=0o700:
            raise BackupError("CLOUD_SPOOL_OWNER_MODE_WRONG")
    return SPOOL/PROJECT/"outgoing"


def cloud_module():
    spec=importlib.util.spec_from_file_location("asa_school_cloud",CLOUD_PY)
    if spec is None or spec.loader is None:
        raise BackupError("CLOUD_MODULE_NOT_AVAILABLE")
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def copy_ciphertext_once(source: Path, target: Path, digest: str):
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not target.is_file() or target.stat().st_uid!=1000:
            raise BackupError("EXISTING_SPOOL_UNTRUSTED")
        if hash_bytes(target)!=digest:
            raise BackupError("EXISTING_SPOOL_CHANGED")
        return
    fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    try:
        with os.fdopen(fd,"wb") as output, source.open("rb") as incoming:
            os.fchown(output.fileno(),1000,1000)
            os.fchmod(output.fileno(),0o600)
            shutil.copyfileobj(incoming,output,1024*1024)
            output.flush()
            os.fsync(output.fileno())
        if hash_bytes(target)!=digest:
            raise BackupError("COPIED_SPOOL_SHA256_MISMATCH")
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def transfer(name: str):
    src,digest=verify_source(name)
    cloud=cloud_module()
    state=cloud.status()
    if state.get("connected") is not True:
        raise BackupError("YANDEX_NOT_CONNECTED")
    outgoing=ensure_spool()
    copy_ciphertext_once(src,outgoing/name,digest)
    upload=cloud.transfer("upload",PROJECT,name,expected_sha256=digest)
    if upload.get("sha256")!=digest:
        raise BackupError("UPLOAD_HASH_DID_NOT_MATCH")
    download=cloud.transfer("download",PROJECT,name,expected_sha256=digest)
    if download.get("sha256")!=digest:
        raise BackupError("DOWNLOAD_HASH_DID_NOT_MATCH")
    path=Path(download["local_path"])
    if hash_bytes(path)!=digest:
        raise BackupError("DOWNLOADED_CIPHERTEXT_CORRUPTED")
    print("SCHOOL_CONFIG_ENCRYPTED_UPLOAD_DOWNLOAD=PASS")
    print("ARCHIVE_PROJECT="+PROJECT)
    print("ARCHIVE_NAME="+name)
    print("ARCHIVE_SIZE="+str(src.stat().st_size))
    print("CIPHERTEXT_SHA256="+digest)
    print("DECRYPTION_REQUIRES_OWNER_OFFLINE_AGE_IDENTITY=YES")
    print("ASA_POSTGRES_MINIO_BACKUP_NOT_INCLUDED=YES")
    print("BACKUP_SCHEDULE_NOT_CHANGED=YES")


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--archive",required=True)
    p.add_argument("--apply",action="store_true")
    args=p.parse_args()
    try:
        name=valid_name(args.archive)
        if not args.apply:
            print("PLAN: upload/download encrypted "+name+" to Yandex:Server-Backups/server-settings, checksum verified. No data deletion, no schedules.")
            return 0
        transfer(name)
    except (BackupError,ValueError,OSError) as exc:
        print("SCHOOL_SETTINGS_CLOUD_BLOCKED="+(str(exc) if isinstance(exc,(BackupError,ValueError)) else type(exc).__name__),file=sys.stderr)
        return 2
    return 0

if __name__=="__main__":
    raise SystemExit(main())
