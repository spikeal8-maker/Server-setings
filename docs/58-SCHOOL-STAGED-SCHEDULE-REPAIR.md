# Школьные задания: замена ошибочного STAGED worker с сохранением истории

Дата: 2026-11-08. Исходный статус до применения: **код и локальные проверки подготовлены; выполнение repair на VM — NOT_RUN**. Этот исторический статус сохранён; ниже добавлен отдельный датированный фактический результат.

## Принятый repair: 08.11.2026, 16:09:44 UTC

Применён опубликованный Settings `57ca0440d6360ab9127d9987c9d5e315647174a7`
с wrapper `4b391bbd4b4589212acffc25a4239b79d15e5830176300610dfd1f7fc3ef596b`.
Операция repair `c56e3b76ddfc4115ada0ed3b01f70a86` завершилась
`2026-11-08T16:08:38.024272+00:00`, exit 0. Независимое read-only reconcile
`472c9d923f28451aa3a7a7cbc64b7153` завершилось
`2026-11-08T16:09:44.263046+00:00`, exit 0; оба результата приняты контролёром.

Прежнее поколение `dc880db9be0e49b0953973ba39f19cde` сохранено в истории;
новое — `4ccfa3f7ffe34b358523a02872e03f53`. Native ID 1/2 не изменились,
задачи оставались `Disable`, owner — `legacy`, старые таймеры enabled/active.
Точные старые state/dispatch/runs, native records и журналы сохранены;
новые текущие dispatch/runs были пусты. Collector PID 2770698 и его private
identity, восемь application containers и private index inode 816721 сохранены.
Реальные backup этой операцией не запускались. Этот результат допускает
следующие две gated пробы, но сам по себе не доказывает marker/ZIP, activation
или восстановление. Последующие датированные факты относятся к
[docs56](56-SCHOOL-NATIVE-BACKUP-SCHEDULES.md); воспроизводимая приёмка маркеров
публикуется отдельно в [docs59](59-SCHOOL-NATIVE-MARKER-ACCEPTANCE.md).

## Причина и граница исправления

Школьный worker из Server-setings `c23b8d970b7160b2146301c2af514a21071216dc` дважды печатал один terminal JSON: сначала из `worker()`, затем из `main()`. Read-only операция `2b1a67931c7c4fb480c8a3d9d714c465` от 2026-11-08 15:11 UTC подтвердила две одинаковые строки, всего 1006 байт, и одну успешную native Cron запись. Сам staged worker не выполнял резервное копирование. Проверяющий оператор остановился на требовании единственного маркера; это исходное событие сохраняется.

Зафиксированное поколение: `dc880db9be0e49b0953973ba39f19cde`. Собственные native Cron ID: database — 1, settings — 2; оба `Disable`. Database process завершён (`EXITED`), settings process не запускался (`STOPPED`). Владелец расписания — `legacy`. Рабочий сборщик журналов и приложение относятся к самостоятельным принятым операциям и этим helper не обновляются.

Узкое исправление wrapper исключает повторную печать из `main()` для действия `worker`. Привязки файлов:

| Файл wrapper | SHA-256 |
|---|---|
| Предыдущий, установленный | `6f5a51cbe94a7bf22152884eeb7d7c0efd149667cdc21b60e889d682740e18b2` |
| Исправленный | `4b391bbd4b4589212acffc25a4239b79d15e5830176300610dfd1f7fc3ef596b` |

`scripts/school-1panel-staged-repair.py` поддерживает именно эту пару исходников и два принадлежащих установке задания. Изменения `worker`, `trigger`, формата их текущих каталогов и алгоритма резервного копирования не требуются. Чистая установка Linux этой операцией не проверяется.

## Почему создаётся новое поколение

Старый `trigger` намеренно не повторяет завершённый запуск того же дня и той же фазы. Простая замена файла или удаление признака успешного probe не являются правильным повторным тестом.

Helper сохраняет точные байты старого `state.json`, включая `probe` и `probeIntent`, в истории исходного поколения. Старые каталоги `dispatch` и `runs` перемещаются туда через rename с сохранением файлов, их содержимого и inode. На стандартных текущих путях создаются новые пустые каталоги root 0700. Текущий `state.json` получает новое явно выбранное поколение и исправленный wrapper; native ID 1/2 сохраняются. Принимаемые probes нового поколения ещё отсутствуют. Валидный `nativeNextPreview` сохраняется, поскольку расписание не меняется.

Исходный встроенный статус старого probe остаётся историческим фактом. Он не переносится в новое поколение и не превращает остановку внешней проверки в успешный результат.

## Предусловия и порядок операции

Проверяется именно школьная установка, точные SHA обоих wrapper, совпадение неизменившихся зависимостей резервного копирования, старое поколение, ожидаемый SHA текущего состояния и фаза `STAGED/legacy`. Любой настоящий или незавершённый backup, неизвестный dispatch, активный native процесс, включённое native задание, чужой объект или изменение старого состояния запрещает repair.

