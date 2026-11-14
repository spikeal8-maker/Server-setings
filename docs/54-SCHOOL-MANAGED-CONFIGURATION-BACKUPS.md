# Школьная VM100: конфигурации, журналы и ежедневная копия

## Проверка выпуска после обновления ASA Lab

Снимок сверяет установленный выпуск из защищённого файла
`/srv/asa-lab/.asa/installed-release.json` с текущим Compose-контейнером API.
Проверяются неизменяемая ссылка GHCR, локальный ID образа, SHA выпуска в OCI
метках, статус API и подключение обоих хранилищ журналов. Проверка повторяется
после экспорта. Если API перезапустился или его образ не совпал с установленным
выпуском, снимок останавливается до создания архива.

Формат конфигурационного архива v2 сохраняет сведения об установленном выпуске.
Изолированная проверка продолжает принимать архивы v1 с исходной зафиксированной
связкой выпуска и образа. На H03 архив v2 создан и расшифрован тестовым ключом.
Прочитаны три тестовые SQLite и оба ZIP нормализованных журналов.
[Снимок проверки H03](../snapshots/2026-11-12/h03-settings-backup-check.json).

В репозитории обновлены контрольные суммы неизменяемого пакета для штатной
установки. Переход действующего расписания 1Panel на этот пакет выполняется
отдельно, с проверкой перехода активного поколения. Прежние файлы неизменяемых
выпусков сохраняются для проверки истории. Скрипт действующей копии
PostgreSQL/MinIO и его контрольная сумма сохранены.

## Фактическое состояние на 8 ноября 2026 года

Первый архив конфигурации **создан, выгружен в Яндекс Диск, полностью скачан,
расшифрован и проверен в отдельном каталоге**. Независимая приёмка root:
**2026-11-08T09:53:41Z**. Это доказательство конкретного архива, указанного ниже.

Код опубликован в PR81/main `fb22071b`. **Settings timer установлен и принят
08.11.2026 в 13:45:49 МСК; он enabled/active, следующий запуск — 09.11.2026
в 04:00 МСК. Первый автоматический запуск NOT_RUN.** При установке копирование
не запускалось; PostgreSQL/MinIO timer 03:20 МСК не менялся. Это отдельная
фактическая приёмка установки, а не следствие публикации кода.

Хост: `asa-school-01`, установка `asa-lab-school`, instance
`fc0ba60fe1d946bba1694d73557cc11f`. Этот код не предназначен для Proxmox,
домашней тестовой Ubuntu или действующего пользовательского сервера.

## Что сохраняется

Школьный режим дополняет существующую копию PostgreSQL/MinIO. В нём:

- Выбранные конфигурации 1Panel/OpenResty, TLS, `1pctl`, root MCP и статические
  ключи OAuth gateway. FRP включает **оба** файла `frpc.toml` и `frps-token`.
- Настройки cloud-контейнера: `compose.json` и `rclone.conf`; выбранные ASA
  installation/collector конфигурации; 11 известных systemd units и один
  известный gateway drop-in. Полный allowlist находится в
  `scripts/school_settings_snapshot.py`, без значений секретов.
- Три SQLite-базы 1Panel (`core.db`, `agent.db`, `xpack.db`) через SQLite online
  backup API. Каждая копия проходит `PRAGMA integrity_check`. Это три отдельных
  согласованных снимка, а не общая транзакция сразу нескольких БД.
- Два ZIP сохранённых **нормализованных** журналов: `host` и `application` через
  существующий canonical `exportLogs`. Проверяются CRC, manifest, имена членов,
  SHA256/размеры/число записей каждой части и чтение JSONL.

Nginx include/TLS/auth dependencies разрешены только внутри шести заданных
корней. WAF `1pwaf/data/conf/waf.conf` и `www/stream.d/*.conf` проходят ограниченный
preflight. Изменившийся набор файлов, внешняя/динамическая зависимость или
неизвестные extended attributes останавливают снимок.

