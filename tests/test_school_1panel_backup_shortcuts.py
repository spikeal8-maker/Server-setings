import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

SRC=Path(__file__).resolve().parents[1]/"scripts/install-school-1panel-backup-shortcuts.py"
spec=importlib.util.spec_from_file_location("school_panel_shortcuts",SRC)
mod=importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class QuickCommandsTests(unittest.TestCase):
    def test_top_sorted_commands_include_backup_and_recovery(self):
        self.assertEqual(len(mod.COMMANDS),11)
        names=[row[0] for row in mod.COMMANDS]
        self.assertEqual(sorted(names),names)
        self.assertEqual(len(set(names)),len(names))
        self.assertEqual(mod.COMMANDS[0][1],"sudo asa-school-backup-now")
        self.assertEqual(mod.COMMANDS[2][1],"sudo asa-school-restore list")
        self.assertEqual(mod.COMMANDS[3][1],"sudo asa-school-restore download")
        self.assertEqual(mod.COMMANDS[4][1],"sudo asa-school-restore prepare")
        self.assertEqual(mod.COMMANDS[5][1],"sudo asa-school-restore restore-test")

    def test_native_api_only_with_no_direct_database_change(self):
        body=SRC.read_text(encoding="utf-8")
        self.assertIn('"/core/commands"',body)
        self.assertIn('"/core/commands/search"',body)
        self.assertIn('"/core/groups/search"',body)
        self.assertNotIn('sqlite3.connect',body)
        self.assertNotIn('UPDATE commands',body)
        self.assertNotIn('DROP TABLE',body)

    def test_readonly_plan_is_idempotent_and_refuses_name_collision(self):
        class Fake:
            def __init__(self,items):
                self.items=items
            def request(self,verb,path,data):
                if path=="/core/groups/search":
                    return [{"id":3,"type":"command","isDefault":True,"name":"Default"}]
                if path=="/core/commands/search":
                    return {"total":len(self.items),"items":self.items}
                raise AssertionError("not a readonly request")
        with patch("builtins.print"):
            _,new=mod.plan(Fake([]))
        self.assertEqual(len(new),len(mod.COMMANDS))
        existing=[{"id":9,"type":"command","groupID":3,
                   "name":mod.COMMANDS[0][0],"command":mod.COMMANDS[0][1]}]
        with patch("builtins.print"):
            _,new=mod.plan(Fake(existing))
        self.assertEqual(len(new),len(mod.COMMANDS)-1)
        existing[0]["command"]="untrusted changed"
        with self.assertRaisesRegex(mod.ShortcutError,"EXISTING_NAMED_SHORTCUT_DIFFERENT"):
            with patch("builtins.print"):
                mod.plan(Fake(existing))


if __name__=="__main__":
    unittest.main()
