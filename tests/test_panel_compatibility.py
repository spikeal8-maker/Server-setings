import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from asa_ops.panel_api import list_databases
class PanelCompatibilityTests(unittest.TestCase):
    def test_supported_sort_and_secret_filtering(self):
        reply={'code':200,'data':{'total':1,'items':[{'id':1,'name':'fixture','username':'fixture-user','password':'private-fixture-password','address':'private-address'}]}}
        captured=[]
        def respond(request,timeout):
            captured.append(json.loads(request.data));return io.BytesIO(json.dumps(reply).encode())
        with patch.dict('os.environ',{'PANEL_HOST':'http://127.0.0.1:19192','PANEL_ACCESS_TOKEN':'fixture-key'}),patch('urllib.request.urlopen',side_effect=respond):
            result=list_databases({'name':'fixture'})
        self.assertEqual(captured[0]['orderBy'],'createdAt');self.assertEqual(captured[0]['info'],'fixture')
        self.assertNotIn('private-fixture-password',json.dumps(result));self.assertNotIn('private-address',json.dumps(result))
        self.assertEqual(result['structuredContent']['data']['total'],1)
if __name__=='__main__':unittest.main()
