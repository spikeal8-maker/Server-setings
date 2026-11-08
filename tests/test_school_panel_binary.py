"""Portable syscall-boundary cases; no host binary, API, root or writes needed."""
from contextlib import contextmanager
import copy
import importlib.util
from pathlib import Path
import stat
from types import SimpleNamespace
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1] / 'scripts/school-1panel-backup-schedules.py'
spec = importlib.util.spec_from_file_location('school_panel_binary_under_test', SOURCE)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def metadata(mode, inode, *, uid=0, gid=0, links=1):
    return SimpleNamespace(st_mode=mode, st_uid=uid, st_gid=gid, st_nlink=links,
                           st_dev=2049, st_ino=inode, st_size=1,
                           st_mtime_ns=100, st_ctime_ns=100)


class PanelBinaryTrustTests(unittest.TestCase):
    @contextmanager
    def boundary(self, selected='/usr/bin/1panel'):
        alias, target = m.PANEL_BINARY_ALIAS, m.PANEL_BINARY_TARGET
        files = {p: metadata(stat.S_IFDIR | 0o755, index + 1, links=2)
                 for index, p in enumerate(sorted(set(alias.parents) | set(target.parents)))}
        files[alias] = metadata(stat.S_IFLNK | 0o777, 100)
        files[target] = metadata(stat.S_IFREG | 0o700, 101)
        files[Path('/usr/local/bin/1panel')] = metadata(stat.S_IFREG | 0o700, 102)
        value = SimpleNamespace(files=files, resolved=target, linkText=str(target),
                                lstatHook=None, linkHook=None, lstatCalls={}, linkCalls=0)

        def lstat(path):
            path = Path(path)
            value.lstatCalls[path] = value.lstatCalls.get(path, 0) + 1
            info = value.files[path]
            return value.lstatHook(path, info, value.lstatCalls[path]) if value.lstatHook else info

        def readlink(path):
            self.assertEqual(Path(path), alias)
            value.linkCalls += 1
            return value.linkHook(value.linkCalls) if value.linkHook else value.linkText

        def resolve(path, strict=False):
            self.assertEqual(Path(path), alias)
            self.assertTrue(strict)
            return value.resolved

        status = {'config': {'isExist': True, 'ctlExist': True, 'status': 'running',
                            'init': False, 'configPath': '/etc/supervisor/supervisord.conf'}}
        panel = m.Panel.__new__(m.Panel)
        panel.request = mock.Mock(return_value=status)
        value.panel = panel
        with mock.patch.object(m.shutil, 'which', return_value=selected), \
                mock.patch.object(Path, 'lstat', autospec=True, side_effect=lstat), \
                mock.patch.object(Path, 'resolve', autospec=True, side_effect=resolve), \
                mock.patch.object(m.os, 'readlink', side_effect=readlink), \
                mock.patch.object(m, 'command', return_value='1Panel version v2.3.2') as command:
            value.command = command
            yield value

    def reject(self, fixture, code):
        with self.assertRaisesRegex(m.ScheduleError, '^' + code + '$'):
            fixture.panel.prerequisite()
        fixture.command.assert_not_called()
        fixture.panel.request.assert_not_called()

    def test_supported_root_alias_executes_exact_core_and_keeps_general_trusted_strict(self):
        with self.boundary() as fixture:
            with self.assertRaisesRegex(m.ScheduleError, '^PATH_UNTRUSTED$'):
                m.trusted(m.PANEL_BINARY_ALIAS)
            fixture.panel.prerequisite()
            fixture.command.assert_called_once_with(['/usr/local/bin/1panel-core', 'version'], 8)
            fixture.panel.request.assert_called_once_with('/hosts/tool/status', {'type': 'supervisord'})

    def test_existing_ordinary_trusted_binary_keeps_its_path(self):
        with self.boundary('/usr/local/bin/1panel') as fixture:
            fixture.panel.prerequisite()
            fixture.command.assert_called_once_with(['/usr/local/bin/1panel', 'version'], 8)
            self.assertEqual(fixture.linkCalls, 0)

    def test_missing_or_unrecognized_alias_does_not_execute(self):
        with self.boundary(None) as fixture:
            self.reject(fixture, 'PANEL_BINARY_MISSING')
        with self.boundary('/usr/local/bin/1panel') as fixture:
            fixture.files[Path('/usr/local/bin/1panel')] = metadata(stat.S_IFLNK | 0o777, 103)
            self.reject(fixture, 'PANEL_BINARY_ALIAS_UNSUPPORTED')

    def test_nonroot_or_hardlinked_alias_does_not_execute(self):
        for change in ({'st_uid': 1000}, {'st_nlink': 2}):
            with self.subTest(change=change), self.boundary() as fixture:
                fixture.files[m.PANEL_BINARY_ALIAS].__dict__.update(change)
                self.reject(fixture, 'PANEL_BINARY_ALIAS_UNTRUSTED')

    def test_wrong_link_text_or_resolved_target_does_not_execute(self):
        for text in ('../local/bin/1panel-core', '/tmp/1panel-core', '/usr/local/bin/another-core'):
            with self.subTest(linkText=text), self.boundary() as fixture:
                fixture.linkText = text
                self.reject(fixture, 'PANEL_BINARY_ALIAS_TARGET')
        with self.boundary() as fixture:
            fixture.resolved = Path('/usr/local/bin/another-core')
            self.reject(fixture, 'PANEL_BINARY_ALIAS_TARGET')

    def test_alias_and_target_parents_must_remain_ordinary_root_nonwritable(self):
        for path, change in ((Path('/usr/bin'), {'st_mode': stat.S_IFDIR | 0o777}),
                             (Path('/usr/bin'), {'st_uid': 1000}),
                             (Path('/usr/bin'), {'st_mode': stat.S_IFLNK | 0o777}),
                             (Path('/usr/local/bin'), {'st_mode': stat.S_IFDIR | 0o775})):
            with self.subTest(path=path, change=change), self.boundary() as fixture:
                fixture.files[path].__dict__.update(change)
                self.reject(fixture, 'PATH_UNTRUSTED')

    def test_target_must_be_root_ordinary_single_link_nonwritable_and_executable(self):
        for change, code in (({'st_uid': 1000}, 'PATH_UNTRUSTED'),
                             ({'st_mode': stat.S_IFREG | 0o770}, 'PATH_UNTRUSTED'),
                             ({'st_mode': stat.S_IFLNK | 0o777}, 'PATH_UNTRUSTED'),
                             ({'st_mode': stat.S_IFDIR | 0o700}, 'PATH_TYPE'),
                             ({'st_nlink': 2}, 'PATH_TYPE'),
                             ({'st_mode': stat.S_IFREG | 0o600}, 'PANEL_BINARY_NOT_EXECUTABLE')):
            with self.subTest(change=change), self.boundary() as fixture:
                fixture.files[m.PANEL_BINARY_TARGET].__dict__.update(change)
                self.reject(fixture, code)

    def test_alias_inode_or_link_text_change_before_execution_stops(self):
        def changed(path, info, count):
            if path == m.PANEL_BINARY_ALIAS and count > 1:
                result = copy.copy(info)
                result.st_ino += 1
                return result
            return info

        with self.boundary() as fixture:
            fixture.lstatHook = changed
            self.reject(fixture, 'PANEL_BINARY_ALIAS_CHANGED')
        with self.boundary() as fixture:
            fixture.linkHook = lambda count: fixture.linkText if count == 1 else '/tmp/replaced-core'
            self.reject(fixture, 'PANEL_BINARY_ALIAS_CHANGED')

    def test_unexpected_version_still_blocks_native_status(self):
        with self.boundary() as fixture:
            fixture.command.return_value = '1Panel version v2.3.1'
            with self.assertRaisesRegex(m.ScheduleError, '^PANEL_VERSION_CHANGED$'):
                fixture.panel.prerequisite()
            fixture.panel.request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
