"""Exercise the active generation transaction with real Linux files and a scoped native API fixture."""

import copy
import json
import unittest
from unittest.mock import patch

from test_school_staged_repair import CODE, DISPATCH, Fixture, NEW_GEN, OLD_GEN, load

m = load('active_settings_transition_under_test', CODE / 'school-1panel-active-settings-transition.py')


class ActiveFixture(Fixture):
    def __enter__(self):
        super().__enter__()
        self.state.update(phase='ACTIVE', owner='1panel')
        self.write(self.state_dir / 'state.json', self.old.canonical(self.state) + b'\n')
        self.legacy_object.disabled = lambda kind: True
        for row in self.panel.rows.values():
            row['status'] = 'Enable'
        self.transition = m.Transition(self.old, self.new, self.panel,
                                       self.old.source_release()[1], self.new.source_release()[1], OLD_GEN, NEW_GEN)
        self.transition.updater = self.updater
        self.transition.path = self.state_dir / 'state.json'
        self.transition.history = self.state_dir / 'history' / OLD_GEN
        self.stack.enter_context(patch.object(m, 'BASE', self.base))
        self.stack.enter_context(patch.object(m, 'STATE', self.state_dir))
        self.panel.request = self.request
        self.updater.post = self.post
        self.new.SETTINGS_BASE = self.root / 'settings'
        self.new.command = self.command
        return self

    def command(self, args, *a, **kw):
        if args[0] == '/usr/bin/python3':
            if self.fail == ('settings-check', 'settings'):
                raise self.new.ScheduleError('CANDIDATE_CHECK_FAILED')
            return json.dumps({'status': 'CONFIG_SCHEDULE_READONLY_CHECK_PASS', 'catalogsReady': True,
                'changes': False, 'releaseSha256': self.new.digest(self.new.canonical(self.new.SETTINGS_FILES))})
        return super().command(args, *a, **kw)

    def install_release(self):
        result = super().install_release()
        self.mkdir(self.new.settings_release())
        return result

    def request(self, endpoint, payload=None, **kwargs):
        kind = next((k for k in self.old.KINDS if self.old.program(k) == (payload or {}).get('name')), None)
        if endpoint == '/hosts/tool/supervisor/process/file/get':
            return self.panel.configs[kind]
        state = json.loads((self.state_dir / 'state.json').read_text())
        if state['phase'] != 'ACTIVE_SETTINGS_TRANSITION':
            raise AssertionError('mutation without durable gate')
        if endpoint == '/cronjobs/status':
            kind = next(k for k, row in self.panel.rows.items() if row['id'] == payload['id'])
            self.mutations.append(('status', kind, payload['status']))
            self.panel.rows[kind]['status'] = payload['status']
        elif endpoint == '/hosts/tool/supervisor/process/file':
            self.mutations.append(('process', kind))
            if self.fail == ('process-no-commit', kind):
                raise self.new.ScheduleError('PANEL_REQUEST_OUTCOME_UNKNOWN')
            self.panel.configs[kind] = payload['content']
            line = next(line for line in payload['content'].splitlines() if line.startswith('command='))
            self.panel.processes[kind]['command'] = line.split('=', 1)[1]
        else:
            raise AssertionError('unexpected native write')

    def post(self, endpoint, payload):
        kind = next(k for k, row in self.panel.rows.items() if row['id'] == payload['id'])
        if endpoint == '/cronjobs/load/info':
            return copy.deepcopy(self.panel.infos[kind])
        if endpoint != '/cronjobs/update' or self.panel.rows[kind]['status'] != 'Disable':
            raise AssertionError('unexpected native update')
        self.mutations.append(('cron', kind))
        self.panel.infos[kind] = copy.deepcopy(payload)
        self.panel.rows[kind]['script'] = payload['script']
        if self.fail == ('cron-committed-response-lost', kind):
            raise m.shared.RepairStop('REPAIR_REQUEST_OUTCOME_UNKNOWN')

    def switch(self):
        plan = self.transition.plan()
        return self.transition.apply(plan['oldStateSha256']), plan['oldStateSha256']


