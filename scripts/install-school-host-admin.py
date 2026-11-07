#!/usr/bin/env python3
"""Enroll VM100's existing MCP in explicit host-only administration.

Plan by default. Install the private backend first, accept it, then switch the
existing OAuth gateway separately. No application data, Docker service, firewall,
SSH policy, OAuth issuer, owner password or OAuth state is changed.
"""
import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import time

sys.dont_write_bytecode = True
from asa_ops.admin_executor import root_installation_identity
from asa_ops.host_profile import HOST_ONLY, HOST_TOOLS, trusted_metadata
from asa_ops.store import OpsError

SOURCE = Path(__file__).resolve().parents[1]
HOST = 'asa-school-01'
INSTALLATION = 'asa-school-vm100'
SERVICE_USER = 'asa-school-mcp-admin'
ROOT = Path('/srv/asa-lab')
CONFIG = Path('/etc/server-settings/asa-operations.json')
ENV = CONFIG.with_suffix('.env')
EXECUTOR = CONFIG.with_name('asa-admin-executor.json')
STATE = Path('/var/lib/server-settings/asa-operations')
ROOT_STATE = Path('/var/lib/server-settings/asa-admin-executor')
RECEIPT = Path('/var/lib/server-settings/asa-school-host-admin-install.json')
MCP_ENV = CONFIG.with_name('mcp.env')
GATEWAY_ENV = CONFIG.with_name('asa-school-mcp-gateway.env')
GATEWAY_UNIT = Path('/etc/systemd/system/asa-school-mcp-oauth-gateway.service')
DROPIN = Path(str(GATEWAY_UNIT)+'.d/20-host-admin.conf')
ROLLBACK = Path('/var/lib/server-settings/asa-school-host-admin-rollback')
ROUTE_BACKUP = Path('/var/lib/server-settings/asa-school-mcp-route.before.service')
OFFICIAL = frozenset({'get_system_info', 'get_dashboard_info', 'list_websites',
                     'list_ssls', 'list_databases', 'list_installed_apps',
                     'create_website', 'create_ssl', 'create_database',
                     'install_openresty', 'install_mysql'})
NEW_DROPIN = '''[Unit]
After=
After=network-online.target asa-operations.service
Requires=
Requires=asa-operations.service

[Service]
ExecStart=
ExecStart=/opt/server-settings/bin/asa-mcp-auth-proxy --listen 127.0.0.1:39450 --no-auto-tls --external-url https://x.asa-lab.ru -- http://127.0.0.1:39446
'''


class Blocked(Exception):
    pass


def require(value, code):
    if not value:
        raise Blocked(code)


def run(args, *, timeout=60):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=timeout)


def environment(path):
    trusted_metadata(path)
    require(stat.S_IMODE(path.stat().st_mode) == 0o600, 'ENV_NOT_ROOT_ONLY')
    result = {}
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        key, sep, value = line.partition('=')
        require(sep and re.fullmatch(r'[A-Z][A-Z0-9_]*', key)
                and key not in result, 'ENV_FORMAT')
        result[key] = value
    return result


def create(path, content, *, mode=0o600, gid=0):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, 'w') as out:
        os.fchown(out.fileno(), 0, gid)
        os.fchmod(out.fileno(), mode)
        out.write(content)
        out.flush()
        os.fsync(out.fileno())


