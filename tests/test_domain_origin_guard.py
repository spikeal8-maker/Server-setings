"""Offline tests: no real API credentials, network or server mutations."""
from pathlib import Path
import importlib.util
import tempfile
import unittest


PATH = Path(__file__).resolve().parents[1] / "scripts" / "check-asa-domain-origins.py"
spec = importlib.util.spec_from_file_location("asa_domain_guard", PATH)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class TestDomainOriginGuard(unittest.TestCase):
    def test_existing_ip_school_and_new_domain_allowed(self):
        env = "ASA_PUBLIC_WEB_ORIGINS=https://192.168.1.115:4610,https://94.29.44.211,https://test.asa-lab.ru\n"
        trusted = guard.configured_origins(env)
        self.assertEqual(
            guard.check_configured({"https://test.asa-lab.ru", "https://94.29.44.211"}, trusted),
            [],
        )

    def test_missing_domain_is_blocking(self):
        trusted = guard.configured_origins(
            "ASA_PUBLIC_WEB_ORIGINS=https://192.168.1.115:4610,https://94.29.44.211\n"
        )
        self.assertEqual(
            guard.check_configured({"https://test.asa-lab.ru"}, trusted),
            ["https://test.asa-lab.ru"],
        )

    def test_https_not_wildcards_or_path(self):
        for value in ("http://test.asa-lab.ru", "*", "https://test.asa-lab.ru/auth", "https://user:pass@host"):
            with self.subTest(value=value):
                with self.assertRaises(guard.OriginAuditError):
                    guard.origin_of(value)

    def test_duplicate_declarations_rejected(self):
        with self.assertRaises(guard.OriginAuditError):
            guard.configured_origins(
                "ASA_PUBLIC_WEB_ORIGINS=https://test.asa-lab.ru\n"
                "ASA_PUBLIC_WEB_ORIGINS=https://94.29.44.211\n"
            )

    def test_more_than_eight_origins_rejected(self):
        with self.assertRaises(guard.OriginAuditError):
            guard.configured_origins(
                "ASA_PUBLIC_WEB_ORIGINS=" + ",".join(f"https://example{i}.org" for i in range(9))
            )

    def test_autodiscovery_only_proxy_root_target(self):
        api_calls = []
        def api(path, body):
            api_calls.append((path, body))
            if path == "/websites/search":
                return {
                    "total": 3,
                    "items": [
                        {"id": 2, "type": "proxy", "primaryDomain": "94.29.44.211", "protocol": "HTTPS"},
                        {"id": 5, "type": "proxy", "primaryDomain": "test.asa-lab.ru", "protocol": "HTTPS"},
                        {"id": 6, "type": "proxy", "primaryDomain": "unrelated.example", "protocol": "HTTPS"},
                    ],
                }
            lookup = {
                2: [{"enable": True, "match": "/", "proxyPass": "http://127.0.0.1:4610"}],
                5: [{"enable": True, "match": "/", "proxyPass": "http://127.0.0.1:4610"}],
                6: [{"enable": True, "match": "/", "proxyPass": "http://127.0.0.1:4620"}],
            }
            return lookup[body["id"]]
        self.assertEqual(
            guard.site_origins(api),
            frozenset({"https://94.29.44.211", "https://test.asa-lab.ru"}),
        )
        self.assertEqual(len(api_calls), 4)

    def test_https_required_for_target_site(self):
        def api(path, body):
            if path == "/websites/search":
                return {"total": 1, "items": [{"id": 5, "type": "proxy", "protocol": "HTTP", "primaryDomain": "test.asa-lab.ru"}]}
            return [{"match": "/", "enable": True, "proxyPass": "http://127.0.0.1:4610"}]
        with self.assertRaises(guard.OriginAuditError):
            guard.site_origins(api)

    def test_local_file_check_is_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory) / ".env"
            original = "ASA_PUBLIC_WEB_ORIGINS=https://test.asa-lab.ru\nSECRET=example\n"
            file.write_text(original, encoding="utf-8")
            found = guard.configured_origins(file.read_text(encoding="utf-8"))
            self.assertEqual(found, frozenset({"https://test.asa-lab.ru"}))
            self.assertEqual(file.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()
