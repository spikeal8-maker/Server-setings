#!/usr/bin/env python3
"""School VM100 encrypted grandfather-father-son backups to Yandex app-folder.

Hourly = PostgreSQL only (no user-visible writer pause).
Daily = the existing verified ASA manager PostgreSQL+MinIO+configuration export.
All uploads use the established owner-held public age recipient and the
school Yandex app-folder transport, including a full ciphertext SHA256 readback.
No application install/update, network changes, or live restore are performed.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import importlib.util
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

import school_yandex_app_folder as cloud

HOST = "asa-school-01"
SCRIPTS = Path(__file__).resolve().parent
ASA = Path("/srv/asa-lab")
ROOT = Path("/var/backups/asa-school-gfs")
STAGING = ROOT / "staging"
ARCHIVES = ROOT / "archives"
RECEIPTS = ROOT / "receipts"
LOCK = Path("/run/lock/asa-school-gfs.lock")
PUBLIC_RECIPIENT = Path("/etc/server-settings/age-recipients.txt")
REMOTE = "app:/School-VM100/ASA-Lab-Full-Server"
MOSCOW = ZoneInfo("Europe/Moscow")
POSTGRES = "asa-lab-school-postgres-1"
RETENTION = {"hourly-db": 24, "daily-full": 7, "weekly-full": 4, "monthly-full": 12}
LOCAL_KEEP = {"hourly": 6, "full": 2}
BACKUP_NAME = re.compile(r"asa-school-01-(\d{8}T\d{6}Z)-[a-f0-9]{8}\.tar\.age\Z")


class GfsError(RuntimeError):
    pass


def require(condition: bool, code: str) -> None:
    if not condition:
        raise GfsError(code)


def command(args: list[str], *, timeout: int = 60, stdout=None) -> subprocess.CompletedProcess:
    try:
        p = subprocess.run(args, stdout=stdout if stdout is not None else subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise GfsError("BACKUP_COMMAND_UNAVAILABLE_OR_TIMEOUT") from None
    require(p.returncode == 0, "BACKUP_COMMAND_FAILED")
    return p


def paths_present() -> None:
    for directory in (ROOT, STAGING, ARCHIVES, RECEIPTS, ARCHIVES/"hourly", ARCHIVES/"full"):
        require(directory.is_dir() and not directory.is_symlink(), "GFS_DIRECTORY_NOT_READY")
        st = directory.stat()
        require(st.st_uid == 0 and stat.S_IMODE(st.st_mode) == 0o700,
                "GFS_DIRECTORY_OWNER_MODE")


def existing_nightly():
    spec = importlib.util.spec_from_file_location(
        "school_legacy_nightly", SCRIPTS/"school-nightly-database-backup.py"
    )
    require(spec is not None and spec.loader is not None, "NIGHTLY_MODULE_MISSING")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def basic_preflight() -> dict:
    require(os.geteuid() == 0 and socket.gethostname() == HOST, "SCHOOL_VM100_ROOT_REQUIRED")
    paths_present()
    existing_nightly().check_host()
    require(not (ASA/".asa/failed-switch.json").exists(), "FAILED_SWITCH_ACTIVE")
    require(shutil.disk_usage(ROOT).free >= 15 * 1024**3, "INSUFFICIENT_LOCAL_DISK")
    record = json.loads((ASA/".asa/installed-release.json").read_text(encoding="utf-8"))
    config = json.loads((ASA/".asa/installation.json").read_text(encoding="utf-8"))
    require(config.get("profile") == "production" and
            config.get("project") == "asa-lab-school", "WRONG_ASA_PROJECT")
    require(isinstance(record.get("revision"), str) and isinstance(record.get("schema"), int),
            "RELEASE_METADATA_INVALID")
    require(not (ASA/".asa/maintenance.json").exists(), "MAINTENANCE_IN_PROGRESS")
    return record


def get_provider(tier: str):
    require(tier in RETENTION, "UNKNOWN_RETENTION_TIER")
    cloud.ROOT = REMOTE + "/" + tier
    provider = cloud.AppFolder(cloud.fresh_token())
    provider.folder_preflight()
    return provider


def before_export(kind: str) -> dict:
    record = basic_preflight()
    get_provider("hourly-db" if kind == "hourly" else "daily-full")
    if kind == "hourly":
        status = command(["docker", "inspect", "--format", "{{.State.Health.Status}}", POSTGRES])
        require(status.stdout.decode("utf-8", "replace").strip() == "healthy",
                "POSTGRES_NOT_HEALTHY")
    else:
        require(dt.datetime.now(MOSCOW).hour in (0, 1),
                "FULL_BACKUP_NIGHT_ONLY")
    return record


def db_query(sql: str) -> str:
    # postgres shell expands credentials from its OWN protected environment.
    p = command(["docker", "exec", POSTGRES, "sh", "-eu", "-c",
                 'psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atqc "$1"',
                 "sql", sql], timeout=30)
    return p.stdout.decode("utf-8", "replace").strip()


def verified_hourly_export(stage: Path, release: dict) -> Path:
    target = stage / "snapshot"
    target.mkdir(mode=0o700)
    from_parent = str(ASA)
    sys.path.insert(0, from_parent + "/tools")
    from deployment.system import operation_lock
    with operation_lock(ASA):
        require(not (ASA/".asa/maintenance.json").exists(), "MAINTENANCE_IN_PROGRESS")
        before = db_query("SELECT max(version) FROM schema_migrations")
        require(before.isdigit() and int(before) == release["schema"], "SCHEMA_MISMATCH")
        with (target/"database.dump").open("wb") as output:
            command(["docker", "exec", POSTGRES, "sh", "-eu", "-c",
                     'pg_dump --format=custom -U "$POSTGRES_USER" -d "$POSTGRES_DB"'],
                    timeout=360, stdout=output)
        require((target/"database.dump").stat().st_size > 1024, "EMPTY_DATABASE_DUMP")
        with (target/"globals.sql").open("wb") as output:
            command(["docker", "exec", POSTGRES, "sh", "-eu", "-c",
                     'pg_dumpall --globals-only --no-role-passwords -U "$POSTGRES_USER"'],
                    timeout=120, stdout=output)
        # A custom pg_dump archive requires a seekable archive for verification.
        verify_path = "/tmp/asa-gfs-check-" + uuid.uuid4().hex + ".dump"
        try:
            command(["docker", "cp", str(target/"database.dump"),
                     POSTGRES + ":" + verify_path], timeout=90)
            command(["docker", "exec", POSTGRES, "pg_restore", "--list", verify_path], timeout=45)
        finally:
            command(["docker", "exec", POSTGRES, "rm", "-f", verify_path], timeout=30)
        after = db_query("SELECT max(version) FROM schema_migrations")
        require(before == after, "SCHEMA_MOVED_DURING_DUMP")
    checksum = hashlib.sha256((target/"database.dump").read_bytes()).hexdigest()
    metadata = {
        "format": "asa-school-gfs-hourly-v1",
        "createdAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "scope": "postgresql-only",
        "minioIncluded": False,
        "canRestoreFullApplicationAlone": False,
        "revision": release["revision"],
        "schema": release["schema"],
        "databaseDumpSha256": checksum,
        "databaseDumpBytes": (target/"database.dump").stat().st_size,
    }
    (target/"metadata.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    return target


def verified_full_export(stage: Path) -> Path:
    # The existing ASA manager stops all writers briefly, exports PostgreSQL,
    # MinIO objects and private configuration, verifies hashes, then restarts.
    return existing_nightly().export_asa(stage)


def age_encrypt(stage: Path, exported: Path, result: Path) -> None:
    partial = result.with_name("." + result.name + ".incomplete")
    require(not result.exists() and not partial.exists() and
            not result.is_symlink() and not partial.is_symlink(), "ARCHIVE_ALREADY_EXISTS")
    tar = subprocess.Popen(
        ["/usr/bin/tar", "-C", str(stage), "-cf", "-", "--", exported.name],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )
    try:
        with partial.open("wb") as ciphertext:
            age = subprocess.run(["/usr/bin/age", "-R", str(PUBLIC_RECIPIENT)],
                                 stdin=tar.stdout, stdout=ciphertext,
                                 stderr=subprocess.PIPE, timeout=600, check=False)
        tar.stdout.close()
        rc = tar.wait(timeout=30)
        require(rc == 0 and age.returncode == 0 and partial.stat().st_size > 128,
                "AGE_ENCRYPTION_FAILED")
        require(partial.open("rb").read(30).startswith(b"age-encryption.org/v1"),
                "AGE_HEADER_INVALID")
        os.chmod(partial, 0o600)
        os.replace(partial, result)
    finally:
        if tar.poll() is None:
            tar.kill()
            tar.wait()
        if partial.exists():
            partial.unlink()


def list_remote(provider, tier: str) -> list[str]:
    cloud.ROOT = REMOTE + "/" + tier
    code, data = provider.api("/resources", params={"path": cloud.ROOT, "limit": 1000, "offset": 0})
    require(code == 200, "REMOTE_RETENTION_LIST_FAILED")
    embedded = data.get("_embedded")
    require(isinstance(embedded, dict), "REMOTE_RETENTION_STRUCTURE_INVALID")
    names = embedded.get("items")
    total = embedded.get("total")
    require(isinstance(names, list) and isinstance(total, int) and total == len(names),
            "REMOTE_RETENTION_PAGINATION_UNVERIFIED")
    return sorted([x["name"] for x in names
                   if isinstance(x, dict) and x.get("type") == "file"
                   and isinstance(x.get("name"), str)
                   and BACKUP_NAME.fullmatch(x["name"])])


def prune_remote(provider, tier: str, *, apply: bool) -> list[str]:
    require(tier in RETENTION, "UNKNOWN_RETENTION_TIER")
    entries = list_remote(provider, tier)
    obsolete = entries[:-RETENTION[tier]]
    if apply:
        for name in obsolete:
            require(bool(BACKUP_NAME.fullmatch(name)), "RETENTION_NAME_UNTRUSTED")
            cloud.ROOT = REMOTE + "/" + tier
            status, _ = provider.api("/resources", method="DELETE",
                                     params={"path": cloud.ROOT + "/" + name, "permanently": "false"})
            require(status in (202, 204), "REMOTE_RETENTION_DELETE_UNCONFIRMED")
    return obsolete


def atomic_receipt(kind: str, value: dict) -> None:
    target = RECEIPTS / ("latest-" + kind + ".json")
    require(target.name in ("latest-full.json", "latest-hourly.json"), "RECEIPT_NAME_INVALID")
    require(not target.is_symlink(), "RECEIPT_SYMLINK")
    fd, temp = tempfile.mkstemp(prefix=".gfs-receipt-", dir=RECEIPTS)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            os.fchmod(fp.fileno(), 0o600)
            json.dump(value, fp, sort_keys=True)
            fp.write("\n")
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(temp, target)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def local_retention(kind: str) -> None:
    directory = ARCHIVES / kind
    old = sorted(p for p in directory.iterdir()
                 if p.is_file() and not p.is_symlink() and BACKUP_NAME.fullmatch(p.name))
    for file in old[:-LOCAL_KEEP[kind]]:
        require(file.stat().st_uid == 0 and stat.S_IMODE(file.stat().st_mode) == 0o600,
                "LOCAL_ARCHIVE_OWNER_MODE")
        file.unlink()


def execute(kind: str, *, manual: bool = False) -> None:
    require(kind in ("hourly", "full"), "UNKNOWN_BACKUP_KIND")
    if kind == "full" and not manual:
        require(dt.datetime.now(MOSCOW).hour in (0, 1), "FULL_BACKUP_NIGHT_ONLY")
    fd = os.open(LOCK, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        release = basic_preflight()
        tier = "hourly-db" if kind == "hourly" else "daily-full"
        provider = get_provider(tier)  # Authenticate/write slot BEFORE any writer pause.
        if kind == "hourly":
            check = command(["docker", "inspect", "--format", "{{.State.Health.Status}}", POSTGRES])
            require(check.stdout.decode().strip() == "healthy", "POSTGRES_NOT_HEALTHY")
        else:
            require(manual or dt.datetime.now(MOSCOW).hour in (0, 1), "FULL_BACKUP_NIGHT_ONLY")
        stage = Path(tempfile.mkdtemp(prefix=".gfs-", dir=STAGING))
        archive = ARCHIVES / kind / (
            "asa-school-01-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            + "-" + uuid.uuid4().hex[:8] + ".tar.age")
        try:
            saved = verified_hourly_export(stage, release) if kind == "hourly" else verified_full_export(stage)
            age_encrypt(stage, saved, archive)
        finally:
            # A failed full writer restart leaves a maintenance marker; keep
            # the protected staged data for human recovery rather than delete.
            if not (ASA/".asa/maintenance.json").exists():
                shutil.rmtree(stage)
        kinds = [tier]
        now = dt.datetime.now(MOSCOW)
        if kind == "full":
            if now.weekday() == 6:
                kinds.append("weekly-full")
            if now.day == 1:
                kinds.append("monthly-full")
        verified = {}
        for destination in kinds:
            target_provider = get_provider(destination)
            proof = target_provider.upload_and_verify(archive)
            require(proof.get("download_sha256_verified") is True, "REMOTE_SHA256_NOT_VERIFIED")
            verified[destination] = {
                "path": REMOTE + "/" + destination + "/" + archive.name,
                "sha256": proof["sha256"],
                "bytes": proof["size"],
            }
        receipt = {
            "format": "asa-school-gfs-receipt-v1",
            "createdAt": dt.datetime.now(dt.timezone.utc).isoformat(),
            "kind": kind, "revision": release["revision"], "schema": release["schema"],
            "archive": archive.name, "remote": verified,
            "remoteFullReadbackSha256": True,
            "encrypted": "age", "recoveryKeyProvenOffServer": False,
        }
        atomic_receipt(kind, receipt)
        for destination in kinds:
            provider = get_provider(destination)
            outdated = prune_remote(provider, destination, apply=True)
            print("RETENTION", destination, "obsolete_removed", len(outdated), flush=True)
        local_retention(kind)
        print("GFS_BACKUP_VERIFIED", json.dumps({
            "kind": kind, "archive": archive.name,
            "schema": release["schema"],
            "remote": sorted(verified),
            "sha256": next(iter(verified.values()))["sha256"],
        }, sort_keys=True), flush=True)
    finally:
        os.close(fd)


def preflight(kind: str) -> None:
    basic_preflight()
    get_provider("hourly-db" if kind == "hourly" else "daily-full")
    if kind == "hourly":
        status = command(["docker", "inspect", "--format", "{{.State.Health.Status}}", POSTGRES])
        require(status.stdout.decode().strip() == "healthy", "POSTGRES_NOT_HEALTHY")
    print("GFS_PREFLIGHT_PASS", kind, "writers_stopped=NO", flush=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=("hourly", "full"), required=True)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--apply", action="store_true")
    group.add_argument("--preflight", action="store_true")
    parser.add_argument("--manual", action="store_true", help="explicit manual full backup outside night")
    args = parser.parse_args(argv)
    try:
        if args.preflight:
            preflight(args.kind)
        elif args.apply:
            execute(args.kind, manual=args.manual)
        else:
            print("PLAN", args.kind, "remote", REMOTE,
                  "retention", json.dumps(RETENTION, sort_keys=True))
        return 0
    except (GfsError, OSError, ValueError, KeyError, json.JSONDecodeError,
            cloud.BackupTransportError, subprocess.SubprocessError,
            BlockingIOError) as error:
        safe = str(error) if isinstance(error, (GfsError, cloud.BackupTransportError)) else type(error).__name__
        print("GFS_BLOCKED=" + safe, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