def replace(path, content, *, mode=0o600):
    # The containing directory is root-owned and not writable by the service.
    trusted_metadata(path)
    temp = path.with_name(path.name+'.host-admin-new')
    create(temp, content, mode=mode)
    os.replace(temp, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def identity():
    trusted_metadata(ROOT, directory=True)
    trusted_metadata(ROOT/'.asa', directory=True)
    trusted_metadata(ROOT/'.asa/installation.json')
    current = json.loads((ROOT/'.asa/installation.json').read_text())
    require(current.get('profile') in ('production', 'staging')
            and isinstance(current.get('project'), str)
            and re.fullmatch(r'[A-Za-z0-9_-]+', current['project']), 'INSTALLATION_PROFILE')
    return root_installation_identity({'root': str(ROOT),
           'installation_project': current['project'], 'installation_profile': current['profile']})


def verify_source(source_sha):
    """Bind the local reviewed checkout/bundle bytes to the deployment receipt.

The deployment operator obtains the bundle and its manifest from the reviewed
GitHub commit. This step checks that trusted transport's declared SHA and all
local file hashes; it does not discover or trust a floating remote release.
"""
    if (SOURCE/'.git').exists():
        require(run(['git', '-C', str(SOURCE), 'rev-parse', 'HEAD']).stdout.strip() == source_sha,
                'SOURCE_HEAD_MISMATCH')
        require(not run(['git', '-C', str(SOURCE), 'status', '--porcelain', '--untracked-files=all',
                         '--', 'scripts', 'templates']).stdout.strip(), 'SOURCE_DIRTY')
        return 'git_head_and_clean_source'
    manifest_path = SOURCE/'source-manifest.json'
    trusted_metadata(manifest_path)
    value = json.loads(manifest_path.read_text())
    require(value.get('format') == 'asa-school-host-admin-source-v1'
            and value.get('source_sha') == source_sha and isinstance(value.get('files'), dict), 'SOURCE_MANIFEST')
    needed = {str(p.relative_to(SOURCE)) for p in (SOURCE/'scripts/asa_ops').glob('*.py')}
    needed.update({'scripts/install-school-host-admin.py', 'scripts/install-asa-operations.sh',
                   'scripts/school-mcp-host-admin-acceptance.py',
                   'templates/asa-operations.service', 'templates/asa-admin-executor.socket',
                   'templates/asa-admin-executor@.service'})
    require(needed and needed <= set(value['files']), 'SOURCE_MANIFEST_INCOMPLETE')
    for filename, expected in value['files'].items():
        path = SOURCE/filename
        require(isinstance(filename, str) and not Path(filename).is_absolute()
                and '..' not in Path(filename).parts
                and isinstance(expected, str) and re.fullmatch(r'[a-f0-9]{64}', expected), 'SOURCE_MANIFEST_ENTRY')
        trusted_metadata(path)
        require(hashlib.sha256(path.read_bytes()).hexdigest() == expected, 'SOURCE_FILE_MISMATCH')
    return 'trusted_bundle_manifest_and_file_hashes'


def repair_gateway_route():
    """Correct only the known duplicated-path bug, with a private local backup."""
    trusted_metadata(GATEWAY_UNIT)
    original = GATEWAY_UNIT.read_text()
    old = '-- http://127.0.0.1:39443/mcp'
    new = '-- http://127.0.0.1:39443'
    if old not in original:
        baseline_gateway()
        return {'gateway_route': 'already_correct', 'oauth_environment_changed': False}
    require(original.count(old) == 1 and old+'\n' in original, 'GATEWAY_ROUTE_DRIFT')
    effective = run(['systemctl', 'show', GATEWAY_UNIT.name, '--property=ExecStart', '--value']).stdout
    require(re.search(r'http://127\.0\.0\.1:39443/mcp(?:\s|;|$)', effective), 'GATEWAY_EFFECTIVE_ROUTE_DRIFT')
    if ROUTE_BACKUP.exists():
        trusted_metadata(ROUTE_BACKUP)
        require(stat.S_IMODE(ROUTE_BACKUP.stat().st_mode) == 0o600
                and ROUTE_BACKUP.read_text() == original, 'GATEWAY_ROUTE_BACKUP_DRIFT')
    else:
        create(ROUTE_BACKUP, original)
    try:
        require(GATEWAY_UNIT.read_text() == original, 'GATEWAY_UNIT_CHANGED')
        replace(GATEWAY_UNIT, original.replace(old+'\n', new+'\n', 1), mode=0o644)
        run(['systemctl', 'daemon-reload'])
        run(['systemctl', 'restart', GATEWAY_UNIT.name])
        run(['systemctl', 'is-active', '--quiet', GATEWAY_UNIT.name])
        baseline_gateway()
    except BaseException:
        replace(GATEWAY_UNIT, original, mode=0o644)
        run(['systemctl', 'daemon-reload'])
        run(['systemctl', 'restart', GATEWAY_UNIT.name])
        raise
    return {'gateway_backend': 'http://127.0.0.1:39443', 'oauth_environment_changed': False,
            'public_authenticated_acceptance': False, 'route_backup': str(ROUTE_BACKUP)}


def baseline_gateway():
    """Rollback must restore an already-corrected 11-tool gateway, not /mcp/mcp."""
    trusted_metadata(GATEWAY_UNIT)
    args = run(['systemctl', 'show', GATEWAY_UNIT.name, '--property=ExecStart', '--value']).stdout
    require('http://127.0.0.1:39443/mcp' not in args
            and re.search(r'http://127\.0\.0\.1:39443(?:\s|;|$)', args),
            'CORRECTED_11_TOOL_GATEWAY_REQUIRED')
    values = environment(GATEWAY_ENV)
    require(values.get('EXTERNAL_URL') == 'https://x.asa-lab.ru'
            and values.get('DATA_PATH') == '/var/lib/asa-school-mcp-gateway'
            and values.get('PASSWORD') and values.get('PROXY_BEARER_TOKEN'), 'GATEWAY_IDENTITY')
    return values


def no_workers():
    dbfile = STATE/'operations.sqlite3'
    if dbfile.exists():
        with sqlite3.connect(dbfile.as_uri()+'?mode=ro', uri=True) as db:
            require(not db.execute("SELECT 1 FROM operations WHERE status IN ('queued','running','recovery_required')").fetchone(),
                    'ACTIVE_OR_INTERRUPTED_OPERATION')


def preflight_install():
    current = identity()
    baseline_gateway()
    mcp = environment(MCP_ENV)
    require(mcp.get('PANEL_HOST') == 'http://127.0.0.1:19192'
            and mcp.get('PANEL_ACCESS_TOKEN') and mcp.get('MCP_ACCESS_LEVEL') == 'full',
            'OFFICIAL_FULL_MCP_REQUIRED')
    require(shutil.which('setfacl') and shutil.which('runuser'), 'ACL_AND_RUNUSER_REQUIRED')
    for path in (CONFIG, ENV, EXECUTOR, RECEIPT,
                 Path('/etc/systemd/system/asa-operations.service'),
                 Path('/etc/systemd/system/asa-admin-executor.socket'),
                 Path('/etc/systemd/system/asa-admin-executor@.service')):
        require(not path.exists() and not path.is_symlink(), 'EXISTING_ADMIN_CONFIGURATION')
    no_workers()
    for path in (SOURCE/'scripts/install-asa-operations.sh',
                 SOURCE/'scripts/asa_ops/admin_executor.py',
                 SOURCE/'templates/asa-admin-executor.socket',
                 SOURCE/'templates/asa-admin-executor@.service'):
        trusted_metadata(path)
    listeners = run(['ss', '-ltnH']).stdout
    require(not any(line.split()[3].endswith(':39446') for line in listeners.splitlines()), 'BACKEND_PORT_IN_USE')
    return current, mcp


def rpc(token, method, params=None):
    connection = http.client.HTTPConnection('127.0.0.1', 39446, timeout=65)
    try:
        payload = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}})
        connection.request('POST', '/mcp', payload,
                           {'Content-Type': 'application/json', 'Authorization': 'Bearer '+token})
        response = connection.getresponse()
        data = response.read(262145)
        require(response.status == 200 and len(data) <= 262144, 'PRIVATE_MCP_HTTP')
        value = json.loads(data)
        require(value.get('id') == 1 and value.get('jsonrpc') == '2.0'
                and 'error' not in value and isinstance(value.get('result'), dict), 'PRIVATE_MCP_RPC')
        return value['result']
    finally:
        connection.close()


