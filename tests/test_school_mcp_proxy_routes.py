import importlib.util
from email.message import Message
from pathlib import Path
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / "scripts/publish-school-mcp-via-1panel.py"
spec = importlib.util.spec_from_file_location("school_mcp_route", SOURCE)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class SchoolMcpRouteTests(unittest.TestCase):
    def test_case_insensitive_headers(self):
        for casing in ("WWW-Authenticate", "Www-Authenticate", "www-authenticate"):
            with self.subTest(casing=casing):
                headers = Message()
                headers[casing] = "challenge"
                result = mod.canonical_headers(headers)
                self.assertEqual(result.get("www-authenticate"), "challenge")

    def test_only_five_non_root_proxy_paths(self):
        self.assertEqual(len(mod.PATHS), 5)
        self.assertNotIn("/", [value[2] for value in mod.PATHS])
        self.assertEqual(mod.MCP, "http://127.0.0.1:39450")

    def test_root_proxy_preflight_rejects_drift(self):
        cfg = {"name": "root", "modifier": "^~", "match": "/",
               "proxyPass": "http://127.0.0.1:4610", "proxyHost": "$host"}
        with patch.object(mod, "proxies", return_value={"root": cfg}):
            mod.unchanged_root(object())
        cfg["proxyPass"] = "http://127.0.0.1:39450"
        with patch.object(mod, "proxies", return_value={"root": cfg}):
            with self.assertRaises(mod.PublishError):
                mod.unchanged_root(object())


if __name__ == "__main__":
    unittest.main()
