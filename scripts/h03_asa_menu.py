#!/usr/bin/env python3
"""Owner-facing ASA LAB commands for the H03 1Panel terminal.

The menu only invokes the canonical ASA launcher or separately installed,
host-verified backup/cloud transport. It does not replace live data.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import subprocess
import sys

HOST = "admins-MS-7D22"
ROOT = Path("/srv/asa-lab")
LAUNCHER = Path("/usr/local/bin/asa-test")
CLOUD = Path("/opt/server-settings/asa-operations/cloud_storage.py")
NIGHTLY = Path(
    "/opt/server-settings/asa-nightly-backup/"
    "389a7b201a2541b63a13a1a48d415076faec2bc5/nightly-asa-backup.py"
)
RECEIPT = Path("/var/lib/asa-nightly-backup/last-success.json")
TIMER = "asa-nightly-backup.timer"
CLOUD_PROJECT = "asa-test-nightly"
SNAPSHOT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{4,90}\Z")
CLOUD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,180}\.age\Z")
ACTIONS = (
    "self-test", "status", "report", "doctor", "logs-api", "logs-scratch",
    "backups-status", "backups-list", "backups-verify", "backup-local",
    "backup-cloud", "backup-timer", "cloud-check", "cloud-list",
    "cloud-download", "restore-test",
)


class MenuError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise MenuError(message)


def require_file(path: Path, owner: int = 0) -> None:
    metadata = path.lstat()
    require(stat.S_ISREG(metadata.st_mode), "Неожиданный тип файла " + str(path))
    require(metadata.st_uid == owner and metadata.st_mode & 0o022 == 0,
            "Неверные права файла " + str(path))


def runtime_identity() -> None:
    require(os.geteuid() == 0 and socket.gethostname() == HOST,
            "Команда разрешена только root на тестовом H03")
    require_file(LAUNCHER)
    require_file(CLOUD)
    require_file(NIGHTLY)
    record = json.loads((ROOT / ".asa/installation.json").read_text(encoding="utf-8"))
    require(record.get("profile") == "production" and record.get("project") == "asa-lab-dev",
            "Неверная каноническая установка")
    require(ROOT.resolve() == ROOT and ROOT.is_dir(), "Неожиданный путь ASA LAB")


def exec_checked(args: list[str], timeout: int = 90) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, check=True, timeout=timeout)


def owner_launcher(*args: str, timeout: int = 120) -> None:
    exec_checked(["/usr/bin/sudo", "-n", str(LAUNCHER), *args], timeout=timeout)


def approved_backup(name: str) -> Path:
    require(bool(SNAPSHOT_RE.fullmatch(name)) and ".." not in name and "/" not in name,
            "Укажите только имя каталога копии")
    folder = ROOT / "backups" / name
    require(folder.is_dir() and not folder.is_symlink(), "Копия не обнаружена")
    require(folder.resolve().parent == (ROOT / "backups").resolve(),
            "Копия находится вне защищённого каталога")
    manifest = folder / "manifest.json"
    require_file(manifest, owner=1000)
    return folder


def backup_manifests() -> list[tuple[str, int | None, str]]:
    directory = ROOT / "backups"
    items: list[tuple[str, int | None, str]] = []
    if not directory.is_dir():
        return items
    for file in sorted(directory.glob("*/manifest.json"), reverse=True):
        try:
            if file.is_symlink() or file.parent.is_symlink():
                continue
            if file.parent.resolve().parent != directory.resolve():
                continue
            data = json.loads(file.read_text(encoding="utf-8"))
            if data.get("complete") is True:
                runtime = data.get("runtime") or {}
                items.append((file.parent.name, runtime.get("schema"), runtime.get("revision", "")))
        except (OSError, ValueError, TypeError):
            continue
    return items


def local_list() -> None:
    records = backup_manifests()
    print("Полные локальные архивы ASA LAB:", len(records))
    for name, schema, revision in records[:20]:
        print(" •", name, "| схема", schema, "| SHA", str(revision)[:12])
    if not records:
        print("Проверенных полных копий не найдено")


def cloud_call(*args: str, timeout: int = 90) -> dict:
    result = subprocess.run(
        ["/usr/bin/python3", "-I", "-B", str(CLOUD), *args],
        text=True, capture_output=True, timeout=timeout, check=False,
    )
    require(result.returncode == 0, "Ошибка облачного транспорта; проверьте MCP/Яндекс.Диск")
    try:
        data = json.loads(result.stdout)
    except ValueError as error:
        raise MenuError("Облачный транспорт вернул некорректный ответ") from error
    require(isinstance(data, dict), "Неверный формат ответа")
    return data


def cloud_archives() -> list[dict]:
    result = cloud_call("list", "--project", CLOUD_PROJECT, timeout=110)
    values = result.get("archives")
    require(isinstance(values, list), "Отсутствует список облачных архивов")
    return sorted(values, key=lambda x: str(x.get("modified", "")), reverse=True)


def read_last_success() -> dict:
    if not RECEIPT.is_file() or RECEIPT.is_symlink():
        return {}
    require_file(RECEIPT)
    data = json.loads(RECEIPT.read_text(encoding="utf-8"))
    require(isinstance(data, dict), "Квитанция имеет неверный формат")
    return data


def date_age_hours(value: str) -> float | None:
    try:
        at = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (dt.datetime.now(dt.timezone.utc) - at).total_seconds() / 3600
    except (ValueError, TypeError):
        return None


def backup_status() -> None:
    current = json.loads((ROOT / ".asa/installed-release.json").read_text(encoding="utf-8"))
    print("Текущая схема ASA LAB:", current.get("schema"))
    records = backup_manifests()
    if records:
        name, schema, _ = records[0]
        print("Локальная полная копия:", name, "схема", schema)
        print("Локальная копия актуальна по схеме:", "ДА" if schema == current.get("schema") else "НЕТ")
    else:
        print("Локальная полная копия: НЕ НАЙДЕНА")
    receipt = read_last_success()
    if receipt:
        age = date_age_hours(str(receipt.get("at", "")))
        print("Последняя зашифрованная копия:", receipt.get("at"))
        print("Схема облачной квитанции:", receipt.get("runtime_schema"))
        print("Cloud round-trip проверен:", receipt.get("cloud_roundtrip_verified") is True)
        print("Возраст копии (часы):", round(age, 1) if age is not None else "неизвестно")
        if age is None or age > 26 or receipt.get("runtime_schema") != current.get("schema"):
            print("ВНИМАНИЕ: последняя копия не подтверждает свежую защиту текущей версии")
    else:
        print("Квитанция облачной копии: отсутствует")
    cloud = cloud_archives()
    if cloud:
        print("Последний архив в Яндекс.Диске:", cloud[0].get("name"), cloud[0].get("modified"))
    else:
        print("Архивов в Яндекс.Диске не найдено")


def ask(message: str) -> str:
    try:
        return input(message).strip()
    except EOFError as error:
        raise MenuError("Нет интерактивного ввода: действие не выполнялось") from error


def confirm(message: str) -> None:
    require(ask(message + " Введите ДА: ") == "ДА", "Операция отменена")


def select_backup() -> Path:
    local_list()
    return approved_backup(ask("Введите точное имя каталога локальной копии: "))


def backup_timer() -> None:
    for flag in ("is-enabled", "is-active"):
        result = subprocess.run(
            ["/usr/bin/systemctl", flag, TIMER],
            text=True, capture_output=True, check=False, timeout=10,
        )
        print(TIMER, flag, (result.stdout.strip() or "unknown"))


def run_action(action: str) -> None:
    runtime_identity()
    if action == "self-test":
        owner_launcher("--launcher-verify", str(ROOT), timeout=30)
        print("H03_MENU_RUNTIME_OK")
    elif action in ("status", "report", "doctor"):
        owner_launcher(action, timeout=90)
    elif action in ("logs-api", "logs-scratch"):
        service = action.split("-", 1)[1]
        exec_checked([
            "/usr/bin/sudo", "-n", "-H", "-u", "admins", "--",
            "/usr/bin/python3", "-I", "-B", str(ROOT / "tools/asa_status.py"),
            "logs", service, "--root", str(ROOT), "--tail", "80",
        ], timeout=35)
    elif action == "backups-list":
        local_list()
    elif action == "backups-status":
        backup_status()
    elif action == "backups-verify":
        folder = select_backup()
        owner_launcher("verify-backup", "--backup", str(folder), timeout=600)
    elif action == "backup-local":
        confirm("Создание полной копии может кратко прервать работу ASA LAB.")
        owner_launcher("backup", timeout=3600)
    elif action == "backup-cloud":
        confirm("Штатная облачная копия временно приостановит записи ASA LAB.")
        exec_checked(["/usr/bin/python3", "-I", "-B", str(NIGHTLY), "schedule", "--apply"], timeout=7100)
    elif action == "backup-timer":
        backup_timer()
    elif action == "cloud-check":
        result = cloud_call("status")
        print("Яндекс.Диск подключён:", result.get("connected") is True)
        print("Облачный транспорт установлен:", result.get("installed") is True)
        require(result.get("connected") is True, "Нет подтверждения подключения Яндекс.Диска")
    elif action == "cloud-list":
        records = cloud_archives()
        print("Облачные архивы в", CLOUD_PROJECT, ":", len(records))
        for row in records[:30]:
            print(" •", row.get("name"), row.get("modified"), row.get("size"), "байт")
    elif action == "cloud-download":
        archives = cloud_archives()
        for row in archives[:20]:
            print(" •", row.get("name"))
        filename = ask("Точное имя зашифрованного .age архива: ")
        require(bool(CLOUD_RE.fullmatch(filename)), "Неверное имя .age архива")
        require(any(row.get("name") == filename for row in archives), "Файл отсутствует в облачном каталоге")
        confirm("Будет скачан только зашифрованный архив, без замены базы.")
        result = cloud_call("download", "--project", CLOUD_PROJECT, "--filename", filename, "--apply", timeout=1800)
        print("Загрузка:", result.get("filename", filename), "завершена")
    elif action == "restore-test":
        folder = select_backup()
        confirm("Создать только НОВУЮ изолированную тестовую БД? Рабочая БД останется прежней.")
        db = "asa_h03_menu_" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d") + "_" + secrets.token_hex(5) + "_test"
        print("Целевая тестовая БД:", db, flush=True)
        owner_launcher("restore-check", "--backup", str(folder), "--database", db, timeout=1800)
    else:
        raise MenuError("Неизвестное действие меню")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=ACTIONS)
    args = parser.parse_args()
    try:
        run_action(args.action)
        return 0
    except (MenuError, OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError) as error:
        if isinstance(error, MenuError):
            message = str(error)
        elif isinstance(error, subprocess.CalledProcessError):
            message = "Команда завершилась с кодом " + str(error.returncode)
        else:
            message = "Операция прервана: " + type(error).__name__
        print("ASA LAB:", message, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
