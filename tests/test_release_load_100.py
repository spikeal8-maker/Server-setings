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
import copy

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


def completed_receipt(users=20, profile='core', ai_texts=0, ai_images=0):
    f = fixture()
    catalog = contracts.route_inventory()
    binding = {'origin': f['origin'], 'revision': 'a'*40, 'schema': 286,
               'cohortDigest': contracts.cohort_digest(f), 'sourceInventoryDigest': contracts.digest(catalog),
               'telemetrySource': contracts.telemetry_source(f, profile)}
    workload = {'profile': profile, 'paceMs': 0, 'maxP95Ms': 5000, 'maxP99Ms': 10000,
                'deadlineSeconds': 600, 'aiTextUsers': ai_texts, 'aiImages': ai_images,
                'aiFirstTokenMs': 120000, 'aiTotalMs': 240000}
    runner = gate.Runner(f, 'a'*40, users, schema=286, profile=profile, ai_texts=ai_texts, ai_images=ai_images,
                         deployment_binding=binding, workload=workload, inventory_verified=profile=='mixed')
    runner.completed = True; runner.started_at = time.time()-10; runner.finished_at = time.time()
    runner.actual_seats = [s['seat'] for s in f['seats'][:users]]
    runner.provenance = [{'revision': 'a'*40, 'schema': 286}]*2
    runner.telemetry = [{'memory.rssMb': 100, 'database.waiting': 0, 'requests.total': 900,
                         'sampledAt': runner.finished_at-1}]
    modules = [*gate.MODULES, *(['mixed-portal'] if profile == 'mixed' else [])]
    runner.results = [{'user': i+1, 'module': m, 'ok': True} for i in range(users) for m in modules]
    counts = {'identity':users, 'create':3*users, 'save':3*users, 'reopen':3*users,
              'snapshot-save':3*users, 'snapshot-read':3*users, 'scratch-read':users,
              'scratch-upload':users, 'isolation-denial':3*users, 'revision':2, 'runtime-metrics':1}
    if profile == 'mixed': counts.update({'portal-'+key:1 for key in contracts.READS})
    runner.samples = [{'label': label, 'actor': ('admin' if profile=='mixed' else 'guest') if label=='runtime-metrics' else i%users+1,
                       'ms': 1.0, 'status': 403 if label=='isolation-denial' else 200,
                       'ok': True, 'expectedNegative': label=='isolation-denial', 'error': None}
                      for label, count in counts.items() for i in range(count)]
    runner.ai_results = ([{'kind':'text','ok':True,'ms':20,'firstByteMs':1,'firstTokenMs':2,'emptyWorkingFilesVerified':True}]*ai_texts
                        + [{'kind':'image','ok':True,'ms':30,'admitMs':1,'emptyWorkingFilesVerified':True}]*ai_images)
    report = runner.report()
    assert report['passed']
    return report, binding, workload


