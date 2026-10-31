import importlib.util
import json
from pathlib import Path
import unittest

BASE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "repair_yandex", BASE / "scripts/repair-school-yandex-oauth.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class RepairYandexTests(unittest.TestCase):
    def payload(self):
        return {
            "repair_bad_standard": True,
            "client_id": "example-custom-client-id-0123456789",
            "client_secret": "example_secret_abcdefghijklmnopqrst",
            "token": {
                "access_token": "test_" + "a" * 45,
                "refresh_token": "test_" + "b" * 45,
                "token_type": "OAuth",
                "expiry": "2027-01-01T00:00:00Z"
            }
        }

    def test_valid_enrollment_and_config(self):
        token, client_id, secret = mod.parse_payload(
            json.dumps(self.payload()).encode())
        config = mod.build_config(token, client_id, secret)
        self.assertIn(b"client_id = example-custom-client-id", config)
        self.assertIn(b"client_secret = example_secret_", config)
        self.assertIn(b"token = {", config)
        self.assertFalse(mod.is_failed_standard_config(config))

    def test_invalid_tokens_rejected(self):
        cases = [b"", b"{}", b"[]", b"null", b"x" * (mod.MAX_BYTES + 1)]
        for raw in cases:
            with self.subTest(size=len(raw)):
                with self.assertRaises(ValueError):
                    mod.parse_payload(raw)

    def test_requires_explicit_owner_repair_and_custom_app(self):
        for changed in ({"repair_bad_standard": False},
                        {"client_id": ""},
                        {"client_secret": ""},
                        {"token": {"access_token": "short", "token_type": "OAuth"}}):
            val = self.payload()
            val.update(changed)
            with self.subTest(changed=list(changed)):
                with self.assertRaises(ValueError):
                    mod.parse_payload(json.dumps(val).encode())

    def test_only_failed_standard_config_eligible(self):
        old=b'[yandex]\ntype = yandex\ntoken = {"access_token":"old"}\n'
        self.assertTrue(mod.is_failed_standard_config(old))
        self.assertFalse(mod.is_failed_standard_config(b""))
        self.assertFalse(mod.is_failed_standard_config(
            old+b"client_id = custom_app\n"))
        self.assertFalse(mod.is_failed_standard_config(
            b"[someother]\ntype = yandex\n"))


if __name__ == "__main__":
    unittest.main()
