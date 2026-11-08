# PANEL-LOG-01: воспроизводимое журналирование через 1Panel

Статус этого пакета: **локальный кандидат, read-only preflight принят;
runtime install/adoption не выполнялись**. Школьная VM не объявляется переведённой
на Supervisor до отдельного фактического подтверждения.

## Факты школьного узла на 2026-11-08

Наблюдение `9b3b5721a0fa4769ad8791c14e464404`,
`2026-11-08T12:04:16.604868+00:00`, exit 0:

| Проверка | Наблюдаемый результат |
| --- | --- |
| Узел / instance | `asa-school-01` / `fc0ba60fe1d946bba1694d73557cc11f` |
| Canonical root / project | `/srv/asa-lab` / `asa-lab-school` |
| Canonical source | `3e8713476cafedda3e24a39665458e833efd7c22`; tracked clean, untracked 0, active hooks 0 |
| 1Panel / Python | v2.3.2 / `/usr/bin/python3`, 3.12.3 |
| Supervisor | Пакет/ctl отсутствуют, foreign configs 0, native programs 0 |
| APT simulation | Только NEW `supervisor=4.2.5-1ubuntu0.1`; upgrades/removals 0 |
| Коллектор | `system-root`, прежняя systemd-служба enabled/active, PID 867926 |
| Private index | inode 816721, 150335488 bytes; содержимое не читалось preflight |
| Известный ASA auto-updater | settings=false, candidate units 0, native Cron 0 |

Это проверка известного ASA updater, а не утверждение об отсутствии любой
сторонней автоматизации. Две database sources стали fresh ещё на старом коде
при спокойной нагрузке; этот результат не приписывается новому исправлению.
Существующие 16 исторических Docker coverage-gap windows сохраняются.

Нативный Compose inventory показывает ASA и cloud storage как внешние проекты
(`createdBy=""`). Видимость в панели не означает panel-created/adopted. В этом
пакете новый жизненный цикл касается collector Supervisor. Backup Cron/Supervisor
имеют отдельный пакет PANEL-CRON-01; старые расписания сохраняются до принятия
замены.

## Что управляется в панели

`scripts/install-asa-1panel-collector.py` запускается из 1Panel Terminal на
root-controlled checkout принятого commit. Он следует официальной предпосылке
Ubuntu/Debian: установка пакета Supervisor. План проверяет только новые пакеты,
запрещает upgrades/removals, выводит SHA256 набора точных версий. При apply
обязателен тот же SHA256; перед установкой симуляция повторяется. `apt update`,
OS upgrade и перезапуск приложений в процедуру не входят.

Свежий Supervisor инициализируется через официальный `/hosts/tool/init`, а не
редактированием 1Panel database. Эта операция меняет include.files и
перезапускает Supervisor, поэтому предварительно требуются пустой distro
conf.d, отсутствие running programs и сохранение исходной конфигурации.
Существующий уже инициализированный Supervisor повторно не инициализируется.

Native create/update вызывает глобальные supervisorctl reread/update. Для
текущего школьного handover поэтому stage/adoption коллектора выполняются
до stage backup-программ, при подтверждённом отсутствии чужих pending INIs.
На другом существующем Supervisor это отдельная предпосылка review: нельзя
считать произвольные чужие pending configuration изменения безопасными.

Затем native API создаёт видимую программу `asa-log-collector-<project>` с
`autostart=false`, `autorestart=true`, `numprocs=1`. Установленный bridge
`/usr/local/sbin/asa-1panel-collector` выполняет команды только для своего
canonical root/project/instance. Приватная конфигурация и API key находятся
на узле с правами 0600. Публичные документы содержат только пути и provenance.

Installer допускает прежний canonical adapter только пока manager остаётся
legacy, чтобы заранее подготовить STOPPED native program. Он не выбирает
manager и не запускает collector. Команды run/adopt/reconcile требуют уже
новый canonical adapter. Так можно сначала принять stopped stage, затем
выполнить отдельно проверенный source-only fast-forward ASA и только после
него перейти к adoption.

## Повторение из Git на Linux с 1Panel

Нужны Ubuntu/Debian с 1Panel v2.3.2, подготовленная canonical ASA installation,
поддерживаемый root collector mode и группа чтения API. Application/data
deployment остаётся канонической процедурой ASA. Для нового collector
используйте `tools/log_collector.py prepare --mode system-root --reader-gid`
с выбранной ненулевой группой: prepare создаёт metadata/directories, но не
создаёт systemd runner. Installer регистрирует отсутствующую identity сам.
Чистый fresh-install прогон этого пакета пока **NOT_RUN**.

