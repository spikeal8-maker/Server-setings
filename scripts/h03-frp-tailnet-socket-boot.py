#!/usr/bin/env python3
"""H03 host-bound correction for FRP private socket boot-order cycles.

The socket binds its Tailscale IPv4 with FreeBind=yes. Depending on
tailscaled.service puts this socket after sockets.target's own dependency
chain and prevents it from starting during boot. Only the two known H03
private sockets may be changed. Default mode never writes.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import tempfile

HOST = "admins-MS-7D22"
ROOT = Path("/etc/systemd/system")
BACKUP_DIR = Path("/var/backups/asa-system-maintenance")
EXPECTED = {
    "asa-frp-school-panel-tailnet.socket": "100.75.149.26:14630",
    "asa-frp-school-ipmi-tailnet.socket": "100.75.149.26:14610",
}
OLD = b"After=tailscaled.service\n"
NEW = b"After=sysinit.target\n"


class Refuse(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise Refuse(message)


def unit_file(name: str) -> Path:
    require(name in EXPECTED, "Неизвестный socket unit")
    return ROOT / name


def classify_unit(path: Path, address: str) -> str:
    raw = path.read_bytes()
    require(b"FreeBind=yes" in raw and b"ListenStream=" + address.encode() in raw,
            "Не совпадает Tailscale адрес/порт или FreeBind")
    require(raw.count(OLD) + raw.count(NEW) == 1,
            "Непредвиденный порядок systemd")
    return "old" if OLD in raw else "corrected"


def check_units() -> dict[str, str]:
    states: dict[str, str] = {}
    for name, address in EXPECTED.items():
        path = unit_file(name)
        metadata = path.lstat()
        require(stat.S_ISREG(metadata.st_mode) and not path.is_symlink()
                and metadata.st_uid == 0 and not metadata.st_mode & 0o022,
                "Небезопасный unit: " + name)
        dropin = ROOT / (name + ".d") / "10-freebind-boot-order.conf"
        require(not dropin.exists() and not dropin.is_symlink(),
                "Неожиданный override: " + name)
        states[name] = classify_unit(path, address)
    require(len(set(states.values())) == 1, "Частично изменённая конфигурация socket units")
    return states


def run(*args: str) -> str:
    result = subprocess.run(args, check=False, text=True, capture_output=True, timeout=40)
    require(result.returncode == 0,
            "Ошибка " + args[0] + " " + (args[1] if len(args) > 1 else "")
            + " код=" + str(result.returncode))
    return result.stdout


def check_host() -> None:
    require(os.geteuid() == 0 and socket.gethostname() == HOST,
            "Требуется root на тестовом сервере H03")
    require(run("systemctl", "is-active", "tailscaled").strip() == "active",
            "Tailscale не запущен")
    require(BACKUP_DIR.is_dir() and not BACKUP_DIR.is_symlink()
            and BACKUP_DIR.stat().st_uid == 0
            and stat.S_IMODE(BACKUP_DIR.stat().st_mode) == 0o700,
            "Нет защищённого каталога аудита")


def safe_atomic(path: Path, value: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=".asa-frp-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), 0o644)
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def check_no_connections() -> None:
    sessions = run("ss", "-Htan", "state", "established")
    require(not any(value in sessions for value in EXPECTED.values()),
            "Есть активные приватные соединения")
    for name in EXPECTED:
        service = name.replace(".socket", ".service")
        result = subprocess.run(["systemctl", "is-active", "--quiet", service],
                                capture_output=True, timeout=15, check=False)
        require(result.returncode != 0, "Socket proxy используется: " + service)


def save_originals() -> tuple[Path, dict[str, bytes]]:
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder = BACKUP_DIR / ("frp-sockets-before-" + stamp)
    folder.mkdir(mode=0o700)
    raw: dict[str, bytes] = {}
    metadata = []
    for name in EXPECTED:
        value = unit_file(name).read_bytes()
        raw[name] = value
        target = folder / (name + ".original")
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(value); stream.flush(); os.fsync(stream.fileno())
        metadata.append({"unit": name, "sha256": hashlib.sha256(value).hexdigest()})
    report = folder / "report.json"
    fd = os.open(report, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump({"at": stamp, "before": metadata,
                   "change": "remove After=tailscaled.service; retain FreeBind=yes"}, stream, indent=2)
        stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
    return folder, raw


def apply() -> None:
    states = check_units()
    if next(iter(states.values())) == "corrected":
        for name in EXPECTED:
            after = run("systemctl", "show", name, "-p", "After", "-p", "ActiveState", "-p", "UnitFileState")
            require("tailscaled.service" not in after and "ActiveState=active" in after
                    and "UnitFileState=enabled" in after, "Принятая конфигурация неактивна")
        print("ALREADY_CORRECTED_PRIVATE_SOCKETS_ACTIVE")
        return
    check_no_connections()
    original = {name: unit_file(name).read_bytes() for name in EXPECTED}
    backup, _ = save_originals()
    print("ORIGINAL_UNITS_BACKED_UP", backup, flush=True)
    try:
        run("systemctl", "stop", *EXPECTED)
        for name in EXPECTED:
            safe_atomic(unit_file(name), original[name].replace(OLD, NEW))
        run("systemctl", "daemon-reload")
        for name in EXPECTED:
            after = run("systemctl", "show", name, "-p", "After", "-p", "UnitFileState")
            require("tailscaled.service" not in after and "UnitFileState=enabled" in after,
                    "Осталась зависимость tailscaled.service")
        run("systemctl", "start", *EXPECTED)
        for name in EXPECTED:
            after = run("systemctl", "show", name, "-p", "ActiveState", "-p", "UnitFileState")
            require("ActiveState=active" in after and "UnitFileState=enabled" in after,
                    "Сокет не активирован")
        print("H03_PRIVATE_SOCKET_BOOT_ORDER_PASS", flush=True)
    except Exception:
        for name in EXPECTED:
            subprocess.run(["systemctl", "stop", name], check=False, capture_output=True)
        for name in EXPECTED:
            safe_atomic(unit_file(name), original[name])
        subprocess.run(["systemctl", "daemon-reload"], check=False, capture_output=True)
        print("RESTORED_OLD_SOCKET_UNIT_FILES", flush=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        check_host()
        states = check_units()
        print("PLAN", states)
        if args.apply:
            apply()
        else:
            print("DRY_RUN_NO_CHANGES")
        return 0
    except (Refuse, OSError, subprocess.TimeoutExpired, ValueError) as error:
        print("FRP_BOOT_REPAIR_BLOCKED", type(error).__name__,
              str(error)[:180])
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
