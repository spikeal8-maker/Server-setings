"""Receive the selected decrypted tar over an authenticated private channel.

Run as admins, with tar on stdin. The age key stays on the owner's workstation.
Only this previously authorized snapshot is accepted; no extraction links/code.
"""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sys
import tarfile

SNAPSHOT = '20261017T231505Z-65b68074'
ROOT = Path('/srv/asa-restore-snapshots')


def receive(snapshot_id=SNAPSHOT):
    if snapshot_id != SNAPSHOT:
        return receive_approved(snapshot_id)
    os.umask(0o077)
    target = ROOT/(SNAPSHOT+'.partial')
    final = ROOT/SNAPSHOT
    if target.exists() or final.exists():
        raise RuntimeError('Snapshot already present or partial; explicit review required')
    target.mkdir(mode=0o700)
    inventory = {}
    total = 0
    with tarfile.open(fileobj=sys.stdin.buffer, mode='r|*') as stream:
        for index, member in enumerate(stream):
            parts = PurePosixPath(member.name).parts
            if (index > 100000 or not parts or parts[0] != SNAPSHOT or '..' in parts
                    or '\\' in member.name or ':' in member.name or member.name.startswith('/')
                    or not (member.isfile() or member.isdir()) or member.size > 512*1024**2):
                raise RuntimeError('Unsafe snapshot member')
            relative = '/'.join(parts[1:])
            if not relative:
                if not member.isdir():
                    raise RuntimeError('Invalid snapshot root')
                continue
            destination = target/relative
            if member.isdir():
                destination.mkdir(mode=0o700, parents=True, exist_ok=True)
                continue
            if relative in inventory:
                raise RuntimeError('Duplicate snapshot member')
            total += member.size
            if total > 4*1024**3:
                raise RuntimeError('Snapshot exceeds capacity limit')
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            hashed = hashlib.sha256()
            with destination.open('xb') as output:
                source = stream.extractfile(member)
                while chunk := source.read(1024*1024):
                    output.write(chunk);hashed.update(chunk)
                output.flush();os.fsync(output.fileno())
            inventory[relative] = {'sha256': hashed.hexdigest(), 'bytes': member.size}
    manifest = json.loads((target/'manifest.json').read_text())
    expected = manifest.get('files')
    if (manifest.get('format') != 'asa-backup-v1' or not manifest.get('complete')
            or manifest.get('createdAt') != SNAPSHOT or not isinstance(expected, dict)
            or set(inventory) != set(expected)|{'manifest.json'}
            or any(inventory[name] != value for name, value in expected.items())):
        raise RuntimeError('Snapshot integrity verification failed; partial retained privately')
    target.rename(final)
    print(json.dumps({'snapshot_id': SNAPSHOT, 'files_verified': len(expected),
                      'manifest_sha256': inventory['manifest.json']['sha256'], 'bytes': total}))


