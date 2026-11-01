#!/usr/bin/env python3
"""ASA School VM100: list/download/verify historical Yandex ASA backups.

Restore safety contract:
- Yandex app:/School-VM100/Database-Backups and legacy disk:/ASA-Backups
  are read-only sources. Never delete or overwrite remote archives.
- Download retains only .tar.age under a root-only recovery directory.
- To decrypt, the owner supplies an offline-mounted age identity by its path;
  never copy it into this script, ChatGPT, GitHub or persistent VM100 config.
- Prepare verifies the complete signed-in-age backup via the existing ASA
  manager. Production PostgreSQL and MinIO are never modified by prepare.
- A separate --restore-test may create a NEW *_test database through the
  official ASA restore-check (requires interactive confirmation); never
  drops, replaces, or selects the live database.
- Full production replacement is deliberately not automatic; it must use
  the separate verified fresh-target import/cutover workflow.
"""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import getpass
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from urllib.parse import urlunsplit
import uuid

import school_yandex_app_folder as yandex

ROOT=Path("/var/backups/asa-school-recovery")
INCOMING=ROOT/"incoming"
UNPACKED=ROOT/"unpacked"
ASA=Path("/srv/asa-lab")
MANAGER=ASA/"tools/asa_manager.py"
HOST="asa-school-01"
SOURCES={
    "school": "app:/School-VM100/Database-Backups",
    "legacy": "disk:/ASA-Backups",
}
NAMES={
    "school":re.compile(r"asa-school-01-\d{8}T\d{6}Z-[a-f0-9]{8}\.tar\.age\Z"),
    "legacy":re.compile(r"asa-lab-\d{8}T\d{6}Z-[a-f0-9]{8}\.tar\.age\Z"),
}
MAX_ARCHIVE_SIZE=25*1024**3
MAX_UNPACKED=50*1024**3
MAX_MEMBERS=300000


class RestoreError(Exception):
    pass


def require_host():
    if os.geteuid()!=0 or socket.gethostname()!=HOST:
        raise RestoreError("ROOT_ON_SCHOOL_VM100_REQUIRED")
    if MANAGER.is_symlink() or not MANAGER.is_file():
        raise RestoreError("ASA_MANAGER_NOT_FOUND")
    if not Path("/srv/Server-setings/scripts/cloud_storage.py").is_file():
        raise RestoreError("CLOUD_MODULE_MISSING")


def ensure_private_dir(path:Path):
    if path.is_symlink():
        raise RestoreError("RECOVERY_PATH_IS_SYMLINK")
    if not path.exists():
        path.mkdir(mode=0o700)
        os.chown(path,0,0)
        os.chmod(path,0o700)
    st=path.lstat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid!=0 or stat.S_IMODE(st.st_mode)!=0o700:
        raise RestoreError("RECOVERY_DIRECTORY_NOT_PRIVATE")


def ensure_recovery():
    for path in (ROOT,INCOMING,UNPACKED):
        ensure_private_dir(path)


def name_ok(source:str,name:str)->str:
    if source not in SOURCES or not isinstance(name,str) or not NAMES[source].fullmatch(name):
        raise RestoreError("SOURCE_OR_ARCHIVE_NAME_REJECTED")
    return name


