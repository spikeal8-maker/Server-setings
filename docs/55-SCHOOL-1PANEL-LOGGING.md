# PANEL-LOG-01: воспроизводимое журналирование через 1Panel

## Принято на школьном сервере: ACTIVE — native collector и Cron

Срез **08.11.2026 16:41:02 UTC / 19:41:02 МСК**. Collector и оба расписания
работают под нативным управлением 1Panel. Новая ручная ACTIVE-копия конфигурации
прошла полный облачный цикл, authenticated decrypt и изолированную проверку.
H04 и основной домен не переключены; общего производственного допуска этот
этап не выдаёт.

| Проверка | Принятый результат |
| --- | --- |
| Исходники и приложение | Server-setings PR86/main `57ca0440`, wrapper `4b391bbd`; host tools `17d937c0`, app images `3e871347`, schema 307 |
| Collector | Native Supervisor PID 2770698; свежий heartbeat 11,068 с, source/digest проверены. Все 6 рабочих контейнеров healthy, перезапуски 0 |
| Repair и пробы | Закрытый repair и независимая сверка приняты; ID 1/2 сохранены. Новые DB record 2 и settings record 3 дали уникальные terminal markers |
| Доставка журналов | Оба маркера подтверждены native → normalized/published → HOST ZIP; CRC/part SHA, application ZIP и временная очистка PASS |
| Расписания | ACTIVE, owner 1panel, обе задачи Enable; старые backup timers disabled/inactive. DB — 03:20, settings — 04:00 Europe/Moscow, следующие запуски 09.11 |
| Конфигурационная копия | 27 613 976 байт, cipher SHA256 `ccb146c2…`; полный Яндекс upload/download, authenticated decrypt и isolated verify PASS |
| Последние метаданные журналов | 26/27 OK. Единственный incomplete — docker-lifecycle с историческим possible gap при свежем текущем сборе. Оба AI ledger OK/20 с |

### Точная копия и проверка восстановления

Архив `asa-school-config-01-20261108T162748Z-d0ba6e1f.tar.age` находится в
`app:/School-VM100/Configuration-Backups`. SHA256 ciphertext:
`ccb146c2eebc68de08710f7954b54b7790eecc1011c14c05017f220b550db055`.
CREATE fcdb завершён в 16:29:35 UTC, полный cloud roundtrip 2ffc — в 16:32:32 UTC.
D2 PID 41112 завершился exit 0 за 14,54 с; VM receipt наблюдался в 16:40:21 UTC.
Root и независимый peer приняли **70 payload files плюс manifest**, permissions
manifest, integrity OK у `agent.db`, `core.db`, `xpack.db`, оба normalized ZIP.

Изолированно проверены ACTIVE owner1panel/generation4ccf, оба native backup INI,
immutable wrapper4b, collector bridge17d и оба ожидаемых probe markers в HOST ZIP.
`livePathsWritten=false`: рабочие пути этим восстановлением не заменялись.
Данные PostgreSQL/MinIO и namespace Database-Backups этой конфигурационной
операцией не менялись. [Точные recovery receipts, операции и хеши](../snapshots/2026-11-08/school-native-logging-final-checkpoint-20261108T164102Z.json)
и [порядок конфигурационного резервирования](54-SCHOOL-MANAGED-CONFIGURATION-BACKUPS.md).

### Разные срезы журналов

| Артефакт / время | Подтверждённый объём и граница |
| --- | --- |
| Отдельная marker-проверка | HOST ZIP 543 части / 223286 записей; два конкретных marker-события подтверждены |
| Новая ACTIVE-копия, проверенная после расшифровки | HOST ZIP 544 части / 223525 записей / 178557171 raw B; application ZIP 20 частей / 25033 записи / 20138069 raw B; CRC/part SHA PASS |
| Текущая host metadata 16:41 UTC | 611 сегментов / 223791 запись; отдельная history metadata 610/610 завершена примерно на 20 с раньше |

Последняя строка не является атомарным срезом или новым побайтовым аудитом:
contents/checksums сегментов при этой проверке не перечитывались. Active storage
174790921 B, physical 175132535 B, retired 341614 B; политика 1 GiB / 30 дней,
trim=false. Health OK, длительность цикла 4890 мс, возраст последнего успеха 19 с.
Docker rotation — json-file 10m × 5 у шести контейнеров. Supervisor stdout/stderr
2MB; число backups явно не задано, неразрешённые defaults не объявлены проверенными.

**Global coverageComplete=false.** Ранее зафиксированные 16 possible Docker gap
windows не переоценивались; число утраченных событий неизвестно. Полный authenticated
срез остаётся 24/27 от 14:18 UTC и не повторялся. Новая metadata 26/27 не подменяет
его и не объявляет новый native writer integrity PASS. Поздняя публикация 296 own
audit-событий принята d99f/e947, но исходный burst STOP и недоказанная задержка
в первых 50 с сохранены; повторного burst не было.

