#!/usr/bin/env python3
"""Independent Yandex Disk OAuth enrollment on the ASA Lab school VM100.

Run from the VM100 host terminal. The owner registers/authorizes an API app
directly at Yandex, then enters the code and credentials locally, never in
ChatGPT, GitHub, command-line arguments, Desktop Commander, or another host.

Retains the existing cloud_storage.py/rclone deployment and validates file
access before marking enrollment successful. Does not configure schedules,
access PostgreSQL/MinIO, or transfer backup data.
"""
from __future__ import annotations

import argparse
import base64
import configparser
from datetime import datetime, timedelta, timezone
import fcntl
import getpass
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent
VM_HOST = "asa-school-01"
CLOUD_CONFIG = Path("/srv/asa-cloud-storage/config/rclone.conf")
REDIRECT_URI = "https://oauth.yandex.ru/verification_code"
CREATE_APP_URL = "https://oauth.yandex.ru/client/new/"
TOKEN_URL = "https://oauth.yandex.ru/token"
TIMEOUT = 24


class EnrollmentError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def load_peer(filename: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    if spec is None or spec.loader is None:
        raise EnrollmentError("REPOSITORY_MODULE_NOT_FOUND")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def verify_host(expected_host: str) -> None:
    if (expected_host != VM_HOST or socket.gethostname() != VM_HOST
            or os.geteuid() != 0):
        raise EnrollmentError("ROOT_ON_SCHOOL_VM100_REQUIRED")


def build_authorize_url(client_id: str, verifier: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{10,256}", client_id):
        raise EnrollmentError("CLIENT_ID_INVALID")
    if not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", verifier):
        raise EnrollmentError("PKCE_VERIFIER_INVALID")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    query = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT_URI,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "force_confirm": "yes",
    })
    return "https://oauth.yandex.ru/authorize?" + query


def normalize_code(value: str) -> str:
    value = value.strip()
    if value.startswith("https://"):
        url = urllib.parse.urlsplit(value)
        if url.scheme != "https" or url.hostname != "oauth.yandex.ru" or url.path != "/verification_code":
            raise EnrollmentError("UNEXPECTED_REDIRECT_URL")
        query = urllib.parse.parse_qs(url.query, keep_blank_values=True)
        codes = query.get("code", [])
        if len(codes) != 1:
            raise EnrollmentError("OAUTH_CODE_MISSING_IN_URL")
        value = codes[0]
    if not re.fullmatch(r"[A-Za-z0-9_.~-]{4,256}", value):
        raise EnrollmentError("OAUTH_CODE_INVALID")
    return value


def safe_token_response(response: dict, now: datetime | None = None) -> dict:
    if not isinstance(response, dict):
        raise EnrollmentError("YANDEX_TOKEN_RESPONSE_INVALID")
    token = response.get("access_token")
    refresh = response.get("refresh_token")
    if not isinstance(token, str) or len(token) < 20:
        raise EnrollmentError("YANDEX_ACCESS_TOKEN_MISSING")
    if not isinstance(refresh, str) or len(refresh) < 20:
        # A temporary access-only token is not acceptable for unattended backups.
        raise EnrollmentError("YANDEX_REFRESH_TOKEN_REQUIRED")
    seconds = response.get("expires_in")
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 120 <= seconds <= 315360000:
        raise EnrollmentError("YANDEX_EXPIRY_INVALID")
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        raise EnrollmentError("CLOCK_NOT_TIMEZONE_AWARE")
    expiry = clock.astimezone(timezone.utc) + timedelta(seconds=int(seconds)-60)
    return {
        "access_token": token,
        "token_type": "OAuth",
        "refresh_token": refresh,
        "expiry": expiry.isoformat().replace("+00:00", "Z"),
    }


