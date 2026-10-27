"""Pinned encrypted backup provenance. No AGE secret, arbitrary paths or live restore.

The owner signs a JSON receipt after *local* decryption with a separate Ed25519
signing key. Only its public verification key may be installed under the private
staging root. A ciphertext hash alone is never a plaintext attestation.
"""
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


def owner_attestation(root=ROOT, snapshot_id=NEW_ID):
    if select(snapshot_id) != NEW_ID:
        raise OpsError('SOURCE_ID', 'Only the new source uses a signed owner receipt')
    att = attest_dir(root)
    receipt = att/(snapshot_id+'.json')
    signature = att/(snapshot_id+'.sig')
    public_key = att/'owner-ed25519-public.pem'
    raw = _read_private(receipt)
    _private_file(signature, 512)
    _private_file(public_key, 8192)
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
    try:
        result = subprocess.run([
            'openssl', 'pkeyutl', '-verify', '-pubin', '-rawin',
            '-inkey', str(public_key), '-in', str(receipt), '-sigfile', str(signature),
        ], capture_output=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise OpsError('OWNER_SIGNATURE', 'Owner signature verification is unavailable') from None
    if result.returncode:
        raise OpsError('OWNER_SIGNATURE', 'Owner signature is not valid')
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
