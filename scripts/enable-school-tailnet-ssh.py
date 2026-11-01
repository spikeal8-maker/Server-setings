#!/usr/bin/env python3
"""Permit the exact D2-R2 tailnet peer to SSH into school VM100.

Only SSH 22/tcp from 100.96.255.113 on tailscale0 is allowed; never opens
1Panel, PVE, Docker, MCP or SSH to the whole tailnet/public internet.
No Tailscale auth/network flags or existing UFW defaults are modified.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import shutil
import socket
import stat
import subprocess
import sys

HOST="asa-school-01"
VM_TAIL_IP="100.124.247.24"
D2_IP="100.96.255.113"
DEVICE="tailscale0"
TCP_PORT="22"
BACKUP=Path("/var/backups/server-settings/ufw-user-before-vm100-tailnet-ssh-20261101.rules")
UFW=("/usr/sbin/ufw",)
RULE=("allow","in","on",DEVICE,"from",D2_IP,"to","any","port",TCP_PORT,"proto","tcp")


class SafeStop(Exception):
    pass


def command(args: tuple | list, *, timeout: int=15):
    try:
        result=subprocess.run(list(args),capture_output=True,text=True,
                              timeout=timeout,check=False)
    except subprocess.TimeoutExpired:
        raise SafeStop("FIREWALL_COMMAND_TIMEOUT") from None
    if result.returncode:
        if Path(str(args[0])).name=="ufw":
            # This command has no secrets; preserve its bounded diagnostic.
            detail=" ".join((result.stderr or result.stdout).split())[:200]
            raise SafeStop("UFW_ERROR_"+(detail or str(result.returncode)))
        raise SafeStop("COMMAND_FAILED_"+Path(str(args[0])).name)
    return result.stdout


def d2_peer_verified(peer):
    return (
        D2_IP in (peer.get("TailscaleIPs") or [])
        and str(peer.get("HostName") or "").casefold()=="d2-r2"
        and peer.get("DNSName")=="d2-r2.tail605710.ts.net."
        and peer.get("Online") is True
    )


def preflight():
    if os.geteuid()!=0 or socket.gethostname()!=HOST:
        raise SafeStop("WRONG_SCHOOL_HOST")
    state=json.loads(command(["/usr/bin/tailscale","status","--json"]))
    self_data=state.get("Self") or {}
    if (state.get("BackendState")!="Running" or
            VM_TAIL_IP not in (state.get("TailscaleIPs") or []) or
            self_data.get("DNSName")!="asa-school-01.tail605710.ts.net."):
        raise SafeStop("TAILNET_MACHINE_NOT_VERIFIED")
    peers=state.get("Peer") or {}
    expected=[x for x in peers.values() if d2_peer_verified(x)]
    if len(expected)!=1:
        raise SafeStop("D2_TAILNET_IDENTITY_NOT_VERIFIED")

    sshd=command(["/usr/sbin/sshd","-T"])
    if not re.search(r"(?m)^passwordauthentication no$",sshd) or not re.search(r"(?m)^pubkeyauthentication yes$",sshd):
        raise SafeStop("SSH_KEY_ONLY_REQUIRED")
    ufw=command([*UFW,"status","verbose"])
    if "Status: active" not in ufw or "Default: deny (incoming)" not in ufw:
        raise SafeStop("UFW_POLICY_NOT_DENY")
    if not Path("/etc/ufw/user.rules").is_file():
        raise SafeStop("UFW_CONFIG_MISSING")


def rule_installed():
    # UFW controls this chain and generates rule from the exact documented
    # command. Do not edit its iptables/nftables tables directly.
    result=command(["/usr/sbin/iptables","-S","ufw-user-input"])
    entries=[line for line in result.splitlines() if line.startswith("-A ufw-user-input ")]
    return any("-i tailscale0" in s and "-p tcp" in s and "--dport 22" in s
               and "-s 100.96.255.113/32" in s and "-j ACCEPT" in s
               for s in entries)


def backup_ufw_rules():
    folder=BACKUP.parent
    if folder.is_symlink():
        raise SafeStop("BACKUP_FOLDER_SYMLINK")
    folder.mkdir(parents=True,exist_ok=True,mode=0o700)
    if folder.stat().st_uid!=0 or stat.S_IMODE(folder.stat().st_mode)!=0o700:
        raise SafeStop("BACKUP_FOLDER_UNTRUSTED")
    source=Path("/etc/ufw/user.rules")
    st=source.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_uid!=0:
        raise SafeStop("UFW_RULES_FILE_UNTRUSTED")
    with source.open("rb") as stream:
        body=stream.read(250000)
    if BACKUP.exists() or BACKUP.is_symlink():
        previous=BACKUP.lstat()
        if (not stat.S_ISREG(previous.st_mode) or previous.st_uid!=0
                or stat.S_IMODE(previous.st_mode)!=0o600
                or BACKUP.read_bytes()!=body):
            raise SafeStop("BACKUP_EXISTS_BUT_UFW_CONTENT_CHANGED")
        return
    fd=os.open(BACKUP,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,"wb") as target:
        target.write(body)
        target.flush()
        os.fsync(target.fileno())


def apply():
    preflight()
    if rule_installed():
        print("VM100_TAILNET_D2_SSH_RULE_ALREADY_PRESENT=YES")
        return
    command([*UFW,"--dry-run",*RULE],timeout=12)
    backup_ufw_rules()
    command([*UFW,"--force",*RULE],timeout=25)
    if not rule_installed():
        # Explicit deletion via UFW, not iptables.
        subprocess.run([*UFW,"--force","delete",*RULE],
                       capture_output=True,timeout=20,check=False)
        raise SafeStop("TAILNET_SSH_RULE_NOT_VERIFIED_ROLLBACK_ATTEMPTED")
    print("VM100_UFW_ONLY_D2_R2_SSH22_TAILSCALE0=PASS")
    print("VM100_PUBLIC_ADMIN_PORTS_UNCHANGED=YES")
    print("D2_R2_SSH_TCP_CONNECTIVITY_CHECK_FROM_D2=REQUIRED")
    print("UFW_PREVIOUS_CONFIG_ROOT0600_BACKUP=YES")


if __name__=="__main__":
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply",action="store_true")
    opts=parser.parse_args()
    try:
        preflight()
        if opts.apply:
            apply()
        else:
            print("PLAN: allow ONLY 100.96.255.113 → tailscale0 TCP22 on VM100; UFW default DENY unchanged.")
    except SafeStop as exc:
        print("SCHOOL_TAILNET_SSH_BLOCKED="+str(exc),file=sys.stderr)
        sys.exit(2)
