"""Detached operation worker. Raw output stays private; MCP gets structured events."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from .store import Store, OpsError, process_identity, digest


def adapter(config_path, action, payload, store=None, opid=None):
    config = json.loads(Path(config_path).read_text())
    root = Path(config['installation']['state_root'])
    # Private payload is server-generated, not a caller-selected file.
    import tempfile
    fd, name = tempfile.mkstemp(prefix='request-', suffix='.json', dir=root)
    os.chmod(name, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(payload, f)
    result = None
    try:
        command = [sys.executable, '-m', 'asa_ops.adapter', str(config_path), action, name]
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                encoding='utf-8', errors='replace', env=dict(os.environ, PYTHONUNBUFFERED='1'))
        # Never log arbitrary subprocess output. Docker output can contain secrets.
        for line in proc.stdout:
            if line.startswith('ASA_OPS_EVENT '):
                if store:
                    value = json.loads(line[len('ASA_OPS_EVENT '):])
                    phase = value.get('phase', 'manager')
                    store.update(opid, phase=phase)
                    store.event(opid, phase, 'Manager stage: '+phase)
            elif line.startswith('ASA_OPS_RESULT '):
                result = json.loads(line[len('ASA_OPS_RESULT '):])
        proc.wait()
        if result is None:
            raise OpsError('ADAPTER_EXIT', 'Manager adapter exited without a result; no automatic retry')
        if not result['ok']:
            raise OpsError(result['error']['code'], result['error']['message'])
        return result['result']
    finally:
        Path(name).unlink(missing_ok=True)


def work(config_path, opid, action):
    config = json.loads(Path(config_path).read_text())
    store = Store(config['installation']['state_root'])
    record = store.get(opid)
    store.update(opid, status='running', pid=os.getpid(), process_identity=process_identity(os.getpid()), phase=action)
    store.event(opid, action, 'Server worker started; reconnect using this operation_id')
    try:
        planned = store.get_plan(record['plan_id'])
        from .maintenance import check_generation
        check_generation(config['installation']['root'],planned)
        if action == 'admin':
            from .admin import request
            result = request(config, {'action': 'execute', 'operation_id': opid,
                                     **{k: planned[k] for k in ('installation_instance_id', 'command', 'cwd', 'timeout_seconds')}})
            if result.get('status') != 'complete':
                raise OpsError('ADMIN_RUNNING', 'Root audit shows an unfinished administration command; never replay')
            status = 'success' if result.get('exit_code') == 0 and not result.get('timed_out') else 'failed'
        elif action == 'restore_rehearse':
            result = adapter(config_path, action, {'plan': planned}, store, opid)
            status = 'success'  # rehearsal completed; ready_to_apply remains a separate result field
        elif action == 'verify':
            result = adapter(config_path, 'verify', {'release': planned['release']}, store, opid)
            status = 'success' if result['accepted'] else 'acceptance_pending'
        elif action == 'finalize':
            result = adapter(config_path, 'finalize', {'plan': planned}, store, opid)
            status = 'success'
        else:
            result = adapter(config_path, 'update', {'plan': planned}, store, opid)
            if result.get('retry_new_manager'):
                # Continuation is deliberately bounded; no new stable discovery.
                store.event(opid, 'new_manager', 'Pinned manager changed; validating one continuation')
                continuation = adapter(config_path, 'continuation', {'plan': planned, 'transition_fingerprint': result['transition_fingerprint']}, store, opid)
                result = adapter(config_path, 'update', {'plan': continuation}, store, opid)
            if result.get('manager_exit_code') != 0:
                status = 'acceptance_pending' if result.get('manager_status') == 'failed_switch' else 'failed'
            else:
                if not (result.get('receipt_attributed') or result.get('no_change')):
                    raise OpsError('RECEIPT_NOT_CURRENT', 'Manager returned zero without a current receipt; release is not accepted')
                result['verification'] = adapter(config_path, 'verify', {'release': planned['release']}, store, opid)
                status = 'success' if result['verification']['accepted'] else 'acceptance_pending'
                if status == 'success' and not result.get('no_change'):
                    result['finalization'] = adapter(config_path, 'finalize', {'plan': planned}, store, opid)
        store.update(opid, status=status, phase='complete' if status == 'success' else status, result=result)
        store.event(opid, status, 'Operation finished: '+status)
    except Exception as exc:
        code = getattr(exc, 'code', type(exc).__name__)
        # If a manager switch began, keep the installation locked for recovery.
        state = Path(config['installation']['root'])/'.asa'
        recovering = any((state/n).exists() for n in ('maintenance.json', 'failed-switch.json'))
        receipt = state/'last-operation.json'
        if receipt.exists():
            try:
                recovering = recovering or json.loads(receipt.read_text()).get('status') == 'failed_switch'
            except (OSError, ValueError):
                recovering = True  # an unreadable receipt must not release a possibly interrupted switch
        status = 'recovery_required' if recovering or action == 'admin' else 'failed'
        store.update(opid, status=status, phase='error', result={'error': {'code': code,
                     'message': str(exc) if isinstance(exc, OpsError) else 'Inspect server operation stage'}})
        store.event(opid, 'error', 'Operation stopped: '+code)


if __name__ == '__main__':
    work(sys.argv[1], sys.argv[2], sys.argv[3])
