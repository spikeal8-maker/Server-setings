"""P1-R3A: inert isolation specification, never a Docker/SQL/MinIO executor.

This is NOT a Compose document, an MCP schema, or an execution authorization.
The future trusted bridge must load the immutable plan from the private Store,
run verify_staged(), capture current installation generation, complete engine
inventory/capacity, and hold an operation lock. None of that I/O happens here.
TrustedContext is an internal dependency, NOT deserializable client evidence:
hashes and a constructed SourceProof alone do not authenticate owner signatures.

All positive tests use synthetic trusted inputs and patched release policies.
Production owner pin remains None and the reviewed image policy is empty.
Even a valid specification always denies rehearsal/apply/execution. Actual
quota enforcement, host/network isolation, fresh secret provisioning and atomic
create-only reservations require a separately reviewed future implementation.
"""
from dataclasses import dataclass
import datetime as dt
import hashlib
import json
import re

from . import source_registry as sources
from .store import OpsError

PLAN_FORMAT = 'asa-isolation-plan-v1'
CONTRACT_FORMAT = 'asa-isolation-contract-v1'
# Reviewed (manifest_sha256, postgres_digest_ref, minio_digest_ref) tuples only.
# Do not derive approvals from a plan, MCP arguments, environment or staging.
APPROVED_IMAGE_SETS = ()
MIB = 1024 ** 2
GIB = 1024 ** 3
MAX_TTL = 900
EVIDENCE_MAX_AGE = 60
HEX64 = re.compile(r'[a-f0-9]{64}\Z')
HEX32 = re.compile(r'[a-f0-9]{32}\Z')
NAME = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}\Z')
PLAN_KEYS = frozenset(('format', 'kind', 'plan_id', 'plan_hash',
    'installation_id', 'installation_instance_id', 'operation_id', 'snapshot_id',
    'source_sha256', 'manifest_sha256', 'owner_receipt_sha256',
    'staging_receipt_sha256', 'created_at', 'expires_at', 'images', 'limits',
    'ready_to_rehearse', 'ready_to_apply'))
LIMIT_BOUNDS = {
    'postgres_cpu_millis': (100, 4000), 'minio_cpu_millis': (100, 4000),
    'postgres_memory_bytes': (256 * MIB, 16 * GIB),
    'minio_memory_bytes': (256 * MIB, 16 * GIB),
    'postgres_data_bytes': (MIB, 1024 * GIB),
    'postgres_wal_bytes': (MIB, 1024 * GIB),
    'minio_data_bytes': (MIB, 1024 * GIB),
    'postgres_tmpfs_bytes': (MIB, GIB), 'minio_tmpfs_bytes': (MIB, GIB),
    'log_bytes': (MIB, 64 * MIB), 'max_seconds': (1, MAX_TTL),
}


@dataclass(frozen=True)
class SourceProof:
    """Nonsecret projection of a trusted signature AND full staging verifier."""
    snapshot_id: str
    ciphertext_sha256: str
    manifest_sha256: str
    owner_receipt_sha256: str
    staging_receipt_sha256: str
    owner_spki_sha256: str
    verified_at: int


@dataclass(frozen=True)
class Inventory:
    """Complete current names, captured privately for this engine."""
    engine_id: str
    observed_at: int
    containers: tuple
    volumes: tuple
    networks: tuple


@dataclass(frozen=True)
class Capacity:
    cpu_millis: int
    memory_bytes: int
    disk_bytes: int
    hard_volume_quotas: bool


@dataclass(frozen=True)
class TrustedContext:
    """No JSON/MCP constructor. Only a future privileged, reviewed producer."""
    plan_id: str
    plan_hash: str
    installation_id: str
    installation_instance_id: str
    operation_id: str
    source: SourceProof
    inventory: Inventory
    capacity: Capacity


@dataclass(frozen=True)
class CreatedResource:
    """Private creation journal, NOT a receipt supplied with a public spec.

    native_id: full container/network ID; for a volume, immutable allocation
    identity of the quota backend. A Docker volume name/label is insufficient.
    """
    engine_id: str
    kind: str
    name: str
    native_id: str
    contract_sha256: str
    created_at: int