def list_entries(client:yandex.AppFolder,source:str)->list[dict]:
    if source not in SOURCES:raise RestoreError("SOURCE_UNKNOWN")
    result=[]
    offset=0
    while True:
        status,data=client.api("/resources",params={
            "path":SOURCES[source],"limit":"100","offset":str(offset),
            "fields":"type,_embedded.items.name,_embedded.items.type,_embedded.items.size,_embedded.items.md5,_embedded.total",
        })
        if status!=200 or data.get("type")!="dir":
            raise RestoreError("YANDEX_FOLDER_LIST_FAILED_"+source+"_"+str(status))
        meta=data.get("_embedded") or {}
        items=meta.get("items") or []
        if not isinstance(items,list):raise RestoreError("YANDEX_ITEMS_NOT_ARRAY")
        for item in items:
            name=item.get("name","")
            if item.get("type")=="file" and NAMES[source].fullmatch(str(name)):
                size=item.get("size")
                if type(size) is int and 128<=size<=MAX_ARCHIVE_SIZE:
                    result.append({"source":source,"name":name,"size":size,
                                   "md5":item.get("md5")})
        offset+=len(items)
        total=meta.get("total")
        if not items or (type(total) is int and offset>=total):
            break
        if offset>10000:raise RestoreError("TOO_MANY_REMOTE_FILES")
    return sorted(result,key=lambda x:x["name"],reverse=True)


def inventory(client:yandex.AppFolder)->list[dict]:
    result=[]
    for source in SOURCES:
        result.extend(list_entries(client,source))
    return sorted(result,key=lambda x:x["name"],reverse=True)


def choose(items:list[dict])->dict:
    if not items:
        raise RestoreError("NO_YANDEX_ENCRYPTED_ARCHIVES")
    print("Доступные зашифрованные архивы (новые первыми):")
    for i,item in enumerate(items,1):
        print(f"{i:>3}. [{item['source']}] {item['name']} — {item['size']/(1024**2):.1f} МиБ")
    chosen=input("Выберите номер архива (0 — отмена): ").strip()
    if chosen=="0":raise RestoreError("CANCELLED")
    if not re.fullmatch(r"[0-9]{1,4}",chosen):
        raise RestoreError("ARCHIVE_SELECTION_INVALID")
    index=int(chosen)
    if index<1 or index>len(items):raise RestoreError("ARCHIVE_SELECTION_OUT_OF_RANGE")
    return items[index-1]


def signed_url(client:yandex.AppFolder,item:dict)->str:
    path=SOURCES[item["source"]]+"/"+name_ok(item["source"],item["name"])
    code,data=client.api("/resources/download",params={"path":path})
    if code!=200 or data.get("method")!="GET":
        raise RestoreError("YANDEX_DOWNLOAD_PERMISSION_"+str(code))
    href=data.get("href")
    host,port,target=yandex.validate_signed_link(str(href))
    return urlunsplit(("https",host,target.split("?",1)[0],
                       target.split("?",1)[1] if "?" in target else "",""))


def source_path(item):
    return INCOMING / (item["source"]+"__"+item["name"])


def file_hashes(file:Path)->tuple[int,str,str]:
    size=0;sha=hashlib.sha256();md5=hashlib.md5()
    with file.open("rb") as stream:
        for chunk in iter(lambda:stream.read(2*1024*1024),b""):
            size+=len(chunk);sha.update(chunk);md5.update(chunk)
    return size,sha.hexdigest(),md5.hexdigest()


