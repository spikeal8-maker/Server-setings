import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("school_yandex_connect", ROOT / "scripts/school-yandex-connect.py")
subject = importlib.util.module_from_spec(spec)
spec.loader.exec_module(subject)


class SchoolYandexConnectTests(unittest.TestCase):
    def test_official_link_and_pkce_s256(self):
        uri = subject.build_authorize_url("customapp1234567890", "A" * 44)
        parsed = urlsplit(uri)
        query = parse_qs(parsed.query)
        self.assertEqual((parsed.scheme, parsed.netloc, parsed.path),
                         ("https", "oauth.yandex.ru", "/authorize"))
        self.assertEqual(query["redirect_uri"], ["https://oauth.yandex.ru/verification_code"])
        self.assertEqual(query["response_type"], ["code"])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["force_confirm"], ["yes"])
        self.assertNotIn("client_secret", parsed.query)
        self.assertNotIn("access_token", parsed.query)

    def test_invalid_client_id_or_pkce(self):
        for client in ("", "short", "malicious&secret=x", "C" * 300):
            with self.subTest(client=client[:10]):
                with self.assertRaises(subject.EnrollmentError):
                    subject.build_authorize_url(client, "A" * 44)
        with self.assertRaises(subject.EnrollmentError):
            subject.build_authorize_url("customapp123456", "foo")

    def test_screen_code_and_redirect_code(self):
        self.assertEqual(subject.normalize_code("1234567"), "1234567")
        self.assertEqual(
            subject.normalize_code("https://oauth.yandex.ru/verification_code?code=AB-cd_70"),
            "AB-cd_70")
        for value in ("https://evil.example/verification_code?code=1234567",
                      "http://oauth.yandex.ru/verification_code?code=1234567",
                      "https://oauth.yandex.ru/verification_code?error=access_denied",
                      "1234567&other=1"):
            with self.subTest(value=value):
                with self.assertRaises(subject.EnrollmentError):
                    subject.normalize_code(value)

    def test_refresh_token_required_for_independent_unattended_operation(self):
        now = datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc)
        token = subject.safe_token_response({
            "access_token": "test_" + "a" * 40,
            "refresh_token": "test_" + "b" * 40,
            "expires_in": 3600,
            "token_type": "OAuth",
        }, now)
        self.assertEqual(token["token_type"], "OAuth")
        self.assertEqual(token["expiry"], "2026-11-01T00:59:00Z")
        self.assertEqual(token["refresh_token"], "test_" + "b" * 40)
        with self.assertRaises(subject.EnrollmentError):
            subject.safe_token_response({
                "access_token": "test_" + "a" * 40, "expires_in": 3600,
            }, now)

    def test_missing_invalid_expiry(self):
        for expiry in (None, True, -1, 2, "3600", 999999999):
            with self.subTest(expiry=expiry):
                with self.assertRaises(subject.EnrollmentError):
                    subject.safe_token_response({
                        "access_token": "A" * 40,
                        "refresh_token": "B" * 40,
                        "expires_in": expiry,
                    })

    def test_rollback_when_yandex_file_probe_fails(self):
        class SimulatedRepair:
            results = [(False, True), (False, False)]

            @staticmethod
            def check_identity():
                return

            @staticmethod
            def build_config(token, client_id, secret):
                return (client_id + ":" + secret).encode()

            @staticmethod
            def is_failed_standard_config(value):
                return value == b"OLD_BROKEN_410"

            @classmethod
            def remote_probe(cls):
                return cls.results.pop(0)

            @staticmethod
            def write_locked(stream, data):
                stream.seek(0)
                stream.truncate(0)
                stream.write(data)
                stream.flush()

        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "rclone.conf"
            config.write_bytes(b"OLD_BROKEN_410")
            with patch.object(subject, "load_peer", return_value=SimulatedRepair), \
                 patch.object(subject, "CLOUD_CONFIG", config):
                with self.assertRaises(subject.EnrollmentError):
                    subject.apply_verified_oauth({"access_token": "A"*40}, "client-app-id", "client-secret")
            self.assertEqual(config.read_bytes(), b"OLD_BROKEN_410")
            self.assertEqual(SimulatedRepair.results, [])

    def test_existing_custom_config_not_overwritten(self):
        class SimulatedRepair:
            @staticmethod
            def check_identity(): return
            @staticmethod
            def build_config(*args): return b"REPLACEMENT"
            @staticmethod
            def is_failed_standard_config(value): return False
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "rclone.conf"
            config.write_bytes(b"WORKING_CUSTOM")
            with patch.object(subject, "load_peer", return_value=SimulatedRepair), \
                 patch.object(subject, "CLOUD_CONFIG", config):
                with self.assertRaises(subject.EnrollmentError):
                    subject.apply_verified_oauth({"access_token": "A"*40}, "client-app-id", "client-secret")
            self.assertEqual(config.read_bytes(), b"WORKING_CUSTOM")


if __name__ == "__main__":
    unittest.main()
