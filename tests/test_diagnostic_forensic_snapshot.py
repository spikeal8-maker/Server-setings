"""Synthetic host history only; no Docker, real host, payload or credentials."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('forensic', Path(__file__).parents[1]/'scripts/diagnostic_forensic_snapshot.py')
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)
SECRET = b'SYNTHETIC-PRIVATE-KEY-NEVER-STDOUT'


class PublicContract(unittest.TestCase):
    def test_cli_defaults_to_plan(self):
        with patch.object(m, 'snapshot', return_value={'status': 'PLAN'}) as call, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(m.main(['--installation', '/example', '--destination', '/private/example']), 0)
            self.assertFalse(call.call_args.args[-1])

    def test_error_does_not_print_payload_paths_or_key(self):
        for error in (OSError(str(SECRET)), sqlite3.DatabaseError(str(SECRET))):
            output = io.StringIO()
            with patch.object(m, 'snapshot', side_effect=error), contextlib.redirect_stdout(output):
                self.assertEqual(m.main(['--installation', '/example', '--destination', '/private/example', '--apply']), 1)
            self.assertNotIn('SYNTHETIC', output.getvalue())
            self.assertEqual(json.loads(output.getvalue())['code'], 'COPY_FAILED')

    def test_only_fixed_collector_units_and_inactive_state(self):
        inactive = type('Result', (), {'returncode': 0, 'stdout': 'LoadState=loaded\nActiveState=inactive\nMainPID=0\n'})()
        with patch.object(m.subprocess, 'run', return_value=inactive) as run:
            m.quiescent('asa-log-collector.service')
            self.assertEqual(run.call_count, 2)
            self.assertTrue(all(c.args[0][:2] == ['systemctl', 'show'] for c in run.call_args_list))
        with self.assertRaisesRegex(m.Blocked, 'UNSUPPORTED'):
            m.quiescent('ssh.service')
        active = type('Result', (), {'returncode': 0, 'stdout': 'LoadState=loaded\nActiveState=active\nMainPID=12\n'})()
        with patch.object(m.subprocess, 'run', return_value=active), self.assertRaisesRegex(m.Blocked, 'NOT_QUIESCENT'):
            m.quiescent('asa-log-collector.service')


@unittest.skipUnless(os.name == 'posix' and hasattr(os, 'O_NOFOLLOW'), 'Actual fd/flock/modes require Linux; Ubuntu CI runs these')
class LinuxCopy(unittest.TestCase):
    def setUp(self):
        # Workspace ancestor permissions are trusted; /tmp intentionally is not.
        self.temp = tempfile.TemporaryDirectory(prefix='forensic-synthetic-', dir=Path(__file__).parents[1])
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.install = self.base/'install'
        self.asa = self.install/'.asa'
        self.private = self.asa/'diagnostics/private'
        self.store = self.asa/'diagnostics/store'
        self.private.mkdir(parents=True)
        self.store.mkdir()
        (self.asa/'log-collector.json').write_text('{"version":1}')
        (self.private/'segment-verification.key').write_bytes(SECRET)
        (self.private/'collector.lock').write_bytes(b'')
        (self.store/'catalog.json').write_text('{"historyAudit":{"state":"error"},"segments":[]}')
        (self.store/'seg-1-2.jsonl').write_bytes(b'known-corrupt-payload\x00\xff')
        db = sqlite3.connect(self.private/'index.sqlite')
        db.executescript('CREATE TABLE state(key TEXT PRIMARY KEY,value TEXT); CREATE TABLE segment_verification(name TEXT,proof TEXT,metadata TEXT);')
        db.execute('INSERT INTO state VALUES(?,?)', ('retired:segments', json.dumps({'seg-1-2.jsonl': None})))
        db.execute('INSERT INTO state VALUES(?,?)', ('retired:metadata', '{}'))
        db.commit(); db.close()
        self.target = self.base/'snapshot'
        self.addCleanup(patch.stopall)
        patch.object(m, 'OWNER_UID', os.geteuid()).start()
        patch.object(m, 'quiescent').start()

    def source(self):
        fd = m.directory(self.asa)
        try:
            return m.inventory(fd, host_scope=True)
        finally:
            os.close(fd)

    def test_plan_is_completely_readonly(self):
        before = self.source()
        self.assertEqual(m.snapshot(self.install, self.target)['status'], 'PLAN')
        self.assertEqual(before, self.source())
        self.assertFalse(self.target.exists())
        self.assertFalse(list(self.base.glob('snapshot.partial-*')))

    def test_preserves_corruption_missing_receipts_key_and_private_permissions(self):
        before = self.source()
        result = m.snapshot(self.install, self.target, apply=True)
        self.assertEqual(result['legacyDebt'], 1)
        self.assertFalse(result['recoverySnapshotVerified'])
        self.assertEqual(result['qualification'], 'NOT_A_VERIFIED_RECOVERY_SNAPSHOT')
        self.assertEqual(before, self.source())
        self.assertEqual((self.target/'raw/diagnostics/store/seg-1-2.jsonl').read_bytes(), b'known-corrupt-payload\x00\xff')
        self.assertEqual((self.target/'raw/diagnostics/private/segment-verification.key').read_bytes(), SECRET)
        self.assertNotIn('SYNTHETIC', json.dumps(result))
        for p in [self.target, *self.target.rglob('*')]:
            self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o700 if p.is_dir() else 0o600)
        manifest_bytes = (self.target/'manifest.private.json').read_bytes()
        self.assertEqual(m.hashlib.sha256(manifest_bytes).hexdigest(), result['manifestSha256'])
        manifest = json.loads(manifest_bytes)
        for relative, metadata in manifest['copies'].items():
            if 'sha256' in metadata:
                self.assertEqual(m.hashlib.sha256((self.target/relative).read_bytes()).hexdigest(), metadata['sha256'])
        self.assertFalse(list(self.base.glob('snapshot.partial-*')))

    def test_independent_native_and_business_state_are_excluded(self):
        native = self.asa/'diagnostics/application'
        native.mkdir(); (native/'active.jsonl').write_bytes(SECRET)
        (self.asa/'business-do-not-copy.json').write_bytes(SECRET)
        m.snapshot(self.install, self.target, apply=True)
        self.assertFalse((self.target/'raw/diagnostics/application').exists())
        self.assertFalse((self.target/'raw/business-do-not-copy.json').exists())
        self.assertEqual((native/'active.jsonl').read_bytes(), SECRET)

    def test_wal_is_backed_up_without_touching_original(self):
        db = sqlite3.connect(self.private/'index.sqlite')
        self.addCleanup(db.close)
        db.execute('PRAGMA journal_mode=WAL')
        db.execute("INSERT INTO state VALUES('wal-only','42')"); db.commit()
        before = self.source()
        m.snapshot(self.install, self.target, apply=True)
        self.assertEqual(before, self.source())
        name = m.hashlib.sha256(b'diagnostics/private/index.sqlite').hexdigest()+'.sqlite'
        with sqlite3.connect(self.target/'sqlite-backup'/name) as copied:
            self.assertEqual(copied.execute("SELECT value FROM state WHERE key='wal-only'").fetchone(), ('42',))

    def test_busy_collector_lock_rejects_without_copy(self):
        import fcntl
        with (self.private/'collector.lock').open('rb') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(m.Blocked, 'LOCK_BUSY'):
                m.snapshot(self.install, self.target, apply=True)
        self.assertFalse(self.target.exists())

    def test_source_race_retains_private_partial_never_promotes(self):
        original = m.copy_entry
        def race(*args):
            original(*args)
            (self.store/'seg-1-2.jsonl').write_bytes(b'changed-during-copy')
        with patch.object(m, 'copy_entry', side_effect=race), self.assertRaisesRegex(m.Blocked, 'SOURCE_RACE'):
            m.snapshot(self.install, self.target, apply=True)
        self.assertFalse(self.target.exists())
        partial = list(self.base.glob('snapshot.partial-*'))
        self.assertEqual(len(partial), 1)
        self.assertEqual(stat.S_IMODE(partial[0].stat().st_mode), 0o700)
        self.assertFalse((partial[0]/'receipt.json').exists())

    def test_low_space_and_active_service_do_not_create_destination(self):
        vfs = type('Space', (), {'f_favail': 100000, 'f_bavail': 0, 'f_frsize': 4096})()
        with patch.object(m.os, 'fstatvfs', return_value=vfs), self.assertRaisesRegex(m.Blocked, 'SPACE'):
            m.snapshot(self.install, self.target, apply=True)
        with patch.object(m, 'quiescent', side_effect=m.Blocked('COLLECTOR_NOT_QUIESCENT')), self.assertRaises(m.Blocked):
            m.snapshot(self.install, self.target, apply=True)
        self.assertFalse(self.target.exists())

    def test_symlink_and_hardlink_sources_and_destination_symlink_rejected(self):
        link = self.store/'link'
        link.symlink_to(self.private/'segment-verification.key')
        with self.assertRaises(m.Blocked): m.snapshot(self.install, self.target)
        link.unlink(); os.link(self.private/'segment-verification.key', link)
        with self.assertRaises(m.Blocked): m.snapshot(self.install, self.target)
        link.unlink(); self.target.symlink_to(self.base/'absent')
        with self.assertRaises(m.Blocked): m.snapshot(self.install, self.target)

    def test_copy_failure_retains_partial_and_existing_final_is_never_overwritten(self):
        before = self.source()
        with patch.object(m, 'sqlite_backup', side_effect=sqlite3.DatabaseError('synthetic')), self.assertRaises(sqlite3.DatabaseError):
            m.snapshot(self.install, self.target, apply=True)
        self.assertEqual(before, self.source())
        self.assertFalse(self.target.exists())
        self.assertTrue(list(self.base.glob('snapshot.partial-*')))
        self.target.mkdir(); (self.target/'owner-file').write_bytes(SECRET)
        with self.assertRaisesRegex(m.Blocked, 'DESTINATION_EXISTS'):
            m.snapshot(self.install, self.target, apply=True)
        self.assertEqual((self.target/'owner-file').read_bytes(), SECRET)


if __name__ == '__main__':
    unittest.main()
