"""Loopback authenticated HTTP MCP multiplexer and durable job controller."""
import argparse
import hmac
import http.server
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

from .schemas import INPUTS, tools, validate, input_schema
from .store import Store, OpsError, process_identity
from .worker import adapter
from .maintenance import check_generation


class Official:
    """One initialized stdio SDK session; serialized JSON-RPC requests."""
    def __init__(self, command):
        self.command = command
        self.lock = threading.Lock()
        self.process = None
        self.serial = 0

    def _start(self):
        self.process = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, text=True, encoding='utf-8', bufsize=1)
        self.responses = queue.Queue()
        child = self.process
        def read():
            try:
                for line in child.stdout:
                    if len(line) > 4*1024*1024:
                        break
                    self.responses.put(json.loads(line))
            finally:
                self.responses.put(None)
        threading.Thread(target=read, daemon=True).start()
        self._request('initialize', {'protocolVersion': '2025-03-26', 'capabilities': {},
                      'clientInfo': {'name': 'asa-operations', 'version': '1'}})
        self.process.stdin.write(json.dumps({'jsonrpc': '2.0', 'method': 'notifications/initialized'})+'\n')
        self.process.stdin.flush()

    def _request(self, method, params):
        self.serial += 1
        ident = self.serial
        self.process.stdin.write(json.dumps({'jsonrpc': '2.0', 'id': ident, 'method': method, 'params': params})+'\n')
        self.process.stdin.flush()
        deadline = time.monotonic()+60
        while True:
            try:
                value = self.responses.get(timeout=max(0.01, deadline-time.monotonic()))
            except queue.Empty:
                raise OpsError('OFFICIAL_TIMEOUT', 'Official 1Panel response timed out; call not automatically repeated') from None
            if value is None:
                raise OpsError('OFFICIAL_EXIT', 'Official 1Panel child exited; call not automatically repeated')
            if value.get('id') == ident:
                if 'error' in value:
                    raise OpsError('OFFICIAL_ERROR', 'Official 1Panel rejected the request')
                return value['result']

    def request(self, method, params):
        with self.lock:
            if self.process is None or self.process.poll() is not None:
                self._start()
            return self._request(method, params)


