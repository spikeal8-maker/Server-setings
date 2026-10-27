# ASA Lab P0 — безопасное восстановление синтетического browser fixture

Issue #19. Только тестовый хост admins-MS-7D22, установка asa-test,
HTTPS origin https://test.asa-lab.ru, релиз ASA Lab
4957352d412fb532afae1830ebc9812b609a1497.

## Причина и ограничения

HTTP 401 на teacher_login_submit не доказывает неисправность ASA Lab.
Предшествующая read-only диагностика нашла registered=true в локальном fixture
при отсутствии соответствующей учётной записи в PostgreSQL. Старый браузерный
runner пропускает регистрацию на основании локального флага. Из PR #15
перенесены исправления селекторов к актуальным DOM ID без слияния старого PR.

В этом PR реализован только код и офлайн-регрессии. Живые БД, Docker,
учётные записи, резервные копии и действующее acceptance_pending не изменены.

## Защита

- Привязка к точным host, installation ID, HTTPS origin, текущей
  generation ID и уникальному имени fixture. Устаревший файл может быть
  принят только из штатного generation-suffixed пути с явной меткой
  синтетического аккаунта и корректным адресом вида asa-ops-* @example.invalid.
- Прямая проверка accounts, legacy users и profiles в PostgreSQL через
  штатный Compose postgres/psql. Транзакция READ ONLY; командная строка
  не содержит synthetic идентификаторов. Ненулевой или недостоверный
  результат блокирует изменение.
- Разрешён только **собственный** запущенный MCP `asa_admin_execute`: запись
  `admin/running` в реальном SQLite Store с совпадающей boot/start PID identity,
  root-аудит `asa_admin_status` через защищённый Unix-сокет и доказанная цепочка
  предков `/proc` от вызывающего Python до root command PID. Чужой
  `admin/running`, любые `queued/running` другого типа и каждый
  `recovery_required` (даже без живого PID) — запрет.
- Индекс Store `one_active_installation` и общий неблокирующий flock
  с настоящей браузерной приёмкой остаются обязательными. При ошибке
  доверенного root-аудита или невозможности прочитать `/proc` — запрет.
- Приватный versioned audit archive (0600, каталог 0700), журнал без секретов.
  Новые реквизиты создаются криптографическим генератором. Все старые
  class/student/course/project references сбрасываются.
- После HTTP 409 при повторной реальной регистрации восстановление возможно
  только через успешный настоящий логин и подтверждение identity через
  /api/auth/me. Никаких имитаций HTTP, SQL INSERT, bypass BotCheck, password reset
  или принудительного выставления checks=true.
- Новая заявка repair возвращает стабильный operation_id; повторное применение
  исходного SHA не создаёт новую identity и не меняет старый пароль.

## Отдельный следующий этап: только после review и exact-SHA CI

**В этой задаче никакие операции на тестовом сервере не выполнять.**
Сначала независимо принять PR, затем отдельно санкционировать deployment
частного backend Server-setings без ASA Lab OCI update и без изменения БД.

1. Проверить через штатный MCP 1Panel `asa_admin_status`, что root executor
   доступен, разрешён администратором и находится на admins-MS-7D22 /
   asa-test с правильной generation. Если есть `recovery_required`, сначала
   завершить штатное reconciliation по его настоящему operation_id.
   Исчезновение PID, ручная правка SQLite и удаление lock НЕ являются
   reconciliation.
2. Для предварительного плана вызвать **именно** `asa_admin_execute` с
   `installation_id=asa-test`, `cwd=/opt/server-settings/asa-operations`,
   `timeout_seconds=180`, отдельным уникальным `idempotency_key` длиной
   16–128 символов (например, `fixture-plan-20261027-owner-001`) и
   следующей командой. Корневой executor запускает её как свой дочерний
   процесс; Python обязательно выполняется от владельца `admins`:

   ```bash
   sudo -u admins env PYTHONPATH=/opt/server-settings/asa-operations python3 -m asa_ops.acceptance_repair --config /etc/server-settings/asa-operations.json --plan
   ```

3. Получить вывод только через `asa_operation_get` и/или
   `asa_admin_status` для полученного operation_id. Должно быть
   `status=eligible`, `database_matches=0` и `fixture_sha256` из 64
   символов. Надёжно сохранить **ровно этот SHA** для отдельного решения
   владельца. Если операция показала любой BLOCKED/error, остановиться.
4. После отдельного явного разрешения применить **новую**
   `asa_admin_execute` операцию, с новым idempotency_key (например,
   `fixture-apply-20261027-owner-001`). В `command` передать тот же
   Python-запуск под `sudo -u admins`, но с `--apply`,
   `--authorize-installation asa-test` и
   `--expected-fixture-sha256`, равным буквально ранее утверждённому
   64-символьному SHA. Не вычислять новый SHA автоматически вместо
   утверждённого и не подменять цель после разрешения. Команду apply
   составлять только после того, как SHA реально получен; не применять
   демонстрационные значения.
5. Если MCP-запрос прервался, повторить его с **тем же** idempotency_key и
   идентичными аргументами, предварительно проверить существующий результат
   через `asa_operation_get`. Store не запустит повторно root-команду;
   fixture также распознаёт повторное применение утверждённого SHA.
   Для отдельного исполнения, если оно требуется после подтверждённого
   отказа, нужен новый согласованный запрос. `recovery_required` никогда
   не разблокируется на основании отсутствия PID.
6. Только отдельным будущим заданием — один `asa_deployment_verify`
   по одобренному SHA ASA Lab `4957352d412fb532afae1830ebc9812b609a1497`
   с реальными браузерными сценариями. При любом FAIL оставить
   `acceptance_pending`, не форсировать успех.

**Запрещено:** SSH-обход для plan/apply, прямое исполнение CLI вне
`asa_admin_execute`, вмешательство в Operations SQLite или root audit,
удаление lock, изменение PostgreSQL, MinIO, пользовательских паролей,
Docker volumes и перезапуск ASA Lab OCI update. CLI без подтверждённого
MCP-предка возвращает `MCP_ADMIN_OPERATION_REQUIRED` либо
`MCP_ADMIN_PROOF_INVALID`, не правит fixture.

## Остаточный риск

Первый реальный browser E2E с новым аккаунтом запрещён в этой задаче.
Подтверждение готовности runtime-зависимостей (Docker Compose, psql, hostname,
права каталога) произойдёт только в следующем разрешённом этапе через --plan.
При несоответствии процесс блокируется, а не обходит проверки.
