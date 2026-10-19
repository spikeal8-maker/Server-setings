#!/usr/bin/env python3
"""Воспроизводимая публикация домена через API 1Panel. Без --apply только план.

Цель: отдельный SNI-vhost/OpenResty к уже существующей ASA Lab.
Не меняет DNS, .env ASA Lab, UFW и контейнеры; удаление/откат только вручную.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import socket
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from panelctl import PanelAPI, PanelError, read_api_key, require_local_write, websites, certificates


class DomainError(PanelError):
    pass


def canonical_domain(value: str) -> str:
    name = value.lower().strip().rstrip(".")
    try:
        name = name.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise DomainError("Не удаётся преобразовать домен в IDNA") from exc
    if len(name) > 253 or len(name.split(".")) < 2 or not re.fullmatch(r"[a-z0-9.-]+", name):
        raise DomainError("Требуется корректный публичный домен с точкой, без схемы и порта")
    if any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-") for label in name.split(".")):
        raise DomainError("Недопустимая метка домена")
    if name.endswith((".local", ".localhost", ".test", ".invalid", ".example")):
        raise DomainError("Тестовые или локальные имена не могут получить публичный ACME-сертификат")
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return name
    raise DomainError("Для IP и DNS-имени используются отдельные сертификаты и сайты")


def expected_public_ipv4(value: str) -> str:
    try:
        ip = ipaddress.IPv4Address(value)
    except ipaddress.AddressValueError as exc:
        raise DomainError("Укажите публичный IPv4 целевого сервера") from exc
    if not ip.is_global:
        raise DomainError("Указан не публичный IPv4; при CGNAT прямой HTTP-01 не заработает")
    return str(ip)


def dns_addresses(name: str) -> tuple[set[str], set[str]]:
    v4: set[str] = set()
    v6: set[str] = set()
    try:
        infos = socket.getaddrinfo(name, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise DomainError(f"DNS не разрешает {name}: {exc}") from exc
    for family, _, _, _, sockaddr in infos:
        if family == socket.AF_INET:
            v4.add(sockaddr[0])
        if family == socket.AF_INET6:
            v6.add(sockaddr[0])
    return v4, v6


def require_dns_ready(name: str, address: str, expected_ipv6: str | None = None) -> dict:
    ipv4 = expected_public_ipv4(address)
    v4, v6 = dns_addresses(name)
    if v4 != {ipv4}:
        raise DomainError(f"DNS A для {name}: {sorted(v4)} вместо единственного {ipv4}")
    if expected_ipv6:
        try:
            ip6 = str(ipaddress.IPv6Address(expected_ipv6))
        except ipaddress.AddressValueError as exc:
            raise DomainError("Некорректный ожидаемый IPv6") from exc
        if v6 != {ip6}:
            raise DomainError(f"DNS AAAA должен указывать только на {ip6}; найдено: {sorted(v6)}")
    elif v6:
        raise DomainError("У домена есть AAAA, но IPv6 вход на этот сервер не подтверждён; возможен сбой ACME")
    return {"A": sorted(v4), "AAAA": sorted(v6)}


def validate_upstream(port: int) -> str:
    if not 1 <= port <= 65535:
        raise DomainError("Порт backend должен быть в диапазоне 1–65535")
    return f"http://127.0.0.1:{port}"


def validate_alias(name: str, alias: str | None = None) -> str:
    if alias is None:
        alias = "site-" + name.replace(".", "-")
    if not re.fullmatch(r"[a-z0-9-]{3,48}", alias) or alias.startswith("-") or alias.endswith("-"):
        raise DomainError("Псевдоним 1Panel содержит недопустимые символы или превышает 48 символов")
    return alias


def acme_account(api: PanelAPI) -> dict:
    resp = api.request("POST", "/websites/acme/search", {"page": 1, "pageSize": 100}) or {}
    items = resp.get("items") or []
    if int(resp.get("total", len(items))) > len(items):
        raise DomainError("Неполный список ACME-аккаунтов: прервана выдача сертификата")
    letsencrypt = [i for i in items if str(i.get("type", "")).lower() == "letsencrypt"]
    if len(letsencrypt) != 1:
        raise DomainError("Ожидается ровно одна учётная запись Let's Encrypt в 1Panel; проверьте раздел ACME")
    return letsencrypt[0]


def website_group(api: PanelAPI) -> int:
    groups = api.request("POST", "/groups/search", {"type": "website"}) or []
    group_ids = [int(g["id"]) for g in groups if g.get("isDefault")]
    if len(group_ids) != 1:
        raise DomainError("Не найдена одна группа сайтов 1Panel по умолчанию")
    return group_ids[0]


def matching_site(api: PanelAPI, domain: str, alias: str) -> dict | None:
    all_sites = websites(api)
    collision = [s for s in all_sites if s.get("primaryDomain") == domain or s.get("alias") == alias]
    if not collision:
        return None
    if len(collision) != 1 or collision[0].get("primaryDomain") != domain or collision[0].get("alias") != alias:
        raise DomainError("Конфликт имени сайта или псевдонима: прекращаю установку")
    return collision[0]


def matching_certificate(api: PanelAPI, domain: str) -> dict | None:
    matching = [c for c in certificates(api) if c.get("primaryDomain") == domain]
    if len(matching) > 1:
        raise DomainError("Обнаружено несколько сертификатов домена; автоматический выбор запрещён")
    return matching[0] if matching else None


def upstream_ready(port: int) -> dict:
    url = f"http://127.0.0.1:{port}/health/ready"
    req = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status != 200:
                raise DomainError(f"Backend вернул HTTP {resp.status}")
            data = json.loads(resp.read(4096))
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise DomainError("ASA Lab backend недоступен или вернул ошибку /health/ready") from exc
    if data.get("status") != "ready" or data.get("dependencies", {}).get("database") != "up":
        raise DomainError("Backend не готов или PostgreSQL недоступен")
    return {"status": "ready", "database": "up", "revision": data.get("deployment", {}).get("revision")}


def ssl_id(api: PanelAPI, domain: str, account_id: int, apply: bool, *, is_ip: bool = False) -> int:
    found = matching_certificate(api, domain)
    if found:
        if found.get("status", "").lower() == "ready" and found.get("autoRenew") is True:
            return int(found["id"])
        if not apply:
            raise DomainError("Существующий сертификат не готов или autoRenew выключено")
        raise DomainError("Существующий сертификат требует отдельного ремонта; автоматическое перевыпускание запрещено")
    if not apply:
        return 0
    issued = api.request("POST", "/websites/ssl", {
        "primaryDomain": domain, "otherDomains": "", "provider": "http",
        "acmeAccountId": account_id, "keyType": "P256", "autoRenew": True,
        "apply": True, "isIp": is_ip,
    }) or {}
    if int(issued.get("id", 0)) < 1:
        raise DomainError("1Panel не вернула ID созданного SSL-сертификата")
    return int(issued["id"])


def wait_for_certificate(api: PanelAPI, domain: str, issued_id: int, max_wait: int = 180) -> None:
    deadline = time.monotonic() + max_wait
    while time.monotonic() <= deadline:
        found = matching_certificate(api, domain)
        if not found or int(found.get("id", 0)) != issued_id:
            raise DomainError("Сертификат изменился во время оформления")
        status = str(found.get("status", "")).lower()
        if status == "ready":
            if found.get("autoRenew") is not True:
                raise DomainError("Полученный сертификат не настроен на автопродление")
            return
        if status in {"error", "failed", "failure", "disabled"}:
            raise DomainError("Получение сертификата завершилось ошибкой; проверьте SSL-журнал 1Panel")
        time.sleep(5)
    raise DomainError("1Panel не завершила получение сертификата за отведённое время")


def attach_https(api: PanelAPI, site_id: int, certificate_id: int) -> None:
    existing = api.request("GET", f"/websites/{site_id}/https") or {}
    if existing.get("enable") and int(existing.get("websiteSSLId", 0)) == certificate_id:
        return
    if existing.get("enable"):
        raise DomainError("У сайта уже включён HTTPS с другим сертификатом — ручная проверка обязательна")
    api.request("POST", f"/websites/{site_id}/https", {
        "websiteId": site_id, "enable": True, "websiteSSLId": certificate_id,
        "type": "existed", "httpConfig": "HTTPToHTTPS",
        "SSLProtocol": ["TLSv1.2", "TLSv1.3"], "hsts": False,
        "httpsPorts": [443], "http3": False,
    })
    now = api.request("GET", f"/websites/{site_id}/https") or {}
    if not now.get("enable"):
        raise DomainError("1Panel не подтвердила HTTPS после применения")


def provision(api: PanelAPI, domain: str, public_ip: str, port: int, alias: str,
              *, apply: bool, expected_ipv6: str | None = None) -> dict:
    group = website_group(api)
    acme = acme_account(api)
    current = matching_site(api, domain, alias)
    existing_ssl = matching_certificate(api, domain)
    result = {
        "domain": domain, "expected_ipv4": public_ip, "dns": None,
        "alias": alias, "backend": validate_upstream(port),
        "site_id": current.get("id") if current else None,
        "certificate_id": existing_ssl.get("id") if existing_ssl else None,
        "acme_account_id": acme["id"],
        "needs_create_site": current is None,
        "needs_certificate": existing_ssl is None,
        "will_not_change": ["публичный IP-сайт 94.29.44.211", "DNS", "ASA Lab .env", "UFW", "Tailscale"],
    }
    if not apply:
        return result
    require_local_write(api)
    result["dns"] = require_dns_ready(domain, public_ip, expected_ipv6)
    result["backend_health"] = upstream_ready(port)
    if current is None:
        payload = {"domains": [{"domain": domain, "port": 80, "ssl": False}],
                   "alias": alias, "type": "proxy", "webSiteGroupID": group,
                   "proxy": validate_upstream(port), "appType": "new"}
        api.request("POST", "/websites", payload)
        current = matching_site(api, domain, alias)
        if not current:
            raise DomainError("1Panel не подтвердила создание сайта")
    else:
        # Без проверки точного upstream через API нельзя безопасно принимать чужой сайт.
        # Автоматизация не должна менять существующую конфигурацию и IP-сайт.
        raise DomainError("Сайт уже существует: проверяйте upstream и HTTPS через 1Panel вручную")
    site_id = int(current["id"])
    result["site_id"] = site_id
    cert_id = ssl_id(api, domain, int(acme["id"]), apply=True)
    result["certificate_id"] = cert_id
    wait_for_certificate(api, domain, cert_id)
    attach_https(api, site_id, cert_id)
    result["https"] = "configured_via_1panel"
    result["required_manual_e2e"] = "Проверить вход, CSRF origin, проекты и Scratch через новый домен"
    return result


def main() -> None:
    p = argparse.ArgumentParser(description="Добавление домена к ASA Lab через 1Panel/OpenResty")
    p.add_argument("action", choices=("plan", "apply", "status"))
    p.add_argument("--domain")
    p.add_argument("--public-ip")
    p.add_argument("--expected-ipv6")
    p.add_argument("--origin-prepared", action="store_true", help="Подтвердить, что ASA Lab разрешает HTTPS origin нового домена")
    p.add_argument("--backend-port", type=int, default=4610)
    p.add_argument("--site-alias")
    p.add_argument("--host", default="http://127.0.0.1:19192")
    p.add_argument("--key-file", default="/var/lib/server-settings/1panel-api.key")
    args = p.parse_args()
    if args.action == "status":
        api = PanelAPI(args.host, read_api_key(args.key_file))
        print(json.dumps({"sites": [{"name": x.get("primaryDomain"), "status": x.get("status"), "type": x.get("type")}
                                    for x in websites(api)],
                          "certificates": [{"name": x.get("primaryDomain"), "status": x.get("status"),
                                             "renew": x.get("autoRenew"), "expiry": x.get("expireDate")}
                                            for x in certificates(api)]}, ensure_ascii=False, indent=2))
        return
    if not args.domain or not args.public_ip:
        p.error("Для plan/apply нужны --domain и --public-ip")
    domain = canonical_domain(args.domain)
    ip = expected_public_ipv4(args.public_ip)
    alias = validate_alias(domain, args.site_alias)
    upstream = validate_upstream(args.backend_port)
    if args.action == "plan":
        print(json.dumps({"mode": "DRY_RUN", "domain": domain, "ip": ip, "upstream": upstream,
                          "alias": alias, "dns": "A должен указывать только на целевой IP, AAAA проверяется",
                          "https": "Let's Encrypt через 1Panel; автоматическое продление",
                          "next": "Запустить apply только на Ubuntu после проверки DNS/NAT и резервной копии"},
                         ensure_ascii=False, indent=2))
        return
    if not args.origin_prepared:
        raise DomainError("Домен ещё не подтверждён как допустимый origin в ASA Lab; требуется --origin-prepared")
    api = PanelAPI(args.host, read_api_key(args.key_file))
    require_local_write(api)
    result = provision(api, domain, ip, args.backend_port, alias,
                       apply=True, expected_ipv6=args.expected_ipv6)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (DomainError, PanelError, OSError, ValueError) as exc:
        print("ОТКАЗ:", str(exc), file=sys.stderr)
        sys.exit(2)
