"""Fresh-process bridge to the canonical manager. No shell or client-selected paths."""
import contextlib
import datetime as dt
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import time

from .store import OpsError, digest, now


def event(phase):
    print('ASA_OPS_EVENT '+json.dumps({'phase': phase}), file=sys.__stdout__, flush=True)


def load(config):
    root = Path(config['root']).resolve()
    if root.stat().st_uid != os.getuid() or os.getuid() != config['owner_uid']:
        raise OpsError('OWNER', 'Executor must run as the installation owner')
    sys.path.insert(0, str(root / 'tools'))
    manager = importlib.import_module('asa_manager')
    install = manager.Installation(root, config['profile'])
    if install.project != config['project']:
        raise OpsError('IDENTITY', 'Compose project differs from configured installation')
    return manager, install


def fingerprint(install):
    files = ['.env', '.asa/installation.json', '.asa/installed-release.json',
             '.asa/last-operation.json', '.asa/maintenance.json', '.asa/failed-switch.json',
             'tools/asa_manager.py', *install.files]
    return {'checkout_revision': install.git('rev-parse', 'HEAD'),
            'files': {name: hashlib.sha256((install.root/name).read_bytes()).hexdigest()
                      if (install.root/name).exists() else None for name in files}}


def runtime(install, release=None):
    # installed-release may be stale after a failed switch. Match live images to
    # the selected release, then use the manager's full data-identity checks.
    if release:
        from deployment.releases import apply_release_environment
        apply_release_environment(install, release)
    records = install.identity()
    ready = install.readiness(release['revision'] if release else None, release['schema'] if release else None)
    if release:
        for name, ref in release['images'].items():
            if records.get(name, {}).get('image') != ref:
                raise OpsError('IMAGE_DRIFT', 'Live '+name+' image differs from selected release')
    volumes = {name: [m['Name'] for m in records[name].get('mounts', []) if m.get('Type') == 'volume']
               for name in ('postgres', 'minio')}
    return {**ready, 'volumes': volumes}


def discover(manager):
    from deployment import releases
    original = releases.run
    image_id = []
    def tracked(args, **kw):
        result = original(args, **kw)
        if args[:3] == ['docker', 'image', 'inspect'] and '--format' in args:
            image_id.append(result)
        return result
    releases.run = tracked
    try:
        release = manager.discover_release()
    finally:
        releases.run = original
    if not image_id:
        raise OpsError('MANIFEST_PIN', 'Manager did not expose the selected manifest image identity')
    refs = json.loads(original(['docker', 'image', 'inspect', '--format', '{{json .RepoDigests}}', image_id[-1]], capture=True))
    refs = [ref for ref in (refs or []) if '/deployment@sha256:' in ref]
    if not refs:
        raise OpsError('MANIFEST_PIN', 'Selected deployment manifest has no immutable registry digest')
    return release, {'manifest_image_id': image_id[-1], 'manifest_reference': refs[0], 'manifest_sha256': digest(release)}


def check_entry(config, install, release=None):
    from deployment.contracts import attest_editor_env, verify_embedded_entry_http
    entry = config['entry_origin']
    if install.env.get('ASA_UPDATE_ENTRY_ORIGIN') != entry:
        raise OpsError('ENTRY_ORIGIN', 'Configured operator entry differs from manager entry; explicit configuration transition required')
    attest_editor_env(entry, install.env.get('ASA_BLOCKS_PARENT_ORIGIN'), install.env.get('ASA_BLOCKS_RUNTIME_ORIGIN'), install.profile)
    current = install.readiness()
    verify_embedded_entry_http(entry, (release or current)['revision'])
    return {'entry_origin': entry, 'tls': 'trusted', 'editor_origins': 'match'}


