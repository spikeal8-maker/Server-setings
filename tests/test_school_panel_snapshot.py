"""Restore coverage for optional 1Panel management, with old archive compatibility."""
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_school_managed_config_snapshot import snapshot, fixture, make_tar, ARCHIVE


class PanelSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.manifest, self.payloads = fixture(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def add_paths(self, paths):
        directories = {row['path'] for row in self.manifest['directories']}
        for path in paths:
            member = 'files/' + str(path).lstrip('/')
            data = ('synthetic:' + path.name).encode()
            self.payloads[member] = data
            self.manifest['entries'].append({'member': member, 'kind': 'config', 'bytes': len(data),
                'sha256': hashlib.sha256(data).hexdigest(),
                'sourceMetadata': {'uid': 0, 'gid': 0, 'mode': 0o600, 'bytes': len(data), 'mtimeNs': 1}})
            for parent in path.parents:
                if parent == Path('/') or str(parent) in directories:
                    continue
                directories.add(str(parent))
                self.manifest['directories'].append({'path': str(parent), 'metadata':
                    {'uid': 0, 'gid': 0, 'mode': 0o700, 'bytes': 0, 'mtimeNs': 1}})

    def test_pre_panel_archive_still_verifies(self):
        self.assertEqual(len(snapshot.validate_manifest(self.manifest, ARCHIVE)), len(snapshot.STATIC) + 5)

    def test_complete_panel_configuration_restores_only_in_isolated_directory(self):
        release = snapshot.SCHEDULE_RELEASES / ('a' * 16) / 'school-1panel-backup-schedules.py'
        added = [snapshot.SUPERVISOR_CONFIG, *snapshot.PANEL_COLLECTOR, *snapshot.PANEL_SCHEDULES,
                 *snapshot.PANEL_OPTIONAL, release]
        self.add_paths(added)
        archive = self.root / 'new.tar'
        output = self.root / 'restore_test'
        output.mkdir(mode=0o700)
        make_tar(archive, self.manifest, self.payloads)
        result = snapshot.verify_tar(archive, output, ARCHIVE)
        self.assertFalse(result['livePathsWritten'])
        self.assertFalse(result['coverageComplete'])
        for path in added:
            self.assertTrue((output / 'files' / str(path).lstrip('/')).is_file())

    def test_partial_collector_group_cannot_pass_restore_validation(self):
        self.add_paths([snapshot.PANEL_COLLECTOR[0], snapshot.SUPERVISOR_CONFIG])
        with self.assertRaisesRegex(snapshot.SnapshotError, 'MANIFEST_REQUIRED_SCOPE'):
            snapshot.validate_manifest(self.manifest, ARCHIVE)

    def test_schedule_reference_requires_archived_immutable_wrapper(self):
        self.add_paths([snapshot.SUPERVISOR_CONFIG, *snapshot.PANEL_SCHEDULES])
        with self.assertRaisesRegex(snapshot.SnapshotError, 'MANIFEST_PANEL_RELEASE_MISSING'):
            snapshot.validate_manifest(self.manifest, ARCHIVE)

    def test_raw_logs_private_index_and_unbounded_release_paths_are_rejected(self):
        for path in ('/srv/asa-lab/.asa/diagnostics/private/index.sqlite',
                     '/opt/1panel/tools/supervisord/log/asa-school-nightly-settings-backup.out.log',
                     '/opt/asa-school-panel-schedules/releases/latest/school-1panel-backup-schedules.py',
                     '/opt/asa-school-panel-schedules/releases/' + 'a' * 16 + '/private.env'):
            self.assertFalse(snapshot.allowed_config(Path(path)))

    def test_partial_installation_fails_before_snapshot_instead_of_silent_omission(self):
        present = {snapshot.PANEL_COLLECTOR[0]}
        with patch.object(Path, 'exists', lambda path: path in present), patch.object(Path, 'is_symlink', return_value=False), \
                self.assertRaisesRegex(snapshot.SnapshotError, 'PANEL_CONFIGURATION_INCOMPLETE'):
            snapshot.panel_configuration_paths()


if __name__ == '__main__':
    unittest.main()
