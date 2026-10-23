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


def receive():
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


if __name__ == '__main__':
    receive()