def verify_private():
    config = json.loads(CONFIG.read_text())
    require(config.get('operations_mode') == HOST_ONLY
            and config['installation']['id'] == INSTALLATION
            and config['hostname'] == HOST, 'BACKEND_IDENTITY')
    token = environment(ENV)['ASA_OPS_TOKEN']
    connection = http.client.HTTPConnection('127.0.0.1', 39446, timeout=5)
    try:
        connection.request('POST', '/mcp', '{}', {'Content-Type': 'application/json'})
        require(connection.getresponse().status == 401, 'PRIVATE_MCP_UNAUTHENTICATED')
    finally:
        connection.close()
    rpc(token, 'initialize', {'protocolVersion': '2025-03-26', 'capabilities': {},
        'clientInfo': {'name': 'school-host-admin-installer', 'version': '1'}})
    names = [t['name'] for t in rpc(token, 'tools/list')['tools']]
    require(len(names) == 16 and set(names) == OFFICIAL | HOST_TOOLS, 'PRIVATE_MCP_TOOLSET')
    status = rpc(token, 'tools/call', {'name': 'asa_admin_status',
                 'arguments': {'installation_id': INSTALLATION}})
    body = status.get('structuredContent') or {}
    data = body.get('data') or {}
    require(body.get('ok') is True and not status.get('isError')
            and data.get('uid') == 0 and data.get('hostname') == HOST
            and data.get('installation_id') == INSTALLATION
            and data.get('installation_instance_id') == identity()['instanceId'], 'PRIVATE_ROOT_IDENTITY')
    listeners = run(['ss', '-ltnH']).stdout
    found = [line.split()[3] for line in listeners.splitlines() if line.split()[3].endswith(':39446')]
    require(found == ['127.0.0.1:39446'], 'PRIVATE_MCP_LISTENER')
    return config, token


