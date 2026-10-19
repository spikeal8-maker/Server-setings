import importlib.util
import json
from pathlib import Path
import socket
import subprocess
import sys
import unittest
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "scripts"))
spec = importlib.util.spec_from_file_location("domainctl", BASE / "scripts" / "domainctl.py")
domainctl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(domainctl)


class DomainProvisionTests(unittest.TestCase):
    def test_dns_valid_and_idna(self):
        self.assertEqual("shop.example.org", domainctl.canonical_domain(" Shop.Example.org. "))
        self.assertEqual("xn--e1afmkfd.xn--p1ai", domainctl.canonical_domain("пример.рф"))

    def test_domain_rejects_local_and_ip(self):
        for name in ("192.168.1.115", "localhost", "demo.local", "demo.test", "https://test.org", "example.org:8080", "bad..org", "-start.org"):
            with self.subTest(domain=name), self.assertRaises(domainctl.DomainError):
                domainctl.canonical_domain(name)

    def test_expected_public_ipv4_only(self):
        self.assertEqual("94.29.44.211", domainctl.expected_public_ipv4("94.29.44.211"))
        for ip in ("127.0.0.1", "192.168.1.115", "100.64.0.3", "203.0.113.4"):
            with self.subTest(ip=ip), self.assertRaises(domainctl.DomainError):
                domainctl.expected_public_ipv4(ip)

    def test_dns_mismatch_cannot_deploy(self):
        with mock.patch.object(domainctl, "dns_addresses", return_value=({"198.51.100.4"}, set())):
            with self.assertRaisesRegex(domainctl.DomainError, "DNS A"):
                domainctl.require_dns_ready("demo.example.org", "94.29.44.211")

    def test_dns_ipv6_route_must_be_known(self):
        with mock.patch.object(domainctl, "dns_addresses", return_value=({"94.29.44.211"}, {"2001:db8::4"})):
            with self.assertRaisesRegex(domainctl.DomainError, "AAAA"):
                domainctl.require_dns_ready("demo.example.org", "94.29.44.211")

    def test_dns_valid(self):
        with mock.patch.object(domainctl, "dns_addresses", return_value=({"94.29.44.211"}, set())):
            self.assertEqual({"A": ["94.29.44.211"], "AAAA": []}, domainctl.require_dns_ready("demo.example.org", "94.29.44.211"))

    def test_no_other_backend(self):
        for p in (0, -1, 65536):
            with self.assertRaises(domainctl.DomainError): domainctl.validate_upstream(p)
        self.assertEqual("http://127.0.0.1:4610", domainctl.validate_upstream(4610))

    def test_acme_requires_single_le_account(self):
        class API:
            def request(self, *args):
                return {"total": 2, "items": [{"id": 1, "type": "letsencrypt"}, {"id": 2, "type": "letsencrypt"}]}
        with self.assertRaisesRegex(domainctl.DomainError, "ровно одн"):
            domainctl.acme_account(API())

    def test_existing_site_without_ssl_rejected_before_mutation(self):
        class API:
            def request(self, verb, url, body=None):
                if url == "/websites/search": return {"total": 1, "items": [{"id": 5, "primaryDomain": "demo.example.org", "alias": "site-demo-example-org", "type": "proxy"}]}
                if url == "/websites/ssl/search": return {"total": 0, "items": []}
                if url == "/websites/acme/search": return {"total": 1, "items": [{"id": 2, "type": "letsencrypt"}]}
                if url == "/groups/search": return [{"id": 3, "isDefault": True}]
                raise AssertionError("UNEXPECTED WRITE "+url)
        fake = API()
        with mock.patch.object(domainctl, "require_local_write"), mock.patch.object(domainctl, "require_dns_ready"), mock.patch.object(domainctl, "upstream_ready"):
            with self.assertRaisesRegex(domainctl.DomainError, "уже существует"):
                domainctl.provision(fake, "demo.example.org", "94.29.44.211", 4610, "site-demo-example-org", apply=True)

    def test_plan_requires_no_access_key_or_network(self):
        proc = subprocess.run([sys.executable, str(BASE / "scripts/domainctl.py"), "plan", "--domain", "edu.example.org", "--public-ip", "94.29.44.211"], capture_output=True, text=True)
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual("DRY_RUN", json.loads(proc.stdout)["mode"])

    def test_apply_requires_origin_confirmation_even_with_key(self):
        proc = subprocess.run([sys.executable, str(BASE / "scripts/domainctl.py"), "apply", "--domain", "edu.example.org", "--public-ip", "94.29.44.211"], capture_output=True, text=True)
        self.assertEqual(2, proc.returncode)
        self.assertIn("origin", proc.stderr)

    def test_certificate_ready_must_autorenew(self):
        class API:
            def request(self, verb, url, body=None):
                if url == "/websites/ssl/search": return {"total": 1, "items": [{"id": 9, "primaryDomain": "demo.example.org", "status": "ready", "autoRenew": False}]}
                raise AssertionError("Write forbidden")
        with self.assertRaisesRegex(domainctl.DomainError, "autoRenew"):
            domainctl.ssl_id(API(), "demo.example.org", 3, False)

    def test_website_group_cannot_be_ambiguous(self):
        class API:
            def request(self, *args): return [{"id": 1, "isDefault": True}, {"id": 2, "isDefault": True}]
        with self.assertRaises(domainctl.DomainError): domainctl.website_group(API())

    def test_existing_matching_ssl_is_idempotent(self):
        class API:
            def __init__(self): self.calls = []
            def request(self, verb, url, body=None):
                self.calls.append((verb, url))
                return {"enable": True, "SSL": {"id": 19}}
        api = API()
        domainctl.attach_https(api, 5, 19)
        self.assertEqual([("GET", "/websites/5/https")], api.calls)

    def test_unidentified_ssl_on_existing_https_rejected(self):
        class API:
            def request(self, verb, url, body=None):
                if verb != "GET": raise AssertionError("Unexpected write")
                return {"enable": True, "websiteSSLId": 19}
        with self.assertRaisesRegex(domainctl.DomainError, "SSL.id"):
            domainctl.attach_https(API(), 5, 19)

    def test_new_https_requires_bound_ssl_id_proof(self):
        class API:
            def __init__(self): self.count=0
            def request(self, verb, url, body=None):
                if verb == "GET":
                    self.count += 1
                    return {"enable": False} if self.count == 1 else {"enable": True, "SSL": {"id": 11}}
                if verb == "POST": return None
                raise AssertionError("Unexpected read")
        with self.assertRaisesRegex(domainctl.DomainError, "ожидаемым SSL.id"):
            domainctl.attach_https(API(), 5, 19)

    def test_does_not_attach_different_ssl_to_live_website(self):
        class API:
            def request(self, verb, url, body=None):
                self.calls.append((verb, url))
                return {"enable": True, "SSL": {"id": 21}}
            calls = []
        api = API()
        with self.assertRaisesRegex(domainctl.DomainError, "другим сертификатом"):
            domainctl.attach_https(api, 5, 19)
        self.assertEqual([("GET", "/websites/5/https")], api.calls)


if __name__ == "__main__": unittest.main()