### Практические границы

Нативное управление принято для collector и двух расписаний на существующей
школьной установке. ASA и asa-cloud отображаются как внешние Compose; этим
этапом они не стали native-created/adopted приложениями панели. Stage не является
универсальным bootstrap Linux: нужны подготовленные host/instance, ASA,
Supervisor/API, установленный runner, транспорт и защищённые каталоги/ключи.
[Граница stage и prerequisites](56-SCHOOL-NATIVE-BACKUP-SCHEDULES.md) и
[проверенный путь закрытого repair](58-SCHOOL-STAGED-SCHEDULE-REPAIR.md).

Первая автоматическая ночная копия — **NOT_PROVEN**; реальная чистая Linux-установка —
**NOT_RUN**. В этот пакет включены принятые root и peer два точных marker CLI,
18 проверок и [процедура docs59](59-SCHOOL-NATIVE-MARKER-ACCEPTANCE.md). Команды
не зависят от нашей scratch-среды; их область — STAGED/legacy до activate,
без повторного запуска на уже ACTIVE и без сертификации fresh/originalOwner=none.
До основного перехода остаются браузерный класс/школьная сеть, роли/курсы/медиа,
финальная свежая синхронизация H04, вопрос AI/VPN. Полный live rollback не выполнялся.

Постоянная private age identity хранится на съёмном носителе владельца.
Использованный courier: pinned SSH/QGA stdin → VM transient sealed memfd → age;
descriptor закрывается. Постоянный именованный VM key-file не создаётся, значение
не попадает в env/argv/log/Git. Ключ временно проходит в память VM. После аварийного
восстановления потребуется новая регистрация ChatGPT/MCP/OAuth:
`gatewayOAuthContinuity=false`; сохранность прежней OAuth-сессии не доказана.

Таблица записана и перечитана 19:52:15 МСК: 16/16 значений, 43/43 соседей,
metadata 59/59 сохранены. Пакет объединяет фактическую приёмку и воспроизводимые
команды проверки; production go этим не объявлен.

## Датированная история до нового среза

> Ниже сохранены прежние редакции. Слова «текущий», «ожидается» и NOT_RUN в них относятся к указанным датам; текущий статус дан выше.


## Текущие результаты: 08.11.2026, 14:19 UTC / 17:19 МСК

**Управление collector через native 1Panel принято; следующие проверки ещё
не закрыты.** Host source — `17d937c01660025e6b5030f44bcb0d776e7c79aa`,
действующие app images — `3e8713476cafedda3e24a39665458e833efd7c22`, schema307.
Принятые adoption и независимое чтение в14:08 UTC подробно сохранены ниже.

| Проверка | Фактический исход и граница |
| --- | --- |
| Collector | Native Supervisor RUNNING, PID2770698; autostart/autorestart=true, legacy disabled/inactive |
| Cron preflight | Операция `d51fb8bc550540c0a3c24c31723b872f`,14:10:17 UTC,exit2: `ScheduleError` на `identity-and-published-source`; `stepMayHaveCommitted=false` |
| Cron stage/probes/activation | Не запускались; старые backup timers03:20/04:00 МСК продолжают работать; причина preflight STOP диагностируется |
| Bounded burst | Операция `6fb61a28ba5143d89e98bd85690ddf11`,14:17:32–14:19:02 UTC,exit2: `BURST_DRAIN_NOT_PROVEN` |
| Auth и сохранность burst | 296 HTTP200 и четыре отрицательных проверки:401×2/403×2; scope/NULL-inclusive foreign controls/cleanup PASS, завершение DB child известно |
| Проверка доставки burst | В завершённом окне eligible296; quiet DB sources fresh. Observer дал matchedNativeKeys0/observationRows0/publishedRows0, поэтому доставка не принята |

Проверяется read-only соответствие source/key наблюдателя каноническому
producer. Нулевой результат сопоставления сам по себе не доказывает потерю
событий или причину ошибки. Повторный burst не выполнялся; cursors и gap history
не сбрасывались. Прежние transport INVALID_ARGUMENT/PATH_TRUST STOP и их
reconciliation сохранены в датированной истории ниже.

Отдельный authenticated status-срез14:18:11 UTC наблюдал27 required sources:
24 OK/3 incomplete. Native counters7490 accepted=persisted, drops/failures/pending0;
AI journal pending, metrics pending/stale, Docker fresh с историческим gap.
Это не полный PASS покрытия. Последующий drain читает quiet-source metadata,
не повторяя весь authenticated requiredCoverage. Исторические16 possible
Docker gap windows сохраняются; доказанное количество потерянных событий неизвестно.

### Опубликованный код и установленная версия

