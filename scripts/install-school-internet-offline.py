#!/usr/bin/env python3
"""Install only the dormant offline profile CLI; plan by default."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import stat


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true');parser.add_argument('--source-sha')
    args=parser.parse_args()
    if not args.apply:
        print(json.dumps({'plan':True,'cli':'/usr/local/sbin/asa-school-internet','packages_installed':False,'network_changed':False,'automatic_failover_implemented':False}));return
    if os.geteuid()!=0 or socket.gethostname()!='pve' or not re.fullmatch('[0-9a-f]{40}',args.source_sha or ''):raise RuntimeError('Root on PVE and reviewed exact SHA required')
    source=Path(__file__).absolute().with_name('school-internet-offline.py')
    # Validate both installer and imported source ancestors without executing imports.
    for path in (Path(__file__).absolute(),source):
        current=Path(path.anchor)
        for part in path.parts[1:]:
            current=current/part;s=current.lstat()
            if stat.S_ISLNK(s.st_mode) or s.st_uid!=0 or s.st_mode&0o022:raise RuntimeError('Untrusted source path')
    spec=importlib.util.spec_from_file_location('offline_install',source)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    module.baseline()
    destination=Path('/opt/server-settings/school-internet-offline')/args.source_sha
    module.trust(destination,True);destination.mkdir(mode=0o755,parents=True,exist_ok=True)
    installed=destination/source.name;wrapper=Path('/usr/local/sbin/asa-school-internet')
    module.trust(installed,True);module.trust(wrapper,True)
    data=source.read_bytes()
    if installed.exists() and installed.read_bytes()!=data:raise RuntimeError('Version path content drift')
    body=('#!/bin/sh\nexec /usr/bin/python3 '+str(installed)+' "$@"\n').encode()
    # A foreign CLI cannot silently be replaced.
    if wrapper.exists() and not wrapper.read_bytes().startswith(b'#!/bin/sh\nexec /usr/bin/python3 /opt/server-settings/school-internet-offline/'):
        raise RuntimeError('Foreign CLI exists')
    for path,content in ((installed,data),(wrapper,body)):
        module.atomic_private(path,content);os.chmod(path,0o755)
    print(json.dumps({'installed':True,'source_sha':args.source_sha,'sha256':hashlib.sha256(data).hexdigest(),'network_changed':False,'units_installed':False}))

if __name__=='__main__':
    try:main()
    except Exception:raise SystemExit('STOP: offline CLI installation not accepted; inspect prerequisites locally') from None
