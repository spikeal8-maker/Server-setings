"""Failure-path tests for native clock ownership and detached backup supervision.

Fakes model 1Panel's important non-transactional behavior: creation is enabled,
mutation responses can be lost, and native Supervisor is a separate executor.
These tests are not evidence of a real 1Panel deployment or a cloud backup.
"""
import copy
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'scripts/school-1panel-backup-schedules.py'
SPEC = importlib.util.spec_from_file_location('school_native_schedules', SOURCE)
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)


class FakeLegacy:
    def __init__(self):
        self.values = {kind: {
            'timer': {'UnitFileState': 'enabled', 'ActiveState': 'active', 'SubState': 'waiting', 'sha256': 'timer-' + kind},
            'service': {'UnitFileState': 'static', 'ActiveState': 'inactive', 'SubState': 'dead', 'sha256': 'service-' + kind},
        } for kind in m.KINDS}
        self.baseline = copy.deepcopy(self.values)
        self.disabled_calls = []
        self.restore_calls = []
        self.fail_disable = None
        self.fail_after_disable = False
        self.fail_restore = None

    def inspect(self, kind):
        return copy.deepcopy(self.values[kind])

    def idle(self):
        m.need(all(value['service'] is None or value['service']['ActiveState'] == 'inactive'
                   for value in self.values.values()), 'LEGACY_BACKUP_NOT_IDLE')

    def disabled(self, kind):
        timer = self.values[kind]['timer']
        return timer is None or (timer['UnitFileState'] == 'disabled' and timer['ActiveState'] == 'inactive')

    def disable(self, kind):
        self.disabled_calls.append(kind)
        if self.fail_disable == kind and not self.fail_after_disable:
            raise m.ScheduleError('TEST_DISABLE_FAILED')
        if self.values[kind]['timer'] is not None:
            self.values[kind]['timer'].update(UnitFileState='disabled', ActiveState='inactive', SubState='dead')
        if self.fail_disable == kind and self.fail_after_disable:
            raise m.ScheduleError('TEST_DISABLE_RESPONSE_LOST')

    def restore(self, kind, previous):
        self.restore_calls.append(kind)
        if self.fail_restore == kind:
            raise m.ScheduleError('TEST_RESTORE_FAILED')
        self.values[kind] = copy.deepcopy(previous)


