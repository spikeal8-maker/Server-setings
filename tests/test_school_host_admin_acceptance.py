import base64
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
import urllib.parse

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/school-mcp-host-admin-acceptance.py'
spec = importlib.util.spec_from_file_location('school_host_admin_acceptance', SCRIPT)
proof = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proof)


def rpc(value, ident=1):
    return json.dumps({'jsonrpc': '2.0', 'id': ident, 'result': value}).encode()


class ProtocolPredicates(unittest.TestCase):
    def test_plan_never_reads_credentials_or_contacts_server(self):
        with patch.object(proof, 'run', side_effect=AssertionError('must not run')), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(proof.main([]), 0)
        report = json.loads(out.getvalue())
        self.assertEqual(report['mode'], 'plan')
        self.assertFalse(report['chatgpt_connected'])

    def test_consent_302_and_303_have_exact_callback_and_state(self):
        target = proof.CALLBACK + '?' + urllib.parse.urlencode({'code': 'one-use-code', 'state': 'bound-state', 'iss': proof.ISSUER})
        for status in (302, 303):
            self.assertEqual(proof.callback_code(status, {'Location': target}, 'bound-state'), 'one-use-code')
        for status, url in [(200, target), (307, target), (303, target.replace('/asa-mcp-', '/asa-mcp-evil-')),
                            (303, target.replace('127.0.0.1:', '127.0.0.1.evil:')),
                            (303, target.replace(':61987', ':61988')), (303, target + '&state=second'),
                            (303, target.replace('bound-state', 'foreign-state')), (303, target + '#fragment'),
                            (303, target.replace(urllib.parse.quote(proof.ISSUER, safe=''), 'https%3A%2F%2Fevil.example%2F'))]:
            with self.subTest(url=url), self.assertRaises(proof.ProofError):
                proof.callback_code(status, {'Location': url}, 'bound-state')

    def test_origin_guards_never_send_password_to_external_host(self):
        for url in ['https://evil.example/.auth/login', 'https://x.asa-lab.ru.evil/.auth/login',
                    'https://x.asa-lab.ru@evil.example/', 'http://x.asa-lab.ru/.auth/login',
                    '//evil.example/.auth/login', 'https://x.asa-lab.ru:444/.auth/login']:
            with self.subTest(url=url), self.assertRaises(proof.ProofError):
                proof.origin_url(url)
        self.assertEqual(proof.origin_url('/.auth/login'), proof.ORIGIN + '/.auth/login')

    def test_json_and_sse_rpc_require_unique_matching_success(self):
        value = {'tools': []}
        for content_type, data in [('application/json', rpc(value)),
                                   ('text/event-stream; charset=utf-8', b'event: message\ndata: ' + rpc(value) + b'\n\n')]:
            self.assertEqual(proof.rpc_result(200, {'Content-Type': content_type}, data, 1), value)
        for status, content_type, data in [(404, 'text/plain', b'404 page not found'),
                                           (200, 'text/html', b'<h1>OK</h1>'),
                                           (200, 'application/json', rpc(value, 2)),
                                           (200, 'application/json', rpc(value, True)),
                                           (200, 'application/json', b'{"jsonrpc":"2.0","id":1,"error":{"message":"secret-value"}}'),
                                           (200, 'text/event-stream', b'data: ' + rpc(value) + b'\n\ndata: ' + rpc(value) + b'\n\n')]:
            with self.subTest(status=status, content_type=content_type), self.assertRaises(proof.ProofError) as caught:
                proof.rpc_result(status, {'Content-Type': content_type}, data, 1)
            self.assertNotIn('secret-value', str(caught.exception))

    def test_stateless_initialize_is_accepted_but_wrong_server_is_not(self):
        value = {'protocolVersion': proof.PROTOCOL, 'capabilities': {'tools': {}},
                 'serverInfo': {'name': 'asa-school-host-admin', 'version': '1'}}
        client = proof.PublicClient()
        with patch.object(client, 'request', return_value=(200, {'Content-Type': 'application/json'}, rpc(value))):
            proof.initialize_result(client.rpc('initialize', {}))
        self.assertIsNone(client.session)
        for bad in [dict(value, protocolVersion='old'), dict(value, capabilities={}),
                    dict(value, serverInfo={'name': 'mcp-1panel'}),
                    dict(value, serverInfo=[]), dict(value, serverInfo=None), None, []]:
            with self.assertRaises(proof.ProofError):
                proof.initialize_result(bad)

    def test_exact_tool_set_rejects_hidden_lifecycle_missing_and_duplicates(self):
        correct = {'tools': [{'name': name} for name in sorted(proof.REQUIRED_TOOLS)]}
        proof.toolset(correct)
        self.assertEqual(len(correct['tools']), 16)
        bad_values = [dict(correct, tools=correct['tools'][:-1]),
                      dict(correct, tools=correct['tools'] + [{'name': 'asa_update_start'}]),
                      dict(correct, tools=correct['tools'][:-1] + [correct['tools'][0]]),
                      dict(correct, nextCursor='more')]
        for bad in bad_values:
            with self.assertRaises(proof.ProofError):
                proof.toolset(bad)

    def test_exact_host_uid_and_instance_are_required(self):
        value = {'enabled': True, 'hostname': proof.HOST, 'installation_id': proof.INSTALLATION,
                 'installation_instance_id': 'a' * 32, 'uid': 0}
        proof.host_identity(value, 'a' * 32)
        for bad in [dict(value, hostname='admins-MS-7D22'), dict(value, uid=1000), dict(value, uid=False),
                    dict(value, installation_id='asa-test'), dict(value, installation_instance_id='b' * 32),
                    dict(value, enabled=False)]:
            with self.assertRaises(proof.ProofError):
                proof.host_identity(bad, 'a' * 32)
        inventory = {k: v for k, v in value.items() if k not in ('enabled', 'uid')}
        inventory.update(executor_uid=0, root_administration_enabled=True,
                         containers=[{'Names': 'asa-lab-school-api-1'}], containers_total=1, containers_truncated=False)
        proof.host_identity(inventory, 'a' * 32, inventory=True)
        inventory['containers'][0]['Env'] = ['PRIVATE=not-for-output']
        with self.assertRaises(proof.ProofError):
            proof.host_identity(inventory, 'a' * 32, inventory=True)

    def test_token_claims_require_exact_issuer_resource_and_expiry(self):
        def token(claims):
            body = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b'=').decode()
            return 'eyJhbGciOiJIUzI1NiJ9.' + body + '.' + 'a' * 43
        claims = {'iss': proof.ISSUER, 'aud': proof.RESOURCE, 'exp': time.time() + 300}
        proof.token_claims(token(claims))
        proof.token_claims(token(dict(claims, aud=[proof.RESOURCE])))
        for bad in [dict(claims, iss=proof.ORIGIN), dict(claims, aud=proof.ORIGIN),
                    dict(claims, aud=[proof.RESOURCE, 'other']), dict(claims, exp=time.time() - 1),
                    dict(claims, exp=True), dict(claims, exp=float('inf')), dict(claims, exp=10**500),
                    dict(claims, nbf=time.time() + 90), dict(claims, nbf=-float('inf'))]:
            with self.assertRaises(proof.ProofError):
                proof.token_claims(token(bad))

    def test_full_oauth_flow_binds_pkce_resource_and_callback_before_issuing_token(self):
        for consent_status in (302, 303):
            client = proof.PublicClient()
            state = {}
            claims = {'iss': proof.ISSUER, 'aud': proof.RESOURCE, 'exp': time.time() + 300}
            token = 'eyJhbGciOiJIUzI1NiJ9.' + base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b'=').decode() + '.' + 'a' * 43
            consent = proof.ORIGIN + '/.idp/auth/test-consent'
            def response(path, *, data=None, content_type=None, headers=None):
                parts = urllib.parse.urlsplit(path)
                if parts.path == '/.well-known/oauth-protected-resource/mcp':
                    return 200, {}, json.dumps({'resource': proof.RESOURCE, 'authorization_servers': [proof.ISSUER]}).encode()
                if parts.path == '/.well-known/oauth-authorization-server':
                    return 200, {}, json.dumps({'issuer': proof.ISSUER,
                        'authorization_endpoint': proof.ORIGIN + '/.idp/auth', 'token_endpoint': proof.ORIGIN + '/.idp/token',
                        'registration_endpoint': proof.ORIGIN + '/.idp/register', 'code_challenge_methods_supported': ['S256']}).encode()
                if parts.path == '/.idp/register':
                    registration = json.loads(data)
                    self.assertEqual(registration['redirect_uris'], [proof.CALLBACK])
                    return 201, {}, json.dumps({'client_id': 'test-client-1234', 'redirect_uris': [proof.CALLBACK]}).encode()
                if parts.path == '/.idp/auth':
                    state.update(urllib.parse.parse_qs(parts.query))
                    self.assertEqual(state['resource'], [proof.RESOURCE])
                    self.assertEqual(state['code_challenge_method'], ['S256'])
                    return 302, {'Location': consent}, b''
                if path == consent and data is None:
                    if not state.get('logged_in'):
                        return 302, {'Location': proof.ORIGIN + '/.auth/login'}, b''
                    return 200, {}, b'<form method="post">'
                if parts.path == '/.auth/login':
                    if data is None:
                        return 200, {}, b'<input name="password">'
                    self.assertEqual(urllib.parse.parse_qs(data.decode()), {'password': ['private-owner-password']})
                    state['logged_in'] = True
                    return 302, {'Location': consent}, b''
                if path == consent:
                    callback = proof.CALLBACK + '?' + urllib.parse.urlencode({'state': state['state'][0], 'code': 'one-use-code'})
                    return consent_status, {'Location': callback}, b''
                if parts.path == '/.idp/token':
                    form = urllib.parse.parse_qs(data.decode())
                    self.assertEqual(form['resource'], [proof.RESOURCE])
                    self.assertEqual(form['redirect_uri'], state['redirect_uri'])
                    self.assertEqual(form['client_id'], state['client_id'])
                    self.assertEqual(form['code'], ['one-use-code'])
                    challenge = base64.urlsafe_b64encode(hashlib.sha256(form['code_verifier'][0].encode()).digest()).rstrip(b'=').decode()
                    self.assertEqual([challenge], state['code_challenge'])
                    return 200, {}, json.dumps({'token_type': 'Bearer', 'access_token': token}).encode()
                self.fail('Unexpected OAuth route')
            with self.subTest(consent_status=consent_status), patch.object(client, 'request', side_effect=response):
                client.authenticate('private-owner-password')
            self.assertEqual(client.token, token)

    def test_wrong_discovery_identity_stops_before_registration_and_password(self):
        client = proof.PublicClient()
        with patch.object(client, 'request', return_value=(200, {}, b'{"resource":"https://evil.example/mcp"}')) as request:
            with self.assertRaises(proof.ProofError):
                client.authenticate('private-owner-password')
        self.assertEqual(request.call_count, 1)

    def test_public_client_forbids_acceptance_lifecycle_calls(self):
        client = proof.PublicClient()
        with patch.object(client, 'rpc') as call:
            with self.assertRaises(proof.ProofError):
                client.call('asa_update_plan', {'installation_id': proof.INSTALLATION})
        call.assert_not_called()

    def test_replay_must_keep_id_and_reused_true(self):
        value = {'reused': True, 'operation': {'operation_id': 'b' * 32,
                                             'installation_id': proof.INSTALLATION, 'kind': 'admin'}}
        self.assertEqual(proof.started(value, reused=True), 'b' * 32)
        for bad in [dict(value, reused=False), dict(value, operation={}),
                    dict(value, operation=dict(value['operation'], installation_id='asa-test'))]:
            with self.assertRaises(proof.ProofError):
                proof.started(bad, reused=True)

    def test_wait_does_not_accept_exit_zero_without_complete_success(self):
        operation_id = 'a' * 32
        result = {'status': 'complete', 'operation_id': operation_id, 'exit_code': 0,
                  'timed_out': False, 'output_may_be_truncated': False, 'output': '{}'}
        value = {'operation_id': operation_id, 'installation_id': proof.INSTALLATION,
                 'kind': 'admin', 'status': 'success', 'result': result,
                 'log': {'cursor': 92, 'lines': [{'at': '2026-11-07T00:00:00Z', 'phase': 'admin', 'message': 'Server worker started'}]}}
        client = proof.PublicClient()
        for bad in [dict(value, status='recovery_required'), dict(value, result=dict(result, timed_out=True)),
                    dict(value, result=dict(result, output_may_be_truncated=True)), dict(value, kind='update'),
                    dict(value, log=None), dict(value, log={'cursor': 0, 'lines': []})]:
            with patch.object(client, 'call', return_value=bad), self.assertRaises(proof.ProofError):
                proof.wait_operation(client, operation_id)

    def test_marker_command_rejects_path_or_shell_input(self):
        for action, run_id, nonce in [('erase', 'a' * 32, 'b' * 32), ('write', '../file', 'b' * 32),
                                      ('write', 'a' * 32, '$(touch bad)'), ('write', 'a' * 32, 'b' * 32 + '\n')]:
            with self.assertRaises(proof.ProofError):
                proof.marker_command(action, run_id, nonce)
        command = proof.marker_command('write', 'a' * 32, 'b' * 32)
        self.assertIn(str(proof.MARKER_ROOT), command)
        self.assertNotIn('/srv/asa-lab', command)

    def test_malformed_response_shapes_fail_without_raw_values(self):
        for value in (None, [], 'token-secret-value', 0):
            for function in (proof.initialize_result, proof.toolset, proof.tool_data):
                with self.subTest(function=function.__name__, value=type(value)), self.assertRaises(proof.ProofError) as caught:
                    function(value)
                self.assertNotIn('token-secret-value', str(caught.exception))
            with self.assertRaises(proof.ProofError):
                proof.host_identity(value, 'a' * 32)
            with self.assertRaises(proof.ProofError):
                proof.started(value, reused=False)

    def test_prove_root_reuses_exact_request_and_runs_independent_read_then_cleanup(self):
        run_id, nonce = 'a' * 32, 'b' * 32
        ids = {action: digit * 32 for action, digit in [('write', '1'), ('read', '2'), ('cleanup', '3')]}
        def operation(action):
            return {'operation_id': ids[action], 'installation_id': proof.INSTALLATION,
                    'kind': 'admin', 'status': 'success'}
        client = Mock()
        client.call.side_effect = [
            {'operation': operation('write'), 'reused': False},
            {'operation': operation('write'), 'reused': True},
            {'operation': operation('read'), 'reused': False},
            {'operation': operation('cleanup'), 'reused': False},
            {'operations': [operation(action) for action in ids]},
        ]
        def witness(action):
            return {'output': json.dumps({'action': action, 'hostname': proof.HOST, 'uid': 0, 'mode': '0600',
                                          'sha256': hashlib.sha256((nonce+'\n').encode()).hexdigest(),
                                          'line_count': 1, 'removed': action == 'cleanup'})}
        with patch.object(proof, 'wait_operation', side_effect=[witness(action) for action in ('write', 'write', 'read', 'cleanup')]):
            self.assertEqual(proof.prove_root(client, run_id, nonce), ids)
        calls = client.call.call_args_list
        self.assertEqual(calls[0], calls[1])
        self.assertEqual([call.args[0] for call in calls], ['asa_admin_execute'] * 4 + ['asa_operation_list'])
        self.assertEqual([call.args[1]['idempotency_key'].rsplit('-', 1)[1] for call in calls[:4]],
                         ['write', 'write', 'read', 'cleanup'])

    def test_uncertain_transport_never_retries_root_command(self):
        client = Mock()
        client.call.side_effect = OSError('private-token-must-not-be-printed')
        with self.assertRaises(OSError):
            proof.prove_root(client, 'a' * 32, 'b' * 32)
        self.assertEqual(client.call.call_count, 1)

    def test_failure_output_never_contains_transport_exception_or_connects_chatgpt(self):
        with patch.object(proof, 'run', side_effect=OSError('private-token')), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(proof.main(['--apply']), 2)
        report = json.loads(err.getvalue())
        self.assertEqual(report['code'], 'OSError')
        self.assertFalse(report['chatgpt_connected'])
        self.assertFalse(report['automatic_command_retry'])
        self.assertNotIn('private-token', err.getvalue())


