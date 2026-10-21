"""Closed input schemas. Callers select identities, never commands or paths."""
import re
from .store import OpsError

INSTALLATION = {'type': 'string', 'pattern': '^[A-Za-z][A-Za-z0-9_-]{0,63}$'}
ID = {'type': 'string', 'pattern': '^[a-f0-9]{32}$'}
KEY = {'type': 'string', 'pattern': '^[A-Za-z0-9_.:-]{16,128}$'}


def obj(properties, required):
    return {'type': 'object', 'properties': properties, 'required': required, 'additionalProperties': False}


INPUTS = {
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
    'asa_restore_plan': 'Plan restoration of the explicitly approved catalog snapshot, pin its manifest and the actual live release, and inventory current data. Never replaces a database or objects. This is separate from update_start.',
    'asa_restore_rehearse': 'Restore only into a new test database, migrate it to the pinned schema, compare row fingerprints, and verify a disposable network-isolated MinIO clone. Returns durable operation_id; success means rehearsal completed, not live restore accepted. Never switches live data.',
    'asa_update_plan': 'Preflight the fixed ASA installation and pin an immutable release plan. Preserve existing PostgreSQL/MinIO data. Reports blockers before downtime; never starts an update.',
    'asa_update_start': 'Start one approved, ready, immutable ASA update plan on the server. Returns operation_id immediately. Reuse the same idempotency key after a lost response; never launch a second update.',
    'asa_operation_get': 'Read durable operation state, exact release/data provenance, verification results and incremental sanitized log using a byte cursor. Does not restart work.',
    'asa_operation_list': 'Find active and recent server operations after losing the chat or operation ID. Does not resume migrations.',
    'asa_deployment_verify': 'Start server-side technical and isolated browser acceptance for a pinned plan. Returns operation_id. Distinguishes container health from user-flow acceptance.',
    'asa_operation_finalize': 'Verify and reconcile receipts for an interrupted known update. Never reruns migrations or restarts containers. Refuses missing backup, drift or incomplete browser acceptance.',
}
READ = {'asa_update_plan', 'asa_operation_get', 'asa_operation_list', 'asa_restore_plan'}
OUTPUT = obj({'ok': {'type': 'boolean'}, 'data': {'type': ['object', 'null']},
              'error': {'anyOf': [obj({'code': {'type': 'string'}, 'message': {'type': 'string'}}, ['code', 'message']), {'type': 'null'}]}}, ['ok', 'data', 'error'])


def tools():
    return [{'name': name, 'description': DESCRIPTIONS[name], 'inputSchema': schema, 'outputSchema': OUTPUT,
             'annotations': {'readOnlyHint': name in READ, 'destructiveHint': name not in READ,
                             'idempotentHint': True, 'openWorldHint': False}}
            for name, schema in INPUTS.items()]


def validate(schema, value):
    if not isinstance(value, dict):
        raise OpsError('INVALID_ARGUMENT', 'Arguments must be an object')
    if not set(schema['required']) <= set(value) or not set(value) <= set(schema['properties']):
        raise OpsError('INVALID_ARGUMENT', 'Missing or unknown argument')
    for key, v in value.items():
        rule = schema['properties'][key]
        if rule['type'] == 'string':
            ok = isinstance(v, str) and len(v) <= 128
            ok = ok and ('enum' not in rule or v in rule['enum'])
            ok = ok and ('pattern' not in rule or re.fullmatch(rule['pattern'], v) is not None)
        else:
            ok = type(v) is int and rule['minimum'] <= v <= rule['maximum']
        if not ok:
            raise OpsError('INVALID_ARGUMENT', 'Invalid value for '+key)
