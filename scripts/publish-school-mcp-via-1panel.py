#!/usr/bin/env python3
"""Publish VM100 MCP-only paths through the official 1Panel v2 proxy API.

Does not overwrite the ASA root proxy, FRP config, DNS or external TLS.
A failed preflight restores only routes created during this invocation.
"""
import argparse
import importlib.util
import json
import os
import socket
import ssl
import sys
import urllib.error
import urllib.request

DOMAIN="x.asa-lab.ru"
HOST="asa-school-01"
MCP="http://127.0.0.1:39450"
PATHS=(
 ("asa-mcp","^~","/mcp"),
 ("asa-mcp-oauth-metadata","^~","/.well-known/oauth-"),
 ("asa-mcp-oauth-jwks","=","/.well-known/jwks.json"),
 ("asa-mcp-identity","^~","/.idp/"),
 ("asa-mcp-login","^~","/.auth/"),
)

class PublishError(Exception): pass

def panel():
    if os.geteuid()!=0 or socket.gethostname()!=HOST: raise PublishError("WRONG_HOST")
    modpath="/srv/Server-setings/scripts/panelctl.py"
    spec=importlib.util.spec_from_file_location("school_panelctl",modpath)
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    api=mod.PanelAPI("http://127.0.0.1:19192",mod.read_api_key("/var/lib/server-settings/1panel-api.key"))
    mod.require_local_write(api)
    sites=mod.websites(api)
    if len(sites)!=1 or sites[0].get("id")!=1 or sites[0].get("primaryDomain")!=DOMAIN or sites[0].get("alias")!="asa-lab-x":
        raise PublishError("1PANEL_WEBSITE_IDENTITY_CHANGED")
    return api

def proxies(api):
    arr=api.request("POST","/websites/proxies",{"id":1})
    if not isinstance(arr,list): raise PublishError("PROXY_LIST_NOT_ARRAY")
    result={item.get("name"):item for item in arr}
    if len(result)!=len(arr):raise PublishError("DUPLICATED_PROXY_NAMES")
    return result

def unchanged_root(api):
    records=proxies(api)
    if set(records)!={"root"}:raise PublishError("UNEXPECTED_EXISTING_PROXIES")
    root=records["root"]
    if tuple(root.get(x) for x in ("modifier","match","proxyPass","proxyHost"))!=("^~","/","http://127.0.0.1:4610","$host"):
        raise PublishError("ROOT_PROXY_DRIFT")

def http_probe(path,public):
    url=("https://x.asa-lab.ru" if public else "http://127.0.0.1")+path
    request=urllib.request.Request(url,headers={} if public else {"Host":DOMAIN})
    try:
        with urllib.request.urlopen(request,timeout=10,context=ssl.create_default_context() if public else None) as reply:
            return reply.status,dict(reply.headers),reply.read(15000)
    except urllib.error.HTTPError as e:
        return e.code,dict(e.headers),e.read(15000)

def ready():
    for remote in (False,True):
        status,_,data=http_probe("/health/ready",remote)
        if status!=200: raise PublishError("ASA_HEALTH_HTTP_"+str(status))
        obj=json.loads(data)
        if obj.get("status")!="ready" or obj.get("deployment",{}).get("schemaVersion")!=286:
            raise PublishError("ASA_HEALTH_SCHEMA_WRONG")

def gateway_private():
    for path,key,expected in (("/.well-known/oauth-protected-resource/mcp","resource","https://x.asa-lab.ru/mcp"),
                              ("/.well-known/oauth-authorization-server","issuer","https://x.asa-lab.ru")):
        with urllib.request.urlopen(MCP+path,timeout=8) as response:
            if response.status!=200 or json.load(response).get(key,"").rstrip("/")!=expected:
                raise PublishError("GATEWAY_LOCAL_OAUTH_METADATA_WRONG")
    try:urllib.request.urlopen(MCP+"/mcp",timeout=8)
    except urllib.error.HTTPError as e:
        if e.code==401:return
    raise PublishError("UNAUTHENTICATED_LOCAL_MCP_EXPOSED")

def create(api,name,modifier,match):
    api.request("POST","/websites/proxies/update",{"id":1,"operate":"create","name":name,
       "modifier":modifier,"match":match,"proxyPass":MCP,"proxyHost":"$host",
       "enable":True,"cache":False,"cors":False})
    result=proxies(api).get(name,{})
    if result.get("match")!=match or result.get("proxyPass")!=MCP or not result.get("enable"):
        raise PublishError("1PANEL_PROXY_CREATE_VERIFY_FAILED")

def rollback_one(api,name):
    api.request("POST","/websites/proxies/delete",{"id":1,"name":name})

def external():
    for path,expected in (("/.well-known/oauth-protected-resource/mcp",200),
                          ("/.well-known/oauth-authorization-server",200),
                          ("/.well-known/jwks.json",200),
                          ("/.auth/login",200),
                          ("/mcp",401)):
        code,headers,body=http_probe(path,True)
        if code!=expected:raise PublishError("PUBLIC_MCP_HTTP_"+path.replace("/","_")+"_"+str(code))
        if path.endswith("resource/mcp") and json.loads(body).get("resource")!="https://x.asa-lab.ru/mcp":
            raise PublishError("PUBLIC_OAUTH_RESOURCE_MISMATCH")
        if path=="/mcp" and "oauth-protected-resource/mcp" not in headers.get("WWW-Authenticate",""):
            raise PublishError("PUBLIC_CHALLENGE_MISSING")
    ready()

def run(apply):
    api=panel();unchanged_root(api);ready();gateway_private()
    if not apply:
        print("PLAN: create only five managed MCP locations, leave ASA root/FRP/DNS/TLS unchanged")
        return
    added=[]
    try:
        for name,modifier,match in PATHS:
            create(api,name,modifier,match);added.append(name)
            ready()
        external()
        if set(proxies(api))!=set(["root"]+[v[0] for v in PATHS]):
            raise PublishError("FINAL_PROXY_INVENTORY_MISMATCH")
    except BaseException:
        for name in reversed(added):
            try:rollback_one(api,name)
            except Exception:pass
        print("NEW_MCP_LOCATIONS_ROLLBACK_ATTEMPTED",file=sys.stderr)
        raise
    print("MCP_OAUTH_PUBLIC_1PANEL_MANAGED_LOCATIONS=5")
    print("MCP_METADATA_PUBLIC_200=YES")
    print("MCP_UNAUTHENTICATED_PUBLIC_401=YES")
    print("ASA_PUBLIC_READY_200=YES")
    print("CHATGPT_OWNER_OAUTH_LOGIN_PENDING=YES")

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--apply",action="store_true")
    args=p.parse_args()
    try:run(args.apply)
    except (PublishError,OSError,ValueError) as e:
        print("MCP_PROXY_BLOCKED="+(str(e) if isinstance(e,PublishError) else type(e).__name__),file=sys.stderr)
        return 2
    return 0

if __name__=="__main__":
    raise SystemExit(main())
