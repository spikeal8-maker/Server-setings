"""Synthetic receipt/file tests only; no server, API, export or credential access."""
from copy import deepcopy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('marker_receipt_binding_test', HERE.parent / 'scripts/school-native-marker-receipt-binding.py')
binding = importlib.util.module_from_spec(spec)
spec.loader.exec_module(binding)


def fixture():
    expected = {'runId': 'xlog-native-marker-20261108t130000z-1234567890abcdef',
                'hostSourceRevision': binding.SOURCE, 'generation': 'a' * 32,
                'jobs': {kind: {'id': n, 'recordId': n + 10, 'dispatchId': str(n) * 32,
                                'acceptedAt': '2026-11-08T12:59:00+00:00'}
                         for n, kind in enumerate(binding.KINDS, 1)}}
    path = binding.receipt_path(expected)
    receipt = {'format': 'asa-school-native-marker-acceptance-v1',
               'status': 'NATIVE_MARKERS_NORMALIZED_HOST_EXPORT_PASS',
               'runId': expected['runId'], 'receiptPath': str(path), 'hostname': 'asa-school-01',
               'project': 'asa-lab-school', 'instance': 'fc0ba60fe1d946bba1694d73557cc11f',
               'hostSourceRevision': binding.SOURCE, 'applicationRevision': binding.APP,
               'generation': expected['generation'], 'acceptedProbeBindings': deepcopy(expected['jobs']),
               'snapshotHelperSha256': binding.SNAPSHOT, 'cronProgramSha256': binding.CRON,
               'preservedClosedGateAndInputs': True, 'temporaryCleanupVerified': True,
               'applicationOrScheduleMutation': False, 'scope': 'native-marker-normalized-host-export',
               'markerSource': 'linux:1panel-supervisor', 'authenticatedCoverageReevaluated': False,
               'globalCoverageComplete': False, 'createdAt': '2026-11-08T13:00:00+00:00',
               'observedAt': '2026-11-08T13:00:01+00:00',
               'hostZip': {'sha256': 'b' * 64, 'bytes': 1000, 'records': 2, 'parts': 1,
                           'recordBytes': 500, 'crcAndPartSha256Verified': True, 'normalized': True,
                           'allTimeAllSources': True, 'coverageComplete': False, 'exporterBlob': binding.EXPORTER},
               'jobs': {kind: {'acceptedProbeBound': True, 'nativeSuccessRecord': True,
                               'sameMarkerObject': True, 'nativeMarkerCount': 1, 'hostZipMarkerCount': 1,
                               'publishedIndexCount': 1, 'backupExecuted': False} for kind in binding.KINDS}}
    return expected, path, receipt


def wire(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':')) + '\n').encode()


