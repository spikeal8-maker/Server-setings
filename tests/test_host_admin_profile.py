"""Host-only authority boundaries, using actual private files and Unix peers.

No live school machine, Docker daemon, network service or ASA installation is
used. Root fixtures live under a temporary root-owned /var/lib directory; the
only location override preserves validate_service_state's real owner/mode and
name checks. The fork test drops the client to uid65534 and retains a root
executor in the parent, so a chmod/UID bypass would not make it pass.
"""
import copy
import json
import os
from pathlib import Path
import select
import shlex
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from asa_ops import admin, admin_executor, adapter, host_profile, maintenance, schemas, server, worker
from asa_ops.store import OpsError, Store


INSTALLATION = 'asa-school-vm100'
INSTANCE = 'a' * 32
SERVICE_UID = 65534
HOST_TOOLS = {'asa_host_inventory', 'asa_admin_status', 'asa_admin_execute',
              'asa_operation_get', 'asa_operation_list'}
OFFICIAL_TOOLS = {'create_database', 'create_ssl', 'create_website', 'get_dashboard_info',
                  'get_system_info', 'install_mysql', 'install_openresty', 'list_databases',
                  'list_installed_apps', 'list_ssls', 'list_websites'}
LIFECYCLE_TOOLS = {'asa_restore_plan', 'asa_restore_rehearse', 'asa_update_plan',
                   'asa_update_start', 'asa_deployment_verify', 'asa_operation_finalize'}
LEGACY_DEFAULT = (HOST_TOOLS - {'asa_admin_status', 'asa_admin_execute'}) | LIFECYCLE_TOOLS
FORBIDDEN_TOOLS = LIFECYCLE_TOOLS | {'asa_cloud_transfer'}
ROOT_LINUX = sys.platform.startswith('linux') and os.geteuid() == 0 and Path('/var/lib').is_dir()


def service_identity_available():
    """Some root containers map only uid0 and have no chown/setuid capability."""
    if not ROOT_LINUX:
        return False
    try:
        for filename in ('uid_map', 'gid_map'):
            ranges = [tuple(map(int, line.split())) for line in Path('/proc/self', filename).read_text().splitlines()]
            if not any(start <= SERVICE_UID < start + count for start, _, count in ranges):
                return False
        capabilities = next(line.split()[1] for line in Path('/proc/self/status').read_text().splitlines()
                            if line.startswith('CapEff:'))
        required = (1 << 0) | (1 << 6) | (1 << 7)  # CHOWN, SETGID, SETUID
        return int(capabilities, 16) & required == required
    except (OSError, ValueError, StopIteration):
        return False


SERVICE_IDENTITY_AVAILABLE = service_identity_available()
SERVICE_SKIP = 'UID65534/GID65534 must be mapped and CHOWN/SETUID/SETGID capabilities available'


class FakeOfficial:
    def request(self, method, params):
        if method != 'tools/list':
            raise AssertionError('Official mutations are not part of this test')
        return {'tools': [{'name': name, 'description': 'Official tool',
                           'inputSchema': {'type': 'object'}} for name in sorted(OFFICIAL_TOOLS)]}


def bare_controller():
    controller = server.Controller.__new__(server.Controller)
    controller.config = {'operations_mode': 'host-admin-only', 'host_admin_enabled': True,
                         'cloud_storage_enabled': False, 'hostname': socket.gethostname(),
                         'installation': {'id': INSTALLATION}}
    controller.store = Mock()
    controller.lock = threading.Lock()
    controller.official = FakeOfficial()
    return controller