class FakePanel:
    def __init__(self):
        self.crons = []
        self.processes = []
        self.configs = {}
        self.logs = {}
        self.record_rows = {}
        self.calls = []
        self.lose_create = False
        self.reject_create = False
        self.lose_status = None
        self.reject_status = None
        self.foreign_restart_on_config = False
        self.on_start = None
        self.on_handle = None

    def jobs(self):
        return copy.deepcopy(self.crons)

    def programs(self):
        return copy.deepcopy(self.processes)

    def records(self, job_id, since):
        return copy.deepcopy(self.record_rows.get(job_id, []))

    def request(self, endpoint, payload=None, method='POST'):
        payload = copy.deepcopy(payload)
        self.calls.append((endpoint, payload, method))
        if endpoint == '/hosts/tool/supervisor/process':
            if payload['operate'] == 'create':
                row = {key: value for key, value in payload.items() if key != 'operate'}
                row['status'] = [{'name': row['name'] + ':' + row['name'] + '_00', 'status': 'STOPPED', 'PID': ''}]
                self.processes.append(row)
                expected = {'command': row['command'], 'directory': row['dir'],
                    'autorestart': row['autoRestart'], 'autostart': row['autoStart'],
                    'startsecs': '3', 'user': row['user'], 'priority': '999', 'numprocs': row['numprocs'],
                    'process_name': '%(program_name)s_%(process_num)02d', 'environment': row['environment']}
                for stream, short in (('stdout', 'out'), ('stderr', 'err')):
                    expected[stream + '_logfile'] = '/opt/1panel/tools/supervisord/log/' + row['name'] + '.' + short + '.log'
                    expected[stream + '_logfile_maxbytes'] = '2MB'
                self.configs[row['name']] = '[program:' + row['name'] + ']\n' + ''.join(k + '=' + v + '\n' for k, v in expected.items())
                return None
            if payload['operate'] == 'start' and self.on_start:
                return self.on_start(payload)
            raise AssertionError('Unexpected native process mutation: ' + payload['operate'])
        if endpoint == '/hosts/tool/supervisor/process/file/get':
            return self.configs[payload['name']] if payload['file'] == 'config' else self.logs.get(payload['name'], '')
        if endpoint == '/hosts/tool/supervisor/process/file':
            assert payload['operate'] == 'update' and payload['file'] == 'config'
            self.configs[payload['name']] = payload['content']
            if self.foreign_restart_on_config:
                for row in self.processes:
                    if row['name'] == m.COLLECTOR:
                        row['status'][0]['PID'] = '999'
            return ''
        if endpoint == '/cronjobs':
            if self.reject_create:
                raise m.ScheduleError('TEST_CREATE_REJECTED')
            self.crons.append(dict(payload, id=len(self.crons) + 1, status='Enable'))
            if self.lose_create:
                raise m.ScheduleError('PANEL_REQUEST_OUTCOME_UNKNOWN')
            return None
        if endpoint == '/cronjobs/status':
            row = next(row for row in self.crons if row['id'] == payload['id'])
            if self.reject_status == (row['name'], payload['status']):
                raise m.ScheduleError('TEST_STATUS_REJECTED')
            row['status'] = payload['status']
            if self.lose_status == (row['name'], payload['status']):
                raise m.ScheduleError('PANEL_REQUEST_OUTCOME_UNKNOWN')
            return None
        if endpoint == '/cronjobs/next':
            return ['2026-11-%02d 00:20:00' % day for day in range(9, 14)]
        if endpoint == '/cronjobs/handle' and self.on_handle:
            return self.on_handle(payload)
        raise AssertionError('Unexpected native API endpoint: ' + endpoint)


class Harness:
    def __init__(self):
        self.panel = FakePanel()
        self.legacy = FakeLegacy()
        self.saves = []
        self.state = {'format': m.FORMAT, 'instance': m.INSTANCE, 'project': m.PROJECT,
            'generation': 'b' * 32, 'programSha256': 'a' * 64,
            'programPath': '/opt/asa-school-panel-schedules/releases/' + 'a' * 16 + '/' + m.NAME,
            'createdAt': m.stamp(), 'phase': 'STAGING', 'owner': 'legacy', 'originalOwner': 'legacy',
            'legacy': copy.deepcopy(self.legacy.values),
            'jobs': {kind: {'id': None, 'processConfigured': False} for kind in m.KINDS}}
        self.controller = m.Controller(self.panel, self.legacy, self.save)

    def save(self, state):
        if state['owner'] == '1panel':
            assert all(self.legacy.disabled(kind) for kind in m.KINDS)
            assert all(row['status'] == 'Enable' for row in self.panel.crons)
        self.saves.append(copy.deepcopy(state))

    def staged(self):
        self.controller.stage(self.state)
        return self

    def accepted(self):
        self.staged()
        for kind in m.KINDS:
            self.state['jobs'][kind]['probe'] = {'status': 'NATIVE_GATED_PROBE_PASS',
                'backupExecuted': False, 'programSha256': self.state['programSha256']}
        return self

    def active(self):
        self.accepted()
        self.controller.activate(self.state)
        return self


