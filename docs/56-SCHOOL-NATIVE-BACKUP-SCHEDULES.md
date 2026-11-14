# 56. Школьные копии: плановые задачи и процессы в 1Panel

## Принятое состояние — ACTIVE / native 1Panel, 08.11.2026 19:41:02 МСК

ID 1/2 сохранены, обе задачи Enable, owner 1panel. Старые backup timers
отключены и неактивны; DB выполняется по расписанию 03:20, конфигурация — 04:00
Europe/Moscow. Следующие запуски 09.11; display Etc/UTC и пять preview отдельно
приняты root. Settings57ca/wrapper4b/generation4ccf установлены и проверены.

Исходный fa96 STOP остаётся историей. После закрытого repair c56e/472c новые
DB/settings-пробы дали record2/record3 с уникальными terminal markers. Их доставка
native → normalized/published → HOST ZIP, CRC/part SHA, application ZIP и cleanup
приняты. Activation f967 и отдельный ACTIVE status d76b прошли; collector2770698,
8 app identities и index816721 сохранены. Пробы и activation реального backup
не выполняли; последующая ручная ACTIVE-копия проверялась отдельной цепочкой.

Новый архив d0ba6e1f — 27 613 976 байт, ciphertext SHA256 `ccb146c2…` — прошёл
CREATE fcdb, полный Яндекс upload/download 2ffc и authenticated decrypt/isolated
verify через D2 PID41112. Приняты 70 payload files+manifest, permissions manifest,
3 SQLite integrity OK, HOST ZIP544/223525 и application ZIP20/25033 с CRC/SHA.
Восстановленные ACTIVE state, оба native INI, wrapper, collector bridge и оба
ожидаемых probe markers проверены. Live paths не записывались.
[Полный recovery receipt и хеш](../snapshots/2026-11-08/school-native-logging-final-checkpoint-20261108T164102Z.json).

### Stage не является bootstrap чистой Linux-системы

Этот маршрут рассчитан на уже подготовленный школьный host `asa-school-01`,
project `asa-lab-school` и согласованный instance. До stage нужны работающие ASA,
1Panel/Supervisor и доступ к локальному API, установленные DB/settings runners,
репозиторий `/srv/Server-setings` с их транспортами, Python/age, настроенный Яндекс
app-folder, защищённые каталоги копий и внешняя private age identity для recovery.
Stage не создаёт эти зависимости и не является доказательством полной установки
на произвольный Linux. Режим fresh stage не заменяет подготовку host/instance.

Связанные опубликованные инструкции: [конфигурационные копии и транспорт](54-SCHOOL-MANAGED-CONFIGURATION-BACKUPS.md),
[collector и его native prerequisites](55-SCHOOL-1PANEL-LOGGING.md),
[fresh native snapshot](57-SCHOOL-FRESH-NATIVE-SNAPSHOT.md),
[закрытый STAGED repair](58-SCHOOL-STAGED-SCHEDULE-REPAIR.md).
Проверка native marker→normalized→HOST ZIP должна приниматься **до activate**.
В этом пакете включена [полная процедура docs59](59-SCHOOL-NATIVE-MARKER-ACCEPTANCE.md)
и два точных CLI, которыми получен принятый marker/ZIP proof. Они не зависят от
нашей scratch-среды и сохраняют STAGED/legacy scope; повторный запуск на ACTIVE
и сертификация fresh/originalOwner=none ими не разрешаются.

Первая автоматическая native копия NOT_PROVEN, реальная чистая Linux-установка
NOT_RUN. Новая logging metadata26/27 показывает только исторический Docker gap;
полный authenticated coverage не повторялся, global coverage=false. После
аварийного восстановления требуется новая ChatGPT/MCP/OAuth регистрация;
сохранность прежних OAuth-сессий не доказана. Общий H04/domain cutover не выполнен.

Ниже сохранена опубликованная инструкция PR86 вместе с alias PR85 и worker-output
diagnosis; её датированные исходные ограничения не переписывают текущую приёмку.

## Сохранённая опубликованная инструкция и исходные границы


## Статус этой редакции

PANEL-CRON-01 — подготовленный код и локальные проверки. Эта инструкция сама по
себе **не подтверждает перенос работающего сервера**. После применения отдельно
нужны фактические ID задач, результаты двух закрытых проб, состояния старых
таймеров и первая автоматическая копия. Действующие копии и прежние доказательства
восстановления остаются в силе как датированные результаты.

