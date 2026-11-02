#!/usr/bin/env python3
"""Owned school panel publication through FRP and edge Traefik; plan by default."""
import argparse
import hashlib
import multiprocessing
import importlib.util
import json
import os
from pathlib import Path
import socket
import stat
import sys
import subprocess
import tempfile
import time
import tomllib

DOMAIN = '1panel.x.asa-lab.ru'
ALIAS = 'asa-school-panel'
FRP = Path('/etc/asa-frpc/frpc.toml')
EDGE = Path('/data/coolify/proxy/dynamic/asa-school-panel-routes.yaml')
STATE = Path('/var/lib/server-settings/school-panel-publication')
COMPANION_SHA256 = {
 'publish-panel-https.py': 'e182f176d3b024f9302effff4a013b1540d3b23538194ccc3c7afe6edcbb4231',
 'panelctl.py': 'cec93e00e1b34163034eafc7d46e9d2e8e540b6852cf419793c64c62c8a8074d',
}
FRPC_SHA256 = '142f447f43fef286acc8da8a6852dda80631db631d604b2e63634b2db4d6848c'


def trusted_chain(path, allow_missing=False, public_edge=False):
    path=Path(path)
    if not path.is_absolute() or '..' in path.parts: raise RuntimeError('Absolute canonical path required')
    current=Path(path.anchor)
    for part in path.parts[1:]:
        current=current/part
        try: value=current.lstat()
        except FileNotFoundError:
            if allow_missing: return
            raise RuntimeError('Trusted path missing') from None
        allowed_uid=1000 if public_edge and current in (Path('/data/coolify/proxy'),Path('/data/coolify/proxy/dynamic'),EDGE) else 0
        if stat.S_ISLNK(value.st_mode) or value.st_uid!=allowed_uid or value.st_mode & 0o022:
            raise RuntimeError('Untrusted path ancestor')
        if current!=path and not stat.S_ISDIR(value.st_mode): raise RuntimeError('Non-directory path ancestor')


def pinned_file(path, expected):
    trusted_chain(path)
    if not stat.S_ISREG(path.lstat().st_mode) or hashlib.sha256(path.read_bytes()).hexdigest()!=expected:
        raise RuntimeError('Pinned file content mismatch')


def prepare_state():
    trusted_chain(STATE,allow_missing=True)
    STATE.mkdir(mode=0o700,parents=True,exist_ok=True)
    trusted_chain(STATE)
    if STATE.stat().st_mode & 0o077: raise RuntimeError('Private state directory required')



def require_private(path):
    trusted_chain(path)
    s = path.lstat()
    if not stat.S_ISREG(s.st_mode) or s.st_uid != 0 or s.st_mode & 0o077:
        raise RuntimeError('Private root-owned regular file required')


def atomic(path, content, mode=0o600, group=None):
    trusted_chain(path.parent)
    trusted_chain(path,allow_missing=True)
    fd, name = tempfile.mkstemp(prefix='.school-panel-', dir=path.parent)
    try:
        if group is not None: os.fchown(fd,0,group)
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'wb') as out:
            out.write(content); out.flush(); os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)


def frp_candidate(raw):
    data = tomllib.loads(raw)
    if data.get('serverAddr') != '161.104.46.160' or data.get('serverPort') != 7000 or data.get('transport', {}).get('tls', {}).get('enable') is not True:
        raise RuntimeError('FRP endpoint or TLS drift')
    proxies = data.get('proxies', [])
    matches = [p for p in proxies if p.get('name') == 'asa-school-100-x']
    if len(matches) != 1:
        raise RuntimeError('Expected existing school HTTP proxy')
    p = matches[0]
    if (p.get('type'), p.get('localIP'), p.get('localPort')) != ('http', '127.0.0.1', 80):
        raise RuntimeError('FRP proxy target drift')
    domains = p.get('customDomains')
    if domains not in (['x.asa-lab.ru'], ['x.asa-lab.ru', DOMAIN]):
        raise RuntimeError('FRP domain drift')
    # Replace only the exact TOML assignment in the selected proxy table.
    import re
    blocks = re.split(r'(?m)^(?=\[\[proxies\]\])', raw)
    found = 0
    for i, block in enumerate(blocks):
        parsed = tomllib.loads(block) if block.startswith('[[proxies]]') else {}
        if parsed.get('proxies', [{}])[0].get('name') == p['name']:
            blocks[i], n = re.subn(r'(?m)^customDomains[ \t]*=[ \t]*\[[^\n]*\][ \t]*$', 'customDomains = ["x.asa-lab.ru", "'+DOMAIN+'"]', block)
            found += n
    if found != 1: raise RuntimeError('FRP assignment ambiguous')
    candidate = ''.join(blocks)
    # Do not alter reconnect policy implicitly: current service already retries.
    check = tomllib.loads(candidate)
    expected = json.loads(json.dumps(data))
    next(p for p in expected['proxies'] if p['name']=='asa-school-100-x')['customDomains'] = ['x.asa-lab.ru', DOMAIN]
    if check != expected: raise RuntimeError('Unexpected FRP mutation')
    return candidate


