import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from asa_ops import stage_snapshot
from asa_ops.schemas import INPUTS, validate
from asa_ops.store import OpsError


class SnapshotReceiverTests(unittest.TestCase):
    def stream(self, entries):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode='w') as tar:
            for name, content, kind in entries:
                info = tarfile.TarInfo(name);info.type = kind
                info.size = len(content) if kind == tarfile.REGTYPE else 0
                tar.addfile(info, io.BytesIO(content) if kind == tarfile.REGTYPE else None)
        output.seek(0)
        return output

    def test_complete_inventory_is_staged_privately(self):
        data = b'private fixture, not real backup data'
        manifest = {'format': 'asa-backup-v1', 'complete': True, 'createdAt': stage_snapshot.SNAPSHOT,
                    'files': {'database.dump': {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}}}
        stream = self.stream([(stage_snapshot.SNAPSHOT+'/database.dump', data, tarfile.REGTYPE),
            (stage_snapshot.SNAPSHOT+'/manifest.json', json.dumps(manifest).encode(), tarfile.REGTYPE)])
        with tempfile.TemporaryDirectory() as td, patch.object(stage_snapshot, 'ROOT', Path(td)), \
                patch.object(sys, 'stdin', SimpleNamespace(buffer=stream)), patch.object(sys, 'stdout', io.StringIO()):
            stage_snapshot.receive()
            self.assertEqual((Path(td)/stage_snapshot.SNAPSHOT/'database.dump').read_bytes(), data)
            self.assertFalse((Path(td)/(stage_snapshot.SNAPSHOT+'.partial')).exists())

    def test_traversal_links_and_foreign_snapshot_are_never_promoted(self):
        for name, kind in [(stage_snapshot.SNAPSHOT+'/../outside', tarfile.REGTYPE),
                           (stage_snapshot.SNAPSHOT+'/link', tarfile.SYMTYPE),
                           ('foreign/database.dump', tarfile.REGTYPE)]:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as td, \
                    patch.object(stage_snapshot, 'ROOT', Path(td)), \
                    patch.object(sys, 'stdin', SimpleNamespace(buffer=self.stream([(name,b'x',kind)]))):
                with self.assertRaises(RuntimeError):
                    stage_snapshot.receive()
                self.assertFalse((Path(td)/stage_snapshot.SNAPSHOT).exists())

    def test_restore_inputs_cannot_select_paths_commands_or_other_archives(self):
        for args in [{'installation_id':'asa-test','snapshot_id':'../../private'},
                     {'installation_id':'asa-test','snapshot_id':stage_snapshot.SNAPSHOT,'command':'sh'},
                     {'installation_id':'foreign','snapshot_id':stage_snapshot.SNAPSHOT}]:
            with self.assertRaises(OpsError):
                validate(INPUTS['asa_restore_plan'], args)


if __name__ == '__main__':
    unittest.main()
