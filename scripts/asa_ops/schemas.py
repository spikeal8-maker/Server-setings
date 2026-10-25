"""Closed schemas, including explicitly owner-enabled privileged administration."""
import re
import copy
from .store import OpsError

INSTALLATION = {'type': 'string', 'enum': ['asa-test']}
ID = {'type': 'string', 'pattern': '^[a-f0-9]{32}$'}
KEY = {'type': 'string', 'pattern': '^[A-Za-z0-9_.:-]{16,128}$'}


def obj(properties, required):
    return {'type': 'object', 'properties': properties, 'required': required, 'additionalProperties': False}


INPUTS = {
    'asa_host_inventory': obj({'installation_id': INSTALLATION}, ['installation_id']),
    'asa_admin_status': obj({'installation_id': INSTALLATION, 'operation_id': ID}, ['installation_id']),
    'asa_admin_execute': obj({'installation_id': INSTALLATION, 'command': {'type': 'string', 'minLength': 1, 'maxLength': 32000},
                              'cwd': {'type': 'string', 'minLength': 1, 'maxLength': 4096},
                              'timeout_seconds': {'type': 'integer', 'minimum': 1, 'maximum': 7200},
                              'idempotency_key': KEY}, ['installation_id', 'command', 'idempotency_key']),
    'asa_cloud_transfer': obj({'installation_id': INSTALLATION,
                              'action': {'type':'string','enum':['list','upload','download']},
                              'project': {'type':'string','pattern':'^[a-z0-9][a-z0-9_-]{0,63}$'},
                              'filename': {'type':'string','pattern':'^[A-Za-z0-9][A-Za-z0-9_.-]{0,180}\\.age$','maxLength':185},
                              'sha256': {'type':'string','pattern':'^[a-f0-9]{64}$'},
                              'idempotency_key':KEY}, ['installation_id','action','project','idempotency_key']),
    'asa_restore_plan': obj({'installation_id': INSTALLATION, 'snapshot_id': {'type': 'string', 'enum': ['20261017T231505Z-65b68074']}}, ['installation_id', 'snapshot_id']),
    'asa_restore_rehearse': obj({'installation_id': INSTALLATION, 'plan_id': ID, 'idempotency_key': KEY}, ['installation_id', 'plan_id', 'idempotency_key']),
    'asa_update_plan': obj({'installation_id': INSTALLATION, 'data_mode': {'type': 'string', 'enum': ['preserve']}}, ['installation_id', 'data_mode']),
    'asa_update_start': obj({'installation_id': INSTALLATION, 'plan_id': ID, 'idempotency_key': KEY}, ['installation_id', 'plan_id', 'idempotency_key']),
    'asa_operation_get': obj({'operation_id': ID, 'log_cursor': {'type': 'integer', 'minimum': 0, 'maximum': 1000000000}}, ['operation_id']),
    'asa_operation_list': obj({'installation_id': INSTALLATION, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 100}}, ['installation_id']),
    'asa_deployment_verify': obj({'installation_id': INSTALLATION, 'plan_id': ID, 'idempotency_key': KEY}, ['installation_id', 'plan_id', 'idempotency_key']),
    'asa_operation_finalize': obj({'operation_id': ID, 'idempotency_key': KEY}, ['operation_id', 'idempotency_key']),
}
DESCRIPTIONS = {
    'asa_host_inventory': 'Read actual host and Docker container metadata without exposing container environments. Includes whether owner-authorized root administration is enabled.',
    'asa_admin_status': 'Inspect the private root executor or root audit state of an administration operation. Never reruns commands. Reconciles a completed root job after a lost worker response.',
    'asa_admin_execute': 'FULL ROOT ADMINISTRATION of the configured test host, explicitly enabled by its owner. Run a shell command for Docker/Compose, Git, builds, files, services or recovery. This can bypass ordinary deployment gates and can destroy data. Do not put credentials in commands or print secrets. Prefer update_plan/start for normal releases. Returns durable operation_id; poll operation_get. Retry only with the SAME idempotency_key. cwd defaults to the ASA root; timeout defaults to3600 seconds, maximum7200. Private raw logs and root audit are retained; only bounded known-secret-redacted output is returned. No automatic backup or rollback is performed for arbitrary commands.',
    'asa_cloud_transfer': 'Shared Docker Yandex Disk: asynchronously list/upload/download encrypted .age backup archives for any project. Project asa-lab uses existing ASA-Backups; other project slugs use Server-Backups/project. Upload reads /srv/asa-cloud-storage/data/project/outgoing/filename. Download writes incoming only, refuses existing files and never restores a live database. Optional sha256 verifies bytes. Poll operation_get for JSON output/result. Requires one-time owner OAuth connection. Never sends login tokens in arguments; no mirror/delete or backup schedule.',
    'asa_restore_plan': 'Plan restoration of the explicitly approved catalog snapshot, pin its manifest and the actual live release, and inventory current data. Never replaces a database or objects. This is separate from update_start.',
    'asa_restore_rehearse': 'Restore only into a new test database, migrate it to the pinned schema, compare row fingerprints, and verify a disposable network-isolated MinIO clone. Returns durable operation_id; success means rehearsal completed, not live restore accepted. Never switches live data.',
    'asa_update_plan': 'Preflight the fixed ASA installation and pin an immutable release plan. Preserve existing PostgreSQL/MinIO data. Reports blockers before downtime; never starts an update.',
    'asa_update_start': 'Start one approved, ready, immutable ASA update plan on the server. Returns operation_id immediately. Reuse the same idempotency key after a lost response; never launch a second update.',
    'asa_operation_get': 'Read durable operation state, exact release/data provenance, verification results and incremental sanitized log using a byte cursor. Does not restart work.',
    'asa_operation_list': 'Find active and recent server operations after losing the chat or operation ID. Does not resume migrations.',
    'asa_deployment_verify': 'Start server-side technical and isolated browser acceptance for a pinned plan. Returns operation_id. Distinguishes container health from user-flow acceptance.',
    'asa_operation_finalize': 'Verify and reconcile receipts for an interrupted known update. Never reruns migrations or restarts containers. Refuses missing backup, drift or incomplete browser acceptance.',
}
READ = {'asa_update_plan', 'asa_operation_get', 'asa_operation_list', 'asa_restore_plan', 'asa_host_inventory', 'asa_admin_status'}
OUTPUT = obj({'ok': {'type': 'boolean'}, 'data': {'type': ['object', 'null']},
              'error': {'anyOf': [obj({'code': {'type': 'string'}, 'message': {'type': 'string'}}, ['code', 'message']), {'type': 'null'}]}}, ['ok', 'data', 'error'])


