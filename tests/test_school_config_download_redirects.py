"""Regression for actual Yandex302; no network or production data."""
import hashlib
import importlib.util
import io
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

HERE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(HERE/'scripts'))
import school_settings_snapshot as snapshot
def load(name,file):
    spec=importlib.util.spec_from_file_location(name,HERE/'scripts'/file)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module
reader=load('config_reader_v2','school-settings-cloud-roundtrip.py')
transport=load('config_transport_v2','school_yandex_app_folder.py')
PAYLOAD=b'age-encryption.org/v1\n'+b'synthetic ciphertext fixture'*30
SHA=hashlib.sha256(PAYLOAD).hexdigest()
NAME='asa-school-config-01-20261108T092203Z-fc8b50ef.tar.age'


class RedirectDownload(unittest.TestCase):
    def run_download(self,responses,expected=SHA):
        calls=[]
        class Response:
            def __init__(self,status,location=None):
                self.status=status;self.location=location;self.body=io.BytesIO(PAYLOAD)
            def getheader(self,name): return self.location if name=='Location' else None
            def read(self,n): return self.body.read(n)
        class Connection:
            def __init__(self,host,port,**kw):
                self.host=host;self.port=port
            def request(self,method,path,**kw): calls.append((self.host,self.port,method,path,kw))
            def getresponse(self): return Response(*responses.pop(0))
            def close(self): pass
        provider=types.SimpleNamespace(signed_url=lambda *a:('fixture.yandex.net',443,'/initial?fixture=1'))
        with tempfile.TemporaryDirectory() as temporary:
            target=Path(temporary)/NAME
            with mock.patch.object(snapshot,'pinned_transport',return_value=transport), \
                    mock.patch.object(reader.http.client,'HTTPSConnection',Connection):
                error=None
                try: reader.download_configuration(provider,NAME,target,len(PAYLOAD),expected)
                except Exception as exc: error=exc
            copied=target.read_bytes() if target.exists() else None
            partial=target.with_suffix('.age.partial').exists()
            return copied,partial,calls,error

    def test_validated_redirects_keep_complete_sha_verified_download(self):
        data,partial,calls,error=self.run_download([(302,'https://second.yandex.ru/step'),(307,'/final'),(200,None)])
        self.assertIsNone(error)
        self.assertEqual(data,PAYLOAD)
        self.assertFalse(partial)
        self.assertEqual(len(calls),3)
        self.assertTrue(all(method=='GET' and kwargs=={} for _,_,method,_,kwargs in calls))

    def test_fourth_redirect_is_not_followed(self):
        data,partial,calls,error=self.run_download([(302,'/one'),(302,'/two'),(302,'/three'),(302,'/four')])
        self.assertEqual(str(error),'CONFIG_DOWNLOAD_REDIRECT_LIMIT')
        self.assertEqual(len(calls),4)
        self.assertIsNone(data)
        self.assertFalse(partial)

    def test_https_host_userinfo_restrictions_and_cycle(self):
        for location in ('http://second.yandex.net/file','https://untrusted.invalid/file',
                         'https://user:pass@second.yandex.net/file','/initial?fixture=1'):
            with self.subTest(location=location):
                data,partial,calls,error=self.run_download([(302,location)])
                self.assertIsNotNone(error)
                self.assertIsNone(data)
                self.assertFalse(partial)
                self.assertEqual(len(calls),1)

    def test_http_status_and_hash_failure_preserve_no_false_success(self):
        data,partial,calls,error=self.run_download([(403,None)])
        self.assertEqual(str(error),'CONFIG_DOWNLOAD_HTTP_STATUS_403')
        self.assertIsNone(data);self.assertFalse(partial)
        data,partial,calls,error=self.run_download([(302,'/final'),(200,None)],expected='0'*64)
        self.assertEqual(str(error),'CONFIG_DOWNLOAD_INTEGRITY_FAILED')
        self.assertIsNone(data);self.assertFalse(partial)


if __name__=='__main__': unittest.main(verbosity=2)