class ClockOwnershipTests(unittest.TestCase):
    def test_enabled_creation_remains_gated_and_finishes_disabled(self):
        h = Harness().staged()
        self.assertEqual(h.state['phase'], 'STAGED')
        self.assertEqual(h.state['owner'], 'legacy')
        self.assertEqual(h.legacy.disabled_calls, [])
        self.assertEqual([row['status'] for row in h.panel.crons], ['Disable', 'Disable'])
        self.assertTrue(all(state['owner'] != '1panel' for state in h.saves))
        self.assertTrue(all(row['autoStart'] == row['autoRestart'] == 'false' for row in h.panel.processes))
        self.assertTrue(all('startsecs=0\n' in value and 'startretries=0\n' in value for value in h.panel.configs.values()))

    def test_lost_create_response_is_read_back_without_duplicate(self):
        h = Harness()
        h.panel.lose_create = True
        h.controller.stage(h.state)
        h.controller.stage(h.state)
        self.assertEqual(len(h.panel.crons), 2)
        self.assertEqual(sum(call[0] == '/cronjobs' for call in h.panel.calls), 2)
        self.assertEqual(h.legacy.disabled_calls, [])

    def test_rejected_create_does_not_disable_legacy_clock(self):
        h = Harness()
        h.panel.reject_create = True
        with self.assertRaisesRegex(m.ScheduleError, 'NATIVE_CRON_MISSING'):
            h.controller.stage(h.state)
        self.assertEqual(h.legacy.values, h.legacy.baseline)
        self.assertEqual(h.state['owner'], 'legacy')

    def test_same_name_preexisting_objects_are_not_adopted_without_state(self):
        h = Harness()
        h.panel.crons.append({'name': m.cron_name('database'), 'script': 'foreign'})
        with self.assertRaisesRegex(m.ScheduleError, 'WITHOUT_OWNERSHIP'):
            m.new_state(h.panel, h.legacy, 'a' * 64, h.state['programPath'])
        self.assertEqual(h.panel.calls, [])

    def test_drifted_owned_job_is_not_silently_replaced(self):
        h = Harness().staged()
        h.panel.crons[0]['script'] = 'changed-by-operator'
        calls = len(h.panel.calls)
        with self.assertRaisesRegex(m.ScheduleError, 'PAYLOAD_CHANGED'):
            h.controller.stage(h.state)
        self.assertFalse(any(call[0] in ('/cronjobs', '/cronjobs/status') for call in h.panel.calls[calls:]))
        self.assertEqual(h.legacy.values, h.legacy.baseline)

    def test_extra_supervisor_section_cannot_be_overwritten_as_ours(self):
        h = Harness()
        payload = m.process_payload(h.state, 'database')
        h.panel.request('/hosts/tool/supervisor/process', payload)
        h.panel.configs[payload['name']] += '\n[program:foreign]\ncommand=/bin/true\n'
        with self.assertRaisesRegex(m.ScheduleError, 'CONFIG_SCOPE'):
            h.controller.stage(h.state)
        self.assertIn('[program:foreign]', h.panel.configs[payload['name']])
        self.assertEqual(h.legacy.disabled_calls, [])

    def test_native_global_reread_must_not_restart_collector(self):
        h = Harness()
        h.panel.processes.append({'name': m.COLLECTOR, 'command': '/usr/bin/python3 collect_logs.py',
            'user': 'root', 'dir': '/srv/asa-lab', 'numprocs': '1', 'autoStart': 'true',
            'autoRestart': 'true', 'environment': '', 'status': [{'status': 'RUNNING', 'PID': '123'}]})
        h.panel.foreign_restart_on_config = True
        with self.assertRaisesRegex(m.ScheduleError, 'FOREIGN_SUPERVISOR_CHANGED'):
            h.controller.stage(h.state)
        self.assertEqual(h.legacy.disabled_calls, [])

    def test_both_native_gated_probes_required_before_activation(self):
        h = Harness().staged()
        with self.assertRaisesRegex(m.ScheduleError, 'PROBE_NOT_ACCEPTED'):
            h.controller.activate(h.state)
        self.assertEqual(h.legacy.disabled_calls, [])
        self.assertTrue(all(row['status'] == 'Disable' for row in h.panel.crons))

    def test_ownership_commits_only_after_both_legacy_timers_are_disabled(self):
        h = Harness().active()
        self.assertEqual(h.legacy.disabled_calls, list(m.KINDS))
        self.assertEqual(h.state['owner'], '1panel')
        self.assertEqual(h.state['phase'], 'ACTIVE')
        self.assertEqual(h.controller.activate(h.state)['status'], 'NATIVE_CLOCK_ALREADY_ACTIVE')
        self.assertEqual(h.legacy.disabled_calls, list(m.KINDS))

    def test_lost_native_status_response_reconciles_by_readback(self):
        h = Harness().accepted()
        h.panel.lose_status = (m.cron_name('database'), 'Enable')
        h.controller.activate(h.state)
        enables = [call for call in h.panel.calls if call[0] == '/cronjobs/status'
                   and call[1] == {'id': 1, 'status': 'Enable'}]
        self.assertEqual(len(enables), 1)
        self.assertEqual(h.state['owner'], '1panel')

    def test_partial_timer_handover_restores_original_clocks(self):
        for after in (False, True):
            with self.subTest(response_lost_after_mutation=after):
                h = Harness().accepted()
                h.legacy.fail_disable = 'settings'
                h.legacy.fail_after_disable = after
                with self.assertRaisesRegex(m.ScheduleError, 'ORIGINAL_CLOCK_RESTORED'):
                    h.controller.activate(h.state)
                self.assertEqual(h.legacy.values, h.legacy.baseline)
                self.assertEqual(h.state['phase'], 'ROLLED_BACK')
                self.assertEqual(h.state['owner'], 'legacy')
                self.assertTrue(all(row['status'] == 'Disable' for row in h.panel.crons))
                self.assertEqual(len(h.panel.crons), 2)

    def test_failed_compensation_keeps_gate_closed_and_reports_recovery(self):
        h = Harness().accepted()
        h.legacy.fail_disable = 'settings'
        h.legacy.fail_restore = 'database'
        with self.assertRaisesRegex(m.ScheduleError, 'RECOVERY_REQUIRED'):
            h.controller.activate(h.state)
        self.assertEqual(h.state['phase'], 'RECOVERY_REQUIRED')
        self.assertEqual(h.state['owner'], 'none')
        self.assertIn('LEGACY_RESTORE_DATABASE', h.state['recoveryFailures'])

    def test_rollback_never_stops_live_supervisor_backup(self):
        h = Harness().active()
        h.panel.processes[0]['status'][0].update(status='RUNNING', PID='321')
        before = len(h.panel.calls)
        with self.assertRaisesRegex(m.ScheduleError, 'RECOVERY_REQUIRED'):
            h.controller.restore_original(h.state)
        self.assertEqual(h.legacy.restore_calls, [])
        self.assertFalse(any(call[0] == '/hosts/tool/supervisor/process' for call in h.panel.calls[before:]))
        self.assertEqual(h.state['owner'], 'none')

    def test_fresh_profile_refuses_any_existing_legacy_unit(self):
        h = Harness()
        with self.assertRaisesRegex(m.ScheduleError, 'FRESH_MODE_LEGACY_EXISTS'):
            m.new_state(h.panel, h.legacy, 'a' * 64, h.state['programPath'], fresh=True)

    def test_fresh_profile_has_no_implicit_systemd_clock(self):
        h = Harness()
        h.legacy.values = {kind: {'timer': None, 'service': None} for kind in m.KINDS}
        state = m.new_state(h.panel, h.legacy, 'a' * 64, h.state['programPath'], fresh=True)
        self.assertEqual(state['owner'], 'none')
        self.assertEqual(state['originalOwner'], 'none')
        self.assertEqual(h.legacy.disabled_calls, [])


