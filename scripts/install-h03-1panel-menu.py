#!/usr/bin/env python3
"""Safely reconcile H03 ASA LAB quick commands in the actual 1Panel API.

Only the known ASA entries are managed. External commands, websites, OpenResty
and the application databases are never changed. Default is read-only plan.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import tempfile

from panelctl import PanelAPI, read_api_key, require_local_write

HOST = "admins-MS-7D22"
SOURCE = Path(__file__).with_name("h03_asa_menu.py")
TARGET = Path("/usr/local/libexec/asa-h03/asa-menu.py")
HISTORY = Path("/var/lib/server-settings/h03-1panel-menu-history")
API_KEY = "/etc/server-settings/panel-domain-publisher.key"
API_URL = "http://127.0.0.1:19192"
BASE_GROUP = "ASA Lab · Обновление"
# H03 mitigation until the upstream state writer from issue #1222 is released:
# sudo resets umask to 0002 for admins; set 0077 AFTER switching user.
# The trusted root-owned launcher and all of its checks are preserved.
LEGACY_UPDATE_COMMAND = "sudo asa-test update"
PREVIOUS_HARDENED_UPDATE_COMMAND = (
    "sudo -n -H -u admins -- /bin/sh -c "
    "'umask 0077; exec /usr/local/bin/asa-test update'"
)
# User service collector must connect to admins' lingering systemd user bus.
# sudo -u does not preserve XDG_RUNTIME_DIR/DBUS_SESSION_BUS_ADDRESS.
# Only this pinned H03 user (UID 1000) and private local bus are accepted.
HARDENED_UPDATE_COMMAND = (
    "sudo -n -H -u admins -- /bin/sh -c "
    "'umask 0077; "
    "export XDG_RUNTIME_DIR=/run/user/1000 "
    "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus; "
    "exec /usr/local/bin/asa-test update'"
)
LEGACY_SHA256 = {
    "Обновление": "7e9f5399c27405a93feda3fc956094446eb2e984499f6e6128b731143bee0fe0",
    "Статус обновлений": "9c89b79100d7c4bc7b193e3c66f739a63543b09e4f1a4540499f58c1549fc193",
    "обновлени Asa-Lab": "7900bc0eedae505a3e4208a7c7ab9a2aee31f5f83bc79adfc1bc6da5b083ae0a",
}
LEGACY_GROUP = "Default"


def menu_call(action: str) -> str:
    return "sudo -- /usr/bin/python3 -I -B /usr/local/libexec/asa-h03/asa-menu.py " + action


MENU = {
    BASE_GROUP: (
        ("Проверить обновления", "sudo asa-test check"),
        ("Установить обновление", HARDENED_UPDATE_COMMAND),
    ),
    "ASA Lab · Резервные копии": (
        ("Состояние резервных копий", menu_call("backups-status")),
        ("Проверить Яндекс.Диск", menu_call("cloud-check")),
        ("Создать локальную полную копию", menu_call("backup-local")),
        ("Создать зашифрованную облачную копию", menu_call("backup-cloud")),
        ("Проверить локальную копию", menu_call("backups-verify")),
        ("Состояние расписания копий", menu_call("backup-timer")),
    ),
    "ASA Lab · Восстановление": (
        ("Посмотреть локальные архивы", menu_call("backups-list")),
        ("Архивы на Яндекс.Диске", menu_call("cloud-list")),
        ("Скачать зашифрованный архив", menu_call("cloud-download")),
        ("Восстановить в отдельную тестовую БД", menu_call("restore-test")),
    ),
    "ASA Lab · Состояние и журналы": (
        ("Состояние ASA LAB", menu_call("status")),
        ("Отчёт о сервере", menu_call("report")),
        ("Диагностика без изменений", menu_call("doctor")),
        ("Журнал API", menu_call("logs-api")),
        ("Журнал Scratch", menu_call("logs-scratch")),
    ),
}


class MenuConflict(RuntimeError):
    pass


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise MenuConflict(message)


def identity() -> None:
    ensure(os.geteuid() == 0 and socket.gethostname() == HOST, "Требуется root на тестовом H03")
    for path in (SOURCE, SOURCE.parent, TARGET.parent):
        info = path.lstat()
        ensure(info.st_uid == 0 and info.st_mode & 0o022 == 0, "Небезопасный путь " + str(path))
    ensure(SOURCE.is_file() and not SOURCE.is_symlink(), "Не найдена утверждённая программа меню")
    ensure(TARGET.parent.is_dir() and not TARGET.parent.is_symlink(), "Каталог локальных команд недоступен")
    record = json.loads(Path("/srv/asa-lab/.asa/installation.json").read_text(encoding="utf-8"))
    ensure(record.get("profile") == "production" and record.get("project") == "asa-lab-dev",
           "Неверная установка ASA LAB")
    verify = subprocess.run(["/usr/bin/sudo", "-n", "/usr/local/bin/asa-test", "--launcher-verify",
                             "/srv/asa-lab"], check=True, text=True, capture_output=True, timeout=30)
    ensure(verify.stdout.strip() == "ASA_LAUNCHER_FORWARD_V1", "Не подтверждён обновлятор H03")


def get_snapshot(api: PanelAPI) -> tuple[dict, dict]:
    groups = api.request("POST", "/core/groups/search", {"type": "command"})
    page = api.request("POST", "/core/commands/search", {
        "type": "command", "page": 1, "pageSize": 200, "orderBy": "name",
        "order": "ascending", "info": "",
    })
    ensure(isinstance(groups, list) and isinstance(page, dict), "Не удалось получить меню")
    entries = page.get("items") or []
    ensure(page.get("total") == len(entries) and len(entries) <= 200, "Список команд неполный")
    by_group = {row["name"]: row for row in groups}
    by_command = {row["name"]: row for row in entries}
    ensure(len(by_group) == len(groups) and len(by_command) == len(entries), "Конфликт имён")
    return by_group, by_command


def managed_commands(groups: dict, commands: dict) -> tuple[dict, dict]:
    desired = {title: (group, body) for group, entries in MENU.items() for title, body in entries}
    ensure(len(desired) == sum(len(entries) for entries in MENU.values()),
           "Повторное имя команды в спецификации")
    by_id = {row["id"]: name for name, row in groups.items()}
    for name, (group, body) in desired.items():
        if name in commands:
            actual = commands[name]
            valid_body = actual["command"] == body or (
                name == "Установить обновление" and actual["command"] in (
                    LEGACY_UPDATE_COMMAND, PREVIOUS_HARDENED_UPDATE_COMMAND
                )
            )
            ensure(by_id.get(actual["groupID"]) == group and valid_body,
                   "Действующая команда отличается от утверждённой: " + name)
    retired = {}
    for name, digest in LEGACY_SHA256.items():
        if name not in commands:
            continue
        actual = commands[name]
        computed = hashlib.sha256(actual["command"].encode("utf-8")).hexdigest()
        ensure(by_id.get(actual["groupID"]) == LEGACY_GROUP and computed == digest,
               "Неизвестный владелец или изменённая старая команда: " + name)
        retired[name] = actual
    return desired, retired


def save_snapshot(groups: dict, commands: dict) -> Path:
    HISTORY.mkdir(mode=0o700, exist_ok=True)
    info = HISTORY.lstat()
    ensure(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and
           stat.S_IMODE(info.st_mode) == 0o700, "Небезопасный архив меню")
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=HISTORY,
        prefix="h03-menu-" + timestamp + "-", suffix=".json",
        delete=False,
    ) as out:
        os.fchmod(out.fileno(), 0o600)
        json.dump({"time": timestamp, "groups": list(groups.values()),
                   "commands": list(commands.values())}, out, ensure_ascii=False, indent=2)
        out.write("\n")
        out.flush()
        os.fsync(out.fileno())
        return Path(out.name)


def install_dispatcher() -> None:
    data = SOURCE.read_bytes()
    if TARGET.exists() or TARGET.is_symlink():
        info = TARGET.lstat()
        ensure(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022,
               "Обнаружен чужой исполнитель меню")
        ensure(TARGET.read_bytes() == data, "Существующий исполнитель отличается")
        return
    with tempfile.NamedTemporaryFile(dir=TARGET.parent, delete=False) as out:
        temporary = Path(out.name)
        try:
            os.fchmod(out.fileno(), 0o755)
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
            os.link(temporary, TARGET)
        finally:
            temporary.unlink(missing_ok=True)
    result = subprocess.run(
        ["/usr/bin/python3", "-I", "-B", str(TARGET), "self-test"],
        check=True, text=True, capture_output=True, timeout=35,
    )
    ensure("H03_MENU_RUNTIME_OK" in result.stdout, "Исполнитель меню не прошёл проверку")


def verify_tree(api: PanelAPI, desired: dict) -> None:
    tree = api.request("POST", "/core/commands/tree", {"type": "command"})
    visible = {row["label"]: {child["label"]: child["value"] for child in row["children"]}
               for row in tree}
    for title, (group, body) in desired.items():
        ensure(visible.get(group, {}).get(title) == body, "Команда не отображается: " + title)


def apply(api: PanelAPI) -> None:
    groups, current = get_snapshot(api)
    desired, obsolete = managed_commands(groups, current)
    missing_groups = [name for name in MENU if name not in groups]
    missing_commands = [title for title in desired if title not in current]
    update_candidates = {
        title: (group, body)
        for title, (group, body) in desired.items()
        if title in current and current[title]["command"] != body
    }
    ensure(set(update_candidates).issubset({"Установить обновление"}),
           "Отказ от неожиданного редактирования существующих команд")
    print("PLAN groups_to_add=", len(missing_groups),
          "commands_to_add=", len(missing_commands),
          "commands_to_update=", len(update_candidates),
          "legacy_to_archive=", len(obsolete))
    archived = save_snapshot(groups, current)
    print("OWNER_MENU_BACKUP", archived)
    install_dispatcher()
    for group in missing_groups:
        api.request("POST", "/core/groups", {"id": 0, "type": "command", "name": group})
    new_groups, new_current = get_snapshot(api)
    for title in missing_commands:
        group, body = desired[title]
        ensure(title not in new_current, "Конкурирующее изменение команды " + title)
        api.request("POST", "/core/commands", {
            "id": 0, "type": "command", "groupID": new_groups[group]["id"],
            "name": title, "command": body,
        })
    for title, (group, body) in update_candidates.items():
        old = current[title]
        ensure(new_current.get(title) == old,
               "Конкурирующее изменение команды " + title)
        api.request("POST", "/core/commands/update", {
            "id": old["id"], "type": old["type"],
            "groupID": new_groups[group]["id"],
            "name": title, "command": body,
        })
    verify_tree(api, desired)
    _, before_delete = get_snapshot(api)
    for title, previous in obsolete.items():
        ensure(before_delete.get(title) == previous, "Старая команда изменилась: " + title)
    if obsolete:
        api.request("POST", "/core/commands/del", {"ids": [row["id"] for row in obsolete.values()]})
    final_groups, final_commands = get_snapshot(api)
    final_desired, retired = managed_commands(final_groups, final_commands)
    ensure(not retired, "Старые команды не удалены")
    ensure(all(title in final_commands for title in final_desired), "Неполное меню")
    ensure(all(final_commands.get(title) == row for title, row in current.items()
               if title not in obsolete and title not in update_candidates),
           "Посторонние команды изменены")
    verify_tree(api, desired)
    print("MENU_VERIFIED", "groups", len(MENU), "commands", len(desired),
          "old_removed", len(obsolete))


def main() -> None:
    import fcntl
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Apply the exact reviewed plan")
    args = parser.parse_args()
    identity()
    api = PanelAPI(API_URL, read_api_key(API_KEY), timeout=20)
    require_local_write(api)
    with open("/run/lock/asa-h03-short-commands.lock", "a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        groups, current = get_snapshot(api)
        desired, obsolete = managed_commands(groups, current)
        print("PLAN_ONLY" if not args.apply else "APPLY",
              "add_groups", len([g for g in MENU if g not in groups]),
              "add_commands", len([n for n in desired if n not in current]),
              "retire_legacy", len(obsolete),
              "update_commands", sum(
                  title in current and current[title]["command"] != body
                  for title, (group, body) in desired.items()
              ))
        if args.apply:
            apply(api)
        else:
            print("No changes made")


if __name__ == "__main__":
    main()
