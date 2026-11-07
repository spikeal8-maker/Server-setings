# VM100: прямой MCP с полным администрированием узла

## Текущий результат: ChatGPT подключён, прямая приёмка PASS

**07.11.2026 22:00:54 UTC / 08.11.2026 01:00:54 МСК** подключение владельца
завершено и проверено непосредственно из ChatGPT. Плагин **«ASA Lab — школа
VM100»** обнаружен с точным набором **16 инструментов**. Реальные вызовы плагина
подтвердили:

- `asa_admin_status`: enabled=true, hostname `asa-school-01`, installation
  `asa-school-vm100`, UID0 и разрешённый владельцем root shell.
- Официальный `get_system_info`: code200, Ubuntu/Linux, amd64.
- `asa_admin_execute`: безопасная команда чтения hostname/UID выполнилась
  успешно от root; `asa_operation_get` вернул status=success, exit_code=0,
  timed_out=false, полный результат и журналы начала/завершения.

Operation ID этой проверки — `749d1a3bd71849a4a97c4da8422faf7e`.
В результате команды получены `hostname=asa-school-01` и `uid=0`. Приёмка
проведена прямыми tool calls подключённого плагина; SSH/RDC/H03 для неё не
использовались. Команда читала состояние; штатный durable audit операции записан.

Таким образом, подтверждены обе части: серверная OAuth/MCP приёмка и фактическое
использование полного root-доступа из ChatGPT. Прежний статус подключения
**pending закрыт**. Установленный source SHA по-прежнему
`1d87c1c1578ae85ad982d1ac6f050ce4af5b700f`; эта запись не меняет runtime-код.

Специализированные ASA lifecycle/cloud tools остаются выключенными. Обновление
ASA, перенос пользователей/production и нагрузочная проверка этим действием не
выполнялись; готовность миграции требует отдельной программы проверки.

Датированный первичный снимок:
[school-host-admin-chatgpt-20261107T220054Z.json](../snapshots/2026-11-07/school-host-admin-chatgpt-20261107T220054Z.json).
Снимок серверной приёмки 21:40 UTC ниже сохранён без изменения: его
`chatgpt_connected=false` верен для более раннего момента.

## Принятая версия опубликована в main — 08.11.2026 01:26 МСК

