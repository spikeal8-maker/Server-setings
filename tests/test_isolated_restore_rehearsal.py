import ast
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import subprocess
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('isolated_restore', Path(__file__).parents[1] / 'scripts/isolated-asa-restore-rehearsal.py')
restore = importlib.util.module_from_spec(spec)
spec.loader.exec_module(restore)


def member(name, kind=tarfile.REGTYPE):
    item = tarfile.TarInfo(name)
    item.type = kind
    return item


class FakeDocker:
    nonce = 'a' * 32

    def __init__(self):
        self.calls = []

    def inspect(self, cid):
        return {'Config': {'Labels': {restore.LABEL: self.nonce}}}

    def run(self, args, **kwargs):
        self.calls.append((args, kwargs))
        return ''


class ArchiveTests(unittest.TestCase):
    def test_real_canonical_dependency_closure_and_import(self):
        revision = 'dfee5badd299d318a8e51ff0cbf88e9656333a5d'
        repo = Path(os.environ.get('ASA_CANONICAL_SOURCE_REPO',
                    str(Path(__file__).parents[2] / 'asa-lab-source')))
        if not (repo / '.git').exists():
            self.skipTest('Set ASA_CANONICAL_SOURCE_REPO to canonical ASA checkout containing pinned runtime commit')
        sources = {}
        for name in restore.SOURCE_FILES:
            sources[name] = subprocess.check_output(['git', '-C', str(repo), 'show',
                                                     revision + ':tools/deployment/' + name])
        # Audit the entire relative-import closure of the exact deployed revision,
        # rather than silently assuming the developer checkout matches runtime.
        for name, content in sources.items():
            for node in ast.walk(ast.parse(content, filename=name)):
                if isinstance(node, ast.ImportFrom) and node.level:
                    self.assertEqual(node.level, 1)
                    self.assertIn(node.module + '.py', restore.SOURCE_FILES)
        receipt = {'repository': 'spikeal8-maker/asa-lab', 'commit': revision,
                   'files': {name: hashlib.sha256(content).hexdigest() for name, content in sources.items()}}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, content in sources.items():
                (root / name).write_bytes(content)
            # This cross-platform source test replaces only POSIX trust metadata;
            # imports must not spawn host/Docker processes or network requests.
            with patch.object(restore, 'trusted_tree', return_value=receipt), \
                 patch('subprocess.run', side_effect=AssertionError('import must not execute commands')), \
                 patch('urllib.request.urlopen', side_effect=AssertionError('import must not contact network')):
                canonical, actual = restore.load_canonical(root, 'dependency_closure_test')
            self.assertTrue(callable(canonical.restore_check))
            self.assertEqual(actual['commit'], revision)
            self.assertEqual({path.name for path in root.iterdir()}, restore.SOURCE_FILES)

    def test_container_rejects_host_network_and_foreign_mounts(self):
        image = 'sha256:' + 'b' * 64
        cid = 'c' * 64
        for change in ['host-network', 'bind-mount']:
            docker = restore.Docker('d' * 32, io.BytesIO())
            row = {'Config': {'Labels': {restore.LABEL: docker.nonce}}, 'Image': image,
                   'HostConfig': {'NetworkMode': 'none', 'ReadonlyRootfs': True, 'CapDrop': ['ALL']},
                   'Mounts': [{'Type': 'volume', 'Name': 'owned', 'Destination': '/data', 'RW': True}]}
            if change == 'host-network':
                row['HostConfig']['NetworkMode'] = 'host'
            else:
                row['Mounts'].append({'Type': 'bind', 'Source': '/srv/live', 'Destination': '/live'})
            with patch.object(docker, 'run', side_effect=['[]', cid]), patch.object(docker, 'inspect', return_value=row):
                with self.subTest(change=change), self.assertRaises(RuntimeError):
                    docker.create('unique', image, 'owned', '/data', Path('/private.env'), '1000', ['server'])

    def test_rejects_escaping_and_control_character_paths(self):
        for name in ['/asa-backup/x', 'asa-backup/../x', 'other/x', 'asa-backup/x\\y', 'asa-backup/x:y', 'asa-backup/x\ny']:
            with self.subTest(name=name), self.assertRaises(RuntimeError):
                restore.validate_members([member('asa-backup/manifest.json'), member(name)])

    def test_rejects_links_devices_duplicates_and_sparse(self):
        for kind in [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.FIFOTYPE]:
            with self.subTest(kind=kind), self.assertRaises(RuntimeError):
                restore.validate_members([member('asa-backup/manifest.json'), member('asa-backup/data', kind)])
        with self.assertRaises(RuntimeError):
            restore.validate_members([member('asa-backup/manifest.json'), member('asa-backup/manifest.json')])
        sparse = member('asa-backup/data')
        sparse.sparse = [(0, 10)]
        with self.assertRaises(RuntimeError):
            restore.validate_members([member('asa-backup/manifest.json'), sparse])

    def test_unpacks_only_regular_members_under_single_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = root / 'input.tar'
            with tarfile.open(bundle, 'w') as tar:
                info = member('asa-backup/manifest.json')
                info.size = 2
                tar.addfile(info, io.BytesIO(b'{}'))
            target = root / 'output'
            target.mkdir()
            result = restore.unpack(bundle, target)
            self.assertEqual((result / 'manifest.json').read_bytes(), b'{}')


