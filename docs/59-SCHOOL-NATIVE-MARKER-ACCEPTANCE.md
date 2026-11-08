# 59. Приёмка native marker → нормализованный журнал → HOST ZIP

## Назначение и граница

Этот рецепт выполняется **до `activate`**, на подготовленной школьной установке
`asa-school-01` / `asa-lab-school` / instance `fc0ba60fe1d946bba1694d73557cc11f`.
Допустимы `STAGED`, owner/originalOwner `legacy`, два выключенных native Cron
и простаивающие backup-процессы. Он не переводит работающую `ACTIVE` установку
назад и не запускает probes для неё. Для fresh-профиля с `originalOwner=none`
принятый здесь проверяющий инструмент пока не подходит: его отдельная приёмка
не объявляется выполненной и guard не обходится.

[`school-native-marker-acceptance.py`](../scripts/school-native-marker-acceptance.py)
и [`school-native-marker-receipt-binding.py`](../scripts/school-native-marker-receipt-binding.py)
опубликованы побайтно из ранее принятых проверяющих исходников. Их SHA-256:

| Скрипт | SHA-256 |
|---|---|
| `school-native-marker-acceptance.py` | `8057501ff3aecf8ec99255bd68c6762f5d600445b4223163d2e7a1a7f14cff80` |
| `school-native-marker-receipt-binding.py` | `281d3349e15bcc17e1c6ac8a608c0fdbd2b8f02e00ff7695559064e7e6695a62` |

Оба CLI принимают один JSON-аргумент с точными `runId`, `hostSourceRevision`,
`generation` и двумя фактическими probe bindings. Этот аргумент не содержит
пароля, API-ключа или содержимого логов. Marker при явном вызове создаёт только
свой приватный временный экспорт и receipt; это отдельная операция приёмки,
а не установщик с `--apply`. Binding читает единственный собственный root-600
receipt и не экспортирует журналы повторно.

Marker заново проверяет native `Success` record, одну JSON-строку собственного
dispatch в stdout, её полное равенство `runtime_state`, один normalized event в
HOST ZIP и `published=1` для этого ID в SQLite read-only. Полный HOST ZIP проходит
CRC/manifest/part SHA/count проверки канонического exporter. Старые строки других
dispatch, в том числе исторический двойной terminal marker, сохраняются и не
входят в счётчик текущего dispatch. Дубль текущего dispatch остаётся STOP.

`school-1panel-backup-schedules.py check` проверяет native metadata; обычный
snapshot CLI проверяет архив конфигурации. Эти команды сами по себе не доказывают
описанную связь конкретного dispatch с единственным нормализованным событием.

## Порядок подготовки

1. Подготовить или восстановить школьную ASA installation и её данные по
   канонической процедуре ASA; сохранить root/project/instance. Это не установка
   приложения средствами Settings. Host-tools source — `17d937c01660025e6b5030f44bcb0d776e7c79aa`,
   app runtime — `3e8713476cafedda3e24a39665458e833efd7c22`, schema 307.
2. Принять collector через [docs55](55-SCHOOL-1PANEL-LOGGING.md): Supervisor,
   один writer, manager `1panel-supervisor`, источник `linux:1panel-supervisor`.
3. Подготовить зависимости [docs56](56-SCHOOL-NATIVE-BACKUP-SCHEDULES.md):
   root-owned DB runner и transports в `/srv/Server-setings/scripts/`, защищённые
   backup-каталоги, `/etc/server-settings/mcp.env`, публичный age-recipient,
   конфигурацию Яндекс Диска. Значения секретов в Git и команды не переносятся.
