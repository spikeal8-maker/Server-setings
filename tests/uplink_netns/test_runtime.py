import base64
import importlib.util
import hashlib
import json
import os
from pathlib import Path
import signal
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
from school_uplink_runtime.adapters import wifi_configuration
from school_uplink_runtime.manager import Manager
from school_uplink_runtime.network import Commands, lease_from_environment, route_proof, https_probe, BACKUP_TABLE, MANAGEMENT_TABLE, PRIMARY_IP
from school_uplink_runtime.storage import DNSLease, Stop, load, save


ENV = {'interface': 'nic0', 'reason': 'BOUND', 'new_ip_address': '198.18.0.2',
       'new_subnet_mask': '255.255.255.0', 'new_routers': '198.18.0.1',
       'new_domain_name_servers': '198.18.0.1', 'new_dhcp_lease_time': '3600'}


class RuntimeNetns(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.geteuid() != 0 or os.readlink('/proc/self/ns/net') == os.readlink('/proc/1/ns/net'):
            raise RuntimeError('Tests MUST run as root inside an isolated network namespace')
        cls.directory = tempfile.TemporaryDirectory(prefix='uplink-netns-')
        cls.root = Path(cls.directory.name)
        cls.commands = Commands()
        cls.commands.ip('link', 'set', 'lo', 'up')
        cert, key = cls.root / 'cert.pem', cls.root / 'key.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                        '-subj', '/CN=one.one.one.one', '-addext', 'subjectAltName=DNS:one.one.one.one,DNS:dns.google',
                        '-keyout', str(key), '-out', str(cert)], check=True, capture_output=True)
        cls.peer = subprocess.Popen(['unshare', '--net', sys.executable, str(Path(__file__).with_name('peer.py')), str(cert), str(key)],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        assert cls.peer.stdout.readline().strip() == 'WAIT'
        for local, remote in (('vmbr0', 'primpeer'), ('nic0', 'backpeer')):
            cls.commands.ip('link', 'add', local, 'type', 'veth', 'peer', 'name', remote)
            cls.commands.ip('link', 'set', remote, 'netns', cls.peer.pid)
            cls.commands.ip('link', 'set', local, 'up')
        cls.commands.ip('addr', 'add', PRIMARY_IP + '/23', 'dev', 'vmbr0')
        cls.commands.ip('route', 'add', 'default', 'via', '172.23.104.1', 'dev', 'vmbr0')
        cls.peer.stdin.write('start\n')
        cls.peer.stdin.flush()
        assert cls.peer.stdout.readline().strip() == 'READY'
        original_context = ssl.create_default_context
        cls.tls = patch('school_uplink_runtime.network.ssl.create_default_context', side_effect=lambda cafile=None: original_context(cafile=str(cert)))
        cls.tls.start()
        cls.pin = patch('school_uplink_runtime.network.BMC_LEAF_SHA256', hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert.read_text())).hexdigest())
        cls.pin.start()

    @classmethod
    def tearDownClass(cls):
        cls.tls.stop()
        cls.pin.stop()
        cls.peer.terminate()
        cls.peer.wait(timeout=10)
        cls.directory.cleanup()

    def setUp(self):
        self.case = Path(tempfile.mkdtemp(dir=self.root))
        self.config, self.state = self.case / 'config', self.case / 'state'
        self.config.mkdir(mode=0o700)
        self.state.mkdir(mode=0o700)
        self.resolver = self.case / 'resolv.conf'
        self.original_dns = b'# exact original bytes\nnameserver 10.167.146.13\n'
        self.resolver.write_bytes(self.original_dns)
        self.resolver.chmod(0o644)
        save(self.config / 'profiles.json', {'schema': 1, 'armed': False, 'profiles': [{'mode': 'router', 'interface': 'nic0', 'qualified': False}]})
        self.manager = Manager(self.commands, self.config, self.state, self.resolver)
        self.original_routes = self.commands.query('-4', 'route', 'show', 'table', 'main')

    def lease(self, device):
        self.assertEqual(device['interface'], 'nic0')
        return lease_from_environment(ENV, 'nic0')

    def peer_link(self, value):
        self.peer.stdin.write(value + '\n')
        self.peer.stdin.flush()
        self.assertEqual(self.peer.stdout.readline().strip(), 'DONE')
        time.sleep(0.1)

    def test_real_tls_failover_failback_management_and_exact_dns_restore(self):
        self.manager.qualify('router', self.lease)
        self.assertTrue(https_probe(self.commands, '198.18.0.2', 'nic0', BACKUP_TABLE))
        self.manager.arm()
        self.peer_link('down')
        try:
            for _ in range(3):
                self.manager.tick()
            self.assertEqual(load(self.manager.journal)['phase'], 'backup')
            route_proof(self.commands, '1.1.1.1', PRIMARY_IP, 'vmbr0', MANAGEMENT_TABLE)
            route = self.commands.query('-4', 'route', 'get', '1.1.1.1')[0]
            self.assertEqual(route['dev'], 'nic0')
            self.assertEqual(self.resolver.read_bytes(), b'nameserver 198.18.0.1\n')
        finally:
            self.peer_link('up')
        record = load(self.manager.journal)
        record['changed'] -= 61
        save(self.manager.journal, record)
        for _ in range(5):
            self.manager.tick()
        self.assertEqual(load(self.manager.journal)['phase'], 'standby')
        self.assertEqual(self.resolver.read_bytes(), self.original_dns)
        self.manager.disarm()
        self.assertEqual(self.commands.query('-4', 'route', 'show', 'table', 'main'), self.original_routes)
        self.assertEqual([r['priority'] for r in self.commands.query('-4', 'rule', 'show')], [0, 32766, 32767])
        self.assertEqual(self.resolver.stat().st_mode & 0o777, 0o644)

    def test_dns_partial_crash_intent_and_foreign_cas_preservation(self):
        dns = DNSLease(self.resolver)
        original = dns.snapshot()
        planned = b'nameserver 198.18.0.1\n'
        dns.replace(original, planned)
        dns.restore({'original': original, 'planned': base64.b64encode(planned).decode()})
        self.assertEqual(self.resolver.read_bytes(), self.original_dns)
        original = dns.snapshot()
        owned = dns.replace(original, planned)
        self.resolver.write_bytes(b'nameserver 9.9.9.9\n')
        with self.assertRaisesRegex(Stop, 'CAS'):
            dns.restore({'original': original, 'owned': owned})
        self.assertEqual(self.resolver.read_bytes(), b'nameserver 9.9.9.9\n')

    def test_partial_route_transaction_recovery(self):
        pid = os.fork()
        if pid == 0:
            manager = Manager(self.commands, self.config, self.state, self.resolver)
            original = manager.remember
            def crash(forward, inverse):
                original(forward, inverse)
                if len(manager.record['undo']) == 3:
                    os._exit(77)
            manager.remember = crash
            manager.prepare({'mode': 'router', 'interface': 'nic0', 'qualified': False}, self.lease)
            os._exit(99)
        _, status = os.waitpid(pid, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 77)
        self.assertTrue(self.manager.journal.exists())
        self.manager.disarm()
        self.assertFalse(self.manager.journal.exists())
        self.assertFalse(self.commands.query('-4', 'address', 'show', 'dev', 'nic0')[0].get('addr_info'))
        self.assertEqual(self.commands.query('-4', 'route', 'show', 'table', 'main'), self.original_routes)

    def test_rogue_dhcp_rejected_without_routes_or_dns_changes(self):
        for changes in ({'interface': 'vmbr0'}, {'new_routers': '198.18.0.1;reboot'},
                        {'new_ip_address': '172.23.104.171'}, {'new_subnet_mask': '0.0.0.0'},
                        {'new_domain_name_servers': '172.23.104.1'}, {'new_dhcp_lease_time': '-1'}):
            with self.subTest(changes=changes), self.assertRaises((Stop, ValueError)):
                lease_from_environment(dict(ENV, **changes), 'nic0')
        self.assertEqual(self.resolver.read_bytes(), self.original_dns)
        self.assertEqual(self.commands.query('-4', 'route', 'show', 'table', 'main'), self.original_routes)

    def test_wifi_config_cannot_inject_directives(self):
        config = wifi_configuration('ssid"\nctrl_interface=/tmp/evil', 'password\n123')
        self.assertNotIn(b'ctrl_interface', config)
        self.assertNotIn(b'password', config)


if __name__ == '__main__':
    unittest.main()