def exchange_code(client_id: str, client_secret: str, code: str, verifier: str) -> dict:
    body = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": normalize_code(code),
        "client_id": client_id,
        "client_secret": client_secret,
        "code_verifier": verifier,
    }).encode("utf-8")
    request = urllib.request.Request(
        TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            if response.status != 200:
                raise EnrollmentError("YANDEX_TOKEN_REQUEST_REJECTED")
            data = response.read(32769)
    except urllib.error.HTTPError as exc:
        # Do not print provider replies: they may include sensitive details.
        raise EnrollmentError("YANDEX_TOKEN_HTTP_" + str(exc.code)) from None
    except (urllib.error.URLError, TimeoutError):
        raise EnrollmentError("YANDEX_OAUTH_NETWORK_ERROR") from None
    if len(data) > 32768:
        raise EnrollmentError("YANDEX_TOKEN_REPLY_TOO_LARGE")
    try:
        decoded = json.loads(data)
    except (ValueError, UnicodeError):
        raise EnrollmentError("YANDEX_TOKEN_REPLY_NOT_JSON") from None
    return safe_token_response(decoded)


def read_existing_custom_app() -> tuple[str, str]:
    """Read app registration on VM100, not from the test server or D2-R2."""
    config = configparser.ConfigParser(interpolation=None)
    try:
        with CLOUD_CONFIG.open("r", encoding="utf-8") as source:
            config.read_file(source)
        if config.get("yandex", "type") != "yandex":
            raise EnrollmentError("YANDEX_CONFIG_TYPE_INVALID")
        client_id = config.get("yandex", "client_id")
        client_secret = config.get("yandex", "client_secret")
        token = config.get("yandex", "token")
        if not token or not client_id or not client_secret:
            raise EnrollmentError("EXISTING_CUSTOM_APP_MISSING")
        return client_id, client_secret
    except (OSError, configparser.Error, KeyError, ValueError):
        raise EnrollmentError("EXISTING_CUSTOM_APP_NOT_READABLE") from None


def apply_verified_oauth(token: dict, client_id: str, client_secret: str,
                         *, allow_custom_reauthorization: bool = False) -> None:
    repair = load_peer("repair-school-yandex-oauth.py", "asa_yandex_repair")
    repair.check_identity()
    new_bytes = repair.build_config(token, client_id, client_secret)
    flags = os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW
    with os.fdopen(os.open(CLOUD_CONFIG, flags), "r+b", buffering=0) as destination:
        fcntl.flock(destination.fileno(), fcntl.LOCK_EX)
        old_bytes = destination.read()
        is_broken_standard = bool(old_bytes) and repair.is_failed_standard_config(old_bytes)
        if old_bytes and not is_broken_standard:
            if not allow_custom_reauthorization:
                raise EnrollmentError("EXISTING_CUSTOM_YANDEX_CONFIG_REQUIRES_REVIEW")
            current_id, current_secret = read_existing_custom_app()
            if client_id != current_id or client_secret != current_secret:
                raise EnrollmentError("EXISTING_APP_CREDENTIALS_MISMATCH")
        if is_broken_standard:
            was_ok, was_410 = repair.remote_probe()
            if was_ok or not was_410:
                raise EnrollmentError("OLD_OAUTH_NOT_CONFIRMED_BROKEN_410")
        try:
            repair.write_locked(destination, new_bytes)
            works, _ = repair.remote_probe()
            if not works:
                raise EnrollmentError("NEW_OAUTH_FILE_ACCESS_FAILED")
        except BaseException:
            repair.write_locked(destination, old_bytes)
            raise


def check_status() -> dict:
    cloud = load_peer("cloud_storage.py", "asa_yandex_cloud")
    return cloud.status()


def run_connect(expected_host: str, *, reauthorize: bool = False) -> int:
    verify_host(expected_host)
    before = check_status()
    if not before.get("installed"):
        raise EnrollmentError("CLOUD_STORAGE_NOT_INSTALLED")
    if reauthorize:
        if not before.get("token_configured"):
            raise EnrollmentError("NO_EXISTING_OAUTH_USE_CONNECT")
    else:
        if before.get("connected"):
            print("Уже подключено: Яндекс.Диск работает на VM 100.")
            return 0
        if before.get("token_configured") and before.get("provider_error") != "CLOUD_PROVIDER_GONE":
            raise EnrollmentError("EXISTING_OAUTH_DIFFERENT_ERROR_REQUIRES_REVIEW")

    print("АСА LAB / ЯНДЕКС ДИСК / ШКОЛЬНАЯ VM100")
    print("Это самостоятельная авторизация сервера, без D2-R2 или тестового стенда.")
    print("1. Зарегистрируйте приложение «Для доступа к API или отладки».")
    print("   Адрес регистрации:", CREATE_APP_URL)
    print("   Название: ASA Lab School Server Backups")
    print("   Права: cloud_api:disk.read, cloud_api:disk.write, cloud_api:disk.info")
    print("   Redirect URI:", REDIRECT_URI)
    print("2. В свойствах созданного приложения найдите Client ID и Client Secret.")
    if reauthorize:
        client_id, client_secret = read_existing_custom_app()
        print("Использую собственное OAuth-приложение, уже сохранённое на VM100.")
    else:
        client_id = getpass.getpass("Client ID (ввод скрыт): ").strip()
        client_secret = getpass.getpass("Client Secret (ввод скрыт): ").strip()
    # Use the same validation contract as the existing Server-setings repair tool.
    repair = load_peer("repair-school-yandex-oauth.py", "asa_yandex_repair_validator")
    repair.parse_payload(json.dumps({
        "client_id": client_id,
        "client_secret": client_secret,
        "token": {"access_token": "x" * 40, "token_type": "OAuth"},
        "repair_bad_standard": True,
    }).encode("utf-8"))
    verifier = secrets.token_urlsafe(40)
    url = build_authorize_url(client_id, verifier)
    print("3. Откройте в ЛЮБОМ браузере следующий официальный адрес Яндекса:")
    print(url)
    print("   Разрешите доступ, затем скопируйте КОД подтверждения с Яндекса.")
    print("   Никакого localhost/53682 и входа на тестовом сервере не требуется.")
    code = getpass.getpass("Код подтверждения Яндекса (ввод скрыт): ")
    oauth = exchange_code(client_id, client_secret, code, verifier)
    print("Яндекс выдал access+refresh token. Проверяю реальный доступ к ASA-Backups…")
    apply_verified_oauth(oauth, client_id, client_secret,
                         allow_custom_reauthorization=reauthorize)
    after = check_status()
    if not after.get("connected"):
        # This should not happen if the normal rclone listing succeeded.
        raise EnrollmentError("POST_ENROLLMENT_CONNECTION_NOT_CONFIRMED")
    print("YANDEX_VM100_CONNECTED=YES")
    print("FOLDER_ASA_BACKUPS_READ=PASS")
    print("TOKEN_STORED_ONLY_ON_VM100=YES")
    print("BACKUP_SCHEDULER_ENABLED=NO")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["status", "connect", "reauthorize"])
    parser.add_argument("--expected-host", default="")
    args = parser.parse_args(argv)
    try:
        if args.action == "status":
            verify_host(args.expected_host)
            s = check_status()
            print(json.dumps({
                "host": VM_HOST,
                "installed": s.get("installed", False),
                "oauth_configured": s.get("token_configured", False),
                "connected": s.get("connected", False),
                "backup_schedule_enabled": s.get("backup_schedule_enabled", False),
                "provider_error": s.get("provider_error"),
            }, ensure_ascii=False))
            return 0 if s.get("connected") else 2
        return run_connect(args.expected_host, reauthorize=(args.action == "reauthorize"))
    except EnrollmentError as exc:
        print("YANDEX_SETUP_BLOCKED=" + exc.code, file=sys.stderr)
    except KeyboardInterrupt:
        print("YANDEX_SETUP_CANCELLED", file=sys.stderr)
    except (OSError, ValueError, EOFError):
        print("YANDEX_SETUP_FAILED_SAFE", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
