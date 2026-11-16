#!/usr/bin/env python3
"""Idempotent H03 Tailscale FRP socket boot-order repair.

The two existing sockets use FreeBind=true and a pinned Tailscale address.
Depending on tailscaled.service from a sockets.target unit creates a systemd
boot ordering cycle. The sockets should be started before tailscaled.

Read-only plan by default. --apply handles only the exact two known sockets.
No DNS, Tailscale, routing, OpenResty, UFW or FRP client change.
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
UNIT_ROOT = Path("/etc/systemd/system")
AUDIT_ROOT = Path("/var/backups/asa-system-maintenance")
UNIT_ENDPOINTS = {
    "asa-frp-school-panel-tailnet.socket": ("100.75.149.26:14630", "asa-frp-school-panel-tailnet.service"),
    "asa-frp-school-ipmi-tailnet.socket": ("100.75.149.26:14610", "asa-frp-school-ipmi-tailnet.service"),
}
LEGACY_AFTER = "After=tailscaled.service"
NEW_AFTER = "After=sysinit.target"
OVERRIDE = "10-freebind-boot-order.conf"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def unit_candidate(name: str, content: str) -> tuple[str, bool]:
    """Return corrected content and whether an exact legacy entry is present."""
    require(name in UNIT_ENDPOINTS, "Unknown unit")
    endpoint, service = UNIT_ENDPOINTS[name]
    require(content.count("[Unit]\n") == 1 and content.count("[Socket]\n") == 1
            and content.count("[Install]\n") == 1, "Unknown unit section layout")
    for line in (
        "ListenStream=" + endpoint,
        "FreeBind=yes",
        "Service=" + service,
        "WantedBy=sockets.target",
        "NoDelay=true",
    ):
        require(content.count(line + "\n") == 1, "Unexpected socket contract: " + line)
    require(content.count(NEW_AFTER + "\n") + content.count(LEGACY_AFTER + "\n") == 1,
            "Unexpected After policy")
    require("Requires=tailscaled.service" not in content
            and "Wants=tailscaled.service" not in content, "Unexpected tailscaled binding")
    if LEGACY_AFTER + "\n" in content:
        return content.replace(LEGACY_AFTER + "\n", NEW_AFTER + "\n"), True
    return content, False


def run(*args: str) -> str:
    process = subprocess.run(args, check=False, capture_output=True,
                             text=True, timeout=35)
    require(process.returncode == 0,
            "Command failed: " + args[0] + " " + args[1] + " code " + str(process.returncode))
    return process.stdout


def safe_root_owned_file(path: Path) -> bytes:
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == 0
            and not info.st_mode & 0o022
            and not path.is_symlink(), "Unsafe unit file")
    return path.read_bytes()


def get_units() -> dict[str, tuple[Path, bytes, bytes, bool]]:
    result = {}
    for name in UNIT_ENDPOINTS:
        path = UNIT_ROOT / name
        current = safe_root_owned_file(path)
        candidate, changes = unit_candidate(name, current.decode("utf-8"))
        override = UNIT_ROOT / (name + ".d") / OVERRIDE
        require(not override.exists() and not override.is_symlink(),
                "A custom unit override exists: " + str(override))
        result[name] = (path, current, candidate.encode("utf-8"), changes)
    return result


def effective_check() -> None:
    for name in UNIT_ENDPOINTS:
        state = run("systemctl", "show", name, "-p", "After",
                    "-p", "Before", "-p", "UnitFileState", "-p", "ActiveState")
        require("tailscaled.service" not in state
                and "UnitFileState=enabled" in state
                and "ActiveState=active" in state
                and "sockets.target" in state,
                "Socket startup invariant failed: " + name)
        endpoint, _ = UNIT_ENDPOINTS[name]
        listen = run("ss", "-lntH")
        require(endpoint in listen, "Socket not listening on intended address")


def atomic_file(path: Path, content: bytes, mode: int = 0o644) -> None:
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == 0
            and not path.is_symlink(), "Unsafe destination for atomic write")
    fd, temporary = tempfile.mkstemp(prefix=".asa-frp-boot-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def inspect() -> dict[str, tuple[Path, bytes, bytes, bool]]:
    require(os.geteuid() == 0 and socket.gethostname() == HOST,
            "Requires root on H03")
    require(AUDIT_ROOT.is_dir() and not AUDIT_ROOT.is_symlink(),
            "Missing protected audit directory")
    audit_info = AUDIT_ROOT.stat()
    require(audit_info.st_uid == 0 and stat.S_IMODE(audit_info.st_mode) == 0o700,
            "Audit directory must be root-only")
    return get_units()


def apply() -> None:
    units = inspect()
    names = tuple(units)
    kinds = {units[name][3] for name in names}
    require(len(kinds) == 1, "Mixed old/new unit state: manual review required")
    if not kinds.pop():
        effective_check()
        print("ALREADY_ACCEPTED: both sockets active and boot order correct")
        return
    for name, (path, _, _, _) in units.items():
        state = run("systemctl", "show", name, "-p", "ActiveState", "-p", "UnitFileState")
        require("UnitFileState=enabled" in state, "Unowned disabled socket")
        _, service = UNIT_ENDPOINTS[name]
        require(run("systemctl", "is-active", service).strip() in ("inactive", "failed"),
                "Private proxy currently active: " + service)
    established = run("ss", "-Htan", "state", "established")
    for address, _ in UNIT_ENDPOINTS.values():
        require(address not in established, "Private connection in use")
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive = AUDIT_ROOT / ("frp-sockets-original-" + timestamp)
    archive.mkdir(mode=0o700)
    require(archive.stat().st_uid == 0
            and stat.S_IMODE(archive.stat().st_mode) == 0o700, "Unsafe archive")
    receipt = {}
    for name, (_, previous, _, _) in units.items():
        record = archive / (name + ".original")
        fd = os.open(record, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as out:
            out.write(previous)
            out.flush()
            os.fsync(out.fileno())
        receipt[name] = {"sha256": hashlib.sha256(previous).hexdigest()}
    (archive / "metadata.json").write_text(
        json.dumps({"at": timestamp, "units": receipt}, indent=2) + "\n",
        encoding="utf-8",
    )
    (archive / "metadata.json").chmod(0o600)
    print("BACKUP_CREATED", str(archive), flush=True)
    try:
        run("systemctl", "stop", *names)
        for name, (path, _, updated, _) in units.items():
            atomic_file(path, updated)
        run("systemctl", "daemon-reload")
        for name in names:
            state = run("systemctl", "show", name, "-p", "After", "-p", "UnitFileState")
            require("tailscaled.service" not in state
                    and "UnitFileState=enabled" in state, "Cycle still present")
        run("systemctl", "start", *names)
        effective_check()
        print("H03_FRP_SOCKET_BOOT_ORDER_ACCEPTED", flush=True)
    except BaseException:
        subprocess.run(["systemctl", "stop", *names], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for name, (path, old, _, _) in units.items():
            atomic_file(path, old)
        subprocess.run(["systemctl", "daemon-reload"], check=False)
        print("RESTORED_ORIGINAL_UNIT_CONTENT_AFTER_FAILURE", flush=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        units = inspect()
        print("PLAN", {name: "fix-after" if entry[3] else "already-fixed"
                       for name, entry in units.items()})
        if args.apply:
            apply()
        else:
            print("NO_CHANGES")
        return 0
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
        print("BLOCKED", type(error).__name__, str(error)[:170])
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
