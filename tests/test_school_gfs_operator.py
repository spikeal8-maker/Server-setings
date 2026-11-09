"""VM100 GFS backup manual commands and audit receipts: no live cloud calls."""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import subprocess

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))


def load(alias, filename):
    spec = importlib.util.spec_from_file_location(alias, SCRIPTS / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gfs = load("school_gfs_for_operator_tests", "school-gfs-backup.py")
cli = load("school_gfs_cli_for_tests", "asa-school-gfs-cli.py")
installer = load("school_gfs_cli_installer_for_tests", "install-school-gfs-cli.py")


class GfsAuditTests(unittest.TestCase):
    def test_success_and_failure_append_only_redacted_events(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            with patch.object(gfs, "RECEIPTS", root):
                gfs.record_attempt("hourly", "manual", "success")
                gfs.record_attempt("full", "scheduled", "failure",
                                   code="YANDEX_NETWORK_TIMEOUT")
                path = root / "events.jsonl"
                data = [json.loads(x) for x in path.read_text().splitlines()]
                self.assertEqual([x["outcome"] for x in data], ["success", "failure"])
                self.assertEqual(data[-1]["code"], "YANDEX_NETWORK_TIMEOUT")
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                with patch.object(cli, "RECEIPTS", root):
                    n, found = cli.recent_failures(24)
                    self.assertEqual(n, 1)
                    self.assertEqual(found[-1]["kind"], "full")
                self.assertNotIn("PRIVATE", path.read_text())

    def test_audit_never_records_arbitrary_error_message(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            with patch.object(gfs, "RECEIPTS", root):
                with self.assertRaisesRegex(gfs.GfsError, "AUDIT_CODE_INVALID"):
                    gfs.record_attempt("full", "scheduled", "failure",
                                       code="token=secret 12345")
                self.assertFalse((root / "events.jsonl").exists())

    def test_audit_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.chmod(0o700)
            outside = root.parent / "gfs-untrusted-log"
            try:
                (root / "events.jsonl").symlink_to(outside)
                with patch.object(gfs, "RECEIPTS", root):
                    with self.assertRaises(OSError):
                        gfs.record_attempt("hourly", "manual", "success")
                self.assertFalse(outside.exists())
            finally:
                if outside.exists():
                    outside.unlink(missing_ok=True)

    def test_empty_audit_history_is_not_false_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(cli, "RECEIPTS", Path(directory)):
                self.assertEqual(cli.recent_events(), [])
                self.assertEqual(cli.recent_failures(24), (0, []))


class GfsCommandTests(unittest.TestCase):
    def test_manual_hourly_and_full_use_only_installed_pinned_runner(self):
        with patch.object(cli, "current_runner", return_value=("a" * 40, Path("/opt/asa-school-gfs/releases/"+"a"*40+"/school-gfs-backup.py"))), \
             patch.object(cli.subprocess, "run",
                          return_value=subprocess.CompletedProcess(args=[], returncode=0)) as process:
            self.assertEqual(cli.manual("hourly"), 0)
            self.assertEqual(cli.manual("full"), 0)
            args = [x.args[0] for x in process.call_args_list]
            self.assertEqual(args[0][-4:], ["--kind", "hourly", "--apply", "--manual"])
            self.assertEqual(args[1][-4:], ["--kind", "full", "--apply", "--manual"])

    def test_both_scheduled_services_must_match_exact_sha(self):
        sha="a"*40
        def fake(unit):
            return {"LoadState": "loaded",
                    "ExecStart": "/usr/bin/python3 -B /opt/asa-school-gfs/releases/" + (
                        "b"*40 if "full" in unit else sha) +
                        "/school-gfs-backup.py --apply"}
        with patch.object(cli, "unit_properties", side_effect=fake):
            with self.assertRaisesRegex(cli.OperatorError,
                                        "GFS_SERVICES_ON_DIFFERENT_RELEASES"):
                cli.current_runner()

    def test_status_uses_receipt_and_explicitly_marks_seed(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)
            receipts=base/"receipts"
            receipts.mkdir()
            (receipts/"latest-hourly.json").write_text(json.dumps({
                "createdAt":"2026-11-09T13:03:43+00:00",
                "archive":"asa-school-01-20261109T130327Z-c09b6413.tar.age",
                "remoteFullReadbackSha256": True,
                "remote":{"hourly-db":{"bytes":48897880}}
            }),encoding="utf-8")
            seed=base/"seed.json"
            seed.write_text(json.dumps({
                "createdAt":"2026-11-09T12:40:00+00:00",
                "sourceArchive":"asa-school-01-20261109T002038Z-024601f2.tar.age",
                "verification":"full-remote-download-sha256"
            }),encoding="utf-8")
            def fake_unit(unit):
                return {"UnitFileState":"enabled","ActiveState":"active",
                        "NextElapseUSecRealtime":"Mon 2026-11-09 13:45:00 UTC"}
            with patch.object(cli, "RECEIPTS", receipts), \
                 patch.object(cli, "LEGACY_SEED", seed), \
                 patch.object(cli, "unit_properties", side_effect=fake_unit), \
                 patch.object(cli, "current_runner", return_value=("a"*40,base/"runner")):
                data=cli.status_report()
            self.assertEqual(data["hourly"]["remote"],["hourly-db"])
            self.assertIsNone(data["full"]["lastVerified"])
            self.assertTrue(data["initialSeed"]["verified"])
            self.assertTrue(data["hourly"]["timerActive"])

    def test_journal_error_filter_never_prints_arbitrary_messages(self):
        raw=[{"MESSAGE":"GFS_BLOCKED=BACKUP_COMMAND_FAILED",
              "__REALTIME_TIMESTAMP":"1794229200000000",
              "_SYSTEMD_UNIT":"asa-school-gfs-hourly.service"},
             {"MESSAGE":"Authorization: OAuth token=SECRET",
              "_SYSTEMD_UNIT":"asa-school-gfs-full.service"}]
        with patch.object(cli, "run_readonly", return_value="\n".join(map(json.dumps, raw))):
            items=cli.read_journal_failures(24)
        self.assertEqual(len(items),1)
        self.assertEqual(items[0]["code"],"BACKUP_COMMAND_FAILED")
        self.assertNotIn("SECRET",str(items))

    def test_install_idempotent_and_refuses_foreign_command(self):
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory)
            root=base/"releases"/("a"*40)
            root.mkdir(parents=True)
            (root/"asa-school-gfs-cli.py").write_text("#!/usr/bin/env python3\n",
                                                      encoding="utf-8")
            link=base/"asa-school-gfs"
            cli_file=root/"asa-school-gfs-cli.py"
            self.assertTrue(installer.is_managed_link(cli_file, root.parent))
            self.assertFalse(installer.is_managed_link(
                base/"foreign"/"asa-school-gfs-cli.py", root.parent))
            with patch.object(installer, "SOURCE", root), \
                 patch.object(installer, "DESTINATION", link), \
                 patch.object(installer, "preflight",return_value=cli_file):
                installer.apply()
                self.assertEqual(link.resolve(),cli_file)
                installer.apply()
                link.unlink()
                link.write_text("owner script",encoding="utf-8")
                self.assertEqual(link.read_text(),"owner script")


if __name__ == "__main__":
    unittest.main()
