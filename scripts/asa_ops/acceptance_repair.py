"""Explicit owner-authorized repair of *synthetic* browser acceptance fixtures.

CLI intentionally runs only on the dedicated ASA Lab TEST host. It never edits
PostgreSQL, application users, environment files, or server deployment receipts.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import subprocess
import sys
import uuid

from .acceptance_fixture import (
    FixtureError, archive_fixture, audit_event, bound_path, fresh_fixture,
    locked_fixture, read_fixture, save_fixture,
)
from .maintenance import instance
from .store import process_identity

TEST_HOST = "admins-ms-7d22"
TEST_INSTALLATION = "asa-test"
TEST_ORIGIN = "https://test.asa-lab.ru"


def _deny(code):
    raise FixtureError(code)


def context(config, config_path):
    """Bind to the installed test checkout, service identity and private fixture."""
    cfg = config["installation"]
    if (cfg.get("id") != TEST_INSTALLATION or cfg.get("entry_origin") != TEST_ORIGIN
            or socket.gethostname().lower() != TEST_HOST):
        _deny("NOT_DEDICATED_TEST_HOST")
    root = Path(cfg["root"]).absolute()
    if (root.resolve() != root or not root.is_dir()
            or root.stat().st_uid != os.geteuid()
            or cfg.get("owner_uid") != os.geteuid()):
        _deny("INSTALLATION_OWNER_INVALID")
    if not str(Path(config_path).absolute()).startswith("/etc/server-settings/"):
        _deny("CONFIG_LOCATION_INVALID")
    generation = instance(str(root))
    if not generation:
        _deny("INSTALLATION_GENERATION_MISSING")
    command = cfg.get("acceptance_command")
    if not isinstance(command, list) or not all(isinstance(x, str) for x in command):
        _deny("ACCEPTANCE_COMMAND_INVALID")
    fixtures = [command[index+1] for index, arg in enumerate(command[:-1]) if arg == "--fixture"]
    if len(fixtures) != 1:
        _deny("ACCEPTANCE_FIXTURE_UNAVAILABLE")
    base = Path(fixtures[0])
    if not base.is_absolute():
        base = root / base
    path = bound_path(base, generation)
    # The official acceptance command keeps fixtures in the private Operations
    # state_root, not in the ASA Lab source checkout.
    state_root = Path(cfg["state_root"]).absolute()
    if state_root.resolve() != state_root or state_root not in path.parents:
        _deny("FIXTURE_OUTSIDE_OPS_STATE")
    if state_root.stat().st_uid != os.geteuid():
        _deny("OPS_STATE_OWNER_INVALID")
    return cfg, root, generation, path


def _descends_from_admin_command(command_pid):
    """Root-audited command PID must be an actual ancestor of this CLI process.

    An opaque operation ID, an environment variable or a matching UID is not
    proof of ownership. Fail closed if /proc lineage cannot be inspected.
    """
    if type(command_pid) is not int or command_pid < 2:
        return False
    current = os.getppid()
    seen = set()
    for _ in range(64):
        if current < 2 or current in seen:
            return False
        if current == command_pid:
            return True
        seen.add(current)
        try:
            fields = Path(f"/proc/{current}/stat").read_text().rsplit(")", 1)[1].split()
            current = int(fields[1])  # PPID follows process state in /proc stat.
        except (OSError, IndexError, ValueError):
            return False
    return False


def assert_no_workers(cfg, config):
    """Exempt only the verified current MCP admin command, never another worker.

    The Store's unique index keeps the admin worker's writer lease until
    completion. An unresolved recovery_required receipt blocks regardless of
    whether its worker still has a live PID or process identity.
    """
    path = Path(cfg["state_root"]) / "operations.sqlite3"
    if path.is_symlink() or not path.is_file():
        _deny("OPS_STATE_UNAVAILABLE")
    try:
        with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5) as db:
            rows = db.execute(
                "SELECT id,kind,status,pid,process_identity FROM operations "
                "WHERE installation=? AND status IN ('queued','running','recovery_required')",
                (cfg["id"],),
            ).fetchall()
    except (sqlite3.Error, OSError):
        _deny("OPS_STATE_UNAVAILABLE")

    # A lost worker does not prove a reconciled installation. Deny before
    # considering the self-admin exemption, including recovery rehearsals.
    if any(row[2] == "recovery_required" for row in rows):
        _deny("RECOVERY_RECONCILIATION_REQUIRED")
    if any(row[1] != "admin" or row[2] != "running" for row in rows):
        _deny("ACTIVE_OPS_WORKER")
    if not rows:
        _deny("MCP_ADMIN_OPERATION_REQUIRED")
    if len(rows) != 1:
        _deny("ACTIVE_OPS_WORKER")

    opid, _, _, pid, identity = rows[0]
    if (type(pid) is not int or pid < 2 or not isinstance(identity, str)
            or not identity or process_identity(pid) != identity):
        _deny("MCP_ADMIN_PROOF_INVALID")

    # The root-owned audit is accessed through the executor's private
    # SO_PEERCRED-protected Unix socket. The returned root command PID must
    # be in *this* process's real ancestry, excluding unrelated admin shells.
    from .admin import request as root_admin_request
    try:
        audit = root_admin_request(config, {"action": "status", "operation_id": opid})
    except Exception:
        _deny("MCP_ADMIN_PROOF_UNAVAILABLE")
    if (not isinstance(audit, dict) or audit.get("operation_id") != opid
            or audit.get("status") != "running"
            or not _descends_from_admin_command(audit.get("command_pid"))):
        _deny("MCP_ADMIN_PROOF_INVALID")


def postgres_matches(cfg, email, username):
    """Probe actual DB via existing Compose postgres; SQL is read-only.

    Synthetic identifiers are validated before interpolation and transmitted only
    over stdin, never in command-line arguments or public logs.
    """
    if not re.fullmatch(r"asa-ops-[0-9a-f]{16}@example\.invalid", email):
        _deny("SYNTHETIC_IDENTITY_REQUIRED")
    if username != "asaops_" + email[8:24]:
        _deny("SYNTHETIC_USERNAME_REQUIRED")
    from .adapter import load
    _, installation = load(cfg)
    sql = (
        "BEGIN TRANSACTION READ ONLY;\n"
        "SELECT (CASE WHEN current_setting('is_superuser')='on' THEN 1 ELSE 0 END),"
        "(SELECT count(*) FROM public.accounts WHERE lower(email)=lower('" + email + "')),"
        "(SELECT count(*) FROM public.users WHERE lower(email)=lower('" + email + "')),"
        "(SELECT count(*) FROM public.profiles WHERE lower(username)=lower('" + username + "'));\n"
        "COMMIT;\n"
    )
    command = list(installation.compose_args) + [
        "exec", "-T", "postgres", "sh", "-c",
        'exec psql -X -q -A -t -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"',
    ]
    try:
        result = subprocess.run(
            command, cwd=cfg["root"], input=sql, capture_output=True,
            text=True, timeout=45, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        _deny("POSTGRES_PROBE_UNAVAILABLE")
    if result.returncode != 0:
        _deny("POSTGRES_PROBE_UNAVAILABLE")
    lines = [s.strip() for s in result.stdout.splitlines() if s.strip()]
    if len(lines) != 1 or not re.fullmatch(r"[0-9]+\|[0-9]+\|[0-9]+\|[0-9]+", lines[0]):
        _deny("POSTGRES_PROBE_UNVERIFIED")
    authorized, *counts = (int(item) for item in lines[0].split("|"))
    if authorized != 1:
        _deny("POSTGRES_AUTHORITY_UNVERIFIED")
    return sum(counts)


def inspect(config, config_path):
    cfg, _, generation, path = context(config, config_path)
    with locked_fixture(path):
        assert_no_workers(cfg, config)
        value, sha, legacy = read_fixture(path, cfg["entry_origin"], generation, cfg["id"])
        if value is None:
            return {"status": "blocked", "code": "FIXTURE_MISSING"}
        if "recovery" in value:
            return {"status": "pending_registration" if not value.get("registered")
                    else "registered_after_repair",
                    "source_sha256": value["recovery"]["source_sha256"]}
        if value.get("registered") is not True:
            return {"status": "blocked", "code": "NOT_A_STALE_REGISTERED_FIXTURE"}
        count = postgres_matches(cfg, value["email"], value["username"])
        if count:
            return {"status": "blocked", "code": "ACCOUNT_PRESENT_INVESTIGATE_401"}
        return {"status": "eligible", "fixture_sha256": sha,
                "legacy": legacy, "database_matches": 0}


def apply(config, config_path, expected_sha):
    if not isinstance(expected_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
        _deny("EXPECTED_DIGEST_REQUIRED")
    cfg, _, generation, path = context(config, config_path)
    with locked_fixture(path):
        assert_no_workers(cfg, config)
        value, sha, _ = read_fixture(path, cfg["entry_origin"], generation, cfg["id"])
        if value is None:
            _deny("FIXTURE_MISSING")
        if "recovery" in value and value["recovery"]["source_sha256"] == expected_sha:
            return {"status": "already_repaired" if not value.get("registered")
                    else "already_registered", "operation_id": value["recovery"]["operation_id"]}
        if sha != expected_sha or "recovery" in value or value.get("registered") is not True:
            _deny("FIXTURE_CHANGED")
        if postgres_matches(cfg, value["email"], value["username"]) != 0:
            _deny("ACCOUNT_PRESENT_INVESTIGATE_401")
        candidate = fresh_fixture(cfg["entry_origin"], generation, cfg["id"])
        if postgres_matches(cfg, candidate["email"], candidate["username"]) != 0:
            _deny("SYNTHETIC_COLLISION")
        operation_id = uuid.uuid4().hex
        candidate["recovery"] = {"source_sha256": sha, "operation_id": operation_id}
        # Verify worker/identity a second time directly before file mutation.
        assert_no_workers(cfg, config)
        if postgres_matches(cfg, value["email"], value["username"]) != 0:
            _deny("ACCOUNT_PRESENT_INVESTIGATE_401")
        archive_fixture(path, sha)
        audit_event(path, "archived", operation_id, sha)
        save_fixture(path, candidate, cfg["entry_origin"], generation, cfg["id"], expected_sha=sha)
        audit_event(path, "pending_registration", operation_id, sha)
        return {"status": "pending_registration", "operation_id": operation_id}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Repair ASA Lab TEST synthetic browser fixture")
    parser.add_argument("--config", required=True)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--plan", action="store_true")
    action.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-fixture-sha256")
    parser.add_argument("--authorize-installation")
    args = parser.parse_args(argv)
    try:
        config = json.loads(Path(args.config).read_text(encoding="utf-8"))
        if args.apply:
            if args.authorize_installation != TEST_INSTALLATION:
                _deny("EXPLICIT_OWNER_AUTHORIZATION_REQUIRED")
            result = apply(config, args.config, args.expected_fixture_sha256)
        else:
            result = inspect(config, args.config)
        print(json.dumps(result, sort_keys=True))
        return 0 if result["status"] not in ("blocked",) else 2
    except (FixtureError, KeyError, ValueError, OSError, TypeError) as exc:
        code = str(exc) if isinstance(exc, FixtureError) else "REPAIR_CONFIGURATION_INVALID"
        print(json.dumps({"status": "blocked", "code": code}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
