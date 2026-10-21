import io
import json
from pathlib import Path
import sys
import unittest
import http.server
import threading
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from asa_ops.panel_api import list_databases
from asa_ops.store import OpsError
class PanelCompatibilityTests(unittest.TestCase):
    def test_redirects_and_environment_proxies_cannot_receive_signed_headers(self):
        counts={'POST':0,'GET':0}
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                counts['POST']+=1;self.send_response(302);self.send_header('Location','/unexpected');self.end_headers()
            def do_GET(self):
                counts['GET']+=1;self.send_response(200);self.end_headers()
        server=http.server.HTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with patch.dict('os.environ',{'PANEL_HOST':'http://127.0.0.1:'+str(server.server_port),
                    'PANEL_ACCESS_TOKEN':'fixture-key','HTTP_PROXY':'http://127.0.0.1:9','http_proxy':'http://127.0.0.1:9','NO_PROXY':'','no_proxy':''}):
                with self.assertRaises(OpsError):list_databases({'name':'fixture'})
            self.assertEqual(counts,{'POST':1,'GET':0})
        finally:server.shutdown();server.server_close();thread.join()

    def test_supported_sort_and_secret_filtering(self):
        reply={'code':200,'data':{'total':1,'items':[{'id':1,'name':'fixture','username':'fixture-user','password':'private-fixture-password','address':'private-address'}]}}
        captured=[]
        def respond(request,timeout):
            self.assertTrue(request.full_url.endswith('/api/v2/databases/db/search'))
            captured.append(json.loads(request.data));return io.BytesIO(json.dumps(reply).encode())
        with patch.dict('os.environ',{'PANEL_HOST':'http://127.0.0.1:19192','PANEL_ACCESS_TOKEN':'fixture-key'}),patch('urllib.request.build_opener',return_value=SimpleNamespace(open=respond)):
            result=list_databases({'name':'fixture'})
        self.assertEqual(captured[0]['orderBy'],'createdAt');self.assertEqual(captured[0]['info'],'fixture')
        self.assertNotIn('private-fixture-password',json.dumps(result));self.assertNotIn('private-address',json.dumps(result))
        self.assertEqual(result['structuredContent']['data']['total'],1)
if __name__=='__main__':unittest.main()