def enroll(source_sha):
    source_verification = verify_source(source_sha)
    current, mcp = preflight_install()
    try:
        owner = pwd.getpwnam(SERVICE_USER)
    except KeyError:
        run(['useradd', '--system', '--user-group', '--home-dir', '/nonexistent',
             '--shell', '/usr/sbin/nologin', SERVICE_USER])
        owner = pwd.getpwnam(SERVICE_USER)
    require(owner.pw_uid > 0 and owner.pw_shell == '/usr/sbin/nologin'
            and set(os.getgrouplist(SERVICE_USER, owner.pw_gid)) == {owner.pw_gid}, 'DEDICATED_SERVICE_ACCOUNT')
    for path in (CONFIG.parent, ROOT_STATE.parent):
        trusted_metadata(path, directory=True)
    # Config metadata is readable only by the service group; secrets stay 0600.
    if subprocess.run(['runuser', '-u', SERVICE_USER, '--', 'test', '-x', str(CONFIG.parent)],
                      capture_output=True).returncode:
        run(['setfacl', '-m', 'u:'+SERVICE_USER+':--x', str(CONFIG.parent)])
    if ROOT_STATE.exists():
        trusted_metadata(ROOT_STATE, directory=True)
        require(stat.S_IMODE(ROOT_STATE.stat().st_mode) == 0o700, 'ROOT_STATE_MODE')
    else:
        ROOT_STATE.mkdir(mode=0o700)
    install = {'id': INSTALLATION, 'root': str(ROOT), 'project': current['project'],
               'profile': current['profile'], 'owner_uid': 0,
               'installation_instance_id': current['instanceId'], 'state_root': str(STATE)}
    config = {'operations_mode': HOST_ONLY, 'hostname': HOST, 'service_uid': owner.pw_uid,
              'host_admin_enabled': True, 'cloud_storage_enabled': False, 'port': 39446,
              'official_command': ['/opt/server-settings/bin/mcp-1panel', '-transport', 'stdio', '-access-level', 'full'],
              'installation': install}
    executor = {'enabled': True, 'operations_mode': HOST_ONLY, 'hostname': HOST,
                'owner_uid': owner.pw_uid, 'installation_id': INSTALLATION, 'root': str(ROOT),
                'installation_instance_id': current['instanceId'],
                'installation_project': current['project'], 'installation_profile': current['profile'],
                'secret_files': [str(ROOT/'.env'), str(MCP_ENV), str(ENV), str(GATEWAY_ENV),
                                 '/var/lib/server-settings/asa-school-mcp-owner-password',
                                 '/var/lib/server-settings/1panel-api.key']}
    created = []
    try:
        create(CONFIG, json.dumps(config, indent=2)+'\n', mode=0o640, gid=owner.pw_gid); created.append(CONFIG)
        create(ENV, 'PANEL_HOST='+mcp['PANEL_HOST']+'\nPANEL_ACCESS_TOKEN='+mcp['PANEL_ACCESS_TOKEN']+
                    '\nASA_OPS_TOKEN='+secrets.token_hex(32)+'\n'); created.append(ENV)
        create(EXECUTOR, json.dumps(executor, indent=2)+'\n'); created.append(EXECUTOR)
        for name in ('asa-admin-executor.socket', 'asa-admin-executor@.service'):
            text = (SOURCE/'templates'/name).read_text().replace('SocketUser=admins', 'SocketUser='+SERVICE_USER)
            import grp
            text = text.replace('SocketGroup=admins', 'SocketGroup='+grp.getgrgid(owner.pw_gid).gr_name)
            path = Path('/etc/systemd/system')/name
            create(path, text, mode=0o644); created.append(path)
        # This reviewed installer has an explicit host-only branch. It does not
        # run prepare-asa-maintenance, chown the checkout or import ASA code.
        created.append(Path('/etc/systemd/system/asa-operations.service'))
        run(['bash', str(SOURCE/'scripts/install-asa-operations.sh'), '--apply'], timeout=120)
        run(['systemctl', 'daemon-reload'])
        run(['systemctl', 'enable', '--now', 'asa-admin-executor.socket'])
        for attempt in range(10):
            try:
                verify_private()
                break
            except (OSError, Blocked, ValueError):
                if attempt == 9:
                    raise
                time.sleep(0.3)
        create(RECEIPT, json.dumps({'source_sha': source_sha, 'hostname': HOST,
            'source_verification': source_verification,
            'installation_id': INSTALLATION, 'instance_id_sha256': hashlib.sha256(current['instanceId'].encode()).hexdigest(),
            'project': current['project'], 'operations_mode': HOST_ONLY,
            'service_uid': owner.pw_uid, 'gateway_switched': False,
            'application_changed': False}, indent=2)+'\n')
        return {'private_backend': 'accepted', 'tools': 16, 'hostname': HOST,
                'project': current['project'], 'gateway_switched': False, 'source_sha': source_sha}
    except BaseException:
        for unit in ('asa-operations.service', 'asa-admin-executor.socket'):
            subprocess.run(['systemctl', 'disable', '--now', unit], capture_output=True, timeout=30)
        for path in reversed(created):
            path.unlink(missing_ok=True)
        subprocess.run(['systemctl', 'daemon-reload'], capture_output=True, timeout=30)
        # Preserve private state/audit and the disabled account for inspection.
        raise


