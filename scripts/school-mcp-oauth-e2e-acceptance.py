#!/usr/bin/env python3
"""One-shot OAuth/PKCE + MCP read-only end-to-end acceptance via public HTTPS.

Only VM100 root may run this test; owner password is read from its root-only
file. No login details, JWT, DCR client IDs or authorization codes are printed
or retained locally. Adds one test DCR client to the persistent OAuth store,
which is a normal operation of the gateway. Never invokes a mutation tool.
"""
from __future__ import annotations
import base64
import hashlib
import http.cookiejar
import json
import os
from pathlib import Path
import secrets
import socket
import ssl
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT="https://x.asa-lab.ru"
CALLBACK="http://127.0.0.1:61987/asa-mcp-oauth-proof"
PASSWORD=Path("/var/lib/server-settings/asa-school-mcp-owner-password")
REQUIRED={"create_database","create_ssl","create_website","get_dashboard_info",
          "get_system_info","install_mysql","install_openresty","list_databases",
          "list_installed_apps","list_ssls","list_websites"}


class ProofError(Exception):
    pass


def callback_code(status, headers, state):
    if status not in (302, 303):
        raise ProofError("CONSENT_NOT_REDIRECTED_"+str(status))
    value=headers.get("Location")
    if not value:
        raise ProofError("OAUTH_REDIRECT_MISSING")
    actual=urllib.parse.urlsplit(value)
    expected=urllib.parse.urlsplit(CALLBACK)
    if ((actual.scheme,actual.netloc,actual.path)!=(expected.scheme,expected.netloc,expected.path)
            or actual.fragment):
        raise ProofError("CALLBACK_URI_WRONG")
    qs=urllib.parse.parse_qs(actual.query,keep_blank_values=True)
    if (qs.get("state")!=[state] or len(qs.get("code",[]))!=1
            or not qs["code"][0] or "error" in qs):
        raise ProofError("CODE_STATE_MISMATCH")
    if "iss" in qs and qs["iss"]!=[ROOT+"/"]:
        raise ProofError("CALLBACK_ISSUER_WRONG")
    return qs["code"][0]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def decode_jwt_body(token: str) -> dict:
    parts=token.split(".")
    if len(parts)!=3:
        raise ProofError("JWT_SHAPE_INVALID")
    try:
        data=base64.urlsafe_b64decode(parts[1]+"="*((-len(parts[1]))%4))
        return json.loads(data)
    except (UnicodeError, ValueError):
        raise ProofError("JWT_PAYLOAD_INVALID") from None


