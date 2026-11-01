import importlib.util
from pathlib import Path
import json
import unittest

BASE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("school_yandex_oauth", BASE / "scripts/store-school-yandex-oauth.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class SchoolYandexOAuthTests(unittest.TestCase):
    def make_token(self):
        return {"access_token": "test_" + ("a" * 35),
                "refresh_token": "test_" + ("b" * 35),
                "token_type": "OAuth",
                "expiry": "2027-01-01T00:00:00Z"}

    def test_valid_token_payload_and_rclone_config(self):
        payload = json.dumps(self.make_token()).encode("ascii")
        value = module.decode_token(payload)
        body = module.config_bytes(value)
        self.assertIn(b"[yandex]\ntype = yandex\ntoken = ", body)
        self.assertIn(b'"token_type":"OAuth"', body)
        self.assertTrue(body.endswith(b"\n"))

    def test_requires_access_token(self):
        for bad in (b"", b"garbage", b"[]", b"{}",
                    json.dumps({"access_token": "short", "token_type": "OAuth"}).encode()):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    module.decode_token(bad)

    def test_rejects_huge_or_malformed_payload(self):
        for bad in (b"x" * (module.MAX_BYTES + 1), b"null", b"\"secret\"",
                    json.dumps({"access_token": "x" * 30, "token_type": 123}).encode()):
            with self.subTest(size=len(bad)):
                with self.assertRaises(ValueError):
                    module.decode_token(bad)


if __name__ == "__main__":
    unittest.main()