def browser_check(config, release):
    command = config.get('acceptance_command')
    if not command:
        return {'status': 'not_configured', 'checks': {}, 'reason': 'Isolated browser acceptance hook is not configured'}
    started = now()
    env = {**os.environ, 'ASA_ACCEPTANCE_ORIGIN': config['entry_origin'],
           'ASA_ACCEPTANCE_REVISION': release['revision'],
           'ASA_ACCEPTANCE_INSTALLATION_ID': config['id']}
    from .maintenance import instance
    generation=instance(config['root'])
    if generation:env['ASA_ACCEPTANCE_INSTANCE_ID']=generation
    result = subprocess.run(command, cwd=config['root'], env=env, capture_output=True, timeout=600, text=True)
    try:
        report = json.loads(result.stdout)
    except ValueError:
        raise OpsError('USER_FLOW', 'Browser acceptance did not return a structured report') from None
    required = {'teacher_login', 'student_login', 'course_open', 'existing_project_open',
                'scratch_save_reopen', 'electronics_save_reopen'}
    ok = (result.returncode == 0 and report.get('revision') == release['revision']
          and report.get('entry_origin') == config['entry_origin']
          and required <= set(report.get('checks', {}))
          and all(report['checks'][k] is True for k in required))
    import re
    error = report.get('error') or {}
    safe_error = {k: v for k, v in error.items() if k in ('stage', 'code')
                  and isinstance(v, str) and re.fullmatch(r'[A-Za-z0-9_]{1,80}', v)}
    return {'status': 'passed' if ok else 'failed', 'checks': {k: report.get('checks', {}).get(k) is True for k in sorted(required)},
            'error': safe_error or None, 'started_at': started, 'finished_at': now(),
            'revision': release['revision'], 'entry_origin': config['entry_origin']}


def forward_release(install,current,selected):
    if int(selected['schema']) < int(current['schema']):
        raise OpsError('RELEASE_DOWNGRADE','Selected release schema is older than the running database')
    if selected['revision'] != current['revision']:
        install.git('fetch','origin','main:refs/remotes/origin/main')
        install.git('merge-base','--is-ancestor',current['revision'],selected['revision'])


def plan(config, payload):
    manager, install = load(config)
    blockers, checks = [], []
    def check(name, fn):
        event(name)
        try:
            value = fn()
            checks.append({'name': name, 'status': 'passed'})
            return value
        except Exception as exc:
            code = getattr(exc, 'code', type(exc).__name__)
            blockers.append({'check': name, 'code': code, 'message': str(exc) if isinstance(exc, OpsError) else 'Preflight failed; inspect the named check on the server'})
            checks.append({'name': name, 'status': 'blocked', 'code': code})
            return None
    check('clean_checkout', lambda: manager.clean_checkout(install))
    def pending():
        if any((install.state/n).exists() for n in ('maintenance.json', 'failed-switch.json')):
            raise OpsError('UNFINISHED_MANAGER', 'A previous manager operation needs verification/finalization')
        last = install.state/'last-operation.json'
        if last.exists() and json.loads(last.read_text()).get('status') == 'failed_switch':
            raise OpsError('FAILED_RECEIPT', 'Last manager receipt records failed_switch; acknowledge is not successful finalization')
    check('previous_operation', pending)
    current = check('runtime_identity', lambda: runtime(install))
    def backup_preflight():
        target = Path(install.settings.get('backupDirectory', install.root/'backups')).resolve()
        if not target.is_dir():
            raise OpsError('BACKUP_DIRECTORY', 'Configured backup directory must already exist')
        with tempfile.TemporaryFile(dir=target) as stream:
            stream.write(b'preflight');stream.flush();os.fsync(stream.fileno())
        db_size = int(install.compose('exec', '-T', 'postgres', 'sh', '-eu', '-c',
                      'psql -XAt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT pg_database_size(current_database());"', capture=True))
        object_size = int(install.compose('exec', '-T', 'minio', 'du', '-sk', '/data', capture=True).split()[0])*1024
        # Uncompressed database + complete object copy + isolated restore budget.
        required = max(int(config.get('minimum_free_bytes', 4*1024**3)), 2*(db_size+object_size)+2*1024**3)
        if min(shutil.disk_usage(target).free, shutil.disk_usage(install.root).free) < required:
            raise OpsError('SPACE', 'Insufficient free space for backup and update')
        return str(target)
    backup = check('backup_permissions_and_space', backup_preflight)
    entry = check('trusted_tls_and_editor', lambda: check_entry(config, install))
    recovery_receipt = None
    for name in ('maintenance.json', 'failed-switch.json', 'last-operation.json'):
        p = install.state/name
        if p.exists():
            candidate = json.loads(p.read_text())
            if candidate.get('selected') and candidate.get('phase') in ('switch', 'verify_entry') and candidate.get('status') != 'success':
                recovery_receipt = {key: candidate.get(key) for key in ('selected', 'previous', 'backup', 'phase')}
                break
    selected = payload.get('pinned_release') or (recovery_receipt or {}).get('selected')
    pin = {'manifest_sha256': digest(selected)} if selected else {}
    def select():
        nonlocal selected, pin
        from deployment.contracts import validate_release
        if selected is None:
            selected, pin = discover(manager)
        selected = validate_release(selected)
        manager.assert_ci(selected)
    check('release_and_exact_ci', select)
    if selected and current:
        check('forward_release',lambda: forward_release(install,current,selected))
    if selected:
        def images_and_migrations():
            manager.fetch_images(selected)
            # Resolve and validate the target source in a disposable Git worktree.
            # A git worktree records shared admin metadata but never switches the live checkout.
            manager.clean_checkout(install)
            install.git('fetch', 'origin', 'main:refs/remotes/origin/main')
            install.git('merge-base', '--is-ancestor', selected['revision'], 'origin/main')
            if current:
                install.git('merge-base', '--is-ancestor', current['revision'], selected['revision'])
            with tempfile.TemporaryDirectory(prefix='asa-plan-', dir=config['state_root']) as td:
                preview = Path(td)/'checkout'
                install.git('worktree', 'add', '--detach', str(preview), selected['revision'])
                try:
                    shutil.copy2(install.root/'.env', preview/'.env')
                    for file in install.files:
                        if not install.git('ls-files', '--', file):
                            shutil.copy2(install.root/file, preview/file)
                    # Match the real project directory for variable interpolation; never run up here.
                    probe = manager.Installation(preview, install.profile)
                    if probe.files != install.files:
                        raise OpsError('IDENTITY_FILES', 'Preview overlay set differs from the real installation')
                    probe.compose_args = ['docker', 'compose', '--project-directory', str(install.root), '-p', install.project]
                    for file in probe.files:
                        probe.compose_args += ['-f', str(preview/file)]
                    manager.apply_release_environment(probe, selected)
                    manager.validate_resolved_images(probe, selected)
                    probe.compose('run', '--rm', '--no-deps', '--entrypoint', 'node', 'migration', 'tools/migrate.mjs', '--plan')
                finally:
                    install.git('worktree', 'remove', '--force', str(preview))
        # Do not launch a migration-plan container when identity or origin checks failed.
        if not blockers:
            check('pinned_images_and_migration_plan', images_and_migrations)
        else:
            checks.append({'name': 'pinned_images_and_migration_plan', 'status': 'not_run'})
    if not config.get('acceptance_command'):
        blockers.append({'check': 'browser_acceptance', 'code': 'NOT_CONFIGURED', 'message': 'Configure the isolated browser acceptance hook before an update'})
    return {'installation_instance_id':install.settings.get('instanceId'), 'ready': not blockers, 'blockers': blockers, 'checks': checks, 'release': selected,
            'pin': pin, 'current': current, 'fingerprint': fingerprint(install),
            'entry': entry, 'backup_directory': backup, 'data_mode': 'preserve',
            'recovery_receipt': recovery_receipt,
            'data_source': {'kind': 'existing_postgresql_minio', 'snapshot_id': None, 'snapshot_time': None}}


