"""Synthetic published producer contract, with real receipt writer and no cloud/DB."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT/'scripts'
sys.path.insert(0, str(SCRIPTS))
CONTRACT = json.loads((ROOT/'tests/fixtures/school-backup-success.json').read_text())


class SchoolReceiptContractTests(unittest.TestCase):
    def test_contract_is_pinned_to_the_actual_producer_source(self):
        self.assertEqual(set(CONTRACT['receipts']), {'manual', 'scheduled'})
        self.assertEqual(hashlib.sha256((ROOT/CONTRACT['producerPath']).read_bytes().replace(b'\r\n', b'\n')).hexdigest(), CONTRACT['producerLfSha256'])

    @unittest.skipIf(os.name == 'nt', 'Actual producer requires Linux fcntl and private file modes; Linux CI gate')
    def test_real_writer_matches_published_manual_and_default_scheduled_receipts(self):
        spec = importlib.util.spec_from_file_location('school_receipt_producer', SCRIPTS/'school-nightly-database-backup.py')
        producer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(producer)
        clock = datetime.fromisoformat(CONTRACT['receipts']['scheduled']['createdAt'])
        for trigger, expected in CONTRACT['receipts'].items():
            with self.subTest(trigger=trigger), tempfile.TemporaryDirectory() as temp:
                target = Path(temp)
                with patch.object(producer, 'ROOT', target), patch.object(producer, 'datetime') as fake_clock:
                    fake_clock.now.return_value = clock
                    result = {'name': expected['archive'], 'size': expected['size'], 'sha256': expected['sha256']}
                    producer.write_verified_receipt(result, **({'trigger': trigger} if trigger == 'manual' else {}))
                receipt = target/'latest-success.json'
                self.assertEqual(json.loads(receipt.read_text()), expected)
                self.assertEqual(stat.S_IMODE(receipt.stat().st_mode), 0o600)


if __name__ == '__main__':
    unittest.main()