Фактический срез 08.11.2026 15:11 UTC: immutable Settings `c23b8d9` установлен,
native stage принят, задачи ID 1/2 выключены, владелец расписания — `legacy`.
Первая DB-проба дала одну native запись `Success`, но независимая проверка
остановилась: worker и общий CLI дважды напечатали один итоговый JSON. Read-only
диагностика подтвердила две одинаковые строки в API и локальном журнале, без
повторного запуска и без резервирования. Settings-проба и активация не выполнялись.
Исправление этой редакции относится к исходному коду; его применение на существующей
STAGED установке требует отдельного проверенного перехода. [Датированный снимок](../snapshots/2026-11-08/school-native-worker-output-diagnosis-20261108.json).

Срез рассчитан на школьную `asa-school-01`, проект `asa-lab-school`, 1Panel
`v2.3.2` и уже проверенную установку ASA. Он не меняет домен, H04, Proxmox, сеть,
данные PostgreSQL/MinIO или архивы на Яндекс Диске.

## Что управляется панелью

| Раздел 1Panel | Объект | Назначение |
|---|---|---|
| Плановые задачи | `ASA-School-database-backup` | Запуск в 03:20 Europe/Moscow; история запросов и наблюдения |
| Плановые задачи | `ASA-School-settings-backup` | Запуск в 04:00 Europe/Moscow; история запросов и наблюдения |
| Инструменты → Supervisor | `asa-school-nightly-database-backup` | Отдельный процесс согласованного PostgreSQL/MinIO-экспортера |
| Инструменты → Supervisor | `asa-school-nightly-settings-backup` | Отдельный процесс копирования конфигурации и нормализованных журналов |

У процессов `autostart=false`, `autorestart=false`, `startsecs=0`,
`startretries=0`. Состояние `EXITED` после успешной одноразовой копии нормально.
Задачи Cron вызывают только свой процесс через официальный API 1Panel. Они не
вызывают `systemctl start`, не создают новый Linux-таймер и не останавливают
процесс резервирования при завершении наблюдения.

Это разделение необходимо для сохранения поведения DB-экспортера. В 1Panel
`v2.3.2` поле Cron `timeout` обязательно и должно быть не меньше одной секунды;
при отмене команды реализация посылает `SIGKILL` её группе процессов. Прямое
выполнение экспортера в Cron могло бы оборвать операцию на середине копирования.
Supervisor запускает резервирование отдельно от наблюдателя. Экспорт выполняет
менеджер установленного ASA Lab. Новый менеджер отмечает копию, созданную во
время работы сайта, полем `captureMode: online-postgres-minio` в manifest.
При старом менеджере возможна краткая пауза.
Ограничение Cron относится к наблюдателю; DB-экспортер сохраняет отсутствие
автоматического таймаута. Для копии конфигурации сохранены внутренние 1800 секунд
runner и внешний предел 1900 секунд с 45 секундами на завершение.