@unittest.skipUnless(os.name == 'posix' and os.geteuid() == 0 and Path('/root').is_dir(), 'Root-only marker witness test in isolated local fixture')
class MarkerWitness(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='asa-mcp-marker-unit-', dir='/root')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name) / 'acceptance'
        self.run_id, self.nonce = 'a' * 32, 'b' * 32
        self.marker = self.base / ('root-proof-' + self.run_id + '.txt')
        self.addCleanup(patch.stopall)
        patch.object(proof, 'MARKER_ROOT', self.base).start()
        patch.object(proof, 'HOST', socket.gethostname()).start()

    def command(self, action):
        return subprocess.run(['/bin/bash', '--noprofile', '--norc', '-c', proof.marker_command(action, self.run_id, self.nonce)],
                              capture_output=True, text=True, timeout=5)

    def test_root_marker_read_cleanup_and_mode(self):
        for action in ('write', 'read', 'cleanup'):
            result = self.command(action)
            self.assertEqual(result.returncode, 0, result.stderr)
            proof.marker_result({'output': result.stdout}, action, self.nonce)
        self.assertFalse(self.marker.exists())
        self.assertTrue(self.base.is_dir())

    def test_real_second_execution_is_detected_by_independent_read(self):
        self.assertEqual(self.command('write').returncode, 0)
        second = self.command('write')
        self.assertEqual(second.returncode, 0)
        with self.assertRaises(proof.ProofError):
            proof.marker_result({'output': second.stdout}, 'write', self.nonce)
        self.assertEqual(self.marker.read_text(), (self.nonce + '\n') * 2)
        self.assertNotEqual(self.command('read').returncode, 0)
        self.assertNotEqual(self.command('cleanup').returncode, 0)

    def test_symlink_marker_never_writes_target(self):
        self.base.mkdir(mode=0o700)
        target = Path(self.temp.name) / 'untouched'
        target.write_text('keep\n')
        self.marker.symlink_to(target)
        self.assertNotEqual(self.command('write').returncode, 0)
        self.assertEqual(target.read_text(), 'keep\n')

    def test_owner_password_requires_root_private_regular_file(self):
        password = Path(self.temp.name) / 'password'
        password.write_text('private-owner-password')
        password.chmod(0o600)
        self.assertEqual(proof.private_file(password, exact_mode=0o600), 'private-owner-password')
        password.chmod(0o640)
        with self.assertRaises(proof.ProofError):
            proof.private_file(password, exact_mode=0o600)
        password.chmod(0o600)
        link = Path(self.temp.name) / 'password-link'
        link.symlink_to(password)
        with self.assertRaises(proof.ProofError):
            proof.private_file(link, exact_mode=0o600)


if __name__ == '__main__':
    unittest.main()
