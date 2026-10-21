#!/usr/bin/env python3
"""One owner-authorized clean test reinstall. Default is a read-only plan."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request
import uuid

ROOT = Path('/srv/asa-lab')
STAGE = Path('/srv/asa-reinstall-20261021')
SOURCE = STAGE / 'fresh-source'
SNAPSHOT = Path('/srv/asa-restore-snapshots/20261017T231505Z-65b68074')
PROJECT = 'asa-lab-dev'
REVISION = '6e80e3f3e87a8f815ac6b254fd0b3ffa942db2d3'
MANIFEST_SHA = '8be098e3c37cd43fb704e7f92f300574bb685445d92697223ba0b158ac2e8a01'
VOLUMES = {'postgres-data': PROJECT+'_postgres-data', 'blocks-object-data': PROJECT+'_blocks-object-data'}
SERVICES = {'api', 'web', 'scratch', 'migration', 'minio-init', 'postgres', 'minio'}
RECEIPT = STAGE / 'clean-install-receipt.json'
BOOTSTRAP = ROOT / '.env.reinstall-bootstrap.json'


def require(condition, code):
    if not condition:
        raise RuntimeError(code)


def run(args, *, env=None, input_data=None, input_file=None, timeout=300):
    result = subprocess.run(args, input=input_data, capture_output=True, text=True,
                            stdin=input_file, env=env, timeout=timeout)
    # Never emit stderr/SQL/private interpolated configuration into a public log.
    require(result.returncode == 0, 'COMMAND_FAILED_'+Path(str(args[0])).name)
    return result.stdout.strip()


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix+'.tmp')
    with os.fdopen(os.open(temporary, os.O_WRONLY|os.O_CREAT|os.O_TRUNC, 0o600), 'w') as stream:
        json.dump(value, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def state():
    return json.loads(RECEIPT.read_text()) if RECEIPT.exists() else {}


def mark(phase, **values):
    data = {**state(), **values, 'phase': phase, 'updated_at_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
    write_json(RECEIPT, data)
    print(json.dumps({'phase': phase, 'revision': REVISION}), flush=True)


def inspect_objects(kind, names):
    if not names:
        return []
    return json.loads(run(['docker', kind, 'inspect', *names]))


def containers(project=None):
    args = ['docker', 'ps', '-aq']
    if project:
        args += ['--filter', 'label=com.docker.compose.project='+project]
    return inspect_objects('container', run(args).split())


def panel(endpoint, payload):
    gateway = inspect_objects('container', ['asa-1panel-mcp-gateway'])[0]
    environment = dict(item.split('=',1) for item in gateway['Config']['Env'] if '=' in item)
    key = environment['PANEL_ACCESS_TOKEN']
    stamp = str(int(time.time()))
    request = urllib.request.Request('http://127.0.0.1:19192/api/v2'+endpoint,
        data=json.dumps(payload).encode(), method='POST', headers={
            'Content-Type':'application/json', '1Panel-Timestamp':stamp,
            '1Panel-Token':hashlib.md5(('1panel'+key+stamp).encode()).hexdigest()})
    with urllib.request.urlopen(request, timeout=180) as response:
        document = json.load(response)
    require(document.get('code') in (0,200), 'PANEL_OPERATION_FAILED')
    return document.get('data')


def source_root():
    return SOURCE if SOURCE.exists() else ROOT


def configuration(root=None):
    from deployment.contracts import read_env
    return read_env((root or source_root())/'.env')


def compose(*args, bootstrap=False, input_data=None, input_file=None, env=None):
    files = [BOOTSTRAP] if bootstrap else [ROOT/'compose.yaml', ROOT/'compose.production.yaml']
    command = ['docker','compose','--project-directory',str(ROOT),'--env-file',str(ROOT/'.env'),'-p',PROJECT]
    for file in files:
        command += ['-f',str(file)]
    return run([*command,*args], input_data=input_data, input_file=input_file, env=env)


def sql(database, statement, *, bootstrap=True):
    require(database.replace('_','').isalnum(), 'UNSAFE_DATABASE_NAME')
    values = configuration(ROOT)
    return compose('exec','-T','postgres','psql','-XAt','-v','ON_ERROR_STOP=1',
        '-U',values['POSTGRES_USER'],'-d',database,bootstrap=bootstrap,input_data=statement+'\n')


def population(database, *, bootstrap=True):
    return json.loads(sql(database,"SELECT json_build_object('accounts',(SELECT count(*) FROM accounts),"
        "'classrooms',(SELECT count(*) FROM classrooms),'seats',(SELECT count(*) FROM classroom_student_seats),"
        "'projects',(SELECT count(*) FROM projects),'schema',(SELECT max(version) FROM schema_migrations));",bootstrap=bootstrap))


def check_expected_population(value, schema):
    value = {**value, 'schema': int(value['schema'])}
    require(value == {'accounts':90,'classrooms':31,'seats':418,'projects':1414,'schema':schema}, 'RESTORED_POPULATION_MISMATCH')


def guard_source():
    require(socket.gethostname() == 'admins-MS-7D22', 'WRONG_HOST')
    require(ROOT.resolve() == ROOT and STAGE.resolve() == STAGE and SNAPSHOT.resolve() == SNAPSHOT, 'SYMLINK_ROOT')
    require(hashlib.sha256((SNAPSHOT/'manifest.json').read_bytes()).hexdigest() == MANIFEST_SHA, 'SNAPSHOT_IDENTITY')
    root = source_root()
    require(run(['git','-C',str(root),'rev-parse','HEAD']) == REVISION, 'SOURCE_REVISION')
    require(not run(['git','-C',str(root),'status','--porcelain','--untracked-files=no']), 'SOURCE_MODIFIED')
    release = json.loads((STAGE/'release.json').read_text())
    require(release['revision'] == REVISION and release['schema'] == 234, 'RELEASE_IDENTITY')
    return release


def guard_old_objects(records):
    require(len(records) == 7, 'EXPECTED_SEVEN_OLD_CONTAINERS')
    require({r['Config']['Labels'].get('com.docker.compose.service') for r in records} == SERVICES, 'OLD_SERVICE_SET')
    for record in records:
        labels = record['Config']['Labels']
        require(labels.get('com.docker.compose.project') == PROJECT
            and labels.get('com.docker.compose.project.working_dir') == str(ROOT)
            and labels.get('com.docker.compose.project.config_files') == str(ROOT/'compose.yaml')+','+str(ROOT/'compose.production.yaml'), 'OLD_CONTAINER_PROVENANCE')
    for item in containers():
        if item['Config'].get('Labels',{}).get('com.docker.compose.project') == PROJECT:
            continue
        require(not any(Path(m['Source']).is_relative_to(ROOT) for m in item.get('Mounts',[])), 'FOREIGN_CONTAINER_USES_ROOT')
    for role,name in VOLUMES.items():
        value = inspect_objects('volume',[name])[0]
        require((value.get('Labels') or {}).get('com.docker.compose.project') == PROJECT
            and (value.get('Labels') or {}).get('com.docker.compose.volume') == role, 'OLD_VOLUME_PROVENANCE')
        attached = run(['docker','ps','-aq','--no-trunc','--filter','volume='+name]).split()
        require(set(attached) <= {r['Id'] for r in records}, 'FOREIGN_CONTAINER_USES_VOLUME')


class BootstrapAdapter:
    def __init__(self):
        self.env = configuration(ROOT)
    def identity(self):
        records = containers(PROJECT)
        require(len(records)==1, 'BOOTSTRAP_MUST_CONTAIN_ONLY_POSTGRES')
        record=records[0]
        labels=record['Config']['Labels']
        require(labels.get('com.docker.compose.service')=='postgres'
            and labels.get('com.docker.compose.project.working_dir')==str(ROOT)
            and labels.get('com.docker.compose.project.config_files')==str(BOOTSTRAP), 'BOOTSTRAP_PROVENANCE')
        require(any(m.get('Name')==VOLUMES['postgres-data'] and m['Destination']=='/var/lib/postgresql/data' for m in record['Mounts']), 'BOOTSTRAP_VOLUME')
        return {'postgres':{'id':record['Id']}}
    def compose(self,*args,**kwargs):
        kwargs.pop('capture',None)
        return compose(*args,bootstrap=True,**kwargs)


def migrate(database, release, *, existing=False):
    from deployment.contracts import read_env
    from urllib.parse import urlsplit,urlunsplit
    values=read_env(ROOT/'.env')
    parsed=urlsplit(values['MIGRATION_DATABASE_URL'])
    process_env=dict(os.environ)
    process_env.update(MIGRATION_DATABASE_URL=urlunsplit(parsed._replace(path='/'+database)),
        MIGRATION_EXPECT_DATABASE=database,MIGRATION_CONFIRM='APPLY:'+database)
    for action in ('--plan','--apply','--plan'):
        run(['docker','run','--rm','--network',PROJECT+'_database','--read-only','--cap-drop','ALL',
            '--security-opt','no-new-privileges:true','-e','MIGRATION_DATABASE_URL','-e','MIGRATION_EXPECT_DATABASE',
            '-e','MIGRATION_CONFIRM','--entrypoint','node',release['images']['api'],'tools/migrate.mjs',action],env=process_env)


def plan(release):
    from deployment.contracts import verify_backup
    manifest=verify_backup(SNAPSHOT)
    records=containers(PROJECT)
    guard_old_objects(records)
    print(json.dumps({'mode':'plan','root':str(ROOT),'project':PROJECT,'containers':len(records),
        'volumes':list(VOLUMES.values()),'snapshot_files':len(manifest['files']),'target_revision':release['revision'],
        'target_schema':234,'current_data':'discard; no fresh backup','preserve':['OS','1Panel','OpenResty','MCP']}))


def erase(release):
    from deployment.contracts import verify_backup
    verify_backup(SNAPSHOT)
    current=state()
    require(current.get('phase') in ('rehearsed','erasing'), 'REHEARSAL_REQUIRED')
    require(current.get('revision')==REVISION and current.get('schema')==234 and current.get('credential_config')=='passed','REHEARSAL_TARGET_MISMATCH')
    require(json.loads((STAGE/'images-receipt.json').read_text()).get('status')=='passed','IMAGES_REQUIRED')
    if current['phase']=='rehearsed':
        records=containers(PROJECT)
        guard_old_objects(records)
        mark('erasing',old_container_ids=[r['Id'] for r in records],preserved_container_ids=[r['Id'] for r in containers() if r['Config'].get('Labels',{}).get('com.docker.compose.project')!=PROJECT])
    else:
        require({r['Id'] for r in containers(PROJECT)} <= set(current['old_container_ids']), 'UNEXPECTED_CONTAINER_DURING_ERASE')
    if containers(PROJECT):
        panel('/containers/compose/operate',{'name':PROJECT,'path':str(ROOT/'compose.yaml')+','+str(ROOT/'compose.production.yaml'),
            'operation':'delete','withFile':False,'force':False})
    require(not containers(PROJECT),'OLD_CONTAINERS_REMAIN')
    for role,name in VOLUMES.items():
        if name not in run(['docker','volume','ls','-q']).split():continue
        value=inspect_objects('volume',[name])[0]
        require((value.get('Labels') or {}).get('com.docker.compose.project')==PROJECT
            and (value.get('Labels') or {}).get('com.docker.compose.volume')==role,'VOLUME_IDENTITY_CHANGED')
        require(not run(['docker','ps','-aq','--filter','volume='+name]),'VOLUME_STILL_ATTACHED')
        run(['docker','volume','rm',name])
    if SOURCE.exists():
        require(ROOT.resolve()==ROOT and ROOT.name=='asa-lab','ERASE_PATH')
        lock=ROOT/'.asa/operation.lock'
        target=SOURCE/'.asa/operation.lock'
        target.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        require(not target.exists(),'FRESH_LOCK_EXISTS')
        os.link(lock,target)  # Retain the locked inode across checkout replacement.
        if ROOT.exists():shutil.rmtree(ROOT)
        SOURCE.rename(ROOT)
    require(run(['git','-C',str(ROOT),'rev-parse','HEAD'])==REVISION,'NEW_CHECKOUT_MISSING')
    # New diagnostic storage contains no previous test logs or projects.
    store=ROOT/'.asa/diagnostics/store'
    store.mkdir(parents=True,mode=0o750,exist_ok=True)
    os.chown(store,1000,1000)
    os.chmod(ROOT,0o755)
    os.chown(ROOT,1000,1000)
    for path in ROOT.rglob('*'):
        os.chown(path,1000,1000,follow_symlinks=False)
    mark('erased',current_data_preserved=False)


def bootstrap(release):
    require(state().get('phase')=='erased','ERASE_REQUIRED')
    resolved=json.loads(compose('config','--format','json'))
    boot={k:v for k,v in resolved.items() if k in ('name','networks','volumes')}
    boot['services']={'postgres':resolved['services']['postgres']}
    write_json(BOOTSTRAP,boot)
    panel('/containers/compose',{'taskID':str(uuid.uuid4()),'name':PROJECT,'from':'path','path':str(BOOTSTRAP),
        'env':(ROOT/'.env').read_text(),'forcePull':False})
    for _ in range(60):
        records=containers(PROJECT)
        if len(records)==1 and records[0]['State'].get('Health',{}).get('Status')=='healthy':break
        time.sleep(2)
    require(len(records)==1 and records[0]['State'].get('Health',{}).get('Status')=='healthy','BOOTSTRAP_UNHEALTHY')
    BootstrapAdapter().identity()
    values=configuration(ROOT)
    require(all(c in '0123456789abcdef' for c in values['ASA_APP_DB_PASSWORD']),'PASSWORD_ENCODING')
    sql('postgres',"CREATE ROLE asalab_app LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD '"+values['ASA_APP_DB_PASSWORD']+"';")
    mark('bootstrap_ready')


def restore(release):
    require(state().get('phase')=='bootstrap_ready','EMPTY_BOOTSTRAP_REQUIRED')
    from deployment.backup import restore_check
    adapter=BootstrapAdapter()
    database='asa_fresh_20261021_test'
    restore_check(adapter,SNAPSHOT,database)
    check_expected_population(population(database),212)
    migrate(database,release)
    check_expected_population(population(database),234)
    # Promotion touches only the new empty installation. No previous test DB remains.
    require(sql('asalab','SELECT count(*) FROM pg_tables WHERE schemaname=\'public\';')=='0','BASE_DATABASE_NOT_EMPTY')
    sql('postgres','ALTER DATABASE asalab RENAME TO asa_empty_reinstall;')
    mark('database_promoting')
    sql('postgres','ALTER DATABASE '+database+' RENAME TO asalab;')
    sql('postgres','DROP DATABASE asa_empty_reinstall;')
    check_expected_population(population('asalab'),234)
    require(sql('postgres',"SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname='asalab_app';")=='f','RUNTIME_ROLE_PRIVILEGED')
    mark('database_restored',restored_population=population('asalab'))


def objects(release):
    require(state().get('phase')=='database_restored','DATABASE_RESTORE_REQUIRED')
    name=VOLUMES['blocks-object-data']
    require(name not in run(['docker','volume','ls','-q']).split(),'OBJECT_VOLUME_NOT_EMPTY_NEW')
    run(['docker','volume','create','--label','com.docker.compose.project='+PROJECT,
        '--label','com.docker.compose.volume=blocks-object-data',name])
    run(['docker','run','--rm','--network','none','--user','0','--security-opt','no-new-privileges:true',
        '--mount','type=bind,source='+str(SNAPSHOT/'objects')+',target=/source,readonly',
        '--mount','type=volume,source='+name+',target=/target','--entrypoint','sh',release['images']['minio'],
        '-eu','-c','cp -a /source/. /target/; chown -R 1000:1000 /target'])
    value=inspect_objects('volume',[name])[0]
    root=Path(value['Mountpoint'])
    manifest=json.loads((SNAPSHOT/'manifest.json').read_text())
    expected={k.removeprefix('objects/'):v['sha256'] for k,v in manifest['files'].items() if k.startswith('objects/')}
    actual={p.relative_to(root).as_posix():hashlib.file_digest(p.open('rb'),'sha256').hexdigest() for p in root.rglob('*') if p.is_file()}
    require(actual==expected,'OBJECT_BYTES_MISMATCH')
    mark('objects_restored',physical_object_files=len(actual))


def activate(release):
    require(state().get('phase')=='objects_restored','OBJECT_RESTORE_REQUIRED')
    from deployment.contracts import atomic_json
    panel('/containers/compose/operate',{'name':PROJECT,'path':str(BOOTSTRAP),'operation':'delete','withFile':False,'force':False})
    require(not containers(PROJECT),'BOOTSTRAP_CONTAINERS_REMAIN')
    BOOTSTRAP.unlink()
    atomic_json(ROOT/'.asa/installed-release.json',release)
    atomic_json(ROOT/'.asa/installation.json',{'format':'asa-installation-v1','profile':'production','project':PROJECT,'automaticUpdates':False})
    panel('/containers/compose/operate',{'name':PROJECT,'path':str(ROOT/'compose.yaml')+','+str(ROOT/'compose.production.yaml'),
        'operation':'up','withFile':False,'force':False})
    # Pin the auto-detected canonical multi-file project through the panel API.
    # Do not edit the panel DB or import one file as a weaker development stack.
    panel('/containers/compose/pin',{'name':PROJECT,'isPinned':True})
    mark('activated')


def verify(release):
    require(state().get('phase') in ('activated','technical_verified'),'ACTIVATION_REQUIRED')
    from deployment.system import Installation
    from deployment.contracts import verify_embedded_entry_http
    install=Installation(ROOT,'production')
    install.wait_ready(REVISION,234)
    records=install.identity()
    for service,image in release['images'].items():require(records[service]['image']==image,'RUNNING_IMAGE_MISMATCH')
    verify_embedded_entry_http('https://test.asa-lab.ru',REVISION)
    check_expected_population(population('asalab',bootstrap=False),234)
    ids={r['Id'] for r in containers()}
    require(set(state()['preserved_container_ids']) <= ids,'UNRELATED_CONTAINER_CHANGED')
    mark('technical_verified',readiness='passed',entry='passed',user_flow='NOT_RUN',fresh_backup_created=False)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phase',choices=['plan','erase','bootstrap','restore','objects','activate','verify'],default='plan')
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    sys.path.insert(0,str(source_root()/'tools'))
    release=guard_source()
    if args.phase!='plan':require(args.apply,'APPLY_REQUIRED')
    if args.phase=='plan':
        plan(release)
        return
    import fcntl
    from deployment.system import operation_lock
    with (STAGE/'reinstall.lock').open('a+b') as external:
        fcntl.flock(external,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with operation_lock(ROOT):
            if args.phase=='erase' and state().get('phase')=='rehearsed':
                config=json.loads(Path('/etc/server-settings/asa-operations.json').read_text())
                store=Path(config['installation']['state_root'])/'operations.sqlite3'
                def idle():
                    with sqlite3.connect('file:'+str(store)+'?mode=ro',uri=True) as db:
                        require(db.execute("SELECT count(*) FROM operations WHERE status IN ('queued','running')").fetchone()[0]==0,'ACTIVE_MCP_WORKER')
                idle()
                run(['systemctl','stop','asa-operations.service'])
                idle()
                # Detached workers survive service stops; confirm none exist.
                workers=subprocess.run(['pgrep','-f','asa_ops.worker|asa[_-]manager.py'],capture_output=True,text=True)
                require(workers.returncode==1,'ACTIVE_MANAGER_PROCESS')
            require(subprocess.run(['systemctl','is-active','--quiet','asa-operations.service']).returncode!=0,'MCP_MAINTENANCE_GATE_REQUIRED')
            globals()[args.phase](release)
            if args.phase=='verify':
                run(['systemctl','start','asa-operations.service'])


if __name__=='__main__':
    try:main()
    except Exception as error:
        print(json.dumps({'status':'stopped','error_class':type(error).__name__,'code':str(error) if isinstance(error,RuntimeError) else 'PRIVATE_DIAGNOSTIC_REQUIRED'}),flush=True)
        sys.exit(2)
