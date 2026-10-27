"""Private, instance-bound ASA Operations synthetic acceptance fixture primitives.

Do not import this module from the application runtime. Never log fixture contents.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat

INSTANCE = re.compile(r"[0-9a-f]{32}\Z")
EMAIL = re.compile(r"asa-ops-([0-9a-f]{16})@example\.invalid\Z")
UUID = re.compile(r"[0-9a-fA-F-]{36}\Z")
MAX_BYTES = 32768
OPEN_SAFE = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)


class FixtureError(RuntimeError):
    """Public messages are constant codes; raw fixture data is never included."""


def _fail(code):
    raise FixtureError(code)


def bound_path(base, generation):
    if not isinstance(generation, str) or not INSTANCE.fullmatch(generation):
        _fail("INSTANCE_INVALID")
    path = Path(base).absolute()
    if path.suffix != ".json" or not path.name or path.name in (".", ".."):
        _fail("FIXTURE_PATH_INVALID")
    if path.parent.resolve() != path.parent:
        _fail("FIXTURE_PARENT_SYMLINK")
    return path.with_name(path.stem + "-" + generation + path.suffix)


def _private_dir(path, create=False):
    path = Path(path)
    if create:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        mode = path.lstat()
    except OSError:
        _fail("PRIVATE_DIRECTORY_MISSING")
    if not stat.S_ISDIR(mode.st_mode) or mode.st_uid != os.geteuid() or mode.st_mode & 0o077:
        _fail("PRIVATE_DIRECTORY_PERMISSIONS")
    if path.resolve() != path.absolute():
        _fail("PRIVATE_DIRECTORY_SYMLINK")


def _regular(path, allow_missing=False):
    path = Path(path)
    try:
        descriptor = os.open(path, os.O_RDONLY | OPEN_SAFE)
    except FileNotFoundError:
        if allow_missing:
            return None
        _fail("FIXTURE_MISSING")
    except OSError:
        _fail("FIXTURE_OPEN_FAILED")
    try:
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
                or metadata.st_mode & 0o077 or metadata.st_nlink != 1):
            _fail("FIXTURE_FILE_UNSAFE")
        raw = os.read(descriptor, MAX_BYTES + 1)
        after = os.fstat(descriptor)
        if len(raw) > MAX_BYTES or metadata.st_size != len(raw) or (
            metadata.st_dev, metadata.st_ino, metadata.st_mtime_ns
        ) != (after.st_dev, after.st_ino, after.st_mtime_ns):
            _fail("FIXTURE_CONTENT_UNSAFE")
        return raw
    finally:
        os.close(descriptor)


def _sync_dir(path):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | OPEN_SAFE)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _exclusive(path, raw):
    path = Path(path)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | OPEN_SAFE, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


@contextmanager
def locked_fixture(path):
    """The browser and operator repair must hold the same nonblocking lock."""
    path = Path(path)
    _private_dir(path.parent, create=True)
    lock = path.with_name("." + path.name + ".lock")
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | OPEN_SAFE, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
                or metadata.st_mode & 0o077 or metadata.st_nlink != 1):
            _fail("FIXTURE_LOCK_UNSAFE")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _fail("FIXTURE_BUSY")
        yield
    finally:
        os.close(descriptor)


def validate(value, origin, generation, installation):
    if not isinstance(value, dict) or set(value) - {
        "origin", "generation", "installation_id", "email", "username",
        "password", "registered", "projects", "authorized_test_fixture",
        "classroom", "join_code", "student_code", "course",
        "electronics_seeded", "recovery"
    }:
        _fail("FIXTURE_SCHEMA_INVALID")
    email = value.get("email")
    match = EMAIL.fullmatch(email) if isinstance(email, str) else None
    if (value.get("authorized_test_fixture") is not True or match is None
            or value.get("username") != "asaops_" + match.group(1)
            or not isinstance(value.get("password"), str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", value["password"])
            or value.get("origin") != origin
            or type(value.get("registered", False)) is not bool
            or not isinstance(value.get("projects"), dict)
            or set(value["projects"]) - {"blocks", "electronics"}):
        _fail("FIXTURE_IDENTITY_INVALID")
    if ("installation_id" in value) != ("generation" in value):
        _fail("FIXTURE_BINDING_INCOMPLETE")
    legacy = "generation" not in value
    if not legacy and (value["generation"] != generation
                       or value["installation_id"] != installation):
        _fail("FIXTURE_FOREIGN_INSTALLATION")
    for item in ("classroom", "course"):
        if item in value and (not isinstance(value[item], str) or not UUID.fullmatch(value[item])):
            _fail("FIXTURE_REFERENCE_INVALID")
    for item in value["projects"].values():
        if not isinstance(item, str) or not UUID.fullmatch(item):
            _fail("FIXTURE_REFERENCE_INVALID")
    for item in ("student_code", "join_code"):
        if item in value and (not isinstance(value[item], str)
                              or not re.fullmatch(r"[A-Za-z0-9_-]{4,48}", value[item])):
            _fail("FIXTURE_REFERENCE_INVALID")
    if "electronics_seeded" in value and not isinstance(value["electronics_seeded"], bool):
        _fail("FIXTURE_SCHEMA_INVALID")
    if "recovery" in value:
        rec = value["recovery"]
        if (not isinstance(rec, dict)
                or set(rec) != {"source_sha256", "operation_id"}
                or not isinstance(rec.get("source_sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", rec["source_sha256"])
                or not isinstance(rec.get("operation_id"), str)
                or not INSTANCE.fullmatch(rec["operation_id"])):
            _fail("FIXTURE_RECOVERY_INVALID")
    return legacy


def read_fixture(path, origin, generation, installation):
    _private_dir(Path(path).parent)
    raw = _regular(path, allow_missing=True)
    if raw is None:
        return None, None, False
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        _fail("FIXTURE_JSON_INVALID")
    legacy = validate(value, origin, generation, installation)
    return value, hashlib.sha256(raw).hexdigest(), legacy


def fresh_fixture(origin, generation, installation):
    ident = secrets.token_hex(8)
    return {
        "origin": origin, "generation": generation,
        "installation_id": installation,
        "email": "asa-ops-" + ident + "@example.invalid",
        "username": "asaops_" + ident,
        "password": secrets.token_urlsafe(48),
        "registered": False, "projects": {},
        "authorized_test_fixture": True,
    }


def save_fixture(path, value, origin, generation, installation, expected_sha=None):
    validate(value, origin, generation, installation)
    if "generation" not in value:
        _fail("FIXTURE_LEGACY_WRITE")
    current = _regular(path, allow_missing=True)
    sha = hashlib.sha256(current).hexdigest() if current is not None else None
    if expected_sha is not None and sha != expected_sha:
        _fail("FIXTURE_CHANGED")
    raw = (json.dumps(value, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
    if len(raw) > MAX_BYTES:
        _fail("FIXTURE_TOO_LARGE")
    target = Path(path)
    temporary = target.with_name("." + target.name + "." + secrets.token_hex(8) + ".tmp")
    try:
        _exclusive(temporary, raw)
        os.replace(temporary, target)
        _sync_dir(target.parent)
    finally:
        temporary.unlink(missing_ok=True)
    return hashlib.sha256(raw).hexdigest()


def archive_fixture(path, expected_sha):
    """Never overwrite an audit copy, even on an interrupted retry."""
    raw = _regular(path)
    if hashlib.sha256(raw).hexdigest() != expected_sha:
        _fail("FIXTURE_CHANGED")
    root = Path(path).parent / ".fixture-audit"
    _private_dir(root, create=True)
    # SHA names are versioned by content; a crash after archive but before
    # replace can safely reuse the exact same private archive on a retry.
    target = root / ("fixture-" + expected_sha + ".json")
    existing = _regular(target, allow_missing=True)
    if existing is not None:
        if existing != raw:
            _fail("ARCHIVE_INTEGRITY")
        return target
    try:
        _exclusive(target, raw)
    except FileExistsError:
        if _regular(target) != raw:
            _fail("ARCHIVE_INTEGRITY")
    _sync_dir(root)
    return target


def audit_event(path, action, operation_id, source_sha):
    """A secret-free, durable audit ledger separate from the sensitive archive."""
    root = Path(path).parent / ".fixture-audit"
    _private_dir(root, create=True)
    ledger = root / "events.jsonl"
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | OPEN_SAFE
    fd = os.open(ledger, flags, 0o600)
    try:
        mode = os.fstat(fd)
        if (not stat.S_ISREG(mode.st_mode) or mode.st_mode & 0o077
                or mode.st_uid != os.geteuid() or mode.st_nlink != 1):
            _fail("AUDIT_FILE_UNSAFE")
        record = {
            "at": datetime.now(timezone.utc).isoformat(),
            "action": action, "operation_id": operation_id,
            "source_sha256": source_sha,
        }
        with os.fdopen(fd, "ab") as output:
            output.write((json.dumps(record, separators=(",", ":")) + "\n").encode("utf-8"))
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        raise
    _sync_dir(root)
