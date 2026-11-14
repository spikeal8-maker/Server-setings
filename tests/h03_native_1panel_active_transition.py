#!/usr/bin/env python3
"""Check the real active-schedule transition on H03 with fresh isolated 1Panel.

The only host dependencies are the trusted 1Panel binaries, bubblewrap, a
verified Supervisor package, Python, Git, and read-only system account files.
The separate panel uses new SQLite state and a generated localhost API key.
The application, legacy systemd, and settings-readiness edges are fixtures.
"""

INNER = r"""
import hashlib,ipaddress,json,os,pathlib,secrets,shutil,socket,sqlite3,stat,subprocess,sys,time,urllib.request
p=pathlib.Path
os.environ['PATH']='/work/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
assert len(list(p('/proc').glob('[0-9]*'))) <= 3
assert not p('/run/docker.sock').exists() and not p('/var/run/docker.sock').exists()
assert not p('/opt/1panel').exists() and not p('/etc/1panel/agent.sock').exists()
subprocess.run(['/usr/bin/ip','link','set','lo','up'],check=True)
assert socket.if_nameindex()==[(1,'lo')]
for d in ('/etc/1panel','/etc/supervisor','/opt/1panel/db','/opt/1panel/log','/opt/1panel/tools/supervisord/supervisor.d','/srv/asa-lab'):
    p(d).mkdir(parents=True,exist_ok=True)
if not p('/var/run').exists():p('/var/run').symlink_to('/run',target_is_directory=True)
for n in ('1panel-core','1panel-agent'):
    assert p('/usr/local/bin',n).is_file()
key=secrets.token_hex(24)
p('/usr/local/bin/1pctl').write_text('BASE_DIR=/opt\nORIGINAL_PORT=19192\nORIGINAL_VERSION=v2.3.2\nORIGINAL_USERNAME=asa-fixture-admin\nORIGINAL_PASSWORD='+secrets.token_hex(18)+'\nORIGINAL_ENTRANCE=asa-fixture-entry\nLANGUAGE=en\nPANEL_EDITION=intl\n')
def start(name):
    log=open('/work/'+name+'.log','wb')
    try:return subprocess.Popen(['/usr/local/bin/'+name],stdout=log,stderr=log)
    finally:log.close()
def stop(proc):
    if proc.poll() is None:
        proc.terminate()
        try:proc.wait(5)
        except subprocess.TimeoutExpired:proc.kill();proc.wait(5)
def wait(ready, timeout=22):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        if ready():return True
        time.sleep(.15)
    return False
def http(endpoint, payload=None):
    headers={}
    ts=str(int(time.time()))
    headers.update({'1Panel-Timestamp':ts,'1Panel-Token':hashlib.md5(('1panel'+key+ts).encode()).hexdigest()})
    data=None
    if payload is not None:data=json.dumps(payload).encode();headers['Content-Type']='application/json'
    req=urllib.request.Request('http://127.0.0.1:19192/api/v2'+endpoint,data=data,headers=headers)
    with urllib.request.urlopen(req,timeout=3) as conn:return json.load(conn)
def web_alive():
    with socket.socket() as c:return c.connect_ex(('127.0.0.1',19192))==0
os.environ['PATH']='/work/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
p('/work/bin').mkdir()
for command in ('timedatectl','systemctl','hostnamectl','docker','iptables','nft','ufw','firewall-cmd','dmidecode','lsb_release','snap','virsh','virt-what','service','rc-service','journalctl','loginctl','hostname'):
    f=p('/work/bin',command); f.write_text('#!/bin/sh\nprintf "%s\\n" "'+command+' $*" >> /work/external.log\nexit 1\n'); f.chmod(0o700)
agent=None
core=None

def test_native():
    global agent
    os.umask(0o077)
    import importlib.util,datetime
    def load(name,path):
        spec=importlib.util.spec_from_file_location(name,path)
        m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m
    def touch(f,content=None,mode=0o600):
        f=p(f);f.parent.mkdir(parents=True,exist_ok=True)
        if isinstance(content,bytes):f.write_bytes(content)
        elif content is not None:f.write_text(content)
        else:f.touch()
        f.chmod(mode)
        return f
    for path in ('/run/lock','/etc/server-settings','/opt/1panel/tools/supervisord/log','/var/backups/asa-school-nightly',
       '/var/backups/asa-school-nightly/staging','/var/backups/asa-school-nightly/archives',
       '/var/backups/server-settings','/var/backups/server-settings/school-nightly-settings','/var/lib/server-settings',
       '/srv/Server-setings/scripts','/srv/asa-lab/.asa'):
        pp=p(path);pp.mkdir(parents=True,exist_ok=True);pp.chmod(0o700)
    touch('/etc/server-settings/mcp.env','PANEL_HOST=http://127.0.0.1:19192\nPANEL_ACCESS_TOKEN='+key+'\n')
    touch('/work/bin/systemctl','#!/bin/sh\ncase "$1 $2" in\n"is-enabled supervisord.service"|"is-enabled supervisor.service") echo enabled;;\n"is-active supervisord.service"|"is-active supervisor.service") echo active;;\n"restart supervisord.service"|"restart supervisor.service") exit 0;;\n*) exit 1;; esac\n',0o700)
    for name in ('supervisord','supervisorctl'):
        touch('/usr/local/bin/'+name,'#!/bin/sh\nexport PYTHONPATH=/vendor\nexec /usr/bin/python3 -m supervisor.'+name+' "$@"\n',0o755)
    conf=touch('/etc/supervisor/supervisord.conf','[unix_http_server]\nfile=/var/run/supervisor.sock\nchmod=0700\n'
      '[supervisord]\nnodaemon=true\nuser=root\nlogfile=/work/supervisor.log\npidfile=/run/supervisor.pid\n'
      '[rpcinterface:supervisor]\nsupervisor.rpcinterface_factory=supervisor.rpcinterface:make_main_rpcinterface\n'
      '[supervisorctl]\nserverurl=unix:///var/run/supervisor.sock\n[include]\nfiles=/opt/1panel/tools/supervisord/supervisor.d/*.ini\n',0o644)
    sv=subprocess.Popen(['/usr/local/bin/supervisord','-c',str(conf)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        assert wait(lambda:p('/run/supervisor.sock').is_socket())
        res=http('/hosts/tool/init',{'type':'supervisord','configPath':str(conf),'serviceName':'supervisor'})
        assert res.get('code') in (0,200),res
        sys.dont_write_bytecode=True
        new=load('candidate','/work/source/school-1panel-backup-schedules.py')
        harness=load('transition','/work/source/school-1panel-active-settings-transition.py')
        harness.SUPERVISOR_CTL=p('/usr/local/bin/supervisorctl')
        # The isolated agent is supervised by this harness, so supply the
        # stdout of that exact peer instead of a host systemd invocation.
        def startup_log(_,epoch):
            assert epoch['pid']==agent.pid
            return p('/work/1panel-agent.log').read_text(errors='replace')
        harness.agent_startup_log=startup_log
        new.PANEL_BINARY_ALIAS=p('/usr/bin/1panel')
        oldsha='4b391bbd4b4589212acffc25a4239b79d15e5830176300610dfd1f7fc3ef596b'
        oldpath=new.BASE/'releases'/oldsha[:16]/harness.NAME
        oldsource=p('/work/old-wrapper.py').read_bytes()
        assert hashlib.sha256(oldsource).hexdigest()==oldsha
        touch(oldpath,oldsource)
        old=load('old_schedule',oldpath)
        prior_id='4ccfa3f7ffe34b358523a02872e03f53'
        next_id='6a78d63fe3b741adba75c253f8d0750a'
        touch('/srv/asa-lab/.asa/installation.json',json.dumps({'instanceId':new.INSTANCE,'project':new.PROJECT}))
        for f in (old.LOCK,old.DB_LOCK,harness.shared.CONFIG_BACKUP_LOCK,old.APP/'.asa/operation.lock'):touch(f)
        for n in ('dispatch','runs','output'):
            (old.STATE/n).mkdir(parents=True,exist_ok=True)
        for f in ('school-nightly-database-backup.py','school_yandex_app_folder.py','cloud_storage.py'):
            touch('/srv/Server-setings/scripts/'+f,p('/work/source',f).read_bytes())
        for f in old.SETTINGS_FILES:
            touch(old.settings_release()/f,p('/work/old',f).read_bytes())
        touch(old.settings_release()/'release.json',json.dumps({'format':'asa-school-settings-release-v1','files':old.SETTINGS_FILES}))
        assert old.verify_runners() is None
        os.umask(0o077)
        state={'format':old.FORMAT,'instance':old.INSTANCE,'project':old.PROJECT,'generation':prior_id,
          'programSha256':oldsha,'programPath':str(oldpath),'createdAt':(datetime.datetime.now(datetime.timezone.utc)-datetime.timedelta(hours=1)).isoformat(),
          'phase':'ACTIVE','owner':'1panel','originalOwner':'legacy','legacy':{},'clockTimeZone':'Europe/Moscow',
          'jobs':{kind:{'id':n,'processConfigured':True} for kind,n in harness.JOB_IDS.items()}}
        old.atomic(old.STATE/'state.json',state)
        def panel_err(result):
            assert result.get('code') in (0,200),result;return result.get('data')
        class Legacy:
            def idle(self):pass
            def disabled(self,_):return True
        for module in (old,new):
            module.check_host=lambda:None
            module.Legacy=Legacy
        # Test script updates real native API state and Supervisor, not a faux Panel. App inventory
        # and settings-runner readiness are covered by the existing isolated ASA+Settings test.
        harness.shared.application=lambda _: ['fixture-with-no-docker-access']
        realcommand=new.command
        def command(args,*a,**kw):
            if ' --check' in (' '.join(map(str,args))) and str(new.runner('settings')) in args:
                return json.dumps({'status':'CONFIG_SCHEDULE_READONLY_CHECK_PASS',
                  'releaseSha256':new.digest(new.canonical(new.SETTINGS_FILES)),'catalogsReady':True,'changes':False})
            return realcommand(args,*a,**kw)
        new.command=command
        panel=new.Panel()
        panel.prerequisite()
        native_dir=p('/opt/1panel/tools/supervisord/supervisor.d')
        for kind in old.KINDS:
            touch(native_dir/(old.program(kind)+'.ini'),old.process_config(state,kind),0o600)
            panel_err(http('/cronjobs',old.cron_payload(state,kind)))
        assert p('/work/1panel-agent.log').read_text(errors='replace').count('start cronjob entryID:')==2
        collector=old.COLLECTOR
        touch(native_dir/(collector+'.ini'),'[program:'+collector+']\ncommand=/usr/bin/sleep 180\nautostart=true\nnumprocs=1\nprocess_name=%(program_name)s_%(process_num)02d\nstdout_logfile=/work/collector.log\nstderr_logfile=/work/collector.err\n',0o600)
        def ctl(*a,check=True):
            return subprocess.run(['/usr/local/bin/supervisorctl','-c',str(conf),*a],capture_output=True,text=True,timeout=8,check=check).stdout.strip()
        ctl('update')
        assert wait(lambda:'RUNNING' in ctl('status',collector+':'+collector+'_00',check=False))
        collector_pid=ctl('pid',collector+':'+collector+'_00')
        assert collector_pid.isdecimal() and int(collector_pid)
        for kind in old.KINDS:
            path=p('/opt/1panel/tools/supervisord/log',old.program(kind)+'.out.log')
            with path.open('ab') as f:f.write(b'pre-existing fixture log\n')

        # Preserve native Cron history that predates the switch. Fixture hostname
        # prevents the old school CLI from starting any actual backup.
        prior_records={}
        for kind,n in harness.JOB_IDS.items():
            panel_err(http('/cronjobs/handle',{'id':n}))
            assert wait(lambda:bool(panel.records(n,state['createdAt'])))
            assert wait(lambda:all(row['status'] not in ('Running','Waiting') for row in panel.records(n,state['createdAt'])))
            prior_records[kind]=panel.records(n,state['createdAt'])
        assert all(prior_records.values())

        raw=old.read(old.STATE/'state.json',private=True)
        trans=harness.Transition(old,new,panel,oldsha,new.source_release()[1],prior_id,next_id)
        # Real lifecycle calls go through native 1Panel after checking its initial state.
        with harness.shared.existing_idle_locks(old):
            before=trans.plan()
            assert hashlib.sha256(raw).hexdigest()==before['oldStateSha256'] and before['changes'] is False
            out=trans.apply(before['oldStateSha256'])
        print('REAL_NATIVE_SWITCH',json.dumps(out,sort_keys=True),flush=True)
        readonly=trans.reconcile(before['oldStateSha256'])
        assert readonly['supervisorMatchesDisk'] and readonly['generation']==next_id
        assert ctl('pid',collector+':'+collector+'_00')==collector_pid
        with harness.shared.existing_idle_locks(old):
            rollback=trans.reconcile(before['oldStateSha256'],apply=True)
        print('REAL_NATIVE_ROLLBACK',json.dumps(rollback,sort_keys=True),flush=True)
        assert old.load_state()==state
        assert ctl('pid',collector+':'+collector+'_00')==collector_pid
        assert all(panel.records(n,state['createdAt'])==prior_records[kind] for kind,n in harness.JOB_IDS.items())
        request=panel.request
        for status,next_id in (
            ('Disable','37e35e39aef946db9581bd853ea4686e'),
            ('Enable','eea4a3c4716d4e38b1ffd1499cbd91a7'),
        ):
            candidate=harness.Transition(old,new,panel,oldsha,new.source_release()[1],prior_id,next_id)
            with harness.shared.existing_idle_locks(old):
                plan=candidate.plan()
            sha=plan['oldStateSha256']
            once=[True]
            def lose_response(endpoint,payload=None,**kw):
                if once[0] and endpoint=='/cronjobs/status' and payload=={'id':1,'status':status}:
                    once[0]=False
                    if status=='Enable':
                        # In this private SQLite DB only, force persistence to fail
                        # after 1Panel has added its in-memory entry.
                        with sqlite3.connect('/opt/1panel/db/agent.db',timeout=5) as db:
                            db.execute("""CREATE TRIGGER reject_fixture_enable BEFORE UPDATE ON cronjobs
                                WHEN NEW.id = 1 AND NEW.status = 'Enable'
                                BEGIN SELECT RAISE(ABORT, 'fixture rejects enable'); END;""")
                        try:
                            request(endpoint,payload,**kw)
                        except new.ScheduleError:
                            with sqlite3.connect('/opt/1panel/db/agent.db',timeout=5) as db:
                                assert db.execute('SELECT status FROM cronjobs WHERE id=1').fetchone()==('Disable',)
                            assert 'fixture rejects enable' in p('/work/1panel-agent.log').read_text(errors='replace')
                            raise
                        finally:
                            with sqlite3.connect('/opt/1panel/db/agent.db',timeout=5) as db:
                                db.execute('DROP TRIGGER reject_fixture_enable')
                        raise AssertionError('native status unexpectedly persisted')
                    request(endpoint,payload,**kw)
                    raise new.ScheduleError('PANEL_REQUEST_OUTCOME_UNKNOWN')
                return request(endpoint,payload,**kw)
            panel.request=lose_response
            try:
                with harness.shared.existing_idle_locks(old):
                    try:candidate.apply(sha)
                    except harness.shared.RepairStop as e:assert str(e)=='CRON_RUNTIME_STATUS_UNKNOWN',e
                    else:raise AssertionError('lost response was accepted')
            finally:panel.request=request
            with harness.shared.existing_idle_locks(old):
                observed=candidate.reconcile(sha)
                assert observed['nativeCronRuntimeStatusUnknown'] and observed['phase']=='ACTIVE_SETTINGS_TRANSITION'
                try:candidate.reconcile(sha,apply=True)
                except harness.shared.RepairStop as e:assert str(e)=='CRON_RUNTIME_REQUIRES_MANUAL_RECONCILIATION'
                else:raise AssertionError('unknown runtime allowed rollback')
                assert candidate.arm_recovery(sha)['gateClosed']
                try:candidate.reconcile(sha,apply=True,recover=True)
                except harness.shared.RepairStop as e:assert str(e)=='PANEL_AGENT_RESTART_NOT_ATTESTED'
                else:raise AssertionError('recovery accepted the original agent')
            assert ctl('pid',collector+':'+collector+'_00')==collector_pid
            stop(agent)
            p('/etc/1panel/agent.sock').unlink(missing_ok=True)
            agent=start('1panel-agent')
            assert wait(lambda:p('/etc/1panel/agent.sock').is_socket())
            with harness.shared.existing_idle_locks(old):
                assert candidate.reconcile(sha,apply=True,recover=True)['generation']==prior_id
            assert old.load_state()==state
            assert {row['id']:row['status'] for row in panel.jobs()}=={1:'Enable',2:'Enable'}
            assert ctl('pid',collector+':'+collector+'_00')==collector_pid
            assert all(panel.records(n,state['createdAt'])==prior_records[kind] for kind,n in harness.JOB_IDS.items())
            assert trans.history.is_dir() and candidate.history.is_dir()
            print('REAL_NATIVE_' + status.upper() + '_LOST_RESPONSE_RECOVERED',flush=True)
        # Run a quick native schedule with a separate no-cloud synthetic shell job.
        synthetic=old.cron_payload(state,'settings') | {'name':'ASA-H03-clock-proof','spec':'@every 2s','user':'',
           'script':'#!/bin/bash\nprintf "clock-triggered\\n" >> /work/cron-clock-proof.log\n'}
        panel_err(http('/cronjobs',synthetic))
        assert wait(lambda:p('/work/cron-clock-proof.log').is_file(),timeout=11),panel.records(3,state['createdAt'])
        assert 'clock-triggered' in p('/work/cron-clock-proof.log').read_text()
        assert candidate.reconcile(sha)['phase']=='ACTIVE'
        print('REAL_NATIVE_CRON_LOST_RESPONSE_DURABLE_GATE_PASS',flush=True)
        print('REAL_NATIVE_CRON_RESTART_RESTORES_PERSISTED_CLOCKS_AND_EXECUTES_SYNTHETIC_SCHEDULE_PASS',flush=True)
    finally:stop(sv)

try:
    core=start('1panel-core')
    print('secret created',wait(lambda:p('/etc/1panel/.1panel').is_file() or core.poll() is not None),flush=True)
    print('names from core', [x.name for x in p('/etc/1panel').iterdir()],flush=True)
    agent=start('1panel-agent')
    print('agent ready?',wait(lambda:p('/etc/1panel/agent.sock').is_socket() or agent.poll() is not None),'agent exit',agent.poll(),flush=True)
    print('core ready?',wait(lambda:web_alive() or core.poll() is not None),'core exit',core.poll(),flush=True)
    print('port',web_alive(),flush=True)
    if not web_alive():raise SystemExit(2)
    stop(core)
    with sqlite3.connect('/opt/1panel/db/core.db') as db:
        for k,v in {'ApiKey':key,'ApiInterfaceStatus':'Enable','IpWhiteList':'127.0.0.1','ApiKeyValidityTime':'120'}.items():
            r=db.execute('UPDATE settings SET value = ? WHERE key = ?',(v,k))
            assert r.rowcount==1,k
    core=start('1panel-core')
    assert wait(web_alive)
    test_native()
finally:
    if core is not None:stop(core)
    if agent is not None:stop(agent)
"""