def receive_approved(snapshot_id):
    """New-source receiver; invoked only after signed owner-side decryption proof.

    The old zero-argument legacy receiver above is intentionally unmodified.
    """
    if __package__:
        from .source_registry import (NEW_ID, NEW_SOURCE, STAGE_FORMAT, attest_dir,
                                      owner_attestation, canonical, sha, select)
    else:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from asa_ops.source_registry import (NEW_ID, NEW_SOURCE, STAGE_FORMAT, attest_dir,
                                             owner_attestation, canonical, sha, select)
    import unicodedata

    if select(snapshot_id) != NEW_ID:
        raise RuntimeError('Use the original receiver for the approved legacy source')
    signed, owner_sha = owner_attestation(ROOT, snapshot_id)
    att = attest_dir(ROOT)
    target = ROOT/(snapshot_id+'.partial')
    final = ROOT/snapshot_id
    receipt = att/(snapshot_id+'.stage.json')
    pending = att/(snapshot_id+'.stage.partial')
    if any(p.exists() or p.is_symlink() for p in (target, final, receipt, pending)):
        raise RuntimeError('Existing partial, snapshot or staging receipt requires review')
    os.umask(0o077)
    target.mkdir(mode=0o700)
    inventory = {}
    entries = set()
    total = 0

    class HashedInput:
        def __init__(self, inner):
            self.inner = inner
            self.digest = hashlib.sha256()
            self.total = 0

        def read(self, count=-1):
            data = self.inner.read(count)
            self.total += len(data)
            if self.total > 5 * 1024**3:
                raise RuntimeError('Decrypted input stream exceeds the safe limit')
            self.digest.update(data)
            return data

    incoming = HashedInput(sys.stdin.buffer)
    with tarfile.open(fileobj=incoming, mode='r|*') as stream:
        for index, member in enumerate(stream):
            original = member.name
            name = original[:-1] if member.isdir() and original.endswith('/') else original
            parts = name.split('/')
            if (index >= 100000 or not name or name.startswith('/')
                    or any(p in ('', '.', '..') for p in parts)
                    or parts[0] != snapshot_id or '\\' in name or ':' in name
                    or unicodedata.normalize('NFC', name) != name
                    or unicodedata.normalize('NFKC', name) != name
                    or len(name) > 4096 or member.size > 512 * 1024**2
                    or not (member.isdir() or member.isfile())
                    or getattr(member, 'sparse', None)):
                raise RuntimeError('Unsafe snapshot member')
            if name in entries:
                raise RuntimeError('Repeated snapshot member')
            entries.add(name)
            if len(parts) == 1:
                if not member.isdir():
                    raise RuntimeError('Unexpected root member type')
                continue
            rel = '/'.join(parts[1:])
            destination = target.joinpath(*parts[1:])
            if member.isdir():
                destination.mkdir(mode=0o700, parents=True, exist_ok=True)
                continue
            total += member.size
            if total > 4 * 1024**3:
                raise RuntimeError('Extracted snapshot exceeds capacity limit')
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            h = hashlib.sha256()
            with destination.open('xb') as out:
                source = stream.extractfile(member)
                if source is None:
                    raise RuntimeError('Missing tar file content')
                size = 0
                while block := source.read(1024 * 1024):
                    size += len(block)
                    out.write(block)
                    h.update(block)
                if size != member.size:
                    raise RuntimeError('Truncated tar member')
                out.flush()
                os.fsync(out.fileno())
            inventory[rel] = {'sha256': h.hexdigest(), 'bytes': size}
    while incoming.read(1024 * 1024):
        pass
    if incoming.digest.hexdigest() != signed['plaintext_sha256']:
        raise RuntimeError('Decrypted archive differs from signed owner receipt')
    if 'manifest.json' not in inventory:
        raise RuntimeError('Missing manifest')
    raw_manifest = (target/'manifest.json').read_bytes()
    if (inventory['manifest.json']['sha256'] != signed['manifest_sha256']
            or sha(raw_manifest) != signed['manifest_sha256']):
        raise RuntimeError('Manifest differs from signed owner receipt')
    try:
        manifest = json.loads(raw_manifest)
    except (ValueError, UnicodeError):
        raise RuntimeError('Invalid source manifest') from None
    expected = manifest.get('files')
    if (manifest.get('format') != 'asa-backup-v1'
            or manifest.get('complete') is not True
            or manifest.get('createdAt') != snapshot_id or not isinstance(expected, dict)
            or not {'database.dump', 'globals.sql', 'private.env'} <= set(expected)
            or {k: v for k, v in inventory.items() if k != 'manifest.json'} != expected):
        raise RuntimeError('Full snapshot manifest inventory differs')
    proof = {'format': STAGE_FORMAT, 'source': dict(NEW_SOURCE),
             'owner_receipt_sha256': owner_sha,
             'manifest_sha256': signed['manifest_sha256'],
             'plaintext_sha256': signed['plaintext_sha256']}
    for directory in (target, ROOT):
        fd = os.open(directory, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    target.rename(final)
    fd = os.open(ROOT, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    with pending.open('xb') as out:
        out.write(canonical(proof) + b'\n')
        out.flush()
        os.fsync(out.fileno())
    pending.rename(receipt)
    fd = os.open(att, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    print(json.dumps({'snapshot_id': snapshot_id, 'files_verified': len(expected),
                      'manifest_sha256': signed['manifest_sha256'],
                      'owner_attested': True, 'staged': True}))


if __name__ == '__main__':
    if len(sys.argv) == 1:
        receive()
    elif len(sys.argv) == 3 and sys.argv[1] == '--snapshot-id':
        receive(sys.argv[2])
    else:
        raise SystemExit('Usage: stage_snapshot.py [--snapshot-id APPROVED_ID]')
