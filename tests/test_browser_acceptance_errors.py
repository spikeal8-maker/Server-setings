"""Source-only telemetry tests: execute real producer fragments, never a browser."""

import ast
import contextlib
import copy
import io
import json
from pathlib import Path
import re
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "scripts/asa_ops/browser_acceptance.py"
SECRET = "SOURCE_ONLY_PRIVATE_SENTINEL_22"
CODES = ("SAVE_NOT_OBSERVED", "REOPEN_VALUE_MISMATCH", "REOPEN_DRAFT_MISMATCH")


class NoBrowser:
    def locator(self, selector):
        # The unchanged producer tolerates an unavailable failure screenshot.
        # This boundary never represents a browser or a successful UI check.
        raise RuntimeError("SOURCE_ONLY_NO_BROWSER")


def producer_fragments():
    """Compile the real helper, except handler and report/exit tail in isolation.

    The UI body is replaced only in this test program by a supplied exception.
    Nothing is patched or traced in the imported product or a browser runtime.
    The result proves error reporting and exit policy, not browser acceptance.
    """
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    helper = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_error_code")
    main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main")
    handlers = [
        node for node in ast.walk(main)
        if isinstance(node, ast.ExceptHandler)
        and isinstance(node.type, ast.Name) and node.type.id == "Exception"
        and node.name == "exc"
    ]
    if len(handlers) != 1:
        raise AssertionError("Expected the one real acceptance report handler")
    assignments = {
        node.targets[0].id: node
        for node in main.body
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
    }
    tail = main.body[-2:]
    if (not isinstance(tail[0], ast.Expr)
            or not isinstance(tail[0].value, ast.Call)
            or not isinstance(tail[0].value.func, ast.Name)
            or tail[0].value.func.id != "print"
            or not isinstance(tail[1], ast.Return)):
        raise AssertionError("Expected the real printed report and exit policy")
    driver = ast.parse(
        "def report_failure(injected_error, stage, prior_checks):\n"
        "    checks.update(prior_checks)\n"
        "    failures = []\n"
        "    try:\n"
        "        raise injected_error\n"
        "    except Exception as exc:\n"
        "        pass\n"
    ).body[0]
    update, failures, guarded = driver.body
    guarded.handlers = copy.deepcopy(handlers)
    driver.body = [
        copy.deepcopy(assignments["checks"]), update,
        copy.deepcopy(assignments["report"]), failures, guarded, *copy.deepcopy(tail),
    ]
    program = ast.fix_missing_locations(ast.Module(body=[copy.deepcopy(helper), driver], type_ignores=[]))
    namespace = {
        "re": re, "json": json, "revision": "a" * 40,
        "origin": "https://acceptance.example.invalid", "page": NoBrowser(),
    }
    exec(compile(program, str(SOURCE), "exec"), namespace)
    return namespace["_error_code"], namespace["report_failure"]


class BrowserAcceptanceErrorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        classify, report = producer_fragments()
        cls.classify = staticmethod(classify)
        cls.report = staticmethod(report)

    def failed_report(self, error, stage="blocks_save", prior_checks=None):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = self.report(error, stage, prior_checks or {})
        raw = output.getvalue()
        report = json.loads(raw)
        self.assertEqual(code, 2)
        self.assertEqual(set(report), {"revision", "entry_origin", "checks", "error", "http_failures"})
        self.assertEqual(report["http_failures"], [])
        self.assertEqual(set(report["error"]), {"stage", "code"})
        self.assertEqual(report["error"]["stage"], stage)
        self.assertNotIn(SECRET, raw)
        self.assertNotIn("SOURCE_ONLY_NO_BROWSER", raw)
        self.assertFalse(all(report["checks"].values()))
        return report

    def test_three_fixed_save_reopen_codes_are_preserved(self):
        for code in CODES:
            with self.subTest(code=code):
                self.assertEqual(self.classify(RuntimeError(code)), code)

    def test_existing_api_status_rule_is_preserved(self):
        for status in ("000", "200", "403", "500", "999"):
            for error_type in (RuntimeError, ValueError):
                code = "API_STATUS_" + status
                with self.subTest(code=code, error_type=error_type.__name__):
                    self.assertEqual(self.classify(error_type(code)), code)

    def test_near_matches_and_secret_messages_keep_class_fallback(self):
        messages = (
            SECRET, "REVISION_DRIFT", "save_not_observed",
            "SAVE_NOT_OBSERVED " + SECRET, SECRET + "REOPEN_VALUE_MISMATCH",
            "REOPEN_DRAFT_MISMATCH\n" + SECRET, "REOPEN_VALUE_MISMATCH_EXTRA",
            "API_STATUS_403 " + SECRET, "API_STATUS_403\n", "API_STATUS_40",
            "API_STATUS_0403", "API_STATUS_٤٠٣", "",
        )
        for message in messages:
            with self.subTest(message_kind=messages.index(message)):
                self.assertEqual(self.classify(RuntimeError(message)), "RuntimeError")

    def test_other_exception_types_keep_existing_name_fallback(self):
        errors = (ValueError(SECRET), KeyError(SECRET), OSError(SECRET), TimeoutError(SECRET),
                  RuntimeError("SAVE_NOT_OBSERVED", SECRET))
        for error in errors:
            with self.subTest(error_type=type(error).__name__):
                self.assertEqual(self.classify(error), type(error).__name__)

    def test_real_producer_reports_each_fixed_code_and_keeps_exit_two(self):
        for code in CODES:
            with self.subTest(code=code):
                report = self.failed_report(RuntimeError(code))
                self.assertEqual(report["error"]["code"], code)
                self.assertFalse(any(report["checks"].values()))

    def test_real_producer_keeps_api_and_secret_fallback_in_failed_reports(self):
        for error, expected in (
                (RuntimeError("API_STATUS_403"), "API_STATUS_403"),
                (RuntimeError("SAVE_NOT_OBSERVED " + SECRET), "RuntimeError"),
                (RuntimeError(SECRET), "RuntimeError"),
                (TimeoutError(SECRET), "TimeoutError")):
            with self.subTest(expected=expected):
                self.assertEqual(self.failed_report(error)["error"]["code"], expected)

    def test_failed_report_preserves_completed_and_unfinished_checks(self):
        completed = {
            "teacher_login": True, "student_login": True, "course_open": True,
            "existing_project_open": True, "scratch_save_reopen": True,
        }
        report = self.failed_report(RuntimeError("REOPEN_DRAFT_MISMATCH"), "electronics_save", completed)
        self.assertEqual(report["checks"], {**completed, "electronics_save_reopen": False})


if __name__ == "__main__":
    unittest.main()
