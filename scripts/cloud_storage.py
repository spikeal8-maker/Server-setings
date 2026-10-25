#!/usr/bin/env python3
"""Shared encrypted-archive transport. Never restores or mirrors a live database."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import uuid

ROOT = Path('/srv/asa-cloud-storage')
IMAGE = 'rclone/rclone@sha256:2687085f718d3c628f7fdfb77c52a1d344332aed543110bb88088f2c70d43eb5'
PROJECT = 'asa-cloud-storage'


def require(value, code):
    if not value:
        raise ValueError(code)


def remote_path(project):
    require(isinstance(project, str) and re.fullmatch('[a-z0-9][a-z0-9_-]{0,63}', project), 'CLOUD_PROJECT')
    return 'yandex:ASA-Backups' if project == 'asa-lab' else 'yandex:Server-Backups/' + project


def archive_name(value):
    require(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,180}\.age', value), 'CLOUD_ENCRYPTED_ARCHIVE_NAME')
    return value


def file_hash(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def compose():
    return {'services': {'rclone': {'image': IMAGE, 'user': '1000:1000', 'restart': 'unless-stopped',
            'command': ['rcd', '--config', '/config/rclone/rclone.conf', '--rc-addr', '127.0.0.1:5572', '--rc-no-auth'],
            'read_only': True, 'cap_drop': ['ALL'], 'security_opt': ['no-new-privileges:true'],
            'mem_limit': '256m', 'cpus': 0.5, 'pids_limit': 128, 'tmpfs': ['/tmp:uid=1000,gid=1000,mode=0700,size=32m'],
            'volumes': [{'type':'bind','source':str(ROOT/'config'),'target':'/config/rclone'},
                        {'type':'bind','source':str(ROOT/'data'),'target':'/data'}],
            'healthcheck': {'test':['CMD','rclone','rc','--url','http://127.0.0.1:5572','core/version'],
                            'interval':'30s','timeout':'10s','retries':3},
            'logging': {'driver':'json-file','options':{'max-size':'5m','max-file':'3'}},
            'labels': {'asa.owner':PROJECT}}}}


def connected():
    import configparser
    config=configparser.ConfigParser(interpolation=None)
    config.read(ROOT/'config/rclone.conf')
    try:
        value=json.loads(config['yandex']['token'])
        return bool(value.get('access_token')) and config['yandex']['type']=='yandex'
    except (ValueError, KeyError):
        return False


def install():
    require(os.geteuid()==0, 'CLOUD_ROOT')
    import stat
    if ROOT.exists():
        st=ROOT.lstat()
        require(stat.S_ISDIR(st.st_mode) and st.st_uid==0 and stat.S_IMODE(st.st_mode)==0o700, 'CLOUD_ROOT_IDENTITY')
        require(json.loads((ROOT/'compose.json').read_text())==compose(), 'CLOUD_EXISTING_CONFIGURATION')
    else:
        require(ROOT.parent.stat().st_uid==0 and not ROOT.parent.stat().st_mode & 0o022, 'CLOUD_PARENT')
        ROOT.mkdir(mode=0o700)
        for name in ['config','data']:
            p=ROOT/name;p.mkdir(mode=0o700);os.chown(p,1000,1000)
        (ROOT/'config/rclone.conf').write_text('');(ROOT/'config/rclone.conf').chmod(0o600);os.chown(ROOT/'config/rclone.conf',1000,1000)
        (ROOT/'compose.json').write_text(json.dumps(compose(),indent=2)+'\n');(ROOT/'compose.json').chmod(0o600)
    subprocess.run(['docker','pull',IMAGE],check=True,capture_output=True)
    subprocess.run(['docker','compose','-p',PROJECT,'-f',str(ROOT/'compose.json'),'config','--quiet'],check=True,capture_output=True)
    subprocess.run(['docker','compose','-p',PROJECT,'-f',str(ROOT/'compose.json'),'up','-d','--no-build'],check=True,capture_output=True)
    return {'project':PROJECT,'image':IMAGE,'public_ports':[],'connected':connected(),'backup_schedule_enabled':False}


def rclone(*args):
    command=['docker','exec',PROJECT+'-rclone-1','rclone','--config','/config/rclone/rclone.conf',
             '--contimeout','20s','--timeout','60s','--max-duration','100m','--retries','2',*args]
    p=subprocess.run(command,capture_output=True,text=True,timeout=6800)
    # Never return raw provider errors: these may contain account or token details.
    require(p.returncode==0, 'CLOUD_TRANSFER_FAILED')
    return p.stdout


def transfer(action, project, filename=None, expected_sha256=None):
    remote=remote_path(project)
    require(connected(), 'CLOUD_NOT_CONNECTED')
    if action=='list':
        rows=json.loads(rclone('lsjson','--files-only','--max-depth','1',remote))
        return {'project':project,'archives':[{'name':r['Name'],'size':r['Size'],'modified':r['ModTime']}
                                            for r in rows if r['Name'].endswith('.age')]}
    require(action in ('upload','download'), 'CLOUD_ACTION')
    filename=archive_name(filename)
    if expected_sha256:
        require(re.fullmatch('[a-f0-9]{64}', expected_sha256), 'CLOUD_HASH')
    base=ROOT/'data'/project
    for p in [base,base/'incoming',base/'outgoing']:
        if p.exists():
            require(not p.is_symlink() and p.is_dir(), 'CLOUD_SPOOL_IDENTITY')
        else:
            p.mkdir(mode=0o700);os.chown(p,1000,1000)
    if action=='upload':
        source=base/'outgoing'/filename
        require(source.is_file() and not source.is_symlink(), 'CLOUD_UPLOAD_SOURCE')
        digest=file_hash(source)
        require(not expected_sha256 or expected_sha256==digest, 'CLOUD_HASH_MISMATCH')
        # Only authenticated encryption envelopes, not plaintext dumps renamed .age.
        with source.open('rb') as f:
            prefix=f.read(40)
        require(prefix.startswith((b'age-encryption.org/v1',b'-----BEGIN AGE ENCRYPTED FILE-----')), 'CLOUD_AGE_ENVELOPE')
        rclone('copyto','/data/'+project+'/outgoing/'+filename,remote+'/'+filename,'--immutable','--checksum')
        return {'action':action,'project':project,'name':filename,'sha256':digest,'bytes':source.stat().st_size}
    target=base/'incoming'/filename
    require(not target.exists() and not target.is_symlink(), 'CLOUD_DOWNLOAD_EXISTS')
    partial=target.with_name('.'+uuid.uuid4().hex+'.partial')
    rclone('copyto',remote+'/'+filename,'/data/'+project+'/incoming/'+partial.name,'--immutable','--checksum')
    require(partial.is_file() and not partial.is_symlink(), 'CLOUD_DOWNLOAD_TYPE')
    digest=file_hash(partial)
    require(not expected_sha256 or expected_sha256==digest, 'CLOUD_HASH_MISMATCH')
    # Link provides no-overwrite promotion even if another process created target.
    os.link(partial,target);partial.unlink()
    return {'action':action,'project':project,'name':filename,'sha256':digest,'bytes':target.stat().st_size,
            'local_path':str(target),'database_restored':False}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['install','status','list','upload','download'])
    p.add_argument('--apply',action='store_true')
    p.add_argument('--project',default='asa-lab')
    p.add_argument('--filename')
    p.add_argument('--sha256')
    a=p.parse_args()
    try:
        if a.action=='status':
            value={'installed':ROOT.exists(),'connected':connected(),'project':PROJECT,'backup_schedule_enabled':False}
        elif a.action=='list':
            value=transfer(a.action,a.project)
        elif not a.apply:
            value={'plan':True,'action':a.action,'project':a.project,'remote':remote_path(a.project),
                   'database_changed':False,'delete_or_mirror':False}
        elif a.action=='install':
            value=install()
        else:
            value=transfer(a.action,a.project,a.filename,a.sha256)
        print(json.dumps(value));return 0
    except Exception as e:
        print(json.dumps({'ok':False,'code':str(e) if isinstance(e,ValueError) else type(e).__name__}));return 2


if __name__=='__main__':
    raise SystemExit(main())
