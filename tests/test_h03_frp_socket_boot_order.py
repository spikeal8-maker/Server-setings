"""Tests for the H03 school panel/IPMI private Tailscale socket boot-order repair."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "scripts" / "h03_frp_socket_boot_order.py"
SPEC = importlib.util.spec_from_file_location("h03_frp_socket_boot_order", str(SRC))
assert SPEC is not None and SPEC.loader is not None
script = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(script)


def fixture(name: str, after: str) -> str:
    endpoint, service = script.UNIT_ENDPOINTS[name]
    return (
        "[Unit]\n"
        "Description=H03 owned Tailscale endpoint\n"
        + after + "\n"
        "[Socket]\n"
        "ListenStream=" + endpoint + "\n"
        "FreeBind=yes\n"
        "Service=" + service + "\n"
        "NoDelay=true\n"
        "[Install]\n"
        "WantedBy=sockets.target\n"
    )


class FrpSocketBootOrderTests(unittest.TestCase):
    def test_exact_legacy_to_current(self):
        for name in script.UNIT_ENDPOINTS:
            with self.subTest(unit=name):
                previous = fixture(name, script.LEGACY_AFTER)
                result, old = script.unit_candidate(name, previous)
                self.assertTrue(old)
                self.assertNotIn(script.LEGACY_AFTER, result)
                self.assertIn(script.NEW_AFTER, result)
                self.assertEqual(result.replace(script.NEW_AFTER, script.LEGACY_AFTER), previous)
                self.assertTrue(script.unit_candidate(name, result) == (result, False))

    def test_reject_unknown_unit(self):
        with self.assertRaisesRegex(RuntimeError, "Unknown unit"):
            script.unit_candidate("ssh.socket", fixture(next(iter(script.UNIT_ENDPOINTS)), script.LEGACY_AFTER))

    def test_drift_fail_closed(self):
        for name in script.UNIT_ENDPOINTS:
            sample = fixture(name, script.LEGACY_AFTER)
            endpoint, service = script.UNIT_ENDPOINTS[name]
            changed = [
                sample.replace(endpoint, "0.0.0.0:443"),
                sample.replace("FreeBind=yes", "FreeBind=no"),
                sample.replace(service, "ssh.service"),
                sample.replace("WantedBy=sockets.target", "WantedBy=multi-user.target"),
                sample.replace(script.LEGACY_AFTER, "After=network-online.target"),
                sample.replace("Service=" + service, "Service=" + service + "\nRequires=tailscaled.service"),
                sample.replace("NoDelay=true", "NoDelay=false"),
            ]
            for candidate in changed:
                with self.subTest(unit=name, drift=candidate):
                    with self.assertRaises(RuntimeError):
                        script.unit_candidate(name, candidate)

    def test_get_units_no_mutation_on_correct_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in script.UNIT_ENDPOINTS:
                (root / name).write_text(fixture(name, script.NEW_AFTER), encoding="utf-8")
            with mock.patch.object(script, "UNIT_ROOT", root), \
                 mock.patch.object(script, "safe_root_owned_file",
                                   side_effect=lambda p: p.read_bytes()):
                result = script.get_units()
                self.assertEqual(len(result), 2)
                self.assertTrue(all(not row[3] for row in result.values()))
                self.assertTrue(all(row[1] == row[2] for row in result.values()))

    def test_get_units_rejects_existing_override(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in script.UNIT_ENDPOINTS:
                (root / name).write_text(fixture(name, script.LEGACY_AFTER), encoding="utf-8")
            first = next(iter(script.UNIT_ENDPOINTS))
            folder = root / (first + ".d")
            folder.mkdir()
            (folder / script.OVERRIDE).write_text("[Unit]\nAfter=\n", encoding="utf-8")
            with mock.patch.object(script, "UNIT_ROOT", root), \
                 mock.patch.object(script, "safe_root_owned_file",
                                   side_effect=lambda p: p.read_bytes()):
                with self.assertRaisesRegex(RuntimeError, "override"):
                    script.get_units()

    def test_effective_check_requires_active_and_correct_address(self):
        def expected(*args):
            if args[:2] == ("systemctl", "show"):
                return "After=system.slice sysinit.target\nBefore=sockets.target\nUnitFileState=enabled\nActiveState=active\n"
            if args[:2] == ("ss", "-lntH"):
                return "\n".join(
                    "LISTEN 0 4096 " + endpoint
                    for endpoint, _ in script.UNIT_ENDPOINTS.values()
                )
            raise AssertionError(args)

        with mock.patch.object(script, "run", side_effect=expected):
            script.effective_check()
        with mock.patch.object(script, "run",
                               side_effect=lambda *args: expected(*args).replace(
                                   "After=system.slice", "After=tailscaled.service")
                               if args[:2] == ("systemctl", "show") else expected(*args)):
            with self.assertRaisesRegex(RuntimeError, "startup invariant"):
                script.effective_check()

    def test_current_state_apply_is_strict_noop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            units = {}
            for name in script.UNIT_ENDPOINTS:
                path = root / name
                data = fixture(name, script.NEW_AFTER).encode()
                units[name] = (path, data, data, False)
            with mock.patch.object(script, "inspect", return_value=units), \
                 mock.patch.object(script, "effective_check") as verified, \
                 mock.patch.object(script, "atomic_file") as changed:
                script.apply()
                verified.assert_called_once()
                changed.assert_not_called()

    def test_main_default_does_not_apply(self):
        with tempfile.TemporaryDirectory() as directory:
            units = {}
            for name in script.UNIT_ENDPOINTS:
                path = Path(directory) / name
                data = fixture(name, script.NEW_AFTER).encode()
                units[name] = (path, data, data, False)
            with mock.patch.object(script, "inspect", return_value=units), \
                 mock.patch.object(script, "apply") as applied, \
                 mock.patch.object(sys, "argv", ["script.py"]):
                self.assertEqual(script.main(), 0)
                applied.assert_not_called()


if __name__ == "__main__":
    unittest.main()
