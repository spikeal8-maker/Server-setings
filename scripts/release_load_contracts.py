"""Pinned, read-only portal API contracts and secret-free load evidence helpers."""
import hashlib
import json
import math
from pathlib import Path
import re
import stat
import subprocess
import uuid
import struct
import zlib
from datetime import datetime, timezone

PROFILES = (20, 50, 100)
UUID = re.compile(r'^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$')
CATALOG = Path(__file__).resolve().parents[1] / 'profiles' / 'school-route-inventory.json'
TOKEN = re.compile(r'^[A-Za-z0-9._~-]{32,4096}$')


class ContractError(ValueError):
    pass


def require(value, message):
    if not value:
        raise ContractError(message)


def field(data, dotted):
    for name in dotted.split('.'):
        if not isinstance(data, dict) or name not in data:
            raise ContractError('missing response contract field: ' + dotted)
        data = data[name]
    return data


def contract(data, fields):
    require(isinstance(data, dict) and 'error' not in data, 'expected data object, not HTML/error envelope')
    for dotted, kind in fields.items():
        value = field(data, dotted)
        require(type(value) is {'list': list, 'object': dict, 'string': str, 'bool': bool, 'int': int}[kind],
                'response contract type: ' + dotted)
    return data


def quantiles(values):
    ordered = sorted(values)
    if not ordered:
        return None
    def q(p):
        return ordered[max(0, math.ceil(len(ordered) * p) - 1)]
    return {'count': len(ordered), 'p50Ms': q(.5), 'p95Ms': q(.95), 'p99Ms': q(.99), 'maxMs': ordered[-1]}


def safe_code(value):
    # Never forward arbitrary provider/API messages or purported error codes.
    allowed = {'too_many_requests', 'too_many_attempts', 'ai_image_not_admitted', 'ai_image_busy',
               'ai_ollama_busy', 'ai_scope_denied', 'ai_session_required', 'ai_provider_unavailable',
               'ai_image_cancelled', 'unauthorized', 'forbidden', 'project_not_found', 'validation_error'}
    return value if isinstance(value, str) and value in allowed else 'unclassified_api_error'


def private_fixture(path, origin, users, profile):
    path = Path(path)
    for node in (path.absolute(), *path.absolute().parents):
        if node.exists():
            require(not node.is_symlink(), 'fixture cannot traverse symbolic links')
            require(not getattr(node.lstat(), 'st_file_attributes', 0) & 0x400, 'fixture cannot traverse reparse points')
    require(not path.is_symlink() and stat.S_ISREG(path.stat().st_mode) and path.stat().st_nlink == 1, 'fixture must be a single regular file')
    if __import__('os').name != 'nt':
        require(path.stat().st_mode & 0o077 == 0, 'fixture must have mode 0600')
    require(path.stat().st_size <= 2 * 1024 * 1024, 'fixture too large')
    f = json.loads(path.read_text(encoding='utf-8'))
    require(f.get('authorizedSyntheticFixture') is True and f.get('origin') == origin, 'explicit synthetic fixture/exact origin required')
    seats = f.get('seats', [])
    require(isinstance(seats, list) and len(seats) >= users, 'insufficient synthetic pupils')
    for seat in seats[:users]:
        require(isinstance(seat, dict) and UUID.fullmatch(str(seat.get('seat', ''))) is not None,
                'synthetic seat must have UUID')
        require(isinstance(seat.get('token'), str) and TOKEN.fullmatch(seat['token']) is not None,
                'invalid synthetic session token')
    require(len({s['seat'] for s in seats[:users]}) == users and len({s['token'] for s in seats[:users]}) == users,
            'distinct synthetic pupils/sessions required')
    for layer, reference in f.get('evidenceReferences', {}).items():
        require(isinstance(layer, str) and re.fullmatch(r'[a-z0-9-]{1,80}', layer)
                and isinstance(reference, str) and re.fullmatch(r'sha256:[a-f0-9]{64}', reference),
                'external evidence references must be opaque SHA256 digests, never URLs/tokens')
    if profile == 'mixed':
        expected = f.get('expectedRequiredLogSourceIds', [])
        require(isinstance(expected, list) and bool(expected) and all(isinstance(v, str) and re.fullmatch(r'[A-Za-z0-9:_./-]{1,160}', v) for v in expected)
                and len(set(expected)) == len(expected), 'mixed fixture requires pinned nonempty expectedRequiredLogSourceIds')
        for role in ('teacher', 'admin'):
            actor = f.get('roles', {}).get(role, {})
            require(UUID.fullmatch(str(actor.get('accountId', ''))) is not None and TOKEN.fullmatch(str(actor.get('token', ''))) is not None,
                    'mixed profile requires a separate synthetic ' + role)
        resources = f.get('resources', {})
        for key in ('classroomId', 'classProjectId', 'publicationId', 'publicCourseId', 'courseRunId', 'lessonId'):
            require(UUID.fullmatch(str(resources.get(key, ''))) is not None, 'mixed fixture resource missing: ' + key)
    return f


