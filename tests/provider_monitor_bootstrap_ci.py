"""Ephemeral GitHub Ubuntu component rehearsal only; real systemd, synthetic providers.

Never run on H03/School or a reused host. Requires explicit opt-in and absent targets.
"""
import argparse
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import grp
import json
import os
from pathlib import Path
import pwd
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
STATE = Path('/var/lib/asa-provider-bootstrap-ci')
APP = Path('/var/lib/asa-provider-bootstrap-ci-installation')
ROOT_APP = Path('/var/lib/asa-provider-bootstrap-ci-root-installation')
FIXTURE = Path('/var/lib/asa-provider-bootstrap-ci-app')
NAMES = ('asa-provider-monitor.service', 'asa-provider-monitor.timer')


def execute(argv, expected=0):
    r = subprocess.run(argv, capture_output=True, text=True, timeout=60)
    assert r.returncode == expected, (argv[0], r.returncode, r.stdout[-1000:], r.stderr[-1000:])
    return json.loads(r.stdout) if r.stdout.strip().startswith('{') else r.stdout.strip()


def mock_server():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            replies = {'/system_stats': {'system': {'ram_total': 100, 'ram_free': 50}, 'devices': []},
                       '/api/ps': {'models': []}, '/api/tags': {'models': []}}
            if self.path not in replies:
                self.send_error(404)
                return
            raw = json.dumps(replies[self.path]).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    servers = [ThreadingHTTPServer(('127.0.0.1', 0), Handler) for _ in range(2)]
    STATE.mkdir(mode=0o700)
    config = {name: 'http://127.0.0.1:'+str(server.server_port) for name, server in zip(('ollama', 'comfyui'), servers)}
    fd = os.open(STATE/'origins.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(config, stream)
        stream.flush()
        os.fsync(stream.fileno())
    for server in servers:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Event().wait(600)


def private(path, value, uid, gid):
    path.write_bytes(value if isinstance(value, bytes) else (json.dumps(value)+'\n').encode())
    path.chmod(0o600)
    os.chown(path, uid, gid)


def fixture(owner):
    APP.mkdir(mode=0o700)
    os.chown(APP, owner.pw_uid, owner.pw_gid)
    for path in (APP/'.asa', APP/'tools', APP/'tools/deployment'):
        path.mkdir(mode=0o700)
        os.chown(path, owner.pw_uid, owner.pw_gid)
    names = ('collect_logs.py', 'collect-windows-logs.ps1', 'resource_sampling.py', 'memory_sampling.py', 'deployment/operational.py')
    digest = hashlib.sha256()
    for name in names:
        raw = (FIXTURE/'tools'/name).read_bytes()
        private(APP/'tools'/name, raw, owner.pw_uid, owner.pw_gid)
        digest.update(name.encode())
        digest.update(raw)
    project = 'provider-bootstrap-ci'
    unit_name = 'asa-log-collector-'+project+'.service'
    private(APP/'.asa/installation.json', dict(format='asa-installation-v1', profile='production', project=project), owner.pw_uid, owner.pw_gid)
    private(APP/'.asa/collector-runner.json', dict(version=1, root=str(APP), project=project, owner=owner.pw_name, mode='user',
            name=unit_name, scriptDigest=digest.hexdigest(), revision='2a28cffe7a20302f9b7231ef823bdc05f3fe43d0'), owner.pw_uid, owner.pw_gid)
    config = dict(version=1, project=project, retentionDays=30, maxBytes=1024**3, intervalSeconds=20,
                  fileSources=[dict(source='existing', path='/var/lib/synthetic-existing')], preserved={'unknown': ['keep', 123]})
    private(APP/'.asa/log-collector.json', config, owner.pw_uid, owner.pw_gid)
    private(APP/'.asa/synthetic-history-marker', b'never change', owner.pw_uid, owner.pw_gid)
    unit = Path(owner.pw_dir)/'.config/systemd/user'/unit_name
    assert not unit.exists()
    execute(['setpriv', '--reuid='+str(owner.pw_uid), '--regid='+str(owner.pw_gid), '--init-groups', 'mkdir', '-p', str(unit.parent)])
    text = ('[Unit]\nDescription=ASA Lab diagnostic collector\nAfter=network.target\n[Service]\n'
            f'WorkingDirectory={APP}\nExecStart="/usr/bin/python3" "{APP}/tools/collect_logs.py" --root "{APP}"\n'
            'Restart=always\nRestartSec=20\nNoNewPrivileges=true\nUMask=0077\n[Install]\nWantedBy=default.target\n')
    private(unit, text.encode(), owner.pw_uid, owner.pw_gid)
    return config, unit_name


def hashes(paths):
    return {str(p): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_ino, p.stat().st_uid, p.stat().st_gid, p.stat().st_mode) for p in paths}


