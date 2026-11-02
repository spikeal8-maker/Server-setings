import contextlib
import importlib.util
import io
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('kernel_update', Path(__file__).parents[1] / 'scripts/update-security-kernel.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class SecurityKernelTests(unittest.TestCase):
    def invoke(self, archive='noble-security', plan='Inst linux-image-test (42)'):
        candidate = SimpleNamespace(version='42', origins=[SimpleNamespace(archive=archive)])
        package = SimpleNamespace(is_installed=True, is_upgradable=True, candidate=candidate)
        cache = {name: package for name in module.PACKAGES}
        with patch.dict('sys.modules', {'apt': SimpleNamespace(Cache=lambda: cache)}), patch('sys.argv', ['kernel_update']), patch.object(module.subprocess, 'run', return_value=SimpleNamespace(stdout=plan)) as run, contextlib.redirect_stdout(io.StringIO()):
            module.main()
            return run.call_args.args[0]

    def test_plan_never_installs_and_keeps_no_removal_guard(self):
        command = self.invoke()
        self.assertIn('--simulate', command)
        self.assertIn('--no-remove', command)
        self.assertIn('--only-upgrade', command)
        self.assertIn('linux-generic-hwe-24.04=42', command)

    def test_non_security_origin_is_refused(self):
        with self.assertRaises(AssertionError):
            self.invoke(archive='noble-updates')

    def test_removal_or_unrelated_package_is_refused(self):
        for plan in ['Remv openssh-server', 'Inst network-manager (42)']:
            with self.assertRaises(AssertionError):
                self.invoke(plan=plan)


if __name__ == '__main__':
    unittest.main()
