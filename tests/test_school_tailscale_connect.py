import importlib.util
from pathlib import Path
import unittest

HERE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "school_tailscale_connect", HERE / "scripts/school-tailscale-connect.py"
)
subject = importlib.util.module_from_spec(spec)
spec.loader.exec_module(subject)


class SchoolTailscaleConnectTests(unittest.TestCase):
    def test_one_off_key_format(self):
        secret = "tskey-auth-" + "q" * 48
        self.assertEqual(subject.validate_key(secret + " \n"), secret)
        for bad in ("", "tskey-api-" + "a" * 48,
                    "tskey-auth-short", "tskey-auth-" + "a" * 20 + " --force-reauth"):
            with self.subTest(bad=bad[:25]):
                with self.assertRaises(subject.TailnetSetupError):
                    subject.validate_key(bad)

    def test_auth_key_never_in_process_arguments(self):
        key_file = "/run/tailscale/.asa-vm100-authkey-6431d"
        args = subject.up_command(key_file)
        self.assertEqual(args[0:2], ["/usr/bin/tailscale", "up"])
        self.assertIn("--auth-key=file:" + key_file, args)
        self.assertIn("--accept-dns=false", args)
        self.assertIn("--accept-routes=false", args)
        self.assertIn("--hostname=asa-school-01", args)
        self.assertNotIn("tskey-auth-", " ".join(args))
        for unsafe in ("./token", "/tmp/asa-key", "token\x00injected"):
            with self.subTest(unsafe=unsafe):
                with self.assertRaises(subject.TailnetSetupError):
                    subject.up_command(unsafe)

    def test_ready_requires_correct_tailnet_and_running_vm_identity(self):
        good = {
            "state": "Running", "ip": ["100.120.1.3"],
            "online": True, "dns_name": "asa-school-01.tail605710.ts.net.",
            "node_id_set": True,
        }
        self.assertTrue(subject.successful(good))
        for field, wrong in (
            ("state", "NeedsLogin"), ("ip", []), ("online", False),
            ("dns_name", "asa-school-01.other.ts.net."),
            ("node_id_set", False),
        ):
            with self.subTest(field=field):
                trial = dict(good)
                trial[field] = wrong
                self.assertFalse(subject.successful(trial))


if __name__ == "__main__":
    unittest.main()
