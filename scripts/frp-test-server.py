#!/usr/bin/env python3
"""Private Docker FRP test ingress. Plan by default; no firewall/DNS/app changes."""
import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import secrets
import socket
import stat
import subprocess
import zipfile

IMAGE = 'ghcr.io/fatedier/frps@sha256:48720c81f048e842f7f0b8053753c1a8514053d59d2dee8a953f67995de73a18'
ROOT = Path('/srv/asa-frp')
PROJECT = 'asa-frp-test'
PORTS = [14610, 14620, 14630]


def run(args):
    return subprocess.run([str(x) for x in args], check=True, capture_output=True, text=True).stdout


def private_write(path, value):
    with open(path, 'x', encoding='utf-8') as f:
        os.chmod(path, 0o600)
        f.write(value)


def validate_address(address):
    ip = ipaddress.ip_address(address)
    if ip.version != 4 or ip not in ipaddress.ip_network('100.64.0.0/10'):
        raise ValueError('Test control address must be an existing Tailscale IPv4 address')
    return str(ip)


def compose(address):
    return {'services': {'frps': {
        'image': IMAGE, 'command': ['-c', '/etc/frp/frps.toml'],
        'user': '65532:65532', 'read_only': True, 'cap_drop': ['ALL'],
        'security_opt': ['no-new-privileges:true'], 'restart': 'unless-stopped',
        'pids_limit': 128, 'mem_limit': '128m', 'cpus': 1,
        'volumes': [{'type': 'bind', 'source': str(ROOT / 'server'), 'target': '/etc/frp', 'read_only': True}],
        'ports': [f'{address}:7000:7000/tcp'] + [f'127.0.0.1:{p}:{p}/tcp' for p in PORTS],
        'logging': {'driver': 'json-file', 'options': {'max-size': '5m', 'max-file': '3'}},
        'labels': {'asa.owner': PROJECT}
    }}}


def server_config():
    return '''bindAddr = "0.0.0.0"
bindPort = 7000
proxyBindAddr = "0.0.0.0"
allowPorts = [{ single = 14610 }, { single = 14620 }, { single = 14630 }]
maxPortsPerClient = 3
auth.method = "token"
auth.tokenSource.type = "file"
auth.tokenSource.file.path = "/etc/frp/token"
transport.tls.force = true
transport.tls.certFile = "/etc/frp/server.crt"
transport.tls.keyFile = "/etc/frp/server.key"
transport.tls.trustedCaFile = "/etc/frp/ca.crt"
log.to = "console"
log.level = "warn"
'''


def trusted(path, uid, mode, directory=False):
    st = path.lstat()
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if not kind(st.st_mode) or st.st_uid != uid or stat.S_IMODE(st.st_mode) != mode:
        raise RuntimeError('Untrusted ownership/type/mode: ' + str(path))


def prepare(address):
    parent = ROOT.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != 0 or parent.st_mode & 0o022:
        raise RuntimeError('Deployment parent must be root-owned and not group/world writable')
    if ROOT.exists():
        trusted(ROOT, 0, 0o700, directory=True)
        marker = ROOT / 'manifest.json'
        if not marker.is_file():
            raise RuntimeError('Existing unowned/incomplete root; inspect, never overwrite automatically')
        trusted(marker, 0, 0o600)
        m = json.loads(marker.read_text())
        if m != {'project': PROJECT, 'address': address, 'image': IMAGE}:
            raise RuntimeError('Existing deployment differs; explicit migration required')
        trusted(ROOT / 'compose.json', 0, 0o600)
        if json.loads((ROOT / 'compose.json').read_text()) != compose(address):
            raise RuntimeError('Existing Compose is not the exact trusted deployment')
        trusted(ROOT / 'server', 65532, 0o700, directory=True)
        for name in ['frps.toml', 'server.crt', 'server.key', 'ca.crt', 'token']:
            trusted(ROOT / 'server' / name, 65532, 0o400)
        if (ROOT / 'server/frps.toml').read_text() != server_config():
            raise RuntimeError('Existing FRPS configuration differs')
        trusted(ROOT / 'pki', 0, 0o700, directory=True)
        for name in ['ca.key', 'ca.crt']: trusted(ROOT / 'pki' / name, 0, 0o600)
        return
    for host, port in [(address, 7000)] + [('127.0.0.1', p) for p in PORTS]:
        with socket.socket() as s:
            s.bind((host, port))
    ROOT.mkdir(mode=0o700)
    pki, server = ROOT / 'pki', ROOT / 'server'
    pki.mkdir(mode=0o700); server.mkdir(mode=0o700)
    run(['openssl', 'req', '-x509', '-newkey', 'rsa:3072', '-nodes', '-days', '365', '-subj', '/CN=ASA FRP Test CA', '-addext', 'basicConstraints=critical,CA:TRUE', '-addext', 'keyUsage=critical,keyCertSign,cRLSign', '-keyout', pki / 'ca.key', '-out', pki / 'ca.crt'])
    run(['openssl', 'req', '-new', '-newkey', 'rsa:3072', '-nodes', '-subj', '/CN=asa-frp-test', '-keyout', server / 'server.key', '-out', pki / 'server.csr'])
    private_write(pki / 'server.ext', f'basicConstraints=CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\nsubjectAltName=DNS:test.asa-lab.ru,IP:{address}\nextendedKeyUsage=serverAuth\n')
    run(['openssl', 'x509', '-req', '-in', pki / 'server.csr', '-CA', pki / 'ca.crt', '-CAkey', pki / 'ca.key', '-CAcreateserial', '-days', '180', '-extfile', pki / 'server.ext', '-out', server / 'server.crt'])
    private_write(server / 'ca.crt', (pki / 'ca.crt').read_text())
    private_write(server / 'token', secrets.token_hex(32))
    private_write(server / 'frps.toml', server_config())
    for f in pki.iterdir(): os.chmod(f, 0o600)
    for f in server.iterdir():
        os.chmod(f, 0o400); os.chown(f, 65532, 65532)
    os.chown(server, 65532, 65532)
    private_write(ROOT / 'compose.json', json.dumps(compose(address), indent=2))
    # Marker only after all materials are prepared; partial failures remain for inspection.
    private_write(ROOT / 'manifest.json', json.dumps({'project': PROJECT, 'address': address, 'image': IMAGE}))