Следующий полный блок — интерактивная подготовка из 1Panel Terminal. Revision
вводится из принятого review; ветка main не заменяет точный SHA. URL панели —
только loopback с фактическим портом. Ключ уже должен быть сохранён владельцем
в указанном приватном файле; значение ключа не вводится в командную строку.

```sh
set -eu
umask 077
read -r -p 'Reviewed Server-setings commit (40 lowercase hex): ' ASA_PANEL_SETTINGS_REV
printf '%s\n' "$ASA_PANEL_SETTINGS_REV" | LC_ALL=C grep -Eq '^[a-f0-9]{40}$'
read -r -p 'Absolute canonical ASA root: ' ASA_PANEL_ROOT
read -r -p 'Local 1Panel URL with port: ' ASA_PANEL_ORIGIN
read -r -p 'Absolute root-owned 0600 API key file: ' ASA_PANEL_KEY_FILE
ASA_PANEL_CHECKOUT="/root/asa-1panel-source-$ASA_PANEL_SETTINGS_REV"
test ! -e "$ASA_PANEL_CHECKOUT"
git clone --no-checkout https://github.com/spikeal8-maker/Server-setings.git "$ASA_PANEL_CHECKOUT"
git -C "$ASA_PANEL_CHECKOUT" checkout --detach "$ASA_PANEL_SETTINGS_REV"
test "$(git -C "$ASA_PANEL_CHECKOUT" rev-parse HEAD)" = "$ASA_PANEL_SETTINGS_REV"
test -z "$(git -C "$ASA_PANEL_CHECKOUT" status --porcelain)"
python3 -B "$ASA_PANEL_CHECKOUT/scripts/install-asa-1panel-collector.py" \
  --root "$ASA_PANEL_ROOT" --panel-host "$ASA_PANEL_ORIGIN" --key-file "$ASA_PANEL_KEY_FILE"
```

Просмотрите `newPackages`, `aptPlanSha256`, `supervisorInitializationRequired`,
canonical identity и source SHA. Приведённый apply запрашивает именно ранее
проверенный digest, а не принимает автоматически новый результат симуляции:

```sh
read -r -p 'Reviewed aptPlanSha256: ' ASA_PANEL_APT_PLAN_SHA
printf '%s\n' "$ASA_PANEL_APT_PLAN_SHA" | LC_ALL=C grep -Eq '^[a-f0-9]{64}$'
python3 -B "$ASA_PANEL_CHECKOUT/scripts/install-asa-1panel-collector.py" \
  --root "$ASA_PANEL_ROOT" --panel-host "$ASA_PANEL_ORIGIN" --key-file "$ASA_PANEL_KEY_FILE" \
  --install-supervisor --apt-plan-sha256 "$ASA_PANEL_APT_PLAN_SHA" --apply
/usr/local/sbin/asa-1panel-collector status
/usr/local/sbin/asa-1panel-collector adopt
```

На уже подготовленном Supervisor installer не устанавливает пакет повторно.
При другой 1Panel version, изменившемся APT plan, чужом существующем bridge,
иной identity или unsafe paths операция блокируется. Она не исправляет чужое
состояние вслепую. API key всегда остаётся только в памяти процесса/приватном
файле; native process environment пуст.

## Контролируемый handover коллектора

После независимого review конкретного плана:

```sh
/usr/local/sbin/asa-1panel-collector adopt --apply
/usr/local/sbin/asa-1panel-collector status
```

Порядок: canonical maintenance lock → durable private receipt → disable/stop
только прежнего collector → native absence proof → atomically сохранить manager,
fresh revision/digest и расширение fileSources → включить native program →
fresh heartbeat с новым nonce → receipt `accepted`.

Новый manager фиксируется одновременно по смыслу в installation и runner
metadata, но три записи на диске не считаются единой транзакцией: частичный
переход сохраняет `prepared` receipt и требует recovery. Файлы metadata
сохраняют прежние UID/GID/mode. Private index, событийные сегменты и cursors
не заменяются. Native program использует тот же canonical `tools/collect_logs.py`.

Bridge добавляет `fileSources.source="linux:1panel-supervisor"`; каталог
выводится из настоящей native configuration и подтверждается stdout/stderr
путями созданной программы. Прежние fileSources не удаляются. Это источник
host logging, в том числе для двух backup Supervisor stdout/stderr файлов.
После его добавления нужен новый required-source catalog observation.

После native autostart bridge дожидается явного RUNNING. В 1Panel v2.3.2
промежуточная короткая строка Supervisor STARTING/STOPPING может не попасть
в status parser и выглядеть как null/пустой status внутри существующей
регистрации. Такой ответ допускает только ограниченное ожидание, не start
повторно и не доказательство STOPPED. По истечении срока результат остаётся
STOP. Это отличается от null всего списка программ на пустом include dir.