def verify(config, payload):
    manager, install = load(config)
    release = payload['release']
    from deployment.contracts import validate_release
    validate_release(release)
    current = runtime(install, release)
    entry = check_entry(config, install, release)
    browser = browser_check(config, release)
    return {'technical_status': 'passed', 'accepted': browser['status'] == 'passed',
            'runtime': current, 'entry': entry, 'browser': browser, 'data_mode': 'preserve',
            'data_source': {'kind': 'existing_postgresql_minio', 'snapshot_id': None, 'snapshot_time': None}}


# Receipt provenance is computed locally and never exposes its raw content.
# A historical receipt is only evidence of the historical operation.
_MAX_RECEIPT_BYTES = 65536
_SAFE_MANAGER_CODES = frozenset({
    'RETRY_NEW_MANAGER', 'SEARCH_SECRET_INVALID', 'IO_OR_CONFIGURATION',
    'MAINTENANCE', 'FAILED_SWITCH', 'BACKUP', 'BACKUP_SERVICES',
    'BACKUP_STORAGE', 'BACKUP_CONFIGURATION', 'SPACE', 'WINDOW_CLOSED',
    'RELEASE_DOWNGRADE', 'IMAGE_DRIFT', 'IDENTITY', 'DIRTY', 'ORIGIN',
    'MANIFEST_PIN', 'USER_FLOW', 'ENTRY_ORIGIN',
})


