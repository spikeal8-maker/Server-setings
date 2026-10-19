#!/usr/bin/env python3
"""Создаёт СТРОГО ОДИН IP-сайт с отдельным кратким IP-SSL через 1Panel.

Только для нового сервера, где ASA Lab и БД уже восстановлены. --apply обязателен.
На действующем сервере совпадающий готовый IP-сайт не переписывает.
"""
from __future__ import annotations
import argparse
import json
import sys
from domainctl import (
    DomainError, acme_account, attach_https, attached_certificate_id, expected_public_ipv4,
    matching_certificate, matching_site, require_dns_ready, ssl_id,
    upstream_ready, validate_alias, validate_upstream, wait_for_certificate,
    website_group,
)
from panelctl import PanelAPI, PanelError, read_api_key, require_local_write


def provision_ip(api: PanelAPI, ip: str, port: int, alias: str) -> dict:
    require_local_write(api)
    ip = expected_public_ipv4(ip)
    upstream_ready(port)
    found = matching_site(api, ip, alias)
    cert = matching_certificate(api, ip)
    if found:
        if found.get("type") != "proxy":
            raise DomainError("IP-сайт другого типа уже существует: отказ")
        if not cert or str(cert.get("status", "")).lower() != "ready" or cert.get("autoRenew") is not True:
            raise DomainError("IP-сайт существует, но сертификат/автопродление не готов: ручной аудит без перезаписи")
        existing = api.request("GET", f"/websites/{int(found['id'])}/https") or {}
        if not existing.get("enable"):
            raise DomainError("HTTPS IP-сайта выключен: остановка без вмешательства")
        attached = attached_certificate_id(existing)
        if attached is None or attached != int(cert["id"]):
            raise DomainError("Не подтверждена привязка ожидаемого SSL.id к HTTPS IP-сайта")
        return {"status": "existing_site_not_modified", "ip": ip, "site_id": found["id"],
                "ssl_id": cert["id"], "autoRenew": True, "unchanged": True,
                "needs_backend_and_browser_verification": True}
    if cert:
        raise DomainError("Найдена отдельная запись SSL без сайта; автоматическое переиспользование запрещено")
    group = website_group(api)
    le = acme_account(api)
    api.request("POST", "/websites", {
        "domains": [{"domain": ip, "port": 80, "ssl": False}],
        "alias": alias, "type": "proxy", "webSiteGroupID": group,
        "proxy": validate_upstream(port), "appType": "new",
    })
    found = matching_site(api, ip, alias)
    if not found:
        raise DomainError("1Panel не подтвердила создание сайта для IPv4")
    site_id = int(found["id"])
    cert_id = ssl_id(api, ip, int(le["id"]), apply=True, is_ip=True)
    wait_for_certificate(api, ip, cert_id)
    attach_https(api, site_id, cert_id)
    return {"status": "configured_ip_https", "site_id": site_id, "ssl_id": cert_id,
            "ip": ip, "autoRenew": True, "required_external_proof": True,
            "note": "IP SSL короткоживущий: отдельно проверить реальное следующее продление"}


def main() -> None:
    p = argparse.ArgumentParser(description="Публичный IP-сайт ASA Lab в 1Panel")
    p.add_argument("action", choices=("plan", "apply"))
    p.add_argument("--public-ip", required=True)
    p.add_argument("--upstream-port", type=int, default=4610)
    p.add_argument("--alias", default="asa-lab-public-ip")
    p.add_argument("--origin-prepared", action="store_true")
    p.add_argument("--host", default="http://127.0.0.1:19192")
    p.add_argument("--key-file", default="/var/lib/server-settings/1panel-api.key")
    args = p.parse_args()
    ip = expected_public_ipv4(args.public_ip)
    alias = validate_alias(ip, args.alias)
    upstream = validate_upstream(args.upstream_port)
    if args.action == "plan":
        print(json.dumps({"mode":"DRY_RUN","ip":ip,"alias":alias,"proxy":upstream,
            "acme":"HTTP-01 / Let's Encrypt / isIp=true / autoRenew=true",
            "status":"На уже созданном IP-сайте изменения запрещены без проверки"},ensure_ascii=False,indent=2))
        return
    if not args.origin_prepared:
        raise DomainError("Не подтверждены разрешённые origin IP в ASA Lab; требуется --origin-prepared")
    api = PanelAPI(args.host, read_api_key(args.key_file))
    result = provision_ip(api, ip, args.upstream_port, alias)
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__ == "__main__":
    try: main()
    except (DomainError, PanelError, OSError, ValueError) as exc:
        print("ОТКАЗ:", str(exc), file=sys.stderr)
        sys.exit(2)
