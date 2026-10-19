#!/usr/bin/env python3
"""Проверка цепочки доверия и срока публичного HTTPS; 0=OK, 2=предупреждение."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import ipaddress
import json
import socket
import ssl
import sys


def inspect_certificate(host: str, port: int = 443, timeout: float = 9) -> dict:
    if not 1 <= port <= 65535:
        raise ValueError("Недопустимый порт")
    context = ssl.create_default_context()
    with socket.create_connection((host, port), timeout=timeout) as tcp:
        with context.wrap_socket(tcp, server_hostname=host) as stream:
            certificate = stream.getpeercert()
    if not certificate or not certificate.get("notAfter"):
        raise ValueError("TLS не вернул валидный сертификат")
    expires = datetime.strptime(certificate["notAfter"], "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
    subject_alt = certificate.get("subjectAltName") or []
    try:
        ipaddress.ip_address(host)
        match = ("IP Address", host) in subject_alt
    except ValueError:
        # wrap_socket с check_hostname=True уже проверил DNS и wildcard через доверенную CA.
        match = True
    if not match:
        raise ValueError("Публичный сертификат не содержит запрошенный host")
    hours = (expires - datetime.now(timezone.utc)).total_seconds() / 3600.0
    return {"host": host, "port": port, "valid_to_utc": expires.isoformat(),
            "hours_until_expiry": round(hours, 2), "verified_trust": True, "verified_subject": True}


def main():
    p = argparse.ArgumentParser(description="Проверка короткоживущего публичного HTTPS сертификата")
    p.add_argument("--host", required=True)
    p.add_argument("--port", type=int, default=443)
    p.add_argument("--warn-hours", type=float, default=48)
    args = p.parse_args()
    if args.warn_hours <= 0 or args.warn_hours > 720:
        p.error("Неправильный порог предупреждения")
    try:
        result = inspect_certificate(args.host, args.port)
    except (OSError, ssl.SSLError, ValueError) as exc:
        print(json.dumps({"ok": False, "reason": f"TLS проверка не пройдена: {type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 2
    result["ok"] = result["hours_until_expiry"] >= args.warn_hours
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
