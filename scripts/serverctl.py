#!/usr/bin/env python3
"""Сборка и аудит Ubuntu-сервера. Установка изменяет систему ТОЛЬКО с --apply."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
PROFILES = ROOT / "profiles"
STATE = Path("/var/lib/server-settings")
ARCHES = {"x86_64": "amd64", "aarch64": "arm64"}
PANEL_ARTIFACT_BASE = "https://resource.1panel.pro/v2/stable"
PANEL_VERSION_PATTERN = re.compile(r"^v2\.\d+\.\d+$")


def load_profile(name: str) -> dict:
    if not re.fullmatch(r"[a-z0-9-]+", name):
        raise ValueError("Некорректное имя профиля")
    profile_file = PROFILES / f"{name}.json"
    data = json.loads(profile_file.read_text(encoding="utf-8"))
    if data["name"] != name or data["os"] != "ubuntu-24.04":
        raise ValueError("Неправильный профиль либо неподдерживаемая ОС")
    if not PANEL_VERSION_PATTERN.fullmatch(data["panel_version"]):
        raise ValueError("Версия 1Panel должна быть закреплена как v2.x.y")
    for field in ("panel_port", "ssh_port"):
        if not 1 <= int(data[field]) <= 65535:
            raise ValueError(f"Недопустимый порт {field}")
    for cidr in data.get("lan_cidrs", []):
        ipaddress.ip_network(cidr, strict=False)
    for item in data.get("websites", []):
        if not item.get("upstream", "").startswith("http://127.0.0.1:"):
            raise ValueError("Публичный прокси разрешён только к loopback")
    return data


def os_release() -> dict[str, str]:
    path = Path("/etc/os-release")
    if not path.exists():
        return {}
    answer = {}
    for line in path.read_text().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            answer[k] = v.strip('"')
    return answer


def command(*cmd: str, check: bool = True, capture: bool = False, env: dict | None = None) -> subprocess.CompletedProcess:
    print("Выполняю:", " ".join(cmd), flush=True)
    return subprocess.run(cmd, check=check, text=True, capture_output=capture, env=env)


def is_installed(name: str) -> bool:
    return shutil.which(name) is not None


def service_state(name: str, mode: str = "is-active") -> str:
    p = subprocess.run(["systemctl", mode, name], capture_output=True, text=True, check=False)
    return p.stdout.strip() or "unknown"


def ssh_admin_cidrs(data: dict, override: list[str]) -> list[str]:
    ips = set(data.get("lan_cidrs", []))
    ips.update(override)
    ssh = os.environ.get("SSH_CONNECTION", "").split()
    if len(ssh) == 4:
        client = ipaddress.ip_address(ssh[0])
        ips.add(f"{client}/{client.max_prefixlen}")
    result = []
    for value in sorted(ips):
        result.append(str(ipaddress.ip_network(value, strict=False)))
    return result


def resolve_ssh_ports(data: dict) -> list[int]:
    ports = {int(data["ssh_port"])}
    ssh = os.environ.get("SSH_CONNECTION", "").split()
    if len(ssh) == 4 and ssh[3].isdigit():
        port = int(ssh[3])
        if 1 <= port <= 65535:
            ports.add(port)
    return sorted(ports)


def plan(data: dict, admin_cidrs: list[str]) -> dict:
    return {
        "профиль": data["name"],
        "установка": {"Docker": data["install_docker"], "Tailscale": data["install_tailscale"],
                      "1Panel": data["panel_version"] if data["install_1panel"] else "не требуется"},
        "1Panel": {"порт": data["panel_port"], "администратор": data["panel_user"],
                   "публичное открытие панели": False},
        "SSH": {"порты": resolve_ssh_ports(data), "разрешённые сети": admin_cidrs},
        "Firewall": {"политика": "deny incoming", "публичные HTTP/HTTPS": data["allow_public_web"],
                     "Tailscale для SSH и панели": data["admin_over_tailscale"]},
        "веб-сайты": data.get("websites", []),
        "отдельные этапы после установки": ["Авторизация Tailscale", "Установка OpenResty в 1Panel через API",
                                             "Регистрация сайтов и сертификатов в 1Panel", "Резервные копии приложений"],
    }


def verify_host(data: dict):
    release = os_release()
    if release.get("ID") != "ubuntu" or release.get("VERSION_ID") != "24.04":
        raise RuntimeError("Автоматический установщик разрешён только для Ubuntu 24.04 LTS")
    machine = os.uname().machine
    if machine not in ARCHES:
        raise RuntimeError("Архитектура не проверена: " + machine)
    if os.geteuid() != 0:
        raise PermissionError("Для установки требуется sudo/root")
    if shutil.disk_usage("/").free < 8 * 1024**3:
        raise RuntimeError("На диске менее 8 ГБ свободного места")
    if Path("/opt/1panel/db/core.db").exists() or is_installed("1pctl"):
        raise RuntimeError("Обнаружена существующая 1Panel. Режим установки НЕ обновляет действующий сервер")
    for port in (int(data["panel_port"]), 80, 443):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as conn:
            conn.settimeout(0.5)
            if conn.connect_ex(("127.0.0.1", port)) == 0:
                raise RuntimeError(f"Порт {port} уже занят; отказываюсь устанавливать поверх действующей системы")
    return ARCHES[machine]


def write_secure(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)


def download(url: str, path: Path):
    if not url.startswith("https://"):
        raise RuntimeError("Небезопасная ссылка скачивания")
    req = urllib.request.Request(url, headers={"User-Agent": "ASA-server-settings/1.0"})
    with urllib.request.urlopen(req, timeout=120) as response, path.open("wb") as target:
        shutil.copyfileobj(response, target)


def install_base_packages():
    command("apt-get", "update")
    command("apt-get", "install", "-y", "ca-certificates", "curl", "gnupg", "git", "jq", "openssl", "rsync", "ufw", "python3")


def install_docker():
    if is_installed("docker") and subprocess.run(["docker", "compose", "version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
        print("Docker + Compose уже установлены")
        return
    arch = ARCHES[os.uname().machine]
    Path("/etc/apt/keyrings").mkdir(mode=0o755, exist_ok=True)
    download("https://download.docker.com/linux/ubuntu/gpg", Path("/etc/apt/keyrings/docker.asc"))
    Path("/etc/apt/keyrings/docker.asc").chmod(0o644)
    release = os_release()["VERSION_CODENAME"]
    source = ("Types: deb\nURIs: https://download.docker.com/linux/ubuntu\nSuites: " + release +
              "\nComponents: stable\nArchitectures: " + arch +
              "\nSigned-By: /etc/apt/keyrings/docker.asc\n")
    Path("/etc/apt/sources.list.d/docker.sources").write_text(source, encoding="utf-8")
    command("apt-get", "update")
    command("apt-get", "install", "-y", "docker-ce", "docker-ce-cli", "containerd.io", "docker-buildx-plugin", "docker-compose-plugin")
    command("systemctl", "enable", "--now", "docker")


def install_tailscale():
    if is_installed("tailscale"):
        print("Tailscale уже установлен; в tailnet автоматически не авторизуем")
        return
    release = os_release()["VERSION_CODENAME"]
    if release != "noble":
        raise RuntimeError("Неизвестный выпуск для Tailscale")
    key = Path("/usr/share/keyrings/tailscale-archive-keyring.gpg")
    download("https://pkgs.tailscale.com/stable/ubuntu/noble.noarmor.gpg", key)
    download("https://pkgs.tailscale.com/stable/ubuntu/noble.tailscale-keyring.list",
             Path("/etc/apt/sources.list.d/tailscale.list"))
    command("apt-get", "update")
    command("apt-get", "install", "-y", "tailscale")
    command("systemctl", "enable", "--now", "tailscaled")
    print("Далее владелец должен выполнить: sudo tailscale up")


def install_panel(data: dict, arch: str):
    if is_installed("1pctl"):
        print("1Panel уже установлена; пропускаю")
        return
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    credentials = STATE / "1panel-admin.env"
    if credentials.exists():
        raise RuntimeError("Найдены прежние credentials при отсутствии 1Panel. Требуется ручной аудит")
    password = secrets.token_urlsafe(24)
    entrance = "access_" + secrets.token_hex(12)
    version = data["panel_version"]
    basename = f"1panel-{version}-linux-{arch}.tar.gz"
    base = f"{PANEL_ARTIFACT_BASE}/{version}/release"
    with tempfile.TemporaryDirectory(prefix="server-settings-panel-") as d:
        folder = Path(d)
        checksums = folder / "checksums.txt"
        bundle = folder / basename
        download(base + "/checksums.txt", checksums)
        expected = None
        for line in checksums.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[-1].lstrip("*") == basename:
                expected = parts[0].lower()
        if expected is None or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise RuntimeError("Отсутствует SHA256 установочного пакета в официальном манифесте")
        locked = json.loads((ROOT / "versions.lock.json").read_text(encoding="utf-8"))
        if locked["1panel"]["version"] != version or locked["1panel"]["sha256"].get(arch) != expected:
            raise RuntimeError("Официальный манифест отличается от закреплённого SHA256 в репозитории")
        download(base + "/" + basename, bundle)
        actual = hashlib.sha256(bundle.read_bytes()).hexdigest()
        if actual != expected:
            raise RuntimeError("SHA256 пакета 1Panel не совпадает с официальным манифестом")
        with tarfile.open(bundle, "r:gz") as tar:
            root = folder.resolve()
            for member in tar.getmembers():
                target = (folder / member.name).resolve()
                if not target.is_relative_to(root) or member.issym() or member.islnk():
                    raise RuntimeError("Недоверенная структура установочного архива")
            tar.extractall(folder, filter="data")
        installer = folder / f"1panel-{version}-linux-{arch}" / "install.sh"
        if not installer.is_file():
            raise RuntimeError("В официальном архиве нет install.sh")
        write_secure(credentials, f"PANEL_USER={data['panel_user']}\nPANEL_PASSWORD={password}\nPANEL_ENTRANCE={entrance}\nPANEL_PORT={data['panel_port']}\n")
        environment = dict(os.environ, PANEL_NON_INTERACTIVE="true", PANEL_LANG="ru", PANEL_INSTALL_DIR="/opt",
                           PANEL_PORT=str(data["panel_port"]), PANEL_ENTRANCE=entrance,
                           PANEL_USERNAME=data["panel_user"], PANEL_PASSWORD=password,
                           PANEL_INSTALL_DOCKER="n", PANEL_CONFIGURE_ACCELERATOR="n", PANEL_REPLACE_DAEMON_JSON="n")
        command("bash", str(installer), "--non-interactive", "--lang", "ru", "--install-dir", "/opt",
                "--port", str(data["panel_port"]), "--entrance", entrance,
                "--username", data["panel_user"], "--install-docker", "n",
                "--configure-accelerator", "n", "--replace-daemon-json", "n", env=environment)
        print("1Panel установлен. Временные файлы скачивания удалены. Корневые credentials: ", credentials)


def apply_firewall(data: dict, cidrs: list[str]):
    if not cidrs:
        raise RuntimeError("Блокировка UFW: нет безопасного CIDR для SSH. Используйте --admin-cidr")
    for port in resolve_ssh_ports(data):
        for cidr in cidrs:
            command("ufw", "allow", "from", cidr, "to", "any", "port", str(port), "proto", "tcp")
    for cidr in data.get("lan_cidrs", []):
        command("ufw", "allow", "from", cidr, "to", "any", "port", str(data["panel_port"]), "proto", "tcp")
    if data["admin_over_tailscale"] and data["install_tailscale"]:
        for port in {*resolve_ssh_ports(data), data["panel_port"]}:
            command("ufw", "allow", "in", "on", "tailscale0", "to", "any", "port", str(port), "proto", "tcp")
    if data["allow_public_web"]:
        command("ufw", "allow", "80/tcp")
        command("ufw", "allow", "443/tcp")
    command("ufw", "default", "deny", "incoming")
    command("ufw", "default", "allow", "outgoing")
    command("ufw", "--force", "enable")


def read_command(args: list[str], limit: int = 10000) -> str:
    if not is_installed(args[0]):
        return "НЕ УСТАНОВЛЕНО"
    res = subprocess.run(args, capture_output=True, text=True, check=False, timeout=12)
    return (res.stdout or res.stderr).strip()[:limit]


def doctor() -> dict:
    states = {name: {"active": service_state(name), "enabled": service_state(name, "is-enabled")}
              for name in ("1panel-core", "1panel-agent", "docker", "tailscaled", "asa-lab-public-ip.service",
                           "asa-lab-lan-https.service")}
    p80 = read_command(["ss", "-lntp"]) if is_installed("ss") else ""
    warning = (states["asa-lab-public-ip.service"]["enabled"] == "enabled" and
               states["asa-lab-public-ip.service"]["active"] != "active" and
               ("0.0.0.0:80" in p80 or "[::]:80" in p80))
    return {"дата": dt.datetime.now(dt.timezone.utc).isoformat(), "хост": socket.gethostname(),
            "ОС": os_release().get("PRETTY_NAME", "UNKNOWN"), "службы": states,
            "1Panel": read_command(["1pctl", "version"], limit=700),
            "Docker": read_command(["docker", "ps", "--format", "{{.Names}} | {{.Image}}"], limit=2500),
            "Firewall": read_command(["ufw", "status", "verbose"], limit=3500),
            "предупреждения": (["Старый публичный Caddy включён в автозагрузку и может конфликтовать с OpenResty"] if warning else []),
            "секреты": "Не собирались и не экспортировались"}


def repair_stale_public_caddy(apply: bool):
    old = "asa-lab-public-ip.service"
    if service_state(old) != "inactive":
        raise RuntimeError("Старый публичный Caddy не inactive — требуется отдельный аудит")
    if service_state(old, "is-enabled") != "enabled":
        print("Старая служба не включена в автозагрузку. Изменений не требуется")
        return
    output = read_command(["ss", "-lntp"])
    if not ("0.0.0.0:80" in output and "0.0.0.0:443" in output and "openresty" in output):
        raise RuntimeError("Проверка OpenResty 80/443 не прошла — ничего не меняю")
    if not apply:
        print("ПЛАН: systemctl disable asa-lab-public-ip.service — потребуется --apply")
        return
    command("systemctl", "disable", old)
    print("Устаревшая, уже остановленная публичная служба исключена из автозагрузки")


def main():
    parser = argparse.ArgumentParser(description="Установка и аудит сервера Ubuntu с 1Panel")
    parser.add_argument("action", choices=("plan", "install", "doctor", "repair-public-caddy"))
    parser.add_argument("--profile", choices=("home-static", "home-dynamic", "vps"), default="home-static")
    parser.add_argument("--admin-cidr", action="append", default=[])
    parser.add_argument("--apply", action="store_true", help="разрешить изменения системы")
    args = parser.parse_args()
    if args.action == "doctor":
        print(json.dumps(doctor(), ensure_ascii=False, indent=2))
        return
    if args.action == "repair-public-caddy":
        if args.apply and os.geteuid() != 0:
            raise PermissionError("Для изменения службы требуется root")
        repair_stale_public_caddy(args.apply)
        return
    data = load_profile(args.profile)
    cidrs = ssh_admin_cidrs(data, args.admin_cidr)
    print(json.dumps(plan(data, cidrs), ensure_ascii=False, indent=2))
    if args.action == "plan" or not args.apply:
        print("Это только план. Ничего не изменено; для выполнения: --apply")
        return
    arch = verify_host(data)
    if not cidrs:
        raise RuntimeError("Нельзя включить UFW, не зная доверенных SSH CIDR")
    install_base_packages()
    if data["install_docker"]:
        install_docker()
    if data["install_tailscale"]:
        install_tailscale()
    if data["install_1panel"]:
        install_panel(data, arch)
    apply_firewall(data, cidrs)
    print("БАЗОВАЯ УСТАНОВКА ЗАВЕРШЕНА. Проверьте 1Panel, затем установите OpenResty и сайты через панель/API.")
    print("Доступ к панели (порт):", data["panel_port"])
    print("Для диагностики: sudo python3 scripts/serverctl.py doctor")
    print("Не открывайте порт 1Panel через NAT или облачные security groups")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, PermissionError, OSError, subprocess.CalledProcessError) as exc:
        print("ОШИБКА:", str(exc), file=sys.stderr)
        raise SystemExit(1)