def _receipt_snapshot(path):
    """Read at most 64 KiB of a regular receipt; refuse symlinks or changing files."""
    try:
        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, 'rb') as stream:
            metadata = os.fstat(stream.fileno())
            if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
                return {'invalid': True}
            raw = stream.read(_MAX_RECEIPT_BYTES + 1)
            final_metadata = os.fstat(stream.fileno())
    except FileNotFoundError:
        return None
    except OSError:
        return {'invalid': True}
    if len(raw) > _MAX_RECEIPT_BYTES or (
        metadata.st_dev, metadata.st_ino, metadata.st_mtime_ns, metadata.st_size
    ) != (
        final_metadata.st_dev, final_metadata.st_ino, final_metadata.st_mtime_ns, final_metadata.st_size
    ):
        return {'invalid': True}
    return {'identity': (metadata.st_dev, metadata.st_ino, metadata.st_mtime_ns,
                         metadata.st_size, hashlib.sha256(raw).hexdigest()),
            'raw': raw}


def _attributed_receipt(before, after, started_at, release, previous):
    if after is None:
        return {}, 'missing'
    if after.get('invalid'):
        return {}, 'invalid'
    if before and not before.get('invalid') and before['identity'] == after['identity']:
        return {}, 'stale'
    try:
        receipt = json.loads(after['raw'].decode('utf-8'))
        if not isinstance(receipt, dict):
            return {}, 'invalid'
        timestamp = dt.datetime.fromisoformat(receipt['at'])
        if timestamp.tzinfo is None:
            return {}, 'invalid'
        timestamp = timestamp.astimezone(dt.timezone.utc)
        if not started_at - dt.timedelta(seconds=2) <= timestamp <= dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=2):
            return {}, 'mismatch'
    except (ValueError, UnicodeError, KeyError, TypeError, OverflowError):
        return {}, 'invalid'
    if receipt.get('selected') != release:
        return {}, 'mismatch'
    old = receipt.get('previous')
    if previous is not None and (not isinstance(old, dict) or
                                 any(old.get(key) != previous.get(key) for key in ('revision', 'schema'))):
        return {}, 'mismatch'
    status, phase = receipt.get('status'), receipt.get('phase')
    if not ((status == 'success' and phase in ('verify_entry', 'complete')) or
            (status == 'failed_switch' and phase in ('switch', 'verify_entry'))):
        return {}, 'invalid'
    return receipt, 'current'


def _verified_manager_backup(install, exported, previous):
    """Independently check the export from this invocation, not an old receipt."""
    if exported is None:
        return {'status': 'not_observed', 'path': None, 'files': 0}
    try:
        from deployment.contracts import verify_backup
        configured = Path(install.settings.get('backupDirectory', install.root / 'backups')).resolve()
        source = Path(exported)
        destination = source.resolve()
        if source.is_symlink() or configured not in destination.parents or not destination.is_dir():
            raise ValueError('Backup path is not an owned export directory')
        manifest = verify_backup(destination)
        runtime = manifest.get('runtime')
        if previous is not None and (not isinstance(runtime, dict) or
                                     any(runtime.get(key) != previous.get(key) for key in ('revision', 'schema'))):
            raise ValueError('Backup runtime does not match the prior running version')
        files = manifest.get('files')
        if not isinstance(files, dict):
            raise ValueError('Backup verification did not report files')
        return {'status': 'verified', 'path': str(destination), 'files': len(files)}
    except Exception:
        # The export remains untouched for server-side forensic inspection.
        return {'status': 'unverified', 'path': None, 'files': 0}


def _safe_manager_error(stderr):
    import re
    match = re.search(r'(?m)^BLOCKED ([A-Z][A-Z0-9_]{0,63}):', stderr)
    if match and match.group(1) in _SAFE_MANAGER_CODES:
        return match.group(1)
    return 'MANAGER_EXIT_NONZERO'


