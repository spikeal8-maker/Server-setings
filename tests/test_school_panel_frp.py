import importlib.util
import json
from pathlib import Path, PurePosixPath
import subprocess
import sys
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import stat

SCRIPT=Path(__file__).resolve().parents[1]/'scripts/school-panel-via-frp.py'
spec=importlib.util.spec_from_file_location('schoolpanel',SCRIPT)
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
RAW='''serverAddr = "161.104.46.160"
serverPort = 7000
transport.tls.enable = true
[[proxies]]
name = "asa-school-100-x"
type = "http"
localIP = "127.0.0.1"
localPort = 80
customDomains = ["x.asa-lab.ru"]
'''
class Tests(unittest.TestCase):
    def test_plan_no_privileged_io(self):
        for role in ('school','edge'):
            r=subprocess.run([sys.executable,str(SCRIPT),role],capture_output=True,text=True)
            self.assertEqual(r.returncode,0,r.stderr);self.assertTrue(json.loads(r.stdout)['plan'])
    def test_frp_minimal_idempotent(self):
        value=m.frp_candidate(RAW)
        self.assertEqual(m.frp_candidate(value),value)
        self.assertIn('"x.asa-lab.ru", "1panel.x.asa-lab.ru"',value)
    def test_frp_rejects_drift(self):
        for before,after in [('7000','7001'),('true','false'),('127.0.0.1','0.0.0.0'),('"x.asa-lab.ru"','"other.asa-lab.ru"')]:
            with self.subTest(after=after),self.assertRaises(RuntimeError):m.frp_candidate(RAW.replace(before,after))
    def test_other_proxies_preserved(self):
        raw=RAW+'\n[[proxies]]\nname="other"\ntype="tcp"\nlocalPort=42\nremotePort=442\n'
        self.assertEqual(m.tomllib.loads(m.frp_candidate(raw))['proxies'][1],m.tomllib.loads(raw)['proxies'][1])
    def test_edge_exact_routes(self):
        doc=m.edge_document()['http']
        self.assertEqual(len(doc['routers']),2)
        for r in doc['routers'].values():self.assertEqual(r['rule'],'Host(`1panel.x.asa-lab.ru`)')
        self.assertEqual(doc['services']['asa-school-panel']['loadBalancer'],{'passHostHeader':True,'servers':[{'url':'http://frps:8080'}]})
        self.assertEqual(doc['routers']['asa-school-panel-https']['tls'],{'certResolver':'letsencrypt'})
    def test_trust_rejects_writable_ancestor(self):
        def info(path):
            mode=stat.S_IFDIR | (0o777 if path.name=='unsafe' else 0o755)
            return SimpleNamespace(st_mode=mode,st_uid=0)
        with patch.object(m,'Path',PurePosixPath),patch.object(PurePosixPath,'lstat',info,create=True),self.assertRaises(RuntimeError):
            m.trusted_chain(PurePosixPath('/unsafe/file'))
    def test_trust_rejects_symlink_ancestor(self):
        def info(path):
            return SimpleNamespace(st_mode=(stat.S_IFLNK|0o777 if path.name=='link' else stat.S_IFDIR|0o755),st_uid=0)
        with patch.object(m,'Path',PurePosixPath),patch.object(PurePosixPath,'lstat',info,create=True),self.assertRaises(RuntimeError):
            m.trusted_chain(PurePosixPath('/link/file'))
    def test_pins_match_git_blobs(self):
        import hashlib
        for name,digest in m.COMPANION_SHA256.items():
            raw=subprocess.check_output(['git','show','HEAD:scripts/'+name],cwd=SCRIPT.parents[1])
            self.assertEqual(hashlib.sha256(raw).hexdigest(),digest)
    def test_only_exact_coolify_ancestors_allow_uid1000(self):
        def info(path):
            uid=1000 if str(path).replace('\\','/').endswith(('/data/coolify/proxy','/data/coolify/proxy/dynamic')) else 0
            return SimpleNamespace(st_mode=stat.S_IFDIR|0o700,st_uid=uid)
        with patch.object(m,'Path',PurePosixPath),patch.object(PurePosixPath,'lstat',info,create=True):
            m.trusted_chain(PurePosixPath('/data/coolify/proxy/dynamic'),public_edge=True)
            with self.assertRaises(RuntimeError):m.trusted_chain(PurePosixPath('/data/coolify/proxy/dynamic'))
    def test_unrelated_uid1000_ancestor_rejected(self):
        def info(path):return SimpleNamespace(st_mode=stat.S_IFDIR|0o700,st_uid=1000 if path.name=='other' else 0)
        with patch.object(m,'Path',PurePosixPath),patch.object(PurePosixPath,'lstat',info,create=True),self.assertRaises(RuntimeError):
            m.trusted_chain(PurePosixPath('/other/file'),public_edge=True)
    def test_writer_drops_privileges_before_path_io(self):
        events=[]
        with patch.object(m.os,'setgroups',lambda value:events.append('groups'),create=True),patch.object(m.os,'setgid',lambda value:events.append('gid'),create=True),patch.object(m.os,'setuid',lambda value:events.append('uid'),create=True),patch.object(m.os,'getuid',return_value=1000,create=True),patch.object(m.os,'geteuid',return_value=1000,create=True),patch.object(m,'trusted_chain',side_effect=RuntimeError('stop')):
            with self.assertRaises(RuntimeError):m.edge_writer(b'public')
        self.assertEqual(events,['groups','gid','uid'])
    def test_wrong_host_before_io(self):
        r=subprocess.run([sys.executable,str(SCRIPT),'school','--apply','--expected-host','wrong'],capture_output=True,text=True)
        self.assertNotEqual(r.returncode,0);self.assertNotIn('Traceback',r.stderr)
if __name__=='__main__':unittest.main()