@dataclass(frozen=True)
class ObservedResource:
    """Fresh trusted inspect projection; labels are immutable tuple pairs."""
    engine_id: str
    kind: str
    name: str
    native_id: str
    labels: tuple
    in_use: bool
    observed_at: int


def _require(condition, code):
    if not condition:
        # Never echo rejected input: it may contain a DSN or a secret.
        raise OpsError(code, 'Isolation contract prerequisite or invariant failed')


def _hex(value, pattern=HEX64):
    return type(value) is str and pattern.fullmatch(value) is not None


def _positive(value):
    return type(value) is int and 0 < value <= 2 ** 53


def _canonical(value):
    visited = 0
    def plain(item, depth=0):
        nonlocal visited
        visited += 1
        _require(visited <= 4096, 'CONTRACT_SCHEMA')
        _require(depth <= 12, 'CONTRACT_SCHEMA')
        if type(item) is dict:
            _require(len(item) <= 64 and all(type(k) is str for k in item), 'CONTRACT_SCHEMA')
            for key, child in item.items():
                _require(len(key) <= 128, 'CONTRACT_SCHEMA')
                plain(child, depth + 1)
        elif type(item) is list:
            _require(len(item) <= 128, 'CONTRACT_SCHEMA')
            for child in item:
                plain(child, depth + 1)
        elif type(item) is str:
            _require(len(item) <= 2048, 'CONTRACT_SCHEMA')
        else:
            _require(item is None or type(item) is bool or
                     (type(item) is int and abs(item) <= 2 ** 53), 'CONTRACT_SCHEMA')
    plain(value)
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True, allow_nan=False).encode('ascii')


def _digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _snapshot(value):
    """Detach public mutable inputs before checking or using their contents."""
    try:
        result = json.loads(_canonical(value))
        # Check the detached value too: the caller may mutate during encoding.
        _canonical(result)
        return result
    except (RuntimeError, TypeError, ValueError, OverflowError):
        raise OpsError('CONTRACT_SCHEMA', 'Invalid or unstable JSON input') from None


def _timestamp(value):
    _require(type(value) is str and re.fullmatch(
        r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)', value),
        'PLAN_TIME')
    try:
        return dt.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except ValueError:
        raise OpsError('PLAN_TIME', 'Invalid UTC plan time') from None


