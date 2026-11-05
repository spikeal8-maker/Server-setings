"""Source-only telemetry tests: execute real producer fragments, never a browser."""

import ast
import contextlib
import copy
import io
import json
from pathlib import Path
import re
import unittest
import uuid
from types import SimpleNamespace
from urllib.parse import urlsplit


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


    def test_exact_identity_marker_preserves_guard_failure_and_exit_two(self):
        marker = "SYNTHETIC_LOGIN_IDENTITY_MISMATCH"
        self.assertEqual(self.classify(RuntimeError(marker)), marker)
        report = self.failed_report(RuntimeError(marker), "teacher_login_submit")
        self.assertEqual(report["error"]["code"], marker)
        self.assertFalse(any(report["checks"].values()))

    def test_identity_near_matches_and_secrets_keep_class_fallback(self):
        marker = "SYNTHETIC_LOGIN_IDENTITY_MISMATCH"
        messages = (marker + " ", marker + "\n", marker.lower(),
                    marker + SECRET, SECRET + marker, marker + "_EXTRA")
        for message in messages:
            with self.subTest(case=messages.index(message)):
                self.assertEqual(self.classify(RuntimeError(message)), "RuntimeError")
                report = self.failed_report(RuntimeError(message), "teacher_login_submit")
                self.assertEqual(report["error"]["code"], "RuntimeError")


def transport_fragments(page, context, origin="http://127.0.0.1:4610", stage="release_provenance"):
    """Execute the actual Python adapter and dispatcher, without importing Playwright."""
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    constant = next(
        node for node in tree.body
        if isinstance(node, ast.Assign) and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "_BROWSER_API_FETCH"
    )
    adapter = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_browser_api")
    main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main")
    dispatcher = next(node for node in ast.walk(main) if isinstance(node, ast.FunctionDef) and node.name == "api")
    program = ast.fix_missing_locations(ast.Module(
        body=copy.deepcopy([constant, adapter, dispatcher]), type_ignores=[],
    ))
    namespace = {
        "json": json, "re": re, "uuid": uuid, "urlsplit": urlsplit,
        "page": page, "context": context, "origin": origin, "stage": stage,
    }
    exec(compile(program, str(SOURCE), "exec"), namespace)
    return namespace


class RecordingPage:
    def __init__(self, url="http://127.0.0.1:4610/#/home"):
        self.url = url
        self.calls = []
        self.response = {"status": 200, "data": {"synthetic": True}}

    def evaluate(self, expression, argument):
        self.calls.append((expression, argument))
        return self.response


