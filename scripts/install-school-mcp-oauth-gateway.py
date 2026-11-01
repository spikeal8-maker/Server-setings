#!/usr/bin/env python3
"""Install the pinned school MCP OAuth gateway on VM100 localhost only.

The OAuth gateway is not the server's root operator. It publishes the existing
official full 1Panel MCP (11 tools) only after a separate 1Panel route switch.
All credentials remain on school VM100. No ASA app/DB/container is modified.
"""
from __future__ import annotations
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import subprocess
import sys
import tempfile
import time

HOST = "asa-school-01"
DOMAIN = "x.asa-lab.ru"
BINARY = Path("/opt/server-settings/bin/asa-mcp-auth-proxy")
EXPECTED_SHA = "114b57ca5cddf7206a6bd935bf1d8c342f10aab2ff6a0d76dad0840012178d15"
SOURCE_ENV = Path("/etc/server-settings/mcp.env")
GATEWAY_ENV = Path("/etc/server-settings/asa-school-mcp-gateway.env")
OWNER_PASSWORD = Path("/var/lib/server-settings/asa-school-mcp-owner-password")
UNIT = Path("/etc/systemd/system/asa-school-mcp-oauth-gateway.service")
PORT = 39450


class GateError(Exception):
    pass


def require_state():
    import hashlib
    if os.geteuid() != 0 or socket.gethostname() != HOST:
        raise GateError("WRONG_HOST")
    if not BINARY.is_file() or BINARY.is_symlink():
        raise GateError("PINNED_BINARY_MISSING")
    h = hashlib.sha256()
    with BINARY.open("rb") as f:
        while b := f.read(1024 * 1024):
            h.update(b)
    if h.hexdigest() != EXPECTED_SHA:
        raise GateError("GATEWAY_BINARY_HASH_UNTRUSTED")
    if SOURCE_ENV.is_symlink() or not SOURCE_ENV.is_file():
        raise GateError("MCP_ENV_MISSING")
    if SOURCE_ENV.stat().st_uid != 0 or stat.S_IMODE(SOURCE_ENV.stat().st_mode) != 0o600:
        raise GateError("MCP_ENV_NOT_ROOT_ONLY")
    data = {}
    for line in SOURCE_ENV.read_text().splitlines():
        k,sep,value=line.partition("=")
        if sep:
            if k in data:
                raise GateError("DUPLICATE_ENV_KEY")
            data[k]=value
    if data.get("PANEL_HOST") != "http://127.0.0.1:19192" or data.get("MCP_ACCESS_LEVEL") != "full":
        raise GateError("OFFICIAL_MCP_NOT_FULL")
    bearer=data.get("MCP_AUTH_TOKEN","")
    if not re.fullmatch(r"[a-fA-F0-9]{64}",bearer):
        raise GateError("OFFICIAL_MCP_BEARER_INVALID")
    return bearer


def check_local_gateway():
    import urllib.request, urllib.error
    url="http://127.0.0.1:39450"
    resources=(
        ("/.well-known/oauth-protected-resource/mcp","resource"),
        ("/.well-known/oauth-authorization-server","issuer"),
    )
    for path,key in resources:
        with urllib.request.urlopen(url+path,timeout=7) as res:
            if res.status != 200:
                raise GateError("OAUTH_METADATA_HTTP")
            value=json.loads(res.read(16000))
            expected=f"https://{DOMAIN}/mcp" if key=="resource" else f"https://{DOMAIN}"
            actual=str(value.get(key,"")).rstrip("/")
            if actual != expected.rstrip("/"):
                raise GateError("OAUTH_METADATA_RESOURCE_MISMATCH")
    try:
        with urllib.request.urlopen(url+"/mcp",timeout=7):
            raise GateError("MCP_WITHOUT_AUTH_SHOULD_NOT_PASS")
    except urllib.error.HTTPError as e:
        if e.code != 401:
            raise GateError("MCP_WITHOUT_AUTH_NOT_401")
        header=e.headers.get("WWW-Authenticate","")
        if "oauth-protected-resource/mcp" not in header:
            raise GateError("OAUTH_401_CHALLENGE_INVALID")
    p=subprocess.run(["ss","-ltn"],capture_output=True,text=True,check=True,timeout=7)
    listeners=[line for line in p.stdout.splitlines() if ":39450" in line]
    if not listeners or any("127.0.0.1:39450" not in line for line in listeners):
        raise GateError("GATEWAY_WAN_LISTENER_FORBIDDEN")