Источники протокола закреплены на официальном commit
[`65243c68c463cc055ab044093f641ea5d2e9e28b`](https://github.com/1Panel-dev/1Panel/tree/65243c68c463cc055ab044093f641ea5d2e9e28b):
[`CronjobOperate`](https://github.com/1Panel-dev/1Panel/blob/65243c68c463cc055ab044093f641ea5d2e9e28b/agent/app/dto/cronjob.go),
[`Create/UpdateStatus`](https://github.com/1Panel-dev/1Panel/blob/65243c68c463cc055ab044093f641ea5d2e9e28b/agent/app/service/cronjob.go),
[`handleShell`](https://github.com/1Panel-dev/1Panel/blob/65243c68c463cc055ab044093f641ea5d2e9e28b/agent/app/service/cronjob_helper.go),
[`CommandHelper`](https://github.com/1Panel-dev/1Panel/blob/65243c68c463cc055ab044093f641ea5d2e9e28b/agent/utils/cmd/cmdx.go),
[`Supervisor`](https://github.com/1Panel-dev/1Panel/blob/65243c68c463cc055ab044093f641ea5d2e9e28b/agent/app/service/host_tool.go).

## Замена поколения уже активного расписания

Для подготовленной замены пакета настроек используйте
`scripts/school-1panel-active-settings-transition.py` из принятого Git checkout.
Переход требует действующий статус `ACTIVE/1panel`, точные старый и новый
SHA-256 wrapper и UUID поколений, совпадение ID 1/2, свободные блокировки,
отключённые legacy-таймеры и полное совпадение действующего Cron DTO и
конфигураций Supervisor. Файлы базы, MinIO и планировщик Docker не участвуют.

`plan` ничего не меняет. Он выводит `oldStateSha256` для передачи в `switch`.
`switch --apply` устанавливает неизменяемый пакет, выполняет read-only
проверку его runner, сохраняет исходное состояние, закрывает запуск worker,
меняет путь к коду и поколение у тех же Cron ID и групп Supervisor. Расписания
03:20 и 04:00 по Москве остаются прежними. В конце сверяются старые записи Cron,
журналы Supervisor, контейнеры приложения и сборщик. Копирование в ходе
переключения не запускается.

`reconcile` классифицирует состояние двух задач и групп. `rollback --apply`
возвращает точное исходное поколение после ошибки или для проверки перехода.
Он требует исходный хеш и разрешён только до первой новой записи Cron или
запуска нового worker. После неоднозначного результата запускается
`reconcile`. Повторный `switch` не возобновляет незавершённую операцию.
Старые конфигурации, квитанции, исходный вывод и история Cron не удаляются.

```bash
old_sha='<SHA256 действующего wrapper>'
new_sha="$(sha256sum scripts/school-1panel-backup-schedules.py | cut -d' ' -f1)"
old_generation='<generation из рабочего состояния>'
new_generation="$(python3 -I -c 'import uuid; print(uuid.uuid4().hex)')"
args=(--old-program-sha256 "$old_sha" --new-program-sha256 "$new_sha"
      --old-generation "$old_generation" --new-generation "$new_generation")
sudo python3 -B scripts/school-1panel-active-settings-transition.py plan "${args[@]}"
# Вставить oldStateSha256 из принятого плана.
state_sha='<oldStateSha256>'
sudo python3 -B scripts/school-1panel-active-settings-transition.py switch "${args[@]}" --old-state-sha256 "$state_sha" --apply
sudo python3 -B scripts/school-1panel-active-settings-transition.py reconcile "${args[@]}" --old-state-sha256 "$state_sha"
# Только если нужен возврат до первого нового запуска:
sudo python3 -B scripts/school-1panel-active-settings-transition.py rollback "${args[@]}" --old-state-sha256 "$state_sha" --apply
```

На H03 фактический Supervisor 4.2.5 проверил обновление и возврат групп с
сохранением байтов и inode журналов и PID отдельного сборщика. Тесты транзакции
используют имитацию API Cron и настоящие Linux-файлы. Полная проверка на живой
1Panel с Cron и Supervisor ещё требуется. Состояние школьных задач на этом
этапе не изменялось.
[Доказательства H03](../snapshots/2026-11-14/h03-active-settings-transition.json).

## Порядок воспроизведения

Этот порядок начинается с подготовленной или восстановленной школьной ASA
installation. Collector принимается по [docs55](55-SCHOOL-1PANEL-LOGGING.md),
затем native backup clocks проходят stage и обе пробы. До activate выполняется
[полный опубликованный marker → normalized → HOST ZIP proof из docs59](59-SCHOOL-NATIVE-MARKER-ACCEPTANCE.md).
Дальнейшая конфигурационная копия/restore — [docs54](54-SCHOOL-MANAGED-CONFIGURATION-BACKUPS.md),
условия fresh snapshot — [docs57](57-SCHOOL-FRESH-NATIVE-SNAPSHOT.md).
`stage --fresh` не подготавливает данные, secrets, директории и runners;
чистый Linux rehearsal остаётся NOT_RUN. Проверяющие CLI docs59 сохраняют
принятый STAGED/legacy scope; fresh/originalOwner=none ими не сертифицируется.

Сначала применяется и принимается отдельный срез управления сборщиком через
1Panel Supervisor. Служба Supervisor, её конфигурация и доступ к локальному API
должны быть готовы; этот установщик не инициализирует общий Supervisor и не
перезапускает чужие программы. Допустим только известный школьный сборщик и два
собственных backup-процесса. Ключ панели читается на сервере из защищённого
`/etc/server-settings/mcp.env` и не попадает в аргументы, Git, таблицу или вывод.

Для проверки версии допускается штатная ссылка `/usr/bin/1panel` только на
`/usr/local/bin/1panel-core`: ссылка должна принадлежать root, её родители — быть
обычными каталогами root без групповой/общей записи. Проверяются точный
абсолютный адрес цели и неизменность ссылки. Конечный файл проходит прежнюю
строгую `trusted()` и должен быть исполняемым; команда `version` запускает
именно `/usr/local/bin/1panel-core`. Другие ссылки не допускаются. Права,
сама системная ссылка и файлы панели этим кодом не изменяются. Для обычного
доверенного файла `1panel` сохраняется прежний путь запуска. Причина исправления
и границы серверной проверки: [снимок диагностики](../snapshots/2026-11-08/school-native-cron-alias-diagnosis-20261108.json).

Используется отдельная проверенная root-owned копия репозитория с точным SHA и
всеми шестью файлами settings-release. Нельзя заменять существующую грязную копию
через слепой `git pull/reset`. Команды ниже выполняются из корня проверенной копии.

```bash
python3 scripts/school-1panel-backup-schedules.py plan
sudo python3 -B scripts/school-1panel-backup-schedules.py preflight
sudo python3 -B scripts/school-1panel-backup-schedules.py stage --apply
sudo python3 -B scripts/school-1panel-backup-schedules.py probe --kind database --apply
sudo python3 -B scripts/school-1panel-backup-schedules.py probe --kind settings --apply
sudo python3 -B scripts/school-1panel-backup-schedules.py check
```

Здесь остановиться до [приёмки docs59](59-SCHOOL-NATIVE-MARKER-ACCEPTANCE.md).
Если canonical probes уже сохранены, их повторный вызов читает существующий
успех; новый dispatch для обхода STOP не создаётся. После принятого marker/ZIP
proof и независимого подтверждения timezone preview выполнить отдельный шаг:

```bash
sudo python3 -B scripts/school-1panel-backup-schedules.py activate --apply
sudo python3 -B scripts/school-1panel-backup-schedules.py check
```

Без `--apply` изменяющая команда показывает план. `preflight` проверяет
установленную панель, pinned source и фактические старые unit без создания задач.
`stage` устанавливает только
неизменяемый публичный код и два выключенных native Cron-объекта с закрытым
разрешением на резервирование. В 1Panel создание Cron сразу включает его; поэтому
закрытое разрешение записывается **до** запроса создания, а затем статус задачи
явно переводится в `Disable` и перечитывается.

Каждый `probe` один раз вызывает задачу через native `/cronjobs/handle`. Задача
проходит весь маршрут Cron → Supervisor → неизменяемый worker, но worker не
запускает экспортер. Результат:

```text
STAGED_GATE_VERIFIED_NO_BACKUP
```

Проба требует новую запись Cron `Success`, собственный `dispatchId`,
`backupExecuted=false`, совпадающий публичный хеш программы и этот же маркер в
native stdout-журнале Supervisor. Успешная проба подтверждает управление и
доставку журнала. **Она не является новой резервной копией или проверкой её
восстановления.**

Worker сам печатает поток progress/result/error. Общий CLI не повторяет его
последний результат; для других команд итоговый JSON сохраняется. Проверка
уникальности маркера остаётся строгой: для собственного `dispatchId` нужна ровно
одна подходящая terminal-запись. Наличие маркера в native log не заменяет отдельную
проверку normalized event и HOST ZIP.

Уже установленный immutable wrapper нельзя исправлять на месте или заменять
ручным изменением `state.json`. Повторный `stage` с другим SHA не является
обновлением старого поколения. [Процедура docs58](58-SCHOOL-STAGED-SCHEDULE-REPAIR.md)
относится к конкретному историческому переходу из STAGED с выключенными
задачами. Для действующего расписания нужен отдельный проверенный переход
из ACTIVE с сохранением истории задач и прежнего выпуска для возврата.
Проверка копии настроек на H03 подтверждает новый пакет. Переключение
действующего расписания школы этой проверкой не выполнялось.

`activate` допускается только после обеих проб и вне 02:45–05:15 МСК, с запасом
не меньше десяти минут до ближайшего расписания. Переключение выполняется при
свободной блокировке DB-экспортера и отсутствии выполняющихся копий. Сначала
включаются закрытые native задачи, затем отключаются оба прежних таймера; после
чтения фактических состояний атомарно фиксируется владелец `1panel`. Файлы прежних
unit, архивы и история не удаляются.

Прежние ограничения времени выполнения сохранены: DB runner с `--apply`
допускает резервирование только с 03:00 до 05:00 МСК, settings runner — с 04:00
до 05:00 МСК. Поэтому ручная кнопка «Запустить» в native Cron после `activate`
днём завершится блокировкой по этому ограничению, без резервной копии. Закрытые
`stage`-пробы можно выполнять днём: они проверяют маршрут запуска и журналы, но
не вызывают runners. Первую автоматическую native копию отмечают только после
фактического ночного выполнения. Дневная проверка нового конфигурационного
архива выполняется отдельным уже принятым manual snapshot/cloud helper и не
считается первым автоматическим запуском.

На восстановленной чистой Linux/1Panel установке, где оба прежних backup-unit
отсутствуют, вместо обычного `stage` используется:

```bash
sudo python3 -B scripts/school-1panel-backup-schedules.py stage --fresh --apply
```

Этот режим не создаёт Linux-таймеров. Он всё равно требует восстановленные
идентичность ASA, данные, защищённые backup-каталоги, публичный age-recipient,
конфигурацию Яндекс Диска и проверенные runners. Проверка полного развёртывания
на чистой Linux остаётся отдельным испытанием; наличие режима не является её
результатом.

## Поведение при сбоях и повторных запросах

Намерение запуска записывается до native API-запроса. Если ответ потерян,
контролёр наблюдает тот же `dispatchId`; второй запуск автоматически не отправляет.
Незавершённое или неоднозначное намерение блокирует повтор до сверки действительного
состояния. Не следует удалять receipt или выдавать новый идентификатор для обхода
такого STOP. Повтор завершённой успешной копии в те же сутки не запускается.

Оба исходных runners используют общую блокировку
`/run/lock/asa-school-nightly-db.lock`. Она сохранена для совместимости с уже
существующими ручными копиями. Дополнительные блокировки отделяют переключение
владельца расписания, запрос запуска и выполняющийся worker.

При частичном переключении контролёр закрывает разрешение, выключает native
задачи и восстанавливает прежние состояния таймеров только после подтверждения,
что резервирование не выполняется. Если это доказать нельзя, результат —
`RECOVERY_REQUIRED`; успешный откат не выдумывается. Откат выполняется той же
проверенной версией программы:

```bash
sudo python3 -B scripts/school-1panel-backup-schedules.py rollback --apply
```

Откат не вызывает `stop`, `restart` или `delete` для backup-процессов и не чистит
native историю. Отмена наблюдателя Cron оставляет сам backup-процесс работающим;
его результат нужно смотреть в Supervisor и durable run-receipt. Глобальный
перезапуск Supervisor или перезапуск ОС во время DB-экспорта данным срезом не
испытан и не объявлен безопасным сценарием восстановления.

## Журналы и доказательства результата

Native Supervisor stdout содержит структурированные сообщения с `timestamp`,
`level`, `dispatchId`, `kind` и статусом. Ошибки содержат безопасные коды причин.
Публичный `dispatchId` позволяет связать запись Cron, процесс и событие
нормализованного журнала. Источник `linux:1panel-supervisor` настраивается срезом сборщика;
доставку native маркера до нормализованного события и экспортируемого ZIP
принимают отдельно, по фактическому `dispatchId`.

| Путь | Содержание |
|---|---|
| `/var/lib/server-settings/1panel-backup-schedules/state.json` | Владелец расписания, native ID, закрытые пробы, публичные pins |
| `/var/lib/server-settings/1panel-backup-schedules/dispatch/` | Намерения запуска и исход наблюдения |
| `/var/lib/server-settings/1panel-backup-schedules/runs/` | Фактический результат worker и ссылка на собственный архив |
| `/var/lib/server-settings/1panel-backup-schedules/output/` | Исходный вывод runner, root 0600; не публикуется и не подменяет normalized ZIP |
| `/opt/1panel/tools/supervisord/log/` | Native stdout/stderr процессов, доступны через Supervisor |
| `/opt/asa-school-panel-schedules/releases/` | Неизменяемые публичные версии wrapper |

Успех backup требует нулевой код собственного runner, единственное имя архива в
его собственном выводе и новую свежую receipt с тем же архивом. Копия другого
параллельного ручного запуска не может подставиться как доказательство. Проверка
SHA256 относится к ciphertext roundtrip Яндекс Диска; новый decrypt/restore этим
статусом не подразумевается.

Новая immutable settings-release включает точную разрешённую конфигурацию
Supervisor, wrapper и владельца расписания. Копии прежних release не удаляются.
Изменение `school-nightly-settings-backup.py` ограничено pin расширенного snapshot
helper; изменение его legacy installer — этими публичными pins. Сам DB runner,
форматы архивов, namespaces Яндекс Диска и внешнее хранение приватного age-ключа
не меняются.

## Граница приёмки

Локально проверяются потерянные ответы API, конфликт владельца, создание Cron в
состоянии Enable, частичный перенос/откат, отказ от остановки активного процесса,
неоднозначные повторные запуски, сохранение существующих восстановлений и
связывание receipt с собственным архивом. Это тесты кода, а не серверная приёмка.

После реального применения в `snapshots/`, матрице проверок и существующей
таблице оборудования записывают: точные Git SHA, native Cron ID, результаты обеих
проб и доставки в normalized ZIP, фактические состояния старых таймеров,
состояние процессов, первую автоматическую DB/config-copy и её ciphertext SHA.
Статусы «первая автоматическая копия» и «чистая установка» остаются
`NOT_PROVEN`, пока соответствующее испытание не выполнено.