class ReceiptBindingTests(unittest.TestCase):
    def test_real_receipt_bytes_hash_differs_from_redacted_tool_text(self):
        expected, path, receipt = fixture()
        raw = wire(receipt)
        tool_text = raw.replace(b'asa-lab-school', b'[REDACTED]')
        value = binding.projection(raw, expected, path)
        self.assertEqual(value['status'], 'NATIVE_MARKER_RECEIPT_BINDING_PASS')
        self.assertEqual(value['receiptSha256'], hashlib.sha256(raw).hexdigest())
        self.assertNotEqual(value['receiptSha256'], hashlib.sha256(tool_text).hexdigest())
        self.assertTrue(all(value['checks'].values()))
        public = json.dumps(value)
        for private in ('asa-lab-school', expected['generation'], expected['jobs']['database']['dispatchId']):
            self.assertNotIn(private, public)
        self.assertLessEqual(len(public.encode()), 3072)
        changed = binding.projection(tool_text, expected, path)
        self.assertFalse(changed['checks']['hostProjectInstance'])
        self.assertEqual(changed['status'], 'NATIVE_MARKER_RECEIPT_BINDING_STOP')

    def test_wrong_generation_or_probe_identity_does_not_pass(self):
        expected, path, receipt = fixture()
        for mutate in (lambda row: row.update(generation='c' * 32),
                       lambda row: row['acceptedProbeBindings']['settings'].update(recordId=999)):
            candidate = deepcopy(receipt)
            mutate(candidate)
            result = binding.projection(wire(candidate), expected, path)
            self.assertFalse(result['checks']['generationAndAcceptedProbes'])
            self.assertEqual(result['status'], 'NATIVE_MARKER_RECEIPT_BINDING_STOP')

    def test_closed_run_path_rejects_traversal_and_mismatched_receipt_path(self):
        expected, path, receipt = fixture()
        wrong = deepcopy(expected)
        wrong['runId'] += '/../another'
        with self.assertRaisesRegex(RuntimeError, 'BINDING_IDENTITY'):
            binding.receipt_path(wrong)
        receipt['receiptPath'] = str(path.parent / 'other.json')
        self.assertFalse(binding.projection(wire(receipt), expected, path)['checks']['ownRunAndPath'])

    def test_duplicate_or_incomplete_marker_proof_is_not_accepted(self):
        expected, path, receipt = fixture()
        for mutate in (lambda row: row['jobs']['database'].update(hostZipMarkerCount=2),
                       lambda row: row['jobs'].pop('settings'),
                       lambda row: row['hostZip'].update(crcAndPartSha256Verified=False)):
            candidate = deepcopy(receipt)
            mutate(candidate)
            self.assertEqual(binding.projection(wire(candidate), expected, path)['status'],
                             'NATIVE_MARKER_RECEIPT_BINDING_STOP')

    def test_cleanup_and_coverage_fields_cannot_be_promoted(self):
        expected, path, receipt = fixture()
        for field in ('temporaryCleanupVerified', 'globalCoverageComplete', 'authenticatedCoverageReevaluated'):
            candidate = deepcopy(receipt)
            candidate[field] = not candidate[field]
            self.assertEqual(binding.projection(wire(candidate), expected, path)['status'],
                             'NATIVE_MARKER_RECEIPT_BINDING_STOP')

    def test_single_root600_file_and_rejected_public_mode_hardlink_symlink(self):
        # Real file bytes, chmod, hardlink and symlink; only privileged ownership
        # of this synthetic hierarchy is modelled so ordinary CI can run it.
        with tempfile.TemporaryDirectory(prefix='receipt-fixture-') as temporary:
            path = Path(temporary) / 'receipt.json'
            alias = path.with_name('alias.json')
            path.write_bytes(b'{"fixture":true}\n')
            path.chmod(0o600)
            real_lstat = Path.lstat
            parents = set(path.parents)
            wrong_owner = False

            def model_lstat(selected):
                self.assertIn(selected, parents | {path, alias})
                value = real_lstat(selected)
                mode = value.st_mode & ~0o022 if selected in parents else value.st_mode
                return SimpleNamespace(st_uid=1234 if wrong_owner and selected == path else 0,
                    st_mode=mode, st_nlink=value.st_nlink, st_dev=value.st_dev, st_ino=value.st_ino,
                    st_size=value.st_size, st_mtime_ns=value.st_mtime_ns, st_ctime_ns=value.st_ctime_ns)

            with patch.object(Path, 'lstat', autospec=True, side_effect=model_lstat):
                self.assertEqual(binding.read_private(path), b'{"fixture":true}\n')
                wrong_owner = True
                with self.assertRaisesRegex(RuntimeError, 'RECEIPT_PATH_UNTRUSTED'):
                    binding.read_private(path)
                wrong_owner = False
                path.chmod(0o644)
                with self.assertRaisesRegex(RuntimeError, 'OWN_ROOT600_RECEIPT_REQUIRED'):
                    binding.read_private(path)
                path.chmod(0o600)
                os.link(path, alias)
                with self.assertRaisesRegex(RuntimeError, 'OWN_ROOT600_RECEIPT_REQUIRED'):
                    binding.read_private(path)
                alias.unlink()
                alias.symlink_to(path)
                with self.assertRaisesRegex(RuntimeError, 'RECEIPT_PATH_UNTRUSTED'):
                    binding.read_private(alias)


if __name__ == '__main__':
    unittest.main()