class Release100Safety(unittest.TestCase):
    def test_prior_step_accepts_complete_matching_fresh_chain(self):
        report, binding, workload = completed_receipt()
        self.assertEqual(contracts.previous_step(report, binding, workload, 50, time.time()), [])
        next_report, _, _ = completed_receipt(50)
        next_report['stepChain'] = [{'users':20, 'receiptSha256':contracts.digest(report),
            'bindingDigest':contracts.digest(binding), 'workloadDigest':contracts.digest(workload), 'runFinishedAt':report['runFinishedAt']}]
        self.assertEqual(len(contracts.previous_step(next_report,binding,workload,100,time.time())),1)

    def test_prior_step_rejects_empty_green_forgery_and_partial_evidence(self):
        report, binding, workload = completed_receipt()
        green = {k: report[k] for k in ('format','passed','users','revision','schema','profile','paceMs','actualDistinctStudents','aiWorkloadState','maxP95Ms','maxP99Ms')}
        with self.assertRaises(contracts.ContractError): contracts.previous_step(green,binding,workload,50,time.time())
        for key, value in [('journeys',[]),('provenance',[]),('telemetry',[]),('requests',[]),('operationLatency',{}),
                           ('completionStage','incomplete'),('state','running'),('inFlight',[{'requestId':'synthetic'}]),('unexpectedErrors',1)]:
            with self.subTest(key=key), self.assertRaises(contracts.ContractError):
                contracts.previous_step(dict(report,**{key:value}),binding,workload,50,time.time())

    def test_prior_step_rejects_cross_origin_cohort_revision_and_source(self):
        report, binding, workload = completed_receipt()
        for key, value in [('origin','https://other.example'),('cohortDigest','b'*64),('revision','b'*40),('schema',287),('sourceInventoryDigest','b'*64)]:
            with self.subTest(key=key), self.assertRaises(contracts.ContractError):
                contracts.previous_step(report,dict(binding,**{key:value}),workload,50,time.time())
        self.assertNotEqual(contracts.cohort_digest(fixture(100)), contracts.cohort_digest(fixture(99)))
        changed=fixture(); changed['seats'][0]['token']='x'*64
        self.assertEqual(contracts.cohort_digest(fixture()),contracts.cohort_digest(changed))

    def test_prior_step_rejects_stale_future_overlong_and_broken_chain(self):
        report, binding, workload = completed_receipt()
        for start, finish in [(time.time()-2000,time.time()-1900),(time.time(),time.time()+60),(time.time()-1000,time.time())]:
            with self.assertRaises(contracts.ContractError):
                contracts.previous_step(dict(report,runStartedAt=start,runFinishedAt=finish),binding,workload,50,time.time())
        report, binding, workload = completed_receipt(50)
        with self.assertRaises(contracts.ContractError): contracts.previous_step(report,binding,workload,100,time.time())
        report['stepChain']=[{'users':20,'receiptSha256':'a'*64,'bindingDigest':'b'*64,'workloadDigest':contracts.digest(workload),'runFinishedAt':time.time()}]
        with self.assertRaises(contracts.ContractError): contracts.previous_step(report,binding,workload,100,time.time())

    def test_prior_step_rejects_ai_counts_budgets_and_missing_source_checks(self):
        report, binding, workload = completed_receipt(profile='mixed',ai_texts=2,ai_images=1)
        contracts.previous_step(report,binding,workload,50,time.time())
        for key, value in [('aiImages',10),('aiTextUsers',3),('aiFirstTokenMs',1000),('aiTotalMs',30000),('deadlineSeconds',900),('maxP95Ms',6000),('paceMs',1),('profile','core')]:
            with self.subTest(key=key), self.assertRaises(contracts.ContractError):
                contracts.previous_step(report,binding,dict(workload,**{key:value}),50,time.time())
        for key, value in [('sourceInventoryVerified',False),('routeCoverage',[]),('ai',[]),('aiWorkloadState','FAIL')]:
            with self.assertRaises(contracts.ContractError): contracts.previous_step(dict(report,**{key:value}),binding,workload,50,time.time())
        bad=copy.deepcopy(report); bad['ai'][0]['firstTokenMs']=999999
        with self.assertRaises(contracts.ContractError): contracts.previous_step(bad,binding,workload,50,time.time())

    def test_prior_step_rejects_request_polarity_and_inconsistent_counters(self):
        report,binding,workload=completed_receipt()
        for kind in ('missing-identity','wrong-polarity','bad-latency','missing-count'):
            bad=copy.deepcopy(report)
            if kind=='missing-identity': bad['requests']=[r for r in bad['requests'] if r['label']!='identity']
            if kind=='wrong-polarity': next(r for r in bad['requests'] if r['label']=='create')['expectedNegative']=True
            if kind=='bad-latency': bad['operationLatency']['save']['p99Ms']=0
            if kind=='missing-count': bad['modules']['blocks']['passed']=19
            with self.subTest(kind=kind), self.assertRaises(contracts.ContractError): contracts.previous_step(bad,binding,workload,50,time.time())

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

    def test_network_route_requires_both_own_reads_and_keeps_gameplay_not_run(self):
        catalog = contracts.route_inventory()
        self.assertEqual(len(catalog['routes']), 65)
        # All 64 approved routes retain their exact roles, checks and limitations.
        legacy = [r for r in catalog['routes'] if r['id'] != 'chess-network']
        self.assertEqual(contracts.digest(legacy), '7f9713065aa046f5d3f53c588b81228c18b730f49a5f3bcc68b925fc77725b01')
        row = next(r for r in catalog['routes'] if r['id'] == 'chess-network')
        self.assertEqual(row['route'], '/#/chess/:projectId/network')
        self.assertEqual(row['roles'], ['Seat', 'Teacher', 'Admin'])
        for checks, expected in [({'chess-network-access'}, 'NOT_RUN'),
                                 ({'chess-network-access', 'chess-network-history'}, 'DATA_CONTRACT_VERIFIED')]:
            result = next(r for r in contracts.coverage(catalog, checks) if r['id'] == 'chess-network')
            self.assertEqual(result['http'], expected)
            self.assertEqual(result['browser'], 'NOT_RUN')

    def test_network_access_reads_actual_pupil_and_validates_closed_policy(self):
        runner = gate.Runner(fixture(20), 'a'*40, 20, schema=300)
        valid = {'player': {'playerId': str(uuid.uuid4()), 'displayAlias': 'QA'},
                 'allowed': True, 'reason': 'allowed', 'scopes': ['classroom']}
        denied = dict(valid, allowed=False, reason='teacher_restricted', scopes=[])
        for value in (valid, denied):
            with mock.patch.object(runner, 'call', return_value=value) as call:
                runner.probe('chess-network-access', 19)
                call.assert_called_once_with(19, '/api/games/chess/access', label='portal-chess-network-access')
        for change in ({'scopes':['external']}, {'scopes':['classroom','classroom']},
                       {'allowed':False}, {'reason':'private-provider-message'}, {'scopes':[]},
                       {'player':{'playerId':'not-a-player','displayAlias':'QA'}}):
            with self.subTest(change=change), mock.patch.object(runner, 'call', return_value=dict(valid, **change)), \
                    self.assertRaises(contracts.ContractError):
                runner.probe('chess-network-access', 19)

    def test_network_history_is_empty_own_projection_and_never_mutates(self):
        runner = gate.Runner(fixture(20), 'a'*40, 20, schema=300)
        valid = {'matches':[], 'invites':[], 'nextCursor':None}
        with mock.patch.object(runner, 'call', return_value=valid) as call:
            runner.probe('chess-network-history', 7)
            call.assert_called_once_with(7, '/api/games/chess/history?limit=10', label='portal-chess-network-history')
        for value in ({'matches':[], 'invites':[]}, dict(valid, matches=[{'id':'foreign'}]),
                      dict(valid, invites=[{'id':'foreign'}]), dict(valid, nextCursor='foreign'), '<html>'):
            with self.subTest(value=value), mock.patch.object(runner, 'call', return_value=value), \
                    self.assertRaises(contracts.ContractError):
                runner.probe('chess-network-history', 7)

    def test_working_files_fault_prevents_image_provider_submission(self):
        for value in ({'items':[{'name':'foreign-private-file'}]}, {'items':'invalid'}, '429'):
            runner = gate.Runner(fixture(20), 'a'*40, 20, schema=300, ai_images=1)
            thread = str(uuid.uuid4())
            original = runner.call
            def call(user, path, body=None, **kwargs):
                self.assertEqual(user, 0)
                if path == '/api/ai/threads':
                    self.assertEqual(body, {})
                    return {'id':thread}
                self.assertEqual(path, f'/api/ai/threads/{thread}/working-files')
                self.assertIsNone(body)
                if value == '429':
                    return original(user, path, **kwargs)
                return value
            def busy(request, timeout):
                raise urllib.error.HTTPError(request.full_url, 429, 'synthetic', {}, io.BytesIO(b'{"error":{"code":"secret"}}'))
            with self.subTest(value=value), mock.patch.object(runner, 'call', side_effect=call), \
                    mock.patch.object(runner.opener, 'open', side_effect=busy):
                result = runner.ai_image(0, threading.Barrier(1))
            self.assertFalse(result['ok'])
            self.assertNotIn('emptyWorkingFilesVerified', result)
            self.assertTrue(runner.halt_new.is_set())
            self.assertFalse(runner.inflight)
            self.assertNotIn('foreign-private-file', json.dumps(result))
            self.assertNotIn('secret', json.dumps(runner.samples))

    def test_ai_escalation_requires_own_empty_working_files_proof(self):
        report, binding, workload = completed_receipt(profile='mixed', ai_texts=1, ai_images=1)
        for index in (0, 1):
            changed = copy.deepcopy(report)
            changed['ai'][index].pop('emptyWorkingFilesVerified')
            with self.assertRaisesRegex(contracts.ContractError, 'working-files'):
                contracts.previous_step(changed, binding, workload, 50, time.time())

    def test_text_checks_own_empty_working_files_before_local_stream_and_history(self):
        runner = gate.Runner(fixture(20), 'a'*40, 20, schema=300, ai_texts=1)
        thread = str(uuid.uuid4())
        calls = []
        sent = {}
        answer = 'A variable stores a value. 18.'
        def call(user, path, body=None, **kwargs):
            self.assertEqual(user, 4)
            calls.append(path)
            if path == '/api/ai/threads': return {'id':thread}
            if path == f'/api/ai/threads/{thread}/working-files': return {'items':[]}
            if path == '/api/ai/status':
                return {'enabled':True,'provider':'ollama','model':'ollama-local','availableModels':['ollama-local']}
            self.assertEqual(path, f'/api/ai/threads/{thread}/messages')
            return {'items':[{'requestId':sent['requestId'],'role':'assistant','content':answer}]}
        def open_stream(request, timeout):
            self.assertEqual(calls, ['/api/ai/threads', f'/api/ai/threads/{thread}/working-files', '/api/ai/status'])
            self.assertEqual(request.full_url, runner.origin + f'/api/ai/threads/{thread}/messages/stream')
            sent.update(json.loads(request.data))
            self.assertEqual(sent['model'], 'ollama-local')
            self.assertEqual(sent['webSearchMode'], 'off')
            self.assertEqual(sent['nativeImage'], {'mode':'off','size':768})
            done = {'stopped':False,'messages':[{'requestId':sent['requestId'],'role':'assistant','content':answer}]}
            chunks = iter([('event: delta\ndata: '+json.dumps({'text':answer})+'\n\nevent: done\ndata: '+json.dumps(done)+'\n\n').encode(), b''])
            response = mock.MagicMock()
            response.status = 200
            response.headers = {'Content-Type':'text/event-stream'}
            response.read1.side_effect = lambda size: next(chunks)
            response.__enter__.return_value = response
            return response
        with mock.patch.object(runner, 'call', side_effect=call), mock.patch.object(runner.opener, 'open', side_effect=open_stream):
            result = runner.ai_text(4, threading.Barrier(1))
        self.assertTrue(result['ok'])
        self.assertTrue(result['emptyWorkingFilesVerified'])
        self.assertEqual(result['answerChars'], len(answer))
        self.assertEqual(result['doneCount'], 1)
        self.assertFalse(runner.inflight)

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
            if path == f'/api/ai/threads/{thread}/working-files': return {'items':[]}
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
        self.assertTrue(result['emptyWorkingFilesVerified'])
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
