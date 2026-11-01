#!/usr/bin/env python3
"""Promote only the private official VM100 1Panel MCP to full tool access.

No network port is published. Only the existing localhost MCP service is
restarted. Root-only env is backed up and atomically restored on any error.
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import tempfile
import time

HOST = "asa-school-01"
MCP_ENV = Path("/etc/server-settings/mcp.env")
BACKUP = Path("/var/backups/server-settings/mcp-before-full-20261101.env")
SERVICE = "server-settings-1panel-mcp.service"
API_HOST = "127.0.0.1"
API_PORT = 39443
EXPECTED_READONLY = {
    "get_dashboard_info", "get_system_info", "list_databases",
    "list_installed_apps", "list_ssls", "list_websites",
}
EXPECTED_FULL = EXPECTED_READONLY | {
    "create_database", "create_ssl", "create_website",
    "install_mysql", "install_openresty",
}


class SafetyError(Exception):
    pass


def check_identity() -> None:
    if os.geteuid() != 0 or socket.gethostname() != HOST:
        raise SafetyError("ROOT_ON_SCHOOL_VM100_REQUIRED")
    for path in (MCP_ENV,):
        if path.is_symlink() or not path.is_file():
            raise SafetyError("UNTRUSTED_ENV_PATH")
        info = path.stat()
        if info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600:
            raise SafetyError("UNTRUSTED_ENV_MODE")


def parse_environment(raw: bytes) -> tuple[bytes, str, str]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise SafetyError("ENV_NOT_UTF8") from None
    lines = text.splitlines()
    keys = {}
    for line in lines:
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or key in keys:
            raise SafetyError("ENV_FIELDS_INVALID")
        keys[key] = value
    if keys.get("PANEL_HOST") != "http://127.0.0.1:19192":
        raise SafetyError("PANEL_HOST_CHANGED")
    if not keys.get("PANEL_ACCESS_TOKEN") or len(keys["PANEL_ACCESS_TOKEN"]) != 32:
        raise SafetyError("PANEL_TOKEN_INVALID")
    if not keys.get("MCP_AUTH_TOKEN") or len(keys["MCP_AUTH_TOKEN"]) < 48:
        raise SafetyError("MCP_BEARER_INVALID")
    if keys.get("MCP_ACCESS_LEVEL") not in ("readonly", "full"):
        raise SafetyError("MCP_UNKNOWN_ACCESS_LEVEL")
    if len(keys) != 4:
        raise SafetyError("UNEXPECTED_ADDITIONAL_ENV_FIELDS")
    return raw, keys["MCP_ACCESS_LEVEL"], keys["MCP_AUTH_TOKEN"]


def mcp_call(method: str, request_id: int, token: str, session: str | None = None):
    conn = http.client.HTTPConnection(API_HOST, API_PORT, timeout=8)
    body = {"jsonrpc": "2.0", "method": method, "id": request_id,
            "params": ({"protocolVersion": "2025-03-26", "capabilities": {},
                        "clientInfo": {"name": "vm100-mcp-full-verify", "version": "1"}}
                       if method == "initialize" else {})}
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": "Bearer " + token,
        "MCP-Protocol-Version": "2025-03-26",
    }
    if session:
        headers["Mcp-Session-Id"] = session
    try:
        conn.request("POST", "/mcp", json.dumps(body), headers)
        response = conn.getresponse()
        status = response.status
        content_type = response.getheader("Content-Type", "")
        session = response.getheader("Mcp-Session-Id") or session
        payload = response.read(100000).decode("utf-8", "replace")
    finally:
        conn.close()
    if status != 200:
        raise SafetyError("MCP_AUTHENTICATED_RPC_FAILED")
    if "text/event-stream" in content_type:
        results = []
        for line in payload.splitlines():
            if line.startswith("data: "):
                try:
                    results.append(json.loads(line[6:]))
                except ValueError:
                    pass
    else:
        try:
            results = [json.loads(payload)]
        except ValueError:
            results = []
    matched = [value for value in results if value.get("id") == request_id]
    if len(matched) != 1 or "result" not in matched[0]:
        raise SafetyError("MCP_JSONRPC_INVALID")
    return session, matched[0]["result"]


def tools_list(token: str) -> set[str]:
    session, _ = mcp_call("initialize", 1, token)
    session, result = mcp_call("tools/list", 2, token, session)
    tools = result.get("tools")
    if not isinstance(tools, list):
        raise SafetyError("MCP_TOOLS_LIST_MISSING")
    names = [tool.get("name") for tool in tools]
    if not all(isinstance(n, str) for n in names) or len(set(names)) != len(names):
        raise SafetyError("MCP_TOOLS_LIST_INVALID")
    return set(names)


def check_private_listener() -> None:
    p = subprocess.run(["ss", "-ltn"], capture_output=True, text=True,
                       timeout=8, check=True)
    listeners = [line for line in p.stdout.splitlines() if ":39443" in line]
    if not listeners or any("127.0.0.1:39443" not in line for line in listeners):
        raise SafetyError("MCP_LISTENER_NOT_PRIVATE")
    conn = http.client.HTTPConnection(API_HOST, API_PORT, timeout=6)
    try:
        conn.request("GET", "/mcp")
        res = conn.getresponse()
        res.read()
        if res.status != 401:
            raise SafetyError("UNAUTHENTICATED_MCP_NOT_DENIED")
    finally:
        conn.close()


def systemd(command: str) -> None:
    p = subprocess.run(["systemctl", command, SERVICE], capture_output=True,
                       text=True, timeout=24, check=False)
    if p.returncode:
        raise SafetyError("MCP_SERVICE_" + command.upper() + "_FAILED")


def atomic_replace(contents: bytes) -> None:
    fd, temp = tempfile.mkstemp(prefix=".mcp-mode-", dir=MCP_ENV.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, MCP_ENV)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def apply() -> None:
    check_identity()
    before, mode, token = parse_environment(MCP_ENV.read_bytes())
    systemd("is-active")
    check_private_listener()
    observed = tools_list(token)
    if mode == "full":
        if observed != EXPECTED_FULL:
            raise SafetyError("FULL_MCP_TOOLS_MISMATCH")
        print("MCP_LOCAL_FULL_ALREADY_ACTIVE=YES")
        return
    if observed != EXPECTED_READONLY:
        raise SafetyError("READONLY_TOOLS_NOT_VERIFIED")
    text = before.decode("utf-8")
    if text.count("MCP_ACCESS_LEVEL=readonly") != 1:
        raise SafetyError("ENV_MODE_ANCHOR_MISMATCH")
    upgraded = text.replace("MCP_ACCESS_LEVEL=readonly",
                            "MCP_ACCESS_LEVEL=full", 1).encode()
    directory = BACKUP.parent
    if directory.is_symlink():
        raise SafetyError("BACKUP_DIRECTORY_UNTRUSTED")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.stat().st_uid != 0 or stat.S_IMODE(directory.stat().st_mode) != 0o700:
        raise SafetyError("BACKUP_DIRECTORY_PERMISSIONS")
    with os.fdopen(os.open(BACKUP, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                          os.O_CLOEXEC, 0o600), "wb") as stream:
        stream.write(before)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        atomic_replace(upgraded)
        systemd("restart")
        for attempt in range(12):
            try:
                systemd("is-active")
                check_private_listener()
                got = tools_list(token)
                if got == EXPECTED_FULL:
                    print("MCP_LOCAL_FULL_TOOLS=11")
                    print("MCP_LOCAL_UNAUTHENTICATED=401")
                    print("MCP_LOCAL_ONLY_127_0_0_1=YES")
                    print("MCP_EXPOSURE_TO_CHATGPT_NOT_YET_CONFIGURED=YES")
                    print("MCP_ENV_BACKUP_ROOT_ONLY=YES")
                    return
            except (SafetyError, OSError, TimeoutError):
                pass
            time.sleep(1)
        raise SafetyError("MCP_FULL_VERIFICATION_FAILED")
    except BaseException:
        atomic_replace(before)
        systemd("restart")
        raise


def main() -> int:
    arg = argparse.ArgumentParser(description=__doc__)
    arg.add_argument("--apply", action="store_true")
    opts = arg.parse_args()
    try:
        check_identity()
        _, mode, token = parse_environment(MCP_ENV.read_bytes())
        if not opts.apply:
            print("VM100_LOCAL_MCP_MODE=" + mode)
            print("PLAN: promote existing loopback MCP to official full tool level.")
            print("No public gateway, ASA database or Docker application changes.")
            return 0
        apply()
        return 0
    except (SafetyError, OSError, subprocess.SubprocessError) as exc:
        error = exc.__class__.__name__ if not isinstance(exc, SafetyError) else str(exc)
        print("VM100_MCP_FULL_BLOCKED=" + error, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
