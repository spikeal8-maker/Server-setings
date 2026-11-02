import importlib.util
import json
from pathlib import Path, PurePosixPath
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
SCRIPT=Path(__file__).resolve().parents[1]/'scripts/school-internet-offline.py'
spec=importlib.util.spec_from_file_location('offline',SCRIPT);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
class Tests(unittest.TestCase):
    def test_profiles_accept_only_offline_modes(self):
        for profile in ({'mode':'wifi','ssid':'school','password':'abcdefgh','qualified':False},{'mode':'router','interface':'nic2','qualified':False},{'mode':'modem','apn':'internet.operator','qualified':False}):m.validate(profile)
    def test_credentials_and_primary_interface_input_rejected(self):
        for profile in ({'mode':'wifi','ssid':'x\ncommand','password':'abcdefgh','qualified':False},{'mode':'router','interface':'nic1','qualified':False},{'mode':'router','interface':'vmbr0','qualified':False},{'mode':'modem','apn':'internet;command','qualified':False},{'mode':'modem','apn':'internet','pin':'1234','qualified':False},{'mode':'router','interface':'nic0','qualified':True}):
            with self.subTest(mode=profile['mode']),self.assertRaises(m.Stop):m.validate(profile)
    def test_no_secret_in_status(self):
        with patch.object(m,'read_profiles',return_value={'profiles':[{'mode':'wifi','password':'TOPSECRET','ssid':'PRIVATE'}]}),patch('builtins.print') as output:m.status()
        self.assertNotIn('TOPSECRET',str(output.call_args_list));self.assertNotIn('PRIVATE',str(output.call_args_list))
    def test_packages_plan_refuses_removal_upgrade_unreviewed_dependency(self):
        for value in ('Remv proxmox-ve [1]\n','Inst iw [1] (2 Debian)\n','Inst network-manager (1 Debian)\n','Inst iw (999 Debian)\n'):
            with patch.object(m,'baseline'),patch.object(m.subprocess,'run',return_value=SimpleNamespace(stdout=value)),self.assertRaises(m.Stop):m.package_plan()
    def test_packages_plan_never_applies(self):
        with patch.object(m,'baseline'),patch.object(m.subprocess,'run',return_value=SimpleNamespace(stdout='Inst iw (6.9-1 Debian)\n')) as call:
            result=m.package_plan()
        self.assertEqual(result['packages_to_add'],['iw']);self.assertIn('-s',call.call_args.args[0]);self.assertFalse(result['automatic_failover_implemented'])
    def test_arming_stops_before_mutation(self):
        with patch.object(m,'require_host'),patch.object(sys,'argv',['offline','arm']),patch.object(m,'setup') as setup,patch.object(m,'packages_apply') as install,self.assertRaises(m.Stop):m.main()
        setup.assert_not_called();install.assert_not_called()
    def test_wizard_password_uses_hidden_input_and_no_network(self):
        with patch.object(m.sys.stdin,'isatty',return_value=True),patch('builtins.input',side_effect=['1','MySSID']),patch.object(m.getpass,'getpass',return_value='hidden123'),patch.object(m,'save') as save,patch('builtins.print'),patch.object(m.subprocess,'run') as command:
            m.setup()
        self.assertEqual(save.call_args.args[0]['password'],'hidden123');command.assert_not_called()
    def test_trust_rejects_symlink_and_writable_parent(self):
        for mode in (stat.S_IFLNK|0o777,stat.S_IFDIR|0o777):
            def info(path):return SimpleNamespace(st_uid=0,st_mode=mode if path.name=='unsafe' else stat.S_IFDIR|0o755)
            with patch.object(m,'Path',PurePosixPath),patch.object(PurePosixPath,'lstat',info,create=True),self.assertRaises(m.Stop):m.trust(PurePosixPath('/unsafe/file'))
    def test_mutating_actions_default_to_plan(self):
        for action in ('setup','packages-apply'):
            with patch.object(m,'require_host'),patch.object(sys,'argv',['offline',action]),patch.object(m,'setup') as setup,patch.object(m,'packages_apply') as packages,patch('builtins.print'):
                m.main()
            setup.assert_not_called();packages.assert_not_called()
    def test_package_apply_uses_pins_offline_and_only_new_optional_units(self):
        fake_lock=SimpleNamespace(flock=lambda *args:None,LOCK_EX=1,LOCK_NB=2)
        seen=[]
        def command(argv,**kwargs):
            if argv[:2]==['systemctl','show']:
                seen.append(argv[2]);return SimpleNamespace(stdout='not-found\n' if len(seen)<=2 else 'loaded\n')
            return SimpleNamespace(returncode=0)
        with tempfile.TemporaryDirectory() as directory,patch.dict(sys.modules,{'fcntl':fake_lock}),patch.object(m,'ROOT',Path(directory)),patch.object(m,'private_root'),patch.object(m,'trust'),patch.object(m,'package_plan',return_value={}),patch.object(m,'baseline'),patch.object(m,'network_snapshot',return_value={'routes':[],'rules':[]}),patch.object(m,'atomic_private'),patch.object(m.os,'O_NOFOLLOW',0,create=True),patch.object(m.subprocess,'run',side_effect=command) as calls,patch('builtins.print'):
            m.packages_apply()
        commands=[call.args[0] for call in calls.call_args_list]
        install=next(call for call in calls.call_args_list if call.args[0][0]=='apt-get')
        self.assertIn('--no-remove',install.args[0]);self.assertIn('--no-upgrade',install.args[0])
        self.assertIn('modemmanager=1.24.0-1+deb13u1',install.args[0]);self.assertEqual(install.kwargs['env']['SYSTEMD_OFFLINE'],'1')
        disabled=[cmd[-1] for cmd in commands if cmd[:2]==['systemctl','disable']]
        self.assertEqual(disabled,['ModemManager.service','wpa_supplicant.service'])
        self.assertFalse(any(cmd[0] in ('ip','ifreload','ifup','ifdown') for cmd in commands))
    def test_installer_plan_has_no_io(self):
        result=subprocess.run([sys.executable,str(SCRIPT.with_name('install-school-internet-offline.py'))],capture_output=True,text=True)
        self.assertEqual(result.returncode,0);self.assertFalse(json.loads(result.stdout)['network_changed'])
if __name__=='__main__':unittest.main()