def run() -> None:
    if os.geteuid()!=0 or socket.gethostname()!="asa-school-01":
        raise ProofError("VM100_ROOT_REQUIRED")
    st=PASSWORD.lstat()
    if (not stat.S_ISREG(st.st_mode) or st.st_uid!=0
            or stat.S_IMODE(st.st_mode)!=0o600):
        raise ProofError("OWNER_PASSWORD_FILE_UNTRUSTED")
    pwd=PASSWORD.read_text(encoding="utf-8").strip()
    if len(pwd)<40:
        raise ProofError("OWNER_PASSWORD_MISSING")

    jar=http.cookiejar.CookieJar()
    opener=urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        urllib.request.HTTPCookieProcessor(jar),
        NoRedirect()
    )

    def request(path: str, *, data: bytes | None = None,
                content_type: str | None = None, headers: dict | None = None):
        if path.startswith(ROOT):
            address=path
        else:
            address=ROOT+path
        parts=urllib.parse.urlsplit(address)
        if parts.scheme!="https" or parts.hostname!="x.asa-lab.ru":
            raise ProofError("REDIRECT_ORIGIN_INVALID")
        h=dict(headers or {})
        if content_type:
            h["Content-Type"]=content_type
        if data is None:
            req=urllib.request.Request(address,headers=h,method="GET")
        else:
            req=urllib.request.Request(address,headers=h,data=data,method="POST")
        try:
            with opener.open(req,timeout=12) as response:
                return response.status, response.headers, response.read(135000)
        except urllib.error.HTTPError as err:
            return err.code, err.headers, err.read(135000)

    def redirect_location(headers, expected_host: str):
        value=headers.get("Location")
        if not value:
            raise ProofError("OAUTH_REDIRECT_MISSING")
        url=urllib.parse.urljoin(ROOT+"/",value)
        p=urllib.parse.urlsplit(url)
        if p.scheme=="https" and p.hostname==expected_host:
            return url
        if expected_host=="127.0.0.1" and p.hostname=="127.0.0.1":
            return url
        raise ProofError("OAUTH_REDIRECT_UNTRUSTED")

    verifier=secrets.token_urlsafe(48)
    challenge=base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    state=secrets.token_urlsafe(20)

    registration={
        "client_name":"ASA-School-VM100-OAuth-Proof",
        "redirect_uris":[CALLBACK],
        "token_endpoint_auth_method":"none",
        "grant_types":["authorization_code","refresh_token"],
        "response_types":["code"],
    }
    status,_,body=request("/.idp/register",data=json.dumps(registration).encode(),
                          content_type="application/json")
    if status!=201:
        raise ProofError("DCR_REGISTRATION_HTTP_"+str(status))
    reg=json.loads(body)
    client_id=reg.get("client_id")
    if not isinstance(client_id,str) or len(client_id)<8:
        raise ProofError("DCR_CLIENT_ID_MISSING")
    print("PUBLIC_OAUTH_DYNAMIC_CLIENT_REGISTRATION=201")

    args={
        "response_type":"code","client_id":client_id,"redirect_uri":CALLBACK,
        "state":state,"code_challenge":challenge,"code_challenge_method":"S256",
        "resource":ROOT+"/mcp",
    }
    auth_url=ROOT+"/.idp/auth?"+urllib.parse.urlencode(args)
    status,headers,_=request(auth_url)
    if status!=302:
        raise ProofError("AUTHORIZE_START_NOT_302_"+str(status))
    consent_url=redirect_location(headers,"x.asa-lab.ru")
    if "/.idp/auth/" not in consent_url:
        raise ProofError("AUTHORIZE_CONSENT_ROUTE_WRONG")

    status,headers,_=request(consent_url)
    if status!=302:
        raise ProofError("OWNER_LOGIN_REDIRECT_MISSING_"+str(status))
    login_url=redirect_location(headers,"x.asa-lab.ru")
    if "/.auth/login" not in login_url:
        raise ProofError("OWNER_LOGIN_ROUTE_WRONG")

    status,_,body=request(login_url)
    if status!=200 or b'name="password"' not in body:
        raise ProofError("OWNER_LOGIN_FORM_FAILED")
    status,headers,_=request("/.auth/login",
        data=urllib.parse.urlencode({"password":pwd}).encode(),
        content_type="application/x-www-form-urlencoded")
    pwd=""
    if status!=302:
        raise ProofError("OWNER_PASSWORD_LOGIN_REJECTED_"+str(status))
    destination=redirect_location(headers,"x.asa-lab.ru")
    if destination!=consent_url:
        raise ProofError("OWNER_LOGIN_RETURN_ROUTE_CHANGED")
    print("PUBLIC_OAUTH_OWNER_LOGIN=PASS")

    status,_,body=request(consent_url)
    if status!=200 or b'form method="post"' not in body:
        raise ProofError("CONSENT_FORM_NOT_AVAILABLE")
    status,headers,_=request(consent_url,data=b"",
                             content_type="application/x-www-form-urlencoded")
    code=callback_code(status,headers,state)
    print("PUBLIC_OAUTH_AUTHORIZATION_CODE_PKCE=PASS")

    token_form={
        "grant_type":"authorization_code","code":code,
        "client_id":client_id,"redirect_uri":CALLBACK,
        "code_verifier":verifier,
        "resource":ROOT+"/mcp",
    }
    status,_,body=request("/.idp/token",
        data=urllib.parse.urlencode(token_form).encode(),
        content_type="application/x-www-form-urlencoded")
    if status!=200:
        raise ProofError("TOKEN_EXCHANGE_HTTP_"+str(status))
    response=json.loads(body)
    token=response.get("access_token")
    if not isinstance(token,str) or len(token)<100:
        raise ProofError("ACCESS_TOKEN_MISSING")
    claims=decode_jwt_body(token)
    if claims.get("iss","").rstrip("/")!=ROOT:
        raise ProofError("JWT_ISSUER_WRONG")
    aud=claims.get("aud")
    if (aud != ROOT+"/mcp" and
        not (isinstance(aud,list) and aud==[ROOT+"/mcp"])):
        raise ProofError("JWT_AUDIENCE_NOT_BOUND_TO_MCP_RESOURCE")
    print("PUBLIC_OAUTH_TOKEN_AUDIENCE_MCP_ONLY=PASS")

    def rpc(method,request_id,params,session=None,bearer=None):
        h={
            "Authorization":"Bearer "+(bearer or token),
            "Accept":"application/json, text/event-stream",
            "MCP-Protocol-Version":"2025-03-26",
        }
        if session:
            h["Mcp-Session-Id"]=session
        payload={"jsonrpc":"2.0","id":request_id,"method":method,"params":params}
        status,headers,body=request("/mcp",data=json.dumps(payload).encode(),
                                   content_type="application/json",headers=h)
        return status,headers,body

    status,_,_=rpc("initialize",99,{
        "protocolVersion":"2025-03-26","capabilities":{},
        "clientInfo":{"name":"ASA-VM100-HTTPS-acceptance","version":"1"},
    },bearer="invalid-token")
    if status!=401:
        raise ProofError("INVALID_BEARER_WAS_NOT_REJECTED")
    print("PUBLIC_MCP_INVALID_TOKEN=401")

    status,headers,body=rpc("initialize",1,{
        "protocolVersion":"2025-03-26","capabilities":{},
        "clientInfo":{"name":"ASA-VM100-HTTPS-acceptance","version":"1"},
    })
    if status!=200:
        raise ProofError("AUTHENTICATED_MCP_INIT_"+str(status))
    session=headers.get("Mcp-Session-Id")
    if not session:
        raise ProofError("MCP_SESSION_MISSING")

    status,headers,body=rpc("tools/list",2,{},session)
    if status!=200:
        raise ProofError("AUTHENTICATED_TOOLS_LIST_HTTP_"+str(status))
    content_type=headers.get("Content-Type","")
    events=[]
    if "text/event-stream" in content_type:
        for line in body.decode("utf-8","replace").splitlines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    else:
        events=[json.loads(body)]
    result=[e["result"] for e in events if e.get("id")==2 and "result" in e]
    if len(result)!=1:
        raise ProofError("MCP_TOOLS_RPC_INVALID")
    got={item.get("name") for item in result[0].get("tools",[])}
    if got!=REQUIRED:
        raise ProofError("MCP_FULL_TOOL_SET_UNEXPECTED")
    print("PUBLIC_MCP_FULL_TOOLS_LIST=11")
    print("PUBLIC_HTTPS_OAUTH_MCP_SERVER_ACCEPTANCE=PASS")
    print("CHATGPT_APP_CONNECTION_OWNER_ACTION_REQUIRED=YES")


if __name__=="__main__":
    try:
        run()
    except (ProofError, ValueError, OSError, json.JSONDecodeError) as e:
        label=e.args[0] if isinstance(e,ProofError) else type(e).__name__
        print("PUBLIC_HTTPS_MCP_E2E_FAILED="+str(label),file=sys.stderr)
        sys.exit(2)

