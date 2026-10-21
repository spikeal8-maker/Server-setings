"""Read-only 1Panel v2 compatibility; never returns stored instance credentials."""
import hashlib
import json
import os
import time
import urllib.request
from urllib.parse import urlsplit
from .store import OpsError


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,request,fp,code,msg,headers,newurl):
        return None


def list_databases(args):
    if set(args)!={'name'} or not isinstance(args['name'],str) or not 0<len(args['name'])<=256:
        raise OpsError('DATABASE_NAME','A database instance name is required')
    host=os.environ.get('PANEL_HOST','').rstrip('/')
    parsed=urlsplit(host)
    if parsed.scheme!='http' or parsed.hostname not in ('127.0.0.1','localhost') or parsed.path or parsed.query or parsed.fragment or parsed.username:
        raise OpsError('PANEL_HOST','Expected the configured private local panel')
    stamp=str(int(time.time()));key=os.environ.get('PANEL_ACCESS_TOKEN','')
    if not key:raise OpsError('PANEL_AUTH','Panel key is not configured')
    payload={'page':1,'pageSize':500,'order':'null','orderBy':'createdAt','info':args['name']}
    request=urllib.request.Request(host+'/api/v2/databases/db/search',data=json.dumps(payload).encode(),method='POST',
        headers={'Content-Type':'application/json','1Panel-Timestamp':stamp,
                 '1Panel-Token':hashlib.md5(('1panel'+key+stamp).encode()).hexdigest()})
    try:
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
        with opener.open(request,timeout=20) as response:
            raw=response.read(1048577)
        if len(raw)>1048576:raise ValueError()
        document=json.loads(raw)
        if document.get('code')!=200:raise ValueError()
        data=document['data']
        rows=data['items']
        if rows is None and data['total']==0:rows=[]
        if not isinstance(rows,list):raise ValueError()
        safe={'code':200,'message':'','data':{'total':data['total'],'items':[
            {key:row[key] for key in ('id','name','username') if key in row} for row in rows]}}
    except Exception:
        raise OpsError('PANEL_DATABASE_LIST','Panel database-instance query failed') from None
    return {'content':[{'type':'text','text':json.dumps(safe)+'\nScope: registered 1Panel database instances only; ASA Compose PostgreSQL is checked separately by runtime_identity.'}],
            'structuredContent':safe,'isError':False}
