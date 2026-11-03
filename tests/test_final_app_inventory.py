"""Frozen final-source alignment must preserve the approved workload contract."""
import hashlib
import json
from pathlib import Path
import unittest


class FinalInventoryTests(unittest.TestCase):
    def test_final_source_alignment_preserves_every_workload_field(self):
        profiles = Path(__file__).resolve().parents[1] / 'profiles'
        expected = {
            'school-route-inventory.json': (44, '2b2797a3782c598dc4b3a93ce81311af3a561b376b023616c1286dcd7d540fea'),
            'school-mixed-resource-contract.json': (23, 'de0b2ca7bcca4ca5fc4e1331d9ec344fcc120a7ff2f59468a087fd95370d8f3d'),
            'school-fixture-source-contract.json': (11, '22090321192cc1ba0ff16b8e56f3df6a97cd47fbae1d7417328dfd9a77df88be'),
        }
        for name, (count, preserved_digest) in expected.items():
            with self.subTest(name=name):
                data = json.loads((profiles / name).read_text(encoding='utf-8'))
                key = 'sourceRevision' if 'sourceRevision' in data else 'observedSourceRevision'
                self.assertEqual(data.pop(key), '2a28cffe7a20302f9b7231ef823bdc05f3fe43d0')
                self.assertEqual(len(data['sourceFiles']), count)
                if name == 'school-route-inventory.json':
                    self.assertEqual(len(data['routes']), 65)
                    self.assertEqual(data['sourceFiles'].pop('apps/web/src/App.tsx'),
                                     '65b911ef06b04f1f3e8cced977d4202778c336586c1059371cc7179b65b39c94')
                # Computed from approved infra8044 before the alignment, not from
                # the new profiles: roles/checks/limits and all other pins survive.
                actual = hashlib.sha256(json.dumps(data, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
                self.assertEqual(actual, preserved_digest)


if __name__ == '__main__':
    unittest.main()
