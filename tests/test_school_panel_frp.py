import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest

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
    def test_wrong_host_before_io(self):
        r=subprocess.run([sys.executable,str(SCRIPT),'school','--apply','--expected-host','wrong'],capture_output=True,text=True)
        self.assertNotEqual(r.returncode,0);self.assertNotIn('Traceback',r.stderr)
if __name__=='__main__':unittest.main()
