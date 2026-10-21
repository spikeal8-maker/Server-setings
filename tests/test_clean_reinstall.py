import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch
spec=importlib.util.spec_from_file_location('clean',Path(__file__).resolve().parents[1]/'scripts/asa-clean-reinstall.py')
clean=importlib.util.module_from_spec(spec);spec.loader.exec_module(clean)
class SafetyTests(unittest.TestCase):
    def records(self):
        return [{'Id':str(i),'Config':{'Labels':{'com.docker.compose.project':clean.PROJECT,'com.docker.compose.service':service,'com.docker.compose.project.working_dir':str(clean.ROOT),'com.docker.compose.project.config_files':str(clean.ROOT/'compose.yaml')+','+str(clean.ROOT/'compose.production.yaml')}}} for i,service in enumerate(sorted(clean.SERVICES))]
    def test_foreign_project_blocks_before_volume_delete(self):
        rows=self.records();rows[0]['Config']['Labels']['com.docker.compose.project']='school'
        with self.assertRaisesRegex(RuntimeError,'OLD_CONTAINER_PROVENANCE'):
            clean.guard_old_objects(rows)
    def test_foreign_volume_user_blocks(self):
        def volume(kind,names):
            role=next(k for k,v in clean.VOLUMES.items() if v==names[0])
            return [{'Labels':{'com.docker.compose.project':clean.PROJECT,'com.docker.compose.volume':role}}]
        with patch.object(clean,'containers',return_value=[]),patch.object(clean,'inspect_objects',side_effect=volume),patch.object(clean,'run',return_value='foreign-id'):
            with self.assertRaisesRegex(RuntimeError,'FOREIGN_CONTAINER_USES_VOLUME'):
                clean.guard_old_objects(self.records())
    def test_foreign_root_bind_blocks(self):
        foreign={'Config':{'Labels':{'com.docker.compose.project':'other'}},'Mounts':[{'Source':str(clean.ROOT/'shared')}]}
        with patch.object(clean,'containers',return_value=[foreign]):
            with self.assertRaisesRegex(RuntimeError,'FOREIGN_CONTAINER_USES_ROOT'):
                clean.guard_old_objects(self.records())
    def test_wrong_phase_cannot_bootstrap_or_activate(self):
        with patch.object(clean,'state',return_value={}),patch.object(clean,'panel') as panel:
            for method in (clean.bootstrap,clean.restore,clean.objects,clean.activate,clean.verify):
                with self.assertRaises(RuntimeError):method({})
            panel.assert_not_called()
    def test_schema_versions_are_numeric(self):
        clean.check_expected_population({'accounts':90,'classrooms':31,'seats':418,'projects':1414,'schema':'0234'},234)
        with self.assertRaisesRegex(RuntimeError,'RESTORED_POPULATION_MISMATCH'):
            clean.check_expected_population({'accounts':90,'classrooms':31,'seats':418,'projects':1417,'schema':'0234'},234)
if __name__=='__main__':unittest.main()