4. Выполнить `preflight` и `stage` из docs56. Установленные pins для этого proof:
   wrapper `4b391bbd4b4589212acffc25a4239b79d15e5830176300610dfd1f7fc3ef596b`,
   helper `9a7719c5705fb6883857b0c25be1e19f9ee7f3009c8594a43ca15d04d0bffbe2`,
   settings release `fdd7ebb8daaebcfcebc8d46782aa362fd182cce2f6c43699144740f0b581f431`.
   Для временного canonical экспорта нужно не менее 4 GiB свободного места.
   Для перехода старого ошибочного STAGED поколения существует отдельная
   [опубликованная процедура docs58](58-SCHOOL-STAGED-SCHEDULE-REPAIR.md).
5. После двух gated probes выполнить приёмку ниже, затем независимо принять
   результаты и часовой пояс native preview. Активация — отдельный шаг docs56.
   Архив конфигурации, облачный roundtrip и изолированное восстановление описаны
   в [docs54](54-SCHOOL-MANAGED-CONFIGURATION-BACKUPS.md); условия будущего fresh
   snapshot — в [docs57](57-SCHOOL-FRESH-NATIVE-SNAPSHOT.md).

Шаги `stage --fresh` не создают школьные данные, secrets или проверенные runners.
Чистый Linux rehearsal остаётся **NOT_RUN**. Application Compose виден в панели
как внешний проект; этот маршрут относится к native collector и backup clocks.

## Полные команды до активации

Запускать одной root-сессией в 1Panel Terminal из корня отдельного чистого
checkout принятого Settings commit, содержащего эти два скрипта. Точный SHA
checkout сверяется с review. Существующая ACTIVE generation останавливает
рецепт **до probes**. `runId` выбирается один раз; после STOP нельзя удалять
receipt или выдавать новый ID ради повторного запуска.

```bash
set -euo pipefail
umask 077
test "$(id -u)" -eq 0
ASA_QA_SOURCE_ROOT="$(/usr/bin/git rev-parse --show-toplevel)"
read -r -p 'Принятый Settings commit, 40 lowercase hex: ' ASA_QA_SOURCE_REV
printf '%s\n' "$ASA_QA_SOURCE_REV" | LC_ALL=C grep -Eq '^[a-f0-9]{40}$'
test "$(/usr/bin/git -C "$ASA_QA_SOURCE_ROOT" rev-parse HEAD)" = "$ASA_QA_SOURCE_REV"
test -z "$(/usr/bin/git -C "$ASA_QA_SOURCE_ROOT" status --porcelain=v1 --untracked-files=all)"
ASA_MARKER_RUN="$(/usr/bin/python3 -I -B -c 'from datetime import datetime,timezone; import uuid; print("xlog-native-marker-"+datetime.now(timezone.utc).strftime("%Y%m%dt%H%M%Sz-")+uuid.uuid4().hex[:16])')"
ASA_MARKER_DIR="/var/lib/server-settings/native-marker-review/$ASA_MARKER_RUN"
/usr/bin/python3 -I -B - "$ASA_QA_SOURCE_ROOT" "$ASA_MARKER_DIR" <<'PY'
import hashlib, importlib.util, json, stat, sys
from pathlib import Path
source = Path(sys.argv[1]) / 'scripts/school-native-marker-acceptance.py'
assert hashlib.sha256(source.read_bytes()).hexdigest() == '8057501ff3aecf8ec99255bd68c6762f5d600445b4223163d2e7a1a7f14cff80'
spec = importlib.util.spec_from_file_location('accepted_marker_inputs', source)
marker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(marker)
marker.trusted(source)
state = json.loads(marker.read(marker.STATE / 'state.json'))
assert state['phase'] == 'STAGED' and state['owner'] == state['originalOwner'] == 'legacy'
assert state['programSha256'] == marker.CRON_SHA and state['clockTimeZone'] == 'Europe/Moscow'
directory = Path(sys.argv[2])
assert directory.parent == Path('/var/lib/server-settings/native-marker-review')
assert marker.RUN.fullmatch(directory.name)
marker.trusted(directory.parent.parent, directory=True)
if not directory.parent.exists() and not directory.parent.is_symlink():
    directory.parent.mkdir(mode=0o700)
marker.trusted(directory.parent, directory=True)
assert stat.S_IMODE(directory.parent.stat().st_mode) == 0o700
directory.mkdir(mode=0o700)
PY
/usr/bin/python3 -I -B "$ASA_QA_SOURCE_ROOT/scripts/school-1panel-backup-schedules.py" probe --kind database --apply > "$ASA_MARKER_DIR/probe-database.json"
/usr/bin/python3 -I -B "$ASA_QA_SOURCE_ROOT/scripts/school-1panel-backup-schedules.py" probe --kind settings --apply > "$ASA_MARKER_DIR/probe-settings.json"
/usr/bin/python3 -I -B "$ASA_QA_SOURCE_ROOT/scripts/school-1panel-backup-schedules.py" check > "$ASA_MARKER_DIR/native-check.json"
```

