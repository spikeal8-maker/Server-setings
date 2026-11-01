import contextlib
import hashlib
import importlib.util
import io
import json
import multiprocessing
from pathlib import Path
import tempfile
import threading
import time
import types
import unittest
from unittest import mock
import urllib.error
import uuid
import struct
import zlib

SPEC = importlib.util.spec_from_file_location('release_load_100', Path(__file__).resolve().parents[1] / 'scripts/asa-release-load.py')
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)
import release_load_contracts as contracts
from release_load_ai import SseDecoder


def fixture(users=100):
    return {'origin': 'https://school.example', 'authorizedSyntheticFixture': True,
            'seats': [{'seat': str(uuid.UUID(int=i+1)), 'token': str(i).zfill(64)} for i in range(users)]}


def hold_pipe(send):
    time.sleep(10)
    send.close()


class Release100Safety(unittest.TestCase):
    def test_plans_all_three_steps_without_network_source_or_fixture_access(self):
        with mock.patch.object(gate.urllib.request.OpenerDirector, 'open', side_effect=AssertionError('network')), \
                mock.patch.object(gate, 'private_fixture', side_effect=AssertionError('fixture')), \
                mock.patch.object(gate, 'route_inventory', side_effect=AssertionError('source')):
            for users in (20, 50, 100):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(gate.main(['--origin', 'https://school.example', '--revision', 'a'*40,
                                                '--users', str(users), '--profile', 'mixed']), 0)
                data = json.loads(out.getvalue())
                self.assertEqual(data['users'], users)
                self.assertEqual(data['minimumSyntheticProjects'], users*3)
                self.assertFalse(data['releaseAccepted'])

    def test_fixture_refuses_real_marker_duplicate_role_and_header_injection(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'fixture.json'
            for change in ('not-synthetic', 'duplicate', 'header-injection', 'missing-role'):
                f = fixture()
                if change == 'not-synthetic': f['authorizedSyntheticFixture'] = False
                if change == 'duplicate': f['seats'][99]['seat'] = f['seats'][0]['seat']
                if change == 'header-injection': f['seats'][0]['token'] += '\r\nCookie: synthetic-only'
                p.write_text(json.dumps(f)); p.chmod(0o600)
                with self.subTest(change=change), self.assertRaises(contracts.ContractError):
                    contracts.private_fixture(p, f['origin'], 100, 'mixed' if change == 'missing-role' else 'core')

    def test_escalation_cannot_use_a_failed_or_wrong_revision_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); p = root/'fixture'; p.write_text(json.dumps(fixture())); p.chmod(0o600)
            prior = root/'prior.json'; prior.write_text(json.dumps({'passed': False, 'users': 20, 'revision': 'a'*40, 'schema': 286}))
            with mock.patch.object(gate, 'route_inventory', return_value={}), self.assertRaises(contracts.ContractError):
                gate.main(['--origin', 'https://school.example', '--revision', 'a'*40, '--schema', '286',
                           '--users', '50', '--fixture', str(p), '--previous-receipt', str(prior), '--output', str(root/'new.json'), '--apply'])
            self.assertFalse((root/'new.json').exists())

    def test_identity_mismatch_never_starts_project_writes(self):
        f = fixture(20)
        runner = gate.Runner(f, 'a'*40, 20, schema=286)
        calls = []
        def call(user, path, *args, **kwargs):
            calls.append(path)
            if path == '/health/ready':
                return {'status': 'ready', 'deployment': {'revision': 'a'*40, 'schemaVersion': 286, 'synchronized': True}}
            return {'authenticated': True, 'student': {'seatId': f['seats'][0]['seat']}, 'classroom': {'id': str(uuid.uuid4())}}
        with mock.patch.object(runner, 'call', side_effect=call), self.assertRaises(contracts.ContractError):
            runner.run()
        self.assertNotIn('/api/projects', calls)

    def test_html_shell_and_wrong_schema_are_not_functional_pass(self):
        with self.assertRaises(contracts.ContractError): contracts.contract('<html>200</html>', {'items':'list'})
        runner = gate.Runner(fixture(20), 'a'*40, 20, schema=286)
        with mock.patch.object(runner, 'call', return_value={'status':'ready','deployment':{'revision':'a'*40,'schemaVersion':285,'synchronized':True}}):
            with self.assertRaises(gate.CheckFailed): runner.assert_revision()

    def test_expected_isolation_denial_is_separate_but_429_stops_new_work(self):
        runner = gate.Runner(fixture(20), 'a'*40, 20, schema=286)
        def denied(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 404, 'synthetic', {}, io.BytesIO(b'{"error":{"code":"project_not_found"}}'))
        with mock.patch.object(runner.opener, 'open', side_effect=denied):
            runner.call(0, '/api/projects/synthetic', expected=(403,404), label='isolation-denial')
        self.assertTrue(runner.samples[0]['expectedNegative'])
        self.assertTrue(runner.samples[0]['ok'])
        self.assertFalse(runner.halt_new.is_set())
        def busy(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 429, 'synthetic', {}, io.BytesIO(b'{"error":{"code":"private-provider-secret"}}'))
        with mock.patch.object(runner.opener, 'open', side_effect=busy), self.assertRaises(gate.CheckFailed):
            runner.call(0, '/api/projects', label='create')
        self.assertTrue(runner.halt_new.is_set())
        self.assertNotIn('private-provider-secret', json.dumps(runner.samples))
        with mock.patch.object(runner.opener, 'open', side_effect=AssertionError('must not issue another mutation')):
            with self.assertRaises(gate.CheckFailed): runner.call(1, '/api/projects', {}, label='create')

    def test_paid_default_refused_even_when_local_option_is_available(self):
        status = {'enabled': True, 'provider':'ollama-cloud', 'model':'gemma4:31b', 'availableModels':['ollama-local','gemma4:31b']}
        with self.assertRaises(contracts.ContractError): contracts.local_ai(status)
        status.update(provider='ollama', model='ollama-local')
        contracts.local_ai(status)

    def test_telemetry_whitelist_rejects_missing_and_nonfinite_and_drops_secrets(self):
        source = {'memory':{'rssMb':100.0},'requests':{'total':4},'database':{'waiting':2},'private':'synthetic-secret',
                  'host':{'cpuUsedByApiPercent':float('nan')}}
        actual = contracts.numeric_telemetry(source)
        self.assertEqual(actual['database.waiting'], 2)
        self.assertNotIn('synthetic-secret', json.dumps(actual))
        self.assertNotIn('host.cpuUsedByApiPercent', actual)
        with self.assertRaises(contracts.ContractError): contracts.numeric_telemetry({'status':'ok'})

    def test_route_inventory_drift_blocks_before_any_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root/'App.tsx').write_text('route-v1')
            cat = root/'catalog.json'
            cat.write_text(json.dumps({'sourceFiles':{'App.tsx':hashlib.sha256(b'route-v1').hexdigest()},
                                      'routes':[{'id':'one','route':'/','roles':['Guest'],'notRunReason':'browser-only'}]}))
            with mock.patch.object(contracts, 'CATALOG', cat), mock.patch.object(contracts.subprocess, 'run', side_effect=[types.SimpleNamespace(stdout='a'*40+'\n'),types.SimpleNamespace(stdout=''),types.SimpleNamespace(stdout=b'route-v1')]):
                contracts.route_inventory(root, 'a'*40)
            (root/'App.tsx').write_text('route-v2-new-unclassified-route')
            with mock.patch.object(contracts, 'CATALOG', cat), mock.patch.object(contracts.subprocess, 'run', side_effect=[types.SimpleNamespace(stdout='a'*40+'\n'),types.SimpleNamespace(stdout=''),types.SimpleNamespace(stdout=b'route-v2-new-unclassified-route')]):
                with self.assertRaisesRegex(contracts.ContractError, 'drift'): contracts.route_inventory(root, 'a'*40)

    def test_catalog_never_calls_shell_200_browser_or_missing_checks_pass(self):
        catalog = contracts.route_inventory()
        self.assertGreaterEqual(len(catalog['routes']), 60)
        coverage = contracts.coverage(catalog, {'project-three-d'})
        self.assertTrue(all(r['browser'] == 'NOT_RUN' for r in coverage))
        self.assertEqual(next(r for r in coverage if r['id']=='home')['http'], 'NOT_RUN')
        self.assertEqual(next(r for r in coverage if r['id']=='join-class')['http'], 'NOT_RUN')

    def test_hard_deadline_kills_worker_and_preserves_partial_failure(self):
        context = multiprocessing.get_context('spawn'); receive, send = context.Pipe(duplex=False); cancel = context.Event()
        child = context.Process(target=hold_pipe, args=(send,)); child.start(); send.close()
        with tempfile.TemporaryDirectory() as tmp:
            partial = Path(tmp)/'partial.json'
            partial.write_text(json.dumps({'revision':'a'*40,'users':20,'requests':[{'label':'save','ok':True}], 'passed':False}))
            started = time.monotonic()
            report = gate.supervise(child, receive, cancel, .05, {'revision':'a'*40,'users':20,'passed':False}, partial)
            self.assertLess(time.monotonic()-started, 4)
            self.assertFalse(child.is_alive()); self.assertTrue(cancel.is_set())
            self.assertFalse(report['passed']); self.assertEqual(report['error'],'wall_time_deadline_exceeded')
            self.assertEqual(len(report['requests']), 1)

    def test_sse_comments_fragmentation_completion_identity_and_duplicate_done(self):
        rid = str(uuid.uuid4())
        done = {'stopped':False,'messages':[{'requestId':rid,'role':'assistant','content':'Ответ'}]}
        text = ': connected\n\n: heartbeat\n\nevent: delta\ndata: '+json.dumps({'text':'Ответ'})+'\n\nevent: done\ndata: '+json.dumps(done)+'\n\n'
        parser = SseDecoder(rid)
        for byte in text.encode(): parser.feed(bytes([byte]))
        parser.finish(); self.assertEqual(parser.text,'Ответ'); self.assertEqual(parser.comments,2)
        with self.assertRaises(contracts.ContractError): parser.feed(('event: done\ndata: '+json.dumps(done)+'\n\n').encode())
        wrong = SseDecoder(str(uuid.uuid4()))
        with self.assertRaises(contracts.ContractError): wrong.feed(('event: done\ndata: '+json.dumps(done)+'\n\n').encode())

    def test_optional_local_image_cancel_replay_retry_and_saved_bytes_are_scoped(self):
        runner = gate.Runner(fixture(20), 'a'*40, 20, schema=286, ai_images=1)
        thread = str(uuid.uuid4()); jobs = {}; calls = []; dispatched = []
        def chunk(kind, raw):
            return struct.pack('!I', len(raw))+kind+raw+struct.pack('!I',zlib.crc32(kind+raw))
        png = (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR',struct.pack('!2I5B',768,768,8,6,0,0,0))
               + chunk(b'IDAT',zlib.compress((b'\0'+b'\0\x50\xa0\xff'*768)*768)) + chunk(b'IEND',b''))
        def call(user, path, body=None, **kwargs):
            calls.append((user,path,body,kwargs.get('label')))
            self.assertEqual(user, 0)
            if path == '/api/ai/threads': return {'id':thread}
            if path == '/api/ai/status': return {'enabled':True,'provider':'ollama','model':'ollama-local','availableModels':['ollama-local']}
            if path.endswith('/image-generations'):
                rid=body['requestId']
                if rid not in jobs:
                    dispatched.append(rid)
                    jobs[rid]={'requestId':rid,'threadId':thread,'state':'admitted' if len(dispatched)==1 else 'complete','cancelRequested':False,
                               'image':{'downloadUrl':path+'/'+rid+'/image','mimeType':'image/png','width':768,'height':768,'sizeBytes':len(png)}}
                return jobs[rid].copy()
            rid = path.split('/image-generations/')[1].split('/')[0]
            if path.endswith('/cancel'):
                jobs[rid].update(state='cancelled',cancelRequested=True)
                return jobs[rid].copy()
            if path.endswith('/image'): return png
            return jobs[rid].copy()
        with mock.patch.object(runner,'call',side_effect=call):
            result=runner.ai_image(0,threading.Barrier(1))
        self.assertTrue(result['ok']); self.assertTrue(result['cancelledConfirmed']); self.assertTrue(result['confirmedReplaySameRequestId'])
        self.assertEqual(len(dispatched),2)
        self.assertEqual(dispatched[0],result['requestId']); self.assertEqual(dispatched[1],result['retryRequestId'])
        self.assertEqual(sum(c[1].endswith('/image') for c in calls),2)
        self.assertFalse(any('interrupt' in c[1] for c in calls))
        self.assertFalse(runner.inflight)

    def test_required_coverage_is_finite_pinned_and_fresh(self):
        from datetime import datetime, timezone
        now = datetime(2026, 11, 2, tzinfo=timezone.utc)
        row = {'source': 'metrics', 'state': 'ok', 'checkedAt': now.isoformat(),
               'collectedThrough': None, 'requireThrough': False, 'maxAgeSeconds': 300}
        data = {'coverageComplete': False, 'requiredCoverage': {'version': 1, 'policy': 'asa-connected-v1',
            'state': 'ok', 'requiredSourceIds': ['metrics'], 'sources': [row],
            'native': {'state': 'ok'}, 'historyAudit': {'state': 'ok'}}}
        contracts.required_log_coverage(data, ['metrics'], now)
        for patch in ({'state': 'unknown'}, {'native': {'state': 'unknown'}}, {'historyAudit': {'state': 'incomplete'}},
                      {'requiredSourceIds': []}, {'requiredSourceIds': ['docker']}, {'sources': []}):
            with self.assertRaises(contracts.ContractError):
                contracts.required_log_coverage({'requiredCoverage': dict(data['requiredCoverage'], **patch)}, ['metrics'], now)
        for patch in ({'state': 'pending'}, {'checkedAt': '2026-11-01T00:00:00Z'},
                      {'checkedAt': '2026-11-03T00:00:00Z'}, {'requireThrough': True}):
            modified = dict(data['requiredCoverage'], sources=[dict(row, **patch)])
            with self.assertRaises(contracts.ContractError): contracts.required_log_coverage({'requiredCoverage': modified}, ['metrics'], now)

    def test_png_corruption_and_secret_evidence_reference_are_rejected(self):
        image=gate.png_bytes(1)
        self.assertEqual(contracts.png_dimensions(image),(16,16))
        with self.assertRaises(contracts.ContractError): contracts.png_dimensions(image[:-1]+b'!')
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'fixture'; f=fixture(20)
            f['evidenceReferences']={'browser-rendering':'https://school.example/private?token=synthetic-private'}
            p.write_text(json.dumps(f));p.chmod(0o600)
            with self.assertRaises(contracts.ContractError): contracts.private_fixture(p,f['origin'],20,'core')


if __name__ == '__main__': unittest.main()