Архив не включает PostgreSQL/MinIO повторно, raw logs, целые деревья `www/sites`,
живую bbolt-БД gateway, operations history SQLite, приватный collector index,
Docker images, SSH/Tailscale/network настройки или offline age identity.

**Ограничения доказательства:** `coverageComplete=false` сохраняется — известные
исторические пробелы журналов не исчезли. `gatewayOAuthContinuity=false`:
после восстановления gateway нужна новая регистрация ChatGPT.

Пределы: до160 конфигурационных файлов, до2MiB на файл и8MiB суммарно;
до128MiB на SQLite; до256MiB на каждый ZIP; до512MiB ciphertext. Перед новым
снимком требуется4GiB свободного места. Источники проверяются повторно после
снимка. Manifest с исходными UID/GID/modes и приватными контрольными суммами
находится **внутри age**, не в публичном отчёте.

## Проверенный архив

| Параметр | Фактическое значение |
|---|---|
| Архив | `asa-school-config-01-20261108T092203Z-fc8b50ef.tar.age` |
| Назначение | `app:/School-VM100/Configuration-Backups` |
| Размер | 25 186 504 байта |
| Ciphertext SHA256 | `e772ecf0e98b9624f8231e9e7887310fd7080fbd10ecc28e8502e5f992afdbd0` |
| Payload | 54 конфигурации +3 SQLite +2 ZIP =59 файлов и manifest |
| Host ZIP | 504 части,201417 записей |
| Application ZIP | 16 частей,19596 записей |
| SQLite integrity | `core.db=ok`, `agent.db=ok`, `xpack.db=ok` |
| Live restore | Не выполнялся; `livePathsWritten=false` |
| Offline identity | Передана только через существующий SSH/QGA stdin в sealed memfd; на VM не сохранена |

Создание: native `d35f94e116b1418a8a693f4831d4a362`, exit0/16.979s.
Первый cloud шаг `5cab5904dfd94a81ba4640edfddd74fd` остановился после upload
с `CONFIG_DOWNLOAD_HTTP_STATUS`; полного успеха на этом этапе не было.
Read-only `d56cfc41cdbe4eae9a391f0c59919cd6` установил реальный **HTTP302**
и совпадение удалённого размера/ciphertext SHA.

Исправленный reader разрешает максимум3 HTTPS redirects; каждый адрес проходит
существующий Yandex allowlist. OAuth/cookie headers не передаются по signed URL.
Native `d1435b651f96477e9ab144812127912a` полностью скачал **тот же** объект,
exit0/2.938s, uploads0, folderWrites0. Размер и полный SHA256 совпали.

Первый D2 launcher PID39984 остановился в Python parser до выполнения payload.
Исправлены только кавычки launcher; verifier/courier остались прежними.
Stage PID35496 проверил код и строгий ACL. Courier **PID8348**, Python27748:
exit0/12.11s, guest exit0; durable verification SUCCESS. ACL ключа не менялся.

Исходная возрастная identity остаётся у владельца на съёмном носителе.
Значения ключей, токенов, содержимое конфигураций и приватные файловые SHA
в Git/таблицу не публикуются. Безопасный снимок подготовки:
[`school-settings-scheduler-prepared-20261108.json`](../snapshots/2026-11-08/school-settings-scheduler-prepared-20261108.json).

## Код и закреплённые зависимости

| Файл | Назначение |
|---|---|
| `scripts/backup-settings.sh` | Существующий entrypoint; добавлен школьный режим `--school-vm100` |
| `scripts/school_settings_snapshot.py` | Ограниченный снимок, online SQLite, canonical ZIP, age, изолированная проверка восстановления |
| `scripts/school-settings-cloud-roundtrip.py` | Отдельный Configuration-Backups namespace, immutable upload, full retained download |
| `scripts/school-nightly-settings-backup.py` | Датированный nightly результат, общие locks, окно и failure receipts |
| `scripts/install-school-nightly-settings-backup.py` | Декларативные units и установка точного immutable release |

Generic mode существующих entrypoints сохранён. `school_yandex_app_folder.py`
и `cloud_storage.py` не изменены; школьная конфигурационная копия использует
их закреплённые версии и существующую OAuth application folder авторизацию.
`Database-Backups` остаётся отдельным namespace.

