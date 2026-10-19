import importlib.util
from pathlib import Path
import subprocess
import sys
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("public_ipctl", ROOT / "scripts" / "public-ipctl.py")
iptool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(iptool)


class TestPublicIPCtl(unittest.TestCase):
    def test_dry_run_without_api_key(self):
        cp = subprocess.run([sys.executable, str(ROOT / "scripts/public-ipctl.py"), "plan",
                             "--public-ip", "94.29.44.211"], capture_output=True, text=True)
        self.assertEqual(0, cp.returncode, cp.stderr)
        self.assertIn('"mode": "DRY_RUN"', cp.stdout)

    def test_ip_requires_real_public_address(self):
        cp = subprocess.run([sys.executable, str(ROOT / "scripts/public-ipctl.py"), "plan",
                             "--public-ip", "192.168.1.115"], capture_output=True, text=True)
        self.assertNotEqual(0, cp.returncode)

    def test_apply_needs_origin_confirmation_before_key(self):
        cp = subprocess.run([sys.executable, str(ROOT / "scripts/public-ipctl.py"), "apply",
                             "--public-ip", "94.29.44.211"], capture_output=True, text=True)
        self.assertNotEqual(0, cp.returncode)
        self.assertIn("origin", cp.stderr)

    def test_does_not_touch_existing_good_site(self):
        class API:
            def __init__(self): self.calls = []
            def request(self, verb, path, body=None):
                self.calls.append((verb, path))
                if path == "/websites/search":return {"total":1,"items":[{"id":2,"primaryDomain":"94.29.44.211","type":"proxy","alias":"asa-lab-public-ip"}]}
                if path == "/websites/ssl/search":return {"total":1,"items":[{"id":3,"primaryDomain":"94.29.44.211","status":"ready","autoRenew":True}]}
                if path == "/websites/2/https":return {"enable":True,"websiteSSLId":3}
                raise AssertionError("Unexpected mutation: "+path)
        api = API()
        with mock.patch.object(iptool,"require_local_write"), mock.patch.object(iptool,"upstream_ready"):
            out=iptool.provision_ip(api,"94.29.44.211",4610,"asa-lab-public-ip")
        self.assertTrue(out["unchanged"])
        self.assertTrue(all(method == "GET" or path.endswith("/search") for method,path in api.calls))

    def test_incomplete_existing_certificate_fails_without_writes(self):
        class API:
            def request(self, verb, path, body=None):
                if path == "/websites/search":return {"total":1,"items":[{"id":2,"primaryDomain":"94.29.44.211","type":"proxy","alias":"asa-lab-public-ip"}]}
                if path == "/websites/ssl/search":return {"total":0,"items":[]}
                raise AssertionError("Unexpected mutation: "+path)
        with mock.patch.object(iptool,"require_local_write"), mock.patch.object(iptool,"upstream_ready"):
            with self.assertRaisesRegex(iptool.DomainError,"существует"):
                iptool.provision_ip(API(),"94.29.44.211",4610,"asa-lab-public-ip")

    def test_new_site_requests_ip_acme_and_tls_only_on_safe_preflight(self):
        class API:
            def __init__(self):self.site=False;self.cert=False;self.https=False;self.mutations=[]
            def request(self, verb, path, body=None):
                if path == "/websites/search":return {"total":int(self.site),"items":([{"id":4,"primaryDomain":"94.29.44.211","type":"proxy","alias":"asa-lab-public-ip"}] if self.site else [])}
                if path == "/websites/ssl/search":return {"total":int(self.cert),"items":([{"id":7,"primaryDomain":"94.29.44.211","autoRenew":True,"status":"ready"}] if self.cert else [])}
                if path == "/groups/search":return [{"id":10,"isDefault":True}]
                if path == "/websites/acme/search":return {"total":1,"items":[{"id":5,"type":"letsencrypt"}]}
                if path == "/websites":
                    assert body["proxy"] == "http://127.0.0.1:4610"
                    self.site=True;self.mutations.append(path);return None
                if path == "/websites/ssl":
                    assert body["isIp"] is True and body["autoRenew"] is True
                    self.cert=True;self.mutations.append(path);return {"id":7}
                if path == "/websites/4/https" and verb == "GET":return {"enable":self.https,"websiteSSLId":7 if self.https else 0}
                if path == "/websites/4/https" and verb == "POST":
                    assert body["httpConfig"] == "HTTPToHTTPS"
                    self.https=True;self.mutations.append(path);return None
                raise AssertionError(path)
        api=API()
        with mock.patch.object(iptool,"require_local_write"),mock.patch.object(iptool,"upstream_ready"):
            out=iptool.provision_ip(api,"94.29.44.211",4610,"asa-lab-public-ip")
        self.assertEqual("configured_ip_https",out["status"])
        self.assertTrue(api.https)
        self.assertEqual(["/websites","/websites/ssl","/websites/4/https"],api.mutations)


if __name__=="__main__":unittest.main()