[PR72](https://github.com/spikeal8-maker/Server-setings/pull/72) объединён
07.11.2026 в 22:26:38 UTC; прежний Draft закрыт. Merge commit:
[`40ca6116a55f4426851d6857574bfa4d2dc57596`](https://github.com/spikeal8-maker/Server-setings/commit/40ca6116a55f4426851d6857574bfa4d2dc57596).
Он содержит точный принятый head `d54bd811759d18d2174d2fe4a5ce0fa7e20297db`
и его runtime-предка `1d87c1c1578ae85ad982d1ac6f050ce4af5b700f`.
Дерево merge commit совпало с принятым head; новый
[push CI42420635003](https://github.com/spikeal8-maker/Server-setings/actions/runs/42420635003)
для **40ca6116** завершился **success** в 22:27 UTC. Это отдельный результат
от прежнего PR CI42418846571 для d54bd811.

Слияние изменило GitHub `main`, без развёртывания или перезапуска на VM100.
Установленный MCP source остаётся `1d87c1c`; последующие коммиты PR72
документируют серверную и прямую ChatGPT приёмку. Датированные первичные
снимки выше сохранены без переписывания.

Следующий принятый срез X-PREP-01 создал полную копию текущего X в Яндексе,
проверил её расшифрование и подтвердил происхождение legacy-архива H04.
Подробности и незавершённые проверки восстановления —
[в инструкции backup/recovery](33-SCHOOL-YANDEX-RESTORE-1PANEL-QUICK-COMMANDS.md).
Специализированные lifecycle/cloud tools остаются выключенными; административные
действия выполнялись через уже принятый root shell, без их включения.

## Фактический результат: серверная приёмка PASS

**07.11.2026 21:40:21 UTC / 08.11.2026 00:40:21 МСК** публичный endpoint
`https://x.asa-lab.ru/mcp` прошёл полную серверную приёмку с **16 инструментами
и реальным выполнением команды от UID0**. Установленный и проверенный source SHA —
`1d87c1c1578ae85ad982d1ac6f050ce4af5b700f`. Последующее обновление этой документации
фиксирует результаты; код на сервере остаётся именно этой версии.

На момент этой серверной квитанции **21:40 UTC подключение приложения в ChatGPT
ещё не было принято:** автоматизация интерфейса остановилась на ограничении
доступа browser к host. Владелец завершил подключение позднее; его прямая приёмка
22:00 UTC зафиксирована выше. Перенос ASA и нагрузочная проверка не выполнялись.

| Проверка на VM100 | Наблюдаемый результат |
| --- | --- |
| Доставка | SHA256 архива и manifest всех 35 source files совпали с опубликованной версией |
| Реальная граница полномочий | `test_host_admin_profile.py`: 23/23 PASS, 0 SKIP, 0 failures, 0.746 s; семь локально пропущенных UID-проверок закрыты на VM100 |
| Старый gateway | Target исправлен на base `http://127.0.0.1:39443`; публичная OAuth/11-tool baseline PASS до switch |
| Приватный backend | 16 tools, root UID0, официальный `get_system_info` получил API code 200 |
| Публичный MCP | OAuth S256/PKCE, resource `/mcp`, stateless initialize, exact16, hostname/installation identity PASS; запросы без авторизации и с неверным token получили 401 |
| Root-команда | Собственный marker записан однократно; повтор idempotency key вернул прежнюю операцию; независимое чтение и отдельный cleanup PASS |
| Журналы | `operation_get/list` и operation logs проверены; marker удалён, durable root audit сохранён |
| Службы | Backend, executor socket, OAuth gateway, legacy 1Panel MCP и FRP: active + enabled |
| Локальные порты | `39443`, `39446`, `39450` слушают только `127.0.0.1` |
| Service account | UID995, только собственная группа `asa-school-mcp-admin`; чтение ASA `.env` от этого UID отклоняется |
| Private files | Socket `0600`; root audit и service state `0700`; `asa-operations.env` root-owned `0600` |
| OAuth | Поля сохранены, кроме предусмотренной замены private backend Bearer |
| Приложение | Все 8 container IDs, start times, restart counts и images совпали до/после; metadata защищённых файлов совпала; health ready/revision/schema остались прежними |

Health до и после показывает revision `cdb77ed6ac003ef2eb4635b9f55c4eb1c9a35bfa`,
schema `300`, database up, credential configuration valid и synchronized=true.
При этом `artifactIntegrity=unknown` сохранён как наблюдаемый факт: ответ ready
не подменяет проверку происхождения артефакта или готовности к production migration.

Операции публичной приёмки: write `38eb10a112694785a7e1f65652be95ca`,
независимый read `57b5d1d65a374438bcc64509f436c80a`, cleanup
`e9368a20ba8a4c3cb7a0bbe0e2a90140`. Они относятся к собственному тестовому marker,
а не к данным ASA. Добавлен тестовый OAuth client; токены и пароль не опубликованы.

**Границы этапа 21:40 UTC:** подключение ChatGPT на том этапе ещё не было принято
(закрыто отдельной проверкой 22:00 UTC). Специализированные ASA update/restore/cloud
tools, обновление приложения, миграция и нагрузочные тесты этим этапом не
подтверждались. Полный root shell работает; специализированный lifecycle остаётся
выключенным.
Откат проверен unit-тестами, рабочая 11-tool baseline принята до switch;
**live rollback после успешного switch не выполнялся**. Bootstrap использовал
существующий pinned SSH → Proxmox → QEMU Guest Agent, без нового туннеля и
изменений tailnet policy.

Первичный очищенный снимок: [school-host-admin-live.json](../snapshots/2026-11-07/school-host-admin-live.json).
Исторический [candidate snapshot](../snapshots/2026-11-07/school-host-admin-candidate.json)
сохраняет состояние до развёртывания и не переписан задним числом.

## Назначение и границы

Кандидат добавляет явный профиль `operations_mode=host-admin-only` в существующий
`asa_ops`. Владелец разрешил административные команды уровня root. Публичная
точка остаётся `https://x.asa-lab.ru/mcp`, а существующий OAuth сохраняет issuer,
пароль владельца и постоянное хранилище. Выпуск этого кода сам по себе не означает
подключения приложения в ChatGPT или готовности ASA Lab к переносу пользователей.

Цель — **VM100 `asa-school-01`**, установка `asa-school-vm100`. Приватный каталог
`/srv/asa-lab` остаётся root:root `0700`. Фактический Compose-проект VM100 —
**`asa-lab-school`**; имя `asa-lab-dev` из исторического эталона H03 к этой машине
не применяется. Installer читает существующие `instanceId`, `profile` и `project`
из `.asa/installation.json`; не создаёт фиктивную установку, не импортирует код
приложения и не запускает `prepare-asa-maintenance.py` для нового профиля.

## Разделение полномочий

| Компонент | Полномочия и доступ |
| --- | --- |
| Существующий OAuth gateway | Тот же домен, issuer и storage; слушает `127.0.0.1:39450` |
| Новый HTTP adapter | Отдельный `asa-school-mcp-admin`, без дополнительных групп, порт `127.0.0.1:39446`; отдельный Bearer |
| Хранилище операций | `/var/lib/server-settings/asa-operations`, владелец — UID сервиса; SQLite, планы, состояния и очищенные журналы |
| Root executor | Существующий механизм `asa-admin-executor.socket` и `asa-admin-executor@.service`; socket `0600`, `SO_PEERCRED`, root-owned код, Python `-I` |
| Приложение | Root-owned `/srv/asa-lab`; сервис не получает право чтения или владения каталогом, не включается в группу Docker |
| Приватный аудит root | `/var/lib/server-settings/asa-admin-executor`, `0700`; запрос, durable status и сырой вывод `0600` |

Adapter публикует ровно **16 инструментов**: существующие 11 инструментов 1Panel
и `asa_host_inventory`, `asa_admin_status`, `asa_admin_execute`,
`asa_operation_get`, `asa_operation_list`. Update, restore, cloud и другие ASA
lifecycle-инструменты отсутствуют в `tools/list`; прямой вызов тоже отклоняется
с `CAPABILITY_DISABLED` до reconciliation или доступа к приложению. Старый
lifecycle-профиль и его запрет root-owned установки остаются прежними.

`asa_admin_execute` даёт авторизованному владельцу настоящий root shell, включая
администрирование системы. Это полномочие не ограничено каталогом приложения.
Каждая операция привязывается к hostname, installation ID и текущему instance ID.
Root executor повторно читает authoritative metadata перед командой. Inventory
выполняется через этот же executor; результат содержит только выбранные поля
контейнеров, без environment, labels, команд запуска и mounts.

Повтор с тем же idempotency key возвращает существующую операцию. Потеря worker
или соединения не разрешает повторное исполнение. `operation_get/list`
обнаруживают потерянный worker и показывают `recovery_required`, не читая
root-private `.asa` через UID сервиса. `asa_admin_status` с operation ID
согласовывает завершённый результат с durable root audit; после этого get/list
показывают `success` или `failed`. Неопределённый результат остаётся
`recovery_required` и никогда не запускается автоматически повторно.

Для обычного ответа известные секреты из app `.env`, `mcp.env`,
`asa-operations.env`, gateway env, owner-password и 1Panel key заменяются до
ограничения ответа 32 KiB. Сырой журнал ограничен 8 MiB и остаётся root-only;
его чтение для очистки тоже ограничено. Это защита известных локальных секретов,
а не распознавание всех возможных новых секретов в произвольной команде.

## Предварительные условия

Нужны Python 3.12, `bash`, `runuser`, `ss`, `systemctl`, рабочий Docker и
**`setfacl` из Ubuntu-пакета `acl`**. Installer сам пакеты не устанавливает.
На VM100 оператор подтвердил 07.11.2026 установку только `acl=2.3.2-1build1.1`:
один новый пакет, без upgrade/removal и без перезапуска служб. Перед применением
preflight всё равно проверяет наличие `setfacl` и `runuser`.

Используется минимальный ACL для прохода через существующие приватные каталоги
и запуска официального MCP binary. Каталоги приложения не открываются для
сервиса. Необходимы root-owned исходники и доверенные родители без group/world
write, свободный `39446`, отсутствие прежнего enrollment или активных/прерванных
операций, существующий full 1Panel MCP и точный hostname VM100.

Исходники должны быть получены из проверенного GitHub commit. Для git checkout
проверяются точный HEAD и чистота `scripts/` и `templates/`; для portable bundle —
root-controlled `source-manifest.json` с exact SHA и SHA256 каждого файла.
Manifest является квитанцией доверенной доставки из review commit: он не заменяет
проверку происхождения bundle оператором. Не использовать плавающий `main`.

Минимальный source-only bundle содержит все `scripts/asa_ops/*.py`,
`install-asa-operations.sh`, `install-school-host-admin.py`, обе school MCP
acceptance-программы и три шаблона `asa-operations.service`,
`asa-admin-executor.socket`, `asa-admin-executor@.service`. Исправленный старый
gateway installer можно включить как исходник; повторно запускать его на VM100
не требуется. Секретов, конфигурации live, базы и данных ASA в bundle нет.

## Порядок применения на VM100

Все команды выполняются root из каталога проверенного release. Сначала план;
`--apply` используется только после просмотра результата. Установка backend и
переключение gateway — отдельные шаги, чтобы сохранить рабочую точку отката.

### 1. Исправить существующий маршрут и принять 11 инструментов

Обнаружена конкретная ошибка старого ExecStart: target
`http://127.0.0.1:39443/mcp` объединяется proxy с входящим `/mcp` и даёт
`/mcp/mcp`. Правильный target — **base URL без `/mcp`**. Это не изменение
публичного endpoint, issuer или правил OAuth.

```bash
python3 -B scripts/install-school-host-admin.py --repair-gateway-route
python3 -B scripts/install-school-host-admin.py --repair-gateway-route --apply --expected-host asa-school-01
python3 -B scripts/school-mcp-oauth-e2e-acceptance.py
```

Repair изменяет только известную старую строку в существующем unit, сохраняет
root-only копию `asa-school-mcp-route.before.service`, проверяет effective unit
и откатывает unit при неуспешном restart. Неизвестный drift блокирует операцию.
OAuth env, owner-password и storage не меняются. Legacy acceptance допускает
302 и 303 после consent, проверяет точный callback и state; ожидает прежние
11 инструментов. Если публичная приёмка не прошла, дальнейший enrollment
останавливается. Этот исправленный рабочий gateway будет baseline для rollback.

### 2. Установить только приватный backend

Для portable release точный SHA берётся из проверенной квитанции доставки:

```bash
reviewed_sha="$(python3 -c 'import json; print(json.load(open("source-manifest.json"))["source_sha"])')"
python3 -B scripts/install-school-host-admin.py
python3 -B scripts/install-school-host-admin.py --apply --enable-full-admin --expected-host asa-school-01 --source-sha "$reviewed_sha"
```

Installer создаёт отдельный service account, root-controlled profile и private
credentials. PANEL_HOST и API token наследуются локально из существующего
root-only `mcp.env`; новый Bearer генерируется локально и не выводится. Root
executor получает фактическую identity приложения и перечень redaction-файлов.
Затем используется общий installer с явной веткой host-only и включается socket.

До успешной квитанции проверяются: unauthenticated POST `401`, authenticated
initialize, точный набор 16 инструментов, UID0/hostname/instance executor и
единственный listener `127.0.0.1:39446`. Ошибка отключает новые службы и удаляет
созданные конфиги/units; приватный аудит, state и отключённый account сохраняются
для диагностики. Gateway на этом шаге остаётся на принятом 11-tool baseline.

### 3. Переключить существующий gateway и принять полный путь

```bash
python3 -B scripts/install-school-host-admin.py --switch-gateway
python3 -B scripts/install-school-host-admin.py --switch-gateway --apply --enable-full-admin --expected-host asa-school-01
python3 -B scripts/school-mcp-host-admin-acceptance.py
python3 -B scripts/school-mcp-host-admin-acceptance.py --apply
```

Переключение сохраняет root-only backups unit/env в
`/var/lib/server-settings/asa-school-host-admin-rollback`, меняет только private
Bearer и добавляет drop-in с base target `http://127.0.0.1:39446`. Drop-in
заменяет dependency на `asa-operations.service`: остановка старого HTTP11
сервиса не должна останавливать новый gateway. Старый HTTP11 сервис сохраняется
для отката. Перезапускается только gateway; OAuth storage/пароль/issuer остаются.

Новая публичная приёмка использует owner-password только локально из `0600` файла,
проходит OAuth S256/PKCE, проверяет issuer/resource/state/callback, invalid и
missing Bearer `401`, authenticated initialize и exact16. Stateless backend не
обязан выдавать session header. `inventory/status` должны подтвердить точный
узел, installation ID, instance и UID0.

Затем отдельная admin-операция создаёт собственный root-only marker в
`/var/lib/server-settings/asa-mcp-acceptance`, повтор с тем же ключом обязан
вернуть тот же operation ID. Независимая read-операция доказывает единственную
запись, cleanup-операция удаляет только свой marker. `operation_get/list` и
журналы проверяются в ходе приёмки. На неопределённом результате повтор команды
и обещание cleanup запрещены; вывод FAIL требует чтения audit.

Приёмка создаёт один OAuth test client и оставляет durable audit. Пустой каталог
marker может сохраниться. Приложение, пользовательские данные и lifecycle не
вызываются. `chatgpt_connected` остаётся **false**: подключение в приложении
ChatGPT — отдельный наблюдаемый результат владельца.

## Откат

```bash
python3 -B scripts/install-school-host-admin.py --rollback-gateway
python3 -B scripts/install-school-host-admin.py --rollback-gateway --apply --expected-host asa-school-01
python3 -B scripts/school-mcp-oauth-e2e-acceptance.py
```

Откат удаляет только собственный drop-in и восстанавливает сохранённый env,
возвращая **исправленный base target `39443`**, а не старую ошибку `/mcp/mcp`.
При параллельном изменении unit, env или drop-in владельцем откат останавливается
и не перезаписывает его работу. Backend, root audit и принятые worker-операции
не удаляются и не перезапускаются. Повторный switch после rollback требует
отдельного просмотра сохранённого rollback-каталога; installer не затирает его.

## Доказательства и оставшаяся приёмка

Локально выполняются focused suites legacy host admin/operations/maintenance,
новая OAuth acceptance, installer transaction/manifest и host-only profile.
Проверяется как публикация tools, так и отказ прямого lifecycle dispatch.
Failure/recovery пути и secret boundary проверяются фиктивными данными.
Тесты не используют реальные credentials и не обращаются к live VM.

Контейнер review предоставляет только отображение UID0; отдельный service UID
здесь создать нельзя. Тест реального UNIX socket с `SO_PEERCRED` и разными UID
в таком окружении обязан отмечаться SKIP, а не PASS. Разделение UID дополнительно
доказывается private/public приёмкой на VM100. Числа tests/skips и текущая граница
доказательств записываются в snapshot этой работы; GitHub CI, actual deployment,
ChatGPT connection, перенос ASA и нагрузочная проверка не предполагаются.