def download(client:yandex.AppFolder,item:dict)->Path:
    ensure_recovery()
    dest=source_path(item)
    size_expected=item["size"]
    if dest.exists() or dest.is_symlink():
        if dest.is_symlink() or not dest.is_file() or dest.stat().st_uid!=0:
            raise RestoreError("EXISTING_DOWNLOAD_UNTRUSTED")
        size,sha,md5=file_hashes(dest)
        if size!=size_expected or (item.get("md5") and md5!=item["md5"]):
            raise RestoreError("EXISTING_DOWNLOAD_HASH_OR_SIZE_MISMATCH")
        print("Локальный архив уже существует и совпадает с удалёнными метаданными.")
        return dest
    if shutil.disk_usage(ROOT).free<2*size_expected+5*1024**3:
        raise RestoreError("INSUFFICIENT_FREE_SPACE_FOR_RESTORE")
    url=signed_url(client,item)
    tmp=INCOMING/("."+dest.name+"."+uuid.uuid4().hex+".partial")
    descriptor=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    try:
        sha=hashlib.sha256();md5=hashlib.md5();n=0
        with os.fdopen(descriptor,"wb") as sink:
            with urllib.request.urlopen(url,timeout=3600) as response:
                if response.status!=200:raise RestoreError("DOWNLOAD_HTTP_NOT_200")
                # Reject redirects away from Yandex-controlled HTTPS.
                yandex.validate_signed_link(response.geturl())
                while True:
                    chunk=response.read(2*1024*1024)
                    if not chunk:break
                    n+=len(chunk)
                    if n>size_expected:
                        raise RestoreError("DOWNLOAD_LARGER_THAN_REMOTE_METADATA")
                    sink.write(chunk);sha.update(chunk);md5.update(chunk)
            sink.flush();os.fsync(sink.fileno())
        if n!=size_expected:raise RestoreError("DOWNLOAD_SIZE_MISMATCH")
        if item.get("md5") and md5.hexdigest()!=item["md5"]:
            raise RestoreError("DOWNLOAD_PROVIDER_MD5_MISMATCH")
        with tmp.open("rb") as source:
            if not source.read(30).startswith(b"age-encryption.org/v1"):
                raise RestoreError("NOT_AGE_ENCRYPTED")
        os.replace(tmp,dest)
        print("YANDEX_ARCHIVE_DOWNLOADED=YES")
        print("ARCHIVE_NAME="+item["name"])
        print("ARCHIVE_BYTES="+str(n))
        print("CIPHERTEXT_SHA256="+sha.hexdigest())
        print("LIVE_DATABASE_UNMODIFIED=YES")
        return dest
    finally:
        if tmp.exists():tmp.unlink()


def trusted_age_identity(user_path:str)->Path:
    path=Path(user_path).absolute()
    if path.is_symlink() or not path.is_file():
        raise RestoreError("OFFLINE_IDENTITY_NOT_AVAILABLE")
    meta=path.stat()
    if meta.st_uid!=0 or stat.S_IMODE(meta.st_mode)!=0o600:
        raise RestoreError("IDENTITY_MUST_BE_ROOT_0600_ON_TEMP_MOUNT")
    # An identity on the same VM100 disk defeats the independent recovery key.
    # Mount removable media or an explicit temporary recovery mount.
    if not str(path).startswith(("/media/","/run/media/","/mnt/recovery/")):
        raise RestoreError("IDENTITY_MUST_BE_ON_OFFLINE_MOUNT")
    return path


@contextmanager
def offline_identity(identity_file: str | None):
    """Use a mounted root0600 file or a one-shot hidden terminal paste.

    The pasted private key lives only in an anonymous kernel memfd, never
    appears in argv, a named disk file, GitHub, logs, or 1Panel shortcuts.
    """
    if identity_file:
        yield str(trusted_age_identity(identity_file)),()
        return
    supplied=input(
        "Путь к временному USB age identity (Enter — скрыто вставить сам ключ): "
    ).strip()
    if supplied:
        yield str(trusted_age_identity(supplied)),()
        return
    secret=getpass.getpass("Вставьте приватный AGE-SECRET-KEY (скрытый ввод): ").strip()
    if re.fullmatch(r"AGE-SECRET-KEY-1[A-Z0-9]{50,110}",secret) is None:
        raise RestoreError("AGE_PRIVATE_KEY_FORMAT_INVALID")
    if not hasattr(os,"memfd_create"):
        raise RestoreError("AGE_SECURE_MEMORY_FD_UNAVAILABLE")
    descriptor=os.memfd_create("asa-temporary-recovery-age-key",0)
    try:
        os.fchmod(descriptor,0o600)
        os.write(descriptor,secret.encode("ascii")+b"\n")
        os.lseek(descriptor,0,os.SEEK_SET)
        secret=None
        yield "/proc/self/fd/"+str(descriptor),(descriptor,)
    finally:
        os.close(descriptor)


