import importlib.util
from pathlib import Path
import unittest

SRC = Path(__file__).resolve().parents[1] / "scripts/promote-school-local-mcp-full.py"
spec = importlib.util.spec_from_file_location("school_mcp_full", SRC)
target = importlib.util.module_from_spec(spec)
spec.loader.exec_module(target)


class SchoolMcpFullTests(unittest.TestCase):
    def test_exact_tool_inventory(self):
        self.assertEqual(len(target.EXPECTED_READONLY), 6)
        self.assertEqual(len(target.EXPECTED_FULL), 11)
        self.assertTrue(target.EXPECTED_READONLY <= target.EXPECTED_FULL)
        self.assertIn("install_openresty", target.EXPECTED_FULL)
        self.assertIn("create_website", target.EXPECTED_FULL)

    def test_rejects_other_host(self):
        base = (b"PANEL_HOST=http://127.0.0.1:19192\n"
                b"PANEL_ACCESS_TOKEN=" + b"a" * 32 +
                b"\nMCP_AUTH_TOKEN=" + b"b" * 64 +
                b"\nMCP_ACCESS_LEVEL=readonly\n")
        original, mode, auth = target.parse_environment(base)
        self.assertEqual(original, base)
        self.assertEqual(mode, "readonly")
        self.assertEqual(auth, "b" * 64)
        bad = base.replace(b"http://127.0.0.1:19192", b"http://example.org:19192")
        with self.assertRaises(target.SafetyError):
            target.parse_environment(bad)

    def test_rejects_duplicate_keys_and_unknown_modes(self):
        base = (b"PANEL_HOST=http://127.0.0.1:19192\n"
                b"PANEL_ACCESS_TOKEN=" + b"a" * 32 +
                b"\nMCP_AUTH_TOKEN=" + b"b" * 64 +
                b"\nMCP_ACCESS_LEVEL=readonly\n")
        for bad in (
            base + b"MCP_AUTH_TOKEN=unexpected\n",
            base.replace(b"readonly", b"readwrite"),
            base.replace(b"readonly", b"root"),
            base + b"NEW_SECRET=something\n",
        ):
            with self.subTest(size=len(bad)):
                with self.assertRaises(target.SafetyError):
                    target.parse_environment(bad)


if __name__ == "__main__":
    unittest.main()
