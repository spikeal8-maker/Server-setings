#!/usr/bin/env python3
"""Repair ONLY a failed default-rclone Yandex OAuth on school VM100.

An owner's new OAuth token and working custom app credentials arrive through
pinned SSH -> Proxmox QGA stdin. Never put them in argv, logs or GitHub.
The previous credentials are held only in RAM and restored on failed probe.
"""
from __future__ import annotations
import configparser
import fcntl
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys

ROOT = Path("/srv/asa-cloud-storage")
CONFIG = ROOT / "config" / "rclone.conf"
MAX_BYTES = 16384
RC = ["docker", "exec", "asa-cloud-storage-rclone-1", "rclone",
      "--config", "/config/rclone/rclone.conf",
      "--contimeout", "8s", "--timeout", "12s", "--retries", "1",
      "--max-duration", "22s"]


def parse_payload(raw: bytes) -> tuple[dict, str, str]:
    if not 1 <= len(raw) <= MAX_BYTES:
        raise ValueError("OAUTH_PAYLOAD_LENGTH")
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("OAUTH_JSON_INVALID") from exc
    if not isinstance(payload, dict):
        raise ValueError("OAUTH_OBJECT_REQUIRED")
    if set(payload) != {"token", "client_id", "client_secret", "repair_bad_standard"}:
        raise ValueError("OAUTH_FIELDS_REJECTED")
    if payload.get("repair_bad_standard") is not True:
        raise ValueError("REPAIR_NOT_OWNER_CONFIRMED")
    client_id = payload.get("client_id")
    client_secret = payload.get("client_secret")
    if not isinstance(client_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{10,256}", client_id):
        raise ValueError("OAUTH_CUSTOM_CLIENT_ID_INVALID")
    if (not isinstance(client_secret, str) or not 10 <= len(client_secret) <= 1024
            or not all(33 <= ord(x) <= 126 for x in client_secret)):
        raise ValueError("OAUTH_CUSTOM_SECRET_INVALID")
    token = payload.get("token")
    if not isinstance(token, dict):
        raise ValueError("OAUTH_TOKEN_REQUIRED")
    if not isinstance(token.get("access_token"), str) or len(token["access_token"]) < 20:
        raise ValueError("OAUTH_ACCESS_TOKEN_INVALID")
    if token.get("token_type") not in ("OAuth", "Bearer"):
        raise ValueError("OAUTH_TYPE_INVALID")
    if token.get("refresh_token") is not None and not isinstance(token["refresh_token"], str):
        raise ValueError("OAUTH_REFRESH_INVALID")
    if len(json.dumps(token)) > 12000:
        raise ValueError("OAUTH_TOKEN_TOO_LARGE")
    return token, client_id, client_secret


def build_config(token: dict, client_id: str, client_secret: str) -> bytes:
    return ("[yandex]\ntype = yandex\nclient_id = " + client_id +
            "\nclient_secret = " + client_secret +
            "\ntoken = " + json.dumps(token, ensure_ascii=True, separators=(",", ":")) +
            "\n").encode("ascii")


def is_failed_standard_config(contents: bytes) -> bool:
    try:
        c = configparser.ConfigParser(interpolation=None)
        c.read_string(contents.decode("utf-8"))
        return ("yandex" in c and c.get("yandex", "type") == "yandex"
                and bool(c.get("yandex", "token", fallback=""))
                and not c.get("yandex", "client_id", fallback="")
                and not c.get("yandex", "client_secret", fallback=""))
    except (ValueError, UnicodeError, configparser.Error, KeyError):
        return False


def check_identity() -> None:
    if os.geteuid() != 0 or socket.gethostname() != "asa-school-01":
        raise ValueError("UNEXPECTED_EXECUTION_HOST")
    if ROOT.is_symlink() or not ROOT.is_dir():
        raise ValueError("CLOUD_ROOT_IDENTITY")
    if ROOT.stat().st_uid != 0 or stat.S_IMODE(ROOT.stat().st_mode) != 0o700:
        raise ValueError("CLOUD_ROOT_PERMISSIONS")
    if CONFIG.parent.is_symlink() or CONFIG.parent.stat().st_uid != 1000:
        raise ValueError("CLOUD_CONFIG_DIRECTORY")
    if CONFIG.is_symlink() or not CONFIG.is_file():
        raise ValueError("CLOUD_CONFIG_IDENTITY")
    meta = CONFIG.stat()
    if meta.st_uid != 1000 or stat.S_IMODE(meta.st_mode) != 0o600:
        raise ValueError("CLOUD_CONFIG_PERMISSIONS")


def remote_probe() -> tuple[bool, bool]:
    try:
        result = subprocess.run(RC + ["lsjson", "--stat", "yandex:ASA-Backups"],
                                capture_output=True, text=True, timeout=33, check=False)
    except subprocess.TimeoutExpired:
        return False, False
    return result.returncode == 0, "410" in result.stderr and "Gone" in result.stderr


def write_locked(stream, contents: bytes) -> None:
    stream.seek(0)
    stream.truncate(0)
    stream.write(contents)
    stream.flush()
    os.fsync(stream.fileno())


def repair(raw: bytes) -> None:
    token, client_id, client_secret = parse_payload(raw)
    check_identity()
    replacement = build_config(token, client_id, client_secret)
    with os.fdopen(os.open(CONFIG, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC),
                   "r+b", buffering=0) as target:
        fcntl.flock(target.fileno(), fcntl.LOCK_EX)
        meta = os.fstat(target.fileno())
        if not stat.S_ISREG(meta.st_mode) or meta.st_uid != 1000 or stat.S_IMODE(meta.st_mode) != 0o600:
            raise ValueError("CLOUD_CONFIG_CHANGED")
        old = target.read()
        if not is_failed_standard_config(old):
            raise ValueError("ONLY_FAILED_STANDARD_OAUTH_CAN_BE_REPLACED")
        before_ok, before_gone = remote_probe()
        if before_ok or not before_gone:
            raise ValueError("OLD_CREDENTIAL_NOT_PROVEN_410_GONE")
        try:
            write_locked(target, replacement)
            after_ok, _ = remote_probe()
            if not after_ok:
                raise ValueError("NEW_OAUTH_FOLDER_NOT_ACCESSIBLE")
        except Exception:
            write_locked(target, old)
            raise


def main() -> int:
    try:
        repair(sys.stdin.buffer.read(MAX_BYTES + 1))
        print("YANDEX_CUSTOM_OAUTH_VM100_CONNECTED=YES")
        print("YANDEX_DIRECTORY_ASA_BACKUPS_VERIFIED=YES")
        print("TOKEN_NOT_PRINTED=YES")
        print("BACKUP_SCHEDULER_UNCHANGED=YES")
        return 0
    except (ValueError, OSError) as exc:
        print("YANDEX_REPAIR_ERROR=" +
              (str(exc) if isinstance(exc, ValueError) else "IO_ERROR"),
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
