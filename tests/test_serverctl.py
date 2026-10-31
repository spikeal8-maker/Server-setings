import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

BASE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("serverctl", BASE / "scripts" / "serverctl.py")
serverctl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(serverctl)


class TestProfiles(unittest.TestCase):
    def test_pinned_1panel_hashes(self):
        locked=json.loads((BASE/'versions.lock.json').read_text(encoding='utf-8'))
        self.assertEqual('v2.3.2', locked['1panel']['version'])
        for digest in locked['1panel']['sha256'].values():
            self.assertEqual(64, len(digest))
            int(digest, 16)

    def test_generated_panel_credentials_satisfy_official_v2_installer(self):
        for _ in range(48):
            password, entrance = serverctl.generate_panel_credentials()
            self.assertRegex(password, r"^[A-Za-z0-9_!@#$%*,.?]{8,30}$")
            self.assertRegex(entrance, r"^[A-Za-z0-9_]{3,30}$")
            self.assertEqual(18, len(password))
            self.assertEqual(23, len(entrance))
            self.assertTrue(entrance.startswith("access_"))

    def test_all_profiles_valid(self):
        for name in ("home-static", "home-dynamic", "vps", "school-lan"):
            with self.subTest(name=name):
                profile = serverctl.load_profile(name)
                self.assertEqual(name, profile["name"])
                self.assertEqual("ubuntu-24.04", profile["os"])
                self.assertEqual(19192, profile["panel_port"])
                self.assertTrue(profile["install_1panel"])

    def test_https_download_is_atomic_and_requires_https(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "artifact.txt"
            with mock.patch.object(serverctl, "is_installed", return_value=True):
                with self.assertRaisesRegex(RuntimeError, "Небезопасная"):
                    serverctl.download("http://example.invalid/a", destination)

                def fake_curl(cmd, **kwargs):
                    self.assertEqual("curl", cmd[0])
                    self.assertIn("--fail", cmd)
                    self.assertIn("--proto", cmd)
                    self.assertIn("=https", cmd)
                    self.assertIn("--proto-redir", cmd)
                    self.assertNotIn("--insecure", cmd)
                    partial = Path(cmd[cmd.index("--output") + 1])
                    self.assertNotEqual(partial, destination)
                    partial.write_bytes(b"checksum-manifest")
                    return subprocess.CompletedProcess(cmd, 0)

                with mock.patch.object(serverctl.subprocess, "run", side_effect=fake_curl):
                    serverctl.download("https://example.invalid/checksums.txt", destination)
            self.assertEqual(b"checksum-manifest", destination.read_bytes())
            self.assertEqual(["artifact.txt"], [p.name for p in Path(tmp).iterdir()])

    def test_failed_https_download_preserves_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "artifact.txt"
            destination.write_bytes(b"original")

            def interrupted_curl(cmd, **kwargs):
                Path(cmd[cmd.index("--output") + 1]).write_bytes(b"partial")
                raise subprocess.CalledProcessError(22, cmd)

            with mock.patch.object(serverctl, "is_installed", return_value=True):
                with mock.patch.object(serverctl.subprocess, "run", side_effect=interrupted_curl):
                    with self.assertRaises(subprocess.CalledProcessError):
                        serverctl.download("https://example.invalid/a", destination)
            self.assertEqual(b"original", destination.read_bytes())
            self.assertEqual(["artifact.txt"], [p.name for p in Path(tmp).iterdir()])

    def test_offline_panel_archive_cache_verified_without_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            cache_dir = state / "verified-artifacts"
            cache_dir.mkdir()
            archive = cache_dir / "1panel-v2.3.2-linux-amd64.tar.gz"
            archive.write_bytes(b"synthetic-pinned-archive")
            archive.chmod(0o600)
            target = state / "copied" / archive.name
            target.parent.mkdir()
            import hashlib
            expected = hashlib.sha256(archive.read_bytes()).hexdigest()
            with mock.patch.object(serverctl, "STATE", state), mock.patch.object(
                serverctl, "download", side_effect=AssertionError("network fallback not allowed")
            ):
                serverctl.stage_panel_bundle("https://resource.1panel.pro/official", target, expected)
            self.assertEqual(archive.read_bytes(), target.read_bytes())

    def test_offline_panel_archive_rejects_bad_checksum_and_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            cache_dir = state / "verified-artifacts"
            cache_dir.mkdir()
            archive = cache_dir / "1panel-v2.3.2-linux-amd64.tar.gz"
            archive.write_bytes(b"untrusted-bytes")
            archive.chmod(0o600)
            target = state / "bundle" / archive.name
            target.parent.mkdir()
            with mock.patch.object(serverctl, "STATE", state), mock.patch.object(
                serverctl, "download", side_effect=AssertionError("must not fetch")
            ):
                with self.assertRaisesRegex(RuntimeError, "SHA256"):
                    serverctl.stage_panel_bundle("https://resource.1panel.pro/official", target, "a" * 64)
                self.assertFalse(target.exists())
                archive.chmod(0o644)
                import hashlib
                with self.assertRaisesRegex(RuntimeError, "права"):
                    serverctl.stage_panel_bundle(
                        "https://resource.1panel.pro/official", target,
                        hashlib.sha256(archive.read_bytes()).hexdigest(),
                    )
                self.assertFalse(target.exists())

    def test_home_dynamic_does_not_open_public_ports(self):
        config = serverctl.load_profile("home-dynamic")
        x = serverctl.plan(config, ["192.168.1.0/24"])
        self.assertFalse(x["Firewall"]["публичные HTTP/HTTPS"])
        self.assertFalse(x["1Panel"]["публичное открытие панели"])
        self.assertEqual([], x["веб-сайты"])

    def test_school_lan_plan_is_private_and_without_tailscale(self):
        config = serverctl.load_profile("school-lan")
        self.assertFalse(config["allow_public_web"])
        self.assertFalse(config["install_tailscale"])
        self.assertFalse(config["admin_over_tailscale"])
        self.assertEqual([], config["websites"])
        with mock.patch.dict(os.environ, {}, clear=True):
            admin_cidrs = serverctl.ssh_admin_cidrs(config, [])
        self.assertEqual(["10.53.0.2/32", "172.23.104.191/32"], admin_cidrs)
        summary = serverctl.plan(config, admin_cidrs)
        self.assertFalse(summary["Firewall"]["публичные HTTP/HTTPS"])
        self.assertFalse(summary["1Panel"]["публичное открытие панели"])
        self.assertNotIn("Авторизация Tailscale", summary["отдельные этапы после установки"])
        self.assertTrue(config["install_docker"])
        self.assertTrue(config["install_1panel"])

    def test_vps_needs_explicit_admin_cidr_when_no_ssh(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual([], serverctl.ssh_admin_cidrs(serverctl.load_profile("vps"), []))

    def test_ssh_connection_port_not_lost(self):
        with mock.patch.dict(os.environ, {"SSH_CONNECTION": "198.51.100.8 50002 203.0.113.5 2221"}):
            x = serverctl.ssh_admin_cidrs(serverctl.load_profile("vps"), [])
            self.assertEqual(["198.51.100.8/32"], x)
            self.assertEqual([22, 2221], serverctl.resolve_ssh_ports(serverctl.load_profile("vps")))

    def test_websites_only_loopback(self):
        conf = serverctl.load_profile("home-static")
        self.assertEqual("http://127.0.0.1:4610", conf["websites"][0]["upstream"])
        conf["websites"][0]["upstream"] = "http://0.0.0.0:5432"
        with mock.patch.object(serverctl, "PROFILES", BASE / "profiles"):
            # Logic in load_profile must reject if the underlying profile contains public upstream.
            with mock.patch.object(Path, "read_text", return_value=json.dumps(conf)):
                with self.assertRaises(ValueError):
                    serverctl.load_profile("home-static")

    def test_readonly_plan_does_not_need_root(self):
        res = subprocess.run([sys.executable, str(BASE / "scripts/serverctl.py"), "plan", "--profile", "home-static"],
                             check=False, text=True, capture_output=True)
        self.assertEqual(0, res.returncode, res.stderr)
        self.assertIn("Ничего не изменено", res.stdout)

    def test_unsupported_host_cannot_apply(self):
        with mock.patch.object(serverctl, "os_release", return_value={"ID": "debian", "VERSION_ID": "13"}):
            with self.assertRaisesRegex(RuntimeError, "Ubuntu"):
                serverctl.verify_host(serverctl.load_profile("vps"))

    def test_repair_old_caddy_only_when_inactive(self):
        with mock.patch.object(serverctl, "service_state", return_value="active"):
            with self.assertRaises(RuntimeError):
                serverctl.repair_stale_public_caddy(apply=True)

    def test_repair_no_side_effects_in_plan(self):
        def states(n, mode="is-active"):
            return "inactive" if mode == "is-active" else "enabled"
        with mock.patch.object(serverctl, "service_state", side_effect=states), mock.patch.object(
            serverctl, "read_command", return_value="0.0.0.0:80 openresty\n0.0.0.0:443 openresty"
        ), mock.patch.object(serverctl, "command") as change:
            serverctl.repair_stale_public_caddy(apply=False)
            change.assert_not_called()


if __name__ == "__main__":
    unittest.main()
