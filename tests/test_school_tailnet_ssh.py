import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

source=Path(__file__).resolve().parents[1]/"scripts/enable-school-tailnet-ssh.py"
spec=importlib.util.spec_from_file_location("school_tailnet_ssh",source)
mod=importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

class TailnetFirewallTests(unittest.TestCase):
    def test_exact_scoped_rule(self):
        self.assertEqual(mod.DEVICE,"tailscale0")
        self.assertEqual(mod.D2_IP,"100.96.255.113")
        self.assertEqual(mod.VM_TAIL_IP,"100.124.247.24")
        self.assertEqual(mod.RULE,("allow","in","on","tailscale0",
                        "from","100.96.255.113","to","any",
                        "port","22","proto","tcp"))
        self.assertNotIn("19192",mod.RULE)
        self.assertNotIn("39443",mod.RULE)
        self.assertNotIn("0.0.0.0/0",mod.RULE)

    def test_verified_peer_accepts_case_but_rejects_other_tailnet(self):
        correct={"HostName":"D2-R2","DNSName":"d2-r2.tail605710.ts.net.",
                 "TailscaleIPs":["100.96.255.113"],"Online":True}
        self.assertTrue(mod.d2_peer_verified(correct))
        for change in (
            {"HostName":"unrelated"},
            {"DNSName":"d2-r2.other.ts.net."},
            {"TailscaleIPs":["100.96.255.114"]},
            {"Online":False},
        ):
            with self.subTest(change=change):
                candidate={**correct,**change}
                self.assertFalse(mod.d2_peer_verified(candidate))

    def test_rule_detected_only_with_full_identity(self):
        good="-A ufw-user-input -i tailscale0 -p tcp -s 100.96.255.113/32 -m tcp --dport 22 -j ACCEPT\n"
        with patch.object(mod,"command",return_value=good):
            self.assertTrue(mod.rule_installed())
        for s in (good.replace("tailscale0","eth0"),
                  good.replace("100.96.255.113/32","100.96.255.114/32"),
                  good.replace("--dport 22","--dport 19192"),
                  good.replace("-j ACCEPT","-j DROP")):
            with self.subTest(s=s),patch.object(mod,"command",return_value=s):
                self.assertFalse(mod.rule_installed())

if __name__=="__main__":
    unittest.main()
