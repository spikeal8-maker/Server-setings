import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from asa_ops.maintenance import private_directory,check_generation,supersede,validate_service_state
from asa_ops.store import Store,OpsError
from asa_ops.adapter import forward_release

class MaintenanceTests(unittest.TestCase):
    def test_downgrade_rejected_before_source_or_image_operations(self):
        install=Mock()
        with self.assertRaises(OpsError):forward_release(install,{'schema':234,'revision':'b'*40},{'schema':225,'revision':'a'*40})
        install.git.assert_not_called()
        forward_release(install,{'schema':'0234','revision':'b'*40},{'schema':234,'revision':'b'*40})
        install.git.assert_not_called()

    def test_protected_state_paths_are_rejected_before_creation(self):
        for path in ('/','/var/lib/docker','/srv/asa-lab','/var/lib/server-settings'):
            with self.assertRaises(OpsError):validate_service_state(path,1000)
    def test_unconfigured_generation_is_not_executable(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(OpsError):check_generation(folder,{})
    def test_old_generation_cannot_start(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'.asa').mkdir();(root/'.asa/installation.json').write_text(json.dumps({'instanceId':'b'*32}))
            with self.assertRaises(OpsError):check_generation(root,{'installation_instance_id':'a'*32})
            with self.assertRaises(OpsError):check_generation(root,{})
            check_generation(root,{'installation_instance_id':'b'*32})
    @unittest.skipIf(os.name=='nt','POSIX ownership')
    def test_backup_preview_does_not_create_and_symlink_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);target=root/'backups'
            private_directory(target,os.getuid());self.assertFalse(target.exists())
            private_directory(target,os.getuid(),apply=True);self.assertEqual(target.stat().st_mode&0o777,0o700)
            link=root/'link';link.symlink_to(target,target_is_directory=True)
            with self.assertRaises(OpsError):private_directory(link,os.getuid(),apply=True)
    def test_old_recovery_is_retired_with_history_and_repeat_is_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Store(folder);plan=store.plan('test',{'ready':True});row,_=store.start('test','update',plan['plan_id'],'maintenance-test-key-0001',{})
            store.update(row['operation_id'],status='recovery_required',result={'error':{'code':'OLD_FAILURE'}})
            proof={'proof_sha256':'a'*64,'new_volumes_after':'2099-01-01T00:00:00+00:00','instance_id':'b'*32}
            self.assertEqual(supersede(store,'test',row['operation_id'],proof)['status'],'would_supersede')
            self.assertEqual(store.get(row['operation_id'])['status'],'recovery_required')
            supersede(store,'test',row['operation_id'],proof,apply=True)
            final=store.get(row['operation_id']);self.assertEqual(final['status'],'superseded');self.assertEqual(final['result']['error']['code'],'OLD_FAILURE')
            self.assertTrue(supersede(store,'test',row['operation_id'],proof,apply=True)['reused'])
            self.assertTrue(store.start('test','update',plan['plan_id'],'maintenance-test-key-0002',{})[1])
    def test_live_operation_is_never_retired(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Store(folder);plan=store.plan('test',{});row,_=store.start('test','update',plan['plan_id'],'maintenance-live-key-001',{})
            with self.assertRaises(OpsError):supersede(store,'test',row['operation_id'],{'new_volumes_after':'2099-01-01T00:00:00+00:00'},apply=True)
if __name__=='__main__':unittest.main()
