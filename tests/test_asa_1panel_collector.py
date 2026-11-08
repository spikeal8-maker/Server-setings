"""Native wire/state tests. No package, service, VM or real 1Panel is changed."""
import copy
from contextlib import nullcontext
import importlib.util
import io
import itertools
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/asa-1panel-collector.py'
spec = importlib.util.spec_from_file_location('test_panel_collector_bridge', SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class FakePanel:
    def __init__(self, config):
        self.config = config
        self.rows = []
        self.calls = []
        self.fail_operation = None
        self.ready = True

    def request(self, method, path, payload=None):
        self.calls.append((method, path, copy.deepcopy(payload)))
        if path == '/hosts/tool/status':
            return {'type': 'supervisord', 'config': {'isExist': self.ready, 'ctlExist': self.ready,
                    'status': 'running' if self.ready else '', 'init': False, 'version': '4.2.5'}}
        if method == 'GET':
            return copy.deepcopy(self.rows)
        operation = payload['operate']
        if operation == self.fail_operation:
            raise module.PanelCollectorError('PANEL_REQUEST_FAILED')
        if operation == 'create':
            self.rows.append({key: value for key, value in payload.items() if key != 'operate'})
            self.rows[-1]['status'] = [{'status': 'STOPPED', 'PID': '', 'uptime': ''}]
        elif operation == 'update':
            for row in self.rows:
                if row['name'] == payload['name']:
                    row.update({key: value for key, value in payload.items() if key != 'operate'})
                    row['status'] = [{'status': 'RUNNING' if row['autoStart'] == 'true' else 'STOPPED',
                                      'PID': '7654' if row['autoStart'] == 'true' else '', 'uptime': '0:00:04'}]
        elif operation in ('start', 'restart', 'stop'):
            for row in self.rows:
                if row['name'] == payload['name']:
                    row['status'] = [{'status': 'STOPPED' if operation == 'stop' else 'RUNNING',
                                      'PID': '' if operation == 'stop' else '8765', 'uptime': '0:00:04'}]


class NativeCollectorTests(unittest.TestCase):
    def setUp(self):
        self.config = {'root': '/srv/asa-lab', 'project': 'asa-lab-school',
                       'program': 'asa-log-collector-asa-lab-school', 'instanceId': 'f' * 32}
        self.api = FakePanel(self.config)
        for name, value in (('program_directory_snapshot', ('/native/supervisor.d', 1, 2, 3, ())),
                            ('canonical', (None, None)),
                            ('handover_receipt', {'state': 'accepted'})):
            patched = patch.object(module, name, return_value=value)
            patched.start()
            self.addCleanup(patched.stop)

    def test_stage_never_starts_process_and_repeated_stage_is_idempotent(self):
        before = module.stage(self.api, self.config)
        self.assertEqual(before['state'], 'planned')
        self.assertFalse(any(method == 'POST' and path == module.PROCESS_ROUTE for method, path, _ in self.api.calls))
        self.assertEqual(module.stage(self.api, self.config, True)['state'], 'staged')
        module.stage(self.api, self.config, True)
        writes = [payload for method, path, payload in self.api.calls if method == 'POST' and path == module.PROCESS_ROUTE]
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0]['autoStart'], 'false')
        self.assertEqual(writes[0]['numprocs'], '1')
        self.assertEqual(writes[0]['environment'], '')

    def test_stage_rejects_active_existing_process_without_modifying_it(self):
        module.stage(self.api, self.config, True)
        self.api.rows[0]['autoStart'] = 'true'
        self.api.rows[0]['status'] = [{'status': 'RUNNING', 'PID': '111'}]
        before = len(self.api.calls)
        with self.assertRaisesRegex(module.PanelCollectorError, 'NOT_STOPPED'):
            module.stage(self.api, self.config, True)
        self.assertFalse(any(m == 'POST' and path == module.PROCESS_ROUTE for m, path, _ in self.api.calls[before:]))

    def test_existing_foreign_or_parallel_registration_is_never_overwritten(self):
        module.stage(self.api, self.config, True)
        self.api.rows[0]['command'] = '/usr/bin/python3 /srv/other.py'
        with self.assertRaises(module.PanelCollectorError):
            module.stage(self.api, self.config, True)
        self.assertEqual(len([p for m, path, p in self.api.calls if m == 'POST' and path == module.PROCESS_ROUTE]), 1)
        self.api.rows = [{'name': 'foreign', 'dir': self.config['root'], 'command': 'python3 tools/collect_logs.py'}]
        with self.assertRaisesRegex(module.PanelCollectorError, 'PARALLEL'):
            module.stage(self.api, self.config, True)

    def test_missing_supervisor_is_a_prerequisite_not_success(self):
        self.api.ready = False
        with self.assertRaisesRegex(module.PanelCollectorError, 'PREREQUISITE'):
            module.stage(self.api, self.config, True)
        self.assertEqual(self.api.rows, [])

    def test_nil_process_list_requires_unchanged_empty_native_directory(self):
        real = self.api.request
        self.api.request = lambda method, path, data=None: None if method == 'GET' else real(method, path, data)
        self.assertIsNone(module.process(self.api, self.config, missing=True))
        with patch.object(module, 'program_directory_snapshot', return_value=('/native', 1, 2, 3, ('foreign.ini',))), \
                self.assertRaisesRegex(module.PanelCollectorError, 'NIL_WITHOUT_EMPTY'):
            module.process(self.api, self.config, missing=True)
        with self.assertRaisesRegex(module.PanelCollectorError, 'PROCESS_SCHEMA'):
            module.process(self.api, self.config, missing=False)

    def test_reconcile_checks_old_service_before_native_start(self):
        with patch.object(module, 'registration'), \
                patch.object(module, 'require_legacy_disabled', side_effect=module.PanelCollectorError('LEGACY_COLLECTOR_STILL_ENABLED_OR_RUNNING')):
            with self.assertRaisesRegex(module.PanelCollectorError, 'LEGACY'):
                module.reconcile(self.api, self.config, {'collectorManager': module.MANAGER}, apply=True)
        self.assertEqual(self.api.calls, [])

    def test_reconcile_requires_new_adapter_even_with_selected_metadata(self):
        with patch.object(module, 'canonical', side_effect=module.PanelCollectorError('CANONICAL_PANEL_ADAPTER_MISSING')), \
                self.assertRaisesRegex(module.PanelCollectorError, 'ADAPTER_MISSING'):
            module.reconcile(self.api, self.config, {'collectorManager': module.MANAGER}, apply=True)
        self.assertEqual(self.api.calls, [])

    def test_reconcile_rechecks_process_after_native_restart(self):
        module.stage(self.api, self.config, True)
        self.api.rows[0]['autoStart'] = 'true'
        self.api.rows[0]['status'] = [{'status': 'RUNNING', 'PID': '111'}]
        with patch.object(module, 'registration'), patch.object(module, 'require_legacy_disabled') as old:
            result = module.reconcile(self.api, self.config, {'collectorManager': module.MANAGER}, True, True)
        self.assertEqual(result['processCount'], 1)
        self.assertTrue(result['restartRequested'])
        self.assertEqual(old.call_count, 2)
        restart = next(i for i, call in enumerate(self.api.calls) if call[2] and call[2].get('operate') == 'restart')
        self.assertTrue(any(call[0] == 'GET' for call in self.api.calls[restart + 1:]))

    def test_native_autostart_starting_is_observed_without_duplicate_start(self):
        for pending, restart in itertools.product(([{'status': 'STARTING', 'PID': '111'}], None, []), (False, True)):
            with self.subTest(restart=restart, pending=pending):
                api = FakePanel(self.config)
                module.stage(api, self.config, True)
                api.rows[0]['autoStart'] = 'true'
                api.rows[0]['status'] = pending
                real_request = api.request
                gets = 0
                def request(method, path, payload=None):
                    nonlocal gets
                    if method == 'GET':
                        gets += 1
                        if gets >= 2:
                            api.rows[0]['status'] = [{'status': 'RUNNING', 'PID': '111'}]
                    return real_request(method, path, payload)
                api.request = request
                with patch.object(module, 'registration'), patch.object(module, 'require_legacy_disabled'), \
                        patch.object(module.time, 'sleep'):
                    result = module.reconcile(api, self.config, {'collectorManager': module.MANAGER}, restart, True)
                operations = [p['operate'] for m, path, p in api.calls if m == 'POST' and path == module.PROCESS_ROUTE]
                self.assertEqual(operations, ['create', 'restart'] if restart else ['create'])
                self.assertEqual(result['state'], 'running')

    def test_native_empty_status_timeout_never_sends_start(self):
        module.stage(self.api, self.config, True)
        self.api.rows[0]['autoStart'] = 'true'
        self.api.rows[0]['status'] = None
        with patch.object(module, 'registration'), patch.object(module, 'require_legacy_disabled'), \
                patch.object(module.time, 'monotonic', side_effect=[0, 21]), \
                self.assertRaisesRegex(module.PanelCollectorError, 'NOT_SETTLED'):
            module.reconcile(self.api, self.config, {'collectorManager': module.MANAGER}, apply=True)
        self.assertFalse(any(p and p.get('operate') in ('start', 'restart') for _, _, p in self.api.calls))

    def test_unsettled_native_state_cannot_trigger_a_parallel_start(self):
        module.stage(self.api, self.config, True)
        self.api.rows[0]['autoStart'] = 'true'
        for state in ('STOPPING', 'BACKOFF', 'UNKNOWN', 'FATAL', 'EXITED'):
            self.api.rows[0]['status'] = [{'status': state, 'PID': ''}]
            with self.subTest(state=state), patch.object(module, 'registration'), \
                    patch.object(module, 'require_legacy_disabled'), self.assertRaisesRegex(module.PanelCollectorError, 'UNSETTLED'):
                module.reconcile(self.api, self.config, {'collectorManager': module.MANAGER}, apply=True)
        self.assertFalse(any(p and p.get('operate') in ('start', 'restart') for _, _, p in self.api.calls))

    def test_disabled_native_process_stays_disabled_on_updater(self):
        module.stage(self.api, self.config, True)
        with patch.object(module, 'registration'), patch.object(module, 'require_legacy_disabled'), \
                self.assertRaisesRegex(module.PanelCollectorError, 'DISABLED_BY_OPERATOR'):
            module.reconcile(self.api, self.config, {'collectorManager': module.MANAGER}, apply=True)
        self.assertFalse(any(call[2] and call[2].get('operate') in ('start', 'restart') for call in self.api.calls))

    def test_prepared_handover_cannot_be_reconciled_as_accepted(self):
        with patch.object(module, 'registration'), patch.object(module, 'handover_receipt', return_value={'state': 'prepared'}), \
                self.assertRaisesRegex(module.PanelCollectorError, 'EXPLICIT_RECOVERY'):
            module.reconcile(self.api, self.config, {'collectorManager': module.MANAGER}, apply=True)
        self.assertEqual(self.api.calls, [])

    def test_api_failure_cannot_report_success_or_create_fallback(self):
        module.stage(self.api, self.config, True)
        self.api.rows[0]['autoStart'] = 'true'
        self.api.fail_operation = 'start'
        with patch.object(module, 'registration'), patch.object(module, 'require_legacy_disabled'), \
                patch.object(module, 'run') as command, self.assertRaises(module.PanelCollectorError):
            module.reconcile(self.api, self.config, {'collectorManager': module.MANAGER}, apply=True)
        command.assert_not_called()

    def test_starting_process_is_not_safe_rollback_evidence(self):
        for state in ('STARTING', 'RUNNING', 'STOPPING', 'BACKOFF', 'FATAL', 'UNKNOWN'):
            self.assertFalse(module.is_stopped({'status': [{'status': state, 'PID': ''}]}))
        self.assertTrue(module.is_stopped({'status': [{'status': 'STOPPED', 'PID': ''}]}))
        self.assertFalse(module.is_running({'status': [{'status': 'RUNNING', 'PID': '0'}]}))

    def test_api_client_rejects_nonlocal_or_credentialed_urls(self):
        for host in ('https://example.com', 'http://192.168.1.10:19192', 'http://secret@127.0.0.1:19192',
                     'http://127.0.0.1:19192/?key=private', 'http://127.0.0.1:19192/mcp'):
            with self.subTest(host=host), self.assertRaises(module.PanelCollectorError):
                module.PanelAPI(host, 'a' * 32)

    def test_native_process_output_excludes_environment_and_commands(self):
        row = {**module.expected_process(self.config), 'environment': 'SECRET=private',
               'status': [{'status': 'STOPPED', 'PID': '', 'uptime': '', 'msg': 'private'}]}
        output = json.dumps(module.safe_process(row))
        self.assertNotIn('private', output)
        self.assertNotIn('command', output)

    def test_native_log_source_is_idempotent_and_preserves_existing_configuration(self):
        original = {'version': 1, 'retentionDays': 30, 'fileSources': [{'source': 'backup', 'path': '/retained/logs'}]}
        updated = module.with_native_logs(original, Path('/native/log'))
        self.assertEqual(updated['fileSources'][0], original['fileSources'][0])
        self.assertEqual(updated['fileSources'][1], {'source': 'linux:1panel-supervisor', 'path': '/native/log'})
        self.assertEqual(len(original['fileSources']), 1)
        self.assertEqual(module.with_native_logs(updated, Path('/native/log')), updated)
        with self.assertRaisesRegex(module.PanelCollectorError, 'SOURCE_COLLISION'):
            module.with_native_logs(updated, Path('/different/log'))


class PrerequisiteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        installer_path = SCRIPT.with_name('install-asa-1panel-collector.py')
        spec = importlib.util.spec_from_file_location('panel_collector_installer', installer_path)
        cls.installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.installer)

    def test_apt_plan_pins_only_new_packages(self):
        output = 'Inst python3-pkg-resources (68.1.2-2 Ubuntu:24.04/noble [all])\nInst supervisor (4.2.5-1 Ubuntu:24.04/noble [all])\n'
        with patch.object(self.installer.bridge, 'run', return_value=output):
            self.assertEqual(self.installer.apt_plan(), [('python3-pkg-resources', '68.1.2-2'), ('supervisor', '4.2.5-1')])

    def test_apt_upgrade_removal_and_empty_plan_are_rejected(self):
        for output in ('Inst supervisor [4.2.4] (4.2.5-1 Ubuntu [all])', 'Remv python3\n', '0 upgraded\n'):
            with self.subTest(output=output), patch.object(self.installer.bridge, 'run', return_value=output), \
                    self.assertRaises(self.installer.bridge.PanelCollectorError):
                self.installer.apt_plan()

    def test_apply_requires_reviewed_package_digest_before_any_mutation(self):
        config = {'root': '/srv/asa-lab', 'project': 'asa-lab-school', 'panelVersion': 'v2.3.2',
                  'program': 'asa-log-collector-asa-lab-school'}
        arguments = ['install-asa-1panel-collector.py', '--root', config['root'], '--panel-host', 'http://127.0.0.1:19192',
                     '--apply', '--install-supervisor']
        for suffix in ([], ['--apt-plan-sha256', '0' * 64]):
            with self.subTest(suffix=suffix), patch.object(sys, 'argv', arguments + suffix), \
                    patch.object(self.installer.bridge.os, 'geteuid', return_value=0), \
                    patch.object(self.installer.bridge, 'controlled', return_value=b'reviewed bridge'), \
                    patch.object(self.installer, 'configuration', return_value=config), \
                    patch.object(self.installer.bridge, 'api_client'), \
                    patch.object(self.installer.bridge, 'canonical', return_value=(None, None)), \
                    patch.object(self.installer, 'supervisor_status', return_value={'isExist': False}), \
                    patch.object(self.installer, 'apt_plan', return_value=[('supervisor', '4.2.5-1ubuntu0.1')]), \
                    patch.object(self.installer, 'private_directory') as directories, \
                    patch.object(self.installer, 'install_supervisor') as packages, \
                    patch.object(sys, 'stdout', new_callable=io.StringIO) as output:
                self.assertEqual(self.installer.main(), 2)
                self.assertEqual(json.loads(output.getvalue())['code'], 'APT_REVIEWED_PLAN_REQUIRED')
                directories.assert_not_called()
                packages.assert_not_called()

    def test_package_install_uses_reviewed_versions_and_forbids_removal(self):
        packages = [('supervisor', '4.2.5-1ubuntu0.1')]
        with patch.object(self.installer, 'apt_plan', return_value=packages), \
                patch.object(self.installer.bridge, 'run') as execute, \
                patch.object(self.installer, 'supervisor_status', return_value={'isExist': True, 'ctlExist': True}):
            self.installer.install_supervisor(None, packages)
        self.assertEqual(execute.call_args.args[0], ['/usr/bin/apt-get', '--yes', '--no-install-recommends',
                    '--no-upgrade', '--no-remove', 'install', 'supervisor=4.2.5-1ubuntu0.1'])

    def test_legacy_adapter_is_restricted_to_preselection_installer_context(self):
        install = SimpleNamespace(settings={'collectorMode': 'system-root'})
        legacy = SimpleNamespace(Installation=lambda root: install, root_runner_paths=lambda install: None,
                                 validate_config=lambda install: None)
        with patch.object(module, 'controlled', return_value=b'trusted canonical source'), \
                patch.object(module.importlib, 'import_module', return_value=legacy), \
                patch.object(module.sys, 'path', list(sys.path)):
            self.assertEqual(module.canonical({'root': '/srv/asa-lab'}, require_adapter=False), (legacy, install))
            with self.assertRaisesRegex(module.PanelCollectorError, 'ADAPTER_MISSING'):
                module.canonical({'root': '/srv/asa-lab'})
            install.settings['collectorManager'] = module.MANAGER
            with self.assertRaisesRegex(module.PanelCollectorError, 'ADAPTER_MISSING'):
                module.canonical({'root': '/srv/asa-lab'}, require_adapter=False)

    def test_installer_with_legacy_adapter_stages_without_selecting_or_starting(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            state = root / '.asa'
            state.mkdir()
            runner = state / 'collector-runner.json'
            runner.write_text('{"manager":null}')
            install = SimpleNamespace(state=state, settings={'collectorMode': 'system-root'})
            config = {'root': str(root), 'project': 'asa-lab-school', 'panelVersion': 'v2.3.2',
                      'program': 'asa-log-collector-asa-lab-school'}
            api = FakePanel(config)
            with patch.object(sys, 'argv', ['installer', '--root', str(root), '--panel-host', 'http://127.0.0.1:19192', '--apply']), \
                    patch.object(self.installer.bridge.os, 'geteuid', return_value=0), \
                    patch.object(self.installer.bridge, 'controlled', return_value=b'reviewed bridge'), \
                    patch.object(self.installer, 'configuration', return_value=config), \
                    patch.object(self.installer.bridge, 'api_client', return_value=api), \
                    patch.object(self.installer.bridge, 'canonical', return_value=(SimpleNamespace(), install)) as canonical, \
                    patch.object(self.installer, 'supervisor_status', return_value={'isExist': True, 'init': False}), \
                    patch.object(self.installer, 'private_directory'), patch.object(self.installer, 'initialize_supervisor'), \
                    patch.object(self.installer.bridge, 'program_directory_snapshot', return_value=('/native', 1, 2, 3, ())), \
                    patch.object(self.installer, 'new_file') as writes, patch.object(sys, 'stdout', new_callable=io.StringIO):
                self.assertEqual(self.installer.main(), 0)
            self.assertFalse(canonical.call_args.kwargs['require_adapter'])
            self.assertEqual(runner.read_text(), '{"manager":null}')
            self.assertNotIn('collectorManager', install.settings)
            self.assertEqual([call.args[0] for call in writes.call_args_list],
                             [self.installer.bridge.BRIDGE, self.installer.bridge.CONFIG])
            native = [payload for method, path, payload in api.calls if method == 'POST' and path == module.PROCESS_ROUTE]
            self.assertEqual(len(native), 1)
            self.assertEqual(native[0]['operate'], 'create')
            self.assertEqual(native[0]['autoStart'], 'false')
            self.assertTrue(module.is_stopped(api.rows[0]))


class HandoverFaultTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = self.root / '.asa'
        (self.state / 'diagnostics/private').mkdir(parents=True)
        self.index = self.state / 'diagnostics/private/index.sqlite'
        self.index.write_bytes(b'opaque retained events and cursors')
        self.index_before = (self.index.read_bytes(), self.index.stat().st_ino)
        self.settings = {'project': 'asa-lab-school', 'instanceId': 'f' * 32, 'collectorMode': 'system-root'}
        self.logs = {'version': 1, 'project': 'asa-lab-school', 'fileSources': [{'source': 'backup', 'path': '/retained/logs'}]}
        self.runner = {'version': 1, 'root': str(self.root), 'project': 'asa-lab-school', 'mode': 'system-root',
                       'owner': 'root', 'name': 'asa-log-collector-asa-lab-school.service',
                       'revision': 'b' * 40, 'scriptDigest': 'a' * 64}
        self.target = {**self.runner, 'revision': 'c' * 40, 'scriptDigest': 'd' * 64}
        for name, value in (('installation.json', self.settings), ('collector-runner.json', self.runner), ('log-collector.json', self.logs)):
            path = self.state / name
            path.write_bytes(module.json_bytes(value))
            path.chmod(0o640)
        self.config = {'root': str(self.root), 'project': 'asa-lab-school', 'instanceId': 'f' * 32,
                       'program': 'asa-log-collector-asa-lab-school', 'python': '/usr/bin/python3'}
        self.api = FakePanel(self.config)
        self.old_state = {'LoadState': 'loaded', 'UnitFileState': 'enabled', 'ActiveState': 'active', 'MainPID': '1234'}
        self.commands = []
        self.force_stale = False
        self.install = SimpleNamespace(root=self.root, state=self.state, project='asa-lab-school')
        self.lifecycle = SimpleNamespace(Installation=lambda root: self.install,
                        runner_identity=lambda install: copy.deepcopy(self.target),
                        verify_systemd_state=lambda *args: None, runner_lock=lambda install: nullcontext(), status=self.health)
        bindings = [('canonical', lambda config: (self.lifecycle, self.install)),
                    ('controlled', lambda path, **kw: Path(path).read_bytes()),
                    ('legacy_state', lambda config: dict(self.old_state)),
                    ('program_directory_snapshot', lambda api: ('/native/supervisor.d', 1, 2, 3, ())),
                    ('native_log_directory', lambda api, config, staged=True: Path('/native/log')),
                    ('run', self.fake_command)]
        for name, value in bindings:
            patched = patch.object(module, name, side_effect=value)
            patched.start()
            self.addCleanup(patched.stop)

    def fake_command(self, argv, timeout=20):
        self.commands.append(argv)
        if argv[1] == 'disable':
            self.old_state['UnitFileState'] = 'disabled'
            if '--now' in argv:
                self.old_state.update(ActiveState='inactive', MainPID='0')
        elif argv[1] == 'enable':
            self.old_state['UnitFileState'] = 'enabled'
        elif argv[1] == 'start':
            self.old_state.update(ActiveState='active', MainPID='4321')
        elif argv[1] == 'stop':
            self.old_state.update(ActiveState='inactive', MainPID='0')
        return ''

    def health(self, install):
        selected = json.loads((self.state / 'installation.json').read_text()).get('collectorManager') == module.MANAGER
        return {'state': 'running', 'process': {'nonce': 'new' if selected and not self.force_stale else 'old'}}

    def assert_history_untouched(self):
        self.assertEqual((self.index.read_bytes(), self.index.stat().st_ino), self.index_before)
        self.assertEqual((self.state / 'installation.json').stat().st_mode & 0o777, 0o640)

    def test_adoption_persists_current_canonical_source_identity_not_old_registration(self):
        result = module.adopt(self.api, self.config, self.settings, apply=True)
        current = json.loads((self.state / 'collector-runner.json').read_text())
        self.assertEqual(current, {**self.target, 'manager': module.MANAGER})
        self.assertEqual(result['handover'], 'accepted')
        self.assertEqual(self.old_state['UnitFileState'], 'disabled')
        self.assertEqual(self.old_state['ActiveState'], 'inactive')
        configured = json.loads((self.state / 'log-collector.json').read_text())
        self.assertEqual(configured, module.with_native_logs(self.logs, Path('/native/log')))
        self.assert_history_untouched()

    def test_old_heartbeat_nonce_cannot_accept_handover_and_rolls_back_safely(self):
        self.force_stale = True
        with patch.object(module.time, 'monotonic', side_effect=[0, 1, 40]), \
                self.assertRaisesRegex(module.PanelCollectorError, 'FAILED_ROLLED_BACK'):
            module.adopt(self.api, self.config, self.settings, apply=True)
        self.assertEqual(json.loads((self.state / 'collector-runner.json').read_text()), self.runner)
        self.assertEqual(self.old_state['ActiveState'], 'active')
        self.assertTrue(module.is_stopped(self.api.rows[0]))
        self.assertEqual(json.loads((self.state / 'log-collector.json').read_text()), self.logs)
        self.assert_history_untouched()

    def test_uncertain_native_api_failure_keeps_old_writer_disabled_until_explicit_recovery(self):
        self.api.fail_operation = 'update'
        with self.assertRaisesRegex(module.PanelCollectorError, 'RECOVERY_REQUIRED'):
            module.adopt(self.api, self.config, self.settings, apply=True)
        self.assertEqual(self.old_state['ActiveState'], 'inactive')
        selected = json.loads((self.state / 'installation.json').read_text())
        before = len(self.api.calls)
        with self.assertRaisesRegex(module.PanelCollectorError, 'EXPLICIT_RECOVERY'):
            module.adopt(self.api, self.config, selected, apply=True)
        self.assertEqual(len(self.api.calls), before)
        self.api.fail_operation = None
        result = module.recover(self.api, self.config, selected, apply=True)
        self.assertEqual(result['state'], 'rolled_back')
        self.assertFalse(result['collectorAccepted'])
        self.assertEqual(self.old_state['ActiveState'], 'active')
        self.assertEqual(json.loads((self.state / 'collector-runner.json').read_text()), self.runner)
        self.assertEqual(json.loads((self.state / 'log-collector.json').read_text()), self.logs)
        self.assert_history_untouched()

    def test_unknown_or_accepted_receipt_cannot_trigger_automatic_rollback(self):
        module.adopt(self.api, self.config, self.settings, apply=True)
        selected = json.loads((self.state / 'installation.json').read_text())
        before = list(self.commands)
        with self.assertRaisesRegex(module.PanelCollectorError, 'NOT_RECOVERABLE'):
            module.recover(self.api, self.config, selected, apply=True)
        self.assertEqual(self.commands, before)


if __name__ == '__main__':
    unittest.main()