class Controller:
    def __init__(self, config_path, official=None):
        self.config_path = str(Path(config_path).resolve())
        self.config = json.loads(Path(config_path).read_text())
        self.store = Store(self.config['installation']['state_root'])
        self.official = official or Official(self.config['official_command'])
        self.lock = threading.Lock()
        self.store.reconcile()

    def installation(self, ident):
        if ident != self.config['installation']['id']:
            raise OpsError('INSTALLATION', 'Unknown installation')

    def launch(self, record, action):
        opid = record['operation_id']
        try:
            # Detached process survives HTTP disconnect and systemd main-process restart.
            proc = subprocess.Popen([sys.executable, '-m', 'asa_ops.worker', self.config_path, opid, action],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    start_new_session=True)
            self.store.update(opid, pid=proc.pid, process_identity=process_identity(proc.pid))
            threading.Thread(target=proc.wait, daemon=True).start()
        except OSError:
            self.store.update(opid, status='failed', phase='launch_failed', result={'error': {'code': 'LAUNCH', 'message': 'Server could not launch worker'}})
            raise OpsError('LAUNCH', 'Server could not launch worker') from None
        return self.store.get(opid)

    def dispatch(self, name, args):
        validate(input_schema(name,self.config['installation']['id']), args)
        self.store.reconcile()
        if 'installation_id' in args:
            self.installation(args['installation_id'])
        if name == 'asa_host_inventory':
            from .admin import inventory
            return inventory(self.config)
        if name == 'asa_cloud_transfer':
            if self.config.get('cloud_storage_enabled') is not True:
                raise OpsError('CLOUD_DISABLED', 'Cloud transport is not installed/enabled')
            if (args['action'] == 'list' and ('filename' in args or 'sha256' in args)) or (args['action'] != 'list' and not args.get('filename')):
                raise OpsError('INVALID_ARGUMENT', 'Transfers require filename; list does not accept filename or sha256')
            import shlex
            command = ['/usr/bin/python3', '/opt/server-settings/asa-operations/cloud_storage.py',
                       args['action'], '--project', args['project']]
            if args.get('filename'):
                command += ['--filename', args['filename']]
            if args.get('sha256'):
                command += ['--sha256', args['sha256']]
            if args['action'] != 'list':
                command += ['--apply']
            return self.dispatch('asa_admin_execute', {'installation_id': args['installation_id'],
                                 'command': shlex.join(command), 'cwd': self.config['installation']['root'],
                                 'timeout_seconds': 7000, 'idempotency_key': args['idempotency_key']})
        if name == 'asa_admin_status':
            from .admin import request
            value = request(self.config, {'action': 'status', **({'operation_id': args['operation_id']} if args.get('operation_id') else {})})
            if args.get('operation_id') and value.get('status') == 'complete':
                record = self.store.get(args['operation_id'])
                self.installation(record['installation_id'])
                if record['kind'] == 'admin' and record['status'] == 'recovery_required':
                    self.store.update(record['operation_id'], status='success' if value.get('exit_code') == 0 and not value.get('timed_out') else 'failed',
                                      phase='root_audit_reconciled', result=value)
            return value
        if name == 'asa_admin_execute':
            from .admin import enabled
            from .maintenance import instance
            enabled(self.config)
            with self.lock:
                reused = self.store.reused(args['idempotency_key'], {'method': name, **args})
                if reused:
                    return {'operation': reused, 'reused': True}
                generation = instance(self.config['installation']['root'])
                if not generation:
                    raise OpsError('MAINTENANCE_NOT_CONFIGURED', 'Installation identity is missing')
                planned = self.store.plan(args['installation_id'], {
                    'kind': 'admin', 'installation_instance_id': generation,
                    'command': args['command'], 'cwd': args.get('cwd', self.config['installation']['root']),
                    'timeout_seconds': args.get('timeout_seconds', 3600)})
                record, created = self.store.start(args['installation_id'], 'admin', planned['plan_id'], args['idempotency_key'], {'method': name, **args})
                if created:
                    record = self.launch(record, 'admin')
                return {'operation': record, 'reused': not created}
        if name == 'asa_operation_get':
            record = self.store.get(args['operation_id'])
            self.installation(record['installation_id'])
            return dict(record, log=self.store.log(record['operation_id'], args.get('log_cursor', 0)))
        if name == 'asa_operation_list':
            return {'operations': self.store.list(args['installation_id'], args.get('limit', 20))}
        if name == 'asa_restore_plan':
            with self.lock:
                if any(r['status'] in ('queued', 'running') for r in self.store.list(args['installation_id'], 100)):
                    raise OpsError('BUSY', 'Wait for the active server worker')
                value = adapter(self.config_path, 'restore_plan', {'snapshot_id': args['snapshot_id']})
                return {'plan': self.store.plan(args['installation_id'], value)}
        if name == 'asa_update_plan':
            with self.lock:
                active = [r for r in self.store.list(args['installation_id'], 100) if r['status'] in ('queued', 'running', 'recovery_required')]
                if active:
                    if active[0]['status'] == 'recovery_required':
                        previous = self.store.get_plan(active[0]['plan_id'])
                        value = adapter(self.config_path, 'plan', {'pinned_release': previous['release']})
                        planned = self.store.plan(args['installation_id'], value)
                        return {'plan': planned, 'recovery_operation': active[0]}
                    raise OpsError('BUSY', 'Inspect the active operation before preparing another plan')
                value = adapter(self.config_path, 'plan', {})
                planned = self.store.plan(args['installation_id'], value)
                result = {'plan': planned}
                if value.get('recovery_receipt'):
                    from .store import digest
                    key = 'recovery:'+digest(value['recovery_receipt'])
                    record, created = self.store.start(args['installation_id'], 'update', planned['plan_id'], key,
                                                       {'recovery_receipt_hash': digest(value['recovery_receipt'])})
                    if created:
                        self.store.update(record['operation_id'], status='recovery_required', phase='imported_manager_receipt')
                    result['recovery_operation'] = self.store.get(record['operation_id'])
                return result
        if name in ('asa_update_start', 'asa_deployment_verify', 'asa_restore_rehearse'):
            with self.lock:
                # Hard-deny the new source even if an old or forged immutable plan
                # claims ready_to_rehearse=True. Check before idempotent replay,
                # generation reads or any worker/manager invocation.
                plan = self.store.get_plan(args['plan_id']) if name == 'asa_restore_rehearse' else None
                if plan is not None and plan.get('snapshot_id') == '20261026T231505Z-038beee0':
                    raise OpsError('BLOCKED_PLAN', '26 October rehearsal requires the separate P1-R3 isolation')
                reused = self.store.reused(args['idempotency_key'], {'method': name, **args})
                if reused:
                    return {'operation': reused, 'reused': True}
                if plan is None:
                    plan = self.store.get_plan(args['plan_id'])
                self.installation(plan['installation_id'])
                check_generation(self.config['installation']['root'],plan)
                if (name == 'asa_restore_rehearse') != (plan.get('kind') == 'restore'):
                    raise OpsError('PLAN_KIND', 'Restore plans and update plans are not interchangeable')
                if name == 'asa_update_start' and not plan.get('ready'):
                    raise OpsError('BLOCKED_PLAN', 'Preflight blockers must be resolved in a new plan')
                if name == 'asa_restore_rehearse' and plan.get('ready_to_rehearse') is not True:
                    raise OpsError('BLOCKED_PLAN', 'Source or isolated rehearsal has not been approved')
                if name == 'asa_restore_rehearse' and plan.get('snapshot_id') == '20261026T231505Z-038beee0':
                    import datetime as dt
                    age = (dt.datetime.now(dt.timezone.utc)-dt.datetime.fromisoformat(plan['created_at'])).total_seconds()
                    if age > self.config.get('plan_ttl_seconds', 3600) or age < -60:
                        raise OpsError('EXPIRED_PLAN', 'Prepare a fresh source-attested restore plan')
                if name == 'asa_update_start':
                    import datetime as dt
                    age = (dt.datetime.now(dt.timezone.utc)-dt.datetime.fromisoformat(plan['created_at'])).total_seconds()
                    if age > self.config.get('plan_ttl_seconds', 3600):
                        raise OpsError('EXPIRED_PLAN', 'Prepare a new plan; this plan expired')
                kind = 'restore_rehearse' if name == 'asa_restore_rehearse' else 'update' if name == 'asa_update_start' else 'verify'
                record, created = self.store.start(args['installation_id'], kind, args['plan_id'], args['idempotency_key'],
                                                   {'method': name, **args})
                if created:
                    record = self.launch(record, kind)
                return {'operation': record, 'reused': not created}
        if name == 'asa_operation_finalize':
            with self.lock, self.store.db() as db:
                from .store import identifier, digest, now
                opid = identifier(args['operation_id'])
                db.execute('BEGIN IMMEDIATE')
                row = db.execute('SELECT * FROM operations WHERE id=?', (opid,)).fetchone()
                if row is None:
                    raise OpsError('NOT_FOUND', 'Operation does not exist')
                self.installation(row['installation'])
                if row['status']=='superseded':
                    raise OpsError('STALE_INSTALLATION','Operation belongs to a replaced installation')
                check_generation(self.config['installation']['root'],self.store.get_plan(row['plan_id']))
                if row['kind'] != 'update':
                    raise OpsError('FINALIZE_KIND', 'Only an update operation can be finalized')
                # Do not overwrite the start key. Finalize has its own durable key.
                db.execute('CREATE TABLE IF NOT EXISTS finalizations(id TEXT PRIMARY KEY, operation TEXT, request_hash TEXT)')
                key = args['idempotency_key']
                old = db.execute('SELECT * FROM finalizations WHERE id=?', (key,)).fetchone()
                if old and (old['operation'] != opid or old['request_hash'] != digest(args)):
                    raise OpsError('IDEMPOTENCY_CONFLICT', 'Finalize key belongs to another request')
                if old:
                    return {'operation': self.store._public(row), 'reused': True}
                if row['status'] in ('running', 'queued'):
                    raise OpsError('BUSY', 'The operation still has an active worker')
                if db.execute("SELECT 1 FROM operations WHERE installation=? AND id!=? AND status IN ('queued','running')", (row['installation'], opid)).fetchone():
                    raise OpsError('BUSY', 'Another worker is running; finalization is deferred')
                if row['status'] == 'success':
                    return {'operation': self.store._public(row), 'reused': True}
                db.execute('INSERT OR IGNORE INTO finalizations VALUES(?,?,?)', (key, opid, digest(args)))
                try:
                    db.execute("UPDATE operations SET status='queued',phase='finalize',updated=? WHERE id=?", (now(), opid))
                except Exception as exc:
                    raise OpsError('BUSY', 'Another operation owns this installation') from exc
            return {'operation': self.launch(self.store.get(opid), 'finalize'), 'reused': False}
        raise OpsError('METHOD', 'Unknown operation')

    def rpc(self, request):
        method, params = request.get('method'), request.get('params') or {}
        if method == 'initialize':
            return {'protocolVersion': '2025-03-26', 'capabilities': {'tools': {'listChanged': False}},
                    'serverInfo': {'name': 'asa-1panel-operations', 'version': '1.0.0'}}
        if method == 'ping':
            return {}
        if method == 'tools/list':
            result = self.official.request(method, {})
            for tool in result['tools']:
                if tool.get('name')=='list_databases':
                    tool['description']='List registered 1Panel database instances. Compose PostgreSQL is inventoried separately by ASA runtime_identity.'
            return {'tools': result['tools']+tools(self.config['installation']['id'], self.config.get('host_admin_enabled') is True,
                                                 self.config.get('cloud_storage_enabled') is True)}
        if method == 'tools/call':
            name = params.get('name')
            if name=='list_databases':
                from .panel_api import list_databases
                return list_databases(params.get('arguments') or {})
            if name not in INPUTS:
                return self.official.request(method, params)
            try:
                data = self.dispatch(name, params.get('arguments', {}))
                content = {'ok': True, 'data': data, 'error': None}
            except OpsError as exc:
                content = {'ok': False, 'data': None, 'error': {'code': exc.code, 'message': str(exc)}}
            return {'content': [{'type': 'text', 'text': json.dumps(content)}], 'structuredContent': content,
                    'isError': not content['ok']}
        raise OpsError('METHOD_NOT_FOUND', 'Unsupported MCP method')


