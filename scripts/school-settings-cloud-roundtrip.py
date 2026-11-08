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
import http.client
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import socket
import ssl
import stat
import sys
import time
import urllib.parse

HOST="asa-school-01"
PREFIX="server-settings-"
PROJECT="server-settings"
SOURCE=Path("/var/backups/server-settings")
SPOOL=Path("/srv/asa-cloud-storage/data")
CLOUD_PY=Path(__file__).with_name("cloud_storage.py")
CONFIG_ROOT="app:/School-VM100/Configuration-Backups"


def configuration_provider(transport, token):
    """Use the existing transport, with a closed configuration-only namespace."""
    import school_settings_snapshot as snapshot

    class ConfigurationFolder(transport.AppFolder):
        def folder_preflight(self):
            status, info=self.api("/resources",params={"path":"app:/School-VM100","fields":"type,path"})
            snapshot.need(status==200 and info.get("type")=="dir","SCHOOL_APP_FOLDER_MISSING")
            status, info=self.api("/resources",params={"path":CONFIG_ROOT,"fields":"type,path"})
            if status==404:
                created,_=self.api("/resources",params={"path":CONFIG_ROOT},method="PUT")
                snapshot.need(created in (201,409),"CONFIG_FOLDER_CREATE_FAILED")
                status, info=self.api("/resources",params={"path":CONFIG_ROOT,"fields":"type,path"})
            snapshot.need(status==200 and info.get("type")=="dir","CONFIG_FOLDER_NOT_DIRECTORY")

        def metadata(self,name):
            status, info=self.api("/resources",params={"path":CONFIG_ROOT+"/"+snapshot.archive_name(name)})
            if status==404:
                return None
            snapshot.need(status==200 and info.get("type")=="file" and info.get("name")==name,
                          "CONFIG_REMOTE_METADATA_REJECTED")
            return info

        def signed_url(self,resource,name):
            snapshot.need(resource in ("upload","download"),"CONFIG_SIGNED_URL_ACTION")
            params={"path":CONFIG_ROOT+"/"+snapshot.archive_name(name)}
            if resource=="upload":
                params["overwrite"]="false"
            status, info=self.api("/resources/"+resource,params=params)
            snapshot.need(status==200 and info.get("method")==("PUT" if resource=="upload" else "GET"),
                          "CONFIG_SIGNED_URL_FAILED")
            return transport.validate_signed_link(str(info.get("href","")))

    return ConfigurationFolder(token)


def download_configuration(provider,name,target,size,checksum):
    """Save the full ciphertext; allow at most three validated HTTPS redirects."""
    import school_settings_snapshot as snapshot
    transport=snapshot.pinned_transport()
    temporary=target.with_suffix(target.suffix+".partial")
    snapshot.need(not target.exists() and not target.is_symlink(),"CONFIG_DOWNLOAD_ALREADY_EXISTS")
    host,port,path=provider.signed_url("download",name)
    connection=None
    created=False
    try:
        fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        created=True
        with os.fdopen(fd,"wb") as stream:
            seen={(host,port,path)}
            for redirects in range(4):
                connection=http.client.HTTPSConnection(host,port,timeout=600,context=ssl.create_default_context())
                # No OAuth/cookie/request headers are forwarded to a signed URL.
                connection.request("GET",path)
                response=connection.getresponse()
                if response.status not in (301,302,303,307,308):
                    break
                snapshot.need(redirects<3,"CONFIG_DOWNLOAD_REDIRECT_LIMIT")
                location=response.getheader("Location")
                snapshot.need(isinstance(location,str) and 0<len(location)<=16384,"CONFIG_DOWNLOAD_REDIRECT_LOCATION")
                candidate=urllib.parse.urljoin("https://"+host+path,location)
                following=transport.validate_signed_link(candidate)
                snapshot.need(following not in seen,"CONFIG_DOWNLOAD_REDIRECT_CYCLE")
                seen.add(following)
                connection.close()
                connection=None
                host,port,path=following
            snapshot.need(response.status==200,"CONFIG_DOWNLOAD_HTTP_STATUS_"+str(response.status))
            h=hashlib.sha256()
            count=0
            while True:
                block=response.read(1024*1024)
                if not block:
                    break
                count+=len(block)
                snapshot.need(count<=size,"CONFIG_DOWNLOAD_SIZE_EXCEEDED")
                stream.write(block)
                h.update(block)
            snapshot.need(count==size and h.hexdigest()==checksum,"CONFIG_DOWNLOAD_INTEGRITY_FAILED")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary,target)
        temporary.unlink()
        created=False
    finally:
        if connection is not None:
            connection.close()
        if created:
            temporary.unlink(missing_ok=True)


