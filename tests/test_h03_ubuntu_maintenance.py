"""CI contracts for the H03 Ubuntu LTS maintenance menu; no apt mutations."""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))


def load(filename: str, name: str):
    spec = importlib.util.spec_from_file_location(name, str(SCRIPTS / filename))
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


ubuntu = load("h03_ubuntu_maintenance.py", "h03_ubuntu_maintenance")
installer = load("install-h03-ubuntu-menu.py", "install_h03_ubuntu_menu")


class UbuntuMaintenanceTests(unittest.TestCase):
    def test_menu_contract(self):
        self.assertEqual(len(installer.MENU), 5)
        self.assertEqual(len({name for name, _ in installer.MENU}), 5)
        for name, line in installer.MENU:
            self.assertTrue(name)
            self.assertTrue(line.startswith(
                "sudo -- /usr/bin/python3 -I -B /usr/local/libexec/asa-h03/ubuntu-maintenance.py "))
            self.assertIn(line.rsplit(" ", 1)[-1], ubuntu.ACTIONS)
            self.assertNotIn(";", line)
            self.assertNotIn("&&", line)
        self.assertIn('Automatic-Reboot "false";', installer.POLICY_TEXT)
        self.assertIn('Automatic-Reboot-WithUsers "false";', installer.POLICY_TEXT)

    def test_critical_classification(self):
        names = [
            "containerd", "tailscale", "network-manager", "linux-image-7.0.0",
            "libc6", "systemd", "docker-ce", "netplan.io", "cloud-init", "nodejs",
            "gnome-shell", "curl",
        ]
        self.assertEqual(set(ubuntu.critical_packages(names)), {
            "containerd", "tailscale", "network-manager", "linux-image-7.0.0",
            "libc6", "systemd", "docker-ce", "netplan.io", "cloud-init",
        })

    def test_dry_run_only_never_installs(self):
        returned = mock.Mock(returncode=0, stdout="No packages found")
        with mock.patch.object(ubuntu, "apt_policy"), \
             mock.patch.object(ubuntu, "apt_candidates", return_value=([], [])), \
             mock.patch.object(ubuntu, "command", return_value=returned) as runner:
            ubuntu.security_check()
            runner.assert_called_once_with(
                ["/usr/bin/unattended-upgrade", "--dry-run"], timeout=270)

    def test_no_pending_security_does_not_run_apt(self):
        with mock.patch.object(ubuntu, "apt_policy"), \
             mock.patch.object(ubuntu, "apt_candidates", return_value=(["nodejs"], [])), \
             mock.patch.object(ubuntu, "command") as runner, \
             mock.patch.object(ubuntu, "confirm") as confirm:
            ubuntu.security_apply()
            runner.assert_not_called()
            confirm.assert_not_called()

    def test_risky_security_package_fails_closed(self):
        with mock.patch.object(ubuntu, "apt_policy"), \
             mock.patch.object(ubuntu, "apt_candidates",
                               return_value=(["containerd"], ["containerd"])), \
             mock.patch.object(ubuntu, "backup_preflight") as backup, \
             mock.patch.object(ubuntu, "confirm") as confirm:
            with self.assertRaises(ubuntu.GuardError):
                ubuntu.security_apply()
            backup.assert_not_called()
            confirm.assert_not_called()

    def test_backup_preflight_requires_current_revision_and_cloud_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshots = root / "backups"
            target = snapshots / "20261116T190207Z-67552e7a"
            target.mkdir(parents=True)
            (target / "manifest.json").write_text(json.dumps({
                "complete": True, "runtime": {"revision": "abc", "schema": 328}
            }), encoding="utf-8")
            now = dt.datetime.now(dt.timezone.utc)
            selected = {
                str(ubuntu.STATE / "installed-release.json"): {"revision": "abc", "schema": 328},
                str(ubuntu.STATE / "installation.json"): {"instanceId": "same"},
                str(ubuntu.LAST_CLOUD): {
                    "at": now.isoformat(), "status": "success", "backup_verified": True,
                    "cloud_roundtrip_verified": True, "runtime_revision": "abc",
                    "runtime_schema": 328, "installation_instance_id": "same",
                },
                str(ubuntu.RESTORE_WITNESS): {
                    "local_restore_passed": True, "cleanup_verified": True,
                    "revision": "abc", "schema": 328, "snapshot": target.name
                },
            }
            with mock.patch.object(ubuntu, "ROOT", root), \
                 mock.patch.object(ubuntu, "safe_json",
                                   side_effect=lambda p: selected[str(p)]), \
                 mock.patch.object(ubuntu, "service_active", return_value=True), \
                 mock.patch.object(ubuntu, "command",
                                   return_value=mock.Mock(stdout="Контейнеры: 6/6 healthy")):
                ubuntu.backup_preflight()
                selected[str(ubuntu.LAST_CLOUD)]["cloud_roundtrip_verified"] = False
                with self.assertRaises(ubuntu.GuardError):
                    ubuntu.backup_preflight()

    def test_operator_menu_prevents_foreign_command_collision(self):
        groups = {installer.GROUP: {"id": 20}}
        name = installer.MENU[0][0]
        good = installer.MENU[0][1]
        installer.guard_existing(groups, {name: {"groupID": 20, "command": good}})
        for mutation in (
            {"groupID": 20, "command": "sudo apt-get full-upgrade -y"},
            {"groupID": 3, "command": good},
        ):
            with self.assertRaises(RuntimeError):
                installer.guard_existing(groups, {name: mutation})


if __name__ == "__main__":
    unittest.main()