Первым берётся существующий ownership lock в exclusive режиме. Все канонические `probe`, `trigger` и `worker` берут его shared до чтения состояния. Дополнительно захватываются существующие DB/config-backup/ASA operation locks и существующие per-kind/probe locks. Отсутствующие settings locks не создаются: они появятся штатно при первом probe нового поколения. Каталог `/run/lock` и права существующих lockfiles не меняются.

Последовательность после read-only плана и приёмки Git:

1. Сохранить immutable исходное состояние, DTO/config references и intent в приватной истории. Зафиксировать каталоги через fsync.
2. Записать и перечитать `STAGED_REPAIRING/legacy`. Этот durable gate закрывается **до** переноса текущих квитанций и native изменений.
3. Разместить исправленный wrapper в его отдельном immutable release. Старый wrapper и прежний settings release остаются. Перенести старые `dispatch/runs`, синхронизировать оба родительских каталога и создать пустые текущие каталоги.
4. Через native 1Panel API обновить INI только простаивающего собственного one-shot процесса, затем команду существующего выключенного Cron ID. После каждого запроса проверить точный результат. Программа не использует start, handle, enable, delete или очистку логов.
5. Подтвердить сохранность legacy таймеров, сборщика, восьми контейнеров приложения, identity индекса, старых квитанций, native Cron records и собственных backup logs. Только после этого записать `STAGED/legacy` нового поколения.

Legacy таймеры остаются enabled. Ни резервная копия, ни проверочный worker этой операцией не запускаются.

## Native API и сохранение журналов

У обычного wrapper остаётся прежний allowlist. Отдельный adapter в helper использует только дополнительные официальные POST `/api/v2/cronjobs/load/info` и `/api/v2/cronjobs/update` версии 1Panel v2.3.2. Он сохраняет полный `CronjobOperate` DTO, меняя только `script`, повторно проверяет собственный ID и `Disable`, требует пустую настройку alerts. Endpoint фиксирован на loopback; proxy и redirects отключены. Ключ остаётся в памяти и не выводится.

Официальный `agent/app/service/cronjob.go`, `Update`, сохраняет ID и записи истории. Для `Disable` он не вызывает `StartJob`. Ошибка обновления alerts возможна **после** записи задания; поэтому ошибка ответа сама по себе не доказывает отсутствия изменения. Helper делает один запрос и одну проверку эффекта. Если точный эффект не подтверждён, результат — STOP с закрытым gate. Автоматического повторного запроса нет.

INI изменяется существующим native `/hosts/tool/supervisor/process/file` с `operate=update`, `file=config`. В официальном `host_tool.go` версии v2.3.2 это вызывает `supervisorctl update` для конкретной группы. Проверенные исходники установленного Supervisor 4.2.5 показывают, что обычное удаление/добавление остановленной группы при update не вызывает очистку явных logfile paths; обработчики таких файлов открываются для добавления. Отдельные команды очистки и ротации helper не вызывает. Сохранность существующих out/err и ротаций дополнительно проверяется до и после фактической операции; source proof не заменяет этот runtime readback.

## Полные команды

Запускать от уже авторизованного root из отдельного, чистого, принятого Git checkout. Его точный commit и CI фиксируются внешним проверяющим перед выполнением. Существующие рабочие checkout, настройки и сервисы не используются как место для ручного исправления исходника.

Подготовить один новый generation и получить read-only план:

```bash
set -euo pipefail
ASA_SETTINGS_ROOT="$(/usr/bin/git rev-parse --show-toplevel)"
test -z "$(/usr/bin/git status --porcelain=v1 --untracked-files=all)"
ASA_REPAIR_GENERATION="$(/usr/bin/python3 -I -c 'import uuid; print(uuid.uuid4().hex)')"
ASA_REPAIR_PLAN="$(/usr/bin/python3 -I -B "$ASA_SETTINGS_ROOT/scripts/school-1panel-staged-repair.py" plan \
  --old-generation dc880db9be0e49b0953973ba39f19cde \
  --old-program-sha256 6f5a51cbe94a7bf22152884eeb7d7c0efd149667cdc21b60e889d682740e18b2 \
  --new-generation "$ASA_REPAIR_GENERATION")"
printf '%s\n' "$ASA_REPAIR_PLAN"
ASA_REPAIR_STATE_SHA="$(printf '%s' "$ASA_REPAIR_PLAN" | /usr/bin/python3 -I -c 'import json,sys; value=json.load(sys.stdin); assert value["status"]=="STAGED_REPAIR_PLAN_ONLY"; print(value["oldStateSha256"])')"
```

Внешний оператор сохраняет точный план, generation, SHA состояния и принятый commit в приватной квитанции операции до применения. Generation выбирается один раз. После проверки именно этого плана использовать те же generation и SHA состояния:

