"""Pinned encrypted backup provenance. No AGE secret, arbitrary paths or live restore.

The owner signs a JSON receipt after *local* decryption with a separate Ed25519
signing key. The verification key may exist in staging, but is NEVER trusted
unless its SPKI digest matches the independently owner-approved release pin.
The pin is initially absent (fail closed). Key rotation requires a distinct,
owner-approved and independently reviewed release commit replacing the pin.
The AGE identity and the signing private key never enter GitHub or the server.
A ciphertext hash alone is never a plaintext attestation.
"""
import base64
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess

from .store import OpsError

LEGACY_ID = '20261017T231505Z-65b68074'
NEW_ID = '20261026T231505Z-038beee0'
ALLOWED_IDS = (LEGACY_ID, NEW_ID)
ROOT = Path('/srv/asa-restore-snapshots')
NEW_SOURCE = {
    'snapshot_id': NEW_ID,
    'provider': 'yandex-disk',
    'namespace': 'ASA-Backups',
    'project': 'asa-lab',
    'filename': 'asa-lab-20261026T231505Z-038beee0.tar.age',
    'ciphertext_bytes': 207415432,
    'ciphertext_sha256': '526988100deb2ba55028ff00255ff3a63add0f3ebcf772c8ba4f9cee0b296592',
}
RECEIPT_FORMAT = 'asa-owner-attestation-v1'
STAGE_FORMAT = 'asa-staging-receipt-v1'
HEX = re.compile(r'[a-f0-9]{64}\Z')

# RELEASE TRUST ANCHOR: unprovisioned until the owner approves an actual key.
# The code/release, NOT the writable staging directory, owns this fingerprint.
# Rotation is a separately authorized and reviewed commit to this constant.
# Never source this value from MCP arguments, staging, environment or receipts.
OWNER_ED25519_SPKI_SHA256 = None

# RFC 8410: DER SubjectPublicKeyInfo for Ed25519 is exactly this 12-byte prefix
# followed by the raw 32-byte public key. Other algorithms/parameters fail closed.
ED25519_SPKI_PREFIX = bytes.fromhex('302a300506032b6570032100')


def select(snapshot_id):
    if type(snapshot_id) is not str or snapshot_id not in ALLOWED_IDS:
        raise OpsError('SOURCE_ID', 'Snapshot is not in the approved allowlist')
    return snapshot_id


def _private_dir(path, missing='SOURCE_STAGE_MISSING'):
    path = Path(path)
    try:
        st = path.lstat()
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise OpsError(missing, 'Private source directory is not available') from None
    if (not stat.S_ISDIR(st.st_mode) or resolved != path.absolute()
            or st.st_uid != os.getuid() or st.st_mode & 0o077):
        raise OpsError('SOURCE_PATH', 'Private source directory identity differs')
    return path


def _private_file(path, limit=65536, missing='BLOCKED_KEY_ACCESS'):
    path = Path(path)
    try:
        st = path.lstat()
    except OSError:
        raise OpsError(missing, 'Owner verification material is not available') from None
    if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid()
            or st.st_nlink != 1 or st.st_mode & 0o077 or st.st_size > limit):
        raise OpsError('SOURCE_FILE', 'Private source file identity differs')
    return path