def _check_inputs(plan, context, now, *, creating):
    _require(type(plan) is dict and set(plan) == PLAN_KEYS, 'PLAN_SCHEMA')
    _require(plan['ready_to_rehearse'] is False and plan['ready_to_apply'] is False, 'BLOCKED_PLAN')
    _require(type(context) is TrustedContext and _positive(now), 'TRUST_CONTEXT')
    pin = sources.OWNER_ED25519_SPKI_SHA256
    _require(_hex(pin), 'BLOCKED_KEY_ACCESS')
    _require(type(context.source) is SourceProof, 'SOURCE_PROOF')
    proof = context.source
    _require(proof.snapshot_id == sources.NEW_ID and
             proof.ciphertext_sha256 == sources.NEW_SOURCE['ciphertext_sha256'], 'SOURCE_IDENTITY')
    for name in ('manifest_sha256', 'owner_receipt_sha256', 'staging_receipt_sha256', 'owner_spki_sha256'):
        _require(_hex(getattr(proof, name)), 'SOURCE_PROOF')
    _require(proof.owner_spki_sha256 == pin, 'OWNER_KEY_UNTRUSTED')
    _require(_positive(proof.verified_at) and proof.verified_at <= now, 'STALE_PROOF')
    if creating:
        _require(now - proof.verified_at <= EVIDENCE_MAX_AGE, 'STALE_PROOF')
    _require(_hex(context.plan_id, HEX32) and _hex(context.plan_hash) and
             _hex(context.operation_id, HEX32) and _hex(context.installation_instance_id, HEX32), 'TRUST_CONTEXT')
    _require(type(context.installation_id) is str and
             re.fullmatch(r'[a-z][a-z0-9-]{1,62}', context.installation_id), 'TRUST_CONTEXT')
    _require(plan['format'] == PLAN_FORMAT and plan['kind'] == 'isolation_contract', 'PLAN_SCHEMA')
    for key in ('plan_id', 'plan_hash', 'installation_id', 'installation_instance_id', 'operation_id'):
        _require(type(plan[key]) is str and plan[key] == getattr(context, key), 'PLAN_BINDING')
    _require(plan['plan_hash'] == _digest({k: v for k, v in plan.items()
                                        if k not in ('plan_id', 'plan_hash')}), 'PLAN_CHANGED')
    for key, expected in (
            ('snapshot_id', proof.snapshot_id), ('source_sha256', proof.ciphertext_sha256),
            ('manifest_sha256', proof.manifest_sha256), ('owner_receipt_sha256', proof.owner_receipt_sha256),
            ('staging_receipt_sha256', proof.staging_receipt_sha256)):
        _require(type(plan[key]) is str and plan[key] == expected, 'SOURCE_BINDING')
    created, expires = _timestamp(plan['created_at']), _timestamp(plan['expires_at'])
    _require(created <= now < expires and 0 < expires - created <= MAX_TTL, 'PLAN_EXPIRED')
    images = plan['images']
    _require(type(images) is dict and set(images) == {'postgres', 'minio'}, 'IMAGE_PIN')
    for role, repo in (('postgres', 'docker.io/library/postgres'), ('minio', 'quay.io/minio/minio')):
        _require(type(images[role]) is str and re.fullmatch(re.escape(repo) + r'@sha256:[a-f0-9]{64}', images[role]), 'IMAGE_PIN')
    _require(type(APPROVED_IMAGE_SETS) is tuple and
             (proof.manifest_sha256, images['postgres'], images['minio']) in APPROVED_IMAGE_SETS, 'IMAGE_NOT_APPROVED')
    limits = plan['limits']
    _require(type(limits) is dict and set(limits) == set(LIMIT_BOUNDS), 'RESOURCE_SCHEMA')
    for key, (low, high) in LIMIT_BOUNDS.items():
        _require(type(limits[key]) is int and low <= limits[key] <= high, 'RESOURCE_LIMIT')
    _require(expires - created <= limits['max_seconds'], 'RESOURCE_LIMIT')
    inventory, capacity = context.inventory, context.capacity
    _require(type(inventory) is Inventory and _hex(inventory.engine_id), 'INVENTORY')
    _require(_positive(inventory.observed_at) and 0 <= now - inventory.observed_at <= EVIDENCE_MAX_AGE, 'STALE_INVENTORY')
    for names in (inventory.containers, inventory.volumes, inventory.networks):
        _require(type(names) is tuple and all(type(n) is str and NAME.fullmatch(n) for n in names), 'INVENTORY')
        _require(len(set(names)) == len(names), 'INVENTORY')
    if creating:
        _require(type(capacity) is Capacity and all(_positive(getattr(capacity, n))
                 for n in ('cpu_millis', 'memory_bytes', 'disk_bytes')), 'RESOURCE_CAPACITY')
        _require(capacity.hard_volume_quotas is True, 'QUOTA_UNAVAILABLE')
        cpu = limits['postgres_cpu_millis'] + limits['minio_cpu_millis']
        # Conservative reservation also accounts for tmpfs and private /dev/shm.
        ram = sum(limits[k] for k in ('postgres_memory_bytes', 'minio_memory_bytes',
                                     'postgres_tmpfs_bytes', 'minio_tmpfs_bytes')) + 128 * MIB
        disk = sum(limits[k] for k in ('postgres_data_bytes', 'postgres_wal_bytes',
                                      'minio_data_bytes')) + 2 * limits['log_bytes']
        _require(cpu <= capacity.cpu_millis and ram <= capacity.memory_bytes and
                 disk <= capacity.disk_bytes, 'INSUFFICIENT_RESOURCES')


