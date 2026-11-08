"""Read one owned root600 marker receipt; emit its real SHA and closed booleans.

No export, API, DB, credential read, file write or redaction change. The exact
future marker run/generation/probes must be supplied by the reviewed request.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import sys

BASE = Path('/var/lib/server-settings/xlog-native-marker-evidence')
SOURCE = '17d937c01660025e6b5030f44bcb0d776e7c79aa'
APP = '3e8713476cafedda3e24a39665458e833efd7c22'
SNAPSHOT = '9a7719c5705fb6883857b0c25be1e19f9ee7f3009c8594a43ca15d04d0bffbe2'
CRON = '4b391bbd4b4589212acffc25a4239b79d15e5830176300610dfd1f7fc3ef596b'
EXPORTER = '47e562c63f0246648736461aabc31c70bac670ce'
KINDS = ('database', 'settings')


def need(value, code):
    if not value:
        raise RuntimeError(code)


def instant(value):
    need(isinstance(value, str) and 20 <= len(value) <= 40, 'TIME_SHAPE')
    value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    need(value.tzinfo is not None, 'TIME_OFFSET_REQUIRED')
    return value.astimezone(timezone.utc)


def receipt_path(binding):
    need(isinstance(binding, dict) and set(binding) == {'runId', 'hostSourceRevision', 'generation', 'jobs'}, 'BINDING_SCHEMA')
    need(re.fullmatch(r'xlog-native-marker-[0-9]{8}t[0-9]{6}z-[a-f0-9]{16}', binding['runId'])
         and binding['hostSourceRevision'] == SOURCE and re.fullmatch(r'[a-f0-9]{32}', binding['generation'])
         and set(binding['jobs']) == set(KINDS), 'BINDING_IDENTITY')
    for kind in KINDS:
        row = binding['jobs'][kind]
        need(set(row) == {'id', 'recordId', 'dispatchId', 'acceptedAt'}
             and all(type(row[key]) is int and row[key] > 0 for key in ('id', 'recordId'))
             and re.fullmatch(r'[a-f0-9]{32}', row['dispatchId']), 'BINDING_PROBE')
        instant(row['acceptedAt'])
    return BASE / binding['runId'] / 'receipt.json'


def read_private(path):
    for item in (path, *path.parents):
        info = item.lstat()
        need(info.st_uid == 0 and not stat.S_ISLNK(info.st_mode) and not info.st_mode & 0o022,
             'RECEIPT_PATH_UNTRUSTED')
        need(stat.S_ISREG(info.st_mode) if item == path else stat.S_ISDIR(info.st_mode), 'RECEIPT_PATH_TYPE')
    before = path.lstat()
    need(before.st_nlink == 1 and stat.S_IMODE(before.st_mode) == 0o600 and 0 < before.st_size <= 8192
         and stat.S_IMODE(path.parent.stat().st_mode) == 0o700, 'OWN_ROOT600_RECEIPT_REQUIRED')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, 'rb') as stream:
        raw = stream.read(8193)
        after = os.fstat(stream.fileno())
    sig = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
    need(len(raw) == before.st_size and sig(before) == sig(after) == sig(path.lstat()), 'RECEIPT_CHANGED_DURING_READ')
    return raw


def projection(raw, binding, path):
    value = json.loads(raw)
    archive = value.get('hostZip', {})
    jobs = value.get('jobs', {})
    checks = {
        'receiptFormatAndPass': value.get('format') == 'asa-school-native-marker-acceptance-v1'
            and value.get('status') == 'NATIVE_MARKERS_NORMALIZED_HOST_EXPORT_PASS',
        'ownRunAndPath': value.get('runId') == binding['runId'] and value.get('receiptPath') == str(path),
        'hostProjectInstance': value.get('hostname') == 'asa-school-01' and value.get('project') == 'asa-lab-school'
            and value.get('instance') == 'fc0ba60fe1d946bba1694d73557cc11f',
        'sourceAndRuntime': value.get('hostSourceRevision') == SOURCE and value.get('applicationRevision') == APP,
        'generationAndAcceptedProbes': value.get('generation') == binding['generation']
            and value.get('acceptedProbeBindings') == binding['jobs'],
        'acceptedHelpers': value.get('snapshotHelperSha256') == SNAPSHOT and value.get('cronProgramSha256') == CRON,
        'closedGateAndCleanup': value.get('preservedClosedGateAndInputs') is True
            and value.get('temporaryCleanupVerified') is True and value.get('applicationOrScheduleMutation') is False,
        'declaredProofScope': value.get('scope') == 'native-marker-normalized-host-export'
            and value.get('markerSource') == 'linux:1panel-supervisor'
            and value.get('authenticatedCoverageReevaluated') is False and value.get('globalCoverageComplete') is False,
        'hostZipProofFields': bool(re.fullmatch(r'[a-f0-9]{64}', str(archive.get('sha256', ''))))
            and type(archive.get('bytes')) is int and 0 < archive['bytes'] <= 256 * 1024**2
            and archive.get('crcAndPartSha256Verified') is True and archive.get('normalized') is True
            and archive.get('allTimeAllSources') is True and archive.get('coverageComplete') is False
            and archive.get('exporterBlob') == EXPORTER and type(archive.get('records')) is int and archive['records'] >= 2
            and type(archive.get('parts')) is int and 0 < archive['parts'] <= 20000
            and type(archive.get('recordBytes')) is int and 0 < archive['recordBytes'] <= 2 * 1024**3,
        'twoUniqueMarkerProofs': set(jobs) == set(KINDS) and all(
            all(jobs[kind].get(key) is True for key in ('acceptedProbeBound', 'nativeSuccessRecord', 'sameMarkerObject'))
            and all(type(jobs[kind].get(key)) is int and jobs[kind][key] == 1
                    for key in ('nativeMarkerCount', 'hostZipMarkerCount', 'publishedIndexCount'))
            and jobs[kind].get('backupExecuted') is False for kind in KINDS),
        'receiptTimeOrder': max(instant(binding['jobs'][kind]['acceptedAt']) for kind in KINDS)
            <= instant(value['createdAt']) <= instant(value['observedAt']) <= datetime.now(timezone.utc),
    }
    need(all(type(item) is bool for item in checks.values()), 'BOOLEAN_PROJECTION_REQUIRED')
    return {'format': 'asa-school-native-marker-receipt-binding-v1',
            'status': 'NATIVE_MARKER_RECEIPT_BINDING_PASS' if all(checks.values()) else 'NATIVE_MARKER_RECEIPT_BINDING_STOP',
            'receiptSha256': hashlib.sha256(raw).hexdigest(), 'receiptBytes': len(raw), 'checks': checks,
            'root600StableRead': True, 'readOnly': True, 'exportRepeated': False,
            'receiptProofFieldsOnly': True, 'credentialsRead': False, 'redactionChanged': False}


def main(binding):
    need(os.geteuid() == 0 and socket.gethostname() == 'asa-school-01' and sys.flags.isolated
         and sys.dont_write_bytecode, 'VM100_ISOLATED_ROOT_REQUIRED')
    path = receipt_path(binding)
    result = projection(read_private(path), binding, path)
    raw = json.dumps(result, sort_keys=True, separators=(',', ':'))
    need(len(raw.encode()) <= 3072, 'PUBLIC_OUTPUT_BOUND')
    print(raw, flush=True)
    return 0 if result['status'] == 'NATIVE_MARKER_RECEIPT_BINDING_PASS' else 2


if __name__ == '__main__':
    try:
        need(len(sys.argv) == 2 and len(sys.argv[1].encode()) <= 4096, 'ONE_EXACT_BINDING_REQUIRED')
        raise SystemExit(main(json.loads(sys.argv[1])))
    except Exception as error:
        code = str(error)
        print(json.dumps({'status': 'NATIVE_MARKER_RECEIPT_BINDING_STOP',
                          'code': code if re.fullmatch(r'[A-Z0-9_]{1,100}', code) else type(error).__name__,
                          'readOnly': True, 'exportRepeated': False, 'redactionChanged': False}), flush=True)
        raise SystemExit(2)