При сбое новый native writer останавливается и STOPPED подтверждается до
восстановления прежнего writer. Неопределённый API-ответ оставляет прежний
writer выключенным и выводит `HANDOVER_RECOVERY_REQUIRED`. Следующий запуск
не объявляет незавершённый receipt успешным. Явное восстановление:

```sh
/usr/local/sbin/asa-1panel-collector recover
/usr/local/sbin/asa-1panel-collector recover --apply
```

Recovery принимает только точные original/target metadata и receipt
`prepared`/`rolled_back`; `accepted` не откатывается автоматически. Receipt
сохраняется; после проверки его отдельно архивируют перед повторным adoption.
Удаление native program не используется для rollback: native delete удаляет
и лог-файлы. Восстановление из архива также сначала проверяется изолированно.

## Source-only выпуск и backup compatibility

Для школы выбран clean canonical source-only fast-forward на точный принятый
ASA host-tools commit. До него проверяются clean tracked/untracked state,
hooks, известный auto-updater и полный diff allowed paths. Compose, API exporter,
database contract и app runtime inputs сохраняются. Действующие images/build
label остаются `3e8713476cafedda3e24a39665458e833efd7c22`, schema 307.
Collector source revision после принятия будет новым; это отдельная величина.
Никакого `git pull/reset` и пофайлового live hotpatch в процедуре нет.

Snapshot helper добавляет необязательные, но целостные группы native Supervisor,
bridge/config/handover receipt, native backup INIs/state и immutable schedule
wrapper. Неизвестные native INIs требуют нового review. Секретные конфиги
попадают только в age-encrypted archive. Raw logs, dispatch/runs, private
collector index и данные PostgreSQL не расширяют этот configuration allowlist.
Старые архивы без новых групп остаются допустимыми.

PANEL-CRON-01 готовит новый immutable settings backup release с новым helper
pin; прежний принятый release не переписывается. Новый release/preflight и
изолированное восстановление проверяются перед принятием нового расписания.

## Обязательная фактическая приёмка

1. Git exact commits и CI для обоих репозиториев; source-only scope и неизменные
   application images/schema отдельно подтверждены.
2. В 1Panel виден ровно один RUNNING collector, прежний collector disabled и
   inactive; manager metadata совпадают, heartbeat имеет текущие revision/digest
   и новый nonce. Bridge status/reconcile проходят тем же путём native API.
3. Существующий private index inode и исторические windows сохранены; выполняется
   содержательная bounded database burst проверка исправления LOG-FIX-01.
4. Gated Cron marker имеет `timestamp`, `level`, `dispatchId`,
   `STAGED_GATE_VERIFIED_NO_BACKUP`, `backupExecuted=false`; один и тот же dispatchId
   находится в native stdout, нормализованном `linux:1panel-supervisor` и host ZIP.
   Это не доказательство выполнения реального backup.
5. Проверены новый encrypted settings archive и изолированное восстановление;
   native backup schedules приняты отдельным staged/commit handover.
6. Обновлены snapshot, docs/08, инструкция и таблица оборудования с временем,
   источником фактов, next step и ограничениями. Нельзя выводить полный PASS
   покрытия из одного fresh heartbeat или из старого каталога на 26 IDs.

## Источники API и историческая оговорка

API зафиксирован по официальному
[1Panel v2.3.2, commit 65243c68c463cc055ab044093f641ea5d2e9e28b](https://github.com/1Panel-dev/1Panel/tree/65243c68c463cc055ab044093f641ea5d2e9e28b):
[host_tool service](https://github.com/1Panel-dev/1Panel/blob/65243c68c463cc055ab044093f641ea5d2e9e28b/agent/app/service/host_tool.go),
[request DTO](https://github.com/1Panel-dev/1Panel/blob/65243c68c463cc055ab044093f641ea5d2e9e28b/agent/app/dto/request/host_tool.go),
[host routes](https://github.com/1Panel-dev/1Panel/blob/65243c68c463cc055ab044093f641ea5d2e9e28b/agent/router/ro_host.go),
[официальная установка Supervisor](https://1panel-dev.github.io/docs/v2/user_manual/toolbox/supervisor/).

`GET /api/v2/hosts/tool/supervisor/process` в этой версии может создать
отсутствующий native config directory. Первое наблюдение 11:36 с `[]` могло
иметь этот lazy side effect. Поле `mutations:0` исходного запроса описывает
намерение caller, не доказывает отсутствие FS-эффекта; исходный receipt не
переписан. Существующий пустой directory возвращает nil slice → `data:null`.
Bridge принимает null только при code 200 и неизменном обычном пустом
root-controlled directory до/после запроса. Другие формы остаются STOP.
Отдельные failed preflight receipts сохранены; они не заменены поздним PASS.
