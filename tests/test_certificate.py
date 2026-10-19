import importlib.util
from pathlib import Path
import unittest
from unittest import mock
from datetime import datetime, timezone, timedelta

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('check_certificate', ROOT/'scripts/check-certificate.py')
cert=importlib.util.module_from_spec(spec)
spec.loader.exec_module(cert)


class TestCertMonitor(unittest.TestCase):
    def test_expiration_threshold(self):
        expiry=(datetime.now(timezone.utc)+timedelta(days=5)).strftime('%b %d %H:%M:%S %Y GMT')
        fake={'notAfter':expiry,'subjectAltName':(('IP Address','94.29.44.211'),)}
        stream=mock.MagicMock()
        stream.__enter__.return_value.getpeercert.return_value=fake
        tcp=mock.MagicMock()
        ctx=mock.MagicMock()
        ctx.wrap_socket.return_value=stream
        with mock.patch.object(cert.socket,'create_connection',return_value=tcp), mock.patch.object(cert.ssl,'create_default_context',return_value=ctx):
            out=cert.inspect_certificate('94.29.44.211')
        self.assertTrue(out['verified_trust'])
        self.assertGreater(out['hours_until_expiry'], 100)

    def test_missing_ip_san_rejected(self):
        future=(datetime.now(timezone.utc)+timedelta(days=5)).strftime('%b %d %H:%M:%S %Y GMT')
        fake={'notAfter':future,'subjectAltName':(('DNS','example.org'),)}
        ctx=mock.MagicMock()
        ctx.wrap_socket.return_value.__enter__.return_value.getpeercert.return_value=fake
        with mock.patch.object(cert.socket,'create_connection'), mock.patch.object(cert.ssl,'create_default_context',return_value=ctx):
            with self.assertRaisesRegex(ValueError,'не содержит'):
                cert.inspect_certificate('94.29.44.211')