def extract_verified(cipher:Path,identity:str,pass_fds:tuple[int,...]=())->Path:
    ensure_recovery()
    dest=UNPACKED/cipher.name.removesuffix(".tar.age")
    if dest.exists() or dest.is_symlink():
        raise RestoreError("RESTORE_STAGING_ALREADY_EXISTS")
    dest.mkdir(mode=0o700)
    created=True
    age=subprocess.Popen(["/usr/bin/age","--decrypt","-i",str(identity),str(cipher)],
                         stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL,pass_fds=pass_fds)
    members=0;total=0;first=None
    try:
        with tarfile.open(fileobj=age.stdout,mode="r|") as contents:
            for obj in contents:
                members+=1
                if members>MAX_MEMBERS:raise RestoreError("TOO_MANY_ARCHIVE_MEMBERS")
                name=PurePosixPath(obj.name)
                components=name.parts
                if (name.is_absolute() or not components or
                    any(t in ("..",".") for t in components) or
                    any("\x00" in t for t in components) or len(str(name))>1024):
                    raise RestoreError("TAR_PATH_TRAVERSAL")
                if first is None:first=components[0]
                if components[0]!=first:raise RestoreError("MULTIPLE_TOP_LEVEL_BACKUPS")
                if not (obj.isfile() or obj.isdir()):
                    raise RestoreError("ARCHIVE_CONTAINS_LINK_OR_SPECIAL_FILE")
                target=dest.joinpath(*components)
                if obj.isdir():
                    target.mkdir(mode=0o700,parents=True,exist_ok=True)
                else:
                    if obj.size<0 or obj.size>MAX_UNPACKED:
                        raise RestoreError("ARCHIVE_MEMBER_SIZE_INVALID")
                    total+=obj.size
                    if total>MAX_UNPACKED:raise RestoreError("DECOMPRESSED_SIZE_LIMIT")
                    target.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
                    descriptor=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
                    with os.fdopen(descriptor,"wb") as sink:
                        with contents.extractfile(obj) as source:
                            shutil.copyfileobj(source,sink,1024*1024)
                        sink.flush()
            # Exhaust tar stream before accepting the age result.
        age.stdout.close()
        if age.wait(timeout=20)!=0:
            raise RestoreError("AGE_AUTHENTICATION_OR_IDENTITY_FAILED")
        if first is None or not (dest/first).is_dir():
            raise RestoreError("EXTRACTED_BACKUP_ROOT_MISSING")
        backup=dest/first
        command=["/usr/bin/python3",str(MANAGER),"verify-backup","--backup",str(backup)]
        verify=subprocess.run(command,cwd=ASA,capture_output=True,text=True,timeout=120)
        if verify.returncode!=0 or "BACKUP_VERIFIED" not in verify.stdout:
            raise RestoreError("ASA_MANIFEST_OR_CHECKSUM_VERIFICATION_FAILED")
        print("ASA_BACKUP_CONTENTS_VERIFIED=YES")
        print("STAGED_BACKUP_PATH="+str(backup))
        print("STAGED_BYTES="+str(total))
        print("LIVE_POSTGRES_AND_MINIO_UNCHANGED=YES")
        return backup
    except BaseException:
        if age.poll() is None:age.kill()
        age.wait()
        # Leave failed staging private for inspection; no live state is touched.
        raise
    finally:
        if age.stdout:age.stdout.close()


def latest_staged_backup(cipher:Path)->Path:
    root=UNPACKED/cipher.name.removesuffix(".tar.age")
    if root.is_symlink() or not root.is_dir():
        raise RestoreError("NO_STAGED_VERIFIED_BACKUP")
    candidates=[p for p in root.iterdir() if p.is_dir() and not p.is_symlink()]
    if len(candidates)!=1 or not (candidates[0]/"manifest.json").is_file():
        raise RestoreError("STAGED_BACKUP_STRUCTURE_INVALID")
    return candidates[0]