def _build_checked(plan, context, *, now, creating):
    """Derive only from a detached plan; creation admission is a separate gate."""
    _check_inputs(plan, context, now, creating=creating)
    prefix = 'asa-r3a-' + context.operation_id
    names = {key: prefix + '-' + suffix for key, suffix in (
        ('postgres', 'pg'), ('minio', 'minio'), ('postgres_data', 'pg-data'),
        ('postgres_wal', 'pg-wal'), ('minio_data', 'objects'), ('network', 'net'))}
    _require(len(set(names.values())) == len(names), 'RESOURCE_COLLISION')
    if creating:
        occupied = set(context.inventory.containers + context.inventory.volumes + context.inventory.networks)
        _require(not occupied.intersection(names.values()), 'RESOURCE_COLLISION')
    binding = {key: plan[key] for key in ('plan_id', 'plan_hash', 'installation_id',
        'installation_instance_id', 'operation_id', 'snapshot_id', 'source_sha256',
        'manifest_sha256', 'owner_receipt_sha256', 'staging_receipt_sha256', 'expires_at')}
    proof_hash = _digest({key: binding[key] for key in ('source_sha256', 'manifest_sha256',
                                      'owner_receipt_sha256', 'staging_receipt_sha256')})
    def labels(kind):
        return {'org.asa.restore.contract': CONTRACT_FORMAT,
            'org.asa.restore.kind': kind, 'org.asa.restore.operation': context.operation_id,
            'org.asa.restore.installation': context.installation_id,
            'org.asa.restore.generation': context.installation_instance_id,
            'org.asa.restore.plan': context.plan_id, 'org.asa.restore.plan-sha256': context.plan_hash,
            'org.asa.restore.snapshot': sources.NEW_ID, 'org.asa.restore.proof-sha256': proof_hash}
    limits = dict(plan['limits'])
    volumes = {role: {'name': names[role], 'create_only': True, 'external': False,
        'quota_bytes': limits[role + '_bytes'], 'quota_enforcement_required': 'hard_backend_allocation',
        'labels': labels(role)} for role in ('postgres_data', 'postgres_wal', 'minio_data')}
    network = {'name': names['network'], 'create_only': True, 'driver': 'bridge',
        'internal': True, 'attachable': False, 'external_networks': [],
        'host_access': 'deny_required', 'live_service_access': 'deny_required', 'labels': labels('network')}
    containers = {}
    for role in ('postgres', 'minio'):
        mounts = ([('postgres_data', '/var/lib/postgresql/data'), ('postgres_wal', '/var/lib/postgresql/wal')]
                  if role == 'postgres' else [('minio_data', '/data')])
        containers[role] = {'name': names[role], 'create_only': True, 'image': plan['images'][role],
            'labels': labels(role), 'networks': [names['network']], 'ports': [],
            'network_mode': 'private', 'pid_mode': 'private', 'ipc_mode': 'private',
            'privileged': False, 'devices': [], 'binds': [], 'docker_socket': False,
            'cap_drop': ['ALL'], 'no_new_privileges': True, 'requires_nonroot': True,
            'read_only_rootfs': True, 'environment': {}, 'command': None, 'entrypoint': None,
            'extra_hosts': [], 'dns': [],
            'mounts': [{'type': 'volume', 'source': names[key], 'target': target} for key, target in mounts],
            'tmpfs': {'target': '/tmp', 'bytes': limits[role + '_tmpfs_bytes'], 'noexec': True, 'mode': '0700'},
            'shm_bytes': 64 * MIB, 'pids_limit': 128,
            'cpu_millis': limits[role + '_cpu_millis'], 'memory_bytes': limits[role + '_memory_bytes'],
            'memory_swap_bytes': limits[role + '_memory_bytes'], 'log_limit_bytes': limits['log_bytes'],
            'credentials': {'principal': 'r3a_' + context.operation_id + ('_pg' if role == 'postgres' else '_s3'),
                'secret_slot': context.operation_id + '-' + role,
                'provisioning': 'fresh_private_runtime_only', 'reuse_live': False, 'public_values': False}}
    spec = {'format': CONTRACT_FORMAT, 'status': 'SPECIFICATION_ONLY', 'binding': binding,
        'execution_allowed': False, 'ready_to_rehearse': False, 'ready_to_apply': False,
        'engine_id': context.inventory.engine_id, 'network': network, 'volumes': volumes,
        'containers': containers, 'limits': limits,
        'postgres_target': {'host': names['postgres'], 'port': 5432, 'database': 'r3a_' + context.operation_id,
                            'fallback': None},
        'minio_target': {'host': names['minio'], 'port': 9000, 'fallback': None},
        'source_access': 'separately_verified_private_transfer_required',
        'cleanup_policy': 'exact_private_creation_journal_and_fresh_identity_only',
        'runtime_enforcement': 'NOT_VERIFIED', 'environment_state': 'NOT_CREATED'}
    return dict(spec, contract_sha256=_digest(spec))


