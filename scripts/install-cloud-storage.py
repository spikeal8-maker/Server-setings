#!/usr/bin/env python3
"""Install the shared container and enable its MCP archive transport. Plan by default."""
import argparse
import json
import os
from pathlib import Path
import pwd
import shutil
import socket
import sqlite3
import subprocess

from cloud_storage import install
from asa_ops.admin_executor import save


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--apply',action='store_true');p.add_argument('--expected-host')
    a=p.parse_args()
    if not a.apply:
        print('PLAN: shared Docker rclone, private account config/spool, MCP encrypted-archive transport. No OAuth token, mirror, database restore or backup schedule is created.');return
    assert os.geteuid()==0 and a.expected_host==socket.gethostname()
    source=Path(__file__).resolve().parents[1]
    path=Path('/etc/server-settings/asa-operations.json');c=json.loads(path.read_text())
    assert c.get('host_admin_enabled') is True
    with sqlite3.connect((Path(c['installation']['state_root'])/'operations.sqlite3').as_uri()+'?mode=ro',uri=True) as db:
        assert not db.execute("SELECT 1 FROM operations WHERE status IN ('queued','running','recovery_required')").fetchone(),'Active operation: defer installation'
    value=install()
    subprocess.run(['bash',str(source/'scripts/install-asa-operations.sh'),'--apply'],check=True,capture_output=True)
    target=Path('/opt/server-settings/asa-operations/cloud_storage.py')
    shutil.copyfile(source/'scripts/cloud_storage.py',target);target.chmod(0o644)
    c['cloud_storage_enabled']=True;save(path,c);os.chown(path,0,pwd.getpwuid(c['installation']['owner_uid']).pw_gid);path.chmod(0o640)
    secret_path=Path('/etc/server-settings/asa-admin-executor.json');secrets=json.loads(secret_path.read_text())
    filename='/srv/asa-cloud-storage/config/rclone.conf'
    if filename not in secrets['secret_files']:
        secrets['secret_files'].append(filename);save(secret_path,secrets)
    subprocess.run(['systemctl','restart','asa-operations.service'],check=True)
    print(json.dumps(value))


if __name__=='__main__':
    main()