def update(config, payload):
    manager, install = load(config)
    planned = payload['plan']
    if fingerprint(install) != planned['fingerprint']:
        raise OpsError('STALE_PLAN', 'Installation changed since the plan was prepared')
    if not planned['ready']:
        raise OpsError('BLOCKED_PLAN', 'Plan contains unresolved blockers')
    from deployment.contracts import validate_release
    release = validate_release(planned['release'])
    if digest(release) != planned['pin']['manifest_sha256']:
        raise OpsError('MANIFEST_PIN', 'Selected release hash differs')
    # Revalidate nondowntime prerequisites, retaining the exact approved manifest.
    recheck = plan(config, {'pinned_release': release})
    if not recheck['ready']:
        return {'manager_exit_code': None, 'status': 'blocked_preflight', 'preflight': recheck}
    manager.discover_release = lambda: json.loads(json.dumps(release))
    transition = {'stage': 'manager_update'}
    for name, phase in [('doctor', 'doctor'), ('fetch_images', 'download_images'),
                        ('selected_source', 'select_source'), ('export_backup', 'backup')]:
        original = getattr(manager, name)
        def wrapped(*args, _original=original, _phase=phase, **kw):
            transition['stage'] = _phase
            event(_phase)
            if _phase == 'select_source':
                transition['fingerprint'] = fingerprint(args[0])
            value = _original(*args, **kw)
            if _phase == 'backup' and value is not None:
                transition['backup_path'] = str(value)
                transition['stage'] = 'post_backup'
                event('post_backup')
            return value
        setattr(manager, name, wrapped)
    receipt_path = install.state / 'last-operation.json'
    before_receipt = _receipt_snapshot(receipt_path)
    started_at = dt.datetime.now(dt.timezone.utc)
    event('manager_update')
    errors = io.StringIO()
    with contextlib.redirect_stderr(errors):
        code = manager.main(['update', '--root', config['root'], '--profile', config['profile']])
    receipt, receipt_state = _attributed_receipt(
        before_receipt, _receipt_snapshot(receipt_path), started_at, release, planned['current'])
    backup = _verified_manager_backup(install, transition.get('backup_path'), planned['current'])
    if receipt_state == 'current':
        backup_pointer = receipt.get('backup')
        if (not isinstance(backup_pointer, str) or not backup_pointer or
                backup['status'] != 'verified' or
                Path(backup_pointer).resolve() != Path(backup['path'])):
            receipt, receipt_state = {}, 'backup_mismatch'
    if receipt_state == 'current' and (code == 0) != (receipt['status'] == 'success'):
        receipt, receipt_state = {}, 'conflicting_exit'
    no_change = bool(code == 0 and receipt_state != 'current'
                     and transition['stage'] == 'doctor' and backup['status'] == 'not_observed'
                     and fingerprint(install)['files']['.asa/last-operation.json']
                     == planned['fingerprint']['files']['.asa/last-operation.json']
                     and planned['current']['revision'] == release['revision'])
    if code != 0:
        manager_status = receipt['status'] if receipt_state == 'current' else 'failed'
        phase = receipt['phase'] if receipt_state == 'current' else 'failed'
    elif receipt_state == 'current':
        manager_status, phase = receipt['status'], receipt['phase']
    elif no_change:
        manager_status, phase = 'no_change', 'complete'
    else:
        manager_status, phase = 'not_written', 'unverified'
    error = {'code': _safe_manager_error(errors.getvalue()), 'stage': transition['stage']} if code != 0 else None
    checkout = install.git('rev-parse', 'HEAD')
    return {'manager_exit_code': code, 'manager_status': manager_status, 'phase': phase,
            'receipt_attributed': receipt_state == 'current', 'receipt_evidence': receipt_state,
            'error': error, 'no_change': no_change,
            'backup': backup['path'], 'backup_status': backup['status'], 'backup_files': backup['files'],
            'selected_revision': release['revision'], 'checkout_revision': checkout,
            'transition_fingerprint': transition.get('fingerprint'),
            'retry_new_manager': bool(code == 2 and error['code'] == 'RETRY_NEW_MANAGER'
                                      and transition['stage'] == 'select_source'
                                      and backup['status'] == 'not_observed' and receipt_state != 'current'
                                      and checkout == release['revision']
                                      and planned['fingerprint']['checkout_revision'] != release['revision']
                                      and not any((install.state/n).exists() for n in ('maintenance.json', 'failed-switch.json')))}

def continuation(config, payload):
    manager, install = load(config)
    planned = payload['plan']
    manager.clean_checkout(install)
    if install.git('rev-parse', 'HEAD') != planned['release']['revision']:
        raise OpsError('MANAGER_CONTINUATION', 'Checkout is not the pinned manager revision')
    current = fingerprint(install)
    for key in ('.env', '.asa/installation.json', '.asa/installed-release.json',
                '.asa/last-operation.json', '.asa/maintenance.json', '.asa/failed-switch.json'):
        if current['files'][key] != payload['transition_fingerprint']['files'][key]:
            raise OpsError('MANAGER_CONTINUATION', 'Runtime settings or manager state changed during source transition')
    # This continuation fingerprint is internal to the already approved immutable
    # plan; its release and image pins remain byte-identical.
    return dict(planned, fingerprint=current)


