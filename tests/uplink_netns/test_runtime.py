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
from school_uplink_runtime.network import Commands, lease_from_environment, route_proof, https_probe, BACKUP_TABLE, MANAGEMENT_TABLE, PRIMARY_IP, addresses
from school_uplink_runtime.storage import DNSLease, Stop, load, save


ENV = {'interface': 'nic0', 'reason': 'BOUND', 'new_ip_address': '198.18.0.2',
       'new_subnet_mask': '255.255.255.0', 'new_routers': '198.18.0.1',
       'new_domain_name_servers': '198.18.0.1', 'new_dhcp_lease_time': '3600'}


class RuntimeNetns(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.geteuid() != 0 or any(not os.environ.get('ASA_TEST_HOST_' + kind.upper() + 'NS') or os.readlink('/proc/self/ns/' + kind) == os.environ['ASA_TEST_HOST_' + kind.upper() + 'NS'] for kind in ('net', 'pid', 'mnt')):
            raise RuntimeError('Tests MUST run in isolated network/PID/mount namespaces with explicit parent namespace markers')
        subprocess.run(['mount', '--make-rprivate', '/'], check=True)
        subprocess.run(['mount', '-t', 'tmpfs', '-o', 'mode=0755', 'uplink-test-private-run', '/run'], check=True)
        cls.directory = tempfile.TemporaryDirectory(prefix='uplink-netns-', dir='/run')
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
            if local == 'vmbr0':
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
        self.manager = Manager(self.commands, self.config, self.state, self.resolver, watchdog=lambda commands: True)
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
        ipv6 = {knob: Path('/proc/sys/net/ipv6/conf/nic0', knob).read_text() for knob in ('accept_ra', 'autoconf', 'disable_ipv6')}
        self.manager.qualify('router', self.lease)
        self.assertEqual(Path('/proc/sys/net/ipv6/conf/nic0/disable_ipv6').read_text().strip(), '1')
        self.assertNotEqual(self.commands.call('ip', '-6', 'addr', 'add', '2001:db8::2/64', 'dev', 'nic0', check=False).returncode, 0)
        self.assertFalse(any(r.get('dst') == 'default' for r in self.commands.query('-6', 'route', 'show', 'table', 'main')))
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
        record['changed_mono'] -= 61
        save(self.manager.journal, record)
        for _ in range(5):
            self.manager.tick()
        self.assertEqual(load(self.manager.journal)['phase'], 'standby')
        self.assertEqual(self.resolver.read_bytes(), self.original_dns)
        self.manager.disarm()
        self.assertEqual(self.commands.query('-4', 'route', 'show', 'table', 'main'), self.original_routes)
        self.assertEqual([r['priority'] for r in self.commands.query('-4', 'rule', 'show')], [0, 32766, 32767])
        self.assertEqual(self.resolver.stat().st_mode & 0o777, 0o644)
        self.assertEqual({knob: Path('/proc/sys/net/ipv6/conf/nic0', knob).read_text() for knob in ipv6}, ipv6)

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
            try:
                manager = Manager(self.commands, self.config, self.state, self.resolver, watchdog=lambda commands: True)
                original = manager.remember
                def crash(forward, inverse):
                    original(forward, inverse)
                    if len(manager.record['undo']) == 6:
                        manager.record['heartbeat'] = time.time() - 46
                        manager.record['heartbeat_mono'] = time.monotonic() - 46
                        save(manager.journal, manager.record)
                        data = manager.profiles()
                        data['armed'] = True
                        save(manager.config / 'profiles.json', data)
                        os.kill(os.getpid(), signal.SIGSTOP)
                manager.remember = crash
                manager.prepare({'mode': 'router', 'interface': 'nic0', 'qualified': False}, self.lease)
            except BaseException:
                import traceback
                traceback.print_exc()
                os._exit(78)
            os._exit(99)
        _, status = os.waitpid(pid, os.WUNTRACED)
        self.assertTrue(os.WIFSTOPPED(status))
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
        self.assertTrue(self.manager.journal.exists())
        watcher = os.fork()
        if watcher == 0:
            try:
                self.manager.watchdog()
                os._exit(0)
            except BaseException:
                import traceback
                traceback.print_exc()
                os._exit(79)
        _, status = os.waitpid(watcher, 0)
        self.assertEqual(os.waitstatus_to_exitcode(status), 0)
        self.assertFalse(self.manager.journal.exists())
        self.assertFalse(addresses(self.commands, 'nic0'))
        self.assertEqual(self.commands.query('-4', 'route', 'show', 'table', 'main'), self.original_routes)

    def test_failed_rule_delete_keeps_undo_and_journal(self):
        self.manager.prepare({'mode': 'router', 'interface': 'nic0', 'qualified': False}, self.lease)
        self.manager.activate()
        original = self.commands.call
        def refuse(*args, **kwargs):
            if args[:6] == ('ip', '-4', 'rule', 'del', 'priority', '10020'):
                raise Stop('INJECTED_RULE_DELETE_FAILURE')
            return original(*args, **kwargs)
        with patch.object(self.commands, 'call', side_effect=refuse), self.assertRaises(Stop):
            self.manager.failback()
        record = load(self.manager.journal)
        self.assertTrue(any(isinstance(entry, list) and '10020' in entry for entry in record['undo']))
        self.assertTrue(any(row['priority'] == 10020 for row in self.commands.query('-4', 'rule', 'show')))
        self.manager.disarm()

    def test_watchdog_required_before_first_side_effect(self):
        self.manager.watchdog_ready = lambda commands: False
        with self.assertRaisesRegex(Stop, 'WATCHDOG'):
            self.manager.prepare({'mode': 'router', 'interface': 'nic0', 'qualified': False}, self.lease)
        self.assertFalse(self.manager.journal.exists())
        self.assertEqual(self.commands.query('-4', 'route', 'show', 'table', 'main'), self.original_routes)

    def test_qualified_renewal_expiry_and_same_mac_usb_replug_reprobe_automatically(self):
        self.manager.qualify('router', self.lease)
        self.manager.arm()
        self.manager.activate()
        current = dict(ENV, new_ip_address='198.18.0.3', new_routers='198.18.0.254', new_domain_name_servers='198.18.0.254')
        self.manager.acquire = lambda device: lease_from_environment(current, 'nic0')
        save(self.state / 'lease-nic0.json', lease_from_environment(current, 'nic0'))
        self.manager.tick()
        self.assertEqual(load(self.manager.journal)['lease']['address'], '198.18.0.3')
        self.assertEqual(self.resolver.read_bytes(), b'nameserver 198.18.0.254\n')
        current['new_ip_address'] = '198.18.0.4'
        save(self.state / 'lease-nic0.json', {'interface': 'nic0', 'expires': 0})
        self.manager.tick()
        self.assertEqual(load(self.manager.journal)['lease']['address'], '198.18.0.4')
        mac = load(self.manager.journal)['device']['mac']
        self.commands.ip('link', 'del', 'nic0')
        self.commands.ip('link', 'add', 'nic0', 'type', 'veth', 'peer', 'name', 'backpeer')
        self.commands.ip('link', 'set', 'nic0', 'address', mac)
        self.commands.ip('link', 'set', 'backpeer', 'netns', self.peer.pid)
        self.peer.stdin.write('back-replug\n')
        self.peer.stdin.flush()
        self.assertEqual(self.peer.stdout.readline().strip(), 'DONE')
        current['new_ip_address'] = '198.18.0.5'
        self.manager.tick()
        self.assertEqual(load(self.manager.journal)['lease']['address'], '198.18.0.5')
        self.assertTrue(https_probe(self.commands, '198.18.0.5', 'nic0', BACKUP_TABLE))
        self.manager.disarm()
        self.assertEqual(self.resolver.read_bytes(), self.original_dns)

    def test_parent_chain_rejects_writable_ancestor(self):
        from school_uplink_runtime.storage import private_directory
        unsafe = self.case / 'unsafe'
        unsafe.mkdir(mode=0o777)
        unsafe.chmod(0o777)
        with self.assertRaisesRegex(Stop, 'UNTRUSTED'):
            private_directory(unsafe / 'child')
        self.assertFalse((unsafe / 'child').exists())

    def test_old_boot_reused_pid_and_foreign_executable_never_signalled(self):
        from school_uplink_runtime.adapters import boot_id, process_identity, stop_process
        record = {'pid': os.getpid(), 'start': process_identity(os.getpid()), 'boot_id': 'old-boot', 'executable': '/wrong'}
        with patch('school_uplink_runtime.adapters.signal.pidfd_send_signal') as send:
            stop_process(record)
            stop_process(dict(record, boot_id=boot_id(), start='stale-start'))
            with self.assertRaisesRegex(Stop, 'FOREIGN_PROCESS'):
                stop_process(dict(record, boot_id=boot_id()))
            send.assert_not_called()

    def test_stopped_child_barrier_pidfd_and_owned_pidfile_cleanup(self):
        from school_uplink_runtime.adapters import spawn_owned, process_identity
        records = []
        pidfile = self.state / 'synthetic.pid'
        def remember(forward, inverse):
            records.append(inverse)
            save(self.state / 'process-proof.json', inverse)
        spawn_owned(['sleep', '60'], remember, pidfile)
        record = records[0]
        pidfile.write_text(str(record['pid']))
        pidfile.chmod(0o600)
        self.manager.undo(record)
        self.assertIsNone(process_identity(record['pid']))
        self.assertFalse(pidfile.exists())

    def test_old_boot_watchdog_before_manager_preserves_armed_intent_and_resumes(self):
        self.manager.qualify('router', self.lease)
        self.manager.arm()
        old = load(self.manager.journal)
        self.manager.rollback()  # simulate kernel ownership disappearing at boot
        old['boot_id'] = 'previous-boot'
        save(self.manager.journal, old)
        self.manager.watchdog()
        self.assertTrue(self.manager.profiles()['armed'])
        self.assertFalse(self.manager.journal.exists())
        self.manager.acquire = self.lease
        self.manager.tick()
        self.assertEqual(load(self.manager.journal)['phase'], 'standby')
        self.assertTrue(self.manager.profiles()['armed'])
        self.manager.disarm()

    def test_foreign_route_attributes_preserved_and_whole_journal_retained(self):
        self.manager.prepare({'mode': 'router', 'interface': 'nic0', 'qualified': False}, self.lease)
        before = load(self.manager.journal)['undo']
        self.commands.ip('-4', 'route', 'del', 'table', BACKUP_TABLE, 'default')
        self.commands.ip('-4', 'route', 'add', 'table', BACKUP_TABLE, 'default', 'via', '198.18.0.1', 'dev', 'nic0', 'src', '198.18.0.2', 'proto', '186', 'metric', '9')
        with self.assertRaisesRegex(Stop, 'FOREIGN_ROUTE'):
            self.manager.disarm()
        self.assertEqual(load(self.manager.journal)['undo'], before)
        self.assertTrue(any(r.get('metric') == 9 for r in self.commands.query('-4', 'route', 'show', 'table', BACKUP_TABLE)))
        self.commands.ip('-4', 'route', 'del', 'table', BACKUP_TABLE, 'default', 'metric', '9')
        self.commands.ip('-4', 'route', 'add', 'table', BACKUP_TABLE, 'default', 'via', '198.18.0.1', 'dev', 'nic0', 'src', '198.18.0.2', 'proto', '186')
        self.manager.disarm()

    def test_device_name_index_mac_and_boot_all_required_for_undo(self):
        from school_uplink_runtime.adapters import boot_id
        self.manager.record = {'boot_id': boot_id(), 'device': {'interface': 'nic0', 'ifindex': 123, 'mac': '00:11:22:33:44:55'}}
        foreign = [{'ifname': 'nic0', 'ifindex': 999, 'address': '00:11:22:33:44:66'},
                   {'ifname': 'other', 'ifindex': 123, 'address': '00:11:22:33:44:77'}]
        with patch.object(self.commands, 'query', return_value=foreign):
            self.assertFalse(self.manager.owned_device())
        self.manager.record['boot_id'] = 'old-boot'
        with patch.object(self.commands, 'query') as query:
            self.assertFalse(self.manager.owned_device())
            query.assert_not_called()

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
