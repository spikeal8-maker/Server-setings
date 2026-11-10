#!/usr/bin/env python3
"""Install the grouped ASA Lab terminal menu in the school 1Panel.

Run as root on asa-school-01. Without --apply, report the intended changes.
The installer uses the local 1Panel v2 API and never executes the shortcuts.
"""
from __future__ import annotations

import argparse
import fcntl
import os
from pathlib import Path
import runpy
from shlex import join
import socket

from panelctl import PanelAPI, read_api_key, require_local_write, websites

HOST = "asa-school-01"
ASA = "sudo -- /usr/local/bin/asa"
GFS = "sudo -- /usr/local/bin/asa-school-gfs"
ARCHIVE = "ASA 90 · Архив старых команд"
PROOF = "https://github.com/spikeal8-maker/asa-lab/issues/1029#issuecomment-6759834811"
TARGET = "sha256:537aaa66687fd953528b6165998e2e4de61107ea74fe7a00100d99523b7f21ba"
CHECK = f"{ASA} check --require-application-rollback --deployment-digest"
TOOL_CHECKOUT = "/opt/asa-school-release-tools"
TOOLS = f"{TOOL_CHECKOUT}/tools/asa_manager.py"
MANAGER = f"sudo -- /usr/bin/python3 -E -s -B {TOOLS}"
INSTALL = "--root /srv/asa-lab --profile production"
TOOL_CHECK = (
    f"if ! sudo -- /usr/bin/test -f {TOOLS} || "
    f"! issues=$(sudo -- /usr/bin/find -P {TOOL_CHECKOUT} "
    r"\( -type l -o ! -user root -o -perm /022 \) -print -quit) || [[ -n $issues ]]; then "
    "echo 'Сначала администратор должен подготовить закреплённые инструменты возврата.' >&2; exit 2; fi"
)
RELEASE_PROMPT = (
    "read -r -p 'Введите OCI digest допущенного выпуска (sha256:...): ' digest || exit 1; "
    "if [[ ! $digest =~ ^sha256:[0-9a-f]{64}$ ]]; then echo 'Неверный digest' >&2; exit 2; fi"
)


def school(command):
    return (
        f'if [ "$(hostname -s)" = {HOST} ]; then {command}; '
        "else echo 'Откройте терминал школьной VM100' >&2; fi"
    )


def confirmed(prompt, word, command):
    return f"read -r -p '{prompt} ({word}): ' answer && [[ $answer == {word} ]] && {command}"


def bash(command):
    return join(["bash", "-c", command])


MENU = {
    "ASA 01 · Состояние и логи": (
        ("01 · Статус: релиз и контейнеры", f"{ASA} status"),
        ("02 · Отчёт: версия, копия и нагрузка", f"{ASA} report"),
        ("03 · API: последние 100 строк", f"{ASA} logs api --tail 100"),
        ("04 · Scratch: последние 100 строк", f"{ASA} logs scratch --tail 100"),
        ("05 · Журнал сборщика логов: 80 строк",
         "sudo journalctl -u asa-log-collector-asa-lab-school.service -n 80 --no-pager"),
    ),
    "ASA 02 · Обновление и возврат": (
        ("01 · Проверить 312 и возможность возврата", f"{CHECK} {TARGET}"),
        ("02 · Проверить другой выпуск по digest", bash(
            f'{RELEASE_PROMPT}; {TOOL_CHECK}; {MANAGER} check {INSTALL} '
            '--require-application-rollback --deployment-digest "$digest"'
        )),
        ("03 · Установить выпуск: проверка и подтверждение", bash(
            f'{RELEASE_PROMPT}; {TOOL_CHECK}; {MANAGER} check {INSTALL} '
            f'--require-application-rollback --deployment-digest "$digest" && {GFS} status && '
            + confirmed("Свежая копия проверена? Для запуска вне уроков введите", "UPDATE",
                        f'{MANAGER} update {INSTALL} --require-application-rollback --deployment-digest "$digest"')
        )),
        ("04 · План возврата: только проверка", bash(f"{TOOL_CHECK}; {MANAGER} rollback-plan {INSTALL}")),
        ("05 · Справка: статус выпуска 312", join(["printf", "%s\\n",
             "10.11.2026: VM100 — схема 308. На H03 обновление до 312 прошло, возврат заблокирован.",
             "Для школы нужен выпуск, прошедший проверку возврата на H03.", PROOF])),
    ),
    "ASA 03 · Резервные копии": (
        ("01 · Статус копий и расписание", f"{GFS} status"),
        ("02 · Проверить Яндекс и БД без простоя", f"{GFS} check"),
        ("03 · Сделать копию PostgreSQL сейчас", f"{GFS} hourly"),
        ("04 · Полная копия: краткая пауза записи", bash(confirmed(
            "Копия PostgreSQL, MinIO и настроек. Запускать вне уроков. Введите", "FULL", f"{GFS} full"
        ))),
        ("05 · Ошибки резервирования за сутки", f"{GFS} errors --hours 24"),
        ("06 · Восстановить доступ Яндекса при отзыве",
         "sudo -- /usr/local/sbin/asa-school-connect yandex-reauthorize"),
    ),
    "ASA 04 · Восстановление в тест": (
        ("01 · Показать список архивов Яндекса", f"{GFS} restore list"),
        ("02 · Скачать выбранный архив", f"{GFS} restore download"),
        ("03 · Проверить архив с ключом age", f"{GFS} restore prepare"),
        ("04 · Восстановить в отдельную тестовую БД", f"{GFS} restore restore-test"),
    ),
}


