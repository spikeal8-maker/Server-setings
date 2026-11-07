# VM100: прямой MCP с полным администрированием узла

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
