"""Статические приемочные проверки новых публичных и LAN-скриптов (без root)."""
from pathlib import Path
import subprocess
import unittest
import sys

ROOT = Path(__file__).resolve().parents[1]


def run(script: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(ROOT / "scripts" / script), *args],
                          capture_output=True, text=True, timeout=12, check=False)


class TestEdgeScripts(unittest.TestCase):
    def test_lan_https_plan_is_nondestructive(self):
        p = run("lan-https.sh", "--lan-ip", "192.168.1.115", "--lan-cidr", "192.168.1.0/24")
        self.assertEqual(0, p.returncode, p.stderr)
        self.assertIn("Это план", p.stdout)
        self.assertIn("Публичный ingress", p.stdout)

    def test_lan_refuses_public_address(self):
        p = run("lan-https.sh", "--lan-ip", "94.29.44.211", "--lan-cidr", "192.168.1.0/24")
        self.assertNotEqual(0, p.returncode)

    def test_lan_rejects_wrong_subnet(self):
        p = run("lan-https.sh", "--lan-ip", "192.168.1.115", "--lan-cidr", "10.10.0.0/16")
        self.assertNotEqual(0, p.returncode)

    def test_remote_plan_without_secret(self):
        p = run("tailnet-admin.sh", "--ssh-port", "22", "--panel-port", "19192")
        self.assertEqual(0, p.returncode, p.stderr)
        self.assertIn("Без изменений", p.stdout)

    def test_edge_profile_uses_required_cidr(self):
        p = run("edge-setup.sh", "--profile", "home-static", "--public-ip", "94.29.44.211",
                "--lan-ip", "192.168.1.115", "--lan-cidr", "192.168.1.0/24", "--lan-https")
        self.assertEqual(0, p.returncode, p.stderr)
        self.assertIn("РЕЖИМ ПРОСМОТРА", p.stdout)

    def test_cgnat_does_not_publish_domain(self):
        p = run("edge-setup.sh", "--profile", "home-dynamic", "--domain", "edu.example.org",
                "--public-ip", "94.29.44.211")
        self.assertNotEqual(0, p.returncode)
        self.assertIn("CGNAT", p.stderr)

    def test_unsupported_profile_is_rejected(self):
        p = run("edge-setup.sh", "--profile", "unsafe")
        self.assertNotEqual(0, p.returncode)

    def test_missing_origin_does_not_apply(self):
        p = run("edge-setup.sh", "--profile", "vps", "--domain", "edu.example.org",
                "--public-ip", "94.29.44.211", "--apply")
        self.assertNotEqual(0, p.returncode)
        self.assertTrue("origin" in p.stderr or "root" in p.stderr)


if __name__ == "__main__":
    unittest.main()