def snapshot(api):
    groups = api.request("POST", "/core/groups/search", {"type": "command"})
    page = api.request("POST", "/core/commands/search", {
        "type": "command", "page": 1, "pageSize": 200, "orderBy": "name",
        "order": "ascending", "info": "",
    })
    commands = page.get("items") or []
    by_group = {entry["name"]: entry for entry in groups}
    by_command = {entry["name"]: entry for entry in commands}
    if len(by_group) != len(groups) or len(by_command) != page["total"] or len(commands) != page["total"]:
        raise SystemExit("Список команд неполный или содержит дубликаты")
    return by_group, by_command


def reconcile(api, legacy, apply):
    groups, commands = snapshot(api)
    defaults = [entry["id"] for entry in groups.values() if entry["isDefault"]]
    if len(defaults) != 1:
        raise SystemExit("Не найдена единственная стандартная группа команд")
    archive_id = groups.get(ARCHIVE, {}).get("id")
    for name, command in legacy.items():
        if name in commands and (commands[name]["command"] != command
                                 or commands[name]["groupID"] not in (defaults[0], archive_id)):
            raise SystemExit(f"Старая команда изменена: {name}")
    desired = {
        f"{group[4:6]}.{title}": (group, school(command))
        for group, entries in MENU.items() for title, command in entries
    }
    for name, (group, command) in desired.items():
        if name in commands and (commands[name]["command"] != command
                                 or commands[name]["groupID"] != groups.get(group, {}).get("id")):
            raise SystemExit(f"Команда с таким именем уже отличается: {name}")
    new_groups = [group for group in (*MENU, ARCHIVE) if group not in groups]
    add = {name: spec for name, spec in desired.items() if name not in commands}
    move = [entry for name, entry in commands.items() if name in legacy and entry["groupID"] != archive_id]
    print(f"groups_to_add={len(new_groups)} commands_to_add={len(add)} legacy_to_move={len(move)}")
    if not apply:
        return
    for group in new_groups:
        api.request("POST", "/core/groups", {"id": 0, "type": "command", "name": group})
    group_ids = {name: entry["id"] for name, entry in snapshot(api)[0].items()}
    for name, (group, command) in add.items():
        api.request("POST", "/core/commands", {
            "id": 0, "type": "command", "groupID": group_ids[group], "name": name, "command": command,
        })
    for entry in move:
        api.request("POST", "/core/commands/update", {
            "id": entry["id"], "type": "command", "groupID": group_ids[ARCHIVE],
            "name": entry["name"], "command": entry["command"],
        })
    tree = api.request("POST", "/core/commands/tree", {"type": "command"})
    visible = {group["label"]: {leaf["label"]: leaf["value"] for leaf in group["children"]}
               for group in tree}
    for name, (group, command) in desired.items():
        if visible.get(group, {}).get(name) != command:
            raise SystemExit(f"Команда не появилась в меню терминала: {name}")
    for entry in move:
        if visible.get(ARCHIVE, {}).get(entry["name"]) != entry["command"]:
            raise SystemExit(f"Команда не появилась в архиве: {entry['name']}")
    final = snapshot(api)[1]
    for name, entry in commands.items():
        expected = {**entry}
        if name in legacy:
            expected.update(groupID=group_ids[ARCHIVE], groupBelong=ARCHIVE)
        if final.get(name) != expected:
            raise SystemExit(f"Не совпала запись после изменения: {name}")
    print(f"terminal_verified={len(desired)} legacy_preserved={sum(n in final for n in legacy)}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if os.geteuid() != 0 or socket.gethostname() != HOST:
        raise SystemExit("Требуется root на школьной VM100")
    api = PanelAPI("http://127.0.0.1:19192", read_api_key("/var/lib/server-settings/1panel-api.key"))
    require_local_write(api)
    school_sites = [site for site in websites(api) if site.get("primaryDomain") == "asa-lab.ru"]
    if len(school_sites) != 1 or school_sites[0].get("type") != "proxy" or school_sites[0].get("alias") != "asa-lab-x":
        raise SystemExit("Не подтверждён сайт ASA Lab в школьной 1Panel")
    previous = Path(__file__).with_name("install-school-1panel-backup-shortcuts.py")
    legacy = dict(runpy.run_path(str(previous))["COMMANDS"])
    with open("/run/lock/asa-school-1panel-shortcuts.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        reconcile(api, legacy, args.apply)


if __name__ == "__main__":
    main()
