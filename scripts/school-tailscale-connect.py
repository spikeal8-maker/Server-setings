#!/usr/bin/env python3
"""Enroll school VM100 in the owner's tailnet with a one-off auth key.

Interactive host-local operation; never put authentication keys in shell
history, process arguments, GitHub, ChatGPT, logs, or permanent configuration.
The actual Tailscale device identity persists in tailscaled.state after login.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile

HOST = "asa-school-01"
TAILNET_SUFFIX = "tail605710.ts.net."
KEYS_PAGE = "https://login.tailscale.com/admin/settings/keys"


class TailnetSetupError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def check_host(expected: str) -> None:
    if (os.geteuid() != 0 or socket.gethostname() != HOST
            or expected != HOST):
        raise TailnetSetupError("ROOT_ON_SCHOOL_VM100_REQUIRED")


def validate_key(secret: str) -> str:
    token = secret.strip()
    if not re.fullmatch(r"tskey-auth-[A-Za-z0-9_-]{16,256}", token):
        raise TailnetSetupError("TAILSCALE_ONE_OFF_KEY_FORMAT_INVALID")
    return token


def up_command(auth_file: str) -> list[str]:
    if not auth_file.startswith("/run/tailscale/") or "\x00" in auth_file:
        raise TailnetSetupError("AUTHKEY_FILE_LOCATION_INVALID")
    return [
        "/usr/bin/tailscale", "up",
        "--auth-key=file:" + auth_file,
        "--hostname=" + HOST,
        "--accept-dns=false",
        "--accept-routes=false",
        "--netfilter-mode=off",
    ]


def status() -> dict:
    result = subprocess.run(
        ["/usr/bin/tailscale", "status", "--json"],
        capture_output=True, text=True, timeout=12, check=False,
    )
    if result.returncode:
        raise TailnetSetupError("TAILSCALE_STATUS_NOT_AVAILABLE")
    try:
        data = json.loads(result.stdout)
    except ValueError:
        raise TailnetSetupError("TAILSCALE_STATUS_NOT_JSON") from None
    self_node = data.get("Self") or {}
    return {
        "state": data.get("BackendState"),
        "ip": data.get("TailscaleIPs") or [],
        "online": self_node.get("Online") is True,
        "dns_name": self_node.get("DNSName") or "",
        "node_id_set": bool(self_node.get("ID")),
    }


def successful(state: dict) -> bool:
    return (state.get("state") == "Running"
            and bool(state.get("ip"))
            and state.get("online") is True
            and state.get("dns_name", "").endswith(TAILNET_SUFFIX)
            and state.get("node_id_set") is True)


def connect(expected_host: str) -> int:
    check_host(expected_host)
    current = status()
    if successful(current):
        print("TAILSCALE_VM100_CONNECTED=YES")
        print("TAILSCALE_VM100_IP=" + ",".join(current["ip"]))
        return 0
    if current["state"] == "Running":
        raise TailnetSetupError("VM100_CONNECTED_TO_UNEXPECTED_TAILNET_OR_INCOMPLETE")
    if current["state"] not in ("NeedsLogin", "Stopped", "NoState", "Starting"):
        raise TailnetSetupError("TAILSCALE_UNKNOWN_INITIAL_STATE")

    print("ASA LAB — TAILSCALE — ШКОЛЬНАЯ VM100")
    print("Получите одноразовый ключ на официальной странице:", KEYS_PAGE)
    print("Выберите существующий Tailnet; ключ One-off, не Ephemeral.")
    print("Если включено подтверждение устройств — отметьте Pre-approved.")
    secret = validate_key(getpass.getpass("Одноразовый Tailscale auth key (скрытый ввод): "))
    key_dir = Path("/run/tailscale")
    if key_dir.is_symlink() or not key_dir.is_dir():
        raise TailnetSetupError("TAILSCALE_RUNTIME_DIR_MISSING")
    file_path = None
    try:
        fd, file_path = tempfile.mkstemp(prefix=".asa-vm100-authkey-", dir=key_dir)
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(secret)
            handle.flush()
            os.fsync(handle.fileno())
        del secret
        proc = subprocess.run(
            up_command(file_path), capture_output=True, text=True,
            timeout=115, check=False,
        )
    except subprocess.TimeoutExpired:
        raise TailnetSetupError("TAILSCALE_ENROLLMENT_TIMED_OUT") from None
    finally:
        if file_path is not None:
            os.unlink(file_path)

    if proc.returncode:
        # Never return stdout/stderr; some Tailscale versions echo preferences.
        raise TailnetSetupError("TAILSCALE_AUTH_KEY_NOT_ACCEPTED")
    updated = status()
    if not successful(updated):
        raise TailnetSetupError("TAILSCALE_DEVICE_NOT_RUNNING_IN_EXPECTED_TAILNET")
    print("TAILSCALE_VM100_CONNECTED=YES")
    print("TAILSCALE_VM100_IP=" + ",".join(updated["ip"]))
    print("TAILSCALE_TAILNET=" + TAILNET_SUFFIX)
    print("ONE_OFF_AUTHKEY_REMOVED=YES")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["status", "connect"])
    parser.add_argument("--expected-host", default="")
    args = parser.parse_args(argv)
    try:
        check_host(args.expected_host)
        if args.action == "connect":
            return connect(args.expected_host)
        state = status()
        print(json.dumps({
            "host": HOST,
            "running": successful(state),
            "backend": state["state"],
            "tailnet_ip": state["ip"],
            "expected_tailnet": TAILNET_SUFFIX,
            "node_id_present": state["node_id_set"],
        }, ensure_ascii=False))
        return 0 if successful(state) else 2
    except TailnetSetupError as exc:
        print("TAILSCALE_VM100_SETUP_BLOCKED=" + exc.code, file=sys.stderr)
    except (OSError, ValueError, EOFError, KeyboardInterrupt):
        print("TAILSCALE_VM100_SETUP_CANCELLED_OR_FAILED_SAFE", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
