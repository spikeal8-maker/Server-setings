"""Real file/rename/fsync/JSON fixtures; host, privilege and native API boundaries are simulated.

No service, Git, database, API or cloud command is executed. The exact previous
wrapper is reconstructed from the two-line fix and checked against its public
SHA, so the test remains self-contained in the new Git checkout.
"""
import copy
from contextlib import ExitStack, nullcontext
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

CODE = Path(__file__).resolve().parents[1] / 'scripts'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


repair = load('staged_repair_under_test', CODE / 'school-1panel-staged-repair.py')
OLD_GEN = 'dc880db9be0e49b0953973ba39f19cde'
NEW_GEN = 'afca020a42684d62b644f46584ce1954'
DISPATCH = 'eff006b69a1644bf9ea44ddce5bb0f32'
FIXED_TAIL = ("    # The worker owns its streaming output, including the terminal result.\n"
              "    if args.action != 'worker':\n"
              "        print(json.dumps(value, sort_keys=True), flush=True)\n")
OLD_TAIL = "    print(json.dumps(value, sort_keys=True), flush=True)\n"


def accepted_stdout_fix():
    # This case tests a historical, two-line repair against its immutable source.
    # Reverse only the later settings package pins, then verify the historical SHA.
    wrapper = CODE / repair.NAME
    current = load('current_native_schedule_pins', wrapper)
    installer = load('current_legacy_settings_package_pins', CODE / 'install-school-nightly-settings-backup.py')
    current_unit = hashlib.sha256(installer.SERVICE_BODY.encode()).hexdigest()
    text = wrapper.read_text()
    text = text.replace(current.SETTINGS_FILES['school_settings_snapshot.py'],
                        '9a7719c5705fb6883857b0c25be1e19f9ee7f3009c8594a43ca15d04d0bffbe2', 1)
    text = text.replace(current.SETTINGS_FILES['school-nightly-settings-backup.py'],
                        'fd27a26dba0850663010501ad4456a494bf0203e4974c288a59c3e0474b0352b', 1)
    text = text.replace("',\n                    '" + current_unit + "')", "')", 1)
    return text.encode()