def _read_private(path, limit=65536, missing='BLOCKED_KEY_ACCESS'):
    _private_file(path, limit, missing)
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'rb') as f:
        st = os.fstat(f.fileno())
        if (not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or
                st.st_uid != os.getuid() or st.st_mode & 0o077 or st.st_size > limit):
            raise OpsError('SOURCE_FILE', 'Private file changed during verification')
        raw = f.read(limit + 1)
    if len(raw) > limit:
        raise OpsError('SOURCE_FILE', 'Private verification material is too large')
    return raw


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(data):
    return json.dumps(data, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode('ascii')


def attest_dir(root):
    _private_dir(root, 'BLOCKED_KEY_ACCESS')
    return _private_dir(Path(root)/'.owner-attestations', 'BLOCKED_KEY_ACCESS')


def ed25519_spki_sha256(pem):
    """Fingerprint an exact Ed25519 SPKI, not any PEM an attacker provides."""
    try:
        lines = pem.splitlines()
        if (len(lines) < 3 or lines[0] != b'-----BEGIN PUBLIC KEY-----'
                or lines[-1] != b'-----END PUBLIC KEY-----'):
            raise ValueError('Invalid PEM envelope')
        der = base64.b64decode(b''.join(lines[1:-1]), validate=True)
    except (AttributeError, TypeError, ValueError):
        raise OpsError('OWNER_KEY_TYPE', 'Owner public key PEM is not valid Ed25519') from None
    if len(der) != len(ED25519_SPKI_PREFIX) + 32 or not der.startswith(ED25519_SPKI_PREFIX):
        raise OpsError('OWNER_KEY_TYPE', 'Only Ed25519 public keys are accepted')
    return sha(der)


def _file_identity(st):
    return (st.st_dev, st.st_ino, st.st_size, st.st_uid, st.st_gid,
            st.st_mode, st.st_nlink, st.st_mtime_ns, st.st_ctime_ns)


def _capture_private(path, limit):
    """Read and pin the exact bytes and metadata of one private regular file."""
    path = Path(path)
    try:
        before = path.lstat()
        payload = _read_private(path, limit)
        after = path.lstat()
    except OSError:
        raise OpsError('OWNER_IDENTITY_CHANGED', 'Owner proof changed during read') from None
    identity = _file_identity(after)
    if _file_identity(before) != identity or len(payload) != after.st_size:
        raise OpsError('OWNER_IDENTITY_CHANGED', 'Owner proof changed during read')
    return payload, identity


def _assert_unchanged(path, data, identity, limit):
    """A replace, truncate or in-place mutation during verification is fatal."""
    latest, current = _capture_private(path, limit)
    if current != identity or latest != data:
        raise OpsError('OWNER_IDENTITY_CHANGED', 'Owner proof changed during verification')


@contextlib.contextmanager
def _sealed_verifier_input(name, data):
    """Transfer captured key/signature bytes using sealed anonymous Linux FDs."""
    try:
        fd = os.memfd_create(name, os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
    except (AttributeError, OSError):
        raise OpsError('OWNER_SIGNATURE', 'Immutable signature verification is unavailable') from None
    try:
        view = memoryview(data)
        while view:
            length = os.write(fd, view)
            if length <= 0:
                raise OSError('Unable to copy immutable verification material')
            view = view[length:]
        fcntl.fcntl(fd, fcntl.F_ADD_SEALS, fcntl.F_SEAL_WRITE |
                    fcntl.F_SEAL_GROW | fcntl.F_SEAL_SHRINK | fcntl.F_SEAL_SEAL)
        os.lseek(fd, 0, os.SEEK_SET)
        yield fd
    except OSError:
        raise OpsError('OWNER_SIGNATURE', 'Immutable signature verification failed') from None
    finally:
        os.close(fd)


def owner_attestation(root=ROOT, snapshot_id=NEW_ID):
    if select(snapshot_id) != NEW_ID:
        raise OpsError('SOURCE_ID', 'Only the new source uses a signed owner receipt')
    expected_pin = OWNER_ED25519_SPKI_SHA256
    if type(expected_pin) is not str or not HEX.fullmatch(expected_pin):
        raise OpsError('BLOCKED_KEY_ACCESS', 'An independently approved owner key fingerprint is required')
    att = attest_dir(root)
    receipt = att/(snapshot_id+'.json')
    signature = att/(snapshot_id+'.sig')
    public_key = att/'owner-ed25519-public.pem'

    # Everything OpenSSL consumes is captured once; no verifier pathname reopen.
    raw, receipt_id = _capture_private(receipt, 65536)
    signature_bytes, signature_id = _capture_private(signature, 512)
    key_bytes, key_id = _capture_private(public_key, 8192)
    if ed25519_spki_sha256(key_bytes) != expected_pin:
        raise OpsError('OWNER_KEY_UNTRUSTED', 'Public key is not the approved owner key')
    if len(signature_bytes) != 64:
        raise OpsError('OWNER_SIGNATURE', 'Invalid Ed25519 signature length')

    try:
        with _sealed_verifier_input('asa-owner-receipt', raw) as receipt_fd, \
                _sealed_verifier_input('asa-owner-key', key_bytes) as key_fd, \
                _sealed_verifier_input('asa-owner-signature', signature_bytes) as sig_fd:
            result = subprocess.run([
                'openssl', 'pkeyutl', '-verify', '-pubin', '-rawin',
                '-inkey', '/proc/self/fd/'+str(key_fd),
                '-in', '/proc/self/fd/'+str(receipt_fd),
                '-sigfile', '/proc/self/fd/'+str(sig_fd),
            ], capture_output=True, timeout=15, check=False,
                pass_fds=(receipt_fd, key_fd, sig_fd))
    except (OSError, subprocess.TimeoutExpired):
        raise OpsError('OWNER_SIGNATURE', 'Owner signature verification is unavailable') from None

    # Files may change while OpenSSL runs. Such a rotation is *never* accepted
    # even if their captured immutable bytes happened to verify successfully.
    _assert_unchanged(receipt, raw, receipt_id, 65536)
    _assert_unchanged(signature, signature_bytes, signature_id, 512)
    _assert_unchanged(public_key, key_bytes, key_id, 8192)
    if result.returncode:
        raise OpsError('OWNER_SIGNATURE', 'Owner signature is not valid')
    try:
        signed = json.loads(raw)
    except (ValueError, UnicodeError):
        raise OpsError('OWNER_RECEIPT', 'Invalid signed owner receipt') from None
    if (type(signed) is not dict
            or set(signed) != {'format', 'snapshot_id', 'source', 'manifest_sha256', 'plaintext_sha256'}
            or signed['format'] != RECEIPT_FORMAT or signed['snapshot_id'] != NEW_ID
            or signed['source'] != NEW_SOURCE
            or not isinstance(signed['manifest_sha256'], str) or not HEX.fullmatch(signed['manifest_sha256'])
            or not isinstance(signed['plaintext_sha256'], str) or not HEX.fullmatch(signed['plaintext_sha256'])
            or raw != canonical(signed) + b'\n'):
        raise OpsError('OWNER_RECEIPT', 'Owner receipt does not match the pinned source')
    return signed, sha(raw)


def _file_hash(path, limit):
    _private_file(path, limit, 'SOURCE_INTEGRITY')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    digest = hashlib.sha256()
    count = 0
    with os.fdopen(fd, 'rb') as handle:
        for part in iter(lambda: handle.read(1024 * 1024), b''):
            count += len(part)
            if count > limit:
                raise OpsError('SOURCE_INTEGRITY', 'Source file exceeds size limit')
            digest.update(part)
    return {'bytes': count, 'sha256': digest.hexdigest()}


def verify_staged(root=ROOT, snapshot_id=NEW_ID):
    """Fail closed on owner, stage, manifest or any extracted file drift."""
    signed, owner_sha = owner_attestation(root, snapshot_id)
    att = attest_dir(root)
    stage_path = att/(NEW_ID+'.stage.json')
    try:
        stage = json.loads(_read_private(stage_path, missing='SOURCE_STAGE_MISSING'))
    except (ValueError, UnicodeError):
        raise OpsError('SOURCE_STAGE', 'Invalid private staging receipt') from None
    expected_stage = {
        'format': STAGE_FORMAT, 'source': NEW_SOURCE,
        'owner_receipt_sha256': owner_sha,
        'manifest_sha256': signed['manifest_sha256'],
        'plaintext_sha256': signed['plaintext_sha256'],
    }
    if stage != expected_stage:
        raise OpsError('SOURCE_STAGE', 'Staging receipt does not match owner attestation')
    stage_sha = sha(_read_private(stage_path))
    target = _private_dir(Path(root)/NEW_ID)
    if (Path(root)/(NEW_ID+'.partial')).exists():
        raise OpsError('SOURCE_PARTIAL', 'Unfinished staging blocks source use')
    raw = _read_private(target/'manifest.json', 32 * 1024 * 1024, 'SOURCE_INTEGRITY')
    if sha(raw) != signed['manifest_sha256']:
        raise OpsError('SOURCE_MANIFEST', 'Manifest differs from signed owner receipt')
    try:
        manifest = json.loads(raw)
    except (ValueError, UnicodeError):
        raise OpsError('SOURCE_MANIFEST', 'Manifest is not valid JSON') from None
    if (type(manifest) is not dict or manifest.get('format') != 'asa-backup-v1'
            or manifest.get('complete') is not True or manifest.get('createdAt') != NEW_ID
            or not isinstance(manifest.get('files'), dict)
            or not {'database.dump', 'globals.sql', 'private.env'} <= set(manifest['files'])):
        raise OpsError('SOURCE_MANIFEST', 'Source manifest identity or completeness differs')
    expected = manifest['files']
    actual = {}
    count = 0
    for path in target.rglob('*'):
        count += 1
        if count > 100000:
            raise OpsError('SOURCE_LIMIT', 'Too many snapshot filesystem entries')
        st = path.lstat()
        relative = path.relative_to(target).as_posix()
        if (st.st_uid != os.getuid() or st.st_mode & 0o077
                or not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode))):
            raise OpsError('SOURCE_TYPE', 'Unexpected filesystem entry in staged source')
        if stat.S_ISREG(st.st_mode):
            if st.st_nlink != 1 or st.st_size > 512 * 1024 * 1024:
                raise OpsError('SOURCE_TYPE', 'Invalid staged file link count or size')
            if relative != 'manifest.json':
                actual[relative] = _file_hash(path, 512 * 1024 * 1024)
    if actual != expected:
        raise OpsError('SOURCE_INTEGRITY', 'Source file inventory does not match manifest')
    return {
        'root': target, 'manifest': manifest, 'source': dict(NEW_SOURCE),
        'manifest_sha256': signed['manifest_sha256'],
        'owner_receipt_sha256': owner_sha, 'staging_receipt_sha256': stage_sha,
    }
