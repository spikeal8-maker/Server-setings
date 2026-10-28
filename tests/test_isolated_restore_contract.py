"""P1-R3A: inert contract checks using synthetic trust context, never real services.

The policy pins below are fake test digests. Patching a trusted internal context
is not evidence that an owner signature, image or live archive was verified.
Every contract call runs with process, network, SQL and filesystem I/O denied.
"""
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from dataclasses import FrozenInstanceError, asdict, replace
import datetime as dt
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from asa_ops import isolated_restore_contract as contract
from asa_ops import restore, source_registry as sources
from asa_ops.schemas import INPUTS, validate
from asa_ops.store import OpsError, digest


MIB = 1024 ** 2
GIB = 1024 ** 3
NOW = 1793203200
PIN = 'a' * 64
MANIFEST = 'b' * 64
POSTGRES_IMAGE = 'docker.io/library/postgres@sha256:' + 'c' * 64
MINIO_IMAGE = 'quay.io/minio/minio@sha256:' + 'd' * 64


def timestamp(seconds):
    return dt.datetime.fromtimestamp(seconds, dt.timezone.utc).isoformat()


def rehash_plan(plan):
    plan['plan_hash'] = digest({key: value for key, value in plan.items()
                                if key not in {'plan_id', 'plan_hash'}})
    return plan


