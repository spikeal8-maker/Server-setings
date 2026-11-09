#!/usr/bin/env python3
"""ASA School VM100 backup operator: manual copies, latest status and errors."""
from __future__ import annotations

import argparse
from collections import Counter, deque
import datetime as dt
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys
from zoneinfo import ZoneInfo

HOST = "asa-school-01"
BASE = Path("/opt/asa-school-gfs/releases")
RECEIPTS = Path("/var/backups/asa-school-gfs/receipts")
LEGACY_SEED = Path("/var/backups/asa-school-nightly/ASA-Lab-Full-Server-seed.json")
MOSCOW = ZoneInfo("Europe/Moscow")
TIMER = {
    "hourly": "asa-school-gfs-hourly.timer",
    "full": "asa-school-gfs-full.timer",
}
SERVICE = {
    "hourly": "asa-school-gfs-hourly.service",
    "full": "asa-school-gfs-full.service",
}
RUNNER = re.compile(r"/opt/asa-school-gfs/releases/([0-9a-f]{40})/school-gfs-backup[.]py")
ERROR_CODE = re.compile(r"GFS_(?:BLOCKED|AUDIT_WRITE_FAILED)=([A-Za-z][A-Za-z0-9_]{0,79})")


class OperatorError(RuntimeError):
    pass


def require(condition: bool, code: str) -> None:
    if not condition:
        raise OperatorError(code)


