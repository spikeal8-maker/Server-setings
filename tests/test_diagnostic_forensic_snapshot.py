"""Synthetic host history only; no Docker, real host, payload or credentials."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
import stat
import tempfile
import unittest
from types import SimpleNamespace
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
        identity = dict(mode='system', owner='admins', uid=1000, gid=1000, root='/example', name='asa-log-collector.service')
        inactive = SimpleNamespace(returncode=0, stdout='LoadState=loaded\nActiveState=inactive\nMainPID=0\nWorkingDirectory=/example\nUser=admins\n')
        with patch.object(m, 'registered_runner', return_value=identity), patch.object(m.subprocess, 'run', return_value=inactive) as run:
            m.quiescent('/example', None)
            self.assertEqual(run.call_count, 2)
            self.assertTrue(all(c.args[0][:3] == ['/usr/bin/systemctl', '--system', 'show'] for c in run.call_args_list))
        active = type('Result', (), {'returncode': 0, 'stdout': 'LoadState=loaded\nActiveState=active\nMainPID=12\n'})()
        with patch.object(m, 'registered_runner', return_value=identity), patch.object(m.subprocess, 'run', return_value=active), self.assertRaisesRegex(m.Blocked, 'NOT_QUIESCENT'):
            m.quiescent('/example', None)

    def test_user_manager_uses_only_verified_owner_bus_and_no_shell(self):
        identity = dict(mode='user', owner='admins', uid=1000, gid=1000, root='/example', name='asa-log-collector-demo.service')
        prefix = ['/usr/bin/setpriv', '--reuid', '1000', '/usr/bin/systemctl', '--user']
        service = SimpleNamespace(returncode=0, stdout='LoadState=loaded\nActiveState=inactive\nMainPID=0\nWorkingDirectory=/example\nUser=\n')
        timer = SimpleNamespace(returncode=0, stdout='LoadState=loaded\nActiveState=inactive\n')
        with patch.object(m, 'registered_runner', return_value=identity), patch.object(m, 'user_manager_prefix', return_value=prefix) as bus, patch.object(m.subprocess, 'run', side_effect=[service, timer]) as run:
            m.quiescent('/example', None)
            bus.assert_called_once_with(identity)
            self.assertEqual(run.call_args_list[0].args[0], prefix+['show', identity['name'], '--property=LoadState,ActiveState,MainPID,WorkingDirectory,User'])
            self.assertTrue(all(not c.kwargs.get('shell') for c in run.call_args_list))
        missing_timer = SimpleNamespace(returncode=1, stdout='LoadState=not-found\n')
        with patch.object(m, 'registered_runner', return_value=identity), patch.object(m, 'user_manager_prefix', return_value=prefix), patch.object(m.subprocess, 'run', side_effect=[service, missing_timer]):
            m.quiescent('/example', None) # Canonical service-only installation needs no dummy timer.
        for stdout in ('LoadState=not-found\n', 'LoadState=loaded\nActiveState=active\nMainPID=3\n',
                       'LoadState=loaded\nActiveState=inactive\nMainPID=0\nWorkingDirectory=/foreign\nUser=\n'):
            with patch.object(m, 'registered_runner', return_value=identity), patch.object(m, 'user_manager_prefix', return_value=prefix), patch.object(m.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=stdout)), self.assertRaises(m.Blocked):
                m.quiescent('/example', None)

    def test_registration_denies_unknown_manager_missing_uid_wrong_unit_foreign_root(self):
        settings = {'project': 'demo'}
        runner = dict(version=1, root='/example', project='demo', owner='admins', mode='user',
                      name='asa-log-collector-demo.service', scriptDigest='a'*64, revision='3e32de6987665c4d7c1d33ba7ba82019afdf9cdb')
        account = SimpleNamespace(pw_uid=1000, pw_gid=1000)
        with patch.object(m, 'owner_account', return_value=account):
            self.assertEqual(m.validate_registration(PurePosixPath('/example'), settings, runner, None)['uid'], 1000)
            for changed in ({'root':'/foreign'}, {'project':'foreign'}, {'mode':'unknown'}, {'name':'ssh.service'},
                            {'owner':'admins;touch /tmp/no'}, {'ownerUid':2000}, {'scriptDigest':'stale'}, {'revision':'b'*40}):
                with self.subTest(changed=changed), self.assertRaises(m.Blocked):
                    m.validate_registration(PurePosixPath('/example'), settings, {**runner, **changed}, None)
            with self.assertRaises(m.Blocked):
                m.validate_registration(PurePosixPath('/example'), settings, runner, 'asa-log-collector.service')
        with patch.object(m, 'owner_account', side_effect=KeyError), self.assertRaisesRegex(m.Blocked, 'OWNER_MISSING'):
            m.validate_registration(PurePosixPath('/example'), settings, runner, None)
        with patch.object(m, 'owner_account', return_value=SimpleNamespace(pw_uid=None, pw_gid=1000)), self.assertRaisesRegex(m.Blocked, 'UID_MISSING'):
            m.validate_registration(PurePosixPath('/example'), settings, runner, None)

    def test_user_bus_owner_linger_and_exact_clean_argv(self):
        identity = dict(uid=1000, gid=1000)
        info = SimpleNamespace(st_uid=1000, st_mode=stat.S_IFDIR | 0o700)
        socket = SimpleNamespace(st_uid=1000, st_mode=stat.S_IFSOCK | 0o777)
        login = SimpleNamespace(returncode=0, stdout='Linger=yes\nRuntimePath=/run/user/1000\nState=lingering\n')
        with patch.object(m, 'Path', PurePosixPath), patch.object(m, 'directory', return_value=99), patch.object(m.os, 'fstat', return_value=info), patch.object(m.os, 'stat', return_value=socket), patch.object(m.os, 'close'), patch.object(m.subprocess, 'run', return_value=login) as query:
            prefix = m.user_manager_prefix(identity)
            self.assertEqual(prefix[:7], ['/usr/bin/setpriv', '--reuid', '1000', '--regid', '1000', '--clear-groups', '--no-new-privs'])
            self.assertEqual(prefix[-2:], ['/usr/bin/systemctl', '--user'])
            self.assertIn('DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus', prefix)
            self.assertIn('-i', prefix)
            query.assert_called_once_with(
                ['/usr/bin/loginctl', 'show-user', '1000', '--property=Linger',
                 '--property=RuntimePath', '--property=State'],
                capture_output=True, text=True, timeout=10)
            for bad in (SimpleNamespace(st_uid=2000, st_mode=stat.S_IFSOCK | 0o777), SimpleNamespace(st_uid=1000, st_mode=stat.S_IFLNK | 0o777)):
                with patch.object(m.os, 'stat', return_value=bad), self.assertRaisesRegex(m.Blocked, 'USER_BUS_UNTRUSTED'):
                    m.user_manager_prefix(identity)
            with patch.object(m.subprocess, 'run', return_value=SimpleNamespace(returncode=1, stdout='')), self.assertRaisesRegex(m.Blocked, 'USER_MANAGER_UNAVAILABLE'):
                m.user_manager_prefix(identity)
            # H03's comma-list adapter returned exit0 with no requested fields.
            # A successful process exit is never proof of a usable user manager.
            for stdout in ('', 'Linger=yes\nRuntimePath=/run/user/1000\n',
                           'RuntimePath=/run/user/1000\nState=active\n', 'Linger=yes\nState=active\n'):
                with self.subTest(stdout=stdout), patch.object(m.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=stdout)), self.assertRaisesRegex(m.Blocked, 'USER_MANAGER_UNAVAILABLE'):
                    m.user_manager_prefix(identity)


    def test_fixed_bundle_contract_requires_exact_pin_and_valid_revision(self):
        self.assertEqual(m.runner_digest_files('b'*40, m.FIXED_RUNTIME_BUNDLE_SHA256),
                         (*m.BASE_DIGEST_FILES, 'deployment/operational.py'))
        self.assertEqual(m.runner_digest_files('b'*40, m.CANONICAL_RUNTIME_BUNDLE_SHA256),
                         (*m.BASE_DIGEST_FILES, 'deployment/operational.py'))
        for revision in (None, [], '', 'B'*40, 'b'*39, 'b'*41, 'x'*40):
            with self.subTest(revision=revision), self.assertRaisesRegex(m.Blocked, 'REVISION_CONTRACT_UNSUPPORTED'):
                m.runner_digest_files(revision, m.FIXED_RUNTIME_BUNDLE_SHA256)
        for digest in (None, '', 'a'*64, m.FIXED_RUNTIME_BUNDLE_SHA256[:-1]+'0'):
            with self.subTest(digest=digest), self.assertRaisesRegex(m.Blocked, 'REVISION_CONTRACT_UNSUPPORTED'):
                m.runner_digest_files('b'*40, digest)
        self.assertEqual(m.runner_digest_files('dfee5badd299d318a8e51ff0cbf88e9656333a5d', 'a'*64),
                         m.BASE_DIGEST_FILES)
        self.assertEqual(m.runner_digest_files('3e32de6987665c4d7c1d33ba7ba82019afdf9cdb', 'a'*64),
                         (*m.BASE_DIGEST_FILES, 'deployment/operational.py'))

    def test_fixed_bundle_contract_preserves_registration_identity_guards(self):
        settings = {'project': 'demo'}
        runner = dict(version=1, root='/example', project='demo', owner='admins', mode='user',
                      name='asa-log-collector-demo.service', scriptDigest=m.FIXED_RUNTIME_BUNDLE_SHA256,
                      revision='b'*40)
        with patch.object(m, 'owner_account', return_value=SimpleNamespace(pw_uid=1000, pw_gid=1000)):
            self.assertEqual(m.validate_registration(PurePosixPath('/example'), settings, runner, None)['revision'], 'b'*40)
            for changed in ({'root':'/foreign'}, {'project':'foreign'}, {'mode':'unknown'},
                            {'name':'ssh.service'}, {'owner':'root;bad'}, {'ownerUid':2000}):
                with self.subTest(changed=changed), self.assertRaises(m.Blocked):
                    m.validate_registration(PurePosixPath('/example'), settings, {**runner, **changed}, None)
            with self.assertRaisesRegex(m.Blocked, 'UNIT_MISMATCH'):
                m.validate_registration(PurePosixPath('/example'), settings, runner, 'asa-log-collector.service')

    def test_fixed_file_pins_reject_actual_6a98_redistribution_profiles(self):
        # Recorded from immutable public 6a98 blobs: splitting collect_logs.py at
        # byte74486 and moving the filename delimiter keeps the aggregate b5d79.
        profiles = {
            'collect_logs.py': (74486, 'd23cdb795b88d7e73b518d180d272c041257cef67e40fd4d2fec5c56e2c438af'),
            'collect-windows-logs.ps1': (68641, '79a11984579f21c79d3ac50bf3bd3828634fde4fedd1e46232566c10968bd349'),
        }
        for profile_digest, pins in m.FIXED_RUNTIME_PROFILES.items():
            for name, (length, digest) in pins.items():
                m.verify_fixed_runtime_file(name, length, digest, profile_digest)
                for wrong in ((length+1, digest), (length, '0'*64)):
                    with self.subTest(profile=profile_digest, name=name, wrong=wrong), self.assertRaisesRegex(m.Blocked, 'FIXED_FILE_MISMATCH'):
                        m.verify_fixed_runtime_file(name, *wrong, profile_digest)
            for name, profile in profiles.items():
                with self.subTest(profile=profile_digest, name=name), self.assertRaisesRegex(m.Blocked, 'FIXED_FILE_MISMATCH'):
                    m.verify_fixed_runtime_file(name, *profile, profile_digest)

    def test_materialization_profiles_require_one_complete_matching_profile(self):
        ps1 = 'collect-windows-logs.ps1'
        lf = m.FIXED_RUNTIME_BUNDLE_SHA256
        crlf = m.CANONICAL_RUNTIME_BUNDLE_SHA256
        self.assertEqual(len(m.FIXED_RUNTIME_PROFILES), 2)
        self.assertEqual(set(m.FIXED_RUNTIME_PROFILES[lf]), set(m.FIXED_RUNTIME_DIGEST_FILES))
        self.assertEqual(set(m.FIXED_RUNTIME_PROFILES[crlf]), set(m.FIXED_RUNTIME_DIGEST_FILES))
        for selected, other in ((lf, crlf), (crlf, lf)):
            with self.subTest(selected=selected), self.assertRaisesRegex(m.Blocked, 'FIXED_FILE_MISMATCH'):
                m.verify_fixed_runtime_file(ps1, *m.FIXED_RUNTIME_PROFILES[other][ps1], selected)
        for selected in (lf, crlf, 'a'*64):
            with self.subTest(selected=selected), self.assertRaisesRegex(m.Blocked, 'FIXED_FILE_MISMATCH'):
                m.verify_fixed_runtime_file(ps1, 4144, '0'*64, selected)

    def test_filename_delimiter_can_preserve_aggregate_but_not_file_pins(self):
        first, second = m.FIXED_RUNTIME_DIGEST_FILES[:2]
        original = {name: b'# synthetic\n' for name in m.FIXED_RUNTIME_DIGEST_FILES}
        original[first] = b'prefix'+second.encode()+b'suffix'
        changed = dict(original)
        changed[first] = b'prefix'
        changed[second] = b'suffix'+second.encode()+original[second]
        def aggregate(files):
            h = m.hashlib.sha256()
            for name in m.FIXED_RUNTIME_DIGEST_FILES:
                h.update(name.encode()); h.update(files[name])
            return h.hexdigest()
        self.assertEqual(aggregate(original), aggregate(changed))
        pins = {n:(len(b), m.hashlib.sha256(b).hexdigest()) for n,b in original.items()}
        selected = aggregate(original)
        with patch.object(m, 'FIXED_RUNTIME_PROFILES', {selected: pins}):
            for name, value in changed.items():
                if name in (first, second):
                    with self.subTest(name=name), self.assertRaisesRegex(m.Blocked, 'FIXED_FILE_MISMATCH'):
                        m.verify_fixed_runtime_file(name, len(value), m.hashlib.sha256(value).hexdigest(), selected)


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
        import pwd
        account = pwd.getpwuid(os.geteuid())
        settings = dict(project='demo', **({'collectorMode':'system-root'} if account.pw_uid == 0 else {}))
        (self.asa/'installation.json').write_text(json.dumps(settings))
        self.runner = dict(version=1, root=str(self.install), project='demo', owner=account.pw_name,
                           mode='system-root' if account.pw_uid == 0 else 'system', name='asa-log-collector-demo.service',
                           revision='3e32de6987665c4d7c1d33ba7ba82019afdf9cdb')
        digest = m.hashlib.sha256()
        for name in ('collect_logs.py', 'collect-windows-logs.ps1', 'resource_sampling.py', 'memory_sampling.py', 'deployment/operational.py'):
            path = self.install/'tools'/name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'# synthetic source only\n')
            digest.update(name.encode()); digest.update(path.read_bytes())
        self.runner['scriptDigest'] = digest.hexdigest()
        (self.asa/'collector-runner.json').write_text(json.dumps(self.runner))
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
        self.quiet_patch = patch.object(m, 'quiescent')
        self.quiet_patch.start()

    def source(self):
        fd = m.directory(self.asa)
        try:
            return m.inventory(fd, host_scope=True)
        finally:
            os.close(fd)

    def runtime_file_pins(self):
        return {name: (len(value), m.hashlib.sha256(value).hexdigest())
                for name in m.FIXED_RUNTIME_DIGEST_FILES
                for value in [(self.install/'tools'/name).read_bytes()]}

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

    def test_actual_registration_denies_stale_digest(self):
        identity = m.registered_runner(self.install, None)
        self.assertEqual(identity['name'], self.runner['name'])
        (self.install/'tools/collect_logs.py').write_bytes(b'# changed after registration\n')
        with self.assertRaisesRegex(m.Blocked, 'REGISTRATION_STALE'):
            m.registered_runner(self.install, None)

    def test_known_legacy_four_file_contract_and_no_fallback_for_new_contract(self):
        (self.install/'tools/deployment/operational.py').unlink()
        self.runner['revision'] = 'dfee5badd299d318a8e51ff0cbf88e9656333a5d'
        digest = m.hashlib.sha256()
        for name in m.BASE_DIGEST_FILES:
            digest.update(name.encode()); digest.update((self.install/'tools'/name).read_bytes())
        self.runner['scriptDigest'] = digest.hexdigest()
        (self.asa/'collector-runner.json').write_text(json.dumps(self.runner))
        self.assertEqual(m.registered_runner(self.install, None)['revision'], self.runner['revision'])
        self.runner['revision'] = '3e32de6987665c4d7c1d33ba7ba82019afdf9cdb'
        (self.asa/'collector-runner.json').write_text(json.dumps(self.runner))
        with self.assertRaises(FileNotFoundError):
            m.registered_runner(self.install, None)

    def test_fixed_bundle_later_revision_recomputes_every_required_file(self):
        # Synthetic bytes exercise the closed hash mechanism; the production pin
        # is separately derived from immutable public Git blobs, never this fixture.
        self.runner['revision'] = 'b'*40
        (self.asa/'collector-runner.json').write_text(json.dumps(self.runner))
        with patch.object(m, 'FIXED_RUNTIME_PROFILES', {self.runner['scriptDigest']: self.runtime_file_pins()}):
            self.assertEqual(m.registered_runner(self.install, None)['revision'], 'b'*40)
            for name in m.FIXED_RUNTIME_DIGEST_FILES:
                path = self.install/'tools'/name
                original = path.read_bytes()
                with self.subTest(name=name, fault='changed-byte'):
                    path.write_bytes(bytes([original[0] ^ 1])+original[1:])
                    with self.assertRaisesRegex(m.Blocked, 'FIXED_FILE_MISMATCH'):
                        m.registered_runner(self.install, None)
                    path.write_bytes(original)
                with self.subTest(name=name, fault='missing-file'):
                    path.unlink()
                    with self.assertRaises(FileNotFoundError):
                        m.registered_runner(self.install, None)
                    path.write_bytes(original)

    def test_fixed_bundle_forged_registration_cannot_replace_actual_bytes(self):
        self.runner.update(revision='b'*40, scriptDigest=m.FIXED_RUNTIME_BUNDLE_SHA256)
        (self.asa/'collector-runner.json').write_text(json.dumps(self.runner))
        with self.assertRaisesRegex(m.Blocked, 'FIXED_FILE_MISMATCH'):
            m.registered_runner(self.install, None)

    def test_unknown_revision_denies_unreviewed_self_consistent_bundle(self):
        self.runner['revision'] = 'b'*40
        (self.asa/'collector-runner.json').write_text(json.dumps(self.runner))
        with self.assertRaisesRegex(m.Blocked, 'REVISION_CONTRACT_UNSUPPORTED'):
            m.registered_runner(self.install, None)

    def test_fixed_bundle_preserves_actual_registration_and_quiescence_checks(self):
        self.quiet_patch.stop()
        self.runner['revision'] = 'b'*40
        (self.asa/'collector-runner.json').write_text(json.dumps(self.runner))
        inactive = ('LoadState=loaded\nActiveState=inactive\nMainPID=0\nWorkingDirectory='
                    +str(self.install)+'\nUser='+self.runner['owner']+'\n')
        with patch.object(m, 'FIXED_RUNTIME_PROFILES', {self.runner['scriptDigest']: self.runtime_file_pins()}):
            with patch.object(m.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=inactive)):
                m.quiescent(self.install, None)
            for stdout in (inactive.replace('inactive', 'active'), inactive.replace('MainPID=0', 'MainPID=8'),
                           inactive.replace(str(self.install), '/foreign'), inactive.replace('User='+self.runner['owner'], 'User=foreign')):
                with self.subTest(stdout=stdout), patch.object(m.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=stdout)), self.assertRaises(m.Blocked):
                    m.quiescent(self.install, None)
            with self.assertRaisesRegex(m.Blocked, 'UNIT_MISMATCH'):
                m.registered_runner(self.install, 'ssh.service')

    def test_fixed_bundle_registered_runner_denies_delimiter_redistribution(self):
        first, second = m.FIXED_RUNTIME_DIGEST_FILES[:2]
        (self.install/'tools'/first).write_bytes(b'prefix'+second.encode()+b'suffix')
        pins = self.runtime_file_pins()
        def digest():
            h = m.hashlib.sha256()
            for name in m.FIXED_RUNTIME_DIGEST_FILES:
                h.update(name.encode()); h.update((self.install/'tools'/name).read_bytes())
            return h.hexdigest()
        self.runner.update(revision='b'*40, scriptDigest=digest())
        (self.asa/'collector-runner.json').write_text(json.dumps(self.runner))
        with patch.object(m, 'FIXED_RUNTIME_PROFILES', {self.runner['scriptDigest']: pins}):
            m.registered_runner(self.install, None)
            previous_second = (self.install/'tools'/second).read_bytes()
            (self.install/'tools'/first).write_bytes(b'prefix')
            (self.install/'tools'/second).write_bytes(b'suffix'+second.encode()+previous_second)
            self.assertEqual(digest(), self.runner['scriptDigest'])
            with self.assertRaisesRegex(m.Blocked, 'FIXED_FILE_MISMATCH'):
                m.registered_runner(self.install, None)

    def test_actual_fixed_profile_reads_accept_raw_and_canonical_but_deny_hybrids(self):
        first, ps1 = m.FIXED_RUNTIME_DIGEST_FILES[:2]
        original = {n:(self.install/'tools'/n).read_bytes() for n in m.FIXED_RUNTIME_DIGEST_FILES}
        original[first] = b'prefix'+ps1.encode()+b'suffix'
        original[ps1] = b'first\nsecond\n'
        canonical = {**original, ps1: b'first\r\nsecond\r\n'}
        def aggregate(values):
            h = m.hashlib.sha256()
            for n in m.FIXED_RUNTIME_DIGEST_FILES:
                h.update(n.encode()); h.update(values[n])
            return h.hexdigest()
        def write(values, registered):
            for n,v in values.items(): (self.install/'tools'/n).write_bytes(v)
            self.runner.update(revision='b'*40, scriptDigest=registered)
            (self.asa/'collector-runner.json').write_text(json.dumps(self.runner))
        profiles = {aggregate(v):{n:(len(b),m.hashlib.sha256(b).hexdigest()) for n,b in v.items()}
                    for v in (original, canonical)}
        with patch.object(m, 'FIXED_RUNTIME_PROFILES', profiles):
            for values in (original, canonical):
                selected = aggregate(values)
                write(values, selected)
                m.registered_runner(self.install, None)
                changed = {**values, ps1: bytes([values[ps1][0] ^ 1])+values[ps1][1:]}
                write(changed, selected)
                with self.assertRaisesRegex(m.Blocked, 'FIXED_FILE_MISMATCH'):
                    m.registered_runner(self.install, None)
                redistributed = {**values, first:b'prefix', ps1:b'suffix'+ps1.encode()+values[ps1]}
                self.assertEqual(aggregate(redistributed), selected)
                write(redistributed, selected)
                with self.assertRaisesRegex(m.Blocked, 'FIXED_FILE_MISMATCH'):
                    m.registered_runner(self.install, None)
            write(canonical, aggregate(original))
            with self.assertRaisesRegex(m.Blocked, 'FIXED_FILE_MISMATCH'):
                m.registered_runner(self.install, None)
            mixed = {**original, ps1:b'first\r\nsecond\n'}
            write(mixed, aggregate(mixed))
            with self.assertRaisesRegex(m.Blocked, 'REVISION_CONTRACT_UNSUPPORTED'):
                m.registered_runner(self.install, None)
            write(mixed, aggregate(original))
            with self.assertRaisesRegex(m.Blocked, 'FIXED_FILE_MISMATCH'):
                m.registered_runner(self.install, None)

    def test_user_inactive_service_timer_and_actual_lock_copy(self):
        self.quiet_patch.stop()
        identity = dict(mode='user', owner='synthetic', uid=1000, gid=1000, root=str(self.install), name=self.runner['name'], revision=self.runner['revision'])
        service = SimpleNamespace(returncode=0, stdout='LoadState=loaded\nActiveState=inactive\nMainPID=0\nWorkingDirectory='+str(self.install)+'\nUser=\n')
        timer = SimpleNamespace(returncode=0, stdout='LoadState=loaded\nActiveState=inactive\n')
        with patch.object(m, 'registered_runner', return_value=identity), patch.object(m, 'user_manager_prefix', return_value=['/usr/bin/systemctl', '--user']), patch.object(m.subprocess, 'run', side_effect=[service, timer, service, timer]):
            result = m.snapshot(self.install, self.target, apply=True)
        self.assertEqual(result['status'], 'FORENSIC_COPY_ONLY')
        self.assertFalse(result['recoverySnapshotVerified'])

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
