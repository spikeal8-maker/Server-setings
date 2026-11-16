#!/usr/bin/env python3
"""Install a guarded Ubuntu security-maintenance menu in H03's 1Panel.

Default: plan only. Apply affects one new command group, one root-owned local
dispatcher and an explicit no-auto-reboot APT policy. Existing commands, sites,
network rules and application data are not modified.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import tempfile

from panelctl import PanelAPI, read_api_key, require_local_write

HOST = "admins-MS-7D22"
SOURCE = Path(__file__).with_name("h03_ubuntu_maintenance.py")
TARGET = Path("/usr/local/libexec/asa-h03/ubuntu-maintenance.py")
BACKUPS = Path("/var/lib/server-settings/h03-ubuntu-menu-history")
POLICY = Path("/etc/apt/apt.conf.d/52asa-h03-security-no-reboot")
POLICY_TEXT = ('// ASA H03: require controlled reboot after backup, never auto reboot\n'
               'Unattended-Upgrade::Automatic-Reboot "false";\n'
               'Unattended-Upgrade::Automatic-Reboot-WithUsers "false";\n')
GROUP = "Ubuntu · Обновления и безопасность"


def call(action: str) -> str:
    return "sudo -- /usr/bin/python3 -I -B /usr/local/libexec/asa-h03/ubuntu-maintenance.py " + action


MENU = (
    ("Состояние Ubuntu и безопасности", call("status")),
    ("Обновить каталог пакетов Ubuntu", call("refresh")),
    ("План полного обновления Ubuntu", call("plan")),
    ("Проверить исправления безопасности", call("security-check")),
    ("Установить исправления безопасности", call("security-apply")),
)


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise RuntimeError(reason)


def identity() -> None:
    require(os.geteuid() == 0 and socket.gethostname() == HOST,
            "Действие разрешено только root на тестовом H03")
    for path in (SOURCE, SOURCE.parent, TARGET.parent):
        metadata = path.lstat()
        require(metadata.st_uid == 0 and not metadata.st_mode & 0o022,
                "Небезопасный путь инструментов: " + str(path))
    require(SOURCE.is_file() and not SOURCE.is_symlink()
            and TARGET.parent.is_dir() and not TARGET.parent.is_symlink(),
            "Неправильная структура инструментов")
    result = subprocess.run(["/usr/bin/systemctl", "is-active", "--quiet", "1panel-core"],
                            timeout=15, check=False)
    require(result.returncode == 0, "1Panel Core не работает")


def snapshot(api: PanelAPI) -> tuple[dict, dict]:
    groups = api.request("POST", "/core/groups/search", {"type": "command"})
    records = api.request("POST", "/core/commands/search",
                          {"type": "command", "page": 1, "pageSize": 200,
                           "orderBy": "name", "order": "ascending", "info": ""})
    require(isinstance(groups, list) and isinstance(records, dict), "API не вернул меню")
    rows = records.get("items") or []
    require(len(rows) == records.get("total") and len(rows) <= 200,
            "Неполный список команд")
    by_group = {row["name"]: row for row in groups}
    by_command = {row["name"]: row for row in rows}
    require(len(by_group) == len(groups) and len(by_command) == len(rows),
            "Найдены дубликаты в существующем меню")
    return by_group, by_command


def guard_existing(groups: dict, commands: dict) -> None:
    existing = groups.get(GROUP)
    for title, body in MENU:
        if title not in commands:
            continue
        row = commands[title]
        require(existing is not None and row["groupID"] == existing["id"]
                and row["command"] == body,
                "Команда совпадает по имени, но имеет другое действие: " + title)


def save_snapshot(groups: dict, commands: dict) -> Path:
    BACKUPS.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = BACKUPS.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0
            and stat.S_IMODE(info.st_mode) == 0o700, "Небезопасный каталог истории")
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=BACKUPS,
        prefix="before-" + stamp + "-", suffix=".json", delete=False,
    ) as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump({"at": stamp, "groups": list(groups.values()),
                   "commands": list(commands.values())}, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
        return Path(stream.name)


def install_dispatcher() -> None:
    body = SOURCE.read_bytes()
    if TARGET.exists() or TARGET.is_symlink():
        old = TARGET.lstat()
        require(stat.S_ISREG(old.st_mode) and old.st_uid == 0
                and not old.st_mode & 0o022 and TARGET.read_bytes() == body,
                "Уже существует другой Ubuntu dispatcher: " + str(TARGET))
        return
    with tempfile.NamedTemporaryFile(dir=TARGET.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            os.fchmod(stream.fileno(), 0o755)
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
            os.link(temporary, TARGET)
        finally:
            temporary.unlink(missing_ok=True)
    result = subprocess.run(["/usr/bin/python3", "-I", "-B", str(TARGET), "status"],
                            text=True, capture_output=True, timeout=40, check=False)
    require(result.returncode == 0, "Ubuntu dispatcher не прошёл безопасный status")


def install_policy() -> None:
    parent = POLICY.parent
    require(parent.is_dir() and not parent.is_symlink(), "Не определён каталог APT")
    if POLICY.exists() or POLICY.is_symlink():
        info = POLICY.lstat()
        require(stat.S_ISREG(info.st_mode) and info.st_uid == 0
                and POLICY.read_text(encoding="utf-8") == POLICY_TEXT,
                "Чужая политика APT: не заменять")
        return
    with tempfile.NamedTemporaryFile(dir=parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            os.fchmod(stream.fileno(), 0o644)
            stream.write(POLICY_TEXT.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
            os.link(temporary, POLICY)
        finally:
            temporary.unlink(missing_ok=True)
    output = subprocess.check_output(["/usr/bin/apt-config", "dump"], text=True, timeout=15)
    require('Unattended-Upgrade::Automatic-Reboot "false";' in output,
            "APT не принял запрет автоматической перезагрузки")


def verify_tree(api: PanelAPI) -> None:
    tree = api.request("POST", "/core/commands/tree", {"type": "command"})
    values = {g["label"]: {x["label"]: x["value"] for x in g["children"]} for g in tree}
    for title, body in MENU:
        require(values.get(GROUP, {}).get(title) == body,
                "Команда отсутствует или отличается: " + title)


def apply(api: PanelAPI) -> None:
    groups, commands = snapshot(api)
    guard_existing(groups, commands)
    retained = dict(commands)
    archive = save_snapshot(groups, commands)
    print("MENU_SNAPSHOT", archive)
    install_dispatcher()
    install_policy()
    if GROUP not in groups:
        api.request("POST", "/core/groups", {"id": 0, "type": "command", "name": GROUP})
    groups, current = snapshot(api)
    group_id = groups[GROUP]["id"]
    for title, body in MENU:
        if title not in current:
            api.request("POST", "/core/commands",
                        {"id": 0, "type": "command", "groupID": group_id,
                         "name": title, "command": body})
            current[title] = {"name": title, "groupID": group_id, "command": body}
    _, after = snapshot(api)
    require(all(after.get(title) == row for title, row in retained.items()),
            "Посторонние команды изменились")
    verify_tree(api)
    print("UBUNTU_MENU_ACCEPTED groups=1 commands=5 no_auto_reboot=true")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        identity()
        api = PanelAPI("http://127.0.0.1:19192",
                       read_api_key("/etc/server-settings/panel-domain-publisher.key"),
                       timeout=20)
        require_local_write(api)
        with open("/run/lock/asa-h03-short-commands.lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            groups, commands = snapshot(api)
            guard_existing(groups, commands)
            print("PLAN add_group", int(GROUP not in groups),
                  "add_commands", sum(title not in commands for title, _ in MENU))
            if args.apply:
                apply(api)
            else:
                print("NO_CHANGES")
        return 0
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print("UBUNTU_MENU_FAILED",type(error).__name__,str(error)[:150])
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
