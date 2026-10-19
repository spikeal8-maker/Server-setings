import importlib.util
from io import BytesIO
from pathlib import Path
import json
import os
import tempfile
import unittest
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("panelctl", BASE / "scripts/panelctl.py")
panelctl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(panelctl)


class TestPanelAPI(unittest.TestCase):
    def test_write_requires_local_api(self):
        remote=panelctl.PanelAPI("http://192.168.1.115:19192", "a" * 32)
        with self.assertRaisesRegex(panelctl.PanelError, "локальный"):
            panelctl.require_local_write(remote)

    def test_signature_and_headers(self):
        cap = {}
        class FakeReply:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self, limit): return b'{"code":200,"data":{"items":[],"total":0}}'
        def fake_urlopen(req, timeout):
            cap["headers"] = dict(req.header_items())
            cap["url"] = req.full_url
            return FakeReply()
        with mock.patch.object(panelctl.urllib.request, "urlopen", side_effect=fake_urlopen), mock.patch.object(
                panelctl.time, "time", return_value=1760000000):
            client = panelctl.PanelAPI("http://127.0.0.1:19192", "a" * 32)
            obj = client.request("POST", "/websites/search", {"page": 1})
        self.assertEqual([], obj["items"])
        self.assertIn("1panel-token", cap["headers"])
        self.assertEqual(32, len(cap["headers"]["1panel-token"]))
        self.assertEqual("1760000000", cap["headers"]["1panel-timestamp"])
        self.assertTrue(cap["url"].endswith("/api/v2/websites/search"))

    def test_prevent_open_http_to_public_address(self):
        with self.assertRaises(panelctl.PanelError):
            panelctl.PanelAPI("http://8.8.8.8:19192", "a" * 32)

    def test_no_insecure_file_mode(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "key"
            p.write_text("a" * 32 + "\n")
            p.chmod(0o644)
            with self.assertRaises(panelctl.PanelError):
                panelctl.read_api_key(str(p))
            p.chmod(0o600)
            self.assertEqual("a" * 32, panelctl.read_api_key(str(p)))

    def test_proxy_host_must_be_local(self):
        panelctl.validate_site("94.29.44.211", "http://127.0.0.1:4610", "asa-lab-public-ip")
        for bad in ("http://192.168.1.115:4610", "https://127.0.0.1:4610", "http://127.0.0.1:4610/x", "http://127.0.0.1:4610?x=y"):
            with self.subTest(bad=bad):
                with self.assertRaises(panelctl.PanelError):
                    panelctl.validate_site("94.29.44.211", bad, "asa-lab-public-ip")

    def test_existing_site_not_recreated(self):
        class FakeAPI:
            def request(self, method, endpoint, payload=None):
                if endpoint == "/websites/search": return {"items": [{"primaryDomain": "94.29.44.211", "alias": "asa-lab-public-ip", "type": "proxy"}], "total": 1}
                raise AssertionError("write called for existing site")
        panelctl.create_site(FakeAPI(), "94.29.44.211", "http://127.0.0.1:4610", "asa-lab-public-ip", apply=False)

    def test_observe_sites_and_certs(self):
        class FakeAPI:
            def request(self, method, endpoint, payload=None):
                if endpoint == "/websites/search": return {"total": 1, "items": [{"primaryDomain": "94.29.44.211", "type": "proxy", "status": "Running"}]}
                if endpoint == "/websites/ssl/search": return {"total": 1, "items": [{"primaryDomain": "94.29.44.211", "autoRenew": True}]}
                raise AssertionError(endpoint)
        result = panelctl.inspect(FakeAPI())
        self.assertEqual(1, len(result["сайты"]))
        self.assertTrue(result["сертификаты"][0]["автообновление"])


if __name__ == "__main__":
    unittest.main()
