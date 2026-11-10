#!/usr/bin/env python3
"""Replace the managed school 1Panel shortcuts through its local v2 API."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import runpy
import socket
import tempfile

from panelctl import PanelAPI, read_api_key, require_local_write, websites

HOST = "asa-school-01"
ASA = "sudo -- /usr/local/bin/asa"
GFS = "sudo -- /usr/local/bin/asa-school-gfs"
HELPER = Path("/usr/local/sbin/asa-school-operator")
OPERATOR = f"sudo -- {HELPER}"
HISTORY = Path("/var/lib/server-settings/1panel-shortcuts-history")
SCHOOL_PREFIX = 'if [ "$(hostname -s)" = asa-school-01 ]; then '
OLD_GROUPS = {
    "ASA 01 · Состояние и логи": "01",
    "ASA 02 · Обновление и возврат": "02",
    "ASA 03 · Резервные копии": "03",
    "ASA 04 · Восстановление в тест": "04",
}
ARCHIVE = "ASA 90 · Архив старых команд"

MENU = {
    "ASA Lab · Обновление": (
        ("Проверить обновления", f"{OPERATOR} check-update"),
        ("Установить обновление", f"{OPERATOR} install-update"),
        ("Посмотреть план возврата", f"{OPERATOR} rollback-plan"),
    ),
    "ASA Lab · Резервные копии": (
        ("Посмотреть состояние копий", f"{GFS} status"),
        ("Проверить доступ к Яндекс Диску", f"{GFS} check"),
        ("Сделать копию базы", f"{GFS} hourly"),
        ("Сделать полную копию", f"{OPERATOR} backup-full"),
        ("Посмотреть ошибки копирования", f"{GFS} errors --hours 24"),
        ("Восстановить доступ к Яндекс Диску",
         "sudo -- /usr/local/sbin/asa-school-connect yandex-reauthorize"),
    ),
    "ASA Lab · Восстановление": (
        ("Посмотреть архивы на Яндекс Диске", f"{GFS} restore list"),
        ("Скачать архив", f"{GFS} restore download"),
        ("Проверить скачанный архив", f"{GFS} restore prepare"),
        ("Восстановить копию в тестовую базу", f"{GFS} restore restore-test"),
    ),
    "ASA Lab · Состояние и журналы": (
        ("Проверить работу ASA Lab", f"{ASA} status"),
        ("Посмотреть отчёт о сервере", f"{ASA} report"),
        ("Открыть журнал приложения", f"{ASA} logs api --tail 100"),
        ("Открыть журнал Scratch", f"{ASA} logs scratch --tail 100"),
        ("Открыть журнал сборщика",
         "sudo -- /usr/bin/journalctl -u asa-log-collector-asa-lab-school.service -n 80 --no-pager"),
    ),
}


def school(command):
    return SCHOOL_PREFIX + command + "; else echo 'Откройте терминал школьного сервера' >&2; false; fi"


def snapshot(api):
    groups = api.request("POST", "/core/groups/search", {"type": "command"})
    page = api.request("POST", "/core/commands/search", {
        "type": "command", "page": 1, "pageSize": 200, "orderBy": "name",
        "order": "ascending", "info": "",
    })
    rows = page.get("items") or []
    by_group = {row["name"]: row for row in groups}
    by_command = {row["name"]: row for row in rows}
    if len(by_group) != len(groups) or len(by_command) != page["total"] or len(rows) != page["total"]:
        raise SystemExit("Список команд неполный или содержит дубликаты")
    return by_group, by_command


def old_commands(groups, commands, legacy):
    defaults = [row["id"] for row in groups.values() if row["isDefault"]]
    if len(defaults) != 1:
        raise SystemExit("Не найдена стандартная группа команд")
    group_names = {row["id"]: name for name, row in groups.items()}
    owned = {}
    for name, row in commands.items():
        group = group_names.get(row["groupID"])
        if (group in OLD_GROUPS
                and re.match(rf"^{OLD_GROUPS[group]}\.\d{{2}} · ", name)
                and row["command"].startswith(SCHOOL_PREFIX)):
            owned[name] = row
        elif (group == ARCHIVE or row["groupID"] == defaults[0]) and legacy.get(name) == row["command"]:
            owned[name] = row
    return owned


def archive_old(groups, obsolete):
    HISTORY.mkdir(mode=0o700, exist_ok=True)
    if HISTORY.is_symlink() or HISTORY.stat().st_uid != 0 or HISTORY.stat().st_mode & 0o077:
        raise SystemExit("Каталог резервных копий меню должен принадлежать root и иметь права 0700")
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix="menu-", suffix=".json",
                                     dir=HISTORY, delete=False) as stream:
        json.dump({"groups": [g for g in groups.values() if g["name"] in (*OLD_GROUPS, ARCHIVE)],
                   "commands": list(obsolete.values())}, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())


def verify_menu(api, desired):
    tree = api.request("POST", "/core/commands/tree", {"type": "command"})
    visible = {group["label"]: {leaf["label"]: leaf["value"] for leaf in group["children"]}
               for group in tree}
    for name, (group, command) in desired.items():
        if visible.get(group, {}).get(name) != command:
            raise SystemExit(f"Команда отсутствует в меню терминала: {name}")


def reconcile(api, legacy, apply):
    groups, commands = snapshot(api)
    obsolete = old_commands(groups, commands, legacy)
    desired = {name: (group, school(command))
               for group, entries in MENU.items() for name, command in entries}
    for name, (group, command) in desired.items():
        if name in commands and (commands[name]["command"] != command
                                 or commands[name]["groupID"] != groups.get(group, {}).get("id")):
            raise SystemExit(f"Команда с таким именем уже отличается: {name}")
    missing_groups = [group for group in MENU if group not in groups]
    missing = {name: spec for name, spec in desired.items() if name not in commands}
    print(f"groups_to_add={len(missing_groups)} commands_to_add={len(missing)} old_commands_to_remove={len(obsolete)}")
    if not apply:
        return
    if obsolete:
        archive_old(groups, obsolete)
    for name in missing_groups:
        api.request("POST", "/core/groups", {"id": 0, "type": "command", "name": name})
    group_ids = {name: row["id"] for name, row in snapshot(api)[0].items()}
    for name, (group, command) in missing.items():
        api.request("POST", "/core/commands", {
            "id": 0, "type": "command", "groupID": group_ids[group], "name": name, "command": command,
        })
    verify_menu(api, desired)
    if obsolete:
        current_groups, current = snapshot(api)
        still_owned = old_commands(current_groups, current, legacy)
        if any(still_owned.get(name) != row for name, row in obsolete.items()):
            raise SystemExit("Старые записи изменились во время установки меню")
        api.request("POST", "/core/commands/del", {"ids": [row["id"] for row in obsolete.values()]})
    final_groups, final = snapshot(api)
    used_group_ids = {row["groupID"] for row in final.values()}
    for name in (*OLD_GROUPS, ARCHIVE):
        if name in final_groups and final_groups[name]["id"] not in used_group_ids:
            api.request("POST", "/core/groups/del", {"id": final_groups[name]["id"]})
    _, final = snapshot(api)
    if any(name in final for name in obsolete):
        raise SystemExit("Старые записи остались в терминале")
    if any(final.get(name) != row for name, row in commands.items() if name not in obsolete):
        raise SystemExit("Одна из сторонних записей изменилась во время установки меню")
    verify_menu(api, desired)
    print(f"terminal_verified={len(desired)} groups_verified={len(MENU)} old_removed={len(obsolete)}")


def install_helper(source):
    if HELPER.is_symlink() or (HELPER.exists() and (not HELPER.is_file() or HELPER.stat().st_uid != 0)):
        raise SystemExit("Неожиданный файл команды школьного сервера")
    with tempfile.NamedTemporaryFile(dir=HELPER.parent, prefix=".asa-school-operator-",
                                     delete=False) as stream:
        staged = Path(stream.name)
        try:
            stream.write(source.read_bytes())
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), 0o755)
            os.replace(staged, HELPER)
        finally:
            staged.unlink(missing_ok=True)


def main():
    import fcntl

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if os.geteuid() != 0 or socket.gethostname() != HOST:
        raise SystemExit("Требуется root на школьном сервере")
    api = PanelAPI("http://127.0.0.1:19192", read_api_key("/var/lib/server-settings/1panel-api.key"))
    require_local_write(api)
    school_sites = [site for site in websites(api) if site.get("primaryDomain") == "asa-lab.ru"]
    if len(school_sites) != 1 or school_sites[0].get("type") != "proxy" or school_sites[0].get("alias") != "asa-lab-x":
        raise SystemExit("Не подтверждён сайт ASA Lab в школьной 1Panel")
    previous = Path(__file__).with_name("install-school-1panel-backup-shortcuts.py")
    legacy = dict(runpy.run_path(str(previous))["COMMANDS"])
    with open("/run/lock/asa-school-1panel-shortcuts.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if args.apply:
            install_helper(Path(__file__).with_name("asa-school-operator.py"))
        reconcile(api, legacy, args.apply)


if __name__ == "__main__":
    main()
