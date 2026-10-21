"""Fixed-catalog restore planning and isolated rehearsal; never replace live data."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid

from .adapter import load, runtime, fingerprint, check_entry
from .store import OpsError, digest, now

SNAPSHOT = '20261017T231505Z-65b68074'
MANIFEST_SHA = '8be098e3c37cd43fb704e7f92f300574bb685445d92697223ba0b158ac2e8a01'
ARCHIVE_SHA = '910795bae418b265ac37a8d9ac84e22a92e9c2c870ef11138de4cbeffb7b41e4'


def snapshot():
    from deployment.contracts import verify_backup, read_env
    root = Path('/srv/asa-restore-snapshots')/SNAPSHOT
    if root.is_symlink() or root.resolve() != root:
        raise OpsError('SNAPSHOT_IDENTITY', 'Snapshot store identity differs')
    if hashlib.sha256((root/'manifest.json').read_bytes()).hexdigest() != MANIFEST_SHA:
        raise OpsError('SNAPSHOT_HASH', 'Approved manifest hash differs')
    manifest = verify_backup(root)
    if manifest['createdAt'] != SNAPSHOT:
        raise OpsError('SNAPSHOT_IDENTITY', 'Snapshot identity differs')
    return root, manifest, read_env(root/'private.env')


def release_for_running(install):
    from deployment.contracts import validate_release
    ready = install.readiness()
    candidates = []
    for name in ('last-operation.json', 'installed-release.json'):
        p = install.state/name
        if p.exists():
            value = json.loads(p.read_text());candidates.append(value.get('selected', value))
    for value in candidates:
        try:
            value = validate_release(value)
            if value['revision'] == ready['revision'] and value['schema'] == ready['schema']:
                runtime(install, value)
                return value
        except Exception:
            continue
    raise OpsError('RELEASE_IDENTITY', 'No manifest matches the actual ready runtime')


def sql(install, database, statement):
    if not re.fullmatch(r'[a-z][a-z0-9_]{0,60}', database):
        raise OpsError('DATABASE_IDENTITY', 'Unsafe database identity')
    return install.compose('exec', '-T', '-e', 'RESTORE_DATABASE='+database, 'postgres', 'sh', '-eu', '-c',
            'psql -XAt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$RESTORE_DATABASE"',
            input_data=statement+'\n', capture=True)


def population(install, database):
    return json.loads(sql(install, database, """SELECT json_build_object(
        'accounts',(select count(*) from accounts), 'classrooms',(select count(*) from classrooms),
        'seats',(select count(*) from classroom_student_seats),
        'active_seats',(select count(*) from classroom_student_seats where status='active'),
        'projects',(select count(*) from projects), 'schema',(select max(version) from schema_migrations),
        'last_project_created_at',(select max(created_at) from projects));"""))


def plan(config, payload):
    manager, install = load(config)
    from deployment.system import operation_lock
    with operation_lock(install.root):
        manager.clean_checkout(install)
        root, manifest, private = snapshot()
        selected = release_for_running(install)
        entry = check_entry(config, install, selected)
        current = population(install, install.env['POSTGRES_DB'])
        keys = ('POSTGRES_DB', 'POSTGRES_USER', 'ASA_OBJECT_STORAGE_BUCKET', 'ASA_OBJECT_STORAGE_ENDPOINT')
        if any(private.get(k) != install.env.get(k) for k in keys):
            raise OpsError('RESTORE_TARGET', 'Snapshot and current database/storage targets differ')
        source_schema = int(manifest['runtime']['schema'])
        if source_schema > selected['schema']:
            raise OpsError('RESTORE_SCHEMA', 'Snapshot schema exceeds deployed release')
        return {'kind': 'restore', 'snapshot_id': SNAPSHOT, 'archive_sha256': ARCHIVE_SHA,
                'manifest_sha256': MANIFEST_SHA, 'source_schema': source_schema,
                'source_timestamp_utc': '2026-10-17T23:15:05Z', 'release': selected,
                'fingerprint': fingerprint(install), 'current': current, 'entry': entry,
                'ready_to_rehearse': True, 'ready_to_apply': False,
                'blockers': ['REHEARSAL_AND_ROW_COMPARISON_REQUIRED', 'USER_FLOW_ACCEPTANCE_REQUIRED'],
                'data_mode': 'restore_separate_database_and_objects', 'created_at': now()}


def compare_rows(install, source, current):
    # Only keys/hashes exist in memory. Public results contain aggregate counts.
    tables = sql(install, current, "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename;").splitlines()
    result = {}
    for table in tables:
        if table == 'schema_migrations' or not re.fullmatch(r'[a-z][a-z0-9_]*', table):
            continue
        cols = sql(install, current, "SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=ANY(i.indkey) WHERE i.indrelid='public."+table+"'::regclass AND i.indisprimary ORDER BY a.attnum;").splitlines()
        if any(not re.fullmatch(r'[a-z][a-z0-9_]*', c) for c in cols):
            raise OpsError('DATABASE_SCHEMA', 'Unsupported primary key')
        key = 'jsonb_build_array('+','.join('t."'+c+'"' for c in cols)+')::text' if cols else 'md5(to_jsonb(t)::text)'
        query = 'SELECT '+key+" || chr(9) || md5(to_jsonb(t)::text) FROM public.\""+table+'" t;'
        def inventory(db):
            rows = {}
            for line in sql(install, db, query).splitlines():
                identity, value = line.rsplit('\t', 1)
                if identity in rows:
                    raise OpsError('COMPARISON_KEY', 'Comparison requires unique row identities')
                rows[identity] = value
            return rows
        left, right = inventory(source), inventory(current)
        same = left.keys() & right.keys()
        result[table] = {'snapshot_rows': len(left), 'current_rows': len(right),
            'current_only': len(right.keys()-left.keys()), 'snapshot_only': len(left.keys()-right.keys()),
            'changed': sum(left[k] != right[k] for k in same),
            'identical': sum(left[k] == right[k] for k in same)}
    return result


def rehearse(config, payload):
    manager, install = load(config)
    from deployment.system import operation_lock
    from deployment.backup import restore_check, privileges
    from deployment.releases import apply_release_environment, validate_resolved_images
    from deployment.contracts import atomic_json
    from urllib.parse import urlsplit, urlunsplit
    planned = payload['plan']
    if planned.get('kind') != 'restore' or planned.get('snapshot_id') != SNAPSHOT:
        raise OpsError('RESTORE_PLAN', 'Use a restore plan for the approved snapshot')
    with operation_lock(install.root):
        root, manifest, private = snapshot()
        selected = release_for_running(install)
        if selected != planned['release'] or MANIFEST_SHA != planned['manifest_sha256']:
            raise OpsError('RESTORE_DRIFT', 'Selected release or snapshot differs from plan')
        manager.assert_ci(selected)
        apply_release_environment(install, selected)
        validate_resolved_images(install, selected)
        before = install.identity()
        ident = uuid.uuid4().hex[:16]
        database = 'asa_restore_'+ident+'_test'
        state = Path(config['state_root'])/'restore-rehearsals'/ident
        state.mkdir(mode=0o700, parents=True)
        receipt = {'snapshot_id': SNAPSHOT, 'database': database, 'phase': 'restore_start',
                   'manifest_sha256': MANIFEST_SHA, 'release': selected, 'started_at': now()}
        atomic_json(state/'receipt.json', receipt)
        restore_check(install, root, database)  # preserves ACL; never globals.sql or live DB
        receipt.update(phase='restored', restored=population(install, database))
        atomic_json(state/'receipt.json', receipt)
        uri = urlsplit(install.env['MIGRATION_DATABASE_URL'])
        if uri.hostname != 'postgres' or uri.path != '/'+install.env['POSTGRES_DB']:
            raise OpsError('MIGRATION_TARGET', 'Unexpected migration target')
        install.process_env.update(MIGRATION_DATABASE_URL=urlunsplit(uri._replace(path='/'+database)),
            MIGRATION_EXPECT_DATABASE=database, MIGRATION_CONFIRM='APPLY:'+database)
        for action in ('--plan', '--apply', '--plan'):
            install.compose('run', '--rm', '--no-deps', '-T', '--entrypoint', 'node',
                '-e', 'MIGRATION_DATABASE_URL', '-e', 'MIGRATION_EXPECT_DATABASE', '-e', 'MIGRATION_CONFIRM',
                'migration', 'tools/migrate.mjs', action, capture=True)
        migrated = population(install, database)
        if int(migrated['schema']) != selected['schema']:
            raise OpsError('MIGRATION_SCHEMA', 'Rehearsed database did not reach pinned schema')
        acl = privileges(install, database)
        if not acl or any(not row['rls'] for row in acl if row['relation'] in ('public.projects', 'public.classroom_student_seats')):
            raise OpsError('RESTORE_PRIVILEGES', 'Required application RLS differs')
        comparison = compare_rows(install, database, install.env['POSTGRES_DB'])
        receipt.update(phase='database_migrated', migrated=migrated, comparison=comparison)
        atomic_json(state/'receipt.json', receipt)
        after = install.identity()
        if any(before[n]['id'] != after[n]['id'] for n in before):
            raise OpsError('LIVE_IDENTITY', 'Live containers changed during rehearsal')
        conflicts = {name: value for name, value in comparison.items() if value['current_only'] or value['changed']}
        receipt.update(phase='database_rehearsed', finished_at=now(), ready_to_apply=False,
            blockers=['MINIO_REHEARSAL_REQUIRED', 'USER_FLOW_ACCEPTANCE_REQUIRED']+(['CURRENT_DATA_WOULD_BE_REPLACED'] if conflicts else []),
            conflicting_tables=sorted(conflicts), live_containers_unchanged=True,
            minio_status='not_rehearsed', rehearsal_id=ident)
        atomic_json(state/'receipt.json', receipt)
        return receipt