def cas_fixture():
    """As synthetic ASA owner, interpose a concurrent config write immediately at CAS."""
    assert os.geteuid() != 0 and os.environ.get('ASA_EPHEMERAL_BOOTSTRAP_CI') == '1'
    spec = importlib.util.spec_from_file_location('cas_bootstrap', ROOT/'scripts/bootstrap-provider-monitor.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    original = module.os.stat
    changed = False
    def competing_write(path, *args, **kwargs):
        nonlocal changed
        if path == 'log-collector.json' and kwargs.get('dir_fd') is not None and not changed:
            changed = True
            (APP/'.asa/log-collector.json').write_text('{"externalWriter":"preserved"}\n')
        return original(path, *args, **kwargs)
    module.os.stat = competing_write
    try:
        try:
            module.integrate(argparse.Namespace(root=str(APP), project='provider-bootstrap-ci', apply=True))
        except module.Blocked as error:
            assert str(error) == 'CONFIG_CAS' and changed
        else:
            raise AssertionError('concurrent config was not rejected')
    finally:
        module.os.stat = original
    assert json.loads((APP/'.asa/log-collector.json').read_text()) == {'externalWriter': 'preserved'}
    assert not list((APP/'.asa').glob('.provider-monitor-*.tmp'))
    print(json.dumps(dict(status='CONFIG_CAS_REJECTED', competingWritePreserved=True)))


def root_cas_fixture():
    """Only a fresh synthetic root installation; never an operating collector."""
    assert os.geteuid() == 0 and os.environ.get('GITHUB_ACTIONS') == 'true'
    assert os.environ.get('ASA_EPHEMERAL_BOOTSTRAP_CI') == '1'
    spec = importlib.util.spec_from_file_location('root_cas_bootstrap', ROOT/'scripts/bootstrap-provider-monitor.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    original, changed = module.os.stat, False
    def competing_write(path, *args, **kwargs):
        nonlocal changed
        if path == 'log-collector.json' and kwargs.get('dir_fd') is not None and not changed:
            changed = True
            (ROOT_APP/'.asa/log-collector.json').write_text('{"externalRootWriter":"preserved"}\n')
        return original(path, *args, **kwargs)
    module.os.stat = competing_write
    try:
        try:
            module.integrate(argparse.Namespace(root=str(ROOT_APP), project='provider-bootstrap-system-root-ci', apply=True))
        except module.Blocked as error:
            assert str(error) == 'CONFIG_CAS' and changed
        else:
            raise AssertionError('concurrent root config was not rejected')
    finally:
        module.os.stat = original
    assert json.loads((ROOT_APP/'.asa/log-collector.json').read_text()) == {'externalRootWriter': 'preserved'}
    assert not list((ROOT_APP/'.asa').glob('.provider-monitor-*.tmp'))
    print(json.dumps(dict(status='ROOT_CONFIG_CAS_REJECTED', competingWritePreserved=True)))


def root_integration(bootstrap, reader):
    """Canonical cdb root:reader metadata, root-primary process, no collector start."""
    assert os.geteuid() == os.getegid() == 0 and reader not in {0, *os.getgroups()}
    assert not ROOT_APP.exists() and not ROOT_APP.is_symlink()
    ROOT_APP.mkdir(mode=0o700)
    for path in (ROOT_APP/'.asa', ROOT_APP/'tools', ROOT_APP/'tools/deployment'):
        path.mkdir(mode=0o700)
    digest = hashlib.sha256()
    for name in ('collect_logs.py', 'collect-windows-logs.ps1', 'resource_sampling.py', 'memory_sampling.py', 'deployment/operational.py'):
        data = (FIXTURE/'tools'/name).read_bytes()
        private(ROOT_APP/'tools'/name, data, 0, 0)
        digest.update(name.encode()); digest.update(data)
    project = 'provider-bootstrap-system-root-ci'
    name = 'asa-log-collector-'+project+'.service'
    unit = Path('/etc/systemd/system')/name
    assert not unit.exists() and not unit.is_symlink()
    assert 'LoadState=not-found' in execute(['systemctl','show',name,'--property=LoadState'])
    settings = dict(format='asa-installation-v1', profile='production', project=project,
                    collectorMode='system-root', collectorReaderGid=reader)
    runner = dict(version=1, root=str(ROOT_APP), project=project, owner='root', mode='system-root', name=name,
                  scriptDigest=digest.hexdigest(), revision='cdb77ed6ac003ef2eb4635b9f55c4eb1c9a35bfa')
    config = dict(version=1, project=project, fileSources=[], preserved={'nested':['retained',123]})
    for file, value in [('installation.json',settings),('collector-runner.json',runner),('log-collector.json',config)]:
        private(ROOT_APP/'.asa'/file, value, 0, reader)
    private(ROOT_APP/'.asa/synthetic-history-marker', b'root history unchanged', 0, 0)
    text = ('[Unit]\nDescription=ASA Lab diagnostic collector\nAfter=network.target\n[Service]\n'
            f'User=root\nGroup={reader}\nWorkingDirectory={ROOT_APP}\n'
            f'ExecStart="/usr/bin/python3" "{ROOT_APP}/tools/collect_logs.py" --root "{ROOT_APP}"\n'
            'Restart=always\nRestartSec=20\nNoNewPrivileges=true\nUMask=0077\n[Install]\nWantedBy=multi-user.target\n')
    private(unit, text.encode(), 0, 0); unit.chmod(0o644)
    integration = bootstrap+['integrate','--root',str(ROOT_APP),'--project',project]
    path = ROOT_APP/'.asa/log-collector.json'
    protected = hashes([ROOT_APP/'.asa/installation.json',ROOT_APP/'.asa/collector-runner.json',ROOT_APP/'.asa/synthetic-history-marker',unit])
    original = hashes([path])
    assert execute(integration)['status'] == 'PLAN' and hashes([path]) == original
    assert execute(integration+['--apply'])['status'] == 'INTEGRATED'
    info = path.stat()
    assert (info.st_uid,info.st_gid,info.st_mode & 0o777,info.st_nlink) == (0,reader,0o600,1)
    assert json.loads(path.read_text()) == {**config,'fileSources':[dict(source='provider-monitor',path='/var/lib/asa-provider-monitor')]}
    applied = hashes([path])
    assert execute(integration+['--apply'])['status'] == 'NO_CHANGE' and hashes([path]) == applied
    assert hashes([Path(p)for p in protected]) == protected
    # Selected group does not come from root's primary/supplementary membership.
    assert Path('/var/lib/asa-provider-monitor').stat().st_gid == reader
    assert (ROOT_APP/'.asa').stat().st_gid == 0  # New temp needs fchown; no setgid inheritance.
    for bad_gid in (0, reader+1):
        private(ROOT_APP/'.asa/installation.json', {**settings,'collectorReaderGid':bad_gid}, 0, reader)
        before = hashes([path])
        assert execute(integration+['--apply'],expected=1)['status'] == 'BLOCKED'
        assert hashes([path]) == before
    forbidden = set()
    try:
        forbidden.add(grp.getgrnam('docker').gr_gid)
    except KeyError:
        pass
    try:
        forbidden.add(Path('/var/run/docker.sock').stat().st_gid)
    except FileNotFoundError:
        pass
    for bad_gid in forbidden:
        private(ROOT_APP/'.asa/installation.json', {**settings,'collectorReaderGid':bad_gid}, 0, reader)
        before = hashes([path])
        assert execute(integration+['--apply'],expected=1)['code'] == 'ROOT_READER_GROUP'
        assert hashes([path]) == before
    private(ROOT_APP/'.asa/installation.json', settings, 0, reader)
    os.chown(path,0,0)
    assert execute(integration+['--apply'],expected=1)['code'] == 'FILE_GROUP'
    os.chown(path,0,reader)  # Only the fresh synthetic fixture is restored between challenges.
    for foreign in (text.replace('User=root','User=nobody'), text.replace('Group='+str(reader),'Group=0'),
                    text.replace('Group='+str(reader)+'\n','')):
        private(unit,foreign.encode(),0,0); unit.chmod(0o644)
        before=hashes([path])
        assert execute(integration+['--apply'],expected=1)['code']=='COLLECTOR_UNIT'
        assert hashes([path])==before
    private(unit,text.encode(),0,0); unit.chmod(0o644)
    dropin=unit.with_name(name+'.d'); dropin.mkdir(mode=0o700)
    assert execute(integration+['--apply'],expected=1)['code']=='COLLECTOR_DROPINS'
    dropin.rmdir()
    private(path,config,0,reader)
    assert execute(['/usr/bin/python3','-I','-B',str(Path(__file__)),'--root-cas-fixture'])['status']=='ROOT_CONFIG_CAS_REJECTED'
    assert 'MainPID=0' in execute(['systemctl','show',name,'--property=MainPID'])
    return dict(rootPrimaryGroup=0,selectedReaderGroup=reader,rootMetadataGroupPreserved=True,
                rootPlanApplyRepeat=True,rootUnitGroupStrict=True,rootCasCompetitorPreserved=True,
                syntheticRootCollectorStarted=False)


def rehearse(owner_name):
    assert os.environ.get('GITHUB_ACTIONS') == 'true' and os.environ.get('ASA_EPHEMERAL_BOOTSTRAP_CI') == '1'
    assert os.geteuid() == 0 and Path('/run/systemd/system').is_dir()
    assert not any(p.exists() or p.is_symlink() for p in (Path('/var/lib/asa-provider-monitor-runtime'), Path('/etc/asa-provider-monitor'), Path('/var/lib/asa-provider-monitor'), STATE, APP, ROOT_APP))
    owner = pwd.getpwnam(owner_name)
    assert owner.pw_uid and owner.pw_gid
    parents = [Path('/'), Path('/opt'), Path('/etc'), Path('/etc/systemd'), Path('/etc/systemd/system'), Path('/var'), Path('/var/lib'), ROOT, ROOT/'scripts', ROOT/'templates', ROOT/'templates/provider-monitor']
    print(json.dumps(dict(kind='EPHEMERAL_PARENT_TRUST_INVENTORY', parents=[dict(path=str(p), uid=p.stat().st_uid, gid=p.stat().st_gid,
                     mode=oct(p.stat().st_mode & 0o7777)) for p in parents])), flush=True)
    for name in NAMES+('asa-provider-bootstrap-ci-mock.service',):
        properties = execute(['systemctl', 'show', name, '--property=LoadState,FragmentPath'])
        assert 'LoadState=not-found' in properties and 'FragmentPath=\n' in properties+'\n'
    execute(['systemd-run', '--unit=asa-provider-bootstrap-ci-mock', '--property=User=root', '--property=UMask=0077',
             '/usr/bin/python3', '-I', '-B', str(Path(__file__)), '--mock'])
    until = time.monotonic()+10
    while not (STATE/'origins.json').exists() and time.monotonic() < until:
        time.sleep(.1)
    origins = json.loads((STATE/'origins.json').read_text())
    bootstrap = ['/usr/bin/python3', '-I', '-B', str(ROOT/'scripts/bootstrap-provider-monitor.py')]
    setup = bootstrap+['install', '--reader-gid', str(owner.pw_gid), '--ollama-origin', origins['ollama'], '--comfyui-origin', origins['comfyui']]
    first = execute(setup)
    assert first['status'] == 'PLAN' and first['writes'] is False
    assert not any(p.exists() for p in (Path('/var/lib/asa-provider-monitor-runtime'), Path('/etc/asa-provider-monitor'), Path('/var/lib/asa-provider-monitor')))
    # Actual automatic three-file HTTPS GitHub path, not monkeypatched/offline.
    applied = execute(setup+['--apply', '--activate'])
    assert applied['status'] == 'INSTALLED'
    files = [p for base in ('/var/lib/asa-provider-monitor-runtime', '/etc/asa-provider-monitor') for p in Path(base).rglob('*') if p.is_file()]
    files += [Path('/etc/systemd/system')/n for n in NAMES]
    before = hashes(files)
    second = execute(setup+['--apply', '--activate'])
    assert second['status'] == 'NO_CHANGE' and hashes(files) == before
    assert second['timerActivationChanged'] is False
    # Explicit offline path also verifies the exact installed immutable closure without replacement.
    assert execute(setup+['--offline-bundle', '/var/lib/asa-provider-monitor-runtime/app-source/tools', '--apply'])['status'] == 'NO_CHANGE'
    exporter = ['/usr/bin/python3', '-I', '-B', '/var/lib/asa-provider-monitor-runtime/provider_health_exporter.py', 'check', '--config', '/etc/asa-provider-monitor/config.json']
    assert execute(exporter)['status'] == 'AVAILABLE'
    initial = json.loads(Path('/var/lib/asa-provider-monitor/latest.json').read_text())
    deadline = time.monotonic()+100
    while time.monotonic() < deadline:
        latest = json.loads(Path('/var/lib/asa-provider-monitor/latest.json').read_text())
        if latest['sampleId'] != initial['sampleId']:
            break
        time.sleep(1)
    assert latest['sampleId'] != initial['sampleId'], 'real timer did not create a second sample'
    assert execute(exporter)['status'] == 'AVAILABLE'
    config, unit_name = fixture(owner)
    integration = ['setpriv', '--reuid='+str(owner.pw_uid), '--regid='+str(owner.pw_gid), '--init-groups', *bootstrap,
                   'integrate', '--root', str(APP), '--project', 'provider-bootstrap-ci']
    config_path = APP/'.asa/log-collector.json'
    preserved = hashes([APP/'.asa/installation.json', APP/'.asa/collector-runner.json', APP/'.asa/synthetic-history-marker'])
    original_config = config_path.read_bytes()
    assert execute(integration)['status'] == 'PLAN' and config_path.read_bytes() == original_config
    assert execute(integration+['--apply'])['status'] == 'INTEGRATED'
    expected = {**config, 'fileSources': config['fileSources']+[dict(source='provider-monitor', path='/var/lib/asa-provider-monitor')]}
    assert json.loads(config_path.read_text()) == expected
    integrated = hashes([config_path])
    repeat = execute(integration+['--apply'])
    assert repeat['status'] == 'NO_CHANGE' and repeat['restartRequired'] is None and hashes([config_path]) == integrated
    assert hashes([Path(p) for p in preserved]) == preserved
    # Meaningful negative: root must not maintain a USER config, and duplicate source cannot be silently rewritten.
    assert execute(bootstrap+['integrate', '--root', str(APP), '--project', 'provider-bootstrap-ci', '--apply'], expected=1)['status'] == 'BLOCKED'
    conflict = {**config, 'fileSources': [dict(source='provider-monitor', path='/var/lib/foreign')]}
    private(config_path, conflict, owner.pw_uid, owner.pw_gid)
    conflict_raw = config_path.read_bytes()
    assert execute(integration+['--apply'], expected=1)['code'] == 'FILE_SOURCE_COLLISION'
    assert config_path.read_bytes() == conflict_raw
    private(config_path, config, owner.pw_uid, owner.pw_gid)
    cas = execute(['setpriv', '--reuid='+str(owner.pw_uid), '--regid='+str(owner.pw_gid), '--init-groups',
                   '/usr/bin/python3', '-I', '-B', str(Path(__file__)), '--cas-fixture'])
    assert cas['status'] == 'CONFIG_CAS_REJECTED'
    root_result = root_integration(bootstrap, owner.pw_gid)
    # A synthetic collector process is not started: this checks actual pinned 2a pure consumer functions only.
    os.environ['ASA_PROVIDER_TEST_SOURCE'] = str(FIXTURE)
    spec = importlib.util.spec_from_file_location('consumer_contracts', ROOT/'tests/test_provider_health_exporter.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    import unittest
    consumer = module.collector_functions(unittest.TestCase())
    raw_lines = Path('/var/lib/asa-provider-monitor/health.log').read_text().splitlines()
    assert len(raw_lines) >= 2
    events = []
    for raw in raw_lines:
        value = json.loads(raw)
        at, basis = consumer.file_timestamp(raw, '2000-01-01T00:00:00.000Z')
        event = consumer.normalize('provider-monitor', raw, at, value['sampleId'])
        duplicate = consumer.normalize('provider-monitor', raw, at, value['sampleId'])
        assert basis == 'event' and event['time'] == value['checkedAt'] and event['level'] == 'info'
        assert event['id'] == duplicate['id']
        events.append(event['id'])
    assert len(set(events)) == len(events)
    assert Path('/var/lib/asa-provider-monitor/latest.json').read_bytes() == (raw_lines[-1]+'\n').encode()
    execute(['systemctl', 'disable', '--now', NAMES[1]])
    execute(['systemctl', 'stop', NAMES[0], 'asa-provider-bootstrap-ci-mock.service'])
    summary = dict(status='EPHEMERAL_LOGGING_COMPONENT_PASS', actualSystemd=True, mockProvidersOnly=True,
                   automaticGitRawDownloads=3, secondApplyBytesAndInodesUnchanged=True, offlineRepeatVerified=True,
                   ownerIntegrationPlanApplyRepeat=True, rootOnUserAndSourceCollisionRejected=True,
                   configCasCompetingWritePreserved=True,
                   syntheticHistoryUnchanged=True, actualPinnedConsumerEventTime=True, deterministicDuplicateId=True,
                   systemRootIntegration=root_result,
                   timerSamples=len(raw_lines), fullAsaInstallationTest=False, realGpuGeneration=False)
    print(json.dumps(summary, sort_keys=True))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--mock', action='store_true')
    parser.add_argument('--cas-fixture', action='store_true')
    parser.add_argument('--root-cas-fixture', action='store_true')
    parser.add_argument('--owner')
    args = parser.parse_args()
    mock_server() if args.mock else cas_fixture() if args.cas_fixture else root_cas_fixture() if args.root_cas_fixture else rehearse(args.owner)
