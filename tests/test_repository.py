from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]


class TestHygiene(unittest.TestCase):
    def test_secret_never_committed_in_source(self):
        # Не хранить реальные секреты даже в форме конкатенации строк.
        forbidden = ["-----BEGIN " + "OPENSSH PRIVATE KEY-----", "-----BEGIN " + "PRIVATE KEY-----"]
        standalone_32hex = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{32}(?![0-9a-fA-F])")
        for path in ROOT.rglob("*"):
            if not path.is_file() or ".git" in path.parts or "__pycache__" in path.parts:
                continue
            try: content = path.read_text(encoding="utf-8")
            except UnicodeError: continue
            for token in forbidden:
                self.assertNotIn(token, content, str(path))
            # 1Panel API-ключи имеют 32 hex символа; SHA1/SHA256 длиной 40/64 допускаются.
            self.assertIsNone(standalone_32hex.search(content), str(path))

    def test_required_docs(self):
        self.assertTrue((ROOT / "AGENTS.md").exists())
        self.assertTrue((ROOT / "SECURITY.md").exists())
        self.assertTrue((ROOT / "snapshots/2026-10-19/test-ubuntu.json").exists())
        self.assertTrue((ROOT / "docs/05-API-И-MCP.md").exists())
        self.assertTrue((ROOT / "scripts/setup-mcp.sh").exists())

    def test_main_scripts_require_apply(self):
        self.assertIn('"$@"', (ROOT / 'install.sh').read_text())
        self.assertIn('args.apply', (ROOT / 'scripts/serverctl.py').read_text())
        self.assertIn('args.apply', (ROOT / 'scripts/panelctl.py').read_text())


if __name__ == "__main__":
    unittest.main()