def build_contract(plan, context, *, now):
    """Pure specification builder. Inputs/context are never fetched or mutated."""
    return _build_checked(_snapshot(plan), context, now=now, creating=True)


def _validated(spec, plan, context, *, now, creating):
    spec, plan = _snapshot(spec), _snapshot(plan)
    _require(type(spec) is dict, 'CONTRACT_SCHEMA')
    _require(all(spec.get(k) is False for k in
                 ('execution_allowed', 'ready_to_rehearse', 'ready_to_apply')), 'BLOCKED_PLAN')
    expected = _build_checked(plan, context, now=now, creating=creating)
    _require(_canonical(spec) == _canonical(expected), 'CONTRACT_CHANGED')
    return expected, plan


def validate_contract(spec, plan, context, *, now):
    """Return a detached validated spec; callers must use this returned snapshot.

    Public hashes do not authenticate a plan. This also checks fresh creation
    admission; the returned snapshot still grants no execution permission.
    """
    return _validated(spec, plan, context, now=now, creating=True)[0]


def validate_cleanup_target(spec, plan, context, created, observed, *, now):
    """Identify ONE owned inactive resource; never grant deletion or emit commands.

    Creation journal and fresh observation must be trusted independently of
    labels. Expired plans fail closed here too; orphan recovery is a later task.
    A future executor must re-inspect under its lock immediately before cleanup.
    """
    # Cleanup uses historical source proof and current resource identity. It
    # neither re-admits creation nor requires spare capacity to identify a
    # resource that already exists. Always consume the validated snapshots.
    spec, plan = _validated(spec, plan, context, now=now, creating=False)
    _require(type(created) is CreatedResource and type(observed) is ObservedResource, 'CLEANUP_PROOF')
    objects = [('network', spec['network'])]
    objects += [('volume', x) for x in spec['volumes'].values()]
    objects += [('container', x) for x in spec['containers'].values()]
    matches = [value for kind, value in objects if kind == created.kind and value['name'] == created.name]
    _require(len(matches) == 1, 'CLEANUP_FOREIGN')
    inventory_field = {'network': 'networks', 'volume': 'volumes', 'container': 'containers'}[created.kind]
    _require(created.name in getattr(context.inventory, inventory_field), 'CLEANUP_MISSING')
    _require(created.engine_id == context.inventory.engine_id and _hex(created.native_id) and
             created.contract_sha256 == spec['contract_sha256'], 'CLEANUP_PROOF')
    _require(_positive(created.created_at) and
             _timestamp(plan['created_at']) <= created.created_at <= now,
             'CLEANUP_PROOF')
    _require(_positive(observed.observed_at) and created.created_at <= observed.observed_at <= now and
             now - observed.observed_at <= 5,
             'CLEANUP_STALE')
    for key in ('engine_id', 'kind', 'name', 'native_id'):
        _require(type(getattr(observed, key)) is str and getattr(observed, key) == getattr(created, key), 'CLEANUP_FOREIGN')
    _require(type(observed.labels) is tuple and all(type(p) is tuple and len(p) == 2 and
             all(type(s) is str for s in p) for p in observed.labels), 'CLEANUP_LABELS')
    _require(len(dict(observed.labels)) == len(observed.labels) and
             dict(observed.labels) == matches[0]['labels'], 'CLEANUP_LABELS')
    _require(observed.in_use is False, 'CLEANUP_IN_USE')
    return {'engine_id': created.engine_id, 'kind': created.kind, 'name': created.name,
            'native_id': created.native_id, 'delete_authorized': False}