def enroll(csr):
    """Client key stays on Windows; only public CSR enters, encrypted token leaves."""
    target = ROOT / 'clients' / 'school'
    digest = hashlib.sha256(csr.read_bytes()).hexdigest()
    if target.exists():
        if (target / 'csr.sha256').read_text() != digest:
            raise RuntimeError('School identity already enrolled; do not replace its key silently')
        return target / 'school-public-bundle.zip'
    target.mkdir(mode=0o700, parents=True)
    os.chmod(target.parent, 0o700)
    private_write(target / 'client.csr', csr.read_text())
    private_write(target / 'client.ext', 'basicConstraints=CA:FALSE\nkeyUsage=digitalSignature,keyEncipherment\nextendedKeyUsage=clientAuth\n')
    run(['openssl', 'req', '-in', target / 'client.csr', '-verify', '-noout'])
    run(['openssl', 'x509', '-req', '-in', target / 'client.csr', '-CA', ROOT / 'pki/ca.crt', '-CAkey', ROOT / 'pki/ca.key', '-CAcreateserial', '-days', '180', '-extfile', target / 'client.ext', '-out', target / 'client.crt'])
    private_write(target / 'public.pem', run(['openssl', 'req', '-in', target / 'client.csr', '-pubkey', '-noout']))
    run(['openssl', 'pkeyutl', '-encrypt', '-pubin', '-inkey', target / 'public.pem', '-in', ROOT / 'server/token', '-out', target / 'token.rsa', '-pkeyopt', 'rsa_padding_mode:oaep', '-pkeyopt', 'rsa_oaep_md:sha256'])
    bundle = target / 'school-public-bundle.zip'
    with zipfile.ZipFile(bundle, 'x', zipfile.ZIP_DEFLATED) as z:
        z.write(target / 'client.crt', 'client.crt')
        z.write(ROOT / 'pki/ca.crt', 'ca.crt')
        z.write(target / 'token.rsa', 'token.rsa')
    os.chmod(bundle, 0o600)
    private_write(target / 'csr.sha256', digest)
    return bundle


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--control-address', default='100.75.149.26')
    p.add_argument('--apply', action='store_true')
    p.add_argument('--enroll-csr', type=Path)
    a = p.parse_args(); address = validate_address(a.control_address)
    if not a.apply:
        print(json.dumps({'plan': True, 'root': str(ROOT), 'compose': compose(address), 'firewall_changes': False})); return
    if os.geteuid() != 0: raise RuntimeError('Root required on Ubuntu')
    os.umask(0o077)
    prepare(address)
    args = ['docker', 'compose', '-p', PROJECT, '-f', ROOT / 'compose.json']
    run(args + ['config', '--quiet'])
    run(['docker', 'run', '--rm', '--network', 'none', '--user', '65532:65532', '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '-v', f'{ROOT}/server:/etc/frp:ro', IMAGE, 'verify', '-c', '/etc/frp/frps.toml'])
    run(args + ['up', '-d', '--no-deps', 'frps'])
    result = {'project': PROJECT, 'image': IMAGE, 'control': address + ':7000', 'loopback_proxy_ports': PORTS}
    if a.enroll_csr: result['public_client_bundle'] = str(enroll(a.enroll_csr))
    print(json.dumps(result))


if __name__ == '__main__': main()