class Fixture:
    def __enter__(self):
        self.stack = ExitStack()
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix='staged-repair-proof-')))
        self.app = self.root / 'application'
        self.state_dir = self.root / 'state'
        self.base = self.root / 'schedule-code'
        self.calls, self.mutations = [], []
        self.fail = None
        self.collector_pid = '2770698'
        fixed = accepted_stdout_fix()
        if hashlib.sha256(fixed).hexdigest() != repair.NEW_SHA:
            raise AssertionError('fixed wrapper fixture pin changed')
        text = fixed.decode()
        if text.count(FIXED_TAIL) != 1:
            raise AssertionError('known stdout-only delta changed')
        previous = text.replace(FIXED_TAIL, OLD_TAIL, 1).encode()
        if hashlib.sha256(previous).hexdigest() != repair.OLD_SHA:
            raise AssertionError('previous wrapper fixture is not exact published source')
        old_path = self.base / 'releases' / repair.OLD_SHA[:16] / repair.NAME
        new_path = self.root / 'published' / repair.NAME
        self.write(old_path, previous)
        self.write(new_path, fixed)
        self.old = load('school_staged_repair_old_wrapper', old_path)
        self.new = load('school_staged_repair_fixed_wrapper', new_path)
        for module in (self.old, self.new):
            module.APP, module.STATE, module.BASE = self.app, self.state_dir, self.base
            module.LOCK, module.DB_LOCK = self.root / 'ownership.lock', self.root / 'database.lock'
            module.trusted = self.trusted
            module.check_host = lambda: None
            module.handover_window = lambda: None
            module.verify_runners = lambda: None
            module.stamp = lambda: '2026-11-08T15:30:00+00:00'
            module.command = self.command
            module.Legacy = lambda: self.legacy_object
        self.new.install_release = self.install_release
        self.legacy = {kind: {'timer': {'UnitFileState': 'enabled', 'ActiveState': 'active',
            'SubState': 'waiting', 'sha256': 'a' * 64}, 'service': {'UnitFileState': 'static',
            'ActiveState': 'inactive', 'SubState': 'dead', 'sha256': 'b' * 64}} for kind in self.old.KINDS}
        self.legacy_object = SimpleNamespace(inspect=lambda kind: copy.deepcopy(self.legacy[kind]), idle=self.legacy_idle)
        preview = [f'2026-11-{day:02d} 03:20:00' for day in range(9, 14)]
        self.state = {'format': self.old.FORMAT, 'instance': self.old.INSTANCE, 'project': self.old.PROJECT,
            'generation': OLD_GEN, 'programSha256': repair.OLD_SHA, 'programPath': str(old_path),
            'createdAt': '2026-11-08T15:00:00+00:00', 'updatedAt': '2026-11-08T15:05:09+00:00',
            'phase': 'STAGED', 'owner': 'legacy', 'originalOwner': 'legacy', 'legacy': copy.deepcopy(self.legacy),
            'clockTimeZone': 'Europe/Moscow', 'backupExecutedDuringInstall': False,
            'jobs': {kind: {'id': repair.JOB_IDS[kind], 'processConfigured': True,
                           'nativeNextPreview': copy.deepcopy(preview)} for kind in self.old.KINDS}}
        self.state['jobs']['database'].update(probeIntent={'at': '2026-11-08T15:05:00+00:00', 'previousRecordIds': []},
            probe={'status': 'NATIVE_GATED_PROBE_PASS', 'backupExecuted': False, 'dispatchId': DISPATCH,
                   'recordId': 1, 'programSha256': repair.OLD_SHA})
        self.raw_state = self.old.canonical(self.state) + b'\n'
        self.write(self.state_dir / 'state.json', self.raw_state)
        for name in ('dispatch', 'runs', 'output'):
            self.mkdir(self.state_dir / name)
        dispatch = {'dispatchId': DISPATCH, 'generation': OLD_GEN, 'kind': 'database', 'phase': 'STAGED',
            'programSha256': repair.OLD_SHA, 'status': 'OBSERVED_COMPLETE', 'dayMoscow': '2026-11-08',
            'createdAt': '2026-11-08T15:05:08+00:00', 'finishedAt': '2026-11-08T15:05:09+00:00'}
        run = {'dispatchId': DISPATCH, 'generation': OLD_GEN, 'kind': 'database', 'programSha256': repair.OLD_SHA,
            'status': 'STAGED_GATE_VERIFIED_NO_BACKUP', 'backupExecuted': False, 'restoreValidated': False,
            'startedAt': '2026-11-08T15:05:08+00:00', 'finishedAt': '2026-11-08T15:05:09+00:00'}
        self.write(self.state_dir / 'dispatch/database.json', self.old.canonical(dispatch) + b'\n')
        self.write(self.state_dir / 'runs/database.json', self.old.canonical(run) + b'\n')
        self.write(self.app / '.asa/diagnostics/private/index.sqlite', b'index contents never opened by repair')
        self.log = self.root / 'native-database.out.log'
        line = self.old.canonical(run) + b'\n'
        self.write(self.log, line + line)
        self.panel = FakePanel(self)
        self.updater = FakeUpdater(self)
        self.stack.enter_context(patch.object(repair, 'backup_logs', side_effect=self.logs))
        return self

    def __exit__(self, *args):
        return self.stack.__exit__(*args)

    def mkdir(self, path):
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        while path.is_relative_to(self.root):
            path.chmod(0o700)
            if path == self.root:
                break
            path = path.parent

    def write(self, path, raw):
        self.mkdir(path.parent)
        path.write_bytes(raw)
        path.chmod(0o600)

    def trusted(self, path, *, directory=False, private=False):
        """Model the host's ownership boundary with this unprivileged fixture UID.

        The production root/ancestor rule is not changed. All data operations,
        link/type checks, modes, atomic writes, renames and fsync remain real.
        """
        path = Path(path)
        if not path.is_relative_to(self.root):
            raise AssertionError('filesystem boundary escaped fixture')
        for item in (path, *[p for p in path.parents if p.is_relative_to(self.root)]):
            info = item.lstat()
            if stat.S_ISLNK(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
                raise AssertionError('fixture ownership/link boundary')
        info = path.lstat()
        if directory:
            if not stat.S_ISDIR(info.st_mode):
                raise AssertionError('directory required')
        elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise AssertionError('ordinary single-link file required')
        if private and stat.S_IMODE(info.st_mode) != (0o700 if directory else 0o600):
            raise AssertionError('fixture private mode')
        return info

    def legacy_idle(self):
        if any(row['service']['ActiveState'] != 'inactive' for row in self.legacy.values()):
            raise self.new.ScheduleError('LEGACY_BACKUP_NOT_IDLE')

    def command(self, args, *a, **kw):
        self.calls.append(('command', tuple(args)))
        if args[:3] == ['/usr/bin/docker', 'ps', '--all']:
            return '\n'.join(f'{n:012x}' for n in range(1, 9))
        if args[:3] == ['/usr/bin/docker', 'inspect', '--format']:
            n = int(args[-1], 16)
            return json.dumps({'id': args[-1], 'image': 'pinned-fixture-image', 'started': 'unchanged',
                               'restarts': 0, 'state': 'running' if n <= 6 else 'exited'})
        raise AssertionError('unexpected external command')

    def install_release(self):
        raw, sha, target = self.new.source_release()
        self.new.directory(target.parent)
        self.new.immutable(target, raw)
        self.new.immutable(target.parent / 'release.json', self.new.canonical({'format': self.new.FORMAT, 'programSha256': sha}))
        return sha, str(target)

    def logs(self, cron):
        return {str(self.log): {'sha256': repair.checksum(cron.read(self.log)),
                               'metadata': repair.signature(self.log.lstat())}}

    def before(self):
        return repair.preflight(self.old, self.new, self.panel, self.updater, OLD_GEN, NEW_GEN)

    def apply(self, before=None):
        before = before or self.before()
        return repair.apply_repair(self.old, self.new, self.panel, self.updater, before, NEW_GEN, before['stateSha256'])

    def classify(self):
        return repair.reconcile(self.old, self.new, self.panel, self.updater, OLD_GEN, NEW_GEN)

    def tree(self):
        return {str(p.relative_to(self.root)): (p.read_bytes(), p.stat().st_ino, stat.S_IMODE(p.stat().st_mode))
                for p in self.root.rglob('*') if p.is_file()}


class FakePanel:
    def __init__(self, fixture):
        self.fixture = fixture
        self.configs = {kind: fixture.old.process_config(fixture.state, kind) for kind in fixture.old.KINDS}
        self.processes = {kind: {**{k: v for k, v in fixture.old.process_payload(fixture.state, kind).items() if k != 'operate'},
            'status': [{'status': 'EXITED' if kind == 'database' else 'STOPPED', 'PID': ''}]} for kind in fixture.old.KINDS}
        self.rows = {kind: {**fixture.old.cron_payload(fixture.state, kind), 'id': repair.JOB_IDS[kind],
                           'status': 'Disable'} for kind in fixture.old.KINDS}
        self.infos = {}
        for kind in fixture.old.KINDS:
            value = {key: '' for key in repair.DTO_FIELDS}
            value.update({key: 0 for key in ('id', 'groupID', 'scriptID', 'downloadAccountID', 'retainCopies', 'retryTimes', 'timeout', 'alertCount')})
            value.update(isDir=False, ignoreErr=False, specCustom=False, scopes=None,
                         snapshotRule={'withImage': False, 'ignoreAppIDs': None}, sourceDir='preserve-this-existing-field')
            value.update(fixture.old.cron_payload(fixture.state, kind), id=repair.JOB_IDS[kind])
            self.infos[kind] = value

    def prerequisite(self):
        return None

    def jobs(self):
        return copy.deepcopy(list(self.rows.values()))

    def programs(self):
        return copy.deepcopy(list(self.processes.values()) + [{'name': self.fixture.old.COLLECTOR,
            'command': 'accepted collector', 'user': 'root', 'dir': str(self.fixture.app), 'numprocs': '1',
            'autoStart': 'true', 'autoRestart': 'true', 'environment': 'PRIVATE_FIXTURE_NOT_FOR_OUTPUT',
            'status': [{'status': 'RUNNING', 'PID': self.fixture.collector_pid}]}])

    def records(self, job_id, since):
        if job_id == 1 and since < '2026-11-08T15:05:00+00:00':
            return [{'id': 1, 'cronjobID': 1, 'status': 'Success', 'startTime': '2026-11-08T15:05:09+00:00', 'interval': 0}]
        return []

    def request(self, endpoint, payload=None, **kwargs):
        kind = next((k for k in self.fixture.old.KINDS if self.fixture.old.program(k) == (payload or {}).get('name')), None)
        if endpoint == '/hosts/tool/supervisor/process/file/get':
            if payload['file'] != 'config':
                raise AssertionError('unexpected native file request')
            return self.configs[kind]
        if endpoint == '/hosts/tool/supervisor/process/file':
            if json.loads((self.fixture.state_dir / 'state.json').read_text())['phase'] != 'STAGED_REPAIRING':
                raise AssertionError('native mutation without closed durable gate')
            if payload.get('operate') != 'update' or payload.get('file') != 'config':
                raise AssertionError('unexpected lifecycle mutation')
            self.fixture.mutations.append(('process', kind))
            if self.fixture.fail == ('process-no-commit', kind):
                raise self.fixture.new.ScheduleError('PANEL_REQUEST_OUTCOME_UNKNOWN')
            self.configs[kind] = payload['content']
            line = next(line for line in payload['content'].splitlines() if line.startswith('command='))
            self.processes[kind]['command'] = line.split('=', 1)[1]
            self.processes[kind]['status'] = [{'status': 'STOPPED', 'PID': ''}]
            if self.fixture.fail == ('collector-change', kind):
                self.fixture.collector_pid = '2770699'
            if self.fixture.fail == ('process-committed-response-lost', kind):
                raise self.fixture.new.ScheduleError('PANEL_REQUEST_OUTCOME_UNKNOWN')
            return None
        raise AssertionError('unexpected native API mutation: ' + endpoint)


class FakeUpdater(repair.UpdateAdapter):
    def __init__(self, fixture):
        self.fixture = fixture
        super().__init__(fixture.new, fixture.panel)

    def post(self, endpoint, payload):
        kind = next(k for k, value in repair.JOB_IDS.items() if value == payload['id'])
        if endpoint == '/cronjobs/load/info':
            return copy.deepcopy(self.panel.infos[kind])
        if endpoint == '/cronjobs/update':
            if json.loads((self.fixture.state_dir / 'state.json').read_text())['phase'] != 'STAGED_REPAIRING':
                raise AssertionError('job update without durable closed gate')
            self.fixture.mutations.append(('cron', kind))
            if self.fixture.fail == ('cron-no-commit', kind):
                raise repair.RepairStop('REPAIR_REQUEST_OUTCOME_UNKNOWN')
            self.panel.infos[kind] = copy.deepcopy(payload)
            self.panel.rows[kind]['script'] = payload['script']
            if self.fixture.fail == ('cron-committed-response-lost', kind):
                raise repair.RepairStop('REPAIR_REQUEST_OUTCOME_UNKNOWN')
            return None
        raise AssertionError('unexpected repair DTO endpoint')


class StagedRepairTests(unittest.TestCase):
    def test_success_preserves_history_ids_preview_other_dto_fields_and_both_code_releases(self):
        with Fixture() as f:
            before = f.before()
            old_inodes = {name: (f.state_dir / name / 'database.json').stat().st_ino for name in ('dispatch', 'runs')}
            old_logs = f.log.read_bytes()
            result = f.apply(before)
            history = f.state_dir / 'history' / OLD_GEN
            self.assertEqual((history / 'old-state.json').read_bytes(), f.raw_state)
            for name in ('dispatch', 'runs'):
                self.assertEqual((history / name / 'database.json').stat().st_ino, old_inodes[name])
                self.assertEqual(list((f.state_dir / name).iterdir()), [])
            state = f.new.load_state()
            self.assertEqual((state['generation'], state['phase'], state['owner']), (NEW_GEN, 'STAGED', 'legacy'))
            for kind in f.new.KINDS:
                self.assertEqual(state['jobs'][kind]['nativeNextPreview'], f.state['jobs'][kind]['nativeNextPreview'])
                self.assertNotIn('probe', state['jobs'][kind])
                self.assertNotIn('probeIntent', state['jobs'][kind])
                self.assertEqual(f.panel.infos[kind]['sourceDir'], 'preserve-this-existing-field')
                self.assertEqual(f.panel.rows[kind]['status'], 'Disable')
            self.assertEqual(f.log.read_bytes(), old_logs)
            self.assertEqual(len(list((f.base / 'releases').iterdir())), 2)
            self.assertEqual(f.mutations, [('process', 'database'), ('cron', 'database'), ('process', 'settings'), ('cron', 'settings')])
            self.assertEqual(result['newProbeAcceptance'], 'NOT_RUN')
            self.assertNotIn('PRIVATE_FIXTURE_NOT_FOR_OUTPUT', json.dumps(result))

    def test_reconcile_json_roundtrip_reports_exact_archive_and_has_no_side_effects(self):
        with Fixture() as f:
            f.apply()
            snapshot = f.tree()
            mutations = list(f.mutations)
            result = f.classify()
            self.assertEqual(result['stateClass'], 'new')
            self.assertEqual(result['archivedReceiptDirectoriesExact'], {'dispatch': True, 'runs': True})
            self.assertEqual(result['currentReceiptDirectories'], {'dispatch': 'empty', 'runs': 'empty'})
            self.assertTrue(all(row == {'cron': 'new', 'process': 'new'} for row in result['objects'].values()))
            self.assertEqual(f.tree(), snapshot)
            self.assertEqual(f.mutations, mutations)

    def test_commit_then_lost_response_is_read_back_once_without_a_second_mutation(self):
        for fault in ('process-committed-response-lost', 'cron-committed-response-lost'):
            with self.subTest(fault=fault), Fixture() as f:
                f.fail = (fault, 'database')
                f.apply()
                self.assertEqual(f.mutations.count(('process', 'database')), 1)
                self.assertEqual(f.mutations.count(('cron', 'database')), 1)
                progress = json.loads((f.state_dir / 'history' / OLD_GEN / 'progress.json').read_text())
                key = ('process-' if fault.startswith('process') else 'cron-') + 'database'
                self.assertFalse(progress['steps'][key]['responseConfirmed'])

    def test_unknown_uncommitted_request_stops_closed_and_readonly_reconcile_does_not_retry(self):
        with Fixture() as f:
            f.fail = ('cron-no-commit', 'database')
            with self.assertRaises(Exception):
                f.apply()
            self.assertEqual(json.loads((f.state_dir / 'state.json').read_text())['phase'], 'STAGED_REPAIRING')
            mutations, snapshot = list(f.mutations), f.tree()
            result = f.classify()
            self.assertEqual(result['objects']['database'], {'cron': 'old', 'process': 'new'})
            self.assertEqual(result['objects']['settings'], {'cron': 'old', 'process': 'old'})
            self.assertTrue(result['gateClosed'])
            self.assertFalse(result['automaticResumeAllowed'])
            self.assertEqual(f.mutations, mutations)
            self.assertEqual(f.tree(), snapshot)
            with self.assertRaises(Exception):
                f.before()
            self.assertEqual(f.mutations, mutations)

    def test_stale_state_hash_is_rejected_before_history_or_native_changes(self):
        with Fixture() as f:
            before, snapshot = f.before(), f.tree()
            with self.assertRaisesRegex(repair.RepairStop, '^EXPLICIT_STAGED_STATE_HASH_CHANGED$'):
                repair.apply_repair(f.old, f.new, f.panel, f.updater, before, NEW_GEN, '0' * 64)
            self.assertEqual(f.tree(), snapshot)
            self.assertEqual(f.mutations, [])

    def test_active_generation_and_unresolved_run_are_rejected_without_mutation(self):
        for fault in ('ACTIVE', 'STARTING'):
            with self.subTest(fault=fault), Fixture() as f:
                if fault == 'ACTIVE':
                    value = copy.deepcopy(f.state)
                    value.update(phase='ACTIVE', owner='1panel')
                    f.old.atomic(f.state_dir / 'state.json', value)
                else:
                    path = f.state_dir / 'runs/database.json'
                    value = json.loads(path.read_text())
                    value['status'] = 'STARTING'
                    f.old.atomic(path, value)
                snapshot = f.tree()
                with self.assertRaises(Exception):
                    f.before()
                self.assertEqual(f.tree(), snapshot)
                self.assertEqual(f.mutations, [])

    def test_foreign_collector_change_keeps_gate_closed_and_history_intact(self):
        with Fixture() as f:
            f.fail = ('collector-change', 'database')
            with self.assertRaisesRegex(repair.RepairStop, '^FOREIGN_COLLECTOR_CHANGED$'):
                f.apply()
            self.assertEqual(json.loads((f.state_dir / 'state.json').read_text())['phase'], 'STAGED_REPAIRING')
            self.assertEqual(f.mutations, [('process', 'database')])
            self.assertEqual((f.state_dir / 'history' / OLD_GEN / 'old-state.json').read_bytes(), f.raw_state)

    def test_archive_parent_entries_are_synced_before_gate_and_after_rename(self):
        with Fixture() as f:
            trace = []
            original_sync, original_atomic = repair.fsync_directory, f.new.atomic
            def sync(path):
                trace.append(('sync', str(path)))
                original_sync(path)
            def atomic(path, value):
                if Path(path) == f.state_dir / 'state.json':
                    trace.append(('state', value['phase']))
                return original_atomic(path, value)
            with patch.object(repair, 'fsync_directory', side_effect=sync), patch.object(f.new, 'atomic', side_effect=atomic):
                f.apply()
            gate = trace.index(('state', 'STAGED_REPAIRING'))
            self.assertIn(('sync', str(f.state_dir / 'history')), trace[:gate])
            self.assertIn(('sync', str(f.state_dir / 'history' / OLD_GEN)), trace[:gate])
            commit = trace.index(('state', 'STAGED'))
            for name in ('dispatch', 'runs'):
                position = trace.index(('sync', str(f.state_dir / name)), gate)
                self.assertLess(position, commit)
                self.assertEqual(trace[position + 1], ('sync', str(f.state_dir)))
            self.assertGreaterEqual(trace[gate:commit].count(('sync', str(f.state_dir / 'history' / OLD_GEN))), 2)

    def test_same_day_old_dispatch_cannot_suppress_a_new_generation_dispatch(self):
        with Fixture() as f:
            f.apply()
            state = f.new.load_state()
            old_archive = (f.state_dir / 'history' / OLD_GEN / 'dispatch/database.json').read_bytes()
            calls = []
            original = f.panel.request
            def no_child(endpoint, payload=None, **kwargs):
                if endpoint == '/hosts/tool/supervisor/process':
                    calls.append(payload)
                    raise f.new.ScheduleError('PANEL_REQUEST_OUTCOME_UNKNOWN')
                return original(endpoint, payload, **kwargs)
            with patch.object(f.panel, 'request', side_effect=no_child), patch('sys.stdout', new_callable=io.StringIO), \
                    self.assertRaisesRegex(f.new.ScheduleError, '^OBSERVATION_TIMEOUT_BACKUP_NOT_STOPPED$'):
                f.new.trigger(f.panel, state, 'database', NEW_GEN, budget=0)
            created = json.loads((f.state_dir / 'dispatch/database.json').read_text())
            self.assertEqual(created['generation'], NEW_GEN)
            self.assertNotEqual(created['dispatchId'], DISPATCH)
            self.assertEqual(len(calls), 1)
            self.assertEqual((f.state_dir / 'history' / OLD_GEN / 'dispatch/database.json').read_bytes(), old_archive)

    def test_plan_is_readonly_and_reconcile_handles_an_incomplete_archive_intent(self):
        with Fixture() as f:
            args = ['repair', 'plan', '--old-generation', OLD_GEN, '--new-generation', NEW_GEN,
                    '--old-program-sha256', repair.OLD_SHA]
            snapshot = f.tree()
            with patch.object(repair, 'modules', return_value=(f.old, f.new)), \
                    patch.object(f.new, 'Panel', return_value=f.panel), \
                    patch.object(repair, 'UpdateAdapter', return_value=f.updater), \
                    patch.object(repair, 'existing_idle_locks', return_value=nullcontext()), \
                    patch('sys.argv', args), patch('sys.stdout', new_callable=io.StringIO) as output:
                self.assertEqual(repair.main(), 0)
                value = json.loads(output.getvalue())
                self.assertEqual(value['status'], 'STAGED_REPAIR_PLAN_ONLY')
                self.assertNotIn('PRIVATE_FIXTURE_NOT_FOR_OUTPUT', output.getvalue())
            self.assertEqual(snapshot, f.tree())
            self.assertEqual(f.mutations, [])
            f.mkdir(f.state_dir / 'history' / OLD_GEN)
            after_mkdir = f.tree()
            result = f.classify()
            self.assertFalse(result['intentPresent'])
            self.assertEqual(result['stateClass'], 'old-closed-without-intent')
            self.assertEqual(f.tree(), after_mkdir)

    def test_adapter_refuses_enabled_job_and_readback_does_not_swallow_precondition_failure(self):
        with Fixture() as f:
            before = f.before()
            target = repair.new_state(f.new, before, NEW_GEN)
            f.panel.rows['database']['status'] = 'Enable'
            observe = unittest.mock.Mock()
            with self.assertRaises(f.new.ScheduleError):
                repair.readback_once(lambda: f.updater.update(f.state, target, 'database', before['infos']['database']),
                                     observe, {}, lambda: None)
            observe.assert_not_called()
            self.assertEqual(f.mutations, [])

    def test_full_dto_unknown_field_and_nonempty_alert_are_rejected(self):
        for field, value in (('unknownServerField', 'unexpected'), ('alertCount', 2)):
            with self.subTest(field=field), Fixture() as f:
                f.panel.infos['database'][field] = value
                with self.assertRaises(repair.RepairStop):
                    f.before()
                self.assertEqual(f.mutations, [])


class LockBoundaryTests(unittest.TestCase):
    def test_existing_locks_do_not_create_absent_per_kind_files_and_busy_ownership_stops(self):
        import fcntl
        with Fixture() as f:
            config_lock = f.root / 'config-backup.lock'
            paths = [f.old.LOCK, f.old.DB_LOCK, config_lock, f.app / '.asa/operation.lock']
            for path in paths:
                f.write(path, b'')
            original_lstat, original_fstat = Path.lstat, os.fstat
            def root_view(info, ancestors=False):
                result = {key: getattr(info, key) for key in ('st_dev', 'st_ino', 'st_uid', 'st_gid', 'st_mode',
                    'st_size', 'st_mtime_ns', 'st_ctime_ns', 'st_nlink')}
                result.update(st_uid=0, st_gid=0)
                if ancestors:
                    result['st_mode'] &= ~0o022
                return SimpleNamespace(**result)
            def lstat(path, *a, **kw):
                return root_view(original_lstat(path, *a, **kw), ancestors=not Path(path).is_relative_to(f.root))
            with patch.object(repair, 'CONFIG_BACKUP_LOCK', config_lock), patch.object(Path, 'lstat', lstat), \
                    patch.object(os, 'fstat', side_effect=lambda fd: root_view(original_fstat(fd))):
                before = {p.name for p in f.state_dir.iterdir()}
                with repair.existing_idle_locks(f.old):
                    pass
                self.assertEqual(before, {p.name for p in f.state_dir.iterdir()})
                with f.old.LOCK.open('r+b') as held:
                    fcntl.flock(held.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
                    with self.assertRaisesRegex(repair.RepairStop, '^OPERATION_OR_BACKUP_BUSY$'):
                        with repair.existing_idle_locks(f.old):
                            pass
                    fcntl.flock(held.fileno(), fcntl.LOCK_UN)
            self.assertTrue(all(path.stat().st_size == 0 for path in paths))


if __name__ == '__main__':
    unittest.main()
