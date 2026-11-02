import importlib.util
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('uplink_installer', ROOT / 'scripts/install-school-uplink-runtime.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)
spec = importlib.util.spec_from_file_location('uplink_offline', ROOT / 'scripts/school-internet-offline.py')
offline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(offline)


class InstallerContract(unittest.TestCase):
    def test_wrapper_uses_isolated_python_and_fixed_version_path(self):
        value = installer.wrapper(PurePosixPath('/opt/reviewed/scripts/school-internet-dispatch.py'))
        self.assertEqual(value, b'#!/bin/sh\nexec /usr/bin/python3 -I /opt/reviewed/scripts/school-internet-dispatch.py "$@"\n')

    def test_foreign_systemd_dropin_rejected_before_activation(self):
        with patch.object(installer.subprocess, 'run', return_value=SimpleNamespace(stdout='/etc/systemd/system/foreign.conf\n')), self.assertRaisesRegex(RuntimeError, 'DROPINS'):
            installer.unit_preflight('asa-school-uplink-watchdog.timer')

    def test_alternate_systemd_fragment_rejected(self):
        with patch.object(installer.subprocess, 'run', side_effect=[SimpleNamespace(stdout=''), SimpleNamespace(stdout='/usr/lib/systemd/system/foreign.service')]), self.assertRaisesRegex(RuntimeError, 'FRAGMENT'):
            installer.unit_preflight('asa-school-uplink-watchdog.service')

    def test_installed_regdb_pin_requires_no_apt_or_network_changes(self):
        observer = Mock()
        with patch.object(installer.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='installed|' + installer.REGDB_VERSION)) as execute:
            installer.regdb(observer)
        self.assertEqual(execute.call_count, 1)
        observer.network_snapshot.assert_not_called()

    def test_dpkg_unknown_empty_version_is_not_installed(self):
        with patch.object(installer.subprocess, 'run', side_effect=[SimpleNamespace(returncode=0, stdout='not-installed|'), SimpleNamespace(stdout='Inst foreign (1 Debian)\n')]) as execute, self.assertRaisesRegex(RuntimeError, 'PLAN_CHANGED'):
            installer.regdb(Mock())
        self.assertIn('-s', execute.call_args.args[0])

    def test_regdb_wrong_installed_version_fails_closed(self):
        with patch.object(installer.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='installed|old')), self.assertRaisesRegex(RuntimeError, 'VERSION'):
            installer.regdb(Mock())

    def test_modem_apn_optional_but_pin_and_injection_forbidden(self):
        offline.validate({'mode': 'modem', 'apn': '', 'qualified': False})
        for value in ({'mode': 'modem', 'apn': 'x;command', 'qualified': False}, {'mode': 'modem', 'apn': '', 'qualified': False, 'pin': '1234'}):
            with self.assertRaises(offline.Stop):
                offline.validate(value)


if __name__ == '__main__':
    unittest.main()
