"""Offline fresh-native config contracts; no server, key, cloud or native calls."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from test_school_managed_config_snapshot import snapshot, fixture, make_tar, ARCHIVE


CODE = Path(__file__).resolve().parents[1] / 'scripts'
spec = importlib.util.spec_from_file_location('fresh_snapshot_cron_contract', CODE / 'school-1panel-backup-schedules.py')
cron = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cron)


class FreshPanelSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.manifest, self.payloads = fixture(self.root)
        self.remove(snapshot.LEGACY_PATHS)
        self.settings = {'project': snapshot.PROJECT, 'instanceId': snapshot.INSTANCE,
                         'collectorMode': 'system-root', 'collectorManager': '1panel-supervisor'}
        self.runner = {'root': '/srv/asa-lab', 'project': snapshot.PROJECT, 'mode': 'system-root', 'owner': 'root',
                       'name': snapshot.LEGACY_COLLECTOR[0].name, 'manager': '1panel-supervisor',
                       'revision': 'a' * 40, 'scriptDigest': 'b' * 64}
        self.logs = {'fileSources': [{'source': 'linux:1panel-supervisor',
                                      'path': str(snapshot.SUPERVISOR_ROOT / 'log')}]}
        self.receipt = {'format': 'asa-1panel-handover-v1', 'state': 'accepted', 'project': snapshot.PROJECT,
                        'instanceId': snapshot.INSTANCE, 'legacyState': {'LoadState': 'not-found'},
                        'targetSettings': self.settings, 'targetRunner': self.runner,
                        'targetCollectorConfiguration': self.logs}
        self.config = {'format': 'asa-1panel-collector-v1', 'root': '/srv/asa-lab', 'project': snapshot.PROJECT,
                       'instanceId': snapshot.INSTANCE, 'panelHost': 'http://127.0.0.1:19192',
                       'panelKeyFile': '/var/lib/server-settings/1panel-api.key', 'panelVersion': 'v2.3.2',
                       'python': '/usr/bin/python3', 'program': 'asa-log-collector-' + snapshot.PROJECT}
        for path, value in ((Path('/srv/asa-lab/.asa/installation.json'), self.settings),
                            (Path('/srv/asa-lab/.asa/collector-runner.json'), self.runner),
                            (Path('/srv/asa-lab/.asa/log-collector.json'), self.logs),
                            (snapshot.PANEL_COLLECTOR[0], self.config), (snapshot.PANEL_OPTIONAL[1], self.receipt)):
            self.add(path, json.dumps(value).encode())
        self.add(snapshot.PANEL_COLLECTOR[1], (CODE / 'asa-1panel-collector.py').read_bytes())
        self.add(snapshot.SUPERVISOR_CONFIG,
                 ('[supervisord]\n[include]\nfiles=' + str(snapshot.SUPERVISOR_ROOT / 'supervisor.d/*.ini') + '\n').encode())
        # 1Panel v2.3.2 native create/update fields; not a custom Supervisor unit.
        values = {'command': '/usr/local/sbin/asa-1panel-collector run --root /srv/asa-lab --project ' + snapshot.PROJECT,
                  'directory': '/srv/asa-lab', 'autorestart': 'true', 'autostart': 'true', 'startsecs': '3',
                  'user': 'root', 'priority': '999', 'numprocs': '1',
                  'process_name': '%(program_name)s_%(process_num)02d', 'environment': ''}
        for stream, short in (('stdout', 'out'), ('stderr', 'err')):
            values[stream + '_logfile'] = str(snapshot.SUPERVISOR_ROOT / 'log' / (self.config['program'] + '.' + short + '.log'))
            values[stream + '_logfile_maxbytes'] = '2MB'
        self.add(snapshot.PANEL_COLLECTOR[2], ('[program:' + self.config['program'] + ']\n' +
                 ''.join(k + '=' + v + '\n' for k, v in values.items())).encode())
        wrapper = (CODE / 'school-1panel-backup-schedules.py').read_bytes()
        digest = hashlib.sha256(wrapper).hexdigest()
        self.source = snapshot.SCHEDULE_RELEASES / digest[:16] / 'school-1panel-backup-schedules.py'
        self.state = {'format': cron.FORMAT, 'instance': snapshot.INSTANCE, 'project': snapshot.PROJECT,
                      'phase': 'ACTIVE', 'owner': '1panel', 'originalOwner': 'none', 'clockTimeZone': 'Europe/Moscow',
                      'generation': 'c' * 32, 'programSha256': digest, 'programPath': str(self.source),
                      'legacy': {kind: {'timer': None, 'service': None} for kind in cron.KINDS},
                      'jobs': {kind: {'id': index, 'processConfigured': True,
                          'probe': {'status': 'NATIVE_GATED_PROBE_PASS', 'backupExecuted': False,
                                    'programSha256': digest, 'dispatchId': str(index) * 32}}
                               for index, kind in enumerate(cron.KINDS, 1)}}
        self.add(snapshot.PANEL_SCHEDULES[0], json.dumps(self.state).encode())
        self.add(self.source, wrapper)
        # Cross-check against the existing canonical pure generator, never archived code.
        for kind, path in zip(cron.KINDS, snapshot.PANEL_SCHEDULES[1:]):
            self.add(path, cron.process_config(self.state, kind).encode())

    def tearDown(self):
        self.temporary.cleanup()

    def remove(self, paths):
        members = {'files/' + str(path).lstrip('/') for path in paths}
        self.manifest['entries'] = [row for row in self.manifest['entries'] if row['member'] not in members]
        for member in members:
            self.payloads.pop(member, None)

    def add(self, path, data):
        self.remove((path,))
        member = 'files/' + str(path).lstrip('/')
        self.payloads[member] = data
        self.manifest['entries'].append({'member': member, 'kind': 'config', 'bytes': len(data),
            'sha256': hashlib.sha256(data).hexdigest(),
            'sourceMetadata': {'uid': 0, 'gid': 0, 'mode': 0o600, 'bytes': len(data), 'mtimeNs': 1}})
        known = {row['path'] for row in self.manifest['directories']}
        for parent in path.parents:
            if parent != Path('/') and str(parent) not in known:
                self.manifest['directories'].append({'path': str(parent), 'metadata':
                    {'uid': 0, 'gid': 0, 'mode': 0o700, 'bytes': 0, 'mtimeNs': 1}})

    def captured(self):
        return {Path('/' + member[6:]): raw for member, raw in self.payloads.items() if member.startswith('files/')}

    def check(self):
        captured = self.captured()
        snapshot.native_ownership_for_omissions(captured, captured.__getitem__)

    def test_complete_fresh_native_archive_passes_isolated_restore(self):
        self.check()
        archive, destination = self.root / 'native.tar', self.root / 'restored'
        destination.mkdir(mode=0o700)
        make_tar(archive, self.manifest, self.payloads)
        result = snapshot.verify_tar(archive, destination, ARCHIVE)
        self.assertTrue(result['manifestVerified'])
        self.assertFalse(result['livePathsWritten'])
        self.assertFalse(result['coverageComplete'])
        for path in snapshot.LEGACY_PATHS:
            self.assertFalse((destination / 'files' / str(path).lstrip('/')).exists())

    def test_partial_legacy_database_pair_is_always_rejected(self):
        self.add(snapshot.LEGACY_DATABASE[0], b'existing service only')
        with self.assertRaisesRegex(snapshot.SnapshotError, 'PARTIAL_LEGACY_CONFIGURATION'):
            snapshot.validate_manifest(self.manifest, ARCHIVE)
        with self.assertRaisesRegex(snapshot.SnapshotError, 'PARTIAL_LEGACY_CONFIGURATION'):
            self.check()

    def test_missing_legacy_without_full_corresponding_native_group_is_rejected(self):
        for absent in (snapshot.PANEL_COLLECTOR[2], snapshot.PANEL_OPTIONAL[1], snapshot.PANEL_SCHEDULES[2]):
            with self.subTest(absent=absent):
                captured = self.captured()
                del captured[absent]
                with self.assertRaisesRegex(snapshot.SnapshotError, 'NATIVE_OWNERSHIP_SCOPE_REQUIRED'):
                    snapshot.native_ownership_for_omissions(captured, captured.__getitem__)

    def test_staged_or_foreign_schedule_ownership_cannot_authorize_omission(self):
        for change in ({'phase': 'STAGED'}, {'owner': 'legacy'}, {'originalOwner': 'legacy'},
                       {'instance': 'd' * 32}, {'programPath': '/tmp/foreign.py'}, {'generation': 'invalid'}):
            with self.subTest(change=change):
                self.add(snapshot.PANEL_SCHEDULES[0], json.dumps(self.state | change).encode())
                with self.assertRaisesRegex(snapshot.SnapshotError, 'NATIVE_SCHEDULE_OWNERSHIP'):
                    self.check()

    def test_offline_restore_rechecks_state_content_not_only_manifest_paths(self):
        self.add(snapshot.PANEL_SCHEDULES[0], json.dumps(self.state | {'phase': 'STAGED'}).encode())
        snapshot.validate_manifest(self.manifest, ARCHIVE)
        archive, destination = self.root / 'staged.tar', self.root / 'staged-restore'
        destination.mkdir(mode=0o700)
        make_tar(archive, self.manifest, self.payloads)
        with self.assertRaisesRegex(snapshot.SnapshotError, 'NATIVE_SCHEDULE_OWNERSHIP'):
            snapshot.verify_tar(archive, destination, ARCHIVE)

    def test_unaccepted_or_historic_legacy_collector_receipt_cannot_authorize_omission(self):
        for change in ({'state': 'prepared'}, {'instanceId': 'd' * 32},
                       {'legacyState': {'LoadState': 'loaded'}}, {'targetRunner': {}}):
            with self.subTest(change=change):
                self.add(snapshot.PANEL_OPTIONAL[1], json.dumps(self.receipt | change).encode())
                with self.assertRaisesRegex(snapshot.SnapshotError, 'NATIVE_COLLECTOR_OWNERSHIP'):
                    self.check()

    def test_collector_manager_and_exact_program_configuration_are_required(self):
        captured = self.captured()
        for path, raw in ((Path('/srv/asa-lab/.asa/installation.json'), json.dumps(self.settings | {'collectorManager': None}).encode()),
                          (snapshot.PANEL_COLLECTOR[2], captured[snapshot.PANEL_COLLECTOR[2]].replace(b'autostart=true', b'autostart=false')),
                          (snapshot.SUPERVISOR_CONFIG, b'[include]\nfiles=/tmp/*.ini\n')):
            with self.subTest(path=path):
                changed = captured | {path: raw}
                with self.assertRaises(snapshot.SnapshotError):
                    snapshot.native_ownership_for_omissions(changed, changed.__getitem__)

    def test_wrapper_digest_native_process_and_probe_identity_are_bound(self):
        captured = self.captured()
        duplicate = copy.deepcopy(self.state)
        duplicate['jobs']['settings']['probe']['dispatchId'] = duplicate['jobs']['database']['probe']['dispatchId']
        for path, raw in ((self.source, captured[self.source] + b'\n'),
                          (snapshot.PANEL_COLLECTOR[1], b'foreign bridge'),
                          (snapshot.PANEL_SCHEDULES[1], captured[snapshot.PANEL_SCHEDULES[1]].replace(b'autorestart=false', b'autorestart=true')),
                          (snapshot.PANEL_SCHEDULES[0], json.dumps(duplicate).encode())):
            with self.subTest(path=path):
                changed = captured | {path: raw}
                with self.assertRaises(snapshot.SnapshotError):
                    snapshot.native_ownership_for_omissions(changed, changed.__getitem__)

    def test_legacy_file_appearance_during_snapshot_is_not_silent(self):
        path = self.root / 'absent-legacy.service'
        snapshot.unchanged({str(path): None}, {})
        path.write_bytes(b'unexpected new unit')
        with self.assertRaisesRegex(snapshot.SnapshotError, 'CONFIG_SET_CHANGED_DURING_SNAPSHOT'):
            snapshot.unchanged({str(path): None}, {})

    def test_new_release_pins_match_all_exact_dependency_bytes_and_keep_old_legacy_pins(self):
        spec = importlib.util.spec_from_file_location('fresh_settings_installer_contract', CODE / 'install-school-nightly-settings-backup.py')
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        actual = {name: hashlib.sha256((CODE / name).read_bytes()).hexdigest() for name in installer.FILES}
        self.assertEqual(actual, installer.FILES)
        self.assertEqual(actual, cron.SETTINGS_FILES)
        release = hashlib.sha256(json.dumps(actual, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        self.assertEqual(installer.RELEASE_HASH, release)
        self.assertIn(hashlib.sha256(installer.SERVICE_BODY.encode()).hexdigest(), cron.LEGACY_PINS['settings']['service'])
        self.assertIn('f213e01a07157b2cf4d46a1e7bd776367539aa4f046a67626eddb556cc128c2c', cron.LEGACY_PINS['settings']['service'])
        self.assertIn('aeb07a2d2d99cced1ce48db5801eb615643324a896626a224dafef7840410105', cron.LEGACY_PINS['settings']['service'])


if __name__ == '__main__':
    unittest.main()