Сохранённые probe JSON — прямой результат опубликованного CLI. Не подставляйте
придуманные record/dispatch IDs, чужое поколение или отредактированный вывод
посредника. Уже принятый probe того же поколения canonical CLI возвращает из
состояния, без нового `/cronjobs/handle`.

До следующего блока дождаться одного успешного collector cycle **после обеих
проб**. Проверка ниже читает один health-срез и останавливается, если такого
цикла ещё нет; скрытого polling или повторного probe нет. При STOP сначала
разбирают сохранённый результат; протокол не догоняет зелёный статус повторами.

```bash
/usr/bin/python3 -I -B - "$ASA_QA_SOURCE_ROOT" "$ASA_MARKER_DIR" <<'PY'
import hashlib, importlib.util, json, os, sys
from pathlib import Path
source = Path(sys.argv[1]) / 'scripts/school-native-marker-acceptance.py'
assert hashlib.sha256(source.read_bytes()).hexdigest() == '8057501ff3aecf8ec99255bd68c6762f5d600445b4223163d2e7a1a7f14cff80'
spec = importlib.util.spec_from_file_location('accepted_marker_inputs', source)
marker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(marker)
marker.trusted(source)
directory = Path(sys.argv[2])
marker.trusted(directory, directory=True)
state = json.loads(marker.read(marker.STATE / 'state.json'))
native = json.loads(marker.read(directory / 'native-check.json', 131072))
assert state['phase'] == native['phase'] == 'STAGED'
assert state['owner'] == state['originalOwner'] == native['owner'] == 'legacy'
assert state['programSha256'] == marker.CRON_SHA
assert native['status'] == 'NATIVE_SCHEDULE_METADATA'
binding = {'runId': directory.name, 'hostSourceRevision': '17d937c01660025e6b5030f44bcb0d776e7c79aa',
           'generation': state['generation'], 'jobs': {}}
for kind in marker.KINDS:
    probe = json.loads(marker.read(directory / ('probe-' + kind + '.json'), 8192))
    job, observed = state['jobs'][kind], native['jobs'][kind]
    assert probe == job['probe'] and probe['status'] == 'NATIVE_GATED_PROBE_PASS'
    assert probe['programSha256'] == marker.CRON_SHA and probe['backupExecuted'] is False
    assert probe['nativeProcessLogObserved'] is True
    assert observed['id'] == job['id'] and observed['cronStatus'] == 'Disable'
    assert observed['supervisorStatus'] in ('STOPPED', 'EXITED')
    run = observed['run']
    assert run['dispatchId'] == probe['dispatchId'] and run['generation'] == state['generation']
    assert run['programSha256'] == marker.CRON_SHA and run['backupExecuted'] is False
    assert run['status'] == 'STAGED_GATE_VERIFIED_NO_BACKUP'
    records = [row for row in observed['records'] if row['id'] == probe['recordId']]
    assert len(records) == 1 and records[0]['cronjobID'] == job['id'] and records[0]['status'] == 'Success'
    binding['jobs'][kind] = {'id': job['id'], **{key: probe[key] for key in ('recordId', 'dispatchId', 'acceptedAt')}}
marker.validate_binding(binding)
health = json.loads(marker.read(marker.ROOT / '.asa/diagnostics/store/collector-health.json', 8192))
assert health['state'] == 'ok'
assert marker.instant(health['lastSuccessAt']) > max(marker.instant(row['acceptedAt']) for row in binding['jobs'].values()), 'COLLECTOR_CYCLE_AFTER_PROBES_NOT_OBSERVED'
raw = (json.dumps(binding, sort_keys=True, separators=(',', ':')) + '\n').encode()
assert len(raw) <= 4096
fd = os.open(directory / 'binding.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
with os.fdopen(fd, 'wb') as output:
    output.write(raw)
    output.flush()
    os.fsync(output.fileno())
PY
ASA_MARKER_BINDING="$(cat "$ASA_MARKER_DIR/binding.json")"
/usr/bin/python3 -I -B "$ASA_QA_SOURCE_ROOT/scripts/school-native-marker-acceptance.py" "$ASA_MARKER_BINDING" > "$ASA_MARKER_DIR/marker-result.json"
/usr/bin/python3 -I -B "$ASA_QA_SOURCE_ROOT/scripts/school-native-marker-receipt-binding.py" "$ASA_MARKER_BINDING" > "$ASA_MARKER_DIR/receipt-binding.json"
/usr/bin/python3 -I -B - "$ASA_MARKER_DIR" <<'PY'
import json, sys
from pathlib import Path
directory = Path(sys.argv[1])
result = json.loads((directory / 'marker-result.json').read_bytes())
binding = json.loads((directory / 'receipt-binding.json').read_bytes())
assert result['status'] == 'NATIVE_MARKERS_NORMALIZED_HOST_EXPORT_PASS'
assert binding['status'] == 'NATIVE_MARKER_RECEIPT_BINDING_PASS' and all(binding['checks'].values())
print(json.dumps({'status': 'NATIVE_MARKER_PROOF_READY_FOR_REVIEW',
                  'receiptPath': result['receiptPath'], 'receiptSha256': binding['receiptSha256'],
                  'receiptBytes': binding['receiptBytes'], 'activationPerformed': False}, sort_keys=True))
PY
```