def required_log_coverage(data, expected_ids, now=None):
    """Finite, operator-pinned connected-source coverage; never global-history proof."""
    coverage = data.get('requiredCoverage', {})
    require(coverage.get('version') == 1 and coverage.get('policy') == 'asa-connected-v1'
            and coverage.get('state') == 'ok' and coverage.get('native', {}).get('state') == 'ok'
            and coverage.get('historyAudit', {}).get('state') == 'ok',
            'required connected log coverage/native health not verified')
    actual = coverage.get('requiredSourceIds', [])
    require(bool(expected_ids) and isinstance(actual, list) and len(actual) == len(set(actual))
            and set(actual) == set(expected_ids), 'required log source set differs from operator expectation')
    rows = coverage.get('sources', [])
    require(isinstance(rows, list) and len({r.get('source') for r in rows}) == len(rows), 'invalid required coverage rows')
    indexed = {r.get('source'): r for r in rows}
    now = now or datetime.now(timezone.utc)
    for source in actual:
        row = indexed.get(source, {})
        require(row.get('state') == 'ok', 'required log source missing/error/pending/stale')
        require(type(row.get('requireThrough')) is bool, 'required source must declare through-window policy')
        age = row.get('maxAgeSeconds')
        require(type(age) in (int, float) and math.isfinite(age) and 0 < age <= 86400, 'invalid required-source freshness budget')
        for key in ('checkedAt', 'collectedThrough'):
            if key == 'collectedThrough' and row.get('requireThrough') is False:
                continue
            try:
                stamp = datetime.fromisoformat(row[key].replace('Z', '+00:00'))
                delta = (now - stamp).total_seconds()
            except (KeyError, TypeError, ValueError, AttributeError):
                raise ContractError('required log source timestamp missing/invalid')
            require(0 <= delta <= age, 'required log source timestamp stale/future')
    return coverage


def route_inventory(source_root=None, revision=None):
    catalog = json.loads(CATALOG.read_text(encoding='utf-8'))
    require(len({r['id'] for r in catalog['routes']}) == len(catalog['routes']), 'duplicate inventory route')
    for route in catalog['routes']:
        require(bool(route['roles']) and ('checks' in route or 'notRunReason' in route), 'unclassified route')
    if source_root is not None:
        source_root = Path(source_root).resolve()
        actual = subprocess.run(['git', '-C', str(source_root), 'rev-parse', 'HEAD'], capture_output=True, text=True, check=True).stdout.strip()
        require(actual == revision, 'source checkout differs from tested runtime revision')
        dirty = subprocess.run(['git', '-C', str(source_root), 'status', '--porcelain'], capture_output=True, text=True, check=True).stdout
        require(not dirty.strip(), 'source checkout is dirty')
        for relative, expected in catalog['sourceFiles'].items():
            path = (source_root / relative).resolve()
            require(path.is_relative_to(source_root) and path.is_file(), 'inventory source path missing')
            blob = subprocess.run(['git', '-C', str(source_root), 'show', actual + ':' + relative], capture_output=True, check=True).stdout
            require(hashlib.sha256(blob).hexdigest() == expected, 'route/API contract drift: ' + relative)
    return catalog


def coverage(catalog, passed_checks):
    return [{'id': r['id'], 'route': r['route'], 'roles': r['roles'],
             'http': ('DATA_CONTRACT_VERIFIED' if r.get('checks') and set(r['checks']) <= passed_checks else 'NOT_RUN'),
             'requiredChecks': r.get('checks', []),
             'browser': 'NOT_RUN', 'reason': r.get('notRunReason', 'HTTP data checks do not execute this page in a browser')}
            for r in catalog['routes']]


def numeric_telemetry(data):
    fields = ('uptimeSeconds', 'eventLoopDelayMs.p50', 'eventLoopDelayMs.p99', 'eventLoopDelayMs.max',
              'memory.rssMb', 'memory.heapUsedMb', 'host.cpuUsedByApiPercent', 'host.logicalCpuCount',
              'host.memoryTotalMb', 'host.memoryUsedPercent', 'database.total', 'database.idle', 'database.waiting',
              'requests.total', 'requests.inFlight', 'requests.durationMs.p50', 'requests.durationMs.p95', 'requests.durationMs.p99')
    out = {}
    for key in fields:
        try:
            value = field(data, key)
        except ContractError:
            continue
        if type(value) in (int, float) and math.isfinite(value):
            out[key] = value
    require('memory.rssMb' in out and 'requests.total' in out and 'database.waiting' in out, 'runtime telemetry unavailable/incomplete')
    return out


def local_ai(status):
    contract(status, {'enabled': 'bool', 'provider': 'string', 'model': 'string', 'availableModels': 'list'})
    require(status['enabled'] is True and status['provider'] == 'ollama' and status['model'] == 'ollama-local'
            and 'ollama-local' in status['availableModels'], 'AI workload requires active local Ollama; paid/default fallback forbidden')


