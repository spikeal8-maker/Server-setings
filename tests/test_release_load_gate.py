import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
import urllib.error
import urllib.request

SPEC = importlib.util.spec_from_file_location('asa_release_load_gate', Path(__file__).resolve().parents[1] / 'scripts/asa-release-load.py')
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


class ReleaseLoadSafety(unittest.TestCase):
    def fixture(self, path):
        f = {'origin': 'https://school.example', 'authorizedSyntheticFixture': True,
             'seats': [{'seat': str(i), 'token': str(i).zfill(64)} for i in range(50)]}
        path.write_text(json.dumps(f))
        path.chmod(0o600)
        return f

    def test_plan_performs_no_network_or_fixture_reads(self):
        with mock.patch.object(urllib.request.OpenerDirector, 'open', side_effect=AssertionError('network')), mock.patch.object(gate, 'fixture_input', side_effect=AssertionError('fixture')):
            self.assertEqual(gate.main(['--origin', 'https://school.example', '--revision', 'a' * 40]), 0)

    def test_requires_explicit_synthetic_distinct_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'fixture'
            f = self.fixture(p)
            f['seats'][49]['token'] = f['seats'][0]['token']
            p.write_text(json.dumps(f))
            with self.assertRaises(gate.CheckFailed):
                gate.fixture_input(p, f['origin'], 50)
            f = self.fixture(p)
            f['authorizedSyntheticFixture'] = False
            p.write_text(json.dumps(f))
            with self.assertRaises(gate.CheckFailed):
                gate.fixture_input(p, f['origin'], 50)

    def test_refuses_to_overwrite_fixture_or_prior_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'fixture'
            self.fixture(p)
            before = p.read_bytes()
            with self.assertRaises(SystemExit):
                gate.main(['--origin', 'https://school.example', '--revision', 'a' * 40,
                           '--fixture', str(p), '--output', str(p), '--apply'])
            self.assertEqual(p.read_bytes(), before)

    def test_redirect_never_reaches_destination_with_credentials(self):
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.path)
                if self.path == '/source':
                    self.send_response(302)
                    self.send_header('Location', '/destination')
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.end_headers()

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            opener = urllib.request.build_opener(gate.NoRedirect())
            req = urllib.request.Request(f'http://127.0.0.1:{server.server_port}/source',
                                         headers={'Cookie': 'synthetic-only', 'Authorization': 'Bearer synthetic-only'})
            with self.assertRaises(urllib.error.HTTPError) as rejected:
                opener.open(req, timeout=2)
            self.assertEqual(rejected.exception.code, 302)
            self.assertEqual(seen, ['/source'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)

    def test_running_receipt_is_fail_closed_and_final_failure_is_durable(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'receipt.json'
            gate.write_receipt(p, {'passed': False, 'state': 'running'}, exclusive=True)
            self.assertFalse(json.loads(p.read_text())['passed'])
            gate.write_receipt(p, {'passed': False, 'state': 'failed', 'error': 'wall_time_deadline_exceeded'})
            self.assertEqual(json.loads(p.read_text())['error'], 'wall_time_deadline_exceeded')
            if os.name != 'nt':
                self.assertEqual(p.stat().st_mode & 0o077, 0)


if __name__ == '__main__':
    unittest.main()
