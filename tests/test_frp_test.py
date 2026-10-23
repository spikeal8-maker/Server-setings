import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('frp_test_server', Path(__file__).resolve().parents[1] / 'scripts/frp-test-server.py')
frp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(frp)


class FrpBoundaries(unittest.TestCase):
    def test_public_or_wildcard_control_is_rejected(self):
        for address in ['0.0.0.0', '94.29.44.211', '127.0.0.1', '::', 'example.com']:
            with self.assertRaises(ValueError): frp.validate_address(address)
        self.assertEqual(frp.validate_address('100.75.149.26'), '100.75.149.26')

    def test_only_overlay_control_and_loopback_proxies_are_published(self):
        c = frp.compose('100.75.149.26')['services']['frps']
        self.assertEqual(c['ports'][0], '100.75.149.26:7000:7000/tcp')
        self.assertTrue(all(p.startswith('127.0.0.1:') for p in c['ports'][1:]))
        self.assertEqual(c['user'], '65532:65532')
        self.assertTrue(c['read_only'])
        self.assertNotIn('network_mode', c)
        self.assertEqual(c['cap_drop'], ['ALL'])
        self.assertTrue(c['volumes'][0]['read_only'])

    def test_secret_creation_never_overwrites_existing_material(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'fixture'
            frp.private_write(p, 'original')
            with self.assertRaises(FileExistsError): frp.private_write(p, 'replacement')
            self.assertEqual(p.read_text(), 'original')

    def test_server_requires_client_certificate_and_file_auth(self):
        import tomllib
        c = tomllib.loads(frp.server_config())
        self.assertTrue(c['transport']['tls']['force'])
        self.assertEqual(c['transport']['tls']['trustedCaFile'], '/etc/frp/ca.crt')
        self.assertEqual(c['auth']['tokenSource']['type'], 'file')
        self.assertNotIn('token', c['auth'])
        self.assertEqual([p['single'] for p in c['allowPorts']], [14610, 14620, 14630])


if __name__ == '__main__': unittest.main()
