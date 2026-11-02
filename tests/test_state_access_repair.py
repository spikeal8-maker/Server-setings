import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('acl_repair', Path(__file__).parents[1] / 'scripts/repair-asa-state-access.py')
repair = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repair)


class ParentAclTests(unittest.TestCase):
    def test_masked_owner_traversal_is_repairable(self):
        repair.validate_acl('user::rwx\nuser:admins:--x\t#effective:---\ngroup::---\nmask::---\nother::---', 'admins')

    def test_root_only_parent_is_repairable(self):
        repair.validate_acl('user::rwx\ngroup::---\nother::---', 'admins')

    def test_other_masked_reader_is_not_unmasked(self):
        with self.assertRaises(ValueError):
            repair.validate_acl('user:other:r-- #effective:---\nmask::---', 'admins')

    def test_group_or_public_access_requires_review(self):
        for value in ['group::r-x', 'other::--x', 'default:user::rwx']:
            with self.assertRaises(ValueError):
                repair.validate_acl(value, 'admins')


if __name__ == '__main__':
    unittest.main()