def png_dimensions(raw):
    require(isinstance(raw, bytes) and len(raw) <= 8 * 1024 * 1024 and raw.startswith(b'\x89PNG\r\n\x1a\n'), 'invalid PNG signature/size')
    offset, dimensions, image_data, ended = 8, None, False, False
    while offset + 12 <= len(raw):
        length, kind = struct.unpack('!I4s', raw[offset:offset+8])
        require(length <= len(raw) - offset - 12, 'truncated PNG chunk')
        data = raw[offset+8:offset+8+length]
        crc = struct.unpack('!I', raw[offset+8+length:offset+12+length])[0]
        require(zlib.crc32(kind+data) == crc, 'PNG checksum mismatch')
        if dimensions is None:
            require(kind == b'IHDR' and length == 13, 'missing PNG IHDR')
            dimensions = struct.unpack('!II', data[:8])
        image_data |= kind == b'IDAT'
        offset += 12 + length
        if kind == b'IEND':
            require(length == 0 and offset == len(raw), 'PNG trailing data')
            ended = True
            break
    require(ended and image_data and dimensions is not None, 'incomplete PNG')
    return dimensions


# Only source-backed GETs. Never accept arbitrary fixture endpoints or bodies.
READS = {
    'providers': (None, '/api/auth/providers', {'providers': 'list'}),
    'public-list': (None, '/api/public-projects?limit=10', {'items': 'list'}),
    'publication': (None, '/api/public-projects/{publicationId}', {'publication.publicationId': 'string', 'publication.title': 'string'}),
    'modules': (0, '/api/modules', {'items': 'list'}),
    'learning': (0, '/api/class-join/me/course-runs', {'items': 'list'}),
    'assignments': (0, '/api/class-join/me/assignments', {'items': 'list'}),
    'results': (0, '/api/class-join/me/results', {'items': 'list'}),
    'knowledge': (0, '/api/gallery/knowledge?limit=10', {'items': 'list'}),
    'knowledge-course': (0, '/api/gallery/knowledge/{publicCourseId}', {'title': 'string', 'versionNumber': 'int', 'sections': 'list'}),
    'collections': (0, '/api/collections', {'items': 'list'}),
    'games': (0, '/api/games/access?game=chess', {'granted': 'bool', 'reason': 'string'}),
    'ai-status': (0, '/api/ai/status', {'enabled': 'bool', 'provider': 'string', 'availableModels': 'list'}),
    'classrooms': ('teacher', '/api/classrooms', {'items': 'list', 'meta.total': 'int'}),
    'classroom': ('teacher', '/api/classrooms/{classroomId}', {'classroom.id': 'string', 'classroom.title': 'string'}),
    'roster': ('teacher', '/api/classrooms/{classroomId}/roster', {'items': 'list'}),
    'class-project-list': ('teacher', '/api/projects?scope=classroom&classroomId={classroomId}&limit=100', {'items': 'list'}),
    'class-editor-specific': ('teacher', '/api/projects/{classProjectId}', {'project.id': 'string', 'project.classroomId': 'string', 'project.scope': 'string', 'draft.document': 'object'}),
    'courses': ('teacher', '/api/courses', {'items': 'list'}),
    'activities': ('teacher', '/api/learning/activities', {'items': 'list'}),
    'attending': ('teacher', '/api/class-join/account/classes', {'items': 'list'}),
    'account': ('teacher', '/api/account/profile', {'username': 'string', 'displayName': 'string', 'capabilities': 'list'}),
    'admin-me': ('admin', '/api/admin/v1/me', {'administrator': 'bool', 'accountId': 'string', 'scopes': 'list'}),
    'admin-overview': ('admin', '/api/admin/v1/dashboard?scopeKind=platform&range=24h', {'generatedAt': 'string', 'summary': 'object', 'timeline': 'list'}),
    'admin-ai': ('admin', '/api/admin/v1/ai', {'enabled': 'bool', 'provider': 'string'}),
    'admin-publications': ('admin', '/api/admin/v1/publications', {'items': 'list'}),
    'admin-users': ('admin', '/api/admin/v1/users?scopeKind=platform&limit=10', {'items': 'list'}),
    'admin-organizations': ('admin', '/api/admin/v1/organizations?scopeKind=platform&limit=10', {'items': 'list'}),
    'admin-security': ('admin', '/api/admin/v1/security/sessions?scopeKind=platform&limit=10', {'items': 'list'}),
    'admin-confirmations': ('admin', '/api/admin/v1/auth/verification-policy', {'enabled': 'bool', 'graceHours': 'int'}),
    'admin-operations': ('admin', '/api/admin/v1/operations/status', {'services.api': 'string', 'services.database': 'string', 'migration.version': 'string', 'build.revision': 'string'}),
    'admin-logs': ('admin', '/api/admin/v1/logs/status', {'state': 'string', 'sources': 'list'}),
    'admin-history': ('admin', '/api/admin/v1/audit-events?scopeKind=platform&limit=10', {'items': 'list'}),
}
