#!/usr/bin/env python3
"""Безопасный аудит HTTPS Origin ASA Lab без изменения конфигурации сервера."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit


class OriginAuditError(Exception):
    pass


def origin_of(raw: str) -> str:
    value = raw.strip()
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("not an HTTPS origin")
        port = parsed.port
    except ValueError as exc:
        raise OriginAuditError(f"Некорректный HTTPS origin: {value!r}") from exc
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    return f"https://{host}" + (f":{port}" if port and port != 443 else "")


def configured_origins(env_text: str) -> frozenset[str]:
    records = [
        line.split("=", 1)[1].strip()
        for line in env_text.splitlines()
        if re.match(r"^\s*ASA_PUBLIC_WEB_ORIGINS\s*=", line)
    ]
    if len(records) != 1:
        raise OriginAuditError("В .env должна быть ровно одна запись ASA_PUBLIC_WEB_ORIGINS")
    content = records[0].strip("'\"")
    values = [x.strip() for x in content.split(",") if x.strip()]
    if not values or len(values) > 8:
        raise OriginAuditError("ASA_PUBLIC_WEB_ORIGINS: требуется от 1 до 8 HTTPS-адресов")
    result = [origin_of(x) for x in values]
    if len(result) != len(set(result)):
        raise OriginAuditError("В ASA_PUBLIC_WEB_ORIGINS обнаружены повторяющиеся адреса")
    return frozenset(result)


def site_origins(api_post, upstream: str = "http://127.0.0.1:4610") -> frozenset[str]:
    found = set()
    page = 1
    while True:
        data = api_post(
            "/websites/search",
            {"page": page, "pageSize": 100, "order": "null", "orderBy": "created_at"},
        )
        items = data.get("items", [])
        if not isinstance(items, list):
            raise OriginAuditError("1Panel: неожиданный формат списка сайтов")
        for site in items:
            if site.get("type") != "proxy":
                continue
            entries = api_post("/websites/proxies", {"id": site["id"]})
            if not isinstance(entries, list):
                raise OriginAuditError("1Panel: неожиданный формат списка прокси")
            if not any(
                p.get("enable") is True
                and p.get("match") == "/"
                and str(p.get("proxyPass", "")).rstrip("/") == upstream.rstrip("/")
                for p in entries
            ):
                continue
            if str(site.get("protocol", "")).upper() != "HTTPS":
                raise OriginAuditError(f"1Panel: сайт {site.get('primaryDomain')} не использует HTTPS")
            domain = str(site.get("primaryDomain", "")).strip()
            if not domain:
                raise OriginAuditError("1Panel: пустой primaryDomain")
            found.add(origin_of(f"https://{domain}"))
        if page * 100 >= int(data.get("total", len(items))) or not items:
            break
        page += 1
        if page > 100:
            raise OriginAuditError("Слишком много страниц сайтов 1Panel")
    return frozenset(found)


def make_panel_api(base: str, api_key: str):
    root = base.rstrip("/")
    if urlsplit(root).hostname not in ("localhost", "127.0.0.1", "192.168.1.115"):
        raise OriginAuditError("API 1Panel разрешён только через известный локальный адрес")
    def send(path: str, data: dict):
        stamp = str(int(time.time()))
        signed = hashlib.md5(
            ("1panel" + api_key + stamp).encode("utf-8"),
            usedforsecurity=False,
        ).hexdigest()
        request = urllib.request.Request(
            root + "/api/v2" + path,
            data=json.dumps(data).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "1Panel-Token": signed,
                "1Panel-Timestamp": stamp,
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as result:
            message = json.load(result)
        if message.get("code") != 200:
            raise OriginAuditError(f"1Panel API вернул code={message.get('code')}")
        return message.get("data")
    return send


def check_configured(required: set[str] | frozenset[str], configured: frozenset[str]) -> list[str]:
    return sorted(required - configured)


def main() -> int:
    parser = argparse.ArgumentParser(description="Проверить доверенные HTTPS-домены ASA Lab")
    parser.add_argument("--env-file", type=Path, default=Path("/srv/asa-lab/.env"))
    parser.add_argument("--domain", action="append", default=[], help="Домен, проверяемый перед публикацией")
    parser.add_argument("--discover-1panel", action="store_true", help="Проверить все HTTPS-сайты, ведущие на ASA Lab")
    parser.add_argument("--panel-url", default="http://127.0.0.1:19192")
    parser.add_argument("--api-key-file", type=Path, help="Локальный файл ключа API 1Panel с правами 0600")
    args = parser.parse_args()
    try:
        actual = configured_origins(args.env_file.read_text(encoding="utf-8"))
        required = {origin_of("https://" + name) for name in args.domain}
        if args.discover_1panel:
            if args.api_key_file is None:
                raise OriginAuditError("--discover-1panel требует --api-key-file")
            mode = stat.S_IMODE(args.api_key_file.stat().st_mode)
            if mode & 0o077:
                raise OriginAuditError("Файл API-ключа доступен группе/остальным: требуется 0600")
            token = args.api_key_file.read_text(encoding="utf-8").strip()
            if not re.fullmatch(r"[0-9a-fA-F]{32}", token):
                raise OriginAuditError("Неверный формат API-ключа 1Panel")
            required.update(site_origins(make_panel_api(args.panel_url, token)))
        if not required:
            raise OriginAuditError("Укажите --domain или --discover-1panel")
        missing = check_configured(required, actual)
        if missing:
            for item in missing:
                print(f"FAIL: отсутствует доверенный origin: {item}")
            print("Сначала добавьте точный HTTPS origin в ASA_PUBLIC_WEB_ORIGINS, затем")
            print("выборочно пересоздайте только API с закреплённым release-образом.")
            print("Не отключайте CSRF и не доверяйте произвольным Host-заголовкам.")
            return 2
        for item in sorted(required):
            print(f"PASS: {item} есть в ASA_PUBLIC_WEB_ORIGINS")
        return 0
    except (OSError, OriginAuditError, json.JSONDecodeError) as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