def edge_document():
    return {'http': {'routers': {
        'asa-school-panel-http': {'rule': 'Host(`'+DOMAIN+'`)', 'entryPoints':['http'], 'middlewares':['asa-school-panel-redirect'], 'service':'asa-school-panel'},
        'asa-school-panel-https': {'rule':'Host(`'+DOMAIN+'`)', 'entryPoints':['https'], 'tls':{'certResolver':'letsencrypt'}, 'middlewares':['asa-school-panel-headers'], 'service':'asa-school-panel'}},
        'middlewares': {'asa-school-panel-redirect':{'redirectScheme':{'scheme':'https','permanent':True}},
          'asa-school-panel-headers':{'headers':{'contentTypeNosniff':True,'frameDeny':False,'customFrameOptionsValue':'SAMEORIGIN','referrerPolicy':'no-referrer','stsSeconds':31536000}}},
        'services':{'asa-school-panel':{'loadBalancer':{'passHostHeader':True,'servers':[{'url':'http://frps:8080'}]}}}}}


def receipt(role, content):
    prepare_state()
    s=STATE.lstat()
    if not stat.S_ISDIR(s.st_mode) or s.st_uid!=0 or s.st_mode & 0o077: raise RuntimeError('Unsafe state directory')
    path=STATE/(role+'-original')
    if not path.exists(): atomic(path,content)
    return path


def frp_metadata():
    import grp
    trusted_chain(FRP)
    value=FRP.lstat()
    group=grp.getgrnam('asa-frpc').gr_gid
    if not stat.S_ISREG(value.st_mode) or value.st_uid!=0 or value.st_gid!=group or stat.S_IMODE(value.st_mode)!=0o640:
        raise RuntimeError('FRP service-secret metadata drift')
    result=subprocess.run(['systemctl','show','asa-frpc','--property=User','--property=Group','--property=DynamicUser'],capture_output=True,text=True,check=True)
    values=dict(line.split('=',1) for line in result.stdout.splitlines() if '=' in line)
    if values!={'User':'asa-frpc','Group':'asa-frpc','DynamicUser':'no'}:
        raise RuntimeError('FRP service identity drift')
    return group