class ActiveTransitionTests(unittest.TestCase):
    def test_plan_is_read_only_and_switch_preserves_job_ids_and_history(self):
        with ActiveFixture() as f:
            before = f.tree()
            plan = f.transition.plan()
            self.assertEqual(f.tree(), before)
            self.assertEqual(f.mutations, [])
            result = f.transition.apply(plan['oldStateSha256'])
            self.assertEqual(result['nativeJobIds'], {'database': 1, 'settings': 2})
            self.assertFalse(result['backupExecuted'])
            self.assertEqual(f.new.load_state()['generation'], NEW_GEN)
            self.assertEqual(f.new.load_state()['owner'], '1panel')
            for kind, row in f.panel.rows.items():
                self.assertEqual(row['status'], 'Enable')
                self.assertEqual(row['spec'], f.old.spec(kind))
                self.assertEqual(row['id'], m.JOB_IDS[kind])
            history = f.transition.history
            self.assertEqual((history / 'dispatch' / 'database.json').stat().st_ino,
                             before['state/dispatch/database.json'][1])
            self.assertEqual(f.log.read_bytes(), before['native-database.out.log'][0])

    def test_successful_switch_can_roll_back_before_next_dispatch(self):
        with ActiveFixture() as f:
            result, sha = f.switch()
            self.assertEqual(f.transition.reconcile(sha)['generation'], NEW_GEN)
            self.assertEqual(f.transition.reconcile(sha, apply=True)['generation'], OLD_GEN)
            self.assertEqual(f.old.load_state(), f.state)
            self.assertEqual(f.old.runtime_state('database')['dispatchId'], DISPATCH)
            self.assertTrue(all(row['status'] == 'Enable' for row in f.panel.rows.values()))

    def test_ambiguous_uncommitted_request_leaves_gate_closed_then_can_roll_back(self):
        with ActiveFixture() as f:
            f.fail = ('process-no-commit', 'settings')
            sha = f.transition.plan()['oldStateSha256']
            with self.assertRaisesRegex(m.shared.RepairStop, 'PROCESS_UPDATE_READBACK'):
                f.transition.apply(sha)
            inspection = f.transition.reconcile(sha)
            self.assertEqual(inspection['phase'], 'ACTIVE_SETTINGS_TRANSITION')
            self.assertEqual(inspection['objects']['database']['cron'], 'new')
            self.assertEqual(inspection['objects']['settings']['process'], 'old')
            self.assertEqual(f.mutations.count(('process', 'settings')), 1)
            f.fail = None
            self.assertEqual(f.transition.reconcile(sha, apply=True)['generation'], OLD_GEN)

    def test_committed_request_with_lost_response_is_read_back_once(self):
        with ActiveFixture() as f:
            f.fail = ('cron-committed-response-lost', 'settings')
            result, sha = f.switch()
            self.assertEqual(result['generation'], NEW_GEN)
            self.assertEqual(f.mutations.count(('cron', 'settings')), 1)

    def test_changed_state_or_unhealthy_candidate_blocks_native_writes(self):
        for fault in ('state', 'runner'):
            with self.subTest(fault=fault), ActiveFixture() as f:
                sha = f.transition.plan()['oldStateSha256']
                if fault == 'runner':
                    f.fail = ('settings-check', 'settings')
                else:
                    sha = '0' * 64
                with self.assertRaises((m.shared.RepairStop, f.new.ScheduleError)):
                    f.transition.apply(sha)
                self.assertEqual(f.old.load_state(), f.state)
                self.assertEqual(f.mutations, [])

    def test_rollback_after_new_dispatch_refuses_without_further_mutation(self):
        with ActiveFixture() as f:
            _, sha = f.switch()
            f.write(f.state_dir / 'dispatch' / 'settings.json', b'{}')
            before = list(f.mutations)
            with self.assertRaisesRegex(m.shared.RepairStop, 'NEW_GENERATION_ALREADY_DISPATCHED'):
                f.transition.reconcile(sha, apply=True)
            self.assertEqual(f.mutations, before)
            self.assertEqual(f.new.load_state()['generation'], NEW_GEN)


if __name__ == '__main__':
    unittest.main()
