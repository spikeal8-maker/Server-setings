import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
spec = importlib.util.spec_from_file_location('publisher', Path(__file__).parents[1] / 'scripts/publish-panel-https.py')
publisher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publisher)


class Api:
    def __init__(self, tls=True):
        self.calls = []
        self.tls = tls

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path.endswith('/https') and method == 'GET':
            return {'enable': self.tls, 'httpConfig': 'HTTPToHTTPS'}
        if path == '/websites/proxies':
            return [{'name': 'root', 'match': '/'}]
        return None


SITE = {'id': 8, 'primaryDomain': publisher.DOMAIN, 'alias': publisher.ALIAS, 'type': 'proxy'}
CERT = {'id': 9, 'primaryDomain': publisher.DOMAIN, 'status': 'ready', 'autoRenew': True,
        'expireDate': '2099-01-01T00:00:00Z'}


class PublisherTests(unittest.TestCase):
    def test_plan_does_not_mutate(self):
        api = Api()
        with patch.object(publisher, 'websites', return_value=[]), patch.object(publisher, 'certificates', return_value=[]):
            self.assertTrue(publisher.deploy(api, 'prepare', False, None, None)['plan'])
        self.assertEqual(api.calls, [])

    def test_foreign_site_refused(self):
        with patch.object(publisher, 'websites', return_value=[{**SITE, 'alias': 'other'}]):
            with self.assertRaises(RuntimeError):
                publisher.find_site(Api())

    def test_missing_tls_confirmation_never_connects_panel(self):
        api = Api(tls=False)
        with patch.object(publisher, 'websites', return_value=[SITE]), patch.object(publisher, 'certificates', return_value=[CERT]), patch.object(publisher.socket, 'gethostbyname_ex', return_value=('', [], ['1.2.3.4'])):
            with self.assertRaises(RuntimeError):
                publisher.deploy(api, 'activate', True, None, '1.2.3.4')
        self.assertFalse(any(path == '/websites/proxies/file' for _, path, _ in api.calls))

    def test_expired_cert_never_connects_panel(self):
        api = Api()
        with patch.object(publisher, 'websites', return_value=[SITE]), patch.object(publisher, 'certificates', return_value=[{**CERT, 'expireDate': '2000-01-01T00:00:00Z'}]), patch.object(publisher.socket, 'gethostbyname_ex', return_value=('', [], ['1.2.3.4'])):
            with self.assertRaises(RuntimeError):
                publisher.deploy(api, 'activate', True, None, '1.2.3.4')
        self.assertEqual(api.calls, [])

    def test_activation_follows_tls_and_preserves_websocket(self):
        api = Api()
        with patch.object(publisher, 'websites', return_value=[SITE]), patch.object(publisher, 'certificates', return_value=[CERT]), patch.object(publisher.socket, 'gethostbyname_ex', return_value=('', [], ['1.2.3.4'])):
            self.assertTrue(publisher.deploy(api, 'activate', True, None, '1.2.3.4')['activated'])
        self.assertTrue(api.calls[0][1].endswith('/https'))
        content = api.calls[-1][2]['content']
        self.assertIn('proxy_set_header Upgrade $http_upgrade;', content)
        self.assertIn('proxy_set_header X-Forwarded-For $remote_addr;', content)

    def test_rollback_only_quarantines_owned_proxy(self):
        api = Api()
        with patch.object(publisher, 'websites', return_value=[SITE]), patch.object(publisher, 'certificates', return_value=[CERT]):
            publisher.deploy(api, 'rollback', True, None, None)
        self.assertEqual(api.calls[-1][1], '/websites/proxies/file')
        self.assertNotIn('19192', api.calls[-1][2]['content'])


if __name__ == '__main__':
    unittest.main()