def finalize(config, payload):
    manager, install = load(config)
    planned = payload['plan']
    release = planned['release']
    from deployment.contracts import atomic_json, verify_backup, validate_release
    from deployment.system import operation_lock
    validate_release(release)
    if digest(release) != planned['pin']['manifest_sha256']:
        raise OpsError('MANIFEST_PIN', 'Release integrity failed')
    with operation_lock(install.root):
        event('verify_before_finalize')
        accepted = verify(config, {'release': release})
        if not accepted['accepted']:
            error = (accepted.get('browser') or {}).get('error') or {}
            detail = ('; stage='+error['stage']+', code='+error.get('code', 'unknown')) if error.get('stage') else ''
            raise OpsError('ACCEPTANCE_PENDING', 'Required browser acceptance has not passed'+detail)
        last_path = install.state/'last-operation.json'
        last = json.loads(last_path.read_text()) if last_path.exists() else {}
        candidates = [last]
        for name in ('maintenance.json', 'failed-switch.json'):
            p = install.state/name
            if p.exists():
                candidates.append(json.loads(p.read_text()))
        intent = install.state/'mcp-finalize-intent.json'
        if intent.exists():
            saved = json.loads(intent.read_text())
            if saved.get('release') == release:
                candidates.append(saved.get('receipt', {}))
        matches = [value for value in candidates if value.get('selected') == release
                   and value.get('phase') in ('switch', 'verify_entry', 'complete')]
        if not matches:
            raise OpsError('FINALIZE_RECEIPT', 'No matching interrupted switch receipt')
        last = matches[0]
        backup = Path(last.get('backup') or '').resolve()
        backup_root = Path(install.settings.get('backupDirectory', install.root/'backups')).resolve()
        if backup_root not in backup.parents:
            raise OpsError('BACKUP_IDENTITY', 'Receipt backup is outside the configured backup store')
        manifest = verify_backup(backup)
        if manifest.get('runtime') != last.get('previous'):
            raise OpsError('BACKUP_IDENTITY', 'Backup runtime does not match previous deployment')
        for name in ('maintenance.json', 'failed-switch.json'):
            marker = install.state/name
            if marker.exists() and json.loads(marker.read_text()).get('selected') != release:
                raise OpsError('FINALIZE_RECEIPT', 'Pending marker belongs to another release')
        # Journal intent first. Repeating finalize can finish metadata reconciliation,
        # but never executes migrations or Compose up.
        atomic_json(intent, {'release': release, 'accepted': accepted, 'receipt': last, 'at': now()})
        atomic_json(install.state/'installed-release.json', release)
        manager.save_settings(install, install.settings.get('automaticUpdates', False))
        install.receipt({**last, 'status': 'success', 'phase': 'complete', 'entryOrigin': config['entry_origin'],
                         'entryHttp': 'pass', 'userFlow': 'pass', 'mcpFinalize': True, 'acceptance': accepted})
        for name in ('maintenance.json', 'failed-switch.json'):
            p = install.state/name
            if p.exists():
                p.rename(install.state/(name+'.finalized-'+str(time.time_ns())))
        intent.unlink()
    return {'status': 'success', 'release': release, 'backup': str(backup), 'acceptance': accepted,
            'migration_repeated': False, 'containers_restarted': False}


def main():
    config = json.loads(Path(sys.argv[1]).read_text())
    payload = json.loads(Path(sys.argv[3]).read_text())
    action = sys.argv[2]
    if action not in ('plan', 'verify', 'update', 'finalize', 'continuation', 'restore_plan', 'restore_rehearse'):
        raise SystemExit(2)
    # Suppress manager prints in JSON output; subprocess output is captured by
    # worker in a private file and never returned directly over MCP.
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            if action == 'plan':
                from contextlib import ExitStack
                # The module must first come from the configured trusted checkout.
                manager, install = load(config['installation'])
                from deployment.system import operation_lock
                with operation_lock(install.root):
                    result = plan(config['installation'], payload)
            elif action.startswith('restore_'):
                from . import restore
                result = getattr(restore, action.removeprefix('restore_'))(config['installation'], payload)
            else:
                result = globals()[action](config['installation'], payload)
        print('ASA_OPS_RESULT '+json.dumps({'ok': True, 'result': result}), file=sys.__stdout__, flush=True)
    except Exception as exc:
        print('ASA_OPS_RESULT '+json.dumps({'ok': False, 'error': {'code': getattr(exc, 'code', type(exc).__name__),
              'message': str(exc) if isinstance(exc, OpsError) else 'Manager check failed; inspect the named stage'}}), file=sys.__stdout__, flush=True)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
