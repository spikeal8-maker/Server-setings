import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('mcp_security_scan', ROOT / 'security/mcp/check_scan.py')
scan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scan)


def report():
    return {'Metadata': {'OS': {'Family': 'debian', 'Name': '12.15'}}, 'Results': [
        {'Class': 'lang-pkgs', 'Type': 'gobinary', 'Target': 'usr/local/bin/' + binary,
         'Packages': [{'Name': 'stdlib', 'Version': 'v1.27.3'}]}
        for binary in ('mcp-auth-proxy', 'mcp-1panel')]}


class MCPScanAcceptance(unittest.TestCase):
    def test_supported_os_and_both_actual_binary_targets(self):
        scan.check(report(), ['mcp-auth-proxy', 'mcp-1panel'], True)

    def test_empty_scan_is_not_clean(self):
        with self.assertRaisesRegex(ValueError, 'coverage'):
            scan.check({'Results': []}, ['mcp-1panel'], False)

    def test_empty_packages_is_not_coverage(self):
        data = report()
        data['Results'][0]['Packages'] = []
        with self.assertRaisesRegex(ValueError, 'coverage'):
            scan.check(data, ['mcp-auth-proxy'], True)

    def test_eol_zero_is_rejected(self):
        data = report()
        data['Metadata']['OS']['EOSL'] = True
        with self.assertRaisesRegex(ValueError, 'EOL'):
            scan.check(data, ['mcp-1panel'], True)

    def test_undetected_os_is_rejected(self):
        data = report()
        data['Metadata'] = {}
        with self.assertRaisesRegex(ValueError, 'Debian'):
            scan.check(data, ['mcp-1panel'], True)

    def test_fixable_high_is_rejected(self):
        data = report()
        data['Results'][0]['Vulnerabilities'] = [{'Severity': 'HIGH', 'FixedVersion': '1.27.3'}]
        with self.assertRaisesRegex(ValueError, 'findings'):
            scan.check(data, ['mcp-1panel'], True)

    def test_pins_are_immutable_and_toolchain_is_fixed(self):
        pins = json.loads((ROOT / 'security/mcp/pins.json').read_text())
        for key in ('builder', 'runtime'):
            self.assertRegex(pins[key], r'@sha256:[0-9a-f]{64}$')
        self.assertEqual(pins['go_version'], 'go1.27.3')
        self.assertEqual(pins['panel_commit'], '6cd495d74010e762e0ab6ce3ead31730f8ba572a')
        self.assertEqual(pins['gateway_commit'], 'c8868adee557e793b73c7e8a3b3d37e080854c20')


if __name__ == '__main__':
    unittest.main()