class IsolationContractTests(unittest.TestCase):
    def setUp(self):
        self.production_pin = sources.OWNER_ED25519_SPKI_SHA256
        self.production_images = contract.APPROVED_IMAGE_SETS
        policies = ExitStack()
        self.addCleanup(policies.close)
        policies.enter_context(patch.object(sources, 'OWNER_ED25519_SPKI_SHA256', PIN))
        policies.enter_context(patch.object(contract, 'APPROVED_IMAGE_SETS',
                                           ((MANIFEST, POSTGRES_IMAGE, MINIO_IMAGE),)))
        self.plan = rehash_plan({
            'format': 'asa-isolation-plan-v1', 'kind': 'isolation_contract',
            'plan_id': '1' * 32, 'installation_id': 'synthetic-installation',
            'installation_instance_id': '2' * 32, 'operation_id': '3' * 32,
            'snapshot_id': sources.NEW_ID,
            'source_sha256': sources.NEW_SOURCE['ciphertext_sha256'],
            'manifest_sha256': MANIFEST, 'owner_receipt_sha256': '4' * 64,
            'staging_receipt_sha256': '5' * 64,
            'created_at': timestamp(NOW), 'expires_at': timestamp(NOW + 600),
            'images': {'postgres': POSTGRES_IMAGE, 'minio': MINIO_IMAGE},
            'limits': {
                'postgres_cpu_millis': 1000, 'minio_cpu_millis': 1000,
                'postgres_memory_bytes': GIB, 'minio_memory_bytes': GIB,
                'postgres_data_bytes': 8 * GIB, 'postgres_wal_bytes': 4 * GIB,
                'minio_data_bytes': 8 * GIB,
                'postgres_tmpfs_bytes': 64 * MIB, 'minio_tmpfs_bytes': 64 * MIB,
                'log_bytes': 16 * MIB, 'max_seconds': 600,
            },
            'ready_to_rehearse': False, 'ready_to_apply': False,
        })
        self.context = contract.TrustedContext(
            plan_id=self.plan['plan_id'], plan_hash=self.plan['plan_hash'],
            installation_id=self.plan['installation_id'],
            installation_instance_id=self.plan['installation_instance_id'],
            operation_id=self.plan['operation_id'],
            source=contract.SourceProof(
                snapshot_id=sources.NEW_ID,
                ciphertext_sha256=sources.NEW_SOURCE['ciphertext_sha256'],
                manifest_sha256=MANIFEST, owner_receipt_sha256='4' * 64,
                staging_receipt_sha256='5' * 64, owner_spki_sha256=PIN,
                verified_at=NOW,
            ),
            inventory=contract.Inventory(
                engine_id='6' * 64, observed_at=NOW,
                containers=('synthetic-live-postgres', 'synthetic-live-minio'),
                volumes=('synthetic-live-pgdata', 'synthetic-live-objects'),
                networks=('synthetic-live-compose-default',),
            ),
            capacity=contract.Capacity(cpu_millis=8000, memory_bytes=16 * GIB,
                                       disk_bytes=128 * GIB, hard_volume_quotas=True),
        )

    @contextmanager
    def no_side_effects(self):
        targets = (
            'subprocess.run', 'subprocess.Popen', 'os.system', 'os.open',
            'os.unlink', 'os.mkdir', 'os.rmdir', 'builtins.open',
            'socket.socket', 'sqlite3.connect', 'pathlib.Path.open',
            'pathlib.Path.read_text', 'pathlib.Path.read_bytes',
            'pathlib.Path.write_text', 'pathlib.Path.write_bytes',
            'asa_ops.source_registry.verify_staged',
            'asa_ops.source_registry.owner_attestation',
            'asa_ops.adapter.load', 'asa_ops.restore.load',
            'asa_ops.restore.sql', 'asa_ops.restore.rehearse_objects',
            'asa_ops.store.Store',
        )
        with ExitStack() as stack:
            guards = [stack.enter_context(patch(target, side_effect=AssertionError(
                'Forbidden side effect: ' + target))) for target in targets]
            try:
                yield
            finally:
                for guard in guards:
                    guard.assert_not_called()

    def build(self, plan=None, context=None, now=NOW):
        with self.no_side_effects():
            return contract.build_contract(self.plan if plan is None else plan,
                                           self.context if context is None else context,
                                           now=now)

    def validate_spec(self, spec, plan=None, context=None, now=NOW):
        with self.no_side_effects():
            return contract.validate_contract(spec, self.plan if plan is None else plan,
                                              self.context if context is None else context,
                                              now=now)

    def cleanup_context(self, spec, now=NOW):
        inventory = replace(
            self.context.inventory, observed_at=now,
            containers=self.context.inventory.containers + tuple(
                resource['name'] for resource in spec['containers'].values()),
            volumes=self.context.inventory.volumes + tuple(
                resource['name'] for resource in spec['volumes'].values()),
            networks=self.context.inventory.networks + (spec['network']['name'],),
        )
        return replace(self.context, inventory=inventory)

    def cleanup(self, spec, created, observed, now=NOW, plan=None, context=None):
        context = self.cleanup_context(spec, now) if context is None else context
        with self.no_side_effects():
            return contract.validate_cleanup_target(spec, self.plan if plan is None else plan,
                                                    context,
                                                    created, observed, now=now)

    def rejected_spec(self, spec):
        # Rehashing attacker-controlled bytes must not replace trusted context.
        spec['contract_sha256'] = digest({key: value for key, value in spec.items()
                                          if key != 'contract_sha256'})
        with self.assertRaises(OpsError):
            self.validate_spec(spec)

    def trusted_plan_variant(self, **changes):
        plan = deepcopy(self.plan)
        plan.update(changes)
        rehash_plan(plan)
        return plan, replace(self.context, plan_hash=plan['plan_hash'])

    def cleanup_fixture(self, spec, kind='volume', role='postgres_data'):
        if kind == 'network':
            resource = spec['network']
        else:
            resource = spec['containers' if kind == 'container' else 'volumes'][role]
        created = contract.CreatedResource(
            engine_id=self.context.inventory.engine_id, kind=kind,
            name=resource['name'], native_id='7' * 64,
            contract_sha256=spec['contract_sha256'], created_at=NOW,
        )
        observed = contract.ObservedResource(
            engine_id=created.engine_id, kind=kind, name=created.name,
            native_id=created.native_id,
            labels=tuple(sorted(resource['labels'].items())), in_use=False,
            observed_at=NOW,
        )
        return created, observed

    def test_production_policy_is_unprovisioned_and_owner_pin_blocks(self):
        self.assertIsNone(self.production_pin)
        self.assertEqual(self.production_images, ())
        with patch.object(sources, 'OWNER_ED25519_SPKI_SHA256', None):
            with self.assertRaises(OpsError) as caught:
                self.build()
        self.assertEqual(caught.exception.code, 'BLOCKED_KEY_ACCESS')

    def test_empty_reviewed_image_policy_rejects_well_formed_digests(self):
        with patch.object(contract, 'APPROVED_IMAGE_SETS', ()):
            with self.assertRaises(OpsError):
                self.build()

    def test_synthetic_spec_is_inert_and_allocates_only_fresh_resources(self):
        spec = self.build()
        self.assertEqual(self.validate_spec(spec), spec)
        for flag in ('execution_allowed', 'ready_to_rehearse', 'ready_to_apply'):
            self.assertIs(spec[flag], False)
        self.assertEqual(set(spec['containers']), {'postgres', 'minio'})
        self.assertEqual(set(spec['volumes']), {'postgres_data', 'postgres_wal', 'minio_data'})
        self.assertIs(spec['network']['internal'], True)
        self.assertIs(spec['network']['attachable'], False)
        volumes = {value['name'] for value in spec['volumes'].values()}
        self.assertEqual(len(volumes), 3)
        self.assertFalse(volumes & set(self.context.inventory.volumes))
        self.assertNotIn(spec['network']['name'], self.context.inventory.networks)
        for service in spec['containers'].values():
            self.assertEqual(service['ports'], [])
            self.assertEqual(service['networks'], [spec['network']['name']])
            self.assertIs(service['privileged'], False)
            self.assertEqual(service['environment'], {})
            self.assertTrue(all(mount['type'] == 'volume' and mount['source'] in volumes
                                for mount in service['mounts']))

    def test_pure_calls_are_deterministic_and_do_not_mutate_trusted_inputs(self):
        plan_before, context_before = deepcopy(self.plan), asdict(self.context)
        first = self.build()
        second = self.build()
        self.assertEqual(first, second)
        first['containers']['postgres']['ports'].append('5432:5432')
        self.assertEqual(self.build(), second)
        self.assertEqual(self.plan, plan_before)
        self.assertEqual(asdict(self.context), context_before)
        with self.assertRaises(FrozenInstanceError):
            self.context.operation_id = '9' * 32

    def test_validation_returns_detached_snapshot_for_future_consumers(self):
        spec = self.build()
        expected = deepcopy(spec)
        validated = self.validate_spec(spec)
        self.assertEqual(validated, expected)
        self.assertIsNot(validated, spec)
        self.assertIsNot(validated['containers'], spec['containers'])
        self.assertIsNot(validated['limits'], self.plan['limits'])
        spec['containers']['postgres']['privileged'] = True
        spec['containers']['postgres']['mounts'][0]['source'] = 'synthetic-live-pgdata'
        spec['binding']['snapshot_id'] = sources.LEGACY_ID
        self.plan['images']['postgres'] = 'postgres:latest'
        self.plan['limits']['postgres_memory_bytes'] = 64 * GIB
        self.assertEqual(validated, expected)

    def test_plan_mutation_after_input_check_cannot_change_built_spec(self):
        expected = self.build()
        original_check = contract._check_inputs

        def mutate_caller_after_check(*args, **kwargs):
            result = original_check(*args, **kwargs)
            self.plan['images']['postgres'] = 'postgres:latest'
            self.plan['limits']['postgres_memory_bytes'] = 64 * GIB
            self.plan['source_sha256'] = '0' * 64
            self.plan['operation_id'] = '9' * 32
            self.plan['owner_receipt_sha256'] = '0' * 64
            return result

        with patch.object(contract, '_check_inputs',
                          side_effect=mutate_caller_after_check) as checked:
            actual = self.build()
        checked.assert_called_once()
        self.assertEqual(actual, expected)

    def test_create_only_rejects_every_preexisting_named_object(self):
        spec = self.build()
        cases = [('networks', spec['network']['name'])]
        cases += [('volumes', value['name']) for value in spec['volumes'].values()]
        cases += [('containers', value['name']) for value in spec['containers'].values()]
        for field, name in cases:
            with self.subTest(field=field, name=name):
                inventory = replace(self.context.inventory,
                                    **{field: getattr(self.context.inventory, field) + (name,)})
                with self.assertRaises(OpsError):
                    self.build(context=replace(self.context, inventory=inventory))

    def test_public_spec_cannot_select_live_or_shared_volumes(self):
        for role in ('postgres_data', 'postgres_wal', 'minio_data'):
            with self.subTest(role=role):
                spec = self.build()
                allocated = spec['volumes'][role]['name']
                spec['volumes'][role]['name'] = self.context.inventory.volumes[0]
                for service in spec['containers'].values():
                    for mount in service['mounts']:
                        if mount['source'] == allocated:
                            mount['source'] = self.context.inventory.volumes[0]
                self.rejected_spec(spec)
        spec = self.build()
        shared = spec['volumes']['postgres_data']['name']
        spec['volumes']['minio_data']['name'] = shared
        spec['containers']['minio']['mounts'][0]['source'] = shared
        self.rejected_spec(spec)

    def test_live_postgres_host_dsn_and_credentials_are_rejected_without_echo(self):
        sentinel = 'synthetic-credential-never-a-real-secret'
        injections = {
            'host': 'synthetic-live-postgres',
            'dsn': 'postgresql://synthetic-live-postgres/working_db',
            'environment': {'POSTGRES_PASSWORD': sentinel},
            'credentials': {'password': sentinel},
        }
        for key, value in injections.items():
            with self.subTest(key=key):
                spec = self.build()
                target = (spec['postgres_target'] if key in {'host', 'dsn'}
                          else spec['containers']['postgres'])
                target[key] = value
                spec['contract_sha256'] = digest({name: item for name, item in spec.items()
                                                  if name != 'contract_sha256'})
                with self.assertRaises(OpsError) as caught:
                    self.validate_spec(spec)
                self.assertNotIn(sentinel, str(caught.exception))
        spec = self.build()
        spec['postgres_target']['fallback'] = injections['dsn']
        self.rejected_spec(spec)
        spec = self.build()
        spec['containers']['minio']['environment'] = {'MINIO_ROOT_PASSWORD': sentinel}
        self.rejected_spec(spec)
        plan, context = self.trusted_plan_variant(postgres_dsn=injections['dsn'])
        with self.assertRaises(OpsError):
            self.build(plan, context)

    def test_live_network_or_public_network_flags_are_rejected(self):
        for mode in ('live_attachment', 'external_network', 'not_internal', 'attachable'):
            with self.subTest(mode=mode):
                spec = self.build()
                if mode == 'live_attachment':
                    spec['containers']['postgres']['networks'].append(
                        self.context.inventory.networks[0])
                elif mode == 'external_network':
                    spec['network']['name'] = self.context.inventory.networks[0]
                else:
                    spec['network']['internal' if mode == 'not_internal' else 'attachable'] = (
                        mode == 'attachable')
                self.rejected_spec(spec)

    def test_privileged_host_namespaces_devices_socket_and_bind_are_rejected(self):
        mutations = {
            'ports': ['127.0.0.1:5432:5432'], 'privileged': True,
            'network_mode': 'host', 'pid_mode': 'host', 'ipc_mode': 'host',
            'devices': ['/dev/synthetic-device'],
            'binds': ['/synthetic/live/data:/var/lib/postgresql/data'],
            'mounts': [{'type': 'bind', 'source': '/var/run/docker.sock',
                        'target': '/var/run/docker.sock'}],
            'command': ['sh', '-c', 'synthetic-command-must-not-execute'],
            'entrypoint': '/bin/sh', 'extra_hosts': ['live:host-gateway'],
            'dns': ['192.0.2.1'], 'docker_args': ['--privileged'],
            'url': 'https://example.invalid/synthetic-backup',
        }
        for field, value in mutations.items():
            with self.subTest(field=field):
                spec = self.build()
                spec['containers']['postgres'][field] = value
                self.rejected_spec(spec)

    def test_unknown_fields_are_rejected_at_every_public_level(self):
        for level in ('root', 'network', 'volume', 'container', 'credentials'):
            with self.subTest(level=level):
                spec = self.build()
                targets = {'root': spec, 'network': spec['network'],
                           'volume': spec['volumes']['postgres_data'],
                           'container': spec['containers']['postgres'],
                           'credentials': spec['containers']['postgres']['credentials']}
                targets[level]['client_override'] = {'verified': True}
                self.rejected_spec(spec)

    def test_rehashed_public_plan_does_not_replace_private_plan_authority(self):
        changed = deepcopy(self.plan)
        changed['limits']['postgres_memory_bytes'] += MIB
        rehash_plan(changed)
        self.assertNotEqual(changed['plan_hash'], self.context.plan_hash)
        with self.assertRaises(OpsError):
            self.build(changed)
        changed = deepcopy(self.plan)
        changed['plan_id'] = '8' * 32
        with self.assertRaises(OpsError):
            self.build(changed)

    def test_source_and_receipt_substitution_rejects_even_a_rehashed_plan(self):
        for field, value in (
                ('snapshot_id', sources.LEGACY_ID), ('snapshot_id', '../source'),
                ('source_sha256', '0' * 64), ('manifest_sha256', '0' * 64),
                ('owner_receipt_sha256', '0' * 64),
                ('staging_receipt_sha256', '0' * 64)):
            with self.subTest(field=field, value=value):
                plan, context = self.trusted_plan_variant(**{field: value})
                with self.assertRaises(OpsError):
                    self.build(plan, context)

    def test_proof_is_mandatory_and_public_verified_flags_are_not_authority(self):
        for label, source in (
                ('missing', None), ('unsigned_flags', {'verified': True, 'signed': True}),
                ('matching_public_hashes', asdict(self.context.source))):
            with self.subTest(proof=label):
                with self.assertRaises(OpsError):
                    self.build(context=replace(self.context, source=source))
        with self.assertRaises(OpsError):
            self.build(context=asdict(self.context))

    def test_unknown_manifest_missing_receipts_or_wrong_owner_key_fail_closed(self):
        for field, value in (
                ('manifest_sha256', None), ('owner_receipt_sha256', None),
                ('staging_receipt_sha256', None), ('owner_spki_sha256', '0' * 64),
                ('ciphertext_sha256', '0' * 64), ('snapshot_id', sources.LEGACY_ID)):
            with self.subTest(field=field):
                proof = replace(self.context.source, **{field: value})
                with self.assertRaises(OpsError):
                    self.build(context=replace(self.context, source=proof))

    def test_stale_or_future_proof_and_inventory_are_rejected(self):
        for offset in (-61, 1):
            for field, clock in (('source', 'verified_at'), ('inventory', 'observed_at')):
                with self.subTest(field=field, offset=offset):
                    altered = replace(getattr(self.context, field), **{clock: NOW + offset})
                    with self.assertRaises(OpsError):
                        self.build(context=replace(self.context, **{field: altered}))

    def test_expired_future_and_excessively_long_plans_are_rejected(self):
        for created, expires in ((NOW - 600, NOW), (NOW + 1, NOW + 600),
                                 (NOW, NOW + 901), (NOW, NOW)):
            with self.subTest(created=created, expires=expires):
                plan, context = self.trusted_plan_variant(
                    created_at=timestamp(created), expires_at=timestamp(expires))
                with self.assertRaises(OpsError):
                    self.build(plan, context)
        plan, context = self.trusted_plan_variant(created_at='2026-10-28T00:00:00')
        with self.assertRaises(OpsError):
            self.build(plan, context)

    def test_changed_installation_generation_and_operation_are_rejected(self):
        for field, value in (('installation_instance_id', '9' * 32),
                             ('installation_id', 'other-installation'),
                             ('operation_id', '9' * 32)):
            with self.subTest(field=field):
                with self.assertRaises(OpsError):
                    self.build(context=replace(self.context, **{field: value}))

    def test_images_must_match_reviewed_manifest_policy_not_only_digest_shape(self):
        for ref in ('postgres:latest', 'docker.io/library/postgres@sha256:' + '0' * 64,
                    'https://example.invalid/image@sha256:' + 'c' * 64,
                    'other.invalid/evil@sha256:' + 'c' * 64):
            with self.subTest(ref=ref):
                plan, context = self.trusted_plan_variant(
                    images={'postgres': ref, 'minio': MINIO_IMAGE})
                with self.assertRaises(OpsError):
                    self.build(plan, context)
        with patch.object(contract, 'APPROVED_IMAGE_SETS',
                          (('0' * 64, POSTGRES_IMAGE, MINIO_IMAGE),)):
            with self.assertRaises(OpsError):
                self.build()

    def test_insufficient_capacity_and_unknown_hard_quota_support_block(self):
        for field, value in (('memory_bytes', 256 * MIB), ('disk_bytes', GIB),
                             ('cpu_millis', 1000), ('hard_volume_quotas', False),
                             ('hard_volume_quotas', None)):
            with self.subTest(field=field, value=value):
                capacity = replace(self.context.capacity, **{field: value})
                with self.assertRaises(OpsError):
                    self.build(context=replace(self.context, capacity=capacity))

    def test_resource_limits_reject_type_confusion_zero_and_overflow(self):
        for field, bad in (
                ('postgres_memory_bytes', True), ('postgres_memory_bytes', 0),
                ('postgres_memory_bytes', 16 * GIB + 1),
                ('minio_memory_bytes', float('nan')), ('postgres_data_bytes', -1),
                ('postgres_wal_bytes', 1), ('minio_data_bytes', 2 ** 65),
                ('postgres_cpu_millis', False), ('minio_cpu_millis', 4001),
                ('postgres_tmpfs_bytes', 0), ('minio_tmpfs_bytes', '65536'),
                ('log_bytes', 65 * MIB), ('max_seconds', 901)):
            with self.subTest(field=field, value=bad):
                plan = deepcopy(self.plan)
                plan['limits'][field] = bad
                rehash_plan(plan)
                context = replace(self.context, plan_hash=plan['plan_hash'])
                with self.assertRaises(OpsError):
                    self.build(plan, context)

    def test_public_spec_cannot_enable_execution_or_rehearsal_by_rehashing(self):
        for flag in ('execution_allowed', 'ready_to_rehearse', 'ready_to_apply'):
            with self.subTest(flag=flag):
                spec = self.build()
                spec[flag] = True
                self.rejected_spec(spec)
        for flag in ('ready_to_rehearse', 'ready_to_apply'):
            plan, context = self.trusted_plan_variant(**{flag: True})
            with self.subTest(plan_flag=flag), self.assertRaises(OpsError) as caught:
                self.build(plan, context)
            self.assertEqual(caught.exception.code, 'BLOCKED_PLAN')

    def test_boolean_security_flags_cannot_be_replaced_with_integer_zero(self):
        for flag in ('privileged', 'execution_allowed'):
            spec = self.build()
            (spec if flag == 'execution_allowed' else spec['containers']['postgres'])[flag] = 0
            with self.subTest(flag=flag):
                self.rejected_spec(spec)

    def test_cleanup_matching_is_inert_and_requires_exact_created_resource(self):
        spec = self.build()
        resources = [('volume', role) for role in spec['volumes']]
        resources += [('container', role) for role in spec['containers']]
        resources.append(('network', None))
        for kind, role in resources:
            with self.subTest(kind=kind, role=role):
                created, observed = self.cleanup_fixture(spec, kind, role)
                target = self.cleanup(spec, created, observed)
                self.assertEqual(target['name'], created.name)
                self.assertIs(target['delete_authorized'], False)

    def test_cleanup_uses_validated_snapshots_after_caller_mutation(self):
        spec = self.build()
        created, observed = self.cleanup_fixture(spec)
        original_validate = contract._validated

        def mutate_caller_after_validation(*args, **kwargs):
            result = original_validate(*args, **kwargs)
            spec['volumes']['postgres_data']['name'] = 'synthetic-live-pgdata'
            spec['volumes']['postgres_data']['labels'].clear()
            spec['contract_sha256'] = '0' * 64
            self.plan['created_at'] = timestamp(NOW + 1)
            self.plan['plan_hash'] = '0' * 64
            return result

        with patch.object(contract, '_validated',
                          side_effect=mutate_caller_after_validation) as checked:
            target = self.cleanup(spec, created, observed)
        checked.assert_called_once()
        self.assertEqual(target['name'], created.name)
        self.assertEqual(target['native_id'], created.native_id)
        self.assertIs(target['delete_authorized'], False)

    def test_cleanup_lifecycle_uses_current_identity_not_creation_admission(self):
        spec = self.build()
        later = NOW + 61
        current = replace(self.cleanup_context(spec, later),
                          capacity=contract.Capacity(0, 0, 0, False))
        resources = [('volume', role) for role in spec['volumes']]
        resources += [('container', role) for role in spec['containers']]
        resources.append(('network', None))
        for kind, role in resources:
            with self.subTest(kind=kind, role=role):
                created, observed = self.cleanup_fixture(spec, kind, role)
                observed = replace(observed, observed_at=later)
                target = self.cleanup(spec, created, observed, now=later, context=current)
                self.assertEqual(target['name'], created.name)
                self.assertIs(target['delete_authorized'], False)
                field = {'volume': 'volumes', 'container': 'containers', 'network': 'networks'}[kind]
                inventory = replace(current.inventory, **{
                    field: tuple(name for name in getattr(current.inventory, field)
                                 if name != created.name)})
                with self.assertRaises(OpsError):
                    self.cleanup(spec, created, observed, now=later,
                                 context=replace(current, inventory=inventory))
        with self.assertRaises(OpsError):
            self.build(context=current, now=later)
        created, observed = self.cleanup_fixture(spec)
        observed = replace(observed, observed_at=later)
        stale_inventory = replace(current.inventory, observed_at=NOW)
        with self.assertRaises(OpsError):
            self.cleanup(spec, created, observed, now=later,
                         context=replace(current, inventory=stale_inventory))
        with self.assertRaises(OpsError):
            self.cleanup(spec, created, observed, now=later,
                         context=replace(current, operation_id='9' * 32))
        expired = NOW + 600
        with self.assertRaises(OpsError):
            self.cleanup(spec, created, replace(observed, observed_at=expired),
                         now=expired, context=self.cleanup_context(spec, expired))

    def test_cleanup_labels_alone_cannot_claim_a_foreign_resource(self):
        spec = self.build()
        created, observed = self.cleanup_fixture(spec)
        for field, value in (('native_id', '8' * 64), ('engine_id', '8' * 64),
                             ('name', 'synthetic-live-pgdata'), ('kind', 'container'),
                             ('in_use', True)):
            with self.subTest(field=field), self.assertRaises(OpsError):
                self.cleanup(spec, created, replace(observed, **{field: value}))
        with self.assertRaises(OpsError):
            self.cleanup(spec, replace(created, contract_sha256='8' * 64), observed)
        with self.assertRaises(OpsError):
            self.cleanup(spec, asdict(created), observed)

    def test_cleanup_rejects_relabeling_extra_labels_and_public_spec_overrides(self):
        spec = self.build()
        created, observed = self.cleanup_fixture(spec)
        key, value = observed.labels[0]
        for labels in ((), ((key, 'forged'),) + observed.labels[1:],
                       observed.labels + (('foreign.owner', 'another-operation'),),
                       observed.labels + ((key, value),)):
            with self.subTest(labels=labels), self.assertRaises(OpsError):
                self.cleanup(spec, created, replace(observed, labels=labels))
        spec['volumes']['postgres_data']['labels'][key] = 'forged'
        self.rejected_spec(spec)

    def test_cleanup_rejects_unknown_receipts_and_out_of_window_creation(self):
        spec = self.build()
        created, observed = self.cleanup_fixture(spec)
        for value in (NOW - 1, NOW + 1):
            with self.subTest(created_at=value), self.assertRaises(OpsError):
                self.cleanup(spec, replace(created, created_at=value), observed)
        with self.assertRaises(OpsError):
            self.cleanup(spec, None, observed)

    def test_cleanup_rejects_stale_or_future_observation(self):
        spec = self.build()
        created, observed = self.cleanup_fixture(spec)
        for value in (NOW - 6, NOW + 1, True):
            with self.subTest(observed_at=value), self.assertRaises(OpsError):
                self.cleanup(spec, created, replace(observed, observed_at=value))
        with self.assertRaises(OpsError):
            self.cleanup(spec, replace(created, created_at=NOW + 1), observed, now=NOW + 1)

    def test_contract_does_not_expose_apply_or_bypass_existing_new_source_deny(self):
        self.assertNotIn('asa_restore_apply', INPUTS)
        validate(INPUTS['asa_restore_plan'], {
            'installation_id': 'asa-test', 'snapshot_id': sources.LEGACY_ID})
        forged = {'kind': 'restore', 'snapshot_id': sources.NEW_ID,
                  'ready_to_rehearse': True, 'isolation_contract': self.build()}
        with self.no_side_effects(), self.assertRaises(OpsError) as caught:
            restore.rehearse({}, {'plan': forged})
        self.assertEqual(caught.exception.code, 'BLOCKED_PLAN')


if __name__ == '__main__':
    unittest.main()
