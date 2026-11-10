import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
SPEC = importlib.util.spec_from_file_location("school_operator", SCRIPTS / "asa-school-operator.py")
operator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(operator)
PERMIT = dict(fromRevision="a" * 40, toRevision="b" * 40, toolsRevision="c" * 40,
              deploymentDigest="sha256:" + "d" * 64)


class OperatorTests(unittest.TestCase):
    def test_install_without_admission_never_calls_manager(self):
        with patch.object(operator, "approval", return_value=None), patch.object(operator, "run_manager") as run:
            with self.assertRaisesRegex(operator.Blocked, "нет допущенного"):
                operator.install_update()
            run.assert_not_called()

    def test_check_reports_published_but_not_admitted(self):
        with patch.object(operator, "approval", return_value=None), patch.object(operator, "probe",
                side_effect=[dict(revision="a" * 40, schema=308), dict(revision="b" * 40, schema=312)]), \
                patch("builtins.print") as output, patch.object(operator, "run_manager") as run:
            self.assertEqual(operator.check_update(), 0)
            self.assertIn("Опубликован выпуск", output.call_args_list[1].args[0])
            self.assertIn("нет допущенного", output.call_args.args[0])
            run.assert_not_called()

    def test_confirmation_and_release_drift_block_update(self):
        running, target = dict(revision=PERMIT["fromRevision"]), dict(revision=PERMIT["toRevision"])
        for answer, after in [(False, PERMIT), (True, None)]:
            with self.subTest(answer=answer), patch.object(operator, "approval", side_effect=[PERMIT, after]), \
                    patch.object(operator, "probe", side_effect=[running, target]), \
                    patch.object(operator, "manager", return_value=["manager"]), \
                    patch.object(operator, "run_manager", return_value=0) as run, \
                    patch.object(operator, "fresh_backup"), patch.object(operator, "confirm", return_value=answer):
                if answer:
                    with self.assertRaisesRegex(operator.Blocked, "изменились"):
                        operator.install_update()
                else:
                    self.assertEqual(operator.install_update(), 0)
                run.assert_called_once_with(["manager"], "check", "--require-application-rollback",
                                            "--deployment-digest", PERMIT["deploymentDigest"])

    def test_full_backup_requires_confirmation(self):
        with patch.object(operator, "confirm", return_value=False), patch.object(operator.subprocess, "run") as run:
            self.assertEqual(operator.backup_full(), 0)
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