class RuntimeDispatchTests(unittest.TestCase):
    def test_gated_worker_cannot_run_exporter(self):
        h = Harness().staged()
        state = copy.deepcopy(h.state)
        state['programPath'] = str(SOURCE)
        intent = {'generation': state['generation'], 'kind': 'database', 'phase': 'STAGED',
                  'programSha256': state['programSha256'], 'dispatchId': 'c' * 32, 'createdAt': m.stamp()}
        writes = []
        with patch.object(m, 'file_json', return_value=intent), patch.object(m, 'runtime_state', return_value=None), \
             patch.object(m, 'verify_runners'), patch.object(m, 'atomic', side_effect=lambda p, v: writes.append(copy.deepcopy(v))), \
             patch.object(m.subprocess, 'Popen') as child, redirect_stdout(io.StringIO()):
            result = m.worker(state, 'database', state['generation'])
        self.assertEqual(result['status'], 'STAGED_GATE_VERIFIED_NO_BACKUP')
        self.assertFalse(result['backupExecuted'])
        child.assert_not_called()
        self.assertTrue(all(value['backupExecuted'] is False for value in writes))

    def test_worker_refuses_ambiguous_replayed_dispatch(self):
        h = Harness().staged()
        state = copy.deepcopy(h.state)
        state['programPath'] = str(SOURCE)
        intent = {'generation': state['generation'], 'kind': 'database', 'phase': 'STAGED',
                  'programSha256': state['programSha256'], 'dispatchId': 'c' * 32, 'createdAt': m.stamp()}
        with patch.object(m, 'file_json', return_value=intent), \
             patch.object(m, 'runtime_state', return_value={'dispatchId': intent['dispatchId']}), \
             patch.object(m.subprocess, 'Popen') as child:
            with self.assertRaisesRegex(m.ScheduleError, 'DISPATCH_REPLAY'):
                m.worker(state, 'database', state['generation'])
        child.assert_not_called()

    def test_worker_refuses_old_start_intent(self):
        h = Harness().staged()
        state = copy.deepcopy(h.state)
        state['programPath'] = str(SOURCE)
        intent = {'generation': state['generation'], 'kind': 'database', 'phase': 'STAGED',
                  'programSha256': state['programSha256'], 'dispatchId': 'c' * 32,
                  'createdAt': (datetime.now(timezone.utc) - timedelta(seconds=61)).isoformat()}
        with patch.object(m, 'file_json', return_value=intent), patch.object(m.subprocess, 'Popen') as child:
            with self.assertRaisesRegex(m.ScheduleError, 'DISPATCH_TOO_OLD'):
                m.worker(state, 'database', state['generation'])
        child.assert_not_called()

    def test_unresolved_dispatch_is_never_automatically_started_twice(self):
        h = Harness().active()
        old = {'dispatchId': 'c' * 32, 'phase': 'ACTIVE', 'status': 'START_REQUEST_RECORDED'}
        with patch.object(m, 'verify_runners'), patch.object(m, 'Legacy', return_value=h.legacy), \
             patch.object(m, 'file_json', return_value=old), patch.object(m, 'runtime_state', return_value=None):
            with self.assertRaisesRegex(m.ScheduleError, 'PRIOR_DISPATCH_OUTCOME_UNRESOLVED'):
                m.trigger(h.panel, h.state, 'database', h.state['generation'], budget=0)
        self.assertFalse(any(call[0] == '/hosts/tool/supervisor/process' and call[1]['operate'] == 'start' for call in h.panel.calls))

    def test_lost_start_response_observes_same_worker(self):
        h = Harness().active()
        storage = {}
        run = {}
        def save(path, value):
            storage[str(path)] = copy.deepcopy(value)
        def start(payload):
            intent = storage[str(m.STATE / 'dispatch/database.json')]
            run.update(dispatchId=intent['dispatchId'], status='BACKUP_CLOUD_SHA256_PASS', finishedAt=m.stamp())
            raise m.ScheduleError('PANEL_REQUEST_OUTCOME_UNKNOWN')
        h.panel.on_start = start
        with patch.object(m, 'verify_runners'), patch.object(m, 'Legacy', return_value=h.legacy), \
             patch.object(m, 'file_json', side_effect=lambda p: storage.get(str(p))), \
             patch.object(m, 'runtime_state', side_effect=lambda k: run or None), \
             patch.object(m, 'atomic', side_effect=save), redirect_stdout(io.StringIO()):
            result = m.trigger(h.panel, h.state, 'database', h.state['generation'], budget=1)
        self.assertEqual(result['status'], 'NATIVE_RUN_RESULT_OBSERVED')
        self.assertEqual(sum(call[0] == '/hosts/tool/supervisor/process' and call[1]['operate'] == 'start' for call in h.panel.calls), 1)

    def test_observer_deadline_does_not_stop_native_worker(self):
        h = Harness().active()
        storage = {}
        def start(payload):
            h.panel.processes[0]['status'][0].update(status='RUNNING', PID='222')
        h.panel.on_start = start
        with patch.object(m, 'verify_runners'), patch.object(m, 'Legacy', return_value=h.legacy), \
             patch.object(m, 'file_json', return_value=None), patch.object(m, 'runtime_state', return_value=None), \
             patch.object(m, 'atomic', side_effect=lambda p, v: storage.update({str(p): copy.deepcopy(v)})), \
             redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(m.ScheduleError, 'BACKUP_NOT_STOPPED'):
                m.trigger(h.panel, h.state, 'database', h.state['generation'], budget=0)
        self.assertEqual(h.panel.processes[0]['status'][0]['status'], 'RUNNING')
        self.assertEqual(storage[str(m.STATE / 'dispatch/database.json')]['status'], 'OBSERVER_DEADLINE_BACKUP_NOT_STOPPED')
        self.assertFalse(any(call[0] == '/hosts/tool/supervisor/process' and call[1]['operate'] in ('stop', 'restart', 'delete') for call in h.panel.calls))

    def test_failed_same_day_backup_cannot_be_relabelled_success_or_retried(self):
        h = Harness().active()
        old = {'dispatchId': 'c' * 32, 'phase': 'ACTIVE', 'dayMoscow': datetime.now(m.MOSCOW).date().isoformat()}
        failed = {'dispatchId': 'c' * 32, 'status': 'BACKUP_STOP', 'finishedAt': m.stamp()}
        with patch.object(m, 'verify_runners'), patch.object(m, 'Legacy', return_value=h.legacy), \
             patch.object(m, 'file_json', return_value=old), patch.object(m, 'runtime_state', return_value=failed):
            with self.assertRaisesRegex(m.ScheduleError, 'FAILED_REVIEW_REQUIRED'):
                m.trigger(h.panel, h.state, 'database', h.state['generation'], budget=0)
        self.assertFalse(any(call[0] == '/hosts/tool/supervisor/process' and call[1]['operate'] == 'start' for call in h.panel.calls))

    def test_gated_probe_lost_handle_response_is_observed_once(self):
        h = Harness().staged()
        observed = {}
        def handle(payload):
            observed.update(status='STAGED_GATE_VERIFIED_NO_BACKUP', generation=h.state['generation'],
                programSha256=h.state['programSha256'], backupExecuted=False, finishedAt=m.stamp(), dispatchId='c' * 32)
            h.panel.record_rows[payload['id']] = [{'id': 15, 'cronjobID': payload['id'], 'status': 'Success'}]
            h.panel.logs[m.program('database')] = json.dumps(observed)
            raise m.ScheduleError('PANEL_REQUEST_OUTCOME_UNKNOWN')
        h.panel.on_handle = handle
        with patch.object(m, 'Legacy', return_value=h.legacy), patch.object(m, 'save_state', side_effect=h.save), \
             patch.object(m, 'runtime_state', side_effect=lambda k: observed or None):
            result = m.probe(h.panel, h.state, 'database')
            again = m.probe(h.panel, h.state, 'database')
        self.assertEqual(result, again)
        self.assertFalse(result['backupExecuted'])
        self.assertEqual(sum(call[0] == '/cronjobs/handle' for call in h.panel.calls), 1)

    def test_probe_does_not_repeat_ambiguous_prior_handle(self):
        h = Harness().staged()
        h.state['jobs']['database']['probeIntent'] = {'at': m.stamp(), 'previousRecordIds': []}
        with patch.object(m, 'Legacy', return_value=h.legacy), \
             patch.object(m.time, 'monotonic', side_effect=[0, 41]):
            with self.assertRaisesRegex(m.ScheduleError, 'DO_NOT_REDISPATCH'):
                m.probe(h.panel, h.state, 'database')
        self.assertFalse(any(call[0] == '/cronjobs/handle' for call in h.panel.calls))


