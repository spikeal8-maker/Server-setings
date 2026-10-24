"""Contract for the operator browser smoke against supported ASA Lab auth forms."""

from pathlib import Path
import unittest


RUNNER = (Path(__file__).resolve().parents[1] / "scripts" / "asa_ops" / "browser_acceptance.py").read_text(encoding="utf-8")


class BrowserAcceptanceSelectorsTest(unittest.TestCase):
    def test_registration_uses_stable_dom_ids(self):
        for control in ("#register-email", "#register-username", "#register-name",
                        "#register-birth-date", "#register-password"):
            self.assertIn(control, RUNNER)
        self.assertIn("if legacy_username.count():", RUNNER)
        self.assertNotIn("get_by_label('Имя пользователя',exact=True).fill", RUNNER)

    def test_login_does_not_depend_on_visible_label(self):
        self.assertIn("page.locator('#identifier').fill(fixture['email'])", RUNNER)
        self.assertIn("page.locator('#password').fill(fixture['password'])", RUNNER)
        self.assertNotIn("get_by_label('Email или имя пользователя',exact=True)", RUNNER)

    def test_new_create_account_navigation(self):
        self.assertIn("page.get_by_role('button', name='Создать', exact=True).click()", RUNNER)


if __name__ == "__main__":
    unittest.main()
