"""Synthetic-only P1-R2 gates. No real backup, AGE identity, database or Docker."""
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from asa_ops import restore, source_registry as sources, stage_snapshot
from asa_ops.schemas import INPUTS, validate
from asa_ops.server import Controller
from asa_ops.store import OpsError


class SourceIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base/'staging'
        self.root.mkdir(mode=0o700)
        self.att = self.root/'.owner-attestations'
        if hasattr(sources, 'OWNER_ED25519_SPKI_SHA256'):
            pin = patch.object(sources, 'OWNER_ED25519_SPKI_SHA256', None)
            pin.start()
            self.addCleanup(pin.stop)

    @staticmethod
    def digest(content):
        return hashlib.sha256(content).hexdigest()

    def tar(self, *, omit=(), extra=(), source_id=sources.NEW_ID, missing_manifest=False):
        blobs = {
            'database.dump': b'synthetic pg fixture - not real account data',
            'globals.sql': b'-- no database roles in this fixture\n',
            'private.env': b'SYNTHETIC_ONLY=yes\n',
            'objects/item': b'public synthetic object bytes',
        }
        manifest = {
            'format': 'asa-backup-v1', 'complete': True,
            'createdAt': sources.NEW_ID, 'project': 'asa-lab-dev',
            'profile': 'production', 'postgresImage': 'synthetic-postgres-image',
            'runtime': {'revision': 'a' * 40, 'schema': 200},
            'files': {name: {'bytes': len(content), 'sha256': self.digest(content)}
                      for name, content in blobs.items()},
        }
        manifest_bytes = json.dumps(manifest, sort_keys=True).encode()
        members = [(source_id + '/' + name, content, tarfile.REGTYPE)
                   for name, content in blobs.items() if name not in omit]
        if not missing_manifest:
            members.append((source_id + '/manifest.json', manifest_bytes, tarfile.REGTYPE))
        members.extend(extra)
        buff = io.BytesIO()
        with tarfile.open(fileobj=buff, mode='w') as writer:
            for name, data, member_type in members:
                entry = tarfile.TarInfo(name)
                entry.type = member_type
                entry.mode = 0o600
                entry.size = len(data) if member_type == tarfile.REGTYPE else 0
                writer.addfile(entry, io.BytesIO(data) if member_type == tarfile.REGTYPE else None)
        return buff.getvalue(), self.digest(manifest_bytes)

    def sign(self, archive, manifest_sha, source=None, *, plaintext_sha=None):
        self.att.mkdir(mode=0o700, exist_ok=True)
        private = self.base/'synthetic-signing-only.pem'
        if not private.exists():
            subprocess.run(['openssl', 'genpkey', '-algorithm', 'ed25519', '-out', str(private)],
                           capture_output=True, check=True)
            pub = self.att/'owner-ed25519-public.pem'
            subprocess.run(['openssl', 'pkey', '-in', str(private), '-pubout', '-out', str(pub)],
                           capture_output=True, check=True)
            pub.chmod(0o600)
        if hasattr(sources, 'OWNER_ED25519_SPKI_SHA256'):
            sources.OWNER_ED25519_SPKI_SHA256 = sources.ed25519_spki_sha256(
                (self.att/'owner-ed25519-public.pem').read_bytes())
        payload = {
            'format': sources.RECEIPT_FORMAT, 'snapshot_id': sources.NEW_ID,
            'source': source if source is not None else dict(sources.NEW_SOURCE),
            'manifest_sha256': manifest_sha,
            'plaintext_sha256': plaintext_sha or self.digest(archive),
        }
        path = self.att/(sources.NEW_ID + '.json')
        path.write_bytes(sources.canonical(payload) + b'\n')
        path.chmod(0o600)
        signature = self.att/(sources.NEW_ID + '.sig')
        subprocess.run(['openssl', 'pkeyutl', '-sign', '-rawin', '-inkey', str(private),
                        '-in', str(path), '-out', str(signature)], capture_output=True, check=True)
        signature.chmod(0o600)
        return payload

    def stage(self, archive):
        with patch.object(stage_snapshot, 'ROOT', self.root), \
             patch.object(sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(archive))), \
             patch.object(sys, 'stdout', io.StringIO()):
            return stage_snapshot.receive(sources.NEW_ID)

    def test_legacy_and_exact_allowlist(self):
        self.assertEqual(restore.SNAPSHOT, sources.LEGACY_ID)
        self.assertEqual(restore.MANIFEST_SHA,
                         '8be098e3c37cd43fb704e7f92f300574bb685445d92697223ba0b158ac2e8a01')
        for source_id in sources.ALLOWED_IDS:
            validate(INPUTS['asa_restore_plan'], {'installation_id': 'asa-test', 'snapshot_id': source_id})
        for source_id in ('unknown', '../etc/passwd', 'https://bad.test/archive.age',
                          sources.NEW_ID.replace('0', '０'), sources.NEW_ID + '/other',
                          sources.NEW_ID + '\x00'):
            with self.subTest(source_id=source_id), self.assertRaises(OpsError):
                validate(INPUTS['asa_restore_plan'], {'installation_id': 'asa-test', 'snapshot_id': source_id})
        with self.assertRaises(OpsError):
            validate(INPUTS['asa_restore_plan'], {
                'installation_id': 'asa-test', 'snapshot_id': sources.NEW_ID, 'url': 'https://bad.test'})

    def test_missing_owner_prevents_all_live_access(self):
        with patch.object(restore, 'SOURCE_ROOT', self.root), patch.object(restore, 'load') as load:
            with self.assertRaises(OpsError) as cm:
                restore.plan({}, {'snapshot_id': sources.NEW_ID})
            self.assertEqual(cm.exception.code, 'BLOCKED_KEY_ACCESS')
            load.assert_not_called()

    def test_verified_chain_and_fail_closed_rehearsal(self):
        raw, manifest_sha = self.tar()
        self.sign(raw, manifest_sha)
        self.stage(raw)
        checked = sources.verify_staged(self.root)
        self.assertEqual(checked['source'], sources.NEW_SOURCE)
        self.assertEqual(checked['manifest_sha256'], manifest_sha)
        self.assertEqual(checked['source']['ciphertext_bytes'], 207415432)
        self.assertEqual(checked['source']['ciphertext_sha256'],
                         '526988100deb2ba55028ff00255ff3a63add0f3ebcf772c8ba4f9cee0b296592')
        self.assertEqual(len(checked['owner_receipt_sha256']), 64)
        self.assertEqual(len(checked['staging_receipt_sha256']), 64)
        plan = {'kind': 'restore', 'snapshot_id': sources.NEW_ID,
                'ready_to_rehearse': False, 'release': {'revision': 'a'*40}}
        with patch.object(restore, 'load') as load:
            with self.assertRaises(OpsError) as cm:
                restore.rehearse({}, {'plan': plan})
            self.assertEqual(cm.exception.code, 'BLOCKED_PLAN')
            load.assert_not_called()

    def test_source_size_hash_id_and_signature_mismatch(self):
        raw, manifest_sha = self.tar()
        cases = [
            ('bytes', {**sources.NEW_SOURCE, 'ciphertext_bytes': 17}, manifest_sha, None, 'OWNER_RECEIPT'),
            ('sha', {**sources.NEW_SOURCE, 'ciphertext_sha256': 'f'*64}, manifest_sha, None, 'OWNER_RECEIPT'),
            ('id', {**sources.NEW_SOURCE, 'snapshot_id': sources.LEGACY_ID}, manifest_sha, None, 'OWNER_RECEIPT'),
            ('manifest', None, 'f'*64, None, 'SOURCE_MANIFEST'),
            ('plaintext', None, manifest_sha, 'f'*64, 'SOURCE_STAGE_MISSING'),
        ]
        for name, source, declared_manifest, plaintext, expected in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as td:
                old_root, old_att, old_base = self.root, self.att, self.base
                self.root = Path(td)/'staging'
                self.root.mkdir(mode=0o700)
                self.att = self.root/'.owner-attestations'
                self.base = Path(td)
                try:
                    self.sign(raw, declared_manifest, source, plaintext_sha=plaintext)
                    if expected == 'OWNER_RECEIPT':
                        with self.assertRaises(OpsError) as cm:
                            self.stage(raw)
                        self.assertEqual(cm.exception.code, expected)
                    elif expected == 'SOURCE_MANIFEST':
                        with self.assertRaisesRegex(RuntimeError, 'Manifest'):
                            self.stage(raw)
                    else:
                        with self.assertRaisesRegex(RuntimeError, 'Decrypted archive differs'):
                            self.stage(raw)
                    self.assertFalse((self.root/sources.NEW_ID).exists())
                finally:
                    self.root, self.att, self.base = old_root, old_att, old_base
        self.sign(raw, manifest_sha)
        (self.att/(sources.NEW_ID+'.sig')).write_bytes(b'bad-signature')
        with self.assertRaises(OpsError) as cm:
            sources.owner_attestation(self.root)
        self.assertEqual(cm.exception.code, 'OWNER_SIGNATURE')

    def test_invalid_tar_members_never_promoted(self):
        bad = [
            ('traversal', (sources.NEW_ID+'/../escape', b'bad', tarfile.REGTYPE)),
            ('symlink', (sources.NEW_ID+'/link', b'', tarfile.SYMTYPE)),
            ('hardlink', (sources.NEW_ID+'/link', b'', tarfile.LNKTYPE)),
            ('device', (sources.NEW_ID+'/device', b'', tarfile.CHRTYPE)),
            ('fifo', (sources.NEW_ID+'/pipe', b'', tarfile.FIFOTYPE)),
            ('foreign', ('foreign/database.dump', b'no', tarfile.REGTYPE)),
            ('unicode', (sources.NEW_ID.replace('0', '０')+'/escape', b'no', tarfile.REGTYPE)),
            ('confusable', (sources.NEW_ID+'/objects/ＦILE', b'no', tarfile.REGTYPE)),
            ('duplicate', (sources.NEW_ID+'/database.dump', b'extra', tarfile.REGTYPE)),
        ]
        for name, extra in bad:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as td:
                self.root = Path(td)/'staging'
                self.root.mkdir(mode=0o700)
                self.att = self.root/'.owner-attestations'
                self.base = Path(td)
                raw, h = self.tar(extra=[extra])
                self.sign(raw, h)
                with self.assertRaises(RuntimeError):
                    self.stage(raw)
                self.assertFalse((self.root/sources.NEW_ID).exists())
                self.assertTrue((self.root/(sources.NEW_ID+'.partial')).exists())

    def test_missing_file_and_missing_manifest_block(self):
        for omission in (('objects/item',), ('manifest.json',)):
            with self.subTest(omission=omission), tempfile.TemporaryDirectory() as td:
                self.root = Path(td)/'staging'
                self.root.mkdir(mode=0o700)
                self.att = self.root/'.owner-attestations'
                self.base = Path(td)
                raw, h = self.tar(omit=omission, missing_manifest='manifest.json' in omission)
                self.sign(raw, h)
                with self.assertRaises(RuntimeError):
                    self.stage(raw)
                self.assertFalse((self.root/sources.NEW_ID).exists())

    def test_staged_owner_manifest_and_file_drift_block(self):
        raw, h = self.tar()
        self.sign(raw, h)
        self.stage(raw)
        stage = self.att/(sources.NEW_ID+'.stage.json')
        original_stage = stage.read_bytes()
        stage.write_bytes(original_stage.replace(b'asa-staging-receipt-v1', b'fake-stage'))
        with self.assertRaises(OpsError):
            sources.verify_staged(self.root)
        stage.write_bytes(original_stage)
        manifest = self.root/sources.NEW_ID/'manifest.json'
        original_manifest = manifest.read_bytes()
        manifest.write_bytes(original_manifest+b'\n')
        with self.assertRaises(OpsError) as cm:
            sources.verify_staged(self.root)
        self.assertEqual(cm.exception.code, 'SOURCE_MANIFEST')
        manifest.write_bytes(original_manifest)
        target = self.root/sources.NEW_ID/'objects/item'
        target.unlink()
        target.symlink_to(manifest)
        with self.assertRaises(OpsError):
            sources.verify_staged(self.root)
        target.unlink()
        target.write_bytes(b'changed')
        with self.assertRaises(OpsError) as cm:
            sources.verify_staged(self.root)
        self.assertEqual(cm.exception.code, 'SOURCE_INTEGRITY')
        target.write_bytes(b'public synthetic object bytes')
        (self.root/(sources.NEW_ID+'.partial')).mkdir()
        with self.assertRaises(OpsError) as cm:
            sources.verify_staged(self.root)
        self.assertEqual(cm.exception.code, 'SOURCE_PARTIAL')

    def test_server_forward_and_blocks_unready_or_stale_generation(self):
        config = self.base/'controller.json'
        config.write_text(json.dumps({'installation': {'id':'asa-test', 'root': str(self.base),
                                     'state_root': str(self.base/'state')}, 'official_command': []}))
        meta = self.base/'.asa'
        meta.mkdir(exist_ok=True)
        (meta/'installation.json').write_text(json.dumps({'instanceId': 'c'*32}))
        ctrl = Controller(config, official=object())
        with patch('asa_ops.server.adapter', return_value={
            'kind': 'restore', 'snapshot_id': sources.NEW_ID,
            'installation_instance_id': 'c'*32,
            'ready_to_rehearse': False}) as adapter:
            result = ctrl.dispatch('asa_restore_plan', {
                'installation_id':'asa-test', 'snapshot_id':sources.NEW_ID})
            adapter.assert_called_once_with(ctrl.config_path, 'restore_plan',
                                            {'snapshot_id': sources.NEW_ID})
        plan = result['plan']
        with patch.object(ctrl, 'launch') as launch:
            with self.assertRaises(OpsError) as cm:
                ctrl.dispatch('asa_restore_rehearse', {
                    'installation_id':'asa-test', 'plan_id':plan['plan_id'],
                    'idempotency_key':'new-rehearsal-blocked-001'})
            self.assertEqual(cm.exception.code, 'BLOCKED_PLAN')
            launch.assert_not_called()
        artificial = ctrl.store.plan('asa-test', {
            'kind': 'restore', 'snapshot_id': sources.NEW_ID,
            'installation_instance_id': 'd'*32, 'ready_to_rehearse': True})
        with patch.object(ctrl, 'launch') as launch:
            with self.assertRaises(OpsError) as cm:
                ctrl.dispatch('asa_restore_rehearse', {
                    'installation_id':'asa-test', 'plan_id':artificial['plan_id'],
                    'idempotency_key':'new-rehearsal-generation-001'})
            self.assertEqual(cm.exception.code, 'BLOCKED_PLAN')
            launch.assert_not_called()


    def test_stale_runtime_cannot_reuse_source_attested_plan(self):
        """Future rehearsal enablement still must revalidate the pinned runtime."""
        import contextlib
        from types import ModuleType

        raw, manifest_sha = self.tar()
        self.sign(raw, manifest_sha)
        self.stage(raw)
        proof = sources.verify_staged(self.root)
        planned = {
            'kind': 'restore', 'snapshot_id': sources.NEW_ID,
            'ready_to_rehearse': True,
            'release': {'revision': 'a'*40, 'schema': 267},
            'manifest_sha256': proof['manifest_sha256'],
        }
        modules = {name: ModuleType(name) for name in (
            'deployment', 'deployment.system', 'deployment.backup',
            'deployment.releases', 'deployment.contracts',
        )}
        modules['deployment.system'].operation_lock = lambda root: contextlib.nullcontext()
        modules['deployment.backup'].restore_check = lambda *args: self.fail('restore_check was called')
        modules['deployment.backup'].privileges = lambda *args: self.fail('privileges was called')
        modules['deployment.releases'].apply_release_environment = lambda *args: self.fail('release environment was modified')
        modules['deployment.releases'].validate_resolved_images = lambda *args: self.fail('runtime images were inspected')
        modules['deployment.contracts'].atomic_json = lambda *args: self.fail('receipt file was written')
        fake_install = SimpleNamespace(root=self.root)
        with patch.dict(sys.modules, modules), \
             patch.object(restore, 'load', return_value=(object(), fake_install)), \
             patch.object(restore, 'selected_snapshot', return_value=(
                 proof['root'], proof['manifest'], {}, proof)), \
             patch.object(restore, 'release_for_running', return_value={
                 'revision': 'b'*40, 'schema': 267}):
            with self.assertRaises(OpsError) as cm:
                restore.rehearse({}, {'plan': planned})
        self.assertEqual(cm.exception.code, 'BLOCKED_PLAN')

    def test_new_staging_hardlink_and_owner_receipt_rotation_are_rejected(self):
        raw, manifest_sha = self.tar()
        self.sign(raw, manifest_sha)
        self.stage(raw)
        target = self.root/sources.NEW_ID/'objects/item'
        manifest = self.root/sources.NEW_ID/'manifest.json'
        target.unlink()
        import os
        os.link(manifest, target)
        with self.assertRaises(OpsError) as cm:
            sources.verify_staged(self.root)
        self.assertIn(cm.exception.code, ('SOURCE_FILE', 'SOURCE_TYPE'))
        target.unlink()
        target.write_bytes(b'public synthetic object bytes')
        self.sign(raw, 'f'*64)
        with self.assertRaises(OpsError) as cm:
            sources.verify_staged(self.root)
        self.assertEqual(cm.exception.code, 'SOURCE_STAGE')


    def test_security_p1a_replacement_key_must_not_be_owner(self):
        raw, manifest_sha = self.tar()
        self.sign(raw, manifest_sha)
        receipt = self.att/(sources.NEW_ID+'.json')
        rogue = self.base/'rogue-signing-key.pem'
        public = self.att/'owner-ed25519-public.pem'
        signature = self.att/(sources.NEW_ID+'.sig')
        subprocess.run(['openssl', 'genpkey', '-algorithm', 'ed25519',
                        '-out', str(rogue)], check=True, capture_output=True)
        subprocess.run(['openssl', 'pkey', '-in', str(rogue), '-pubout',
                        '-out', str(public)], check=True, capture_output=True)
        public.chmod(0o600)
        subprocess.run(['openssl', 'pkeyutl', '-sign', '-rawin',
                        '-inkey', str(rogue), '-in', str(receipt),
                        '-out', str(signature)], check=True, capture_output=True)
        signature.chmod(0o600)
        with self.assertRaises(OpsError):
            sources.owner_attestation(self.root)

    def test_security_p1b_swapped_receipt_during_verify_is_rejected(self):
        raw, manifest_sha = self.tar()
        self.sign(raw, manifest_sha)
        receipt = self.att/(sources.NEW_ID+'.json')
        signature = self.att/(sources.NEW_ID+'.sig')
        original_receipt, original_sig = receipt.read_bytes(), signature.read_bytes()
        self.sign(raw, 'f'*64)
        replacement_receipt, replacement_sig = receipt.read_bytes(), signature.read_bytes()
        receipt.write_bytes(original_receipt)
        signature.write_bytes(original_sig)
        original_run = subprocess.run
        def swap_between_read_and_verify(argv, **kwargs):
            if argv[0] == 'openssl' and 'pkeyutl' in argv and '-verify' in argv:
                receipt.write_bytes(replacement_receipt)
                signature.write_bytes(replacement_sig)
            return original_run(argv, **kwargs)
        with patch.object(sources.subprocess, 'run', side_effect=swap_between_read_and_verify):
            with self.assertRaises(OpsError):
                sources.owner_attestation(self.root)

    def test_security_p1c_forged_true_plan_cannot_load_manager(self):
        forged = {'kind':'restore', 'snapshot_id':sources.NEW_ID,
                  'ready_to_rehearse':True,
                  'release':{'revision':'a'*40}}
        with patch.object(restore, 'load', side_effect=AssertionError(
                'manager or Docker accessed for forbidden rehearsal')) as load:
            with self.assertRaises(OpsError) as caught:
                restore.rehearse({}, {'plan': forged})
        self.assertEqual(caught.exception.code, 'BLOCKED_PLAN')
        load.assert_not_called()



if __name__ == '__main__':
    unittest.main()
