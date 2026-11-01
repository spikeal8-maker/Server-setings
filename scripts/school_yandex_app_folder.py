#!/usr/bin/env python3
"""Yandex Disk app-folder transport for encrypted school VM100 backups.

Uses the existing VM100 OAuth token with cloud_api:disk.app_folder. No
full-disk write permission, no rclone upgrade or changes to its Docker image,
and no third-party computer after installation. Secrets stay in the root-only
/uid1000 rclone.conf. Remote files are immutable: overwrite is always false.
"""
from __future__ import annotations

import configparser
import hashlib
import http.client
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import ssl
import stat
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

HOST = "asa-school-01"
CONFIG = Path("/srv/asa-cloud-storage/config/rclone.conf")
API_BASE = "https://cloud-api.yandex.net/v1/disk"
ROOT = "app:/School-VM100/Database-Backups"
SOURCE_MODULE = Path(__file__).with_name("cloud_storage.py")
ALLOWED_NAME = re.compile(r"asa-school-01-[0-9]{8}T[0-9]{6}Z-[a-f0-9]{8}[.]tar[.]age\Z")
AGE_HEADER = b"age-encryption.org/v1"


class BackupTransportError(Exception):
    pass


def validate_archive_name(name: str) -> str:
    if not isinstance(name, str) or ALLOWED_NAME.fullmatch(name) is None:
        raise BackupTransportError("ARCHIVE_NAME_REJECTED")
    return name


def validate_signed_link(href: str) -> tuple[str, int, str]:
    parsed = urllib.parse.urlsplit(href)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or not host.endswith((".yandex.net", ".yandex.ru", ".yandex.com")):
        raise BackupTransportError("PROVIDER_SIGNED_URL_UNTRUSTED")
    if parsed.username or parsed.password or parsed.fragment:
        raise BackupTransportError("PROVIDER_SIGNED_URL_MALFORMED")
    try:
        port = parsed.port or 443
    except ValueError:
        raise BackupTransportError("PROVIDER_SIGNED_URL_PORT_INVALID") from None
    if port != 443 or not parsed.path.startswith("/"):
        raise BackupTransportError("PROVIDER_SIGNED_URL_PORT_OR_PATH")
    return host, port, parsed.path + ("?" + parsed.query if parsed.query else "")


