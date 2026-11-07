#!/usr/bin/env python3
"""Public HTTPS/OAuth acceptance of the explicit VM100 host-admin-only profile.

Plan by default. --apply registers a test OAuth client, uses the VM100 owner
password in memory, and performs three audited root operations: append an own
marker, independently read it after an idempotent replay, and remove it. ASA
data, configuration and lifecycle tools are never touched. This is protocol
acceptance, not evidence that the owner's ChatGPT plugin is connected.

OAuth flow is adapted from school-mcp-oauth-e2e-acceptance.py. It accepts the
gateway's POST-consent 302/303 and both stateless JSON and stateful SSE MCP.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import http.cookiejar
import json
import os
from pathlib import Path
import re
import secrets
import socket
import ssl
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ORIGIN = 'https://x.asa-lab.ru'
ISSUER = ORIGIN + '/'
RESOURCE = ORIGIN + '/mcp'
HOST = 'asa-school-01'
INSTALLATION = 'asa-school-vm100'
CALLBACK = 'http://127.0.0.1:61987/asa-mcp-host-admin-proof'
PASSWORD = Path('/var/lib/server-settings/asa-school-mcp-owner-password')
IDENTITY = Path('/srv/asa-lab/.asa/installation.json')
MARKER_ROOT = Path('/var/lib/server-settings/asa-mcp-acceptance')
PROTOCOL = '2025-03-26'
MAX_BODY = 2 * 1024 * 1024
USE_TOKEN = object()
OFFICIAL_TOOLS = frozenset({
    'create_database', 'create_ssl', 'create_website', 'get_dashboard_info',
    'get_system_info', 'install_mysql', 'install_openresty', 'list_databases',
    'list_installed_apps', 'list_ssls', 'list_websites',
})
HOST_TOOLS = frozenset({'asa_host_inventory', 'asa_admin_execute',
                        'asa_admin_status', 'asa_operation_get', 'asa_operation_list'})
REQUIRED_TOOLS = OFFICIAL_TOOLS | HOST_TOOLS


class ProofError(Exception):
    """Only fixed non-secret labels reach the caller."""


def require(value, code):
    if not value:
        raise ProofError(code)


def hexadecimal(value, length=32):
    return isinstance(value, str) and re.fullmatch('[a-f0-9]{'+str(length)+'}', value) is not None


def private_file(path, *, exact_mode=None):
    require(path.is_absolute() and path.resolve() == path, 'LOCAL_FILE_SYMLINK')
    st = path.lstat()
    require(stat.S_ISREG(st.st_mode) and st.st_uid == 0
            and not st.st_mode & 0o022, 'LOCAL_FILE_UNTRUSTED')
    if exact_mode is not None:
        require(stat.S_IMODE(st.st_mode) == exact_mode, 'LOCAL_FILE_MODE')
    for parent in path.parents:
        st = parent.lstat()
        require(stat.S_ISDIR(st.st_mode) and st.st_uid == 0
                and not st.st_mode & 0o022, 'LOCAL_PARENT_UNTRUSTED')
    return path.read_text(encoding='utf-8')


def instance_id(value):
    require(isinstance(value, dict) and hexadecimal(value.get('instanceId')), 'LOCAL_INSTANCE_ID_INVALID')
    return value['instanceId']


def origin_url(value):
    url = urllib.parse.urljoin(ORIGIN + '/', value)
    parts = urllib.parse.urlsplit(url)
    require(parts.scheme == 'https' and parts.netloc == 'x.asa-lab.ru'
            and not parts.fragment, 'OAUTH_ORIGIN_INVALID')
    return url


def redirect(headers, expected_path=None):
    require(bool(headers.get('Location')), 'OAUTH_REDIRECT_MISSING')
    url = origin_url(headers['Location'])
    if expected_path:
        require(urllib.parse.urlsplit(url).path.startswith(expected_path), 'OAUTH_REDIRECT_ROUTE')
    return url


def callback_code(status, headers, state):
    require(status in (302, 303), 'CONSENT_REDIRECT_HTTP_'+str(status))
    require(bool(headers.get('Location')), 'CALLBACK_MISSING')
    actual = urllib.parse.urlsplit(headers['Location'])
    expected = urllib.parse.urlsplit(CALLBACK)
    require((actual.scheme, actual.netloc, actual.path) ==
            (expected.scheme, expected.netloc, expected.path)
            and not actual.fragment, 'CALLBACK_URI_WRONG')
    values = urllib.parse.parse_qs(actual.query, keep_blank_values=True)
    require(values.get('state') == [state] and len(values.get('code', [])) == 1
            and bool(values['code'][0]) and 'error' not in values, 'CODE_STATE_MISMATCH')
    require('iss' not in values or values['iss'] == [ISSUER], 'CALLBACK_ISSUER_WRONG')
    return values['code'][0]


def token_claims(token):
    require(isinstance(token, str) and 100 <= len(token) <= 65536, 'ACCESS_TOKEN_MISSING')
    parts = token.split('.')
    require(len(parts) == 3, 'JWT_SHAPE_INVALID')
    try:
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + '=' * (-len(parts[1]) % 4)))
    except (ValueError, UnicodeError):
        raise ProofError('JWT_PAYLOAD_INVALID') from None
    require(isinstance(claims, dict) and claims.get('iss') == ISSUER, 'JWT_ISSUER_WRONG')
    require(claims.get('aud') in (RESOURCE, [RESOURCE]), 'JWT_AUDIENCE_WRONG')
    require(type(claims.get('exp')) in (int, float)
            and time.time() < claims['exp'] < 253402300800, 'JWT_EXPIRED')
    if 'nbf' in claims:
        require(type(claims['nbf']) in (int, float)
                and 0 <= claims['nbf'] <= time.time() + 30, 'JWT_NOT_YET_VALID')
    # Claims inspection is not signature verification; the authenticated gateway
    # must also accept this token and reject absent/invalid tokens below.
    return claims


def rpc_result(status, headers, body, request_id):
    require(status == 200, 'AUTHENTICATED_MCP_HTTP_'+str(status))
    require(len(body) <= MAX_BODY, 'MCP_RESPONSE_TOO_LARGE')
    content_type = headers.get('Content-Type', '').split(';', 1)[0].strip().lower()
    try:
        if content_type == 'application/json':
            events = [json.loads(body)]
        elif content_type == 'text/event-stream':
            events, data = [], []
            for line in body.decode('utf-8').splitlines() + ['']:
                if line.startswith('data:'):
                    data.append(line[5:].removeprefix(' '))
                elif not line and data:
                    events.append(json.loads('\n'.join(data)))
                    data = []
        else:
            raise ProofError('MCP_RESPONSE_CONTENT_TYPE')
    except (ValueError, UnicodeError):
        raise ProofError('MCP_RESPONSE_JSON') from None
    matches = [item for item in events if isinstance(item, dict)
               and type(item.get('id')) is int and item['id'] == request_id]
    require(len(matches) == 1 and matches[0].get('jsonrpc') == '2.0'
            and 'error' not in matches[0]
            and isinstance(matches[0].get('result'), dict), 'MCP_RPC_RESULT_INVALID')
    return matches[0]['result']


def initialize_result(value):
    require(isinstance(value, dict) and value.get('protocolVersion') == PROTOCOL
            and isinstance(value.get('capabilities'), dict)
            and isinstance(value['capabilities'].get('tools'), dict)
            and isinstance(value.get('serverInfo'), dict)
            and value['serverInfo'].get('name') == 'asa-school-host-admin', 'MCP_INIT_IDENTITY')


def toolset(value):
    require(isinstance(value, dict), 'MCP_TOOL_LIST_INVALID')
    items = value.get('tools')
    require(isinstance(items, list) and not value.get('nextCursor'), 'MCP_TOOL_LIST_INVALID')
    names = [item.get('name') for item in items if isinstance(item, dict)]
    require(len(items) == len(names) == len(REQUIRED_TOOLS)
            and all(isinstance(name, str) for name in names)
            and set(names) == REQUIRED_TOOLS, 'MCP_HOST_TOOL_SET_UNEXPECTED')


def tool_data(value):
    require(isinstance(value, dict), 'MCP_TOOL_CALL_FAILED')
    data = value.get('structuredContent')
    require(value.get('isError') is False and isinstance(data, dict)
            and data.get('ok') is True and data.get('error') is None
            and isinstance(data.get('data'), dict), 'MCP_TOOL_CALL_FAILED')
    return data['data']


def host_identity(value, expected_instance, *, inventory=False):
    uid_key = 'executor_uid' if inventory else 'uid'
    enabled_key = 'root_administration_enabled' if inventory else 'enabled'
    require(isinstance(value, dict) and hexadecimal(expected_instance)
            and value.get('hostname') == HOST and value.get('installation_id') == INSTALLATION
            and value.get('installation_instance_id') == expected_instance
            and type(value.get(uid_key)) is int and value[uid_key] == 0
            and value.get(enabled_key) is True, 'HOST_ADMIN_IDENTITY_MISMATCH')
    if inventory:
        require(isinstance(value.get('containers'), list)
                and type(value.get('containers_total')) is int
                and value['containers_total'] >= len(value['containers'])
                and len(value['containers']) <= 200
                and type(value.get('containers_truncated')) is bool
                and value['containers_truncated'] == (value['containers_total'] > len(value['containers'])),
                'HOST_INVENTORY_INVALID')
        allowed = {'ID', 'Names', 'Image', 'State', 'Status', 'Ports', 'Networks', 'CreatedAt', 'RunningFor'}
        require(all(isinstance(item, dict) and set(item) <= allowed
                    for item in value['containers']), 'HOST_INVENTORY_PRIVATE_FIELDS')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


class PublicClient:
    def __init__(self):
        self.token, self.session, self.serial = None, None, 0
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()), NoRedirect())

    def request(self, path, *, data=None, content_type=None, headers=None):
        h = dict(headers or {})
        if content_type:
            h['Content-Type'] = content_type
        req = urllib.request.Request(origin_url(path), data=data, headers=h,
                                     method='GET' if data is None else 'POST')
        try:
            with self.opener.open(req, timeout=15) as response:
                body = response.read(MAX_BODY + 1)
                require(len(body) <= MAX_BODY, 'HTTP_RESPONSE_TOO_LARGE')
                return response.status, response.headers, body
        except urllib.error.HTTPError as error:
            body = error.read(MAX_BODY + 1)
            require(len(body) <= MAX_BODY, 'HTTP_RESPONSE_TOO_LARGE')
            return error.code, error.headers, body

    def authenticate(self, password):
        status, _, body = self.request('/.well-known/oauth-protected-resource/mcp')
        require(status == 200, 'RESOURCE_DISCOVERY_HTTP')
        resource = json.loads(body)
        require(isinstance(resource, dict) and resource.get('resource') == RESOURCE
                and resource.get('authorization_servers') == [ISSUER], 'RESOURCE_DISCOVERY_IDENTITY')
        status, _, body = self.request('/.well-known/oauth-authorization-server')
        require(status == 200, 'ISSUER_DISCOVERY_HTTP')
        metadata = json.loads(body)
        require(isinstance(metadata, dict) and metadata.get('issuer') == ISSUER
                and metadata.get('authorization_endpoint') == ORIGIN + '/.idp/auth'
                and metadata.get('token_endpoint') == ORIGIN + '/.idp/token'
                and metadata.get('registration_endpoint') == ORIGIN + '/.idp/register'
                and isinstance(metadata.get('code_challenge_methods_supported'), list)
                and 'S256' in metadata['code_challenge_methods_supported'], 'ISSUER_DISCOVERY_IDENTITY')
        registration = {'client_name': 'ASA-School-VM100-Host-Admin-Acceptance',
                        'redirect_uris': [CALLBACK], 'token_endpoint_auth_method': 'none',
                        'grant_types': ['authorization_code', 'refresh_token'], 'response_types': ['code']}
        status, _, body = self.request('/.idp/register', data=json.dumps(registration).encode(), content_type='application/json')
        require(status == 201, 'DCR_HTTP_'+str(status))
        registered = json.loads(body)
        require(isinstance(registered, dict), 'DCR_RESPONSE_INVALID')
        client_id = registered.get('client_id')
        require(isinstance(client_id, str) and len(client_id) >= 8, 'DCR_CLIENT_ID_MISSING')
        require(registered.get('redirect_uris') == [CALLBACK], 'DCR_CALLBACK_CHANGED')
        verifier, state = secrets.token_urlsafe(48), secrets.token_urlsafe(20)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        query = {'response_type': 'code', 'client_id': client_id, 'redirect_uri': CALLBACK,
                 'state': state, 'code_challenge': challenge, 'code_challenge_method': 'S256', 'resource': RESOURCE}
        status, headers, _ = self.request('/.idp/auth?' + urllib.parse.urlencode(query))
        require(status == 302, 'AUTHORIZE_START_HTTP_'+str(status))
        consent = redirect(headers, '/.idp/auth/')
        status, headers, _ = self.request(consent)
        require(status == 302, 'OWNER_LOGIN_REDIRECT_HTTP_'+str(status))
        login = redirect(headers)
        require(urllib.parse.urlsplit(login).path == '/.auth/login', 'OWNER_LOGIN_ROUTE')
        status, _, body = self.request(login)
        require(status == 200 and b'name="password"' in body, 'OWNER_LOGIN_FORM')
        status, headers, _ = self.request('/.auth/login', data=urllib.parse.urlencode({'password': password}).encode(),
                                           content_type='application/x-www-form-urlencoded')
        password = ''
        require(status in (302, 303) and redirect(headers) == consent, 'OWNER_LOGIN_RETURN_ROUTE')
        status, _, body = self.request(consent)
        require(status == 200 and b'form method="post"' in body, 'OWNER_CONSENT_FORM')
        status, headers, _ = self.request(consent, data=b'', content_type='application/x-www-form-urlencoded')
        code = callback_code(status, headers, state)
        form = {'grant_type': 'authorization_code', 'client_id': client_id,
                'code': code, 'redirect_uri': CALLBACK, 'code_verifier': verifier, 'resource': RESOURCE}
        status, _, body = self.request('/.idp/token', data=urllib.parse.urlencode(form).encode(),
                                     content_type='application/x-www-form-urlencoded')
        require(status == 200, 'TOKEN_EXCHANGE_HTTP_'+str(status))
        issued = json.loads(body)
        require(isinstance(issued, dict) and str(issued.get('token_type', '')).lower() == 'bearer', 'TOKEN_TYPE_INVALID')
        token = issued.get('access_token')
        token_claims(token)
        self.token = token

    def send_rpc(self, method, params, *, bearer=USE_TOKEN, notification=False):
        self.serial += 1
        request_id = self.serial
        payload = {'jsonrpc': '2.0', 'method': method, 'params': params}
        if not notification:
            payload['id'] = request_id
        h = {'Accept': 'application/json, text/event-stream', 'MCP-Protocol-Version': PROTOCOL}
        actual_token = self.token if bearer is USE_TOKEN else bearer
        if actual_token is not None:
            h['Authorization'] = 'Bearer ' + actual_token
        if self.session:
            h['Mcp-Session-Id'] = self.session
        response = self.request('/mcp', data=json.dumps(payload).encode(), content_type='application/json', headers=h)
        return request_id, response

    def rpc(self, method, params):
        request_id, response = self.send_rpc(method, params)
        result = rpc_result(*response, request_id)
        if method == 'initialize':
            self.session = response[1].get('Mcp-Session-Id') or None
        return result

    def call(self, name, arguments):
        require(name in HOST_TOOLS, 'ACCEPTANCE_TOOL_FORBIDDEN')
        return tool_data(self.rpc('tools/call', {'name': name, 'arguments': arguments}))


def marker_command(action, run_id, nonce):
    require(action in ('write', 'read', 'cleanup') and hexadecimal(run_id)
            and hexadecimal(nonce), 'MARKER_ARGUMENTS')
    source = '''import hashlib,json,os,socket,stat
from pathlib import Path
base=Path(BASE_LITERAL)
name=NAME_LITERAL
payload=PAYLOAD_LITERAL.encode('ascii')
action=ACTION_LITERAL
assert os.geteuid()==0 and socket.gethostname()==HOST_LITERAL, 'MARKER_HOST'
assert base.is_absolute() and base.resolve()==base, 'MARKER_PATH'
for p in base.parents:
 s=p.lstat()
 assert stat.S_ISDIR(s.st_mode) and s.st_uid==0 and not s.st_mode&0o022, 'MARKER_PARENT'
if action=='write':
 base.mkdir(mode=0o700,exist_ok=True)
s=base.lstat()
assert stat.S_ISDIR(s.st_mode) and s.st_uid==0 and stat.S_IMODE(s.st_mode)==0o700, 'MARKER_DIRECTORY'
directory=os.open(str(base),os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
try:
 flags=os.O_RDWR|os.O_APPEND|os.O_CREAT if action=='write' else os.O_RDONLY
 fd=os.open(name,flags|os.O_NOFOLLOW,0o600,dir_fd=directory)
 try:
  s=os.fstat(fd)
  assert stat.S_ISREG(s.st_mode) and s.st_uid==0 and stat.S_IMODE(s.st_mode)==0o600 and s.st_nlink==1, 'MARKER_FILE'
  before=os.read(fd,4096)
  if action=='write':
   assert before in (b'',payload), 'MARKER_FOREIGN_CONTENT'
   assert os.write(fd,payload)==len(payload), 'MARKER_SHORT_WRITE'
   os.fsync(fd)
   os.lseek(fd,0,os.SEEK_SET)
   data=os.read(fd,4096)
  else:
   data=before
   assert data==payload, 'MARKER_CONTENT_OR_REPLAY'
  result={'action':action,'hostname':socket.gethostname(),'uid':os.geteuid(),'mode':'0600',
          'sha256':hashlib.sha256(data).hexdigest(),'line_count':data.count(b'\\n')}
 finally:
  os.close(fd)
 if action=='cleanup':
  current=os.stat(name,dir_fd=directory,follow_symlinks=False)
  assert (current.st_ino,current.st_dev)==(s.st_ino,s.st_dev), 'MARKER_CHANGED'
  os.unlink(name,dir_fd=directory)
  try:
   os.stat(name,dir_fd=directory,follow_symlinks=False)
   raise AssertionError('MARKER_NOT_REMOVED')
  except FileNotFoundError:
   result['removed']=True
 os.fsync(directory)
finally:
 os.close(directory)
print(json.dumps(result))
'''
    replacements = {'BASE_LITERAL': repr(str(MARKER_ROOT)),
                    'NAME_LITERAL': repr('root-proof-'+run_id+'.txt'),
                    'PAYLOAD_LITERAL': repr(nonce+'\n'), 'ACTION_LITERAL': repr(action), 'HOST_LITERAL': repr(HOST)}
    for key, value in replacements.items():
        source = source.replace(key, value)
    return "/usr/bin/python3 - <<'ASA_MCP_HOST_ADMIN_PROOF'\n" + source + 'ASA_MCP_HOST_ADMIN_PROOF'


def started(value, *, reused):
    require(isinstance(value, dict), 'ADMIN_START_OR_REPLAY_INVALID')
    operation = value.get('operation')
    require(value.get('reused') is reused and isinstance(operation, dict)
            and hexadecimal(operation.get('operation_id'))
            and operation.get('installation_id') == INSTALLATION
            and operation.get('kind') == 'admin', 'ADMIN_START_OR_REPLAY_INVALID')
    return operation['operation_id']


def wait_operation(client, operation_id, *, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = client.call('asa_operation_get', {'operation_id': operation_id, 'log_cursor': 0})
        require(isinstance(value, dict) and value.get('operation_id') == operation_id and value.get('installation_id') == INSTALLATION
                and value.get('kind') == 'admin', 'ADMIN_OPERATION_IDENTITY')
        if value.get('status') in ('queued', 'running'):
            time.sleep(0.5)
            continue
        result = value.get('result')
        require(value.get('status') == 'success' and isinstance(result, dict)
                and result.get('status') == 'complete' and result.get('operation_id') == operation_id
                and type(result.get('exit_code')) is int and result['exit_code'] == 0
                and result.get('timed_out') is False and result.get('output_may_be_truncated') is False,
                'ADMIN_OPERATION_NOT_SUCCESSFUL')
        log = value.get('log')
        require(isinstance(log, dict) and type(log.get('cursor')) is int and log['cursor'] > 0
                and isinstance(log.get('lines'), list) and len(log['lines']) > 0
                and all(isinstance(line, dict) and isinstance(line.get('at'), str)
                        and isinstance(line.get('phase'), str) and isinstance(line.get('message'), str)
                        for line in log['lines']), 'ADMIN_OPERATION_LOG_MISSING')
        return result
    raise ProofError('ADMIN_OPERATION_DEADLINE_NO_REPLAY')


def marker_result(result, action, nonce):
    require(isinstance(result, dict), 'MARKER_OUTPUT_INVALID')
    try:
        value = json.loads(result.get('output', ''))
    except (TypeError, ValueError):
        raise ProofError('MARKER_OUTPUT_INVALID') from None
    require(isinstance(value, dict) and value.get('action') == action and value.get('hostname') == HOST
            and type(value.get('uid')) is int and value['uid'] == 0 and value.get('mode') == '0600'
            and type(value.get('line_count')) is int and value['line_count'] == 1
            and value.get('sha256') == hashlib.sha256((nonce+'\n').encode()).hexdigest(), 'MARKER_PROOF_OR_REPLAY_FAILED')
    if action == 'cleanup':
        require(value.get('removed') is True, 'MARKER_CLEANUP_FAILED')


def prove_root(client, run_id, nonce):
    ids = {}
    def arguments(action):
        return {'installation_id': INSTALLATION, 'command': marker_command(action, run_id, nonce),
                'cwd': '/', 'timeout_seconds': 30, 'idempotency_key': 'vm100-proof-'+run_id+'-'+action}
    write_args = arguments('write')
    ids['write'] = started(client.call('asa_admin_execute', write_args), reused=False)
    marker_result(wait_operation(client, ids['write']), 'write', nonce)
    # Only this deliberate same-key replay is performed. No transport exception
    # causes an automatic command retry or a new key.
    replay = started(client.call('asa_admin_execute', write_args), reused=True)
    require(replay == ids['write'], 'ADMIN_REPLAY_OPERATION_CHANGED')
    marker_result(wait_operation(client, replay), 'write', nonce)
    ids['read'] = started(client.call('asa_admin_execute', arguments('read')), reused=False)
    marker_result(wait_operation(client, ids['read']), 'read', nonce)
    ids['cleanup'] = started(client.call('asa_admin_execute', arguments('cleanup')), reused=False)
    marker_result(wait_operation(client, ids['cleanup']), 'cleanup', nonce)
    records = client.call('asa_operation_list', {'installation_id': INSTALLATION, 'limit': 100}).get('operations')
    require(isinstance(records, list), 'ADMIN_OPERATION_LIST_INVALID')
    own = [record for record in records if isinstance(record, dict) and record.get('operation_id') in ids.values()]
    require(len(own) == 3 and {record['operation_id'] for record in own} == set(ids.values())
            and all(record.get('installation_id') == INSTALLATION and record.get('kind') == 'admin'
                    and record.get('status') == 'success' for record in own), 'ADMIN_OPERATION_LIST_MISSING')
    return ids


def run():
    require(os.geteuid() == 0 and socket.gethostname() == HOST, 'VM100_ROOT_REQUIRED')
    identity = instance_id(json.loads(private_file(IDENTITY)))
    password = private_file(PASSWORD, exact_mode=0o600).strip()
    require(len(password) >= 40, 'OWNER_PASSWORD_MISSING')
    client = PublicClient()
    client.authenticate(password)
    password = ''
    init = {'protocolVersion': PROTOCOL, 'capabilities': {},
            'clientInfo': {'name': 'ASA-VM100-Host-Admin-Acceptance', 'version': '1'}}
    for bearer in (None, 'invalid-token'):
        _, response = client.send_rpc('initialize', init, bearer=bearer)
        require(response[0] == 401, 'UNAUTHENTICATED_MCP_NOT_401' if bearer is None else 'INVALID_TOKEN_MCP_NOT_401')
    initialize_result(client.rpc('initialize', init))
    _, response = client.send_rpc('notifications/initialized', {}, notification=True)
    require(response[0] == 202, 'MCP_INITIALIZED_NOTIFICATION_HTTP')
    toolset(client.rpc('tools/list', {}))
    host_identity(client.call('asa_admin_status', {'installation_id': INSTALLATION}), identity)
    host_identity(client.call('asa_host_inventory', {'installation_id': INSTALLATION}), identity, inventory=True)
    run_id, nonce = secrets.token_hex(16), secrets.token_hex(16)
    operations = prove_root(client, run_id, nonce)
    return {'status': 'PASS', 'checked_at_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
            'endpoint': RESOURCE, 'hostname': HOST, 'installation_id': INSTALLATION,
            'instance_id_sha256': hashlib.sha256(identity.encode()).hexdigest(), 'tool_count': 16,
            'oauth_s256_flow': True, 'token_resource': RESOURCE, 'unauthenticated_and_invalid_token_http': 401,
            'mcp_session': 'stateful' if client.session else 'stateless', 'root_uid': 0,
            'idempotent_replay': True, 'marker_removed': True, 'operations': operations,
            'operation_list_verified': True, 'operation_logs_verified': True,
            'chatgpt_connected': False, 'asa_data_changed': False, 'configuration_changed': False,
            'oauth_test_client_registered': True, 'root_audit_retained': True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true', help='Register test OAuth client and perform the own-marker root proof')
    args = parser.parse_args(argv)
    if not args.apply:
        print(json.dumps({'mode': 'plan', 'endpoint': RESOURCE, 'hostname': HOST, 'expected_tools': 16,
                          'writes': ['OAuth test client', 'own root marker', 'durable operation audit'],
                          'marker_directory': str(MARKER_ROOT), 'marker_cleanup': 'separate audited operation',
                          'asa_data_changed': False, 'chatgpt_connected': False}))
        return 0
    try:
        print(json.dumps(run()))
        return 0
    except (ProofError, ValueError, OSError, TypeError, KeyError, AttributeError) as error:
        code = str(error) if isinstance(error, ProofError) else type(error).__name__
        # Never include response bodies, URLs with codes, passwords or JWTs.
        if not re.fullmatch(r'[A-Za-z0-9_]+', code):
            code = 'ACCEPTANCE_ERROR'
        print(json.dumps({'status': 'FAIL', 'code': code, 'chatgpt_connected': False,
                          'automatic_command_retry': False, 'root_audit_retained': True}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
