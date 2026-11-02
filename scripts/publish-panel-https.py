#!/usr/bin/env python3
"""Publish 1Panel via its API-managed OpenResty site; plan by default.

prepare creates an unavailable upstream and requests ACME; activate requires
ready TLS plus HTTP->HTTPS before it connects the panel. rollback quarantines
only this owned site, without deleting certificates, sites or application data.
"""
import argparse
import json
import re
import socket
import time

from panelctl import PanelAPI, certificates, read_api_key, websites

DOMAIN = '1panel.asa-lab.ru'
ALIAS = 'asa-admin-panel-https'
SINK = 'http://127.0.0.1:9'
UPSTREAM = 'http://127.0.0.1:19192'


def proxy_content(active):
    if not active:
        return 'location / { return 503; }\n'
    return '''location / {
    proxy_pass http://127.0.0.1:19192;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header X-Forwarded-Proto https;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
    proxy_buffering off;
    proxy_cookie_flags ~ secure httponly samesite=lax;
    add_header Cache-Control "no-store" always;
}
'''


def find_site(api):
    rows = [r for r in websites(api) if r.get('primaryDomain') == DOMAIN]
    if len(rows) > 1:
        raise RuntimeError('Ambiguous site')
    if rows and (rows[0].get('alias') != ALIAS or rows[0].get('type') != 'proxy'):
        raise RuntimeError('Existing site is not managed by this publisher')
    return rows[0] if rows else None


def proxy_record(api, ident):
    rows = api.request('POST', '/websites/proxies', {'id': ident}) or []
    if len(rows) != 1 or rows[0].get('match') != '/':
        raise RuntimeError('Expected one root proxy; no changes applied')
    row = rows[0]
    if not re.fullmatch(r'[A-Za-z0-9_-]+', row.get('name', '')):
        raise RuntimeError('Unexpected proxy name')
    return row


def configure_proxy(api, site, active):
    row = proxy_record(api, site['id'])
    api.request('POST', '/websites/proxies/file', {
        'websiteID': site['id'], 'name': row['name'], 'content': proxy_content(active)})


def deploy(api, stage, apply, account, expected_ip):
    site = find_site(api)
    certs = [r for r in certificates(api) if r.get('primaryDomain') == DOMAIN]
    if len(certs) > 1:
        raise RuntimeError('Ambiguous certificate; select/reconcile manually')
    if not apply:
        return {'plan': True, 'domain': DOMAIN, 'stage': stage,
                'site_exists': bool(site), 'certificate_exists': bool(certs),
                'public_listener': 'existing 443 only', 'direct_panel_port_opened': False}
    if stage == 'rollback':
        if not site:
            raise RuntimeError('No owned site to quarantine')
        configure_proxy(api, site, False)
        return {'quarantined': True, 'website_id': site['id'], 'data_deleted': False}
    if not expected_ip or set(socket.gethostbyname_ex(DOMAIN)[2]) != {expected_ip}:
        raise RuntimeError('DNS does not match explicitly expected public IPv4')
    if stage == 'prepare':
        if not site:
            groups = api.request('GET', '/websites/groups') or []
            default = [g for g in groups if g.get('isDefault')]
            if len(default) != 1:
                raise RuntimeError('Expected exactly one default website group')
            api.request('POST', '/websites', {'domains': [{'domain': DOMAIN, 'port': 80, 'ssl': False}],
                        'alias': ALIAS, 'type': 'proxy', 'webSiteGroupID': default[0]['id'],
                        'proxy': SINK, 'appType': 'new'})
            site = find_site(api)
        if not certs:
            if not account:
                raise RuntimeError('Select an existing ACME account explicitly')
            api.request('POST', '/websites/ssl', {'primaryDomain': DOMAIN, 'otherDomains': '',
                        'provider': 'http', 'acmeAccountId': account, 'autoRenew': True,
                        'keyType': 'EC256', 'apply': True})
        return {'prepared': True, 'website_id': site['id'], 'panel_upstream_activated': False}
    if not site:
        raise RuntimeError('Run prepare first')
    deadline = time.monotonic() + 180
    while True:
        rows = [r for r in certificates(api) if r.get('primaryDomain') == DOMAIN]
        if len(rows) == 1 and rows[0].get('status') == 'ready':
            cert = rows[0]
            break
        if time.monotonic() > deadline:
            raise RuntimeError('Certificate not ready; panel upstream not activated')
        time.sleep(3)
    if cert.get('autoRenew') is not True:
        raise RuntimeError('Certificate auto-renew must be enabled before activation')
    https = {'websiteId': site['id'], 'enable': True, 'websiteSSLId': cert['id'],
             'type': 'existed', 'httpConfig': 'HTTPToHTTPS',
             'SSLProtocol': ['TLSv1.2', 'TLSv1.3'], 'algorithm': 'ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384',
             'hsts': True, 'hstsIncludeSubDomains': False, 'httpsPorts': [443], 'http3': False}
    api.request('POST', '/websites/' + str(site['id']) + '/https', https)
    current = api.request('GET', '/websites/' + str(site['id']) + '/https')
    if current.get('enable') is not True or current.get('httpConfig') != 'HTTPToHTTPS':
        raise RuntimeError('HTTPS redirect not confirmed; panel upstream not activated')
    configure_proxy(api, site, True)
    return {'activated': True, 'website_id': site['id'], 'certificate_id': cert['id'],
            'auto_renew': cert['autoRenew'], 'expires': cert.get('expireDate'),
            'direct_panel_port_opened': False, 'entrance_changed': False}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--stage', choices=['prepare', 'activate', 'rollback'], default='prepare')
    p.add_argument('--apply', action='store_true')
    p.add_argument('--expected-host')
    p.add_argument('--expected-ip')
    p.add_argument('--acme-account-id', type=int)
    p.add_argument('--key-file', required=True)
    a = p.parse_args()
    if a.apply and a.expected_host != socket.gethostname():
        raise RuntimeError('Exact expected hostname required')
    api = PanelAPI('http://127.0.0.1:19192', read_api_key(a.key_file), timeout=45)
    print(json.dumps(deploy(api, a.stage, a.apply, a.acme_account_id, a.expected_ip)))


if __name__ == '__main__':
    main()