class ProfileContracts(unittest.TestCase):
    def test_exact_host_tools_and_legacy_defaults(self):
        self.assertEqual({item['name'] for item in schemas.tools()}, LEGACY_DEFAULT)
        self.assertEqual({item['name'] for item in schemas.tools(host_admin_enabled=True)},
                         LEGACY_DEFAULT | {'asa_admin_execute', 'asa_admin_status'})
        self.assertEqual({item['name'] for item in schemas.tools(host_admin_enabled=True, cloud_storage_enabled=True)},
                         LEGACY_DEFAULT | {'asa_admin_execute', 'asa_admin_status', 'asa_cloud_transfer'})
        host = schemas.tools(INSTALLATION, True, False, 'host-admin-only')
        self.assertEqual(len(host), 5)
        self.assertEqual({item['name'] for item in host}, HOST_TOOLS)
        for item in host:
            fields = item['inputSchema']['properties']
            if 'installation_id' in fields:
                self.assertEqual(fields['installation_id']['enum'], [INSTALLATION])
        self.assertEqual(schemas.INPUTS['asa_admin_execute']['properties']['installation_id']['enum'], ['asa-test'])
        self.assertEqual(host_profile.mode({}), 'lifecycle')
        self.assertEqual(host_profile.service_uid({'installation': {'owner_uid': 1000}}), 1000)
        with self.assertRaises(OpsError):
            schemas.tools(operations_mode='silently-root')

    def test_rpc_lists_exact_eleven_official_plus_five_and_identifies_school(self):
        controller = bare_controller()
        result = controller.rpc({'method': 'tools/list'})
        self.assertEqual(len(result['tools']), 16)
        self.assertEqual({item['name'] for item in result['tools']}, OFFICIAL_TOOLS | HOST_TOOLS)
        initialized = controller.rpc({'method': 'initialize'})
        self.assertEqual(initialized['serverInfo']['name'], 'asa-school-host-admin')
        self.assertIn(INSTALLATION, initialized['instructions'])
        self.assertIn(socket.gethostname(), initialized['instructions'])
        del controller.config['operations_mode']
        self.assertEqual(controller.rpc({'method': 'initialize'})['serverInfo']['name'], 'asa-1panel-operations')

    def test_direct_and_rpc_denials_precede_schema_reconcile_adapter_and_generation(self):
        controller = bare_controller()
        with patch.object(server, 'adapter', side_effect=AssertionError('adapter called')) as bridge, \
                patch.object(server, 'check_generation', side_effect=AssertionError('checkout read')) as generation, \
                patch.object(controller, 'launch', side_effect=AssertionError('worker launched')) as launch, \
                patch.object(controller.official, 'request', side_effect=AssertionError('official fallback')) as official:
            for name in sorted(FORBIDDEN_TOOLS):
                # Empty arguments would fail schema validation if capability
                # denial happened too late; forbidden calls must reach no store.
                with self.subTest(name=name), self.assertRaises(OpsError) as caught:
                    controller.dispatch(name, {})
                self.assertEqual(caught.exception.code, 'CAPABILITY_DISABLED')
                result = controller.rpc({'method': 'tools/call', 'params': {'name': name, 'arguments': {}}})
                self.assertTrue(result['isError'])
                self.assertEqual(result['structuredContent']['error']['code'], 'CAPABILITY_DISABLED')
            controller.store.assert_not_called()
            self.assertEqual(controller.store.mock_calls, [])
            for mock in (bridge, generation, launch, official):
                mock.assert_not_called()

    def test_inventory_uses_root_request_without_granting_docker_to_service(self):
        config = bare_controller().config
        expected = {'hostname': socket.gethostname(), 'executor_uid': 0, 'containers': []}
        with patch.object(admin, 'request', return_value=expected) as request, \
                patch.object(admin.subprocess, 'run', side_effect=AssertionError('unprivileged Docker call')) as docker:
            self.assertEqual(admin.inventory(config), expected)
        request.assert_called_once_with(config, {'action': 'inventory'})
        docker.assert_not_called()

    def test_executor_identity_requires_every_pinned_identity_field(self):
        config = bare_controller().config
        config['installation']['installation_instance_id'] = INSTANCE
        identity = {'uid': 0, 'hostname': config['hostname'], 'installation_id': INSTALLATION,
                    'installation_instance_id': INSTANCE}
        with patch.object(admin, 'request', return_value=identity):
            self.assertEqual(host_profile.executor_identity(config), INSTANCE)
        for field, bad in [('uid', 1000), ('hostname', 'foreign-host'),
                           ('installation_id', 'asa-test'), ('installation_instance_id', 'b' * 32)]:
            with self.subTest(field=field), patch.object(admin, 'request', return_value=dict(identity, **{field: bad})), \
                    self.assertRaises(OpsError) as caught:
                host_profile.executor_identity(config)
            self.assertEqual(caught.exception.code, 'HOST_EXECUTOR_IDENTITY')

    def test_legacy_adapter_still_requires_installation_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            owner = Path(directory).stat().st_uid
            with patch.object(adapter.os, 'getuid', return_value=owner + 1), \
                    patch.object(adapter.importlib, 'import_module', side_effect=AssertionError('manager imported')) as manager, \
                    self.assertRaises(OpsError) as caught:
                adapter.load({'root': directory, 'owner_uid': owner, 'profile': 'school', 'project': 'asa-lab-school'})
            self.assertEqual(caught.exception.code, 'OWNER')
            manager.assert_not_called()

    def test_lost_worker_reconciles_from_root_audit_and_reuses_key_without_checkout(self):
        """Controller/store semantics only; __new__ does not prove the UID boundary.

        get/list discover the lost worker, and the explicit status tool imports
        the completed root audit. Reads and an exact-key retry must never invoke
        the application or execute that root command a second time.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = root / 'private-application'
            app.mkdir(mode=0o700)
            controller = bare_controller()
            controller.config['installation'].update(root=str(app), state_root=str(root / 'state'),
                                                     installation_instance_id=INSTANCE)
            controller.store = Store(root / 'state')
            args = {'installation_id': INSTALLATION, 'command': 'read-only fixture command', 'cwd': '/',
                    'timeout_seconds': 5, 'idempotency_key': 'host-only-recovery-fixture-0001'}
            plan = controller.store.plan(INSTALLATION, {'kind': 'admin', 'installation_instance_id': INSTANCE,
                                                       'command': args['command'], 'cwd': '/', 'timeout_seconds': 5})
            record, created = controller.store.start(INSTALLATION, 'admin', plan['plan_id'], args['idempotency_key'],
                                                      {'method': 'asa_admin_execute', **args})
            self.assertTrue(created)
            opid = record['operation_id']
            controller.store.event(opid, 'admin', 'Worker started before its response was lost')
            with controller.store.db() as database:
                database.execute('UPDATE operations SET status=?,updated=?,pid=?,process_identity=? WHERE id=?',
                                 ('running', '2020-01-01T00:00:00+00:00', 99999999, 'lost-worker-identity', opid))
            audit = {'status': 'complete', 'operation_id': opid, 'exit_code': 0, 'timed_out': False,
                     'output': '{"uid":0}', 'output_may_be_truncated': False}
            original_open, original_exists = Path.open, Path.exists
            def guarded_open(path, *parameters, **kwargs):
                if path == app or app in path.parents:
                    raise AssertionError('Recovery tried reading the private application')
                return original_open(path, *parameters, **kwargs)
            def guarded_exists(path):
                if path == app or app in path.parents:
                    raise AssertionError('Recovery tried probing private application receipts')
                return original_exists(path)
            with patch.object(Path, 'open', new=guarded_open), patch.object(Path, 'exists', new=guarded_exists), \
                    patch.object(maintenance, 'instance', side_effect=AssertionError('Private instance read')) as instance, \
                    patch.object(maintenance, 'check_generation', side_effect=AssertionError('Private generation read')) as generation, \
                    patch.object(server, 'check_generation', side_effect=AssertionError('Private generation read')) as imported_generation, \
                    patch.object(server, 'adapter', side_effect=AssertionError('Lifecycle adapter called')) as bridge, \
                    patch.object(controller, 'launch', side_effect=AssertionError('Root command replayed')) as launch, \
                    patch.object(admin, 'request', return_value=audit) as request:
                lost = controller.dispatch('asa_operation_get', {'operation_id': opid, 'log_cursor': 0})
                self.assertEqual(lost['status'], 'recovery_required')
                self.assertEqual(lost['phase'], 'worker_lost')
                self.assertTrue(lost['log']['lines'])
                listed = controller.dispatch('asa_operation_list', {'installation_id': INSTALLATION})['operations']
                self.assertEqual([(item['operation_id'], item['status']) for item in listed], [(opid, 'recovery_required')])
                request.assert_not_called()
                self.assertEqual(controller.dispatch('asa_admin_status', {'installation_id': INSTALLATION,
                                                                        'operation_id': opid}), audit)
                recovered = controller.dispatch('asa_operation_get', {'operation_id': opid, 'log_cursor': 0})
                self.assertEqual(recovered['status'], 'success')
                self.assertEqual(recovered['phase'], 'root_audit_reconciled')
                self.assertEqual(recovered['result'], audit)
                listed = controller.dispatch('asa_operation_list', {'installation_id': INSTALLATION})['operations']
                self.assertEqual([(item['operation_id'], item['status']) for item in listed], [(opid, 'success')])
                reused = controller.dispatch('asa_admin_execute', args)
                self.assertTrue(reused['reused'])
                self.assertEqual(reused['operation']['operation_id'], opid)
                self.assertEqual(reused['operation']['status'], 'success')
                request.assert_called_once_with(controller.config, {'action': 'status', 'operation_id': opid})
                for mock in (instance, generation, imported_generation, bridge, launch):
                    mock.assert_not_called()
            # The successful import persists independently of the Controller.
            self.assertEqual(Store(root / 'state').get(opid)['phase'], 'root_audit_reconciled')


@unittest.skipUnless(ROOT_LINUX, 'Linux root is required for actual trusted ownership fixtures')
class RootFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='asa-host-profile-', dir='/var/lib')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.base.chmod(0o755)
        self.app = self.base / 'application'
        self.app.mkdir(mode=0o700)
        (self.app / '.asa').mkdir(mode=0o700)
        self.identity_path = self.app / '.asa/installation.json'
        self.identity = {'instanceId': INSTANCE, 'profile': 'school', 'project': 'asa-lab-school'}
        self.identity_path.write_text(json.dumps(self.identity))
        self.identity_path.chmod(0o600)
        self.state_parent = self.base / 'server-settings'
        self.state_parent.mkdir(mode=0o755)
        self.state = self.state_parent / 'asa-operations-school'
        if SERVICE_IDENTITY_AVAILABLE:
            self.state.mkdir(mode=0o700)
            os.chown(self.state, SERVICE_UID, SERVICE_UID)
        self.audit = self.base / 'root-audit'
        self.audit.mkdir(mode=0o700)
        self.config_path = self.base / 'operations.json'
        self.config = {'operations_mode': 'host-admin-only', 'host_admin_enabled': True,
                       'cloud_storage_enabled': False, 'hostname': socket.gethostname(),
                       'service_uid': SERVICE_UID, 'official_command': [],
                       'installation': {'id': INSTALLATION, 'owner_uid': 0, 'root': str(self.app),
                                        'state_root': str(self.state), 'installation_instance_id': INSTANCE}}
        self.write_config()
        self.executor = {'enabled': True, 'operations_mode': 'host-admin-only', 'owner_uid': SERVICE_UID,
                         'hostname': socket.gethostname(), 'root': str(self.app),
                         'installation_id': INSTALLATION, 'installation_instance_id': INSTANCE,
                         'installation_profile': 'school', 'installation_project': 'asa-lab-school'}
        actual_validator = maintenance.validate_service_state
        state_guard = patch.object(host_profile, 'validate_service_state',
                                   side_effect=lambda path, uid: actual_validator(path, uid, parent=self.state_parent))
        state_guard.start()
        self.addCleanup(state_guard.stop)

    def write_config(self):
        self.config_path.write_text(json.dumps(self.config))
        self.config_path.chmod(0o644)

    def payload(self):
        return {'action': 'execute', 'installation_id': INSTALLATION, 'installation_instance_id': INSTANCE}


class TrustAndExecutorTests(RootFixture):
    def test_trusted_root_application_and_explicit_service_uid_config_are_accepted(self):
        host_profile.validate(self.config, self.config_path)
        with patch.object(host_profile.os, 'geteuid', return_value=SERVICE_UID):
            host_profile.validate(self.config, self.config_path, runtime=True)
        with self.assertRaises(OpsError) as caught:
            host_profile.validate(self.config, self.config_path, runtime=True)
        self.assertEqual(caught.exception.code, 'HOST_SERVICE_IDENTITY')
        admin_executor.authorize(self.executor, self.payload(), SERVICE_UID)
        self.assertEqual(stat.S_IMODE(self.app.stat().st_mode), 0o700)
        self.assertEqual(self.app.stat().st_uid, 0)

    def test_profile_service_and_owner_mismatches_are_rejected(self):
        changes = [({'operations_mode': 'implicit-root'}, 'OPERATIONS_MODE'),
                   ({'host_admin_enabled': False}, 'HOST_ADMIN_NOT_ENABLED'),
                   ({'cloud_storage_enabled': True}, 'HOST_CLOUD_FORBIDDEN'),
                   ({'hostname': 'foreign-host'}, 'HOST_IDENTITY')]
        changes += [({'service_uid': uid}, 'HOST_SERVICE_UID') for uid in (0, -1, True, '65534')]
        for changeset, code in changes:
            with self.subTest(changes=changeset), self.assertRaises(OpsError) as caught:
                host_profile.validate(dict(self.config, **changeset), self.config_path)
            self.assertEqual(caught.exception.code, code)
        for field, value, code in [('owner_uid', SERVICE_UID, 'HOST_INSTALLATION_OWNER'),
                                   ('id', '../foreign', 'HOST_INSTALLATION_ID'),
                                   ('installation_instance_id', 'not-an-instance', 'HOST_INSTANCE_ID')]:
            config = copy.deepcopy(self.config)
            config['installation'][field] = value
            with self.subTest(field=field), self.assertRaises(OpsError) as caught:
                host_profile.validate(config, self.config_path)
            self.assertEqual(caught.exception.code, code)

    def test_root_paths_and_state_permissions_are_not_relaxed(self):
        host_profile.validate(self.config, self.config_path)
        cases = [(self.app, 0o770, 'HOST_PATH_TRUST'), (self.config_path, 0o666, 'HOST_PATH_TRUST'),
                 (self.base, 0o777, 'HOST_PARENT_TRUST')]
        if SERVICE_IDENTITY_AVAILABLE:
            cases.append((self.state, 0o750, 'STATE_OWNER_OR_MODE'))
        for path, mode, code in cases:
            original = stat.S_IMODE(path.stat().st_mode)
            try:
                path.chmod(mode)
                with self.subTest(path=path.name), self.assertRaises(OpsError) as caught:
                    host_profile.validate(self.config, self.config_path)
                self.assertEqual(caught.exception.code, code)
            finally:
                path.chmod(original)
        link = self.base / 'application-link'
        link.symlink_to(self.app, target_is_directory=True)
        config = copy.deepcopy(self.config)
        config['installation']['root'] = str(link)
        with self.assertRaises(OpsError) as caught:
            host_profile.validate(config, self.config_path)
        self.assertEqual(caught.exception.code, 'HOST_PATH_SYMLINK')
        config = copy.deepcopy(self.config)
        config['installation']['state_root'] = str(self.state_parent / 'arbitrary-state')
        with self.assertRaises(OpsError) as caught:
            host_profile.validate(config, self.config_path)
        self.assertEqual(caught.exception.code, 'STATE_LOCATION')

    @unittest.skipUnless(SERVICE_IDENTITY_AVAILABLE, SERVICE_SKIP)
    def test_service_state_owner_and_root_config_owners_are_enforced(self):
        host_profile.validate(self.config, self.config_path)
        for path, bad_owner, code in [(self.app, SERVICE_UID, 'HOST_PATH_TRUST'),
                                      (self.config_path, SERVICE_UID, 'HOST_PATH_TRUST'),
                                      (self.state, 0, 'STATE_OWNER_OR_MODE')]:
            original = path.stat().st_uid
            try:
                os.chown(path, bad_owner, -1)
                with self.subTest(path=path.name), self.assertRaises(OpsError) as caught:
                    host_profile.validate(self.config, self.config_path)
                self.assertEqual(caught.exception.code, code)
            finally:
                os.chown(path, original, -1)

    def test_executor_refuses_wrong_peer_host_installation_and_generation(self):
        payload = self.payload()
        cases = [(self.executor, payload, SERVICE_UID - 1, 'ADMIN_PEER'),
                 (dict(self.executor, hostname='foreign-host'), payload, SERVICE_UID, 'ADMIN_HOST'),
                 (dict(self.executor, enabled=False), payload, SERVICE_UID, 'ADMIN_DISABLED'),
                 (self.executor, dict(payload, installation_id='asa-test'), SERVICE_UID, 'ADMIN_INSTALLATION'),
                 (self.executor, dict(payload, installation_instance_id='b' * 32), SERVICE_UID, 'STALE_INSTALLATION'),
                 (dict(self.executor, installation_instance_id='b' * 32), payload, SERVICE_UID, 'STALE_INSTALLATION'),
                 (dict(self.executor, operations_mode='implicit-root'), payload, SERVICE_UID, 'ADMIN_PROFILE'),
                 (dict(self.executor, owner_uid=0), payload, 0, 'ADMIN_PEER')]
        for config, request, uid, code in cases:
            with self.subTest(code=code), self.assertRaisesRegex(ValueError, '^'+code+'$'):
                admin_executor.authorize(config, request, uid)
        self.identity_path.write_text(json.dumps(dict(self.identity, instanceId='b' * 32)))
        with self.assertRaisesRegex(ValueError, '^STALE_INSTALLATION$'):
            admin_executor.authorize(self.executor, payload, SERVICE_UID)

    def test_executor_attests_private_modes_real_path_and_compose_profile(self):
        for field in ('installation_profile', 'installation_project'):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, '^ADMIN_INSTALLATION_PROFILE$'):
                admin_executor.authorize(dict(self.executor, **{field: 'foreign'}), self.payload(), SERVICE_UID)
        for path in (self.app, self.app / '.asa', self.identity_path):
            original = stat.S_IMODE(path.stat().st_mode)
            try:
                path.chmod(original | 0o022)
                with self.subTest(path=path.name), self.assertRaisesRegex(ValueError, '^ADMIN_INSTALLATION_TRUST$'):
                    admin_executor.authorize(self.executor, self.payload(), SERVICE_UID)
            finally:
                path.chmod(original)
        link = self.base / 'application-link'
        link.symlink_to(self.app, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, '^ADMIN_INSTALLATION_PATH$'):
            admin_executor.authorize(dict(self.executor, root=str(link)), self.payload(), SERVICE_UID)
        self.identity_path.unlink()
        target = self.base / 'replacement-identity.json'
        target.write_text(json.dumps(self.identity))
        self.identity_path.symlink_to(target)
        with self.assertRaisesRegex(ValueError, '^ADMIN_INSTALLATION_TRUST$'):
            admin_executor.authorize(self.executor, self.payload(), SERVICE_UID)

    @unittest.skipUnless(SERVICE_IDENTITY_AVAILABLE, SERVICE_SKIP)
    def test_executor_refuses_service_owned_application_or_authoritative_identity(self):
        admin_executor.authorize(self.executor, self.payload(), SERVICE_UID)
        for path in (self.app, self.app / '.asa', self.identity_path):
            try:
                os.chown(path, SERVICE_UID, -1)
                with self.subTest(path=path.name), self.assertRaisesRegex(ValueError, '^ADMIN_INSTALLATION_TRUST$'):
                    admin_executor.authorize(self.executor, self.payload(), SERVICE_UID)
            finally:
                os.chown(path, 0, -1)

    def test_status_returns_only_safe_identity(self):
        config = dict(self.executor, secret_files=['/private/not-for-output'], token='not-for-output')
        value = admin_executor.execute(config, {'action': 'status', 'installation_id': INSTALLATION}, self.audit)
        self.assertEqual(set(value), {'enabled', 'hostname', 'uid', 'installation_id', 'installation_instance_id', 'authority'})
        self.assertEqual(value['uid'], 0)
        self.assertEqual(value['installation_id'], INSTALLATION)
        self.assertEqual(value['installation_instance_id'], INSTANCE)
        self.assertEqual(value['hostname'], socket.gethostname())
        self.assertTrue(value['enabled'])
        self.assertNotIn('not-for-output', json.dumps(value))

    def test_inventory_is_bounded_safe_metadata_and_runs_as_root(self):
        row = {'ID': 'abc', 'Names': 'asa-lab-school-api-1', 'Image': 'local-test-image',
               'State': 'running', 'Status': 'Up', 'Ports': '127.0.0.1:8080', 'Networks': 'school',
               'CreatedAt': '2026-11-07', 'RunningFor': '1 hour', 'Labels': 'private-label',
               'Command': 'private-command', 'Env': ['private-env'], 'Mounts': 'private-mount',
               'environment': 'private-environment', 'unknown': 'private-extra'}
        output = '\n'.join(json.dumps(row) for _ in range(207))
        with patch.object(admin_executor.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, output, '')) as docker:
            value = admin_executor.execute(self.executor, {'action': 'inventory', 'installation_id': INSTALLATION}, self.audit)
        docker.assert_called_once_with(['docker', 'ps', '-a', '--format', '{{json .}}'], capture_output=True, text=True, timeout=30)
        self.assertEqual(value['executor_uid'], 0)
        self.assertEqual(value['installation_instance_id'], INSTANCE)
        self.assertEqual(value['containers_total'], 207)
        self.assertEqual(len(value['containers']), 200)
        self.assertTrue(value['containers_truncated'])
        self.assertEqual(set(value['containers'][0]), {'ID', 'Names', 'Image', 'State', 'Status', 'Ports', 'Networks', 'CreatedAt', 'RunningFor'})
        self.assertNotIn('private-', json.dumps(value))

    def test_large_raw_output_is_private_and_secrets_crossing_old_boundary_are_redacted(self):
        env_secret = 'fake-school-gateway-env-secret-1234567890'
        owner_secret = 'fake-bare-owner-password-ABCDEFGHIJKLMNOPQRSTUVWXYZ'
        boundary_secret = 'BOUNDARY-PRIVATE-TOKEN-0123456789abcdef'
        env = self.base / 'school-gateway.env'
        env.write_text('PROXY_BEARER_TOKEN='+env_secret+'\nBOUNDARY_TOKEN='+boundary_secret+'\n')
        password = self.base / 'owner-password'
        password.write_text(owner_secret+'\n')
        for path in (env, password):
            path.chmod(0o600)
        lead = (env_secret+' '+owner_secret+' Bearer fake.jwt.signature\n').encode()
        padding = 32768 - 12 - len(lead)
        program = ('import sys; out=sys.stdout.buffer; '
                   'out.write('+repr(lead)+'); out.write(b"X"*'+str(padding)+'); '
                   'out.write('+repr(boundary_secret.encode())+'); '
                   'out.write(b"Z"*'+str(8*1024*1024+4096)+')')
        payload = dict(self.payload(), operation_id='e' * 32, cwd='/', timeout_seconds=10,
                       command='/usr/bin/python3 -I -c '+shlex.quote(program))
        config = dict(self.executor, secret_files=[str(env), str(password)])
        admin_executor.authorize(config, payload, SERVICE_UID)
        result = admin_executor.execute(config, payload, self.audit)
        self.assertEqual(result['exit_code'], 0)
        self.assertFalse(result['timed_out'])
        self.assertTrue(result['output_may_be_truncated'])
        self.assertLessEqual(len(result['output'].encode()), 32768)
        for secret in (env_secret, owner_secret, 'fake.jwt.signature', boundary_secret, boundary_secret[:12]):
            self.assertNotIn(secret, result['output'])
        self.assertIn('[REDACTED]', result['output'])
        private = self.audit / ('e' * 32) / 'output.private.log'
        self.assertEqual(private.stat().st_size, 8 * 1024 * 1024)
        self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o600)
        self.assertEqual(private.stat().st_uid, 0)
        raw = private.read_bytes()
        self.assertEqual(raw[32768-12:32768-12+len(boundary_secret)], boundary_secret.encode())
        self.assertIn(owner_secret.encode(), raw)
        self.assertNotIn('request_hash', result)


class WorkerProfileTests(RootFixture):
    def queued(self, *, kind='admin', generation=INSTANCE):
        store = Store(self.state)
        plan = store.plan(INSTALLATION, {'kind': kind, 'installation_instance_id': generation,
                                          'command': 'test command never executed in worker unit test',
                                          'cwd': '/', 'timeout_seconds': 5})
        record, created = store.start(INSTALLATION, kind, plan['plan_id'], 'host-only-worker-fixture-0001', {'fixture': kind})
        self.assertTrue(created)
        return store, record['operation_id']

    def run_guarded(self, opid, action, *, error=None):
        original_read, original_exists = Path.read_text, Path.exists
        def read(path, *args, **kwargs):
            if path == self.app or self.app in path.parents:
                raise AssertionError('Worker tried reading the private application')
            return original_read(path, *args, **kwargs)
        def exists(path):
            if path == self.app or self.app in path.parents:
                raise AssertionError('Worker tried probing private maintenance receipts')
            return original_exists(path)
        response = {'status': 'complete', 'operation_id': opid, 'exit_code': 0,
                    'timed_out': False, 'output': '{"uid":0}', 'output_may_be_truncated': False}
        with patch.object(host_profile.os, 'geteuid', return_value=SERVICE_UID), \
                patch.object(Path, 'read_text', new=read), patch.object(Path, 'exists', new=exists), \
                patch.object(worker, 'adapter', side_effect=AssertionError('ASA adapter called')) as bridge, \
                patch.object(maintenance, 'check_generation', side_effect=AssertionError('Private generation read')) as generation, \
                patch.object(admin, 'request', side_effect=error, return_value=response) as request:
            worker.work(str(self.config_path), opid, action)
        bridge.assert_not_called()
        generation.assert_not_called()
        return request

    @unittest.skipUnless(SERVICE_IDENTITY_AVAILABLE, SERVICE_SKIP)
    def test_host_worker_success_never_reads_application_or_imports_adapter(self):
        store, opid = self.queued()
        request = self.run_guarded(opid, 'admin')
        self.assertEqual(store.get(opid)['status'], 'success')
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args[1]['installation_instance_id'], INSTANCE)
        self.assertEqual([line['phase'] for line in store.log(opid)['lines']], ['admin', 'success'])

    @unittest.skipUnless(SERVICE_IDENTITY_AVAILABLE, SERVICE_SKIP)
    def test_host_worker_error_uses_operation_audit_without_maintenance_receipts(self):
        store, opid = self.queued()
        request = self.run_guarded(opid, 'admin', error=OpsError('ADMIN_PROTOCOL', 'Uncertain root result; do not replay'))
        self.assertEqual(request.call_count, 1)
        record = store.get(opid)
        self.assertEqual(record['status'], 'recovery_required')
        self.assertEqual(record['result']['error']['code'], 'ADMIN_PROTOCOL')
        self.assertEqual([line['phase'] for line in store.log(opid)['lines']], ['admin', 'error'])

    @unittest.skipUnless(SERVICE_IDENTITY_AVAILABLE, SERVICE_SKIP)
    def test_host_worker_rejects_lifecycle_before_root_or_adapter(self):
        store, opid = self.queued(kind='update')
        request = self.run_guarded(opid, 'update')
        request.assert_not_called()
        self.assertEqual(store.get(opid)['status'], 'failed')
        self.assertEqual(store.get(opid)['result']['error']['code'], 'CAPABILITY_DISABLED')

    @unittest.skipUnless(SERVICE_IDENTITY_AVAILABLE, SERVICE_SKIP)
    def test_host_worker_rejects_stale_generation_before_execute(self):
        store, opid = self.queued(generation='b' * 32)
        request = self.run_guarded(opid, 'admin')
        request.assert_not_called()
        self.assertEqual(store.get(opid)['status'], 'recovery_required')
        self.assertEqual(store.get(opid)['result']['error']['code'], 'STALE_INSTALLATION')

    def test_adapter_itself_rejects_host_only_before_subprocess(self):
        with patch.object(worker.subprocess, 'Popen', side_effect=AssertionError('ASA subprocess started')) as process, \
                self.assertRaises(OpsError) as caught:
            worker.adapter(self.config_path, 'plan', {})
        self.assertEqual(caught.exception.code, 'CAPABILITY_DISABLED')
        process.assert_not_called()


@unittest.skipUnless(SERVICE_IDENTITY_AVAILABLE and hasattr(os, 'fork') and hasattr(socket, 'SO_PEERCRED'), SERVICE_SKIP)
class UnixPeerIntegration(RootFixture):
    def test_service_uid_cannot_read_checkout_but_executes_once_through_root_socket(self):
        socket_path = self.base / 'control.sock'
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(str(socket_path))
        socket_path.chmod(0o600)
        os.chown(socket_path, SERVICE_UID, SERVICE_UID)
        listener.listen(8)
        listener.settimeout(0.2)
        marker = self.audit / 'root-marker'
        program = ('import json,os; from pathlib import Path; p=Path('+repr(str(marker))+'); '
                   'f=p.open("a"); f.write("root-once\\n"); f.close(); p.chmod(0o600); '
                   'print(json.dumps({"uid":os.geteuid()}))')
        command = '/usr/bin/python3 -I -c '+shlex.quote(program)
        reader, writer = os.pipe()
        pid = os.fork()
        if pid == 0:
            try:
                os.close(reader)
                listener.close()
                os.setgroups([])
                os.setgid(SERVICE_UID)
                os.setuid(SERVICE_UID)
                try:
                    self.identity_path.read_text()
                except PermissionError:
                    private_checkout = True
                else:
                    raise AssertionError('Service unexpectedly read the root0700 checkout')
                with patch.object(admin, 'SOCKET', str(socket_path)):
                    controller = server.Controller(self.config_path, official=FakeOfficial())
                    def synchronous_launch(record, action):
                        worker.work(str(self.config_path), record['operation_id'], action)
                        return controller.store.get(record['operation_id'])
                    controller.launch = synchronous_launch
                    self.assertEqual(len(controller.rpc({'method': 'tools/list'})['tools']), 16)
                    for name in FORBIDDEN_TOOLS:
                        with self.assertRaises(OpsError) as caught:
                            controller.dispatch(name, {})
                        self.assertEqual(caught.exception.code, 'CAPABILITY_DISABLED')
                    status = controller.dispatch('asa_admin_status', {'installation_id': INSTALLATION})
                    self.assertEqual(status['uid'], 0)
                    self.assertEqual(status['installation_instance_id'], INSTANCE)
                    args = {'installation_id': INSTALLATION, 'command': command, 'cwd': '/',
                            'timeout_seconds': 5, 'idempotency_key': 'host-only-fork-acceptance-0001'}
                    first = controller.dispatch('asa_admin_execute', args)
                    replay = controller.dispatch('asa_admin_execute', args)
                    self.assertFalse(first['reused'])
                    self.assertTrue(replay['reused'])
                    opid = first['operation']['operation_id']
                    self.assertEqual(replay['operation']['operation_id'], opid)
                    operation = controller.dispatch('asa_operation_get', {'operation_id': opid, 'log_cursor': 0})
                    self.assertEqual(operation['status'], 'success')
                    self.assertEqual(json.loads(operation['result']['output']), {'uid': 0})
                    self.assertTrue(operation['log']['lines'])
                    listed = controller.dispatch('asa_operation_list', {'installation_id': INSTALLATION})
                    self.assertEqual([item['operation_id'] for item in listed['operations']], [opid])
                report = {'ok': True, 'service_uid': os.geteuid(), 'checkout_private': private_checkout, 'operation_id': opid}
            except BaseException:
                report = {'ok': False, 'error': traceback.format_exc()}
            os.write(writer, json.dumps(report).encode())
            os.close(writer)
            os._exit(0 if report['ok'] else 1)
        os.close(writer)
        stopped = threading.Event()
        requests, errors = [], []
        def executor_loop():
            while not stopped.is_set():
                try:
                    peer, _ = listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    return
                with peer:
                    try:
                        _, uid, _ = struct.unpack('3i', peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                        with peer.makefile('rb') as stream:
                            raw = stream.readline(65537)
                        payload = json.loads(raw)
                        requests.append((uid, os.geteuid(), payload['action']))
                        admin_executor.authorize(self.executor, payload, uid)
                        result = {'ok': True, 'data': admin_executor.execute(self.executor, payload, self.audit)}
                    except Exception as error:
                        errors.append(type(error).__name__+': '+str(error))
                        result = {'ok': False, 'code': 'TEST_EXECUTOR_FAILURE'}
                    peer.sendall(json.dumps(result).encode()+b'\n')
        thread = threading.Thread(target=executor_loop, daemon=True)
        thread.start()
        reaped = False
        try:
            ready, _, _ = select.select([reader], [], [], 20)
            self.assertTrue(ready, 'Unprivileged controller did not complete its bounded socket proof')
            report = json.loads(os.read(reader, 65536))
            self.assertTrue(report['ok'], report.get('error'))
            self.assertEqual(report['service_uid'], SERVICE_UID)
            self.assertTrue(report['checkout_private'])
            _, status = os.waitpid(pid, 0)
            reaped = True
            self.assertEqual(os.waitstatus_to_exitcode(status), 0)
            self.assertEqual(marker.read_text(), 'root-once\n')
            self.assertEqual(marker.stat().st_uid, 0)
            self.assertEqual(stat.S_IMODE(marker.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(socket_path.stat().st_mode), 0o600)
            self.assertEqual(socket_path.stat().st_uid, SERVICE_UID)
            self.assertEqual(sum(action == 'execute' for _, _, action in requests), 1)
            self.assertTrue(all(uid == SERVICE_UID and executor_uid == 0 for uid, executor_uid, _ in requests))
            self.assertEqual(errors, [])
            self.assertEqual(json.loads(self.identity_path.read_text()), self.identity)
            root_log = self.audit / report['operation_id'] / 'output.private.log'
            self.assertEqual(stat.S_IMODE(root_log.stat().st_mode), 0o600)
            self.assertEqual(self.state.stat().st_uid, SERVICE_UID)
            self.assertEqual(self.app.stat().st_uid, 0)
            self.assertEqual(stat.S_IMODE(self.app.stat().st_mode), 0o700)
        finally:
            os.close(reader)
            if not reaped:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                os.waitpid(pid, 0)
            stopped.set()
            thread.join(timeout=6)
            self.assertFalse(thread.is_alive(), 'Fixture executor thread failed to stop')


if __name__ == '__main__':
    unittest.main()
