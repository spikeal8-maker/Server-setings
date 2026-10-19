#!/usr/bin/env python3
"""Официальный API 1Panel v2: чтение, осторожная установка OpenResty и создание proxy-сайта."""
from __future__ import annotations
import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


class PanelError(RuntimeError):
    pass


def read_api_key(path: str | None) -> str:
    if path:
        p = Path(path)
        if not p.is_file():
            raise PanelError(f"Файл ключа не найден: {p}")
        if os.name != "nt" and stat.S_IMODE(p.stat().st_mode) & 0o077:
            raise PanelError("Файл API-ключа должен быть доступен только владельцу: chmod 600")
        value = p.read_text(encoding="utf-8").strip()
    else:
        value = os.getenv("PANEL_API_KEY", "").strip()
    if not re.fullmatch(r"[0-9a-fA-F]{32}", value):
        raise PanelError("Не указан API-ключ (32 шестнадцатеричных символа): --key-file")
    return value


class PanelAPI:
    def __init__(self, host: str, key: str, timeout: int = 15):
        parsed = urllib.parse.urlsplit(host)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.path not in ("", "/"):
            raise PanelError("Для 1Panel укажите только базовый адрес http(s)://host:port")
        if parsed.scheme == "http":
            if self._ip(parsed.hostname):
                address = ipaddress.ip_address(parsed.hostname)
                secure_network = address.is_private or address in ipaddress.ip_network("100.64.0.0/10")
            else:
                secure_network = parsed.hostname == "localhost"
            if not secure_network:
                raise PanelError("Администрирование по HTTP разрешено только в LAN/Tailscale")
        self.host = host.rstrip("/")
        self.key = key
        self.timeout = timeout

    @staticmethod
    def _ip(value: str) -> bool:
        try: ipaddress.ip_address(value); return True
        except ValueError: return False

    def request(self, method: str, endpoint: str, payload=None):
        if not endpoint.startswith("/") or endpoint.startswith("//"):
            raise PanelError("Неожиданный путь API")
        stamp = str(int(time.time()))
        token = hashlib.md5(("1panel" + self.key + stamp).encode("utf-8")).hexdigest()
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(self.host + "/api/v2" + endpoint, data=body, method=method.upper(),
                                     headers={"1Panel-Token": token, "1Panel-Timestamp": stamp,
                                              "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as reply:
                raw = reply.read(2_000_000)
        except urllib.error.HTTPError as exc:
            raise PanelError(f"1Panel API HTTP {exc.code}, путь {endpoint}") from None
        except urllib.error.URLError as exc:
            raise PanelError(f"API недоступен ({type(exc.reason).__name__})") from None
        try:
            response = json.loads(raw)
        except (UnicodeError, json.JSONDecodeError):
            raise PanelError("1Panel вернул не-JSON") from None
        if response.get("code") not in (0, 200):
            raise PanelError(f"1Panel API ошибка {response.get('code')}: {str(response.get('message', ''))[:200]}")
        return response.get("data")


def websites(api: PanelAPI):
    data = api.request("POST", "/websites/search", {"page": 1, "pageSize": 100, "order": "null", "orderBy": "created_at"}) or {}
    if int(data.get("total", 0)) > len(data.get("items") or []):
        raise PanelError("Сайтов больше 100 — чтение неполное, операция остановлена")
    return data.get("items") or []


def certificates(api: PanelAPI):
    data = api.request("POST", "/websites/ssl/search", {"page": 1, "pageSize": 100}) or {}
    if int(data.get("total", 0)) > len(data.get("items") or []):
        raise PanelError("Сертификатов больше 100 — чтение неполное")
    return data.get("items") or []


def inspect(api: PanelAPI):
    site_data = websites(api)
    ssl_data = certificates(api)
    return {
        "сайты": [{"адрес": x.get("primaryDomain"), "тип": x.get("type"), "статус": x.get("status"), "псевдоним": x.get("alias")} for x in site_data],
        "сертификаты": [{"адрес": x.get("primaryDomain"), "автообновление": x.get("autoRenew"), "истекает": x.get("expireDate")} for x in ssl_data],
        "секрет_не_выводится": True,
    }


def openresty_install(api: PanelAPI, apply: bool, desired: str):
    if os.name == "posix":
        import subprocess
        res = subprocess.run(["ss", "-lntp"], check=False, text=True, capture_output=True)
        ports = (re.search(r"(?m):80\s", res.stdout), re.search(r"(?m):443\s", res.stdout))
        if any(ports):
            if all(ports) and "openresty" in res.stdout:
                print("OpenResty уже занимает 80/443; повторную установку не выполняю")
                return
            raise PanelError("На 80 или 443 находится другой процесс. Отказываюсь менять действующую схему")
    appdata = api.request("GET", "/apps/openresty")
    if not appdata or not appdata.get("id"):
        raise PanelError("1Panel App Store не вернул OpenResty. Проверьте локальный магазин и интернет")
    versions = appdata.get("versions") or []
    if desired not in versions:
        raise PanelError("Точная версия OpenResty отсутствует в магазине: " + desired + "; доступны: " + ", ".join(versions))
    details = api.request("GET", f"/apps/detail/{int(appdata['id'])}/{urllib.parse.quote(desired)}/app")
    if not details or not details.get("id"):
        raise PanelError("1Panel не вернул appDetailId для закреплённой версии")
    print("Проверено: OpenResty", desired, "; appDetailId", details["id"])
    if not apply:
        print("РЕЖИМ ПРОСМОТРА: установка не начата (используйте --apply)")
        return
    if os.name == "posix":
        import subprocess
        res = subprocess.run(["ss", "-lnt"], check=False, text=True, capture_output=True)
        if re.search(r"(?m):(?:80|443)\s", res.stdout):
            raise PanelError("Порт 80 либо 443 уже занят. Остановлено, существующий сайт не трогаем")
    payload = {"appDetailId": int(details["id"]), "name": "openresty", "params": {
        "PANEL_APP_PORT_HTTP": 80, "PANEL_APP_PORT_HTTPS": 443}}
    api.request("POST", "/apps/install", payload)
    print("Команда установки OpenResty принята 1Panel. Проверьте контейнер и приложение в панели")


def validate_site(host: str, upstream: str, alias: str):
    if not re.fullmatch(r"[a-zA-Z0-9.-]+", host) or host.startswith("-") or ".." in host or len(host) > 250:
        raise PanelError("Некорректный DNS/IP адрес сайта")
    if not re.fullmatch(r"[a-z0-9-]{3,48}", alias):
        raise PanelError("Некорректный псевдоним")
    parsed = urllib.parse.urlsplit(upstream)
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.port is None
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment or parsed.username):
        raise PanelError("Прокси допускает только http://127.0.0.1:PORT")
    if not 1 <= parsed.port <= 65535:
        raise PanelError("Некорректный порт upstream")


def create_site(api: PanelAPI, host: str, upstream: str, alias: str, apply: bool):
    validate_site(host, upstream, alias)
    existing = [x for x in websites(api) if x.get("primaryDomain") == host or x.get("alias") == alias]
    if existing:
        print("Сайт уже существует в 1Panel; автоматическая перезапись запрещена:",
              json.dumps([{"адрес": x.get("primaryDomain"), "тип": x.get("type"), "псевдоним": x.get("alias")} for x in existing], ensure_ascii=False))
        return
    groups = api.request("POST", "/groups/search", {"type": "website"}) or []
    defaults = [x for x in groups if x.get("isDefault")]
    if len(defaults) != 1:
        raise PanelError("Не найдена ровно одна группа сайтов по умолчанию")
    payload = {"domains": [{"domain": host, "port": 80, "ssl": False}], "alias": alias,
               "type": "proxy", "websiteGroupId": int(defaults[0]["id"]),
               "proxy": upstream, "appType": "new"}
    print("ПЛАН proxy-сайта:", json.dumps(payload, ensure_ascii=False))
    if not apply:
        print("Создание отключено: добавьте --apply. HTTPS настраивается отдельно через сертификат 1Panel")
        return
    if os.name == "posix":
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
            if connection.connect_ex(("127.0.0.1", int(urllib.parse.urlsplit(upstream).port))) != 0:
                raise PanelError("Сервис upstream недоступен — не создаю сломанный сайт")
    api.request("POST", "/websites", payload)
    if not any(x.get("primaryDomain") == host for x in websites(api)):
        raise PanelError("API ответил успешно, но новый сайт не появился в списке")
    print("Сайт создан в 1Panel, HTTP. Настройте и проверьте SSL в панели до публикации")


def main():
    p = argparse.ArgumentParser(description="Управление 1Panel через официальный API v2")
    p.add_argument("action", choices=("inspect", "install-openresty", "create-site"))
    p.add_argument("--host", default="http://127.0.0.1:19192")
    p.add_argument("--key-file", default="/var/lib/server-settings/1panel-api.key")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--openresty-version", default="1.27.1.2-5-1-focal")
    p.add_argument("--site-host", default="94.29.44.211")
    p.add_argument("--site-upstream", default="http://127.0.0.1:4610")
    p.add_argument("--site-alias", default="asa-lab-public-ip")
    args = p.parse_args()
    key = read_api_key(args.key_file)
    api = PanelAPI(args.host, key)
    if args.action == "inspect":
        print(json.dumps(inspect(api), ensure_ascii=False, indent=2))
    elif args.action == "install-openresty":
        openresty_install(api, args.apply, args.openresty_version)
    elif args.action == "create-site":
        create_site(api, args.site_host, args.site_upstream, args.site_alias, args.apply)


if __name__ == "__main__":
    try: main()
    except (PanelError, ValueError, OSError) as exc:
        print("ОШИБКА:", str(exc), file=sys.stderr)
        sys.exit(1)
