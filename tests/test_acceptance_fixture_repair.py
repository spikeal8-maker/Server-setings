"""Offline regression tests: no real database, browser, or school server access."""
from concurrent.futures import ThreadPoolExecutor
import contextlib
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import stat
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from asa_ops import acceptance_repair as repair
from asa_ops.acceptance_fixture import (
    FixtureError, archive_fixture, bound_path, fresh_fixture,
    locked_fixture, read_fixture, save_fixture,
)

ORIGIN = "https://test.asa-lab.ru"
GEN = "a" * 32
INSTALL = "asa-test"


class PrivateFixtureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = bound_path(self.root / "acceptance.json", GEN)

    def write_legacy(self, **changes):
        original = fresh_fixture(ORIGIN, GEN, INSTALL)
        original.pop("generation")
        original.pop("installation_id")
        original["registered"] = True
        original.update(changes)
        raw = json.dumps(original).encode()
        with open(self.path, "wb") as fp:
            fp.write(raw)
        self.path.chmod(0o600)
        return original

    def test_legacy_synthetic_fixture_is_bound_by_filename(self):
        old = self.write_legacy()
        loaded, digest, legacy = read_fixture(self.path, ORIGIN, GEN, INSTALL)
        self.assertTrue(legacy)
        self.assertEqual(loaded, old)
        self.assertEqual(len(digest), 64)

    def test_foreign_generation_or_origin_fails_closed(self):
        fresh = fresh_fixture(ORIGIN, GEN, INSTALL)
        save_fixture(self.path, fresh, ORIGIN, GEN, INSTALL)
        for origin, generation, installation in (
            ("https://elsewhere.example", GEN, INSTALL),
            (ORIGIN, "b" * 32, INSTALL),
            (ORIGIN, GEN, "foreign"),
        ):
            with self.subTest(origin=origin, generation=generation, installation=installation):
                with self.assertRaises(FixtureError):
                    read_fixture(self.path, origin, generation, installation)

    def test_malformed_foreign_or_fake_identity_rejected(self):
        for contents in (b"{", b"{}", b"[]", b"null", b'{"email":"teacher@school.ru"}'):
            self.path.write_bytes(contents)
            self.path.chmod(0o600)
            with self.assertRaises(FixtureError):
                read_fixture(self.path, ORIGIN, GEN, INSTALL)

    def test_symlink_and_excess_permissions_rejected(self):
        target = self.root / "target.json"
        target.write_text("{}")
        self.path.symlink_to(target)
        with self.assertRaises(FixtureError):
            read_fixture(self.path, ORIGIN, GEN, INSTALL)
        self.path.unlink()
        self.write_legacy()
        self.path.chmod(0o644)
        with self.assertRaises(FixtureError):
            read_fixture(self.path, ORIGIN, GEN, INSTALL)

    def test_private_archive_and_atomic_writes(self):
        self.write_legacy()
        _, sha, _ = read_fixture(self.path, ORIGIN, GEN, INSTALL)
        archive = archive_fixture(self.path, sha)
        self.assertEqual(stat.S_IMODE(archive.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(archive.parent.stat().st_mode), 0o700)
        candidate = fresh_fixture(ORIGIN, GEN, INSTALL)
        save_fixture(self.path, candidate, ORIGIN, GEN, INSTALL, expected_sha=sha)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        with self.assertRaises(FixtureError):
            save_fixture(self.path, candidate, ORIGIN, GEN, INSTALL, expected_sha=sha)

    def test_parallel_lock_rejected(self):
        with locked_fixture(self.path):
            with ThreadPoolExecutor(max_workers=1) as pool:
                result = pool.submit(self.try_lock).result(timeout=3)
        self.assertEqual(result, "FIXTURE_BUSY")

    def try_lock(self):
        try:
            with locked_fixture(self.path):
                return "unexpected_lock"
        except FixtureError as error:
            return str(error)


class RepairTests(PrivateFixtureTests):
    def setUp(self):
        super().setUp()
        self.cfg = {"id": INSTALL, "entry_origin": ORIGIN, "state_root": str(self.root)}

    @contextlib.contextmanager
    def guards(self, matches):
        with (
            patch.object(repair, "context", return_value=(self.cfg, self.root, GEN, self.path)),
            patch.object(repair, "assert_no_workers") as workers,
            patch.object(repair, "postgres_matches", side_effect=matches) as db,
        ):
            yield workers, db

    def test_stale_registered_missing_in_db_repairs_once(self):
        self.write_legacy(projects={"blocks": "b" * 8 + "-bbbb-bbbb-bbbb-bbbbbbbbbbbb"},
                          classroom="c" * 8 + "-cccc-cccc-cccc-cccccccccccc",
                          course="d" * 8 + "-dddd-dddd-dddd-dddddddddddd",
                          student_code="ABCD56", join_code="ZXCV78")
        with self.guards([0, 0, 0, 0]) as (_, db):
            plan = repair.inspect({}, "ignored")
            self.assertEqual(plan["status"], "eligible")
            result = repair.apply({}, "ignored", plan["fixture_sha256"])
            self.assertEqual(result["status"], "pending_registration")
            db.assert_called()
        value, _, legacy = read_fixture(self.path, ORIGIN, GEN, INSTALL)
        self.assertFalse(legacy)
        self.assertFalse(value["registered"])
        self.assertEqual(value["projects"], {})
        for key in ("student_code", "join_code", "course", "classroom"):
            self.assertNotIn(key, value)
        self.assertNotEqual(value["password"], self.original_password_or_empty())
        with self.guards([]):
            repeated = repair.apply({}, "ignored", plan["fixture_sha256"])
        self.assertEqual(repeated["status"], "already_repaired")
        self.assertEqual(repeated["operation_id"], result["operation_id"])
        archives = list((self.root / ".fixture-audit").glob("fixture-*.json"))
        self.assertEqual(len(archives), 1)
        ledger = (self.root / ".fixture-audit" / "events.jsonl").read_text()
        self.assertNotIn(value["email"], ledger)
        self.assertNotIn(value["password"], ledger)

    def original_password_or_empty(self):
        archive_root = self.root / ".fixture-audit"
        archives = list(archive_root.glob("fixture-*.json"))
        return json.loads(archives[0].read_text())["password"] if archives else ""

    def test_registered_account_present_but_401_blocks_repair(self):
        old = self.write_legacy()
        data = self.path.read_bytes()
        with self.guards([1]):
            plan = repair.inspect({}, "ignored")
            self.assertEqual(plan["code"], "ACCOUNT_PRESENT_INVESTIGATE_401")
        with self.guards([1]):
            with self.assertRaisesRegex(FixtureError, "ACCOUNT_PRESENT_INVESTIGATE_401"):
                repair.apply({}, "ignored", "0" * 64 if False else self.sha())
        self.assertEqual(self.path.read_bytes(), data)
        self.assertEqual(old["email"], json.loads(data)["email"])
        self.assertFalse((self.root / ".fixture-audit").exists())

    def sha(self):
        import hashlib
        return hashlib.sha256(self.path.read_bytes()).hexdigest()

    def test_active_worker_blocks_before_db_or_write(self):
        self.write_legacy()
        data = self.path.read_bytes()
        with self.guards([]) as (workers, db):
            workers.side_effect = FixtureError("ACTIVE_OPS_WORKER")
            with self.assertRaises(FixtureError):
                repair.apply({}, "ignored", self.sha())
            db.assert_not_called()
        self.assertEqual(self.path.read_bytes(), data)

    def test_db_probe_unknown_blocks(self):
        self.write_legacy()
        with self.guards([FixtureError("POSTGRES_PROBE_UNAVAILABLE")]):
            with self.assertRaisesRegex(FixtureError, "POSTGRES_PROBE_UNAVAILABLE"):
                repair.apply({}, "ignored", self.sha())

    def test_interrupted_after_archive_retains_original_and_retries(self):
        self.write_legacy()
        before = self.path.read_bytes()
        with self.guards([0, 0, 0]):
            with patch.object(repair, "save_fixture", side_effect=KeyboardInterrupt()):
                with self.assertRaises(KeyboardInterrupt):
                    repair.apply({}, "ignored", self.sha())
        self.assertEqual(self.path.read_bytes(), before)
        with self.guards([0, 0, 0]):
            outcome = repair.apply({}, "ignored", self.sha())
        self.assertEqual(outcome["status"], "pending_registration")
        self.assertEqual(len(list((self.root / ".fixture-audit").glob("fixture-*.json"))), 1)

    def test_interrupted_after_swap_resumes_without_regenerating(self):
        self.write_legacy()
        old_sha = self.sha()
        real_audit = repair.audit_event
        calls = []
        def interrupted_audit(*args):
            calls.append(args[1])
            if args[1] == "pending_registration":
                raise KeyboardInterrupt()
            return real_audit(*args)
        with self.guards([0, 0, 0]):
            with patch.object(repair, "audit_event", side_effect=interrupted_audit):
                with self.assertRaises(KeyboardInterrupt):
                    repair.apply({}, "ignored", old_sha)
        fixture_before_retry = self.path.read_bytes()
        with self.guards([]):
            self.assertEqual(repair.apply({}, "ignored", old_sha)["status"], "already_repaired")
        self.assertEqual(self.path.read_bytes(), fixture_before_retry)

    def test_existing_healthy_fixture_remains_usable_and_unchanged(self):
        value = fresh_fixture(ORIGIN, GEN, INSTALL)
        value["registered"] = True
        save_fixture(self.path, value, ORIGIN, GEN, INSTALL)
        before = self.path.read_bytes()
        with self.guards([1]):
            self.assertEqual(repair.inspect({}, "ignored")["code"], "ACCOUNT_PRESENT_INVESTIGATE_401")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertTrue(read_fixture(self.path, ORIGIN, GEN, INSTALL)[0]["registered"])

    def test_no_secret_echo_in_plan_or_apply_errors(self):
        self.write_legacy()
        raw = json.loads(self.path.read_text())
        with self.guards([FixtureError("POSTGRES_PROBE_UNAVAILABLE")]) as (_, db):
            buf = io.StringIO()
            with patch.object(Path, "read_text", return_value=json.dumps({"installation": self.cfg})):
                with patch("sys.stdout", buf):
                    code = repair.main(["--config", "/etc/server-settings/asa-operations.json", "--plan"])
            db.assert_called_once()
        self.assertEqual(code, 2)
        self.assertNotIn(raw["email"], buf.getvalue())
        self.assertNotIn(raw["password"], buf.getvalue())

    def test_direct_db_probe_is_read_only_and_requires_superuser(self):
        synthetic = fresh_fixture(ORIGIN, GEN, INSTALL)
        installation = SimpleNamespace(compose_args=["docker", "compose", "-p", "asa-test"])
        completed = SimpleNamespace(returncode=0, stdout="1|0|0|0\n", stderr="")
        with patch("asa_ops.adapter.load", return_value=(None, installation)):
            with patch.object(repair.subprocess, "run", return_value=completed) as runner:
                self.assertEqual(repair.postgres_matches({"root": str(self.root)}, synthetic["email"], synthetic["username"]), 0)
                command = runner.call_args.args[0]
                sql = runner.call_args.kwargs["input"]
                self.assertNotIn(synthetic["email"], repr(command))
                self.assertIn("BEGIN TRANSACTION READ ONLY", sql)
                self.assertNotIn("INSERT INTO", sql)
                self.assertIn("-T", command)
            with patch.object(repair.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout="0|0|0|0\n")):
                with self.assertRaisesRegex(FixtureError, "POSTGRES_AUTHORITY_UNVERIFIED"):
                    repair.postgres_matches({"root": str(self.root)}, synthetic["email"], synthetic["username"])


class BrowserContractTests(unittest.TestCase):
    def test_real_ui_and_login_identity_guard(self):
        runner = (Path(__file__).resolve().parents[1] / "scripts/asa_ops/browser_acceptance.py").read_text()
        for expected in (
            "page.locator('#register-email')",
            "page.locator('#register-name')",
            "page.locator('#register-birth-date')",
            "page.locator('#register-password')",
            "page.locator('#identifier')",
            "page.locator('#password')",
            "SYNTHETIC_LOGIN_IDENTITY_MISMATCH",
            "FOREIGN_TEST_PROJECT",
            "with locked_fixture(fixture_path):",
            "if pending.value.status not in (200, 201, 409):",
        ):
            self.assertIn(expected, runner)
        self.assertNotIn("checks = {k: True", runner)
        self.assertNotIn("ignore_https_errors=True", runner)


if __name__ == "__main__":
    unittest.main()