class GuardTests(unittest.TestCase):
    def test_readonly_preflight_does_not_create_native_or_local_objects(self):
        h = Harness()
        with patch.object(h.panel, 'prerequisite', create=True), \
             patch.object(m, 'handover_window'), patch.object(m, 'verify_database_transport'), \
             patch.object(m, 'source_release', return_value=(b'public', 'a' * 64, Path(h.state['programPath']))), \
             patch.object(m, 'read', return_value=b'public'), patch.object(m, 'digest', return_value='pinned'), \
             patch.object(m, 'SETTINGS_FILES', {'helper.py': 'pinned'}), \
             patch.object(m.Path, 'exists', return_value=False), patch.object(m.Path, 'is_symlink', return_value=False), \
             patch.object(m, 'atomic') as writes:
            result = m.preflight(h.panel, h.legacy)
        self.assertEqual(result['status'], 'NATIVE_CLOCK_READONLY_PREFLIGHT_PASS')
        self.assertFalse(result['changes'])
        self.assertEqual(h.panel.calls, [])
        self.assertEqual(h.legacy.disabled_calls, [])
        writes.assert_not_called()

    def test_plan_and_unapplied_mutation_do_not_touch_host(self):
        for argv in (['script'], ['script', 'stage'], ['script', 'activate']):
            with self.subTest(argv=argv), patch.object(m.sys, 'argv', argv), \
                 patch.object(m, 'check_host') as host, redirect_stdout(io.StringIO()) as out:
                self.assertEqual(m.main(), 0)
                self.assertEqual(json.loads(out.getvalue())['status'], 'PLAN_ONLY')
                host.assert_not_called()

    def test_moscow_times_do_not_depend_on_local_system_timezone(self):
        now = datetime(2026, 11, 8, 10, 0, tzinfo=timezone.utc)
        self.assertEqual(m.intended_next('database', now).isoformat(), '2026-11-09T00:20:00+00:00')
        self.assertEqual(m.intended_next('settings', now).isoformat(), '2026-11-09T01:00:00+00:00')
        m.handover_window(now)
        with self.assertRaisesRegex(m.ScheduleError, '0245_TO_0515'):
            m.handover_window(datetime(2026, 11, 8, 0, 0, tzinfo=timezone.utc))

    def test_unsafe_native_endpoints_are_not_available_in_client(self):
        client = m.Panel.__new__(m.Panel)
        for endpoint in ('/cronjobs/stop', '/cronjobs/del', '/cronjobs/records/clean', '/hosts/tool/init'):
            with self.subTest(endpoint=endpoint), self.assertRaisesRegex(m.ScheduleError, 'ENDPOINT_SCOPE'):
                client.request(endpoint, {})
        for operation in ('stop', 'restart', 'delete'):
            with self.subTest(operation=operation), self.assertRaisesRegex(m.ScheduleError, 'MUTATION_SCOPE'):
                client.request('/hosts/tool/supervisor/process', {'name': m.program('database'), 'operate': operation})
        with self.assertRaisesRegex(m.ScheduleError, 'MUTATION_SCOPE'):
            client.request('/hosts/tool/supervisor/process', {'name': m.COLLECTOR, 'operate': 'start'})

    def test_runner_receipt_cannot_be_stale_or_reused(self):
        prior = {'status': 'verified_cloud_sha256', 'archive': 'old.tar.age',
                 'createdAt': '2026-11-01T00:20:00+00:00'}
        with patch.object(m, 'file_json', return_value=prior):
            with self.assertRaisesRegex(m.ScheduleError, 'NOT_ADVANCED'):
                m.receipt_after('database', prior, datetime.now(timezone.utc))
        with patch.object(m, 'file_json', return_value=dict(prior, archive='different.tar.age')):
            with self.assertRaisesRegex(m.ScheduleError, 'PREDATES'):
                m.receipt_after('database', prior, datetime.now(timezone.utc))

    def test_fresh_other_backup_receipt_cannot_be_attributed_to_this_run(self):
        current = {'status': 'verified_cloud_sha256',
            'archive': 'asa-school-01-20261108T100000Z-ffffffff.tar.age', 'createdAt': m.stamp()}
        with patch.object(m, 'file_json', return_value=current):
            with self.assertRaisesRegex(m.ScheduleError, 'ARCHIVE_CHANGED'):
                m.receipt_after('database', None, datetime.now(timezone.utc) - timedelta(seconds=2),
                    'asa-school-01-20261108T100000Z-aaaaaaaa.tar.age')

    def test_each_runner_output_binds_its_own_archive(self):
        name = 'asa-school-01-20261108T100000Z-aaaaaaaa.tar.age'
        raw = ('BACKUP_TRIGGER=scheduled\nNIGHTLY_DATABASE_LOCAL_ENCRYPTED=' + name + '\n').encode()
        self.assertEqual(m.archive_from_output('database', raw), name)
        settings_name = 'asa-school-config-01-20261108T100000Z-bbbbbbbb.tar.age'
        raw = json.dumps({'status': 'CONFIG_NIGHTLY_FULL_DOWNLOAD_SHA256_PASS', 'archive': settings_name}).encode()
        self.assertEqual(m.archive_from_output('settings', raw), settings_name)

    def test_ambiguous_runner_output_does_not_get_success_receipt(self):
        name = 'asa-school-01-20261108T100000Z-aaaaaaaa.tar.age'
        duplicate = (('NIGHTLY_DATABASE_LOCAL_ENCRYPTED=' + name + '\n') * 2).encode()
        with self.assertRaisesRegex(m.ScheduleError, 'ARCHIVE_IDENTITY_UNPROVEN'):
            m.archive_from_output('database', duplicate)
        with self.assertRaisesRegex(m.ScheduleError, 'OUTPUT_BOUND'):
            m.archive_from_output('database', b'x' * (512 * 1024 + 1))

    def test_native_failure_summary_selects_codes_without_forwarding_raw_output(self):
        raw = b'unrelated private diagnostic\nSCHOOL_NIGHTLY_BACKUP_BLOCKED=DATABASE_LOCK_BUSY\n' \
            b'<3>{"status":"CONFIG_NIGHTLY_STOP","code":"CLOUD_UPLOAD_FAILED"}\n' \
            b'SCHOOL_NIGHTLY_BACKUP_BLOCKED=invalid raw value with spaces\n'
        self.assertEqual(m.runner_failure_codes(raw), ['DATABASE_LOCK_BUSY', 'CLOUD_UPLOAD_FAILED'])

    @unittest.skipUnless(os.geteuid() == 0, 'positive root-only filesystem invariants require root')
    def test_atomic_private_receipt_and_symlink_rejection(self):
        with tempfile.TemporaryDirectory(prefix='school-panel-cron-test-', dir='/root') as tmp:
            directory = Path(tmp)
            target = directory / 'state.json'
            m.atomic(target, {'state': 'closed'})
            self.assertEqual(m.read(target, private=True), b'{"state":"closed"}\n')
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            link = directory / 'alias.json'
            link.symlink_to(target)
            with self.assertRaisesRegex(m.ScheduleError, 'PATH_UNTRUSTED'):
                m.atomic(link, {'state': 'open'})
            self.assertEqual(json.loads(target.read_text()), {'state': 'closed'})


if __name__ == '__main__':
    unittest.main()