Скрипт marker повторно проверяет живые native объекты и входные файлы вокруг
экспорта. При PASS два ZIP проверены; временные файлы удалены, собственный
`/var/lib/server-settings/xlog-native-marker-evidence/<runId>/receipt.json`
сохраняется root 0600. Финальное значение `receiptSha256` — хеш байтов именно
этого серверного файла. Хеш скопированного или редактированного MCP-вывода не
заменяет его. Binder требует все 11 закрытых proof checks.

После независимой приёмки этих результатов и timezone preview продолжить
`activate` из docs56. До неё native Cron остаётся Disable, прежние таймеры —
владельцами расписания. Повторять эту процедуру на уже ACTIVE установке нельзя.

## Что результат не доказывает

Это preactivation proof двух собственных событий. Он не создаёт резервную копию
PostgreSQL/MinIO, не доказывает первую автоматическую ночную копию или новое
восстановление. Application ZIP проверяется неизменённым helper, но marker
ищется только в HOST ZIP. Authenticated coverage не перепроверяется,
`globalCoverageComplete=false` и исторические gaps сохраняются. Подтверждение
текущего healthy collector не превращает отсутствовавшую историю в полную.

Локальные тесты из корня репозитория:

```bash
python3 -B -m unittest discover -s tests -p 'test_school_native_marker*.py' -v
```

Проверяются native/normalized uniqueness своего dispatch при сохранённом старом
дубле, JSON/time/origin/classification binding, read-only published ID, отказ при
неверном receipt/generation/режиме файла и реальный хеш до возможной редактуры
tool output. Ownership synthetic fixture моделируется для обычного CI; реальные
файлы, chmod, hardlink и symlink используются без изменения production guards.
Эти тесты не заменяют чистый Linux rehearsal или фактический запуск нового рецепта.
