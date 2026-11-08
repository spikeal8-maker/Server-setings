#!/usr/bin/env python3
"""Preserve one closed STAGED generation while replacing the double-output worker.

This school-only repair supports the exact 6f5a51 -> 4b391b source change. It
never starts a worker, enables a clock, deletes a native object or retries a
native mutation. A stopped repair can only be inspected with reconcile.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
import copy
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys
import time
import urllib.request

sys.dont_write_bytecode = True
OLD_SHA = '6f5a51cbe94a7bf22152884eeb7d7c0efd149667cdc21b60e889d682740e18b2'
NEW_SHA = '4b391bbd4b4589212acffc25a4239b79d15e5830176300610dfd1f7fc3ef596b'
NAME = 'school-1panel-backup-schedules.py'
OLD_SOURCE = Path('/opt/asa-school-panel-schedules/releases') / OLD_SHA[:16] / NAME
NEW_SOURCE = Path(__file__).absolute().with_name(NAME)
FORMAT = 'asa-school-staged-repair-v1'
JOB_IDS = {'database': 1, 'settings': 2}
CONFIG_BACKUP_LOCK = Path('/run/lock/asa-school-host-config-backup.lock')
DTO_FIELDS = set(('id name type groupID specCustom spec executor scriptMode script command containerName user '
    'scriptID appID website exclusionRules dbType dbName url isDir sourceDir snapshotRule sourceAccountIDs '
    'downloadAccountID retainCopies retryTimes timeout ignoreErr secret args alertCount alertTitle alertMethod scopes').split())


class RepairStop(RuntimeError):
    pass


def need(value, code):
    if not value:
        raise RepairStop(code)


def checksum(raw):
    return hashlib.sha256(raw).hexdigest()


def signature(info):
    return [info.st_dev, info.st_ino, info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode), info.st_size, info.st_mtime_ns]


def fsync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def load_module(name, path, expected):
    path = Path(path)
    for item in (path, *path.parents):
        info = item.lstat()
        need(info.st_uid == 0 and not stat.S_ISLNK(info.st_mode) and not info.st_mode & 0o022,
             'WRAPPER_PATH_UNTRUSTED')
    info = path.lstat()
    need(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_size <= 256 * 1024, 'WRAPPER_FILE_BOUND')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, 'rb') as source:
        raw = source.read(256 * 1024 + 1)
        final = os.fstat(source.fileno())
    need(signature(info) == signature(final) == signature(path.lstat()) and checksum(raw) == expected,
         'WRAPPER_SOURCE_CHANGED')
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def modules():
    old = load_module('school_staged_repair_old_wrapper', OLD_SOURCE, OLD_SHA)
    new = load_module('school_staged_repair_fixed_wrapper', NEW_SOURCE, NEW_SHA)
    need(old.SETTINGS_FILES == new.SETTINGS_FILES and old.STATE == new.STATE and old.BASE == new.BASE
         and old.KINDS == new.KINDS == ('database', 'settings') and old.FORMAT == new.FORMAT,
         'WRAPPER_CONTRACT_CHANGED')
    need(old.source_release()[2] == OLD_SOURCE and old.source_release()[1] == OLD_SHA
         and new.source_release()[1] == NEW_SHA, 'WRAPPER_RELEASE_SCOPE')
    return old, new


@contextmanager
def existing_idle_locks(cron):
    """The first exclusive lock excludes all canonical shared entry points."""
    required = [cron.LOCK, cron.DB_LOCK, CONFIG_BACKUP_LOCK,
                cron.APP / '.asa/operation.lock']
    optional = [cron.STATE / 'probe.lock'] + [cron.STATE / (kind + suffix)
        for kind in cron.KINDS for suffix in ('.dispatch.lock', '.run.lock')]
    with ExitStack() as stack:
        for path in required + optional:
            if not path.exists() and not path.is_symlink():
                need(path in optional, 'EXISTING_COORDINATION_LOCK_MISSING')
                continue
            for parent in path.parents:
                info = parent.lstat()
                need(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not stat.S_ISLNK(info.st_mode)
                     and (not info.st_mode & 0o022 or parent == Path('/run/lock')
                          and stat.S_IMODE(info.st_mode) == 0o1777), 'LOCK_PARENT_UNTRUSTED')
            before = path.lstat()
            need(stat.S_ISREG(before.st_mode) and before.st_uid == 0 and before.st_nlink == 1
                 and stat.S_IMODE(before.st_mode) == 0o600, 'LOCK_INODE_UNTRUSTED')
            fd = os.open(path, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
            stack.callback(os.close, fd)
            need(signature(before) == signature(os.fstat(fd)), 'LOCK_REPLACED')
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RepairStop('OPERATION_OR_BACKUP_BUSY') from None
            need(signature(before) == signature(path.lstat()) == signature(os.fstat(fd)), 'LOCK_REPLACED')
        yield


class UpdateAdapter:
    """Only the two v2.3.2 DTO endpoints absent from the canonical client.

    The inherited client's fixed loopback/no-proxy/no-redirect opener and
    in-memory key are reused; its ordinary endpoint allowlist is unchanged.
    """
    def __init__(self, cron, panel):
        self.cron, self.panel = cron, panel

    def post(self, endpoint, payload):
        need(endpoint in ('/cronjobs/load/info', '/cronjobs/update') and type(payload.get('id')) is int
             and payload['id'] in JOB_IDS.values(), 'REPAIR_ENDPOINT_SCOPE')
        timestamp = str(int(time.time()))
        token = hashlib.md5(('1panel' + self.panel.key + timestamp).encode()).hexdigest()
        request = urllib.request.Request(self.cron.PANEL + '/api/v2' + endpoint, method='POST',
            data=self.cron.canonical(payload), headers={'Content-Type': 'application/json',
                '1Panel-Timestamp': timestamp, '1Panel-Token': token})
        try:
            with self.panel.opener.open(request, timeout=15) as response:
                raw = response.read(256 * 1024 + 1)
            need(len(raw) <= 256 * 1024, 'REPAIR_RESPONSE_BOUND')
            value = json.loads(raw)
            need(value.get('code') in (0, 200), 'REPAIR_RESPONSE_REJECTED')
            return value.get('data')
        except RepairStop:
            raise
        except Exception:
            raise RepairStop('REPAIR_REQUEST_OUTCOME_UNKNOWN') from None

    def info(self, state, kind):
        value = self.post('/cronjobs/load/info', {'id': JOB_IDS[kind]})
        need(isinstance(value, dict) and set(value) == DTO_FIELDS and type(value['id']) is int
             and value['id'] == JOB_IDS[kind] and all(value.get(k) == v for k, v in self.cron.cron_payload(state, kind).items()),
             'OWNED_CRON_FULL_DTO_CHANGED')
        need(value['alertCount'] == 0 and value['alertTitle'] == value['alertMethod'] == '',
             'OWNED_CRON_ALERT_NOT_EMPTY')
        return value

    def update(self, old_state, new_state, kind, original):
        row = self.cron.unique(self.panel.jobs(), self.cron.cron_name(kind))
        self.cron.verify_job(old_state, kind, row, 'Disable')
        need(self.info(old_state, kind) == original, 'CRON_DTO_CHANGED_BEFORE_UPDATE')
        payload = copy.deepcopy(original)
        payload.update(self.cron.cron_payload(new_state, kind))
        need([key for key in DTO_FIELDS if payload[key] != original[key]] == ['script'],
             'CRON_UPDATE_EXCEEDS_SCRIPT_SCOPE')
        return self.post('/cronjobs/update', payload)


def captured_directory(cron, path):
    info = cron.trusted(path, directory=True, private=True)
    names = sorted(item.name for item in path.iterdir())
    need(len(names) <= 2 and set(names) <= {'database.json', 'settings.json'}, 'UNREVIEWED_RECEIPT_FILE')
    files = {}
    for name in names:
        child = path / name
        raw = cron.read(child, 128 * 1024, private=True)
        files[name] = {'sha256': checksum(raw), 'metadata': signature(child.lstat())}
    after = cron.trusted(path, directory=True, private=True)
    need(signature(info) == signature(after), 'RECEIPT_DIRECTORY_CHANGED')
    return {'directoryIdentity': signature(info)[:5], 'files': files}


def backup_logs(cron):
    result, total = {}, 0
    for kind in cron.KINDS:
        for stream in ('out', 'err'):
            base = Path('/opt/1panel/tools/supervisord/log') / (cron.program(kind) + '.' + stream + '.log')
            for path in (base, *(Path(str(base) + '.' + str(n)) for n in range(1, 11))):
                if not path.exists() and not path.is_symlink():
                    continue
                raw = cron.read(path, 2 * 1024 * 1024)
                total += len(raw)
                need(total <= 8 * 1024 * 1024, 'OWNED_BACKUP_LOG_BOUND')
                result[str(path)] = {'sha256': checksum(raw), 'metadata': signature(path.lstat())}
    return result


def application(cron):
    ids = sorted(cron.command(['/usr/bin/docker', 'ps', '--all', '--filter',
        'label=com.docker.compose.project=' + cron.PROJECT, '--format', '{{.ID}}']).split())
    need(len(ids) == 8 and all(re.fullmatch('[a-f0-9]{12,64}', cid) for cid in ids), 'APPLICATION_CONTAINER_SET_CHANGED')
    fields = ('{"id":{{json .Id}},"image":{{json .Image}},"started":{{json .State.StartedAt}},'
              '"restarts":{{json .RestartCount}},"state":{{json .State.Status}}}')
    rows = [json.loads(cron.command(['/usr/bin/docker', 'inspect', '--format', fields, cid])) for cid in ids]
    need(sum(row.get('state') == 'running' for row in rows) == 6, 'APPLICATION_RUNNING_SET_CHANGED')
    return rows


def environmental(cron, panel, state):
    legacy = cron.Legacy()
    legacy.idle()
    previous = {kind: legacy.inspect(kind) for kind in cron.KINDS}
    need(previous == state['legacy'] and all(row['timer'] and row['timer']['UnitFileState'] == 'enabled'
         and row['timer']['ActiveState'] == 'active' for row in previous.values()), 'LEGACY_CLOCK_OWNERSHIP_CHANGED')
    foreign = cron.foreign_snapshot(panel.programs())
    need(len(foreign) == 1 and len(foreign[0]['PID']) == 1 and foreign[0]['PID'][0][0] == 'RUNNING'
         and str(foreign[0]['PID'][0][1]).isdigit() and int(foreign[0]['PID'][0][1]) > 0,
         'FOREIGN_COLLECTOR_NOT_RUNNING')
    index = cron.trusted(cron.APP / '.asa/diagnostics/private/index.sqlite')
    return {'legacy': previous, 'collector': foreign, 'application': application(cron),
            'index': signature(index)[:5], 'logs': backup_logs(cron)}


def validate_terminal_receipts(cron, panel, state):
    for kind in cron.KINDS:
        dispatch = cron.file_json(cron.STATE / 'dispatch' / (kind + '.json'))
        run = cron.runtime_state(kind)
        records = panel.records(JOB_IDS[kind], state['createdAt'])
        job = state['jobs'][kind]
        if dispatch is None and run is None:
            need(not records and 'probe' not in job and 'probeIntent' not in job, 'UNRESOLVED_PROBE_WITHOUT_RUN')
            continue
        need(isinstance(dispatch, dict) and isinstance(run, dict)
             and dispatch.get('phase') == 'STAGED' and dispatch.get('status') == 'OBSERVED_COMPLETE'
             and run.get('status') == 'STAGED_GATE_VERIFIED_NO_BACKUP' and run.get('backupExecuted') is False
             and not run.get('backupAttempted') and run.get('finishedAt') and dispatch.get('finishedAt')
             and all(doc.get('kind') == kind and doc.get('generation') == state['generation']
                     and doc.get('programSha256') == OLD_SHA for doc in (dispatch, run))
             and re.fullmatch('[a-f0-9]{32}', str(dispatch.get('dispatchId')))
             and dispatch['dispatchId'] == run.get('dispatchId'), 'OLD_DISPATCH_NOT_TERMINAL_STAGED')
        probe = job.get('probe', {})
        need(probe.get('status') == 'NATIVE_GATED_PROBE_PASS' and probe.get('backupExecuted') is False
             and probe.get('dispatchId') == dispatch['dispatchId'] and probe.get('programSha256') == OLD_SHA
             and len(records) == 1 and records[0]['id'] == probe.get('recordId')
             and records[0]['status'] == 'Success', 'OLD_PROBE_RECORD_NOT_RECONCILED')


def preflight(old, new, panel, updater, old_generation, new_generation):
    need(re.fullmatch('[a-f0-9]{32}', old_generation) and re.fullmatch('[a-f0-9]{32}', new_generation)
         and old_generation != new_generation, 'EXPLICIT_DISTINCT_GENERATIONS_REQUIRED')
    old.check_host()
    panel.prerequisite()
    old.handover_window()
    old.verify_runners()
    raw = old.read(old.STATE / 'state.json', private=True)
    state = old.load_state()
    need(state['generation'] == old_generation and state.get('phase') == 'STAGED'
         and state.get('owner') == state.get('originalOwner') == 'legacy'
         and state.get('backupExecutedDuringInstall') is False and state.get('clockTimeZone') == 'Europe/Moscow',
         'EXACT_LEGACY_OWNED_STAGED_GENERATION_REQUIRED')
    need(all(state['jobs'][kind].get('id') == JOB_IDS[kind]
         and state['jobs'][kind].get('processConfigured') is True for kind in old.KINDS), 'EXACT_NATIVE_JOB_IDS_REQUIRED')
    need(all(isinstance(state['jobs'][kind].get('nativeNextPreview'), list)
         and len(state['jobs'][kind]['nativeNextPreview']) == 5
         and all(isinstance(value, str) and re.fullmatch(r'\d{4}-\d\d-\d\d \d\d:\d\d:\d\d', value)
                 for value in state['jobs'][kind]['nativeNextPreview']) for kind in old.KINDS),
         'EXISTING_NATIVE_PREVIEW_REQUIRED')
    need(not (old.STATE / 'history' / old_generation).exists()
         and not (old.STATE / 'history' / old_generation).is_symlink(), 'REPAIR_ALREADY_EXISTS_RECONCILE_ONLY')
    need(len(panel.jobs()) == 2, 'UNREVIEWED_NATIVE_CRON_OBJECT')
    old.Controller(panel, old.Legacy()).objects(state, status='Disable', idle=True)
    validate_terminal_receipts(old, panel, state)
    receipts = {name: captured_directory(old, old.STATE / name) for name in ('dispatch', 'runs')}
    old.trusted(old.STATE / 'output', directory=True, private=True)
    need(not list((old.STATE / 'output').iterdir()), 'STAGED_RUNNER_OUTPUT_PRESENT')
    infos = {kind: updater.info(state, kind) for kind in old.KINDS}
    records = {kind: panel.records(JOB_IDS[kind], state['createdAt']) for kind in old.KINDS}
    environment = environmental(old, panel, state)
    need(old.read(old.STATE / 'state.json', private=True) == raw, 'STAGED_STATE_CHANGED_DURING_PREFLIGHT')
    return {'state': state, 'raw': raw, 'stateSha256': checksum(raw), 'receipts': receipts,
            'infos': infos, 'records': records, 'environment': environment}


def new_state(cron, before, generation):
    result = copy.deepcopy(before['state'])
    result.update(generation=generation, programSha256=NEW_SHA, programPath=str(cron.source_release()[2]),
                  createdAt=cron.stamp(), phase='STAGED', owner='legacy', previousGeneration=before['state']['generation'])
    result['jobs'] = {kind: {'id': JOB_IDS[kind], 'processConfigured': True,
        'nativeNextPreview': copy.deepcopy(before['state']['jobs'][kind]['nativeNextPreview'])} for kind in cron.KINDS}
    result.pop('updatedAt', None)
    return result


def readback_once(change, observe, progress, save):
    progress['status'] = 'NATIVE_REQUEST_INTENT'
    save()
    response_confirmed = True
    try:
        change()
    except Exception as error:
        known = isinstance(error, RepairStop) or type(error).__name__ == 'ScheduleError' and type(error).__module__ in (
            'school_staged_repair_old_wrapper', 'school_staged_repair_fixed_wrapper')
        if not (known and str(error) in ('REPAIR_REQUEST_OUTCOME_UNKNOWN', 'REPAIR_RESPONSE_BOUND', 'REPAIR_RESPONSE_REJECTED',
                'PANEL_REQUEST_OUTCOME_UNKNOWN', 'PANEL_RESPONSE_BOUND', 'PANEL_RESPONSE_REJECTED')):
            raise
        response_confirmed = False
    # One observation after one request. A late alert error can follow a
    # committed update; a matching native readback establishes the effect.
    observe()
    progress.update(status='CONFIRMED_BY_READBACK', responseConfirmed=response_confirmed)
    save()


def apply_repair(old, new, panel, updater, before, generation, expected_state_sha):
    need(before['stateSha256'] == expected_state_sha, 'EXPLICIT_STAGED_STATE_HASH_CHANGED')
    need(old.read(old.STATE / 'state.json', private=True) == before['raw'], 'STAGED_STATE_CHANGED_BEFORE_REPAIR')
    history = old.STATE / 'history' / before['state']['generation']
    new.directory(history.parent)
    fsync_directory(old.STATE)
    need(not history.exists() and not history.is_symlink(), 'REPAIR_ALREADY_EXISTS_RECONCILE_ONLY')
    new.directory(history)
    fsync_directory(history.parent)
    target = new_state(new, before, generation)
    intent = {'format': FORMAT, 'oldGeneration': before['state']['generation'], 'newGeneration': generation,
              'oldProgramSha256': OLD_SHA, 'newProgramSha256': NEW_SHA,
              'oldStateSha256': before['stateSha256'], 'newState': target,
              'oldReceipts': before['receipts'], 'nativeJobIds': JOB_IDS}
    new.immutable(history / 'old-state.json', before['raw'])
    new.immutable(history / 'native-before.json', new.canonical({'infos': before['infos'], 'records': before['records']}) + b'\n')
    new.immutable(history / 'intent.json', new.canonical(intent) + b'\n')
    fsync_directory(history)
    progress = {'format': FORMAT, 'oldGeneration': intent['oldGeneration'], 'newGeneration': generation,
                'phase': 'ARCHIVE_PREPARED', 'steps': {}, 'automaticResumeAllowed': False}
    save = lambda: new.atomic(history / 'progress.json', progress)
    save()
    closed = copy.deepcopy(before['state'])
    closed.update(phase='STAGED_REPAIRING', owner='legacy', repairHistory=str(history))
    new.atomic(old.STATE / 'state.json', closed)
    need(json.loads(old.read(old.STATE / 'state.json', private=True)) == closed, 'CLOSED_GATE_NOT_DURABLE')
    progress['phase'] = 'GATE_CLOSED'
    save()
    checksum_value, target_path = new.install_release()
    need(checksum_value == NEW_SHA and target_path == target['programPath'], 'FIXED_RELEASE_NOT_INSTALLED')
    fsync_directory(Path(target_path).parent)
    fsync_directory(Path(target_path).parent.parent)
    for name in ('dispatch', 'runs'):
        source, destination = old.STATE / name, history / name
        need(captured_directory(old, source) == before['receipts'][name], 'OLD_RECEIPTS_CHANGED_BEFORE_ARCHIVE')
        need(not destination.exists() and not destination.is_symlink(), 'ARCHIVE_DESTINATION_EXISTS')
        os.rename(source, destination)
        fsync_directory(history)
        fsync_directory(old.STATE)
        new.directory(source)
        fsync_directory(source)
        fsync_directory(old.STATE)
        need(captured_directory(old, destination) == before['receipts'][name] and not list(source.iterdir()),
             'RECEIPT_ARCHIVE_NOT_EXACT')
        progress['steps']['archive-' + name] = {'status': 'PRESERVED_AND_CURRENT_EMPTY'}
        save()
    for kind in old.KINDS:
        old.verify_job(before['state'], kind, old.unique(panel.jobs(), old.cron_name(kind)), 'Disable')
        need(old.verify_process(before['state'], kind, old.unique(panel.programs(), old.program(kind))) in old.INACTIVE,
             'OWNED_PROCESS_NO_LONGER_IDLE')
        old.verify_config(panel, before['state'], kind)
        progress['steps']['process-' + kind] = {}
        def observe_process():
            need(new.verify_process(target, kind, new.unique(panel.programs(), new.program(kind))) in new.INACTIVE,
                 'UPDATED_PROCESS_NOT_IDLE')
            new.verify_config(panel, target, kind)
            need(old.foreign_snapshot(panel.programs()) == before['environment']['collector'], 'FOREIGN_COLLECTOR_CHANGED')
        readback_once(lambda: panel.request('/hosts/tool/supervisor/process/file',
            {'name': new.program(kind), 'file': 'config', 'operate': 'update', 'content': new.process_config(target, kind)}),
            observe_process, progress['steps']['process-' + kind], save)
        progress['steps']['cron-' + kind] = {}
        expected_info = copy.deepcopy(before['infos'][kind])
        expected_info.update(new.cron_payload(target, kind))
        def observe_job():
            new.verify_job(target, kind, new.unique(panel.jobs(), new.cron_name(kind)), 'Disable')
            need(updater.info(target, kind) == expected_info, 'UPDATED_CRON_DTO_NOT_EXACT')
        readback_once(lambda: updater.update(before['state'], target, kind, before['infos'][kind]),
                      observe_job, progress['steps']['cron-' + kind], save)
    new.Controller(panel, new.Legacy()).objects(target, status='Disable', idle=True)
    need(environmental(new, panel, before['state']) == before['environment'], 'PRESERVATION_READBACK_CHANGED')
    need({kind: panel.records(JOB_IDS[kind], before['state']['createdAt']) for kind in new.KINDS} == before['records'],
         'NATIVE_CRON_HISTORY_CHANGED')
    need(all(captured_directory(new, history / name) == before['receipts'][name]
             and not list((new.STATE / name).iterdir()) for name in ('dispatch', 'runs')), 'ARCHIVE_OR_NEW_RECEIPTS_CHANGED')
    new.atomic(new.STATE / 'state.json', target)
    need(new.load_state() == target, 'NEW_STAGED_STATE_NOT_DURABLE')
    progress['phase'] = 'COMMITTED_STAGED'
    save()
    return {'status': 'STAGED_GENERATION_REPLACED_GATE_CLOSED', 'oldGeneration': intent['oldGeneration'],
            'generation': generation, 'programSha256': NEW_SHA, 'nativeJobIds': JOB_IDS,
            'phase': 'STAGED', 'owner': 'legacy', 'historyDirectory': str(history), 'oldStateSha256': before['stateSha256'],
            'oldReceiptsAndLogsPreserved': True, 'nativeRecordsPreserved': True, 'legacyTimersEnabled': True,
            'collectorAndApplicationUnchanged': True, 'newProbeAcceptance': 'NOT_RUN', 'backupExecuted': False,
            'automaticResumeAllowed': False}


def reconcile(old, new, panel, updater, old_generation, new_generation):
    """Read and classify only. No source install, state write or mutation retry."""
    history = old.STATE / 'history' / old_generation
    current = json.loads(new.read(new.STATE / 'state.json', private=True))
    intent_path = history / 'intent.json'
    if not intent_path.exists() and not intent_path.is_symlink():
        old_closed = current.get('format') == new.FORMAT and current.get('instance') == new.INSTANCE \
            and current.get('project') == new.PROJECT and current.get('phase') == 'STAGED' \
            and current.get('owner') == 'legacy' and current.get('generation') == old_generation \
            and current.get('programSha256') == OLD_SHA
        return {'status': 'STAGED_REPAIR_READONLY_RECONCILIATION', 'intentPresent': False,
                'stateClass': 'old-closed-without-intent' if old_closed else 'unknown-without-intent',
                'oldGeneration': old_generation, 'generation': new_generation, 'gateClosed': old_closed,
                'nativeObjectsClassified': False, 'changes': False, 'automaticResumeAllowed': False}
    intent = json.loads(new.read(intent_path, private=True))
    previous_raw = new.read(history / 'old-state.json', private=True)
    previous = json.loads(previous_raw)
    need(intent.get('format') == FORMAT and intent.get('oldGeneration') == old_generation
         and intent.get('newGeneration') == new_generation and intent.get('oldProgramSha256') == OLD_SHA
         and intent.get('newProgramSha256') == NEW_SHA and checksum(previous_raw) == intent.get('oldStateSha256'),
         'REPAIR_INTENT_IDENTITY_CHANGED')
    target = intent['newState']
    need(previous.get('format') == target.get('format') == new.FORMAT
         and previous.get('instance') == target.get('instance') == new.INSTANCE
         and previous.get('project') == target.get('project') == new.PROJECT
         and previous.get('phase') == target.get('phase') == 'STAGED'
         and previous.get('owner') == target.get('owner') == 'legacy'
         and previous.get('generation') == old_generation and target.get('generation') == new_generation
         and previous.get('programSha256') == OLD_SHA and target.get('programSha256') == NEW_SHA,
         'ARCHIVED_STAGED_SCOPE_CHANGED')
    state_class = 'old' if current == previous else 'new' if current == target else 'other'
    if current.get('phase') == 'STAGED_REPAIRING' and current.get('owner') == 'legacy' \
            and current.get('generation') == old_generation and current.get('programSha256') == OLD_SHA \
            and current.get('repairHistory') == str(history):
        state_class = 'closed-transition'
    jobs, processes = panel.jobs(), panel.programs()
    objects = {}
    for kind in new.KINDS:
        objects[kind] = {}
        for name, state in (('old', previous), ('new', target)):
            try:
                new.verify_job(state, kind, new.unique(jobs, new.cron_name(kind)), 'Disable')
                updater.info(state, kind)
                objects[kind]['cron'] = name
            except Exception:
                pass
            try:
                need(new.verify_process(state, kind, new.unique(processes, new.program(kind))) in new.INACTIVE,
                     'PROCESS_NOT_IDLE')
                new.verify_config(panel, state, kind)
                objects[kind]['process'] = name
            except Exception:
                pass
        objects[kind].setdefault('cron', 'unknown')
        objects[kind].setdefault('process', 'unknown')
    archived, current_directories = {}, {}
    for name in ('dispatch', 'runs'):
        path = history / name
        archived[name] = (captured_directory(new, path) == intent['oldReceipts'][name]) if path.exists() else False
        current_path = new.STATE / name
        if not current_path.exists() and not current_path.is_symlink():
            current_directories[name] = 'missing'
        else:
            observed = captured_directory(new, current_path)
            current_directories[name] = 'empty' if not observed['files'] else \
                'old-exact' if observed == intent['oldReceipts'][name] else 'other'
    return {'status': 'STAGED_REPAIR_READONLY_RECONCILIATION', 'stateClass': state_class, 'objects': objects,
            'oldGeneration': old_generation, 'generation': new_generation, 'archivedReceiptDirectoriesExact': archived,
            'currentReceiptDirectories': current_directories,
            'changes': False, 'backupExecuted': False, 'automaticResumeAllowed': False,
            'gateClosed': state_class in ('old', 'new', 'closed-transition')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('plan', 'repair', 'reconcile'), nargs='?', default='plan')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--old-generation', required=True)
    parser.add_argument('--old-program-sha256', required=True)
    parser.add_argument('--new-generation', required=True)
    parser.add_argument('--old-state-sha256')
    args = parser.parse_args()
    stage = 'arguments'
    try:
        need(args.old_program_sha256 == OLD_SHA and re.fullmatch('[a-f0-9]{32}', args.old_generation)
             and re.fullmatch('[a-f0-9]{32}', args.new_generation) and args.new_generation != args.old_generation,
             'EXPLICIT_OLD_SOURCE_AND_GENERATIONS_REQUIRED')
        need(args.action != 'repair' or not args.apply or re.fullmatch('[a-f0-9]{64}', args.old_state_sha256 or ''),
             'APPLY_REQUIRES_REVIEWED_STATE_HASH')
        old, new = modules()
        old.check_host()
        panel = new.Panel()
        updater = UpdateAdapter(new, panel)
        with existing_idle_locks(old):
            if args.action == 'reconcile':
                stage = 'read-only-reconciliation'
                value = reconcile(old, new, panel, updater, args.old_generation, args.new_generation)
            else:
                stage = 'read-only-staged-preflight'
                before = preflight(old, new, panel, updater, args.old_generation, args.new_generation)
                value = {'status': 'STAGED_REPAIR_PLAN_ONLY', 'oldGeneration': args.old_generation,
                         'generation': args.new_generation, 'oldProgramSha256': OLD_SHA, 'programSha256': NEW_SHA,
                         'oldStateSha256': before['stateSha256'], 'nativeJobIds': JOB_IDS,
                         'oldTerminalReceiptsVerified': True, 'changes': False, 'backupExecuted': False}
                if args.action == 'repair' and args.apply:
                    stage = 'closed-gate-repair'
                    value = apply_repair(old, new, panel, updater, before, args.new_generation, args.old_state_sha256)
        print(json.dumps(value, sort_keys=True), flush=True)
        return 0
    except Exception as error:
        code = str(error) if isinstance(error, RepairStop) and re.fullmatch('[A-Z0-9_]{1,100}', str(error)) else type(error).__name__
        print(json.dumps({'status': 'STAGED_REPAIR_STOP', 'stage': stage, 'code': code,
                          'nativeOrStateMutationMayHaveCommitted': stage == 'closed-gate-repair',
                          'automaticResumeAllowed': False, 'nextAction': 'READONLY_RECONCILE_AFTER_MUTATION_ATTEMPT'},
                         sort_keys=True), flush=True)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
