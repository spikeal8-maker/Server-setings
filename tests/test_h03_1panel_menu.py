"""Regression contracts for the H03 1Panel ASA LAB owner menu."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))


def module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, str(path))
    item = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(item)
    return item


installer = module(SCRIPTS / "install-h03-1panel-menu.py", "h03_panel_menu")
dispatcher = module(SCRIPTS / "h03_asa_menu.py", "h03_terminal_dispatcher")


class H03MenuTests(unittest.TestCase):
    def test_menu_structure_and_action_mapping(self):
        self.assertEqual(len(installer.MENU), 4)
        entries = [(title, cmd) for group in installer.MENU.values() for title, cmd in group]
        self.assertEqual(len(entries), 17)
        self.assertEqual(len(set(title for title, _ in entries)), 17)
        self.assertEqual(installer.MENU[installer.BASE_GROUP], (
            ("Проверить обновления", "sudo asa-test check"),
            ("Установить обновление", installer.HARDENED_UPDATE_COMMAND),
        ))
        self.assertEqual(
            installer.HARDENED_UPDATE_COMMAND,
            "sudo -n -H -u admins -- /bin/sh -c "
            "'umask 0077; "
            "export XDG_RUNTIME_DIR=/run/user/1000 "
            "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus; "
            "exec /usr/local/bin/asa-test update'",
        )
        self.assertIn("DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus", installer.HARDENED_UPDATE_COMMAND)
        self.assertIn("XDG_RUNTIME_DIR=/run/user/1000", installer.HARDENED_UPDATE_COMMAND)
        self.assertIn("umask 0077;", installer.HARDENED_UPDATE_COMMAND)
        self.assertNotIn("--no-backup", installer.HARDENED_UPDATE_COMMAND)
        self.assertNotIn("--force", installer.HARDENED_UPDATE_COMMAND)
        for title, command in entries[2:]:
            self.assertTrue(command.startswith("sudo -- /usr/bin/python3 -I -B /usr/local/libexec/asa-h03/asa-menu.py "))
            action = command.rsplit(" ", 1)[-1]
            self.assertIn(action, dispatcher.ACTIONS)
            self.assertNotIn(";", command)
            self.assertNotIn("&&", command)
            self.assertNotIn("|", command)

    def test_collision_and_legacy_hash_guard(self):
        groups = {
            "Default": {"id": 3, "name": "Default"},
            installer.BASE_GROUP: {"id": 9, "name": installer.BASE_GROUP},
        }
        commands = {
            "Проверить обновления": {"groupID": 9, "command": "sudo asa-test check"},
            "Установить обновление": {"groupID": 9, "command": "sudo asa-test update"},
            "Обновление": {"groupID": 3, "command": "sudo asa update"},
        }
        desired, old = installer.managed_commands(groups, commands)
        self.assertEqual(len(desired), 17)
        self.assertEqual(list(old), ["Обновление"])
        complete = dict(commands)
        complete["Установить обновление"] = {
            "groupID": 9, "command": installer.HARDENED_UPDATE_COMMAND,
        }
        self.assertEqual(installer.managed_commands(groups, complete)[0], desired)
        previous = dict(commands)
        previous["Установить обновление"] = {
            "groupID": 9, "command": installer.PREVIOUS_HARDENED_UPDATE_COMMAND,
        }
        self.assertEqual(installer.managed_commands(groups, previous)[0], desired)
        broken = dict(commands)
        broken["Установить обновление"] = {
            "groupID": 9, "command": "sudo asa-test update --no-backup",
        }
        with self.assertRaises(installer.MenuConflict):
            installer.managed_commands(groups, broken)
        bad = dict(commands)
        bad["Обновление"] = {"groupID": 3, "command": "sudo asa update --force"}
        with self.assertRaises(installer.MenuConflict):
            installer.managed_commands(groups, bad)
        bad = dict(commands)
        bad["Проверить обновления"] = {"groupID": 3, "command": "sudo asa-test check"}
        with self.assertRaises(installer.MenuConflict):
            installer.managed_commands(groups, bad)

    def test_archive_name_validation_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(dispatcher, "ROOT", root):
                backups = root / "backups"
                backups.mkdir()
                target = backups / "20261116T160803Z-0d75afb7"
                target.mkdir()
                (target / "manifest.json").write_text("{}")
                with mock.patch.object(dispatcher, "require_file"):
                    self.assertEqual(dispatcher.approved_backup(target.name), target)
                for name in ("../etc", "/etc/passwd", ".backup", "x", "folder/../backup", "bad filename"):
                    with self.assertRaises(dispatcher.MenuError, msg=name):
                        dispatcher.approved_backup(name)

    def test_symlink_backup_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "backups").mkdir()
            (root / "elsewhere").mkdir()
            (root / "elsewhere" / "manifest.json").write_text("{}")
            (root / "backups" / "linkbackup").symlink_to(root / "elsewhere")
            with mock.patch.object(dispatcher, "ROOT", root):
                with self.assertRaises(dispatcher.MenuError):
                    dispatcher.approved_backup("linkbackup")

    def test_no_automatic_backup_or_restore(self):
        with mock.patch.object(dispatcher, "runtime_identity"):
            with mock.patch.object(dispatcher, "owner_launcher") as launch:
                dispatcher.run_action("status")
                dispatcher.run_action("report")
                launch.assert_any_call("status", timeout=90)
                launch.assert_any_call("report", timeout=90)
                with mock.patch.object(dispatcher, "confirm", side_effect=dispatcher.MenuError("cancel")):
                    with self.assertRaises(dispatcher.MenuError):
                        dispatcher.run_action("backup-local")
                    with self.assertRaises(dispatcher.MenuError):
                        dispatcher.run_action("backup-cloud")
                    with self.assertRaises(dispatcher.MenuError):
                        dispatcher.run_action("restore-test")
                self.assertEqual(launch.call_count, 2)

    def test_restore_targets_new_database_only(self):
        with mock.patch.object(dispatcher, "runtime_identity"):
            with mock.patch.object(dispatcher, "select_backup", return_value=Path("/srv/asa-lab/backups/20261116T160803Z-0d75afb7")):
                with mock.patch.object(dispatcher, "confirm"):
                    with mock.patch.object(dispatcher, "owner_launcher") as launch:
                        dispatcher.run_action("restore-test")
                        args, kw = launch.call_args
                        self.assertEqual(args[0:3], ("restore-check", "--backup", "/srv/asa-lab/backups/20261116T160803Z-0d75afb7"))
                        self.assertEqual(args[3], "--database")
                        self.assertTrue(args[4].endswith("_test"))
                        self.assertLessEqual(len(args[4]), 63)

    def test_age_and_cloud_name_contract(self):
        self.assertIsNone(dispatcher.date_age_hours("invalid"))
        self.assertTrue(dispatcher.CLOUD_RE.fullmatch("asa-nightly-20261109T233000Z.tar.age"))
        self.assertFalse(dispatcher.CLOUD_RE.fullmatch("../tokens.age"))
        self.assertFalse(dispatcher.CLOUD_RE.fullmatch("secret.tar"))

    def test_backup_listing_prefers_complete_manifests(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "backups" / "20261116T160803Z-0d75afb7"
            first.mkdir(parents=True)
            (first / "manifest.json").write_text(json.dumps({"complete": True, "runtime": {"schema": 314, "revision": "a"}}))
            second = root / "backups" / "20261117T120000Z-bbbb"
            second.mkdir()
            (second / "manifest.json").write_text(json.dumps({"complete": False, "runtime": {"schema": 328}}))
            with mock.patch.object(dispatcher, "ROOT", root):
                rows = dispatcher.backup_manifests()
                self.assertEqual(rows, [(first.name, 314, "a")])


if __name__ == "__main__":
    unittest.main()