def install():
    bearer=require_state()
    if any(p.exists() or p.is_symlink() for p in (GATEWAY_ENV,OWNER_PASSWORD,UNIT)):
        raise GateError("EXISTING_GATEWAY_CONFIGURATION_NO_OVERWRITE")
    envdir=GATEWAY_ENV.parent
    if envdir.is_symlink() or envdir.stat().st_uid!=0:
        raise GateError("CONFIG_DIR_UNTRUSTED")
    secretsdir=OWNER_PASSWORD.parent
    if secretsdir.is_symlink() or secretsdir.stat().st_uid!=0:
        raise GateError("SECRETS_DIR_UNTRUSTED")
    q=subprocess.run(["ss","-ltn"],capture_output=True,text=True,check=True,timeout=5)
    if ":39450" in q.stdout:
        raise GateError("LOOPBACK_PORT_IN_USE")

    # Only the VM100 owner can read this generated password in a local terminal.
    # No password or MCP bearer goes into GitHub, issue comments or process args.
    password=secrets.token_urlsafe(40)
    env=(
        "LISTEN=127.0.0.1:39450\n"
        "NO_AUTO_TLS=true\n"
        f"EXTERNAL_URL=https://{DOMAIN}\n"
        "DATA_PATH=/var/lib/asa-school-mcp-gateway\n"
        f"PASSWORD={password}\n"
        f"PROXY_BEARER_TOKEN={bearer}\n"
        "TRUSTED_PROXIES=127.0.0.1/32\n"
        "NO_PROVIDER_AUTO_SELECT=true\n"
    )
    unit = """[Unit]
Description=Independent ASA Lab School MCP OAuth Gateway (loopback only)
Wants=network-online.target
After=network-online.target server-settings-1panel-mcp.service
Requires=server-settings-1panel-mcp.service

[Service]
Type=simple
DynamicUser=yes
StateDirectory=asa-school-mcp-gateway
StateDirectoryMode=0700
WorkingDirectory=/var/lib/asa-school-mcp-gateway
EnvironmentFile=/etc/server-settings/asa-school-mcp-gateway.env
ExecStart=/opt/server-settings/bin/asa-mcp-auth-proxy --listen 127.0.0.1:39450 --no-auto-tls --external-url https://x.asa-lab.ru -- http://127.0.0.1:39443/mcp
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectSystem=strict
ProtectHome=yes
ProtectKernelTunables=yes
ProtectControlGroups=yes
CapabilityBoundingSet=
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
MemoryMax=512M
TasksMax=256

[Install]
WantedBy=multi-user.target
"""
    def create_secure(path,contents,mode):
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,mode)
        with os.fdopen(fd,"w") as stream:
            os.fchmod(stream.fileno(),mode)
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
    created=[]
    try:
        create_secure(GATEWAY_ENV,env,0o600);created.append(GATEWAY_ENV)
        create_secure(OWNER_PASSWORD,password+"\n",0o600);created.append(OWNER_PASSWORD)
        create_secure(UNIT,unit,0o644);created.append(UNIT)
        subprocess.run(["systemctl","daemon-reload"],check=True,capture_output=True,timeout=12)
        subprocess.run(["systemctl","enable","--now",UNIT.name],check=True,capture_output=True,timeout=18)
        for i in range(12):
            time.sleep(1)
            try:
                check_local_gateway()
                print("SCHOOL_MCP_OAUTH_LOCAL_GATEWAY=PASS")
                print("UNAUTHENTICATED_MCP=401")
                print("OAUTH_RESOURCE=https://x.asa-lab.ru/mcp")
                print("LISTEN=127.0.0.1:39450")
                print("OWNER_PASSWORD_ROOT_ONLY_FILE=/var/lib/server-settings/asa-school-mcp-owner-password")
                print("PUBLIC_1PANEL_MCP_ROUTE_NOT_MODIFIED=YES")
                return
            except Exception:
                continue
        raise GateError("LOCAL_GATEWAY_HEALTH_CHECK_FAILED")
    except BaseException:
        subprocess.run(["systemctl","disable","--now",UNIT.name],capture_output=True,timeout=20,check=False)
        for path in created:
            try: path.unlink()
            except OSError: pass
        subprocess.run(["systemctl","daemon-reload"],capture_output=True,timeout=10,check=False)
        raise


def main():
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply",action="store_true")
    args=parser.parse_args()
    try:
        require_state()
        if not args.apply:
            print("PLAN: install pinned OAuth gateway on 127.0.0.1:39450 with independent root-only owner password; no public route.")
            return 0
        install()
        return 0
    except (GateError, OSError, subprocess.SubprocessError) as e:
        reason=str(e) if isinstance(e,GateError) else type(e).__name__
        print("SCHOOL_MCP_OAUTH_GATEWAY_BLOCKED="+reason,file=sys.stderr)
        return 2

if __name__=="__main__":
    raise SystemExit(main())