def rehearsed_restore(backup:Path)->None:
    suffix=datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    name="asa_cloud_rehearsal_"+suffix+"_test"
    print("Будет создана НОВАЯ тестовая PostgreSQL БД: "+name)
    print("Боевая БД/MinIO не удаляются и не заменяются.")
    if input('Введите ровно RESTORE-TEST для подтверждения: ').strip()!="RESTORE-TEST":
        raise RestoreError("RESTORE_TEST_NOT_CONFIRMED")
    program=["/usr/bin/python3",str(MANAGER),"--root",str(ASA),
             "--profile","production","restore-check","--backup",str(backup),
             "--database",name]
    p=subprocess.run(program,cwd=ASA,capture_output=True,text=True,timeout=1800)
    if p.returncode!=0:
        raise RestoreError("ASA_RESTORE_CHECK_FAILED")
    print("ISOLATED_POSTGRES_RESTORE_CHECK=PASS")
    print("RESTORED_TEST_DATABASE="+name)
    print("PRODUCTION_DATABASE_UNTOUCHED=YES")
    print("MINIO_OBJECTS_STAGED_OFFLINE_NOT_PROMOTED=YES")


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action",nargs="?",choices=["list","download","prepare","restore-test"],
                        default="list")
    parser.add_argument("--source",choices=list(SOURCES))
    parser.add_argument("--archive")
    parser.add_argument("--identity",help="owner-held age identity on removable mounted recovery drive")
    opts=parser.parse_args()
    try:
        require_host()
        if bool(opts.source) != bool(opts.archive):
            raise RestoreError("SOURCE_AND_ARCHIVE_MUST_BE_SUPPLIED_TOGETHER")
        client=yandex.AppFolder(yandex.fresh_token())
        rows=inventory(client)
        if opts.action=="list":
            if not rows:
                print("Архивов age в Яндексе пока нет.")
            else:
                for record in rows[:100]:
                    print(f"[{record['source']}] {record['name']}  {record['size']/(1024**2):.1f} МиБ")
            print("LEGACY_OLDER_BACKUPS_MAY_HAVE_DIFFERENT_ENCRYPTION_KEYS=YES")
            print("READ_ONLY=YES")
            return 0
        if opts.source:
            target=next((x for x in rows if x["source"]==opts.source
                         and x["name"]==opts.archive),None)
            if target is None:raise RestoreError("ARCHIVE_NOT_FOUND_ON_YANDEX")
        else:
            target=choose(rows)
        cipher=download(client,target)
        if opts.action=="download":
            print("Проверка/восстановление требует личный age-ключ из офлайн-носителя.")
            return 0
        if opts.action=="restore-test":
            backup=latest_staged_backup(cipher)
        else:
            with offline_identity(opts.identity) as (key,pass_fds):
                backup=extract_verified(cipher,key,pass_fds)
        if opts.action=="restore-test":
            # Always verify manifest once more immediately before DB work.
            verify=subprocess.run(["/usr/bin/python3",str(MANAGER),"verify-backup",
                                   "--backup",str(backup)],cwd=ASA,
                                  capture_output=True,text=True,timeout=120)
            if verify.returncode!=0:raise RestoreError("STAGED_BACKUP_NO_LONGER_VALID")
            rehearsed_restore(backup)
        else:
            print("Чтобы проверить восстановление только в тестовую БД: sudo asa-school-restore restore-test")
        return 0
    except (RestoreError,yandex.BackupTransportError,OSError,ValueError,
            tarfile.TarError,subprocess.SubprocessError,EOFError,
            KeyboardInterrupt) as exc:
        message=str(exc) if isinstance(exc,(RestoreError,yandex.BackupTransportError)) else type(exc).__name__
        print("SCHOOL_YANDEX_RESTORE_STOPPED="+message,file=sys.stderr)
        return 2


if __name__=="__main__":
    raise SystemExit(main())
