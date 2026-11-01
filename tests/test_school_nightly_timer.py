import importlib.util
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]
P=ROOT/"scripts/install-school-nightly-database-backup.py"
spec=importlib.util.spec_from_file_location("school_backup_timer",P)
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class NightlyTimerTests(unittest.TestCase):
    def test_daily_night_moscow_no_catchup(self):
        self.assertIn("OnCalendar=*-*-* 03:20:00 Europe/Moscow",module.TIMER_BODY)
        self.assertIn("Persistent=false",module.TIMER_BODY)
        self.assertIn("RandomizedDelaySec=0",module.TIMER_BODY)
        self.assertIn("Unit=asa-school-nightly-database-backup.service",module.TIMER_BODY)
        self.assertNotIn("OnBootSec=",module.TIMER_BODY)
        self.assertNotIn("Persistent=true",module.TIMER_BODY)

    def test_service_uses_only_pinned_school_backups(self):
        self.assertIn("User=root",module.SERVICE_BODY)
        self.assertIn("WorkingDirectory=/srv/asa-lab",module.SERVICE_BODY)
        self.assertIn("school-nightly-database-backup.py --apply",module.SERVICE_BODY)
        self.assertIn("TimeoutStartSec=infinity",module.SERVICE_BODY)
        self.assertIn("UMask=0077",module.SERVICE_BODY)
        self.assertNotIn("docker compose down -v",module.SERVICE_BODY)
        self.assertEqual(module.ROOT,Path("/var/backups/asa-school-nightly"))

    def test_timers_not_run_from_preflight_or_dryrun(self):
        text=P.read_text(encoding="utf-8")
        self.assertIn("if args.apply:",text)
        self.assertIn("run([\"/usr/bin/python3\",str(SCRIPT),\"--preflight\"]",text)
        self.assertIn("systemctl\",\"enable\",\"--now\",TIMER.name",text)
        self.assertNotIn("systemctl\",\"start\",SERVICE.name",text)


if __name__=="__main__":
    unittest.main()
