import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from asa_ops.admin_executor import authorize, execute, redact
from asa_ops.schemas import INPUTS, validate, tools
from asa_ops.store import OpsError, Store
from asa_ops.server import Controller


class AdminContracts(unittest.TestCase):
    def test_default_does_not_advertise_root(self):
        self.assertNotIn('asa_admin_execute', [t['name'] for t in tools()])
        item = next(t for t in tools(host_admin_enabled=True) if t['name'] == 'asa_admin_execute')
        self.assertTrue(item['annotations']['destructiveHint'])
        self.assertTrue(item['annotations']['openWorldHint'])

    def test_command_size_and_null_are_validated(self):
        args = {'installation_id': 'asa-test', 'command': 'echo '+('x'*1000), 'idempotency_key': 'test-admin-key-001'}
        validate(INPUTS['asa_admin_execute'], args)
        for bad in ['x'*32001, 'echo\0bad', '']:
            with self.assertRaises(OpsError):
                validate(INPUTS['asa_admin_execute'], dict(args, command=bad))

    def test_root_independently_rejects_wrong_peer_host_and_generation(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t); (root/'.asa').mkdir()
            (root/'.asa/installation.json').write_text(json.dumps({'instanceId': 'a'*32}))
            c = {'enabled': True, 'owner_uid': 1000, 'hostname': socket.gethostname(), 'root': t,
                 'installation_id': 'asa-test', 'installation_instance_id': 'a'*32}
            payload = {'action': 'execute', 'installation_id': 'asa-test', 'installation_instance_id': 'a'*32}
            authorize(c, payload, 1000)
            for bad, uid in [(dict(c, enabled=False), 1000), (c, 65534), (dict(c, hostname='another-host'), 1000),
                             (dict(c, installation_instance_id='b'*32), 1000)]:
                with self.assertRaises(ValueError):
                    authorize(bad, payload, uid)
            with self.assertRaises(ValueError):
                authorize(c, dict(payload, installation_instance_id='b'*32), 1000)

    def test_literal_secrets_and_bearer_removed(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t)/'secret'; p.write_text("TOKEN='sensitive-value-123'\nKEYS='{\"k\":\"other-private-value\"}'\n")
            text = redact('sensitive-value-123 other-private-value Bearer abcdef.jwt.signature', {'secret_files': [str(p)]})
            for value in ('sensitive-value-123', 'other-private-value', 'abcdef.jwt.signature'):
                self.assertNotIn(value, text)

    def test_admin_shares_update_exclusion_and_request_idempotency(self):
        with tempfile.TemporaryDirectory() as t:
            c = Controller.__new__(Controller)
            import threading
            c.lock = threading.Lock(); c.store = Store(Path(t)/'state')
            c.config = {'host_admin_enabled': True, 'installation': {'id':'asa-test', 'root':t}}
            (Path(t)/'.asa').mkdir(); (Path(t)/'.asa/installation.json').write_text(json.dumps({'instanceId':'a'*32}))
            c.launch = lambda record, action: record
            args = {'installation_id':'asa-test', 'command':'id', 'idempotency_key':'test-admin-key-001'}
            r = c.dispatch('asa_admin_execute', args)
            self.assertEqual(c.dispatch('asa_admin_execute', args)['operation']['operation_id'], r['operation']['operation_id'])
            with self.assertRaises(OpsError):
                c.store.start('asa-test', 'update', r['operation']['plan_id'], 'test-update-key-001', {})
            with self.assertRaises(OpsError):
                c.dispatch('asa_admin_execute', dict(args, command='another'))


@unittest.skipUnless(os.name == 'posix' and Path('/bin/bash').exists(), 'Linux executor tests run on Ubuntu')
class ExecutorTests(unittest.TestCase):
    def test_root_audit_prevents_replay(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t); state = root/'state'; state.mkdir(); marker = root/'marker'
            payload = {'action':'execute', 'operation_id':'a'*32, 'installation_id':'asa-test', 'installation_instance_id':'c'*32,
                       'command': 'echo once >> '+str(marker), 'cwd':t, 'timeout_seconds':5}
            result = execute({}, payload, state)
            self.assertEqual(result['exit_code'], 0)
            self.assertEqual(execute({}, payload, state)['exit_code'], 0)
            self.assertEqual(marker.read_text(), 'once\n')
            (state/('a'*32)/'state.json').write_text(json.dumps(dict(json.loads((state/('a'*32)/'state.json').read_text()), status='running')))
            self.assertEqual(execute({}, payload, state)['status'], 'running')
            self.assertEqual(marker.read_text(), 'once\n')
            with self.assertRaises(ValueError):
                execute({}, dict(payload, command='echo twice'), state)

    def test_timeout_even_when_shell_closes_stdout(self):
        with tempfile.TemporaryDirectory() as t:
            before=time.monotonic()
            result=execute({}, {'action':'execute','operation_id':'b'*32,'installation_id':'asa-test','installation_instance_id':'c'*32,
                               'command':'exec 1>&- 2>&-; sleep 30','cwd':t,'timeout_seconds':1}, Path(t))
            self.assertTrue(result['timed_out'])
            self.assertLess(time.monotonic()-before, 6)


if __name__ == '__main__':
    unittest.main()