```bash
/usr/bin/python3 -I -B "$ASA_SETTINGS_ROOT/scripts/school-1panel-staged-repair.py" repair --apply \
  --old-generation dc880db9be0e49b0953973ba39f19cde \
  --old-program-sha256 6f5a51cbe94a7bf22152884eeb7d7c0efd149667cdc21b60e889d682740e18b2 \
  --new-generation "$ASA_REPAIR_GENERATION" \
  --old-state-sha256 "$ASA_REPAIR_STATE_SHA"
```

При STOP, потере ответа или повторном подключении сначала только read-only классификация с тем же generation. Следующий блок заново определяет принятый checkout и читает generation из проверенного приватного intent. При отсутствующем или некорректном intent он останавливается: нужно сверить исходный сохранённый план, а не создавать новый ID.

```bash
set -euo pipefail
ASA_SETTINGS_ROOT="$(/usr/bin/git rev-parse --show-toplevel)"
test -z "$(/usr/bin/git status --porcelain=v1 --untracked-files=all)"
ASA_REPAIR_GENERATION="$(/usr/bin/python3 -I -B - <<'PY'
import json, os, re, stat
from pathlib import Path
path = Path('/var/lib/server-settings/1panel-backup-schedules/history/dc880db9be0e49b0953973ba39f19cde/intent.json')
for item in (path, *path.parents):
    info = item.lstat()
    assert info.st_uid == 0 and not stat.S_ISLNK(info.st_mode) and not info.st_mode & 0o022
info = path.lstat()
assert stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == 0o600
assert info.st_size <= 128 * 1024
fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
with os.fdopen(fd, 'rb') as source:
    raw = source.read(128 * 1024 + 1)
    after = os.fstat(source.fileno())
assert (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) == (after.st_dev, after.st_ino, len(raw), after.st_mtime_ns)
value = json.loads(raw)
assert value['format'] == 'asa-school-staged-repair-v1'
assert value['oldGeneration'] == 'dc880db9be0e49b0953973ba39f19cde'
assert value['oldProgramSha256'] == '6f5a51cbe94a7bf22152884eeb7d7c0efd149667cdc21b60e889d682740e18b2'
assert value['newProgramSha256'] == '4b391bbd4b4589212acffc25a4239b79d15e5830176300610dfd1f7fc3ef596b'
generation = value['newGeneration']
assert re.fullmatch(r'[a-f0-9]{32}', generation) and generation != value['oldGeneration']
assert value['newState']['generation'] == generation
print(generation)
PY
)"
/usr/bin/python3 -I -B "$ASA_SETTINGS_ROOT/scripts/school-1panel-staged-repair.py" reconcile \
  --old-generation dc880db9be0e49b0953973ba39f19cde \
  --old-program-sha256 6f5a51cbe94a7bf22152884eeb7d7c0efd149667cdc21b60e889d682740e18b2 \
  --new-generation "$ASA_REPAIR_GENERATION"
```

`reconcile` не продолжает, не откатывает и не повторяет native запросы. Он сообщает old/new/closed-transition/unknown для состояния и объектов, наличие архивных и текущих каталогов. Незавершённый intent также остаётся фактом для ручной проверки. Новая mutation после такого результата требует отдельного конкретного плана по наблюдаемому состоянию; удалять историю для повторного запуска запрещено.

## Проверки и дальнейшая приёмка

`tests/test_school_staged_repair.py`: 13 локальных тестов используют настоящие файлы, JSON, rename, fsync и flock, с моделированием привилегий/host/native API. Проверены сохранение старых bytes/inode и DTO полей, перенос preview, read-only plan/reconcile, JSON roundtrip, busy lock, stale state hash, ACTIVE/незавершённый run, потеря ответа после commit, неподтверждённый запрос без commit, отсутствие автоматического повтора и отсутствие подавления нового dispatch старой записью того же дня. Внешние команды, VM/API, backup и cloud не запускались.

После успешного repair всё ещё нужны отдельные два gated probe нового поколения, проверка **одной** terminal строки на собственный dispatch, доставка обеих записей в `linux:1panel-supervisor` и нормализованный журнал/экспорт. Активация расписания и новая резервная копия с проверенным восстановлением выполняются отдельными принятыми шагами. Repair сам по себе их не доказывает.

## Граница резервного копирования истории

Текущий configuration snapshot helper распознаёт оба immutable wrapper release и актуальное состояние расписания. После размещения исправленного wrapper доставку обоих release в новый архив нужно подтвердить отдельной проверкой. Приватная operational history этой repair-операции — каталог `history/dc880db9be0e49b0953973ba39f19cde`, intent/progress и старые run/dispatch — намеренно не входит в существующий configuration archive allowlist. Она остаётся на VM, вместе с сохранёнными native records и исходными логами. Это известное исключение покрытия: проверка восстановления актуальной ACTIVE конфигурации не является проверкой восстановления этой исторической папки. Snapshot helper этим изменением не расширяется.
