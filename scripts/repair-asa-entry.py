#!/usr/bin/env python3
"""Controlled, fixed-host origin repair. Default is plan; no release/data update."""
import argparse
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys
import time

ROOT = Path('/srv/asa-lab')
ORIGIN = 'https://test.asa-lab.ru'
KEYS = ('ASA_UPDATE_ENTRY_ORIGIN', 'ASA_BLOCKS_PARENT_ORIGIN', 'ASA_BLOCKS_RUNTIME_ORIGIN')


def owner_stage(apply):
    assert os.getuid() == pwd.getpwnam('admins').pw_uid
    sys.path.insert(0, str(ROOT/'tools'))
    import asa_manager as manager
    from deployment.releases import apply_release_environment, validate_resolved_images
    from deployment.contracts import attest_editor_env
    install = manager.Installation(ROOT, 'production')
    assert install.project == 'asa-lab-dev'
    manager.clean_checkout(install)
    before = install.identity()
    ready = install.readiness()
    last = json.loads((install.state/'last-operation.json').read_text())
    release = last['selected']
    assert release['revision'] == ready['revision'] and release['schema'] == ready['schema']
    assert all(before[name]['image'] == release['images'][name] for name in ('api', 'web', 'scratch', 'minio'))
    apply_release_environment(install, release)  # never use the stale installed receipt's images
    previous_origins = {k: install.env.get(k) for k in KEYS}
    expected = dict(install.env)
    expected.update({key: ORIGIN for key in KEYS})
    public = {x.strip() for x in expected.get('ASA_PUBLIC_WEB_ORIGINS', '').split(',') if x.strip()}
    public.add(ORIGIN)
    expected['ASA_PUBLIC_WEB_ORIGINS'] = ','.join(sorted(public))
    attest_editor_env(*(expected[key] for key in KEYS), install.profile)
    install.env = expected
    install.process_env.update(expected)
    apply_release_environment(install, release)  # release pins override stale private build/schema hints
    validate_resolved_images(install, release)
    if not apply:
        return {'origin': ORIGIN, 'revision': ready['revision'], 'schema': ready['schema'],
                'services': ['api', 'web', 'scratch'], 'previous_origins': previous_origins}
    target = ROOT/'.env'
    lines = target.read_text().splitlines()
    changes = {k: expected[k] for k in (*KEYS, 'ASA_PUBLIC_WEB_ORIGINS')}
    remaining = dict(changes)
    output = []
    for line in lines:
        key = line.split('=', 1)[0]
        if key in changes:
            if key in remaining:
                output.append(key+'='+remaining.pop(key))
        else:
            output.append(line)
    output.extend(key+'='+value for key, value in remaining.items())
    temp = target.with_name('.env.entry-repair.tmp')
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write('\n'.join(output)+'\n');stream.flush();os.fsync(stream.fileno())
    os.replace(temp, target)
    install.compose('up', '-d', '--no-deps', '--no-build', '--pull', 'never', 'api', 'scratch', 'web', capture=True)
    for attempt in range(60):
        try:
            result = install.readiness(release['revision'], release['schema'])
            manager.verify_embedded_entry_http(ORIGIN, release['revision'])
            break
        except Exception:
            if attempt == 59:
                raise
            time.sleep(2)
    after = install.identity()
    assert all(after[n]['id'] == before[n]['id'] and after[n]['mounts'] == before[n]['mounts'] for n in ('postgres', 'minio'))
    assert all(after[n]['image'] == before[n]['image'] for n in ('api', 'web', 'scratch'))
    return {'origin': ORIGIN, 'revision': result['revision'], 'schema': result['schema'],
            'entry_http': 'pass', 'postgres_minio_unchanged': True, 'images_unchanged': True,
            'migrations_run': False, 'receipts_finalized': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--owner-stage', choices=['check', 'apply'], help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.owner_stage:
        print(json.dumps(owner_stage(args.owner_stage == 'apply')))
        return
    assert os.geteuid() == 0, 'Run as root for host resolution and private rollback capture'
    def owner(stage):
        p = subprocess.run(['runuser', '-u', 'admins', '--', 'python3', str(Path(__file__).resolve()),
                            '--owner-stage', stage], capture_output=True, text=True)
        if p.returncode:
            raise RuntimeError('Owner stage '+stage+' failed; no raw environment/output is published')
        return json.loads(p.stdout)
    with (ROOT/'.asa/operation.lock').open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        planned = owner('check')
        hosts = Path('/etc/hosts')
        text = hosts.read_text()
        for line in text.splitlines():
            fields = line.split('#', 1)[0].split()
            if 'test.asa-lab.ru' in fields[1:]:
                assert fields[0] == '127.0.0.1', 'Conflicting existing host mapping; review explicitly'
        probe = subprocess.run(['curl', '--noproxy', '*', '--resolve', 'test.asa-lab.ru:443:127.0.0.1',
                                '--max-time', '15', '--fail', '--silent', '--output', '/dev/null', ORIGIN+'/health/ready'])
        assert probe.returncode == 0, 'Local domain TLS entry is not ready'
        if not args.apply:
            print(json.dumps({'mode': 'plan', **planned, 'host_resolution': 'Ubuntu-only domain to 127.0.0.1'}))
            return
        backup = Path('/var/backups')/('asa-entry-'+dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
        backup.mkdir(mode=0o700)
        shutil.copy2(hosts, backup/'hosts')
        shutil.copy2(ROOT/'.env', backup/'app.env')
        shutil.copytree(ROOT/'.asa', backup/'manager-state')
        (backup/'plan.json').write_text(json.dumps(planned, indent=2))
        if not any('test.asa-lab.ru' in l.split('#', 1)[0].split()[1:] for l in text.splitlines()):
            with hosts.open('a') as stream:
                stream.write('\n127.0.0.1 test.asa-lab.ru # ASA local entry; preserve TLS hostname\n')
                stream.flush();os.fsync(stream.fileno())
        result = owner('apply')
        print(json.dumps({'backup': str(backup), **result}))


if __name__ == '__main__':
    main()
