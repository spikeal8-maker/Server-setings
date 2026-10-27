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
- Запрет при активных операциях Operations; общий неблокирующий flock
  с настоящей браузерной приёмкой.
- Приватный versioned audit archive (0600, каталог 0700), журнал без секретов.
  Новые реквизиты создаются криптографическим генератором. Все старые
  class/student/course/project references сбрасываются.
- После HTTP 409 при повторной реальной регистрации восстановление возможно
  только через успешный настоящий логин и подтверждение identity через
  /api/auth/me. Никаких имитаций HTTP, SQL INSERT, bypass BotCheck, password reset
  или принудительного выставления checks=true.
- Новая заявка repair возвращает стабильный operation_id; повторное применение
  исходного SHA не создаёт новую identity и не меняет старый пароль.

## Отдельный следующий этап: сначала review, точный CI, только затем server repair

1. Развернуть проверенную версию частного backend Server-setings отдельно.
   Ни в коем случае не обновлять OCI ASA Lab и не изменять его базу вручную.
2. Проверить Operations и штатную установку. Запускать под фактическим
   владельцем установки (на тестовом сервере ожидается admins).
3. Безопасный предварительный план:

    cd /opt/server-settings/asa-operations
    sudo -u admins env PYTHONPATH=/opt/server-settings/asa-operations python3 -m asa_ops.acceptance_repair \
      --config /etc/server-settings/asa-operations.json --plan

4. Должно быть status=eligible, database_matches=0 и fixture_sha256.
   Если найден аккаунт или проверка невозможна — STOP, исследовать HTTP 401,
   никаких правок fixture. После отдельного разрешения владельца:

    cd /opt/server-settings/asa-operations
    PLAN="$(sudo -u admins env PYTHONPATH=/opt/server-settings/asa-operations python3 -m asa_ops.acceptance_repair \
      --config /etc/server-settings/asa-operations.json --plan)"
    SHA="$(printf '%s' "$PLAN" | python3 -c 'import json,sys; x=json.load(sys.stdin); assert x["status"]=="eligible"; print(x["fixture_sha256"])')"
    sudo -u admins env PYTHONPATH=/opt/server-settings/asa-operations python3 -m asa_ops.acceptance_repair \
      --config /etc/server-settings/asa-operations.json --apply \
      --expected-fixture-sha256 "$SHA" --authorize-installation asa-test

5. Получить pending_registration или already_repaired.
   В отдельной операции координатора — один asa_deployment_verify по
   одобренному SHA 4957352d412fb532afae1830ebc9812b609a1497:
   настоящий teacher login, student login, course/project, Scratch и Electronics
   save/reopen. Только при полном PASS возможно обсуждать штатную финализацию.
   Любой FAIL сохраняет acceptance_pending; не форсировать success.

## Остаточный риск

Первый реальный browser E2E с новым аккаунтом запрещён в этой задаче.
Подтверждение готовности runtime-зависимостей (Docker Compose, psql, hostname,
права каталога) произойдёт только в следующем разрешённом этапе через --plan.
При несоответствии процесс блокируется, а не обходит проверки.
