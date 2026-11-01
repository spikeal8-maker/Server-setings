#!/usr/bin/env python3
"""Register ASA backup shortcuts through the official 1Panel v2 core API.

1Panel 2.3.2 stores quick commands as Name+Command+GroupID; there is no
separate description field. Therefore these shortcuts have numbered Russian
self-documenting names, which sort to the top of the native terminal list.

Do not edit core.db directly. Preserve unrelated commands/groups. Each
command uses the VM100 host-locked CLI. Restore never overwrites live data.
"""
from __future__ import annotations
import argparse
import importlib.util
import os
from pathlib import Path
import socket
import sys

PANELCTL=Path("/srv/Server-setings/scripts/panelctl.py")
KEY=Path("/var/lib/server-settings/1panel-api.key")
PANEL="http://127.0.0.1:19192"
HOST="asa-school-01"

COMMANDS=(
    ("00 · ASA Lab | СРОЧНЫЙ бэкап БД+MinIO (в любое время)",
     "sudo asa-school-backup-now"),
    ("01 · ASA Lab | Проверить Яндекс перед копией (без простоя)",
     "sudo asa-school-backup-now --check"),
    ("02 · ASA Lab | Архивы Яндекс — список (новые и старые)",
     "sudo asa-school-restore list"),
    ("03 · ASA Lab | Скачать выбранный архив (боевую БД не трогает)",
     "sudo asa-school-restore download"),
    ("04 · ASA Lab | Подготовить восстановление и сверить архив",
     "sudo asa-school-restore prepare"),
    ("05 · ASA Lab | Пробное восстановление в новую тестовую БД",
     "sudo asa-school-restore restore-test"),
    ("06 · ASA Lab | Статус Яндекса, расписания и последней копии",
     "sudo asa-school-backup-now --status"),
    ("07 · ASA Lab | Когда следующий ночной бэкап (03:20 МСК)",
     "systemctl list-timers --all asa-school-nightly-database-backup.timer"),
    ("08 · ASA Lab | Журнал ночных резервных копий",
     "sudo journalctl -u asa-school-nightly-database-backup.service -n 80 --no-pager"),
    ("09 · ASA Lab | Повторный вход в Яндекс при отзыве токена",
     "sudo asa-school-connect yandex-reauthorize"),
    ("10 · ASA Lab | Помощь: ручной бэкап и восстановление",
     "sudo asa-school-backup-now --help"),
)


class ShortcutError(Exception):
    pass


def require_host():
    if os.geteuid()!=0 or socket.gethostname()!=HOST:
        raise ShortcutError("ROOT_ON_VM100_REQUIRED")
    for p in (PANELCTL,KEY,Path("/usr/local/sbin/asa-school-backup-now"),
              Path("/usr/local/sbin/asa-school-restore")):
        if p.is_symlink() or not p.is_file():
            raise ShortcutError("QUICK_COMMAND_PREREQUISITE_MISSING")
    if len({name for name,_ in COMMANDS})!=len(COMMANDS):
        raise ShortcutError("DUPLICATED_QUICK_COMMANDS")


def client():
    spec=importlib.util.spec_from_file_location("official_school_panel",PANELCTL)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    key=module.read_api_key(str(KEY))
    api=module.PanelAPI(PANEL,key)
    module.require_local_write(api)
    sites=module.websites(api)
    if len(sites)!=1 or sites[0].get("primaryDomain")!="x.asa-lab.ru":
        raise ShortcutError("1PANEL_VM100_IDENTITY_UNVERIFIED")
    return api


def current(api):
    groups=api.request("POST","/core/groups/search",{"type":"command"})
    if not isinstance(groups,list):
        raise ShortcutError("1PANEL_COMMAND_GROUPS_UNAVAILABLE")
    defaults=[g for g in groups if g.get("isDefault") is True
              and g.get("type")=="command" and type(g.get("id")) is int]
    if len(defaults)!=1:
        raise ShortcutError("1PANEL_DEFAULT_COMMAND_GROUP_MISMATCH")
    search=api.request("POST","/core/commands/search",{
        "page":1,"pageSize":200,"type":"command",
        "orderBy":"name","order":"ascending","info":"",
    })
    if not isinstance(search,dict) or type(search.get("total")) is not int:
        raise ShortcutError("1PANEL_COMMAND_SEARCH_BAD_RESPONSE")
    commands=search.get("items") or []
    if search["total"]!=len(commands) or search["total"]>200:
        raise ShortcutError("1PANEL_COMMAND_LIST_INCOMPLETE")
    return defaults[0]["id"],commands


def plan(api):
    group,existing=current(api)
    byname={entry.get("name"):entry for entry in existing}
    if len(byname)!=len(existing):
        raise ShortcutError("QUICK_COMMAND_NAME_COLLISION")
    add=[]
    for name,command in COMMANDS:
        value=byname.get(name)
        if value is None:
            add.append((name,command))
            continue
        if (value.get("command")!=command or value.get("groupID")!=group
                or value.get("type")!="command"):
            raise ShortcutError("EXISTING_NAMED_SHORTCUT_DIFFERENT")
    print("1PANEL_NATIVE_QUICK_COMMANDS_TOTAL="+str(len(existing)))
    print("NEW_VM100_SHORTCUTS_TO_ADD="+str(len(add)))
    print("ALREADY_MATCHING="+str(len(COMMANDS)-len(add)))
    print("SORTED_BY_NUMERIC_NAME_PREFIX=00_TO_10")
    print("EXISTING_UNRELATED_COMMANDS_PRESERVED=YES")
    return group,add


def apply(api):
    group,toadd=plan(api)
    created=[]
    try:
        for name,command in toadd:
            api.request("POST","/core/commands",{
                "id":0,"type":"command","groupID":group,
                "name":name,"command":command,
            })
            _,listnow=current(api)
            mine=[c for c in listnow if c.get("name")==name and
                  c.get("command")==command and c.get("groupID")==group]
            if len(mine)!=1:
                raise ShortcutError("1PANEL_QUICK_COMMAND_NOT_SAVED")
            created.append(mine[0]["id"])
        _,final=current(api)
        names={c["name"]:c for c in final}
        for name,command in COMMANDS:
            if name not in names or names[name]["command"]!=command:
                raise ShortcutError("1PANEL_QUICK_COMMANDS_FINAL_VERIFY_FAILED")
        print("1PANEL_QUICK_COMMANDS_INSTALLED="+str(len(COMMANDS)))
        print("1PANEL_TERMINAL_COMMANDS_SAVED_THROUGH_OFFICIAL_API=YES")
        print("1PANEL_CORE_DB_DIRECT_EDIT=NO")
        print("PRODUCTION_DATABASE_UNCHANGED=YES")
    except BaseException:
        # Roll back *only* the IDs created by this invocation.
        if created:
            try:api.request("POST","/core/commands/del",{"ids":created})
            except Exception:pass
        raise


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--apply",action="store_true")
    args=p.parse_args()
    try:
        require_host()
        api=client()
        if args.apply:apply(api)
        else:
            plan(api)
            for name,command in COMMANDS:
                print(name+"  =>  "+command)
        return 0
    except (ShortcutError,OSError,ValueError) as error:
        reason=str(error) if isinstance(error,ShortcutError) else type(error).__name__
        print("1PANEL_QUICK_COMMANDS_BLOCKED="+reason,file=sys.stderr)
        return 2


if __name__=="__main__":
    raise SystemExit(main())
