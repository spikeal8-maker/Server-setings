import hashlib
import importlib.util
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("school_menu", SCRIPTS / "install-school-1panel-operator-shortcuts.py")
menu = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(menu)


class OperatorMenuTests(unittest.TestCase):
    def test_migration_ignores_a_modified_old_shortcut(self):
        group, name = "Former managed group", "Former command"
        body = menu.school("original")
        owned = {"groupID": 2, "command": body}
        groups = {"Default": {"id": 1, "isDefault": True}, group: {"id": 2, "isDefault": False}}
        fingerprint = hashlib.sha256((name + "\0" + body).encode()).hexdigest()
        with patch.object(menu, "OLD_GROUPS", {group: (fingerprint,)}):
            self.assertEqual(menu.old_commands(groups, {name: owned}, {}), {name: owned})
            self.assertEqual(menu.old_commands(groups, {name: {**owned, "command": menu.school("custom")}}, {}), {})

    def test_existing_update_button_has_a_read_only_migration_plan(self):
        group = "ASA Lab · Обновление"
        name = "Установить обновление"
        groups = {"Default": {"id": 1, "isDefault": True}, group: {"id": 2, "isDefault": False}}
        commands = {name: {"groupID": 2, "command": menu.school(menu.PREVIOUS_COMMANDS[name])}}
        api = Mock()
        with patch.object(menu, "snapshot", return_value=(groups, commands)), patch("builtins.print") as report:
            menu.reconcile(api, {}, apply=False)
            self.assertIn("commands_to_update=1", report.call_args.args[0])
            commands[name]["command"] = menu.school(f"{menu.ASA} update")
            menu.reconcile(api, {}, apply=False)
            self.assertIn("commands_to_update=1", report.call_args.args[0])
            api.request.assert_not_called()
            commands[name]["command"] = "custom command"
            with self.assertRaisesRegex(SystemExit, "уже отличается"):
                menu.reconcile(api, {}, apply=False)

    def test_update_buttons_store_short_commands_for_the_school_host(self):
        self.assertEqual(dict(menu.MENU[menu.UPDATE_GROUP]), {
            "Проверить обновления": "sudo asa-school check",
            "Установить обновление": "sudo asa-school update",
        })

    @unittest.skipUnless(os.name == "posix", "Tests the Ubuntu shell launcher")
    def test_school_launcher_refuses_to_update_another_1panel_terminal_host(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            hostname = root / "hostname"
            engine = root / "engine"
            hostname.write_text('#!/bin/sh\nprintf "%s\\n" "$TEST_HOST"\n')
            engine.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
            for path in (hostname, engine):
                path.chmod(0o755)
            wrapper = root / "asa-school"
            wrapper.write_text(menu.SCHOOL_LAUNCHER_SOURCE.read_text()
                               .replace("/usr/bin/hostname", str(hostname))
                               .replace("/usr/local/bin/asa", str(engine)))
            for host, expected in (("asa-school-01", 0), ("admins-MS-7D22", 2)):
                with self.subTest(host=host):
                    result = subprocess.run(["sh", str(wrapper), "update"], env={**os.environ, "TEST_HOST": host},
                                            capture_output=True, text=True, timeout=5)
                    self.assertEqual(result.returncode, expected)
                    self.assertEqual(result.stdout, "update\n" if expected == 0 else "")

    def test_h03_shortcut_plan_does_not_archive_an_existing_school_group(self):
        group, name = menu.UPDATE_GROUP, menu.RETIRED_RETURN
        groups = {"Default": {"id": 1, "isDefault": True}, group: {"id": 2, "isDefault": False}}
        prior = {name: {"groupID": 2, "command": menu.school(f"{menu.ASA} rollback-plan")}}
        h03 = {group: tuple((label, "sudo asa-test " + action) for label, action in menu.UPDATE_ACTIONS.items())}
        api = Mock()
        with patch.object(menu, "snapshot", return_value=(groups, prior)), patch("builtins.print") as report:
            menu.reconcile(api, {}, apply=False, menu=h03)
            self.assertIn("commands_to_add=2 old_commands_to_remove=0", report.call_args.args[0])
            api.request.assert_not_called()

    def test_only_managed_return_button_is_archived(self):
        group, name = menu.UPDATE_GROUP, menu.RETIRED_RETURN
        groups = {"Default": {"id": 1, "isDefault": True}, group: {"id": 2, "isDefault": False}}
        row = {"groupID": 2, "command": menu.school(f"{menu.ASA} rollback-plan")}
        self.assertEqual(menu.old_commands(groups, {name: row}, {}), {name: row})
        self.assertEqual(menu.old_commands(groups, {name: {**row, "command": "custom"}}, {}), {})

    def test_legacy_launcher_is_rejected_before_it_can_run(self):
        with patch.object(menu.LAUNCHER.__class__, "lstat", return_value=Mock(st_mode=0o120777, st_uid=0)), \
                patch.object(menu.subprocess, "run") as run:
            with self.assertRaisesRegex(SystemExit, "постоянную команду"):
                menu.require_canonical_updater()
            run.assert_not_called()

    def test_old_protected_launcher_cannot_silently_pass_the_new_contract(self):
        path_type = menu.LAUNCHER.__class__
        settings = '{"profile":"production","project":"asa-lab-school",' \
                   '"updatePolicy":{"receipt":"/var/backups/asa-school-gfs/receipts/latest-full.json"}}'
        with patch.object(path_type, "lstat", return_value=Mock(st_mode=0o100755, st_uid=0)), \
                patch.object(path_type, "read_text", return_value=settings), \
                patch.object(menu.subprocess, "run", return_value=Mock(stdout="ASA_LAUNCHER_OFFSITE_V1\n")) as run:
            with self.assertRaisesRegex(SystemExit, "постоянную команду"):
                menu.require_canonical_updater()
            self.assertEqual(run.call_args.args[0][-1], "/srv/asa-lab")


if __name__ == "__main__":
    unittest.main()