Server-setings [PR84](https://github.com/spikeal8-maker/Server-setings/pull/84)
слит в14:16:28 UTC /17:16:28 МСК, main
`ca18ec36097d47d732b33d57d88458e8c111fd31`; exact-head CI42488362495,
job128755101227 SUCCESS. Root проверил tree2a49e9bc и parents eb21+a608.
Это source fix для будущего fresh-native snapshot; 64 независимых root-теста
прошли. Текущая VM не перепривязана: Settings eb21, snapshot helper2a0,
Cron wrapper444 остаются установленными. Реальная чистая Linux-установка
**NOT_RUN**. Исправление публикации не является применением на VM.

### Что остаётся принять

Нужны результат диагностики burst и доказательство доставки его событий,
разбор Cron preflight STOP, отдельные stage/две закрытые пробы/clock activation,
native marker→normalized event→host ZIP, новый конфигурационный архив и его
облачный roundtrip/decrypt/isolated verify. Первая автоматическая native копия
ещё не доказана. Полное новое authenticated coverage и производственный переход
не приняты; H04 и основной домен не переключались.

Приватная age identity постоянно хранится на съёмном носителе владельца.
Принятый courier использует существующий pinned SSH/QGA stdin → transient sealed
VM memfd → age; descriptor закрывается после использования. Постоянный VM
key-file не создаётся, значение не попадает в env/argv/log/Git. Ключ временно
проходит в память VM; это не полностью офлайн-расшифровка на D2. После аварийного
восстановления нужна новая регистрация/авторизация ChatGPT OAuth/MCP; текущий
доступ этой оговоркой не объявляется требующим повторного входа.

Сводка: [текущий фактический checkpoint](../snapshots/2026-11-08/school-native-logging-checkpoint-20261108T141902Z.json).
Google Sheet обновлена17:20:02 МСК и перечитана17:20:47:16/16 значений,
43 соседа и metadata сохранены. Этот batch содержит adoption/PR84/Cron STOP;
позднее переданный root результат burst добавляется следующим checkpoint
после диагностики, а не считается уже записанным.

## Принятые этапы и инструкция: сохранённая редакция 14:08 UTC

Статус среза на 2026-11-08 14:08 UTC: **source-only переключение ASA и переход
collector под native 1Panel Supervisor приняты**. Действующий collector имеет
manager `1panel-supervisor`; прежняя systemd-служба disabled/inactive. Приложение
и database schema остаются на прежнем выпуске. Native Cron markers, bounded
burst, новый облачный архив и полнота требуемого покрытия принимаются отдельно.

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

## Фактически принятый stopped stage: 12:40 UTC

Settings PR [83](https://github.com/spikeal8-maker/Server-setings/pull/83)
принят в main commit `eb21b1bbb8852c38a3c5b4d9ed6cfa3f72fa58f4`,
tree `7c000184e4b26e9433af4721cdc48577604380ea`. Проверены точные 14 Git blobs,
PR CI `42480730589` и main push CI `42480826529`: SUCCESS.

Применение exact reviewed prerequisite/stage-пакета
`a5f4f5fbc2be985776d676f2fafa36dececeeef314131c8cec16c9cfabd38578`:
операция `081b68be8edd48c4a7ccad2d11088295`,
2026-11-08 12:40:11.199–12:40:25.902 UTC, exit 0, 14.72 секунды.

| Проверка после stage | Фактический результат |
| --- | --- |
| Supervisor | `4.2.5-1ubuntu0.1` установлен; native status running, ctl доступен, include инициализирован |
| Native collector | `asa-log-collector-asa-lab-school`: STOPPED, autostart=false, autorestart=true |
| Прежний collector | systemd enabled/active, прежний PID 867926 |
| Manager metadata | Native manager ещё не выбран; исходные байты/UID/GID/modes сохранены |
| Canonical ASA | Detached clean `3e8713476cafedda3e24a39665458e833efd7c22` |
| Приложение | Все 8 контейнеров с прежними ID/images/startedAt/restartCount; deployment не выполнялся |
| Private index | Тот же inode 816721; size 153305088 bytes на момент наблюдения |
| Settings source | Exact checkout `/opt/asa-school-panel-source/releases/eb21b1bbb8852c38a3c5b4d9ed6cfa3f72fa58f4` |

Независимый readback `18c154e9969e44bebf212c61c3dcbc1d` подтвердил STOPPED native
program и работающий прежний collector. Поэтому на этом этапе в 1Panel видна
подготовленная программа, но управление действующим сборщиком ещё не передано.
Сводка сохранена в
[stage receipt](../snapshots/2026-11-08/school-1panel-logging-stage-20261108.json).

Для следующего source-only handover отдельно подготовлен observer сохранности
private index. Read-only capability `d5971e0c38ec45328accfcd699a934d5` обнаружил
140 state rows, максимум одного значения 500149 bytes, суммарно 643320 bytes;
aggregate snapshot занял 0.03397 секунды. Исходный 128 KiB per-value limit не
прошёл, и полные значения в этой операции не читались. По фактическому размеру
принят предел 1 MiB на значение при сохранении 4 MiB total / 4096 rows / 2 seconds;
будущий handover повторно измеряет aggregate в той же read-only транзакции до
полного чтения. Это подготовка наблюдения, не доказательство уже выполненного
adoption. Существующие failed preflight и исходный capability receipt сохранены.

## Фактически принятый collector handover: 14:08 UTC

ASA PR [1024](https://github.com/spikeal8-maker/asa-lab/pull/1024) принят обычным
merge в commit `17d937c01660025e6b5030f44bcb0d776e7c79aa`, tree
`9c43779f3b74588eeb48b8b83b84fe1eaeabc631`. Проверены точные product blobs,
обязательный job `ASA / required` (`128751110336`) и все пять связанных runs:
SUCCESS. Исправления тестов для Windows не изменяли production algorithm.

Операция `826479b05afe47a6b85a425bcb22d129` выполнила reviewed source-only
fast-forward и canonical adoption за 16.942 секунды, exit 0;
2026-11-08 14:07:57.984–14:08:14.904 UTC. Exact operator source SHA256:
`7c9e2d3ffe2b1aae158ee541a6038f631b6ec8b530745e47060bde7f75b17fe1`.

| Проверка | Фактический результат |
| --- | --- |
| Canonical host-tools source | Detached clean `17d937c01660025e6b5030f44bcb0d776e7c79aa` |
| Application runtime / schema | Прежний release `3e8713476cafedda3e24a39665458e833efd7c22`, schema 307; deployment не выполнялся |
| Collector в 1Panel | RUNNING, PID 2770698; autostart=true, autorestart=true, manager `1panel-supervisor` |
| Прежний collector | systemd disabled/inactive; второго writer нет |
| Handover receipt | `accepted`; target identity/settings/log configuration совпадают с текущими |
| Новый процесс | Новый private nonce, текущие revision/digest, новый успешный cycle; повторный native liveness readback |
| Private index | Тот же inode 816721; reset/import/delete не выполнялись |
| Граница остановки | Все 140 state keys и event counts до/после смены metadata, пока оба writer STOPPED, совпали |
| Прогресс после старта | 9 live database cursors не уменьшились; 219414 events до запуска → 219425 после нового cycle |
| Историческое покрытие | Все 16 Docker gap windows и их private metadata сохранены; число доказанно утраченных событий неизвестно |
| Приложение и метаданные | Все 8 контейнеров, application configuration и UID/GID/modes private metadata сохранены |
| Backup compatibility | Пин старого API runtime/exporter и чтение normalized catalogs прошли до/после; новый backup не запускался |

Независимая read-only операция `e9f4864d4ea8430cb4921dd27a8dad78`,
2026-11-08 14:08:51.480–14:08:53.035 UTC, exit 0, повторно подтвердила живой
native процесс дважды вокруг наблюдения. PID 2770698 и его private identity
оставались прежними; heartbeat 14:08:48.114 UTC и cycle 14:08:38.441 UTC относятся
к новому процессу, стартовавшему 14:08:08.106 UTC. В индексе наблюдались 219431
событие, все опубликованы, 143 state rows и прежние 16 gap windows. Увеличение
state rows после запуска допустимо: появляются записи нового native source;
равенство состояния требовалось на остановленной границе, а после старта
проверяется сохранность и законный прогресс.

Сводка операций и их точные hashes:
[actual adoption receipt](../snapshots/2026-11-08/school-1panel-collector-adopted-20261108.json).
Ни nonce, ни содержимое private cursors в публичную сводку не включены.
Эта приёмка не доказывает доставку Cron marker в normalized ZIP, прохождение
нагрузочного burst или полную историческую полноту журнала.

Первый запрос был отклонён до создания operation из-за gateway command limit
32000 символов. Следующий проверенный запрос остановился до любых изменений
на строгом parent trust guard: `/run/lock` является штатным root-owned sticky
каталогом 1777. Отдельное read-only reconciliation
`152a58e756b94862af68723dedb3a096` подтвердило clean source 3e, прежний PID,
STOPPED native process, прежние manager metadata и отсутствие handover receipt.
Корректирующий packet изменил только проверку трёх заранее существующих locks:
разрешён точный наблюдавшийся sticky `/run/lock`, inode/owner/mode проверяются
до открытия, через fd и после nonblocking flock. Общий `trusted()` не ослаблялся;
новый lock-файл, chmod или удаление не применялись. Исходные STOP receipts
сохранены и не заменены поздним PASS.

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
Принятый collector source revision — `17d937c01660025e6b5030f44bcb0d776e7c79aa`; это отдельная величина.
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