import argparse
import hashlib
import os
import pathlib
import socket
import subprocess
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
OLD_REVISION = '2ecb89e969ff96f8fb68d9429552cb576542bcb4'
SUPERVISOR_SHA256 = 'd6d9c70d73da4e5ad2e3a2355e96827545a4edf054de548164b0689c3b5f1f01'
BINARY_SHA256 = {
    '/usr/local/bin/1panel-core': '7926e212d9e966eccb3a56a649f1f7903f5b21092aedafcc442172abf720bf9b',
    '/usr/local/bin/1panel-agent': 'e12321d3a0d796310066aa77ff9dc916b2b69c4f3efa34716b51c2f02595a97d',
}
SETTINGS_FILES = ('school-nightly-settings-backup.py', 'school_settings_snapshot.py',
                  'backup-settings.sh', 'school-settings-cloud-roundtrip.py',
                  'school_yandex_app_folder.py', 'cloud_storage.py')
WRAPPER = 'school-1panel-backup-schedules.py'


def checksum(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def checkout_files(destination, version, files):
    destination.mkdir(mode=0o700)
    for name in files:
        raw = subprocess.check_output(['/usr/bin/git', '-C', str(ROOT), 'show', version + ':scripts/' + name],
                                      timeout=10)
        (destination / name).write_bytes(raw)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--supervisor-deb', type=pathlib.Path, required=True)
    args = parser.parse_args()
    assert os.geteuid() == 0 and socket.gethostname() == 'admins-MS-7D22', 'Requires H03 root'
    assert checksum(args.supervisor_deb) == SUPERVISOR_SHA256
    for path, expected in BINARY_SHA256.items():
        assert checksum(pathlib.Path(path)) == expected
    # Record only process IDs. No contents of standing configuration, panel DB,
    # tokens, sites, or application data are read, mounted, or copied.
    def standing_pids():
        return [subprocess.check_output(['/usr/bin/systemctl', 'show', name, '-p', 'MainPID', '--value'],
                                        text=True, timeout=8).strip()
                for name in ('1panel-core', '1panel-agent')]
    before = standing_pids()
    with tempfile.TemporaryDirectory(dir='/opt', prefix='asa-native-panel-acceptance-') as t:
        box = pathlib.Path(t)
        subprocess.run(['/usr/bin/dpkg-deb', '-x', str(args.supervisor_deb), str(box/'vendor')],
                       check=True, timeout=8)
        checkout_files(box / 'source', 'HEAD',
                       (WRAPPER, 'school-1panel-staged-repair.py', 'school-1panel-active-settings-transition.py',
                        'school-nightly-database-backup.py', *SETTINGS_FILES))
        checkout_files(box / 'old', OLD_REVISION, (WRAPPER, *SETTINGS_FILES))
        (box / 'old-wrapper.py').write_bytes((box / 'old' / WRAPPER).read_bytes())
        (box / 'localbin').mkdir(mode=0o700)
        (box / 'localbin' / '1pctl').touch(mode=0o600)
        (box / 'inside.py').write_text(INNER, encoding='utf-8')
        args = ['/usr/bin/bwrap', '--unshare-pid', '--unshare-net', '--unshare-ipc', '--unshare-uts',
                '--hostname', 'asa-native-h03-fixture', '--die-with-parent', '--new-session',
                '--ro-bind', '/usr', '/usr', '--ro-bind', '/lib', '/lib', '--ro-bind', '/lib64', '/lib64',
                '--symlink', 'usr/bin', '/bin', '--symlink', 'usr/sbin', '/sbin', '--proc', '/proc',
                '--dev', '/dev', '--tmpfs', '/etc', '--tmpfs', '/opt', '--tmpfs', '/run', '--tmpfs', '/var',
                '--tmpfs', '/tmp', '--tmpfs', '/srv', '--bind', t, '/work',
                '--ro-bind', str(box/'vendor/usr/lib/python3/dist-packages'), '/vendor',
                '--bind', str(box/'localbin'), '/usr/local/bin']
        for path in (*BINARY_SHA256, '/etc/passwd', '/etc/group', '/etc/nsswitch.conf', '/etc/localtime',
                     '/etc/ld.so.cache'):
            args.extend(('--ro-bind', path, path))
        args.extend(('--chdir', '/work', '/usr/bin/python3', '-B', '/work/inside.py'))
        result = subprocess.run(args, check=False, timeout=120)
        assert result.returncode == 0, f'Isolated 1Panel rehearsal exited {result.returncode}'
    assert standing_pids() == before
    print('H03_STANDING_PANEL_PIDS_UNCHANGED', flush=True)


if __name__ == '__main__':
    main()