def school_configuration_transfer(name):
    import school_settings_snapshot as snapshot
    snapshot.check_host()
    name=snapshot.archive_name(name)
    snapshot.private_directory(snapshot.TARGET)
    source=snapshot.TARGET/name
    size,checksum=snapshot.verify_cipher(source)
    raw,_=snapshot.stable_read(snapshot.TARGET/(name+".sha256"),4096)
    snapshot.need(raw.decode("ascii").split()==[checksum,str(source)],"CONFIG_CIPHERTEXT_SIDECAR")
    snapshot.private_directory(snapshot.RECOVERY,create=True)
    incoming=snapshot.RECOVERY/"incoming"
    snapshot.private_directory(incoming,create=True)
    snapshot.need(shutil.disk_usage(incoming).free>=size+512*1024**2,"CONFIG_DOWNLOAD_DISK_HEADROOM")
    target=incoming/name
    snapshot.need(not target.exists() and not target.is_symlink(),"CONFIG_DOWNLOAD_MUST_BE_NEW")
    transport=snapshot.pinned_transport()
    provider=configuration_provider(transport,transport.fresh_token())
    provider.folder_preflight()
    remote=provider.metadata(name)
    if remote is None:
        provider.upload_file(source,name,size)
    else:
        snapshot.need(remote.get("size")==size,"CONFIG_IMMUTABLE_REMOTE_COLLISION")
    for attempt in range(30):
        remote=provider.metadata(name)
        if remote is not None:
            break
        time.sleep(2)
    snapshot.need(remote is not None and remote.get("size")==size,"CONFIG_REMOTE_SIZE_UNVERIFIED")
    download_configuration(provider,name,target,size,checksum)
    snapshot.need(snapshot.verify_cipher(target)==(size,checksum),"CONFIG_RETAINED_DOWNLOAD_CHANGED")
    receipt={"status":"CONFIG_UPLOAD_FULL_DOWNLOAD_PASS_DECRYPT_PENDING","archive":name,
        "remoteDirectory":CONFIG_ROOT,"bytes":size,"ciphertextSha256":checksum,
        "fullDownloadVerified":True,"downloadedCiphertext":str(target),"restoreValidated":False,
        "offlineIdentityRequired":True,"schedulesChanged":False,"databaseBackupsChanged":False}
    snapshot.write_json(snapshot.TARGET/(name+".cloud.json"),receipt)
    print(json.dumps(receipt,sort_keys=True,separators=(",",":")))


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
    p.add_argument("--school-vm100",action="store_true")
    args=p.parse_args()
    try:
        if args.school_vm100:
            import school_settings_snapshot as snapshot
            name=snapshot.archive_name(args.archive)
            if not args.apply:
                print(json.dumps({"status":"PLAN_ONLY","archive":name,"remoteDirectory":CONFIG_ROOT,
                    "fullDownloadRetained":True,"offlineDecryptionSeparate":True,"changes":False}))
                return 0
            os.umask(0o077)
            school_configuration_transfer(name)
            return 0
        name=valid_name(args.archive)
        if not args.apply:
            print("PLAN: upload/download encrypted "+name+" to Yandex:Server-Backups/server-settings, checksum verified. No data deletion, no schedules.")
            return 0
        transfer(name)
    except Exception as exc:
        code=str(exc) if isinstance(exc,BackupError) or type(exc).__name__ in ("SnapshotError","BackupTransportError") else type(exc).__name__
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,96}",code):
            code=type(exc).__name__
        print("SCHOOL_SETTINGS_CLOUD_BLOCKED="+code,file=sys.stderr)
        return 2
    return 0

if __name__=="__main__":
    raise SystemExit(main())