def serve(config_path):
    controller = Controller(config_path)
    token = os.environ.get('ASA_OPS_TOKEN', '')
    if len(token) < 32:
        raise RuntimeError('ASA_OPS_TOKEN is required')
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Do not journal request bodies, authorization or URL parameters.

        def reply(self, code, value=None):
            raw = json.dumps(value).encode() if value is not None else b''
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass  # An accepted durable operation continues.

        def do_POST(self):
            if self.path != '/mcp':
                return self.reply(404)
            if not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer '+token):
                return self.reply(401)
            if self.headers.get('Origin'):
                # Only server-to-server proxy traffic belongs on this private hop.
                return self.reply(403)
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 65536:
                    return self.reply(413)
                request = json.loads(self.rfile.read(length))
                if not isinstance(request, dict) or request.get('jsonrpc') != '2.0' or not isinstance(request.get('method'), str):
                    return self.reply(400)
                if 'id' not in request:
                    return self.reply(202)
                result = controller.rpc(request)
                return self.reply(200, {'jsonrpc': '2.0', 'id': request['id'], 'result': result})
            except (ValueError, TypeError):
                return self.reply(400)
            except OpsError as exc:
                return self.reply(200, {'jsonrpc': '2.0', 'id': request.get('id'),
                                  'error': {'code': -32000, 'message': exc.code+': '+str(exc)}})
            except Exception:
                return self.reply(200, {'jsonrpc': '2.0', 'id': request.get('id'),
                                  'error': {'code': -32603, 'message': 'Internal operation error; no automatic retry'}})

        def do_GET(self):
            self.reply(405)

        def do_DELETE(self):
            self.reply(405)
    server = http.server.ThreadingHTTPServer(('127.0.0.1', controller.config.get('port', 39446)), Handler)
    server.daemon_threads = True
    server.serve_forever()


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--config', required=True)
    serve(p.parse_args().config)