Helper закреплён на ASA revision `3e8713476cafedda3e24a39665458e833efd7c22`,
API image `sha256:8c75744e9ffa805b9f10fd08755b55de2148bf4b420fa008e78ffc28aec4e36f`
и exporter blob `47e562c63f0246648736461aabc31c70bac670ce`. DB interlock source:
`db883469c107dc9d5459fec33b5a76e91bfe9fa5`. Смена этих зависимостей требует
проверенного обновления pins; новый runtime не принимается автоматически.

## Ежедневный запуск: установленный контракт

Новый timer `asa-school-nightly-settings-backup.timer` назначен на
**04:00 Europe/Moscow**. Существующий PostgreSQL/MinIO timer03:20 не меняется.
Runner ждёт общий `/run/lock/asa-school-nightly-db.lock` не более600s,
затем берёт `/run/lock/asa-school-host-config-backup.lock`. Оба lock удерживаются
на протяжении создания архива и cloud transfer. Если lock занят сверх предела,
запуск даёт видимый STOP и не обходит блокировку.

Один запуск ограничен1800s. Новое выполнение разрешено только в04:00–05:00
по Москве; время повторно проверяется после ожидания lock. `Persistent=false`:
если timer был выключен в назначенное время, пропущенный запуск не переносится
автоматически на время следующего включения. Задержанный вызов после05:00
останавливается. Автоматического retry после STOP и дневного manual bypass нет;
следующий обычный запуск — следующие04:00. Пропуск виден по отсутствию свежего
`latest-attempt`/`latest-success` и состоянию timer.

Установка создаёт прямой root-private release
`/opt/asa-school-settings-backup/releases/ffb8bf2a3540cb55`, без `current` symlink.
В release шесть public code files с проверенными SHA256, mode0600; каталоги0700.
Обе units имеют mode0644. Существующее грязное `/srv/Server-setings` не
переключается, не обновляется через pull и не сбрасывается через reset.

Для воспроизводимой установки из отдельного проверенного checkout:

```bash
sudo python3 scripts/install-school-nightly-settings-backup.py --repository-source
```

Это read-only preflight. После независимого review и разрешения на конкретную
установку используется тот же закреплённый checkout:

```bash
sudo python3 scripts/install-school-nightly-settings-backup.py --repository-source --apply
```

Installer проверяет host/instance, code pins, существующий DB timer, отсутствие
конфликтующих новых units и baseline restore proof. За10min до next-fire установка
блокируется. Приёмка установки требует: новый timer enabled/active, следующий
fire строго04:00MSK, а service остаётся never-started. **Backup во время установки
не запускается.** При сбое активации отключается только новый timer; созданные
файлы сохраняются для review, старые архивы не удаляются.

## Наблюдение и честные статусы

Runner записывает root-private JSON в
`/var/backups/server-settings/school-nightly-settings`:

- `latest-attempt.json` — текущая/последняя попытка и её окончательный исход.
- `latest-success.json` — последняя успешная полная проверка cloud ciphertext.
- `latest-failure.json` — последний STOP; прежний success при ошибке сохраняется.

Ночной успех называется `CONFIG_NIGHTLY_FULL_DOWNLOAD_SHA256_PASS`.
`nightlyRestoreValidated=false`: новая ночная копия не расшифровывается автоматически.
`baselineIsolatedRestore` отдельно хранит архив и дату уже принятой ручной проверки.
Offline private key ради scheduler на VM не переносится.

Для диагностики:

```bash
sudo systemctl show asa-school-nightly-settings-backup.timer -p UnitFileState -p ActiveState -p NextElapseUSecRealtime
sudo systemctl show asa-school-nightly-settings-backup.service -p Result -p ExecMainStatus
sudo journalctl -u asa-school-nightly-settings-backup.service --no-pager -n 30
```