def school():
    trusted_chain(STATE,allow_missing=True)
    companions=Path(__file__).absolute().parent
    trusted_chain(Path(__file__).absolute())
    for filename,digest in COMPANION_SHA256.items(): pinned_file(companions/filename,digest)
    frp_group=frp_metadata()
    require_private(Path('/var/lib/server-settings/1panel-api.key'))
    raw=FRP.read_bytes(); candidate=frp_candidate(raw.decode()).encode()
    # Validate executable/config before any API mutation or service restart.
    binary='/usr/local/bin/frpc'
    pinned_file(Path(binary),FRPC_SHA256)
    prepare_state()
    probe=subprocess.run(['systemctl','show','asa-frpc','--property=ExecStart','--value'],capture_output=True,text=True,check=True)
    if binary not in probe.stdout or str(FRP) not in probe.stdout: raise RuntimeError('FRP service executable drift')
    fd,name=tempfile.mkstemp(prefix='.panel-verify-',dir=FRP.parent)
    try:
        os.fchmod(fd,0o600)
        with os.fdopen(fd,'wb') as out: out.write(candidate)
        subprocess.run([binary,'verify','-c',name],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    finally: os.unlink(name)
    ctl_spec=importlib.util.spec_from_file_location('panelctl',companions/'panelctl.py')
    ctl=importlib.util.module_from_spec(ctl_spec);sys.modules['panelctl']=ctl;ctl_spec.loader.exec_module(ctl)
    module=companions/'publish-panel-https.py'
    spec=importlib.util.spec_from_file_location('panel_publish',module)
    pub=importlib.util.module_from_spec(spec);spec.loader.exec_module(pub)
    api=pub.LoopbackPanelAPI(pub.read_api_key('/var/lib/server-settings/1panel-api.key'))
    rows=pub.websites(api)
    selected=[r for r in rows if r.get('primaryDomain')==DOMAIN or r.get('alias')==ALIAS]
    if selected and (len(selected)!=1 or selected[0].get('primaryDomain')!=DOMAIN or selected[0].get('alias')!=ALIAS or selected[0].get('type')!='proxy'):
        raise RuntimeError('Website ownership drift')
    if not selected:
        groups=api.request('POST','/groups/search',{'type':'website'}) or []
        defaults=[g for g in groups if g.get('isDefault')]
        if len(defaults)!=1: raise RuntimeError('Website default group ambiguous')
        api.request('POST','/websites',{'domains':[{'domain':DOMAIN,'port':80,'ssl':False}], 'alias':ALIAS,'type':'proxy','webSiteGroupID':defaults[0]['id'],'proxy':pub.SINK,'appType':'new'})
        selected=[r for r in pub.websites(api) if r.get('primaryDomain')==DOMAIN and r.get('alias')==ALIAS]
    if len(selected)!=1: raise RuntimeError('Owned website not confirmed')
    site=selected[0]; row=pub.proxy_record(api,site['id'])
    if row.get('proxyPass') not in (pub.SINK,pub.UPSTREAM): raise RuntimeError('Owned proxy upstream drift')
    pub.configure_proxy(api,site,True)
    receipt('frpc',raw)
    if candidate!=raw:
        if FRP.read_bytes()!=raw: raise RuntimeError('FRP changed concurrently')
        atomic(FRP,candidate,0o640,frp_group)
        try:
            subprocess.run(['systemctl','restart','asa-frpc'],check=True,timeout=30)
            time.sleep(3)
            subprocess.run(['systemctl','is-active','--quiet','asa-frpc'],check=True,timeout=10)
        except Exception:
            atomic(FRP,raw,0o640,frp_group)
            subprocess.run(['systemctl','restart','asa-frpc'],check=True,timeout=30)
            raise RuntimeError('FRP restart failed; previous config restored') from None
    return {'school_configured':True,'direct_panel_port_opened':False,'password_or_entrance_changed':False}


def edge_writer(content):
    """Child loses all root credentials before touching Coolify-owned public path."""
    os.setgroups([])
    os.setgid(1000)
    os.setuid(1000)
    if os.getuid()!=1000 or os.geteuid()!=1000: raise RuntimeError('Privilege drop failed')
    trusted_chain(EDGE,allow_missing=True,public_edge=True)
    trusted_chain(EDGE.parent,public_edge=True)
    flags=os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW
    directory=os.open(EDGE.parent,flags)
    name='.asa-school-panel-'+str(os.getpid())
    try:
        # Directory FD prevents rename races from redirecting the write elsewhere.
        fd=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o644,dir_fd=directory)
        with os.fdopen(fd,'wb') as output:
            output.write(content);output.flush();os.fsync(output.fileno())
        os.replace(name,EDGE.name,src_dir_fd=directory,dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        try: os.unlink(name,dir_fd=directory)
        except FileNotFoundError: pass
        os.close(directory)


def public_edge_write(content):
    # fork only a one-purpose writer; root never opens output under uid1000 parents.
    process=multiprocessing.get_context('fork').Process(target=edge_writer,args=(content,))
    process.start();process.join(15)
    if process.is_alive():
        process.terminate();process.join();raise RuntimeError('Public writer timeout')
    if process.exitcode!=0: raise RuntimeError('Unprivileged public writer failed')


def edge():
    trusted_chain(EDGE,allow_missing=True,public_edge=True)
    trusted_chain(EDGE.parent,public_edge=True)
    prepare_state()
    content=(json.dumps(edge_document(),indent=2)+'\n').encode()
    marker=STATE/'edge-installed.sha256'
    if EDGE.exists():
        if EDGE.is_symlink(): raise RuntimeError('Edge symlink refused')
        old=EDGE.read_bytes()
        if old==content: return {'edge_unchanged':True}
        if not marker.exists() or hashlib.sha256(old).hexdigest()!=marker.read_text().strip(): raise RuntimeError('Existing edge file not owned or changed')
    else: old=b''
    receipt('edge',old)
    public_edge_write(content)
    atomic(marker,(hashlib.sha256(content).hexdigest()+'\n').encode())
    return {'edge_configured':True,'proxy_restarted':False}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('role',choices=['school','edge']);p.add_argument('--apply',action='store_true');p.add_argument('--expected-host')
    a=p.parse_args()
    if not a.apply:
        print(json.dumps({'plan':True,'role':a.role,'domain':DOMAIN,'changes':'owned panel site + existing FRP domain' if a.role=='school' else str(EDGE)}));return
    if os.geteuid()!=0 or not a.expected_host or socket.gethostname()!=a.expected_host or a.expected_host != {'school':'asa-school-01','edge':'leticia'}[a.role]:
        raise RuntimeError('Root and exact expected hostname required')
    print(json.dumps(school() if a.role=='school' else edge()))

if __name__=='__main__':
    try: main()
    except Exception: raise SystemExit('School panel publication failed; inspect private state locally') from None
