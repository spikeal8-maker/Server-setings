import hashlib
import importlib.util
from pathlib import Path
import sys
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
            api.request.assert_not_called()
            commands[name]["command"] = "custom command"
            with self.assertRaisesRegex(SystemExit, "уже отличается"):
                menu.reconcile(api, {}, apply=False)

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