class AdapterTests(unittest.TestCase):
    def test_canonical_exec_cannot_target_production_or_other_services(self):
        docker = FakeDocker()
        adapter = restore.IsolatedInstallation(docker, 'nonce-pg', 'admin')
        for args in [('up', '-d'), ('ps',), ('exec', '-T', 'api', 'sh'), ('exec', '-T', 'postgres', 'curl')]:
            with self.subTest(args=args), self.assertRaises(RuntimeError):
                adapter.compose(*args)
        self.assertEqual(docker.calls, [])

    def test_only_fixed_new_container_receives_canonical_exec(self):
        docker = FakeDocker()
        adapter = restore.IsolatedInstallation(docker, 'nonce-pg', 'admin')
        adapter.compose('exec', '-T', '-e', 'RESTORE_DATABASE=one_test', 'postgres', 'psql', '-XAt', input_data='SELECT 1;')
        args, kwargs = docker.calls[0]
        self.assertEqual(args[:5], ['exec', '-i', '-e', 'RESTORE_DATABASE=one_test', 'nonce-pg'])
        self.assertEqual(kwargs['data'], b'SELECT 1;')
        with self.assertRaises(RuntimeError):
            adapter.compose('exec', '-T', '-e', 'POSTGRES_PASSWORD=leak', 'postgres', 'psql')

    def test_foreign_label_refuses_access_and_cleanup(self):
        docker = FakeDocker()
        docker.inspect = lambda cid: {'Id': cid, 'Config': {'Labels': {restore.LABEL: 'foreign'}}}
        with self.assertRaises(RuntimeError):
            restore.IsolatedInstallation(docker, 'nonce-pg', 'admin').identity()
        controlled = restore.Docker('a' * 32, io.BytesIO())
        controlled.containers = ['nonce-pg']
        controlled.inspect = docker.inspect
        controlled.run = docker.run
        with self.assertRaises(RuntimeError):
            controlled.cleanup()
        self.assertEqual(docker.calls, [])


class InputTests(unittest.TestCase):
    def test_receipt_binds_exact_plaintext_archive_and_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / '.asa').mkdir()
            (root / '.asa/installation.json').write_text(json.dumps({'instanceId': 'uuid'}))
            archive, bundle = root / 'cipher.age', root / 'plain.tar'
            archive.write_bytes(b'cipher')
            bundle.write_bytes(b'plain')
            c = {'hostname': 'host', 'root': str(root), 'installation_instance_id': 'uuid'}
            last = {'installation_instance_id': 'uuid', 'status': 'success', 'backup_verified': True,
                    'cloud_roundtrip_verified': True, 'sha256': restore.sha(archive)}
            proof = {'installation_instance_id': 'uuid', 'decryption_verified': True,
                     'archive_sha256': last['sha256'], 'plaintext_sha256': restore.sha(bundle)}
            with patch.object(restore.socket, 'gethostname', return_value='host'):
                restore.validate_inputs(c, proof, last, bundle, archive)
                for changes in [{'archive_sha256': '0' * 64}, {'plaintext_sha256': '0' * 64},
                                {'installation_instance_id': 'other'}, {'decryption_verified': False}]:
                    with self.subTest(changes=changes), self.assertRaises(RuntimeError):
                        restore.validate_inputs(c, {**proof, **changes}, last, bundle, archive)

    def test_secret_env_file_values_cannot_inject_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            restore.write_env(root / 'one', {'MC_HOST_audit': 'http://user:own-ephemeral@127.0.0.1:9000'})
            for values in [{'BAD\nKEY': 'one'}, {'GOOD': 'one\nTWO=three'}]:
                with self.assertRaises(RuntimeError):
                    restore.write_env(root / 'bad', values)


if __name__ == '__main__':
    unittest.main()