class RecordingRequest:
    def __init__(self):
        self.calls = []
        self.status = 200
        self.data = {"deployment": {"revision": "a" * 40}}

    def fetch(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self

    def json(self):
        return self.data


class BrowserAcceptanceTransportTests(unittest.TestCase):
    def setUp(self):
        self.page = RecordingPage()
        self.request = RecordingRequest()
        self.namespace = transport_fragments(self.page, SimpleNamespace(request=self.request))
        self.adapter = self.namespace["_browser_api"]
        self.api = self.namespace["api"]
        self.origin = "http://127.0.0.1:4610"

    def test_authenticated_get_uses_page_and_preserves_response_and_idempotency(self):
        self.assertEqual(self.api("GET", "/api/auth/me"), {"synthetic": True})
        self.assertEqual(self.request.calls, [])
        expression, argument = self.page.calls[0]
        self.assertEqual(expression, self.namespace["_BROWSER_API_FETCH"])
        self.assertEqual(set(argument), {
            "origin", "method", "path", "body", "contentType", "allowed", "idempotencyKey",
        })
        self.assertEqual((argument["origin"], argument["method"], argument["path"]),
                         (self.origin, "GET", "/api/auth/me"))
        self.assertIsNone(argument["body"])
        self.assertIsNone(argument["contentType"])
        self.assertEqual(argument["allowed"], [200, 201])
        self.assertEqual(uuid.UUID(argument["idempotencyKey"]).version, 4)

    def test_authenticated_json_and_literal_bodies_preserve_request_contract(self):
        for body, content_type in (({"document": {"title": "synthetic", "values": [1, True, None]}},
                                    "application/json"), ("literal synthetic", "application/octet-stream")):
            with self.subTest(content_type=content_type):
                self.page.response = {"status": 201, "data": {"id": "fixture-only"}}
                self.assertEqual(self.api("POST", "/api/projects", body), {"id": "fixture-only"})
                argument = self.page.calls[-1][1]
                self.assertEqual(argument["contentType"], content_type)
                self.assertEqual(json.loads(argument["body"]) if isinstance(body, dict) else argument["body"], body)
        self.assertEqual(self.request.calls, [])

    def test_pre_navigation_health_keeps_separate_unauthenticated_transport(self):
        self.page.url = "about:blank"
        self.assertEqual(self.api("GET", "/health/ready"), self.request.data)
        self.assertEqual(self.page.calls, [])
        self.assertEqual(len(self.request.calls), 1)
        url, kwargs = self.request.calls[0]
        self.assertEqual(url, self.origin + "/health/ready")
        self.assertEqual(set(kwargs), {"method", "data", "timeout", "headers"})
        self.assertEqual((kwargs["method"], kwargs["data"], kwargs["timeout"]), ("GET", None, 30000))
        self.assertEqual(set(kwargs["headers"]), {"Origin", "Idempotency-Key"})
        self.assertEqual(kwargs["headers"]["Origin"], self.origin)
        self.assertEqual(uuid.UUID(kwargs["headers"]["Idempotency-Key"]).version, 4)

    def test_health_cannot_be_reused_after_navigation_or_for_a_payload(self):
        cases = (
            ("GET", None, "teacher_login_submit", "about:blank"),
            ("GET", None, "release_provenance", self.origin + "/"),
            ("POST", None, "release_provenance", "about:blank"),
            ("GET", {"synthetic": True}, "release_provenance", "about:blank"),
        )
        for method, body, stage, url in cases:
            with self.subTest(method=method, stage=stage, url=url):
                self.page.url = url
                self.namespace["stage"] = stage
                with self.assertRaisesRegex(RuntimeError, "^BROWSER_HEALTH_REQUEST$"):
                    self.api(method, "/health/ready", body)
        self.assertEqual(self.request.calls, [])
        self.assertEqual(self.page.calls, [])

    def test_foreign_or_unnavigated_page_fails_before_serializing_payload(self):
        urls = (
            "about:blank", "https://127.0.0.1:4610/", "http://127.0.0.1:4611/",
            "http://localhost:4610/", "https://other.example.invalid/",
            "http://synthetic@127.0.0.1:4610/",
        )
        for url in urls:
            with self.subTest(url=url):
                self.page.url = url
                with self.assertRaisesRegex(RuntimeError, "^BROWSER_API_ORIGIN$"):
                    self.api("POST", "/api/projects", object())
        self.assertEqual(self.page.calls, [])
        self.assertEqual(self.request.calls, [])

    def test_malformed_origin_and_unsafe_paths_are_rejected_before_evaluate(self):
        for origin in (None, self.origin + "/", self.origin + "#fragment",
                       "http://user@127.0.0.1:4610", "http://127.0.0.1:wrong",
                       "\n" + self.origin, "http://127.0.0.1\\:4610"):
            with self.subTest(origin_kind=type(origin).__name__):
                with self.assertRaisesRegex(RuntimeError, "^BROWSER_API_ORIGIN$"):
                    self.adapter(self.page, origin, "POST", "/api/projects", object())
        paths = ("https://other.example.invalid/api/x", "//other.example.invalid/api/x",
                 "/health/ready", "/api/\\other", "/api/x\n", "/api/x\x00", "/api/x ", None)
        for path in paths:
            with self.subTest(path_kind=type(path).__name__):
                with self.assertRaisesRegex(RuntimeError, "^BROWSER_API_REQUEST$"):
                    self.adapter(self.page, self.origin, "POST", path, object())
        with self.assertRaisesRegex(RuntimeError, "^BROWSER_API_REQUEST$"):
            self.api("GET", "/api/projects", {"payload": True})
        with self.assertRaisesRegex(RuntimeError, "^BROWSER_API_REQUEST$"):
            self.api("DELETE", "/api/projects")
        self.assertEqual(self.page.calls, [])
        self.assertEqual(self.request.calls, [])

    def test_http_error_is_closed_and_does_not_switch_transport(self):
        self.page.response = {"status": 403, "data": {"private": SECRET}}
        with self.assertRaisesRegex(RuntimeError, "^API_STATUS_403$") as caught:
            self.api("GET", "/api/auth/me")
        classify, report_failure = producer_fragments()
        self.assertEqual(classify(caught.exception), "API_STATUS_403")
        with contextlib.redirect_stdout(io.StringIO()) as output:
            exit_code = report_failure(caught.exception, "teacher_login_submit", {})
        self.assertEqual(exit_code, 2)
        self.assertEqual(json.loads(output.getvalue())["error"]["code"], "API_STATUS_403")
        self.assertNotIn(SECRET, output.getvalue())
        self.assertEqual(self.request.calls, [])

    def test_response_envelope_requires_integer_status_and_exact_fields(self):
        responses = (
            None, [], {"status": True, "data": None}, {"status": "200", "data": None},
            {"status": 99, "data": None}, {"status": 600, "data": None},
            {"status": 200}, {"status": 200, "data": {}, "extra": SECRET},
        )
        for response in responses:
            with self.subTest(case=responses.index(response)):
                self.page.response = response
                with self.assertRaisesRegex(RuntimeError, "^BROWSER_API_RESPONSE$") as caught:
                    self.api("GET", "/api/auth/me")
                self.assertNotIn(SECRET, str(caught.exception))
        self.assertEqual(self.request.calls, [])

    def test_reopen_uses_its_existing_page_and_explicit_allowed_status(self):
        reopened = RecordingPage(self.origin + "/#/home/fixture-only")
        reopened.response = {"status": 200, "data": {"draft": {"revision": 3}}}
        self.assertEqual(
            self.adapter(reopened, self.origin, "GET", "/api/projects/fixture-only", allowed=(200,)),
            {"draft": {"revision": 3}},
        )
        self.assertEqual(reopened.calls[0][1]["allowed"], [200])
        self.assertEqual(self.page.calls, [])
        reopened.response = {"status": 201, "data": {"private": SECRET}}
        with self.assertRaisesRegex(RuntimeError, "^API_STATUS_201$"):
            self.adapter(reopened, self.origin, "GET", "/api/projects/fixture-only", allowed=(200,))

    def test_real_call_sites_preserve_reopen_cookie_flow_and_unauthenticated_routes(self):
        source = SOURCE.read_text(encoding="utf-8")
        tree = ast.parse(source)
        main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main")
        reread = next(
            node for node in ast.walk(main)
            if isinstance(node, ast.Assign) and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "reread"
        ).value
        self.assertIsInstance(reread, ast.Call)
        self.assertEqual(ast.unparse(reread),
                         "_browser_api(reopened, origin, 'GET', '/api/projects/' + project, allowed=(200,))")
        self.assertIn("fresh.add_cookies(context.cookies())  # no localStorage/recovery cache", source)
        self.assertIn("learner.request.post(origin+'/api/class-join/studentseat',", source)
        self.assertEqual(source.count("context.request.fetch("), 1)
        self.assertNotIn("fresh.request.get(", source)



if __name__ == "__main__":
    unittest.main()
