#!/usr/bin/env python3
"""Switch the managed 1Panel backup workers to an accepted settings release."""

import argparse
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import stat
import struct
import sys
import tempfile

sys.dont_write_bytecode = True
HERE = Path(__file__).absolute().parent
BASE = Path('/opt/asa-school-panel-schedules')
STATE = Path('/var/lib/server-settings/1panel-backup-schedules')
HOST = 'asa-school-01'
NAME = 'school-1panel-backup-schedules.py'
FORMAT = 'asa-active-settings-transition-v1'
SUPERVISOR_CONFIG = Path('/etc/supervisor/supervisord.conf')
SUPERVISOR_CTL = Path('/usr/bin/supervisorctl')
SUPERVISOR_SOCKET = Path('/run/supervisor.sock')
AGENT_SOCKET = Path('/etc/1panel/agent.sock')
AGENT_EXE = Path('/usr/local/bin/1panel-agent')
AGENT_SHA256 = 'e12321d3a0d796310066aa77ff9dc916b2b69c4f3efa34716b51c2f02595a97d'
UNCERTAIN_CRON = ('NATIVE_CRON_STATUS_REQUEST', 'NATIVE_CRON_STATUS_UNKNOWN')

spec = importlib.util.spec_from_file_location('staged_repair_contract', HERE / 'school-1panel-staged-repair.py')
shared = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shared)
need = shared.need
JOB_IDS = shared.JOB_IDS


def active(state, module, digest, generation):
    need(state.get('format') == module.FORMAT and state.get('instance') == module.INSTANCE
         and state.get('project') == module.PROJECT and state.get('phase') == 'ACTIVE'
         and state.get('owner') == '1panel' and state.get('originalOwner') in ('legacy', 'none')
         and state.get('clockTimeZone') == 'Europe/Moscow'
         and state.get('programSha256') == digest and state.get('generation') == generation
         and state.get('programPath') == str(BASE / 'releases' / digest[:16] / NAME)
         and set(state.get('jobs', {})) == set(JOB_IDS)
         and all(type(state['jobs'][k].get('id')) is int and state['jobs'][k]['id'] == i
                 and state['jobs'][k].get('processConfigured') is True for k, i in JOB_IDS.items()),
         'EXACT_ACTIVE_GENERATION_REQUIRED')


def validate_terminal_receipts(cron, state):
    for kind in cron.KINDS:
        dispatch = cron.file_json(cron.STATE / 'dispatch' / (kind + '.json'))
        run = cron.runtime_state(kind)
        if dispatch is None and run is None:
            continue
        need(isinstance(dispatch, dict) and isinstance(run, dict)
             and dispatch.get('status') == 'OBSERVED_COMPLETE'
             and run.get('status') in ('BACKUP_CLOUD_SHA256_PASS', 'BACKUP_STOP', 'STAGED_GATE_VERIFIED_NO_BACKUP')
             and dispatch.get('phase') in ('ACTIVE', 'STAGED')
             and isinstance(dispatch.get('finishedAt'), str) and isinstance(run.get('finishedAt'), str)
             and all(r.get('kind') == kind and r.get('generation') == state['generation']
                     and r.get('programSha256') == state['programSha256'] for r in (dispatch, run))
             and re.fullmatch('[a-f0-9]{32}', str(dispatch.get('dispatchId')))
             and run.get('dispatchId') == dispatch['dispatchId'], 'OLD_DISPATCH_NOT_TERMINAL')


def loaded_supervisor_config(cron):
    """Use the same privileged local control socket as the reviewed daemon."""
    cron.trusted(SUPERVISOR_CTL)
    cron.trusted(SUPERVISOR_CONFIG)
    cron.trusted(SUPERVISOR_SOCKET.parent, directory=True)
    socket_stat = SUPERVISOR_SOCKET.lstat()
    need(stat.S_ISSOCK(socket_stat.st_mode) and socket_stat.st_uid == 0
         and not socket_stat.st_mode & 0o022, 'SUPERVISOR_SOCKET_UNTRUSTED')
    output = cron.command([str(SUPERVISOR_CTL), '-c', str(SUPERVISOR_CONFIG),
                           '-s', 'unix://' + str(SUPERVISOR_SOCKET), 'reread'], 25)
    return output.strip() == 'No config updates to processes'