def switch_gateway():
    verify_private()
    baseline_gateway()
    require(not DROPIN.exists() and not DROPIN.is_symlink()
            and not ROLLBACK.exists() and not ROLLBACK.is_symlink(), 'EXISTING_GATEWAY_SWITCH')
    ROLLBACK.mkdir(mode=0o700)
    create(ROLLBACK/'gateway.env.before', GATEWAY_ENV.read_text())
    create(ROLLBACK/'gateway.service.before', GATEWAY_UNIT.read_text())
    token = environment(ENV)['ASA_OPS_TOKEN']
    lines = GATEWAY_ENV.read_text().splitlines()
    require(sum(line.startswith('PROXY_BEARER_TOKEN=') for line in lines) == 1, 'GATEWAY_TOKEN_FIELD')
    changed = '\n'.join('PROXY_BEARER_TOKEN='+token if line.startswith('PROXY_BEARER_TOKEN=') else line for line in lines)+'\n'
    create(ROLLBACK/'gateway.env.switched', changed)
    try:
        if not DROPIN.parent.exists():
            DROPIN.parent.mkdir(mode=0o755)
        trusted_metadata(DROPIN.parent, directory=True)
        create(DROPIN, NEW_DROPIN, mode=0o644)
        replace(GATEWAY_ENV, changed)
        run(['systemctl', 'daemon-reload'])
        run(['systemctl', 'restart', GATEWAY_UNIT.name])
        run(['systemctl', 'is-active', '--quiet', GATEWAY_UNIT.name])
    except BaseException:
        rollback_gateway()
        raise
    return {'gateway_backend': 'http://127.0.0.1:39446', 'oauth_identity_and_state_preserved': True,
            'public_authenticated_acceptance': False, 'rollback_directory': str(ROLLBACK)}


