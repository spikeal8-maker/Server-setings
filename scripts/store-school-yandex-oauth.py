#!/usr/bin/env python3
"""Receive a fresh Yandex OAuth token over trusted Proxmox guest stdin.

This helper never prints, logs, accepts argv secrets, or overwrites an existing
rclone authorization. It is only for the new isolated school VM100.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import socket
import stat
import sys

CLOUD_ROOT = Path("/srv/asa-cloud-storage")
CONFIG = CLOUD_ROOT / "config" / "rclone.conf"
MAX_BYTES = 16384


def decode_token(payload: bytes) -> dict:
    if not isinstance(payload, bytes) or not 1 <= len(payload) <= MAX_BYTES:
        raise ValueError("OAUTH_PAYLOAD_LENGTH")
    try:
        value = json.loads(payload)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("OAUTH_JSON_INVALID") from exc
    if not isinstance(value, dict):
        raise ValueError("OAUTH_OBJECT_REQUIRED")
    for field in ("access_token", "token_type"):
        item = value.get(field)
        if not isinstance(item, str) or not 3 <= len(item) <= 8192:
            raise ValueError("OAUTH_FIELD_" + field.upper())
    if len(value["access_token"]) < 20:
        raise ValueError("OAUTH_ACCESS_TOKEN_INVALID")
    refresh = value.get("refresh_token")
    if refresh is not None and (not isinstance(refresh, str) or len(refresh) > 8192):
        raise ValueError("OAUTH_REFRESH_INVALID")
    expiry = value.get("expiry")
    if expiry is not None and (not isinstance(expiry, str) or len(expiry) > 100):
        raise ValueError("OAUTH_EXPIRY_INVALID")
    if len(value) > 12:
        raise ValueError("OAUTH_UNEXPECTED_FIELDS")
    return value


def config_bytes(value: dict) -> bytes:
    # Rclone's official Yandex backend reads the JSON token as a config field.
    return ("[yandex]\ntype = yandex\ntoken = " +
            json.dumps(value, ensure_ascii=True, separators=(",", ":")) +
            "\n").encode("ascii")


def check_location() -> None:
    if os.geteuid() != 0 or socket.gethostname() != "asa-school-01":
        raise ValueError("WRONG_HOST_OR_PRIVILEGES")
    if CLOUD_ROOT.is_symlink() or not CLOUD_ROOT.is_dir():
        raise ValueError("CLOUD_ROOT_IDENTITY")
    root = CLOUD_ROOT.stat()
    if root.st_uid != 0 or stat.S_IMODE(root.st_mode) != 0o700:
        raise ValueError("CLOUD_ROOT_PERMISSIONS")
    directory = CONFIG.parent
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("CLOUD_CONFIG_DIRECTORY")
    meta = directory.stat()
    if meta.st_uid != 1000 or stat.S_IMODE(meta.st_mode) != 0o700:
        raise ValueError("CLOUD_CONFIG_OWNER")
    if CONFIG.is_symlink() or not CONFIG.is_file():
        raise ValueError("CLOUD_CONFIG_NOT_REGULAR")
    current = CONFIG.stat()
    if current.st_uid != 1000 or stat.S_IMODE(current.st_mode) != 0o600:
        raise ValueError("CLOUD_CONFIG_PERMISSIONS")


def install_token(raw: bytes) -> None:
    token = decode_token(raw)
    check_location()
    data = config_bytes(token)
    # No temporary credential files. The existing empty, dedicated file is
    # exclusively locked while its identity and size are checked.
    fd = os.open(CONFIG, os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        meta = os.fstat(fd)
        if (not stat.S_ISREG(meta.st_mode) or meta.st_uid != 1000 or
                stat.S_IMODE(meta.st_mode) != 0o600 or meta.st_size != 0):
            raise ValueError("CLOUD_OAUTH_ALREADY_CONFIGURED")
        try:
            with os.fdopen(os.dup(fd), "wb", closefd=True) as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        except Exception:
            os.ftruncate(fd, 0)
            os.fsync(fd)
            raise
    finally:
        os.close(fd)


def main() -> int:
    try:
        # stdin only. Never print token content, CLI arguments or environment.
        payload = sys.stdin.buffer.read(MAX_BYTES + 1)
        install_token(payload)
        print("YANDEX_OAUTH_VM100_SAVED=YES")
        print("YANDEX_OAUTH_CONFIG_MODE=0600")
        print("DATA_BACKUP_SCHEDULER=UNCHANGED")
        return 0
    except (ValueError, OSError) as exc:
        print("YANDEX_OAUTH_VM100_ERROR=" +
              (str(exc) if isinstance(exc, ValueError) else "IO_ERROR"),
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