def agent_epoch(cron):
    """Read the identity of the agent actually listening on the local socket."""
    try:
        cron.trusted(AGENT_SOCKET.parent, directory=True, private=True)
        executable = cron.trusted(AGENT_EXE)
        sock_stat = AGENT_SOCKET.lstat()
        need(stat.S_ISSOCK(sock_stat.st_mode) and sock_stat.st_uid == 0
             and not sock_stat.st_mode & 0o077, 'PANEL_AGENT_SOCKET_UNTRUSTED')
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.settimeout(3)
            conn.connect(str(AGENT_SOCKET))
            pid, uid, _ = struct.unpack('3i', conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            need(pid > 0 and uid == 0, 'PANEL_AGENT_PEER_UNTRUSTED')
            proc = Path('/proc') / str(pid)
            live_exe = (proc / 'exe').stat()
            need((live_exe.st_dev, live_exe.st_ino) == (executable.st_dev, executable.st_ino),
                 'PANEL_AGENT_EXECUTABLE_CHANGED')
            start_ticks = (proc / 'stat').read_text().rsplit(') ', 1)[1].split()[19]
            boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
            need(start_ticks.isdecimal() and re.fullmatch('[a-f0-9-]{36}', boot), 'PANEL_AGENT_EPOCH_INVALID')
            return {'boot': boot, 'pid': pid, 'startTicks': int(start_ticks)}
    except (OSError, IndexError, ValueError):
        raise shared.RepairStop('PANEL_AGENT_IDENTITY_UNAVAILABLE') from None


def agent_startup_log(cron, epoch):
    fields = ('MainPID', 'InvocationID', 'ActiveState', 'SubState')
    observed = dict(line.split('=', 1) for line in cron.command(['/usr/bin/systemctl', 'show',
        '1panel-agent.service', '--property=' + ','.join(fields)], 8).splitlines())
    invocation = observed.get('InvocationID', '')
    need(observed.get('ActiveState') == 'active' and observed.get('SubState') == 'running'
         and observed.get('MainPID') == str(epoch['pid']) and re.fullmatch('[a-f0-9]{32}', invocation),
         'PANEL_AGENT_SYSTEMD_IDENTITY_CHANGED')
    return cron.command(['/usr/bin/journalctl', '-u', '1panel-agent.service', '--output=cat',
        '--no-pager', '--lines=500', '_SYSTEMD_INVOCATION_ID=' + invocation, '_PID=' + str(epoch['pid'])], 15)


def verify_agent_build():
    with AGENT_EXE.open('rb') as executable:
        need(hashlib.file_digest(executable, 'sha256').hexdigest() == AGENT_SHA256, 'PANEL_AGENT_BUILD_CHANGED')


def verify_agent_startup(cron, epoch, jobs):
    verify_agent_build()
    output = agent_startup_log(cron, epoch)
    markers = output.split('agent startup: cron initialized', 1)
    need(len(markers) == 2 and 'agent startup: cron initialized' not in markers[1],
         'PANEL_CRON_STARTUP_NOT_OBSERVED')
    expected_entries = 0
    for _, name, status, spec in jobs:
        if status != 'Enable':
            continue
        registrations = len(spec.split('&&'))
        pattern = r'add \S+ job ' + re.escape(name) + r' successful\r?$'
        need(len(re.findall(pattern, markers[0], re.MULTILINE)) == registrations,
             'PANEL_CRON_JOB_STARTUP_NOT_OBSERVED')
        expected_entries += registrations
    entry_ids = re.findall(r'start cronjob entryID: ([1-9][0-9]*)\r?$', markers[0], re.MULTILINE)
    need(len(entry_ids) == len(set(entry_ids)) == expected_entries, 'PANEL_CRON_STARTUP_COUNT_CHANGED')
    need(not re.search(r'start my cronjob failed|start .+ job failed|update cronjob .+ failed', markers[0]),
         'PANEL_CRON_STARTUP_FAILED')


class Transition:
    def __init__(self, old, new, panel, old_sha, new_sha, generation, target_generation):
        self.old, self.new, self.panel = old, new, panel
        self.old_sha, self.new_sha = old_sha, new_sha
        self.generation, self.target_generation = generation, target_generation
        self.updater = shared.UpdateAdapter(new, panel)
        self.path = STATE / 'state.json'
        self.history = STATE / 'history' / (generation + '-' + target_generation)
        self.previous = self.target = self.intent = None

    def safety(self):
        self.old.check_host()
        self.panel.prerequisite()
        self.old.handover_window()
        legacy = self.old.Legacy()
        legacy.idle()
        need(all(legacy.disabled(kind) for kind in self.old.KINDS), 'DUPLICATE_CLOCK_DETECTED')

    def environment(self):
        return {'application': shared.application(self.old), 'collector': self.foreign(),
                'logs': shared.backup_logs(self.old)}

    def foreign(self):
        return self.old.foreign_snapshot(self.panel.programs())

    def records(self):
        return {kind: self.panel.records(job_id, self.intent['recordsSince'])
                for kind, job_id in JOB_IDS.items()}

    def idle_panel_jobs(self):
        jobs = sorted((row['id'], row['name'], row['status'], row['spec']) for row in self.panel.jobs())
        need(len({job[0] for job in jobs}) == len(jobs), 'NATIVE_JOB_IDS_CHANGED')
        end = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
        for job_id, _, _, _ in jobs:
            for status in ('Running', 'Waiting'):
                records = self.panel.request('/cronjobs/search/records', {'page': 1, 'pageSize': 1,
                    'cronjobID': job_id, 'startTime': '1970-01-01T00:00:00+00:00',
                    'endTime': end, 'status': status})
                need(isinstance(records, dict) and records.get('total') == 0
                     and not records.get('items'), 'PANEL_CRON_NOT_IDLE')
        return [list(job) for job in jobs]

    def check_records(self, *, allow_blocked=False):
        records = self.records()
        previous = {kind: {r['id']: r for r in self.intent['records'][kind]} for kind in JOB_IDS}
        current = {kind: {r['id']: r for r in records[kind]} for kind in JOB_IDS}
        need(all(len(current[k]) == len(records[k]) and
                 all(current[k].get(id) == row for id, row in previous[k].items()) for k in JOB_IDS),
             'NATIVE_HISTORY_CHANGED_SINCE_TRANSITION')
        added = {kind: [row for id, row in current[kind].items() if id not in previous[kind]] for kind in JOB_IDS}
        if allow_blocked:
            need(all(row['status'] in ('Failed', 'Canceled') for rows in added.values() for row in rows),
                 'UNEXPECTED_NATIVE_CRON_RUN_DURING_TRANSITION')
        return {kind: len(rows) for kind, rows in added.items()}

    def classification(self, kind):
        row = self.new.unique(self.panel.jobs(), self.new.cron_name(kind))
        process = self.new.unique(self.panel.programs(), self.new.program(kind))
        payload = self.updater.post('/cronjobs/load/info', {'id': JOB_IDS[kind]})
        config = self.panel.request('/hosts/tool/supervisor/process/file/get',
                                    {'name': self.new.program(kind), 'file': 'config'})
        verified = {}
        for name, state in (('old', self.previous), ('new', self.target)):
            try:
                self.new.verify_job(state, kind, row)
                expected = self.intent['cronInfo'][kind] | self.new.cron_payload(state, kind)
                need(payload == expected, 'CRON_FULL_DTO_DRIFT')
                verified['cron'] = name
            except (self.new.ScheduleError, shared.RepairStop):
                pass
            try:
                need(self.new.verify_process(state, kind, process) in self.new.INACTIVE,
                     'OWNED_PROCESS_NOT_IDLE')
                need(config == self.new.process_config(state, kind), 'PROCESS_CONFIGURATION_DRIFT')
                verified['process'] = name
            except (self.new.ScheduleError, shared.RepairStop):
                pass
        need(set(verified) == {'cron', 'process'}, 'NATIVE_OBJECT_NOT_EXACTLY_OLD_OR_NEW')
        return verified | {'status': row['status']}

    def observe(self):
        return {kind: self.classification(kind) for kind in JOB_IDS}

    def verify_loaded(self):
        need(loaded_supervisor_config(self.new), 'SUPERVISOR_CONFIGURATION_NOT_LOADED')

    def before(self):
        self.safety()
        self.old.verify_runners()
        raw = self.old.read(self.path, private=True)
        self.previous = self.old.load_state()
        active(self.previous, self.old, self.old_sha, self.generation)
        need(json.loads(raw) == self.previous and not self.history.exists() and not self.history.is_symlink(),
             'ACTIVE_TRANSITION_ALREADY_EXISTS_OR_DRIFTED')
        self.old.Controller(self.panel, self.old.Legacy()).objects(self.previous, status='Enable', idle=True)
        self.verify_loaded()
        agent_epoch(self.new)
        verify_agent_build()
        validate_terminal_receipts(self.old, self.previous)
        self.target = copy.deepcopy(self.previous)
        self.target.update(generation=self.target_generation, previousGeneration=self.generation,
                           programSha256=self.new_sha, programPath=str(self.new.source_release()[2]),
                           transitionedAt=self.new.stamp())
        for job in self.target['jobs'].values():
            job.pop('probe', None)
            job.pop('probeIntent', None)
        active(self.target, self.new, self.new_sha, self.target_generation)
        # The native list endpoint is bounded at 100 rows. Verify a fixed recent
        # interval across the switch without rewriting the full native history.
        since = max(self.previous['createdAt'], (datetime.now(timezone.utc) - timedelta(days=7)).isoformat())
        self.intent = {'format': FORMAT, 'oldStateSha256': shared.checksum(raw), 'targetState': self.target,
                       'cronInfo': {k: self.updater.info(self.previous, k) for k in JOB_IDS},
                       'recordsSince': since,
                       'receipts': {n: shared.captured_directory(self.old, STATE / n) for n in ('dispatch', 'runs')}}
        self.intent['records'] = self.records()
        need(self.observe() == dict.fromkeys(JOB_IDS, {'cron': 'old', 'process': 'old', 'status': 'Enable'})
             and self.old.read(self.path, private=True) == raw, 'NATIVE_BASELINE_CHANGED')
        return raw

    def plan(self):
        self.before()
        return {'status': 'ACTIVE_TRANSITION_PLAN_ONLY', 'changes': False,
                'oldStateSha256': self.intent['oldStateSha256'], 'oldGeneration': self.generation,
                'newGeneration': self.target_generation, 'oldProgramSha256': self.old_sha,
                'newProgramSha256': self.new_sha, 'nativeJobIds': JOB_IDS,
                'schedule': {kind: self.old.spec(kind) for kind in JOB_IDS}}

    def closed(self):
        return self.previous | {'phase': 'ACTIVE_SETTINGS_TRANSITION', 'owner': 'none',
                                'transitionHistory': str(self.history)}

    def marker(self, status, *, operation=None):
        self.new.atomic(self.history / 'progress.json', {'format': FORMAT, 'status': status,
                        'operation': operation, 'observedAt': self.new.stamp()})

    def request_once(self, change, verify):
        # Reconcile one possibly committed request before declaring an error.
        try:
            change()
        except (self.new.ScheduleError, shared.RepairStop) as error:
            need(str(error) in ('PANEL_REQUEST_OUTCOME_UNKNOWN', 'PANEL_RESPONSE_REJECTED',
                                'PANEL_RESPONSE_BOUND', 'REPAIR_REQUEST_OUTCOME_UNKNOWN',
                                'REPAIR_RESPONSE_REJECTED', 'REPAIR_RESPONSE_BOUND'), 'NATIVE_WRITE_FAILED')
        verify()

    def set_status(self, kind, target, *, bound):
        row = self.classification(kind)
        if row['status'] == target:
            return
        need(row['cron'] == bound, 'CRON_GENERATION_CHANGED_BEFORE_STATUS_UPDATE')
        epoch = agent_epoch(self.new)
        operation = {'kind': kind, 'target': target, 'agentEpoch': epoch}
        self.marker('NATIVE_CRON_STATUS_REQUEST', operation=operation)
        try:
            self.panel.request('/cronjobs/status', {'id': JOB_IDS[kind], 'status': target})
        except self.new.ScheduleError:
            # 1Panel adjusts the in-memory entry before persisting job status.
            # A DTO alone cannot confirm whether an unsuccessful call changed it.
            self.marker('NATIVE_CRON_STATUS_UNKNOWN', operation=operation)
            raise shared.RepairStop('CRON_RUNTIME_STATUS_UNKNOWN') from None
        need(self.classification(kind)['status'] == target, 'CRON_STATUS_READBACK')
        need(agent_epoch(self.new) == epoch, 'PANEL_AGENT_CHANGED_DURING_CRON_REQUEST')
        self.marker('NATIVE_CRON_STATUS_ACKNOWLEDGED', operation=operation)

    def switch_native(self, kind, destination, foreign, *, verify_loaded=True):
        target = self.target if destination == 'new' else self.previous
        observed = self.classification(kind)
        need(observed['status'] == 'Disable', 'CRON_MUST_BE_DISABLED')
        if observed['process'] != destination:
            self.request_once(lambda: self.panel.request('/hosts/tool/supervisor/process/file',
                {'name': self.new.program(kind), 'file': 'config', 'operate': 'update',
                 'content': self.new.process_config(target, kind)}),
                 lambda: need(self.classification(kind)['process'] == destination, 'PROCESS_UPDATE_READBACK'))
            need(self.foreign() == foreign, 'COLLECTOR_CHANGED_DURING_SWITCH')
        if verify_loaded:
            self.verify_loaded()
        if observed['cron'] != destination:
            payload = self.intent['cronInfo'][kind] | self.new.cron_payload(target, kind)
            self.request_once(lambda: self.updater.post('/cronjobs/update', payload),
                              lambda: need(self.classification(kind)['cron'] == destination, 'CRON_UPDATE_READBACK'))

    def receipt_locations(self, *, allow_current_new=False):
        result = {}
        for name, expected in self.intent['receipts'].items():
            archived = self.history / name
            current = STATE / name
            if archived.exists() or archived.is_symlink():
                present = current.exists() or current.is_symlink()
                need(shared.captured_directory(self.new, archived) == expected, 'OLD_RECEIPTS_CHANGED')
                if present and shared.captured_directory(self.new, current)['files']:
                    need(allow_current_new, 'NEW_GENERATION_ALREADY_DISPATCHED_OR_RECEIPTS_CHANGED')
                    result[name] = 'history-and-new'
                else:
                    result[name] = 'history'
            else:
                need(shared.captured_directory(self.new, current) == expected, 'OLD_RECEIPTS_CHANGED')
                result[name] = 'current'
        return result

    def archive_receipts(self):
        for name in self.receipt_locations():
            os.rename(STATE / name, self.history / name)
            shared.fsync_directory(self.history)
            shared.fsync_directory(STATE)
            self.new.directory(STATE / name)
            shared.fsync_directory(STATE)
        need(set(self.receipt_locations().values()) == {'history'}, 'RECEIPT_ARCHIVE_READBACK')

    def restore_receipts(self):
        for name, location in self.receipt_locations().items():
            if location == 'history':
                current = STATE / name
                if current.exists():
                    current.rmdir()  # Verified empty; rmdir refuses a concurrent nonempty directory.
                os.rename(self.history / name, current)
                shared.fsync_directory(self.history)
                shared.fsync_directory(STATE)
        need(set(self.receipt_locations().values()) == {'current'}, 'RECEIPT_ROLLBACK_READBACK')

    def verify_end(self, state, selected, environment, *, epoch=None):
        need(json.loads(self.new.read(self.path, private=True)) == self.closed(), 'DURABLE_GATE_CHANGED')
        need(self.observe() == dict.fromkeys(JOB_IDS, {'cron': selected, 'process': selected, 'status': 'Enable'}),
             'NATIVE_COMMIT_READBACK')
        self.verify_loaded()
        self.check_records(allow_blocked=True)
        need(self.environment() == environment, 'NATIVE_HISTORY_OR_APPLICATION_CHANGED')
        self.safety()
        if epoch is not None:
            need(agent_epoch(self.new) == epoch, 'PANEL_AGENT_CHANGED_DURING_RECONCILIATION')
        self.new.atomic(self.path, state)

    def persist_intent(self, raw):
        self.new.directory(self.history.parent)
        shared.fsync_directory(STATE)
        # A process crash can leave a private staging directory but cannot
        # publish a half-written intent under the actual generation name.
        stage = Path(tempfile.mkdtemp(prefix='.active-transition-', dir=self.history.parent))
        self.new.immutable(stage / 'old-state.json', raw)
        self.new.immutable(stage / 'intent.json', self.new.canonical(self.intent) + b'\n')
        shared.fsync_directory(stage)
        need(not self.history.exists() and not self.history.is_symlink(), 'TRANSITION_INTENT_ALREADY_PUBLISHED')
        os.rename(stage, self.history)
        shared.fsync_directory(self.history.parent)

    def apply(self, expected):
        raw = self.before()
        need(expected == self.intent['oldStateSha256'], 'REVIEWED_STATE_HASH_CHANGED')
        environment = self.environment()
        new_sha, path = self.new.install_release()
        need(new_sha == self.new_sha and path == self.target['programPath'], 'NEW_RELEASE_IDENTITY_CHANGED')
        checked = json.loads(self.new.command(['/usr/bin/python3', '-B',
            str(self.new.runner('settings')), '--check'], 75))
        need(checked.get('status') == 'CONFIG_SCHEDULE_READONLY_CHECK_PASS'
             and checked.get('releaseSha256') == self.new.digest(self.new.canonical(self.new.SETTINGS_FILES))
             and checked.get('catalogsReady') is True and checked.get('changes') is False, 'NEW_SETTINGS_RUNNER_NOT_READY')
        self.persist_intent(raw)
        shared.fsync_directory(Path(path).parent)
        shared.fsync_directory(Path(path).parent.parent)
        shared.fsync_directory(self.new.settings_release())
        shared.fsync_directory(self.new.settings_release().parent)
        self.marker('PREPARED')
        need(self.old.read(self.path, private=True) == raw, 'ACTIVE_STATE_CHANGED_BEFORE_CLOSING_GATE')
        self.new.atomic(self.path, self.closed())
        self.marker('GATE_CLOSED')
        for kind in JOB_IDS:
            self.set_status(kind, 'Disable', bound='old')
        need(self.foreign() == environment['collector'], 'COLLECTOR_CHANGED_BEFORE_SWITCH')
        self.archive_receipts()
        for kind in JOB_IDS:
            self.switch_native(kind, 'new', environment['collector'])
        for kind in JOB_IDS:
            self.set_status(kind, 'Enable', bound='new')
        self.verify_end(self.target, 'new', environment)
        self.marker('COMMITTED')
        return {'status': 'ACTIVE_SETTINGS_GENERATION_SWITCHED', 'generation': self.target_generation,
                'nativeJobIds': JOB_IDS, 'backupExecuted': False, 'historyPreserved': True}

    def read_intent(self, expected):
        self.safety()
        raw = self.old.read(self.history / 'old-state.json', private=True)
        self.previous = json.loads(raw)
        self.intent = json.loads(self.new.read(self.history / 'intent.json', private=True))
        self.target = self.intent['targetState']
        need(self.intent.get('format') == FORMAT
             and self.intent.get('oldStateSha256') == shared.checksum(raw) == expected, 'TRANSITION_INTENT_CHANGED')
        active(self.previous, self.old, self.old_sha, self.generation)
        active(self.target, self.new, self.new_sha, self.target_generation)
        need(self.target.get('previousGeneration') == self.generation, 'TRANSITION_LINEAGE_CHANGED')
        current = json.loads(self.new.read(self.path, private=True))
        need(current in (self.previous, self.target, self.closed()), 'TRANSITION_STATE_DRIFT')
        return current

    def arm_recovery(self, expected):
        current = self.read_intent(expected)
        progress = self.new.file_json(self.history / 'progress.json')
        need(current == self.closed() and progress is not None and progress['status'] in UNCERTAIN_CRON,
             'UNCERTAIN_CLOSED_TRANSITION_REQUIRED')
        self.receipt_locations()
        self.check_records(allow_blocked=True)
        epoch = agent_epoch(self.new)
        objects = self.observe()
        jobs = self.idle_panel_jobs()
        need(agent_epoch(self.new) == epoch, 'PANEL_AGENT_CHANGED_DURING_RECONCILIATION')
        self.new.atomic(self.history / 'recovery-arm.json', {'format': FORMAT,
            'uncertainty': progress, 'agentEpoch': epoch, 'objects': objects, 'jobs': jobs,
            'observedAt': self.new.stamp()})
        return {'status': 'ACTIVE_TRANSITION_RECOVERY_ARMED', 'gateClosed': True, 'nativeJobsIdle': True,
                'panelRestarted': False}

    def reconcile(self, expected, *, apply=False, recover=False):
        current = self.read_intent(expected)
        locations = self.receipt_locations(allow_current_new=not apply)
        objects = self.observe()
        loaded = loaded_supervisor_config(self.new)
        added = self.check_records(allow_blocked=apply)
        progress = self.new.file_json(self.history / 'progress.json')
        runtime_unknown = progress is not None and progress['status'] in UNCERTAIN_CRON
        if not apply:
            return {'status': 'ACTIVE_TRANSITION_READ_ONLY', 'changes': False, 'objects': objects,
                    'generation': current['generation'], 'phase': current['phase'], 'receipts': locations,
                    'supervisorMatchesDisk': loaded, 'nativeCronRuntimeStatusUnknown': runtime_unknown,
                    'postTransitionCronRecords': added}
        epoch = None
        if recover:
            need(runtime_unknown and current == self.closed(), 'UNCERTAIN_CLOSED_TRANSITION_REQUIRED')
            armed = self.new.file_json(self.history / 'recovery-arm.json')
            need(armed is not None and armed.get('format') == FORMAT and armed.get('uncertainty') == progress
                 and armed.get('objects') == objects, 'EXACT_RECOVERY_ARM_REQUIRED')
            prior_epoch = armed['agentEpoch']
            epoch = agent_epoch(self.new)
            need(epoch['boot'] == prior_epoch['boot'] and epoch['startTicks'] > prior_epoch['startTicks'],
                 'PANEL_AGENT_RESTART_NOT_ATTESTED')
            need(self.idle_panel_jobs() == armed.get('jobs'), 'PANEL_JOBS_CHANGED_SINCE_RECOVERY_ARM')
            verify_agent_startup(self.new, epoch, armed['jobs'])
            need(agent_epoch(self.new) == epoch, 'PANEL_AGENT_CHANGED_DURING_RECONCILIATION')
        else:
            need(not runtime_unknown, 'CRON_RUNTIME_REQUIRES_MANUAL_RECONCILIATION')
        environment = self.environment()
        self.old.verify_runners()
        self.new.atomic(self.path, self.closed())
        self.marker('ROLLING_BACK')
        for kind in JOB_IDS:
            self.set_status(kind, 'Disable', bound=objects[kind]['cron'])
        # Repair an observed partial INI write by restoring the reviewed source
        # via the same scoped native group update. No uncertain update is retried.
        for kind in JOB_IDS:
            self.switch_native(kind, 'old', environment['collector'], verify_loaded=False)
        self.verify_loaded()
        self.restore_receipts()
        for kind in JOB_IDS:
            self.set_status(kind, 'Enable', bound='old')
        self.verify_end(self.previous, 'old', environment, epoch=epoch)
        self.marker('ROLLED_BACK')
        return {'status': 'ACTIVE_SETTINGS_GENERATION_ROLLED_BACK', 'generation': self.generation,
                'nativeJobIds': JOB_IDS, 'backupExecuted': False, 'historyPreserved': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('plan', 'switch', 'reconcile', 'rollback', 'arm-recovery', 'recover'))
    parser.add_argument('--old-program-sha256', required=True)
    parser.add_argument('--new-program-sha256', required=True)
    parser.add_argument('--old-generation', required=True)
    parser.add_argument('--new-generation', required=True)
    parser.add_argument('--old-state-sha256')
    parser.add_argument('--apply', action='store_true')
    a = parser.parse_args()
    os.umask(0o077)
    need((a.action in ('switch', 'rollback', 'arm-recovery', 'recover')) == a.apply, 'UNEXPECTED_APPLY_FLAG')
    need(all(re.fullmatch('[a-f0-9]{64}', v) for v in (a.old_program_sha256, a.new_program_sha256))
         and a.old_program_sha256 != a.new_program_sha256
         and all(re.fullmatch('[a-f0-9]{32}', v) for v in (a.old_generation, a.new_generation))
         and a.old_generation != a.new_generation
         and (a.action == 'plan' or re.fullmatch('[a-f0-9]{64}', a.old_state_sha256 or '')),
         'EXPLICIT_GENERATIONS_AND_HASHES_REQUIRED')
    need(os.geteuid() == 0 and socket.gethostname() == HOST, 'VM100_ROOT_REQUIRED')
    old = shared.load_module('old_schedule', BASE / 'releases' / a.old_program_sha256[:16] / NAME,
                             a.old_program_sha256)
    new = shared.load_module('new_schedule', HERE / NAME, a.new_program_sha256)
    need(old.BASE == new.BASE == BASE and old.STATE == new.STATE == STATE
         and old.FORMAT == new.FORMAT and old.KINDS == new.KINDS == tuple(JOB_IDS)
         and old.DATABASE_BLOB == new.DATABASE_BLOB and old.DATABASE_SCRIPT == new.DATABASE_SCRIPT
         and old.spec('database') == new.spec('database') and old.spec('settings') == new.spec('settings'),
         'SCHEDULER_CONTRACT_CHANGED')
    transition = Transition(old, new, new.Panel(), a.old_program_sha256, a.new_program_sha256,
                            a.old_generation, a.new_generation)
    with shared.existing_idle_locks(old):
        if a.action == 'plan':
            result = transition.plan()
        elif a.action == 'switch':
            result = transition.apply(a.old_state_sha256)
        elif a.action == 'arm-recovery':
            result = transition.arm_recovery(a.old_state_sha256)
        else:
            result = transition.reconcile(a.old_state_sha256, apply=a.apply, recover=a.action == 'recover')
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        value = str(error)
        code = value if re.fullmatch('[A-Z][A-Z0-9_]{1,95}', value) else type(error).__name__
        print(json.dumps({'status': 'ACTIVE_SETTINGS_TRANSITION_STOP', 'code': code,
                          'nextAction': 'RECONCILE_IF_SWITCH_ATTEMPTED'}, sort_keys=True), file=sys.stderr)
        sys.exit(2)