def file_hash(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(2 * 1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def private_cipher(path: Path) -> tuple[int, str]:
    if path.is_symlink() or not path.is_file():
        raise BackupTransportError("CIPHER_FILE_NOT_REGULAR")
    meta = path.stat()
    if meta.st_uid != 0 or stat.S_IMODE(meta.st_mode) != 0o600:
        raise BackupTransportError("CIPHER_OWNER_MODE_UNTRUSTED")
    if not 128 <= meta.st_size <= 100 * 1024**3:
        raise BackupTransportError("CIPHER_SIZE_INVALID")
    with path.open("rb") as f:
        if not f.read(30).startswith(AGE_HEADER):
            raise BackupTransportError("NOT_AGE_CIPHERTEXT")
    return meta.st_size, file_hash(path)


def check_host() -> None:
    if os.geteuid() != 0 or socket.gethostname() != HOST:
        raise BackupTransportError("VM100_ROOT_REQUIRED")


def fresh_token() -> str:
    # Existing pinned rclone container refreshes the same token file without
    # changing its backend or OAuth account. Only read permission is exercised.
    spec = importlib.util.spec_from_file_location("school_cloud_read", SOURCE_MODULE)
    if spec is None or spec.loader is None:
        raise BackupTransportError("CLOUD_MODULE_MISSING")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    response = module.status()
    if response.get("connected") is not True:
        raise BackupTransportError("YANDEX_READ_TOKEN_NOT_READY")
    if CONFIG.is_symlink() or not CONFIG.is_file():
        raise BackupTransportError("YANDEX_CONFIG_UNTRUSTED")
    meta = CONFIG.stat()
    if meta.st_uid != 1000 or stat.S_IMODE(meta.st_mode) != 0o600:
        raise BackupTransportError("YANDEX_CONFIG_OWNER_MODE")
    conf = configparser.ConfigParser(interpolation=None)
    with CONFIG.open(encoding="utf-8") as stream:
        conf.read_file(stream)
    if conf.get("yandex", "type", fallback="") != "yandex":
        raise BackupTransportError("YANDEX_REMOTE_IDENTITY_CHANGED")
    try:
        access = json.loads(conf.get("yandex", "token"))["access_token"]
    except (KeyError, ValueError, configparser.Error):
        raise BackupTransportError("YANDEX_TOKEN_NOT_CONFIGURED") from None
    if not isinstance(access, str) or len(access) < 20:
        raise BackupTransportError("YANDEX_ACCESS_TOKEN_INVALID")
    return access


class AppFolder:
    def __init__(self, token: str):
        if not isinstance(token, str) or len(token) < 20:
            raise BackupTransportError("YANDEX_TOKEN_INVALID")
        self._token = token

    def api(self, suffix: str, *, params: dict | None = None, method: str = "GET") -> tuple[int, dict]:
        if suffix not in ("/resources", "/resources/upload", "/resources/download"):
            raise BackupTransportError("UNSUPPORTED_YANDEX_ENDPOINT")
        url = API_BASE + suffix
        if params:
            url += "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(
            url, method=method,
            headers={"Authorization": "OAuth " + self._token,
                     "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=24) as response:
                code = response.status
                raw = response.read(250000)
        except urllib.error.HTTPError as exc:
            code = exc.code
            raw = exc.read(20000)
        except (OSError, TimeoutError) as exc:
            raise BackupTransportError("YANDEX_NETWORK_" + type(exc).__name__) from None
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError):
            value = {}
        if not isinstance(value, dict):
            value = {}
        return code, value

    def folder_preflight(self) -> None:
        status, info = self.api("/resources", params={"path": ROOT, "fields": "type,path"})
        if status != 200 or info.get("type") != "dir":
            raise BackupTransportError("DATABASE_FOLDER_NOT_FOUND")
        probe_name = "_nightly_preflight_" + os.urandom(8).hex() + ".age"
        status, info = self.api("/resources/upload", params={
            "path": ROOT + "/" + probe_name, "overwrite": "false",
        })
        if status != 200 or info.get("method") != "PUT":
            raise BackupTransportError("DATABASE_FOLDER_NOT_WRITABLE")
        validate_signed_link(str(info.get("href", "")))

    def metadata(self, name: str) -> dict | None:
        status, info = self.api("/resources", params={"path": ROOT + "/" + validate_archive_name(name)})
        if status == 404:
            return None
        if status != 200:
            raise BackupTransportError("REMOTE_METADATA_HTTP_" + str(status))
        if info.get("type") != "file":
            raise BackupTransportError("REMOTE_PATH_NOT_FILE")
        return info

    def signed_url(self, resource: str, name: str) -> tuple[str, int, str]:
        query = {"path": ROOT + "/" + validate_archive_name(name)}
        if resource == "upload":
            query["overwrite"] = "false"
        if resource not in ("upload", "download"):
            raise BackupTransportError("UNKNOWN_SIGNED_URL_ACTION")
        status, info = self.api("/resources/" + resource, params=query)
        method = "PUT" if resource == "upload" else "GET"
        if status != 200 or info.get("method") != method:
            raise BackupTransportError("SIGNED_URL_" + resource.upper() + "_HTTP_" + str(status))
        return validate_signed_link(str(info.get("href", "")))

    def upload_file(self, source: Path, name: str, size: int) -> None:
        host, port, target = self.signed_url("upload", name)
        connection = http.client.HTTPSConnection(host, port, timeout=3600,
                                                  context=ssl.create_default_context())
        try:
            with source.open("rb") as stream:
                connection.request("PUT", target, body=stream, headers={
                    "Content-Type": "application/octet-stream",
                    "Content-Length": str(size),
                })
                response = connection.getresponse()
                status = response.status
                response.read(4096)
        except (OSError, TimeoutError, http.client.HTTPException) as exc:
            raise BackupTransportError("YANDEX_UPLOAD_NETWORK_" + type(exc).__name__) from None
        finally:
            connection.close()
        if status not in (201, 202):
            raise BackupTransportError("YANDEX_UPLOAD_HTTP_" + str(status))

    def verify_download(self, name: str, expected_size: int, expected_sha: str) -> None:
        host, port, target = self.signed_url("download", name)
        url = urllib.parse.urlunsplit(("https", host, target.split("?", 1)[0],
                                      target.split("?", 1)[1] if "?" in target else "", ""))
        # urllib performs HTTPS certificate verification; URL is never logged.
        try:
            with urllib.request.urlopen(url, timeout=600) as response:
                if response.status != 200:
                    raise BackupTransportError("YANDEX_DOWNLOAD_NOT_200")
                digest = hashlib.sha256()
                count = 0
                while True:
                    chunk = response.read(2 * 1024 * 1024)
                    if not chunk:
                        break
                    count += len(chunk)
                    if count > expected_size:
                        raise BackupTransportError("YANDEX_DOWNLOAD_SIZE_EXCEEDED")
                    digest.update(chunk)
        except (OSError, TimeoutError, urllib.error.HTTPError) as exc:
            raise BackupTransportError("YANDEX_DOWNLOAD_NETWORK_" + type(exc).__name__) from None
        if count != expected_size or digest.hexdigest() != expected_sha:
            raise BackupTransportError("YANDEX_DOWNLOADED_SHA256_MISMATCH")

    def upload_and_verify(self, source: Path) -> dict:
        name = validate_archive_name(source.name)
        size, checksum = private_cipher(source)
        self.folder_preflight()
        remote = self.metadata(name)
        if remote is not None:
            if remote.get("size") != size:
                raise BackupTransportError("REMOTE_IMMUTABLE_ARCHIVE_COLLISION")
        else:
            self.upload_file(source, name, size)
        # An accepted asynchronous upload can take a short time to become visible.
        for _ in range(30):
            remote = self.metadata(name)
            if remote is not None:
                break
            time.sleep(2)
        if remote is None or remote.get("size") != size:
            raise BackupTransportError("REMOTE_ARCHIVE_NOT_VERIFIED")
        self.verify_download(name, size, checksum)
        return {"name": name, "size": size, "sha256": checksum,
                "download_sha256_verified": True, "remote": "app_folder"}


if __name__ == "__main__":
    # Intentionally no unauthenticated CLI that could transmit arbitrary files.
    raise SystemExit("Import via the pinned ASA school nightly backup workflow.")
