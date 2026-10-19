#!/usr/bin/env python3
"""Безопасный локальный ввод 1Panel API-ключа без аргументов и истории shell."""
from getpass import getpass
import os
from pathlib import Path
import re
import sys

p = Path("/var/lib/server-settings/1panel-api.key")
if os.geteuid() != 0:
    print("Запустите через sudo", file=sys.stderr)
    sys.exit(1)
if p.exists():
    print("Ключ уже существует. Сначала отзовите/архивируйте старый в 1Panel; не перезаписываю", file=sys.stderr)
    sys.exit(1)
secret = getpass("Введите созданный в 1Panel API-ключ (ввод скрыт): ").strip()
if not re.fullmatch(r"[a-fA-F0-9]{32}", secret):
    print("Отказ: неверный формат ключа", file=sys.stderr)
    sys.exit(1)
p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as f:
    f.write(secret + "\n")
print("API-ключ сохранён в root-only файле. Содержимое не выводилось и не отправлялось в GitHub")