STOP печатается безопасным JSON с priority3. Unit использует
`SyslogLevelPrefix=yes`; это штатное [поведение systemd](https://www.freedesktop.org/software/systemd/man/260/systemd.exec.html#SyslogLevelPrefix=).
Существующий `linux:journal` collector читает warning/error всех units:
`collect_logs.py` blob `3f8c858eea637a128d9a7b8cd68b95628f537e6a`.
Новый collector source не добавляется. Это проверенный по исходникам маршрут;
доставка конкретной ошибки нового timer ещё не испытана.

Если процесс вообще не стартовал — например, отсутствовала зависимость systemd —
смотреть unit Result/journal и возраст последней попытки. Старый success не означает
успех сегодняшнего запуска. Ночные receipt не подтверждают работу пользовательского
admin API, нагрузочную ёмкость или готовность перевода домена.

## Восстановление и хранение

Проверка расшифровки всегда отдельная: точное имя архива и ciphertext SHA,
скачанная копия, offline identity через утверждённый stdin маршрут, новый private
isolated directory. Helper проверяет manifest, все разрешённые пути/хеши/modes,
три SQLite и оба ZIP. Он не применяет настройки в живые `/etc`/`opt` и не
перезапускает службы. Service UIDs нужно сопоставить с заново установленными
службами; OAuth gateway потребует новой регистрации ChatGPT.

**Первый архив из54 конфигураций не содержит новые scheduler units/runner/installer.**
Они декларативно восстанавливаются из этого закреплённого Git-кода. Включение новых
путей в последующие encrypted snapshots отдельно не разрешалось. `--repository-source`
не ослабляет host/instance/restore proof/pin проверки; новая чистая Ubuntu в рамках
этой задачи не проверялась.

Retention пока **append-only**: нет удаления local/cloud archives или прежних
verified trees. Политику удаления следует вводить отдельным reviewed изменением
с dry-run и защитой проверенных опорных копий. Недостаток свободного места даёт
STOP, а не автоматическую очистку.

## Проверки

Локально PASS:14 restore-contract tests (включая настоящий online SQLite backup
с открытым writer и committed WAL),4 redirect regression tests и4 focused
scheduler tests. Последние проверяют реальный flock в обе стороны, отказ при
изменении public code, failure receipt без утечки exception contents и разделение
transfer/decrypt статусов. Scheduler filesystem tests требуют root-owned fixture
tree и при непривилегированном CI явно пропускаются; они выполнены отдельно сroot.

Три прежних generic cloud tests и синтаксис проверяются совместно с новым кодом.
Реальная криптография/cloud/canonical export/isolated restore доказаны конкретным
первым архивом. Установка timer теперь принята отдельно; первый автоматический
запуск пока NOT_RUN, `nightlyRestoreValidated=false`.

## Принятая установка и заключительная проверка — 08.11.2026

[Безопасный результат установки](../snapshots/2026-11-08/school-settings-timer-install-4606bb0b-20261108.json) и [независимое принятие](../snapshots/2026-11-08/school-settings-timer-root-acceptance-4606bb0b-20261108.json) сохраняют
операцию `4606bb0b95264d548aab2d134289e926`: exit 0 / 1,415 с, фактическая установка
в 10:45:07.272068 UTC, root review в 10:45:49 UTC. Установлен ровно release
`ffb8bf2a3540cb55`; полный release SHA и source commit находятся в receipt.

[Заключительный read-only health](../snapshots/2026-11-08/school-final-health-metadata-23507486-20261108.json) в 10:46:23 UTC подтвердил оба
timer enabled/active и следующий запуск 09.11: database в 00:20 UTC (03:20 МСК),
settings в 01:00 UTC (04:00 МСК). Service settings ни разу не запускался, install
receipt проверен; все шесть сервисов приложения healthy. Проверка ограничена
метаданными: authenticated API и native integrity повторно не оценивались.

Offline key на VM не устанавливался, старые архивы не удалялись, настройки
data timer не менялись. Исходный snapshot подготовки с NOT_INSTALLED сохранён
неизменным как история. Первый архив по-прежнему не содержит новые units и
runner: их источник восстановления — опубликованный закреплённый Git-код.