def run_readonly(args: list[str], timeout: int = 12) -> str:
    try:
        result = subprocess.run(args, capture_output=True, text=True,
                                check=False, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        raise OperatorError("SYSTEM_QUERY_UNAVAILABLE") from None
    require(result.returncode == 0, "SYSTEM_QUERY_FAILED")
    return result.stdout


def read_json(path: Path) -> dict | None:
    try:
        require(path.is_file() and not path.is_symlink(), "RECEIPT_UNAVAILABLE")
        obj = json.loads(path.read_text(encoding="utf-8"))
        require(isinstance(obj, dict), "RECEIPT_INVALID")
        return obj
    except (OSError, ValueError, OperatorError):
        return None


def stamp(value: str | None) -> str:
    if not isinstance(value, str) or not value:
        return "нет данных"
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        require(parsed.tzinfo is not None, "TIMEZONE_MISSING")
        return parsed.astimezone(MOSCOW).strftime("%d.%m.%Y %H:%M МСК")
    except (ValueError, OperatorError):
        return "неверная дата"


def unit_properties(unit: str) -> dict:
    out = run_readonly(["systemctl", "show", unit, "--no-pager",
                        "-p", "LoadState", "-p", "ActiveState",
                        "-p", "UnitFileState", "-p", "Result",
                        "-p", "ExecMainStatus", "-p", "NextElapseUSecRealtime",
                        "-p", "LastTriggerUSec", "-p", "ExecStart"])
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def current_runner() -> tuple[str, Path]:
    releases = []
    for kind in ("hourly", "full"):
        properties = unit_properties(SERVICE[kind])
        require(properties.get("LoadState") == "loaded", "GFS_SERVICE_NOT_INSTALLED")
        matches = RUNNER.findall(properties.get("ExecStart", ""))
        require(len(matches) == 1, "GFS_PIN_NOT_IDENTIFIED")
        releases.append(matches[0])
    require(releases[0] == releases[1], "GFS_SERVICES_ON_DIFFERENT_RELEASES")
    sha = releases[0]
    runner = BASE / sha / "school-gfs-backup.py"
    require(runner.is_file() and not runner.is_symlink(), "GFS_RUNNER_MISSING")
    info = runner.stat()
    require(info.st_uid == 0 and stat.S_ISREG(info.st_mode), "GFS_RUNNER_UNTRUSTED")
    return sha, runner


def recent_events(limit: int = 4000) -> list[dict]:
    path = RECEIPTS / "events.jsonl"
    if not path.exists():
        return []
    require(not path.is_symlink() and path.is_file(), "GFS_EVENTS_UNTRUSTED")
    require(path.stat().st_uid == os.geteuid() and stat.S_IMODE(path.stat().st_mode) == 0o600,
            "GFS_EVENTS_PERMISSIONS")
    buffer = deque(maxlen=limit)
    try:
        with path.open("r", encoding="utf-8") as source:
            for line in source:
                require(len(line) <= 2048, "GFS_EVENT_LINE_OVERSIZED")
                buffer.append(line)
    except OSError:
        raise OperatorError("GFS_EVENTS_UNREADABLE") from None
    entries = []
    for line in buffer:
        try:
            obj = json.loads(line)
            if (isinstance(obj, dict) and obj.get("format") == "asa-school-gfs-event-v1"
                    and obj.get("kind") in ("hourly", "full")
                    and obj.get("outcome") in ("success", "failure")
                    and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", str(obj.get("code", "")))):
                entries.append({key: obj.get(key) for key in
                                ("at", "kind", "trigger", "outcome", "code")})
        except ValueError:
            continue
    return entries


def recent_failures(hours: int, limit: int = 15) -> tuple[int, list[dict]]:
    require(1 <= hours <= 24 * 365, "INVALID_HOURS")
    threshold = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    failures = []
    for entry in recent_events():
        if entry.get("outcome") != "failure":
            continue
        try:
            event_at = dt.datetime.fromisoformat(str(entry["at"]).replace("Z", "+00:00"))
            if event_at.tzinfo is not None and event_at >= threshold:
                failures.append(entry)
        except (ValueError, TypeError):
            continue
    return len(failures), failures[-limit:]


def status_report() -> dict:
    sha, _ = current_runner()
    items = {}
    for kind in ("hourly", "full"):
        receipt = read_json(RECEIPTS / ("latest-" + kind + ".json"))
        confirmed = bool(receipt and receipt.get("remoteFullReadbackSha256") is True
                         and isinstance(receipt.get("remote"), dict)
                         and receipt.get("remote"))
        properties = unit_properties(TIMER[kind])
        items[kind] = {
            "lastVerified": receipt.get("createdAt") if confirmed else None,
            "archive": receipt.get("archive") if confirmed else None,
            "remote": sorted(receipt.get("remote", {})) if confirmed else [],
            "timerEnabled": properties.get("UnitFileState") == "enabled",
            "timerActive": properties.get("ActiveState") == "active",
            "nextRun": properties.get("NextElapseUSecRealtime"),
            "lastTrigger": properties.get("LastTriggerUSec"),
        }
    seed = read_json(LEGACY_SEED)
    fail_count, failures = recent_failures(24, 1)
    return {
        "host": HOST, "sourceSha": sha, "hourly": items["hourly"],
        "full": items["full"],
        "initialSeed": {
            "at": seed.get("createdAt"), "archive": seed.get("sourceArchive"),
            "verified": seed.get("verification") == "full-remote-download-sha256",
        } if seed else None,
        "failures24h": fail_count, "lastFailure": failures[-1] if failures else None,
        "errorHistoryStarted": (RECEIPTS / "events.jsonl").exists(),
    }


def read_journal_failures(hours: int, limit: int = 15) -> list[dict]:
    require(1 <= hours <= 24 * 365, "INVALID_HOURS")
    cmd = ["journalctl", "-u", SERVICE["hourly"], "-u", SERVICE["full"],
           "--since", f"{hours} hours ago", "--no-pager", "-o", "json"]
    try:
        output = run_readonly(cmd, timeout=20)
    except OperatorError:
        return []
    results = deque(maxlen=limit)
    for line in output.splitlines():
        try:
            record = json.loads(line)
            message = str(record.get("MESSAGE", ""))
            found = ERROR_CODE.search(message)
            if found:
                kind = "hourly" if "hourly" in str(record.get("_SYSTEMD_UNIT", "")) else "full"
                micros = int(record.get("__REALTIME_TIMESTAMP", 0))
                when = dt.datetime.fromtimestamp(micros / 1_000_000, tz=dt.timezone.utc)
                results.append({"at": when.isoformat(), "kind": kind,
                                "code": found.group(1), "origin": "systemd"})
        except (ValueError, TypeError, OverflowError):
            continue
    return list(results)


def show_status(*, as_json: bool = False) -> int:
    result = status_report()
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("ASA Lab • школьное резервирование •", result["host"])
        print("Код в GitHub SHA:", result["sourceSha"])
        for kind, label in (("hourly", "Почасовая БД"), ("full", "Полная БД + MinIO")):
            item = result[kind]
            print(f"{label}:",
                  stamp(item["lastVerified"]) if item["lastVerified"] else "ещё нет подтверждённого запуска",
                  ("| " + item["archive"]) if item["archive"] else "")
            print("  Расписание:", "активно" if item["timerEnabled"] and item["timerActive"]
                  else "НЕ АКТИВНО",
                  "| следующее:", item["nextRun"] or "неизвестно")
        seed = result.get("initialSeed")
        if seed and seed["verified"]:
            print("Начальная полная копия (перенос):", stamp(seed["at"]),
                  "|", seed["archive"])
        print("Ошибок за 24 ч (в новом журнале):", result["failures24h"])
        if not result["errorHistoryStarted"]:
            print("История нового журнала ещё не начата; системные ошибки см. в errors")
        if result["lastFailure"]:
            print("Последняя ошибка:", stamp(result["lastFailure"]["at"]),
                  result["lastFailure"]["kind"], result["lastFailure"]["code"])
    return 0 if all(result[k]["timerEnabled"] and result[k]["timerActive"]
                    for k in ("hourly", "full")) else 2


def show_errors(hours: int, *, as_json: bool = False) -> int:
    count, events = recent_failures(hours, 20)
    journals = read_journal_failures(hours, 15)
    result = {"windowHours": hours, "persistedFailures": count,
              "recentPersisted": events, "systemdFailureMarkers": journals}
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"Ошибки резервирования ASA Lab за {hours} ч")
        print("Зарегистрированных сбоев нового журнала:", count)
        for entry in events:
            print(" ", stamp(entry["at"]), entry["kind"],
                  entry["trigger"], entry["code"])
        if journals:
            print("Системный журнал (возможные дубликаты тех же сбоев):")
            for entry in journals:
                print(" ", stamp(entry["at"]), entry["kind"], entry["code"])
        if not count and not journals:
            print("Ошибок по доступным журналам не найдено; это не гарантия полноты истории.")
    return 0


def manual(kind: str) -> int:
    sha, runner = current_runner()
    print("ASA School manual backup:", kind, "| GitHub SHA:", sha, flush=True)
    if kind == "full":
        print("ПОЛНАЯ КОПИЯ: Web/API/Scratch/MinIO могут кратковременно остановиться.",
              flush=True)
    else:
        print("ПОЧАСОВАЯ БД: без остановки Web/API.", flush=True)
    try:
        job = subprocess.run(["/usr/bin/python3", "-B", str(runner),
                              "--kind", kind, "--apply", "--manual"], check=False)
    except OSError:
        print("MANUAL_BACKUP_ERROR=PYTHON_NOT_AVAILABLE", file=sys.stderr)
        return 2
    return job.returncode


def restore(action: str | None, source: str | None, archive: str | None, identity: str | None) -> int:
    """Delegate to the existing age-safe Yandex restoration tool, never to live DB."""
    if action == "live":
        print("LIVE_RESTORE_BLOCKED: рабочие PostgreSQL и MinIO нельзя принудительно",
              "переписывать без предварительной новой копии, испытания восстановления",
              "и отдельно согласованного плана с потерей/переносом новых записей.",
              file=sys.stderr)
        return 2
    if bool(source) != bool(archive):
        print("Для заданного архива нужны одновременно --source и --archive", file=sys.stderr)
        return 2
    script = Path(__file__).resolve().parent / "school-yandex-restore.py"
    if not script.is_file() or script.is_symlink() or script.stat().st_uid != os.geteuid():
        print("VERSIONED_RESTORE_TOOL_MISSING", file=sys.stderr)
        return 2
    command = ["/usr/bin/python3", "-B", str(script), action or "list"]
    if source:
        command.extend(["--source", source, "--archive", archive])
    if identity:
        command.extend(["--identity", identity])
    try:
        return subprocess.run(command, check=False).returncode
    except OSError:
        print("RESTORE_HELPER_EXEC_FAILED", file=sys.stderr)
        return 2


def check() -> int:
    sha, runner = current_runner()
    print("GFS source SHA:", sha)
    for kind in ("hourly", "full"):
        code = subprocess.run(["/usr/bin/python3", "-B", str(runner),
                               "--kind", kind, "--preflight"], check=False).returncode
        if code:
            return code
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("hourly", "full", "status", "errors", "check", "restore"))
    parser.add_argument("restore_action", nargs="?", choices=("list", "download", "prepare", "restore-test", "live"))
    parser.add_argument("--source", choices=("school","legacy","gfs-daily","gfs-weekly","gfs-monthly","gfs-hourly"))
    parser.add_argument("--archive")
    parser.add_argument("--identity")
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if os.geteuid() != 0 or socket.gethostname() != HOST:
        print("Требуется sudo и школьный сервер asa-school-01", file=sys.stderr)
        return 2
    if args.json and args.action not in ("status", "errors"):
        parser.error("--json доступен только для status и errors")
    if args.action != "restore" and (args.restore_action or args.source or args.archive or args.identity):
        parser.error("параметры восстановления допустимы только с restore")
    if args.action == "restore":
        return restore(args.restore_action, args.source, args.archive, args.identity)
    try:
        if args.action in ("hourly", "full"):
            return manual(args.action)
        if args.action == "check":
            return check()
        if args.action == "status":
            return show_status(as_json=args.json)
        return show_errors(args.hours, as_json=args.json)
    except (OperatorError, OSError, ValueError, subprocess.SubprocessError) as error:
        code = str(error) if isinstance(error, OperatorError) else type(error).__name__
        print("GFS_OPERATOR_BLOCKED=" + code, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