def input_schema(name, installation_id='asa-test'):
    schema=copy.deepcopy(INPUTS[name])
    if 'installation_id' in schema['properties']:
        schema['properties']['installation_id']={'type':'string','enum':[installation_id]}
    return schema


def tools(installation_id='asa-test', host_admin_enabled=False, cloud_storage_enabled=False):
    return [{'name': name, 'description': DESCRIPTIONS[name], 'inputSchema': input_schema(name,installation_id), 'outputSchema': OUTPUT,
             'annotations': {'readOnlyHint': name in READ, 'destructiveHint': name not in READ,
                             'idempotentHint': True, 'openWorldHint': name in ('asa_admin_execute','asa_cloud_transfer')}}
            for name, schema in INPUTS.items()
            if (host_admin_enabled or name not in ('asa_admin_execute', 'asa_admin_status'))
            and (cloud_storage_enabled and host_admin_enabled or name != 'asa_cloud_transfer')]


def validate(schema, value):
    if not isinstance(value, dict):
        raise OpsError('INVALID_ARGUMENT', 'Arguments must be an object')
    if not set(schema['required']) <= set(value) or not set(value) <= set(schema['properties']):
        raise OpsError('INVALID_ARGUMENT', 'Missing or unknown argument')
    for key, v in value.items():
        rule = schema['properties'][key]
        if rule['type'] == 'string':
            ok = isinstance(v, str) and rule.get('minLength', 0) <= len(v) <= rule.get('maxLength', 128) and '\0' not in v
            ok = ok and ('enum' not in rule or v in rule['enum'])
            ok = ok and ('pattern' not in rule or re.fullmatch(rule['pattern'], v) is not None)
        else:
            ok = type(v) is int and rule['minimum'] <= v <= rule['maximum']
        if not ok:
            raise OpsError('INVALID_ARGUMENT', 'Invalid value for '+key)
