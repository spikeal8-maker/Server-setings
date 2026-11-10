#!/usr/bin/env python3
"""Replace the managed school 1Panel shortcuts through its local v2 API."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import runpy
import socket
import stat
import subprocess
import tempfile

from panelctl import PanelAPI, read_api_key, require_local_write, websites

HOST = "asa-school-01"
ASA = "sudo -- /usr/local/bin/asa"
LOGS = "sudo -- /usr/bin/python3 -E -s -B /srv/asa-lab/tools/asa_status.py logs"
GFS = "sudo -- /usr/local/bin/asa-school-gfs"
LAUNCHER = Path("/usr/local/bin/asa")
INSTALLATION = Path("/srv/asa-lab/.asa/installation.json")
OFFSITE_RECEIPT = "/var/backups/asa-school-gfs/receipts/latest-full.json"
HISTORY = Path("/var/lib/server-settings/1panel-shortcuts-history")
SCHOOL_PREFIX = 'if [ "$(hostname -s)" = asa-school-01 ]; then '
# SHA-256 of name + NUL + shell body from the previously shipped 7ebd711 menu.
OLD_GROUPS = {
    "ASA 01 · Состояние и логи": (
        "b9efa16f698c0ee17797faea14c85ade494cd0f01605ae6f9dab0939b3a84640",
        "ff837fb35959f1362acbe0cc7a379ee4be318542e628f944de922f6e037424e4",
        "7bd68644b1da94ad79c8af63bb8121d0b77d40f93960deb0c9870d1e0d1ef2e7",
        "2ccb17256642bab0e05e3e153192e640e7fb0be4f601aea22b6b0ecbcd425b15",
        "2db2a4e5d51aef4af2a22e3993aa607fca25b99a1f8fa569178c068259c41f15",
    ),
    "ASA 02 · Обновление и возврат": (
        "84f62db5962c43689df89c669dd255ba3d18b7e4a8129d3799635a1c106e721c",
        "7e28e0aa7155994fe7b1945fa151f61e9072bd08b47fd053ebce5c8f0330f315",
        "73f73218cd2a46cbb21d3152a924242129b1552f0c4890b8a5441cde8236bce2",
        "94b3ee1a2d301729db9373037390110b3271a936870db1f8724a9a123695d7f4",
        "44753c90fb0a807d5670d7f561bc7c3ae92239398c444b7f6ab5ee24414e2d0f",
    ),
    "ASA 03 · Резервные копии": (
        "f08b46a0651d698f81281fa32b5b3ca0e9cd304e16aa3ef4f7282b342e6ec0af",
        "51de7adc11b0883399f1a285cd510481def0881d2561313202a738e8e633a78e",
        "de7a2ab10316f870f83cec91ef5fc70c2ca4a8ed946ff531e3fdbad261168a8d",
        "5a5224dbd73cd55e6adc4c1853bd8bec19d38c08e1598409e8303be7f701f870",
        "a18d75a0135f1fe941061bb408b6a094e9afa37ec18e559fc3fa94e22bdd0656",
        "8b04b5988df28ede03e2c60b882a75aa56dbc3146bfb231b521d85d1f61070ae",
    ),
    "ASA 04 · Восстановление в тест": (
        "9369045dadc418f65c93070826a1c387619f9845f1286bab6ec8a04305b18f5a",
        "1672e4559c9304e6a7c03ccab833d7409faf6b08caa45ac166f1560cce3fccb4",
        "af6c060c12a90e8fbb9c4ee88791c68a7d65faffd39a14c38c4f77142a0527a9",
        "cc55078bb3da70a2cf41ef508d20e5c6146ea5b4e256a0b1de6dda2cfad61647",
    ),
}
ARCHIVE = "ASA 90 · Архив старых команд"
UPDATE_GROUP = "ASA Lab · Обновление"
RETIRED_RETURN = "Посмотреть план возврата"
PREVIOUS_COMMANDS = {
    "Проверить обновления": "sudo -- /usr/local/sbin/asa-school-operator check-update",
    "Установить обновление": "sudo -- /usr/local/sbin/asa-school-operator install-update",
    "Сделать полную копию": "sudo -- /usr/local/sbin/asa-school-operator backup-full",
    "Открыть журнал приложения": f"{ASA} logs api --tail 100",
    "Открыть журнал Scratch": f"{ASA} logs scratch --tail 100",
}

MENU = {
    UPDATE_GROUP: (
        ("Проверить обновления", f"{ASA} check"),
        ("Установить обновление", f"{ASA} update"),
    ),
    "ASA Lab · Резервные копии": (
        ("Посмотреть состояние копий", f"{GFS} status"),
        ("Проверить доступ к Яндекс Диску", f"{GFS} check"),
        ("Сделать копию базы", f"{GFS} hourly"),
        ("Сделать полную копию", "printf '%s\\n' 'Полная копия временно прервёт работу ASA Lab. Введите ДА:'; "
         f'if IFS= read -r answer && [ "$answer" = ДА ]; then {GFS} full; else echo "Операция отменена."; fi'),
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
        ("Открыть журнал приложения", f"{LOGS} api --root /srv/asa-lab --tail 100"),
        ("Открыть журнал Scratch", f"{LOGS} scratch --root /srv/asa-lab --tail 100"),
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
        fingerprint = hashlib.sha256((name + "\0" + row["command"]).encode()).hexdigest()
        if fingerprint in OLD_GROUPS.get(group, ()):
            owned[name] = row
        elif group == UPDATE_GROUP and name == RETIRED_RETURN and row["command"] in (
                school("sudo -- /usr/local/sbin/asa-school-operator rollback-plan"), school(f"{ASA} rollback-plan")):
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
        json.dump({"groups": [g for g in groups.values() if g["name"] in (*OLD_GROUPS, *MENU, ARCHIVE)],
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
    replacements = {}
    for name, (group, command) in desired.items():
        if name not in commands:
            continue
        entry = commands[name]
        if entry["groupID"] != groups.get(group, {}).get("id"):
            raise SystemExit(f"Команда с таким именем уже отличается: {name}")
        if entry["command"] != command:
            if name not in PREVIOUS_COMMANDS or entry["command"] != school(PREVIOUS_COMMANDS[name]):
                raise SystemExit(f"Команда с таким именем уже отличается: {name}")
            replacements[name] = entry
    missing_groups = [group for group in MENU if group not in groups]
    missing = {name: spec for name, spec in desired.items() if name not in commands}
    print(f"groups_to_add={len(missing_groups)} commands_to_add={len(missing)} old_commands_to_remove={len(obsolete)} "
          f"commands_to_update={len(replacements)}")
    if not apply:
        return
    if obsolete or replacements:
        archive_old(groups, {**obsolete, **replacements})
    for name in missing_groups:
        api.request("POST", "/core/groups", {"id": 0, "type": "command", "name": name})
    group_ids = {name: row["id"] for name, row in snapshot(api)[0].items()}
    for name, (group, command) in missing.items():
        api.request("POST", "/core/commands", {
            "id": 0, "type": "command", "groupID": group_ids[group], "name": name, "command": command,
        })
    if replacements:
        current = snapshot(api)[1]
        for name, entry in replacements.items():
            if current.get(name) != entry:
                raise SystemExit(f"Команда изменилась во время установки меню: {name}")
            api.request("POST", "/core/commands/update", {
                "id": entry["id"], "type": "command", "groupID": entry["groupID"],
                "name": name, "command": desired[name][1],
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
    if any(final.get(name) != row for name, row in commands.items()
           if name not in obsolete and name not in replacements):
        raise SystemExit("Одна из сторонних записей изменилась во время установки меню")
    verify_menu(api, desired)
    print(f"terminal_verified={len(desired)} groups_verified={len(MENU)} old_removed={len(obsolete)}")


def require_canonical_updater():
    message = "Сначала подключите постоянную команду ASA и школьную проверку облачной копии"
    try:
        for path in (LAUNCHER, INSTALLATION):
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                raise SystemExit(message)
        settings = json.loads(INSTALLATION.read_text(encoding="utf-8"))
        if (settings.get("profile") != "production" or settings.get("project") != "asa-lab-school"
                or settings.get("updatePolicy") != {"receipt": OFFSITE_RECEIPT}):
            raise SystemExit(message)
        check = subprocess.run([str(LAUNCHER), "--launcher-verify", INSTALLATION.parents[1].as_posix()],
                               capture_output=True, encoding="utf-8", check=True, timeout=30)
        if check.stdout.strip() != "ASA_LAUNCHER_FORWARD_V1":
            raise SystemExit(message)
    except (OSError, ValueError, AttributeError, subprocess.SubprocessError) as exc:
        raise SystemExit(message) from exc


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
            require_canonical_updater()
        reconcile(api, legacy, args.apply)


if __name__ == "__main__":
    main()
