import base64
import contextlib
import fcntl
import json
import os
from pathlib import Path
import stat
import tempfile


class Stop(RuntimeError):
    pass


def require(value, reason):
    if not value:
        raise Stop(reason)


def private_directory(path):
    path = Path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    s = path.lstat()
    require(stat.S_ISDIR(s.st_mode) and s.st_uid == 0 and stat.S_IMODE(s.st_mode) == 0o700, 'PRIVATE_DIRECTORY')
    return path


def atomic(path, data, mode=0o600):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name, dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        parent = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save(path, value):
    atomic(path, json.dumps(value, sort_keys=True).encode())


def load(path):
    path = Path(path)
    s = path.lstat()
    require(stat.S_ISREG(s.st_mode) and s.st_uid == 0 and stat.S_IMODE(s.st_mode) == 0o600 and s.st_nlink == 1, 'PRIVATE_FILE')
    return json.loads(path.read_bytes())


@contextlib.contextmanager
def lock(path):
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        s = os.fstat(fd)
        require(s.st_uid == 0 and stat.S_ISREG(s.st_mode) and stat.S_IMODE(s.st_mode) == 0o600, 'PRIVATE_LOCK')
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


class DNSLease:
    """Compare bytes, inode identity, permissions and ownership before replacement.

    No replacement if another writer changed the resolver since our snapshot.
    External writers ignoring flock remain a race; post-write verification and
    CAS rollback never intentionally overwrite an observed foreign change.
    """
    def __init__(self, path):
        self.path = Path(path)

    def snapshot(self):
        fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            s = os.fstat(fd)
            require(stat.S_ISREG(s.st_mode) and s.st_uid == 0 and s.st_gid == 0 and stat.S_IMODE(s.st_mode) == 0o644 and s.st_nlink == 1, 'DNS_REGULAR_ROOT_0644')
            data = os.read(fd, 65537)
            require(len(data) <= 65536, 'DNS_SIZE')
            return {'data': base64.b64encode(data).decode(), 'uid': s.st_uid, 'gid': s.st_gid, 'mode': stat.S_IMODE(s.st_mode), 'inode': s.st_ino, 'device': s.st_dev}
        finally:
            os.close(fd)

    def replace(self, expected, data):
        require(self.snapshot() == expected, 'DNS_CAS_CONFLICT')
        atomic(self.path, data, 0o644)
        current = self.snapshot()
        require(base64.b64decode(current['data']) == data, 'DNS_WRITE_CHANGED')
        return current

    def restore(self, lease):
        current = self.snapshot()
        # Crash after intent but before replacement: original resolver already OK.
        if current == lease['original']:
            return
        if 'owned' not in lease and current['data'] == lease.get('planned') and current['inode'] != lease['original']['inode']:
            # Write-ahead DNS intent survived a crash after atomic replacement
            # but before its new inode could be journalled. Exact planned bytes
            # and root/mode checks are required; a different writer is retained.
            lease = dict(lease, owned=current)
        require(current == lease.get('owned'), 'DNS_CAS_CONFLICT')
        self.replace(current, base64.b64decode(lease['original']['data']))
