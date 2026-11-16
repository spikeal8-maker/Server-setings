#!/usr/bin/env python3
"""Safe Ubuntu 24.04 operator entry for the ASA LAB test 1Panel terminal.

Check/plan never install packages. Security install is limited to official
Ubuntu security origins, requires current protected backups and an accepted
isolated restore, never reboots and refuses infrastructure-critical packages.
No full distribution upgrade, Docker restart, network change or apt autoremove.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys
import time

HOST = "admins-MS-7D22"
ROOT = Path("/srv/asa-lab")
STATE = ROOT / ".asa"
LAST_CLOUD = Path("/var/lib/asa-nightly-backup/last-success.json")
RESTORE_WITNESS = Path("/var/backups/asa-system-maintenance/local-restore-20261116.json")
LOCK = Path("/run/lock/asa-h03-ubuntu-maintenance.lock")
AUDIT = Path("/var/backups/asa-system-maintenance")
CRITICAL = (
    "docker", "containerd", "tailscale", "netplan", "network-manager",
    "linux-", "grub", "systemd", "openssh", "libc6", "libssl", "openssl",
    "ufw", "iptables", "nftables", "udev", "cloud-init", "1panel",
)
ACTIONS = ("status", "refresh", "plan", "security-check", "security-apply")


class GuardError(RuntimeError):
    pass


def require(value: bool, explanation: str) -> None:
    if not value:
        raise GuardError(explanation)


def command(args: list[str], *, timeout: int = 30, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, check=False, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, timeout=timeout, env=env)
    require(result.returncode == 0, f"Команда {Path(args[0]).name} завершилась с кодом {result.returncode}")
    return result


def identity() -> None:
    require(os.geteuid() == 0 and socket.gethostname() == HOST,
            "Используйте только root-терминал тестовой Ubuntu H03")
    release = Path("/etc/os-release").read_text(encoding="utf-8")
    require('ID=ubuntu' in release and 'VERSION_ID="24.04"' in release,
            "Этот сценарий предусмотрен только для Ubuntu 24.04")
    require(STATE.is_dir() and not STATE.is_symlink(), "Не определена ASA LAB")
    require(AUDIT.is_dir() and not AUDIT.is_symlink(), "Нет подготовленного каталога аудита")


def apt_candidates() -> tuple[list[str], list[str]]:
    import apt
    cache = apt.Cache()
    all_pending: list[str] = []
    security: list[str] = []
    for pkg in cache:
        if not pkg.is_upgradable:
            continue
        all_pending.append(pkg.name)
        origins = pkg.candidate.origins
        if any("security" in (o.archive or "").lower()
               and (o.origin or "").startswith("Ubuntu") for o in origins):
            security.append(pkg.name)
    return sorted(all_pending), sorted(security)


def critical_packages(names: list[str]) -> list[str]:
    return sorted(name for name in names if name.startswith(CRITICAL))


def updated() -> bool:
    return Path("/run/reboot-required").is_file()


def service_active(name: str) -> bool:
    return subprocess.run(["/usr/bin/systemctl", "is-active", "--quiet", name],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          timeout=10, check=False).returncode == 0


def safe_json(path: Path) -> dict:
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and not path.is_symlink(),
            "Не найден достоверный файл состояния: " + path.name)
    require(info.st_mode & 0o077 == 0 and info.st_uid in (0, 1000),
            "Неверные права файла состояния: " + path.name)
    data = json.loads(path.read_text(encoding="utf-8"))
    require(isinstance(data, dict), "Некорректная структура состояния: " + path.name)
    return data


def backup_preflight() -> None:
    live = safe_json(STATE / "installed-release.json")
    installed = safe_json(STATE / "installation.json")
    cloud = safe_json(LAST_CLOUD)
    witness = safe_json(RESTORE_WITNESS)
    now = dt.datetime.now(dt.timezone.utc)
    at = dt.datetime.fromisoformat(cloud["at"].replace("Z", "+00:00"))
    age = (now - at).total_seconds()
    require(0 <= age <= 26 * 3600, "Нет свежей облачной копии (до 26 часов)")
    require(cloud.get("status") == "success"
            and cloud.get("backup_verified") is True
            and cloud.get("cloud_roundtrip_verified") is True,
            "Неподтверждённая облачная копия")
    require(cloud.get("runtime_revision") == live.get("revision")
            and cloud.get("runtime_schema") == live.get("schema")
            and cloud.get("installation_instance_id") == installed.get("instanceId"),
            "Облачная копия относится к другой версии ASA LAB")
    require(witness.get("local_restore_passed") is True
            and witness.get("cleanup_verified") is True
            and witness.get("revision") == live.get("revision")
            and witness.get("schema") == live.get("schema"),
            "Нет подтверждённой изолированной проверки восстановления текущего релиза")
    local = ROOT / "backups" / witness["snapshot"] / "manifest.json"
    require(local.is_file() and not local.is_symlink(), "Контрольная локальная копия отсутствует")
    manifest = json.loads(local.read_text(encoding="utf-8"))
    require(manifest.get("complete") is True
            and (manifest.get("runtime") or {}).get("revision") == live["revision"]
            and (manifest.get("runtime") or {}).get("schema") == live["schema"],
            "Контрольная локальная копия не соответствует живому релизу")
    for name in ("docker", "1panel-core", "1panel-agent", "tailscaled", "asa-operations"):
        require(service_active(name), f"Служба {name} недоступна")
    output = command(["/usr/bin/sudo", "-n", "/usr/local/bin/asa-test", "status"], timeout=30).stdout
    require("Контейнеры: 6/6 healthy" in output,
            "Основной контейнерный стек ASA LAB не прошёл проверку")
    print("PREUPDATE_BACKUP_AND_SERVICES_PASS")


def apt_policy() -> None:
    config = command(["/usr/bin/apt-config", "dump"]).stdout
    require("Unattended-Upgrade::Allowed-Origins" in config
            and "security" in config.lower(),
            "Не настроены Ubuntu Security Origins")
    for row in config.splitlines():
        if row.startswith("Unattended-Upgrade::Automatic-Reboot "):
            value = row.split('"')[1].lower() if '"' in row else ""
            require(value not in ("true", "yes", "1"), "Автоматический reboot запрещён")
    require(service_active("apt-daily-upgrade.timer") or
            subprocess.run(["/usr/bin/systemctl", "is-enabled", "--quiet",
                            "apt-daily-upgrade.timer"], check=False).returncode == 0,
            "Нет расписания установки исправлений безопасности")


def status() -> None:
    all_pending, security = apt_candidates()
    print("Сервер:", HOST)
    print("Ubuntu:", "24.04 LTS")
    print("Ядро:", os.uname().release)
    print("Нужна перезагрузка:", updated())
    print("Обновлений пакетов:", len(all_pending))
    print("Кандидатов из Ubuntu Security:", len(security))
    print("Инфраструктурных пакетов в общем плане:", ", ".join(critical_packages(all_pending)) or "нет")
    for name in ("apt-daily.timer", "apt-daily-upgrade.timer", "docker",
                 "1panel-core", "1panel-agent", "tailscaled", "asa-nightly-backup.timer"):
        print("systemd", name, "active" if service_active(name) else "inactive")
    live = safe_json(STATE / "installed-release.json")
    print("ASA LAB:", str(live.get("revision", ""))[:12], "схема", live.get("schema"))
    try:
        receipt = safe_json(LAST_CLOUD)
        print("Облачная копия:", receipt.get("at"), "схема", receipt.get("runtime_schema"),
              "контрольное скачивание", receipt.get("cloud_roundtrip_verified"))
    except (OSError, ValueError, GuardError, KeyError):
        print("Облачная копия: не подтверждена")
    print("Полное обновление системы и reboot автоматически не запускаются")


def refresh() -> None:
    print("Обновление подписанных списков пакетов Ubuntu и подключённых репозиториев")
    result = command(["/usr/bin/apt-get", "update"], timeout=400)
    print("\n".join(result.stdout.splitlines()[-12:]))
    print("APT_INDEX_REFRESHED_NO_PACKAGE_INSTALLED")


def plan() -> None:
    all_pending, security = apt_candidates()
    x = command(["/usr/bin/apt-get", "-s", "-o", "Debug::NoLocking=1", "upgrade"], timeout=90)
    planned = re.findall(r"^Inst ([A-Za-z0-9_.+:-]+) ", x.stdout, flags=re.MULTILINE)
    print("Общий план:", len(planned), "пакетов")
    print("Security origins:", len(security), "пакетов")
    risky = critical_packages(planned)
    print("КРИТИЧЕСКИЕ ЗАВИСИМОСТИ:", ", ".join(risky) if risky else "нет")
    print("Остальные обновления:", ", ".join(p for p in planned if p not in risky)[:2000])
    if risky:
        print("ПОЛНЫЙ UPGRADE БЛОКИРОВАН ДО ОТДЕЛЬНОГО ОКНА ОБСЛУЖИВАНИЯ")
    print("Это только симуляция: apt-get upgrade не выполнен")


def security_check() -> None:
    apt_policy()
    _, security = apt_candidates()
    print("Кандидатов Ubuntu Security:", len(security))
    print("Имена:", ", ".join(security) if security else "нет")
    x = command(["/usr/bin/unattended-upgrade", "--dry-run"], timeout=270)
    print("Результат dry-run:", x.returncode)
    print("\n".join(x.stdout.splitlines()[-12:]))
    print("SECURITY_CHECK_PASSED_NO_CHANGES")


def confirm() -> None:
    require(sys.stdin.isatty(), "Нет интерактивного терминала для подтверждения")
    answer = input("Установить ТОЛЬКО исправления Ubuntu Security, БЕЗ перезагрузки? Введите ДА: ")
    require(answer.strip() == "ДА", "Операция отменена")


def security_apply() -> None:
    apt_policy()
    _, security = apt_candidates()
    if not security:
        print("UBUNTU_SECURITY_UP_TO_DATE: нет ожидающих исправлений безопасности")
        return
    dangerous = critical_packages(security)
    require(not dangerous, "Нужное отдельное окно обслуживания: " + ", ".join(dangerous))
    backup_preflight()
    confirm()
    # Serialize with any manual ASA maintenance; never seize an existing lock.
    with LOCK.open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with (STATE / "operation.lock").open("a+b") as asa:
            fcntl.flock(asa, fcntl.LOCK_EX | fcntl.LOCK_NB)
            audit = AUDIT / ("ubuntu-security-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".log")
            descriptor = os.open(audit, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            environment = dict(os.environ, DEBIAN_FRONTEND="noninteractive",
                               APT_LISTCHANGES_FRONTEND="none", NEEDRESTART_MODE="l")
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                try:
                    result = subprocess.run(["/usr/bin/unattended-upgrade", "--verbose"],
                                            check=False, stdout=output, stderr=subprocess.STDOUT,
                                            text=True, timeout=1500, env=environment)
                except subprocess.TimeoutExpired:
                    raise GuardError("APT превысил время; не повторяйте автоматически. Журнал: " + str(audit))
            require(result.returncode == 0, "Установка безопасности остановлена; журнал: " + str(audit))
            print("UBUNTU_SECURITY_INSTALL_COMPLETED_NO_REBOOT")
            print("Журнал:", audit)
    for service in ("docker", "1panel-core", "1panel-agent", "tailscaled"):
        require(service_active(service), "После обновления не отвечает: " + service)
    print("POST_SECURITY_SERVICE_CHECK_PASS")
    print("Если требуется reboot, выполняйте его отдельно после проверки резервного доступа")


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=ACTIONS)
    args = parser.parse_args()
    try:
        identity()
        if args.action == "status":
            status()
        elif args.action == "refresh":
            refresh()
        elif args.action == "plan":
            plan()
        elif args.action == "security-check":
            security_check()
        elif args.action == "security-apply":
            security_apply()
        return 0
    except (GuardError, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        message = str(error) if isinstance(error, GuardError) else type(error).__name__
        print("UBUNTU MAINTENANCE: " + message, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