def rollback_gateway():
    trusted_metadata(ROLLBACK, directory=True)
    for filename in ('gateway.env.before', 'gateway.env.switched', 'gateway.service.before'):
        environment(ROLLBACK/filename) if filename.startswith('gateway.env') else trusted_metadata(ROLLBACK/filename)
    # Do not overwrite a concurrent owner change to credentials or the unit.
    require(GATEWAY_UNIT.read_text() == (ROLLBACK/'gateway.service.before').read_text(), 'GATEWAY_UNIT_CHANGED')
    require(GATEWAY_ENV.read_text() in ((ROLLBACK/'gateway.env.before').read_text(),
                                     (ROLLBACK/'gateway.env.switched').read_text()), 'GATEWAY_ENV_CHANGED')
    if DROPIN.exists():
        trusted_metadata(DROPIN)
        require(DROPIN.read_text() == NEW_DROPIN, 'GATEWAY_DROPIN_CHANGED')
        DROPIN.unlink()
    replace(GATEWAY_ENV, (ROLLBACK/'gateway.env.before').read_text())
    run(['systemctl', 'daemon-reload'])
    run(['systemctl', 'restart', GATEWAY_UNIT.name])
    baseline_gateway()
    return {'gateway_backend': 'http://127.0.0.1:39443', 'oauth_identity_and_state_preserved': True,
            'private_backend_and_audit_retained': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--enable-full-admin', action='store_true')
    parser.add_argument('--expected-host')
    parser.add_argument('--source-sha')
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--repair-gateway-route', action='store_true')
    action.add_argument('--switch-gateway', action='store_true')
    action.add_argument('--rollback-gateway', action='store_true')
    args = parser.parse_args()
    try:
        if not args.apply:
            result = {'mode': 'plan', 'hostname': HOST, 'installation_id': INSTALLATION,
                      'operation': 'repair_gateway_route' if args.repair_gateway_route else 'rollback_gateway' if args.rollback_gateway else 'switch_gateway' if args.switch_gateway else 'enroll_private_backend',
                      'root_owned_application_preserved': True, 'public_ports_added': False,
                      'live_preflight': 'not_run_on_this_host'}
            if os.geteuid() == 0 and socket.gethostname() == HOST:
                if args.repair_gateway_route:
                    trusted_metadata(GATEWAY_UNIT)
                    result['live_preflight'] = 'read_only_route_inspection'
                    result['duplicated_path_present'] = '-- http://127.0.0.1:39443/mcp' in GATEWAY_UNIT.read_text()
                elif not args.switch_gateway and not args.rollback_gateway:
                    current, _ = preflight_install()
                    result.update(live_preflight='passed', project=current['project'])
                elif args.switch_gateway:
                    verify_private(); baseline_gateway(); result['live_preflight'] = 'passed'
            print(json.dumps(result))
            return 0
        require(os.geteuid() == 0 and args.expected_host == HOST == socket.gethostname(), 'EXACT_ROOT_HOST_REQUIRED')
        require(args.rollback_gateway or args.repair_gateway_route or args.enable_full_admin, 'EXPLICIT_FULL_ADMIN_REQUIRED')
        if args.repair_gateway_route:
            result = repair_gateway_route()
        elif args.rollback_gateway:
            result = rollback_gateway()
        elif args.switch_gateway:
            result = switch_gateway()
        else:
            require(isinstance(args.source_sha, str) and re.fullmatch(r'[a-f0-9]{40}', args.source_sha), 'REVIEWED_SOURCE_SHA_REQUIRED')
            result = enroll(args.source_sha)
        print(json.dumps(result))
        return 0
    except (Blocked, OpsError, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        code = str(exc) if isinstance(exc, Blocked) else exc.code if isinstance(exc, OpsError) else type(exc).__name__
        print(json.dumps({'status': 'blocked', 'code': code}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
