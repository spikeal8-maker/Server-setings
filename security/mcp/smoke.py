"""Synthetic CI-only OAuth/full-tool/restart acceptance against the built image.

No host configuration, owner credential, real panel or deployment is accessed.
"""
import base64
import hashlib
import http.cookiejar
import http.server
import json
import os
from pathlib import Path
import secrets
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

TOOLS = {'get_dashboard_info', 'get_system_info', 'list_databases', 'list_installed_apps',
         'list_ssls', 'list_websites', 'create_website', 'create_ssl', 'create_database',
         'install_mysql', 'install_openresty'}
BASE = 'https://localhost:39448'
RESOURCE = BASE + '/mcp'


def run(args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout.strip()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def smoke(image, binary):
    key = secrets.token_hex(16)
    password = secrets.token_urlsafe(32)
    hits = []

    class Panel(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.reply()

        def do_POST(self):
            self.rfile.read(int(self.headers.get('Content-Length', '0')))
            self.reply()

        def reply(self):
            stamp = self.headers.get('1Panel-Timestamp', '')
            expected = hashlib.md5(('1panel' + key + stamp).encode()).hexdigest()
            assert self.headers.get('1Panel-Token') == expected
            assert abs(int(stamp) - time.time()) < 60
            assert self.path in ('/api/v2/dashboard/base/os', '/api/v2/dashboard/base/all/all', '/api/v2/websites/search')
            hits.append(self.path)
            data = {'os': 'synthetic-ci', 'hostname': 'synthetic-ci', 'websiteNumber': 0}
            if self.path.endswith('/search'):
                data = {'items': [], 'total': 0}
            body = json.dumps({'code': 200, 'message': '', 'data': data}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    panel = http.server.ThreadingHTTPServer(('127.0.0.1', 39446), Panel)
    threading.Thread(target=panel.serve_forever, daemon=True).start()
    extended_hits = []

    class ExtendedMCP(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            assert self.path == '/mcp'
            message = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            method = message['method']
            if method == 'notifications/initialized':
                self.send_response(202)
                self.end_headers()
                return
            if method == 'initialize':
                result = {'protocolVersion': '2025-03-26', 'capabilities': {'tools': {}},
                          'serverInfo': {'name': 'synthetic-extended-admin', 'version': '1'}}
            elif method == 'tools/list':
                result = {'tools': [{'name': 'asa_admin_execute', 'description': 'Synthetic forwarding test; never executes commands',
                                     'inputSchema': {'type': 'object', 'properties': {'command': {'type': 'string'}}}}]}
            elif method == 'tools/call':
                assert message['params'] == {'name': 'asa_admin_execute', 'arguments': {'command': 'synthetic-id'}}
                extended_hits.append(message['params'])
                result = {'content': [{'type': 'text', 'text': 'synthetic UID 0; no command executed'}],
                          'structuredContent': {'uid': 0, 'executed': False}}
            else:
                raise AssertionError('Unexpected synthetic upstream method')
            body = json.dumps({'jsonrpc': '2.0', 'id': message['id'], 'result': result}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    extended = http.server.ThreadingHTTPServer(('127.0.0.1', 39449), ExtendedMCP)
    threading.Thread(target=extended.serve_forever, daemon=True).start()
    container = 'asa-mcp-security-' + secrets.token_hex(6)
    with tempfile.TemporaryDirectory(prefix='asa-mcp-security-') as directory:
        directory = Path(directory)
        state = directory / 'state'
        state.mkdir()
        cert, private = directory / 'cert.pem', directory / 'key.pem'
        run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
             '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost',
             '-keyout', str(private), '-out', str(cert)])
        context = ssl.create_default_context(cafile=str(cert))
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                  urllib.request.HTTPSHandler(context=context),
                  urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()), NoRedirect())

        def request(path, body=None, headers=None, form=False):
            url = path if path.startswith('https://') else BASE + path
            # Metadata is untrusted input even in this fixture.
            assert urllib.parse.urlsplit(url).netloc == 'localhost:39448'
            headers = dict(headers or {})
            if body is not None:
                headers['Content-Type'] = 'application/x-www-form-urlencoded' if form else 'application/json'
                body = (urllib.parse.urlencode(body) if form else json.dumps(body)).encode()
            try:
                response = opener.open(urllib.request.Request(url, data=body, headers=headers), timeout=10)
            except urllib.error.HTTPError as error:
                response = error
            return response.code, response.headers, response.read()

        def ready():
            for _ in range(60):
                try:
                    status, _, _ = request('/.well-known/oauth-protected-resource/mcp')
                    if status == 200:
                        return
                except (OSError, urllib.error.URLError):
                    pass
                time.sleep(0.5)
            raise AssertionError('Candidate did not become ready')

        os.environ['PASSWORD'] = password
        os.environ['PANEL_ACCESS_TOKEN'] = key

        def launch(target):
            run(['docker', 'run', '-d', '--name', container, '--network', 'host',
                 '-e', 'PASSWORD', '-e', 'PANEL_ACCESS_TOKEN', '-e', 'PANEL_HOST=http://127.0.0.1:39446',
                 '-v', str(state) + ':/data', '-v', str(cert) + ':/fixture/cert.pem:ro',
                 '-v', str(private) + ':/fixture/key.pem:ro',
                 '-v', str(Path(binary).resolve()) + ':/usr/local/bin/mcp-1panel:ro', image,
                 '--listen', '127.0.0.1:39447', '--tls-listen', '127.0.0.1:39448',
                 '--external-url', BASE, '--tls-cert-file', '/fixture/cert.pem',
                 '--tls-key-file', '/fixture/key.pem', '--'] + target)

        try:
            launch(['/usr/local/bin/mcp-1panel', '-transport', 'stdio', '-access-level', 'full'])
            ready()
            status, _, body = request('/.well-known/oauth-protected-resource/mcp')
            assert status == 200 and json.loads(body)['resource'] == RESOURCE
            status, headers, _ = request('/mcp')
            assert status == 401 and '/.well-known/oauth-protected-resource/mcp' in headers['WWW-Authenticate']
            status, _, body = request('/.well-known/oauth-authorization-server')
            metadata = json.loads(body)
            assert status == 200 and 'S256' in metadata['code_challenge_methods_supported']
            callback = 'http://127.0.0.1:39876/callback'
            status, _, body = request(metadata['registration_endpoint'], {
                'client_name': 'Synthetic CI security test', 'redirect_uris': [callback],
                'token_endpoint_auth_method': 'none', 'grant_types': ['authorization_code', 'refresh_token'],
                'response_types': ['code']})
            client = json.loads(body)
            assert status in (200, 201) and client.get('client_id') and not client.get('client_secret')
            verifier = secrets.token_urlsafe(48)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
            state_nonce = secrets.token_urlsafe(24)
            params = {'client_id': client['client_id'], 'redirect_uri': callback, 'response_type': 'code',
                      'code_challenge': challenge, 'code_challenge_method': 'S256', 'state': state_nonce,
                      'resource': RESOURCE}
            status, headers, _ = request(metadata['authorization_endpoint'] + '?' + urllib.parse.urlencode(params))
            assert status == 302
            consent = urllib.parse.urljoin(BASE, headers['Location'])
            status, headers, _ = request(consent)
            if status == 302:
                login = urllib.parse.urljoin(BASE, headers['Location'])
                assert request(login)[0] == 200
                status, headers, _ = request(login, {'password': password}, form=True)
                assert status == 302
                consent = urllib.parse.urljoin(BASE, headers['Location'])
                status, _, _ = request(consent)
            assert status == 200
            status, headers, _ = request(consent, {}, form=True)
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(headers.get('Location', '')).query)
            assert status in (302, 303) and query.get('state') == [state_nonce] and 'code' in query
            token_request = {'grant_type': 'authorization_code', 'client_id': client['client_id'],
                             'code': query['code'][0], 'redirect_uri': callback,
                             'code_verifier': verifier, 'resource': RESOURCE}
            status, _, body = request(metadata['token_endpoint'], token_request, form=True)
            tokens = json.loads(body)
            assert status == 200 and tokens.get('access_token')
            assert request(metadata['token_endpoint'], token_request, form=True)[0] == 400
            auth = {'Authorization': 'Bearer ' + tokens['access_token'], 'Accept': 'application/json, text/event-stream'}

            def rpc(method, params=None):
                message = {'jsonrpc': '2.0', 'id': 1, 'method': method}
                if params is not None:
                    message['params'] = params
                status, headers, body = request('/mcp', message, auth)
                assert status == 200
                if headers.get('Mcp-Session-Id'):
                    auth['Mcp-Session-Id'] = headers['Mcp-Session-Id']
                if body.startswith((b'event:', b'data:')):
                    body = next(line[5:].strip() for line in body.splitlines() if line.startswith(b'data:'))
                result = json.loads(body)
                assert 'error' not in result
                return result['result']

            def initialize():
                rpc('initialize', {'protocolVersion': '2025-03-26', 'capabilities': {},
                                   'clientInfo': {'name': 'synthetic-security-ci', 'version': '1'}})
                request('/mcp', {'jsonrpc': '2.0', 'method': 'notifications/initialized'}, auth)

            initialize()
            assert {t['name'] for t in rpc('tools/list')['tools']} == TOOLS
            for tool in ('get_dashboard_info', 'get_system_info', 'list_websites'):
                result = rpc('tools/call', {'name': tool, 'arguments': {}})
                assert not result.get('isError') and result['structuredContent']['code'] == 200
            assert len(hits) == 3
            run(['docker', 'restart', container])
            ready()
            auth.pop('Mcp-Session-Id', None)
            initialize()
            assert {t['name'] for t in rpc('tools/list')['tools']} == TOOLS
            if tokens.get('refresh_token'):
                status, _, body = request(metadata['token_endpoint'], {'grant_type': 'refresh_token',
                    'client_id': client['client_id'], 'refresh_token': tokens['refresh_token'],
                    'resource': RESOURCE}, form=True)
                assert status == 200 and json.loads(body).get('access_token')
            # The gateway is transport/auth, not a whitelist of official panel
            # tools. Preserve opaque extended/root-capable tool forwarding too.
            run(['docker', 'rm', '-f', container])
            launch(['http://127.0.0.1:39449/mcp'])
            ready()
            auth.pop('Mcp-Session-Id', None)
            initialize()
            assert {t['name'] for t in rpc('tools/list')['tools']} == {'asa_admin_execute'}
            result = rpc('tools/call', {'name': 'asa_admin_execute', 'arguments': {'command': 'synthetic-id'}})
            assert result['structuredContent'] == {'uid': 0, 'executed': False}
            assert len(extended_hits) == 1
            print('PASS OAuth discovery/S256/full11/signed mock-panel reads/token restart/opaque extended admin forwarding')
        finally:
            subprocess.run(['docker', 'rm', '-f', container], capture_output=True)
            os.environ.pop('PASSWORD', None)
            os.environ.pop('PANEL_ACCESS_TOKEN', None)
            panel.shutdown()
            extended.shutdown()


if __name__ == '__main__':
    smoke(*sys.argv[1:])
