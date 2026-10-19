import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
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

    def test_all_profiles_valid(self):
        for name in ("home-static", "home-dynamic", "vps"):
            with self.subTest(name=name):
                profile = serverctl.load_profile(name)
                self.assertEqual(name, profile["name"])
                self.assertEqual("ubuntu-24.04", profile["os"])
                self.assertEqual(19192, profile["panel_port"])
                self.assertTrue(profile["install_1panel"])

    def test_home_dynamic_does_not_open_public_ports(self):
        config = serverctl.load_profile("home-dynamic")
        x = serverctl.plan(config, ["192.168.1.0/24"])
        self.assertFalse(x["Firewall"]["публичные HTTP/HTTPS"])
        self.assertFalse(x["1Panel"]["публичное открытие панели"])
        self.assertEqual([], x["веб-сайты"])

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
